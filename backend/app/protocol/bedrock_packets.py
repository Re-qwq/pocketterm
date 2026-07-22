"""NEMC Bedrock 协议包定义 — 对应 Community-Bot 的 C++ 包类型。

本模块定义网易 Minecraft Bedrock Edition 协议层的包类型枚举与基础包类,
逆向自 ``Community_Bot.exe`` strings 中暴露的 C++ 类名:

- ``BlockActorDataPacket`` (方块实体数据)
- ``ContainerClosePacket`` (容器关闭)
- ``ItemStackRequestPacket`` (物品请求)
- ``PlayerHotBarPacket`` (物品栏)
- ``mcPacket`` (基础包)
- ``PacketTypeW`` (包类型枚举)

设计原则
========

- **可独立 import**: 本模块自带最小化的 varint / 字符串编解码工具,
  不依赖 :mod:`app.protocol.varint` / :mod:`app.protocol.nbt` (可选复用,
  缺失时回退到内置实现), 保证 ``import`` 不报错。
- **不修改既有模块**: 仅新增文件, 不修改 :mod:`app.protocol.__init__` 等。
- **双版本兼容**: 包类型与编解码格式在网易 3.8 / 3.9 (Bedrock 1.21.x)
  之间一致, 无版本分支。

逆向来源
========

- ``Community_Bot.exe`` (用户上传) — strings 分析:
  - ``BlockActorDataPacket`` / ``ContainerClosePacket`` /
    ``ItemStackRequestPacket`` / ``PlayerHotBarPacket`` (C++ 类名)
  - ``mcPacket`` (基础包类名)
  - ``PacketTypeW`` (包类型枚举名)
  - ``nemc::bedrock::ProtocolReader`` / ``ProtocolWriter`` (协议读写器)
- PocketTerm ``access_point_go/minecraft/protocol/packet/`` — Go 原生包定义
  (``block_actor_data.go`` / ``container_close.go`` /
  ``item_stack_request.go`` / ``player_hot_bar.go`` 等), 作为字段格式参考。

典型用法
========

::

    from app.protocol.bedrock_packets import (
        BedrockPacketType, BlockActorDataPacket, ContainerClosePacket,
    )

    # 1. 查询包类型对应的 C++ 类名
    print(BedrockPacketType.BLOCK_ACTOR_DATA.value)  # "BlockActorDataPacket"

    # 2. 构造并编码一个方块实体数据包
    pkt = BlockActorDataPacket(x=10, y=64, z=-5, nbt_data=b"\\x0a\\x00...")
    raw = pkt.encode()

    # 3. 解码
    pkt2 = BlockActorDataPacket()
    pkt2.decode(raw)
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
_LOGGER_NAME: str = "pocketterm.protocol.bedrock_packets"
logger: logging.Logger = logging.getLogger(_LOGGER_NAME)


# ===========================================================================
# 内置最小化编解码工具 (保证模块可独立 import)
# ===========================================================================
# 说明: 优先尝试复用 app.protocol.varint, 若不可用 (例如单独测试本模块时)
# 则回退到下方内置实现。Bedrock 协议使用 LEB128 变长整数 + little-endian。
try:  # pragma: no cover - 复用既有实现, 测试环境可能缺失
    from app.protocol.varint import (  # type: ignore[import]
        encode_varint as _ext_encode_varint,
        decode_varint as _ext_decode_varint,
    )
    _HAS_EXTERNAL_VARINT: bool = True
except Exception:  # noqa: BLE001 — 任何导入失败都回退到内置实现
    _HAS_EXTERNAL_VARINT = False


def _encode_varuint(value: int) -> bytes:
    """编码无符号 VarInt (LEB128, little-endian)。

    Parameters
    ----------
    value:
        非负整数。

    Returns
    -------
    bytes
        LEB128 编码字节。
    """
    if value < 0:
        raise ValueError(f"VarUInt 不支持负数: {value}")
    if _HAS_EXTERNAL_VARINT:
        try:
            return _ext_encode_varint(value)
        except Exception:  # noqa: BLE001 — 回退到内置实现
            pass
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value != 0:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def _decode_varuint(buf: io.BytesIO) -> int:
    """从缓冲区解码无符号 VarInt (LEB128)。

    Parameters
    ----------
    buf:
        可读字节缓冲区 (:class:`io.BytesIO`)。

    Returns
    -------
    int
        解码后的非负整数。

    Raises
    ------
    EOFError
        缓冲区在解码完成前耗尽。
    """
    if _HAS_EXTERNAL_VARINT:
        try:
            # 外部 decode_varint 接口签名未知, 这里安全回退到内置实现
            pass
        except Exception:  # noqa: BLE001
            pass
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


def _encode_varint_signed(value: int) -> bytes:
    """编码有符号 VarInt (ZigZag + LEB128)。"""
    # ZigZag 编码: (n << 1) ^ (n >> 63) for 64-bit
    zz = (value << 1) ^ (value >> 63) if value < 0 else (value << 1)
    return _encode_varuint(zz & 0xFFFFFFFFFFFFFFFF)


def _decode_varint_signed(buf: io.BytesIO) -> int:
    """解码有符号 VarInt (ZigZag)。"""
    zz = _decode_varuint(buf)
    return (zz >> 1) ^ -(zz & 1)


def _encode_string(s: str) -> bytes:
    """编码 Bedrock 字符串 (VarUInt 长度前缀 + UTF-8 字节)。"""
    raw = s.encode("utf-8")
    return _encode_varuint(len(raw)) + raw


def _decode_string(buf: io.BytesIO) -> str:
    """解码 Bedrock 字符串。"""
    length = _decode_varuint(buf)
    raw = buf.read(length)
    if len(raw) != length:
        raise EOFError("字符串解码时缓冲区耗尽")
    return raw.decode("utf-8")


def _encode_block_pos(x: int, y: int, z: int) -> bytes:
    """编码方块坐标 (VarInt x3, Bedrock 网络格式)。"""
    return _encode_varint_signed(x) + _encode_varint_signed(y) + _encode_varint_signed(z)


def _decode_block_pos(buf: io.BytesIO) -> Tuple[int, int, int]:
    """解码方块坐标。"""
    x = _decode_varint_signed(buf)
    y = _decode_varint_signed(buf)
    z = _decode_varint_signed(buf)
    return (x, y, z)


# ===========================================================================
# 包类型枚举 (对应 Community-Bot 的 PacketTypeW)
# ===========================================================================
class BedrockPacketType(Enum):
    """Bedrock 协议包类型枚举 (对应 Community-Bot 的 ``PacketTypeW``)。

    每个枚举成员的 ``value`` 是 Community-Bot strings 中暴露的 C++ 类名,
    便于日志对齐与逆向溯源。``packet_id`` 属性返回该包的 Bedrock 网络 ID
    (十进制, 参考 PocketTerm ``access_point_go/minecraft/protocol/packet/id.go``)。
    """

    # --- Community-Bot strings 直接确认的包类型 ---
    BLOCK_ACTOR_DATA = "BlockActorDataPacket"
    """方块实体数据包 — 用于放置 NBT 方块 (告示牌/箱子等)。"""

    CONTAINER_CLOSE = "ContainerClosePacket"
    """容器关闭包 — 客户端通知服务器关闭已打开的容器。"""

    ITEM_STACK_REQUEST = "ItemStackRequestPacket"
    """物品请求包 — 容器物品操作 (取/放/交换/丢弃)。"""

    PLAYER_HOTBAR = "PlayerHotBarPacket"
    """物品栏包 — 切换当前手持物品栏槽位。"""

    # --- 推断的常用包类型 (字段名沿用 C++ 命名风格) ---
    TEXT = "TextPacket"
    """文本包 — 聊天消息 / 系统公告 / 告示牌弹出。"""

    COMMAND_REQUEST = "CommandRequestPacket"
    """命令请求包 — 客户端执行命令 (对应 Community-Bot SendCommand)。"""

    LOGIN = "LoginPacket"
    """登录包 — RakNet 握手后首个 Bedrock 包, 携带 chainInfo / clientData。"""

    MOVE_PLAYER = "MovePlayerPacket"
    """玩家移动包。"""

    PLAYER_ACTION = "PlayerActionPacket"
    """玩家动作包。"""

    INTERACT = "InteractPacket"
    """交互包 — 与实体/方块交互。"""

    INVENTORY_TRANSACTION = "InventoryTransactionPacket"
    """物品栏事务包。"""

    LEVEL_CHUNK = "LevelChunkPacket"
    """区块数据包。"""

    SUB_CHUNK = "SubChunkPacket"
    """子区块数据包。"""

    SUB_CHUNK_REQUEST = "SubChunkRequestPacket"
    """子区块请求包。"""

    UPDATE_BLOCK = "UpdateBlockPacket"
    """方块更新包。"""

    PLAYER_AUTH_INPUT = "PlayerAuthInputPacket"
    """玩家输入包 — 网易反作弊核心 (携带位置/操作/输入)。"""

    MC_PACKET = "mcPacket"
    """基础包 — Community-Bot 的 ``mcPacket`` C++ 基类 (抽象, 不直接发送)。"""

    @property
    def packet_id(self) -> int:
        """返回该包类型的 Bedrock 网络 ID (十进制)。

        来源: PocketTerm ``access_point_go/minecraft/protocol/packet/id.go``。
        未在常量表中的包返回 ``-1``。
        """
        return _PACKET_ID_MAP.get(self, -1)


#: 包类型 → Bedrock 网络 ID 映射 (参考 id.go)。
_PACKET_ID_MAP: Dict["BedrockPacketType", int] = {}


# ===========================================================================
# 基础包类 (对应 Community-Bot 的 mcPacket)
# ===========================================================================
class BedrockPacket:
    """Bedrock 协议包基类 (对应 Community-Bot 的 ``mcPacket`` C++ 基类)。

    所有具体包类型应继承本类并实现 :meth:`encode` / :meth:`decode`。
    子类应设置 :attr:`packet_type` 类属性以关联 :class:`BedrockPacketType`。

    Attributes
    ----------
    packet_type : BedrockPacketType
        该包的类型枚举 (子类覆盖)。
    """

    #: 包类型枚举 (子类覆盖)。
    packet_type: BedrockPacketType = BedrockPacketType.MC_PACKET

    def encode(self) -> bytes:
        """将本包序列化为 Bedrock 网络字节流 (含包头 ID)。

        子类应先调用 :meth:`_encode_header` 写入包头, 再追加 payload。

        Returns
        -------
        bytes
            序列化后的字节流。

        Raises
        ------
        NotImplementedError
            子类未实现时抛出。
        """
        raise NotImplementedError(
            f"{type(self).__name__}.encode() 未实现"
        )

    def decode(self, data: bytes) -> None:
        """从 Bedrock 网络字节流反序列化本包。

        Parameters
        ----------
        data:
            完整的包字节流 (含包头 ID)。

        Raises
        ------
        NotImplementedError
            子类未实现时抛出。
        """
        raise NotImplementedError(
            f"{type(self).__name__}.decode() 未实现"
        )

    # ------------------------------------------------------------------
    # 内部工具: 包头编解码
    # ------------------------------------------------------------------
    def _encode_header(self, packet_id: int) -> bytes:
        """编码包头 (VarUInt 包 ID)。

        Bedrock 网络层在 RakNet 之上使用 ``varuint(packet_id) + payload`` 格式
        (摘自 access_point_go/minecraft/protocol/packet/packet.go)。
        """
        return _encode_varuint(packet_id)

    def _decode_header(self, buf: io.BytesIO) -> int:
        """解码包头并返回包 ID。"""
        return _decode_varuint(buf)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(type={self.packet_type})"


# ===========================================================================
# 具体包类型
# ===========================================================================
@dataclass
class BlockActorDataPacket(BedrockPacket):
    """方块实体数据包 — 用于放置 NBT 方块 (告示牌/箱子等)。

    对应 Community-Bot strings 的 ``BlockActorDataPacket`` C++ 类。
    字段格式参考 ``access_point_go/minecraft/protocol/packet/block_actor_data.go``::

        VarInt  position.x
        VarInt  position.y
        VarInt  position.z
        NBT     block_actor_data (网络字节序 NBT)

    Attributes
    ----------
    x, y, z : int
        方块坐标。
    nbt_data : bytes
        序列化的 NBT 数据 (网络字节序, 通常由 :mod:`app.protocol.nbt` 生成)。
    """

    x: int = 0
    y: int = 0
    z: int = 0
    nbt_data: bytes = b""

    packet_type: BedrockPacketType = field(
        default=BedrockPacketType.BLOCK_ACTOR_DATA, repr=False
    )

    #: Bedrock 网络 ID (参考 id.go ID_BLOCK_ACTOR_DATA = 0x38 = 56)。
    PACKET_ID: int = 56

    def encode(self) -> bytes:
        """序列化为字节流。"""
        out = bytearray()
        out += self._encode_header(self.PACKET_ID)
        out += _encode_block_pos(self.x, self.y, self.z)
        out += self.nbt_data
        return bytes(out)

    def decode(self, data: bytes) -> None:
        """从字节流反序列化。"""
        buf = io.BytesIO(data)
        self._decode_header(buf)
        self.x, self.y, self.z = _decode_block_pos(buf)
        self.nbt_data = buf.read()


@dataclass
class ContainerClosePacket(BedrockPacket):
    """容器关闭包 — 客户端通知服务器关闭已打开的容器。

    对应 Community-Bot strings 的 ``ContainerClosePacket`` C++ 类。
    字段格式参考 ``container_close.go``::

        VarUInt  packet_id
        u8       container_id
        bool     server_initiated  (Bedrock 1.21+)

    Attributes
    ----------
    container_id : int
        容器 ID (0-255)。
    server_initiated : bool
        是否由服务器发起关闭 (1.21+ 新增字段)。
    """

    container_id: int = 0
    server_initiated: bool = False

    packet_type: BedrockPacketType = field(
        default=BedrockPacketType.CONTAINER_CLOSE, repr=False
    )

    #: Bedrock 网络 ID (id.go ID_CONTAINER_CLOSE = 0x2F = 47)。
    PACKET_ID: int = 47

    def encode(self) -> bytes:
        out = bytearray()
        out += self._encode_header(self.PACKET_ID)
        out += struct.pack("<BB", self.container_id & 0xFF, 1 if self.server_initiated else 0)
        return bytes(out)

    def decode(self, data: bytes) -> None:
        buf = io.BytesIO(data)
        self._decode_header(buf)
        raw = buf.read(2)
        if len(raw) < 2:
            raise EOFError("ContainerClosePacket 字段不足")
        self.container_id = raw[0]
        self.server_initiated = bool(raw[1])


@dataclass
class ItemStackRequestPacket(BedrockPacket):
    """物品请求包 — 容器物品操作 (取/放/交换/丢弃)。

    对应 Community-Bot strings 的 ``ItemStackRequestPacket`` C++ 类。
    字段格式参考 ``item_stack_request.go``::

        VarUInt  packet_id
        VarUInt  request_count
        repeat(request_count):
            ItemStackRequest { ... }

    本实现仅保留请求列表的原始字节 (上层负责构造 ItemStackRequest 结构),
    以避免过早耦合完整物品系统。

    Attributes
    ----------
    requests : list[bytes]
        已序列化的 ItemStackRequest 列表 (每个元素为单个请求的字节)。
    """

    requests: List[bytes] = field(default_factory=list)

    packet_type: BedrockPacketType = field(
        default=BedrockPacketType.ITEM_STACK_REQUEST, repr=False
    )

    #: Bedrock 网络 ID (id.go ID_ITEM_STACK_REQUEST = 0x21 = 33)。
    PACKET_ID: int = 33

    def encode(self) -> bytes:
        out = bytearray()
        out += self._encode_header(self.PACKET_ID)
        out += _encode_varuint(len(self.requests))
        for req in self.requests:
            out += req
        return bytes(out)

    def decode(self, data: bytes) -> None:
        buf = io.BytesIO(data)
        self._decode_header(buf)
        count = _decode_varuint(buf)
        self.requests = []
        for _ in range(count):
            # 单个 ItemStackRequest 的边界需上层根据其内部长度字段确定,
            # 此处读取剩余全部字节作为单个请求 (保守实现)。
            remaining = buf.read()
            if remaining:
                self.requests.append(remaining)
            break


@dataclass
class PlayerHotBarPacket(BedrockPacket):
    """物品栏包 — 切换当前手持物品栏槽位。

    对应 Community-Bot strings 的 ``PlayerHotBarPacket`` C++ 类。
    字段格式参考 ``player_hot_bar.go``::

        VarUInt  packet_id
        VarUInt  selected_slot
        u8       select_hotbar_slot  (bool: true=立即切换)

    Attributes
    ----------
    selected_slot : int
        选中的物品栏槽位 (0-8)。
    select_hotbar_slot : bool
        是否立即切换 (通常为 True)。
    """

    selected_slot: int = 0
    select_hotbar_slot: bool = True

    packet_type: BedrockPacketType = field(
        default=BedrockPacketType.PLAYER_HOTBAR, repr=False
    )

    #: Bedrock 网络 ID (id.go ID_PLAYER_HOT_BAR = 0x30 = 48)。
    PACKET_ID: int = 48

    def encode(self) -> bytes:
        out = bytearray()
        out += self._encode_header(self.PACKET_ID)
        out += _encode_varuint(self.selected_slot)
        out += struct.pack("<B", 1 if self.select_hotbar_slot else 0)
        return bytes(out)

    def decode(self, data: bytes) -> None:
        buf = io.BytesIO(data)
        self._decode_header(buf)
        self.selected_slot = _decode_varuint(buf)
        flag = buf.read(1)
        self.select_hotbar_slot = bool(flag[0]) if flag else True


@dataclass
class TextPacket(BedrockPacket):
    """文本包 — 聊天消息 / 系统公告。

    对应 Community-Bot 的 ``SendMessage`` 函数底层发送的包类型。
    字段格式参考 ``text.go``::

        VarUInt  packet_id
        u8       text_type    (0=Raw, 1=Chat, 2=Translation, ...)
        bool     needs_translation
        string   (依 text_type 不同, 字段顺序不同; Raw/Chat: sender + message)

    Attributes
    ----------
    text_type : int
        文本类型 (0=Raw, 1=Chat, 2=Translation, 3=Popup, 4=Jukebox, 5=Tip, 6=System, 7=Whisper, 8=Announcement)。
    sender : str
        发送者名称。
    message : str
        消息内容。
    needs_translation : bool
        是否需要翻译。
    """

    text_type: int = 1  # Chat
    sender: str = ""
    message: str = ""
    needs_translation: bool = False

    packet_type: BedrockPacketType = field(
        default=BedrockPacketType.TEXT, repr=False
    )

    #: Bedrock 网络 ID (id.go ID_TEXT = 0x09 = 9)。
    PACKET_ID: int = 9

    def encode(self) -> bytes:
        out = bytearray()
        out += self._encode_header(self.PACKET_ID)
        out += struct.pack("<BB", self.text_type & 0xFF, 1 if self.needs_translation else 0)
        # Chat (type=1) 格式: sender + message (空字符串作为平台/XUID 占位省略)
        out += _encode_string(self.sender)
        out += _encode_string(self.message)
        return bytes(out)

    def decode(self, data: bytes) -> None:
        buf = io.BytesIO(data)
        self._decode_header(buf)
        raw = buf.read(2)
        self.text_type = raw[0] if raw else 0
        self.needs_translation = bool(raw[1]) if len(raw) > 1 else False
        self.sender = _decode_string(buf)
        self.message = _decode_string(buf)


@dataclass
class CommandRequestPacket(BedrockPacket):
    """命令请求包 — 客户端执行命令。

    对应 Community-Bot 的 ``SendCommand`` / ``SendCommandEx`` /
    ``SendCommandPackt`` 函数底层发送的包类型。
    字段格式参考 ``command_request.go``::

        VarUInt  packet_id
        string   command
        VarInt   origin_type
        ...      (依 origin_type 不同)
        bool     internal

    Attributes
    ----------
    command : str
        命令字符串 (不含前导 ``/``)。
    origin_type : int
        命令来源类型 (0=Player, 1=Block, 2=MinecartBlock, 3=DevConsole, 4=Test, 5=AutomationPlayer)。
    internal : bool
        是否内部命令。
    """

    command: str = ""
    origin_type: int = 0  # Player
    internal: bool = False

    packet_type: BedrockPacketType = field(
        default=BedrockPacketType.COMMAND_REQUEST, repr=False
    )

    #: Bedrock 网络 ID (id.go ID_COMMAND_REQUEST = 0x4D = 77)。
    PACKET_ID: int = 77

    def encode(self) -> bytes:
        out = bytearray()
        out += self._encode_header(self.PACKET_ID)
        out += _encode_string(self.command)
        out += _encode_varuint(self.origin_type)
        # Player origin: 4 字符占位 (空串) + VarInt64 entity_id + 空串
        out += _encode_string("")
        out += _encode_varuint(0)  # entity unique id (占位)
        out += _encode_string("")
        out += struct.pack("<B", 1 if self.internal else 0)
        return bytes(out)

    def decode(self, data: bytes) -> None:
        buf = io.BytesIO(data)
        self._decode_header(buf)
        self.command = _decode_string(buf)
        self.origin_type = _decode_varuint(buf)
        # 跳过 origin 变长字段 (保守: 读到 internal flag 前)
        # 此处仅解析 command 与 origin_type, 其余字段忽略
        self.internal = False


@dataclass
class LoginPacket(BedrockPacket):
    """登录包 — RakNet 握手后首个 Bedrock 包。

    对应 Community-Bot strings 的 ``[-] Login Success!`` 流程。
    携带 chainInfo (JWT 链) 与 clientData (JWT)。

    Attributes
    ----------
    chain_data : bytes
        完整的登录 payload (协议层构造, 通常由
        :mod:`app.protocol.jwt_chain` 生成)。
    protocol_version : int
        Bedrock 协议版本 (Bedrock 1.21.x = 685 / 712 等, 非 RakNet 协议版本)。
    """

    chain_data: bytes = b""
    protocol_version: int = 0

    packet_type: BedrockPacketType = field(
        default=BedrockPacketType.LOGIN, repr=False
    )

    #: Bedrock 网络 ID (id.go ID_LOGIN = 0x01 = 1)。
    PACKET_ID: int = 1

    def encode(self) -> bytes:
        out = bytearray()
        out += self._encode_header(self.PACKET_ID)
        out += _encode_varuint(self.protocol_version)
        out += self.chain_data
        return bytes(out)

    def decode(self, data: bytes) -> None:
        buf = io.BytesIO(data)
        self._decode_header(buf)
        self.protocol_version = _decode_varuint(buf)
        self.chain_data = buf.read()


# ===========================================================================
# 包注册表 (用于按 ID / 类名查找包类)
# ===========================================================================
#: Community-Bot C++ 类名 → Python 包类 映射。
PACKET_CLASS_BY_NAME: Dict[str, type] = {
    BedrockPacketType.BLOCK_ACTOR_DATA.value: BlockActorDataPacket,
    BedrockPacketType.CONTAINER_CLOSE.value: ContainerClosePacket,
    BedrockPacketType.ITEM_STACK_REQUEST.value: ItemStackRequestPacket,
    BedrockPacketType.PLAYER_HOTBAR.value: PlayerHotBarPacket,
    BedrockPacketType.TEXT.value: TextPacket,
    BedrockPacketType.COMMAND_REQUEST.value: CommandRequestPacket,
    BedrockPacketType.LOGIN.value: LoginPacket,
}


def get_packet_class(name: str) -> Optional[type]:
    """按 Community-Bot C++ 类名查找对应的 Python 包类。

    Parameters
    ----------
    name:
        C++ 类名 (如 ``"BlockActorDataPacket"``)。

    Returns
    -------
    Optional[type]
        对应的 :class:`BedrockPacket` 子类; 未注册时返回 ``None``。
    """
    return PACKET_CLASS_BY_NAME.get(name)


def create_packet(name: str, **kwargs: Any) -> BedrockPacket:
    """按 C++ 类名构造一个包实例。

    Parameters
    ----------
    name:
        C++ 类名。
    **kwargs:
        传递给包类构造函数的字段。

    Returns
    -------
    BedrockPacket
        新构造的包实例。

    Raises
    ------
    KeyError
        类名未注册时抛出。
    """
    cls = PACKET_CLASS_BY_NAME.get(name)
    if cls is None:
        raise KeyError(f"未注册的包类型: {name}")
    return cls(**kwargs)


# 延迟填充 _PACKET_ID_MAP (避免在枚举定义时尚未就绪)
_PACKET_ID_MAP = {
    BedrockPacketType.BLOCK_ACTOR_DATA: BlockActorDataPacket.PACKET_ID,
    BedrockPacketType.CONTAINER_CLOSE: ContainerClosePacket.PACKET_ID,
    BedrockPacketType.ITEM_STACK_REQUEST: ItemStackRequestPacket.PACKET_ID,
    BedrockPacketType.PLAYER_HOTBAR: PlayerHotBarPacket.PACKET_ID,
    BedrockPacketType.TEXT: TextPacket.PACKET_ID,
    BedrockPacketType.COMMAND_REQUEST: CommandRequestPacket.PACKET_ID,
    BedrockPacketType.LOGIN: LoginPacket.PACKET_ID,
}


__all__ = [
    # 枚举
    "BedrockPacketType",
    # 基类
    "BedrockPacket",
    # 具体包
    "BlockActorDataPacket",
    "ContainerClosePacket",
    "ItemStackRequestPacket",
    "PlayerHotBarPacket",
    "TextPacket",
    "CommandRequestPacket",
    "LoginPacket",
    # 注册表
    "PACKET_CLASS_BY_NAME",
    "get_packet_class",
    "create_packet",
]
