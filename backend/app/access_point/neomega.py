"""NeOmega 接入点

NeOmega 是面向网易我的世界租赁服 / 联机大厅的接入点。
它以独立进程方式运行，通过 **WebSocket** 接口对外提供服务。

二进制文件命名规则（与 ToolDelta / NeOmega 官方发行版一致）::

    NeOmega_<os>_<arch>(.exe)
    例:
        NeOmega_windows_amd64.exe
        NeOmega_linux_amd64
        NeOmega_android_aarch64

启动参数::

    NeOmega_<os>_<arch> -p <port> [-s <secondary-auth-proxy>]

NeOmega 启动后会监听一个 WebSocket 端口，客户端通过 WS 连接后:
    - ``send_packet()``: 将 JSON 数据包通过 WS 发送给 NeOmega
    - ``on_packet()``:   接收 NeOmega 推送过来的 WS 消息

本模块实现了:
    1. NeOmega 二进制的查找与启动（子进程管理）
    2. WebSocket 客户端连接（使用 aiohttp 或 websockets 库）
    3. 数据包的发送与接收
    4. 二进制下载管理（从 GitHub Release 下载对应平台版本）

典型用法::

    from .neomega import NeOmegaAccessPoint

    config = {
        "server_code": "123456",
        "server_password": "",
        "auth_server": "https://nv1.nethard.pro",
        "api_key": "...",
        "binary_dir": "/opt/pocketterm/bin",
    }
    ap = NeOmegaAccessPoint(config)
    await ap.start()
    await ap.send_packet({"type": "chat", "message": "hello"})
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .base import (
    AccessPoint,
    AccessPointInfo,
    AccessPointStatus,
    BinaryNotFoundError,
    Colors,
    ConnectionTimeoutError,
    PacketHandler,
    SubprocessCrashedError,
)

logger = logging.getLogger("pocketterm.access_point.neomega")

# ======================================================================
# 常量
# ======================================================================

#: 本地模式 WebSocket 端口搜索起点
DEFAULT_LOCAL_PORT: int = 24000

#: ``wait_ready`` 默认超时（秒）
DEFAULT_READY_TIMEOUT: float = 60.0

#: NeOmega GitHub Release 下载地址模板
#: {version} / {system} / {arch} / {ext} 会被替换
NEOMEGA_DOWNLOAD_URL: str = (
    "https://github.com/ToolDelta/NeOmega/releases/download/"
    "{version}/NeOmega_{system}_{arch}{ext}"
)

#: NeOmega 默认下载版本（可由配置覆盖）
DEFAULT_VERSION: str = "v1.0.0"


# ======================================================================
# 主类
# ======================================================================


class NeOmegaAccessPoint(AccessPoint):
    """NeOmega 接入点。

    通过 WebSocket 与 NeOmega 子进程通信。

    工作流程::

        1. find_binary()      -> 在 binary_dir 中查找 NeOmega 二进制
        2. _get_free_port()   -> 分配空闲端口
        3. start()             -> 拉起子进程 (NeOmega -p <port>)
        4. _connect_ws()       -> WebSocket 连接到 ws://127.0.0.1:<port>
        5. send_packet()       -> 通过 WS 发送 JSON 数据包
        6. _ws_receive_loop() -> 后台协程持续接收 WS 消息并分发
        7. stop()             -> 关闭 WS + 终止子进程

    Args:
        config: 接入点配置字典。常用键:
            - ``server_code``:      租赁服号
            - ``server_password``:  服务器密码
            - ``auth_server``:      认证服务器 URL
            - ``api_key``:          API Key
            - ``binary_dir``:       二进制所在目录
            - ``bind_host``:        本地绑定主机（默认 ``"127.0.0.1"``）
            - ``start_port``:       端口搜索起点（默认 24000）
            - ``extra_args``:       额外子进程参数字典
            - ``version``:          NeOmega 版本（用于下载）
            - ``indirect``:         是否远端模式（不启动子进程）
            - ``external_addr``:    远端 WS 地址
        status_callback: 状态变更回调。
    """

    launch_type: str = "NeOmega"
    binary_name_patterns: list[str] = [
        "NeOmega_{system}_{arch}",
        "NeOmega_{system}_{arch}{ext}",
        "NeOmega_{system}_{arch}.exe",
    ]
    default_start_port: int = DEFAULT_LOCAL_PORT

    def __init__(
        self,
        config: dict[str, Any],
        status_callback=None,
    ) -> None:
        super().__init__(config=config, status_callback=status_callback)

        # 远端模式：不启动子进程，直接连接已有的 NeOmega
        self.indirect: bool = bool(config.get("indirect", False))
        # 远端 WS 地址（如 ws://192.168.1.10:24016）
        self.external_addr: str = config.get("external_addr", "")

        # WebSocket 连接对象
        self._ws: Any = None  # aiohttp.ClientWebSocketResponse 或 websockets.WebSocketClientProtocol
        # WebSocket 客户端 session（aiohttp）
        self._session: Any = None
        # Bug 14.4 修复: _ws_lib 仅在 _ws_connect 中赋值, __init__ 未初始化。
        # 若在 _ws_connect 之前调用 _ws_send / _ws_receive (理论上不应发生,
        # 但防御性不足), 会抛 AttributeError。在此初始化为空字符串。
        self._ws_lib: str = ""
        # 后台接收协程
        self._receive_task: Optional[asyncio.Task[None]] = None
        # 后台 stdout/stderr 读取协程
        self._stdout_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        # stdout/stderr 缓冲区 (P0 修复: 必须初始化, 否则 _drain_stream 会 AttributeError)
        self._stdout_buf: bytes = b""
        self._stderr_buf: bytes = b""

        # 解析远端地址
        self._ws_host: str = "127.0.0.1"
        self._ws_port: int = 0
        if self.indirect and self.external_addr:
            self._parse_ws_addr(self.external_addr)

    # ------------------------------------------------------------------ #
    # 公开接口实现
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """启动 NeOmega 接入点。

        本地模式:查找二进制 -> 分配端口 -> 拉起子进程 -> WS 连接。
        远端模式:直接 WS 连接到 ``external_addr``。

        Raises:
            BinaryNotFoundError: 本地模式下找不到二进制。
            SubprocessCrashedError: 子进程启动后立即退出。
            ConnectionTimeoutError: WS 连接超时。
        """
        if self.info.status not in (
            AccessPointStatus.IDLE,
            AccessPointStatus.CRASHED,
            AccessPointStatus.DISCONNECTED,
        ):
            self._log(f"已在 {self.info.status.value} 状态，跳过重复 start()", "warning")
            return

        self.update_status(AccessPointStatus.LAUNCHING)
        self.info.started_at = time.time()

        if self.indirect:
            # 远端模式：只连 WS
            self._log("远端模式：连接到已有 NeOmega...", "info")
            await self._connect_ws()
        else:
            # 本地模式：拉子进程 + 连 WS
            self._log("本地模式：启动 NeOmega 子进程...", "info")
            await self._start_subprocess()
            await self._connect_ws()

        self.update_status(AccessPointStatus.RUNNING)

        # 启动后台接收协程
        self._receive_task = asyncio.create_task(self._ws_receive_loop())
        self._log(
            f"NeOmega 已就绪 (PID={self.info.pid}, WS=ws://{self._ws_host}:{self._ws_port})",
            "info",
        )

    async def stop(self) -> None:
        """停止 NeOmega 接入点。

        1. 取消后台接收协程
        2. 关闭 WebSocket 连接
        3. 终止 NeOmega 子进程（SIGTERM -> SIGKILL）
        """
        self._log("正在停止 NeOmega...", "info")

        # 取消接收协程
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        self._receive_task = None

        # 关闭 WebSocket
        await self._close_ws()

        # 终止子进程
        if self.proc is not None:
            await self._terminate_subprocess()

        self.update_status(AccessPointStatus.IDLE)
        self._log("NeOmega 已停止", "info")

    async def send_packet(self, packet: dict[str, Any]) -> bool:
        """通过 WebSocket 发送数据包。

        Args:
            packet: JSON 可序列化的数据包字典。

        Returns:
            ``True`` 发送成功;``False`` 发送失败（WS 未连接或发送异常）。
        """
        if self._ws is None:
            self._log("WebSocket 未连接，无法发送数据包", "error")
            return False

        try:
            data = json.dumps(packet, ensure_ascii=False)
            await self._ws_send(data)
            self.info.packet_count_sent += 1
            packet_type = packet.get("type", "unknown")
            self._log(f"发送数据包: {Colors.colorize(packet_type, Colors.CYAN)}", "debug")
            return True
        except Exception as exc:
            self._log(f"发送数据包失败: {exc}", "error")
            self.info.last_error = str(exc)
            return False

    async def on_packet(self, handler: PacketHandler) -> None:
        """注册数据包接收回调。

        当 NeOmega 通过 WebSocket 推送消息时，所有已注册的处理器
        会被依次调用。

        Args:
            handler: 回调函数，接收一个 ``dict`` 参数。
        """
        self.register_packet_handler(handler)
        self._log(f"已注册数据包处理器 (共 {len(self._packet_handlers)} 个)", "debug")

    def get_status(self) -> AccessPointInfo:
        """获取 NeOmega 接入点信息。"""
        # 更新 PID
        # P0 修复: asyncio.subprocess.Process 没有 poll() 方法, 用 returncode 判断
        if self.proc is not None and self.proc.returncode is None:
            self.info.pid = self.proc.pid
        elif self.proc is not None and self.proc.returncode is not None:
            # 子进程已退出
            if self.info.status == AccessPointStatus.RUNNING:
                self.update_status(AccessPointStatus.CRASHED)
                self.info.last_error = (
                    f"子进程意外退出 (rc={self.proc.returncode})"
                )
        return self.info

    # ------------------------------------------------------------------ #
    # 子进程管理
    # ------------------------------------------------------------------ #

    async def _start_subprocess(self) -> None:
        """查找二进制并启动 NeOmega 子进程。

        Raises:
            BinaryNotFoundError: 找不到二进制文件。
            SubprocessCrashedError: 启动失败。
        """
        binary = self.find_binary()
        if binary is None:
            raise BinaryNotFoundError(
                "找不到 NeOmega 二进制; binary_dir="
                f"{self.config.get('binary_dir')!r}, os={sys.platform}",
                ap_name=self.launch_type,
            )

        self.info.binary_path = str(binary)

        # 分配端口
        start_port = int(
            self.config.get("start_port") or self.default_start_port
        )
        self._ws_port = await self._get_free_port(start_port)
        self._ws_host = self.config.get("bind_host", "127.0.0.1")
        self.info.bind_address = self._ws_host
        self.info.bind_port = self._ws_port

        # 构造启动参数
        args: list[str] = [str(binary), "-p", str(self._ws_port)]

        # 额外参数
        extra_args: dict[str, Any] = self.config.get("extra_args", {})
        if isinstance(extra_args, dict):
            sec_proxy = extra_args.get("sec-auth-proxy")
            if sec_proxy:
                args.extend(["-s", str(sec_proxy)])

        # 环境变量
        env = os.environ.copy()

        self._log(
            f"启动子进程: {Colors.colorize(str(binary), Colors.GREEN)} "
            f"(port={self._ws_port})",
            "info",
        )

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(binary.parent),
            )
        except FileNotFoundError as exc:
            raise BinaryNotFoundError(
                f"无法执行 NeOmega 二进制 {binary}: {exc}",
                ap_name=self.launch_type,
            ) from exc
        except OSError as exc:
            raise SubprocessCrashedError(
                f"启动 NeOmega 失败: {exc}",
                ap_name=self.launch_type,
            ) from exc

        self.info.pid = self.proc.pid

        # 启动后台 stdout/stderr 读取协程（防止 PIPE 缓冲区满阻塞）
        if self.proc.stdout is not None:
            self._stdout_task = asyncio.create_task(
                self._drain_stream(self.proc.stdout, "stdout")
            )
        if self.proc.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._drain_stream(self.proc.stderr, "stderr")
            )

        # 等待子进程稳定（简单判定：1 秒内未退出）
        await asyncio.sleep(1.0)
        # P0 修复: asyncio.subprocess.Process 没有 poll() 方法, 用 returncode
        if self.proc.returncode is not None:
            rc = self.proc.returncode
            stderr_text = self._stderr_buf.decode("utf-8", errors="replace")
            self.update_status(AccessPointStatus.CRASHED)
            raise SubprocessCrashedError(
                f"NeOmega 子进程启动后立即退出 (rc={rc})",
                returncode=rc,
                stderr=stderr_text,
                ap_name=self.launch_type,
            )

    async def _drain_stream(self, stream: Any, kind: str) -> None:
        """持续读取子进程 stdout / stderr 到缓存。

        防止 PIPE 缓冲区满导致子进程阻塞。同时打印重要的 stderr 输出。

        Args:
            stream: 子进程的 stdout 或 stderr 管道。
            kind: ``"stdout"`` 或 ``"stderr"``。
        """
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                if kind == "stdout":
                    self._stdout_buf += chunk
                else:
                    self._stderr_buf += chunk
                    # 打印 stderr 中的重要信息
                    text = chunk.decode("utf-8", errors="replace").strip()
                    if text:
                        for line in text.split("\n"):
                            if line.strip():
                                self._log(f"[stderr] {line.strip()}", "warning")
                # 缓存上限 4 MiB
                buf_attr = "_stdout_buf" if kind == "stdout" else "_stderr_buf"
                buf = getattr(self, buf_attr)
                if len(buf) > 4 * 1024 * 1024:
                    setattr(self, buf_attr, buf[-4 * 1024 * 1024 :])
        except Exception as exc:
            self._log(f"drain_stream({kind}) 异常(忽略): {exc}", "debug")

    async def _terminate_subprocess(self) -> None:
        """终止 NeOmega 子进程。

        先发 SIGTERM，等待 3 秒；若未退出则发 SIGKILL。
        """
        proc = self.proc
        self.proc = None
        self.info.pid = None
        if proc is None or proc.returncode is not None:
            return

        self._log(f"终止子进程 (PID={proc.pid})...", "info")

        try:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self._log("SIGTERM 超时，发送 SIGKILL...", "warning")
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    proc.send_signal(signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._log(f"子进程未响应 SIGKILL (PID={proc.pid})", "error")

        # 取消后台读流任务
        for t in (self._stdout_task, self._stderr_task):
            if t is not None and not t.done():
                t.cancel()
        self._stdout_task = None
        self._stderr_task = None

    # ------------------------------------------------------------------ #
    # WebSocket 通信
    # ------------------------------------------------------------------ #

    async def _connect_ws(self) -> None:
        """连接到 NeOmega 的 WebSocket 端口。

        本地模式:连接 ``ws://127.0.0.1:<port>``。
        远端模式:连接 ``external_addr``。

        Raises:
            ConnectionTimeoutError: 连接超时。
        """
        if not self.indirect:
            ws_url = f"ws://{self._ws_host}:{self._ws_port}"
        else:
            ws_url = self.external_addr

        self._log(f"连接 WebSocket: {Colors.colorize(ws_url, Colors.CYAN)}", "info")

        try:
            await self._ws_connect(ws_url)
        except Exception as exc:
            self.update_status(AccessPointStatus.DISCONNECTED)
            self.info.last_error = str(exc)
            raise ConnectionTimeoutError(
                f"无法连接 NeOmega WebSocket {ws_url}: {exc}",
                ap_name=self.launch_type,
            ) from exc

        self._log("WebSocket 连接成功", "info")

    async def _ws_connect(self, url: str) -> None:
        """实际的 WebSocket 连接实现。

        优先使用 ``aiohttp``，若不可用则尝试 ``websockets`` 库。

        Args:
            url: WebSocket URL (``ws://host:port``)。
        """
        try:
            import aiohttp  # type: ignore

            # trust_env=True 让 aiohttp 读取 HTTP_PROXY/HTTPS_PROXY 环境变量
            self._session = aiohttp.ClientSession(trust_env=True)
            self._ws = await self._session.ws_connect(url, heartbeat=30)
            self._ws_lib = "aiohttp"
        except ImportError:
            try:
                import websockets  # type: ignore

                self._ws = await websockets.connect(url)
                self._ws_lib = "websockets"
            except ImportError:
                raise ImportError(
                    "需要 aiohttp 或 websockets 库来连接 NeOmega WebSocket。"
                    "请安装: pip install aiohttp  或  pip install websockets"
                )

    async def _ws_send(self, data: str) -> None:
        """通过 WebSocket 发送字符串数据。

        根据 ``_ws_lib`` 自动选择发送方式。

        Args:
            data: JSON 字符串。
        """
        if self._ws_lib == "aiohttp":
            await self._ws.send_str(data)
        elif self._ws_lib == "websockets":
            await self._ws.send(data)
        else:
            raise RuntimeError("WebSocket 未连接")

    async def _ws_receive(self) -> Optional[str]:
        """从 WebSocket 接收一条消息。

        Returns:
            收到的消息字符串;连接关闭时返回 ``None``。
        """
        if self._ws_lib == "aiohttp":
            msg = await self._ws.receive()
            # Bug 14.3 修复: 之前使用魔数 0x1 / 0x8 / 0x101 判断消息类型,
            # 注释 "CLOSE / CLOSE" 也错误 (应为 CLOSE / CLOSED), 且漏判
            # CLOSING (0x9) 和 ERROR (0x102) 状态。改用 aiohttp.WSMsgType
            # 枚举常量, 并补全 CLOSING/ERROR 处理。
            from aiohttp import WSMsgType

            if msg.type == WSMsgType.TEXT:
                return msg.data
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                return None
            return None
        elif self._ws_lib == "websockets":
            try:
                data = await self._ws.recv()
                if isinstance(data, bytes):
                    return data.decode("utf-8", errors="replace")
                return str(data)
            except ConnectionClosed:
                return None
        return None

    async def _ws_receive_loop(self) -> None:
        """后台协程:持续接收 WebSocket 消息并分发给已注册的处理器。

        当连接关闭或发生异常时退出循环。
        """
        self._log("WebSocket 接收循环已启动", "debug")
        try:
            while True:
                data = await self._ws_receive()
                if data is None:
                    self._log("WebSocket 连接已关闭", "warning")
                    break

                # 解析 JSON
                try:
                    packet = json.loads(data)
                except (json.JSONDecodeError, TypeError):
                    self._log(f"收到非 JSON 消息: {data[:200]}", "warning")
                    continue

                # 分发给处理器
                await self._dispatch_packet(packet)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"WebSocket 接收循环异常: {exc}", "error")
            self.info.last_error = str(exc)
            # Bug 14.5 修复: 之前无条件覆盖为 DISCONNECTED, 若接入点此前
            # 已因 _start_subprocess 失败被设为 CRASHED, 此处会丢失崩溃
            # 状态信息。加判断: 仅在非 CRASHED/IDLE 状态时才更新为 DISCONNECTED。
            if self.info.status not in (AccessPointStatus.CRASHED, AccessPointStatus.IDLE):
                self.update_status(AccessPointStatus.DISCONNECTED)

    async def _close_ws(self) -> None:
        """关闭 WebSocket 连接和 aiohttp session。"""
        if self._ws is not None:
            try:
                if self._ws_lib == "aiohttp":
                    await self._ws.close()
                elif self._ws_lib == "websockets":
                    await self._ws.close()
            except Exception as exc:
                self._log(f"关闭 WebSocket 异常(忽略): {exc}", "debug")
        self._ws = None

        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    # ------------------------------------------------------------------ #
    # 远端地址解析
    # ------------------------------------------------------------------ #

    def _parse_ws_addr(self, addr: str) -> None:
        """解析 WebSocket 地址。

        支持格式:
            - ``ws://host:port``
            - ``host:port``

        Args:
            addr: WebSocket 地址字符串。
        """
        import re

        addr = addr.strip()
        # 去掉 ws:// 前缀
        addr = re.sub(r"^wss?://", "", addr)
        # 去掉末尾斜杠
        addr = addr.rstrip("/")

        m = re.match(r"^([^:]+):(\d+)$", addr)
        if m:
            self._ws_host = m.group(1)
            self._ws_port = int(m.group(2))
        else:
            self._ws_host = addr
            self._ws_port = DEFAULT_LOCAL_PORT

    # ------------------------------------------------------------------ #
    # 二进制下载
    # ------------------------------------------------------------------ #

    @staticmethod
    async def download(binary_dir: str, version: str = DEFAULT_VERSION) -> Path:
        """下载 NeOmega 二进制文件。

        从 GitHub Release 下载对应平台的 NeOmega 二进制到指定目录。

        Args:
            binary_dir: 二进制存放目录。
            version: NeOmega 版本号 (如 ``"v1.0.0"``)。

        Returns:
            下载后的二进制文件路径。

        Note:
            此方法为静态方法，可直接通过类名调用::

                await NeOmegaAccessPoint.download("/opt/pocketterm/bin")
        """
        from .base import _system_name, _arch_name

        system = _system_name()
        arch = _arch_name()
        ext = ".exe" if os.name == "nt" else ""

        filename = f"NeOmega_{system}_{arch}{ext}"
        url = NEOMEGA_DOWNLOAD_URL.format(
            version=version,
            system=system,
            arch=arch,
            ext=ext,
        )

        bin_dir = Path(binary_dir)
        bin_dir.mkdir(parents=True, exist_ok=True)
        target = bin_dir / filename

        print(
            f"{Colors.colorize('[NeOmega]', Colors.BOLD, Colors.GREEN)} "
            f"下载二进制: {url}",
            flush=True,
        )

        # 使用 urllib 下载（不依赖额外库）
        loop = asyncio.get_running_loop()

        def _download() -> Path:
            import urllib.request

            urllib.request.urlretrieve(url, str(target))
            # Linux/macOS 设置可执行权限
            if ext == "":
                target.chmod(0o755)
            return target

        result = await loop.run_in_executor(None, _download)

        print(
            f"{Colors.colorize('[NeOmega]', Colors.BOLD, Colors.GREEN)} "
            f"下载完成: {target}",
            flush=True,
        )
        return result


# 处理 ConnectionClosed（兼容不同 websockets 版本）
try:
    from websockets.exceptions import ConnectionClosed  # type: ignore
except Exception:  # noqa: BLE001
    # websockets 不可用时定义一个占位异常
    class ConnectionClosed(Exception):  # type: ignore[no-redef]
        """websockets 连接关闭异常（占位实现）。"""

        pass


__all__ = [
    "NeOmegaAccessPoint",
    "DEFAULT_LOCAL_PORT",
    "DEFAULT_READY_TIMEOUT",
    "NEOMEGA_DOWNLOAD_URL",
]
