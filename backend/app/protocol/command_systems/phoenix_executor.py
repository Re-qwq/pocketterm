"""phoenix_executor - PhoenixBuilder 执行器。

逆向自 PhoenixBuilder 的 PhoenixExecutor, 来源:
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/builder.go
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/bdump.go

PhoenixExecutor 职责:
    1. 接收 PlacePlan
    2. 将每个 PlacePlanItem 转换为协议操作 (ItemStackRequest / UpdateBlock)
    3. 通过 GameInterface 发送数据包
    4. 应用速率限制 (PlaceRateLimiter)
    5. 跟踪执行状态 (成功/失败/重试)
    6. 输出 ExecutionResult

执行流程 (逆向自 builder.go Build):
    1. 验证 PlacePlan
    2. 初始化速率限制器
    3. 发送开始广播 (多人游戏时)
    4. 按 Y 升序遍历每个操作
    5. 对每个方块:
        a. 等待速率令牌
        b. 计算运行时方块 ID
        c. 发送 UpdateBlock 数据包
        d. 如有 NBT, 发送 BlockEntityData 数据包
        e. 如有 ChestSlots, 发送 ItemStackRequest (打开容器 + 填充)
        f. 记录执行状态
    6. 发送结束广播
    7. 返回 ExecutionResult
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Iterator, Optional

from .phoenix_planner import PlacePlan, PlacePlanItem, PlaceOperationType

logger = logging.getLogger("pocketterm.protocol.command_systems.phoenix_executor")


# -------------------------------------------------------------------- #
# 常量与枚举
# -------------------------------------------------------------------- #


class ExecutionStatus(Enum):
    """执行状态 (逆向自 builder.go)。"""
    PENDING = auto()
    RUNNING = auto()
    SUCCESS = auto()
    PARTIAL = auto()
    FAILED = auto()
    CANCELLED = auto()
    TIMEOUT = auto()


class FailureReason(Enum):
    """失败原因。"""
    NONE = auto()
    RATE_LIMITED = auto()
    CONNECTION_LOST = auto()
    INVALID_BLOCK = auto()
    NBT_TOO_LARGE = auto()
    CONTAINER_FULL = auto()
    PERMISSION_DENIED = auto()
    UNKNOWN = auto()


#: 默认速率限制 (每秒 30 块)
DEFAULT_RATE_LIMIT: int = 30

#: 默认超时 (60 秒)
DEFAULT_TIMEOUT: float = 60.0

#: 重试次数
DEFAULT_MAX_RETRIES: int = 3

#: 重试间隔 (秒)
DEFAULT_RETRY_INTERVAL: float = 0.5

#: 广播消息 (逆向自 strings)
BROADCAST_START: str = "[PhoenixBuilder] Building started."
BROADCAST_END: str = "[PhoenixBuilder] Building completed."
BROADCAST_FAILED: str = "[PhoenixBuilder] Building failed: %s"

#: NBT 最大字节限制 (协议限制)
MAX_NBT_SIZE: int = 1024 * 1024  # 1MB

#: 容器打开超时 (秒)
CONTAINER_OPEN_TIMEOUT: float = 5.0


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class PhoenixExecutorConfig:
    """PhoenixExecutor 配置 (逆向自 builder.go Config)。

    Attributes:
        rate_limit_per_second: 每秒方块数限制
        timeout: 总超时 (秒)
        max_retries: 最大重试次数
        retry_interval: 重试间隔 (秒)
        broadcast: 是否广播进度
        skip_air: 是否跳过空气方块
        auto_reconnect: 是否自动重连
        verify_placement: 是否验证方块已放置
        use_structure_mode: 是否使用 STRUCTURE 模式 (替代 REPLACEITEM)
    """
    rate_limit_per_second: int = DEFAULT_RATE_LIMIT
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_interval: float = DEFAULT_RETRY_INTERVAL
    broadcast: bool = True
    skip_air: bool = True
    auto_reconnect: bool = True
    verify_placement: bool = False
    use_structure_mode: bool = True


@dataclass
class ExecutionStats:
    """执行统计 (逆向自 builder.go ExecutionStats)。"""
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    retried: int = 0
    rate_limited_count: int = 0
    elapsed_time: float = 0.0
    blocks_per_second: float = 0.0
    failed_blocks: list[tuple[tuple[int, int, int], str]] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """成功率。"""
        if self.total == 0:
            return 0.0
        return self.succeeded / self.total

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "retried": self.retried,
            "rate_limited": self.rate_limited_count,
            "elapsed_time": self.elapsed_time,
            "blocks_per_second": self.blocks_per_second,
            "success_rate": self.success_rate,
            "failed_blocks": [
                {"position": pos, "reason": reason}
                for pos, reason in self.failed_blocks
            ],
        }


@dataclass
class ExecutionResult:
    """执行结果。"""
    status: ExecutionStatus = ExecutionStatus.PENDING
    stats: ExecutionStats = field(default_factory=ExecutionStats)
    error: Optional[str] = None
    failure_reason: FailureReason = FailureReason.NONE
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def elapsed_time(self) -> float:
        """总耗时 (秒)。"""
        if self.started_at == 0.0 or self.finished_at == 0.0:
            return 0.0
        return self.finished_at - self.started_at

    @property
    def is_success(self) -> bool:
        return self.status == ExecutionStatus.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.name,
            "stats": self.stats.to_dict(),
            "error": self.error,
            "failure_reason": self.failure_reason.name,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_time": self.elapsed_time,
        }


# -------------------------------------------------------------------- #
# PhoenixExecutor
# -------------------------------------------------------------------- #


class PhoenixExecutor:
    """PhoenixBuilder 执行器 (逆向自 phoenixbuilder/builder/builder.go)。

    使用方式:
        executor = PhoenixExecutor(game_interface=interface)
        result = executor.execute(plan)
        if result.is_success:
            print(f"Success! {result.stats.succeeded} blocks placed")
    """

    def __init__(
        self,
        game_interface: Optional[Any] = None,
        config: Optional[PhoenixExecutorConfig] = None,
    ) -> None:
        """初始化执行器。

        Args:
            game_interface: 游戏接口 (GameInterface 实例, 可为 None 用于模拟)
            config: 执行器配置
        """
        self.logger = logging.getLogger("pocketterm.protocol.command_systems.phoenix_executor")
        self.game_interface = game_interface
        self.config = config if config else PhoenixExecutorConfig()
        self._cancelled: bool = False
        self._current_plan: Optional[PlacePlan] = None

    def execute(self, plan: PlacePlan) -> ExecutionResult:
        """执行 PlacePlan。

        Args:
            plan: PlacePlan

        Returns:
            ExecutionResult: 执行结果
        """
        result = ExecutionResult(status=ExecutionStatus.RUNNING)
        result.started_at = time.time()

        if not plan.operations:
            result.status = ExecutionStatus.SUCCESS
            result.finished_at = time.time()
            self.logger.warning("Empty plan, nothing to execute")
            return result

        self.logger.info(
            "Executing plan with %d operations, rate_limit=%d/s",
            plan.operation_count, self.config.rate_limit_per_second,
        )

        # 广播开始
        if self.config.broadcast and self.game_interface:
            try:
                self.game_interface.send_text(BROADCAST_START)
            except Exception as e:
                self.logger.warning("Failed to broadcast start: %s", e)

        # 初始化速率限制
        rate_limiter = _RateLimiter(self.config.rate_limit_per_second)

        # 初始化统计
        stats = ExecutionStats(total=plan.operation_count)
        deadline = result.started_at + self.config.timeout

        # 遍历操作
        for op in plan.iter_operations():
            if self._cancelled:
                result.status = ExecutionStatus.CANCELLED
                break

            if time.time() > deadline:
                result.status = ExecutionStatus.TIMEOUT
                result.failure_reason = FailureReason.UNKNOWN
                result.error = "Execution timed out"
                break

            # 跳过空气
            if self.config.skip_air and op.block.name == "minecraft:air":
                stats.skipped += 1
                continue

            # 等待速率令牌
            if not rate_limiter.acquire():
                stats.rate_limited_count += 1
                rate_limiter.wait_for_token()

            # 执行单个方块
            success, reason = self._execute_operation(op)

            if success:
                stats.succeeded += 1
            else:
                stats.failed += 1
                stats.failed_blocks.append((op.block.position, reason))

                # 重试
                for retry in range(self.config.max_retries):
                    stats.retried += 1
                    time.sleep(self.config.retry_interval)
                    success, reason = self._execute_operation(op)
                    if success:
                        stats.succeeded += 1
                        stats.failed -= 1
                        break
                else:
                    self.logger.error(
                        "Failed to place block at %s after %d retries: %s",
                        op.block.position, self.config.max_retries, reason,
                    )

        # 完成统计
        result.finished_at = time.time()
        stats.elapsed_time = result.elapsed_time
        if stats.elapsed_time > 0:
            stats.blocks_per_second = stats.succeeded / stats.elapsed_time

        result.stats = stats

        # 判断最终状态
        if result.status == ExecutionStatus.RUNNING:
            if stats.failed == 0:
                result.status = ExecutionStatus.SUCCESS
            elif stats.succeeded > 0:
                result.status = ExecutionStatus.PARTIAL
            else:
                result.status = ExecutionStatus.FAILED
                result.error = "All blocks failed"

        # 广播结束
        if self.config.broadcast and self.game_interface:
            try:
                if result.is_success:
                    self.game_interface.send_text(BROADCAST_END)
                else:
                    self.game_interface.send_text(BROADCAST_FAILED % (result.error or "unknown"))
            except Exception as e:
                self.logger.warning("Failed to broadcast end: %s", e)

        self.logger.info(
            "Execution finished: status=%s, succeeded=%d, failed=%d, "
            "elapsed=%.2fs, rate=%.2f blocks/s",
            result.status.name, stats.succeeded, stats.failed,
            stats.elapsed_time, stats.blocks_per_second,
        )
        return result

    def _execute_operation(self, op: PlacePlanItem) -> tuple[bool, str]:
        """执行单个方块操作。

        Args:
            op: PlacePlanItem

        Returns:
            (success, reason) 元组
        """
        if self.game_interface is None:
            # 模拟模式
            return True, ""

        try:
            if op.operation_type == PlaceOperationType.PLACE_BLOCK:
                return self._place_block(op)
            if op.operation_type == PlaceOperationType.PLACE_BLOCK_WITH_STATES:
                return self._place_block_with_states(op)
            if op.operation_type == PlaceOperationType.PLACE_BLOCK_WITH_NBT:
                return self._place_block_with_nbt(op)
            if op.operation_type == PlaceOperationType.PLACE_BLOCK_WITH_CHEST:
                return self._place_block_with_chest(op)
            if op.operation_type == PlaceOperationType.PLACE_BLOCK_WITH_COMMAND_BLOCK:
                return self._place_block_with_command_block(op)
            if op.operation_type == PlaceOperationType.PLACE_COMMAND_BLOCK:
                return self._place_command_block(op)
            return False, f"Unknown operation type: {op.operation_type}"
        except Exception as e:
            self.logger.exception("Exception during operation: %s", e)
            return False, str(e)

    def _place_block(self, op: PlacePlanItem) -> tuple[bool, str]:
        """放置基础方块。"""
        block = op.block
        if not block.name:
            return False, "Invalid block name"

        # 发送 UpdateBlock 数据包
        runtime_id = block.runtime_id or self._get_runtime_id(block)
        if runtime_id is None:
            return False, "Failed to get runtime ID"

        try:
            self.game_interface.send_update_block(
                position=block.position,
                runtime_id=runtime_id,
            )
        except Exception as e:
            return False, f"Failed to send UpdateBlock: {e}"
        return True, ""

    def _place_block_with_states(self, op: PlacePlanItem) -> tuple[bool, str]:
        """放置带状态的方块。"""
        block = op.block
        runtime_id = block.runtime_id or self._get_runtime_id(block)
        if runtime_id is None:
            return False, "Failed to get runtime ID with states"

        try:
            self.game_interface.send_update_block(
                position=block.position,
                runtime_id=runtime_id,
            )
        except Exception as e:
            return False, f"Failed to send UpdateBlock with states: {e}"
        return True, ""

    def _place_block_with_nbt(self, op: PlacePlanItem) -> tuple[bool, str]:
        """放置带 NBT 的方块。"""
        block = op.block
        if not block.nbt:
            return False, "Missing NBT data"

        nbt_size = len(repr(block.nbt))
        if nbt_size > MAX_NBT_SIZE:
            return False, f"NBT too large ({nbt_size} > {MAX_NBT_SIZE})"

        # 先放方块
        success, reason = self._place_block(op)
        if not success:
            return False, reason

        # 发送 BlockEntityData
        try:
            self.game_interface.send_block_entity_data(
                position=block.position,
                nbt=block.nbt,
            )
        except Exception as e:
            return False, f"Failed to send BlockEntityData: {e}"
        return True, ""

    def _place_block_with_chest(self, op: PlacePlanItem) -> tuple[bool, str]:
        """放置带箱子数据的方块。

        流程 (逆向自 strings + REPORT.txt):
            1. 放置方块
            2. 发送 ContainerOpen 数据包
            3. 等待 ContainerOpen 回包
            4. 发送 ItemStackRequest (填充物品)
            5. 发送 ContainerClose 数据包
        """
        block = op.block
        # 先放方块
        success, reason = self._place_block(op)
        if not success:
            return False, reason

        # 打开容器
        try:
            container_id = self.game_interface.open_container(block.position)
        except Exception as e:
            return False, f"Failed to open container: {e}"

        if container_id is None:
            return False, "Container open timeout"

        # 填充物品
        try:
            for slot in block.chest_slots:
                self.game_interface.send_item_stack_request(
                    container_id=container_id,
                    slot=slot,
                )
        except Exception as e:
            return False, f"Failed to fill container: {e}"

        # 关闭容器
        try:
            self.game_interface.close_container(container_id)
        except Exception as e:
            return False, f"Failed to close container: {e}"

        return True, ""

    def _place_block_with_command_block(
        self, op: PlacePlanItem
    ) -> tuple[bool, str]:
        """放置带命令方块数据的方块。"""
        block = op.block
        if not block.command_block_data:
            return False, "Missing command block data"

        # 先放方块
        success, reason = self._place_block(op)
        if not success:
            return False, reason

        # 发送 BlockEntityData
        cmd_nbt = {
            "id": "CommandBlock",
            "Command": block.command_block_data.command,
            "CustomName": block.command_block_data.custom_name,
            "LastOutput": block.command_block_data.last_output,
            "TickDelay": block.command_block_data.tick_delay,
            "ExecuteOnFirstTick": int(block.command_block_data.execute_on_first_tick),
            "TrackOutput": int(block.command_block_data.track_output),
            "Conditional": int(block.command_block_data.conditional),
            "NeedsRedstone": int(block.command_block_data.needs_redstone),
        }
        try:
            self.game_interface.send_block_entity_data(
                position=block.position,
                nbt=cmd_nbt,
            )
        except Exception as e:
            return False, f"Failed to send command block NBT: {e}"
        return True, ""

    def _place_command_block(self, op: PlacePlanItem) -> tuple[bool, str]:
        """放置命令方块 (专用)。"""
        return self._place_block_with_command_block(op)

    def _get_runtime_id(self, block: Any) -> Optional[int]:
        """获取运行时方块 ID。

        通过 ToNEMCConvertor 将方块名 + states 转换为运行时 ID。
        """
        if self.game_interface is None:
            return None

        try:
            convertor = getattr(self.game_interface, "block_convertor", None)
            if convertor is None:
                return None
            return convertor.convert(block.name, block.states)
        except Exception as e:
            self.logger.warning("Failed to get runtime ID: %s", e)
            return None

    def cancel(self) -> None:
        """取消当前执行。"""
        self._cancelled = True
        self.logger.info("Execution cancelled")

    def estimate_time(self, plan: PlacePlan) -> float:
        """估算执行时间 (秒)。"""
        if self.config.rate_limit_per_second <= 0:
            return float("inf")
        return plan.operation_count / self.config.rate_limit_per_second


# -------------------------------------------------------------------- #
# 速率限制器 (令牌桶)
# -------------------------------------------------------------------- #


class _RateLimiter:
    """令牌桶速率限制器 (逆向自 rate.Limiter)。"""

    def __init__(self, rate_per_second: int) -> None:
        self.rate = rate_per_second
        self.tokens: float = float(rate_per_second)
        self.max_tokens = float(rate_per_second)
        self.last_refill = time.time()
        self.logger = logging.getLogger("pocketterm.protocol.command_systems.phoenix_executor.rate_limiter")

    def acquire(self) -> bool:
        """尝试获取一个令牌 (不阻塞)。"""
        self._refill()
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def wait_for_token(self) -> None:
        """等待并获取一个令牌 (阻塞)。"""
        while not self.acquire():
            time.sleep(0.001)

    def _refill(self) -> None:
        """补充令牌。"""
        now = time.time()
        elapsed = now - self.last_refill
        if elapsed > 0:
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
            self.last_refill = now
