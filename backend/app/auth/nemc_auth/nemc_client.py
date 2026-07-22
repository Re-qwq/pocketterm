"""NEMC 认证客户端模块.

从 ``ConsoleAppLogin.NetEase/Http.cs`` 完整移植.

提供网易 Minecraft 中国版的完整认证流程:

- Cookie 登录 (login)
- PE 认证 (pe_login)
- 房间搜索/列表/进入/离开
- 进入游戏/踢人
- 获取用户详情

API 服务器
----------
- PC:       ``https://x19apigatewayobt.nie.netease.com``
- PE:       ``https://g79apigatewayobt.minecraft.cn``
- 联机大厅:  ``https://g79mclobt.minecraft.cn``
- 传输服:    ``https://g79mcltransfer.minecraft.cn``
- 认证服:    ``https://g79authobt.minecraft.cn``
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from ..nemc_crypto import aes_helper, x19crypt

__all__ = [
    "LoginResult",
    "PEResult",
    "NemcClient",
    "PC_API_SERVER",
    "PE_API_SERVER",
    "LOBBY_SERVER",
    "TRANSFER_SERVER",
    "AUTH_SERVER",
]

logger = logging.getLogger("pocketterm.auth.nemc.nemc_client")

# --- API 服务器常量 ---------------------------------------------------------

#: PC API 服务器.
PC_API_SERVER: str = "https://x19apigatewayobt.nie.netease.com"

#: PE API 服务器.
PE_API_SERVER: str = "https://g79apigatewayobt.minecraft.cn"

#: 联机大厅服务器.
LOBBY_SERVER: str = "https://g79mclobt.minecraft.cn"

#: 传输服服务器.
TRANSFER_SERVER: str = "https://g79mcltransfer.minecraft.cn"

#: 认证服服务器.
AUTH_SERVER: str = "https://g79authobt.minecraft.cn"

#: 默认游戏版本.
DEFAULT_VERSION: str = "1.14.6.45947"

#: 默认 User-Agent.
_USER_AGENT: str = "libhttpclient/1.0.0.0"


# --- 数据类 ----------------------------------------------------------------

@dataclass
class LoginResult:
    """Cookie 登录结果."""

    uid: str = ""
    """用户 UID (entity_id)."""
    pe_uid: str = ""
    """PE 平台 UID."""
    login_src_token: str = ""
    """解密后的登录令牌 (LoginSRCToken)."""
    login_d_token: str = ""
    """加密的登录令牌 (LoginDToken)."""
    aid: str = ""
    """认证 aid."""
    otp_token: str = ""
    """OTP 令牌."""
    raw_response: str = ""
    """原始响应文本."""


@dataclass
class PEResult:
    """PE 认证结果."""

    uid: str = ""
    pe_uid: str = ""
    login_src_token: str = ""
    login_d_token: str = ""
    min_patch_version: str = ""
    raw_response: str = ""


# --- 认证客户端 ------------------------------------------------------------

class NemcClient:
    """网易 Minecraft 中国版认证客户端.

    从 ``ConsoleAppLogin.NetEase/Http.cs`` 移植.

    Parameters
    ----------
    auth_server
        认证服 URL, 默认 ``https://g79authobt.minecraft.cn``.
    pc_api_server
        PC API 服 URL.
    pe_api_server
        PE API 服 URL.
    lobby_server
        联机大厅 URL.
    transfer_server
        传输服 URL.
    version
        游戏版本号.
    """

    def __init__(
        self,
        auth_server: str = AUTH_SERVER,
        pc_api_server: str = PC_API_SERVER,
        pe_api_server: str = PE_API_SERVER,
        lobby_server: str = LOBBY_SERVER,
        transfer_server: str = TRANSFER_SERVER,
        version: str = DEFAULT_VERSION,
    ) -> None:
        self._auth_server = auth_server
        self._pc_api_server = pc_api_server
        self._pe_api_server = pe_api_server
        self._lobby_server = lobby_server
        self._transfer_server = transfer_server
        self._version = version

        # 登录状态.
        self._uid: str = ""
        self._pe_uid: str = ""
        self._login_src_token: str = ""
        self._login_d_token: str = ""
        self._aid: str = ""
        self._otp_token: str = ""

        # HTTP 客户端.
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def uid(self) -> str:
        """当前用户 UID."""
        return self._uid

    @property
    def pe_uid(self) -> str:
        """当前 PE UID."""
        return self._pe_uid

    @property
    def login_src_token(self) -> str:
        """当前登录令牌."""
        return self._login_src_token

    @property
    def login_d_token(self) -> str:
        """当前加密登录令牌."""
        return self._login_d_token

    async def _get_client(self) -> httpx.AsyncClient:
        """获取或创建 HTTP 客户端."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0),
                verify=False,
            )
        return self._client

    async def close(self) -> None:
        """关闭 HTTP 客户端."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # --- 内部 HTTP 方法 ----------------------------------------------------

    def _compute_token(self, url: str, body: str, token: str = "") -> str:
        """计算动态用户令牌.

        使用 ``x19Crypt.ComputeDynamicToken`` 计算. C# 原版使用
        ``CoreNative.ComputeDynamicToken`` (原生 DLL), 此处使用纯 Python
        管理版本.
        """
        return x19crypt.ComputeDynamicToken(url, body, token or "")

    async def _post_plain(
        self,
        host: str,
        url: str,
        body: str,
        uid: str = "",
        token: str = "",
    ) -> str:
        """明文 POST 请求 (不加密 body, 带动态 token 头).

        对应 C# ``postAPI`` / ``postAPIPC`` / ``postAPIHost`` / ``x19post``.
        """
        client = await self._get_client()
        dyn_token = self._compute_token(url, body, token or self._login_src_token)
        headers = {
            "Content-Type": "application/json",
            "user-token": dyn_token,
            "user-id": uid or self._uid,
        }
        full_url = host + url
        logger.debug("POST plain %s", full_url)
        resp = await client.post(full_url, content=body, headers=headers)
        text = resp.text
        logger.debug("Response: %s", text[:200])
        return text

    async def _post_encrypted(
        self,
        host: str,
        url: str,
        body: str,
        uid: str = "",
        token: str = "",
    ) -> str:
        """加密 POST 请求 (第一组密钥, 对应 ``CoreNative.HttpEncrypt``).

        对应 C# ``EncyptPOST`` / ``CoreNative.HttpEncrypt`` +
        ``CoreNative.ParseLoginResponse``.
        """
        client = await self._get_client()
        dyn_token = self._compute_token(url, body, token or self._login_src_token)
        encrypted_body = x19crypt.HttpEncrypt(body.encode("utf-8"))
        headers = {
            "Content-Type": "application/json",
            "user-token": dyn_token,
            "user-id": uid or self._uid,
        }
        full_url = host + url
        logger.debug("POST encrypted %s", full_url)
        resp = await client.post(full_url, content=encrypted_body, headers=headers)
        decrypted = x19crypt.ParseLoginResponse(resp.content)
        logger.debug("Decrypted: %s", decrypted[:200])
        return decrypted

    async def _post_g79v12(
        self,
        host: str,
        url: str,
        body: str,
        uid: str = "",
        token: str = "",
        hex_token: bool = False,
    ) -> str:
        """g79v12 加密 POST 请求.

        对应 C# ``EncyptPOST_g79v12`` (hex_token=False) 或
        ``HttpPost`` (hex_token=True).
        """
        client = await self._get_client()
        dyn_token = self._compute_token(url, body, token or self._login_src_token)
        if hex_token:
            # HttpPost: user-token = hex(ascii(token)).
            user_token = dyn_token.encode("ascii").hex().upper()
            headers = {
                "Content-Type": "application/json",
                "user-token": user_token,
                "user-id": uid or self._uid,
                "User-Agent": _USER_AGENT,
            }
        else:
            headers = {
                "Content-Type": "application/json",
                "user-token": dyn_token,
                "user-id": uid or self._uid,
            }
        encrypted_body = x19crypt.HttpEncrypt_g79v12(body.encode("utf-8")).hex()
        full_url = host + url
        logger.debug("POST g79v12 %s", full_url)
        resp = await client.post(full_url, content=encrypted_body, headers=headers)
        decrypted = x19crypt.HttpDecrypt_g79v12(bytes.fromhex(resp.text))
        logger.debug("Decrypted: %s", decrypted[:200])
        return decrypted.decode("utf-8")

    @staticmethod
    def _parse_json_safely(text: str) -> Optional[dict]:
        """安全解析 JSON, 处理尾部垃圾数据.

        ``ParseLoginResponse`` 不去除随机填充, JSON 尾部可能有非 JSON 字符.
        此方法找到最后一个 ``}`` 并截断.
        """
        text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试找到最后一个完整的 JSON 对象.
            last_brace = text.rfind("}")
            if last_brace >= 0:
                try:
                    return json.loads(text[: last_brace + 1])
                except json.JSONDecodeError:
                    pass
            return None

    @staticmethod
    def _extract_sauth_json(cookie: str) -> str:
        """从 cookie/sauth 字符串中提取 sauth_json.

        如果 cookie 是 JSON 且包含 ``sauth_json`` 字段, 提取该字段.
        否则直接返回原字符串.
        """
        try:
            obj = json.loads(cookie)
            if isinstance(obj, dict) and "sauth_json" in obj:
                # sauth_json 本身可能是 JSON 字符串或对象.
                sj = obj["sauth_json"]
                if isinstance(sj, str):
                    return sj
                return json.dumps(sj)
            return cookie
        except (json.JSONDecodeError, TypeError):
            return cookie

    # --- 登录流程 ----------------------------------------------------------

    async def login(self, cookie: str) -> LoginResult:
        """Cookie 登录.

        从 ``Http.Login`` 移植.

        流程:
        1. POST ``{auth_server}/login-otp`` (body=cookie) → 获取 aid + otp_token
        2. 构建认证 JSON (version, aid, otp_token, sauth_json)
        3. POST ``{auth_server}/authentication-otp`` (加密) → 获取 UID + token
        4. 解密 token (AES-128-CBC, skip(8).take(rest-8))
        5. POST ``{pc_api}/user-official-account-info/check`` → 获取 PE UID

        Parameters
        ----------
        cookie
            登录 Cookie 字符串 (或包含 sauth_json 的 JSON).

        Returns
        -------
        LoginResult
            登录结果.
        """
        result = LoginResult()
        client = await self._get_client()

        if not cookie:
            logger.error("Cookie 为空")
            return result

        # Step 1: POST /login-otp (明文, 无加密).
        logger.info("Step 1: 请求 /login-otp ...")
        headers = {"Content-Type": "application/json"}
        resp = await client.post(
            f"{self._auth_server}/login-otp",
            content=cookie,
            headers=headers,
        )
        login_resp_text = resp.text
        logger.debug("login-otp response: %s", login_resp_text[:200])

        login_resp = self._parse_json_safely(login_resp_text)
        if not login_resp or login_resp.get("code") != 0:
            logger.error("login-otp 失败: %s", login_resp_text)
            return result

        entity = login_resp.get("entity", {})
        aid = entity.get("aid", "")
        otp_token = entity.get("otp_token", "")
        result.aid = aid
        result.otp_token = otp_token
        logger.info("获取 aid=%s, otp_token=%s...", aid, otp_token[:16])

        # Step 2: 构建认证 JSON.
        sauth_json = self._extract_sauth_json(cookie)
        auth_json = json.dumps({
            "sa_data": None,
            "sauth_json": sauth_json,
            "version": {"version": self._version},
            "aid": aid,
            "otp_token": otp_token,
        }, ensure_ascii=False)
        logger.debug("Auth JSON: %s", auth_json[:200])

        # Step 3: POST /authentication-otp (加密).
        logger.info("Step 2: 请求 /authentication-otp (加密) ...")
        auth_url = "/authentication-otp"
        encrypted_body = x19crypt.HttpEncrypt(auth_json.encode("utf-8"))
        # 登录前无 token, 使用空字符串.
        dyn_token = self._compute_token(auth_url, auth_json, "")
        headers = {
            "Content-Type": "application/json",
            "user-token": dyn_token,
            "user-id": "",
        }
        resp = await client.post(
            f"{self._auth_server}{auth_url}",
            content=encrypted_body,
            headers=headers,
        )
        auth_resp_bytes = resp.content
        # 解密响应 (ParseLoginResponse 不去随机填充).
        auth_resp_text = x19crypt.ParseLoginResponse(auth_resp_bytes)
        logger.debug("auth response: %s", auth_resp_text[:200])

        auth_resp = self._parse_json_safely(auth_resp_text)
        if not auth_resp:
            logger.error("authentication-otp 解析失败: %s", auth_resp_text)
            return result

        auth_entity = auth_resp.get("entity", {})
        uid = auth_entity.get("entity_id", "")
        d_token = auth_entity.get("token", "")
        result.uid = uid
        result.login_d_token = d_token
        result.raw_response = auth_resp_text

        # Step 4: 解密 token.
        try:
            src_token = aes_helper.mclnet_get_decrypt_token(d_token)
            result.login_src_token = src_token
        except Exception as e:
            logger.error("Token 解密失败: %s", e)
            return result

        logger.info("登录成功! UID=%s", uid)

        # 更新内部状态.
        self._uid = uid
        self._login_src_token = src_token
        self._login_d_token = d_token
        self._aid = aid
        self._otp_token = otp_token

        # Step 5: 获取 PE UID.
        try:
            pe_uid = await self._get_pe_uid(uid)
            result.pe_uid = pe_uid
            self._pe_uid = pe_uid
            logger.info("PE UID=%s", pe_uid)
        except Exception as e:
            logger.warning("获取 PE UID 失败: %s", e)

        return result

    async def _get_pe_uid(self, uid: str) -> str:
        """获取 PE UID (POST /user-official-account-info/check)."""
        body = json.dumps({"entity_id": uid})
        resp_text = await self._post_plain(
            self._pc_api_server,
            "/user-official-account-info/check",
            body,
            uid=uid,
            token=self._login_src_token,
        )
        resp = self._parse_json_safely(resp_text)
        if resp:
            return resp.get("entity", {}).get("entity_id", "")
        return ""

    # --- PE 认证 -----------------------------------------------------------

    async def pe_login(
        self,
        engine_version: str,
        patch_version: str,
        sauth_json: str,
        seed: str = "",
        sign: str = "",
    ) -> PEResult:
        """PE 认证.

        从 ``Http.PE_Login`` + ``Http.LoadToken`` 移植.

        流程:
        1. 构建 PE 认证 JSON (engine_version, patch_version, sauth_json, seed, sign)
        2. POST ``{pe_server}/pe-authentication`` (g79v12 加密) → 获取 login OTP
        3. 用 login OTP 调用 ``/authentication-otp`` 获取 UID + token
        4. 解密 token

        Parameters
        ----------
        engine_version
            引擎版本 (如 ``"1.0.0.7"``).
        patch_version
            补丁版本.
        sauth_json
            sauth JSON 字符串.
        seed
            随机种子 (GUID). 如不提供则自动生成.
        sign
            Base64 编码的签名. 如不提供则为空.

        Returns
        -------
        PEResult
            PE 认证结果.
        """
        result = PEResult()
        if not seed:
            seed = str(uuid.uuid4()).replace("-", "")

        # 构建 PE 消息.
        message = (
            engine_version
            + "44d2991bd358c4a877cb21636a7f3df1"
            + patch_version
            + "23825e3d68a134ee8bdb450cf7d5561c2b3e7ca013bb30a74d822579860c042b"
            + seed
        )

        # 解析 sauth_json.
        try:
            sauth_obj = json.loads(sauth_json)
            if isinstance(sauth_obj, dict) and "sauth_json" in sauth_obj:
                sauth_obj = json.loads(sauth_obj["sauth_json"])
        except (json.JSONDecodeError, TypeError):
            sauth_obj = sauth_json

        # 构建 PE 请求 JSON.
        pe_request = {
            "sa_data": None,
            "engine_version": engine_version,
            "patch_version": patch_version,
            "message": message,
            "sauth_json": sauth_obj,
            "seed": seed,
            "sign": sign,
        }
        pe_json = json.dumps(pe_request, ensure_ascii=False)
        logger.debug("PE request: %s", pe_json[:200])

        # POST /pe-authentication (g79v12 加密).
        logger.info("PE 认证: 请求 /pe-authentication ...")
        pe_resp_text = await self._post_g79v12(
            self._pe_api_server,
            "/pe-authentication",
            pe_json,
            uid=self._uid,
            token=self._login_src_token,
        )

        # 处理 min_patch_version 字段.
        marker = '"min_patch_version":""}}'
        idx = pe_resp_text.find(marker)
        if idx != -1:
            pe_resp_text = pe_resp_text[: idx + len(marker)]
            result.min_patch_version = ""

        result.raw_response = pe_resp_text
        logger.debug("PE response: %s", pe_resp_text[:200])

        # 使用 PE 响应作为 login OTP 进行认证.
        await self._load_token(pe_resp_text)

        result.uid = self._uid
        result.pe_uid = self._pe_uid
        result.login_src_token = self._login_src_token
        result.login_d_token = self._login_d_token

        return result

    async def _load_token(self, login_otp: str) -> None:
        """使用 login OTP 进行认证.

        从 ``Http.LoadToken`` 移植.

        1. 加密 login OTP
        2. POST ``/authentication-otp``
        3. 解密 token
        4. 获取 PE UID
        """
        logger.info("LoadToken: 认证中 ...")
        auth_url = "/authentication-otp"
        encrypted_body = x19crypt.HttpEncrypt(login_otp.encode("utf-8"))
        dyn_token = self._compute_token(auth_url, login_otp, "")

        client = await self._get_client()
        headers = {
            "Content-Type": "application/json",
            "user-token": dyn_token,
            "user-id": "",
        }
        resp = await client.post(
            f"{self._auth_server}{auth_url}",
            content=encrypted_body,
            headers=headers,
        )
        auth_text = x19crypt.ParseLoginResponse(resp.content)
        logger.debug("LoadToken response: %s", auth_text[:200])

        auth_resp = self._parse_json_safely(auth_text)
        if not auth_resp:
            logger.error("LoadToken 解析失败")
            return

        entity = auth_resp.get("entity", {})
        uid = entity.get("entity_id", "")
        d_token = entity.get("token", "")

        self._uid = uid
        self._login_d_token = d_token
        try:
            self._login_src_token = aes_helper.mclnet_get_decrypt_token(d_token)
        except Exception as e:
            logger.error("Token 解密失败: %s", e)

        logger.info("LoadToken 成功! UID=%s", uid)

        # 获取 PE UID.
        try:
            self._pe_uid = await self._get_pe_uid(uid)
        except Exception as e:
            logger.warning("获取 PE UID 失败: %s", e)

    # --- 房间操作 ----------------------------------------------------------

    async def search_room(
        self,
        res_id: str,
        keyword: str,
    ) -> List[dict]:
        """搜索房间.

        对应 C# ``LobbySearch`` + ``RoomWithName``.

        Parameters
        ----------
        res_id
            资源 ID.
        keyword
            搜索关键词 (房间名).

        Returns
        -------
        list[dict]
            搜索结果列表.
        """
        # 联机大厅搜索.
        body = json.dumps({
            "keyword": keyword,
            "length": 10,
            "offset": 0,
            "version": self._version,
        })
        resp_text = await self._post_plain(
            self._lobby_server,
            "/online-lobby-room/query/search-by-name-v2",
            body,
        )
        results = []
        resp = self._parse_json_safely(resp_text)
        if resp and "entities" in resp:
            results.extend(resp["entities"])
        return results

    async def list_rooms(self, res_id: str) -> List[dict]:
        """列出房间.

        对应 C# ``RoomList``.

        Parameters
        ----------
        res_id
            资源 ID.

        Returns
        -------
        list[dict]
            房间列表.
        """
        body = json.dumps({
            "res_id": res_id,
            "version": self._version,
            "with_friend": True,
            "offset": 0,
            "length": 10,
        })
        resp_text = await self._post_plain(
            self._pc_api_server,
            "/online-lobby-room/query/list-room-by-res-id",
            body,
        )
        resp = self._parse_json_safely(resp_text)
        if resp and "entities" in resp:
            return resp["entities"]
        return []

    async def enter_room(self, room_id: str) -> dict:
        """进入房间.

        对应 C# ``joined``.

        Parameters
        ----------
        room_id
            房间 ID.

        Returns
        -------
        dict
            进入结果.
        """
        body = json.dumps({
            "room_id": room_id,
            "password": "",
            "check_visibilily": True,
        })
        resp_text = await self._post_plain(
            self._pe_api_server,
            "/online-lobby-room-enter",
            body,
        )
        return self._parse_json_safely(resp_text) or {}

    async def leave_room(self) -> dict:
        """离开房间.

        对应 C# ``left``.
        """
        body = json.dumps({"room_id": ""})
        resp_text = await self._post_plain(
            self._pc_api_server,
            "/online-lobby-room-enter/leave-room",
            body,
        )
        return self._parse_json_safely(resp_text) or {}

    async def enter_game(self) -> dict:
        """进入游戏.

        对应 C# ``GetIP`` → ``postAPI("", "/online-lobby-game-enter")``.
        """
        resp_text = await self._post_plain(
            self._pe_api_server,
            "/online-lobby-game-enter",
            "",
        )
        return self._parse_json_safely(resp_text) or {}

    async def kick_player(self, player_id: str) -> dict:
        """踢出玩家.

        对应 C# ``Result`` 中的 kick 逻辑.

        Parameters
        ----------
        player_id
            玩家 ID (user_id).

        Returns
        -------
        dict
            操作结果.
        """
        body = json.dumps({
            "room_id": "",
            "user_id": int(player_id) if player_id.isdigit() else player_id,
        })
        resp_text = await self._post_plain(
            self._pc_api_server,
            "/online-lobby-member-kick",
            body,
        )
        return self._parse_json_safely(resp_text) or {}

    async def get_user_detail(self) -> dict:
        """获取用户详情.

        对应 C# ``GetName`` → ``POST /user-detail``.
        """
        resp_text = await self._post_plain(
            self._pc_api_server,
            "/user-detail",
            "",
        )
        return self._parse_json_safely(resp_text) or {}

    # --- 辅助方法 ----------------------------------------------------------

    async def search_room_with_name(self, name: str) -> dict:
        """通过名称搜索房间 (传输服).

        对应 C# ``RoomWithName``.
        """
        body = json.dumps({"name": name, "uid": self._uid})
        resp_text = await self._post_plain(
            self._transfer_server,
            "/room-with-name",
            body,
        )
        return self._parse_json_safely(resp_text) or {}

    async def __aenter__(self) -> "NemcClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()
