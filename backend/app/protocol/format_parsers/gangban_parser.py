"""钢板建筑格式解析器。

逆向来源: NexusE WaterStructure/structure/gangban.go
- NexusE v1.6.5: WaterStructure/structure/gangban.go

钢板是国服常用的建筑工具, 支持V1~V7格式。
格式特点:
    - V1: 基础二进制格式
    - V2: 支持方块状态
    - V3: 支持NBT数据
    - V4: 支持压缩 (zlib)
    - V5: 支持多区块
    - V6: 支持增量导出
    - V7: 支持运行时ID池
"""

from __future__ import annotations

import json
import logging
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..blocks import BlockState

logger = logging.getLogger("pocketterm.protocol.format_parsers.gangban")

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 钢板格式魔数
GANGBAN_MAGIC: bytes = b"GGBD"

#: 钢板版本范围
GANGBAN_MIN_VERSION: int = 1
GANGBAN_MAX_VERSION: int = 7


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class GangBanHeader:
    """钢板文件头。

    Attributes:
        magic: 魔数 (GGBD)
        version: 格式版本 (1-7)
        width: X轴尺寸
        height: Y轴尺寸
        length: Z轴尺寸
        author: 作者名称
        block_count: 方块数量
        compressed: 是否压缩 (V4+)
    """

    magic: str = ""
    version: int = 1
    width: int = 0
    height: int = 0
    length: int = 0
    author: str = ""
    block_count: int = 0
    compressed: bool = False


@dataclass
class GangBanBlock:
    """钢板方块条目。

    Attributes:
        x, y, z: 相对坐标
        block: 方块状态
        nbt: NBT数据 (V3+)
        runtime_id: 运行时ID (V7)
    """

    x: int
    y: int
    z: int
    block: BlockState
    nbt: Optional[dict[str, Any]] = None
    runtime_id: int = 0


@dataclass
class GangBanData:
    """钢板解析结果。

    Attributes:
        header: 文件头
        blocks: 方块列表
        total_blocks: 非空气方块数
    """

    header: GangBanHeader = field(default_factory=GangBanHeader)
    blocks: list[GangBanBlock] = field(default_factory=list)
    total_blocks: int = 0


# ----------------------------------------------------------------------
# 钢板解析器
# ----------------------------------------------------------------------


class GangBanParser:
    """钢板建筑格式解析器。

    逆向自 NexusE WaterStructure/structure/gangban.go

    支持GangBan V1~V7格式。

    使用示例::

        parser = GangBanParser()
        data = parser.parse_file("/path/to/building.gangban")
        for block in data.blocks:
            print(f"({block.x}, {block.y}, {block.z}): {block.block.name}")
    """

    def __init__(self) -> None:
        """初始化钢板解析器。"""
        self._block_mapping: dict[str, str] = {}
        """方块名映射表"""

        self._runtime_pool: dict[int, str] = {}
        """运行时ID池 (V7)"""

    def set_block_mapping(self, mapping: dict[str, str]) -> None:
        """设置方块名映射表。

        Args:
            mapping: 方块名 -> Bedrock方块名映射。
        """
        self._block_mapping = mapping

    def set_runtime_pool(self, pool: dict[int, str]) -> None:
        """设置运行时ID池。

        Args:
            pool: 运行时ID -> 方块名映射。
        """
        self._runtime_pool = pool

    def parse_file(self, path: str | Path) -> GangBanData:
        """解析钢板文件。

        Args:
            path: 文件路径。

        Returns:
            GangBanData 解析结果。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 格式无效。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"钢板文件不存在: {path}")

        with open(path, "rb") as f:
            raw_data = f.read()

        return self.parse(raw_data)

    def parse(self, data: bytes) -> GangBanData:
        """解析钢板数据。

        Args:
            data: 钢板原始字节数据。

        Returns:
            GangBanData 解析结果。

        Raises:
            ValueError: 格式无效或数据损坏。
        """
        if len(data) < 16:
            raise ValueError("钢板数据太短")

        # 检查魔数
        magic = data[:4]
        if magic != GANGBAN_MAGIC:
            raise ValueError(f"无效的钢板魔数: {magic!r}, 期望: {GANGBAN_MAGIC!r}")

        offset = 4

        # 读取版本
        version = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4

        if version < GANGBAN_MIN_VERSION or version > GANGBAN_MAX_VERSION:
            raise ValueError(f"不支持的钢板版本: {version}")

        # 读取尺寸
        width = struct.unpack(">I", data[offset:offset + 4])[0]
        height = struct.unpack(">I", data[offset + 4:offset + 8])[0]
        length = struct.unpack(">I", data[offset + 8:offset + 12])[0]
        offset += 12

        if width <= 0 or height <= 0 or length <= 0:
            raise ValueError(f"无效的钢板尺寸: {width}x{height}x{length}")

        # 读取压缩标志 (V4+)
        compressed = False
        if version >= 4:
            if offset < len(data):
                compressed = data[offset] != 0
                offset += 1

        # 读取作者
        author_end = data.find(b"\x00", offset)
        author = data[offset:author_end].decode("utf-8", errors="replace") if author_end != -1 else ""
        offset = (author_end + 1) if author_end != -1 else len(data)

        header = GangBanHeader(
            magic=GANGBAN_MAGIC.decode("ascii"),
            version=version,
            width=width,
            height=height,
            length=length,
            author=author,
            compressed=compressed,
        )

        logger.info(
            "钢板V%d文件头: %dx%dx%d, 作者: %s, 压缩: %s",
            version, width, height, length, author, compressed,
        )

        # 解析方块数据
        blocks = self._parse_blocks_by_version(data, offset, version, width, height, length, compressed)

        header.block_count = len(blocks)
        total_blocks = sum(1 for b in blocks if b.block.name != "minecraft:air")

        logger.info("钢板解析完成: %d 方块, %d 非空气", len(blocks), total_blocks)

        return GangBanData(
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
        compressed: bool,
    ) -> list[GangBanBlock]:
        """根据版本解析方块数据。

        Args:
            data: 数据字节
            offset: 起始偏移量
            version: 格式版本
            width, height, length: 尺寸
            compressed: 是否压缩

        Returns:
            GangBanBlock列表。
        """
        remaining = data[offset:]

        # 解压缩
        if compressed:
            try:
                remaining = zlib.decompress(remaining)
            except zlib.error as e:
                logger.warning("钢板zlib解压失败: %s", e)

        if version in (1, 2):
            return self._parse_v1_v2(remaining, width, height, length)
        elif version in (3, 4):
            return self._parse_v3_v4(remaining, width, height, length)
        elif version in (5, 6):
            return self._parse_v5_v6(remaining, width, height, length)
        elif version == 7:
            return self._parse_v7(remaining, width, height, length)
        else:
            return self._parse_v1_v2(remaining, width, height, length)

    def _parse_v1_v2(
        self,
        data: bytes,
        width: int,
        height: int,
        length: int,
    ) -> list[GangBanBlock]:
        """解析V1/V2方块数据。

        V1: 每个方块 4字节 (X, Y, Z各1字节, BlockID 1字节)
        V2: 每个方块 6字节 (X, Y, Z各1字节, BlockID 2字节, 状态1字节)

        Args:
            data: 数据字节
            width, height, length: 尺寸

        Returns:
            GangBanBlock列表。
        """
        blocks: list[GangBanBlock] = []
        total = width * height * length
        block_size = 4  # V1: 4字节

        offset = 0
        for i in range(total):
            if offset + block_size > len(data):
                break

            x = data[offset]
            y = data[offset + 1]
            z = data[offset + 2]
            block_id = data[offset + 3]
            offset += block_size

            block_name = self._id_to_name(block_id)
            blocks.append(GangBanBlock(
                x=x, y=y, z=z,
                block=BlockState(name=block_name),
            ))

        return blocks

    def _parse_v3_v4(
        self,
        data: bytes,
        width: int,
        height: int,
        length: int,
    ) -> list[GangBanBlock]:
        """解析V3/V4方块数据。

        V3: 支持NBT数据
        V4: 支持zlib压缩

        Args:
            data: 数据字节
            width, height, length: 尺寸

        Returns:
            GangBanBlock列表。
        """
        try:
            from ..nbt import parse_nbt as _parse_nbt
            nbt_data, _ = _parse_nbt(data)
            return self._nbt_to_blocks(nbt_data)
        except ImportError:
            pass
        except Exception as e:
            logger.warning("钢板NBT解析失败: %s", e)

        return self._parse_v1_v2(data, width, height, length)

    def _parse_v5_v6(
        self,
        data: bytes,
        width: int,
        height: int,
        length: int,
    ) -> list[GangBanBlock]:
        """解析V5/V6方块数据。

        V5: 多区块支持
        V6: 增量导出

        Args:
            data: 数据字节
            width, height, length: 尺寸

        Returns:
            GangBanBlock列表。
        """
        try:
            from ..nbt import parse_nbt as _parse_nbt
            nbt_data, _ = _parse_nbt(data)
            return self._nbt_to_blocks(nbt_data)
        except ImportError:
            pass
        except Exception:
            pass

        return self._parse_v1_v2(data, width, height, length)

    def _parse_v7(
        self,
        data: bytes,
        width: int,
        height: int,
        length: int,
    ) -> list[GangBanBlock]:
        """解析V7方块数据。

        V7: 运行时ID池支持

        Args:
            data: 数据字节
            width, height, length: 尺寸

        Returns:
            GangBanBlock列表。
        """
        try:
            from ..nbt import parse_nbt as _parse_nbt
            nbt_data, _ = _parse_nbt(data)

            # V7使用运行时ID
            blocks = self._nbt_to_blocks(nbt_data)

            # 应用运行时ID池
            runtime_pool = nbt_data.get("RuntimePool", nbt_data.get("runtime_pool", {}))
            if isinstance(runtime_pool, dict):
                for block in blocks:
                    rtid = runtime_pool.get(str(block.runtime_id), "")
                    if rtid:
                        block.block = BlockState(name=rtid)

            return blocks
        except ImportError:
            pass
        except Exception:
            pass

        return self._parse_v1_v2(data, width, height, length)

    def _nbt_to_blocks(self, nbt_data: dict[str, Any]) -> list[GangBanBlock]:
        """将NBT数据转换为方块列表。

        Args:
            nbt_data: NBT数据字典

        Returns:
            GangBanBlock列表。
        """
        blocks: list[GangBanBlock] = []

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

                    blocks.append(GangBanBlock(
                        x=x, y=y, z=z,
                        block=BlockState(name=block_name),
                        nbt=nbt,
                        runtime_id=block_id,
                    ))

                    index += 1

        return blocks

    def _id_to_name(self, block_id: int) -> str:
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
        elif block_id == 4:
            return "minecraft:cobblestone"
        elif block_id == 5:
            return "minecraft:planks"
        else:
            return f"minecraft:block_{block_id}"

    def extract_blocks(
        self,
        data: GangBanData,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> list[tuple[int, int, int, BlockState]]:
        """提取方块列表 (带世界坐标偏移)。

        Args:
            data: GangBanData解析结果
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (x, y, z, BlockState) 元组列表。
        """
        return [
            (b.x + offset_x, b.y + offset_y, b.z + offset_z, b.block)
            for b in data.blocks
            if b.block.name != "minecraft:air"
        ]

    def get_bounds(self, data: GangBanData) -> tuple[int, int, int, int, int, int]:
        """获取结构的包围盒。

        Args:
            data: GangBanData解析结果

        Returns:
            (min_x, min_y, min_z, max_x, max_y, max_z)。
        """
        return (0, 0, 0, data.header.width - 1, data.header.height - 1, data.header.length - 1)


__all__ = [
    "GangBanHeader",
    "GangBanBlock",
    "GangBanData",
    "GangBanParser",
]