"""packet_dispatcher - 数据包分发器模块。

逆向自 NovaBuilder 的 StarShuttler PacketDispatcher, 来源:
    - /workspace/novuilder_reverse/REPORT.txt (第 405-438 行)
    - /workspace/novuilder_reverse/packet_full.txt

数据包分发器负责接收游戏服务器的数据包, 解析并分发到对应的处理器。

数据包类型 (逆向自 REPORT.txt 第 408-429 行):
    AvailableCommands     -- 可用命令
    CommandOutput          -- 命令输出
    CommandRequest         -- 命令请求
    ContainerOpen          -- 容器打开
    ContainerClose         -- 容器关闭
    InventoryContent       -- 物品栏内容
    InventorySlot          -- 物品栏槽位
    InventoryTransaction   -- 物品栏交易
    ItemStackRequest       -- 物品堆请求
    ItemStackResponse      -- 物品堆响应
    NeteaseJson            -- 网易 JSON 数据包
    neteaseCompression     -- 网易压缩
    SubChunk               -- 子区块
    StructureTemplateDataRequest   -- 结构模板数据请求
    StructureTemplateDataResponse  -- 结构模板数据响应
    CodeBuilder            -- 代码构建器
    CodeBuilderSource      -- 代码构建器源码
    SetPlayerInventoryOptions -- 设置玩家物品栏选项
    PlayerToggleCrafterSlotRequest -- 玩家切换合成器槽位请求
    UpdateBlock            -- 更新方块
    ClientBoundDebugRenderer -- 客户端调试渲染器

处理函数 (逆向自 REPORT.txt 第 431-438 行):
    handleCommandOutput()       -- 处理命令输出
    handleContainerOpen()       -- 处理容器打开
    handleContainerClose()      -- 处理容器关闭
    handleInventoryContent()    -- 处理物品栏内容
    handleInventorySlot()       -- 处理物品栏槽位
    handleItemStackResponse()   -- 处理物品堆响应
    onAvailableCommands()       -- 处理可用命令
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.starshuttler.packet_dispatcher")


# -------------------------------------------------------------------- #
# 数据包 ID 常量 (逆向自 protocol/packet)
# -------------------------------------------------------------------- #


class PacketID(IntEnum):
    """数据包 ID 枚举 (逆向自 minecraft/protocol/packet)。

    逆向自 REPORT.txt 第 408-429 行的数据包列表。
    """

    LOGIN = 0x01
    PLAY_STATUS = 0x02
    SERVER_TO_CLIENT_HANDSHAKE = 0x03
    CLIENT_TO_SERVER_HANDSHAKE = 0x04
    DISCONNECT = 0x05
    RESOURCE_PACKS_INFO = 0x06
    RESOURCE_PACK_STACK = 0x07
    RESOURCE_PACK_CLIENT_RESPONSE = 0x08
    TEXT = 0x09
    SET_TIME = 0x0A
    START_GAME = 0x0B
    ADD_PLAYER = 0x0C
    ADD_ENTITY = 0x0D
    REMOVE_ENTITY = 0x0E
    ADD_ITEM_ENTITY = 0x0F

    # 生物和交互
    INTERACT = 0x21
    ACTION = 0x22
    HURT_ARMOR = 0x23

    # 命令
    COMMAND_REQUEST = 0x4D
    COMMAND_OUTPUT = 0x4E
    AVAILABLE_COMMANDS = 0x4F
    COMMAND_BLOCK_UPDATE = 0x50

    # 容器和物品
    CONTAINER_OPEN = 0x2E
    CONTAINER_CLOSE = 0x2F
    INVENTORY_CONTENT = 0x31
    INVENTORY_SLOT = 0x32
    INVENTORY_TRANSACTION = 0x33
    ITEM_STACK_REQUEST = 0x35
    ITEM_STACK_RESPONSE = 0x36

    # 方块
    UPDATE_BLOCK = 0x15
    LEVEL_CHUNK = 0x16
    SUB_CHUNK = 0x17
    LEVEL_EVENT = 0x18

    # 结构
    STRUCTURE_TEMPLATE_DATA_REQUEST = 0x52
    STRUCTURE_TEMPLATE_DATA_RESPONSE = 0x53

    # 玩家
    SET_PLAYER_INVENTORY_OPTIONS = 0x54
    PLAYER_TOGGLE_CRAFTER_SLOT_REQUEST = 0x55
    PLAYER_LIST = 0x56

    # 网易特化 (逆向自 REPORT.txt 第 518-534 行)
    NETEASE_JSON = 0x90
    NETEASE_COMPRESSION = 0x91

    # CodeBuilder
    CODE_BUILDER = 0x58
    CODE_BUILDER_SOURCE = 0x59

    # 调试
    CLIENT_BOUND_DEBUG_RENDERER = 0x5A

    # MCPC 挑战 (逆向自 REPORT.txt 第 488-516 行)
    MODAL_FORM_REQUEST = 0x64
    MODAL_FORM_RESPONSE = 0x65

    # 认证 (逆向自 REPORT.txt 第 440-482 行)
    AUTH_INPUT = 0x66
    AUTH_RESPONSE = 0x67

    UNKNOWN = 0xFF


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class Packet:
    """数据包基类。"""

    id: int = int(PacketID.UNKNOWN)
    data: bytes = b""
    timestamp: float = field(default_factory=time.time)
    parsed: Any = None

    def __repr__(self) -> str:
        try:
            name = PacketID(self.id).name
        except ValueError:
            name = f"UNKNOWN_0x{self.id:02X}"
        return f"Packet(id={name}, size={len(self.data)})"


@dataclass
class PacketStats:
    """数据包统计。"""

    total_received: int = 0
    total_dispatched: int = 0
    total_errors: int = 0
    by_packet_type: dict[int, int] = field(default_factory=dict)
    last_packet_time: float = 0.0

    def record(self, packet_id: int) -> None:
        """记录接收的数据包。"""
        self.total_received += 1
        self.by_packet_type[packet_id] = self.by_packet_type.get(packet_id, 0) + 1
        self.last_packet_time = time.time()

    def reset(self) -> None:
        """重置统计。"""
        self.total_received = 0
        self.total_dispatched = 0
        self.total_errors = 0
        self.by_packet_type.clear()
        self.last_packet_time = 0.0


# -------------------------------------------------------------------- #
# 数据包处理器基类
# -------------------------------------------------------------------- #


class PacketHandler:
    """数据包处理器基类。

    所有数据包处理器都应继承此类并实现 :meth:`handle` 方法。
    """

    #: 处理器关注的数据包 ID 列表
    HANDLED_PACKET_IDS: list[int] = []

    def handle(self, packet: Packet) -> None:
        """处理数据包。

        Args:
            packet: 接收到的数据包。
        """
        raise NotImplementedError

    def can_handle(self, packet_id: int) -> bool:
        """检查是否能处理指定数据包 ID。

        Args:
            packet_id: 数据包 ID。

        Returns:
            True 如果可以处理。
        """
        return packet_id in self.HANDLED_PACKET_IDS


# -------------------------------------------------------------------- #
# 数据包分发器
# -------------------------------------------------------------------- #


class PacketDispatcher:
    """数据包分发器。

    逆向自 StarShuttler 的数据包分发系统。

    功能:
        1. 接收原始数据包
        2. 解析数据包 ID
        3. 分发到注册的处理器
        4. 统计数据包流量
        5. 支持通用监听器

    处理流程 (逆向自 REPORT.txt 第 431-438 行):
        handleCommandOutput()       -- CommandOutput
        handleContainerOpen()       -- ContainerOpen
        handleContainerClose()      -- ContainerClose
        handleInventoryContent()    -- InventoryContent
        handleInventorySlot()       -- InventorySlot
        handleItemStackResponse()   -- ItemStackResponse
        onAvailableCommands()       -- AvailableCommands

    使用示例::

        dispatcher = PacketDispatcher()
        dispatcher.register_handler(PacketID.COMMAND_OUTPUT, my_handler)
        dispatcher.dispatch(packet)
    """

    def __init__(self) -> None:
        """初始化数据包分发器。"""
        self._handlers: dict[int, list[Callable[[Packet], None]]] = {}
        self._packet_handlers: list[PacketHandler] = []
        self._global_listeners: list[Callable[[Packet], None]] = []
        self._stats: PacketStats = PacketStats()
        self._lock: threading.RLock = threading.RLock()
        self._running: bool = False
        self._dispatch_thread: threading.Thread | None = None
        self._packet_queue: list[Packet] = []
        self._queue_event: threading.Event = threading.Event()

        logger.debug("PacketDispatcher initialized")

    @property
    def stats(self) -> PacketStats:
        """数据包统计。"""
        return self._stats

    @property
    def is_running(self) -> bool:
        """是否正在运行。"""
        return self._running

    # ---------------------------------------------------------------- #
    # 处理器注册
    # ---------------------------------------------------------------- #

    def register_handler(
        self,
        packet_id: int | PacketID,
        handler: Callable[[Packet], None],
    ) -> None:
        """注册数据包处理器。

        Args:
            packet_id: 数据包 ID。
            handler: 处理函数。
        """
        pid = int(packet_id)
        with self._lock:
            if pid not in self._handlers:
                self._handlers[pid] = []
            self._handlers[pid].append(handler)

        try:
            name = PacketID(pid).name
        except ValueError:
            name = f"0x{pid:02X}"
        logger.debug("Registered handler for %s", name)

    def unregister_handler(
        self,
        packet_id: int | PacketID,
        handler: Callable[[Packet], None],
    ) -> None:
        """取消注册数据包处理器。

        Args:
            packet_id: 数据包 ID。
            handler: 处理函数。
        """
        pid = int(packet_id)
        with self._lock:
            if pid in self._handlers:
                try:
                    self._handlers[pid].remove(handler)
                    if not self._handlers[pid]:
                        del self._handlers[pid]
                except ValueError:
                    pass

    def register_packet_handler(self, handler: PacketHandler) -> None:
        """注册数据包处理器对象。

        Args:
            handler: :class:`PacketHandler` 实例。
        """
        with self._lock:
            self._packet_handlers.append(handler)
        logger.debug(
            "Registered packet handler: %s (handles %d IDs)",
            type(handler).__name__, len(handler.HANDLED_PACKET_IDS),
        )

    def add_global_listener(self, listener: Callable[[Packet], None]) -> None:
        """添加全局监听器 (接收所有数据包)。

        Args:
            listener: 监听函数。
        """
        with self._lock:
            self._global_listeners.append(listener)
        logger.debug("Added global listener: %s", type(listener).__name__)

    def clear_handlers(self) -> None:
        """清除所有处理器。"""
        with self._lock:
            count = sum(len(h) for h in self._handlers.values())
            count += len(self._packet_handlers)
            count += len(self._global_listeners)
            self._handlers.clear()
            self._packet_handlers.clear()
            self._global_listeners.clear()
        logger.info("Cleared %d handlers", count)

    # ---------------------------------------------------------------- #
    # 数据包分发
    # ---------------------------------------------------------------- #

    def dispatch(self, packet: Packet) -> None:
        """分发数据包 (同步)。

        将数据包发送到所有注册的处理器。

        Args:
            packet: 要分发的数据包。
        """
        self._stats.record(packet.id)

        # 全局监听器
        with self._lock:
            global_listeners = list(self._global_listeners)

        for listener in global_listeners:
            try:
                listener(packet)
            except Exception:
                logger.exception("Global listener error for packet id=0x%02X", packet.id)

        # 注册的处理器
        with self._lock:
            handlers = list(self._handlers.get(packet.id, []))

        for handler in handlers:
            try:
                handler(packet)
                self._stats.total_dispatched += 1
            except Exception:
                self._stats.total_errors += 1
                logger.exception("Handler error for packet id=0x%02X", packet.id)

        # PacketHandler 对象
        with self._lock:
            packet_handlers = list(self._packet_handlers)

        for ph in packet_handlers:
            if ph.can_handle(packet.id):
                try:
                    ph.handle(packet)
                    self._stats.total_dispatched += 1
                except Exception:
                    self._stats.total_errors += 1
                    logger.exception(
                        "PacketHandler %s error for packet id=0x%02X",
                        type(ph).__name__, packet.id,
                    )

    def dispatch_raw(self, data: bytes) -> None:
        """分发原始数据。

        解析数据包 ID 并分发。

        Args:
            data: 原始数据包数据。
        """
        if not data:
            return

        try:
            packet_id, offset = _decode_varint(data)
            packet = Packet(
                id=packet_id,
                data=data[offset:],
                timestamp=time.time(),
            )
            self.dispatch(packet)
        except Exception:
            self._stats.total_errors += 1
            logger.exception("Failed to dispatch raw packet")

    # ---------------------------------------------------------------- #
    # 异步分发
    # ---------------------------------------------------------------- #

    def start_async(self) -> None:
        """启动异步分发线程。"""
        if self._running:
            logger.warning("Dispatcher already running")
            return

        self._running = True
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="NovaBuilder-PacketDispatcher",
        )
        self._dispatch_thread.start()
        logger.info("PacketDispatcher started (async mode)")

    def stop_async(self) -> None:
        """停止异步分发。"""
        self._running = False
        self._queue_event.set()
        if self._dispatch_thread and self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout=5.0)
        logger.info("PacketDispatcher stopped")

    def enqueue(self, packet: Packet) -> None:
        """将数据包加入异步队列。

        Args:
            packet: 要入队的数据包。
        """
        with self._lock:
            if len(self._packet_queue) >= MAX_QUEUE_LENGTH:
                logger.warning("Packet queue full, dropping oldest packet")
                self._packet_queue.pop(0)
            self._packet_queue.append(packet)
        self._queue_event.set()

    def enqueue_raw(self, data: bytes) -> None:
        """将原始数据包加入异步队列。

        Args:
            data: 原始数据包数据。
        """
        if not data:
            return

        try:
            packet_id, offset = _decode_varint(data)
            packet = Packet(
                id=packet_id,
                data=data[offset:],
                timestamp=time.time(),
            )
            self.enqueue(packet)
        except Exception:
            self._stats.total_errors += 1
            logger.exception("Failed to enqueue raw packet")

    def _dispatch_loop(self) -> None:
        """异步分发循环。"""
        while self._running:
            self._queue_event.wait(timeout=1.0)
            self._queue_event.clear()

            while self._running:
                with self._lock:
                    if not self._packet_queue:
                        break
                    packet = self._packet_queue.pop(0)

                try:
                    self.dispatch(packet)
                except Exception:
                    logger.exception("Async dispatch error")

    # ---------------------------------------------------------------- #
    # 便捷注册
    # ---------------------------------------------------------------- #

    def on_command_output(self, handler: Callable[[Packet], None]) -> None:
        """注册 CommandOutput 处理器 (逆向自 handleCommandOutput)。"""
        self.register_handler(PacketID.COMMAND_OUTPUT, handler)

    def on_container_open(self, handler: Callable[[Packet], None]) -> None:
        """注册 ContainerOpen 处理器 (逆向自 handleContainerOpen)。"""
        self.register_handler(PacketID.CONTAINER_OPEN, handler)

    def on_container_close(self, handler: Callable[[Packet], None]) -> None:
        """注册 ContainerClose 处理器 (逆向自 handleContainerClose)。"""
        self.register_handler(PacketID.CONTAINER_CLOSE, handler)

    def on_inventory_content(self, handler: Callable[[Packet], None]) -> None:
        """注册 InventoryContent 处理器 (逆向自 handleInventoryContent)。"""
        self.register_handler(PacketID.INVENTORY_CONTENT, handler)

    def on_inventory_slot(self, handler: Callable[[Packet], None]) -> None:
        """注册 InventorySlot 处理器 (逆向自 handleInventorySlot)。"""
        self.register_handler(PacketID.INVENTORY_SLOT, handler)

    def on_item_stack_response(self, handler: Callable[[Packet], None]) -> None:
        """注册 ItemStackResponse 处理器 (逆向自 handleItemStackResponse)。"""
        self.register_handler(PacketID.ITEM_STACK_RESPONSE, handler)

    def on_available_commands(self, handler: Callable[[Packet], None]) -> None:
        """注册 AvailableCommands 处理器 (逆向自 onAvailableCommands)。"""
        self.register_handler(PacketID.AVAILABLE_COMMANDS, handler)

    def on_text(self, handler: Callable[[Packet], None]) -> None:
        """注册 Text 处理器。"""
        self.register_handler(PacketID.TEXT, handler)

    def on_modal_form_request(self, handler: Callable[[Packet], None]) -> None:
        """注册 ModalFormRequest 处理器 (用于 MCPC 挑战)。"""
        self.register_handler(PacketID.MODAL_FORM_REQUEST, handler)

    def on_disconnect(self, handler: Callable[[Packet], None]) -> None:
        """注册 Disconnect 处理器。"""
        self.register_handler(PacketID.DISCONNECT, handler)


# -------------------------------------------------------------------- #
# 工具函数
# -------------------------------------------------------------------- #


#: 最大队列长度
MAX_QUEUE_LENGTH: int = 10000


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


def get_packet_name(packet_id: int) -> str:
    """获取数据包名称。

    Args:
        packet_id: 数据包 ID。

    Returns:
        数据包名称。
    """
    try:
        return PacketID(packet_id).name
    except ValueError:
        return f"UNKNOWN_0x{packet_id:02X}"


__all__ = [
    # 常量
    "MAX_QUEUE_LENGTH",
    # 枚举
    "PacketID",
    # 数据结构
    "Packet", "PacketStats",
    # 处理器
    "PacketHandler",
    # 分发器
    "PacketDispatcher",
    # 工具函数
    "_decode_varint", "_encode_varint", "get_packet_name",
]
