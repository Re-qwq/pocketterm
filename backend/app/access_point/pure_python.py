"""纯Python接入点 - 不依赖任何外部二进制

直接使用 PocketTerm 内置的协议层 (app.protocol) 实现 Minecraft Bedrock 连接。
完全自主可控，不依赖 NeOmega / FateArk / Go 二进制，不需要外部认证服务器。

支持的连接方式:
    1. 租赁服 (服务器号 + 密码)
    2. 联机大厅 (服务器号)
    3. 自定义服务器 (IP + 端口)

认证方式:
    使用 PocketTerm 的网易直连认证 (sauth_json / cookie)，
    通过 protocol.jwt_chain 构建 JWT 登录链。

逆向来源:
    - NovaBuilder_windows_amd64.exe (PhoenixBuilder/StarShuttler)
    - DependencyLibrary-main (neomega-core + FateArk)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from .base import (
    AccessPoint,
    AccessPointInfo,
    AccessPointStatus,
    AccountBannedError,
    Colors,
    LoginFailedError,
    NetworkError,
    PacketHandler,
)
from ..constants.minecraft import GameVersion

logger = logging.getLogger("pocketterm.access_point.pure_python")


class PurePythonAccessPoint(AccessPoint):
    """纯Python接入点 - 直接使用内置协议层连接 Minecraft 服务器。

    与 CustomAccessPoint (依赖Go二进制) 和 NeOmegaAccessPoint (依赖neomega二进制) 不同,
    PurePythonAccessPoint 完全使用 Python 实现的 RakNet + JWT + NBT 协议栈,
    不需要任何外部二进制文件或认证服务器。

    认证流程:
        1. 使用 PocketTerm 的网易直连认证获取 chainInfo + server_address
        2. 用 protocol.jwt_chain.build_login_chain() 构建 JWT 登录链
        3. 用 protocol.raknet.RakNetConnection 建立 UDP 连接
        4. 用 protocol.connection.BedrockClient 完成完整登录

    连接方式:
        - 租赁服: server_code + server_password
        - 自定义: server_address (IP:Port)
    """

    launch_type: str = "PurePython"
    binary_name_patterns: list[str] = []  # 不需要二进制
    default_start_port: int = 0  # 不需要绑定端口

    def __init__(self, config: dict, status_callback=None):
        super().__init__(config, status_callback=status_callback)

        # BedrockClient 实例
        self._client: Optional[Any] = None  # BedrockClient
        self._connected = False
        self._spawned = False
        self._event_handlers: dict[str, list[Callable]] = {}
        self._recv_task: Optional[asyncio.Task] = None

        # 认证结果缓存
        self._chain_info: Optional[str] = None
        self._server_address: Optional[str] = None

    def on(self, event: str, handler: Callable) -> None:
        """注册事件处理器。"""
        if event not in self._event_handlers:
            self._event_handlers[event] = []
        self._event_handlers[event].append(handler)

    async def on_packet(self, handler: PacketHandler) -> None:
        """注册数据包处理器。"""
        self.register_packet_handler(handler)

    async def _emit(self, event: str, *args, **kwargs) -> None:
        """触发事件。"""
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

    async def start(self) -> None:
        """启动纯Python接入点。

        流程:
            1. 网易直连认证 (获取 chainInfo + server_address)
            2. 创建 BedrockClient 并连接服务器
            3. 等待游戏开始 (StartGame)
            4. 启动后台接收循环
        """
        self.update_status(AccessPointStatus.LAUNCHING)
        self.info.started_at = time.time()

        # Step 1: 认证
        server_code = self.config.get("server_code", "")
        server_password = self.config.get("server_password", "")
        server_address = self.config.get("server_address", "")

        auth_result = await self._authenticate(server_code, server_password)
        if not auth_result:
            self._log("认证失败", "error")
            if not self.info.last_error:
                self.info.last_error = "认证失败 (请检查账号凭证或服务器状态)"
            self.update_status(AccessPointStatus.CRASHED)
            return

        self._chain_info = auth_result.get("chain_info")
        self._server_address = auth_result.get("server_address") or server_address

        if not self._server_address:
            self._log("未获取到服务器地址", "error")
            self.info.last_error = "未获取到服务器地址"
            self.update_status(AccessPointStatus.CRASHED)
            return

        # 解析服务器地址
        host, port = self._parse_server_address(self._server_address)

        # Step 2: 创建客户端并连接
        try:
            from ..protocol.connection import BedrockClient, LoginError, DisconnectError
            from ..protocol.jwt_chain import build_login_chain
        except ImportError as e:
            self._log(f"协议模块导入失败: {e}", "error")
            self.update_status(AccessPointStatus.CRASHED)
            return

        # 使用原始 sauth_json (不是从 chain_info 提取)
        sauth_json = self.config.get("sauth_json", "") or self.config.get("cookie", "")
        if not sauth_json:
            self._log("缺少 sauth_json 凭证", "error")
            self.update_status(AccessPointStatus.CRASHED)
            return

        # 设备指纹
        device_fingerprint = self._build_device_fingerprint()

        self._log(f"正在连接 {host}:{port}...", "info")

        try:
            self._client = BedrockClient(
                sauth_json=sauth_json,
                device_fingerprint=device_fingerprint,
                chain_info=self._chain_info or "",
            )
            await self._client.connect(host, port)

            self._connected = True
            self._spawned = True
            self._log("已连接到游戏服务器", "success")
            await self._emit("event", "connected", {})

            # 纯Python接入点: 连接成功后即视为已生成 (spawn)
            # Go接入点需要等待 StartGame 包, 但纯Python在 connect() 中已完成握手
            bot_name = self.config.get("bot_name", "")
            self._log(f"机器人已生成: {bot_name}", "success")
            await self._emit("event", "spawn", {"bot_name": bot_name})

            # 启动接收循环
            self._recv_task = asyncio.create_task(self._recv_loop())

            self.update_status(AccessPointStatus.RUNNING)

        except LoginError as e:
            self._log(f"登录失败: {e}", "error")
            self.info.last_error = f"登录失败: {e}"
            self.update_status(AccessPointStatus.CRASHED)
            await self._emit("error", "login_failed", str(e))

        except AccountBannedError as e:
            self._log(f"账号被封禁: {e}", "error")
            self.info.last_error = f"账号被封禁: {e}"
            self.update_status(AccessPointStatus.CRASHED)
            await self._emit("ban", "account_banned", str(e))

        except Exception as e:
            self._log(f"连接失败: {e}", "error")
            self.info.last_error = f"连接失败: {e}"
            self.update_status(AccessPointStatus.CRASHED)
            await self._emit("error", "connection_failed", str(e))

    def _parse_server_address(self, address: str) -> tuple[str, int]:
        """解析服务器地址 (host:port 或 host)。"""
        if ":" in address:
            host, port_str = address.rsplit(":", 1)
            try:
                return host, int(port_str)
            except ValueError:
                # Bug 12.2 修复: 端口解析失败时, 之前 fall through 到
                # `return address, 19132`, 会把完整地址 (含无效端口) 作为 host
                # 返回 (如 "example.com:abc" → ("example.com:abc", 19132))。
                # 现改为返回已分离的 host 和默认端口。
                return host, 19132
        return address, 19132  # 默认端口

    def _extract_sauth_from_chain_info(self, chain_info: str) -> Optional[str]:
        """从 chainInfo 中提取 sauth_json。

        chainInfo 通常是服务器返回的认证链，包含 sauth_json。
        """
        if not chain_info:
            return None

        # 如果直接就是 sauth_json 格式
        if chain_info.startswith("{") and "sessionid" in chain_info:
            return chain_info

        # 尝试解析 JSON
        try:
            data = json.loads(chain_info) if isinstance(chain_info, str) else chain_info
            if isinstance(data, dict):
                # 可能直接就是 sauth_json
                if "sessionid" in data or "sdkuid" in data:
                    return json.dumps(data)
                # 可能在某个字段里
                for key in ("sauth_json", "sauth", "chain", "data"):
                    if key in data:
                        val = data[key]
                        if isinstance(val, str) and ("sessionid" in val or "sdkuid" in val):
                            return val
                        if isinstance(val, dict) and ("sessionid" in val or "sdkuid" in val):
                            return json.dumps(val)
        except (json.JSONDecodeError, TypeError):
            pass

        # 直接返回原值，让 build_login_chain 处理
        return chain_info

    def _build_device_fingerprint(self) -> dict:
        """构建设备指纹。

        为每个账号生成独立的设备指纹,防止多账号因设备指纹相同被反作弊系统
        识别为机器人农场而连封。

        关键字段:
            - device_id: 基于 sdkuid 生成的确定性 UUID (同账号一致, 不同账号不同)
            - platform: "windows" (PC 版)
            - device_model: 从 sa_data 中提取的设备型号
            - game_version: 协议版本
        """
        import hashlib
        import json as _json
        import uuid as _uuid
        from ..auth.netease_direct.constants import generate_sa_data_pc

        # 从 sauth_json 中提取 sdkuid 作为种子 (每个账号唯一)
        sauth_json_str = self.config.get("sauth_json", "") or self.config.get("cookie", "")
        sdkuid = ""
        try:
            sauth_data = _json.loads(sauth_json_str) if sauth_json_str else {}
            sdkuid = str(sauth_data.get("sdkuid", ""))
        except (ValueError, TypeError):
            pass

        seed = sdkuid or sauth_json_str or str(time.time())

        # 生成 sa_data (PC 设备指纹 JSON)
        try:
            sa_data_str = generate_sa_data_pc(seed=seed)
            sa_data = _json.loads(sa_data_str) if sa_data_str else {}
        except Exception:
            sa_data = {}

        # 基于 sdkuid 生成确定性的 device_id (同账号每次相同, 不同账号不同)
        # 这确保 DeviceId 在 JWT ClientData 中唯一, 游戏服务器不会关联
        if sdkuid:
            # Bug 12.4 修复: 之前使用 MD5 生成 device_id, MD5 已被证明存在
            # 碰撞风险且不推荐用于新代码。改用 SHA256 (取前 32 个十六进制字符)。
            h = hashlib.sha256(sdkuid.encode("utf-8")).hexdigest()
            # 格式化为标准 UUID 格式
            device_id = f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"
        else:
            device_id = str(_uuid.uuid4())

        # 从 sa_data 中提取设备型号信息
        cpu_type = sa_data.get("cpu_type", "Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz")
        gpu = sa_data.get("video_card1", "NVIDIA GeForce GTX 1660")
        device_model = f"PC/{cpu_type[:30]}/{gpu[:20]}"

        return {
            "device_id": device_id,
            "device_model": device_model,
            "platform": "windows",
            "game_version": GameVersion,
            "os_name": "windows",
            "os_ver": sa_data.get("os_ver", "Microsoft Windows 10 专业版"),
            "sa_data": sa_data,
            # 额外的设备特征, 增加随机性
            "mac_addr": sa_data.get("mac_addr", ""),
            "disk_serial": sa_data.get("disk", ""),
            "udid": sa_data.get("udid", ""),
            "ram_size": sa_data.get("ram_size", "8589934592"),
            "screen_width": sa_data.get("device_width", "1920"),
            "screen_height": sa_data.get("device_height", "1080"),
        }

    # ------------------------------------------------------------------
    # 认证逻辑
    # ------------------------------------------------------------------

    async def _authenticate(
        self, server_code: str, server_password: str
    ) -> Optional[dict]:
        """执行网易直连认证。

        支持多种凭证来源:
            1. cookie / sauth_json (直接使用)
            2. 4399 账号密码 (通过 OAuth2 登录获取 sauth_json)
            3. 无凭证 (自动生成 sauth_json, 访客模式)

        Returns:
            包含 chain_info 和 server_address 的字典，失败返回 None
        """
        cookie = self.config.get("cookie", "")
        sauth_json = self.config.get("sauth_json", "")
        username = self.config.get("username", "")
        password = self.config.get("password", "")

        # 如果没有 cookie/sauth_json 但有 4399 账号, 先通过 OAuth2 获取
        if not cookie and not sauth_json and username and password:
            self._log(f"正在通过 4399 OAuth2 登录获取游戏凭证 (用户: {username})...", "protocol")
            try:
                from ..auth.netease_direct.login_4399_oauth2 import login_4399_oauth2
                import json as _json
                result = await login_4399_oauth2(username, password)
                if result and result.sauth_json:
                    sauth_json = _json.dumps(result.sauth_json, ensure_ascii=False)
                    self.config["sauth_json"] = sauth_json
                    self._log("4399 OAuth2 登录成功!", "success")
                else:
                    self._log("4399 OAuth2 登录失败", "error")
                    return None
            except Exception as e:
                self._log(f"4399 OAuth2 登录异常: {e}", "error")
                return None

        if not cookie and not sauth_json:
            # 尝试自动生成 sauth_json (访客模式)
            try:
                from ..auth.netease_direct import generate_sauth_json
                self._log("无凭证, 自动生成 sauth_json (访客模式)...", "warning")
                sauth_json = generate_sauth_json(mode="pc")
                self.config["sauth_json"] = sauth_json
            except Exception as e:
                self._log(f"自动生成 sauth_json 失败: {e}", "error")
                return None

        try:
            from ..auth.netease_direct import NeteaseDirectClient

            mode = "pc"

            async with NeteaseDirectClient(mode=mode, timeout=60.0) as client:
                # 登录
                if cookie:
                    self._log("正在使用 Cookie 登录网易认证服务器...", "protocol")
                    await client.login_with_cookie(cookie)
                else:
                    self._log("正在使用 sauth_json 登录网易认证服务器...", "protocol")
                    await client.login(sauth_json)

                self._log(f"网易登录成功! UID: {client.uid}", "success")

                # 获取用户信息
                try:
                    await client.get_user_detail()
                    if client.player_name:
                        self._log(f"用户昵称: {client.player_name}", "success")
                except Exception:
                    self._log("获取用户详情失败 (不影响后续流程)", "warning")

                # 连接租赁服
                if server_code and server_code != "custom":
                    bot_name = self.config.get("bot_name", "")
                    display_name = bot_name or (
                        f"PT_{client.player_name}" if client.player_name else f"PT_{client.uid}"
                    )

                    self._log(f"正在搜索租赁服 {server_code}...", "protocol")

                    result = await client.connect_rental_server(
                        server_code, server_password, display_name=display_name
                    )

                    self._log("已找到并进入租赁服!", "success")
                    self._log(f"服务器地址: {result.server_address}", "info")

                    return {
                        "chain_info": result.chain_info,
                        "server_address": result.server_address,
                    }
                else:
                    # 自定义服务器 - 直接使用 server_address
                    server_address = self.config.get("server_address", "")
                    if not server_address:
                        self._log("自定义模式需要提供 server_address", "error")
                        return None

                    return {
                        "chain_info": "",
                        "server_address": server_address,
                    }

        except Exception as e:
            err_msg = str(e)
            if "code=29" in err_msg or "封禁" in err_msg:
                self._log("账号已被网易封禁 (code=29)", "error")
                self.info.last_error = "账号已被网易封禁 (code=29)"
            elif "code=32" in err_msg:
                # code=32: sessionid 过期或损坏, 尝试自动刷新 sauth_json 后重试
                self._log("认证已过期 (code=32), 正在自动刷新 sauth_json...", "warning")
                try:
                    from ..auth.sauth_refresh import sauth_refresher
                    fresh_sauth = await sauth_refresher.get_fresh_sauth()
                    if fresh_sauth:
                        self._log("sauth_json 刷新成功, 正在重试登录...", "protocol")
                        # 更新 config 中的 sauth_json
                        self.config["sauth_json"] = fresh_sauth
                        # 重试登录
                        async with NeteaseDirectClient(mode=mode, timeout=60.0) as retry_client:
                            await retry_client.login(fresh_sauth)
                            self._log(f"重试登录成功! UID: {retry_client.uid}", "success")

                            # 获取用户信息
                            try:
                                await retry_client.get_user_detail()
                                if retry_client.player_name:
                                    self._log(f"用户昵称: {retry_client.player_name}", "success")
                            except Exception:
                                pass

                            # 连接租赁服
                            if server_code and server_code != "custom":
                                bot_name = self.config.get("bot_name", "")
                                display_name = bot_name or (
                                    f"PT_{retry_client.player_name}" if retry_client.player_name else f"PT_{retry_client.uid}"
                                )
                                result = await retry_client.connect_rental_server(
                                    server_code, server_password, display_name=display_name
                                )
                                self._log("已找到并进入租赁服!", "success")
                                self._log(f"服务器地址: {result.server_address}", "info")
                                return {
                                    "chain_info": result.chain_info,
                                    "server_address": result.server_address,
                                }
                            else:
                                server_address = self.config.get("server_address", "")
                                if not server_address:
                                    self._log("自定义模式需要提供 server_address", "error")
                                    return None
                                return {
                                    "chain_info": "",
                                    "server_address": server_address,
                                }
                    else:
                        self._log("sauth_json 自动刷新失败, 无可用 4399 账号", "error")
                        self.info.last_error = "认证已过期 (code=32) 且自动刷新失败, 请检查4399账号池"
                except Exception as refresh_err:
                    self._log(f"sauth_json 自动刷新异常: {refresh_err}", "error")
                    self.info.last_error = f"认证已过期 (code=32), 自动刷新异常: {refresh_err}"
                # 如果自动刷新和重试都失败, 不再设置 last_error (已在上面设置)
            elif "code=10" in err_msg:
                self._log("未登录或登录已过期 (code=10), 请检查sauth_json", "error")
                self.info.last_error = "未登录或登录已过期 (code=10), 请检查sauth_json"
            elif "code=" in err_msg:
                self._log(f"网易认证失败: {err_msg}", "error")
                self.info.last_error = f"网易认证失败: {err_msg}"
            else:
                self._log(f"网易直连认证异常: {err_msg}", "error")
                self.info.last_error = f"认证异常: {err_msg}"
            logger.exception("网易直连认证失败")
            return None

    # ------------------------------------------------------------------
    # 接收循环
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """后台接收循环 - 处理来自服务器的数据包。"""
        if not self._client:
            return

        try:
            while self._connected:
                try:
                    packet_id, data = await asyncio.wait_for(
                        self._client.recv_packet(),
                        timeout=30.0,
                    )
                except asyncio.TimeoutError:
                    # 超时正常，继续等待
                    continue
                except Exception as e:
                    if self._connected:
                        self._log(f"接收数据包错误: {e}", "warning")
                    break

                await self._handle_packet(packet_id, data)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._log(f"接收循环异常: {e}", "error")

        self._connected = False
        if self.info.status == AccessPointStatus.RUNNING:
            self.update_status(AccessPointStatus.DISCONNECTED)
            await self._emit("event", "disconnected", {})

    async def _handle_packet(self, packet_id: int, data: bytes) -> None:
        """处理接收到的数据包。"""
        # 构建数据包字典
        packet = {
            "id": packet_id,
            "data": data.hex() if isinstance(data, bytes) else data,
            "timestamp": time.time(),
        }

        # 分发给已注册的处理器
        await self._dispatch_packet(packet)

        # 处理特定包类型
        from ..protocol.connection import PacketID

        if packet_id == PacketID.TEXT:
            await self._handle_text_packet(data)
        elif packet_id == PacketID.DISCONNECT:
            await self._handle_disconnect_packet(data)

    async def _handle_text_packet(self, data: bytes) -> None:
        """处理文本（聊天）数据包。"""
        try:
            # 解析 Text 包
            # [Byte: type][Bool: needs_translation][Uint32LE: param_count]
            # [String[]: params][String: message][String: xuid][String: platform_chat_id]
            # 注意: 字符串长度使用 Varuint32 编码 (Bedrock 协议标准),
            # 而 param_count 使用 Uint32LE (参考 connection.py 的 _build_text_packet)
            import struct
            from ..protocol.varint import decode_varuint32

            offset = 0
            text_type = data[offset]
            offset += 1
            needs_translation = data[offset] != 0
            offset += 1

            param_count = struct.unpack_from("<I", data, offset)[0]
            offset += 4

            # 跳过参数 (每个参数是 [Varuint32: 长度][UTF-8 字节])
            for _ in range(param_count):
                str_len, offset = decode_varuint32(data, offset)
                offset += str_len

            # 读取消息
            msg_len, offset = decode_varuint32(data, offset)
            message = data[offset:offset + msg_len].decode("utf-8", errors="replace")
            offset += msg_len

            self._log(f"[聊天] {message}", "info")
            await self._emit("chat", "", message)

        except Exception as e:
            logger.debug(f"解析Text包失败: {e}")

    async def _handle_disconnect_packet(self, data: bytes) -> None:
        """处理断开连接数据包。"""
        try:
            # Disconnect 包: [Bool: skip_message][String: reason]
            # 参考 connection.py 的 _parse_disconnect 方法
            from ..protocol.varint import decode_varuint32

            reason_str = "未知原因"
            if data:
                offset = 0
                # 读取 skip_message (1 字节 bool)
                skip_message = data[offset]
                offset += 1
                # 读取 reason 字符串 (Varuint32 长度 + UTF-8 字节)
                str_len, offset = decode_varuint32(data, offset)
                reason_str = data[offset:offset + str_len].decode("utf-8", errors="replace")

            self._log(f"服务器断开连接: {reason_str}", "warning")
            self._connected = False

            # 检测封禁关键词
            ban_keywords = ["封禁", "ban", "banned", "禁用", "禁止登录"]
            if any(kw in reason_str.lower() for kw in ban_keywords):
                await self._emit("ban", "server_banned", reason_str)
            else:
                await self._emit("event", "disconnected", {"reason": reason_str})

        except Exception as e:
            logger.debug(f"解析Disconnect包失败: {e}")

    # ------------------------------------------------------------------
    # 游戏操作
    # ------------------------------------------------------------------

    async def send_packet(self, packet: dict) -> bool:
        """发送数据包到服务器。"""
        if not self._client or not self._connected:
            self._log("无法发送数据包：未连接", "error")
            return False

        try:
            packet_id = packet.get("id", 0)
            data = packet.get("data", b"")

            if isinstance(data, str):
                data = bytes.fromhex(data)

            await self._client.send_packet(packet_id, data)
            self.info.packet_count_sent += 1
            return True

        except Exception as e:
            self._log(f"发送数据包失败: {e}", "error")
            return False

    async def send_command(self, command: str) -> Optional[str]:
        """发送命令并等待响应。

        Args:
            command: 要发送的命令 (不带 /)

        Returns:
            命令响应文本，失败返回 None
        """
        if not self._client or not self._connected:
            self._log("无法发送命令：未连接", "error")
            return None

        try:
            response = await self._client.send_command(command)
            self.info.packet_count_sent += 1
            self._log(f"[命令] /{command} -> {response[:100] if response else '无响应'}", "info")
            await self._emit("command_output", {"command": command, "response": response})
            return response

        except Exception as e:
            self._log(f"发送命令失败: {e}", "error")
            return None

    async def send_chat(self, message: str) -> bool:
        """发送聊天消息。"""
        if not self._client or not self._connected:
            return False

        try:
            await self._client.send_chat(message)
            self.info.packet_count_sent += 1
            self._log(f"[发送] {message}", "info")
            return True

        except Exception as e:
            self._log(f"发送聊天失败: {e}", "error")
            return False

    async def move_to(self, x: float, y: float, z: float) -> bool:
        """移动到指定坐标。"""
        if not self._client or not self._connected:
            return False

        try:
            # 发送 PlayerAuthInput 包 (使用 BedrockClient 的完整实现)
            await self._client.send_player_auth_input((x, y, z))
            return True

        except Exception as e:
            self._log(f"移动失败: {e}", "error")
            return False

    async def disconnect(self) -> bool:
        """断开游戏连接。"""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass

        self._connected = False
        return True

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    async def stop(self) -> None:
        """停止接入点。"""
        self._log("正在停止纯Python接入点...", "info")

        # 取消接收循环
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        # 断开连接
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

        self._connected = False
        self._spawned = False
        self.update_status(AccessPointStatus.DISCONNECTED)
        self._log("纯Python接入点已停止", "info")

    def get_status(self) -> AccessPointInfo:
        """获取接入点状态。"""
        self.info.is_connected = self._connected
        self.info.is_spawned = self._spawned
        self.info.is_running = self._connected and self._client is not None
        return self.info

    def _log(self, message: str, level: str = "info") -> None:
        """打印日志。"""
        timestamp = time.strftime("%H:%M:%S")
        icons = {
            "info": "✅", "error": "❌", "warning": "⚠️",
            "debug": "🔍", "protocol": "🔐", "success": "🎉",
        }
        icon = icons.get(level, "ℹ️")
        print(f"[{timestamp}] {icon} [纯Python] {message}", flush=True)

        if level == "error":
            logger.error(message)
        elif level == "warning":
            logger.warning(message)
        elif level == "debug":
            logger.debug(message)
        else:
            logger.info(message)


__all__ = ["PurePythonAccessPoint"]
