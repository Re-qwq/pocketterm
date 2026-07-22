"""Varint 编码/解码 — Minecraft Bedrock 协议使用的变长整数编码。

Bedrock 协议使用两种 Varint:
    - **Varint32/Varuint32**: 每字节 7 位有效位, 最高位为 continuation flag
    - **Varint64/Varuint64**: 同上, 但 64 位

逆向来源: neomega `minecraft/protocol/varint.go` + gophertunnel
"""

from __future__ import annotations

import struct
from typing import BinaryIO

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: Varint32 的最大字节数 (5)
MAX_VARINT32_SIZE: int = 5

#: Varint64 的最大字节数 (10)
MAX_VARINT64_SIZE: int = 10

#: Varint32 的最大值
MAX_VARINT32: int = (1 << 31) - 1

#: Varuint32 的最大值
MAX_VARUINT32: int = (1 << 32) - 1

#: Varint64 的最大值
MAX_VARINT64: int = (1 << 63) - 1

#: Varuint64 的最大值
MAX_VARUINT64: int = (1 << 64) - 1

#: 续传标志位 (最高位为 1 表示后续还有字节)
_CONTINUATION_FLAG: int = 0x80

#: 有效数据位掩码 (低 7 位)
_DATA_MASK: int = 0x7F


# ----------------------------------------------------------------------
# 编码函数
# ----------------------------------------------------------------------

def encode_varuint32(value: int) -> bytes:
    """编码无符号 32 位 Varint。

    Args:
        value: 要编码的无符号整数 (0 ~ 2^32-1)。

    Returns:
        编码后的字节串 (1~5 字节)。

    Raises:
        ValueError: 值超出范围。
    """
    if value < 0:
        raise ValueError(f"Varuint32 不能为负数: {value}")
    if value > MAX_VARUINT32:
        raise ValueError(f"Varuint32 溢出: {value} > {MAX_VARUINT32}")

    buf = bytearray()
    while value >= _CONTINUATION_FLAG:
        buf.append((value & _DATA_MASK) | _CONTINUATION_FLAG)
        value >>= 7
    buf.append(value & _DATA_MASK)
    return bytes(buf)


def encode_varint32(value: int) -> bytes:
    """编码有符号 32 位 Varint (ZigZag 编码)。

    ZigZag 编码将负数映射为正数:
        0 -> 0, -1 -> 1, 1 -> 2, -2 -> 3, 2 -> 4, ...

    Args:
        value: 要编码的有符号整数 (-2^31 ~ 2^31-1)。

    Returns:
        编码后的字节串 (1~5 字节)。
    """
    if value < -MAX_VARINT32 - 1 or value > MAX_VARINT32:
        raise ValueError(f"Varint32 溢出: {value}")
    # ZigZag 编码: (n << 1) ^ (n >> 31)
    zigzag = (value << 1) ^ (value >> 31)
    return encode_varuint32(zigzag & MAX_VARUINT32)


def encode_varuint64(value: int) -> bytes:
    """编码无符号 64 位 Varint。

    Args:
        value: 要编码的无符号整数 (0 ~ 2^64-1)。

    Returns:
        编码后的字节串 (1~10 字节)。
    """
    if value < 0:
        raise ValueError(f"Varuint64 不能为负数: {value}")
    if value > MAX_VARUINT64:
        raise ValueError(f"Varuint64 溢出: {value} > {MAX_VARUINT64}")

    buf = bytearray()
    while value >= _CONTINUATION_FLAG:
        buf.append((value & _DATA_MASK) | _CONTINUATION_FLAG)
        value >>= 7
    buf.append(value & _DATA_MASK)
    return bytes(buf)


def encode_varint64(value: int) -> bytes:
    """编码有符号 64 位 Varint (ZigZag 编码)。

    Args:
        value: 要编码的有符号整数 (-2^63 ~ 2^63-1)。

    Returns:
        编码后的字节串 (1~10 字节)。
    """
    if value < -MAX_VARINT64 - 1 or value > MAX_VARINT64:
        raise ValueError(f"Varint64 溢出: {value}")
    # ZigZag 编码: (n << 1) ^ (n >> 63)
    zigzag = (value << 1) ^ (value >> 63)
    return encode_varuint64(zigzag & MAX_VARUINT64)


# ----------------------------------------------------------------------
# 解码函数
# ----------------------------------------------------------------------

def decode_varuint32(data: bytes, offset: int = 0) -> tuple[int, int]:
    """解码无符号 32 位 Varint。

    Args:
        data: 包含 Varint 的字节串。
        offset: 起始偏移量。

    Returns:
        ``(value, new_offset)`` — 解码后的值和新偏移量。

    Raises:
        ValueError: 数据不合法或超出最大字节数。
    """
    value = 0
    shift = 0
    pos = offset

    for i in range(MAX_VARINT32_SIZE):
        if pos >= len(data):
            raise ValueError(
                f"Varuint32 数据不完整: offset={offset}, pos={pos}, "
                f"data_len={len(data)}"
            )
        byte = data[pos]
        pos += 1
        # 第 5 字节 (索引 4) 只有低 4 位有效 (32 - 4*7 = 4):
        #   - bit 7 (0x80): continuation flag 不应置位 (否则有第 6 字节)
        #   - bits 4-6 (0x70): 数据位会溢出 32 位范围
        # 高 4 位 (0xF0) 任一置位均视为溢出, 抛 ValueError 防止静默截断
        if i == 4 and (byte & 0xF0):
            raise ValueError(
                f"Varuint32 第{i+1}字节溢出: 0x{byte:02x} "
                "(有效数据位不超过4位)"
            )
        value |= (byte & _DATA_MASK) << shift
        if (byte & _CONTINUATION_FLAG) == 0:
            return value & MAX_VARUINT32, pos
        shift += 7

    raise ValueError(
        f"Varuint32 超过最大字节数 ({MAX_VARINT32_SIZE}): offset={offset}"
    )


def decode_varint32(data: bytes, offset: int = 0) -> tuple[int, int]:
    """解码有符号 32 位 Varint (ZigZag 解码)。

    Args:
        data: 包含 Varint 的字节串。
        offset: 起始偏移量。

    Returns:
        ``(value, new_offset)`` — 解码后的有符号整数和新偏移量。
    """
    varuint, new_offset = decode_varuint32(data, offset)
    # ZigZag 解码: (n >> 1) ^ -(n & 1)
    value = (varuint >> 1) ^ -(varuint & 1)
    return value, new_offset


def decode_varuint64(data: bytes, offset: int = 0) -> tuple[int, int]:
    """解码无符号 64 位 Varint。

    Args:
        data: 包含 Varint 的字节串。
        offset: 起始偏移量。

    Returns:
        ``(value, new_offset)`` — 解码后的值和新偏移量。
    """
    value = 0
    shift = 0
    pos = offset

    for i in range(MAX_VARINT64_SIZE):
        if pos >= len(data):
            raise ValueError(
                f"Varuint64 数据不完整: offset={offset}, pos={pos}, "
                f"data_len={len(data)}"
            )
        byte = data[pos]
        pos += 1
        # 第 10 字节 (索引 9) 高 4 位必须为 0:
        #   - bit 7 (0x80): continuation flag 不应置位 (否则有第 11 字节)
        #   - bits 4-6 (0x70): 数据位会溢出 64 位范围
        # 高 4 位 (0xF0) 任一置位均视为溢出, 抛 ValueError 防止静默截断
        if i == 9 and (byte & 0xF0):
            raise ValueError(
                f"Varuint64 第{i+1}字节溢出: 0x{byte:02x} "
                "(有效数据位不超过4位)"
            )
        value |= (byte & _DATA_MASK) << shift
        if (byte & _CONTINUATION_FLAG) == 0:
            return value & MAX_VARUINT64, pos
        shift += 7

    raise ValueError(
        f"Varuint64 超过最大字节数 ({MAX_VARINT64_SIZE}): offset={offset}"
    )


def decode_varint64(data: bytes, offset: int = 0) -> tuple[int, int]:
    """解码有符号 64 位 Varint (ZigZag 解码)。

    Args:
        data: 包含 Varint 的字节串。
        offset: 起始偏移量。

    Returns:
        ``(value, new_offset)`` — 解码后的有符号整数和新偏移量。
    """
    varuint, new_offset = decode_varuint64(data, offset)
    # ZigZag 解码: (n >> 1) ^ -(n & 1)
    value = (varuint >> 1) ^ -(varuint & 1)
    return value, new_offset


# ----------------------------------------------------------------------
# 流式读写 (用于 BinaryReader/Writer)
# ----------------------------------------------------------------------

class VarIntReader:
    """从字节流中读取 Varint 的辅助类。

    用于 :class:`nbt.Reader` 和 :class:`protocol.Reader`。
    """

    def __init__(self, data: bytes, offset: int = 0) -> None:
        self.data = data
        self.offset = offset

    def read_varuint32(self) -> int:
        value, self.offset = decode_varuint32(self.data, self.offset)
        return value

    def read_varint32(self) -> int:
        value, self.offset = decode_varint32(self.data, self.offset)
        return value

    def read_varuint64(self) -> int:
        value, self.offset = decode_varuint64(self.data, self.offset)
        return value

    def read_varint64(self) -> int:
        value, self.offset = decode_varint64(self.data, self.offset)
        return value


class VarIntWriter:
    """向字节流中写入 Varint 的辅助类。

    用于 :class:`nbt.Writer` 和 :class:`protocol.Writer`。
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def write_varuint32(self, value: int) -> None:
        self.buf.extend(encode_varuint32(value))

    def write_varint32(self, value: int) -> None:
        self.buf.extend(encode_varint32(value))

    def write_varuint64(self, value: int) -> None:
        self.buf.extend(encode_varuint64(value))

    def write_varint64(self, value: int) -> None:
        self.buf.extend(encode_varint64(value))

    def get_bytes(self) -> bytes:
        return bytes(self.buf)


__all__ = [
    "MAX_VARINT32_SIZE",
    "MAX_VARINT64_SIZE",
    "MAX_VARINT32",
    "MAX_VARUINT32",
    "MAX_VARINT64",
    "MAX_VARUINT64",
    "encode_varuint32",
    "encode_varint32",
    "encode_varuint64",
    "encode_varint64",
    "decode_varuint32",
    "decode_varint32",
    "decode_varuint64",
    "decode_varint64",
    "VarIntReader",
    "VarIntWriter",
]
