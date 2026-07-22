"""系统管理 API - nv1状态、封号检测、系统统计。"""
from __future__ import annotations

import json
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import get_db

router = APIRouter(prefix="/api/v2/system", tags=["system-admin"])


# ============================================================================
# 请求模型
# ============================================================================

class SetNV1KeyRequest(BaseModel):
    key: str = Field(..., max_length=8192, description="SAuth Key")
    api_token: str = Field("", max_length=2048, description="nv1 API 令牌 (留空则使用模拟模式)")
    expires_in_days: int = Field(7, description="Key 有效期 (天), 0=永久")


class SetNovaBuilderCredentialsRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100, description="NovaBuilder 用户名")
    password: str = Field(..., min_length=1, max_length=200, description="NovaBuilder 密码")
    api_key: str = Field("", max_length=8192, description="已有的 NovaBuilder API Key (可选)")


class ReportLoginFailureRequest(BaseModel):
    account_id: str = Field(..., max_length=64, description="游戏账号 ID")
    bot_id: str = Field("", max_length=64, description="机器人实例 ID")


# ============================================================================
# nv1 SAuth Key 管理
# ============================================================================

@router.get("/nv1/status")
async def nv1_status(request: Request):
    """获取 nv1 SAuth Key 状态。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    from app.auth.nv1_manager import nv1_manager
    status = nv1_manager.get_status()

    return {"success": True, "data": status}


@router.post("/nv1/config")
async def set_nv1_key(req: SetNV1KeyRequest, request: Request):
    """设置 nv1 SAuth Key (管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)

    from app.auth.nv1_manager import nv1_manager
    expires_at = 0 if req.expires_in_days == 0 else time.time() + req.expires_in_days * 86400
    await nv1_manager.set_key(req.key, expires_at=expires_at, api_token=req.api_token)

    db = await get_db()
    await db.add_log(
        target_type="system", target_id="nv1_config",
        level="success",
        message=f"管理员 {admin['username']} 设置 nv1 SAuth Key "
                f"(模式: {'模拟' if not req.api_token else '真实'}, "
                f"有效期: {req.expires_in_days}天)",
        created_by=admin["user_id"],
    )

    return {"success": True, "message": "nv1 Key 已设置"}


@router.post("/nv1/novabuilder-credentials")
async def set_novabuilder_credentials(req: SetNovaBuilderCredentialsRequest, request: Request):
    """设置 NovaBuilder 用户中心凭据, 启用自动刷新模式 (管理员)。

    设置后, 系统将自动登录 novabuilder.pro 获取/刷新 API Key,
    无需管理员手动替换过期的 Key。
    """
    from .auth import require_admin
    admin = await require_admin(request)

    from app.auth.nv1_manager import nv1_manager
    await nv1_manager.set_novabuilder_credentials(req.username, req.password)

    # 如果提供了已有的 API Key, 直接设置
    if req.api_key:
        await nv1_manager.set_key(
            req.api_key,
            expires_at=time.time() + 7 * 86400,
            api_token=req.api_key,
        )

    db = await get_db()
    await db.add_log(
        target_type="system", target_id="nv1_novabuilder",
        level="success",
        message=f"管理员 {admin['username']} 设置 NovaBuilder 凭据 (用户: {req.username})",
        created_by=admin["user_id"],
    )

    return {"success": True, "message": "NovaBuilder 凭据已设置, 已启用自动刷新模式"}


@router.post("/nv1/refresh")
async def refresh_nv1_key(request: Request):
    """手动刷新 nv1 SAuth Key (管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)

    from app.auth.nv1_manager import nv1_manager
    result = await nv1_manager.refresh_key()

    db = await get_db()
    if result["success"]:
        await db.add_log(
            target_type="system", target_id="nv1_refresh",
            level="success",
            message=f"管理员 {admin['username']} 手动刷新 nv1 Key 成功",
            details=json.dumps({"mode": result.get("mode")}),
            created_by=admin["user_id"],
        )
    else:
        await db.add_log(
            target_type="system", target_id="nv1_refresh",
            level="error",
            message=f"管理员 {admin['username']} 刷新 nv1 Key 失败: {result.get('error')}",
            created_by=admin["user_id"],
        )

    return {"success": result["success"], "data": result if result["success"] else None,
            "error": result.get("error")}


# ============================================================================
# 封号检测管理
# ============================================================================

@router.get("/ban/status")
async def ban_status(request: Request):
    """获取封号检测状态 (管理员)。"""
    from .auth import require_admin
    await require_admin(request)

    from app.ban_detection import ban_detector
    return {"success": True, "data": ban_detector.get_stats()}


@router.get("/ban/accounts")
async def banned_accounts(request: Request):
    """获取所有疑似封号账号 (管理员)。"""
    from .auth import require_admin
    await require_admin(request)

    from app.ban_detection import ban_detector
    accounts = ban_detector.get_all_banned_accounts()
    return {"success": True, "data": accounts}


@router.post("/ban/{account_id}/clear")
async def clear_ban_flag(account_id: str, request: Request):
    """解除账号封号标记 (管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)

    from app.ban_detection import ban_detector
    success = ban_detector.clear_ban_flag(account_id)

    if success:
        db = await get_db()
        await db.add_log(
            target_type="system", target_id="ban_clear",
            level="success",
            message=f"管理员 {admin['username']} 解除账号封号标记: {account_id}",
            created_by=admin["user_id"],
        )

        # 更新账号状态
        await db.update_account_status(account_id, "active")

    return {"success": success, "message": "封号标记已解除" if success else "账号未被标记为封号"}


@router.post("/ban/report-failure")
async def report_login_failure(req: ReportLoginFailureRequest, request: Request):
    """报告登录失败 (内部接口, 供机器人管理器调用)。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    from app.ban_detection import ban_detector
    count = ban_detector.report_login_failure(req.account_id, req.bot_id)

    db = await get_db()
    is_banned = ban_detector.is_suspected_ban(req.account_id)

    # 记录日志
    if is_banned:
        await db.add_log(
            target_type="bot", target_id=req.bot_id,
            level="error",
            message=f"疑似封号! 账号 {req.account_id} 连续 {count} 次登录失败",
            details=json.dumps({"account_id": req.account_id, "failure_count": count}),
            created_by=user["user_id"],
        )

        # 自动停止机器人
        if req.bot_id:
            try:
                from app.bot.manager import bot_manager
                await bot_manager.stop_bot(req.bot_id)
            except Exception:
                pass
            await db.update_bot_status(req.bot_id, "stopped")
            await db.add_log(
                target_type="bot", target_id=req.bot_id,
                level="warn",
                message=f"封号检测触发, 自动停止机器人",
                created_by="system",
            )

        # 更新账号状态
        await db.update_account_status(req.account_id, "banned")
    else:
        await db.add_log(
            target_type="bot", target_id=req.bot_id,
            level="warn",
            message=f"登录失败: 账号 {req.account_id} (累计 {count} 次)",
            created_by=user["user_id"],
        )

    return {
        "success": True,
        "data": {
            "failure_count": count,
            "suspected_ban": is_banned,
            "threshold": 3,
        },
    }


@router.post("/ban/report-success")
async def report_login_success(request: Request, account_id: str = ""):
    """报告登录成功 (内部接口)。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")

    from app.ban_detection import ban_detector
    ban_detector.report_login_success(account_id)

    return {"success": True, "message": "登录成功, 失败计数已重置"}


# ============================================================================
# 系统统计
# ============================================================================

@router.get("/stats")
async def system_stats(request: Request):
    """获取系统统计信息 (管理员)。"""
    from .auth import require_admin
    await require_admin(request)

    db = await get_db()

    users = await db.list_users()
    all_panels = await db.list_all_panels()
    all_bots = await db.list_all_bots()
    all_cards = await db.list_cards()

    from app.auth.nv1_manager import nv1_manager
    from app.ban_detection import ban_detector

    # 统计
    active_panels = [p for p in all_panels if p["status"] == "active"]
    expired_panels = [p for p in all_panels if p["status"] == "expired"]
    running_bots = [b for b in all_bots if b["status"] == "running"]

    return {
        "success": True,
        "data": {
            "users": {
                "total": len(users),
                "active": sum(1 for u in users if u["status"] == "active"),
                "admins": sum(1 for u in users if u["role"] in ("admin", "superadmin")),
                "banned": sum(1 for u in users if u["status"] == "banned"),
            },
            "panels": {
                "total": len(all_panels),
                "active": len(active_panels),
                "expired": len(expired_panels),
            },
            "bots": {
                "total": len(all_bots),
                "running": len(running_bots),
                "stopped": sum(1 for b in all_bots if b["status"] == "stopped"),
                "error": sum(1 for b in all_bots if b["status"] == "error"),
            },
            "cards": {
                "total": len(all_cards),
                "unused": sum(1 for c in all_cards if c["status"] == "unused"),
                "used": sum(1 for c in all_cards if c["status"] == "used"),
                "revoked": sum(1 for c in all_cards if c["status"] == "revoked"),
            },
            "nv1": nv1_manager.get_status(),
            "ban_detection": ban_detector.get_stats(),
            "timestamp": time.time(),
        },
    }


# ============================================================================
# 接入点管理 (v2 兼容端点)
# ============================================================================

@router.get("/access-points")
async def list_access_points_v2(request: Request):
    """列出可用接入点 (v2 兼容)。"""
    from .auth import require_user
    await require_user(request)
    from app.access_point.manager import get_manager
    mgr = get_manager()
    available = mgr.list_available()
    return {"success": True, "data": {"available": available}}


@router.post("/access-points/{name}/download")
async def download_access_point_v2(name: str, request: Request):
    """下载接入点二进制 (v2 兼容)。"""
    from .auth import require_user
    await require_user(request)
    from app.access_point.manager import get_manager
    mgr = get_manager()
    try:
        path = await mgr.download(name)
        return {"success": True, "data": {"path": str(path)}}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
