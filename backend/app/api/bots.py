"""PocketTerm 机器人管理 API

路由前缀: ``/api/bots``

提供以下端点:

    - ``GET  ""``                  列出所有机器人
    - ``POST ""``                  创建机器人
    - ``POST "/stop-all"``         停止所有机器人 (必须在 /{bot_id} 路由前注册)
    - ``GET  "/{bot_id}"``         获取机器人详情
    - ``POST "/{bot_id}/start"``   启动机器人
    - ``POST "/{bot_id}/stop"``    停止机器人
    - ``DELETE "/{bot_id}"``       删除机器人
    - ``POST "/{bot_id}/command"`` 发送命令
    - ``POST "/{bot_id}/chat"``    发送聊天
    - ``GET  "/{bot_id}/logs"``    获取日志
    - ``GET  "/{bot_id}/chat"``    获取聊天历史
    - ``GET  "/{bot_id}/inventory"`` 获取背包
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..bot.manager import bot_manager
from ..bot.models import (
    AccessPointType,
    BotConfig,
    BotStatus,
    ServerType,
)
from ..config import get_config
from ..logger import get_logger
from .deps import error_response, get_current_user, success_response

logger = get_logger("api.bots")

router = APIRouter(prefix="/api/bots", tags=["机器人管理"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class CreateBotRequest(BaseModel):
    """创建机器人请求体。

    支持 server_type: rental/lobby/local/custom
    支持 access_point_type: neomega/fateark/custom
    account_id 可选。
    """

    name: str = Field("", description="机器人名称（为空自动生成）")
    server_code: str = Field("", description="租赁服号 / 房间号")
    server_password: str = Field("", description="服务器密码")
    server_type: ServerType = Field(ServerType.RENTAL, description="服务器类型")
    server_address: str = Field("", description="自定义服务器地址（custom 时使用）")
    server_port: int = Field(19132, description="服务器端口")
    auth_server: str = Field("", description="认证服务器 URL")
    api_key: str = Field("", description="认证服务器 API Key")
    cookie: str = Field("", description="网易登录 Cookie（cookie 认证模式使用）")
    sauth_json: str = Field("", description="sauth 认证 JSON（direct/fatalder 模式使用）")
    auth_method: str = Field(
        "auto", description="认证方式: auto / direct / fatalder / cookie / fbauth"
    )
    device_model: str = Field("", description="设备型号")
    access_point_type: AccessPointType = Field(
        AccessPointType.NEOMEGA, description="接入点类型"
    )
    auto_reconnect: bool = Field(True, description="断开后是否自动重连")
    max_reconnect_attempts: int = Field(3, description="最大重连次数")
    reconnect_delay: int = Field(30, description="重连基础延迟（秒）")
    account_id: str = Field("", description="账号 ID（可选，用于多账号管理）")


class CommandRequest(BaseModel):
    """发送命令请求体。"""

    command: str = Field(..., description="命令字符串（不含前导 /）")


class ChatRequest(BaseModel):
    """发送聊天请求体。"""

    message: str = Field(..., description="聊天消息内容")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _require_bot(bot_id: str) -> Any:
    """根据 bot_id 获取机器人实例，不存在则抛 404。"""
    bot = bot_manager.get_bot(bot_id)
    if bot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"机器人不存在: {bot_id}",
        )
    return bot


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("")
async def list_bots(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """列出所有机器人。"""
    bots: List[Dict[str, Any]] = bot_manager.list_bots()
    counts = bot_manager.get_status_counts()
    return success_response(
        data={"bots": bots, "total": len(bots), "status_counts": counts},
        message=f"共 {len(bots)} 个机器人",
    )


@router.post("")
async def create_bot(
    body: CreateBotRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """创建机器人。

    根据 server_type / access_point_type 构造 :class:`BotConfig` 并交由
    :data:`bot_manager` 创建。创建后处于 IDLE 状态，需调用 ``/{bot_id}/start``。
    """
    config = get_config()
    bot_cfg_section = config.bot

    # 用请求体覆盖默认值，缺省则取 config.yaml 中的配置
    device_model = body.device_model or bot_cfg_section.get("device_model", "Xiaomi 13")
    auth_server = body.auth_server or config.auth_server_url

    # 如果提供了 account_id 但没有 sauth_json，自动从账号管理中加载
    sauth_json = body.sauth_json
    cookie = body.cookie
    if body.account_id and not sauth_json:
        try:
            from .accounts import get_account_store
            store = get_account_store()
            account = store.get(body.account_id)  # 返回 dict
            if account:
                # account["sauth_json"] 可能是嵌套结构 {"sauth_json": "..."}
                raw = account.get("sauth_json", "")
                if isinstance(raw, str):
                    try:
                        import json as _json
                        parsed = _json.loads(raw)
                        if isinstance(parsed, dict) and "sauth_json" in parsed:
                            sauth_json = parsed["sauth_json"]
                        else:
                            sauth_json = raw
                    except Exception:
                        sauth_json = raw
                elif isinstance(raw, dict) and "sauth_json" in raw:
                    sauth_json = raw["sauth_json"]
                else:
                    sauth_json = str(raw) if raw else ""
                logger.info(f"从账号 {body.account_id} 自动加载 sauth_json ({len(sauth_json)} 字符)")
            else:
                logger.warning(f"账号 {body.account_id} 不存在，无法自动加载 sauth_json")
        except Exception as exc:
            logger.warning(f"加载账号 {body.account_id} 的 sauth_json 失败: {exc}")

    bot_config = BotConfig(
        name=body.name,
        server_code=body.server_code,
        server_password=body.server_password,
        server_type=body.server_type,
        server_address=body.server_address,
        server_port=body.server_port,
        auth_server=auth_server,
        api_key=body.api_key,
        cookie=cookie,
        sauth_json=sauth_json,
        auth_method=body.auth_method,
        device_model=device_model,
        access_point_type=body.access_point_type,
        auto_reconnect=body.auto_reconnect,
        max_reconnect_attempts=body.max_reconnect_attempts,
        reconnect_delay=body.reconnect_delay,
        account_id=body.account_id,
    )

    try:
        bot = await bot_manager.create_bot(bot_config)
    except ValueError as exc:
        # 达到最大机器人数量限制
        logger.warning(f"创建机器人失败: {exc}")
        return error_response(
            error="max_bots_reached",
            message=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建机器人异常")
        return error_response(error="create_failed", message=f"创建失败: {exc}")

    logger.info(
        f"创建机器人 {bot.name} (ID={bot.bot_id}, "
        f"server_type={body.server_type.value}, "
        f"ap={body.access_point_type.value})"
    )
    return success_response(
        data=bot.info.to_dict(),
        message=f"机器人 {bot.name} 创建成功",
    )


@router.post("/stop-all")
async def stop_all_bots(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """停止所有正在运行的机器人。

    注意: 该路由必须注册在所有 ``/{bot_id}`` 动态路由之前，否则
    ``stop-all`` 会被 ``{bot_id}`` 路径参数捕获 (尽管当前 HTTP 方法
    不同不会直接冲突，但显式前置可避免未来新增 ``POST /{bot_id}``
    路由时引入回归)。
    """
    try:
        await bot_manager.stop_all()
    except Exception as exc:  # noqa: BLE001
        logger.exception("停止所有机器人异常")
        return error_response(error="stop_all_failed", message=f"停止失败: {exc}")
    return success_response(message="所有机器人已停止")


@router.post("/restart")
async def restart_all_bots(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """重启所有机器人。

    停止所有正在运行的机器人, 然后重新启动之前处于运行状态的机器人。
    用于前端 ``#restartServiceBtn`` 按钮 (BUG-09 修复)。
    """
    try:
        # 记录当前运行中的机器人配置
        running_configs: List[BotConfig] = []
        # Bug 2.1 修复: 之前直接访问 bot_manager._bots 私有属性, 破坏封装。
        # 改用公开的 bots 属性 (返回 PocketBot 列表)。
        for bot in bot_manager.bots:
            if bot.info.status in (BotStatus.RUNNING, BotStatus.CONNECTED,
                                   BotStatus.SPAWNED, BotStatus.AUTHENTICATING):
                running_configs.append(bot.config)

        # 停止所有
        await bot_manager.stop_all()
        logger.info(f"已停止所有机器人, 准备重启 {len(running_configs)} 个")

        # 重新启动之前运行中的机器人
        restarted = 0
        for cfg in running_configs:
            try:
                await bot_manager.create_bot(cfg)
                restarted += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(f"重启机器人 {cfg.name} 失败: {exc}")

        return success_response(
            message=f"服务已重启, 成功恢复 {restarted}/{len(running_configs)} 个机器人",
            data={"restarted": restarted, "total": len(running_configs)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("重启所有机器人异常")
        return error_response(error="restart_failed", message=f"重启失败: {exc}")


@router.get("/{bot_id}")
async def get_bot(
    bot_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取机器人详情。"""
    bot = _require_bot(bot_id)
    return success_response(data=bot.info.to_dict())


@router.post("/{bot_id}/start")
async def start_bot(
    bot_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """启动机器人。"""
    _require_bot(bot_id)
    try:
        ok = await bot_manager.start_bot(bot_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"启动机器人 {bot_id} 异常")
        return error_response(error="start_failed", message=f"启动失败: {exc}")

    if not ok:
        return error_response(
            error="start_failed",
            message="机器人启动失败（可能已在运行）",
        )
    return success_response(message="机器人启动成功")


@router.post("/{bot_id}/stop")
async def stop_bot(
    bot_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """停止机器人。"""
    _require_bot(bot_id)
    try:
        ok = await bot_manager.stop_bot(bot_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"停止机器人 {bot_id} 异常")
        return error_response(error="stop_failed", message=f"停止失败: {exc}")

    if not ok:
        return error_response(error="stop_failed", message="机器人停止失败")
    return success_response(message="机器人已停止")


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """删除机器人（运行中会先停止）。"""
    try:
        ok = await bot_manager.remove_bot(bot_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"删除机器人 {bot_id} 异常")
        return error_response(error="delete_failed", message=f"删除失败: {exc}")

    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"机器人不存在: {bot_id}",
        )
    return success_response(message="机器人已删除")


@router.post("/{bot_id}/command")
async def send_command(
    bot_id: str,
    body: CommandRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """向机器人发送游戏命令。"""
    bot = _require_bot(bot_id)
    if bot.status not in (BotStatus.CONNECTED, BotStatus.SPAWNED):
        return error_response(
            error="not_connected",
            message="机器人未连接，无法发送命令",
        )
    try:
        ok = await bot_manager.send_command(bot_id, body.command)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"发送命令异常 (bot={bot_id})")
        return error_response(error="command_failed", message=f"发送失败: {exc}")

    if not ok:
        return error_response(error="command_failed", message="命令发送失败")
    return success_response(message="命令已发送")


@router.post("/{bot_id}/chat")
async def send_chat(
    bot_id: str,
    body: ChatRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """向机器人发送聊天消息。"""
    bot = _require_bot(bot_id)
    if bot.status not in (BotStatus.CONNECTED, BotStatus.SPAWNED):
        return error_response(
            error="not_connected",
            message="机器人未连接，无法发送聊天",
        )
    try:
        ok = await bot_manager.send_chat(bot_id, body.message)
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"发送聊天异常 (bot={bot_id})")
        return error_response(error="chat_failed", message=f"发送失败: {exc}")

    if not ok:
        return error_response(error="chat_failed", message="聊天发送失败")
    return success_response(message="聊天已发送")


@router.get("/{bot_id}/logs")
async def get_bot_logs(
    bot_id: str,
    limit: int = Query(100, ge=1, le=1000, description="返回最近 N 条日志"),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取机器人运行日志。"""
    _require_bot(bot_id)
    logs = bot_manager.get_bot_logs(bot_id, limit=limit)
    return success_response(
        data={"logs": logs, "total": len(logs)},
        message=f"共 {len(logs)} 条日志",
    )


@router.get("/{bot_id}/chat")
async def get_bot_chat(
    bot_id: str,
    limit: int = Query(50, ge=1, le=500, description="返回最近 N 条消息"),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取机器人聊天历史。"""
    _require_bot(bot_id)
    chat = bot_manager.get_bot_chat(bot_id, limit=limit)
    return success_response(
        data={"messages": chat, "total": len(chat)},
        message=f"共 {len(chat)} 条消息",
    )


@router.get("/{bot_id}/inventory")
async def get_bot_inventory(
    bot_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取机器人背包物品。"""
    bot = _require_bot(bot_id)
    inventory: List[Dict[str, Any]] = bot.get_inventory()
    return success_response(
        data={"slots": inventory, "total": len(inventory)},
        message=f"共 {len(inventory)} 个槽位",
    )


__all__ = ["router"]
