"""4399 账号 sauth_json 自动刷新管理器。

功能:
    - 管理 4399 账号池 (用户名/密码), 持久化到数据库 ``sauth_accounts`` 表
    - 当机器人需要 sauth_json 时, 使用一个可用的 4399 账号通过 OAuth2 登录
      获取全新的 sauth_json
    - 缓存 sauth_json 与时间戳, 超过 2 小时自动刷新
    - 支持多账号轮询 (round-robin), 单个账号失败自动尝试下一个
    - 记录所有刷新尝试到日志

使用方式::

    from app.auth.sauth_refresh import sauth_refresher

    # 添加 4399 账号
    await sauth_refresher.add_4399_account("user1", "pass1")

    # 获取新鲜的 sauth_json (缓存有效则返回缓存, 否则刷新)
    sauth_json = await sauth_refresher.get_fresh_sauth()

    # 手动刷新
    sauth_json = await sauth_refresher.refresh_sauth("user1", "pass1")

    # 测试账号
    result = await sauth_refresher.test_4399_account("user1", "pass1")

底层登录流程复用 :class:`app.auth.netease_direct.login_4399_oauth2.Login4399OAuth2`,
sauth_json 结构由 :func:`app.auth.netease_direct.login_4399_oauth2.build_sauth_json` 构建。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from app.auth.netease_direct.login_4399_oauth2 import (
    Login4399OAuth2,
    build_sauth_json,
)

logger = logging.getLogger("pocketterm.sauth_refresh")

#: sauth_json 缓存有效期 (2 小时)。超过该时间认为凭证已过期, 需要重新登录。
SAUTH_CACHE_TTL: int = 2 * 3600

#: 账号状态: 可用
STATUS_ACTIVE: str = "active"
#: 账号状态: 失败 (登录失败被临时标记)
STATUS_FAILED: str = "failed"
#: 账号状态: 已禁用
STATUS_DISABLED: str = "disabled"


class SauthRefresher:
    """4399 账号 sauth_json 自动刷新管理器。

    管理一个 4399 账号池, 当需要 sauth_json 时使用轮询方式依次尝试登录,
    成功后缓存 sauth_json 与时间戳, 2 小时内复用缓存, 超时自动刷新。

    单例实例 :data:`sauth_refresher` 在模块导入时创建, 与
    :class:`app.auth.nv1_manager.NV1Manager` 保持一致的使用风格。
    """

    def __init__(self) -> None:
        # 内存缓存
        self._cached_sauth: str = ""
        self._cached_at: float = 0.0
        self._cached_uid: str = ""
        self._cached_username: str = ""
        # 轮询索引
        self._rr_index: int = 0
        # 刷新锁: 避免并发刷新
        self._lock = asyncio.Lock()
        # 调试信息: 记录最后一次刷新的详细过程
        self._last_refresh_debug: dict = {}

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """获取刷新状态 (内存缓存信息)。"""
        now = time.time()
        has_cache = bool(self._cached_sauth)
        age = (now - self._cached_at) if has_cache else None
        return {
            "cached": has_cache,
            "cached_at": self._cached_at if has_cache else None,
            "cached_age_seconds": age,
            "is_valid": bool(
                has_cache and (now - self._cached_at) < SAUTH_CACHE_TTL
            ),
            "cached_uid": self._cached_uid,
            "cached_username": self._cached_username,
            "cache_ttl_seconds": SAUTH_CACHE_TTL,
            "last_refresh_debug": self._last_refresh_debug,
        }

    def _is_cache_valid(self) -> bool:
        """缓存是否仍然有效 (< 2 小时)。"""
        return bool(
            self._cached_sauth
            and (time.time() - self._cached_at) < SAUTH_CACHE_TTL
        )

    # ------------------------------------------------------------------
    # 核心: 获取新鲜的 sauth_json
    # ------------------------------------------------------------------
    async def get_fresh_sauth(self) -> Optional[str]:
        """返回新鲜的 sauth_json 字符串。

        - 若缓存的 sauth_json 仍然有效 (< 2 小时), 直接返回缓存。
        - 否则使用 4399 账号池中的账号轮询登录, 获取全新 sauth_json。
        - 若所有账号均登录失败, 返回最近一次缓存的 sauth_json (可能已过期)
          作为降级, 没有缓存则返回 None。
        """
        # 快速路径: 缓存有效直接返回 (无需加锁)
        if self._is_cache_valid():
            logger.debug("sauth_json 缓存有效, 直接返回缓存")
            return self._cached_sauth

        async with self._lock:
            # 双重检查: 等锁期间可能已被其他协程刷新
            if self._is_cache_valid():
                return self._cached_sauth

            db = await self._get_db()
            accounts = await db.get_active_sauth_accounts()
            if not accounts:
                # 没有可用账号时，尝试将 failed 账号重置为 active 并重试一次
                # (disabled 账号属于主动禁用，不参与重置)
                logger.warning(
                    "没有可用的 active 4399 账号, 尝试重置 failed 账号后重试"
                )
                all_accounts = await db.list_sauth_accounts()
                reset_count = 0
                for acc in all_accounts:
                    if acc["status"] == STATUS_FAILED:
                        await db.update_sauth_account_status(
                            acc["id"], STATUS_ACTIVE
                        )
                        reset_count += 1
                if reset_count > 0:
                    logger.info(
                        f"已重置 {reset_count} 个 failed 4399 账号为 active, 重试刷新"
                    )
                    accounts = await db.get_active_sauth_accounts()
                if not accounts:
                    logger.warning("没有可用的 4399 账号, 无法刷新 sauth_json")
                    await self._log_refresh(
                        db, success=False,
                        message="刷新 sauth_json 失败: 没有可用的 4399 账号",
                        username="",
                    )
                    return self._cached_sauth or None

            # 轮询尝试每个账号
            n = len(accounts)
            start = self._rr_index % n
            for i in range(n):
                idx = (start + i) % n
                account = accounts[idx]
                # 推进轮询索引到下一个账号
                self._rr_index = (idx + 1) % n

                username = account["username"]
                password = account["password"]
                logger.info(
                    f"尝试使用 4399 账号刷新 sauth_json: {username} "
                    f"({i + 1}/{n})"
                )

                sauth_str = await self.refresh_sauth(username, password)
                if sauth_str:
                    self._cached_sauth = sauth_str
                    self._cached_at = time.time()
                    self._cached_uid = account["uid"] or ""
                    self._cached_username = username
                    logger.info(
                        f"sauth_json 刷新成功 (账号: {username}, "
                        f"uid: {self._cached_uid})"
                    )
                    return sauth_str

                # 该账号失败, 标记为 failed
                await db.update_sauth_account_status(account["id"], STATUS_FAILED)

            logger.error("所有 4399 账号登录均失败, 无法刷新 sauth_json")
            await self._log_refresh(
                db, success=False,
                message="刷新 sauth_json 失败: 所有 4399 账号登录均失败",
                username="",
            )
            # 降级: 返回过期缓存
            return self._cached_sauth or None

    # ------------------------------------------------------------------
    # 刷新: 使用指定账号登录
    # ------------------------------------------------------------------
    async def refresh_sauth(
        self, account_username: str, account_password: str
    ) -> Optional[str]:
        """使用指定 4399 账号登录, 获取全新的 sauth_json 字符串。

        优先使用非 OAuth2 流程 (可获取 MPay SDK token 用于 fever_to_sauth
        转换为 netease 频道), 失败则回退到 OAuth2 流程 (4399com 频道)。

        Args:
            account_username: 4399 用户名。
            account_password: 4399 密码。

        Returns:
            sauth_json 字符串 (JSON 序列化), 登录失败返回 None。
        """
        logger.info(f"开始刷新 sauth_json (4399 账号: {account_username})")
        db = await self._get_db()
        import time as _time
        self._last_refresh_debug = {
            "timestamp": _time.time(),
            "username": account_username,
            "mpay_flow": None,
            "oauth2_flow": None,
            "final_channel": None,
        }

        sauth_str: Optional[str] = None
        uid: str = ""

        # 优先: 非 OAuth2 流程 (获取 MPay token → fever_to_sauth → netease 频道)
        try:
            sauth_str, uid = await self._refresh_via_mpay(
                account_username, account_password
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"非 OAuth2 流程异常: {e}")
            self._last_refresh_debug["mpay_flow"] = {
                "status": "exception",
                "error": str(e),
            }

        # 回退: OAuth2 流程 (4399com 频道, 可能遇到 code=32)
        if sauth_str is None:
            logger.info(
                f"回退到 OAuth2 流程 (账号: {account_username})"
            )
            try:
                sauth_str, uid = await self._refresh_via_oauth2(
                    account_username, account_password
                )
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    f"OAuth2 流程异常 (账号: {account_username}): {e}"
                )
                self._last_refresh_debug["oauth2_flow"] = {
                    "status": "exception",
                    "error": str(e),
                }

        if sauth_str is None:
            logger.error(f"4399 登录失败 (账号: {account_username})")
            await self._log_refresh(
                db, success=False,
                message=f"4399 登录失败 (账号: {account_username})",
                username=account_username,
            )
            return None

        # 更新账号记录: uid / sauth_json / last_refresh_at, 恢复 active
        account = await db.get_sauth_account_by_username(account_username)
        if account:
            await db.update_sauth_account_refresh(
                account["id"], uid, sauth_str
            )

        await self._log_refresh(
            db, success=True,
            message=(
                f"sauth_json 刷新成功 (账号: {account_username}, "
                f"uid: {uid})"
            ),
            username=account_username,
            details={"uid": uid},
        )
        return sauth_str

    async def _refresh_via_mpay(
        self, username: str, password: str
    ) -> tuple[Optional[str], str]:
        """非 OAuth2 流程: AES 加密密码 → login.do → checkKidLoginUserCookie → sdk/info → fever_to_sauth。

        4399 的 login.do 在 sec=1 时要求密码使用 CryptoJS AES 加密
        (密钥: 'lzYW5qaXVqa', OpenSSL 格式)。

        关键修复 (参考 account_register.py 验证过的正确实现):
        - sessionId 必须设置为验证码 ID (之前为空导致登录失败)
        - captchaId 从 verify.do 响应中提取 (之前自己生成)
        - checkKidLoginUserCookie 必须带完整参数 (gameUrl/nick/onLineStart/show/isCrossDomain/retUrl)
        - sdk/info 的 queryStr 必须完整 URL 编码, 包含 nick/fcm/show/isCrossDomain/rand_time/ptusertype
        - SDK 响应中 sdk_login_data 是字符串 (token=XXX&sdkuid=YYY), 不是字典

        Returns:
            (sauth_json, uid) 或 (None, "")
        """
        import re as _re
        import os as _os
        import hashlib as _hashlib
        import base64 as _base64
        import random as _random
        import httpx
        from Crypto.Cipher import AES as _AES
        from .netease_direct.login_4399 import (
            _generate_deviceid,
            VERIFY_URL,
            CAPTCHA_URL as _4399_CAPTCHA_URL,
            LOGIN_URL as _4399_LOGIN_URL,
            CHECK_COOKIE_URL,
            SDK_INFO_URL,
        )
        from .netease_direct.login_4399_oauth2 import ocr_captcha as _ocr_captcha
        from .netease_direct.fever_to_sauth import fever_to_sauth as _fever_to_sauth

        _AES_PASSPHRASE = "lzYW5qaXVqa"

        def _cryptojs_aes_encrypt(data: str, passphrase: str) -> str:
            """CryptoJS 兼容的 AES-256-CBC 加密 (OpenSSL 格式)。"""
            salt = _os.urandom(8)
            d = b""
            d_i = b""
            while len(d) < 48:
                d_i = _hashlib.md5(
                    d_i + passphrase.encode("utf-8") + salt
                ).digest()
                d += d_i
            key, iv = d[:32], d[32:48]
            data_bytes = data.encode("utf-8")
            pad_len = 16 - (len(data_bytes) % 16)
            padded = data_bytes + bytes([pad_len] * pad_len)
            cipher = _AES.new(key, _AES.MODE_CBC, iv)
            encrypted = cipher.encrypt(padded)
            return _base64.b64encode(
                b"Salted__" + salt + encrypted
            ).decode("utf-8")

        _HEADERS = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:134.0) Gecko/20100101 Firefox/134.0",
            "Referer": "https://ptlogin.4399.com/",
            "Origin": "https://ptlogin.4399.com",
        }

        deviceid = _generate_deviceid()
        self._last_refresh_debug["mpay_flow"] = {"status": "starting"}

        try:
            async with httpx.AsyncClient(
                timeout=20, follow_redirects=True, headers=_HEADERS
            ) as client:
                # Step 1: 访问登录页面建立 session cookies
                await client.get("https://ptlogin.4399.com/ptlogin/loginFrame.do?app=kid_wdsj&redirectUrl=&displayMode=popup&layout=vertical&level=8&css=http://microgame.5054399.net/v2/resource/cssSdk/default/login.css&regLevel=8&bizId=2100001792&appId=kid_wdsj&gameId=wd&externalLogin=qq&welcomeTip=%E6%AC%A2%E8%BF%8E%E5%9B%9E%E5%88%B04399&sessionId=1&sec=1&includeFcmInfo=false&inputWidth=iptw2&postLoginHandler=default&layoutSelfAdapting=true&loginFrom=uframe&mainDivId=popup_login_div")
                self._last_refresh_debug["mpay_flow"]["step1"] = "login_page_visited"

                # Step 2: 验证用户名 + 提取 captchaId
                import time as _time_mod
                ts = int(_time_mod.time() * 1000)
                verify_url = f"{VERIFY_URL}?username={username}&appId=kid_wdsj&t={ts}&inputWidth=iptw2"
                resp = await client.get(verify_url)
                verify_text = resp.text.strip()

                # verify.do 返回 "0" 表示不需要验证码, 其他值表示需要
                need_captcha = verify_text != "0"
                self._last_refresh_debug["mpay_flow"]["verify_resp"] = verify_text[:200]

                # 从 verify.do 响应中提取 captchaId
                captcha_id = ""
                if need_captcha:
                    m = _re.search(r'captchaId=([A-Za-z0-9]+)', verify_text)
                    if m:
                        captcha_id = m.group(1)
                    if not captcha_id:
                        # 回退: 自己生成
                        captcha_id = "captchaReq" + "".join(_random.choices("0123456789", k=8))

                self._last_refresh_debug["mpay_flow"]["need_captcha"] = need_captcha
                self._last_refresh_debug["mpay_flow"]["captcha_id"] = captcha_id

                # Step 3: 获取验证码 + OCR (如果需要)
                captcha_answer = ""

                if need_captcha:
                    for attempt in range(8):
                        captcha_resp = await client.get(
                            f"{_4399_CAPTCHA_URL}?xx=1&captchaId={captcha_id}"
                        )
                        captcha_answer = await _ocr_captcha(captcha_resp.content)
                        if captcha_answer and len(captcha_answer) >= 4:
                            break
                        # 验证码识别失败, 重新获取
                        captcha_id = "captchaReq" + "".join(_random.choices("0123456789", k=8))
                        captcha_answer = ""

                self._last_refresh_debug["mpay_flow"]["captcha_answer"] = captcha_answer or False

                # Step 4: POST login.do (AES 加密密码, sec=1)
                encrypted_pwd = _cryptojs_aes_encrypt(password, _AES_PASSPHRASE)
                self._last_refresh_debug["mpay_flow"]["pwd_encrypted"] = True

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
                    "sessionId": captcha_id,  # BUG FIX: 之前为空字符串!
                    "mainDivId": "popup_login_div",
                    "includeFcmInfo": "false",
                    "level": "8",
                    "regLevel": "8",
                    "userNameLabel": "4399用户名",
                    "username": username,
                    "password": encrypted_pwd,
                    "welcomeTip": "欢迎回到4399",
                    "sec": "1",
                    "inputCaptcha": captcha_answer,
                    "reg_eula_agree": "on",
                }

                login_resp = await client.post(
                    f"{_4399_LOGIN_URL}", data=login_data,
                    follow_redirects=False,
                )
                login_text = login_resp.text
                login_status = login_resp.status_code
                login_location = login_resp.headers.get("location", "")

                self._last_refresh_debug["mpay_flow"]["login_status"] = login_status
                self._last_refresh_debug["mpay_flow"]["login_location"] = login_location[:300]
                self._last_refresh_debug["mpay_flow"]["login_text_500"] = login_text[:500]
                self._last_refresh_debug["mpay_flow"]["login_set_cookies"] = [
                    v for k, v in login_resp.headers.multi_items()
                    if k.lower() == "set-cookie"
                ][:5]
                self._last_refresh_debug["mpay_flow"]["client_cookies"] = {
                    k: v[:50] for k, v in client.cookies.items()
                }

                # 检查登录是否成功:
                # - "登录成功" in body, 或
                # - Set-Cookie 包含 Pauth (认证 cookie), 或
                # - 302 重定向
                has_pauth = any(
                    "Pauth" in v
                    for k, v in login_resp.headers.multi_items()
                    if k.lower() == "set-cookie"
                )
                login_ok = (
                    "登录成功" in login_text
                    or has_pauth
                    or login_status in (301, 302, 303, 307, 308)
                )

                if not login_ok:
                    self._last_refresh_debug["mpay_flow"]["status"] = "login_failed"
                    return None, ""

                self._last_refresh_debug["mpay_flow"]["status"] = "login_ok"

                # 提取 rand_time (尝试多种格式, 参考 account_register.py)
                rand_time = str(int(_time_mod.time() * 1000))
                for _pattern in [
                    r'"rand_time"\s*:\s*"?(\d+)"?',
                    r'parent\.timestamp\s*=\s*"(\d+)"',
                    r'timestamp\s*:\s*"(\d+)"',
                    r'rand_time=(\d+)',
                    r'"time"\s*:\s*"?(\d+)"?',
                ]:
                    rt_match = _re.search(_pattern, login_text)
                    if rt_match:
                        rand_time = rt_match.group(1)
                        break

                # Step 5: checkKidLoginUserCookie → sig/uid/time/validateState
                # 参数必须与 account_register.py 完全一致, 否则返回 "invalid request"
                from urllib.parse import quote as _url_quote
                from urllib.parse import unquote as _url_unquote
                from urllib.parse import urlparse as _url_parse
                from urllib.parse import parse_qs as _parse_qs

                _GAME_URL = "http://cdn.h5wan.4399sj.com/microterminal-h5-frame"
                _RET_URL = (
                    "http://ptlogin.4399.com/resource/ucenter.html"
                    "?action=login&appId=kid_wdsj&loginLevel=8"
                    "&regLevel=8&bizId=2100001792&externalLogin=qq"
                    "&qrLogin=true&layout=vertical&level=101"
                    "&css=http://microgame.5054399.net/v2/resource/cssSdk/default/login.css"
                    "&v=2018_11_26_16&postLoginHandler=redirect"
                    "&checkLoginUserCookie=true"
                    "&redirectUrl=http://cdn.h5wan.4399sj.com/microterminal-h5-frame?game_id=500352"
                    f"&rand_time={rand_time}"
                )
                check_url = (
                    f"{CHECK_COOKIE_URL}?appId=kid_wdsj"
                    f"&gameUrl={_GAME_URL}?game_id=500352&rand_time={rand_time}"
                    f"&nick=null&onLineStart=false&show=1&isCrossDomain=1"
                    f"&retUrl={_url_quote(_RET_URL, safe='')}"
                )

                resp2 = await client.get(check_url, follow_redirects=False)

                self._last_refresh_debug["mpay_flow"]["check_status"] = resp2.status_code
                self._last_refresh_debug["mpay_flow"]["check_location"] = resp2.headers.get("location", "")[:500]

                if resp2.status_code != 302:
                    self._last_refresh_debug["mpay_flow"]["check_body_500"] = resp2.text[:500]
                    self._last_refresh_debug["mpay_flow"]["status"] = "check_not_302"
                    return None, ""

                redirect_url = resp2.headers.get("location", "")
                self._last_refresh_debug["mpay_flow"]["check_redirect_3000"] = redirect_url[:3000]

                # 从重定向 URL 查询参数中提取 sig/uid/time/validateState
                parsed = _url_parse(redirect_url)
                qs = _parse_qs(parsed.query)
                sig = qs.get("sig", [""])[0]
                ck_uid = qs.get("uid", [""])[0]
                login_time = qs.get("time", [""])[0]
                validate_state = qs.get("validateState", [""])[0]

                if not sig:
                    self._last_refresh_debug["mpay_flow"]["status"] = "no_sig"
                    return None, ""

                self._last_refresh_debug["mpay_flow"]["check_cookie"] = "ok"
                self._last_refresh_debug["mpay_flow"]["uid"] = ck_uid

                # Step 6: sdk/info → MPay SDK token
                # queryStr 必须完整 URL 编码, 且包含 nick/fcm/show/isCrossDomain/rand_time/ptusertype
                query_str = _url_quote(
                    f"game_id=500352&nick=null&sig={sig}"
                    f"&uid={ck_uid}&fcm=0&show=1&isCrossDomain=1"
                    f"&rand_time={rand_time}&ptusertype=4399"
                    f"&time={login_time}&validateState={validate_state}"
                    f"&username={username}",
                    safe="",
                )
                sdk_url = (
                    f"{SDK_INFO_URL}?callback="
                    f"&queryStr={query_str}"
                    f"&_={int(_time_mod.time() * 1000)}"
                )
                resp3 = await client.get(sdk_url)

                # 解析 SDK 响应 (account_register.py 格式):
                # JSON 结构: {"data": {"sdk_login_data": "token=XXX&sdkuid=YYY&..."}}
                try:
                    sdk_data = resp3.json()
                    sdk_login_data = sdk_data.get("data", {}).get("sdk_login_data", "")
                except Exception:
                    sdk_login_data = ""

                # 回退: 从响应文本中正则提取
                if not sdk_login_data:
                    m = _re.search(r"sdk_login_data[=:]([^&\"<]+)", resp3.text)
                    if m:
                        sdk_login_data = _url_unquote(m.group(1))

                self._last_refresh_debug["mpay_flow"]["sdk_login_data_200"] = sdk_login_data[:200]

                # 从 sdk_login_data 字符串中提取 token 和 sdkuid
                # 格式: token=XXX&sdkuid=YYY&...
                m_token = _re.search(r"token=([^&]+)", sdk_login_data)
                mpay_token = m_token.group(1) if m_token else ""

                m_sdkuid = _re.search(r"sdkuid=([^&]+)", sdk_login_data)
                mpay_sdkuid = m_sdkuid.group(1) if m_sdkuid else ck_uid

                self._last_refresh_debug["mpay_flow"]["mpay_token_len"] = len(mpay_token)
                self._last_refresh_debug["mpay_flow"]["mpay_sdkuid"] = mpay_sdkuid

        except Exception as e:
            self._last_refresh_debug["mpay_flow"]["status"] = "exception"
            self._last_refresh_debug["mpay_flow"]["error"] = str(e)
            logger.warning(f"非 OAuth2 流程异常: {e}")
            return None, ""

        if not mpay_token:
            self._last_refresh_debug["mpay_flow"]["mpay_token"] = "empty"
            logger.warning("未获取到 MPay token")
            return None, ""

        # Step 7: fever_to_sauth → netease 频道
        self._last_refresh_debug["mpay_flow"]["deviceid"] = deviceid

        try:
            convert_result = await _fever_to_sauth(
                sdkuid=mpay_sdkuid,
                sessionid=mpay_token,
                deviceid=deviceid,
            )
            if convert_result.get("success"):
                logger.info(
                    f"fever_to_sauth 转换成功, "
                    f"已切换到 netease 频道 (账号: {username})"
                )
                self._last_refresh_debug["mpay_flow"]["fever_to_sauth"] = "success"
                self._last_refresh_debug["final_channel"] = "netease"
                return convert_result["sauth_json"], mpay_sdkuid
            else:
                logger.warning(
                    f"fever_to_sauth 转换失败: "
                    f"{convert_result.get('message', '')}"
                )
                self._last_refresh_debug["mpay_flow"]["fever_to_sauth"] = "failed"
                self._last_refresh_debug["mpay_flow"]["fever_error"] = convert_result.get("message", "")
                return None, ""
        except Exception as convert_err:
            logger.warning(f"fever_to_sauth 异常: {convert_err}")
            self._last_refresh_debug["mpay_flow"]["fever_to_sauth"] = "exception"
            self._last_refresh_debug["mpay_flow"]["fever_error"] = str(convert_err)
            return None, ""

    async def _refresh_via_oauth2(
        self, username: str, password: str
    ) -> tuple[Optional[str], str]:
        """OAuth2 流程 (回退方案): 构建 4399com 频道 sauth_json。

        注意: 4399com 频道在 /login-otp 认证时可能返回 code=32。

        Returns:
            (sauth_json, uid) 或 (None, "")
        """
        client = Login4399OAuth2()
        try:
            result = await client.login(username, password)
            if result is None:
                self._last_refresh_debug["oauth2_flow"] = {
                    "status": "login_failed",
                }
                return None, ""

            sauth_dict = build_sauth_json(result.uid, result.sessionid)
            sauth_str = json.dumps(
                {"sauth_json": json.dumps(sauth_dict, ensure_ascii=False)},
                ensure_ascii=False,
            )
            if not self._last_refresh_debug.get("final_channel"):
                self._last_refresh_debug["final_channel"] = "4399com"
            self._last_refresh_debug["oauth2_flow"] = {
                "status": "success",
                "uid": result.uid,
            }
            return sauth_str, result.uid
        except Exception as e:
            self._last_refresh_debug["oauth2_flow"] = {
                "status": "exception",
                "error": str(e),
            }
            raise
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 账号池管理
    # ------------------------------------------------------------------
    async def add_4399_account(self, username: str, password: str) -> bool:
        """添加一个 4399 账号到账号池。

        若用户名已存在则更新密码并恢复为 active 状态。

        Returns:
            True 表示新增, False 表示更新了已存在的账号。
        """
        db = await self._get_db()
        existing = await db.get_sauth_account_by_username(username)
        if existing:
            await db.update_sauth_account_password(existing["id"], password)
            await db.update_sauth_account_status(existing["id"], STATUS_ACTIVE)
            logger.info(f"4399 账号已存在, 已更新密码: {username}")
            return False

        account_id = f"sa_{uuid.uuid4().hex[:12]}"
        await db.add_sauth_account(account_id, username, password)
        logger.info(f"已添加 4399 账号: {username} (id={account_id})")
        return True

    async def list_4399_accounts(self) -> list:
        """列出所有存储的 4399 账号 (不含密码)。"""
        db = await self._get_db()
        rows = await db.list_sauth_accounts()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "uid": r["uid"],
                "status": r["status"],
                "last_refresh_at": r["last_refresh_at"],
                "has_sauth": bool(r["sauth_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def delete_4399_account(self, account_id: str) -> bool:
        """删除指定 4399 账号。"""
        db = await self._get_db()
        success = await db.delete_sauth_account(account_id)
        if success:
            logger.info(f"已删除 4399 账号 (id={account_id})")
        return success

    async def get_4399_account(self, account_id: str) -> Optional[dict]:
        """获取单个 4399 账号 (含密码, 供测试使用)。"""
        db = await self._get_db()
        row = await db.get_sauth_account(account_id)
        if row is None:
            return None
        return dict(row)

    async def test_4399_account(
        self, username: str, password: str
    ) -> dict:
        """测试 4399 账号能否登录。

        Returns:
            ``{"success": bool, "uid": str, "message": str}``
        """
        logger.info(f"测试 4399 账号登录: {username}")
        client = Login4399OAuth2()
        try:
            result = await client.login(username, password)
            if result is None:
                return {
                    "success": False,
                    "uid": "",
                    "message": "登录失败 (账号/密码错误或验证码识别失败)",
                }
            return {
                "success": True,
                "uid": result.uid,
                "message": "登录成功",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception(f"测试 4399 账号异常: {username}")
            return {
                "success": False,
                "uid": "",
                "message": f"登录异常: {e}",
            }
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    async def _get_db(self):
        """惰性获取数据库单例。"""
        from app.database import get_db
        return await get_db()

    async def _log_refresh(
        self,
        db,
        *,
        success: bool,
        message: str,
        username: str = "",
        details: Optional[dict] = None,
    ) -> None:
        """记录刷新日志到数据库。"""
        try:
            await db.add_log(
                target_type="system",
                target_id="sauth_refresh",
                level="success" if success else "error",
                message=message,
                details=json.dumps(
                    {"username": username, **(details or {})},
                    ensure_ascii=False,
                ),
                created_by="system",
            )
        except Exception:  # noqa: BLE001
            logger.debug("记录 sauth_refresh 日志失败", exc_info=True)


#: 全局单例 (与 nv1_manager 风格一致)
sauth_refresher: SauthRefresher = SauthRefresher()


__all__ = [
    "SauthRefresher",
    "sauth_refresher",
    "SAUTH_CACHE_TTL",
]
