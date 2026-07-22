"""4399 账号自动注册 + sauth_json 获取工具

逆向自 CYXHSJ 永久Cookies获取.exe (PyInstaller 反编译)
参考: 4399toX19Sauth.exe (Go), InfBotLobby/x19.h (C++)

流程:
    1. 自动注册 4399 账号 (随机用户名/密码/身份证/邮箱)
    2. 验证码处理 (OCR 自动识别 或 人工输入)
    3. 登录 4399 → 获取 sig/uid/time/validateState
    4. 获取 SDK 登录数据 → 提取 token 作为 sessionid
    5. 拼装 sauth_json (udid 随机生成,每账号独立)
    6. 网易统一认证 (uni_sauth) → 最终 sauth_json
    7. login-otp 验证

关键优势:
    - udid 是随机生成的 32 位 HEX,每账号不同
    - 不依赖第三方 cookie 网站
    - 完全自主可控

合规提示:
    自动注册账号、伪造设备指纹、绕过实名认证等行为可能违反
    4399 与网易的服务条款,也可能违反《网络安全法》《个人信息保护法》
    关于实名制的规定。仅供学习研究使用。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
import secrets
import string
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import urlencode, unquote, quote, urlparse, parse_qs

import httpx

logger = logging.getLogger("pocketterm.account_register")

# ============================================================================
# 常量
# ============================================================================

UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 4399 端点
PTLOGIN_BASE = "https://ptlogin.4399.com"
CAPTCHA_URL = PTLOGIN_BASE + "/ptlogin/captcha.do"
REGISTER_URL = PTLOGIN_BASE + "/ptlogin/register.do"
VERIFY_URL = PTLOGIN_BASE + "/ptlogin/verify.do"
LOGIN_URL = PTLOGIN_BASE + "/ptlogin/login.do"
CHECK_COOKIE_URL = PTLOGIN_BASE + "/ptlogin/checkKidLoginUserCookie.do"

# 5054399 SDK
SDK_INFO_URL = "https://microgame.5054399.net/v2/service/sdk/info"

# 网易统一认证
UNI_SAUTH_URL = "https://mgbsdk.matrix.netease.com/x19/sdk/uni_sauth"

# 网易 login-otp
LOGIN_OTP_URL = "https://x19obtcore.nie.netease.com:8443/login-otp"

# 身份证校验位算法常量 (GB11643)
_IDCARD_FACTORS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_IDCARD_CODES = ['1', '0', 'X', '9', '8', '7', '6', '5', '4', '3', '2']

# 百家姓
_SURNAMES = (
    "李王张刘陈杨赵黄周吴徐孙胡朱高林何郭马罗梁宋郑谢韩唐冯于董萧程曹袁邓许"
    "傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金"
    "陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段漕钱汤尹黎易常武乔贺赖龚文"
)


# ============================================================================
# 随机数据生成
# ============================================================================

def generate_random_string(length: int, charset: str = None,
                           numbers: bool = False, lowercase: bool = False,
                           uppercase: bool = False) -> str:
    """生成随机字符串。"""
    if charset is not None:
        pool = charset
    else:
        pool = ""
        if numbers: pool += string.digits
        if lowercase: pool += string.ascii_lowercase
        if uppercase: pool += string.ascii_uppercase
        if not pool:
            pool = string.ascii_letters + string.digits
    return "".join(secrets.choice(pool) for _ in range(length))


def get_idcard_last_code(idcard17: str) -> str:
    """GB11643 校验位算法。"""
    total = sum(int(idcard17[i]) * _IDCARD_FACTORS[i] for i in range(17))
    return _IDCARD_CODES[total % 11]


def generate_fake_idcard() -> str:
    """生成能通过 GB11643 校验的假身份证号。"""
    region = "110108"  # 北京海淀
    # 1970-01-01 ~ 2004-12-31 (保证成年)
    start = datetime(1970, 1, 1)
    end = datetime(2004, 12, 31)
    days = (end - start).days
    birthday = (start + timedelta(days=random.randint(0, days))).strftime("%Y%m%d")
    seq = generate_random_string(3, charset=string.digits)
    id17 = region + birthday + seq
    return id17 + get_idcard_last_code(id17)


def generate_random_name() -> str:
    """随机中文姓名。"""
    surname = secrets.choice(_SURNAMES)
    # 取 2 个 CJK 汉字
    given = "".join(chr(random.randint(0x4E00, 0x9FA5)) for _ in range(2))
    return surname + given


def generate_device_fingerprint() -> dict:
    """生成每账号独立的设备指纹 (CYXHSJ 路线)。

    udid/client_login_sn/deviceid 三者相同,都是随机 32 位 HEX。
    每个账号生成不同的指纹,避免连封。
    """
    hex32 = generate_random_string(32, charset="0123456789ABCDEF")
    return {
        "client_login_sn": hex32,
        "deviceid": hex32,
        "udid": hex32,
    }


# ============================================================================
# 验证码处理
# ============================================================================

class CaptchaHandler:
    """验证码处理器。

    默认实现: 保存图片到临时文件,调用系统查看器打开,人工输入。
    可替换为 OCR 自动识别 (如 ddddocr)。
    """

    def __init__(self, use_ocr: bool = False):
        self.use_ocr = use_ocr
        self._ocr = None
        if use_ocr:
            try:
                import ddddocr
                self._ocr = ddddocr.DdddOcr(show_ad=False)
                logger.info("OCR 自动识别已启用 (ddddocr)")
            except ImportError:
                logger.warning("ddddocr 未安装,回退到人工输入模式")
                self.use_ocr = False

    async def solve(self, image_data: bytes, expected_len: int = 4) -> str:
        """处理验证码图片,返回识别结果。

        Args:
            image_data: 验证码图片字节数据
            expected_len: 期望的验证码长度 (默认4位)
        """
        if self.use_ocr and self._ocr is not None:
            # OCR 模式: 最多重试10次
            for attempt in range(10):
                try:
                    result = self._ocr.classification(image_data)
                    logger.info(f"OCR 识别结果 (第{attempt+1}次): '{result}'")
                    if result and len(result) == expected_len:
                        return result
                    logger.warning(
                        f"OCR 识别长度不对 (期望{expected_len}位,得到{len(result)}位),重试"
                    )
                    # 长度不匹配,继续循环重试
                    continue
                except Exception as e:
                    logger.warning(f"OCR 识别失败: {e}")
                    break

            # OCR 识别不理想,返回最后一次结果 (不回退到人工输入, 服务器环境无法人工输入)
            logger.warning("OCR 识别不理想, 使用最后一次识别结果")
            return result if result else "0000"

        # 人工输入模式 (仅限桌面环境)
        return await self._manual_input(image_data)

    async def _manual_input(self, image_data: bytes) -> str:
        """人工输入验证码。"""
        # 保存到临时文件
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp.write(image_data)
            tmp_path = tmp.name

        try:
            # 打开图片查看器
            if os.name == "nt":
                os.startfile(tmp_path)
            elif sys.platform == "darwin":
                subprocess.call(["open", tmp_path])
            else:
                subprocess.call(["xdg-open", tmp_path])

            print(f"\n[验证码] 图片已打开: {tmp_path}")
            print("[验证码] 请查看图片并输入验证码 (回车确认):")

            # 在异步环境中读取输入
            # BUG-4.5 修复: 之前用 __import__("asyncio") 动态导入, 不规范且
            # 难以静态分析。asyncio 已在模块顶部导入, 直接使用即可。
            loop = asyncio.get_event_loop()
            captcha = await loop.run_in_executor(None, input, "验证码> ")
            return captcha.strip()
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ============================================================================
# 4399 账号注册器
# ============================================================================

class AccountRegistrar:
    """4399 账号自动注册 + sauth_json 获取。

    用法::

        async with AccountRegistrar() as reg:
            result = await reg.register_and_get_sauth()
            if result["success"]:
                print(result["sauth_json"])
    """

    def __init__(self, timeout: float = 30.0, captcha_handler: Optional[CaptchaHandler] = None, verify_ssl: Optional[bool] = None):
        self.timeout = timeout
        self.captcha_handler = captcha_handler or CaptchaHandler()
        # SSL 证书校验: None 表示从全局配置读取, 否则使用指定值
        self._verify_ssl = verify_ssl
        self._client: Optional[httpx.AsyncClient] = None

    def _resolve_verify_ssl(self) -> bool:
        """解析 SSL 校验设置: 显式传参优先, 否则从全局配置读取。"""
        if self._verify_ssl is not None:
            return self._verify_ssl
        try:
            from ...config import get_config
            return get_config().verify_ssl
        except Exception:
            return False

    async def __aenter__(self) -> "AccountRegistrar":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            verify=self._resolve_verify_ssl(),
            follow_redirects=True,
            headers={"User-Agent": UA_BROWSER},
        )
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("AccountRegistrar 未在 async with 上下文中使用")
        return self._client

    # ------------------------------------------------------------------
    # 注册流程
    # ------------------------------------------------------------------

    async def register(self, username: str = None, password: str = None) -> dict:
        """注册 4399 账号。

        Returns:
            {"success": bool, "message": str, "username": str, "password": str}
        """
        result = {"success": False, "message": "", "username": "", "password": ""}
        logger.info("开始注册 4399 账号")

        if not username:
            username = generate_random_string(10, numbers=True, lowercase=True)
        if not password:
            password = generate_random_string(
                12, numbers=True, lowercase=True, uppercase=True
            )

        logger.info(f"用户名: {username}")

        # 1. 获取验证码 (最多重试10次)
        max_captcha_retries = 10
        captcha = ""
        captcha_id = ""
        for attempt in range(max_captcha_retries):
            captcha_id = "captchaReqb3d25c6d6a4" + generate_random_string(
                8, charset=string.digits
            )
            captcha_url = f"{CAPTCHA_URL}?xx=1&captchaId={captcha_id}"
            logger.info(f"正在获取验证码 (第{attempt+1}次)...")
            resp = await self.client.get(captcha_url)
            if resp.status_code != 200:
                result["message"] = f"获取验证码失败: HTTP {resp.status_code}"
                continue

            # OCR 识别 (如果启用)
            if self.captcha_handler.use_ocr and self.captcha_handler._ocr is not None:
                try:
                    captcha = self.captcha_handler._ocr.classification(resp.content)
                    logger.info(f"OCR 识别结果 (第{attempt+1}次): '{captcha}'")
                    if captcha and len(captcha) == 4:
                        logger.info(f"✓ 验证码识别成功: {captcha}")
                        break
                    logger.warning(
                        f"OCR 识别长度不对 (期望4位,得到{len(captcha) if captcha else 0}位),重试"
                    )
                    captcha = ""
                    continue
                except Exception as e:
                    logger.warning(f"OCR 识别失败: {e}")
                    # P2 修复: OCR 失败时用 continue 重试, 而非 break 直接放弃
                    captcha = ""
                    continue
            else:
                # 人工输入模式
                captcha = await self.captcha_handler._manual_input(resp.content)
                if captcha:
                    break

        if not captcha:
            result["message"] = "验证码识别失败 (多次尝试)"
            return result

        # 2. 生成身份证与姓名
        id_card = generate_fake_idcard()
        realname = generate_random_name()
        # BUG-4.7 修复: 身份证号和姓名属于敏感个人信息 (PII), 不应在日志中
        # 完整输出。改为掩码显示, 仅保留必要长度的尾部信息用于核对。
        masked_id = id_card[:6] + "********" + id_card[-4:] if len(id_card) > 10 else "***"
        masked_name = realname[0] + "*" * (len(realname) - 1) if realname else "***"
        logger.info(f"身份证: {masked_id}, 姓名: {masked_name}")

        # 3. 拼注册参数
        register_params = {
            "postLoginHandler": "default",
            "displayMode": "popup",
            "appId": "www_home",
            "gameId": "",
            "cid": "",
            "externalLogin": "qq",
            "aid": "",
            "ref": "",
            "css": "",
            "redirectUrl": "",
            "regMode": "reg_normal",
            "sessionId": captcha_id,
            "regIdcard": "true",
            "noEmail": "false",
            "crossDomainIFrame": "",
            "crossDomainUrl": "",
            "mainDivId": "popup_reg_div",
            "showRegInfo": "true",
            "includeFcmInfo": "false",
            "expandFcmInput": "true",
            "fcmFakeValidate": "true",
            "userNameLabel": "4399用户名",
            "username": username,
            "password": password,
            "realname": realname,
            "idcard": id_card,
            "email": f"{generate_random_string(9, charset=string.digits)}@qq.com",
            "reg_eula_agree": "on",
            "inputCaptcha": captcha,
        }
        register_full_url = REGISTER_URL + "?" + urlencode(register_params)

        # 4. 发送注册请求
        logger.info("发送注册请求...")
        resp = await self.client.get(register_full_url)
        text = resp.text

        # 5. 判定结果
        if "验证码错误" in text:
            result["message"] = "验证码错误"
            return result
        if "用户名格式错误" in text:
            result["message"] = "用户名格式错误"
            return result
        if "用户名已被注册" in text:
            result["message"] = "用户名已被注册"
            return result
        if "请一定记住您注册的用户名和密码" not in text:
            result["message"] = f"未知错误: {text[:200]}"
            return result

        result["success"] = True
        result["message"] = "注册成功"
        result["username"] = username
        result["password"] = password
        logger.info(f"注册成功! 用户名: {username}")
        return result

    # ------------------------------------------------------------------
    # 登录 + 获取 sauth_json
    # ------------------------------------------------------------------

    async def login_and_get_sauth(self, username: str, password: str) -> dict:
        """用 4399 账密登录,最终换取网易 sauth_json。

        Returns:
            {"success": bool, "message": str, "sauth_json": str}
        """
        result = {
            "success": False, "message": "",
            "sauth_json": "", "sauth_json_inner": "",
            "username": username,
        }
        logger.info(f"开始登录流程,用户名: {username}")

        current_time = str(int(time.time() * 1000))

        # 1. 验证用户名 (检查是否需要验证码)
        verify_url = (
            f"{VERIFY_URL}?username={username}"
            f"&appId=kid_wdsj&t={current_time}&inputWidth=iptw2&v=1"
        )
        logger.info("验证用户名...")
        resp = await self.client.get(verify_url)
        verify_text = resp.text

        captcha = ""
        captcha_id = ""
        if verify_text != "0":
            logger.info("需要验证码")
            # 提取 captchaId
            m = re.search(r"captchaId=([^&\"']+)", verify_text)
            if not m:
                result["message"] = "获取 captchaId 失败"
                return result
            captcha_id = m.group(1)
            captcha_url = f"{CAPTCHA_URL}?captchaId={captcha_id}"
            resp = await self.client.get(captcha_url)
            captcha = await self.captcha_handler.solve(resp.content)

        # 2. 登录
        login_params = {
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
            "userNameTip": "4399用户名",
            "userNameLabel": "4399用户名",
            "welcomeTip": "欢迎回到4399",
            "sec": "1",
            "password": password,
            "username": username,
            "inputCaptcha": captcha,
        }
        logger.info("发送登录请求...")
        resp = await self.client.post(LOGIN_URL + "?v=1", data=login_params)
        login_text = resp.text

        if "验证码错误" in login_text:
            result["message"] = "验证码错误"
            return result
        if "密码错误" in login_text:
            result["message"] = "密码错误"
            return result
        if "用户不存在" in login_text:
            result["message"] = "用户不存在"
            return result
        logger.info("登录请求成功")

        # 3. 提取时间戳
        time_val = current_time
        for pattern in [
            r'parent\.timestamp\s*=\s*"(\d+)"',
            r'timestamp\s*:\s*"(\d+)"',
            r'rand_time=(\d+)',
            r'time=(\d+)',
        ]:
            m = re.search(pattern, login_text)
            if m:
                time_val = m.group(1)
                break

        # 4. checkKidLoginUserCookie 获取 sig/uid/time/validateState
        logger.info("检查登录状态...")
        check_url = (
            CHECK_COOKIE_URL
            + "?appId=kid_wdsj"
            + "&gameUrl=http://cdn.h5wan.4399sj.com/microterminal-h5-frame"
            + f"?game_id=500352&rand_time={time_val}"
            + "&nick=null&onLineStart=false&show=1&isCrossDomain=1"
            + "&retUrl=" + quote(
                "http://ptlogin.4399.com/resource/ucenter.html"
                "?action=login&appId=kid_wdsj&loginLevel=8"
                "&regLevel=8&bizId=2100001792&externalLogin=qq"
                "&qrLogin=true&layout=vertical&level=101"
                "&css=http://microgame.5054399.net/v2/resource/cssSdk/default/login.css"
                "&v=2018_11_26_16&postLoginHandler=redirect"
                "&checkLoginUserCookie=true"
                "&redirectUrl=http://cdn.h5wan.4399sj.com/microterminal-h5-frame?game_id=500352"
                f"&rand_time={time_val}"
            )
        )
        # 不跟随重定向
        resp = await self.client.get(check_url, follow_redirects=False)
        if resp.status_code != 302:
            result["message"] = f"检查登录状态失败: HTTP {resp.status_code}"
            return result

        redirect_url = resp.headers.get("Location", "")
        if not redirect_url:
            result["message"] = "获取重定向地址失败"
            return result

        # 5. 解析 sig/uid/time/validateState
        parsed = urlparse(redirect_url)
        qs = parse_qs(parsed.query)
        try:
            sig = qs["sig"][0]
            uid_4399 = qs["uid"][0]
            t_val = qs["time"][0]
            validate_state = qs["validateState"][0]
        except KeyError as e:
            result["message"] = f"解析重定向参数失败: {e}"
            return result

        logger.info(f"4399 uid: {uid_4399}")

        # 6. 获取 SDK 登录数据
        logger.info("获取 SDK 信息...")
        sdk_url = (
            SDK_INFO_URL + "?callback="
            + "&queryStr=" + quote(
                f"game_id=500352&nick=null&sig={sig}"
                f"&uid={uid_4399}&fcm=0&show=1&isCrossDomain=1"
                f"&rand_time={time_val}&ptusertype=4399"
                f"&time={t_val}&validateState={validate_state}"
                f"&username={username}"
            )
            + f"&_={int(time.time() * 1000)}"
        )
        resp = await self.client.get(sdk_url)
        try:
            sdk_data = resp.json()
            sdk_login_data = sdk_data.get("data", {}).get("sdk_login_data", "")
        except Exception:
            m = re.search(r"sdk_login_data[=:]([^&]+)", resp.text)
            sdk_login_data = unquote(m.group(1)) if m else ""

        if not sdk_login_data:
            result["message"] = "解析 SDK 数据失败"
            return result

        # 7. 提取 token 作为 sessionid
        m = re.search(r"token=([^&]+)", sdk_login_data)
        if not m:
            result["message"] = "获取 token 失败"
            return result
        session_id = m.group(1)
        logger.info(f"获取 sessionid 成功 (长度: {len(session_id)})")

        # 8. 拼装 sauth_json (每账号独立 udid)
        device_fp = generate_device_fingerprint()
        sauth_inner = {
            "aim_info": '{"aim":"127.0.0.1","country":"CN","tz":"+0800","tzid":""}',
            "app_channel": "4399pc",
            "client_login_sn": device_fp["client_login_sn"],
            "deviceid": device_fp["deviceid"],
            "gameid": "x19",
            "gas_token": "",
            "ip": "127.0.0.1",
            "login_channel": "4399pc",
            "platform": "pc",
            "realname": '{"realname_type":"0"}',
            "sdk_version": "1.0.0",
            "sdkuid": uid_4399,
            "sessionid": session_id,
            "source_platform": "pc",
            "timestamp": time_val,
            "udid": device_fp["udid"],
            "userid": username.lower(),
        }
        sauth_inner_str = json.dumps(sauth_inner, ensure_ascii=False)
        sauth_wrapped = json.dumps({"sauth_json": sauth_inner_str}, ensure_ascii=False)

        # 9. 网易统一认证
        logger.info("发送网易统一认证请求...")
        headers = {
            "User-Agent": "WPFLauncher/0.0.0.0",
            "Content-Type": "application/json",
        }
        resp = await self.client.post(
            UNI_SAUTH_URL, content=sauth_inner_str.encode("utf-8"), headers=headers
        )
        # BUG-4.8 修复: 之前 uni_sauth 的响应完全未检查就直接进入 login-otp,
        # 若 uni_sauth 失败 (网络错误/非 200/code!=0), 后续 login-otp 必然失败,
        # 但错误信息会指向 login-otp 而非真正的失败点。现增加响应检查。
        try:
            uni_resp = resp.json()
        except Exception:
            result["message"] = "uni_sauth 响应格式异常 (非 JSON)"
            return result
        uni_code = uni_resp.get("code", -1)
        if uni_code != 0:
            result["message"] = (
                f"uni_sauth 失败: code={uni_code}, "
                f"msg={uni_resp.get('message', '')}"
            )
            return result

        # 10. 最终 login-otp 验证
        logger.info("发送 login-otp 验证请求...")
        # P2 修复: Content-Type 保持 application/json, 与 client.py 保持一致
        # (login-otp 接受 JSON 格式的 sauth_json, text/plain 会导致部分服务器解析失败)
        resp = await self.client.post(
            LOGIN_OTP_URL, content=sauth_wrapped.encode("utf-8"), headers=headers
        )
        try:
            final_data = resp.json()
        except Exception:
            result["message"] = "login-otp 响应格式异常"
            return result

        code = final_data.get("code", -1)
        if code != 0:
            result["message"] = f"login-otp 失败: code={code}, msg={final_data.get('message', '')}"
            return result

        entity = final_data.get("entity") or {}
        aid = entity.get("aid", "")
        logger.info(f"login-otp 成功! aid={aid}")

        result["success"] = True
        result["message"] = "登录成功"
        result["sauth_json"] = sauth_wrapped
        result["sauth_json_inner"] = sauth_inner_str
        return result

    # ------------------------------------------------------------------
    # 一键注册 + 获取 sauth_json
    # ------------------------------------------------------------------

    async def register_and_get_sauth(
        self, username: str = None, password: str = None
    ) -> dict:
        """一键注册 4399 账号并获取 sauth_json。

        Returns:
            {"success": bool, "message": str,
             "username": str, "password": str,
             "sauth_json": str, "sauth_json_inner": str}
        """
        # 1. 注册
        reg = await self.register(username, password)
        if not reg["success"]:
            return reg

        # 2. 登录获取 sauth_json
        login = await self.login_and_get_sauth(reg["username"], reg["password"])
        if not login["success"]:
            return login

        return {
            "success": True,
            "message": "注册并获取 sauth_json 成功",
            "username": reg["username"],
            "password": reg["password"],
            "sauth_json": login["sauth_json"],
            "sauth_json_inner": login["sauth_json_inner"],
        }

    # ------------------------------------------------------------------
    # 邮箱批量注册 (4399 官方支持邮箱注册, 不需要手机号)
    # ------------------------------------------------------------------

    async def register_with_email(
        self,
        email: str = None,
        email_code_handler: Optional[Callable] = None,
    ) -> dict:
        """用邮箱注册 4399 账号 (不需要手机号)。

        4399 官方支持邮箱注册, 流程:
          1. 填写邮箱 → 接收验证邮件
          2. 输入验证码 → 完成注册

        Args:
            email: 指定邮箱 (可选, 默认随机生成)
            email_code_handler: 邮箱验证码处理函数
                签名: async def handler(email: str) -> str
                返回收到的验证码

        Returns:
            {"success": bool, "message": str,
             "username": str, "password": str, "email": str}
        """
        result = {
            "success": False, "message": "",
            "username": "", "password": "", "email": "",
        }

        if not email:
            # 生成随机 QQ 邮箱 (4399 接受 QQ 邮箱)
            email = f"{generate_random_string(9, charset=string.digits)}@qq.com"
        result["email"] = email

        username = generate_random_string(10, numbers=True, lowercase=True)
        password = generate_random_string(
            12, numbers=True, lowercase=True, uppercase=True
        )
        result["username"] = username
        result["password"] = password

        # BUG-4.7 修复: 邮箱属于敏感信息, 掩码显示 (保留首字符和域名)
        masked_email = (
            email[0] + "***@" + email.split("@", 1)[1]
            if "@" in email else "***"
        )
        logger.info(f"邮箱注册: {masked_email}, 用户名: {username}")

        # 1. 获取图形验证码
        captcha_id = "captchaReqb3d25c6d6a4" + generate_random_string(
            8, charset=string.digits
        )
        captcha_url = f"{CAPTCHA_URL}?xx=1&captchaId={captcha_id}"
        resp = await self.client.get(captcha_url)
        if resp.status_code != 200:
            result["message"] = f"获取验证码失败: HTTP {resp.status_code}"
            return result

        captcha = await self.captcha_handler.solve(resp.content)
        if not captcha or len(captcha) != 4:
            result["message"] = "验证码识别失败"
            return result

        # 2. 发送邮箱验证码
        id_card = generate_fake_idcard()
        realname = generate_random_name()
        # BUG-4.7 修复: 身份证号和姓名属于敏感 PII, 掩码显示
        masked_id = id_card[:6] + "********" + id_card[-4:] if len(id_card) > 10 else "***"
        masked_name = realname[0] + "*" * (len(realname) - 1) if realname else "***"
        logger.info(f"身份证: {masked_id}, 姓名: {masked_name}")

        # 注意: 4399 的邮箱注册 API 可能与手机号注册不同
        # 这里尝试用 send_email_code 端点
        # 由于 4399 注册流程已变, 此方法可能需要调整
        result["message"] = "邮箱注册功能需要根据 4399 最新流程调整"
        result["success"] = False
        return result

    async def batch_register(
        self,
        count: int = 1,
        use_email: bool = False,
        delay: float = 5.0,
    ) -> list[dict]:
        """批量注册账号。

        Args:
            count: 注册数量
            use_email: 是否使用邮箱注册 (False 用默认流程)
            delay: 每次注册间隔 (秒, 防止 IP 被限制)

        Returns:
            结果列表
        """
        results = []
        for i in range(count):
            logger.info(f"批量注册 [{i+1}/{count}]")
            # BUG-4.6 修复: 之前 batch_register 未捕获单次注册异常, 一次失败
            # 会导致整个批量注册中断。现捕获异常并记录, 继续后续注册。
            try:
                if use_email:
                    result = await self.register_with_email()
                else:
                    result = await self.register_and_get_sauth()
            except Exception as exc:  # noqa: BLE001
                logger.error(f"批量注册 [{i+1}/{count}] 异常: {exc}")
                result = {
                    "success": False,
                    "message": f"注册异常: {exc}",
                }
            results.append(result)

            if i < count - 1:
                logger.info(f"等待 {delay} 秒...")
                await asyncio.sleep(delay)

        success_count = sum(1 for r in results if r.get("success"))
        logger.info(f"批量注册完成: {success_count}/{count} 成功")
        return results


__all__ = [
    "AccountRegistrar",
    "CaptchaHandler",
    "generate_device_fingerprint",
    "generate_fake_idcard",
    "generate_random_name",
    "generate_random_string",
]
