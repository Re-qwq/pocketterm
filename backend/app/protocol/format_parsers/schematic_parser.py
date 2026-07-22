"""schematic_parser - WorldEdit Schematic 格式解析器。

逆向自 NexusEgo v1.6.5 的 Schematic 解析层:

    - WaterStructure/structure/ 中的 schematic 支持
    - strings 中的 "Schematic" / "Litematic" / "GangBanV1" 标记

Schematic 格式:
    - WorldEdit 经典格式 (.schematic / .schem)
    - 使用大端序 NBT + GZIP 压缩
    - 顶层 TAG_Compound 包含:
        * Width, Height, Length (Short)
        * Materials (String, "Alpha" 或 "Classic")
        * Blocks (ByteArray)
        * Data (ByteArray)
        * TileEntities (List)
        * Entities (List)
    - Litematica 格式 (.litematic) 是 Java 版 Litematica mod 的格式,
      使用大端序 NBT + GZIP 压缩。
"""

from __future__ import annotations

import gzip
import logging
from dataclasses import dataclass, field
from typing import Any

from .nbt_parser import (
    NBTError, nbt_unmarshal_big_endian, LITTLE_ENDIAN,
    nbt_unmarshal, Byte, Short, Int, Long, ByteArray,
)

logger = logging.getLogger("pocketterm.protocol.format_parsers.schematic_parser")


# -------------------------------------------------------------------- #
# 常量 (合并自 NovaBuilder schematic_parser)
# -------------------------------------------------------------------- #

#: 废弃警告 (逆向自 NovaBuilder strings: "schem' is deprecated")
SCHEMATIC_DEPRECATED_WARNING: str = (
    "WARNING - `schem' is deprecated and has been removed, "
    "please migrate to BDX format instead."
)

#: 旧版 BlockId 列表上限 (逆向自 NovaBuilder)
SCHEMATIC_MAX_BLOCKS: int = 16384

#: Materials 取值 (逆向自 WorldEdit 标准)
MATERIALS_ALPHA: str = "Alpha"
MATERIALS_POCKET: str = "Pocket"


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class SchematicError(Exception):
    """Schematic 文件解析错误基类。"""


class SchematicFormatError(SchematicError):
    """Schematic 格式错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class SchematicBlock:
    """Schematic 方块条目。"""
    position: tuple[int, int, int]
    block_id: int = 0
    block_data: int = 0
    tile_entity: dict[str, Any] | None = None


@dataclass
class SchematicResult:
    """Schematic 文件解析结果。"""
    width: int = 0
    height: int = 0
    length: int = 0
    materials: str = "Alpha"
    blocks: list[SchematicBlock] = field(default_factory=list)
    tile_entities: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    we_offset: tuple[int, int, int] | None = None  # WorldEdit 偏移 (WEOriginX/Y/Z)
    raw_nbt: dict[str, Any] = field(default_factory=dict)

    @property
    def total_blocks(self) -> int:
        """方块总数。"""
        return len(self.blocks)

    @property
    def non_air_blocks(self) -> int:
        """非空气方块数。"""
        return sum(1 for b in self.blocks if b.block_id != 0)


# -------------------------------------------------------------------- #
# 解析主流程
# -------------------------------------------------------------------- #


def parse_schematic_bytes(data: bytes) -> SchematicResult:
    """解析 Schematic 文件的字节数据。

    逆向自 WaterStructure/structure/ 中的 schematic 解析入口。
    支持 GZIP 压缩和未压缩的 NBT 数据。

    Args:
        data: Schematic 文件完整字节数据。

    Returns:
        :class:`SchematicResult` 解析结果。

    Raises:
        SchematicFormatError: 文件格式错误。
    """
    if not data:
        raise SchematicFormatError("empty schematic data")

    # GZIP 解压
    if data[:2] == b"\x1f\x8b":
        try:
            decompressed = gzip.decompress(data)
        except OSError as exc:
            raise SchematicFormatError(f"gzip decompression failed: {exc}") from exc
    else:
        decompressed = data

    # 大端序 NBT 解析 (Schematic 是 Java 版格式)
    try:
        nbt = nbt_unmarshal_big_endian(decompressed)
    except NBTError as exc:
        raise SchematicFormatError(f"NBT parse failed: {exc}") from exc

    return _build_schematic_result(nbt)


def _build_schematic_result(nbt: dict[str, Any]) -> SchematicResult:
    """从 NBT 构建 SchematicResult。"""
    result = SchematicResult(raw_nbt=nbt)

    # 尺寸 (Short 类型包装)
    def _get_int(key: str, default: int = 0) -> int:
        v = nbt.get(key)
        if v is None:
            return default
        if isinstance(v, (Short, Int, Byte, Long)):
            return v.value
        if isinstance(v, int):
            return v
        return default

    result.width = _get_int("Width")
    result.height = _get_int("Height")
    result.length = _get_int("Length")

    # Materials
    materials = nbt.get("Materials", "Alpha")
    result.materials = materials if isinstance(materials, str) else str(materials)

    # WorldEdit 偏移
    we_x = _get_int("WEOriginX", 0)
    we_y = _get_int("WEOriginY", 0)
    we_z = _get_int("WEOriginZ", 0)
    if any(nbt.get(k) is not None for k in ("WEOriginX", "WEOriginY", "WEOriginZ")):
        result.we_offset = (we_x, we_y, we_z)

    # Blocks 和 Data (ByteArray)
    blocks_arr = nbt.get("Blocks")
    data_arr = nbt.get("Data")
    if isinstance(blocks_arr, ByteArray):
        block_bytes = blocks_arr.value
    elif isinstance(blocks_arr, (list, tuple)):
        block_bytes = list(blocks_arr)
    else:
        block_bytes = []

    if isinstance(data_arr, ByteArray):
        data_bytes = data_arr.value
    elif isinstance(data_arr, (list, tuple)):
        data_bytes = list(data_arr)
    else:
        data_bytes = []

    # 构建方块列表
    total = result.width * result.height * result.length
    for i in range(min(total, len(block_bytes))):
        x = i % result.width
        z = (i // result.width) % result.length
        y = i // (result.width * result.length)
        block_id = block_bytes[i] & 0xFF
        block_data = data_bytes[i] & 0xFF if i < len(data_bytes) else 0
        if block_id == 0:
            continue  # 跳过空气
        result.blocks.append(SchematicBlock(
            position=(x, y, z),
            block_id=block_id,
            block_data=block_data,
        ))

    # TileEntities (List)
    tile_entities = nbt.get("TileEntities", [])
    if isinstance(tile_entities, list):
        result.tile_entities = tile_entities
        # 将 TileEntity 关联到方块
        for te in tile_entities:
            if isinstance(te, dict):
                x = te.get("x")
                y = te.get("y")
                z = te.get("z")
                if x is not None and y is not None and z is not None:
                    x_val = x.value if isinstance(x, (Byte, Short, Int, Long)) else x
                    y_val = y.value if isinstance(y, (Byte, Short, Int, Long)) else y
                    z_val = z.value if isinstance(z, (Byte, Short, Int, Long)) else z
                    for b in result.blocks:
                        if b.position == (x_val, y_val, z_val):
                            b.tile_entity = te
                            break

    # Entities (List)
    entities = nbt.get("Entities", [])
    if isinstance(entities, list):
        result.entities = entities

    # 元数据
    metadata = nbt.get("Metadata", {})
    if isinstance(metadata, dict):
        result.metadata = metadata

    logger.info(
        "Schematic parsed: %dx%dx%d, blocks=%d, tile_entities=%d",
        result.width, result.height, result.length,
        result.total_blocks, len(result.tile_entities),
    )
    return result


def parse_schematic_file(file_path: str) -> SchematicResult:
    """解析 Schematic 文件。"""
    with open(file_path, "rb") as f:
        data = f.read()
    return parse_schematic_bytes(data)


# -------------------------------------------------------------------- #
# 辅助函数
# -------------------------------------------------------------------- #


def get_tile_entity_at(result: SchematicResult,
                        pos: tuple[int, int, int]) -> dict[str, Any] | None:
    """获取指定位置的 TileEntity。

    Args:
        result: Schematic 解析结果。
        pos: 方块坐标 (x, y, z)。

    Returns:
        TileEntity 数据, 如果不存在则返回 None。
    """
    for te in result.tile_entities:
        if not isinstance(te, dict):
            continue
        x = te.get("x")
        y = te.get("y")
        z = te.get("z")
        if x is None or y is None or z is None:
            continue
        x_val = x.value if isinstance(x, (Byte, Short, Int, Long)) else x
        y_val = y.value if isinstance(y, (Byte, Short, Int, Long)) else y
        z_val = z.value if isinstance(z, (Byte, Short, Int, Long)) else z
        if (x_val, y_val, z_val) == pos:
            return te
    return None


def get_tile_entities_by_id(result: SchematicResult,
                              tile_id: str) -> list[dict[str, Any]]:
    """获取指定 ID 的所有 TileEntity。

    Args:
        result: Schematic 解析结果。
        tile_id: TileEntity ID (如 "Chest", "Sign", "CommandBlock")。

    Returns:
        TileEntity 列表。
    """
    return [
        te for te in result.tile_entities
        if isinstance(te, dict) and te.get("id", "") == tile_id
    ]


__all__ = [
    "SchematicError", "SchematicFormatError",
    "SchematicBlock", "SchematicResult",
    "parse_schematic_bytes", "parse_schematic_file",
    "get_tile_entity_at", "get_tile_entities_by_id",
    # 合并自 NovaBuilder 的常量
    "SCHEMATIC_DEPRECATED_WARNING", "SCHEMATIC_MAX_BLOCKS",
    "MATERIALS_ALPHA", "MATERIALS_POCKET",
]
