"""PocketTerm 系统设置 API

路由前缀: ``/api/settings``

提供系统级配置的读取与保存:

    - ``GET  ""``   获取系统设置
    - ``POST ""``   保存系统设置

设置持久化到 ``<DATA_DIR>/settings.json``。

设置结构::

    {
        "default_version": "3.8",
        "default_platform": "pc",
        "pc_auth_server": "https://x19obtcore.nie.netease.com:8443",
        "pe_auth_server": "https://g79obtapigtcoregray.minecraft.cn",
        "api_server": "https://x19apigatewayobt.nie.netease.com",
        "lobby_server": "https://g79mclobt.minecraft.cn",
        "refresh_interval": 30,
        "max_bots": 10,
        "auto_reconnect": true,
        "import_defaults": {
            "algorithm": "auto",
            "chunk_size": 1,
            "import_nbt": true,
            "import_command_blocks": true,
            "command_block_rate": 10,
            "block_rate": 20,
            "patch_mode": false
        }
    }

响应格式说明:
    为了同时兼容前端（直接读取顶层字段）和统一响应规范，
    本模块的 GET 响应在保留 ``success`` / ``message`` 字段的同时，
    将所有设置字段平铺到顶层。
"""
from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..config import DATA_DIR, get_config
from ..logger import get_logger
from .deps import get_current_user

logger = get_logger("api.settings")

router = APIRouter(prefix="/api/settings", tags=["系统设置"])

#: 设置持久化文件路径: ``<DATA_DIR>/settings.json``
SETTINGS_FILE: Path = DATA_DIR / "settings.json"

#: 模块加载时间戳（用于计算运行时长）
_START_TIME: float = time.time()


#: 默认设置（文件不存在或字段缺失时使用）
DEFAULT_SETTINGS: Dict[str, Any] = {
    "default_version": "3.8",
    "default_platform": "pc",
    "pc_auth_server": "https://x19obtcore.nie.netease.com:8443",
    "pe_auth_server": "https://g79obtapigtcoregray.minecraft.cn",
    "api_server": "https://x19apigatewayobt.nie.netease.com",
    "lobby_server": "https://g79mclobt.minecraft.cn",
    "refresh_interval": 30,
    "max_bots": 10,
    "auto_reconnect": True,
    "import_defaults": {
        "algorithm": "auto",
        "chunk_size": 1,
        "import_nbt": True,
        "import_command_blocks": True,
        "command_block_rate": 10,
        "block_rate": 20,
        "patch_mode": False,
    },
    # 兼容前端 settings.js 使用的字段（默认值）
    "default_port": 19132,
    "api_url": "",
    "ws_url": "",
    "api_token": "",
    "default_algorithm": "chunk_fill",
    "chunk_size": 32,
    "block_speed": 20,
    "command_speed": 10,
    "nbt_delay": 0.5,
    "import_nbt": True,
    "import_command_block": True,
}


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ImportDefaultsModel(BaseModel):
    """导入默认配置。"""

    algorithm: str = Field("auto", description="默认导入算法")
    chunk_size: int = Field(1, ge=1, description="默认区块大小")
    import_nbt: bool = Field(True, description="默认是否导入 NBT")
    import_command_blocks: bool = Field(True, description="默认是否导入命令方块")
    command_block_rate: int = Field(10, ge=1, description="命令方块速率")
    block_rate: int = Field(20, ge=1, description="方块速率")
    patch_mode: bool = Field(False, description="补丁模式")


class SettingsModel(BaseModel):
    """系统设置请求体。

    所有字段均为可选，仅更新提供的字段。
    同时兼容任务规范字段和前端 ``settings.js`` 使用的字段。
    """

    # 任务规范字段
    default_version: Optional[str] = Field(None, description="默认协议版本")
    default_platform: Optional[str] = Field(None, description="默认平台 (pc/pe)")
    pc_auth_server: Optional[str] = Field(None, description="PC 认证服务器")
    pe_auth_server: Optional[str] = Field(None, description="PE 认证服务器")
    api_server: Optional[str] = Field(None, description="API 服务器")
    lobby_server: Optional[str] = Field(None, description="大厅服务器")
    refresh_interval: Optional[int] = Field(None, ge=1, le=3600, description="刷新间隔（秒）")
    max_bots: Optional[int] = Field(None, ge=1, le=100, description="最大机器人数")
    auto_reconnect: Optional[bool] = Field(None, description="是否自动重连")
    import_defaults: Optional[ImportDefaultsModel] = Field(None, description="导入默认配置")

    # 前端 settings.js 使用的字段
    default_port: Optional[int] = Field(None, ge=1, le=65535, description="默认端口")
    api_url: Optional[str] = Field(None, description="API 地址")
    ws_url: Optional[str] = Field(None, description="WebSocket 地址")
    api_token: Optional[str] = Field(None, description="认证 Token")
    default_algorithm: Optional[str] = Field(None, description="默认导入算法")
    chunk_size: Optional[int] = Field(None, ge=1, description="区块大小")
    block_speed: Optional[int] = Field(None, ge=1, le=500, description="方块速度")
    command_speed: Optional[int] = Field(None, ge=1, le=100, description="命令速度")
    nbt_delay: Optional[float] = Field(None, ge=0.01, le=10, description="NBT 延迟")
    import_nbt: Optional[bool] = Field(None, description="是否导入 NBT")
    import_command_block: Optional[bool] = Field(None, description="是否导入命令方块")

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# 持久化辅助
# ---------------------------------------------------------------------------
def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并 ``override`` 到 ``base``，返回新的字典。"""
    import copy

    result: Dict[str, Any] = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _load_settings() -> Dict[str, Any]:
    """从磁盘加载设置，缺失时使用默认值。"""
    if not SETTINGS_FILE.exists():
        return dict(DEFAULT_SETTINGS)
    try:
        text = SETTINGS_FILE.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            logger.warning("settings.json 格式不正确，使用默认设置")
            return dict(DEFAULT_SETTINGS)
        # 与默认值合并，确保新字段存在
        return _deep_merge(DEFAULT_SETTINGS, data)
    except Exception:  # noqa: BLE001
        logger.exception("读取系统设置失败，使用默认设置")
        return dict(DEFAULT_SETTINGS)


def _save_settings(settings: Dict[str, Any]) -> None:
    """将设置写入磁盘。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.exception("保存系统设置失败")
        raise


def _build_system_info() -> Dict[str, Any]:
    """构造系统信息（用于前端 settings.js 的 “系统信息” 卡片）。"""
    config = get_config()
    return {
        "backend_version": "1.0.0",
        "uptime": _START_TIME,
        "os": platform.platform(),
        "python": sys.version.split()[0],
        "hostname": platform.node(),
    }


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("")
async def get_settings(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取系统设置。

    返回顶层字段:
        - 所有设置字段（平铺到顶层）
        - ``system``: 系统信息子对象
        - ``success`` / ``message``: 统一响应字段
    """
    try:
        settings = _load_settings()
        # 加入系统信息
        response: Dict[str, Any] = {
            "success": True,
            "message": "系统设置",
            "system": _build_system_info(),
        }
        response.update(settings)
        return response
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取系统设置失败")
        # 失败时返回默认设置，保证前端可正常显示
        response: Dict[str, Any] = {
            "success": False,
            "message": f"获取系统设置失败: {exc}",
            "error": "read_failed",
            "system": _build_system_info(),
        }
        response.update(DEFAULT_SETTINGS)
        return response


@router.post("")
async def save_settings(
    body: SettingsModel,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """保存系统设置。

    请求体字段均为可选，仅更新提供的字段。
    返回更新后的完整设置。
    """
    try:
        # 读取现有设置
        current = _load_settings()
        # 提取请求体中的非 None 字段
        updates: Dict[str, Any] = body.model_dump(exclude_none=True)
        # 特殊处理 import_defaults（嵌套对象）
        if body.import_defaults is not None:
            updates["import_defaults"] = body.import_defaults.model_dump()

        # 合并更新
        new_settings = _deep_merge(current, updates)
        # 持久化
        _save_settings(new_settings)

        logger.info(
            f"系统设置已更新: {len(updates)} 个字段 "
            f"(用户={_user.get('username', 'unknown')})"
        )

        # 构造响应（平铺到顶层）
        response: Dict[str, Any] = {
            "success": True,
            "message": "设置已保存",
            "system": _build_system_info(),
        }
        response.update(new_settings)
        return response
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("保存系统设置失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"保存系统设置失败: {exc}",
        )


__all__ = ["router", "DEFAULT_SETTINGS", "SETTINGS_FILE"]
