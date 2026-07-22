"""nbt_assigner - NBT 分配器 (15+ 方块实体, 20+ 物品类型)。

逆向自 NexusEgo v1.6.5 的 NBT 分配器, 来源:

    - WaterStructure/modules/nbt_assigner/        (NBT 分配器)
    - strings_nbt.txt                              (NBT 字符串)

NBTAssigner 是 NexusEgo 的核心 NBT 处理组件, 用于:
    1. 根据方块/物品类型选择合适的 NBT 处理器
    2. 将结构化数据转换为 NBT 复合标签
    3. 处理方块实体和物品 NBT 的互转

支持的方块实体 (29 种, 详见 :mod:`.block_entities`):
    Sign (告示牌), Banner (旗帜), Lectern (讲台), JukeBox (唱片机),
    Crafter (合成器), BrewingStand (酿造台), CommandBlock (命令方块),
    StructureBlock (结构方块), Frame (物品展示框), Beacon (信标),
    Hopper (漏斗), Dispenser (发射器), Dropper (投掷器), Furnace (熔炉),
    Barrel (木桶), ShulkerBox (潜影盒), ChiseledBookshelf (雕纹书架),
    CalibratedSculkSensor (校准潜声传感器), DecoratedPot (饰纹罐),
    EnchantTable (附魔台), EndPortal (末地传送门), Spawner (刷怪笼),
    Skull (头颅), BlastFurnace (高炉), Smoker (烟熏炉),
    Composter (堆肥桶), Campfire (营火), Conduit (潮涌核心), Jigsaw (拼图方块)

支持的物品 NBT 类型 (20+):
    Book (书本), Head (头颅), Banner (旗帜), Shield (盾牌),
    Bundle (收纳袋), LeatherArmor (皮革盔甲),
    SmithingArmor (锻造盔甲), SmithingTrim (锻造纹饰),
    FireworkRocket (烟花火箭), FireworkStar (烟花之星),
    Crossbow (弩), Compass (指南针), RecoveryCompass (追溯指南针),
    GoatHorn (山羊角)

.. important::

    **网易 3.8 协议限制**: replaceitem 命令已被阉割, 只能放耐久/特殊值/数量/
    NBT 标签 (keep_on_death, item_lock), 不能放附魔/自定义名字。
    NBTAssigner 生成的 NBT 数据需通过 STRUCTURE 平台模式搬运到目标位置。

字符串证据 (逆向自 strings_nbt.txt):
    "DefaultBlock"             -- 默认方块 (无 NBT)
    "BlockAdditionalData"      -- 方块附加数据
    "Container"                -- 容器
    "DefaultItem"              -- 默认物品
    "ItemWithSlot"             -- 带槽位物品
    "ItemBasicData"            -- 物品基础数据
    "ItemBlockData"            -- 物品方块数据
    "ItemEnhanceData"          -- 物品增强数据
    "SingleItemEnch"           -- 单物品附魔
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("pocketterm.protocol.nbt_handler.nbt_assigner")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class NBTAssignerError(Exception):
    """NBT 分配器错误。"""


# -------------------------------------------------------------------- #
# 类型定义
# -------------------------------------------------------------------- #

#: NBT 处理器函数类型
#: 接收结构化数据字典, 返回 NBT 复合标签 (Python dict 表示)
BlockEntityHandler = Callable[[dict[str, Any]], dict[str, Any]]
ItemNBTHandler = Callable[[dict[str, Any]], dict[str, Any]]


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class NBTAssignmentResult:
    """NBT 分配结果。

    Attributes:
        block_entity_id: 方块实体 ID。
        nbt_data: 生成的 NBT 数据。
        handler_name: 处理器名称。
        warnings: 警告信息列表。
        success: 是否成功。
    """
    block_entity_id: str = ""
    nbt_data: dict[str, Any] = field(default_factory=dict)
    handler_name: str = ""
    warnings: list[str] = field(default_factory=list)
    success: bool = False


# -------------------------------------------------------------------- #
# 方块实体处理器注册表
# -------------------------------------------------------------------- #

#: 方块实体 ID (逆向自 strings_nbt.txt)
BLOCK_ENTITY_HANDLERS: dict[str, str] = {
    "Sign": "SignNBT",
    "Banner": "BannerNBT",
    "Lectern": "LecternNBT",
    "Jukebox": "JukeBoxNBT",
    "Crafter": "CrafterNBT",
    "BrewingStand": "BrewingStandNBT",
    "CommandBlock": "CommandBlockNBT",
    "StructureBlock": "StructureBlockNBT",
    "ItemFrame": "FrameNBT",
    "Beacon": "BeaconNBT",
    "Hopper": "HopperNBT",
    "Dispenser": "DispenserNBT",
    "Dropper": "DropperNBT",
    "Furnace": "FurnaceNBT",
    "Barrel": "BarrelNBT",
    "ShulkerBox": "ShulkerBoxNBT",
    "ChiseledBookshelf": "ChiseledBookshelfNBT",
    "CalibratedSculkSensor": "CalibratedSculkSensorNBT",
    "DecoratedPot": "DecoratedPotNBT",
    # NovaBuilder 额外方块实体
    "EnchantTable": "EnchantTableNBT",
    "EndPortal": "EndPortalNBT",
    "MobSpawner": "SpawnerNBT",
    "Skull": "SkullNBT",
    "BlastFurnace": "BlastFurnaceNBT",
    "Smoker": "SmokerNBT",
    "Composter": "ComposterNBT",
    "Campfire": "CampfireNBT",
    "Conduit": "ConduitNBT",
    "JigsawBlock": "JigsawNBT",
}


#: 物品 NBT 处理器注册表 (逆向自 strings_nbt.txt)
ITEM_NBT_HANDLERS: dict[str, str] = {
    "Book": "BookNBT",
    "Head": "HeadNBT",
    "Banner": "BannerItemNBT",
    "Shield": "ShieldNBT",
    "Bundle": "BundleNBT",
    "LeatherArmor": "LeatherArmorNBT",
    "SmithingArmor": "SmithingArmorNBT",
    "SmithingTrim": "SmithingTrimNBT",
    "FireworkRocket": "FireworkRocketNBT",
    "FireworkStar": "FireworkStarNBT",
    "Crossbow": "CrossbowNBT",
    "Compass": "CompassNBT",
    "RecoveryCompass": "RecoveryCompassNBT",
    "GoatHorn": "GoatHornNBT",
    "DefaultItem": "DefaultItem",
    "ItemWithSlot": "ItemWithSlot",
    "ItemBasicData": "ItemBasicData",
    "ItemBlockData": "ItemBlockData",
    "ItemEnhanceData": "ItemEnhanceData",
    "SingleItemEnch": "SingleItemEnch",
}


# -------------------------------------------------------------------- #
# NBT 分配器
# -------------------------------------------------------------------- #


class NBTAssigner:
    """NBT 分配器。

    逆向自 WaterStructure/modules/nbt_assigner/nbt_assigner.go。
    根据方块/物品类型选择合适的 NBT 处理器。

    工作流程:
        1. 接收方块/物品的结构化数据
        2. 根据类型查找对应的处理器
        3. 调用处理器生成 NBT 复合标签
        4. 返回分配结果

    使用方式::

        assigner = NBTAssigner()
        result = assigner.assign_block_nbt(
            "minecraft:chest",
            {"x": 100, "y": 64, "z": 100, "items": [...]},
        )
        if result.success:
            nbt_data = result.nbt_data
    """

    def __init__(self) -> None:
        self._block_handlers: dict[str, BlockEntityHandler] = {}
        self._item_handlers: dict[str, ItemNBTHandler] = {}
        self._register_default_handlers()

    def _register_default_handlers(self) -> None:
        """注册默认处理器。"""
        from .block_entities import create_block_entity, BLOCK_ENTITY_IDS
        from .item_nbt import create_item_nbt, ITEM_NBT_TYPES

        for entity_id, handler_name in BLOCK_ENTITY_HANDLERS.items():
            def _make_handler(eid: str) -> BlockEntityHandler:
                def handler(data: dict[str, Any]) -> dict[str, Any]:
                    be = create_block_entity(eid, data)
                    return be.to_nbt()
                return handler
            self._block_handlers[entity_id] = _make_handler(entity_id)

        for item_type, handler_name in ITEM_NBT_HANDLERS.items():
            def _make_item_handler(it: str) -> ItemNBTHandler:
                def handler(data: dict[str, Any]) -> dict[str, Any]:
                    item = create_item_nbt(it, data)
                    return item.to_nbt()
                return handler
            self._item_handlers[item_type] = _make_item_handler(item_type)

    def assign_block_nbt(self, block_name: str,
                          block_data: dict[str, Any]) -> NBTAssignmentResult:
        """分配方块 NBT。

        Args:
            block_name: 方块名 (如 "minecraft:chest")。
            block_data: 方块数据字典。

        Returns:
            :class:`NBTAssignmentResult`。
        """
        result = NBTAssignmentResult()
        # 从方块名推断方块实体类型
        entity_id = self._infer_block_entity_id(block_name)
        if not entity_id:
            result.warnings.append(
                f"no block entity handler for {block_name!r}"
            )
            result.success = True  # 普通方块, 无 NBT
            return result

        handler = self._block_handlers.get(entity_id)
        if handler is None:
            raise NBTAssignerError(
                f"no handler registered for block entity {entity_id!r}"
            )

        try:
            nbt = handler(block_data)
            result.block_entity_id = entity_id
            result.nbt_data = nbt
            result.handler_name = BLOCK_ENTITY_HANDLERS[entity_id]
            result.success = True
        except Exception as exc:
            logger.exception("block NBT assignment failed: %s", exc)
            result.warnings.append(str(exc))

        return result

    def assign_item_nbt(self, item_type: str,
                          item_data: dict[str, Any]) -> NBTAssignmentResult:
        """分配物品 NBT。

        Args:
            item_type: 物品类型 (如 "Book", "Head")。
            item_data: 物品数据字典。

        Returns:
            :class:`NBTAssignmentResult`。
        """
        result = NBTAssignmentResult()
        handler = self._item_handlers.get(item_type)
        if handler is None:
            raise NBTAssignerError(
                f"no handler registered for item type {item_type!r}"
            )

        try:
            nbt = handler(item_data)
            result.nbt_data = nbt
            result.handler_name = ITEM_NBT_HANDLERS[item_type]
            result.success = True
        except Exception as exc:
            logger.exception("item NBT assignment failed: %s", exc)
            result.warnings.append(str(exc))

        return result

    def _infer_block_entity_id(self, block_name: str) -> str | None:
        """从方块名推断方块实体类型。

        逆向自 nbt_assigner.go 的 inferBlockEntityID 函数。
        NexusEgo 通过方块名映射到对应的方块实体处理器。

        Args:
            block_name: 方块名。

        Returns:
            方块实体 ID, 如果不匹配则返回 None。
        """
        name = block_name.lower()
        if name.endswith(":sign") or "sign" in name and "wall_sign" not in name:
            return "Sign"
        if name.endswith(":standing_banner") or name.endswith(":wall_banner"):
            return "Banner"
        if name.endswith(":lectern"):
            return "Lectern"
        if name.endswith(":jukebox"):
            return "Jukebox"
        if name.endswith(":crafter"):
            return "Crafter"
        if name.endswith(":brewing_stand"):
            return "BrewingStand"
        if "command_block" in name:
            return "CommandBlock"
        if "structure_block" in name:
            return "StructureBlock"
        if name.endswith(":frame") or name.endswith(":glow_frame"):
            return "ItemFrame"
        if name.endswith(":beacon"):
            return "Beacon"
        if name.endswith(":hopper"):
            return "Hopper"
        if name.endswith(":dispenser"):
            return "Dispenser"
        if name.endswith(":dropper"):
            return "Dropper"
        if name.endswith(":furnace") or name.endswith(":blast_furnace") or name.endswith(":smoker"):
            return "Furnace"
        if name.endswith(":barrel"):
            return "Barrel"
        if "shulker_box" in name:
            return "ShulkerBox"
        if name.endswith(":chiseled_bookshelf"):
            return "ChiseledBookshelf"
        if name.endswith(":calibrated_sculk_sensor"):
            return "CalibratedSculkSensor"
        if name.endswith(":decorated_pot"):
            return "DecoratedPot"
        # NovaBuilder 额外方块实体
        if name.endswith(":enchanting_table") or name.endswith(":enchant_table"):
            return "EnchantTable"
        if name.endswith(":end_portal"):
            return "EndPortal"
        if name.endswith(":mob_spawner"):
            return "MobSpawner"
        if name.endswith(":skull") or name.endswith(":head") or "player_head" in name:
            return "Skull"
        if name.endswith(":composter"):
            return "Composter"
        if name.endswith(":campfire") or name.endswith(":soul_campfire"):
            return "Campfire"
        if name.endswith(":conduit"):
            return "Conduit"
        if name.endswith(":jigsaw"):
            return "JigsawBlock"
        # 容器类
        if name.endswith(":chest") or name.endswith(":trapped_chest") or name.endswith(":ender_chest"):
            return "Container"
        return None


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def assign_block_nbt(block_name: str,
                       block_data: dict[str, Any]) -> NBTAssignmentResult:
    """分配方块 NBT (便捷函数)。

    Args:
        block_name: 方块名。
        block_data: 方块数据字典。

    Returns:
        :class:`NBTAssignmentResult`。
    """
    assigner = NBTAssigner()
    return assigner.assign_block_nbt(block_name, block_data)


def assign_item_nbt(item_type: str,
                      item_data: dict[str, Any]) -> NBTAssignmentResult:
    """分配物品 NBT (便捷函数)。

    Args:
        item_type: 物品类型。
        item_data: 物品数据字典。

    Returns:
        :class:`NBTAssignmentResult`。
    """
    assigner = NBTAssigner()
    return assigner.assign_item_nbt(item_type, item_data)


def get_supported_block_entities() -> list[str]:
    """获取支持的方块实体列表。"""
    return list(BLOCK_ENTITY_HANDLERS.keys())


def get_supported_item_types() -> list[str]:
    """获取支持的物品 NBT 类型列表。"""
    return list(ITEM_NBT_HANDLERS.keys())


__all__ = [
    "NBTAssignerError", "NBTAssignmentResult",
    "BLOCK_ENTITY_HANDLERS", "ITEM_NBT_HANDLERS",
    "NBTAssigner",
    "assign_block_nbt", "assign_item_nbt",
    "get_supported_block_entities", "get_supported_item_types",
]
