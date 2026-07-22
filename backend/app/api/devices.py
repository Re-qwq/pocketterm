"""PocketTerm 设备指纹管理 API

路由前缀: ``/api/devices``

提供以下端点:

设备指纹 CRUD:
    - ``GET    ""``                       列出所有设备指纹 (含统计)
    - ``POST   ""``                       生成新设备指纹
    - ``GET    "/stats"``                 获取指纹集合统计
    - ``GET    "/{device_id}"``           获取特定设备指纹
    - ``PUT    "/{device_id}"``           更新设备指纹
    - ``DELETE "/{device_id}"``          删除设备指纹
    - ``GET    "/by-account/{account_id}"``  按账号查询指纹
    - ``DELETE "/by-account/{account_id}"``  删除账号下所有指纹
    - ``POST   "/by-account/{account_id}/reset"``  重置账号指纹 (生成新设备)

设备指纹核心字段 (与 NovaBuilder / NexusE 的 ``uqholder.Player`` 对齐):

    - device_id            设备 ID (如 "amawufyaaxtu3ufq-d")
    - client_random_id     客户端随机 ID (int64)
    - uuid                 玩家 UUID
    - build_platform       平台编号 (0/1/2/7/11/...)
    - device_os            操作系统字符串
    - game_version         游戏版本
    - language_code        BCP-47 语言代码
    - current_input_mode   输入模式
    - default_input_mode   默认输入模式
    - ui_profile           UI 配置文件
    - is_editor_mode       是否编辑器模式
    - device_model         设备型号描述
    - account_id           所属账号 ID
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..auth.device_fingerprint import (
    BuildPlatform,
    DEFAULT_GAME_VERSION,
    DEFAULT_LANGUAGE_CODE,
    DeviceFingerprint,
    DeviceFingerprintManager,
    get_fingerprint_manager,
)
from ..logger import get_logger
from .deps import error_response, get_current_user, success_response

logger = get_logger("api.devices")

router = APIRouter(prefix="/api/devices", tags=["设备指纹管理"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class CreateDeviceRequest(BaseModel):
    """创建设备指纹请求体。"""

    account_id: str = Field("", description="账号 ID (用于多账号隔离; 可为空)")
    build_platform: Optional[int] = Field(
        None, description="指定平台编号 (1=Win32, 2=Mac, 7=Linux, 8=Android, 11=Win10)"
    )
    device_model: str = Field("", description="指定设备型号描述; 为空随机选取")
    game_version: str = Field("", description="游戏版本, 默认取配置")
    language_code: str = Field("", description="BCP-47 语言代码, 默认 zh_CN")


class UpdateDeviceRequest(BaseModel):
    """更新设备指纹请求体 (部分更新, 所有字段可选)。"""

    device_model: Optional[str] = Field(None, description="设备型号描述")
    build_platform: Optional[int] = Field(None, description="平台编号")
    device_os: Optional[str] = Field(None, description="操作系统字符串")
    game_version: Optional[str] = Field(None, description="游戏版本")
    language_code: Optional[str] = Field(None, description="语言代码")
    current_input_mode: Optional[int] = Field(None, description="当前输入模式")
    default_input_mode: Optional[int] = Field(None, description="默认输入模式")
    ui_profile: Optional[int] = Field(None, description="UI 配置文件 (0=Classic, 1=Pocket)")
    is_editor_mode: Optional[bool] = Field(None, description="是否编辑器模式")


def _manager() -> DeviceFingerprintManager:
    """获取全局设备指纹管理器 (快捷别名)。"""
    return get_fingerprint_manager()


def _fp_to_public_dict(fp: DeviceFingerprint) -> Dict[str, Any]:
    """将指纹转换为对外可读字典 (与 :meth:`DeviceFingerprint.to_dict` 等价)。"""
    return fp.to_dict()


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("")
async def list_devices(
    _user: Dict[str, Any] = Depends(get_current_user),
    account_id: str = Query("", description="按账号 ID 过滤"),
) -> Dict[str, Any]:
    """列出所有设备指纹。

    可通过 ``?account_id=xxx`` 过滤特定账号下的指纹。
    """
    mgr = _manager()
    if account_id:
        fp = mgr.get_by_account(account_id)
        devices = [_fp_to_public_dict(fp)] if fp else []
    else:
        devices = [_fp_to_public_dict(fp) for fp in mgr.list_all()]

    return success_response(
        data={"devices": devices, "total": len(devices)},
        message=f"共 {len(devices)} 个设备指纹",
    )


@router.get("/stats")
async def device_stats(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取设备指纹集合统计信息。"""
    mgr = _manager()
    stats = mgr.stats()
    return success_response(data=stats, message="设备指纹统计")


@router.post("")
async def create_device(
    body: CreateDeviceRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """生成新设备指纹。

    即使 ``account_id`` 已存在也会覆盖原指纹 (用于主动重置)。
    """
    mgr = _manager()
    try:
        fp = mgr.create(
            account_id=body.account_id,
            build_platform=body.build_platform,
            device_model=body.device_model or None,
            game_version=body.game_version or None,
            language_code=body.language_code or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("生成设备指纹失败")
        return error_response(error="create_failed", message=f"生成失败: {exc}")

    logger.info(f"API 创建设备指纹: {fp.device_id} (account={fp.account_id})")
    return success_response(
        data=_fp_to_public_dict(fp),
        message=f"设备指纹已生成 (device_id={fp.device_id})",
    )


@router.get("/{device_id}")
async def get_device(
    device_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取特定设备指纹详情。"""
    mgr = _manager()
    fp = mgr.get_by_device_id(device_id)
    if fp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"设备指纹不存在: {device_id}",
        )
    return success_response(data=_fp_to_public_dict(fp))


@router.put("/{device_id}")
async def update_device(
    device_id: str,
    body: UpdateDeviceRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """更新设备指纹字段 (部分更新)。"""
    mgr = _manager()
    updates: Dict[str, Any] = {}
    for field_name in (
        "device_model",
        "build_platform",
        "device_os",
        "game_version",
        "language_code",
        "current_input_mode",
        "default_input_mode",
        "ui_profile",
        "is_editor_mode",
    ):
        value = getattr(body, field_name, None)
        if value is not None:
            updates[field_name] = value

    if not updates:
        return error_response(error="no_updates", message="未提供任何更新字段")

    fp = mgr.update(device_id, updates)
    if fp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"设备指纹不存在: {device_id}",
        )

    logger.info(f"API 更新设备指纹: {device_id} fields={list(updates.keys())}")
    return success_response(
        data=_fp_to_public_dict(fp),
        message=f"设备指纹已更新 (fields={list(updates.keys())})",
    )


@router.delete("/{device_id}")
async def delete_device(
    device_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """删除特定设备指纹。"""
    mgr = _manager()
    ok = mgr.delete(device_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"设备指纹不存在: {device_id}",
        )
    logger.info(f"API 删除设备指纹: {device_id}")
    return success_response(message=f"设备指纹已删除 (device_id={device_id})")


@router.get("/by-account/{account_id}")
async def get_device_by_account(
    account_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """按账号 ID 查询设备指纹。"""
    mgr = _manager()
    fp = mgr.get_by_account(account_id)
    if fp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"账号 {account_id} 无设备指纹",
        )
    return success_response(data=_fp_to_public_dict(fp))


@router.delete("/by-account/{account_id}")
async def delete_device_by_account(
    account_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """删除账号下所有设备指纹。"""
    mgr = _manager()
    removed = mgr.delete_by_account(account_id)
    logger.info(f"API 删除账号 {account_id} 的 {removed} 条设备指纹")
    return success_response(
        data={"removed": removed},
        message=f"已删除 {removed} 条设备指纹",
    )


@router.post("/by-account/{account_id}/reset")
async def reset_device_by_account(
    account_id: str,
    # Bug 5.1 修复: 类型注解与默认值不匹配, None 不是 CreateDeviceRequest。
    # 改为 Optional[CreateDeviceRequest] = None。
    body: Optional[CreateDeviceRequest] = None,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """重置账号设备指纹 (删除旧指纹并生成新指纹)。

    通常在账号被封禁 / 怀疑指纹泄漏 / 主动换机时调用。
    """
    mgr = _manager()
    removed = mgr.delete_by_account(account_id)

    build_platform = body.build_platform if body else None
    device_model = body.device_model if body else ""
    game_version = body.game_version if body else ""
    language_code = body.language_code if body else ""

    try:
        fp = mgr.create(
            account_id=account_id,
            build_platform=build_platform,
            device_model=device_model or None,
            game_version=game_version or None,
            language_code=language_code or None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("重置设备指纹失败")
        return error_response(error="reset_failed", message=f"重置失败: {exc}")

    logger.info(
        f"API 重置账号 {account_id} 设备指纹: removed={removed} "
        f"new_device_id={fp.device_id}"
    )
    return success_response(
        data={
            "removed": removed,
            "fingerprint": _fp_to_public_dict(fp),
        },
        message=f"账号 {account_id} 设备指纹已重置 (新 device_id={fp.device_id})",
    )


__all__ = ["router"]
