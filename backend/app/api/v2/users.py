"""用户认证 API - 注册、登录、卡密验证、邮箱验证。"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
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

logger = logging.getLogger("pocketterm.api.users")

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
    email: str = Field("", description="邮箱 (可选，QQ邮箱: @qq.com / @foxmail.com)")
    email_code: str = Field("", description="邮箱验证码 (填写邮箱时必填)")


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
    email: str = Field("", description="邮箱 (可选，管理员创建无需验证)")


class SendEmailCodeRequest(BaseModel):
    email: str = Field(..., description="QQ邮箱 (@qq.com / @foxmail.com)")


class VerifyEmailCodeRequest(BaseModel):
    email: str = Field(..., description="邮箱")
    code: str = Field(..., description="验证码")


# ============================================================================
# 邮箱相关辅助函数
# ============================================================================

#: QQ 邮箱后缀
_QQ_EMAIL_SUFFIXES = ("@qq.com", "@foxmail.com")

#: 邮箱验证码有效期 (秒)
EMAIL_CODE_EXPIRE_SECONDS = 5 * 60


def _is_qq_email(email: str) -> bool:
    """判断是否为 QQ 邮箱 (@qq.com / @foxmail.com)。"""
    if not email:
        return False
    email_lower = email.strip().lower()
    return email_lower.endswith(_QQ_EMAIL_SUFFIXES)


def _extract_qq_number(email: str) -> Optional[str]:
    """从 QQ 邮箱中提取 QQ 号 (如 12345@qq.com -> 12345)。"""
    if not _is_qq_email(email):
        return None
    email_lower = email.strip().lower()
    for suffix in _QQ_EMAIL_SUFFIXES:
        if email_lower.endswith(suffix):
            number = email_lower[: -len(suffix)]
            if number.isdigit():
                return number
    return None


def _qq_avatar(email: str) -> str:
    """根据 QQ 邮箱生成 QQ 头像 URL。"""
    number = _extract_qq_number(email)
    if number:
        return f"https://q1.qlogo.cn/g?b=qq&nk={number}&s=100"
    return ""


def _send_email_resend(to_email: str, subject: str, body: str) -> bool:
    """通过 Resend HTTP API 发送邮件。

    适用于 Railway 等 PaaS 平台 (阻止出站 SMTP 端口)。
    需要环境变量 RESEND_API_KEY。
    发件人默认 onboarding@resend.dev (免费层) 或 RESEND_FROM。

    Resend 免费层: 3000 封/月, 无需域名验证即可使用 onboarding@resend.dev。
    """
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    if not api_key:
        return False

    sender = os.environ.get("RESEND_FROM", "PocketTerm <onboarding@resend.dev>").strip()

    try:
        import httpx

        # BUG 修复: 之前使用 httpx.post (同步), 会阻塞 asyncio 事件循环
        # 导致前端等待 10+ 秒。改为在单独线程中执行同步请求。
        # 同步 httpx.post 的超时设为 8 秒 (之前无超时, 默认 30 秒)
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [to_email],
                "subject": subject,
                "text": body,
            },
            timeout=8.0,  # 从 15 秒降到 8 秒
        )
        if resp.status_code in (200, 201):
            logger.info("Resend 邮件发送成功: %s", to_email)
            return True
        logger.error(
            "Resend 邮件发送失败: status=%s, body=%s",
            resp.status_code,
            resp.text[:200],
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("Resend 邮件发送异常 (%s): %s", to_email, exc)
        return False


def _send_email_smtp(to_email: str, subject: str, body: str) -> bool:
    """通过 SMTP 发送邮件 (本地开发或非 Railway 部署使用)。

    SMTP 配置从环境变量读取::

        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM

    注意: Railway 等 PaaS 平台会阻止出站 SMTP 端口 (25/465/587),
    请改用 _send_email_resend (Resend HTTP API)。
    """
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASS", "").strip()
    # SMTP_USER 未设置时回退到 SMTP_FROM (QQ 邮箱 SMTP 用户名即邮箱地址)
    if not user:
        user = os.environ.get("SMTP_FROM", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip() or user

    if not host or not user or not password:
        logger.warning(
            "SMTP 未配置 (SMTP_HOST/SMTP_USER/SMTP_PASS)，跳过邮件发送: %s",
            to_email,
        )
        return False

    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        # 465 -> SMTP_SSL，其他端口 -> SMTP + STARTTLS
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=15) as server:
                server.login(user, password)
                server.sendmail(sender, [to_email], msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls()
                server.login(user, password)
                server.sendmail(sender, [to_email], msg.as_string())
        logger.info("邮件发送成功: %s", to_email)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("邮件发送失败 (%s): %s", to_email, exc)
        return False


def _send_email(to_email: str, subject: str, body: str) -> bool:
    """发送邮件，优先使用 Resend HTTP API，回退到 SMTP。

    发送优先级:
        1. Resend HTTP API (RESEND_API_KEY) — 适用于 Railway 等阻止 SMTP 的平台
        2. SMTP (SMTP_HOST/SMTP_USER/SMTP_PASS) — 适用于本地或 VPS 部署
    """
    # 1. 尝试 Resend API
    if _send_email_resend(to_email, subject, body):
        return True
    # 2. 回退到 SMTP
    if _send_email_smtp(to_email, subject, body):
        return True
    return False


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

    # 3b. 邮箱验证 (可选)
    email = ""
    avatar = ""
    if req.email and req.email.strip():
        email = req.email.strip().lower()
        # 校验 QQ 邮箱格式
        if not _is_qq_email(email):
            raise HTTPException(
                status_code=400,
                detail="邮箱格式不正确，仅支持 @qq.com 或 @foxmail.com 邮箱",
            )
        # 检查邮箱是否已被注册
        email_row = await (await db.conn.execute(
            "SELECT user_id FROM users WHERE email = ? AND email != ''", (email,)
        )).fetchone()
        if email_row:
            raise HTTPException(status_code=400, detail="该邮箱已被注册")
        # 校验邮箱验证码
        if not req.email_code:
            raise HTTPException(status_code=400, detail="请填写邮箱验证码")
        code_row = await (await db.conn.execute(
            "SELECT * FROM email_verifications WHERE email = ? AND code = ? "
            "AND used = 0 AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (email, req.email_code.strip(), time.time()),
        )).fetchone()
        if code_row is None:
            raise HTTPException(status_code=400, detail="邮箱验证码错误或已过期")
        # 标记验证码已使用
        await db.conn.execute(
            "UPDATE email_verifications SET used = 1 WHERE id = ?",
            (code_row["id"],),
        )
        await db.conn.commit()
        # 生成 QQ 头像
        avatar = _qq_avatar(email)

    # 4. 计算过期时间
    expire_at = None
    if card["duration_days"]:
        expire_at = time.time() + card["duration_days"] * 86400

    # 5. 创建用户 (写入 email / avatar)
    user_id = await db.create_user(
        username=req.username,
        password_hash=hash_password(req.password),
        role="user",
        created_by=card["card_id"],
        expire_at=expire_at,
        email=email,
        avatar=avatar,
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
# 邮箱验证码
# ============================================================================

@router.post("/email/send")
async def send_email_code(req: SendEmailCodeRequest, request: Request):
    """发送邮箱验证码到 QQ 邮箱。

    流程:
        1. 校验邮箱格式
        2. 检查邮箱是否已被注册 (任务要求: 先检查)
        3. 频率限制 (IP 5次/分钟, 单邮箱 1次/分钟)
        4. 生成 6 位数字验证码
        5. 存入 email_verifications 表 (5 分钟有效期)
        6. 通过 SMTP 发送 (未配置 SMTP 时仅入库，便于开发)
    """
    client_ip = request.client.host if request.client else ""
    email = req.email.strip().lower()

    # 1. 校验邮箱格式
    if not _is_qq_email(email):
        raise HTTPException(
            status_code=400,
            detail="邮箱格式不正确，仅支持 @qq.com 或 @foxmail.com 邮箱",
        )

    db = await get_db()

    # 2. 检查邮箱是否已被注册 (任务要求: 先检查)
    email_row = await (await db.conn.execute(
        "SELECT user_id FROM users WHERE email = ? AND email != ''", (email,)
    )).fetchone()
    if email_row:
        raise HTTPException(status_code=400, detail="该邮箱已被注册")

    # 3. 频率限制 (IP 5次/分钟, 单邮箱 1次/分钟)
    if not rate_limiter.check(f"email_send_ip:{client_ip}", max_requests=5, window=60):
        raise HTTPException(status_code=429, detail="请求过于频繁，请1分钟后再试")
    if not rate_limiter.check(f"email_send_email:{email}", max_requests=1, window=60):
        raise HTTPException(status_code=429, detail="该邮箱请求过于频繁，请1分钟后再试")

    # 4. 生成 6 位数字验证码
    code = f"{secrets.randbelow(1000000):06d}"
    now = time.time()
    code_id = f"ev_{uuid.uuid4().hex[:12]}"

    # 5. 写入验证码表
    await db.conn.execute(
        """INSERT INTO email_verifications (id, email, code, expires_at, used, created_at)
           VALUES (?, ?, ?, ?, 0, ?)""",
        (code_id, email, code, now + EMAIL_CODE_EXPIRE_SECONDS, now),
    )
    await db.conn.commit()

    # 6. 发送邮件 (异步执行, 不阻塞响应)
    # BUG 修复: 之前同步发送邮件 (_send_email) 会阻塞整个事件循环
    # 导致前端等待 10+ 秒才收到 "已发送" 响应
    subject = "PocketTerm 邮箱验证码"
    body = (
        f"您的 PocketTerm 验证码是: {code}\n\n"
        f"验证码 5 分钟内有效，请勿泄露给他人。\n"
        f"如非本人操作，请忽略此邮件。"
    )

    # 先检查邮件配置是否可用
    has_resend = bool(os.environ.get("RESEND_API_KEY", "").strip())
    has_smtp = bool(
        os.environ.get("SMTP_HOST", "").strip()
        and os.environ.get("SMTP_USER", "").strip()
        and os.environ.get("SMTP_PASS", "").strip()
    )

    if not has_resend and not has_smtp:
        return {
            "success": True,
            "message": "验证码已生成 (邮件未配置, 验证码: " + code + ")",
        }

    # 在后台线程中发送邮件, 不阻塞响应
    import asyncio
    loop = asyncio.get_event_loop()
    # 使用 run_in_executor 在线程池中执行同步的 _send_email
    asyncio.ensure_future(
        loop.run_in_executor(None, _send_email, email, subject, body)
    )

    return {"success": True, "message": "验证码已发送"}


@router.post("/email/verify")
async def verify_email_code(req: VerifyEmailCodeRequest, request: Request):
    """验证邮箱验证码 (不标记为已使用，注册时才标记)。"""
    email = req.email.strip().lower()
    code = req.code.strip()

    db = await get_db()
    row = await (await db.conn.execute(
        "SELECT * FROM email_verifications WHERE email = ? AND code = ? "
        "AND used = 0 AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
        (email, code, time.time()),
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=400, detail="验证码错误或已过期")

    return {"success": True, "message": "验证码正确"}


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

    # 在校验密码前先检查账号状态，返回友好的中文提示
    if user["status"] == "banned":
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "您已被禁止登录，请联系管理员"},
        )
    if user["status"] == "suspended":
        return JSONResponse(
            status_code=403,
            content={"success": False, "message": "账号已被暂停，请联系管理员"},
        )
    if user["status"] != "active":
        raise HTTPException(status_code=403, detail=f"账号状态异常: {user['status']}")

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
    """查看用户活动日志（登录、注册、登出）。

    管理员可查看所有用户的活动日志; 普通用户只能查看自己的活动日志。
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 权限控制: 普通用户只能查看自己的活动日志, 管理员可查看所有
    target_id = None
    if user["role"] == "user":
        target_id = user["user_id"]

    # 查询用户相关日志
    logs = await db.list_logs(target_type="user", target_id=target_id, limit=limit)

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
            "balance": user.get("balance", 0),
            "email": user.get("email", ""),
            "avatar": user.get("avatar", ""),
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
                "balance": u["balance"] if "balance" in u.keys() else 0,
                "email": u["email"] if "email" in u.keys() else "",
                "avatar": u["avatar"] if "avatar" in u.keys() else "",
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

    # 管理员创建的用户无需邮箱验证，但仍可写入邮箱
    admin_email = req.email.strip().lower() if req.email else ""
    admin_avatar = _qq_avatar(admin_email) if admin_email else ""

    user_id = await db.create_user(
        username=req.username,
        password_hash=hash_password(req.password),
        role=req.role,
        created_by=admin["user_id"],
        expire_at=expire_at,
        email=admin_email,
        avatar=admin_avatar,
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
    # 自我保护：不能修改自己的状态
    if target["user_id"] == admin["user_id"]:
        raise HTTPException(status_code=400, detail="不能修改自己的状态")
    # 统一超级管理员保护：禁止对任何超级管理员修改状态
    # (与 delete_user / update_user_role 保持一致)
    if target["role"] == "superadmin":
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
