"""网易直连认证客户端 - 直接与网易服务器通信完成认证。

支持两种登录模式:
    - PE 模式 (g79): 使用 /pe-authentication + HttpEncrypt_g79v12 (AES-128, hex编码密钥)
    - PC 模式 (x19): 使用 /login-otp + /authentication-otp + HttpEncrypt (AES-128)

完整租赁服连接流程:
    1. 登录 → 获取 UID + LoginSRCToken
    2. 搜索租赁服 (SearchRentalServerByName) → 获取 serverID
    3. 进入租赁服 (EnterRentalServerWorld) → 获取游戏服务器 IP:port
    4. 获取 ECDH 密钥 (get-client-ecdh-key)
    5. 生成 auth v2 (authentication-v2) → 获取 chain JWT
    6. 获取 chainInfo (get-chain-info) → 获取最终身份链

参考来源:
    - Login.Core.dll IL 反编译代码
    - Drug.NetEase.Opensource (PC→PE 认证源码)
    - Fatalder 源码 (g79client 使用示例)
    - FastBuilder 源码 (WebSocket 认证流程)
    - NoneBot2 示例插件 (API 路径参考)
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .constants import (
    AUTH_SERVER,
    APIGATEWAYOBT_PC,
    APIGATEWAYOBT_PE,
    COREOBT_PC,
    COREOBT_PE,
    ENGINE_VERSION,
    GAME_VERSION,
    KEYS,
    KEYS_G79V12,
    LAUNCHER_VERSION,
    LIB_MINECRAFT_PE,
    PATCH_HASH,
    PATCH_VERSION,
    SA_DATA_PC,
    SA_DATA_PE,
    SDK_VERSION_PC,
    SDK_VERSION_PE,
    generate_sa_data_pc,
)
from .crypto import (
    compute_dynamic_token,
    compute_dynamic_token_auth,
    generate_trace_id,
    http_decrypt,
    http_decrypt_g79v12,
    http_encrypt,
    http_encrypt_g79v12,
)

logger = logging.getLogger("pocketterm.netease_direct")


def _parse_json_lenient(text: str) -> dict:
    """容错 JSON 解析 - 处理解密后可能存在的尾部垃圾字节。

    解密后的数据有时会包含尾部填充字节,导致标准 json.loads 失败。
    使用 raw_decode 只解析开头的有效 JSON 部分。
    """
    text = text.strip()
    # 先尝试标准解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 尝试只解析开头的 JSON (忽略尾部垃圾)
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text.lstrip())
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    raise RuntimeError(f"JSON 解析失败: {text[:300]}")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class DirectAuthResult:
    """直连认证结果。"""
    chain_info: str = ""
    server_address: str = ""
    uid: str = ""
    player_name: str = ""
    auth_mode: str = ""  # "pe" 或 "pc"
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 网易直连认证客户端
# ---------------------------------------------------------------------------

class NeteaseDirectClient:
    """异步网易直连认证客户端。

    用法::

        async with NeteaseDirectClient(mode="pe") as client:
            await client.login(sauth_json)
            result = await client.connect_rental_server("1895088")
            print(result.chain_info, result.server_address)

    登录策略:
        始终使用 PC 认证 (/login-otp + /authentication-otp + HttpEncrypt)。
        PE 认证 (/pe-authentication + HttpEncrypt_g79v12) 需要 PESignCount 原生签名
        (Auth.Sign.dll 中的 CountSign 函数), 无法在 Python 中实现, 因此不使用。
        如果输入的 sauth_json 是 PE 格式 (gameid=g79), 会自动转换为 PC 格式。
    """

    def __init__(self, mode: str = "pc", timeout: float = 30.0, verify_ssl: Optional[bool] = None) -> None:
        self.mode = mode  # "pc" 或 "pe"
        self.timeout = timeout
        # SSL 证书校验: None 表示从全局配置读取, 否则使用指定值
        self._verify_ssl = verify_ssl
        self.uid: str = ""
        self.login_src_token: str = ""
        self.login_md5_token: str = ""
        self.h5token: bytes = b""  # Base64Decode(LoginSRCToken), 用于认证服务器
        self.player_name: str = ""
        self._client: Optional[httpx.AsyncClient] = None
        # 实际使用的 API 网关模式 (登录后确定)
        self._api_mode: str = mode
        # 每个账号独立的设备指纹 (防止多账号共用同一指纹被反作弊检测)
        self._sa_data_pc: str = ""
        # 当前登录的账号 ID (用于设备指纹 uqholder 持久化)
        self._current_account_id: str = ""
        # 当前登录的服务器地址 (用于 uqholder 记录)
        self._current_login_host: str = ""
        # 重连次数 (用于 EnhancedAntiBan 退避判断)
        self._reconnect_attempt: int = 0
        # C-6: XUID (Xbox Live UID, 认证后从响应中提取)
        self._xuid: str = ""

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def coreobt(self) -> str:
        return COREOBT_PE if self.mode == "pe" else COREOBT_PC

    @property
    def apigatewayobt(self) -> str:
        """API 网关地址,基于实际认证模式而非请求模式。"""
        return APIGATEWAYOBT_PE if self._api_mode == "pe" else APIGATEWAYOBT_PC

    @property
    def sa_data(self) -> str:
        if self.mode == "pe":
            return SA_DATA_PE
        # PC 模式: 使用为该账号生成的独立设备指纹
        # 注意: sa_data 中的 udid 不需要与 sauth_json 中的 udid 一致
        # 参考源 (Drug.NetEase x19Auth.cs) 使用固定的 sa_data.udid,
        # 与 sauth_json.udid 不同。sa_data.udid 是设备硬件指纹 (CPUID+磁盘),
        # sauth_json.udid 是会话标识, 两者用途不同。
        # 防封修复: 如果 _sa_data_pc 未设置, 立即生成一个 (用默认种子),
        # 不回退到硬编码的 SA_DATA_PC。硬编码指纹所有账号共用同一设备信息,
        # 会被反作弊系统识别为机器人农场。
        if not self._sa_data_pc:
            self._sa_data_pc = generate_sa_data_pc()
            logger.warning(
                "sa_data 未预先生成, 已生成随机设备指纹 (建议在 login() 中预先生成)"
            )
        return self._sa_data_pc

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("NeteaseDirectClient 未在 async with 上下文中使用")
        return self._client

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "NeteaseDirectClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=15.0),
            verify=self._resolve_verify_ssl(),  # 网易证书有时不被系统信任, 可通过配置开关
            follow_redirects=True,
        )
        return self

    def _resolve_verify_ssl(self) -> bool:
        """解析 SSL 校验设置: 显式传参优先, 否则从全局配置读取。"""
        if self._verify_ssl is not None:
            return self._verify_ssl
        try:
            from ...config import get_config
            return get_config().verify_ssl
        except Exception:
            return False

    async def __aexit__(self, *args) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # 轻量重连 (C-2 修复: Scheme B - 用缓存的 LoginSRCToken 跳过 /login-otp)
    # ------------------------------------------------------------------

    def set_login_state(
        self,
        *,
        uid: str,
        login_src_token: str,
        login_md5_token: str = "",
        player_name: str = "",
        h5token_b64: str = "",
    ) -> None:
        """恢复缓存的登录状态 (Scheme B: 跳过 /login-otp)。

        ToolDelta 的"机器人退出重进"是 RakNet 重连 + 复用缓存的 chainInfo。
        当 chainInfo 过期后, 用缓存的 LoginSRCToken 只走 /authentication-otp,
        跳过 /login-otp, 减少 50% 认证频率。

        Args:
            uid: 网易 UID。
            login_src_token: LoginSRCToken (MD5 格式)。
            login_md5_token: LoginSRCToken 的 MD5 (可选)。
            player_name: 玩家名 (可选)。
            h5token_b64: Base64 编码的 h5token (可选, 为空时从 login_src_token 推导)。
        """
        self.uid = uid
        self.login_src_token = login_src_token
        self.login_md5_token = login_md5_token or hashlib.md5(
            login_src_token.encode()
        ).hexdigest()
        self.player_name = player_name
        # h5token = Base64Decode(LoginSRCToken), 用于认证服务器
        if h5token_b64:
            self.h5token = base64.b64decode(h5token_b64)
        else:
            try:
                self.h5token = base64.b64decode(login_src_token)
            except Exception:
                self.h5token = b""
        logger.info(
            "Scheme B: 已恢复缓存的登录状态 (跳过 /login-otp): UID=%s", uid
        )

    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------

    async def check_session(self, sauth_json: str) -> dict:
        """轻量检查: 仅调用 /login-otp, 不触发 /authentication-otp。

        login-otp 只验证 sessionid 是否有效, 不会触发封禁检测。
        适合用于"检测账号"功能, 可安全反复调用。

        Returns:
            {"valid": True/False, "code": int, "aid": str, "message": str}
        """
        inner_sauth = self._extract_inner_sauth(sauth_json)
        pc_inner = self._convert_sauth_to_pc(inner_sauth)
        # 始终重新包装为完整的 {"sauth_json":"<inner>"} 格式
        # (避免当 inner 已是 PC 格式时, 直接发送 inner 字符串导致 "参数为空")
        sauth_json = json.dumps({"sauth_json": pc_inner}, ensure_ascii=False)

        try:
            resp = await self._post_login_otp(sauth_json)
        except RuntimeError as e:
            return {"valid": False, "code": -1, "aid": "", "message": str(e)[:200]}
        # BUG-1.6 修复: _post_login_otp 内部使用 httpx, 可能抛出 httpx.HTTPError
        # (含连接错误、超时、协议错误) 或 asyncio.TimeoutError。这些网络异常
        # 之前会直接传播给调用方, 现统一返回无效结果。
        except (httpx.HTTPError, asyncio.TimeoutError) as e:
            return {"valid": False, "code": -1, "aid": "", "message": str(e)[:200]}

        code = resp.get("code", -1)
        if code == 0:
            entity = resp.get("entity") or {}
            return {
                "valid": True,
                "code": 0,
                "aid": str(entity.get("aid", "")),
                "message": "sessionid 有效",
            }
        return {
            "valid": False,
            "code": code,
            "aid": "",
            "message": resp.get("message", f"code={code}"),
        }

    async def login(
        self,
        sauth_json: str,
        *,
        account_id: str = "",
        login_host: str = "",
        is_reconnect: bool = False,
    ) -> None:
        """执行登录流程 - 始终使用 PC 认证。

        PC 认证 (/login-otp + /authentication-otp) 不需要 PESignCount 原生签名,
        是唯一可在 Python 中实现的认证方式。

        如果输入的 sauth_json 是 PE 格式 (gameid=g79, platform=android),
        会自动转换为 PC 格式 (gameid=x19, platform=pc, sdk_version=SDK_VERSION_PC)。

        **增强防封策略** (ToolDelta / NexusE 逆向):
            - 登录前若为重连, 应用 ToolDelta 指数退避序列
              ``[5, 10, 20, 40, 80, 160, 300]`` 秒
            - 登录前随机延迟 1.5-3.5s (模拟真实用户)
            - 登录成功后更新设备指纹的 uqholder 扩展信息
              (XUID / IdentityName / 登录次数 / 上次登录时间)

        Args:
            sauth_json: 登录凭证 JSON 字符串，格式为 ``{"sauth_json":"<inner>"}``
                inner 包含 sdkuid, sessionid, udid, deviceid 等。
            account_id: 账号 ID (用于 uqholder 持久化, 可选)。
            login_host: 登录的服务器地址 (用于 uqholder 记录, 可选)。
            is_reconnect: 是否为重连 (用于触发 ToolDelta 退避)。

        Raises:
            RuntimeError: 登录失败时抛出
        """
        inner_sauth = self._extract_inner_sauth(sauth_json)

        # 将 PE 格式的 sauth_json 转换为 PC 格式
        pc_inner = self._convert_sauth_to_pc(inner_sauth)
        if pc_inner != inner_sauth:
            logger.info("sauth_json 已从 PE 格式转换为 PC 格式")
            inner_sauth = pc_inner
            sauth_json = json.dumps(
                {"sauth_json": pc_inner}, ensure_ascii=False
            )

        # 为该账号生成独立的设备指纹 (SA_DATA)
        # 参考源 (Drug.NetEase x19Auth.cs): sa_data.udid 是设备硬件指纹,
        # 不需要与 sauth_json.udid 一致。sauth_json.udid 是会话标识。
        # SA_DATA 中的设备指纹独立生成, 每账号不同, 防止多账号共用指纹。
        try:
            inner_data = json.loads(inner_sauth)
            sdkuid = str(inner_data.get("sdkuid", ""))
            # 用 sdkuid 作为种子,确保同一账号每次生成相同指纹
            seed = sdkuid or inner_sauth[:32]
            self._sa_data_pc = generate_sa_data_pc(seed)
            logger.debug("已为账号生成独立 SA_DATA 设备指纹")
            # 记录账号 ID (用于登录后 uqholder 持久化)
            if not account_id:
                account_id = sdkuid
        except (json.JSONDecodeError, TypeError):
            self._sa_data_pc = generate_sa_data_pc()
            logger.warning("无法解析 inner_sauth, 使用随机设备指纹")

        self._current_account_id = account_id
        self._current_login_host = login_host

        # C-6 修复: 在认证前创建/获取设备指纹 (鸡生蛋问题修复)
        # 之前: 指纹在认证后才创建, 导致认证时没有持久化指纹可用
        # 现在: 先创建指纹, 认证后只需 update_login_info 更新 uid/player_name
        # 这样 sa_data 和 Go 配置都能使用持久化的指纹
        if account_id:
            try:
                from ..device_fingerprint import get_fingerprint_manager
                fp_mgr = get_fingerprint_manager()
                fp = fp_mgr.get_by_account(account_id)
                if fp is None:
                    fp = fp_mgr.get_or_create(account_id)
                    logger.debug(
                        "C-6: 认证前创建设备指纹 (account=%s)", account_id
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("C-6: 认证前创建设备指纹失败: %s", exc)

        # 防封策略 1: 重连时应用 ToolDelta 指数退避序列
        # [5, 10, 20, 40, 80, 160, 300] 秒
        if is_reconnect:
            try:
                from ..anti_ban_enhanced import get_enhanced_anti_ban
                enhanced = get_enhanced_anti_ban()
                if enhanced.backoff.should_retry():
                    backoff_delay = enhanced.backoff.next_delay()
                    logger.info(
                        "ToolDelta 退避: 等待 %.1fs 后重连 (attempt=%d)",
                        backoff_delay, enhanced.backoff.attempt
                    )
                    await asyncio.sleep(backoff_delay)
                else:
                    raise RuntimeError(
                        f"ToolDelta 退避已用尽 (max={enhanced.config.max_retry_attempts}), "
                        "停止重连"
                    )
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("EnhancedAntiBan 退避不可用, 使用默认延迟: %s", exc)
                # 回退到默认重连延迟
                await asyncio.sleep(5.0)
            self._reconnect_attempt += 1

        # 防封策略 2: 登录前添加随机延迟 (模拟真实用户行为)
        import random as _random
        _pre_delay = _random.uniform(1.5, 3.5)
        logger.info("登录前等待 %.1fs (防封策略)", _pre_delay)
        await asyncio.sleep(_pre_delay)

        logger.info("开始登录流程 (PC 认证)")
        try:
            await self._pc_login(sauth_json, inner_sauth)
            self._api_mode = "pc"
            logger.info("PC 登录成功: UID=%s", self.uid)
        except RuntimeError as exc:
            # 如果 PC 登录失败且用户要求 PE 模式, 尝试 PE 登录
            # PE 登录需要 PESignCount 原生签名 (通过 PESignBridge)
            if self.mode == "pe":
                logger.warning(
                    "PC 登录失败, 回退到 PE 认证: %s", exc
                )
                await self._pe_login(sauth_json, inner_sauth)
                self._api_mode = "pe"
                logger.info("PE 登录成功: UID=%s", self.uid)
            else:
                raise

        # 防封策略 3: 登录成功后更新设备指纹的 uqholder 扩展信息
        # (NexusE 风格: 持久化 XUID / IdentityName / 登录次数, 保持多登录一致性)
        if account_id:
            try:
                from ..device_fingerprint import get_fingerprint_manager
                fp_mgr = get_fingerprint_manager()
                # 如果账号没有预先创建设备指纹, 先创建一个
                fp = fp_mgr.get_by_account(account_id)
                if fp is None:
                    fp = fp_mgr.get_or_create(account_id)
                    logger.debug(
                        "为账号 %s 创建新设备指纹 (登录时自动创建)",
                        account_id,
                    )
                fp_mgr.update_login_info(
                    account_id=account_id,
                    identity_name=self.player_name,
                    login_host=login_host,
                    extend_info={
                        "uid": self.uid,
                        "xuid": self._xuid,
                    },
                )
                logger.debug(
                    "已更新账号 %s 的 uqholder 登录信息", account_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("更新 uqholder 登录信息失败 (不影响登录): %s", exc)

        # 防封策略 4: 重置重连计数 (登录成功)
        if is_reconnect:
            try:
                from ..anti_ban_enhanced import get_enhanced_anti_ban
                enhanced = get_enhanced_anti_ban()
                enhanced.on_reconnect_success()
            except Exception as exc:  # noqa: BLE001
                # BUG-1.8 修复: 之前用 pass 静默吞没异常, 调试困难。
                # 现至少记录日志, 便于排查防封模块问题。
                logger.debug("on_reconnect_success 回调失败 (不影响登录): %s", exc)
            self._reconnect_attempt = 0

    async def _pe_login(self, sauth_json: str, inner_sauth: str) -> None:
        """PE 登录: POST /pe-authentication (HttpEncrypt_g79v12)。

        构造 PEAURequest 结构, 通过 :class:`pesign_bridge.PESignBridge` 生成 sign 字段:
            1. Windows + Auth.Sign.dll: cgo 调用原生 CountSign (方案一)
            2. Linux/无 DLL: sign 为空, 服务器可能拒绝 (需回退到 fbauth)

        Args:
            sauth_json: 完整的 sauth_json (外层)。
            inner_sauth: 内层 sauth_json。
        """
        seed = str(uuid.uuid4())
        message = f"{ENGINE_VERSION}{LIB_MINECRAFT_PE}{PATCH_VERSION}{PATCH_HASH}{seed}"

        # 生成 sign 字段 (方案一: PESignCount 原生签名)
        sign = ""
        sign_method = "none"
        try:
            from ..pesign_bridge import get_pesign_bridge
            bridge = get_pesign_bridge()
            if bridge.is_available:
                logger.info("使用 PESignBridge 生成签名...")
                sign_result = await bridge.sign(
                    message=message,
                    offset=2,
                    rounds=9,
                )
                if sign_result.success and sign_result.sign:
                    sign = sign_result.sign
                    sign_method = sign_result.method
                    logger.info(
                        "PESignCount 成功 (method=%s): sign=%s...",
                        sign_method, sign[:16] if len(sign) >= 16 else sign,
                    )
                else:
                    logger.warning(
                        "PESignCount 失败 (method=%s): %s",
                        sign_result.method, sign_result.error,
                    )
            else:
                logger.warning("PESignBridge 不可用, sign 字段为空")
        except Exception as exc:
            logger.warning("调用 PESignBridge 失败 (sign 为空): %s", exc)

        peau_request = {
            "sa_data": SA_DATA_PE,
            "engine_version": ENGINE_VERSION,
            "patch_version": PATCH_VERSION,
            "message": message,
            "sauth_json": inner_sauth,
            "seed": seed,
            "sign": sign,  # PESignCount 生成 (方案一), 或空 (回退)
            "extra_param": "",
            "pay_channel": "",
        }

        logger.info(
            "PE 登录请求构造完成: sign_method=%s sign_len=%d",
            sign_method, len(sign),
        )

        body_json = json.dumps(peau_request, ensure_ascii=False, separators=(",", ":"))
        encrypted = http_encrypt_g79v12(body_json.encode("utf-8"))
        # 关键: PE 认证请求必须以 hex 编码字符串发送 (非原始二进制)
        hex_encrypted = encrypted.hex()

        url = f"{COREOBT_PE}/pe-authentication"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "WPFLauncher/0.0.0.0",
        }

        resp = await self.client.post(
            url, content=hex_encrypted.encode("utf-8"), headers=headers
        )
        resp_text = resp.text.strip()

        if resp.status_code != 200:
            raise RuntimeError(
                f"pe-authentication HTTP {resp.status_code}: {resp_text[:200]}"
            )

        # 响应也是 hex 编码的,先 hex 解码再解密
        try:
            resp_bytes = bytes.fromhex(resp_text)
        except ValueError:
            # 如果不是 hex,尝试直接用原始字节
            resp_bytes = resp.content

        decrypted = http_decrypt_g79v12(resp_bytes)
        text = decrypted.decode("utf-8", errors="replace")

        try:
            data = _parse_json_lenient(text)
        except RuntimeError:
            raise RuntimeError(f"pe-authentication 响应不是 JSON: {text[:300]}")

        code = data.get("code", -1)
        if code != 0:
            raise RuntimeError(
                f"pe-authentication 失败: code={code}, msg={data.get('message', '')}"
            )

        entity = data.get("entity") or {}
        self.uid = str(entity.get("entity_id", ""))
        self.login_src_token = str(entity.get("token", ""))

        if not self.uid or not self.login_src_token:
            raise RuntimeError(
                f"pe-authentication 返回数据不完整: uid={self.uid}, token_len={len(self.login_src_token)}"
            )

        self._compute_md5_token()

    async def _pc_login(self, sauth_json: str, inner_sauth: str) -> None:
        """PC 登录: /login-otp (明文) → /authentication-otp (加密)。"""
        # 步骤1: POST /login-otp
        loginotp_resp = await self._post_login_otp(sauth_json)

        aid = ""
        otp_token = ""
        try:
            entity = loginotp_resp.get("entity") or {}
            aid = str(entity.get("aid", ""))
            otp_token = str(entity.get("otp_token", ""))
        except (AttributeError, TypeError):
            pass

        if not aid or not otp_token:
            code = loginotp_resp.get("code", -1)
            msg = loginotp_resp.get("message", str(loginotp_resp))
            raise RuntimeError(f"login-otp 失败: code={code}, msg={msg}")

        logger.info("login-otp 成功: aid=%s", aid)

        # 步骤2: 构造 AuthenticationEntity 并 POST /authentication-otp
        # 关键防封修复: 保留原始 sauth_json 中的 client_login_sn、sdk_version、
        #   source_platform 等字段, 不用 Drug.NetEase 硬编码值覆盖。
        #   之前用 Drug.NetEase 模板的硬编码值 (client_login_sn=846C15C9...,
        #   sdk_version=3.4.0) 覆盖了 sauth_json 中的真实值, 导致所有登录请求
        #   都带有相同的已知机器人签名, 被网易反作弊系统识别为机器人并封禁 (code=29)。
        #   真实的 sauth_json (MPay 工具获取) 包含每个会话唯一的 client_login_sn
        #   和当前的 sdk_version, 必须保留这些值。
        # auth_entity 结构: 只含 otp_token, otp_pwd, aid, sauth_json, sa_data,
        #   version 六个字段。aid 为整数 (非字符串)。
        try:
            inner_data = json.loads(inner_sauth)
            # 从原始 sauth_json 提取 IP (用于 aim_info 和 ip 字段)
            orig_ip = "127.0.0.1"
            try:
                aim_raw = inner_data.get("aim_info", "")
                if isinstance(aim_raw, str) and aim_raw:
                    aim_obj = json.loads(aim_raw)
                    orig_ip = str(aim_obj.get("aim", "127.0.0.1"))
                elif isinstance(aim_raw, dict):
                    orig_ip = str(aim_raw.get("aim", "127.0.0.1"))
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
            # 也检查 ip 字段
            if not orig_ip or orig_ip == "127.0.0.1":
                orig_ip = str(inner_data.get("ip", "127.0.0.1")) or "127.0.0.1"

            aim_info_str = json.dumps(
                {"aim": orig_ip, "country": "CN", "tz": "+0800", "tzid": ""},
                ensure_ascii=False,
            )
            # 防封关键: 保留原始 sauth_json 中的 client_login_sn、sdk_version、
            # source_platform, 仅在缺失时使用默认值。这些值是会话唯一的,
            # 用硬编码值覆盖会被反作弊系统识别为机器人。
            orig_client_login_sn = str(inner_data.get("client_login_sn", "")) or ""
            orig_sdk_version = str(inner_data.get("sdk_version", "")) or ""
            orig_source_platform = str(inner_data.get("source_platform", "")) or ""
            # 保留原始 login_channel/app_channel/platform:
            # 4399com 频道必须保留 platform=ad 和 channel=4399com,
            # 否则 authentication-otp 会返回错误。
            orig_login_channel = str(inner_data.get("login_channel", "")) or "netease"
            orig_app_channel = str(inner_data.get("app_channel", "")) or "netease"
            orig_platform = str(inner_data.get("platform", "")) or "pc"
            auth_sauth = {
                "gameid": "x19",
                "login_channel": orig_login_channel,
                "app_channel": orig_app_channel,
                "platform": orig_platform,
                "sdkuid": str(inner_data.get("sdkuid", "")),
                "sessionid": str(inner_data.get("sessionid", "")),
                # 保留原始 sdk_version (如 5.9.0), 仅在缺失时用 SDK_VERSION_PC
                "sdk_version": orig_sdk_version or SDK_VERSION_PC,
                "udid": str(inner_data.get("udid", "")),
                "deviceid": str(inner_data.get("deviceid", "")),
                "aim_info": aim_info_str,
                # 防封关键: 保留原始 client_login_sn (会话唯一标识),
                # 不用 Drug.NetEase 硬编码值覆盖
                "client_login_sn": orig_client_login_sn,
                "gas_token": "",
                # 保留原始 source_platform (如 "netease"), 仅在缺失时用 "pc"
                "source_platform": orig_source_platform or "pc",
                "ip": orig_ip,
            }
            # 保留原始 nickname (如果有)
            orig_nickname = str(inner_data.get("nickname", "")) or ""
            if orig_nickname:
                auth_sauth["nickname"] = orig_nickname
            # 保留原始 realname (4399 频道的 sauth_json 包含 realname 字段)
            orig_realname = str(inner_data.get("realname", "")) or ""
            if orig_realname:
                auth_sauth["realname"] = orig_realname
            auth_sauth_json = json.dumps(auth_sauth, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            auth_sauth_json = inner_sauth

        # aid 保持为整数 (Drug.NetEase 模板中 aid 不带引号: "aid":$A1D$)
        # BUG-1.3 修复: 转换失败时不应保留字符串类型, 因为模板要求 aid 为整数,
        # 字符串 aid 会导致服务端拒绝。改为抛出 ValueError 以便尽早暴露问题。
        try:
            aid_int = int(aid)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"aid 无法转换为整数 (aid={aid!r}), 认证模板要求 aid 为整数"
            ) from exc

        auth_entity = {
            "otp_token": otp_token,
            "otp_pwd": "",
            "aid": aid_int,
            "sauth_json": auth_sauth_json,
            "sa_data": self.sa_data,
            "version": {
                "version": LAUNCHER_VERSION,
                "launcher_md5": "",
                "updater_md5": "",
            },
        }
        auth_json = json.dumps(auth_entity, ensure_ascii=False)

        auth_resp = await self._post_authentication_otp(auth_json)

        code = auth_resp.get("code", -1)
        entity = auth_resp.get("entity") or {}

        # P1 修复: 先检查 code, 成功后再设置状态, 避免失败时污染客户端状态
        if code != 0:
            raise RuntimeError(
                f"authentication-otp 失败: code={code}, msg={auth_resp.get('message', '')}"
            )

        # 成功后才更新状态
        self.uid = str(entity.get("entity_id", ""))
        self.login_src_token = str(entity.get("token", ""))
        # 玩家名: 优先从 entity.name 取, 其次 entity.nickname, 最后 entity.user_name
        self.player_name = (
            str(entity.get("name", ""))
            or str(entity.get("nickname", ""))
            or str(entity.get("user_name", ""))
        )
        # C-6 修复: 提取 XUID (Xbox Live UID, 可能不在 login-otp 响应中)
        # XUID 通常在后续的 chainInfo JWT 中, 但如果 auth 响应包含就直接取
        self._xuid = str(entity.get("xuid", "") or entity.get("identity", ""))

        if not self.uid or not self.login_src_token:
            raise RuntimeError(
                f"authentication-otp 返回数据不完整: uid={self.uid}, token_len={len(self.login_src_token)}"
            )

        self._compute_md5_token()

    def _compute_md5_token(self) -> None:
        """计算 LoginMD5Token 和 H5Token。

        关键修复 (参考 nemc_check MclNetClient.GetDecryptToken):
          - 服务器返回的 entity.token 是 LoginDToken (加密的)
          - LoginSRCToken = GetDecryptToken(LoginDToken)
            AES-128-CBC 解密, Key="debbde3548928fab", IV="afd4c5c5a7c456a1"
            解密后 Skip(8).Take(16) 得到 16 字节 ASCII 字符串
          - LoginMD5Token = Base64(MD5(LoginSRCToken)) (API 网关用)
          - H5Token = Base64Decode(LoginSRCToken) (认证服务器用)
        """
        # 解密 LoginDToken 获取 LoginSRCToken
        self.login_src_token = self._get_decrypt_token(self.login_src_token)
        logger.debug("已解密 LoginSRCToken (长度: %d)", len(self.login_src_token))

        md5_bytes = hashlib.md5(self.login_src_token.encode("utf-8")).digest()
        self.login_md5_token = base64.b64encode(md5_bytes).decode("ascii")
        # H5Token: Base64 解码 LoginSRCToken 得到原始字节 (FastBuilder 模式)
        try:
            self.h5token = base64.b64decode(self.login_src_token)
        except Exception:
            self.h5token = self.login_src_token.encode("utf-8")

    @staticmethod
    def _get_decrypt_token(dtoken: str) -> str:
        """解密 LoginDToken 获取 LoginSRCToken。

        复刻 nemc_check MclNetClient.GetDecryptToken:
          AES-128-CBC 解密 HexToBytes(DToken)
          Key: debbde3548928fab (16字节ASCII)
          IV:  afd4c5c5a7c456a1 (16字节ASCII)
          解密后 Skip(8).Take(16) -> 16字节ASCII字符串

        如果 DToken 不是 hex 格式 (可能已解密),原样返回。
        """
        # 检查是否是 hex 格式
        # AES-128-CBC 需要 IV(16) + 至少1个数据块(16) = 32字节 = 64 hex 字符
        # 解密后 Skip(8).Take(16) 需要明文至少 24 字节, 即密文至少 32 字节
        if not dtoken or len(dtoken) < 64 or not all(
            c in "0123456789abcdefABCDEF" for c in dtoken
        ):
            # 不是 hex 格式,可能已经是解密后的,原样返回
            return dtoken

        try:
            from cryptography.hazmat.primitives.ciphers import (
                Cipher, algorithms, modes,
            )
            from cryptography.hazmat.backends import default_backend

            key = b"debbde3548928fab"
            iv = b"afd4c5c5a7c456a1"
            ciphertext = bytes.fromhex(dtoken)

            cipher = Cipher(
                algorithms.AES(key), modes.CBC(iv),
                backend=default_backend(),
            )
            decryptor = cipher.decryptor()
            plaintext = decryptor.update(ciphertext) + decryptor.finalize()

            # Skip(8).Take(16)
            result = plaintext[8:24]
            return result.decode("ascii")
        except Exception as e:
            logger.warning(f"GetDecryptToken 失败: {e}, 使用原始 token")
            return dtoken

    async def _post_login_otp(self, sauth_json: str) -> dict:
        """POST /login-otp - 获取 aid + otp_token (明文请求)。"""
        url = f"{COREOBT_PC}/login-otp"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "WPFLauncher/0.0.0.0",
        }

        resp = await self.client.post(
            url, content=sauth_json.encode("utf-8"), headers=headers
        )
        text = resp.text

        try:
            return _parse_json_lenient(text)
        except RuntimeError:
            raise RuntimeError(f"login-otp 响应不是 JSON: {text[:300]}")

    async def _post_authentication_otp(self, auth_json: str) -> dict:
        """POST /authentication-otp - 加密认证,获取 UID + token。

        实测确认: 服务器使用 G79V12 密钥集 (AES-128, hex解码密钥)。
        请求和响应都使用 HttpEncrypt_g79v12 / HttpDecrypt_g79v12。
        (之前误改为常规 KEYS 导致响应解密失败, 已回退)
        """
        url = f"{COREOBT_PC}/authentication-otp"
        encrypted = http_encrypt_g79v12(auth_json.encode("utf-8"))

        headers = {
            "Content-Type": "application/json",
            "User-Agent": "WPFLauncher/0.0.0.0",
            "Accept-Encoding": "identity",
        }

        resp = await self.client.post(url, content=encrypted, headers=headers)
        body = resp.content

        decrypted = http_decrypt_g79v12(body)
        text = decrypted.decode("utf-8", errors="replace")

        try:
            return _parse_json_lenient(text)
        except RuntimeError:
            raise RuntimeError(f"authentication-otp 解密后不是 JSON: {text[:300]}")

    @staticmethod
    def _extract_inner_sauth(sauth_json: str) -> str:
        """从 ``{"sauth_json":"<inner>"}`` 中提取 inner 字符串。

        如果输入不是此格式,则原样返回。
        """
        try:
            data = json.loads(sauth_json)
            if isinstance(data, dict) and "sauth_json" in data:
                return str(data["sauth_json"])
        except (json.JSONDecodeError, TypeError):
            pass
        return sauth_json

    @staticmethod
    def _convert_sauth_to_pc(inner_sauth: str) -> str:
        """将 sauth_json inner 从 PE 格式转换为 PC 格式。

        PE 格式: gameid=g79, platform=android
        PC 格式: gameid=x19, platform=pc, sdk_version=SDK_VERSION_PC

        如果输入已经是 PC 格式, 补充缺失字段后返回。
        如果无法解析, 原样返回。

        重要: 4399com 等 4399 频道的 sauth_json 必须保留 platform=ad,
        否则 login-otp 会返回 code=32 (session 失效)。
        """
        try:
            data = json.loads(inner_sauth)
            if not isinstance(data, dict):
                return inner_sauth

            changed = False

            # 4399 频道 (4399com, 4399pc) 的 platform 必须保留原始值 (通常是 "ad"),
            # 不能改为 "pc", 否则 login-otp 返回 code=32
            login_channel = data.get("login_channel", "") or data.get("app_channel", "")
            is_4399_channel = login_channel.startswith("4399")

            # PE → PC 转换 (仅对非 4399 频道执行 platform 转换)
            if data.get("gameid") != "x19":
                data["gameid"] = "x19"
                changed = True
            if not is_4399_channel and data.get("platform") != "pc":
                data["platform"] = "pc"
                changed = True
            # 防封修复: 不覆盖已有的 sdk_version
            if not data.get("sdk_version"):
                data["sdk_version"] = SDK_VERSION_PC
                changed = True
            # source_platform: 保留原始值, 不强制修改

            if not changed:
                return inner_sauth
            return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        except (json.JSONDecodeError, TypeError):
            return inner_sauth

    # ------------------------------------------------------------------
    # API 网关调用
    # ------------------------------------------------------------------

    async def _api_post(self, path: str, body: dict) -> dict:
        """通过 API 网关发送 POST 请求。

        IL 代码确认: API 网关请求仅包含 3 个头:
        - Content-Type: application/json
        - user-token: ComputeDynamicToken(path, body, LoginSRCToken)
        - user-id: UID (字符串)
        User-Agent 通过 HttpClient 默认头设置。

        关键修复: token 参数使用 LoginSRCToken (解密后的),
        而非 LoginMD5Token 或原始 LoginDToken。
        nemc_check 中 CoreNative.ComputeDynamicToken 内部使用全局 LoginSRCToken。

        防封修复: PC 模式也加密请求体 (与 authentication-otp 一致使用 G79V12)。
        之前 PC 模式发送明文请求体, 服务器无法验证 token, 返回 code=10 "请先登录"。
        token 仍然从明文 body_str 计算 (服务器解密后用明文验证 token)。
        """
        body_str = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        # 使用 LoginSRCToken (已解密) 计算 ComputeDynamicToken
        # token 从明文 body 计算, 服务器解密请求体后用明文验证
        token = compute_dynamic_token(path, body_str, self.login_src_token)

        # 仅设置与 IL 代码一致的头信息
        headers = {
            "Content-Type": "application/json",
            "user-token": token,
            "user-id": self.uid,
            "User-Agent": "WPFLauncher/0.0.0.0",
        }

        url = f"{self.apigatewayobt}{path}"

        # PE 和 PC 模式都使用 G79V12 加密 (与 authentication-otp 一致)
        # 之前 PC 模式发送明文导致 code=10 "请先登录"
        encrypted = http_encrypt_g79v12(body_str.encode("utf-8"))
        resp = await self.client.post(url, content=encrypted, headers=headers)
        # 解密响应 (G79V12 密钥)
        decrypted = http_decrypt_g79v12(resp.content)
        text = decrypted.decode("utf-8", errors="replace")

        try:
            return _parse_json_lenient(text)
        except RuntimeError:
            raise RuntimeError(f"API {path} 响应不是 JSON: {text[:300]}")

    # ------------------------------------------------------------------
    # 认证服务器调用 (auth v2 流程)
    # ------------------------------------------------------------------

    async def _auth_post_encrypted(self, path: str, body: str) -> str:
        """通过认证服务器发送加密 POST 请求 (用于 /authentication-v2)。

        参考源 (nemc_check Http.cs HttpPost): 使用 x19Crypt.HttpEncrypt_g79v12。
        - 加密: HttpEncrypt_g79v12 (G79V12 密钥集, hex解码密钥) → hex 字符串
        - user-token: compute_dynamic_token_auth (H5Token, hex 编码结果)
        - user-id: UID
        """
        # 使用 H5Token 计算动态令牌 (FastBuilder 模式)
        token = compute_dynamic_token_auth(path, body, self.h5token)

        # 使用 G79V12 密钥集加密 (参考 nemc_check HttpPost: x19Crypt.HttpEncrypt_g79v12)
        encrypted = http_encrypt_g79v12(body.encode("utf-8"))
        hex_body = encrypted.hex()

        headers = {
            "Content-Type": "application/json",
            "user-token": token,
            "user-id": self.uid,
            "User-Agent": "WPFLauncher/0.0.0.0",
        }

        url = f"{AUTH_SERVER}{path}"

        resp = await self.client.post(
            url, content=hex_body.encode("utf-8"), headers=headers
        )
        hex_resp = resp.text

        # hex → bytes → HttpDecrypt_g79v12 (G79V12 密钥集) → string
        try:
            resp_bytes = bytes.fromhex(hex_resp.strip())
        except ValueError:
            resp_bytes = hex_resp.encode("utf-8")

        decrypted = http_decrypt_g79v12(resp_bytes)
        return decrypted.decode("utf-8", errors="replace")

    async def _auth_post_plain(self, path: str, body: str) -> str:
        """通过认证服务器发送明文 POST 请求 (用于 get-client-ecdh-key, get-chain-info)。

        FastBuilder 模式: 无 user-token, 无 user-id, 仅 Content-Type。
        """
        headers = {
            "Content-Type": "application/json",
        }

        url = f"{AUTH_SERVER}{path}"

        resp = await self.client.post(
            url, content=body.encode("utf-8"), headers=headers
        )
        return resp.text

    # ------------------------------------------------------------------
    # 租赁服操作
    # ------------------------------------------------------------------

    async def search_rental_server(self, server_code: str) -> dict:
        """搜索租赁服。

        API: POST /rental-server/query/search-by-name
        返回包含 entity_id, name, owner_id 等的服务器信息。
        """
        body = {
            "server_name": server_code,
            "offset": 0,
        }
        data = await self._api_post("/rental-server/query/search-by-name", body)

        code = data.get("code", -1)
        if code != 0:
            raise RuntimeError(
                f"搜索租赁服失败: code={code}, msg={data.get('message', '')}"
            )

        entities = data.get("entities", []) or []
        if not entities:
            raise RuntimeError(f"未找到租赁服: {server_code}")

        # 找到匹配的服务器
        for ent in entities:
            if str(ent.get("name", "")) == server_code:
                return ent

        # 回退到第一个结果
        return entities[0]

    async def enter_rental_server(
        self, server_id: str, password: str = ""
    ) -> str:
        """进入租赁服世界，返回游戏服务器地址 (host:port)。

        API: POST /rental-server-world-enter/get
        """
        body = {
            "server_id": str(server_id),
            "pwd": password or "",
        }
        data = await self._api_post("/rental-server-world-enter/get", body)

        code = data.get("code", -1)
        if code != 0:
            raise RuntimeError(
                f"进入租赁服失败: code={code}, msg={data.get('message', '')}"
            )

        entity = data.get("entity") or {}
        host = str(entity.get("mcserver_host", ""))
        port = entity.get("mcserver_port", 0)

        try:
            port = int(port)
        except (ValueError, TypeError):
            port = 19132

        if not host:
            raise RuntimeError("进入租赁服成功但未返回服务器地址")

        addr = f"{host}:{port}"
        logger.info("租赁服地址: %s", addr)
        return addr

    # ------------------------------------------------------------------
    # Auth V2 流程
    # ------------------------------------------------------------------

    async def get_ecdh_keys(self) -> dict:
        """获取 ECDH 密钥对。

        API: POST /interconn/web/common/get-client-ecdh-key
        FastBuilder 模式: 明文 POST, 无 auth 头, uid 为整数。
        """
        uid_val = int(self.uid) if self.uid.isdigit() else self.uid
        body = json.dumps({"uid": uid_val}, ensure_ascii=False, separators=(",", ":"))
        text = await self._auth_post_plain(
            "/interconn/web/common/get-client-ecdh-key", body
        )

        try:
            data = _parse_json_lenient(text)
        except RuntimeError:
            raise RuntimeError(f"get-client-ecdh-key 响应不是 JSON: {text[:300]}")

        entity = data.get("entity") or data
        public_key = str(entity.get("public_key", ""))
        private_key = str(entity.get("private_key", ""))

        if not public_key or not private_key:
            raise RuntimeError(f"get-client-ecdh-key 未返回密钥对: {text[:300]}")

        return {"public_key": public_key, "private_key": private_key}

    async def generate_auth_v2(
        self,
        ecdh_public_key: str,
        game_type: str = "RentalGame",
        game_id: str = "",
        display_name: str = "",
    ) -> dict:
        """生成 auth v2 数据。

        API: POST /authentication-v2 (加密)
        
        Args:
            ecdh_public_key: ECDH 公钥 (Base64)
            game_type: 游戏类型 ("RentalGame" / "LobbyGame" / "DomainGame")
            game_id: 游戏 ID (租赁服 serverID)
            display_name: 游戏内显示名称 (优先使用, 为空则用 player_name 或 uid)

        返回包含 exp, nbf, chain 等的 auth v2 响应。
        """
        # 构造 Authenticationg79 (PE) 或 Authenticationx19 (PC)
        # patchVersion 为空字符串 (来自 FastBuilder IL 代码)
        # displayName: 优先使用传入的 display_name (如 PT_ 前缀), 其次用 player_name, 最后用 uid
        dn = display_name or self.player_name or self.uid
        if self._api_mode == "pe":
            auth_entity = {
                "bit": "64",
                "clientKey": ecdh_public_key,
                "displayName": dn,
                "engineVersion": ENGINE_VERSION,
                "netease_sid": secrets.token_hex(16),
                "os_name": "android",
                "patchVersion": "",
                "uid": int(self.uid) if self.uid.isdigit() else 0,
            }
        else:
            auth_entity = {
                "bit": "32",
                "clientKey": ecdh_public_key,
                "displayName": dn,
                "engineVersion": ENGINE_VERSION,
                "netease_sid": secrets.token_hex(16),
                "os_name": "windows",
                "patchVersion": "",
                "platform": "pc",
                "uid": int(self.uid) if self.uid.isdigit() else 0,
            }

        # 添加游戏类型和游戏 ID (用于租赁服 auth v2)
        if game_type and game_id:
            auth_entity["game_type"] = game_type
            auth_entity["game_id"] = game_id

        body = json.dumps(auth_entity, ensure_ascii=False, separators=(",", ":"))
        text = await self._auth_post_encrypted("/authentication-v2", body)

        try:
            data = _parse_json_lenient(text)
        except RuntimeError:
            raise RuntimeError(f"authentication-v2 响应不是 JSON: {text[:300]}")

        logger.info("auth v2 数据已生成 (game_type=%s, game_id=%s)", game_type, game_id)
        return data

    async def get_chain_info(
        self, ecdh_keys: dict, auth_v2_data: dict
    ) -> str:
        """获取 chainInfo (JWT 身份链)。

        API: POST /interconn/web/common/get-chain-info
        chainInfo = [chain[0] from get-chain-info, chain[0] from auth-v2, chain[1] from auth-v2]
        """
        chain_v2 = auth_v2_data.get("chain", [])
        exp = auth_v2_data.get("exp", 0)
        nbf = auth_v2_data.get("nbf", 0)

        # 如果 chain 是字符串列表，提取 exp/nbf 从第一个 JWT
        if isinstance(chain_v2, list) and chain_v2:
            first_jwt = str(chain_v2[0])
            try:
                parts = first_jwt.split(".")
                if len(parts) >= 2:
                    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
                    payload = json.loads(
                        base64.urlsafe_b64decode(payload_b64)
                    )
                    exp = payload.get("exp", exp)
                    nbf = payload.get("nbf", nbf)
            except Exception:
                pass

        body = json.dumps(
            {
                "private_key": ecdh_keys["private_key"],
                "public_key": ecdh_keys["public_key"],
                "exp": exp,
                "nbf": nbf,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

        text = await self._auth_post_plain(
            "/interconn/web/common/get-chain-info", body
        )

        try:
            data = _parse_json_lenient(text)
        except RuntimeError:
            raise RuntimeError(f"get-chain-info 响应不是 JSON: {text[:300]}")

        # 组装 chainInfo: 3-element JSON array
        chain_from_get = (data.get("entity") or {}).get("chain", [])
        if isinstance(chain_from_get, str):
            chain_from_get = [chain_from_get]

        chain_v2_list = auth_v2_data.get("chain", [])
        if isinstance(chain_v2_list, str):
            chain_v2_list = [chain_v2_list]

        chain_info_list = []
        if chain_from_get:
            chain_info_list.append(chain_from_get[0])
        if chain_v2_list:
            chain_info_list.append(chain_v2_list[0])
            if len(chain_v2_list) > 1:
                chain_info_list.append(chain_v2_list[1])

        if not chain_info_list:
            raise RuntimeError("未能获取 chainInfo")

        chain_info = json.dumps(chain_info_list, ensure_ascii=False)
        logger.info("chainInfo 已生成 (%d 个 JWT)", len(chain_info_list))
        return chain_info

    # ------------------------------------------------------------------
    # 完整租赁服连接流程
    # ------------------------------------------------------------------

    async def connect_rental_server(
        self,
        server_code: str,
        password: str = "",
        display_name: str = "",
    ) -> DirectAuthResult:
        """完整租赁服连接流程。

        步骤:
            1. 搜索租赁服 → 获取 serverID
            2. 进入租赁服 → 获取 IP:port
            3. 获取 ECDH 密钥
            4. 生成 auth v2 (包含 game_type=RentalGame, game_id=serverID)
            5. 获取 chainInfo

        Args:
            server_code: 租赁服编号
            password: 租赁服密码 (无密码留空)
            display_name: 游戏内显示名称 (如 PT_xxxxxx)

        Returns:
            DirectAuthResult 包含 chain_info 和 server_address
        """
        result = DirectAuthResult(
            uid=self.uid,
            player_name=display_name or self.player_name,
            auth_mode=self._api_mode,
        )

        # 1. 搜索租赁服
        logger.info("搜索租赁服: %s", server_code)
        server_entity = await self.search_rental_server(server_code)
        server_id = str(server_entity.get("entity_id", ""))
        if not server_id:
            raise RuntimeError("搜索租赁服成功但未返回 entity_id")

        result.raw["server_entity"] = server_entity
        logger.info("找到租赁服: serverID=%s", server_id)

        # 2. 进入租赁服
        logger.info("进入租赁服世界...")
        server_address = await self.enter_rental_server(server_id, password)
        result.server_address = server_address

        # 3. 获取 ECDH 密钥
        logger.info("获取 ECDH 密钥对...")
        ecdh_keys = await self.get_ecdh_keys()

        # 4. 生成 auth v2 (包含游戏类型和游戏 ID)
        logger.info("生成 auth v2 数据 (RentalGame, serverID=%s)...", server_id)
        auth_v2_data = await self.generate_auth_v2(
            ecdh_keys["public_key"],
            game_type="RentalGame",
            game_id=server_id,
            display_name=display_name,
        )

        # 5. 获取 chainInfo
        logger.info("获取 chainInfo...")
        chain_info = await self.get_chain_info(ecdh_keys, auth_v2_data)
        result.chain_info = chain_info

        logger.info("租赁服连接流程完成: addr=%s, mode=%s", server_address, self._api_mode)
        return result

    # ------------------------------------------------------------------
    # Cookie / sauth_json 认证
    # ------------------------------------------------------------------

    async def login_with_cookie(self, cookie: str) -> None:
        """通过 cookie 或 sauth_json 登录。

        "cookie" 实际上是 sauth_json 字符串 (来源: Drug.NetEase.Opensource 代码分析)。
        格式为 ``{"sauth_json":"<inner>"}`` 或直接是 inner JSON。

        如果 cookie 看起来像 HTTP Cookie (包含 ``=`` 和 ``;``),
        则尝试从中提取 session 信息构造 sauth_json。

        Args:
            cookie: sauth_json 字符串或 HTTP Cookie 字符串
        """
        logger.info("尝试 cookie/sauth_json 登录...")

        # 判断输入类型
        cookie_stripped = cookie.strip()

        # 如果是 JSON 格式 (sauth_json)
        if cookie_stripped.startswith("{"):
            try:
                data = json.loads(cookie_stripped)
                if isinstance(data, dict):
                    # 检查是否是 {"sauth_json": "..."} 格式
                    if "sauth_json" in data:
                        logger.info("检测到标准 sauth_json 格式")
                        await self.login(cookie_stripped)
                        return
                    # 检查是否包含 sessionid/sdkuid 等 (inner sauth)
                    if "sessionid" in data or "sdkuid" in data:
                        logger.info("检测到内联 sauth 格式,包装为标准格式")
                        wrapped = json.dumps({"sauth_json": cookie_stripped})
                        await self.login(wrapped)
                        return
            except json.JSONDecodeError:
                pass

        # 如果是 HTTP Cookie 格式 (key=value; key=value)
        logger.info("尝试从 HTTP Cookie 构造 sauth_json...")
        inner_sauth = self._build_sauth_from_cookie(cookie_stripped)
        wrapped = json.dumps({"sauth_json": inner_sauth})
        await self.login(wrapped)

    def _build_sauth_from_cookie(self, cookie: str) -> str:
        """从 HTTP Cookie 字符串构造 sauth_json inner。

        尝试从 cookie 中提取 sessionid, sdkuid 等信息。
        """
        # 解析 cookie 键值对
        cookie_parts = {}
        for part in cookie.split(";"):
            part = part.strip()
            if "=" in part:
                key, _, value = part.partition("=")
                cookie_parts[key.strip()] = value.strip()

        sessionid = cookie_parts.get("SESSION", cookie_parts.get("sessionid", ""))
        sdkuid = cookie_parts.get("P_INFO", cookie_parts.get("sdkuid", ""))

        # BUG-1.2 修复: 当 cookie 中缺少 sessionid 时, 不应生成假凭证,
        # 否则后续认证会使用无效 sessionid 而调用方无法察觉。改为抛出异常。
        if not sessionid:
            raise ValueError(
                "cookie 中缺少 SESSION/sessionid, 无法构造有效凭证"
            )

        # 构造 sauth_json inner (始终使用 PC 格式, 因为 login() 只走 PC 认证)
        inner = json.dumps({
            "gameid": "x19",
            "login_channel": "netease",
            "app_channel": "netease",
            "platform": "pc",
            "sdkuid": sdkuid or secrets.token_hex(8),
            "sessionid": sessionid,
            "udid": secrets.token_hex(16),
            "deviceid": str(uuid.uuid4()),
            "sdk_version": SDK_VERSION_PC,
            "aim_info": json.dumps({
                "aim": "127.0.0.1",
                "country": "CN",
                "tz": "+0800",
                "tzid": "",
            }),
            "client_login_sn": secrets.token_hex(16).upper(),
            "gas_token": "",
            "source_platform": "pc",
            "ip": "127.0.0.1",
        }, ensure_ascii=False, separators=(",", ":"))

        return inner

    # ------------------------------------------------------------------
    # 获取用户详情
    # ------------------------------------------------------------------

    async def get_user_detail(self) -> dict:
        """获取用户详情 (昵称、等级等)。

        API: POST /user-detail
        防封修复: 使用 G79V12 加密请求体 (与 _api_post 保持一致),
        之前发送明文空 body 导致 code=10 "请先登录"。
        """
        path = "/user-detail"
        # 关键修复: 使用 LoginSRCToken (解密后的) 计算 token,
        # 与 _api_post 保持一致 (而非 login_md5_token)
        token = compute_dynamic_token(path, "", self.login_src_token)

        headers = {
            "Content-Type": "application/json",
            "user-token": token,
            "user-id": self.uid,
            "X_TRACE_ID": generate_trace_id(),
            "User-Agent": "WPFLauncher/0.0.0.0",
        }

        url = f"{self.apigatewayobt}{path}"

        # 防封修复: 加密请求体 (G79V12), 与 _api_post 保持一致
        encrypted = http_encrypt_g79v12(b"")
        resp = await self.client.post(url, content=encrypted, headers=headers)
        # 解密响应 (G79V12 密钥)
        decrypted = http_decrypt_g79v12(resp.content)
        text = decrypted.decode("utf-8", errors="replace")

        try:
            data = _parse_json_lenient(text)
            entity = data.get("entity") or {}
            self.player_name = str(entity.get("name", ""))
            return data
        except (RuntimeError, AttributeError):
            return {}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def generate_sauth_json(
    device_model: str = "",
    platform: str = "",
    mode: str = "pc",
    sdkuid: str = "",
    sessionid: str = "",
) -> str:
    """生成 sauth_json 字符串。

    构造一个基本的设备认证 JSON，用于登录。
    参考: Drug.NetEase.Opensource 源码中的 sauth_json 格式。

    Args:
        device_model: 设备型号 (PC 模式不需要)
        platform: 平台 (为空时根据 mode 自动选择: pe→android, pc→pc)
        mode: 模式 (pe/pc)
        sdkuid: SDK 用户 ID (为空时自动生成)
        sessionid: 会话 ID (为空时自动生成)

    Returns:
        sauth_json 字符串，格式为 ``{"sauth_json":"<inner>"}``
    """
    if not platform:
        platform = "android" if mode == "pe" else "pc"

    if not sdkuid:
        sdkuid = secrets.token_hex(8)
    if not sessionid:
        sessionid = secrets.token_hex(16)

    udid = secrets.token_hex(16)
    deviceid = str(uuid.uuid4())

    inner = json.dumps({
        "gameid": "g79" if mode == "pe" else "x19",
        "login_channel": "netease",
        "app_channel": "netease",
        "platform": platform,
        "sdkuid": sdkuid,
        "sessionid": sessionid,
        "sdk_version": SDK_VERSION_PC if mode == "pc" else SDK_VERSION_PE,
        "udid": udid,
        "deviceid": deviceid,
        "aim_info": json.dumps({
            "aim": "127.0.0.1",
            "country": "CN",
            "tz": "+0800",
            "tzid": "",
        }),
        "client_login_sn": secrets.token_hex(16).upper(),
        "gas_token": "",
        "source_platform": platform,
        "ip": "127.0.0.1",
    }, ensure_ascii=False, separators=(",", ":"))

    return json.dumps({"sauth_json": inner}, ensure_ascii=False)
