"""PocketTerm 业务巡检保活系统 (Business Heartbeat Monitor)

本模块从 NexusE 逆向的 ``check bot command status each 10s`` 机制提取,
实现业务级巡检保活, 与 ToolDelta 的 8s 应用层心跳 (``anti_ban_enhanced.HeartbeatMonitor``)
形成互补。

逆向来源 (NexusE)
------------------

NexusE 在 ``modules/heartbeat`` 中维护一个独立的 10s 业务巡检循环:

- 每 10 秒发送一条 ``testfor @s`` 命令
- 连续 3 次失败判定为掉线
- 触发异步回调 ``on_disconnect``
- 支持暂停/恢复 (用于重连期间)

与 ToolDelta 8s 心跳的差异
----------------------------

===========  ================  =====================  ==============
特性         ToolDelta 8s 心跳   BusinessHeartbeat 10s  说明
===========  ================  =====================  ==============
用途         应用层存活检测      业务层巡检保活          不同层级
频率         8s +/- 2s jitter   10s (可配置)            不同间隔
失败阈值     3 次 (24s)         3 次 (可配置)           相同策略
设计来源     ToolDelta 源码      NexusE 逆向            不同逆向源
===========  ================  =====================  ==============

两者互补: ToolDelta 心跳负责快速检测连接断开, BusinessHeartbeat
负责业务级巡检 (如命令响应是否正常)。

类组织
------

- :class:`HeartbeatConfig`     -- 心跳配置
- :class:`BusinessHeartbeat`   -- 业务巡检心跳
- :class:`HeartbeatState`      -- 心跳状态枚举
"""
from __future__ import annotations

import asyncio
import enum
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from ..logger import get_logger

logger = get_logger("auth.heartbeat_monitor")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class HeartbeatState(enum.Enum):
    """业务巡检心跳状态。"""

    #: 未启动
    IDLE = "idle"
    #: 运行中
    RUNNING = "running"
    #: 已暂停 (通常因重连而暂停)
    PAUSED = "paused"
    #: 已停止
    STOPPED = "stopped"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class HeartbeatConfig:
    """业务巡检心跳配置 (逆向自 NexusE ``check bot command status each 10s``)。

    可配置参数:
        - interval: 巡检间隔 (秒)
        - failure_threshold: 连续失败阈值
        - command_timeout: 单次命令超时 (秒)
        - jitter: 抖动幅度 (秒), 实际间隔 = interval +/- random(jitter)
    """

    #: 巡检间隔 (秒) - NexusE 默认为 10s
    interval: float = 10.0

    #: 连续失败阈值 - 超过则触发 on_disconnect
    failure_threshold: int = 3

    #: 单次命令超时 (秒)
    command_timeout: float = 5.0

    #: 抖动幅度 (秒) - 模拟人类操作间隔变化
    jitter: float = 1.0

    #: 是否启用抖动
    enable_jitter: bool = True


# ---------------------------------------------------------------------------
# 回调类型
# ---------------------------------------------------------------------------

#: 心跳命令发送回调: 返回 True 表示成功, False 表示失败
HeartbeatCommandCallback = Callable[[], bool]

#: 异步心跳命令发送回调
AsyncHeartbeatCommandCallback = Callable[[], "asyncio.Future[bool]"]

#: 断线回调: 在连续失败达到阈值时触发
DisconnectCallback = Callable[[], None]

#: 异步断线回调
AsyncDisconnectCallback = Callable[[], "asyncio.Future[None]"]


# ---------------------------------------------------------------------------
# BusinessHeartbeat
# ---------------------------------------------------------------------------

class BusinessHeartbeat:
    """业务巡检保活心跳 (逆向自 NexusE ``check bot command status each 10s``)。

    功能:
        - 每 10 秒发送 ``testfor @s`` 命令
        - 连续 3 次失败判定掉线
        - 支持异步回调 ``on_disconnect``
        - 与 ``anti_ban_enhanced.HeartbeatMonitor`` (8s 心跳) 配合
        - 可配置巡检间隔和失败阈值
        - 提供 start/stop/pause/resume 生命周期管理

    使用示例::

        heartbeat = BusinessHeartbeat(
            config=HeartbeatConfig(interval=10.0, failure_threshold=3),
            on_send_command=my_send_testfor,
            on_disconnect=my_handle_disconnect,
        )
        await heartbeat.start()
        # ... 运行中 ...
        await heartbeat.stop()

    线程安全: 使用 ``asyncio.Lock`` 保护状态变更, ``threading.Lock`` 保护统计。
    """

    def __init__(
        self,
        config: Optional[HeartbeatConfig] = None,
        on_send_command: Optional[HeartbeatCommandCallback] = None,
        on_disconnect: Optional[DisconnectCallback] = None,
        on_send_command_async: Optional[AsyncHeartbeatCommandCallback] = None,
        on_disconnect_async: Optional[AsyncDisconnectCallback] = None,
    ) -> None:
        """
        Args:
            config: 心跳配置, 默认使用 HeartbeatConfig()。
            on_send_command: 同步命令发送回调。
            on_disconnect: 同步断线回调。
            on_send_command_async: 异步命令发送回调 (优先于同步版本)。
            on_disconnect_async: 异步断线回调 (优先于同步版本)。
        """
        self._config: HeartbeatConfig = config or HeartbeatConfig()
        self._on_send_command: Optional[HeartbeatCommandCallback] = on_send_command
        self._on_disconnect: Optional[DisconnectCallback] = on_disconnect
        self._on_send_command_async: Optional[AsyncHeartbeatCommandCallback] = (
            on_send_command_async
        )
        self._on_disconnect_async: Optional[AsyncDisconnectCallback] = (
            on_disconnect_async
        )

        # 异步原语
        self._task: Optional[asyncio.Task[None]] = None
        self._async_lock: asyncio.Lock = asyncio.Lock()
        self._pause_event: asyncio.Event = asyncio.Event()
        self._pause_event.set()  # 初始状态: 未暂停

        # 统计
        self._lock: threading.Lock = threading.Lock()
        self._state: HeartbeatState = HeartbeatState.IDLE
        self._consecutive_failures: int = 0
        self._total_sent: int = 0
        self._total_acked: int = 0
        self._total_failed: int = 0
        self._last_sent_at: float = 0.0
        self._last_acked_at: float = 0.0
        self._started_at: float = 0.0
        self._disconnect_count: int = 0
        self._last_disconnect_at: float = 0.0

        logger.info(
            f"BusinessHeartbeat 初始化: interval={self._config.interval}s, "
            f"threshold={self._config.failure_threshold}, "
            f"timeout={self._config.command_timeout}s"
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def state(self) -> HeartbeatState:
        """当前心跳状态。"""
        with self._lock:
            return self._state

    @property
    def consecutive_failures(self) -> int:
        """连续失败次数。"""
        with self._lock:
            return self._consecutive_failures

    @property
    def is_alive(self) -> bool:
        """是否存活 (未超过失败阈值)。"""
        with self._lock:
            return self._state == HeartbeatState.RUNNING and (
                self._consecutive_failures < self._config.failure_threshold
            )

    @property
    def seconds_since_last_ack(self) -> float:
        """距离上次成功响应的秒数。"""
        with self._lock:
            if self._last_acked_at == 0.0:
                return 0.0
            return time.time() - self._last_acked_at

    # ------------------------------------------------------------------
    # 生命周期管理
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动业务巡检心跳。

        启动一个后台 ``asyncio.Task`` 执行心跳循环。
        如果已在运行, 则忽略此次调用。
        """
        async with self._async_lock:
            if self._state == HeartbeatState.RUNNING:
                logger.warning("BusinessHeartbeat 已在运行中, 忽略 start()")
                return

            self._state = HeartbeatState.RUNNING
            self._consecutive_failures = 0
            self._started_at = time.time()
            self._pause_event.set()

            self._task = asyncio.create_task(self._heartbeat_loop())
            logger.info(
                f"BusinessHeartbeat 已启动: interval={self._config.interval}s, "
                f"threshold={self._config.failure_threshold}"
            )

    async def stop(self) -> None:
        """停止业务巡检心跳。

        取消后台任务并等待其完成。
        """
        async with self._async_lock:
            if self._state == HeartbeatState.STOPPED:
                return

            self._state = HeartbeatState.STOPPED
            self._pause_event.set()  # 解除暂停以便任务退出

            if self._task is not None and not self._task.done():
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
                self._task = None

            logger.info(
                f"BusinessHeartbeat 已停止: total_sent={self._total_sent}, "
                f"total_acked={self._total_acked}, total_failed={self._total_failed}"
            )

    async def pause(self) -> None:
        """暂停业务巡检心跳 (通常在重连期间调用)。

        暂停后心跳循环进入等待状态, 直到 ``resume()`` 被调用。
        """
        async with self._async_lock:
            if self._state != HeartbeatState.RUNNING:
                logger.warning(f"BusinessHeartbeat 状态为 {self._state}, 无法暂停")
                return

            self._state = HeartbeatState.PAUSED
            self._pause_event.clear()
            logger.info("BusinessHeartbeat 已暂停")

    async def resume(self) -> None:
        """恢复业务巡检心跳。

        重置连续失败计数, 从干净状态恢复。
        """
        async with self._async_lock:
            if self._state != HeartbeatState.PAUSED:
                logger.warning(f"BusinessHeartbeat 状态为 {self._state}, 无法恢复")
                return

            self._state = HeartbeatState.RUNNING
            self._consecutive_failures = 0
            self._pause_event.set()
            logger.info("BusinessHeartbeat 已恢复, 失败计数已重置")

    async def trigger_now(self) -> bool:
        """立即触发一次心跳检测 (不等待定时器)。

        Returns:
            True 如果命令发送成功, False 如果失败。
        """
        return await self._send_heartbeat()

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        """心跳主循环 (运行在后台 Task 中)。

        循环逻辑:
        1. 等待暂停事件 (如果处于暂停状态)
        2. 计算抖动后的间隔
        3. 发送心跳命令
        4. 更新统计
        5. 检查失败阈值
        6. 等待下一次间隔
        """
        logger.debug("BusinessHeartbeat 循环已启动")

        while True:
            # 检查是否需要退出
            with self._lock:
                if self._state == HeartbeatState.STOPPED:
                    break

            # 等待暂停解除
            await self._pause_event.wait()

            # 再次检查状态
            with self._lock:
                if self._state == HeartbeatState.STOPPED:
                    break

            try:
                # 计算间隔
                sleep_time = self._compute_interval()

                # 发送心跳
                success = await self._send_heartbeat()

                if success:
                    self._on_heartbeat_success()
                else:
                    self._on_heartbeat_failure()

                # 检查是否触发断线
                await self._check_disconnect()

                # 等待下一次
                await asyncio.sleep(sleep_time)

            except asyncio.CancelledError:
                logger.debug("BusinessHeartbeat 循环被取消")
                break
            except Exception as exc:
                logger.error(f"BusinessHeartbeat 循环异常: {exc}", exc_info=True)
                # 短暂等待后继续
                await asyncio.sleep(1.0)

        logger.debug("BusinessHeartbeat 循环已退出")

    def _compute_interval(self) -> float:
        """计算下一次心跳间隔 (含抖动)。

        Returns:
            间隔秒数。
        """
        base = self._config.interval
        if self._config.enable_jitter:
            import random as _random
            jitter = _random.uniform(-self._config.jitter, self._config.jitter)
            return max(1.0, base + jitter)
        return base

    async def _send_heartbeat(self) -> bool:
        """发送一次心跳命令并等待响应。

        Returns:
            True 如果命令成功, False 如果失败/超时。
        """
        with self._lock:
            self._total_sent += 1
            self._last_sent_at = time.time()

        try:
            # 优先使用异步回调
            if self._on_send_command_async is not None:
                future = self._on_send_command_async()
                result = await asyncio.wait_for(
                    future,
                    timeout=self._config.command_timeout,
                )
                return bool(result)

            # 同步回调
            if self._on_send_command is not None:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None,
                    self._on_send_command,
                )
                return bool(result)

            # 没有回调: 无法发送心跳
            logger.warning("BusinessHeartbeat: 未设置命令发送回调, 跳过心跳")
            return False

        except asyncio.TimeoutError:
            logger.warning(
                f"BusinessHeartbeat 命令超时: "
                f"timeout={self._config.command_timeout}s"
            )
            return False
        except Exception as exc:
            logger.error(f"BusinessHeartbeat 命令发送异常: {exc}")
            return False

    def _on_heartbeat_success(self) -> None:
        """心跳成功时的处理。"""
        with self._lock:
            self._consecutive_failures = 0
            self._total_acked += 1
            self._last_acked_at = time.time()
        logger.debug(
            f"BusinessHeartbeat 成功: "
            f"consecutive_failures=0, total_acked={self._total_acked}"
        )

    def _on_heartbeat_failure(self) -> None:
        """心跳失败时的处理。"""
        with self._lock:
            self._consecutive_failures += 1
            self._total_failed += 1
            remaining = self._config.failure_threshold - self._consecutive_failures
        logger.warning(
            f"BusinessHeartbeat 失败: "
            f"consecutive_failures={self._consecutive_failures}/"
            f"{self._config.failure_threshold}, "
            f"remaining={remaining}"
        )

    async def _check_disconnect(self) -> None:
        """检查是否触发断线回调。"""
        with self._lock:
            should_disconnect = (
                self._consecutive_failures >= self._config.failure_threshold
                and self._state == HeartbeatState.RUNNING
            )
            if should_disconnect:
                self._disconnect_count += 1
                self._last_disconnect_at = time.time()

        if should_disconnect:
            logger.error(
                f"BusinessHeartbeat 检测到断线: "
                f"consecutive_failures={self._consecutive_failures}, "
                f"disconnect_count={self._disconnect_count}"
            )
            await self._invoke_on_disconnect()

    async def _invoke_on_disconnect(self) -> None:
        """调用断线回调 (优先异步版本)。"""
        try:
            if self._on_disconnect_async is not None:
                await self._on_disconnect_async()
            elif self._on_disconnect is not None:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._on_disconnect)
        except Exception as exc:
            logger.error(f"BusinessHeartbeat 断线回调异常: {exc}", exc_info=True)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """返回心跳状态统计。

        Returns:
            包含所有统计信息的字典。
        """
        with self._lock:
            return {
                "state": self._state.value,
                "interval": self._config.interval,
                "failure_threshold": self._config.failure_threshold,
                "consecutive_failures": self._consecutive_failures,
                "total_sent": self._total_sent,
                "total_acked": self._total_acked,
                "total_failed": self._total_failed,
                "success_rate": (
                    round(self._total_acked / max(self._total_sent, 1) * 100, 2)
                ),
                "last_sent_at": self._last_sent_at,
                "last_acked_at": self._last_acked_at,
                "seconds_since_last_ack": self.seconds_since_last_ack,
                "started_at": self._started_at,
                "uptime_seconds": (
                    time.time() - self._started_at
                    if self._started_at > 0
                    else 0.0
                ),
                "disconnect_count": self._disconnect_count,
                "last_disconnect_at": self._last_disconnect_at,
                "is_alive": self.is_alive,
            }

    def reset_stats(self) -> None:
        """重置统计计数器 (不影响运行状态)。"""
        with self._lock:
            self._consecutive_failures = 0
            self._total_sent = 0
            self._total_acked = 0
            self._total_failed = 0
            self._last_sent_at = 0.0
            self._last_acked_at = 0.0
            self._disconnect_count = 0
            self._last_disconnect_at = 0.0
        logger.info("BusinessHeartbeat 统计已重置")


__all__ = [
    "HeartbeatState",
    "HeartbeatConfig",
    "BusinessHeartbeat",
    "HeartbeatCommandCallback",
    "AsyncHeartbeatCommandCallback",
    "DisconnectCallback",
    "AsyncDisconnectCallback",
]