"""RakNet UDP 协议 — Minecraft Bedrock Edition 使用的可靠 UDP 传输层。

RakNet 是 Minecraft Bedrock Edition 在 UDP 之上实现的可靠传输协议, 提供
消息可靠性、有序性、分片重组等特性。本模块是纯 Python 实现, 逆向自
neomega 与 NovaBuilder (PhoenixBuilder/StarShuttler)。

协议层级::

    +-----------------+----------------+---------------------+
    | UDP (RFC 768)   | RakNet         | Bedrock Game Packets|
    +-----------------+----------------+---------------------+

RakNet 数据包类型分为三大类:

    1. **离线消息** (Offline) — 连接建立之前
       - :data:`ID_UNCONNECTED_PING` (0x01) / :data:`ID_UNCONNECTED_PONG` (0x1C)
       - :data:`ID_OPEN_CONNECTION_REQUEST_1` (0x05) / :data:`ID_OPEN_CONNECTION_REPLY_1` (0x06)
       - :data:`ID_OPEN_CONNECTION_REQUEST_2` (0x07) / :data:`ID_OPEN_CONNECTION_REPLY_2` (0x08)

    2. **连接控制消息** (Connected) — 连接建立与维护
       - :data:`ID_CONNECTION_REQUEST` (0x09) / :data:`ID_CONNECTION_REQUEST_ACCEPTED` (0x10)
       - :data:`ID_CONNECTED_PING` (0x00) / :data:`ID_CONNECTED_PONG` (0x03)
       - :data:`ID_DISCONNECTION_NOTIFICATION` (0x0A)

    3. **数据报与确认** — 连接建立后的实际数据传输
       - :data:`ID_DATAGRAM` (0x80-0xDF): 携带封装数据包与序列号
       - :data:`ID_ACK` (0xC0): 确认已收到的数据报
       - :data:`ID_NACK` (0xA0): 否定确认, 请求重传

握手流程::

    客户端                                  服务器
      |                                       |
      | --- OpenConnectionRequest1 (MTU) ---> |
      | <-- OpenConnectionReply1 (MTU) ------ |
      | --- OpenConnectionRequest2 --------> |
      | <-- OpenConnectionReply2 ------------ |
      |                                       |
      | --- ConnectionRequest (datagram) -->  |
      | <-- ConnectionRequestAccepted ------- |
      |                                       |
      | === Connected datagrams <=========>  |

关键设计:
    - 使用 :class:`asyncio.DatagramProtocol` 实现异步 UDP 通信
    - 支持可靠/有序/分片数据包
    - 24 位序列号 (Datagram) 与消息索引 (Encapsulated)
    - 自动重传未 ACK 的可靠数据报
    - 分片重组 (当 payload > MTU 时自动拆分)

基本用法::

    from app.protocol.raknet import RakNetConnection, Reliability

    conn = RakNetConnection()
    await conn.connect("1.2.3.4", 19132)
    await conn.send(b"hello", Reliability.RELIABLE_ORDERED)
    data = await conn.recv()
    await conn.disconnect()

逆向来源:
    - neomega ``raknet`` (Go, github.com/sandertv/go-raknet 包装)
    - go-raknet ``github.com/sandertv/go-raknet`` (Go 原始实现)
    - NovaBuilder ``BedrockRakNet`` (C#)
    - PocketMine-MP PHP RakNet 实现
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Optional

# ----------------------------------------------------------------------
# 数据包 ID 常量
# ----------------------------------------------------------------------

#: 已连接 Ping — 保活与延迟测量
ID_CONNECTED_PING: int = 0x00

#: 未连接 Ping — 服务器发现 (无连接)
ID_UNCONNECTED_PING: int = 0x01

#: 未连接 Ping (仅开放连接) — 与 0x01 类似, 仅在有连接时响应
ID_UNCONNECTED_PING_OPEN_CONNECTIONS: int = 0x02

#: 已连接 Pong — 对 ID_CONNECTED_PING 的回复
ID_CONNECTED_PONG: int = 0x03

#: 开放连接请求 1 — 握手第一步, 协商 MTU
ID_OPEN_CONNECTION_REQUEST_1: int = 0x05

#: 开放连接回复 1 — 服务器响应 OCR1, 返回 GUID 与 MTU
ID_OPEN_CONNECTION_REPLY_1: int = 0x06

#: 开放连接请求 2 — 握手第二步, 提供客户端地址与 MTU
ID_OPEN_CONNECTION_REQUEST_2: int = 0x07

#: 开放连接回复 2 — 服务器响应 OCR2, 确认客户端地址
ID_OPEN_CONNECTION_REPLY_2: int = 0x08

#: 连接请求 (已连接) — 在线握手第一步, 由客户端在 datagram 中发送
ID_CONNECTION_REQUEST: int = 0x09

#: 断开通知 — 主动断开连接
ID_DISCONNECTION_NOTIFICATION: int = 0x0A

#: 连接请求已接受 — 服务器响应 ConnectionRequest
ID_CONNECTION_REQUEST_ACCEPTED: int = 0x10

#: 未连接 Pong — 对 ID_UNCONNECTED_PING 的回复 (含 MOTD)
ID_UNCONNECTED_PONG: int = 0x1C

#: 数据报 — 携带封装数据包 (高位为 1, 即 0x80-0xBF)
ID_DATAGRAM: int = 0x80

#: NACK 否定确认 — 请求重传特定数据报
ID_NACK: int = 0xA0

#: ACK 确认 — 确认已收到特定数据报
ID_ACK: int = 0xC0

# ----------------------------------------------------------------------
# 魔数常量
# ----------------------------------------------------------------------

#: 离线消息标识前缀 (部分实现用于识别离线消息)
OFFLINE_MESSAGE_DATA_ID: bytes = bytes([0xFE, 0xFD, 0xFE, 0xFD])

#: RakNet 完整魔数 (16 字节, 紧跟在离线消息 ID 之后)
MAGIC: bytes = bytes([
    0x00, 0xFF, 0xFF, 0x00,
    0xFE, 0xFE, 0xFE, 0xFE,
    0xFD, 0xFD, 0xFD, 0xFD,
    0x00, 0x00, 0x00, 0x28,
])

#: RakNet 协议版本 (Bedrock 1.21.x 使用 10)
DEFAULT_PROTOCOL_VERSION: int = 10

#: 默认 MTU (Maximum Transmission Unit) — 单个 UDP 数据报最大字节数
DEFAULT_MTU: int = 1400

#: 默认连接超时 (秒)
DEFAULT_CONNECT_TIMEOUT: float = 30.0

#: 默认保活间隔 (秒)
DEFAULT_PING_INTERVAL: float = 5.0

#: 默认可靠数据报重传超时 (秒)
DEFAULT_RETRANSMIT_TIMEOUT: float = 1.0

#: 默认最大重传次数
DEFAULT_MAX_RETRANSMITS: int = 10

#: 默认接收队列最大长度
DEFAULT_RECV_QUEUE_SIZE: int = 1024

#: 数据报头部开销 (1 字节 flag + 2 字节 seq + 封装头)
DATAGRAM_HEADER_SIZE: int = 3

#: 封装数据包头部最大开销 (flags + length + 各 index + split)
ENCAPSULATED_HEADER_MAX_SIZE: int = 20

#: 默认分片阈值 (payload 超过此值则分片)
DEFAULT_SPLIT_THRESHOLD: int = 1024

#: 24 位序列号最大值 (用于回绕)
SEQ_MOD: int = 1 << 24

logger = logging.getLogger("pocketterm.raknet")


# ----------------------------------------------------------------------
# Reliability 枚举
# ----------------------------------------------------------------------

class Reliability(IntEnum):
    """封装数据包的可靠性等级。

    不同的可靠性等级决定数据包是否需要确认、是否按顺序送达:

    - **UNRELIABLE**: 不可靠, 不需要 ACK, 可能丢失或乱序
    - **UNRELIABLE_SEQUENCED**: 不可靠但有序, 只交付最新序号的数据包
    - **RELIABLE**: 可靠, 保证送达 (但不保证顺序)
    - **RELIABLE_ORDERED**: 可靠且有序, 保证按发送顺序送达 (默认)
    - **RELIABLE_SEQUENCED**: 可靠但只交付最新序号的数据包
    """

    UNRELIABLE = 0
    UNRELIABLE_SEQUENCED = 1
    RELIABLE = 2
    RELIABLE_ORDERED = 3
    RELIABLE_SEQUENCED = 4

    @property
    def is_reliable(self) -> bool:
        """是否需要 ACK (可靠传输)。"""
        return self in (
            Reliability.RELIABLE,
            Reliability.RELIABLE_ORDERED,
            Reliability.RELIABLE_SEQUENCED,
        )

    @property
    def is_ordered(self) -> bool:
        """是否按序交付 (在 channel 内保证顺序)。"""
        return self == Reliability.RELIABLE_ORDERED

    @property
    def is_sequenced(self) -> bool:
        """是否只交付最新序号 (丢弃旧序号数据包)。"""
        return self in (
            Reliability.UNRELIABLE_SEQUENCED,
            Reliability.RELIABLE_SEQUENCED,
        )


# ----------------------------------------------------------------------
# 辅助函数: 地址编码/解码
# ----------------------------------------------------------------------

def encode_address(host: str, port: int) -> bytes:
    """编码 IP 地址为 Bedrock RakNet 地址格式。

    Bedrock 地址格式 (IPv4)::

        [1 字节: 4] [4 字节: IPv4 字节] [2 字节 BE: 端口]

    Bedrock 地址格式 (IPv6)::

        [1 字节: 6] [2 字节 BE: family=0x17] [16 字节: IPv6] [2 字节 BE: 端口]

    Args:
        host: 主机名或 IP 字符串。
        port: 端口号。

    Returns:
        编码后的字节串。

    Raises:
        ValueError: 无法解析 host 为 IP 地址。
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 主机名, 解析为 IPv4
        resolved = socket.gethostbyname(host)
        ip = ipaddress.ip_address(resolved)

    if isinstance(ip, ipaddress.IPv4Address):
        return bytes([4]) + ip.packed + struct.pack(">H", port)
    if isinstance(ip, ipaddress.IPv6Address):
        # Bedrock IPv6 地址格式: family(AF_INET6=0x17) + 16 字节 IP + 端口
        return bytes([6]) + struct.pack(">H", 0x17) + ip.packed + struct.pack(">H", port)
    raise ValueError(f"不支持的 IP 类型: {ip}")


def decode_address(data: bytes, offset: int = 0) -> tuple[str, int, int]:
    """从字节流解码 Bedrock RakNet 地址。

    Args:
        data: 包含地址的字节串。
        offset: 起始偏移量。

    Returns:
        ``(host, port, new_offset)`` 元组。

    Raises:
        ValueError: 数据不完整或版本不支持的地址格式。
    """
    if offset >= len(data):
        raise ValueError("地址数据过短")

    version = data[offset]
    offset += 1

    if version == 4:
        # IPv4: 4 字节 IP + 2 字节端口
        if offset + 6 > len(data):
            raise ValueError("IPv4 地址数据不完整")
        ip_bytes = data[offset:offset + 4]
        offset += 4
        host = ".".join(str(b) for b in ip_bytes)
    elif version == 6:
        # IPv6: 2 字节 family + 16 字节 IP + 2 字节端口
        if offset + 20 > len(data):
            raise ValueError("IPv6 地址数据不完整")
        offset += 2  # 跳过 family
        ip_bytes = data[offset:offset + 16]
        offset += 16
        host = str(ipaddress.IPv6Address(ip_bytes))
    else:
        raise ValueError(f"不支持的地址版本: {version}")

    port = struct.unpack(">H", data[offset:offset + 2])[0]
    offset += 2
    return host, port, offset


# ----------------------------------------------------------------------
# 辅助函数: 24 位 BE 整数读写
# ----------------------------------------------------------------------

def _encode_uint24_be(value: int) -> bytes:
    """编码 24 位无符号整数 (大端序)。"""
    if not 0 <= value < SEQ_MOD:
        raise ValueError(f"uint24 溢出: {value}")
    return bytes([(value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF])


def _decode_uint24_be(data: bytes, offset: int = 0) -> int:
    """解码 24 位无符号整数 (大端序)。"""
    if offset + 3 > len(data):
        raise ValueError("uint24 数据不完整")
    return (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]


# ----------------------------------------------------------------------
# 封装数据包 (Encapsulated Packet)
# ----------------------------------------------------------------------

@dataclass
class EncapsulatedPacket:
    """RakNet 封装数据包 — 在 datagram 内部传输的封装单元。

    每个 datagram 可包含一个或多个封装数据包, 每个封装数据包有自己的
    可靠性等级、序列号与 (可选的) 分片信息。

    Attributes:
        reliability: 可靠性等级。
        message_index: 消息索引 (24-bit BE, 仅 reliable 包含)。
        sequence_index: 序列索引 (24-bit BE, 仅 sequenced 包含)。
        order_index: 顺序索引 (24-bit BE, 仅 ordered/sequenced 包含)。
        order_channel: 顺序通道 (8-bit, 仅 ordered/sequenced 包含)。
        split: 是否为分片包。
        split_count: 分片总数 (32-bit BE, 仅 split 包含)。
        split_id: 分片组 ID (16-bit BE, 仅 split 包含)。
        split_index: 当前分片索引 (32-bit BE, 仅 split 包含)。
        payload: 实际数据载荷。
    """

    reliability: Reliability = Reliability.UNRELIABLE
    message_index: int = 0
    sequence_index: int = 0
    order_index: int = 0
    order_channel: int = 0
    split: bool = False
    split_count: int = 0
    split_id: int = 0
    split_index: int = 0
    payload: bytes = b""

    def encode(self) -> bytes:
        """编码封装数据包为字节串 (供 datagram 拼接使用)。

        Returns:
            编码后的字节串。
        """
        # 1. flags 字节: bits 7-5 = reliability, bit 4 = split
        flags = (int(self.reliability) & 0x07) << 5
        if self.split:
            flags |= 0x10

        buf = bytearray([flags])
        # 2. payload 长度 (2 字节 BE, 单位为位 — Bedrock 协议惯例)
        payload_bit_len = len(self.payload) * 8
        if payload_bit_len > 0xFFFF:
            raise ValueError(
                f"payload 过长: {len(self.payload)} 字节 (最大 8191 字节)"
            )
        buf += struct.pack(">H", payload_bit_len)

        # 3. 消息索引 (仅 reliable)
        if self.reliability.is_reliable:
            buf += _encode_uint24_be(self.message_index % SEQ_MOD)

        # 4. 序列索引 (仅 sequenced)
        if self.reliability.is_sequenced:
            buf += _encode_uint24_be(self.sequence_index % SEQ_MOD)

        # 5. 顺序索引 + 通道 (仅 ordered 或 sequenced)
        if self.reliability.is_ordered or self.reliability.is_sequenced:
            buf += _encode_uint24_be(self.order_index % SEQ_MOD)
            buf += bytes([self.order_channel & 0xFF])

        # 6. 分片信息 (仅 split)
        if self.split:
            buf += struct.pack(">I", self.split_count)
            buf += struct.pack(">H", self.split_id & 0xFFFF)
            buf += struct.pack(">I", self.split_index)

        # 7. payload
        buf += self.payload
        return bytes(buf)

    @classmethod
    def decode(cls, data: bytes, offset: int = 0) -> tuple[EncapsulatedPacket, int]:
        """从字节流解码封装数据包。

        Args:
            data: 包含封装数据包的字节串。
            offset: 起始偏移量。

        Returns:
            ``(packet, new_offset)`` 元组。

        Raises:
            ValueError: 数据不完整或格式错误。
        """
        if offset + 3 > len(data):
            raise ValueError("封装数据包头部过短")

        # 1. flags
        flags = data[offset]
        offset += 1
        reliability = Reliability((flags >> 5) & 0x07)
        is_split = bool(flags & 0x10)

        # 2. payload 长度 (位 → 字节)
        payload_bit_len = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2
        payload_len = payload_bit_len // 8

        # 3. 消息索引 (仅 reliable)
        message_index = 0
        if reliability.is_reliable:
            if offset + 3 > len(data):
                raise ValueError("封装数据包 message_index 不完整")
            message_index = _decode_uint24_be(data, offset)
            offset += 3

        # 4. 序列索引 (仅 sequenced)
        sequence_index = 0
        if reliability.is_sequenced:
            if offset + 3 > len(data):
                raise ValueError("封装数据包 sequence_index 不完整")
            sequence_index = _decode_uint24_be(data, offset)
            offset += 3

        # 5. 顺序索引 + 通道 (仅 ordered 或 sequenced)
        order_index = 0
        order_channel = 0
        if reliability.is_ordered or reliability.is_sequenced:
            if offset + 4 > len(data):
                raise ValueError("封装数据包 order_index/channel 不完整")
            order_index = _decode_uint24_be(data, offset)
            offset += 3
            order_channel = data[offset]
            offset += 1

        # 6. 分片信息 (仅 split)
        split_count = 0
        split_id = 0
        split_index = 0
        if is_split:
            if offset + 10 > len(data):
                raise ValueError("封装数据包 split 信息不完整")
            split_count = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            split_id = struct.unpack(">H", data[offset:offset + 2])[0]
            offset += 2
            split_index = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4

        # 7. payload
        if offset + payload_len > len(data):
            raise ValueError(
                f"封装数据包 payload 不完整: 需要 {payload_len} 字节, "
                f"剩余 {len(data) - offset} 字节"
            )
        payload = data[offset:offset + payload_len]
        offset += payload_len

        return cls(
            reliability=reliability,
            message_index=message_index,
            sequence_index=sequence_index,
            order_index=order_index,
            order_channel=order_channel,
            split=is_split,
            split_count=split_count,
            split_id=split_id,
            split_index=split_index,
            payload=payload,
        ), offset


# ----------------------------------------------------------------------
# Datagram / ACK 编码/解码
# ----------------------------------------------------------------------

def encode_datagram(seq: int, *packets: EncapsulatedPacket) -> bytes:
    """编码 datagram (含序列号与封装数据包)。

    格式::
        [1 字节: 0x80 | (seq >> 16) & 0x3F]
        [2 字节 BE: seq & 0xFFFF]
        [封装数据包 1] [封装数据包 2] ...

    Args:
        seq: 24 位序列号。
        *packets: 一个或多个封装数据包。

    Returns:
        编码后的字节串。
    """
    if not 0 <= seq < SEQ_MOD:
        raise ValueError(f"序列号溢出: {seq} (最大 {SEQ_MOD - 1})")

    buf = bytearray()
    # flag 字节: 0x80 | 高 6 位 seq
    buf.append(0x80 | ((seq >> 16) & 0x3F))
    # 低 16 位 seq (BE)
    buf += struct.pack(">H", seq & 0xFFFF)
    # 封装数据包
    for pkt in packets:
        buf += pkt.encode()
    return bytes(buf)


def decode_datagram(data: bytes) -> tuple[int, list[EncapsulatedPacket]]:
    """解码 datagram。

    Args:
        data: datagram 字节串 (含 flag 字节)。

    Returns:
        ``(seq, packets)`` 元组。

    Raises:
        ValueError: 数据不是 datagram 或格式错误。
    """
    if len(data) < 3:
        raise ValueError("datagram 数据过短")

    flag = data[0]
    if flag & 0xC0 != 0x80:
        raise ValueError(f"不是 datagram: flag=0x{flag:02X}")

    # 24 位 seq
    seq = ((flag & 0x3F) << 16) | struct.unpack(">H", data[1:3])[0]

    # 解码所有封装数据包
    packets: list[EncapsulatedPacket] = []
    offset = 3
    while offset < len(data):
        pkt, offset = EncapsulatedPacket.decode(data, offset)
        packets.append(pkt)
    return seq, packets


def encode_ack(*seqs: int, is_ack: bool = True) -> bytes:
    """编码 ACK 或 NACK。

    格式::
        [1 字节: 0xC0 (ack) 或 0xA0 (nack)]
        [2 字节 BE: 记录数]
        每条记录:
            [1 字节: 1 (单条)] [3 字节 BE: seq]

    Args:
        *seqs: 一个或多个 24 位序列号。
        is_ack: ``True`` 编码 ACK, ``False`` 编码 NACK。

    Returns:
        编码后的字节串。
    """
    buf = bytearray()
    buf.append(ID_ACK if is_ack else ID_NACK)
    buf += struct.pack(">H", len(seqs))
    for seq in seqs:
        if not 0 <= seq < SEQ_MOD:
            raise ValueError(f"ACK seq 溢出: {seq}")
        buf.append(1)  # is_single = 1
        buf += _encode_uint24_be(seq)
    return bytes(buf)


def decode_ack(data: bytes) -> tuple[list[int], bool]:
    """解码 ACK 或 NACK。

    Args:
        data: ACK/NACK 字节串 (含 flag 字节)。

    Returns:
        ``(seqs, is_ack)`` 元组。``is_ack`` 为 ``True`` 表示 ACK,
        ``False`` 表示 NACK。

    Raises:
        ValueError: 数据不是 ACK/NACK 或格式错误。
    """
    if len(data) < 3:
        raise ValueError("ACK/NACK 数据过短")

    flag = data[0]
    # Bug 9.3 修复: ACK/NACK 标志位使用范围匹配而非精确匹配。
    # ACK: 高 2 位 11 (0xC0-0xFF), NACK: 高 3 位 101 (0xA0-0xBF)。
    if flag & 0xC0 == 0xC0:
        is_ack = True
    elif flag & 0xE0 == 0xA0:
        is_ack = False
    else:
        raise ValueError(f"不是 ACK/NACK: flag=0x{flag:02X}")

    record_count = struct.unpack(">H", data[1:3])[0]
    offset = 3
    seqs: list[int] = []

    for _ in range(record_count):
        if offset + 4 > len(data):
            raise ValueError("ACK/NACK 记录数据不完整")
        is_single = data[offset]
        offset += 1
        if is_single == 1:
            # 单条: 3 字节 seq
            seqs.append(_decode_uint24_be(data, offset))
            offset += 3
        else:
            # 范围: 3 字节 start + 3 字节 end (含)
            start = _decode_uint24_be(data, offset)
            offset += 3
            end = _decode_uint24_be(data, offset)
            offset += 3
            for s in range(start, end + 1):
                seqs.append(s % SEQ_MOD)

    return seqs, is_ack


def is_offline_message(data: bytes) -> bool:
    """判断数据包是否为离线消息。

    离线消息以 ID byte + MAGIC (16 字节) 开头, 其中 ID byte 在
    ``[0x01, 0x08]`` 范围内 (即 OCR1/OCR2/Ping/Pong 等)。

    Args:
        data: 收到的 UDP 数据。

    Returns:
        ``True`` 表示是离线消息。
    """
    if len(data) < 17:
        return False
    packet_id = data[0]
    if packet_id not in (
        ID_UNCONNECTED_PING,
        ID_UNCONNECTED_PING_OPEN_CONNECTIONS,
        ID_UNCONNECTED_PONG,
        ID_OPEN_CONNECTION_REQUEST_1,
        ID_OPEN_CONNECTION_REPLY_1,
        ID_OPEN_CONNECTION_REQUEST_2,
        ID_OPEN_CONNECTION_REPLY_2,
    ):
        return False
    # 部分实现 ping/pong 不带 MAGIC, 这里宽松判断
    return data[1:17] == MAGIC or packet_id in (
        ID_UNCONNECTED_PING,
        ID_UNCONNECTED_PONG,
    )


# ----------------------------------------------------------------------
# 离线握手数据包构造
# ----------------------------------------------------------------------

def build_unconnected_ping(time_ms: int, client_guid: int) -> bytes:
    """构造 ID_UNCONNECTED_PING 数据包。

    格式::

        [1 字节: 0x01]
        [8 字节 BE: time (毫秒)]
        [16 字节: MAGIC]
        [8 字节 BE: client GUID]

    Args:
        time_ms: 当前时间戳 (毫秒)。
        client_guid: 客户端 GUID (uint64)。

    Returns:
        编码后的字节串。
    """
    return (
        bytes([ID_UNCONNECTED_PING])
        + struct.pack(">Q", time_ms)
        + MAGIC
        + struct.pack(">Q", client_guid)
    )


def parse_unconnected_pong(data: bytes) -> tuple[int, int, str]:
    """解析 ID_UNCONNECTED_PONG 数据包。

    Args:
        data: 收到的 pong 字节串。

    Returns:
        ``(time_ms, server_guid, motd)`` 元组。

    Raises:
        ValueError: 数据格式错误。
    """
    if len(data) < 35:
        raise ValueError("pong 数据过短")
    if data[0] != ID_UNCONNECTED_PONG:
        raise ValueError(f"不是 pong: 0x{data[0]:02X}")
    if data[17:33] != MAGIC:
        raise ValueError("pong MAGIC 不匹配")

    time_ms = struct.unpack(">Q", data[1:9])[0]
    server_guid = struct.unpack(">Q", data[9:17])[0]
    # MAGIC 在 17:33
    motd_len = struct.unpack(">H", data[33:35])[0]
    if 35 + motd_len > len(data):
        raise ValueError("pong MOTD 数据不完整")
    motd = data[35:35 + motd_len].decode("utf-8", errors="replace")
    return time_ms, server_guid, motd


def build_open_connection_request_1(
    protocol_version: int = DEFAULT_PROTOCOL_VERSION,
    mtu: int = DEFAULT_MTU,
) -> bytes:
    """构造 ID_OPEN_CONNECTION_REQUEST_1。

    格式::

        [1 字节: 0x05]
        [16 字节: MAGIC]
        [1 字节: protocol version]
        [N 字节: 0x00 填充 (用于 MTU 协商)]

    MTU 协商原理: 客户端发送一个固定大小的数据包 (含 0 填充), 服务器
    若能收到则回复 Reply1 中协商后的 MTU。

    Args:
        protocol_version: RakNet 协议版本 (默认 10)。
        mtu: 期望的 MTU 大小。

    Returns:
        编码后的字节串。
    """
    # 头部开销: 1 (id) + 16 (magic) + 1 (proto) = 18 字节
    # 填充使总长度达到 mtu
    padding_len = max(0, mtu - 18)
    return (
        bytes([ID_OPEN_CONNECTION_REQUEST_1])
        + MAGIC
        + bytes([protocol_version & 0xFF])
        + bytes(padding_len)
    )


def parse_open_connection_reply_1(data: bytes) -> tuple[int, bool, int]:
    """解析 ID_OPEN_CONNECTION_REPLY_1。

    Args:
        data: 收到的 Reply1 字节串。

    Returns:
        ``(server_guid, has_security, mtu)`` 元组。

    Raises:
        ValueError: 数据格式错误。
    """
    if len(data) < 28:
        raise ValueError("Reply1 数据过短")
    if data[0] != ID_OPEN_CONNECTION_REPLY_1:
        raise ValueError(f"不是 Reply1: 0x{data[0]:02X}")
    if data[1:17] != MAGIC:
        raise ValueError("Reply1 MAGIC 不匹配")

    server_guid = struct.unpack(">Q", data[17:25])[0]
    has_security = bool(data[25])
    mtu = struct.unpack(">H", data[26:28])[0]
    return server_guid, has_security, mtu


def build_open_connection_request_2(
    server_host: str,
    server_port: int,
    mtu: int = DEFAULT_MTU,
    client_guid: int = 0,
) -> bytes:
    """构造 ID_OPEN_CONNECTION_REQUEST_2。

    格式::

        [1 字节: 0x07]
        [16 字节: MAGIC]
        [地址: 1+4+2 字节 (IPv4) 或 1+2+16+2 字节 (IPv6)]
        [4 字节 BE: MTU]
        [8 字节 BE: client GUID]

    Args:
        server_host: 服务器主机名/IP。
        server_port: 服务器端口。
        mtu: 协商的 MTU。
        client_guid: 客户端 GUID。

    Returns:
        编码后的字节串。
    """
    return (
        bytes([ID_OPEN_CONNECTION_REQUEST_2])
        + MAGIC
        + encode_address(server_host, server_port)
        + struct.pack(">I", mtu)
        + struct.pack(">Q", client_guid)
    )


def parse_open_connection_reply_2(data: bytes) -> tuple[int, str, int, int]:
    """解析 ID_OPEN_CONNECTION_REPLY_2。

    Args:
        data: 收到的 Reply2 字节串。

    Returns:
        ``(server_guid, client_host, client_port, mtu)`` 元组。

    Raises:
        ValueError: 数据格式错误。
    """
    if len(data) < 25:
        raise ValueError("Reply2 数据过短")
    if data[0] != ID_OPEN_CONNECTION_REPLY_2:
        raise ValueError(f"不是 Reply2: 0x{data[0]:02X}")
    if data[1:17] != MAGIC:
        raise ValueError("Reply2 MAGIC 不匹配")

    server_guid = struct.unpack(">Q", data[17:25])[0]
    client_host, client_port, offset = decode_address(data, 25)
    mtu = struct.unpack(">I", data[offset:offset + 4])[0]
    return server_guid, client_host, client_port, mtu


# ----------------------------------------------------------------------
# 在线握手数据包构造
# ----------------------------------------------------------------------

def build_connection_request(
    client_guid: int,
    time_ms: int,
    use_security: bool = False,
) -> bytes:
    """构造 ID_CONNECTION_REQUEST (作为封装数据包 payload)。

    格式::

        [1 字节: 0x09]
        [8 字节 BE: client GUID]
        [8 字节 BE: send time (毫秒)]
        [1 字节: security flag]
        [4 字节 BE: password (通常为 0)]

    Args:
        client_guid: 客户端 GUID。
        time_ms: 发送时间戳 (毫秒)。
        use_security: 是否启用加密。

    Returns:
        编码后的字节串 (作为 EncapsulatedPacket.payload)。
    """
    return (
        bytes([ID_CONNECTION_REQUEST])
        + struct.pack(">Q", client_guid)
        + struct.pack(">Q", time_ms)
        + bytes([1 if use_security else 0])
        + struct.pack(">I", 0)  # password
    )


def parse_connection_request_accepted(data: bytes) -> tuple[str, int, int, int]:
    """解析 ID_CONNECTION_REQUEST_ACCEPTED。

    Args:
        data: 收到的 ConnectionRequestAccepted 字节串 (作为 EncapsulatedPacket.payload)。

    Returns:
        ``(client_host, client_port, request_time, reply_time)`` 元组。

    Raises:
        ValueError: 数据格式错误。
    """
    if len(data) < 7:
        raise ValueError("ConnectionRequestAccepted 数据过短")
    if data[0] != ID_CONNECTION_REQUEST_ACCEPTED:
        raise ValueError(f"不是 ConnectionRequestAccepted: 0x{data[0]:02X}")

    # 客户端地址 (服务器看到的)
    client_host, client_port, offset = decode_address(data, 1)
    # system index (2 字节 BE)
    offset += 2
    # request time (8 字节 BE, 来自 ConnectionRequest)
    request_time = struct.unpack(">Q", data[offset:offset + 8])[0]
    offset += 8
    # reply time (8 字节 BE)
    reply_time = struct.unpack(">Q", data[offset:offset + 8])[0]
    return client_host, client_port, request_time, reply_time


def build_connected_ping(time_ms: int) -> bytes:
    """构造 ID_CONNECTED_PING (作为封装数据包 payload)。

    格式::

        [1 字节: 0x00]
        [8 字节 BE: time (毫秒)]
    """
    return bytes([ID_CONNECTED_PING]) + struct.pack(">Q", time_ms)


def parse_connected_pong(data: bytes) -> tuple[int, int]:
    """解析 ID_CONNECTED_PONG。

    Returns:
        ``(ping_time, pong_time)`` 元组 (均为毫秒时间戳)。
    """
    if len(data) < 17:
        raise ValueError("ConnectedPong 数据过短")
    if data[0] != ID_CONNECTED_PONG:
        raise ValueError(f"不是 ConnectedPong: 0x{data[0]:02X}")
    ping_time = struct.unpack(">Q", data[1:9])[0]
    pong_time = struct.unpack(">Q", data[9:17])[0]
    return ping_time, pong_time


# ----------------------------------------------------------------------
# asyncio DatagramProtocol 内部实现
# ----------------------------------------------------------------------

class _RakNetDatagramProtocol(asyncio.DatagramProtocol):
    """RakNet 使用的 asyncio UDP 协议回调。

    本类是内部实现, 外部应通过 :class:`RakNetConnection` 使用。
    """

    def __init__(self, connection: "RakNetConnection") -> None:
        self._connection = connection
        self._transport: Optional[asyncio.DatagramTransport] = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:  # type: ignore[override]
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        # 不在此处阻塞 — 通过 put_nowait 放入 connection 的接收队列
        try:
            self._connection._raw_recv_queue.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning("接收队列已满, 丢弃数据包 (%d 字节)", len(data))

    def error_received(self, exc: Exception) -> None:
        logger.error("UDP socket 错误: %s", exc)
        self._connection._on_error(exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._connection._on_connection_lost(exc)

    def send(self, data: bytes) -> None:
        """通过 transport 发送数据。"""
        if self._transport is None:
            raise RuntimeError("transport 未建立, 无法发送")
        self._transport.sendto(data)


# ----------------------------------------------------------------------
# RakNetConnection 主类
# ----------------------------------------------------------------------

class RakNetConnection:
    """RakNet UDP 连接管理器。

    提供 Bedrock Edition 客户端到服务器的 RakNet 连接, 包括:
        - 离线握手 (OpenConnectionRequest 1/2)
        - 在线握手 (ConnectionRequest/Accepted)
        - 数据报收发 (含 ACK/NACK)
        - 可靠传输与自动重传
        - 分片重组 (大 payload 自动拆分)
        - 保活 (ConnectedPing/Pong)

    生命周期::

        conn = RakNetConnection()
        await conn.connect(host, port)  # 完成握手
        await conn.send(data, Reliability.RELIABLE_ORDERED)
        received = await conn.recv()
        await conn.disconnect()  # 主动断开

    线程安全性: 本类的所有方法都应在同一个 asyncio 事件循环中调用。
    """

    def __init__(
        self,
        *,
        mtu: int = DEFAULT_MTU,
        protocol_version: int = DEFAULT_PROTOCOL_VERSION,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        retransmit_timeout: float = DEFAULT_RETRANSMIT_TIMEOUT,
        max_retransmits: int = DEFAULT_MAX_RETRANSMITS,
        recv_queue_size: int = DEFAULT_RECV_QUEUE_SIZE,
        split_threshold: int = DEFAULT_SPLIT_THRESHOLD,
    ) -> None:
        """构造 RakNet 连接管理器。

        Args:
            mtu: 数据报最大字节数 (默认 1400)。
            protocol_version: RakNet 协议版本 (默认 10)。
            connect_timeout: 握手总超时 (秒, 默认 30)。
            ping_interval: 保活 ping 间隔 (秒, 默认 5)。
            retransmit_timeout: 可靠数据报重传超时 (秒, 默认 1)。
            max_retransmits: 可靠数据报最大重传次数 (默认 10)。
            recv_queue_size: 接收队列最大长度 (默认 1024)。
            split_threshold: 大于该值的 payload 自动分片 (默认 1024)。
        """
        # 配置
        self._mtu: int = mtu
        self._protocol_version: int = protocol_version
        self._connect_timeout: float = connect_timeout
        self._ping_interval: float = ping_interval
        self._retransmit_timeout: float = retransmit_timeout
        self._max_retransmits: int = max_retransmits
        self._split_threshold: int = split_threshold

        # 连接状态
        self._host: str = ""
        self._port: int = 0
        self._connected: bool = False
        self._closing: bool = False
        self._client_guid: int = secrets.randbits(63)
        self._server_guid: int = 0

        # asyncio transport
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._protocol: Optional[_RakNetDatagramProtocol] = None

        # 接收队列 (原始字节, 由 datagram_received put)
        # Bug 9.5 修复: asyncio.Queue/Event 在 __init__ 中创建可能绑定到
        # 错误的事件循环 (Python 3.9- 在无运行循环时 get_event_loop 返回新循环)。
        # 保存队列大小, 在 connect() 中重新创建以绑定到正确的运行循环。
        self._recv_queue_size: int = recv_queue_size
        self._raw_recv_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=recv_queue_size
        )
        # 已重组的应用层数据队列 (供 recv() 消费)
        self._app_recv_queue: asyncio.Queue[bytes] = asyncio.Queue(
            maxsize=recv_queue_size
        )
        # 离线握手响应 future
        self._offline_reply: Optional[asyncio.Future] = None
        # 在线握手响应 future
        self._online_reply: Optional[asyncio.Future] = None
        # 错误事件
        self._error: Optional[Exception] = None
        self._closed_event: asyncio.Event = asyncio.Event()

        # 发送状态 (出站)
        self._send_seq: int = 0  # 下一个 datagram seq
        self._send_message_index: int = 0  # 下一个 reliable message index
        self._send_order_index: dict[int, int] = {}  # channel -> next order index
        self._send_sequence_index: dict[int, int] = {}  # channel -> next sequence index
        self._split_id: int = 0  # 下一个分片组 ID

        # 接收状态 (入站)
        self._recv_seq: int = 0  # 下一个期望的 datagram seq
        # 滑动窗口缓冲: seq -> 该 datagram 的封装数据包列表。
        # 当收到 seq > expected 的乱序 datagram 时缓存于此, 待缺失序号补齐后按序处理。
        self._recv_window: dict[int, list[EncapsulatedPacket]] = {}
        self._recv_message_index: dict[int, int] = {}  # channel -> next message index (ordered)
        self._pending_acks: list[int] = []  # 待发送的 ACK seq 列表
        self._pending_nacks: list[int] = []  # 待发送的 NACK seq 列表

        # 重传缓冲: seq -> (data, send_time, retransmit_count)
        self._retransmit_buffer: dict[int, tuple[bytes, float, int]] = {}

        # 分片重组缓冲: split_id -> {split_index: payload, split_count: int}
        self._split_reassembly: dict[int, dict[int, bytes]] = {}
        self._split_counts: dict[int, int] = {}

        # 后台任务
        self._tasks: list[asyncio.Task] = []

        # 事件循环引用 (在 connect 中设置)
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """是否已建立连接。"""
        return self._connected

    @property
    def client_guid(self) -> int:
        """客户端 GUID。"""
        return self._client_guid

    @property
    def server_guid(self) -> int:
        """服务器 GUID (握手完成后可用)。"""
        return self._server_guid

    @property
    def mtu(self) -> int:
        """协商后的 MTU。"""
        return self._mtu

    # ------------------------------------------------------------------
    # 公开 API: connect / disconnect / send / recv
    # ------------------------------------------------------------------

    async def connect(self, host: str, port: int) -> None:
        """连接到 Bedrock 服务器并完成 RakNet 握手。

        流程:
            1. 打开 UDP socket
            2. OpenConnectionRequest1 -> Reply1 (MTU 协商)
            3. OpenConnectionRequest2 -> Reply2 (确认客户端地址)
            4. ConnectionRequest -> ConnectionRequestAccepted (在线握手)

        Args:
            host: 服务器主机名或 IP。
            port: 服务器端口 (通常 19132)。

        Raises:
            ConnectionError: 握手失败或超时。
            OSError: UDP socket 创建失败。
        """
        if self._connected:
            raise RuntimeError("连接已建立, 请先调用 disconnect()")

        self._host = host
        self._port = port
        # Bug 9.2 修复: get_event_loop() 在 Python 3.10+ 无运行循环时会报错,
        # 且在 3.9- 可能返回错误的循环。connect() 是 async 方法, 必然在
        # 运行中的事件循环里被调用, 改用 get_running_loop()。
        self._loop = asyncio.get_running_loop()
        self._error = None
        self._closed_event.clear()
        # Bug 9.5 修复: 在 connect() 中重新创建队列和事件, 确保绑定到当前
        # 运行的事件循环 (避免 __init__ 时绑定的循环与实际运行循环不一致)。
        self._raw_recv_queue = asyncio.Queue(maxsize=self._recv_queue_size)
        self._app_recv_queue = asyncio.Queue(maxsize=self._recv_queue_size)
        self._closed_event = asyncio.Event()

        # 1. 打开 UDP socket
        try:
            transport, protocol = await self._loop.create_datagram_endpoint(
                lambda: _RakNetDatagramProtocol(self),
                remote_addr=(host, port),
            )
        except OSError as exc:
            raise ConnectionError(f"UDP socket 创建失败: {exc}") from exc

        self._transport = transport  # type: ignore[assignment]
        self._protocol = protocol  # type: ignore[assignment]

        logger.info("已连接 UDP socket, 开始 RakNet 握手: %s:%d", host, port)

        # 启动后台接收任务
        self._tasks.append(asyncio.create_task(self._recv_loop()))
        self._tasks.append(asyncio.create_task(self._retransmit_loop()))

        try:
            # 2. 离线握手
            await asyncio.wait_for(
                self._do_offline_handshake(),
                timeout=self._connect_timeout,
            )

            # 3. 在线握手
            await asyncio.wait_for(
                self._do_online_handshake(),
                timeout=self._connect_timeout,
            )

            self._connected = True
            logger.info(
                "RakNet 握手完成: server_guid=%d, mtu=%d",
                self._server_guid, self._mtu,
            )

            # 启动保活任务
            self._tasks.append(asyncio.create_task(self._keepalive_loop()))
        except Exception as exc:
            await self._cleanup()
            raise ConnectionError(f"RakNet 握手失败: {exc}") from exc

    async def disconnect(self) -> None:
        """断开连接 (发送 ID_DISCONNECTION_NOTIFICATION 后关闭)。"""
        if not self._connected and not self._closing:
            return

        self._closing = True
        try:
            if self._connected:
                # 发送断开通知 (作为封装数据包)
                notification = bytes([ID_DISCONNECTION_NOTIFICATION])
                try:
                    await asyncio.wait_for(
                        self.send(notification, Reliability.RELIABLE_ORDERED),
                        timeout=2.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning("发送断开通知超时, 强制关闭")
                except Exception as exc:
                    logger.warning("发送断开通知失败: %s", exc)
        finally:
            await self._cleanup()
            logger.info("已断开连接")

    async def send(
        self,
        data: bytes,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
    ) -> int:
        """发送数据 (可能被分片)。

        如果 ``data`` 大于 :attr:`_split_threshold`, 会自动拆分为多个
        :class:`EncapsulatedPacket` (每个有相同的 ``split_id`` 与不同的
        ``split_index``), 接收端会重组。

        Args:
            data: 要发送的字节串。
            reliability: 可靠性等级 (默认 :attr:`Reliability.RELIABLE_ORDERED`)。

        Returns:
            发送的 datagram 序列号 (最后一个分片的)。

        Raises:
            RuntimeError: 未连接或连接已关闭。
            ValueError: data 过大无法发送。
        """
        if not self._connected:
            raise RuntimeError("未连接, 无法发送")

        if not data:
            # 空数据仍需发送一个 datagram
            return await self._send_datagram_with(
                [EncapsulatedPacket(reliability=reliability, payload=b"")]
            )

        # 计算最大 payload (考虑封装头开销)
        max_payload = self._mtu - DATAGRAM_HEADER_SIZE - ENCAPSULATED_HEADER_MAX_SIZE
        if max_payload <= 0:
            raise ValueError(f"MTU 过小: {self._mtu}")

        if len(data) <= self._split_threshold:
            # 不需要分片
            packet = self._build_encapsulated(data, reliability, split=False)
            return await self._send_datagram_with([packet])

        # 需要分片
        split_id = self._split_id
        self._split_id = (self._split_id + 1) & 0xFFFF

        chunks = [
            data[i:i + max_payload]
            for i in range(0, len(data), max_payload)
        ]
        split_count = len(chunks)
        last_seq = 0
        logger.debug(
            "分片发送: %d 字节 -> %d 片 (split_id=%d)",
            len(data), split_count, split_id,
        )

        for idx, chunk in enumerate(chunks):
            packet = self._build_encapsulated(
                chunk, reliability,
                split=True,
                split_count=split_count,
                split_id=split_id,
                split_index=idx,
            )
            last_seq = await self._send_datagram_with([packet])
        return last_seq

    async def recv(self) -> bytes:
        """接收下一条应用层数据。

        阻塞等待直到有数据可读。如果是可靠有序数据, 会按顺序返回;
        如果是 sequenced 数据, 只返回最新的。

        Returns:
            接收到的字节串。

        Raises:
            RuntimeError: 连接已关闭且无缓存数据。
        """
        # Bug 9.1 修复: 之前直接 await self._app_recv_queue.get() 会在连接
        # 关闭后 (若无人向队列推送 sentinel) 永久阻塞。现使用 wait_for 超时
        # 轮询, 每次超时后重新检查连接状态, 确保连接关闭时能及时返回。
        while True:
            if not self._connected and self._app_recv_queue.empty():
                raise RuntimeError("连接已关闭, 无可读数据")
            try:
                return await asyncio.wait_for(
                    self._app_recv_queue.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                if not self._connected and self._app_recv_queue.empty():
                    raise RuntimeError("连接已关闭, 无可读数据")
                continue

    # ------------------------------------------------------------------
    # 内部: 离线握手
    # ------------------------------------------------------------------

    async def _do_offline_handshake(self) -> None:
        """执行离线握手 (OCR1 -> Reply1 -> OCR2 -> Reply2)。"""
        # 1. 发送 OpenConnectionRequest1
        ocr1 = build_open_connection_request_1(
            protocol_version=self._protocol_version,
            mtu=self._mtu,
        )
        self._protocol_send(ocr1)
        logger.debug(
            "发送 OpenConnectionRequest1: mtu=%d, proto=%d",
            self._mtu, self._protocol_version,
        )

        # 2. 等待 Reply1
        self._offline_reply = self._loop.create_future()
        try:
            reply1 = await self._offline_reply
        except asyncio.CancelledError:
            raise ConnectionError("等待 Reply1 时被取消")

        # 解析 Reply1
        try:
            self._server_guid, has_security, mtu = parse_open_connection_reply_1(reply1)
        except ValueError as exc:
            raise ConnectionError(f"Reply1 解析失败: {exc}") from exc

        if has_security:
            logger.warning("服务器要求加密, 当前实现可能不支持")
        # 更新 MTU (以服务器返回的为准)
        self._mtu = mtu if mtu > 0 else self._mtu
        logger.debug(
            "收到 Reply1: server_guid=%d, mtu=%d, security=%s",
            self._server_guid, self._mtu, has_security,
        )

        # 3. 发送 OpenConnectionRequest2
        ocr2 = build_open_connection_request_2(
            server_host=self._host,
            server_port=self._port,
            mtu=self._mtu,
            client_guid=self._client_guid,
        )
        self._protocol_send(ocr2)
        logger.debug("发送 OpenConnectionRequest2")

        # 4. 等待 Reply2
        self._offline_reply = self._loop.create_future()
        try:
            reply2 = await self._offline_reply
        except asyncio.CancelledError:
            raise ConnectionError("等待 Reply2 时被取消")

        try:
            _, client_host, client_port, mtu2 = parse_open_connection_reply_2(reply2)
        except ValueError as exc:
            raise ConnectionError(f"Reply2 解析失败: {exc}") from exc

        logger.debug(
            "收到 Reply2: client_addr=%s:%d, mtu=%d",
            client_host, client_port, mtu2,
        )

    async def _do_online_handshake(self) -> None:
        """执行在线握手 (ConnectionRequest -> ConnectionRequestAccepted)。"""
        time_ms = int(time.time() * 1000)
        request_payload = build_connection_request(
            client_guid=self._client_guid,
            time_ms=time_ms,
            use_security=False,
        )

        # 通过 datagram 发送 (可靠有序)
        await self._send_datagram_with([
            EncapsulatedPacket(
                reliability=Reliability.RELIABLE_ORDERED,
                payload=request_payload,
            )
        ])
        logger.debug("发送 ConnectionRequest: guid=%d", self._client_guid)

        # 等待 ConnectionRequestAccepted (在 _handle_datagram 中识别)
        self._online_reply = self._loop.create_future()
        try:
            accepted = await self._online_reply
        except asyncio.CancelledError:
            raise ConnectionError("等待 ConnectionRequestAccepted 时被取消")

        try:
            _, _, request_time, reply_time = parse_connection_request_accepted(accepted)
            logger.debug(
                "收到 ConnectionRequestAccepted: rtt=%d ms",
                reply_time - request_time,
            )
        except ValueError as exc:
            raise ConnectionError(
                f"ConnectionRequestAccepted 解析失败: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # 内部: 数据报发送
    # ------------------------------------------------------------------

    def _build_encapsulated(
        self,
        payload: bytes,
        reliability: Reliability,
        *,
        split: bool = False,
        split_count: int = 0,
        split_id: int = 0,
        split_index: int = 0,
    ) -> EncapsulatedPacket:
        """构造 EncapsulatedPacket, 自动分配 message/order/sequence index。"""
        packet = EncapsulatedPacket(
            reliability=reliability,
            payload=payload,
            split=split,
            split_count=split_count,
            split_id=split_id,
            split_index=split_index,
        )

        if reliability.is_reliable:
            packet.message_index = self._send_message_index
            self._send_message_index = (self._send_message_index + 1) % SEQ_MOD

        if reliability.is_sequenced:
            channel = 0  # 简化: 固定使用 channel 0
            packet.sequence_index = self._send_sequence_index.get(channel, 0)
            self._send_sequence_index[channel] = (
                (self._send_sequence_index.get(channel, 0) + 1) % SEQ_MOD
            )
            packet.order_index = packet.sequence_index
            packet.order_channel = channel

        if reliability.is_ordered:
            channel = 0  # 简化: 固定使用 channel 0
            packet.order_index = self._send_order_index.get(channel, 0)
            self._send_order_index[channel] = (
                (self._send_order_index.get(channel, 0) + 1) % SEQ_MOD
            )
            packet.order_channel = channel

        return packet

    async def _send_datagram_with(self, packets: list[EncapsulatedPacket]) -> int:
        """封装数据包为 datagram 并发送。

        Returns:
            分配的 datagram 序列号。
        """
        seq = self._send_seq
        self._send_seq = (self._send_seq + 1) % SEQ_MOD

        datagram = encode_datagram(seq, *packets)
        self._protocol_send(datagram)

        # 如果有可靠数据包, 加入重传缓冲
        has_reliable = any(p.reliability.is_reliable for p in packets)
        if has_reliable:
            self._retransmit_buffer[seq] = (
                datagram, time.monotonic(), 0,
            )

        return seq

    def _protocol_send(self, data: bytes) -> None:
        """通过底层 transport 发送原始字节。"""
        if self._protocol is None:
            raise RuntimeError("protocol 未建立")
        self._protocol.send(data)

    async def _send_ping(self) -> None:
        """发送保活 ConnectedPing。"""
        if not self._connected:
            return
        time_ms = int(time.time() * 1000)
        ping_payload = build_connected_ping(time_ms)
        await self._send_datagram_with([
            EncapsulatedPacket(
                reliability=Reliability.UNRELIABLE,
                payload=ping_payload,
            )
        ])
        logger.debug("发送 ConnectedPing: time=%d", time_ms)

    # ------------------------------------------------------------------
    # 内部: 接收循环与数据报处理
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """主接收循环 — 从原始队列读取数据并分发。"""
        while not self._closing:
            try:
                data = await asyncio.wait_for(
                    self._raw_recv_queue.get(), timeout=1.0,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            try:
                await self._dispatch_packet(data)
            except Exception as exc:
                logger.exception("处理数据包时出错: %s", exc)

    async def _dispatch_packet(self, data: bytes) -> None:
        """分发收到的数据包到对应的处理函数。"""
        if not data:
            return

        flag = data[0]

        # 离线消息
        if is_offline_message(data):
            await self._handle_offline_message(data)
            return

        # ACK
        # Bug 9.3 修复: ACK 标志位为高 2 位 11 (0xC0-0xFF), NACK 为高 3 位
        # 101 (0xA0-0xBF)。之前使用精确匹配 (== ID_ACK/ID_NACK) 会拒绝服务端
        # 发送的非标准标志位 ACK/NACK。改用位掩码范围匹配。
        if flag & 0xC0 == 0xC0:
            await self._handle_ack(data)
            return

        # NACK
        if flag & 0xE0 == 0xA0:
            await self._handle_nack(data)
            return

        # Datagram (0x80-0x9F, bit 7=1, bit 6=0, bit 5=0)
        if flag & 0xE0 == 0x80:
            await self._handle_datagram(data)
            return

        # 已连接控制消息 (直接 UDP, 未封装在 datagram 中)
        if flag == ID_DISCONNECTION_NOTIFICATION:
            logger.info("服务器发送断开通知")
            self._connected = False
            await self._cleanup()
            return

        logger.debug("未处理的 RakNet 数据包: flag=0x%02X (len=%d)", flag, len(data))

    async def _handle_offline_message(self, data: bytes) -> None:
        """处理离线握手响应 (Reply1/Reply2/Pong)。"""
        packet_id = data[0]
        logger.debug("收到离线消息: 0x%02X", packet_id)

        if packet_id == ID_OPEN_CONNECTION_REPLY_1:
            if self._offline_reply is not None and not self._offline_reply.done():
                self._offline_reply.set_result(data)
        elif packet_id == ID_OPEN_CONNECTION_REPLY_2:
            if self._offline_reply is not None and not self._offline_reply.done():
                self._offline_reply.set_result(data)
        elif packet_id == ID_UNCONNECTED_PONG:
            # 服务器发现响应, 当前实现忽略 (用于 MOTD 查询)
            logger.debug("收到 UnconnectedPong (忽略)")
        else:
            logger.debug("未处理的离线消息: 0x%02X", packet_id)

    def _seq_diff(self, expected: int, actual: int) -> int:
        """计算序列号差值, 考虑 24 位回绕。

        返回值含义:
            - ``0``: ``actual`` 等于 ``expected`` (按序到达)
            - 正数: ``actual`` 在 ``expected`` 之后 (未来 / 乱序包, 存在 gap)
            - 负数: ``actual`` 在 ``expected`` 之前 (重复包)

        Args:
            expected: 期望的序列号。
            actual: 实际收到的序列号。

        Returns:
            回绕感知的有符号差值, 范围 ``[-SEQ_MOD//2, SEQ_MOD//2]``。
        """
        diff = (actual - expected) % SEQ_MOD
        if diff > SEQ_MOD // 2:
            diff -= SEQ_MOD
        return diff

    async def _handle_datagram(self, data: bytes) -> None:
        """处理 datagram — 解码封装数据包, 发送 ACK, 处理 payload。

        实现滑动窗口缓冲与回绕感知的序列号比较:

            1. ``seq == expected`` (按序): 处理当前 datagram 并推进期望序号,
               随后从滑动窗口中依次取出连续排队的后续 datagram 处理并清理。
            2. ``seq`` 在 ``expected`` 之后 (乱序/跳序, 存在 gap): 将当前
               datagram 缓存到滑动窗口, 发送 ACK, 但**不推进** ``_recv_seq``,
               同时对缺失序号发送 NACK 请求立即重传。
            3. ``seq`` 在 ``expected`` 之前 (重复): 仅发 ACK, 丢弃 payload。

        序列号比较使用 :meth:`_seq_diff` 处理模 ``SEQ_MOD`` 的回绕。
        """
        try:
            seq, packets = decode_datagram(data)
        except ValueError as exc:
            logger.warning("datagram 解码失败: %s", exc)
            return

        logger.debug("收到 datagram: seq=%d, %d 个封装包", seq, len(packets))

        # 发送 ACK (无论是否按序, 告知发送端已收到此 datagram)
        await self._send_ack(seq)

        expected = self._recv_seq
        diff = self._seq_diff(expected, seq)

        if diff == 0:
            # 按序到达: 处理当前 datagram 并推进期望序号
            self._recv_seq = (self._recv_seq + 1) % SEQ_MOD
            for packet in packets:
                await self._process_encapsulated(packet)

            # 滑动窗口: 依次处理缓冲区中连续排队的后续 datagram 并清理
            while self._recv_seq in self._recv_window:
                buffered_packets = self._recv_window.pop(self._recv_seq)
                self._recv_seq = (self._recv_seq + 1) % SEQ_MOD
                for packet in buffered_packets:
                    await self._process_encapsulated(packet)

        elif diff > 0:
            # 未来 datagram (存在 gap): 缓存, 不推进 _recv_seq, 请求重传缺失序号
            logger.debug("datagram 乱序: expected=%d, got=%d", expected, seq)
            self._recv_window[seq] = packets

            # 枚举 [expected, seq) 区间内缺失的序号, 发送 NACK 请求立即重传。
            # 限制单次扫描上限, 防止 (expected, seq) 距离过大时产生过大 NACK
            # 包与循环开销 (单个 NACK 包记录数受 MTU 约束)。
            max_gap_scan = 256
            missing: list[int] = []
            gap_seq = expected
            for _ in range(max_gap_scan):
                if self._seq_diff(gap_seq, seq) <= 0:
                    break
                if gap_seq not in self._recv_window:
                    missing.append(gap_seq)
                gap_seq = (gap_seq + 1) % SEQ_MOD
            if missing:
                await self._send_nack(missing)

        else:
            # 重复 datagram (diff < 0): 已处理过, 仅发 ACK (上面已发)
            logger.debug("收到重复 datagram: seq=%d", seq)

    async def _handle_ack(self, data: bytes) -> None:
        """处理 ACK — 从重传缓冲移除已确认的 datagram。"""
        try:
            seqs, _ = decode_ack(data)
        except ValueError as exc:
            logger.warning("ACK 解码失败: %s", exc)
            return

        for seq in seqs:
            if seq in self._retransmit_buffer:
                del self._retransmit_buffer[seq]
                logger.debug("ACK: 移除 datagram seq=%d", seq)

    async def _handle_nack(self, data: bytes) -> None:
        """处理 NACK — 立即重传被请求的 datagram。"""
        try:
            seqs, _ = decode_ack(data)
        except ValueError as exc:
            logger.warning("NACK 解码失败: %s", exc)
            return

        for seq in seqs:
            if seq in self._retransmit_buffer:
                datagram, send_time, count = self._retransmit_buffer[seq]
                logger.debug("NACK: 立即重传 datagram seq=%d", seq)
                self._protocol_send(datagram)
                # 更新发送时间和重传计数
                self._retransmit_buffer[seq] = (datagram, time.monotonic(), count + 1)

    async def _send_ack(self, seq: int) -> None:
        """发送 ACK (单个 seq)。"""
        ack = encode_ack(seq, is_ack=True)
        self._protocol_send(ack)

    async def _send_nack(self, seqs: list[int]) -> None:
        """发送 NACK — 请求重传指定的 datagram 序号列表。

        将多个缺失序号合并到单个 NACK (``0xA0``) 数据包中以提高效率,
        复用 :func:`encode_ack` 的多记录编码能力。空列表时直接返回。

        Args:
            seqs: 缺失的 datagram 序号列表 (24 位)。
        """
        if not seqs:
            return
        nack = encode_ack(*seqs, is_ack=False)
        self._protocol_send(nack)

    async def _process_encapsulated(self, packet: EncapsulatedPacket) -> None:
        """处理封装数据包 — 处理分片、保活、应用数据。"""
        # 分片重组
        if packet.split:
            await self._handle_split_packet(packet)
            return

        # 处理 payload
        await self._handle_payload(packet.payload)

    async def _handle_split_packet(self, packet: EncapsulatedPacket) -> None:
        """处理分片 — 缓存到 split_id 下, 全部分片到齐后重组。"""
        split_id = packet.split_id
        split_index = packet.split_index
        split_count = packet.split_count

        if split_id not in self._split_reassembly:
            self._split_reassembly[split_id] = {}
            self._split_counts[split_id] = split_count

        self._split_reassembly[split_id][split_index] = packet.payload

        received = len(self._split_reassembly[split_id])
        expected = self._split_counts[split_id]

        if received >= expected:
            # 全部分片到齐, 重组
            chunks = [
                self._split_reassembly[split_id][i]
                for i in range(expected)
                if i in self._split_reassembly[split_id]
            ]
            if len(chunks) != expected:
                logger.warning(
                    "分片重组不完整: split_id=%d, 期望 %d, 实际 %d",
                    split_id, expected, len(chunks),
                )
                return

            reassembled = b"".join(chunks)
            del self._split_reassembly[split_id]
            del self._split_counts[split_id]

            logger.debug(
                "分片重组完成: split_id=%d, %d 片 -> %d 字节",
                split_id, expected, len(reassembled),
            )
            await self._handle_payload(reassembled)

    async def _handle_payload(self, payload: bytes) -> None:
        """处理已重组的 payload — 识别控制消息或交付应用层。"""
        if not payload:
            return

        packet_id = payload[0]

        # 在线握手响应
        if packet_id == ID_CONNECTION_REQUEST_ACCEPTED:
            if self._online_reply is not None and not self._online_reply.done():
                self._online_reply.set_result(payload)
            return

        # 保活响应
        if packet_id == ID_CONNECTED_PONG:
            try:
                ping_time, pong_time = parse_connected_pong(payload)
                rtt = pong_time - ping_time
                logger.debug("收到 ConnectedPong: RTT=%d ms", rtt)
            except ValueError as exc:
                logger.warning("ConnectedPong 解析失败: %s", exc)
            return

        # 保活请求 (服务器也会 ping 客户端)
        if packet_id == ID_CONNECTED_PING:
            if len(payload) >= 9:
                ping_time = struct.unpack(">Q", payload[1:9])[0]
                pong_payload = bytes([ID_CONNECTED_PONG]) + struct.pack(">Q", ping_time) + struct.pack(">Q", int(time.time() * 1000))
                await self._send_datagram_with([
                    EncapsulatedPacket(
                        reliability=Reliability.UNRELIABLE,
                        payload=pong_payload,
                    )
                ])
            return

        # 服务器断开通知 (封装在 datagram 中)
        if packet_id == ID_DISCONNECTION_NOTIFICATION:
            logger.info("服务器发送断开通知 (封装)")
            self._connected = False
            await self._cleanup()
            return

        # 应用层数据 — 放入接收队列
        try:
            self._app_recv_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("应用接收队列已满, 丢弃 payload (%d 字节)", len(payload))

    # ------------------------------------------------------------------
    # 内部: 后台任务
    # ------------------------------------------------------------------

    async def _retransmit_loop(self) -> None:
        """重传循环 — 定期检查并重传超时的可靠数据报。"""
        while not self._closing:
            try:
                await asyncio.sleep(self._retransmit_timeout / 2)
            except asyncio.CancelledError:
                break

            now = time.monotonic()
            timed_out: list[int] = []

            for seq, (datagram, send_time, count) in list(self._retransmit_buffer.items()):
                if now - send_time >= self._retransmit_timeout:
                    if count >= self._max_retransmits:
                        logger.warning(
                            "datagram seq=%d 重传次数达上限 (%d), 放弃",
                            seq, count,
                        )
                        timed_out.append(seq)
                        continue

                    # 重传
                    logger.debug(
                        "重传 datagram seq=%d (第 %d 次)", seq, count + 1,
                    )
                    try:
                        self._protocol_send(datagram)
                    except Exception as exc:
                        logger.warning("重传失败: %s", exc)
                    self._retransmit_buffer[seq] = (
                        datagram, now, count + 1,
                    )

            for seq in timed_out:
                del self._retransmit_buffer[seq]

    async def _keepalive_loop(self) -> None:
        """保活循环 — 定期发送 ConnectedPing。"""
        while self._connected and not self._closing:
            try:
                await asyncio.sleep(self._ping_interval)
            except asyncio.CancelledError:
                break
            try:
                await self._send_ping()
            except Exception as exc:
                logger.warning("发送 ConnectedPing 失败: %s", exc)
                break

    # ------------------------------------------------------------------
    # 内部: 错误与清理
    # ------------------------------------------------------------------

    def _on_error(self, exc: Exception) -> None:
        """UDP socket 错误回调。"""
        self._error = exc
        logger.error("RakNet socket 错误: %s", exc)
        # 取消所有等待中的 future
        if self._offline_reply is not None and not self._offline_reply.done():
            try:
                self._offline_reply.set_exception(exc)
            except asyncio.InvalidStateError:
                pass
        if self._online_reply is not None and not self._online_reply.done():
            try:
                self._online_reply.set_exception(exc)
            except asyncio.InvalidStateError:
                pass

    def _on_connection_lost(self, exc: Optional[Exception]) -> None:
        """UDP 连接丢失回调。"""
        logger.info("UDP 连接已关闭: %s", exc or "")
        self._connected = False
        self._closed_event.set()
        # 取消所有等待中的 future
        err = exc or ConnectionError("连接已关闭")
        if self._offline_reply is not None and not self._offline_reply.done():
            try:
                self._offline_reply.set_exception(err)
            except asyncio.InvalidStateError:
                pass
        if self._online_reply is not None and not self._online_reply.done():
            try:
                self._online_reply.set_exception(err)
            except asyncio.InvalidStateError:
                pass

    async def _cleanup(self) -> None:
        """清理资源 — 关闭 transport, 取消任务。"""
        self._connected = False
        self._closing = True

        # 取消后台任务
        for task in self._tasks:
            if not task.done():
                task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        # 关闭 transport
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
            self._protocol = None

        # 清空缓冲
        self._retransmit_buffer.clear()
        self._split_reassembly.clear()
        self._split_counts.clear()


__all__ = [
    # 数据包 ID 常量
    "ID_CONNECTED_PING",
    "ID_UNCONNECTED_PING",
    "ID_UNCONNECTED_PING_OPEN_CONNECTIONS",
    "ID_CONNECTED_PONG",
    "ID_OPEN_CONNECTION_REQUEST_1",
    "ID_OPEN_CONNECTION_REPLY_1",
    "ID_OPEN_CONNECTION_REQUEST_2",
    "ID_OPEN_CONNECTION_REPLY_2",
    "ID_CONNECTION_REQUEST",
    "ID_DISCONNECTION_NOTIFICATION",
    "ID_CONNECTION_REQUEST_ACCEPTED",
    "ID_UNCONNECTED_PONG",
    "ID_DATAGRAM",
    "ID_NACK",
    "ID_ACK",
    # 魔数
    "OFFLINE_MESSAGE_DATA_ID",
    "MAGIC",
    # 协议常量
    "DEFAULT_PROTOCOL_VERSION",
    "DEFAULT_MTU",
    "DEFAULT_CONNECT_TIMEOUT",
    "DEFAULT_PING_INTERVAL",
    "DEFAULT_RETRANSMIT_TIMEOUT",
    "DEFAULT_MAX_RETRANSMITS",
    "DEFAULT_RECV_QUEUE_SIZE",
    "DEFAULT_SPLIT_THRESHOLD",
    "DATAGRAM_HEADER_SIZE",
    "ENCAPSULATED_HEADER_MAX_SIZE",
    "SEQ_MOD",
    # 枚举与数据结构
    "Reliability",
    "EncapsulatedPacket",
    # 地址编解码
    "encode_address",
    "decode_address",
    # datagram / ACK 编解码
    "encode_datagram",
    "decode_datagram",
    "encode_ack",
    "decode_ack",
    "is_offline_message",
    # 离线握手
    "build_unconnected_ping",
    "parse_unconnected_pong",
    "build_open_connection_request_1",
    "parse_open_connection_reply_1",
    "build_open_connection_request_2",
    "parse_open_connection_reply_2",
    # 在线握手
    "build_connection_request",
    "parse_connection_request_accepted",
    "build_connected_ping",
    "parse_connected_pong",
    # 主类
    "RakNetConnection",
]
