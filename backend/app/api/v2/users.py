"""用户认证 API - 注册、登录、卡密验证。"""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.database import get_db
from app.security import (
    create_jwt_token,
    hash_password,
    rate_limiter,
    verify_password,
    verify_csrf_token,
    check_anomaly,
    report_suspicious,
)
from .captcha import generate_captcha, verify_captcha

router = APIRouter(prefix="/api/v2/auth", tags=["auth"])


# ============================================================================
# 请求模型
# ============================================================================

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20, description="用户名")
    password: str = Field(..., min_length=6, max_length=100, description="密码")
    card_key: str = Field(..., description="注册卡密")
    captcha_answer: str = Field(..., description="图形验证码答案")
    captcha_id: str = Field(..., description="验证码 ID")


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="当前密码")
    new_password: str = Field(..., min_length=6, max_length=100, description="新密码 (至少6位)")


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)
    password: str = Field(..., min_length=6, max_length=100)
    role: str = Field("user", description="角色: user/admin")
    duration_days: Optional[int] = Field(None, description="有效期天数，None=永久")


# ============================================================================
# 注册
# ============================================================================

@router.post("/register")
async def register(req: RegisterRequest, request: Request):
    """用户注册（需要注册卡密 + 图形验证码）。"""
    client_ip = request.client.host if request.client else ""

    # 频率限制
    if not rate_limiter.check(f"register:{client_ip}", max_requests=5, window=300):
        raise HTTPException(status_code=429, detail="注册请求过于频繁，请5分钟后再试")

    db = await get_db()

    # 1. 验证图形验证码 (内置验证码系统)
    if not verify_captcha(req.captcha_id, req.captcha_answer):
        report_suspicious(f"register:{client_ip}")
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    # 2. 验证注册卡密
    card = await db.get_card_by_key(req.card_key.upper())
    if card is None or card["key_type"] != "register":
        raise HTTPException(status_code=400, detail="卡密无效或类型不正确")
    if card["status"] != "unused":
        raise HTTPException(status_code=400, detail="卡密已被使用")
    if card["expires_at"] and card["expires_at"] < time.time():
        raise HTTPException(status_code=400, detail="卡密已过期")

    # 3. 检查用户名是否已存在
    existing = await db.get_user_by_username(req.username)
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    # 4. 计算过期时间
    expire_at = None
    if card["duration_days"]:
        expire_at = time.time() + card["duration_days"] * 86400

    # 5. 创建用户
    user_id = await db.create_user(
        username=req.username,
        password_hash=hash_password(req.password),
        role="user",
        created_by=card["card_id"],
        expire_at=expire_at,
    )

    # 6. 标记卡密已使用
    await db.use_card(card["card_id"], user_id=user_id)

    # 7. 记录日志
    await db.add_log(
        target_type="user", target_id=user_id,
        level="success", message=f"用户注册成功: {req.username}",
        ip=client_ip, created_by=user_id,
    )

    return {"success": True, "message": "注册成功", "data": {"user_id": user_id}}


# ============================================================================
# 获取验证码
# ============================================================================

@router.get("/captcha")
async def get_captcha():
    """获取注册验证码图片。"""
    captcha_id, image_base64 = generate_captcha()
    return {
        "success": True,
        "data": {
            "captcha_id": captcha_id,
            "image": f"data:image/png;base64,{image_base64}",
        },
    }


@router.get("/captcha/debug")
async def get_captcha_debug(captcha_id: str):
    """调试用: 获取验证码答案 (仅开发环境, 生产环境自动禁用)。"""
    import os
    # 生产环境禁用此接口
    if os.environ.get("POCKETTERM_ENV") == "production":
        raise HTTPException(status_code=404, detail="接口不存在")
    if not os.environ.get("POCKETTERM_DEBUG"):
        raise HTTPException(status_code=404, detail="接口不存在")
    from .captcha import _captcha_store
    entry = _captcha_store.get(captcha_id)
    if entry:
        return {"success": True, "answer": entry["answer"]}
    return {"success": False, "answer": None}


# ============================================================================
# 登录
# ============================================================================

@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """用户登录（不需要验证码）。"""
    client_ip = request.client.host if request.client else ""

    # 频率限制
    if not rate_limiter.check(f"login:{client_ip}", max_requests=10, window=300):
        raise HTTPException(status_code=429, detail="登录请求过于频繁，请5分钟后再试")

    db = await get_db()
    user = await db.get_user_by_username(req.username)

    if user is None:
        report_suspicious(f"login:{client_ip}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if user["status"] != "active":
        raise HTTPException(status_code=403, detail=f"账号已被暂停或封禁: {user['status']}")

    if not verify_password(req.password, user["password_hash"]):
        report_suspicious(f"login:{client_ip}")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 检查过期
    if user["expire_at"] and user["expire_at"] < time.time():
        await db.update_user_status(user["user_id"], "suspended")
        raise HTTPException(status_code=403, detail="账号已过期")

    # 异常检测
    anomaly = check_anomaly(f"login:{client_ip}")
    if anomaly["suspended"]:
        raise HTTPException(status_code=403, detail="检测到异常行为，账号已被暂停")

    # 生成 JWT Token
    token = create_jwt_token({
        "user_id": user["user_id"],
        "username": user["username"],
        "role": user["role"],
    }, expires_in=86400)

    # 设置 Cookie
    response.set_cookie(
        key="pocketterm_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400,
    )

    # 更新登录信息
    await db.update_user_login(user["user_id"], ip=client_ip)

    # 记录日志
    await db.add_log(
        target_type="user", target_id=user["user_id"],
        level="info", message=f"用户登录: {req.username}",
        ip=client_ip, created_by=user["user_id"],
    )

    return {
        "success": True,
        "token": token,
        "data": {
            "user_id": user["user_id"],
            "username": user["username"],
            "role": user["role"],
            "must_change_password": bool(user["must_change_password"]),
        },
    }


# ============================================================================
# 登出
# ============================================================================

@router.post("/logout")
async def logout(request: Request, response: Response):
    """用户登出。"""
    # 记录登出日志
    from .auth import get_current_user
    user = await get_current_user(request)
    if user:
        client_ip = request.client.host if request.client else ""
        db = await get_db()
        await db.add_log(
            target_type="user", target_id=user["user_id"],
            level="info", message=f"用户登出: {user['username']}",
            ip=client_ip, created_by=user["user_id"],
        )
    response.delete_cookie("pocketterm_token")
    return {"success": True, "message": "已登出"}


# ============================================================================
# 活动日志 (管理员)
# ============================================================================

@router.get("/activity-log")
async def activity_log(request: Request, limit: int = 200):
    """查看用户活动日志（登录、注册、登出）。管理员权限。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 查询所有用户相关日志
    logs = await db.list_logs(target_type="user", limit=limit)

    # 解析每条日志，提取用户名和操作类型
    result = []
    for l in logs:
        msg = l["message"] or ""
        action = "other"
        action_desc = msg
        username = ""

        if "登录成功" in msg or "用户登录" in msg:
            action = "login"
            # 尝试从消息中提取用户名
            parts = msg.split(":")
            if len(parts) > 1:
                username = parts[-1].strip()
        elif "注册" in msg:
            action = "register"
            parts = msg.split(":")
            if len(parts) > 1:
                username = parts[-1].strip()
        elif "登出" in msg:
            action = "logout"
            parts = msg.split(":")
            if len(parts) > 1:
                username = parts[-1].strip()

        result.append({
            "log_id": l["log_id"],
            "action": action,
            "action_desc": action_desc,
            "username": username,
            "ip": l["ip"],
            "timestamp": l["created_at"],
            "level": l["level"],
        })

    return {"success": True, "data": result}


# ============================================================================
# 获取当前用户信息
# ============================================================================

@router.get("/me")
async def get_me(request: Request):
    """获取当前登录用户信息。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return {
        "success": True,
        "data": {
            "user_id": user["user_id"],
            "username": user["username"],
            "role": user["role"],
            "status": user["status"],
            "created_at": user["created_at"],
            "expire_at": user["expire_at"],
            "must_change_password": bool(user["must_change_password"]),
        },
    }


# ============================================================================
# 修改密码 (首次登录强制改密 / 主动改密)
# ============================================================================

@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request):
    """修改当前用户密码。

    适用场景:
        - 首次登录时 ``must_change_password=True`` 的用户被前端引导到此接口
          完成强制改密。
        - 已登录用户主动修改密码。

    流程:
        1. 要求用户已登录
        2. 验证当前密码正确
        3. 新密码不能与旧密码相同
        4. 更新密码哈希并清除 ``must_change_password`` 标记
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 1. 验证当前密码
    if not verify_password(req.old_password, user["password_hash"]):
        report_suspicious(f"change-password:{user['user_id']}")
        raise HTTPException(status_code=400, detail="当前密码错误")

    # 2. 新密码不能与旧密码相同
    if req.old_password == req.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    # 3. 更新密码并清除 must_change_password 标记
    await db.update_user_password(
        user["user_id"],
        hash_password(req.new_password),
        clear_must_change=True,
    )

    # 4. 记录日志
    client_ip = request.client.host if request.client else ""
    await db.add_log(
        target_type="user", target_id=user["user_id"],
        level="warn", message=f"用户修改密码: {user['username']}",
        ip=client_ip, created_by=user["user_id"],
    )

    return {"success": True, "message": "密码修改成功"}


# ============================================================================
# 用户管理（管理员）
# ============================================================================

@router.get("/users")
async def list_users(request: Request):
    """列出所有用户（管理员）。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()
    users = await db.list_users()
    return {
        "success": True,
        "data": [
            {
                "user_id": u["user_id"],
                "username": u["username"],
                "role": u["role"],
                "status": u["status"],
                "created_at": u["created_at"],
                "last_login_at": u["last_login_at"],
                "expire_at": u["expire_at"],
                "created_by": u["created_by"],
                "must_change_password": bool(u["must_change_password"]),
            }
            for u in users
        ],
    }


@router.post("/users")
async def create_user(req: CreateUserRequest, request: Request):
    """管理员创建用户。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 权限提升防护: 普通管理员不能创建超级管理员
    if req.role == "superadmin" and admin["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="无权创建超级管理员")
    # 角色校验
    if req.role not in ("user", "admin", "superadmin"):
        raise HTTPException(status_code=400, detail="无效的角色")

    existing = await db.get_user_by_username(req.username)
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")

    expire_at = None
    if req.duration_days:
        expire_at = time.time() + req.duration_days * 86400

    user_id = await db.create_user(
        username=req.username,
        password_hash=hash_password(req.password),
        role=req.role,
        created_by=admin["user_id"],
        expire_at=expire_at,
    )

    await db.add_log(
        target_type="user", target_id=user_id,
        level="success", message=f"管理员 {admin['username']} 创建用户: {req.username} (角色: {req.role})",
        created_by=admin["user_id"],
    )

    return {"success": True, "data": {"user_id": user_id}}


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, request: Request):
    """删除用户（仅超级管理员）。"""
    from .auth import require_superadmin
    admin = await require_superadmin(request)
    db = await get_db()

    target = await db.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["role"] == "superadmin":
        raise HTTPException(status_code=400, detail="不能删除超级管理员")

    await db.delete_user(user_id)
    await db.add_log(
        target_type="user", target_id=user_id,
        level="warn", message=f"管理员 {admin['username']} 删除用户: {target['username']}",
        created_by=admin["user_id"],
    )
    return {"success": True}


@router.put("/users/{user_id}/role")
async def update_user_role(user_id: str, role: str, request: Request):
    """修改用户角色（仅超级管理员）。"""
    from .auth import require_superadmin
    admin = await require_superadmin(request)
    db = await get_db()

    # 角色校验
    if role not in ("user", "admin", "superadmin"):
        raise HTTPException(status_code=400, detail="无效的角色")

    target = await db.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["role"] == "superadmin" and role != "superadmin":
        raise HTTPException(status_code=400, detail="不能降级超级管理员")

    await db.update_user_role(user_id, role)
    await db.add_log(
        target_type="user", target_id=user_id,
        level="warn", message=f"管理员 {admin['username']} 修改用户角色: {target['username']} -> {role}",
        created_by=admin["user_id"],
    )
    return {"success": True}


@router.put("/users/{user_id}/status")
async def update_user_status(user_id: str, status: str, request: Request):
    """修改用户状态（管理员）。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 状态校验
    if status not in ("active", "suspended", "banned"):
        raise HTTPException(status_code=400, detail="无效的状态")

    target = await db.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target["role"] == "superadmin" and admin["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="不能操作超级管理员")

    await db.update_user_status(user_id, status)
    await db.add_log(
        target_type="user", target_id=user_id,
        level="warn", message=f"管理员 {admin['username']} 修改用户状态: {target['username']} -> {status}",
        created_by=admin["user_id"],
    )
    return {"success": True}


# ============================================================================
# 替换卡密（管理员）
# ============================================================================

class ReplaceKeyRequest(BaseModel):
    old_key: str
    key_type: str = "register"
    duration_days: Optional[int] = None


@router.post("/cards/replace")
async def replace_card_key(req: ReplaceKeyRequest, request: Request):
    """替换卡密（管理员）。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    # 查找原卡密
    old_card = await db.get_card_by_key(req.old_key.upper())
    if not old_card:
        raise HTTPException(status_code=404, detail="原卡密不存在")

    # 创建新卡密
    new_card_id, new_key = await db.create_card_key(
        key_type=req.key_type,
        duration_days=req.duration_days,
        created_by=admin["user_id"],
    )

    # 将旧卡密标记为已替换（用 used 状态）
    await db.conn.execute(
        "UPDATE card_keys SET status = 'replaced' WHERE card_id = ?",
        (old_card["card_id"],)
    )
    await db.conn.commit()

    # 记录日志
    await db.add_log(
        target_type="system", target_id="cards",
        level="warn", message=f"卡密替换: {req.old_key} -> {new_key}",
        ip=request.client.host if request.client else "",
        created_by=admin["user_id"],
    )

    return {
        "success": True,
        "data": {
            "old_key": req.old_key,
            "new_key": new_key,
            "new_card_id": new_card_id,
        },
    }
