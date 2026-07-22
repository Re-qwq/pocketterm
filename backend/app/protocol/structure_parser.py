"""Minecraft 建筑文件解析器 — 支持 mcstructure / nbt / schematic / schem / bdx / mcworld / litematic 格式。

本模块解析 Minecraft 建筑结构文件, 将其统一转换为 :class:`ParsedStructure`
对象, 包含尺寸、方块列表、3D 方块索引数组、实体和方块实体等信息。

支持的格式:
    ===================  ============================================  ===========  ========
    格式                  说明                                          NBT 字节序    压缩
    ===================  ============================================  ===========  ========
    .mcstructure         Minecraft Bedrock 原生结构文件                小端 (磁盘)   无
    .nbt                 Minecraft 结构文件 (Java/Bedrock)             大端/小端     可选 gzip
    .schematic           WorldEdit Schematic v1                       大端          gzip
    .schem               WorldEdit Schematic v2                       大端          gzip
    .bdx                 FastBuilder/PhoenixBuilder 建筑文件           网络 (小端)   无 (二进制)
    .mcworld             Bedrock 世界存档 (ZIP)                        小端 (磁盘)   ZIP
    .litematic           Litematica mod 建筑文件                      大端          gzip
    ===================  ============================================  ===========  ========

关键设计点:
    - mcstructure 使用 **小端序磁盘 NBT** (不是网络 NBT), 不压缩
    - schematic / schem 使用 **大端序 NBT** (Java 版格式) + gzip 压缩
    - schem 的 BlockData 使用 **Varint 编码的索引** (不是固定字节)
    - schematic 需要将 Java 版方块 ID (1.12 及更早) 映射到 Bedrock 方块名
    - schem 的 Palette 直接使用命名空间方块名, 可能内嵌方块状态
    - 自动检测 gzip 压缩 (魔术字节 0x1f 0x8b)
    - 方块数据统一存储为 3D 数组 ``block_data[x][y][z]`` (索引指向 blocks 列表)

基本用法::

    from app.protocol.structure_parser import StructureParser

    parser = StructureParser()
    structure = await parser.parse_file("house.mcstructure")

    print(f"尺寸: {structure.size}")
    print(f"非空方块数: {structure.get_block_count()}")

    # 遍历所有方块
    for x, y, z, block in structure.iter_blocks():
        print(f"({x}, {y}, {z}): {block.name}")

    # 获取指定位置方块
    block = structure.get_block_at(0, 0, 0)
    if block is not None:
        print(f"原点方块: {block.name}")

逆向来源:
    - Minecraft Bedrock Edition 结构文件格式 (wiki.vg / bedrock.dev)
    - WorldEdit schematic 格式 (EngineHub/WorldEdit)
    - FastAsyncWorldEdit schem v2 格式 (IntellectualSites/FAWE)
    - Minecraft Java 版结构方块 .nbt 格式 (1.13+)
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .nbt import (
    NBTError,
    NETWORK_LITTLE_ENDIAN,
    NBTReader,
    parse_snbt,
    unmarshal,
    unmarshal_big_endian,
    unmarshal_disk,
)
from .blocks import BlockState, SchematicBlockMapping, get_block_mapping
from .varint import decode_varint32, decode_varuint32

logger = logging.getLogger("pocketterm.structure_parser")


# ======================================================================
# 常量
# ======================================================================

#: Gzip 文件魔术字节 (0x1f 0x8b), 用于检测 gzip 压缩
_GZIP_MAGIC: bytes = b"\x1f\x8b"

#: BDX 文件魔术字节 "BDX\x00" (FastBuilder/PhoenixBuilder 建筑文件)
_BDX_MAGIC: bytes = b"BDX\x00"

#: ZIP 文件魔术字节 "PK\x03\x04" (mcworld 是 ZIP 压缩包)
_ZIP_MAGIC: bytes = b"PK\x03\x04"

#: 支持的格式名称 — Bedrock 原生结构文件
FORMAT_MCSTRUCTURE: str = "mcstructure"

#: 支持的格式名称 — Minecraft 结构文件 (Java/Bedrock, 可能 gzip 压缩)
FORMAT_NBT: str = "nbt"

#: 支持的格式名称 — WorldEdit Schematic v1
FORMAT_SCHEMATIC: str = "schematic"

#: 支持的格式名称 — WorldEdit Schematic v2
FORMAT_SCHEM: str = "schem"

#: 支持的格式名称 — FastBuilder/PhoenixBuilder 建筑文件 (二进制格式)
FORMAT_BDX: str = "bdx"

#: 支持的格式名称 — Bedrock 世界存档 (ZIP 压缩包)
FORMAT_MCWORLD: str = "mcworld"

#: 支持的格式名称 — Litematica mod 建筑文件 (Java 大端 NBT + gzip)
FORMAT_LITEMATIC: str = "litematic"

#: 所有支持的格式集合
SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {
        FORMAT_MCSTRUCTURE,
        FORMAT_NBT,
        FORMAT_SCHEMATIC,
        FORMAT_SCHEM,
        FORMAT_BDX,
        FORMAT_MCWORLD,
        FORMAT_LITEMATIC,
    }
)

#: 文件扩展名到格式名称的映射
_EXTENSION_MAP: dict[str, str] = {
    ".mcstructure": FORMAT_MCSTRUCTURE,
    ".nbt": FORMAT_NBT,
    ".schematic": FORMAT_SCHEMATIC,
    ".schem": FORMAT_SCHEM,
    ".schem2": FORMAT_SCHEM,  # FAWE 别名
    ".bdx": FORMAT_BDX,
    ".mcworld": FORMAT_MCWORLD,
    ".litematic": FORMAT_LITEMATIC,
}

#: 空气方块的方块名 (Bedrock 命名空间)
AIR_BLOCK_NAME: str = "minecraft:air"

#: mcstructure 中 block_indices 的正层索引 (通常使用的层)
_BLOCK_LAYER_POSITIVE: int = 0

#: mcstructure 中表示空气的方块索引值 (-1)
_AIR_INDEX: int = -1

#: BDX 操作码 — 放置方块 (含坐标 xyz 和方块名 + states)
_BDX_OP_PLACE_BLOCK: int = 0

#: BDX 操作码 — 放置方块提供者 (通过命名提供者引用方块)
_BDX_OP_PLACE_BLOCK_PROVIDER: int = 1

#: Bedrock 颜色名称列表 (索引对应 Java 数据值 0~15)
_BEDROCK_COLORS: tuple[str, ...] = (
    "white",
    "orange",
    "magenta",
    "light_blue",
    "yellow",
    "lime",
    "pink",
    "gray",
    "silver",
    "cyan",
    "purple",
    "blue",
    "brown",
    "green",
    "red",
    "black",
)


# ======================================================================
# 异常
# ======================================================================


class StructureParserError(Exception):
    """所有建筑文件解析错误的基类。"""


class UnsupportedFormatError(StructureParserError):
    """不支持的文件格式。

    Attributes:
        filename: 文件名 (如有)。
        detail: 详细说明 (如有)。
    """

    def __init__(self, filename: str = "", detail: str = "") -> None:
        self.filename = filename
        self.detail = detail
        msg = "不支持的文件格式"
        if filename:
            msg += f": {filename}"
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


class CorruptFileError(StructureParserError):
    """文件损坏或格式不合法。

    Attributes:
        cause: 导致错误的原始异常 (如有)。
    """

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        self.cause = cause
        if cause is not None:
            message = f"{message}: {cause}"
        super().__init__(message)


# ======================================================================
# Java 版方块 ID -> Bedrock 方块名映射
# ======================================================================

# 经典 Java 版方块 ID (1.12 及更早, "Classic" 材质格式) 到 Bedrock 方块名的
# 基础映射。对于有 data 值区分变种的方块, 在 _JAVA_BLOCK_DATA_MAP 中进一步处理。
_JAVA_BLOCK_ID_MAP: dict[int, str] = {
    0: "minecraft:air",
    1: "minecraft:stone",
    2: "minecraft:grass",
    3: "minecraft:dirt",
    4: "minecraft:cobblestone",
    5: "minecraft:planks",
    6: "minecraft:sapling",
    7: "minecraft:bedrock",
    8: "minecraft:flowing_water",
    9: "minecraft:water",
    10: "minecraft:flowing_lava",
    11: "minecraft:lava",
    12: "minecraft:sand",
    13: "minecraft:gravel",
    14: "minecraft:gold_ore",
    15: "minecraft:iron_ore",
    16: "minecraft:coal_ore",
    17: "minecraft:log",
    18: "minecraft:leaves",
    19: "minecraft:sponge",
    20: "minecraft:glass",
    21: "minecraft:lapis_ore",
    22: "minecraft:lapis_block",
    23: "minecraft:dispenser",
    24: "minecraft:sandstone",
    25: "minecraft:noteblock",
    27: "minecraft:golden_rail",
    28: "minecraft:detector_rail",
    29: "minecraft:sticky_piston",
    30: "minecraft:web",
    31: "minecraft:tallgrass",
    32: "minecraft:deadbush",
    33: "minecraft:piston",
    35: "minecraft:wool",
    37: "minecraft:yellow_flower",
    38: "minecraft:red_flower",
    39: "minecraft:brown_mushroom",
    40: "minecraft:red_mushroom",
    41: "minecraft:gold_block",
    42: "minecraft:iron_block",
    43: "minecraft:double_stone_slab",
    44: "minecraft:stone_slab",
    45: "minecraft:brick_block",
    46: "minecraft:tnt",
    47: "minecraft:bookshelf",
    48: "minecraft:mossy_cobblestone",
    49: "minecraft:obsidian",
    50: "minecraft:torch",
    52: "minecraft:mob_spawner",
    53: "minecraft:oak_stairs",
    54: "minecraft:chest",
    56: "minecraft:diamond_ore",
    57: "minecraft:diamond_block",
    58: "minecraft:crafting_table",
    60: "minecraft:farmland",
    61: "minecraft:furnace",
    62: "minecraft:lit_furnace",
    64: "minecraft:wooden_door",
    65: "minecraft:ladder",
    66: "minecraft:rail",
    67: "minecraft:stone_stairs",
    68: "minecraft:wall_sign",
    71: "minecraft:iron_door",
    73: "minecraft:lit_redstone_ore",
    74: "minecraft:lit_redstone_ore",
    75: "minecraft:unlit_redstone_torch",
    76: "minecraft:redstone_torch",
    77: "minecraft:stone_button",
    78: "minecraft:snow_layer",
    79: "minecraft:ice",
    80: "minecraft:snow",
    81: "minecraft:cactus",
    82: "minecraft:clay",
    84: "minecraft:jukebox",
    85: "minecraft:fence",
    86: "minecraft:pumpkin",
    87: "minecraft:netherrack",
    88: "minecraft:soul_sand",
    89: "minecraft:glowstone",
    91: "minecraft:lit_pumpkin",
    95: "minecraft:stained_glass",
    96: "minecraft:trapdoor",
    97: "minecraft:monster_egg",
    98: "minecraft:stonebrick",
    99: "minecraft:brown_mushroom_block",
    100: "minecraft:red_mushroom_block",
    101: "minecraft:iron_bars",
    102: "minecraft:glass_pane",
    103: "minecraft:melon_block",
    104: "minecraft:pumpkin_stem",
    105: "minecraft:melon_stem",
    106: "minecraft:vine",
    107: "minecraft:fence_gate",
    108: "minecraft:brick_stairs",
    109: "minecraft:stone_brick_stairs",
    110: "minecraft:mycelium",
    112: "minecraft:nether_brick",
    113: "minecraft:nether_brick_fence",
    114: "minecraft:nether_brick_stairs",
    116: "minecraft:enchanting_table",
    119: "minecraft:end_portal",
    120: "minecraft:end_portal_frame",
    121: "minecraft:end_stone",
    122: "minecraft:dragon_egg",
    123: "minecraft:redstone_lamp",
    124: "minecraft:lit_redstone_lamp",
    125: "minecraft:double_wooden_slab",
    126: "minecraft:wooden_slab",
    127: "minecraft:cocoa",
    128: "minecraft:sandstone_stairs",
    129: "minecraft:emerald_ore",
    130: "minecraft:ender_chest",
    133: "minecraft:emerald_block",
    134: "minecraft:spruce_stairs",
    135: "minecraft:sandstone_stairs",
    136: "minecraft:quartz_ore",
    137: "minecraft:quartz_stairs",
    138: "minecraft:double_stone_slab2",
    139: "minecraft:stone_slab2",
    141: "minecraft:carrots",
    142: "minecraft:potatoes",
    145: "minecraft:anvil",
    146: "minecraft:trapped_chest",
    147: "minecraft:light_weighted_pressure_plate",
    148: "minecraft:heavy_weighted_pressure_plate",
    151: "minecraft:daylight_detector",
    152: "minecraft:redstone_block",
    153: "minecraft:quartz_ore",
    154: "minecraft:hopper",
    155: "minecraft:quartz_block",
    156: "minecraft:quartz_stairs",
    157: "minecraft:double_stone_slab2",
    158: "minecraft:stone_slab2",
    159: "minecraft:stained_hardened_clay",
    160: "minecraft:stained_glass_pane",
    161: "minecraft:leaves2",
    162: "minecraft:log2",
    163: "minecraft:acacia_stairs",
    164: "minecraft:dark_oak_stairs",
    165: "minecraft:slime",
    166: "minecraft:barrier",
    169: "minecraft:sea_lantern",
    170: "minecraft:hay_block",
    172: "minecraft:hardened_clay",
    173: "minecraft:coal_block",
    174: "minecraft:packed_ice",
    179: "minecraft:red_sandstone",
    180: "minecraft:red_sandstone_stairs",
    181: "minecraft:double_stone_slab3",
    182: "minecraft:stone_slab3",
    183: "minecraft:double_stone_slab4",
    184: "minecraft:stone_slab4",
    198: "minecraft:end_rod",
    199: "minecraft:chorus_plant",
    200: "minecraft:chorus_flower",
    201: "minecraft:purpur_block",
    202: "minecraft:purpur_pillar",
    203: "minecraft:purpur_stairs",
    206: "minecraft:bone_block",
    207: "minecraft:concrete",
    208: "minecraft:concrete_powder",
}

# 带 data 值变种的方块映射 (block_id, data) -> (bedrock_name, states_dict)。
# 对于未列出的 data 值, 回退到 _JAVA_BLOCK_ID_MAP 的基础方块 (默认状态)。
_JAVA_BLOCK_DATA_MAP: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {
    # 石头变种
    (1, 1): ("minecraft:stone", {"stone_type": "granite"}),
    (1, 2): ("minecraft:stone", {"stone_type": "granite_smooth"}),
    (1, 3): ("minecraft:stone", {"stone_type": "diorite"}),
    (1, 4): ("minecraft:stone", {"stone_type": "diorite_smooth"}),
    (1, 5): ("minecraft:stone", {"stone_type": "andesite"}),
    (1, 6): ("minecraft:stone", {"stone_type": "andesite_smooth"}),
    # 泥土变种
    (3, 1): ("minecraft:dirt", {"dirt_type": "coarse"}),
    (3, 2): ("minecraft:dirt", {"dirt_type": "normal"}),
    # 木板
    (5, 0): ("minecraft:planks", {"wood_type": "oak"}),
    (5, 1): ("minecraft:planks", {"wood_type": "spruce"}),
    (5, 2): ("minecraft:planks", {"wood_type": "birch"}),
    (5, 3): ("minecraft:planks", {"wood_type": "jungle"}),
    (5, 4): ("minecraft:planks", {"wood_type": "acacia"}),
    (5, 5): ("minecraft:planks", {"wood_type": "dark_oak"}),
    # 木头 (低 2 位区分树种)
    (17, 0): ("minecraft:log", {"old_log_type": "oak"}),
    (17, 1): ("minecraft:log", {"old_log_type": "spruce"}),
    (17, 2): ("minecraft:log", {"old_log_type": "birch"}),
    (17, 3): ("minecraft:log", {"old_log_type": "jungle"}),
    (162, 0): ("minecraft:log2", {"new_log_type": "acacia"}),
    (162, 1): ("minecraft:log2", {"new_log_type": "dark_oak"}),
    # 树叶 (低 2 位区分树种)
    (18, 0): ("minecraft:leaves", {"old_leaf_type": "oak"}),
    (18, 1): ("minecraft:leaves", {"old_leaf_type": "spruce"}),
    (18, 2): ("minecraft:leaves", {"old_leaf_type": "birch"}),
    (18, 3): ("minecraft:leaves", {"old_leaf_type": "jungle"}),
    (161, 0): ("minecraft:leaves2", {"new_leaf_type": "acacia"}),
    (161, 1): ("minecraft:leaves2", {"new_leaf_type": "dark_oak"}),
    # 沙岩
    (24, 0): ("minecraft:sandstone", {"sand_stone_type": "default"}),
    (24, 1): ("minecraft:sandstone", {"sand_stone_type": "heiroglyphic"}),
    (24, 2): ("minecraft:sandstone", {"sand_stone_type": "cut"}),
    # 红沙岩
    (179, 0): ("minecraft:red_sandstone", {"red_sandstone_type": "default"}),
    (179, 1): ("minecraft:red_sandstone", {"red_sandstone_type": "heiroglyphic"}),
    (179, 2): ("minecraft:red_sandstone", {"red_sandstone_type": "cut"}),
    # 石砖
    (98, 0): ("minecraft:stonebrick", {"stone_brick_type": "default"}),
    (98, 1): ("minecraft:stonebrick", {"stone_brick_type": "mossy"}),
    (98, 2): ("minecraft:stonebrick", {"stone_brick_type": "cracked"}),
    (98, 3): ("minecraft:stonebrick", {"stone_brick_type": "chiseled"}),
    # 草丛
    (31, 0): ("minecraft:tallgrass", {"tall_grass_type": "dead"}),
    (31, 1): ("minecraft:tallgrass", {"tall_grass_type": "grass"}),
    (31, 2): ("minecraft:tallgrass", {"tall_grass_type": "fern"}),
    # 花
    (38, 0): ("minecraft:red_flower", {"flower_type": "poppy"}),
    (38, 1): ("minecraft:red_flower", {"flower_type": "orchid"}),
    (38, 2): ("minecraft:red_flower", {"flower_type": "allium"}),
    (38, 3): ("minecraft:red_flower", {"flower_type": "houstonia"}),
    (38, 4): ("minecraft:red_flower", {"flower_type": "tulip_red"}),
    (38, 5): ("minecraft:red_flower", {"flower_type": "tulip_orange"}),
    (38, 6): ("minecraft:red_flower", {"flower_type": "tulip_white"}),
    (38, 7): ("minecraft:red_flower", {"flower_type": "tulip_pink"}),
    (38, 8): ("minecraft:red_flower", {"flower_type": "oxeye"}),
    # 石英块
    (155, 0): ("minecraft:quartz_block", {"chisel_type": "default"}),
    (155, 1): ("minecraft:quartz_block", {"chisel_type": "chiseled"}),
    (155, 2): ("minecraft:quartz_block", {"chisel_type": "lines"}),
    (155, 3): ("minecraft:quartz_block", {"chisel_type": "smooth"}),
    # 砖楼梯/石砖楼梯数据值在 Bedrock 中通常无 state 区分, 走默认。
    # 末地石砖等无变种, 走默认。
}

# 使用颜色 data 值 (0~15) 区分变种的 Java 方块 ID 集合。
# 这些方块的 data 值对应 _BEDROCK_COLORS 中的颜色, Bedrock 用 "color" 状态表示。
_COLOR_BLOCK_IDS: frozenset[int] = frozenset({35, 95, 159, 160, 207, 208})


def _java_block_to_bedrock(block_id: int, block_data: int) -> BlockState:
    """将经典 Java 版 (1.12 及更早) 方块 ID + data 转换为 Bedrock :class:`BlockState`。

    解析顺序:
        1. 优先查 JSON 映射表 (:func:`get_block_mapping`), 该表覆盖 253 个
           方块 ID 及其数据值变种, 并支持内嵌方块状态字符串。
        2. JSON 映射未命中 (返回 air 但 block_id != 0) 时, 回退到内置的
           硬编码映射: 先查 data 变种表, 再查颜色方块表, 最后查基础方块
           ID 映射。
        3. 仍未知则回退为 ``minecraft:stone`` 并记录警告。

    Args:
        block_id: Java 版方块 ID (0~255)。
        block_data: Java 版方块数据值 (0~15)。

    Returns:
        对应的 Bedrock :class:`BlockState` 对象。
    """
    # 空气
    if block_id == 0:
        return BlockState(name=AIR_BLOCK_NAME)

    # 优先查 JSON 映射表 (SchematicBlockMapping, 覆盖面最广)
    try:
        block_state = get_block_mapping().resolve_to_block_state(block_id, block_data)
    except Exception as exc:  # pragma: no cover - 容错保护
        logger.warning("查询 schematic 方块映射表异常: %s", exc)
        block_state = BlockState(name=AIR_BLOCK_NAME)

    # JSON 映射命中 (非 air, 因为非零 block_id 在 JSON 中不会映射到 air)
    if block_state.name != AIR_BLOCK_NAME:
        return block_state

    # 以下为 JSON 映射未命中时的硬编码回退逻辑

    # 优先查 data 变种表 (使用低 4 位 data, 忽略朝向等高位)
    low_data = block_data & 0x0F
    key = (block_id, low_data)
    if key in _JAVA_BLOCK_DATA_MAP:
        name, states = _JAVA_BLOCK_DATA_MAP[key]
        return BlockState(name=name, states=dict(states))

    # 颜色方块 (羊毛、染色玻璃、染色陶瓦、染色玻璃板、混凝土、混凝土粉末)
    if block_id in _COLOR_BLOCK_IDS:
        color = _BEDROCK_COLORS[low_data] if low_data < len(_BEDROCK_COLORS) else "white"
        name = _JAVA_BLOCK_ID_MAP.get(block_id, "minecraft:wool")
        return BlockState(name=name, states={"color": color})

    # 回退到基础方块 ID 映射
    name = _JAVA_BLOCK_ID_MAP.get(block_id)
    if name is None:
        logger.warning("未知的 Java 方块 ID: %d (data=%d), 回退为 stone", block_id, block_data)
        return BlockState(name="minecraft:stone")
    return BlockState(name=name)


def _parse_block_state_string(block_str: str) -> BlockState:
    """解析 schem v2 Palette 中的方块状态字符串。

    支持的格式:
        - ``minecraft:stone`` (仅方块名)
        - ``minecraft:stairs[facing=east,half=top,shape=straight]`` (逗号分隔)
        - ``minecraft:stairs[facing=east;half=top]`` (分号分隔, FAWE 旧格式)

    Args:
        block_str: 方块状态字符串。

    Returns:
        解析后的 :class:`BlockState` 对象。
    """
    text = block_str.strip()
    bracket_idx = text.find("[")

    # 无状态部分
    if bracket_idx == -1:
        return BlockState(name=text)

    name = text[:bracket_idx].strip()
    states: dict[str, Any] = {}

    # 提取方括号内的状态字符串
    close_idx = text.rfind("]")
    if close_idx == -1 or close_idx <= bracket_idx:
        # 没有闭合方括号, 仅返回方块名
        return BlockState(name=name)

    states_str = text[bracket_idx + 1:close_idx]
    # 同时支持逗号和分号分隔 (FAWE 旧格式用分号)
    for pair in states_str.replace(";", ",").split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        # 尝试将值转换为整数, 否则保留字符串
        try:
            states[key] = int(value)
        except ValueError:
            # 去除可能的引号
            if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                value = value[1:-1]
            states[key] = value

    return BlockState(name=name, states=states)


def _maybe_gunzip(data: bytes) -> bytes:
    """检测并解压 gzip 数据, 若非 gzip 则原样返回。

    通过检查前两个字节是否为 gzip 魔术字节 (0x1f 0x8b) 判断。

    Args:
        data: 可能 gzip 压缩的字节串。

    Returns:
        解压后的字节串 (若未压缩则为原始数据)。

    Raises:
        CorruptFileError: gzip 数据损坏。
    """
    if len(data) >= 2 and data[0:2] == _GZIP_MAGIC:
        try:
            return gzip.decompress(data)
        except OSError as exc:
            raise CorruptFileError("gzip 解压失败", exc) from exc
    return data


def _to_native_value(value: Any) -> Any:
    """将 NBT 包装类型 (Byte/Short/Int/Long/Float/Double) 转为原生 Python 类型。

    Args:
        value: 可能是 NBT 包装类型的值。

    Returns:
        转换后的原生 Python 值 (int/float/str/bool)。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


def _states_from_nbt(states_nbt: Any) -> dict[str, Any]:
    """从 NBT 复合标签提取方块状态字典 (值转为原生类型)。

    Args:
        states_nbt: NBT 复合标签 (dict) 或 None。

    Returns:
        方块状态字典。输入为 None 或非 dict 时返回空字典。
    """
    if not isinstance(states_nbt, dict):
        return {}
    return {str(k): _to_native_value(v) for k, v in states_nbt.items()}


# ======================================================================
# ParsedStructure — 解析后的建筑结构
# ======================================================================


@dataclass
class ParsedStructure:
    """解析后的建筑结构。

    统一表示来自不同格式 (mcstructure / nbt / schematic / schem / bdx /
    mcworld / litematic) 的建筑数据。方块信息通过 ``blocks`` 列表 (去重的
    方块状态) 和 ``block_data`` 3D 索引数组共同表示: ``block_data[x][y][z]``
    的值是 ``blocks`` 列表中的索引, 若为负值 (如 -1) 表示空气。

    Attributes:
        size: 结构尺寸 ``(width, height, length)``, 即 ``(x, y, z)``。
        blocks: 方块状态列表 (按索引引用, 通常已去重)。
        block_data: 3D 方块索引数组, 形状为 ``[width][height][length]``。
            每个元素是 ``blocks`` 列表的索引, 负值表示空气。
        entities: 实体列表 (每个实体为一个 dict, 来自 NBT 复合标签)。
        block_entities: 方块实体列表 (如箱子、熔炉等含 NBT 数据的方块)。
        format: 源文件格式名称 (如 ``"mcstructure"``、``"schematic"``)。
        offset: 结构原点偏移 ``(x, y, z)``。
        metadata: 额外元数据 (如 DataVersion、Materials、author 等)。
    """

    size: tuple[int, int, int]
    blocks: list[BlockState]
    block_data: list[list[list[int]]]
    entities: list[dict] = field(default_factory=list)
    block_entities: list[dict] = field(default_factory=list)
    format: str = ""
    offset: tuple[int, int, int] = (0, 0, 0)
    metadata: dict = field(default_factory=dict)

    def get_block_at(self, x: int, y: int, z: int) -> Optional[BlockState]:
        """获取指定位置的方块状态。

        Args:
            x: X 坐标 (0 ~ width-1)。
            y: Y 坐标 (0 ~ height-1)。
            z: Z 坐标 (0 ~ length-1)。

        Returns:
            该位置的 :class:`BlockState` 对象。若坐标越界或该位置为空气,
            返回 ``None``。
        """
        width, height, length = self.size
        if not (0 <= x < width and 0 <= y < height and 0 <= z < length):
            return None
        idx = self.block_data[x][y][z]
        if idx < 0 or idx >= len(self.blocks):
            return None
        return self.blocks[idx]

    def iter_blocks(self) -> Iterator[tuple[int, int, int, BlockState]]:
        """遍历所有非空气方块。

        以 ``x -> y -> z`` 的顺序遍历整个结构, 跳过空气方块。

        Yields:
            ``(x, y, z, block_state)`` 元组, 其中 ``block_state`` 是该位置的
            :class:`BlockState` 对象。
        """
        width, height, length = self.size
        for x in range(width):
            plane = self.block_data[x]
            for y in range(height):
                row = plane[y]
                for z in range(length):
                    idx = row[z]
                    if idx < 0 or idx >= len(self.blocks):
                        continue
                    yield x, y, z, self.blocks[idx]

    def get_block_count(self) -> int:
        """获取非空气方块数量。

        Returns:
            结构中非空气方块的总数。
        """
        count = 0
        width, height, length = self.size
        for x in range(width):
            for y in range(height):
                for z in range(length):
                    if self.block_data[x][y][z] >= 0:
                        count += 1
        return count

    def get_block_entity_at(self, x: int, y: int, z: int) -> Optional[dict]:
        """获取指定位置的方块实体 NBT 数据。

        在 ``block_entities`` 列表中查找坐标匹配的方块实体。
        方块实体的坐标存储在 ``pos`` 字段中 (格式为 ``[x, y, z]``)。

        Args:
            x: X 坐标 (相对坐标, 0 ~ width-1)。
            y: Y 坐标 (相对坐标, 0 ~ height-1)。
            z: Z 坐标 (相对坐标, 0 ~ length-1)。

        Returns:
            匹配的方块实体 dict (含 NBT 数据), 未找到时返回 ``None``。
        """
        for be in self.block_entities:
            if not isinstance(be, dict):
                continue
            pos = be.get("pos")
            if not isinstance(pos, (list, tuple)) or len(pos) != 3:
                continue
            try:
                if int(pos[0]) == x and int(pos[1]) == y and int(pos[2]) == z:
                    return be
            except (TypeError, ValueError):
                continue
        return None

    def __repr__(self) -> str:
        return (
            f"ParsedStructure(format={self.format!r}, "
            f"size={self.size}, blocks={len(self.blocks)}, "
            f"count={self.get_block_count()}, "
            f"entities={len(self.entities)}, "
            f"block_entities={len(self.block_entities)})"
        )


# ======================================================================
# StructureParser — 建筑文件解析器
# ======================================================================


class StructureParser:
    """建筑文件解析器。

    支持自动检测格式并解析以下文件:
        - ``.mcstructure`` (Bedrock 原生, 小端磁盘 NBT)
        - ``.nbt`` (Java/Bedrock 结构文件, 可能 gzip 压缩)
        - ``.schematic`` (WorldEdit v1, gzip 压缩大端 NBT)
        - ``.schem`` (WorldEdit v2, gzip 压缩大端 NBT, Varint 索引)
        - ``.bdx`` (FastBuilder/PhoenixBuilder, 二进制 varint + 网络 NBT)
        - ``.mcworld`` (Bedrock 世界存档, ZIP + level.dat)
        - ``.litematic`` (Litematica mod, gzip 压缩大端 NBT, 位打包)

    Example::

        parser = StructureParser()
        structure = await parser.parse_file("house.schem")
        print(structure.size, structure.get_block_count())
    """

    #: 方块数据最大总体积上限 (防止恶意文件导致内存耗尽)
    MAX_BLOCK_VOLUME: int = 100_000_000

    # ------------------------------------------------------------------
    # 文件读取与格式检测
    # ------------------------------------------------------------------

    async def parse_file(self, file_path: str) -> ParsedStructure:
        """解析建筑文件 (自动检测格式)。

        根据文件扩展名和文件头 (魔术字节) 判断格式, 读取文件内容并调用
        对应的解析方法。

        Args:
            file_path: 建筑文件路径。

        Returns:
            解析后的 :class:`ParsedStructure` 对象。

        Raises:
            FileNotFoundError: 文件不存在。
            UnsupportedFormatError: 不支持的文件格式。
            CorruptFileError: 文件损坏或解析失败。
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        with open(file_path, "rb") as f:
            data = f.read()

        filename = os.path.basename(file_path)
        fmt = self.detect_format(data, filename)
        logger.debug("检测到格式 %r (文件: %s)", fmt, filename)

        return self._dispatch_parse(fmt, data)

    def detect_format(self, data: bytes, filename: str) -> str:
        """检测文件格式。

        优先使用文件扩展名判断; 若扩展名无法识别, 则通过文件头魔术字节
        和内容启发式分析。

        检测逻辑:
            1. 扩展名匹配 (最可靠)。
            2. 文件头魔术字节:
               - ``"BDX\\x00"`` -> bdx (FastBuilder/PhoenixBuilder)
               - ``"PK\\x03\\x04"`` (ZIP) 且含 level.dat -> mcworld
            3. 内容启发式: 解压 gzip 后检查 NBT 根标签字段名。
               - 含 ``"Palette"`` + ``"BlockData"`` -> schem
               - 含 ``"Blocks"`` + ``"Data"`` -> schematic
               - 含 ``"structure"`` + ``"block_indices"`` -> mcstructure
               - 含 ``"palette"`` + ``"blocks"`` (小写) -> nbt (Java 结构)
               - 含 ``"MinecraftData"`` + ``"Regions"`` -> litematic
            4. 无法识别时抛出 :class:`UnsupportedFormatError`。

        Args:
            data: 文件内容字节串。
            filename: 文件名 (用于扩展名判断)。

        Returns:
            格式名称 (见 :data:`SUPPORTED_FORMATS`)。

        Raises:
            UnsupportedFormatError: 无法识别文件格式。
        """
        # 1. 扩展名匹配
        _, ext = os.path.splitext(filename)
        ext_lower = ext.lower()
        if ext_lower in _EXTENSION_MAP:
            return _EXTENSION_MAP[ext_lower]

        # 2. 文件头魔术字节检测
        # BDX 魔术字节 "BDX\x00" (唯一, 可直接判断)
        if len(data) >= 4 and data[:4] == _BDX_MAGIC:
            return FORMAT_BDX

        # ZIP 魔术字节 "PK\x03\x04" (mcworld 是含 level.dat 的 ZIP)
        if len(data) >= 4 and data[:4] == _ZIP_MAGIC:
            if self._is_mcworld_zip(data):
                return FORMAT_MCWORLD

        # 3. 内容启发式检测 (含 litematic 的 NBT 结构检测)
        return self._detect_by_content(data, filename)

    def _detect_by_content(self, data: bytes, filename: str) -> str:
        """通过文件内容启发式检测格式。

        Args:
            data: 文件内容字节串。
            filename: 文件名 (用于错误消息)。

        Returns:
            格式名称。

        Raises:
            UnsupportedFormatError: 无法识别格式。
        """
        # 尝试解压 gzip 并分析 NBT 字段
        candidates: list[tuple[str, Any]] = []

        # 候选 1: 原始数据 (mcstructure / 未压缩 nbt / bdx 已在前面处理)
        candidates.append(("raw", data))
        # 候选 2: gzip 解压后 (schematic / schem / 压缩 nbt / litematic)
        if len(data) >= 2 and data[0:2] == _GZIP_MAGIC:
            try:
                decompressed = gzip.decompress(data)
                candidates.append(("gzip", decompressed))
            except OSError:
                pass

        for _, payload in candidates:
            for fmt in (self._probe_schem, self._probe_schematic,
                        self._probe_mcstructure, self._probe_nbt_java,
                        self._probe_litematic):
                result = fmt(payload)
                if result is not None:
                    return result

        raise UnsupportedFormatError(
            filename=filename,
            detail="无法通过扩展名或文件内容识别格式",
        )

    # -- 内容探测辅助方法 (返回格式名或 None) --

    @staticmethod
    def _safe_unmarshal(data: bytes, encoding: str) -> Any:
        """安全解码 NBT, 失败返回 None。"""
        try:
            from .nbt import LITTLE_ENDIAN, BIG_ENDIAN
            if encoding == "disk":
                return unmarshal_disk(data)
            if encoding == "big":
                return unmarshal_big_endian(data)
            return None
        except (NBTError, ValueError, IndexError):
            return None

    @staticmethod
    def _probe_mcstructure(data: bytes) -> Optional[str]:
        """探测是否为 mcstructure (小端磁盘 NBT + structure 字段)。"""
        nbt = StructureParser._safe_unmarshal(data, "disk")
        if not isinstance(nbt, dict):
            return None
        if "structure" in nbt and "size" in nbt:
            structure = nbt.get("structure")
            if isinstance(structure, dict) and "block_indices" in structure:
                return FORMAT_MCSTRUCTURE
        return None

    @staticmethod
    def _probe_schematic(data: bytes) -> Optional[str]:
        """探测是否为 schematic (大端 NBT + Blocks/Data 字段)。"""
        nbt = StructureParser._safe_unmarshal(data, "big")
        if not isinstance(nbt, dict):
            return None
        if "Blocks" in nbt and "Data" in nbt and "Width" in nbt:
            return FORMAT_SCHEMATIC
        return None

    @staticmethod
    def _probe_schem(data: bytes) -> Optional[str]:
        """探测是否为 schem v2 (大端 NBT + Palette/BlockData 字段)。"""
        nbt = StructureParser._safe_unmarshal(data, "big")
        if not isinstance(nbt, dict):
            return None
        if "Palette" in nbt and "BlockData" in nbt:
            return FORMAT_SCHEM
        return None

    @staticmethod
    def _probe_nbt_java(data: bytes) -> Optional[str]:
        """探测是否为 Java 结构方块 .nbt (大端 NBT + palette/blocks 字段)。"""
        nbt = StructureParser._safe_unmarshal(data, "big")
        if not isinstance(nbt, dict):
            return None
        if "palette" in nbt and "blocks" in nbt and "size" in nbt:
            return FORMAT_NBT
        # Bedrock 风格的 .nbt (小端磁盘 NBT, 含 structure 字段) 也归为 nbt
        nbt_le = StructureParser._safe_unmarshal(data, "disk")
        if isinstance(nbt_le, dict) and "structure" in nbt_le and "size" in nbt_le:
            return FORMAT_NBT
        return None

    @staticmethod
    def _probe_litematic(data: bytes) -> Optional[str]:
        """探测是否为 litematic (大端 NBT + MinecraftData/Regions 字段)。

        Litematica mod 的 .litematic 文件使用 Java 大端 NBT, 根复合标签
        包含 ``"MinecraftData"`` 字段, 其下有 ``"Regions"`` 复合标签。
        """
        nbt = StructureParser._safe_unmarshal(data, "big")
        if not isinstance(nbt, dict):
            return None
        mc_data = nbt.get("MinecraftData")
        if isinstance(mc_data, dict) and "Regions" in mc_data:
            return FORMAT_LITEMATIC
        return None

    @staticmethod
    def _is_mcworld_zip(data: bytes) -> bool:
        """检查 ZIP 压缩包是否为 mcworld (含 level.dat)。

        mcworld 文件本质是 ZIP 压缩包, 必须包含 ``level.dat`` 文件
        (Bedrock 世界存档的根 NBT 文件)。

        Args:
            data: 可能是 mcworld 的字节串。

        Returns:
            若 ZIP 中含 ``level.dat`` 返回 True, 否则 False。
        """
        try:
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                for name in zf.namelist():
                    if name.lower() == "level.dat":
                        return True
        except (zipfile.BadZipFile, OSError):
            pass
        return False

    # ------------------------------------------------------------------
    # 解析分发
    # ------------------------------------------------------------------

    def _dispatch_parse(self, fmt: str, data: bytes) -> ParsedStructure:
        """根据格式名称分发到对应的解析方法。

        Args:
            fmt: 格式名称。
            data: 文件内容字节串。

        Returns:
            解析后的 :class:`ParsedStructure`。

        Raises:
            UnsupportedFormatError: 未知格式。
            CorruptFileError: 解析失败。
        """
        try:
            if fmt == FORMAT_MCSTRUCTURE:
                return self.parse_mcstructure(data)
            if fmt == FORMAT_NBT:
                return self.parse_nbt(data)
            if fmt == FORMAT_SCHEMATIC:
                return self.parse_schematic(data)
            if fmt == FORMAT_SCHEM:
                return self.parse_schem(data)
            if fmt == FORMAT_BDX:
                return self.parse_bdx(data)
            if fmt == FORMAT_MCWORLD:
                return self.parse_mcworld(data)
            if fmt == FORMAT_LITEMATIC:
                return self.parse_litematic(data)
        except StructureParserError:
            raise
        except NBTError as exc:
            raise CorruptFileError(f"解析 {fmt} 文件时 NBT 解码失败", exc) from exc
        except (IndexError, KeyError, ValueError, TypeError) as exc:
            raise CorruptFileError(f"解析 {fmt} 文件时发生错误", exc) from exc

        raise UnsupportedFormatError(detail=f"未知格式: {fmt}")

    # ------------------------------------------------------------------
    # mcstructure 解析 (Bedrock 原生)
    # ------------------------------------------------------------------

    def parse_mcstructure(self, data: bytes) -> ParsedStructure:
        """解析 ``.mcstructure`` 文件 (Bedrock 原生小端磁盘 NBT)。

        mcstructure 格式使用小端序磁盘 NBT, 结构如下::

            TAG_Compound {
                "format_version": Int(1),
                "size": [Int, Int, Int],
                "structure": {
                    "block_indices": [[Int...], [Int...]],
                    "entities": [...],
                    "palette": {"default": {"block_palette": [...]}}
                }
            }

        ``block_indices[0]`` (正层) 是方块索引数组, 值为 -1 表示空气,
        其他值为 ``block_palette`` 列表的索引。索引顺序为 YZX
        (Y 最外层, X 最内层): ``idx = x + z*size_x + y*size_x*size_z``。

        Args:
            data: mcstructure 文件的原始字节串 (未压缩)。

        Returns:
            解析后的 :class:`ParsedStructure`。

        Raises:
            CorruptFileError: 文件损坏或格式不合法。
        """
        nbt = unmarshal_disk(data)
        if not isinstance(nbt, dict):
            raise CorruptFileError("mcstructure 根标签不是复合标签")

        # 尺寸 [x, y, z]
        size_list = nbt.get("size")
        if not isinstance(size_list, list) or len(size_list) != 3:
            raise CorruptFileError("mcstructure 缺少有效的 size 字段")
        size_x, size_y, size_z = (int(size_list[0]), int(size_list[1]), int(size_list[2]))
        size = (size_x, size_y, size_z)
        self._check_volume(size_x, size_y, size_z)

        structure = nbt.get("structure")
        if not isinstance(structure, dict):
            raise CorruptFileError("mcstructure 缺少 structure 字段")

        # 方块调色板
        palette = structure.get("palette", {})
        if not isinstance(palette, dict):
            palette = {}
        default_palette = palette.get("default", {})
        if not isinstance(default_palette, dict):
            default_palette = {}
        block_palette = default_palette.get("block_palette", [])
        if not isinstance(block_palette, list):
            block_palette = []

        # 构建 BlockState 列表
        blocks: list[BlockState] = []
        for entry in block_palette:
            if not isinstance(entry, dict):
                blocks.append(BlockState(name=AIR_BLOCK_NAME))
                continue
            name = str(entry.get("name", AIR_BLOCK_NAME))
            states = _states_from_nbt(entry.get("states"))
            blocks.append(BlockState(name=name, states=states))

        # block_indices: [正层, 负层], 通常使用正层 (索引 0)
        block_indices = structure.get("block_indices", [])
        if not isinstance(block_indices, list) or len(block_indices) == 0:
            raise CorruptFileError("mcstructure 缺少 block_indices 字段")
        positive_layer = block_indices[_BLOCK_LAYER_POSITIVE]
        if not isinstance(positive_layer, list):
            raise CorruptFileError("mcstructure block_indices 正层不是列表")

        # 构建 3D 方块索引数组 [x][y][z]
        block_data = self._build_3d_from_flat_yzx(
            positive_layer, size_x, size_y, size_z
        )

        # 实体
        entities = self._extract_entities(structure.get("entities"))

        # 方块实体 (block_position_data)
        block_entities: list[dict] = []
        block_position_data = default_palette.get("block_position_data", {})
        if isinstance(block_position_data, dict):
            for pos_key, pos_nbt in block_position_data.items():
                if isinstance(pos_nbt, dict) and "block_entity" in pos_nbt:
                    be = pos_nbt.get("block_entity")
                    if isinstance(be, dict):
                        block_entities.append(be)

        metadata = {
            "format_version": int(nbt.get("format_version", 0)) if nbt.get("format_version") is not None else 0,
        }

        return ParsedStructure(
            size=size,
            blocks=blocks,
            block_data=block_data,
            entities=entities,
            block_entities=block_entities,
            format=FORMAT_MCSTRUCTURE,
            offset=(0, 0, 0),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # nbt 解析 (Java 结构方块 / Bedrock 结构)
    # ------------------------------------------------------------------

    def parse_nbt(self, data: bytes) -> ParsedStructure:
        """解析 ``.nbt`` 文件 (可能 gzip 压缩)。

        .nbt 文件来源多样, 本方法按以下顺序尝试:
            1. 检测并解压 gzip。
            2. 尝试作为大端 NBT (Java 结构方块格式, 1.13+) 解析。
            3. 尝试作为小端磁盘 NBT (Bedrock mcstructure 风格) 解析。

        Java 结构方块格式::

            TAG_Compound {
                "size": [Int, Int, Int],
                "palette": [{"Name": str, "Properties": {...}}],
                "blocks": [{"pos": [x,y,z], "state": int, "nbt": {...}}],
                "entities": [...],
                "DataVersion": Int
            }

        Args:
            data: .nbt 文件的字节串 (可能 gzip 压缩)。

        Returns:
            解析后的 :class:`ParsedStructure`。

        Raises:
            CorruptFileError: 文件损坏或格式不合法。
        """
        payload = _maybe_gunzip(data)

        # 优先尝试 Java 结构方块格式 (大端 NBT)
        try:
            nbt = unmarshal_big_endian(payload)
        except (NBTError, ValueError, IndexError) as exc:
            # 尝试小端磁盘 NBT (Bedrock 风格)
            try:
                nbt = unmarshal_disk(payload)
            except (NBTError, ValueError, IndexError) as exc2:
                raise CorruptFileError(
                    ".nbt 文件 NBT 解码失败 (大端和小端均无效)", exc2
                ) from exc2

        if not isinstance(nbt, dict):
            raise CorruptFileError(".nbt 根标签不是复合标签")

        # 判断子格式
        if "palette" in nbt and "blocks" in nbt and "size" in nbt:
            # Java 结构方块格式
            return self._parse_java_structure_nbt(nbt)

        if "structure" in nbt and "size" in nbt:
            # Bedrock mcstructure 风格 (保存为 .nbt)
            return self._parse_as_mcstructure_like(nbt)

        raise CorruptFileError(
            ".nbt 文件缺少必要的字段 (palette/blocks/size 或 structure/size)"
        )

    def _parse_java_structure_nbt(self, nbt: dict) -> ParsedStructure:
        """解析 Java 结构方块格式的 .nbt (大端 NBT, 1.13+)。

        Args:
            nbt: 已解码的 NBT 根复合标签。

        Returns:
            解析后的 :class:`ParsedStructure`。
        """
        size_list = nbt.get("size")
        if not isinstance(size_list, list) or len(size_list) != 3:
            raise CorruptFileError(".nbt 缺少有效的 size 字段")
        size_x, size_y, size_z = (int(size_list[0]), int(size_list[1]), int(size_list[2]))
        # Java 结构方块的 size 可能为负 (表示反向), 取绝对值
        size_x, size_y, size_z = abs(size_x), abs(size_y), abs(size_z)
        size = (size_x, size_y, size_z)
        self._check_volume(size_x, size_y, size_z)

        # 调色板: [{"Name": str, "Properties": {...}}]
        palette = nbt.get("palette", [])
        if not isinstance(palette, list):
            palette = []
        blocks: list[BlockState] = []
        for entry in palette:
            if not isinstance(entry, dict):
                blocks.append(BlockState(name=AIR_BLOCK_NAME))
                continue
            name = str(entry.get("Name", AIR_BLOCK_NAME))
            # Java 版 Properties 使用方块状态名 (如 "facing"), 直接作为 Bedrock states
            states = _states_from_nbt(entry.get("Properties"))
            blocks.append(BlockState(name=name, states=states))

        # blocks: [{"pos": [x,y,z], "state": int, "nbt": {...}}]
        blocks_list = nbt.get("blocks", [])
        if not isinstance(blocks_list, list):
            blocks_list = []

        # 初始化 3D 数组为空气 (-1)
        block_data: list[list[list[int]]] = [
            [[_AIR_INDEX for _z in range(size_z)] for _y in range(size_y)]
            for _x in range(size_x)
        ]

        block_entities: list[dict] = []
        for entry in blocks_list:
            if not isinstance(entry, dict):
                continue
            pos = entry.get("pos")
            state_idx = entry.get("state")
            if not isinstance(pos, list) or len(pos) != 3:
                continue
            if not isinstance(state_idx, int):
                continue
            x, y, z = int(pos[0]), int(pos[1]), int(pos[2])
            if 0 <= x < size_x and 0 <= y < size_y and 0 <= z < size_z:
                block_data[x][y][z] = state_idx
            # 方块实体数据
            be = entry.get("nbt")
            if isinstance(be, dict):
                be_copy = dict(be)
                be_copy.setdefault("pos", [x, y, z])
                block_entities.append(be_copy)

        entities = self._extract_entities(nbt.get("entities"))

        metadata: dict[str, Any] = {}
        if "DataVersion" in nbt:
            metadata["data_version"] = int(nbt["DataVersion"])
        if "author" in nbt:
            metadata["author"] = str(nbt["author"])

        return ParsedStructure(
            size=size,
            blocks=blocks,
            block_data=block_data,
            entities=entities,
            block_entities=block_entities,
            format=FORMAT_NBT,
            offset=(0, 0, 0),
            metadata=metadata,
        )

    def _parse_as_mcstructure_like(self, nbt: dict) -> ParsedStructure:
        """解析 Bedrock mcstructure 风格的 NBT (保存为 .nbt)。

        复用 mcstructure 解析逻辑。

        Args:
            nbt: 已解码的 NBT 根复合标签。

        Returns:
            解析后的 :class:`ParsedStructure`。
        """
        size_list = nbt.get("size")
        if not isinstance(size_list, list) or len(size_list) != 3:
            raise CorruptFileError("结构缺少有效的 size 字段")
        size_x, size_y, size_z = (int(size_list[0]), int(size_list[1]), int(size_list[2]))
        size = (size_x, size_y, size_z)
        self._check_volume(size_x, size_y, size_z)

        structure = nbt.get("structure")
        if not isinstance(structure, dict):
            raise CorruptFileError("结构缺少 structure 字段")

        palette = structure.get("palette", {})
        if not isinstance(palette, dict):
            palette = {}
        default_palette = palette.get("default", {})
        if not isinstance(default_palette, dict):
            default_palette = {}
        block_palette = default_palette.get("block_palette", [])
        if not isinstance(block_palette, list):
            block_palette = []

        blocks: list[BlockState] = []
        for entry in block_palette:
            if not isinstance(entry, dict):
                blocks.append(BlockState(name=AIR_BLOCK_NAME))
                continue
            name = str(entry.get("name", AIR_BLOCK_NAME))
            states = _states_from_nbt(entry.get("states"))
            blocks.append(BlockState(name=name, states=states))

        block_indices = structure.get("block_indices", [])
        if not isinstance(block_indices, list) or len(block_indices) == 0:
            raise CorruptFileError("结构缺少 block_indices 字段")
        positive_layer = block_indices[_BLOCK_LAYER_POSITIVE]
        if not isinstance(positive_layer, list):
            raise CorruptFileError("block_indices 正层不是列表")

        block_data = self._build_3d_from_flat_yzx(
            positive_layer, size_x, size_y, size_z
        )

        entities = self._extract_entities(structure.get("entities"))

        block_entities: list[dict] = []
        block_position_data = default_palette.get("block_position_data", {})
        if isinstance(block_position_data, dict):
            for pos_key, pos_nbt in block_position_data.items():
                if isinstance(pos_nbt, dict) and "block_entity" in pos_nbt:
                    be = pos_nbt.get("block_entity")
                    if isinstance(be, dict):
                        block_entities.append(be)

        return ParsedStructure(
            size=size,
            blocks=blocks,
            block_data=block_data,
            entities=entities,
            block_entities=block_entities,
            format=FORMAT_NBT,
            offset=(0, 0, 0),
            metadata={},
        )

    # ------------------------------------------------------------------
    # schematic 解析 (WorldEdit v1)
    # ------------------------------------------------------------------

    def parse_schematic(self, data: bytes) -> ParsedStructure:
        """解析 ``.schematic`` 文件 (WorldEdit Schematic v1)。

        schematic 格式使用 gzip 压缩的大端 NBT, 结构如下::

            TAG_Compound {
                "Width": Short, "Height": Short, "Length": Short,
                "Materials": String,   // "Alpha" 或 "Classic"
                "Blocks": ByteArray,   // 方块 ID 数组
                "Data": ByteArray,     // 方块数据数组
                "BlockEntities": [...],
                "Entities": [...]
            }

        ``Blocks`` 和 ``Data`` 是平铺的字节数组, 索引顺序为 YZX:
        ``idx = x + z*width + y*width*length``。方块 ID 为经典 Java 版编号,
        需要通过 :func:`_java_block_to_bedrock` 映射到 Bedrock 方块名。

        ``_java_block_to_bedrock`` 优先使用 ``data/block_mapping.json`` 中
        的 JSON 映射表 (通过 :func:`get_block_mapping`), 未命中时回退到
        内置硬编码映射。

        Args:
            data: .schematic 文件的字节串 (gzip 压缩)。

        Returns:
            解析后的 :class:`ParsedStructure`。

        Raises:
            CorruptFileError: 文件损坏或格式不合法。
        """
        payload = _maybe_gunzip(data)
        nbt = unmarshal_big_endian(payload)
        if not isinstance(nbt, dict):
            raise CorruptFileError("schematic 根标签不是复合标签")

        width = int(nbt.get("Width", 0))
        height = int(nbt.get("Height", 0))
        length = int(nbt.get("Length", 0))
        if width <= 0 or height <= 0 or length <= 0:
            raise CorruptFileError(
                f"schematic 尺寸无效: width={width}, height={height}, length={length}"
            )
        size = (width, height, length)
        self._check_volume(width, height, length)

        materials = str(nbt.get("Materials", "Classic"))
        blocks_bytes = nbt.get("Blocks", b"")
        data_bytes = nbt.get("Data", b"")
        if not isinstance(blocks_bytes, (bytes, bytearray)):
            blocks_bytes = bytes(blocks_bytes) if blocks_bytes else b""
        if not isinstance(data_bytes, (bytes, bytearray)):
            data_bytes = bytes(data_bytes) if data_bytes else b""

        expected = width * height * length
        if len(blocks_bytes) < expected:
            raise CorruptFileError(
                f"schematic Blocks 数组长度不足: {len(blocks_bytes)} < {expected}"
            )

        # 构建 (block_id, block_data) -> palette 索引 的映射, 去重
        palette_map: dict[tuple[int, int], int] = {}
        blocks: list[BlockState] = []
        # 预置空气
        palette_map[(0, 0)] = 0
        blocks.append(BlockState(name=AIR_BLOCK_NAME))

        block_data: list[list[list[int]]] = [
            [[0 for _z in range(length)] for _y in range(height)]
            for _x in range(width)
        ]

        for y in range(height):
            for z in range(length):
                for x in range(width):
                    flat_idx = x + z * width + y * width * length
                    block_id = blocks_bytes[flat_idx]
                    block_data_val = data_bytes[flat_idx] if flat_idx < len(data_bytes) else 0
                    key = (block_id, block_data_val)
                    if key not in palette_map:
                        palette_map[key] = len(blocks)
                        blocks.append(_java_block_to_bedrock(block_id, block_data_val))
                    block_data[x][y][z] = palette_map[key]

        entities = self._extract_entities(nbt.get("Entities"))
        block_entities = self._extract_entities(nbt.get("BlockEntities"))

        metadata = {"materials": materials}
        if "WEOriginX" in nbt:
            metadata["we_origin"] = (
                int(nbt.get("WEOriginX", 0)),
                int(nbt.get("WEOriginY", 0)),
                int(nbt.get("WEOriginZ", 0)),
            )

        return ParsedStructure(
            size=size,
            blocks=blocks,
            block_data=block_data,
            entities=entities,
            block_entities=block_entities,
            format=FORMAT_SCHEMATIC,
            offset=(0, 0, 0),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # schem 解析 (WorldEdit v2)
    # ------------------------------------------------------------------

    def parse_schem(self, data: bytes) -> ParsedStructure:
        """解析 ``.schem`` 文件 (WorldEdit Schematic v2)。

        schem 格式使用 gzip 压缩的大端 NBT, 结构如下::

            TAG_Compound {
                "Version": Int(2), "DataVersion": Int,
                "Metadata": {"WEOffsetX/Y/Z": Int},
                "Width": Short, "Height": Short, "Length": Short,
                "Offset": [Int, Int, Int],
                "Palette": {"minecraft:stone": Int, ...},  // 方块名 -> 索引
                "BlockData": ByteArray,  // Varint 编码的索引序列
                "BlockEntities": [...], "Entities": [...]
            }

        ``BlockData`` 是 Varuint32 编码的索引序列 (每个索引指向 Palette
        中的方块), 顺序为 YZX: ``idx = x + z*width + y*width*length``。
        Palette 的键是命名空间方块名, 可能内嵌方块状态 (如
        ``minecraft:stairs[facing=east,half=top]``)。

        Args:
            data: .schem 文件的字节串 (gzip 压缩)。

        Returns:
            解析后的 :class:`ParsedStructure`。

        Raises:
            CorruptFileError: 文件损坏或格式不合法。
        """
        payload = _maybe_gunzip(data)
        nbt = unmarshal_big_endian(payload)
        if not isinstance(nbt, dict):
            raise CorruptFileError("schem 根标签不是复合标签")

        version = int(nbt.get("Version", 2))
        if version < 2:
            raise CorruptFileError(f"不支持的 schem 版本: {version}")

        width = int(nbt.get("Width", 0))
        height = int(nbt.get("Height", 0))
        length = int(nbt.get("Length", 0))
        if width <= 0 or height <= 0 or length <= 0:
            raise CorruptFileError(
                f"schem 尺寸无效: width={width}, height={height}, length={length}"
            )
        size = (width, height, length)
        self._check_volume(width, height, length)

        # 偏移
        offset_list = nbt.get("Offset", [0, 0, 0])
        if isinstance(offset_list, list) and len(offset_list) == 3:
            offset = (int(offset_list[0]), int(offset_list[1]), int(offset_list[2]))
        else:
            offset = (0, 0, 0)

        # 调色板: {方块名: 索引}, 反转为 {索引: BlockState}
        palette_nbt = nbt.get("Palette", {})
        if not isinstance(palette_nbt, dict) or not palette_nbt:
            raise CorruptFileError("schem 缺少 Palette 字段或为空")

        max_index = -1
        palette_by_index: dict[int, BlockState] = {}
        for block_str, idx in palette_nbt.items():
            idx = int(idx)
            palette_by_index[idx] = _parse_block_state_string(str(block_str))
            if idx > max_index:
                max_index = idx

        blocks: list[BlockState] = [
            palette_by_index.get(i, BlockState(name=AIR_BLOCK_NAME))
            for i in range(max_index + 1)
        ]

        # BlockData: Varuint32 编码的索引序列
        block_data_bytes = nbt.get("BlockData", b"")
        if not isinstance(block_data_bytes, (bytes, bytearray)):
            block_data_bytes = bytes(block_data_bytes) if block_data_bytes else b""

        indices = self._decode_varint_indices(bytes(block_data_bytes), width * height * length)

        # 构建 3D 数组
        block_data: list[list[list[int]]] = [
            [[0 for _z in range(length)] for _y in range(height)]
            for _x in range(width)
        ]

        for y in range(height):
            for z in range(length):
                for x in range(width):
                    flat_idx = x + z * width + y * width * length
                    if flat_idx < len(indices):
                        block_data[x][y][z] = indices[flat_idx]
                    else:
                        block_data[x][y][z] = 0  # 默认空气

        entities = self._extract_entities(nbt.get("Entities"))
        block_entities = self._extract_entities(nbt.get("BlockEntities"))

        metadata: dict[str, Any] = {"version": version}
        if "DataVersion" in nbt:
            metadata["data_version"] = int(nbt["DataVersion"])
        meta_nbt = nbt.get("Metadata", {})
        if isinstance(meta_nbt, dict) and meta_nbt:
            metadata["we_metadata"] = {
                str(k): _to_native_value(v) for k, v in meta_nbt.items()
            }

        return ParsedStructure(
            size=size,
            blocks=blocks,
            block_data=block_data,
            entities=entities,
            block_entities=block_entities,
            format=FORMAT_SCHEM,
            offset=offset,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # bdx 解析 (FastBuilder/PhoenixBuilder)
    # ------------------------------------------------------------------

    def parse_bdx(self, data: bytes) -> ParsedStructure:
        """解析 ``.bdx`` 文件 (FastBuilder/PhoenixBuilder 建筑文件)。

        bdx 是 FastBuilder/PhoenixBuilder 使用的二进制建筑文件格式, 结构如下::

            [魔术字 "BDX\\x00" (4 字节)]
            [版本号: varuint32]
            [作者名: varuint32 长度 + UTF8 字符串]
            [导出器名: varuint32 长度 + UTF8 字符串]
            [命令块列表]:
                每个命令块包含:
                    [命令类型: varuint32]
                    [操作码: varuint32]
                    [数据 (取决于操作码)]

        关键操作码:
            - 0 (placeBlock): 放置方块
                [x, y, z: varint32 (有符号 ZigZag)]
                [方块名: varuint32 长度 + UTF8 字符串]
                [states: 网络 NBT (小端, Varint 长度)]
            - 1 (placeBlockProvider): 放置方块提供者
                [x, y, z: varint32 (有符号 ZigZag)]
                [提供者名: varuint32 长度 + UTF8 字符串]

        坐标使用有符号 Varint (ZigZag) 编码, 方块 states 使用网络 NBT 格式
        (与 Bedrock 网络协议一致)。

        由于 bdx 存储的是离散的方块放置命令 (而非规则的三维数组), 本方法
        会计算所有方块的包围盒, 将最小坐标作为原点偏移, 并构建 3D 索引数组。

        Args:
            data: .bdx 文件的原始字节串。

        Returns:
            解析后的 :class:`ParsedStructure`。``offset`` 为方块坐标的
           最小值 (即包围盒原点)。

        Raises:
            CorruptFileError: 文件损坏或格式不合法。
        """
        if len(data) < 4 or data[:4] != _BDX_MAGIC:
            raise CorruptFileError("bdx 文件头魔术字无效 (期望 BDX\\x00)")

        try:
            offset = 4
            # 版本号
            version, offset = decode_varuint32(data, offset)
            # 作者名: varuint32 长度 + UTF8 字符串
            author, offset = self._read_varuint_string(data, offset)
            # 导出器名: varuint32 长度 + UTF8 字符串
            exporter, offset = self._read_varuint_string(data, offset)

            # 解析命令块列表
            block_records: list[tuple[int, int, int, BlockState]] = []
            data_len = len(data)
            while offset < data_len:
                cmd_type, offset = decode_varuint32(data, offset)
                opcode, offset = decode_varuint32(data, offset)

                if opcode == _BDX_OP_PLACE_BLOCK:
                    # placeBlock: x, y, z (有符号 varint), 方块名, states (NBT)
                    x, offset = decode_varint32(data, offset)
                    y, offset = decode_varint32(data, offset)
                    z, offset = decode_varint32(data, offset)
                    block_name, offset = self._read_varuint_string(data, offset)
                    states_nbt, offset = self._read_network_nbt_payload(data, offset)
                    states = (
                        _states_from_nbt(states_nbt)
                        if isinstance(states_nbt, dict)
                        else {}
                    )
                    block_records.append(
                        (x, y, z, BlockState(name=block_name, states=states))
                    )
                elif opcode == _BDX_OP_PLACE_BLOCK_PROVIDER:
                    # placeBlockProvider: x, y, z (有符号 varint), 提供者名
                    x, offset = decode_varint32(data, offset)
                    y, offset = decode_varint32(data, offset)
                    z, offset = decode_varint32(data, offset)
                    provider, offset = self._read_varuint_string(data, offset)
                    # 提供者名作为方块名 (简化处理)
                    block_records.append(
                        (x, y, z, BlockState(name=provider))
                    )
                else:
                    # 未知操作码, 无法确定数据长度, 停止解析
                    logger.warning(
                        "bdx 遇到未知操作码 %d (offset=%d), 停止解析剩余数据",
                        opcode, offset,
                    )
                    break
        except ValueError as exc:
            raise CorruptFileError("bdx 文件解析失败 (varint 解码错误)", exc) from exc
        except NBTError as exc:
            raise CorruptFileError("bdx 文件 states NBT 解码失败", exc) from exc

        metadata: dict[str, Any] = {
            "version": version,
            "author": author,
            "exporter": exporter,
        }

        # 无方块记录时返回空结构
        if not block_records:
            return ParsedStructure(
                size=(0, 0, 0),
                blocks=[BlockState(name=AIR_BLOCK_NAME)],
                block_data=[],
                format=FORMAT_BDX,
                offset=(0, 0, 0),
                metadata=metadata,
            )

        # 计算包围盒
        xs = [r[0] for r in block_records]
        ys = [r[1] for r in block_records]
        zs = [r[2] for r in block_records]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        min_z, max_z = min(zs), max(zs)
        size_x = max_x - min_x + 1
        size_y = max_y - min_y + 1
        size_z = max_z - min_z + 1
        self._check_volume(size_x, size_y, size_z)

        # 构建去重调色板和 3D 方块索引数组
        palette_map: dict[str, int] = {}
        blocks: list[BlockState] = []
        block_data: list[list[list[int]]] = [
            [[_AIR_INDEX for _z in range(size_z)] for _y in range(size_y)]
            for _x in range(size_x)
        ]
        for x, y, z, block in block_records:
            # 使用 BlockState 的 SNBT 表示作为去重键
            key = block.to_snbt()
            if key not in palette_map:
                palette_map[key] = len(blocks)
                blocks.append(block)
            idx = palette_map[key]
            bx, by, bz = x - min_x, y - min_y, z - min_z
            block_data[bx][by][bz] = idx

        return ParsedStructure(
            size=(size_x, size_y, size_z),
            blocks=blocks,
            block_data=block_data,
            format=FORMAT_BDX,
            offset=(min_x, min_y, min_z),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # mcworld 解析 (Bedrock 世界存档)
    # ------------------------------------------------------------------

    def parse_mcworld(self, data: bytes) -> ParsedStructure:
        """解析 ``.mcworld`` 文件 (Bedrock 世界存档)。

        mcworld 文件本质是一个 ZIP 压缩包, 包含:
            - ``level.dat``: Bedrock 小端磁盘 NBT (世界元数据, 可能 gzip 压缩)
            - ``db/``: LevelDB 数据库目录 (区块数据)
            - ``levelname.txt``: 世界名称文本文件

        本方法为 **简化实现**: 解压 ZIP, 读取 ``level.dat`` 获取世界出生点
        和元数据信息。完整的区块方块数据需要解析 LevelDB 数据库 (较为复杂,
        涉及 Snappy 压缩和自定义块格式), 此处暂不实现。

        返回的 :class:`ParsedStructure` 的 ``offset`` 为世界出生点坐标,
        ``metadata`` 中标记 ``needs_world_coords=True`` 表示需要进一步
        指定世界坐标范围才能提取具体方块。

        Args:
            data: .mcworld 文件的字节串 (ZIP 压缩包)。

        Returns:
            解析后的 :class:`ParsedStructure` (含世界元数据, 方块数据为空)。

        Raises:
            CorruptFileError: 文件不是有效的 ZIP 或缺少 level.dat。
        """
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise CorruptFileError("mcworld 文件不是有效的 ZIP 压缩包")

        tmp_dir: Optional[str] = None
        try:
            # 使用 tempfile 解压 ZIP 到临时目录
            tmp_dir = tempfile.mkdtemp(prefix="mcworld_parse_")
            with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
                zf.extractall(tmp_dir)

            # 读取 level.dat (Bedrock 小端磁盘 NBT, 可能 gzip 压缩)
            level_dat_path = os.path.join(tmp_dir, "level.dat")
            if not os.path.isfile(level_dat_path):
                raise CorruptFileError("mcworld 缺少 level.dat 文件")

            with open(level_dat_path, "rb") as f:
                level_dat_bytes = f.read()

            # level.dat 可能使用 gzip 压缩
            level_payload = _maybe_gunzip(level_dat_bytes)
            level_nbt = unmarshal_disk(level_payload)
            if not isinstance(level_nbt, dict):
                raise CorruptFileError("mcworld level.dat 根标签不是复合标签")

            # 提取出生点和世界信息
            spawn_x = int(level_nbt.get("SpawnX", 0))
            spawn_y = int(level_nbt.get("SpawnY", 64))
            spawn_z = int(level_nbt.get("SpawnZ", 0))
            world_name = str(level_nbt.get("LevelName", ""))

            # 检查 db/ 目录是否存在 (LevelDB 区块数据)
            db_dir = os.path.join(tmp_dir, "db")
            has_db = os.path.isdir(db_dir)

            # 读取 levelname.txt (如果存在)
            levelname_txt = ""
            levelname_path = os.path.join(tmp_dir, "levelname.txt")
            if os.path.isfile(levelname_path):
                try:
                    with open(levelname_path, "r", encoding="utf-8",
                              errors="replace") as f:
                        levelname_txt = f.read().strip()
                except OSError:
                    pass

            metadata: dict[str, Any] = {
                "world_name": world_name or levelname_txt,
                "spawn": (spawn_x, spawn_y, spawn_z),
                "has_db": has_db,
                # 标记需要世界坐标范围才能提取方块 (LevelDB 完整解析较复杂)
                "needs_world_coords": True,
            }

            # 提取 level.dat 中的额外字段
            for key in ("StorageVersion", "GameType", "LastPlayed",
                        "Time", "Difficulty"):
                if key in level_nbt:
                    try:
                        metadata[key.lower()] = int(level_nbt[key])
                    except (ValueError, TypeError):
                        metadata[key.lower()] = level_nbt[key]

            # 简化实现: 返回空方块结构, 仅含世界元数据
            # 完整的区块方块提取需要解析 LevelDB (db/ 目录)
            return ParsedStructure(
                size=(0, 0, 0),
                blocks=[BlockState(name=AIR_BLOCK_NAME)],
                block_data=[],
                format=FORMAT_MCWORLD,
                offset=(spawn_x, spawn_y, spawn_z),
                metadata=metadata,
            )
        except zipfile.BadZipFile as exc:
            raise CorruptFileError("mcworld ZIP 解压失败", exc) from exc
        except NBTError as exc:
            raise CorruptFileError("mcworld level.dat NBT 解码失败", exc) from exc
        finally:
            # 清理临时目录
            if tmp_dir and os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # litematic 解析 (Litematica mod)
    # ------------------------------------------------------------------

    def parse_litematic(self, data: bytes) -> ParsedStructure:
        """解析 ``.litematic`` 文件 (Litematica mod 建筑文件)。

        litematic 使用 Java 大端 NBT + gzip 压缩, 结构如下::

            TAG_Compound {
                "MinecraftData": {
                    "Regions": {
                        "<区域名>": {
                            "size": {"x": Int, "y": Int, "z": Int},
                            "block_palette": [
                                {"Name": String, "Properties": Compound}, ...
                            ],
                            "block_states": LongArray,  // 位打包的调色板索引
                            "tile_entities": [...],
                            "entities": [...]
                        }
                    }
                },
                "DataVersion": Int,
                "Version": Int,
                "Metadata": {"Name": String, "Author": String, ...}
            }

        **位打包格式** (block_states):
            - ``bits_per_block = max(2, ceil(log2(palette_size)))``
            - 每个 64 位 long 存储 ``64 // bits_per_block`` 个方块
            - 方块索引顺序为 YZX (Y 最外层, X 最内层):
              ``index = (y * size_z + z) * size_x + x``
            - 从 long 数组提取索引时需处理跨 long 边界的情况
            - long 以有符号形式存储, 需转换为无符号处理

        一个 litematic 文件可包含多个区域, 本方法解析第一个区域。

        Args:
            data: .litematic 文件的字节串 (gzip 压缩)。

        Returns:
            解析后的 :class:`ParsedStructure`。

        Raises:
            CorruptFileError: 文件损坏或格式不合法。
        """
        payload = _maybe_gunzip(data)
        nbt = unmarshal_big_endian(payload)
        if not isinstance(nbt, dict):
            raise CorruptFileError("litematic 根标签不是复合标签")

        minecraft_data = nbt.get("MinecraftData")
        if not isinstance(minecraft_data, dict):
            raise CorruptFileError("litematic 缺少 MinecraftData 字段")

        regions = minecraft_data.get("Regions")
        if not isinstance(regions, dict) or not regions:
            raise CorruptFileError("litematic 缺少 Regions 字段或为空")

        # 使用第一个区域 (litematic 通常一个文件一个区域)
        region_name = next(iter(regions))
        region = regions[region_name]
        if not isinstance(region, dict):
            raise CorruptFileError("litematic 区域不是复合标签")

        # 尺寸 (支持 size 为含 x/y/z 的复合标签)
        size_nbt = region.get("size")
        if not isinstance(size_nbt, dict):
            raise CorruptFileError("litematic 缺少 size 字段")
        size_x = abs(int(size_nbt.get("x", 0)))
        size_y = abs(int(size_nbt.get("y", 0)))
        size_z = abs(int(size_nbt.get("z", 0)))
        if size_x <= 0 or size_y <= 0 or size_z <= 0:
            raise CorruptFileError(
                f"litematic 尺寸无效: {size_x}x{size_y}x{size_z}"
            )
        self._check_volume(size_x, size_y, size_z)

        # 方块调色板 (支持 snake_case 和 camelCase 两种字段名)
        palette_raw = region.get("block_palette")
        if palette_raw is None:
            palette_raw = region.get("blockPalette", [])
        if not isinstance(palette_raw, list):
            palette_raw = []

        blocks: list[BlockState] = []
        for entry in palette_raw:
            if not isinstance(entry, dict):
                blocks.append(BlockState(name=AIR_BLOCK_NAME))
                continue
            name = str(entry.get("Name", AIR_BLOCK_NAME))
            states = _states_from_nbt(entry.get("Properties"))
            blocks.append(BlockState(name=name, states=states))

        # 确保调色板至少有一个方块 (空气)
        if not blocks:
            blocks.append(BlockState(name=AIR_BLOCK_NAME))

        # 方块状态位打包数据 (LongArray)
        states_raw = region.get("block_states")
        if states_raw is None:
            states_raw = region.get("blockStates", [])

        # 将有符号 long 转为无符号 64 位整数列表
        if isinstance(states_raw, list):
            longs = [int(v) & 0xFFFFFFFFFFFFFFFF for v in states_raw]
        else:
            longs = []

        palette_size = len(blocks)
        bits_per_block = self._compute_bits_per_block(palette_size)

        # 构建 3D 方块索引数组 [x][y][z]
        # litematic 的索引顺序: index = (y * size_z + z) * size_x + x
        block_data: list[list[list[int]]] = [
            [[0 for _z in range(size_z)] for _y in range(size_y)]
            for _x in range(size_x)
        ]

        for y in range(size_y):
            for z in range(size_z):
                for x in range(size_x):
                    block_index = (y * size_z + z) * size_x + x
                    palette_idx = self._extract_packed_index(
                        longs, block_index, bits_per_block
                    )
                    if palette_idx < 0 or palette_idx >= palette_size:
                        palette_idx = 0  # 回退到第一个方块 (通常为空气)
                    block_data[x][y][z] = palette_idx

        # 方块实体 (tile_entities)
        tile_raw = region.get("tile_entities")
        if tile_raw is None:
            tile_raw = region.get("tileEntities", [])
        block_entities = self._extract_entities(tile_raw)

        # 实体
        entities_raw = region.get("entities", [])
        entities = self._extract_entities(entities_raw)

        # 元数据
        metadata: dict[str, Any] = {"region_name": str(region_name)}
        if "DataVersion" in nbt:
            metadata["data_version"] = int(nbt["DataVersion"])
        if "Version" in nbt:
            metadata["version"] = int(nbt["Version"])
        meta_nbt = nbt.get("Metadata")
        if isinstance(meta_nbt, dict):
            if "Name" in meta_nbt:
                metadata["name"] = str(meta_nbt["Name"])
            if "Author" in meta_nbt:
                metadata["author"] = str(meta_nbt["Author"])
            if "TimeCreated" in meta_nbt:
                try:
                    metadata["time_created"] = int(meta_nbt["TimeCreated"])
                except (ValueError, TypeError):
                    pass

        return ParsedStructure(
            size=(size_x, size_y, size_z),
            blocks=blocks,
            block_data=block_data,
            entities=entities,
            block_entities=block_entities,
            format=FORMAT_LITEMATIC,
            offset=(0, 0, 0),
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _decode_varint_indices(data: bytes, expected_count: int) -> list[int]:
        """从字节数组解码 Varuint32 编码的索引序列。

        Args:
            data: 包含 Varuint32 编码索引的字节串。
            expected_count: 期望的索引数量 (用于验证)。

        Returns:
            解码后的索引列表。

        Raises:
            CorruptFileError: 数据不完整或包含无效 Varint。
        """
        indices: list[int] = []
        offset = 0
        data_len = len(data)
        while offset < data_len:
            try:
                value, offset = decode_varuint32(data, offset)
            except ValueError as exc:
                raise CorruptFileError(
                    f"schem BlockData Varint 解码失败 (offset={offset})", exc
                ) from exc
            indices.append(value)
        if expected_count > 0 and len(indices) < expected_count:
            logger.warning(
                "schem BlockData 索引数量不足: %d < %d", len(indices), expected_count
            )
        return indices

    @staticmethod
    def _build_3d_from_flat_yzx(
        flat: list, size_x: int, size_y: int, size_z: int
    ) -> list[list[list[int]]]:
        """从 YZX 顺序的平铺数组构建 3D 数组 ``[x][y][z]``。

        Bedrock mcstructure 的 block_indices 是 YZX 顺序的平铺数组:
        ``flat_idx = x + z*size_x + y*size_x*size_z``。

        Args:
            flat: 平铺的索引列表。
            size_x: X 方向尺寸。
            size_y: Y 方向尺寸。
            size_z: Z 方向尺寸。

        Returns:
            3D 列表 ``block_data[x][y][z]``。
        """
        block_data: list[list[list[int]]] = [
            [[0 for _z in range(size_z)] for _y in range(size_y)]
            for _x in range(size_x)
        ]
        for y in range(size_y):
            for z in range(size_z):
                for x in range(size_x):
                    flat_idx = x + z * size_x + y * size_x * size_z
                    if flat_idx < len(flat):
                        block_data[x][y][z] = int(flat[flat_idx])
                    else:
                        block_data[x][y][z] = _AIR_INDEX
        return block_data

    @staticmethod
    def _extract_entities(raw: Any) -> list[dict]:
        """从 NBT 数据中提取实体列表。

        Args:
            raw: 原始 NBT 值 (期望为 list)。

        Returns:
            实体字典列表。非 dict 元素会被跳过。
        """
        if not isinstance(raw, list):
            return []
        result: list[dict] = []
        for entry in raw:
            if isinstance(entry, dict):
                result.append(entry)
        return result

    @staticmethod
    def _read_varuint_string(data: bytes, offset: int) -> tuple[str, int]:
        """从字节流读取 varuint32 长度前缀的 UTF8 字符串。

        用于 bdx 文件中的作者名、导出器名、方块名等字符串字段。
        格式: ``[varuint32 长度] [长度个 UTF8 字节]``。

        Args:
            data: 完整的字节串。
            offset: 起始偏移量。

        Returns:
            ``(string, new_offset)`` — 解码后的字符串和新偏移量。

        Raises:
            ValueError: varuint32 解码失败或数据不足。
        """
        length, offset = decode_varuint32(data, offset)
        end = offset + length
        if end > len(data):
            raise ValueError(
                f"varuint 字符串长度超出数据范围: offset={offset}, "
                f"length={length}, data_len={len(data)}"
            )
        raw = data[offset:end]
        return raw.decode("utf-8", errors="replace"), end

    @staticmethod
    def _read_network_nbt_payload(data: bytes, offset: int) -> tuple[Any, int]:
        """从网络 NBT 格式读取一个 NBT 值 (含根标签头)。

        用于 bdx 文件中的方块 states NBT 数据。网络 NBT 使用小端序,
        字符串/int32/int64 长度用 Varint 编码 (与 Bedrock 网络协议一致)。

        切片数据使 reader 从偏移 0 开始, 以避免网络 NBT 的 4MB 字节限制
        作用于整个 bdx 文件 (而非单个 NBT 值)。

        Args:
            data: 完整的字节串。
            offset: 起始偏移量。

        Returns:
            ``(value, new_offset)`` — 解码后的 NBT 值和新偏移量。
            若遇到 TAG_End, value 为 None。
        """
        reader = NBTReader(data[offset:], encoding=NETWORK_LITTLE_ENDIAN)
        tag_type = reader._read_byte_raw("ReadNetworkNBT")
        if tag_type == 0:  # TAG_END
            return None, offset + reader.offset
        reader.read_string()  # 读取并丢弃根标签名称
        value = reader.read_payload(tag_type)
        return value, offset + reader.offset

    @staticmethod
    def _compute_bits_per_block(palette_size: int) -> int:
        """计算 litematic 位打包所需的每方块位数。

        Litematica 使用 ``max(2, ceil(log2(palette_size)))`` 作为每方块位数,
        即最少 2 位 (即使调色板只有 1 个方块)。

        Args:
            palette_size: 调色板中的方块数量。

        Returns:
            每方块的位数 (最少 2 位)。
        """
        if palette_size <= 1:
            return 2  # Litematica 最少使用 2 位
        bits = 2
        while (1 << bits) < palette_size:
            bits += 1
        return bits

    @staticmethod
    def _extract_packed_index(
        longs: list[int], block_index: int, bits_per_block: int
    ) -> int:
        """从 litematic 位打包的 long 数组中提取调色板索引。

        Litematica 的 block_states 是一个 LongArray, 其中每个调色板索引
        使用 ``bits_per_block`` 位打包存储。方块索引 ``i`` 的位偏移为
        ``i * bits_per_block``, 可能跨越两个 long 的边界。

        long 以有符号形式存储, 本方法将其转换为无符号 64 位整数处理。

        Args:
            longs: 无符号 64 位整数列表 (调用前应已转换)。
            block_index: 方块在线性数组中的索引。
            bits_per_block: 每方块的位数。

        Returns:
            该方块的调色板索引。若数据不足则返回 0。
        """
        if bits_per_block <= 0 or not longs:
            return 0

        bit_offset = block_index * bits_per_block
        long_index = bit_offset // 64
        bit_in_long = bit_offset % 64

        if long_index >= len(longs):
            return 0

        # 当前 long 的值 (调用前应已转为无符号, 此处再保险一次)
        long_val = longs[long_index] & 0xFFFFFFFFFFFFFFFF
        long_val >>= bit_in_long

        # 若所需位跨越到下一个 long, 从下一个 long 补足高位
        bits_from_current = 64 - bit_in_long
        if bits_from_current < bits_per_block and long_index + 1 < len(longs):
            next_long = longs[long_index + 1] & 0xFFFFFFFFFFFFFFFF
            long_val |= next_long << bits_from_current

        mask = (1 << bits_per_block) - 1
        return int(long_val & mask)

    @classmethod
    def _check_volume(cls, size_x: int, size_y: int, size_z: int) -> None:
        """检查结构总体积是否超出上限。

        Args:
            size_x: X 方向尺寸。
            size_y: Y 方向尺寸。
            size_z: Z 方向尺寸。

        Raises:
            CorruptFileError: 体积超出 :attr:`MAX_BLOCK_VOLUME`。
        """
        volume = size_x * size_y * size_z
        if volume > cls.MAX_BLOCK_VOLUME:
            raise CorruptFileError(
                f"结构体积过大: {size_x}x{size_y}x{size_z} = {volume} "
                f"(上限 {cls.MAX_BLOCK_VOLUME})"
            )


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    # 格式常量
    "FORMAT_MCSTRUCTURE",
    "FORMAT_NBT",
    "FORMAT_SCHEMATIC",
    "FORMAT_SCHEM",
    "FORMAT_BDX",
    "FORMAT_MCWORLD",
    "FORMAT_LITEMATIC",
    "SUPPORTED_FORMATS",
    "AIR_BLOCK_NAME",
    # 异常
    "StructureParserError",
    "UnsupportedFormatError",
    "CorruptFileError",
    # 数据类
    "ParsedStructure",
    # 解析器
    "StructureParser",
]
