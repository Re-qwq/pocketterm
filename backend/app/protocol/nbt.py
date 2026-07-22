"""NBT (Named Binary Tag) 编解码 — Minecraft Bedrock/Java 通用 NBT 格式。

本模块是纯 Python 实现的 NBT 编解码器, 逆向自 neomega 的 ``minecraft/nbt`` Go 包
(原实现见 ``gophertunnel/minecraft/nbt``)。

支持的 12 种 Tag 类型:
    ===============  =====  =============  ====================================
    Tag 类型          ID     Python 类型    说明
    ===============  =====  =============  ====================================
    TAG_End          0      —              复合标签结束标记
    TAG_Byte         1      Byte           有符号 8 位整数
    TAG_Short        2      Short          有符号 16 位整数
    TAG_Int          3      Int            有符号 32 位整数
    TAG_Long         4      Long           有符号 64 位整数
    TAG_Float        5      Float          32 位浮点数 (IEEE 754)
    TAG_Double       6      Double         64 位浮点数 (IEEE 754)
    TAG_ByteArray    7      ByteArray      有符号 8 位整数数组
    TAG_String       8      str            UTF-8 字符串
    TAG_List         9      list           同类型标签列表
    TAG_Compound     10     dict           键值对 (键为字符串)
    TAG_IntArray     11     IntArray       有符号 32 位整数数组
    TAG_LongArray    12     LongArray      有符号 64 位整数数组
    ===============  =====  =============  ====================================

支持的 4 种字节序编码:
    - ``littleEndian``: 标准小端序 (磁盘 NBT), 字符串长度用 uint16
    - ``bigEndian``: 大端序 (Java 版 NBT), 字符串长度用 uint16
    - ``networkLittleEndian``: 网络小端序 (网络 NBT), 字符串/int32/int64 长度用 Varint
    - ``networkBigEndian``: 网络大端序, 字符串/int32/int64 长度用 Varint

关键设计点:
    - 网络 NBT 的字符串长度、int32、int64 使用 ZigZag Varint 编码
    - 磁盘 NBT 的字符串长度使用 uint16 编码
    - TAG_List 的元素类型由头部 type 字段指定
    - TAG_Compound 以 TAG_End 结尾
    - 支持嵌套 (递归解析), 最大深度限制 512
    - 网络 NBT 最大读取字节数 4MB

基本用法::

    from app.protocol.nbt import marshal_network, unmarshal_network

    # 编码
    data = marshal_network({"name": "Steve", "health": 20})
    # 解码
    nbt = unmarshal_network(data)
    print(nbt)  # {'name': 'Steve', 'health': Int(20)}

逆向来源:
    - neomega ``minecraft/nbt`` (Go)
    - gophertunnel ``minecraft/nbt`` (Go)
"""

from __future__ import annotations

import struct
from typing import Any, Union

from .varint import (
    decode_varint32,
    decode_varint64,
    decode_varuint32,
    encode_varint32,
    encode_varint64,
    encode_varuint32,
)

# ======================================================================
# 常量: Tag 类型 ID
# ======================================================================

#: TAG_End — 复合标签结束标记 (无 payload)
TAG_END: int = 0

#: TAG_Byte — 有符号 8 位整数
TAG_BYTE: int = 1

#: TAG_Short — 有符号 16 位整数
TAG_SHORT: int = 2

#: TAG_Int — 有符号 32 位整数
TAG_INT: int = 3

#: TAG_Long — 有符号 64 位整数
TAG_LONG: int = 4

#: TAG_Float — 32 位浮点数
TAG_FLOAT: int = 5

#: TAG_Double — 64 位浮点数
TAG_DOUBLE: int = 6

#: TAG_ByteArray — 有符号 8 位整数数组
TAG_BYTE_ARRAY: int = 7

#: TAG_String — UTF-8 字符串
TAG_STRING: int = 8

#: TAG_List — 同类型标签列表
TAG_LIST: int = 9

#: TAG_Compound — 键值对复合标签
TAG_COMPOUND: int = 10

#: TAG_IntArray — 有符号 32 位整数数组
TAG_INT_ARRAY: int = 11

#: TAG_LongArray — 有符号 64 位整数数组
TAG_LONG_ARRAY: int = 12

#: 所有有效 Tag 类型的集合
_VALID_TAGS: frozenset[int] = frozenset(range(0, 13))

#: Tag 类型 ID 到名称的映射
_TAG_NAMES: dict[int, str] = {
    TAG_END: "TAG_End",
    TAG_BYTE: "TAG_Byte",
    TAG_SHORT: "TAG_Short",
    TAG_INT: "TAG_Int",
    TAG_LONG: "TAG_Long",
    TAG_FLOAT: "TAG_Float",
    TAG_DOUBLE: "TAG_Double",
    TAG_BYTE_ARRAY: "TAG_ByteArray",
    TAG_STRING: "TAG_String",
    TAG_LIST: "TAG_List",
    TAG_COMPOUND: "TAG_Compound",
    TAG_INT_ARRAY: "TAG_IntArray",
    TAG_LONG_ARRAY: "TAG_LongArray",
}


# ======================================================================
# 常量: 字节序编码
# ======================================================================

#: 标准小端序 (磁盘 NBT) — 字符串长度用 uint16, 整数用固定大小小端序
LITTLE_ENDIAN: str = "littleEndian"

#: 大端序 (Java 版 NBT) — 字符串长度用 uint16, 整数用固定大小大端序
BIG_ENDIAN: str = "bigEndian"

#: 网络小端序 (网络 NBT) — 字符串/int32/int64 用 Varint, short/float 用小端序
NETWORK_LITTLE_ENDIAN: str = "networkLittleEndian"

#: 网络大端序 — 字符串/int32/int64 用 Varint, short/float 用大端序
NETWORK_BIG_ENDIAN: str = "networkBigEndian"

#: 所有有效的编码名称
_VALID_ENCODINGS: frozenset[str] = frozenset(
    {LITTLE_ENDIAN, BIG_ENDIAN, NETWORK_LITTLE_ENDIAN, NETWORK_BIG_ENDIAN}
)

#: 网络编码集合 (使用 Varint)
_NETWORK_ENCODINGS: frozenset[str] = frozenset(
    {NETWORK_LITTLE_ENDIAN, NETWORK_BIG_ENDIAN}
)

#: 小端编码集合 (fixed-size 整数用小端序)
_LITTLE_ENCODINGS: frozenset[str] = frozenset(
    {LITTLE_ENDIAN, NETWORK_LITTLE_ENDIAN}
)


# ======================================================================
# 常量: 限制
# ======================================================================

#: 最大嵌套深度 (Compound/List 嵌套层级)
MAX_NESTING_DEPTH: int = 512

#: 网络 NBT 最大读取字节数 (4MB)
MAX_NETWORK_BYTES: int = 4 * 1024 * 1024

#: 字符串最大长度 (uint16 可表示范围)
MAX_STRING_LENGTH: int = 0x7FFF  # 32767


# ======================================================================
# 异常
# ======================================================================


class NBTError(Exception):
    """所有 NBT 相关错误的基类。"""


class InvalidTagError(NBTError):
    """遇到了未知或无效的 Tag 类型。

    Attributes:
        tag_type: 无效的 Tag 类型 ID。
        offset: 发生错误时的字节偏移量。
    """

    def __init__(self, tag_type: int, offset: int = -1, op: str = "") -> None:
        self.tag_type = tag_type
        self.offset = offset
        self.op = op
        tag_name = _TAG_NAMES.get(tag_type, f"Unknown({tag_type})")
        msg = f"nbt: invalid tag type '{tag_name}' (0x{tag_type:02x})"
        if offset >= 0:
            msg += f" at offset {offset}"
        if op:
            msg += f" during op '{op}'"
        super().__init__(msg)


class BufferOverrunError(NBTError):
    """读取操作超出了缓冲区末尾。

    Attributes:
        op: 发生错误的操作名称。
        offset: 发生错误时的字节偏移量。
        needed: 需要读取的字节数。
        available: 缓冲区中可用的字节数。
    """

    def __init__(
        self,
        op: str = "",
        offset: int = -1,
        needed: int = 0,
        available: int = 0,
    ) -> None:
        self.op = op
        self.offset = offset
        self.needed = needed
        self.available = available
        msg = f"nbt: buffer overrun during op '{op}'"
        if offset >= 0:
            msg += f" at offset {offset}"
        if needed:
            msg += f" (needed {needed} bytes, {available} available)"
        super().__init__(msg)


class InvalidTypeError(NBTError):
    """值的类型无法转换为 NBT 标签, 或与期望的类型不匹配。

    Attributes:
        value: 导致错误的值。
        expected_type: 期望的 Python 类型 (如有)。
    """

    def __init__(self, message: str, value: Any = None, expected_type: Any = None) -> None:
        self.value = value
        self.expected_type = expected_type
        super().__init__(message)


class InvalidStringError(NBTError):
    """字符串无效 (长度超出限制或编码不合法)。

    Attributes:
        offset: 发生错误时的字节偏移量。
        string: 导致错误的字符串 (如有)。
    """

    def __init__(self, message: str, offset: int = -1, string: str = "") -> None:
        self.offset = offset
        self.string = string
        msg = f"nbt: invalid string: {message}"
        if offset >= 0:
            msg += f" at offset {offset}"
        super().__init__(msg)


class InvalidArraySizeError(NBTError):
    """数组大小无效 (为负数或超出合理范围)。

    Attributes:
        offset: 发生错误时的字节偏移量。
        size: 无效的数组大小。
    """

    def __init__(self, message: str, offset: int = -1, size: int = 0) -> None:
        self.offset = offset
        self.size = size
        msg = f"nbt: invalid array size: {message}"
        if offset >= 0:
            msg += f" at offset {offset}"
        super().__init__(msg)


class MaximumDepthReachedError(NBTError):
    """达到了最大嵌套深度 ({MAX_NESTING_DEPTH})。

    Attributes:
        depth: 当前的嵌套深度。
    """

    def __init__(self, depth: int = 0) -> None:
        self.depth = depth
        super().__init__(
            f"nbt: maximum nesting depth of {MAX_NESTING_DEPTH} was reached "
            f"(current depth: {depth})"
        )


class MaximumBytesReadError(NBTError):
    """网络 NBT 读取超过了最大字节数限制 ({MAX_NETWORK_BYTES} 字节)。

    Attributes:
        offset: 当前读取偏移量。
    """

    def __init__(self, offset: int = 0) -> None:
        self.offset = offset
        super().__init__(
            f"nbt: limit of {MAX_NETWORK_BYTES} bytes read with network format "
            f"exhausted (current offset: {offset})"
        )


class FailedWriteError(NBTError):
    """写入操作失败。

    Attributes:
        op: 发生错误的操作名称。
    """

    def __init__(self, op: str = "", message: str = "") -> None:
        self.op = op
        msg = f"nbt: failed write during op '{op}'"
        if message:
            msg += f": {message}"
        super().__init__(msg)


class SNBTParseError(NBTError):
    """SNBT 字符串解析错误。

    Attributes:
        position: 解析错误在字符串中的位置。
        text: 正在解析的 SNBT 文本。
    """

    def __init__(self, message: str, position: int = -1, text: str = "") -> None:
        self.position = position
        self.text = text
        msg = f"nbt: SNBT parse error: {message}"
        if position >= 0:
            msg += f" at position {position}"
        super().__init__(msg)


# ======================================================================
# Tag 包装类型
# ======================================================================


class Byte(int):
    """TAG_Byte 包装类型 — 有符号 8 位整数 (-128 ~ 127)。

    继承自 :class:`int`, 可与普通 int 互操作::

        >>> b = Byte(42)
        >>> b == 42
        True
        >>> isinstance(b, int)
        True
    """

    def __repr__(self) -> str:
        return f"Byte({int(self)})"


class Short(int):
    """TAG_Short 包装类型 — 有符号 16 位整数 (-32768 ~ 32767)。

    继承自 :class:`int`, 可与普通 int 互操作。
    """

    def __repr__(self) -> str:
        return f"Short({int(self)})"


class Int(int):
    """TAG_Int 包装类型 — 有符号 32 位整数 (-2147483648 ~ 2147483647)。

    继承自 :class:`int`, 可与普通 int 互操作。
    """

    def __repr__(self) -> str:
        return f"Int({int(self)})"


class Long(int):
    """TAG_Long 包装类型 — 有符号 64 位整数。

    继承自 :class:`int`, 可与普通 int 互操作。
    """

    def __repr__(self) -> str:
        return f"Long({int(self)})"


class Float(float):
    """TAG_Float 包装类型 — 32 位浮点数 (IEEE 754 单精度)。

    继承自 :class:`float`, 可与普通 float 互操作::

        >>> f = Float(3.14)
        >>> f == 3.14
        True
        >>> isinstance(f, float)
        True
    """

    def __repr__(self) -> str:
        return f"Float({float(self)})"


class Double(float):
    """TAG_Double 包装类型 — 64 位浮点数 (IEEE 754 双精度)。

    继承自 :class:`float`, 可与普通 float 互操作。
    """

    def __repr__(self) -> str:
        return f"Double({float(self)})"


class ByteArray(bytes):
    """TAG_ByteArray 包装类型 — 有符号 8 位整数数组。

    继承自 :class:`bytes`, 可与普通 bytes 互操作::

        >>> ba = ByteArray(b'\\x01\\x02\\x03')
        >>> ba == b'\\x01\\x02\\x03'
        True
        >>> isinstance(ba, bytes)
        True
    """

    def __repr__(self) -> str:
        return f"ByteArray({bytes(self)!r})"


class IntArray(list):
    """TAG_IntArray 包装类型 — 有符号 32 位整数数组。

    继承自 :class:`list`, 可与普通 list 互操作::

        >>> ia = IntArray([1, 2, 3])
        >>> ia == [1, 2, 3]
        True
        >>> isinstance(ia, list)
        True

    注意: 在类型推断时, IntArray 优先于 list (会被识别为 TAG_IntArray)。
    """

    def __repr__(self) -> str:
        return f"IntArray({list.__repr__(self)})"


class LongArray(list):
    """TAG_LongArray 包装类型 — 有符号 64 位整数数组。

    继承自 :class:`list`, 可与普通 list 互操作。

    注意: 在类型推断时, LongArray 优先于 list (会被识别为 TAG_LongArray)。
    """

    def __repr__(self) -> str:
        return f"LongArray({list.__repr__(self)})"


#: 所有整数包装类型的元组 (用于 isinstance 检查)
_INT_WRAPPERS: tuple[type, ...] = (Byte, Short, Int, Long)

#: 所有浮点数包装类型的元组
_FLOAT_WRAPPERS: tuple[type, ...] = (Float, Double)


# ======================================================================
# NBTReader — NBT 读取器
# ======================================================================


class NBTReader:
    """从字节缓冲区读取 NBT 数据。

    支持 4 种字节序编码, 通过 ``encoding`` 参数指定。

    Args:
        data: 包含 NBT 数据的字节串。
        encoding: 字节序编码, 默认为 :data:`NETWORK_LITTLE_ENDIAN`。

    Example::

        reader = NBTReader(data, encoding=NETWORK_LITTLE_ENDIAN)
        tag_type = reader.read_byte()
        tag_name = reader.read_string()
        value = reader.read_payload(tag_type)
    """

    def __init__(self, data: bytes, encoding: str = NETWORK_LITTLE_ENDIAN) -> None:
        if encoding not in _VALID_ENCODINGS:
            raise ValueError(
                f"Unknown encoding: {encoding!r}. "
                f"Valid encodings: {_VALID_ENCODINGS}"
            )
        self.data: bytes = data
        self.offset: int = 0
        self.encoding: str = encoding
        self.depth: int = 0
        self._is_network: bool = encoding in _NETWORK_ENCODINGS
        self._is_little: bool = encoding in _LITTLE_ENCODINGS

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _read_bytes(self, n: int, op: str = "") -> bytes:
        """从缓冲区读取 ``n`` 个字节。

        Args:
            n: 要读取的字节数。
            op: 调用方操作名称 (用于错误消息)。

        Returns:
            读取到的 ``n`` 个字节。

        Raises:
            BufferOverrunError: 缓冲区中剩余字节不足。
        """
        if n < 0:
            raise InvalidArraySizeError(
                f"cannot read negative bytes ({n})", self.offset, n
            )
        end = self.offset + n
        if end > len(self.data):
            raise BufferOverrunError(
                op=op or "ReadBytes",
                offset=self.offset,
                needed=n,
                available=len(self.data) - self.offset,
            )
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def _read_byte_raw(self, op: str = "") -> int:
        """读取单个字节并返回其无符号整数值。"""
        data = self._read_bytes(1, op)
        return data[0]

    def _check_depth(self) -> None:
        """检查嵌套深度是否超过限制。"""
        if self.depth >= MAX_NESTING_DEPTH:
            raise MaximumDepthReachedError(self.depth)

    def _check_network_limit(self) -> None:
        """检查网络 NBT 读取字节数是否超过限制。"""
        if self._is_network and self.offset >= MAX_NETWORK_BYTES:
            raise MaximumBytesReadError(self.offset)

    # ------------------------------------------------------------------
    # 基本类型读取方法
    # ------------------------------------------------------------------

    def read_byte(self) -> int:
        """读取 TAG_Byte (1 字节有符号整数)。

        Returns:
            -128 ~ 127 的有符号整数值。
        """
        value = self._read_byte_raw("Byte")
        if value >= 128:
            value -= 256
        return value

    def read_short(self) -> int:
        """读取 TAG_Short (2 字节有符号整数)。

        字节序取决于当前编码:
            - littleEndian / networkLittleEndian: 小端序
            - bigEndian / networkBigEndian: 大端序

        Returns:
            -32768 ~ 32767 的有符号整数值。
        """
        data = self._read_bytes(2, "Short")
        byte_order = "little" if self._is_little else "big"
        return int.from_bytes(data, byte_order, signed=True)

    def read_int(self) -> int:
        """读取 TAG_Int (4 字节有符号整数)。

        字节序取决于当前编码:
            - littleEndian: 小端序 4 字节
            - bigEndian: 大端序 4 字节
            - networkLittleEndian / networkBigEndian: ZigZag Varint32

        Returns:
            有符号 32 位整数值。

        Raises:
            BufferOverrunError: 缓冲区中数据不足。
        """
        if self._is_network:
            try:
                value, self.offset = decode_varint32(self.data, self.offset)
            except ValueError as exc:
                raise BufferOverrunError(
                    op="Int", offset=self.offset,
                    needed=1, available=len(self.data) - self.offset,
                ) from exc
            return value
        data = self._read_bytes(4, "Int")
        byte_order = "little" if self._is_little else "big"
        return int.from_bytes(data, byte_order, signed=True)

    def read_long(self) -> int:
        """读取 TAG_Long (8 字节有符号整数)。

        字节序取决于当前编码:
            - littleEndian: 小端序 8 字节
            - bigEndian: 大端序 8 字节
            - networkLittleEndian / networkBigEndian: ZigZag Varint64

        Returns:
            有符号 64 位整数值。

        Raises:
            BufferOverrunError: 缓冲区中数据不足。
        """
        if self._is_network:
            try:
                value, self.offset = decode_varint64(self.data, self.offset)
            except ValueError as exc:
                raise BufferOverrunError(
                    op="Long", offset=self.offset,
                    needed=1, available=len(self.data) - self.offset,
                ) from exc
            return value
        data = self._read_bytes(8, "Long")
        byte_order = "little" if self._is_little else "big"
        return int.from_bytes(data, byte_order, signed=True)

    def read_float(self) -> float:
        """读取 TAG_Float (4 字节 IEEE 754 单精度浮点数)。

        字节序取决于当前编码:
            - littleEndian / networkLittleEndian: 小端序
            - bigEndian / networkBigEndian: 大端序

        Returns:
            32 位浮点数值。
        """
        data = self._read_bytes(4, "Float")
        fmt = "<f" if self._is_little else ">f"
        return struct.unpack(fmt, data)[0]

    def read_double(self) -> float:
        """读取 TAG_Double (8 字节 IEEE 754 双精度浮点数)。

        字节序取决于当前编码:
            - littleEndian / networkLittleEndian: 小端序
            - bigEndian / networkBigEndian: 大端序

        Returns:
            64 位浮点数值。
        """
        data = self._read_bytes(8, "Double")
        fmt = "<d" if self._is_little else ">d"
        return struct.unpack(fmt, data)[0]

    def read_string(self) -> str:
        """读取 TAG_String (UTF-8 字符串)。

        字符串长度编码取决于当前编码:
            - littleEndian / bigEndian: uint16 (2 字节固定长度前缀)
            - networkLittleEndian / networkBigEndian: Varuint32 (变长前缀)

        Returns:
            UTF-8 解码后的字符串。

        Raises:
            InvalidStringError: 字符串长度超出限制。
            BufferOverrunError: 缓冲区中数据不足。
        """
        if self._is_network:
            try:
                length, self.offset = decode_varuint32(self.data, self.offset)
            except ValueError as exc:
                raise BufferOverrunError(
                    op="String", offset=self.offset,
                    needed=1, available=len(self.data) - self.offset,
                ) from exc
        else:
            data = self._read_bytes(2, "String")
            byte_order = "little" if self._is_little else "big"
            length = int.from_bytes(data, byte_order, signed=False)

        if length > MAX_STRING_LENGTH:
            raise InvalidStringError(
                f"string length {length} exceeds maximum {MAX_STRING_LENGTH}",
                offset=self.offset,
            )
        if length < 0:
            raise InvalidStringError(
                f"string length is negative ({length})", offset=self.offset
            )

        raw = self._read_bytes(length, "String")
        return raw.decode("utf-8")

    # ------------------------------------------------------------------
    # 复合类型读取方法
    # ------------------------------------------------------------------

    def read_byte_array(self) -> bytes:
        """读取 TAG_ByteArray (有符号 8 位整数数组)。

        格式: ``[length: int32] [length 个字节]``

        Returns:
            包含数组内容的 bytes 对象。
        """
        length = self.read_int()
        if length < 0:
            raise InvalidArraySizeError(
                f"byte array length is negative ({length})", self.offset, length
            )
        return self._read_bytes(length, "ByteArray")

    def read_int_array(self) -> list[int]:
        """读取 TAG_IntArray (有符号 32 位整数数组)。

        格式: ``[length: int32] [length 个 int32]``

        Returns:
            包含 int32 值的列表。
        """
        length = self.read_int()
        if length < 0:
            raise InvalidArraySizeError(
                f"int array length is negative ({length})", self.offset, length
            )
        return [self.read_int() for _ in range(length)]

    def read_long_array(self) -> list[int]:
        """读取 TAG_LongArray (有符号 64 位整数数组)。

        格式: ``[length: int32] [length 个 int64]``

        Returns:
            包含 int64 值的列表。
        """
        length = self.read_int()
        if length < 0:
            raise InvalidArraySizeError(
                f"long array length is negative ({length})", self.offset, length
            )
        return [self.read_long() for _ in range(length)]

    def read_list(self) -> list[Any]:
        """读取 TAG_List (同类型标签列表)。

        格式: ``[element_type: byte] [length: int32] [length 个 element_type payload]``

        列表中的所有元素必须是同一种 Tag 类型, 由头部 ``element_type`` 字段指定。

        Returns:
            包含元素值的列表。元素类型取决于 ``element_type``。
        """
        self.depth += 1
        self._check_depth()

        elem_type = self._read_byte_raw("List")
        if elem_type not in _VALID_TAGS:
            raise InvalidTagError(elem_type, self.offset, "List")

        length = self.read_int()
        if length < 0:
            raise InvalidArraySizeError(
                f"list length is negative ({length})", self.offset, length
            )

        result: list[Any] = []
        for _ in range(length):
            result.append(self.read_payload(elem_type))

        self.depth -= 1
        return result

    def read_compound(self) -> dict[str, Any]:
        """读取 TAG_Compound (键值对复合标签)。

        格式: ``{ [tag_type: byte] [tag_name: string] [payload] }* [TAG_End: byte]``

        复合标签由一系列命名标签组成, 以 TAG_End (0x00) 结尾。

        Returns:
            键为字符串、值为对应 Tag payload 的字典。
        """
        self.depth += 1
        self._check_depth()

        result: dict[str, Any] = {}
        while True:
            self._check_network_limit()
            tag_type = self._read_byte_raw("Compound")
            if tag_type == TAG_END:
                break
            if tag_type not in _VALID_TAGS:
                raise InvalidTagError(tag_type, self.offset, "Compound")
            tag_name = self.read_string()
            value = self.read_payload(tag_type)
            result[tag_name] = value

        self.depth -= 1
        return result

    def read_tag_header(self) -> tuple[int, str]:
        """读取一个命名标签的类型和名称。

        这是对 :meth:`read_compound` 中循环逻辑的提取, 用于手动解析 NBT 流。

        Returns:
            ``(tag_type, tag_name)`` 元组。对于 TAG_End, tag_name 为空字符串。

        Raises:
            MaximumDepthReachedError: 嵌套深度超过限制。
            MaximumBytesReadError: 网络 NBT 读取超过字节限制。
            InvalidTagError: 遇到未知的 Tag 类型。
        """
        self._check_depth()
        self._check_network_limit()
        tag_type = self._read_byte_raw("ReadTag")
        if tag_type == TAG_END:
            return TAG_END, ""
        if tag_type not in _VALID_TAGS:
            raise InvalidTagError(tag_type, self.offset, "ReadTag")
        tag_name = self.read_string()
        return tag_type, tag_name

    def read_payload(self, tag_type: int) -> Any:
        """根据 Tag 类型读取 payload。

        Args:
            tag_type: Tag 类型 ID (0-12)。

        Returns:
            对应的 Python 值:
                - TAG_Byte -> :class:`Byte`
                - TAG_Short -> :class:`Short`
                - TAG_Int -> :class:`Int`
                - TAG_Long -> :class:`Long`
                - TAG_Float -> :class:`Float`
                - TAG_Double -> :class:`Double`
                - TAG_ByteArray -> :class:`ByteArray`
                - TAG_String -> :class:`str`
                - TAG_List -> :class:`list`
                - TAG_Compound -> :class:`dict`
                - TAG_IntArray -> :class:`IntArray`
                - TAG_LongArray -> :class:`LongArray`

        Raises:
            InvalidTagError: tag_type 为 TAG_End 或未知类型。
        """
        if tag_type == TAG_BYTE:
            return Byte(self.read_byte())
        if tag_type == TAG_SHORT:
            return Short(self.read_short())
        if tag_type == TAG_INT:
            return Int(self.read_int())
        if tag_type == TAG_LONG:
            return Long(self.read_long())
        if tag_type == TAG_FLOAT:
            return Float(self.read_float())
        if tag_type == TAG_DOUBLE:
            return Double(self.read_double())
        if tag_type == TAG_BYTE_ARRAY:
            return ByteArray(self.read_byte_array())
        if tag_type == TAG_STRING:
            return self.read_string()
        if tag_type == TAG_LIST:
            return self.read_list()
        if tag_type == TAG_COMPOUND:
            return self.read_compound()
        if tag_type == TAG_INT_ARRAY:
            return IntArray(self.read_int_array())
        if tag_type == TAG_LONG_ARRAY:
            return LongArray(self.read_long_array())
        if tag_type == TAG_END:
            raise InvalidTagError(
                TAG_END, self.offset, "ReadPayload"
            )
        raise InvalidTagError(tag_type, self.offset, "ReadPayload")


# ======================================================================
# NBTWriter — NBT 写入器
# ======================================================================


class NBTWriter:
    """向字节缓冲区写入 NBT 数据。

    支持 4 种字节序编码, 通过 ``encoding`` 参数指定。

    Args:
        encoding: 字节序编码, 默认为 :data:`NETWORK_LITTLE_ENDIAN`。

    Example::

        writer = NBTWriter(encoding=NETWORK_LITTLE_ENDIAN)
        writer.write_tag(TAG_COMPOUND, "")
        writer.write_compound({"name": "Steve", "health": Int(20)})
        data = writer.get_bytes()
    """

    def __init__(self, encoding: str = NETWORK_LITTLE_ENDIAN) -> None:
        if encoding not in _VALID_ENCODINGS:
            raise ValueError(
                f"Unknown encoding: {encoding!r}. "
                f"Valid encodings: {_VALID_ENCODINGS}"
            )
        self.buf: bytearray = bytearray()
        self.encoding: str = encoding
        self.depth: int = 0
        self._is_network: bool = encoding in _NETWORK_ENCODINGS
        self._is_little: bool = encoding in _LITTLE_ENCODINGS

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _write_raw(self, data: bytes, op: str = "") -> None:
        """将原始字节写入缓冲区。"""
        self.buf.extend(data)

    def _write_byte_raw(self, value: int, op: str = "") -> None:
        """写入单个字节 (无符号 0-255)。"""
        self.buf.append(value & 0xFF)

    def _check_depth(self) -> None:
        """检查嵌套深度是否超过限制。"""
        if self.depth >= MAX_NESTING_DEPTH:
            raise MaximumDepthReachedError(self.depth)

    # ------------------------------------------------------------------
    # 基本类型写入方法
    # ------------------------------------------------------------------

    def write_byte(self, value: int) -> None:
        """写入 TAG_Byte (1 字节有符号整数)。

        Args:
            value: -128 ~ 127 的整数值。
        """
        self._write_byte_raw(value, "Byte")

    def write_short(self, value: int) -> None:
        """写入 TAG_Short (2 字节有符号整数)。

        字节序取决于当前编码。

        Args:
            value: -32768 ~ 32767 的整数值。
        """
        byte_order = "little" if self._is_little else "big"
        self._write_raw(value.to_bytes(2, byte_order, signed=True), "Short")

    def write_int(self, value: int) -> None:
        """写入 TAG_Int (4 字节有符号整数)。

        编码方式取决于当前编码:
            - littleEndian: 小端序 4 字节
            - bigEndian: 大端序 4 字节
            - networkLittleEndian / networkBigEndian: ZigZag Varint32

        Args:
            value: 有符号 32 位整数值。
        """
        if self._is_network:
            self._write_raw(encode_varint32(value), "Int")
        else:
            byte_order = "little" if self._is_little else "big"
            self._write_raw(value.to_bytes(4, byte_order, signed=True), "Int")

    def write_long(self, value: int) -> None:
        """写入 TAG_Long (8 字节有符号整数)。

        编码方式取决于当前编码:
            - littleEndian: 小端序 8 字节
            - bigEndian: 大端序 8 字节
            - networkLittleEndian / networkBigEndian: ZigZag Varint64

        Args:
            value: 有符号 64 位整数值。
        """
        if self._is_network:
            self._write_raw(encode_varint64(value), "Long")
        else:
            byte_order = "little" if self._is_little else "big"
            self._write_raw(value.to_bytes(8, byte_order, signed=True), "Long")

    def write_float(self, value: float) -> None:
        """写入 TAG_Float (4 字节 IEEE 754 单精度浮点数)。

        字节序取决于当前编码。

        Args:
            value: 32 位浮点数值。
        """
        fmt = "<f" if self._is_little else ">f"
        self._write_raw(struct.pack(fmt, value), "Float")

    def write_double(self, value: float) -> None:
        """写入 TAG_Double (8 字节 IEEE 754 双精度浮点数)。

        字节序取决于当前编码。

        Args:
            value: 64 位浮点数值。
        """
        fmt = "<d" if self._is_little else ">d"
        self._write_raw(struct.pack(fmt, value), "Double")

    def write_string(self, value: str) -> None:
        """写入 TAG_String (UTF-8 字符串)。

        字符串长度编码取决于当前编码:
            - littleEndian / bigEndian: uint16 (2 字节固定长度前缀)
            - networkLittleEndian / networkBigEndian: Varuint32 (变长前缀)

        Args:
            value: 要写入的字符串。

        Raises:
            InvalidStringError: 字符串长度超出限制。
        """
        raw = value.encode("utf-8")
        length = len(raw)
        if length > MAX_STRING_LENGTH:
            raise InvalidStringError(
                f"string length {length} exceeds maximum {MAX_STRING_LENGTH}",
                offset=len(self.buf),
                string=value,
            )
        if self._is_network:
            self._write_raw(encode_varuint32(length), "String")
        else:
            byte_order = "little" if self._is_little else "big"
            self._write_raw(length.to_bytes(2, byte_order, signed=False), "String")
        self._write_raw(raw, "String")

    # ------------------------------------------------------------------
    # 复合类型写入方法
    # ------------------------------------------------------------------

    def write_byte_array(self, value: bytes) -> None:
        """写入 TAG_ByteArray (有符号 8 位整数数组)。

        格式: ``[length: int32] [length 个字节]``

        Args:
            value: 包含数组内容的 bytes 对象。
        """
        self.write_int(len(value))
        self._write_raw(bytes(value), "ByteArray")

    def write_int_array(self, value: list[int]) -> None:
        """写入 TAG_IntArray (有符号 32 位整数数组)。

        格式: ``[length: int32] [length 个 int32]``

        Args:
            value: 包含 int32 值的列表。
        """
        self.write_int(len(value))
        for v in value:
            self.write_int(int(v))

    def write_long_array(self, value: list[int]) -> None:
        """写入 TAG_LongArray (有符号 64 位整数数组)。

        格式: ``[length: int32] [length 个 int64]``

        Args:
            value: 包含 int64 值的列表。
        """
        self.write_int(len(value))
        for v in value:
            self.write_long(int(v))

    def write_list(self, value: list[Any]) -> None:
        """写入 TAG_List (同类型标签列表)。

        格式: ``[element_type: byte] [length: int32] [length 个 element_type payload]``

        列表元素类型由第一个元素推断。空列表默认使用 TAG_Byte 作为元素类型。

        Args:
            value: 包含同类型元素的列表。

        Raises:
            InvalidTypeError: 无法推断元素类型。
        """
        self.depth += 1
        self._check_depth()

        if not value:
            elem_type = TAG_BYTE
        else:
            elem_type = self.infer_tag_type(value[0])

        self._write_byte_raw(elem_type, "List")
        self.write_int(len(value))
        for elem in value:
            self.write_payload(elem, elem_type)

        self.depth -= 1

    def write_compound(self, value: dict[str, Any]) -> None:
        """写入 TAG_Compound (键值对复合标签)。

        格式: ``{ [tag_type: byte] [tag_name: string] [payload] }* [TAG_End: byte]``

        Args:
            value: 键为字符串、值为对应 Tag payload 的字典。

        Raises:
            InvalidTypeError: 无法推断某个值的 Tag 类型。
        """
        self.depth += 1
        self._check_depth()

        for key, val in value.items():
            tag_type = self.infer_tag_type(val)
            self.write_tag(tag_type, str(key))
            self.write_payload(val, tag_type)

        self._write_byte_raw(TAG_END, "Compound")
        self.depth -= 1

    def write_tag(self, tag_type: int, name: str) -> None:
        """写入一个命名标签的类型和名称 (不含 payload)。

        Args:
            tag_type: Tag 类型 ID (0-12)。
            name: 标签名称。

        Raises:
            MaximumDepthReachedError: 嵌套深度超过限制。
            InvalidTagError: tag_type 为未知类型。
        """
        self._check_depth()
        if tag_type not in _VALID_TAGS:
            raise InvalidTagError(tag_type, len(self.buf), "WriteTag")
        self._write_byte_raw(tag_type, "WriteTag")
        self.write_string(name)

    def write_payload(self, value: Any, tag_type: int | None = None) -> None:
        """根据 Tag 类型写入 payload。

        如果 ``tag_type`` 为 None, 则从 ``value`` 的类型推断 Tag 类型。

        Args:
            value: 要写入的 Python 值。
            tag_type: Tag 类型 ID。如果为 None 则自动推断。

        Raises:
            InvalidTagError: tag_type 为未知类型。
            InvalidTypeError: 无法从 value 推断 Tag 类型。
        """
        if tag_type is None:
            tag_type = self.infer_tag_type(value)

        if tag_type == TAG_BYTE:
            self.write_byte(int(value))
        elif tag_type == TAG_SHORT:
            self.write_short(int(value))
        elif tag_type == TAG_INT:
            self.write_int(int(value))
        elif tag_type == TAG_LONG:
            self.write_long(int(value))
        elif tag_type == TAG_FLOAT:
            self.write_float(float(value))
        elif tag_type == TAG_DOUBLE:
            self.write_double(float(value))
        elif tag_type == TAG_BYTE_ARRAY:
            self.write_byte_array(bytes(value))
        elif tag_type == TAG_STRING:
            self.write_string(str(value))
        elif tag_type == TAG_LIST:
            self.write_list(list(value))
        elif tag_type == TAG_COMPOUND:
            self.write_compound(dict(value))
        elif tag_type == TAG_INT_ARRAY:
            self.write_int_array(list(value))
        elif tag_type == TAG_LONG_ARRAY:
            self.write_long_array(list(value))
        else:
            raise InvalidTagError(tag_type, len(self.buf), "WritePayload")

    # ------------------------------------------------------------------
    # 类型推断
    # ------------------------------------------------------------------

    @staticmethod
    def infer_tag_type(value: Any) -> int:
        """从 Python 值推断 NBT Tag 类型。

        推断规则:
            - bool -> TAG_Byte (布尔值编码为 0/1 的 byte)
            - Byte -> TAG_Byte
            - Short -> TAG_Short
            - Int -> TAG_Int
            - Long -> TAG_Long
            - Float -> TAG_Float
            - Double -> TAG_Double
            - ByteArray -> TAG_ByteArray
            - IntArray -> TAG_IntArray
            - LongArray -> TAG_LongArray
            - str -> TAG_String
            - bytes / bytearray -> TAG_ByteArray
            - list -> TAG_List
            - dict -> TAG_Compound
            - int (普通整数) -> TAG_Int (默认)
            - float (普通浮点数) -> TAG_Double (默认)

        Args:
            value: 要推断类型的 Python 值。

        Returns:
            对应的 Tag 类型 ID。

        Raises:
            InvalidTypeError: 无法从值推断 Tag 类型。
        """
        # bool 必须在 int 之前检查 (bool 是 int 的子类)
        if isinstance(value, bool):
            return TAG_BYTE
        # 整数包装类型 (在普通 int 之前检查)
        if isinstance(value, Byte):
            return TAG_BYTE
        if isinstance(value, Short):
            return TAG_SHORT
        if isinstance(value, Int):
            return TAG_INT
        if isinstance(value, Long):
            return TAG_LONG
        # 浮点数包装类型 (在普通 float 之前检查)
        if isinstance(value, Float):
            return TAG_FLOAT
        if isinstance(value, Double):
            return TAG_DOUBLE
        # 数组包装类型 (在普通 bytes/list 之前检查)
        if isinstance(value, ByteArray):
            return TAG_BYTE_ARRAY
        if isinstance(value, IntArray):
            return TAG_INT_ARRAY
        if isinstance(value, LongArray):
            return TAG_LONG_ARRAY
        # 基本类型
        if isinstance(value, int):
            return TAG_INT
        if isinstance(value, float):
            return TAG_DOUBLE
        if isinstance(value, str):
            return TAG_STRING
        if isinstance(value, (bytes, bytearray)):
            return TAG_BYTE_ARRAY
        if isinstance(value, list):
            return TAG_LIST
        if isinstance(value, dict):
            return TAG_COMPOUND
        raise InvalidTypeError(
            f"cannot infer NBT tag type for value of type {type(value).__name__}: {value!r}",
            value=value,
        )

    # ------------------------------------------------------------------
    # 获取结果
    # ------------------------------------------------------------------

    def get_bytes(self) -> bytes:
        """返回已写入的 NBT 数据。

        Returns:
            不可变的字节串。
        """
        return bytes(self.buf)


# ======================================================================
# 编解码函数
# ======================================================================


def marshal(data: Any, encoding: str = NETWORK_LITTLE_ENDIAN) -> bytes:
    """将 Python 值编码为 NBT 字节串。

    这是 NBT 编码的通用入口函数。根标签名称为空字符串 (Bedrock 网络协议约定)。

    Args:
        data: 要编码的 Python 值 (通常是 dict)。
        encoding: 字节序编码, 默认为 :data:`NETWORK_LITTLE_ENDIAN`。

    Returns:
        编码后的 NBT 字节串。

    Raises:
        InvalidTypeError: 无法从 data 推断根 Tag 类型。
        ValueError: encoding 不是有效的编码名称。

    Example::

        data = marshal({"name": "Steve", "health": Int(20)})
        data = marshal({"name": "Steve"}, encoding=LITTLE_ENDIAN)
    """
    writer = NBTWriter(encoding)
    tag_type = writer.infer_tag_type(data)
    writer.write_tag(tag_type, "")
    writer.write_payload(data, tag_type)
    return writer.get_bytes()


def unmarshal(data: bytes, encoding: str = NETWORK_LITTLE_ENDIAN) -> Any:
    """将 NBT 字节串解码为 Python 值。

    读取根标签的类型、名称 (丢弃) 和 payload, 返回 payload 对应的 Python 值。

    Args:
        data: 包含 NBT 数据的字节串。
        encoding: 字节序编码, 默认为 :data:`NETWORK_LITTLE_ENDIAN`。

    Returns:
        解码后的 Python 值 (通常是 dict, 取决于根 Tag 类型)。

    Raises:
        InvalidTagError: 根标签类型为 TAG_End 或未知类型。
        BufferOverrunError: 数据不完整。
        ValueError: encoding 不是有效的编码名称。

    Example::

        nbt = unmarshal(data)
        print(nbt)  # {'name': 'Steve', 'health': Int(20)}
    """
    reader = NBTReader(data, encoding)
    tag_type = reader._read_byte_raw("Unmarshal")
    if tag_type == TAG_END:
        raise InvalidTagError(TAG_END, reader.offset, "Unmarshal")
    if tag_type not in _VALID_TAGS:
        raise InvalidTagError(tag_type, reader.offset, "Unmarshal")
    reader.read_string()  # 读取并丢弃根标签名称
    return reader.read_payload(tag_type)


def marshal_network(data: Any) -> bytes:
    """使用网络小端序编码 NBT (Bedrock 网络协议最常用)。

    等价于 ``marshal(data, NETWORK_LITTLE_ENDIAN)``。

    Args:
        data: 要编码的 Python 值。

    Returns:
        编码后的 NBT 字节串。
    """
    return marshal(data, NETWORK_LITTLE_ENDIAN)


def unmarshal_network(data: bytes) -> Any:
    """使用网络小端序解码 NBT (Bedrock 网络协议最常用)。

    等价于 ``unmarshal(data, NETWORK_LITTLE_ENDIAN)``。

    Args:
        data: 包含 NBT 数据的字节串。

    Returns:
        解码后的 Python 值。
    """
    return unmarshal(data, NETWORK_LITTLE_ENDIAN)


def marshal_disk(data: Any) -> bytes:
    """使用标准小端序编码 NBT (磁盘存储格式)。

    等价于 ``marshal(data, LITTLE_ENDIAN)``。

    Args:
        data: 要编码的 Python 值。

    Returns:
        编码后的 NBT 字节串。
    """
    return marshal(data, LITTLE_ENDIAN)


def unmarshal_disk(data: bytes) -> Any:
    """使用标准小端序解码 NBT (磁盘存储格式)。

    等价于 ``unmarshal(data, LITTLE_ENDIAN)``。

    Args:
        data: 包含 NBT 数据的字节串。

    Returns:
        解码后的 Python 值。
    """
    return unmarshal(data, LITTLE_ENDIAN)


def marshal_big_endian(data: Any) -> bytes:
    """使用大端序编码 NBT (Java 版格式)。

    等价于 ``marshal(data, BIG_ENDIAN)``。

    Args:
        data: 要编码的 Python 值。

    Returns:
        编码后的 NBT 字节串。
    """
    return marshal(data, BIG_ENDIAN)


def unmarshal_big_endian(data: bytes) -> Any:
    """使用大端序解码 NBT (Java 版格式)。

    等价于 ``unmarshal(data, BIG_ENDIAN)``。

    Args:
        data: 包含 NBT 数据的字节串。

    Returns:
        解码后的 Python 值。
    """
    return unmarshal(data, BIG_ENDIAN)


# ======================================================================
# SNBT 解析器
# ======================================================================


class _SNBTParser:
    """SNBT (Stringified NBT) 递归下降解析器。

    支持 Minecraft 标准 SNBT 格式::

        复合标签: {key:value, key2:value2} 或 {key:value key2:value2}
        列表:     [v1, v2, v3] 或 [v1 v2 v3]
        类型化数组: [B;1,2,3] (ByteArray) [I;1,2,3] (IntArray) [L;1,2,3] (LongArray)
        字符串:   "双引号字符串" 或 '单引号字符串' 或 无引号字符串
        整数:     42 (Int) 42b (Byte) 42s (Short) 42l (Long)
        浮点数:   3.14 (Double) 3.14f (Float) 3.14d (Double)
        布尔值:   true (Byte=1) false (Byte=0)
    """

    def __init__(self, text: str) -> None:
        self.text: str = text
        self.pos: int = 0
        self.len: int = len(text)

    # ------------------------------------------------------------------
    # 字符操作辅助
    # ------------------------------------------------------------------

    def _peek(self, offset: int = 0) -> str:
        """查看当前偏移 ``offset`` 处的字符 (不消费)。"""
        idx = self.pos + offset
        if idx >= self.len:
            raise SNBTParseError(
                "unexpected end of input", self.pos, self.text
            )
        return self.text[idx]

    def _advance(self, n: int = 1) -> str:
        """消费 ``n`` 个字符, 返回最后一个消费的字符。"""
        if self.pos + n > self.len:
            raise SNBTParseError(
                "unexpected end of input", self.pos, self.text
            )
        consumed = self.text[self.pos:self.pos + n]
        self.pos += n
        return consumed[-1]

    def _skip_whitespace(self) -> None:
        """跳过空白字符 (空格、制表符、换行、回车)。"""
        while self.pos < self.len and self.text[self.pos] in " \t\n\r":
            self.pos += 1

    def _expect(self, char: str) -> None:
        """期望当前字符为 ``char``, 消费它; 否则抛出异常。"""
        if self.pos >= self.len or self.text[self.pos] != char:
            actual = self.text[self.pos] if self.pos < self.len else "EOF"
            raise SNBTParseError(
                f"expected {char!r} but got {actual!r}",
                self.pos,
                self.text,
            )
        self.pos += 1

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def parse(self) -> Any:
        """解析整个 SNBT 文本, 返回对应的 Python 值。

        Returns:
            解析后的 Python 值 (通常是 dict)。

        Raises:
            SNBTParseError: 解析错误。
        """
        self._skip_whitespace()
        value = self._parse_value()
        self._skip_whitespace()
        if self.pos != self.len:
            raise SNBTParseError(
                f"unexpected trailing characters at position {self.pos}: "
                f"{self.text[self.pos:]!r}",
                self.pos,
                self.text,
            )
        return value

    # ------------------------------------------------------------------
    # 值解析
    # ------------------------------------------------------------------

    def _parse_value(self) -> Any:
        """解析一个 SNBT 值 (复合、列表、字符串、数字)。"""
        self._skip_whitespace()
        if self.pos >= self.len:
            raise SNBTParseError(
                "unexpected end of input", self.pos, self.text
            )
        c = self.text[self.pos]
        if c == "{":
            return self._parse_compound()
        if c == "[":
            return self._parse_list()
        if c == '"' or c == "'":
            return self._parse_quoted_string(c)
        return self._parse_primitive()

    def _parse_compound(self) -> dict[str, Any]:
        """解析复合标签: ``{key:value, key2:value2}``。"""
        self._expect("{")
        result: dict[str, Any] = {}
        self._skip_whitespace()
        if self.pos < self.len and self.text[self.pos] == "}":
            self.pos += 1
            return result

        while True:
            self._skip_whitespace()
            key = self._parse_key()
            self._skip_whitespace()
            self._expect(":")
            value = self._parse_value()
            result[key] = value
            self._skip_whitespace()
            if self.pos >= self.len:
                raise SNBTParseError(
                    "unexpected end of input in compound (missing '}')",
                    self.pos,
                    self.text,
                )
            c = self.text[self.pos]
            if c == ",":
                self.pos += 1
                self._skip_whitespace()
                # 允许尾随逗号
                if self.pos < self.len and self.text[self.pos] == "}":
                    self.pos += 1
                    break
            elif c == "}":
                self.pos += 1
                break
            else:
                raise SNBTParseError(
                    f"expected ',' or '}}' but got {c!r}",
                    self.pos,
                    self.text,
                )
        return result

    def _parse_key(self) -> str:
        """解析复合标签的键 (带引号或不带引号的字符串)。"""
        if self.pos >= self.len:
            raise SNBTParseError(
                "unexpected end of input, expected key",
                self.pos,
                self.text,
            )
        c = self.text[self.pos]
        if c == '"' or c == "'":
            return self._parse_quoted_string(c)
        return self._parse_unquoted_string()

    def _parse_list(self) -> Any:
        """解析列表或类型化数组: ``[v,v,v]`` 或 ``[B;v,v,v]``。"""
        self._expect("[")
        self._skip_whitespace()

        # 检查类型化数组前缀: [B;...] [I;...] [L;...]
        if self.pos + 1 < self.len and self.text[self.pos + 1] == ";":
            prefix = self.text[self.pos]
            self.pos += 2  # 跳过 "X;"
            self._skip_whitespace()
            return self._parse_typed_array(prefix)

        # 普通列表
        result: list[Any] = []
        self._skip_whitespace()
        if self.pos < self.len and self.text[self.pos] == "]":
            self.pos += 1
            return result

        while True:
            value = self._parse_value()
            result.append(value)
            self._skip_whitespace()
            if self.pos >= self.len:
                raise SNBTParseError(
                    "unexpected end of input in list (missing ']')",
                    self.pos,
                    self.text,
                )
            c = self.text[self.pos]
            if c == ",":
                self.pos += 1
                self._skip_whitespace()
                # 允许尾随逗号
                if self.pos < self.len and self.text[self.pos] == "]":
                    self.pos += 1
                    break
            elif c == "]":
                self.pos += 1
                break
            else:
                raise SNBTParseError(
                    f"expected ',' or ']' but got {c!r}",
                    self.pos,
                    self.text,
                )
        return result

    def _parse_typed_array(self, prefix: str) -> Any:
        """解析类型化数组: ``[B;1,2,3]`` (ByteArray) 等。

        Args:
            prefix: 数组类型前缀 ('B', 'I', 'L')。
        """
        values: list[int] = []
        self._skip_whitespace()
        if self.pos < self.len and self.text[self.pos] == "]":
            self.pos += 1
        else:
            while True:
                value = self._parse_value()
                if not isinstance(value, int):
                    raise SNBTParseError(
                        f"typed array element must be integer, got {type(value).__name__}",
                        self.pos,
                        self.text,
                    )
                values.append(int(value))
                self._skip_whitespace()
                if self.pos >= self.len:
                    raise SNBTParseError(
                        "unexpected end of input in typed array (missing ']')",
                        self.pos,
                        self.text,
                    )
                c = self.text[self.pos]
                if c == ",":
                    self.pos += 1
                    self._skip_whitespace()
                    if self.pos < self.len and self.text[self.pos] == "]":
                        self.pos += 1
                        break
                elif c == "]":
                    self.pos += 1
                    break
                else:
                    raise SNBTParseError(
                        f"expected ',' or ']' but got {c!r}",
                        self.pos,
                        self.text,
                    )

        if prefix in ("B", "b"):
            return ByteArray(bytes(v & 0xFF for v in values))
        if prefix in ("I", "i"):
            return IntArray(values)
        if prefix in ("L", "l"):
            return LongArray(values)
        raise SNBTParseError(
            f"unknown typed array prefix {prefix!r}",
            self.pos,
            self.text,
        )

    def _parse_quoted_string(self, quote: str) -> str:
        """解析带引号的字符串 (支持转义)。"""
        self._expect(quote)
        chars: list[str] = []
        while True:
            if self.pos >= self.len:
                raise SNBTParseError(
                    f"unterminated string (missing closing {quote!r})",
                    self.pos,
                    self.text,
                )
            c = self.text[self.pos]
            if c == "\\":
                # 转义字符
                self.pos += 1
                if self.pos >= self.len:
                    raise SNBTParseError(
                        "unexpected end of input after escape",
                        self.pos,
                        self.text,
                    )
                escaped = self.text[self.pos]
                escape_map = {
                    "n": "\n",
                    "r": "\r",
                    "t": "\t",
                    "b": "\b",
                    "f": "\f",
                    "\\": "\\",
                    "'": "'",
                    '"': '"',
                    "/": "/",
                }
                chars.append(escape_map.get(escaped, escaped))
                self.pos += 1
            elif c == quote:
                self.pos += 1
                break
            else:
                chars.append(c)
                self.pos += 1
        return "".join(chars)

    def _parse_unquoted_string(self) -> str:
        """解析不带引号的字符串 (由字母、数字、下划线等组成)。"""
        start = self.pos
        while self.pos < self.len:
            c = self.text[self.pos]
            if c in "{}[]:,\"' \t\n\r":
                break
            self.pos += 1
        if self.pos == start:
            raise SNBTParseError(
                f"expected unquoted string but got {self.text[self.pos]!r}",
                self.pos,
                self.text,
            )
        return self.text[start:self.pos]

    def _parse_primitive(self) -> Any:
        """解析原始值 (数字、布尔值、无引号字符串)。"""
        token = self._parse_unquoted_string()
        return self._interpret_primitive(token)

    def _interpret_primitive(self, token: str) -> Any:
        """将原始 token 解释为 Python 值。

        解析顺序:
            1. 布尔值 (true/false)
            2. 带后缀的数字 (42b, 3.14f, 等)
            3. 纯整数
            4. 纯浮点数
            5. 字符串 (回退)
        """
        # 布尔值
        if token == "true":
            return Byte(1)
        if token == "false":
            return Byte(0)

        # 带后缀的数字
        if len(token) >= 2:
            suffix = token[-1]
            number_part = token[:-1]
            if suffix in ("b", "B"):
                value = self._try_parse_int(number_part)
                if value is not None:
                    return Byte(value)
            elif suffix in ("s", "S"):
                value = self._try_parse_int(number_part)
                if value is not None:
                    return Short(value)
            elif suffix in ("l", "L"):
                value = self._try_parse_int(number_part)
                if value is not None:
                    return Long(value)
            elif suffix in ("f", "F"):
                value = self._try_parse_float(number_part)
                if value is not None:
                    return Float(value)
            elif suffix in ("d", "D"):
                value = self._try_parse_float(number_part)
                if value is not None:
                    return Double(value)
            elif suffix in ("i", "I"):
                value = self._try_parse_int(number_part)
                if value is not None:
                    return Int(value)

        # 纯整数
        int_val = self._try_parse_int(token)
        if int_val is not None:
            return Int(int_val)

        # 纯浮点数
        float_val = self._try_parse_float(token)
        if float_val is not None:
            return Double(float_val)

        # 回退: 作为字符串
        return token

    @staticmethod
    def _try_parse_int(s: str) -> int | None:
        """尝试将字符串解析为整数, 失败返回 None。"""
        if not s:
            return None
        try:
            return int(s, 10)
        except ValueError:
            try:
                # 尝试十六进制 0x...
                if s.lower().startswith("0x"):
                    return int(s, 16)
            except ValueError:
                pass
            return None

    @staticmethod
    def _try_parse_float(s: str) -> float | None:
        """尝试将字符串解析为浮点数, 失败返回 None。

        仅当字符串包含 ``.``, ``e`` 或 ``E`` 时才视为浮点数。
        """
        if not s:
            return None
        # 必须包含 . 或 e/E 才视为浮点数
        if "." not in s and "e" not in s.lower():
            return None
        try:
            return float(s)
        except ValueError:
            return None


def parse_snbt(text: str) -> Any:
    """解析 SNBT (Stringified NBT) 字符串为 Python 值。

    SNBT 是 NBT 的文本表示格式, 类似 JSON 但有 Minecraft 特有的数字后缀。

    支持的格式::

        复合标签: {key:value, key2:value2}
        列表:     [v1, v2, v3]
        类型化数组: [B;1,2,3]  [I;1,2,3]  [L;1,2,3]
        字符串:   "双引号" 或 '单引号' 或 无引号
        整数:     42  42b(Byte)  42s(Short)  42l(Long)  42i(Int)
        浮点数:   3.14  3.14f(Float)  3.14d(Double)
        布尔值:   true  false

    Args:
        text: SNBT 格式的字符串。

    Returns:
        解析后的 Python 值 (通常是 dict)。

    Raises:
        SNBTParseError: 解析错误。

    Example::

        >>> nbt = parse_snbt('{name:"Steve",health:20b,scores:[10,20,30]}')
        >>> nbt['name']
        'Steve'
        >>> nbt['health']
        Byte(20)
        >>> nbt['scores']
        [Int(10), Int(20), Int(30)]
    """
    parser = _SNBTParser(text)
    return parser.parse()


# ======================================================================
# __all__
# ======================================================================


__all__ = [
    # Tag 类型常量
    "TAG_END",
    "TAG_BYTE",
    "TAG_SHORT",
    "TAG_INT",
    "TAG_LONG",
    "TAG_FLOAT",
    "TAG_DOUBLE",
    "TAG_BYTE_ARRAY",
    "TAG_STRING",
    "TAG_LIST",
    "TAG_COMPOUND",
    "TAG_INT_ARRAY",
    "TAG_LONG_ARRAY",
    # 字节序编码常量
    "LITTLE_ENDIAN",
    "BIG_ENDIAN",
    "NETWORK_LITTLE_ENDIAN",
    "NETWORK_BIG_ENDIAN",
    # 限制常量
    "MAX_NESTING_DEPTH",
    "MAX_NETWORK_BYTES",
    "MAX_STRING_LENGTH",
    # 异常
    "NBTError",
    "InvalidTagError",
    "BufferOverrunError",
    "InvalidTypeError",
    "InvalidStringError",
    "InvalidArraySizeError",
    "MaximumDepthReachedError",
    "MaximumBytesReadError",
    "FailedWriteError",
    "SNBTParseError",
    # Tag 包装类型
    "Byte",
    "Short",
    "Int",
    "Long",
    "Float",
    "Double",
    "ByteArray",
    "IntArray",
    "LongArray",
    # Reader / Writer
    "NBTReader",
    "NBTWriter",
    # 编解码函数
    "marshal",
    "unmarshal",
    "marshal_network",
    "unmarshal_network",
    "marshal_disk",
    "unmarshal_disk",
    "marshal_big_endian",
    "unmarshal_big_endian",
    # SNBT
    "parse_snbt",
]
