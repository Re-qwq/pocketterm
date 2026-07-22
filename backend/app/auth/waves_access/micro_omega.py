"""micro_omega - MicroOmega 主控接口模块。

逆向自 NovaBuilder 的 WavesAccess MicroOmega, 来源:
    - /workspace/novuilder_reverse/strings_security.txt
    - /workspace/novuilder_reverse/REPORT.txt
    - /workspace/novuilder_reverse/player_options.txt

MicroOmega 是 WavesAccess 的主控组件, 名称来源于
"Omega System" (逆向自 strings_security.txt)。

MicroOmega 职责:
    1. 协调所有子系统 (ReactCore, PlayerKit, BotInfo)
    2. 管理机器人生命周期 (启动/停止/重启)
    3. 处理服务器事件 (PlayerAddRoom 等)
    4. 管理任务队列
    5. 协调反作弊系统

字符串证据 (逆向自 strings):
    "Omega System"          -- Omega 系统主循环
    "PlayerAddRoom"         -- 玩家加入房间事件
    "getTypeInfo"           -- 获取类型信息
    "executeResult"         -- 执行结果
    "generate_type"         -- 生成类型
    "Server not found, please check your server's public state"
    "connection not established after very long time"
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.waves_access.micro_omega")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: Omega 系统主循环间隔 (秒)
OMEGA_LOOP_INTERVAL: float = 0.1

#: 默认任务超时 (秒)
DEFAULT_TASK_TIMEOUT: float = 300.0

#: 最大任务队列长度
MAX_TASK_QUEUE: int = 1000

#: 系统状态消息 (逆向自 strings)
MSG_OMEGA_SYSTEM: str = "Omega System"
MSG_SERVER_NOT_FOUND: str = "Server not found, please check your server's public state"
MSG_CONNECTION_TIMEOUT: str = "connection not established after very long time"
MSG_PLAYER_ADD_ROOM: str = "PlayerAddRoom"
MSG_GET_TYPE_INFO: str = "getTypeInfo"
MSG_EXECUTE_RESULT: str = "executeResult"
MSG_GENERATE_TYPE: str = "generate_type"


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class MicroOmegaState(Enum):
    """MicroOmega 运行状态。"""

    UNINITIALIZED = auto()   # 未初始化
    INITIALIZED = auto()     # 已初始化
    CONNECTING = auto()      # 正在连接
    CONNECTED = auto()       # 已连接
    RUNNING = auto()         # 运行中
    PAUSED = auto()          # 已暂停
    STOPPING = auto()        # 正在停止
    STOPPED = auto()         # 已停止
    ERROR = auto()           # 错误


class TaskPriority(Enum):
    """任务优先级。"""

    CRITICAL = 0    # 关键 (最高)
    HIGH = 1        # 高
    NORMAL = 2      # 普通
    LOW = 3         # 低
    BACKGROUND = 4  # 后台 (最低)


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class MicroOmegaConfig:
    """MicroOmega 配置。"""

    #: 主循环间隔 (秒)
    loop_interval: float = OMEGA_LOOP_INTERVAL

    #: 默认任务超时 (秒)
    task_timeout: float = DEFAULT_TASK_TIMEOUT

    #: 最大任务队列长度
    max_task_queue: int = MAX_TASK_QUEUE

    #: 是否启用自动重连
    auto_reconnect: bool = True

    #: 是否启用反作弊
    enable_anti_ban: bool = True

    #: 是否启用命令人类化
    enable_humanize: bool = True

    #: 是否启用速率限制
    enable_rate_limit: bool = True

    #: 构建速度 (方块/秒)
    build_speed: int = 30

    #: 是否在启动时发送问候消息
    send_greeting: bool = False

    #: 问候消息
    greeting_message: str = "NovaBuilder connected"


@dataclass
class OmegaTask:
    """Omega 任务。"""

    task_id: str = ""
    name: str = ""
    priority: TaskPriority = TaskPriority.NORMAL
    func: Callable[[], Any] | None = None
    timeout: float = DEFAULT_TASK_TIMEOUT
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    result: Any = None
    error: str = ""
    status: str = "pending"  # pending / running / completed / failed / timeout

    @property
    def is_expired(self) -> bool:
        """是否超时。"""
        if self.timeout <= 0:
            return False
        if self.started_at == 0:
            return time.time() - self.created_at > self.timeout
        return time.time() - self.started_at > self.timeout

    @property
    def duration(self) -> float:
        """执行耗时 (秒)。"""
        if self.started_at == 0:
            return 0.0
        end = self.completed_at if self.completed_at else time.time()
        return end - self.started_at


@dataclass
class MicroOmegaStats:
    """MicroOmega 统计。"""

    start_time: float = 0.0
    uptime: float = 0.0
    total_tasks: int = 0
    completed_tasks: int = 0
    failed_tasks: int = 0
    timeout_tasks: int = 0
    total_errors: int = 0
    last_error: str = ""
    last_error_time: float = 0.0

    @property
    def success_rate(self) -> float:
        """任务成功率。"""
        if self.total_tasks == 0:
            return 0.0
        return self.completed_tasks / self.total_tasks

    def reset(self) -> None:
        """重置统计。"""
        self.start_time = 0.0
        self.uptime = 0.0
        self.total_tasks = 0
        self.completed_tasks = 0
        self.failed_tasks = 0
        self.timeout_tasks = 0
        self.total_errors = 0
        self.last_error = ""
        self.last_error_time = 0.0


# -------------------------------------------------------------------- #
# MicroOmega 主控
# -------------------------------------------------------------------- #


class MicroOmega:
    """MicroOmega 主控接口。

    逆向自 NovaBuilder 的 WavesAccess MicroOmega 主控组件。

    MicroOmega 是整个机器人系统的核心控制器, 负责:
        1. 初始化和协调所有子系统
        2. 管理机器人生命周期
        3. 处理任务队列
        4. 响应服务器事件
        5. 错误恢复和重试

    架构::

        MicroOmega
            |-- ReactCore (反应核心)
            |-- PlayerKit (玩家操作)
            |-- BotBasicInfoHolder (机器人信息)
            |-- CmdSender (命令发送)
            |-- PacketDispatcher (数据包分发)
            |-- PyRPC (RPC 事件)

    使用示例::

        omega = MicroOmega(config=MicroOmegaConfig())
        omega.initialize(game_interface=interface)
        omega.start()
        omega.submit_task("build_house", build_func)
        omega.stop()
    """

    def __init__(self, config: MicroOmegaConfig | None = None) -> None:
        """初始化 MicroOmega。

        Args:
            config: 配置, 默认使用 :class:`MicroOmegaConfig` 默认值。
        """
        self.config: MicroOmegaConfig = config or MicroOmegaConfig()
        self._state: MicroOmegaState = MicroOmegaState.UNINITIALIZED
        self._stats: MicroOmegaStats = MicroOmegaStats()
        self._task_queue: list[OmegaTask] = []
        self._completed_tasks: list[OmegaTask] = []
        self._lock: threading.RLock = threading.RLock()
        self._loop_thread: threading.Thread | None = None
        self._stop_event: threading.Event = threading.Event()

        # 子系统引用
        self._react_core: Any | None = None
        self._player_kit: Any | None = None
        self._bot_info: Any | None = None
        self._game_interface: Any | None = None
        self._cmd_sender: Any | None = None
        self._packet_dispatcher: Any | None = None
        self._py_rpc: Any | None = None
        self._rate_limiter: Any | None = None
        self._reconnect_manager: Any | None = None

        # 事件回调
        self._on_state_change: Callable[[MicroOmegaState], None] | None = None
        self._on_task_complete: Callable[[OmegaTask], None] | None = None
        self._on_error: Callable[[str], None] | None = None

        logger.debug("MicroOmega initialized (uninitialized)")

    # ---------------------------------------------------------------- #
    # 属性
    # ---------------------------------------------------------------- #

    @property
    def state(self) -> MicroOmegaState:
        """当前状态。"""
        with self._lock:
            return self._state

    @property
    def stats(self) -> MicroOmegaStats:
        """统计信息。"""
        with self._lock:
            return self._stats

    @property
    def is_running(self) -> bool:
        """是否正在运行。"""
        return self.state == MicroOmegaState.RUNNING

    @property
    def react_core(self) -> Any | None:
        """反应核心。"""
        return self._react_core

    @property
    def player_kit(self) -> Any | None:
        """玩家操作。"""
        return self._player_kit

    @property
    def bot_info(self) -> Any | None:
        """机器人信息。"""
        return self._bot_info

    @property
    def game_interface(self) -> Any | None:
        """游戏接口。"""
        return self._game_interface

    # ---------------------------------------------------------------- #
    # 状态管理
    # ---------------------------------------------------------------- #

    def _set_state(self, new_state: MicroOmegaState) -> None:
        """设置新状态。"""
        with self._lock:
            old_state = self._state
            self._state = new_state

        if old_state != new_state:
            logger.info("MicroOmega state: %s -> %s", old_state.name, new_state.name)
            if self._on_state_change:
                try:
                    self._on_state_change(new_state)
                except Exception:
                    logger.exception("on_state_change callback failed")

    def on_state_change(self, callback: Callable[[MicroOmegaState], None]) -> None:
        """注册状态变化回调。"""
        self._on_state_change = callback

    def on_task_complete(self, callback: Callable[[OmegaTask], None]) -> None:
        """注册任务完成回调。"""
        self._on_task_complete = callback

    def on_error(self, callback: Callable[[str], None]) -> None:
        """注册错误回调。"""
        self._on_error = callback

    # ---------------------------------------------------------------- #
    # 初始化
    # ---------------------------------------------------------------- #

    def initialize(
        self,
        game_interface: Any | None = None,
        cmd_sender: Any | None = None,
        packet_dispatcher: Any | None = None,
        py_rpc: Any | None = None,
        react_core: Any | None = None,
        player_kit: Any | None = None,
        bot_info: Any | None = None,
        rate_limiter: Any | None = None,
        reconnect_manager: Any | None = None,
    ) -> None:
        """初始化所有子系统。

        Args:
            game_interface: 游戏接口。
            cmd_sender: 命令发送器。
            packet_dispatcher: 数据包分发器。
            py_rpc: PyRPC 事件系统。
            react_core: 反应核心。
            player_kit: 玩家操作。
            bot_info: 机器人信息。
            rate_limiter: 速率限制器。
            reconnect_manager: 重连管理器。
        """
        self._game_interface = game_interface
        self._cmd_sender = cmd_sender
        self._packet_dispatcher = packet_dispatcher
        self._py_rpc = py_rpc
        self._react_core = react_core
        self._player_kit = player_kit
        self._bot_info = bot_info
        self._rate_limiter = rate_limiter
        self._reconnect_manager = reconnect_manager

        self._set_state(MicroOmegaState.INITIALIZED)
        logger.info("MicroOmega initialized with all subsystems")

    # ---------------------------------------------------------------- #
    # 生命周期管理
    # ---------------------------------------------------------------- #

    def start(self) -> bool:
        """启动 MicroOmega。

        Returns:
            True 如果启动成功。
        """
        if self.state in (MicroOmegaState.RUNNING, MicroOmegaState.CONNECTING):
            logger.warning("MicroOmega already running")
            return True

        if self.state == MicroOmegaState.UNINITIALIZED:
            logger.error("MicroOmega not initialized")
            return False

        self._stop_event.clear()
        self._stats.start_time = time.time()
        self._stats.uptime = 0.0

        # 启动主循环
        self._set_state(MicroOmegaState.RUNNING)
        self._loop_thread = threading.Thread(
            target=self._main_loop,
            daemon=True,
            name="NovaBuilder-MicroOmega",
        )
        self._loop_thread.start()

        logger.info("MicroOmega started")

        # 发送问候消息
        if self.config.send_greeting and self._cmd_sender:
            try:
                self._cmd_sender.send_command_with_resp(
                    f"say {self.config.greeting_message}",
                    timeout=5.0,
                )
            except Exception:
                logger.debug("Failed to send greeting message")

        return True

    def stop(self) -> None:
        """停止 MicroOmega。"""
        if self.state == MicroOmegaState.STOPPED:
            return

        self._set_state(MicroOmegaState.STOPPING)
        self._stop_event.set()

        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=10.0)

        self._set_state(MicroOmegaState.STOPPED)
        logger.info("MicroOmega stopped")

    def pause(self) -> None:
        """暂停 MicroOmega。"""
        if self.state == MicroOmegaState.RUNNING:
            self._set_state(MicroOmegaState.PAUSED)
            logger.info("MicroOmega paused")

    def resume(self) -> None:
        """恢复 MicroOmega。"""
        if self.state == MicroOmegaState.PAUSED:
            self._set_state(MicroOmegaState.RUNNING)
            logger.info("MicroOmega resumed")

    def restart(self) -> bool:
        """重启 MicroOmega。

        Returns:
            True 如果重启成功。
        """
        logger.info("Restarting MicroOmega...")
        self.stop()
        time.sleep(1.0)
        return self.start()

    # ---------------------------------------------------------------- #
    # 主循环
    # ---------------------------------------------------------------- #

    def _main_loop(self) -> None:
        """MicroOmega 主循环。

        逆向自 "Omega System" 主循环。
        """
        logger.info("Omega System main loop started")

        while not self._stop_event.is_set():
            try:
                if self.state == MicroOmegaState.RUNNING:
                    self._update_uptime()
                    self._process_tasks()
                    self._check_timeouts()

                self._stop_event.wait(self.config.loop_interval)

            except Exception as exc:
                self._handle_error(str(exc))

        logger.info("Omega System main loop stopped")

    def _update_uptime(self) -> None:
        """更新运行时间。"""
        if self._stats.start_time > 0:
            self._stats.uptime = time.time() - self._stats.start_time

    def _process_tasks(self) -> None:
        """处理任务队列。"""
        with self._lock:
            if not self._task_queue:
                return
            # 按优先级排序
            self._task_queue.sort(key=lambda t: t.priority.value)
            task = self._task_queue.pop(0)

        # 执行任务
        task.started_at = time.time()
        task.status = "running"

        try:
            if task.func:
                task.result = task.func()
            task.status = "completed"
            task.completed_at = time.time()
            self._stats.completed_tasks += 1

            logger.debug(
                "Task completed: %s (took %.2fs)",
                task.name, task.duration,
            )

            if self._on_task_complete:
                try:
                    self._on_task_complete(task)
                except Exception:
                    logger.exception("on_task_complete callback failed")

        except Exception as exc:
            task.status = "failed"
            task.error = str(exc)
            task.completed_at = time.time()
            self._stats.failed_tasks += 1
            logger.warning("Task failed: %s: %s", task.name, exc)

        finally:
            self._stats.total_tasks += 1
            with self._lock:
                self._completed_tasks.append(task)
                # 限制历史记录长度
                if len(self._completed_tasks) > 100:
                    self._completed_tasks = self._completed_tasks[-50:]

    def _check_timeouts(self) -> None:
        """检查超时任务。"""
        with self._lock:
            expired: list[OmegaTask] = []
            remaining: list[OmegaTask] = []
            for task in self._task_queue:
                if task.is_expired and task.status == "pending":
                    task.status = "timeout"
                    task.error = "task timeout before execution"
                    expired.append(task)
                    self._stats.timeout_tasks += 1
                else:
                    remaining.append(task)
            self._task_queue = remaining

        for task in expired:
            logger.warning("Task timeout: %s", task.name)
            with self._lock:
                self._completed_tasks.append(task)

    def _handle_error(self, error: str) -> None:
        """处理错误。"""
        self._stats.total_errors += 1
        self._stats.last_error = error
        self._stats.last_error_time = time.time()
        logger.error("MicroOmega error: %s", error)

        if self._on_error:
            try:
                self._on_error(error)
            except Exception:
                logger.exception("on_error callback failed")

    # ---------------------------------------------------------------- #
    # 任务管理
    # ---------------------------------------------------------------- #

    def submit_task(
        self,
        name: str,
        func: Callable[[], Any],
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout: float | None = None,
    ) -> str:
        """提交任务。

        Args:
            name: 任务名称。
            func: 任务函数。
            priority: 任务优先级。
            timeout: 任务超时 (秒), None 使用默认值。

        Returns:
            任务 ID。
        """
        import uuid as uuid_module

        task = OmegaTask(
            task_id=str(uuid_module.uuid4()),
            name=name,
            priority=priority,
            func=func,
            timeout=timeout or self.config.task_timeout,
        )

        with self._lock:
            if len(self._task_queue) >= self.config.max_task_queue:
                logger.warning("Task queue full, rejecting task: %s", name)
                return ""
            self._task_queue.append(task)

        logger.debug("Task submitted: %s (priority=%s)", name, priority.name)
        return task.task_id

    def submit_critical_task(self, name: str, func: Callable[[], Any]) -> str:
        """提交关键任务 (最高优先级)。

        Args:
            name: 任务名称。
            func: 任务函数。

        Returns:
            任务 ID。
        """
        return self.submit_task(name, func, priority=TaskPriority.CRITICAL, timeout=60.0)

    def cancel_task(self, task_id: str) -> bool:
        """取消任务。

        Args:
            task_id: 任务 ID。

        Returns:
            True 如果成功取消。
        """
        with self._lock:
            for i, task in enumerate(self._task_queue):
                if task.task_id == task_id:
                    task.status = "cancelled"
                    self._task_queue.pop(i)
                    logger.info("Task cancelled: %s", task.name)
                    return True
        return False

    def get_pending_count(self) -> int:
        """获取待处理任务数。"""
        with self._lock:
            return len(self._task_queue)

    def get_completed_tasks(self) -> list[OmegaTask]:
        """获取已完成任务列表。"""
        with self._lock:
            return list(self._completed_tasks)

    def clear_completed_tasks(self) -> None:
        """清空已完成任务。"""
        with self._lock:
            count = len(self._completed_tasks)
            self._completed_tasks.clear()
        logger.info("Cleared %d completed tasks", count)

    # ---------------------------------------------------------------- #
    # 事件处理
    # ---------------------------------------------------------------- #

    def on_player_add_room(self, player_info: dict[str, Any]) -> None:
        """玩家加入房间事件 (逆向自 PlayerAddRoom)。

        Args:
            player_info: 玩家信息。
        """
        logger.info("PlayerAddRoom: %s", player_info.get("name", "unknown"))

        if self._react_core:
            try:
                self._react_core.trigger("player_add_room", player_info)
            except Exception:
                logger.exception("ReactCore trigger failed for player_add_room")

    def on_player_leave(self, player_info: dict[str, Any]) -> None:
        """玩家离开事件。

        Args:
            player_info: 玩家信息。
        """
        logger.info("PlayerLeave: %s", player_info.get("name", "unknown"))

        if self._react_core:
            try:
                self._react_core.trigger("player_leave", player_info)
            except Exception:
                logger.exception("ReactCore trigger failed for player_leave")

    def on_server_message(self, message: str) -> None:
        """服务器消息事件。

        Args:
            message: 消息内容。
        """
        logger.debug("Server message: %s", message[:100])

        if self._react_core:
            try:
                self._react_core.trigger("server_message", {"message": message})
            except Exception:
                logger.exception("ReactCore trigger failed for server_message")

    # ---------------------------------------------------------------- #
    # 子系统访问
    # ---------------------------------------------------------------- #

    def get_react_core(self) -> Any | None:
        """获取反应核心。"""
        return self._react_core

    def get_player_kit(self) -> Any | None:
        """获取玩家操作。"""
        return self._player_kit

    def get_bot_info(self) -> Any | None:
        """获取机器人信息。"""
        return self._bot_info

    def get_game_interface(self) -> Any | None:
        """获取游戏接口。"""
        return self._game_interface

    def get_cmd_sender(self) -> Any | None:
        """获取命令发送器。"""
        return self._cmd_sender

    def get_packet_dispatcher(self) -> Any | None:
        """获取数据包分发器。"""
        return self._packet_dispatcher

    def get_py_rpc(self) -> Any | None:
        """获取 PyRPC。"""
        return self._py_rpc

    # ---------------------------------------------------------------- #
    # 健康检查
    # ---------------------------------------------------------------- #

    def health_check(self) -> dict[str, Any]:
        """执行健康检查。

        Returns:
            健康状态字典。
        """
        return {
            "state": self.state.name,
            "uptime": self._stats.uptime,
            "pending_tasks": self.get_pending_count(),
            "total_tasks": self._stats.total_tasks,
            "success_rate": self._stats.success_rate,
            "total_errors": self._stats.total_errors,
            "last_error": self._stats.last_error,
            "subsystems": {
                "react_core": self._react_core is not None,
                "player_kit": self._player_kit is not None,
                "bot_info": self._bot_info is not None,
                "game_interface": self._game_interface is not None,
                "cmd_sender": self._cmd_sender is not None,
                "packet_dispatcher": self._packet_dispatcher is not None,
                "py_rpc": self._py_rpc is not None,
            },
        }


__all__ = [
    # 常量
    "OMEGA_LOOP_INTERVAL", "DEFAULT_TASK_TIMEOUT", "MAX_TASK_QUEUE",
    "MSG_OMEGA_SYSTEM", "MSG_SERVER_NOT_FOUND", "MSG_CONNECTION_TIMEOUT",
    "MSG_PLAYER_ADD_ROOM", "MSG_GET_TYPE_INFO", "MSG_EXECUTE_RESULT",
    "MSG_GENERATE_TYPE",
    # 枚举
    "MicroOmegaState", "TaskPriority",
    # 数据结构
    "MicroOmegaConfig", "OmegaTask", "MicroOmegaStats",
    # 核心
    "MicroOmega",
]
