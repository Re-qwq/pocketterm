"""PocketTerm 系统 API

路由前缀: ``/api/system``

提供以下端点:

    - ``GET  "/stats"``             系统统计（CPU / 内存 / 机器人 / 接入点）
    - ``GET  "/version"``           版本信息
    - ``GET  "/plugins"``           已加载插件
    - ``GET  "/access-points"``     可用接入点
    - ``GET  "/import-settings"``   读取导入速度设置
    - ``POST "/import-settings"``  保存导入速度设置
    - ``POST "/restart"``           重启系统（清理缓存并重新加载配置）
"""
from __future__ import annotations

import asyncio
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, Literal

import psutil
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from ..access_point.manager import get_manager
from ..bot.manager import bot_manager
from ..config import DATA_DIR, PROJECT_ROOT, get_config
from ..logger import get_logger
from ..plugins.manager import plugin_manager as _real_plugin_manager
from .accounts import get_account_store
from .deps import error_response, get_current_user, success_response

logger = get_logger("api.system")

router = APIRouter(prefix="/api/system", tags=["系统"])

#: 应用版本号
APP_VERSION: str = "1.2"

#: 启动时间戳（模块导入即记录）
_START_TIME: float = time.time()

#: 导入速度设置文件路径: ``<DATA_DIR>/import_settings.json``
IMPORT_SETTINGS_FILE: Path = DATA_DIR / "import_settings.json"

#: 默认导入速度设置（文件不存在时使用）
# 注: 网易 3.8 阉割了 replaceitem, nbt_mode 默认 "structure" (平台模式)
DEFAULT_IMPORT_SETTINGS: Dict[str, Any] = {
    "import_engine": "phoenix",
    "speed_preset": "medium",
    "block_speed": 20,
    "command_speed": 10,
    "container_speed": 5,
    "nbt_delay": 0.5,
    "group_wait": 1.0,
    # 网易 3.8 阉割了 replaceitem, 默认 "structure" (平台模式)
    "nbt_mode": "structure",
    "nbt_auto_detect": True,
}


class ImportSettings(BaseModel):
    """导入速度设置请求体。

    .. important::

        **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
        不能放附魔/自定义名字), 因此 ``nbt_mode`` 默认值为 ``"structure"``
        (平台模式, 通过 structure save/load 搬运 NBT 方块)。

    字段说明::

        import_engine    导入引擎: phoenix / retalcer / auto
        speed_preset     速度预设: slow / medium / fast / turbo / custom
        block_speed      方块速度（块/秒），1 - 500
        command_speed    命令速度（条/秒），1 - 100
        container_speed  容器速度（项/秒），1 - 50
        nbt_delay        NBT 延迟（秒），0.01 - 10
        group_wait       分组等待时间（秒），0.1 - 30
        nbt_mode         NBT 放置模式: structure / replaceitem / auto
                         (默认 "structure", 网易 3.8 推荐方案)
        nbt_auto_detect  是否启用自动检测 (仅在 nbt_mode="auto" 时生效)
    """

    import_engine: Literal["phoenix", "retalcer", "auto"] = Field(
        "phoenix", description="导入引擎: phoenix/retalcer/auto"
    )
    speed_preset: Literal["slow", "medium", "fast", "turbo", "custom"] = Field(
        "medium", description="速度预设: slow/medium/fast/turbo/custom"
    )
    block_speed: int = Field(20, ge=1, le=500, description="方块速度（块/秒），1-500")
    command_speed: int = Field(
        10, ge=1, le=100, description="命令速度（条/秒），1-100"
    )
    container_speed: int = Field(
        5, ge=1, le=50, description="容器速度（项/秒），1-50"
    )
    nbt_delay: float = Field(0.5, ge=0.01, le=10, description="NBT 延迟（秒），0.01-10")
    group_wait: float = Field(
        1.0, ge=0.1, le=30, description="分组等待时间（秒），0.1-30"
    )
    # 网易 3.8 阉割了 replaceitem, 默认 "structure" (平台模式)
    nbt_mode: Literal["structure", "replaceitem", "auto"] = Field(
        "structure",
        description="NBT 放置模式: structure(默认,网易3.8推荐)/replaceitem(3.8风险)/auto",
    )
    nbt_auto_detect: bool = Field(
        True, description="是否启用自动检测 (仅在 nbt_mode=auto 时生效)"
    )


@router.get("/stats")
async def system_stats(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """系统统计信息。

    返回 CPU / 内存 / 磁盘使用率、运行时长、机器人数与账号数等。
    """
    try:
        # Bug 7.2 修复: psutil.cpu_percent(interval=0.1) 会阻塞 100ms,
        # psutil.disk_usage 也是同步 IO。在 async 端点中直接调用会阻塞
        # 事件循环, 影响并发性能。改用 asyncio.to_thread 在线程池中执行。
        vm = await asyncio.to_thread(psutil.virtual_memory)
        cpu_percent = await asyncio.to_thread(psutil.cpu_percent, interval=0.1)
        disk = await asyncio.to_thread(psutil.disk_usage, str(PROJECT_ROOT))
    except Exception as exc:  # noqa: BLE001
        logger.exception("采集系统指标失败")
        return error_response(error="stats_failed", message=f"采集失败: {exc}")

    bot_counts = bot_manager.get_status_counts()
    ap_counts = get_manager().get_status_counts()
    _accounts = get_account_store().list_all()
    account_count = len(_accounts)
    account_active = sum(1 for a in _accounts if a.get("status") == "active")
    account_banned = sum(1 for a in _accounts if a.get("status") == "banned")
    uptime = time.time() - _START_TIME

    data = {
        "server": {
            "uptime_seconds": round(uptime, 2),
            "started_at": _START_TIME,
        },
        "system": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "hostname": platform.node(),
            "cpu_percent": cpu_percent,
            "cpu_count": psutil.cpu_count() or 0,
            "memory": {
                "total": vm.total,
                "available": vm.available,
                "used": vm.used,
                "percent": vm.percent,
            },
            "disk": {
                "total": disk.total,
                "used": disk.used,
                "free": disk.free,
                "percent": disk.percent,
            },
        },
        "bots": {
            "total": bot_manager.bot_count,
            "status_counts": bot_counts,
        },
        "access_points": {
            "instance_count": get_manager().instance_count,
            "status_counts": ap_counts,
        },
        "accounts": {
            "total": account_count,
            "active": account_active,
            "banned": account_banned,
        },
    }
    return success_response(data=data, message="系统统计")


@router.get("/version")
async def version_info(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """版本信息。"""
    config = get_config()
    bot_cfg = config.bot
    data = {
        "version": APP_VERSION,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "game_version": bot_cfg.get("game_version", ""),
        "api_versions": {
            "fastapi": "0.115.0",
        },
    }
    return success_response(data=data, message="版本信息")


@router.get("/plugins")
async def loaded_plugins(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """已加载插件列表。"""
    _real_plugin_manager.discover_plugins()
    plugins = _real_plugin_manager.list_plugins()
    loaded = [p.to_dict() for p in plugins if p.is_loaded]
    data = {
        "loaded": loaded,
        "loaded_count": len(loaded),
        "total": len(plugins),
    }
    return success_response(
        data=data,
        message=f"已加载 {len(loaded)}/{len(plugins)} 个插件",
    )


@router.get("/access-points")
async def available_access_points(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """可用接入点列表。"""
    ap_manager = get_manager()
    available = ap_manager.list_available()
    recommended = ap_manager.auto_select()
    data = {
        "access_points": available,
        "recommended": recommended,
        "instance_count": ap_manager.instance_count,
    }
    return success_response(data=data, message="可用接入点")


@router.get("/import-settings")
async def get_import_settings(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """读取导入速度设置。

    若 ``data/import_settings.json`` 文件不存在，则返回默认设置。
    """
    try:
        if IMPORT_SETTINGS_FILE.exists():
            text = IMPORT_SETTINGS_FILE.read_text(encoding="utf-8")
            data = json.loads(text)
        else:
            data = dict(DEFAULT_IMPORT_SETTINGS)
    except Exception as exc:  # noqa: BLE001
        logger.exception("读取导入设置失败")
        return error_response(
            error="read_failed",
            message=f"读取导入设置失败: {exc}",
            data=dict(DEFAULT_IMPORT_SETTINGS),
        )
    return success_response(data=data, message="导入设置")


@router.post("/import-settings")
async def save_import_settings(
    body: ImportSettings,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """保存导入速度设置。

    请求体经 :class:`ImportSettings` 校验通过后，写入
    ``data/import_settings.json``。若目录不存在会自动创建。
    """
    try:
        settings_data = body.model_dump()
        IMPORT_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        IMPORT_SETTINGS_FILE.write_text(
            json.dumps(settings_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("保存导入设置失败")
        return error_response(error="save_failed", message=f"保存导入设置失败: {exc}")
    return success_response(data=settings_data, message="导入设置已保存")


@router.post("/restart")
async def restart_system(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """重启系统（实际上是清理缓存和重新加载配置）。

    该端点不会真正终止进程，而是执行以下操作:
        1. 重新加载 ``config.yaml`` 配置
        2. 刷新插件列表
        3. 返回成功响应

    前端 ``settings.js`` 中的 “重启服务” 按钮调用此接口。
    """
    try:
        # 重新加载配置
        try:
            get_config(reload=True)
            logger.info("系统重启: 配置已重新加载")
        except Exception:  # noqa: BLE001
            logger.exception("系统重启: 重新加载配置失败")

        # 刷新插件列表
        try:
            _real_plugin_manager.discover_plugins()
            logger.info("系统重启: 插件列表已刷新")
        except Exception:  # noqa: BLE001
            logger.exception("系统重启: 刷新插件列表失败")

        return success_response(
            data={"restarted_at": time.time()},
            message="系统已重启",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("系统重启失败")
        return error_response(
            error="restart_failed",
            message=f"系统重启失败: {exc}",
        )


__all__ = ["router", "APP_VERSION"]
