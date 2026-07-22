"""PocketTerm Cookie 池管理 API

路由前缀: ``/api/cookies``

提供针对网易 Minecraft 中国版 Cookie 账号池的管理接口:

    - ``GET    ""``                    获取 Cookie 池状态与列表
    - ``POST   "/add"``                批量添加 Cookie（文本或数组）
    - ``POST   "/validate"``           批量验证所有 Cookie
    - ``POST   "/{cookie_id}/validate"`` 验证单个 Cookie
    - ``DELETE "/{cookie_id}"``        删除单个 Cookie
    - ``DELETE ""``                    清空所有 Cookie

底层使用 :class:`app.auth.nemc_auth.cookie_pool.CookiePool` 进行异步管理，
数据持久化到 ``<DATA_DIR>/cookie_pool.json``。

响应格式说明:
    为了同时兼容前端（直接读取顶层字段）和统一响应规范，
    本模块的响应在保留 ``success`` / ``message`` 字段的同时，
    将业务字段平铺到顶层。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ..auth.nemc_auth.cookie_pool import (
    CookieEntry,
    CookiePool,
    CookieStatus,
)
from ..config import DATA_DIR
from ..logger import get_logger
from .deps import get_current_user

logger = get_logger("api.cookies")

router = APIRouter(prefix="/api/cookies", tags=["Cookie池管理"])

#: Cookie 池持久化文件路径: ``<DATA_DIR>/cookie_pool.json``
COOKIE_POOL_FILE: str = str(DATA_DIR / "cookie_pool.json")

#: 全局 Cookie 池实例（懒加载，整个应用共享）
_pool_instance: Optional[CookiePool] = None

#: 保护全局实例初始化的异步锁
_pool_init_lock: asyncio.Lock = asyncio.Lock()


def get_cookie_pool() -> CookiePool:
    """获取全局 Cookie 池实例（懒加载）。

    首次调用时创建 :class:`CookiePool` 实例并自动加载持久化文件。
    后续调用返回同一实例。

    Returns:
        全局共享的 :class:`CookiePool` 实例。
    """
    global _pool_instance
    if _pool_instance is None:
        # 确保数据目录存在
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(f"无法创建数据目录: {DATA_DIR}")
        _pool_instance = CookiePool(pool_file=COOKIE_POOL_FILE)
        logger.info(f"Cookie 池已初始化，持久化文件: {COOKIE_POOL_FILE}")
    return _pool_instance


# ---------------------------------------------------------------------------
# 适配层：补充 CookiePool 缺失的清空方法
# ---------------------------------------------------------------------------
async def clear_pool(pool: CookiePool) -> int:
    """清空 Cookie 池（适配层）。

    :class:`CookiePool` 本身没有提供 ``clear`` 方法，
    这里通过持有相同的内部锁来安全地清空内部列表，
    并立即持久化到磁盘。

    Args:
        pool: Cookie 池实例。

    Returns:
        被清除的 Cookie 数量。
    """
    async with pool._lock:  # type: ignore[attr-defined]  # 复用内部锁
        count: int = len(pool._cookies)  # type: ignore[attr-defined]
        pool._cookies.clear()  # type: ignore[attr-defined]
        pool._last_used_index = 0  # type: ignore[attr-defined]
    # 锁外执行同步 IO，避免长时间持锁
    try:
        pool.save_to_file()
    except Exception:  # noqa: BLE001
        logger.exception("清空 Cookie 池后保存失败")
    return count


def _entry_to_dict(entry: CookieEntry) -> Dict[str, Any]:
    """将 :class:`CookieEntry` 转换为 API 响应字典。

    Args:
        entry: Cookie 条目。

    Returns:
        包含 ``id`` / ``uid`` / ``status`` / ``cookie`` 等字段的字典。
    """
    status_value: str = (
        entry.status.value
        if isinstance(entry.status, CookieStatus)
        else str(entry.status or "unknown")
    )
    return {
        "id": entry.cookie_id,
        "cookie_id": entry.cookie_id,
        "uid": entry.uid,
        "pe_uid": entry.pe_uid,
        "status": status_value,
        "in_use": bool(entry.in_use),
        "last_checked": entry.last_validated,
        "last_validated": entry.last_validated,
        "last_error": entry.last_error,
        # CookieEntry 没有创建时间字段，使用最后验证时间或当前时间作为近似值
        "created_at": entry.last_validated or time.time(),
        # 前端 “复制” 操作需要原始 cookie 内容
        "cookie": entry.cookie,
        "content": entry.cookie,
    }


def _parse_cookie_input(cookies: Union[str, List[str]]) -> List[str]:
    """将 Cookie 输入（文本或数组）解析为 Cookie 字符串列表。

    支持两种输入:
        - 字符串: 一行一个 Cookie（兼容 ``\\r\\n`` / ``\\n``）
        - 字符串数组: 每个元素为一个 Cookie

    Args:
        cookies: Cookie 输入。

    Returns:
        去重并去除空白后的 Cookie 字符串列表。
    """
    if isinstance(cookies, str):
        lines = cookies.splitlines()
    elif isinstance(cookies, list):
        lines = []
        for item in cookies:
            if isinstance(item, str):
                # 兼容数组元素本身包含多行的情况
                lines.extend(item.splitlines())
            else:
                lines.append(str(item))
    else:
        lines = []

    result: List[str] = []
    seen: set = set()
    for line in lines:
        cookie = line.strip()
        if not cookie or cookie in seen:
            continue
        seen.add(cookie)
        result.append(cookie)
    return result


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class AddCookiesRequest(BaseModel):
    """批量添加 Cookie 请求体。

    ``cookies`` 字段同时支持两种格式:
        - 文本字符串: 一行一个 Cookie
        - 字符串数组: 每个元素为一个 Cookie

    这样既匹配前端实际发送的数组格式，也兼容任务规范中
    “一行一个 cookie 的文本” 描述。

    注: ``validate_now`` 字段在 JSON 中使用 ``validate`` 作为键名
    （通过 alias 映射），避免与 :class:`pydantic.BaseModel.validate`
    类方法同名产生告警。
    """

    model_config = ConfigDict(populate_by_name=True)

    cookies: Union[str, List[str]] = Field(
        ...,
        description="Cookie 字符串（一行一个）或字符串数组",
    )
    validate_now: bool = Field(
        False,
        alias="validate",
        description="添加后是否立即验证有效性",
    )


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("")
async def list_cookies(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取 Cookie 池状态和列表。

    返回顶层字段:
        - ``total`` / ``valid`` / ``invalid`` / ``in_use`` / ``unknown`` / ``available``
        - ``items`` / ``cookies``: Cookie 条目数组（两者为同一份数据，兼容不同前端字段名）
    """
    try:
        pool = get_cookie_pool()
        pool_status = await pool.get_status()
        entries = await pool.get_all_cookies()
        items: List[Dict[str, Any]] = [_entry_to_dict(e) for e in entries]

        return {
            "success": True,
            "message": f"共 {pool_status.total} 个 Cookie",
            "total": pool_status.total,
            "valid": pool_status.valid,
            "invalid": pool_status.invalid,
            "in_use": pool_status.in_use,
            "unknown": pool_status.unknown,
            "available": pool_status.available,
            "items": items,
            "cookies": items,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取 Cookie 池列表失败")
        return {
            "success": False,
            "message": f"获取 Cookie 池列表失败: {exc}",
            "error": "list_failed",
            "total": 0,
            "valid": 0,
            "invalid": 0,
            "in_use": 0,
            "items": [],
            "cookies": [],
        }


@router.post("/add")
async def add_cookies(
    body: AddCookiesRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """批量添加 Cookie。

    请求体:
        - ``cookies``: Cookie 字符串（一行一个）或字符串数组
        - ``validate``: 是否在添加后立即验证（默认 False）

    返回:
        ``{"success": True, "added": N, "duplicates": N, ...}``
    """
    cookie_list = _parse_cookie_input(body.cookies)
    if not cookie_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="未识别到有效的 Cookie",
        )

    pool = get_cookie_pool()
    added: int = 0
    duplicates: int = 0
    added_entries: List[Dict[str, Any]] = []

    try:
        for cookie in cookie_list:
            # 记录添加前的数量来判断是否为新添加
            before_count = len(await pool.get_all_cookies())
            entry = await pool.add_cookie(cookie)
            after_count = len(await pool.get_all_cookies())
            if after_count > before_count:
                added += 1
                added_entries.append(_entry_to_dict(entry))
            else:
                duplicates += 1

        # 持久化
        await pool.save()

        # 可选：立即验证
        validation_result: Optional[Dict[str, int]] = None
        if body.validate_now and added > 0:
            validation_result = await pool.validate_all()
            await pool.save()

        result: Dict[str, Any] = {
            "success": True,
            "message": f"成功添加 {added} 个 Cookie（重复 {duplicates} 个）",
            "added": added,
            "duplicates": duplicates,
            "total_input": len(cookie_list),
            "items": added_entries,
        }
        if validation_result is not None:
            result["validation"] = validation_result
        return result
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("批量添加 Cookie 失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"批量添加 Cookie 失败: {exc}",
        )


@router.post("/validate")
async def validate_all_cookies(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """批量验证所有 Cookie。

    返回:
        ``{"success": True, "valid": N, "invalid": N, "total": N}``
    """
    try:
        pool = get_cookie_pool()
        result = await pool.validate_all()
        await pool.save()
        return {
            "success": True,
            "message": (
                f"验证完成: {result.get('valid', 0)} 个有效, "
                f"{result.get('invalid', 0)} 个失效"
            ),
            "valid": result.get("valid", 0),
            "invalid": result.get("invalid", 0),
            "total": result.get("total", 0),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("批量验证 Cookie 失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"批量验证 Cookie 失败: {exc}",
        )


@router.post("/{cookie_id}/validate")
async def validate_cookie(
    cookie_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """验证单个 Cookie。

    Path 参数:
        cookie_id: Cookie 条目 ID（SHA-256 前 16 位）

    返回:
        ``{"success": True, "status": "valid" | "invalid", "entry": {...}}``
    """
    try:
        pool = get_cookie_pool()
        entry = await pool.get_cookie(cookie_id)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cookie 不存在: {cookie_id}",
            )

        # 调用 validate_cookie 会重新验证并更新状态
        updated = await pool.validate_cookie(entry.cookie)
        await pool.save()

        status_value = (
            updated.status.value
            if isinstance(updated.status, CookieStatus)
            else str(updated.status)
        )
        return {
            "success": True,
            "message": f"验证完成: {status_value}",
            "status": status_value,
            "valid": updated.status == CookieStatus.VALID,
            "entry": _entry_to_dict(updated),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"验证 Cookie 失败: {cookie_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"验证 Cookie 失败: {exc}",
        )


@router.delete("/{cookie_id}")
async def delete_cookie(
    cookie_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """删除单个 Cookie。

    Path 参数:
        cookie_id: Cookie 条目 ID
    """
    try:
        pool = get_cookie_pool()
        removed = await pool.remove_cookie(cookie_id)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Cookie 不存在: {cookie_id}",
            )
        await pool.save()
        return {
            "success": True,
            "message": f"Cookie 已删除: {cookie_id}",
            "cookie_id": cookie_id,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"删除 Cookie 失败: {cookie_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除 Cookie 失败: {exc}",
        )


@router.delete("")
async def clear_all_cookies(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """清空所有 Cookie。

    前端 ``settings.js`` 中的 “清空 Cookie 池” 按钮调用此接口。
    """
    try:
        pool = get_cookie_pool()
        removed = await clear_pool(pool)
        return {
            "success": True,
            "message": f"已清空 {removed} 个 Cookie",
            "removed": removed,
            "total": 0,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("清空 Cookie 池失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"清空 Cookie 池失败: {exc}",
        )


__all__ = ["router", "get_cookie_pool"]
