"""PocketTerm API - 共享依赖与工具函数

本模块提供 API 路由共享的:

    - :func:`get_current_user`  认证依赖（从 Cookie 解析 JWT）
    - :func:`require_user`      FastAPI 依赖注入入口
    - :func:`success_response`  统一成功响应
    - :func:`error_response`    统一错误响应

所有 API 路由均使用统一的 JSON 响应格式::

    {
        "success": true | false,
        "message": "...",
        "data": ...          # success=True 时存在
        "error": "..."       # success=False 时存在
    }
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import Depends, HTTPException, Request, status

from ..auth.security import verify_token
from ..config import get_config
from ..logger import get_logger

logger = get_logger("api.deps")

#: 存放 JWT 的 Cookie 名称
ACCESS_COOKIE_NAME: str = "pocketterm_token"

#: Cookie 属性（HttpOnly + SameSite=Lax，过期时间由 token 本身控制）
COOKIE_MAX_AGE: int = 60 * 60 * 24  # 24 小时，与 ACCESS_TOKEN_EXPIRE_MINUTES 对齐


# ---------------------------------------------------------------------------
# 认证依赖
# ---------------------------------------------------------------------------
def get_current_user(request: Request) -> Dict[str, Any]:
    """从请求 Cookie 中解析 JWT 并返回当前用户信息。

    作为 FastAPI 依赖注入使用::

        @router.get("/me")
        async def me(user: dict = Depends(get_current_user)):
            return success_response(data=user)

    Args:
        request: FastAPI ``Request`` 对象（用于读取 cookies）。

    Returns:
        用户信息字典，至少包含::

            {"username": "admin", "sub": "admin", "exp": <unix>}

    Raises:
        HTTPException: 401 未认证（Cookie 缺失 / token 无效 / 已过期）。
    """
    token: Optional[str] = request.cookies.get(ACCESS_COOKIE_NAME)
    if not token:
        logger.warning("认证失败: Cookie 中未找到 token")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未登录或会话已过期",
        )

    config = get_config()
    payload = verify_token(token, config.jwt_secret)
    if payload is None:
        logger.warning("认证失败: JWT 验证失败或已过期")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="登录已过期，请重新登录",
        )

    # 兼容老 payload：sub 字段即用户名
    username = payload.get("sub") or payload.get("username") or ""
    if not username:
        logger.warning("认证失败: payload 中缺少用户标识")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的登录凭证",
        )

    return {
        "username": username,
        "sub": username,
        "exp": payload.get("exp"),
    }


#: FastAPI 依赖快捷别名
require_user = Depends(get_current_user)


# ---------------------------------------------------------------------------
# 统一响应构造工具
# ---------------------------------------------------------------------------
def success_response(
    data: Any = None,
    message: str = "操作成功",
) -> Dict[str, Any]:
    """构造统一的成功响应字典。

    Args:
        data: 业务数据。
        message: 提示信息。

    Returns:
        ``{"success": True, "message": message, "data": data}``
    """
    return {"success": True, "message": message, "data": data}


def error_response(
    error: str,
    message: str = "操作失败",
    data: Any = None,
) -> Dict[str, Any]:
    """构造统一的错误响应字典。

    Args:
        error: 错误标识 / 错误信息。
        message: 面向用户的提示。
        data: 附加数据（可选）。

    Returns:
        ``{"success": False, "message": message, "error": error, "data": data}``
    """
    resp: Dict[str, Any] = {"success": False, "message": message, "error": error}
    if data is not None:
        resp["data"] = data
    return resp


__all__ = [
    "ACCESS_COOKIE_NAME",
    "COOKIE_MAX_AGE",
    "get_current_user",
    "require_user",
    "success_response",
    "error_response",
]
