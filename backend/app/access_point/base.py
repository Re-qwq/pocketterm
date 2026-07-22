"""PocketTerm 接入点抽象基类与共享数据结构

本模块定义了所有接入点（NeOmega / FateArk / Custom）共享的:

    - :class:`Colors`           ANSI 颜色常量（彩色控制台输出）
    - 异常类层次                接入点 / 连接 / 认证相关异常
    - :class:`AccessPointStatus` 接入点运行时状态枚举
    - :class:`AccessPointInfo`   接入点信息 dataclass
    - :class:`AccessPoint`       抽象基类，定义 start / stop / send_packet / on_packet / get_status 五大接口

设计思路:
    PocketTerm 不直接实现网易 MCBE 协议，而是通过「接入点」与服务器通信。
    接入点是一个独立的进程或库，负责底层 RakNet 连接、认证、登录序列等，
    上层机器人只需调用 ``send_packet()`` 发送数据、通过 ``on_packet()``
    注册回调接收数据即可。

    这种分层设计使得 PocketTerm 可以灵活切换 NeOmega、FateArk 或自建接入点，
    而机器人逻辑保持不变。
"""
from __future__ import annotations

import abc
import asyncio
import enum
import logging
import os
import socket
import subprocess  # noqa: F401  类型注解用
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger("pocketterm.access_point.base")


# ======================================================================
# ANSI 颜色常量
# ======================================================================


class Colors:
    """ANSI 转义码常量，用于彩色控制台输出。

    用法::

        print(f"{Colors.GREEN}成功{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.RED}错误{Colors.RESET}")

    所有颜色码在 Windows 10+ / Linux / macOS 终端均可正常显示。
    若终端不支持颜色，这些转义码会被原样显示（无害）。
    """

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    BLINK = "\033[5m"

    # 前景色
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # 亮色前景色
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    # 背景色
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"

    @staticmethod
    def colorize(text: str, *codes: str) -> str:
        """便捷方法：给文本上色并自动追加 RESET。

        Args:
            text: 要着色的文本。
            codes: 一个或多个 :class:`Colors` 常量。

        Returns:
            着色后的字符串（含 ANSI 转义码）。

        Example::

            Colors.colorize("成功", Colors.BOLD, Colors.GREEN)
        """
        prefix = "".join(codes)
        return f"{prefix}{text}{Colors.RESET}"


# ======================================================================
# 异常类层次
# ======================================================================


class AccessPointError(Exception):
    """接入点基础异常。

    所有接入点相关异常的基类。捕获此异常可处理所有接入点错误。
    """

    pass


class NetworkError(AccessPointError):
    """网络错误（可重连）。

    包括连接超时、DNS 解析失败、TCP 连接被拒绝等。
    这类错误通常是暂时的，自动重连可能成功。
    """

    pass


class ConnectionTimeoutError(AccessPointError):
    """连接超时错误。

    在指定时间内未能建立连接或未收到响应。
    """

    def __init__(self, message: str, timeout: float = 0.0, ap_name: str = ""):
        super().__init__(message)
        self.timeout = timeout
        self.ap_name = ap_name


class BinaryNotFoundError(AccessPointError):
    """接入点二进制文件未找到。

    本地模式下，NeOmega / FateArk 二进制不在预期目录中。
    """

    def __init__(self, message: str, ap_name: str = ""):
        super().__init__(message)
        self.ap_name = ap_name


class SubprocessCrashedError(AccessPointError):
    """接入点子进程崩溃。

    NeOmega / FateArk 子进程意外退出。
    """

    def __init__(
        self,
        message: str,
        returncode: Optional[int] = None,
        stderr: str = "",
        ap_name: str = "",
    ):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr
        self.ap_name = ap_name


class AccountBannedError(AccessPointError):
    """账号被封禁（不可重连）。

    检测到服务器返回封禁消息时抛出。此错误会停止所有重连尝试。
    """

    pass


class InvalidCredentialsError(AccessPointError):
    """凭证无效（不可重连）。

    服务器号、密码或 API Key 错误。需用户修正后手动重启。
    """

    pass


class VersionTooLowError(AccessPointError):
    """客户端版本过低（不可重连）。

    需更新 PocketTerm 或接入点二进制版本。
    """

    pass


class ServerFullError(AccessPointError):
    """服务器已满（可重连）。

    服务器在线人数达到上限，稍后重连可能成功。
    """

    pass


class ServerNotFoundError(AccessPointError):
    """服务器不存在（不可重连）。

    指定的服务器号不存在或已关闭。
    """

    pass


class ServerRejectedError(AccessPointError):
    """服务器拒绝连接。"""

    def __init__(self, message: str, http_status: int = 0):
        super().__init__(message)
        self.http_status = http_status


class ProtocolError(AccessPointError):
    """协议错误。

    接入点返回了不符合预期的数据格式。
    """

    pass


class LoginFailedError(AccessPointError):
    """登录失败。"""

    def __init__(
        self,
        message: str,
        status: int = -1,
        error_msg: str = "",
        payload: str = "",
        ap_name: str = "",
    ):
        super().__init__(message)
        self.status = status
        self.error_msg = error_msg
        self.payload = payload
        self.ap_name = ap_name


# ======================================================================
# 状态枚举
# ======================================================================


class AccessPointStatus(enum.Enum):
    """接入点运行时状态。

    状态机::

        IDLE ──start()──► LAUNCHING ──► RUNNING
                                           │
                                           ├── 进程死 ──► CRASHED
                                           └── 网络断 ──► DISCONNECTED
        任意状态 ──stop()──► IDLE

    各状态含义:
        - ``IDLE``         空闲，未启动
        - ``LAUNCHING``    正在启动（拉起子进程 / 建立连接）
        - ``RUNNING``      正在运行，可收发数据包
        - ``CRASHED``       子进程崩溃
        - ``DISCONNECTED`` 连接已断开
    """

    IDLE = "idle"
    LAUNCHING = "launching"
    RUNNING = "running"
    CRASHED = "crashed"
    DISCONNECTED = "disconnected"


# ======================================================================
# 接入点信息
# ======================================================================


@dataclass
class AccessPointInfo:
    """接入点信息。

    Attributes:
        ap_id: 接入点唯一标识（UUID 前 8 位）。
        ap_type: 接入点类型名称（如 ``"NeOmega"`` / ``"FateArk"`` / ``"Custom"``）。
        status: 当前运行状态。
        created_at: 创建时间戳。
        started_at: 启动时间戳（未启动时为 ``None``）。
        binary_path: 二进制文件路径（本地模式）。
        bind_address: 绑定地址。
        bind_port: 绑定端口。
        pid: 子进程 PID（无子进程时为 ``None``）。
        last_error: 最近一次错误信息。
        packet_count_sent: 已发送数据包数。
        packet_count_received: 已接收数据包数。
        metadata: 附加元数据。
    """

    ap_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    ap_type: str = ""
    status: AccessPointStatus = AccessPointStatus.IDLE
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    binary_path: str = ""
    bind_address: str = "127.0.0.1"
    bind_port: int = 0
    pid: Optional[int] = None
    last_error: str = ""
    packet_count_sent: int = 0
    packet_count_received: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # 运行时状态标志 (由具体子类在 get_status() 中刷新)
    is_connected: bool = False
    is_spawned: bool = False
    is_running: bool = False

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "ap_id": self.ap_id,
            "ap_type": self.ap_type,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "binary_path": self.binary_path,
            "bind_address": self.bind_address,
            "bind_port": self.bind_port,
            "pid": self.pid,
            "last_error": self.last_error,
            "packet_count_sent": self.packet_count_sent,
            "packet_count_received": self.packet_count_received,
            "metadata": dict(self.metadata),
            "is_connected": self.is_connected,
            "is_spawned": self.is_spawned,
            "is_running": self.is_running,
        }


# ======================================================================
# 抽象基类
# ======================================================================


#: 数据包处理器回调签名: ``Callable[[dict], Awaitable[None] | None]``
PacketHandler = Callable[[dict[str, Any]], Union[None, Awaitable[None]]]

#: 状态变更回调签名: ``Callable[[AccessPoint, AccessPointStatus], None]``
StatusCallback = Callable[["AccessPoint", AccessPointStatus], None]


class AccessPoint(abc.ABC):
    """接入点抽象基类。

    子类必须实现以下五个核心方法:

        - :meth:`start`        启动接入点
        - :meth:`stop`         停止接入点
        - :meth:`send_packet`  发送数据包到服务器
        - :meth:`on_packet`    注册数据包接收回调
        - :meth:`get_status`   获取接入点运行状态信息

    类属性:
        launch_type: 接入点人类可读名称（子类必须覆盖，如 ``"NeOmega"``）。
        binary_name_patterns: 候选二进制文件名模板列表。
            使用 ``{system}`` / ``{arch}`` / ``{ext}`` 占位符。
        default_start_port: 本地端口搜索起点。

    Args:
        config: 接入点配置字典。各子类自行解析所需字段。
            常用键: ``server_code`` / ``server_password`` / ``auth_server`` /
            ``api_key`` / ``server_address`` / ``server_port`` /
            ``binary_dir`` / ``bind_host`` / ``extra_args``。
        status_callback: 状态变更回调函数（可选）。
    """

    #: 子类必须覆盖
    launch_type: str = ""
    #: 子类可覆盖:``{system}`` / ``{arch}`` / ``{ext}`` 占位符
    binary_name_patterns: list[str] = []
    #: 子类可覆盖:本地模式端口起点
    default_start_port: int = 0

    def __init__(
        self,
        config: dict[str, Any],
        status_callback: Optional[StatusCallback] = None,
    ) -> None:
        self.config: dict[str, Any] = config
        self.status_callback: Optional[StatusCallback] = status_callback
        # 运行时状态
        self.info: AccessPointInfo = AccessPointInfo(ap_type=self.launch_type)
        # 子进程引用（NeOmega / FateArk 使用）
        self.proc: Optional[subprocess.Popen] = None
        # 数据包处理器列表
        self._packet_handlers: list[PacketHandler] = []
        # stdout / stderr 缓存（崩溃诊断用）
        self._stdout_buf: bytes = b""
        self._stderr_buf: bytes = b""

    # ------------------------------------------------------------------ #
    # 抽象接口 —— 子类必须实现
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    async def start(self) -> None:
        """启动接入点。

        本地模式:查找并拉起二进制子进程，建立通信通道。
        远端模式:直接连接到已在别处启动的接入点。

        Raises:
            BinaryNotFoundError: 本地模式下找不到二进制文件。
            SubprocessCrashedError: 子进程启动后立即退出。
            ConnectionTimeoutError: 无法在超时内建立连接。
        """

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止接入点。

        关闭通信通道、终止子进程（若有）、释放资源。
        可安全重复调用。
        """

    @abc.abstractmethod
    async def send_packet(self, packet: dict[str, Any]) -> bool:
        """发送数据包到服务器（经由接入点转发）。

        Args:
            packet: 数据包字典，格式由具体接入点协议决定。

        Returns:
            ``True`` 表示发送成功，``False`` 表示发送失败。
        """

    @abc.abstractmethod
    async def on_packet(self, handler: PacketHandler) -> None:
        """注册数据包接收回调。

        当接入点从服务器收到数据包时，会依次调用所有已注册的处理器。

        Args:
            handler: 回调函数，接收一个 ``dict`` 参数。
                可以是普通函数或协程函数。
        """

    @abc.abstractmethod
    def get_status(self) -> AccessPointInfo:
        """获取接入点当前运行状态信息。

        Returns:
            :class:`AccessPointInfo` 实例。
        """

    # ------------------------------------------------------------------ #
    # 通用辅助方法
    # ------------------------------------------------------------------ #

    def update_status(self, status: AccessPointStatus) -> None:
        """更新运行状态并触发回调。

        状态变化时打印彩色日志，并调用 ``status_callback``（若有）。

        Args:
            status: 新状态。
        """
        old = self.info.status
        self.info.status = status
        timestamp = time.strftime("%H:%M:%S")

        # 状态 -> 颜色 / 图标 映射
        status_styles = {
            AccessPointStatus.IDLE: (Colors.DIM, "[]"),
            AccessPointStatus.LAUNCHING: (Colors.YELLOW, "[~]"),
            AccessPointStatus.RUNNING: (Colors.GREEN, "[+]"),
            AccessPointStatus.CRASHED: (Colors.RED, "[!]\t"),
            AccessPointStatus.DISCONNECTED: (Colors.MAGENTA, "[-]"),
        }
        color, icon = status_styles.get(status, (Colors.WHITE, "[?]"))

        if old != status:
            msg = (
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{color}{icon} {Colors.BOLD}[{self.launch_type}]{Colors.RESET} "
                f"{color}状态变更: {old.value} -> {status.value}{Colors.RESET}"
            )
            print(msg, flush=True)
            logger.info(f"[{self.launch_type}] 状态: {old.value} -> {status.value}")

            # 触发回调
            if self.status_callback is not None:
                try:
                    self.status_callback(self, status)
                except Exception as exc:
                    logger.warning(f"status_callback 抛出异常(忽略): {exc}")

    def register_packet_handler(self, handler: PacketHandler) -> None:
        """注册数据包处理器（内部辅助方法）。

        Args:
            handler: 处理器函数或协程函数。
        """
        self._packet_handlers.append(handler)

    async def _dispatch_packet(self, packet: dict[str, Any]) -> None:
        """内部方法：分发收到的数据包给所有已注册的处理器。

        Args:
            packet: 收到的数据包字典。
        """
        self.info.packet_count_received += 1
        for handler in self._packet_handlers:
            try:
                result = handler(packet)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.error(f"数据包处理器异常: {exc}", exc_info=True)

    def find_binary(self) -> Optional[Path]:
        """根据 ``binary_name_patterns`` 在配置的目录中查找二进制文件。

        配置中的 ``binary_dir`` 指定搜索目录。
        二进制文件名通过 ``{system}`` / ``{arch}`` / ``{ext}`` 模板匹配。

        Returns:
            找到的第一个匹配路径;未配置目录或无匹配时返回 ``None``。
        """
        binary_dir = self.config.get("binary_dir")
        if binary_dir is None:
            return None
        bin_dir = Path(binary_dir)
        if not bin_dir.is_dir():
            return None

        system_name = _system_name()
        arch_name = _arch_name()
        ext = ".exe" if os.name == "nt" else ""

        for pattern in self.binary_name_patterns:
            try:
                name = pattern.format(
                    system=system_name,
                    arch=arch_name,
                    ext=ext,
                )
            except (KeyError, IndexError):
                continue
            candidate = bin_dir / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
            # Windows 下 .exe 后缀缺失时也可尝试
            if ext and not name.endswith(ext):
                candidate2 = bin_dir / (name + ext)
                if candidate2.is_file() and os.access(candidate2, os.X_OK):
                    return candidate2
        return None

    async def _get_free_port(self, start: int) -> int:
        """从 ``start`` 开始找一个空闲的 TCP 端口。

        将同步 bind/close 操作放到默认 executor 中执行，
        避免阻塞事件循环。

        Args:
            start: 端口搜索起点。

        Returns:
            第一个可绑定的空闲端口号。

        Raises:
            OSError: 搜索区间内找不到空闲端口。
        """
        loop = asyncio.get_running_loop()
        bind_host = self.config.get("bind_host", "127.0.0.1")

        def _find() -> int:
            for port in range(start, start + 200):
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        s.bind((bind_host, port))
                    except OSError:
                        continue
                    return port
            raise OSError(f"在 [{start}, {start + 200}) 内找不到空闲端口")

        return await loop.run_in_executor(None, _find)

    def _log(self, message: str, level: str = "info") -> None:
        """打印带接入点名称前缀的彩色日志。

        Args:
            message: 日志内容。
            level: 日志级别 (``"info"`` / ``"warning"`` / ``"error"`` / ``"debug"``)。
        """
        timestamp = time.strftime("%H:%M:%S")
        level_styles = {
            "info": (Colors.CYAN, "[i]"),
            "warning": (Colors.YELLOW, "[!]"),
            "error": (Colors.RED, "[X]"),
            "debug": (Colors.DIM, "[d]"),
            "protocol": (Colors.CYAN, "[P]"),
            "success": (Colors.GREEN, "[+]"),
        }
        color, icon = level_styles.get(level, (Colors.WHITE, "[?]"))
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{color}{icon} {Colors.BOLD}[{self.launch_type}]{Colors.RESET} "
            f"{color}{message}{Colors.RESET}",
            flush=True,
        )
        if level == "error":
            self.info.last_error = message
            logger.error(f"[{self.launch_type}] {message}")
        elif level == "warning":
            logger.warning(f"[{self.launch_type}] {message}")
        else:
            logger.info(f"[{self.launch_type}] {message}")

    # ------------------------------------------------------------------ #
    # 上下文管理器支持
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "AccessPoint":
        """异步上下文管理器入口:启动接入点。"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        """异步上下文管理器出口:停止接入点。"""
        await self.stop()


# ======================================================================
# 平台识别辅助函数
# ======================================================================


def _system_name() -> str:
    """返回当前操作系统的标识字符串。

    用于匹配接入点二进制文件名中的 ``{system}`` 占位符。

    Returns:
        - Windows -> ``"windows"``
        - Linux   -> ``"linux"``
        - Termux  -> ``"android"``
        - macOS   -> ``"darwin"``
    """
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform.startswith("linux"):
        if "com.termux" in (sys.prefix or "") or os.path.exists("/data/data/com.termux"):
            return "android"
        return "linux"
    if sys.platform.startswith("darwin"):
        return "darwin"
    return sys.platform


def _arch_name() -> str:
    """返回当前 CPU 架构的标识字符串。

    Returns:
        - x86_64 / amd64 -> ``"amd64"``
        - aarch64 / arm64 -> ``"arm64"``
        - i386 / i686     -> ``"386"``
        - 其它            -> ``uname -m`` 原值
    """
    machine = ""
    if hasattr(os, "uname"):
        machine = os.uname().machine.lower()
    else:
        import platform

        machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("i386", "i686", "x86"):
        return "386"
    return machine or "amd64"


__all__ = [
    # 颜色
    "Colors",
    # 异常
    "AccessPointError",
    "NetworkError",
    "ConnectionTimeoutError",
    "BinaryNotFoundError",
    "SubprocessCrashedError",
    "AccountBannedError",
    "InvalidCredentialsError",
    "VersionTooLowError",
    "ServerFullError",
    "ServerNotFoundError",
    "ServerRejectedError",
    "ProtocolError",
    "LoginFailedError",
    # 枚举 / 数据类
    "AccessPointStatus",
    "AccessPointInfo",
    # 基类
    "AccessPoint",
    "PacketHandler",
    "StatusCallback",
    # 辅助函数
    "_system_name",
    "_arch_name",
]
