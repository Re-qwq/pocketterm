"""postpone_actions - 动作排队 (PostponeActionsAfterChallengePassed)。

逆向自 NexusEgo v1.6.5 的动作排队系统, 来源:

    - WavesAccess/postpone_actions/  (动作排队模块)
    - anticheat.txt                   (反作弊数据)

PostponeActionsAfterChallengePassed 用于在挑战通过后执行排队的动作:

    1. 当 MCPC/Flowers 挑战未通过时, 客户端不能执行某些操作
    2. NexusE 将这些操作排队, 等待挑战通过后批量执行
    3. 避免在挑战期间触发服务器反作弊

字符串证据 (逆向自 anticheat.txt):
    "PostponeActionsAfterChallengePassed" -- 挑战通过后执行排队动作
    "postpone"                            -- 推迟
    "queue"                               -- 队列
    "pending actions"                     -- 待处理动作
    "execute"                             -- 执行
    "clear"                               -- 清除
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("pocketterm.auth.postpone_actions")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class PostponeError(Exception):
    """动作排队错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class PostponeAction:
    """排队动作。"""
    action_id: str = ""
    action_type: str = ""  # setblock / fill / tellraw / command
    action_data: dict[str, Any] = field(default_factory=dict)
    callback: Callable[..., Any] | None = None
    timestamp: float = 0.0
    priority: int = 0  # 优先级 (数字越大越优先)
    executed: bool = False
    error: str = ""

    def __repr__(self) -> str:
        return (
            f"PostponeAction(id={self.action_id!r}, type={self.action_type!r}, "
            f"priority={self.priority})"
        )


# -------------------------------------------------------------------- #
# 动作排队管理器
# -------------------------------------------------------------------- #


class PostponeActions:
    """动作排队管理器。

    逆向自 WavesAccess/postpone_actions/ 的 PostponeActionsAfterChallengePassed。
    """

    def __init__(self) -> None:
        self._queue: list[PostponeAction] = []
        self._lock = threading.Lock()
        self._challenge_passed: bool = False
        self._max_queue_size: int = 10000
        logger.debug("PostponeActions initialized")

    @property
    def challenge_passed(self) -> bool:
        """挑战是否已通过。"""
        return self._challenge_passed

    @property
    def queue_size(self) -> int:
        """队列大小。"""
        return len(self._queue)

    def mark_challenge_passed(self) -> None:
        """标记挑战已通过。

        逆向自 strings: "challenge passed"。
        标记后, 所有排队的动作将被执行。
        """
        with self._lock:
            self._challenge_passed = True
        logger.info("Challenge passed, executing %d postponed actions",
                      len(self._queue))
        self._execute_all()

    def mark_challenge_failed(self) -> None:
        """标记挑战失败。"""
        with self._lock:
            self._challenge_passed = False
        logger.warning("Challenge failed, keeping %d actions in queue",
                         len(self._queue))

    def postpone(self, action: PostponeAction) -> bool:
        """排队动作。

        逆向自 strings: "postpone"。

        Args:
            action: 要排队的动作。

        Returns:
            True 如果动作已排队 (挑战未通过), False 如果动作已立即执行。
        """
        action.timestamp = time.time()
        with self._lock:
            if self._challenge_passed:
                # 挑战已通过, 立即执行
                self._execute_action(action)
                return False
            if len(self._queue) >= self._max_queue_size:
                raise PostponeError(
                    f"queue full (max={self._max_queue_size})"
                )
            self._queue.append(action)
        logger.debug(
            "Action postponed: id=%s, type=%s, queue_size=%d",
            action.action_id, action.action_type, len(self._queue),
        )
        return True

    def postpone_action(self, action_type: str,
                          action_data: dict[str, Any],
                          action_id: str = "",
                          priority: int = 0,
                          callback: Callable[..., Any] | None = None) -> bool:
        """排队动作 (便捷方法)。

        Args:
            action_type: 动作类型。
            action_data: 动作数据。
            action_id: 动作 ID (可选, 自动生成如果为空)。
            priority: 优先级。
            callback: 回调函数。

        Returns:
            True 如果动作已排队, False 如果已立即执行。
        """
        if not action_id:
            action_id = f"{action_type}_{int(time.time() * 1000)}"
        action = PostponeAction(
            action_id=action_id,
            action_type=action_type,
            action_data=action_data,
            callback=callback,
            priority=priority,
        )
        return self.postpone(action)

    def execute_postponed_actions(self) -> int:
        """执行所有排队的动作。

        逆向自 strings: "execute"。

        Returns:
            执行的动作数。
        """
        if not self._challenge_passed:
            logger.warning("Cannot execute postponed actions: challenge not passed")
            return 0
        return self._execute_all()

    def _execute_all(self) -> int:
        """执行所有排队动作。"""
        with self._lock:
            # 按优先级排序
            self._queue.sort(key=lambda a: -a.priority)
            actions = list(self._queue)
            self._queue.clear()

        executed = 0
        for action in actions:
            if self._execute_action(action):
                executed += 1
        logger.info("Executed %d/%d postponed actions", executed, len(actions))
        return executed

    def _execute_action(self, action: PostponeAction) -> bool:
        """执行单个动作。"""
        try:
            if action.callback:
                action.callback(**action.action_data)
            action.executed = True
            logger.debug(
                "Action executed: id=%s, type=%s",
                action.action_id, action.action_type,
            )
            return True
        except Exception as exc:
            action.error = str(exc)
            logger.exception(
                "Action execution failed: id=%s, error=%s",
                action.action_id, exc,
            )
            return False

    def clear_postponed_actions(self) -> int:
        """清除所有排队的动作。

        逆向自 strings: "clear"。

        Returns:
            清除的动作数。
        """
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
        logger.info("Cleared %d postponed actions", count)
        return count

    def get_pending_actions(self) -> list[PostponeAction]:
        """获取待处理的动作列表。"""
        with self._lock:
            return list(self._queue)


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


#: 全局 PostponeActions 实例
_global_postpone: PostponeActions | None = None


def _get_postpone() -> PostponeActions:
    global _global_postpone
    if _global_postpone is None:
        _global_postpone = PostponeActions()
    return _global_postpone


def postpone_actions_after_challenge_passed(action_type: str,
                                              action_data: dict[str, Any],
                                              priority: int = 0) -> bool:
    """挑战通过后执行的动作排队。

    逆向自 strings: "PostponeActionsAfterChallengePassed"。

    Args:
        action_type: 动作类型。
        action_data: 动作数据。
        priority: 优先级。

    Returns:
        True 如果动作已排队, False 如果已立即执行。
    """
    return _get_postpone().postpone_action(action_type, action_data, priority=priority)


def execute_postponed_actions() -> int:
    """执行所有排队的动作。"""
    return _get_postpone().execute_postponed_actions()


def clear_postponed_actions() -> int:
    """清除所有排队的动作。"""
    return _get_postpone().clear_postponed_actions()


__all__ = [
    "PostponeError",
    "PostponeAction", "PostponeActions",
    "postpone_actions_after_challenge_passed",
    "execute_postponed_actions", "clear_postponed_actions",
]
