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
        """非 OAuth2 流程: 持久化客户端 + 预设 cookies → login.do → sdk/info → fever_to_sauth。

        使用单一 httpx.AsyncClient 保持所有 cookies, 先访问登录页面建立 session,
        再 POST login.do, 避免 "请稍后再试~" 限流。

        Returns:
            (sauth_json, uid) 或 (None, "")
        """
        import re as _re
        import httpx
        from .netease_direct.login_4399 import (
            _generate_deviceid,
            VERIFY_URL,
            CAPTCHA_URL as _4399_CAPTCHA_URL,
            LOGIN_URL as _4399_LOGIN_URL,
            CHECK_COOKIE_URL,
            SDK_INFO_URL,
            _SDK_QUERY,
        )
        from .netease_direct.login_4399_oauth2 import ocr_captcha as _ocr_captcha
        from .netease_direct.fever_to_sauth import fever_to_sauth as _fever_to_sauth

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

                # Step 2: 验证用户名
                import time as _time_mod
                ts = int(_time_mod.time() * 1000)
                verify_url = f"{VERIFY_URL}?username={username}&appId=kid_wdsj&t={ts}&inputWidth=iptw2&v=1"
                resp = await client.get(verify_url)
                verify_text = resp.text
                need_captcha = "captchaId" in verify_text or '"code":0' not in verify_text
                self._last_refresh_debug["mpay_flow"]["need_captcha"] = need_captcha

                # Step 3: 获取验证码 + OCR (如果需要)
                captcha_answer = ""
                captcha_id = "captchaReq" + "".join(__import__("random").choices("0123456789", k=8))

                if need_captcha:
                    for attempt in range(8):
                        captcha_resp = await client.get(
                            f"{_4399_CAPTCHA_URL}?xx=1&captchaId={captcha_id}"
                        )
                        captcha_answer = await _ocr_captcha(captcha_resp.content)
                        if captcha_answer and len(captcha_answer) >= 4:
                            break
                        captcha_id = "captchaReq" + "".join(__import__("random").choices("0123456789", k=8))
                        captcha_answer = ""

                self._last_refresh_debug["mpay_flow"]["captcha_answer"] = bool(captcha_answer)

                # Step 4: POST login.do (不自动重定向, 手动处理)
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
                # 用单独的 client (不自动重定向) POST login.do
                login_resp = await client.post(
                    f"{_4399_LOGIN_URL}", data=login_data,
                    follow_redirects=False,
                )
                login_text = login_resp.text
                login_status = login_resp.status_code
                login_location = login_resp.headers.get("location", "")

                self._last_refresh_debug["mpay_flow"]["login_status"] = login_status
                self._last_refresh_debug["mpay_flow"]["login_location"] = login_location[:300]
                self._last_refresh_debug["mpay_flow"]["login_text_2000"] = login_text[:2000]
                # 捕获 Set-Cookie 和客户端 cookies
                self._last_refresh_debug["mpay_flow"]["login_set_cookies"] = [
                    v for k, v in login_resp.headers.multi_items()
                    if k.lower() == "set-cookie"
                ][:5]
                self._last_refresh_debug["mpay_flow"]["client_cookies"] = {
                    k: v[:50] for k, v in client.cookies.items()
                }

                # login.do 现在返回加载页面 (JavaScript 处理), 但 Set-Cookie
                # 已在 HTTP 头中设置。不再检查 "登录成功" 字样, 直接继续到
                # checkKidLoginUserCookie。
                rand_time = str(int(_time_mod.time() * 1000))

                # 尝试提取 rand_time (如果存在于响应中)
                rt_match = _re.search(r'"rand_time"\s*:\s*(\d+)', login_text)
                if rt_match:
                    rand_time = rt_match.group(1)

                # 如果是重定向, 跟随一次
                if login_status in (301, 302, 303, 307, 308) and login_location:
                    resp_redirect = await client.get(login_location)
                    rt_match2 = _re.search(
                        r'"rand_time"\s*:\s*(\d+)', resp_redirect.text
                    )
                    if rt_match2:
                        rand_time = rt_match2.group(1)

                # 尝试提取 HTML 中的跳转 URL
                url_match = _re.search(
                    r'(?:location\.href|window\.location|url)\s*=\s*'
                    r'["\']([^"\']+)["\']',
                    login_text,
                )
                if url_match:
                    self._last_refresh_debug["mpay_flow"]["html_redirect"] = (
                        url_match.group(1)[:300]
                    )

                # 等待 1 秒, 让服务器完成登录处理
                import asyncio as _asyncio_mod
                await _asyncio_mod.sleep(1)

                self._last_refresh_debug["mpay_flow"]["status"] = (
                    "proceeding_to_check_cookie"
                )

                # Step 5: checkKidLoginUserCookie → sig/uid/time/validateState
                check_url = (
                    f"{CHECK_COOKIE_URL}?appId=kid_wdsj"
                    f"&gameUrl=&rand_time={rand_time}"
                )
                resp2 = await client.get(check_url, follow_redirects=False)

                # 记录 check_cookie 响应详情
                self._last_refresh_debug["mpay_flow"]["check_status"] = (
                    resp2.status_code
                )
                self._last_refresh_debug["mpay_flow"]["check_location"] = (
                    resp2.headers.get("location", "")[:500]
                )

                # 提取 sig/uid/time/validateState from redirect URL or text
                if resp2.status_code in (301, 302, 303, 307, 308):
                    redirect_url = resp2.headers.get("location", "")
                else:
                    redirect_url = resp2.text

                self._last_refresh_debug["mpay_flow"]["check_redirect_500"] = (
                    redirect_url[:500]
                )

                # FIX: 使用 redirect_url (之前误用 redirect_text)
                sig_match = _re.search(r"sig=([^&]+)", redirect_url)
                uid_match = _re.search(r"uid=([^&]+)", redirect_url)
                time_match = _re.search(r"time=([^&]+)", redirect_url)
                state_match = _re.search(
                    r"validateState=([^&]+)", redirect_url
                )

                sig = sig_match.group(1) if sig_match else ""
                ck_uid = uid_match.group(1) if uid_match else ""
                login_time = time_match.group(1) if time_match else ""
                validate_state = (
                    state_match.group(1) if state_match else ""
                )

                if not sig:
                    self._last_refresh_debug["mpay_flow"]["check_cookie"] = (
                        "no_sig"
                    )
                    return None, ""

                self._last_refresh_debug["mpay_flow"]["check_cookie"] = "ok"
                self._last_refresh_debug["mpay_flow"]["uid"] = ck_uid

                # Step 6: sdk/info → MPay SDK token
                query_str = _SDK_QUERY.format(
                    game_id="500352",
                    sig=sig,
                    uid=ck_uid,
                    time=login_time,
                    validateState=validate_state,
                    username=username,
                )
                sdk_url = f"{SDK_INFO_URL}?callback=&queryStr={query_str}"
                resp3 = await client.get(sdk_url)
                sdk_text = resp3.text.strip()

                # 去除 JSONP 包裹
                if sdk_text.startswith("(") and sdk_text.endswith(")"):
                    sdk_text = sdk_text[1:-1]
                elif "(" in sdk_text and sdk_text.endswith(")"):
                    inner_start = sdk_text.find("(")
                    if inner_start != -1:
                        sdk_text = sdk_text[inner_start + 1 : -1]

                sdk_data = json.loads(sdk_text) if sdk_text else {}
                sdk_login_data = sdk_data.get("sdk_login_data", {})
                mpay_token = sdk_login_data.get("token", "")
                mpay_sdkuid = sdk_login_data.get("sdkuid", ck_uid)

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
