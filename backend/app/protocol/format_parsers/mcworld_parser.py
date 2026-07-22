"""mcworld_parser - MCWorld 格式解析器 (Bedrock Edition 世界打包)。

逆向自 NovaBuilder 对 .mcworld 文件的支持。

MCWorld 文件格式:
    - 实际上是 ZIP 压缩包, 后缀 .mcworld
    - 内部结构:
        level.dat                   - 世界元数据 (NBT 小端序)
        level.dat_old               - 备份
        db/                         - LevelDB 数据库目录
            MANIFEST-*
            CURRENT
            *.log
            *.ldb
            *.sst
        world_behavior_packs.json   - 行为包列表
        world_resource_packs.json   - 资源包列表

NovaBuilder 主要从 .mcworld 中提取:
    - level.dat 中的世界设置
    - db/ 中的方块数据 (LevelDB 格式)
    - 用于参考或重新打包

LevelDB Key 格式 (逆向自 goleveldb + bedrock-world-operator):
    chunk_key = chunk_x (int32 LE) + chunk_z (int32 LE) + dimension (int32 LE) + type (1 byte) + subchunk_index (1 byte)
    type 47 (0x2F): SubchunkPrefix (方块数据)
    type 45 (0x2D): Data2D (高度图 + 生物群系)
    type 49 (0x31): BlockEntity (方块实体)
    type 50 (0x32): Entity (实体)
    type 51 (0x33): PendingTicks
"""

from __future__ import annotations

import json
import logging
import os
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .nbt_parser import NBTParser, NBTError

logger = logging.getLogger("pocketterm.protocol.format_parsers.mcworld_parser")

#: LevelDB Key 类型 (逆向自 bedrock-world-operator)
KEY_DATA_2D: int = 45
KEY_SUBCHUNK_PREFIX: int = 47
KEY_LEGACY_TERRAIN: int = 46
KEY_BLOCK_ENTITIES: int = 49
KEY_ENTITIES: int = 50
KEY_PENDING_TICKS: int = 51
KEY_FINALIZED_STATE: int = 54
KEY_METADATA: int = 55
KEY_VERSION: int = 60
KEY_TICK_REGISTRATION_DATA: int = 65

#: 子区块版本
SUBCHUNK_VERSION_INITIAL: int = 0
SUBCHUNK_VERSION_PALETTE_ONLY: int = 1
SUBCHUNK_VERSION_PALETTE_AND_INDEX: int = 8
SUBCHUNK_VERSION_PALETTE_HASH: int = 9

#: 方块存储格式
BLOCK_STORAGE_TYPE_DATALESS: int = 0
BLOCK_STORAGE_TYPE_VARINT: int = 1
BLOCK_STORAGE_TYPE_PALETTED: int = 2
BLOCK_STORAGE_TYPE_SIZE_CONSCIOUS: int = 3
BLOCK_STORAGE_TYPE_VOID: int = 255


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class SubChunkBlock:
    """子区块中的方块。"""
    name: str = "minecraft:air"
    states: dict[str, Any] = field(default_factory=dict)
    version: int = 0


@dataclass
class SubChunkData:
    """Minecraft Bedrock 子区块数据 (16x16x16)。

    逆向自 bedrock-world-operator/subchunk。
    """
    chunk_x: int = 0
    chunk_z: int = 0
    dimension: int = 0
    index: int = 0
    version: int = 0
    layers: list[list[SubChunkBlock]] = field(default_factory=list)  # 每个 layer 4096 个方块
    block_palette_hashes: list[int] = field(default_factory=list)


@dataclass
class LevelData:
    """level.dat 数据 (世界元数据)。

    逆向自 bedrock-world-operator/level.dat。
    """
    version: int = 0
    seed: int = 0
    spawn_x: int = 0
    spawn_y: int = 0
    spawn_z: int = 0
    world_name: str = ""
    storage_version: int = 0
    game_type: int = 0
    difficulty: int = 0
    time: int = 0
    generator: int = 0
    abilities: dict[str, Any] = field(default_factory=dict)
    experiments: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCWorldData:
    """MCWorld 文件数据。"""
    level_data: LevelData = field(default_factory=LevelData)
    behavior_packs: list[dict[str, Any]] = field(default_factory=list)
    resource_packs: list[dict[str, Any]] = field(default_factory=list)
    subchunks: list[SubChunkData] = field(default_factory=list)
    block_entities: list[dict[str, Any]] = field(default_factory=list)
    entities: list[dict[str, Any]] = field(default_factory=list)
    file_list: list[str] = field(default_factory=list)
    world_directory: str = ""

    @property
    def subchunk_count(self) -> int:
        return len(self.subchunks)


# -------------------------------------------------------------------- #
# 解析器
# -------------------------------------------------------------------- #


class MCWorldParser:
    """MCWorld 文件解析器 (逆向自 NovaBuilder + bedrock-world-operator)。

    使用方式:
        parser = MCWorldParser()
        data = parser.parse_file("example.mcworld")
        print(f"World: {data.level_data.world_name}, seed: {data.level_data.seed}")
        print(f"Subchunks: {data.subchunk_count}")
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger("pocketterm.protocol.format_parsers.mcworld_parser.parser")
        self._nbt_parser = NBTParser()

    def parse_file(self, path: str, extract_dir: Optional[str] = None) -> MCWorldData:
        """解析 .mcworld 文件。

        Args:
            path: .mcworld 文件路径 (实际为 ZIP)
            extract_dir: 提取目录 (None 表示不提取)

        Returns:
            MCWorldData: 解析后的数据
        """
        self.logger.info("Parsing mcworld file: %s", path)

        if not os.path.isfile(path):
            raise FileNotFoundError(f"File not found: {path}")

        try:
            with zipfile.ZipFile(path, "r") as zf:
                return self._parse_zip(zf, extract_dir)
        except zipfile.BadZipFile as e:
            raise NBTError(f"Invalid mcworld file (not a ZIP): {e}") from e

    def parse_directory(self, path: str) -> MCWorldData:
        """解析已解压的世界目录。"""
        self.logger.info("Parsing world directory: %s", path)
        result = MCWorldData()
        result.world_directory = path

        # level.dat
        level_dat = os.path.join(path, "level.dat")
        if os.path.isfile(level_dat):
            with open(level_dat, "rb") as f:
                result.level_data = self._parse_level_dat(f.read())

        # 行为包/资源包列表
        for packs_file, packs_list in (
            ("world_behavior_packs.json", result.behavior_packs),
            ("world_resource_packs.json", result.resource_packs),
        ):
            packs_path = os.path.join(path, packs_file)
            if os.path.isfile(packs_path):
                try:
                    with open(packs_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, list):
                        packs_list.extend(data)
                except (OSError, json.JSONDecodeError) as e:
                    self.logger.warning("Failed to parse %s: %s", packs_file, e)

        # db/ 目录 (LevelDB)
        db_dir = os.path.join(path, "db")
        if os.path.isdir(db_dir):
            self._parse_leveldb_directory(db_dir, result)

        return result

    def _parse_zip(self, zf: zipfile.ZipFile, extract_dir: Optional[str]) -> MCWorldData:
        """解析 ZIP 内容。"""
        result = MCWorldData()
        result.file_list = zf.namelist()

        # 提取所有文件 (如果指定了 extract_dir)
        if extract_dir:
            self.logger.info("Extracting to: %s", extract_dir)
            zf.extractall(extract_dir)
            result.world_directory = extract_dir

        # level.dat
        if "level.dat" in zf.namelist():
            try:
                with zf.open("level.dat") as f:
                    result.level_data = self._parse_level_dat(f.read())
            except Exception as e:
                self.logger.warning("Failed to parse level.dat: %s", e)

        # 行为包/资源包列表
        for packs_file, packs_list in (
            ("world_behavior_packs.json", result.behavior_packs),
            ("world_resource_packs.json", result.resource_packs),
        ):
            if packs_file in zf.namelist():
                try:
                    with zf.open(packs_file) as f:
                        data = json.loads(f.read().decode("utf-8"))
                    if isinstance(data, list):
                        packs_list.extend(data)
                except Exception as e:
                    self.logger.warning("Failed to parse %s: %s", packs_file, e)

        # db/ 目录 (LevelDB)
        db_files = [name for name in zf.namelist() if name.startswith("db/")]
        if db_files:
            self._parse_leveldb_zip(zf, db_files, result)

        return result

    def _parse_level_dat(self, data: bytes) -> LevelData:
        """解析 level.dat (NBT 小端序)。"""
        result = LevelData()
        try:
            nbt = self._nbt_parser.parse_bytes(data, encoding="little")
            level_data = nbt.get("Data", nbt)
            result.raw = level_data if isinstance(level_data, dict) else {}
            result.version = int(level_data.get("StorageVersion", 0))
            result.seed = int(level_data.get("RandomSeed", 0))
            result.spawn_x = int(level_data.get("SpawnX", 0))
            result.spawn_y = int(level_data.get("SpawnY", 0))
            result.spawn_z = int(level_data.get("SpawnZ", 0))
            result.world_name = str(level_data.get("LevelName", ""))
            result.game_type = int(level_data.get("GameType", 0))
            result.difficulty = int(level_data.get("Difficulty", 0))
            result.time = int(level_data.get("Time", 0))
            result.generator = int(level_data.get("Generator", 0))
            result.abilities = level_data.get("abilities", {})
            result.experiments = level_data.get("experiments", {})
        except Exception as e:
            self.logger.warning("Failed to parse level.dat NBT: %s", e)

        self.logger.info(
            "Level data: name=%r, seed=%d, spawn=(%d,%d,%d)",
            result.world_name, result.seed,
            result.spawn_x, result.spawn_y, result.spawn_z,
        )
        return result

    def _parse_leveldb_zip(
        self, zf: zipfile.ZipFile, db_files: list[str], result: MCWorldData
    ) -> None:
        """从 ZIP 中解析 LevelDB 数据。

        注意: 完整的 LevelDB 读取需要 RocksDB/LevelDB 引擎,
        这里只解析 manifest 和提取的 .ldb 文件元信息。
        实际方块数据需要使用 goleveldb 库。
        """
        self.logger.info("Found %d db files", len(db_files))

        # 提取 MANIFEST 信息
        for name in db_files:
            if "MANIFEST" in name:
                try:
                    with zf.open(name) as f:
                        manifest_data = f.read()
                    self.logger.debug("MANIFEST %s: %d bytes", name, len(manifest_data))
                except Exception as e:
                    self.logger.warning("Failed to read %s: %s", name, e)

        # 提取 CURRENT
        if "db/CURRENT" in db_files:
            try:
                with zf.open("db/CURRENT") as f:
                    current = f.read().decode("utf-8", errors="replace").strip()
                self.logger.debug("CURRENT: %s", current)
            except Exception as e:
                self.logger.warning("Failed to read db/CURRENT: %s", e)

    def _parse_leveldb_directory(self, db_dir: str, result: MCWorldData) -> None:
        """从目录解析 LevelDB 数据。"""
        # 完整 LevelDB 读取需要专用库, 这里只列出文件
        for name in os.listdir(db_dir):
            full_path = os.path.join(db_dir, name)
            if os.path.isfile(full_path):
                self.logger.debug("db file: %s (%d bytes)",
                                  name, os.path.getsize(full_path))

    def parse_chunk_key(self, key: bytes) -> dict[str, Any]:
        """解析 LevelDB 的 chunk key。

        逆向自 bedrock-world-operator/key:
            chunk_key = chunk_x (int32 LE)
                      + chunk_z (int32 LE)
                      + dimension (int32 LE)
                      + type (1 byte)
                      + subchunk_index (1 byte, 仅对 SubchunkPrefix)

        Args:
            key: LevelDB key 字节

        Returns:
            包含 chunk_x, chunk_z, dimension, type, subchunk_index 的字典
        """
        if len(key) < 9:
            return {"_error": "key too short", "length": len(key)}

        chunk_x = struct.unpack("<i", key[0:4])[0]
        chunk_z = struct.unpack("<i", key[4:8])[0]

        if len(key) >= 12:
            # 含 dimension
            dimension = struct.unpack("<i", key[8:12])[0]
            type_byte = key[12] if len(key) >= 13 else 0
            subchunk_index = key[13] if len(key) >= 14 else 0
        else:
            dimension = 0
            type_byte = key[8] if len(key) >= 9 else 0
            subchunk_index = key[9] if len(key) >= 10 else 0

        return {
            "chunk_x": chunk_x,
            "chunk_z": chunk_z,
            "dimension": dimension,
            "type": type_byte,
            "type_name": self._key_type_name(type_byte),
            "subchunk_index": subchunk_index,
        }

    def _key_type_name(self, type_byte: int) -> str:
        """获取 LevelDB key type 名称。"""
        names = {
            KEY_DATA_2D: "Data2D",
            KEY_SUBCHUNK_PREFIX: "SubchunkPrefix",
            KEY_LEGACY_TERRAIN: "LegacyTerrain",
            KEY_BLOCK_ENTITIES: "BlockEntities",
            KEY_ENTITIES: "Entities",
            KEY_PENDING_TICKS: "PendingTicks",
            KEY_FINALIZED_STATE: "FinalizedState",
            KEY_METADATA: "Metadata",
            KEY_VERSION: "Version",
            KEY_TICK_REGISTRATION_DATA: "TickRegistrationData",
        }
        return names.get(type_byte, f"Unknown({type_byte})")

    def parse_subchunk(self, data: bytes, chunk_x: int, chunk_z: int, index: int) -> SubChunkData:
        """解析子区块数据。

        逆向自 bedrock-world-operator/subchunk SubChunk.Unmarshal:
            版本字节
            层数 (1 byte)
            每层:
                BlockStorage (1 byte version + palette + indices)
        """
        result = SubChunkData(
            chunk_x=chunk_x,
            chunk_z=chunk_z,
            index=index,
        )

        if not data:
            return result

        pos = 0
        # 版本 (1 byte)
        result.version = data[pos]
        pos += 1

        if result.version >= SUBCHUNK_VERSION_PALETTE_AND_INDEX:
            # 跳过 index byte
            if pos < len(data):
                pos += 1

        # 层数
        if pos >= len(data):
            return result
        layer_count = data[pos]
        pos += 1

        for _ in range(layer_count):
            layer_blocks, pos = self._parse_block_storage(data, pos)
            result.layers.append(layer_blocks)

        return result

    def _parse_block_storage(self, data: bytes, pos: int) -> tuple[list[SubChunkBlock], int]:
        """解析 BlockStorage。

        逆向自 bedrock-world-operator/block_storage BlockStorage.Unmarshal:
            version (1 byte)
            如果 version == BLOCK_STORAGE_TYPE_DATALESS:
                block_name (varint length + string)
            如果 version == BLOCK_STORAGE_TYPE_PALETTED:
                bits_per_block (1 byte)
                block_indices (bits_per_block * 4096 bits)
                palette_size (varint32)
                palette (palette_size * SubChunkBlock)
            如果 version == BLOCK_STORAGE_TYPE_VARINT:
                bits_per_block == 32 (每个方块用 varint32 编码)
        """
        if pos >= len(data):
            return [], pos

        version = data[pos]
        pos += 1

        blocks: list[SubChunkBlock] = []

        if version == BLOCK_STORAGE_TYPE_DATALESS:
            # 整个 storage 是单一方块类型
            if pos < len(data):
                name_len = data[pos]
                pos += 1
                name = data[pos:pos + name_len].decode("utf-8", errors="replace")
                pos += name_len
                blocks = [SubChunkBlock(name=name)] * 4096
        elif version == BLOCK_STORAGE_TYPE_PALETTED:
            if pos >= len(data):
                return [], pos
            bits_per_block = data[pos]
            pos += 1

            # block_indices: bits_per_block * 4096 bits = bits_per_block * 512 bytes
            if bits_per_block == 0:
                indices_size = 0
            else:
                indices_size = (bits_per_block * 4096 + 7) // 8
            if pos + indices_size > len(data):
                return [], pos
            indices_data = data[pos:pos + indices_size]
            pos += indices_size

            # palette_size (varint32)
            palette_size, pos = self._read_varuint32(data, pos)

            # palette
            palette: list[SubChunkBlock] = []
            for _ in range(palette_size):
                block, pos = self._parse_palette_block(data, pos)
                palette.append(block)

            # 解码 4096 个方块
            if bits_per_block > 0 and palette:
                mask = (1 << bits_per_block) - 1
                for i in range(4096):
                    bit_offset = i * bits_per_block
                    byte_offset = bit_offset // 8
                    if byte_offset + (bits_per_block + 7) // 8 > len(indices_data):
                        break
                    # 读取 bits_per_block 位
                    value = 0
                    for b in range(bits_per_block):
                        bit_pos = bit_offset + b
                        byte_pos = bit_pos // 8
                        bit_in_byte = bit_pos % 8
                        if byte_pos < len(indices_data):
                            if indices_data[byte_pos] & (1 << bit_in_byte):
                                value |= (1 << b)
                    value &= mask
                    if value < len(palette):
                        blocks.append(palette[value])
                    else:
                        blocks.append(SubChunkBlock())
        elif version == BLOCK_STORAGE_TYPE_VARINT:
            # varint 编码, 每个方块占 4 字节
            for _ in range(4096):
                if pos + 4 > len(data):
                    break
                block_hash = struct.unpack("<I", data[pos:pos + 4])[0]
                pos += 4
                blocks.append(SubChunkBlock(
                    name=f"minecraft:block_{block_hash}",
                    version=block_hash,
                ))

        return blocks, pos

    def _parse_palette_block(self, data: bytes, pos: int) -> tuple[SubChunkBlock, int]:
        """解析 palette 中的方块。"""
        if pos >= len(data):
            return SubChunkBlock(), pos

        # name (varint length + string)
        name_len, pos = self._read_varuint32(data, pos)
        if pos + name_len > len(data):
            return SubChunkBlock(), pos
        name = data[pos:pos + name_len].decode("utf-8", errors="replace")
        pos += name_len

        # states (varuint count + NBT)
        states_count, pos = self._read_varuint32(data, pos)
        states: dict[str, Any] = {}
        for _ in range(states_count):
            state_name_len, pos = self._read_varuint32(data, pos)
            if pos + state_name_len > len(data):
                break
            state_name = data[pos:pos + state_name_len].decode("utf-8", errors="replace")
            pos += state_name_len
            state_value, pos = self._read_varuint32(data, pos)
            states[state_name] = state_value

        # version (varuint32)
        version, pos = self._read_varuint32(data, pos)

        return SubChunkBlock(
            name=name,
            states=states,
            version=version,
        ), pos

    def _read_varuint32(self, data: bytes, pos: int) -> tuple[int, int]:
        """读取 varuint32。"""
        result = 0
        shift = 0
        while pos < len(data):
            byte = data[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                break
            shift += 7
            if shift >= 32:
                break
        return result, pos

    def iter_blocks(self, data: MCWorldData) -> Iterator[dict[str, Any]]:
        """迭代世界中的所有方块 (来自所有子区块)。"""
        for subchunk in data.subchunks:
            for layer_idx, layer in enumerate(subchunk.layers):
                for block_idx, block in enumerate(layer):
                    if block.name == "minecraft:air":
                        continue
                    # 子区块内坐标: 16x16x16
                    x = block_idx % 16
                    z = (block_idx // 16) % 16
                    y = block_idx // 256
                    yield {
                        "position": (
                            subchunk.chunk_x * 16 + x,
                            subchunk.index * 16 + y,
                            subchunk.chunk_z * 16 + z,
                        ),
                        "block_name": block.name,
                        "block_states": block.states,
                        "block_version": block.version,
                        "layer": layer_idx,
                    }
