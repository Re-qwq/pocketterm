"""游戏事件监听模块 - 类似 ToolDelta 的玩家发言监听等功能。

本模块提供独立的游戏事件监听系统, 在 Bedrock 协议层拦截并解析数据包,
将游戏内事件转换为结构化事件对象, 通过回调机制分发给注册的处理器。

功能:
    - 监听玩家聊天消息 (PlayerChat 事件)
    - 监听玩家加入/离开 (PlayerJoin / PlayerLeave 事件)
    - 监听玩家死亡 (PlayerDeath 事件)
    - 监听方块放置/破坏 (BlockPlace / BlockBreak 事件)
    - 事件回调注册与分发机制
    - 事件过滤器 (按玩家名、事件类型过滤)
    - 异步非阻塞处理, 不影响导入性能

使用方式::

    from app.protocol.connection import BedrockClient
    from app.protocol.game_events import GameEventListener

    client = BedrockClient(sauth_json="...", device_fingerprint={...})
    await client.connect("host", 19132)

    listener = GameEventListener(client)

    @listener.on("player_chat")
    async def on_chat(sender, message):
        print(f"{sender} 说: {message}")

    # 启动监听 (在后台 receive loop 中处理数据包)
    await listener.start()

    # 停止监听
    await listener.stop()

注意:
    - 导入命令不能在游戏中执行, 只能在控制台。本模块的聊天命令系统
      (chat_commands.py) 仅处理 !help, !status 等查询类命令。
    - 事件监听是异步的, 通过回调机制处理, 不阻塞数据包接收。
    - 所有回调函数在 asyncio 事件循环中执行, 支持协程。

逆向来源:
    - ToolDelta 插件框架 (事件监听机制)
    - neomega / NovaBuilder (Bedrock 协议逆向)
    - Minecraft Bedrock Protocol Wiki (数据包结构)
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional, TYPE_CHECKING

from .varint import decode_varuint32
from .connection import decode_string, PacketID

if TYPE_CHECKING:
    from .connection import BedrockClient

logger = logging.getLogger("pocketterm.game_events")

# ======================================================================
# 常量
# ======================================================================

#: 玩家列表操作类型 (PlayerList 包)
PLAYER_LIST_ADD = 0
PLAYER_LIST_REMOVE = 1

#: 事件包中常见的玩家死亡事件类型名
PLAYER_DEATH_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "PlayerDied",
        "EntityDied",
        "death",
        "player_died",
        "die",
    }
)

#: 事件包中常见的方块交互事件类型名
BLOCK_INTERACT_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "BlockPlacedByPlayer",
        "BlockDestroyedByPlayer",
        "block_place",
        "block_break",
        "BlockPlace",
        "BlockBreak",
    }
)


# ======================================================================
# 事件数据类
# ======================================================================


@dataclass
class GameEvent:
    """游戏事件基类。

    所有游戏事件都继承自此基类, 包含通用字段。

    Attributes:
        event_type: 事件类型字符串 (如 "player_chat", "player_join")
        timestamp: 事件发生时间戳 (Unix 时间)
        bot_id: 关联的机器人 ID
        data: 事件附加数据字典
    """

    event_type: str = "unknown"
    timestamp: float = field(default_factory=time.time)
    bot_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlayerChatEvent(GameEvent):
    """玩家聊天事件。

    Attributes:
        sender: 发送者玩家名
        message: 聊天消息内容
        text_type: 文本类型 (见 PacketID.TEXT_TYPE_* 常量)
        xuid: 发送者 XUID
        is_system: 是否为系统消息
    """

    sender: str = ""
    message: str = ""
    text_type: int = 0
    xuid: str = ""
    is_system: bool = False

    def __post_init__(self):
        self.event_type = "player_chat"


@dataclass
class PlayerJoinEvent(GameEvent):
    """玩家加入事件。

    Attributes:
        player_name: 加入的玩家名
        player_uuid: 玩家 UUID (字符串形式)
    """

    player_name: str = ""
    player_uuid: str = ""

    def __post_init__(self):
        self.event_type = "player_join"


@dataclass
class PlayerLeaveEvent(GameEvent):
    """玩家离开事件。

    Attributes:
        player_name: 离开的玩家名
        player_uuid: 玩家 UUID (字符串形式)
    """

    player_name: str = ""
    player_uuid: str = ""

    def __post_init__(self):
        self.event_type = "player_leave"


@dataclass
class PlayerDeathEvent(GameEvent):
    """玩家死亡事件。

    Attributes:
        player_name: 死亡的玩家名
        cause: 死亡原因 (如 "fell", "drowned", "entity_attack")
        killer: 击杀者 (如果有)
    """

    player_name: str = ""
    cause: str = ""
    killer: str = ""

    def __post_init__(self):
        self.event_type = "player_death"


@dataclass
class BlockPlaceEvent(GameEvent):
    """方块放置事件。

    Attributes:
        player_name: 放置方块的玩家名
        block_name: 方块名称 (如 "minecraft:stone")
        x, y, z: 方块坐标
    """

    player_name: str = ""
    block_name: str = ""
    x: int = 0
    y: int = 0
    z: int = 0

    def __post_init__(self):
        self.event_type = "block_place"


@dataclass
class BlockBreakEvent(GameEvent):
    """方块破坏事件。

    Attributes:
        player_name: 破坏方块的玩家名
        block_name: 方块名称 (如 "minecraft:stone")
        x, y, z: 方块坐标
    """

    player_name: str = ""
    block_name: str = ""
    x: int = 0
    y: int = 0
    z: int = 0

    def __post_init__(self):
        self.event_type = "block_break"


#: 事件类型到事件类的映射
EVENT_CLASS_MAP: dict[str, type[GameEvent]] = {
    "player_chat": PlayerChatEvent,
    "player_join": PlayerJoinEvent,
    "player_leave": PlayerLeaveEvent,
    "player_death": PlayerDeathEvent,
    "block_place": BlockPlaceEvent,
    "block_break": BlockBreakEvent,
}


# ======================================================================
# 事件过滤器
# ======================================================================


@dataclass
class EventFilter:
    """事件过滤器配置。

    支持按事件类型、玩家名、消息内容过滤事件。

    Attributes:
        enabled_event_types: 允许的事件类型集合 (空集 = 全部允许)
        blocked_players: 屏蔽的玩家名集合
        allowed_players: 仅允许的玩家名集合 (为空 = 不限制)
        keyword_filter: 聊天消息关键词过滤 (包含任一关键词则通过, 空 = 不过滤)
    """

    enabled_event_types: set[str] = field(default_factory=set)
    blocked_players: set[str] = field(default_factory=set)
    allowed_players: set[str] = field(default_factory=set)
    keyword_filter: set[str] = field(default_factory=set)

    def should_process(self, event: GameEvent) -> bool:
        """判断事件是否应该被处理。

        Args:
            event: 游戏事件对象。

        Returns:
            True 表示事件应该被处理, False 表示应被过滤。
        """
        # 检查事件类型
        if self.enabled_event_types and event.event_type not in self.enabled_event_types:
            return False

        # 检查玩家名 (从事件中提取)
        player_name = ""
        if isinstance(event, (PlayerChatEvent, PlayerJoinEvent, PlayerLeaveEvent)):
            player_name = getattr(event, "sender", "") or getattr(
                event, "player_name", ""
            )
        elif isinstance(event, (PlayerDeathEvent, BlockPlaceEvent, BlockBreakEvent)):
            player_name = getattr(event, "player_name", "")

        # 屏蔽列表
        if player_name and player_name in self.blocked_players:
            return False

        # 白名单
        if self.allowed_players and player_name:
            if player_name not in self.allowed_players:
                return False

        # 关键词过滤 (仅对聊天事件)
        if self.keyword_filter and isinstance(event, PlayerChatEvent):
            msg = event.message.lower()
            if not any(kw.lower() in msg for kw in self.keyword_filter):
                return False

        return True

    def to_dict(self) -> dict[str, Any]:
        """将过滤器序列化为字典 (用于 API 响应)。"""
        return {
            "enabled_event_types": list(self.enabled_event_types),
            "blocked_players": list(self.blocked_players),
            "allowed_players": list(self.allowed_players),
            "keyword_filter": list(self.keyword_filter),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EventFilter":
        """从字典创建过滤器 (用于 API 请求)。"""
        return cls(
            enabled_event_types=set(data.get("enabled_event_types", [])),
            blocked_players=set(data.get("blocked_players", [])),
            allowed_players=set(data.get("allowed_players", [])),
            keyword_filter=set(data.get("keyword_filter", [])),
        )


# ======================================================================
# 游戏事件监听器
# ======================================================================


class GameEventListener:
    """游戏事件监听器 — 类似 ToolDelta 的事件监听机制。

    在 Bedrock 协议层拦截数据包, 解析游戏事件并分发给注册的处理器。
    完全独立于主导入流程, 异步非阻塞。

    Args:
        client: 已连接的 BedrockClient 实例。
        bot_id: 关联的机器人 ID (用于事件溯源)。
        filter_config: 事件过滤器配置 (可选)。

    支持的事件类型:
        - ``player_chat``:   玩家聊天, 回调签名 ``(event: PlayerChatEvent)``
        - ``player_join``:   玩家加入, 回调签名 ``(event: PlayerJoinEvent)``
        - ``player_leave``:  玩家离开, 回调签名 ``(event: PlayerLeaveEvent)``
        - ``player_death``:  玩家死亡, 回调签名 ``(event: PlayerDeathEvent)``
        - ``block_place``:   方块放置, 回调签名 ``(event: BlockPlaceEvent)``
        - ``block_break``:   方块破坏, 回调签名 ``(event: BlockBreakEvent)``
        - ``*`` (通配符):    所有事件, 回调签名 ``(event: GameEvent)``
    """

    def __init__(
        self,
        client: "BedrockClient",
        bot_id: str = "",
        filter_config: Optional[EventFilter] = None,
    ) -> None:
        """初始化游戏事件监听器。

        Args:
            client: 已连接的 BedrockClient 实例。
            bot_id: 关联的机器人 ID。
            filter_config: 事件过滤器配置 (可选)。
        """
        self._client: "BedrockClient" = client
        self._bot_id: str = bot_id
        self._filter: EventFilter = filter_config or EventFilter()

        #: 事件处理器注册表 {event_type: [handler, ...]}
        self._handlers: dict[str, list[Callable]] = {}

        #: 是否正在运行
        self._running: bool = False

        #: 后台处理任务
        self._process_task: Optional[asyncio.Task] = None

        #: 事件队列 (用于缓冲和异步处理)
        self._event_queue: asyncio.Queue[GameEvent] = asyncio.Queue()

        #: 已知玩家集合 (用于检测玩家加入/离开)
        self._known_players: set[str] = set()

        #: 事件历史 (最近 N 条, 用于 WebSocket 新客户端同步)
        self._event_history: list[GameEvent] = []
        self._max_history: int = 200

        #: 事件计数器 (用于统计)
        self._event_counts: dict[str, int] = {}
        self._start_time: float = 0.0

    # ------------------------------------------------------------------
    # 事件注册
    # ------------------------------------------------------------------

    def on(self, event_type: str):
        """装饰器: 注册事件处理器。

        用法::

            listener = GameEventListener(client)

            @listener.on("player_chat")
            async def on_chat(event: PlayerChatEvent):
                print(f"{event.sender} 说: {event.message}")

            @listener.on("*")  # 通配符, 监听所有事件
            async def on_all(event: GameEvent):
                print(f"事件: {event.event_type}")

        Args:
            event_type: 事件类型 ("player_chat", "player_join", 等),
                或 "*" 表示监听所有事件。

        Returns:
            装饰器函数。
        """

        def decorator(handler: Callable) -> Callable:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)
            logger.debug(
                "注册事件处理器: event_type=%s, handler=%s",
                event_type,
                handler.__name__,
            )
            return handler

        return decorator

    def add_handler(self, event_type: str, handler: Callable) -> None:
        """注册事件处理器 (非装饰器方式)。

        Args:
            event_type: 事件类型 ("player_chat", "player_join", 等),
                或 "*" 表示监听所有事件。
            handler: 处理函数, 可以是普通函数或协程函数。
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        logger.debug(
            "注册事件处理器: event_type=%s, handler=%s",
            event_type,
            handler.__name__,
        )

    def remove_handler(self, event_type: str, handler: Callable) -> bool:
        """移除事件处理器。

        Args:
            event_type: 事件类型。
            handler: 要移除的处理函数。

        Returns:
            True 移除成功; False 未找到。
        """
        handlers = self._handlers.get(event_type, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    # ------------------------------------------------------------------
    # 过滤器管理
    # ------------------------------------------------------------------

    def set_filter(self, filter_config: EventFilter) -> None:
        """设置事件过滤器。

        Args:
            filter_config: 事件过滤器配置。
        """
        self._filter = filter_config
        logger.info("事件过滤器已更新: %s", filter_config.to_dict())

    def get_filter(self) -> EventFilter:
        """获取当前事件过滤器配置。"""
        return self._filter

    def update_filter(
        self,
        enabled_event_types: Optional[list[str]] = None,
        blocked_players: Optional[list[str]] = None,
        allowed_players: Optional[list[str]] = None,
        keyword_filter: Optional[list[str]] = None,
    ) -> None:
        """更新过滤器部分字段 (未指定的字段保持不变)。

        Args:
            enabled_event_types: 启用的事件类型列表。
            blocked_players: 屏蔽的玩家列表。
            allowed_players: 白名单玩家列表。
            keyword_filter: 聊天关键词过滤列表。
        """
        if enabled_event_types is not None:
            self._filter.enabled_event_types = set(enabled_event_types)
        if blocked_players is not None:
            self._filter.blocked_players = set(blocked_players)
        if allowed_players is not None:
            self._filter.allowed_players = set(allowed_players)
        if keyword_filter is not None:
            self._filter.keyword_filter = set(keyword_filter)
        logger.info("事件过滤器已更新: %s", self._filter.to_dict())

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动事件监听。

        启动后台事件处理任务, 开始接收并处理游戏事件。
        需要在 BedrockClient 连接成功后调用。
        """
        if self._running:
            logger.warning("游戏事件监听器已在运行中")
            return

        self._running = True
        self._start_time = time.time()
        self._process_task = asyncio.create_task(self._process_loop())
        logger.info(
            "游戏事件监听器已启动 (bot_id=%s), 过滤配置: %s",
            self._bot_id,
            self._filter.to_dict(),
        )

    async def stop(self) -> None:
        """停止事件监听。

        取消后台处理任务, 清理资源。
        """
        self._running = False
        if self._process_task and not self._process_task.done():
            self._process_task.cancel()
            try:
                await self._process_task
            except asyncio.CancelledError:
                pass
            self._process_task = None
        logger.info("游戏事件监听器已停止 (bot_id=%s)", self._bot_id)

    # ------------------------------------------------------------------
    # 数据包处理 (由外部调用)
    # ------------------------------------------------------------------

    async def process_packet(self, packet_id: int, data: bytes) -> None:
        """处理原始数据包, 解析游戏事件。

        此方法应在 BedrockClient 的后台接收循环中调用,
        对每个接收到的数据包进行事件解析。
        解析是同步的 (不阻塞), 事件对象放入队列后异步处理。

        Args:
            packet_id: 数据包 ID。
            data: 数据包载荷 (不含 packet_id)。
        """
        if not self._running:
            return

        try:
            event = self._parse_packet(packet_id, data)
            if event is not None:
                event.bot_id = self._bot_id
                # 放入队列进行异步处理, 不阻塞数据包接收
                await self._event_queue.put(event)
        except Exception as exc:
            logger.debug("解析数据包事件失败 (packet_id=0x%02X): %s", packet_id, exc)

    # ------------------------------------------------------------------
    # 查询与统计
    # ------------------------------------------------------------------

    def get_event_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取最近的事件历史。

        Args:
            limit: 返回最近 N 条事件。

        Returns:
            事件字典列表, 可序列化为 JSON。
        """
        return [
            self._event_to_dict(e) for e in self._event_history[-limit:]
        ]

    def get_event_counts(self) -> dict[str, int]:
        """获取各类事件的计数统计。"""
        return dict(self._event_counts)

    def get_known_players(self) -> list[str]:
        """获取当前已知的在线玩家列表。"""
        return sorted(self._known_players)

    def get_stats(self) -> dict[str, Any]:
        """获取监听器运行统计信息。"""
        uptime = time.time() - self._start_time if self._start_time > 0 else 0
        return {
            "running": self._running,
            "bot_id": self._bot_id,
            "uptime": uptime,
            "event_counts": self.get_event_counts(),
            "known_players": self.get_known_players(),
            "known_player_count": len(self._known_players),
            "filter": self._filter.to_dict(),
            "handler_count": sum(len(h) for h in self._handlers.values()),
            "queue_size": self._event_queue.qsize(),
        }

    # ==================================================================
    # 私有方法 — 事件处理循环
    # ==================================================================

    async def _process_loop(self) -> None:
        """后台事件处理循环。

        从事件队列中取出事件, 经过过滤器后分发给注册的处理器。
        处理是异步的, 不阻塞数据包接收。
        """
        logger.debug("游戏事件处理循环已启动")
        while self._running:
            try:
                # 非阻塞等待, 超时检查 _running 状态
                try:
                    event = await asyncio.wait_for(
                        self._event_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                # 应用过滤器
                if not self._filter.should_process(event):
                    continue

                # 更新统计
                self._event_counts[event.event_type] = (
                    self._event_counts.get(event.event_type, 0) + 1
                )

                # 更新已知玩家
                if isinstance(event, PlayerJoinEvent):
                    self._known_players.add(event.player_name)
                elif isinstance(event, PlayerLeaveEvent):
                    self._known_players.discard(event.player_name)

                # 保存事件历史
                self._event_history.append(event)
                if len(self._event_history) > self._max_history:
                    self._event_history = self._event_history[-self._max_history:]

                # 分发事件
                await self._dispatch_event(event)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("事件处理循环异常: %s", exc, exc_info=True)

        logger.debug("游戏事件处理循环已退出")

    async def _dispatch_event(self, event: GameEvent) -> None:
        """分发事件给所有注册的处理器。

        同时调用通配符处理器 ("*") 和特定类型处理器。

        Args:
            event: 游戏事件对象。
        """
        # 通配符处理器
        wildcard_handlers = self._handlers.get("*", [])
        # 特定类型处理器
        type_handlers = self._handlers.get(event.event_type, [])

        all_handlers = wildcard_handlers + type_handlers

        if not all_handlers:
            return

        for handler in all_handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as exc:
                logger.error(
                    "事件处理器错误 (event=%s, handler=%s): %s",
                    event.event_type,
                    handler.__name__,
                    exc,
                    exc_info=True,
                )

    # ==================================================================
    # 私有方法 — 数据包解析
    # ==================================================================

    def _parse_packet(self, packet_id: int, data: bytes) -> Optional[GameEvent]:
        """解析数据包为游戏事件。

        根据 packet_id 路由到对应的解析器。

        Args:
            packet_id: 数据包 ID。
            data: 数据包载荷。

        Returns:
            解析出的游戏事件对象, 如果无法解析或不感兴趣则返回 None。
        """
        if packet_id == PacketID.TEXT:
            return self._parse_text_packet(data)
        elif packet_id == PacketID.PLAYER_LIST:
            return self._parse_player_list_packet(data)
        elif packet_id == PacketID.SIMPLE_EVENT:
            return self._parse_event_packet(data, is_simple=True)
        elif packet_id == PacketID.EVENT:
            return self._parse_event_packet(data, is_simple=False)
        elif packet_id == PacketID.STANDARDIZE_EVENT:
            return self._parse_standardize_event_packet(data)
        return None

    def _parse_text_packet(self, data: bytes) -> Optional[PlayerChatEvent]:
        """解析 TEXT 数据包 (0x09) 为聊天事件。

        TEXT 包结构 (服务器 -> 客户端):
            [Byte: text_type]              # 0=raw, 1=chat, 2=translation, ...
            [Bool: needs_translation]
            [Varuint32: param_count]
            [String[]: parameters]
            [String: message]
            [String: xuid]
            [String: platform_chat_id]
            [String: source_name]         # 发送者名称 (仅 chat 类型)
            [String: message2]            # 消息内容 (仅 chat 类型)

        注意: 网易版可能使用不同的字段顺序, 此处采用启发式解析。

        Args:
            data: TEXT 包载荷。

        Returns:
            PlayerChatEvent 对象, 解析失败时返回 None。
        """
        try:
            offset = 0
            if not data:
                return None

            # [Byte: text_type]
            text_type = data[offset]
            offset += 1

            # [Bool: needs_translation]
            if offset >= len(data):
                return None
            needs_translation = data[offset] != 0
            offset += 1

            # [Varuint32: param_count]
            param_count, offset = _safe_decode_varuint32(data, offset)
            if param_count is None:
                return None

            # [String[]: parameters]
            parameters: list[str] = []
            for _ in range(min(param_count, 20)):  # 限制参数数量, 防止恶意数据
                param, offset = _safe_decode_string(data, offset)
                if param is None:
                    break
                parameters.append(param)

            # [String: message]
            message, offset = _safe_decode_string(data, offset)
            if message is None:
                return None

            # [String: xuid]
            xuid, offset = _safe_decode_string(data, offset)
            if xuid is None:
                xuid = ""

            # [String: platform_chat_id]
            platform_chat_id, offset = _safe_decode_string(data, offset)
            if platform_chat_id is None:
                platform_chat_id = ""

            # 尝试解析后续字段 (source_name 和 message2)
            source_name = ""
            actual_message = message

            if text_type == PacketID.TEXT_TYPE_CHAT:
                # 聊天类型通常有 source_name 和 message2 字段
                sn, new_offset = _safe_decode_string(data, offset)
                if sn is not None:
                    source_name = sn
                    offset = new_offset
                    msg2, new_offset2 = _safe_decode_string(data, offset)
                    if msg2 is not None:
                        actual_message = msg2
                        offset = new_offset2

            # 判断是否为系统消息
            is_system = text_type in (
                PacketID.TEXT_TYPE_SYSTEM,
                PacketID.TEXT_TYPE_POPUP,
                PacketID.TEXT_TYPE_TIP,
                PacketID.TEXT_TYPE_JUKEBOX_POPUP,
            )

            # 如果发送者名为空且是系统消息类型, 使用 "System"
            if not source_name and is_system:
                source_name = "System"

            # 过滤掉空消息
            if not actual_message and not message:
                return None

            # 过滤掉机器人自己发送的消息 (粗略判断: 消息不含有效内容)
            # 注意: 这里无法精确判断, 因为协议层不区分发送者

            logger.debug(
                "解析聊天事件: sender=%s, message=%s, text_type=%d",
                source_name,
                actual_message[:50],
                text_type,
            )

            return PlayerChatEvent(
                sender=source_name or "未知",
                message=actual_message or message,
                text_type=text_type,
                xuid=xuid,
                is_system=is_system,
            )

        except (ValueError, IndexError, struct.error) as exc:
            logger.debug("TEXT 包解析失败: %s", exc)
            return None

    def _parse_player_list_packet(
        self, data: bytes
    ) -> Optional[PlayerJoinEvent | PlayerLeaveEvent]:
        """解析 PlayerList 数据包 (0x0C) 为玩家加入/离开事件。

        PlayerList 包结构:
            [Byte: action_type]           # 0=add, 1=remove
            [Varuint32: entry_count]
            ... (每个 entry 的 UUID、名称等字段)

        注意: 此处仅解析玩家名称, 用于检测加入/离开。

        Args:
            data: PlayerList 包载荷。

        Returns:
            PlayerJoinEvent 或 PlayerLeaveEvent; 解析失败时返回 None。
        """
        try:
            offset = 0
            if not data:
                return None

            # [Byte: action_type]
            action_type = data[offset]
            offset += 1

            if action_type not in (PLAYER_LIST_ADD, PLAYER_LIST_REMOVE):
                return None

            # [Varuint32: entry_count]
            entry_count, offset = _safe_decode_varuint32(data, offset)
            if entry_count is None or entry_count == 0:
                return None

            # 对于 REMOVE 操作, 每个 entry 只有 UUID
            # 对于 ADD 操作, 每个 entry 包含 UUID + entity_unique_id + name + ...
            for _ in range(min(entry_count, 50)):  # 限制条目数
                # UUID 在 PlayerList 中通常是 16 字节的原始 UUID
                # 但不同版本可能不同, 我们先跳过 UUID 字段
                # 对于 ADD 操作, 尝试找到玩家名称
                if action_type == PLAYER_LIST_ADD and offset < len(data):
                    # 跳过 UUID (16 字节) + entity_unique_id (8 字节 varint64)
                    # 这里采用启发式: 尝试在数据中查找字符串
                    # 简化处理: 直接尝试从当前偏移量解析字符串
                    player_name, offset = _safe_decode_string(data, offset)
                    if player_name:
                        logger.debug(
                            "解析玩家加入: %s", player_name
                        )
                        return PlayerJoinEvent(
                            player_name=player_name,
                            player_uuid="",
                        )
                elif action_type == PLAYER_LIST_REMOVE and offset < len(data):
                    # REMOVE 操作: UUID + 可能的一些字段
                    # 尝试从数据中提取玩家名
                    player_name, offset = _safe_decode_string(data, offset)
                    if player_name:
                        logger.debug(
                            "解析玩家离开: %s", player_name
                        )
                        return PlayerLeaveEvent(
                            player_name=player_name,
                            player_uuid="",
                        )

            return None

        except (ValueError, IndexError, struct.error) as exc:
            logger.debug("PlayerList 包解析失败: %s", exc)
            return None

    def _parse_event_packet(
        self, data: bytes, is_simple: bool = False
    ) -> Optional[PlayerDeathEvent | BlockPlaceEvent | BlockBreakEvent]:
        """解析事件包 (0x0E SimpleEvent / 0x0F Event) 为游戏事件。

        SimpleEvent 包结构:
            [Int64LE: event_type]        # 事件类型枚举

        Event 包结构:
            [Varuint32: player_runtime_id]
            [Varuint32: event_type]
            [Byte: use_player_id]
            ... (事件数据)

        注意: 网易版的事件包结构可能与标准不同, 此处采用启发式解析。

        Args:
            data: 事件包载荷。
            is_simple: 是否为 SimpleEvent (0x0E)。

        Returns:
            游戏事件对象; 解析失败时返回 None。
        """
        try:
            if not data:
                return None

            # 先尝试提取玩家名称 (事件包中可能以字符串形式存在)
            # 启发式搜索: 在数据中查找可识别的玩家名或事件类型
            # 这种方式可以处理网易版的自定义事件格式

            # 尝试按字符串解析事件类型
            offset = 0
            event_type_str = ""

            if is_simple and len(data) >= 8:
                # SimpleEvent: 可能是 int64 事件类型
                event_id = struct.unpack_from("<q", data, 0)[0]
                # 对于 SimpleEvent, 尝试后续数据
                if len(data) > 8:
                    name, _ = _safe_decode_string(data, 8)
                    if name:
                        event_type_str = name

            if not event_type_str:
                # 尝试从数据中解析字符串
                name, _ = _safe_decode_string(data, 0)
                if name:
                    event_type_str = name

            if not event_type_str:
                return None

            event_lower = event_type_str.lower()

            # 检测死亡事件
            for death_name in PLAYER_DEATH_EVENT_NAMES:
                if death_name.lower() in event_lower:
                    # 尝试从事件数据中提取玩家名
                    player_name = self._extract_player_name_from_event(data)
                    cause = event_type_str
                    logger.debug("解析玩家死亡: player=%s, cause=%s", player_name, cause)
                    return PlayerDeathEvent(
                        player_name=player_name,
                        cause=cause,
                    )

            # 检测方块放置/破坏事件
            for block_name_pattern in BLOCK_INTERACT_EVENT_NAMES:
                if block_name_pattern.lower() in event_lower:
                    player_name = self._extract_player_name_from_event(data)
                    # 尝试提取坐标
                    x, y, z = self._extract_coordinates_from_event(data)

                    if "break" in event_lower or "destroy" in event_lower:
                        logger.debug(
                            "解析方块破坏: player=%s, pos=(%d, %d, %d)",
                            player_name, x, y, z,
                        )
                        return BlockBreakEvent(
                            player_name=player_name,
                            x=x, y=y, z=z,
                        )
                    else:
                        logger.debug(
                            "解析方块放置: player=%s, pos=(%d, %d, %d)",
                            player_name, x, y, z,
                        )
                        return BlockPlaceEvent(
                            player_name=player_name,
                            x=x, y=y, z=z,
                        )

            return None

        except (ValueError, IndexError, struct.error) as exc:
            logger.debug("事件包解析失败: %s", exc)
            return None

    def _parse_standardize_event_packet(
        self, data: bytes
    ) -> Optional[PlayerDeathEvent | BlockPlaceEvent | BlockBreakEvent]:
        """解析 StandardizeEvent 包 (0x11) 为游戏事件。

        StandardizeEvent 是网易版特有的标准化事件格式,
        事件数据以结构化方式编码。

        Args:
            data: StandardizeEvent 包载荷。

        Returns:
            游戏事件对象; 解析失败时返回 None。
        """
        # StandardizeEvent 与 Event 格式类似, 复用解析逻辑
        return self._parse_event_packet(data, is_simple=False)

    def _extract_player_name_from_event(self, data: bytes) -> str:
        """从事件数据中尝试提取玩家名称。

        启发式方法: 扫描数据中的可读字符串, 返回最可能的玩家名。

        Args:
            data: 事件数据。

        Returns:
            提取的玩家名, 失败时返回 "未知"。
        """
        # 在数据中查找可读字符串 (长度 2-16 的 ASCII/UTF-8 字符串)
        offset = 0
        while offset < len(data) - 2:
            name, new_offset = _safe_decode_string(data, offset)
            if name and 2 <= len(name) <= 16 and not name.startswith("minecraft:"):
                # 检查是否为有效的玩家名 (字母数字下划线)
                if all(c.isalnum() or c in "_-" for c in name):
                    return name
            offset += 1
        return "未知"

    def _extract_coordinates_from_event(self, data: bytes) -> tuple[int, int, int]:
        """从事件数据中尝试提取坐标。

        Args:
            data: 事件数据。

        Returns:
            (x, y, z) 坐标元组, 失败时返回 (0, 0, 0)。
        """
        # 尝试在数据中查找连续的 3 个 int32 值 (坐标)
        if len(data) >= 12:
            # 扫描可能的 int32 坐标
            for i in range(len(data) - 11):
                try:
                    x = struct.unpack_from("<i", data, i)[0]
                    y = struct.unpack_from("<i", data, i + 4)[0]
                    z = struct.unpack_from("<i", data, i + 8)[0]
                    # 合理的坐标范围 (Minecraft 世界)
                    if -30000000 <= x <= 30000000 and -64 <= y <= 320 and -30000000 <= z <= 30000000:
                        return x, y, z
                except struct.error:
                    break
        return 0, 0, 0

    # ==================================================================
    # 私有方法 — 序列化
    # ==================================================================

    def _event_to_dict(self, event: GameEvent) -> dict[str, Any]:
        """将事件对象序列化为字典 (用于 JSON 序列化)。

        Args:
            event: 游戏事件对象。

        Returns:
            可序列化的字典。
        """
        base = {
            "event_type": event.event_type,
            "timestamp": event.timestamp,
            "bot_id": event.bot_id,
        }

        if isinstance(event, PlayerChatEvent):
            base.update(
                {
                    "sender": event.sender,
                    "message": event.message,
                    "text_type": event.text_type,
                    "xuid": event.xuid,
                    "is_system": event.is_system,
                }
            )
        elif isinstance(event, PlayerJoinEvent):
            base.update(
                {
                    "player_name": event.player_name,
                    "player_uuid": event.player_uuid,
                }
            )
        elif isinstance(event, PlayerLeaveEvent):
            base.update(
                {
                    "player_name": event.player_name,
                    "player_uuid": event.player_uuid,
                }
            )
        elif isinstance(event, PlayerDeathEvent):
            base.update(
                {
                    "player_name": event.player_name,
                    "cause": event.cause,
                    "killer": event.killer,
                }
            )
        elif isinstance(event, BlockPlaceEvent):
            base.update(
                {
                    "player_name": event.player_name,
                    "block_name": event.block_name,
                    "x": event.x,
                    "y": event.y,
                    "z": event.z,
                }
            )
        elif isinstance(event, BlockBreakEvent):
            base.update(
                {
                    "player_name": event.player_name,
                    "block_name": event.block_name,
                    "x": event.x,
                    "y": event.y,
                    "z": event.z,
                }
            )

        # 附加数据
        if event.data:
            base["data"] = event.data

        return base


# ======================================================================
# 辅助函数
# ======================================================================


def _safe_decode_varuint32(
    data: bytes, offset: int
) -> tuple[Optional[int], int]:
    """安全解码 varuint32, 失败时返回 (None, offset)。

    Args:
        data: 字节数据。
        offset: 起始偏移量。

    Returns:
        (解码值, 新偏移量) 或 (None, 原偏移量)。
    """
    try:
        return decode_varuint32(data, offset)
    except (ValueError, IndexError):
        return None, offset


def _safe_decode_string(
    data: bytes, offset: int
) -> tuple[Optional[str], int]:
    """安全解码 Bedrock 字符串, 失败时返回 (None, offset)。

    Args:
        data: 字节数据。
        offset: 起始偏移量。

    Returns:
        (解码字符串, 新偏移量) 或 (None, 原偏移量)。
    """
    try:
        return decode_string(data, offset)
    except (ValueError, IndexError, UnicodeDecodeError):
        return None, offset


# ======================================================================
# 全局游戏事件总线
# ======================================================================

#: 全局游戏事件总线 (单例), 用于跨模块事件通信
#: 各个模块可以通过此总线注册和触发事件, 无需直接依赖 GameEventListener 实例
class GameEventBus:
    """全局游戏事件总线。

    提供跨模块的游戏事件发布/订阅机制。使用时通过全局单例
    ``game_event_bus`` 访问。

    示例::

        from app.protocol.game_events import game_event_bus

        @game_event_bus.on("player_chat")
        async def handle_chat(event):
            print(f"玩家 {event.sender} 说: {event.message}")

        # 发布事件
        await game_event_bus.emit(chat_event)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = {}
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def on(self, event_type: str):
        """装饰器: 注册全局事件处理器。

        Args:
            event_type: 事件类型。

        Returns:
            装饰器函数。
        """
        def decorator(handler: Callable) -> Callable:
            if event_type not in self._handlers:
                self._handlers[event_type] = []
            self._handlers[event_type].append(handler)
            return handler
        return decorator

    async def emit(self, event: GameEvent) -> None:
        """发布事件到所有注册的处理器。

        Args:
            event: 游戏事件对象。
        """
        handlers = (
            self._handlers.get("*", [])
            + self._handlers.get(event.event_type, [])
        )
        if not handlers:
            return

        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception as exc:
                logger.error(
                    "全局事件总线处理器错误 (event=%s): %s",
                    event.event_type,
                    exc,
                )

    def clear(self) -> None:
        """清除所有已注册的处理器。"""
        self._handlers.clear()


#: 全局游戏事件总线实例
game_event_bus = GameEventBus()


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    # 事件数据类
    "GameEvent",
    "PlayerChatEvent",
    "PlayerJoinEvent",
    "PlayerLeaveEvent",
    "PlayerDeathEvent",
    "BlockPlaceEvent",
    "BlockBreakEvent",
    "EVENT_CLASS_MAP",
    # 事件过滤器
    "EventFilter",
    # 事件监听器
    "GameEventListener",
    # 事件总线
    "GameEventBus",
    "game_event_bus",
    # 辅助函数
    "_safe_decode_varuint32",
    "_safe_decode_string",
]