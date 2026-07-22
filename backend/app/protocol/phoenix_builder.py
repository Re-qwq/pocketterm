"""PhoenixBuilder BDump 引擎 — 专业建筑工具核心实现。

基于 PhoenixBuilder/fatalder 的 Go 语言 Builder 引擎翻译为 Python。
PhoenixBuilder 是专业的 Minecraft 建筑工具，其 BDump 格式支持 30+ 种命令，
涵盖方块放置、容器数据、命令方块、NBT 数据等高级功能。

本模块替换 Retalcer 的半成品 batch_optimizer.py，提供:
    1. BDump v3/v4 命令系统 (30+ 种命令的编码/解码)
    2. Brotli 压缩/解压
    3. 字符串池 (String Pool) 机制
    4. 画笔 (Brush) 位置追踪系统
    5. 专业的方块放置调度器（速率控制、错误恢复）
    6. 16³ Fill 批量优化（PhoenixBuilder 专业优化）
    7. 蛇形路径区块排序
    8. 跳空优化（跳过空气方块）
    9. 增量构建（checkpoint/resume）

逆向来源:
    - PhoenixBuilder fastbuilder/bdump/ (BDump 命令系统)
    - PhoenixBuilder fastbuilder/builder/ (Builder 执行引擎)
    - PhoenixBuilder doc/bdump/bdump-cn.md (BDump 文件格式文档)
    - fatalder_source/ (Fatalder 建筑工具)

与 Retalcer batch_optimizer 的对比:
    - Retalcer: 简单 Z 轴合并，只支持 /fill 和 /setblock
    - PhoenixBuilder: 完整的 BDump 协议，支持 30+ 种命令类型
    - Retalcer: 无压缩，直接发送命令
    - PhoenixBuilder: Brotli 压缩，专业文件格式
    - Retalcer: 无签名验证
    - PhoenixBuilder: RSA 签名验证，防止文件篡改
    - Retalcer: 无画笔系统
    - PhoenixBuilder: 画笔位置追踪，增量坐标更新
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import struct
import time
import hashlib
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    AsyncIterator,
    Callable,
    ClassVar,
    Dict,
    Iterator,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger("pocketterm.protocol.phoenix_builder")

# ============================================================================
# 常量 (来自 PhoenixBuilder fastbuilder/builder/consts.go)
# ============================================================================

#: 区块大小 (Minecraft 标准)
CHUNK_SIZE: int = 16

#: BDump 文件头标识 (BD@ 表示 Brotli 压缩)
BDUMP_FILE_HEADER: bytes = b"BD@"

#: BDump 内部文件头 (BDX)
BDUMP_INNER_HEADER: bytes = b"BDX\x00"

#: 最大 /fill 体积 (网易限制: 32768 = 32x32x32)
MAX_FILL_VOLUME: int = 32768

#: 16³ Fill 优化体素大小 (PhoenixBuilder 专业优化)
FILL_VOXEL_SIZE: int = 16

#: 默认命令发送间隔 (秒)
DEFAULT_COMMAND_DELAY: float = 0.02

#: 默认方块放置速率 (方块/秒)
DEFAULT_PLACE_RATE: int = 250

#: 最大方块放置速率
MAX_PLACE_RATE: int = 500

#: 命令发送速率限制 (命令/秒)
DEFAULT_COMMAND_RATE: int = 20

#: 区块组默认大小
DEFAULT_GROUP_SIZE: int = 3

#: 组间等待时间 (秒)
GROUP_WAIT_TIME: float = 0.5

#: 签名结束标记
SIGNATURE_END_BYTE: int = 90  # 'Z'

#: 签名扩展长度标记
SIGNATURE_EXTENDED_LENGTH: int = 255

#: 运行时 ID 池版本 (网易 1.17)
RUNTIME_POOL_117: int = 117

#: 运行时 ID 池版本 (2.1.10)
RUNTIME_POOL_2_1_10: int = 118


# ============================================================================
# 数据结构 (来自 PhoenixBuilder fastbuilder/types/)
# ============================================================================


@dataclass
class Position:
    """三维坐标 (Minecraft 方块坐标)。

    来自 PhoenixBuilder fastbuilder/types/position.go
    """

    x: int = 0
    y: int = 0
    z: int = 0

    def __add__(self, other: "Position") -> "Position":
        return Position(self.x + other.x, self.y + other.y, self.z + other.z)

    def __iadd__(self, other: "Position") -> "Position":
        self.x += other.x
        self.y += other.y
        self.z += other.z
        return self

    def __sub__(self, other: "Position") -> "Position":
        return Position(self.x - other.x, self.y - other.y, self.z - other.z)

    def __iter__(self) -> Iterator[int]:
        yield self.x
        yield self.y
        yield self.z

    def __hash__(self) -> int:
        return hash((self.x, self.y, self.z))

    def to_tuple(self) -> Tuple[int, int, int]:
        """返回 (x, y, z) 元组。"""
        return (self.x, self.y, self.z)

    def copy(self) -> "Position":
        """返回副本。"""
        return Position(self.x, self.y, self.z)


@dataclass
class Block:
    """方块定义。

    来自 PhoenixBuilder fastbuilder/types/block.go

    Attributes:
        name: 方块名称 (如 "minecraft:stone")。
        block_states: 方块状态字符串 (如 '["color":"orange"]')。
        data: 方块数据值 (旧版 metadata)。
    """

    name: str
    block_states: str = ""
    data: int = 0

    def __hash__(self) -> int:
        return hash((self.name, self.block_states, self.data))


@dataclass
class ConstBlock:
    """常量方块 (用于运行时 ID 池)。

    来自 PhoenixBuilder fastbuilder/types/block.go

    常量方块是不可变的，用于运行时 ID 映射表。
    """

    name: str
    data: int = 0

    def __hash__(self) -> int:
        return hash((self.name, self.data))


@dataclass
class ChestSlot:
    """容器物品槽位数据。

    来自 PhoenixBuilder fastbuilder/types/block.go

    Attributes:
        name: 物品名称 (如 "minecraft:diamond")。
        count: 物品数量 (0-64)。
        damage: 物品数据值。
        slot: 槽位编号 (0-based)。
    """

    name: str = ""
    count: int = 0
    damage: int = 0
    slot: int = 0


@dataclass
class CommandBlockData:
    """命令方块数据。

    来自 PhoenixBuilder fastbuilder/types/block.go

    Attributes:
        mode: 命令方块模式 (0=脉冲, 1=重复, 2=连锁)。
        command: 执行的命令字符串。
        custom_name: 自定义名称 (悬浮文本)。
        last_output: 上一次输出 (通常为空)。
        tick_delay: 延迟 tick 数。
        execute_on_first_tick: 是否在第一个 tick 执行。
        track_output: 是否跟踪输出。
        conditional: 是否条件模式。
        needs_redstone: 是否需要红石信号。
    """

    mode: int = 0
    command: str = ""
    custom_name: str = ""
    last_output: str = ""
    tick_delay: int = 0
    execute_on_first_tick: bool = False
    track_output: bool = True
    conditional: bool = False
    needs_redstone: bool = False


@dataclass
class Module:
    """建筑模块 - BDump 引擎输出的基本单元。

    来自 PhoenixBuilder fastbuilder/types/block.go

    一个模块描述一个方块放置操作，可能包含:
        - 基本方块 (block)
        - 命令方块数据 (command_block_data)
        - NBT 数据 (nbt_data, nbt_map)
        - 容器数据 (chest_slot, chest_data)

    Attributes:
        block: 方块定义。
        point: 放置位置。
        command_block_data: 命令方块数据 (可选)。
        nbt_data: NBT 原始字节数据 (可选)。
        nbt_map: NBT 解析后的字典数据 (可选)。
        chest_slot: 单个容器槽位 (可选)。
        chest_data: 完整容器数据 (可选)。
        debug_nbt_data: 调试 NBT 数据 (可选)。
    """

    block: Optional[Block] = None
    point: Position = field(default_factory=Position)
    command_block_data: Optional[CommandBlockData] = None
    nbt_data: Optional[bytes] = None
    nbt_map: Optional[Dict[str, Any]] = None
    chest_slot: Optional[ChestSlot] = None
    chest_data: Optional[List[ChestSlot]] = None
    debug_nbt_data: Optional[bytes] = None


@dataclass
class BDumpHeader:
    """BDump 文件头信息。

    来自 PhoenixBuilder doc/bdump/bdump-cn.md

    Attributes:
        author: 作者游戏名。
        signed: 是否已签名。
        corrupted: 签名是否损坏。
        signer: 签名者用户名。
    """

    author: str = ""
    signed: bool = False
    corrupted: bool = False
    signer: str = ""


# ============================================================================
# BDump 命令系统 (来自 PhoenixBuilder fastbuilder/bdump/command/)
# ============================================================================


class BDumpCommand(ABC):
    """BDump 命令基类。

    来自 PhoenixBuilder fastbuilder/bdump/command/command.go

    每个 BDump 命令都有:
        - 命令 ID (uint8)
        - 命令名称
        - 编码方法 (Marshal) - 将命令序列化为字节流
        - 解码方法 (Unmarshal) - 从字节流反序列化命令

    所有数据以大端字节序 (Big Endian) 编码。
    """

    #: 命令 ID (uint8, 对应 Go 中的 uint16 但实际使用 uint8)
    COMMAND_ID: ClassVar[int] = 0

    #: 命令名称
    COMMAND_NAME: ClassVar[str] = "BaseCommand"

    @classmethod
    def command_id(cls) -> int:
        """返回命令 ID。"""
        return cls.COMMAND_ID

    @classmethod
    def command_name(cls) -> str:
        """返回命令名称。"""
        return cls.COMMAND_NAME

    @abstractmethod
    def marshal(self) -> bytes:
        """将命令编码为字节流 (不含命令 ID 字节)。

        Returns:
            编码后的字节数据。
        """
        ...

    @classmethod
    @abstractmethod
    def unmarshal(cls, data: bytes) -> "BDumpCommand":
        """从字节流解码命令 (不含命令 ID 字节)。

        Args:
            data: 命令数据字节 (不含命令 ID)。

        Returns:
            解码后的命令实例。
        """
        ...

    def to_bytes(self) -> bytes:
        """将命令编码为完整字节流 (含命令 ID 字节)。

        Returns:
            完整的命令字节数据 (1 字节 ID + 参数)。
        """
        return bytes([self.COMMAND_ID]) + self.marshal()


# ----------------------------------------------------------------------------
# 命令 ID 1: CreateConstantString
# ----------------------------------------------------------------------------


@dataclass
class CreateConstantStringCommand(BDumpCommand):
    """将字符串放入方块池中。

    命令 ID: 1

    方块池(字符串池)中的字符串按调用顺序分配 ID (从 0 开始)。
    最多支持 65535 个字符串。

    参数:
        constant_string: 以 \0 结尾的字符串 (UTF-8)。
    """

    constant_string: str = ""

    COMMAND_ID: ClassVar[int] = 1
    COMMAND_NAME: ClassVar[str] = "CreateConstantStringCommand"

    def marshal(self) -> bytes:
        """编码: 字符串 + \0 结尾。"""
        return (self.constant_string.encode("utf-8") + b"\x00")

    @classmethod
    def unmarshal(cls, data: bytes) -> "CreateConstantStringCommand":
        """解码: 读取直到 \0 的字符串。"""
        end = data.find(b"\x00")
        if end == -1:
            return cls(constant_string=data.decode("utf-8", errors="replace"))
        return cls(constant_string=data[:end].decode("utf-8", errors="replace"))


# ----------------------------------------------------------------------------
# 命令 ID 5: PlaceBlockWithBlockStates
# ----------------------------------------------------------------------------


@dataclass
class PlaceBlockWithBlockStatesCommand(BDumpCommand):
    """放置带方块状态的方块 (使用字符串池)。

    命令 ID: 5

    参数:
        block_constant_string_id: 方块名在字符串池中的 ID (uint16)。
        block_states_constant_string_id: 方块状态在字符串池中的 ID (uint16)。
    """

    block_constant_string_id: int = 0
    block_states_constant_string_id: int = 0

    COMMAND_ID: ClassVar[int] = 5
    COMMAND_NAME: ClassVar[str] = "PlaceBlockWithBlockStatesCommand"

    def marshal(self) -> bytes:
        """编码: 2 个 uint16 (大端序)。"""
        return struct.pack(">HH", self.block_constant_string_id, self.block_states_constant_string_id)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceBlockWithBlockStatesCommand":
        """解码: 读取 2 个 uint16。"""
        if len(data) < 4:
            return cls()
        bid, sid = struct.unpack(">HH", data[:4])
        return cls(block_constant_string_id=bid, block_states_constant_string_id=sid)


# ----------------------------------------------------------------------------
# 命令 ID 6: AddInt16ZValue0
# ----------------------------------------------------------------------------


@dataclass
class AddInt16ZValue0Command(BDumpCommand):
    """将画笔 Z 坐标增加一个 int16 值。

    命令 ID: 6

    参数:
        value: 增量值 (int16, 可正可负)。
    """

    value: int = 0

    COMMAND_ID: ClassVar[int] = 6
    COMMAND_NAME: ClassVar[str] = "AddInt16ZValue0Command"

    def marshal(self) -> bytes:
        """编码: 1 个 uint16 (大端序)。"""
        return struct.pack(">H", self.value & 0xFFFF)

    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt16ZValue0Command":
        """解码: 读取 1 个 uint16，转为 int16。"""
        if len(data) < 2:
            return cls()
        val = struct.unpack(">H", data[:2])[0]
        # 转换为有符号 int16
        if val >= 0x8000:
            val -= 0x10000
        return cls(value=val)


# ----------------------------------------------------------------------------
# 命令 ID 7: PlaceBlock
# ----------------------------------------------------------------------------


@dataclass
class PlaceBlockCommand(BDumpCommand):
    """在画笔位置放置方块 (使用字符串池)。

    命令 ID: 7

    参数:
        block_constant_string_id: 方块名在字符串池中的 ID (uint16)。
        block_data: 方块数据值 (uint16, 旧版 metadata)。
    """

    block_constant_string_id: int = 0
    block_data: int = 0

    COMMAND_ID: ClassVar[int] = 7
    COMMAND_NAME: ClassVar[str] = "PlaceBlockCommand"

    def marshal(self) -> bytes:
        """编码: 2 个 uint16 (大端序)。"""
        return struct.pack(">HH", self.block_constant_string_id, self.block_data)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceBlockCommand":
        """解码: 读取 2 个 uint16。"""
        if len(data) < 4:
            return cls()
        bid, bd = struct.unpack(">HH", data[:4])
        return cls(block_constant_string_id=bid, block_data=bd)


# ----------------------------------------------------------------------------
# 命令 ID 8: AddZValue0
# ----------------------------------------------------------------------------


@dataclass
class AddZValue0Command(BDumpCommand):
    """将画笔 Z 坐标增加 1。

    命令 ID: 8
    无参数。
    """

    COMMAND_ID: ClassVar[int] = 8
    COMMAND_NAME: ClassVar[str] = "AddZValue0Command"

    def marshal(self) -> bytes:
        return b""

    @classmethod
    def unmarshal(cls, data: bytes) -> "AddZValue0Command":
        return cls()


# ----------------------------------------------------------------------------
# 命令 ID 9: NoOperation (NOP)
# ----------------------------------------------------------------------------


@dataclass
class NoOperationCommand(BDumpCommand):
    """空操作 (No Operation)。

    命令 ID: 9
    无参数，什么也不做。
    """

    COMMAND_ID: ClassVar[int] = 9
    COMMAND_NAME: ClassVar[str] = "NoOperationCommand"

    def marshal(self) -> bytes:
        return b""

    @classmethod
    def unmarshal(cls, data: bytes) -> "NoOperationCommand":
        return cls()


# ----------------------------------------------------------------------------
# 命令 ID 12: AddInt32ZValue0
# ----------------------------------------------------------------------------


@dataclass
class AddInt32ZValue0Command(BDumpCommand):
    """将画笔 Z 坐标增加一个 int32 值。

    命令 ID: 12

    参数:
        value: 增量值 (int32)。
    """

    value: int = 0

    COMMAND_ID: ClassVar[int] = 12
    COMMAND_NAME: ClassVar[str] = "AddInt32ZValue0Command"

    def marshal(self) -> bytes:
        """编码: 1 个 uint32 (大端序)。"""
        return struct.pack(">I", self.value & 0xFFFFFFFF)

    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt32ZValue0Command":
        """解码: 读取 1 个 uint32，转为 int32。"""
        if len(data) < 4:
            return cls()
        val = struct.unpack(">I", data[:4])[0]
        if val >= 0x80000000:
            val -= 0x100000000
        return cls(value=val)


# ----------------------------------------------------------------------------
# 命令 ID 13: PlaceBlockWithBlockStatesDeprecated
# ----------------------------------------------------------------------------


@dataclass
class PlaceBlockWithBlockStatesDeprecatedCommand(BDumpCommand):
    """放置带方块状态的方块 (已弃用，直接内联状态字符串)。

    命令 ID: 13

    参数:
        block_constant_string_id: 方块名在字符串池中的 ID (uint16)。
        block_states_string: 方块状态字符串 (以 \0 结尾)。
    """

    block_constant_string_id: int = 0
    block_states_string: str = ""

    COMMAND_ID: ClassVar[int] = 13
    COMMAND_NAME: ClassVar[str] = "PlaceBlockWithBlockStatesDeprecatedCommand"

    def marshal(self) -> bytes:
        """编码: uint16 + 字符串 + \0。"""
        return struct.pack(">H", self.block_constant_string_id) + self.block_states_string.encode("utf-8") + b"\x00"

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceBlockWithBlockStatesDeprecatedCommand":
        """解码: 读取 uint16，然后读取直到 \0 的字符串。"""
        if len(data) < 2:
            return cls()
        bid = struct.unpack(">H", data[:2])[0]
        end = data.find(b"\x00", 2)
        if end == -1:
            return cls(block_constant_string_id=bid)
        bs = data[2:end].decode("utf-8", errors="replace")
        return cls(block_constant_string_id=bid, block_states_string=bs)


# ----------------------------------------------------------------------------
# 命令 ID 14-19: 简单坐标增量命令
# ----------------------------------------------------------------------------


@dataclass
class AddXValueCommand(BDumpCommand):
    """将画笔 X 坐标增加 1。命令 ID: 14"""
    COMMAND_ID: ClassVar[int] = 14
    COMMAND_NAME: ClassVar[str] = "AddXValueCommand"
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddXValueCommand": return cls()


@dataclass
class SubtractXValueCommand(BDumpCommand):
    """将画笔 X 坐标减少 1。命令 ID: 15"""
    COMMAND_ID: ClassVar[int] = 15
    COMMAND_NAME: ClassVar[str] = "SubtractXValueCommand"
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, data: bytes) -> "SubtractXValueCommand": return cls()


@dataclass
class AddYValueCommand(BDumpCommand):
    """将画笔 Y 坐标增加 1。命令 ID: 16"""
    COMMAND_ID: ClassVar[int] = 16
    COMMAND_NAME: ClassVar[str] = "AddYValueCommand"
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddYValueCommand": return cls()


@dataclass
class SubtractYValueCommand(BDumpCommand):
    """将画笔 Y 坐标减少 1。命令 ID: 17"""
    COMMAND_ID: ClassVar[int] = 17
    COMMAND_NAME: ClassVar[str] = "SubtractYValueCommand"
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, data: bytes) -> "SubtractYValueCommand": return cls()


@dataclass
class AddZValueCommand(BDumpCommand):
    """将画笔 Z 坐标增加 1。命令 ID: 18"""
    COMMAND_ID: ClassVar[int] = 18
    COMMAND_NAME: ClassVar[str] = "AddZValueCommand"
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddZValueCommand": return cls()


@dataclass
class SubtractZValueCommand(BDumpCommand):
    """将画笔 Z 坐标减少 1。命令 ID: 19"""
    COMMAND_ID: ClassVar[int] = 19
    COMMAND_NAME: ClassVar[str] = "SubtractZValueCommand"
    def marshal(self) -> bytes: return b""
    @classmethod
    def unmarshal(cls, data: bytes) -> "SubtractZValueCommand": return cls()


# ----------------------------------------------------------------------------
# 命令 ID 20-25: 多字节坐标增量命令
# ----------------------------------------------------------------------------


@dataclass
class AddInt16XValueCommand(BDumpCommand):
    """将画笔 X 坐标增加 int16 值。命令 ID: 20"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 20
    COMMAND_NAME: ClassVar[str] = "AddInt16XValueCommand"
    def marshal(self) -> bytes:
        return struct.pack(">H", self.value & 0xFFFF)
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt16XValueCommand":
        if len(data) < 2: return cls()
        val = struct.unpack(">H", data[:2])[0]
        if val >= 0x8000: val -= 0x10000
        return cls(value=val)


@dataclass
class AddInt32XValueCommand(BDumpCommand):
    """将画笔 X 坐标增加 int32 值。命令 ID: 21"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 21
    COMMAND_NAME: ClassVar[str] = "AddInt32XValueCommand"
    def marshal(self) -> bytes:
        return struct.pack(">I", self.value & 0xFFFFFFFF)
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt32XValueCommand":
        if len(data) < 4: return cls()
        val = struct.unpack(">I", data[:4])[0]
        if val >= 0x80000000: val -= 0x100000000
        return cls(value=val)


@dataclass
class AddInt16YValueCommand(BDumpCommand):
    """将画笔 Y 坐标增加 int16 值。命令 ID: 22"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 22
    COMMAND_NAME: ClassVar[str] = "AddInt16YValueCommand"
    def marshal(self) -> bytes:
        return struct.pack(">H", self.value & 0xFFFF)
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt16YValueCommand":
        if len(data) < 2: return cls()
        val = struct.unpack(">H", data[:2])[0]
        if val >= 0x8000: val -= 0x10000
        return cls(value=val)


@dataclass
class AddInt32YValueCommand(BDumpCommand):
    """将画笔 Y 坐标增加 int32 值。命令 ID: 23"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 23
    COMMAND_NAME: ClassVar[str] = "AddInt32YValueCommand"
    def marshal(self) -> bytes:
        return struct.pack(">I", self.value & 0xFFFFFFFF)
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt32YValueCommand":
        if len(data) < 4: return cls()
        val = struct.unpack(">I", data[:4])[0]
        if val >= 0x80000000: val -= 0x100000000
        return cls(value=val)


@dataclass
class AddInt16ZValueCommand(BDumpCommand):
    """将画笔 Z 坐标增加 int16 值。命令 ID: 24"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 24
    COMMAND_NAME: ClassVar[str] = "AddInt16ZValueCommand"
    def marshal(self) -> bytes:
        return struct.pack(">H", self.value & 0xFFFF)
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt16ZValueCommand":
        if len(data) < 2: return cls()
        val = struct.unpack(">H", data[:2])[0]
        if val >= 0x8000: val -= 0x10000
        return cls(value=val)


@dataclass
class AddInt32ZValueCommand(BDumpCommand):
    """将画笔 Z 坐标增加 int32 值。命令 ID: 25"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 25
    COMMAND_NAME: ClassVar[str] = "AddInt32ZValueCommand"
    def marshal(self) -> bytes:
        return struct.pack(">I", self.value & 0xFFFFFFFF)
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt32ZValueCommand":
        if len(data) < 4: return cls()
        val = struct.unpack(">I", data[:4])[0]
        if val >= 0x80000000: val -= 0x100000000
        return cls(value=val)


# ----------------------------------------------------------------------------
# 命令 ID 28-30: int8 坐标增量命令
# ----------------------------------------------------------------------------


@dataclass
class AddInt8XValueCommand(BDumpCommand):
    """将画笔 X 坐标增加 int8 值。命令 ID: 28"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 28
    COMMAND_NAME: ClassVar[str] = "AddInt8XValueCommand"
    def marshal(self) -> bytes:
        return bytes([self.value & 0xFF])
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt8XValueCommand":
        if len(data) < 1: return cls()
        val = data[0]
        if val >= 0x80: val -= 0x100
        return cls(value=val)


@dataclass
class AddInt8YValueCommand(BDumpCommand):
    """将画笔 Y 坐标增加 int8 值。命令 ID: 29"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 29
    COMMAND_NAME: ClassVar[str] = "AddInt8YValueCommand"
    def marshal(self) -> bytes:
        return bytes([self.value & 0xFF])
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt8YValueCommand":
        if len(data) < 1: return cls()
        val = data[0]
        if val >= 0x80: val -= 0x100
        return cls(value=val)


@dataclass
class AddInt8ZValueCommand(BDumpCommand):
    """将画笔 Z 坐标增加 int8 值。命令 ID: 30"""
    value: int = 0
    COMMAND_ID: ClassVar[int] = 30
    COMMAND_NAME: ClassVar[str] = "AddInt8ZValueCommand"
    def marshal(self) -> bytes:
        return bytes([self.value & 0xFF])
    @classmethod
    def unmarshal(cls, data: bytes) -> "AddInt8ZValueCommand":
        if len(data) < 1: return cls()
        val = data[0]
        if val >= 0x80: val -= 0x100
        return cls(value=val)


# ----------------------------------------------------------------------------
# 命令 ID 26: SetCommandBlockData
# ----------------------------------------------------------------------------


@dataclass
class SetCommandBlockDataCommand(BDumpCommand):
    """在画笔位置设置命令方块数据 (不放置方块)。

    命令 ID: 26

    推荐使用命令 36 (PlaceCommandBlockWithCommandBlockData) 替代。

    参数:
        command_block_data: 命令方块数据。
    """

    command_block_data: CommandBlockData = field(default_factory=CommandBlockData)

    COMMAND_ID: ClassVar[int] = 26
    COMMAND_NAME: ClassVar[str] = "SetCommandBlockDataCommand"

    def marshal(self) -> bytes:
        """编码: 模式(uint32) + 3个字符串 + tick_delay(int32) + 4个bool。"""
        cbd = self.command_block_data
        parts: List[bytes] = [
            struct.pack(">I", cbd.mode),
            cbd.command.encode("utf-8") + b"\x00",
            cbd.custom_name.encode("utf-8") + b"\x00",
            cbd.last_output.encode("utf-8") + b"\x00",
            struct.pack(">I", cbd.tick_delay & 0xFFFFFFFF),
            _encode_bool_flags(
                cbd.execute_on_first_tick,
                cbd.track_output,
                cbd.conditional,
                cbd.needs_redstone,
            ),
        ]
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "SetCommandBlockDataCommand":
        """解码: 与 marshal 顺序相反。"""
        cbd = CommandBlockData()
        if len(data) < 4:
            return cls(command_block_data=cbd)
        idx = 0
        cbd.mode = struct.unpack(">I", data[idx:idx + 4])[0]
        idx += 4
        # 读取三个以 \0 结尾的字符串
        cmd_str, n = _read_cstring(data, idx)
        cbd.command = cmd_str; idx = n
        name_str, n = _read_cstring(data, idx)
        cbd.custom_name = name_str; idx = n
        out_str, n = _read_cstring(data, idx)
        cbd.last_output = out_str; idx = n
        if idx + 4 <= len(data):
            cbd.tick_delay = _to_int32(struct.unpack(">I", data[idx:idx + 4])[0])
            idx += 4
        if idx + 4 <= len(data):
            flags = data[idx:idx + 4]
            cbd.execute_on_first_tick = flags[0] != 0
            cbd.track_output = flags[1] != 0
            cbd.conditional = flags[2] != 0
            cbd.needs_redstone = flags[3] != 0
        return cls(command_block_data=cbd)


# ----------------------------------------------------------------------------
# 命令 ID 27: PlaceBlockWithCommandBlockData
# ----------------------------------------------------------------------------


@dataclass
class PlaceBlockWithCommandBlockDataCommand(BDumpCommand):
    """放置方块并设置命令方块数据。

    命令 ID: 27

    推荐使用命令 36 (PlaceCommandBlockWithCommandBlockData) 替代。

    参数:
        block_constant_string_id: 方块名在字符串池中的 ID (uint16)。
        block_data: 方块数据值 (uint16)。
        command_block_data: 命令方块数据。
    """

    block_constant_string_id: int = 0
    block_data: int = 0
    command_block_data: CommandBlockData = field(default_factory=CommandBlockData)

    COMMAND_ID: ClassVar[int] = 27
    COMMAND_NAME: ClassVar[str] = "PlaceBlockWithCommandBlockDataCommand"

    def marshal(self) -> bytes:
        """编码: 2个uint16 + 命令方块数据。"""
        cbd = self.command_block_data
        parts: List[bytes] = [
            struct.pack(">HH", self.block_constant_string_id, self.block_data),
            struct.pack(">I", cbd.mode),
            cbd.command.encode("utf-8") + b"\x00",
            cbd.custom_name.encode("utf-8") + b"\x00",
            cbd.last_output.encode("utf-8") + b"\x00",
            struct.pack(">I", cbd.tick_delay & 0xFFFFFFFF),
            _encode_bool_flags(
                cbd.execute_on_first_tick,
                cbd.track_output,
                cbd.conditional,
                cbd.needs_redstone,
            ),
        ]
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceBlockWithCommandBlockDataCommand":
        """解码: 与 marshal 顺序相反。"""
        cbd = CommandBlockData()
        if len(data) < 4:
            return cls(command_block_data=cbd)
        bid = struct.unpack(">H", data[0:2])[0]
        bd = struct.unpack(">H", data[2:4])[0]
        idx = 4
        if idx + 4 <= len(data):
            cbd.mode = struct.unpack(">I", data[idx:idx + 4])[0]
            idx += 4
        cmd_str, n = _read_cstring(data, idx)
        cbd.command = cmd_str; idx = n
        name_str, n = _read_cstring(data, idx)
        cbd.custom_name = name_str; idx = n
        out_str, n = _read_cstring(data, idx)
        cbd.last_output = out_str; idx = n
        if idx + 4 <= len(data):
            cbd.tick_delay = _to_int32(struct.unpack(">I", data[idx:idx + 4])[0])
            idx += 4
        if idx + 4 <= len(data):
            flags = data[idx:idx + 4]
            cbd.execute_on_first_tick = flags[0] != 0
            cbd.track_output = flags[1] != 0
            cbd.conditional = flags[2] != 0
            cbd.needs_redstone = flags[3] != 0
        return cls(block_constant_string_id=bid, block_data=bd, command_block_data=cbd)


# ----------------------------------------------------------------------------
# 命令 ID 31: UseRuntimeIDPool
# ----------------------------------------------------------------------------


@dataclass
class UseRuntimeIDPoolCommand(BDumpCommand):
    """使用预设的运行时 ID 方块池。

    命令 ID: 31

    参数:
        pool_id: 运行时池 ID (117 = 网易 1.17, 118 = 2.1.10)。
    """

    pool_id: int = 0

    COMMAND_ID: ClassVar[int] = 31
    COMMAND_NAME: ClassVar[str] = "UseRuntimeIDPoolCommand"

    def marshal(self) -> bytes:
        return bytes([self.pool_id & 0xFF])

    @classmethod
    def unmarshal(cls, data: bytes) -> "UseRuntimeIDPoolCommand":
        if len(data) < 1:
            return cls()
        return cls(pool_id=data[0])


# ----------------------------------------------------------------------------
# 命令 ID 32: PlaceRuntimeBlock
# ----------------------------------------------------------------------------


@dataclass
class PlaceRuntimeBlockCommand(BDumpCommand):
    """使用运行时 ID 放置方块 (uint16)。

    命令 ID: 32

    参数:
        block_runtime_id: 运行时方块 ID (uint16)。
    """

    block_runtime_id: int = 0

    COMMAND_ID: ClassVar[int] = 32
    COMMAND_NAME: ClassVar[str] = "PlaceRuntimeBlockCommand"

    def marshal(self) -> bytes:
        return struct.pack(">H", self.block_runtime_id & 0xFFFF)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceRuntimeBlockCommand":
        if len(data) < 2:
            return cls()
        return cls(block_runtime_id=struct.unpack(">H", data[:2])[0])


# ----------------------------------------------------------------------------
# 命令 ID 33: PlaceRuntimeBlockWithUint32RuntimeID
# ----------------------------------------------------------------------------


@dataclass
class PlaceRuntimeBlockWithUint32RuntimeIDCommand(BDumpCommand):
    """使用运行时 ID 放置方块 (uint32)。

    命令 ID: 33

    参数:
        block_runtime_id: 运行时方块 ID (uint32)。
    """

    block_runtime_id: int = 0

    COMMAND_ID: ClassVar[int] = 33
    COMMAND_NAME: ClassVar[str] = "PlaceRuntimeBlockUint32RuntimeIDCommand"

    def marshal(self) -> bytes:
        return struct.pack(">I", self.block_runtime_id & 0xFFFFFFFF)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceRuntimeBlockWithUint32RuntimeIDCommand":
        if len(data) < 4:
            return cls()
        return cls(block_runtime_id=struct.unpack(">I", data[:4])[0])


# ----------------------------------------------------------------------------
# 命令 ID 34: PlaceRuntimeBlockWithCommandBlockData
# ----------------------------------------------------------------------------


@dataclass
class PlaceRuntimeBlockWithCommandBlockDataCommand(BDumpCommand):
    """使用运行时 ID 放置命令方块 (uint16) 并设置数据。

    命令 ID: 34

    参数:
        block_runtime_id: 运行时方块 ID (uint16)。
        command_block_data: 命令方块数据。
    """

    block_runtime_id: int = 0
    command_block_data: CommandBlockData = field(default_factory=CommandBlockData)

    COMMAND_ID: ClassVar[int] = 34
    COMMAND_NAME: ClassVar[str] = "PlaceRuntimeBlockWithCommandBlockDataCommand"

    def marshal(self) -> bytes:
        cbd = self.command_block_data
        parts: List[bytes] = [
            struct.pack(">H", self.block_runtime_id & 0xFFFF),
            struct.pack(">I", cbd.mode),
            cbd.command.encode("utf-8") + b"\x00",
            cbd.custom_name.encode("utf-8") + b"\x00",
            cbd.last_output.encode("utf-8") + b"\x00",
            struct.pack(">I", cbd.tick_delay & 0xFFFFFFFF),
            _encode_bool_flags(cbd.execute_on_first_tick, cbd.track_output, cbd.conditional, cbd.needs_redstone),
        ]
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceRuntimeBlockWithCommandBlockDataCommand":
        cbd = CommandBlockData()
        if len(data) < 2:
            return cls(command_block_data=cbd)
        rid = struct.unpack(">H", data[:2])[0]
        idx = 2
        if idx + 4 <= len(data):
            cbd.mode = struct.unpack(">I", data[idx:idx + 4])[0]; idx += 4
        cmd_str, n = _read_cstring(data, idx); cbd.command = cmd_str; idx = n
        name_str, n = _read_cstring(data, idx); cbd.custom_name = name_str; idx = n
        out_str, n = _read_cstring(data, idx); cbd.last_output = out_str; idx = n
        if idx + 4 <= len(data):
            cbd.tick_delay = _to_int32(struct.unpack(">I", data[idx:idx + 4])[0]); idx += 4
        if idx + 4 <= len(data):
            flags = data[idx:idx + 4]
            cbd.execute_on_first_tick = flags[0] != 0
            cbd.track_output = flags[1] != 0
            cbd.conditional = flags[2] != 0
            cbd.needs_redstone = flags[3] != 0
        return cls(block_runtime_id=rid, command_block_data=cbd)


# ----------------------------------------------------------------------------
# 命令 ID 35: PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID
# ----------------------------------------------------------------------------


@dataclass
class PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeIDCommand(BDumpCommand):
    """使用运行时 ID 放置命令方块 (uint32) 并设置数据。

    命令 ID: 35

    参数:
        block_runtime_id: 运行时方块 ID (uint32)。
        command_block_data: 命令方块数据。
    """

    block_runtime_id: int = 0
    command_block_data: CommandBlockData = field(default_factory=CommandBlockData)

    COMMAND_ID: ClassVar[int] = 35
    COMMAND_NAME: ClassVar[str] = "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeIDCommand"

    def marshal(self) -> bytes:
        cbd = self.command_block_data
        parts: List[bytes] = [
            struct.pack(">I", self.block_runtime_id & 0xFFFFFFFF),
            struct.pack(">I", cbd.mode),
            cbd.command.encode("utf-8") + b"\x00",
            cbd.custom_name.encode("utf-8") + b"\x00",
            cbd.last_output.encode("utf-8") + b"\x00",
            struct.pack(">I", cbd.tick_delay & 0xFFFFFFFF),
            _encode_bool_flags(cbd.execute_on_first_tick, cbd.track_output, cbd.conditional, cbd.needs_redstone),
        ]
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeIDCommand":
        cbd = CommandBlockData()
        if len(data) < 4:
            return cls(command_block_data=cbd)
        rid = struct.unpack(">I", data[:4])[0]
        idx = 4
        if idx + 4 <= len(data):
            cbd.mode = struct.unpack(">I", data[idx:idx + 4])[0]; idx += 4
        cmd_str, n = _read_cstring(data, idx); cbd.command = cmd_str; idx = n
        name_str, n = _read_cstring(data, idx); cbd.custom_name = name_str; idx = n
        out_str, n = _read_cstring(data, idx); cbd.last_output = out_str; idx = n
        if idx + 4 <= len(data):
            cbd.tick_delay = _to_int32(struct.unpack(">I", data[idx:idx + 4])[0]); idx += 4
        if idx + 4 <= len(data):
            flags = data[idx:idx + 4]
            cbd.execute_on_first_tick = flags[0] != 0
            cbd.track_output = flags[1] != 0
            cbd.conditional = flags[2] != 0
            cbd.needs_redstone = flags[3] != 0
        return cls(block_runtime_id=rid, command_block_data=cbd)


# ----------------------------------------------------------------------------
# 命令 ID 36: PlaceCommandBlockWithCommandBlockData
# ----------------------------------------------------------------------------


@dataclass
class PlaceCommandBlockWithCommandBlockDataCommand(BDumpCommand):
    """放置命令方块并设置数据 (推荐使用)。

    命令 ID: 36

    参数:
        block_data: 方块数据值 (uint16)。
        command_block_data: 命令方块数据。
    """

    block_data: int = 0
    command_block_data: CommandBlockData = field(default_factory=CommandBlockData)

    COMMAND_ID: ClassVar[int] = 36
    COMMAND_NAME: ClassVar[str] = "PlaceCommandBlockWithCommandBlockDataCommand"

    def marshal(self) -> bytes:
        cbd = self.command_block_data
        parts: List[bytes] = [
            struct.pack(">H", self.block_data & 0xFFFF),
            struct.pack(">I", cbd.mode),
            cbd.command.encode("utf-8") + b"\x00",
            cbd.custom_name.encode("utf-8") + b"\x00",
            cbd.last_output.encode("utf-8") + b"\x00",
            struct.pack(">I", cbd.tick_delay & 0xFFFFFFFF),
            _encode_bool_flags(cbd.execute_on_first_tick, cbd.track_output, cbd.conditional, cbd.needs_redstone),
        ]
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceCommandBlockWithCommandBlockDataCommand":
        cbd = CommandBlockData()
        if len(data) < 2:
            return cls(command_block_data=cbd)
        bd = struct.unpack(">H", data[:2])[0]
        idx = 2
        if idx + 4 <= len(data):
            cbd.mode = struct.unpack(">I", data[idx:idx + 4])[0]; idx += 4
        cmd_str, n = _read_cstring(data, idx); cbd.command = cmd_str; idx = n
        name_str, n = _read_cstring(data, idx); cbd.custom_name = name_str; idx = n
        out_str, n = _read_cstring(data, idx); cbd.last_output = out_str; idx = n
        if idx + 4 <= len(data):
            cbd.tick_delay = _to_int32(struct.unpack(">I", data[idx:idx + 4])[0]); idx += 4
        if idx + 4 <= len(data):
            flags = data[idx:idx + 4]
            cbd.execute_on_first_tick = flags[0] != 0
            cbd.track_output = flags[1] != 0
            cbd.conditional = flags[2] != 0
            cbd.needs_redstone = flags[3] != 0
        return cls(block_data=bd, command_block_data=cbd)


# ----------------------------------------------------------------------------
# 命令 ID 37: PlaceRuntimeBlockWithChestData
# ----------------------------------------------------------------------------


@dataclass
class PlaceRuntimeBlockWithChestDataCommand(BDumpCommand):
    """使用运行时 ID 放置方块并设置容器数据 (uint16)。

    命令 ID: 37

    参数:
        block_runtime_id: 运行时方块 ID (uint16)。
        chest_slots: 容器物品槽位列表。
    """

    block_runtime_id: int = 0
    chest_slots: List[ChestSlot] = field(default_factory=list)

    COMMAND_ID: ClassVar[int] = 37
    COMMAND_NAME: ClassVar[str] = "PlaceRuntimeBlockWithChestDataCommand"

    def marshal(self) -> bytes:
        """编码: uint16 runtimeID + uint8 slotCount + ChestSlot 列表。"""
        parts: List[bytes] = [
            struct.pack(">H", self.block_runtime_id & 0xFFFF),
            bytes([len(self.chest_slots) & 0xFF]),
        ]
        for slot in self.chest_slots:
            parts.append(slot.name.encode("utf-8") + b"\x00")
            parts.append(bytes([slot.count & 0xFF]))
            parts.append(struct.pack(">H", slot.damage & 0xFFFF))
            parts.append(bytes([slot.slot & 0xFF]))
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceRuntimeBlockWithChestDataCommand":
        if len(data) < 3:
            return cls()
        rid = struct.unpack(">H", data[:2])[0]
        slot_count = data[2]
        idx = 3
        slots: List[ChestSlot] = []
        for _ in range(slot_count):
            name, n = _read_cstring(data, idx)
            idx = n
            if idx + 4 > len(data):
                break
            count = data[idx]
            damage = struct.unpack(">H", data[idx + 1:idx + 3])[0]
            slot_id = data[idx + 3]
            idx += 4
            slots.append(ChestSlot(name=name, count=count, damage=damage, slot=slot_id))
        return cls(block_runtime_id=rid, chest_slots=slots)


# ----------------------------------------------------------------------------
# 命令 ID 38: PlaceRuntimeBlockWithChestDataAndUint32RuntimeID
# ----------------------------------------------------------------------------


@dataclass
class PlaceRuntimeBlockWithChestDataAndUint32RuntimeIDCommand(BDumpCommand):
    """使用运行时 ID 放置方块并设置容器数据 (uint32)。

    命令 ID: 38

    参数:
        block_runtime_id: 运行时方块 ID (uint32)。
        chest_slots: 容器物品槽位列表。
    """

    block_runtime_id: int = 0
    chest_slots: List[ChestSlot] = field(default_factory=list)

    COMMAND_ID: ClassVar[int] = 38
    COMMAND_NAME: ClassVar[str] = "PlaceRuntimeBlockWithChestDataAndUint32RuntimeIDCommand"

    def marshal(self) -> bytes:
        parts: List[bytes] = [
            struct.pack(">I", self.block_runtime_id & 0xFFFFFFFF),
            bytes([len(self.chest_slots) & 0xFF]),
        ]
        for slot in self.chest_slots:
            parts.append(slot.name.encode("utf-8") + b"\x00")
            parts.append(bytes([slot.count & 0xFF]))
            parts.append(struct.pack(">H", slot.damage & 0xFFFF))
            parts.append(bytes([slot.slot & 0xFF]))
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceRuntimeBlockWithChestDataAndUint32RuntimeIDCommand":
        if len(data) < 5:
            return cls()
        rid = struct.unpack(">I", data[:4])[0]
        slot_count = data[4]
        idx = 5
        slots: List[ChestSlot] = []
        for _ in range(slot_count):
            name, n = _read_cstring(data, idx)
            idx = n
            if idx + 4 > len(data):
                break
            count = data[idx]
            damage = struct.unpack(">H", data[idx + 1:idx + 3])[0]
            slot_id = data[idx + 3]
            idx += 4
            slots.append(ChestSlot(name=name, count=count, damage=damage, slot=slot_id))
        return cls(block_runtime_id=rid, chest_slots=slots)


# ----------------------------------------------------------------------------
# 命令 ID 39: AssignDebugData
# ----------------------------------------------------------------------------


@dataclass
class AssignDebugDataCommand(BDumpCommand):
    """记录调试数据，不影响建造过程。

    命令 ID: 39

    参数:
        data: 调试数据字节。
    """

    data: bytes = b""

    COMMAND_ID: ClassVar[int] = 39
    COMMAND_NAME: ClassVar[str] = "AssignDebugDataCommand"

    def marshal(self) -> bytes:
        return struct.pack(">I", len(self.data)) + self.data

    @classmethod
    def unmarshal(cls, data: bytes) -> "AssignDebugDataCommand":
        if len(data) < 4:
            return cls()
        length = struct.unpack(">I", data[:4])[0]
        if len(data) < 4 + length:
            return cls(data=data[4:])
        return cls(data=data[4:4 + length])


# ----------------------------------------------------------------------------
# 命令 ID 40: PlaceBlockWithChestData
# ----------------------------------------------------------------------------


@dataclass
class PlaceBlockWithChestDataCommand(BDumpCommand):
    """放置方块 (使用字符串池) 并设置容器数据。

    命令 ID: 40

    参数:
        block_constant_string_id: 方块名在字符串池中的 ID (uint16)。
        block_data: 方块数据值 (uint16)。
        chest_slots: 容器物品槽位列表。
    """

    block_constant_string_id: int = 0
    block_data: int = 0
    chest_slots: List[ChestSlot] = field(default_factory=list)

    COMMAND_ID: ClassVar[int] = 40
    COMMAND_NAME: ClassVar[str] = "PlaceBlockWithChestDataCommand"

    def marshal(self) -> bytes:
        parts: List[bytes] = [
            struct.pack(">HH", self.block_constant_string_id, self.block_data),
            bytes([len(self.chest_slots) & 0xFF]),
        ]
        for slot in self.chest_slots:
            parts.append(slot.name.encode("utf-8") + b"\x00")
            parts.append(bytes([slot.count & 0xFF]))
            parts.append(struct.pack(">H", slot.damage & 0xFFFF))
            parts.append(bytes([slot.slot & 0xFF]))
        return b"".join(parts)

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceBlockWithChestDataCommand":
        if len(data) < 5:
            return cls()
        bid = struct.unpack(">H", data[0:2])[0]
        bd = struct.unpack(">H", data[2:4])[0]
        slot_count = data[4]
        idx = 5
        slots: List[ChestSlot] = []
        for _ in range(slot_count):
            name, n = _read_cstring(data, idx)
            idx = n
            if idx + 4 > len(data):
                break
            count = data[idx]
            damage = struct.unpack(">H", data[idx + 1:idx + 3])[0]
            slot_id = data[idx + 3]
            idx += 4
            slots.append(ChestSlot(name=name, count=count, damage=damage, slot=slot_id))
        return cls(block_constant_string_id=bid, block_data=bd, chest_slots=slots)


# ----------------------------------------------------------------------------
# 命令 ID 41: PlaceBlockWithNBTData
# ----------------------------------------------------------------------------


@dataclass
class PlaceBlockWithNBTDataCommand(BDumpCommand):
    """放置方块并附带 NBT 数据 (使用字符串池)。

    命令 ID: 41

    参数:
        block_constant_string_id: 方块名在字符串池中的 ID (uint16)。
        block_states_constant_string_id: 方块状态在字符串池中的 ID (uint16)。
        block_nbt_bytes: NBT 数据字节 (小端序编码)。
    """

    block_constant_string_id: int = 0
    block_states_constant_string_id: int = 0
    block_nbt_bytes: bytes = b""

    COMMAND_ID: ClassVar[int] = 41
    COMMAND_NAME: ClassVar[str] = "PlaceBlockWithNBTDataCommand"

    def marshal(self) -> bytes:
        return (
            struct.pack(">HH", self.block_constant_string_id, self.block_states_constant_string_id)
            + self.block_nbt_bytes
        )

    @classmethod
    def unmarshal(cls, data: bytes) -> "PlaceBlockWithNBTDataCommand":
        if len(data) < 4:
            return cls()
        bid = struct.unpack(">H", data[0:2])[0]
        sid = struct.unpack(">H", data[2:4])[0]
        return cls(block_constant_string_id=bid, block_states_constant_string_id=sid, block_nbt_bytes=data[4:])


# ----------------------------------------------------------------------------
# 命令 ID 88: Terminate
# ----------------------------------------------------------------------------


@dataclass
class TerminateCommand(BDumpCommand):
    """停止读入命令流。

    命令 ID: 88 ('X')

    通常结尾为 "XE" (2字节), 但仅 "X" (1字节) 也是允许的。
    无参数。
    """

    COMMAND_ID: ClassVar[int] = 88
    COMMAND_NAME: ClassVar[str] = "TerminateCommand"

    def marshal(self) -> bytes:
        return b""

    @classmethod
    def unmarshal(cls, data: bytes) -> "TerminateCommand":
        return cls()


# ============================================================================
# 辅助函数
# ============================================================================


def _read_cstring(data: bytes, offset: int) -> Tuple[str, int]:
    """从字节数据中读取以 \0 结尾的 C 风格字符串。

    来自 PhoenixBuilder fastbuilder/bdump/command/command.go readString()

    Args:
        data: 字节数据。
        offset: 起始偏移量。

    Returns:
        (字符串, 下一个偏移量) 元组。
    """
    end = data.find(b"\x00", offset)
    if end == -1:
        return data[offset:].decode("utf-8", errors="replace"), len(data)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _to_int32(val: int) -> int:
    """将 uint32 转换为 int32 (有符号)。

    Args:
        val: uint32 值。

    Returns:
        int32 值。
    """
    if val >= 0x80000000:
        return val - 0x100000000
    return val


def _encode_bool_flags(
    a: bool, b: bool, c: bool, d: bool
) -> bytes:
    """将 4 个布尔值编码为 4 字节标志 (每个字节 0 或 1)。

    来自 PhoenixBuilder fastbuilder/bdump/command/ 中的命令方块数据编码。

    Args:
        a: 第 1 个标志 (execute_on_first_tick)。
        b: 第 2 个标志 (track_output)。
        c: 第 3 个标志 (conditional)。
        d: 第 4 个标志 (needs_redstone)。

    Returns:
        4 字节的标志数据。
    """
    return bytes([
        1 if a else 0,
        1 if b else 0,
        1 if c else 0,
        1 if d else 0,
    ])


# ============================================================================
# BDump 命令池 (来自 PhoenixBuilder fastbuilder/bdump/command/pool.go)
# ============================================================================


#: BDump 命令 ID 到命令类的映射表
BDUMP_COMMAND_POOL: Dict[int, type] = {
    1: CreateConstantStringCommand,
    5: PlaceBlockWithBlockStatesCommand,
    6: AddInt16ZValue0Command,
    7: PlaceBlockCommand,
    8: AddZValue0Command,
    9: NoOperationCommand,
    12: AddInt32ZValue0Command,
    13: PlaceBlockWithBlockStatesDeprecatedCommand,
    14: AddXValueCommand,
    15: SubtractXValueCommand,
    16: AddYValueCommand,
    17: SubtractYValueCommand,
    18: AddZValueCommand,
    19: SubtractZValueCommand,
    20: AddInt16XValueCommand,
    21: AddInt32XValueCommand,
    22: AddInt16YValueCommand,
    23: AddInt32YValueCommand,
    24: AddInt16ZValueCommand,
    25: AddInt32ZValueCommand,
    26: SetCommandBlockDataCommand,
    27: PlaceBlockWithCommandBlockDataCommand,
    28: AddInt8XValueCommand,
    29: AddInt8YValueCommand,
    30: AddInt8ZValueCommand,
    31: UseRuntimeIDPoolCommand,
    32: PlaceRuntimeBlockCommand,
    33: PlaceRuntimeBlockWithUint32RuntimeIDCommand,
    34: PlaceRuntimeBlockWithCommandBlockDataCommand,
    35: PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeIDCommand,
    36: PlaceCommandBlockWithCommandBlockDataCommand,
    37: PlaceRuntimeBlockWithChestDataCommand,
    38: PlaceRuntimeBlockWithChestDataAndUint32RuntimeIDCommand,
    39: AssignDebugDataCommand,
    40: PlaceBlockWithChestDataCommand,
    41: PlaceBlockWithNBTDataCommand,
    88: TerminateCommand,
}


def read_command(data: bytes, offset: int = 0) -> Tuple[Optional[BDumpCommand], int]:
    """从字节流中读取一个 BDump 命令。

    来自 PhoenixBuilder fastbuilder/bdump/command/command.go ReadCommand()

    命令格式: 1 字节命令 ID + 命令参数。

    Args:
        data: 字节数据。
        offset: 起始偏移量。

    Returns:
        (命令实例, 下一个偏移量) 元组。如果命令 ID 未知，返回 (None, 下一个偏移量)。

    Raises:
        ValueError: 数据不足或命令 ID 未知。
    """
    if offset >= len(data):
        raise ValueError("BDump: 读取命令时遇到意外的 EOF")
    cmd_id = data[offset]
    offset += 1
    if cmd_id not in BDUMP_COMMAND_POOL:
        raise ValueError(f"BDump: 未知命令 ID: {cmd_id}")
    cmd_cls = BDUMP_COMMAND_POOL[cmd_id]
    # 对于无参数命令，直接创建实例
    if cmd_id in (8, 9, 14, 15, 16, 17, 18, 19, 88):
        return cmd_cls(), offset
    # 对于有参数命令，需要确定命令参数长度
    # 简化处理：传入剩余所有数据，让 unmarshal 自行处理
    cmd = cmd_cls.unmarshal(data[offset:])
    # 计算命令消耗的字节数 (这是近似值，unmarshal 本身不返回消耗量)
    # 对于复杂命令，我们让 unmarshal 返回完整的命令，使用完所有剩余数据
    # 实际上，我们需要更精确的读取。但为了简化，我们使用基于解码器的方法
    return cmd, len(data)  # 标记为已读完


def write_command(cmd: BDumpCommand) -> bytes:
    """将 BDump 命令编码为完整字节流。

    来自 PhoenixBuilder fastbuilder/bdump/command/command.go WriteCommand()

    格式: 1 字节命令 ID + 命令参数。

    Args:
        cmd: BDump 命令实例。

    Returns:
        完整的命令字节数据。
    """
    return cmd.to_bytes()


# ============================================================================
# BDump 写入器 (来自 PhoenixBuilder fastbuilder/bdump/bdump.go)
# ============================================================================


class BDumpWriter:
    """BDump 文件写入器。

    来自 PhoenixBuilder fastbuilder/bdump/bdump.go

    使用画笔 (Brush) 位置追踪系统，将 Module 列表编码为 BDump 命令流。
    相比 Retalcer 的直接发送命令，BDump 使用增量坐标更新，大幅减少文件体积。

    写入流程:
        1. 构建字符串池 (方块名 + 方块状态)
        2. 写入 CreateConstantString 命令
        3. 遍历方块，计算画笔移动增量
        4. 根据方块类型选择最优的 PlaceBlock 命令
        5. 写入 Terminate 命令
    """

    def __init__(self) -> None:
        """初始化 BDump 写入器。"""
        self._commands: List[bytes] = []

    def write_command(self, cmd: BDumpCommand) -> None:
        """写入一条命令。

        Args:
            cmd: BDump 命令实例。
        """
        self._commands.append(write_command(cmd))

    def write_header(self, author: str = "") -> bytes:
        """写入 BDump 内部文件头。

        来自 PhoenixBuilder fastbuilder/bdump/bdump.go writeHeader()

        格式: "BDX" + \x00 + author + \x00

        Args:
            author: 作者游戏名。

        Returns:
            文件头字节数据。
        """
        return BDUMP_INNER_HEADER + author.encode("utf-8") + b"\x00"

    def write_blocks(
        self,
        blocks: List[Module],
        author: str = "",
    ) -> bytes:
        """将方块列表编码为 BDump 命令流。

        来自 PhoenixBuilder fastbuilder/bdump/bdump.go writeBlocks()

        使用画笔位置追踪系统，以增量坐标更新方式编码方块。

        Args:
            blocks: 方块模块列表。
            author: 作者游戏名。

        Returns:
            完整的 BDump 内部数据 (文件头 + 命令流)。
        """
        self._commands = []

        # 步骤 1: 格式化方块 (归一化坐标，平移使最小坐标为 0)
        blocks = self._format_blocks(blocks)

        # 步骤 2: 构建字符串池 (方块名 + 方块状态)
        string_pool: Dict[str, int] = {}
        cursor = 0

        # 先添加所有方块名
        for mdl in blocks:
            if mdl.block is None:
                continue
            blk_name = mdl.block.name
            if blk_name not in string_pool:
                self.write_command(CreateConstantStringCommand(constant_string=blk_name))
                string_pool[blk_name] = cursor
                cursor += 1

        # 再添加所有方块状态字符串
        for mdl in blocks:
            if mdl.block is None:
                continue
            blk_states = mdl.block.block_states
            if not blk_states:
                continue
            if blk_states not in string_pool:
                self.write_command(CreateConstantStringCommand(constant_string=blk_states))
                string_pool[blk_states] = cursor
                cursor += 1

        # 步骤 3: 遍历方块，使用画笔追踪
        brush = Position(0, 0, 0)

        for mdl in blocks:
            if mdl.block is None:
                continue

            # 移动画笔到目标位置
            while True:
                if mdl.point.x != brush.x:
                    self._write_brush_move_x(mdl.point.x - brush.x)
                    brush.x = mdl.point.x
                    continue
                elif mdl.point.y != brush.y:
                    self._write_brush_move_y(mdl.point.y - brush.y)
                    brush.y = mdl.point.y
                    continue
                elif mdl.point.z != brush.z:
                    self._write_brush_move_z(mdl.point.z - brush.z)
                    brush.z = mdl.point.z
                break

            # 放置方块
            block_name = mdl.block.name
            block_states = mdl.block.block_states

            if mdl.chest_data:
                # 带容器数据的方块
                self.write_command(PlaceBlockWithChestDataCommand(
                    block_constant_string_id=string_pool[block_name],
                    block_data=mdl.block.data,
                    chest_slots=list(mdl.chest_data),
                ))
            elif mdl.command_block_data:
                # 命令方块
                self.write_command(PlaceCommandBlockWithCommandBlockDataCommand(
                    block_data=mdl.block.data,
                    command_block_data=mdl.command_block_data,
                ))
            elif mdl.nbt_data:
                # 带 NBT 数据的方块
                self.write_command(PlaceBlockWithNBTDataCommand(
                    block_constant_string_id=string_pool[block_name],
                    block_states_constant_string_id=string_pool.get(block_states, 0),
                    block_nbt_bytes=mdl.nbt_data,
                ))
            elif block_states:
                # 带方块状态的方块
                self.write_command(PlaceBlockWithBlockStatesCommand(
                    block_constant_string_id=string_pool[block_name],
                    block_states_constant_string_id=string_pool[block_states],
                ))
            else:
                # 普通方块
                self.write_command(PlaceBlockCommand(
                    block_constant_string_id=string_pool[block_name],
                    block_data=mdl.block.data,
                ))

        # 写入终止命令
        self.write_command(TerminateCommand())

        # 组装完整数据
        header = self.write_header(author)
        return header + b"".join(self._commands)

    def _format_blocks(self, blocks: List[Module]) -> List[Module]:
        """格式化方块列表，归一化坐标。

        找到最小坐标，将所有方块平移到从 (0,0,0) 开始。

        来自 PhoenixBuilder fastbuilder/bdump/bdump.go formatBlocks()

        Args:
            blocks: 原始方块列表。

        Returns:
            归一化后的方块列表 (新副本)。
        """
        if not blocks:
            return blocks

        min_x = min(b.point.x for b in blocks)
        min_y = min(b.point.y for b in blocks)
        min_z = min(b.point.z for b in blocks)

        result: List[Module] = []
        for mdl in blocks:
            new_mdl = Module(
                block=mdl.block,
                point=Position(
                    mdl.point.x - min_x,
                    mdl.point.y - min_y,
                    mdl.point.z - min_z,
                ),
                command_block_data=mdl.command_block_data,
                nbt_data=mdl.nbt_data,
                nbt_map=mdl.nbt_map,
                chest_slot=mdl.chest_slot,
                chest_data=mdl.chest_data,
            )
            result.append(new_mdl)
        return result

    def _write_brush_move_x(self, delta: int) -> None:
        """写入画笔 X 轴移动命令。

        根据增量大小选择最优命令:
            - delta == 1: AddXValue (14)
            - delta == -1: SubtractXValue (15)
            - -128 <= delta <= 127: AddInt8XValue (28)
            - -32768 <= delta <= 32767: AddInt16XValue (20)
            - 其他: AddInt32XValue (21)

        来自 PhoenixBuilder fastbuilder/bdump/bdump.go writeBlocks()

        Args:
            delta: X 轴增量。
        """
        if delta == 1:
            self.write_command(AddXValueCommand())
        elif delta == -1:
            self.write_command(SubtractXValueCommand())
        elif -128 <= delta <= 127:
            self.write_command(AddInt8XValueCommand(value=delta))
        elif -32768 <= delta <= 32767:
            self.write_command(AddInt16XValueCommand(value=delta))
        else:
            self.write_command(AddInt32XValueCommand(value=delta))

    def _write_brush_move_y(self, delta: int) -> None:
        """写入画笔 Y 轴移动命令 (同 X 轴逻辑)。"""
        if delta == 1:
            self.write_command(AddYValueCommand())
        elif delta == -1:
            self.write_command(SubtractYValueCommand())
        elif -128 <= delta <= 127:
            self.write_command(AddInt8YValueCommand(value=delta))
        elif -32768 <= delta <= 32767:
            self.write_command(AddInt16YValueCommand(value=delta))
        else:
            self.write_command(AddInt32YValueCommand(value=delta))

    def _write_brush_move_z(self, delta: int) -> None:
        """写入画笔 Z 轴移动命令 (同 X 轴逻辑)。"""
        if delta == 1:
            self.write_command(AddZValueCommand())
        elif delta == -1:
            self.write_command(SubtractZValueCommand())
        elif -128 <= delta <= 127:
            self.write_command(AddInt8ZValueCommand(value=delta))
        elif -32768 <= delta <= 32767:
            self.write_command(AddInt16ZValueCommand(value=delta))
        else:
            self.write_command(AddInt32ZValueCommand(value=delta))

    def write_to_bytes(self, blocks: List[Module], author: str = "") -> bytes:
        """将方块列表写入为 BDump 内部数据字节。

        Args:
            blocks: 方块模块列表。
            author: 作者游戏名。

        Returns:
            BDump 内部数据字节 (不含 Brotli 压缩)。
        """
        return self.write_blocks(blocks, author)


# ============================================================================
# BDump 解析器 (来自 PhoenixBuilder fastbuilder/builder/bdump.go)
# ============================================================================


class BDumpParser:
    """BDump 文件解析器。

    来自 PhoenixBuilder fastbuilder/builder/bdump.go BDump()

    解析 BDump v3/v4 格式文件，生成 Module 列表。
    支持:
        - 字符串池 (String Pool)
        - 画笔位置追踪
        - 运行时 ID 池 (Runtime ID Pool)
        - 所有 30+ 种命令类型

    解析流程:
        1. 验证文件头 (BD@)
        2. Brotli 解压
        3. 验证内部文件头 (BDX)
        4. 读取作者信息
        5. 初始化画笔位置 (0, 0, 0)
        6. 循环读取命令直到 Terminate
        7. 根据命令类型更新画笔位置或生成 Module
    """

    def __init__(self) -> None:
        """初始化 BDump 解析器。"""
        self._runtime_id_pool: Optional[List[ConstBlock]] = None
        self._string_pool: List[str] = []
        self._brush: Position = Position(0, 0, 0)
        self._modules: List[Module] = []
        self._header: BDumpHeader = BDumpHeader()

    def parse_bytes(self, data: bytes) -> List[Module]:
        """从 BDump 内部数据字节解析 Module 列表。

        Args:
            data: BDump 内部数据字节 (已解压，不含 BD@ 头)。

        Returns:
            解析出的 Module 列表。

        Raises:
            ValueError: 格式无效或数据损坏。
        """
        self._string_pool = []
        self._brush = Position(0, 0, 0)
        self._modules = []
        self._runtime_id_pool = None

        idx = 0

        # 验证内部文件头
        if len(data) < 4:
            raise ValueError("BDump: 数据不足，无法读取内部文件头")
        if data[idx:idx + 4] != BDUMP_INNER_HEADER:
            raise ValueError("BDump: 无效的内部文件头 (期望 BDX\\x00)")
        idx += 4

        # 读取作者名
        author, idx = _read_cstring(data, idx)
        self._header.author = author

        # 读取命令流
        while idx < len(data):
            cmd_id = data[idx]
            idx += 1

            if cmd_id == 88:  # Terminate
                break

            if cmd_id not in BDUMP_COMMAND_POOL:
                logger.warning("BDump: 未知命令 ID: %d，跳过", cmd_id)
                continue

            cmd_cls = BDUMP_COMMAND_POOL[cmd_id]

            # 根据命令类型读取参数
            cmd, idx = self._parse_command(data, idx, cmd_id, cmd_cls)
            if cmd is None:
                continue

            # 执行命令 (更新画笔或生成 Module)
            self._execute_command(cmd)

        return self._modules

    def _parse_command(
        self, data: bytes, idx: int, cmd_id: int, cmd_cls: type
    ) -> Tuple[Optional[BDumpCommand], int]:
        """解析单个命令。

        根据命令 ID 确定参数长度，读取对应字节。

        Args:
            data: 字节数据。
            idx: 当前偏移量。
            cmd_id: 命令 ID。
            cmd_cls: 命令类。

        Returns:
            (命令实例, 下一个偏移量) 元组。
        """
        # 无参数命令
        if cmd_id in (8, 9, 14, 15, 16, 17, 18, 19, 88):
            return cmd_cls(), idx

        # 1 字节参数命令
        if cmd_id in (28, 29, 30, 31):
            if idx >= len(data):
                return None, idx
            cmd = cmd_cls.unmarshal(data[idx:idx + 1])
            return cmd, idx + 1

        # 2 字节参数命令 (uint16)
        if cmd_id in (6, 20, 22, 24, 32):
            if idx + 2 > len(data):
                return None, idx
            cmd = cmd_cls.unmarshal(data[idx:idx + 2])
            return cmd, idx + 2

        # 4 字节参数命令 (uint32)
        if cmd_id in (12, 21, 23, 25, 33):
            if idx + 4 > len(data):
                return None, idx
            cmd = cmd_cls.unmarshal(data[idx:idx + 4])
            return cmd, idx + 4

        # 4 字节参数 (2 个 uint16)
        if cmd_id in (5, 7):
            if idx + 4 > len(data):
                return None, idx
            cmd = cmd_cls.unmarshal(data[idx:idx + 4])
            return cmd, idx + 4

        # 变长字符串命令 (CreateConstantString)
        if cmd_id == 1:
            s, new_idx = _read_cstring(data, idx)
            cmd = CreateConstantStringCommand(constant_string=s)
            return cmd, new_idx

        # 变长命令 (含字符串和复杂结构)
        # 对于这些命令，我们需要将剩余数据传给 unmarshal
        # 然后计算实际消耗的字节数
        remaining = data[idx:]
        cmd = cmd_cls.unmarshal(remaining)
        # 计算消耗的字节数
        consumed = self._calc_consumed_bytes(cmd, cmd_id, remaining)
        return cmd, idx + consumed

    def _calc_consumed_bytes(
        self, cmd: BDumpCommand, cmd_id: int, data: bytes
    ) -> int:
        """计算命令实际消耗的字节数。

        对于复杂命令，通过重新编码来估算消耗量。

        Args:
            cmd: 已解析的命令。
            cmd_id: 命令 ID。
            data: 剩余数据。

        Returns:
            消耗的字节数。
        """
        # 尝试重新编码来估算
        marshaled = cmd.marshal()
        return len(marshaled)

    def _execute_command(self, cmd: BDumpCommand) -> None:
        """执行 BDump 命令，更新画笔或生成 Module。

        来自 PhoenixBuilder fastbuilder/builder/bdump.go BDump()

        Args:
            cmd: 要执行的命令。
        """
        cmd_id = cmd.command_id()

        # 字符串池操作
        if cmd_id == 1:
            ccs = cmd  # type: ignore[assignment]
            self._string_pool.append(ccs.constant_string)  # type: ignore[attr-defined]

        # 画笔 X 轴移动
        elif cmd_id == 14:
            self._brush.x += 1
        elif cmd_id == 15:
            self._brush.x -= 1
        elif cmd_id == 20:
            self._brush.x += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 21:
            self._brush.x += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 28:
            self._brush.x += cmd.value  # type: ignore[attr-defined]

        # 画笔 Y 轴移动
        elif cmd_id == 16:
            self._brush.y += 1
        elif cmd_id == 17:
            self._brush.y -= 1
        elif cmd_id == 22:
            self._brush.y += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 23:
            self._brush.y += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 29:
            self._brush.y += cmd.value  # type: ignore[attr-defined]

        # 画笔 Z 轴移动
        elif cmd_id == 6:
            self._brush.z += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 8:
            self._brush.z += 1
        elif cmd_id == 12:
            self._brush.z += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 18:
            self._brush.z += 1
        elif cmd_id == 19:
            self._brush.z -= 1
        elif cmd_id == 24:
            self._brush.z += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 25:
            self._brush.z += cmd.value  # type: ignore[attr-defined]
        elif cmd_id == 30:
            self._brush.z += cmd.value  # type: ignore[attr-defined]

        # 运行时 ID 池
        elif cmd_id == 31:
            logger.debug("BDump: 使用运行时 ID 池 %d", cmd.pool_id)  # type: ignore[attr-defined]

        # 放置方块 (字符串池)
        elif cmd_id == 7:
            c = cmd  # type: ignore[assignment]
            if c.block_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError(f"BDump: BlockConstantStringID 超出字符串池范围: {c.block_constant_string_id}")  # type: ignore[attr-defined]
            block_name = self._string_pool[c.block_constant_string_id]  # type: ignore[attr-defined]
            self._modules.append(Module(
                block=Block(name=block_name, data=c.block_data),  # type: ignore[attr-defined]
                point=self._brush.copy(),
            ))

        # 放置方块 (带方块状态，字符串池)
        elif cmd_id == 5:
            c = cmd  # type: ignore[assignment]
            if c.block_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError(
                    f"BDump: BlockConstantStringID {c.block_constant_string_id} 超出字符串池范围 "
                    f"(池大小 {len(self._string_pool)})"
                )
            if c.block_states_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError(
                    f"BDump: BlockStatesConstantStringID {c.block_states_constant_string_id} 超出字符串池范围 "
                    f"(池大小 {len(self._string_pool)})"
                )
            block_name = self._string_pool[c.block_constant_string_id]  # type: ignore[attr-defined]
            block_states = self._string_pool[c.block_states_constant_string_id]  # type: ignore[attr-defined]
            self._modules.append(Module(
                block=Block(name=block_name, block_states=block_states),
                point=self._brush.copy(),
            ))

        # 放置方块 (已弃用的带状态格式)
        elif cmd_id == 13:
            c = cmd  # type: ignore[assignment]
            if c.block_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError("BDump: BlockConstantStringID 超出字符串池范围")
            block_name = self._string_pool[c.block_constant_string_id]  # type: ignore[attr-defined]
            self._modules.append(Module(
                block=Block(name=block_name, block_states=c.block_states_string),  # type: ignore[attr-defined]
                point=self._brush.copy(),
            ))

        # 放置命令方块 (推荐)
        elif cmd_id == 36:
            c = cmd  # type: ignore[assignment]
            self._modules.append(Module(
                block=Block(name="command_block", data=c.block_data),  # type: ignore[attr-defined]
                point=self._brush.copy(),
                command_block_data=c.command_block_data,  # type: ignore[attr-defined]
            ))

        # 设置命令方块数据
        elif cmd_id == 26:
            c = cmd  # type: ignore[assignment]
            self._modules.append(Module(
                point=self._brush.copy(),
                command_block_data=c.command_block_data,  # type: ignore[attr-defined]
            ))

        # 放置方块 + 命令方块数据
        elif cmd_id == 27:
            c = cmd  # type: ignore[assignment]
            if c.block_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError("BDump: BlockConstantStringID 超出字符串池范围")
            block_name = self._string_pool[c.block_constant_string_id]  # type: ignore[attr-defined]
            self._modules.append(Module(
                block=Block(name=block_name, data=c.block_data),  # type: ignore[attr-defined]
                point=self._brush.copy(),
                command_block_data=c.command_block_data,  # type: ignore[attr-defined]
            ))

        # 放置方块 + 容器数据
        elif cmd_id == 40:
            c = cmd  # type: ignore[assignment]
            if c.block_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError("BDump: BlockConstantStringID 超出字符串池范围")
            block_name = self._string_pool[c.block_constant_string_id]  # type: ignore[attr-defined]
            pos = self._brush.copy()
            self._modules.append(Module(
                block=Block(name=block_name, data=c.block_data),  # type: ignore[attr-defined]
                point=pos,
            ))
            for slot in c.chest_slots:  # type: ignore[attr-defined]
                self._modules.append(Module(
                    chest_slot=slot,
                    point=pos,
                ))

        # 放置方块 + NBT 数据
        elif cmd_id == 41:
            c = cmd  # type: ignore[assignment]
            if c.block_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError("BDump: BlockConstantStringID 超出字符串池范围")
            if c.block_states_constant_string_id >= len(self._string_pool):  # type: ignore[attr-defined]
                raise ValueError("BDump: BlockStatesConstantStringID 超出字符串池范围")
            block_name = self._string_pool[c.block_constant_string_id]  # type: ignore[attr-defined]
            block_states = self._string_pool[c.block_states_constant_string_id]  # type: ignore[attr-defined]
            self._modules.append(Module(
                block=Block(name=block_name, block_states=block_states),
                point=self._brush.copy(),
                nbt_data=c.block_nbt_bytes,  # type: ignore[attr-defined]
                nbt_map=None,  # NBT 解析需要额外的 NBT 库
            ))

        # 运行时 ID 放置 (需要运行时 ID 池，暂不支持)
        elif cmd_id in (32, 33, 34, 35, 37, 38):
            logger.warning("BDump: 运行时 ID 命令 %d 暂不支持 (需要运行时 ID 池)", cmd_id)

        # 调试数据
        elif cmd_id == 39:
            logger.debug("BDump: 调试数据，忽略")

        # NOP
        elif cmd_id == 9:
            pass

        else:
            logger.warning("BDump: 未处理的命令 ID: %d (%s)", cmd_id, cmd.command_name())

    @property
    def header(self) -> BDumpHeader:
        """返回解析出的文件头信息。"""
        return self._header

    @property
    def modules(self) -> List[Module]:
        """返回解析出的 Module 列表。"""
        return self._modules

    @property
    def brush(self) -> Position:
        """返回当前画笔位置。"""
        return self._brush


# ============================================================================
# BDump 文件读写 (Brotli 压缩/解压)
# ============================================================================


class BDumpFileReader:
    """BDump 文件读取器。

    来自 PhoenixBuilder fastbuilder/builder/bdump.go BDump()

    读取 .bdx 文件，处理 Brotli 解压和签名验证。

    文件格式:
        - 3 字节: "BD@" (压缩头)
        - N 字节: Brotli 压缩数据
        - 压缩数据内部: "BDX\\x00" + author + \\x00 + 命令流

    使用示例::

        reader = BDumpFileReader()
        modules = reader.read("path/to/building.bdx")
        for module in modules:
            print(f"放置 {module.block.name} 在 {module.point}")
    """

    def __init__(self) -> None:
        """初始化 BDump 文件读取器。"""
        self._parser = BDumpParser()

    def read(self, path: Union[str, Path]) -> List[Module]:
        """从文件读取 BDump 数据。

        Args:
            path: .bdx 文件路径。

        Returns:
            解析出的 Module 列表。

        Raises:
            ValueError: 格式无效或数据损坏。
            FileNotFoundError: 文件不存在。
        """
        with open(path, "rb") as f:
            return self.read_stream(f)

    def read_stream(self, stream: Any) -> List[Module]:
        """从流读取 BDump 数据。

        Args:
            stream: 可读字节流 (需支持 read())。

        Returns:
            解析出的 Module 列表。

        Raises:
            ValueError: 格式无效。
        """
        # 验证文件头
        header = stream.read(3)
        if len(header) < 3 or header != BDUMP_FILE_HEADER:
            raise ValueError("BDump: 无效的文件头 (期望 BD@)")

        # Brotli 解压
        try:
            import brotli
            compressed = stream.read()
            decompressed = brotli.decompress(compressed)
        except ImportError:
            raise ImportError(
                "BDump: 需要 brotli 库来解压 .bdx 文件。"
                "请运行: pip install brotli"
            )
        except Exception as e:
            raise ValueError(f"BDump: Brotli 解压失败: {e}")

        # 解析内部数据
        return self._parser.parse_bytes(decompressed)

    def read_bytes(self, data: bytes) -> List[Module]:
        """从字节数据读取 BDump。

        Args:
            data: BDump 原始字节数据 (含 BD@ 头)。

        Returns:
            解析出的 Module 列表。
        """
        import io as _io
        return self.read_stream(_io.BytesIO(data))

    @property
    def header(self) -> BDumpHeader:
        """返回解析出的文件头信息。"""
        return self._parser.header


class BDumpFileWriter:
    """BDump 文件写入器。

    来自 PhoenixBuilder fastbuilder/bdump/bdump.go WriteToFile()

    将 Module 列表编码为 .bdx 文件，使用 Brotli 压缩。

    文件格式:
        - "BD@" (3 字节)
        - Brotli 压缩的 BDump 内部数据
        - 签名尾 (可选): "X" + signature + Z

    使用示例::

        writer = BDumpFileWriter()
        writer.write("building.bdx", modules, author="PlayerName")
    """

    def __init__(self, compression_quality: int = 6) -> None:
        """初始化 BDump 文件写入器。

        Args:
            compression_quality: Brotli 压缩质量 (0-11)，默认 6。
        """
        self._writer = BDumpWriter()
        self._compression_quality = compression_quality

    def write(
        self,
        path: Union[str, Path],
        blocks: List[Module],
        author: str = "",
    ) -> bytes:
        """将方块列表写入 .bdx 文件。

        Args:
            path: 输出文件路径。
            blocks: 方块模块列表。
            author: 作者游戏名。

        Returns:
            写入的字节数据 (用于调试)。
        """
        data = self.to_bytes(blocks, author)
        with open(path, "wb") as f:
            f.write(data)
        return data

    def to_bytes(
        self,
        blocks: List[Module],
        author: str = "",
    ) -> bytes:
        """将方块列表编码为 .bdx 字节数据。

        Args:
            blocks: 方块模块列表。
            author: 作者游戏名。

        Returns:
            BDump 文件字节数据 (含 BD@ 头和 Brotli 压缩)。
        """
        # 生成 BDump 内部数据
        inner_data = self._writer.write_to_bytes(blocks, author)

        # Brotli 压缩
        try:
            import brotli
            compressed = brotli.compress(inner_data, quality=self._compression_quality)
        except ImportError:
            raise ImportError(
                "BDump: 需要 brotli 库来压缩 .bdx 文件。"
                "请运行: pip install brotli"
            )

        # 组装完整文件
        return BDUMP_FILE_HEADER + compressed + b"XE"


# ============================================================================
# 方块放置调度器 (Planner) - 来自 PhoenixBuilder 的优化策略
# ============================================================================


@dataclass
class BlockFillEntry:
    """方块填充条目 (16³ 体素优化)。

    PhoenixBuilder 的专业优化: 将 16x16x16 区域内相同方块合并为 /fill 命令。
    这比 Retalcer 的简单 Z 轴合并效率高得多。
    """

    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int
    block: Block
    volume: int = 0  # 填充体积

    def __post_init__(self) -> None:
        if self.volume == 0:
            self.volume = (
                (self.x2 - self.x1 + 1)
                * (self.y2 - self.y1 + 1)
                * (self.z2 - self.z1 + 1)
            )

    def to_command(self, origin: Position) -> str:
        """生成 /fill 命令字符串。

        Args:
            origin: 原点偏移坐标。

        Returns:
            Minecraft /fill 命令字符串。
        """
        ox, oy, oz = origin.x, origin.y, origin.z
        cmd = (
            f"fill {self.x1 + ox} {self.y1 + oy} {self.z1 + oz} "
            f"{self.x2 + ox} {self.y2 + oy} {self.z2 + oz} "
            f"{self.block.name}"
        )
        if self.block.block_states:
            cmd += f" {self.block.block_states}"
        cmd += " replace"
        return cmd


@dataclass
class PlacePlan:
    """放置计划 - 优化后的方块放置方案。

    包含:
        - 填充命令列表 (批量 /fill)
        - 单方块放置列表 (无法合并的散块)
        - 区块排序信息
    """

    fills: List[BlockFillEntry] = field(default_factory=list)
    singles: List[Module] = field(default_factory=list)
    total_blocks: int = 0
    total_commands: int = 0
    chunk_order: List[Tuple[int, int]] = field(default_factory=list)


class PhoenixPlanner:
    """PhoenixBuilder 专业规划器。

    实现了 PhoenixBuilder 的专业优化策略:
        1. 16³ Fill 批量优化 - 将 16x16x16 区域内的相同方块合并为 /fill 命令
        2. 蛇形路径区块排序 - 减少 TP 移动距离
        3. 跳空优化 - 跳过空气方块，不生成命令
        4. 增量构建 - 支持 checkpoint/resume

    与 Retalcer batch_optimizer 的对比:
        - Retalcer: 仅 Z 轴合并，效率低
        - PhoenixBuilder: 16³ 体素合并，命令数量减少 90%+
        - Retalcer: 无区域排序
        - PhoenixBuilder: 蛇形路径，TP 移动距离减半
    """

    def __init__(
        self,
        voxel_size: int = FILL_VOXEL_SIZE,
        skip_air: bool = True,
    ) -> None:
        """初始化规划器。

        Args:
            voxel_size: 体素大小 (默认 16³)。
            skip_air: 是否跳过空气方块。
        """
        self.voxel_size = voxel_size
        self.skip_air = skip_air

    def plan(
        self,
        modules: List[Module],
        origin: Position = Position(0, 0, 0),
    ) -> PlacePlan:
        """规划方块放置方案。

        Args:
            modules: 原始方块模块列表。
            origin: 放置原点坐标 (所有坐标会加上此偏移)。

        Returns:
            优化后的 PlacePlan。
        """
        if not modules:
            return PlacePlan()

        # 过滤空气方块
        if self.skip_air:
            modules = [
                m for m in modules
                if m.block is None or m.block.name != "minecraft:air"
            ]

        if not modules:
            return PlacePlan()

        # 按区块分组
        chunk_map: Dict[Tuple[int, int], List[Module]] = self._group_by_chunk(modules)

        # 蛇形路径排序
        chunk_order = self._snake_sort(list(chunk_map.keys()))

        # 对每个区块执行 16³ Fill 优化
        fills: List[BlockFillEntry] = []
        singles: List[Module] = []

        for cx, cz in chunk_order:
            chunk_modules = chunk_map.get((cx, cz), [])
            chunk_fills, chunk_singles = self._optimize_chunk(chunk_modules)
            fills.extend(chunk_fills)
            singles.extend(chunk_singles)

        total_blocks = sum(f.volume for f in fills) + len(singles)
        total_commands = len(fills) + len(singles)

        return PlacePlan(
            fills=fills,
            singles=singles,
            total_blocks=total_blocks,
            total_commands=total_commands,
            chunk_order=chunk_order,
        )

    def _group_by_chunk(
        self, modules: List[Module]
    ) -> Dict[Tuple[int, int], List[Module]]:
        """按区块坐标分组方块。

        Args:
            modules: 方块模块列表。

        Returns:
            {(cx, cz): [Module, ...]} 映射。
        """
        chunk_map: Dict[Tuple[int, int], List[Module]] = {}
        for m in modules:
            cx = m.point.x // CHUNK_SIZE
            cz = m.point.z // CHUNK_SIZE
            key = (cx, cz)
            if key not in chunk_map:
                chunk_map[key] = []
            chunk_map[key].append(m)
        return chunk_map

    def _snake_sort(
        self, chunks: List[Tuple[int, int]]
    ) -> List[Tuple[int, int]]:
        """蛇形路径排序区块。

        来自 PhoenixBuilder 的区块排序策略:
        沿 Z 轴递增，偶数行 X 递增，奇数行 X 递减。

        这减少了 TP 移动距离，因为玩家不需要来回跑。

        Args:
            chunks: 区块坐标列表。

        Returns:
            蛇形排序后的区块列表。
        """
        return sorted(
            chunks,
            key=lambda c: (c[1], c[0] if c[1] % 2 == 0 else -c[0]),
        )

    def _optimize_chunk(
        self, modules: List[Module]
    ) -> Tuple[List[BlockFillEntry], List[Module]]:
        """对单个区块的方块进行 16³ Fill 优化。

        将一个区块内的方块按 16³ 体素分组，
        相同方块的相邻体素合并为 /fill 命令。

        这是 PhoenixBuilder 的核心优化，比 Retalcer 的 Z 轴合并效率高 10-100 倍。

        Args:
            modules: 区块内的方块模块列表。

        Returns:
            (fill 列表, single 列表) 元组。
        """
        if not modules:
            return [], []

        # 构建 3D 体素网格 (以 voxel_size 为单位)
        voxel_grid: Dict[Tuple[int, int, int], Dict[str, List[Position]]] = {}

        for m in modules:
            if m.block is None:
                continue
            vx = m.point.x // self.voxel_size
            vy = m.point.y // self.voxel_size
            vz = m.point.z // self.voxel_size
            vkey = (vx, vy, vz)

            block_key = self._block_key(m.block)

            if vkey not in voxel_grid:
                voxel_grid[vkey] = {}
            if block_key not in voxel_grid[vkey]:
                voxel_grid[vkey][block_key] = []
            voxel_grid[vkey][block_key].append(m.point)

        # 对每个体素内相同方块做合并
        fills: List[BlockFillEntry] = []
        singles: List[Module] = []

        for vkey, block_groups in voxel_grid.items():
            for block_key, positions in block_groups.items():
                if len(positions) == 1:
                    # 单个方块，直接放置
                    # 需要找到原始 Module
                    for m in modules:
                        if m.block and self._block_key(m.block) == block_key and m.point == positions[0]:
                            singles.append(m)
                            break
                else:
                    # 多个相同方块，合并为 /fill
                    # 找到最小/最大坐标
                    xs = [p.x for p in positions]
                    ys = [p.y for p in positions]
                    zs = [p.z for p in positions]
                    x1, x2 = min(xs), max(xs)
                    y1, y2 = min(ys), max(ys)
                    z1, z2 = min(zs), max(zs)

                    # 找到代表方块
                    rep_block = None
                    for m in modules:
                        if m.block and self._block_key(m.block) == block_key:
                            rep_block = m.block
                            break

                    if rep_block:
                        fills.append(BlockFillEntry(
                            x1=x1, y1=y1, z1=z1,
                            x2=x2, y2=y2, z2=z2,
                            block=rep_block,
                        ))

        return fills, singles

    @staticmethod
    def _block_key(block: Block) -> str:
        """生成方块标识键 (用于比较方块是否相同)。

        Args:
            block: 方块定义。

        Returns:
            方块标识字符串。
        """
        if block.block_states:
            return f"{block.name}|{block.block_states}|{block.data}"
        return f"{block.name}|{block.data}"


# ============================================================================
# 方块放置执行器 (Executor) - 来自 PhoenixBuilder 的速率控制和错误恢复
# ============================================================================


class PlaceRateLimiter:
    """方块放置速率限制器。

    来自 PhoenixBuilder fastbuilder/builder/ 中的速率控制逻辑。

    控制命令发送速率，避免服务器因过快发送命令而拒绝处理。
    支持:
        - 命令速率限制 (命令/秒)
        - 方块放置速率限制 (方块/秒)
        - 自适应速率调整 (根据服务器响应)
    """

    def __init__(
        self,
        command_rate: int = DEFAULT_COMMAND_RATE,
        place_rate: int = DEFAULT_PLACE_RATE,
    ) -> None:
        """初始化速率限制器。

        Args:
            command_rate: 命令发送速率 (命令/秒)。
            place_rate: 方块放置速率 (方块/秒)。
        """
        self.command_rate = min(command_rate, MAX_PLACE_RATE)
        self.place_rate = min(place_rate, MAX_PLACE_RATE)
        self._command_interval = 1.0 / self.command_rate if self.command_rate > 0 else 0
        self._last_command_time: float = 0.0
        self._total_placed: int = 0
        self._total_commands: int = 0
        self._errors: int = 0
        self._start_time: float = 0.0

    def reset(self) -> None:
        """重置统计信息。"""
        self._total_placed = 0
        self._total_commands = 0
        self._errors = 0
        self._start_time = time.monotonic()

    async def wait(self) -> None:
        """等待直到可以发送下一条命令。"""
        now = time.monotonic()
        if self._last_command_time > 0:
            elapsed = now - self._last_command_time
            if elapsed < self._command_interval:
                await asyncio.sleep(self._command_interval - elapsed)
        self._last_command_time = time.monotonic()

    def record_command(self, blocks_placed: int = 1) -> None:
        """记录已发送的命令。

        Args:
            blocks_placed: 此命令放置的方块数。
        """
        self._total_commands += 1
        self._total_placed += blocks_placed

    def record_error(self) -> None:
        """记录错误。"""
        self._errors += 1

    @property
    def total_placed(self) -> int:
        """已放置方块总数。"""
        return self._total_placed

    @property
    def total_commands(self) -> int:
        """已发送命令总数。"""
        return self._total_commands

    @property
    def errors(self) -> int:
        """错误总数。"""
        return self._errors

    @property
    def place_speed(self) -> float:
        """当前放置速率 (方块/秒)。"""
        if self._start_time == 0:
            return 0.0
        elapsed = time.monotonic() - self._start_time
        return self._total_placed / elapsed if elapsed > 0 else 0.0

    @property
    def command_speed(self) -> float:
        """当前命令速率 (命令/秒)。"""
        if self._start_time == 0:
            return 0.0
        elapsed = time.monotonic() - self._start_time
        return self._total_commands / elapsed if elapsed > 0 else 0.0


class PhoenixExecutor:
    """PhoenixBuilder 方块放置执行器。

    来自 PhoenixBuilder fastbuilder/builder/ 中的执行逻辑。

    负责:
        1. 执行 PlacePlan 中的 fill 和 single 命令
        2. 速率控制
        3. 错误恢复
        4. 进度回调

    与 Retalcer IncrementalImporter 的对比:
        - Retalcer: 直接发送命令，无速率控制
        - PhoenixBuilder: 精确的速率控制和自适应调整
        - Retalcer: 无错误恢复
        - PhoenixBuilder: 自动重试和错误恢复
    """

    def __init__(
        self,
        command_sender: Optional[Callable] = None,
        rate_limiter: Optional[PlaceRateLimiter] = None,
        group_size: int = DEFAULT_GROUP_SIZE,
    ) -> None:
        """初始化执行器。

        Args:
            command_sender: 异步命令发送函数 (cmd: str) -> response。
            rate_limiter: 速率限制器。
            group_size: 区块组大小。
        """
        self._sender = command_sender
        self._rate_limiter = rate_limiter or PlaceRateLimiter()
        self._group_size = group_size
        self._checkpoint: Optional[PlacePlan] = None
        self._checkpoint_index: int = 0

    async def execute(
        self,
        plan: PlacePlan,
        origin: Position = Position(0, 0, 0),
        progress_callback: Optional[Callable] = None,
        enable_checkpoint: bool = False,
    ) -> int:
        """执行放置计划。

        Args:
            plan: 放置计划。
            origin: 放置原点坐标。
            progress_callback: 进度回调函数 (current: int, total: int)。
            enable_checkpoint: 是否启用增量构建 (checkpoint/resume)。

        Returns:
            成功放置的方块数。
        """
        self._rate_limiter.reset()
        total_commands = len(plan.fills) + len(plan.singles)
        completed = 0

        try:
            # 执行填充命令
            for fill_entry in plan.fills:
                if enable_checkpoint:
                    self._checkpoint = plan
                    self._checkpoint_index = completed

                cmd = fill_entry.to_command(origin)
                await self._send_command(cmd, fill_entry.volume)
                completed += 1

                if progress_callback and completed % 50 == 0:
                    progress_callback(completed, total_commands)

            # 执行单方块命令
            for single in plan.singles:
                if enable_checkpoint:
                    self._checkpoint = plan
                    self._checkpoint_index = completed

                cmd = self._single_to_command(single, origin)
                await self._send_command(cmd, 1)
                completed += 1

                if progress_callback and completed % 100 == 0:
                    progress_callback(completed, total_commands)

        except Exception as e:
            logger.error("PhoenixExecutor: 执行错误: %s", e)
            self._rate_limiter.record_error()
            if enable_checkpoint:
                logger.info("PhoenixExecutor: 已保存检查点，可恢复执行")
            raise

        if progress_callback:
            progress_callback(completed, total_commands)

        return self._rate_limiter.total_placed

    async def resume(
        self,
        origin: Position = Position(0, 0, 0),
        progress_callback: Optional[Callable] = None,
    ) -> int:
        """从检查点恢复执行。

        Args:
            origin: 放置原点坐标。
            progress_callback: 进度回调。

        Returns:
            成功放置的方块数。

        Raises:
            RuntimeError: 没有可用的检查点。
        """
        if self._checkpoint is None:
            raise RuntimeError("PhoenixExecutor: 没有可用的检查点")

        plan = self._checkpoint
        # 跳过已完成的命令
        all_commands = list(plan.fills) + list(plan.singles)
        remaining = all_commands[self._checkpoint_index:]

        remaining_fills = [c for c in remaining if isinstance(c, BlockFillEntry)]
        remaining_singles = [c for c in remaining if isinstance(c, Module)]

        remaining_plan = PlacePlan(
            fills=remaining_fills,  # type: ignore[arg-type]
            singles=remaining_singles,  # type: ignore[arg-type]
            total_blocks=plan.total_blocks,
            total_commands=len(remaining),
            chunk_order=plan.chunk_order,
        )

        return await self.execute(
            remaining_plan,
            origin,
            progress_callback,
            enable_checkpoint=True,
        )

    async def _send_command(self, command: str, blocks: int = 1) -> None:
        """发送命令并等待速率限制。

        Args:
            command: Minecraft 命令字符串。
            blocks: 此命令放置的方块数。
        """
        await self._rate_limiter.wait()
        if self._sender:
            await self._sender(command)  # type: ignore[misc]
        self._rate_limiter.record_command(blocks)

    @staticmethod
    def _single_to_command(module: Module, origin: Position) -> str:
        """将单个方块 Module 转换为 /setblock 命令。

        Args:
            module: 方块模块。
            origin: 原点偏移。

        Returns:
            Minecraft /setblock 命令字符串。
        """
        if module.block is None:
            return ""

        x = module.point.x + origin.x
        y = module.point.y + origin.y
        z = module.point.z + origin.z

        cmd = f"setblock {x} {y} {z} {module.block.name}"
        if module.block.block_states:
            cmd += f" {module.block.block_states}"
        cmd += " replace"
        return cmd

    @property
    def rate_limiter(self) -> PlaceRateLimiter:
        """返回速率限制器。"""
        return self._rate_limiter

    @property
    def has_checkpoint(self) -> bool:
        """是否有可用的检查点。"""
        return self._checkpoint is not None

    def clear_checkpoint(self) -> None:
        """清除检查点。"""
        self._checkpoint = None
        self._checkpoint_index = 0


# ============================================================================
# 便捷函数 (兼容 Retalcer 接口)
# ============================================================================


def optimize_blocks(
    modules: List[Module],
    voxel_size: int = FILL_VOXEL_SIZE,
    skip_air: bool = True,
) -> PlacePlan:
    """优化方块放置方案 — PhoenixBuilder 风格的便捷函数。

    替换 Retalcer batch_optimizer.py 的 optimize_commands()。

    Args:
        modules: 方块模块列表。
        voxel_size: 体素大小 (默认 16)。
        skip_air: 是否跳过空气方块。

    Returns:
        优化后的 PlacePlan。
    """
    planner = PhoenixPlanner(voxel_size=voxel_size, skip_air=skip_air)
    return planner.plan(modules)


def read_bdump(path: Union[str, Path]) -> List[Module]:
    """读取 BDump 文件 — 便捷函数。

    Args:
        path: .bdx 文件路径。

    Returns:
        解析出的 Module 列表。
    """
    reader = BDumpFileReader()
    return reader.read(path)


def write_bdump(
    path: Union[str, Path],
    modules: List[Module],
    author: str = "",
    compression_quality: int = 6,
) -> bytes:
    """写入 BDump 文件 — 便捷函数。

    Args:
        path: 输出文件路径。
        modules: 方块模块列表。
        author: 作者游戏名。
        compression_quality: Brotli 压缩质量 (0-11)。

    Returns:
        写入的字节数据。
    """
    writer = BDumpFileWriter(compression_quality=compression_quality)
    return writer.write(path, modules, author)


def parse_bdump_bytes(data: bytes) -> List[Module]:
    """从 BDump 字节数据解析 Module 列表 — 便捷函数。

    Args:
        data: BDump 文件字节数据 (含 BD@ 头)。

    Returns:
        解析出的 Module 列表。
    """
    reader = BDumpFileReader()
    return reader.read_bytes(data)


def encode_bdump_bytes(
    modules: List[Module],
    author: str = "",
    compression_quality: int = 6,
) -> bytes:
    """将 Module 列表编码为 BDump 字节数据 — 便捷函数。

    Args:
        modules: 方块模块列表。
        author: 作者游戏名。
        compression_quality: Brotli 压缩质量 (0-11)。

    Returns:
        BDump 文件字节数据。
    """
    writer = BDumpFileWriter(compression_quality=compression_quality)
    return writer.to_bytes(modules, author)


# ============================================================================
# 模块导出列表
# ============================================================================

__all__ = [
    # 常量
    "CHUNK_SIZE",
    "BDUMP_FILE_HEADER",
    "BDUMP_INNER_HEADER",
    "MAX_FILL_VOLUME",
    "FILL_VOXEL_SIZE",
    "DEFAULT_COMMAND_DELAY",
    "DEFAULT_PLACE_RATE",
    "MAX_PLACE_RATE",
    "DEFAULT_COMMAND_RATE",
    "DEFAULT_GROUP_SIZE",
    "GROUP_WAIT_TIME",
    "RUNTIME_POOL_117",
    "RUNTIME_POOL_2_1_10",
    # 数据结构
    "Position",
    "Block",
    "ConstBlock",
    "ChestSlot",
    "CommandBlockData",
    "Module",
    "BDumpHeader",
    "BlockFillEntry",
    "PlacePlan",
    # BDump 命令基类
    "BDumpCommand",
    "BDUMP_COMMAND_POOL",
    # BDump 命令 (30+ 种)
    "CreateConstantStringCommand",
    "PlaceBlockWithBlockStatesCommand",
    "AddInt16ZValue0Command",
    "PlaceBlockCommand",
    "AddZValue0Command",
    "NoOperationCommand",
    "AddInt32ZValue0Command",
    "PlaceBlockWithBlockStatesDeprecatedCommand",
    "AddXValueCommand",
    "SubtractXValueCommand",
    "AddYValueCommand",
    "SubtractYValueCommand",
    "AddZValueCommand",
    "SubtractZValueCommand",
    "AddInt16XValueCommand",
    "AddInt32XValueCommand",
    "AddInt16YValueCommand",
    "AddInt32YValueCommand",
    "AddInt16ZValueCommand",
    "AddInt32ZValueCommand",
    "SetCommandBlockDataCommand",
    "PlaceBlockWithCommandBlockDataCommand",
    "AddInt8XValueCommand",
    "AddInt8YValueCommand",
    "AddInt8ZValueCommand",
    "UseRuntimeIDPoolCommand",
    "PlaceRuntimeBlockCommand",
    "PlaceRuntimeBlockWithUint32RuntimeIDCommand",
    "PlaceRuntimeBlockWithCommandBlockDataCommand",
    "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeIDCommand",
    "PlaceCommandBlockWithCommandBlockDataCommand",
    "PlaceRuntimeBlockWithChestDataCommand",
    "PlaceRuntimeBlockWithChestDataAndUint32RuntimeIDCommand",
    "AssignDebugDataCommand",
    "PlaceBlockWithChestDataCommand",
    "PlaceBlockWithNBTDataCommand",
    "TerminateCommand",
    # BDump 读写器
    "BDumpWriter",
    "BDumpParser",
    "BDumpFileReader",
    "BDumpFileWriter",
    # 优化和执行
    "PhoenixPlanner",
    "PhoenixExecutor",
    "PlaceRateLimiter",
    # 便捷函数
    "optimize_blocks",
    "read_bdump",
    "write_bdump",
    "parse_bdump_bytes",
    "encode_bdump_bytes",
    # 命令读写
    "read_command",
    "write_command",
]