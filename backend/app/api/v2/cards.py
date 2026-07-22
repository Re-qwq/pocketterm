"""卡密管理 API - 创建、查看、撤销卡密。"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import get_db
from app.security import rate_limiter

router = APIRouter(prefix="/api/v2/cards", tags=["cards"])

# 预设时长选项 (天) -- None = 永久
DURATION_PRESETS = {
    "1h": 1 / 24,
    "6h": 6 / 24,
    "12h": 12 / 24,
    "1d": 1.0,
    "3d": 3.0,
    "7d": 7.0,
    "14d": 14.0,
    "30d": 30.0,
    "90d": 90.0,
    "180d": 180.0,
    "365d": 365.0,
    "permanent": None,
}


# ============================================================================
# 请求模型
# ============================================================================

class CreateCardRequest(BaseModel):
    key_type: str = Field(..., max_length=32, description="卡密类型: register/panel/renewal")
    duration: str = Field("permanent", max_length=32, description="时长预设: 1h/6h/12h/1d/3d/7d/14d/30d/90d/180d/365d/permanent")
    count: int = Field(1, ge=1, le=100, description="批量生成数量 (1-100)")
    card_expires_at: Optional[str] = Field(None, max_length=32, description="卡密本身过期时间 (ISO格式), 不填则永不过期")


class CreateCardCustomRequest(BaseModel):
    key_type: str = Field(..., max_length=32, description="卡密类型: register/panel/renewal")
    duration_days: Optional[float] = Field(None, description="自定义时长(天), None=永久, 支持小数如0.5=12小时")
    count: int = Field(1, ge=1, le=100)
    card_expires_at: Optional[str] = Field(None, max_length=32, description="卡密本身过期时间 (ISO格式)")


# ============================================================================
# 创建卡密
# ============================================================================

@router.post("")
async def create_cards(req: CreateCardRequest, request: Request):
    """创建卡密 (管理员)。支持批量生成。"""
    from .auth import require_admin
    admin = await require_admin(request)

    if req.key_type not in ("register", "panel", "renewal"):
        raise HTTPException(status_code=400, detail="卡密类型无效，可选: register/panel/renewal")

    if req.duration not in DURATION_PRESETS:
        raise HTTPException(
            status_code=400,
            detail=f"时长预设无效，可选: {', '.join(DURATION_PRESETS.keys())}",
        )

    duration_days = DURATION_PRESETS[req.duration]
    db = await get_db()

    # 解析卡密本身过期时间
    card_expires_ts = None
    if req.card_expires_at:
        try:
            card_expires_ts = time.mktime(time.strptime(req.card_expires_at, "%Y-%m-%dT%H:%M"))
        except (ValueError, TypeError):
            card_expires_ts = None

    created = []
    for _ in range(req.count):
        card_id, key = await db.create_card_key(
            key_type=req.key_type,
            duration_days=duration_days,
            created_by=admin["user_id"],
            expires_at=card_expires_ts,
        )
        created.append({"card_id": card_id, "key": key})

    # 记录创建日志
    dur_desc = req.duration if req.duration == "permanent" else f"{req.duration}"
    await db.add_log(
        target_type="system", target_id="card_creation",
        level="success",
        message=f"管理员 {admin['username']} 创建 {len(created)} 个 {req.key_type} 卡密 (时长: {dur_desc})",
        details='{"count": %d, "key_type": "%s", "duration": "%s"}' % (len(created), req.key_type, req.duration),
        created_by=admin["user_id"],
    )

    return {"success": True, "data": {"cards": created, "count": len(created)}}


@router.post("/custom")
async def create_cards_custom(req: CreateCardCustomRequest, request: Request):
    """创建卡密 (自定义天数, 管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)

    if req.key_type not in ("register", "panel", "renewal"):
        raise HTTPException(status_code=400, detail="卡密类型无效")

    # 限制最小时长为 1 小时 = 1/24 天
    if req.duration_days is not None and req.duration_days < (1 / 24):
        raise HTTPException(status_code=400, detail="最小时长为 1 小时")

    db = await get_db()

    card_expires_ts = None
    if req.card_expires_at:
        try:
            card_expires_ts = time.mktime(time.strptime(req.card_expires_at, "%Y-%m-%dT%H:%M"))
        except (ValueError, TypeError):
            card_expires_ts = None

    created = []
    for _ in range(req.count):
        card_id, key = await db.create_card_key(
            key_type=req.key_type,
            duration_days=req.duration_days,
            created_by=admin["user_id"],
            expires_at=card_expires_ts,
        )
        created.append({"card_id": card_id, "key": key})

    dur_desc = "永久" if req.duration_days is None else f"{req.duration_days}天"
    await db.add_log(
        target_type="system", target_id="card_creation",
        level="success",
        message=f"管理员 {admin['username']} 创建 {len(created)} 个 {req.key_type} 卡密 (自定义时长: {dur_desc})",
        created_by=admin["user_id"],
    )

    return {"success": True, "data": {"cards": created, "count": len(created)}}


# ============================================================================
# 查询卡密
# ============================================================================

@router.get("")
async def list_cards(
    request: Request,
    key_type: Optional[str] = None,
    status: Optional[str] = None,
    created_by: Optional[str] = None,
    include_revoked: bool = False,
    limit: int = 100,
):
    """列出卡密。管理员看自己的，超管可看所有人的。

    ``include_revoked`` 为 False (默认) 时隐藏已撤销的卡密; 为 True 时显示
    全部卡密 (用于管理员审计)。
    """
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 普通管理员只能看自己的卡密; 超管默认看全部，传了 created_by 则按值过滤
    if admin["role"] == "superadmin":
        filter_created_by = created_by if created_by else None
    else:
        filter_created_by = admin["user_id"]

    cards = await db.list_cards(
        created_by=filter_created_by,
        key_type=key_type,
        status=status,
    )

    # 默认隐藏已撤销的卡密, 除非显式请求包含
    if not include_revoked:
        cards = [c for c in cards if c["status"] != "revoked"]

    return {
        "success": True,
        "data": [
            {
                "card_id": c["card_id"],
                "key": c["key"],
                "key_type": c["key_type"],
                "status": c["status"],
                "duration_days": c["duration_days"],
                "bound_user_id": c["bound_user_id"],
                "bound_panel_id": c["bound_panel_id"],
                "created_by": c["created_by"],
                "created_at": c["created_at"],
                "used_at": c["used_at"],
                "expires_at": c["expires_at"],
            }
            for c in cards[:limit]
        ],
    }


@router.get("/stats")
async def card_stats(request: Request):
    """卡密统计 (管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 超管看全部, 管理员看自己
    filter_by = None if admin["role"] == "superadmin" else admin["user_id"]
    cards = await db.list_cards(created_by=filter_by)

    stats = {
        "total": len(cards),
        "unused": sum(1 for c in cards if c["status"] == "unused"),
        "used": sum(1 for c in cards if c["status"] == "used"),
        "revoked": sum(1 for c in cards if c["status"] == "revoked"),
        "by_type": {
            "register": sum(1 for c in cards if c["key_type"] == "register"),
            "panel": sum(1 for c in cards if c["key_type"] == "panel"),
            "renewal": sum(1 for c in cards if c["key_type"] == "renewal"),
        },
    }
    return {"success": True, "data": stats}


@router.get("/{card_id}")
async def get_card(card_id: str, request: Request):
    """查看单个卡密详情。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    card = await db.get_card_by_id(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="卡密不存在")

    # 管理员只能看自己的, 超管可以看任意
    if admin["role"] != "superadmin" and card["created_by"] != admin["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看此卡密")

    return {
        "success": True,
        "data": {
            "card_id": card["card_id"],
            "key": card["key"],
            "key_type": card["key_type"],
            "status": card["status"],
            "duration_days": card["duration_days"],
            "bound_user_id": card["bound_user_id"],
            "bound_panel_id": card["bound_panel_id"],
            "created_by": card["created_by"],
            "created_at": card["created_at"],
            "used_at": card["used_at"],
            "expires_at": card["expires_at"],
        },
    }


# ============================================================================
# 撤销卡密
# ============================================================================

@router.post("/{card_id}/revoke")
async def revoke_card(card_id: str, request: Request):
    """撤销卡密 (管理员, 只能撤销自己创建的)。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    card = await db.get_card_by_id(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="卡密不存在")

    # 管理员只能撤销自己的, 超管可以撤销任意
    if admin["role"] != "superadmin" and card["created_by"] != admin["user_id"]:
        raise HTTPException(status_code=403, detail="无权撤销此卡密")

    if card["status"] == "revoked":
        raise HTTPException(status_code=400, detail="卡密已被撤销")

    if card["status"] == "used":
        raise HTTPException(status_code=400, detail="卡密已被使用，无法撤销")

    await db.revoke_card(card_id)
    await db.add_log(
        target_type="system", target_id="card_revoke",
        level="warn",
        message=f"管理员 {admin['username']} 撤销卡密: {card['key']}",
        created_by=admin["user_id"],
    )
    return {"success": True, "message": "卡密已撤销"}


# ============================================================================
# 卡密创建日志
# ============================================================================

@router.get("/logs/creation")
async def card_creation_logs(
    request: Request,
    admin_id: Optional[str] = None,
    limit: int = 100,
):
    """查看卡密创建日志。管理员看自己的, 超管可看任意人的。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 普通管理员看自己的创建日志; 超管默认看全部，传了 admin_id 则按值过滤
    if admin["role"] == "superadmin" and admin_id:
        logs = await db.list_logs_by_creator(admin_id, limit=limit)
    elif admin["role"] == "superadmin":
        # 超管不指定 admin_id 时查看全部日志
        logs = await db.list_logs(limit=limit)
    else:
        logs = await db.list_logs_by_creator(admin["user_id"], limit=limit)
    # 过滤出卡密创建相关日志
    card_logs = [l for l in logs if "card" in l["message"].lower() or "卡密" in l["message"]]

    return {
        "success": True,
        "data": [
            {
                "log_id": l["log_id"],
                "level": l["level"],
                "message": l["message"],
                "details": l["details"],
                "created_at": l["created_at"],
                "created_by": l["created_by"],
            }
            for l in card_logs
        ],
    }


@router.get("/logs/all")
async def all_card_logs(request: Request, limit: int = 200):
    """查看所有管理员的卡密创建日志 (仅超管)。"""
    from .auth import require_superadmin
    admin = await require_superadmin(request)
    db = await get_db()

    # 获取所有卡密记录, 按创建者分组
    cards = await db.list_cards()
    by_creator: dict[str, list] = {}
    for c in cards:
        creator = c["created_by"] or "system"
        if creator not in by_creator:
            by_creator[creator] = []
        by_creator[creator].append({
            "card_id": c["card_id"],
            "key": c["key"],
            "key_type": c["key_type"],
            "status": c["status"],
            "duration_days": c["duration_days"],
            "created_at": c["created_at"],
            "used_at": c["used_at"],
            "bound_user_id": c["bound_user_id"],
            "bound_panel_id": c["bound_panel_id"],
        })

    # 获取用户名映射
    users_map = {}
    all_users = await db.list_users()
    for u in all_users:
        users_map[u["user_id"]] = u["username"]

    result = []
    for creator_id, creator_cards in by_creator.items():
        result.append({
            "admin_id": creator_id,
            "admin_username": users_map.get(creator_id, creator_id),
            "total_created": len(creator_cards),
            "cards": creator_cards[:limit],
        })

    return {"success": True, "data": result}
