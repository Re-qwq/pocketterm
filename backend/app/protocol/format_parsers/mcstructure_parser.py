"""mcstructure_parser - MCStructure 格式解析器。

逆向自 NovaBuilder 对 .mcstructure 文件的支持, 来源:
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/mcstructure/main.go
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/mcstructure/...

MCStructure 文件格式 (Bedrock Edition 原生结构格式):
    - 编码: NBT 小端序 (Bedrock)
    - 根标签: 无名 TAG_Compound
    - 结构:

        TAG_Compound
            format_version     int32         (= 1)
            size               list[int32]   [x, y, z]
            structure_world_origin list[int32]  [x, y, z]  (可选)
            block_position     list[int32]   [x, y, z]      (可选)
            structure          TAG_Compound
                block_indices   list[list[varint32]]  (3层, 索引到 palette)
                entities        list[TAG_Compound]
                palette         TAG_Compound
                    default     TAG_Compound
                        block_palette      list[TAG_Compound]
                            - name        string
                            - states       TAG_Compound
                            - version      int32
                        block_position_data TAG_Compound (可选, 含方块实体 NBT)
            structure_entity_data  list[TAG_Compound]  (可选)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .nbt_parser import NBTParser, NBTError

logger = logging.getLogger("pocketterm.protocol.format_parsers.mcstructure_parser")

#: MCStructure 格式版本
DEFAULT_FORMAT_VERSION: int = 1

#: Block palette 层索引 (逆向自 mcstructure/main.go)
LAYER_DEFAULT: int = 0
LAYER_WATERLOGGED: int = 1
LAYER_COUNT: int = 3


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class MCStructureBlock:
    """MCStructure 方块 (逆向自 mcstructure/main.go)。"""
    name: str = "minecraft:air"
    states: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    position: tuple[int, int, int] = (0, 0, 0)
    nbt: dict[str, Any] = field(default_factory=dict)
    has_nbt: bool = False


@dataclass
class MCStructureEntity:
    """MCStructure 实体 (逆向自 mcstructure/main.go)。"""
    nbt: dict[str, Any] = field(default_factory=dict)
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass
class MCStructureData:
    """MCStructure 文件数据。"""
    format_version: int = DEFAULT_FORMAT_VERSION
    size: tuple[int, int, int] = (0, 0, 0)
    world_origin: tuple[int, int, int] = (0, 0, 0)
    has_origin: bool = False
    block_palette: list[MCStructureBlock] = field(default_factory=list)
    block_indices: list[list[list[int]]] = field(default_factory=list)
    entities: list[MCStructureEntity] = field(default_factory=list)
    block_position_data: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def total_blocks(self) -> int:
        return self.size[0] * self.size[1] * self.size[2]

    def get_block_at(
        self, x: int, y: int, z: int, layer: int = LAYER_DEFAULT
    ) -> MCStructureBlock:
        """获取指定位置指定层的方块。

        坐标顺序 (逆向自 Bedrock Edition):
            index = (x * size_y + y) * size_z + z  (size = (sx, sy, sz))

        注意: Bedrock 的存储顺序与 Java 不同。
        """
        sx, sy, sz = self.size
        if not (0 <= x < sx and 0 <= y < sy and 0 <= z < sz):
            raise IndexError(f"Position ({x}, {y}, {z}) out of bounds ({sx}, {sy}, {sz})")

        index = (x * sy + y) * sz + z

        if layer >= len(self.block_indices):
            return MCStructureBlock()

        layer_data = self.block_indices[layer]
        if index >= len(layer_data):
            return MCStructureBlock()

        palette_index = layer_data[index]
        if palette_index == -1 or palette_index >= len(self.block_palette):
            return MCStructureBlock(name="minecraft:air")

        block = self.block_palette[palette_index]
        # 检查是否有方块实体 NBT
        if index in self.block_position_data:
            block_nbt = self.block_position_data[index]
            if "block_entity_data" in block_nbt:
                block.nbt = block_nbt["block_entity_data"]
                block.has_nbt = True

        block.position = (x, y, z)
        return block


# -------------------------------------------------------------------- #
# 解析器
# -------------------------------------------------------------------- #


class MCStructureParser:
    """MCStructure 文件解析器 (逆向自 NovaBuilder + mcstructure/main.go)。

    使用方式:
        parser = MCStructureParser()
        data = parser.parse_file("example.mcstructure")
        for y in range(data.size[1]):
            for z in range(data.size[2]):
                for x in range(data.size[0]):
                    block = data.get_block_at(x, y, z)
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger("pocketterm.protocol.format_parsers.mcstructure_parser.parser")
        self._nbt_parser = NBTParser()

    def parse_file(self, path: str) -> MCStructureData:
        """解析 .mcstructure 文件。"""
        self.logger.info("Parsing mcstructure file: %s", path)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as e:
            raise NBTError(f"Failed to open file: {e}") from e

        return self.parse_bytes(raw)

    def parse_bytes(self, data: bytes) -> MCStructureData:
        """解析 .mcstructure 字节。"""
        if not data:
            raise NBTError("Empty mcstructure data")

        # MCStructure 使用小端序 NBT (Bedrock)
        nbt_data = self._nbt_parser.parse_bytes(data, encoding="little")
        return self._parse_structure(nbt_data)

    def _parse_structure(self, root: dict[str, Any]) -> MCStructureData:
        """解析结构根节点。"""
        result = MCStructureData()
        result.format_version = int(root.get("format_version", DEFAULT_FORMAT_VERSION))

        # size (list of 3 int32)
        size = root.get("size", [])
        if isinstance(size, list) and len(size) >= 3:
            result.size = (int(size[0]), int(size[1]), int(size[2]))

        # structure_world_origin (可选)
        origin = root.get("structure_world_origin")
        if origin is None:
            origin = root.get("block_position")
        if isinstance(origin, list) and len(origin) >= 3:
            result.world_origin = (int(origin[0]), int(origin[1]), int(origin[2]))
            result.has_origin = True

        # structure (TAG_Compound)
        structure = root.get("structure", {})
        if not isinstance(structure, dict):
            structure = {}

        # block_indices (list of list of varint32)
        block_indices = structure.get("block_indices", [])
        if isinstance(block_indices, list):
            result.block_indices = []
            for layer_data in block_indices:
                if isinstance(layer_data, (bytes, list)):
                    raw_bytes = bytes(layer_data) if isinstance(layer_data, list) else layer_data
                    indices = self._decode_varint_indices(raw_bytes)
                    result.block_indices.append(indices)
                elif isinstance(layer_data, list):
                    result.block_indices.append([int(x) for x in layer_data])
                else:
                    result.block_indices.append([])

        # palette
        palette = structure.get("palette", {})
        if not isinstance(palette, dict):
            palette = {}

        default_palette = palette.get("default", {})
        if not isinstance(default_palette, dict):
            default_palette = {}

        block_palette = default_palette.get("block_palette", [])
        if isinstance(block_palette, list):
            for block_nbt in block_palette:
                if isinstance(block_nbt, dict):
                    result.block_palette.append(self._parse_block(block_nbt))

        # block_position_data (含方块实体 NBT)
        block_position_data = default_palette.get("block_position_data", {})
        if isinstance(block_position_data, dict):
            for pos_str, data in block_position_data.items():
                if isinstance(data, dict):
                    result.block_position_data[int(pos_str)] = data

        # entities
        entities = structure.get("entities", [])
        if isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, dict):
                    pos = ent.get("Pos", ent.get("Internal", {}).get("Pos", [0.0, 0.0, 0.0]))
                    if isinstance(pos, list) and len(pos) >= 3:
                        pos_tuple = (float(pos[0]), float(pos[1]), float(pos[2]))
                    else:
                        pos_tuple = (0.0, 0.0, 0.0)
                    result.entities.append(MCStructureEntity(
                        nbt=ent,
                        position=pos_tuple,
                    ))

        # structure_entity_data (顶层, 可选)
        struct_entity_data = root.get("structure_entity_data", [])
        if isinstance(struct_entity_data, list) and not result.entities:
            for ent in struct_entity_data:
                if isinstance(ent, dict):
                    result.entities.append(MCStructureEntity(nbt=ent))

        self.logger.info(
            "MCStructure parsed: size=%s, palette=%d blocks, %d entities",
            result.size, len(result.block_palette), len(result.entities),
        )
        return result

    def _parse_block(self, block_nbt: dict[str, Any]) -> MCStructureBlock:
        """解析方块 palette 中的方块。"""
        name = str(block_nbt.get("name", "minecraft:air"))
        states = block_nbt.get("states", {})
        if not isinstance(states, dict):
            states = {}
        version = int(block_nbt.get("version", 0))

        return MCStructureBlock(
            name=name,
            states=states,
            version=version,
        )

    def _decode_varint_indices(self, data: bytes) -> list[int]:
        """解码 varint 编码的索引列表。

        Bedrock 使用 zigzag varint32 编码 block_indices。
        """
        result: list[int] = []
        i = 0
        while i < len(data):
            # 读取 varint32
            value = 0
            shift = 0
            while True:
                if i >= len(data):
                    break
                byte = data[i]
                i += 1
                value |= (byte & 0x7F) << shift
                if byte & 0x80 == 0:
                    break
                shift += 7
            # zigzag 解码
            decoded = (value >> 1) ^ -(value & 1)
            result.append(decoded)
        return result

    def iter_blocks(
        self, data: MCStructureData, offset: Optional[tuple[int, int, int]] = None
    ) -> Iterator[dict[str, Any]]:
        """迭代 mcstructure 中的所有方块。

        Args:
            data: MCStructureData
            offset: 偏移量 (None 表示使用 world_origin)

        Yields:
            每个方块的字典:
                - position: (x, y, z) 绝对坐标
                - block_name: 方块名 (如 "minecraft:stone")
                - block_states: 方块状态字典
                - block_version: 方块版本
                - nbt: 方块实体 NBT (可选)
        """
        if offset is None:
            if data.has_origin:
                offset = data.world_origin
            else:
                offset = (0, 0, 0)

        sx, sy, sz = data.size
        for x in range(sx):
            for y in range(sy):
                for z in range(sz):
                    block = data.get_block_at(x, y, z, LAYER_DEFAULT)
                    yield {
                        "position": (
                            x + offset[0],
                            y + offset[1],
                            z + offset[2],
                        ),
                        "block_name": block.name,
                        "block_states": block.states,
                        "block_version": block.version,
                        "nbt": block.nbt if block.has_nbt else None,
                    }
