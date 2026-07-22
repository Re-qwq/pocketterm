"""4399 账号登录流程。

逆向来源:
  - CYXHSJ 永久Cookies获取.exe (PyInstaller Python 3.13)
  - Drug.NetEase/Drug.NetEase.Client/BaseAuth/x19Auth.cs (Pt4399Login 方法)

流程:
  1. 验证用户名 → ptlogin.4399.com/ptlogin/verify.do
  2. 获取验证码 → ptlogin.4399.com/ptlogin/captcha.do
  3. 登录 → ptlogin.4399.com/ptlogin/login.do
  4. 获取 SDK 信息 → microgame.5054399.net/v2/service/sdk/info
  5. 构建 sauth_json

注意:
  - 生成的 cookie 为 4399pc 频道 (login_channel=4399pc)
  - 4399pc 频道目前可能已失效 (返回 code=32)
  - 可结合 fever_to_sauth 转换为 netease 频道
"""

from __future__ import annotations

import json
import random
import re
import string
import time
import uuid

import httpx

VERIFY_URL = "https://ptlogin.4399.com/ptlogin/verify.do"
CAPTCHA_URL = "https://ptlogin.4399.com/ptlogin/captcha.do"
LOGIN_URL = "https://ptlogin.4399.com/ptlogin/login.do?v=1"
CHECK_COOKIE_URL = "https://ptlogin.4399.com/ptlogin/checkKidLoginUserCookie.do"
SDK_INFO_URL = "https://microgame.5054399.net/v2/service/sdk/info"

CAPTCHA_ID_PREFIX = "captchaReq"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
    "Referer": "https://ptlogin.4399.com/",
}


def uuid4_hex() -> str:
    """生成 16 进制 UUID4。"""
    return uuid.uuid4().hex


def _generate_deviceid() -> str:
    """生成设备 ID (格式: amaw + 12位小写字母数字 + -d)。

    与 CYXHSJ.exe 生成的格式一致, 例如 amawufyaaxtu3ufq-d。
    """
    chars = string.ascii_lowercase + string.digits
    return f"amaw{''.join(random.choices(chars, k=12))}-d"

# SDK 查询参数模板
_SDK_QUERY = (
    "game_id={game_id}&platform=pc&is_sub_account=0&"
    "sig={sig}&uid={uid}&time={time}&validateState={validateState}&"
    "username={username}&client_id=&sdk_version=1.0.0"
)


def generate_captcha_id() -> str:
    """生成验证码 session ID。"""
    return CAPTCHA_ID_PREFIX + "".join(random.choices(string.digits, k=8))


async def fetch_captcha(captcha_id: str) -> bytes:
    """获取验证码图片。

    Args:
        captcha_id: 验证码 session ID

    Returns:
        验证码图片二进制数据 (PNG)
    """
    url = f"{CAPTCHA_URL}?xx=1&captchaId={captcha_id}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_HEADERS)
        return resp.content


async def verify_username(username: str) -> dict:
    """验证 4399 用户名是否需要验证码。

    Args:
        username: 4399 用户名

    Returns:
        dict: {"need_captcha": bool, "captcha_id": str}
    """
    ts = int(time.time() * 1000)
    url = f"{VERIFY_URL}?username={username}&appId=kid_wdsj&t={ts}&inputWidth=iptw2&v=1"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url, headers=_HEADERS)
        text = resp.text

    # 检查是否需要验证码
    need_captcha = "captchaId" in text or '"code":0' not in text
    # 从响应中提取 captchaId (与服务器会话匹配)
    captcha_id = generate_captcha_id()
    if need_captcha:
        import re as _re
        m = _re.search(r'"captchaId"\s*[:=]\s*["\']?([A-Za-z0-9]+)', text)
        if m:
            captcha_id = m.group(1)

    return {"need_captcha": need_captcha, "captcha_id": captcha_id}


async def login_4399(
    username: str,
    password: str,
    *,
    captcha_id: str = "",
    captcha_answer: str = "",
) -> dict:
    """登录 4399 并获取 SDK token。

    Args:
        username: 4399 用户名
        password: 4399 密码
        captcha_id: 验证码 session ID (如果需要验证码)
        captcha_answer: 验证码答案 (如果需要验证码)

    Returns:
        dict: {"success": bool, "data": dict, "message": str}
        data 包含: sig, uid, time, validateState, sdk_login_data, token, sdkuid
    """
    login_data = {
        "loginFrom": "uframe",
        "postLoginHandler": "default",
        "layoutSelfAdapting": "true",
        "externalLogin": "qq",
        "displayMode": "popup",
        "layout": "vertical",
        "bizId": "2100001792",
        "appId": "kid_wdsj",
        "gameId": "wd",
        "css": "http://microgame.5054399.net/v2/resource/cssSdk/default/login.css",
        "redirectUrl": "",
        "sessionId": captcha_id,
        "mainDivId": "popup_login_div",
        "includeFcmInfo": "false",
        "level": "8",
        "regLevel": "8",
        "userNameLabel": "4399用户名",
        "username": username,
        "password": password,
        "welcomeTip": "欢迎回到4399",
        "sec": "1",
        "inputCaptcha": captcha_answer,
        "reg_eula_agree": "on",
    }

    result = {"success": False, "data": {}, "message": ""}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            # Step 1: 登录
            resp = await client.post(LOGIN_URL, data=login_data, headers=_HEADERS)

            # 解析登录响应
            resp_text = resp.text
            if "登录成功" not in resp_text and resp.status_code != 200:
                result["message"] = f"登录失败: {resp_text[:200]}"
                return result

            # 提取时间戳
            time_match = re.search(r'"rand_time"\s*:\s*(\d+)', resp_text)
            rand_time = time_match.group(1) if time_match else str(int(time.time() * 1000))

            # Step 2: checkKidLoginUserCookie
            check_url = f"{CHECK_COOKIE_URL}?appId=kid_wdsj&gameUrl=&rand_time={rand_time}"
            resp2 = await client.get(check_url, headers=_HEADERS)

            # 检查 302 重定向
            if resp2.status_code in (301, 302):
                redirect_url = resp2.headers.get("location", "")
            else:
                redirect_url = resp2.text

            # 提取 sig, uid, time, validateState
            sig_match = re.search(r"sig=([^&]+)", redirect_url)
            uid_match = re.search(r"uid=([^&]+)", redirect_url)
            time_match2 = re.search(r"time=([^&]+)", redirect_url)
            state_match = re.search(r"validateState=([^&]+)", redirect_url)

            sig = sig_match.group(1) if sig_match else ""
            uid = uid_match.group(1) if uid_match else ""
            login_time = time_match2.group(1) if time_match2 else ""
            validate_state = state_match.group(1) if state_match else ""

            if not sig or not uid:
                result["message"] = "无法提取登录凭证 (sig/uid)"
                return result

            # Step 3: 获取 SDK 信息
            query_str = _SDK_QUERY.format(
                game_id="500352",
                sig=sig,
                uid=uid,
                time=login_time,
                validateState=validate_state,
                username=username,
            )
            sdk_url = f"{SDK_INFO_URL}?callback=&queryStr={query_str}"

            resp3 = await client.get(sdk_url, headers=_HEADERS)
            sdk_text = resp3.text

            # 解析 SDK 响应 (可能被 JSONP 回调包裹)
            sdk_text = resp3.text.strip()
            # 去除可能的 JSONP 包裹: callback({...}) 或 ({...})
            if sdk_text.startswith("(") and sdk_text.endswith(")"):
                sdk_text = sdk_text[1:-1]
            elif "(" in sdk_text and sdk_text.endswith(")"):
                # callback(data) 格式
                inner_start = sdk_text.find("(")
                if inner_start != -1:
                    sdk_text = sdk_text[inner_start + 1 : -1]

            try:
                sdk_data = json.loads(sdk_text) if sdk_text else {}
            except json.JSONDecodeError:
                result["message"] = f"SDK 响应解析失败: {sdk_text[:200]}"
                return result
            sdk_login_data = sdk_data.get("sdk_login_data", {})
            token = sdk_login_data.get("token", "")
            sdkuid = sdk_login_data.get("sdkuid", uid)

            if not token:
                result["message"] = "无法获取 SDK token"
                return result

            result["success"] = True
            result["data"] = {
                "sig": sig,
                "uid": uid,
                "time": login_time,
                "validateState": validate_state,
                "sdk_login_data": sdk_login_data,
                "token": token,
                "sdkuid": sdkuid,
                "username": username,
            }
            return result

    except Exception as e:
        result["message"] = f"请求异常: {e}"
        return result


async def login_4399_to_sauth(
    username: str,
    password: str,
    *,
    captcha_answer: str = "",
    captcha_id: str = "",
) -> dict:
    """完整的 4399 登录 → sauth_json 流程。

    Args:
        username: 4399 用户名
        password: 4399 密码
        captcha_answer: 验证码答案 (如果需要)
        captcha_id: 已获取的验证码 ID (如果为空则自动生成)

    Returns:
        dict: {"success": bool, "sauth_json": str, "message": str}
    """
    # Step 1: 验证用户名
    verify_result = await verify_username(username)
    # 使用前端传入的 captcha_id 或验证用户名时生成的
    if captcha_id:
        captcha_id_to_use = captcha_id
    else:
        captcha_id_to_use = verify_result["captcha_id"]

    # 如果需要验证码但没提供答案
    if verify_result["need_captcha"] and not captcha_answer:
        return {
            "success": False,
            "sauth_json": "",
            "message": "需要验证码,请先获取验证码图片并输入答案",
            "need_captcha": True,
            "captcha_id": captcha_id_to_use,
        }

    # Step 2: 登录
    login_result = await login_4399(
        username, password, captcha_id=captcha_id_to_use, captcha_answer=captcha_answer
    )

    if not login_result["success"]:
        return {
            "success": False,
            "sauth_json": "",
            "message": login_result["message"],
        }

    data = login_result["data"]
    token = data["token"]
    sdkuid = data["sdkuid"]

    # Step 3: 构建 sauth_json (4399pc 频道)
    udid = "".join(random.choices("0123456789ABCDEF", k=32))
    deviceid = _generate_deviceid()
    client_login_sn = "".join(random.choices("0123456789ABCDEF", k=32))

    sau = {
        "gameid": "x19",
        "login_channel": "4399pc",
        "app_channel": "4399pc",
        "platform": "pc",
        "sdkuid": sdkuid,
        "sessionid": token,
        "sdk_version": "1.0.0",
        "udid": udid,
        "deviceid": deviceid,
        "aim_info": json.dumps(
            {"aim": "127.0.0.1", "country": "CN", "tz": "+0800", "tzid": ""},
            ensure_ascii=False,
        ),
        "client_login_sn": client_login_sn,
        "gas_token": "",
        "source_platform": "pc",
        "ip": "127.0.0.1",
    }

    sauth_json_value = json.dumps(sau, ensure_ascii=False)
    sauth_json = json.dumps({"sauth_json": sauth_json_value}, ensure_ascii=False)

    return {
        "success": True,
        "sauth_json": sauth_json,
        "message": "4399 登录成功 (4399pc 频道)",
        "data": {
            "sdkuid": sdkuid,
            "token": token,
            "deviceid": deviceid,
            "username": data["username"],
        },
    }
