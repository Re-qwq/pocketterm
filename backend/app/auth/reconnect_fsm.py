"""PocketTerm 分层重连状态机 (Reconnect FSM)

本模块从 NexusE 逆向的 5 层重连策略提取, 实现分层逐级重连状态机,
覆盖网络层、任务层、命令层、OP 层、反馈层五个独立层级。

逆向来源 (NexusE)
------------------

NexusE 在 ``modules/reconnect`` 中维护一个 5 层重连系统:

1. **网络层 (Network)**: 底层 TCP/UDP 连接断开时触发
2. **任务层 (Task)**: 异步任务调度失败时触发
3. **命令层 (Command)**: 命令发送/响应失败时触发
4. **OP 层 (OP)**: 操作员权限丢失时触发
5. **反馈层 (Feedback)**: 心跳/keepalive 无响应时触发

每层独立退避策略, 包含 ``retryInterval``、``retryCount``、``maxRetry``、
``retryAt`` 等字段。

分层设计
--------

分层重连的核心思想是: 不同层级的断线原因使用不同的重连策略。

- 网络层: 快速重试 (2-5s), 因为网络抖动通常是瞬时的
- 任务层: 中等退避 (5-15s), 等待任务调度器恢复
- 命令层: 标准退避 (10-30s), 等待服务器解除限流
- OP 层: 长退避 (30-60s), OP 权限恢复通常需要较长时间
- 反馈层: 最长退避 (60-300s), 仅在心跳彻底失效时触发

指数退避公式
--------------

参考 ToolDelta 的指数退避::

    delay = min(5 * 2^(n-1), 300) 秒

其中 n 为当前重试次数, 序列为::

    [5, 10, 20, 40, 80, 160, 300, 300, ...]

与 ToolDelta 的 ``[5, 10, 20, 40, 80, 160, 300]`` 一致。

类组织
------

- :class:`ReconnectReason`          -- 断线原因枚举
- :class:`RetryConfig`              -- 重试配置
- :class:`ReconnectLayer`           -- 单个重连层
- :class:`ReconnectFSM`             -- 分层状态机总控
- :class:`ReconnectWatchdog`        -- 看门狗定时器
"""
from __future__ import annotations

import asyncio
import enum
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..logger import get_logger

logger = get_logger("auth.reconnect_fsm")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ReconnectReason(enum.Enum):
    """断线原因枚举 (逆向自 NexusE ``importReconnectReason``)。

    每种断线原因对应不同的重连层级和排空延迟。
    """

    #: 网络层断开 (TCP/UDP 连接丢失)
    NETWORK = "network"

    #: 任务层失败 (异步任务调度失败)
    TASK = "task"

    #: 命令层失败 (命令发送/响应超时)
    COMMAND = "command"

    #: OP 层丢失 (操作员权限被撤销)
    OP = "op"

    #: 反馈层超时 (心跳/keepalive 无响应)
    FEEDBACK = "feedback"

    #: 未知原因
    UNKNOWN = "unknown"

    #: 手动断开 (用户主动断开)
    MANUAL = "manual"

    #: 服务器踢出 (Kicked from server)
    KICKED = "kicked"

    @property
    def drain_delay(self) -> float:
        """排空延迟 (秒) - 不同断线原因需要不同的等待时间。

        反向自 NexusE ``importReconnectReason`` 中的排空逻辑。
        """
        mapping = {
            ReconnectReason.NETWORK: 2.0,    # 网络抖动, 快速重试
            ReconnectReason.TASK: 5.0,       # 任务恢复, 中等等待
            ReconnectReason.COMMAND: 10.0,   # 命令限流, 标准等待
            ReconnectReason.OP: 30.0,        # OP 恢复, 较长等待
            ReconnectReason.FEEDBACK: 60.0,  # 心跳失效, 最长等待
            ReconnectReason.UNKNOWN: 15.0,   # 未知, 中等等待
            ReconnectReason.MANUAL: 0.0,     # 手动, 不等待
            ReconnectReason.KICKED: 30.0,    # 被踢, 较长等待
        }
        return mapping.get(self, 15.0)

    @property
    def default_layer(self) -> str:
        """获取断线原因对应的默认重连层。"""
        mapping = {
            ReconnectReason.NETWORK: "network",
            ReconnectReason.TASK: "task",
            ReconnectReason.COMMAND: "command",
            ReconnectReason.OP: "op",
            ReconnectReason.FEEDBACK: "feedback",
            ReconnectReason.UNKNOWN: "network",
            ReconnectReason.MANUAL: "network",
            ReconnectReason.KICKED: "network",
        }
        return mapping.get(self, "network")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class RetryConfig:
    """单层重试配置 (逆向自 NexusE ``retryConfig``)。

    每层独立配置以下参数:
        - retry_interval: 基础重试间隔 (秒)
        - max_retry: 最大重试次数 (0 = 无限)
        - backoff_base: 指数退避基数
        - jitter_ratio: 抖动比例
    """

    #: 基础重试间隔 (秒)
    retry_interval: float = 5.0

    #: 最大重试次数 (0 = 无限制)
    max_retry: int = 10

    #: 指数退避基数 (delay = base * 2^(n-1))
    backoff_base: float = 5.0

    #: 最大退避延迟 (秒) - 参考 ToolDelta 的 300s 上限
    max_backoff: float = 300.0

    #: 抖动比例 (实际 = delay * (1 +/- jitter_ratio))
    jitter_ratio: float = 0.3

    #: 是否启用指数退避 (False = 使用固定间隔)
    enable_backoff: bool = True


# ---------------------------------------------------------------------------
# 预定义层级配置
# ---------------------------------------------------------------------------

#: 网络层: 快速重试, 应对瞬时网络抖动
NETWORK_RETRY_CONFIG = RetryConfig(
    retry_interval=3.0,
    max_retry=20,
    backoff_base=2.0,
    max_backoff=60.0,
    jitter_ratio=0.2,
    enable_backoff=True,
)

#: 任务层: 中等退避, 等待任务调度器恢复
TASK_RETRY_CONFIG = RetryConfig(
    retry_interval=5.0,
    max_retry=10,
    backoff_base=5.0,
    max_backoff=120.0,
    jitter_ratio=0.3,
    enable_backoff=True,
)

#: 命令层: 标准退避, 等待服务器解除限流
COMMAND_RETRY_CONFIG = RetryConfig(
    retry_interval=10.0,
    max_retry=7,
    backoff_base=5.0,
    max_backoff=300.0,
    jitter_ratio=0.3,
    enable_backoff=True,
)

#: OP 层: 较长退避, OP 权限恢复需要时间
OP_RETRY_CONFIG = RetryConfig(
    retry_interval=30.0,
    max_retry=5,
    backoff_base=5.0,
    max_backoff=300.0,
    jitter_ratio=0.2,
    enable_backoff=True,
)

#: 反馈层: 最长退避, 仅在心跳彻底失效时触发
FEEDBACK_RETRY_CONFIG = RetryConfig(
    retry_interval=60.0,
    max_retry=3,
    backoff_base=5.0,
    max_backoff=300.0,
    jitter_ratio=0.1,
    enable_backoff=True,
)

#: 层名称到默认配置的映射
LAYER_CONFIGS: Dict[str, RetryConfig] = {
    "network": NETWORK_RETRY_CONFIG,
    "task": TASK_RETRY_CONFIG,
    "command": COMMAND_RETRY_CONFIG,
    "op": OP_RETRY_CONFIG,
    "feedback": FEEDBACK_RETRY_CONFIG,
}


# ---------------------------------------------------------------------------
# ReconnectLayer
# ---------------------------------------------------------------------------

class ReconnectLayer:
    """单个重连层 (逆向自 NexusE 分层重连策略)。

    每层独立维护:
        - retryCount: 当前重试次数
        - retryInterval: 当前重试间隔
        - maxRetry: 最大重试次数
        - retryAt: 下次重试时间

    功能:
        - 计算指数退避延迟
        - 跟踪重试次数
        - 判断是否超过最大重试
        - 提供重置和状态查询
    """

    def __init__(
        self,
        name: str,
        config: Optional[RetryConfig] = None,
        on_retry: Optional[Callable[["ReconnectLayer"], None]] = None,
    ) -> None:
        """
        Args:
            name: 层名称 (如 "network", "task", "command", "op", "feedback")。
            config: 重试配置, 默认使用 LAYER_CONFIGS 中的预定义配置。
            on_retry: 重试回调 (每次重试前调用)。
        """
        self._name: str = name
        self._config: RetryConfig = config or LAYER_CONFIGS.get(name, RetryConfig())
        self._on_retry: Optional[Callable[[ReconnectLayer], None]] = on_retry
        self._lock: threading.Lock = threading.Lock()

        # 运行时状态
        self._retry_count: int = 0
        self._retry_at: float = 0.0
        self._last_delay: float = 0.0
        self._total_retries: int = 0
        self._last_success_at: float = 0.0
        self._last_failure_at: float = 0.0
        self._enabled: bool = True

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """层名称。"""
        return self._name

    @property
    def retry_count(self) -> int:
        """当前重试次数。"""
        with self._lock:
            return self._retry_count

    @property
    def retry_at(self) -> float:
        """下次重试时间 (Unix 时间戳)。"""
        with self._lock:
            return self._retry_at

    @property
    def last_delay(self) -> float:
        """上次计算的延迟 (秒)。"""
        with self._lock:
            return self._last_delay

    @property
    def enabled(self) -> bool:
        """是否启用此层。"""
        with self._lock:
            return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        with self._lock:
            self._enabled = value

    @property
    def is_exhausted(self) -> bool:
        """是否已耗尽重试次数。"""
        with self._lock:
            if self._config.max_retry == 0:
                return False  # 无限制
            return self._retry_count >= self._config.max_retry

    @property
    def seconds_until_retry(self) -> float:
        """距离下次重试的秒数。"""
        with self._lock:
            if self._retry_at == 0.0:
                return 0.0
            return max(0.0, self._retry_at - time.time())

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def compute_delay(self) -> float:
        """计算下一次重连延迟 (秒), 含指数退避和抖动。

        指数退避公式::

            delay = min(backoff_base * 2^(n-1), max_backoff)

        其中 n 为当前重试次数 (retry_count)。

        参考 ToolDelta 的退避序列::

            [5, 10, 20, 40, 80, 160, 300, 300, ...]

        Returns:
            延迟秒数 (含抖动)。
        """
        import random

        with self._lock:
            self._retry_count += 1
            self._total_retries += 1
            n = self._retry_count

            if self._config.enable_backoff:
                delay = min(
                    self._config.backoff_base * (2 ** (n - 1)),
                    self._config.max_backoff,
                )
            else:
                delay = self._config.retry_interval

            # 抖动
            jitter = random.uniform(
                -self._config.jitter_ratio,
                self._config.jitter_ratio,
            )
            delay = max(1.0, delay * (1.0 + jitter))

            self._last_delay = delay
            self._retry_at = time.time() + delay
            self._last_failure_at = time.time()

            logger.debug(
                f"ReconnectLayer [{self._name}]: retry={n}/{self._config.max_retry}, "
                f"delay={delay:.1f}s, retry_at={self._retry_at}"
            )

            return delay

    def reset(self) -> None:
        """重置重试计数 (连接成功后调用)。"""
        with self._lock:
            self._retry_count = 0
            self._retry_at = 0.0
            self._last_delay = 0.0
            self._last_success_at = time.time()
            logger.debug(f"ReconnectLayer [{self._name}] 已重置")

    def reset_all(self) -> None:
        """完全重置, 包括总计数。"""
        with self._lock:
            self._retry_count = 0
            self._total_retries = 0
            self._retry_at = 0.0
            self._last_delay = 0.0
            self._last_success_at = 0.0
            self._last_failure_at = 0.0
            logger.debug(f"ReconnectLayer [{self._name}] 已完全重置")

    def should_retry(self) -> bool:
        """是否应该继续重试。

        Returns:
            True 如果未耗尽且层已启用。
        """
        with self._lock:
            if not self._enabled:
                return False
            if self._config.max_retry == 0:
                return True
            return self._retry_count < self._config.max_retry

    def is_ready(self) -> bool:
        """是否已到达重试时间。

        Returns:
            True 如果 retry_at 已过或未设置。
        """
        with self._lock:
            if self._retry_at == 0.0:
                return True
            return time.time() >= self._retry_at

    def stats(self) -> Dict[str, Any]:
        """返回层状态统计。"""
        with self._lock:
            return {
                "name": self._name,
                "enabled": self._enabled,
                "retry_count": self._retry_count,
                "total_retries": self._total_retries,
                "max_retry": self._config.max_retry,
                "is_exhausted": self.is_exhausted,
                "last_delay": round(self._last_delay, 2),
                "retry_at": self._retry_at,
                "seconds_until_retry": self.seconds_until_retry,
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "backoff_base": self._config.backoff_base,
                "max_backoff": self._config.max_backoff,
            }


# ---------------------------------------------------------------------------
# ReconnectWatchdog
# ---------------------------------------------------------------------------

class ReconnectWatchdog:
    """重连看门狗定时器 (逆向自 NexusE ``importReconnectWatchdogDrain``)。

    看门狗负责:
        - 监控重连进度
        - 在超时时强制触发重连
        - 排除已完成的层
    """

    def __init__(
        self,
        timeout: float = 300.0,
        on_timeout: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Args:
            timeout: 看门狗超时 (秒), 默认 300s。
            on_timeout: 超时回调。
        """
        self._timeout: float = timeout
        self._on_timeout: Optional[Callable[[], None]] = on_timeout
        self._lock: threading.Lock = threading.Lock()
        self._started_at: float = 0.0
        self._timer: Optional[threading.Timer] = None
        self._triggered: bool = False

    @property
    def is_running(self) -> bool:
        """看门狗是否在运行。"""
        with self._lock:
            return self._timer is not None and self._timer.is_alive()

    def start(self) -> None:
        """启动看门狗。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._started_at = time.time()
            self._triggered = False
            self._timer = threading.Timer(
                self._timeout,
                self._on_watchdog_timeout,
            )
            self._timer.daemon = True
            self._timer.start()
            logger.debug(f"ReconnectWatchdog 已启动: timeout={self._timeout}s")

    def reset(self) -> None:
        """重置看门狗 (重新计时)。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._started_at = time.time()
            self._triggered = False
            self._timer = threading.Timer(
                self._timeout,
                self._on_watchdog_timeout,
            )
            self._timer.daemon = True
            self._timer.start()
            logger.debug(f"ReconnectWatchdog 已重置: timeout={self._timeout}s")

    def stop(self) -> None:
        """停止看门狗。"""
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            logger.debug("ReconnectWatchdog 已停止")

    def _on_watchdog_timeout(self) -> None:
        """看门狗超时处理。"""
        with self._lock:
            self._triggered = True
        logger.warning(
            f"ReconnectWatchdog 超时: timeout={self._timeout}s, "
            f"elapsed={time.time() - self._started_at:.1f}s"
        )
        if self._on_timeout is not None:
            try:
                self._on_timeout()
            except Exception as exc:
                logger.error(f"ReconnectWatchdog 超时回调异常: {exc}")


# ---------------------------------------------------------------------------
# ReconnectFSM
# ---------------------------------------------------------------------------

class ReconnectFSM:
    """分层重连状态机总控 (逆向自 NexusE 5 层重连策略)。

    维护 5 个独立重连层:
        1. 网络层 (network)
        2. 任务层 (task)
        3. 命令层 (command)
        4. OP 层 (op)
        5. 反馈层 (feedback)

    每层独立退避, 按顺序逐层重试。上一层成功后自动重置,
    所有层均成功后视为重连成功。

    支持:
        - 自动判定是否需要重连 (``should_auto_reconnect``)
        - 看门狗监控 (``watchdog``)
        - 按断线原因排空延迟 (``drain``)

    使用示例::

        fsm = ReconnectFSM()
        # 触发网络层重连
        reason = ReconnectReason.NETWORK
        await fsm.start_reconnect(reason)
        # 等待重连完成
        while not fsm.is_connected:
            await fsm.tick()
            await asyncio.sleep(1)
    """

    #: 层名称列表 (按优先级排序)
    LAYER_NAMES: Tuple[str, ...] = (
        "network",
        "task",
        "command",
        "op",
        "feedback",
    )

    def __init__(
        self,
        layer_configs: Optional[Dict[str, RetryConfig]] = None,
        watchdog_timeout: float = 300.0,
        on_reconnect_success: Optional[Callable[[], None]] = None,
        on_reconnect_failure: Optional[Callable[[ReconnectReason], None]] = None,
    ) -> None:
        """
        Args:
            layer_configs: 各层配置字典, 默认使用 LAYER_CONFIGS。
            watchdog_timeout: 看门狗超时 (秒)。
            on_reconnect_success: 重连成功回调。
            on_reconnect_failure: 重连失败回调 (参数为断线原因)。
        """
        configs = layer_configs or LAYER_CONFIGS
        self._layers: Dict[str, ReconnectLayer] = {}
        for name in self.LAYER_NAMES:
            self._layers[name] = ReconnectLayer(
                name=name,
                config=configs.get(name, RetryConfig()),
            )

        self._on_reconnect_success: Optional[Callable[[], None]] = on_reconnect_success
        self._on_reconnect_failure: Optional[Callable[[ReconnectReason], None]] = (
            on_reconnect_failure
        )

        self._watchdog: ReconnectWatchdog = ReconnectWatchdog(
            timeout=watchdog_timeout,
            on_timeout=self._on_watchdog_expired,
        )

        # 异步锁
        self._async_lock: asyncio.Lock = asyncio.Lock()
        self._lock: threading.Lock = threading.Lock()

        # 运行时状态
        self._is_connected: bool = True
        self._is_reconnecting: bool = False
        self._current_reason: ReconnectReason = ReconnectReason.UNKNOWN
        self._reconnect_started_at: float = 0.0
        self._reconnect_success_count: int = 0
        self._reconnect_failure_count: int = 0
        self._drain_until: float = 0.0

        # 每层的重连回调
        self._layer_callbacks: Dict[str, Callable[[], bool]] = {}

        logger.info(
            f"ReconnectFSM 初始化: layers={list(self._layers.keys())}, "
            f"watchdog_timeout={watchdog_timeout}s"
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        with self._lock:
            return self._is_connected

    @property
    def is_reconnecting(self) -> bool:
        """是否正在重连。"""
        with self._lock:
            return self._is_reconnecting

    @property
    def current_reason(self) -> ReconnectReason:
        """当前断线原因。"""
        with self._lock:
            return self._current_reason

    @property
    def drain_until(self) -> float:
        """排空结束时间 (Unix 时间戳)。"""
        with self._lock:
            return self._drain_until

    @property
    def layers(self) -> Dict[str, ReconnectLayer]:
        """获取所有重连层。"""
        return self._layers

    def get_layer(self, name: str) -> Optional[ReconnectLayer]:
        """获取指定名称的重连层。

        Args:
            name: 层名称。

        Returns:
            重连层对象, 不存在则返回 None。
        """
        return self._layers.get(name)

    # ------------------------------------------------------------------
    # 回调注册
    # ------------------------------------------------------------------

    def set_layer_callback(
        self,
        layer_name: str,
        callback: Callable[[], bool],
    ) -> None:
        """注册指定层的重连回调。

        Args:
            layer_name: 层名称 (如 "network", "task")。
            callback: 回调函数, 返回 True 表示重连成功。
        """
        self._layer_callbacks[layer_name] = callback
        logger.debug(f"ReconnectFSM [{layer_name}] 回调已注册")

    def clear_layer_callback(self, layer_name: str) -> None:
        """清除指定层的重连回调。"""
        self._layer_callbacks.pop(layer_name, None)

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    async def start_reconnect(self, reason: ReconnectReason) -> None:
        """开始重连流程。

        根据断线原因:
        1. 设置排空延迟 (drain)
        2. 重置相关层的重试计数
        3. 启动看门狗
        4. 按顺序重试各层

        Args:
            reason: 断线原因。
        """
        async with self._async_lock:
            with self._lock:
                self._is_connected = False
                self._is_reconnecting = True
                self._current_reason = reason
                self._reconnect_started_at = time.time()
                self._drain_until = time.time() + reason.drain_delay

            logger.warning(
                f"ReconnectFSM 开始重连: reason={reason.value}, "
                f"drain={reason.drain_delay}s"
            )

            # 启动看门狗
            self._watchdog.start()

            # 执行排空等待
            drain = reason.drain_delay
            if drain > 0:
                logger.info(f"ReconnectFSM 排空等待: {drain}s")
                await asyncio.sleep(drain)

            # 从最底层开始逐层重试
            start_layer = self._get_start_layer(reason)
            await self._retry_layers(start_layer)

    async def _retry_layers(self, start_layer: str) -> None:
        """从指定层开始, 按顺序重试所有层。

        Args:
            start_layer: 起始层名称。
        """
        layer_names = self.LAYER_NAMES
        start_idx = layer_names.index(start_layer) if start_layer in layer_names else 0

        for i in range(start_idx, len(layer_names)):
            layer_name = layer_names[i]
            layer = self._layers[layer_name]

            if not layer.enabled:
                logger.debug(f"ReconnectFSM [{layer_name}] 已禁用, 跳过")
                continue

            logger.info(f"ReconnectFSM 开始重试层 [{layer_name}]")

            while layer.should_retry():
                # 等待重试时间
                while not layer.is_ready():
                    await asyncio.sleep(0.5)

                # 计算延迟并执行重连
                delay = layer.compute_delay()
                logger.debug(
                    f"ReconnectFSM [{layer_name}]: "
                    f"attempt={layer.retry_count}, delay={delay:.1f}s"
                )

                # 执行重连回调
                success = await self._execute_layer_callback(layer_name)

                if success:
                    layer.reset()
                    logger.info(f"ReconnectFSM [{layer_name}] 重连成功")
                    self._watchdog.reset()
                    break

                # 等待退避延迟
                await asyncio.sleep(delay)

            if layer.is_exhausted:
                logger.error(
                    f"ReconnectFSM [{layer_name}] 重试耗尽: "
                    f"retry_count={layer.retry_count}/{layer._config.max_retry}"
                )
                await self._on_all_layers_failed()
                return

        # 所有层都成功
        await self._on_reconnect_complete()

    def _get_start_layer(self, reason: ReconnectReason) -> str:
        """根据断线原因确定起始重连层。

        Args:
            reason: 断线原因。

        Returns:
            起始层名称。
        """
        return reason.default_layer

    async def _execute_layer_callback(self, layer_name: str) -> bool:
        """执行指定层的重连回调。

        Args:
            layer_name: 层名称。

        Returns:
            True 如果回调返回 True, 否则 False。
        """
        callback = self._layer_callbacks.get(layer_name)
        if callback is None:
            logger.warning(
                f"ReconnectFSM [{layer_name}] 未注册回调, 假定成功"
            )
            return True

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(None, callback)
            return bool(result)
        except Exception as exc:
            logger.error(
                f"ReconnectFSM [{layer_name}] 回调异常: {exc}",
                exc_info=True,
            )
            return False

    async def _on_reconnect_complete(self) -> None:
        """所有层重连成功后的处理。"""
        with self._lock:
            self._is_connected = True
            self._is_reconnecting = False
            self._reconnect_success_count += 1

        self._watchdog.stop()
        elapsed = time.time() - self._reconnect_started_at

        logger.info(
            f"ReconnectFSM 重连成功: reason={self._current_reason.value}, "
            f"elapsed={elapsed:.1f}s"
        )

        if self._on_reconnect_success is not None:
            try:
                self._on_reconnect_success()
            except Exception as exc:
                logger.error(f"ReconnectFSM 成功回调异常: {exc}", exc_info=True)

    async def _on_all_layers_failed(self) -> None:
        """所有层都失败后的处理。"""
        with self._lock:
            self._is_connected = False
            self._is_reconnecting = False
            self._reconnect_failure_count += 1

        self._watchdog.stop()
        elapsed = time.time() - self._reconnect_started_at

        logger.error(
            f"ReconnectFSM 所有层重试失败: reason={self._current_reason.value}, "
            f"elapsed={elapsed:.1f}s"
        )

        if self._on_reconnect_failure is not None:
            try:
                self._on_reconnect_failure(self._current_reason)
            except Exception as exc:
                logger.error(f"ReconnectFSM 失败回调异常: {exc}", exc_info=True)

    def _on_watchdog_expired(self) -> None:
        """看门狗超时处理。"""
        logger.error(
            f"ReconnectFSM 看门狗超时: reason={self._current_reason.value}, "
            f"elapsed={time.time() - self._reconnect_started_at:.1f}s"
        )
        # 标记所有层为已耗尽, 强制停止重试
        for layer in self._layers.values():
            layer.reset()

    # ------------------------------------------------------------------
    # 自动判定
    # ------------------------------------------------------------------

    def should_auto_reconnect(self, reason: ReconnectReason) -> bool:
        """自动判定是否应该重连 (逆向自 NexusE ``shouldAutoReconnectImport``)。

        以下情况不自动重连:
            - 手动断开 (MANUAL)
            - 被服务器踢出 (KICKED) - 可能是封禁

        Args:
            reason: 断线原因。

        Returns:
            True 如果应该自动重连。
        """
        no_reconnect = {ReconnectReason.MANUAL, ReconnectReason.KICKED}
        if reason in no_reconnect:
            logger.info(f"ReconnectFSM: 不自动重连, reason={reason.value}")
            return False
        return True

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def mark_connected(self) -> None:
        """标记为已连接 (外部调用, 用于手动恢复连接状态)。"""
        with self._lock:
            self._is_connected = True
            self._is_reconnecting = False
        # 重置所有层
        for layer in self._layers.values():
            layer.reset()
        self._watchdog.stop()
        logger.info("ReconnectFSM 已标记为已连接")

    def mark_disconnected(self, reason: ReconnectReason = ReconnectReason.UNKNOWN) -> None:
        """标记为已断开 (外部调用)。

        Args:
            reason: 断线原因。
        """
        with self._lock:
            self._is_connected = False
            self._current_reason = reason
        logger.warning(f"ReconnectFSM 已标记为断开: reason={reason.value}")

    def cancel_reconnect(self) -> None:
        """取消当前重连 (外部调用)。"""
        with self._lock:
            self._is_reconnecting = False
        self._watchdog.stop()
        for layer in self._layers.values():
            layer.reset()
        logger.info("ReconnectFSM 已取消重连")

    def reset(self) -> None:
        """完全重置状态机 (测试/重启用)。"""
        with self._lock:
            self._is_connected = True
            self._is_reconnecting = False
            self._current_reason = ReconnectReason.UNKNOWN
            self._reconnect_started_at = 0.0
            self._drain_until = 0.0
        self._watchdog.stop()
        for layer in self._layers.values():
            layer.reset_all()
        logger.info("ReconnectFSM 已完全重置")

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """返回状态机统计信息。"""
        with self._lock:
            layers_stats = {
                name: layer.stats() for name, layer in self._layers.items()
            }
            return {
                "is_connected": self._is_connected,
                "is_reconnecting": self._is_reconnecting,
                "current_reason": self._current_reason.value,
                "reconnect_started_at": self._reconnect_started_at,
                "reconnect_success_count": self._reconnect_success_count,
                "reconnect_failure_count": self._reconnect_failure_count,
                "drain_until": self._drain_until,
                "watchdog_running": self._watchdog.is_running,
                "layers": layers_stats,
            }


__all__ = [
    "ReconnectReason",
    "RetryConfig",
    "ReconnectLayer",
    "ReconnectFSM",
    "ReconnectWatchdog",
    "NETWORK_RETRY_CONFIG",
    "TASK_RETRY_CONFIG",
    "COMMAND_RETRY_CONFIG",
    "OP_RETRY_CONFIG",
    "FEEDBACK_RETRY_CONFIG",
    "LAYER_CONFIGS",
]