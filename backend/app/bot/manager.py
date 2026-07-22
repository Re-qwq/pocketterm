"""PocketTerm 机器人管理器

:class:`BotManager` 是一个单例类，负责统一管理多个 :class:`PocketBot` 实例。

提供以下功能:

    - **创建 / 移除**:  ``create_bot`` / ``remove_bot``
    - **生命周期**:      ``start_bot`` / ``stop_bot`` / ``stop_all``
    - **查询**:          ``get_bot`` / ``list_bots`` / ``get_status_counts``
    - **操作代理**:      ``send_command`` / ``get_bot_logs`` / ``get_bot_chat``

线程安全说明:
    所有修改机器人列表的操作都使用 ``asyncio.Lock`` 保护，
    确保在异步环境中并发安全。

典型用法::

    from bot.manager import BotManager, bot_manager
    from bot.models import BotConfig, ServerType, AccessPointType

    # 使用全局单例
    config = BotConfig(
        server_code="123456",
        server_type=ServerType.RENTAL,
        access_point_type=AccessPointType.NEOMEGA,
    )
    bot = await bot_manager.create_bot(config)
    await bot_manager.start_bot(bot.bot_id)

    # 列出所有机器人
    bots = bot_manager.list_bots()

    # 获取状态统计
    counts = bot_manager.get_status_counts()
    print(f"运行中: {counts.get('spawned', 0)}")
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from ..access_point.base import Colors
from ..access_point.manager import AccessPointManager, get_manager
from .bot import PocketBot
from .models import BotConfig, BotStatus

logger = logging.getLogger("pocketterm.bot_manager")


# ======================================================================
# BotManager 主类
# ======================================================================


class BotManager:
    """机器人管理器（单例）。

    管理多个 :class:`PocketBot` 实例的完整生命周期。

    Args:
        max_bots: 最大机器人数量限制。
        name_prefix: 自动生成机器人名称的前缀（默认 ``"PT_"``）。
        ap_manager: 接入点管理器实例。为 ``None`` 时使用全局管理器。
    """

    # 单例实例
    _instance: Optional["BotManager"] = None
    _initialized: bool = False

    def __new__(cls, *args, **kwargs) -> "BotManager":
        """单例模式:确保全局只有一个 BotManager 实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        max_bots: int = 20,
        name_prefix: str = "PT_",
        ap_manager: Optional[AccessPointManager] = None,
    ) -> None:
        # 避免单例模式下重复初始化
        if BotManager._initialized:
            return
        BotManager._initialized = True

        self.max_bots: int = max_bots
        self.name_prefix: str = name_prefix
        self._ap_manager: AccessPointManager = ap_manager or get_manager()
        #: 机器人实例字典 {bot_id -> PocketBot}
        self._bots: dict[str, PocketBot] = {}
        #: 异步锁（保护 _bots 的并发访问）- H-8 修复: 懒加载
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------ #
    # 类方法:重置单例（主要用于测试）
    # ------------------------------------------------------------------ #

    @classmethod
    def _reset_singleton(cls) -> None:
        """重置单例实例（仅用于测试）。

        清除单例实例和初始化标志，允许重新创建。
        生产环境中不应调用此方法。
        """
        cls._instance = None
        cls._initialized = False

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def bot_count(self) -> int:
        """当前管理的机器人数量。"""
        return len(self._bots)

    @property
    def bots(self) -> list[PocketBot]:
        """所有机器人实例列表。"""
        return list(self._bots.values())

    # ------------------------------------------------------------------ #
    # 创建 / 移除
    # ------------------------------------------------------------------ #

    async def create_bot(self, config: BotConfig) -> PocketBot:
        """创建新机器人。

        在创建时自动注册全局事件处理器（connect / disconnect / error）。
        创建后机器人处于 IDLE 状态，需调用 :meth:`start_bot` 启动。

        Args:
            config: 机器人配置。

        Returns:
            新创建的 :class:`PocketBot` 实例。

        Raises:
            ValueError: 已达到最大机器人数量限制。
        """
        async with self._get_lock():
            if len(self._bots) >= self.max_bots:
                raise ValueError(
                    f"已达到最大机器人数量限制 ({self.max_bots})"
                )

            bot = PocketBot(
                config,
                name_prefix=self.name_prefix,
                ap_manager=self._ap_manager,
            )

            # 注册全局事件处理器
            self._register_global_handlers(bot)

            self._bots[bot.bot_id] = bot
            self._print_manager_log(
                f"创建机器人 {Colors.colorize(bot.name, Colors.GREEN)} "
                f"(ID: {bot.bot_id})",
                Colors.CYAN,
            )
            logger.info(f"创建机器人 {bot.name} (ID: {bot.bot_id})")
            return bot

    def _register_global_handlers(self, bot: PocketBot) -> None:
        """为机器人注册全局事件处理器。

        这些处理器会在机器人 connect / disconnect / error 时
        打印管理器级别的日志。

        Args:
            bot: 机器人实例。
        """

        async def on_connect(b: PocketBot) -> None:
            self._print_manager_log(
                f"机器人 {Colors.colorize(b.name, Colors.GREEN)} 已连接",
                Colors.GREEN,
            )
            logger.info(f"机器人 {b.name} 已连接")

        async def on_disconnect(b: PocketBot) -> None:
            self._print_manager_log(
                f"机器人 {Colors.colorize(b.name, Colors.MAGENTA)} 已断开",
                Colors.MAGENTA,
            )
            logger.info(f"机器人 {b.name} 已断开")

        async def on_error(b: PocketBot, error: Exception) -> None:
            self._print_manager_log(
                f"机器人 {Colors.colorize(b.name, Colors.RED)} 错误: {error}",
                Colors.RED,
            )
            logger.error(f"机器人 {b.name} 错误: {error}")

        async def on_ban(b: PocketBot, reason: str) -> None:
            self._print_manager_log(
                f"机器人 {Colors.colorize(b.name, Colors.BG_RED)} 被封禁: {reason}",
                Colors.RED,
            )
            logger.critical(f"机器人 {b.name} 被封禁: {reason}")

        async def on_kick(b: PocketBot, reason: str) -> None:
            self._print_manager_log(
                f"机器人 {Colors.colorize(b.name, Colors.YELLOW)} 被踢出: {reason}",
                Colors.YELLOW,
            )
            logger.warning(f"机器人 {b.name} 被踢出: {reason}")

        async def on_spawn(b: PocketBot) -> None:
            self._print_manager_log(
                f"机器人 {Colors.colorize(b.name, Colors.BRIGHT_GREEN)} 已在游戏中生成",
                Colors.BRIGHT_GREEN,
            )

        # 注册事件处理器
        bot.on("connect", on_connect)
        bot.on("disconnect", on_disconnect)
        bot.on("error", on_error)
        bot.on("ban", on_ban)
        bot.on("kick", on_kick)
        bot.on("spawn", on_spawn)

    async def remove_bot(self, bot_id: str) -> bool:
        """移除机器人。

        如果机器人正在运行，会先停止它。

        Args:
            bot_id: 机器人 ID。

        Returns:
            ``True`` 移除成功;``False`` 机器人不存在。
        """
        async with self._get_lock():
            bot = self._bots.get(bot_id)
            if bot is None:
                return False

            # 如果机器人正在运行，先停止
            if bot.status in (
                BotStatus.CONNECTING,
                BotStatus.AUTHENTICATING,
                BotStatus.CONNECTED,
                BotStatus.SPAWNED,
            ):
                await bot.stop()

            del self._bots[bot_id]
            self._print_manager_log(
                f"移除机器人 {Colors.colorize(bot.name, Colors.YELLOW)} "
                f"(ID: {bot_id})",
                Colors.YELLOW,
            )
            logger.info(f"移除机器人 {bot.name} (ID: {bot_id})")
            return True

    # ------------------------------------------------------------------ #
    # 生命周期管理
    # ------------------------------------------------------------------ #

    async def start_bot(self, bot_id: str) -> bool:
        """启动机器人。

        机器人启动成功后自动加载已启用的插件。

        Args:
            bot_id: 机器人 ID。

        Returns:
            ``True`` 启动成功;``False`` 机器人不存在或已在运行。
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return False
        result = await bot.start()
        if result:
            # 机器人启动成功后, 加载已启用的插件
            try:
                from ..plugins.manager import plugin_manager
                count = await plugin_manager.load_plugins_for_bot(bot)
                if count > 0:
                    self._print_manager_log(
                        f"已为机器人 {bot.name} 加载 {count} 个插件",
                        Colors.GREEN,
                    )
            except Exception as e:
                self._print_manager_log(
                    f"加载插件时出错 (不影响机器人运行): {e}",
                    Colors.YELLOW,
                )
        return result

    async def stop_bot(self, bot_id: str) -> bool:
        """停止机器人。

        停止前先卸载该机器人绑定的所有插件。

        Args:
            bot_id: 机器人 ID。

        Returns:
            ``True`` 停止成功;``False`` 机器人不存在。
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return False
        # 先卸载插件
        try:
            from ..plugins.manager import plugin_manager
            count = await plugin_manager.unload_plugins_for_bot(bot)
            if count > 0:
                self._print_manager_log(
                    f"已卸载机器人 {bot.name} 的 {count} 个插件",
                    Colors.YELLOW,
                )
        except Exception as e:
            self._print_manager_log(
                f"卸载插件时出错 (不影响停止): {e}",
                Colors.YELLOW,
            )
        await bot.stop()
        return True

    async def stop_all(self) -> None:
        """停止所有正在运行的机器人。"""
        self._print_manager_log("停止所有机器人...", Colors.YELLOW)

        tasks = []
        for bot in self._bots.values():
            if bot.status in (
                BotStatus.CONNECTING,
                BotStatus.AUTHENTICATING,
                BotStatus.CONNECTED,
                BotStatus.SPAWNED,
            ):
                tasks.append(bot.stop())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._print_manager_log("所有机器人已停止", Colors.GREEN)
        logger.info("所有机器人已停止")

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def get_bot(self, bot_id: str) -> Optional[PocketBot]:
        """获取指定机器人。

        Args:
            bot_id: 机器人 ID。

        Returns:
            机器人实例;不存在时返回 ``None``。
        """
        return self._bots.get(bot_id)

    def list_bots(self) -> list[dict[str, Any]]:
        """列出所有机器人的信息。

        Returns:
            机器人信息字典列表（通过 ``BotInfo.to_dict()`` 序列化）。
        """
        return [bot.info.to_dict() for bot in self._bots.values()]

    def get_status_counts(self) -> dict[str, int]:
        """获取各状态的机器人数量统计。

        Returns:
            ``{status_value: count}`` 字典。
            例如::

                {
                    "idle": 2,
                    "connecting": 0,
                    "connected": 1,
                    "spawned": 3,
                    "error": 0,
                    ...
                }
        """
        counts: dict[str, int] = {
            status.value: 0 for status in BotStatus
        }
        for bot in self._bots.values():
            counts[bot.status.value] += 1
        return counts

    # ------------------------------------------------------------------ #
    # 操作代理
    # ------------------------------------------------------------------ #

    async def send_command(self, bot_id: str, command: str) -> bool:
        """向指定机器人发送游戏命令。

        Args:
            bot_id: 机器人 ID。
            command: 命令字符串。

        Returns:
            ``True`` 发送成功;``False`` 机器人不存在或未连接。
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return False
        return await bot.send_command(command)

    async def send_chat(self, bot_id: str, message: str) -> bool:
        """向指定机器人发送聊天消息。

        Args:
            bot_id: 机器人 ID。
            message: 聊天消息。

        Returns:
            ``True`` 发送成功;``False`` 机器人不存在或未连接。
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return False
        return await bot.send_chat(message)

    def get_bot_logs(
        self, bot_id: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """获取机器人运行日志。

        Args:
            bot_id: 机器人 ID。
            limit: 返回最近 N 条日志。

        Returns:
            日志字典列表;机器人不存在时返回空列表。
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return []
        return bot.info.logs[-limit:]

    def get_bot_chat(
        self, bot_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """获取机器人聊天历史。

        Args:
            bot_id: 机器人 ID。
            limit: 返回最近 N 条消息。

        Returns:
            聊天消息字典列表;机器人不存在时返回空列表。
        """
        bot = self._bots.get(bot_id)
        if bot is None:
            return []
        return bot.get_chat_history(limit)

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def _print_manager_log(self, message: str, color: str = Colors.CYAN) -> None:
        """打印管理器级别的彩色日志。

        格式: ``[HH:MM:SS] [BotManager] message``

        同时输出到 stdout (带 ANSI 颜色) 和 logger，确保日志既能
        在控制台直观显示，也能被日志收集系统捕获。

        Args:
            message: 日志内容。
            color: ANSI 颜色码。
        """
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{color}{Colors.BOLD}[BotManager]{Colors.RESET} "
            f"{color}{message}{Colors.RESET}",
            flush=True,
        )
        # 同步记录到 logger (去除 ANSI 颜色码，仅保留纯文本)
        import re
        plain = re.sub(r"\033\[[0-9;]*m", "", message)
        logger.info(f"[BotManager] {plain}")


# ======================================================================
# 全局单例
# ======================================================================

#: 全局机器人管理器实例
#: 首次访问时通过单例模式创建
bot_manager = BotManager()


__all__ = ["BotManager", "bot_manager"]
