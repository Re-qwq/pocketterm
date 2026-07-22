"""PocketTerm WebSocket 路由

路由前缀: ``/ws``

提供 WebSocket 端点 ``/ws``，通过 ``token`` 查询参数进行认证。

主要能力:

    - **认证**: 客户端连接时携带 ``token`` 查询参数（即登录后获得的 JWT），
      验证通过后接入广播池。
    - **广播日志**: 周期性采样机器人日志，增量推送给所有在线客户端。
    - **广播机器人状态**: 机器人状态变更时推送 ``bot_status`` 事件。
    - **广播聊天消息**: 机器人收到新聊天时推送 ``chat`` 事件。

消息统一使用如下 JSON 格式::

    {"type": "bot_status" | "logs" | "chat" | "pong" | "error", "data": ...}
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional, Set

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from ..auth.security import verify_token
from ..bot.manager import bot_manager
from ..config import get_config
from ..logger import get_logger
from ..security import verify_jwt_token as verify_v2_token
from .deps import ACCESS_COOKIE_NAME

logger = get_logger("api.ws")

router = APIRouter(prefix="/ws", tags=["WebSocket"])

#: 广播采样间隔（秒）
POLL_INTERVAL: float = 2.0


# ---------------------------------------------------------------------------
# 连接管理器
# ---------------------------------------------------------------------------
class ConnectionManager:
    """WebSocket 连接管理器。

    维护在线连接池，提供广播能力，并通过后台任务周期性采样机器人
    运行态（状态 / 日志 / 聊天），将增量推送给所有客户端。
    """

    def __init__(self) -> None:
        self._active: Set[WebSocket] = set()
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None
        self._poller_started: bool = False
        # bot_id -> {"status": str, "log_count": int, "chat_len": int}
        self._last_state: Dict[str, Dict[str, Any]] = {}
        # 后台采样任务引用，便于在应用关闭时取消
        self._poll_task: Optional[asyncio.Task] = None

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def connect(self, websocket: WebSocket) -> None:
        """接受连接并加入广播池。"""
        await websocket.accept()
        async with self._get_lock():
            self._active.add(websocket)
        logger.info(f"WebSocket 客户端接入，当前在线 {self.active_count} 个")
        self._ensure_poller()

    async def disconnect(self, websocket: WebSocket) -> None:
        """从广播池移除连接。"""
        async with self._get_lock():
            self._active.discard(websocket)
        logger.info(f"WebSocket 客户端断开，当前在线 {self.active_count} 个")

    async def broadcast(self, message: Dict[str, Any]) -> None:
        """向所有在线客户端广播一条消息。"""
        if not self._active:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        async with self._get_lock():
            targets = list(self._active)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._get_lock():
                for ws in dead:
                    self._active.discard(ws)

    async def send(self, websocket: WebSocket, message: Dict[str, Any]) -> None:
        """向单个客户端发送消息。"""
        await websocket.send_text(
            json.dumps(message, ensure_ascii=False, default=str)
        )

    # ------------------------------------------------------------------
    # 后台采样任务
    # ------------------------------------------------------------------
    def _ensure_poller(self) -> None:
        """确保后台采样任务已启动（仅启动一次）。"""
        if self._poller_started:
            return
        self._poller_started = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # 没有运行中的事件循环，稍后重试
            self._poller_started = False
            return
        self._poll_task = loop.create_task(self._poll_loop())
        logger.info("WebSocket 广播采样任务已启动")

    async def cancel_poller(self) -> None:
        """取消后台采样任务（应用关闭时调用）。"""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        self._poller_started = False

    async def _poll_loop(self) -> None:
        """周期性采样机器人状态 / 日志 / 聊天并广播增量。"""
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                await self._sample_and_broadcast()
            except asyncio.CancelledError:
                logger.info("WebSocket 广播采样任务已取消")
                break
            except Exception:  # noqa: BLE001
                logger.exception("WebSocket 采样任务异常")

    async def _sample_and_broadcast(self) -> None:
        """采集所有机器人增量并广播。无在线客户端时跳过。"""
        if not self._active:
            return

        # 清理已删除机器人的状态记录
        current_ids = {bot.bot_id for bot in bot_manager.bots}
        stale = [bid for bid in self._last_state if bid not in current_ids]
        for bid in stale:
            self._last_state.pop(bid, None)

        for bot in bot_manager.bots:
            info = bot.info.to_dict()
            bid = bot.bot_id
            prev = self._last_state.get(
                bid, {"status": None, "log_count": 0, "chat_len": 0}
            )

            # 1. 状态变更
            if prev["status"] != info["status"]:
                await self.broadcast({"type": "bot_status", "data": info})

            # 2. 新日志（增量）
            logs = bot.info.logs
            log_count = len(logs)
            last_sent = prev["log_count"]
            if log_count < last_sent:
                # 日志数组已被裁剪 (超过 1000 条后只保留最近 1000 条),
                # last_sent 指向的索引已失效, 重发整个 logs 数组以避免丢日志。
                await self.broadcast(
                    {
                        "type": "logs",
                        "data": {"bot_id": bid, "logs": logs},
                    }
                )
            elif log_count > last_sent:
                new_logs = logs[last_sent:]
                await self.broadcast(
                    {
                        "type": "logs",
                        "data": {"bot_id": bid, "logs": new_logs},
                    }
                )

            # 3. 新聊天（增量）
            chat = bot.get_chat_history(500)
            chat_len = len(chat)
            last_chat = prev["chat_len"]
            if chat_len < last_chat:
                # 聊天历史同样可能被裁剪, 重发整个数组。
                await self.broadcast(
                    {
                        "type": "chat",
                        "data": {"bot_id": bid, "messages": chat},
                    }
                )
            elif chat_len > last_chat:
                new_chat = chat[last_chat:]
                await self.broadcast(
                    {
                        "type": "chat",
                        "data": {"bot_id": bid, "messages": new_chat},
                    }
                )

            self._last_state[bid] = {
                "status": info["status"],
                "log_count": log_count,
                "chat_len": chat_len,
            }

    # ------------------------------------------------------------------
    # 主动广播接口（供其它模块调用）
    # ------------------------------------------------------------------
    async def broadcast_log(self, message: str, level: str = "info") -> None:
        """广播一条系统日志。"""
        await self.broadcast(
            {
                "type": "system_log",
                "data": {"message": message, "level": level},
            }
        )

    async def broadcast_bot_status(self, bot_id: str) -> None:
        """主动广播某个机器人的当前状态。"""
        bot = bot_manager.get_bot(bot_id)
        if bot is None:
            return
        await self.broadcast({"type": "bot_status", "data": bot.info.to_dict()})


#: 全局连接管理器单例
manager: ConnectionManager = ConnectionManager()


# ---------------------------------------------------------------------------
# 认证
# ---------------------------------------------------------------------------
def _verify_ws_token(token: Optional[str]) -> bool:
    """验证 WebSocket 连接携带的 JWT，合法返回 True。

    优先使用 v2 安全模块验证 (环境变量密钥), 回退到 v1 config 密钥。
    """
    if not token:
        return False
    # 1. 优先用 v2 安全模块 (环境变量 JWT 密钥)
    if verify_v2_token(token):
        return True
    # 2. 回退: v1 config 密钥 (兼容旧 token)
    config = get_config()
    payload = verify_token(token, config.jwt_secret)
    return payload is not None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.websocket("")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(None, description="JWT 令牌"),
) -> None:
    """WebSocket 主端点。

    连接示例::

        ws://host/ws?token=<JWT>

    认证失败会以 1008 状态码关闭连接。

    客户端可发送以下 JSON 消息:

        - ``{"action": "ping"}``        服务端回复 ``{"type": "pong"}``
        - ``{"action": "status"}``      回复当前机器人列表快照
        - 其它消息将被忽略并回复 ``{"type": "error", "data": ...}``
    """
    # 认证: 优先使用 query 参数中的 token，若缺失则尝试从 Cookie 中读取
    if not token:
        token = websocket.cookies.get(ACCESS_COOKIE_NAME)
    if not _verify_ws_token(token):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        logger.warning("WebSocket 连接被拒绝: token 无效或缺失")
        return

    await manager.connect(websocket)
    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_client_message(websocket, raw)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("WebSocket 连接异常")
    finally:
        await manager.disconnect(websocket)


async def _handle_client_message(websocket: WebSocket, raw: str) -> None:
    """处理客户端发来的消息。"""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await manager.send(
            websocket,
            {"type": "error", "data": {"message": "无效的 JSON"}},
        )
        return

    action = msg.get("action") if isinstance(msg, dict) else None
    if action == "ping":
        await manager.send(websocket, {"type": "pong", "data": {}})
    elif action == "status":
        await manager.send(
            websocket,
            {
                "type": "status",
                "data": {
                    "bots": bot_manager.list_bots(),
                    "counts": bot_manager.get_status_counts(),
                    "online_clients": manager.active_count,
                },
            },
        )
    else:
        await manager.send(
            websocket,
            {"type": "error", "data": {"message": f"未知 action: {action!r}"}},
        )


__all__ = ["router", "manager", "ConnectionManager"]
