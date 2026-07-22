"""nv1.nethard.pro SAuth Key 管理器。

功能:
    - 管理 nv1 SAuth Key 的存储和获取
    - 支持自动刷新 (到期前 24 小时)
    - 支持手动刷新
    - 模拟模式: 无需真实 Key 即可运行
    - 真实模式: 通过 nv1 API 刷新 Key

使用方式::

    from app.auth.nv1_manager import nv1_manager

    # 设置 Key
    await nv1_manager.set_key("your_sauth_key", expires_at=timestamp)

    # 获取 Key
    key = nv1_manager.get_key()

    # 手动刷新
    result = await nv1_manager.refresh_key()
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("pocketterm.nv1")

#: nv1 API 地址
NV1_API_BASE = "https://nv1.nethard.pro"

#: Key 有效期 (7 天)
KEY_VALIDITY_SECONDS = 7 * 24 * 3600

#: 刷新阈值 (到期前 24 小时刷新)
REFRESH_THRESHOLD = 24 * 3600


class NV1Manager:
    """nv1 SAuth Key 管理器。"""

    def __init__(self):
        self._key: str = ""
        self._key_expires_at: float = 0.0
        self._api_token: str = ""  # nv1 API 令牌 (用于真实刷新)
        self._mock_mode: bool = True  # 默认模拟模式
        self._lock = asyncio.Lock()

    def is_configured(self) -> bool:
        """是否已配置 Key。"""
        return bool(self._key)

    def is_mock_mode(self) -> bool:
        """是否为模拟模式。"""
        return self._mock_mode

    def get_key(self) -> str:
        """获取当前 SAuth Key。"""
        return self._key

    def get_expires_at(self) -> float:
        """获取 Key 过期时间戳。"""
        return self._key_expires_at

    def get_remaining_seconds(self) -> Optional[float]:
        """获取剩余有效时间 (秒), None 表示永久。"""
        if not self._key:
            return None
        if self._key_expires_at == 0:
            return None
        return max(0, self._key_expires_at - time.time())

    def is_valid(self) -> bool:
        """Key 是否有效。"""
        if not self._key:
            return False
        if self._key_expires_at == 0:
            return True  # 永久
        return self._key_expires_at > time.time()

    async def set_key(self, key: str, expires_at: float = 0, api_token: str = "") -> None:
        """设置 SAuth Key。

        Args:
            key: SAuth Key 字符串
            expires_at: 过期时间戳, 0 表示永久
            api_token: nv1 API 令牌 (用于真实刷新, 留空则使用模拟模式)
        """
        async with self._lock:
            self._key = key
            self._key_expires_at = expires_at if expires_at > 0 else 0
            self._api_token = api_token
            self._mock_mode = not api_token
            logger.info(
                f"nv1 Key 已设置 (模式: {'模拟' if self._mock_mode else '真实'}, "
                f"过期: {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at)) if expires_at else '永久'})"
            )

    async def refresh_key(self) -> dict:
        """刷新 SAuth Key。

        模拟模式: 生成新的模拟 Key, 延长 7 天有效期。
        真实模式: 调用 nv1 API 刷新 Key。

        Returns:
            {"success": bool, "key": str, "expires_at": float, "error": str}
        """
        async with self._lock:
            if self._mock_mode:
                # 模拟模式: 生成新 Key
                self._key = f"mock_sauth_{secrets.token_hex(16)}"
                self._key_expires_at = time.time() + KEY_VALIDITY_SECONDS
                logger.info("nv1 Key 已刷新 (模拟模式)")
                return {
                    "success": True,
                    "key": self._key,
                    "expires_at": self._key_expires_at,
                    "mode": "mock",
                }

            # 真实模式: 调用 nv1 API
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{NV1_API_BASE}/api/sauth/refresh",
                        json={"token": self._api_token},
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            self._key = data.get("sauth_key", "")
                            self._key_expires_at = time.time() + KEY_VALIDITY_SECONDS
                            logger.info("nv1 Key 已刷新 (真实模式)")
                            return {
                                "success": True,
                                "key": self._key,
                                "expires_at": self._key_expires_at,
                                "mode": "real",
                            }
                        else:
                            error_text = await resp.text()
                            logger.error(f"nv1 Key 刷新失败: HTTP {resp.status} - {error_text}")
                            return {
                                "success": False,
                                "error": f"HTTP {resp.status}: {error_text}",
                            }
            except aiohttp.ClientError as e:
                logger.error(f"nv1 Key 刷新失败 (网络错误): {e}")
                return {"success": False, "error": f"网络错误: {e}"}
            except Exception as e:
                logger.error(f"nv1 Key 刷新失败 (未知错误): {e}")
                return {"success": False, "error": str(e)}

    def get_status(self) -> dict:
        """获取当前状态。"""
        remaining = self.get_remaining_seconds()
        return {
            "configured": self.is_configured(),
            "valid": self.is_valid(),
            "mode": "mock" if self._mock_mode else "real",
            "key_preview": self._key[:16] + "..." if len(self._key) > 16 else self._key,
            "expires_at": self._key_expires_at if self._key_expires_at > 0 else None,
            "remaining_seconds": remaining,
            "remaining_days": round(remaining / 86400, 1) if remaining else None,
            "needs_refresh": remaining is not None and remaining < REFRESH_THRESHOLD,
        }

    async def init_mock_if_needed(self) -> None:
        """如果没有配置 Key, 初始化一个模拟 Key。"""
        if not self._key:
            await self.refresh_key()


# 全局单例
nv1_manager = NV1Manager()
