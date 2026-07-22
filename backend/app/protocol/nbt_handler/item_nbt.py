"""item_nbt - 物品 NBT 处理。

逆向自 NexusEgo v1.6.5 的物品 NBT 处理层, 来源:

    - StarShuttler/nbt_parser/             (NBT 解析器)
    - WaterStructure/modules/nbt_assigner/  (NBT 分配器)
    - strings_nbt.txt                       (物品 NBT 字符串)

支持的物品 NBT 类型 (14 种):

    Book (书本):
        - 成书: pages (JSON 文本数组), author, title, generation
        - 书与笔: pages (可编辑)
        - 附魔书: StoredEnchantments

    Head (头颅):
        - SkullOwner: 玩家名或复合标签
        - SkullType: 头颅类型

    Banner (旗帜物品):
        - BlockEntityTag: 方块实体数据
        - Base, Patterns

    Shield (盾牌):
        - BlockEntityTag: 旗帜图案
        - Damage: 耐久

    Bundle (收纳袋):
        - Items: 物品列表

    LeatherArmor (皮革盔甲):
        - customColor: 自定义颜色 (RGB)

    SmithingArmor (锻造盔甲):
        - Trim: 纹饰 {pattern, material}

    SmithingTrim (锻造纹饰):
        - Trim: {pattern, material}

    FireworkRocket (烟花火箭):
        - Fireworks: {Explosions, Flight}

    FireworkStar (烟花之星):
        - Explosion: {Type, Colors, FadeColors, Trail, Flicker}

    Crossbow (弩):
        - ChargedProjectiles: 装填的弹丸列表
        - Charged: 是否已装填

    Compass (指南针):
        - LodestoneTracked: 是否追踪磁石
        - LodestoneDimension: 磁石维度
        - LodestonePos: 磁石位置

    RecoveryCompass (追溯指南针):
        - LodestoneTracked: false (不追踪磁石)

    GoatHorn (山羊角):
        - instrument: 乐器类型

.. important::

    **网易 3.8 协议限制**: replaceitem 命令已被阉割, 只能放耐久/特殊值/数量/
    NBT 标签 (keep_on_death, item_lock), **不能放附魔/自定义名字**。
    因此带复杂 NBT 的物品 (如附魔书、自定义名牌) 需使用 STRUCTURE 平台模式搬运。

字符串证据 (逆向自 strings_nbt.txt):
    "BookNBT"          -- 书本 NBT
    "HeadNBT"          -- 头颅 NBT
    "ShieldNBT"        -- 盾牌 NBT
    "BundleNBT"        -- 收纳袋 NBT
    "LeatherArmorNBT"  -- 皮革盔甲 NBT
    "SmithingArmorNBT" -- 锻造盔甲 NBT
    "SmithingTrimNBT"  -- 锻造纹饰 NBT
    "SingleItemEnch"   -- 单物品附魔
    "DefaultItem"      -- 默认物品
    "ItemWithSlot"     -- 带槽位物品
    "ItemBasicData"    -- 物品基础数据
    "ItemBlockData"    -- 物品方块数据
    "ItemEnhanceData"  -- 物品增强数据
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pocketterm.protocol.nbt_handler.item_nbt")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class ItemNBTError(Exception):
    """物品 NBT 错误。"""


# -------------------------------------------------------------------- #
# 物品 NBT 类型
# -------------------------------------------------------------------- #

#: 物品 NBT 类型 (逆向自 strings_nbt.txt)
ITEM_NBT_TYPES: dict[str, str] = {
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
# 基类
# -------------------------------------------------------------------- #


@dataclass
class ItemNBT:
    """物品 NBT 基类。

    Attributes:
        item_type: 物品类型。
        id: 物品 ID。
        count: 物品数量。
        damage: 物品耐久 (可选)。
        slot: 槽位编号 (可选)。
    """
    item_type: str = ""
    id: str = ""
    count: int = 1
    damage: int | None = None
    slot: int | None = None

    def to_nbt(self) -> dict[str, Any]:
        """转换为 NBT 复合标签。"""
        nbt: dict[str, Any] = {
            "id": self.id,
            "Count": self.count,
        }
        if self.damage is not None:
            nbt["Damage"] = self.damage
        if self.slot is not None:
            nbt["Slot"] = self.slot
        return nbt

    def to_item_tag(self) -> dict[str, Any]:
        """转换为物品 tag (不含 id/Count/Damage/Slot)。"""
        return {}


# -------------------------------------------------------------------- #
# Book (书本)
# -------------------------------------------------------------------- #


@dataclass
class BookNBT(ItemNBT):
    """书本 NBT。

    逆向自 strings_nbt.txt: "BookNBT"。
    支持成书、书与笔、附魔书。
    """
    item_type: str = "Book"
    id: str = "minecraft:written_book"
    pages: list[str] = field(default_factory=list)  # JSON 文本数组
    author: str = ""
    title: str = ""
    generation: int = 0  # 0=Original, 1=Copy of Original, 2=Copy of Copy
    resolved: bool = False
    #: 附魔书字段
    stored_enchantments: list[dict[str, Any]] = field(default_factory=list)

    def add_page(self, text: str) -> None:
        """添加一页。"""
        self.pages.append(text)

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {}
        if self.pages:
            tag["pages"] = self.pages
        if self.author:
            tag["author"] = self.author
        if self.title:
            tag["title"] = self.title
        if self.generation:
            tag["generation"] = self.generation
        if self.resolved:
            tag["resolved"] = 1
        if self.stored_enchantments:
            tag["StoredEnchantments"] = self.stored_enchantments
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


# -------------------------------------------------------------------- #
# Head (头颅)
# -------------------------------------------------------------------- #


@dataclass
class HeadNBT(ItemNBT):
    """头颅 NBT。

    逆向自 strings_nbt.txt: "HeadNBT"。
    注意: 这是物品 NBT (背包中的头颅), 与 :class:`block_entities.SkullNBT`
    (方块实体) 不同。
    """
    item_type: str = "Head"
    id: str = "minecraft:player_head"
    skull_owner: str | dict[str, Any] = ""
    skull_type: int = 3  # 0=Skeleton, 1=Wither, 2=Zombie, 3=Player, 4=Creeper, 5=Dragon

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {}
        if self.skull_owner:
            tag["SkullOwner"] = self.skull_owner
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


# -------------------------------------------------------------------- #
# Banner (旗帜物品)
# -------------------------------------------------------------------- #


@dataclass
class BannerItemNBT(ItemNBT):
    """旗帜物品 NBT。"""
    item_type: str = "Banner"
    id: str = "minecraft:banner"
    base: int = 0  # 基础颜色 (0-15)
    patterns: list[dict[str, Any]] = field(default_factory=list)

    def add_pattern(self, pattern: str, color: int) -> None:
        """添加图案。"""
        self.patterns.append({"Pattern": pattern, "Color": color})

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {}
        block_entity_tag: dict[str, Any] = {"Base": self.base}
        if self.patterns:
            block_entity_tag["Patterns"] = self.patterns
        tag["BlockEntityTag"] = block_entity_tag
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


# -------------------------------------------------------------------- #
# Shield (盾牌)
# -------------------------------------------------------------------- #


@dataclass
class ShieldNBT(ItemNBT):
    """盾牌 NBT。"""
    item_type: str = "Shield"
    id: str = "minecraft:shield"
    base: int = 0
    patterns: list[dict[str, Any]] = field(default_factory=list)

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {}
        block_entity_tag: dict[str, Any] = {"Base": self.base}
        if self.patterns:
            block_entity_tag["Patterns"] = self.patterns
        tag["BlockEntityTag"] = block_entity_tag
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


# -------------------------------------------------------------------- #
# Bundle (收纳袋)
# -------------------------------------------------------------------- #


@dataclass
class BundleNBT(ItemNBT):
    """收纳袋 NBT。"""
    item_type: str = "Bundle"
    id: str = "minecraft:bundle"
    items: list[dict[str, Any]] = field(default_factory=list)

    def add_item(self, item_id: str, count: int = 1,
                   tag: dict[str, Any] | None = None) -> None:
        """添加物品。"""
        item: dict[str, Any] = {"id": item_id, "Count": count}
        if tag:
            item["tag"] = tag
        self.items.append(item)

    def to_item_tag(self) -> dict[str, Any]:
        return {"Items": self.items} if self.items else {}

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


# -------------------------------------------------------------------- #
# LeatherArmor (皮革盔甲)
# -------------------------------------------------------------------- #


@dataclass
class LeatherArmorNBT(ItemNBT):
    """皮革盔甲 NBT。

    逆向自 strings_nbt.txt: "LeatherArmorNBT"。
    """
    item_type: str = "LeatherArmor"
    id: str = "minecraft:leather_helmet"
    custom_color: int | None = None  # RGB 颜色值
    armor_piece: str = "helmet"  # helmet/chestplate/leggings/boots

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {}
        if self.custom_color is not None:
            tag["customColor"] = self.custom_color
        return tag

    def to_nbt(self) -> dict[str, Any]:
        # 根据 armor_piece 设置正确的 id
        armor_ids = {
            "helmet": "minecraft:leather_helmet",
            "chestplate": "minecraft:leather_chestplate",
            "leggings": "minecraft:leather_leggings",
            "boots": "minecraft:leather_boots",
        }
        self.id = armor_ids.get(self.armor_piece, self.id)
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


# -------------------------------------------------------------------- #
# SmithingArmor / SmithingTrim (锻造盔甲/纹饰)
# -------------------------------------------------------------------- #


@dataclass
class SmithingArmorNBT(ItemNBT):
    """锻造盔甲 NBT。

    逆向自 strings_nbt.txt: "SmithingArmorNBT"。
    """
    item_type: str = "SmithingArmor"
    id: str = "minecraft:netherite_chestplate"
    trim_pattern: str = ""
    trim_material: str = ""

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {}
        if self.trim_pattern or self.trim_material:
            tag["Trim"] = {
                "pattern": self.trim_pattern,
                "material": self.trim_material,
            }
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


@dataclass
class SmithingTrimNBT(ItemNBT):
    """锻造纹饰 NBT。

    逆向自 strings_nbt.txt: "SmithingTrimNBT"。
    """
    item_type: str = "SmithingTrim"
    id: str = "minecraft:smithing_template"
    trim_pattern: str = ""
    trim_material: str = ""

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {}
        if self.trim_pattern or self.trim_material:
            tag["Trim"] = {
                "pattern": self.trim_pattern,
                "material": self.trim_material,
            }
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        tag = self.to_item_tag()
        if tag:
            nbt["tag"] = tag
        return nbt


# -------------------------------------------------------------------- #
# FireworkRocket / FireworkStar (烟花)
# -------------------------------------------------------------------- #


@dataclass
class FireworkRocketNBT(ItemNBT):
    """烟花火箭 NBT。"""
    item_type: str = "FireworkRocket"
    id: str = "minecraft:firework_rocket"
    flight: int = 1  # 飞行时长 (1-3)
    explosions: list[dict[str, Any]] = field(default_factory=list)

    def add_explosion(self, exp_type: int, colors: list[int],
                        fade_colors: list[int] | None = None,
                        trail: bool = False, flicker: bool = False) -> None:
        """添加爆炸效果。"""
        explosion: dict[str, Any] = {
            "Type": exp_type,
            "Colors": colors,
            "Trail": 1 if trail else 0,
            "Flicker": 1 if flicker else 0,
        }
        if fade_colors:
            explosion["FadeColors"] = fade_colors
        self.explosions.append(explosion)

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {
            "Fireworks": {
                "Flight": self.flight,
                "Explosions": self.explosions,
            },
        }
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["tag"] = self.to_item_tag()
        return nbt


@dataclass
class FireworkStarNBT(ItemNBT):
    """烟花之星 NBT。"""
    item_type: str = "FireworkStar"
    id: str = "minecraft:firework_star"
    exp_type: int = 0
    colors: list[int] = field(default_factory=list)
    fade_colors: list[int] = field(default_factory=list)
    trail: bool = False
    flicker: bool = False

    def to_item_tag(self) -> dict[str, Any]:
        explosion: dict[str, Any] = {
            "Type": self.exp_type,
            "Colors": self.colors,
            "Trail": 1 if self.trail else 0,
            "Flicker": 1 if self.flicker else 0,
        }
        if self.fade_colors:
            explosion["FadeColors"] = self.fade_colors
        return {"Explosion": explosion}

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["tag"] = self.to_item_tag()
        return nbt


# -------------------------------------------------------------------- #
# Crossbow (弩)
# -------------------------------------------------------------------- #


@dataclass
class CrossbowNBT(ItemNBT):
    """弩 NBT。"""
    item_type: str = "Crossbow"
    id: str = "minecraft:crossbow"
    charged: bool = False
    charged_projectiles: list[dict[str, Any]] = field(default_factory=list)

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {
            "Charged": 1 if self.charged else 0,
        }
        if self.charged_projectiles:
            tag["ChargedProjectiles"] = self.charged_projectiles
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["tag"] = self.to_item_tag()
        return nbt


# -------------------------------------------------------------------- #
# Compass / RecoveryCompass (指南针)
# -------------------------------------------------------------------- #


@dataclass
class CompassNBT(ItemNBT):
    """指南针 NBT。"""
    item_type: str = "Compass"
    id: str = "minecraft:compass"
    lodestone_tracked: bool = False
    lodestone_dimension: str = ""
    lodestone_pos: tuple[int, int, int] | None = None

    def to_item_tag(self) -> dict[str, Any]:
        tag: dict[str, Any] = {
            "LodestoneTracked": 1 if self.lodestone_tracked else 0,
        }
        if self.lodestone_dimension:
            tag["LodestoneDimension"] = self.lodestone_dimension
        if self.lodestone_pos:
            tag["LodestonePos"] = list(self.lodestone_pos)
        return tag

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["tag"] = self.to_item_tag()
        return nbt


@dataclass
class RecoveryCompassNBT(ItemNBT):
    """追溯指南针 NBT。

    追溯指南针不追踪磁石, 始终指向玩家上次死亡点。
    """
    item_type: str = "RecoveryCompass"
    id: str = "minecraft:recovery_compass"

    def to_item_tag(self) -> dict[str, Any]:
        return {"LodestoneTracked": 0}


# -------------------------------------------------------------------- #
# GoatHorn (山羊角)
# -------------------------------------------------------------------- #


@dataclass
class GoatHornNBT(ItemNBT):
    """山羊角 NBT。"""
    item_type: str = "GoatHorn"
    id: str = "minecraft:goat_horn"
    instrument: str = "ponder_goat_horn"

    def to_item_tag(self) -> dict[str, Any]:
        return {"instrument": self.instrument}

    def to_nbt(self) -> dict[str, Any]:
        nbt = super().to_nbt()
        nbt["tag"] = self.to_item_tag()
        return nbt


# -------------------------------------------------------------------- #
# 工厂函数
# -------------------------------------------------------------------- #


#: 物品类型 -> 类映射
ITEM_CLASS_MAP: dict[str, type[ItemNBT]] = {
    "Book": BookNBT,
    "Head": HeadNBT,
    "Banner": BannerItemNBT,
    "Shield": ShieldNBT,
    "Bundle": BundleNBT,
    "LeatherArmor": LeatherArmorNBT,
    "SmithingArmor": SmithingArmorNBT,
    "SmithingTrim": SmithingTrimNBT,
    "FireworkRocket": FireworkRocketNBT,
    "FireworkStar": FireworkStarNBT,
    "Crossbow": CrossbowNBT,
    "Compass": CompassNBT,
    "RecoveryCompass": RecoveryCompassNBT,
    "GoatHorn": GoatHornNBT,
    "DefaultItem": ItemNBT,
    "ItemWithSlot": ItemNBT,
    "ItemBasicData": ItemNBT,
    "ItemBlockData": ItemNBT,
    "ItemEnhanceData": ItemNBT,
    "SingleItemEnch": ItemNBT,
}


def create_item_nbt(item_type: str,
                      data: dict[str, Any] | None = None) -> ItemNBT:
    """创建物品 NBT 实例。

    Args:
        item_type: 物品类型。
        data: 初始化数据。

    Returns:
        :class:`ItemNBT` 子类实例。

    Raises:
        ItemNBTError: 创建失败。
    """
    cls = ITEM_CLASS_MAP.get(item_type, ItemNBT)
    data = data or {}
    try:
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(cls)}
        filtered = {k: v for k, v in data.items() if k in field_names}
        return cls(**filtered)
    except TypeError as exc:
        raise ItemNBTError(f"failed to create {item_type}: {exc}") from exc


def get_item_nbt_handler(item_type: str) -> type[ItemNBT] | None:
    """获取物品 NBT 处理器类。

    Args:
        item_type: 物品类型。

    Returns:
        处理器类, 如果不存在则返回 None。
    """
    return ITEM_CLASS_MAP.get(item_type)


__all__ = [
    "ItemNBTError", "ItemNBT", "ITEM_NBT_TYPES",
    "BookNBT", "HeadNBT", "BannerItemNBT", "ShieldNBT", "BundleNBT",
    "LeatherArmorNBT", "SmithingArmorNBT", "SmithingTrimNBT",
    "FireworkRocketNBT", "FireworkStarNBT", "CrossbowNBT",
    "CompassNBT", "RecoveryCompassNBT", "GoatHornNBT",
    "create_item_nbt", "get_item_nbt_handler",
]
