"""Fever Token 转 SAuth 转换器。

将 MPay/Fever Token (sdkuid + sessionid + deviceid) 转换为 netease 频道的 sauth_json。

逆向来源: NEMCTOOLS/查UID源码(1.3.8)/FeverToSauth/FeverAuth.cs
算法流程:
  1. POST /mpay/api/users/create_ticket → 获取 ticket
  2. POST /mpay/api/users/login/ticket → 用 ticket 换取新 sessionid
  3. 构建 netease 频道 sauth_json
"""

from __future__ import annotations

import json
import random
import time

import httpx

from .constants import GAME_VERSION

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
            # BUG-5.4 修复: use_random_udid=False 时之前固定使用 _HEX_PREFIX,
            # 导致所有账号共享同一 udid, 易被反作弊识别为机器人农场。
            # 现基于 sdkuid 生成确定性 udid (hash), 保证同账号 udid 稳定、
            # 不同账号 udid 不同。
            if use_random_udid:
                udid = _generate_udid()
            else:
                import hashlib
                udid = hashlib.sha256(
                    f"udid:{sdkuid}".encode("utf-8")
                ).hexdigest()[:32].upper()
            client_login_sn = _generate_client_login_sn()
            aim_info = json.dumps(
                {"aim": src_client_ip, "country": "CN", "tz": "+0800", "tzid": ""},
                ensure_ascii=False,
            )

            sau = {
                "gameid": "x19",
                "login_channel": "netease",
                "app_channel": "netease",
                "platform": "pc",
                "sdkuid": sdkuid,
                "sessionid": new_token,
                "sdk_version": src_sdk_version,
                "udid": udid,
                "deviceid": deviceid,
                "aim_info": aim_info,
                "client_login_sn": client_login_sn,
                "gas_token": "",
                "source_platform": "pc",
                "ip": src_client_ip,
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
