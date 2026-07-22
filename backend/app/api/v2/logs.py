"""日志查询 API - 用户/面板/机器人/系统日志。"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from app.database import get_db

router = APIRouter(prefix="/api/v2/logs", tags=["logs"])


# ============================================================================
# 通用日志查询
# ============================================================================

@router.get("")
async def query_logs(
    request: Request,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    level: Optional[str] = None,
    limit: int = 100,
):
    """查询日志。用户看自己的, 管理员看所有。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    # 权限控制
    if user["role"] == "user":
        # 普通用户只能看自己的用户日志, 和自己面板/机器人的日志
        if target_type == "user" and target_id and target_id != user["user_id"]:
            raise HTTPException(status_code=403, detail="无权查看其他用户的日志")
        if target_type == "user" and not target_id:
            target_id = user["user_id"]
        # 对于 panel/bot 日志, 需要验证所有权 (在具体路由中检查)
        if target_type in ("panel", "bot") and target_id:
            if not await _check_target_access(target_type, target_id, user, db):
                raise HTTPException(status_code=403, detail="无权查看此日志")
        if target_type == "system":
            raise HTTPException(status_code=403, detail="无权查看系统日志")
        # 普通用户不传 target_type 时, 默认只能看自己的日志
        if target_type is None:
            target_type = "user"
            target_id = user["user_id"]

    logs = await db.list_logs(
        target_type=target_type,
        target_id=target_id,
        level=level,
        limit=limit,
    )

    return {
        "success": True,
        "data": [
            {
                "log_id": l["log_id"],
                "target_type": l["target_type"],
                "target_id": l["target_id"],
                "level": l["level"],
                "message": l["message"],
                "details": l["details"],
                "ip": l["ip"],
                "created_at": l["created_at"],
                "created_by": l["created_by"],
            }
            for l in logs
        ],
    }


# ============================================================================
# 用户日志
# ============================================================================

@router.get("/user/{user_id}")
async def user_logs(user_id: str, request: Request, limit: int = 100):
    """查看指定用户的日志。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    # 权限: 用户看自己, 管理员看所有
    if user["role"] == "user" and user_id != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看其他用户的日志")

    logs = await db.list_logs(target_type="user", target_id=user_id, limit=limit)
    return {
        "success": True,
        "data": [
            {
                "log_id": l["log_id"],
                "level": l["level"],
                "message": l["message"],
                "ip": l["ip"],
                "created_at": l["created_at"],
                "created_by": l["created_by"],
            }
            for l in logs
        ],
    }


# ============================================================================
# 面板日志
# ============================================================================

@router.get("/panel/{panel_id}")
async def panel_logs(panel_id: str, request: Request, limit: int = 100):
    """查看指定面板的日志。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    # 权限: 验证面板所有权
    if user["role"] == "user":
        panel = await db.get_panel(panel_id)
        if panel is None or panel["user_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="无权查看此面板日志")

    logs = await db.list_logs(target_type="panel", target_id=panel_id, limit=limit)
    return {
        "success": True,
        "data": [
            {
                "log_id": l["log_id"],
                "level": l["level"],
                "message": l["message"],
                "ip": l["ip"],
                "created_at": l["created_at"],
                "created_by": l["created_by"],
            }
            for l in logs
        ],
    }


# ============================================================================
# 机器人日志
# ============================================================================

@router.get("/bot/{bot_id}")
async def bot_logs(bot_id: str, request: Request, limit: int = 100):
    """查看指定机器人的日志。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    # 权限: 验证机器人所属面板的所有权
    if user["role"] == "user":
        bot = await db.get_bot(bot_id)
        if bot is None:
            raise HTTPException(status_code=404, detail="机器人不存在")
        panel = await db.get_panel(bot["panel_id"])
        if panel is None or panel["user_id"] != user["user_id"]:
            raise HTTPException(status_code=403, detail="无权查看此机器人日志")

    logs = await db.list_logs(target_type="bot", target_id=bot_id, limit=limit)
    return {
        "success": True,
        "data": [
            {
                "log_id": l["log_id"],
                "level": l["level"],
                "message": l["message"],
                "ip": l["ip"],
                "created_at": l["created_at"],
                "created_by": l["created_by"],
            }
            for l in logs
        ],
    }


# ============================================================================
# 系统日志 (管理员)
# ============================================================================

@router.get("/system")
async def system_logs(request: Request, limit: int = 200):
    """查看系统日志 (管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    logs = await db.list_logs(target_type="system", limit=limit)
    return {
        "success": True,
        "data": [
            {
                "log_id": l["log_id"],
                "target_id": l["target_id"],
                "level": l["level"],
                "message": l["message"],
                "details": l["details"],
                "ip": l["ip"],
                "created_at": l["created_at"],
                "created_by": l["created_by"],
            }
            for l in logs
        ],
    }


# ============================================================================
# 管理员操作日志 (管理员)
# ============================================================================

@router.get("/admin/{admin_id}")
async def admin_action_logs(admin_id: str, request: Request, limit: int = 100):
    """查看指定管理员的操作日志。管理员看自己的, 超管可看任意。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 普通管理员看自己的, 超管可看任意
    if admin["role"] != "superadmin" and admin_id != admin["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看其他管理员的日志")

    logs = await db.list_logs_by_creator(admin_id, limit=limit)
    return {
        "success": True,
        "data": [
            {
                "log_id": l["log_id"],
                "target_type": l["target_type"],
                "target_id": l["target_id"],
                "level": l["level"],
                "message": l["message"],
                "details": l["details"],
                "ip": l["ip"],
                "created_at": l["created_at"],
                "created_by": l["created_by"],
            }
            for l in logs
        ],
    }


# ============================================================================
# 辅助函数
# ============================================================================

async def _check_target_access(target_type: str, target_id: str, user: dict, db) -> bool:
    """检查用户是否有权查看指定目标的日志。"""
    if target_type == "user":
        return target_id == user["user_id"]
    elif target_type == "panel":
        panel = await db.get_panel(target_id)
        return panel is not None and panel["user_id"] == user["user_id"]
    elif target_type == "bot":
        bot = await db.get_bot(target_id)
        if bot is None:
            return False
        panel = await db.get_panel(bot["panel_id"])
        return panel is not None and panel["user_id"] == user["user_id"]
    return False
