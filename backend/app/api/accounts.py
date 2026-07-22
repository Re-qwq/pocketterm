"""PocketTerm 账号管理 API

路由前缀: ``/api/accounts``

提供以下端点:

账号 CRUD:
    - ``GET    ""``                            列出所有账号
    - ``POST   ""``                            添加账号 (sauth_json)
    - ``DELETE "/{account_id}"``             删除账号
    - ``PUT    "/{account_id}"``             更新账号
    - ``POST   "/import"``                   批量导入
    - ``GET    "/export"``                   导出所有账号
    - ``POST   "/{account_id}/assign/{bot_id}"`` 分配账号给机器人
    - ``POST   "/{account_id}/check"``       检测账号是否可用 (尝试登录)

注册方式 1 - SMS 注册 (代理 cookie.xingbai.top):
    - ``GET    "/register/captcha"``          获取验证码 (数学题)
    - ``POST   "/register/verify"``           验证验证码
    - ``POST   "/register/start"``            开始注册 (用户已发短信后调用)
    - ``GET    "/register/status"``           轮询注册状态
    - ``GET    "/register/results"``          获取注册结果

注册方式 2 - NetHard OpenAPI (nv1.nethard.pro):
    - ``POST   "/nethard/login"``             登录 NetHard 获取 OpenAPI Key
    - ``POST   "/nethard/sauth"``             使用 OpenAPI Key 获取 SAuth

注册方式 3 - FastBuilder 游客旁路 (本地生成指纹):
    - ``POST   "/fastbuilder/guest"``          生成设备指纹并尝试获取 fbtoken

账号核心字段:
    - sauth_json: 网易登录凭证 JSON (核心)
    - nickname: 游戏昵称
    - uid: 游戏 UID
    - status: active / banned / unknown
    - last_checked: 上次检测时间戳
    - ban_reason: 封禁原因 (如果被封)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..bot.manager import bot_manager
from ..config import DATA_DIR, get_config
from ..logger import get_logger
from ..auth.netease_direct.constants import SDK_VERSION_PC, SDK_VERSION_PE
from .deps import error_response, get_current_user, success_response

logger = get_logger("api.accounts")

router = APIRouter(prefix="/api/accounts", tags=["账号管理"])

# cookie.xingbai.top 注册服务地址
COOKIE_API_BASE = "https://cookie.xingbai.top/api"
# 注册需要发送的短信验证码和目标号码
SMS_CODE = "367550"
SMS_TARGET = "1069016373035"

# NetHard 用户中心 API 地址
NETHARD_API_BASE = "https://nv1.nethard.pro/api"

# FastBuilder 默认认证服务器
FASTBUILDER_DEFAULT_AUTH_SERVER = "https://nv1.nethard.pro"
# 备用认证服务器 (NetHard)
NETHARD_AUTH_SERVER = "https://nv1.nethard.pro"

# MPay API (网易官方手机号登录, 免费)
MPAY_HOST = "https://service.mkey.163.com"
MPAY_PROJECT_ID = "x19"
MPAY_GAME_VERSION = "c1.25.0"


def _get_verify_ssl() -> bool:
    """从全局配置读取 SSL 证书校验开关。

    对应 ``config.yaml`` 中的 ``network.verify_ssl``。
    生产环境应设为 ``true``; 默认 ``false`` 因为网易 API 证书
    有时不受系统信任。
    """
    try:
        return get_config().verify_ssl
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 注册会话 - 共享 HTTP 客户端 (保持 cookie)
# ---------------------------------------------------------------------------
class RegistrationSession:
    """cookie.xingbai.top 注册会话。

    使用 httpx.Cookies 保持验证码验证后的 session,
    确保 /start 等后续请求能识别已验证的用户。
    """

    def __init__(self) -> None:
        self._cookies: httpx.Cookies = httpx.Cookies()
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None
        self._sms_code: str = ""
        self._sms_target: str = ""
        self._sms_backup: str = ""

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def request(self, method: str, path: str, json_body: dict = None) -> dict:
        url = f"{COOKIE_API_BASE}{path}"
        async with self._get_lock():
            async with httpx.AsyncClient(
                timeout=15.0, verify=_get_verify_ssl(), cookies=self._cookies
            ) as client:
                if method == "GET":
                    resp = await client.get(url)
                else:
                    resp = await client.post(url, json=json_body)
                # 保存更新后的 cookies
                self._cookies.update(resp.cookies)
        try:
            data = resp.json()
        except Exception:
            data = {"error": resp.text[:200]}
        data["_status_code"] = resp.status_code
        return data

    async def fetch_sms_info(self) -> dict:
        """从 cookie.xingbai.top/cookie-generator HTML 页面提取短信验证码和目标号码。

        页面 HTML 中包含:
            - 验证码 (sms-code): 如 "367550"
            - 目标号码 (sms-num): 如 "1069016373035"
            - 备用号码: 如 "10698163016373035"
        """
        try:
            async with self._get_lock():
                async with httpx.AsyncClient(
                    timeout=15.0, verify=_get_verify_ssl(), cookies=self._cookies
                ) as client:
                    resp = await client.get("https://cookie.xingbai.top/cookie-generator")
            html = resp.text

            # 提取验证码 (sms-code class 中的数字)
            code_match = re.findall(r'sms-code[^>]*>(\d+)', html)
            # 提取所有 11+ 位数字 (可能是目标号码)
            all_nums = re.findall(r'(\d{11,})', html)

            sms_code = code_match[0] if code_match else ""
            # 第一个长号码通常是主号码，第二个是备用
            sms_target = all_nums[0] if all_nums else ""
            sms_backup = all_nums[1] if len(all_nums) > 1 else ""

            if sms_code:
                self._sms_code = sms_code
            if sms_target:
                self._sms_target = sms_target
            if sms_backup:
                self._sms_backup = sms_backup

        except Exception as e:
            logger.warning(f"获取 SMS 信息失败: {e}")

        return {
            "sms_code": self._sms_code or SMS_CODE,
            "sms_target": self._sms_target or SMS_TARGET,
            "sms_backup": self._sms_backup or "",
        }

    def get_sms_info(self) -> dict:
        """返回缓存的 SMS 信息 (不发起网络请求)。"""
        return {
            "sms_code": self._sms_code or SMS_CODE,
            "sms_target": self._sms_target or SMS_TARGET,
            "sms_backup": self._sms_backup or "",
        }

    def reset(self) -> None:
        self._cookies = httpx.Cookies()
        self._sms_code = ""
        self._sms_target = ""
        self._sms_backup = ""


#: 全局注册会话
_registration_session = RegistrationSession()


# ---------------------------------------------------------------------------
# MPay 手机号登录会话 (网易官方 API, 免费)
# ---------------------------------------------------------------------------
class MPaySession:
    """MPay 手机号登录会话。

    通过网易官方 MPay API (service.mkey.163.com) 完成手机号登录。
    完全免费, 直接产生有效的网易 sauth_json, 不需要 uni_sauth 转换。

    流程:
        1. register_device()  → 自动注册设备, 获取 device_id
        2. send_sms(phone)    → 发送短信, 返回验证模式 (normal/upstream)
        3. verify_sms(phone, code/up_content) → 验证, 获取 ticket
        4. finish(phone, ticket) → 完成登录, 返回 user_id + token
        5. build_sauth(user_id, token, device_id) → 生成 sauth_json

    来源: WPFLauncher_Hook 项目的 loginByPhone.py
    """

    def __init__(self) -> None:
        self._device_id: str = ""
        self._unique_id: str = ""
        self._phone: str = ""
        self._sms_mode: str = ""  # "normal" 或 "upstream"
        self._upstream_content: str = ""
        self._upstream_number: str = ""
        self._ticket: str = ""
        self._user_id: str = ""
        self._token: str = ""

    @staticmethod
    def _base_params() -> dict:
        return {
            "app_channel": "netease",
            "app_mode": "2",
            "app_type": "games",
            "arch": "win_x64",
            "cv": "c4.2.0",
            "mcount_app_key": "EEkEEXLymcNjM42yLY3Bn6AO15aGy4yq",
            "mcount_transaction_id": "0",
            "process_id": "1000",
            "sv": "10.0.22621",
            "updater_cv": "c1.0.0",
            "game_id": MPAY_PROJECT_ID,
            "gv": MPAY_GAME_VERSION,
        }

    async def register_device(self) -> str:
        """注册设备, 返回 device_id。"""
        import random
        import string as string_mod

        self._unique_id = uuid.uuid4().hex
        mac = ":".join(["{:02x}".format(random.randint(0, 255)) for _ in range(6)])
        device_name = f"PC-{''.join(random.choices(string_mod.ascii_letters + string_mod.digits, k=12))}"

        params = self._base_params()
        params.update({
            "unique_id": self._unique_id,
            "brand": "Microsoft",
            "device_model": "pc_mode",
            "device_name": device_name,
            "device_type": "Computer",
            "init_urs_device": "0",
            "mac": mac,
            "resolution": "1920x1080",
            "system_name": "windows",
            "system_version": "10.0.22621",
        })

        async with httpx.AsyncClient(verify=_get_verify_ssl(), timeout=15.0) as client:
            resp = await client.post(
                f"{MPAY_HOST}/mpay/games/{MPAY_PROJECT_ID}/devices",
                data=params,
                headers={
                    "User-Agent": "WPFLauncher/0.0.0.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            data = resp.json()
            # Bug 1.2 修复: 之前直接用 data["device"]["id"] 下标访问, 若网易 API
            # 返回非预期结构 (如错误响应), 会抛出未捕获的 KeyError。改用 .get()
            # 链式访问并校验非空。
            device_id = ""
            if isinstance(data, dict):
                device_id = str(data.get("device", {}).get("id", ""))
            if not device_id:
                raise RuntimeError(
                    f"MPay 设备注册响应结构异常: {str(data)[:300]}"
                )
            self._device_id = device_id
            logger.info(f"MPay 设备注册成功: device_id={self._device_id}")
            return self._device_id

    async def send_sms(self, phone: str) -> dict:
        """发送短信验证码。

        返回:
            {"mode": "normal"} - 下行短信 (服务器发送验证码到手机)
            {"mode": "upstream", "content": "...", "number": "..."} - 上行短信 (用户发送短信)
        """
        self._phone = phone
        params = self._base_params()
        params.update({"device_id": self._device_id, "mobile": phone})

        async with httpx.AsyncClient(verify=_get_verify_ssl(), timeout=15.0) as client:
            resp = await client.post(
                f"{MPAY_HOST}/mpay/api/users/login/mobile/get_sms",
                data=params,
                headers={
                    "User-Agent": "WPFLauncher/0.0.0.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code == 200:
                self._sms_mode = "normal"
                return {"mode": "normal", "message": "验证码已发送到您的手机"}

            code = data.get("code", -1)
            if code == 1373:
                reply_sms = data.get("reply_sms", {})
                self._sms_mode = "upstream"
                self._upstream_content = reply_sms.get("content", "")
                self._upstream_number = reply_sms.get("number", "")
                return {
                    "mode": "upstream",
                    "content": self._upstream_content,
                    "number": self._upstream_number,
                    "tips": reply_sms.get("tips", ""),
                    "message": f"请用手机 {phone} 发送短信 '{self._upstream_content}' 到 {self._upstream_number}",
                }

            reason = data.get("reason", "未知错误")
            raise RuntimeError(f"发送短信失败: code={code}, reason={reason}")

    async def verify_sms(self, phone: str, code: str = "", up_content: str = "") -> str:
        """验证短信, 返回 ticket。

        Args:
            phone: 手机号
            code: 下行短信验证码 (normal 模式)
            up_content: 上行短信内容 (upstream 模式)
        """
        params = self._base_params()
        params.update({
            "device_id": self._device_id,
            "mobile": phone,
            "smscode": code,
            "up_content": up_content,
        })

        async with httpx.AsyncClient(verify=_get_verify_ssl(), timeout=15.0) as client:
            resp = await client.post(
                f"{MPAY_HOST}/mpay/api/users/login/mobile/verify_sms",
                data=params,
                headers={
                    "User-Agent": "WPFLauncher/0.0.0.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if resp.status_code == 200:
                self._ticket = resp.json().get("ticket", "")
                if not self._ticket:
                    raise RuntimeError("验证成功但未返回 ticket")
                return self._ticket

            try:
                data = resp.json()
                reason = data.get("reason", f"HTTP {resp.status_code}")
            except Exception:
                reason = f"HTTP {resp.status_code}"
            raise RuntimeError(f"验证失败: {reason}")

    async def finish(self, phone: str, ticket: str) -> dict:
        """完成登录, 返回 user_id 和 token。"""
        encoded_phone = base64.b64encode(phone.encode("utf-8")).decode("utf-8")
        params = self._base_params()
        params.update({
            "device_id": self._device_id,
            "ticket": ticket,
            "opt_fields": "nickname,avatar,realname_status,mobile_bind_status,mask_related_mobile,related_login_status",
        })

        async with httpx.AsyncClient(verify=_get_verify_ssl(), timeout=15.0) as client:
            resp = await client.post(
                f"{MPAY_HOST}/mpay/api/users/login/mobile/finish?un={encoded_phone}",
                data=params,
                headers={
                    "User-Agent": "WPFLauncher/0.0.0.0",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"完成登录失败: HTTP {resp.status_code}")

            data = resp.json()
            user = data.get("user", {})
            self._user_id = str(user.get("id", ""))
            self._token = str(user.get("token", ""))

            if not self._user_id or not self._token:
                raise RuntimeError(f"登录返回数据不完整: {data}")

            return {
                "user_id": self._user_id,
                "token": self._token,
                "nickname": user.get("nickname", ""),
            }

    def build_sauth(self, user_id: str, token: str, device_id: str = "") -> str:
        """构建 sauth_json 字符串。"""
        dev = device_id or self._device_id
        sauth = {
            "gameid": MPAY_PROJECT_ID,
            "login_channel": "netease",
            "app_channel": "netease",
            "platform": "pc",
            "sdkuid": user_id,
            "sessionid": token,
            "sdk_version": SDK_VERSION_PC,
            "udid": uuid.uuid4().hex.upper(),
            "deviceid": dev,
            "aim_info": '{"aim":"127.0.0.1","country":"CN","tz":"+0800","tzid":""}',
            "client_login_sn": secrets.token_hex(16).upper(),
            "gas_token": "",
            "source_platform": "pc",
            "ip": "127.0.0.1",
        }
        return json.dumps(sauth, separators=(",", ":"))

    def reset(self) -> None:
        self._device_id = ""
        self._unique_id = ""
        self._phone = ""
        self._sms_mode = ""
        self._upstream_content = ""
        self._upstream_number = ""
        self._ticket = ""
        self._user_id = ""
        self._token = ""

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def sms_mode(self) -> str:
        return self._sms_mode

    @property
    def upstream_content(self) -> str:
        return self._upstream_content

    @property
    def upstream_number(self) -> str:
        return self._upstream_number


#: 全局 MPay 会话
_mpay_session = MPaySession()


# ---------------------------------------------------------------------------
# 持久化存储
# ---------------------------------------------------------------------------
class AccountStore:
    """基于 JSON 文件的账号存储（线程安全）。

    文件位置: ``backend/data/accounts.json``。

    每条记录包含:
        - account_id: 内部 ID
        - sauth_json: 网易登录凭证 (核心)
        - nickname: 游戏昵称
        - uid: 游戏 UID
        - status: active / banned / unknown
        - last_checked: 上次检测时间戳
        - ban_reason: 封禁原因
        - notes: 备注
        - assigned_bot_id: 已分配的机器人 ID
        - created_at / updated_at: 时间戳
    """

    def __init__(self, path: Path) -> None:
        self._path: Path = path
        self._lock: threading.Lock = threading.Lock()
        self._accounts: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._accounts = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._accounts = data
            else:
                self._accounts = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"加载账号文件失败，使用空存储: {exc}")
            self._accounts = {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as handle:
                json.dump(self._accounts, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            logger.error(f"保存账号文件失败: {exc}")

    @staticmethod
    def _new_id() -> str:
        return f"acc_{uuid.uuid4().hex[:12]}"

    def list_all(self) -> List[Dict[str, Any]]:
        with self._lock:
            accounts = list(self._accounts.values())
        accounts.sort(key=lambda a: a.get("created_at", 0))
        return accounts

    def get(self, account_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._accounts.get(account_id)) if account_id in self._accounts else None

    def add(self, account: Dict[str, Any]) -> Dict[str, Any]:
        account_id = account.get("account_id") or self._new_id()
        now = time.time()
        record: Dict[str, Any] = {
            "account_id": account_id,
            "sauth_json": account.get("sauth_json", ""),
            "nickname": account.get("nickname", ""),
            "uid": account.get("uid", ""),
            "status": account.get("status", "unknown"),
            "last_checked": account.get("last_checked", 0),
            "ban_reason": account.get("ban_reason", ""),
            "notes": account.get("notes", ""),
            "assigned_bot_id": account.get("assigned_bot_id", ""),
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._accounts[account_id] = record
            self._save()
        logger.info(f"添加账号 {account_id} (nickname={record['nickname']})")
        return record

    def update(self, account_id: str, updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._accounts.get(account_id)
            if record is None:
                return None
            for key in (
                "sauth_json",
                "nickname",
                "uid",
                "status",
                "last_checked",
                "ban_reason",
                "notes",
                "assigned_bot_id",
                "last_auth_time",
            ):
                if key in updates:
                    record[key] = updates[key]
            record["updated_at"] = time.time()
            self._save()
            return dict(record)

    def delete(self, account_id: str) -> bool:
        with self._lock:
            if account_id not in self._accounts:
                return False
            del self._accounts[account_id]
            self._save()
            return True

    def assign(self, account_id: str, bot_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            record = self._accounts.get(account_id)
            if record is None:
                return None
            record["assigned_bot_id"] = bot_id
            record["updated_at"] = time.time()
            self._save()
            return dict(record)

    def find_by_sauth(self, sauth_json: str) -> Optional[Dict[str, Any]]:
        """查找是否已有相同 sauth_json 的账号。"""
        with self._lock:
            for record in self._accounts.values():
                if record.get("sauth_json", "") == sauth_json:
                    return dict(record)
        return None


#: 全局账号存储实例
_account_store: Optional[AccountStore] = None
_store_lock = threading.Lock()


def get_account_store() -> AccountStore:
    global _account_store
    if _account_store is None:
        with _store_lock:
            if _account_store is None:
                _account_store = AccountStore(DATA_DIR / "accounts.json")
    return _account_store


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class AccountRequest(BaseModel):
    """添加/更新账号请求。"""
    sauth_json: str = Field("", description="网易登录凭证 JSON")
    nickname: str = Field("", description="游戏昵称")
    uid: str = Field("", description="游戏 UID")
    status: str = Field("unknown", description="账号状态: active/banned/unknown")
    notes: str = Field("", description="备注")
    assigned_bot_id: str = Field("", description="已分配的机器人 ID")


class ImportRequest(BaseModel):
    """批量导入请求体。"""
    accounts: List[AccountRequest] = Field(..., description="账号列表")


class VerifyCaptchaRequest(BaseModel):
    """验证码验证请求。"""
    answer: str = Field(..., description="验证码答案")


class RegisterStartRequest(BaseModel):
    """开始注册请求。"""
    # 无需额外参数，短信已发送即可调用


class NetHardLoginRequest(BaseModel):
    """NetHard 登录请求。"""
    username: str = Field(..., description="NetHard 用户名")
    password: str = Field(..., description="NetHard 密码 (明文, 后端会 SHA256)")


class NetHardSAuthRequest(BaseModel):
    """通过 NetHard OpenAPI Key 获取 SAuth。"""
    api_key: str = Field(..., description="NetHard OpenAPI Key (UUID 格式)")


class FastBuilderGuestRequest(BaseModel):
    """FastBuilder 游客旁路请求。"""
    auth_server: str = Field(
        FASTBUILDER_DEFAULT_AUTH_SERVER,
        description="认证服务器地址",
    )
    api_key: str = Field("", description="认证服务器 API Key (可选)")
    server_code: str = Field("1", description="目标服务器代码")
    server_password: str = Field("", description="服务器密码 (可选)")


# ---------------------------------------------------------------------------
# 账号 CRUD 路由
# ---------------------------------------------------------------------------
@router.get("")
async def list_accounts(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_account_store()
    accounts = store.list_all()
    return success_response(
        data={"accounts": accounts, "total": len(accounts)},
        message=f"共 {len(accounts)} 个账号",
    )


@router.post("")
async def add_account(
    body: AccountRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_account_store()
    if not body.sauth_json:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sauth_json 不能为空",
        )
    # 去重检查
    existing = store.find_by_sauth(body.sauth_json)
    if existing:
        return success_response(data=existing, message="该账号已存在")
    record = store.add(body.model_dump())
    return success_response(data=record, message="账号添加成功")


@router.delete("/{account_id}")
async def delete_account(
    account_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_account_store()
    ok = store.delete(account_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"账号不存在: {account_id}",
        )
    return success_response(message="账号已删除")


@router.put("/{account_id}")
async def update_account(
    account_id: str,
    body: AccountRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_account_store()
    record = store.update(account_id, body.model_dump())
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"账号不存在: {account_id}",
        )
    return success_response(data=record, message="账号已更新")


@router.post("/import")
async def import_accounts(
    body: ImportRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_account_store()
    imported: List[Dict[str, Any]] = []
    skipped = 0
    for item in body.accounts:
        data = item.model_dump()
        if not data.get("sauth_json"):
            skipped += 1
            continue
        existing = store.find_by_sauth(data["sauth_json"])
        if existing:
            skipped += 1
            continue
        record = store.add(data)
        imported.append(record)
    logger.info(f"批量导入 {len(imported)} 个账号 (跳过 {skipped} 个重复/无效)")
    return success_response(
        data={"imported": imported, "count": len(imported), "skipped": skipped},
        message=f"成功导入 {len(imported)} 个账号 (跳过 {skipped} 个)",
    )


@router.get("/export")
async def export_accounts(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Response:
    store = get_account_store()
    accounts = store.list_all()
    payload = json.dumps(
        {"accounts": accounts, "exported_at": time.time()},
        ensure_ascii=False,
        indent=2,
    )
    return Response(
        content=payload,
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="pocketterm_accounts.json"'
        },
    )


@router.post("/{account_id}/assign/{bot_id}")
async def assign_account(
    account_id: str,
    bot_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    store = get_account_store()
    if bot_manager.get_bot(bot_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"机器人不存在: {bot_id}",
        )
    record = store.assign(account_id, bot_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"账号不存在: {account_id}",
        )
    return success_response(data=record, message=f"账号已分配给机器人 {bot_id}")


# ---------------------------------------------------------------------------
# 账号检测 (尝试登录)
# ---------------------------------------------------------------------------
@router.post("/{account_id}/check")
async def check_account(
    account_id: str,
    full_check: bool = False,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """检测账号是否可用。

    默认使用轻量检查 (仅 login-otp), 不会触发封禁检测, 可安全反复调用。
    设置 full_check=true 会进行完整认证 (login-otp + authentication-otp),
    能检测封禁状态, 但频繁调用有封禁风险。

    轻量检查:
        - code=0 → sessionid 有效 (账号可能可用)
        - code=32 → sessionid 已过期
        - code=29 → 封禁 (少见, 通常在 auth 阶段返回)
    完整检查:
        - code=0 → 账号可用, 获取 UID
        - code=29 → 封禁
        - code=32 → 过期/维护
    """
    store = get_account_store()
    account = store.get(account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"账号不存在: {account_id}",
        )

    sauth_json = account.get("sauth_json", "")
    if not sauth_json:
        return error_response("sauth_json 为空", "账号凭证缺失")

    # 完整检查冷却: 10 分钟内不重复 auth, 避免触发反作弊
    AUTH_COOLDOWN = 600  # 10 分钟
    now = time.time()
    if full_check:
        last_auth = account.get("last_auth_time", 0)
        if last_auth and (now - last_auth) < AUTH_COOLDOWN:
            remaining = int(AUTH_COOLDOWN - (now - last_auth))
            return success_response(
                data={
                    "status": account.get("status", "unknown"),
                    "cooldown": remaining,
                },
                message=f"完整检查冷却中, 请 {remaining} 秒后再试 (防止频繁认证触发封禁)",
            )

    try:
        from ..auth.netease_direct.client import NeteaseDirectClient

        async with NeteaseDirectClient(mode="pc") as client:
            if full_check:
                # 完整认证: login-otp + authentication-otp
                # 会触发封禁检测, 有被封风险!
                await client.login(sauth_json)
                store.update(account_id, {
                    "status": "active",
                    "last_checked": now,
                    "last_auth_time": now,
                    "ban_reason": "",
                    "uid": client.uid or account.get("uid", ""),
                    "nickname": client.player_name or account.get("nickname", ""),
                })
                return success_response(
                    data={
                        "status": "active",
                        "uid": client.uid,
                        "nickname": client.player_name,
                    },
                    message="完整认证成功，账号可用",
                )
            else:
                # 轻量检查: 仅 login-otp, 不触发封禁检测
                result = await client.check_session(sauth_json)
                code = result["code"]

                if code == 0:
                    # sessionid 有效, 但不确定是否被封 (需要 full_check 才知道)
                    store.update(account_id, {
                        "status": "active",
                        "last_checked": now,
                        "ban_reason": "",
                    })
                    return success_response(
                        data={
                            "status": "active",
                            "session_valid": True,
                            "aid": result.get("aid", ""),
                            "note": "sessionid 有效 (未检测封禁状态, 需要 full_check=true 检测)",
                        },
                        message="sessionid 有效",
                    )
                elif code == 32:
                    store.update(account_id, {
                        "status": "expired",
                        "last_checked": now,
                        "ban_reason": f"login-otp code=32, {result.get('message', '')}",
                    })
                    return success_response(
                        data={"status": "expired", "code": 32, "reason": result.get("message", "")},
                        message="sessionid 已过期或服务器维护中",
                    )
                elif code == 29:
                    store.update(account_id, {
                        "status": "banned",
                        "last_checked": now,
                        "ban_reason": f"login-otp code=29, {result.get('message', '')}",
                    })
                    return success_response(
                        data={"status": "banned", "code": 29, "reason": result.get("message", "")},
                        message="账号已被封禁",
                    )
                else:
                    store.update(account_id, {
                        "status": "unknown",
                        "last_checked": now,
                        "ban_reason": f"login-otp code={code}, {result.get('message', '')}",
                    })
                    return success_response(
                        data={"status": "unknown", "code": code, "reason": result.get("message", "")},
                        message=f"检查失败: code={code}",
                    )
    except RuntimeError as e:
        err_msg = str(e)
        now = time.time()
        if full_check:
            store.update(account_id, {"last_auth_time": now})
        if "code=29" in err_msg or "禁止登录" in err_msg:
            store.update(account_id, {
                "status": "banned",
                "last_checked": now,
                "ban_reason": err_msg[:200],
            })
            return success_response(
                data={"status": "banned", "reason": err_msg[:200]},
                message="账号已被封禁",
            )
        elif "code=32" in err_msg or "服务器维护" in err_msg:
            store.update(account_id, {
                "status": "unknown",
                "last_checked": now,
                "ban_reason": err_msg[:200],
            })
            return success_response(
                data={"status": "unknown", "reason": err_msg[:200]},
                message="登录异常 (可能服务器维护中)",
            )
        else:
            store.update(account_id, {
                "status": "unknown",
                "last_checked": now,
                "ban_reason": err_msg[:200],
            })
            return success_response(
                data={"status": "unknown", "reason": err_msg[:200]},
                message=f"登录失败: {err_msg[:100]}",
            )
    except Exception as e:
        logger.exception(f"检测账号 {account_id} 时出错")
        return error_response(str(e), "检测过程中出错")


# ---------------------------------------------------------------------------
# 注册向导 - 代理 cookie.xingbai.top (使用共享 session 保持 cookie)
# ---------------------------------------------------------------------------
@router.get("/register/captcha")
async def register_captcha(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取注册验证码 (数学题)。

    每次获取验证码时重置 session，确保旧的验证状态被清除。
    同时从 cookie.xingbai.top HTML 页面动态提取短信验证码和目标号码。
    """
    _registration_session.reset()
    try:
        data = await _registration_session.request("GET", "/captcha")
        if data.get("_status_code") != 200 and "question" not in data:
            return error_response(
                f"HTTP {data.get('_status_code', '?')}",
                f"获取验证码失败: {data.get('error', data)}",
            )
        # 动态获取短信信息 (从 HTML 页面提取)
        sms_info = await _registration_session.fetch_sms_info()
        data["sms_code"] = sms_info["sms_code"]
        data["sms_target"] = sms_info["sms_target"]
        data["sms_backup"] = sms_info["sms_backup"]
        return success_response(data=data, message="验证码获取成功")
    except Exception as e:
        logger.exception("获取验证码时出错")
        return error_response(str(e), "获取验证码失败")


@router.post("/register/verify")
async def register_verify(
    body: VerifyCaptchaRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """验证验证码答案。"""
    try:
        data = await _registration_session.request(
            "POST", "/verify-captcha", {"answer": body.answer}
        )
        ok = data.get("ok", data.get("success", False))
        if not ok or data.get("_status_code", 200) != 200:
            return error_response(
                f"HTTP {data.get('_status_code', '?')}",
                f"验证失败: {data.get('error', data)}",
            )
        return success_response(data=data, message="验证码已验证")
    except Exception as e:
        logger.exception("验证验证码时出错")
        return error_response(str(e), "验证失败")


@router.post("/register/start")
async def register_start(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """开始注册 (用户已发送短信后调用)。"""
    try:
        data = await _registration_session.request("POST", "/start")
        ok = data.get("ok", data.get("success", False))
        if not ok:
            err = data.get("error", data.get("message", ""))
            # 检查是否是限流
            if "拉黑" in str(err) or "黑" in str(err):
                return error_response(
                    str(err),
                    f"cookie.xingbai.top 限流: {err}",
                )
            return error_response(
                str(err) if err else "未知错误",
                f"启动注册失败: {err or data}",
            )
        return success_response(data=data, message="注册已启动")
    except Exception as e:
        logger.exception("启动注册时出错")
        return error_response(str(e), "启动注册失败")


@router.get("/register/status")
async def register_status(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """轮询注册状态。"""
    try:
        data = await _registration_session.request("GET", "/status")
        return success_response(data=data, message="状态获取成功")
    except Exception as e:
        logger.exception("获取注册状态时出错")
        return error_response(str(e), "获取状态失败")


@router.get("/register/results")
async def register_results(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取注册结果并自动保存到账号列表。"""
    try:
        data = await _registration_session.request("GET", "/results")
        # 自动保存获取到的账号
        display_cookies = data.get("display_cookies", [])
        store = get_account_store()
        saved: List[Dict[str, Any]] = []
        for cookie in display_cookies:
            sauth_str = cookie.get("sauth_str", "")
            nickname = cookie.get("nickname", "")
            if not sauth_str:
                continue
            # 去重
            existing = store.find_by_sauth(sauth_str)
            if existing:
                saved.append(existing)
                continue
            record = store.add({
                "sauth_json": sauth_str,
                "nickname": nickname,
                "status": "active",
                "last_checked": time.time(),
            })
            saved.append(record)
        data["saved_accounts"] = saved
        data["saved_count"] = len(saved)
        return success_response(
            data=data,
            message=f"获取到 {len(display_cookies)} 个账号, 已保存 {len(saved)} 个",
        )
    except Exception as e:
        logger.exception("获取注册结果时出错")
        return error_response(str(e), "获取结果失败")


# ---------------------------------------------------------------------------
# 注册方式 2 - NetHard OpenAPI (nv1.nethard.pro)
# ---------------------------------------------------------------------------
# NetHard API 结构 (通过逆向前端 JS 获得):
#   POST /api/user/login     → {username, password (SHA256)} → 登录
#   GET  /api/user/get-token → 获取 OpenAPI Key (需登录 session)
#   GET  /api/user/getLoginUserSAuth → 获取 SAuth (需 OpenAPI Key)
#
# OpenAPI 认证头:
#   Authorization: <UUID API Key>
#   X-Caller: gameaccount | helperbot
#
# 密码哈希: SHA256(明文密码)
# ---------------------------------------------------------------------------

#: NetHard 登录后的 session (内存中保存)
_nethard_session: Dict[str, Any] = {"cookie": None, "api_key": "", "username": ""}


@router.post("/nethard/login")
async def nethard_login(
    body: NetHardLoginRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """登录 NetHard 用户中心，获取 OpenAPI Key。

    流程:
    1. POST /api/user/login (密码 SHA256)
    2. GET /api/user/get-token 获取 OpenAPI Key
    """
    hashed_password = hashlib.sha256(body.password.encode()).hexdigest()

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=_get_verify_ssl()) as client:
            # Step 1: Login
            resp = await client.post(
                f"{NETHARD_API_BASE}/user/login",
                json={"username": body.username, "password": hashed_password},
            )
            login_data = resp.json()

            if not login_data.get("success", False):
                msg = login_data.get("message", "登录失败")
                return error_response(msg, f"NetHard 登录失败: {msg}")

            # 保存 session cookie
            _nethard_session["cookie"] = dict(resp.cookies)
            _nethard_session["username"] = body.username

            # Step 2: Get OpenAPI Key
            resp2 = await client.get(
                f"{NETHARD_API_BASE}/user/get-token",
                cookies=resp.cookies,
            )
            token_data = resp2.json()

            api_key = ""
            if token_data.get("success", False):
                # 尝试多种可能的字段名
                api_key = (
                    token_data.get("data", {}).get("key", "")
                    or token_data.get("data", {}).get("apiKey", "")
                    or token_data.get("data", {}).get("token", "")
                )

            if not api_key:
                # 尝试 /user/info 获取 apiKey
                resp3 = await client.get(
                    f"{NETHARD_API_BASE}/user/info",
                    cookies=resp.cookies,
                )
                info_data = resp3.json()
                if info_data.get("success", False):
                    user_info = info_data.get("data", {})
                    api_key = user_info.get("apiKey", user_info.get("api_key", ""))

            _nethard_session["api_key"] = api_key

            return success_response(
                data={
                    "username": body.username,
                    "api_key": api_key,
                    "has_api_key": bool(api_key),
                },
                message="NetHard 登录成功" + ("，已获取 OpenAPI Key" if api_key else "，但未获取到 OpenAPI Key"),
            )
    except httpx.ConnectError as e:
        return error_response(str(e), "无法连接 NetHard 服务器")
    except Exception as e:
        logger.exception("NetHard 登录时出错")
        return error_response(str(e), "登录失败")


@router.post("/nethard/sauth")
async def nethard_get_sauth(
    body: NetHardSAuthRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """使用 NetHard OpenAPI Key 获取 SAuth。

    调用 NetHard OpenAPI:
        GET /api/user/getLoginUserSAuth
        Headers:
            Authorization: <api_key>
            X-Caller: gameaccount

    成功后自动保存到账号列表。
    """
    try:
        async with httpx.AsyncClient(timeout=15.0, verify=_get_verify_ssl()) as client:
            resp = await client.get(
                f"{NETHARD_API_BASE}/user/getLoginUserSAuth",
                headers={
                    "Authorization": body.api_key,
                    "X-Caller": "gameaccount",
                },
            )
            data = resp.json()

            if not data.get("success", False):
                msg = data.get("message", "获取失败")
                return error_response(msg, f"获取 SAuth 失败: {msg}")

            sauth_data = data.get("data", {}).get("sauth", {})
            if not sauth_data:
                return error_response("sauth 字段为空", "未获取到 SAuth 数据")

            # sauth 可能是 JSON 对象或字符串
            if isinstance(sauth_data, dict):
                sauth_json_str = json.dumps(sauth_data)
            else:
                sauth_json_str = str(sauth_data)

            # 去重检查
            store = get_account_store()
            existing = store.find_by_sauth(sauth_json_str)
            if existing:
                return success_response(
                    data=existing,
                    message="该 SAuth 已存在于账号列表中",
                )

            # 保存账号
            username = _nethard_session.get("username", "")
            nick = sauth_data.get("nickname", "") if isinstance(sauth_data, dict) else ""
            nickname = username or nick
            record = store.add({
                "sauth_json": sauth_json_str,
                "nickname": f"NetHard_{nickname}",
                "status": "unknown",
                "last_checked": time.time(),
                "notes": "通过 NetHard OpenAPI 获取",
            })

            return success_response(
                data=record,
                message="SAuth 获取成功，已保存到账号列表",
            )
    except httpx.ConnectError as e:
        return error_response(str(e), "无法连接 NetHard 服务器")
    except Exception as e:
        logger.exception("获取 NetHard SAuth 时出错")
        return error_response(str(e), "获取 SAuth 失败")


# ---------------------------------------------------------------------------
# 注册方式 3 - FastBuilder 游客旁路 (本地生成设备指纹)
# ---------------------------------------------------------------------------
# 通过本地生成随机设备指纹 + sessionid, 发送到 FastBuilder 认证服务器,
# 获取 fbtoken 和 ChainInfo (用于 WebSocket 连接 Minecraft 服务器)。
#
# 认证流程:
#   1. GET /api/new → 获取 secret (Bearer token)
#   2. POST /api/phoenix/login → 使用 secret 认证, 获取 fbtoken
#
# sessionid 构造:
#   s = 随机 32 字符 (a-z0-9)
#   si = SHA1(s + deviceid)
#   payload = {s, odsi, si, u, t:2, g_i}
#   sessionid = "1-" + base64url(JSON(payload))
#
# 注意: 此方法需要认证服务器的 API Key (X-API-Key 头)。
#       fatalder.yeah114.top 现在要求 API Key。
#       nv1.nethard.pro 的 /api/phoenix/login 返回"未知错误"。
# ---------------------------------------------------------------------------


def _generate_device_fingerprint() -> Dict[str, str]:
    """生成随机设备指纹。

    生成与网易 SDK 格式一致的随机设备标识:
        - sdkuid: aibg + 11 位随机字符
        - udid: 32 位随机字符
        - deviceid: amaw + 10 位随机字符 + -d
        - client_login_sn: 32 位大写十六进制
    """
    charset = "abcdefghijklmnopqrstuvwxyz0123456789"
    hex_charset = "0123456789ABCDEF"

    return {
        "sdkuid": f"aibg{''.join(secrets.choice(charset) for _ in range(11))}",
        "udid": "".join(secrets.choice(charset) for _ in range(32)),
        "deviceid": f"amaw{''.join(secrets.choice(charset) for _ in range(10))}-d",
        "client_login_sn": "".join(secrets.choice(hex_charset) for _ in range(32)),
    }


def _build_sessionid(sdkuid: str, deviceid: str) -> str:
    """构造 sessionid (retalcer 格式)。

    格式: ``1-<base64url(JSON(payload))>``

    payload 字段:
        - s: 随机 32 字符
        - odsi: 设备 ID
        - si: SHA1(s + odsi)
        - u: SDK UID
        - t: 2 (固定值)
        - g_i: "aecfrxodyqaaajp" (固定游戏 ID)
    """
    s = "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(32))
    si = hashlib.sha1(f"{s}{deviceid}".encode()).hexdigest()
    payload = {
        "s": s,
        "odsi": deviceid,
        "si": si,
        "u": sdkuid,
        "t": 2,
        "g_i": "aecfrxodyqaaajp",
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"1-{encoded}"


def _build_sauth_json(fp: Dict[str, str], sessionid: str) -> str:
    """构造完整的 sauth_json 字符串。"""
    aim_info = json.dumps(
        {"aim": "127.0.0.1", "country": "CN", "tz": "+0800", "tzid": ""},
        separators=(",", ":"),
    )
    inner = {
        "gameid": "x19",
        "login_channel": "netease",
        "app_channel": "netease",
        "platform": "pc",
        "sdkuid": fp["sdkuid"],
        "sessionid": sessionid,
        "sdk_version": SDK_VERSION_PC,
        "udid": fp["udid"],
        "deviceid": fp["deviceid"],
        "aim_info": aim_info,
        "client_login_sn": fp["client_login_sn"],
        "gas_token": "",
        "source_platform": "pc",
        "ip": "127.0.0.1",
    }
    return json.dumps(inner, separators=(",", ":"))


@router.post("/fastbuilder/guest")
async def fastbuilder_guest(
    body: FastBuilderGuestRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """FastBuilder 游客旁路 - 本地生成设备指纹并尝试获取 fbtoken。

    流程:
    1. 生成随机设备指纹 + sessionid
    2. GET /api/new 获取 secret
    3. POST /api/phoenix/login 使用 secret 认证

    需要:
    - auth_server: 认证服务器地址 (默认 fatalder.yeah114.top)
    - api_key: 认证服务器的 X-API-Key (如果需要)

    注意: 生成的 sauth_json 也可用于直接连接网易服务器 (但可能被拒绝 code=32)。
    """
    # 生成设备指纹
    fp = _generate_device_fingerprint()
    sessionid = _build_sessionid(fp["sdkuid"], fp["deviceid"])
    sauth_json = _build_sauth_json(fp, sessionid)

    # 构建请求头
    headers = {"Content-Type": "application/json"}
    if body.api_key:
        headers["X-API-Key"] = body.api_key

    auth_server = body.auth_server.rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=15.0, verify=_get_verify_ssl()) as client:
            # Step 1: GET /api/new
            resp = await client.get(f"{auth_server}/api/new", headers=headers)
            if resp.status_code == 401:
                return error_response(
                    "需要 X-API-Key",
                    "认证服务器要求 API Key。请在请求中提供 api_key 参数。",
                )
            if resp.status_code != 200:
                return error_response(
                    f"HTTP {resp.status_code}",
                    f"获取 secret 失败: {resp.text[:200]}",
                )

            secret = resp.text.strip().strip('"')

            # Step 2: POST /api/phoenix/login
            headers["Authorization"] = f"Bearer {secret}"
            phoenix_body = {
                "FBToken": json.dumps({"sauth_json": sauth_json}),
                "UserName": "",
                "Password": "",
                "ServerCode": body.server_code,
                "ServerPassword": body.server_password,
                "ClientPublicKey": "",
            }

            resp2 = await client.post(
                f"{auth_server}/api/phoenix/login",
                json=phoenix_body,
                headers=headers,
            )

            try:
                data = resp2.json()
            except Exception:
                data = {"_raw": resp2.text[:500]}

            if data.get("SuccessStates") or data.get("success"):
                # 成功获取 fbtoken
                fb_token = data.get("FBToken", "")
                chain_info = data.get("ChainInfo", [])

                # 保存 sauth_json 到账号列表
                store = get_account_store()
                existing = store.find_by_sauth(sauth_json)
                if existing:
                    record = existing
                else:
                    record = store.add({
                        "sauth_json": sauth_json,
                        "nickname": f"FB_Guest_{fp['sdkuid'][-6:]}",
                        "status": "active",
                        "last_checked": time.time(),
                        "notes": f"FastBuilder 游客旁路 (server={auth_server})",
                    })

                return success_response(
                    data={
                        "account": record,
                        "fb_token": fb_token,
                        "chain_info": chain_info,
                        "device_fingerprint": fp,
                    },
                    message="FastBuilder 游客旁路成功，已获取 fbtoken",
                )
            else:
                # 认证失败，但仍然保存 sauth_json (可用于其他方法)
                msg = data.get("message", data.get("error", "未知错误"))

                # 保存生成的 sauth_json (供用户参考)
                store = get_account_store()
                existing = store.find_by_sauth(sauth_json)
                if not existing:
                    record = store.add({
                        "sauth_json": sauth_json,
                        "nickname": f"FB_Guest_{fp['sdkuid'][-6:]}",
                        "status": "unknown",
                        "last_checked": time.time(),
                        "notes": f"FastBuilder 游客旁路失败: {msg} (server={auth_server})",
                    })
                else:
                    record = existing

                return error_response(
                    msg,
                    f"FastBuilder 认证失败: {msg}",
                    data={
                        "account": record,
                        "device_fingerprint": fp,
                        "sauth_json": sauth_json,
                        "server_response": data,
                    },
                )

    except httpx.ConnectError as e:
        return error_response(str(e), f"无法连接认证服务器: {auth_server}")
    except Exception as e:
        logger.exception("FastBuilder 游客旁路时出错")
        return error_response(str(e), "游客旁路失败")


# ---------------------------------------------------------------------------
# 注册方式 3.5 - PE端 Cookie 转换 (PC端 → PE端)
# ---------------------------------------------------------------------------
class ConvertToPERequest(BaseModel):
    """PE端 Cookie 转换请求。"""
    sauth_json: str = Field(..., description="原始 PC 端 sauth_json 字符串")
    platform: str = Field("android", description="目标平台: android 或 ios")


@router.post("/convert-to-pe")
async def convert_to_pe(
    body: ConvertToPERequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """将 PC 端 cookie 转换为 PE 端 (手机版) cookie。

    修改 sauth_json 中的以下字段:
    - platform: pc → android/ios
    - sdk_version: → 5.2.0 (PE SDK 版本)
    - source_platform: pc → android/ios

    注意:
    - gameid 保持 "x19" 不变 (这是网易我的世界游戏ID, PC/PE通用)
    - 转换后的 cookie 保留原始 sessionid (NetEase 会话不变)
    - sessionid 内部的 g_i 字段已经是 "aecfrxodyqaaajp" (PE 游戏实例 ID)
    - 如果输入已经是 PE 格式, 会返回提示但不重复转换
    """
    platform = body.platform.strip().lower()
    if platform not in ("android", "ios"):
        return error_response(
            "platform 必须是 android 或 ios",
            "不支持的平台",
        )

    try:
        raw = body.sauth_json.strip()
        # 解析双层 JSON
        outer = json.loads(raw)
        inner_str = outer.get("sauth_json", "")
        if not inner_str:
            # 可能直接就是内层 JSON
            inner_str = raw
            outer = {"sauth_json": raw}
        inner = json.loads(inner_str)

        # 记录原始平台
        orig_platform = inner.get("platform", "?")
        orig_sdk = inner.get("sdk_version", "?")

        # 修改字段
        inner["platform"] = platform
        inner["sdk_version"] = SDK_VERSION_PE  # "5.2.0"
        inner["source_platform"] = platform

        # 重新编码
        new_inner_str = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
        new_outer = {"sauth_json": new_inner_str}
        new_sauth = json.dumps(new_outer, ensure_ascii=False, separators=(",", ":"))

        # 保存到账号列表
        store = get_account_store()
        existing = store.find_by_sauth(new_sauth)
        if existing:
            record = existing
        else:
            record = store.add({
                "sauth_json": new_sauth,
                "nickname": f"PE_{inner.get('sdkuid', '?')[-6:]}",
                "status": "active",
                "last_checked": time.time(),
                "notes": f"PE端转换 (platform={platform}, 原始={orig_platform}/{orig_sdk})",
            })

        return success_response(
            data={
                "account": record,
                "original_sauth_json": raw,
                "converted_sauth_json": new_sauth,
                "original_platform": orig_platform,
                "new_platform": platform,
                "original_sdk_version": orig_sdk,
                "new_sdk_version": SDK_VERSION_PE,
                "sdkuid": inner.get("sdkuid", ""),
                "gameid": inner.get("gameid", ""),
            },
            message=f"已将 PC 端 cookie 转换为 PE 端 ({platform})",
        )
    except json.JSONDecodeError as e:
        return error_response(str(e), "sauth_json 格式错误,不是合法 JSON")
    except Exception as e:
        logger.exception("PE端转换时出错")
        return error_response(str(e), "转换失败")


class DetectPlatformRequest(BaseModel):
    """Cookie 平台检测请求。"""
    sauth_json: str = Field(..., description="sauth_json 字符串")


@router.post("/detect-platform")
async def detect_platform(
    body: DetectPlatformRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """自动检测 sauth_json 是 PC 端还是 PE 端。

    根据 platform 字段判断:
    - platform=pc → PC 端
    - platform=android/ios → PE 端 (手机版)
    - 其他 → 未知
    """
    try:
        raw = body.sauth_json.strip()
        outer = json.loads(raw)
        inner_str = outer.get("sauth_json", "")
        if not inner_str:
            inner_str = raw
        inner = json.loads(inner_str)

        platform = inner.get("platform", "")
        sdk_version = inner.get("sdk_version", "")
        gameid = inner.get("gameid", "")
        sdkuid = inner.get("sdkuid", "")

        if platform == "pc":
            detected = "pc"
            label = "PC 端 (电脑版)"
        elif platform in ("android", "ios"):
            detected = "pe"
            label = f"PE 端 (手机版 - {platform})"
        else:
            detected = "unknown"
            label = f"未知 (platform={platform})"

        return success_response(
            data={
                "platform": platform,
                "detected_type": detected,
                "label": label,
                "sdk_version": sdk_version,
                "gameid": gameid,
                "sdkuid": sdkuid,
                "is_pc": detected == "pc",
                "is_pe": detected == "pe",
            },
            message=f"检测到: {label}",
        )
    except json.JSONDecodeError as e:
        return error_response(str(e), "sauth_json 格式错误")
    except Exception as e:
        logger.exception("平台检测时出错")
        return error_response(str(e), "检测失败")


# ---------------------------------------------------------------------------
# 注册方式 4 - MPay 手机号登录 (网易官方 API, 免费)
# ---------------------------------------------------------------------------
class MPaySendSMSRequest(BaseModel):
    """发送短信验证码请求。"""
    phone: str = Field(..., description="手机号 (11位)")


class MPayVerifyRequest(BaseModel):
    """验证短信请求。"""
    phone: str = Field(..., description="手机号")
    code: str = Field("", description="下行短信验证码 (normal 模式)")
    up_content: str = Field("", description="上行短信内容 (upstream 模式, 通常为 '手机登录')")


@router.post("/mpay/device")
async def mpay_register_device(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """注册 MPay 设备 (自动, 不需要用户输入)。

    返回 device_id, 后续发送短信需要此 ID。
    """
    try:
        device_id = await _mpay_session.register_device()
        return success_response(
            data={"device_id": device_id},
            message="设备注册成功",
        )
    except Exception as e:
        logger.exception("MPay 设备注册失败")
        return error_response(str(e), "设备注册失败")


@router.post("/mpay/send-sms")
async def mpay_send_sms(
    req: MPaySendSMSRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """发送短信验证码。

    两种模式:
        - normal: 服务器发送验证码到用户手机 (免费)
        - upstream: 用户发送短信到指定号码 (运营商收取标准短信费)

    如果设备未注册, 会自动先注册设备。
    """
    phone = req.phone.strip()
    if not phone or len(phone) != 11:
        return error_response("手机号格式不正确", "请输入11位手机号")

    try:
        # 自动注册设备 (如果尚未注册)
        if not _mpay_session.device_id:
            await _mpay_session.register_device()

        result = await _mpay_session.send_sms(phone)

        if result["mode"] == "normal":
            return success_response(
                data={"mode": "normal"},
                message="验证码已发送到您的手机, 请查收短信并输入验证码",
            )
        else:
            return success_response(
                data={
                    "mode": "upstream",
                    "content": result.get("content", ""),
                    "number": result.get("number", ""),
                    "tips": result.get("tips", ""),
                },
                message=result.get("message", "请发送上行短信完成验证"),
            )
    except RuntimeError as e:
        return error_response(str(e), "发送短信失败")
    except Exception as e:
        logger.exception("MPay 发送短信失败")
        return error_response(str(e), "发送短信失败")


@router.post("/mpay/verify")
async def mpay_verify_sms(
    req: MPayVerifyRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """验证短信验证码, 完成登录并获取 sauth_json。

    normal 模式: 提供收到的验证码 (code 字段)
    upstream 模式: 提供 up_content 字段 (通常为 '手机登录')
    """
    phone = req.phone.strip()
    if not phone or len(phone) != 11:
        return error_response("手机号格式不正确", "请输入11位手机号")

    try:
        # 验证短信
        ticket = await _mpay_session.verify_sms(
            phone,
            code=req.code.strip(),
            up_content=req.up_content.strip() or _mpay_session.upstream_content,
        )

        # 完成登录
        user_data = await _mpay_session.finish(phone, ticket)

        # 构建 sauth_json
        sauth_str = _mpay_session.build_sauth(
            user_data["user_id"],
            user_data["token"],
        )
        outer_sauth = json.dumps({"sauth_json": sauth_str})

        # 保存到账号库
        store = get_account_store()
        nickname = user_data.get("nickname", "") or f"MPay_{phone[-4:]}"
        record = store.add({
            "sauth_json": outer_sauth,
            "nickname": nickname,
            "uid": "",
            "status": "unknown",
            "notes": f"MPay 手机号登录 ({phone[:3]}****{phone[-4:]})",
        })

        logger.info(f"MPay 登录成功: user_id={user_data['user_id']}, account={record['account_id']}")

        # 重置会话
        _mpay_session.reset()

        return success_response(
            data={
                "account": record,
                "user_id": user_data["user_id"],
                "nickname": nickname,
            },
            message="登录成功! 账号已添加, 请点击检测确认是否可用",
        )
    except RuntimeError as e:
        return error_response(str(e), "验证失败")
    except Exception as e:
        logger.exception("MPay 验证失败")
        return error_response(str(e), "验证失败")


# ---------------------------------------------------------------------------
# 注册方式 5 - Fever Token 转 SAuth (逆向自 NEMCTOOLS)
# 将 MPay/Fever Token (sdkuid + sessionid + deviceid) 转换为 netease 频道 sauth_json
# 来源: NEMCTOOLS/查UID源码(1.3.8)/FeverToSauth/FeverAuth.cs
# ---------------------------------------------------------------------------
class FeverConvertRequest(BaseModel):
    """Fever Token 转换请求。"""
    sdkuid: str = Field(..., description="MPay 用户 ID")
    sessionid: str = Field(..., description="MPay 会话 Token (非 4399pc token)")
    deviceid: str = Field(..., description="MPay 设备 ID")
    auto_add: bool = Field(True, description="转换成功后自动添加到账号列表")


@router.post("/fever/convert")
async def fever_convert(
    body: FeverConvertRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """将 Fever/MPay Token 转换为 netease 频道 sauth_json。

    算法流程 (逆向自 NEMCTOOLS FeverAuth.cs):
      1. POST mkey.163.com/mpay/api/users/create_ticket → 获取 ticket
      2. POST mkey.163.com/mpay/api/users/login/ticket → 用 ticket 换取新 sessionid
      3. 构建 netease 频道 sauth_json

    注意:
        - sessionid 必须是 MPay token (非 4399pc 的 session token)
        - 从 FormLogin.exe 获取的 token 是有效的 MPay token
        - 4399pc cookies 中的 sessionid 不是 MPay token, 无法转换
    """
    from ..auth.netease_direct.fever_to_sauth import fever_to_sauth

    result = await fever_to_sauth(
        sdkuid=body.sdkuid,
        sessionid=body.sessionid,
        deviceid=body.deviceid,
        use_random_udid=True,
    )

    if not result["success"]:
        return error_response(result["message"], "Fever Token 转换失败")

    # 可选: 用 login-otp 验证生成的 cookie
    from ..auth.netease_direct.client import NeteaseDirectClient
    async with NeteaseDirectClient(mode="pc") as client:
        check = await client.check_session(result["sauth_json"])

    account_status = "active" if check["valid"] else "unknown"

    record = None
    if body.auto_add:
        store = get_account_store()
        existing = store.find_by_sauth(result["sauth_json"])
        if existing:
            record = existing
        else:
            record = store.add({
                "sauth_json": result["sauth_json"],
                "nickname": f"Fever_{body.sdkuid[:8]}",
                "status": account_status,
                "last_checked": time.time(),
                "notes": f"FeverToSauth 转换 (sdkuid={body.sdkuid})",
            })

    return success_response(
        data={
            "account": record,
            # Bug 1.2 修复: 使用 .get() 防止 result/check 缺少键时抛 KeyError
            "sauth_json": result.get("sauth_json", ""),
            "session_valid": check.get("valid", False),
            "check_code": check.get("code", -1),
            "aid": check.get("aid", ""),
            "details": {
                "new_sessionid": result.get("details", {}).get("new_sessionid", "")[:20] + "...",
                "src_client_ip": result.get("details", {}).get("src_client_ip", ""),
                "src_sdk_version": result.get("details", {}).get("src_sdk_version", ""),
            },
        },
        message=f"转换成功, session {'有效' if check['valid'] else '无效'}",
    )


# ---------------------------------------------------------------------------
# 注册方式 6 - 4399 账号密码登录 (逆向自 CYXHSJ + Drug.NetEase)
# 用 4399 用户名密码登录, 获取 SDK token, 生成 sauth_json
# 来源: CYXHSJ.exe + Drug.NetEase/x19Auth.cs (Pt4399Login)
# ---------------------------------------------------------------------------
class Login4399Request(BaseModel):
    """4399 登录请求。"""
    username: str = Field(..., description="4399 用户名")
    password: str = Field(..., description="4399 密码")
    captcha_answer: str = Field("", description="验证码答案 (如果需要)")
    captcha_id: str = Field("", description="验证码 ID (来自 /login4399/captcha)")
    convert_to_netease: bool = Field(
        False,
        description="是否用 FeverToSauth 转换为 netease 频道 (需要 MPay token, 实验性)",
    )


@router.get("/login4399/captcha")
async def login4399_captcha(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取 4399 登录验证码图片 (JSON 格式, base64 编码)。"""
    import base64

    from ..auth.netease_direct.login_4399 import generate_captcha_id, fetch_captcha

    captcha_id = generate_captcha_id()
    image_data = await fetch_captcha(captcha_id)

    return success_response(
        data={
            "image": base64.b64encode(image_data).decode("ascii"),
            "id": captcha_id,
        },
        message="验证码获取成功",
    )


@router.post("/login4399")
async def login4399(
    body: Login4399Request,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """用 4399 账号密码登录, 获取 sauth_json。

    流程 (逆向自 CYXHSJ.exe + Drug.NetEase):
      1. 验证用户名 → ptlogin.4399.com/ptlogin/verify.do
      2. 登录 → ptlogin.4399.com/ptlogin/login.do
      3. 获取 SDK token → microgame.5054399.net/v2/service/sdk/info
      4. 构建 sauth_json (4399pc 频道)

    注意:
        - 生成的 cookie 默认为 4399pc 频道 (可能已失效, 返回 code=32)
        - 设置 convert_to_netease=true 可尝试用 FeverToSauth 转换为 netease 频道
        - FeverToSauth 需要 MPay token, 4399pc token 可能无法转换
    """
    from ..auth.netease_direct.login_4399 import login_4399_to_sauth

    result = await login_4399_to_sauth(
        username=body.username,
        password=body.password,
        captcha_answer=body.captcha_answer,
        captcha_id=body.captcha_id,
    )

    if not result["success"]:
        if result.get("need_captcha"):
            return error_response(
                "需要验证码",
                "请先获取验证码图片 (GET /api/accounts/login4399/captcha) 并输入答案",
                data={"need_captcha": True, "captcha_id": result.get("captcha_id", "")},
            )
        return error_response(result["message"], "4399 登录失败")

    sauth_json = result["sauth_json"]

    # 可选: 尝试用 FeverToSauth 转换为 netease 频道
    if body.convert_to_netease:
        from ..auth.netease_direct.fever_to_sauth import fever_to_sauth
        data = result.get("data", {})
        fever_result = await fever_to_sauth(
            sdkuid=data.get("sdkuid", ""),
            sessionid=data.get("token", ""),
            deviceid=data.get("deviceid", ""),
        )
        if fever_result["success"]:
            sauth_json = fever_result["sauth_json"]

    # 验证并保存
    from ..auth.netease_direct.client import NeteaseDirectClient
    async with NeteaseDirectClient(mode="pc") as client:
        check = await client.check_session(sauth_json)

    store = get_account_store()
    existing = store.find_by_sauth(sauth_json)
    if existing:
        record = existing
    else:
        record = store.add({
            "sauth_json": sauth_json,
            "nickname": f"4399_{body.username}",
            "status": "active" if check["valid"] else "unknown",
            "last_checked": time.time(),
            "notes": f"4399 账号登录 (username={body.username})",
        })

    return success_response(
        data={
            "account": record,
            "session_valid": check["valid"],
            "check_code": check["code"],
            "channel": "netease" if body.convert_to_netease else "4399pc",
        },
        message=f"4399 登录成功, session {'有效' if check['valid'] else '无效'}"
        + (" (已转换为 netease 频道)" if body.convert_to_netease else " (4399pc 频道)"),
    )


# ---------------------------------------------------------------------------
# 4399 OAuth2 登录 (NovaBuilder 方案, 4399com 频道)
# ---------------------------------------------------------------------------

class Login4399OAuth2Request(BaseModel):
    """4399 OAuth2 登录请求 (WPFLauncher_Hook 方案)。"""
    username: str = Field(..., description="4399 用户名")
    password: str = Field(..., description="4399 密码")
    captcha_answer: str = Field("", description="验证码答案 (空则自动 OCR)")
    captcha_id: str = Field("", description="验证码 ID (来自 /login4399/oauth2/captcha)")


@router.get("/login4399/oauth2/captcha")
async def login4399_oauth2_captcha(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取 4399 OAuth2 登录验证码图片 (JSON 格式, base64 编码)。

    使用 OAuth2 流程 (loginAndAuthorize.do) 的验证码端点。
    captchaId 格式: 32位大写hex (UUID4)。
    """
    import base64

    from ..auth.netease_direct.login_4399_oauth2 import (
        generate_captcha_id,
        fetch_captcha_image,
    )

    captcha_id = generate_captcha_id()
    image_data = await fetch_captcha_image(captcha_id)

    return success_response(
        data={
            "image": base64.b64encode(image_data).decode("ascii"),
            "id": captcha_id,
        },
        message="验证码获取成功",
    )


@router.post("/login4399/oauth2")
async def login4399_oauth2_endpoint(
    body: Login4399OAuth2Request,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """用 4399 OAuth2 流程登录 (WPFLauncher_Hook 方案, 4399com 频道)。

    逆向自 WPFLauncher_Hook 的 _4399.LoginAsync:
      1. 获取验证码 → captcha.do
      2. 获取 OAuth 参数 → oauth-callback.html
      3. 登录并授权 → loginAndAuthorize.do (不跟随重定向)
      4. OAuth 回调 → GET Location URL, 获取 uid 和 state
      5. 构建 sauth_json (4399com 频道)

    此流程完全绕过失效的 checkKidLoginUserCookie.do 和 sdk/info 端点。
    如果 captcha_answer 为空, 后端会自动 OCR (最多5次重试)。
    """
    from ..auth.netease_direct.login_4399_oauth2 import login_4399_oauth2

    result = await login_4399_oauth2(
        username=body.username,
        password=body.password,
        captcha_answer=body.captcha_answer or None,
        captcha_id=body.captcha_id or None,
    )

    if not result:
        return error_response(
            "登录失败",
            "4399 OAuth2 认证失败，请检查账号密码或刷新验证码重试",
        )

    sauth_json = result.sauth_json

    # 验证并保存
    from ..auth.netease_direct.client import NeteaseDirectClient
    async with NeteaseDirectClient(mode="pc") as client:
        check = await client.check_session(sauth_json)

    store = get_account_store()
    existing = store.find_by_sauth(sauth_json)
    if existing:
        record = existing
    else:
        record = store.add({
            "sauth_json": sauth_json,
            "nickname": f"4399com_{body.username}",
            "status": "active" if check["valid"] else "unknown",
            "last_checked": time.time(),
            "notes": (
                f"4399 OAuth2 登录 (username={body.username}, "
                f"channel=4399com, uid={result.uid})"
            ),
        })

    return success_response(
        data={
            "account": record,
            "session_valid": check["valid"],
            "check_code": check["code"],
            "channel": "4399com",
            "uid": result.uid,
            "auth_method": "oauth2",
        },
        message=(
            f"4399 OAuth2 登录成功 (4399com 频道), "
            f"session {'有效' if check['valid'] else '无效'}"
        ),
    )


@router.get("/launcher/extract")
async def extract_launcher_cookies(
    custom_path: Optional[str] = None,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """从本地网易 Minecraft 启动器配置文件提取 sauth_json。

    扫描启动器配置目录, 自动识别并提取已登录账号的凭证。
    适用于用户已在本地登录过网易我的世界启动器的场景。

    Query Parameters:
        custom_path: 自定义启动器配置路径 (可选)

    Returns:
        提取到的凭证列表
    """
    try:
        from ..auth.netease_direct.cookie_extractor import CookieExtractor

        extractor = CookieExtractor(custom_path=custom_path)
        creds = extractor.extract_all()

        if not creds:
            return success_response(
                data={"credentials": [], "found_paths": []},
                message="未找到启动器凭证, 请确保已登录网易我的世界启动器",
            )

        # 验证每个凭证的 login-otp 状态
        from ..auth.netease_direct import NeteaseDirectClient

        verified = []
        for cred in creds:
            item = cred.to_account_dict()
            # 异步验证 login-otp
            try:
                async with NeteaseDirectClient(mode="pc", timeout=30.0) as client:
                    resp = await client._post_login_otp(cred.to_wrapped())
                    code = resp.get("code", -1)
                    item["login_otp_code"] = code
                    if code == 0:
                        entity = resp.get("entity") or {}
                        item["aid"] = str(entity.get("aid", ""))
                        item["verified"] = True
                    else:
                        item["verified"] = False
                        item["verify_message"] = resp.get("message", f"code={code}")
            except Exception as e:
                item["verified"] = False
                item["verify_message"] = str(e)[:200]

            verified.append(item)

        return success_response(
            data={
                "credentials": verified,
                "found_paths": [str(p) for p in extractor.get_found_paths()],
            },
            message=f"找到 {len(verified)} 个凭证",
        )
    except Exception as exc:
        logger.exception("提取启动器凭证时出错")
        return error_response(str(exc)[:200], "提取启动器凭证失败")


@router.post("/launcher/import")
async def import_launcher_cookies(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """从启动器提取凭证并自动导入到账号库。

    自动调用 /launcher/extract, 将有效的凭证添加到 accounts.json。
    """
    try:
        from ..auth.netease_direct.cookie_extractor import CookieExtractor

        extractor = CookieExtractor()
        creds = extractor.extract_all()

        if not creds:
            return error_response("no_credentials", "未找到启动器凭证")

        store = get_account_store()
        added = 0
        skipped = 0

        for cred in creds:
            if not cred.is_valid():
                skipped += 1
                continue

            sauth_wrapped = cred.to_wrapped()

            # 检查是否已存在
            if store.find_by_sauth(sauth_wrapped):
                skipped += 1
                continue

            # 添加账号
            store.add({
                "sauth_json": sauth_wrapped,
                "nickname": cred.player_name or f"launcher_{cred.sdkuid[:8]}",
                "uid": cred.uid,
                "source": "launcher_extract",
                "status": "active",
                "created_at": int(time.time()),
            })
            added += 1

        return success_response(
            data={"added": added, "skipped": skipped, "total_found": len(creds)},
            message=f"导入 {added} 个账号, 跳过 {skipped} 个已存在",
        )
    except Exception as exc:
        logger.exception("导入启动器凭证时出错")
        return error_response(str(exc)[:200], "导入启动器凭证失败")


# 请求模型
class AutoRegisterRequest(BaseModel):
    """自动注册请求。"""
    username: Optional[str] = Field(None, description="指定用户名 (可选)")
    password: Optional[str] = Field(None, description="指定密码 (可选)")
    use_ocr: bool = Field(True, description="是否使用 OCR 自动识别验证码")


class BatchRegisterRequest(BaseModel):
    """批量注册请求。"""
    count: int = Field(1, ge=1, le=10, description="注册数量 (1-10)")
    use_ocr: bool = Field(True, description="是否使用 OCR")
    delay: float = Field(5.0, ge=1.0, le=60.0, description="每次间隔秒数")


@router.post("/register/auto")
async def auto_register_account(
    body: AutoRegisterRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """自动注册 4399 账号并获取 sauth_json。

    使用 CYXHSJ 逆向的注册流程:
      1. 自动注册 4399 账号 (随机用户名/密码/身份证)
      2. OCR 自动识别验证码 (需要 ddddocr)
      3. 获取 sauth_json (udid 随机, 每账号独立)
      4. 验证 login-otp

    注意:
        - 4399 注册流程已变, 此端点可能返回错误
        - 生成的 udid 是随机的, 与被封账号无关
        - 需要 ddddocr 库 (pip install ddddocr)
    """
    from ..auth.netease_direct.account_register import (
        AccountRegistrar, CaptchaHandler,
    )

    captcha_handler = CaptchaHandler(use_ocr=body.use_ocr)
    if body.use_ocr and not captcha_handler.use_ocr:
        return error_response(
            "OCR 不可用",
            "ddddocr 未安装, 请运行 pip install ddddocr 或使用人工模式",
        )

    try:
        async with AccountRegistrar(
            timeout=30.0, captcha_handler=captcha_handler
        ) as reg:
            result = await reg.register_and_get_sauth(
                username=body.username, password=body.password
            )

            if not result["success"]:
                return error_response(result["message"], "自动注册失败")

            # 验证 login-otp
            from ..auth.netease_direct import NeteaseDirectClient
            async with NeteaseDirectClient(mode="pc", timeout=60.0) as client:
                check = await client.check_session(result["sauth_json"])

            # 保存账号
            store = get_account_store()
            existing = store.find_by_sauth(result["sauth_json"])
            if existing:
                record = existing
            else:
                record = store.add({
                    "sauth_json": result["sauth_json"],
                    "nickname": f"auto_{result['username'][:8]}",
                    "status": "active" if check["valid"] else "unknown",
                    "last_checked": time.time(),
                    "notes": f"自动注册 (username={result['username']})",
                })

            return success_response(
                data={
                    "account": record,
                    "username": result["username"],
                    "password": result["password"],
                    "session_valid": check["valid"],
                    "check_code": check["code"],
                    "udid_independent": True,  # udid 是随机生成的, 不会连封
                },
                message=f"自动注册成功! session {'有效' if check['valid'] else '需验证'}",
            )
    except Exception as e:
        logger.exception("自动注册异常")
        return error_response(str(e)[:200], "自动注册异常")


@router.post("/register/batch")
async def batch_register_accounts(
    body: BatchRegisterRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """批量自动注册账号。

    批量调用 /register/auto, 每次间隔指定秒数。
    """
    from ..auth.netease_direct.account_register import (
        AccountRegistrar, CaptchaHandler,
    )

    captcha_handler = CaptchaHandler(use_ocr=body.use_ocr)
    if body.use_ocr and not captcha_handler.use_ocr:
        return error_response(
            "OCR 不可用",
            "ddddocr 未安装, 请运行 pip install ddddocr",
        )

    results = []
    success_count = 0
    fail_count = 0

    try:
        async with AccountRegistrar(
            timeout=30.0, captcha_handler=captcha_handler
        ) as reg:
            for i in range(body.count):
                logger.info(f"批量注册 [{i+1}/{body.count}]")
                try:
                    result = await reg.register_and_get_sauth()
                    if result["success"]:
                        # 保存账号
                        store = get_account_store()
                        existing = store.find_by_sauth(result["sauth_json"])
                        if not existing:
                            store.add({
                                "sauth_json": result["sauth_json"],
                                "nickname": f"batch_{result['username'][:8]}",
                                "status": "active",
                                "last_checked": time.time(),
                                "notes": f"批量注册 #{i+1}",
                            })
                        results.append({
                            "success": True,
                            "username": result["username"],
                            "password": result["password"],
                        })
                        success_count += 1
                    else:
                        results.append({
                            "success": False,
                            "message": result["message"],
                        })
                        fail_count += 1
                except Exception as e:
                    results.append({
                        "success": False,
                        "message": str(e)[:200],
                    })
                    fail_count += 1

                if i < body.count - 1:
                    await asyncio.sleep(body.delay)

    except Exception as e:
        return error_response(str(e)[:200], "批量注册异常")

    return success_response(
        data={
            "results": results,
            "total": body.count,
            "success": success_count,
            "failed": fail_count,
        },
        message=f"批量注册完成: 成功 {success_count}/{body.count}",
    )


@router.get("/methods")
async def list_registration_methods(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """列出所有可用的账号获取方式及其状态。"""
    methods = [
        {
            "id": "mpay",
            "name": "手机号登录 (推荐, 免费)",
            "description": "通过网易官方 MPay API 用手机号登录, 完全免费, 直接产生有效 sauth_json",
            "available": True,
            "requires": ["手机号", "短信收发能力"],
            "endpoint": "/api/accounts/mpay/send-sms",
            "notes": "网易官方 API, 免费, 支持上行/下行短信验证",
        },
        {
            "id": "fever",
            "name": "Fever Token 转换",
            "description": "将 MPay/Fever Token (sdkuid+sessionid+deviceid) 转换为 netease 频道 sauth_json",
            "available": True,
            "requires": ["MPay Token (从 FormLogin.exe 获取)"],
            "endpoint": "/api/accounts/fever/convert",
            "notes": "逆向自 NEMCTOOLS, 需要有效的 MPay token (非 4399pc token)",
        },
        {
            "id": "login4399",
            "name": "4399 账号密码登录",
            "description": "用 4399 用户名密码登录, 获取 SDK token 并生成 sauth_json",
            "available": True,
            "requires": ["4399 账号", "4399 密码", "验证码 (如需要)"],
            "endpoint": "/api/accounts/login4399",
            "notes": "逆向自 CYXHSJ+Drug.NetEase, 默认生成 4399pc 频道, 可尝试转换为 netease",
        },
        {
            "id": "sms",
            "name": "SMS 注册 (cookie.xingbai.top)",
            "description": "通过 cookie.xingbai.top 发送短信注册网易游客账号, 免费, 仅需一条短信",
            "available": True,
            "requires": ["手机短信发送能力"],
            "endpoint": "/api/accounts/register/captcha",
            "notes": "免费, 但网站可能不稳定",
        },
        {
            "id": "nethard",
            "name": "NetHard OpenAPI",
            "description": "登录 NetHard 用户中心，通过 OpenAPI 获取 SAuth (需付费套餐)",
            "available": True,
            "requires": ["NetHard 账号", "付费套餐"],
            "endpoint": "/api/accounts/nethard/login",
            "notes": "需要在 nv1.nethard.pro 注册并购买套餐",
        },
        {
            "id": "fastbuilder",
            "name": "FastBuilder 游客旁路",
            "description": "通过 FastBuilder 认证服务器获取 fbtoken (服务器可能不可用)",
            "available": False,
            "requires": ["认证服务器在线", "API Key"],
            "endpoint": "/api/accounts/fastbuilder/guest",
            "notes": "fatalder.yeah114.top 当前不可用",
        },
        {
            "id": "manual",
            "name": "手动导入",
            "description": "直接粘贴 sauth_json 添加账号",
            "available": True,
            "requires": ["已有 sauth_json"],
            "endpoint": "/api/accounts",
        },
        {
            "id": "launcher",
            "name": "启动器配置提取 (推荐, 免费且安全)",
            "description": "从本地网易我的世界启动器配置文件自动提取已登录账号的 sauth_json, "
                           "无需手动复制, udid 是真实的设备指纹 (不会连封)",
            "available": True,
            "requires": ["本地已登录网易我的世界启动器"],
            "endpoint": "/api/accounts/launcher/extract",
            "notes": "最安全的方式, udid 是真实设备指纹, 不会触发连封",
        },
        {
            "id": "auto_register",
            "name": "自动注册 4399 账号",
            "description": "自动注册 4399 账号并获取 sauth_json, udid 随机生成 (每账号独立), "
                           "OCR 自动识别验证码 (需 ddddocr)",
            "available": True,
            "requires": ["ddddocr (pip install ddddocr)", "网络访问 4399"],
            "endpoint": "/api/accounts/register/auto",
            "notes": "4399 注册流程已变, 可能返回错误。udid 随机, 不会连封",
        },
        {
            "id": "batch_register",
            "name": "批量注册账号",
            "description": "批量自动注册 4399 账号, 每次间隔指定秒数, "
                           "udid 随机生成, 避免连封",
            "available": True,
            "requires": ["ddddocr", "稳定 IP"],
            "endpoint": "/api/accounts/register/batch",
            "notes": "批量注册可能被 IP 限制, 建议配合代理使用",
        },
    ]
    return success_response(data={"methods": methods}, message=f"共 {len(methods)} 种方式")


__all__ = ["router", "get_account_store", "AccountStore"]
