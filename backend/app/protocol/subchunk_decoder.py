"""NEMC Bedrock 子区块解码器 — 对应 Community-Bot 的 nemc::bedrock::chunk。

本模块实现网易 Minecraft Bedrock Edition 网络传输的子区块 (SubChunk) 数据
解析, 逆向自 ``Community_Bot.exe`` strings 中暴露的 C++ 命名空间:

- ``nemc::bedrock::ProtocolReader`` / ``ProtocolWriter`` — 协议读写器
- ``chunk::bedrock::Encoding`` — 区块编码基类
- ``chunk::bedrock::NetworkBlockPaletteEncodingImpl`` — 网络方块调色板编码
- ``chunk::bedrock::NetworkEncodingImpl`` — 网络编码实现
- ``chunk::bedrock::PaletteEncoding`` — 调色板编码
- ``chunk::bedrock::Palette`` / ``PalettedStorage`` / ``SubChunk`` — 数据结构
- ``NEMCSubChunkDecode`` — 子区块解码入口
- ``NEMCTagNBTDecode`` — NBT 解码

Bedrock 子区块格式
==================

每个子区块 (16x16x16 方块) 的网络格式为::

    u8       version           (子区块版本, 0-9; 8/9 = 绝对 Y 索引)
    u8       layer_count       (层数, 通常 1 或 2)
    repeat(layer_count):
        PalettedStorage {
            u8       bits_per_block    (每方块比特数, 0-8)
            u32[]    palette           (调色板, 数量 = 1 << bits_per_block; 0 时为 1)
            u64[]    blocks            (packed 方块索引, little-endian)
        }
    if version >= 8:
        i8       absolute_y_index  (绝对 Y 索引)

设计原则
========

- **可独立 import**: 自带最小化 NBT / varint 工具, 不强依赖
  :mod:`app.protocol.nbt` (可选复用, 缺失时回退到内置实现)。
- **不修改既有模块**: 仅新增文件。
- **双版本兼容**: 子区块格式在网易 3.8 / 3.9 (Bedrock 1.21.x) 之间一致。

逆向来源
========

- ``Community_Bot.exe`` (用户上传) — strings 分析:
  - ``nemc::bedrock::ProtocolReader`` / ``ProtocolWriter``
  - ``chunk::bedrock::NetworkBlockPaletteEncodingImpl`` /
    ``NetworkEncodingImpl`` / ``PaletteEncoding`` / ``Palette`` /
    ``PalettedStorage`` / ``SubChunk`` / ``Encoding``
  - ``NEMCSubChunkDecode`` / ``NEMCTagNBTDecode``
- PocketTerm ``access_point_go/minecraft/protocol/sub_chunk.go`` — Go 原生实现,
  作为字段格式参考。
- PocketTerm ``access_point_go/minecraft/nbt/`` — Go NBT 实现。

典型用法
========

::

    from app.protocol.subchunk_decoder import SubChunkDecoder, SubChunk

    decoder = SubChunkDecoder()
    sc = decoder.decode(raw_bytes)  # raw_bytes 来自 SubChunkPacket payload
    print(sc.version, sc.layer_count, len(sc.layers))
    block_index = sc.get_block(0, 0, 0)  # (x, y, z) within subchunk
"""

from __future__ import annotations

import io
import logging
import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Logger (用户指定命名空间 pocketterm.protocol.* )
# ---------------------------------------------------------------------------
_LOGGER_NAME: str = "pocketterm.protocol.subchunk_decoder"
logger: logging.Logger = logging.getLogger(_LOGGER_NAME)


# ===========================================================================
# 编码类型常量 (对应 Community-Bot chunk::bedrock::* 命名)
# ===========================================================================
class SubChunkEncoding(Enum):
    """子区块编码类型 (对应 Community-Bot ``chunk::bedrock::Encoding`` 体系)。

    成员的 ``value`` 是 Community-Bot strings 中暴露的 C++ 类名。
    """

    NETWORK = "NetworkEncodingImpl"
    """网络编码实现 — 标准客户端接收的格式。"""

    PALETTE = "PaletteEncoding"
    """调色板编码 — 通用调色板编码基类。"""

    NETWORK_BLOCK_PALETTE = "NetworkBlockPaletteEncodingImpl"
    """网络方块调色板编码 — 方块状态调色板专用。"""


#: Bedrock 子区块常量。
SUBCHUNK_SIZE: int = 16
"""子区块边长 (方块数)。"""

BLOCKS_PER_LAYER: int = SUBCHUNK_SIZE * SUBCHUNK_SIZE * SUBCHUNK_SIZE  # 4096
"""单层方块数 (16*16*16 = 4096)。"""


# ===========================================================================
# 内置最小化 varint / NBT 工具 (保证模块可独立 import)
# ===========================================================================
def _decode_varuint(buf: io.BytesIO) -> int:
    """解码无符号 VarInt (LEB128)。"""
    result = 0
    shift = 0
    while True:
        byte = buf.read(1)
        if not byte:
            raise EOFError("VarUInt 解码时缓冲区耗尽")
        b = byte[0]
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
        if shift > 63:
            raise ValueError("VarUInt 编码过长 (>64 bit)")
    return result


def _decode_varint_signed(buf: io.BytesIO) -> int:
    """解码有符号 VarInt (ZigZag)。"""
    zz = _decode_varuint(buf)
    return (zz >> 1) ^ -(zz & 1)


def _decode_string(buf: io.BytesIO) -> str:
    """解码 Bedrock 字符串 (VarUInt 长度前缀 + UTF-8)。"""
    length = _decode_varuint(buf)
    raw = buf.read(length)
    if len(raw) != length:
        raise EOFError("字符串解码时缓冲区耗尽")
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# 最小化 NBT 解码 (对应 Community-Bot NEMCTagNBTDecode)
# ---------------------------------------------------------------------------
#: NBT 标签类型 ID (对应 Community-Bot ``NEMCTagNBTDecode``)。
NBT_TAG_END: int = 0
NBT_TAG_BYTE: int = 1
NBT_TAG_SHORT: int = 2
NBT_TAG_INT: int = 3
NBT_TAG_LONG: int = 4
NBT_TAG_FLOAT: int = 5
NBT_TAG_DOUBLE: int = 6
NBT_TAG_BYTE_ARRAY: int = 7
NBT_TAG_STRING: int = 8
NBT_TAG_LIST: int = 9
NBT_TAG_COMPOUND: int = 10
NBT_TAG_INT_ARRAY: int = 11
NBT_TAG_LONG_ARRAY: int = 12


def _decode_nbt_payload(buf: io.BytesIO, tag_type: int) -> Any:
    """解码 NBT 标签 payload (网络字节序, little-endian)。

    对应 Community-Bot 的 ``NEMCTagNBTDecode``。本实现为 Bedrock 网络字节序
    (little-endian) 的最小化 NBT 解码, 覆盖常用标签类型。
    """
    if tag_type == NBT_TAG_END:
        return None
    if tag_type == NBT_TAG_BYTE:
        return struct.unpack("<b", buf.read(1))[0]
    if tag_type == NBT_TAG_SHORT:
        return struct.unpack("<h", buf.read(2))[0]
    if tag_type == NBT_TAG_INT:
        return struct.unpack("<i", buf.read(4))[0]
    if tag_type == NBT_TAG_LONG:
        return struct.unpack("<q", buf.read(8))[0]
    if tag_type == NBT_TAG_FLOAT:
        return struct.unpack("<f", buf.read(4))[0]
    if tag_type == NBT_TAG_DOUBLE:
        return struct.unpack("<d", buf.read(8))[0]
    if tag_type == NBT_TAG_BYTE_ARRAY:
        length = struct.unpack("<i", buf.read(4))[0]
        return buf.read(length)
    if tag_type == NBT_TAG_STRING:
        length = struct.unpack("<H", buf.read(2))[0]
        return buf.read(length).decode("utf-8", errors="replace")
    if tag_type == NBT_TAG_LIST:
        child_type = buf.read(1)[0]
        count = struct.unpack("<i", buf.read(4))[0]
        return [_decode_nbt_payload(buf, child_type) for _ in range(count)]
    if tag_type == NBT_TAG_COMPOUND:
        result: Dict[str, Any] = {}
        while True:
            child_type = buf.read(1)[0]
            if child_type == NBT_TAG_END:
                break
            name_len = struct.unpack("<H", buf.read(2))[0]
            name = buf.read(name_len).decode("utf-8", errors="replace")
            result[name] = _decode_nbt_payload(buf, child_type)
        return result
    if tag_type == NBT_TAG_INT_ARRAY:
        length = struct.unpack("<i", buf.read(4))[0]
        return list(struct.unpack(f"<{length}i", buf.read(4 * length)))
    if tag_type == NBT_TAG_LONG_ARRAY:
        length = struct.unpack("<i", buf.read(4))[0]
        return list(struct.unpack(f"<{length}q", buf.read(8 * length)))
    raise ValueError(f"未知的 NBT 标签类型: {tag_type}")


def decode_nbt(data: bytes) -> Dict[str, Any]:
    """解码完整的 NBT 复合标签 (对应 Community-Bot ``NEMCTagNBTDecode``)。

    Parameters
    ----------
    data:
        NBT 字节流 (网络字节序, 以一个 TAG_COMPOUND 头开始)。

    Returns
    -------
    dict
        解析后的 NBT 字典。

    Raises
    ------
    ValueError
        根标签不是 TAG_COMPOUND 或数据损坏时抛出。
    """
    buf = io.BytesIO(data)
    root_type = buf.read(1)[0]
    if root_type != NBT_TAG_COMPOUND:
        raise ValueError(f"NBT 根标签应为 TAG_COMPOUND(10), 实际为 {root_type}")
    # 根名称
    name_len = struct.unpack("<H", buf.read(2))[0]
    buf.read(name_len)  # 跳过根名称
    return _decode_nbt_payload(buf, NBT_TAG_COMPOUND)


# ===========================================================================
# 数据结构
# ===========================================================================
@dataclass
class PalettedStorage:
    """调色板存储 — 单层方块的压缩表示。

    对应 Community-Bot 的 ``chunk::bedrock::PalettedStorage`` /
    ``chunk::bedrock::Palette``。

    Attributes
    ----------
    bits_per_block : int
        每方块占用的比特数 (0-8)。
    palette : list[int]
        调色板 (方块状态运行时 ID 列表)。
    blocks : list[int]
        解包后的方块索引数组 (长度 4096, 每个值是 palette 的下标)。
    raw_word_count : int
        原始 packed 数据的 64 位字数。
    """

    bits_per_block: int = 0
    palette: List[int] = field(default_factory=list)
    blocks: List[int] = field(default_factory=list)
    raw_word_count: int = 0


@dataclass
class SubChunk:
    """子区块数据结构 (对应 Community-Bot ``chunk::bedrock::SubChunk``)。

    Attributes
    ----------
    version : int
        子区块版本 (0-9)。
    layer_count : int
        层数 (通常 1 主世界 / 2 下界)。
    layers : list[PalettedStorage]
        各层的调色板存储。
    absolute_y_index : Optional[int]
        绝对 Y 索引 (version >= 8 时存在)。
    encoding_type : SubChunkEncoding
        编码类型 (默认网络编码)。
    """

    version: int = 0
    layer_count: int = 0
    layers: List[PalettedStorage] = field(default_factory=list)
    absolute_y_index: Optional[int] = None
    encoding_type: SubChunkEncoding = SubChunkEncoding.NETWORK

    # ------------------------------------------------------------------
    # 方块访问
    # ------------------------------------------------------------------
    def get_block(self, x: int, y: int, z: int, layer: int = 0) -> int:
        """获取子区块内指定坐标的方块运行时 ID。

        Parameters
        ----------
        x, y, z:
            子区块内坐标 (0-15)。
        layer:
            层索引 (默认 0)。

        Returns
        -------
        int
            方块运行时 ID (来自调色板)。若坐标越界或层不存在, 返回 ``0`` (空气)。
        """
        if layer < 0 or layer >= len(self.layers):
            return 0
        if not (0 <= x < SUBCHUNK_SIZE and 0 <= y < SUBCHUNK_SIZE and 0 <= z < SUBCHUNK_SIZE):
            return 0
        storage = self.layers[layer]
        if not storage.blocks:
            return 0
        # Bedrock 方块索引顺序: x | (z << 4) | (y << 8)
        index = x | (z << 4) | (y << 8)
        if index >= len(storage.blocks):
            return 0
        palette_index = storage.blocks[index]
        if palette_index < 0 or palette_index >= len(storage.palette):
            return 0
        return storage.palette[palette_index]

    def __repr__(self) -> str:
        return (
            f"SubChunk(version={self.version}, layers={self.layer_count}, "
            f"encoding={self.encoding_type.value}, "
            f"y_index={self.absolute_y_index})"
        )


# ===========================================================================
# 解码器 (对应 Community-Bot NEMCSubChunkDecode)
# ===========================================================================
class SubChunkDecoder:
    """NEMC 子区块解码器 — 解析网络传输的子区块数据。

    对应 Community-Bot 的 ``NEMCSubChunkDecode`` 类与
    ``nemc::bedrock::ProtocolReader`` 的子区块读取逻辑。

    编码类型说明
    ------------
    - :data:`SubChunkEncoding.NETWORK`: 标准客户端格式 (本解码器默认)。
    - :data:`SubChunkEncoding.NETWORK_BLOCK_PALETTE`: 方块调色板编码
      (NETWORK 的特化, 解码逻辑相同)。
    - :data:`SubChunkEncoding.PALETTE`: 通用调色板编码 (本解码器同样处理)。
    """

    # 编码类型常量 (对应 Community-Bot chunk::bedrock::* 类名)
    ENCODING_NETWORK: str = SubChunkEncoding.NETWORK.value
    ENCODING_PALETTE: str = SubChunkEncoding.PALETTE.value
    ENCODING_BLOCK_PALETTE: str = SubChunkEncoding.NETWORK_BLOCK_PALETTE.value

    def decode(self, data: bytes) -> SubChunk:
        """解码子区块字节流。

        Parameters
        ----------
        data:
            子区块 payload (来自 ``SubChunkPacket`` 或 ``LevelChunkPacket``)。

        Returns
        -------
        SubChunk
            解析后的子区块。

        Raises
        ------
        ValueError
            数据格式不合法时抛出。
        EOFError
            数据在解码完成前耗尽时抛出。
        """
        buf = io.BytesIO(data)
        sc = SubChunk()

        # 1. 版本
        version_byte = buf.read(1)
        if not version_byte:
            raise EOFError("子区块数据为空")
        sc.version = version_byte[0]
        if sc.version > 9:
            logger.warning("子区块版本 %d 超出已知范围 (0-9)", sc.version)

        # 2. 层数
        layer_byte = buf.read(1)
        if not layer_byte:
            raise EOFError("子区块层数字段缺失")
        sc.layer_count = layer_byte[0]

        # 3. 各层 PalettedStorage
        sc.layers = []
        for layer_idx in range(sc.layer_count):
            storage = self._decode_palletted_storage(buf, layer_idx)
            sc.layers.append(storage)

        # 4. 绝对 Y 索引 (version >= 8)
        if sc.version >= 8:
            y_byte = buf.read(1)
            if y_byte:
                sc.absolute_y_index = struct.unpack("<b", y_byte)[0]

        logger.debug(
            "子区块解码完成: version=%d, layers=%d, y_index=%s",
            sc.version,
            sc.layer_count,
            sc.absolute_y_index,
        )
        return sc

    def decode_nbt(self, data: bytes) -> Dict[str, Any]:
        """解码 NBT 数据 (对应 Community-Bot ``NEMCTagNBTDecode``)。

        Parameters
        ----------
        data:
            NBT 字节流。

        Returns
        -------
        dict
            解析后的 NBT 字典。
        """
        return decode_nbt(data)

    # ------------------------------------------------------------------
    # 内部: PalettedStorage 解码
    # ------------------------------------------------------------------
    def _decode_palletted_storage(
        self, buf: io.BytesIO, layer_idx: int
    ) -> PalettedStorage:
        """解码单个 PalettedStorage (对应 Community-Bot ``PaletteEncoding``)。

        Bedrock 网络调色板格式::

            u8   bits_per_block
            if bits_per_block == 0:
                u32   single_block_runtime_id   (调色板仅 1 项, 全部方块相同)
            else:
                u32   palette_size              (VarUInt 或 u32, 依实现)
                u32[] palette                   (运行时 ID)
            u64[] packed_blocks                (little-endian, 字数 = ceil(4096 * bpb / 64))
        """
        storage = PalettedStorage()

        bpb_byte = buf.read(1)
        if not bpb_byte:
            raise EOFError(f"层 {layer_idx} 的 bits_per_block 字段缺失")
        storage.bits_per_block = bpb_byte[0]

        if storage.bits_per_block == 0:
            # 单一方块: 调色板仅 1 项, 无 packed 数据
            raw = buf.read(4)
            if len(raw) < 4:
                raise EOFError(f"层 {layer_idx} 的单方块调色板字段缺失")
            single_id = struct.unpack("<I", raw)[0]
            storage.palette = [single_id]
            storage.blocks = [0] * BLOCKS_PER_LAYER
            storage.raw_word_count = 0
            return storage

        # 调色板大小: Bedrock 网络格式使用 VarUInt
        palette_size = _decode_varuint(buf)
        storage.palette = []
        for _ in range(palette_size):
            raw = buf.read(4)
            if len(raw) < 4:
                raise EOFError(f"层 {layer_idx} 调色板项不足")
            storage.palette.append(struct.unpack("<I", raw)[0])

        # packed 方块数据: 每个 64 位字 (little-endian)
        # 字数 = ceil(4096 * bits_per_block / 64)
        word_count = (BLOCKS_PER_LAYER * storage.bits_per_block + 63) // 64
        storage.raw_word_count = word_count

        words: List[int] = []
        for _ in range(word_count):
            raw = buf.read(8)
            if len(raw) < 8:
                raise EOFError(f"层 {layer_idx} packed 方块数据不足")
            words.append(struct.unpack("<Q", raw)[0])

        # 解包方块索引
        storage.blocks = self._unpack_blocks(words, storage.bits_per_block)
        return storage

    @staticmethod
    def _unpack_blocks(words: List[int], bits_per_block: int) -> List[int]:
        """从 64 位 packed 字中解包方块索引。

        Bedrock 使用 little-endian bit packing: 方块索引按 LSB 优先排列在
        连续的 64 位字中。

        Parameters
        ----------
        words:
            64 位字列表 (little-endian)。
        bits_per_block:
            每方块比特数。

        Returns
        -------
        list[int]
            长度 4096 的方块索引数组 (palette 下标)。
        """
        blocks: List[int] = [0] * BLOCKS_PER_LAYER
        if bits_per_block == 0 or not words:
            return blocks

        mask = (1 << bits_per_block) - 1
        blocks_per_word = 64 // bits_per_block

        for word_idx, word in enumerate(words):
            base = word_idx * blocks_per_word
            if base >= BLOCKS_PER_LAYER:
                break
            for i in range(blocks_per_word):
                block_idx = base + i
                if block_idx >= BLOCKS_PER_LAYER:
                    break
                blocks[block_idx] = (word >> (i * bits_per_block)) & mask
        return blocks


__all__ = [
    # 枚举与常量
    "SubChunkEncoding",
    "SUBCHUNK_SIZE",
    "BLOCKS_PER_LAYER",
    # 数据结构
    "PalettedStorage",
    "SubChunk",
    # 解码器
    "SubChunkDecoder",
    # NBT 工具
    "decode_nbt",
]
