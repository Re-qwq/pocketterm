"""PocketTerm MCPC 挑战自动应答与 OperatorChallenge 权限监听

本模块从 NexusE / StarShuttler 逆向提取, 实现 MCPC (Minecraft Protocol
Check Challenge) 挑战的自动应答和 OP 权限监听。

逆向来源
--------

1. **NexusE** (Go 二进制逆向):
   - ``MCPCheckChallengesSolver`` via PyRPC 协议
   - ``OperatorChallenge`` 监听 SetCommandEnabled 数据包
   - ``PostponeActionsAfterChallengePassed`` 挑战期间动作排队

2. **StarShuttler** (辅助逆向确认):
   - ``CanSolveChallenge`` 预检逻辑
   - 挑战类型分类与路由

核心机制
--------

**MCPC 挑战类型**:
    - ``OperatorChallenge``: 操作员挑战, 服务器要求客户端完成特定动作
      (如输入密码、执行命令等)
    - ``CanSolveChallenge``: 可解决挑战, 客户端可识别并自动响应
    - ``PyRPCResponder``: 通过 Python RPC 协议回应挑战

**挑战流程**:
    1. 服务器发送 ``SetCommandEnabled`` 数据包 (携带挑战信息)
    2. ``OperatorChallengeMonitor`` 解析并分类挑战
    3. ``MCPCChallengeSolver`` 通过 PyRPC 生成应答
    4. 应答期间, ``PostponeActionQueue`` 排队所有动作
    5. 挑战通过后, 批量放行排队动作

**PostponeActionsAfterChallengePassed**:
    - 挑战开始: 所有命令/动作加入队列
    - 挑战期间: 队列暂存, 不发送
    - 挑战通过: 批量放行队列中的动作
    - 挑战失败: 清空队列, 触发重连

类组织
------

- :class:`ChallengeStatus`         -- 挑战状态枚举
- :class:`ChallengeType`           -- 挑战类型枚举
- :class:`MCPCChallengeSolver`     -- MCPC 挑战求解器
- :class:`OperatorChallengeMonitor` -- OP 权限监听器
- :class:`PostponeActionQueue`     -- 延迟动作队列
- :class:`ChallengeRecord`         -- 挑战记录数据类
"""
from __future__ import annotations

import asyncio
import enum
import threading
import time
import uuid as _uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..logger import get_logger

logger = get_logger("auth.mcpc_solver")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ChallengeStatus(enum.Enum):
    """MCPC 挑战状态枚举。"""

    #: 未激活
    IDLE = "idle"
    #: 挑战已接收, 等待处理
    PENDING = "pending"
    #: 正在求解挑战
    SOLVING = "solving"
    #: 挑战已通过
    PASSED = "passed"
    #: 挑战失败
    FAILED = "failed"
    #: 挑战已跳过 (客户端选择不处理)
    SKIPPED = "skipped"


class ChallengeType(enum.Enum):
    """MCPC 挑战类型枚举 (逆向自 NexusE ``MCPCheckChallengesSolver``)。"""

    #: 操作员挑战 (服务器要求客户端完成特定动作)
    OPERATOR = "operator"

    #: 可解决挑战 (客户端可识别并自动响应)
    CAN_SOLVE = "can_solve"

    #: PyRPC 挑战 (通过 Python RPC 协议回应)
    PYRPC = "pyrpc"

    #: 未知挑战类型
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class ChallengeRecord:
    """单次挑战记录。

    存储挑战的完整生命周期信息, 用于审计和调试。
    """

    #: 挑战 ID (UUID)
    challenge_id: str = field(default_factory=lambda: str(_uuid.uuid4()))

    #: 挑战类型
    challenge_type: ChallengeType = ChallengeType.UNKNOWN

    #: 挑战状态
    status: ChallengeStatus = ChallengeStatus.PENDING

    #: 挑战数据 (原始 payload)
    payload: Dict[str, Any] = field(default_factory=dict)

    #: 求解结果
    result: Optional[Dict[str, Any]] = None

    #: 创建时间 (Unix 时间戳)
    created_at: float = field(default_factory=time.time)

    #: 求解完成时间
    solved_at: float = 0.0

    #: 求解耗时 (秒)
    solve_duration: float = 0.0

    #: 错误信息 (如果失败)
    error_message: str = ""

    def mark_passed(self, result: Optional[Dict[str, Any]] = None) -> None:
        """标记挑战已通过。"""
        self.status = ChallengeStatus.PASSED
        self.result = result
        self.solved_at = time.time()
        self.solve_duration = self.solved_at - self.created_at

    def mark_failed(self, error: str = "") -> None:
        """标记挑战失败。"""
        self.status = ChallengeStatus.FAILED
        self.error_message = error
        self.solved_at = time.time()
        self.solve_duration = self.solved_at - self.created_at

    def mark_skipped(self) -> None:
        """标记挑战已跳过。"""
        self.status = ChallengeStatus.SKIPPED
        self.solved_at = time.time()


# ---------------------------------------------------------------------------
# 回调类型
# ---------------------------------------------------------------------------

#: 挑战求解回调: 接收挑战数据, 返回应答数据
ChallengeSolverCallback = Callable[[Dict[str, Any]], Dict[str, Any]]

#: 异步挑战求解回调
AsyncChallengeSolverCallback = Callable[
    [Dict[str, Any]], "asyncio.Future[Dict[str, Any]]"
]

#: OP 权限变更回调: 在权限变化时触发
OperatorChangeCallback = Callable[[bool, Dict[str, Any]], None]

#: 动作执行回调: 执行排队中的动作
ActionCallback = Callable[[Dict[str, Any]], bool]


# ---------------------------------------------------------------------------
# PostponeActionQueue
# ---------------------------------------------------------------------------

class PostponeActionQueue:
    """延迟动作队列 (逆向自 NexusE ``PostponeActionsAfterChallengePassed``)。

    挑战期间所有动作排队, 通过后批量放行, 失败后清空。

    功能:
        - 挑战期间动作排队
        - 挑战通过后批量放行
        - 挑战失败后清空队列
        - 支持最大队列长度限制
        - 支持放行间隔控制

    线程安全: 使用 ``threading.Lock`` 保护队列操作。
    """

    def __init__(
        self,
        max_queue_size: int = 100,
        batch_interval: float = 0.2,
        action_executor: Optional[ActionCallback] = None,
    ) -> None:
        """
        Args:
            max_queue_size: 最大队列长度 (超过则丢弃最旧)。
            batch_interval: 批量放行间隔 (秒)。
            action_executor: 动作执行回调 (用于批量放行)。
        """
        self._max_queue_size: int = max_queue_size
        self._batch_interval: float = batch_interval
        self._action_executor: Optional[ActionCallback] = action_executor

        self._lock: threading.Lock = threading.Lock()
        self._async_lock: asyncio.Lock = asyncio.Lock()
        self._queue: Deque[Dict[str, Any]] = deque()
        self._is_postponing: bool = False
        self._total_queued: int = 0
        self._total_released: int = 0
        self._total_dropped: int = 0

        logger.info(
            f"PostponeActionQueue 初始化: max_size={max_queue_size}, "
            f"batch_interval={batch_interval}s"
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def is_postponing(self) -> bool:
        """是否正在延迟排队。"""
        with self._lock:
            return self._is_postponing

    @property
    def queue_size(self) -> int:
        """当前队列长度。"""
        with self._lock:
            return len(self._queue)

    @property
    def is_empty(self) -> bool:
        """队列是否为空。"""
        with self._lock:
            return len(self._queue) == 0

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def start_postponing(self) -> None:
        """开始延迟排队 (挑战开始时调用)。"""
        with self._lock:
            self._is_postponing = True
        logger.info(
            f"PostponeActionQueue 开始排队: queue_size={len(self._queue)}"
        )

    def enqueue(self, action: Dict[str, Any]) -> bool:
        """将动作加入队列。

        Args:
            action: 动作数据字典 (需包含 "type" 和 "payload" 字段)。

        Returns:
            True 如果成功入队, False 如果队列已满 (被丢弃)。
        """
        with self._lock:
            if not self._is_postponing:
                # 未在延迟模式, 不排队
                return False

            if len(self._queue) >= self._max_queue_size:
                self._queue.popleft()
                self._total_dropped += 1
                logger.warning(
                    f"PostponeActionQueue 队列已满, 丢弃最旧动作: "
                    f"dropped_total={self._total_dropped}"
                )

            self._queue.append(action)
            self._total_queued += 1
            logger.debug(
                f"PostponeActionQueue 动作入队: "
                f"type={action.get('type')}, queue_size={len(self._queue)}"
            )
            return True

    async def release_all(self) -> int:
        """批量放行所有排队动作 (挑战通过后调用)。

        Returns:
            成功放行的动作数量。
        """
        async with self._async_lock:
            with self._lock:
                self._is_postponing = False
                actions = list(self._queue)
                self._queue.clear()

            if not actions:
                logger.debug("PostponeActionQueue 队列为空, 无需放行")
                return 0

            logger.info(
                f"PostponeActionQueue 批量放行: count={len(actions)}"
            )

            released = 0
            for action in actions:
                try:
                    if self._action_executor is not None:
                        loop = asyncio.get_running_loop()
                        success = await loop.run_in_executor(
                            None, self._action_executor, action
                        )
                        if success:
                            released += 1
                            self._total_released += 1
                    else:
                        released += 1
                        self._total_released += 1

                    # 放行间隔
                    await asyncio.sleep(self._batch_interval)

                except Exception as exc:
                    logger.error(
                        f"PostponeActionQueue 放行动作失败: "
                        f"type={action.get('type')}, error={exc}"
                    )

            logger.info(
                f"PostponeActionQueue 放行完成: "
                f"released={released}/{len(actions)}"
            )
            return released

    def cancel_all(self) -> int:
        """清空队列 (挑战失败后调用)。

        Returns:
            被丢弃的动作数量。
        """
        with self._lock:
            self._is_postponing = False
            count = len(self._queue)
            self._queue.clear()
            self._total_dropped += count

        logger.warning(
            f"PostponeActionQueue 清空队列: dropped={count}"
        )
        return count

    def peek_all(self) -> List[Dict[str, Any]]:
        """查看队列中所有动作 (不消费)。

        Returns:
            动作列表副本。
        """
        with self._lock:
            return list(self._queue)

    def stats(self) -> Dict[str, Any]:
        """返回队列统计。"""
        with self._lock:
            return {
                "is_postponing": self._is_postponing,
                "queue_size": len(self._queue),
                "max_queue_size": self._max_queue_size,
                "total_queued": self._total_queued,
                "total_released": self._total_released,
                "total_dropped": self._total_dropped,
            }

    def reset_stats(self) -> None:
        """重置统计计数器。"""
        with self._lock:
            self._total_queued = 0
            self._total_released = 0
            self._total_dropped = 0


# ---------------------------------------------------------------------------
# MCPCChallengeSolver
# ---------------------------------------------------------------------------

class MCPCChallengeSolver:
    """MCPC 挑战自动求解器 (逆向自 NexusE ``MCPCheckChallengesSolver``)。

    通过 PyRPC 协议与 NeOmega 接入点集成, 自动求解 MCPC 挑战。

    功能:
        - 预检是否可以求解 (``can_solve``)
        - 自动生成挑战应答 (通过 PyRPC)
        - 挑战记录与审计
        - 与 ``PostponeActionQueue`` 集成

    使用示例::

        solver = MCPCChallengeSolver(
            on_solve_operator=my_operator_solver,
            on_solve_pyrpc=my_pyrpc_solver,
        )
        result = await solver.solve(challenge_data)
        if result.status == ChallengeStatus.PASSED:
            print("挑战通过")
    """

    def __init__(
        self,
        on_solve_operator: Optional[ChallengeSolverCallback] = None,
        on_solve_can_solve: Optional[ChallengeSolverCallback] = None,
        on_solve_pyrpc: Optional[ChallengeSolverCallback] = None,
        on_solve_operator_async: Optional[AsyncChallengeSolverCallback] = None,
        on_solve_can_solve_async: Optional[AsyncChallengeSolverCallback] = None,
        on_solve_pyrpc_async: Optional[AsyncChallengeSolverCallback] = None,
        postpone_queue: Optional[PostponeActionQueue] = None,
        max_history: int = 100,
    ) -> None:
        """
        Args:
            on_solve_operator: OperatorChallenge 同步求解回调。
            on_solve_can_solve: CanSolveChallenge 同步求解回调。
            on_solve_pyrpc: PyRPC 同步求解回调。
            on_solve_operator_async: OperatorChallenge 异步求解回调 (优先)。
            on_solve_can_solve_async: CanSolveChallenge 异步求解回调 (优先)。
            on_solve_pyrpc_async: PyRPC 异步求解回调 (优先)。
            postpone_queue: 延迟动作队列 (挑战期间动作排队)。
            max_history: 最大挑战记录数。
        """
        self._on_solve_operator: Optional[ChallengeSolverCallback] = on_solve_operator
        self._on_solve_can_solve: Optional[ChallengeSolverCallback] = on_solve_can_solve
        self._on_solve_pyrpc: Optional[ChallengeSolverCallback] = on_solve_pyrpc
        self._on_solve_operator_async: Optional[AsyncChallengeSolverCallback] = (
            on_solve_operator_async
        )
        self._on_solve_can_solve_async: Optional[AsyncChallengeSolverCallback] = (
            on_solve_can_solve_async
        )
        self._on_solve_pyrpc_async: Optional[AsyncChallengeSolverCallback] = (
            on_solve_pyrpc_async
        )

        self._postpone_queue: Optional[PostponeActionQueue] = postpone_queue
        self._max_history: int = max_history
        self._async_lock: asyncio.Lock = asyncio.Lock()
        self._lock: threading.Lock = threading.Lock()

        # 挑战记录
        self._history: Deque[ChallengeRecord] = deque()
        self._active_challenge: Optional[ChallengeRecord] = None
        self._total_solved: int = 0
        self._total_failed: int = 0
        self._total_skipped: int = 0

        logger.info(
            f"MCPCChallengeSolver 初始化: max_history={max_history}, "
            f"postpone_queue={'enabled' if postpone_queue else 'disabled'}"
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def active_challenge(self) -> Optional[ChallengeRecord]:
        """当前活跃的挑战记录。"""
        with self._lock:
            return self._active_challenge

    @property
    def is_solving(self) -> bool:
        """是否正在求解挑战。"""
        with self._lock:
            return self._active_challenge is not None

    @property
    def history(self) -> List[ChallengeRecord]:
        """挑战历史记录 (最近 N 条)。"""
        with self._lock:
            return list(self._history)

    # ------------------------------------------------------------------
    # 预检
    # ------------------------------------------------------------------

    def can_solve(self, challenge_data: Dict[str, Any]) -> bool:
        """预检是否可以求解挑战 (逆向自 NexusE ``CanSolveChallenge``)。

        检查条件:
            - 挑战类型已知
            - 对应类型的求解器已注册
            - 挑战数据格式正确

        Args:
            challenge_data: 挑战数据字典。

        Returns:
            True 如果可以求解。
        """
        ct = self._classify_challenge(challenge_data)
        if ct == ChallengeType.UNKNOWN:
            return False
        if ct == ChallengeType.OPERATOR:
            return (
                self._on_solve_operator is not None
                or self._on_solve_operator_async is not None
            )
        if ct == ChallengeType.CAN_SOLVE:
            return (
                self._on_solve_can_solve is not None
                or self._on_solve_can_solve_async is not None
            )
        if ct == ChallengeType.PYRPC:
            return (
                self._on_solve_pyrpc is not None
                or self._on_solve_pyrpc_async is not None
            )
        return False

    def _classify_challenge(self, challenge_data: Dict[str, Any]) -> ChallengeType:
        """分类挑战类型。

        根据挑战数据中的特征字段判断挑战类型:
            - 包含 "operator_token" -> OperatorChallenge
            - 包含 "challenge_id" + "can_solve" -> CanSolveChallenge
            - 包含 "pyrpc_endpoint" -> PyRPCResponder

        Args:
            challenge_data: 挑战数据字典。

        Returns:
            挑战类型。
        """
        if "operator_token" in challenge_data:
            return ChallengeType.OPERATOR
        if "challenge_id" in challenge_data and challenge_data.get("can_solve", False):
            return ChallengeType.CAN_SOLVE
        if "pyrpc_endpoint" in challenge_data:
            return ChallengeType.PYRPC
        return ChallengeType.UNKNOWN

    # ------------------------------------------------------------------
    # 求解
    # ------------------------------------------------------------------

    async def solve(self, challenge_data: Dict[str, Any]) -> ChallengeRecord:
        """求解 MCPC 挑战 (主入口)。

        Args:
            challenge_data: 挑战数据字典。

        Returns:
            挑战记录 (包含求解结果)。
        """
        async with self._async_lock:
            ct = self._classify_challenge(challenge_data)
            record = ChallengeRecord(
                challenge_type=ct,
                payload=challenge_data,
                status=ChallengeStatus.SOLVING,
            )

            with self._lock:
                self._active_challenge = record

            logger.info(
                f"MCPCChallengeSolver 开始求解: "
                f"type={ct.value}, id={record.challenge_id}"
            )

            # 开始延迟排队
            if self._postpone_queue is not None:
                self._postpone_queue.start_postponing()

            try:
                result = await self._dispatch_solve(ct, challenge_data)
                record.mark_passed(result)
                with self._lock:
                    self._total_solved += 1

                # 放行排队动作
                if self._postpone_queue is not None:
                    await self._postpone_queue.release_all()

                logger.info(
                    f"MCPCChallengeSolver 求解成功: "
                    f"type={ct.value}, duration={record.solve_duration:.2f}s"
                )

            except Exception as exc:
                record.mark_failed(str(exc))
                with self._lock:
                    self._total_failed += 1

                # 清空排队动作
                if self._postpone_queue is not None:
                    self._postpone_queue.cancel_all()

                logger.error(
                    f"MCPCChallengeSolver 求解失败: "
                    f"type={ct.value}, error={exc}"
                )

            # 存入历史
            with self._lock:
                self._history.append(record)
                if len(self._history) > self._max_history:
                    self._history.popleft()
                self._active_challenge = None

            return record

    async def skip(self, challenge_data: Dict[str, Any]) -> ChallengeRecord:
        """跳过挑战 (不求解)。

        Args:
            challenge_data: 挑战数据字典。

        Returns:
            挑战记录 (状态为 SKIPPED)。
        """
        ct = self._classify_challenge(challenge_data)
        record = ChallengeRecord(
            challenge_type=ct,
            payload=challenge_data,
        )
        record.mark_skipped()

        with self._lock:
            self._total_skipped += 1
            self._history.append(record)
            if len(self._history) > self._max_history:
                self._history.popleft()

        logger.info(f"MCPCChallengeSolver 跳过挑战: type={ct.value}")
        return record

    async def _dispatch_solve(
        self,
        ct: ChallengeType,
        data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """根据挑战类型分发到对应的求解器。

        Args:
            ct: 挑战类型。
            data: 挑战数据。

        Returns:
            求解结果字典。

        Raises:
            ValueError: 如果挑战类型未知。
            RuntimeError: 如果求解器未注册。
        """
        if ct == ChallengeType.OPERATOR:
            if self._on_solve_operator_async is not None:
                return await self._on_solve_operator_async(data)
            if self._on_solve_operator is not None:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._on_solve_operator, data
                )
            raise RuntimeError("OperatorChallenge 求解器未注册")

        if ct == ChallengeType.CAN_SOLVE:
            if self._on_solve_can_solve_async is not None:
                return await self._on_solve_can_solve_async(data)
            if self._on_solve_can_solve is not None:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._on_solve_can_solve, data
                )
            raise RuntimeError("CanSolveChallenge 求解器未注册")

        if ct == ChallengeType.PYRPC:
            if self._on_solve_pyrpc_async is not None:
                return await self._on_solve_pyrpc_async(data)
            if self._on_solve_pyrpc is not None:
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(
                    None, self._on_solve_pyrpc, data
                )
            raise RuntimeError("PyRPC 求解器未注册")

        raise ValueError(f"未知挑战类型: {ct}")

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """返回求解器统计。"""
        with self._lock:
            return {
                "is_solving": self._active_challenge is not None,
                "active_challenge": (
                    {
                        "id": self._active_challenge.challenge_id,
                        "type": self._active_challenge.challenge_type.value,
                        "status": self._active_challenge.status.value,
                    }
                    if self._active_challenge
                    else None
                ),
                "total_solved": self._total_solved,
                "total_failed": self._total_failed,
                "total_skipped": self._total_skipped,
                "history_count": len(self._history),
                "max_history": self._max_history,
                "postpone_queue": (
                    self._postpone_queue.stats()
                    if self._postpone_queue
                    else None
                ),
            }

    def reset_stats(self) -> None:
        """重置统计计数器。"""
        with self._lock:
            self._total_solved = 0
            self._total_failed = 0
            self._total_skipped = 0
            self._history.clear()
            self._active_challenge = None


# ---------------------------------------------------------------------------
# OperatorChallengeMonitor
# ---------------------------------------------------------------------------

class OperatorChallengeMonitor:
    """OP 权限挑战监听器 (逆向自 NexusE ``OperatorChallenge``).

    监听 ``SetCommandEnabled`` 数据包, 检测 OP 权限变化。

    功能:
        - 监听 SetCommandEnabled 数据包
        - 检测 OP 权限变化事件
        - 触发权限变更回调
        - 集成 MCPCChallengeSolver 自动求解

    使用示例::

        monitor = OperatorChallengeMonitor(
            solver=mcpc_solver,
            on_operator_change=my_operator_handler,
        )
        monitor.feed_packet(set_command_enabled_packet)
    """

    def __init__(
        self,
        solver: Optional[MCPCChallengeSolver] = None,
        on_operator_change: Optional[OperatorChangeCallback] = None,
        on_challenge_detected: Optional[
            Callable[[Dict[str, Any]], None]
        ] = None,
        change_window: float = 30.0,
        change_threshold: int = 3,
    ) -> None:
        """
        Args:
            solver: MCPC 挑战求解器 (用于自动求解)。
            on_operator_change: OP 权限变更回调 (has_op, packet_data)。
            on_challenge_detected: 挑战检测回调 (challenge_data)。
            change_window: 权限变更检测窗口 (秒)。
            change_threshold: 权限变更阈值 (超过则视为异常)。
        """
        self._solver: Optional[MCPCChallengeSolver] = solver
        self._on_operator_change: Optional[OperatorChangeCallback] = on_operator_change
        self._on_challenge_detected: Optional[
            Callable[[Dict[str, Any]], None]
        ] = on_challenge_detected
        self._change_window: float = change_window
        self._change_threshold: int = change_threshold

        self._async_lock: asyncio.Lock = asyncio.Lock()
        self._lock: threading.Lock = threading.Lock()

        # 状态
        self._has_op: bool = False
        self._op_changes: List[float] = []  # 权限变更时间戳列表
        self._last_change_at: float = 0.0
        self._total_packets: int = 0
        self._total_challenges: int = 0
        self._total_auto_solved: int = 0

        # 正在求解的挑战任务
        self._solve_task: Optional[asyncio.Task[None]] = None

        logger.info(
            f"OperatorChallengeMonitor 初始化: "
            f"window={change_window}s, threshold={change_threshold}, "
            f"solver={'enabled' if solver else 'disabled'}"
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------

    @property
    def has_op(self) -> bool:
        """当前是否拥有 OP 权限。"""
        with self._lock:
            return self._has_op

    @property
    def is_operator_anomaly(self) -> bool:
        """是否检测到 OP 权限异常 (短时间内频繁变更)。"""
        with self._lock:
            now = time.time()
            # 清理窗口外的记录
            self._op_changes = [
                t for t in self._op_changes
                if now - t <= self._change_window
            ]
            return len(self._op_changes) >= self._change_threshold

    # ------------------------------------------------------------------
    # 核心方法
    # ------------------------------------------------------------------

    def feed_packet(self, packet: Dict[str, Any]) -> None:
        """处理 SetCommandEnabled 数据包 (同步入口)。

        解析数据包中的挑战信息和权限变化。

        Args:
            packet: SetCommandEnabled 数据包字典。
        """
        with self._lock:
            self._total_packets += 1

        # 检查权限变化
        old_op = self._has_op
        new_op = self._extract_op_status(packet)

        if old_op != new_op:
            self._handle_op_change(old_op, new_op, packet)

        # 检查挑战
        challenge = self._extract_challenge(packet)
        if challenge is not None:
            self._handle_challenge(challenge, packet)

    async def feed_packet_async(self, packet: Dict[str, Any]) -> None:
        """处理 SetCommandEnabled 数据包 (异步入口)。

        与 ``feed_packet`` 相同, 但异步处理挑战求解。

        Args:
            packet: SetCommandEnabled 数据包字典。
        """
        async with self._async_lock:
            self.feed_packet(packet)

            # 自动求解挑战
            challenge = self._extract_challenge(packet)
            if challenge is not None and self._solver is not None:
                if self._solver.can_solve(challenge):
                    try:
                        await self._solver.solve(challenge)
                        with self._lock:
                            self._total_auto_solved += 1
                        logger.info("OperatorChallengeMonitor 自动求解成功")
                    except Exception as exc:
                        logger.error(
                            f"OperatorChallengeMonitor 自动求解失败: {exc}"
                        )

    def _extract_op_status(self, packet: Dict[str, Any]) -> bool:
        """从数据包中提取 OP 权限状态。

        Args:
            packet: SetCommandEnabled 数据包。

        Returns:
            True 如果有 OP 权限。
        """
        # 检查多种可能的字段名
        return bool(
            packet.get("operator", False)
            or packet.get("op", False)
            or packet.get("is_operator", False)
            or packet.get("permission_level", 0) >= 2
        )

    def _extract_challenge(
        self, packet: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """从数据包中提取挑战数据。

        Args:
            packet: SetCommandEnabled 数据包。

        Returns:
            挑战数据字典, 如果没有挑战则返回 None。
        """
        # 直连挑战字段
        if "challenge" in packet:
            return packet["challenge"]
        if "operator_challenge" in packet:
            return packet["operator_challenge"]
        if "mcpc_challenge" in packet:
            return packet["mcpc_challenge"]

        # 隐含挑战: 检查是否包含挑战特征
        if "operator_token" in packet:
            return packet
        if "challenge_id" in packet:
            return packet

        return None

    def _handle_op_change(
        self,
        old_op: bool,
        new_op: bool,
        packet: Dict[str, Any],
    ) -> None:
        """处理 OP 权限变化。

        Args:
            old_op: 旧权限状态。
            new_op: 新权限状态。
            packet: 原始数据包。
        """
        now = time.time()
        with self._lock:
            self._has_op = new_op
            self._op_changes.append(now)
            self._last_change_at = now

        direction = "获得" if new_op else "失去"
        logger.warning(
            f"OperatorChallengeMonitor: OP 权限变化: {direction} OP, "
            f"total_changes_in_window={len(self._op_changes)}"
        )

        if self._on_operator_change is not None:
            try:
                self._on_operator_change(new_op, packet)
            except Exception as exc:
                logger.error(
                    f"OperatorChallengeMonitor 权限变更回调异常: {exc}"
                )

    def _handle_challenge(
        self,
        challenge: Dict[str, Any],
        packet: Dict[str, Any],
    ) -> None:
        """处理挑战检测。

        Args:
            challenge: 挑战数据。
            packet: 原始数据包。
        """
        with self._lock:
            self._total_challenges += 1

        logger.info(
            f"OperatorChallengeMonitor 检测到挑战: "
            f"total_challenges={self._total_challenges}"
        )

        if self._on_challenge_detected is not None:
            try:
                self._on_challenge_detected(challenge)
            except Exception as exc:
                logger.error(
                    f"OperatorChallengeMonitor 挑战检测回调异常: {exc}"
                )

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        """返回监听器统计。"""
        with self._lock:
            return {
                "has_op": self._has_op,
                "is_operator_anomaly": self.is_operator_anomaly,
                "change_window": self._change_window,
                "change_threshold": self._change_threshold,
                "changes_in_window": len(self._op_changes),
                "last_change_at": self._last_change_at,
                "total_packets": self._total_packets,
                "total_challenges": self._total_challenges,
                "total_auto_solved": self._total_auto_solved,
                "solver_enabled": self._solver is not None,
            }

    def reset_stats(self) -> None:
        """重置统计计数器。"""
        with self._lock:
            self._op_changes.clear()
            self._last_change_at = 0.0
            self._total_packets = 0
            self._total_challenges = 0
            self._total_auto_solved = 0


__all__ = [
    "ChallengeStatus",
    "ChallengeType",
    "ChallengeRecord",
    "MCPCChallengeSolver",
    "OperatorChallengeMonitor",
    "PostponeActionQueue",
    "ChallengeSolverCallback",
    "AsyncChallengeSolverCallback",
    "OperatorChangeCallback",
    "ActionCallback",
]