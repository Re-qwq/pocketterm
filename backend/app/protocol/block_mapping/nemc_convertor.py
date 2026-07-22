"""nemc_convertor - NEMC 方块 ID 转换。

逆向自 NexusEgo v1.6.5 的 NEMC 方块 ID 转换器, 来源:

    - WaterStructure/modules/nemc_convertor/        (NEMC 转换器)
    - nemc-tan-lobby-solver/minecraft/              (NEMC NBT)
    - strings_import.txt                             (导入字符串)

NEMC (NetEase Minecraft) 使用自己的方块 ID 系统, 与 Minecraft 原版不同。
NexusE 需要在导入/导出时进行 ID 转换。

ID 系统:
    - NEMC LID (Legacy ID): 旧版方块 ID (1.12 及以前, 数字 ID)
    - NEMC RID (Runtime ID): 运行时方块 ID (网易版)
    - NEMC Name: 方块名 (如 "minecraft:stone")
    - MC RID: Minecraft 原版运行时 ID

转换函数 (逆向自 strings_import.txt):
    NEMCToName(rid)            -- NEMC RID -> 方块名
    NEMCRidToVal(rid)          -- NEMC RID -> 旧版值
    NEMCRidToMCRid(rid)        -- NEMC RID -> MC RID
    MCRidToNEMCRid(mcRid)      -- MC RID -> NEMC RID
    NEMCAirRID                 -- NEMC 空气 RID 常量

转换器初始化 (逆向自 strings_import.txt):
    initNEMCBlocks()           -- 初始化 NEMC 方块表
    AddAnchorByLegacyValue()   -- 按旧版值添加锚点
    AddAnchorByState()         -- 按状态添加锚点
    LoadConvertRecord()        -- 加载转换记录
    LoadTargetBlock()          -- 加载目标方块

搜索函数 (逆向自 strings_import.txt):
    fuzzySearchByState()       -- 按状态模糊搜索
    fuzzySearchByLegacyValue() -- 按旧版值模糊搜索
    TryBestSearchByLegacyValue() -- 按旧版值最佳搜索
    TryBestSearchByState()     -- 按状态最佳搜索
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pocketterm.protocol.block_mapping.nemc_convertor")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: NEMC 空气方块 RID (逆向自 strings: "NEMCAirRID")
NEMC_AIR_RID: int = 0

#: NEMC 空气方块旧版 ID
NEMC_AIR_LEGACY_ID: int = 0

#: NEMC 空气方块名
NEMC_AIR_NAME: str = "minecraft:air"

#: NEMC 方块映射表文件 (逆向自 strings: "nemc_blocks.json")
NEMC_BLOCKS_FILE: str = "nemc_blocks.json"

#: NEMC 转换记录文件 (逆向自 strings: "convert_record.json")
NEMC_CONVERT_RECORD_FILE: str = "convert_record.json"


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class NEMCConvertorError(Exception):
    """NEMC 转换器错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class NEMCBlockAnchor:
    """NEMC 方块锚点。

    逆向自 strings: "AddAnchorByLegacyValue" / "AddAnchorByState"。
    锚点用于建立 NEMC ID 与 MC ID 之间的映射关系。
    """
    nemc_rid: int = 0
    nemc_legacy_id: int = 0
    nemc_legacy_data: int = 0
    mc_rid: int = 0
    block_name: str = ""
    block_states: dict[str, Any] = field(default_factory=dict)


@dataclass
class NEMCConvertRecord:
    """NEMC 转换记录。

    逆向自 strings: "LoadConvertRecord"。
    """
    source_nemc_rid: int = 0
    target_mc_rid: int = 0
    block_name: str = ""
    block_states: dict[str, Any] = field(default_factory=dict)
    conversion_method: str = ""  # exact / fuzzy / try_best


@dataclass
class NEMCBlock:
    """NEMC 方块定义。"""
    rid: int = 0
    legacy_id: int = 0
    legacy_data: int = 0
    name: str = ""
    states: dict[str, Any] = field(default_factory=dict)
    mc_rid: int = 0  # 对应的 MC RID


# -------------------------------------------------------------------- #
# NEMC 转换器
# -------------------------------------------------------------------- #


class NEMCConvertor:
    """NEMC 方块 ID 转换器。

    逆向自 WaterStructure/modules/nemc_convertor/。
    实现 NEMC ID 与 MC ID 之间的双向转换。

    转换流程:
        NEMC LID -> NEMC Name -> NEMC RID -> MC RID -> MC Name

    工作流程:
        1. initNEMCBlocks() 初始化方块表
        2. AddAnchorByLegacyValue() / AddAnchorByState() 添加锚点
        3. LoadConvertRecord() 加载转换记录
        4. NEMCToName() / NEMCRidToMCRid() 等执行转换
    """

    def __init__(self) -> None:
        self._blocks_by_rid: dict[int, NEMCBlock] = {}
        self._blocks_by_name: dict[str, NEMCBlock] = {}
        self._blocks_by_legacy: dict[tuple[int, int], NEMCBlock] = {}
        self._anchors: list[NEMCBlockAnchor] = []
        self._convert_records: list[NEMCConvertRecord] = []
        self._initialized: bool = False

    # ---------------- 便捷属性 (与 ToNEMCConvertor 统一) ---------------- #

    #: 空气方块 NEMC RID (类属性, 便于 ``NEMCConvertor.AIR_RID`` 访问)
    AIR_RID: int = NEMC_AIR_RID

    @property
    def block_count(self) -> int:
        """已注册方块数。"""
        return len(self._blocks_by_rid)

    def initialize(self, mapping_file: str | None = None) -> None:
        """初始化 NEMC 方块表。

        逆向自 strings: "initNEMCBlocks"。

        Args:
            mapping_file: 方块映射表文件路径 (可选)。
        """
        if mapping_file and os.path.exists(mapping_file):
            self._load_mapping_file(mapping_file)
        else:
            # 使用内置的简化映射表
            self._load_builtin_mapping()
        self._initialized = True
        logger.info(
            "NEMCConvertor initialized: %d blocks, %d anchors",
            len(self._blocks_by_rid), len(self._anchors),
        )

    def _load_mapping_file(self, file_path: str) -> None:
        """从文件加载映射表。

        支持以下合并后的 block_mapping.json 键名 (适配 PocketTerm 合并数据):

            - ``blocks`` (list): NexusEgo 原始列表格式
              ``[{"rid":..,"legacy_id":..,"name":..,"states":..,"mc_rid":..}, ...]``
            - ``nemc_blocks_by_rid`` (dict): NexusEgo RID 键控格式
              ``{"1": {"name":..,"nemc_rid":..,"legacy_id":..,...}, ...}``
            - ``nemc_blocks_by_name`` (dict): NovaBuilder 名称键控格式
              ``{"minecraft:stone": {"runtime_id":..,"legacy_id":..,"states":..}, ...}``
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise NEMCConvertorError(f"failed to load mapping: {exc}") from exc

        if not isinstance(data, dict):
            raise NEMCConvertorError("invalid mapping format")

        # 1. NexusEgo 原始列表格式 (data["blocks"] 为 list)
        blocks = data.get("blocks", [])
        if isinstance(blocks, list):
            for block_data in blocks:
                if not isinstance(block_data, dict):
                    continue
                block = NEMCBlock(
                    rid=block_data.get("rid", 0),
                    legacy_id=block_data.get("legacy_id", 0),
                    legacy_data=block_data.get("legacy_data", 0),
                    name=block_data.get("name", ""),
                    states=block_data.get("states", {}),
                    mc_rid=block_data.get("mc_rid", 0),
                )
                self._register_block(block)

        # 2. NexusEgo RID 键控格式 (合并后的 nemc_blocks_by_rid)
        blocks_by_rid = data.get("nemc_blocks_by_rid", {})
        if isinstance(blocks_by_rid, dict):
            for rid_key, block_data in blocks_by_rid.items():
                if not isinstance(block_data, dict):
                    continue
                try:
                    rid = int(rid_key)
                except (TypeError, ValueError):
                    continue
                block = NEMCBlock(
                    rid=rid,
                    legacy_id=int(block_data.get("legacy_id", rid)),
                    legacy_data=int(block_data.get("legacy_data", 0)),
                    name=str(block_data.get("name", "")),
                    states=block_data.get("states", {}),
                    mc_rid=int(block_data.get("mc_rid", rid)),
                )
                self._register_block(block)

        # 3. NovaBuilder 名称键控格式 (合并后的 nemc_blocks_by_name)
        blocks_by_name = data.get("nemc_blocks_by_name", {})
        if isinstance(blocks_by_name, dict):
            for name_key, block_data in blocks_by_name.items():
                if not isinstance(block_data, dict):
                    continue
                rid = int(block_data.get("runtime_id", 0))
                block = NEMCBlock(
                    rid=rid,
                    legacy_id=int(block_data.get("legacy_id", rid)),
                    legacy_data=int(block_data.get("legacy_data", 0)),
                    name=str(name_key),
                    states=block_data.get("states", {}),
                    mc_rid=rid,
                )
                self._register_block(block)

    def _load_builtin_mapping(self) -> None:
        """加载内置的简化映射表。"""
        # 常见方块的 NEMC <-> MC 映射
        builtin_blocks = [
            (0, 0, 0, "minecraft:air", {}, 0),
            (1, 1, 0, "minecraft:stone", {}, 1),
            (2, 2, 0, "minecraft:grass", {}, 2),
            (3, 3, 0, "minecraft:dirt", {}, 3),
            (4, 4, 0, "minecraft:cobblestone", {}, 4),
            (5, 5, 0, "minecraft:planks", {}, 5),
            (7, 7, 0, "minecraft:bedrock", {}, 7),
            (8, 8, 0, "minecraft:flowing_water", {}, 8),
            (9, 9, 0, "minecraft:water", {}, 9),
            (10, 10, 0, "minecraft:flowing_lava", {}, 10),
            (11, 11, 0, "minecraft:lava", {}, 11),
            (12, 12, 0, "minecraft:sand", {}, 12),
            (13, 13, 0, "minecraft:gravel", {}, 13),
            (14, 14, 0, "minecraft:gold_ore", {}, 14),
            (15, 15, 0, "minecraft:iron_ore", {}, 15),
            (16, 16, 0, "minecraft:coal_ore", {}, 16),
            (17, 17, 0, "minecraft:log", {}, 17),
            (18, 18, 0, "minecraft:leaves", {}, 18),
            (20, 20, 0, "minecraft:glass", {}, 20),
            (24, 24, 0, "minecraft:sandstone", {}, 24),
            (35, 35, 0, "minecraft:wool", {}, 35),
            (41, 41, 0, "minecraft:gold_block", {}, 41),
            (42, 42, 0, "minecraft:iron_block", {}, 42),
            (43, 43, 0, "minecraft:double_stone_slab", {}, 43),
            (44, 44, 0, "minecraft:stone_slab", {}, 44),
            (45, 45, 0, "minecraft:brick_block", {}, 45),
            (46, 46, 0, "minecraft:tnt", {}, 46),
            (47, 47, 0, "minecraft:bookshelf", {}, 47),
            (48, 48, 0, "minecraft:mossy_cobblestone", {}, 48),
            (49, 49, 0, "minecraft:obsidian", {}, 49),
            (50, 50, 0, "minecraft:torch", {}, 50),
            (54, 54, 0, "minecraft:chest", {}, 54),
            (57, 57, 0, "minecraft:diamond_block", {}, 57),
            (58, 58, 0, "minecraft:crafting_table", {}, 58),
            (61, 61, 0, "minecraft:furnace", {}, 61),
            (62, 62, 0, "minecraft:lit_furnace", {}, 62),
            (64, 64, 0, "minecraft:wooden_door", {}, 64),
            (65, 65, 0, "minecraft:ladder", {}, 65),
            (66, 66, 0, "minecraft:rail", {}, 66),
            (67, 67, 0, "minecraft:stone_stairs", {}, 67),
            (71, 71, 0, "minecraft:iron_door", {}, 71),
            (73, 73, 0, "minecraft:redstone_ore", {}, 73),
            (74, 74, 0, "minecraft:lit_redstone_ore", {}, 74),
            (76, 76, 0, "minecraft:redstone_torch", {}, 76),
            (77, 77, 0, "minecraft:stone_button", {}, 77),
            (78, 78, 0, "minecraft:snow_layer", {}, 78),
            (79, 79, 0, "minecraft:ice", {}, 79),
            (80, 80, 0, "minecraft:snow", {}, 80),
            (82, 82, 0, "minecraft:clay", {}, 82),
            (84, 84, 0, "minecraft:jukebox", {}, 84),
            (89, 89, 0, "minecraft:glowstone", {}, 89),
            (95, 95, 0, "minecraft:stained_glass", {}, 95),
            (98, 98, 0, "minecraft:stonebrick", {}, 98),
            (103, 103, 0, "minecraft:melon_block", {}, 103),
            (121, 121, 0, "minecraft:end_stone", {}, 121),
            (123, 123, 0, "minecraft:redstone_lamp", {}, 123),
            (124, 124, 0, "minecraft:lit_redstone_lamp", {}, 124),
            (130, 130, 0, "minecraft:ender_chest", {}, 130),
            (133, 133, 0, "minecraft:emerald_block", {}, 133),
            (137, 137, 0, "minecraft:command_block", {}, 137),
            (138, 138, 0, "minecraft:beacon", {}, 138),
            (152, 152, 0, "minecraft:redstone_block", {}, 152),
            (155, 155, 0, "minecraft:quartz_block", {}, 155),
            (159, 159, 0, "minecraft:stained_hardened_clay", {}, 159),
            (165, 165, 0, "minecraft:slime", {}, 165),
            (170, 170, 0, "minecraft:hay_block", {}, 170),
            (172, 172, 0, "minecraft:hardened_clay", {}, 172),
            (173, 173, 0, "minecraft:coal_block", {}, 173),
            (174, 174, 0, "minecraft:packed_ice", {}, 174),
            (188, 188, 0, "minecraft:spruce_fence", {}, 188),
            (189, 189, 0, "minecraft:birch_fence", {}, 189),
            (190, 190, 0, "minecraft:jungle_fence", {}, 190),
            (191, 191, 0, "minecraft:dark_oak_fence", {}, 191),
            (192, 192, 0, "minecraft:acacia_fence", {}, 192),
            (216, 216, 0, "minecraft:magma", {}, 216),
            (217, 217, 0, "minecraft:nether_wart_block", {}, 217),
            (218, 218, 0, "minecraft:red_nether_brick", {}, 218),
            (235, 235, 0, "minecraft:bone_block", {}, 235),
            # 1.16+ 方块
            (-1, 0, 0, "minecraft:netherrack", {}, 0),
            (-2, 0, 0, "minecraft:basalt", {}, 0),
            (-3, 0, 0, "minecraft:blackstone", {}, 0),
            # 1.17+ 方块
            (-4, 0, 0, "minecraft:deepslate", {}, 0),
            (-5, 0, 0, "minecraft:copper_ore", {}, 0),
            (-6, 0, 0, "minecraft:lightning_rod", {}, 0),
            # 1.19+ 方块
            (-7, 0, 0, "minecraft:sculk", {}, 0),
            (-8, 0, 0, "minecraft:sculk_sensor", {}, 0),
            (-9, 0, 0, "minecraft:sculk_catalyst", {}, 0),
            (-10, 0, 0, "minecraft:sculk_shrieker", {}, 0),
        ]
        for rid, lid, ldata, name, states, mc_rid in builtin_blocks:
            block = NEMCBlock(
                rid=rid, legacy_id=lid, legacy_data=ldata,
                name=name, states=states, mc_rid=mc_rid if mc_rid else rid,
            )
            self._register_block(block)

    def _register_block(self, block: NEMCBlock) -> None:
        """注册方块。"""
        self._blocks_by_rid[block.rid] = block
        if block.name:
            self._blocks_by_name[block.name] = block
        self._blocks_by_legacy[(block.legacy_id, block.legacy_data)] = block

    # ---------------- 转换函数 ---------------- #

    def nemc_to_name(self, rid: int) -> str:
        """NEMC RID 转方块名。

        逆向自 strings: "NEMCToName"。
        """
        self._ensure_initialized()
        block = self._blocks_by_rid.get(rid)
        if block is None:
            raise NEMCConvertorError(f"unknown NEMC RID: {rid}")
        return block.name

    def nemc_rid_to_value(self, rid: int) -> tuple[int, int]:
        """NEMC RID 转旧版值 (legacy_id, legacy_data)。

        逆向自 strings: "NEMCRidToVal"。
        """
        self._ensure_initialized()
        block = self._blocks_by_rid.get(rid)
        if block is None:
            raise NEMCConvertorError(f"unknown NEMC RID: {rid}")
        return (block.legacy_id, block.legacy_data)

    def nemc_rid_to_mc_rid(self, rid: int) -> int:
        """NEMC RID 转 MC RID。

        逆向自 strings: "NEMCRidToMCRid"。
        """
        self._ensure_initialized()
        block = self._blocks_by_rid.get(rid)
        if block is None:
            raise NEMCConvertorError(f"unknown NEMC RID: {rid}")
        return block.mc_rid

    def mc_rid_to_nemc_rid(self, mc_rid: int) -> int:
        """MC RID 转 NEMC RID。

        逆向自 strings: "MCRidToNEMCRid"。
        """
        self._ensure_initialized()
        for rid, block in self._blocks_by_rid.items():
            if block.mc_rid == mc_rid:
                return rid
        raise NEMCConvertorError(f"unknown MC RID: {mc_rid}")

    def nemc_legacy_to_rid(self, legacy_id: int, legacy_data: int = 0) -> int:
        """NEMC 旧版值转 RID。

        逆向自 strings: "AddAnchorByLegacyValue"。
        """
        self._ensure_initialized()
        block = self._blocks_by_legacy.get((legacy_id, legacy_data))
        if block is not None:
            return block.rid
        # 尝试只按 legacy_id 匹配
        for block in self._blocks_by_rid.values():
            if block.legacy_id == legacy_id:
                return block.rid
        raise NEMCConvertorError(
            f"unknown NEMC legacy ID: {legacy_id}:{legacy_data}"
        )

    # ---------------- 锚点管理 ---------------- #

    def add_anchor_by_legacy_value(self, legacy_id: int, legacy_data: int,
                                      nemc_rid: int, mc_rid: int,
                                      block_name: str = "") -> None:
        """按旧版值添加锚点。

        逆向自 strings: "AddAnchorByLegacyValue"。
        """
        anchor = NEMCBlockAnchor(
            nemc_rid=nemc_rid,
            nemc_legacy_id=legacy_id,
            nemc_legacy_data=legacy_data,
            mc_rid=mc_rid,
            block_name=block_name,
        )
        self._anchors.append(anchor)
        logger.debug(
            "Anchor added (legacy): %d:%d -> NEMC %d / MC %d",
            legacy_id, legacy_data, nemc_rid, mc_rid,
        )

    def add_anchor_by_state(self, block_name: str,
                               states: dict[str, Any],
                               nemc_rid: int, mc_rid: int) -> None:
        """按状态添加锚点。

        逆向自 strings: "AddAnchorByState"。
        """
        anchor = NEMCBlockAnchor(
            nemc_rid=nemc_rid,
            mc_rid=mc_rid,
            block_name=block_name,
            block_states=states,
        )
        self._anchors.append(anchor)
        logger.debug(
            "Anchor added (state): %s %s -> NEMC %d / MC %d",
            block_name, states, nemc_rid, mc_rid,
        )

    # ---------------- 搜索函数 ---------------- #

    def fuzzy_search_by_state(self, block_name: str,
                                states: dict[str, Any]) -> NEMCBlock | None:
        """按状态模糊搜索。

        逆向自 strings: "fuzzySearchByState"。
        忽略 states 中的次要字段, 匹配主要字段。

        Args:
            block_name: 方块名。
            states: 方块状态。

        Returns:
            匹配的 :class:`NEMCBlock`, 如果未找到则返回 None。
        """
        self._ensure_initialized()
        # 精确匹配
        block = self._blocks_by_name.get(block_name)
        if block is not None:
            return block
        # 模糊匹配: 忽略大小写
        for name, b in self._blocks_by_name.items():
            if name.lower() == block_name.lower():
                return b
        # 模糊匹配: 部分匹配
        for name, b in self._blocks_by_name.items():
            if block_name in name or name in block_name:
                return b
        return None

    def fuzzy_search_by_legacy_value(self, legacy_id: int,
                                        legacy_data: int = 0) -> NEMCBlock | None:
        """按旧版值模糊搜索。

        逆向自 strings: "fuzzySearchByLegacyValue"。
        """
        self._ensure_initialized()
        # 精确匹配
        block = self._blocks_by_legacy.get((legacy_id, legacy_data))
        if block is not None:
            return block
        # 只按 legacy_id 匹配
        for b in self._blocks_by_rid.values():
            if b.legacy_id == legacy_id:
                return b
        return None

    def try_best_search_by_legacy_value(self, legacy_id: int,
                                            legacy_data: int = 0) -> NEMCBlock:
        """按旧版值最佳搜索 (必须找到)。

        逆向自 strings: "TryBestSearchByLegacyValue"。
        如果精确匹配失败, 使用模糊匹配, 如果仍然失败, 返回空气方块。
        """
        block = self.fuzzy_search_by_legacy_value(legacy_id, legacy_data)
        if block is not None:
            return block
        # 返回空气方块作为回退
        return self._blocks_by_rid.get(NEMC_AIR_RID, NEMCBlock(
            rid=NEMC_AIR_RID, name=NEMC_AIR_NAME,
        ))

    def try_best_search_by_state(self, block_name: str,
                                    states: dict[str, Any]) -> NEMCBlock:
        """按状态最佳搜索 (必须找到)。

        逆向自 strings: "TryBestSearchByState"。
        """
        block = self.fuzzy_search_by_state(block_name, states)
        if block is not None:
            return block
        return self._blocks_by_rid.get(NEMC_AIR_RID, NEMCBlock(
            rid=NEMC_AIR_RID, name=NEMC_AIR_NAME,
        ))

    # ---------------- 记录管理 ---------------- #

    def load_convert_record(self, file_path: str) -> None:
        """加载转换记录。

        逆向自 strings: "LoadConvertRecord"。
        """
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise NEMCConvertorError(f"failed to load record: {exc}") from exc
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    self._convert_records.append(NEMCConvertRecord(
                        source_nemc_rid=item.get("source_nemc_rid", 0),
                        target_mc_rid=item.get("target_mc_rid", 0),
                        block_name=item.get("block_name", ""),
                        block_states=item.get("block_states", {}),
                        conversion_method=item.get("conversion_method", ""),
                    ))

    def load_target_block(self, block_name: str) -> NEMCBlock | None:
        """加载目标方块。

        逆向自 strings: "LoadTargetBlock"。
        """
        self._ensure_initialized()
        return self._blocks_by_name.get(block_name)

    def _ensure_initialized(self) -> None:
        """确保已初始化。"""
        if not self._initialized:
            self.initialize()


# -------------------------------------------------------------------- #
# 全局转换器实例
# -------------------------------------------------------------------- #

#: 全局 NEMC 转换器实例
_global_convertor: NEMCConvertor | None = None


def _get_convertor() -> NEMCConvertor:
    """获取全局转换器实例。"""
    global _global_convertor
    if _global_convertor is None:
        _global_convertor = NEMCConvertor()
        _global_convertor.initialize()
    return _global_convertor


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def nemc_to_name(rid: int) -> str:
    """NEMC RID 转方块名。"""
    return _get_convertor().nemc_to_name(rid)


def nemc_rid_to_value(rid: int) -> tuple[int, int]:
    """NEMC RID 转旧版值。"""
    return _get_convertor().nemc_rid_to_value(rid)


def nemc_rid_to_mc_rid(rid: int) -> int:
    """NEMC RID 转 MC RID。"""
    return _get_convertor().nemc_rid_to_mc_rid(rid)


def mc_rid_to_nemc_rid(mc_rid: int) -> int:
    """MC RID 转 NEMC RID。"""
    return _get_convertor().mc_rid_to_nemc_rid(mc_rid)


def nemc_legacy_to_rid(legacy_id: int, legacy_data: int = 0) -> int:
    """NEMC 旧版值转 RID。"""
    return _get_convertor().nemc_legacy_to_rid(legacy_id, legacy_data)


def add_anchor_by_legacy_value(legacy_id: int, legacy_data: int,
                                  nemc_rid: int, mc_rid: int,
                                  block_name: str = "") -> None:
    """按旧版值添加锚点。"""
    _get_convertor().add_anchor_by_legacy_value(
        legacy_id, legacy_data, nemc_rid, mc_rid, block_name,
    )


def add_anchor_by_state(block_name: str, states: dict[str, Any],
                          nemc_rid: int, mc_rid: int) -> None:
    """按状态添加锚点。"""
    _get_convertor().add_anchor_by_state(
        block_name, states, nemc_rid, mc_rid,
    )


def fuzzy_search_by_state(block_name: str,
                            states: dict[str, Any]) -> NEMCBlock | None:
    """按状态模糊搜索。"""
    return _get_convertor().fuzzy_search_by_state(block_name, states)


def fuzzy_search_by_legacy_value(legacy_id: int,
                                    legacy_data: int = 0) -> NEMCBlock | None:
    """按旧版值模糊搜索。"""
    return _get_convertor().fuzzy_search_by_legacy_value(legacy_id, legacy_data)


def try_best_search_by_legacy_value(legacy_id: int,
                                        legacy_data: int = 0) -> NEMCBlock:
    """按旧版值最佳搜索。"""
    return _get_convertor().try_best_search_by_legacy_value(legacy_id, legacy_data)


def try_best_search_by_state(block_name: str,
                                states: dict[str, Any]) -> NEMCBlock:
    """按状态最佳搜索。"""
    return _get_convertor().try_best_search_by_state(block_name, states)


def load_convert_record(file_path: str) -> None:
    """加载转换记录。"""
    _get_convertor().load_convert_record(file_path)


def load_target_block(block_name: str) -> NEMCBlock | None:
    """加载目标方块。"""
    return _get_convertor().load_target_block(block_name)


def init_nemc_blocks(mapping_file: str | None = None) -> None:
    """初始化 NEMC 方块表。"""
    global _global_convertor
    _global_convertor = NEMCConvertor()
    _global_convertor.initialize(mapping_file)


# -------------------------------------------------------------------- #
# NovaBuilder 部分: ToNEMCConvertor + RuntimeIDPool
# (合并自 NovaBuilder block_mapping/to_nemc_convertor.py)
#
# 逆向自 PhoenixBuilder 的 ToNEMCConvertor, 来源:
#     - phoenixbuilder/fastbuilder/bdump/block/to_nemc_convertor.go
#     - phoenixbuilder/fastbuilder/bdump/command/use_runtime_id_pool.go
#
# 与 NexusEgo 的 NEMCConvertor 互补:
#     - NEMCConvertor: NEMC RID <-> 方块名 <-> 旧版值 <-> MC RID (静态表)
#     - ToNEMCConvertor: 方块名+状态 -> 运行时 ID (动态池, 117/118)
# -------------------------------------------------------------------- #

#: 运行时 ID 池编号 (逆向自 use_runtime_id_pool.go)
POOL_ID_117: int = 117
POOL_ID_118: int = 118

#: 空气方块运行时 ID (逆向自 bedrock-world-operator/block.AirRuntimeID)
AIR_RUNTIME_ID: int = 0

#: 空气方块名
AIR_BLOCK_NAME: str = "minecraft:air"

#: 默认方块版本 (Bedrock 1.20+)
DEFAULT_BLOCK_VERSION: int = 18000312

#: 最大方块名长度
MAX_BLOCK_NAME_LENGTH: int = 256

#: 最大方块状态数
MAX_BLOCK_STATES: int = 64


@dataclass
class NEMCBlockMapping:
    """NEMC 方块映射 (逆向自 bedrock-world-operator/block.Block)。

    表示一个方块的完整信息:
        - name: 方块名 (如 "minecraft:stone")
        - states: 方块状态字典 (如 {"old_log_type": "oak"})
        - version: 方块版本
        - runtime_id: 运行时 ID (服务器分配)
    """
    name: str = ""
    states: dict[str, Any] = field(default_factory=dict)
    version: int = DEFAULT_BLOCK_VERSION
    runtime_id: int = 0

    @property
    def is_air(self) -> bool:
        """是否为空气方块。"""
        return (
            self.runtime_id == AIR_RUNTIME_ID
            or self.name == AIR_BLOCK_NAME
        )

    def to_block_state_hash(self) -> str:
        """生成方块状态哈希键 (用于查找)。

        格式: "name|state1=value1,state2=value2,..."
        """
        if not self.states:
            return self.name
        states_str = ",".join(
            f"{k}={self._state_value_to_str(v)}"
            for k, v in sorted(self.states.items())
        )
        return f"{self.name}|{states_str}"

    @staticmethod
    def _state_value_to_str(value: Any) -> str:
        """将状态值转换为字符串。"""
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, (int, float)):
            return str(value)
        return str(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "states": dict(self.states),
            "version": self.version,
            "runtime_id": self.runtime_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "NEMCBlockMapping":
        return cls(
            name=str(data.get("name", "")),
            states=dict(data.get("states", {})),
            version=int(data.get("version", DEFAULT_BLOCK_VERSION)),
            runtime_id=int(data.get("runtime_id", 0)),
        )

    def __repr__(self) -> str:
        return (
            f"NEMCBlockMapping(name={self.name!r}, "
            f"states={self.states}, runtime_id={self.runtime_id})"
        )


class RuntimeIDPool:
    """运行时 ID 池 (逆向自 use_runtime_id_pool.go)。

    存储方块名 + 状态 -> 运行时 ID 的映射表。

    两个池:
        - 池 117: Bedrock 1.17 运行时 ID 表
        - 池 118: Bedrock 1.18+ 运行时 ID 表
    """

    def __init__(self, pool_id: int = POOL_ID_117) -> None:
        """初始化运行时 ID 池。

        Args:
            pool_id: 池 ID (117 或 118)
        """
        self.pool_id: int = pool_id
        self._mapping: dict[str, NEMCBlockMapping] = {}
        self._reverse_mapping: dict[int, NEMCBlockMapping] = {}
        self.logger = logging.getLogger(
            "pocketterm.protocol.block_mapping.runtime_id_pool"
        )

        # 初始化空气方块
        air = NEMCBlockMapping(
            name=AIR_BLOCK_NAME,
            states={},
            version=DEFAULT_BLOCK_VERSION,
            runtime_id=AIR_RUNTIME_ID,
        )
        self._mapping[air.to_block_state_hash()] = air
        self._reverse_mapping[AIR_RUNTIME_ID] = air

    def register_block(
        self,
        name: str,
        states: dict[str, Any],
        runtime_id: int,
        version: int = DEFAULT_BLOCK_VERSION,
    ) -> None:
        """注册方块到运行时 ID 池。

        Args:
            name: 方块名
            states: 方块状态
            runtime_id: 运行时 ID
            version: 方块版本
        """
        block = NEMCBlockMapping(
            name=name,
            states=dict(states),
            version=version,
            runtime_id=runtime_id,
        )
        key = block.to_block_state_hash()
        self._mapping[key] = block
        self._reverse_mapping[runtime_id] = block
        self.logger.debug(
            "Registered block: %s -> runtime_id=%d", key, runtime_id
        )

    def get_runtime_id(
        self, name: str, states: dict[str, Any] | None = None
    ) -> int | None:
        """通过方块名 + 状态查询运行时 ID。

        Args:
            name: 方块名
            states: 方块状态 (可选)

        Returns:
            运行时 ID (None 表示未找到)
        """
        if states is None:
            states = {}

        # 创建临时 Block 用于生成哈希键
        block = NEMCBlockMapping(name=name, states=states)
        key = block.to_block_state_hash()

        # 直接查找
        if key in self._mapping:
            return self._mapping[key].runtime_id

        # 尝试无状态查找 (某些方块状态可以省略)
        if states:
            block_no_states = NEMCBlockMapping(name=name, states={})
            no_states_key = block_no_states.to_block_state_hash()
            if no_states_key in self._mapping:
                self.logger.debug(
                    "Found block %s without states (requested: %s)",
                    name, states,
                )
                return self._mapping[no_states_key].runtime_id

        self.logger.warning("Block not found in pool %d: %s", self.pool_id, key)
        return None

    def get_block_by_runtime_id(
        self, runtime_id: int
    ) -> NEMCBlockMapping | None:
        """通过运行时 ID 反查方块信息。"""
        return self._reverse_mapping.get(runtime_id)

    def load_from_block_palette(
        self, block_palette: list[dict[str, Any]]
    ) -> None:
        """从 BlockPalette 加载方块映射表。

        BlockPalette 是 StartPlayPacket 中的 NBT 数据:
            [
                {"name": "minecraft:air", "states": {}, "version": 18000312},
                {"name": "minecraft:stone", "states": {}, "version": 18000312},
                ...
            ]

        运行时 ID 是数组索引。

        Args:
            block_palette: 方块调色板列表
        """
        self.logger.info(
            "Loading %d blocks from palette to pool %d",
            len(block_palette), self.pool_id,
        )

        self._mapping.clear()
        self._reverse_mapping.clear()

        for runtime_id, block_data in enumerate(block_palette):
            if not isinstance(block_data, dict):
                continue
            name = str(block_data.get("name", ""))
            states = block_data.get("states", {})
            if not isinstance(states, dict):
                states = {}
            version = int(block_data.get("version", DEFAULT_BLOCK_VERSION))

            self.register_block(
                name=name,
                states=states,
                runtime_id=runtime_id,
                version=version,
            )

        self.logger.info(
            "Loaded %d blocks to pool %d", len(self._mapping), self.pool_id
        )

    @property
    def block_count(self) -> int:
        """已注册方块数。"""
        return len(self._mapping)

    def list_blocks(self) -> list[str]:
        """列出所有方块名。"""
        return sorted({block.name for block in self._mapping.values()})

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "pool_id": self.pool_id,
            "block_count": self.block_count,
            "blocks": [
                block.to_dict() for block in self._mapping.values()
            ],
        }


class ToNEMCConvertor:
    """ToNEMCConvertor (逆向自 PhoenixBuilder 的 ToNEMCConvertor)。

    使用方块名 + 状态查询网易中国版运行时 ID。支持 117/118 两个池。

    使用方式::

        convertor = ToNEMCConvertor()
        convertor.set_pool(POOL_ID_118)
        runtime_id = convertor.convert("minecraft:stone", {})
        # runtime_id = 1
    """

    def __init__(self, default_pool_id: int = POOL_ID_117) -> None:
        """初始化转换器。

        Args:
            default_pool_id: 默认池 ID (117 或 118)
        """
        self.logger = logging.getLogger(
            "pocketterm.protocol.block_mapping.to_nemc_convertor"
        )
        self._pools: dict[int, RuntimeIDPool] = {}
        self._current_pool_id: int = default_pool_id
        self._pools[default_pool_id] = RuntimeIDPool(default_pool_id)
        self._init_default_blocks(self._pools[default_pool_id])

    def set_pool(self, pool_id: int) -> None:
        """设置当前使用的运行时 ID 池 (逆向自 UseRuntimeIDPool 命令)。

        Args:
            pool_id: 池 ID (117 或 118)
        """
        if pool_id not in self._pools:
            self._pools[pool_id] = RuntimeIDPool(pool_id)
            self._init_default_blocks(self._pools[pool_id])

        old_pool = self._current_pool_id
        self._current_pool_id = pool_id
        self.logger.info(
            "Switched runtime ID pool: %d -> %d", old_pool, pool_id
        )

    @property
    def current_pool(self) -> RuntimeIDPool:
        """当前使用的运行时 ID 池。"""
        return self._pools[self._current_pool_id]

    @property
    def current_pool_id(self) -> int:
        """当前池 ID。"""
        return self._current_pool_id

    def convert(
        self,
        name: str,
        states: dict[str, Any] | None = None,
    ) -> int | None:
        """将方块名 + 状态转换为运行时 ID。

        逆向自 to_nemc_convertor.go:
            func (c *ToNEMCConvertor) Convert(name string, states map[string]interface{}) (uint32, error)

        Args:
            name: 方块名 (如 "minecraft:stone")
            states: 方块状态 (可选)

        Returns:
            运行时 ID (None 表示未找到)
        """
        if not name:
            return AIR_RUNTIME_ID

        # 空气方块特殊处理
        if name == AIR_BLOCK_NAME:
            return AIR_RUNTIME_ID

        # 查询当前池
        runtime_id = self.current_pool.get_runtime_id(name, states)
        if runtime_id is not None:
            return runtime_id

        # 如果当前池是 117, 尝试 118
        if self._current_pool_id == POOL_ID_117 and POOL_ID_118 in self._pools:
            runtime_id = self._pools[POOL_ID_118].get_runtime_id(name, states)
            if runtime_id is not None:
                self.logger.debug(
                    "Found in pool 118 (not in 117): %s", name
                )
                return runtime_id

        # 如果当前池是 118, 尝试 117
        if self._current_pool_id == POOL_ID_118 and POOL_ID_117 in self._pools:
            runtime_id = self._pools[POOL_ID_117].get_runtime_id(name, states)
            if runtime_id is not None:
                self.logger.debug(
                    "Found in pool 117 (not in 118): %s", name
                )
                return runtime_id

        self.logger.warning(
            "Block not found in any pool: %s (states=%s)", name, states
        )
        return None

    def convert_or_air(
        self,
        name: str,
        states: dict[str, Any] | None = None,
    ) -> int:
        """转换方块名到运行时 ID, 失败时返回空气。"""
        runtime_id = self.convert(name, states)
        if runtime_id is None:
            return AIR_RUNTIME_ID
        return runtime_id

    def reverse_convert(self, runtime_id: int) -> NEMCBlockMapping | None:
        """通过运行时 ID 反查方块信息。

        逆向自 bedrock-world-operator/block.RuntimeIDToState:
            func RuntimeIDToState(runtimeID uint32) (name string, properties map[string]interface{}, found bool)
        """
        # 在当前池中查找
        block = self.current_pool.get_block_by_runtime_id(runtime_id)
        if block is not None:
            return block

        # 在所有池中查找
        for pool_id, pool in self._pools.items():
            if pool_id == self._current_pool_id:
                continue
            block = pool.get_block_by_runtime_id(runtime_id)
            if block is not None:
                return block

        return None

    def load_block_palette(
        self,
        block_palette: list[dict[str, Any]],
        pool_id: int | None = None,
    ) -> None:
        """加载方块调色板。

        Args:
            block_palette: 方块调色板列表 (来自 StartPlayPacket)
            pool_id: 目标池 ID (None 使用当前池)
        """
        if pool_id is None:
            pool_id = self._current_pool_id

        if pool_id not in self._pools:
            self._pools[pool_id] = RuntimeIDPool(pool_id)

        self._pools[pool_id].load_from_block_palette(block_palette)
        self.logger.info(
            "Loaded block palette to pool %d: %d blocks",
            pool_id, self._pools[pool_id].block_count,
        )

    def _init_default_blocks(self, pool: RuntimeIDPool) -> None:
        """初始化默认方块 (网易中国版 1.20+).

        这些是网易服务器常用的方块运行时 ID (近似值)。
        实际使用时应通过 load_block_palette 加载服务器返回的真实映射。
        """
        default_blocks: list[tuple[str, dict[str, Any], int]] = [
            ("minecraft:air", {}, 0),
            ("minecraft:stone", {}, 1),
            ("minecraft:granite", {}, 2),
            ("minecraft:polished_granite", {}, 3),
            ("minecraft:diorite", {}, 4),
            ("minecraft:polished_diorite", {}, 5),
            ("minecraft:andesite", {}, 6),
            ("minecraft:polished_andesite", {}, 7),
            ("minecraft:grass", {}, 8),
            ("minecraft:dirt", {}, 9),
            ("minecraft:coarse_dirt", {}, 10),
            ("minecraft:podzol", {}, 11),
            ("minecraft:bedrock", {}, 12),
            ("minecraft:sand", {}, 13),
            ("minecraft:gravel", {}, 14),
            ("minecraft:gold_ore", {}, 15),
            ("minecraft:iron_ore", {}, 16),
            ("minecraft:coal_ore", {}, 17),
            ("minecraft:log", {"old_log_type": "oak", "pillar_axis": "y"}, 18),
            ("minecraft:log", {"old_log_type": "spruce", "pillar_axis": "y"}, 19),
            ("minecraft:log", {"old_log_type": "birch", "pillar_axis": "y"}, 20),
            ("minecraft:log", {"old_log_type": "jungle", "pillar_axis": "y"}, 21),
            ("minecraft:leaves", {"old_leaf_type": "oak"}, 22),
            ("minecraft:leaves", {"old_leaf_type": "spruce"}, 23),
            ("minecraft:leaves", {"old_leaf_type": "birch"}, 24),
            ("minecraft:leaves", {"old_leaf_type": "jungle"}, 25),
            ("minecraft:glass", {}, 26),
            ("minecraft:lapis_ore", {}, 27),
            ("minecraft:lapis_block", {}, 28),
            ("minecraft:sandstone", {}, 29),
            ("minecraft:wool", {"color": "white"}, 35),
            ("minecraft:wool", {"color": "orange"}, 36),
            ("minecraft:wool", {"color": "magenta"}, 37),
            ("minecraft:wool", {"color": "light_blue"}, 38),
            ("minecraft:wool", {"color": "yellow"}, 39),
            ("minecraft:wool", {"color": "lime"}, 40),
            ("minecraft:wool", {"color": "pink"}, 41),
            ("minecraft:wool", {"color": "gray"}, 42),
            ("minecraft:wool", {"color": "silver"}, 43),
            ("minecraft:wool", {"color": "cyan"}, 44),
            ("minecraft:wool", {"color": "purple"}, 45),
            ("minecraft:wool", {"color": "blue"}, 46),
            ("minecraft:wool", {"color": "brown"}, 47),
            ("minecraft:wool", {"color": "green"}, 48),
            ("minecraft:wool", {"color": "red"}, 49),
            ("minecraft:wool", {"color": "black"}, 50),
            ("minecraft:gold_block", {}, 51),
            ("minecraft:iron_block", {}, 52),
            ("minecraft:brick_block", {}, 55),
            ("minecraft:tnt", {}, 56),
            ("minecraft:obsidian", {}, 59),
            ("minecraft:chest", {"facing_direction": 0}, 74),
            ("minecraft:diamond_block", {}, 77),
            ("minecraft:furnace", {"facing_direction": 0}, 81),
            ("minecraft:lit_furnace", {"facing_direction": 0}, 82),
            ("minecraft:glowstone", {}, 109),
            ("minecraft:sea_lantern", {}, 169),
            ("minecraft:coal_block", {}, 173),
            ("minecraft:redstone_block", {}, 152),
            ("minecraft:quartz_block", {"chisel_type": "default"}, 155),
            ("minecraft:stained_hardened_clay", {"color": "white"}, 159),
            ("minecraft:end_stone", {}, 121),
            ("minecraft:soul_soil", {}, 206),
        ]

        for name, states, runtime_id in default_blocks:
            pool.register_block(
                name=name,
                states=states,
                runtime_id=runtime_id,
            )

        self.logger.info(
            "Initialized %d default blocks in pool %d",
            len(default_blocks), pool.pool_id,
        )

    @property
    def stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        return {
            "current_pool_id": self._current_pool_id,
            "pool_count": len(self._pools),
            "current_pool_blocks": self.current_pool.block_count,
        }


#: 全局 ToNEMCConvertor 实例
_global_to_nemc_convertor: ToNEMCConvertor | None = None


def _get_to_nemc_convertor() -> ToNEMCConvertor:
    """获取全局 ToNEMCConvertor 实例。"""
    global _global_to_nemc_convertor
    if _global_to_nemc_convertor is None:
        _global_to_nemc_convertor = ToNEMCConvertor()
    return _global_to_nemc_convertor


def to_nemc_runtime_id(
    name: str, states: dict[str, Any] | None = None
) -> int | None:
    """将方块名 + 状态转换为 NEMC 运行时 ID (顶层快捷函数)。"""
    return _get_to_nemc_convertor().convert(name, states)


def to_nemc_runtime_id_or_air(
    name: str, states: dict[str, Any] | None = None
) -> int:
    """将方块名 + 状态转换为 NEMC 运行时 ID, 失败返回空气。"""
    return _get_to_nemc_convertor().convert_or_air(name, states)


__all__ = [
    # 异常
    "NEMCConvertorError",
    # NexusEgo 常量
    "NEMC_AIR_RID", "NEMC_AIR_LEGACY_ID", "NEMC_AIR_NAME",
    # NexusEgo 数据结构
    "NEMCBlockAnchor", "NEMCConvertRecord", "NEMCBlock",
    # NexusEgo 转换器
    "NEMCConvertor",
    "nemc_to_name", "nemc_rid_to_value",
    "nemc_rid_to_mc_rid", "mc_rid_to_nemc_rid",
    "nemc_legacy_to_rid",
    "add_anchor_by_legacy_value", "add_anchor_by_state",
    "fuzzy_search_by_state", "fuzzy_search_by_legacy_value",
    "try_best_search_by_legacy_value", "try_best_search_by_state",
    "load_convert_record", "load_target_block",
    "init_nemc_blocks",
    # NovaBuilder 常量
    "POOL_ID_117", "POOL_ID_118",
    "AIR_RUNTIME_ID", "AIR_BLOCK_NAME",
    "DEFAULT_BLOCK_VERSION", "MAX_BLOCK_NAME_LENGTH", "MAX_BLOCK_STATES",
    # NovaBuilder 数据结构
    "NEMCBlockMapping", "RuntimeIDPool", "ToNEMCConvertor",
    # NovaBuilder 顶层函数
    "to_nemc_runtime_id", "to_nemc_runtime_id_or_air",
]
