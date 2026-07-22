"""4399 OAuth2 认证流程 (WPFLauncher_Hook 方案)。

逆向来源:
  - WPFLauncher_Hook/Mcl.Core/Dotnetdetour/Tools/4399Login.cs
  - 方法: _4399.LoginAsync(username, password)

流程:
  1. 获取验证码 → ptlogin.4399.com/ptlogin/captcha.do
  2. 获取 OAuth 参数 → m.4399api.com/openapi/oauth-callback.html
  3. 登录并授权 → ptlogin.4399.com/oauth2/loginAndAuthorize.do (不跟随重定向)
  4. OAuth 回调 → GET Location URL, 获取 uid 和 state
  5. 构建 sauth_json (4399com 频道)
  6. 提交 uni_sauth (可选验证)

关键发现:
  - 此流程完全绕过失效的 checkKidLoginUserCookie.do 和 sdk/info 端点
  - 密码明文提交 (sec=0)
  - 验证码必须 (每次随机生成)
  - captchaId 格式: UUID4 去连字符转大写 (32位hex)
  - 登录成功标志: HTTP 302 + 空 body + Location header
  - state 格式: uid|access_token|gamekey||state|hash|expire|channel
"""

from __future__ import annotations

import json
import random
import re
import string
import uuid
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

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
SAUTH_SDK_VERSION = "3.12.2"

CAPTCHA_URL = "https://ptlogin.4399.com/ptlogin/captcha.do"
OAUTH_CALLBACK_URL = (
    "https://m.4399api.com/openapi/oauth-callback.html"
    "?gamekey=44770&game_key=115716"
)
LOGIN_AND_AUTHORIZE_URL = OAUTH2_BASE_URL + "loginAndAuthorize.do"
UNI_SAUTH_URL = "https://mgbsdk.matrix.netease.com/x19/sdk/uni_sauth"

GAME_ID = "x19"
CHANNEL = "4399com"
PLATFORM = "ad"

_HEADERS = {
    "User-Agent": USER_AGENT,
}

# OCR 最大尝试次数
OCR_MAX_ATTEMPTS = 10


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


# ============================================================================
# 工具函数
# ============================================================================

def generate_captcha_id() -> str:
    """生成 32 位大写 hex captchaId (UUID4 去连字符转大写)。

    与 WPFLauncher_Hook 的 Guid.NewGuid().ToString().Replace("-", "").ToUpper() 一致。
    """
    return uuid.uuid4().hex.upper()


def _random_udid() -> str:
    """生成 16 位随机字母数字 udid。"""
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=16))


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


def build_sauth_json(uid: str, state: str) -> dict:
    """构建 sauth_json (4399com 频道)。

    与 WPFLauncher_Hook 的 BuildSauthJson 一致。
    """
    return {
        "aim_info": '{"aim":"127.0.0.1","country":"CN","tz":"+0800","tzid":""}',
        "realname": '{"realname_type":2}',
        "app_channel": CHANNEL,
        "platform": PLATFORM,
        "client_login_sn": "4399FuckYou",
        "gameid": GAME_ID,
        "login_channel": CHANNEL,
        "sdk_version": SAUTH_SDK_VERSION,
        "sdkuid": uid,
        "sessionid": state,
        "udid": _random_udid(),
        "deviceid": "4399FuckYou",
    }


async def _post_uni_sauth(sauth_json_str: str) -> dict:
    """POST sauth_json 到 uni_sauth (可选验证)。

    Returns:
        dict: uni_sauth 响应
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            UNI_SAUTH_URL,
            content=sauth_json_str,
            headers={**_HEADERS, "Content-Type": "application/json"},
        )
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}


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

                # 构建 sauth_json
                sauth_json = build_sauth_json(uid, state)

                # 提取 access_token (从 state 中解析)
                token = ""
                if state and "|" in state:
                    parts = state.split("|")
                    if len(parts) > 1:
                        token = parts[1]

                # 可选: POST uni_sauth (不阻塞, 仅验证)
                try:
                    sauth_str = json.dumps(sauth_json, ensure_ascii=False)
                    await _post_uni_sauth(sauth_str)
                except Exception:
                    pass  # uni_sauth 失败不影响登录结果

                return OAuth2Result(
                    uid=uid,
                    token=token,
                    sessionid=state,
                    sauth_json=sauth_json,
                    raw_cookie={
                        "uid": uid,
                        "state": state,
                        "username": username,
                    },
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
