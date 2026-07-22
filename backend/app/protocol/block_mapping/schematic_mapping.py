"""schematic_mapping - Schematic 方块 ID 映射。

逆向自 NovaBuilder 对旧版 Schematic 文件的支持, 来源:
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/schematic.go

Schematic 旧版方块 ID (Java Edition 1.12 及以下):
    使用数字 ID (0-255) + 数据值 (0-15) 表示方块。

Bedrock Edition 方块名:
    使用 "minecraft:stone" + 方块状态表示方块。

Schematic -> Bedrock 映射:
    PhoenixBuilder 通过 SCHEMATIC_BLOCK_MAPPING 字典将旧版 ID 映射到 Bedrock 方块名。
    映射格式: (block_id, block_data) -> (block_name, block_states)

数据来源 (逆向自 strings):
    "WARNING - `schem' is deprecated and has been removed,
     please migrate to BDX format instead."

    PhoenixBuilder 实际已废弃此格式, 但仍保留映射表用于参考。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("pocketterm.protocol.block_mapping.schematic_mapping")


# -------------------------------------------------------------------- #
# Schematic 方块 ID 映射表 (旧版 Java ID -> Bedrock 方块名 + 状态)
# -------------------------------------------------------------------- #

#: Schematic 旧版方块 ID 映射 (block_id -> list[(block_data, bedrock_name, bedrock_states)])
#:
#: 数据来源: Minecraft Java 1.12 方块 ID 表 + Bedrock 转换
#: 完整映射表过于庞大, 这里列出常用方块
SCHEMATIC_BLOCK_MAPPING: dict[int, list[tuple[int, str, dict[str, Any]]]] = {
    # 0: 空气
    0: [(0, "minecraft:air", {})],

    # 1: 石头
    1: [
        (0, "minecraft:stone", {}),
        (1, "minecraft:granite", {}),
        (2, "minecraft:polished_granite", {}),
        (3, "minecraft:diorite", {}),
        (4, "minecraft:polished_diorite", {}),
        (5, "minecraft:andesite", {}),
        (6, "minecraft:polished_andesite", {}),
    ],

    # 2: 草方块
    2: [(0, "minecraft:grass", {})],

    # 3: 泥土
    3: [
        (0, "minecraft:dirt", {}),
        (1, "minecraft:coarse_dirt", {}),
        (2, "minecraft:podzol", {}),
    ],

    # 4: 圆石
    4: [(0, "minecraft:cobblestone", {})],

    # 5: 木板
    5: [
        (0, "minecraft:planks", {"wood_type": "oak"}),
        (1, "minecraft:planks", {"wood_type": "spruce"}),
        (2, "minecraft:planks", {"wood_type": "birch"}),
        (3, "minecraft:planks", {"wood_type": "jungle"}),
        (4, "minecraft:planks", {"wood_type": "acacia"}),
        (5, "minecraft:planks", {"wood_type": "dark_oak"}),
    ],

    # 7: 基岩
    7: [(0, "minecraft:bedrock", {})],

    # 8: 流动水
    8: [(0, "minecraft:flowing_water", {"liquid_depth": 0})],

    # 9: 水
    9: [(0, "minecraft:water", {"liquid_depth": 0})],

    # 10: 流动岩浆
    10: [(0, "minecraft:flowing_lava", {"liquid_depth": 0})],

    # 11: 岩浆
    11: [(0, "minecraft:lava", {"liquid_depth": 0})],

    # 12: 沙子
    12: [
        (0, "minecraft:sand", {}),
        (1, "minecraft:sand", {"sand_type": "red"}),
    ],

    # 13: 沙砾
    13: [(0, "minecraft:gravel", {})],

    # 14: 金矿石
    14: [(0, "minecraft:gold_ore", {})],

    # 15: 铁矿石
    15: [(0, "minecraft:iron_ore", {})],

    # 16: 煤矿石
    16: [(0, "minecraft:coal_ore", {})],

    # 17: 原木
    17: [
        (0, "minecraft:log", {"old_log_type": "oak", "pillar_axis": "y"}),
        (1, "minecraft:log", {"old_log_type": "spruce", "pillar_axis": "y"}),
        (2, "minecraft:log", {"old_log_type": "birch", "pillar_axis": "y"}),
        (3, "minecraft:log", {"old_log_type": "jungle", "pillar_axis": "y"}),
    ],

    # 18: 叶子
    18: [
        (0, "minecraft:leaves", {"old_leaf_type": "oak"}),
        (1, "minecraft:leaves", {"old_leaf_type": "spruce"}),
        (2, "minecraft:leaves", {"old_leaf_type": "birch"}),
        (3, "minecraft:leaves", {"old_leaf_type": "jungle"}),
    ],

    # 20: 玻璃
    20: [(0, "minecraft:glass", {})],

    # 21: 青金石矿石
    21: [(0, "minecraft:lapis_ore", {})],

    # 22: 青金石块
    22: [(0, "minecraft:lapis_block", {})],

    # 24: 沙岩
    24: [
        (0, "minecraft:sandstone", {}),
        (1, "minecraft:sandstone", {"sand_stone_type": "chiseled"}),
        (2, "minecraft:sandstone", {"sand_stone_type": "cut"}),
    ],

    # 35: 羊毛
    35: [
        (0,  "minecraft:wool", {"color": "white"}),
        (1,  "minecraft:wool", {"color": "orange"}),
        (2,  "minecraft:wool", {"color": "magenta"}),
        (3,  "minecraft:wool", {"color": "light_blue"}),
        (4,  "minecraft:wool", {"color": "yellow"}),
        (5,  "minecraft:wool", {"color": "lime"}),
        (6,  "minecraft:wool", {"color": "pink"}),
        (7,  "minecraft:wool", {"color": "gray"}),
        (8,  "minecraft:wool", {"color": "silver"}),
        (9,  "minecraft:wool", {"color": "cyan"}),
        (10, "minecraft:wool", {"color": "purple"}),
        (11, "minecraft:wool", {"color": "blue"}),
        (12, "minecraft:wool", {"color": "brown"}),
        (13, "minecraft:wool", {"color": "green"}),
        (14, "minecraft:wool", {"color": "red"}),
        (15, "minecraft:wool", {"color": "black"}),
    ],

    # 41: 金块
    41: [(0, "minecraft:gold_block", {})],

    # 42: 铁块
    42: [(0, "minecraft:iron_block", {})],

    # 45: 砖块
    45: [(0, "minecraft:brick_block", {})],

    # 46: TNT
    46: [(0, "minecraft:tnt", {})],

    # 49: 黑曜石
    49: [(0, "minecraft:obsidian", {})],

    # 54: 箱子
    54: [(0, "minecraft:chest", {"facing_direction": 2})],

    # 57: 钻石块
    57: [(0, "minecraft:diamond_block", {})],

    # 61: 熔炉
    61: [(0, "minecraft:furnace", {"facing_direction": 2})],

    # 62: 燃烧中的熔炉
    62: [(0, "minecraft:lit_furnace", {"facing_direction": 2})],

    # 73: 红石矿石
    73: [(0, "minecraft:redstone_ore", {})],

    # 74: 发光的红石矿石
    74: [(0, "minecraft:lit_redstone_ore", {})],

    # 89: 萤石
    89: [(0, "minecraft:glowstone", {})],

    # 95: 染色玻璃
    95: [(0, "minecraft:stained_glass", {"color": "white"})],

    # 98: 石砖
    98: [
        (0, "minecraft:stonebrick", {"stone_brick_type": "default"}),
        (1, "minecraft:stonebrick", {"stone_brick_type": "mossy"}),
        (2, "minecraft:stonebrick", {"stone_brick_type": "cracked"}),
        (3, "minecraft:stonebrick", {"stone_brick_type": "chiseled"}),
    ],

    # 110: 菌丝
    110: [(0, "minecraft:mycelium", {})],

    # 121: 末地石
    121: [(0, "minecraft:end_stone", {})],

    # 130: 末影箱
    130: [(0, "minecraft:ender_chest", {"facing_direction": 2})],

    # 152: 红石块
    152: [(0, "minecraft:redstone_block", {})],

    # 155: 石英块
    155: [
        (0, "minecraft:quartz_block", {"chisel_type": "default"}),
        (1, "minecraft:quartz_block", {"chisel_type": "chiseled"}),
        (2, "minecraft:quartz_block", {"chisel_type": "lines"}),
    ],

    # 159: 染色陶土
    159: [
        (0,  "minecraft:stained_hardened_clay", {"color": "white"}),
        (1,  "minecraft:stained_hardened_clay", {"color": "orange"}),
        (2,  "minecraft:stained_hardened_clay", {"color": "magenta"}),
        (3,  "minecraft:stained_hardened_clay", {"color": "light_blue"}),
        (4,  "minecraft:stained_hardened_clay", {"color": "yellow"}),
        (5,  "minecraft:stained_hardened_clay", {"color": "lime"}),
        (6,  "minecraft:stained_hardened_clay", {"color": "pink"}),
        (7,  "minecraft:stained_hardened_clay", {"color": "gray"}),
        (8,  "minecraft:stained_hardened_clay", {"color": "silver"}),
        (9,  "minecraft:stained_hardened_clay", {"color": "cyan"}),
        (10, "minecraft:stained_hardened_clay", {"color": "purple"}),
        (11, "minecraft:stained_hardened_clay", {"color": "blue"}),
        (12, "minecraft:stained_hardened_clay", {"color": "brown"}),
        (13, "minecraft:stained_hardened_clay", {"color": "green"}),
        (14, "minecraft:stained_hardened_clay", {"color": "red"}),
        (15, "minecraft:stained_hardened_clay", {"color": "black"}),
    ],

    # 169: 海晶灯
    169: [(0, "minecraft:sea_lantern", {})],

    # 173: 煤炭块
    173: [(0, "minecraft:coal_block", {})],

    # 174: 冰
    174: [(0, "minecraft:ice", {})],

    # 206: 灵魂土
    206: [(0, "minecraft:soul_soil", {})],
}


# -------------------------------------------------------------------- #
# Schematic 方块映射类
# -------------------------------------------------------------------- #


class SchematicBlockMapping:
    """Schematic 方块 ID 映射 (逆向自 PhoenixBuilder builder/schematic.go)。

    使用方式:
        name, states = SchematicBlockMapping.convert(1, 0)
        # name = "minecraft:stone", states = {}
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger("pocketterm.protocol.block_mapping.schematic_mapping.mapping")
        self._mapping: dict[int, list[tuple[int, str, dict[str, Any]]]] = (
            dict(SCHEMATIC_BLOCK_MAPPING)
        )

    @classmethod
    def convert(
        cls, block_id: int, block_data: int = 0
    ) -> tuple[str, dict[str, Any]]:
        """将 Schematic 旧版 ID 转换为 Bedrock 方块名 + 状态。

        Args:
            block_id: 旧版方块 ID (0-255)
            block_data: 旧版方块数据值 (0-15)

        Returns:
            (block_name, block_states) 二元组
        """
        if block_id in SCHEMATIC_BLOCK_MAPPING:
            mappings = SCHEMATIC_BLOCK_MAPPING[block_id]
            for data, name, states in mappings:
                if data == block_data:
                    return name, dict(states)
            # 未找到精确匹配, 返回第一个
            if mappings:
                _, name, states = mappings[0]
                logger.debug(
                    "No exact match for block_id=%d data=%d, using first: %s",
                    block_id, block_data, name,
                )
                return name, dict(states)

        # 未知方块, 返回石头
        logger.warning(
            "Unknown schematic block: id=%d data=%d, using stone", block_id, block_data
        )
        return "minecraft:stone", {}

    @classmethod
    def reverse_convert(
        cls, block_name: str, block_states: Optional[dict[str, Any]] = None
    ) -> tuple[int, int]:
        """反向转换: Bedrock 方块名 + 状态 -> Schematic 旧版 ID。

        Args:
            block_name: Bedrock 方块名
            block_states: 方块状态

        Returns:
            (block_id, block_data) 二元组
        """
        if block_states is None:
            block_states = {}

        # 空气特殊处理
        if block_name == "minecraft:air" or not block_name:
            return (0, 0)

        # 在映射表中查找
        for block_id, mappings in SCHEMATIC_BLOCK_MAPPING.items():
            for data, name, states in mappings:
                if name != block_name:
                    continue
                # 检查状态是否匹配
                if not states:
                    if not block_states:
                        return (block_id, data)
                    continue
                # 简化匹配: 检查所有 states 中的键值
                if all(states.get(k) == v for k, v in block_states.items()):
                    return (block_id, data)

        logger.warning(
            "Unknown Bedrock block: name=%s states=%s, using stone",
            block_name, block_states,
        )
        return (1, 0)

    @classmethod
    def has_mapping(cls, block_id: int) -> bool:
        """检查方块 ID 是否在映射表中。"""
        return block_id in SCHEMATIC_BLOCK_MAPPING

    @classmethod
    def get_all_mappings(cls) -> dict[int, list[tuple[int, str, dict[str, Any]]]]:
        """获取完整映射表。"""
        return dict(SCHEMATIC_BLOCK_MAPPING)

    @classmethod
    def get_block_count(cls) -> int:
        """获取映射表中方块数量。"""
        return len(SCHEMATIC_BLOCK_MAPPING)

    def add_mapping(
        self,
        block_id: int,
        block_data: int,
        bedrock_name: str,
        bedrock_states: dict[str, Any],
    ) -> None:
        """添加新的方块映射。"""
        if block_id not in self._mapping:
            self._mapping[block_id] = []
        # 检查是否已存在
        for i, (data, _, _) in enumerate(self._mapping[block_id]):
            if data == block_data:
                # 替换
                self._mapping[block_id][i] = (
                    block_data, bedrock_name, bedrock_states
                )
                return
        # 添加新映射
        self._mapping[block_id].append(
            (block_data, bedrock_name, bedrock_states)
        )
        self.logger.info(
            "Added mapping: id=%d data=%d -> %s %s",
            block_id, block_data, bedrock_name, bedrock_states,
        )


# -------------------------------------------------------------------- #
# 辅助函数
# -------------------------------------------------------------------- #


def get_block_name_from_legacy_id(
    block_id: int, block_data: int = 0
) -> tuple[str, dict[str, Any]]:
    """从旧版 ID 获取 Bedrock 方块名 + 状态。

    Args:
        block_id: 旧版方块 ID (0-255)
        block_data: 旧版方块数据值 (0-15)

    Returns:
        (block_name, block_states) 二元组
    """
    return SchematicBlockMapping.convert(block_id, block_data)


def get_legacy_id_from_block(
    block_name: str, block_states: Optional[dict[str, Any]] = None
) -> tuple[int, int]:
    """从 Bedrock 方块名获取旧版 ID。

    Args:
        block_name: Bedrock 方块名
        block_states: 方块状态

    Returns:
        (block_id, block_data) 二元组
    """
    return SchematicBlockMapping.reverse_convert(block_name, block_states)
