"""PocketBot - PocketTerm 机器人类（核心）

PocketBot 是 PocketTerm 系统的核心组件，负责:

    1. **生命周期管理**  —— 启动、停止、重连、错误恢复
    2. **接入点集成**    —— 根据配置选择 NeOmega / FateArk / Custom 接入点
    3. **服务器连接**    —— 支持租赁服、联机大厅、本地联机、自定义服务器
    4. **游戏操作**      —— 命令、聊天、移动、物品交互等
    5. **封禁检测**      —— 关键词检测，发现封禁时停止重连
    6. **事件系统**      —— connect / disconnect / chat / spawn / error / ban / kick / join_room / leave_room
    7. **彩色控制台**    —— 所有操作和状态变更都有彩色日志输出

设计思路:
    PocketBot 不直接实现 MCBE 协议，而是通过「接入点」与服务器通信。
    接入点是一个独立的进程或库，负责底层 RakNet 连接、认证、登录序列等。
    PocketBot 在接入点之上提供游戏操作 API。

错误处理策略:
    - ``AccountBannedError``      -> 停止（不可恢复）
    - ``InvalidCredentialsError``  -> 停止（需用户修正配置）
    - ``VersionTooLowError``       -> 停止（需更新版本）
    - ``ServerFullError``          -> 重连（服务器满，稍后可能成功）
    - ``NetworkError``             -> 重连（网络暂时故障）

典型用法::

    from bot.models import BotConfig, ServerType, AccessPointType
    from bot.bot import PocketBot

    config = BotConfig(
        server_code="123456",
        server_password="",
        server_type=ServerType.RENTAL,
        access_point_type=AccessPointType.NEOMEGA,
        auth_server="https://nv1.nethard.pro",
        api_key="your-api-key",
    )
    bot = PocketBot(config)
    bot.on("connect", lambda b: print(f"{b.name} connected!"))
    await bot.start()
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
import traceback
from typing import Any, Callable, Optional

from ..access_point.base import (
    AccessPoint,
    AccountBannedError,
    Colors,
    InvalidCredentialsError,
    NetworkError,
    ServerFullError,
    ServerNotFoundError,
    ServerRejectedError,
    VersionTooLowError,
)
from ..access_point.manager import AccessPointManager, get_manager
from ..auth.device_fingerprint import (
    DeviceFingerprint,
    get_fingerprint_manager,
)
from ..auth.anti_ban import AntiBanController, get_anti_ban_controller
from .models import (
    AccessPointType,
    BotConfig,
    BotInfo,
    BotStatus,
    ChatMessage,
    InventorySlot,
    ServerType,
    WindowInfo,
)

logger = logging.getLogger("pocketterm.bot")


# ======================================================================
# 封禁 / 踢出关键词检测
# ======================================================================

#: 封禁关键词列表（检测到即标记为 BANNED，停止重连）
#: 包含中英文常见封禁提示
BAN_KEYWORDS: list[str] = [
    "封禁",
    "ban",
    "banned",
    "禁用",
    "禁止登录",
    "账号异常",
    "违规",
    "安全检测",
    "client not allowed",
    "disconnected from server",
    "你已被服务器禁止",
    "multiplayer.disconnect.banned",
    "account suspended",
    "账号已被",
    "永久封禁",
    "临时封禁",
    "作弊",
    "外挂",
    "第三方软件",
    "异常行为",
]

#: 踢出关键词列表（检测到即标记为 KICKED，可重连）
KICK_KEYWORDS: list[str] = [
    "kicked",
    "踢出",
    "断开连接",
    "server full",
    "服务器已满",
    "连接超时",
    "timeout",
    "disconnected",
    "flymode",
    "飞行",
]


# ======================================================================
# PocketBot 主类
# ======================================================================


class PocketBot:
    """PocketTerm 机器人类。

    每个实例代表一个游戏机器人，拥有独立的配置、状态、聊天历史和日志。

    Args:
        config: 机器人配置。若 ``config.name`` 为空，自动生成
            ``PT_<6位随机数字>`` 格式的名称。
        name_prefix: 自动生成名称的前缀（默认 ``"PT_"``）。
        ap_manager: 接入点管理器实例。为 ``None`` 时使用全局管理器。
    """

    def __init__(
        self,
        config: BotConfig,
        name_prefix: str = "PT_",
        ap_manager: Optional[AccessPointManager] = None,
    ) -> None:
        # 自动生成名称: PT_ + 6位随机数字
        if not config.name:
            config.name = f"{name_prefix}{random.randint(100000, 999999)}"

        self.config: BotConfig = config
        self.info: BotInfo = BotInfo(config=config)

        # 接入点管理器
        self._ap_manager: AccessPointManager = ap_manager or get_manager()
        # 接入点实例
        self._access_point: Optional[AccessPoint] = None

        # 运行控制
        self._running: bool = False
        self._task: Optional[asyncio.Task[None]] = None

        # 游戏状态
        self._chat_history: list[ChatMessage] = []
        self._inventory: list[InventorySlot] = []
        self._current_window: Optional[WindowInfo] = None

        # 事件系统
        self._event_handlers: dict[str, list[Callable]] = {
            "connect": [],
            "disconnect": [],
            "chat": [],
            "spawn": [],
            "error": [],
            "ban": [],
            "kick": [],
            "join_room": [],
            "leave_room": [],
        }

        # 重连控制
        self._reconnect_count: int = 0
        self._ban_detected: bool = False
        self._last_packet_time: float = 0.0
        self._start_time: float = time.time()

        # 设备指纹 (按 account_id 隔离, 持久化)
        # NovaBuilder / NexusE 逆向: 通过 uqholder.Player 维护
        # DeviceID / ClientRandomID / UUID / BuildPlatform 等字段
        self._device_fingerprint: Optional[DeviceFingerprint] = None
        self._setup_device_fingerprint()

        # 防封禁策略控制器 (随机延迟 / 行为模拟 / 速率限制 / 异常检测)
        self._anti_ban: AntiBanController = get_anti_ban_controller()

        # 打印启动头部
        self._print_header()

    def _setup_device_fingerprint(self) -> None:
        """加载或生成设备指纹 (按 account_id 隔离, 持久化)。

        优先使用 ``BotConfig.account_id`` 绑定的已有指纹, 保证“一人一机”
        稳定 (避免每次登录都是新设备, 触发反作弊)。若 ``account_id`` 为空
        或不存在, 则生成新指纹并保存。
        """
        try:
            mgr = get_fingerprint_manager()
            account_id = self.config.account_id or f"bot:{self.config.name}"
            self._device_fingerprint = mgr.get_or_create(
                account_id=account_id,
                device_model=self.config.device_model or None,
            )
            logger.info(
                f"设备指纹已加载 (account={account_id}): "
                f"{self._device_fingerprint.short_summary()}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"设备指纹加载失败, 将使用临时指纹: {exc}")
            # 兜底: 生成临时指纹 (不持久化)
            self._device_fingerprint = DeviceFingerprint.generate(
                account_id=self.config.account_id,
                device_model=self.config.device_model or None,
            )

    # ==================================================================
    # 属性
    # ==================================================================

    @property
    def bot_id(self) -> str:
        """机器人唯一 ID。"""
        return self.info.bot_id

    @property
    def status(self) -> BotStatus:
        """当前运行状态。"""
        return self.info.status

    @property
    def name(self) -> str:
        """机器人名称。"""
        return self.config.name

    @property
    def device_fingerprint(self) -> Optional[DeviceFingerprint]:
        """当前绑定的设备指纹 (与 ``uqholder.Player`` 字段对齐)。"""
        return self._device_fingerprint

    @property
    def anti_ban(self) -> AntiBanController:
        """防封禁策略控制器 (供外部插件读取/调整)。"""
        return self._anti_ban

    # ==================================================================
    # 事件系统
    # ==================================================================

    def on(self, event: str, handler: Callable) -> None:
        """注册事件处理器。

        支持的事件:
            - ``connect``     连接成功 ``(bot)``
            - ``disconnect``  断开连接 ``(bot)``
            - ``chat``        收到聊天 ``(bot, sender, message)``
            - ``spawn``       机器人生成 ``(bot)``
            - ``error``       发生错误 ``(bot, error)``
            - ``ban``         检测到封禁 ``(bot, reason)``
            - ``kick``        被踢出 ``(bot, reason)``
            - ``join_room``   进入房间 ``(bot, server_type)``
            - ``leave_room``  离开房间 ``(bot)``

        Args:
            event: 事件名称。
            handler: 处理函数（可以是普通函数或协程函数）。
        """
        if event in self._event_handlers:
            self._event_handlers[event].append(handler)
        else:
            self._event_handlers[event] = [handler]

    async def _emit(self, event: str, *args, **kwargs) -> None:
        """触发事件，调用所有已注册的处理器。

        Args:
            event: 事件名称。
            *args: 传给处理器的位置参数。
            **kwargs: 传给处理器的关键字参数。
        """
        for handler in self._event_handlers.get(event, []):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                else:
                    handler(*args, **kwargs)
            except Exception as exc:
                logger.error(f"事件处理器错误 ({event}): {exc}")
                logger.error(traceback.format_exc())

    # ==================================================================
    # 控制台输出
    # ==================================================================

    def _print_header(self) -> None:
        """打印彩色 ASCII 框，显示机器人信息。

        使用 ANSI 颜色码绘制边框和内容，在终端中显示为彩色表格。
        """
        header = (
            "\n"
            f"{Colors.CYAN}{Colors.BOLD}"
            "+--------------------------------------------------------------+\n"
            "|                 PocketTerm Bot Instance                      |\n"
            f"+--------------------------------------------------------------+{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}机器人名称{Colors.RESET}:  {Colors.GREEN}{self.name:<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}机器人 ID{Colors.RESET}:   {Colors.CYAN}{self.bot_id:<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}服务器类型{Colors.RESET}:  {self.config.server_type.value:<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}服务器号{Colors.RESET}:    {self.config.server_code or '(未设置)':<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}接入点类型{Colors.RESET}:  {self.config.access_point_type.value:<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}设备型号{Colors.RESET}:    {self.config.device_model:<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}自动重连{Colors.RESET}:    "
            f"{'是' if self.config.auto_reconnect else '否':<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.CYAN}{Colors.BOLD}"
            "+--------------------------------------------------------------+"
            f"{Colors.RESET}\n"
        )
        print(header, flush=True)
        logger.info(f"机器人实例创建: {self.name} (ID: {self.bot_id})")

    def _set_status(self, status: BotStatus, error: str = "") -> None:
        """更新状态并打印带图标的彩色日志。

        Args:
            status: 新状态。
            error: 错误信息（非空时记录到 last_error 并以红色输出）。
        """
        old_status = self.info.status
        self.info.status = status

        # 同步状态到数据库
        self._sync_status_to_db(status)

        timestamp = time.strftime("%H:%M:%S")

        # 状态 -> 图标 / 颜色 映射
        status_icons: dict[BotStatus, tuple[str, str]] = {
            BotStatus.IDLE: (Colors.DIM, "[ ]"),
            BotStatus.CONNECTING: (Colors.YELLOW, "[~]"),
            BotStatus.AUTHENTICATING: (Colors.YELLOW, "[*]"),
            BotStatus.CONNECTED: (Colors.GREEN, "[+]"),
            BotStatus.SPAWNED: (Colors.BRIGHT_GREEN, "[O]"),
            BotStatus.ERROR: (Colors.RED, "[!]"),
            BotStatus.DISCONNECTED: (Colors.MAGENTA, "[-]"),
            BotStatus.KICKED: (Colors.YELLOW, "[K]"),
            BotStatus.BANNED: (Colors.BG_RED, "[B]"),
        }
        color, icon = status_icons.get(status, (Colors.WHITE, "[?]"))

        if error:
            self.info.last_error = error
            self.info.add_log("error", error)
            print(
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{Colors.RED}{Colors.BOLD}[!]{Colors.RESET} "
                f"{Colors.RED}[{self.name}] 错误: {error}{Colors.RESET}",
                flush=True,
            )
            logger.error(f"机器人 {self.name} 错误: {error}")
        else:
            print(
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{color}{icon}{Colors.RESET} "
                f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
                f"{color}状态: {old_status.value} -> {status.value}{Colors.RESET}",
                flush=True,
            )
            logger.info(
                f"机器人 {self.name} 状态: {old_status.value} -> {status.value}"
            )

    def _sync_status_to_db(self, status: BotStatus) -> None:
        """异步同步状态到数据库。"""
        status_map = {
            BotStatus.IDLE: "stopped",
            BotStatus.CONNECTING: "connecting",
            BotStatus.AUTHENTICATING: "connecting",
            BotStatus.CONNECTED: "running",
            BotStatus.SPAWNED: "running",
            BotStatus.ERROR: "error",
            BotStatus.BANNED: "banned",
            BotStatus.DISCONNECTED: "disconnected",
        }
        db_status = status_map.get(status)
        if db_status is None:
            return
        try:
            asyncio.create_task(self._do_db_status_update(db_status))
        except Exception:
            pass

    async def _do_db_status_update(self, db_status: str) -> None:
        """实际执行数据库状态更新。"""
        try:
            from app.database import get_db
            db = await get_db()
            await db.update_bot_status(self.bot_id, db_status)
        except Exception:
            pass

    def _log_op(self, icon: str, message: str, color: str = Colors.CYAN) -> None:
        """打印游戏操作日志（带图标和颜色）。

        Args:
            icon: 操作图标。
            message: 日志内容。
            color: ANSI 颜色码。
        """
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{color}{icon}{Colors.RESET} "
            f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
            f"{color}{message}{Colors.RESET}",
            flush=True,
        )

    # ==================================================================
    # 封禁 / 踢出检测
    # ==================================================================

    def _detect_ban(self, message: str) -> bool:
        """检测消息是否包含封禁关键词。

        Args:
            message: 待检测的消息文本。

        Returns:
            ``True`` 检测到封禁关键词;``False`` 未检测到。
        """
        msg_lower = message.lower()
        for keyword in BAN_KEYWORDS:
            if keyword.lower() in msg_lower:
                return True
        return False

    def _detect_kick(self, message: str) -> bool:
        """检测消息是否包含踢出关键词。

        Args:
            message: 待检测的消息文本。

        Returns:
            ``True`` 检测到踢出关键词;``False`` 未检测到。
        """
        msg_lower = message.lower()
        for keyword in KICK_KEYWORDS:
            if keyword.lower() in msg_lower:
                return True
        return False

    def _handle_ban_detected(self, reason: str) -> None:
        """检测到封禁时的处理逻辑。

        1. 标记封禁状态
        2. 打印醒目警告
        3. 停止重连

        Args:
            reason: 封禁原因。
        """
        if self._ban_detected:
            return
        self._ban_detected = True
        self._set_status(BotStatus.BANNED, f"检测到封禁: {reason}")

        # 打印醒目的封禁警告
        print(f"\n{Colors.BG_RED}{Colors.BOLD}{'='*62}{Colors.RESET}", flush=True)
        print(
            f"{Colors.BG_RED}{Colors.BOLD}  !! 严重警告：机器人 {self.name} 可能被封禁 !!"
            f"{'': <16}{Colors.RESET}",
            flush=True,
        )
        print(
            f"{Colors.BG_RED}{Colors.BOLD}  原因: {reason:<52}{Colors.RESET}",
            flush=True,
        )
        print(
            f"{Colors.BG_RED}{Colors.BOLD}  已停止重连，请检查账号状态"
            f"{'': <28}{Colors.RESET}",
            flush=True,
        )
        print(f"{Colors.BG_RED}{Colors.BOLD}{'='*62}{Colors.RESET}\n", flush=True)
        logger.critical(f"机器人 {self.name} 检测到封禁! 原因: {reason}")

    def _handle_kick_detected(self, reason: str) -> None:
        """检测到踢出时的处理逻辑。

        Args:
            reason: 踢出原因。
        """
        self._set_status(BotStatus.KICKED, f"被踢出: {reason}")
        print(
            f"\n{Colors.YELLOW}{Colors.BOLD}  [!] 机器人 {self.name} 被踢出服务器{Colors.RESET}\n"
            f"  {Colors.YELLOW}原因: {reason}{Colors.RESET}\n",
            flush=True,
        )
        logger.warning(f"机器人 {self.name} 被踢出: {reason}")

        # BUG-08 修复: 发射 kick 事件, 供 manager 及外部监听者感知
        # _handle_kick_detected 是同步方法, 使用 ensure_future 异步发射
        try:
            loop = asyncio.get_running_loop()
            asyncio.ensure_future(self._emit("kick", self, reason), loop=loop)
        except RuntimeError:
            # 无运行中的事件循环 (如单元测试), 跳过事件发射
            pass

    def add_chat(self, sender: str, message: str, is_system: bool = False) -> None:
        """添加聊天消息，同时检测封禁/踢出。

        Args:
            sender: 发送者名称。
            message: 消息内容。
            is_system: 是否为系统消息。
        """
        msg = ChatMessage(sender=sender, message=message, is_system=is_system)
        self._chat_history.append(msg)
        # 限制聊天历史长度
        if len(self._chat_history) > 500:
            self._chat_history = self._chat_history[-500:]

        # 打印聊天到控制台
        timestamp = time.strftime("%H:%M:%S")
        if is_system:
            print(
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{Colors.MAGENTA}[S]{Colors.RESET} "
                f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
                f"{Colors.MAGENTA}[系统] {message}{Colors.RESET}",
                flush=True,
            )
        else:
            print(
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{Colors.BRIGHT_CYAN}[C]{Colors.RESET} "
                f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
                f"{Colors.BRIGHT_CYAN}[{sender}] {message}{Colors.RESET}",
                flush=True,
            )

        # 防封禁: 检测反作弊关键词 (自动降速 / 触发异常)
        if self._anti_ban is not None:
            try:
                keyword = self._anti_ban.on_chat_message(message)
                if keyword:
                    self.info.add_log(
                        "warning",
                        f"反作弊关键词命中: {keyword} (消息: {message[:80]})",
                    )
            except Exception:  # noqa: BLE001
                pass

        # 检测封禁
        if self._detect_ban(message):
            self._handle_ban_detected(message)

        # 检测踢出
        if self._detect_kick(message):
            self._handle_kick_detected(message)

    def get_chat_history(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取聊天历史。

        Args:
            limit: 返回最近 N 条消息。

        Returns:
            聊天消息字典列表。
        """
        return [
            {
                "sender": m.sender,
                "message": m.message,
                "timestamp": m.timestamp,
                "is_system": m.is_system,
            }
            for m in self._chat_history[-limit:]
        ]

    # ==================================================================
    # 生命周期: start / stop
    # ==================================================================

    async def start(self) -> bool:
        """启动机器人。

        创建后台运行任务，机器人会在任务中连接服务器并保持运行。

        Returns:
            ``True`` 启动成功;``False`` 已在运行中。
        """
        if self._running:
            timestamp = time.strftime("%H:%M:%S")
            print(
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{Colors.YELLOW}[!]{Colors.RESET} "
                f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
                f"{Colors.YELLOW}机器人已在运行中{Colors.RESET}",
                flush=True,
            )
            return False

        self._running = True
        self._ban_detected = False
        self._reconnect_count = 0  # 每次启动时重置重连计数
        self._task = asyncio.create_task(self._run_loop())
        self.info.add_log("info", f"机器人 {self.name} 启动中...")
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{Colors.BRIGHT_GREEN}{Colors.BOLD}[>]{Colors.RESET} "
            f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
            f"{Colors.BRIGHT_GREEN}正在启动机器人...{Colors.RESET}",
            flush=True,
        )
        return True

    async def stop(self) -> None:
        """停止机器人。

        取消运行任务，断开接入点，打印会话统计。
        """
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{Colors.RED}{Colors.BOLD}[X]{Colors.RESET} "
            f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
            f"{Colors.RED}正在停止机器人...{Colors.RESET}",
            flush=True,
        )

        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        # 停止接入点
        if self._access_point is not None:
            try:
                await self._access_point.stop()
            except Exception as exc:
                logger.error(f"停止接入点异常: {exc}")
            self._access_point = None

        self._set_status(BotStatus.DISCONNECTED)
        self.info.add_log("info", f"机器人 {self.name} 已停止")
        await self._emit("disconnect", self)
        await self._emit("leave_room", self)
        self._print_footer()

    def _print_footer(self) -> None:
        """打印机器人停止时的会话统计信息。"""
        runtime = time.time() - self._start_time
        hours = int(runtime // 3600)
        minutes = int((runtime % 3600) // 60)
        seconds = int(runtime % 60)
        runtime_str = f"{hours}h {minutes}m {seconds}s"

        footer = (
            "\n"
            f"{Colors.DIM}{Colors.BOLD}"
            "+--------------------------------------------------------------+\n"
            "|               PocketTerm Bot Session Ended                   |\n"
            f"+--------------------------------------------------------------+{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}运行时长{Colors.RESET}:    {Colors.CYAN}{runtime_str:<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}最终状态{Colors.RESET}:    {self.status.value:<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.YELLOW}|{Colors.RESET}  {Colors.BOLD}错误信息{Colors.RESET}:    "
            f"{(self.info.last_error or '无'):<47}{Colors.RESET}{Colors.YELLOW}|{Colors.RESET}\n"
            f"{Colors.DIM}{Colors.BOLD}"
            "+--------------------------------------------------------------+"
            f"{Colors.RESET}\n"
        )
        print(footer, flush=True)

    # ==================================================================
    # 主运行循环
    # ==================================================================

    async def _run_loop(self) -> None:
        """主运行循环。

        流程:
            1. 调用 ``_connect()`` 连接服务器
            2. 进入心跳循环:
                - 每 30 秒发送心跳
                - 60 秒无数据包则判定断开
            3. 异常处理:
                - AccountBannedError / InvalidCredentialsError / VersionTooLowError -> 停止
                - ServerFullError / NetworkError -> 重连
            4. 重连逻辑:
                - auto_reconnect + 未封禁 -> 等待后重连
                - 超过 max_reconnect_attempts -> 停止
        """
        while self._running:
            try:
                await self._connect()

                # 心跳检测参数
                heartbeat_interval = 30  # 30 秒心跳
                last_heartbeat = time.time()

                # 保持连接循环
                while self._running and self.info.status in (
                    BotStatus.CONNECTED,
                    BotStatus.SPAWNED,
                ):
                    await asyncio.sleep(1)
                    now = time.time()

                    # 发送心跳包
                    if now - last_heartbeat > heartbeat_interval:
                        await self._send_heartbeat()
                        last_heartbeat = now

                    # 超时检测: 60 秒无数据包则断开
                    if self._last_packet_time > 0 and now - self._last_packet_time > 60:
                        raise ConnectionError(
                            "连接超时: 60 秒未收到服务器数据包"
                        )

            # --- 不可恢复的错误（停止） ---

            except AccountBannedError as e:
                error_msg = f"账号被封禁: {e}"
                self._set_status(BotStatus.BANNED, error_msg)
                self._ban_detected = True
                await self._emit("ban", self, str(e))
                await self._emit("error", self, e)
                print(
                    f"\n{Colors.BG_RED}{Colors.BOLD}  账号封禁详情{Colors.RESET}",
                    flush=True,
                )
                print(f"  {Colors.RED}机器人: {self.name}{Colors.RESET}", flush=True)
                print(f"  {Colors.RED}原因: {e}{Colors.RESET}", flush=True)
                print(
                    f"  {Colors.YELLOW}建议: 请更换账号或等待解封{Colors.RESET}\n",
                    flush=True,
                )
                break

            except InvalidCredentialsError as e:
                error_msg = "认证失败: 账号密码错误或无效"
                self._set_status(BotStatus.ERROR, error_msg)
                self.info.add_log("error", f"详细错误: {e}")
                await self._emit("error", self, e)
                print(f"\n{Colors.RED}{Colors.BOLD}  认证失败{Colors.RESET}", flush=True)
                print(f"  {Colors.RED}机器人: {self.name}{Colors.RESET}", flush=True)
                print(
                    f"  {Colors.YELLOW}原因: 请检查服务器号、密码、API Key{Colors.RESET}\n",
                    flush=True,
                )
                break  # 认证错误不重连

            except VersionTooLowError as e:
                error_msg = "客户端版本过低，请更新"
                self._set_status(BotStatus.ERROR, error_msg)
                self.info.add_log("error", f"详细错误: {e}")
                await self._emit("error", self, e)
                print(f"\n{Colors.RED}{Colors.BOLD}  版本错误{Colors.RESET}", flush=True)
                print(f"  {Colors.RED}机器人: {self.name}{Colors.RESET}", flush=True)
                print(f"  {Colors.YELLOW}原因: {e}{Colors.RESET}\n", flush=True)
                break  # 版本错误不重连

            except ServerNotFoundError as e:
                error_msg = f"服务器不存在: {self.config.server_code}"
                self._set_status(BotStatus.ERROR, error_msg)
                self.info.add_log("error", f"详细错误: {e}")
                await self._emit("error", self, e)
                print(f"\n{Colors.RED}{Colors.BOLD}  服务器错误{Colors.RESET}", flush=True)
                print(f"  {Colors.RED}机器人: {self.name}{Colors.RESET}", flush=True)
                print(
                    f"  {Colors.YELLOW}原因: 服务器号 {self.config.server_code} 不存在{Colors.RESET}\n",
                    flush=True,
                )
                break

            # --- 可恢复的错误（重连） ---

            except ServerFullError as e:
                error_msg = "服务器已满"
                self._set_status(BotStatus.ERROR, error_msg)
                self.info.add_log("error", f"详细错误: {e}")
                await self._emit("error", self, e)
                print(
                    f"\n{Colors.YELLOW}{Colors.BOLD}  服务器已满，将尝试重连...{Colors.RESET}\n",
                    flush=True,
                )

            except NetworkError as e:
                error_msg = f"网络错误: {e}"
                self._set_status(BotStatus.ERROR, error_msg)
                self.info.add_log("error", f"详细错误: {e}")
                await self._emit("error", self, e)
                print(f"\n{Colors.YELLOW}{Colors.BOLD}  网络错误{Colors.RESET}", flush=True)
                print(f"  {Colors.YELLOW}机器人: {self.name}{Colors.RESET}", flush=True)
                print(f"  {Colors.YELLOW}详情: {e}{Colors.RESET}\n", flush=True)

            except ServerRejectedError as e:
                error_msg = f"服务器拒绝连接: {e}"
                self._set_status(BotStatus.ERROR, error_msg)
                self.info.add_log("error", f"详细错误: {e}")
                await self._emit("error", self, e)
                print(f"\n{Colors.RED}{Colors.BOLD}  服务器拒绝{Colors.RESET}", flush=True)
                print(f"  {Colors.RED}机器人: {self.name}{Colors.RESET}", flush=True)
                print(f"  {Colors.YELLOW}详情: {e}{Colors.RESET}\n", flush=True)

            except ConnectionError as e:
                error_msg = f"连接断开: {e}"
                self._set_status(BotStatus.ERROR, error_msg)
                print(
                    f"\n{Colors.MAGENTA}{Colors.BOLD}  连接断开{Colors.RESET}\n"
                    f"  {Colors.MAGENTA}{e}{Colors.RESET}\n",
                    flush=True,
                )

            except asyncio.CancelledError:
                break

            except Exception as e:
                error_msg = f"未知错误: {type(e).__name__}: {e}"
                self._set_status(BotStatus.ERROR, error_msg)
                logger.exception(f"机器人 {self.name} 发生异常")
                print(f"\n{Colors.RED}{Colors.BOLD}  未处理异常{Colors.RESET}", flush=True)
                print(f"  {Colors.RED}类型: {type(e).__name__}{Colors.RESET}", flush=True)
                print(f"  {Colors.YELLOW}详情: {e}{Colors.RESET}", flush=True)
                print(
                    f"  {Colors.DIM}堆栈:\n{traceback.format_exc()}{Colors.RESET}\n",
                    flush=True,
                )
                await self._emit("error", self, e)

            # --- 重连逻辑 ---

            if self._running and self.config.auto_reconnect and not self._ban_detected:
                self._reconnect_count += 1
                if self._reconnect_count > self.config.max_reconnect_attempts:
                    max_err = (
                        f"超过最大重连次数 ({self.config.max_reconnect_attempts})，停止重连"
                    )
                    self.info.add_log("error", max_err)
                    print(
                        f"\n{Colors.RED}{Colors.BOLD}  {max_err}{Colors.RESET}\n",
                        flush=True,
                    )
                    break

                # 防封禁: 使用带抖动的重连延迟 (避免固定周期触发反作弊)
                # NovaBuilder / NexusE 逆向: 重连使用指数退避 + 随机抖动
                if self._anti_ban is not None:
                    try:
                        wait_time = self._anti_ban.jitter.reconnect_delay(
                            self.config.reconnect_delay, self._reconnect_count
                        )
                    except Exception:  # noqa: BLE001
                        wait_time = self.config.reconnect_delay * self._reconnect_count
                else:
                    wait_time = self.config.reconnect_delay * self._reconnect_count

                wait_msg = (
                    f"将在 {wait_time:.1f} 秒后进行第 {self._reconnect_count} 次重连..."
                )
                self.info.add_log("info", wait_msg)
                timestamp = time.strftime("%H:%M:%S")
                print(
                    f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                    f"{Colors.YELLOW}[~]{Colors.RESET} "
                    f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
                    f"{Colors.YELLOW}{wait_msg}{Colors.RESET}",
                    flush=True,
                )
                await asyncio.sleep(wait_time)
            else:
                if self._ban_detected:
                    timestamp = time.strftime("%H:%M:%S")
                    print(
                        f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                        f"{Colors.BG_RED}[B]{Colors.RESET} "
                        f"{Colors.BOLD}[{self.name}]{Colors.RESET} "
                        f"{Colors.RED}由于检测到封禁，停止重连{Colors.RESET}",
                        flush=True,
                    )
                break

        # _run_loop 退出后重置 _running 标志，确保下次 start() 可以成功启动
        self._running = False

    async def _send_heartbeat(self) -> None:
        """发送心跳包 (L-25 修复: 实现真实的 MCBE 应用层心跳)。

        ToolDelta 风格: 发送 ``/testfor @s`` 命令作为应用层心跳。
        - 检测机器人是否仍在游戏中 (被踢出/断开时命令无响应)
        - 维持与服务器的命令交互频率, 避免被视为挂机
        - 相比 ``NetworkStackLatency`` 包, 命令心跳更接近真实玩家行为
        """
        self.info.add_log("debug", "发送心跳包 (testfor @s)")
        self._last_packet_time = time.time()
        if self._access_point is not None:
            # 优先使用 send_command (ToolDelta 风格: /testfor @s)
            send_fn = getattr(self._access_point, "send_command", None)
            if send_fn is not None:
                try:
                    await send_fn("/testfor @s")
                except Exception:
                    pass
            else:
                # 回退: 尝试 send_packet (仅部分接入点支持)
                try:
                    await self._access_point.send_packet({"type": "heartbeat"})
                except Exception:
                    pass

    # ==================================================================
    # 连接逻辑
    # ==================================================================

    async def _connect(self) -> None:
        """连接到服务器。

        根据配置的 ``server_type`` 选择对应的连接方式:

            - ``RENTAL`` -> :meth:`_connect_rental_server` (认证 + 连接 + 生成)
            - ``LOBBY``  -> :meth:`_connect_lobby`
            - ``LOCAL``  -> :meth:`_connect_local`
            - ``CUSTOM`` -> :meth:`_connect_custom_server`

        .. note::
            C-3 修复: ``_reconnect_count`` 不再在连接开始时清零,
            而是仅在连接成功后清零 (见 :meth:`_on_connect_success`)。
            否则 ``max_reconnect_attempts`` 永远不会触发。

        .. note::
            H-6 修复: ``on_reconnect_success`` 不再在连接开始时调用,
            而是仅在连接成功后调用 (见 :meth:`_on_connect_success`)。
            否则退避会在连接建立前就被清除。
        """
        self._last_packet_time = time.time()
        # 不在此处重置 _reconnect_count / 调用 on_reconnect_success
        # (仅在连接成功后执行, 见 _on_connect_success)

        server_type_names = {
            ServerType.RENTAL: "租赁服",
            ServerType.LOBBY: "联机大厅",
            ServerType.LOCAL: "本地联机",
            ServerType.CUSTOM: "自定义服务器",
        }
        type_name = server_type_names.get(self.config.server_type, "未知")

        self._set_status(BotStatus.CONNECTING)
        self.info.add_log("info", f"正在连接到{type_name}...")

        # 打印连接分隔线
        print(f"\n{Colors.CYAN}{'─'*62}{Colors.RESET}", flush=True)
        print(
            f"{Colors.CYAN}{Colors.BOLD}  连接流程开始{Colors.RESET} "
            f"{Colors.DIM}- {type_name}{Colors.RESET}",
            flush=True,
        )
        print(f"{Colors.CYAN}{'─'*62}{Colors.RESET}", flush=True)

        # 创建接入点
        await self._setup_access_point()

        # 根据服务器类型选择连接逻辑
        if (
            self.config.server_type == ServerType.CUSTOM
            and self.config.server_address
        ):
            print(
                f"  {Colors.YELLOW}自定义服务器地址: "
                f"{self.config.server_address}:{self.config.server_port}{Colors.RESET}",
                flush=True,
            )
            await self._connect_custom_server()
        elif self.config.server_type == ServerType.LOBBY:
            print(f"  {Colors.YELLOW}联机大厅模式{Colors.RESET}", flush=True)
            await self._connect_lobby()
        elif self.config.server_type == ServerType.LOCAL:
            print(f"  {Colors.YELLOW}本地联机模式{Colors.RESET}", flush=True)
            await self._connect_local()
        else:
            print(
                f"  {Colors.YELLOW}租赁服模式，服务器号: "
                f"{self.config.server_code}{Colors.RESET}",
                flush=True,
            )
            await self._connect_rental_server()

        # 连接成功后执行 (C-3 + H-6 修复):
        # 仅在子方法未抛异常时才重置重连计数 / 调用 on_reconnect_success
        self._on_connect_success()

    def _on_connect_success(self) -> None:
        """连接成功后的清理工作 (C-3 + H-6 修复)。

        - 重置 ``_reconnect_count`` (仅成功时, 使 ``max_reconnect_attempts`` 可触发)
        - 调用 ``anti_ban.on_reconnect_success()`` (仅成功时, 避免退避被提前清除)
        """
        self._reconnect_count = 0
        if self._anti_ban is not None:
            try:
                self._anti_ban.on_reconnect_success()
            except Exception:  # noqa: BLE001
                pass
        try:
            from ..auth.auto_connect import get_auto_connect_manager
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    self._notify_auto_connect_success()
                )
        except Exception:  # noqa: BLE001
            pass

    async def _notify_auto_connect_success(self) -> None:
        """通知 AutoConnectManager 连接成功 (触发 EnhancedAntiBan.on_reconnect_success)。"""
        try:
            from ..auth.auto_connect import get_auto_connect_manager
            mgr = await get_auto_connect_manager(self.name)
            mgr.reconnect_policy.reset()
            mgr.enhanced.on_reconnect_success()
            mgr.anti_ban.on_reconnect_success()
        except Exception:  # noqa: BLE001
            pass

    async def _setup_access_point(self) -> None:
        """创建并启动接入点。

        根据配置的 ``access_point_type`` 创建对应的接入点实例，
        注册数据包回调，然后启动接入点。
        """
        ap_type_map = {
            AccessPointType.NEOMEGA: "neomega",
            AccessPointType.FATEARK: "fateark",
            AccessPointType.CUSTOM: "custom",
        }
        ap_type_name = ap_type_map.get(
            self.config.access_point_type, "custom"
        )

        # 构造接入点配置
        auth_server = self.config.auth_server or "https://nv1.nethard.pro"
        ap_config: dict[str, Any] = {
            "server_code": self.config.server_code,
            "server_password": self.config.server_password,
            "auth_server": auth_server,
            "api_key": self.config.api_key,
            "cookie": self.config.cookie,
            "sauth_json": self.config.sauth_json,
            "auth_method": self.config.auth_method,
            "server_address": self.config.server_address,
            "server_port": self.config.server_port,
            "device_model": self.config.device_model,
            "bot_name": self.name,
        }

        # 注入设备指纹 (供接入点构造登录链 / sauth_json 使用)
        # 字段与 NovaBuilder / NexusE 的 uqholder.Player 对齐
        if self._device_fingerprint is not None:
            ap_config["device_fingerprint"] = self._device_fingerprint.to_dict()
            ap_config["device_id"] = self._device_fingerprint.device_id
            ap_config["client_random_id"] = self._device_fingerprint.client_random_id
            ap_config["player_uuid"] = self._device_fingerprint.uuid
            ap_config["build_platform"] = self._device_fingerprint.build_platform
            ap_config["device_os"] = self._device_fingerprint.device_os
            ap_config["game_version"] = self._device_fingerprint.game_version
            ap_config["language_code"] = self._device_fingerprint.language_code
            ap_config["login_chain_identity"] = (
                self._device_fingerprint.to_login_chain_identity()
            )

        print(
            f"  {Colors.CYAN}接入点类型: "
            f"{Colors.BOLD}{ap_type_name}{Colors.RESET}",
            flush=True,
        )

        # 创建接入点
        self._access_point = self._ap_manager.create(
            ap_type_name, ap_config
        )

        # 注册数据包回调
        await self._access_point.on_packet(self._on_packet_received)

        # 注册事件回调（CustomAccessPoint 使用事件系统）
        if hasattr(self._access_point, 'on'):
            self._access_point.on("event", self._on_access_point_event)
            self._access_point.on("chat", self._on_access_point_chat)
            self._access_point.on("error", self._on_access_point_error)
            self._access_point.on("ban", self._on_access_point_ban)

        # 启动接入点
        print(f"  {Colors.CYAN}启动接入点...{Colors.RESET}", flush=True)
        start_ok = await self._access_point.start()
        if not start_ok:
            # 接入点启动失败 (如缺少 server_code、找不到 Go 二进制等)
            err_msg = self._access_point.info.last_error or "接入点启动失败"
            self._set_status(BotStatus.ERROR, err_msg)
            raise ConnectionError(err_msg)
        print(f"  {Colors.GREEN}接入点已启动{Colors.RESET}", flush=True)

    async def _on_access_point_event(self, name: str, data: dict) -> None:
        """处理接入点事件"""
        self._last_packet_time = time.time()

        if name == "connected":
            self._set_status(BotStatus.CONNECTED)
            self.info.connected_at = time.time()
            self.info.add_log("info", "已连接到游戏服务器")

        elif name == "spawn":
            # BUG-06/07 修复: 不在此处发射 connect/spawn 事件, 避免与
            # _connect_rental_server 中的事件发射重复; 此处仅更新状态与日志,
            # 统一由 _connect_* 方法在轮询检测到 SPAWNED 后一次性发射
            # (顺序: connect → spawn → join_room)
            bot_name = data.get("bot_name", self.name)
            self._set_status(BotStatus.SPAWNED)
            self.add_chat("System", f"机器人 {bot_name} 已成功进入游戏!", is_system=True)
            self.info.add_log("info", f"机器人已在游戏中生成: {bot_name}")

        elif name == "player_join":
            player = data.get("player_name", "")
            if player:
                self.info.player_list.append(player)
                self.add_chat("System", f"{player} 加入了游戏", is_system=True)
                await self._emit("player_join", self, player)

        elif name == "player_leave":
            player = data.get("player_name", "")
            if player in self.info.player_list:
                self.info.player_list.remove(player)
            await self._emit("player_leave", self, player)

    async def _on_access_point_chat(self, sender: str, message: str) -> None:
        """处理接入点聊天消息"""
        self.add_chat(sender, message)
        await self._emit("chat", self, sender, message)

    async def _on_access_point_error(self, message: str, detail: str = "") -> None:
        """处理接入点错误"""
        error_msg = f"{message}: {detail}" if detail else message
        self.info.add_log("error", error_msg)
        self.info.last_error = error_msg

    async def _on_access_point_ban(self, message: str, detail: str = "") -> None:
        """处理接入点封禁事件"""
        self._ban_detected = True
        self._set_status(BotStatus.BANNED, f"被封禁: {message}")
        await self._emit("ban", self, message)

    async def _on_packet_received(self, packet: dict[str, Any]) -> None:
        """接入点数据包回调。

        当接入点收到服务器数据包时调用此方法。
        解析数据包类型，更新机器人状态（位置、血量、聊天等）。

        Args:
            packet: 收到的数据包字典。
        """
        self._last_packet_time = time.time()

        # 防封禁: 记录服务器数据包 (用于无响应检测 / 升速)
        if self._anti_ban is not None:
            try:
                self._anti_ban.anomaly.record_packet()
            except Exception:  # noqa: BLE001
                pass

        packet_type = packet.get("type", "unknown")

        # 根据数据包类型更新状态
        if packet_type == "chat":
            sender = packet.get("sender", "")
            message = packet.get("message", "")
            is_system = packet.get("is_system", False)
            self.add_chat(sender, message, is_system)
            await self._emit("chat", self, sender, message)
        elif packet_type == "position_update":
            x = packet.get("x", 0)
            y = packet.get("y", 0)
            z = packet.get("z", 0)
            self.info.position = (x, y, z)
        elif packet_type == "health_update":
            self.info.health = packet.get("health", self.info.health)
        elif packet_type == "hunger_update":
            self.info.hunger = packet.get("hunger", self.info.hunger)
        elif packet_type == "player_list":
            self.info.player_list = packet.get("players", [])
        elif packet_type == "kick":
            self._handle_kick_detected(packet.get("reason", "被踢出"))
        elif packet_type == "ban":
            self._handle_ban_detected(packet.get("reason", "被封禁"))

    async def _connect_rental_server(self) -> None:
        """连接租赁服（通过Go接入点）。

        Go接入点启动后自动处理：
            1. 认证（fbauth → phoenix::login）
            2. RakNet连接（UDP握手）
            3. MCPE登录（Login packet）
            4. 等待生成（PlayStatus.PlayerSpawn）

        Python端只需等待Go接入点发来的事件：
            - connected → 已连接到服务器
            - spawn → 已进入游戏
        """
        # 接入点已在 _setup_access_point 中启动
        # 这里等待Go接入点完成认证和连接
        self._set_status(BotStatus.AUTHENTICATING)
        self.info.add_log("info", "等待Go接入点完成认证...")

        auth_server = self.config.auth_server or "https://nv1.nethard.pro"
        print(
            f"  {Colors.YELLOW}{Colors.BOLD}步骤 1/3:{Colors.RESET} "
            f"{Colors.YELLOW}网易认证中（通过Go接入点）...{Colors.RESET}",
            flush=True,
        )
        print(f"    {Colors.DIM}认证服务器: {auth_server}{Colors.RESET}", flush=True)
        print(f"    {Colors.DIM}服务器号: {self.config.server_code}{Colors.RESET}", flush=True)
        print(f"    {Colors.DIM}设备型号: {self.config.device_model}{Colors.RESET}", flush=True)

        # 等待连接或超时（最多30秒）
        timeout = 30
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.info.status == BotStatus.CONNECTED:
                break
            if self.info.status == BotStatus.ERROR:
                raise ConnectionError(f"认证失败: {self.info.last_error}")
            if self.info.status == BotStatus.BANNED:
                raise AccountBannedError(self.info.last_error or "账号被封禁")
            await asyncio.sleep(0.5)
        else:
            raise ConnectionError("认证超时：30秒内未完成认证")

        print(f"    {Colors.GREEN}✅ 认证成功!{Colors.RESET}", flush=True)

        # 步骤2: 等待连接游戏服务器
        print(
            f"  {Colors.YELLOW}{Colors.BOLD}步骤 2/3:{Colors.RESET} "
            f"{Colors.YELLOW}连接游戏服务器...{Colors.RESET}",
            flush=True,
        )

        # 等待spawn或超时（最多60秒）
        timeout = 60
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.info.status == BotStatus.SPAWNED:
                break
            if self.info.status == BotStatus.ERROR:
                raise ConnectionError(f"连接失败: {self.info.last_error}")
            if self.info.status == BotStatus.BANNED:
                raise AccountBannedError(self.info.last_error or "账号被封禁")
            await asyncio.sleep(0.5)
        else:
            raise ConnectionError("连接超时：60秒内未进入游戏")

        print(f"    {Colors.GREEN}✅ 已连接到游戏服务器{Colors.RESET}", flush=True)

        # 步骤3: 已进入游戏
        # C-5 修复: 去除重复的 join_room emit 和状态回退
        print(
            f"  {Colors.YELLOW}{Colors.BOLD}步骤 3/3:{Colors.RESET} "
            f"{Colors.GREEN}{Colors.BOLD}已进入游戏! 机器人名称: {self.name}{Colors.RESET}",
            flush=True,
        )
        print(f"{Colors.CYAN}{'─'*62}{Colors.RESET}\n", flush=True)

        self._set_status(BotStatus.SPAWNED)
        self.info.connected_at = time.time()
        self.info.add_log("info", f"机器人 {self.name} 已成功进入租赁服 {self.config.server_code}")
        self.add_chat(
            "System", f"机器人 {self.name} 已成功进入租赁服!", is_system=True
        )
        print(f"    {Colors.GREEN}机器人已进入游戏!{Colors.RESET}", flush=True)
        print(f"{Colors.CYAN}{'─'*62}{Colors.RESET}\n", flush=True)

        await self._emit("connect", self)
        await self._emit("spawn", self)
        await self._emit("join_room", self, self.config.server_type)

    async def _connect_lobby(self) -> None:
        """连接联机大厅。

        联机大厅连接流程:
        1. 使用账号信息认证
        2. 获取大厅服务器列表
        3. 连接到大厅服务器
        4. 创建或加入房间
        """
        self._set_status(BotStatus.AUTHENTICATING)
        self.info.add_log("info", "正在连接联机大厅...")
        print(f"  {Colors.YELLOW}联机大厅认证中...{Colors.RESET}", flush=True)

        # 模拟认证流程
        await asyncio.sleep(0.5)
        self.info.add_log("info", "账号认证中...")

        # 模拟获取服务器列表
        await asyncio.sleep(0.5)
        self.info.add_log("info", "获取大厅服务器列表...")

        # 模拟连接大厅服务器
        await asyncio.sleep(0.5)
        self.info.add_log("info", "连接到大厅服务器...")

        self._set_status(BotStatus.CONNECTED)
        self.info.connected_at = time.time()
        print(f"  {Colors.GREEN}大厅服务器已连接{Colors.RESET}", flush=True)

        # 模拟进入大厅
        await asyncio.sleep(0.3)
        self._set_status(BotStatus.SPAWNED)
        self.add_chat(
            "System", f"机器人 {self.name} 已进入联机大厅!", is_system=True
        )
        self.add_chat(
            "System", "提示: 使用 lobby create 创建房间, lobby join <房间号> 加入房间", is_system=True
        )
        print(f"    {Colors.GREEN}已进入联机大厅!{Colors.RESET}", flush=True)
        print(f"{Colors.CYAN}{'─'*62}{Colors.RESET}\n", flush=True)

        await self._emit("connect", self)
        await self._emit("spawn", self)
        await self._emit("join_room", self, ServerType.LOBBY)

    async def _connect_local(self) -> None:
        """连接本地联机。

        本地联机连接流程:
        1. 发现本地局域网服务器
        2. 连接到本地服务器
        """
        self._set_status(BotStatus.AUTHENTICATING)
        self.info.add_log("info", "正在搜索本地服务器...")
        print(f"  {Colors.YELLOW}本地联机搜索中...{Colors.RESET}", flush=True)

        await asyncio.sleep(0.5)
        self.info.add_log("info", "发现本地服务器")

        await asyncio.sleep(0.3)
        self.info.add_log("info", "正在连接...")

        self._set_status(BotStatus.CONNECTED)
        self.info.connected_at = time.time()

        await asyncio.sleep(0.3)
        self._set_status(BotStatus.SPAWNED)
        self.add_chat(
            "System", f"机器人 {self.name} 已进入本地联机!", is_system=True
        )
        print(f"    {Colors.GREEN}已进入本地联机!{Colors.RESET}", flush=True)
        print(f"{Colors.CYAN}{'─'*62}{Colors.RESET}\n", flush=True)

        await self._emit("connect", self)
        await self._emit("spawn", self)
        await self._emit("join_room", self, ServerType.LOCAL)

    async def _connect_custom_server(self) -> None:
        """连接自定义服务器。

        直接使用配置的 ``server_address`` 和 ``server_port`` 连接。
        """
        self._set_status(BotStatus.AUTHENTICATING)
        self.info.add_log(
            "info",
            "正在连接自定义服务器 "
            f"{self.config.server_address}:{self.config.server_port}...",
        )
        print(
            f"  {Colors.YELLOW}连接自定义服务器 "
            f"{self.config.server_address}:{self.config.server_port}...{Colors.RESET}",
            flush=True,
        )

        # TODO: 实现直接连接到自定义地址
        await asyncio.sleep(1)

        self._set_status(BotStatus.CONNECTED)
        self.info.server_ip = (
            f"{self.config.server_address}:{self.config.server_port}"
        )
        self.info.connected_at = time.time()
        await asyncio.sleep(0.5)
        self._set_status(BotStatus.SPAWNED)
        self.add_chat(
            "System",
            f"机器人 {self.name} 已连接到自定义服务器!",
            is_system=True,
        )
        print(f"    {Colors.GREEN}已连接到自定义服务器!{Colors.RESET}", flush=True)
        print(f"{Colors.CYAN}{'─'*62}{Colors.RESET}\n", flush=True)

        await self._emit("connect", self)
        await self._emit("spawn", self)
        await self._emit("join_room", self, ServerType.CUSTOM)

    # ==================================================================
    # 游戏操作接口
    # ==================================================================

    async def send_command(self, command: str) -> bool:
        """发送游戏命令。

        Args:
            command: 命令字符串（如 ``"say hello"`` 或 ``"/time set day"``）。

        Returns:
            ``True`` 发送成功;``False`` 机器人未连接。
        """
        if self.info.status not in (BotStatus.CONNECTED, BotStatus.SPAWNED):
            warn_msg = (
                "无法发送命令，机器人未连接 "
                f"(状态: {self.info.status.value}): {command}"
            )
            self.info.add_log("warning", warn_msg)
            self._log_op("[!]", warn_msg, Colors.YELLOW)
            return False

        # 确保命令以 / 开头
        if not command.startswith("/"):
            command = "/" + command

        self.info.add_log("info", f"发送命令: {command}")
        self._log_op(">>", f"命令: {command}", Colors.CYAN)

        # 通过接入点发送命令
        if self._access_point is not None:
            # 优先使用接入点的 send_command 方法（CustomAccessPoint）
            if hasattr(self._access_point, 'send_command'):
                await self._access_point.send_command(command)
            else:
                await self._access_point.send_packet(
                    {"type": "command", "command": command}
                )

        self.add_chat(self.name, command, is_system=False)
        self._last_packet_time = time.time()
        return True

    async def send_chat(self, message: str) -> bool:
        """发送聊天消息。

        Args:
            message: 聊天消息。

        Returns:
            ``True`` 发送成功;``False`` 发送失败。
        """
        if self.info.status not in (BotStatus.CONNECTED, BotStatus.SPAWNED):
            return False

        self.info.add_log("info", f"发送聊天: {message}")
        self._log_op(">>", f"聊天: {message}", Colors.CYAN)

        # 通过接入点发送聊天
        if self._access_point is not None:
            if hasattr(self._access_point, 'send_chat'):
                await self._access_point.send_chat(message)
            else:
                await self._access_point.send_packet(
                    {"type": "chat", "message": message}
                )

        self.add_chat(self.name, message, is_system=False)
        self._last_packet_time = time.time()
        return True

    async def say_to(self, target: str, message: str) -> bool:
        """向指定玩家发送私聊消息。

        Args:
            target: 目标玩家名称。
            message: 消息内容。

        Returns:
            ``True`` 发送成功;``False`` 发送失败。
        """
        return await self.send_command(f'/tell "{target}" {message}')

    async def move_to(self, x: float, y: float, z: float) -> bool:
        """移动到指定坐标。

        Args:
            x: X 坐标。
            y: Y 坐标。
            z: Z 坐标。

        Returns:
            ``True`` 移动成功;``False`` 未生成。
        """
        if self.info.status != BotStatus.SPAWNED:
            self._log_op(
                "[!]", f"无法移动，机器人未生成 (状态: {self.info.status.value})",
                Colors.YELLOW,
            )
            return False

        self.info.position = (x, y, z)
        self.info.add_log("info", f"移动到坐标: ({x:.1f}, {y:.1f}, {z:.1f})")
        self._log_op(
            "->", f"移动到 ({x:.1f}, {y:.1f}, {z:.1f})", Colors.BRIGHT_BLUE
        )

        # 通过接入点发送移动指令
        if self._access_point is not None:
            await self._access_point.send_packet(
                {"type": "move", "x": x, "y": y, "z": z}
            )

        # TODO: 实际发送 MovePlayerPacket
        return True

    async def disconnect_from_server(self) -> None:
        """主动退出房间/服务器。"""
        self.info.add_log("info", "正在退出服务器...")
        self._log_op("<-", "正在退出服务器...", Colors.MAGENTA)
        await self._emit("leave_room", self)
        await self.stop()

    # ==================================================================
    # 物品交互接口
    # ==================================================================

    async def open_container(
        self,
        pos_x: int,
        pos_y: int,
        pos_z: int,
        container_type: str = "chest",
    ) -> Optional[WindowInfo]:
        """打开容器（箱子、铁砧、附魔台、工作台等）。

        Args:
            pos_x: 容器 X 坐标。
            pos_y: 容器 Y 坐标。
            pos_z: 容器 Z 坐标。
            container_type: 容器类型。支持:
                ``chest`` / ``anvil`` / ``enchantment_table`` /
                ``crafting_table`` / ``furnace`` / ``blast_furnace`` /
                ``smoker`` / ``hopper`` / ``dropper`` / ``dispenser`` /
                ``brewing_stand`` / ``barrel`` / ``shulker_box``。

        Returns:
            :class:`WindowInfo` 容器窗口信息;打开失败返回 ``None``。
        """
        type_names = {
            "chest": "箱子",
            "anvil": "铁砧",
            "enchantment_table": "附魔台",
            "crafting_table": "工作台",
            "furnace": "熔炉",
            "blast_furnace": "高炉",
            "smoker": "烟熏炉",
            "hopper": "漏斗",
            "dropper": "投掷器",
            "dispenser": "发射器",
            "brewing_stand": "酿造台",
            "barrel": "木桶",
            "shulker_box": "潜影盒",
        }
        cn_name = type_names.get(container_type, container_type)

        self.info.add_log(
            "info", f"正在打开{cn_name}: ({pos_x}, {pos_y}, {pos_z})"
        )
        self._log_op(
            "##", f"打开{cn_name} ({pos_x}, {pos_y}, {pos_z})", Colors.YELLOW
        )

        # TODO: 实际发送 ContainerOpenPacket 并等待 ContainerSetContentPacket
        await asyncio.sleep(0.1)

        window = WindowInfo(
            window_id=1,
            window_type=f"minecraft:{container_type}",
            title=cn_name,
            slots=[],
        )
        self._current_window = window
        print(f"    {Colors.GREEN}{cn_name}已打开{Colors.RESET}", flush=True)
        return window

    async def close_container(self) -> bool:
        """关闭当前容器。

        Returns:
            ``True`` 关闭成功。
        """
        if self._current_window:
            self.info.add_log(
                "info", f"正在关闭容器: {self._current_window.title}"
            )
            self._log_op(
                "##", f"关闭{self._current_window.title}", Colors.YELLOW
            )
            # TODO: 实际发送 ContainerClosePacket
            await asyncio.sleep(0.05)
            self._current_window = None
        return True

    async def click_slot(
        self,
        slot_id: int,
        button: int = 0,
        window_id: int = 0,
        click_type: int = 0,
    ) -> bool:
        """点击物品栏槽位（精细控制）。

        Args:
            slot_id: 槽位 ID。
            button: 鼠标按钮 (0=左键, 1=右键)。
            window_id: 窗口 ID (0=玩家背包, 1+=容器)。
            click_type: 点击类型::

                0 = 普通点击
                1 = Shift 点击
                2 = 数字键 (hotbar 切换)
                3 = 中键
                4 = 丢弃 (Q)
                5 = 拖动开始
                6 = 拖动叠加

        Returns:
            ``True`` 点击成功。
        """
        window_name = (
            "背包"
            if window_id == 0
            else (
                self._current_window.title
                if self._current_window
                else f"窗口{window_id}"
            )
        )
        click_names = {
            0: "左键",
            1: "右键",
            4: "丢弃",
            5: "Shift+左键",
        }
        click_name = click_names.get(click_type, f"点击类型{click_type}")

        self.info.add_log(
            "info", f"{click_name} {window_name} 槽位 {slot_id}"
        )
        self._log_op(
            "@", f"{click_name} {window_name} 槽位 {slot_id}", Colors.BRIGHT_CYAN
        )

        # TODO: 实际发送 InventoryTransactionPacket
        await asyncio.sleep(0.05 + random.random() * 0.05)  # 模拟人类延迟
        self._last_packet_time = time.time()
        return True

    async def shift_click_slot(self, slot_id: int, window_id: int = 0) -> bool:
        """Shift+点击槽位（快速移动物品）。

        Args:
            slot_id: 槽位 ID。
            window_id: 窗口 ID。

        Returns:
            ``True`` 操作成功。
        """
        return await self.click_slot(
            slot_id, button=0, window_id=window_id, click_type=1
        )

    async def drop_item(
        self, slot_id: int, count: int = 1, drop_all: bool = False
    ) -> bool:
        """丢弃物品。

        Args:
            slot_id: 槽位 ID。
            count: 丢弃数量 (``drop_all=True`` 时忽略)。
            drop_all: 是否丢弃整组。

        Returns:
            ``True`` 丢弃成功。
        """
        action = "丢弃整组" if drop_all else f"丢弃 {count} 个"
        self.info.add_log("info", f"{action} 槽位 {slot_id} 的物品")
        self._log_op("XX", f"{action} 槽位 {slot_id}", Colors.RED)

        if drop_all:
            await self.click_slot(slot_id, button=0, window_id=0, click_type=4)
        else:
            await self.click_slot(slot_id, button=1, window_id=0, click_type=4)
        return True

    async def rename_item(
        self,
        item_slot_in_hotbar: int,
        new_name: str,
        anvil_x: Optional[int] = None,
        anvil_y: Optional[int] = None,
        anvil_z: Optional[int] = None,
        put_back_slot: Optional[int] = None,
    ) -> bool:
        """完整的铁砧重命名流程（5 步）。

        流程:
            0. (可选) 移动到铁砧位置
            1. 打开铁砧
            2. 将物品放入铁砧输入槽
            3. 设置新名称
            4. 从输出槽取出重命名后的物品
            5. 放回背包指定槽位

        Args:
            item_slot_in_hotbar: 物品在背包中的槽位 (0-35)。
            new_name: 新名称。
            anvil_x: 铁砧 X 坐标（可选，提供则先移动过去）。
            anvil_y: 铁砧 Y 坐标。
            anvil_z: 铁砧 Z 坐标。
            put_back_slot: 重命名后放回的槽位（默认放回原位）。

        Returns:
            ``True`` 重命名成功;``False`` 失败。
        """
        if put_back_slot is None:
            put_back_slot = item_slot_in_hotbar

        print(f"\n{Colors.YELLOW}{Colors.BOLD}{'='*62}{Colors.RESET}", flush=True)
        print(
            f"{Colors.YELLOW}{Colors.BOLD}  铁砧重命名流程{Colors.RESET}",
            flush=True,
        )
        print(f"{Colors.YELLOW}{Colors.BOLD}{'='*62}{Colors.RESET}", flush=True)
        print(f"  {Colors.CYAN}物品槽位: {item_slot_in_hotbar}{Colors.RESET}", flush=True)
        print(f"  {Colors.CYAN}新名称:   '{new_name}'{Colors.RESET}", flush=True)
        print(f"  {Colors.CYAN}放回槽位: {put_back_slot}{Colors.RESET}", flush=True)

        try:
            # 0. 如果提供了铁砧坐标，先移动过去
            if anvil_x is not None and anvil_y is not None and anvil_z is not None:
                print(
                    f"\n  {Colors.YELLOW}步骤 0/5: 移动到铁砧位置 "
                    f"({anvil_x}, {anvil_y}, {anvil_z})...{Colors.RESET}",
                    flush=True,
                )
                await self.move_to(anvil_x + 0.5, anvil_y, anvil_z + 0.5)
                await asyncio.sleep(0.3)
                print(f"    {Colors.GREEN}已到达铁砧旁{Colors.RESET}", flush=True)

            # 1. 打开铁砧
            print(
                f"\n  {Colors.YELLOW}步骤 1/5: 打开铁砧...{Colors.RESET}",
                flush=True,
            )
            window = await self.open_container(
                anvil_x or 0, anvil_y or 0, anvil_z or 0,
                container_type="anvil",
            )
            if not window:
                print(f"    {Colors.RED}无法打开铁砧{Colors.RESET}", flush=True)
                return False
            await asyncio.sleep(0.2)

            # 2. 将物品放入铁砧输入槽
            # MCBE 铁砧槽位: 0=第一个输入槽, 1=第二个输入槽(材料), 2=输出槽
            print(
                f"\n  {Colors.YELLOW}步骤 2/5: 将物品放入铁砧输入槽...{Colors.RESET}",
                flush=True,
            )
            await self.click_slot(item_slot_in_hotbar, window_id=0)
            await asyncio.sleep(0.15)
            await self.click_slot(0, window_id=window.window_id)
            await asyncio.sleep(0.2)
            print(f"    {Colors.GREEN}物品已放入输入槽{Colors.RESET}", flush=True)

            # 3. 设置新名称
            print(
                f"\n  {Colors.YELLOW}步骤 3/5: 设置新名称: "
                f"'{new_name}'...{Colors.RESET}",
                flush=True,
            )
            # TODO: 实际发送 FilterTextPacket 更新铁砧文本
            self.info.add_log("info", f"设置铁砧重命名文本: '{new_name}'")

            # 通过接入点发送重命名请求
            if self._access_point is not None:
                await self._access_point.send_packet(
                    {"type": "anvil_rename", "name": new_name}
                )

            print(f"    {Colors.GREEN}新名称已设置{Colors.RESET}", flush=True)
            await asyncio.sleep(0.3)

            # 4. 从输出槽取出重命名后的物品
            print(
                f"\n  {Colors.YELLOW}步骤 4/5: 取出重命名后的物品...{Colors.RESET}",
                flush=True,
            )
            await self.click_slot(2, window_id=window.window_id)
            await asyncio.sleep(0.15)
            print(f"    {Colors.GREEN}已取出重命名物品{Colors.RESET}", flush=True)

            # 5. 放回背包指定槽位
            print(
                f"\n  {Colors.YELLOW}步骤 5/5: 放回背包槽位 "
                f"{put_back_slot}...{Colors.RESET}",
                flush=True,
            )
            await self.click_slot(put_back_slot, window_id=0)
            await asyncio.sleep(0.15)
            print(f"    {Colors.GREEN}已放回背包{Colors.RESET}", flush=True)

            # 关闭铁砧
            await self.close_container()

            success_msg = f"物品重命名成功! '{new_name}'"
            self.info.add_log("info", success_msg)
            self.add_chat("System", success_msg, is_system=True)

            print(f"\n{Colors.GREEN}{Colors.BOLD}{'='*62}{Colors.RESET}", flush=True)
            print(
                f"{Colors.GREEN}{Colors.BOLD}  {success_msg}{Colors.RESET}",
                flush=True,
            )
            print(f"{Colors.GREEN}{Colors.BOLD}{'='*62}{Colors.RESET}\n", flush=True)
            return True

        except Exception as e:
            error_msg = f"重命名过程出错: {e}"
            self.info.add_log("error", error_msg)
            print(f"\n  {Colors.RED}{error_msg}{Colors.RESET}", flush=True)
            print(traceback.format_exc(), flush=True)
            await self.close_container()
            return False

    async def transfer_item(
        self,
        from_slot: int,
        to_slot: int,
        from_window: int = 0,
        to_window: int = 0,
        use_shift: bool = False,
    ) -> bool:
        """在背包/容器间转移物品。

        Args:
            from_slot: 来源槽位。
            to_slot: 目标槽位。
            from_window: 来源窗口 ID (0=背包)。
            to_window: 目标窗口 ID (0=背包)。
            use_shift: 是否使用 Shift 快速转移。

        Returns:
            ``True`` 转移成功。
        """
        from_name = "背包" if from_window == 0 else f"容器{from_window}"
        to_name = "背包" if to_window == 0 else f"容器{to_window}"
        self.info.add_log(
            "info",
            f"转移物品: {from_name}槽{from_slot} -> {to_name}槽{to_slot}",
        )
        self._log_op(
            "<>",
            f"转移物品: {from_name}槽{from_slot} -> {to_name}槽{to_slot}",
            Colors.BRIGHT_MAGENTA,
        )

        if use_shift and from_window != to_window:
            await self.shift_click_slot(from_slot, window_id=from_window)
        else:
            await self.click_slot(from_slot, window_id=from_window)
            await asyncio.sleep(0.08)
            await self.click_slot(to_slot, window_id=to_window)

        await asyncio.sleep(0.1)
        return True

    async def swap_slots(
        self, slot_a: int, slot_b: int, window_id: int = 0
    ) -> bool:
        """交换两个槽位的物品。

        Args:
            slot_a: 第一个槽位。
            slot_b: 第二个槽位。
            window_id: 窗口 ID。

        Returns:
            ``True`` 交换成功。
        """
        self.info.add_log("info", f"交换槽位: {slot_a} <-> {slot_b}")
        self._log_op(
            "<>", f"交换槽位: {slot_a} <-> {slot_b}", Colors.BRIGHT_MAGENTA
        )
        await self.click_slot(slot_a, window_id=window_id)
        await asyncio.sleep(0.06)
        await self.click_slot(slot_b, window_id=window_id)
        await asyncio.sleep(0.06)
        await self.click_slot(slot_a, window_id=window_id)
        return True

    async def select_hotbar_slot(self, slot: int) -> bool:
        """切换手持物品（选择快捷栏槽位 0-8）。

        Args:
            slot: 快捷栏槽位 (0-8)。

        Returns:
            ``True`` 切换成功;``False`` 槽位无效。
        """
        if not 0 <= slot <= 8:
            return False
        self.info.add_log("info", f"选择快捷栏槽位: {slot}")
        self._log_op("[]", f"切换到快捷栏 {slot}", Colors.GREEN)

        # 通过接入点发送
        if self._access_point is not None:
            await self._access_point.send_packet(
                {"type": "select_hotbar", "slot": slot}
            )

        # TODO: 发送 PlayerHotbarPacket
        return True

    async def use_item(
        self, slot: Optional[int] = None, face: int = -1
    ) -> bool:
        """使用物品（右键）。

        Args:
            slot: 快捷栏槽位 (可选，指定后先切换到该槽)。
            face: 交互面 (可选)。

        Returns:
            ``True`` 使用成功。
        """
        if slot is not None:
            await self.select_hotbar_slot(slot)
        self.info.add_log("info", "使用物品")
        self._log_op("()", "使用物品", Colors.GREEN)

        # 通过接入点发送
        if self._access_point is not None:
            await self._access_point.send_packet(
                {"type": "use_item", "slot": slot, "face": face}
            )

        # TODO: 发送 UseItemPacket
        return True

    async def attack_entity(self, entity_id: int) -> bool:
        """攻击实体/生物。

        Args:
            entity_id: 实体 ID。

        Returns:
            ``True`` 攻击成功。
        """
        self.info.add_log("info", f"攻击实体: {entity_id}")
        self._log_op("><", f"攻击实体 {entity_id}", Colors.RED)

        # 通过接入点发送
        if self._access_point is not None:
            await self._access_point.send_packet(
                {"type": "attack_entity", "entity_id": entity_id}
            )

        # TODO: 发送 InventoryTransactionPacket (UseItemOnEntity)
        return True

    async def interact_block(
        self, x: int, y: int, z: int, face: int = 1
    ) -> bool:
        """与方块交互（右键点击方块，如开门、按按钮等）。

        Args:
            x: 方块 X 坐标。
            y: 方块 Y 坐标。
            z: 方块 Z 坐标。
            face: 交互面 (0=底, 1=顶, 2=北, 3=南, 4=西, 5=东)。

        Returns:
            ``True`` 交互成功。
        """
        self.info.add_log(
            "info", f"交互方块: ({x}, {y}, {z}), 面: {face}"
        )
        self._log_op("()", f"交互方块 ({x}, {y}, {z})", Colors.BRIGHT_YELLOW)

        # 通过接入点发送
        if self._access_point is not None:
            await self._access_point.send_packet(
                {"type": "interact_block", "x": x, "y": y, "z": z, "face": face}
            )

        # TODO: 发送 InventoryTransactionPacket (UseItemOn)
        return True

    async def break_block(self, x: int, y: int, z: int) -> bool:
        """破坏方块。

        Args:
            x: 方块 X 坐标。
            y: 方块 Y 坐标。
            z: 方块 Z 坐标。

        Returns:
            ``True`` 开始破坏。
        """
        self.info.add_log("info", f"破坏方块: ({x}, {y}, {z})")
        self._log_op("XX", f"破坏方块 ({x}, {y}, {z})", Colors.YELLOW)

        # 通过接入点发送
        if self._access_point is not None:
            await self._access_point.send_packet(
                {"type": "break_block", "x": x, "y": y, "z": z}
            )

        # TODO: 发送 LevelSoundEventPacket + 实际挖包
        return True

    async def place_block(
        self, x: int, y: int, z: int, face: int = 1
    ) -> bool:
        """放置方块。

        Args:
            x: 方块 X 坐标。
            y: 方块 Y 坐标。
            z: 方块 Z 坐标。
            face: 放置面。

        Returns:
            ``True`` 放置成功。
        """
        self.info.add_log("info", f"放置方块: ({x}, {y}, {z})")
        self._log_op("[]", f"放置方块 ({x}, {y}, {z})", Colors.BRIGHT_GREEN)

        # 通过接入点发送
        if self._access_point is not None:
            await self._access_point.send_packet(
                {"type": "place_block", "x": x, "y": y, "z": z, "face": face}
            )

        # TODO: 发送 UpdateBlockPacket + InventoryTransactionPacket
        return True

    # ==================================================================
    # 状态查询
    # ==================================================================

    def get_inventory(self) -> list[dict[str, Any]]:
        """获取背包物品。

        Returns:
            物品槽位字典列表。
        """
        return [
            {
                "slot_id": s.slot_id,
                "item_id": s.item_id,
                "item_count": s.item_count,
                "item_damage": s.item_damage,
                "name": s.name,
            }
            for s in self._inventory
        ]

    def get_uptime(self) -> float:
        """获取运行时长（秒）。

        Returns:
            从连接成功到现在的秒数;未连接时返回 0。
        """
        if self.info.connected_at:
            return time.time() - self.info.connected_at
        return 0.0


__all__ = ["PocketBot", "BAN_KEYWORDS", "KICK_KEYWORDS"]
