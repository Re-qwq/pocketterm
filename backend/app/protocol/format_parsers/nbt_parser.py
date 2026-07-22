"""nbt_parser - 通用 NBT (Named Binary Tag) 编解码器。

逆向自 NexusEgo v1.6.5 的 NBT 处理层, 来源包括:

    - StarShuttler/nbt_parser/             (主解析器)
    - WaterStructure/utils/nbt/             (WaterStructure NBT 工具)
    - WaterStructure/modules/bdump/nbt/     (BDump 内嵌 NBT)
    - nemc-tan-lobby-solver/minecraft/nbt/  (NEMC NBT)
    - WavesAccess/minecraft/nbt/            (WavesAccess NBT)

支持的 4 种字节序 (逆向自 strings: "nbt: NetworkLittleEndian"):
    - LITTLE_ENDIAN           -- 磁盘小端序 (Bedrock 默认磁盘格式)
    - BIG_ENDIAN              -- 磁盘大端序 (Java 版磁盘格式)
    - NETWORK_LITTLE_ENDIAN   -- 网络小端序 (Bedrock 协议格式)
    - NETWORK_BIG_ENDIAN      -- 网络大端序 (极少使用)

支持的 12 种 Tag 类型 (标准 NBT 规范):
    TAG_END=0, TAG_BYTE=1, TAG_SHORT=2, TAG_INT=3, TAG_LONG=4,
    TAG_FLOAT=5, TAG_DOUBLE=6, TAG_BYTE_ARRAY=7, TAG_STRING=8,
    TAG_LIST=9, TAG_COMPOUND=10, TAG_INT_ARRAY=11, TAG_LONG_ARRAY=12

NexusE NBT 特殊点:
    - 使用 "nbt: NetworkLittleEndian" 标记网络小端序
    - 支持 NEMC 专用 NBT 标签 (neteaseEncryptFlag, neteaseStrongholdSelectedChunks)
    - BDump 内嵌 NBT 使用小端序 (nbt.LittleEndian)
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Callable

logger = logging.getLogger("pocketterm.protocol.format_parsers.nbt_parser")


# -------------------------------------------------------------------- #
# Tag 类型常量 (标准 NBT 规范, 逆向自 WaterStructure/utils/nbt/tag.go)
# -------------------------------------------------------------------- #

TAG_END: int = 0
TAG_BYTE: int = 1
TAG_SHORT: int = 2
TAG_INT: int = 3
TAG_LONG: int = 4
TAG_FLOAT: int = 5
TAG_DOUBLE: int = 6
TAG_BYTE_ARRAY: int = 7
TAG_STRING: int = 8
TAG_LIST: int = 9
TAG_COMPOUND: int = 10
TAG_INT_ARRAY: int = 11
TAG_LONG_ARRAY: int = 12

#: Tag ID 到名称的映射
TAG_NAMES: dict[int, str] = {
    TAG_END: "TAG_End",
    TAG_BYTE: "TAG_Byte",
    TAG_SHORT: "TAG_Short",
    TAG_INT: "TAG_Int",
    TAG_LONG: "TAG_Long",
    TAG_FLOAT: "TAG_Float",
    TAG_DOUBLE: "TAG_Double",
    TAG_BYTE_ARRAY: "TAG_Byte_Array",
    TAG_STRING: "TAG_String",
    TAG_LIST: "TAG_List",
    TAG_COMPOUND: "TAG_Compound",
    TAG_INT_ARRAY: "TAG_Int_Array",
    TAG_LONG_ARRAY: "TAG_Long_Array",
}


# -------------------------------------------------------------------- #
# 字节序常量 (逆向自 strings: "nbt: NetworkLittleEndian" + nbt.LittleEndian)
# -------------------------------------------------------------------- #

LITTLE_ENDIAN: int = 0
BIG_ENDIAN: int = 1
NETWORK_LITTLE_ENDIAN: int = 2
NETWORK_BIG_ENDIAN: int = 3

#: 字节序名称
ENCODING_NAMES: dict[int, str] = {
    LITTLE_ENDIAN: "LittleEndian",
    BIG_ENDIAN: "BigEndian",
    NETWORK_LITTLE_ENDIAN: "NetworkLittleEndian",
    NETWORK_BIG_ENDIAN: "NetworkBigEndian",
}


# -------------------------------------------------------------------- #
# 异常类型
# -------------------------------------------------------------------- #


class NBTError(Exception):
    """NBT 编解码错误的基类。"""


class NBTFormatError(NBTError):
    """NBT 格式错误 (无效的字节序或 Tag 类型)。"""


# -------------------------------------------------------------------- #
# 包装类型 - 用于在 Python 中保留 NBT 标签类型信息
# -------------------------------------------------------------------- #


@dataclass
class Byte:
    """TAG_Byte 包装类型。"""
    value: int

    def __post_init__(self) -> None:
        if not (-128 <= self.value <= 127):
            raise NBTFormatError(f"Byte value out of range: {self.value}")


@dataclass
class Short:
    """TAG_Short 包装类型。"""
    value: int

    def __post_init__(self) -> None:
        if not (-32768 <= self.value <= 32767):
            raise NBTFormatError(f"Short value out of range: {self.value}")


@dataclass
class Int:
    """TAG_Int 包装类型。"""
    value: int


@dataclass
class Long:
    """TAG_Long 包装类型。"""
    value: int


@dataclass
class Float:
    """TAG_Float 包装类型。"""
    value: float


@dataclass
class Double:
    """TAG_Double 包装类型。"""
    value: float


@dataclass
class ByteArray:
    """TAG_Byte_Array 包装类型。"""
    value: list[int]


@dataclass
class IntArray:
    """TAG_Int_Array 包装类型。"""
    value: list[int]


@dataclass
class LongArray:
    """TAG_Long_Array 包装类型。"""
    value: list[int]


# -------------------------------------------------------------------- #
# NBT Reader
# -------------------------------------------------------------------- #


class NBTReader:
    """NBT 二进制读取器。

    逆向自 StarShuttler/nbt_parser/reader.go 和
    WaterStructure/modules/bdump/nbt/reader.go。

    支持四种字节序:
        - LITTLE_ENDIAN:           磁盘小端序
        - BIG_ENDIAN:              磁盘大端序
        - NETWORK_LITTLE_ENDIAN:   网络小端序 (Bedrock 协议)
        - NETWORK_BIG_ENDIAN:      网络大端序

    网络字节序与磁盘字节序的差异:
        - 网络字节序中字符串/数组/列表的长度使用 varint 而非固定长度整数
        - 这是 Bedrock 协议为减少数据包体积的设计
    """

    def __init__(self, data: bytes, encoding: int = LITTLE_ENDIAN) -> None:
        """初始化 NBT 读取器。

        Args:
            data: NBT 二进制数据。
            encoding: 字节序编码常量。
        """
        if encoding not in ENCODING_NAMES:
            raise NBTFormatError(f"invalid encoding: {encoding}")
        self._buf = BytesIO(data)
        self._encoding = encoding
        self._is_network = encoding in (NETWORK_LITTLE_ENDIAN, NETWORK_BIG_ENDIAN)
        self._is_big_endian = encoding in (BIG_ENDIAN, NETWORK_BIG_ENDIAN)
        logger.debug(
            "NBTReader initialized: encoding=%s, size=%d bytes",
            ENCODING_NAMES[encoding], len(data),
        )

    # ---------------- 原始读取 ---------------- #

    def read_byte(self) -> int:
        """读取 1 字节有符号整数 (TAG_Byte)。"""
        data = self._buf.read(1)
        if len(data) != 1:
            raise NBTError("unexpected EOF reading byte")
        return struct.unpack("b", data)[0]

    def read_short(self) -> int:
        """读取 2 字节有符号整数 (TAG_Short)。"""
        data = self._buf.read(2)
        if len(data) != 2:
            raise NBTError("unexpected EOF reading short")
        fmt = ">h" if self._is_big_endian else "<h"
        return struct.unpack(fmt, data)[0]

    def read_ushort(self) -> int:
        """读取 2 字节无符号整数。"""
        data = self._buf.read(2)
        if len(data) != 2:
            raise NBTError("unexpected EOF reading ushort")
        fmt = ">H" if self._is_big_endian else "<H"
        return struct.unpack(fmt, data)[0]

    def read_int(self) -> int:
        """读取 4 字节有符号整数 (TAG_Int)。"""
        if self._is_network:
            return self._read_varint32()
        data = self._buf.read(4)
        if len(data) != 4:
            raise NBTError("unexpected EOF reading int")
        fmt = ">i" if self._is_big_endian else "<i"
        return struct.unpack(fmt, data)[0]

    def read_uint32(self) -> int:
        """读取 4 字节无符号整数。"""
        if self._is_network:
            return self._read_uvarint32()
        data = self._buf.read(4)
        if len(data) != 4:
            raise NBTError("unexpected EOF reading uint32")
        fmt = ">I" if self._is_big_endian else "<I"
        return struct.unpack(fmt, data)[0]

    def read_long(self) -> int:
        """读取 8 字节有符号整数 (TAG_Long)。"""
        if self._is_network:
            return self._read_varint64()
        data = self._buf.read(8)
        if len(data) != 8:
            raise NBTError("unexpected EOF reading long")
        fmt = ">q" if self._is_big_endian else "<q"
        return struct.unpack(fmt, data)[0]

    def read_float(self) -> float:
        """读取 4 字节浮点数 (TAG_Float)。"""
        data = self._buf.read(4)
        if len(data) != 4:
            raise NBTError("unexpected EOF reading float")
        fmt = ">f" if self._is_big_endian else "<f"
        return struct.unpack(fmt, data)[0]

    def read_double(self) -> float:
        """读取 8 字节浮点数 (TAG_Double)。"""
        data = self._buf.read(8)
        if len(data) != 8:
            raise NBTError("unexpected EOF reading double")
        fmt = ">d" if self._is_big_endian else "<d"
        return struct.unpack(fmt, data)[0]

    def read_string(self) -> str:
        """读取 UTF-8 字符串 (TAG_String)。"""
        length = self.read_ushort() if not self._is_network else self._read_uvarint32()
        if length == 0:
            return ""
        data = self._buf.read(length)
        if len(data) != length:
            raise NBTError(
                f"unexpected EOF reading string: expected {length} bytes, got {len(data)}"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NBTError(f"invalid UTF-8 in string: {exc}") from exc

    def read_byte_array(self) -> list[int]:
        """读取字节数组 (TAG_Byte_Array)。"""
        length = self.read_int()
        if length < 0:
            raise NBTFormatError(f"negative byte array length: {length}")
        data = self._buf.read(length)
        if len(data) != length:
            raise NBTError("unexpected EOF reading byte array")
        return list(data)

    def read_int_array(self) -> list[int]:
        """读取整数数组 (TAG_Int_Array)。"""
        length = self.read_int()
        if length < 0:
            raise NBTFormatError(f"negative int array length: {length}")
        return [self.read_int() for _ in range(length)]

    def read_long_array(self) -> list[int]:
        """读取长整数数组 (TAG_Long_Array)。"""
        length = self.read_int()
        if length < 0:
            raise NBTFormatError(f"negative long array length: {length}")
        return [self.read_long() for _ in range(length)]

    # ---------------- varint (网络字节序) ---------------- #

    def _read_uvarint32(self) -> int:
        """读取无符号 varint32 (网络字节序专用)。"""
        result = 0
        shift = 0
        for _ in range(5):
            byte = self._buf.read(1)
            if len(byte) != 1:
                raise NBTError("unexpected EOF reading uvarint32")
            b = byte[0]
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                return result
            shift += 7
        raise NBTFormatError("uvarint32 too long")

    def _read_varint32(self) -> int:
        """读取有符号 varint32 (ZigZag 编码)。"""
        raw = self._read_uvarint32()
        # ZigZag 解码
        return (raw >> 1) ^ -(raw & 1)

    def _read_varint64(self) -> int:
        """读取有符号 varint64 (ZigZag 编码)。"""
        result = 0
        shift = 0
        for _ in range(10):
            byte = self._buf.read(1)
            if len(byte) != 1:
                raise NBTError("unexpected EOF reading varint64")
            b = byte[0]
            result |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                # ZigZag 解码
                return (result >> 1) ^ -(result & 1)
            shift += 7
        raise NBTFormatError("varint64 too long")

    # ---------------- 完整 NBT 解析 ---------------- #

    def read_tag(self, tag_id: int) -> Any:
        """根据 Tag ID 读取一个 Tag 的载荷 (不含名称)。

        Args:
            tag_id: Tag 类型 ID。

        Returns:
            Python 对象表示的 Tag 数据。
        """
        if tag_id == TAG_BYTE:
            return Byte(self.read_byte())
        if tag_id == TAG_SHORT:
            return Short(self.read_short())
        if tag_id == TAG_INT:
            return Int(self.read_int())
        if tag_id == TAG_LONG:
            return Long(self.read_long())
        if tag_id == TAG_FLOAT:
            return Float(self.read_float())
        if tag_id == TAG_DOUBLE:
            return Double(self.read_double())
        if tag_id == TAG_BYTE_ARRAY:
            return ByteArray(self.read_byte_array())
        if tag_id == TAG_STRING:
            return self.read_string()
        if tag_id == TAG_LIST:
            return self._read_list_payload()
        if tag_id == TAG_COMPOUND:
            return self._read_compound_payload()
        if tag_id == TAG_INT_ARRAY:
            return IntArray(self.read_int_array())
        if tag_id == TAG_LONG_ARRAY:
            return LongArray(self.read_long_array())
        raise NBTFormatError(f"unknown tag id: {tag_id}")

    def _read_list_payload(self) -> list[Any]:
        """读取 TAG_List 载荷。"""
        list_type = self.read_byte()
        if list_type == TAG_END:
            # 空列表: 长度仍需读取
            length = self.read_int()
            if length != 0 and length != -1:
                logger.warning(
                    "TAG_List has TAG_End type but non-zero length: %d", length
                )
            return []
        length = self.read_int()
        if length < 0:
            length = 0
        items: list[Any] = []
        for _ in range(length):
            items.append(self.read_tag(list_type))
        return items

    def _read_compound_payload(self) -> dict[str, Any]:
        """读取 TAG_Compound 载荷。"""
        result: dict[str, Any] = {}
        while True:
            tag_id = self.read_byte()
            if tag_id == TAG_END:
                break
            name = self.read_string()
            value = self.read_tag(tag_id)
            result[name] = value
        return result

    def read_root(self) -> dict[str, Any]:
        """读取根 TAG_Compound。

        NBT 格式规定根必须是一个 TAG_Compound。
        """
        root_id = self.read_byte()
        if root_id != TAG_COMPOUND:
            raise NBTFormatError(
                f"root tag must be TAG_Compound (10), got {root_id}"
            )
        root_name = self.read_string()
        payload = self._read_compound_payload()
        # 将根名称作为特殊键 "__root_name__" 存储
        if root_name:
            payload["__root_name__"] = root_name
        logger.debug("NBT root parsed: name=%r, keys=%d", root_name, len(payload))
        return payload


# -------------------------------------------------------------------- #
# NBT Writer
# -------------------------------------------------------------------- #


class NBTWriter:
    """NBT 二进制写入器。

    逆向自 StarShuttler/nbt_parser/writer.go 和
    WaterStructure/modules/bdump/nbt/writer.go。
    """

    def __init__(self, encoding: int = LITTLE_ENDIAN) -> None:
        if encoding not in ENCODING_NAMES:
            raise NBTFormatError(f"invalid encoding: {encoding}")
        self._buf = BytesIO()
        self._encoding = encoding
        self._is_network = encoding in (NETWORK_LITTLE_ENDIAN, NETWORK_BIG_ENDIAN)
        self._is_big_endian = encoding in (BIG_ENDIAN, NETWORK_BIG_ENDIAN)

    # ---------------- 原始写入 ---------------- #

    def write_byte(self, value: int) -> None:
        """写入 1 字节有符号整数。"""
        self._buf.write(struct.pack("b", value & 0xFF if value >= 0 else value))

    def write_short(self, value: int) -> None:
        """写入 2 字节有符号整数。"""
        fmt = ">h" if self._is_big_endian else "<h"
        self._buf.write(struct.pack(fmt, value))

    def write_ushort(self, value: int) -> None:
        """写入 2 字节无符号整数。"""
        fmt = ">H" if self._is_big_endian else "<H"
        self._buf.write(struct.pack(fmt, value))

    def write_int(self, value: int) -> None:
        """写入 4 字节有符号整数。"""
        if self._is_network:
            self._write_varint32(value)
            return
        fmt = ">i" if self._is_big_endian else "<i"
        self._buf.write(struct.pack(fmt, value))

    def write_uint32(self, value: int) -> None:
        """写入 4 字节无符号整数。"""
        if self._is_network:
            self._write_uvarint32(value)
            return
        fmt = ">I" if self._is_big_endian else "<I"
        self._buf.write(struct.pack(fmt, value))

    def write_long(self, value: int) -> None:
        """写入 8 字节有符号整数。"""
        if self._is_network:
            self._write_varint64(value)
            return
        fmt = ">q" if self._is_big_endian else "<q"
        self._buf.write(struct.pack(fmt, value))

    def write_float(self, value: float) -> None:
        """写入 4 字节浮点数。"""
        fmt = ">f" if self._is_big_endian else "<f"
        self._buf.write(struct.pack(fmt, value))

    def write_double(self, value: float) -> None:
        """写入 8 字节浮点数。"""
        fmt = ">d" if self._is_big_endian else "<d"
        self._buf.write(struct.pack(fmt, value))

    def write_string(self, value: str) -> None:
        """写入 UTF-8 字符串。"""
        encoded = value.encode("utf-8")
        if self._is_network:
            self._write_uvarint32(len(encoded))
        else:
            self.write_ushort(len(encoded))
        self._buf.write(encoded)

    def write_byte_array(self, value: list[int]) -> None:
        """写入字节数组。"""
        self.write_int(len(value))
        self._buf.write(bytes(v & 0xFF for v in value))

    def write_int_array(self, value: list[int]) -> None:
        """写入整数数组。"""
        self.write_int(len(value))
        for v in value:
            self.write_int(v)

    def write_long_array(self, value: list[int]) -> None:
        """写入长整数数组。"""
        self.write_int(len(value))
        for v in value:
            self.write_long(v)

    # ---------------- varint ---------------- #

    def _write_uvarint32(self, value: int) -> None:
        """写入无符号 varint32。"""
        while value >= 0x80:
            self._buf.write(bytes([(value & 0x7F) | 0x80]))
            value >>= 7
        self._buf.write(bytes([value & 0x7F]))

    def _write_varint32(self, value: int) -> None:
        """写入有符号 varint32 (ZigZag 编码)。"""
        # ZigZag 编码
        zz = (value << 1) ^ (value >> 31)
        # 处理 Python 无限精度
        zz = zz & 0xFFFFFFFF
        self._write_uvarint32(zz)

    def _write_varint64(self, value: int) -> None:
        """写入有符号 varint64 (ZigZag 编码)。"""
        zz = (value << 1) ^ (value >> 63)
        zz = zz & 0xFFFFFFFFFFFFFFFF
        while zz >= 0x80:
            self._buf.write(bytes([(zz & 0x7F) | 0x80]))
            zz >>= 7
        self._buf.write(bytes([zz & 0x7F]))

    # ---------------- 完整 NBT 写入 ---------------- #

    def write_tag(self, tag_id: int, value: Any) -> None:
        """写入一个 Tag 的载荷 (不含名称和类型 ID)。"""
        if tag_id == TAG_BYTE:
            self.write_byte(value.value if isinstance(value, Byte) else int(value))
        elif tag_id == TAG_SHORT:
            self.write_short(value.value if isinstance(value, Short) else int(value))
        elif tag_id == TAG_INT:
            self.write_int(value.value if isinstance(value, Int) else int(value))
        elif tag_id == TAG_LONG:
            self.write_long(value.value if isinstance(value, Long) else int(value))
        elif tag_id == TAG_FLOAT:
            self.write_float(value.value if isinstance(value, Float) else float(value))
        elif tag_id == TAG_DOUBLE:
            self.write_double(value.value if isinstance(value, Double) else float(value))
        elif tag_id == TAG_BYTE_ARRAY:
            arr = value.value if isinstance(value, ByteArray) else value
            self.write_byte_array(arr)
        elif tag_id == TAG_STRING:
            self.write_string(value)
        elif tag_id == TAG_LIST:
            self._write_list_payload(value)
        elif tag_id == TAG_COMPOUND:
            self._write_compound_payload(value)
        elif tag_id == TAG_INT_ARRAY:
            arr = value.value if isinstance(value, IntArray) else value
            self.write_int_array(arr)
        elif tag_id == TAG_LONG_ARRAY:
            arr = value.value if isinstance(value, LongArray) else value
            self.write_long_array(arr)
        else:
            raise NBTFormatError(f"unknown tag id: {tag_id}")

    def _write_list_payload(self, value: list[Any]) -> None:
        """写入 TAG_List 载荷。"""
        if not value:
            self.write_byte(TAG_END)
            self.write_int(0)
            return
        # 推断列表类型 (使用第一个元素的类型)
        list_type = self._infer_tag_type(value[0])
        self.write_byte(list_type)
        self.write_int(len(value))
        for item in value:
            self.write_tag(list_type, item)

    def _write_compound_payload(self, value: dict[str, Any]) -> None:
        """写入 TAG_Compound 载荷。"""
        for name, v in value.items():
            if name == "__root_name__":
                continue
            tag_id = self._infer_tag_type(v)
            self.write_byte(tag_id)
            self.write_string(name)
            self.write_tag(tag_id, v)
        self.write_byte(TAG_END)

    def _infer_tag_type(self, value: Any) -> int:
        """推断 Python 值对应的 NBT Tag 类型。"""
        if isinstance(value, Byte):
            return TAG_BYTE
        if isinstance(value, Short):
            return TAG_SHORT
        if isinstance(value, Int):
            return TAG_INT
        if isinstance(value, Long):
            return TAG_LONG
        if isinstance(value, Float):
            return TAG_FLOAT
        if isinstance(value, Double):
            return TAG_DOUBLE
        if isinstance(value, ByteArray):
            return TAG_BYTE_ARRAY
        if isinstance(value, IntArray):
            return TAG_INT_ARRAY
        if isinstance(value, LongArray):
            return TAG_LONG_ARRAY
        if isinstance(value, str):
            return TAG_STRING
        if isinstance(value, list):
            return TAG_LIST
        if isinstance(value, dict):
            return TAG_COMPOUND
        if isinstance(value, bool):
            return TAG_BYTE
        if isinstance(value, int):
            return TAG_INT
        if isinstance(value, float):
            return TAG_DOUBLE
        raise NBTFormatError(f"cannot infer NBT type for Python type: {type(value).__name__}")

    def write_root(self, value: dict[str, Any], root_name: str = "") -> None:
        """写入根 TAG_Compound。"""
        self.write_byte(TAG_COMPOUND)
        self.write_string(root_name)
        self._write_compound_payload(value)

    def get_bytes(self) -> bytes:
        """获取写入的字节。"""
        return self._buf.getvalue()


# -------------------------------------------------------------------- #
# 顶层编解码函数
# -------------------------------------------------------------------- #


def nbt_marshal(data: dict[str, Any], encoding: int = LITTLE_ENDIAN,
                root_name: str = "") -> bytes:
    """将 Python dict 序列化为 NBT 二进制数据。

    逆向自 WaterStructure/modules/bdump/nbt/writer.go 的 Marshal 函数。

    Args:
        data: 要序列化的字典 (根必须是 TAG_Compound)。
        encoding: 字节序编码。
        root_name: 根 TAG_Compound 的名称。

    Returns:
        NBT 二进制数据。
    """
    writer = NBTWriter(encoding=encoding)
    writer.write_root(data, root_name=root_name)
    return writer.get_bytes()


def nbt_unmarshal(data: bytes, encoding: int = LITTLE_ENDIAN) -> dict[str, Any]:
    """将 NBT 二进制数据反序列化为 Python dict。

    逆向自 WaterStructure/modules/bdump/nbt/reader.go 的 Unmarshal 函数。

    Args:
        data: NBT 二进制数据。
        encoding: 字节序编码。

    Returns:
        Python 字典表示的 NBT 数据。
    """
    reader = NBTReader(data, encoding=encoding)
    return reader.read_root()


# 字节序快捷函数
def nbt_marshal_network(data: dict[str, Any], root_name: str = "") -> bytes:
    """网络小端序序列化 (Bedrock 协议格式)。"""
    return nbt_marshal(data, NETWORK_LITTLE_ENDIAN, root_name)


def nbt_unmarshal_network(data: bytes) -> dict[str, Any]:
    """网络小端序反序列化 (Bedrock 协议格式)。"""
    return nbt_unmarshal(data, NETWORK_LITTLE_ENDIAN)


def nbt_marshal_disk(data: dict[str, Any], root_name: str = "") -> bytes:
    """磁盘小端序序列化 (Bedrock 磁盘格式)。"""
    return nbt_marshal(data, LITTLE_ENDIAN, root_name)


def nbt_unmarshal_disk(data: bytes) -> dict[str, Any]:
    """磁盘小端序反序列化 (Bedrock 磁盘格式)。"""
    return nbt_unmarshal(data, LITTLE_ENDIAN)


def nbt_marshal_big_endian(data: dict[str, Any], root_name: str = "") -> bytes:
    """大端序序列化 (Java 版磁盘格式)。"""
    return nbt_marshal(data, BIG_ENDIAN, root_name)


def nbt_unmarshal_big_endian(data: bytes) -> dict[str, Any]:
    """大端序反序列化 (Java 版磁盘格式)。"""
    return nbt_unmarshal(data, BIG_ENDIAN)


# -------------------------------------------------------------------- #
# SNBT (Stringified NBT) 解析
# -------------------------------------------------------------------- #

_SNBT_TYPE_SUFFIXES: dict[str, int] = {
    "b": TAG_BYTE, "B": TAG_BYTE,
    "s": TAG_SHORT, "S": TAG_SHORT,
    "i": TAG_INT, "I": TAG_INT,
    "l": TAG_LONG, "L": TAG_LONG,
    "f": TAG_FLOAT, "F": TAG_FLOAT,
    "d": TAG_DOUBLE, "D": TAG_DOUBLE,
}


def parse_snbt(text: str) -> Any:
    """解析 Stringified NBT (SNBT) 文本。

    逆向自 StarShuttler/nbt_parser/ 中的 SNBT 解析器。
    NexusE 使用 SNBT 在命令方块和 NBT 标签中传递数据。

    支持的语法:
        - 字符串: "hello" 或 'hello' 或 hello
        - 字节:  10b
        - 短整型: 10s
        - 整型:  10 (默认) 或 10i
        - 长整型: 10L
        - 浮点:  10.0f
        - 双精度: 10.0 (默认) 或 10.0d
        - 字节数组: [B; 1, 2, 3]
        - 整型数组: [I; 1, 2, 3]
        - 长整型数组: [L; 1, 2, 3]
        - 列表: [1, 2, 3]
        - 复合: {key: value, ...}

    Args:
        text: SNBT 文本。

    Returns:
        Python 对象表示的 NBT 数据。
    """
    parser = _SNBTParser(text)
    return parser.parse()


class _SNBTParser:
    """SNBT 文本解析器 (内部类)。"""

    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0

    def parse(self) -> Any:
        """解析整个 SNBT 文本。"""
        self._skip_whitespace()
        result = self._parse_value()
        self._skip_whitespace()
        if self._pos < len(self._text):
            raise NBTFormatError(
                f"unexpected trailing text at position {self._pos}: "
                f"{self._text[self._pos:self._pos + 20]!r}"
            )
        return result

    def _skip_whitespace(self) -> None:
        while self._pos < len(self._text) and self._text[self._pos] in " \t\n\r":
            self._pos += 1

    def _parse_value(self) -> Any:
        self._skip_whitespace()
        if self._pos >= len(self._text):
            raise NBTFormatError("unexpected EOF parsing value")
        ch = self._text[self._pos]
        if ch == "{":
            return self._parse_compound()
        if ch == "[":
            return self._parse_list_or_array()
        if ch in "\"'":
            return self._parse_quoted_string(ch)
        # 原始值 (数字、布尔、未引用字符串)
        return self._parse_primitive()

    def _parse_compound(self) -> dict[str, Any]:
        """解析复合标签 {key: value, ...}。"""
        result: dict[str, Any] = {}
        self._pos += 1  # 跳过 '{'
        self._skip_whitespace()
        if self._pos < len(self._text) and self._text[self._pos] == "}":
            self._pos += 1
            return result
        while True:
            self._skip_whitespace()
            # 解析键
            key = self._parse_key()
            self._skip_whitespace()
            if self._pos >= len(self._text) or self._text[self._pos] != ":":
                raise NBTFormatError(
                    f"expected ':' after key {key!r} at position {self._pos}"
                )
            self._pos += 1  # 跳过 ':'
            value = self._parse_value()
            result[key] = value
            self._skip_whitespace()
            if self._pos >= len(self._text):
                raise NBTFormatError("unexpected EOF in compound")
            ch = self._text[self._pos]
            if ch == ",":
                self._pos += 1
                continue
            if ch == "}":
                self._pos += 1
                break
            raise NBTFormatError(
                f"expected ',' or '}}' at position {self._pos}, got {ch!r}"
            )
        return result

    def _parse_key(self) -> str:
        """解析复合标签的键 (可以是引用或未引用字符串)。"""
        if self._pos < len(self._text) and self._text[self._pos] in "\"'":
            return self._parse_quoted_string(self._text[self._pos])
        # 未引用键
        start = self._pos
        while self._pos < len(self._text) and self._text[self._pos] not in " \t\n\r:":
            self._pos += 1
        if start == self._pos:
            raise NBTFormatError(f"empty key at position {self._pos}")
        return self._text[start:self._pos]

    def _parse_list_or_array(self) -> Any:
        """解析列表 [1,2,3] 或数组 [B; 1,2,3] / [I; ...] / [L; ...]。"""
        self._pos += 1  # 跳过 '['
        self._skip_whitespace()
        # 检查是否为数组类型前缀
        if self._pos + 1 < len(self._text) and self._text[self._pos] in "BIL":
            prefix = self._text[self._pos]
            if self._text[self._pos + 1] == ";":
                self._pos += 2
                return self._parse_array_body(prefix)
        # 普通列表
        items: list[Any] = []
        self._skip_whitespace()
        if self._pos < len(self._text) and self._text[self._pos] == "]":
            self._pos += 1
            return items
        while True:
            items.append(self._parse_value())
            self._skip_whitespace()
            if self._pos >= len(self._text):
                raise NBTFormatError("unexpected EOF in list")
            ch = self._text[self._pos]
            if ch == ",":
                self._pos += 1
                continue
            if ch == "]":
                self._pos += 1
                break
            raise NBTFormatError(
                f"expected ',' or ']' at position {self._pos}, got {ch!r}"
            )
        return items

    def _parse_array_body(self, prefix: str) -> Any:
        """解析数组体 [B; 1,2,3]。"""
        items: list[int] = []
        self._skip_whitespace()
        if self._pos < len(self._text) and self._text[self._pos] == "]":
            self._pos += 1
            if prefix == "B":
                return ByteArray(items)
            if prefix == "I":
                return IntArray(items)
            return LongArray(items)
        while True:
            self._skip_whitespace()
            # 解析数字
            start = self._pos
            while self._pos < len(self._text) and self._text[self._pos] not in ",] \t\n\r":
                self._pos += 1
            token = self._text[start:self._pos]
            # 去掉类型后缀
            if token and token[-1] in "bBsSiIlLdDfF":
                token = token[:-1]
            try:
                items.append(int(token))
            except ValueError as exc:
                raise NBTFormatError(
                    f"invalid integer in array: {token!r}"
                ) from exc
            self._skip_whitespace()
            if self._pos >= len(self._text):
                raise NBTFormatError("unexpected EOF in array")
            ch = self._text[self._pos]
            if ch == ",":
                self._pos += 1
                continue
            if ch == "]":
                self._pos += 1
                break
            raise NBTFormatError(
                f"expected ',' or ']' at position {self._pos}, got {ch!r}"
            )
        if prefix == "B":
            return ByteArray(items)
        if prefix == "I":
            return IntArray(items)
        return LongArray(items)

    def _parse_quoted_string(self, quote: str) -> str:
        """解析引用字符串。"""
        self._pos += 1  # 跳过开头引号
        result_chars: list[str] = []
        while self._pos < len(self._text):
            ch = self._text[self._pos]
            if ch == "\\":
                self._pos += 1
                if self._pos < len(self._text):
                    result_chars.append(self._text[self._pos])
                    self._pos += 1
                continue
            if ch == quote:
                self._pos += 1
                return "".join(result_chars)
            result_chars.append(ch)
            self._pos += 1
        raise NBTFormatError("unterminated quoted string")

    def _parse_primitive(self) -> Any:
        """解析原始值 (数字、布尔、未引用字符串)。"""
        start = self._pos
        while self._pos < len(self._text) and self._text[self._pos] not in ",]}\t\n\r ":
            self._pos += 1
        token = self._text[start:self._pos]
        if not token:
            raise NBTFormatError(f"empty primitive at position {start}")
        # 布尔
        if token == "true":
            return Byte(1)
        if token == "false":
            return Byte(0)
        # 检查类型后缀
        if len(token) > 1 and token[-1] in _SNBT_TYPE_SUFFIXES:
            suffix = token[-1]
            number_part = token[:-1]
            tag_type = _SNBT_TYPE_SUFFIXES[suffix]
            try:
                if tag_type == TAG_BYTE:
                    return Byte(int(number_part))
                if tag_type == TAG_SHORT:
                    return Short(int(number_part))
                if tag_type == TAG_INT:
                    return Int(int(number_part))
                if tag_type == TAG_LONG:
                    return Long(int(number_part))
                if tag_type == TAG_FLOAT:
                    return Float(float(number_part))
                if tag_type == TAG_DOUBLE:
                    return Double(float(number_part))
            except ValueError:
                pass  # 回退到字符串
        # 整数
        try:
            return Int(int(token))
        except ValueError:
            pass
        # 浮点
        try:
            return Double(float(token))
        except ValueError:
            pass
        # 未引用字符串
        return token


# -------------------------------------------------------------------- #
# NBTParser - NovaBuilder 风格的兼容入口 (合并自 NovaBuilder nbt_parser)
# -------------------------------------------------------------------- #
#:
#: Bedrock Edition 默认最大字符串长度 (修改版 mcpredict)
MAX_STRING_LENGTH: int = 32767

#:
#: Bedrock Edition 默认最大列表长度 (限制)
MAX_LIST_LENGTH: int = 2147483647

#:
#: 网络版 NBT 最大字符串长度 (network mode)
NETWORK_MAX_STRING_LENGTH: int = 65535

#:
#: 默认最大深度 (逆向自 strings)
MAX_DEPTH: int = 512


class NBTParser:
    """NBT 解析器主入口 (合并自 NovaBuilder 内置 NBT 处理)。

    适配 PocketTerm 项目结构, 在 NexusEgo NBTReader/NBTWriter 之上提供
    NovaBuilder 风格的 ``parse_bytes`` / ``parse_file`` / ``encode_bytes``
    接口, 供 mcstructure_parser / mcworld_parser 使用。

    自动检测编码方式:
        - 大端序 (Java): 第一字节 0x0A (TAG_Compound)
        - 小端序 (Bedrock): 第一字节 0x0A, 后续字节小端
        - 网络版 (Bedrock Network): 第一字节 0x0A, 字符串长度 varuint

    使用方式::

        parser = NBTParser()
        data = parser.parse_bytes(raw_bytes, encoding="little")
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger(
            "pocketterm.protocol.format_parsers.nbt_parser.parser"
        )

    def parse_bytes(
        self, data: bytes, encoding: str = "auto"
    ) -> "dict[str, Any]":
        """解析 NBT 字节数据。

        Args:
            data: NBT 字节数据
            encoding: 编码方式 ('auto', 'big', 'little')

        Returns:
            解析后的 NBT 字典

        Raises:
            NBTError: 解析失败
        """
        if not data:
            raise NBTError("Empty NBT data")

        if encoding == "auto":
            encoding = self._detect_encoding(data)
            self.logger.debug("Auto-detected encoding: %s", encoding)

        if encoding == "big":
            return nbt_unmarshal_big_endian(data)
        if encoding == "little":
            return nbt_unmarshal_disk(data)
        raise NBTError(f"Unknown encoding: {encoding}")

    def parse_file(self, path: "str", encoding: str = "auto") -> "dict[str, Any]":
        """解析 NBT 文件。

        Args:
            path: 文件路径。
            encoding: 编码方式 ('auto', 'big', 'little')。

        Returns:
            解析后的 NBT 字典。

        Raises:
            NBTError: 读取或解析失败。
        """
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            raise NBTError(f"Failed to open file: {e}") from e
        return self.parse_bytes(data, encoding)

    def encode_bytes(
        self, data: "dict[str, Any]", encoding: str = "little"
    ) -> bytes:
        """将 NBT 字典编码为字节。

        Args:
            data: NBT 字典。
            encoding: 编码方式 ('big', 'little')。

        Returns:
            NBT 二进制数据。

        Raises:
            NBTError: 未知编码。
        """
        if encoding == "big":
            return nbt_marshal_big_endian(data)
        if encoding == "little":
            return nbt_marshal_disk(data)
        raise NBTError(f"Unknown encoding: {encoding}")

    def _detect_encoding(self, data: bytes) -> str:
        """自动检测 NBT 编码 (默认小端, Bedrock 兼容)。

        检测策略:
            - 第一字节必须是 TAG_Compound (0x0A)
            - 默认使用 little-endian (Bedrock 默认)
        """
        if data[0] != TAG_COMPOUND:
            raise NBTError(
                f"Invalid NBT root tag: expected 0x0A, got {data[0]:#x}"
            )
        return "little"

    def get_block_nbt_string(self, data: "dict[str, Any]") -> str:
        """将方块 NBT 数据转换为可读字符串 (用于调试)。

        逆向自 gophertunnel 的 nbt.MarshalString。
        """
        return self._format_value(data, indent=0)

    def _format_value(self, value: Any, indent: int) -> str:
        """递归格式化 NBT 值。"""
        prefix = "  " * indent
        if isinstance(value, dict):
            lines = ["{"]
            for k, v in value.items():
                lines.append(
                    f"{prefix}  {k}: {self._format_value(v, indent + 1)}"
                )
            lines.append(f"{prefix}}}")
            return "\n".join(lines)
        if isinstance(value, list):
            if not value:
                return "[]"
            lines = ["["]
            for item in value:
                lines.append(
                    f"{prefix}  {self._format_value(item, indent + 1)}"
                )
            lines.append(f"{prefix}]")
            return "\n".join(lines)
        if isinstance(value, bytes):
            return repr(value)
        return repr(value)


__all__ = [
    # 常量
    "TAG_END", "TAG_BYTE", "TAG_SHORT", "TAG_INT", "TAG_LONG",
    "TAG_FLOAT", "TAG_DOUBLE", "TAG_BYTE_ARRAY", "TAG_STRING",
    "TAG_LIST", "TAG_COMPOUND", "TAG_INT_ARRAY", "TAG_LONG_ARRAY",
    "TAG_NAMES",
    "LITTLE_ENDIAN", "BIG_ENDIAN",
    "NETWORK_LITTLE_ENDIAN", "NETWORK_BIG_ENDIAN",
    "ENCODING_NAMES",
    # 包装类型
    "Byte", "Short", "Int", "Long", "Float", "Double",
    "ByteArray", "IntArray", "LongArray",
    # Reader/Writer
    "NBTReader", "NBTWriter",
    # 编解码函数
    "nbt_marshal", "nbt_unmarshal",
    "nbt_marshal_network", "nbt_unmarshal_network",
    "nbt_marshal_disk", "nbt_unmarshal_disk",
    "nbt_marshal_big_endian", "nbt_unmarshal_big_endian",
    "parse_snbt",
    # NovaBuilder 兼容入口
    "NBTParser",
    "MAX_STRING_LENGTH", "MAX_LIST_LENGTH",
    "NETWORK_MAX_STRING_LENGTH", "MAX_DEPTH",
    # 异常
    "NBTError", "NBTFormatError",
]
