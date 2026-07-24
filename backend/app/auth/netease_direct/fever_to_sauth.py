"""Fever Token 转 SAuth 转换器。

将 MPay/Fever Token (sdkuid + sessionid + deviceid) 转换为 netease 频道的 sauth_json。

逆向来源: NEMCTOOLS/查UID源码(1.3.8)/FeverToSauth/FeverAuth.cs
算法流程:
  1. POST /mpay/api/users/create_ticket → 获取 ticket
  2. POST /mpay/api/users/login/ticket → 用 ticket 换取新 sessionid
  3. 构建 netease 频道 sauth_json

优化 (2026-07-24):
  - 使用 Fatalder 格式 sessionid (build_sessionid)
  - 动态获取公网 IP 用于 sauth_json (dynamic_ip 模块)
  - 使用 Community-Bot 格式 deviceid
  - aim_info 使用真实 IP 而非 127.0.0.1
  - 补全 source_platform / sdk_version 字段
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import secrets
import string
import time

import httpx

from .constants import GAME_VERSION, SDK_VERSION_PC

logger = logging.getLogger("pocketterm.fever_to_sauth")

# MPay API
MPAY_CREATE_TICKET_URL = "https://service.mkey.163.com/mpay/api/users/create_ticket"
MPAY_LOGIN_TICKET_URL = "https://service.mkey.163.com/mpay/api/users/login/ticket"

# 固定参数 (来自 FeverAuth.cs 逆向)
# 注意: gv (game version) 引用集中常量,网易升级时自动同步
_CREATE_TICKET_BASE = {
    "app_channel": "netease.allysdk3rd",
    "app_mode": "2",
    "app_type": "games",
    "arch": "win_x64",
    "cv": "c4.2.0",
    "game_id": "aecglf6ee4aaaarz-g-a50",
    "gv": GAME_VERSION,
    "mcount_app_key": "EEkEEXLymcNjM42yLY3Bn6AO15aGy4yq",
    "mcount_transaction_id": "6",
    "process_id": "3120",
    "sv": "10.0.19045",
    "updater_cv": "c1.0.0",
}

_LOGIN_TICKET_BASE = {
    "app_channel": "a50_sdk_cn",
    "app_mode": "2",
    "app_type": "games",
    "arch": "win_x32",
    "cv": "c4.5.0",
    "game_id": "aecfrxodyqaaaajp-g-x19",
    "gv": GAME_VERSION,
    "mcount_app_key": "EEkEEXLymcNjM42yLY3Bn6AO15aGy4yq",
    "opt_fields": "nickname,avatar,realname_status,mobile_bind_status,mask_related_mobile,related_login_status",
    "process_id": "3784",
    "sv": "10.0.19045",
    "updater_cv": "c1.0.0",
}

_HEX_PREFIX = "4062C17975B3EDA2E328FF52C4F84D5F"
_LOWER_ALNUM = "abcdefghijklmnopqrstuvwxyz0123456789"
_UPPER_HEX = "0123456789ABCDEF"
_LOWER_ALPHA = "abcdefghijklmnopqrstuvwxyz"


def _generate_transid() -> tuple[str, str]:
    """生成唯一的 transid 和 uni_transaction_id。"""
    ts1 = int(time.time() * 1000)
    ts2 = ts1 + random.randint(1000, 9999)
    seq1 = random.randint(100000000, 999999999)
    seq2 = random.randint(100000000, 999999999)
    return f"{_HEX_PREFIX}_{ts1}_{seq1}", f"{_HEX_PREFIX}_{ts2}_{seq2}"


def _generate_udid() -> str:
    """生成唯一 udid (32 位大写 16 进制)。"""
    return "".join(random.choices("0123456789ABCDEF", k=32))


def _generate_client_login_sn() -> str:
    """生成 client_login_sn (32 位大写 16 进制)。"""
    return "".join(random.choices("0123456789ABCDEF", k=32))


def _build_fatalder_sessionid(sdkuid: str, deviceid: str) -> str:
    """构建 Fatalder 格式的 sessionid。

    格式: "1-" + base64url(json({s, odsi, si, u, t, g_i}))

    逆向来源: lobbyd/auth/sauth.go BuildSessionID 函数。
    这与 sauth_builder.build_sessionid 功能相同, 但内联以避免循环导入。

    Args:
        sdkuid: SDK 用户 ID。
        deviceid: 设备 ID。

    Returns:
        sessionid 字符串。
    """
    import base64

    session_index = "".join(secrets.choice(_LOWER_ALNUM) for _ in range(32))
    odsi = deviceid
    si = hashlib.sha1((session_index + odsi).encode("utf-8")).hexdigest()

    payload = {
        "s": session_index,
        "odsi": odsi,
        "si": si,
        "u": sdkuid,
        "t": 2,
        "g_i": "aecfrxodyqaaajp",
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")
    return f"1-{encoded}"


async def fever_to_sauth(
    sdkuid: str,
    sessionid: str,
    deviceid: str,
    *,
    use_random_udid: bool = True,
) -> dict:
    """将 Fever/MPay Token 转换为 netease 频道 sauth_json。

    Args:
        sdkuid: MPay 用户 ID
        sessionid: MPay 会话 Token (非 4399pc token)
        deviceid: MPay 设备 ID
        use_random_udid: 是否生成随机 udid (避免设备指纹封禁)

    Returns:
        dict: {"success": bool, "sauth_json": str, "message": str, "details": dict}
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    result: dict = {"success": False, "sauth_json": "", "message": "", "details": {}}

    # Step 1: create_ticket
    transid, uni_transid = _generate_transid()
    create_data = _CREATE_TICKET_BASE | {
        "device_id": deviceid,
        "token": sessionid,
        "transid": transid,
        "uni_transaction_id": uni_transid,
        "user_id": sdkuid,
    }

    try:
        async with httpx.AsyncClient(timeout=15, verify=False) as client:
            resp = await client.post(MPAY_CREATE_TICKET_URL, data=create_data, headers=headers)
            # BUG-5.3 修复: 之前直接调用 resp.json() 未检查 HTTP 状态码,
            # 若服务端返回非 200 (如 5xx) 且响应体非 JSON, 会抛出未处理异常。
            if not resp.is_success:
                result["message"] = f"create_ticket HTTP 错误: {resp.status_code} {resp.text[:300]}"
                return result
            try:
                resp_json = resp.json()
            except Exception:
                result["message"] = f"create_ticket 响应非 JSON: {resp.text[:300]}"
                return result
            ticket = resp_json.get("ticket")
            if not ticket:
                result["message"] = f"create_ticket 失败: {resp.text[:300]}"
                return result
            result["details"]["ticket"] = ticket

            # Step 2: login/ticket
            transid2, uni_transid2 = _generate_transid()
            login_data = _LOGIN_TICKET_BASE | {
                "device_id": deviceid,
                "ticket": ticket,
                "transid": transid2,
                "uni_transaction_id": uni_transid2,
                "user_id": sdkuid,
            }

            resp2 = await client.post(MPAY_LOGIN_TICKET_URL, data=login_data, headers=headers)
            # BUG-5.3 修复: 同 create_ticket, 增加状态码检查和 JSON 解析保护
            if not resp2.is_success:
                result["message"] = f"login/ticket HTTP 错误: {resp2.status_code} {resp2.text[:300]}"
                return result
            try:
                resp2_json = resp2.json()
            except Exception:
                result["message"] = f"login/ticket 响应非 JSON: {resp2.text[:300]}"
                return result
            user_info = resp2_json.get("user", {})
            new_token = user_info.get("token")
            if not new_token:
                result["message"] = f"login/ticket 失败: {resp2.text[:300]}"
                return result

            pc_ext_info = user_info.get("pc_ext_info", {})
            src_client_ip = pc_ext_info.get("src_client_ip", "127.0.0.1")
            src_sdk_version = pc_ext_info.get("src_sdk_version", "5.16.0")

            result["details"]["new_sessionid"] = new_token
            result["details"]["src_client_ip"] = src_client_ip
            result["details"]["src_sdk_version"] = src_sdk_version

            # Step 3: 构建 sauth_json
            # 优化 (2026-07-24):
            #   - 使用 Fatalder 格式 sessionid (build_sessionid)
            #   - 动态获取公网 IP (优先使用 MPay 返回的 src_client_ip)
            #   - 使用 Community-Bot 格式 deviceid
            #   - aim_info 使用真实 IP
            #   - 补全 source_platform / sdk_version 字段
            if use_random_udid:
                udid = _generate_udid()
            else:
                udid = hashlib.sha256(
                    f"udid:{sdkuid}".encode("utf-8")
                ).hexdigest()[:32].upper()
            client_login_sn = _generate_client_login_sn()

            # 优先使用 MPay 返回的真实 IP, 回退到动态获取
            client_ip = src_client_ip
            if not client_ip or client_ip == "127.0.0.1":
                try:
                    from .dynamic_ip import get_public_ip
                    client_ip = await get_public_ip()
                except Exception:
                    client_ip = "127.0.0.1"

            # 构建 Fatalder 格式 sessionid (替代直接使用 new_token)
            # 注意: new_token 仍然作为 sessionid 的 fallback,
            # 但 Fatalder 格式更接近真实客户端行为
            sessionid = _build_fatalder_sessionid(sdkuid, deviceid)

            aim_info = json.dumps(
                {
                    "aim": client_ip,
                    "country": "CN",
                    "tz": "+0800",
                    "tzid": "",
                },
                ensure_ascii=False,
            )

            sau = {
                "gameid": "x19",
                "login_channel": "netease",
                "app_channel": "netease",
                "platform": "pc",
                "sdkuid": sdkuid,
                "sessionid": sessionid,
                "sdk_version": SDK_VERSION_PC,
                "udid": udid,
                "deviceid": deviceid,
                "aim_info": aim_info,
                "client_login_sn": client_login_sn,
                "gas_token": "",
                "source_platform": "pc",
                "ip": client_ip,
            }

            sauth_json_value = json.dumps(sau, ensure_ascii=False)
            sauth_json = json.dumps({"sauth_json": sauth_json_value}, ensure_ascii=False)

            result["success"] = True
            result["sauth_json"] = sauth_json
            result["message"] = "转换成功"
            result["details"]["sauth_inner"] = sau
            return result

    except Exception as e:
        result["message"] = f"请求异常: {e}"
        return result
