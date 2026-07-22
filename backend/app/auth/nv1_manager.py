"""nv1.nethard.pro SAuth Key 管理器 (含 NovaBuilder API 集成)。

功能:
    - 管理 nv1 SAuth Key 的存储和获取
    - 支持自动刷新 (到期前 24 小时)
    - 支持手动刷新
    - 模拟模式: 无需真实 Key 即可运行
    - 真实模式: 通过 nv1 API 刷新 Key
    - NovaBuilder 模式: 自动登录 novabuilder.pro 获取/刷新 API Key

NovaBuilder API 流程:
    1. 用户在 novabuilder.pro 注册并获取 API Key (7天有效期)
    2. POST /api/auth/apikey_login  验证 API Key
    3. POST /api/auth/builder_apikey?token=...  重新生成 API Key
    4. 使用 API Key 连接 wss://nv1.nethard.pro 获取 SAuth Key

使用方式::

    from app.auth.nv1_manager import nv1_manager

    # 设置 Key
    await nv1_manager.set_key("your_sauth_key", expires_at=timestamp)

    # 设置 NovaBuilder 凭据 (自动刷新模式)
    await nv1_manager.set_novabuilder_credentials("username", "password")

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

#: nv1 WebSocket 地址
NV1_WSS_URL = "wss://nv1.nethard.pro"

#: NovaBuilder 用户中心 API 地址
NOVABUILDER_API_BASE = "https://user.novabuilder.pro/api"

#: Key 有效期 (7 天)
KEY_VALIDITY_SECONDS = 7 * 24 * 3600

#: 刷新阈值 (到期前 24 小时刷新)
REFRESH_THRESHOLD = 24 * 3600

#: NovaBuilder API 请求头
NOVABUILDER_HEADERS = {
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
}


class NV1Manager:
    """nv1 SAuth Key 管理器 (含 NovaBuilder API 集成)。

    支持三种模式:
        - mock: 模拟模式, 生成假 Key, 无需外部服务
        - real: 真实模式, 使用管理员手动设置的 API Key
        - novabuilder: NovaBuilder 模式, 自动登录 novabuilder.pro 获取/刷新 Key
    """

    def __init__(self):
        self._key: str = ""
        self._key_expires_at: float = 0.0
        self._api_token: str = ""  # nv1 API 令牌 (用于真实刷新)
        self._mock_mode: bool = True  # 默认模拟模式
        self._lock = asyncio.Lock()
        # NovaBuilder 凭据
        self._novabuilder_username: str = ""
        self._novabuilder_password: str = ""
        self._novabuilder_token: str = ""  # NovaBuilder 会话令牌
        self._novabuilder_apikey: str = ""  # NovaBuilder API Key

    def is_configured(self) -> bool:
        """是否已配置 Key。"""
        return bool(self._key)

    def is_mock_mode(self) -> bool:
        """是否为模拟模式。"""
        return self._mock_mode

    def get_key(self) -> str:
        """获取当前 SAuth Key。"""
        return self._key

    def get_auth_server(self) -> str:
        """获取认证服务器 URL。

        NovaBuilder 模式使用 NovaBuilder 用户中心,
        其他模式使用 NV1 服务器。
        """
        if self._novabuilder_username and self._novabuilder_password:
            return "https://user.novabuilder.pro"
        return "https://nv1.nethard.pro"

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
            # 如果有 NovaBuilder 凭据, 使用 novabuilder 模式
            if self._novabuilder_username and self._novabuilder_password:
                self._mock_mode = False
                self._novabuilder_apikey = key
            logger.info(
                f"nv1 Key 已设置 (模式: {'模拟' if self._mock_mode else '真实'}, "
                f"过期: {time.strftime('%Y-%m-%d %H:%M', time.localtime(expires_at)) if expires_at else '永久'})"
            )

        # 如果有 NovaBuilder 凭据, 持久化 API Key
        if self._novabuilder_username and self._novabuilder_password:
            await self._save_apikey_to_db(key, expires_at)

    async def set_novabuilder_credentials(
        self, username: str, password: str
    ) -> None:
        """设置 NovaBuilder 用户中心凭据, 启用自动刷新模式。

        设置后, refresh_key() 将自动登录 novabuilder.pro 获取/刷新 API Key。
        凭据会持久化到数据库, 后端重启后自动加载。

        Args:
            username: NovaBuilder 用户名
            password: NovaBuilder 密码
        """
        async with self._lock:
            self._novabuilder_username = username
            self._novabuilder_password = password
            self._mock_mode = False
            logger.info(f"已设置 NovaBuilder 凭据 (用户: {username}), 启用自动刷新模式")

        # 持久化到数据库
        try:
            from app.database import get_db
            db = await get_db()
            await db.set_setting("novabuilder_credentials", json.dumps({
                "username": username,
                "password": password,
            }))
            logger.info("NovaBuilder 凭据已持久化到数据库")
        except Exception as e:
            logger.warning(f"NovaBuilder 凭据持久化失败: {e}")

    async def _save_apikey_to_db(self, api_key: str, expires_at: float) -> None:
        """将当前 API Key 持久化到数据库。"""
        try:
            from app.database import get_db
            db = await get_db()
            await db.set_setting("novabuilder_apikey", json.dumps({
                "api_key": api_key,
                "expires_at": expires_at,
                "saved_at": time.time(),
            }))
        except Exception as e:
            logger.warning(f"API Key 持久化失败: {e}")

    async def load_from_db(self) -> bool:
        """从数据库加载 NovaBuilder 凭据和 API Key。

        在后端启动时调用, 确保重启后凭据不丢失。

        Returns:
            True 如果成功加载了凭据
        """
        try:
            from app.database import get_db
            db = await get_db()

            # 1. 加载凭据
            creds_json = await db.get_setting("novabuilder_credentials")
            if creds_json:
                creds = json.loads(creds_json)
                self._novabuilder_username = creds.get("username", "")
                self._novabuilder_password = creds.get("password", "")
                if self._novabuilder_username and self._novabuilder_password:
                    self._mock_mode = False
                    logger.info(f"已从数据库加载 NovaBuilder 凭据 (用户: {self._novabuilder_username})")

                    # 2. 加载 API Key
                    key_json = await db.get_setting("novabuilder_apikey")
                    if key_json:
                        key_data = json.loads(key_json)
                        api_key = key_data.get("api_key", "")
                        expires_at = key_data.get("expires_at", 0)
                        if api_key and (expires_at == 0 or expires_at > time.time()):
                            self._key = api_key
                            self._key_expires_at = expires_at
                            self._novabuilder_apikey = api_key
                            self._api_token = api_key
                            logger.info(f"已从数据库加载 API Key (前8位: {api_key[:8]}...)")
                        elif api_key:
                            # Key 已过期, 触发刷新
                            logger.info("数据库中的 API Key 已过期, 将触发刷新")
                            self._novabuilder_apikey = api_key
                    return True
        except Exception as e:
            logger.warning(f"从数据库加载 NovaBuilder 凭据失败: {e}")
        return False

    async def _login_to_novabuilder(self) -> Optional[str]:
        """登录 novabuilder.pro 用户中心, 获取会话令牌。

        Returns:
            会话令牌字符串, 失败返回 None
        """
        if not self._novabuilder_username or not self._novabuilder_password:
            return None

        try:
            async with aiohttp.ClientSession() as session:
                # 1. 调用登录接口
                async with session.post(
                    f"{NOVABUILDER_API_BASE}/auth/login",
                    json={
                        "username": self._novabuilder_username,
                        "password": self._novabuilder_password,
                    },
                    headers=NOVABUILDER_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        logger.error(f"NovaBuilder 登录失败: HTTP {resp.status} - {error_text}")
                        return None

                    data = await resp.json()
                    if data.get("status") != 200:
                        logger.error(f"NovaBuilder 登录失败: {data}")
                        return None

                    # 从响应中提取令牌
                    token = data.get("data", {}).get("token", "")
                    if not token:
                        # 尝试从 cookie 中获取
                        cookies = resp.cookies
                        token = cookies.get("token", "").value if "token" in cookies else ""

                    if not token:
                        # 尝试从 login_info 获取
                        async with session.get(
                            f"{NOVABUILDER_API_BASE}/auth/login_info",
                            headers=NOVABUILDER_HEADERS,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as info_resp:
                            if info_resp.status == 200:
                                info_data = await info_resp.json()
                                token = info_data.get("data", {}).get("token", "")

                    if token:
                        self._novabuilder_token = token
                        logger.info("NovaBuilder 登录成功, 已获取会话令牌")
                        return token
                    else:
                        logger.warning("NovaBuilder 登录成功但未获取到令牌, 尝试使用 cookie 模式")
                        # 即使没有显式 token, session cookie 也能用于后续请求
                        return "session_cookie"

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            logger.error(f"NovaBuilder 登录网络错误: {e}")
            return None
        except Exception as e:
            logger.error(f"NovaBuilder 登录未知错误: {e}")
            return None

    async def _validate_apikey(self, api_key: str) -> bool:
        """验证 NovaBuilder API Key 是否有效。

        通过 POST /api/auth/apikey_login 检查 Key 有效性。
        - 有活跃会话时返回 {"status": 200, "data": "Logined"}
        - 无会话时返回 {"status": 200, "data": "<token_string>"}

        Args:
            api_key: 要验证的 API Key

        Returns:
            True 如果 Key 有效, False 如果无效或验证失败
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{NOVABUILDER_API_BASE}/auth/apikey_login",
                    json={"apiKey": api_key},
                    headers=NOVABUILDER_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == 200:
                            # "Logined" 表示有活跃会话, token 字符串表示无会话但 key 有效
                            return bool(data.get("data"))
                    return False
        except Exception as e:
            logger.warning(f"API Key 验证失败: {e}")
            return False

    async def _regenerate_apikey(self) -> Optional[str]:
        """重新生成 NovaBuilder API Key。

        通过 POST /api/auth/builder_apikey 重新生成 Key。
        需要先登录获取会话令牌。

        Returns:
            新的 API Key 字符串, 失败返回 None
        """
        if not self._novabuilder_token:
            token = await self._login_to_novabuilder()
            if not token:
                return None
        else:
            token = self._novabuilder_token

        try:
            async with aiohttp.ClientSession() as session:
                # 使用 token 进行请求
                url = f"{NOVABUILDER_API_BASE}/auth/builder_apikey"
                params = {"token": token} if token != "session_cookie" else {}

                async with session.post(
                    url,
                    params=params,
                    headers=NOVABUILDER_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get("status") == 200:
                            new_key = data.get("data", {}).get("apiKey", "")
                            if new_key:
                                logger.info("NovaBuilder API Key 重新生成成功")
                                return new_key
                            # data 可能直接是 key 字符串
                            if isinstance(data.get("data"), str) and data["data"]:
                                return data["data"]
                        logger.error(f"NovaBuilder API Key 重新生成失败: {data}")
                        return None
                    else:
                        error_text = await resp.text()
                        logger.error(f"NovaBuilder API Key 重新生成失败: HTTP {resp.status} - {error_text}")
                        return None
        except Exception as e:
            logger.error(f"NovaBuilder API Key 重新生成错误: {e}")
            return None

    async def refresh_key(self) -> dict:
        """刷新 SAuth Key。

        支持三种模式:
            - NovaBuilder 模式: 自动登录 novabuilder.pro, 验证/重新生成 API Key
            - 真实模式: 使用管理员手动设置的 Key
            - 模拟模式: 生成假 Key

        所有真实模式失败时自动降级为模拟模式, 保证机器人可继续运行。

        Returns:
            {"success": bool, "key": str, "expires_at": float, "mode": str, "error": str}
        """
        async with self._lock:
            # NovaBuilder 模式: 有 NovaBuilder 凭据
            if self._novabuilder_username and self._novabuilder_password:
                return await self._refresh_via_novabuilder()

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

            # 真实模式: 使用管理员设置的 Key (不调用外部 API, 直接延长有效期)
            if self._key:
                # 验证当前 Key 是否仍然有效
                if self._novabuilder_apikey:
                    is_valid = await self._validate_apikey(self._novabuilder_apikey)
                    if is_valid:
                        self._key_expires_at = time.time() + KEY_VALIDITY_SECONDS
                        logger.info("nv1 Key 仍然有效, 已延长有效期")
                        return {
                            "success": True,
                            "key": self._key,
                            "expires_at": self._key_expires_at,
                            "mode": "real",
                        }
                    else:
                        logger.warning("nv1 Key 已失效, 降级为模拟模式")
                        return await self._fallback_to_mock("API Key 已失效")

                # 没有NovaBuilder API Key, 直接使用当前 Key
                self._key_expires_at = time.time() + KEY_VALIDITY_SECONDS
                logger.info("nv1 Key 已刷新 (真实模式, 直接使用)")
                return {
                    "success": True,
                    "key": self._key,
                    "expires_at": self._key_expires_at,
                    "mode": "real",
                }

            # 没有配置任何 Key, 降级为模拟模式
            return await self._fallback_to_mock("未配置 Key")

    async def _refresh_via_novabuilder(self) -> dict:
        """通过 NovaBuilder API 刷新 Key。

        流程:
            1. 如果有当前 API Key, 先验证是否有效
            2. 如果 Key 有效且未过期, 直接使用
            3. 如果 Key 无效或过期, 登录 novabuilder.pro 重新生成
            4. 如果重新生成失败, 降级为模拟模式
        """
        # 1. 验证现有 Key
        if self._novabuilder_apikey and self.is_valid():
            is_valid = await self._validate_apikey(self._novabuilder_apikey)
            if is_valid:
                self._key = self._novabuilder_apikey
                self._key_expires_at = time.time() + KEY_VALIDITY_SECONDS
                logger.info("NovaBuilder API Key 验证通过, 继续使用")
                await self._save_apikey_to_db(self._key, self._key_expires_at)
                return {
                    "success": True,
                    "key": self._key,
                    "expires_at": self._key_expires_at,
                    "mode": "novabuilder",
                }

        # 2. 重新生成 API Key
        logger.info("NovaBuilder API Key 需要刷新, 尝试重新生成...")
        new_key = await self._regenerate_apikey()
        if new_key:
            self._novabuilder_apikey = new_key
            self._key = new_key
            self._key_expires_at = time.time() + KEY_VALIDITY_SECONDS
            self._mock_mode = False
            logger.info("NovaBuilder API Key 已重新生成")
            await self._save_apikey_to_db(new_key, self._key_expires_at)
            return {
                "success": True,
                "key": self._key,
                "expires_at": self._key_expires_at,
                "mode": "novabuilder",
            }

        # 3. 重新生成失败, 降级为模拟模式
        logger.error("NovaBuilder API Key 重新生成失败, 降级为模拟模式")
        return await self._fallback_to_mock("NovaBuilder API Key 重新生成失败")

    async def _fallback_to_mock(self, error: str) -> dict:
        """降级为模拟模式, 生成 mock Key。

        当真实模式刷新失败时调用, 保证机器人可以继续运行。
        """
        self._mock_mode = True
        self._key = f"mock_sauth_{secrets.token_hex(16)}"
        self._key_expires_at = time.time() + KEY_VALIDITY_SECONDS
        logger.warning(f"nv1 已降级为模拟模式 (原因: {error})")
        return {
            "success": True,
            "key": self._key,
            "expires_at": self._key_expires_at,
            "mode": "mock_fallback",
            "fallback_reason": error,
        }

    def get_status(self) -> dict:
        """获取当前状态。"""
        remaining = self.get_remaining_seconds()
        mode = "mock" if self._mock_mode else "novabuilder" if self._novabuilder_username else "real"
        return {
            "configured": self.is_configured(),
            "valid": self.is_valid(),
            "mode": mode,
            "auth_server": self.get_auth_server(),
            "key_preview": self._key[:16] + "..." if len(self._key) > 16 else self._key,
            "expires_at": self._key_expires_at if self._key_expires_at > 0 else None,
            "remaining_seconds": remaining,
            "remaining_days": round(remaining / 86400, 1) if remaining else None,
            "needs_refresh": remaining is not None and remaining < REFRESH_THRESHOLD,
            "novabuilder": {
                "configured": bool(self._novabuilder_username),
                "username": self._novabuilder_username or "",
                "has_apikey": bool(self._novabuilder_apikey),
                "apikey_preview": (self._novabuilder_apikey[:16] + "...") if len(self._novabuilder_apikey) > 16 else self._novabuilder_apikey,
            },
        }

    async def init_mock_if_needed(self) -> None:
        """初始化 Key: 先从数据库加载, 再决定是否生成模拟 Key。"""
        # 先尝试从数据库加载已保存的凭据
        loaded = await self.load_from_db()
        if loaded and self._key:
            logger.info("NV1 Key 已从数据库恢复, 无需生成模拟 Key")
            return

        # 如果没有 Key, 生成模拟 Key
        if not self._key:
            await self.refresh_key()


# 全局单例
nv1_manager = NV1Manager()
