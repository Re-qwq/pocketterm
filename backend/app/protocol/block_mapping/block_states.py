"""block_states - 方块状态映射与管理。

合并自两个逆向包的方块状态处理层:

NexusEgo v1.6.5 (`block_mapping/block_states.py`):
    - 来源: WaterStructure/modules/nemc_convertor/ + data/block_mapping.json
    - 提供: BlockState / BlockStateMapping / BlockStateMapper 及顶层函数

NovaBuilder (`block_mapping/block_state.py`):
    - 来源: phoenixbuilder types/block.go + bedrock-world-operator/block
    - 提供: BlockStateDiff / BlockStateParser 及常见状态常量

方块状态 (Block States) 是 Minecraft 1.13+ 的方块属性系统,
取代了旧版的 block_data 数字系统。

示例::

    minecraft:oak_stairs {
        "direction": 0,
        "upside_down_bit": false,
        "weirdo_direction": 0
    }

适配 PocketTerm 项目结构, 适配网易我的世界 3.8 版本协议。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("pocketterm.protocol.block_mapping.block_states")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class BlockStateError(Exception):
    """方块状态错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class BlockState:
    """方块状态 (合并自 NexusEgo + NovaBuilder)。

    表示一个方块的完整状态, 包括方块名和状态属性。

    Attributes:
        name: 方块名 (如 "minecraft:stone")。
        states: 状态字典 (如 {"old_log_type": "oak", "pillar_axis": "y"})。
        version: 方块版本。
    """

    name: str = ""
    states: dict[str, Any] = field(default_factory=dict)
    version: int = 0

    # ---------------- 兼容属性 (NexusEgo 风格) ---------------- #

    @property
    def properties(self) -> "dict[str, Any]":
        """状态属性字典 (NexusEgo 兼容别名, 等价于 :attr:`states`)。"""
        return self.states

    @properties.setter
    def properties(self, value: "dict[str, Any]") -> None:
        self.states = value

    # ---------------- 通用方法 ---------------- #

    @property
    def is_air(self) -> bool:
        """是否为空气。"""
        return self.name == "minecraft:air" or not self.name

    def get(self, key: str, default: Any = None) -> Any:
        """获取状态/属性值。"""
        return self.states.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置状态/属性值。"""
        self.states[key] = value

    # ---------------- 序列化 ---------------- #

    def to_string(self) -> str:
        """转换为 JSON 字符串格式 (紧凑, 无空格)。

        逆向自 NexusEgo strings: "BlockStates" 字段处理。
        """
        if not self.states:
            return ""
        return json.dumps(self.states, separators=(",", ":"))

    def to_state_string(self) -> str:
        """转换为状态字符串 (JSON 数组格式, NovaBuilder 风格)。

        格式: [{"name":"key1","value":"value1"},{"name":"key2","value":1},...]
        """
        if not self.states:
            return "[]"

        items: "list[str]" = []
        for key, value in self.states.items():
            if isinstance(value, bool):
                value_str = "1" if value else "0"
            elif isinstance(value, str):
                value_str = f'"{value}"'
            elif isinstance(value, (int, float)):
                value_str = str(value)
            else:
                value_str = f'"{value}"'
            items.append(f'{{"name":"{key}","value":{value_str}}}')
        return "[" + ",".join(items) + "]"

    def to_dict(self) -> "dict[str, Any]":
        """转换为字典。"""
        return {
            "name": self.name,
            "states": dict(self.states),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: "dict[str, Any]") -> "BlockState":
        """从字典创建 BlockState。"""
        return cls(
            name=str(data.get("name", "")),
            states=dict(data.get("states", {})),
            version=int(data.get("version", 0)),
        )

    # ---------------- 比较 ---------------- #

    def __hash__(self) -> int:
        """计算哈希 (用于字典键)。"""
        return hash((self.name, self.to_state_string()))

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, BlockState):
            return False
        return self.name == other.name and self.states == other.states

    def __repr__(self) -> str:
        return f"BlockState(name={self.name!r}, states={self.states})"


@dataclass
class BlockStateMapping:
    """方块状态映射条目 (NexusEgo)。

    用于在不同版本/平台之间转换方块状态。
    """
    source_name: str = ""
    source_properties: "dict[str, Any]" = field(default_factory=dict)
    target_name: str = ""
    target_properties: "dict[str, Any]" = field(default_factory=dict)
    conversion_type: str = "exact"  # exact / fuzzy / manual


@dataclass
class BlockStateDiff:
    """方块状态差异 (NovaBuilder, 用于状态比较)。"""
    added: "dict[str, Any]" = field(default_factory=dict)
    removed: "dict[str, Any]" = field(default_factory=dict)
    changed: "dict[str, tuple[Any, Any]]" = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """是否无差异。"""
        return not (self.added or self.removed or self.changed)

    def to_dict(self) -> "dict[str, Any]":
        return {
            "added": dict(self.added),
            "removed": dict(self.removed),
            "changed": {k: list(v) for k, v in self.changed.items()},
        }


# -------------------------------------------------------------------- #
# 方块状态解析/序列化函数 (NexusEgo)
# -------------------------------------------------------------------- #


def parse_block_states(states_str: str) -> "dict[str, Any]":
    """解析方块状态 JSON 字符串。

    逆向自 strings: "BlockStates" 字段处理。

    Args:
        states_str: 方块状态 JSON 字符串 (如 '{"direction":0}')。

    Returns:
        状态字典。

    Raises:
        BlockStateError: 解析失败。
    """
    if not states_str:
        return {}
    try:
        result = json.loads(states_str)
        if not isinstance(result, dict):
            raise BlockStateError(f"states must be JSON object: {states_str!r}")
        return result
    except json.JSONDecodeError as exc:
        raise BlockStateError(f"invalid block states JSON: {exc}") from exc


def serialize_block_states(states: "dict[str, Any]") -> str:
    """序列化方块状态为 JSON 字符串。

    Args:
        states: 状态字典。

    Returns:
        JSON 字符串。
    """
    if not states:
        return ""
    return json.dumps(states, separators=(",", ":"))


def get_block_state_property(states: "dict[str, Any]", key: str,
                             default: Any = None) -> Any:
    """获取方块状态属性。

    Args:
        states: 状态字典。
        key: 属性键。
        default: 默认值。

    Returns:
        属性值。
    """
    return states.get(key, default)


def set_block_state_property(states: "dict[str, Any]", key: str,
                             value: Any) -> None:
    """设置方块状态属性。

    Args:
        states: 状态字典 (会被修改)。
        key: 属性键。
        value: 属性值。
    """
    states[key] = value


def compare_block_states(states1: "dict[str, Any]",
                         states2: "dict[str, Any]") -> bool:
    """比较两个方块状态是否相等。

    Args:
        states1: 第一个状态。
        states2: 第二个状态。

    Returns:
        True 如果相等。
    """
    if len(states1) != len(states2):
        return False
    for key, value in states1.items():
        if key not in states2:
            return False
        if states2[key] != value:
            return False
    return True


def merge_block_states(base: "dict[str, Any]",
                      override: "dict[str, Any]") -> "dict[str, Any]":
    """合并两个方块状态。

    override 中的属性覆盖 base 中的同名属性。

    Args:
        base: 基础状态。
        override: 覆盖状态。

    Returns:
        合并后的状态字典 (新字典, 不修改输入)。
    """
    result = dict(base)
    result.update(override)
    return result


# -------------------------------------------------------------------- #
# 方块状态解析器 (NovaBuilder)
# -------------------------------------------------------------------- #


class BlockStateParser:
    """方块状态解析器 (逆向自 bedrock-world-operator/block.BlockStates)。

    提供状态字符串解析、比较、合并、规范化等静态方法。
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger(
            "pocketterm.protocol.block_mapping.block_states.parser"
        )

    @staticmethod
    def parse_state_string(state_str: str) -> "dict[str, Any]":
        """解析状态字符串 (JSON 数组格式, NovaBuilder 风格)。

        输入: [{"name":"key1","value":"value1"},{"name":"key2","value":1}]
        输出: {"key1": "value1", "key2": 1}
        """
        if not state_str or state_str == "[]":
            return {}

        try:
            data = json.loads(state_str)
        except json.JSONDecodeError as e:
            logger.warning("Failed to parse state string: %s", e)
            return {}

        if not isinstance(data, list):
            return {}

        result: "dict[str, Any]" = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name is None:
                continue
            result[str(name)] = value
        return result

    @staticmethod
    def state_to_string(states: "dict[str, Any]") -> str:
        """将状态字典转换为字符串 (JSON 数组格式)。"""
        if not states:
            return "[]"

        items: "list[str]" = []
        for key, value in states.items():
            if isinstance(value, bool):
                value_str = "1" if value else "0"
            elif isinstance(value, str):
                value_str = f'"{value}"'
            elif isinstance(value, (int, float)):
                value_str = str(value)
            else:
                value_str = f'"{value}"'
            items.append(f'{{"name":"{key}","value":{value_str}}}')
        return "[" + ",".join(items) + "]"

    @staticmethod
    def compare_states(
        state1: "dict[str, Any]", state2: "dict[str, Any]"
    ) -> BlockStateDiff:
        """比较两个方块状态, 返回差异。"""
        diff = BlockStateDiff()

        # 添加的键
        for key in state2:
            if key not in state1:
                diff.added[key] = state2[key]

        # 删除的键
        for key in state1:
            if key not in state2:
                diff.removed[key] = state1[key]

        # 修改的键
        for key in state1:
            if key in state2 and state1[key] != state2[key]:
                diff.changed[key] = (state1[key], state2[key])

        return diff

    @staticmethod
    def merge_states(
        base: "dict[str, Any]", override: "dict[str, Any]"
    ) -> "dict[str, Any]":
        """合并方块状态 (override 优先)。"""
        result = dict(base)
        result.update(override)
        return result

    @staticmethod
    def normalize_value(value: Any) -> Any:
        """规范化状态值。

        - 布尔值: True/False 保持
        - 字符串: 保持
        - 整数: 转为 int
        - 浮点数: 转为 float (整数浮点转 int)
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return int(value)
        if isinstance(value, float):
            if value.is_integer():
                return int(value)
            return float(value)
        if isinstance(value, str):
            try:
                if "." in value:
                    return float(value)
                return int(value)
            except ValueError:
                return value
        return value

    @staticmethod
    def normalize_states(states: "dict[str, Any]") -> "dict[str, Any]":
        """规范化所有状态值。"""
        return {
            key: BlockStateParser.normalize_value(value)
            for key, value in states.items()
        }

    @staticmethod
    def create_from_block_name(
        block_name: str, states: Optional["dict[str, Any]"] = None
    ) -> BlockState:
        """从方块名创建 BlockState。"""
        return BlockState(
            name=block_name,
            states=states if states else {},
        )

    @staticmethod
    def is_same_block(state1: BlockState, state2: BlockState) -> bool:
        """判断两个 BlockState 是否为同一种方块 (不考虑状态)。"""
        return state1.name == state2.name

    @staticmethod
    def is_same_state(state1: BlockState, state2: BlockState) -> bool:
        """判断两个 BlockState 是否完全相同。"""
        return state1 == state2


# -------------------------------------------------------------------- #
# 常见方块状态常量 (逆向自 bedrock-world-operator/block)
# -------------------------------------------------------------------- #

#: 朝向方向状态值 (facing_direction)
FACING_DIRECTIONS: "list[int]" = [0, 1, 2, 3, 4, 5]

#: 朝向方向名称映射
FACING_DIRECTION_NAMES: "dict[int, str]" = {
    0: "down",
    1: "up",
    2: "north",
    3: "south",
    4: "west",
    5: "east",
}

#: 颜色状态值 (color)
COLOR_VALUES: "list[str]" = [
    "white", "orange", "magenta", "light_blue",
    "yellow", "lime", "pink", "gray",
    "silver", "cyan", "purple", "blue",
    "brown", "green", "red", "black",
]

#: 木头类型 (old_log_type)
LOG_TYPES: "list[str]" = ["oak", "spruce", "birch", "jungle"]

#: 叶子类型 (old_leaf_type)
LEAF_TYPES: "list[str]" = ["oak", "spruce", "birch", "jungle"]

#: 朝向轴 (pillar_axis)
PILLAR_AXES: "list[str]" = ["x", "y", "z"]

#: 半砖类型 (stone_slab_type)
SLAB_TYPES: "list[str]" = [
    "smooth_stone", "sandstone", "wood", "cobblestone",
    "brick", "stone_brick", "quartz", "nether_brick",
]

#: 楼梯朝向 (weirdo_direction)
STAIR_DIRECTIONS: "list[int]" = [0, 1, 2, 3]


# -------------------------------------------------------------------- #
# 方块状态映射器 (NexusEgo)
# -------------------------------------------------------------------- #


class BlockStateMapper:
    """方块状态映射器。

    逆向自 WaterStructure/modules/nemc_convertor/ 的状态映射逻辑。
    用于在不同版本/平台之间转换方块状态。
    """

    def __init__(self) -> None:
        self._mappings: "list[BlockStateMapping]" = []
        self._block_mapping: "dict[str, Any]" = {}

    def load_mapping(self, file_path: str) -> None:
        """加载方块映射文件。

        逆向自 strings: "LoadConvertRecord"。

        Args:
            file_path: 映射文件路径。
        """
        if not os.path.exists(file_path):
            raise BlockStateError(f"mapping file not found: {file_path}")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise BlockStateError(f"failed to load mapping: {exc}") from exc
        if not isinstance(data, dict):
            raise BlockStateError("invalid mapping format")
        self._block_mapping = data
        # 解析映射条目 (兼容多种键名)
        mappings = data.get("state_mappings", [])
        if isinstance(mappings, list):
            for m in mappings:
                if not isinstance(m, dict):
                    continue
                self._mappings.append(BlockStateMapping(
                    source_name=m.get("source_name", ""),
                    source_properties=m.get("source_properties", {}),
                    target_name=m.get("target_name", ""),
                    target_properties=m.get("target_properties", {}),
                    conversion_type=m.get("conversion_type", "exact"),
                ))
        logger.info(
            "BlockStateMapper loaded: %d mappings from %s",
            len(self._mappings), file_path,
        )

    def map_state(self, source: BlockState) -> BlockState:
        """映射方块状态。

        Args:
            source: 源方块状态。

        Returns:
            目标方块状态。
        """
        # 精确匹配
        for mapping in self._mappings:
            if (mapping.source_name == source.name and
                    compare_block_states(mapping.source_properties, source.states)):
                return BlockState(
                    name=mapping.target_name,
                    states=dict(mapping.target_properties),
                )
        # 模糊匹配 (只匹配名称)
        for mapping in self._mappings:
            if mapping.source_name == source.name:
                merged = merge_block_states(
                    mapping.target_properties, source.states
                )
                return BlockState(
                    name=mapping.target_name,
                    states=merged,
                )
        # 无匹配, 返回原状态
        return source

    def get_mapping_count(self) -> int:
        """获取映射条目数。"""
        return len(self._mappings)


# -------------------------------------------------------------------- #
# 全局映射器实例
# -------------------------------------------------------------------- #

#: 全局方块状态映射器
_global_mapper: Optional[BlockStateMapper] = None

#: 默认方块映射文件路径 (逆向自 strings: "block_mapping.json")
DEFAULT_MAPPING_FILE: str = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "..", "data", "block_mapping.json",
)


def _get_mapper() -> BlockStateMapper:
    """获取全局映射器实例。"""
    global _global_mapper
    if _global_mapper is None:
        _global_mapper = BlockStateMapper()
        if os.path.exists(DEFAULT_MAPPING_FILE):
            try:
                _global_mapper.load_mapping(DEFAULT_MAPPING_FILE)
            except BlockStateError as exc:
                logger.warning("failed to load default mapping: %s", exc)
    return _global_mapper


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def load_block_mapping_file(file_path: str) -> BlockStateMapper:
    """加载方块映射文件。

    Args:
        file_path: 映射文件路径。

    Returns:
        :class:`BlockStateMapper` 实例。
    """
    mapper = BlockStateMapper()
    mapper.load_mapping(file_path)
    return mapper


def get_block_mapping() -> BlockStateMapper:
    """获取全局方块映射器。"""
    return _get_mapper()


__all__ = [
    "BlockStateError",
    "BlockState", "BlockStateMapping", "BlockStateDiff",
    "BlockStateParser", "BlockStateMapper",
    "parse_block_states", "serialize_block_states",
    "get_block_state_property", "set_block_state_property",
    "compare_block_states", "merge_block_states",
    "load_block_mapping_file", "get_block_mapping",
    "DEFAULT_MAPPING_FILE",
    # 常量
    "FACING_DIRECTIONS", "FACING_DIRECTION_NAMES",
    "COLOR_VALUES", "LOG_TYPES", "LEAF_TYPES",
    "PILLAR_AXES", "SLAB_TYPES", "STAIR_DIRECTIONS",
]
