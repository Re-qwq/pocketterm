"""Cookie 账号池管理模块.

提供网易 Minecraft 中国版的 Cookie 账号池管理功能, 包括:

- 添加/移除 Cookie
- 异步验证 Cookie 有效性 (调用 :class:`NemcClient.login`)
- 轮询获取可用 Cookie
- 批量验证所有 Cookie
- JSON 持久化

持久化路径: ``/workspace/PocketTerm/backend/data/cookie_pool.json``
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .nemc_client import LoginResult, NemcClient

__all__ = [
    "CookieStatus",
    "CookieEntry",
    "PoolStatus",
    "CookiePool",
    "DEFAULT_POOL_FILE",
]

logger = logging.getLogger("pocketterm.auth.nemc.cookie_pool")

#: 默认持久化文件路径.
DEFAULT_POOL_FILE: str = "/workspace/PocketTerm/backend/data/cookie_pool.json"

#: 批量验证时的最大并发数.
MAX_CONCURRENT_VALIDATIONS: int = 5


class CookieStatus(str, Enum):
    """Cookie 状态枚举."""

    UNKNOWN = "unknown"
    """未验证."""
    VALID = "valid"
    """有效."""
    INVALID = "invalid"
    """无效 (已过期或被封)."""
    IN_USE = "in_use"
    """使用中 (仍然有效)."""


@dataclass
class CookieEntry:
    """Cookie 账号条目."""

    cookie_id: str = ""
    """唯一标识符 (Cookie 的 SHA-256 前 16 位)."""
    cookie: str = ""
    """Cookie 字符串."""
    status: CookieStatus = CookieStatus.UNKNOWN
    """当前状态."""
    uid: str = ""
    """用户 UID (验证成功后填充)."""
    pe_uid: str = ""
    """PE 平台 UID (验证成功后填充)."""
    login_src_token: str = ""
    """登录令牌 (验证成功后填充)."""
    login_d_token: str = ""
    """加密登录令牌 (验证成功后填充)."""
    last_validated: str = ""
    """最后验证时间 (ISO 格式 UTC)."""
    last_error: str = ""
    """最后一次验证错误信息."""
    in_use: bool = False
    """是否正在使用中."""

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典 (用于 JSON 序列化)."""
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "CookieEntry":
        """从字典创建 (用于 JSON 反序列化)."""
        status_str = d.get("status", "unknown")
        try:
            status = CookieStatus(status_str)
        except ValueError:
            status = CookieStatus.UNKNOWN
        return CookieEntry(
            cookie_id=d.get("cookie_id", ""),
            cookie=d.get("cookie", ""),
            status=status,
            uid=d.get("uid", ""),
            pe_uid=d.get("pe_uid", ""),
            login_src_token=d.get("login_src_token", ""),
            login_d_token=d.get("login_d_token", ""),
            last_validated=d.get("last_validated", ""),
            last_error=d.get("last_error", ""),
            in_use=d.get("in_use", False),
        )


@dataclass
class PoolStatus:
    """账号池状态."""

    total: int = 0
    """总数."""
    valid: int = 0
    """有效数."""
    invalid: int = 0
    """失效数."""
    unknown: int = 0
    """未验证数."""
    in_use: int = 0
    """使用中数."""
    available: int = 0
    """可用数 (有效且未使用)."""


class CookiePool:
    """Cookie 账号池管理器.

    提供线程安全 (异步锁) 的 Cookie 账号池管理.

    Parameters
    ----------
    pool_file
        JSON 持久化文件路径. 默认 ``DEFAULT_POOL_FILE``.
    """

    def __init__(self, pool_file: str = DEFAULT_POOL_FILE) -> None:
        self._pool_file: str = pool_file
        self._cookies: List[CookieEntry] = []
        self._last_used_index: int = 0
        self._lock: asyncio.Lock = asyncio.Lock()

        # 如果文件存在, 自动加载.
        if os.path.exists(self._pool_file):
            try:
                self.load_from_file(self._pool_file)
                logger.info("从 %s 加载了 %d 个 Cookie", self._pool_file, len(self._cookies))
            except Exception as e:
                logger.warning("加载 Cookie 池失败: %s", e)

    # --- 添加/移除 ----------------------------------------------------------

    async def add_cookie(self, cookie: str) -> CookieEntry:
        """添加 Cookie 到池.

        如果 Cookie 已存在 (相同内容), 返回已有条目.

        Parameters
        ----------
        cookie
            Cookie 字符串.

        Returns
        -------
        CookieEntry
            创建或已有的 Cookie 条目.
        """
        async with self._lock:
            # 检查是否已存在.
            cookie_id = self._compute_cookie_id(cookie)
            for entry in self._cookies:
                if entry.cookie_id == cookie_id:
                    logger.debug("Cookie 已存在: %s", cookie_id)
                    return entry

            entry = CookieEntry(
                cookie_id=cookie_id,
                cookie=cookie,
                status=CookieStatus.UNKNOWN,
            )
            self._cookies.append(entry)
            logger.info("添加 Cookie: %s", cookie_id)
            return entry

    async def remove_cookie(self, cookie_id: str) -> bool:
        """移除 Cookie.

        Parameters
        ----------
        cookie_id
            Cookie 条目 ID.

        Returns
        -------
        bool
            是否成功移除.
        """
        async with self._lock:
            for i, entry in enumerate(self._cookies):
                if entry.cookie_id == cookie_id:
                    self._cookies.pop(i)
                    logger.info("移除 Cookie: %s", cookie_id)
                    return True
            return False

    # --- 验证 ----------------------------------------------------------------

    async def validate_cookie(self, cookie: str) -> CookieEntry:
        """验证 Cookie 有效性.

        调用 :meth:`NemcClient.login` 进行登录验证.

        Parameters
        ----------
        cookie
            Cookie 字符串.

        Returns
        -------
        CookieEntry
            验证后的 Cookie 条目 (状态已更新).
        """
        entry = await self.add_cookie(cookie)

        async with self._lock:
            entry.status = CookieStatus.UNKNOWN
            entry.last_error = ""

        client = NemcClient()
        try:
            result: LoginResult = await client.login(cookie)
            if result.uid:
                async with self._lock:
                    entry.status = CookieStatus.VALID
                    entry.uid = result.uid
                    entry.pe_uid = result.pe_uid
                    entry.login_src_token = result.login_src_token
                    entry.login_d_token = result.login_d_token
                    entry.last_validated = datetime.now(timezone.utc).isoformat()
                    entry.last_error = ""
                logger.info("Cookie 验证成功: uid=%s", result.uid)
            else:
                async with self._lock:
                    entry.status = CookieStatus.INVALID
                    entry.last_validated = datetime.now(timezone.utc).isoformat()
                    entry.last_error = "登录失败: 未获取到 UID"
                logger.warning("Cookie 验证失败: 未获取到 UID")
        except Exception as e:
            async with self._lock:
                entry.status = CookieStatus.INVALID
                entry.last_validated = datetime.now(timezone.utc).isoformat()
                entry.last_error = str(e)
            logger.error("Cookie 验证异常: %s", e)
        finally:
            await client.close()

        return entry

    async def validate_all(self) -> Dict[str, int]:
        """批量验证所有 Cookie.

        使用信号量限制并发数, 不阻塞主线程.

        Returns
        -------
        dict
            验证结果统计: ``{"valid": N, "invalid": N, "total": N}``.
        """
        async with self._lock:
            cookies_to_validate = [
                (entry.cookie_id, entry.cookie) for entry in self._cookies
            ]

        if not cookies_to_validate:
            return {"valid": 0, "invalid": 0, "total": 0}

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_VALIDATIONS)

        async def _validate_one(cookie_id: str, cookie: str) -> bool:
            async with semaphore:
                try:
                    entry = await self.validate_cookie(cookie)
                    return entry.status == CookieStatus.VALID
                except Exception as e:
                    logger.error("验证 Cookie %s 异常: %s", cookie_id, e)
                    return False

        tasks = [
            _validate_one(cid, ck) for cid, ck in cookies_to_validate
        ]
        results = await asyncio.gather(*tasks)

        valid_count = sum(1 for r in results if r)
        invalid_count = len(results) - valid_count

        logger.info(
            "批量验证完成: 有效=%d, 失效=%d, 总计=%d",
            valid_count, invalid_count, len(results),
        )
        return {
            "valid": valid_count,
            "invalid": invalid_count,
            "total": len(results),
        }

    # --- 获取 ----------------------------------------------------------------

    async def get_available(self) -> Optional[CookieEntry]:
        """获取一个可用的 Cookie (轮询).

        返回状态为 VALID 且不在使用中的 Cookie. 使用轮询 (round-robin)
        策略, 确保均匀分配.

        Returns
        -------
        CookieEntry or None
            可用的 Cookie 条目, 如果没有可用则返回 None.
        """
        async with self._lock:
            valid_entries = [
                (i, entry) for i, entry in enumerate(self._cookies)
                if entry.status == CookieStatus.VALID and not entry.in_use
            ]

            if not valid_entries:
                return None

            # 轮询: 从上次使用的索引开始.
            n = len(valid_entries)
            start = self._last_used_index % n
            idx, entry = valid_entries[start]
            self._last_used_index = (start + 1) % n

            entry.in_use = True
            entry.status = CookieStatus.IN_USE
            logger.debug("分配 Cookie: %s (index=%d)", entry.cookie_id, idx)
            return entry

    async def release_cookie(self, cookie_id: str) -> bool:
        """释放 Cookie (标记为可用).

        Parameters
        ----------
        cookie_id
            Cookie 条目 ID.

        Returns
        -------
        bool
            是否成功释放.
        """
        async with self._lock:
            for entry in self._cookies:
                if entry.cookie_id == cookie_id:
                    entry.in_use = False
                    if entry.status == CookieStatus.IN_USE:
                        entry.status = CookieStatus.VALID
                    logger.debug("释放 Cookie: %s", cookie_id)
                    return True
            return False

    async def mark_invalid(self, cookie_id: str, reason: str = "") -> bool:
        """标记 Cookie 为失效.

        Parameters
        ----------
        cookie_id
            Cookie 条目 ID.
        reason
            失效原因.

        Returns
        -------
        bool
            是否成功标记.
        """
        async with self._lock:
            for entry in self._cookies:
                if entry.cookie_id == cookie_id:
                    entry.status = CookieStatus.INVALID
                    entry.in_use = False
                    entry.last_error = reason
                    entry.last_validated = datetime.now(timezone.utc).isoformat()
                    logger.info("标记 Cookie 失效: %s (%s)", cookie_id, reason)
                    return True
            return False

    # --- 状态 ----------------------------------------------------------------

    async def get_status(self) -> PoolStatus:
        """获取账号池状态.

        Returns
        -------
        PoolStatus
            池状态统计.
        """
        async with self._lock:
            status = PoolStatus(total=len(self._cookies))
            for entry in self._cookies:
                if entry.status == CookieStatus.VALID:
                    status.valid += 1
                elif entry.status == CookieStatus.INVALID:
                    status.invalid += 1
                elif entry.status == CookieStatus.IN_USE:
                    status.in_use += 1
                    status.valid += 1  # IN_USE 也是有效的
                else:
                    status.unknown += 1

                if entry.status == CookieStatus.VALID and not entry.in_use:
                    status.available += 1

            return status

    async def get_all_cookies(self) -> List[CookieEntry]:
        """获取所有 Cookie 条目 (副本)."""
        async with self._lock:
            return list(self._cookies)

    async def get_cookie(self, cookie_id: str) -> Optional[CookieEntry]:
        """按 ID 获取 Cookie 条目.

        Parameters
        ----------
        cookie_id
            Cookie 条目 ID.

        Returns
        -------
        CookieEntry or None
            Cookie 条目, 如果不存在则返回 None.
        """
        async with self._lock:
            for entry in self._cookies:
                if entry.cookie_id == cookie_id:
                    return entry
            return None

    # --- 持久化 ---------------------------------------------------------------

    def load_from_file(self, path: str = "") -> None:
        """从文件加载 Cookie 池.

        Parameters
        ----------
        path
            JSON 文件路径. 默认使用 ``self._pool_file``.
        """
        filepath = path or self._pool_file
        if not os.path.exists(filepath):
            return

        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        self._cookies = [
            CookieEntry.from_dict(d) for d in data.get("cookies", [])
        ]
        self._last_used_index = data.get("last_used_index", 0)
        logger.info("从 %s 加载了 %d 个 Cookie", filepath, len(self._cookies))

    def save_to_file(self, path: str = "") -> None:
        """保存 Cookie 池到文件.

        Parameters
        ----------
        path
            JSON 文件路径. 默认使用 ``self._pool_file``.
        """
        filepath = path or self._pool_file

        # 确保目录存在.
        dirpath = os.path.dirname(filepath)
        if dirpath and not os.path.exists(dirpath):
            os.makedirs(dirpath, exist_ok=True)

        data = {
            "cookies": [entry.to_dict() for entry in self._cookies],
            "last_used_index": self._last_used_index,
        }

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info("保存 %d 个 Cookie 到 %s", len(self._cookies), filepath)

    async def save(self) -> None:
        """异步保存 Cookie 池到文件."""
        async with self._lock:
            self.save_to_file()

    # --- 辅助 ----------------------------------------------------------------

    @staticmethod
    def _compute_cookie_id(cookie: str) -> str:
        """计算 Cookie 的唯一 ID.

        使用 SHA-256 的前 16 位十六进制字符.

        Parameters
        ----------
        cookie
            Cookie 字符串.

        Returns
        -------
        str
            16 字符的十六进制 ID.
        """
        return hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:16]

    async def __aenter__(self) -> "CookiePool":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.save()
