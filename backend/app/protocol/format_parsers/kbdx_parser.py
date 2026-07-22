"""KBDX格式解析器。

逆向来源: NexusE WaterStructure/structure/kbdx.go
- NexusE v1.6.5: WaterStructure/structure/kbdx.go

KBDX是KBDX建筑工具的专用格式, 基于NBT存储。
支持方块数据、容器内容和命令方块。
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

logger = logging.getLogger("pocketterm.protocol.format_parsers.kbdx")

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: KBDX魔数
KBDX_MAGIC: bytes = b"KBDX"

#: KBDX格式版本
KBDX_VERSION: int = 1

#: 区块大小
CHUNK_SIZE: int = 16


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class KBDXHeader:
    """KBDX文件头。

    Attributes:
        magic: 魔数 (KBDX)
        version: 格式版本
        width: X轴尺寸
        height: Y轴尺寸
        length: Z轴尺寸
        author: 作者名称
        description: 描述
    """

    magic: str = ""
    version: int = 0
    width: int = 0
    height: int = 0
    length: int = 0
    author: str = ""
    description: str = ""


@dataclass
class KBDXBlock:
    """KBDX方块条目。

    Attributes:
        x, y, z: 相对坐标
        block: 方块状态
        nbt: NBT数据 (可选)
    """

    x: int
    y: int
    z: int
    block: BlockState
    nbt: Optional[dict[str, Any]] = None


@dataclass
class KBDXData:
    """KBDX解析结果。

    Attributes:
        header: 文件头
        blocks: 方块列表
        total_blocks: 非空气方块数
    """

    header: KBDXHeader = field(default_factory=KBDXHeader)
    blocks: list[KBDXBlock] = field(default_factory=list)
    total_blocks: int = 0


# ----------------------------------------------------------------------
# KBDX解析器
# ----------------------------------------------------------------------


class KBDXParser:
    """KBDX格式解析器。

    逆向自 NexusE WaterStructure/structure/kbdx.go

    KBDX格式:
        - 文件头: KBDX魔数 + 版本 + 尺寸 + 元数据
        - 方块数据: 压缩的NBT方块数据
        - 容器/命令方块数据: 附加NBT

    使用示例::

        parser = KBDXParser()
        data = parser.parse_file("/path/to/building.kbdx")
        for block in data.blocks:
            print(f"({block.x}, {block.y}, {block.z}): {block.block.name}")
    """

    def __init__(self) -> None:
        """初始化KBDX解析器。"""
        self._block_mapping: dict[str, str] = {}
        """Java方块名 -> Bedrock方块名映射"""

    def set_block_mapping(self, mapping: dict[str, str]) -> None:
        """设置方块名映射表。

        Args:
            mapping: Java方块名 -> Bedrock方块名映射。
        """
        self._block_mapping = mapping

    def parse_file(self, path: str | Path) -> KBDXData:
        """解析KBDX文件。

        Args:
            path: 文件路径。

        Returns:
            KBDXData 解析结果。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 格式无效。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"KBDX文件不存在: {path}")

        with open(path, "rb") as f:
            raw_data = f.read()

        return self.parse(raw_data)

    def parse(self, data: bytes) -> KBDXData:
        """解析KBDX数据。

        Args:
            data: KBDX原始字节数据。

        Returns:
            KBDXData 解析结果。

        Raises:
            ValueError: 格式无效或数据损坏。
        """
        if len(data) < 4:
            raise ValueError("KBDX数据太短")

        # 检查魔数
        magic = data[:4]
        if magic != KBDX_MAGIC:
            raise ValueError(f"无效的KBDX魔数: {magic!r}, 期望: {KBDX_MAGIC!r}")

        offset = 4

        # 读取版本
        if offset + 4 > len(data):
            raise ValueError("KBDX数据不完整: 缺少版本号")
        version = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4

        # 读取尺寸
        if offset + 12 > len(data):
            raise ValueError("KBDX数据不完整: 缺少尺寸信息")
        width = struct.unpack(">I", data[offset:offset + 4])[0]
        height = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        length = struct.unpack(">I", data[offset + 8:offset + 12])[0]
        offset += 12

        if width <= 0 or height <= 0 or length <= 0:
            raise ValueError(f"无效的KBDX尺寸: {width}x{height}x{length}")

        # 读取作者名 (以\0结尾的字符串)
        author_end = data.find(b"\x00", offset)
        if author_end == -1:
            author = data[offset:].decode("utf-8", errors="replace")
            offset = len(data)
        else:
            author = data[offset:author_end].decode("utf-8", errors="replace")
            offset = author_end + 1

        # 读取描述 (以\0结尾的字符串)
        desc_end = data.find(b"\x00", offset)
        if desc_end == -1:
            description = data[offset:].decode("utf-8", errors="replace")
            offset = len(data)
        else:
            description = data[offset:desc_end].decode("utf-8", errors="replace")
            offset = desc_end + 1

        header = KBDXHeader(
            magic=KBDX_MAGIC.decode("ascii"),
            version=version,
            width=width,
            height=height,
            length=length,
            author=author,
            description=description,
        )

        logger.info(
            "KBDX文件头: %dx%dx%d, 作者: %s, 版本: %d",
            width, height, length, author, version,
        )

        # 解析方块数据 (NBT格式)
        blocks = self._parse_blocks(data, offset, width, height, length)

        total_blocks = sum(1 for b in blocks if b.block.name != "minecraft:air")

        logger.info("KBDX解析完成: %d 方块, %d 非空气", len(blocks), total_blocks)

        return KBDXData(
            header=header,
            blocks=blocks,
            total_blocks=total_blocks,
        )

    def _parse_blocks(
        self,
        data: bytes,
        offset: int,
        width: int,
        height: int,
        length: int,
    ) -> list[KBDXBlock]:
        """解析KBDX方块数据。

        KBDX方块数据格式:
            - 可能使用gzip压缩
            - NBT格式存储方块列表

        Args:
            data: 数据字节
            offset: 起始偏移量
            width: X轴尺寸
            height: Y轴尺寸
            length: Z轴尺寸

        Returns:
            KBDXBlock列表。
        """
        blocks: list[KBDXBlock] = []
        remaining = data[offset:]

        # 尝试解压gzip
        if len(remaining) >= 2 and remaining[0] == 0x1F and remaining[1] == 0x8B:
            try:
                remaining = gzip.decompress(remaining)
            except gzip.BadGzipFile as e:
                logger.warning("KBDX gzip解压失败: %s, 尝试原始数据", e)

        # 尝试解析NBT数据
        try:
            from ..nbt import parse_nbt as _parse_nbt
            nbt_data, _ = _parse_nbt(remaining)
            blocks = self._nbt_to_blocks(nbt_data, width, height, length)
        except ImportError:
            logger.warning("NBT库不可用, 使用简化解析")
            blocks = self._parse_simple_blocks(remaining, width, height, length)
        except Exception as e:
            logger.warning("NBT解析失败: %s, 使用简化解析", e)
            blocks = self._parse_simple_blocks(remaining, width, height, length)

        return blocks

    def _nbt_to_blocks(
        self,
        nbt_data: dict[str, Any],
        width: int,
        height: int,
        length: int,
    ) -> list[KBDXBlock]:
        """将NBT数据转换为方块列表。

        Args:
            nbt_data: NBT数据字典
            width, height, length: 尺寸

        Returns:
            KBDXBlock列表。
        """
        blocks: list[KBDXBlock] = []

        # 获取调色板
        palette = nbt_data.get("Palette", nbt_data.get("palette", {}))
        palette_list = nbt_data.get("PaletteList", nbt_data.get("palette_list", []))

        # 获取方块数据
        block_data = nbt_data.get("BlockData", nbt_data.get("block_data", []))
        if isinstance(block_data, bytes):
            block_data = list(block_data)

        # 获取NBT实体数据
        block_entities = nbt_data.get("BlockEntities", nbt_data.get("block_entities", []))
        nbt_map: dict[tuple[int, int, int], dict[str, Any]] = {}
        for entity in block_entities:
            pos = entity.get("Pos", entity.get("pos", [0, 0, 0]))
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                nbt_map[(int(pos[0]), int(pos[1]), int(pos[2]))] = entity

        # 遍历方块数据
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
                    elif isinstance(palette_list, list) and block_id < len(palette_list):
                        block_name = palette_list[block_id]

                    # 映射Java方块名到Bedrock
                    block_name = self._block_mapping.get(block_name, block_name)

                    nbt = nbt_map.get((x, y, z))
                    blocks.append(KBDXBlock(
                        x=x, y=y, z=z,
                        block=BlockState(name=block_name),
                        nbt=nbt,
                    ))

                    index += 1

        return blocks

    def _parse_simple_blocks(
        self,
        data: bytes,
        width: int,
        height: int,
        length: int,
    ) -> list[KBDXBlock]:
        """简化方块解析 (无NBT库回退方案)。

        格式: 每个方块 4字节 (X, Y, Z, BlockID) + 变长方块名

        Args:
            data: 数据字节
            width, height, length: 尺寸

        Returns:
            KBDXBlock列表。
        """
        blocks: list[KBDXBlock] = []
        offset = 0
        total = width * height * length

        for i in range(total):
            if offset >= len(data):
                break

            # 简单解析: 每4字节一个方块ID
            if offset + 4 <= len(data):
                block_id = struct.unpack(">I", data[offset:offset + 4])[0]
                offset += 4

                x = i % width
                z = (i // width) % length
                y = i // (width * length)

                # 简单的方块ID -> 方块名映射
                block_name = self._simple_id_to_name(block_id)

                blocks.append(KBDXBlock(
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
            return f"minecraft:unknown_{block_id}"

    def extract_blocks(
        self,
        data: KBDXData,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> list[tuple[int, int, int, BlockState]]:
        """提取方块列表 (带世界坐标偏移)。

        Args:
            data: KBDXData解析结果
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (x, y, z, BlockState) 元组列表。
        """
        return [
            (b.x + offset_x, b.y + offset_y, b.z + offset_z, b.block)
            for b in data.blocks
            if b.block.name != "minecraft:air"
        ]

    def get_bounds(self, data: KBDXData) -> tuple[int, int, int, int, int, int]:
        """获取结构的包围盒。

        Args:
            data: KBDXData解析结果

        Returns:
            (min_x, min_y, min_z, max_x, max_y, max_z)。
        """
        return (0, 0, 0, data.header.width - 1, data.header.height - 1, data.header.length - 1)


__all__ = [
    "KBDXHeader",
    "KBDXBlock",
    "KBDXData",
    "KBDXParser",
]