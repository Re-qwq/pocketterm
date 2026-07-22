"""PocketTerm 反封禁增强模块 (ToolDelta / NovaBuilder / NexusE 逆向集成)

本模块在 ``anti_ban.py`` 的基础上, 集成从三大反作弊规避系统逆向出的
高级策略, 弥补 PocketTerm 反封禁逻辑的缺陷。

逆向来源与对应策略
------------------

1. **ToolDelta** (Python 源码):
   - 8s 应用层心跳 (``testfor @s``), 3 次失败阈值 (24s)
   - 指数退避重启序列: ``[5, 10, 20, 40, 80, 160, 300]`` 秒
   - 命令 UUID 追踪 (CommandUUID), 用于响应配对
   - ``safe_writer`` 原子写入, 避免状态文件损坏
   - 进程隔离 + 线程异常捕获 + 自动重启

2. **NovaBuilder** (Go 二进制逆向):
   - ``rate.Limiter`` 令牌桶 (Burst/BurstDensity/BurstDuration)
   - ``humanizeCommandMessage`` 命令消息人类化
   - 设备指纹持久化 (``uqholder``)

3. **NexusE** (Go 二进制逆向):
   - ``PostponeActionsAfterChallengePassed`` 挑战期间动作排队
   - ``OperatorChallenge`` 监听 SetCommandEnabled + 权限变更
   - ``MCPCheckChallengesSolver`` via PyRPC
   - 4 层重连策略 (network/task/command/OP)
   - ``FlowersOP`` 隐式 OP (不依赖 ``op`` 权限, 通过 ``wocmd`` 执行)
   - Orion System 阈值规避 (commands <5/sec, attacks <5/sec, movement <0.4 blocks/tick)

模块组织
--------

- :class:`ToolDeltaBackoff`       -- 指数退避序列
- :class:`HeartbeatMonitor`       -- 8s 应用层心跳 + 3 失败阈值
- :class:`HumanizeCommand`        -- 命令消息人类化
- :class:`OrionThresholdMonitor`  -- Orion System 阈值监控
- :class:`CommandUUIDTracker`     -- 命令 UUID 追踪
- :class:`PostponeActionQueue`    -- 挑战期间动作排队
- :class:`OperatorChallengeMonitor` -- OP 权限变更监控
- :func:`safe_writer`             -- 原子写入工具
- :class:`EnhancedAntiBan`        -- 上述组件的聚合总控

设计原则
--------

- 所有公共方法线程安全 (``threading.Lock`` / ``asyncio.Lock``)
- 与 ``anti_ban.AntiBanController`` 解耦, 通过组合模式集成
- 关键策略可配置 (通过 :class:`EnhancedAntiBanConfig`)
- 异步友好的 API (sync + async 双版本)
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import string
import threading
import time
import uuid as _uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Tuple

from ..logger import get_logger

logger = get_logger("auth.anti_ban_enhanced")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class EnhancedAntiBanConfig:
    """反封禁增强配置 (基于 ToolDelta / NexusE 逆向参数)。"""

    # -- ToolDelta 心跳 --
    #: ToolDelta 风格心跳基础间隔 (秒) - ``testfor @s``
    heartbeat_interval: float = 8.0
    #: 心跳抖动上下限 (秒) - 实际间隔 = base +/- jitter
    heartbeat_jitter: float = 2.0
    #: 心跳失败阈值 (连续失败次数) - 超过则触发重连
    heartbeat_failure_threshold: int = 3
    #: 心跳超时 (单次等待响应, 秒)
    heartbeat_timeout: float = 5.0

    # -- ToolDelta 指数退避 --
    #: ToolDelta 指数退避序列 (秒): [5, 10, 20, 40, 80, 160, 300]
    backoff_sequence: Tuple[int, ...] = (5, 10, 20, 40, 80, 160, 300)
    #: 退避抖动比例 (实际 = base * (1 +/- ratio))
    backoff_jitter_ratio: float = 0.3
    #: 最大重试次数 (超过则放弃)
    max_retry_attempts: int = 7

    # -- Orion System 阈值 --
    #: Orion 命令速率阈值 (命令/秒) - 超过则触发降速
    orion_command_threshold: int = 5
    #: Orion 攻击速率阈值 (攻击/秒)
    orion_attack_threshold: int = 5
    #: Orion 移动速率阈值 (方块/tick) - 超过则视为飞行/瞬移
    orion_movement_threshold: float = 0.4
    #: Orion 检测窗口 (秒)
    orion_window_seconds: float = 1.0

    # -- humanizeCommandMessage --
    #: 是否启用命令人类化
    enable_humanize: bool = True
    #: 人类化概率 (每次命令以该概率应用人类化)
    humanize_probability: float = 0.7
    #: 人类化策略: 添加随机空格的概率
    humanize_space_probability: float = 0.3
    #: 人类化策略: 大小写扰动的概率
    humanize_case_probability: float = 0.2
    #: 人类化策略: 添加前缀注释的概率 (如 ``# `` 前缀)
    humanize_prefix_probability: float = 0.1

    # -- PostponeActions --
    #: 是否启用挑战期间动作排队
    enable_postpone: bool = True
    #: 排队最大长度 (超过则丢弃最旧)
    postpone_max_queue: int = 100
    #: 挑战完成后批量执行间隔 (秒)
    postpone_batch_interval: float = 0.2

    # -- OperatorChallenge --
    #: 是否启用 OP 权限监控
    enable_operator_monitor: bool = True
    #: OP 权限变更检测窗口 (秒)
    operator_change_window: float = 30.0
    #: OP 权限变更阈值 (超过则视为异常)
    operator_change_threshold: int = 3

    # -- CommandUUIDTracker --
    #: 命令 UUID 追踪窗口 (秒) - 超过则清理
    command_uuid_window: float = 60.0
    #: 命令 UUID 最大缓存数
    command_uuid_max_cache: int = 500

    # -- safe_writer --
    #: safe_writer 临时文件后缀
    safe_writer_suffix: str = ".tmp"
    #: safe_writer 重试次数
    safe_writer_retries: int = 3
    #: safe_writer 重试间隔 (秒)
    safe_writer_retry_delay: float = 0.1


# ---------------------------------------------------------------------------
# ToolDelta 指数退避
# ---------------------------------------------------------------------------
class ToolDeltaBackoff:
    """ToolDelta 风格指数退避序列。

    ToolDelta 源码中的重连序列为 ``[5, 10, 20, 40, 80, 160, 300]`` 秒,
    每次重连按顺序取下一个值, 超过序列长度则取最后一个值 (300s)。

    与传统 ``base * 2^n`` 指数退避相比, ToolDelta 序列:
        - 前 3 次快速重试 (5/10/20s) - 应对瞬时网络抖动
        - 中间 3 次中等退避 (40/80/160s) - 应对服务器临时限流
        - 最后 1 次长退避 (300s) - 应对账号冷却期
    """

    def __init__(
        self,
        config: EnhancedAntiBanConfig,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._config = config
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._attempt: int = 0
        self._last_delay: float = 0.0
        self._reset_at: float = time.time()

    @property
    def attempt(self) -> int:
        """当前重试次数 (0 = 未重试)。"""
        with self._lock:
            return self._attempt

    @property
    def last_delay(self) -> float:
        """上次计算的延迟 (秒)。"""
        with self._lock:
            return self._last_delay

    def reset(self) -> None:
        """重置退避计数 (成功后调用)。"""
        with self._lock:
            self._attempt = 0
            self._last_delay = 0.0
            self._reset_at = time.time()
            logger.debug("ToolDelta 退避计数已重置")

    def next_delay(self) -> float:
        """计算下一次重连延迟 (秒)。

        Returns:
            延迟秒数 (含抖动)。
        """
        with self._lock:
            sequence = self._config.backoff_sequence
            idx = min(self._attempt, len(sequence) - 1)
            base_delay = float(sequence[idx])
            # 抖动
            jitter = self._rng.uniform(
                -self._config.backoff_jitter_ratio,
                self._config.backoff_jitter_ratio,
            )
            delay = max(1.0, base_delay * (1.0 + jitter))
            self._attempt += 1
            self._last_delay = delay
            logger.info(
                f"ToolDelta 退避: attempt={self._attempt}/{self._config.max_retry_attempts} "
                f"base={base_delay}s delay={delay:.1f}s"
            )
            return delay

    def should_retry(self) -> bool:
        """是否应该继续重试。"""
        with self._lock:
            return self._attempt < self._config.max_retry_attempts

    def stats(self) -> Dict[str, Any]:
        """返回状态。"""
        with self._lock:
            return {
                "attempt": self._attempt,
                "last_delay": round(self._last_delay, 2),
                "max_attempts": self._config.max_retry_attempts,
                "should_retry": self._attempt < self._config.max_retry_attempts,
                "seconds_since_reset": time.time() - self._reset_at,
            }


# ---------------------------------------------------------------------------
# 8s 心跳监控 (ToolDelta 风格)
# ---------------------------------------------------------------------------
class HeartbeatMonitor:
    """ToolDelta 风格 8s 应用层心跳监控。

    ToolDelta 通过 ``testfor @s`` 命令实现应用层心跳:
        - 每 8 秒发送一次 ``testfor @s``
        - 服务器应在 5 秒内返回命令响应
        - 连续 3 次失败 (24s 无响应) 视为断线, 触发重连

    本类提供心跳状态跟踪, 实际发送由调用方实现
    (通过 ``on_heartbeat_callback`` 注入)。
    """

    def __init__(
        self,
        config: EnhancedAntiBanConfig,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._config = config
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._last_heartbeat_sent: float = 0.0
        self._last_heartbeat_ack: float = time.time()
        self._consecutive_failures: int = 0
        self._total_sent: int = 0
        self._total_acked: int = 0
        self._is_alive: bool = True

    @property
    def is_alive(self) -> bool:
        """是否存活 (未超过失败阈值)。"""
        with self._lock:
            return self._is_alive

    @property
    def consecutive_failures(self) -> int:
        """连续失败次数。"""
        with self._lock:
            return self._consecutive_failures

    def next_interval(self) -> float:
        """计算下次心跳间隔 (含抖动)。

        Returns:
            间隔秒数。
        """
        with self._lock:
            jitter = self._rng.uniform(
                -self._config.heartbeat_jitter,
                self._config.heartbeat_jitter,
            )
            return max(1.0, self._config.heartbeat_interval + jitter)

    def record_sent(self) -> None:
        """记录心跳发送。"""
        with self._lock:
            self._last_heartbeat_sent = time.time()
            self._total_sent += 1

    def record_ack(self) -> None:
        """记录心跳响应 (命令响应到达时调用)。"""
        with self._lock:
            now = time.time()
            self._last_heartbeat_ack = now
            self._total_acked += 1
            self._consecutive_failures = 0
            self._is_alive = True

    def record_failure(self) -> bool:
        """记录一次心跳失败 (超时未收到响应)。

        Returns:
            ``True`` 已超过失败阈值, 应触发重连; ``False`` 仍可继续。
        """
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._config.heartbeat_failure_threshold:
                self._is_alive = False
                logger.warning(
                    f"心跳连续失败 {self._consecutive_failures} 次 "
                    f"(阈值 {self._config.heartbeat_failure_threshold}), "
                    f"判定为断线"
                )
                return True
            logger.debug(
                f"心跳失败 {self._consecutive_failures}/"
                f"{self._config.heartbeat_failure_threshold}"
            )
            return False

    def check_timeout(self) -> bool:
        """检查是否超时 (距上次发送已超过 ``heartbeat_timeout`` 秒未收到响应)。

        Returns:
            ``True`` 超时 (应记录失败); ``False`` 未超时。
        """
        with self._lock:
            if self._last_heartbeat_sent == 0.0:
                return False
            elapsed = time.time() - self._last_heartbeat_sent
            return elapsed > self._config.heartbeat_timeout

    def reset(self) -> None:
        """重置心跳状态 (重连成功后调用)。"""
        with self._lock:
            now = time.time()
            self._last_heartbeat_sent = 0.0
            self._last_heartbeat_ack = now
            self._consecutive_failures = 0
            self._is_alive = True
            logger.info("心跳监控状态已重置")

    def stats(self) -> Dict[str, Any]:
        """返回心跳统计。"""
        with self._lock:
            now = time.time()
            return {
                "is_alive": self._is_alive,
                "consecutive_failures": self._consecutive_failures,
                "failure_threshold": self._config.heartbeat_failure_threshold,
                "total_sent": self._total_sent,
                "total_acked": self._total_acked,
                "seconds_since_last_sent": (
                    now - self._last_heartbeat_sent
                    if self._last_heartbeat_sent > 0
                    else -1.0
                ),
                "seconds_since_last_ack": now - self._last_heartbeat_ack,
                "heartbeat_interval": self._config.heartbeat_interval,
            }


# ---------------------------------------------------------------------------
# humanizeCommandMessage (NovaBuilder 风格)
# ---------------------------------------------------------------------------
class HumanizeCommand:
    """命令消息人类化 (逆向自 NovaBuilder ``humanizeCommandMessage``)。

    NovaBuilder 在发送命令前会对命令文本进行人类化处理, 使机器人命令
    在服务器日志中看起来更像真实玩家输入:

    - 随机添加空格 (如 ``/help`` -> ``/ help``)
    - 大小写扰动 (如 ``/help`` -> ``/Help``)
    - 添加前缀注释 (如 ``/help`` -> ``# /help``)
    - 随机省略 ``/`` 前缀 (聊天框直接输入)

    注意: 过度人类化可能导致命令解析失败, 因此默认概率较低。
    """

    def __init__(
        self,
        config: EnhancedAntiBanConfig,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._config = config
        self._rng = rng or random.Random()
        self._lock = threading.Lock()

    def humanize(self, command: str) -> str:
        """对命令文本进行人类化处理。

        Args:
            command: 原始命令 (如 ``"/help"`` 或 ``"help"``)。

        Returns:
            人类化后的命令文本。
        """
        if not self._config.enable_humanize:
            return command
        if not command:
            return command

        with self._lock:
            # 概率门控
            if self._rng.random() > self._config.humanize_probability:
                return command

            result = command
            applied: List[str] = []

            # 1. 大小写扰动 (仅首字母)
            if self._rng.random() < self._config.humanize_case_probability:
                result = self._apply_case_perturbation(result)
                applied.append("case")

            # 2. 添加随机空格 (在 / 之后)
            if self._rng.random() < self._config.humanize_space_probability:
                result = self._apply_space_insertion(result)
                applied.append("space")

            # 3. 添加前缀注释
            if self._rng.random() < self._config.humanize_prefix_probability:
                result = self._apply_prefix(result)
                applied.append("prefix")

            if applied:
                logger.debug(
                    f"humanizeCommandMessage: applied={applied} "
                    f"'{command}' -> '{result}'"
                )
            return result

    def _apply_case_perturbation(self, command: str) -> str:
        """大小写扰动 (首字母大小写切换)。"""
        if not command:
            return command
        # 找到第一个字母字符
        for i, ch in enumerate(command):
            if ch.isalpha():
                if ch.islower():
                    return command[:i] + ch.upper() + command[i + 1:]
                else:
                    return command[:i] + ch.lower() + command[i + 1:]
        return command

    def _apply_space_insertion(self, command: str) -> str:
        """在 ``/`` 之后添加随机空格。"""
        if command.startswith("/"):
            # 50% 概率添加 1 个空格, 50% 概率添加 2 个空格
            spaces = " " * self._rng.randint(1, 2)
            return "/" + spaces + command[1:]
        return command

    def _apply_prefix(self, command: str) -> str:
        """添加前缀注释 (模拟玩家在聊天框输入注释)。

        BUG-7.6 修复: 之前对所有命令都添加 "# "/"// "/". " 前缀,
        但以 "/" 开头的游戏命令 (如 "/testfor @s") 添加前缀后会变成
        "# /testfor @s", 服务器将其视为注释而非命令, 导致命令不执行。
        现在仅对非 "/" 开头的聊天消息添加前缀。
        """
        if command.startswith("/"):
            return command
        prefixes = ["# ", "// ", ". "]
        prefix = self._rng.choice(prefixes)
        return prefix + command


# ---------------------------------------------------------------------------
# Orion System 阈值监控 (NexusE 风格)
# ---------------------------------------------------------------------------
class OrionThresholdMonitor:
    """Orion System 阈值监控 (逆向自 NexusE 反作弊阈值)。

    NexusE 的 Orion System 在服务器端检测:
        - 命令速率 > 5/秒
        - 攻击速率 > 5/秒
        - 移动速率 > 0.4 方块/tick (约 8 方块/秒)

    本类在客户端侧提前监控这些阈值, 超过时主动降速, 避免触发服务器检测。
    """

    def __init__(self, config: EnhancedAntiBanConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._command_timestamps: Deque[float] = deque()
        self._attack_timestamps: Deque[float] = deque()
        self._movement_samples: Deque[Tuple[float, float]] = deque()  # (timestamp, distance)
        self._throttle_until: float = 0.0
        self._throttle_count: int = 0

    @property
    def is_throttled(self) -> bool:
        """是否处于限速状态。"""
        with self._lock:
            return time.time() < self._throttle_until

    @property
    def throttle_count(self) -> int:
        """累计限速次数。"""
        with self._lock:
            return self._throttle_count

    def record_command(self) -> bool:
        """记录一次命令发送。

        Returns:
            ``True`` 超过阈值 (应降速); ``False`` 正常。
        """
        with self._lock:
            now = time.time()
            self._cleanup_locked(self._command_timestamps, now)
            self._command_timestamps.append(now)
            count = len(self._command_timestamps)
            if count > self._config.orion_command_threshold:
                self._trigger_throttle_locked(
                    f"命令速率超阈值: {count}/{self._config.orion_window_seconds}s "
                    f"> {self._config.orion_command_threshold}"
                )
                return True
            return False

    def record_attack(self) -> bool:
        """记录一次攻击。

        Returns:
            ``True`` 超过阈值; ``False`` 正常。
        """
        with self._lock:
            now = time.time()
            self._cleanup_locked(self._attack_timestamps, now)
            self._attack_timestamps.append(now)
            count = len(self._attack_timestamps)
            if count > self._config.orion_attack_threshold:
                self._trigger_throttle_locked(
                    f"攻击速率超阈值: {count}/{self._config.orion_window_seconds}s "
                    f"> {self._config.orion_attack_threshold}"
                )
                return True
            return False

    def record_movement(self, distance_blocks: float) -> bool:
        """记录一次移动 (距离, 单位: 方块)。

        Args:
            distance_blocks: 本次移动的距离 (方块)。

        Returns:
            ``True`` 超过阈值 (飞行/瞬移); ``False`` 正常。
        """
        with self._lock:
            now = time.time()
            # 清理 1 秒前的样本
            while (
                self._movement_samples
                and now - self._movement_samples[0][0] > 1.0
            ):
                self._movement_samples.popleft()
            self._movement_samples.append((now, distance_blocks))
            # 计算 1 秒内总移动距离
            total_distance = sum(d for _, d in self._movement_samples)
            # 1 秒 = 20 tick, 阈值 0.4 方块/tick = 8 方块/秒
            max_distance_per_sec = self._config.orion_movement_threshold * 20
            if total_distance > max_distance_per_sec:
                self._trigger_throttle_locked(
                    f"移动速率超阈值: {total_distance:.2f} 方块/秒 "
                    f"> {max_distance_per_sec:.2f}"
                )
                return True
            return False

    def wait_if_throttled(self, sleeper: Optional[Callable[[float], None]] = None) -> float:
        """如果处于限速状态, 等待直到解除。

        Returns:
            实际等待的秒数。
        """
        with self._lock:
            now = time.time()
            if now >= self._throttle_until:
                return 0.0
            wait = self._throttle_until - now
        if sleeper:
            sleeper(wait)
        else:
            time.sleep(wait)
        return wait

    def reset(self) -> None:
        """重置监控状态。"""
        with self._lock:
            self._command_timestamps.clear()
            self._attack_timestamps.clear()
            self._movement_samples.clear()
            self._throttle_until = 0.0
            self._throttle_count = 0

    def stats(self) -> Dict[str, Any]:
        """返回监控统计。"""
        with self._lock:
            now = time.time()
            return {
                "is_throttled": now < self._throttle_until,
                "throttle_count": self._throttle_count,
                "command_count_in_window": len(self._command_timestamps),
                "attack_count_in_window": len(self._attack_timestamps),
                "movement_distance_in_window": sum(
                    d for _, d in self._movement_samples
                ),
                "command_threshold": self._config.orion_command_threshold,
                "attack_threshold": self._config.orion_attack_threshold,
                "movement_threshold": self._config.orion_movement_threshold,
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _cleanup_locked(self, queue: Deque[float], now: float) -> None:
        """清理窗口外的时间戳 (必须在锁内调用)。"""
        cutoff = now - self._config.orion_window_seconds
        while queue and queue[0] < cutoff:
            queue.popleft()

    def _trigger_throttle_locked(self, reason: str) -> None:
        """触发限速 (必须在锁内调用)。

        限速时长 = 1 秒 + 累计次数 * 0.5 秒 (渐进式)
        """
        now = time.time()
        throttle_duration = 1.0 + self._throttle_count * 0.5
        self._throttle_until = now + throttle_duration
        self._throttle_count += 1
        logger.warning(
            f"Orion 阈值触发限速: {reason}, "
            f"持续 {throttle_duration:.1f}s (第 {self._throttle_count} 次)"
        )


# ---------------------------------------------------------------------------
# 命令 UUID 追踪 (ToolDelta 风格)
# ---------------------------------------------------------------------------
class CommandUUIDTracker:
    """命令 UUID 追踪 (逆向自 ToolDelta ``CommandUUID``)。

    ToolDelta 为每个发送的命令生成唯一 UUID, 并追踪其响应:
        - 命令发送时生成 UUID, 记录到 ``pending`` 队列
        - 收到命令响应时, 通过 UUID 配对, 标记为已完成
        - 超时未响应的命令被视为失败

    本类提供 UUID 生成 / 追踪 / 超时检测功能。
    """

    def __init__(self, config: EnhancedAntiBanConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        # pending: {uuid: {"command": str, "sent_at": float, "timeout": float}}
        self._pending: Dict[str, Dict[str, Any]] = {}
        # completed: deque of {"uuid": str, "command": str, "sent_at": float, "completed_at": float, "success": bool}
        self._completed: Deque[Dict[str, Any]] = deque()
        self._total_sent: int = 0
        self._total_completed: int = 0
        self._total_timeout: int = 0

    def generate_uuid(self) -> str:
        """生成命令 UUID。"""
        return str(_uuid.uuid4())

    def track(
        self,
        command: str,
        timeout: float = 30.0,
        command_uuid: Optional[str] = None,
    ) -> str:
        """追踪一个命令。

        Args:
            command: 命令文本。
            timeout: 超时秒数。
            command_uuid: 可选, 指定 UUID; 为 ``None`` 时自动生成。

        Returns:
            命令 UUID。
        """
        if command_uuid is None:
            command_uuid = self.generate_uuid()
        with self._lock:
            self._cleanup_locked()
            self._pending[command_uuid] = {
                "command": command,
                "sent_at": time.time(),
                "timeout": timeout,
            }
            self._total_sent += 1
            # 限制 pending 大小
            if len(self._pending) > self._config.command_uuid_max_cache:
                # 移除最旧的
                oldest = min(self._pending.items(), key=lambda x: x[1]["sent_at"])
                del self._pending[oldest[0]]
                logger.debug(f"命令 UUID 缓存已满, 移除最旧: {oldest[0]}")
        return command_uuid

    def complete(self, command_uuid: str, success: bool = True) -> bool:
        """标记命令为已完成。

        Args:
            command_uuid: 命令 UUID。
            success: 是否成功。

        Returns:
            ``True`` 找到并完成; ``False`` 未找到 (可能已超时)。
        """
        with self._lock:
            entry = self._pending.pop(command_uuid, None)
            if entry is None:
                return False
            entry["uuid"] = command_uuid
            entry["completed_at"] = time.time()
            entry["success"] = success
            entry["latency"] = entry["completed_at"] - entry["sent_at"]
            self._completed.append(entry)
            # 限制 completed 大小
            while len(self._completed) > self._config.command_uuid_max_cache:
                self._completed.popleft()
            self._total_completed += 1
            return True

    def check_timeouts(self) -> List[str]:
        """检查超时的命令。

        Returns:
            超时命令的 UUID 列表。
        """
        with self._lock:
            now = time.time()
            self._cleanup_locked()
            timed_out: List[str] = []
            for uuid, entry in list(self._pending.items()):
                if now - entry["sent_at"] > entry["timeout"]:
                    del self._pending[uuid]
                    self._total_timeout += 1
                    timed_out.append(uuid)
                    logger.warning(
                        f"命令超时: uuid={uuid} command={entry['command'][:50]} "
                        f"timeout={entry['timeout']}s"
                    )
            return timed_out

    def get_pending(self) -> List[Dict[str, Any]]:
        """返回所有待完成命令。"""
        with self._lock:
            return [
                {"uuid": uuid, **entry} for uuid, entry in self._pending.items()
            ]

    def reset(self) -> None:
        """重置追踪器。"""
        with self._lock:
            self._pending.clear()
            self._completed.clear()
            self._total_sent = 0
            self._total_completed = 0
            self._total_timeout = 0

    def stats(self) -> Dict[str, Any]:
        """返回追踪统计。"""
        with self._lock:
            avg_latency = 0.0
            if self._completed:
                avg_latency = sum(
                    e.get("latency", 0.0) for e in self._completed
                ) / len(self._completed)
            return {
                "total_sent": self._total_sent,
                "total_completed": self._total_completed,
                "total_timeout": self._total_timeout,
                "pending_count": len(self._pending),
                "avg_latency_ms": round(avg_latency * 1000, 2),
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _cleanup_locked(self) -> None:
        """清理过期的已完成记录 (必须在锁内调用)。"""
        now = time.time()
        cutoff = now - self._config.command_uuid_window
        while self._completed and self._completed[0]["completed_at"] < cutoff:
            self._completed.popleft()


# ---------------------------------------------------------------------------
# 挑战期间动作排队 (PostponeActionsAfterChallengePassed)
# ---------------------------------------------------------------------------
class PostponeActionQueue:
    """挑战期间动作排队 (逆向自 NexusE ``PostponeActionsAfterChallengePassed``)。

    NexusE 在检测到 MCPC 挑战时, 会将所有后续动作排队, 等待挑战通过后
    批量执行。这避免了在挑战期间发送命令导致挑战失败。

    工作流程:
        1. ``pause()`` - 检测到挑战, 暂停动作执行
        2. ``enqueue(action)`` - 将动作加入队列
        3. ``resume()`` - 挑战通过, 批量执行队列中的动作
    """

    def __init__(self, config: EnhancedAntiBanConfig) -> None:
        self._config = config
        # H-1 修复: 不在 __init__ 里创建 asyncio.Lock (跨事件循环崩溃)
        # 改为懒加载, 绑定到首次使用的 event loop
        self._lock: Optional[asyncio.Lock] = None
        # BUG-7.2 修复: 使用 threading.Lock 保护 _lock 的懒加载初始化,
        # 防止多线程并发调用 _get_lock 时创建多个 asyncio.Lock 实例。
        self._init_lock = threading.Lock()
        self._queue: Deque[Callable[[], Awaitable[Any]]] = deque()
        self._is_paused: bool = False
        self._pause_since: float = 0.0
        self._total_postponed: int = 0
        self._total_executed: int = 0
        self._total_dropped: int = 0

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-1 修复: 懒加载, 绑定到当前事件循环)。

        BUG-7.2 修复: 使用 double-checked locking 模式, 用 threading.Lock
        保护 asyncio.Lock 的创建, 避免多线程并发时创建多个锁实例。
        """
        if self._lock is None:
            with self._init_lock:
                if self._lock is None:
                    self._lock = asyncio.Lock()
        return self._lock

    @property
    def is_paused(self) -> bool:
        """是否暂停中 (挑战期间)。"""
        return self._is_paused

    @property
    def queue_size(self) -> int:
        """队列长度。"""
        return len(self._queue)

    async def pause(self) -> None:
        """暂停动作执行 (挑战开始时调用)。"""
        async with self._get_lock():
            self._is_paused = True
            self._pause_since = time.time()
            logger.info("PostponeActions: 已暂停动作执行 (挑战期间)")

    async def resume(self) -> None:
        """恢复动作执行 (挑战通过后调用)。

        按 FIFO 顺序批量执行队列中的动作, 每个动作之间间隔
        ``postpone_batch_interval`` 秒。
        """
        async with self._get_lock():
            self._is_paused = False
            queue = list(self._queue)
            self._queue.clear()
            pause_duration = time.time() - self._pause_since
            logger.info(
                f"PostponeActions: 恢复执行, 队列长度={len(queue)}, "
                f"暂停时长={pause_duration:.1f}s"
            )

        # 批量执行 (不持有锁)
        # BUG-7.8 修复: 之前 _total_executed 在锁外递增, 多协程并发时计数
        # 可能不准。现使用局部计数器, 执行完成后在锁内统一更新。
        executed_count = 0
        for action in queue:
            try:
                await action()
                executed_count += 1
            except Exception as exc:  # noqa: BLE001
                logger.error(f"PostponeActions: 执行排队动作失败: {exc}")
            # 批量执行间隔
            if self._config.postpone_batch_interval > 0:
                await asyncio.sleep(self._config.postpone_batch_interval)
        async with self._get_lock():
            self._total_executed += executed_count

    async def enqueue_or_execute(
        self,
        action: Callable[[], Awaitable[Any]],
    ) -> bool:
        """入队或直接执行。

        - 暂停中 -> 入队, 返回 ``True`` (已排队)
        - 非暂停 -> 直接执行, 返回 ``False`` (已执行)

        Args:
            action: 异步动作回调。

        Returns:
            ``True`` 已入队; ``False`` 已直接执行。
        """
        async with self._get_lock():
            if not self._is_paused or not self._config.enable_postpone:
                # 非暂停, 直接执行 (释放锁后)
                pass
            else:
                # 暂停中, 入队
                if len(self._queue) >= self._config.postpone_max_queue:
                    # 队列已满, 丢弃最旧
                    self._queue.popleft()
                    self._total_dropped += 1
                    logger.warning(
                        f"PostponeActions: 队列已满, 丢弃最旧动作 "
                        f"(max={self._config.postpone_max_queue})"
                    )
                self._queue.append(action)
                self._total_postponed += 1
                return True

        # 直接执行 (不持有锁)
        # BUG-7.4 修复: 释放锁后到执行前, pause() 可能被调用。重新检查
        # _is_paused, 若已变为暂停状态则重新入队, 避免在暂停期间执行动作。
        async with self._get_lock():
            if self._is_paused and self._config.enable_postpone:
                self._queue.append(action)
                self._total_postponed += 1
                return True
        try:
            await action()
            # BUG-7.8 修复: 在锁内更新计数器, 避免并发计数不准
            async with self._get_lock():
                self._total_executed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(f"PostponeActions: 直接执行失败: {exc}")
        return False

    async def clear(self) -> int:
        """清空队列。

        Returns:
            清空的动作数。
        """
        async with self._get_lock():
            count = len(self._queue)
            self._queue.clear()
            self._total_dropped += count
            return count

    def stats(self) -> Dict[str, Any]:
        """返回统计。"""
        return {
            "is_paused": self._is_paused,
            "queue_size": len(self._queue),
            "total_postponed": self._total_postponed,
            "total_executed": self._total_executed,
            "total_dropped": self._total_dropped,
            "pause_duration": (
                time.time() - self._pause_since if self._is_paused else 0.0
            ),
        }


# ---------------------------------------------------------------------------
# OperatorChallenge 监控 (NexusE 风格)
# ---------------------------------------------------------------------------
class OperatorChallengeMonitor:
    """OP 权限变更监控 (逆向自 NexusE ``OperatorChallenge``)。

    NexusE 监听以下事件来检测 OP 权限挑战:
        1. ``SetCommandEnabled`` 包 - 服务器启用/禁用某些命令
        2. 玩家权限变更 (OP 等级变化)
        3. 命令方块可用性变更

    当检测到 OP 权限变更时, NexusE 会:
        1. 暂停所有依赖 OP 权限的动作
        2. 等待权限稳定 (无变更超过 ``operator_change_window`` 秒)
        3. 恢复执行
    """

    def __init__(self, config: EnhancedAntiBanConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._change_events: Deque[Dict[str, Any]] = deque()
        self._is_op_enabled: bool = False
        self._last_change_at: float = 0.0
        self._total_changes: int = 0
        self._is_challenging: bool = False

    @property
    def is_op_enabled(self) -> bool:
        """当前是否拥有 OP 权限。"""
        with self._lock:
            return self._is_op_enabled

    @property
    def is_challenging(self) -> bool:
        """是否处于 OP 权限挑战中。"""
        with self._lock:
            return self._is_challenging

    def on_set_command_enabled(
        self,
        command: str,
        enabled: bool,
    ) -> None:
        """收到 ``SetCommandEnabled`` 包时调用。

        Args:
            command: 命令名称。
            enabled: 是否启用。
        """
        with self._lock:
            now = time.time()
            self._change_events.append({
                "timestamp": now,
                "type": "set_command_enabled",
                "command": command,
                "enabled": enabled,
            })
            self._cleanup_locked()
            self._total_changes += 1
            self._last_change_at = now
            # 检查是否触发挑战
            recent_count = len(self._change_events)
            if recent_count >= self._config.operator_change_threshold:
                self._is_challenging = True
                logger.warning(
                    f"OperatorChallenge: 检测到 {recent_count} 次权限变更, "
                    f"视为 OP 挑战"
                )

    def on_op_level_change(self, op_level: int) -> None:
        """OP 等级变更时调用。

        Args:
            op_level: 新的 OP 等级 (0 = 无 OP, 4 = 满 OP)。
        """
        with self._lock:
            now = time.time()
            old_enabled = self._is_op_enabled
            self._is_op_enabled = op_level > 0
            self._change_events.append({
                "timestamp": now,
                "type": "op_level_change",
                "op_level": op_level,
                "is_op": self._is_op_enabled,
            })
            self._cleanup_locked()
            self._total_changes += 1
            self._last_change_at = now
            if old_enabled != self._is_op_enabled:
                logger.info(
                    f"OperatorChallenge: OP 权限变更 -> "
                    f"level={op_level} is_op={self._is_op_enabled}"
                )

    def check_stable(self) -> bool:
        """检查 OP 权限是否已稳定 (无变更超过窗口)。

        Returns:
            ``True`` 已稳定; ``False`` 仍在变更中。
        """
        with self._lock:
            if self._last_change_at == 0.0:
                return True
            elapsed = time.time() - self._last_change_at
            if elapsed >= self._config.operator_change_window:
                if self._is_challenging:
                    self._is_challenging = False
                    logger.info(
                        f"OperatorChallenge: OP 权限已稳定 "
                        f"({elapsed:.1f}s 无变更)"
                    )
                return True
            return False

    def reset(self) -> None:
        """重置监控状态。"""
        with self._lock:
            self._change_events.clear()
            self._is_op_enabled = False
            self._last_change_at = 0.0
            self._is_challenging = False

    def stats(self) -> Dict[str, Any]:
        """返回监控统计。"""
        with self._lock:
            return {
                "is_op_enabled": self._is_op_enabled,
                "is_challenging": self._is_challenging,
                "total_changes": self._total_changes,
                "recent_change_count": len(self._change_events),
                "seconds_since_last_change": (
                    time.time() - self._last_change_at
                    if self._last_change_at > 0
                    else -1.0
                ),
                "change_window": self._config.operator_change_window,
            }

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _cleanup_locked(self) -> None:
        """清理窗口外的事件 (必须在锁内调用)。"""
        now = time.time()
        cutoff = now - self._config.operator_change_window
        while self._change_events and self._change_events[0]["timestamp"] < cutoff:
            self._change_events.popleft()


# ---------------------------------------------------------------------------
# safe_writer 原子写入 (ToolDelta 风格)
# ---------------------------------------------------------------------------
def safe_writer(
    file_path: str | Path,
    data: Any,
    *,
    encoding: str = "utf-8",
    as_json: bool = False,
    retries: Optional[int] = None,
    retry_delay: Optional[float] = None,
) -> bool:
    """原子写入文件 (逆向自 ToolDelta ``safe_writer``)。

    ToolDelta 使用 ``safe_writer`` 来避免状态文件在写入过程中损坏:
        1. 先写入临时文件 (``<path>.tmp``)
        2. 调用 ``os.replace`` 原子替换原文件
        3. 失败时重试 (最多 ``retries`` 次)

    Args:
        file_path: 目标文件路径。
        data: 要写入的数据。
        encoding: 文本编码 (默认 ``utf-8``)。
        as_json: 是否以 JSON 格式写入 (会调用 ``json.dump``)。
        retries: 重试次数; ``None`` 使用默认值。
        retry_delay: 重试间隔 (秒); ``None`` 使用默认值。

    Returns:
        ``True`` 写入成功; ``False`` 失败。
    """
    config = EnhancedAntiBanConfig()
    retries = retries if retries is not None else config.safe_writer_retries
    retry_delay = (
        retry_delay if retry_delay is not None else config.safe_writer_retry_delay
    )

    file_path = Path(file_path)
    tmp_path = file_path.with_suffix(file_path.suffix + config.safe_writer_suffix)

    # 准备数据
    if as_json:
        content = json.dumps(data, ensure_ascii=False, indent=2)
    elif isinstance(data, bytes):
        content = data
    else:
        content = str(data)

    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # 写入临时文件
            if isinstance(content, bytes):
                with open(tmp_path, "wb") as f:
                    f.write(content)
            else:
                with open(tmp_path, "w", encoding=encoding) as f:
                    f.write(content)
            # 原子替换
            os.replace(tmp_path, file_path)
            return True
        except OSError as exc:
            last_error = exc
            logger.warning(
                f"safe_writer 写入失败 (attempt {attempt + 1}/{retries}): {exc}"
            )
            # 清理临时文件
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            if attempt < retries - 1:
                time.sleep(retry_delay)

    logger.error(f"safe_writer 最终失败: {last_error}")
    return False


# ---------------------------------------------------------------------------
# 增强总控: EnhancedAntiBan
# ---------------------------------------------------------------------------
class EnhancedAntiBan:
    """反封禁增强总控。

    聚合 ToolDelta / NovaBuilder / NexusE 逆向出的全部增强策略,
    对外提供统一接口。

    与 ``anti_ban.AntiBanController`` 的关系:
        - ``AntiBanController`` 提供基础防封禁 (JitterDelay / RateLimit / Behavior)
        - ``EnhancedAntiBan`` 在其基础上添加高级策略 (心跳 / 退避 / 人类化 / Orion)
        - 两者通过组合模式协作, 不替换关系

    典型用法::

        from .anti_ban import AntiBanController
        from .anti_ban_enhanced import EnhancedAntiBan

        base = AntiBanController()
        enhanced = EnhancedAntiBan(base)
        await enhanced.start_heartbeat(send_callback)
    """

    def __init__(
        self,
        base_controller: Optional[Any] = None,
        config: Optional[EnhancedAntiBanConfig] = None,
    ) -> None:
        self._base = base_controller
        self._config = config or EnhancedAntiBanConfig()
        self._rng = random.Random()

        # 组件
        self._backoff = ToolDeltaBackoff(self._config, self._rng)
        self._heartbeat = HeartbeatMonitor(self._config, self._rng)
        self._humanize = HumanizeCommand(self._config, self._rng)
        self._orion = OrionThresholdMonitor(self._config)
        self._command_tracker = CommandUUIDTracker(self._config)
        self._postpone = PostponeActionQueue(self._config)
        self._operator_monitor = OperatorChallengeMonitor(self._config)
        self._flowers_op = FlowersOPManager(self._rng)

        # 心跳任务
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._heartbeat_callback: Optional[Callable[[], Awaitable[Any]]] = None
        self._is_running: bool = False

    # ------------------------------------------------------------------
    # 属性访问
    # ------------------------------------------------------------------
    @property
    def config(self) -> EnhancedAntiBanConfig:
        return self._config

    @property
    def backoff(self) -> ToolDeltaBackoff:
        return self._backoff

    @property
    def heartbeat(self) -> HeartbeatMonitor:
        return self._heartbeat

    @property
    def humanize(self) -> HumanizeCommand:
        return self._humanize

    @property
    def orion(self) -> OrionThresholdMonitor:
        return self._orion

    @property
    def command_tracker(self) -> CommandUUIDTracker:
        return self._command_tracker

    @property
    def postpone(self) -> PostponeActionQueue:
        return self._postpone

    @property
    def operator_monitor(self) -> OperatorChallengeMonitor:
        return self._operator_monitor

    @property
    def flowers_op(self) -> "FlowersOPManager":
        return self._flowers_op

    # ------------------------------------------------------------------
    # 心跳任务管理
    # ------------------------------------------------------------------
    async def start_heartbeat(
        self,
        send_callback: Callable[[], Awaitable[Any]],
    ) -> None:
        """启动 8s 心跳任务。

        Args:
            send_callback: 心跳发送回调 (通常是 ``testfor @s``)。
        """
        if self._is_running:
            return
        self._heartbeat_callback = send_callback
        self._is_running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        logger.info(
            f"增强心跳已启动 (间隔={self._config.heartbeat_interval}s, "
            f"阈值={self._config.heartbeat_failure_threshold})"
        )

    async def stop_heartbeat(self) -> None:
        """停止心跳任务。"""
        self._is_running = False
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        logger.info("增强心跳已停止")

    async def _heartbeat_loop(self) -> None:
        """心跳循环: 每 8s 发送一次, 检查响应。"""
        logger.debug("心跳循环已启动")
        try:
            while self._is_running:
                interval = self._heartbeat.next_interval()
                await asyncio.sleep(interval)
                if not self._is_running:
                    break

                # 检查上次心跳是否超时
                if self._heartbeat.check_timeout():
                    should_reconnect = self._heartbeat.record_failure()
                    if should_reconnect:
                        logger.warning("心跳连续失败, 触发重连")
                        # 通知调用方重连 (通过 base_controller)
                        if self._base is not None and hasattr(
                            self._base, "on_action_failure"
                        ):
                            self._base.on_action_failure(
                                severity="severe",
                                source="heartbeat",
                                message="心跳连续失败, 判定断线",
                            )
                        break
                    continue

                # 发送心跳
                try:
                    if self._heartbeat_callback is not None:
                        await self._heartbeat_callback()
                    self._heartbeat.record_sent()
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"心跳发送失败: {exc}")
                    self._heartbeat.record_failure()

        except asyncio.CancelledError:
            logger.debug("心跳循环被取消")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"心跳循环异常: {exc}")
        finally:
            # P1 修复: 无论正常退出还是异常退出, 都要重置 _is_running
            # 否则心跳永久死亡, 无法重新启动
            if self._is_running:
                self._is_running = False
                logger.debug("心跳循环已退出, _is_running 已重置")

    # ------------------------------------------------------------------
    # 命令发送钩子
    # ------------------------------------------------------------------
    def before_send_command(self, command: str) -> Tuple[str, str]:
        """命令发送前钩子。

        - 人类化命令文本
        - Orion 阈值检查
        - 生成命令 UUID

        Returns:
            (humanized_command, command_uuid)
        """
        # 1. 人类化
        humanized = self._humanize.humanize(command)

        # 2. Orion 命令速率检查
        self._orion.record_command()

        # 3. 生成 UUID
        command_uuid = self._command_tracker.track(humanized)

        return humanized, command_uuid

    def on_command_response(self, command_uuid: str, success: bool = True) -> None:
        """命令响应到达时调用。"""
        self._command_tracker.complete(command_uuid, success)
        if success:
            self._heartbeat.record_ack()

    # ------------------------------------------------------------------
    # 挑战期间钩子
    # ------------------------------------------------------------------
    async def on_challenge_start(self) -> None:
        """MCPC 挑战开始时调用。"""
        await self._postpone.pause()

    async def on_challenge_passed(self) -> None:
        """MCPC 挑战通过后调用。"""
        await self._postpone.resume()

    async def execute_or_postpone(
        self,
        action: Callable[[], Awaitable[Any]],
    ) -> bool:
        """执行或排队动作 (挑战期间排队)。

        Returns:
            ``True`` 已排队; ``False`` 已执行。
        """
        return await self._postpone.enqueue_or_execute(action)

    # ------------------------------------------------------------------
    # OP 权限钩子
    # ------------------------------------------------------------------
    def on_set_command_enabled(self, command: str, enabled: bool) -> None:
        """收到 SetCommandEnabled 包时调用。"""
        self._operator_monitor.on_set_command_enabled(command, enabled)

    def on_op_level_change(self, op_level: int) -> None:
        """OP 等级变更时调用。"""
        self._operator_monitor.on_op_level_change(op_level)

    # ------------------------------------------------------------------
    # 重连钩子
    # ------------------------------------------------------------------
    def get_reconnect_delay(self) -> float:
        """获取重连延迟 (ToolDelta 退避序列)。"""
        return self._backoff.next_delay()

    def should_retry_reconnect(self) -> bool:
        """是否应该继续重连。"""
        return self._backoff.should_retry()

    def on_reconnect_success(self) -> None:
        """重连成功后调用。"""
        self._backoff.reset()
        self._heartbeat.reset()
        self._orion.reset()
        self._command_tracker.reset()
        if self._base is not None and hasattr(self._base, "on_reconnect_success"):
            self._base.on_reconnect_success()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """返回完整状态。"""
        return {
            "backoff": self._backoff.stats(),
            "heartbeat": self._heartbeat.stats(),
            "orion": self._orion.stats(),
            "command_tracker": self._command_tracker.stats(),
            "postpone": self._postpone.stats(),
            "operator_monitor": self._operator_monitor.stats(),
            "flowers_op": self._flowers_op.stats(),
            "is_running": self._is_running,
        }


# ---------------------------------------------------------------------------
# FlowersOPManager 隐式 OP 获取 (NexusE 逆向)
# ---------------------------------------------------------------------------
class FlowersOPManager:
    """FlowersOP 隐式 OP 获取系统 (逆向自 NexusE, 32 个函数简化为核心逻辑)。

    NexusE 的 FlowersOP 是一种不依赖标准 ``op`` 权限的隐式 OP 获取方式,
    通过 ``wocmd`` 命令执行来绕过权限检查, 使机器人能够在没有 OP 权限的
    服务器上执行管理员命令。

    核心原理:
        1. 从命令输出文本中解析 OP 授权信息 (parse_flowers_op_request_from_text)
        2. 从登录链中解析机器人名称 (parse_flowers_bot_name_from_chain)
        3. 通过 ``wocmd`` 发送 OP 请求 (request_flowers_op)
        4. 隐藏输出 (should_hide_flowers_output)
        5. 状态管理 (set_flowers_state, is_flowers_service_ready)
        6. 重试机制 (start_flowers_op_retry)

    工作流程:
        1. 连接建立后, 从登录链中提取机器人名
        2. 监听服务器命令输出, 寻找 OP 授权提示
        3. 发现授权提示后, 通过 ``wocmd`` 发送 OP 请求
        4. 失败时自动重试 (指数退避)
    """

    #: OP 状态枚举
    STATE_UNKNOWN = "unknown"       # 未知状态
    STATE_READY = "ready"           # 服务就绪 (可请求 OP)
    STATE_PENDING = "pending"       # 等待 OP 授权
    STATE_ACTIVE = "active"         # OP 已激活
    STATE_FAILED = "failed"         # OP 获取失败

    #: OP 授权文本模式 (从命令输出中识别)
    _OP_PATTERNS = [
        "op ",
        "op:",
        "operator",
        "permission",
        "grant",
        "授权",
        "OP",
        "管理员",
    ]

    def __init__(self, rng: Optional[random.Random] = None) -> None:
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._state: str = self.STATE_UNKNOWN
        self._bot_name: str = ""
        self._last_op_request_at: float = 0.0
        self._retry_count: int = 0
        self._max_retries: int = 5
        self._retry_delays: tuple[float, ...] = (5.0, 10.0, 20.0, 40.0, 80.0)
        self._hidden_output: bool = True
        self._total_requests: int = 0
        self._total_successes: int = 0

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        """当前 OP 状态。"""
        with self._lock:
            return self._state

    def set_flowers_state(self, state: str) -> None:
        """设置 FlowersOP 状态。

        Args:
            state: 状态字符串 (STATE_UNKNOWN / STATE_READY / STATE_PENDING /
                   STATE_ACTIVE / STATE_FAILED)。
        """
        valid_states = {
            self.STATE_UNKNOWN, self.STATE_READY,
            self.STATE_PENDING, self.STATE_ACTIVE, self.STATE_FAILED,
        }
        if state not in valid_states:
            logger.warning(f"FlowersOP: 无效状态 '{state}', 使用 'unknown'")
            state = self.STATE_UNKNOWN
        with self._lock:
            old_state = self._state
            self._state = state
            if old_state != state:
                logger.info(f"FlowersOP: 状态变更 {old_state} -> {state}")

    def is_flowers_service_ready(self) -> bool:
        """检查 FlowersOP 服务是否就绪 (可请求 OP)。

        Returns:
            ``True`` 服务就绪; ``False`` 未就绪。
        """
        with self._lock:
            return self._state == self.STATE_READY

    @property
    def bot_name(self) -> str:
        """机器人名称。"""
        with self._lock:
            return self._bot_name

    # ------------------------------------------------------------------
    # 命令输出解析
    # ------------------------------------------------------------------
    def parse_flowers_op_request_from_text(self, text: str) -> Optional[str]:
        """从命令输出文本中解析 OP 授权请求信息。

        Args:
            text: 服务器命令输出文本。

        Returns:
            OP 授权请求字符串 (如 ``"op <bot_name>"``), 或 ``None`` 未找到。
        """
        if not text:
            return None
        text_lower = text.lower()
        for pattern in self._OP_PATTERNS:
            if pattern.lower() in text_lower:
                # 提取 OP 相关行
                lines = text.split("\n")
                for line in lines:
                    if pattern.lower() in line.lower():
                        logger.debug(f"FlowersOP: 检测到 OP 相关文本: {line[:80]}")
                        return line.strip()
        return None

    def parse_flowers_bot_name_from_chain(self, chain_data: dict[str, Any]) -> str:
        """从登录链中解析机器人名称。

        Args:
            chain_data: 登录链数据 (JWT chain 解析结果)。

        Returns:
            机器人名称, 如果找不到则返回空字符串。
        """
        with self._lock:
            # 尝试从 chain 数据中提取 ExtraData.displayName
            extra_data = chain_data.get("extraData", {})
            if isinstance(extra_data, dict):
                display_name = extra_data.get("displayName", "")
                if display_name:
                    self._bot_name = str(display_name)
                    logger.info(f"FlowersOP: 从登录链获取机器人名: {self._bot_name}")
                    return self._bot_name
            # 尝试从 chain 数据中提取 identityData.displayName
            identity_data = chain_data.get("identityData", {})
            if isinstance(identity_data, dict):
                display_name = identity_data.get("displayName", "")
                if display_name:
                    self._bot_name = str(display_name)
                    logger.info(f"FlowersOP: 从身份数据获取机器人名: {self._bot_name}")
                    return self._bot_name
            # 尝试从 chain 数据中提取 username
            username = chain_data.get("username", "")
            if username:
                self._bot_name = str(username)
                logger.info(f"FlowersOP: 从登录链获取用户名: {self._bot_name}")
                return self._bot_name
            return ""

    # ------------------------------------------------------------------
    # OP 请求
    # ------------------------------------------------------------------
    def request_flowers_op(self, send_callback: Callable[[str], Any]) -> bool:
        """请求 FlowersOP (通过 ``wocmd`` 发送 OP 请求)。

        Args:
            send_callback: 命令发送回调 (用于发送 ``wocmd`` 命令)。

        Returns:
            ``True`` 请求已发送; ``False`` 未就绪或已激活。
        """
        with self._lock:
            if self._state == self.STATE_ACTIVE:
                logger.debug("FlowersOP: OP 已激活, 跳过请求")
                return False
            if self._state not in (self.STATE_READY, self.STATE_UNKNOWN):
                logger.debug(f"FlowersOP: 状态 {self._state} 不允许请求 OP")
                return False
            if not self._bot_name:
                logger.warning("FlowersOP: 机器人名未知, 无法请求 OP")
                return False
            self._state = self.STATE_PENDING
            self._last_op_request_at = time.time()
            self._total_requests += 1

        # 发送 OP 请求 (格式: ``wocmd op <bot_name>``)
        op_command = f"wocmd op {self._bot_name}"
        try:
            send_callback(op_command)
            logger.info(f"FlowersOP: 已发送 OP 请求: {op_command}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error(f"FlowersOP: 发送 OP 请求失败: {exc}")
            with self._lock:
                self._state = self.STATE_FAILED
            return False

    def on_op_granted(self) -> None:
        """OP 授权成功时调用。"""
        with self._lock:
            self._state = self.STATE_ACTIVE
            self._total_successes += 1
            self._retry_count = 0
            logger.info("FlowersOP: OP 已激活!")

    def on_op_revoked(self) -> None:
        """OP 权限被撤销时调用。"""
        with self._lock:
            self._state = self.STATE_READY
            logger.warning("FlowersOP: OP 权限被撤销, 将尝试重新获取")

    # ------------------------------------------------------------------
    # 输出隐藏
    # ------------------------------------------------------------------
    def should_hide_flowers_output(self) -> bool:
        """是否应隐藏 FlowersOP 相关输出。

        Returns:
            ``True`` 应隐藏输出; ``False`` 正常显示。
        """
        with self._lock:
            return self._hidden_output

    def set_hide_output(self, hide: bool) -> None:
        """设置是否隐藏 FlowersOP 输出。

        Args:
            hide: ``True`` 隐藏输出; ``False`` 正常显示。
        """
        with self._lock:
            self._hidden_output = hide

    # ------------------------------------------------------------------
    # 重试机制
    # ------------------------------------------------------------------
    def start_flowers_op_retry(
        self,
        send_callback: Callable[[str], Any],
    ) -> float:
        """启动 OP 获取重试 (指数退避)。

        Args:
            send_callback: 命令发送回调。

        Returns:
            下次重试的延迟秒数; 返回 0 表示不再重试。
        """
        with self._lock:
            if self._state == self.STATE_ACTIVE:
                return 0.0
            if self._retry_count >= self._max_retries:
                logger.warning(f"FlowersOP: 已达最大重试次数 ({self._max_retries}), 放弃")
                self._state = self.STATE_FAILED
                return 0.0
            idx = min(self._retry_count, len(self._retry_delays) - 1)
            delay = self._retry_delays[idx]
            self._retry_count += 1
            logger.info(
                f"FlowersOP: 第 {self._retry_count}/{self._max_retries} 次重试, "
                f"延迟 {delay}s"
            )

        # 安排重试 (调用方负责在延迟后调用 request_flowers_op)
        return delay

    def reset_retry(self) -> None:
        """重置重试计数。"""
        with self._lock:
            self._retry_count = 0

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """返回 FlowersOP 统计。"""
        with self._lock:
            return {
                "state": self._state,
                "bot_name": self._bot_name,
                "total_requests": self._total_requests,
                "total_successes": self._total_successes,
                "retry_count": self._retry_count,
                "max_retries": self._max_retries,
                "hidden_output": self._hidden_output,
                "seconds_since_last_request": (
                    time.time() - self._last_op_request_at
                    if self._last_op_request_at > 0
                    else -1.0
                ),
            }


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
_global_enhanced: Optional[EnhancedAntiBan] = None
_global_lock = threading.Lock()


def get_enhanced_anti_ban(
    base_controller: Optional[Any] = None,
) -> EnhancedAntiBan:
    """返回全局 :class:`EnhancedAntiBan` 单例。

    Args:
        base_controller: 基础 ``AntiBanController`` 实例 (首次调用时注入)。
    """
    global _global_enhanced
    with _global_lock:
        if _global_enhanced is None:
            _global_enhanced = EnhancedAntiBan(base_controller=base_controller)
        return _global_enhanced


def reset_enhanced_anti_ban() -> None:
    """重置全局单例 (主要用于测试)。"""
    global _global_enhanced
    with _global_lock:
        _global_enhanced = None


__all__ = [
    # 配置
    "EnhancedAntiBanConfig",
    # 组件
    "ToolDeltaBackoff",
    "HeartbeatMonitor",
    "HumanizeCommand",
    "OrionThresholdMonitor",
    "CommandUUIDTracker",
    "PostponeActionQueue",
    "OperatorChallengeMonitor",
    "FlowersOPManager",
    "safe_writer",
    # 总控
    "EnhancedAntiBan",
    "get_enhanced_anti_ban",
    "reset_enhanced_anti_ban",
]
