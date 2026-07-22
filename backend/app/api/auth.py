"""PocketTerm 认证 API

路由前缀: ``/api/auth``

提供以下端点:

    - ``GET  /check``           检查登录状态
    - ``POST /login``           登录（验证密码 + 设置 Cookie + 登录锁定保护）
    - ``POST /logout``          登出（清除 Cookie）
    - ``POST /change-password`` 修改密码

所有响应使用统一 JSON 格式（见 :mod:`app.api.deps`）。
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from ..auth.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    login_tracker,
    verify_token,
)
from ..config import get_config, needs_rehash
from ..logger import get_logger
from .deps import (
    ACCESS_COOKIE_NAME,
    COOKIE_MAX_AGE,
    error_response,
    get_current_user,
    success_response,
)

logger = get_logger("api.auth")

router = APIRouter(prefix="/api/auth", tags=["认证"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class LoginRequest(BaseModel):
    """登录请求体。"""

    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class ChangePasswordRequest(BaseModel):
    """修改密码请求体。"""

    old_password: str = Field(..., description="原密码")
    new_password: str = Field(..., min_length=6, description="新密码（至少 6 位）")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    """获取客户端真实 IP（支持反向代理转发）。

    安全说明: ``X-Forwarded-For`` 头可被客户端伪造。本工具主要面向
    开发/局域网场景，因此保留对转发头的支持；若部署在不可信网络或
    公网，应仅信任 ``request.client.host`` (直连 IP)，并在可信反向
    代理层覆写该头。使用时请知悉此风险。
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _set_auth_cookie(
    response: Response,
    token: str,
    # Bug 4.1 修复: 类型注解与默认值不匹配, None 不是 Request。
    # 改为 Optional[Request] = None。
    request: Optional[Request] = None,
) -> None:
    """在响应上设置认证 Cookie。

    Args:
        response: FastAPI ``Response`` 对象。
        token: 要写入 Cookie 的 JWT。
        request: FastAPI ``Request`` 对象（可选）。传入时会根据请求
            scheme 自动设置 ``secure`` 标志 (HTTPS -> True, HTTP -> False)，
            避免 JWT Cookie 在 HTTP 明文链路中传输。
    """
    # 仅当请求通过 HTTPS 到达时才启用 secure，避免在开发环境 (HTTP) 下
    # 浏览器拒绝写入 Cookie。生产环境应通过反向代理统一走 HTTPS。
    secure = (request.url.scheme == "https") if request else False
    response.set_cookie(
        key=ACCESS_COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _clear_auth_cookie(response: Response) -> None:
    """清除认证 Cookie。"""
    response.delete_cookie(key=ACCESS_COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/check")
async def check_auth(request: Request) -> Dict[str, Any]:
    """检查当前登录状态。

    不需要登录即可访问，用于前端判断是否已登录。
    """
    token = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        return success_response(
            data={"authenticated": False, "username": None},
            message="未登录",
        )

    config = get_config()
    payload = verify_token(token, config.jwt_secret)
    if payload is None:
        return success_response(
            data={"authenticated": False, "username": None},
            message="登录已过期",
        )

    username = payload.get("sub") or payload.get("username") or ""
    return success_response(
        data={"authenticated": True, "username": username},
        message="已登录",
    )


@router.post("/login")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    """登录。

    流程:
        1. 检查 IP 是否被登录锁定
        2. 验证用户名 + 密码
        3. 失败 -> 记录失败次数，达到阈值则锁定
        4. 成功 -> 清除失败记录，签发 JWT 并写入 Cookie

    Args:
        body: 登录表单 (username, password)。
        request: 用于读取客户端 IP。
        response: 用于设置 Cookie。
    """
    ip = _client_ip(request)
    config = get_config()

    # 1. 检查登录锁定
    locked, remaining = login_tracker.is_locked(ip)
    if locked:
        logger.warning(f"登录被拒绝: IP {ip} 处于锁定状态，剩余 {remaining:.0f}s")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"登录尝试过多，已锁定。请 {int(remaining)} 秒后重试。"
            ),
        )

    expected_username = config.username

    # 2. 验证用户名
    if body.username != expected_username:
        count, lock_seconds = login_tracker.record_failure(ip)
        attempts_left = max(config.max_login_attempts - count, 0)
        logger.warning(
            f"登录失败: 用户名错误 (输入={body.username}, IP={ip}, 次数={count})"
        )
        # 与密码错误时返回相同的响应结构 (含 attempts_left)，
        # 避免攻击者通过响应差异判断用户名是否存在。
        if lock_seconds > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"失败次数过多，账号已锁定 {int(lock_seconds)} 秒。"
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"用户名或密码错误，剩余尝试次数 {attempts_left}",
        )

    # 3. 验证密码
    if not config.check_password(body.password):
        count, lock_seconds = login_tracker.record_failure(ip)
        attempts_left = max(config.max_login_attempts - count, 0)
        logger.warning(
            f"登录失败: 密码错误 (用户={body.username}, IP={ip}, "
            f"次数={count}, 剩余尝试={attempts_left})"
        )
        if lock_seconds > 0:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"失败次数过多，账号已锁定 {int(lock_seconds)} 秒。"
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"用户名或密码错误，剩余尝试次数 {attempts_left}",
        )

    # 4. 登录成功
    login_tracker.record_success(ip)

    # 若密码哈希强度不足（旧 iterations），自动重新哈希
    if needs_rehash(config.password_hash):
        logger.info("检测到旧版密码哈希，自动升级...")
        # Bug 4.2 修复: 之前 set_password 若抛异常 (如文件写入失败), 会向上
        # 传播为 500 错误。但用户实际已登录成功 (record_success 已执行),
        # 收到错误响应会造成状态不一致。现包裹在独立 try-except 中, 失败时
        # 仅记录日志不中断登录流程。
        try:
            config.set_password(body.password)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"密码哈希升级失败 (不影响本次登录): {exc}")

    # 签发 JWT
    token = create_access_token(
        data={"sub": expected_username},
        secret_key=config.jwt_secret,
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    _set_auth_cookie(response, token, request)

    logger.info(f"用户 {expected_username} 登录成功 (IP={ip})")
    return success_response(
        data={"username": expected_username},
        message="登录成功",
    )


@router.post("/logout")
async def logout(response: Response) -> Dict[str, Any]:
    """登出，清除认证 Cookie。"""
    _clear_auth_cookie(response)
    logger.info("用户登出")
    return success_response(message="已登出")


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """修改密码。

    需要登录后才能访问。验证原密码后写入新密码并持久化配置。
    修改成功后会签发新的 JWT 并通过 Cookie 返回，同时覆盖旧 Cookie，
    使旧 token 在客户端不再可用（仍有效直至自然过期，但客户端无法读取）。
    """
    config = get_config()

    if not config.check_password(body.old_password):
        logger.warning(f"修改密码失败: 原密码错误 (用户={user.get('username')})")
        return error_response(
            error="invalid_password",
            message="原密码错误",
        )

    if body.old_password == body.new_password:
        return error_response(
            error="same_password",
            message="新密码不能与原密码相同",
        )

    config.set_password(body.new_password)
    config.save()
    logger.info(f"用户 {user.get('username')} 修改密码成功")

    # 签发新 token 并覆盖旧 Cookie，使客户端持有的旧 JWT 失效。
    # 注意: 旧 token 在服务端仍有效直至自然过期；如需立即失效需要引入
    # token 版本号机制 (config.token_version) 并在 verify_token 中校验。
    token = create_access_token(
        data={"sub": config.username},
        secret_key=config.jwt_secret,
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    # 先清除旧 Cookie 再设置新 Cookie，确保旧 token 被覆盖。
    _clear_auth_cookie(response)
    _set_auth_cookie(response, token, request)

    return success_response(message="密码修改成功，已自动续签登录态")


__all__ = ["router"]
