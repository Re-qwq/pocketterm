"""认证客户端 - 与 Fatalder / FateArk 认证服务器通信。

使用 aiohttp 发送异步 HTTP 请求,完成网易 Minecraft 租赁服的登录认证流程,
返回包含 ``rental_server_ip`` / ``auth_token`` / ``player_id`` 的 ``AuthResult``。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from .device import DeviceFingerprint
from .exceptions import (
    AccountBannedError,
    AuthError,
    AuthenticationFailedError,
    InvalidCredentialsError,
    MaintenanceError,
    NetworkError,
    RateLimitError,
    ServerFullError,
    ServerNotFoundError,
    ServerRejectedError,
    VersionTooLowError,
)
from .sauth import SauthSession, build_sauth_json

logger = logging.getLogger("pocketterm.mc_auth")

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_AUTH_SERVER: str = "https://nv1.nethard.pro"
DEFAULT_TIMEOUT: float = 30.0

# 协议版本常量(用于登录请求 payload)
GAME_VERSION: str = "1.21.93"
PROTOCOL_VERSION: int = 685

DEFAULT_USER_AGENT: str = f"MinecraftPE/{GAME_VERSION} (Android; PocketTerm)"

# ---------------------------------------------------------------------------
# 错误消息匹配模式(小写比较)
# ---------------------------------------------------------------------------
_BAN_PATTERNS = (
    "banned", "ban", "forbidden", "已被封", "封号", "已封禁",
    "blocked", "blocked by", "账号封禁",
)
_VERSION_PATTERNS = (
    "版本过低", "客户端版本", "请更新", "version too low",
    "upgrade", "outdated", "版本不兼容",
)
_FULL_PATTERNS = (
    "server full", "服务器已满", "已满", "full", "no slot", "没有空位",
)
_NOT_FOUND_PATTERNS = (
    "not found", "不存在", "未找到", "no such server", "服务器不存在", "已下线",
)
_RATE_PATTERNS = (
    "rate limit", "too many", "频率", "限流", "请求过快", "throttle",
)
_MAINTENANCE_PATTERNS = (
    "maintenance", "维护", "维护中", "under maintenance", "不可用",
)


@dataclass
class AuthResult:
    """认证成功结果。

    Attributes:
        rental_server_ip: 租赁服 IP(含端口,如 ``1.2.3.4:19132``)
        auth_token: 认证令牌(用于后续连接租赁服)
        player_id: 玩家 ID(XUID)
        chain_info: JWT身份链(用于MCPE Login数据包)
        raw: 原始响应字典(调试用)
    """
    rental_server_ip: str
    auth_token: str
    player_id: str
    chain_info: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class AuthClient:
    """异步 HTTP 认证客户端。

    使用 ``async with AuthClient(...) as client`` 管理生命周期,
    内部维护单个 aiohttp 会话,退出时自动关闭。

    Example::

        async with AuthClient() as client:
            result = await client.login(server_code="123456")
            print(result.rental_server_ip)
    """

    def __init__(
        self,
        *,
        auth_server: str = DEFAULT_AUTH_SERVER,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self.auth_server = auth_server.rstrip("/")
        self.timeout = timeout
        self.user_agent = user_agent
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------
    async def __aenter__(self) -> "AuthClient":
        timeout_cfg = aiohttp.ClientTimeout(total=self.timeout)
        # trust_env=True 让 aiohttp 读取 HTTP_PROXY/HTTPS_PROXY 环境变量
        self._session = aiohttp.ClientSession(timeout=timeout_cfg, trust_env=True)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        """关闭底层 aiohttp 会话。"""
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError(
                "AuthClient 未在 async with 上下文中使用,无法获取 aiohttp 会话"
            )
        return self._session

    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------
    async def login(
        self,
        *,
        server_code: str,
        server_password: str = "",
        fingerprint: Optional[DeviceFingerprint] = None,
        api_key: str = "",
    ) -> AuthResult:
        """执行登录认证流程。

        Args:
            server_code: 租赁服编号
            server_password: 租赁服密码(可选)
            fingerprint: 设备指纹(可选,未指定时自动生成)
            api_key: API Key(可选,部分服务器需要)

        Returns:
            ``AuthResult`` 实例

        Raises:
            各种 ``AuthError`` 子类异常
        """
        if fingerprint is None:
            fingerprint = DeviceFingerprint.generate()

        logger.info(
            "开始认证 server_code=%s 设备=%s",
            server_code, fingerprint.short_summary(),
        )

        sauth_session = SauthSession.from_fingerprint(
            fingerprint,
            protocol_version=PROTOCOL_VERSION,
        )
        sauth_json_str = build_sauth_json(sauth_session)

        return await self._post_login(
            sauth_json_str=sauth_json_str,
            server_code=server_code,
            server_password=server_password,
            api_key=api_key,
        )

    async def _post_login(
        self,
        *,
        sauth_json_str: str,
        server_code: str,
        server_password: str,
        api_key: str,
    ) -> AuthResult:
        # 1. 获取一次性 secret
        secret = await self._fetch_secret()
        logger.debug("获取 secret 成功: %s...", secret[:20])

        # 2. 构造登录请求
        outer_sauth = json.dumps(
            {"sauth_json": sauth_json_str}, ensure_ascii=False,
        )
        payload: dict[str, Any] = {
            "FBToken": outer_sauth,
            "UserName": "",
            "Password": "",
            "ServerCode": server_code,
            "ServerPassword": server_password,
            "GameVersion": GAME_VERSION,
            "ProtocolVersion": PROTOCOL_VERSION,
            "ClientPublicKey": "",  # 简化版,暂不实现 ECDH 握手
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.auth_server}/api/phoenix/login"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
            "User-Agent": self.user_agent,
        }
        if api_key:
            headers["X-API-Key"] = api_key

        try:
            async with self.session.post(url, data=body, headers=headers) as resp:
                text = await resp.text()
        except aiohttp.ClientError as exc:
            raise NetworkError(f"登录请求失败: {exc}") from exc

        # 3. 处理响应
        self._raise_for_http_status(resp.status, text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ServerRejectedError(
                f"响应不是有效 JSON: {text[:200]}",
                http_status=resp.status,
            ) from exc

        if not isinstance(data, dict):
            raise ServerRejectedError(
                f"响应顶层不是对象: {text[:200]}",
                http_status=resp.status,
            )

        logger.info("认证成功 server_code=%s", server_code)
        return self._parse_response(data)

    async def _fetch_secret(self) -> str:
        """从 ``/api/new`` 获取一次性 secret。"""
        url = f"{self.auth_server}/api/new"
        try:
            async with self.session.get(url) as resp:
                if resp.status == 503:
                    raise MaintenanceError("认证服务器暂时不可用 (503)")
                if resp.status == 429:
                    raise RateLimitError("获取 secret 被限流 (429)")
                if resp.status != 200:
                    body = await resp.text()
                    self._raise_for_http_status(resp.status, body)
                return (await resp.text()).strip()
        except aiohttp.ClientError as exc:
            raise NetworkError(f"获取 secret 网络错误: {exc}") from exc

    # ------------------------------------------------------------------
    # 响应解析与异常映射
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_message(text: str) -> str:
        """尝试从响应文本中提取错误消息。"""
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return text
        if isinstance(data, dict):
            for key in ("message", "Message", "error", "Error", "Information", "info"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    return val
                if isinstance(val, dict):
                    for sub in ("Information", "information", "message", "Message"):
                        sub_val = val.get(sub)
                        if isinstance(sub_val, str) and sub_val:
                            return sub_val
        return text

    @staticmethod
    def _classify_message(message: str) -> Optional[AuthError]:
        """根据错误消息文本匹配具体异常类型(未匹配返回 ``None``)。"""
        msg_lower = message.lower()
        if any(p in msg_lower for p in _BAN_PATTERNS):
            return AccountBannedError(f"账号被封禁: {message}")
        if any(p in message for p in _VERSION_PATTERNS) or any(
            p in msg_lower for p in _VERSION_PATTERNS
        ):
            return VersionTooLowError(f"客户端版本过低: {message}")
        if any(p in msg_lower for p in _FULL_PATTERNS) or any(
            p in message for p in _FULL_PATTERNS
        ):
            return ServerFullError(f"服务器已满: {message}")
        if any(p in msg_lower for p in _NOT_FOUND_PATTERNS) or any(
            p in message for p in _NOT_FOUND_PATTERNS
        ):
            return ServerNotFoundError(f"服务器不存在: {message}")
        if any(p in msg_lower for p in _RATE_PATTERNS) or any(
            p in message for p in _RATE_PATTERNS
        ):
            return RateLimitError(f"请求频率限制: {message}")
        if any(p in msg_lower for p in _MAINTENANCE_PATTERNS) or any(
            p in message for p in _MAINTENANCE_PATTERNS
        ):
            return MaintenanceError(f"服务器维护中: {message}")
        return None

    @classmethod
    def _raise_for_http_status(cls, status: int, text: str) -> None:
        """根据 HTTP 状态码与响应文本抛出适当异常。"""
        if status == 200:
            return

        message = cls._extract_message(text)

        # 按状态码快速映射
        if status == 401 or status == 403:
            err = cls._classify_message(message)
            raise (err or InvalidCredentialsError(
                f"凭证无效 HTTP {status}: {message[:200]}"
            ))
        if status == 404:
            raise ServerNotFoundError(f"服务器不存在 HTTP 404: {message[:200]}")
        if status == 429:
            raise RateLimitError(f"请求频率限制 HTTP 429: {message[:200]}")
        if status == 503:
            raise MaintenanceError(f"服务器维护中 HTTP 503: {message[:200]}")
        if status in (507, 509):
            raise ServerFullError(f"服务器已满 HTTP {status}: {message[:200]}")

        # 兜底:按消息内容分类
        err = cls._classify_message(message)
        if err is not None:
            raise err
        raise ServerRejectedError(
            f"HTTP {status}: {message[:200]}",
            http_status=status,
        )

    def _parse_response(self, data: dict[str, Any]) -> AuthResult:
        """解析认证成功响应,构造 ``AuthResult``。"""
        success = bool(
            data.get("SuccessStates")
            or data.get("success_states")
            or data.get("success")
            or data.get("Success")
        )

        if not success:
            info = ""
            for key in ("Message", "message", "Error", "error"):
                v = data.get(key)
                if isinstance(v, dict):
                    info = v.get("Information", "") or v.get("information", "") or ""
                elif isinstance(v, str):
                    info = v
                if info:
                    break

            err = self._classify_message(info or str(data))
            if err is not None:
                raise err
            raise AuthenticationFailedError(f"认证失败: {info or data}")

        rental_server_ip = str(
            data.get("RentalServerIP")
            or data.get("rental_server_ip")
            or data.get("ServerIP")
            or data.get("server_ip")
            or ""
        )
        auth_token = str(
            data.get("FBToken")
            or data.get("fbtoken")
            or data.get("AuthToken")
            or data.get("auth_token")
            or data.get("Token")
            or ""
        )
        player_id = str(
            data.get("PlayerID")
            or data.get("player_id")
            or data.get("XUID")
            or data.get("xuid")
            or data.get("PlayerXuid")
            or ""
        )

        return AuthResult(
            rental_server_ip=rental_server_ip,
            auth_token=auth_token,
            player_id=player_id,
            chain_info=str(
                data.get("ChainInfo")
                or data.get("chainInfo")
                or data.get("chain_info")
                or ""
            ),
            raw=dict(data),
        )
