"""PocketTerm 设备指纹持久化系统 (MicroUQHolder)

本模块从 NexusE 逆向的 ``MicroUQHolder`` 系统提取, 实现设备指纹的
持久化存储与复用, 模拟"同一台设备反复登录"的行为模式。

逆向来源 (NexusE)
------------------

NexusE 的 ``uqholder`` 包维护以下核心结构:

- **BotBasicInfoHolder**: 机器人自身身份信息
  - BuildPlatform, DeviceID, ClientRandomID, DeviceOS
- **PlayerInfo**: 玩家信息 (UUID, XUID, PlatformChatID, Skin)
- **MicroUQHolder**: 统一管理身份信息, 提供持久化/加载/刷新

关键设计
--------

1. **持久化机制**: 所有指纹写入 ``backend/data/device_fingerprints.json``,
   每次启动自动加载, 登录时复用相同指纹。

2. **指纹过期与刷新**: 指纹可配置有效期 (默认 7 天), 过期后自动刷新部分字段
   (ClientRandomID 等), 但保留 DeviceID 等核心标识。

3. **登录统计**: 记录登录次数、上次登录时间、首次创建时间, 用于
   反作弊行为分析。

4. **多账号隔离**: 每个账号 (按 IdentityName 区分) 独立持有一份指纹。

对标 NovaBuilder 的 ``uqholder`` 包, 本模块将其适配为 Python 异步实现。

类组织
------

- :class:`DeviceFingerprint`  -- 设备指纹数据类
- :class:`UQHolder`           -- 统一身份管理器, 持久化/加载/刷新
- :class:`BotBasicInfo`       -- 机器人自身信息
- :class:`PlayerRecord`       -- 玩家记录
- :class:`FingerprintStore`   -- JSON 持久化存储
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import threading
import time
import uuid as _uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import DATA_DIR
from ..logger import get_logger

logger = get_logger("auth.uqholder")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 默认指纹存储路径
DEFAULT_FINGERPRINT_STORE: Path = DATA_DIR / "device_fingerprints.json"

#: 默认指纹有效期 (秒) - 7 天
DEFAULT_FINGERPRINT_TTL: float = 7 * 24 * 3600.0

#: 默认 BuildPlatform 映射 (来自 NexusE 逆向)
BUILD_PLATFORM_NAMES: Dict[int, str] = {
    0: "Unknown",
    1: "Win32",
    2: "macOS",
    7: "Linux",
    8: "Android",
    9: "iOS",
    10: "Nintendo Switch",
    11: "Windows 10 UWP",
    12: "Xbox One",
    14: "ChromeOS",
    15: "PlayStation 4",
}


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class BotBasicInfo:
    """机器人自身身份信息 (逆向自 NexusE ``BotBasicInfoHolder``)。

    这些字段在设备指纹生命周期内保持稳定, 模拟"同一台设备"的特征。
    """

    #: 构建平台 (BuildPlatform 枚举值, 如 8=Android)
    build_platform: int = 8

    #: 设备唯一标识 (如 "amawufyaaxtu3ufq-d")
    device_id: str = ""

    #: 客户端随机数 (int64, 登录链中使用)
    client_random_id: int = 0

    #: 设备操作系统 (如 "Android", "iOS")
    device_os: str = "Android"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "BuildPlatform": self.build_platform,
            "DeviceID": self.device_id,
            "ClientRandomID": self.client_random_id,
            "DeviceOS": self.device_os,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotBasicInfo":
        """从字典反序列化。"""
        return cls(
            build_platform=data.get("BuildPlatform", 8),
            device_id=data.get("DeviceID", ""),
            client_random_id=data.get("ClientRandomID", 0),
            device_os=data.get("DeviceOS", "Android"),
        )

    @classmethod
    def generate(cls, platform: int = 8) -> "BotBasicInfo":
        """生成新的机器人身份信息。

        Args:
            platform: BuildPlatform 枚举值 (默认 8=Android)。
        """
        return cls(
            build_platform=platform,
            device_id=_generate_device_id(),
            client_random_id=_generate_client_random_id(),
            device_os=_infer_device_os(platform),
        )


@dataclass
class PlayerRecord:
    """玩家记录 (逆向自 NexusE ``PlayerInfo``)。

    存储与玩家身份相关的信息, 登录后由服务器返回填充。
    """

    #: 玩家 UUID (Minecraft UUID)
    uuid: str = ""

    #: Xbox Live 用户 ID (XUID)
    xuid: str = ""

    #: 平台聊天 ID (PlatformChatID)
    platform_chat_id: str = ""

    #: 皮肤 ID / 皮肤数据
    skin: str = ""

    #: 身份名称 (IdentityName, 用于区分不同账号)
    identity_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "UUID": self.uuid,
            "XUID": self.xuid,
            "PlatformChatID": self.platform_chat_id,
            "Skin": self.skin,
            "IdentityName": self.identity_name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlayerRecord":
        """从字典反序列化。"""
        return cls(
            uuid=data.get("UUID", ""),
            xuid=data.get("XUID", ""),
            platform_chat_id=data.get("PlatformChatID", ""),
            skin=data.get("Skin", ""),
            identity_name=data.get("IdentityName", ""),
        )


@dataclass
class DeviceFingerprint:
    """设备指纹完整数据类。

    包含机器人自身信息与玩家记录, 以及元数据 (创建时间、登录次数等)。

    对标 NexusE ``MicroUQHolder`` 的完整状态。
    """

    #: 机器人自身信息
    bot_info: BotBasicInfo = field(default_factory=BotBasicInfo)

    #: 玩家记录
    player: PlayerRecord = field(default_factory=PlayerRecord)

    #: 首次创建时间 (Unix 时间戳)
    created_at: float = 0.0

    #: 上次刷新时间 (Unix 时间戳)
    refreshed_at: float = 0.0

    #: 上次登录时间 (Unix 时间戳)
    last_login_at: float = 0.0

    #: 总登录次数
    login_count: int = 0

    #: 指纹版本号 (用于迁移)
    version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "BotBasicInfo": self.bot_info.to_dict(),
            "PlayerRecord": self.player.to_dict(),
            "CreatedAt": self.created_at,
            "RefreshedAt": self.refreshed_at,
            "LastLoginAt": self.last_login_at,
            "LoginCount": self.login_count,
            "Version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeviceFingerprint":
        """从字典反序列化。"""
        return cls(
            bot_info=BotBasicInfo.from_dict(data.get("BotBasicInfo", {})),
            player=PlayerRecord.from_dict(data.get("PlayerRecord", {})),
            created_at=data.get("CreatedAt", 0.0),
            refreshed_at=data.get("RefreshedAt", 0.0),
            last_login_at=data.get("LastLoginAt", 0.0),
            login_count=data.get("LoginCount", 0),
            version=data.get("Version", 1),
        )

    def is_expired(self, ttl: float = DEFAULT_FINGERPRINT_TTL) -> bool:
        """检查指纹是否过期。

        Args:
            ttl: 有效期 (秒), 默认 7 天。

        Returns:
            True 如果已过期。
        """
        now = time.time()
        return (now - self.refreshed_at) > ttl

    def record_login(self) -> None:
        """记录一次登录事件。"""
        now = time.time()
        self.last_login_at = now
        self.login_count += 1
        if self.created_at == 0.0:
            self.created_at = now
        if self.refreshed_at == 0.0:
            self.refreshed_at = now

    def refresh(self) -> None:
        """刷新指纹中可变的字段 (保留核心 DeviceID)。"""
        self.bot_info.client_random_id = _generate_client_random_id()
        self.refreshed_at = time.time()
        logger.debug(
            f"指纹已刷新: DeviceID={self.bot_info.device_id}, "
            f"新 ClientRandomID={self.bot_info.client_random_id}"
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _generate_device_id() -> str:
    """生成设备唯一标识 (仿 NexusE 格式)。

    NexusE 的 DeviceID 格式为 ``<random_str>-d``, 如 ``amawufyaaxtu3ufq-d``。
    """
    raw = "".join(
        secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789")
        for _ in range(15)
    )
    return f"{raw}-d"


def _generate_client_random_id() -> int:
    """生成客户端随机数 (int64)。

    NexusE 使用 ``secrets.rand.Int63()`` 生成, 范围 [0, 2^63-1]。
    """
    return secrets.randbits(63)


def _infer_device_os(platform: int) -> str:
    """根据 BuildPlatform 推断 DeviceOS 字符串。

    Args:
        platform: BuildPlatform 枚举值。

    Returns:
        DeviceOS 字符串 (如 "Android", "iOS", "Windows_NT" 等)。
    """
    mapping = {
        1: "Windows_NT",
        2: "Darwin",
        7: "Linux",
        8: "Android",
        9: "iOS",
        10: "Horizon",
        11: "Windows_NT",
        12: "Xbox",
        14: "ChromeOS",
        15: "Orbis",
    }
    return mapping.get(platform, "Unknown")


# ---------------------------------------------------------------------------
# JSON 持久化存储
# ---------------------------------------------------------------------------

class FingerprintStore:
    """JSON 持久化存储, 管理所有设备指纹的读写。

    线程安全: 使用 ``threading.Lock`` 保护写操作。
    原子写入: 先写临时文件, 再原子替换 (参考 ``safe_writer``)。
    """

    def __init__(self, store_path: Optional[Path] = None) -> None:
        self._store_path: Path = store_path or DEFAULT_FINGERPRINT_STORE
        self._lock: threading.Lock = threading.Lock()
        self._cache: Dict[str, DeviceFingerprint] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        """确保数据已从磁盘加载。"""
        if not self._loaded:
            self._load()

    def _load(self) -> None:
        """从磁盘加载指纹数据。"""
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._store_path.exists():
            logger.info(f"指纹存储文件不存在, 将创建新文件: {self._store_path}")
            self._loaded = True
            return

        try:
            with open(self._store_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(f"加载指纹存储失败: {exc}, 将使用空存储")
            self._loaded = True
            return

        if not isinstance(raw, dict):
            logger.warning("指纹存储格式异常, 将使用空存储")
            self._loaded = True
            return

        count = 0
        for key, data in raw.items():
            try:
                self._cache[key] = DeviceFingerprint.from_dict(data)
                count += 1
            except Exception as exc:
                logger.warning(f"反序列化指纹 [{key}] 失败: {exc}")

        self._loaded = True
        logger.info(f"已从 {self._store_path} 加载 {count} 条设备指纹")

    def _save(self) -> None:
        """将缓存数据写入磁盘 (原子写入)。"""
        self._store_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._store_path.with_suffix(".tmp")

        data: Dict[str, Any] = {}
        for key, fp in self._cache.items():
            data[key] = fp.to_dict()

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._store_path)
            logger.debug(f"指纹存储已保存: {len(data)} 条记录")
        except OSError as exc:
            logger.error(f"保存指纹存储失败: {exc}")
            # 清理临时文件
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get(self, identity_name: str) -> Optional[DeviceFingerprint]:
        """获取指定账号的设备指纹。

        Args:
            identity_name: 身份名称 (IdentityName)。

        Returns:
            设备指纹, 不存在则返回 None。
        """
        with self._lock:
            self._ensure_loaded()
            return self._cache.get(identity_name)

    def put(self, identity_name: str, fingerprint: DeviceFingerprint) -> None:
        """存储或更新设备指纹。

        Args:
            identity_name: 身份名称。
            fingerprint: 设备指纹数据。
        """
        with self._lock:
            self._ensure_loaded()
            self._cache[identity_name] = fingerprint
            self._save()

    def delete(self, identity_name: str) -> bool:
        """删除指定账号的设备指纹。

        Args:
            identity_name: 身份名称。

        Returns:
            True 如果成功删除, False 如果不存在。
        """
        with self._lock:
            self._ensure_loaded()
            if identity_name in self._cache:
                del self._cache[identity_name]
                self._save()
                return True
            return False

    def list_all(self) -> List[str]:
        """列出所有已存储的身份名称。

        Returns:
            身份名称列表。
        """
        with self._lock:
            self._ensure_loaded()
            return list(self._cache.keys())

    def count(self) -> int:
        """返回已存储的指纹数量。"""
        with self._lock:
            self._ensure_loaded()
            return len(self._cache)

    def reload(self) -> None:
        """强制从磁盘重新加载 (丢弃缓存)。"""
        with self._lock:
            self._cache.clear()
            self._loaded = False
            self._load()

    def flush(self) -> None:
        """强制将缓存写入磁盘。"""
        with self._lock:
            self._save()


# ---------------------------------------------------------------------------
# UQHolder - 统一身份管理器
# ---------------------------------------------------------------------------

class UQHolder:
    """统一身份管理器 (MicroUQHolder)。

    对标 NexusE 的 ``MicroUQHolder``, 提供设备指纹的持久化、加载、刷新
    和登录统计功能。

    核心职责:

    1. **持久化**: 指纹写入 JSON 文件, 下次启动自动加载
    2. **复用**: 每次登录时通过 ``get_or_create`` 复用相同指纹
    3. **刷新**: 指纹过期后自动刷新部分字段
    4. **统计**: 记录登录次数和上次登录时间

    使用示例::

        holder = UQHolder()
        # 获取或创建指纹
        fp = await holder.get_or_create("player_001", platform=8)
        # 记录登录
        holder.record_login("player_001")
        # 获取机器人信息
        bot_info = holder.get_bot_info("player_001")
    """

    def __init__(
        self,
        store_path: Optional[Path] = None,
        ttl: float = DEFAULT_FINGERPRINT_TTL,
    ) -> None:
        self._store = FingerprintStore(store_path)
        self._ttl: float = ttl
        self._async_lock: asyncio.Lock = asyncio.Lock()
        logger.info(f"UQHolder 初始化完成: store={self._store._store_path}, ttl={ttl}s")

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    async def get_or_create(
        self,
        identity_name: str,
        platform: int = 8,
        force_refresh: bool = False,
    ) -> DeviceFingerprint:
        """获取或创建设备指纹。

        如果指纹已存在且未过期, 直接返回缓存副本; 如果过期或不存在,
        则创建新指纹或刷新现有指纹。

        Args:
            identity_name: 身份名称 (用于区分不同账号)。
            platform: BuildPlatform 枚举值 (默认 8=Android)。
            force_refresh: 是否强制刷新。

        Returns:
            设备指纹对象。
        """
        async with self._async_lock:
            existing = self._store.get(identity_name)

            if existing is None:
                # 第一次使用: 创建全新指纹
                fp = DeviceFingerprint(
                    bot_info=BotBasicInfo.generate(platform=platform),
                    player=PlayerRecord(identity_name=identity_name),
                    created_at=time.time(),
                    refreshed_at=time.time(),
                )
                self._store.put(identity_name, fp)
                logger.info(
                    f"已创建新设备指纹: IdentityName={identity_name}, "
                    f"DeviceID={fp.bot_info.device_id}, "
                    f"Platform={BUILD_PLATFORM_NAMES.get(platform, 'Unknown')}"
                )
                return fp

            if force_refresh or existing.is_expired(self._ttl):
                # 过期或强制刷新: 保留核心标识, 刷新可变字段
                existing.refresh()
                self._store.put(identity_name, existing)
                logger.info(
                    f"已刷新设备指纹: IdentityName={identity_name}, "
                    f"DeviceID={existing.bot_info.device_id}"
                )
                return existing

            # 复用现有指纹
            logger.debug(f"复用现有设备指纹: IdentityName={identity_name}")
            return existing

    def get_bot_info(self, identity_name: str) -> Optional[BotBasicInfo]:
        """获取指定账号的机器人身份信息。

        Args:
            identity_name: 身份名称。

        Returns:
            机器人身份信息, 不存在则返回 None。
        """
        fp = self._store.get(identity_name)
        return fp.bot_info if fp else None

    def get_player(self, identity_name: str) -> Optional[PlayerRecord]:
        """获取指定账号的玩家记录。

        Args:
            identity_name: 身份名称。

        Returns:
            玩家记录, 不存在则返回 None。
        """
        fp = self._store.get(identity_name)
        return fp.player if fp else None

    def record_login(self, identity_name: str) -> bool:
        """记录一次登录事件。

        更新 ``last_login_at`` 和 ``login_count``。

        Args:
            identity_name: 身份名称。

        Returns:
            True 如果成功记录, False 如果指纹不存在。
        """
        fp = self._store.get(identity_name)
        if fp is None:
            logger.warning(f"记录登录失败: IdentityName={identity_name} 不存在")
            return False

        fp.record_login()
        self._store.put(identity_name, fp)
        logger.info(
            f"登录记录: IdentityName={identity_name}, "
            f"LoginCount={fp.login_count}, "
            f"LastLoginAt={fp.last_login_at}"
        )
        return True

    def update_player(self, identity_name: str, player: PlayerRecord) -> bool:
        """更新玩家记录 (登录成功后由服务器返回的数据填充)。

        Args:
            identity_name: 身份名称。
            player: 新的玩家记录。

        Returns:
            True 如果成功更新, False 如果指纹不存在。
        """
        fp = self._store.get(identity_name)
        if fp is None:
            logger.warning(f"更新玩家记录失败: IdentityName={identity_name} 不存在")
            return False

        fp.player = player
        self._store.put(identity_name, fp)
        logger.info(
            f"已更新玩家记录: IdentityName={identity_name}, "
            f"UUID={player.uuid}, XUID={player.xuid}"
        )
        return True

    def delete(self, identity_name: str) -> bool:
        """删除指定账号的设备指纹。

        Args:
            identity_name: 身份名称。

        Returns:
            True 如果成功删除。
        """
        result = self._store.delete(identity_name)
        if result:
            logger.info(f"已删除设备指纹: IdentityName={identity_name}")
        return result

    def list_identities(self) -> List[str]:
        """列出所有已存储的身份名称。"""
        return self._store.list_all()

    def stats(self) -> Dict[str, Any]:
        """返回 UQHolder 状态统计。

        Returns:
            包含存储路径、指纹数量、各账号信息的字典。
        """
        identities = self._store.list_all()
        details = []
        for name in identities:
            fp = self._store.get(name)
            if fp:
                details.append({
                    "IdentityName": name,
                    "DeviceID": fp.bot_info.device_id,
                    "BuildPlatform": fp.bot_info.build_platform,
                    "LoginCount": fp.login_count,
                    "LastLoginAt": fp.last_login_at,
                    "CreatedAt": fp.created_at,
                    "IsExpired": fp.is_expired(self._ttl),
                })
        return {
            "store_path": str(self._store._store_path),
            "ttl_seconds": self._ttl,
            "total_count": len(identities),
            "identities": details,
        }

    def reload(self) -> None:
        """强制从磁盘重新加载所有指纹。"""
        self._store.reload()
        logger.info("UQHolder 已重新加载所有指纹")

    def flush(self) -> None:
        """强制将所有缓存的指纹写入磁盘。"""
        self._store.flush()
        logger.info("UQHolder 已刷新所有缓存到磁盘")


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_uqholder_instance: Optional[UQHolder] = None
_uqholder_lock: threading.Lock = threading.Lock()


def get_uqholder(
    store_path: Optional[Path] = None,
    ttl: float = DEFAULT_FINGERPRINT_TTL,
) -> UQHolder:
    """返回共享的 :class:`UQHolder` 单例。

    Args:
        store_path: 存储路径, 仅首次调用时生效。
        ttl: 指纹有效期, 仅首次调用时生效。

    Returns:
        UQHolder 单例。
    """
    global _uqholder_instance
    if _uqholder_instance is None:
        with _uqholder_lock:
            if _uqholder_instance is None:
                _uqholder_instance = UQHolder(store_path=store_path, ttl=ttl)
    return _uqholder_instance


def reset_uqholder() -> None:
    """重置 UQHolder 单例 (用于测试/重启)。"""
    global _uqholder_instance
    with _uqholder_lock:
        if _uqholder_instance is not None:
            _uqholder_instance.flush()
        _uqholder_instance = None
        logger.info("UQHolder 单例已重置")


__all__ = [
    # 数据类
    "DeviceFingerprint",
    "BotBasicInfo",
    "PlayerRecord",
    # 管理器
    "UQHolder",
    "FingerprintStore",
    # 单例
    "get_uqholder",
    "reset_uqholder",
    # 常量
    "DEFAULT_FINGERPRINT_STORE",
    "DEFAULT_FINGERPRINT_TTL",
    "BUILD_PLATFORM_NAMES",
    # 工具
    "_generate_device_id",
    "_generate_client_random_id",
    "_infer_device_os",
]