"""认证中间件 - 从请求中提取当前用户。"""
from __future__ import annotations

from typing import Optional

from fastapi import Request

from app.database import get_db
from app.security import verify_jwt_token


async def get_current_user(request: Request) -> Optional[dict]:
    """从请求中提取当前用户信息。

    优先从 JWT Token 提取，回退到旧的 session cookie。
    也支持 ``?token=`` 查询参数 (用于下载等需要 window.open 的场景)。
    """
    # 1. 尝试 JWT Token (Authorization header)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        payload = verify_jwt_token(token)
        if payload and payload.get("user_id"):
            db = await get_db()
            user = await db.get_user_by_id(payload["user_id"])
            if user and user["status"] == "active":
                return dict(user)

    # 2. 尝试 cookie 中的 pocketterm_token
    token = request.cookies.get("pocketterm_token", "")
    if token:
        payload = verify_jwt_token(token)
        if payload and payload.get("user_id"):
            db = await get_db()
            user = await db.get_user_by_id(payload["user_id"])
            if user and user["status"] == "active":
                return dict(user)

    # 3. 尝试查询参数中的 token (用于 window.open 下载等场景)
    token = request.query_params.get("token", "")
    if token:
        payload = verify_jwt_token(token)
        if payload and payload.get("user_id"):
            db = await get_db()
            user = await db.get_user_by_id(payload["user_id"])
            if user and user["status"] == "active":
                return dict(user)

    return None


async def require_user(request: Request) -> dict:
    """要求用户已登录，否则抛出 401。"""
    from fastapi import HTTPException
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


async def require_admin(request: Request) -> dict:
    """要求管理员权限。"""
    from fastapi import HTTPException
    user = await require_user(request)
    if user["role"] not in ("superadmin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")
    return user


async def require_superadmin(request: Request) -> dict:
    """要求超级管理员权限。"""
    from fastapi import HTTPException
    user = await require_user(request)
    if user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="需要超级管理员权限")
    return user
