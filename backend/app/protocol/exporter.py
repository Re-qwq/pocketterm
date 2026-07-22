"""Minecraft 建筑导出器 — 将方块数据导出为 mcstructure / schematic / mcworld 格式。

本模块是 :mod:`app.protocol.structure_parser` 的逆操作: 将内存中的
方块数据 (:class:`~app.protocol.blocks.BlockState` 的 3D 数组) 序列化为
Minecraft 可识别的建筑结构文件。

支持的导出格式:
    ===================  ============================================  ===========  ========
    格式                  说明                                          NBT 字节序    压缩
    ===================  ============================================  ===========  ========
    .mcstructure         Minecraft Bedrock 原生结构文件                小端 (磁盘)   无
    .schematic           WorldEdit Schematic v1                       大端          gzip
    .mcworld             Bedrock 世界存档 (ZIP)                        小端 (磁盘)   ZIP
    ===================  ============================================  ===========  ========

关键设计点:
    - 方块数据统一为 3D 数组 ``blocks_data[x][y][z]``, 每个元素是
      :class:`~app.protocol.blocks.BlockState` (与解析器输出一致)。
    - mcstructure 使用 **小端序磁盘 NBT** (``marshal_disk``), 不压缩;
      ``block_indices`` 存储整数调色板索引 (-1 = 空气), 索引顺序为 YZX:
      ``idx = x + z*size_x + y*size_x*size_z``。
    - schematic 使用 **大端序 NBT** (``marshal_big_endian``) + gzip 压缩;
      ``Blocks``/``Data`` 是平铺字节数组, 索引顺序为 YZX:
      ``idx = x + z*width + y*width*length``。
    - mcworld 使用 ZIP 压缩包, 内含简化版 ``level.dat`` (小端磁盘 NBT)
      和 ``levelname.txt``; **不包含完整 LevelDB 区块数据** (标记为简化版)。
    - schematic 需要将 Bedrock 方块名映射回经典 Java 版方块 ID (1.12 及更早),
      未知的方块回退为 ``stone`` (ID=1)。

基本用法::

    from app.protocol.exporter import StructureExporter, ExportConfig
    from app.protocol.blocks import BlockState

    exporter = StructureExporter()

    # 构建一个 3x3x3 的石头方块数据
    size = (3, 3, 3)
    blocks_data = [
        [[BlockState(name="minecraft:stone") for _ in range(3)] for _ in range(3)]
        for _ in range(3)
    ]

    # 导出为 mcstructure
    data = await exporter.export_to_mcstructure(blocks_data, size)

    # 保存到文件
    await exporter.save_to_file(data, "house.mcstructure")

    # 导出为 schematic (自动映射 Bedrock -> Java 方块 ID)
    data = await exporter.export_to_schematic(blocks_data, size)

    # 导出为 mcworld (简化版世界存档)
    data = await exporter.export_to_mcworld(blocks_data, size)

从游戏内导出区域::

    from app.protocol.magic_command import MagicCommandSender

    sender = MagicCommandSender(client)
    data = await exporter.export_region(
        sender, x1=0, y1=64, z1=0, x2=10, y2=74, z2=10, fmt="mcstructure"
    )

逆向来源:
    - Minecraft Bedrock Edition 结构文件格式 (wiki.vg / bedrock.dev)
    - WorldEdit schematic 格式 (EngineHub/WorldEdit)
    - Bedrock level.dat 格式 (minecraft.wiki)
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import struct
import zipfile
import tempfile
import shutil
from dataclasses import dataclass, field
from typing import Any, Optional

from .nbt import (
    NBTWriter,
    NBTReader,
    marshal,
    marshal_big_endian,
    marshal_disk,
    unmarshal,
    unmarshal_big_endian,
    unmarshal_disk,
    TAG_COMPOUND,
    TAG_LIST,
    TAG_INT_ARRAY,
    TAG_BYTE_ARRAY,
    TAG_STRING,
    TAG_INT,
    TAG_BYTE,
    TAG_SHORT,
    TAG_LONG_ARRAY,
)
from .blocks import BlockState

logger = logging.getLogger("pocketterm.exporter")


# ======================================================================
# 常量
# ======================================================================

#: 支持的导出格式 — Bedrock 原生结构文件
FORMAT_MCSTRUCTURE: str = "mcstructure"

#: 支持的导出格式 — WorldEdit Schematic v1
FORMAT_SCHEMATIC: str = "schematic"

#: 支持的导出格式 — Bedrock 世界存档 (ZIP)
FORMAT_MCWORLD: str = "mcworld"

#: 所有支持的导出格式集合
SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {FORMAT_MCSTRUCTURE, FORMAT_SCHEMATIC, FORMAT_MCWORLD}
)

#: 空气方块的方块名 (Bedrock 命名空间)
AIR_BLOCK_NAME: str = "minecraft:air"

#: mcstructure 中 block_indices 的正层索引 (通常使用的层)
_BLOCK_LAYER_POSITIVE: int = 0

#: mcstructure 中表示空气的方块索引值 (-1)
_AIR_INDEX: int = -1

#: schematic 的默认材质标识 (Classic = Java 1.12 及更早的方块 ID 体系)
_SCHEMATIC_MATERIALS: str = "Alpha"

#: 未知 Bedrock 方块映射到 Java 时的回退方块 ID (stone)
_JAVA_FALLBACK_ID: int = 1

#: 未知方块的 Java data 值
_JAVA_FALLBACK_DATA: int = 0

#: schematic 的方块数据顺序为 YZX: idx = x + z*width + y*width*length
_SCHEMATIC_INDEX_DOC: str = "YZX (y*width*length + z*width + x)"

#: mcstructure 的方块数据顺序为 YZX: idx = x + z*size_x + y*size_x*size_z
_MCSTRUCTURE_INDEX_DOC: str = "YZX (y*size_x*size_z + z*size_x + x)"

#: mcworld 简化版标记 (写入 level.dat 元数据)
_MCWORLD_SIMPLIFIED_TAG: str = "PocketTerm-Simplified"


# ======================================================================
# Bedrock -> Java 方块 ID 反向映射
# ======================================================================

def _build_bedrock_to_java_map() -> tuple[
    dict[str, int],
    dict[tuple[str, str, Any], tuple[int, int]],
]:
    """构建 Bedrock 方块名 -> Java 方块 ID 的反向映射表。

    从 :mod:`app.protocol.structure_parser` 导入正向映射
    (Java ID -> Bedrock name) 后反转。由于正向映射不是单射
    (同一 Bedrock name 可能对应多个 Java ID), 反向映射只保留
    **第一次出现** 的 ID (通常是 data=0 的基础方块)。

    Returns:
        ``(name_to_id, states_to_id)`` 元组:
        - ``name_to_id``: Bedrock 方块名 -> Java 方块 ID (无状态时使用)
        - ``states_to_id``: ``(name, state_key, state_value)`` ->
          ``(java_id, java_data)`` (有状态时使用)
    """
    # 延迟导入, 避免循环依赖
    from .structure_parser import (
        _JAVA_BLOCK_ID_MAP,
        _JAVA_BLOCK_DATA_MAP,
        _BEDROCK_COLORS,
        _COLOR_BLOCK_IDS,
    )

    name_to_id: dict[str, int] = {}
    # 反转 _JAVA_BLOCK_ID_MAP: bedrock_name -> java_id (保留首次出现)
    for java_id, bedrock_name in _JAVA_BLOCK_ID_MAP.items():
        if bedrock_name not in name_to_id:
            name_to_id[bedrock_name] = java_id

    # 反转 _JAVA_BLOCK_DATA_MAP: (name, state_key, state_value) -> (java_id, java_data)
    states_to_id: dict[tuple[str, str, Any], tuple[int, int]] = {}
    for (java_id, java_data), (bedrock_name, states) in _JAVA_BLOCK_DATA_MAP.items():
        for state_key, state_value in states.items():
            key = (bedrock_name, state_key, str(state_value))
            if key not in states_to_id:
                states_to_id[key] = (java_id, java_data)

    # 颜色方块: minecraft:wool + color="white" -> (35, 0), 等
    for block_id in _COLOR_BLOCK_IDS:
        bedrock_name = _JAVA_BLOCK_ID_MAP.get(block_id, "minecraft:wool")
        for data_val, color in enumerate(_BEDROCK_COLORS):
            key = (bedrock_name, "color", color)
            if key not in states_to_id:
                states_to_id[key] = (block_id, data_val)

    return name_to_id, states_to_id


# 模块级缓存: 反向映射表 (惰性构建)
_BEDROCK_NAME_TO_JAVA: Optional[dict[str, int]] = None
_BEDROCK_STATES_TO_JAVA: Optional[dict[tuple[str, str, Any], tuple[int, int]]] = None


def _get_reverse_maps() -> tuple[
    dict[str, int],
    dict[tuple[str, str, Any], tuple[int, int]],
]:
    """获取 (或惰性构建) Bedrock -> Java 反向映射表。

    Returns:
        ``(name_to_id, states_to_id)`` 元组。
    """
    global _BEDROCK_NAME_TO_JAVA, _BEDROCK_STATES_TO_JAVA
    if _BEDROCK_NAME_TO_JAVA is None or _BEDROCK_STATES_TO_JAVA is None:
        _BEDROCK_NAME_TO_JAVA, _BEDROCK_STATES_TO_JAVA = _build_bedrock_to_java_map()
    return _BEDROCK_NAME_TO_JAVA, _BEDROCK_STATES_TO_JAVA


# ======================================================================
# 数据类: 导出配置与结果
# ======================================================================


@dataclass
class ExportConfig:
    """导出配置。

    控制导出行为的各项参数, 可在调用导出方法时传入以覆盖默认值。

    Attributes:
        format: 目标格式名称 (``"mcstructure"``/``"schematic"``
            /``"mcworld"``), 默认 ``"mcstructure"``。
        include_entities: 是否包含实体数据, 默认 ``True``。
        include_block_entities: 是否包含方块实体数据 (如箱子、熔炉的 NBT),
            默认 ``True``。
        include_biomes: 是否包含生物群系数据 (仅 schematic 有效),
            默认 ``False``。
        split_size: 分割尺寸。``0`` 表示不分割, ``>0`` 表示按此尺寸
            将大区域分割为多个子结构 (未实现的预留字段)。
    """

    format: str = "mcstructure"
    include_entities: bool = True
    include_block_entities: bool = True
    include_biomes: bool = False
    split_size: int = 0  # 0=不分割, >0=按此尺寸分割


@dataclass
class ExportResult:
    """导出结果。

    封装一次导出操作的结果数据及元信息。

    Attributes:
        data: 导出的字节数据 (已编码为目标格式)。
        format: 实际导出的格式名称。
        size: 结构尺寸 ``(width, height, length)``。
        block_count: 非空气方块数量。
        warnings: 导出过程中产生的警告信息列表 (如未知方块的回退提示)。
    """

    data: bytes
    format: str
    size: tuple[int, int, int]
    block_count: int
    warnings: list[str] = field(default_factory=list)


# ======================================================================
# 建筑导出器
# ======================================================================


class StructureExporter:
    """建筑导出器 — 将方块数据序列化为 Minecraft 结构文件。

    本类是 :class:`~app.protocol.structure_parser.StructureParser` 的逆操作,
    将内存中的 3D 方块数组 (``blocks_data[x][y][z]``) 编码为目标格式字节串。

    支持的格式:
        - **mcstructure**: Bedrock 原生结构文件 (小端磁盘 NBT, 无压缩)。
        - **schematic**: WorldEdit Schematic v1 (大端 NBT + gzip, Java 方块 ID)。
        - **mcworld**: Bedrock 世界存档 ZIP (简化版, 仅含 level.dat)。

    方块数据约定:
        - ``blocks_data`` 是 3D 嵌套列表: ``blocks_data[x][y][z]``。
        - 每个元素是 :class:`~app.protocol.blocks.BlockState` (name + states)。
        - ``size`` 是 ``(width, height, length)`` 即 ``(x, y, z)``。
        - ``offset`` 是结构原点在世界中的坐标 ``(x, y, z)``。

    Example::

        exporter = StructureExporter()

        # 导出为 mcstructure
        data = await exporter.export_to_mcstructure(blocks_data, (3, 3, 3))

        # 导出为 schematic
        data = await exporter.export_to_schematic(blocks_data, (3, 3, 3))

        # 保存到文件
        await exporter.save_to_file(data, "output.mcstructure")
    """

    def __init__(self) -> None:
        """初始化建筑导出器。"""
        # Bedrock -> Java 反向映射表 (惰性构建)
        self._name_to_java, self._states_to_java = _get_reverse_maps()
        # 方块状态去重缓存: (name, states_tuple) -> palette_index
        self._palette_cache: dict[tuple[str, tuple], int] = {}

    # ------------------------------------------------------------------
    # mcstructure 导出
    # ------------------------------------------------------------------

    async def export_to_mcstructure(
        self,
        blocks_data: list[list[list[BlockState]]],
        size: tuple[int, int, int],
        offset: tuple[int, int, int] = (0, 0, 0),
        block_entities: Optional[list[dict]] = None,
        entities: Optional[list[dict]] = None,
    ) -> bytes:
        """导出为 mcstructure 格式 (Bedrock 原生结构文件)。

        使用小端序磁盘 NBT (:func:`~app.protocol.nbt.marshal_disk`) 编码,
        不压缩。生成的文件可被 Bedrock 版 Minecraft 的结构方块加载,
        也可被 :class:`~app.protocol.structure_parser.StructureParser` 解析。

        NBT 结构::

            TAG_Compound {
                "format_version": Int(1),
                "size": [Int(x), Int(y), Int(z)],
                "structure": {
                    "block_indices": [[Int...], [Int...]],
                    "entities": [...],
                    "palette": {"default": {"block_palette": [...],
                                             "block_position_data": {...}}}
                },
                "structure_world_origin": [Int(x), Int(y), Int(z)]
            }

        其中:
            - ``block_indices[0]`` (正层) 是方块索引数组, 值为 -1 表示空气,
              其他值为 ``block_palette`` 列表的索引。索引顺序为 YZX:
              ``idx = x + z*size_x + y*size_x*size_z``。
            - ``block_indices[1]`` (负层) 通常为 -1 (空气)。
            - ``block_palette`` 是去重的方块状态列表, 每项含 ``name`` 和 ``states``。

        Args:
            blocks_data: 3D 方块数组 ``blocks_data[x][y][z]``。
            size: 结构尺寸 ``(width, height, length)``。
            offset: 结构原点在世界中的坐标, 默认 ``(0, 0, 0)``。
            block_entities: 方块实体列表 (如箱子、熔炉的 NBT 数据),
                每项为 dict。默认 ``None`` (不含方块实体)。
            entities: 实体列表, 每项为 dict。默认 ``None`` (不含实体)。

        Returns:
            mcstructure 格式的字节串 (未压缩的小端磁盘 NBT)。

        Raises:
            ValueError: 尺寸与 blocks_data 不匹配, 或尺寸为 0。
        """
        width, height, length = size
        if width <= 0 or height <= 0 or length <= 0:
            raise ValueError(f"mcstructure 尺寸必须为正: {size}")

        self._validate_blocks_data(blocks_data, size)

        # 构建调色板 (去重的 BlockState 列表)
        palette, index_map = self._build_palette_with_map(blocks_data)

        # 构建 block_indices 正层 (整数索引数组, YZX 顺序)
        # idx = x + z*size_x + y*size_x*size_z
        positive_layer: list[int] = []
        for y in range(height):
            for z in range(length):
                for x in range(width):
                    block = blocks_data[x][y][z]
                    if block.name == AIR_BLOCK_NAME:
                        positive_layer.append(_AIR_INDEX)
                    else:
                        idx = index_map[self._block_key(block)]
                        positive_layer.append(idx)

        # 负层 (通常是空气, 全部 -1)
        negative_layer: list[int] = [_AIR_INDEX] * (width * height * length)

        # 构建 block_palette (Compound 列表)
        block_palette: list[dict[str, Any]] = []
        for block in palette:
            states_nbt: dict[str, Any] = {}
            for state_key, state_value in block.states.items():
                states_nbt[state_key] = self._encode_state_value(state_value)
            block_palette.append({
                "name": block.name,
                "states": states_nbt,
            })

        # 构建 structure 复合标签
        structure: dict[str, Any] = {
            "block_indices": [positive_layer, negative_layer],
            "entities": entities or [],
            "palette": {
                "default": {
                    "block_palette": block_palette,
                },
            },
        }

        # 添加方块实体到 block_position_data (如果有)
        if block_entities:
            block_position_data: dict[str, Any] = {}
            for i, be in enumerate(block_entities):
                # 使用方块在正层中的索引作为 key
                key = str(i)
                block_position_data[key] = {"block_entity": be}
            structure["palette"]["default"]["block_position_data"] = block_position_data

        # 构建根复合标签
        root: dict[str, Any] = {
            "format_version": 1,
            "size": [width, height, length],
            "structure": structure,
            "structure_world_origin": [offset[0], offset[1], offset[2]],
        }

        # 编码为小端磁盘 NBT (无压缩)
        data = marshal_disk(root)
        logger.info(
            "mcstructure 导出完成: size=%s, palette=%d, blocks=%d",
            size, len(palette), len(positive_layer),
        )
        return data

    # ------------------------------------------------------------------
    # schematic 导出
    # ------------------------------------------------------------------

    async def export_to_schematic(
        self,
        blocks_data: list[list[list[BlockState]]],
        size: tuple[int, int, int],
        offset: tuple[int, int, int] = (0, 0, 0),
        block_entities: Optional[list[dict]] = None,
    ) -> bytes:
        """导出为 schematic 格式 (WorldEdit Schematic v1)。

        使用大端序 NBT (:func:`~app.protocol.nbt.marshal_big_endian`) 编码,
        然后 gzip 压缩。生成的文件可被 WorldEdit 加载。

        NBT 结构::

            TAG_Compound {
                "Materials": String("Alpha"),
                "Width": Short(width),
                "Height": Short(height),
                "Length": Short(length),
                "Blocks": ByteArray([java_id, ...]),
                "Data": ByteArray([java_data, ...]),
                "Entities": List[],
                "TileEntities": List[],
                "Biomes": ByteArray([...])  # 可选
            }

        方块数据顺序为 YZX: ``idx = x + z*width + y*width*length``。
        需要将 Bedrock 方块名映射回经典 Java 版方块 ID (1.12 及更早),
        未知的方块回退为 ``stone`` (ID=1)。

        Args:
            blocks_data: 3D 方块数组 ``blocks_data[x][y][z]``。
            size: 结构尺寸 ``(width, height, length)``。
            offset: 结构原点偏移 (写入 WEOriginX/Y/Z, 默认 ``(0, 0, 0)``)。
            block_entities: 方块实体列表 (TileEntities), 每项为 dict。

        Returns:
            schematic 格式的字节串 (gzip 压缩的大端 NBT)。

        Raises:
            ValueError: 尺寸与 blocks_data 不匹配, 或尺寸为 0。
        """
        width, height, length = size
        if width <= 0 or height <= 0 or length <= 0:
            raise ValueError(f"schematic 尺寸必须为正: {size}")

        self._validate_blocks_data(blocks_data, size)

        # 构建平铺的方块 ID 和 data 字节数组 (YZX 顺序)
        # idx = x + z*width + y*width*length
        total = width * height * length
        blocks_bytes = bytearray(total)
        data_bytes = bytearray(total)
        unknown_blocks: set[str] = set()

        for y in range(height):
            for z in range(length):
                for x in range(width):
                    block = blocks_data[x][y][z]
                    flat_idx = x + z * width + y * width * length
                    java_id, java_data = self._block_to_java_id(block)
                    if java_id == _JAVA_FALLBACK_ID and block.name != AIR_BLOCK_NAME \
                            and block.name != "minecraft:stone":
                        unknown_blocks.add(block.name)
                    # 空气
                    if block.name == AIR_BLOCK_NAME:
                        java_id, java_data = 0, 0
                    # 限制在字节范围内 (有符号 8 位: -128~127, 但方块 ID 为 0~255)
                    blocks_bytes[flat_idx] = java_id & 0xFF
                    data_bytes[flat_idx] = java_data & 0xFF

        # 构建根复合标签
        root: dict[str, Any] = {
            "Materials": _SCHEMATIC_MATERIALS,
            "Width": width,
            "Height": height,
            "Length": length,
            "Blocks": bytes(blocks_bytes),
            "Data": bytes(data_bytes),
            "Entities": [],
            "TileEntities": block_entities or [],
            # WorldEdit 原点
            "WEOriginX": offset[0],
            "WEOriginY": offset[1],
            "WEOriginZ": offset[2],
        }

        # 生物群系数据 (可选, 全 0 = 默认平原)
        biome_bytes = bytes(total)
        root["Biomes"] = biome_bytes

        # 编码为大端 NBT
        nbt_data = marshal_big_endian(root)

        # gzip 压缩
        compressed = gzip.compress(nbt_data)

        if unknown_blocks:
            logger.warning(
                "schematic 导出: %d 种未知 Bedrock 方块回退为 stone: %s",
                len(unknown_blocks), ", ".join(sorted(unknown_blocks)[:10]),
            )

        logger.info(
            "schematic 导出完成: size=%s, bytes=%d, compressed=%d",
            size, len(nbt_data), len(compressed),
        )
        return compressed

    # ------------------------------------------------------------------
    # mcworld 导出 (简化版)
    # ------------------------------------------------------------------

    async def export_to_mcworld(
        self,
        blocks_data: list[list[list[BlockState]]],
        size: tuple[int, int, int],
        offset: tuple[int, int, int] = (0, 0, 0),
    ) -> bytes:
        """导出为 mcworld 格式 (Bedrock 世界存档 ZIP, 简化版)。

        mcworld 文件本质是 ZIP 压缩包, 包含:
            - ``level.dat``: 世界元数据 (Bedrock 小端磁盘 NBT, 含出生点等)
            - ``levelname.txt``: 世界名称文本文件

        **注意**: 这是一个 **简化实现**, **不包含完整的 LevelDB 区块数据**
        (区块方块数据需要 LevelDB + Snappy 压缩, 实现复杂)。生成的 mcworld
        可被 Minecraft 识别为世界存档, 但打开后为空世界 (仅含 level.dat 元数据)。
        metadata 中标记 ``exporter=_MCWORLD_SIMPLIFIED_TAG`` 以便区分。

        Args:
            blocks_data: 3D 方块数组 (当前简化版不写入区块数据, 仅用于
                计算尺寸和方块统计)。
            size: 结构尺寸 ``(width, height, length)``。
            offset: 结构原点偏移 (用作世界出生点参考, 默认 ``(0, 0, 0)``)。

        Returns:
            mcworld 格式的字节串 (ZIP 压缩包)。

        Raises:
            ValueError: 尺寸为 0。
        """
        width, height, length = size
        if width <= 0 or height <= 0 or length <= 0:
            raise ValueError(f"mcworld 尺寸必须为正: {size}")

        # 构建 level.dat (Bedrock 小端磁盘 NBT)
        # 出生点取 offset + size 中心
        spawn_x = offset[0] + width // 2
        spawn_y = offset[1] + height
        spawn_z = offset[2] + length // 2

        level_dat: dict[str, Any] = {
            # 存储版本 (Bedrock level.dat 版本号, 9 = 当前主流)
            "StorageVersion": 9,
            # 游戏模式 (0=生存, 1=创造, 2=冒险, 3=旁观)
            "GameType": 1,
            # 出生点坐标
            "SpawnX": spawn_x,
            "SpawnY": spawn_y,
            "SpawnZ": spawn_z,
            # 世界生成选项 (扁平世界)
            "FlatWorldLayers": "minecraft:bedrock,2*minecraft:dirt,minecraft:grass",
            "Generator": 2,  # 2 = flat world
            # 世界边界
            "WorldStartCount": 0,
            # 最后游戏时间 (毫秒时间戳, 此处用 0)
            "LastPlayed": 0,
            # 简化版标记
            "exporter": _MCWORLD_SIMPLIFIED_TAG,
            # 方块统计 (元数据, 非标准字段)
            "structure_size": [width, height, length],
        }

        # 编码 level.dat (小端磁盘 NBT)
        level_dat_bytes = marshal_disk(level_dat)

        # levelname.txt 内容
        levelname_txt = f"PocketTerm Exported World ({width}x{height}x{length})"

        # 构建 ZIP 压缩包
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("level.dat", level_dat_bytes)
            zf.writestr("levelname.txt", levelname_txt)
            # 写入一个空的 db/ 目录占位 (LevelDB 目录, 实际无数据)
            # ZIP 规范中目录条目以 / 结尾
            zf.writestr("db/.placeholder", b"")

        data = buf.getvalue()
        logger.info(
            "mcworld 导出完成 (简化版): size=%s, spawn=(%d, %d, %d), zip_size=%d",
            size, spawn_x, spawn_y, spawn_z, len(data),
        )
        return data

    # ------------------------------------------------------------------
    # 从游戏内导出区域
    # ------------------------------------------------------------------

    async def export_region(
        self,
        command_sender: Any,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
        fmt: str = "mcstructure",
    ) -> bytes:
        """从游戏中导出指定区域的方块数据。

        通过 :class:`~app.protocol.magic_command.MagicCommandSender` 向游戏
        发送命令, 逐个读取区域内的方块状态, 然后转换为目标格式。

        流程:
            1. 规范化坐标 (确保 x1<=x2, y1<=y2, z1<=z2)。
            2. 逐个调用 ``getblock`` 命令读取每个方块状态。
            3. 构建 3D 方块数组。
            4. 调用对应的导出方法 (mcstructure/schematic/mcworld) 转换。

        注意:
            - 此方法会发送大量 ``getblock`` 命令, 大区域导出会很慢。
              建议区域不超过 32x32x32 (约 32768 次命令)。
            - ``command_sender`` 应是 :class:`MagicCommandSender` 实例,
              其 ``send_any_command`` 会自动路由命令 (getblock 走魔法指令)。

        Args:
            command_sender: :class:`MagicCommandSender` 实例, 用于发送命令。
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。
            fmt: 目标格式 (``"mcstructure"``/``"schematic"``/``"mcworld"``)。

        Returns:
            目标格式的字节串。

        Raises:
            ValueError: 不支持的格式, 或区域尺寸为 0。
        """
        if fmt not in SUPPORTED_FORMATS:
            raise ValueError(
                f"不支持的导出格式: {fmt!r}, 支持: {sorted(SUPPORTED_FORMATS)}"
            )

        # 规范化坐标 (确保 x1<=x2 等)
        min_x, max_x = min(x1, x2), max(x1, x2)
        min_y, max_y = min(y1, y2), max(y1, y2)
        min_z, max_z = min(z1, z2), max(z1, z2)

        width = max_x - min_x + 1
        height = max_y - min_y + 1
        length = max_z - min_z + 1

        if width <= 0 or height <= 0 or length <= 0:
            raise ValueError(
                f"区域尺寸无效: ({min_x},{min_y},{min_z}) -> "
                f"({max_x},{max_y},{max_z})"
            )

        total = width * height * length
        logger.info(
            "开始从游戏导出区域: (%d,%d,%d)-(%d,%d,%d), size=(%d,%d,%d), "
            "total=%d, fmt=%s",
            min_x, min_y, min_z, max_x, max_y, max_z,
            width, height, length, total, fmt,
        )

        # 构建 3D 方块数组 (初始全部为空气)
        blocks_data: list[list[list[BlockState]]] = [
            [[BlockState(name=AIR_BLOCK_NAME) for _ in range(length)]
             for _ in range(height)]
            for _ in range(width)
        ]

        # 逐个读取方块 (使用 getblock 命令)
        # 注意: send_any_command 返回响应文本, 需要解析方块状态
        read_count = 0
        for x in range(width):
            for y in range(height):
                for z in range(length):
                    world_x = min_x + x
                    world_y = min_y + y
                    world_z = min_z + z
                    cmd = f"getblock {world_x} {world_y} {world_z}"
                    try:
                        resp = await command_sender.send_any_command(cmd)
                    except Exception as exc:
                        logger.warning(
                            "getblock 失败 (%d,%d,%d): %s",
                            world_x, world_y, world_z, exc,
                        )
                        continue

                    block = self._parse_getblock_response(resp)
                    if block is not None:
                        blocks_data[x][y][z] = block
                    read_count += 1

                    # 每 1000 个方块记录一次进度
                    if read_count % 1000 == 0:
                        logger.info(
                            "方块读取进度: %d/%d (%.1f%%)",
                            read_count, total, read_count * 100 / total,
                        )

        logger.info("方块读取完成: %d/%d", read_count, total)

        # 调用对应的导出方法
        size = (width, height, length)
        offset = (min_x, min_y, min_z)

        if fmt == FORMAT_MCSTRUCTURE:
            return await self.export_to_mcstructure(
                blocks_data, size, offset=offset
            )
        elif fmt == FORMAT_SCHEMATIC:
            return await self.export_to_schematic(
                blocks_data, size, offset=offset
            )
        elif fmt == FORMAT_MCWORLD:
            return await self.export_to_mcworld(
                blocks_data, size, offset=offset
            )
        else:
            # 不应该到达这里 (前面已校验)
            raise ValueError(f"不支持的导出格式: {fmt!r}")

    # ------------------------------------------------------------------
    # 文件保存
    # ------------------------------------------------------------------

    async def save_to_file(self, data: bytes, file_path: str) -> None:
        """将导出的字节数据保存到文件。

        创建必要的父目录, 然后写入字节数据。

        Args:
            data: 要保存的字节串 (来自导出方法)。
            file_path: 目标文件路径 (绝对或相对路径)。

        Raises:
            OSError: 文件写入失败。
        """
        # 创建父目录 (如果不存在)
        parent_dir = os.path.dirname(file_path)
        if parent_dir and not os.path.isdir(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)

        with open(file_path, "wb") as f:
            f.write(data)

        logger.info("已保存导出文件: %s (%d 字节)", file_path, len(data))

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _build_palette(
        self,
        blocks_data: list[list[list[BlockState]]],
    ) -> list[BlockState]:
        """构建方块调色板 (去重的方块状态列表)。

        遍历 3D 方块数组, 将所有 ``BlockState`` 按 (name, states) 去重,
        返回去重后的列表。空气方块不加入调色板。

        Args:
            blocks_data: 3D 方块数组。

        Returns:
            去重后的 :class:`BlockState` 列表 (不含空气)。
        """
        palette: list[BlockState] = []
        seen: set[tuple[str, tuple]] = set()

        for x_plane in blocks_data:
            for y_row in x_plane:
                for block in y_row:
                    if block.name == AIR_BLOCK_NAME:
                        continue
                    key = self._block_key(block)
                    if key not in seen:
                        seen.add(key)
                        palette.append(block)

        return palette

    def _build_palette_with_map(
        self,
        blocks_data: list[list[list[BlockState]]],
    ) -> tuple[list[BlockState], dict[tuple[str, tuple], int]]:
        """构建方块调色板及 (block_key -> palette_index) 映射。

        Args:
            blocks_data: 3D 方块数组。

        Returns:
            ``(palette, index_map)`` 元组:
            - ``palette``: 去重后的 BlockState 列表。
            - ``index_map``: (name, states_tuple) -> palette 索引。
        """
        palette: list[BlockState] = []
        index_map: dict[tuple[str, tuple], int] = {}

        for x_plane in blocks_data:
            for y_row in x_plane:
                for block in y_row:
                    if block.name == AIR_BLOCK_NAME:
                        continue
                    key = self._block_key(block)
                    if key not in index_map:
                        index_map[key] = len(palette)
                        palette.append(block)

        return palette, index_map

    def _block_key(self, block: BlockState) -> tuple[str, tuple]:
        """生成方块状态的哈希键 (name + 排序后的 states)。

        Args:
            block: :class:`BlockState` 对象。

        Returns:
            ``(name, sorted_states_tuple)`` 元组, 可哈希。
        """
        # 对 states 排序以确保相同状态的方块产生相同键
        sorted_states = tuple(sorted(block.states.items()))
        return (block.name, sorted_states)

    def _block_to_java_id(self, block: BlockState) -> tuple[int, int]:
        """将 Bedrock 方块状态转换为 Java 方块 ID 和 data 值。

        使用从 :mod:`structure_parser` 构建的反向映射表:
            1. 优先匹配 (name + states) 完全对应的 Java ID + data。
            2. 其次匹配仅方块名 (无状态) 的 Java ID。
            3. 空气返回 (0, 0)。
            4. 未知方块回退为 stone (ID=1, data=0)。

        Args:
            block: :class:`BlockState` 对象。

        Returns:
            ``(java_id, java_data)`` 元组。
        """
        # 空气
        if block.name == AIR_BLOCK_NAME:
            return (0, 0)

        # 尝试匹配带状态的方块
        for state_key, state_value in block.states.items():
            key = (block.name, state_key, str(state_value))
            if key in self._states_to_java:
                return self._states_to_java[key]

        # 尝试匹配仅方块名
        if block.name in self._name_to_java:
            return (self._name_to_java[block.name], _JAVA_FALLBACK_DATA)

        # 未知方块, 回退为 stone
        logger.debug("未知 Bedrock 方块, 回退为 stone: %s", block)
        return (_JAVA_FALLBACK_ID, _JAVA_FALLBACK_DATA)

    @staticmethod
    def _encode_state_value(value: Any) -> Any:
        """将 Python 值编码为 NBT 兼容的值。

        Bedrock 方块状态的值可能是字符串或整数。整数需要包装为
        :class:`~app.protocol.nbt.Int` (或 :class:`Byte`) 以确保 NBT 编码
        时使用正确的 Tag 类型。字符串直接返回。

        Args:
            value: 方块状态值 (str/int/bool)。

        Returns:
            NBT 兼容的值 (str/Int/Byte)。
        """
        # 延迟导入以避免循环依赖
        from .nbt import Int, Byte

        if isinstance(value, bool):
            return Byte(int(value))
        if isinstance(value, int):
            # 小整数用 Byte, 大整数用 Int (Bedrock states 通常是 Byte 或 Int)
            if -128 <= value <= 127:
                return Byte(value)
            return Int(value)
        return str(value)

    @staticmethod
    def _validate_blocks_data(
        blocks_data: list[list[list[BlockState]]],
        size: tuple[int, int, int],
    ) -> None:
        """校验 blocks_data 的维度与 size 是否匹配。

        Args:
            blocks_data: 3D 方块数组。
            size: 预期尺寸 ``(width, height, length)``。

        Raises:
            ValueError: 维度不匹配。
        """
        width, height, length = size
        if len(blocks_data) != width:
            raise ValueError(
                f"blocks_data 第一维 (x) 长度 {len(blocks_data)} != width {width}"
            )
        if width > 0:
            if len(blocks_data[0]) != height:
                raise ValueError(
                    f"blocks_data 第二维 (y) 长度 {len(blocks_data[0])} "
                    f"!= height {height}"
                )
            if height > 0 and len(blocks_data[0][0]) != length:
                raise ValueError(
                    f"blocks_data 第三维 (z) 长度 {len(blocks_data[0][0])} "
                    f"!= length {length}"
                )

    @staticmethod
    def _parse_getblock_response(response: Optional[str]) -> Optional[BlockState]:
        """解析 ``getblock`` 命令的响应文本为 :class:`BlockState`。

        Bedrock ``getblock`` 命令的响应格式可能为:
            1. NBT 复合标签: ``{"name":"minecraft:stone","states":{...}}``
            2. 带引号方块名: ``"minecraft:stone"``
            3. 纯方块名: ``minecraft:stone``
            4. 带状态的方块名: ``minecraft:stone[stone_type=granite]``

        Args:
            response: 命令响应文本 (可能为 ``None``)。

        Returns:
            解析后的 :class:`BlockState`, 失败返回 ``None``。
        """
        if not response:
            return None

        text = response.strip()
        if not text:
            return None

        try:
            # 尝试用 BlockState.from_snbt 解析 (支持 NBT 复合和纯方块名)
            return BlockState.from_snbt(text)
        except (ValueError, Exception) as exc:
            logger.debug("解析 getblock 响应失败: %r -> %s", text, exc)
            return None


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    "StructureExporter",
    "ExportConfig",
    "ExportResult",
    "FORMAT_MCSTRUCTURE",
    "FORMAT_SCHEMATIC",
    "FORMAT_MCWORLD",
    "SUPPORTED_FORMATS",
]
