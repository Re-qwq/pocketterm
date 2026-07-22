"""游戏事件管理 REST API — 过滤器配置、事件历史、事件统计。

路由前缀: ``/api/events``

提供游戏事件系统的管理接口, 用户可以通过 HTTP API:
    - 配置和管理事件过滤器
    - 查询事件历史记录
    - 获取事件统计信息
    - 管理命令系统

所有接口需要认证 (Bearer Token)。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel
from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth.security import verify_token
from ..protocol.event_manager import event_manager
from ..protocol.game_events import EventFilter
from ..logger import get_logger
from .deps import get_current_user

logger = get_logger("api.events")

router = APIRouter(prefix="/api/events", tags=["游戏事件"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class FilterUpdateRequest(BaseModel):
    """过滤器更新请求模型。"""
    enabled_event_types: Optional[list[str]] = None
    blocked_players: Optional[list[str]] = None
    allowed_players: Optional[list[str]] = None
    keyword_filter: Optional[list[str]] = None


class FilterSetRequest(BaseModel):
    """过滤器设置请求模型 (完整替换)。"""
    enabled_event_types: list[str] = []
    blocked_players: list[str] = []
    allowed_players: list[str] = []
    keyword_filter: list[str] = []


# ---------------------------------------------------------------------------
# 事件过滤器管理
# ---------------------------------------------------------------------------

@router.get("/{bot_id}/filter")
async def get_filter(
    bot_id: str,
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取指定机器人的事件过滤器配置。

    Args:
        bot_id: 机器人 ID。

    Returns:
        过滤器配置。
    """
    filter_config = event_manager.get_filter(bot_id)
    if filter_config is None:
        raise HTTPException(
            status_code=404,
            detail=f"机器人 {bot_id} 的事件监听器不存在",
        )
    return {
        "success": True,
        "data": {
            "bot_id": bot_id,
            "filter": filter_config,
        },
    }


@router.put("/{bot_id}/filter")
async def update_filter(
    bot_id: str,
    body: FilterUpdateRequest = Body(...),
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """更新指定机器人的事件过滤器。

    所有字段均为可选, 只更新传入的字段。

    请求体示例::

        {
            "enabled_event_types": ["player_chat", "player_join"],
            "blocked_players": ["Player1"]
        }

    Args:
        bot_id: 机器人 ID。
        body: 过滤器更新请求。

    Returns:
        更新后的过滤器配置。
    """
    success = event_manager.update_filter(
        bot_id,
        enabled_event_types=body.enabled_event_types,
        blocked_players=body.blocked_players,
        allowed_players=body.allowed_players,
        keyword_filter=body.keyword_filter,
    )
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"机器人 {bot_id} 的事件监听器不存在",
        )

    filter_config = event_manager.get_filter(bot_id)
    return {
        "success": True,
        "message": "过滤器已更新",
        "data": {
            "bot_id": bot_id,
            "filter": filter_config,
        },
    }


@router.post("/{bot_id}/filter")
async def set_filter(
    bot_id: str,
    body: FilterSetRequest = Body(...),
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """设置指定机器人的事件过滤器 (完整替换)。

    请求体格式::

        {
            "enabled_event_types": ["player_chat", "player_join"],
            "blocked_players": ["Player1"],
            "allowed_players": [],
            "keyword_filter": ["help"]
        }

    Args:
        bot_id: 机器人 ID。
        body: 过滤器配置。

    Returns:
        设置后的过滤器配置。
    """
    try:
        filter_config = EventFilter(
            enabled_event_types=set(body.enabled_event_types),
            blocked_players=set(body.blocked_players),
            allowed_players=set(body.allowed_players),
            keyword_filter=set(body.keyword_filter),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"过滤器配置无效: {exc}",
        )

    success = event_manager.set_filter(bot_id, filter_config)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"机器人 {bot_id} 的事件监听器不存在",
        )

    return {
        "success": True,
        "message": "过滤器已设置",
        "data": {
            "bot_id": bot_id,
            "filter": filter_config.to_dict(),
        },
    }


# ---------------------------------------------------------------------------
# 事件历史查询
# ---------------------------------------------------------------------------

@router.get("/{bot_id}/history")
async def get_event_history(
    bot_id: str,
    limit: int = 50,
    event_type: Optional[str] = None,
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取指定机器人的事件历史。

    Args:
        bot_id: 机器人 ID。
        limit: 返回最近 N 条事件 (默认 50, 最大 200)。
        event_type: 按事件类型过滤 (可选, 如 "player_chat")。

    Returns:
        事件历史列表。
    """
    limit = min(limit, 200)
    events = event_manager.get_event_history(bot_id, limit)

    # 按事件类型过滤
    if event_type:
        events = [e for e in events if e.get("event_type") == event_type]

    return {
        "success": True,
        "data": {
            "bot_id": bot_id,
            "total": len(events),
            "limit": limit,
            "event_type": event_type,
            "events": events,
        },
    }


# ---------------------------------------------------------------------------
# 事件统计
# ---------------------------------------------------------------------------

@router.get("/{bot_id}/stats")
async def get_event_stats(
    bot_id: str,
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取指定机器人的事件和命令统计。

    Args:
        bot_id: 机器人 ID。

    Returns:
        事件和命令统计信息。
    """
    event_stats = event_manager.get_event_stats(bot_id)
    command_stats = event_manager.get_command_stats(bot_id)

    return {
        "success": True,
        "data": {
            "bot_id": bot_id,
            "event_stats": event_stats,
            "command_stats": command_stats,
        },
    }


@router.get("/stats")
async def get_all_event_stats(
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取所有机器人的事件和命令统计汇总。

    Returns:
        所有机器人的统计汇总。
    """
    stats = event_manager.get_all_stats()
    return {
        "success": True,
        "data": stats,
    }


# ---------------------------------------------------------------------------
# 命令系统管理
# ---------------------------------------------------------------------------

@router.get("/{bot_id}/commands")
async def get_commands(
    bot_id: str,
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取指定机器人已注册的命令列表。

    Args:
        bot_id: 机器人 ID。

    Returns:
        命令列表。
    """
    cmd_system = event_manager.get_command_system(bot_id)
    if cmd_system is None:
        raise HTTPException(
            status_code=404,
            detail=f"机器人 {bot_id} 的命令系统不存在",
        )

    return {
        "success": True,
        "data": {
            "bot_id": bot_id,
            "commands": cmd_system.get_commands(),
        },
    }


@router.get("/{bot_id}/commands/history")
async def get_command_history(
    bot_id: str,
    limit: int = 50,
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """获取指定机器人的命令执行历史。

    Args:
        bot_id: 机器人 ID。
        limit: 返回最近 N 条记录 (默认 50, 最大 200)。

    Returns:
        命令历史列表。
    """
    cmd_system = event_manager.get_command_system(bot_id)
    if cmd_system is None:
        raise HTTPException(
            status_code=404,
            detail=f"机器人 {bot_id} 的命令系统不存在",
        )

    limit = min(limit, 200)
    history = cmd_system.get_history(limit)

    return {
        "success": True,
        "data": {
            "bot_id": bot_id,
            "total": len(history),
            "limit": limit,
            "history": history,
        },
    }


@router.post("/{bot_id}/admin")
async def add_admin(
    bot_id: str,
    player_name: str,
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """为指定机器人添加管理员。

    Args:
        bot_id: 机器人 ID。
        player_name: 玩家名。

    Returns:
        操作结果。
    """
    success = event_manager.add_admin(bot_id, player_name)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"机器人 {bot_id} 的命令系统不存在",
        )

    return {
        "success": True,
        "message": f"已添加管理员: {player_name}",
        "data": {"bot_id": bot_id, "player_name": player_name},
    }


@router.delete("/{bot_id}/admin")
async def remove_admin(
    bot_id: str,
    player_name: str,
    _user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """从指定机器人移除管理员。

    Args:
        bot_id: 机器人 ID。
        player_name: 玩家名。

    Returns:
        操作结果。
    """
    success = event_manager.remove_admin(bot_id, player_name)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"机器人 {bot_id} 的命令系统不存在",
        )

    return {
        "success": True,
        "message": f"已移除管理员: {player_name}",
        "data": {"bot_id": bot_id, "player_name": player_name},
    }


# ======================================================================
# 模块导出
# ======================================================================

__all__ = ["router"]