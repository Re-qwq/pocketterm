"""聊天命令系统 - 类似 ToolDelta 的插件命令机制。

本模块提供基于游戏内聊天消息的命令解析和分发系统。
玩家在聊天框中输入以 ``!`` 开头的消息即可触发命令,
机器人可执行查询类命令并回复玩家。

功能:
    - 命令注册与分发 (装饰器注册)
    - 权限检查 (仅允许特定玩家执行命令)
    - 命令帮助系统
    - 命令历史记录
    - 异步命令执行

重要限制:
    - 导入命令不能在游戏中执行, 只能在控制台操作
    - 本系统仅用于查询类命令 (如 !help, !status, !list 等)
    - 所有命令执行通过 BedrockClient.send_chat 回复玩家

使用方式::

    from app.protocol.connection import BedrockClient
    from app.protocol.chat_commands import ChatCommandSystem

    client = BedrockClient(sauth_json="...", device_fingerprint={...})
    await client.connect("host", 19132)

    cmd_system = ChatCommandSystem(client, command_prefix="!")

    @cmd_system.command("help")
    async def cmd_help(sender: str, args: list[str]):
        await cmd_system.reply(sender, "可用命令: help, status, list")

    @cmd_system.command("status")
    async def cmd_status(sender: str, args: list[str]):
        await cmd_system.reply(sender, "机器人状态: 运行中")

    # 处理聊天消息
    await cmd_system.process_chat("玩家名", "!help")

逆向来源:
    - ToolDelta 插件框架 (命令注册与分发)
    - Retalcer导入器 menu.py (菜单交互)
    - NexusEgo (批量命令处理)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, TYPE_CHECKING

from .connection import BedrockClient

if TYPE_CHECKING:
    from .game_events import PlayerChatEvent

logger = logging.getLogger("pocketterm.chat_commands")

# ======================================================================
# 常量
# ======================================================================

#: 默认命令前缀
DEFAULT_COMMAND_PREFIX: str = "!"

#: 命令响应超时 (秒)
COMMAND_RESPONSE_TIMEOUT: float = 10.0

#: 最大命令历史记录数
MAX_COMMAND_HISTORY: int = 100


# ======================================================================
# 数据类
# ======================================================================


@dataclass
class CommandResult:
    """命令执行结果。

    Attributes:
        success: 是否执行成功。
        message: 结果消息 (发送给玩家)。
        data: 附加数据 (可选)。
        execution_time: 执行耗时 (秒)。
    """

    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    execution_time: float = 0.0


@dataclass
class CommandInfo:
    """命令注册信息。

    Attributes:
        name: 命令名称 (不含前缀)。
        description: 命令描述。
        usage: 命令用法示例。
        handler: 命令处理函数。
        permission: 权限要求 ("all" | "admin" | 玩家名列表)。
        cooldown: 冷却时间 (秒), 0 表示无冷却。
        last_used: 上次使用时间戳。
    """

    name: str
    description: str = ""
    usage: str = ""
    handler: Optional[Callable] = None
    permission: str | list[str] = "all"
    cooldown: float = 0.0
    last_used: float = 0.0


# ======================================================================
# 聊天命令系统
# ======================================================================


class ChatCommandSystem:
    """聊天命令系统 — 解析和分发游戏内聊天命令。

    支持通过 ``!`` 前缀触发命令, 提供命令注册、权限检查、
    冷却管理和帮助系统。

    Args:
        client: 已连接的 BedrockClient 实例。
        command_prefix: 命令前缀 (默认 ``"!"``)。
        admin_players: 管理员玩家名列表 (用于权限检查)。
        bot_id: 关联的机器人 ID。
    """

    def __init__(
        self,
        client: BedrockClient,
        command_prefix: str = DEFAULT_COMMAND_PREFIX,
        admin_players: Optional[list[str]] = None,
        bot_id: str = "",
    ) -> None:
        """初始化聊天命令系统。

        Args:
            client: 已连接的 BedrockClient 实例。
            command_prefix: 命令前缀。
            admin_players: 管理员玩家名列表。
            bot_id: 关联的机器人 ID。
        """
        self._client: BedrockClient = client
        self._prefix: str = command_prefix
        self._admin_players: list[str] = admin_players or []
        self._bot_id: str = bot_id

        #: 命令注册表 {command_name: CommandInfo}
        self._commands: dict[str, CommandInfo] = {}

        #: 命令历史记录
        self._history: list[dict[str, Any]] = []

        #: 是否正在运行
        self._running: bool = False

        #: 命令执行统计
        self._command_counts: dict[str, int] = {}
        self._start_time: float = 0.0

        #: 消息回调 (用于通过聊天框发送回复)
        self._message_callback: Optional[Callable[[str], Any]] = None

        # --- 注册内置命令 ---
        self._register_builtin_commands()

    # ------------------------------------------------------------------
    # 命令注册
    # ------------------------------------------------------------------

    def command(
        self,
        name: str,
        description: str = "",
        usage: str = "",
        permission: str | list[str] = "all",
        cooldown: float = 0.0,
    ):
        """装饰器: 注册命令处理器。

        用法::

            cmd_system = ChatCommandSystem(client)

            @cmd_system.command("help", description="显示帮助", usage="!help [命令名]")
            async def cmd_help(sender: str, args: list[str]):
                await cmd_system.reply(sender, "这是帮助信息")

            @cmd_system.command("tp", permission="admin", cooldown=5.0)
            async def cmd_tp(sender: str, args: list[str]):
                # 仅管理员可用, 冷却 5 秒
                ...

        Args:
            name: 命令名称 (不含前缀)。
            description: 命令描述。
            usage: 命令用法示例。
            permission: 权限要求, 可选:
                - ``"all"``: 所有玩家可用
                - ``"admin"``: 仅管理员可用
                - ``[玩家名列表]``: 仅指定玩家可用
            cooldown: 冷却时间 (秒), 0 表示无冷却。

        Returns:
            装饰器函数。
        """
        def decorator(handler: Callable) -> Callable:
            self._commands[name] = CommandInfo(
                name=name,
                description=description,
                usage=usage or f"{self._prefix}{name}",
                handler=handler,
                permission=permission,
                cooldown=cooldown,
            )
            logger.debug(
                "注册命令: %s%s (permission=%s, cooldown=%.1fs)",
                self._prefix,
                name,
                permission,
                cooldown,
            )
            return handler

        return decorator

    def add_command(
        self,
        name: str,
        handler: Callable,
        description: str = "",
        usage: str = "",
        permission: str | list[str] = "all",
        cooldown: float = 0.0,
    ) -> None:
        """注册命令处理器 (非装饰器方式)。

        Args:
            name: 命令名称 (不含前缀)。
            handler: 命令处理函数, 签名 ``(sender: str, args: list[str]) -> ...``。
            description: 命令描述。
            usage: 命令用法。
            permission: 权限要求。
            cooldown: 冷却时间 (秒)。
        """
        self._commands[name] = CommandInfo(
            name=name,
            description=description,
            usage=usage or f"{self._prefix}{name}",
            handler=handler,
            permission=permission,
            cooldown=cooldown,
        )
        logger.debug("注册命令: %s%s", self._prefix, name)

    def remove_command(self, name: str) -> bool:
        """移除已注册的命令。

        Args:
            name: 命令名称 (不含前缀)。

        Returns:
            True 移除成功; False 命令不存在。
        """
        if name in self._commands:
            del self._commands[name]
            logger.debug("移除命令: %s%s", self._prefix, name)
            return True
        return False

    # ------------------------------------------------------------------
    # 权限管理
    # ------------------------------------------------------------------

    def add_admin(self, player_name: str) -> None:
        """添加管理员玩家。

        Args:
            player_name: 玩家名。
        """
        if player_name not in self._admin_players:
            self._admin_players.append(player_name)
            logger.info("添加管理员: %s", player_name)

    def remove_admin(self, player_name: str) -> None:
        """移除管理员玩家。

        Args:
            player_name: 玩家名。
        """
        if player_name in self._admin_players:
            self._admin_players.remove(player_name)
            logger.info("移除管理员: %s", player_name)

    def is_admin(self, player_name: str) -> bool:
        """检查玩家是否为管理员。

        Args:
            player_name: 玩家名。

        Returns:
            True 表示是管理员。
        """
        return player_name in self._admin_players

    def _check_permission(self, command: CommandInfo, sender: str) -> bool:
        """检查发送者是否有权限执行命令。

        Args:
            command: 命令信息。
            sender: 发送者玩家名。

        Returns:
            True 表示有权限。
        """
        if command.permission == "all":
            return True
        if command.permission == "admin":
            return self.is_admin(sender)
        if isinstance(command.permission, list):
            return sender in command.permission
        return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动命令系统。"""
        self._running = True
        self._start_time = time.time()
        logger.info(
            "聊天命令系统已启动 (prefix=%s, admin_players=%s)",
            self._prefix,
            self._admin_players,
        )

    async def stop(self) -> None:
        """停止命令系统。"""
        self._running = False
        logger.info("聊天命令系统已停止")

    # ------------------------------------------------------------------
    # 消息回调
    # ------------------------------------------------------------------

    def set_message_callback(self, callback: Callable[[str], Any]) -> None:
        """设置消息回调函数 (用于通过聊天框发送回复)。

        Args:
            callback: 回调函数, 接收一个字符串参数 (消息内容)。
        """
        self._message_callback = callback

    async def send_chat(self, message: str) -> None:
        """通过 BedrockClient 发送聊天消息。

        会同时调用消息回调 (如果设置了的话)。

        Args:
            message: 要发送的消息内容。
        """
        if self._client.connected:
            await self._client.send_chat(message)
            logger.debug("发送聊天: %s", message[:50])
        if self._message_callback:
            try:
                result = self._message_callback(message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug("消息回调失败: %s", exc)

    async def reply(self, sender: str, message: str) -> None:
        """回复玩家 (在消息前加上玩家名)。

        Args:
            sender: 目标玩家名。
            message: 回复内容。
        """
        await self.send_chat(f"@{sender} {message}")

    # ------------------------------------------------------------------
    # 聊天处理
    # ------------------------------------------------------------------

    async def process_chat(self, sender: str, message: str) -> Optional[CommandResult]:
        """处理聊天消息, 检测并执行命令。

        如果消息以命令前缀开头, 解析命令并执行。
        否则忽略。

        Args:
            sender: 发送者玩家名。
            message: 聊天消息内容。

        Returns:
            命令执行结果; 如果不是命令则返回 None。
        """
        if not self._running:
            return None

        message = message.strip()
        if not message.startswith(self._prefix):
            return None

        # 解析命令
        parts = message[len(self._prefix):].strip().split()
        if not parts:
            return None

        command_name = parts[0].lower()
        args = parts[1:] if len(parts) > 1 else []

        # 记录历史
        start_time = time.time()

        try:
            result = await self._execute_command(command_name, sender, args)
            result.execution_time = time.time() - start_time

            # 更新统计
            self._command_counts[command_name] = (
                self._command_counts.get(command_name, 0) + 1
            )

            # 记录历史
            self._history.append(
                {
                    "sender": sender,
                    "command": command_name,
                    "args": args,
                    "success": result.success,
                    "message": result.message[:200],
                    "timestamp": time.time(),
                    "execution_time": result.execution_time,
                }
            )
            if len(self._history) > MAX_COMMAND_HISTORY:
                self._history = self._history[-MAX_COMMAND_HISTORY:]

            return result

        except Exception as exc:
            logger.error(
                "命令执行异常: %s%s (sender=%s): %s",
                self._prefix,
                command_name,
                sender,
                exc,
                exc_info=True,
            )
            return CommandResult(
                success=False,
                message=f"命令执行出错: {exc}",
                execution_time=time.time() - start_time,
            )

    async def process_event(self, event: "PlayerChatEvent") -> Optional[CommandResult]:
        """处理 PlayerChatEvent, 从中提取命令。

        这是与 game_events.py 集成的便捷方法。

        Args:
            event: 玩家聊天事件。

        Returns:
            命令执行结果; 如果不是命令则返回 None。
        """
        return await self.process_chat(event.sender, event.message)

    # ==================================================================
    # 私有方法 — 命令执行
    # ==================================================================

    async def _execute_command(
        self, command_name: str, sender: str, args: list[str]
    ) -> CommandResult:
        """执行命令。

        Args:
            command_name: 命令名称。
            sender: 发送者玩家名。
            args: 命令参数列表。

        Returns:
            命令执行结果。
        """
        # 查找命令
        cmd_info = self._commands.get(command_name)
        if cmd_info is None:
            logger.debug(
                "未知命令: %s%s (sender=%s)", self._prefix, command_name, sender
            )
            return CommandResult(
                success=False,
                message=f"未知命令: {self._prefix}{command_name}。输入 {self._prefix}help 查看可用命令。",
            )

        # 权限检查
        if not self._check_permission(cmd_info, sender):
            logger.debug(
                "权限不足: %s%s (sender=%s)", self._prefix, command_name, sender
            )
            return CommandResult(
                success=False,
                message=f"权限不足: 你没有权限执行 {self._prefix}{command_name}。",
            )

        # 冷却检查
        if cmd_info.cooldown > 0 and cmd_info.last_used > 0:
            elapsed = time.time() - cmd_info.last_used
            if elapsed < cmd_info.cooldown:
                remaining = cmd_info.cooldown - elapsed
                return CommandResult(
                    success=False,
                    message=f"命令冷却中, 请 {remaining:.1f} 秒后再试。",
                )

        # 执行命令
        handler = cmd_info.handler
        if handler is None:
            return CommandResult(
                success=False,
                message=f"命令 {self._prefix}{command_name} 未配置处理器。",
            )

        cmd_info.last_used = time.time()

        try:
            if asyncio.iscoroutinefunction(handler):
                result = await handler(sender, args)
            else:
                result = handler(sender, args)

            # 处理返回值
            if isinstance(result, CommandResult):
                return result
            elif isinstance(result, str):
                return CommandResult(success=True, message=result)
            elif result is None:
                return CommandResult(
                    success=True, message=f"命令 {self._prefix}{command_name} 执行完成。"
                )
            else:
                return CommandResult(
                    success=True,
                    message=str(result),
                    data={"raw_result": result},
                )

        except Exception as exc:
            logger.error("命令处理器错误: %s", exc, exc_info=True)
            return CommandResult(
                success=False,
                message=f"命令执行出错: {exc}",
            )

    # ==================================================================
    # 私有方法 — 内置命令
    # ==================================================================

    def _register_builtin_commands(self) -> None:
        """注册内置命令 (help, status, list, admin)。"""

        @self.command(
            "help",
            description="显示帮助信息",
            usage=f"{self._prefix}help [命令名]",
        )
        async def _cmd_help(sender: str, args: list[str]):
            """内置帮助命令。"""
            if args:
                # 查看特定命令的帮助
                cmd_name = args[0].lower()
                cmd_info = self._commands.get(cmd_name)
                if cmd_info:
                    return (
                        f"--- {self._prefix}{cmd_name} ---\n"
                        f"描述: {cmd_info.description or '无'}\n"
                        f"用法: {cmd_info.usage}\n"
                        f"权限: {cmd_info.permission}"
                    )
                else:
                    return f"未知命令: {self._prefix}{cmd_name}"
            else:
                # 列出所有可用命令
                lines = [f"--- 可用命令 (前缀: {self._prefix}) ---"]
                for name, info in sorted(self._commands.items()):
                    perm = "[A]" if info.permission == "admin" else ""
                    lines.append(f"  {self._prefix}{name}{perm} - {info.description or '无描述'}")
                lines.append(f"共 {len(self._commands)} 个命令")
                return "\n".join(lines)

        @self.command(
            "status",
            description="查看机器人状态",
            usage=f"{self._prefix}status",
        )
        async def _cmd_status(sender: str, args: list[str]):
            """内置状态命令。"""
            uptime = time.time() - self._start_time if self._start_time > 0 else 0
            return (
                "--- 机器人状态 ---\n"
                f"机器人 ID: {self._bot_id or '未知'}\n"
                f"运行时间: {uptime:.0f} 秒\n"
                f"已注册命令: {len(self._commands)} 个\n"
                f"今日执行命令: {sum(self._command_counts.values())} 次\n"
                f"前缀: {self._prefix}"
            )

        @self.command(
            "list",
            description="列出在线玩家",
            usage=f"{self._prefix}list",
        )
        async def _cmd_list(sender: str, args: list[str]):
            """内置玩家列表命令。"""
            try:
                # 通过发送 /list 命令获取玩家列表
                output = await asyncio.wait_for(
                    self._client.send_command("list"),
                    timeout=COMMAND_RESPONSE_TIMEOUT,
                )
                return f"--- 在线玩家 ---\n{output}"
            except asyncio.TimeoutError:
                return "获取玩家列表超时, 请稍后再试"
            except Exception as exc:
                return f"获取玩家列表失败: {exc}"

        @self.command(
            "admin",
            description="管理员命令 (添加/移除管理员)",
            usage=f"{self._prefix}admin [add|remove|list] [玩家名]",
            permission="admin",
        )
        async def _cmd_admin(sender: str, args: list[str]):
            """内置管理员管理命令。"""
            if not args:
                return f"用法: {self._prefix}admin [add|remove|list] [玩家名]"

            subcmd = args[0].lower()
            if subcmd == "list":
                admins = ", ".join(self._admin_players) if self._admin_players else "无"
                return f"当前管理员: {admins}"
            elif subcmd == "add" and len(args) >= 2:
                self.add_admin(args[1])
                return f"已添加管理员: {args[1]}"
            elif subcmd == "remove" and len(args) >= 2:
                self.remove_admin(args[1])
                return f"已移除管理员: {args[1]}"
            else:
                return f"未知子命令: {subcmd}。用法: {self._prefix}admin [add|remove|list] [玩家名]"

    # ------------------------------------------------------------------
    # 查询与统计
    # ------------------------------------------------------------------

    def get_commands(self) -> list[dict[str, Any]]:
        """获取所有已注册命令的信息列表。

        Returns:
            命令信息字典列表。
        """
        return [
            {
                "name": info.name,
                "description": info.description,
                "usage": info.usage,
                "permission": (
                    info.permission
                    if isinstance(info.permission, str)
                    else list(info.permission)
                ),
                "cooldown": info.cooldown,
            }
            for info in self._commands.values()
        ]

    def get_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取命令执行历史。

        Args:
            limit: 返回最近 N 条记录。

        Returns:
            命令历史字典列表。
        """
        return list(self._history[-limit:])

    def get_stats(self) -> dict[str, Any]:
        """获取命令系统运行统计。"""
        uptime = time.time() - self._start_time if self._start_time > 0 else 0
        return {
            "running": self._running,
            "command_prefix": self._prefix,
            "bot_id": self._bot_id,
            "uptime": uptime,
            "total_commands": sum(self._command_counts.values()),
            "command_counts": dict(self._command_counts),
            "registered_commands": len(self._commands),
            "admin_players": list(self._admin_players),
        }


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    "CommandResult",
    "CommandInfo",
    "ChatCommandSystem",
    "DEFAULT_COMMAND_PREFIX",
    "COMMAND_RESPONSE_TIMEOUT",
    "MAX_COMMAND_HISTORY",
]