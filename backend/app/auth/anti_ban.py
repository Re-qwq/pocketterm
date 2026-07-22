"""PocketTerm 防封禁策略模块

本模块实现多层防封禁策略, 灵感来自 NovaBuilder / NexusE 逆向结果中的
反作弊规避代码 (``rate.Limiter`` / ``SleepTick`` / 心跳包抖动等)。

防封禁策略
----------

1. **随机延迟 (JitterDelay)**
   所有时间敏感操作 (心跳、命令、移动、重连) 都加入随机抖动,
   避免固定周期触发的反作弊检测。

2. **行为模拟 (BehaviorSimulator)**
   模拟真实玩家行为:
   - 随机 "短暂休息" (短时间停止操作)
   - 长时间在线后自动 "短暂离线" (模拟玩家关机/切应用)
   - 命令 / 聊天节奏符合人类分布 (Poisson 间隔)

3. **速率限制 (RateLimitController)**
   基于 Token Bucket 算法, 根据服务器响应动态调整:
   - 收到限流 / 反作弊提示 -> 自动降速
   - 持续稳定运行 -> 逐步恢复
   - 提供全局速率 (操作/分钟) 与突发上限两个维度

4. **异常检测 (AnomalyDetector)**
   检测以下异常并自动停止 / 降级:
   - 反作弊关键词 (kick / ban / 速率 / 频率 / 异常行为)
   - 服务器连续无响应 (超过阈值)
   - 短时间内大量错误响应
   - 命令响应延迟异常增长

设计原则
--------

- 所有公共方法均为线程安全 (使用 ``threading.Lock``)
- 关键策略可配置 (通过 :class:`AntiBanConfig`)
- 与 ``bot.py`` 解耦: 仅暴露 ``should_proceed`` / ``wait_before_action`` 等钩子
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional

from ..logger import get_logger

logger = get_logger("auth.anti_ban")


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@dataclass
class AntiBanConfig:
    """防封禁策略配置。

    所有时间单位均为秒, 速率单位为 "操作次数/分钟"。
    """

    # -- 随机延迟 --
    #: 基础动作延迟下限 (秒)
    min_action_delay: float = 0.5
    #: 基础动作延迟上限 (秒)
    max_action_delay: float = 2.5
    #: 心跳抖动上下限 (秒), 实际心跳间隔 = base +/- jitter
    heartbeat_jitter: float = 5.0
    #: 重连延迟抖动倍率 (实际 = base * (1 +/- jitter_ratio))
    reconnect_jitter_ratio: float = 0.4

    # -- 速率限制 --
    #: 默认速率上限 (操作/分钟)
    default_rate_per_minute: float = 60.0
    #: 默认突发上限 (操作次数)
    default_burst: int = 10
    #: 最低速率 (降速底线, 防止被压到 0)
    min_rate_per_minute: float = 5.0
    #: 最高速率 (升速上限, 防止被推到极限)
    max_rate_per_minute: float = 180.0
    #: 降速系数 (收到异常时 rate *= (1 - factor))
    rate_decrease_factor: float = 0.5
    #: 升速系数 (持续稳定时 rate *= (1 + factor))
    rate_increase_factor: float = 0.1
    #: 升速所需的稳定运行秒数
    stable_window_seconds: float = 300.0

    # -- 行为模拟 --
    #: 是否启用行为模拟
    enable_behavior_simulation: bool = True
    #: 短暂休息触发概率 (每次动作前)
    micro_break_probability: float = 0.05
    #: 短暂休息时长 (秒)
    micro_break_duration: float = 3.0
    #: 长时间在线后休息触发阈值 (秒)
    long_session_threshold: float = 3600.0
    #: 长时间休息时长 (秒)
    long_break_duration: float = 30.0

    # -- 异常检测 --
    #: 服务器无响应阈值 (秒) - 超过则视为异常
    no_response_threshold: float = 90.0
    #: 错误响应窗口 (秒) - 在该窗口内统计错误次数
    error_window_seconds: float = 60.0
    #: 错误响应阈值 - 超过则触发降速
    error_threshold: int = 5
    #: 严重错误阈值 - 超过则触发自动停止
    critical_error_threshold: int = 10
    #: 反作弊关键词列表 (匹配则视为严重异常)
    anticheat_keywords: List[str] = field(
        default_factory=lambda: [
            "anticheat", "反作弊", "作弊检测", "客户端异常",
            "client modified", "modified client", "third party",
            "第三方", "外挂", "脚本", "macro", "按键精灵",
            "rate limit", "too many requests", "操作过于频繁",
            "请求频率过高", "请稍后再试", "频率过快",
            "kicked for", "you are banned", "已被封禁", "已被踢出",
            "suspicious", "可疑行为", "异常行为",
        ]
    )


# ---------------------------------------------------------------------------
# 随机延迟
# ---------------------------------------------------------------------------
class JitterDelay:
    """随机延迟生成器。

    提供多种延迟模式, 所有方法均线程安全。
    """

    def __init__(self, config: AntiBanConfig, rng: Optional[random.Random] = None) -> None:
        self._config = config
        self._rng = rng or random.Random()
        self._lock = threading.Lock()

    def action_delay(self) -> float:
        """普通操作延迟 (秒)。"""
        with self._lock:
            return self._rng.uniform(
                self._config.min_action_delay, self._config.max_action_delay
            )

    def heartbeat_interval(self, base: float = 30.0) -> float:
        """带抖动的心跳间隔 (秒)。"""
        with self._lock:
            jitter = self._rng.uniform(
                -self._config.heartbeat_jitter, self._config.heartbeat_jitter
            )
            return max(1.0, base + jitter)

    def reconnect_delay(self, base: float, attempt: int = 1) -> float:
        """带抖动的重连延迟 (秒)。

        采用指数退避 + 随机抖动:
            delay = base * 2^min(attempt, 6) * (1 +/- jitter_ratio)
        """
        with self._lock:
            exponent = min(max(attempt, 1), 6)
            base_delay = base * (2 ** (exponent - 1))
            jitter = self._rng.uniform(
                -self._config.reconnect_jitter_ratio,
                self._config.reconnect_jitter_ratio,
            )
            return max(1.0, base_delay * (1.0 + jitter))

    def micro_break_delay(self) -> float:
        """短暂休息时长 (秒)。"""
        with self._lock:
            # 加入 50% 抖动
            return self._config.micro_break_duration * self._rng.uniform(0.5, 1.5)

    def long_break_delay(self) -> float:
        """长时间休息时长 (秒)。"""
        with self._lock:
            return self._config.long_break_duration * self._rng.uniform(0.7, 1.3)


# ---------------------------------------------------------------------------
# 速率限制 (Token Bucket)
# ---------------------------------------------------------------------------
class RateLimitController:
    """基于 Token Bucket 的速率限制器, 支持动态调速。

    算法:
        - 桶容量 = burst (突发上限)
        - 补充速率 = rate_per_minute / 60 (token/秒)
        - 每次操作消耗 1 token, 不足时阻塞或拒绝

    特性:
        - 收到异常信号 (``on_anomaly``) 时自动降速
        - 持续稳定运行 (``on_success``) 时逐步升速
    """

    def __init__(self, config: AntiBanConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._rate_per_minute: float = config.default_rate_per_minute
        self._burst: int = config.default_burst
        self._tokens: float = float(config.default_burst)
        self._last_refill: float = time.time()
        self._stable_since: float = time.time()
        self._last_rate_adjust: float = time.time()

    @property
    def rate_per_minute(self) -> float:
        """当前速率 (操作/分钟)。"""
        with self._lock:
            return self._rate_per_minute

    @property
    def burst(self) -> int:
        """当前突发上限。"""
        with self._lock:
            return self._burst

    def tokens(self) -> float:
        """当前可用 token 数 (已补足)。"""
        with self._lock:
            self._refill_locked()
            return self._tokens

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """尝试消耗 token, 不阻塞。

        Returns:
            ``True`` 成功; ``False`` 令牌不足。
        """
        with self._lock:
            self._refill_locked()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        """阻塞消耗 token。

        Args:
            tokens: 需要消耗的 token 数。
            timeout: 最大等待秒数; ``None`` 表示不等待。

        Returns:
            ``True`` 成功获取; ``False`` 超时。
        """
        import time as _time

        deadline = None if timeout is None else _time.time() + timeout
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
            # 估算需要等待的时间
            with self._lock:
                needed = tokens - self._tokens
                rate_per_sec = max(self._rate_per_minute / 60.0, 1e-6)
                wait = needed / rate_per_sec
            if deadline is not None:
                remaining = deadline - _time.time()
                if remaining <= 0:
                    return False
                wait = min(wait, remaining)
            _time.sleep(min(wait, 0.5))

    # ------------------------------------------------------------------
    # 动态调速
    # ------------------------------------------------------------------
    def on_success(self) -> None:
        """记录一次成功操作 (用于稳定期升速)。"""
        with self._lock:
            now = time.time()
            if now - self._last_rate_adjust >= self._config.stable_window_seconds:
                # 持续稳定, 尝试升速
                new_rate = min(
                    self._rate_per_minute * (1.0 + self._config.rate_increase_factor),
                    self._config.max_rate_per_minute,
                )
                if new_rate != self._rate_per_minute:
                    logger.debug(
                        f"速率升速: {self._rate_per_minute:.1f} -> {new_rate:.1f} /min"
                    )
                    self._rate_per_minute = new_rate
                    self._burst = max(
                        self._burst,
                        int(self._rate_per_minute / 10),
                    )
                self._last_rate_adjust = now
                self._stable_since = now

    def on_anomaly(self, severity: str = "normal") -> None:
        """记录一次异常 (触发降速)。

        Args:
            severity: ``"normal"`` 普通异常 (轻度降速);
                      ``"severe"`` 严重异常 (大幅降速)。
        """
        with self._lock:
            factor = (
                self._config.rate_decrease_factor
                if severity == "normal"
                else self._config.rate_decrease_factor * 0.5
            )
            new_rate = max(
                self._rate_per_minute * (1.0 - factor),
                self._config.min_rate_per_minute,
            )
            if new_rate != self._rate_per_minute:
                logger.warning(
                    f"异常 ({severity}) 触发降速: "
                    f"{self._rate_per_minute:.1f} -> {new_rate:.1f} /min"
                )
                self._rate_per_minute = new_rate
                self._burst = max(1, int(self._burst * (1.0 - factor * 0.5)))
            self._last_rate_adjust = time.time()
            self._stable_since = time.time()  # 重置稳定计时

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _refill_locked(self) -> None:
        """补足 token (必须在锁内调用)。"""
        now = time.time()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        refill = elapsed * (self._rate_per_minute / 60.0)
        self._tokens = min(float(self._burst), self._tokens + refill)
        self._last_refill = now

    def stats(self) -> Dict[str, float]:
        """返回当前状态 (供 API / 日志使用)。"""
        with self._lock:
            self._refill_locked()
            return {
                "rate_per_minute": self._rate_per_minute,
                "burst": float(self._burst),
                "tokens": round(self._tokens, 2),
                "stable_seconds": time.time() - self._stable_since,
            }


# ---------------------------------------------------------------------------
# 行为模拟
# ---------------------------------------------------------------------------
class BehaviorSimulator:
    """模拟真实玩家行为。

    在每次动作前以一定概率触发 "短暂休息",
    长时间在线后自动触发 "长休息"。
    """

    def __init__(
        self,
        config: AntiBanConfig,
        jitter: JitterDelay,
        rng: Optional[random.Random] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._config = config
        self._jitter = jitter
        self._rng = rng or random.Random()
        self._sleeper = sleeper or time.sleep
        self._lock = threading.Lock()
        self._session_start: float = time.time()
        self._last_action: float = time.time()
        self._in_long_break: bool = False

    def reset_session(self) -> None:
        """重置会话计时器 (重连后调用)。"""
        with self._lock:
            self._session_start = time.time()
            self._last_action = time.time()
            self._in_long_break = False

    def before_action(self, action_name: str = "action") -> float:
        """动作前钩子: 返回需要等待的秒数 (调用方负责 sleep)。

        - 以 ``micro_break_probability`` 概率触发短暂休息
        - 长时间在线后触发长休息

        Returns:
            需要等待的秒数 (0 表示无需等待)。
        """
        if not self._config.enable_behavior_simulation:
            return 0.0

        with self._lock:
            now = time.time()
            session_duration = now - self._session_start
            self._last_action = now

        # 长时间在线 -> 触发长休息
        if session_duration > self._config.long_session_threshold:
            delay = self._jitter.long_break_delay()
            logger.info(
                f"行为模拟: 长时间在线 {session_duration:.0f}s, "
                f"触发长休息 {delay:.1f}s (action={action_name})"
            )
            return delay

        # 短暂休息
        if self._rng.random() < self._config.micro_break_probability:
            delay = self._jitter.micro_break_delay()
            logger.debug(
                f"行为模拟: 短暂休息 {delay:.1f}s (action={action_name})"
            )
            return delay

        return 0.0

    def stats(self) -> Dict[str, float]:
        """返回当前会话统计。"""
        with self._lock:
            return {
                "session_duration": time.time() - self._session_start,
                "seconds_since_last_action": time.time() - self._last_action,
                "in_long_break": 1.0 if self._in_long_break else 0.0,
            }


# ---------------------------------------------------------------------------
# 异常检测
# ---------------------------------------------------------------------------
@dataclass
class AnomalyEvent:
    """异常事件记录。"""

    timestamp: float
    severity: str  # "normal" / "severe"
    source: str    # 来源标签 (如 "chat" / "command" / "heartbeat")
    message: str
    extras: Dict[str, str] = field(default_factory=dict)


class AnomalyDetector:
    """异常检测器。

    统计错误响应 / 反作弊关键词 / 服务器无响应等异常事件,
    触发降速或自动停止。
    """

    def __init__(self, config: AntiBanConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._events: Deque[AnomalyEvent] = deque()
        self._last_packet_time: float = time.time()
        self._paused: bool = False
        self._stop_requested: bool = False

    @property
    def paused(self) -> bool:
        """是否因异常暂停。"""
        with self._lock:
            return self._paused

    @property
    def stop_requested(self) -> bool:
        """是否请求停止 (严重异常)。"""
        with self._lock:
            return self._stop_requested

    def record_packet(self) -> None:
        """记录收到服务器数据包 (用于无响应检测)。"""
        with self._lock:
            self._last_packet_time = time.time()

    def check_no_response(self) -> bool:
        """检查服务器是否无响应超过阈值。

        Returns:
            ``True`` 视为无响应 (异常); ``False`` 正常。
        """
        with self._lock:
            elapsed = time.time() - self._last_packet_time
            return elapsed > self._config.no_response_threshold

    def record_anomaly(
        self,
        severity: str,
        source: str,
        message: str,
        extras: Optional[Dict[str, str]] = None,
    ) -> None:
        """记录一次异常事件。

        Args:
            severity: ``"normal"`` / ``"severe"``。
            source: 来源标签 (如 ``"chat"`` / ``"command"`` / ``"heartbeat"``)。
            message: 异常描述。
            extras: 附加信息。
        """
        event = AnomalyEvent(
            timestamp=time.time(),
            severity=severity,
            source=source,
            message=message,
            extras=dict(extras or {}),
        )
        with self._lock:
            self._events.append(event)
            # 滑动窗口清理
            cutoff = time.time() - self._config.error_window_seconds
            while self._events and self._events[0].timestamp < cutoff:
                self._events.popleft()

            # 统计
            severe_count = sum(1 for e in self._events if e.severity == "severe")
            normal_count = len(self._events) - severe_count

            # 触发自动停止
            if severe_count >= self._config.critical_error_threshold:
                self._stop_requested = True
                logger.critical(
                    f"严重异常累计 {severe_count} 次, 请求自动停止: {message}"
                )
                return

            # 触发暂停 (中等异常)
            if normal_count >= self._config.error_threshold:
                self._paused = True
                logger.warning(
                    f"异常累计 {normal_count} 次, 暂停操作: {message}"
                )

        logger.warning(
            f"记录异常 ({severity}/{source}): {message}"
        )

    def check_message(self, message: str) -> Optional[str]:
        """检查消息文本是否包含反作弊关键词。

        Returns:
            匹配到的关键词; 未匹配返回 ``None``。
        """
        if not message:
            return None
        msg_lower = message.lower()
        for keyword in self._config.anticheat_keywords:
            if keyword.lower() in msg_lower:
                return keyword
        return None

    def clear(self) -> None:
        """清除所有异常状态 (重连成功后调用)。"""
        with self._lock:
            self._events.clear()
            self._paused = False
            self._stop_requested = False
            self._last_packet_time = time.time()

    def recent_events(self, limit: int = 20) -> List[AnomalyEvent]:
        """返回最近 N 条异常事件 (供 API 使用)。"""
        with self._lock:
            return list(self._events)[-limit:]

    def stats(self) -> Dict[str, float]:
        """返回异常统计。"""
        with self._lock:
            severe = sum(1 for e in self._events if e.severity == "severe")
            normal = len(self._events) - severe
            return {
                "event_count": float(len(self._events)),
                "severe_count": float(severe),
                "normal_count": float(normal),
                "paused": 1.0 if self._paused else 0.0,
                "stop_requested": 1.0 if self._stop_requested else 0.0,
                "seconds_since_last_packet": time.time() - self._last_packet_time,
            }


# ---------------------------------------------------------------------------
# 总控: AntiBanController
# ---------------------------------------------------------------------------
class AntiBanController:
    """防封禁策略总控。

    聚合 :class:`JitterDelay` / :class:`RateLimitController` /
    :class:`BehaviorSimulator` / :class:`AnomalyDetector`, 对外提供统一接口。

    典型用法::

        ctrl = AntiBanController()
        await ctrl.wait_before_action("send_command")
        if not ctrl.should_proceed():
            return  # 被异常检测暂停
        # ... 执行操作 ...
        ctrl.on_action_success()
    """

    def __init__(
        self,
        config: Optional[AntiBanConfig] = None,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._config = config or AntiBanConfig()
        self._jitter = JitterDelay(self._config)
        self._rate_limit = RateLimitController(self._config)
        self._behavior = BehaviorSimulator(
            self._config, self._jitter, sleeper=sleeper
        )
        self._anomaly = AnomalyDetector(self._config)
        self._sleeper = sleeper or time.sleep
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 属性访问
    # ------------------------------------------------------------------
    @property
    def config(self) -> AntiBanConfig:
        return self._config

    @property
    def jitter(self) -> JitterDelay:
        return self._jitter

    @property
    def rate_limit(self) -> RateLimitController:
        return self._rate_limit

    @property
    def behavior(self) -> BehaviorSimulator:
        return self._behavior

    @property
    def anomaly(self) -> AnomalyDetector:
        return self._anomaly

    # ------------------------------------------------------------------
    # 钩子
    # ------------------------------------------------------------------
    def should_proceed(self) -> bool:
        """是否允许继续操作。

        - 异常检测暂停中 -> ``False``
        - 严重异常已请求停止 -> ``False``
        """
        if self._anomaly.stop_requested:
            return False
        if self._anomaly.paused:
            return False
        if self._anomaly.check_no_response():
            self._anomaly.record_anomaly(
                severity="severe",
                source="heartbeat",
                message="服务器无响应超过阈值",
            )
            return False
        return True

    def wait_before_action(self, action_name: str = "action") -> float:
        """动作前统一入口。

        - 检查速率限制
        - 触发行为模拟 (短暂休息 / 长休息)
        - 加入随机抖动

        Returns:
            实际等待的秒数。
        """
        if not self.should_proceed():
            return 0.0

        # 1. 速率限制: 尝试非阻塞获取, 不够则降速并等待
        if not self._rate_limit.try_acquire():
            self._rate_limit.on_anomaly(severity="normal")
            logger.debug(f"速率限制触发, 降速等待 (action={action_name})")

        # 2. 行为模拟
        sim_delay = self._behavior.before_action(action_name)

        # 3. 随机抖动
        jitter_delay = self._jitter.action_delay() * 0.3  # 轻度抖动

        total = sim_delay + jitter_delay
        if total > 0:
            self._sleeper(total)
        return total

    def on_action_success(self) -> None:
        """动作成功后调用 (用于升速 / 清除异常)。"""
        self._rate_limit.on_success()
        self._anomaly.record_packet()

    def on_action_failure(
        self,
        severity: str = "normal",
        source: str = "unknown",
        message: str = "",
    ) -> None:
        """动作失败后调用 (触发降速 / 异常记录)。"""
        self._rate_limit.on_anomaly(severity=severity)
        self._anomaly.record_anomaly(
            severity=severity, source=source, message=message
        )

    def on_chat_message(self, message: str) -> Optional[str]:
        """检查聊天消息, 命中反作弊关键词时记录异常。

        Returns:
            匹配到的关键词; 未匹配返回 ``None``。
        """
        keyword = self._anomaly.check_message(message)
        if keyword:
            self._anomaly.record_anomaly(
                severity="severe",
                source="chat",
                message=f"反作弊关键词: {keyword}",
                extras={"raw_message": message[:200]},
            )
            self._rate_limit.on_anomaly(severity="severe")
        return keyword

    def on_reconnect_success(self) -> None:
        """重连成功后调用 (清除异常状态)。"""
        self._anomaly.clear()
        self._behavior.reset_session()
        self._rate_limit.on_success()
        logger.info("重连成功, 防封禁状态已重置")

    def reset(self) -> None:
        """完全重置 (新会话)。"""
        self._anomaly.clear()
        self._behavior.reset_session()
        with self._lock:
            # 速率限制恢复默认
            self._rate_limit._rate_per_minute = self._config.default_rate_per_minute
            self._rate_limit._burst = self._config.default_burst
            self._rate_limit._tokens = float(self._config.default_burst)

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------
    def stats(self) -> Dict[str, Any]:
        """返回完整状态 (供 API / 日志使用)。"""
        return {
            "should_proceed": self.should_proceed(),
            "rate_limit": self._rate_limit.stats(),
            "behavior": self._behavior.stats(),
            "anomaly": self._anomaly.stats(),
            "recent_events": [
                {
                    "timestamp": e.timestamp,
                    "severity": e.severity,
                    "source": e.source,
                    "message": e.message,
                }
                for e in self._anomaly.recent_events(10)
            ],
        }


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
_global_controller: Optional[AntiBanController] = None
_global_lock = threading.Lock()


def get_anti_ban_controller() -> AntiBanController:
    """返回全局 :class:`AntiBanController` 单例。"""
    global _global_controller
    with _global_lock:
        if _global_controller is None:
            _global_controller = AntiBanController()
        return _global_controller


def reset_anti_ban_controller() -> None:
    """重置全局单例 (主要用于测试)。"""
    global _global_controller
    with _global_lock:
        _global_controller = None


__all__ = [
    # 配置
    "AntiBanConfig",
    # 组件
    "JitterDelay",
    "RateLimitController",
    "BehaviorSimulator",
    "AnomalyDetector",
    "AnomalyEvent",
    # 总控
    "AntiBanController",
    "get_anti_ban_controller",
    "reset_anti_ban_controller",
]
