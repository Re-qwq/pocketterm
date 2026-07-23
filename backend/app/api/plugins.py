"""PocketTerm 插件管理 API

路由前缀: ``/api/plugins``

提供以下端点:

    - ``GET  ""``                       列出所有插件
    - ``POST "/{plugin_id}/load"``      加载插件
    - ``POST "/{plugin_id}/unload"``    卸载插件
    - ``POST "/{plugin_id}/reload"``    重载插件
    - ``POST "/{plugin_id}/enable"``    启用插件
    - ``POST "/{plugin_id}/disable"``   禁用插件
    - ``GET  "/{plugin_id}"``           插件详情

委托给真实的 ``app.plugins.manager.plugin_manager`` 单例,
支持 Python / Go / Java 三种语言的插件加载与运行。
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from ..logger import get_logger
from ..plugins.manager import plugin_manager
from .deps import error_response, get_current_user, success_response

logger = get_logger("api.plugins")

router = APIRouter(prefix="/api/plugins", tags=["插件管理"])


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.get("")
async def list_plugins(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """列出所有已发现的插件。"""
    # 确保最新发现
    plugin_manager.discover_plugins()
    plugins = plugin_manager.list_plugins()
    plugin_dicts = [p.to_dict() for p in plugins]
    loaded_count = sum(1 for p in plugins if p.is_loaded)
    enabled_count = sum(1 for p in plugins if plugin_manager.is_enabled(p.plugin_id))

    resp = success_response(
        data=plugin_dicts,
        message=f"共 {len(plugin_dicts)} 个插件，{loaded_count} 个已加载，{enabled_count} 个已启用",
    )
    resp["total"] = len(plugin_dicts)
    resp["loaded_count"] = loaded_count
    resp["enabled_count"] = enabled_count
    return resp


@router.post("/{plugin_id}/load")
async def load_plugin(
    plugin_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """加载插件（全局，不绑定机器人）。"""
    # 确保已发现
    # Bug 6.2 修复: 统一使用 is None 判断, 与 71 行风格一致。
    if plugin_manager.get_plugin_info(plugin_id) is None:
        plugin_manager.discover_plugins()

    info = plugin_manager.get_plugin_info(plugin_id)
    if info is None:
        return error_response(error="not_found", message=f"插件不存在: {plugin_id}")

    ok = await plugin_manager.load_plugin(plugin_id)
    if not ok:
        info = plugin_manager.get_plugin_info(plugin_id)
        # Bug 6.1 修复: 之前 info.error 为 None 时 err_msg 会变为 None,
        # 传给 error_response 会导致响应字段为 null。增加 info.error 真值检查。
        err_msg = (info.error if info and info.error else "加载失败")
        return error_response(error="load_failed", message=err_msg)

    # 自动启用
    plugin_manager.enable_plugin(plugin_id)
    return success_response(message=f"插件 {plugin_id} 已加载并启用")


@router.post("/{plugin_id}/unload")
async def unload_plugin(
    plugin_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """卸载插件。"""
    info = plugin_manager.get_plugin_info(plugin_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"插件不存在: {plugin_id}",
        )

    ok = await plugin_manager.unload_plugin(plugin_id)
    if not ok:
        return error_response(error="unload_failed", message=f"卸载失败: {plugin_id}")

    return success_response(message=f"插件 {plugin_id} 已卸载")


@router.post("/{plugin_id}/reload")
async def reload_plugin(
    plugin_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """重载插件（卸载后重新加载）。"""
    info = plugin_manager.get_plugin_info(plugin_id)
    if info is None:
        plugin_manager.discover_plugins()
        info = plugin_manager.get_plugin_info(plugin_id)
    if info is None:
        return error_response(error="not_found", message=f"插件不存在: {plugin_id}")

    ok = await plugin_manager.reload_plugin(plugin_id)
    if not ok:
        info = plugin_manager.get_plugin_info(plugin_id)
        # Bug 6.1 修复: 同 load_plugin, 增加 info.error 真值检查。
        err_msg = (info.error if info and info.error else "重载失败")
        return error_response(error="reload_failed", message=err_msg)

    return success_response(message=f"插件 {plugin_id} 已重载")


@router.post("/{plugin_id}/enable")
async def enable_plugin(
    plugin_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """启用插件（允许在机器人启动时自动加载）。"""
    # Bug 6.2 修复: 统一使用 is None 判断。
    if plugin_manager.get_plugin_info(plugin_id) is None:
        plugin_manager.discover_plugins()

    ok = plugin_manager.enable_plugin(plugin_id)
    if not ok:
        return error_response(error="not_found", message=f"插件不存在: {plugin_id}")

    return success_response(message=f"插件 {plugin_id} 已启用")


@router.post("/{plugin_id}/disable")
async def disable_plugin(
    plugin_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """禁用插件。"""
    ok = plugin_manager.disable_plugin(plugin_id)
    if not ok:
        return error_response(error="not_found", message=f"插件不存在: {plugin_id}")

    return success_response(message=f"插件 {plugin_id} 已禁用")


@router.get("/{plugin_id}")
async def get_plugin(
    plugin_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取插件详情。"""
    info = plugin_manager.get_plugin_info(plugin_id)
    if info is None:
        plugin_manager.discover_plugins()
        info = plugin_manager.get_plugin_info(plugin_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"插件不存在: {plugin_id}",
        )

    data = info.to_dict()
    data["enabled"] = plugin_manager.is_enabled(plugin_id)
    return success_response(data=data)


__all__ = ["router"]
