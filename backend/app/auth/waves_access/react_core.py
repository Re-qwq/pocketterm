"""react_core - ReactCore 反应核心模块。

逆向自 NovaBuilder 的 WavesAccess ReactCore, 来源:
    - /workspace/novuilder_reverse/strings_security.txt
    - /workspace/novuilder_reverse/player_options.txt
    - /workspace/novuilder_reverse/REPORT.txt

ReactCore 是 WavesAccess 的事件驱动反应系统, 实现:

    1. 规则注册 - 注册触发条件到反应动作的映射
    2. 事件触发 - 当条件满足时执行对应动作
    3. 优先级排序 - 高优先级规则先执行
    4. 冷却机制 - 防止规则频繁触发
    5. 条件过滤 - 支持复杂条件表达式

ReactCore 工作流程:
    1. 外部事件到达 (如玩家加入、服务器消息)
    2. ReactCore 遍历所有规则
    3. 检查规则的触发条件和过滤条件
    4. 满足条件则执行反应动作
    5. 记录执行日志和统计

字符串证据 (逆向自 strings_security.txt):
    "executeResult"         -- 执行结果
    "getTypeInfo"           -- 获取类型信息
    "generate_type"         -- 生成类型
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.waves_access.react_core")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 默认冷却时间 (秒)
DEFAULT_COOLDOWN: float = 5.0

#: 默认超时 (秒)
DEFAULT_TIMEOUT: float = 10.0

#: 最大规则数
MAX_RULES: int = 500

#: 默认优先级
DEFAULT_PRIORITY: int = 100


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class ReactTrigger(Enum):
    """反应触发类型。"""

    PLAYER_JOIN = auto()        # 玩家加入
    PLAYER_LEAVE = auto()       # 玩家离开
    PLAYER_CHAT = auto()        # 玩家聊天
    PLAYER_COMMAND = auto()     # 玩家命令
    SERVER_MESSAGE = auto()     # 服务器消息
    SERVER_EVENT = auto()       # 服务器事件
    BLOCK_UPDATE = auto()       # 方块更新
    CONTAINER_OPEN = auto()     # 容器打开
    CONTAINER_CLOSE = auto()    # 容器关闭
    TIMER = auto()              # 定时器
    CUSTOM = auto()             # 自定义


class ReactAction(Enum):
    """反应动作类型。"""

    SEND_COMMAND = auto()       # 发送命令
    SEND_MESSAGE = auto()       # 发送消息
    SEND_PACKET = auto()        # 发送数据包
    CALL_FUNCTION = auto()      # 调用函数
    EXECUTE_TASK = auto()       # 执行任务
    NOTIFY = auto()             # 通知
    LOG = auto()                # 记录日志
    CUSTOM = auto()             # 自定义


class RuleState(Enum):
    """规则状态。"""

    ACTIVE = auto()     # 激活
    PAUSED = auto()     # 暂停
    DISABLED = auto()   # 禁用
    ERROR = auto()      # 错误


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class ReactRule:
    """反应规则。

    定义一个触发条件到反应动作的映射。
    """

    rule_id: str = ""                                         # 规则 ID
    name: str = ""                                            # 规则名称
    trigger: ReactTrigger = ReactTrigger.CUSTOM               # 触发类型
    trigger_name: str = ""                                    # 触发名称 (用于自定义)
    condition: Callable[[dict[str, Any]], bool] | None = None # 过滤条件
    action: ReactAction = ReactAction.CALL_FUNCTION           # 动作类型
    action_func: Callable[[dict[str, Any]], Any] | None = None  # 动作函数
    priority: int = DEFAULT_PRIORITY                          # 优先级 (数值越小越高)
    cooldown: float = DEFAULT_COOLDOWN                         # 冷却时间 (秒)
    timeout: float = DEFAULT_TIMEOUT                          # 超时 (秒)
    enabled: bool = True                                      # 是否启用
    state: RuleState = RuleState.ACTIVE                       # 当前状态

    # 统计
    trigger_count: int = 0                                    # 触发次数
    last_triggered: float = 0.0                               # 上次触发时间
    last_result: Any = None                                   # 上次结果
    last_error: str = ""                                      # 上次错误

    @property
    def is_cooling_down(self) -> bool:
        """是否在冷却中。"""
        if self.cooldown <= 0:
            return False
        return (time.time() - self.last_triggered) < self.cooldown

    @property
    def effective_trigger_name(self) -> str:
        """有效触发名称。"""
        if self.trigger_name:
            return self.trigger_name
        return self.trigger.name.lower()

    def matches_trigger(self, trigger_name: str) -> bool:
        """检查是否匹配触发名称。

        Args:
            trigger_name: 触发名称。

        Returns:
            True 如果匹配。
        """
        return self.effective_trigger_name == trigger_name.lower()


@dataclass
class ReactStats:
    """反应系统统计。"""

    total_triggers: int = 0       # 总触发次数
    total_executed: int = 0       # 总执行次数
    total_blocked_cooldown: int = 0  # 冷却阻止次数
    total_blocked_condition: int = 0  # 条件阻止次数
    total_errors: int = 0         # 总错误次数
    by_trigger: dict[str, int] = field(default_factory=dict)  # 按触发类型统计
    by_rule: dict[str, int] = field(default_factory=dict)     # 按规则统计

    def record_trigger(self, trigger_name: str) -> None:
        """记录触发。"""
        self.total_triggers += 1
        self.by_trigger[trigger_name] = self.by_trigger.get(trigger_name, 0) + 1

    def record_execution(self, rule_name: str) -> None:
        """记录执行。"""
        self.total_executed += 1
        self.by_rule[rule_name] = self.by_rule.get(rule_name, 0) + 1

    def reset(self) -> None:
        """重置统计。"""
        self.total_triggers = 0
        self.total_executed = 0
        self.total_blocked_cooldown = 0
        self.total_blocked_condition = 0
        self.total_errors = 0
        self.by_trigger.clear()
        self.by_rule.clear()


# -------------------------------------------------------------------- #
# ReactCore 核心类
# -------------------------------------------------------------------- #


class ReactCore:
    """ReactCore 反应核心。

    逆向自 NovaBuilder 的 WavesAccess ReactCore。

    功能:
        1. 规则注册与管理
        2. 事件触发与分发
        3. 优先级排序
        4. 冷却机制
        5. 条件过滤
        6. 统计追踪

    使用示例::

        core = ReactCore()
        core.add_rule(
            name="greet_player",
            trigger=ReactTrigger.PLAYER_JOIN,
            action_func=lambda data: print(f"Hello {data['name']}!"),
            cooldown=30.0,
        )
        core.trigger("player_join", {"name": "Steve"})
    """

    def __init__(self) -> None:
        """初始化 ReactCore。"""
        self._rules: dict[str, ReactRule] = {}
        self._trigger_index: dict[str, list[str]] = {}  # trigger_name -> [rule_ids]
        self._stats: ReactStats = ReactStats()
        self._lock: threading.RLock = threading.RLock()
        self._global_handlers: list[Callable[[str, dict[str, Any]], None]] = []

        logger.debug("ReactCore initialized")

    @property
    def stats(self) -> ReactStats:
        """统计信息。"""
        return self._stats

    @property
    def rule_count(self) -> int:
        """规则数量。"""
        with self._lock:
            return len(self._rules)

    # ---------------------------------------------------------------- #
    # 规则管理
    # ---------------------------------------------------------------- #

    def add_rule(
        self,
        name: str,
        trigger: ReactTrigger | str,
        action_func: Callable[[dict[str, Any]], Any],
        condition: Callable[[dict[str, Any]], bool] | None = None,
        action: ReactAction = ReactAction.CALL_FUNCTION,
        priority: int = DEFAULT_PRIORITY,
        cooldown: float = DEFAULT_COOLDOWN,
        timeout: float = DEFAULT_TIMEOUT,
        rule_id: str | None = None,
    ) -> str:
        """添加反应规则。

        Args:
            name: 规则名称。
            trigger: 触发类型或自定义触发名称。
            action_func: 动作函数。
            condition: 过滤条件函数。
            action: 动作类型。
            priority: 优先级 (数值越小越高)。
            cooldown: 冷却时间 (秒)。
            timeout: 超时 (秒)。
            rule_id: 规则 ID, None 则自动生成。

        Returns:
            规则 ID。
        """
        import uuid as uuid_module

        rid = rule_id or str(uuid_module.uuid4())

        if isinstance(trigger, str):
            trigger_enum = ReactTrigger.CUSTOM
            trigger_name = trigger
        else:
            trigger_enum = trigger
            trigger_name = trigger.name.lower()

        rule = ReactRule(
            rule_id=rid,
            name=name,
            trigger=trigger_enum,
            trigger_name=trigger_name,
            condition=condition,
            action=action,
            action_func=action_func,
            priority=priority,
            cooldown=cooldown,
            timeout=timeout,
        )

        with self._lock:
            if len(self._rules) >= MAX_RULES:
                logger.warning("Max rules reached (%d), cannot add more", MAX_RULES)
                return ""

            self._rules[rid] = rule

            # 更新触发索引
            if trigger_name not in self._trigger_index:
                self._trigger_index[trigger_name] = []
            self._trigger_index[trigger_name].append(rid)

        logger.debug(
            "Added rule: %s (trigger=%s, priority=%d)",
            name, trigger_name, priority,
        )
        return rid

    def remove_rule(self, rule_id: str) -> bool:
        """移除规则。

        Args:
            rule_id: 规则 ID。

        Returns:
            True 如果成功移除。
        """
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                return False

            trigger_name = rule.effective_trigger_name
            self._rules.pop(rule_id, None)

            if trigger_name in self._trigger_index:
                try:
                    self._trigger_index[trigger_name].remove(rule_id)
                    if not self._trigger_index[trigger_name]:
                        del self._trigger_index[trigger_name]
                except ValueError:
                    pass

        logger.debug("Removed rule: %s (%s)", rule.name, rule_id)
        return True

    def remove_rule_by_name(self, name: str) -> int:
        """按名称移除规则。

        Args:
            name: 规则名称。

        Returns:
            移除的规则数。
        """
        with self._lock:
            to_remove = [rid for rid, rule in self._rules.items() if rule.name == name]

        for rid in to_remove:
            self.remove_rule(rid)

        logger.info("Removed %d rules by name: %s", len(to_remove), name)
        return len(to_remove)

    def get_rule(self, rule_id: str) -> ReactRule | None:
        """获取规则。

        Args:
            rule_id: 规则 ID。

        Returns:
            :class:`ReactRule`, 不存在返回 None。
        """
        with self._lock:
            return self._rules.get(rule_id)

    def get_rules_by_trigger(self, trigger_name: str) -> list[ReactRule]:
        """获取指定触发的所有规则。

        Args:
            trigger_name: 触发名称。

        Returns:
            规则列表 (按优先级排序)。
        """
        with self._lock:
            rule_ids = self._trigger_index.get(trigger_name.lower(), [])
            rules = [self._rules[rid] for rid in rule_ids if rid in self._rules]

        # 按优先级排序
        rules.sort(key=lambda r: r.priority)
        return rules

    def get_all_rules(self) -> list[ReactRule]:
        """获取所有规则。

        Returns:
            规则列表。
        """
        with self._lock:
            return list(self._rules.values())

    def enable_rule(self, rule_id: str) -> bool:
        """启用规则。

        Args:
            rule_id: 规则 ID。

        Returns:
            True 如果成功。
        """
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.enabled = True
                rule.state = RuleState.ACTIVE
                logger.debug("Enabled rule: %s", rule.name)
                return True
            return False

    def disable_rule(self, rule_id: str) -> bool:
        """禁用规则。

        Args:
            rule_id: 规则 ID。

        Returns:
            True 如果成功。
        """
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule:
                rule.enabled = False
                rule.state = RuleState.DISABLED
                logger.debug("Disabled rule: %s", rule.name)
                return True
            return False

    def clear_rules(self) -> None:
        """清除所有规则。"""
        with self._lock:
            count = len(self._rules)
            self._rules.clear()
            self._trigger_index.clear()
        logger.info("Cleared %d rules", count)

    # ---------------------------------------------------------------- #
    # 事件触发
    # ---------------------------------------------------------------- #

    def trigger(self, trigger_name: str, data: dict[str, Any] | None = None) -> int:
        """触发事件。

        遍历所有匹配的规则, 检查条件和冷却, 执行动作。

        Args:
            trigger_name: 触发名称。
            data: 事件数据。

        Returns:
            成功执行的规则数。
        """
        trigger_name = trigger_name.lower()
        data = data or {}
        self._stats.record_trigger(trigger_name)

        # 全局处理器
        with self._lock:
            global_handlers = list(self._global_handlers)
        for handler in global_handlers:
            try:
                handler(trigger_name, data)
            except Exception:
                logger.exception("Global handler error for trigger=%s", trigger_name)

        # 获取匹配的规则
        rules = self.get_rules_by_trigger(trigger_name)
        if not rules:
            return 0

        executed = 0
        for rule in rules:
            if not rule.enabled:
                continue

            # 检查冷却
            if rule.is_cooling_down:
                self._stats.total_blocked_cooldown += 1
                logger.debug(
                    "Rule %s blocked by cooldown (%.1fs remaining)",
                    rule.name,
                    rule.cooldown - (time.time() - rule.last_triggered),
                )
                continue

            # 检查条件
            if rule.condition:
                try:
                    if not rule.condition(data):
                        self._stats.total_blocked_condition += 1
                        continue
                except Exception as exc:
                    logger.warning("Rule %s condition error: %s", rule.name, exc)
                    rule.state = RuleState.ERROR
                    rule.last_error = str(exc)
                    self._stats.total_errors += 1
                    continue

            # 执行动作
            try:
                result = self._execute_rule(rule, data)
                rule.last_triggered = time.time()
                rule.trigger_count += 1
                rule.last_result = result
                rule.state = RuleState.ACTIVE
                rule.last_error = ""

                self._stats.record_execution(rule.name)
                executed += 1

                logger.debug(
                    "Rule executed: %s (trigger=%s)",
                    rule.name, trigger_name,
                )

            except Exception as exc:
                rule.state = RuleState.ERROR
                rule.last_error = str(exc)
                self._stats.total_errors += 1
                logger.warning("Rule %s execution error: %s", rule.name, exc)

        return executed

    def _execute_rule(self, rule: ReactRule, data: dict[str, Any]) -> Any:
        """执行规则动作。

        Args:
            rule: 规则。
            data: 事件数据。

        Returns:
            动作执行结果。
        """
        if rule.action_func is None:
            logger.warning("Rule %s has no action function", rule.name)
            return None

        # 根据动作类型执行
        if rule.action == ReactAction.CALL_FUNCTION:
            return rule.action_func(data)
        elif rule.action == ReactAction.SEND_COMMAND:
            # 动作函数应返回命令字符串
            command = rule.action_func(data)
            if isinstance(command, str) and command:
                logger.info("ReactCore command: %s", command)
                # 实际发送需要通过 cmd_sender
                return command
            return None
        elif rule.action == ReactAction.SEND_MESSAGE:
            message = rule.action_func(data)
            if isinstance(message, str) and message:
                logger.info("ReactCore message: %s", message)
                return message
            return None
        elif rule.action == ReactAction.LOG:
            message = rule.action_func(data)
            logger.info("ReactCore log [%s]: %s", rule.name, message)
            return message
        else:
            return rule.action_func(data)

    # ---------------------------------------------------------------- #
    # 全局处理器
    # ---------------------------------------------------------------- #

    def add_global_handler(
        self, handler: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """添加全局处理器 (接收所有触发)。

        Args:
            handler: 处理函数 (trigger_name, data)。
        """
        with self._lock:
            self._global_handlers.append(handler)
        logger.debug("Added global handler: %s", type(handler).__name__)

    def remove_global_handler(
        self, handler: Callable[[str, dict[str, Any]], None]
    ) -> None:
        """移除全局处理器。

        Args:
            handler: 处理函数。
        """
        with self._lock:
            try:
                self._global_handlers.remove(handler)
            except ValueError:
                pass

    # ---------------------------------------------------------------- #
    # 便捷规则注册
    # ---------------------------------------------------------------- #

    def on_player_join(
        self,
        action_func: Callable[[dict[str, Any]], Any],
        condition: Callable[[dict[str, Any]], bool] | None = None,
        cooldown: float = DEFAULT_COOLDOWN,
    ) -> str:
        """注册玩家加入规则。

        Args:
            action_func: 动作函数。
            condition: 过滤条件。
            cooldown: 冷却时间。

        Returns:
            规则 ID。
        """
        return self.add_rule(
            name="on_player_join",
            trigger=ReactTrigger.PLAYER_JOIN,
            action_func=action_func,
            condition=condition,
            cooldown=cooldown,
        )

    def on_player_leave(
        self,
        action_func: Callable[[dict[str, Any]], Any],
        condition: Callable[[dict[str, Any]], bool] | None = None,
        cooldown: float = DEFAULT_COOLDOWN,
    ) -> str:
        """注册玩家离开规则。

        Args:
            action_func: 动作函数。
            condition: 过滤条件。
            cooldown: 冷却时间。

        Returns:
            规则 ID。
        """
        return self.add_rule(
            name="on_player_leave",
            trigger=ReactTrigger.PLAYER_LEAVE,
            action_func=action_func,
            condition=condition,
            cooldown=cooldown,
        )

    def on_player_chat(
        self,
        action_func: Callable[[dict[str, Any]], Any],
        condition: Callable[[dict[str, Any]], bool] | None = None,
        cooldown: float = 0.0,
    ) -> str:
        """注册玩家聊天规则。

        Args:
            action_func: 动作函数。
            condition: 过滤条件。
            cooldown: 冷却时间。

        Returns:
            规则 ID。
        """
        return self.add_rule(
            name="on_player_chat",
            trigger=ReactTrigger.PLAYER_CHAT,
            action_func=action_func,
            condition=condition,
            cooldown=cooldown,
        )

    def on_server_message(
        self,
        action_func: Callable[[dict[str, Any]], Any],
        condition: Callable[[dict[str, Any]], bool] | None = None,
        cooldown: float = 0.0,
    ) -> str:
        """注册服务器消息规则。

        Args:
            action_func: 动作函数。
            condition: 过滤条件。
            cooldown: 冷却时间。

        Returns:
            规则 ID。
        """
        return self.add_rule(
            name="on_server_message",
            trigger=ReactTrigger.SERVER_MESSAGE,
            action_func=action_func,
            condition=condition,
            cooldown=cooldown,
        )

    def on_custom(
        self,
        trigger_name: str,
        action_func: Callable[[dict[str, Any]], Any],
        condition: Callable[[dict[str, Any]], bool] | None = None,
        cooldown: float = DEFAULT_COOLDOWN,
        priority: int = DEFAULT_PRIORITY,
    ) -> str:
        """注册自定义触发规则。

        Args:
            trigger_name: 自定义触发名称。
            action_func: 动作函数。
            condition: 过滤条件。
            cooldown: 冷却时间。
            priority: 优先级。

        Returns:
            规则 ID。
        """
        return self.add_rule(
            name=f"on_{trigger_name}",
            trigger=trigger_name,
            action_func=action_func,
            condition=condition,
            cooldown=cooldown,
            priority=priority,
        )

    # ---------------------------------------------------------------- #
    # 统计和诊断
    # ---------------------------------------------------------------- #

    def get_rule_stats(self, rule_id: str) -> dict[str, Any] | None:
        """获取规则统计。

        Args:
            rule_id: 规则 ID。

        Returns:
            统计字典, 不存在返回 None。
        """
        with self._lock:
            rule = self._rules.get(rule_id)
            if rule is None:
                return None

            return {
                "rule_id": rule.rule_id,
                "name": rule.name,
                "trigger": rule.effective_trigger_name,
                "state": rule.state.name,
                "enabled": rule.enabled,
                "priority": rule.priority,
                "trigger_count": rule.trigger_count,
                "last_triggered": rule.last_triggered,
                "is_cooling_down": rule.is_cooling_down,
                "last_error": rule.last_error,
            }

    def get_all_stats(self) -> dict[str, Any]:
        """获取所有统计信息。

        Returns:
            统计字典。
        """
        with self._lock:
            return {
                "total_rules": len(self._rules),
                "active_rules": sum(1 for r in self._rules.values() if r.enabled),
                "stats": {
                    "total_triggers": self._stats.total_triggers,
                    "total_executed": self._stats.total_executed,
                    "total_blocked_cooldown": self._stats.total_blocked_cooldown,
                    "total_blocked_condition": self._stats.total_blocked_condition,
                    "total_errors": self._stats.total_errors,
                },
                "by_trigger": dict(self._stats.by_trigger),
                "by_rule": dict(self._stats.by_rule),
            }

    def reset_stats(self) -> None:
        """重置统计。"""
        with self._lock:
            self._stats.reset()
            for rule in self._rules.values():
                rule.trigger_count = 0
                rule.last_triggered = 0.0
                rule.last_error = ""
        logger.info("ReactCore stats reset")


__all__ = [
    # 常量
    "DEFAULT_COOLDOWN", "DEFAULT_TIMEOUT", "MAX_RULES", "DEFAULT_PRIORITY",
    # 枚举
    "ReactTrigger", "ReactAction", "RuleState",
    # 数据结构
    "ReactRule", "ReactStats",
    # 核心
    "ReactCore",
]
