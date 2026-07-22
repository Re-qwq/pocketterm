"""rate_limiter - PlaceRateLimiter 速率限制。

逆向自 NovaBuilder 的速率限制层, 来源:
    - /workspace/novuilder_reverse/rate_full.txt
    - /workspace/novuilder_reverse/rate_limit.txt
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/builder.go

速率限制实现 (逆向自 rate.Limiter, golang.org/x/time/rate):
    使用令牌桶算法 (Token Bucket):
        - 桶容量: burst (允许突发)
        - 补充速率: rate_per_second (每秒补充的令牌数)
        - 获取令牌: 每次 BlockPlace 消耗一个令牌
        - 等待: 如果令牌不足, 阻塞等待

服务器限制 (逆向自 rate_limit.txt):
    - 网易中国版: 每秒 30 个方块 (默认)
    - 官方服务器: 每秒 60 个方块
    - 反作弊服务器: 每秒 10-20 个方块 (推荐)

速率限制模式:
    - BLOCKING: 阻塞等待令牌 (默认)
    - NON_BLOCKING: 不阻塞, 返回 False
    - ADAPTIVE: 自适应 (根据服务器响应调整)

配置值 (逆向自 strings):
    "rate.Limiter"
    "rate_per_second"
    "burst_size"
    "PlaceRateLimiter"
    "RateLimitConfig"
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger("pocketterm.protocol.import_algorithms.rate_limiter")


# -------------------------------------------------------------------- #
# 常量 (逆向自 rate_limit.txt)
# -------------------------------------------------------------------- #

#: 默认速率 (网易服务器, 30 blocks/s)
DEFAULT_RATE_PER_SECOND: int = 30

#: 默认桶容量 (允许突发 30 个)
DEFAULT_BURST_SIZE: int = 30

#: 网易中国版速率 (保守值)
NETEASE_RATE_LIMIT: int = 30

#: 官方服务器速率 (默认值)
OFFICIAL_RATE_LIMIT: int = 60

#: 反作弊服务器速率 (保守值)
ANTICHEAT_RATE_LIMIT: int = 15

#: 最小速率 (10 blocks/s)
MIN_RATE_LIMIT: int = 1

#: 最大速率 (1000 blocks/s)
MAX_RATE_LIMIT: int = 1000


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class RateLimitMode(Enum):
    """速率限制模式 (逆向自 rate_limit.txt)。"""
    BLOCKING = auto()       # 阻塞等待
    NON_BLOCKING = auto()   # 非阻塞, 返回 False
    ADAPTIVE = auto()       # 自适应 (根据响应调整)


class RateLimitResult(Enum):
    """速率限制结果。"""
    ACQUIRED = auto()       # 获取成功
    WAITING = auto()        # 等待中
    REJECTED = auto()       # 被拒绝 (非阻塞模式)
    TIMEOUT = auto()       # 超时


# -------------------------------------------------------------------- #
# 配置
# -------------------------------------------------------------------- #


@dataclass
class RateLimitConfig:
    """速率限制配置 (逆向自 rate_limit.txt)。

    Attributes:
        rate_per_second: 每秒令牌数 (1-1000)
        burst_size: 桶容量 (允许突发)
        mode: 限制模式
        max_wait_time: 最大等待时间 (秒, 超时返回 TIMEOUT)
        adaptive_min_rate: 自适应模式下的最小速率
        adaptive_max_rate: 自适应模式下的最大速率
        enable_jitter: 是否启用抖动 (避免规律性)
        jitter_range_ms: 抖动范围 (毫秒)
    """
    rate_per_second: int = DEFAULT_RATE_PER_SECOND
    burst_size: int = DEFAULT_BURST_SIZE
    mode: RateLimitMode = RateLimitMode.BLOCKING
    max_wait_time: float = 60.0
    adaptive_min_rate: int = 10
    adaptive_max_rate: int = 60
    enable_jitter: bool = False
    jitter_range_ms: int = 50

    def __post_init__(self) -> None:
        # 验证配置
        if self.rate_per_second < MIN_RATE_LIMIT:
            self.rate_per_second = MIN_RATE_LIMIT
        if self.rate_per_second > MAX_RATE_LIMIT:
            self.rate_per_second = MAX_RATE_LIMIT
        if self.burst_size < 1:
            self.burst_size = 1
        if self.burst_size > self.rate_per_second * 2:
            self.burst_size = self.rate_per_second * 2

    @property
    def interval_ms(self) -> float:
        """令牌补充间隔 (毫秒)。"""
        return 1000.0 / self.rate_per_second if self.rate_per_second > 0 else float("inf")

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_per_second": self.rate_per_second,
            "burst_size": self.burst_size,
            "mode": self.mode.name,
            "max_wait_time": self.max_wait_time,
            "adaptive_min_rate": self.adaptive_min_rate,
            "adaptive_max_rate": self.adaptive_max_rate,
            "enable_jitter": self.enable_jitter,
            "jitter_range_ms": self.jitter_range_ms,
            "interval_ms": self.interval_ms,
        }


# -------------------------------------------------------------------- #
# 令牌桶 (逆向自 golang.org/x/time/rate)
# -------------------------------------------------------------------- #


class TokenBucket:
    """令牌桶 (逆向自 golang.org/x/time/rate.Limiter)。

    Go 源码 (简化):
        type Limiter struct {
            mu     sync.Mutex
            limit  rate.Limit  // 每秒令牌数
            burst  int         // 桶容量
            tokens float64     // 当前令牌数
            last   time.Time   // 上次补充时间
        }

        func (lim *Limiter) Allow() bool {
            return lim.AllowN(time.Now(), 1)
        }

        func (lim *Limiter) AllowN(now time.Time, n int) bool {
            return lim.ReserveN(now, n).OK()
        }

        func (lim *Limiter) Wait(ctx context.Context) error {
            return lim.WaitN(ctx, 1)
        }
    """

    def __init__(
        self,
        rate_per_second: int = DEFAULT_RATE_PER_SECOND,
        burst_size: int = DEFAULT_BURST_SIZE,
    ) -> None:
        """初始化令牌桶。

        Args:
            rate_per_second: 每秒令牌数
            burst_size: 桶容量 (突发上限)
        """
        self.rate: float = float(rate_per_second)
        self.burst: int = burst_size
        self.tokens: float = float(burst_size)  # 初始填满
        self.last_refill: float = time.time()
        self._lock: threading.Lock = threading.Lock()
        self.logger = logging.getLogger("pocketterm.protocol.import_algorithms.rate_limiter.bucket")

    def acquire(self, tokens: int = 1) -> bool:
        """尝试获取令牌 (非阻塞)。

        Args:
            tokens: 需要的令牌数

        Returns:
            True 如果获取成功, False 如果令牌不足
        """
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def wait_for_tokens(
        self, tokens: int = 1, timeout: float = 60.0
    ) -> RateLimitResult:
        """等待并获取令牌 (阻塞)。

        Args:
            tokens: 需要的令牌数
            timeout: 最大等待时间 (秒)

        Returns:
            RateLimitResult.ACQUIRED 如果成功
            RateLimitResult.TIMEOUT 如果超时
        """
        deadline = time.time() + timeout

        while time.time() < deadline:
            with self._lock:
                self._refill()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return RateLimitResult.ACQUIRED

                # 计算需要等待的时间
                needed = tokens - self.tokens
                wait_time = needed / self.rate if self.rate > 0 else float("inf")

            # 释放锁后等待
            sleep_time = min(wait_time, 0.1)
            if sleep_time > 0:
                time.sleep(sleep_time)

        return RateLimitResult.TIMEOUT

    def try_acquire_with_timeout(
        self, tokens: int = 1, timeout: float = 60.0
    ) -> RateLimitResult:
        """带超时的获取令牌。"""
        return self.wait_for_tokens(tokens, timeout)

    def _refill(self) -> None:
        """补充令牌。"""
        now = time.time()
        elapsed = now - self.last_refill
        if elapsed > 0 and self.rate > 0:
            self.tokens = min(float(self.burst), self.tokens + elapsed * self.rate)
            self.last_refill = now

    @property
    def available_tokens(self) -> float:
        """当前可用令牌数。"""
        with self._lock:
            self._refill()
            return self.tokens

    def reset(self) -> None:
        """重置令牌桶 (填满)。"""
        with self._lock:
            self.tokens = float(self.burst)
            self.last_refill = time.time()

    def update_rate(self, new_rate: int) -> None:
        """更新速率。"""
        with self._lock:
            self._refill()
            self.rate = float(new_rate)


# -------------------------------------------------------------------- #
# PlaceRateLimiter (逆向自 rate_full.txt)
# -------------------------------------------------------------------- #


class PlaceRateLimiter:
    """PlaceRateLimiter (逆向自 NovaBuilder 的速率限制器)。

    使用方式:
        limiter = PlaceRateLimiter(config=RateLimitConfig(rate_per_second=30))
        if limiter.acquire():
            # 放置方块
            place_block(...)
        else:
            # 等待或跳过
            limiter.wait()

        # 或阻塞模式:
        limiter.wait_and_acquire()
        place_block(...)
    """

    def __init__(self, config: Optional[RateLimitConfig] = None) -> None:
        """初始化速率限制器。

        Args:
            config: 配置 (None 使用默认)
        """
        self.logger = logging.getLogger("pocketterm.protocol.import_algorithms.rate_limiter.limiter")
        self.config = config if config else RateLimitConfig()
        self._bucket = TokenBucket(
            rate_per_second=self.config.rate_per_second,
            burst_size=self.config.burst_size,
        )
        self._total_acquired: int = 0
        self._total_rejected: int = 0
        self._total_waited: float = 0.0
        self._last_adaptation: float = time.time()
        self._consecutive_failures: int = 0

    def acquire(self) -> bool:
        """尝试获取一个令牌 (非阻塞)。

        Returns:
            True 如果获取成功
        """
        if self.config.enable_jitter:
            self._apply_jitter()

        success = self._bucket.acquire(1)
        if success:
            self._total_acquired += 1
            self._consecutive_failures = 0
        else:
            self._total_rejected += 1
            self._consecutive_failures += 1
        return success

    def wait_and_acquire(self) -> RateLimitResult:
        """等待并获取一个令牌 (阻塞)。

        Returns:
            RateLimitResult.ACQUIRED 如果成功
            RateLimitResult.TIMEOUT 如果超时
        """
        start_time = time.time()
        result = self._bucket.wait_for_tokens(1, self.config.max_wait_time)
        self._total_waited += time.time() - start_time

        if result == RateLimitResult.ACQUIRED:
            self._total_acquired += 1
            self._consecutive_failures = 0
        return result

    def acquire_n(self, tokens: int) -> bool:
        """尝试获取多个令牌 (非阻塞)。"""
        return self._bucket.acquire(tokens)

    def wait_and_acquire_n(
        self, tokens: int, timeout: Optional[float] = None
    ) -> RateLimitResult:
        """等待并获取多个令牌 (阻塞)。"""
        if timeout is None:
            timeout = self.config.max_wait_time
        return self._bucket.wait_for_tokens(tokens, timeout)

    def wait(self) -> None:
        """阻塞等待直到有令牌可用 (不消耗)。"""
        self._bucket.wait_for_tokens(0, self.config.max_wait_time)

    def reset(self) -> None:
        """重置速率限制器。"""
        self._bucket.reset()
        self._total_acquired = 0
        self._total_rejected = 0
        self._total_waited = 0.0
        self._consecutive_failures = 0
        self.logger.info("Rate limiter reset")

    def update_rate(self, new_rate: int) -> None:
        """更新速率 (自适应模式使用)。"""
        old_rate = self.config.rate_per_second
        self.config.rate_per_second = max(
            self.config.adaptive_min_rate,
            min(self.config.adaptive_max_rate, new_rate),
        )
        self._bucket.update_rate(self.config.rate_per_second)
        self.logger.info(
            "Rate updated: %d -> %d blocks/s", old_rate, self.config.rate_per_second
        )

    def adapt_rate(self, success_rate: float) -> None:
        """自适应调整速率 (基于成功率)。

        Args:
            success_rate: 最近的成功率 (0.0-1.0)
        """
        if self.config.mode != RateLimitMode.ADAPTIVE:
            return

        now = time.time()
        if now - self._last_adaptation < 5.0:  # 5 秒调整一次
            return
        self._last_adaptation = now

        old_rate = self.config.rate_per_second
        if success_rate > 0.95:
            # 成功率高, 提高速率
            new_rate = min(self.config.adaptive_max_rate, old_rate + 5)
        elif success_rate < 0.5:
            # 成功率低, 降低速率
            new_rate = max(self.config.adaptive_min_rate, old_rate - 10)
        else:
            # 成功率中等, 保持不变
            new_rate = old_rate

        if new_rate != old_rate:
            self.update_rate(new_rate)
            self.logger.info(
                "Adaptive rate adjustment: %.2f%% success -> %d -> %d blocks/s",
                success_rate * 100, old_rate, new_rate,
            )

    def _apply_jitter(self) -> None:
        """应用抖动 (避免规律性, 防被反作弊检测)。"""
        import random
        jitter = random.uniform(
            -self.config.jitter_range_ms / 1000,
            self.config.jitter_range_ms / 1000,
        )
        if jitter > 0:
            time.sleep(jitter)

    @property
    def stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        return {
            "total_acquired": self._total_acquired,
            "total_rejected": self._total_rejected,
            "total_waited_seconds": self._total_waited,
            "consecutive_failures": self._consecutive_failures,
            "current_rate": self.config.rate_per_second,
            "available_tokens": self._bucket.available_tokens,
            "burst_size": self.config.burst_size,
        }

    @property
    def available_tokens(self) -> float:
        """当前可用令牌数。"""
        return self._bucket.available_tokens

    @property
    def is_rate_limited(self) -> bool:
        """是否被速率限制 (令牌不足)。"""
        return self._bucket.available_tokens < 1.0


# -------------------------------------------------------------------- #
# 工厂函数
# -------------------------------------------------------------------- #


def create_rate_limiter(
    server_type: str = "auto",
    custom_rate: Optional[int] = None,
) -> PlaceRateLimiter:
    """根据服务器类型创建速率限制器。

    Args:
        server_type: 服务器类型 ("netease", "official", "anticheat", "auto")
        custom_rate: 自定义速率 (覆盖服务器类型默认值)

    Returns:
        PlaceRateLimiter 实例
    """
    if custom_rate is not None:
        rate = custom_rate
    elif server_type == "netease":
        rate = NETEASE_RATE_LIMIT
    elif server_type == "official":
        rate = OFFICIAL_RATE_LIMIT
    elif server_type == "anticheat":
        rate = ANTICHEAT_RATE_LIMIT
    else:
        rate = DEFAULT_RATE_PER_SECOND

    config = RateLimitConfig(
        rate_per_second=rate,
        burst_size=rate,
        mode=RateLimitMode.BLOCKING,
    )
    return PlaceRateLimiter(config)
