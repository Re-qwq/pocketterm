"""block_entities - 方块实体 NBT 处理 (合并 NexusEgo + NovaBuilder)。

本模块合并了两个逆向来源的方块实体处理层:

来源 1 - NexusEgo v1.6.5:
    - StarShuttler/nbt_parser/             (NBT 解析器)
    - WaterStructure/modules/nbt_assigner/  (NBT 分配器)
    - strings_nbt.txt                       (方块实体字符串)

来源 2 - NovaBuilder (PhoenixBuilder 衍生):
    - PhoenixBuilder/fastbuilder/types/block.go
    - PhoenixBuilder/fastbuilder/bdump/command/*.go
    - Minecraft Bedrock 方块实体规范

支持的方块实体 (取并集, 29 种):

    来自 NexusEgo (19 种):
        Sign (告示牌), Banner (旗帜), Lectern (讲台), JukeBox (唱片机),
        Crafter (合成器), BrewingStand (酿造台), CommandBlock (命令方块),
        StructureBlock (结构方块), Frame (物品展示框), Beacon (信标),
        Hopper (漏斗), Dispenser (发射器), Dropper (投掷器), Furnace (熔炉),
        Barrel (木桶), ShulkerBox (潜影盒), ChiseledBookshelf (雕纹书架),
        CalibratedSculkSensor (校准潜声传感器), DecoratedPot (饰纹罐)

    来自 NovaBuilder (额外 10 种):
        EnchantTable (附魔台), EndPortal (末地传送门),
        Spawner/MobSpawner (刷怪笼), Skull (头颅方块实体),
        BlastFurnace (高炉), Smoker (烟熏炉),
        Composter (堆肥桶), Campfire (营火),
        Conduit (潮涌核心), Jigsaw (拼图方块)

.. important::

    **网易 3.8 协议限制**: replaceitem 命令已被阉割, 只能放耐久/特殊值/数量/
    NBT 标签 (keep_on_death, item_lock), 不能放附魔/自定义名字。
    因此带 NBT 的方块实体放置默认推荐 STRUCTURE 平台模式
    (11x11 海晶灯平台 + structure save/load), 详见 :mod:`.nbt_placer`。

兼容性:
    - NexusEgo API: ``SignNBT(text1="...").to_nbt()``, ``create_block_entity()``
    - NovaBuilder API: ``SignNBT(position=(...)).to_dict()``, ``create_nbt_for_block()``
    - ``BlockNBTBase`` 为 ``BlockEntity`` 的别名 (NovaBuilder 兼容)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

logger = logging.getLogger("pocketterm.protocol.nbt_handler.block_entities")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class BlockEntityError(Exception):
    """方块实体错误。"""


# -------------------------------------------------------------------- #
# 方块实体 ID 映射 (NexusEgo, 逆向自 strings_nbt.txt)
# -------------------------------------------------------------------- #

#: 方块实体 ID 映射 (逆向自 strings_nbt.txt)
BLOCK_ENTITY_IDS: dict[str, str] = {
    "Sign": "Sign",
    "Banner": "Banner",
    "Lectern": "Lectern",
    "JukeBox": "Jukebox",
    "Crafter": "Crafter",
    "BrewingStand": "BrewingStand",
    "CommandBlock": "CommandBlock",
    "StructureBlock": "StructureBlock",
    "Frame": "ItemFrame",
    "Beacon": "Beacon",
    "Hopper": "Hopper",
    "Dispenser": "Dispenser",
    "Dropper": "Dropper",
    "Furnace": "Furnace",
    "Barrel": "Barrel",
    "ShulkerBox": "ShulkerBox",
    "ChiseledBookshelf": "ChiseledBookshelf",
    "CalibratedSculkSensor": "CalibratedSculkSensor",
    "DecoratedPot": "DecoratedPot",
    # NovaBuilder 额外方块实体
    "EnchantTable": "EnchantTable",
    "EndPortal": "EndPortal",
    "MobSpawner": "MobSpawner",
    "Skull": "Skull",
    "BlastFurnace": "BlastFurnace",
    "Smoker": "Smoker",
    "Composter": "Composter",
    "Campfire": "Campfire",
    "Conduit": "Conduit",
    "JigsawBlock": "JigsawBlock",
}


# -------------------------------------------------------------------- #
# 容器物品 (NovaBuilder, 逆向自 phoenixbuilder/types/block.go ChestSlot)
# -------------------------------------------------------------------- #


@dataclass
class ContainerItem:
    """容器中的物品 (逆向自 phoenixbuilder/types/block.go ChestSlot)。

    用于 NovaBuilder 风格的容器 NBT 构建。
    """
    name: str = ""
    count: int = 0
    damage: int = 0
    slot: int = 0
    nbt: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为 NBT 字典 (Bedrock 格式)。"""
        result: dict[str, Any] = {
            "Name": self.name,
            "Count": self.count,
            "Damage": self.damage,
            "Slot": self.slot,
        }
        if self.nbt:
            result["BlockEntityTag"] = self.nbt
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContainerItem":
        """从 NBT 字典反序列化。"""
        return cls(
            name=str(data.get("Name", "")),
            count=int(data.get("Count", 0)),
            damage=int(data.get("Damage", 0)),
            slot=int(data.get("Slot", 0)),
            nbt=dict(data.get("BlockEntityTag", {})),
        )


# -------------------------------------------------------------------- #
# 基类 (NexusEgo dataclass 风格, 兼容 NovaBuilder API)
# -------------------------------------------------------------------- #


@dataclass
class BlockEntity:
    """方块实体基类。

    采用 NexusEgo 的 dataclass 风格, 同时兼容 NovaBuilder 的
    ``position`` / ``to_dict()`` / ``from_dict()`` API。

    Attributes:
        entity_id: 方块实体类型 ID。
        x: X 坐标。
        y: Y 坐标。
        z: Z 坐标。
    """

    entity_id: str = ""
    x: int = 0
    y: int = 0
    z: int = 0

    @property
    def position(self) -> tuple[int, int, int]:
        """方块坐标 (兼容 NovaBuilder API)。"""
        return (self.x, self.y, self.z)

    @position.setter
    def position(self, value: tuple[int, int, int]) -> None:
        self.x, self.y, self.z = int(value[0]), int(value[1]), int(value[2])

    def to_nbt(self) -> dict[str, Any]:
        """转换为 NBT 复合标签。"""
        nbt: dict[str, Any] = {
            "id": BLOCK_ENTITY_IDS.get(self.entity_id, self.entity_id),
            "x": self.x,
            "y": self.y,
            "z": self.z,
        }
        return nbt

    def to_dict(self) -> dict[str, Any]:
        """转换为 NBT 字典 (NovaBuilder 兼容, 等同于 :meth:`to_nbt`)。"""
        return self.to_nbt()

    @classmethod
    def from_nbt(cls, nbt: dict[str, Any]) -> "BlockEntity":
        """从 NBT 创建方块实体。"""
        entity = cls()
        entity.x = int(nbt.get("x", 0))
        entity.y = int(nbt.get("y", 0))
        entity.z = int(nbt.get("z", 0))
        return entity

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlockEntity":
        """从 NBT 字典反序列化 (NovaBuilder 兼容, 等同于 :meth:`from_nbt`)。"""
        return cls.from_nbt(data)


#: NovaBuilder 兼容别名
BlockNBTBase = BlockEntity


# -------------------------------------------------------------------- #
# Sign (告示牌) - 合并 NexusEgo + NovaBuilder 字段
# -------------------------------------------------------------------- #


@dataclass
class SignNBT(BlockEntity):
    """告示牌 NBT。

    逆向自 strings_nbt.txt: "SignNBT" 和 NovaBuilder block_nbt.go。

    Bedrock 使用 Text1-4 (NexusEgo) 或 Text 整体 (NovaBuilder)。
    合并字段: Text1-4, Color, GlowLight (NexusEgo) +
              Text, TextOwner, PersistFormatting, IgnoreLighting (NovaBuilder)。
    """
    entity_id: str = "Sign"
    text1: str = ""
    text2: str = ""
    text3: str = ""
    text4: str = ""
    color: str = "black"
    glow: bool = False
    # NovaBuilder 字段
    text: str = ""
    text_owner: str = ""
    persist_formatting: bool = True
    ignore_lighting: bool = False

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Text1": self.text1,
            "Text2": self.text2,
            "Text3": self.text3,
            "Text4": self.text4,
            "Color": self.color,
            "GlowLight": 1 if self.glow else 0,
            "Text": self.text,
            "TextOwner": self.text_owner,
            "PersistFormatting": 1 if self.persist_formatting else 0,
            "IgnoreLighting": 1 if self.ignore_lighting else 0,
        })
        return nbt


# -------------------------------------------------------------------- #
# Banner (旗帜)
# -------------------------------------------------------------------- #


@dataclass
class BannerNBT(BlockEntity):
    """旗帜 NBT。

    逆向自 strings_nbt.txt: "BannerNBT" 和 NovaBuilder block_nbt.go。
    """
    entity_id: str = "Banner"
    base: int = 0  # 基础颜色 (0-15)
    type: int = 0  # 旗帜类型
    patterns: list[dict[str, Any]] = field(default_factory=list)
    #: patterns 中每项: {"Pattern": str, "Color": int}

    def add_pattern(self, pattern: str, color: int) -> None:
        """添加图案。"""
        self.patterns.append({"Pattern": pattern, "Color": color})

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Base": self.base,
            "Type": self.type,
            "Patterns": self.patterns,
        })
        return nbt


# -------------------------------------------------------------------- #
# Lectern (讲台)
# -------------------------------------------------------------------- #


@dataclass
class LecternNBT(BlockEntity):
    """讲台 NBT。

    逆向自 strings_nbt.txt: "LecternNBT" 和 NovaBuilder block_nbt.go。
    """
    entity_id: str = "Lectern"
    book: dict[str, Any] | None = None  # 书本物品 NBT
    page: int = 0  # 当前页码
    total_pages: int = 0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Book": self.book if self.book else {},
            "Page": self.page,
            "TotalPages": self.total_pages,
        })
        return nbt


# -------------------------------------------------------------------- #
# JukeBox (唱片机)
# -------------------------------------------------------------------- #


@dataclass
class JukeBoxNBT(BlockEntity):
    """唱片机 NBT。

    逆向自 strings_nbt.txt: "JukeBoxNBT"。
    """
    entity_id: str = "JukeBox"
    record_item: dict[str, Any] | None = None  # 唱片物品 NBT

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["RecordItem"] = self.record_item if self.record_item else {}
        return nbt


# -------------------------------------------------------------------- #
# Crafter (合成器)
# -------------------------------------------------------------------- #


@dataclass
class CrafterNBT(BlockEntity):
    """合成器 NBT。

    逆向自 strings_nbt.txt: "CrafterNBT"。
    """
    entity_id: str = "Crafter"
    items: list[dict[str, Any]] = field(default_factory=list)  # 9 个槽位
    disabled_slots: list[int] = field(default_factory=list)

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Items": self.items,
            "DisabledSlots": self.disabled_slots,
        })
        return nbt


# -------------------------------------------------------------------- #
# BrewingStand (酿造台) - 合并字段
# -------------------------------------------------------------------- #


@dataclass
class BrewingStandNBT(BlockEntity):
    """酿造台 NBT。

    逆向自 strings_nbt.txt: "BrewingStandNBT" 和 NovaBuilder block_nbt.go。
    合并字段: Items, Fuel, BrewTime (NexusEgo) +
              FuelAmount, FuelTotal (NovaBuilder)。
    """
    entity_id: str = "BrewingStand"
    items: list[dict[str, Any]] = field(default_factory=list)  # 5 个槽位
    fuel: int = 0
    brew_time: int = 0
    # NovaBuilder 字段
    fuel_amount: int = 0
    fuel_total: int = 0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Items": self.items,
            "Fuel": self.fuel,
            "BrewTime": self.brew_time,
            "FuelAmount": self.fuel_amount,
            "FuelTotal": self.fuel_total,
        })
        return nbt


# -------------------------------------------------------------------- #
# CommandBlock (命令方块) - 合并字段
# -------------------------------------------------------------------- #


@dataclass
class CommandBlockNBT(BlockEntity):
    """命令方块 NBT。

    逆向自 strings_nbt.txt: "CommandBlockNBT"、
    merry-memory/protocol/encoding.CommandBlockData 和 NovaBuilder block_nbt.go。

    合并字段: NexusEgo 全部字段 + NovaBuilder 的 LastExecution, Version。
    """
    entity_id: str = "CommandBlock"
    command: str = ""
    custom_name: str = ""
    last_output: str = ""
    track_output: bool = False
    tick_delay: int = 0
    execute_on_first_tick: bool = False
    conditional: bool = False
    auto: bool = True
    needs_redstone: bool = False
    success_count: int = 0
    mode: int = 0  # 0=Impulse, 1=Repeat, 2=Chain
    powered: bool = False
    # NovaBuilder 字段
    last_execution: int = 0
    version: int = 1

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Command": self.command,
            "CustomName": self.custom_name,
            "LastOutput": self.last_output,
            "TrackOutput": 1 if self.track_output else 0,
            "TickDelay": self.tick_delay,
            "ExecuteOnFirstTick": 1 if self.execute_on_first_tick else 0,
            "Conditional": 1 if self.conditional else 0,
            "Auto": 1 if self.auto else 0,
            "NeedsRedstone": 1 if self.needs_redstone else 0,
            "SuccessCount": self.success_count,
            "Mode": self.mode,
            "Powered": 1 if self.powered else 0,
            "LastExecution": self.last_execution,
            "Version": self.version,
        })
        return nbt


# -------------------------------------------------------------------- #
# StructureBlock (结构方块)
# -------------------------------------------------------------------- #


@dataclass
class StructureBlockNBT(BlockEntity):
    """结构方块 NBT。

    逆向自 strings_nbt.txt: "StructureBlockNBT"。
    """
    entity_id: str = "StructureBlock"
    structure_name: str = ""
    data_mode: int = 0  # 0=Save, 1=Load, 2=Corner, 3=Data
    size: tuple[int, int, int] = (0, 0, 0)
    offset: tuple[int, int, int] = (0, 0, 0)
    ignore_entities: bool = False
    include_players: bool = False
    remove_blocks: bool = False
    rotation: int = 0  # 0/90/180/270
    mirror: int = 0  # 0=None, 1=X, 2=Z
    integrity: float = 1.0
    seed: int = 0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "structureName": self.structure_name,
            "dataMode": self.data_mode,
            "size": list(self.size),
            "offset": list(self.offset),
            "ignoreEntities": 1 if self.ignore_entities else 0,
            "includePlayers": 1 if self.include_players else 0,
            "removeBlocks": 1 if self.remove_blocks else 0,
            "rotation": self.rotation,
            "mirror": self.mirror,
            "integrity": self.integrity,
            "seed": self.seed,
        })
        return nbt


# -------------------------------------------------------------------- #
# Frame (物品展示框)
# -------------------------------------------------------------------- #


@dataclass
class FrameNBT(BlockEntity):
    """物品展示框 NBT。

    逆向自 strings_nbt.txt: "FrameNBT"。
    """
    entity_id: str = "Frame"
    item: dict[str, Any] | None = None
    item_rotation: int = 0
    item_drop_chance: float = 1.0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Item": self.item if self.item else {},
            "ItemRotation": self.item_rotation,
            "ItemDropChance": self.item_drop_chance,
        })
        return nbt


# -------------------------------------------------------------------- #
# Beacon (信标) - 合并字段
# -------------------------------------------------------------------- #


@dataclass
class BeaconNBT(BlockEntity):
    """信标 NBT。

    逆向自 strings_nbt.txt 和 NovaBuilder block_nbt.go。
    合并字段: Primary, Secondary (NexusEgo) + Levels (NovaBuilder)。
    """
    entity_id: str = "Beacon"
    primary: int = 0  # 主效果 ID
    secondary: int = 0  # 副效果 ID
    # NovaBuilder 字段
    levels: int = 0  # 信标等级 0-4

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Primary": self.primary,
            "Secondary": self.secondary,
            "Levels": self.levels,
        })
        return nbt


# -------------------------------------------------------------------- #
# 容器类 (Hopper/Dispenser/Dropper/Furnace/Barrel/ShulkerBox/ChiseledBookshelf)
# -------------------------------------------------------------------- #


@dataclass
class _ContainerEntity(BlockEntity):
    """容器类方块实体基类。"""
    items: list[dict[str, Any]] = field(default_factory=list)
    findable: bool = False

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Items": self.items,
            "Findable": 1 if self.findable else 0,
        })
        return nbt


@dataclass
class HopperNBT(_ContainerEntity):
    """漏斗 NBT。

    合并字段: NexusEgo 容器基类 + NovaBuilder 的 TransferCooldown。
    """
    entity_id: str = "Hopper"
    # NovaBuilder 字段
    transfer_cooldown: int = 0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["TransferCooldown"] = self.transfer_cooldown
        return nbt


@dataclass
class DispenserNBT(_ContainerEntity):
    """发射器 NBT。"""
    entity_id: str = "Dispenser"


@dataclass
class DropperNBT(_ContainerEntity):
    """投掷器 NBT。"""
    entity_id: str = "Dropper"


@dataclass
class FurnaceNBT(_ContainerEntity):
    """熔炉 NBT。

    合并字段: BurnTime, CookTime, CookTimeTotal, StoredXPInt (NexusEgo) +
              BurnDuration, StoredXP (NovaBuilder)。
    """
    entity_id: str = "Furnace"
    burn_time: int = 0
    cook_time: int = 0
    cook_time_total: int = 0
    stored_xp: int = 0
    # NovaBuilder 字段
    burn_duration: int = 0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "BurnTime": self.burn_time,
            "CookTime": self.cook_time,
            "CookTimeTotal": self.cook_time_total,
            "StoredXPInt": self.stored_xp,
            "BurnDuration": self.burn_duration,
            "StoredXP": self.stored_xp,
        })
        return nbt


@dataclass
class BarrelNBT(_ContainerEntity):
    """木桶 NBT。"""
    entity_id: str = "Barrel"


@dataclass
class ShulkerBoxNBT(_ContainerEntity):
    """潜影盒 NBT。"""
    entity_id: str = "ShulkerBox"


@dataclass
class ChiseledBookshelfNBT(_ContainerEntity):
    """雕纹书架 NBT。"""
    entity_id: str = "ChiseledBookshelf"
    last_interacted_slot: int = 0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["LastInteractedSlot"] = self.last_interacted_slot
        return nbt


# -------------------------------------------------------------------- #
# CalibratedSculkSensor / DecoratedPot
# -------------------------------------------------------------------- #


@dataclass
class CalibratedSculkSensorNBT(BlockEntity):
    """校准潜声传感器 NBT。"""
    entity_id: str = "CalibratedSculkSensor"


@dataclass
class DecoratedPotNBT(BlockEntity):
    """饰纹罐 NBT。"""
    entity_id: str = "DecoratedPot"
    sherds: list[str] = field(default_factory=lambda: ["", "", "", ""])
    item: dict[str, Any] | None = None

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "sherds": self.sherds,
            "item": self.item if self.item else {},
        })
        return nbt


# -------------------------------------------------------------------- #
# NovaBuilder 额外方块实体 (10 种)
# -------------------------------------------------------------------- #


@dataclass
class BlastFurnaceNBT(FurnaceNBT):
    """高炉 NBT (NovaBuilder)。

    继承自 FurnaceNBT, 仅 entity_id 不同。
    """
    entity_id: str = "BlastFurnace"


@dataclass
class SmokerNBT(FurnaceNBT):
    """烟熏炉 NBT (NovaBuilder)。

    继承自 FurnaceNBT, 仅 entity_id 不同。
    """
    entity_id: str = "Smoker"


@dataclass
class EnchantTableNBT(BlockEntity):
    """附魔台 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。
    """
    entity_id: str = "EnchantTable"
    book_rotation: float = 0.0
    book_position: tuple[float, float, float] = (0.0, 0.0, 0.0)

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "book_rotation": self.book_rotation,
            "book_position": list(self.book_position),
        })
        return nbt


@dataclass
class EndPortalNBT(BlockEntity):
    """末地传送门 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。无额外业务字段。
    """
    entity_id: str = "EndPortal"


@dataclass
class SpawnerNBT(BlockEntity):
    """刷怪笼 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。
    BLOCK_ENTITY_ID = "MobSpawner"。

    NBT 字段:
        EntityId: 实体 ID (如 "minecraft:zombie")
        SpawnCount: 每次生成数量
        SpawnRange: 生成范围
        Delay: 生成延迟
        MinSpawnDelay / MaxSpawnDelay: 最小/最大生成延迟
        MaxNearbyEntities: 最大附近实体数
        RequiredPlayerRange: 玩家激活范围
    """
    entity_id: str = "MobSpawner"
    entity_id_value: str = "minecraft:zombie"
    spawn_count: int = 4
    spawn_range: int = 4
    delay: int = 0
    min_spawn_delay: int = 200
    max_spawn_delay: int = 800
    max_nearby_entities: int = 6
    required_player_range: int = 16

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "EntityId": self.entity_id_value,
            "SpawnCount": self.spawn_count,
            "SpawnRange": self.spawn_range,
            "Delay": self.delay,
            "MinSpawnDelay": self.min_spawn_delay,
            "MaxSpawnDelay": self.max_spawn_delay,
            "MaxNearbyEntities": self.max_nearby_entities,
            "RequiredPlayerRange": self.required_player_range,
        })
        return nbt


# 兼容别名: NovaBuilder 使用 MobSpawnerNBT 名称
MobSpawnerNBT = SpawnerNBT


@dataclass
class SkullNBT(BlockEntity):
    """头颅方块实体 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。
    注意: 这是方块实体 (放置在地上的头颅), 与 :class:`item_nbt.HeadNBT`
    (物品 NBT) 不同。

    NBT 字段:
        SkullType: 头颅类型 (0=Skeleton, 1=Wither, 2=Zombie, 3=Player, 4=Creeper, 5=Dragon)
        Rot: 旋转
        MouthMoving: 嘴是否在动
    """
    entity_id: str = "Skull"
    skull_type: int = 0
    rotation: int = 0
    mouth_moving: bool = False

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "SkullType": self.skull_type,
            "Rot": self.rotation,
            "MouthMoving": 1 if self.mouth_moving else 0,
        })
        return nbt


@dataclass
class ComposterNBT(BlockEntity):
    """堆肥桶 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。
    """
    entity_id: str = "Composter"
    fill_level: int = 0

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["composter_fill_level"] = self.fill_level
        return nbt


@dataclass
class CampfireNBT(BlockEntity):
    """营火 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。
    """
    entity_id: str = "Campfire"
    items: list[dict[str, Any]] = field(default_factory=list)
    cooking_times: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    cooking_total_times: list[int] = field(default_factory=lambda: [0, 0, 0, 0])

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Items": self.items,
            "CookingTimes": self.cooking_times,
            "CookingTotalTimes": self.cooking_total_times,
        })
        return nbt


@dataclass
class ConduitNBT(BlockEntity):
    """潮涌核心 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。
    """
    entity_id: str = "Conduit"
    target: tuple[int, int, int] = (0, 0, 0)
    active: bool = False

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "Target": list(self.target),
            "Active": 1 if self.active else 0,
        })
        return nbt


@dataclass
class JigsawNBT(BlockEntity):
    """拼图方块 NBT (NovaBuilder)。

    逆向自 NovaBuilder block_nbt.go。
    BLOCK_ENTITY_ID = "JigsawBlock"。
    """
    entity_id: str = "JigsawBlock"
    name: str = ""
    target: str = ""
    pool: str = ""
    final_state: str = "minecraft:air"
    joint: str = "rollable"

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt.update({
            "name": self.name,
            "target": self.target,
            "pool": self.pool,
            "final_state": self.final_state,
            "joint": self.joint,
        })
        return nbt


# -------------------------------------------------------------------- #
# 工厂函数 (NexusEgo)
# -------------------------------------------------------------------- #


#: 实体 ID -> 类映射 (NexusEgo + NovaBuilder 合并)
ENTITY_CLASS_MAP: dict[str, type[BlockEntity]] = {
    # NexusEgo
    "Sign": SignNBT,
    "Banner": BannerNBT,
    "Lectern": LecternNBT,
    "JukeBox": JukeBoxNBT,
    "Crafter": CrafterNBT,
    "BrewingStand": BrewingStandNBT,
    "CommandBlock": CommandBlockNBT,
    "StructureBlock": StructureBlockNBT,
    "Frame": FrameNBT,
    "Beacon": BeaconNBT,
    "Hopper": HopperNBT,
    "Dispenser": DispenserNBT,
    "Dropper": DropperNBT,
    "Furnace": FurnaceNBT,
    "Barrel": BarrelNBT,
    "ShulkerBox": ShulkerBoxNBT,
    "ChiseledBookshelf": ChiseledBookshelfNBT,
    "CalibratedSculkSensor": CalibratedSculkSensorNBT,
    "DecoratedPot": DecoratedPotNBT,
    # NovaBuilder 额外
    "EnchantTable": EnchantTableNBT,
    "EndPortal": EndPortalNBT,
    "MobSpawner": SpawnerNBT,
    "Skull": SkullNBT,
    "BlastFurnace": BlastFurnaceNBT,
    "Smoker": SmokerNBT,
    "Composter": ComposterNBT,
    "Campfire": CampfireNBT,
    "Conduit": ConduitNBT,
    "JigsawBlock": JigsawNBT,
}


def create_block_entity(entity_id: str,
                          data: dict[str, Any] | None = None) -> BlockEntity:
    """创建方块实体实例。

    Args:
        entity_id: 方块实体 ID。
        data: 初始化数据。

    Returns:
        :class:`BlockEntity` 子类实例。

    Raises:
        BlockEntityError: 未知的方块实体 ID 或创建失败。
    """
    cls = ENTITY_CLASS_MAP.get(entity_id)
    if cls is None:
        raise BlockEntityError(f"unknown block entity: {entity_id!r}")
    data = data or {}
    try:
        # 过滤掉不在 dataclass 字段中的键
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in field_names}
        return cls(**filtered)
    except TypeError as exc:
        raise BlockEntityError(f"failed to create {entity_id}: {exc}") from exc


def get_block_entity_id(block_name: str) -> str | None:
    """从方块名获取方块实体 ID。

    Args:
        block_name: 方块名。

    Returns:
        方块实体 ID, 如果不是方块实体则返回 None。
    """
    name = block_name.lower()
    if name.endswith(":sign") or "wall_sign" in name:
        return "Sign"
    if "banner" in name:
        return "Banner"
    if name.endswith(":lectern"):
        return "Lectern"
    if name.endswith(":jukebox"):
        return "JukeBox"
    if name.endswith(":crafter"):
        return "Crafter"
    if name.endswith(":brewing_stand"):
        return "BrewingStand"
    if "command_block" in name:
        return "CommandBlock"
    if "structure_block" in name:
        return "StructureBlock"
    if name.endswith(":frame") or name.endswith(":glow_frame"):
        return "Frame"
    if name.endswith(":beacon"):
        return "Beacon"
    if name.endswith(":hopper"):
        return "Hopper"
    if name.endswith(":dispenser"):
        return "Dispenser"
    if name.endswith(":dropper"):
        return "Dropper"
    if "furnace" in name or "blast_furnace" in name or "smoker" in name:
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
    if name.endswith(":blast_furnace"):
        return "BlastFurnace"
    if name.endswith(":smoker"):
        return "Smoker"
    if name.endswith(":composter"):
        return "Composter"
    if name.endswith(":campfire") or name.endswith(":soul_campfire"):
        return "Campfire"
    if name.endswith(":conduit"):
        return "Conduit"
    if name.endswith(":jigsaw"):
        return "JigsawBlock"
    return None


# -------------------------------------------------------------------- #
# NovaBuilder 注册表兼容
# -------------------------------------------------------------------- #


#: 方块名 -> BlockEntity 子类映射 (NovaBuilder 兼容)
BLOCK_NBT_REGISTRY: dict[str, type[BlockEntity]] = {}


def _register_nbt_types() -> None:
    """注册所有方块 NBT 类型到 BLOCK_NBT_REGISTRY (NovaBuilder 兼容)。"""
    # 方块名 -> 实体 ID 映射
    block_name_to_entity: dict[str, str] = {
        # NexusEgo
        "minecraft:standing_sign": "Sign",
        "minecraft:wall_sign": "Sign",
        "minecraft:spruce_standing_sign": "Sign",
        "minecraft:birch_standing_sign": "Sign",
        "minecraft:standing_banner": "Banner",
        "minecraft:wall_banner": "Banner",
        "minecraft:lectern": "Lectern",
        "minecraft:jukebox": "JukeBox",
        "minecraft:crafter": "Crafter",
        "minecraft:brewing_stand": "BrewingStand",
        "minecraft:command_block": "CommandBlock",
        "minecraft:repeating_command_block": "CommandBlock",
        "minecraft:chain_command_block": "CommandBlock",
        "minecraft:structure_block": "StructureBlock",
        "minecraft:frame": "Frame",
        "minecraft:glow_frame": "Frame",
        "minecraft:beacon": "Beacon",
        "minecraft:hopper": "Hopper",
        "minecraft:dispenser": "Dispenser",
        "minecraft:dropper": "Dropper",
        "minecraft:furnace": "Furnace",
        "minecraft:barrel": "Barrel",
        "minecraft:chiseled_bookshelf": "ChiseledBookshelf",
        "minecraft:calibrated_sculk_sensor": "CalibratedSculkSensor",
        "minecraft:decorated_pot": "DecoratedPot",
        # NovaBuilder
        "minecraft:chest": "Chest",
        "minecraft:trapped_chest": "Chest",
        "minecraft:ender_chest": "Chest",
        "minecraft:shulker_box": "ShulkerBox",
        "minecraft:white_shulker_box": "ShulkerBox",
        "minecraft:orange_shulker_box": "ShulkerBox",
        "minecraft:magenta_shulker_box": "ShulkerBox",
        "minecraft:light_blue_shulker_box": "ShulkerBox",
        "minecraft:yellow_shulker_box": "ShulkerBox",
        "minecraft:lime_shulker_box": "ShulkerBox",
        "minecraft:pink_shulker_box": "ShulkerBox",
        "minecraft:gray_shulker_box": "ShulkerBox",
        "minecraft:light_gray_shulker_box": "ShulkerBox",
        "minecraft:cyan_shulker_box": "ShulkerBox",
        "minecraft:purple_shulker_box": "ShulkerBox",
        "minecraft:blue_shulker_box": "ShulkerBox",
        "minecraft:brown_shulker_box": "ShulkerBox",
        "minecraft:green_shulker_box": "ShulkerBox",
        "minecraft:red_shulker_box": "ShulkerBox",
        "minecraft:black_shulker_box": "ShulkerBox",
        "minecraft:enchanting_table": "EnchantTable",
        "minecraft:end_portal": "EndPortal",
        "minecraft:mob_spawner": "MobSpawner",
        "minecraft:skull": "Skull",
        "minecraft:player_head": "Skull",
        "minecraft:skeleton_skull": "Skull",
        "minecraft:wither_skeleton_skull": "Skull",
        "minecraft:zombie_head": "Skull",
        "minecraft:creeper_head": "Skull",
        "minecraft:dragon_head": "Skull",
        "minecraft:blast_furnace": "BlastFurnace",
        "minecraft:smoker": "Smoker",
        "minecraft:composter": "Composter",
        "minecraft:campfire": "Campfire",
        "minecraft:soul_campfire": "Campfire",
        "minecraft:conduit": "Conduit",
        "minecraft:jigsaw": "JigsawBlock",
    }

    for block_name, entity_id in block_name_to_entity.items():
        cls = ENTITY_CLASS_MAP.get(entity_id)
        if cls is not None:
            BLOCK_NBT_REGISTRY[block_name] = cls

    logger.debug("Registered %d block NBT types", len(BLOCK_NBT_REGISTRY))


# 自动注册
_register_nbt_types()


def get_nbt_class_for_block(block_name: str) -> Optional[type[BlockEntity]]:
    """获取方块对应的 NBT 类型 (NovaBuilder 兼容)。

    Args:
        block_name: 方块名 (如 "minecraft:chest")。

    Returns:
        NBT 类, 如果未注册则返回 None。
    """
    return BLOCK_NBT_REGISTRY.get(block_name)


def create_nbt_for_block(
    block_name: str,
    position: tuple[int, int, int] = (0, 0, 0),
) -> Optional[BlockEntity]:
    """创建方块的 NBT 实例 (NovaBuilder 兼容)。

    Args:
        block_name: 方块名。
        position: 方块坐标。

    Returns:
        BlockEntity 子类实例, 如果未注册则返回 None。
    """
    nbt_class = get_nbt_class_for_block(block_name)
    if nbt_class is None:
        logger.warning("No NBT class registered for block: %s", block_name)
        return None
    instance = nbt_class()
    instance.position = position
    return instance


__all__ = [
    # 异常
    "BlockEntityError",
    # 基类
    "BlockEntity", "BlockNBTBase",
    # 常量
    "BLOCK_ENTITY_IDS", "ENTITY_CLASS_MAP", "BLOCK_NBT_REGISTRY",
    # 容器物品
    "ContainerItem",
    # NexusEgo 方块实体 (19 种)
    "SignNBT", "BannerNBT", "LecternNBT", "JukeBoxNBT", "CrafterNBT",
    "BrewingStandNBT", "CommandBlockNBT", "StructureBlockNBT", "FrameNBT",
    "BeaconNBT", "HopperNBT", "DispenserNBT", "DropperNBT", "FurnaceNBT",
    "BarrelNBT", "ShulkerBoxNBT", "ChiseledBookshelfNBT",
    "CalibratedSculkSensorNBT", "DecoratedPotNBT",
    # NovaBuilder 额外方块实体 (10 种)
    "BlastFurnaceNBT", "SmokerNBT", "EnchantTableNBT", "EndPortalNBT",
    "SpawnerNBT", "MobSpawnerNBT", "SkullNBT", "ComposterNBT",
    "CampfireNBT", "ConduitNBT", "JigsawNBT",
    # 工厂函数
    "create_block_entity", "get_block_entity_id",
    "get_nbt_class_for_block", "create_nbt_for_block",
]
