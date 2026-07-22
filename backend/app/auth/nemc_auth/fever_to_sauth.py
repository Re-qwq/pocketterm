"""Fever Token 转 sauth_json 模块.

从 ``FeverToSauth/FeverAuth.cs`` 移植.

提供将网易云游戏 FeverToken 转换为 Minecraft 中国版认证所需的
sauth_json 的功能.

流程
----

1. 解码 FeverToken (Base64 JSON: sdkuid, sessionid, deviceid)
2. POST ``https://service.mkey.163.com/mpay/api/users/create_ticket`` → 获取 ticket
3. POST ``https://service.mkey.163.com/mpay/api/users/login/ticket`` → 获取新 sessionid
4. 构建 sauth_json (包含 sdkuid, sessionid, deviceid, platform 等)
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

__all__ = [
    "FeverToken",
    "SauthResult",
    "FeverAuth",
    "CREATE_TICKET_URL",
    "LOGIN_TICKET_URL",
]

logger = logging.getLogger("pocketterm.auth.nemc.fever_to_sauth")

# --- API 常量 ---------------------------------------------------------------

#: 创建 ticket 的 API URL.
CREATE_TICKET_URL: str = "https://service.mkey.163.com/mpay/api/users/create_ticket"

#: 通过 ticket 登录的 API URL.
LOGIN_TICKET_URL: str = "https://service.mkey.163.com/mpay/api/users/login/ticket"

#: 固定的 UDID (从 C# 源码硬编码).
_FIXED_UDID: str = "4062C17975B3EDA2E328FF52C4F84D5F"

#: 固定的 client_login_sn (从 C# 源码硬编码).
_FIXED_CLIENT_LOGIN_SN: str = "F1A6560B3E6028C74A4616693EA21430"

#: mcount_app_key (从 C# 源码硬编码).
_MCOUNT_APP_KEY: str = "EEkEEXLymcNjM42yLY3Bn6AO15aGy4yq"


# --- 数据类 -----------------------------------------------------------------

@dataclass
class FeverToken:
    """FeverToken 解码后的内容.

    FeverToken 是 Base64 编码的 JSON, 包含以下字段.
    """

    sdkuid: str = ""
    """SDK 用户 ID."""
    sessionid: str = ""
    """会话 ID."""
    deviceid: str = ""
    """设备 ID."""


@dataclass
class SauthResult:
    """sauth_json 转换结果."""

    sauth_json: str = ""
    """完整的 sauth_json 字符串 (JSON 序列化后的外层对象)."""
    sdkuid: str = ""
    """SDK 用户 ID."""
    sessionid: str = ""
    """新的会话 ID (通过 ticket 登录后获取)."""
    deviceid: str = ""
    """设备 ID."""
    ip: str = ""
    """客户端 IP (从登录响应中获取)."""
    sdk_version: str = ""
    """SDK 版本 (从登录响应中获取)."""


# --- Fever 认证 -------------------------------------------------------------

class FeverAuth:
    """FeverToken 转 sauth_json 认证器.

    从 ``FeverToSauth/FeverAuth.cs`` 移植.

    Parameters
    ----------
    timeout
        HTTP 请求超时时间 (秒).
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                verify=False,
            )
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 客户端."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _base64_decode(text: str) -> str:
        """Base64 解码为 UTF-8 字符串.

        从 ``FeverAuth.Base64UnEncode`` 移植.
        """
        raw = base64.b64decode(text)
        return raw.decode("utf-8")

    @staticmethod
    def _base64_encode(text: str) -> str:
        """UTF-8 字符串编码为 Base64.

        从 ``FeverAuth.Base64Encode`` 移植.
        """
        return base64.b64encode(text.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_fever_token(token: str) -> FeverToken:
        """解码 FeverToken.

        FeverToken 是 Base64 编码的 JSON, 包含 sdkuid, sessionid, deviceid.

        Parameters
        ----------
        token
            FeverToken 字符串 (Base64 编码).

        Returns
        -------
        FeverToken
            解码后的 FeverToken 对象.
        """
        decoded = FeverAuth._base64_decode(token)
        obj = json.loads(decoded)
        return FeverToken(
            sdkuid=obj.get("sdkuid", ""),
            sessionid=obj.get("sessionid", ""),
            deviceid=obj.get("deviceid", ""),
        )

    def _build_create_ticket_body(self, fever_token: FeverToken) -> str:
        """构建 create_ticket 请求体.

        从 C# 源码硬编码的 URL-encoded form data.
        """
        # 注意: 这些参数值直接从 C# 源码复制, 部分包含固定的时间戳/ID.
        params = (
            f"app_channel=netease.allysdk3rd"
            f"&app_mode=2"
            f"&app_type=games"
            f"&arch=win_x64"
            f"&cv=c4.2.0"
            f"&device_id={fever_token.deviceid}"
            f"&game_id=aecglf6ee4aaaarz-g-a50"
            f"&gv=1.17.0.0"
            f"&mcount_app_key={_MCOUNT_APP_KEY}"
            f"&mcount_transaction_id=6"
            f"&process_id=3120"
            f"&sv=10.0.19045"
            f"&token={fever_token.sessionid}"
            f"&transid={_FIXED_UDID}_1731819663617_100030765"
            f"&uni_transaction_id={_FIXED_UDID}_1731819695814_100030869"
            f"&updater_cv=c1.0.0"
            f"&user_id={fever_token.sdkuid}"
        )
        return params

    def _build_login_ticket_body(self, ticket: str, device_id: str) -> str:
        """构建 login/ticket 请求体.

        从 C# 源码硬编码的 URL-encoded form data.
        """
        params = (
            f"app_channel=a50_sdk_cn"
            f"&app_mode=2"
            f"&app_type=games"
            f"&arch=win_x32"
            f"&cv=c4.5.0"
            f"&device_id={device_id}"
            f"&game_id=aecfrxodyqaaaajp-g-x19"
            f"&gv=1.14.18.24399"
            f"&mcount_app_key={_MCOUNT_APP_KEY}"
            f"&mcount_transaction_id=9c4ae54f-a4de-11ef-8ba4-4b696ee8d88b-2"
            f"&opt_fields=nickname%2Cavatar%2Crealname_status%2Cmobile_bind_status%2Cmask_related_mobile%2Crelated_login_status"
            f"&process_id=3784"
            f"&sv=10.0.19045"
            f"&ticket={ticket}"
            f"&transid={_FIXED_UDID}_1731846146081_100018943"
            f"&uni_transaction_id={_FIXED_UDID}_1731846146300_100018943"
            f"&updater_cv=c1.0.0"
        )
        return params

    @staticmethod
    def _build_sauth_json(
        fever_token: FeverToken,
        login_resp: dict,
    ) -> str:
        """构建 sauth_json.

        从 ``FeverAuth.FeverToSauth`` 中的 sauth_json 构建逻辑移植.

        Parameters
        ----------
        fever_token
            解码后的 FeverToken (sessionid 已更新为新值).
        login_resp
            login/ticket 的 JSON 响应.

        Returns
        -------
        str
            完整的 sauth_json 字符串 (外层 JSON 对象的序列化).
        """
        user = login_resp.get("user", {})
        pc_ext_info = user.get("pc_ext_info", {})

        src_client_ip = pc_ext_info.get("src_client_ip", "")
        src_sdk_version = pc_ext_info.get("src_sdk_version", "")

        # 构建 aim_info (JSON 字符串).
        aim_info = json.dumps({
            "aim": src_client_ip,
            "country": "CN",
            "tz": "+0800",
        }, ensure_ascii=False)

        # 构建 sauth 内层对象.
        sauth = {
            "gameid": "x19",
            "login_channel": "netease",
            "app_channel": "netease",
            "platform": "pc",
            "sdkuid": fever_token.sdkuid,
            "sessionid": fever_token.sessionid,
            "sdk_version": src_sdk_version,
            "udid": _FIXED_UDID,
            "deviceid": fever_token.deviceid,
            "aim_info": aim_info,
            "client_login_sn": _FIXED_CLIENT_LOGIN_SN,
            "source_platform": "pc",
            "ip": src_client_ip,
        }

        # 外层对象: {"sauth_json": "<serialized sauth>"}.
        outer = {"sauth_json": json.dumps(sauth, ensure_ascii=False)}
        return json.dumps(outer, ensure_ascii=False)

    async def fever_to_sauth(self, fever_token: str) -> SauthResult:
        """将 FeverToken 转换为 sauth_json.

        从 ``FeverAuth.FeverToSauth`` 移植.

        流程:
        1. 解码 FeverToken (Base64 JSON)
        2. POST create_ticket → 获取 ticket
        3. POST login/ticket → 获取新 sessionid
        4. 构建 sauth_json

        Parameters
        ----------
        fever_token
            FeverToken 字符串 (Base64 编码的 JSON).

        Returns
        -------
        SauthResult
            转换结果, 包含 sauth_json 和相关字段.
        """
        result = SauthResult()
        client = await self._get_client()

        # Step 1: 解码 FeverToken.
        logger.info("解码 FeverToken ...")
        fever = self._decode_fever_token(fever_token)
        result.sdkuid = fever.sdkuid
        result.deviceid = fever.deviceid
        logger.debug("sdkuid=%s, deviceid=%s", fever.sdkuid, fever.deviceid)

        # Step 2: POST create_ticket.
        logger.info("请求 create_ticket ...")
        create_body = self._build_create_ticket_body(fever)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = await client.post(
            CREATE_TICKET_URL,
            content=create_body,
            headers=headers,
        )
        create_resp = resp.json()
        logger.debug("create_ticket response: %s", resp.text[:200])

        ticket = create_resp.get("ticket", "")
        if not ticket:
            logger.error("create_ticket 失败: %s", resp.text)
            return result
        logger.info("获取 ticket 成功")

        # Step 3: POST login/ticket.
        logger.info("请求 login/ticket ...")
        login_body = self._build_login_ticket_body(ticket, fever.deviceid)
        resp = await client.post(
            LOGIN_TICKET_URL,
            content=login_body,
            headers=headers,
        )
        login_resp = resp.json()
        logger.debug("login/ticket response: %s", resp.text[:200])

        # 获取新的 sessionid.
        user = login_resp.get("user", {})
        new_sessionid = user.get("token", "")
        if not new_sessionid:
            logger.error("login/ticket 失败: %s", resp.text)
            return result

        fever.sessionid = new_sessionid
        result.sessionid = new_sessionid

        # 获取 IP 和 SDK 版本.
        pc_ext_info = user.get("pc_ext_info", {})
        result.ip = pc_ext_info.get("src_client_ip", "")
        result.sdk_version = pc_ext_info.get("src_sdk_version", "")

        # Step 4: 构建 sauth_json.
        result.sauth_json = self._build_sauth_json(fever, login_resp)
        logger.info("sauth_json 构建成功")

        return result

    async def __aenter__(self) -> "FeverAuth":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
