"""AxiomBP格式解析器。

逆向来源: NexusE WaterStructure/structure/axiombp.go
- NexusE v1.6.5: WaterStructure/structure/axiombp.go

AxiomBP是Axiom建筑工具的导出格式, 基于NBT存储。
支持方块数据、实体和方块实体。
"""

from __future__ import annotations

import gzip
import json
import logging
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..blocks import BlockState

logger = logging.getLogger("pocketterm.protocol.format_parsers.axiombp")

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: AxiomBP魔数
AXIOMBP_MAGIC: bytes = b"AXBP"

#: AxiomBP格式版本
AXIOMBP_VERSION: int = 1


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class AxiomBPHeader:
    """AxiomBP文件头。

    Attributes:
        magic: 魔数 (AXBP)
        version: 格式版本
        width: X轴尺寸
        height: Y轴尺寸
        length: Z轴尺寸
        total_blocks: 方块总数
        has_entities: 是否包含实体数据
    """

    magic: str = ""
    version: int = 0
    width: int = 0
    height: int = 0
    length: int = 0
    total_blocks: int = 0
    has_entities: bool = False


@dataclass
class AxiomBPBlock:
    """AxiomBP方块条目。

    Attributes:
        x, y, z: 相对坐标
        block: 方块状态
        nbt: NBT数据 (可选)
        block_entity: 方块实体数据 (可选)
    """

    x: int
    y: int
    z: int
    block: BlockState
    nbt: Optional[dict[str, Any]] = None
    block_entity: Optional[dict[str, Any]] = None


@dataclass
class AxiomBPEntity:
    """AxiomBP实体条目。

    Attributes:
        entity_type: 实体类型
        x, y, z: 实体坐标
        nbt: 实体NBT数据
    """

    entity_type: str = ""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    nbt: Optional[dict[str, Any]] = None


@dataclass
class AxiomBPData:
    """AxiomBP解析结果。

    Attributes:
        header: 文件头
        blocks: 方块列表
        entities: 实体列表
        total_blocks: 非空气方块数
    """

    header: AxiomBPHeader = field(default_factory=AxiomBPHeader)
    blocks: list[AxiomBPBlock] = field(default_factory=list)
    entities: list[AxiomBPEntity] = field(default_factory=list)
    total_blocks: int = 0


# ----------------------------------------------------------------------
# AxiomBP解析器
# ----------------------------------------------------------------------


class AxiomBPParser:
    """AxiomBP格式解析器。

    逆向自 NexusE WaterStructure/structure/axiombp.go

    AxiomBP格式:
        - 文件头: AXBP魔数 + 版本 + 尺寸
        - 方块数据: gzip压缩的NBT方块数据
        - 实体数据: 可选的NBT实体数据

    使用示例::

        parser = AxiomBPParser()
        data = parser.parse_file("/path/to/building.axiombp")
        for block in data.blocks:
            print(f"({block.x}, {block.y}, {block.z}): {block.block.name}")
    """

    def __init__(self) -> None:
        """初始化AxiomBP解析器。"""
        self._block_mapping: dict[str, str] = {}
        """方块名映射表"""

    def set_block_mapping(self, mapping: dict[str, str]) -> None:
        """设置方块名映射表。

        Args:
            mapping: 方块名 -> Bedrock方块名映射。
        """
        self._block_mapping = mapping

    def parse_file(self, path: str | Path) -> AxiomBPData:
        """解析AxiomBP文件。

        Args:
            path: 文件路径。

        Returns:
            AxiomBPData 解析结果。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 格式无效。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"AxiomBP文件不存在: {path}")

        with open(path, "rb") as f:
            raw_data = f.read()

        return self.parse(raw_data)

    def parse(self, data: bytes) -> AxiomBPData:
        """解析AxiomBP数据。

        Args:
            data: AxiomBP原始字节数据。

        Returns:
            AxiomBPData 解析结果。

        Raises:
            ValueError: 格式无效或数据损坏。
        """
        if len(data) < 16:
            raise ValueError("AxiomBP数据太短")

        # 检查魔数
        magic = data[:4]
        if magic != AXIOMBP_MAGIC:
            raise ValueError(f"无效的AxiomBP魔数: {magic!r}, 期望: {AXIOMBP_MAGIC!r}")

        offset = 4

        # 读取版本
        version = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4

        # 读取尺寸
        width = struct.unpack(">I", data[offset:offset + 4])[0]
        height = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        length = struct.unpack(">I", data[offset + 8:offset + 12])[0]
        offset += 12

        if width <= 0 or height <= 0 or length <= 0:
            raise ValueError(f"无效的AxiomBP尺寸: {width}x{height}x{length}")

        # 读取实体标志
        has_entities = False
        if offset < len(data):
            has_entities = data[offset] != 0
            offset += 1

        header = AxiomBPHeader(
            magic=AXIOMBP_MAGIC.decode("ascii"),
            version=version,
            width=width,
            height=height,
            length=length,
            has_entities=has_entities,
        )

        logger.info(
            "AxiomBP文件头: %dx%dx%d, 版本: %d, 实体: %s",
            width, height, length, version, has_entities,
        )

        # 解析方块数据
        remaining = data[offset:]

        # 尝试gzip解压
        if len(remaining) >= 2 and remaining[0] == 0x1F and remaining[1] == 0x8B:
            try:
                remaining = gzip.decompress(remaining)
            except gzip.BadGzipFile as e:
                logger.warning("AxiomBP gzip解压失败: %s", e)

        # 解析NBT数据
        blocks, entities = self._parse_nbt_data(remaining, has_entities)

        header.total_blocks = len(blocks)
        total_blocks = sum(1 for b in blocks if b.block.name != "minecraft:air")

        logger.info(
            "AxiomBP解析完成: %d 方块, %d 非空气, %d 实体",
            len(blocks), total_blocks, len(entities),
        )

        return AxiomBPData(
            header=header,
            blocks=blocks,
            entities=entities,
            total_blocks=total_blocks,
        )

    def _parse_nbt_data(
        self,
        data: bytes,
        has_entities: bool,
    ) -> tuple[list[AxiomBPBlock], list[AxiomBPEntity]]:
        """解析NBT数据。

        Args:
            data: NBT数据字节
            has_entities: 是否包含实体数据

        Returns:
            (方块列表, 实体列表) 元组。
        """
        blocks: list[AxiomBPBlock] = []
        entities: list[AxiomBPEntity] = []

        try:
            from ..nbt import parse_nbt as _parse_nbt
            nbt_data, _ = _parse_nbt(data)
            blocks = self._nbt_to_blocks(nbt_data)

            if has_entities:
                entities = self._nbt_to_entities(nbt_data)
        except ImportError:
            logger.warning("NBT库不可用, 使用简化解析")
            blocks = self._parse_simple(data)
        except Exception as e:
            logger.warning("AxiomBP NBT解析失败: %s", e)
            blocks = self._parse_simple(data)

        return blocks, entities

    def _nbt_to_blocks(self, nbt_data: dict[str, Any]) -> list[AxiomBPBlock]:
        """将NBT数据转换为方块列表。

        Args:
            nbt_data: NBT数据字典

        Returns:
            AxiomBPBlock列表。
        """
        blocks: list[AxiomBPBlock] = []

        # AxiomBP使用Palette和BlockData
        palette = nbt_data.get("Palette", nbt_data.get("palette", {}))
        block_data = nbt_data.get("BlockData", nbt_data.get("block_data", []))
        if isinstance(block_data, bytes):
            block_data = list(block_data)

        # 方块实体
        block_entities = nbt_data.get("BlockEntities", nbt_data.get("block_entities", []))
        nbt_map: dict[tuple[int, int, int], dict[str, Any]] = {}
        for entity in block_entities:
            pos = entity.get("Pos", entity.get("pos", [0, 0, 0]))
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                nbt_map[(int(pos[0]), int(pos[1]), int(pos[2]))] = entity

        width = nbt_data.get("Width", nbt_data.get("width", 16))
        height = nbt_data.get("Height", nbt_data.get("height", 16))
        length = nbt_data.get("Length", nbt_data.get("length", 16))

        index = 0
        for y in range(height):
            for z in range(length):
                for x in range(width):
                    if index >= len(block_data):
                        break

                    block_id = block_data[index]
                    block_name = "minecraft:air"

                    if isinstance(palette, dict) and str(block_id) in palette:
                        block_name = palette[str(block_id)]
                    elif isinstance(palette, list) and block_id < len(palette):
                        palette_entry = palette[block_id]
                        if isinstance(palette_entry, dict):
                            block_name = palette_entry.get("Name", palette_entry.get("name", "minecraft:air"))
                        else:
                            block_name = str(palette_entry)

                    block_name = self._block_mapping.get(block_name, block_name)
                    nbt = nbt_map.get((x, y, z))

                    # 解析方块状态
                    states = {}
                    if isinstance(palette, list) and block_id < len(palette):
                        palette_entry = palette[block_id]
                        if isinstance(palette_entry, dict):
                            props = palette_entry.get("Properties", palette_entry.get("properties", {}))
                            if isinstance(props, dict):
                                states = props

                    blocks.append(AxiomBPBlock(
                        x=x, y=y, z=z,
                        block=BlockState(name=block_name, states=states),
                        nbt=nbt,
                        block_entity=nbt_map.get((x, y, z)),
                    ))

                    index += 1

        return blocks

    def _nbt_to_entities(self, nbt_data: dict[str, Any]) -> list[AxiomBPEntity]:
        """将NBT数据转换为实体列表。

        Args:
            nbt_data: NBT数据字典

        Returns:
            AxiomBPEntity列表。
        """
        entities: list[AxiomBPEntity] = []

        entity_list = nbt_data.get("Entities", nbt_data.get("entities", []))
        if not isinstance(entity_list, list):
            return entities

        for entity in entity_list:
            if not isinstance(entity, dict):
                continue

            entity_type = entity.get("Id", entity.get("id", ""))
            pos = entity.get("Pos", entity.get("pos", [0.0, 0.0, 0.0]))

            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                entities.append(AxiomBPEntity(
                    entity_type=str(entity_type),
                    x=float(pos[0]),
                    y=float(pos[1]),
                    z=float(pos[2]),
                    nbt=entity,
                ))

        return entities

    def _parse_simple(self, data: bytes) -> list[AxiomBPBlock]:
        """简化方块解析 (无NBT库回退方案)。

        Args:
            data: 数据字节

        Returns:
            AxiomBPBlock列表。
        """
        blocks: list[AxiomBPBlock] = []
        offset = 0

        # 简单解析: 每8字节一个方块 (X, Y, Z各2字节, BlockID 2字节)
        while offset + 8 <= len(data):
            x = struct.unpack(">H", data[offset:offset + 2])[0]
            y = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            z = struct.unpack(">H", data[offset + 4:offset + 6])[0]
            block_id = struct.unpack(">H", data[offset + 6:offset + 8])[0]
            offset += 8

            block_name = self._simple_id_to_name(block_id)
            blocks.append(AxiomBPBlock(
                x=x, y=y, z=z,
                block=BlockState(name=block_name),
            ))

        return blocks

    def _simple_id_to_name(self, block_id: int) -> str:
        """简单的方块ID -> 方块名映射。

        Args:
            block_id: 方块ID

        Returns:
            方块名。
        """
        if block_id == 0:
            return "minecraft:air"
        elif block_id == 1:
            return "minecraft:stone"
        elif block_id == 2:
            return "minecraft:grass"
        elif block_id == 3:
            return "minecraft:dirt"
        else:
            return f"minecraft:block_{block_id}"

    def extract_blocks(
        self,
        data: AxiomBPData,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> list[tuple[int, int, int, BlockState]]:
        """提取方块列表 (带世界坐标偏移)。

        Args:
            data: AxiomBPData解析结果
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (x, y, z, BlockState) 元组列表。
        """
        return [
            (b.x + offset_x, b.y + offset_y, b.z + offset_z, b.block)
            for b in data.blocks
            if b.block.name != "minecraft:air"
        ]

    def get_bounds(self, data: AxiomBPData) -> tuple[int, int, int, int, int, int]:
        """获取结构的包围盒。

        Args:
            data: AxiomBPData解析结果

        Returns:
            (min_x, min_y, min_z, max_x, max_y, max_z)。
        """
        return (0, 0, 0, data.header.width - 1, data.header.height - 1, data.header.length - 1)


__all__ = [
    "AxiomBPHeader",
    "AxiomBPBlock",
    "AxiomBPEntity",
    "AxiomBPData",
    "AxiomBPParser",
]