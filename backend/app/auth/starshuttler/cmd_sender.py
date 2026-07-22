"""cmd_sender - 命令发送器模块。

逆向自 NovaBuilder 的 StarShuttler CmdSender, 来源:
    - /workspace/novuilder_reverse/REPORT.txt (第 380-403 行)
    - /workspace/novuilder_reverse/strings_commands.txt
    - /workspace/novuilder_reverse/nbt_timing.txt

命令发送器负责向游戏服务器发送命令并接收响应。

核心方法 (逆向自 strings_commands.txt):
    sendCommandWithResp()          -- 发送命令并等待响应
    sendCommandWithRespNoTimeout() -- 发送命令无超时
    SendWSCommandWithResp()        -- WebSocket 命令
    SendWSCommandWithTimeout()     -- WebSocket 带超时
    packCommandRequest()           -- 打包命令请求

回调系统 (逆向自 REPORT.txt 第 395-399 行):
    CommandRequestCallback.SetCommandRequestCallback()
    CommandRequestCallback.DeleteCommandRequestCallback()
    CommandRequestCallback.onCommandOutput()
    CommandRequestCallback.handleConnClose()

超时处理 (逆向自 strings):
    "sendCommandWithResp: Command request %#v (origin = %d) is time out
     (timeout = %v seconds)"

数据包 (逆向自 REPORT.txt 第 387-393 行):
    CommandRequest.ID / Marshal
    CommandOutput.ID / Marshal
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.starshuttler.cmd_sender")



#: 命令发送最大重试次数 (适配 PocketTerm: 从 game_interface 同步以保证模块独立可用)
MAX_RETRIES: int = 3
# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 默认命令超时 (秒), 逆向自 sendCommandWithResp 超时
DEFAULT_TIMEOUT: float = 10.0

#: 无超时标记
NO_TIMEOUT: float = -1.0

#: 最大等待队列长度
MAX_QUEUE_LENGTH: int = 1000

#: 命令请求超时消息 (逆向自 strings)
MSG_COMMAND_TIMEOUT: str = (
    "sendCommandWithResp: Command request %#v (origin = %d) is time out "
    "(timeout = %v seconds)"
)

#: 默认命令源类型
COMMAND_ORIGIN_PLAYER: int = 0
COMMAND_ORIGIN_BLOCK: int = 1
COMMAND_ORIGIN_DEVCONSOLE: int = 2
COMMAND_ORIGIN_TEST: int = 3


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class CommandOriginType(Enum):
    """命令源类型 (逆向自 protocol/packet.CommandRequest)。"""

    PLAYER = COMMAND_ORIGIN_PLAYER
    BLOCK = COMMAND_ORIGIN_BLOCK
    DEV_CONSOLE = COMMAND_ORIGIN_DEVCONSOLE
    TEST = COMMAND_ORIGIN_TEST


class CommandStatus(Enum):
    """命令状态。"""

    PENDING = auto()     # 等待中
    SENT = auto()        # 已发送
    SUCCESS = auto()     # 成功
    FAILED = auto()      # 失败
    TIMEOUT = auto()     # 超时
    CANCELLED = auto()   # 已取消


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class CommandRequest:
    """命令请求 (逆向自 packet.CommandRequest)。

    逆向自 CommandRequest.ID / Marshal。
    """

    command: str = ""                                    # 命令字符串
    origin_type: int = COMMAND_ORIGIN_PLAYER             # 命令源类型
    origin_id: int = 0                                   # 命令源 ID
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 请求 ID
    timestamp: float = field(default_factory=time.time)  # 时间戳
    timeout: float = DEFAULT_TIMEOUT                     # 超时 (秒)
    status: CommandStatus = CommandStatus.PENDING        # 当前状态

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "command": self.command,
            "origin_type": self.origin_type,
            "origin_id": self.origin_id,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "timeout": self.timeout,
            "status": self.status.name,
        }


@dataclass
class CommandOutput:
    """命令输出 (逆向自 packet.CommandOutput)。

    逆向自 CommandOutput.ID / Marshal。
    """

    request_id: str = ""                                 # 对应的请求 ID
    success: bool = False                                # 是否成功
    success_count: int = 0                               # 成功计数
    output_messages: list[dict[str, Any]] = field(default_factory=list)  # 输出消息
    error: str = ""                                      # 错误信息
    timestamp: float = field(default_factory=time.time)  # 时间戳

    @property
    def output_text(self) -> str:
        """获取所有输出文本。"""
        return "\n".join(msg.get("message", "") for msg in self.output_messages)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "request_id": self.request_id,
            "success": self.success,
            "success_count": self.success_count,
            "output_messages": self.output_messages,
            "error": self.error,
            "timestamp": self.timestamp,
        }


# -------------------------------------------------------------------- #
# 命令请求回调系统
# -------------------------------------------------------------------- #


class CommandRequestCallback:
    """命令请求回调系统。

    逆向自 REPORT.txt 第 395-399 行:
        CommandRequestCallback.SetCommandRequestCallback()
        CommandRequestCallback.DeleteCommandRequestCallback()
        CommandRequestCallback.onCommandOutput()
        CommandRequestCallback.handleConnClose()

    管理 origin ID 到回调函数的映射, 当 CommandOutput 到达时
    调用对应的回调。
    """

    def __init__(self) -> None:
        self._callbacks: dict[int, Callable[[CommandOutput], None]] = {}
        self._events: dict[int, threading.Event] = {}
        self._results: dict[int, CommandOutput] = {}
        self._lock: threading.RLock = threading.RLock()
        self._next_origin: int = 1
        logger.debug("CommandRequestCallback initialized")

    def set_command_request_callback(
        self,
        origin_id: int,
        callback: Callable[[CommandOutput], None],
    ) -> None:
        """设置命令请求回调 (逆向自 SetCommandRequestCallback)。

        Args:
            origin_id: 命令源 ID。
            callback: 回调函数。
        """
        with self._lock:
            self._callbacks[origin_id] = callback
            self._events[origin_id] = threading.Event()
        logger.debug("Set callback for origin=%d", origin_id)

    def delete_command_request_callback(self, origin_id: int) -> None:
        """删除命令请求回调 (逆向自 DeleteCommandRequestCallback)。

        Args:
            origin_id: 命令源 ID。
        """
        with self._lock:
            self._callbacks.pop(origin_id, None)
            self._events.pop(origin_id, None)
            self._results.pop(origin_id, None)
        logger.debug("Deleted callback for origin=%d", origin_id)

    def on_command_output(self, output: CommandOutput, origin_id: int) -> None:
        """命令输出到达 (逆向自 onCommandOutput)。

        Args:
            output: 命令输出。
            origin_id: 命令源 ID。
        """
        with self._lock:
            callback = self._callbacks.get(origin_id)
            event = self._events.get(origin_id)
            if event:
                self._results[origin_id] = output
                event.set()

        if callback:
            try:
                callback(output)
            except Exception:
                logger.exception("Command output callback failed for origin=%d", origin_id)

    def handle_conn_close(self) -> None:
        """连接关闭处理 (逆向自 handleConnClose)。

        清理所有待处理的回调。
        """
        with self._lock:
            count = len(self._callbacks)
            for origin_id, event in self._events.items():
                failed_output = CommandOutput(
                    request_id="",
                    success=False,
                    error="connection closed",
                )
                self._results[origin_id] = failed_output
                event.set()
            self._callbacks.clear()

        logger.info("Connection closed, cleared %d pending callbacks", count)

    def wait_for_output(
        self,
        origin_id: int,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> CommandOutput | None:
        """等待命令输出。

        Args:
            origin_id: 命令源 ID。
            timeout: 超时 (秒)。

        Returns:
            :class:`CommandOutput`, 超时返回 None。
        """
        with self._lock:
            event = self._events.get(origin_id)

        if event is None:
            return None

        if event.wait(timeout):
            with self._lock:
                return self._results.get(origin_id)

        logger.warning("Command timeout: origin=%d, timeout=%.1fs", origin_id, timeout)
        return None

    def allocate_origin_id(self) -> int:
        """分配新的命令源 ID。

        Returns:
            新的命令源 ID。
        """
        with self._lock:
            origin_id = self._next_origin
            self._next_origin += 1
            return origin_id


# -------------------------------------------------------------------- #
# 命令发送器
# -------------------------------------------------------------------- #


class CmdSender:
    """命令发送器。

    逆向自 StarShuttler 的命令发送系统, 提供:
        - send_command_with_resp: 发送命令并等待响应
        - send_command_with_resp_no_timeout: 发送命令无超时
        - send_ws_command_with_resp: WebSocket 命令
        - send_ws_command_with_timeout: WebSocket 带超时
        - pack_command_request: 打包命令请求

    逆向自 strings_commands.txt:
        _31YjzFd8.(*ZaWORhpM).SendWSCommandWithTimeout
        game_interface.(*Commands).SendWSCommandWithResp
        game_interface.(*Commands).SendWSCommandWithTimeout
    """

    def __init__(
        self,
        send_packet_func: Callable[[bytes], None] | None = None,
        callback: CommandRequestCallback | None = None,
    ) -> None:
        """初始化命令发送器。

        Args:
            send_packet_func: 发送数据包函数。
            callback: 命令请求回调系统。
        """
        self._send_packet_func: Optional[Callable[[bytes], None]] = send_packet_func
        self._callback: CommandRequestCallback = callback or CommandRequestCallback()
        self._pending_requests: dict[int, CommandRequest] = {}
        self._lock: threading.RLock = threading.RLock()
        self._stats: dict[str, int] = {
            "total_sent": 0,
            "total_success": 0,
            "total_failed": 0,
            "total_timeout": 0,
        }

        logger.debug("CmdSender initialized")

    @property
    def callback(self) -> CommandRequestCallback:
        """命令请求回调系统。"""
        return self._callback

    @property
    def stats(self) -> dict[str, int]:
        """统计信息。"""
        return dict(self._stats)

    def set_send_packet_func(self, func: Callable[[bytes], None]) -> None:
        """设置发送数据包函数。

        Args:
            func: 发送数据包函数。
        """
        self._send_packet_func = func
        logger.debug("Send packet function set")

    # ---------------------------------------------------------------- #
    # 命令发送 (逆向自 sendCommandWithResp)
    # ---------------------------------------------------------------- #

    def send_command_with_resp(
        self,
        command: str,
        timeout: float = DEFAULT_TIMEOUT,
        origin_type: int = COMMAND_ORIGIN_PLAYER,
    ) -> CommandOutput | None:
        """发送命令并等待响应 (逆向自 sendCommandWithResp)。

        Args:
            command: 命令字符串。
            timeout: 超时 (秒), -1 表示无超时。
            origin_type: 命令源类型。

        Returns:
            :class:`CommandOutput`, 超时返回 None。
        """
        if timeout == NO_TIMEOUT:
            return self.send_command_with_resp_no_timeout(command, origin_type)

        request = CommandRequest(
            command=command,
            origin_type=origin_type,
            timeout=timeout,
        )
        origin_id = self._callback.allocate_origin_id()
        request.origin_id = origin_id

        # 设置回调
        result_holder: list[CommandOutput | None] = [None]
        event = threading.Event()

        def on_output(output: CommandOutput) -> None:
            result_holder[0] = output
            event.set()

        self._callback.set_command_request_callback(origin_id, on_output)

        # 打包并发送
        with self._lock:
            self._pending_requests[origin_id] = request
            self._stats["total_sent"] += 1

        packet_data = self.pack_command_request(request)
        self._send_packet(packet_data)

        # 等待响应
        if event.wait(timeout):
            output = result_holder[0]
            if output and output.success:
                self._stats["total_success"] += 1
            else:
                self._stats["total_failed"] += 1
            self._callback.delete_command_request_callback(origin_id)
            with self._lock:
                self._pending_requests.pop(origin_id, None)
            return output
        else:
            self._stats["total_timeout"] += 1
            logger.warning(
                MSG_COMMAND_TIMEOUT, command, origin_id, timeout,
            )
            self._callback.delete_command_request_callback(origin_id)
            with self._lock:
                self._pending_requests.pop(origin_id, None)
            return CommandOutput(
                request_id=request.request_id,
                success=False,
                error=f"timeout after {timeout}s",
            )

    def send_command_with_resp_no_timeout(
        self,
        command: str,
        origin_type: int = COMMAND_ORIGIN_PLAYER,
    ) -> CommandOutput | None:
        """发送命令无超时 (逆向自 sendCommandWithRespNoTimeout)。

        Args:
            command: 命令字符串。
            origin_type: 命令源类型。

        Returns:
            :class:`CommandOutput`。
        """
        request = CommandRequest(
            command=command,
            origin_type=origin_type,
            timeout=NO_TIMEOUT,
        )
        origin_id = self._callback.allocate_origin_id()
        request.origin_id = origin_id

        result_holder: list[CommandOutput | None] = [None]
        event = threading.Event()

        def on_output(output: CommandOutput) -> None:
            result_holder[0] = output
            event.set()

        self._callback.set_command_request_callback(origin_id, on_output)

        with self._lock:
            self._pending_requests[origin_id] = request
            self._stats["total_sent"] += 1

        packet_data = self.pack_command_request(request)
        self._send_packet(packet_data)

        # 无限等待
        event.wait()

        output = result_holder[0]
        if output and output.success:
            self._stats["total_success"] += 1
        else:
            self._stats["total_failed"] += 1

        self._callback.delete_command_request_callback(origin_id)
        with self._lock:
            self._pending_requests.pop(origin_id, None)

        return output

    def send_ws_command_with_resp(
        self,
        command: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> CommandOutput | None:
        """WebSocket 命令 (逆向自 SendWSCommandWithResp)。

        逆向自 game_interface.(*Commands).SendWSCommandWithResp

        Args:
            command: 命令字符串。
            timeout: 超时 (秒)。

        Returns:
            :class:`CommandOutput`。
        """
        logger.debug("SendWSCommandWithResp: %s", command)
        return self.send_command_with_resp(command, timeout=timeout)

    def send_ws_command_with_timeout(
        self,
        command: str,
        timeout: float = DEFAULT_TIMEOUT,
        retry: int = MAX_RETRIES,
    ) -> CommandOutput | None:
        """WebSocket 带超时 (逆向自 SendWSCommandWithTimeout)。

        逆向自 _31YjzFd8.(*ZaWORhpM).SendWSCommandWithTimeout
        和 game_interface.(*Commands).SendWSCommandWithTimeout

        带重试机制的 WebSocket 命令发送。

        Args:
            command: 命令字符串。
            timeout: 超时 (秒)。
            retry: 重试次数。

        Returns:
            :class:`CommandOutput`。
        """
        logger.debug(
            "SendWSCommandWithTimeout: %s, timeout=%.1f, retry=%d",
            command, timeout, retry,
        )

        last_output: CommandOutput | None = None
        for attempt in range(retry + 1):
            try:
                output = self.send_command_with_resp(command, timeout=timeout)
                if output and output.success:
                    return output
                last_output = output
                if attempt < retry:
                    logger.info(
                        "Retrying command (attempt %d/%d): %s",
                        attempt + 2, retry + 1, command,
                    )
                    time.sleep(0.5 * (attempt + 1))
            except Exception as exc:
                logger.warning("SendWSCommandWithTimeout attempt %d failed: %s", attempt + 1, exc)
                last_output = CommandOutput(success=False, error=str(exc))

        return last_output

    # ---------------------------------------------------------------- #
    # 打包命令请求 (逆向自 packCommandRequest)
    # ---------------------------------------------------------------- #

    def pack_command_request(self, request: CommandRequest) -> bytes:
        """打包命令请求 (逆向自 packCommandRequest)。

        逆向自 CommandRequest.Marshal。

        将命令请求序列化为二进制数据包。

        Args:
            request: 命令请求。

        Returns:
            二进制数据包数据。
        """
        # 模拟协议序列化
        # 实际实现需要完整的协议层
        import struct

        # 命令请求包格式 (简化):
        #   packet_id (varint) + origin_type (varint) + origin_id (varint)
        #   + command_length (varint) + command (string) + request_id (string)

        command_bytes = request.command.encode("utf-8")
        request_id_bytes = request.request_id.encode("utf-8")

        data = bytearray()
        # packet ID for CommandRequest (逆向自 packet.CommandRequest.ID)
        data.extend(_encode_varint(0x4D))
        # origin type
        data.extend(_encode_varint(request.origin_type))
        # origin id
        data.extend(_encode_varint(request.origin_id))
        # command
        data.extend(_encode_varint(len(command_bytes)))
        data.extend(command_bytes)
        # request id
        data.extend(_encode_varint(len(request_id_bytes)))
        data.extend(request_id_bytes)

        logger.debug(
            "Packed command request: origin=%d, command=%s, size=%d",
            request.origin_id, request.command[:50], len(data),
        )
        return bytes(data)

    # ---------------------------------------------------------------- #
    # 命令输出处理 (逆向自 onCommandOutput)
    # ---------------------------------------------------------------- #

    def on_command_output(self, output: CommandOutput, origin_id: int) -> None:
        """命令输出到达 (逆向自 onCommandOutput)。

        由 PacketDispatcher 调用。

        Args:
            output: 命令输出。
            origin_id: 命令源 ID。
        """
        self._callback.on_command_output(output, origin_id)

    def handle_conn_close(self) -> None:
        """连接关闭处理 (逆向自 handleConnClose)。"""
        self._callback.handle_conn_close()
        with self._lock:
            self._pending_requests.clear()
        logger.info("CmdSender: connection closed, all pending requests cleared")

    # ---------------------------------------------------------------- #
    # 内部方法
    # ---------------------------------------------------------------- #

    def _send_packet(self, data: bytes) -> None:
        """发送数据包。"""
        if self._send_packet_func:
            try:
                self._send_packet_func(data)
            except Exception as exc:
                logger.error("Failed to send packet: %s", exc)
                raise
        else:
            logger.warning("No send packet function configured")

    def get_pending_count(self) -> int:
        """获取待处理命令数。"""
        with self._lock:
            return len(self._pending_requests)


# -------------------------------------------------------------------- #
# 工具函数
# -------------------------------------------------------------------- #


def _encode_varint(value: int) -> bytes:
    """编码 varint。

    Args:
        value: 要编码的值。

    Returns:
        编码后的字节。
    """
    buf = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        buf.append(byte)
        if not value:
            break
    return bytes(buf)


def _decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """解码 varint。

    Args:
        data: 数据。
        offset: 偏移量。

    Returns:
        (value, new_offset)。
    """
    result = 0
    shift = 0
    pos = offset
    while pos < len(data):
        byte = data[pos]
        result |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            break
        shift += 7
        if shift >= 64:
            raise ValueError("varint too long")
    return result, pos


__all__ = [
    # 常量
    "DEFAULT_TIMEOUT", "NO_TIMEOUT", "MAX_QUEUE_LENGTH",
    "MSG_COMMAND_TIMEOUT",
    "COMMAND_ORIGIN_PLAYER", "COMMAND_ORIGIN_BLOCK",
    "COMMAND_ORIGIN_DEVCONSOLE", "COMMAND_ORIGIN_TEST",
    # 枚举
    "CommandOriginType", "CommandStatus",
    # 数据结构
    "CommandRequest", "CommandOutput",
    # 回调系统
    "CommandRequestCallback",
    # 命令发送器
    "CmdSender",
    # 工具函数
    "_encode_varint", "_decode_varint",
]
