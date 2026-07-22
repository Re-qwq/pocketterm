"""sauth_json 自动刷新管理 API - 4399 账号池与刷新状态。

端点:
    - POST   /api/v2/sauth/accounts            添加 4399 账号 (superadmin)
    - GET    /api/v2/sauth/accounts            列出 4399 账号 (admin, 密码掩码)
    - DELETE /api/v2/sauth/accounts/{id}       删除 4399 账号 (superadmin)
    - POST   /api/v2/sauth/accounts/{id}/test  测试 4399 账号 (admin)
    - POST   /api/v2/sauth/refresh             手动触发刷新 (admin)
    - GET    /api/v2/sauth/status              获取刷新状态 (admin)
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import get_db

logger = logging.getLogger("pocketterm.api.sauth_refresh")

router = APIRouter(prefix="/api/v2/sauth", tags=["sauth-refresh"])


# ============================================================================
# 请求模型
# ============================================================================

class AddAccountRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=100, description="4399 用户名")
    password: str = Field(..., min_length=1, max_length=200, description="4399 密码")


# ============================================================================
# 4399 账号管理
# ============================================================================

@router.post("/accounts")
async def add_account(req: AddAccountRequest, request: Request):
    """添加 4399 账号到账号池 (超级管理员)。"""
    from .auth import require_superadmin
    admin = await require_superadmin(request)

    from app.auth.sauth_refresh import sauth_refresher
    is_new = await sauth_refresher.add_4399_account(req.username, req.password)

    db = await get_db()
    await db.add_log(
        target_type="system", target_id="sauth_account",
        level="success",
        message=(
            f"管理员 {admin['username']} "
            f"{'添加' if is_new else '更新'} 4399 账号: {req.username}"
        ),
        created_by=admin["user_id"],
    )

    return {
        "success": True,
        "message": "账号已添加" if is_new else "账号已存在, 密码已更新",
        "data": {"username": req.username, "is_new": is_new},
    }


@router.get("/accounts")
async def list_accounts(request: Request):
    """列出所有 4399 账号 (管理员, 密码掩码)。"""
    from .auth import require_admin
    await require_admin(request)

    from app.auth.sauth_refresh import sauth_refresher
    accounts = await sauth_refresher.list_4399_accounts()

    # 补充掩码密码字段 (统一掩码, 不泄露真实密码与长度)
    for acc in accounts:
        acc["password"] = "***"

    return {"success": True, "data": accounts}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, request: Request):
    """删除 4399 账号 (超级管理员)。"""
    from .auth import require_superadmin
    admin = await require_superadmin(request)

    from app.auth.sauth_refresh import sauth_refresher
    account = await sauth_refresher.get_4399_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="4399 账号不存在")

    username = account["username"]
    success = await sauth_refresher.delete_4399_account(account_id)

    db = await get_db()
    await db.add_log(
        target_type="system", target_id="sauth_account",
        level="warn",
        message=f"管理员 {admin['username']} 删除 4399 账号: {username}",
        created_by=admin["user_id"],
    )

    if not success:
        raise HTTPException(status_code=500, detail="删除失败")

    return {"success": True, "message": f"账号 {username} 已删除"}


@router.post("/accounts/{account_id}/test")
async def test_account(account_id: str, request: Request):
    """测试 4399 账号能否登录 (管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)

    from app.auth.sauth_refresh import sauth_refresher
    account = await sauth_refresher.get_4399_account(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="4399 账号不存在")

    username = account["username"]
    password = account["password"]

    result = await sauth_refresher.test_4399_account(username, password)

    db = await get_db()
    await db.add_log(
        target_type="system", target_id="sauth_account_test",
        level="success" if result["success"] else "error",
        message=(
            f"管理员 {admin['username']} 测试 4399 账号 {username}: "
            f"{result['message']}"
        ),
        details=json.dumps(
            {"username": username, "uid": result.get("uid", "")},
            ensure_ascii=False,
        ),
        created_by=admin["user_id"],
    )

    return {
        "success": result["success"],
        "data": {
            "username": username,
            "uid": result.get("uid", ""),
            "message": result["message"],
        },
    }


# ============================================================================
# 刷新与状态
# ============================================================================

@router.post("/refresh")
async def refresh_sauth(request: Request):
    """手动触发 sauth_json 刷新 (管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)

    from app.auth.sauth_refresh import sauth_refresher
    sauth_str = await sauth_refresher.get_fresh_sauth()

    db = await get_db()
    if sauth_str:
        await db.add_log(
            target_type="system", target_id="sauth_refresh",
            level="success",
            message=f"管理员 {admin['username']} 手动触发 sauth_json 刷新成功",
            created_by=admin["user_id"],
        )
        return {
            "success": True,
            "message": "sauth_json 刷新成功",
            "data": sauth_refresher.get_status(),
        }

    await db.add_log(
        target_type="system", target_id="sauth_refresh",
        level="error",
        message=f"管理员 {admin['username']} 手动触发 sauth_json 刷新失败",
        created_by=admin["user_id"],
    )
    raise HTTPException(
        status_code=500,
        detail="sauth_json 刷新失败 (无可用 4399 账号或登录均失败)",
    )


@router.get("/status")
async def get_status(request: Request):
    """获取 sauth_json 刷新状态 (管理员)。"""
    from .auth import require_admin
    await require_admin(request)

    from app.auth.sauth_refresh import sauth_refresher
    status = sauth_refresher.get_status()

    # 附带账号池统计
    db = await get_db()
    all_accounts = await db.list_sauth_accounts()
    status["accounts"] = {
        "total": len(all_accounts),
        "active": sum(1 for a in all_accounts if a["status"] == "active"),
        "failed": sum(1 for a in all_accounts if a["status"] == "failed"),
        "disabled": sum(1 for a in all_accounts if a["status"] == "disabled"),
    }

    return {"success": True, "data": status}
