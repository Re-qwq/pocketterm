"""
自建接入点 - Go二进制版 + 网易直连认证

通过调用 pocketterm_ap Go二进制程序实现真正的MCBE协议连接。
Python端负责认证(网易直连/Fatalder/cookie)，Go端负责RakNet连接+MCPE登录。

认证方式优先级:
    1. 网易直连认证 (有 cookie 或 sauth_json 时) - 直接与网易服务器通信
    2. Fatalder API (有 api_key 时) - 通过Fatalder认证服务器
    3. Go fbauth 模式 (回退) - 通过Go二进制的内置认证
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .base import AccessPoint, AccessPointInfo, AccessPointStatus

logger = logging.getLogger("pocketterm.access_point.custom")


def _find_go_binary() -> Optional[str]:
    """查找 pocketterm_ap 二进制文件"""
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / "access_point_go" / "pocketterm_ap",
        Path(__file__).resolve().parent.parent.parent / "data" / "access_points" / "pocketterm_ap",
        "pocketterm_ap",
    ]

    for path in candidates:
        p = Path(path)
        if p.exists() and p.is_file():
            try:
                os.chmod(str(p), 0o755)
                return str(p)
            except Exception:
                pass

    return None


def _extract_account_id_from_config(config: dict) -> str:
    """从接入点配置中提取账号 ID (sdkuid)。

    用于 ChainInfo 缓存键和设备指纹索引。
    优先从 sauth_json 中解析 sdkuid, 其次从 cookie 中提取。
    """
    sauth_json = config.get("sauth_json", "")
    cookie = config.get("cookie", "")
    try:
        if sauth_json:
            raw = sauth_json.strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                inner = data.get("sauth_json", raw)
                if isinstance(inner, str):
                    inner = json.loads(inner)
                sdkuid = str(inner.get("sdkuid", ""))
                if sdkuid:
                    return sdkuid
        if cookie:
            raw = cookie.strip()
            if raw.startswith("{"):
                data = json.loads(raw)
                inner = data.get("sauth_json", raw)
                if isinstance(inner, str):
                    inner = json.loads(inner)
                sdkuid = str(inner.get("sdkuid", ""))
                if sdkuid:
                    return sdkuid
    except (json.JSONDecodeError, TypeError):
        pass
    return config.get("account_id", "")


class CustomAccessPoint(AccessPoint):
    """自建接入点 - 通过Go二进制实现真正的MCBE协议连接。

    支持三种认证方式:
        1. 网易直连认证: 直接与网易服务器通信 (login-otp → rental server → auth v2)
        2. Fatalder API: 通过Fatalder认证服务器获取chainInfo
        3. Go fbauth: Go二进制内置的FastBuilder认证 (回退)
    """

    launch_type: str = "Custom"

    def __init__(self, config: dict, status_callback=None):
        super().__init__(config, status_callback=status_callback)
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stdin: Optional[asyncio.StreamWriter] = None
        self._stdout: Optional[asyncio.StreamReader] = None
        self._stderr: Optional[asyncio.StreamReader] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._binary_path: Optional[str] = _find_go_binary()
        self._connected = False
        self._spawned = False
        self._event_handlers: dict[str, list] = {}

    def on(self, event: str, handler: Callable) -> None:
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    async def on_packet(self, handler) -> None:
        self.register_packet_handler(handler)

    async def _emit(self, event: str, *args, **kwargs) -> None:
        for handler in self._event_handlers.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                else:
                    handler(*args, **kwargs)
            except Exception as e:
                logger.error(f"事件处理器错误 ({event}): {e}")

    # ------------------------------------------------------------------
    # 启动流程
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """启动Go接入点进程。

        认证流程:
            1. 根据配置选择认证方式 (direct / fatalder / cookie / fbauth)
            2. 执行认证获取 chainInfo + server_address
            3. 将认证结果传给Go二进制 (pre_auth模式)
            4. Go二进制用RakNet连接游戏服务器 + MCPE登录
        """
        if not self._binary_path:
            msg = (
                "找不到 pocketterm_ap 二进制文件！\n"
                "请确保文件位于 PocketTerm/access_point_go/pocketterm_ap"
            )
            self._log(msg, "error")
            self.info.status = AccessPointStatus.CRASHED
            return False

        self._log(f"正在启动Go接入点: {self._binary_path}", "info")
        self.info.status = AccessPointStatus.LAUNCHING

        # C-1 修复: 提取账号 ID 并获取设备指纹
        account_id = _extract_account_id_from_config(self.config)

        # Go二进制配置 (基础字段)
        go_config = {
            "bot_name": self.config.get("bot_name", ""),
            "device_model": self.config.get("device_model", "Xiaomi 13"),
        }

        # C-1 修复: 传递完整设备指纹到 Go 端
        # (之前只传 bot_name + device_model, Go 端每次随机生成 DeviceID/ClientRandomID,
        #  导致网易每次看到的都是"新设备", 4 次后判定异常 → 封号)
        if account_id:
            try:
                from ..auth.device_fingerprint import get_fingerprint_manager
                fp_mgr = get_fingerprint_manager()
                fp = fp_mgr.get_by_account(account_id)
                if fp is None:
                    fp = fp_mgr.get_or_create(account_id)
                # 传递所有设备指纹字段到 Go 配置
                go_config["device_id"] = fp.device_id
                go_config["client_random_id"] = fp.client_random_id
                go_config["player_uuid"] = fp.uuid
                go_config["device_os"] = fp.device_os
                go_config["game_version"] = fp.game_version
                go_config["language_code"] = fp.language_code
                go_config["current_input_mode"] = fp.current_input_mode
                go_config["default_input_mode"] = fp.default_input_mode
                go_config["ui_profile"] = fp.ui_profile
                self._log(
                    f"设备指纹已注入 Go 配置: DevId={fp.device_id[:8]}.. "
                    f"OS={fp.device_os} UUID={fp.uuid[:8]}..",
                    "debug",
                )
            except Exception as e:
                self._log(f"获取设备指纹失败 (Go 将使用默认值): {e}", "warning")

        server_code = self.config.get("server_code", "")
        server_password = self.config.get("server_password", "")
        auth_method = self.config.get("auth_method", "auto")
        cookie = self.config.get("cookie", "")
        sauth_json = self.config.get("sauth_json", "")
        api_key = self.config.get("api_key", "")

        # 辅助: 将 https:// URL 转换为 wss:// (Go 的 fbauth 模式需要 WebSocket URL)
        def _to_ws_url(url: str) -> str:
            if url.startswith("https://"):
                return "wss://" + url[len("https://"):]
            if url.startswith("http://"):
                return "ws://" + url[len("http://"):]
            return url

        # 根据认证方式选择流程
        auth_result = None

        if server_code and server_code != "custom":
            # C-2 修复 (Scheme A): 先检查 ChainInfo 缓存, 命中则跳过认证
            try:
                from ..auth.chain_cache import get_chain_cache
                cache = get_chain_cache()
                cached = cache.get_valid_chain(account_id, server_code)
                if cached:
                    self._log(
                        f"ChainInfo 缓存命中 (Scheme A - 零认证轻量重连), "
                        f"跳过网易认证, age={cached.age_seconds():.0f}s",
                        "success",
                    )
                    go_config["mode"] = "pre_auth"
                    go_config["chain_info"] = cached.chain_info
                    go_config["server_address"] = cached.server_address

                    # 跳过认证, 直接启动 Go 进程
                    config_json = json.dumps(go_config) + "\n"
                    return await self._launch_go_process(config_json)
            except Exception as e:
                self._log(f"ChainInfo 缓存检查失败 (继续正常认证): {e}", "warning")

            # 正常认证流程 (缓存未命中或过期)
            self._log(f"准备执行Python认证, auth_method={auth_method}", "info")
            try:
                auth_result = await self._authenticate(
                    auth_method, server_code, server_password
                )
                self._log(f"Python认证完成, 结果: {'成功' if auth_result else '失败'}", "info")
            except Exception as auth_exc:
                self._log(f"Python认证异常: {auth_exc}", "error")
                auth_result = None

            if auth_result and auth_result.get("chain_info") and auth_result.get("server_address"):
                # 认证成功，使用预认证模式
                self._log(f"认证成功! 服务器: {auth_result['server_address']}", "info")
                go_config["mode"] = "pre_auth"
                go_config["chain_info"] = auth_result["chain_info"]
                go_config["server_address"] = auth_result["server_address"]

                # C-2 修复: 缓存认证结果供下次轻量重连使用
                if account_id and auth_result.get("_cache_data"):
                    try:
                        from ..auth.chain_cache import get_chain_cache
                        cache_data = auth_result["_cache_data"]
                        get_chain_cache().update(
                            account_id, server_code, **cache_data
                        )
                        self._log("认证结果已缓存 (下次重连可用 Scheme A/B)", "info")
                    except Exception as e:
                        self._log(f"缓存认证结果失败: {e}", "warning")
            else:
                # 认证失败，回退到Go的fbauth模式
                self._log("Python认证失败，回退到Go直接认证模式", "warning")
                go_config["server_code"] = server_code
                go_config["server_password"] = server_password
                go_config["auth_server"] = _to_ws_url(
                    self.config.get("auth_server", "https://nv1.nethard.pro")
                )
                go_config["fb_token"] = self.config.get("fb_token", "")
                go_config["username"] = self.config.get("username", "")
                go_config["password"] = self.config.get("password", "")
        elif server_code == "custom":
            # 自定义服务器 (直接 IP:Port, 无需网易认证)
            go_config["server_code"] = "custom"
            go_config["server_address"] = self.config.get("server_address", "")
            go_config["server_port"] = self.config.get("server_port", 19132)
            go_config["server_password"] = server_password
            go_config["auth_server"] = _to_ws_url(
                self.config.get("auth_server", "https://nv1.nethard.pro")
            )
            go_config["fb_token"] = self.config.get("fb_token", "")
            go_config["username"] = self.config.get("username", "")
            go_config["password"] = self.config.get("password", "")
        else:
            # server_code 为空 - 检查是否有认证凭证
            if cookie or sauth_json:
                self._log(
                    "缺少服务器编号 (server_code)。"
                    "请在创建机器人时指定要连接的租赁服编号 (如 '123456')。"
                    "如使用自定义服务器, 请将 server_code 设为 'custom' 并提供 server_address。",
                    "error",
                )
                self.info.status = AccessPointStatus.CRASHED
                return False
            # 无认证凭证, 使用 Go 的 fbauth 模式
            self._log("无认证凭证, 使用Go fbauth模式", "warning")
            go_config["server_code"] = server_code
            go_config["server_password"] = server_password
            go_config["auth_server"] = _to_ws_url(
                self.config.get("auth_server", "https://nv1.nethard.pro")
            )
            go_config["fb_token"] = self.config.get("fb_token", "")
            go_config["username"] = self.config.get("username", "")
            go_config["password"] = self.config.get("password", "")

        config_json = json.dumps(go_config) + "\n"
        return await self._launch_go_process(config_json)

    async def _launch_go_process(self, config_json: str) -> bool:
        """启动 Go 接入点进程并发送配置 (C-2 重构: 抽取为独立方法)。

        Args:
            config_json: Go 二进制的启动配置 (JSON + 换行)。

        Returns:
            True 表示进程已成功启动, False 表示失败。
        """
        try:
            self._process = await asyncio.create_subprocess_exec(
                self._binary_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stdin = self._process.stdin
            self._stdout = self._process.stdout
            self._stderr = self._process.stderr

            self._log("Go接入点进程已启动，发送配置...", "info")

            self._stdin.write(config_json.encode())
            await self._stdin.drain()

            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._read_stderr())

            self.info.status = AccessPointStatus.RUNNING
            self.info.started_at = time.time()
            return True

        except Exception as e:
            self._log(f"启动Go接入点失败: {e}", "error")
            self.info.status = AccessPointStatus.CRASHED
            logger.exception("启动接入点失败")
            return False

    # ------------------------------------------------------------------
    # 认证逻辑
    # ------------------------------------------------------------------

    async def _authenticate(
        self, auth_method: str, server_code: str, server_password: str
    ) -> Optional[dict]:
        """根据配置的认证方式执行认证。

        Args:
            auth_method: 认证方式 (auto/direct/fatalder/cookie/fbauth)
            server_code: 服务器编号
            server_password: 服务器密码

        Returns:
            包含 chain_info 和 server_address 的字典，失败返回 None
        """
        cookie = self.config.get("cookie", "")
        sauth_json = self.config.get("sauth_json", "")
        api_key = self.config.get("api_key", "")

        # 调试日志: 确认认证凭证是否正确加载
        self._log(
            f"认证配置: auth_method={auth_method}, "
            f"cookie={'有' if cookie else '无'}({len(cookie)}字符), "
            f"sauth_json={'有' if sauth_json else '无'}({len(sauth_json)}字符), "
            f"api_key={'有' if api_key else '无'}",
            "info",
        )

        # auto 模式: 自动选择最佳认证方式
        if auth_method == "auto":
            # 优先级: cookie > sauth_json > api_key(fatalder) > fbauth
            if cookie:
                auth_method = "cookie"
            elif sauth_json:
                auth_method = "direct"
            elif api_key:
                auth_method = "fatalder"
            else:
                # 无认证凭证，使用Go的fbauth模式
                return None

        # 执行认证
        if auth_method == "direct" or auth_method == "cookie":
            result = await self._auth_netease_direct(
                server_code, server_password, cookie, sauth_json
            )
            if result:
                return result
            # 直连失败，尝试Fatalder
            if api_key:
                self._log("直连认证失败，尝试Fatalder API...", "info")
                return await self._auth_fatalder(server_code, server_password, api_key)
            return None

        elif auth_method == "fatalder":
            return await self._auth_fatalder(server_code, server_password, api_key)

        else:
            # fbauth 模式 - 由Go二进制处理
            return None

    async def _auth_netease_direct(
        self,
        server_code: str,
        server_password: str,
        cookie: str = "",
        sauth_json: str = "",
    ) -> Optional[dict]:
        """通过网易直连认证模块完成认证。

        流程:
            1. 登录 (cookie 或 sauth_json)
            2. 连接租赁服 (搜索 → 进入 → auth v2)
            3. 返回 chainInfo + server_address

        日志级别:
            protocol - 协议步骤 (青色)
            success  - 成功 (绿色)
            error    - 失败 (红色)
            warning  - 警告 (黄色)
            info     - 普通 (白色)
        """
        account_id = ""  # 预初始化, 防止 except 块中 NameError
        try:
            from ..auth.netease_direct import NeteaseDirectClient, generate_sauth_json
            from ..auth.chain_cache import get_chain_cache

            mode = "pc"  # 始终使用 PC 认证 (PE 需要 PESignCount 原生签名,无法实现)

            # C-2 Scheme B: 检查是否有缓存的 LoginSRCToken (跳过 /login-otp)
            account_id = _extract_account_id_from_config(self.config)
            cached_token = None
            if account_id:
                cached_token = get_chain_cache().get_valid_token(
                    account_id, server_code
                )

            async with NeteaseDirectClient(mode=mode, timeout=60.0) as client:
                if cached_token:
                    # Scheme B: 用缓存的 LoginSRCToken, 跳过 /login-otp
                    self._log(
                        f"Scheme B: 使用缓存 Token 跳过 /login-otp, "
                        f"UID={cached_token.uid}, age={cached_token.age_seconds():.0f}s",
                        "info",
                    )
                    import base64 as _b64
                    client.set_login_state(
                        uid=cached_token.uid,
                        login_src_token=cached_token.login_src_token,
                        login_md5_token=cached_token.login_md5_token,
                        player_name=cached_token.player_name,
                        h5token_b64=cached_token.h5token,
                    )
                else:
                    # 正常登录 (Scheme C: 全量认证)
                    if cookie:
                        self._log("正在使用 Cookie 向网易认证服务器发送登录请求 (/login-otp)...", "protocol")
                        await client.login_with_cookie(cookie)
                    elif sauth_json:
                        self._log("正在使用 sauth_json 向网易认证服务器发送登录请求 (/login-otp)...", "protocol")
                        await client.login(sauth_json)
                    else:
                        self._log("正在自动生成 sauth_json 并向网易发送登录请求...", "protocol")
                        generated_sauth = generate_sauth_json(mode=mode)
                        await client.login(generated_sauth)

                    self._log(f"网易登录成功! UID: {client.uid}", "success")

                # 获取用户昵称 (Scheme B 也可能需要)
                if not client.player_name:
                    self._log("正在获取用户详细信息...", "protocol")
                    try:
                        await client.get_user_detail()
                        if client.player_name:
                            self._log(f"用户昵称: {client.player_name}", "success")
                    except Exception:
                        self._log("获取用户详情失败 (不影响后续流程)", "warning")

                # 步骤2: 连接租赁服
                bot_name = self.config.get("bot_name", "")
                display_name = bot_name or (f"PT_{client.player_name}" if client.player_name else f"PT_{client.uid}")

                self._log(f"正在向网易 API 网关搜索租赁服 {server_code}...", "protocol")
                self._log(f"显示名称: {display_name}", "info")

                result = await client.connect_rental_server(
                    server_code, server_password, display_name=display_name
                )

                self._log("已找到并进入租赁服!", "success")
                self._log(f"服务器地址: {result.server_address}", "info")
                self._log("正在获取 ECDH 密钥协商参数...", "protocol")
                self._log("正在生成身份链 (authentication-v2)...", "protocol")
                self._log("正在获取 chainInfo...", "protocol")
                self._log("认证流程全部完成! 机器人准备进入游戏世界...", "success")

                # C-2: 准备缓存数据 (供下次 Scheme A/B 使用)
                import base64 as _b64
                h5token_b64 = ""
                try:
                    if client.h5token:
                        h5token_b64 = _b64.b64encode(client.h5token).decode("ascii")
                except Exception:
                    pass

                cache_data = {
                    "chain_info": result.chain_info,
                    "server_address": result.server_address,
                    "login_src_token": client.login_src_token,
                    "login_md5_token": client.login_md5_token,
                    "uid": client.uid,
                    "player_name": client.player_name,
                    "h5token": h5token_b64,
                }

                return {
                    "chain_info": result.chain_info,
                    "server_address": result.server_address,
                    "_cache_data": cache_data,
                    "_scheme": "B" if cached_token else "C",
                }

        except Exception as e:
            err_msg = str(e)
            if "code=29" in err_msg or "封禁" in err_msg:
                self._log("账号已被网易封禁 (code=29)！请更换账号后重试", "error")
                # C-2: 封禁时清除缓存
                if account_id:
                    try:
                        get_chain_cache().invalidate(account_id, server_code)
                    except Exception:
                        pass
            elif "code=32" in err_msg:
                self._log("网易服务器返回错误 (code=32)：可能是设备信息不匹配或服务器维护中", "error")
            elif "code=" in err_msg:
                self._log(f"网易认证失败: {err_msg}", "error")
            else:
                self._log(f"网易直连认证异常: {err_msg}", "error")
            logger.exception("网易直连认证失败")
            return None

    async def _auth_fatalder(
        self, server_code: str, server_password: str, api_key: str
    ) -> Optional[dict]:
        """通过Fatalder API完成认证。"""
        if not api_key:
            return None

        try:
            from ..auth.mc_auth import AuthClient, DeviceFingerprint

            auth_server = self.config.get("auth_server", "https://nv1.nethard.pro")
            if auth_server.startswith("wss://"):
                auth_server = "https://" + auth_server[len("wss://"):]

            device = DeviceFingerprint.generate(
                device_model=self.config.get("device_model", "Xiaomi 13")
            )

            self._log(f"正在通过Fatalder认证服务器号 {server_code}...", "info")

            async with AuthClient(auth_server=auth_server) as client:
                auth_result = await client.login(
                    server_code=server_code,
                    server_password=server_password,
                    fingerprint=device,
                    api_key=api_key,
                )

            if auth_result.chain_info and auth_result.rental_server_ip:
                self._log(f"Fatalder认证成功! 服务器: {auth_result.rental_server_ip}", "info")
                return {
                    "chain_info": auth_result.chain_info,
                    "server_address": auth_result.rental_server_ip,
                }

        except Exception as e:
            self._log(f"Fatalder认证失败: {e}", "error")
            logger.exception("Fatalder认证失败")

        return None

    # ------------------------------------------------------------------
    # 进程通信
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        """读取Go程序stdout的JSON输出"""
        while self._process and self._process.returncode is None:
            try:
                line = await self._stdout.readline()
                if not line:
                    break

                # Bug 13.2 修复: line.decode() 未指定 errors 参数, 遇到非法
                # UTF-8 字节会抛 UnicodeDecodeError 导致接收循环崩溃。加 errors="replace"。
                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    self._log(f"无法解析Go输出: {line_str}", "warning")
                    continue

                await self._handle_message(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"读取Go输出错误: {e}", "error")
                break

        self._connected = False
        if self.info.status == AccessPointStatus.RUNNING:
            self.info.status = AccessPointStatus.DISCONNECTED
            # Bug 13.3 修复: _read_loop 退出时仅设置状态, 未触发事件通知上层。
            # 对比 pure_python.py 会在断开时 emit "disconnected" 事件, 这里补上,
            # 避免上层在等待数据时不知连接已断。
            await self._emit("event", "disconnected", {})

    async def _read_stderr(self) -> None:
        """读取Go程序stderr（调试用）"""
        while self._process and self._process.returncode is None:
            try:
                line = await self._stderr.readline()
                if not line:
                    break
                # Bug 13.2 修复: 同 _read_loop, stderr 也可能包含非法 UTF-8 字节。
                stderr_str = line.decode(errors="replace").strip()
                if stderr_str:
                    logger.debug(f"[Go stderr] {stderr_str}")
            except asyncio.CancelledError:
                break
            except Exception:
                break

    async def _handle_message(self, msg: dict) -> None:
        """处理来自Go程序的消息"""
        msg_type = msg.get("type", "")

        if msg_type == "log":
            level = msg.get("level", "info")
            message = msg.get("message", "")
            self._log(message, level)

        elif msg_type == "event":
            name = msg.get("name", "")
            data = msg.get("data", {})

            if name == "connected":
                self._connected = True
                self._log("已连接到游戏服务器", "info")

            elif name == "spawn":
                self._spawned = True
                bot_name = data.get("bot_name", "")
                self._log(f"机器人 {bot_name} 已成功进入游戏！", "info")

            elif name == "player_join":
                player = data.get("player_name", "")
                self._log(f"玩家 {player} 加入了游戏", "info")

            elif name == "player_leave":
                player = data.get("player_name", "")
                self._log(f"玩家 {player} 离开了游戏", "info")

            elif name == "system_message":
                message = data.get("message", "")
                self._log(f"[系统] {message}", "info")

            await self._emit("event", name, data)

        elif msg_type == "chat":
            sender = msg.get("data", {}).get("sender", "")
            message = msg.get("data", {}).get("message", "")
            self._log(f"[聊天] {sender}: {message}", "info")
            await self._emit("chat", sender, message)

        elif msg_type == "command_output":
            output = msg.get("data", {})
            self._log(f"[命令输出] {output}", "info")
            await self._emit("command_output", output)

        elif msg_type == "error":
            message = msg.get("message", "未知错误")
            detail = msg.get("detail", "")
            self._log(f"{message}: {detail}", "error")
            await self._emit("error", message, detail)

            # 检测封禁关键词
            ban_keywords = ["封禁", "ban", "banned", "禁用", "禁止登录"]
            full_msg = f"{message} {detail}".lower()
            if any(kw in full_msg for kw in ban_keywords):
                self._log("检测到封禁/踢出！停止重连", "error")
                self.info.status = AccessPointStatus.CRASHED
                await self._emit("ban", message, detail)

        elif msg_type == "fb_token":
            token = msg.get("token", "")
            if token:
                self._log("获取到FBToken", "info")
                self.config["fb_token"] = token

    # ------------------------------------------------------------------
    # 游戏操作
    # ------------------------------------------------------------------

    async def send_command(self, command: str) -> bool:
        if not self._stdin or self._process is None or self._process.returncode is not None:
            self._log("无法发送命令：接入点未运行", "error")
            return False

        msg = {"type": "command", "data": {"command": command}}
        try:
            self._stdin.write((json.dumps(msg) + "\n").encode())
            await self._stdin.drain()
            return True
        except Exception as e:
            self._log(f"发送命令失败: {e}", "error")
            return False

    async def send_chat(self, message: str) -> bool:
        if not self._stdin or self._process is None or self._process.returncode is not None:
            return False

        msg = {"type": "chat", "data": {"message": message}}
        try:
            self._stdin.write((json.dumps(msg) + "\n").encode())
            await self._stdin.drain()
            return True
        except Exception as e:
            self._log(f"发送聊天失败: {e}", "error")
            return False

    async def send_packet(self, packet: dict) -> bool:
        if not self._stdin or self._process is None or self._process.returncode is not None:
            return False

        msg = {"type": "packet", "data": packet}
        try:
            self._stdin.write((json.dumps(msg) + "\n").encode())
            await self._stdin.drain()
            return True
        except Exception as e:
            self._log(f"发送数据包失败: {e}", "error")
            return False

    async def move_to(self, x: float, y: float, z: float) -> bool:
        if not self._stdin or self._process is None or self._process.returncode is not None:
            return False

        msg = {"type": "move", "data": {"x": x, "y": y, "z": z}}
        try:
            self._stdin.write((json.dumps(msg) + "\n").encode())
            await self._stdin.drain()
            return True
        except Exception as e:
            self._log(f"移动失败: {e}", "error")
            return False

    async def disconnect(self) -> bool:
        if not self._stdin or self._process is None or self._process.returncode is not None:
            return False

        msg = {"type": "disconnect"}
        try:
            self._stdin.write((json.dumps(msg) + "\n").encode())
            await self._stdin.drain()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """停止Go接入点进程"""
        self._log("正在停止Go接入点...", "info")

        if self._stdin and self._process and self._process.returncode is None:
            try:
                await self.disconnect()
                await asyncio.sleep(0.5)
            except Exception:
                pass

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
            self._stderr_task = None

        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=3)
            except asyncio.TimeoutError:
                self._process.kill()
                # Bug 13.1 修复: 之前 SIGKILL 后的 wait() 无超时, 若进程不响应
                # SIGKILL (如僵尸进程) 会永久阻塞。增加超时保护。
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    self._log(
                        f"子进程未响应 SIGKILL (PID={self._process.pid})",
                        "error",
                    )
            except Exception:
                pass

        self._process = None
        self._stdin = None
        self._stdout = None
        self._stderr = None
        self._connected = False
        self._spawned = False
        self.info.status = AccessPointStatus.DISCONNECTED
        self._log("Go接入点已停止", "info")

    def get_status(self) -> AccessPointInfo:
        self.info.is_connected = self._connected
        self.info.is_spawned = self._spawned
        if self._process:
            self.info.is_running = self._process.returncode is None
        else:
            self.info.is_running = False
        return self.info

    def _log(self, message: str, level: str = "info") -> None:
        timestamp = time.strftime("%H:%M:%S")
        icons = {
            "info": "✅",
            "error": "❌",
            "warning": "⚠️",
            "debug": "🔍",
            "protocol": "🔐",
            "success": "🎉",
        }
        icon = icons.get(level, "ℹ️")
        print(f"[{timestamp}] {icon} [接入点] {message}", flush=True)

        # 错误级别时同步写入 info.last_error, 供上层 (bot.py) 读取
        if level == "error":
            self.info.last_error = message
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        elif level == "debug":
            logger.debug(message)
        else:
            # info / protocol / success 统一用 info 级别记录
            logger.info(message)
