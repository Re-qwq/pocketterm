"""PocketTerm 自动化连接模块

本模块基于 NovaBuilder / NexusE 逆向结果中的自动化机制实现:

    - 自动登录 (使用保存的 sauth_json + 设备指纹)
    - 自动重连 (指数退避 + 随机抖动)
    - 自动 MCPC 挑战处理 (Minecraft Protocol Check Challenge)
    - 自动速率调整 (基于服务器响应)
    - 连接状态监控

逆向关键发现 (来自 NexusE)
----------------------------

NexusE 在 ``modules/anticheat`` 中维护以下自动化函数::

    solveMCPCheckChallenges   -- 解决 MCPC 检查挑战
    skipMCPCheckChallenge     -- 跳过 MCPC 检查挑战
    waitMCPCheckChallengesDown -- 等待 MCPC 挑战完成
    GetMCPChf                 -- 获取 MCPC 挑战因子
    GetMCPCheckNum            -- 获取 MCPC 检查数
    SetMCPCheckNum            -- 设置 MCPC 检查数
    GetMCPCheckNumSecondArg   -- 获取 MCPC 检查数第二参数

挑战类型:
    - OperatorChallenge   -- 操作员挑战 (服务器要求客户端完成特定动作)
    - CanSolveChallenge   -- 可解决挑战 (客户端可识别并响应)
    - PyRPCResponder      -- Python RPC 响应器 (通过 RPC 回应)

延迟模式:
    - none / discrete (含阈值) / continuous

速率限制:
    - 使用 ``rate.Limiter`` (golang.org/x/time/rate) 控制操作频率

模块职责
--------

本模块将上述机制适配为 Python 异步实现, 与 ``bot.py`` /
``protocol/connection.py`` 解耦, 仅暴露:

    - :class:`AutoConnectManager`   -- 自动化总控
    - :class:`MCPCChallengeHandler` -- MCPC 挑战处理
    - :class:`ReconnectPolicy`      -- 重连策略
    - :class:`ConnectionMonitor`   -- 连接状态监控
"""
from __future__ import annotations

import asyncio
import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .anti_ban import AntiBanController, get_anti_ban_controller
from .anti_ban_enhanced import (
    EnhancedAntiBan,
    EnhancedAntiBanConfig,
    get_enhanced_anti_ban,
)
from .device_fingerprint import DeviceFingerprint, get_fingerprint_manager
from ..logger import get_logger

logger = get_logger("auth.auto_connect")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------
class ConnectionState(enum.Enum):
    """自动化连接状态。"""

    IDLE = "idle"                    # 空闲
    CONNECTING = "connecting"        # 连接中
    AUTHENTICATING = "authenticating"  # 认证中
    MCPC_CHALLENGE = "mcpc_challenge"  # MCPC 挑战处理中
    CONNECTED = "connected"          # 已连接
    DISCONNECTED = "disconnected"   # 已断开
    RECONNECTING = "reconnecting"    # 重连中
    FAILED = "failed"               # 失败 (停止重试)
    BANNED = "banned"                # 已封禁 (停止重试)


class MCPCChallengeType(enum.Enum):
    """MCPC 挑战类型 (来自逆向 challenges.OperatorChallenge 等)。"""

    UNKNOWN = 0                  # 未知类型
    OPERATOR_CHALLENGE = 1       # 操作员挑战
    CAN_SOLVE_CHALLENGE = 2      # 可解决挑战
    PY_RPC_RESPONDER = 3         # Python RPC 响应器
    SKIPPABLE = 4               # 可跳过的挑战


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class MCPCChallenge:
    """MCPC 挑战描述。

    Attributes:
        challenge_id: 挑战 ID (服务器下发)。
        challenge_type: 挑战类型 (见 :class:`MCPCChallengeType`)。
        factor: 挑战因子 (来自 GetMCPChf)。
        check_num: 检查数 (来自 GetMCPCheckNum)。
        check_num_second_arg: 检查数第二参数 (来自 GetMCPCheckNumSecondArg)。
        payload: 原始挑战数据 (供具体处理逻辑使用)。
        received_at: 接收时间戳。
    """

    challenge_id: str = ""
    challenge_type: MCPCChallengeType = MCPCChallengeType.UNKNOWN
    factor: int = 0
    check_num: int = 0
    check_num_second_arg: int = 0
    payload: bytes = b""
    received_at: float = field(default_factory=time.time)


@dataclass
class ConnectionStats:
    """连接统计信息。"""

    total_attempts: int = 0          # 总尝试次数
    successful_logins: int = 0        # 成功登录次数
    failed_logins: int = 0           # 失败登录次数
    reconnect_count: int = 0         # 重连次数
    mcpc_challenges_received: int = 0  # 收到 MCPC 挑战数
    mcpc_challenges_solved: int = 0  # 已解决 MCPC 挑战数
    mcpc_challenges_skipped: int = 0  # 已跳过 MCPC 挑战数
    last_connected_at: float = 0.0   # 上次连接时间
    last_disconnected_at: float = 0.0  # 上次断开时间
    last_error: str = ""             # 最近一次错误信息
    session_start: float = field(default_factory=time.time)
    state: ConnectionState = ConnectionState.IDLE

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典 (供 API 使用)。"""
        return {
            "total_attempts": self.total_attempts,
            "successful_logins": self.successful_logins,
            "failed_logins": self.failed_logins,
            "reconnect_count": self.reconnect_count,
            "mcpc_challenges_received": self.mcpc_challenges_received,
            "mcpc_challenges_solved": self.mcpc_challenges_solved,
            "mcpc_challenges_skipped": self.mcpc_challenges_skipped,
            "last_connected_at": self.last_connected_at,
            "last_disconnected_at": self.last_disconnected_at,
            "last_error": self.last_error,
            "session_start": self.session_start,
            "state": self.state.value,
            "session_duration": time.time() - self.session_start,
        }


# ---------------------------------------------------------------------------
# 回调类型
# ---------------------------------------------------------------------------
#: 连接回调: ``async (host, port) -> None``
ConnectCallback = Callable[[str, int], Awaitable[None]]
#: 断开回调: ``async (reason) -> None``
DisconnectCallback = Callable[[str], Awaitable[None]]
#: MCPC 挑战回调: ``async (challenge) -> bool`` (返回 True 表示已解决)
MCPCChallengeCallback = Callable[[MCPCChallenge], Awaitable[bool]]


# ---------------------------------------------------------------------------
# MCPC 挑战处理器
# ---------------------------------------------------------------------------
class MCPCChallengeHandler:
    """MCPC 挑战处理器。

    基于 NexusE 逆向的 ``solveMCPCheckChallenges`` /
    ``skipMCPCheckChallenge`` / ``waitMCPCheckChallengesDown`` 实现的
    Python 异步版本。

    设计:
        - 维护一个待处理挑战的异步队列
        - 提供 ``solve`` / ``skip`` 两种处理模式
        - ``solve``: 调用注册的求解回调 (具体求解逻辑由调用方注入)
        - ``skip``: 跳过挑战 (服务器允许时使用)
        - 支持超时与并发限制
    """

    def __init__(
        self,
        solver: Optional[MCPCChallengeCallback] = None,
        timeout: float = 30.0,
    ) -> None:
        self._solver = solver
        self._timeout = timeout
        self._pending: asyncio.Queue[MCPCChallenge] = asyncio.Queue()
        self._processor_task: Optional[asyncio.Task[None]] = None
        # H-7 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None
        self._solved_count: int = 0
        self._skipped_count: int = 0
        self._received_count: int = 0
        self._is_running: bool = False

    @property
    def is_running(self) -> bool:
        """处理器是否正在运行。"""
        return self._is_running

    @property
    def pending_count(self) -> int:
        """待处理挑战数。"""
        return self._pending.qsize()

    @property
    def solved_count(self) -> int:
        """已解决挑战数。"""
        return self._solved_count

    @property
    def skipped_count(self) -> int:
        """已跳过挑战数。"""
        return self._skipped_count

    def register_solver(self, solver: MCPCChallengeCallback) -> None:
        """注册挑战求解回调。

        Args:
            solver: ``async (challenge) -> bool`` 回调, 返回 True 表示已解决。
        """
        self._solver = solver
        logger.info("MCPC 挑战求解器已注册")

    def submit(self, challenge: MCPCChallenge) -> None:
        """提交一个挑战到队列。

        Args:
            challenge: 待处理的挑战。
        """
        self._received_count += 1
        self._pending.put_nowait(challenge)
        logger.info(
            f"收到 MCPC 挑战: id={challenge.challenge_id} "
            f"type={challenge.challenge_type.name} "
            f"factor={challenge.factor} check_num={challenge.check_num}"
        )

    async def start(self) -> None:
        """启动后台挑战处理任务。"""
        if self._is_running:
            return
        self._is_running = True
        self._processor_task = asyncio.create_task(self._process_loop())
        logger.info("MCPC 挑战处理器已启动")

    async def stop(self) -> None:
        """停止后台挑战处理任务。"""
        self._is_running = False
        if self._processor_task is not None:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
            self._processor_task = None
        logger.info("MCPC 挑战处理器已停止")

    async def solve_one(self, challenge: MCPCChallenge) -> bool:
        """解决单个挑战。

        Args:
            challenge: 待处理的挑战。

        Returns:
            ``True`` 已解决; ``False`` 跳过或失败。
        """
        if self._solver is None:
            logger.warning(
                f"未注册求解器, 跳过挑战: {challenge.challenge_id}"
            )
            self._skipped_count += 1
            return False

        try:
            result = await asyncio.wait_for(
                self._solver(challenge), timeout=self._timeout
            )
            if result:
                self._solved_count += 1
                logger.info(f"挑战已解决: {challenge.challenge_id}")
            else:
                self._skipped_count += 1
                logger.warning(f"挑战求解失败, 已跳过: {challenge.challenge_id}")
            return result
        except asyncio.TimeoutError:
            self._skipped_count += 1
            logger.error(
                f"挑战求解超时 ({self._timeout}s), 已跳过: {challenge.challenge_id}"
            )
            return False
        except Exception as exc:  # noqa: BLE001
            self._skipped_count += 1
            logger.exception(f"挑战求解异常, 已跳过: {exc}")
            return False

    async def skip(self, challenge: MCPCChallenge) -> bool:
        """跳过一个挑战 (对应逆向 ``skipMCPCheckChallenge``)。

        Args:
            challenge: 待跳过的挑战。

        Returns:
            ``True`` 跳过成功 (服务器允许); ``False`` 跳过失败。
        """
        self._skipped_count += 1
        logger.info(f"主动跳过挑战: {challenge.challenge_id}")
        return True

    async def wait_all_down(self, timeout: Optional[float] = None) -> bool:
        """等待所有待处理挑战完成 (对应逆向 ``waitMCPCheckChallengesDown``)。

        Args:
            timeout: 最大等待秒数; ``None`` 表示不超时。

        Returns:
            ``True`` 全部完成; ``False`` 超时。
        """
        try:
            async def _wait() -> None:
                while self._pending.qsize() > 0:
                    await asyncio.sleep(0.1)
            if timeout is None:
                await _wait()
            else:
                await asyncio.wait_for(_wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning(
                f"等待 MCPC 挑战完成超时 ({timeout}s), "
                f"剩余 {self._pending.qsize()} 个"
            )
            return False

    async def _process_loop(self) -> None:
        """后台处理循环: 从队列取出挑战并解决。"""
        logger.info("MCPC 挑战处理循环已启动")
        try:
            while self._is_running:
                try:
                    challenge = await asyncio.wait_for(
                        self._pending.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                await self.solve_one(challenge)
        except asyncio.CancelledError:
            logger.debug("MCPC 处理循环被取消")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"MCPC 处理循环异常: {exc}")

    def stats(self) -> Dict[str, int]:
        """返回统计信息。"""
        return {
            "received": self._received_count,
            "solved": self._solved_count,
            "skipped": self._skipped_count,
            "pending": self.pending_count,
            "is_running": int(self._is_running),
        }


# ---------------------------------------------------------------------------
# 重连策略
# ---------------------------------------------------------------------------
class ReconnectPolicy:
    """重连策略 (4 层 + ToolDelta 指数退避 + 随机抖动)。

    基于 NovaBuilder / NexusE 中的 ``auto_reconnect`` 实现,
    结合 :class:`anti_ban.JitterDelay` 与 :class:`EnhancedAntiBan.ToolDeltaBackoff`。

    **4 层重连策略** (逆向自 NexusE):
        1. **network** - 网络层断开 (TCP 重连)
        2. **task** - 任务层断开 (异步任务崩溃)
        3. **command** - 命令层断开 (命令响应超时)
        4. **op** - OP 权限层断开 (OP 挑战失败)

    触发停止重连的条件:
        1. 重连次数超过 ``max_attempts``
        2. 检测到封禁 (AccountBannedError / ban 关键词)
        3. 检测到认证失败 (InvalidCredentialsError)
        4. 检测到版本错误 (VersionTooLowError)

    **退避序列** (ToolDelta 风格):
        ``[5, 10, 20, 40, 80, 160, 300]`` 秒, 比传统 ``base * 2^n`` 更贴近
        ToolDelta 真实运行参数。
    """

    #: 4 层重连级别
    LAYER_NETWORK = "network"
    LAYER_TASK = "task"
    LAYER_COMMAND = "command"
    LAYER_OP = "op"

    def __init__(
        self,
        max_attempts: int = 7,
        base_delay: float = 5.0,
        max_delay: float = 600.0,
        enhanced: Optional[EnhancedAntiBan] = None,
    ) -> None:
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._attempt: int = 0
        self._last_error: str = ""
        self._last_layer: str = self.LAYER_NETWORK
        self._enhanced = enhanced
        # ToolDelta 退避序列 (当 enhanced 为 None 时使用)
        self._tooldelta_sequence: Tuple[int, ...] = (5, 10, 20, 40, 80, 160, 300)

    @property
    def attempt(self) -> int:
        """当前重连尝试次数 (0 = 未重连)。"""
        return self._attempt

    @property
    def last_error(self) -> str:
        """最近一次错误。"""
        return self._last_error

    @property
    def last_layer(self) -> str:
        """最近一次重连层级 (network/task/command/op)。"""
        return self._last_layer

    def set_layer(self, layer: str) -> None:
        """设置当前重连层级 (供调用方在断开时标记)。"""
        if layer in (
            self.LAYER_NETWORK, self.LAYER_TASK,
            self.LAYER_COMMAND, self.LAYER_OP,
        ):
            self._last_layer = layer

    def reset(self) -> None:
        """重置策略 (连接成功后调用)。"""
        self._attempt = 0
        self._last_error = ""
        self._last_layer = self.LAYER_NETWORK
        if self._enhanced is not None:
            self._enhanced.backoff.reset()

    def should_retry(self, error: Optional[Exception] = None) -> bool:
        """判断是否应继续重连。

        Args:
            error: 上次失败的异常 (可为 ``None`` 表示无具体异常)。

        Returns:
            ``True`` 应继续重连; ``False`` 应停止。
        """
        if error is not None:
            self._last_error = str(error)
            # 检测致命错误 (同时检查异常类型名与异常消息文本, 支持中英文关键词)
            error_type = type(error).__name__.lower()
            error_msg = str(error).lower()
            # 封禁关键词: 英文 ban / 中文 封禁 / 封号 / 禁止登录 / 账号已封
            ban_keywords = (
                "ban", "封禁", "封号", "账号已封", "已封禁", "banned",
                "禁止登录", "禁止游戏", "禁止进入", "您已被禁止",
            )
            if any(kw in error_type or kw in error_msg for kw in ban_keywords):
                logger.warning(f"检测到封禁类错误, 停止重连: {error}")
                return False
            # 认证失败关键词: 英文 auth / credential / 中文 认证失败 / 凭证
            auth_keywords = (
                "auth", "credential", "认证失败", "凭证", "登录失败",
                "invalid", "sessionid", "无效",
            )
            if any(kw in error_type or kw in error_msg for kw in auth_keywords):
                logger.warning(f"检测到认证类错误, 停止重连: {error}")
                return False
            # 版本错误关键词: 英文 version / 中文 版本
            version_keywords = ("version", "版本", "toolow", "versiontoolow")
            if any(kw in error_type or kw in error_msg for kw in version_keywords):
                logger.warning(f"检测到版本类错误, 停止重连: {error}")
                return False
            # 根据异常类型推断层级
            if isinstance(error, (ConnectionError, OSError, asyncio.TimeoutError)):
                self._last_layer = self.LAYER_NETWORK
            elif isinstance(error, asyncio.CancelledError):
                self._last_layer = self.LAYER_TASK
            elif "command" in error_msg or "timeout" in error_msg:
                self._last_layer = self.LAYER_COMMAND
            elif "op" in error_msg or "permission" in error_msg:
                self._last_layer = self.LAYER_OP

        # 优先使用 EnhancedAntiBan 的退避判断
        if self._enhanced is not None:
            if not self._enhanced.backoff.should_retry():
                logger.warning(
                    f"ToolDelta 退避已用尽 ({self._enhanced.backoff.attempt}/"
                    f"{self._enhanced.config.max_retry_attempts}), 停止重连"
                )
                return False
            return True

        # 回退到本地判断
        if self._attempt >= self._max_attempts:
            logger.warning(
                f"达到最大重连次数 ({self._max_attempts}), 停止重连"
            )
            return False
        return True

    def next_delay(self, anti_ban: Optional[AntiBanController] = None) -> float:
        """计算下次重连延迟 (秒)。

        优先级:
            1. EnhancedAntiBan.ToolDeltaBackoff (若已注入)
            2. anti_ban.JitterDelay (若提供)
            3. 本地 ToolDelta 序列 + 50% 抖动

        Args:
            anti_ban: 防封禁控制器 (用于获取抖动); ``None`` 时使用本地抖动。

        Returns:
            等待秒数。
        """
        self._attempt += 1

        # 1. 优先使用 ToolDelta 退避序列
        if self._enhanced is not None:
            delay = self._enhanced.backoff.next_delay()
            delay = min(delay, self._max_delay)
            logger.info(
                f"ToolDelta 退避: layer={self._last_layer} "
                f"attempt={self._attempt}/{self._max_attempts} "
                f"delay={delay:.1f}s"
            )
            return delay

        # 2. 使用 anti_ban 的 JitterDelay (旧逻辑, 保持向后兼容)
        if anti_ban is not None:
            delay = anti_ban.jitter.reconnect_delay(
                self._base_delay, self._attempt
            )
            delay = min(delay, self._max_delay)
            logger.info(
                f"重连延迟 (jitter): layer={self._last_layer} "
                f"attempt={self._attempt}/{self._max_attempts} "
                f"delay={delay:.1f}s"
            )
            return delay

        # 3. 本地 ToolDelta 序列 + 50% 抖动
        import random as _r
        idx = min(self._attempt - 1, len(self._tooldelta_sequence) - 1)
        base = float(self._tooldelta_sequence[idx])
        jitter = _r.uniform(-0.5, 0.5)
        delay = base * (1.0 + jitter)
        delay = min(max(delay, 1.0), self._max_delay)
        logger.info(
            f"本地 ToolDelta 退避: layer={self._last_layer} "
            f"attempt={self._attempt}/{self._max_attempts} "
            f"base={base}s delay={delay:.1f}s"
        )
        return delay


# ---------------------------------------------------------------------------
# 连接状态监控
# ---------------------------------------------------------------------------
class ConnectionMonitor:
    """连接状态监控器。

    跟踪连接状态、心跳、数据包接收情况, 触发状态变更回调。

    使用 :class:`anti_ban.AnomalyDetector` 进行无响应检测与异常记录,
    并可选集成 :class:`EnhancedAntiBan.HeartbeatMonitor` 实现 ToolDelta 风格
    8s 心跳 (3 次失败阈值 = 24s)。
    """

    def __init__(
        self,
        anti_ban: Optional[AntiBanController] = None,
        enhanced: Optional[EnhancedAntiBan] = None,
        heartbeat_interval: float = 8.0,
        no_response_threshold: float = 90.0,
    ) -> None:
        self._anti_ban = anti_ban or get_anti_ban_controller()
        self._enhanced = enhanced
        # 当存在 EnhancedAntiBan 时, 心跳间隔由 EnhancedAntiBan.config 控制
        if enhanced is not None:
            heartbeat_interval = enhanced.config.heartbeat_interval
            no_response_threshold = (
                enhanced.config.heartbeat_interval
                * enhanced.config.heartbeat_failure_threshold
                + 10.0  # 容差
            )
        self._heartbeat_interval = heartbeat_interval
        self._no_response_threshold = no_response_threshold
        self._state: ConnectionState = ConnectionState.IDLE
        self._last_heartbeat: float = time.time()
        self._last_packet: float = time.time()
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._is_running: bool = False

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def state(self) -> ConnectionState:
        """当前状态。"""
        return self._state

    @property
    def is_running(self) -> bool:
        """是否正在监控。"""
        return self._is_running

    async def set_state(self, state: ConnectionState, error: str = "") -> None:
        """更新状态并通知防封禁控制器。"""
        async with self._get_lock():
            old_state = self._state
            self._state = state
            if state == ConnectionState.CONNECTED:
                self._last_heartbeat = time.time()
                self._last_packet = time.time()
                self._anti_ban.anomaly.record_packet()
            elif state == ConnectionState.DISCONNECTED:
                if error:
                    self._anti_ban.on_action_failure(
                        severity="normal",
                        source="connection",
                        message=f"连接断开: {error}",
                    )
            elif state == ConnectionState.BANNED:
                self._anti_ban.on_action_failure(
                    severity="severe",
                    source="ban",
                    message=f"账号被封禁: {error}",
                )
            elif state == ConnectionState.FAILED:
                self._anti_ban.on_action_failure(
                    severity="severe",
                    source="connection",
                    message=f"连接失败: {error}",
                )

        if old_state != state:
            logger.info(f"连接状态变更: {old_state.value} -> {state.value}")
            if error:
                logger.warning(f"状态变更原因: {error}")

    def record_heartbeat(self) -> None:
        """记录心跳发送。"""
        self._last_heartbeat = time.time()

    def record_packet(self) -> None:
        """记录收到服务器数据包。"""
        self._last_packet = time.time()
        self._anti_ban.anomaly.record_packet()

    def is_alive(self) -> bool:
        """是否仍在存活期内 (未超过无响应阈值)。"""
        return (time.time() - self._last_packet) < self._no_response_threshold

    def next_heartbeat_delay(self) -> float:
        """下次心跳延迟 (含抖动)。

        当 EnhancedAntiBan 注入时, 使用其 8s HeartbeatMonitor。
        """
        if self._enhanced is not None:
            return self._enhanced.heartbeat.next_interval()
        return self._anti_ban.jitter.heartbeat_interval(self._heartbeat_interval)

    async def start(self) -> None:
        """启动后台监控任务。"""
        if self._is_running:
            return
        self._is_running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info("连接状态监控器已启动")

    async def stop(self) -> None:
        """停止后台监控任务。"""
        self._is_running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        logger.info("连接状态监控器已停止")

    async def _monitor_loop(self) -> None:
        """后台监控循环: 检查无响应 / 心跳。"""
        logger.info("连接监控循环已启动")
        try:
            while self._is_running:
                await asyncio.sleep(5.0)
                if self._state not in (ConnectionState.CONNECTED, ConnectionState.MCPC_CHALLENGE):
                    continue
                # 检查无响应
                if not self.is_alive():
                    logger.warning(
                        f"服务器无响应超过 {self._no_response_threshold}s, "
                        "触发异常检测"
                    )
                    self._anti_ban.anomaly.record_anomaly(
                        severity="severe",
                        source="heartbeat",
                        message="服务器无响应超过阈值",
                    )
                    await self.set_state(
                        ConnectionState.DISCONNECTED,
                        error=f"服务器无响应 {self._no_response_threshold}s",
                    )
        except asyncio.CancelledError:
            logger.debug("连接监控循环被取消")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"连接监控循环异常: {exc}")

    def stats(self) -> Dict[str, Any]:
        """返回统计信息。"""
        return {
            "state": self._state.value,
            "is_running": int(self._is_running),
            "is_alive": int(self.is_alive()),
            "seconds_since_last_heartbeat": time.time() - self._last_heartbeat,
            "seconds_since_last_packet": time.time() - self._last_packet,
            "heartbeat_interval": self._heartbeat_interval,
            "no_response_threshold": self._no_response_threshold,
        }


# ---------------------------------------------------------------------------
# 自动化总控: AutoConnectManager
# ---------------------------------------------------------------------------
class AutoConnectManager:
    """自动化连接总控。

    聚合设备指纹、防封禁策略、MCPC 挑战处理、重连策略、连接监控,
    对外提供 ``auto_connect`` / ``auto_reconnect`` / ``auto_login`` 等入口。

    典型用法::

        mgr = AutoConnectManager(
            connect_fn=async def (host, port, fp, sauth):
                # 实际连接逻辑 (调用 connection.py / 接入点)
                ...
        )

        # 自动登录
        await mgr.auto_login(account_id="acc-123", sauth_json="...",
                              host="example.com", port=19132)

        # 自动重连 (连接断开时调用)
        await mgr.auto_reconnect()
    """

    def __init__(
        self,
        connect_fn: Optional[ConnectCallback] = None,
        anti_ban: Optional[AntiBanController] = None,
        enhanced: Optional[EnhancedAntiBan] = None,
        max_reconnect_attempts: int = 7,
        reconnect_base_delay: float = 5.0,
    ) -> None:
        self._connect_fn = connect_fn
        self._anti_ban = anti_ban or get_anti_ban_controller()
        # EnhancedAntiBan: 集成 ToolDelta / NexusE 反封禁增强策略
        # (8s 心跳 + ToolDelta 退避 + humanize + Orion + PostponeActions + OperatorMonitor)
        self._enhanced = enhanced or get_enhanced_anti_ban(base_controller=self._anti_ban)
        self._reconnect_policy = ReconnectPolicy(
            max_attempts=max_reconnect_attempts,
            base_delay=reconnect_base_delay,
            enhanced=self._enhanced,
        )
        self._monitor = ConnectionMonitor(
            anti_ban=self._anti_ban,
            enhanced=self._enhanced,
        )
        self._mcpc_handler = MCPCChallengeHandler()
        self._stats = ConnectionStats()
        self._fingerprint_manager = get_fingerprint_manager()
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None
        self._stop_event = asyncio.Event()

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------
    # 属性访问
    # ------------------------------------------------------------------
    @property
    def anti_ban(self) -> AntiBanController:
        return self._anti_ban

    @property
    def enhanced(self) -> EnhancedAntiBan:
        """反封禁增强总控 (ToolDelta / NexusE 逆向策略)。"""
        return self._enhanced

    @property
    def reconnect_policy(self) -> ReconnectPolicy:
        return self._reconnect_policy

    @property
    def monitor(self) -> ConnectionMonitor:
        return self._monitor

    @property
    def mcpc_handler(self) -> MCPCChallengeHandler:
        return self._mcpc_handler

    @property
    def stats(self) -> ConnectionStats:
        return self._stats

    @property
    def fingerprint_manager(self):
        return self._fingerprint_manager

    # ------------------------------------------------------------------
    # 自动登录
    # ------------------------------------------------------------------
    async def auto_login(
        self,
        account_id: str,
        sauth_json: str,
        host: str,
        port: int = 19132,
        device_fingerprint: Optional[DeviceFingerprint] = None,
    ) -> bool:
        """自动登录入口。

        流程:
            1. 获取或生成设备指纹 (按 account_id 隔离)
            2. 触发防封禁动作前钩子 (随机延迟 / 行为模拟)
            3. 调用注入的 ``connect_fn`` 执行实际连接
            4. 启动 MCPC 处理器 + 连接监控
            5. 成功后更新统计; 失败后触发重连策略

        Args:
            account_id: 账号 ID (用于设备指纹隔离)。
            sauth_json: 已保存的 sauth_json (网易登录凭证)。
            host: 服务器主机名。
            port: 服务器端口。
            device_fingerprint: 可选, 强制使用指定设备指纹。

        Returns:
            ``True`` 登录成功; ``False`` 失败。
        """
        async with self._get_lock():
            self._stop_event.clear()
            self._stats.total_attempts += 1

            # 1. 设备指纹
            if device_fingerprint is None:
                try:
                    device_fingerprint = self._fingerprint_manager.get_or_create(
                        account_id=account_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"获取设备指纹失败: {exc}")
                    self._stats.failed_logins += 1
                    self._stats.last_error = f"设备指纹失败: {exc}"
                    await self._monitor.set_state(
                        ConnectionState.FAILED, error=str(exc)
                    )
                    return False

            logger.info(
                f"自动登录: account={account_id} host={host}:{port} "
                f"fp={device_fingerprint.short_summary()}"
            )

            # 2. 防封禁动作前钩子 (在事件循环中运行, 不阻塞)
            await asyncio.to_thread(
                self._anti_ban.wait_before_action, "auto_login"
            )
            if not self._anti_ban.should_proceed():
                logger.warning("防封禁策略阻止登录, 已停止")
                self._stats.failed_logins += 1
                self._stats.last_error = "防封禁策略阻止"
                await self._monitor.set_state(
                    ConnectionState.FAILED, error="anti_ban_blocked"
                )
                return False

            # 3. 状态变更: 连接中
            await self._monitor.set_state(ConnectionState.CONNECTING)

            # 4. 启动 MCPC 处理器 + 监控器
            try:
                await self._mcpc_handler.start()
                await self._monitor.start()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"启动 MCPC/监控器失败 (继续): {exc}")

            # 5. 执行连接
            try:
                if self._connect_fn is None:
                    raise RuntimeError(
                        "未注册 connect_fn, 无法执行实际连接; "
                        "请通过 AutoConnectManager(connect_fn=...) 注入"
                    )
                await self._connect_fn(host, port)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"自动登录失败: {exc}")
                self._stats.failed_logins += 1
                self._stats.last_error = str(exc)
                self._anti_ban.on_action_failure(
                    severity="normal",
                    source="login",
                    message=f"登录失败: {exc}",
                )
                await self._monitor.set_state(
                    ConnectionState.FAILED, error=str(exc)
                )
                return False

            # 6. 成功
            self._stats.successful_logins += 1
            self._stats.last_connected_at = time.time()
            self._reconnect_policy.reset()
            self._anti_ban.on_reconnect_success()
            # 重置 EnhancedAntiBan 状态 (ToolDelta 退避 / 心跳 / Orion 等)
            self._enhanced.on_reconnect_success()
            await self._monitor.set_state(ConnectionState.CONNECTED)
            logger.info(f"自动登录成功: account={account_id}")
            return True

    # ------------------------------------------------------------------
    # 自动重连
    # ------------------------------------------------------------------
    async def auto_reconnect(
        self,
        account_id: str = "",
        sauth_json: str = "",
        host: str = "",
        port: int = 19132,
        error: Optional[Exception] = None,
    ) -> bool:
        """自动重连入口。

        基于 :class:`ReconnectPolicy` 决定是否继续重连,
        并使用 :class:`anti_ban.JitterDelay` 增加抖动。

        Args:
            account_id / sauth_json / host / port: 重连参数 (与 auto_login 一致)。
            error: 上次失败的异常 (用于判断是否应停止重连)。

        Returns:
            ``True`` 重连成功; ``False`` 停止重连。
        """
        # 判断是否应继续重连
        if not self._reconnect_policy.should_retry(error):
            self._stats.last_error = self._reconnect_policy.last_error or "重连停止"
            await self._monitor.set_state(
                ConnectionState.FAILED, error=self._stats.last_error
            )
            return False

        self._stats.reconnect_count += 1
        await self._monitor.set_state(
            ConnectionState.RECONNECTING, error=str(error or "")
        )

        # 计算延迟 (含抖动)
        delay = self._reconnect_policy.next_delay(self._anti_ban)
        logger.info(f"等待 {delay:.1f}s 后重连 (attempt={self._reconnect_policy.attempt})")

        # 等待 (允许被打断)
        try:
            await asyncio.wait_for(
                self._stop_event.wait(), timeout=delay
            )
            # 被主动停止
            logger.info("重连等待被打断 (主动停止)")
            return False
        except asyncio.TimeoutError:
            pass  # 正常超时, 继续重连

        # 触发自动登录
        if not account_id or not host:
            logger.error("重连参数缺失 (account_id / host 必填)")
            return False

        return await self.auto_login(
            account_id=account_id,
            sauth_json=sauth_json,
            host=host,
            port=port,
        )

    # ------------------------------------------------------------------
    # 自动 MCPC 挑战处理
    # ------------------------------------------------------------------
    def register_mcpc_solver(self, solver: MCPCChallengeCallback) -> None:
        """注册 MCPC 挑战求解器。

        Args:
            solver: ``async (challenge) -> bool`` 回调。
        """
        self._mcpc_handler.register_solver(solver)

    def submit_mcpc_challenge(self, challenge: MCPCChallenge) -> None:
        """提交一个 MCPC 挑战 (供 ``connection.py`` 收到挑战包时调用)。"""
        self._stats.mcpc_challenges_received += 1
        self._mcpc_handler.submit(challenge)

    async def wait_mcpc_challenges_down(
        self, timeout: Optional[float] = None
    ) -> bool:
        """等待所有 MCPC 挑战完成 (对应逆向 ``waitMCPCheckChallengesDown``)。"""
        result = await self._mcpc_handler.wait_all_down(timeout=timeout)
        if result:
            self._stats.mcpc_challenges_solved = self._mcpc_handler.solved_count
            self._stats.mcpc_challenges_skipped = self._mcpc_handler.skipped_count
        return result

    # ------------------------------------------------------------------
    # 自动速率调整 (由 anti_ban 模块负责)
    # ------------------------------------------------------------------
    def on_response_ok(self) -> None:
        """服务器响应正常 -> 触发升速。"""
        self._anti_ban.on_action_success()

    def on_response_error(self, severity: str = "normal", message: str = "") -> None:
        """服务器响应异常 -> 触发降速。"""
        self._anti_ban.on_action_failure(
            severity=severity, source="response", message=message
        )

    def on_chat_message(self, message: str) -> Optional[str]:
        """收到聊天消息 -> 检测反作弊关键词。"""
        return self._anti_ban.on_chat_message(message)

    # ------------------------------------------------------------------
    # 状态监控
    # ------------------------------------------------------------------
    async def on_disconnect(self, reason: str = "") -> None:
        """连接断开时调用。"""
        self._stats.last_disconnected_at = time.time()
        self._stats.last_error = reason
        await self._monitor.set_state(
            ConnectionState.DISCONNECTED, error=reason
        )

    async def on_connect(self) -> None:
        """连接成功时调用。"""
        self._stats.last_connected_at = time.time()
        await self._monitor.set_state(ConnectionState.CONNECTED)

    async def on_packet(self) -> None:
        """收到数据包时调用。"""
        self._monitor.record_packet()

    async def on_heartbeat(self) -> None:
        """发送心跳时调用。"""
        self._monitor.record_heartbeat()

    # ------------------------------------------------------------------
    # 增强反封禁 (EnhancedAntiBan 集成)
    # ------------------------------------------------------------------
    async def start_heartbeat(
        self,
        send_callback: Callable[[], Awaitable[Any]],
    ) -> None:
        """启动 ToolDelta 风格 8s 应用层心跳。

        Args:
            send_callback: 心跳发送回调 (通常是 ``testfor @s``)。
        """
        await self._enhanced.start_heartbeat(send_callback)

    async def stop_heartbeat(self) -> None:
        """停止心跳任务。"""
        await self._enhanced.stop_heartbeat()

    def before_send_command(self, command: str) -> Tuple[str, str]:
        """命令发送前钩子 (人类化 + Orion + UUID)。

        Returns:
            (humanized_command, command_uuid)
        """
        return self._enhanced.before_send_command(command)

    def on_command_response(self, command_uuid: str, success: bool = True) -> None:
        """命令响应到达时调用。"""
        self._enhanced.on_command_response(command_uuid, success)

    async def on_challenge_start(self) -> None:
        """MCPC 挑战开始时调用 (暂停动作执行)。"""
        await self._enhanced.on_challenge_start()

    async def on_challenge_passed(self) -> None:
        """MCPC 挑战通过后调用 (恢复动作执行)。"""
        await self._enhanced.on_challenge_passed()

    async def execute_or_postpone(
        self,
        action: Callable[[], Awaitable[Any]],
    ) -> bool:
        """执行或排队动作 (挑战期间排队)。

        Returns:
            ``True`` 已排队; ``False`` 已直接执行。
        """
        return await self._enhanced.execute_or_postpone(action)

    def on_set_command_enabled(self, command: str, enabled: bool) -> None:
        """收到 SetCommandEnabled 包时调用 (OP 权限监控)。"""
        self._enhanced.on_set_command_enabled(command, enabled)

    def on_op_level_change(self, op_level: int) -> None:
        """OP 等级变更时调用。"""
        self._enhanced.on_op_level_change(op_level)

    def record_attack(self) -> bool:
        """记录一次攻击 (Orion 阈值监控)。

        Returns:
            ``True`` 超过阈值; ``False`` 正常。
        """
        return self._enhanced.orion.record_attack()

    def record_movement(self, distance_blocks: float) -> bool:
        """记录一次移动 (Orion 阈值监控)。

        Returns:
            ``True`` 超过阈值 (飞行/瞬移); ``False`` 正常。
        """
        return self._enhanced.orion.record_movement(distance_blocks)

    def check_command_timeouts(self) -> List[str]:
        """检查超时的命令 (返回超时命令 UUID 列表)。"""
        return self._enhanced.command_tracker.check_timeouts()

    # ------------------------------------------------------------------
    # 停止
    # ------------------------------------------------------------------
    async def stop(self) -> None:
        """停止所有自动化任务。"""
        self._stop_event.set()
        await self._enhanced.stop_heartbeat()
        await self._mcpc_handler.stop()
        await self._monitor.stop()
        logger.info("自动化连接管理器已停止")

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def get_stats(self) -> Dict[str, Any]:
        """返回完整统计信息 (供 API 使用)。"""
        return {
            "connection": self._stats.to_dict(),
            "anti_ban": self._anti_ban.stats(),
            "enhanced_anti_ban": self._enhanced.stats(),
            "mcpc": self._mcpc_handler.stats(),
            "monitor": self._monitor.stats(),
            "reconnect": {
                "attempt": self._reconnect_policy.attempt,
                "last_error": self._reconnect_policy.last_error,
                "last_layer": self._reconnect_policy.last_layer,
            },
        }


# ---------------------------------------------------------------------------
# 全局单例 (不直接持有 connect_fn, 由 bot.py 注入)
# ---------------------------------------------------------------------------
_global_managers: Dict[str, AutoConnectManager] = {}
# C-4 修复: 不再在模块级创建 asyncio.Lock() (会在事件循环切换时崩溃)
# 改为懒加载: 首次需要时在当前事件循环中创建
_global_lock: Optional[asyncio.Lock] = None


async def _get_global_lock() -> asyncio.Lock:
    """获取全局锁 (C-4 修复: 懒加载, 绑定到当前事件循环)。

    模块级 ``asyncio.Lock()`` 在导入时创建, 会绑定到当时的(可能不存在的)
    事件循环。当后续切换到新的事件循环时, 旧锁会抛出
    ``RuntimeError: ... is bound to a different event loop``。

    改为懒加载后, 锁在首次调用时创建, 绑定到当前运行的事件循环。
    """
    global _global_lock
    if _global_lock is None:
        _global_lock = asyncio.Lock()
    return _global_lock


async def get_auto_connect_manager(bot_id: str = "default") -> AutoConnectManager:
    """获取或创建与 bot 关绑定的 :class:`AutoConnectManager`。"""
    lock = await _get_global_lock()
    async with lock:
        if bot_id not in _global_managers:
            _global_managers[bot_id] = AutoConnectManager()
        return _global_managers[bot_id]


async def remove_auto_connect_manager(bot_id: str) -> None:
    """移除并停止与 bot 绑定的管理器。"""
    lock = await _get_global_lock()
    async with lock:
        mgr = _global_managers.pop(bot_id, None)
    if mgr is not None:
        await mgr.stop()


__all__ = [
    # 枚举
    "ConnectionState",
    "MCPCChallengeType",
    # 数据
    "MCPCChallenge",
    "ConnectionStats",
    # 组件
    "MCPCChallengeHandler",
    "ReconnectPolicy",
    "ConnectionMonitor",
    "AutoConnectManager",
    # 单例
    "get_auto_connect_manager",
    "remove_auto_connect_manager",
    # 回调类型
    "ConnectCallback",
    "DisconnectCallback",
    "MCPCChallengeCallback",
]
