"""ChainInfo 缓存 - 轻量重连核心 (C-2 修复)。

ToolDelta 的"机器人退出重进"实际上是 RakNet 重连 + 复用缓存的 chainInfo,
而非全量重新认证。本模块实现同样的轻量重连机制:

    层级 1 (Scheme A - 零认证):
        直接复用缓存的 chainInfo + server_address, 仅 RakNet 重连。
        chainInfo 有效性: ~1-6 小时 (取决于网易 token TTL)。

    层级 2 (Scheme B - 轻量认证):
        chainInfo 过期后, 用缓存的 LoginSRCToken 只走 /authentication-otp,
        跳过 /login-otp (减少 50% 认证频率)。

    层级 3 (全量认证):
        上述都失败时, 回退到完整 login-otp + authentication-otp。

缓存键: (account_id, server_code)
缓存位置: backend/data/chain_info_cache.json
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import DATA_DIR
from ..logger import get_logger

logger = get_logger("auth.chain_cache")

#: 默认缓存文件路径
DEFAULT_CACHE_FILE: Path = DATA_DIR / "chain_info_cache.json"

#: chainInfo 默认 TTL (秒) - 保守估计 2 小时
DEFAULT_CHAIN_TTL: float = 7200.0

#: LoginSRCToken 默认 TTL (秒) - 保守估计 6 小时
DEFAULT_TOKEN_TTL: float = 21600.0


@dataclass
class CachedAuthData:
    """缓存的认证数据 (按 account_id + server_code 索引)。

    Attributes:
        account_id: 账号 ID。
        server_code: 租赁服编号。
        chain_info: JWT chain 数据 (Scheme A 使用)。
        server_address: 游戏服务器地址 (host:port)。
        login_src_token: 网易 LoginSRCToken (Scheme B 使用)。
        login_md5_token: LoginSRCToken 的 MD5。
        uid: 网易 UID。
        player_name: 玩家名。
        h5token: Base64 解码后的 LoginSRCToken (认证服务器用)。
        cached_at: 缓存时间戳 (chainInfo 的 TTL 基准)。
        token_cached_at: LoginSRCToken 的缓存时间戳 (token 的 TTL 基准)。
        chain_ttl: chainInfo 有效期 (秒)。
        token_ttl: LoginSRCToken 有效期 (秒)。
    """
    account_id: str = ""
    server_code: str = ""
    chain_info: str = ""
    server_address: str = ""
    login_src_token: str = ""
    login_md5_token: str = ""
    uid: str = ""
    player_name: str = ""
    h5token: str = ""  # base64 编码的 bytes, 存为字符串
    cached_at: float = 0.0
    # BUG-8.2 修复: 为 token 单独维护时间戳, 避免部分更新 (仅 token) 时
    # 重置 cached_at 导致 chain 的 TTL 被错误延长。
    token_cached_at: float = 0.0
    chain_ttl: float = DEFAULT_CHAIN_TTL
    token_ttl: float = DEFAULT_TOKEN_TTL

    def is_chain_valid(self, now: Optional[float] = None) -> bool:
        """chainInfo 是否仍在有效期内 (Scheme A 可用)。"""
        if not self.chain_info or not self.server_address:
            return False
        now = now or time.time()
        # BUG-8.5 修复: from_dict 不校验字段类型, 若缓存文件损坏导致
        # cached_at 为非数字类型, 减法会抛 TypeError。捕获后返回 False
        # (视为已过期), 避免崩溃。
        try:
            return (now - self.cached_at) < self.chain_ttl
        except TypeError:
            return False

    def is_token_valid(self, now: Optional[float] = None) -> bool:
        """LoginSRCToken 是否仍在有效期内 (Scheme B 可用)。"""
        if not self.login_src_token:
            return False
        now = now or time.time()
        # BUG-8.2 修复: token 有效性基于 token_cached_at (若已设置),
        # 否则回退到 cached_at (兼容旧缓存数据)。
        ref = self.token_cached_at if self.token_cached_at > 0 else self.cached_at
        # BUG-8.5 修复: 同 is_chain_valid, 防止 ref 为非数字时 TypeError 崩溃。
        try:
            return (now - ref) < self.token_ttl
        except TypeError:
            return False

    def age_seconds(self, now: Optional[float] = None) -> float:
        """缓存存活时间 (秒)。"""
        now = now or time.time()
        return now - self.cached_at

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CachedAuthData":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class ChainInfoCache:
    """ChainInfo 缓存管理器。

    负责:
        1. 按 (account_id, server_code) 索引缓存认证数据
        2. 持久化到 JSON 文件 (启动加载 / 变更保存)
        3. 提供 TTL 过期检查

    典型用法::

        cache = ChainInfoCache()
        cache.load()

        # 检查是否有有效缓存 (Scheme A)
        cached = cache.get("acc-123", "1895088")
        if cached and cached.is_chain_valid():
            # 直接用 chain_info + server_address, 无需认证
            ...

        # 认证成功后更新缓存
        cache.update("acc-123", "1895088", chain_info="...", ...)
    """

    def __init__(self, file_path: Optional[Path] = None) -> None:
        self._file_path: Path = Path(file_path) if file_path else DEFAULT_CACHE_FILE
        self._cache: Dict[str, CachedAuthData] = {}
        self._lock = threading.RLock()
        self._loaded: bool = False

    @staticmethod
    def _key(account_id: str, server_code: str) -> str:
        """缓存键: ``account_id\\x1fserver_code``

        BUG-8.3 修复: 之前使用 ``:`` 作为分隔符, 若 account_id 中包含 ``:``,
        可能导致键冲突 (如 "a:b" + "c" vs "a" + "b:c")。改用 ASCII 单元分隔符
        ``\\x1f`` (Unit Separator), 该字符不会出现在正常的账号 ID 或服务器编号中。
        """
        return f"{account_id}\x1f{server_code}"

    def load(self) -> None:
        """从磁盘加载缓存。"""
        with self._lock:
            if not self._file_path.exists():
                self._cache = {}
                self._loaded = True
                return
            try:
                with open(self._file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if not isinstance(raw, dict):
                    self._cache = {}
                    self._loaded = True
                    return
                cache: Dict[str, CachedAuthData] = {}
                for key, val in raw.items():
                    if isinstance(val, dict):
                        try:
                            cache[key] = CachedAuthData.from_dict(val)
                        except TypeError:
                            # BUG-8.6 修复: 之前 from_dict 失败时静默跳过,
                            # 不记录任何日志, 难以排查缓存加载问题。现增加日志。
                            logger.warning(
                                f"ChainInfo 缓存条目 {key} 反序列化失败, 已跳过"
                            )
                            continue
                self._cache = cache
                self._loaded = True
                logger.info(f"已加载 {len(cache)} 条 ChainInfo 缓存")
            except (json.JSONDecodeError, OSError) as exc:
                logger.error(f"ChainInfo 缓存加载失败: {exc}")
                self._cache = {}
                self._loaded = True

    def save(self) -> None:
        """持久化缓存到磁盘 (原子写入)。"""
        with self._lock:
            self._file_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": "1",
                "updated_at": time.time(),
                "entries": {k: v.to_dict() for k, v in self._cache.items()},
            }
            tmp = self._file_path.with_suffix(self._file_path.suffix + ".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._file_path)
                logger.debug(f"ChainInfo 缓存已保存: {len(self._cache)} 条")
            except (OSError, TypeError, ValueError) as exc:
                # BUG-8.1 修复: 之前仅捕获 OSError, 且异常时未清理临时文件。
                # json.dump 可能抛出 TypeError (不可序列化对象) 或 ValueError,
                # 这些异常会导致 .tmp 文件残留。现增加异常类型并清理临时文件。
                logger.error(f"ChainInfo 缓存保存失败: {exc}")
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def get(self, account_id: str, server_code: str) -> Optional[CachedAuthData]:
        """获取缓存的认证数据 (不检查 TTL)。"""
        self._ensure_loaded()
        with self._lock:
            # BUG-8.4 修复: 返回深拷贝而非缓存中的实际对象引用,
            # 防止调用方意外修改返回值而污染内部缓存。
            cached = self._cache.get(self._key(account_id, server_code))
            return copy.deepcopy(cached) if cached is not None else None

    def get_valid_chain(
        self, account_id: str, server_code: str
    ) -> Optional[CachedAuthData]:
        """获取 chainInfo 仍有效的缓存 (Scheme A)。"""
        self._ensure_loaded()
        with self._lock:
            cached = self._cache.get(self._key(account_id, server_code))
            if cached and cached.is_chain_valid():
                logger.info(
                    f"ChainInfo 缓存命中 (Scheme A): "
                    f"account={account_id} server={server_code} "
                    f"age={cached.age_seconds():.0f}s"
                )
                # BUG-8.4 修复: 返回深拷贝防止缓存污染
                return copy.deepcopy(cached)
            return None

    def get_valid_token(
        self, account_id: str, server_code: str
    ) -> Optional[CachedAuthData]:
        """获取 LoginSRCToken 仍有效的缓存 (Scheme B)。"""
        self._ensure_loaded()
        with self._lock:
            cached = self._cache.get(self._key(account_id, server_code))
            if cached and cached.is_token_valid():
                logger.info(
                    f"LoginSRCToken 缓存命中 (Scheme B): "
                    f"account={account_id} server={server_code} "
                    f"age={cached.age_seconds():.0f}s"
                )
                # BUG-8.4 修复: 返回深拷贝防止缓存污染
                return copy.deepcopy(cached)
            return None

    def update(
        self,
        account_id: str,
        server_code: str,
        *,
        chain_info: str = "",
        server_address: str = "",
        login_src_token: str = "",
        login_md5_token: str = "",
        uid: str = "",
        player_name: str = "",
        h5token: str = "",
    ) -> None:
        """更新缓存 (认证成功后调用)。"""
        self._ensure_loaded()
        with self._lock:
            key = self._key(account_id, server_code)
            existing = self._cache.get(key)
            if existing:
                # 更新已有条目 (保留 TTL 设置)
                # BUG-8.2 修复: 之前无论更新什么字段都重置 cached_at, 导致
                # 仅更新 token 时 chain 的 TTL 也被延长。现改为:
                # - 仅在更新 chain_info 时重置 cached_at (chain TTL 基准)
                # - 仅在更新 login_src_token 时重置 token_cached_at (token TTL 基准)
                if chain_info:
                    existing.chain_info = chain_info
                    existing.cached_at = time.time()
                if server_address:
                    existing.server_address = server_address
                if login_src_token:
                    existing.login_src_token = login_src_token
                    existing.token_cached_at = time.time()
                if login_md5_token:
                    existing.login_md5_token = login_md5_token
                if uid:
                    existing.uid = uid
                if player_name:
                    existing.player_name = player_name
                if h5token:
                    existing.h5token = h5token
            else:
                now = time.time()
                self._cache[key] = CachedAuthData(
                    account_id=account_id,
                    server_code=server_code,
                    chain_info=chain_info,
                    server_address=server_address,
                    login_src_token=login_src_token,
                    login_md5_token=login_md5_token,
                    uid=uid,
                    player_name=player_name,
                    h5token=h5token,
                    cached_at=now,
                    # BUG-8.2 修复: 新建条目时同时初始化 token_cached_at
                    token_cached_at=now if login_src_token else 0.0,
                )
            self.save()
            logger.info(
                f"ChainInfo 缓存已更新: account={account_id} server={server_code}"
            )

    def invalidate(self, account_id: str, server_code: str) -> None:
        """删除指定缓存 (认证失败 / 封禁时调用)。"""
        self._ensure_loaded()
        with self._lock:
            key = self._key(account_id, server_code)
            if key in self._cache:
                del self._cache[key]
                self.save()
                logger.info(f"ChainInfo 缓存已失效: {key}")

    def cleanup_expired(self) -> int:
        """清理所有过期缓存, 返回清理数量。"""
        self._ensure_loaded()
        with self._lock:
            now = time.time()
            expired = [
                k for k, v in self._cache.items()
                if not v.is_token_valid(now)
            ]
            for k in expired:
                del self._cache[k]
            if expired:
                self.save()
                logger.info(f"清理 {len(expired)} 条过期 ChainInfo 缓存")
            return len(expired)


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
_global_cache: Optional[ChainInfoCache] = None
_global_cache_lock = threading.Lock()


def get_chain_cache() -> ChainInfoCache:
    """返回全局 ChainInfoCache 单例。"""
    global _global_cache
    with _global_cache_lock:
        if _global_cache is None:
            _global_cache = ChainInfoCache()
            _global_cache.load()
        return _global_cache


__all__ = [
    "CachedAuthData",
    "ChainInfoCache",
    "get_chain_cache",
    "DEFAULT_CHAIN_TTL",
    "DEFAULT_TOKEN_TTL",
]
