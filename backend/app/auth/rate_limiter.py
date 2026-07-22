"""rate_limiter - 速率限制器 (Burst/BurstDensity/BurstDuration)。

逆向自 NexusEgo v1.6.5 的速率限制系统。来源:
    - rate_full.txt / rate_limit.txt (逆向字符串与符号表)
    - golang.org/x/time/rate (Go 标准令牌桶库)

逆向证据 (来自 rate_full.txt):
    - *rate.Limiter
    - Burst
    - BurstDensity
    - BurstDuration
    - ClientThrottle
    - ClientThrottleScalar
    - ClientThrottleThreshold
    - DelayThreshold
    - MoveItemSpeed                (mapstructure:"MoveItemSpeed")
    - SetBurst / SetBurstAt
    - TargetCooldownLength         (mapstructure:"target_cooldown_length")
    - TransferCooldown             (mapstructure:"TransferCooldown")
    - rate: Wait(n=%d) exceeds limiter's burst %d
    - golang.org/x/time/rate.(*Limiter).Burst
    - golang.org/x/time/rate.(*Limiter).SetBurst
    - golang.org/x/time/rate.(*Limiter).SetBurstAt

本模块实现:
    1. RateLimitConfig - 速率限制配置 (Burst/BurstDensity/BurstDuration)
    2. RateLimiter - 令牌桶速率限制器 (对应 Go rate.Limiter)
    3. MoveItemRateLimiter - 移动物品速率限制 (MoveItemSpeed/TransferCooldown)
    4. ClientThrottle - 客户端节流 (ClientThrottle/Scalar/Threshold)
    5. DelayController - 延迟控制器 (DelayThreshold/TargetCooldownLength)

令牌桶算法:
    - 桶容量 (Burst): 最大突发数量
    - 令牌生成速率 (BurstDensity): 每秒生成的令牌数
    - 突发持续时长 (BurstDuration): 突发可持续的时间
    - 当请求到来时, 消耗令牌; 令牌不足时阻塞或拒绝
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any, Callable

logger = logging.getLogger("pocketterm.auth.rate_limiter")


# ======================================================================
# 常量
# ======================================================================

#: 默认突发数量上限 (逆向自 strings: "Burst")
RATE_LIMIT_BURST: int = 100

#: 默认突发密度 (每秒令牌数) (逆向自 strings: "BurstDensity")
RATE_LIMIT_BURST_DENSITY: float = 20.0

#: 默认突发持续时间 (秒) (逆向自 strings: "BurstDuration")
RATE_LIMIT_BURST_DURATION: float = 5.0

#: 默认移动物品速度 (ticks) (逆向自 strings: "MoveItemSpeed")
DEFAULT_MOVE_ITEM_SPEED: int = 2

#: 默认容器转移冷却 (ticks) (逆向自 strings: "TransferCooldown")
DEFAULT_TRANSFER_COOLDOWN: int = 8

#: 默认目标冷却时长 (ticks) (逆向自 strings: "target_cooldown_length")
DEFAULT_TARGET_COOLDOWN_LENGTH: int = 10

#: 默认延迟阈值 (ticks) (逆向自 strings: "DelayThreshold")
DEFAULT_DELAY_THRESHOLD: int = 5

#: 默认客户端节流阈值 (逆向自 strings: "ClientThrottleThreshold")
DEFAULT_CLIENT_THROTTLE_THRESHOLD: int = 50

#: 默认客户端节流标量 (逆向自 strings: "ClientThrottleScalar")
DEFAULT_CLIENT_THROTTLE_SCALAR: float = 1.5

#: 默认客户端节流使能 (逆向自 strings: "ClientThrottle")
DEFAULT_CLIENT_THROTTLE_ENABLED: bool = True

#: 无限速率 (禁用限制)
INFINITE_RATE: float = float("inf")


# ======================================================================
# 异常
# ======================================================================


class RateLimitError(Exception):
    """速率限制相关错误的基类。"""


class RateLimitExceededError(RateLimitError):
    """速率超限 (令牌不足且不等待)。"""

    def __init__(self, requested: int, available: float, burst: int) -> None:
        self.requested = requested
        self.available = available
        self.burst = burst
        super().__init__(
            f"rate: Wait(n={requested}) exceeds limiter's burst {burst} "
            f"(available={available:.2f})"
        )


class InvalidConfigError(RateLimitError):
    """速率限制配置无效。"""


# ======================================================================
# 枚举
# ======================================================================


class DelayMode(IntEnum):
    """延迟模式 (逆向自 strings: "[setdelay] is unavailable with delay mode: none")。"""

    NONE = 0       # 无延迟
    CONSTANT = 1   # 固定延迟
    ADAPTIVE = 2   # 自适应延迟
    THRESHOLD = 3  # 阈值延迟


# ======================================================================
# 数据类 - RateLimitConfig
# ======================================================================


@dataclass
class RateLimitConfig:
    """速率限制配置 (RateLimitConfig)。

    逆向自 NexusEgo v1.6.5 的速率限制配置字段:
        - Burst:           突发数量上限 (桶容量)
        - BurstDensity:    突发密度 (每秒令牌生成速率)
        - BurstDuration:   突发持续时间 (秒)

    三者关系: Burst = BurstDensity * BurstDuration

    Attributes:
        burst: 突发数量上限 (令牌桶容量)。
        burst_density: 突发密度 (每秒生成的令牌数)。
        burst_duration: 突发持续时间 (秒)。
        name: 配置名称 (用于日志)。
    """

    burst: int = RATE_LIMIT_BURST
    burst_density: float = RATE_LIMIT_BURST_DENSITY
    burst_duration: float = RATE_LIMIT_BURST_DURATION
    name: str = "default"

    def __post_init__(self) -> None:
        """校验配置。"""
        if self.burst <= 0:
            raise InvalidConfigError(f"burst must be positive, got {self.burst}")
        if self.burst_density <= 0:
            raise InvalidConfigError(
                f"burst_density must be positive, got {self.burst_density}"
            )
        if self.burst_duration <= 0:
            raise InvalidConfigError(
                f"burst_duration must be positive, got {self.burst_duration}"
            )
        # 调整 burst 以满足 Burst ≈ Density * Duration
        expected_burst = int(self.burst_density * self.burst_duration)
        if self.burst < expected_burst:
            logger.warning(
                "RateLimitConfig(%s): burst=%d < density*duration=%d, adjusting",
                self.name, self.burst, expected_burst,
            )
            self.burst = expected_burst

    @property
    def rate_per_second(self) -> float:
        """每秒令牌生成速率 (= burst_density)。"""
        return self.burst_density

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RateLimitConfig":
        """从字典构建。"""
        return cls(
            burst=int(data.get("burst", RATE_LIMIT_BURST)),
            burst_density=float(data.get("burst_density", RATE_LIMIT_BURST_DENSITY)),
            burst_duration=float(
                data.get("burst_duration", RATE_LIMIT_BURST_DURATION)
            ),
            name=str(data.get("name", "default")),
        )


# ======================================================================
# 数据类 - RateLimitResult
# ======================================================================


@dataclass
class RateLimitResult:
    """速率限制检查结果。

    Attributes:
        allowed: 是否允许请求通过。
        tokens_remaining: 剩余令牌数。
        wait_seconds: 需要等待的秒数 (如果 allowed=False 且 blocking=False)。
        requested: 请求的令牌数。
        timestamp: 检查时间戳。
    """

    allowed: bool
    tokens_remaining: float
    wait_seconds: float
    requested: int
    timestamp: float = field(default_factory=time.time)

    @property
    def rejected(self) -> bool:
        """是否被拒绝。"""
        return not self.allowed


# ======================================================================
# RateLimiter - 令牌桶速率限制器
# ======================================================================


class RateLimiter:
    """令牌桶速率限制器 (对应 Go golang.org/x/time/rate.Limiter)。

    逆向自 NexusEgo v1.6.5 使用的 golang.org/x/time/rate 库。

    Go 方法对应关系:
        - rate.NewLimiter(rate, burst)       -> RateLimiter(config)
        - (*Limiter).Allow()                 -> allow()
        - (*Limiter).AllowN(now, n)          -> allow_n(n)
        - (*Limiter).Wait(ctx)               -> wait()
        - (*Limiter).WaitN(ctx, n)           -> wait_n(n)
        - (*Limiter).Reserve()               -> reserve()
        - (*Limiter).ReserveN(now, n)        -> reserve_n(n)
        - (*Limiter).SetBurst(burst)         -> set_burst(burst)
        - (*Limiter).SetBurstAt(now, burst)  -> set_burst_at(burst)
        - (*Limiter).Burst()                 -> burst
        - (*Limiter).Limit()                 -> limit()
        - (*Limiter).SetLimit(limit)         -> set_limit(limit)
        - (*Limiter).SetLimitAt(now, limit)  -> set_limit_at(limit)
        - (*Limiter).Tokens()                -> tokens()

    令牌桶算法:
        1. 桶初始满 (burst 个令牌)
        2. 每秒生成 burst_density 个令牌 (不超过 burst)
        3. 请求消耗 n 个令牌
        4. 令牌不足时: 阻塞等待 or 拒绝

    用法::

        limiter = RateLimiter(RateLimitConfig(burst=100, burst_density=20))
        if limiter.allow():
            do_work()
        else:
            # 等待令牌
            limiter.wait()
            do_work()
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        *,
        rate: float | None = None,
        burst: int | None = None,
    ) -> None:
        """初始化速率限制器。

        Args:
            config: 速率限制配置。如果为 None, 使用默认配置。
            rate: 每秒令牌数 (覆盖 config.burst_density)。
            burst: 桶容量 (覆盖 config.burst)。

        优先级: rate/burst 参数 > config。
        """
        if config is None:
            config = RateLimitConfig()

        if rate is not None:
            config.burst_density = float(rate)
        if burst is not None:
            config.burst = int(burst)

        self._config: RateLimitConfig = config
        self._lock = threading.Lock()
        self._tokens: float = float(config.burst)
        self._last_update: float = time.monotonic()
        self._total_allowed: int = 0
        self._total_rejected: int = 0
        self._total_waited: float = 0.0

        logger.debug(
            "RateLimiter init: name=%s burst=%d rate=%.2f",
            config.name, config.burst, config.burst_density,
        )

    # ---- 基本属性 ----

    @property
    def config(self) -> RateLimitConfig:
        """获取配置。"""
        return self._config

    def burst(self) -> int:
        """获取桶容量 (对应 (*Limiter).Burst())。"""
        return self._config.burst

    def limit(self) -> float:
        """获取速率限制 (每秒令牌数, 对应 (*Limiter).Limit())。"""
        return self._config.burst_density

    def tokens(self) -> float:
        """获取当前可用令牌数 (对应 (*Limiter).Tokens())。"""
        with self._lock:
            self._refill()
            return self._tokens

    def stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        with self._lock:
            return {
                "name": self._config.name,
                "burst": self._config.burst,
                "rate": self._config.burst_density,
                "tokens": self._tokens,
                "total_allowed": self._total_allowed,
                "total_rejected": self._total_rejected,
                "total_waited_seconds": self._total_waited,
            }

    # ---- 内部: 令牌桶核心 ----

    def _refill(self, now: float | None = None) -> None:
        """补充令牌 (必须在锁内调用)。"""
        if now is None:
            now = time.monotonic()
        elapsed = now - self._last_update
        if elapsed <= 0:
            return
        # 按速率补充令牌, 不超过 burst
        new_tokens = elapsed * self._config.burst_density
        self._tokens = min(self._config.burst, self._tokens + new_tokens)
        self._last_update = now

    def _consume(self, n: int) -> float:
        """消耗 n 个令牌, 返回需要等待的时间 (秒)。

        Returns:
            0.0 表示立即通过; 正数表示需要等待的秒数。
        """
        if n <= 0:
            return 0.0
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return 0.0
        # 令牌不足, 计算等待时间
        deficit = n - self._tokens
        wait = deficit / self._config.burst_density
        # 不立即扣除, 等待后才扣除 (由调用方处理)
        return wait

    # ---- Allow 系列 (非阻塞) ----

    def allow(self) -> bool:
        """检查是否允许 1 个请求通过 (非阻塞)。

        对应 Go: (*Limiter).Allow()

        Returns:
            True 如果允许通过。
        """
        return self.allow_n(1)

    def allow_n(self, n: int) -> bool:
        """检查是否允许 n 个请求通过 (非阻塞)。

        对应 Go: (*Limiter).AllowN(now, n)

        Args:
            n: 请求的令牌数。

        Returns:
            True 如果允许通过。
        """
        with self._lock:
            wait = self._consume(n)
            if wait <= 0:
                self._total_allowed += n
                return True
            self._total_rejected += n
            return False

    def check(self, n: int = 1) -> RateLimitResult:
        """检查速率限制 (不消耗令牌)。

        Args:
            n: 请求的令牌数。

        Returns:
            RateLimitResult。
        """
        with self._lock:
            self._refill()
            if self._tokens >= n:
                return RateLimitResult(
                    allowed=True,
                    tokens_remaining=self._tokens,
                    wait_seconds=0.0,
                    requested=n,
                )
            deficit = n - self._tokens
            wait = deficit / self._config.burst_density
            return RateLimitResult(
                allowed=False,
                tokens_remaining=self._tokens,
                wait_seconds=wait,
                requested=n,
            )

    # ---- Wait 系列 (阻塞) ----

    def wait(self, timeout: float | None = None) -> bool:
        """等待 1 个令牌 (阻塞)。

        对应 Go: (*Limiter).Wait(ctx)

        Args:
            timeout: 最大等待时间 (秒)。None 表示无限等待。

        Returns:
            True 如果获得令牌; False 如果超时。
        """
        return self.wait_n(1, timeout)

    def wait_n(self, n: int, timeout: float | None = None) -> bool:
        """等待 n 个令牌 (阻塞)。

        对应 Go: (*Limiter).WaitN(ctx, n)

        Args:
            n: 请求的令牌数。
            timeout: 最大等待时间 (秒)。None 表示无限等待。

        Returns:
            True 如果获得令牌; False 如果超时。

        Raises:
            RateLimitExceededError: n 超过 burst 容量。
        """
        if n > self._config.burst:
            raise RateLimitExceededError(n, self._tokens, self._config.burst)

        start = time.monotonic()
        while True:
            with self._lock:
                wait = self._consume(n)
                if wait <= 0:
                    self._total_allowed += n
                    return True

            if timeout is not None:
                elapsed = time.monotonic() - start
                if elapsed + wait > timeout:
                    with self._lock:
                        self._total_rejected += n
                    return False

            logger.debug(
                "RateLimiter.wait_n: waiting %.3fs for %d tokens (name=%s)",
                wait, n, self._config.name,
            )
            self._total_waited += wait
            time.sleep(min(wait, 0.1))  # 分段睡眠以响应中断

    # ---- Reserve 系列 ----

    def reserve(self) -> float:
        """预留 1 个令牌, 返回需要等待的时间。

        对应 Go: (*Limiter).Reserve()

        Returns:
            需要等待的秒数 (0.0 表示立即可用)。
        """
        return self.reserve_n(1)

    def reserve_n(self, n: int) -> float:
        """预留 n 个令牌, 返回需要等待的时间。

        对应 Go: (*Limiter).ReserveN(now, n)

        预留会立即扣除令牌 (包括未来生成的), 调用者需自行 sleep。

        Args:
            n: 请求的令牌数。

        Returns:
            需要等待的秒数 (0.0 表示立即可用)。
        """
        with self._lock:
            self._refill()
            if self._tokens >= n:
                self._tokens -= n
                return 0.0
            deficit = n - self._tokens
            wait = deficit / self._config.burst_density
            self._tokens = 0.0
            # 预留未来的令牌
            self._last_update -= deficit / self._config.burst_density
            return wait

    # ---- 动态调整 ----

    def set_burst(self, burst: int) -> None:
        """设置桶容量 (对应 (*Limiter).SetBurst(burst))。

        Args:
            burst: 新的桶容量。
        """
        with self._lock:
            old = self._config.burst
            self._config.burst = max(1, burst)
            self._tokens = min(self._tokens, float(self._config.burst))
            logger.debug(
                "RateLimiter.set_burst: %d -> %d (name=%s)",
                old, self._config.burst, self._config.name,
            )

    def set_burst_at(self, burst: int, now: float | None = None) -> None:
        """在指定时刻设置桶容量 (对应 (*Limiter).SetBurstAt(now, burst))。

        Args:
            burst: 新的桶容量。
            now: 指定时刻 (monotonic)。None 表示当前。
        """
        with self._lock:
            self._refill(now)
            self._config.burst = max(1, burst)
            self._tokens = min(self._tokens, float(self._config.burst))

    def set_limit(self, rate: float) -> None:
        """设置速率 (对应 (*Limiter).SetLimit(limit))。

        Args:
            rate: 新的每秒令牌数。
        """
        with self._lock:
            old = self._config.burst_density
            self._config.burst_density = max(0.0, rate)
            logger.debug(
                "RateLimiter.set_limit: %.2f -> %.2f (name=%s)",
                old, self._config.burst_density, self._config.name,
            )

    def set_limit_at(self, rate: float, now: float | None = None) -> None:
        """在指定时刻设置速率 (对应 (*Limiter).SetLimitAt(now, limit))。"""
        with self._lock:
            self._refill(now)
            self._config.burst_density = max(0.0, rate)

    def reset(self) -> None:
        """重置令牌桶 (令牌恢复满)。"""
        with self._lock:
            self._tokens = float(self._config.burst)
            self._last_update = time.monotonic()
            self._total_allowed = 0
            self._total_rejected = 0
            self._total_waited = 0.0
            logger.debug("RateLimiter.reset (name=%s)", self._config.name)


# ======================================================================
# MoveItemRateLimiter - 移动物品速率限制
# ======================================================================


class MoveItemRateLimiter:
    """移动物品速率限制器 (MoveItemRateLimiter)。

    逆向自 NexusEgo v1.6.5 的容器操作速率限制:
        - MoveItemSpeed:    移动物品速度 (每次操作的 tick 数)
        - TransferCooldown: 容器转移冷却 (ticks)
        - TargetCooldownLength: 目标冷却时长 (ticks, mapstructure:"target_cooldown_length")

    这些字段控制客户端移动物品的速率, 避免触发服务器检测。

    Attributes:
        move_item_speed: 移动物品速度 (每次操作间隔 ticks)。
        transfer_cooldown: 容器转移冷却 (ticks)。
        target_cooldown_length: 目标冷却时长 (ticks)。
    """

    def __init__(
        self,
        move_item_speed: int = DEFAULT_MOVE_ITEM_SPEED,
        transfer_cooldown: int = DEFAULT_TRANSFER_COOLDOWN,
        target_cooldown_length: int = DEFAULT_TARGET_COOLDOWN_LENGTH,
    ) -> None:
        self.move_item_speed: int = max(1, move_item_speed)
        self.transfer_cooldown: int = max(0, transfer_cooldown)
        self.target_cooldown_length: int = max(0, target_cooldown_length)
        self._last_move_time: float = 0.0
        self._lock = threading.Lock()
        logger.debug(
            "MoveItemRateLimiter init: speed=%d cooldown=%d target=%d",
            self.move_item_speed, self.transfer_cooldown,
            self.target_cooldown_length,
        )

    def can_move(self, now: float | None = None) -> bool:
        """检查是否可以移动物品。

        Args:
            now: 当前时间戳。None 表示 time.time()。

        Returns:
            True 如果可以移动。
        """
        if now is None:
            now = time.time()
        with self._lock:
            interval = self.move_item_speed / 20.0  # tick -> 秒 (20 tps)
            return (now - self._last_move_time) >= interval

    def record_move(self, now: float | None = None) -> None:
        """记录一次移动物品操作。

        Args:
            now: 当前时间戳。
        """
        if now is None:
            now = time.time()
        with self._lock:
            self._last_move_time = now

    def wait_for_move(self, timeout: float | None = None) -> bool:
        """等待直到可以移动物品。

        Args:
            timeout: 最大等待时间 (秒)。

        Returns:
            True 如果可以移动; False 如果超时。
        """
        start = time.monotonic()
        while True:
            now = time.time()
            if self.can_move(now):
                self.record_move(now)
                return True
            if timeout is not None and (time.monotonic() - start) > timeout:
                return False
            interval = self.move_item_speed / 20.0
            time.sleep(min(interval, 0.05))

    def get_transfer_cooldown_seconds(self) -> float:
        """获取容器转移冷却时间 (秒)。"""
        return self.transfer_cooldown / 20.0

    def get_target_cooldown_seconds(self) -> float:
        """获取目标冷却时间 (秒)。"""
        return self.target_cooldown_length / 20.0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "MoveItemSpeed": self.move_item_speed,
            "TransferCooldown": self.transfer_cooldown,
            "target_cooldown_length": self.target_cooldown_length,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MoveItemRateLimiter":
        """从字典构建。"""
        return cls(
            move_item_speed=int(data.get("MoveItemSpeed", DEFAULT_MOVE_ITEM_SPEED)),
            transfer_cooldown=int(data.get("TransferCooldown", DEFAULT_TRANSFER_COOLDOWN)),
            target_cooldown_length=int(
                data.get("target_cooldown_length", DEFAULT_TARGET_COOLDOWN_LENGTH)
            ),
        )


# ======================================================================
# ClientThrottle - 客户端节流
# ======================================================================


class ClientThrottle:
    """客户端节流控制器 (ClientThrottle)。

    逆向自 NexusEgo v1.6.5 的客户端节流机制:
        - ClientThrottle:          是否启用节流
        - ClientThrottleScalar:    节流标量 (倍率)
        - ClientThrottleThreshold: 节流阈值 (触发节流的操作数)

    当短时间内操作数超过阈值时, 按 scalar 倍率增加延迟。

    Attributes:
        enabled: 是否启用节流。
        threshold: 节流阈值。
        scalar: 节流标量。
    """

    def __init__(
        self,
        enabled: bool = DEFAULT_CLIENT_THROTTLE_ENABLED,
        threshold: int = DEFAULT_CLIENT_THROTTLE_THRESHOLD,
        scalar: float = DEFAULT_CLIENT_THROTTLE_SCALAR,
    ) -> None:
        self.enabled: bool = enabled
        self.threshold: int = max(1, threshold)
        self.scalar: float = max(1.0, scalar)
        self._operation_count: int = 0
        self._window_start: float = time.monotonic()
        self._lock = threading.Lock()
        logger.debug(
            "ClientThrottle init: enabled=%s threshold=%d scalar=%.2f",
            self.enabled, self.threshold, self.scalar,
        )

    def record_operation(self) -> float:
        """记录一次操作, 返回建议的延迟倍率。

        Returns:
            延迟倍率 (1.0 = 无节流; scalar = 节流中)。
        """
        if not self.enabled:
            return 1.0
        with self._lock:
            now = time.monotonic()
            # 1 秒窗口
            if now - self._window_start >= 1.0:
                self._operation_count = 0
                self._window_start = now
            self._operation_count += 1
            if self._operation_count > self.threshold:
                logger.debug(
                    "ClientThrottle: throttling (count=%d > threshold=%d)",
                    self._operation_count, self.threshold,
                )
                return self.scalar
            return 1.0

    def get_current_rate(self) -> float:
        """获取当前操作速率 (ops/sec)。"""
        with self._lock:
            elapsed = time.monotonic() - self._window_start
            if elapsed <= 0:
                return 0.0
            return self._operation_count / elapsed

    def reset(self) -> None:
        """重置节流状态。"""
        with self._lock:
            self._operation_count = 0
            self._window_start = time.monotonic()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "ClientThrottle": self.enabled,
            "ClientThrottleThreshold": self.threshold,
            "ClientThrottleScalar": self.scalar,
        }


# ======================================================================
# DelayController - 延迟控制器
# ======================================================================


class DelayController:
    """延迟控制器 (DelayController)。

    逆向自 NexusEgo v1.6.5 的命令延迟系统:
        - DelayThreshold:   延迟阈值 (ticks)
        - DelayMode:        延迟模式 (none/constant/adaptive/threshold)
        - SetDelay:         设置延迟

    逆向自 strings:
        - "[setdelay] is unavailable with delay mode: none"
        - "[Task %d] - Delay set: %d"
        - "Delay automatically set to: %d"

    Attributes:
        mode: 延迟模式。
        threshold: 延迟阈值 (ticks)。
        current_delay: 当前延迟 (ticks)。
    """

    def __init__(
        self,
        mode: DelayMode = DelayMode.ADAPTIVE,
        threshold: int = DEFAULT_DELAY_THRESHOLD,
    ) -> None:
        self.mode: DelayMode = mode
        self.threshold: int = max(0, threshold)
        self._current_delay: int = 0
        self._lock = threading.Lock()
        logger.debug(
            "DelayController init: mode=%s threshold=%d",
            mode.name, self.threshold,
        )

    @property
    def current_delay(self) -> int:
        """当前延迟 (ticks)。"""
        return self._current_delay

    def set_delay(self, delay: int, task_id: int | None = None) -> None:
        """设置延迟。

        逆向自 strings: "[Task %d] - Delay set: %d"

        Args:
            delay: 延迟值 (ticks)。
            task_id: 任务 ID (用于日志)。
        """
        if self.mode == DelayMode.NONE:
            logger.warning(
                "[setdelay] is unavailable with delay mode: none"
            )
            return
        with self._lock:
            self._current_delay = max(0, delay)
            if task_id is not None:
                logger.info("[Task %d] - Delay set: %d", task_id, self._current_delay)
            else:
                logger.info("Delay set: %d", self._current_delay)

    def auto_set_delay(self, operation_count: int) -> int:
        """自动设置延迟 (基于操作数)。

        逆向自 strings: "Delay automatically set to: %d"

        Args:
            operation_count: 当前操作数。

        Returns:
            设置的延迟值 (ticks)。
        """
        if self.mode == DelayMode.NONE:
            return 0
        with self._lock:
            if self.mode == DelayMode.CONSTANT:
                self._current_delay = self.threshold
            elif self.mode == DelayMode.THRESHOLD:
                self._current_delay = self.threshold if operation_count > self.threshold else 0
            else:  # ADAPTIVE
                # 自适应: 操作数越多, 延迟越大
                ratio = max(1, operation_count // max(1, self.threshold))
                self._current_delay = min(self.threshold * ratio, self.threshold * 4)
            logger.info(
                "Delay automatically set to: %d (mode=%s, ops=%d)",
                self._current_delay, self.mode.name, operation_count,
            )
            return self._current_delay

    def get_delay_seconds(self) -> float:
        """获取当前延迟 (秒)。"""
        return self._current_delay / 20.0  # 20 tps

    def sleep_delay(self) -> None:
        """按当前延迟休眠。"""
        delay_sec = self.get_delay_seconds()
        if delay_sec > 0:
            time.sleep(delay_sec)


# ======================================================================
# 全局实例与便捷函数
# ======================================================================

_global_rate_limiter: RateLimiter | None = None
_global_rate_limiter_lock = threading.Lock()


def _get_global_rate_limiter() -> RateLimiter:
    """获取全局 RateLimiter 单例。"""
    global _global_rate_limiter
    with _global_rate_limiter_lock:
        if _global_rate_limiter is None:
            _global_rate_limiter = RateLimiter(RateLimitConfig(name="global"))
        return _global_rate_limiter


def check_rate_limit(n: int = 1, *, blocking: bool = False, timeout: float | None = None) -> bool:
    """检查全局速率限制。

    Args:
        n: 请求的令牌数。
        blocking: 是否阻塞等待。
        timeout: 阻塞模式下的超时时间 (秒)。

    Returns:
        True 如果允许通过。
    """
    limiter = _get_global_rate_limiter()
    if blocking:
        return limiter.wait_n(n, timeout)
    return limiter.allow_n(n)


def reset_rate_limit() -> None:
    """重置全局速率限制器。"""
    limiter = _get_global_rate_limiter()
    limiter.reset()
    logger.info("reset_rate_limit: global rate limiter reset")


def get_rate_limit_stats() -> dict[str, Any]:
    """获取全局速率限制器统计信息。"""
    return _get_global_rate_limiter().stats()


def create_rate_limiter(
    burst: int = RATE_LIMIT_BURST,
    burst_density: float = RATE_LIMIT_BURST_DENSITY,
    burst_duration: float = RATE_LIMIT_BURST_DURATION,
    name: str = "custom",
) -> RateLimiter:
    """创建速率限制器 (便捷工厂函数)。

    Args:
        burst: 突发数量上限。
        burst_density: 突发密度 (每秒令牌数)。
        burst_duration: 突发持续时间 (秒)。
        name: 配置名称。

    Returns:
        RateLimiter 实例。
    """
    config = RateLimitConfig(
        burst=burst,
        burst_density=burst_density,
        burst_duration=burst_duration,
        name=name,
    )
    return RateLimiter(config)


# ======================================================================
# __all__
# ======================================================================

__all__ = [
    # 常量
    "RATE_LIMIT_BURST", "RATE_LIMIT_BURST_DENSITY", "RATE_LIMIT_BURST_DURATION",
    "DEFAULT_MOVE_ITEM_SPEED", "DEFAULT_TRANSFER_COOLDOWN",
    "DEFAULT_TARGET_COOLDOWN_LENGTH", "DEFAULT_DELAY_THRESHOLD",
    "DEFAULT_CLIENT_THROTTLE_THRESHOLD", "DEFAULT_CLIENT_THROTTLE_SCALAR",
    "DEFAULT_CLIENT_THROTTLE_ENABLED", "INFINITE_RATE",
    # 异常
    "RateLimitError", "RateLimitExceededError", "InvalidConfigError",
    # 枚举
    "DelayMode",
    # 数据类
    "RateLimitConfig", "RateLimitResult",
    # 主类
    "RateLimiter", "MoveItemRateLimiter", "ClientThrottle", "DelayController",
    # 便捷函数
    "check_rate_limit", "reset_rate_limit", "get_rate_limit_stats",
    "create_rate_limiter",
]
