"""网易 Minecraft Bedrock 批量数据包压缩 — flate/zlib 实现。

Minecraft Bedrock 官方协议使用 **Snappy** 压缩批量数据包, 但网易租赁服
(NetEase) 改用 **flate/zlib** 压缩。本模块实现网易版本的批量数据包压缩
与解压, 逆向自 neomega 与 NovaBuilder (PhoenixBuilder/StarShuttler)。

批量数据包压缩格式::

    +---------------------+---------------------------+---------------------+
    | 1 字节: 压缩算法 ID | Varint: 原始数据长度       | 压缩后的数据        |
    +---------------------+---------------------------+---------------------+

压缩算法 ID 取值:

    ======  ====================  =======================================
    ID      算法                    说明
    ======  ====================  =======================================
    0       不压缩                 原始数据直接拼接 (仍然带长度前缀)
    1       flate / zlib           网易租赁服使用的算法 (本模块默认实现)
    2       snappy                 官方 Bedrock 算法 (网易不用, 此处未实现)
    ======  ====================  =======================================

关键设计点:
    - 网易压缩与官方 Bedrock 压缩格式不同, 不能直接互换
    - 算法 ID 与 Bedrock 官方的 :data:`~.BEDROCK_COMPRESSION_ALGORITHM` 一致
    - Varint 长度前缀使用无符号 32 位 Varint (与 Bedrock 协议一致)
    - 使用 Python 标准库 ``zlib``, 不依赖任何外部二进制

基本用法::

    from app.protocol.compression import compress_batch, decompress_batch

    # 压缩
    compressed = compress_batch(b"hello world" * 100)
    # 解压
    raw = decompress_batch(compressed)
    assert raw == b"hello world" * 100

逆向来源:
    - neomega ``minecraft/compression`` (Go)
    - NovaBuilder ``BedrockCompress`` (C#)
"""

from __future__ import annotations

import zlib

from .varint import decode_varuint32, encode_varuint32

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 压缩算法 ID — 不压缩 (原始数据直接拼接)
COMPRESSION_NONE: int = 0

#: 压缩算法 ID — flate / zlib (网易租赁服使用)
COMPRESSION_FLATE: int = 1

#: 压缩算法 ID — snappy (官方 Bedrock 算法, 网易不用)
COMPRESSION_SNAPPY: int = 2

#: 默认 zlib 压缩级别 (``-1`` 表示使用 zlib 默认值 6)
DEFAULT_COMPRESSION_LEVEL: int = -1

#: zlib 解压最大输出长度 (32MB, 防止解压炸弹)
#: 网易批量数据包通常远小于该值, 此处仅作为安全上限
MAX_DECOMPRESSED_SIZE: int = 32 * 1024 * 1024


# ----------------------------------------------------------------------
# 底层 zlib 压缩/解压
# ----------------------------------------------------------------------

def compress(data: bytes, level: int = DEFAULT_COMPRESSION_LEVEL) -> bytes:
    """网易 flate 压缩 (底层 zlib ``deflate``)。

    对原始数据执行 zlib ``deflate`` 压缩, 返回 **不含** zlib 头/尾的
    纯压缩流 (与网易服务器交互的格式一致)。

    Args:
        data: 要压缩的原始字节串。
        level: zlib 压缩级别 ``0~9`` 或 ``-1`` (默认 6)。

    Returns:
        压缩后的字节串 (不含 zlib 头/尾)。

    Raises:
        ValueError: 压缩级别不合法或数据过大。
        zlib.error: 底层 zlib 压缩失败。

    Note:
        网易使用的 ``flate`` 实际上是 ``zlib.compressobj`` 的 ``Z_SYNC_FLUSH``
        产物, 与标准 :func:`zlib.compress` 不同 — 后者包含 zlib 头 (``0x78``)
        与 adler32 尾。本函数返回的是 **纯 deflate 流**, 便于嵌入批量数据包。
    """
    if not -1 <= level <= 9:
        raise ValueError(f"非法 zlib 压缩级别: {level} (允许 -1~9)")

    if not data:
        # 空数据返回最小 deflate 流 (一个空 block 的 fixed Huffman 编码)
        compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
        return compressor.flush(zlib.Z_FINISH)

    compressor = zlib.compressobj(level, zlib.DEFLATED, -15)
    compressed = compressor.compress(data)
    compressed += compressor.flush(zlib.Z_FINISH)
    return compressed


def decompress(data: bytes) -> bytes:
    """网易 flate 解压 (底层 zlib ``inflate``)。

    解压由 :func:`compress` 生成的纯 deflate 流, 或任何兼容的
    zlib raw deflate 数据。

    Args:
        data: 压缩的字节串 (不含 zlib 头/尾)。

    Returns:
        解压后的原始字节串。

    Raises:
        ValueError: 数据为空或解压后超过 :data:`MAX_DECOMPRESSED_SIZE`。
        zlib.error: 底层 zlib 解压失败 (数据损坏或不完整)。

    Note:
        本函数使用 ``zlib.decompressobj(-15)`` 解压 raw deflate 流, 不接受
        标准 zlib 流 (带 ``0x78`` 头)。如需解压标准 zlib 流, 请使用
        :func:`zlib.decompress`。
    """
    if not data:
        raise ValueError("压缩数据为空, 无法解压")

    decompressor = zlib.decompressobj(-15)
    result = decompressor.decompress(data, MAX_DECOMPRESSED_SIZE)
    # 检查是否触达长度上限 (zlib 在达到 max_length 时会暂停, 需检查 unused_data)
    result += decompressor.flush()
    if decompressor.unconsumed_tail:
        raise ValueError(
            f"解压后数据超过最大长度 {MAX_DECOMPRESSED_SIZE} 字节 "
            f"(仍有 {len(decompressor.unconsumed_tail)} 字节未消费)"
        )
    return result


# ----------------------------------------------------------------------
# 批量数据包压缩/解压 (带算法 ID 与长度前缀)
# ----------------------------------------------------------------------

def compress_batch(data: bytes, level: int = DEFAULT_COMPRESSION_LEVEL) -> bytes:
    """压缩批量数据包 (带算法 ID 与 Varint 长度前缀)。

    按网易 Bedrock 批量数据包格式压缩数据::

        [1 字节: 算法 ID] [Varuint32: 原始数据长度] [压缩数据]

    对于空数据或非常短的数据, 自动选择 ``COMPRESSION_NONE`` 以避免负压缩。

    Args:
        data: 原始批量数据 (通常是多个 Bedrock 数据包拼接而成)。
        level: zlib 压缩级别, 仅在选用 flate 算法时生效。

    Returns:
        压缩后的字节串, 可直接发送给网易租赁服。

    Raises:
        ValueError: 压缩级别不合法。
        zlib.error: 底层 zlib 压缩失败。
    """
    if not data:
        # 空数据使用 "不压缩" 算法, 长度前缀为 0
        return bytes([COMPRESSION_NONE]) + encode_varuint32(0)

    # 先尝试 flate 压缩, 若压缩后反而变大则改用不压缩
    compressed = compress(data, level=level)

    # +1 (算法 ID) + Varint 长度 (最多 5 字节) < 6 字节开销
    if len(compressed) + 6 >= len(data):
        # 负压缩, 直接使用不压缩
        return bytes([COMPRESSION_NONE]) + encode_varuint32(len(data)) + data

    return (
        bytes([COMPRESSION_FLATE])
        + encode_varuint32(len(data))
        + compressed
    )


def decompress_batch(data: bytes) -> bytes:
    """解压批量数据包 (带算法 ID 与 Varint 长度前缀)。

    按网易 Bedrock 批量数据包格式解压数据::

        [1 字节: 算法 ID] [Varuint32: 原始数据长度] [压缩数据]

    Args:
        data: 压缩的批量数据包字节串。

    Returns:
        解压后的原始批量数据。

    Raises:
        ValueError: 数据过短、算法 ID 不支持、长度不匹配或解压后超过上限。
        zlib.error: 底层 zlib 解压失败。
    """
    if len(data) < 1:
        raise ValueError("批量数据包为空, 至少需要 1 字节算法 ID")

    algorithm_id = data[0]
    payload = data[1:]

    if algorithm_id == COMPRESSION_NONE:
        # 不压缩: 直接读取 Varint 长度 + 原始数据
        original_len, offset = decode_varuint32(payload, 0)
        raw = payload[offset:]
        if len(raw) != original_len:
            raise ValueError(
                f"不压缩批量数据包长度不匹配: 声明 {original_len} 字节, "
                f"实际 {len(raw)} 字节"
            )
        return raw

    if algorithm_id == COMPRESSION_FLATE:
        # flate/zlib: 读取 Varint 长度 + 压缩数据
        original_len, offset = decode_varuint32(payload, 0)
        compressed = payload[offset:]
        if original_len > MAX_DECOMPRESSED_SIZE:
            raise ValueError(
                f"声明的解压长度 {original_len} 超过最大值 "
                f"{MAX_DECOMPRESSED_SIZE}"
            )
        raw = decompress(compressed)
        if len(raw) != original_len:
            raise ValueError(
                f"flate 批量数据包长度不匹配: 声明 {original_len} 字节, "
                f"实际解压 {len(raw)} 字节"
            )
        return raw

    if algorithm_id == COMPRESSION_SNAPPY:
        raise ValueError(
            "网易租赁服不使用 snappy 压缩 (算法 ID=2), "
            "请检查数据来源是否为官方 Bedrock 服务器"
        )

    raise ValueError(
        f"未知的压缩算法 ID: {algorithm_id} (仅支持 0=不压缩, 1=flate)"
    )


# ----------------------------------------------------------------------
# 辅助: 算法 ID 解析
# ----------------------------------------------------------------------

def algorithm_name(algorithm_id: int) -> str:
    """返回压缩算法 ID 对应的可读名称。

    Args:
        algorithm_id: 压缩算法 ID (0/1/2)。

    Returns:
        算法名称字符串, 如 ``"flate"`` / ``"none"`` / ``"snappy"`` / ``"unknown"``。
    """
    return {
        COMPRESSION_NONE: "none",
        COMPRESSION_FLATE: "flate",
        COMPRESSION_SNAPPY: "snappy",
    }.get(algorithm_id, "unknown")


__all__ = [
    # 常量
    "COMPRESSION_NONE",
    "COMPRESSION_FLATE",
    "COMPRESSION_SNAPPY",
    "DEFAULT_COMPRESSION_LEVEL",
    "MAX_DECOMPRESSED_SIZE",
    # 底层 zlib
    "compress",
    "decompress",
    # 批量数据包
    "compress_batch",
    "decompress_batch",
    # 辅助
    "algorithm_name",
]
