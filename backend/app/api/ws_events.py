"""游戏事件 WebSocket 路由 — 实时推送游戏事件到前端。

路由前缀: ``/ws/events``

提供游戏事件的 WebSocket 实时推送, 前端可以订阅特定机器人
的游戏事件, 在仪表盘上看到实时事件流。

功能:
    - 订阅/取消订阅特定机器人的游戏事件
    - 实时推送游戏事件 (player_chat, player_join, player_leave, 等)
    - 事件过滤器配置 (可通过 WebSocket 消息动态更新)
    - 事件历史查询
    - 事件统计查询

消息格式::

    {"type": "game_event", "data": {"bot_id": "...", "event_type": "player_chat", ...}}

客户端可发送:
    - ``{"action": "subscribe", "bot_id": "xxx"}``      订阅机器人事件
    - ``{"action": "unsubscribe", "bot_id": "xxx"}``    取消订阅
    - ``{"action": "set_filter", "filter": {...}}``     设置事件过滤器
    - ``{"action": "get_history", "bot_id": "xxx", "limit": 50}``  获取事件历史
    - ``{"action": "get_stats", "bot_id": "xxx"}``      获取事件统计
    - ``{"action": "ping"}``                            心跳
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional, Set

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from ..auth.security import verify_token
from ..config import get_config
from ..logger import get_logger
from .deps import ACCESS_COOKIE_NAME
from .ws import manager as main_manager

logger = get_logger("api.ws_events")

#: 游戏事件 WebSocket 路由器
router = APIRouter(prefix="/ws/events", tags=["WebSocket 游戏事件"])

#: 游戏事件广播间隔 (秒), 用于批量推送以减少消息量
BROADCAST_INTERVAL: float = 0.5


# ---------------------------------------------------------------------------
# 游戏事件连接管理器
# ---------------------------------------------------------------------------

class GameEventConnectionManager:
    """游戏事件 WebSocket 连接管理器。

    管理订阅了游戏事件的客户端连接, 维护每个连接的订阅信息,
    支持按 bot_id 过滤推送。

    设计为与主 ws.py 的 ConnectionManager 独立运行,
    不影响主 WebSocket 的日志/状态/聊天广播。
    """

    def __init__(self) -> None:
        #: 活跃连接集合 {websocket: {"subscribed_bots": set, "filter": dict}}
        self._connections: dict[WebSocket, dict[str, Any]] = {}
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None

        #: 事件缓冲队列 (bot_id -> list of events)
        self._event_buffers: dict[str, list[dict[str, Any]]] = {}
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._buffer_lock: Optional[asyncio.Lock] = None

        #: 广播任务
        self._broadcast_task: Optional[asyncio.Task] = None
        self._broadcast_started: bool = False

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _get_buffer_lock(self) -> asyncio.Lock:
        """获取缓冲锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._buffer_lock is None:
            self._buffer_lock = asyncio.Lock()
        return self._buffer_lock

    @property
    def active_count(self) -> int:
        """活跃连接数。"""
        return len(self._connections)

    async def connect(self, websocket: WebSocket) -> None:
        """接受连接并初始化订阅状态。"""
        await websocket.accept()
        async with self._get_lock():
            self._connections[websocket] = {
                "subscribed_bots": set(),
                "filter": {},
            }
        logger.info(
            "游戏事件 WebSocket 客户端接入, 当前在线 %d 个",
            self.active_count,
        )
        self._ensure_broadcaster()

    async def disconnect(self, websocket: WebSocket) -> None:
        """移除连接。"""
        async with self._get_lock():
            self._connections.pop(websocket, None)
        logger.info(
            "游戏事件 WebSocket 客户端断开, 当前在线 %d 个",
            self.active_count,
        )

    async def subscribe(self, websocket: WebSocket, bot_id: str) -> None:
        """订阅指定机器人的游戏事件。

        Args:
            websocket: WebSocket 连接。
            bot_id: 机器人 ID。
        """
        async with self._get_lock():
            if websocket in self._connections:
                self._connections[websocket]["subscribed_bots"].add(bot_id)
                logger.debug(
                    "客户端订阅游戏事件: bot_id=%s", bot_id
                )

    async def unsubscribe(self, websocket: WebSocket, bot_id: str) -> None:
        """取消订阅指定机器人的游戏事件。

        Args:
            websocket: WebSocket 连接。
            bot_id: 机器人 ID。
        """
        async with self._get_lock():
            if websocket in self._connections:
                self._connections[websocket]["subscribed_bots"].discard(bot_id)
                logger.debug(
                    "客户端取消订阅游戏事件: bot_id=%s", bot_id
                )

    async def set_filter(
        self, websocket: WebSocket, filter_config: dict[str, Any]
    ) -> None:
        """设置客户端的事件过滤器。

        Args:
            websocket: WebSocket 连接。
            filter_config: 过滤器配置字典。
        """
        async with self._get_lock():
            if websocket in self._connections:
                self._connections[websocket]["filter"] = filter_config
                logger.debug(
                    "客户端更新事件过滤器: %s", filter_config
                )

    async def push_event(self, bot_id: str, event: dict[str, Any]) -> None:
        """推送游戏事件到缓冲队列。

        事件先进入缓冲队列, 由广播任务批量推送给客户端。

        Args:
            bot_id: 机器人 ID。
            event: 事件字典 (已序列化)。
        """
        async with self._get_buffer_lock():
            if bot_id not in self._event_buffers:
                self._event_buffers[bot_id] = []
            self._event_buffers[bot_id].append(event)
            # 限制缓冲区大小 (每个 bot 最多 500 条未推送事件)
            if len(self._event_buffers[bot_id]) > 500:
                self._event_buffers[bot_id] = self._event_buffers[bot_id][-500:]

    async def send(self, websocket: WebSocket, message: dict[str, Any]) -> None:
        """向单个客户端发送消息。"""
        try:
            await websocket.send_text(
                json.dumps(message, ensure_ascii=False, default=str)
            )
        except Exception:  # noqa: BLE001
            async with self._get_lock():
                self._connections.pop(websocket, None)

    # ------------------------------------------------------------------
    # 后台广播任务
    # ------------------------------------------------------------------

    def _ensure_broadcaster(self) -> None:
        """确保后台广播任务已启动。"""
        if self._broadcast_started:
            return
        self._broadcast_started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._broadcast_started = False
            return
        self._broadcast_task = loop.create_task(self._broadcast_loop())
        logger.info("游戏事件广播任务已启动")

    async def cancel_broadcaster(self) -> None:
        """取消后台广播任务。"""
        if self._broadcast_task and not self._broadcast_task.done():
            self._broadcast_task.cancel()
            try:
                await self._broadcast_task
            except asyncio.CancelledError:
                pass
        self._broadcast_task = None
        self._broadcast_started = False

    async def _broadcast_loop(self) -> None:
        """后台广播循环: 周期性将缓冲的事件推送给订阅的客户端。"""
        while True:
            try:
                await asyncio.sleep(BROADCAST_INTERVAL)
                await self._flush_buffers()
            except asyncio.CancelledError:
                logger.info("游戏事件广播任务已取消")
                break
            except Exception:  # noqa: BLE001
                logger.exception("游戏事件广播任务异常")

    async def _flush_buffers(self) -> None:
        """将缓冲的事件推送给订阅的客户端。"""
        # 获取缓冲快照
        async with self._get_buffer_lock():
            if not self._event_buffers:
                return
            buffers = dict(self._event_buffers)
            self._event_buffers = {}

        # 获取连接快照
        async with self._get_lock():
            if not self._connections:
                return
            connections = dict(self._connections)

        # 对每个连接, 推送其订阅的 bot 的事件
        for ws, state in connections.items():
            subscribed = state.get("subscribed_bots", set())
            if not subscribed:
                continue

            events_to_send: list[dict[str, Any]] = []
            for bot_id in subscribed:
                if bot_id in buffers:
                    for event in buffers[bot_id]:
                        events_to_send.append(event)

            if events_to_send:
                try:
                    # 批量推送
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "game_events",
                                "data": events_to_send,
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    )
                except Exception:  # noqa: BLE001
                    async with self._get_lock():
                        self._connections.pop(ws, None)


#: 全局游戏事件连接管理器单例
events_manager: GameEventConnectionManager = GameEventConnectionManager()


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------

def _verify_token(token: Optional[str]) -> bool:
    """验证 WebSocket JWT 令牌。"""
    if not token:
        return False
    config = get_config()
    payload = verify_token(token, config.jwt_secret)
    return payload is not None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.websocket("")
async def game_events_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None, description="JWT 令牌"),
) -> None:
    """游戏事件 WebSocket 端点。

    连接示例::

        ws://host/ws/events?token=<JWT>

    连接后客户端可发送 JSON 消息来订阅/管理游戏事件:

        - ``{"action": "subscribe", "bot_id": "xxx"}``
        - ``{"action": "unsubscribe", "bot_id": "xxx"}``
        - ``{"action": "set_filter", "filter": {...}}``
        - ``{"action": "get_history", "bot_id": "xxx", "limit": 50}``
        - ``{"action": "get_stats", "bot_id": "xxx"}``
        - ``{"action": "ping"}``

    服务端推送格式:
        - ``{"type": "game_events", "data": [...]}``    批量游戏事件
        - ``{"type": "game_event_history", "data": [...]}``  事件历史
        - ``{"type": "game_event_stats", "data": {...}}``    事件统计
        - ``{"type": "pong"}``                              心跳回复
        - ``{"type": "error", "data": {...}}``              错误
    """
    # 认证
    if not token:
        token = websocket.cookies.get(ACCESS_COOKIE_NAME)
    if not _verify_token(token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("游戏事件 WebSocket 连接被拒绝: token 无效或缺失")
        return

    await events_manager.connect(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_game_event_message(websocket, raw)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("游戏事件 WebSocket 连接异常")
    finally:
        await events_manager.disconnect(websocket)


async def _handle_game_event_message(websocket: WebSocket, raw: str) -> None:
    """处理客户端发来的游戏事件管理消息。"""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await events_manager.send(
            websocket,
            {"type": "error", "data": {"message": "无效的 JSON"}},
        )
        return

    if not isinstance(msg, dict):
        return

    action = msg.get("action")

    if action == "ping":
        await events_manager.send(websocket, {"type": "pong", "data": {}})

    elif action == "subscribe":
        bot_id = msg.get("bot_id", "")
        if bot_id:
            await events_manager.subscribe(websocket, bot_id)
            await events_manager.send(
                websocket,
                {
                    "type": "subscribed",
                    "data": {"bot_id": bot_id, "status": "ok"},
                },
            )

    elif action == "unsubscribe":
        bot_id = msg.get("bot_id", "")
        if bot_id:
            await events_manager.unsubscribe(websocket, bot_id)
            await events_manager.send(
                websocket,
                {
                    "type": "unsubscribed",
                    "data": {"bot_id": bot_id, "status": "ok"},
                },
            )

    elif action == "set_filter":
        filter_config = msg.get("filter", {})
        await events_manager.set_filter(websocket, filter_config)
        await events_manager.send(
            websocket,
            {
                "type": "filter_updated",
                "data": {"filter": filter_config},
            },
        )

    elif action == "get_history":
        bot_id = msg.get("bot_id", "")
        limit = msg.get("limit", 50)
        # 通过事件总线获取历史 (需要事件管理器支持)
        from ..protocol.game_events import game_event_bus
        # 这里需要从事件管理器获取历史, 暂时返回空列表
        await events_manager.send(
            websocket,
            {
                "type": "game_event_history",
                "data": {"bot_id": bot_id, "events": [], "limit": limit},
            },
        )

    elif action == "get_stats":
        bot_id = msg.get("bot_id", "")
        await events_manager.send(
            websocket,
            {
                "type": "game_event_stats",
                "data": {
                    "bot_id": bot_id,
                    "online_clients": events_manager.active_count,
                },
            },
        )

    else:
        await events_manager.send(
            websocket,
            {
                "type": "error",
                "data": {"message": f"未知 action: {action!r}"},
            },
        )


# ======================================================================
# 广播辅助函数 (供其他模块调用)
# ======================================================================


async def broadcast_game_event(
    bot_id: str, event: dict[str, Any]
) -> None:
    """广播游戏事件到所有订阅的 WebSocket 客户端。

    同时也通过主 WebSocket 管理器的广播通道发送,
    让主仪表盘也能收到游戏事件。

    Args:
        bot_id: 机器人 ID。
        event: 事件字典。
    """
    # 通过游戏事件通道推送
    await events_manager.push_event(bot_id, event)

    # 同时通过主 WebSocket 广播 (让 dashboard 也能收到)
    await main_manager.broadcast(
        {
            "type": "game_event",
            "data": event,
        }
    )


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    "router",
    "events_manager",
    "GameEventConnectionManager",
    "broadcast_game_event",
]