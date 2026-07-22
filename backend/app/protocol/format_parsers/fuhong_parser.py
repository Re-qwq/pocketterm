"""富宏建筑格式解析器。

逆向来源: NexusE WaterStructure/structure/fuhong.go
- NexusE v1.6.5: WaterStructure/structure/fuhong.go

富宏是国服常用的建筑工具, 支持V1~V6格式。
格式特点:
    - V1: 基础文本格式, 每行: x,y,z,block_id
    - V2: 支持方块状态
    - V3: 支持NBT数据
    - V4: 支持压缩 (gzip)
    - V5: 支持多区块
    - V6: 支持增量导出
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

logger = logging.getLogger("pocketterm.protocol.format_parsers.fuhong")

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 富宏格式魔数
FUHONG_MAGIC: bytes = b"FUHG"

#: 富宏版本范围
FUHONG_MIN_VERSION: int = 1
FUHONG_MAX_VERSION: int = 6


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class FuHongHeader:
    """富宏文件头。

    Attributes:
        magic: 魔数 (FUHG)
        version: 格式版本 (1-6)
        width: X轴尺寸
        height: Y轴尺寸
        length: Z轴尺寸
        author: 作者名称
        block_count: 方块数量
    """

    magic: str = ""
    version: int = 1
    width: int = 0
    height: int = 0
    length: int = 0
    author: str = ""
    block_count: int = 0


@dataclass
class FuHongBlock:
    """富宏方块条目。

    Attributes:
        x, y, z: 相对坐标
        block: 方块状态
        nbt: NBT数据 (V3+)
    """

    x: int
    y: int
    z: int
    block: BlockState
    nbt: Optional[dict[str, Any]] = None


@dataclass
class FuHongData:
    """富宏解析结果。

    Attributes:
        header: 文件头
        blocks: 方块列表
        total_blocks: 非空气方块数
    """

    header: FuHongHeader = field(default_factory=FuHongHeader)
    blocks: list[FuHongBlock] = field(default_factory=list)
    total_blocks: int = 0


# ----------------------------------------------------------------------
# 富宏解析器
# ----------------------------------------------------------------------


class FuHongParser:
    """富宏建筑格式解析器。

    逆向自 NexusE WaterStructure/structure/fuhong.go

    支持FuHong V1~V6格式。

    使用示例::

        parser = FuHongParser()
        data = parser.parse_file("/path/to/building.fuhong")
        for block in data.blocks:
            print(f"({block.x}, {block.y}, {block.z}): {block.block.name}")
    """

    def __init__(self) -> None:
        """初始化富宏解析器。"""
        self._block_mapping: dict[str, str] = {}
        """方块名映射表"""

    def set_block_mapping(self, mapping: dict[str, str]) -> None:
        """设置方块名映射表。

        Args:
            mapping: 方块名 -> Bedrock方块名映射。
        """
        self._block_mapping = mapping

    def parse_file(self, path: str | Path) -> FuHongData:
        """解析富宏文件。

        Args:
            path: 文件路径。

        Returns:
            FuHongData 解析结果。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 格式无效。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"富宏文件不存在: {path}")

        with open(path, "rb") as f:
            raw_data = f.read()

        return self.parse(raw_data)

    def parse(self, data: bytes) -> FuHongData:
        """解析富宏数据。

        Args:
            data: 富宏原始字节数据。

        Returns:
            FuHongData 解析结果。

        Raises:
            ValueError: 格式无效或数据损坏。
        """
        if len(data) < 12:
            raise ValueError("富宏数据太短")

        # 检查魔数
        magic = data[:4]
        if magic != FUHONG_MAGIC:
            # 尝试作为文本格式解析 (V1/V2)
            return self._parse_text_format(data)

        offset = 4

        # 读取版本
        version = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4

        if version < FUHONG_MIN_VERSION or version > FUHONG_MAX_VERSION:
            raise ValueError(f"不支持的富宏版本: {version}")

        # 读取尺寸
        width = struct.unpack(">I", data[offset:offset + 4])[0]
        height = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        length = struct.unpack(">I", data[offset + 8:offset + 12])[0]
        offset += 12

        # 读取作者
        author_end = data.find(b"\x00", offset)
        author = data[offset:author_end].decode("utf-8", errors="replace") if author_end != -1 else ""
        offset = (author_end + 1) if author_end != -1 else len(data)

        header = FuHongHeader(
            magic=FUHONG_MAGIC.decode("ascii"),
            version=version,
            width=width,
            height=height,
            length=length,
            author=author,
        )

        logger.info(
            "富宏V%d文件头: %dx%dx%d, 作者: %s",
            version, width, height, length, author,
        )

        # 根据版本解析方块数据
        blocks = self._parse_blocks_by_version(data, offset, version, width, height, length)

        header.block_count = len(blocks)
        total_blocks = sum(1 for b in blocks if b.block.name != "minecraft:air")

        logger.info("富宏解析完成: %d 方块, %d 非空气", len(blocks), total_blocks)

        return FuHongData(
            header=header,
            blocks=blocks,
            total_blocks=total_blocks,
        )

    def _parse_text_format(self, data: bytes) -> FuHongData:
        """解析富宏文本格式 (V1/V2)。

        格式: 每行 "x,y,z,block_id" 或 "x,y,z,block_id,states_json"

        Args:
            data: 文本数据字节

        Returns:
            FuHongData。
        """
        text = data.decode("utf-8", errors="replace")
        lines = text.strip().split("\n")

        blocks: list[FuHongBlock] = []
        min_x = min_y = min_z = float("inf")
        max_x = max_y = max_z = float("-inf")

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            parts = line.split(",")
            if len(parts) < 4:
                continue

            try:
                x = int(parts[0])
                y = int(parts[1])
                z = int(parts[2])
                block_name = parts[3].strip()

                states = {}
                if len(parts) >= 5:
                    try:
                        states = json.loads(parts[4])
                    except (json.JSONDecodeError, TypeError):
                        pass

                # 映射方块名
                block_name = self._block_mapping.get(block_name, block_name)

                blocks.append(FuHongBlock(
                    x=x, y=y, z=z,
                    block=BlockState(name=block_name, states=states),
                ))

                min_x = min(min_x, x)
                min_y = min(min_y, y)
                min_z = min(min_z, z)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
                max_z = max(max_z, z)

            except (ValueError, IndexError) as e:
                logger.warning("富宏文本格式解析跳过行: %s -> %s", line, e)
                continue

        width = int(max_x - min_x + 1) if blocks else 0
        height = int(max_y - min_y + 1) if blocks else 0
        length = int(max_z - min_z + 1) if blocks else 0

        header = FuHongHeader(
            magic="FUHG",
            version=1,
            width=width,
            height=height,
            length=length,
            author="",
            block_count=len(blocks),
        )

        total_blocks = sum(1 for b in blocks if b.block.name != "minecraft:air")

        return FuHongData(
            header=header,
            blocks=blocks,
            total_blocks=total_blocks,
        )

    def _parse_blocks_by_version(
        self,
        data: bytes,
        offset: int,
        version: int,
        width: int,
        height: int,
        length: int,
    ) -> list[FuHongBlock]:
        """根据版本解析方块数据。

        Args:
            data: 数据字节
            offset: 起始偏移量
            version: 格式版本
            width, height, length: 尺寸

        Returns:
            FuHongBlock列表。
        """
        if version in (1, 2):
            return self._parse_v1_v2(data, offset, width, height, length)
        elif version in (3, 4):
            return self._parse_v3_v4(data, offset, width, height, length)
        elif version in (5, 6):
            return self._parse_v5_v6(data, offset, width, height, length)
        else:
            raise ValueError(f"不支持的富宏版本: {version}")

    def _parse_v1_v2(
        self,
        data: bytes,
        offset: int,
        width: int,
        height: int,
        length: int,
    ) -> list[FuHongBlock]:
        """解析V1/V2方块数据。

        V1: 每个方块 6字节 (X, Y, Z, BlockID各2字节)
        V2: 每个方块 8字节 (X, Y, Z, BlockID各2字节, 状态2字节)

        Args:
            data: 数据字节
            offset: 起始偏移量
            width, height, length: 尺寸

        Returns:
            FuHongBlock列表。
        """
        blocks: list[FuHongBlock] = []
        total = width * height * length

        for i in range(total):
            if offset + 6 > len(data):
                break

            x = struct.unpack(">H", data[offset:offset + 2])[0]
            y = struct.unpack(">H", data[offset + 2:offset + 4])[0]
            z = struct.unpack(">H", data[offset + 4:offset + 6])[0]
            block_id_raw = struct.unpack(">H", data[offset + 6:offset + 8])[0] if offset + 8 <= len(data) else 0
            offset += 8 if offset + 8 <= len(data) else 6

            block_name = f"minecraft:block_{block_id_raw}"
            blocks.append(FuHongBlock(
                x=x, y=y, z=z,
                block=BlockState(name=block_name),
            ))

        return blocks

    def _parse_v3_v4(
        self,
        data: bytes,
        offset: int,
        width: int,
        height: int,
        length: int,
    ) -> list[FuHongBlock]:
        """解析V3/V4方块数据。

        V3: 支持NBT数据
        V4: 支持gzip压缩

        Args:
            data: 数据字节
            offset: 起始偏移量
            width, height, length: 尺寸

        Returns:
            FuHongBlock列表。
        """
        remaining = data[offset:]

        # V4: 尝试gzip解压
        if len(remaining) >= 2 and remaining[0] == 0x1F and remaining[1] == 0x8B:
            try:
                remaining = gzip.decompress(remaining)
            except gzip.BadGzipFile as e:
                logger.warning("富宏gzip解压失败: %s", e)

        # 尝试NBT解析
        try:
            from ..nbt import parse_nbt as _parse_nbt
            nbt_data, _ = _parse_nbt(remaining)
            return self._nbt_to_blocks(nbt_data)
        except ImportError:
            pass
        except Exception as e:
            logger.warning("富宏NBT解析失败: %s", e)

        return self._parse_v1_v2(data, offset, width, height, length)

    def _parse_v5_v6(
        self,
        data: bytes,
        offset: int,
        width: int,
        height: int,
        length: int,
    ) -> list[FuHongBlock]:
        """解析V5/V6方块数据。

        V5: 多区块支持
        V6: 增量导出

        Args:
            data: 数据字节
            offset: 起始偏移量
            width, height, length: 尺寸

        Returns:
            FuHongBlock列表。
        """
        # V5/V6使用类似V3/V4的NBT格式, 但增加了区块元数据
        remaining = data[offset:]

        if len(remaining) >= 2 and remaining[0] == 0x1F and remaining[1] == 0x8B:
            try:
                remaining = gzip.decompress(remaining)
            except gzip.BadGzipFile:
                pass

        try:
            from ..nbt import parse_nbt as _parse_nbt
            nbt_data, _ = _parse_nbt(remaining)
            return self._nbt_to_blocks(nbt_data)
        except ImportError:
            pass
        except Exception:
            pass

        return self._parse_v1_v2(data, offset, width, height, length)

    def _nbt_to_blocks(self, nbt_data: dict[str, Any]) -> list[FuHongBlock]:
        """将NBT数据转换为方块列表。

        Args:
            nbt_data: NBT数据字典

        Returns:
            FuHongBlock列表。
        """
        blocks: list[FuHongBlock] = []

        palette = nbt_data.get("Palette", nbt_data.get("palette", {}))
        block_data = nbt_data.get("BlockData", nbt_data.get("block_data", []))
        if isinstance(block_data, bytes):
            block_data = list(block_data)

        block_entities = nbt_data.get("BlockEntities", nbt_data.get("block_entities", []))
        nbt_map: dict[tuple[int, int, int], dict[str, Any]] = {}
        for entity in block_entities:
            pos = entity.get("Pos", entity.get("pos", [0, 0, 0]))
            if isinstance(pos, (list, tuple)) and len(pos) >= 3:
                nbt_map[(int(pos[0]), int(pos[1]), int(pos[2]))] = entity

        # 从NBT数据中提取尺寸
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

                    block_name = self._block_mapping.get(block_name, block_name)
                    nbt = nbt_map.get((x, y, z))

                    blocks.append(FuHongBlock(
                        x=x, y=y, z=z,
                        block=BlockState(name=block_name),
                        nbt=nbt,
                    ))

                    index += 1

        return blocks

    def extract_blocks(
        self,
        data: FuHongData,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> list[tuple[int, int, int, BlockState]]:
        """提取方块列表 (带世界坐标偏移)。

        Args:
            data: FuHongData解析结果
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (x, y, z, BlockState) 元组列表。
        """
        return [
            (b.x + offset_x, b.y + offset_y, b.z + offset_z, b.block)
            for b in data.blocks
            if b.block.name != "minecraft:air"
        ]

    def get_bounds(self, data: FuHongData) -> tuple[int, int, int, int, int, int]:
        """获取结构的包围盒。

        Args:
            data: FuHongData解析结果

        Returns:
            (min_x, min_y, min_z, max_x, max_y, max_z)。
        """
        return (0, 0, 0, data.header.width - 1, data.header.height - 1, data.header.length - 1)


__all__ = [
    "FuHongHeader",
    "FuHongBlock",
    "FuHongData",
    "FuHongParser",
]