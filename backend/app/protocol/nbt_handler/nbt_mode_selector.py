"""nbt_mode_selector - NBT 模式选择器 (NovaBuilder)。

逆向自 NovaBuilder 的 NBT 模式选择逻辑, 来源:
    - nbt_mode_selector.txt (模式选择算法)
    - nbt_platform.txt (11x11 平台模式)
    - nbt_timing.txt (放置时序)

根据服务器类型、方块类型和 NBT 复杂度自动选择最佳放置模式。

三种放置模式 (逆向自 nbt_mode_selector.txt):
    1. STRUCTURE 模式:
        - 原理: 使用 BlockEntityData 数据包直接设置 NBT
        - 适用: 官方 Bedrock 服务器
        - 优点: 简单快速
        - 缺点: 网易反作弊可能拦截 BlockEntityData

    2. REPLACEITEM 模式:
        - 原理: 11x11 海晶灯平台 + InventoryContent 容器操作
        - 适用: 网易反作弊服务器
        - 优点: 可绕过 BlockEntityData 拦截
        - 缺点: 慢, 需要放置/清除平台

    3. PLACE_BLOCK_WITH_CHEST_DATA 模式:
        - 原理: 使用 BDump 命令 40 (PlaceBlockWithChestData)
        - 适用: 容器类方块 (箱子、漏斗等)
        - 优点: 一次性放置容器及其内容物
        - 缺点: 仅适用于容器类

模式选择算法 (逆向自 nbt_mode_selector.txt):
    1. 检测服务器类型 (官方/网易/未知)
    2. 检查方块类型 (容器/方块实体/普通)
    3. 检查 NBT 复杂度 (简单/复杂)
    4. 根据以上信息选择最佳模式

.. important::

    **网易 3.8 协议限制**: replaceitem 命令已被阉割, 只能放耐久/特殊值/数量/
    NBT 标签 (keep_on_death, item_lock), **不能放附魔/自定义名字**。
    因此模式选择器在网易 3.8 环境下默认推荐 STRUCTURE 模式
    (11x11 海晶灯平台 + structure save/load 搬运)。

字符串证据 (逆向自 nbt_mode_selector.txt):
    "auto detect server type"
    "netease -> REPLACEITEM"
    "official -> STRUCTURE"
    "container -> PLACE_BLOCK_WITH_CHEST_DATA"
    "complex nbt -> REPLACEITEM"
    "simple nbt -> STRUCTURE"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .nbt_placer import NBTPlacementMode, PlacementResult

logger = logging.getLogger("pocketterm.protocol.nbt_handler.nbt_mode_selector")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 服务器类型枚举 (字符串形式, 便于扩展)
SERVER_OFFICIAL: str = "official"
SERVER_NETEASE: str = "netease"
SERVER_UNKNOWN: str = "unknown"

#: 方块类型枚举
BLOCK_TYPE_CONTAINER: str = "container"
BLOCK_TYPE_ENTITY: str = "entity"
BLOCK_TYPE_NORMAL: str = "normal"

#: NBT 复杂度阈值
SIMPLE_NBT_MAX_FIELDS: int = 5  # 字段数 <= 5 视为简单 NBT
COMPLEX_NBT_MIN_SIZE: int = 100  # NBT 序列化后字节数 >= 100 视为复杂 NBT


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class ModeSelectionResult:
    """模式选择结果。

    Attributes:
        mode: 选中的放置模式。
        reason: 选择原因。
        server_type: 服务器类型。
        block_type: 方块类型。
        nbt_complexity: NBT 复杂度。
        confidence: 置信度 (0.0-1.0)。
    """
    mode: NBTPlacementMode = NBTPlacementMode.STRUCTURE
    reason: str = ""
    server_type: str = SERVER_UNKNOWN
    block_type: str = BLOCK_TYPE_NORMAL
    nbt_complexity: str = "simple"
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "mode": self.mode.name,
            "reason": self.reason,
            "server_type": self.server_type,
            "block_type": self.block_type,
            "nbt_complexity": self.nbt_complexity,
            "confidence": self.confidence,
        }


@dataclass
class ModeSelectorConfig:
    """模式选择器配置。

    Attributes:
        force_structure: 强制使用 STRUCTURE 模式。
        force_replaceitem: 强制使用 REPLACEITEM 模式。
        prefer_structure_for_netease: 网易服务器是否优先 STRUCTURE。
        container_mode_enabled: 是否启用容器专用模式。
        max_simple_nbt_fields: 简单 NBT 最大字段数。
    """
    force_structure: bool = False
    force_replaceitem: bool = False
    prefer_structure_for_netease: bool = True
    container_mode_enabled: bool = True
    max_simple_nbt_fields: int = SIMPLE_NBT_MAX_FIELDS


# -------------------------------------------------------------------- #
# NBT 模式选择器
# -------------------------------------------------------------------- #


class NBTModeSelector:
    """NBT 模式选择器。

    逆向自 NovaBuilder 的 nbt_mode_selector 实现。
    根据服务器类型、方块类型和 NBT 复杂度自动选择最佳放置模式。

    使用方式::

        selector = NBTModeSelector(server_type="netease")
        result = selector.select_mode(
            block_name="minecraft:chest",
            nbt={"Items": [...]},
        )
        mode = result.mode  # NBTPlacementMode.STRUCTURE (网易 3.8 推荐)

    Args:
        server_type: 服务器类型 ("official" / "netease" / "unknown")。
        config: 选择器配置 (None 使用默认)。
    """

    def __init__(
        self,
        server_type: str = SERVER_UNKNOWN,
        config: Optional[ModeSelectorConfig] = None,
    ) -> None:
        """初始化模式选择器。

        Args:
            server_type: 服务器类型。
            config: 选择器配置。
        """
        self.logger = logging.getLogger(
            "pocketterm.protocol.nbt_handler.nbt_mode_selector.selector"
        )
        self.server_type = server_type
        self.config = config if config else ModeSelectorConfig()
        # 历史选择记录 (用于统计)
        self._selection_history: list[ModeSelectionResult] = []
        # 服务器模式 (运行时调整)
        self.netease_3_8_restricted: bool = True

    def select_mode(
        self,
        block_name: str,
        nbt: dict[str, Any],
        block_states: Optional[dict[str, Any]] = None,
    ) -> ModeSelectionResult:
        """选择放置模式。

        Args:
            block_name: 方块名。
            nbt: NBT 数据。
            block_states: 方块状态 (可选)。

        Returns:
            :class:`ModeSelectionResult`。
        """
        result = ModeSelectionResult(server_type=self.server_type)

        # 1. 强制模式检查
        if self.config.force_structure:
            result.mode = NBTPlacementMode.STRUCTURE
            result.reason = "forced STRUCTURE mode"
            result.confidence = 1.0
            self.logger.debug("Forced STRUCTURE mode")
            self._record_selection(result)
            return result

        if self.config.force_replaceitem:
            result.mode = NBTPlacementMode.REPLACEITEM
            result.reason = "forced REPLACEITEM mode"
            result.confidence = 1.0
            self.logger.debug("Forced REPLACEITEM mode")
            self._record_selection(result)
            return result

        # 2. 检测方块类型
        result.block_type = self._detect_block_type(block_name)

        # 3. 检测 NBT 复杂度
        result.nbt_complexity = self._detect_nbt_complexity(nbt)

        # 4. 根据规则选择模式
        mode, reason, confidence = self._apply_selection_rules(result)
        result.mode = mode
        result.reason = reason
        result.confidence = confidence

        self.logger.info(
            "Selected mode: %s (reason=%s, confidence=%.2f, "
            "server=%s, block_type=%s, nbt_complexity=%s)",
            result.mode.name, result.reason, result.confidence,
            result.server_type, result.block_type, result.nbt_complexity,
        )

        self._record_selection(result)
        return result

    def _detect_block_type(self, block_name: str) -> str:
        """检测方块类型。

        Args:
            block_name: 方块名。

        Returns:
            BLOCK_TYPE_CONTAINER / BLOCK_TYPE_ENTITY / BLOCK_TYPE_NORMAL。
        """
        name = block_name.lower()

        # 容器类型
        container_blocks = {
            "minecraft:chest", "minecraft:trapped_chest",
            "minecraft:ender_chest", "minecraft:shulker_box",
            "minecraft:barrel", "minecraft:hopper",
            "minecraft:dispenser", "minecraft:dropper",
            "minecraft:furnace", "minecraft:blast_furnace",
            "minecraft:smoker", "minecraft:brewing_stand",
            "minecraft:crafter", "minecraft:chiseled_bookshelf",
        }
        # 潜影盒 (所有颜色)
        if "shulker_box" in name:
            return BLOCK_TYPE_CONTAINER
        if name in container_blocks:
            return BLOCK_TYPE_CONTAINER

        # 方块实体类型
        entity_blocks = {
            "minecraft:sign", "minecraft:standing_sign", "minecraft:wall_sign",
            "minecraft:standing_banner", "minecraft:wall_banner",
            "minecraft:lectern", "minecraft:jukebox",
            "minecraft:command_block", "minecraft:repeating_command_block",
            "minecraft:chain_command_block", "minecraft:structure_block",
            "minecraft:frame", "minecraft:glow_frame",
            "minecraft:beacon", "minecraft:enchanting_table",
            "minecraft:end_portal", "minecraft:mob_spawner",
            "minecraft:skull", "minecraft:player_head",
            "minecraft:conduit", "minecraft:jigsaw",
            "minecraft:calibrated_sculk_sensor", "minecraft:decorated_pot",
        }
        if "sign" in name or "banner" in name:
            return BLOCK_TYPE_ENTITY
        if "command_block" in name:
            return BLOCK_TYPE_ENTITY
        if name in entity_blocks:
            return BLOCK_TYPE_ENTITY

        return BLOCK_TYPE_NORMAL

    def _detect_nbt_complexity(self, nbt: dict[str, Any]) -> str:
        """检测 NBT 复杂度。

        Args:
            nbt: NBT 数据。

        Returns:
            "simple" 或 "complex"。
        """
        if not nbt:
            return "simple"

        field_count = len(nbt)
        try:
            import json
            nbt_size = len(json.dumps(nbt, default=str))
        except (TypeError, ValueError):
            nbt_size = 0

        if field_count > self.config.max_simple_nbt_fields:
            return "complex"
        if nbt_size >= COMPLEX_NBT_MIN_SIZE:
            return "complex"
        # 检查是否有嵌套列表/字典
        for value in nbt.values():
            if isinstance(value, (list, dict)) and value:
                return "complex"
        return "simple"

    def _apply_selection_rules(
        self, info: ModeSelectionResult
    ) -> tuple[NBTPlacementMode, str, float]:
        """应用选择规则。

        逆向自 nbt_mode_selector.txt 的选择算法:
            1. 网易服务器 + 复杂 NBT -> REPLACEITEM (3.8 阉割后受限)
            2. 网易服务器 + 简单 NBT -> STRUCTURE (3.8 推荐)
            3. 官方服务器 -> STRUCTURE
            4. 容器类 -> STRUCTURE (3.8 推荐, container_mode 在 3.8 下受限)

        .. important::

            网易 3.8 阉割了 replaceitem, 默认推荐 STRUCTURE 模式。
            仅在 NBT 极度复杂且服务器允许时才用 REPLACEITEM。
        """
        server = info.server_type
        block_type = info.block_type
        complexity = info.nbt_complexity

        # 官方服务器: 一律 STRUCTURE
        if server == SERVER_OFFICIAL:
            return (
                NBTPlacementMode.STRUCTURE,
                "official server, STRUCTURE recommended",
                0.95,
            )

        # 网易服务器
        if server == SERVER_NETEASE:
            # 网易 3.8 阉割了 replaceitem, 默认 STRUCTURE
            if self.netease_3_8_restricted:
                return (
                    NBTPlacementMode.STRUCTURE,
                    "NetEase 3.8 restricted, STRUCTURE recommended "
                    "(replaceitem castrated)",
                    0.9,
                )
            # 网易非 3.8: 复杂 NBT -> REPLACEITEM
            if complexity == "complex" and not self.config.prefer_structure_for_netease:
                return (
                    NBTPlacementMode.REPLACEITEM,
                    "NetEase server with complex NBT, REPLACEITEM recommended",
                    0.8,
                )
            # 网易非 3.8: 简单 NBT -> STRUCTURE
            return (
                NBTPlacementMode.STRUCTURE,
                "NetEase server with simple NBT, STRUCTURE recommended",
                0.85,
            )

        # 未知服务器: 默认 STRUCTURE (网易 3.8 推荐)
        return (
            NBTPlacementMode.STRUCTURE,
            "unknown server, STRUCTURE recommended (default for 3.8)",
            0.7,
        )

    def _record_selection(self, result: ModeSelectionResult) -> None:
        """记录选择历史。"""
        self._selection_history.append(result)
        # 仅保留最近 1000 条
        if len(self._selection_history) > 1000:
            self._selection_history = self._selection_history[-1000:]

    def get_statistics(self) -> dict[str, Any]:
        """获取选择统计。

        Returns:
            统计字典, 包含:
                - total: 总选择次数
                - structure_count: STRUCTURE 模式次数
                - replaceitem_count: REPLACEITEM 模式次数
                - structure_ratio: STRUCTURE 模式占比
        """
        total = len(self._selection_history)
        if total == 0:
            return {
                "total": 0,
                "structure_count": 0,
                "replaceitem_count": 0,
                "structure_ratio": 0.0,
            }
        structure_count = sum(
            1 for r in self._selection_history
            if r.mode == NBTPlacementMode.STRUCTURE
        )
        replaceitem_count = sum(
            1 for r in self._selection_history
            if r.mode == NBTPlacementMode.REPLACEITEM
        )
        return {
            "total": total,
            "structure_count": structure_count,
            "replaceitem_count": replaceitem_count,
            "structure_ratio": structure_count / total,
        }

    def set_server_type(self, server_type: str) -> None:
        """设置服务器类型。

        Args:
            server_type: 服务器类型 ("official" / "netease" / "unknown")。
        """
        self.server_type = server_type
        self.logger.info("Server type set to: %s", server_type)

    def set_netease_3_8_restricted(self, restricted: bool) -> None:
        """设置网易 3.8 限制标志。

        Args:
            restricted: True 表示网易 3.8 阉割了 replaceitem (默认)。
        """
        self.netease_3_8_restricted = restricted
        self.logger.info(
            "NetEase 3.8 restricted: %s", restricted
        )


# -------------------------------------------------------------------- #
# 便捷函数
# -------------------------------------------------------------------- #


def select_nbt_mode(
    block_name: str,
    nbt: dict[str, Any],
    server_type: str = SERVER_UNKNOWN,
) -> ModeSelectionResult:
    """选择 NBT 放置模式 (便捷函数)。

    Args:
        block_name: 方块名。
        nbt: NBT 数据。
        server_type: 服务器类型。

    Returns:
        :class:`ModeSelectionResult`。
    """
    selector = NBTModeSelector(server_type=server_type)
    return selector.select_mode(block_name, nbt)


__all__ = [
    # 常量
    "SERVER_OFFICIAL", "SERVER_NETEASE", "SERVER_UNKNOWN",
    "BLOCK_TYPE_CONTAINER", "BLOCK_TYPE_ENTITY", "BLOCK_TYPE_NORMAL",
    "SIMPLE_NBT_MAX_FIELDS", "COMPLEX_NBT_MIN_SIZE",
    # 数据结构
    "ModeSelectionResult", "ModeSelectorConfig",
    # 选择器
    "NBTModeSelector",
    # 便捷函数
    "select_nbt_mode",
]
