"""command_upgrader - 命令升级器。

逆向自 NexusEgo v1.6.5 的命令升级系统, 来源:

    - nexus/utils/api/commands_generator/upgrader.go
    - strings: "upgrade", "old version", "convert"

NexusE 支持将旧版本的 BDX 命令流升级到新版本:
    - 旧版 PlaceBlock (使用 block_data) -> PlaceBlockWithBlockStates
    - 旧版 BlockStates 字符串 -> 新版 BlockStates JSON
    - 旧版 CommandBlockData (无 needs_redstone) -> 新版 (有 needs_redstone)
    - 旧版 ChestData (固定 27 槽) -> 新版 (可变槽数)

升级规则 (逆向自 strings):
    - 1.6 beta -> 1.16: legacy_data -> block_states
    - 1.16 -> 1.17: 调色板更新
    - 1.17 -> 1.18: 深板岩系列方块
    - 1.18 -> 1.19: 潜声系列方块
    - 1.19 -> 1.20: 竹子系列方块
    - 1.20 -> 1.21: 试炼系列方块
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pocketterm.protocol.command_systems.command_upgrader")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class CommandUpgradeError(Exception):
    """命令升级错误。"""


# -------------------------------------------------------------------- #
# 升级规则
# -------------------------------------------------------------------- #

#: Minecraft 版本顺序 (逆向自 strings 中的版本字符串)
MC_VERSION_ORDER: list[str] = [
    "1.6 beta", "1.7", "1.8", "1.9", "1.10", "1.11", "1.12",
    "1.13", "1.14", "1.15", "1.16", "1.17", "1.18", "1.19",
    "1.20", "1.21", "1.21.60",
]

#: 升级规则表 (逆向自 strings: "upgrade from %v to %v")
#: 每条规则: (from_version, to_version, [changes])
COMMAND_UPGRADE_RULES: list[dict[str, Any]] = [
    {
        "from": "1.6 beta",
        "to": "1.13",
        "changes": [
            "PlaceBlock (block_data) -> PlaceBlockWithBlockStates (states JSON)",
            "block_data 字段被废弃, 使用 block_states 字符串",
        ],
    },
    {
        "from": "1.13",
        "to": "1.16",
        "changes": [
            "下界方块更新: netherrack, basalt, blackstone",
            "添加下界木质方块",
        ],
    },
    {
        "from": "1.16",
        "to": "1.17",
        "changes": [
            "深板岩系列方块: deepslate, deepslate_*",
            "铜系列方块: copper, cut_copper, waxed_copper",
            "避雷针: lightning_rod",
        ],
    },
    {
        "from": "1.17",
        "to": "1.18",
        "changes": [
            "深板岩矿物: deepslate_iron_ore, deepslate_gold_ore 等",
        ],
    },
    {
        "from": "1.18",
        "to": "1.19",
        "changes": [
            "潜声系列方块: sculk, sculk_sensor, sculk_catalyst, sculk_shrieker",
            "潜影贝新增方块",
        ],
    },
    {
        "from": "1.19",
        "to": "1.20",
        "changes": [
            "竹子系列方块: bamboo, bamboo_planks, bamboo_*",
            "樱花系列方块: cherry_*, cherry_leaves",
            "Suspicious sand/gravel",
        ],
    },
    {
        "from": "1.20",
        "to": "1.21",
        "changes": [
            "试炼系列方块: trial_spawner, vault, breeze_*",
            "重泥砖: mud_brick_*",
            "Tuff 系列: tuff_bricks, tuff_wall",
        ],
    },
    {
        "from": "1.21",
        "to": "1.21.60",
        "changes": [
            "新增 Pale Garden 系列: pale_oak_*, pale_moss",
            "新增 Creaking 系列",
        ],
    },
]

#: 旧版 block_data -> 新版 block_states 映射 (示例, 1.6 -> 1.13)
#: 逆向自 strings: "minecraft:stone" + 各种 data 值
LEGACY_BLOCK_DATA_UPGRADES: dict[str, dict[int, str]] = {
    # 石头系列
    "minecraft:stone": {
        0: '{"stone_type":"stone"}',
        1: '{"stone_type":"granite"}',
        2: '{"stone_type":"smooth_granite"}',
        3: '{"stone_type":"diorite"}',
        4: '{"stone_type":"smooth_diorite"}',
        5: '{"stone_type":"andesite"}',
        6: '{"stone_type":"smooth_andesite"}',
    },
    # 草方块
    "minecraft:grass": {
        0: '{}',
    },
    # 泥土系列
    "minecraft:dirt": {
        0: '{"dirt_type":"normal"}',
        1: '{"dirt_type":"coarse"}',
    },
    # 木头系列
    "minecraft:log": {
        0: '{"old_log_type":"oak","pillar_axis":"y"}',
        1: '{"old_log_type":"spruce","pillar_axis":"y"}',
        2: '{"old_log_type":"birch","pillar_axis":"y"}',
        3: '{"old_log_type":"jungle","pillar_axis":"y"}',
    },
    # 叶子系列
    "minecraft:leaves": {
        0: '{"old_leaf_type":"oak","persistent_bit":false,"update_bit":false}',
        1: '{"old_leaf_type":"spruce","persistent_bit":false,"update_bit":false}',
        2: '{"old_leaf_type":"birch","persistent_bit":false,"update_bit":false}',
        3: '{"old_leaf_type":"jungle","persistent_bit":false,"update_bit":false}',
    },
    # 楼梯
    "minecraft:stairs": {
        0: '{"upside_down_bit":false,"weirdo_direction":0}',
        1: '{"upside_down_bit":false,"weirdo_direction":1}',
        2: '{"upside_down_bit":false,"weirdo_direction":2}',
        3: '{"upside_down_bit":false,"weirdo_direction":3}',
        4: '{"upside_down_bit":true,"weirdo_direction":0}',
        5: '{"upside_down_bit":true,"weirdo_direction":1}',
        6: '{"upside_down_bit":true,"weirdo_direction":2}',
        7: '{"upside_down_bit":true,"weirdo_direction":3}',
    },
    # 半砖
    "minecraft:wooden_slab": {
        0: '{"minecraft:vertical_half":"bottom","wood_type":"oak"}',
        1: '{"minecraft:vertical_half":"bottom","wood_type":"spruce"}',
        2: '{"minecraft:vertical_half":"bottom","wood_type":"birch"}',
        3: '{"minecraft:vertical_half":"bottom","wood_type":"jungle"}',
        4: '{"minecraft:vertical_half":"bottom","wood_type":"acacia"}',
        5: '{"minecraft:vertical_half":"bottom","wood_type":"dark_oak"}',
        8: '{"minecraft:vertical_half":"top","wood_type":"oak"}',
        9: '{"minecraft:vertical_half":"top","wood_type":"spruce"}',
        10: '{"minecraft:vertical_half":"top","wood_type":"birch"}',
        11: '{"minecraft:vertical_half":"top","wood_type":"jungle"}',
        12: '{"minecraft:vertical_half":"top","wood_type":"acacia"}',
        13: '{"minecraft:vertical_half":"top","wood_type":"dark_oak"}',
    },
}

#: 旧版方块名升级映射 (1.16 -> 1.17+)
BLOCK_NAME_UPGRADES: dict[str, str] = {
    "minecraft:stonebrick": "minecraft:stone_bricks",
    "minecraft:wooden_door": "minecraft:oak_door",
    "minecraft:wooden_button": "minecraft:oak_button",
    "minecraft:wooden_pressure_plate": "minecraft:oak_pressure_plate",
    "minecraft:wooden_slab": "minecraft:oak_slab",
    "minecraft:double_wooden_slab": "minecraft:oak_double_slab",
    "minecraft:fence": "minecraft:oak_fence",
    "minecraft:fence_gate": "minecraft:oak_fence_gate",
    "minecraft:planks": "minecraft:oak_planks",
    "minecraft:wood": "minecraft:oak_wood",
}

#: CommandBlockData 升级 (1.13 -> 1.16+)
COMMAND_BLOCK_DATA_UPGRADES: list[str] = [
    # 1.13 -> 1.16: 新增 needs_redstone 字段
    "新增 needs_redstone 字段 (1.16+)",
    "新增 conditional 字段 (1.16+)",
    "新增 execute_on_first_tick 字段 (1.16+)",
    "tick_delay 类型从 int16 升级到 int32 (1.16+)",
]


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class UpgradeResult:
    """命令升级结果。"""
    success: bool = False
    from_version: str = ""
    to_version: str = ""
    upgraded_count: int = 0
    skipped_count: int = 0
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# -------------------------------------------------------------------- #
# 升级器
# -------------------------------------------------------------------- #


class CommandUpgrader:
    """命令升级器。

    逆向自 nexus/utils/api/commands_generator/upgrader.go。
    将旧版本的 BDump/CDump 命令流升级到新版本。
    """

    def __init__(self, target_version: str = "1.21.60") -> None:
        """初始化升级器。

        Args:
            target_version: 目标 Minecraft 版本。
        """
        if target_version not in MC_VERSION_ORDER:
            raise CommandUpgradeError(
                f"unknown target version: {target_version!r}. "
                f"Supported: {MC_VERSION_ORDER}"
            )
        self.target_version = target_version
        logger.debug("CommandUpgrader initialized: target=%s", target_version)

    def upgrade_bdump_stream(self, stream,
                              from_version: str = "1.6 beta") -> tuple[Any, UpgradeResult]:
        """升级 BDump 命令流。

        Args:
            stream: :class:`BDumpCommandStream` 实例。
            from_version: 源 Minecraft 版本。

        Returns:
            (升级后的流, 升级结果) 元组。
        """
        result = UpgradeResult(
            from_version=from_version,
            to_version=self.target_version,
        )

        upgraded_count = 0
        skipped_count = 0
        changes: list[str] = []
        warnings: list[str] = []

        # 找出从源版本到目标版本之间的所有升级步骤
        try:
            start_idx = MC_VERSION_ORDER.index(from_version)
            end_idx = MC_VERSION_ORDER.index(self.target_version)
        except ValueError as exc:
            raise CommandUpgradeError(f"invalid version: {exc}") from exc

        if start_idx > end_idx:
            warnings.append(
                f"downgrade not supported: {from_version} -> {self.target_version}"
            )

        # 收集升级规则
        applicable_rules: list[dict[str, Any]] = []
        for rule in COMMAND_UPGRADE_RULES:
            try:
                rule_from_idx = MC_VERSION_ORDER.index(rule["from"])
                rule_to_idx = MC_VERSION_ORDER.index(rule["to"])
            except ValueError:
                continue
            if rule_from_idx >= start_idx and rule_to_idx <= end_idx:
                applicable_rules.append(rule)
                changes.extend(rule["changes"])

        # 应用升级
        from .bdump_commands import COMMAND_ID_TO_NAME
        for cmd in stream.commands:
            name = cmd.name
            if name == "PlaceBlock":
                # 升级 PlaceBlock -> PlaceBlockWithBlockStates
                block_name = stream.context.get_constant_string(
                    cmd.data.get("block_id", 0)
                )
                block_data = cmd.data.get("block_data", 0)
                new_states = self._upgrade_block_data(block_name, block_data)
                # 更新命令
                cmd.command_id = COMMAND_ID_TO_NAME["PlaceBlockWithBlockStates"]
                cmd.name = "PlaceBlockWithBlockStates"
                cmd.data.pop("block_data", None)
                # 添加新的 BlockStates 常量字符串
                states_id = stream.context.add_constant_string(new_states)
                # 插入 CreateConstantString 命令需要修改命令流
                # 简化: 直接使用 states_id
                cmd.data["block_states_id"] = states_id
                upgraded_count += 1

            elif name == "PlaceBlockWithBlockStatesDeprecated":
                # 升级到 PlaceBlockWithBlockStates
                cmd.command_id = COMMAND_ID_TO_NAME["PlaceBlockWithBlockStates"]
                cmd.name = "PlaceBlockWithBlockStates"
                upgraded_count += 1

            elif name in ("SetCommandBlockData",
                          "PlaceBlockWithCommandBlockData",
                          "PlaceCommandBlockWithCommandBlockData",
                          "PlaceRuntimeBlockWithCommandBlockData",
                          "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID"):
                # 升级 CommandBlockData
                if "needs_redstone" not in cmd.data:
                    cmd.data["needs_redstone"] = False
                if "conditional" not in cmd.data:
                    cmd.data["conditional"] = False
                if "execute_on_first_tick" not in cmd.data:
                    cmd.data["execute_on_first_tick"] = False
                upgraded_count += 1

            elif name in ("PlaceBlockWithChestData",
                          "PlaceRuntimeBlockWithChestData",
                          "PlaceRuntimeBlockWithChestDataAndUint32RuntimeID"):
                # 容器数据升级
                chest_data = cmd.data.get("chest_data", {})
                # 确保有 unknown_field
                if "unknown_field" not in chest_data:
                    chest_data["unknown_field"] = 0
                upgraded_count += 1

            else:
                skipped_count += 1

        result.upgraded_count = upgraded_count
        result.skipped_count = skipped_count
        result.changes = changes
        result.warnings = warnings
        result.success = True

        logger.info(
            "BDump upgraded: %s -> %s, upgraded=%d, skipped=%d",
            from_version, self.target_version,
            upgraded_count, skipped_count,
        )
        return stream, result

    def _upgrade_block_data(self, block_name: str,
                              block_data: int) -> str:
        """升级旧版 block_data 到新版 block_states 字符串。

        Args:
            block_name: 方块名。
            block_data: 旧版 block_data 值。

        Returns:
            新版 block_states JSON 字符串。
        """
        upgrades = LEGACY_BLOCK_DATA_UPGRADES.get(block_name)
        if upgrades and block_data in upgrades:
            return upgrades[block_data]
        # 默认返回空状态
        return "{}"


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def upgrade_bdx_commands(stream, from_version: str = "1.6 beta",
                           target_version: str = "1.21.60") -> tuple[Any, UpgradeResult]:
    """升级 BDX 命令流。

    Args:
        stream: BDump 命令流。
        from_version: 源版本。
        target_version: 目标版本。

    Returns:
        (升级后的流, 升级结果) 元组。
    """
    upgrader = CommandUpgrader(target_version=target_version)
    return upgrader.upgrade_bdump_stream(stream, from_version=from_version)


def upgrade_command_block_data(data: dict[str, Any],
                                  target_version: str = "1.21.60") -> dict[str, Any]:
    """升级命令方块数据。

    Args:
        data: 命令方块数据字典。
        target_version: 目标版本。

    Returns:
        升级后的命令方块数据。
    """
    upgraded = dict(data)
    # 确保所有字段存在
    upgraded.setdefault("mode", 0)
    upgraded.setdefault("command", "")
    upgraded.setdefault("custom_name", "")
    upgraded.setdefault("last_output", "")
    upgraded.setdefault("tick_delay", 0)
    upgraded.setdefault("execute_on_first_tick", False)
    upgraded.setdefault("track_output", False)
    upgraded.setdefault("conditional", False)
    upgraded.setdefault("needs_redstone", False)
    return upgraded


def upgrade_legacy_block_data(block_name: str,
                                block_data: int,
                                target_version: str = "1.21.60") -> tuple[str, str]:
    """升级旧版方块数据到新版方块状态。

    Args:
        block_name: 旧版方块名。
        block_data: 旧版 block_data 值。
        target_version: 目标版本。

    Returns:
        (新方块名, 新方块状态 JSON 字符串) 元组。
    """
    # 方块名升级
    new_name = BLOCK_NAME_UPGRADES.get(block_name, block_name)
    # block_data 升级
    upgrades = LEGACY_BLOCK_DATA_UPGRADES.get(block_name)
    if upgrades and block_data in upgrades:
        new_states = upgrades[block_data]
    else:
        new_states = "{}"
    return new_name, new_states


__all__ = [
    "MC_VERSION_ORDER", "COMMAND_UPGRADE_RULES",
    "LEGACY_BLOCK_DATA_UPGRADES", "BLOCK_NAME_UPGRADES",
    "COMMAND_BLOCK_DATA_UPGRADES",
    "CommandUpgradeError",
    "UpgradeResult", "CommandUpgrader",
    "upgrade_bdx_commands", "upgrade_command_block_data",
    "upgrade_legacy_block_data",
]
