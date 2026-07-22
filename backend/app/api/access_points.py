"""PocketTerm 接入点管理 API

路由前缀: ``/api/access-points``

提供以下端点:

    - ``GET  ""``                      列出可用接入点
    - ``POST "/{name}/download"``      下载接入点二进制
    - ``GET  "/{name}/status"``        接入点状态
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..access_point.manager import AP_DISPLAY_NAMES, get_manager
from ..logger import get_logger
from .deps import error_response, get_current_user, success_response

logger = get_logger("api.access_points")

router = APIRouter(prefix="/api/access-points", tags=["接入点管理"])


@router.get("")
async def list_access_points(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """列出可用接入点及推荐项。"""
    ap_manager = get_manager()
    available = ap_manager.list_available()
    recommended = ap_manager.auto_select()
    data = {
        "access_points": available,
        "recommended": recommended,
        "display_names": AP_DISPLAY_NAMES,
        "instance_count": ap_manager.instance_count,
        "instances": ap_manager.list_instances(),
    }
    return success_response(
        data=data,
        message=f"共 {len(available)} 个接入点",
    )


@router.post("/{name}/download")
async def download_access_point(
    name: str,
    version: str = Query("", description="指定版本号，为空则使用默认版本"),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """下载指定接入点的二进制文件。

    目前支持 NeOmega 与 FateArk；自建接入点（custom）无需下载。
    """
    ap_manager = get_manager()
    try:
        path = await ap_manager.download(name, version)
    except ValueError as exc:
        # 不支持的接入点类型 / 自建接入点无需下载
        return error_response(error="unsupported_type", message=str(exc))
    except RuntimeError as exc:
        # binary_dir 未设置等
        return error_response(error="download_failed", message=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"下载接入点 {name} 失败")
        return error_response(
            error="download_failed",
            message=f"下载失败: {exc}",
        )

    return success_response(
        data={
            "name": name.lower(),
            "display_name": AP_DISPLAY_NAMES.get(name.lower(), name),
            "version": version or "default",
            "binary_path": str(path),
        },
        message=f"接入点 {name} 下载完成",
    )


@router.get("/{name}/status")
async def access_point_status(
    name: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取指定接入点的状态信息。

    包括是否可用、二进制路径、该类型的运行实例数等。
    """
    ap_manager = get_manager()
    name_lower = name.lower()
    available = ap_manager.list_available()

    target = next((ap for ap in available if ap.get("type") == name_lower), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未知的接入点类型: {name}",
        )

    # 统计该类型正在运行的实例数
    instances = ap_manager.list_instances()
    type_instances = [
        inst for inst in instances if inst.get("ap_type") == name_lower
    ]

    data = {
        "name": name_lower,
        "display_name": AP_DISPLAY_NAMES.get(name_lower, name),
        "available": target.get("available", False),
        "binary_path": target.get("binary_path", ""),
        "is_custom": target.get("is_custom", False),
        "instance_count": len(type_instances),
        "instances": type_instances,
    }
    return success_response(data=data, message="接入点状态")


__all__ = ["router"]
