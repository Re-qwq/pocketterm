"""面板管理 API - 创建、续费、删除面板。"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import get_db

router = APIRouter(prefix="/api/v2/panels", tags=["panels"])


# ============================================================================
# 请求模型
# ============================================================================

class CreatePanelRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=50, description="面板名称")
    card_key: str = Field(..., max_length=64, description="面板卡密")


class RenewPanelRequest(BaseModel):
    card_key: str = Field(..., max_length=64, description="续期卡密")


# ============================================================================
# 创建面板
# ============================================================================

@router.post("")
async def create_panel(req: CreatePanelRequest, request: Request):
    """创建面板 (用户, 需要面板卡密)。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 1. 验证面板卡密
    card = await db.get_card_by_key(req.card_key.upper())
    if card is None:
        raise HTTPException(status_code=400, detail="卡密无效")
    if card["key_type"] != "panel":
        raise HTTPException(status_code=400, detail="卡密类型不正确，需要面板卡密")
    if card["status"] != "unused":
        raise HTTPException(status_code=400, detail="卡密已被使用或已撤销")
    if card["expires_at"] and card["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="卡密已过期")

    # 2. 计算面板过期时间
    expire_at = None
    if card["duration_days"]:
        expire_at = time.time() + card["duration_days"] * 86400

    # 3. 创建面板
    panel_id = await db.create_panel(
        user_id=user["user_id"],
        name=req.name,
        created_by_card=card["card_id"],
        expire_at=expire_at,
    )

    # 4. 标记卡密已使用
    await db.use_card(card["card_id"], user_id=user["user_id"], panel_id=panel_id)

    # 5. 记录日志
    dur_desc = "永久" if expire_at is None else f"{card['duration_days']}天"
    await db.add_log(
        target_type="panel", target_id=panel_id,
        level="success",
        message=f"创建面板: {req.name} (时长: {dur_desc})",
        ip=request.client.host if request.client else "",
        created_by=user["user_id"],
    )

    return {
        "success": True,
        "data": {
            "panel_id": panel_id,
            "name": req.name,
            "expire_at": expire_at,
        },
    }


# ============================================================================
# 查询面板
# ============================================================================

@router.get("")
async def list_panels(request: Request):
    """列出面板。用户看自己的, 管理员看所有。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    if user["role"] in ("superadmin", "admin"):
        panels = await db.list_all_panels()
    else:
        panels = await db.list_panels_by_user(user["user_id"])

    # 检查过期状态并自动更新
    now = time.time()
    result = []
    for p in panels:
        is_expired = (
            p["status"] == "active"
            and p["expire_at"] is not None
            and p["expire_at"] < now
        )
        if is_expired:
            await db.update_panel_status(p["panel_id"], "expired")
            await db.add_log(
                target_type="panel", target_id=p["panel_id"],
                level="warn",
                message=f"面板已自动过期: {p['name']}",
                created_by="system",
            )

        result.append({
            "panel_id": p["panel_id"],
            "user_id": p["user_id"],
            "name": p["name"],
            "status": "expired" if is_expired else p["status"],
            "created_at": p["created_at"],
            "expire_at": p["expire_at"],
            "created_by_card": p["created_by_card"],
        })

    return {"success": True, "data": result}


@router.get("/{panel_id}")
async def get_panel(panel_id: str, request: Request):
    """获取面板详情。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    panel = await db.get_panel(panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail="面板不存在")

    # 权限检查: 普通用户只能看自己的
    if user["role"] == "user" and panel["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看此面板")

    # 检查过期
    now = time.time()
    is_expired = (
        panel["status"] == "active"
        and panel["expire_at"] is not None
        and panel["expire_at"] < now
    )
    status = "expired" if is_expired else panel["status"]

    return {
        "success": True,
        "data": {
            "panel_id": panel["panel_id"],
            "user_id": panel["user_id"],
            "name": panel["name"],
            "status": status,
            "created_at": panel["created_at"],
            "expire_at": panel["expire_at"],
            "created_by_card": panel["created_by_card"],
        },
    }


# ============================================================================
# 续费面板
# ============================================================================

@router.post("/{panel_id}/renew")
async def renew_panel(panel_id: str, req: RenewPanelRequest, request: Request):
    """续费面板 (需要续期卡密)。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    panel = await db.get_panel(panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail="面板不存在")

    # 权限检查
    if user["role"] == "user" and panel["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权续费此面板")

    # 验证续期卡密
    card = await db.get_card_by_key(req.card_key.upper())
    if card is None:
        raise HTTPException(status_code=400, detail="卡密无效")
    if card["key_type"] != "renewal":
        raise HTTPException(status_code=400, detail="卡密类型不正确，需要续期卡密")
    if card["status"] != "unused":
        raise HTTPException(status_code=400, detail="卡密已被使用或已撤销")
    if card["expires_at"] and card["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="卡密已过期")

    # 计算续期后的过期时间
    now = time.time()
    if card["duration_days"] is None:
        # 永久
        new_expire_at = None
    else:
        # 如果当前未过期, 在现有到期时间上追加; 否则从现在开始计算
        base = panel["expire_at"] if (panel["expire_at"] and panel["expire_at"] > now) else now
        new_expire_at = base + card["duration_days"] * 86400

    await db.renew_panel(panel_id, new_expire_at)
    await db.use_card(card["card_id"], user_id=user["user_id"], panel_id=panel_id)

    dur_desc = "永久" if new_expire_at is None else f"{card['duration_days']}天"
    await db.add_log(
        target_type="panel", target_id=panel_id,
        level="success",
        message=f"面板续费: {panel['name']} (+{dur_desc})",
        ip=request.client.host if request.client else "",
        created_by=user["user_id"],
    )

    return {
        "success": True,
        "data": {
            "panel_id": panel_id,
            "expire_at": new_expire_at,
        },
    }


# ============================================================================
# 删除面板
# ============================================================================

@router.delete("/{panel_id}")
async def delete_panel(panel_id: str, request: Request):
    """删除面板 (面板所有者或管理员)。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    panel = await db.get_panel(panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail="面板不存在")

    # 权限检查
    if user["role"] == "user" and panel["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权删除此面板")

    panel_name = panel["name"]

    # 先停止该面板下所有运行中的机器人
    bots = await db.list_bots_by_panel(panel_id)
    for bot in bots:
        if bot["status"] == "running":
            try:
                from app.bot.manager import bot_manager
                await bot_manager.stop_bot(bot["bot_id"])
            except Exception:
                pass  # 停止失败也不阻塞删除
            await db.update_bot_status(bot["bot_id"], "deleted")

    await db.delete_panel(panel_id)

    await db.add_log(
        target_type="panel", target_id=panel_id,
        level="warn",
        message=f"删除面板: {panel_name} (已停止 {len(bots)} 个机器人)",
        created_by=user["user_id"],
    )
    return {"success": True, "message": "面板已删除"}


# ============================================================================
# 面板启动检查 (过期拦截)
# ============================================================================

@router.post("/{panel_id}/check")
async def check_panel(panel_id: str, request: Request):
    """检查面板状态, 用于启动前验证。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    panel = await db.get_panel(panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail="面板不存在")

    # 权限检查
    if user["role"] == "user" and panel["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作此面板")

    now = time.time()
    is_expired = (
        panel["expire_at"] is not None
        and panel["expire_at"] < now
    )

    if is_expired:
        # 自动更新状态
        if panel["status"] == "active":
            await db.update_panel_status(panel_id, "expired")
        return {
            "success": False,
            "message": "面板已到期，请续费后继续使用",
            "data": {
                "panel_id": panel_id,
                "status": "expired",
                "expire_at": panel["expire_at"],
            },
        }

    return {
        "success": True,
        "data": {
            "panel_id": panel_id,
            "status": panel["status"],
            "expire_at": panel["expire_at"],
            "remaining_seconds": (
                int(panel["expire_at"] - now)
                if panel["expire_at"]
                else None
            ),
        },
    }
