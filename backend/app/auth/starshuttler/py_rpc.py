"""py_rpc - Python RPC 事件系统模块。

逆向自 NovaBuilder 的 StarShuttler PyRPC, 来源:
    - /workspace/novuilder_reverse/REPORT.txt (第 458-513 行)
    - /workspace/novuilder_reverse/anticheat.txt

PyRPC 是 StarShuttler 的 Python RPC 事件系统, 用于客户端与服务器
之间的双向事件通信。

PyRPC 事件系统 (逆向自 mod_event, REPORT.txt 第 458-468 行):

    client_to_server/minecraft:
        ai_command           -- AI 命令
        preset               -- 预设
        vip_event_system     -- VIP 事件系统

    server_to_client/minecraft:
        ai_command           -- ExecuteCommandOutputEvent, AvailableCheckFailed
        achievement          -- 成就
        chat_phrases         -- 聊天短语
        chat_extension       -- 聊天扩展 (NeteaseUserUID)
        pet                  -- 宠物

MCPC 挑战接口 (逆向自 REPORT.txt 第 500-513 行):
    py_rpc.GetMCPCheckNum            -- 获取 MCP 检查编号
    py_rpc.GetMCPCheckNumSecondArg   -- 获取 MCP 检查第二参数
    py_rpc.SetMCPCheckNum            -- 设置 MCP 检查编号

AI 命令事件:
    ai_command.AvailableCheckFailed       -- 可用命令检查失败
    ai_command.ExecuteCommandOutputEvent  -- 执行命令输出事件
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.starshuttler.py_rpc")


# -------------------------------------------------------------------- #
# 常量 (逆向自 REPORT.txt)
# -------------------------------------------------------------------- #

#: PyRPC 事件方向
DIRECTION_CLIENT_TO_SERVER: str = "client_to_server"
DIRECTION_SERVER_TO_CLIENT: str = "server_to_client"

#: 命名空间
NAMESPACE_MINECRAFT: str = "minecraft"

#: 事件通道 (逆向自 REPORT.txt 第 458-468 行)
CHANNEL_AI_COMMAND: str = "ai_command"
CHANNEL_PRESET: str = "preset"
CHANNEL_VIP_EVENT_SYSTEM: str = "vip_event_system"
CHANNEL_ACHIEVEMENT: str = "achievement"
CHANNEL_CHAT_PHRASES: str = "chat_phrases"
CHANNEL_CHAT_EXTENSION: str = "chat_extension"
CHANNEL_PET: str = "pet"

#: MCPC 挑战接口 (逆向自 REPORT.txt 第 500-503 行)
RPC_GET_MCP_CHECK_NUM: str = "GetMCPCheckNum"
RPC_GET_MCP_CHECK_NUM_SECOND_ARG: str = "GetMCPCheckNumSecondArg"
RPC_SET_MCP_CHECK_NUM: str = "SetMCPCheckNum"

#: AI 命令事件类型 (逆向自 REPORT.txt 第 505-507 行)
EVENT_AVAILABLE_CHECK_FAILED: str = "AvailableCheckFailed"
EVENT_EXECUTE_COMMAND_OUTPUT: str = "ExecuteCommandOutputEvent"

#: 默认超时 (秒)
DEFAULT_RPC_TIMEOUT: float = 30.0

#: VIP 皮肤同步 (逆向自 "*py_rpc.SyncVipSkinUUID")
RPC_SYNC_VIP_SKIN_UUID: str = "SyncVipSkinUUID"


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class PyRPCDirection(Enum):
    """RPC 事件方向。"""

    CLIENT_TO_SERVER = DIRECTION_CLIENT_TO_SERVER
    SERVER_TO_CLIENT = DIRECTION_SERVER_TO_CLIENT


class PyRPCEventType(Enum):
    """RPC 事件类型。"""

    REQUEST = auto()      # 请求 (需要响应)
    NOTIFICATION = auto()  # 通知 (不需要响应)
    RESPONSE = auto()     # 响应


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class PyRPCEvent:
    """PyRPC 事件。

    逆向自 StarShuttler py_rpc 事件系统。
    """

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # 事件 ID
    channel: str = ""                                                 # 事件通道
    namespace: str = NAMESPACE_MINECRAFT                              # 命名空间
    direction: PyRPCDirection = PyRPCDirection.CLIENT_TO_SERVER       # 方向
    event_type: PyRPCEventType = PyRPCEventType.NOTIFICATION          # 事件类型
    data: dict[str, Any] = field(default_factory=dict)                # 事件数据
    timestamp: float = field(default_factory=time.time)               # 时间戳
    request_id: str = ""                                              # 请求 ID (用于配对)
    response_to: str = ""                                             # 响应对应的事件 ID

    @property
    def full_path(self) -> str:
        """完整事件路径 (如 "client_to_server/minecraft/ai_command")。"""
        return f"{self.direction.value}/{self.namespace}/{self.channel}"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "event_id": self.event_id,
            "channel": self.channel,
            "namespace": self.namespace,
            "direction": self.direction.value,
            "event_type": self.event_type.name,
            "data": self.data,
            "timestamp": self.timestamp,
            "request_id": self.request_id,
            "response_to": self.response_to,
        }

    def to_json(self) -> str:
        """转换为 JSON 字符串。"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PyRPCEvent:
        """从字典创建事件。"""
        return cls(
            event_id=data.get("event_id", str(uuid.uuid4())),
            channel=data.get("channel", ""),
            namespace=data.get("namespace", NAMESPACE_MINECRAFT),
            direction=PyRPCDirection(data.get("direction", DIRECTION_CLIENT_TO_SERVER)),
            event_type=PyRPCEventType[data.get("event_type", "NOTIFICATION")],
            data=data.get("data", {}),
            timestamp=data.get("timestamp", time.time()),
            request_id=data.get("request_id", ""),
            response_to=data.get("response_to", ""),
        )

    @classmethod
    def from_json(cls, json_str: str) -> PyRPCEvent:
        """从 JSON 字符串创建事件。"""
        return cls.from_dict(json.loads(json_str))


@dataclass
class PyRPCChannel:
    """PyRPC 事件通道。

    逆向自 mod_event 的事件通道。
    """

    name: str                                           # 通道名称
    direction: PyRPCDirection                           # 方向
    namespace: str = NAMESPACE_MINECRAFT                # 命名空间
    handlers: list[Callable[[PyRPCEvent], Any]] = field(default_factory=list)  # 处理器列表

    @property
    def full_path(self) -> str:
        """完整通道路径。"""
        return f"{self.direction.value}/{self.namespace}/{self.name}"

    def add_handler(self, handler: Callable[[PyRPCEvent], Any]) -> None:
        """添加处理器。"""
        self.handlers.append(handler)

    def remove_handler(self, handler: Callable[[PyRPCEvent], Any]) -> None:
        """移除处理器。"""
        try:
            self.handlers.remove(handler)
        except ValueError:
            pass

    def dispatch(self, event: PyRPCEvent) -> Any:
        """分发事件到所有处理器。

        Args:
            event: 要分发的事件。

        Returns:
            第一个处理器返回的结果 (如果有)。
        """
        result: Any = None
        for handler in self.handlers:
            try:
                handler_result = handler(event)
                if result is None and handler_result is not None:
                    result = handler_result
            except Exception:
                logger.exception(
                    "Channel %s handler %s failed",
                    self.full_path, type(handler).__name__,
                )
        return result


# -------------------------------------------------------------------- #
# 事件处理器类型
# -------------------------------------------------------------------- #


class PyRPCEventHandler:
    """PyRPC 事件处理器。

    封装一个事件处理函数及其元数据。
    """

    def __init__(
        self,
        channel: str,
        handler: Callable[[PyRPCEvent], Any],
        direction: PyRPCDirection = PyRPCDirection.SERVER_TO_CLIENT,
        namespace: str = NAMESPACE_MINECRAFT,
    ) -> None:
        """初始化事件处理器。

        Args:
            channel: 事件通道。
            handler: 处理函数。
            direction: 事件方向。
            namespace: 命名空间。
        """
        self.channel: str = channel
        self.handler: Callable[[PyRPCEvent], Any] = handler
        self.direction: PyRPCDirection = direction
        self.namespace: str = namespace
        self.call_count: int = 0
        self.error_count: int = 0
        self.last_call_time: float = 0.0

    @property
    def full_path(self) -> str:
        """完整路径。"""
        return f"{self.direction.value}/{self.namespace}/{self.channel}"

    def __call__(self, event: PyRPCEvent) -> Any:
        """调用处理器。"""
        self.call_count += 1
        self.last_call_time = time.time()
        try:
            return self.handler(event)
        except Exception:
            self.error_count += 1
            raise


# -------------------------------------------------------------------- #
# PyRPC 核心
# -------------------------------------------------------------------- #


class PyRPC:
    """PyRPC 事件系统。

    逆向自 StarShuttler py_rpc 包。

    功能:
        1. 注册/取消注册事件处理器
        2. 发送事件到服务器
        3. 接收并分发服务器事件
        4. 请求-响应模式 (带超时)
        5. MCPC 挑战接口

    使用示例::

        rpc = PyRPC(send_func=my_send_func)
        rpc.on("ai_command", handle_ai_command)
        rpc.emit("preset", {"key": "value"})
        result = rpc.call("GetMCPCheckNum", timeout=10.0)
    """

    def __init__(
        self,
        send_func: Callable[[str], None] | None = None,
    ) -> None:
        """初始化 PyRPC。

        Args:
            send_func: 发送函数 (接收 JSON 字符串)。
        """
        self._send_func: Optional[Callable[[str], None]] = send_func
        self._channels: dict[str, PyRPCChannel] = {}
        self._handlers: dict[str, PyRPCEventHandler] = {}
        self._pending_requests: dict[str, threading.Event] = {}
        self._request_results: dict[str, Any] = {}
        self._lock: threading.RLock = threading.RLock()
        self._stats: dict[str, int] = {
            "total_sent": 0,
            "total_received": 0,
            "total_dispatched": 0,
            "total_errors": 0,
            "total_timeouts": 0,
        }
        self._mcp_check_num: int = 0
        self._mcp_check_second_arg: Any = None

        logger.debug("PyRPC initialized")

    @property
    def stats(self) -> dict[str, int]:
        """统计信息。"""
        return dict(self._stats)

    def set_send_func(self, func: Callable[[str], None]) -> None:
        """设置发送函数。

        Args:
            func: 发送函数。
        """
        self._send_func = func
        logger.debug("Send function set")

    # ---------------------------------------------------------------- #
    # 事件处理器注册
    # ---------------------------------------------------------------- #

    def on(
        self,
        channel: str,
        handler: Callable[[PyRPCEvent], Any],
        direction: PyRPCDirection = PyRPCDirection.SERVER_TO_CLIENT,
        namespace: str = NAMESPACE_MINECRAFT,
    ) -> PyRPCEventHandler:
        """注册事件处理器。

        逆向自 PyRPC 事件监听机制。

        Args:
            channel: 事件通道 (如 "ai_command")。
            handler: 处理函数。
            direction: 事件方向。
            namespace: 命名空间。

        Returns:
            :class:`PyRPCEventHandler` 实例。
        """
        path = f"{direction.value}/{namespace}/{channel}"
        event_handler = PyRPCEventHandler(channel, handler, direction, namespace)

        with self._lock:
            if path not in self._channels:
                self._channels[path] = PyRPCChannel(
                    name=channel, direction=direction, namespace=namespace
                )
            self._channels[path].add_handler(event_handler)
            self._handlers[path] = event_handler

        logger.debug("Registered handler for %s", path)
        return event_handler

    def off(
        self,
        channel: str,
        direction: PyRPCDirection = PyRPCDirection.SERVER_TO_CLIENT,
        namespace: str = NAMESPACE_MINECRAFT,
    ) -> None:
        """取消注册事件处理器。

        Args:
            channel: 事件通道。
            direction: 事件方向。
            namespace: 命名空间。
        """
        path = f"{direction.value}/{namespace}/{channel}"
        with self._lock:
            self._handlers.pop(path, None)
            ch = self._channels.get(path)
            if ch:
                ch.handlers.clear()

        logger.debug("Unregistered handler for %s", path)

    def clear_all_handlers(self) -> None:
        """清除所有处理器。"""
        with self._lock:
            count = len(self._handlers)
            self._handlers.clear()
            self._channels.clear()
        logger.info("Cleared %d handlers", count)

    # ---------------------------------------------------------------- #
    # 事件发送
    # ---------------------------------------------------------------- #

    def emit(
        self,
        channel: str,
        data: dict[str, Any] | None = None,
        namespace: str = NAMESPACE_MINECRAFT,
    ) -> bool:
        """发送通知事件 (不需要响应)。

        逆向自 client_to_server 事件发送。

        Args:
            channel: 事件通道。
            data: 事件数据。
            namespace: 命名空间。

        Returns:
            True 如果发送成功。
        """
        event = PyRPCEvent(
            channel=channel,
            namespace=namespace,
            direction=PyRPCDirection.CLIENT_TO_SERVER,
            event_type=PyRPCEventType.NOTIFICATION,
            data=data or {},
        )
        return self._send_event(event)

    def call(
        self,
        channel: str,
        data: dict[str, Any] | None = None,
        timeout: float = DEFAULT_RPC_TIMEOUT,
        namespace: str = NAMESPACE_MINECRAFT,
    ) -> Any:
        """发送请求事件并等待响应。

        逆向自 py_rpc 请求-响应模式。

        Args:
            channel: 事件通道。
            data: 事件数据。
            timeout: 超时 (秒)。
            namespace: 命名空间。

        Returns:
            响应数据, 超时返回 None。
        """
        event = PyRPCEvent(
            channel=channel,
            namespace=namespace,
            direction=PyRPCDirection.CLIENT_TO_SERVER,
            event_type=PyRPCEventType.REQUEST,
            data=data or {},
            request_id=str(uuid.uuid4()),
        )

        # 注册等待
        wait_event = threading.Event()
        with self._lock:
            self._pending_requests[event.request_id] = wait_event

        # 发送
        if not self._send_event(event):
            with self._lock:
                self._pending_requests.pop(event.request_id, None)
            return None

        # 等待响应
        if wait_event.wait(timeout):
            with self._lock:
                return self._request_results.pop(event.request_id, None)
        else:
            self._stats["total_timeouts"] += 1
            logger.warning("RPC call timeout: channel=%s, timeout=%.1fs", channel, timeout)
            with self._lock:
                self._pending_requests.pop(event.request_id, None)
            return None

    def respond(
        self,
        request_id: str,
        data: dict[str, Any] | None = None,
        namespace: str = NAMESPACE_MINECRAFT,
    ) -> bool:
        """发送响应事件。

        Args:
            request_id: 原始请求 ID。
            data: 响应数据。
            namespace: 命名空间。

        Returns:
            True 如果发送成功。
        """
        event = PyRPCEvent(
            namespace=namespace,
            direction=PyRPCDirection.CLIENT_TO_SERVER,
            event_type=PyRPCEventType.RESPONSE,
            data=data or {},
            request_id=str(uuid.uuid4()),
            response_to=request_id,
        )
        return self._send_event(event)

    def _send_event(self, event: PyRPCEvent) -> bool:
        """发送事件。"""
        if self._send_func is None:
            logger.error("No send function configured")
            return False

        try:
            json_str = event.to_json()
            self._send_func(json_str)
            self._stats["total_sent"] += 1
            logger.debug("Sent event: %s, channel=%s", event.event_id, event.channel)
            return True
        except Exception as exc:
            self._stats["total_errors"] += 1
            logger.error("Failed to send event: %s", exc)
            return False

    # ---------------------------------------------------------------- #
    # 事件接收
    # ---------------------------------------------------------------- #

    def receive(self, json_str: str) -> None:
        """接收事件。

        由网络层调用, 解析 JSON 并分发到注册的处理器。

        Args:
            json_str: JSON 格式的事件字符串。
        """
        self._stats["total_received"] += 1

        try:
            event = PyRPCEvent.from_json(json_str)
        except Exception as exc:
            self._stats["total_errors"] += 1
            logger.error("Failed to parse event: %s", exc)
            return

        self._dispatch_event(event)

    def receive_dict(self, data: dict[str, Any]) -> None:
        """接收事件 (字典形式)。

        Args:
            data: 事件字典。
        """
        self._stats["total_received"] += 1

        try:
            event = PyRPCEvent.from_dict(data)
        except Exception as exc:
            self._stats["total_errors"] += 1
            logger.error("Failed to create event: %s", exc)
            return

        self._dispatch_event(event)

    def _dispatch_event(self, event: PyRPCEvent) -> None:
        """分发事件。"""
        path = event.full_path

        # 检查是否是响应
        if event.event_type == PyRPCEventType.RESPONSE and event.response_to:
            with self._lock:
                wait_event = self._pending_requests.get(event.response_to)
                if wait_event:
                    self._request_results[event.response_to] = event.data
                    wait_event.set()
                    self._stats["total_dispatched"] += 1
                    logger.debug("Dispatched response: %s", event.response_to)
                    return

        # 分发到通道
        with self._lock:
            channel = self._channels.get(path)

        if channel:
            try:
                channel.dispatch(event)
                self._stats["total_dispatched"] += 1
                logger.debug("Dispatched event: %s -> %s", event.event_id, path)
            except Exception:
                self._stats["total_errors"] += 1
                logger.exception("Dispatch error for %s", path)
        else:
            logger.debug("No handler for %s", path)

    # ---------------------------------------------------------------- #
    # MCPC 挑战接口 (逆向自 REPORT.txt 第 500-503 行)
    # ---------------------------------------------------------------- #

    def get_mcp_check_num(self) -> int:
        """获取 MCP 检查编号 (逆向自 py_rpc.GetMCPCheckNum)。

        Returns:
            MCP 检查编号。
        """
        result = self.call(RPC_GET_MCP_CHECK_NUM, timeout=DEFAULT_RPC_TIMEOUT)
        if result is not None:
            self._mcp_check_num = int(result.get("check_num", 0))
        logger.debug("GetMCPCheckNum: %d", self._mcp_check_num)
        return self._mcp_check_num

    def get_mcp_check_num_second_arg(self) -> Any:
        """获取 MCP 检查第二参数 (逆向自 py_rpc.GetMCPCheckNumSecondArg)。

        Returns:
            第二参数。
        """
        result = self.call(RPC_GET_MCP_CHECK_NUM_SECOND_ARG, timeout=DEFAULT_RPC_TIMEOUT)
        if result is not None:
            self._mcp_check_second_arg = result.get("second_arg")
        logger.debug("GetMCPCheckNumSecondArg: %s", self._mcp_check_second_arg)
        return self._mcp_check_second_arg

    def set_mcp_check_num(self, check_num: int) -> bool:
        """设置 MCP 检查编号 (逆向自 py_rpc.SetMCPCheckNum)。

        Args:
            check_num: 检查编号。

        Returns:
            True 如果成功。
        """
        result = self.call(
            RPC_SET_MCP_CHECK_NUM,
            data={"check_num": check_num},
            timeout=DEFAULT_RPC_TIMEOUT,
        )
        success = result is not None and result.get("success", False)
        if success:
            self._mcp_check_num = check_num
        logger.debug("SetMCPCheckNum(%d): %s", check_num, success)
        return success

    def sync_vip_skin_uuid(self, uuid_str: str) -> bool:
        """同步 VIP 皮肤 UUID (逆向自 py_rpc.SyncVipSkinUUID)。

        Args:
            uuid_str: UUID 字符串。

        Returns:
            True 如果成功。
        """
        result = self.call(
            RPC_SYNC_VIP_SKIN_UUID,
            data={"uuid": uuid_str},
            timeout=DEFAULT_RPC_TIMEOUT,
        )
        success = result is not None and result.get("success", False)
        logger.debug("SyncVipSkinUUID(%s): %s", uuid_str, success)
        return success

    # ---------------------------------------------------------------- #
    # 预定义事件注册
    # ---------------------------------------------------------------- #

    def on_ai_command(
        self, handler: Callable[[PyRPCEvent], Any]
    ) -> PyRPCEventHandler:
        """注册 AI 命令事件处理器。

        逆向自 server_to_client/minecraft/ai_command。

        事件类型:
            - ExecuteCommandOutputEvent
            - AvailableCheckFailed

        Args:
            handler: 处理函数。

        Returns:
            :class:`PyRPCEventHandler`。
        """
        return self.on(CHANNEL_AI_COMMAND, handler, PyRPCDirection.SERVER_TO_CLIENT)

    def on_achievement(
        self, handler: Callable[[PyRPCEvent], Any]
    ) -> PyRPCEventHandler:
        """注册成就事件处理器。

        逆向自 server_to_client/minecraft/achievement。

        Args:
            handler: 处理函数。

        Returns:
            :class:`PyRPCEventHandler`。
        """
        return self.on(CHANNEL_ACHIEVEMENT, handler, PyRPCDirection.SERVER_TO_CLIENT)

    def on_chat_phrases(
        self, handler: Callable[[PyRPCEvent], Any]
    ) -> PyRPCEventHandler:
        """注册聊天短语事件处理器。

        逆向自 server_to_client/minecraft/chat_phrases。

        Args:
            handler: 处理函数。

        Returns:
            :class:`PyRPCEventHandler`。
        """
        return self.on(CHANNEL_CHAT_PHRASES, handler, PyRPCDirection.SERVER_TO_CLIENT)

    def on_chat_extension(
        self, handler: Callable[[PyRPCEvent], Any]
    ) -> PyRPCEventHandler:
        """注册聊天扩展事件处理器。

        逆向自 server_to_client/minecraft/chat_extension。

        处理 NeteaseUserUID 相关事件。

        Args:
            handler: 处理函数。

        Returns:
            :class:`PyRPCEventHandler`。
        """
        return self.on(CHANNEL_CHAT_EXTENSION, handler, PyRPCDirection.SERVER_TO_CLIENT)

    def on_pet(
        self, handler: Callable[[PyRPCEvent], Any]
    ) -> PyRPCEventHandler:
        """注册宠物事件处理器。

        逆向自 server_to_client/minecraft/pet。

        Args:
            handler: 处理函数。

        Returns:
            :class:`PyRPCEventHandler`。
        """
        return self.on(CHANNEL_PET, handler, PyRPCDirection.SERVER_TO_CLIENT)

    def emit_preset(self, data: dict[str, Any]) -> bool:
        """发送预设事件。

        逆向自 client_to_server/minecraft/preset。

        Args:
            data: 预设数据。

        Returns:
            True 如果发送成功。
        """
        return self.emit(CHANNEL_PRESET, data)

    def emit_vip_event(self, data: dict[str, Any]) -> bool:
        """发送 VIP 事件。

        逆向自 client_to_server/minecraft/vip_event_system。

        Args:
            data: VIP 事件数据。

        Returns:
            True 如果发送成功。
        """
        return self.emit(CHANNEL_VIP_EVENT_SYSTEM, data)

    def emit_ai_command(self, data: dict[str, Any]) -> bool:
        """发送 AI 命令事件。

        逆向自 client_to_server/minecraft/ai_command。

        Args:
            data: AI 命令数据。

        Returns:
            True 如果发送成功。
        """
        return self.emit(CHANNEL_AI_COMMAND, data)

    # ---------------------------------------------------------------- #
    # 连接管理
    # ---------------------------------------------------------------- #

    def handle_conn_close(self) -> None:
        """连接关闭处理。

        清理所有待处理的请求。
        """
        with self._lock:
            count = len(self._pending_requests)
            for request_id, event in self._pending_requests.items():
                event.set()
            self._pending_requests.clear()
            self._request_results.clear()

        logger.info("Connection closed, cleared %d pending requests", count)

    def get_pending_count(self) -> int:
        """获取待处理请求数。"""
        with self._lock:
            return len(self._pending_requests)


__all__ = [
    # 常量
    "DIRECTION_CLIENT_TO_SERVER", "DIRECTION_SERVER_TO_CLIENT",
    "NAMESPACE_MINECRAFT",
    "CHANNEL_AI_COMMAND", "CHANNEL_PRESET", "CHANNEL_VIP_EVENT_SYSTEM",
    "CHANNEL_ACHIEVEMENT", "CHANNEL_CHAT_PHRASES",
    "CHANNEL_CHAT_EXTENSION", "CHANNEL_PET",
    "RPC_GET_MCP_CHECK_NUM", "RPC_GET_MCP_CHECK_NUM_SECOND_ARG",
    "RPC_SET_MCP_CHECK_NUM", "RPC_SYNC_VIP_SKIN_UUID",
    "EVENT_AVAILABLE_CHECK_FAILED", "EVENT_EXECUTE_COMMAND_OUTPUT",
    "DEFAULT_RPC_TIMEOUT",
    # 枚举
    "PyRPCDirection", "PyRPCEventType",
    # 数据结构
    "PyRPCEvent", "PyRPCChannel", "PyRPCEventHandler",
    # 核心
    "PyRPC",
]
