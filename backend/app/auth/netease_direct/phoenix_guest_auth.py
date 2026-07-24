"""Phoenix 访客认证客户端 (Fatalder/FastBuilder 兼容)。

本模块实现了 Fatalder/FastBuilder Phoenix 的访客认证流程,
使用随机生成的设备指纹构建 netease 频道 sauth_json,
然后通过第三方 Phoenix 认证服务器获取游戏连接凭证。

逆向来源:
  - lobbyd/auth/guest.go (Go 版本, Fatalder 兼容)
  - lobbyd/auth/sauth.go (sauth_json 构建)
  - lobbyd/auth/device.go (设备指纹生成)
  - lobbyd/auth/crypto.go (ECDH P-384 + AES-256-CFB-1 加密)
  - retalcer/retalcer/guest/auth.py (Python 原始版本)
  - tooldelta_antiban_code/launcher/neo_conn.py (AccountOptions.AuthServer)

认证流程:
  1. 生成随机设备指纹 (sdkuid, udid, deviceid, client_login_sn)
  2. 构建 netease 频道 sauth_json (Fatalder 格式)
  3. GET /api/new 获取一次性 secret
  4. POST /api/phoenix/login 提交 sauth_json + 服务器信息
  5. 返回 FBToken, ChainInfo 等游戏连接数据

关键发现:
  - Phoenix 认证完全绕过 4399com 频道 (code=32 问题)
  - 使用 netease 频道 (login_channel="netease", app_channel="netease")
  - sessionid 使用特殊格式: "1-" + base64url(json({s, odsi, si, u, t, g_i}))
  - sdkuid 为随机 16 位小写字母数字 (非真实网易账号)
  - 支持多个认证服务器 (fatalder.yeah114.top / api.fastbuilder.pro)

注意:
  - Phoenix 认证服务器可能需要 API Key
  - 服务器状态可能不稳定 (503 错误)
  - 返回的 chainInfo 可直接用于 RakNet 连接
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import string
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("pocketterm.phoenix_guest_auth")

# ============================================================================
# 常量
# ============================================================================

# 已知的 Phoenix 认证服务器
DEFAULT_AUTH_SERVER = "https://fatalder.yeah114.top"
FALLBACK_AUTH_SERVER = "https://api.fastbuilder.pro"

# 默认超时 (秒)
DEFAULT_TIMEOUT = 30.0

# 封禁关键词 (响应中出现时判定为封禁)
_BAN_PATTERNS = [
    "banned", "ban", "forbidden", "已被封", "封号", "已封禁",
    "blocked", "blocked by",
]

# Fatalder sauth_json 常量
# SDK 版本: 使用 3.9.0 (匹配 Community-Bot 真实样本和 PocketTerm constants.py)
# 注意: lobbyd 使用 4.0.0, 但网易 3.9 协议认证服务器期望 3.9.0
DEFAULT_SDK_VERSION = "3.9.0"
DEFAULT_GAME_ID = "x19"
DEFAULT_LOGIN_CHANNEL = "netease"
DEFAULT_APP_CHANNEL = "netease"
DEFAULT_PLATFORM = "pc"
DEFAULT_SOURCE_PLATFORM = "pc"
DEFAULT_AIM_IP = "127.0.0.1"
DEFAULT_AIM_COUNTRY = "CN"
DEFAULT_AIM_TZ = "+0800"
DEFAULT_AIM_TZID = ""

# NetHard 验证服务器 (ToolDelta AUTH_SERVERS 中标记为可用)
NETHARD_AUTH_SERVER = "https://nv1.nethard.pro"

# Fatalder sessionid 构建常量
_GAME_INTERNAL_ID = "aecfrxodyqaaajp"  # g_i 字段

# 随机字符集
_LOWER_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"
_LOWER_HEX = "0123456789abcdef"
_UPPER_HEX = "0123456789ABCDEF"
_LOWER_ALPHA = "abcdefghijklmnopqrstuvwxyz"


# ============================================================================
# 数据类
# ============================================================================

@dataclass
class DeviceFingerprint:
    """Fatalder 兼容设备指纹。

    所有字段均随机生成, 不关联真实网易账号。
    """
    sdkuid: str           # 16 位小写字母数字
    udid: str             # 32 位小写字母数字
    deviceid: str          # 14 位小写字母 + "-" + 1 字母 + 6 hex
    client_login_sn: str  # 32 位大写 HEX
    game_id: str = DEFAULT_GAME_ID
    platform: str = DEFAULT_PLATFORM
    sdk_version: str = DEFAULT_SDK_VERSION
    login_channel: str = DEFAULT_LOGIN_CHANNEL
    app_channel: str = DEFAULT_APP_CHANNEL
    source_platform: str = DEFAULT_SOURCE_PLATFORM
    aim_ip: str = DEFAULT_AIM_IP
    aim_country: str = DEFAULT_AIM_COUNTRY
    aim_tz: str = DEFAULT_AIM_TZ
    aim_tzid: str = DEFAULT_AIM_TZID


@dataclass
class PhoenixAuthResult:
    """Phoenix 认证结果。"""
    success: bool = False
    fb_token: str = ""           # FBToken (双层 JSON 包装的 sauth_json)
    chain_info: str = ""         # ChainInfo (用于 RakNet 连接)
    server_message: str = ""
    master_name: str = ""
    rental_server_ip: str = ""
    bot_level: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    fingerprint: Optional[DeviceFingerprint] = None


# ============================================================================
# 设备指纹生成
# ============================================================================

def _random_string(charset: str, length: int) -> str:
    """使用 secrets 模块生成安全随机字符串。"""
    return "".join(secrets.choice(charset) for _ in range(length))


def generate_fingerprint() -> DeviceFingerprint:
    """生成完整的 Fatalder 兼容设备指纹。

    所有字段互相独立, 每次调用返回不同值。

    Returns:
        DeviceFingerprint: 随机设备指纹。
    """
    # sdkuid: 16 位小写字母数字
    sdkuid = _random_string(_LOWER_ALNUM, 16)

    # udid: 32 位小写字母数字
    udid = _random_string(_LOWER_ALNUM, 32)

    # deviceid: 14 位小写字母 + "-" + 1 字母 + 6 hex
    name_part = _random_string(_LOWER_ALPHA, 14)
    suffix_letter = secrets.choice(_LOWER_ALPHA)
    suffix_hex = _random_string(_LOWER_HEX, 6)
    deviceid = f"{name_part}-{suffix_letter}{suffix_hex}"

    # client_login_sn: 32 位大写 HEX (16 字节)
    client_login_sn = _random_string(_UPPER_HEX, 32)

    return DeviceFingerprint(
        sdkuid=sdkuid,
        udid=udid,
        deviceid=deviceid,
        client_login_sn=client_login_sn,
    )


# ============================================================================
# sessionid 构建 (Fatalder 格式)
# ============================================================================

def build_sessionid(sdkuid: str, deviceid: str, session_index: str = "") -> str:
    """构建 Fatalder 格式的 sessionid。

    格式: "1-" + base64url(json({s, odsi, si, u, t, g_i}))

    其中:
      - s: 随机 32 字符会话字符串
      - odsi: deviceid (设备 ID)
      - si: sha1(s + odsi) 的十六进制表示
      - u: sdkuid
      - t: 固定值 2
      - g_i: 游戏内部 ID "aecfrxodyqaaajp"

    Args:
        sdkuid: SDK 用户 ID。
        deviceid: 设备 ID。
        session_index: 可选的会话索引 (默认随机生成)。

    Returns:
        sessionid 字符串 ("1-" + base64url 编码的 JSON)。
    """
    if not session_index:
        session_index = _random_string(_LOWER_ALNUM, 32)

    odsi = deviceid

    # si = sha1(session_index + odsi).hex()
    si = hashlib.sha1(
        (session_index + odsi).encode("utf-8")
    ).hexdigest()

    payload = {
        "s": session_index,
        "odsi": odsi,
        "si": si,
        "u": sdkuid,
        "t": 2,
        "g_i": _GAME_INTERNAL_ID,
    }

    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    return f"1-{encoded}"


# ============================================================================
# sauth_json 构建 (Fatalder/netease 频道格式)
# ============================================================================

def build_aim_info(
    aim_ip: str = DEFAULT_AIM_IP,
    country: str = DEFAULT_AIM_COUNTRY,
    tz: str = DEFAULT_AIM_TZ,
    tzid: str = DEFAULT_AIM_TZID,
) -> str:
    """构建 aim_info 字段 (JSON 编码的字符串)。

    Fatalder 样本: {"aim":"127.0.0.1","country":"CN","tz":"+0800","tzid":""}
    """
    v = {"aim": aim_ip, "country": country, "tz": tz, "tzid": tzid}
    return json.dumps(v, ensure_ascii=False)


def build_sauth_json(
    fp: DeviceFingerprint,
    sessionid: str = "",
    aim_info: str = "",
    gas_token: str = "",
    ip: str = "",
) -> str:
    """构建 Fatalder 兼容 sauth_json (netease 频道, 单层 JSON)。

    Args:
        fp: 设备指纹。
        sessionid: 会话 ID (默认自动生成)。
        aim_info: AIM 信息 (默认从指纹构建)。
        gas_token: GAS 令牌 (默认空)。
        ip: 客户端 IP (默认使用指纹中的 aim_ip)。

    Returns:
        sauth_json 内层 JSON 字符串 (未包装)。
    """
    if not sessionid:
        sessionid = build_sessionid(fp.sdkuid, fp.deviceid)
    if not aim_info:
        aim_info = build_aim_info(fp.aim_ip, fp.aim_country, fp.aim_tz, fp.aim_tzid)
    if not ip:
        ip = fp.aim_ip

    inner = {
        "gameid": fp.game_id,
        "login_channel": fp.login_channel,
        "app_channel": fp.app_channel,
        "platform": fp.platform,
        "sdkuid": fp.sdkuid,
        "sessionid": sessionid,
        "sdk_version": fp.sdk_version,
        "udid": fp.udid,
        "deviceid": fp.deviceid,
        "aim_info": aim_info,
        "client_login_sn": fp.client_login_sn,
        "gas_token": gas_token,
        "source_platform": fp.source_platform,
        "ip": ip,
    }
    return json.dumps(inner, ensure_ascii=False)


# ============================================================================
# Phoenix 认证客户端
# ============================================================================

class PhoenixGuestAuthClient:
    """Phoenix 访客认证客户端。

    使用随机设备指纹 + 第三方认证服务器获取游戏连接凭证,
    完全绕过 4399com 频道 (code=32 问题)。

    Usage:
        ::

            client = PhoenixGuestAuthClient(
                auth_server="https://fatalder.yeah114.top",
                api_key="your-api-key",
            )
            result = await client.login(
                server_code="123456",
                server_password="password",
            )
            if result.success:
                # result.chain_info 可用于 RakNet 连接
                # result.fb_token 是双层包装的 sauth_json
                pass
    """

    def __init__(
        self,
        auth_server: str = DEFAULT_AUTH_SERVER,
        api_key: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化 Phoenix 认证客户端。

        Args:
            auth_server: 认证服务器 URL (默认 fatalder.yeah114.top)。
            api_key: 可选的 API Key (部分服务器需要)。
            timeout: 请求超时时间 (秒)。
        """
        self.auth_server = auth_server.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = timeout

    def _apply_headers(self, headers: dict = None) -> dict:
        """构建通用请求头 (修复: 之前在请求发送后才设置 headers)。

        Args:
            headers: 已有的 headers 字典 (可选), 会合并进去。

        Returns:
            合并后的 headers 字典。
        """
        if headers is None:
            headers = {}
        headers["User-Agent"] = "FateArk/3.8 (Android; Minecraft 1.21.93)"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def fetch_secret(self) -> str:
        """从 /api/new 获取一次性 secret。

        Returns:
            secret 字符串。

        Raises:
            httpx.HTTPStatusError: 服务器返回错误状态码。
        """
        url = f"{self.auth_server}/api/new"
        headers = self._apply_headers()
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            resp = await client.get(url, headers=headers)

            if resp.status_code == 503:
                raise RuntimeError("认证服务器不可用 (503)")
            if resp.status_code != 200:
                body = resp.text[:200]
                raise RuntimeError(f"fetch_secret HTTP {resp.status_code}: {body}")

            return resp.text.strip()

    async def login(
        self,
        server_code: str = "",
        server_password: str = "",
        client_public_key: str = "",
        fingerprint: Optional[DeviceFingerprint] = None,
        custom_sauth_json: str = "",
    ) -> PhoenixAuthResult:
        """执行完整的 Phoenix 访客登录流程。

        1. 生成 (或使用传入的) 设备指纹
        2. 构建 sauth_json
        3. 获取 secret
        4. POST /api/phoenix/login

        Args:
            server_code: 服务器代码 (如 "123456")。
            server_password: 服务器密码。
            client_public_key: 客户端公钥 (用于 ECDH 加密)。
            fingerprint: 预生成的设备指纹 (默认随机生成)。
            custom_sauth_json: 自定义 sauth_json (优先于自动构建)。

        Returns:
            PhoenixAuthResult: 认证结果。
        """
        result = PhoenixAuthResult()

        # Step 1: 设备指纹
        fp = fingerprint or generate_fingerprint()
        result.fingerprint = fp

        # Step 2: 构建 sauth_json
        if custom_sauth_json:
            sauth_json_str = custom_sauth_json
        else:
            sauth_json_str = build_sauth_json(fp)
        logger.debug("[Phoenix] sauth_json (inner): %s", sauth_json_str)

        # Step 3: 双层包装
        outer_sauth = json.dumps(
            {"sauth_json": sauth_json_str}, ensure_ascii=False
        )

        # Step 4: 构建 POST payload
        payload = {
            "FBToken": outer_sauth,
            "UserName": "",
            "Password": "",
            "ServerCode": server_code,
            "ServerPassword": server_password,
            "ClientPublicKey": client_public_key,
        }
        body = json.dumps(payload, ensure_ascii=False)

        # Step 5: 获取 secret
        try:
            secret = await self.fetch_secret()
        except Exception as e:
            result.error = f"获取 secret 失败: {e}"
            logger.error("[Phoenix] %s", result.error)
            return result

        logger.debug(
            "[Phoenix] auth_server=%s api_key_set=%s secret_len=%d",
            self.auth_server, bool(self.api_key), len(secret),
        )

        # Step 6: POST /api/phoenix/login
        url = f"{self.auth_server}/api/phoenix/login"
        headers = self._apply_headers({
            "Content-Type": "application/json",
            "Authorization": f"Bearer {secret}",
        })

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, verify=False
            ) as client:
                resp = await client.post(url, content=body.encode("utf-8"), headers=headers)

                if resp.status_code == 503:
                    result.error = "认证服务器不可用 (503)"
                    return result

                if resp.status_code != 200:
                    result.error = self._parse_error(resp.status_code, resp.text)
                    return result

                try:
                    data = resp.json()
                except Exception:
                    result.error = f"非 JSON 响应: {resp.text[:200]}"
                    return result

                return self._parse_response(data, fp)

        except httpx.ConnectError as e:
            result.error = f"连接失败: {e}"
            return result
        except Exception as e:
            result.error = f"请求异常: {e}"
            return result

    def _parse_response(
        self, data: Dict[str, Any], fp: DeviceFingerprint
    ) -> PhoenixAuthResult:
        """解析认证服务器响应。"""
        result = PhoenixAuthResult(fingerprint=fp, raw=data)

        # 检查成功状态
        success = False
        for key in ("SuccessStates", "success_states", "success"):
            if key in data:
                val = data[key]
                if isinstance(val, bool) and val:
                    success = True
                    break

        if not success:
            # 提取错误信息
            info = ""
            for key in ("Message", "message"):
                if key in data:
                    val = data[key]
                    if isinstance(val, dict):
                        info = val.get("Information", "")
                    elif isinstance(val, str):
                        info = val
                    if info:
                        break

            if not info:
                info = str(data)

            # 检查封禁
            if self._looks_banned(info):
                result.error = f"账号被封禁: {info}"
            else:
                result.error = f"认证失败: {info}"
            return result

        # 提取成功字段
        result.success = True
        result.fb_token = str(data.get("FBToken", ""))
        result.chain_info = str(data.get("ChainInfo", ""))
        result.server_message = str(data.get("ServerMessage", ""))
        result.master_name = str(data.get("MasterName", ""))
        result.rental_server_ip = str(data.get("RentalServerIP", ""))

        if "BotLevel" in data:
            try:
                result.bot_level = int(data["BotLevel"])
            except (ValueError, TypeError):
                pass

        logger.info(
            "[Phoenix] 认证成功: master=%s, bot_level=%d, rental_ip=%s",
            result.master_name, result.bot_level, result.rental_server_ip,
        )
        return result

    def _parse_error(self, status: int, body: str) -> str:
        """解析错误响应。"""
        msg = body
        try:
            data = json.loads(body)
            for key in ("message", "Message", "error", "Error", "Information"):
                if key in data:
                    val = data[key]
                    if isinstance(val, str):
                        msg = val
                        break
                    elif isinstance(val, dict):
                        info = val.get("Information", "")
                        if info:
                            msg = info
                            break
        except json.JSONDecodeError:
            pass

        if self._looks_banned(msg):
            return f"账号被封禁: {msg}"
        return f"HTTP {status}: {msg[:200]}"

    @staticmethod
    def _looks_banned(msg: str) -> bool:
        """检查消息是否包含封禁关键词。"""
        lower = msg.lower()
        return any(pat in lower for pat in _BAN_PATTERNS)

    async def close(self) -> None:
        """清理资源 (兼容接口)。"""
        pass


# ============================================================================
# NetHard 验证服务器支持 (ToolDelta 兼容)
# ============================================================================

class NetHardAuthClient:
    """NetHard 验证服务器客户端 (nv1.nethard.pro)。

    NetHard 使用与 Fatalder 不同的 API 格式:
      - 密码使用 SHA256 哈希
      - 字段名不同 (client_public_key, server_code, server_passcode, username, password)
      - 返回 token 而非 ChainInfo

    逆向来源: tooldelta_antiban_code/core/auths.py (fblike_sign_login)
              tooldelta_antiban_code/constants/tooldelta_cli.py (FBLIKE_APIS)

    Usage:
        ::

            client = NetHardAuthClient()
            token = await client.login(
                username="your_account",
                password="your_password",
                server_code="123456",
                server_password="password",
            )
    """

    # NetHard API 端点 (来自 ToolDelta FBLIKE_APIS)
    _LOGIN_API = "%s/api/phoenix/login"
    _NEW_API = "%s/api/new"
    _MAIN_API = "%s"

    def __init__(
        self,
        auth_server: str = NETHARD_AUTH_SERVER,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.auth_server = auth_server.rstrip("/")
        self.timeout = timeout

    async def fetch_secret(self) -> str:
        """从 /api/new 获取一次性 auth_key。"""
        url = self._NEW_API % self.auth_server
        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"NetHard fetch_secret HTTP {resp.status_code}: {resp.text[:200]}"
                )
            return resp.text.strip()

    async def login(
        self,
        username: str,
        password: str,
        server_code: str = "::DRY::",
        server_password: str = "::DRY::",
    ) -> dict:
        """NetHard 登录流程。

        Args:
            username: 账号名。
            password: 明文密码 (内部会 SHA256 哈希)。
            server_code: 服务器代码 (默认 "::DRY::" 表示不指定)。
            server_password: 服务器密码。

        Returns:
            包含 token 和其他字段的 dict。

        Raises:
            RuntimeError: 登录失败。
        """
        # SHA256 哈希密码 (ToolDelta auths.py 兼容)
        password_hash = hashlib.sha256(password.encode()).hexdigest()

        # 获取 secret
        auth_key = await self.fetch_secret()

        # POST /api/phoenix/login
        url = self._LOGIN_API % self.auth_server
        payload = {
            "client_public_key": "",
            "server_code": server_code,
            "server_passcode": server_password,
            "username": username,
            "password": password_hash,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_key}",
        }

        async with httpx.AsyncClient(timeout=self.timeout, verify=False) as client:
            resp = await client.post(
                url,
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
            )

            if resp.status_code != 200:
                raise RuntimeError(
                    f"NetHard login HTTP {resp.status_code}: {resp.text[:200]}"
                )

            data = resp.json()
            if not data.get("success", False):
                msg = data.get("message", "unknown error")
                raise RuntimeError(f"NetHard login failed: {msg}")

            return data


# ============================================================================
# 便捷函数
# ============================================================================

async def phoenix_guest_login(
    server_code: str = "",
    server_password: str = "",
    auth_server: str = DEFAULT_AUTH_SERVER,
    api_key: str = "",
    client_public_key: str = "",
) -> PhoenixAuthResult:
    """一键 Phoenix 访客登录 (便捷函数)。

    Args:
        server_code: 服务器代码。
        server_password: 服务器密码。
        auth_server: 认证服务器 URL。
        api_key: API Key (可选)。
        client_public_key: 客户端公钥 (可选, 用于 ECDH 加密)。

    Returns:
        PhoenixAuthResult: 认证结果。
    """
    client = PhoenixGuestAuthClient(
        auth_server=auth_server,
        api_key=api_key,
    )
    try:
        return await client.login(
            server_code=server_code,
            server_password=server_password,
            client_public_key=client_public_key,
        )
    finally:
        await client.close()


__all__ = [
    # 客户端
    "PhoenixGuestAuthClient",
    "NetHardAuthClient",
    "PhoenixAuthResult",
    # 设备指纹
    "DeviceFingerprint",
    "generate_fingerprint",
    # 构建器
    "build_sessionid",
    "build_aim_info",
    "build_sauth_json",
    # 便捷函数
    "phoenix_guest_login",
    # 常量
    "DEFAULT_AUTH_SERVER",
    "FALLBACK_AUTH_SERVER",
    "NETHARD_AUTH_SERVER",
    "DEFAULT_SDK_VERSION",
]
