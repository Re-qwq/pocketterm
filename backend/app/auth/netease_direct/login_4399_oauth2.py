"""4399 OAuth2 认证流程 (WPFLauncher_Hook 方案 + netease 频道转换)。

逆向来源:
  - WPFLauncher_Hook/Mcl.Core/Dotnetdetour/Tools/4399Login.cs
  - 方法: _4399.LoginAsync(username, password)

流程:
  1. 获取验证码 → ptlogin.4399.com/ptlogin/captcha.do
  2. 获取 OAuth 参数 → m.4399api.com/openapi/oauth-callback.html
  3. 登录并授权 → ptlogin.4399.com/oauth2/loginAndAuthorize.do (不跟随重定向)
  4. OAuth 回调 → GET Location URL, 获取 uid 和 state
  5. 转换为 netease 频道 (解决 code=32 问题):
     a. 优先: MPay fever_to_sauth 转换 (create_ticket + login/ticket)
     b. 回退: 本地构建 netease 频道 sauth_json (Fatalder 格式 sessionid)
  6. 提交 uni_sauth 验证 (netease 频道)

关键发现:
  - 4399com 频道已被网易废弃, uni_sauth 返回 code=32
  - 转换为 netease 频道后可正常通过 uni_sauth 和 login-otp 验证
  - 密码明文提交 (sec=0)
  - 验证码必须 (每次随机生成)
  - captchaId 格式: UUID4 去连字符转大写 (32位hex)
  - 登录成功标志: HTTP 302 + 空 body + Location header
  - state 格式: uid|access_token|gamekey||state|hash|expire|channel
"""

from __future__ import annotations

import json
import logging
import random
import re
import secrets
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

logger = logging.getLogger("pocketterm.login_4399_oauth2")

# ============================================================================
# 常量 (来自 4399Login.cs)
# ============================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36"
)

OAUTH2_BASE_URL = "https://ptlogin.4399.com/oauth2/"
REDIRECT_URI = (
    "https://m.4399api.com/openapi/oauth-callback.html"
    "?gamekey=44770&game_key=115716"
)
SDK_VERSION = "3.12.2.503"
# NovaBuilder 1.3.4 (SW 面板抓包验证) 使用 "1.0.0" 作为 4399com 频道的 sdk_version
# 与 Community-Bot 的 netease 频道 (3.9.0) 不同
SAUTH_SDK_VERSION = "1.0.0"

CAPTCHA_URL = "https://ptlogin.4399.com/ptlogin/captcha.do"
OAUTH_CALLBACK_URL = (
    "https://m.4399api.com/openapi/oauth-callback.html"
    "?gamekey=44770&game_key=115716"
)
LOGIN_AND_AUTHORIZE_URL = OAUTH2_BASE_URL + "loginAndAuthorize.do"
UNI_SAUTH_URL = "https://mgbsdk.matrix.netease.com/x19/sdk/uni_sauth"
LOGIN_OTP_URL = "https://x19obtcore.nie.netease.com:8443/login-otp"

GAME_ID = "x19"
CHANNEL = "4399com"
PLATFORM = "ad"

# Netease 频道常量 (替代已废弃的 4399com 频道, 解决 code=32 问题)
# 4399com/4399pc 频道已被网易废弃, uni_sauth 返回 code=32
# 改用 netease 频道 (与 Community-Bot / Phoenix 访客认证一致)
NETEASE_CHANNEL = "netease"
NETEASE_PLATFORM = "pc"
NETEASE_SDK_VERSION = "3.9.0"  # 匹配 Community-Bot 真实样本

# WPFLauncher User-Agent (用于网易端点请求, 不带 4399 Referer/Origin)
NETEASE_UA = "WPFLauncher/0.0.0.0"

_HEADERS = {
    "User-Agent": USER_AGENT,
}

# OCR 最大尝试次数
OCR_MAX_ATTEMPTS = 10

_HEX_CHARS = "0123456789ABCDEF"
_ALPHA_LOWER = "abcdefghijklmnopqrstuvwxyz"
_ALPHA_LOWER_DIGITS = "abcdefghijklmnopqrstuvwxyz0123456789"


# ============================================================================
# 数据类
# ============================================================================

@dataclass
class OAuth2Result:
    """OAuth2 认证结果。"""
    uid: str
    token: str           # access_token (从 state 中提取)
    sessionid: str       # 完整 state 字符串
    sauth_json: dict
    raw_cookie: dict
    verified: bool = False       # uni_sauth + login-otp 是否验证通过
    verification_code: int = -1 # 验证端点返回的 code (0=成功)


# ============================================================================
# 工具函数
# ============================================================================

def generate_captcha_id() -> str:
    """生成 32 位大写 hex captchaId (UUID4 去连字符转大写)。

    与 WPFLauncher_Hook 的 Guid.NewGuid().ToString().Replace("-", "").ToUpper() 一致。
    """
    return uuid.uuid4().hex.upper()


def _random_hex(length: int = 32) -> str:
    """生成随机大写 HEX 字符串 (用于 deviceid/client_login_sn)。"""
    return "".join(secrets.choice(_HEX_CHARS) for _ in range(length))


def _random_hex_lower(length: int = 32) -> str:
    """生成随机小写 HEX 字符串 (NovaBuilder 格式)。"""
    return "".join(secrets.choice("0123456789abcdef") for _ in range(length))


def _random_udid() -> str:
    """生成 16 位随机字母数字 udid。"""
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=16))


def generate_device_fingerprint() -> dict:
    """生成随机设备指纹 (NovaBuilder 兼容格式)。

    根据 SW 面板抓包数据 (fa.pioneershop.pw) 中 NovaBuilder 1.3.4 的
    cookie.json 真实样本:
        - client_login_sn 和 deviceid 使用相同的值
        - 格式为 32 位小写 HEX (如 7c3689151fdf402f9b47b6f2af9efdb7)
        - udid 为 16 位小写 HEX (如 0947b560c348e447)

    Returns:
        dict: {client_login_sn, deviceid, udid}
    """
    sn = _random_hex_lower(32)
    return {
        "client_login_sn": sn,
        "deviceid": sn,
        "udid": _random_hex_lower(16),
    }


async def fetch_captcha_image(captcha_id: str) -> bytes:
    """下载验证码图片。

    Args:
        captcha_id: 验证码 ID (32位大写hex)

    Returns:
        验证码图片二进制数据 (PNG)
    """
    url = f"{CAPTCHA_URL}?captchaId={captcha_id}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_HEADERS)
        return resp.content


async def _get_oauth_params() -> dict:
    """获取 OAuth 参数。

    GET https://m.4399api.com/openapi/oauth-callback.html?gamekey=44770&game_key=115716
    返回 JSON, result 字段是 OAuth URL, 解析其 query string 获取参数。

    Returns:
        dict: {client_id, state, redirect_uri, _d, bizId, ref}
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(OAUTH_CALLBACK_URL, headers=_HEADERS)
        data = resp.json()
        oauth_url = data.get("result", "")
        if not oauth_url:
            raise Exception(f"获取 OAuth URL 失败: {data}")

        parsed = urlparse(oauth_url)
        qs = parse_qs(parsed.query)
        return {
            "client_id": qs.get("client_id", [""])[0],
            "state": qs.get("state", [""])[0],
            "redirect_uri": qs.get("redirect_uri", [""])[0],
            "_d": qs.get("_d", [""])[0],
            "bizId": qs.get("bizId", [""])[0],
            "ref": qs.get("ref", [""])[0],
        }


def _build_login_form(
    username: str,
    password: str,
    captcha: str,
    captcha_id: str,
    oauth_params: dict,
) -> dict:
    """构建登录表单 (密码明文, sec=0)。

    与 WPFLauncher_Hook 的 BuildLoginForm 一致。
    注意: bizId 和 redirect_uri 是硬编码常量, 不从 OAuth 参数取。
    """
    return {
        "auth_action": "ORILOGIN",
        "bizId": "2100001792",
        "captcha": captcha or "",
        "captcha_id": captcha_id or "",
        "client_id": oauth_params["client_id"],
        "isInputRealname": "false",
        "isVaildRealname": "false",
        "password": password,
        "redirect_uri": REDIRECT_URI,
        "ref": oauth_params["ref"],
        "response_type": "TOKEN",
        "scope": "basic",
        "sec": "0",
        "state": oauth_params["state"],
        "username": username,
    }


async def _login_and_authorize(
    username: str,
    password: str,
    captcha: str,
    captcha_id: str,
    oauth_params: dict,
) -> dict:
    """POST loginAndAuthorize (不跟随重定向)。

    成功: HTTP 302, 空 body, Location header 存在
    失败: HTTP 200, body 包含错误信息
    限流: HTTP 202, body="请稍后再试~"

    Returns:
        dict: {success, location, body, error, retryable}
    """
    form = _build_login_form(username, password, captcha, captcha_id, oauth_params)
    login_url = f"{LOGIN_AND_AUTHORIZE_URL}?channel=&sdk=op&sdk_version={SDK_VERSION}"

    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        resp = await client.post(login_url, data=form, headers=_HEADERS)
        body = resp.text

        # HTTP 202 = 限流
        if resp.status_code == 202:
            return {
                "success": False,
                "error": "请求过于频繁，请稍后再试",
                "body": body,
                "retryable": True,
                "rate_limited": True,
            }

        if not body:
            # 空 body = 登录成功
            location = resp.headers.get("location", "")
            if location:
                return {"success": True, "location": location, "body": "", "retryable": False}
            return {
                "success": False,
                "error": "登录成功但无 Location header",
                "body": "",
                "retryable": False,
            }

        # 有 body = 错误
        if "验证码错误" in body:
            return {
                "success": False,
                "error": "验证码错误",
                "body": body,
                "retryable": True,
            }

        # 提取错误信息
        m = re.search(r'id="login_err_msg"\s*>\s*([^<]*)\s*<', body)
        if m:
            err_msg = m.group(1).strip()
            if err_msg:
                # 有具体错误信息 (如 "密码错误")
                return {
                    "success": False,
                    "error": err_msg,
                    "body": body,
                    "retryable": False,
                }
            else:
                # 空 login_err_msg: 验证码可能过期或 OAuth state 不匹配
                return {
                    "success": False,
                    "error": "验证码已过期",
                    "body": body,
                    "retryable": True,
                }

        return {
            "success": False,
            "error": "未知登录错误",
            "body": body[:500],
            "retryable": False,
        }


async def _get_oauth_callback(location_url: str) -> dict:
    """GET OAuth 回调, 获取 uid 和 state。

    Returns:
        dict: {uid, state}
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(location_url, headers=_HEADERS)
        data = resp.json()
        result = data.get("result", {})
        return {
            "uid": str(result.get("uid", "")),
            "state": result.get("state", ""),
        }


def build_sauth_json(
    uid: str,
    state: str,
    username: str = "",
    device_fp: Optional[dict] = None,
) -> dict:
    """构建 sauth_json (4399com 频道)。

    与 WPFLauncher_Hook 的 BuildSauthJson 一致,但使用随机设备指纹防封。

    优化点 (参考 NovaBuilder 1.3.4 SW 面板抓包数据):
        - client_login_sn/deviceid 使用相同的随机 32 位小写 HEX (不再硬编码)
        - realname 使用 realname_type:"0" (字符串, 匹配 NovaBuilder 实际格式)
        - 新增 get_access_token/is_unisdk_guest/source_app_channel 字段
        - aim_info 包含 celluar_ip/operator/is_vpn_enabled 扩展字段
        - tzid 设为 "Asia/Shanghai" (匹配 NovaBuilder)
        - udid 为 16 位小写 HEX (匹配 NovaBuilder)

    NovaBuilder 真实样本 (2026-07-21 SW 面板抓包):
        sdk_version: "1.0.0"
        realname: {"realname_type": "0"}
        udid: "0947b560c348e447" (16 位小写 hex)
        client_login_sn == deviceid: "7c3689151fdf402f9b47b6f2af9efdb7"
        source_app_channel: "4399com"

    Args:
        uid: 4399 用户 UID。
        state: OAuth2 state 字符串 (uid|access_token|...)。
        username: 4399 用户名 (用于 userid 字段)。
        device_fp: 设备指纹 (可选, 默认随机生成)。

    Returns:
        sauth_json 字典。
    """
    fp = device_fp or generate_device_fingerprint()
    aim_info = json.dumps({
        "aim": "127.0.0.1",
        "country": "CN",
        "tz": "+0800",
        "tzid": "Asia/Shanghai",
        "celluar_ip": "",
        "operator": "",
        "is_vpn_enabled": False,
    }, ensure_ascii=False)
    return {
        "aim_info": aim_info,
        "app_channel": CHANNEL,
        "platform": PLATFORM,
        "client_login_sn": fp["client_login_sn"],
        "deviceid": fp["deviceid"],
        "gameid": GAME_ID,
        "gas_token": "",
        "get_access_token": "1",
        "ip": "127.0.0.1",
        "is_unisdk_guest": 0,
        "login_channel": CHANNEL,
        "realname": '{"realname_type":"0"}',
        "sdk_version": SAUTH_SDK_VERSION,
        "sdkuid": uid,
        "sessionid": state,
        "source_app_channel": CHANNEL,
        "source_platform": PLATFORM,
        "udid": fp["udid"],
    }


def build_sauth_json_netease(
    uid: str,
    username: str = "",
    device_fp: Optional[dict] = None,
) -> dict:
    """构建 netease 频道 sauth_json (绕过 code=32 问题)。

    4399com/4399pc 频道已被网易废弃, uni_sauth 返回 code=32。
    本函数使用 netease 频道构建 sauth_json, 与 Community-Bot 真实样本格式一致。

    关键差异 (vs build_sauth_json):
        - app_channel/login_channel: "netease" (非 "4399com")
        - platform: "pc" (非 "ad")
        - sdk_version: "3.9.0" (非 "1.0.0")
        - sessionid: Fatalder 格式 (非 4399 OAuth state)
        - 不包含 realname/source_app_channel (netease 频道不需要)

    Args:
        uid: 4399 用户 UID (从 OAuth2 回调获取)。
        username: 4399 用户名 (可选)。
        device_fp: 设备指纹 (可选, 默认随机生成)。

    Returns:
        sauth_json 字典 (netease 频道)。
    """
    fp = device_fp or generate_device_fingerprint()
    # 使用 Fatalder 格式 sessionid (与 Phoenix 访客认证一致)
    # 不使用 4399 OAuth state, 因为它绑定 4399com 频道
    from app.auth.sauth_builder import build_sessionid
    sessionid = build_sessionid(uid, fp["deviceid"])

    aim_info = json.dumps({
        "aim": "127.0.0.1",
        "country": "CN",
        "tz": "+0800",
        "tzid": "Asia/Shanghai",
    }, ensure_ascii=False)

    return {
        "aim_info": aim_info,
        "app_channel": NETEASE_CHANNEL,
        "platform": NETEASE_PLATFORM,
        "client_login_sn": fp["client_login_sn"],
        "deviceid": fp["deviceid"],
        "gameid": GAME_ID,
        "gas_token": "",
        "ip": "127.0.0.1",
        "login_channel": NETEASE_CHANNEL,
        "sdk_version": NETEASE_SDK_VERSION,
        "sdkuid": uid,
        "sessionid": sessionid,
        "source_platform": NETEASE_PLATFORM,
        "udid": fp["udid"],
    }


async def convert_to_netease_channel(
    uid: str,
    access_token: str,
    device_fp: Optional[dict] = None,
) -> dict:
    """将 4399 OAuth2 凭证转换为 netease 频道 sauth_json。

    优先尝试 MPay fever_to_sauth 转换 (通过 create_ticket + login/ticket
    获取新的 netease sessionid), 失败则回退到本地构建 netease sauth_json。

    Args:
        uid: 4399 用户 UID。
        access_token: 4399 OAuth2 access_token (从 state 中提取)。
        device_fp: 设备指纹 (可选)。

    Returns:
        dict: {"success": bool, "sauth_json": dict, "method": str, "message": str}
    """
    fp = device_fp or generate_device_fingerprint()
    deviceid = fp["deviceid"]

    # 方案 1: 尝试 MPay fever_to_sauth 转换
    try:
        from .fever_to_sauth import fever_to_sauth
        result = await fever_to_sauth(
            sdkuid=uid,
            sessionid=access_token,
            deviceid=deviceid,
            use_random_udid=True,
        )
        if result.get("success") and result.get("sauth_json"):
            # 解析返回的 sauth_json
            sauth_data = json.loads(result["sauth_json"])
            inner = sauth_data.get("sauth_json", "")
            if inner:
                inner_dict = json.loads(inner)
                logger.info(
                    f"fever_to_sauth 转换成功 (netease 频道, uid={uid})"
                )
                return {
                    "success": True,
                    "sauth_json": inner_dict,
                    "method": "fever_to_sauth",
                    "message": "MPay 转换成功",
                }
    except Exception as e:
        logger.warning(f"fever_to_sauth 转换失败: {e}, 尝试本地构建")

    # 方案 2: 本地构建 netease 频道 sauth_json
    sauth_dict = build_sauth_json_netease(uid, device_fp=fp)
    logger.info(f"本地构建 netease 频道 sauth_json (uid={uid})")
    return {
        "success": True,
        "sauth_json": sauth_dict,
        "method": "local_build",
        "message": "本地构建 netease 频道 sauth_json",
    }


async def _post_uni_sauth(sauth_json_str: str) -> dict:
    """POST sauth_json 到 uni_sauth (网易统一认证)。

    关键修复 (参考 sauth_refresh.py):
        - 使用 WPFLauncher User-Agent (不带 4399 Referer/Origin)
        - 使用独立的 HTTP client (不携带 4399 session cookies)
        - 带 4399 Referer/Origin 会导致 uni_sauth 返回 502

    Args:
        sauth_json_str: sauth_json 内层 JSON 字符串。

    Returns:
        uni_sauth 响应 dict (包含 code 字段, 0=成功)。
    """
    _netease_headers = {
        "User-Agent": NETEASE_UA,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=20, verify=False, headers=_netease_headers
    ) as client:
        resp = await client.post(
            UNI_SAUTH_URL,
            content=sauth_json_str.encode("utf-8"),
        )
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text, "code": -1, "status_code": resp.status_code}


async def _post_login_otp(sauth_wrapped_str: str) -> dict:
    """POST 包装后的 sauth_json 到 login-otp (最终验证)。

    与 sauth_refresh.py 的 MPay 流程 Step 8 一致。

    Args:
        sauth_wrapped_str: 包装后的 JSON 字符串 ({"sauth_json": "..."})

    Returns:
        login-otp 响应 dict (包含 code 字段, 0=成功)。
    """
    _netease_headers = {
        "User-Agent": NETEASE_UA,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(
        timeout=20, verify=False, headers=_netease_headers
    ) as client:
        resp = await client.post(
            LOGIN_OTP_URL,
            content=sauth_wrapped_str.encode("utf-8"),
        )
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text, "code": -1, "status_code": resp.status_code}


# ============================================================================
# OCR 支持
# ============================================================================

_ocr_instance = None


def _get_ocr():
    """获取 ddddocr 实例 (惰性初始化)。"""
    global _ocr_instance
    if _ocr_instance is None:
        try:
            import ddddocr
            _ocr_instance = ddddocr.DdddOcr(show_ad=False)
        except ImportError:
            _ocr_instance = False  # 标记不可用
    return _ocr_instance if _ocr_instance is not False else None


async def ocr_captcha(image: bytes) -> str:
    """OCR 识别验证码。

    Args:
        image: 验证码图片二进制数据

    Returns:
        识别结果字符串 (可能为空)
    """
    ocr = _get_ocr()
    if ocr is None:
        return ""
    result = ocr.classification(image)
    return (result or "").strip()


# ============================================================================
# 认证客户端
# ============================================================================

class Login4399OAuth2:
    """4399 OAuth2 认证客户端 (WPFLauncher_Hook 方案)。

    使用示例:
        client = Login4399OAuth2()
        # 获取验证码
        captcha = await client.get_captcha()
        # 登录
        result = await client.login("user", "pass", "abcd", captcha["id"])
    """

    def __init__(self):
        self._client = httpx.AsyncClient(
            headers=_HEADERS,
            timeout=30.0,
        )

    async def close(self):
        await self._client.aclose()

    async def get_captcha(self) -> dict:
        """获取验证码图片和 ID。

        Returns:
            dict: {id: captcha_id, image: bytes}
        """
        captcha_id = generate_captcha_id()
        image = await fetch_captcha_image(captcha_id)
        return {"id": captcha_id, "image": image}

    async def login(
        self,
        username: str,
        password: str,
        captcha_answer: str = "",
        captcha_id: str = "",
    ) -> Optional[OAuth2Result]:
        """完整的 4399 OAuth2 登录流程。

        Args:
            username: 4399 用户名
            password: 4399 密码
            captcha_answer: 验证码答案 (如果为空, 尝试自动 OCR)
            captcha_id: 验证码 ID (如果为空, 自动生成)

        Returns:
            OAuth2Result 或 None (登录失败)
        """
        import asyncio as _asyncio

        # 如果没有 captcha_id, 自动生成并尝试 OCR
        use_auto_ocr = not captcha_answer
        if not captcha_id:
            captcha_id = generate_captcha_id()

        ocr_attempts = 0
        max_attempts = OCR_MAX_ATTEMPTS if use_auto_ocr else 1

        while ocr_attempts < max_attempts:
            # 确定 captcha 答案
            if use_auto_ocr:
                image = await fetch_captcha_image(captcha_id)
                captcha_answer = await ocr_captcha(image)
                if not captcha_answer or len(captcha_answer) < 4:
                    ocr_attempts += 1
                    captcha_id = generate_captcha_id()
                    continue
            elif not captcha_answer:
                return None

            # 获取 OAuth 参数
            oauth_params = await _get_oauth_params()
            if not oauth_params["client_id"]:
                return None

            # 登录
            result = await _login_and_authorize(
                username, password, captcha_answer, captcha_id, oauth_params
            )

            if result["success"]:
                # 登录成功, GET OAuth 回调
                oauth_data = await _get_oauth_callback(result["location"])
                uid = oauth_data["uid"]
                state = oauth_data["state"]

                if not uid:
                    return None

                # 提取 access_token (从 state 中解析)
                # state 格式: uid|access_token|gamekey||state|hash|expire|channel
                token = ""
                if state and "|" in state:
                    parts = state.split("|")
                    if len(parts) > 1:
                        token = parts[1]

                # 生成设备指纹 (用于两种频道的 sauth_json)
                device_fp = generate_device_fingerprint()

                # === 核心修复: 转换为 netease 频道 (解决 code=32) ===
                # 4399com 频道已被网易废弃, uni_sauth 返回 code=32
                # 优先尝试 MPay fever_to_sauth 转换, 失败则本地构建 netease sauth_json
                netease_result = await convert_to_netease_channel(
                    uid, token, device_fp=device_fp
                )
                sauth_json = netease_result["sauth_json"]
                conversion_method = netease_result["method"]

                logger.info(
                    f"认证频道转换完成: {conversion_method} "
                    f"(uid={uid}, channel=netease)"
                )

                # Step 6: uni_sauth 验证 (netease 频道)
                verified = False
                verification_code = -1
                try:
                    sauth_str = json.dumps(sauth_json, ensure_ascii=False)
                    uni_resp = await _post_uni_sauth(sauth_str)
                    verification_code = uni_resp.get("code", -1)

                    if verification_code == 0:
                        logger.info(
                            f"uni_sauth 验证成功 (netease, uid={uid}, "
                            f"method={conversion_method})"
                        )
                        # Step 7: login-otp 最终验证
                        sauth_wrapped = json.dumps(
                            {"sauth_json": sauth_str}, ensure_ascii=False
                        )
                        otp_resp = await _post_login_otp(sauth_wrapped)
                        otp_code = otp_resp.get("code", -1)

                        if otp_code == 0:
                            logger.info(
                                f"login-otp 验证成功 (netease, uid={uid})"
                            )
                            verified = True
                        else:
                            logger.warning(
                                f"login-otp 验证失败: code={otp_code}, "
                                f"resp={str(otp_resp)[:200]}"
                            )
                    elif verification_code == 32:
                        logger.warning(
                            f"uni_sauth 返回 code=32 (netease 频道也可能受限), "
                            f"uid={uid}"
                        )
                    else:
                        logger.warning(
                            f"uni_sauth 验证失败: code={verification_code}, "
                            f"msg={uni_resp.get('message', '')}, "
                            f"uid={uid}"
                        )
                except Exception as e:
                    logger.warning(f"uni_sauth/login-otp 异常: {e}, uid={uid}")

                return OAuth2Result(
                    uid=uid,
                    token=token,
                    sessionid=state,
                    sauth_json=sauth_json,
                    raw_cookie={
                        "uid": uid,
                        "state": state,
                        "username": username,
                        "channel": "netease",
                        "conversion_method": conversion_method,
                    },
                    verified=verified,
                    verification_code=verification_code,
                )

            # 错误处理
            error = result.get("error", "")
            retryable = result.get("retryable", False)
            rate_limited = result.get("rate_limited", False)

            if rate_limited:
                # 限流: 等待 3 秒后重试
                await _asyncio.sleep(3)
                captcha_id = generate_captcha_id()
                captcha_answer = ""
                continue

            if retryable and use_auto_ocr:
                # 可重试错误 (验证码错误/过期): 自动重试
                ocr_attempts += 1
                captcha_id = generate_captcha_id()
                captcha_answer = ""
                continue
            elif retryable and not use_auto_ocr:
                # 手动模式下的可重试错误: 返回 None, 让前端刷新验证码
                return None

            # 不可重试错误 (如密码错误): 直接返回
            return None

        return None


# ============================================================================
# 便捷函数
# ============================================================================

async def login_4399_oauth2(
    username: str,
    password: str,
    captcha_answer: Optional[str] = None,
    captcha_id: Optional[str] = None,
) -> Optional[OAuth2Result]:
    """便捷的 4399 OAuth2 登录函数。

    Args:
        username: 4399 用户名
        password: 4399 密码
        captcha_answer: 验证码答案 (如果为空, 尝试自动 OCR)
        captcha_id: 验证码 ID (如果为空, 自动生成)

    Returns:
        OAuth2Result 或 None (登录失败)
    """
    client = Login4399OAuth2()
    try:
        return await client.login(
            username,
            password,
            captcha_answer or "",
            captcha_id or "",
        )
    finally:
        await client.close()
