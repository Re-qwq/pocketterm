"""游戏事件管理器 — 整合游戏事件监听、聊天命令和 WebSocket 推送。

本模块是游戏事件系统的顶层集成模块, 负责:

    1. 为每个机器人创建和管理 GameEventListener 实例
    2. 为每个机器人创建和管理 ChatCommandSystem 实例
    3. 将游戏事件通过 WebSocket 推送到前端
    4. 管理事件过滤器和配置
    5. 提供统一的 API 接口

使用方式::

    from app.protocol.event_manager import EventManager, event_manager

    # 为机器人创建事件监听
    await event_manager.create_listener(bot_id, client)

    # 启动事件监听
    await event_manager.start_listener(bot_id)

    # 获取事件历史
    history = event_manager.get_event_history(bot_id)

    # 停止事件监听
    await event_manager.stop_listener(bot_id)

注意:
    - 此模块依赖 game_events.py 和 chat_commands.py
    - WebSocket 推送通过 ws_events.py 实现
    - 事件监听不阻塞主导入流程
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, TYPE_CHECKING

from .game_events import (
    GameEventListener,
    EventFilter,
    GameEvent,
    PlayerChatEvent,
    PlayerJoinEvent,
    PlayerLeaveEvent,
    PlayerDeathEvent,
    BlockPlaceEvent,
    BlockBreakEvent,
)
from .chat_commands import ChatCommandSystem, CommandResult

if TYPE_CHECKING:
    from .connection import BedrockClient

logger = logging.getLogger("pocketterm.event_manager")

# ======================================================================
# 事件管理器
# ======================================================================


class EventManager:
    """游戏事件管理器 — 统一管理所有机器人的事件系统。

    为每个机器人维护独立的 GameEventListener 和 ChatCommandSystem,
    负责事件监听、命令处理和 WebSocket 推送的协调。

    单例模式, 通过 ``event_manager`` 全局实例访问。
    """

    _instance: Optional["EventManager"] = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs) -> "EventManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        if EventManager._initialized:
            return
        EventManager._initialized = True

        #: 每个机器人的监听器 {bot_id: GameEventListener}
        self._listeners: dict[str, GameEventListener] = {}

        #: 每个机器人的命令系统 {bot_id: ChatCommandSystem}
        self._command_systems: dict[str, ChatCommandSystem] = {}

        #: 每个机器人的 BedrockClient {bot_id: BedrockClient}
        self._clients: dict[str, "BedrockClient"] = {}

        #: 异步锁 - H-8 修复: 懒加载
        self._lock: Optional[asyncio.Lock] = None

        #: 是否已注册 WebSocket 推送回调
        self._ws_registered: bool = False

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------
    # 创建和管理
    # ------------------------------------------------------------------

    async def create_listener(
        self,
        bot_id: str,
        client: "BedrockClient",
        filter_config: Optional[EventFilter] = None,
        command_prefix: str = "!",
        admin_players: Optional[list[str]] = None,
    ) -> GameEventListener:
        """为机器人创建事件监听器。

        同时创建 GameEventListener 和 ChatCommandSystem,
        并注册事件处理器 (包括 WebSocket 推送和命令处理)。

        Args:
            bot_id: 机器人 ID。
            client: 已连接的 BedrockClient 实例。
            filter_config: 事件过滤器配置 (可选)。
            command_prefix: 命令前缀 (默认 "!")。
            admin_players: 管理员玩家列表 (可选)。

        Returns:
            创建的 GameEventListener 实例。

        Raises:
            ValueError: 机器人 ID 已存在监听器。
        """
        async with self._get_lock():
            if bot_id in self._listeners:
                raise ValueError(f"机器人 {bot_id} 已存在事件监听器")

            # 创建事件监听器
            listener = GameEventListener(
                client=client,
                bot_id=bot_id,
                filter_config=filter_config,
            )
            self._listeners[bot_id] = listener
            self._clients[bot_id] = client

            # 创建命令系统
            cmd_system = ChatCommandSystem(
                client=client,
                command_prefix=command_prefix,
                admin_players=admin_players,
                bot_id=bot_id,
            )
            self._command_systems[bot_id] = cmd_system

            # 注册内部事件处理器
            self._register_internal_handlers(bot_id, listener, cmd_system)

            logger.info(
                "为机器人 %s 创建事件监听器 (prefix=%s, admins=%s)",
                bot_id,
                command_prefix,
                admin_players,
            )
            return listener

    async def remove_listener(self, bot_id: str) -> bool:
        """移除机器人的事件监听器。

        Args:
            bot_id: 机器人 ID。

        Returns:
            True 移除成功; False 不存在。
        """
        async with self._get_lock():
            listener = self._listeners.pop(bot_id, None)
            cmd_system = self._command_systems.pop(bot_id, None)
            self._clients.pop(bot_id, None)

            if listener is not None:
                await listener.stop()
            if cmd_system is not None:
                await cmd_system.stop()

            logger.info("移除机器人 %s 的事件监听器", bot_id)
            return listener is not None

    async def start_listener(self, bot_id: str) -> bool:
        """启动机器人的事件监听器。

        Args:
            bot_id: 机器人 ID。

        Returns:
            True 启动成功; False 不存在。
        """
        listener = self._listeners.get(bot_id)
        cmd_system = self._command_systems.get(bot_id)

        if listener is None:
            logger.warning("机器人 %s 的事件监听器不存在", bot_id)
            return False

        await listener.start()
        if cmd_system:
            await cmd_system.start()

        logger.info("启动机器人 %s 的事件监听器", bot_id)
        return True

    async def stop_listener(self, bot_id: str) -> bool:
        """停止机器人的事件监听器。

        Args:
            bot_id: 机器人 ID。

        Returns:
            True 停止成功; False 不存在。
        """
        listener = self._listeners.get(bot_id)
        cmd_system = self._command_systems.get(bot_id)

        if listener is None:
            return False

        await listener.stop()
        if cmd_system:
            await cmd_system.stop()

        logger.info("停止机器人 %s 的事件监听器", bot_id)
        return True

    async def stop_all(self) -> None:
        """停止所有机器人的事件监听器。"""
        logger.info("停止所有事件监听器...")
        tasks = []
        for bot_id in list(self._listeners.keys()):
            tasks.append(self.stop_listener(bot_id))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("所有事件监听器已停止")

    # ------------------------------------------------------------------
    # 数据包处理钩子
    # ------------------------------------------------------------------

    async def process_packet(self, bot_id: str, packet_id: int, data: bytes) -> None:
        """处理数据包, 路由到对应机器人的事件监听器。

        此方法应在 BedrockClient 的后台接收循环中调用。
        解析是异步的, 不阻塞数据包接收。

        Args:
            bot_id: 机器人 ID。
            packet_id: 数据包 ID。
            data: 数据包载荷。
        """
        listener = self._listeners.get(bot_id)
        if listener is not None and listener._running:
            await listener.process_packet(packet_id, data)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get_listener(self, bot_id: str) -> Optional[GameEventListener]:
        """获取机器人的事件监听器。

        Args:
            bot_id: 机器人 ID。

        Returns:
            GameEventListener 实例; 不存在时返回 None。
        """
        return self._listeners.get(bot_id)

    def get_command_system(self, bot_id: str) -> Optional[ChatCommandSystem]:
        """获取机器人的命令系统。

        Args:
            bot_id: 机器人 ID。

        Returns:
            ChatCommandSystem 实例; 不存在时返回 None。
        """
        return self._command_systems.get(bot_id)

    def get_event_history(self, bot_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """获取机器人的事件历史。

        Args:
            bot_id: 机器人 ID。
            limit: 返回最近 N 条事件。

        Returns:
            事件字典列表; 不存在时返回空列表。
        """
        listener = self._listeners.get(bot_id)
        if listener is None:
            return []
        return listener.get_event_history(limit)

    def get_event_stats(self, bot_id: str) -> dict[str, Any]:
        """获取机器人的事件统计。

        Args:
            bot_id: 机器人 ID。

        Returns:
            统计信息字典。
        """
        listener = self._listeners.get(bot_id)
        if listener is None:
            return {"running": False, "listener_exists": False}
        return listener.get_stats()

    def get_command_stats(self, bot_id: str) -> dict[str, Any]:
        """获取机器人的命令统计。

        Args:
            bot_id: 机器人 ID。

        Returns:
            统计信息字典。
        """
        cmd_system = self._command_systems.get(bot_id)
        if cmd_system is None:
            return {"running": False, "command_system_exists": False}
        return cmd_system.get_stats()

    def get_all_stats(self) -> dict[str, Any]:
        """获取所有机器人的事件和命令统计汇总。"""
        listener_count = len(self._listeners)
        running_count = sum(
            1 for l in self._listeners.values() if l._running
        )

        total_events = 0
        total_commands = 0
        bot_stats: dict[str, dict[str, Any]] = {}

        for bot_id, listener in self._listeners.items():
            lstats = listener.get_stats()
            cmd_system = self._command_systems.get(bot_id)
            cstats = cmd_system.get_stats() if cmd_system else {}

            total_events += sum(lstats.get("event_counts", {}).values())
            total_commands += cstats.get("total_commands", 0)

            bot_stats[bot_id] = {
                "listener": lstats,
                "commands": cstats,
            }

        return {
            "total_listeners": listener_count,
            "running_listeners": running_count,
            "total_events_processed": total_events,
            "total_commands_executed": total_commands,
            "bots": bot_stats,
        }

    # ------------------------------------------------------------------
    # 过滤器管理
    # ------------------------------------------------------------------

    def set_filter(self, bot_id: str, filter_config: EventFilter) -> bool:
        """设置机器人的事件过滤器。

        Args:
            bot_id: 机器人 ID。
            filter_config: 事件过滤器配置。

        Returns:
            True 设置成功; False 不存在。
        """
        listener = self._listeners.get(bot_id)
        if listener is None:
            return False
        listener.set_filter(filter_config)
        return True

    def update_filter(
        self,
        bot_id: str,
        enabled_event_types: Optional[list[str]] = None,
        blocked_players: Optional[list[str]] = None,
        allowed_players: Optional[list[str]] = None,
        keyword_filter: Optional[list[str]] = None,
    ) -> bool:
        """更新机器人的事件过滤器部分字段。

        Args:
            bot_id: 机器人 ID。
            enabled_event_types: 启用的事件类型。
            blocked_players: 屏蔽的玩家列表。
            allowed_players: 白名单玩家列表。
            keyword_filter: 聊天关键词过滤。

        Returns:
            True 更新成功; False 不存在。
        """
        listener = self._listeners.get(bot_id)
        if listener is None:
            return False
        listener.update_filter(
            enabled_event_types=enabled_event_types,
            blocked_players=blocked_players,
            allowed_players=allowed_players,
            keyword_filter=keyword_filter,
        )
        return True

    def get_filter(self, bot_id: str) -> Optional[dict[str, Any]]:
        """获取机器人的事件过滤器配置。

        Args:
            bot_id: 机器人 ID。

        Returns:
            过滤器配置字典; 不存在时返回 None。
        """
        listener = self._listeners.get(bot_id)
        if listener is None:
            return None
        return listener.get_filter().to_dict()

    # ------------------------------------------------------------------
    # 命令系统管理
    # ------------------------------------------------------------------

    def add_admin(self, bot_id: str, player_name: str) -> bool:
        """为机器人的命令系统添加管理员。

        Args:
            bot_id: 机器人 ID。
            player_name: 玩家名。

        Returns:
            True 添加成功; False 命令系统不存在。
        """
        cmd_system = self._command_systems.get(bot_id)
        if cmd_system is None:
            return False
        cmd_system.add_admin(player_name)
        return True

    def remove_admin(self, bot_id: str, player_name: str) -> bool:
        """从机器人的命令系统中移除管理员。

        Args:
            bot_id: 机器人 ID。
            player_name: 玩家名。

        Returns:
            True 移除成功; False 命令系统不存在。
        """
        cmd_system = self._command_systems.get(bot_id)
        if cmd_system is None:
            return False
        cmd_system.remove_admin(player_name)
        return True

    # ==================================================================
    # 私有方法
    # ==================================================================

    def _register_internal_handlers(
        self,
        bot_id: str,
        listener: GameEventListener,
        cmd_system: ChatCommandSystem,
    ) -> None:
        """注册内部事件处理器。

        包括:
            - 聊天事件 -> 命令系统处理
            - 所有事件 -> WebSocket 推送
            - 加入/离开事件 -> 日志记录

        Args:
            bot_id: 机器人 ID。
            listener: 事件监听器。
            cmd_system: 命令系统。
        """

        # 聊天事件 -> 命令处理
        @listener.on("player_chat")
        async def _handle_chat_for_commands(event: PlayerChatEvent):
            """将聊天事件路由到命令系统。"""
            # 只处理非系统消息
            if event.is_system or not event.message:
                return
            result = await cmd_system.process_chat(
                event.sender, event.message
            )
            if result is not None:
                logger.debug(
                    "命令执行结果: %s%s (sender=%s, success=%s)",
                    cmd_system._prefix,
                    event.message.split()[0][1:] if event.message.startswith(cmd_system._prefix) else "",
                    event.sender,
                    result.success,
                )

        # 所有事件 -> WebSocket 推送
        @listener.on("*")
        async def _handle_all_for_ws(event: GameEvent):
            """将所有事件推送到 WebSocket。"""
            try:
                from ..api.ws_events import broadcast_game_event
                event_dict = listener._event_to_dict(event)
                await broadcast_game_event(bot_id, event_dict)
            except ImportError:
                pass  # WebSocket 模块未加载时跳过
            except Exception as exc:
                logger.debug("WebSocket 事件推送失败: %s", exc)

        # 玩家加入 -> 日志
        @listener.on("player_join")
        async def _handle_join(event: PlayerJoinEvent):
            logger.info(
                "[%s] 玩家加入: %s", bot_id, event.player_name
            )

        # 玩家离开 -> 日志
        @listener.on("player_leave")
        async def _handle_leave(event: PlayerLeaveEvent):
            logger.info(
                "[%s] 玩家离开: %s", bot_id, event.player_name
            )

        # 玩家死亡 -> 日志
        @listener.on("player_death")
        async def _handle_death(event: PlayerDeathEvent):
            logger.info(
                "[%s] 玩家死亡: %s (原因: %s)",
                bot_id,
                event.player_name,
                event.cause,
            )

        logger.debug(
            "已为机器人 %s 注册内部事件处理器", bot_id
        )


# ======================================================================
# 全局单例
# ======================================================================

#: 全局事件管理器实例
event_manager = EventManager()


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    "EventManager",
    "event_manager",
]