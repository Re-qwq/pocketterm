"""bdump_commands - BDump 命令系统 (Yeah114 + merry-memory 双实现)。

逆向自 NexusEgo v1.6.5 的 BDump 命令系统, 来源:

    - github.com/Yeah114/bdump                       (Yeah114 的 BDump 库)
    - github.com/TriM-Organization/merry-memory      (merry-memory 命令系统)
    - embedded_source/merry_memory_command.go        (命令 ID 定义)
    - embedded_source/merry_memory_place_block_with_nbt_data.go (NBT 实现)

NexusE 内嵌了两套 BDump 实现, 命令 ID 完全一致, 但序列化方式不同:

    1. Yeah114 BDump:
       - 用于 BDX 文件格式 (Brotli 压缩 + RSA 签名)
       - 字符串通过 CreateConstantString 命令预定义
       - 后续命令通过 ConstantStringID 引用预定义的字符串

    2. merry-memory BDump:
       - 用于实际网络协议传输 (Bedrock 协议)
       - 使用更紧凑的 varint 编码
       - 支持 UseRuntimeIDPool 运行时 ID 池优化

命令 ID 表 (逆向自 embedded_source/merry_memory_command.go):
    ID=1   CreateConstantString          创建常量字符串
    ID=5   PlaceBlockWithBlockStates     使用 BlockStates 放置方块
    ID=7   PlaceBlock                    放置方块 (旧版, block_data)
    ID=9   NoOperation                   空操作
    ID=14  AddXValue                     X+1
    ID=15  SubtractXValue                X-1
    ID=16  AddYValue                     Y+1
    ID=17  SubtractYValue                Y-1
    ID=18  AddZValue                     Z+1
    ID=19  SubtractZValue                Z-1
    ID=20-25 AddInt8/16/32 X/Y/Z Value   可变步进
    ID=26  SetCommandBlockData           设置命令方块数据
    ID=27  PlaceBlockWithCommandBlockData 放置带命令方块数据的方块
    ID=31  UseRuntimeIDPool              使用运行时 ID 池
    ID=32  PlaceRuntimeBlock             使用运行时 ID 放置方块
    ID=37  PlaceRuntimeBlockWithChestData 使用运行时 ID 放置带容器数据的方块
    ID=40  PlaceBlockWithChestData       放置带容器数据的方块
    ID=41  PlaceBlockWithNBTData         放置带 NBT 数据的方块
    ID=88  Terminate                     终止命令流

特殊点 (逆向自 merry_memory_place_block_with_nbt_data.go):
    PlaceBlockWithNBTData (ID=41) 中 BlockStatesConstantStringID 被写入了两次,
    源码注释 "This is a mistake."
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

logger = logging.getLogger("pocketterm.protocol.command_systems.bdump_commands")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: BDump 编码器版本 (逆向自 strings: "Encoder Version: %v")
BDUMP_ENCODER_VERSION: int = 1

#: BDump 模式常量
BDUMP_MODE_BLOCK_STATES: int = 0       # 使用 BlockStates 字符串
BDUMP_MODE_RUNTIME_ID_POOL: int = 1    # 使用运行时 ID 池
BDUMP_MODE_COMMAND_BLOCK_DATA: int = 2 # 命令方块数据
BDUMP_MODE_CHEST_DATA: int = 3         # 容器数据
BDUMP_MODE_NBT_DATA: int = 4           # NBT 数据

#: 命令方块模式 (逆向自 merry-memory/protocol/encoding.CommandBlockData)
CB_MODE_IMPULSE: int = 0
CB_MODE_REPEAT: int = 1
CB_MODE_CHAIN: int = 2

#: 命令 ID -> 名称映射 (逆向自 embedded_source/merry_memory_command.go)
COMMAND_ID_TO_NAME: dict[int, str] = {
    1:  "CreateConstantString",
    5:  "PlaceBlockWithBlockStates",
    6:  "AddInt16ZValue0",
    7:  "PlaceBlock",
    8:  "AddZValue0",
    9:  "NoOperation",
    12: "AddInt32ZValue0",
    13: "PlaceBlockWithBlockStatesDeprecated",
    14: "AddXValue",
    15: "SubtractXValue",
    16: "AddYValue",
    17: "SubtractYValue",
    18: "AddZValue",
    19: "SubtractZValue",
    20: "AddInt16XValue",
    21: "AddInt32XValue",
    22: "AddInt16YValue",
    23: "AddInt32YValue",
    24: "AddInt16ZValue",
    25: "AddInt32ZValue",
    26: "SetCommandBlockData",
    27: "PlaceBlockWithCommandBlockData",
    28: "AddInt8XValue",
    29: "AddInt8YValue",
    30: "AddInt8ZValue",
    31: "UseRuntimeIDPool",
    32: "PlaceRuntimeBlock",
    33: "PlaceRuntimeBlockWithUint32RuntimeID",
    34: "PlaceRuntimeBlockWithCommandBlockData",
    35: "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID",
    36: "PlaceCommandBlockWithCommandBlockData",
    37: "PlaceRuntimeBlockWithChestData",
    38: "PlaceRuntimeBlockWithChestDataAndUint32RuntimeID",
    39: "AssignDebugData",
    40: "PlaceBlockWithChestData",
    41: "PlaceBlockWithNBTData",
    88: "Terminate",
}

#: 命令名称 -> ID 反向映射
COMMAND_NAME_TO_ID: dict[str, int] = {v: k for k, v in COMMAND_ID_TO_NAME.items()}

#: Terminate 命令 ID
ID_TERMINATE: int = 88


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class BDUMPError(Exception):
    """BDump 命令系统错误基类。"""


class BDumpEncodeError(BDUMPError):
    """BDump 编码错误。"""


class BDumpDecodeError(BDUMPError):
    """BDump 解码错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class BDumpCommand:
    """BDump 命令。

    逆向自 merry-memory/protocol/encoding.Command 接口。
    每个命令有 ID() / Name() / Marshal() / Unmarshal() 四个方法。
    """
    command_id: int
    name: str
    data: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"BDumpCommand(id={self.command_id}, name={self.name!r})"


@dataclass
class BDumpCommandContext:
    """BDump 命令上下文。

    逆向自 merry-memory/protocol/encoding.IO 上下文。
    维护当前坐标和常量字符串表。
    """
    x: int = 0
    y: int = 0
    z: int = 0
    constant_strings: list[str] = field(default_factory=list)
    runtime_id_pool: list[dict[str, Any]] = field(default_factory=list)
    current_pool_id: int = 0
    mode: int = BDUMP_MODE_BLOCK_STATES

    def add_constant_string(self, s: str) -> int:
        """添加常量字符串, 返回其 ID。

        逆向自 CreateConstantString 命令的处理逻辑。
        """
        if s in self.constant_strings:
            return self.constant_strings.index(s)
        self.constant_strings.append(s)
        return len(self.constant_strings) - 1

    def get_constant_string(self, idx: int) -> str:
        """获取常量字符串。"""
        if 0 <= idx < len(self.constant_strings):
            return self.constant_strings[idx]
        return ""

    def move(self, dx: int = 0, dy: int = 0, dz: int = 0) -> None:
        """移动当前坐标。"""
        self.x += dx
        self.y += dy
        self.z += dz


@dataclass
class BDumpCommandStream:
    """BDump 命令流。

    逆向自 merry-memory/protocol/encoding.CommandStream。
    包含一组有序的 BDump 命令。
    """
    commands: list[BDumpCommand] = field(default_factory=list)
    author: str = ""
    version: int = BDUMP_ENCODER_VERSION
    context: BDumpCommandContext = field(default_factory=BDumpCommandContext)

    @property
    def total_commands(self) -> int:
        """命令总数 (不含 Terminate)。"""
        return sum(1 for c in self.commands if c.command_id != ID_TERMINATE)

    @property
    def terminate_count(self) -> int:
        """Terminate 命令数。"""
        return sum(1 for c in self.commands if c.command_id == ID_TERMINATE)

    def append(self, cmd: BDumpCommand) -> None:
        """追加命令。"""
        self.commands.append(cmd)

    def terminate(self) -> None:
        """追加 Terminate 命令。"""
        self.append(BDumpCommand(
            command_id=ID_TERMINATE,
            name="Terminate",
        ))


# -------------------------------------------------------------------- #
# 编码器 (merry-memory 实现)
# -------------------------------------------------------------------- #


class BDumpEncoder:
    """BDump 命令流编码器 (merry-memory 实现)。

    逆向自 merry-memory/protocol/encoding 包中的编码器。
    使用大端序字节序 (与 BDX 文件格式一致)。
    """

    def __init__(self) -> None:
        self._buf = BytesIO()
        self._context = BDumpCommandContext()
        self._constant_strings_written: list[int] = []  # 已写入的常量字符串 ID

    def encode_stream(self, stream: BDumpCommandStream,
                       write_header: bool = True) -> bytes:
        """编码整个命令流。

        Args:
            stream: BDump 命令流。
            write_header: 是否写入 BDX 内层头 (BDX + 版本 + 作者)。

        Returns:
            编码后的字节数据。
        """
        self._buf = BytesIO()
        self._context = stream.context

        if write_header:
            # BDX 内层头
            self._buf.write(b"BDX")
            self._buf.write(struct.pack("B", stream.version))
            # 作者名 (NUL 结尾)
            self._buf.write(stream.author.encode("utf-8"))
            self._buf.write(b"\x00")

        # 编码所有命令
        for cmd in stream.commands:
            self._encode_command(cmd)

        return self._buf.getvalue()

    def _encode_command(self, cmd: BDumpCommand) -> None:
        """编码单条命令。"""
        self._buf.write(struct.pack("B", cmd.command_id))

        name = cmd.name
        data = cmd.data

        if name == "CreateConstantString":
            s = data.get("string", "")
            self._buf.write(s.encode("utf-8"))
            self._buf.write(b"\x00")

        elif name == "PlaceBlockWithBlockStates":
            self._buf.write(struct.pack(">H", data.get("block_id", 0)))
            self._buf.write(struct.pack(">H", data.get("block_states_id", 0)))

        elif name == "PlaceBlock":
            self._buf.write(struct.pack(">H", data.get("block_id", 0)))
            self._buf.write(struct.pack(">H", data.get("block_data", 0)))

        elif name in ("AddXValue", "SubtractXValue",
                      "AddYValue", "SubtractYValue",
                      "AddZValue", "SubtractZValue"):
            # 无载荷, 步进固定为 +/-1
            pass

        elif name in ("AddInt8XValue", "AddInt8YValue", "AddInt8ZValue"):
            self._buf.write(struct.pack(">b", data.get("value", 0)))

        elif name in ("AddInt16XValue", "AddInt16YValue", "AddInt16ZValue",
                      "AddInt16ZValue0"):
            self._buf.write(struct.pack(">h", data.get("value", 0)))

        elif name in ("AddInt32XValue", "AddInt32YValue", "AddInt32ZValue",
                      "AddInt32ZValue0"):
            self._buf.write(struct.pack(">i", data.get("value", 0)))

        elif name == "SetCommandBlockData":
            self._encode_command_block_data(data)

        elif name == "PlaceBlockWithCommandBlockData":
            self._buf.write(struct.pack(">H", data.get("block_id", 0)))
            self._buf.write(struct.pack(">H", data.get("block_states_id", 0)))
            self._encode_command_block_data(data)

        elif name == "PlaceCommandBlockWithCommandBlockData":
            self._encode_command_block_data(data)

        elif name == "UseRuntimeIDPool":
            self._buf.write(struct.pack("B", data.get("pool_id", 0)))

        elif name == "PlaceRuntimeBlock":
            self._buf.write(struct.pack(">H", data.get("runtime_id", 0)))

        elif name == "PlaceRuntimeBlockWithUint32RuntimeID":
            self._buf.write(struct.pack(">I", data.get("runtime_id", 0)))

        elif name == "PlaceRuntimeBlockWithCommandBlockData":
            self._buf.write(struct.pack(">H", data.get("runtime_id", 0)))
            self._encode_command_block_data(data)

        elif name == "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID":
            self._buf.write(struct.pack(">I", data.get("runtime_id", 0)))
            self._encode_command_block_data(data)

        elif name == "PlaceRuntimeBlockWithChestData":
            self._buf.write(struct.pack(">H", data.get("runtime_id", 0)))
            self._encode_chest_data(data.get("chest_data", {}))

        elif name == "PlaceRuntimeBlockWithChestDataAndUint32RuntimeID":
            self._buf.write(struct.pack(">I", data.get("runtime_id", 0)))
            self._encode_chest_data(data.get("chest_data", {}))

        elif name == "PlaceBlockWithChestData":
            self._buf.write(struct.pack(">H", data.get("block_id", 0)))
            self._buf.write(struct.pack(">H", data.get("block_states_id", 0)))
            self._buf.write(struct.pack("B", data.get("unknown_byte", 0)))
            self._encode_chest_data(data.get("chest_data", {}))

        elif name == "PlaceBlockWithNBTData":
            # 注意: BlockStatesConstantStringID 被写入两次 (源码 bug)
            # 逆向自 embedded_source/merry_memory_place_block_with_nbt_data.go
            self._buf.write(struct.pack(">H", data.get("block_id", 0)))
            self._buf.write(struct.pack(">H", data.get("block_states_id", 0)))
            self._buf.write(struct.pack(">H", data.get("block_states_id", 0)))
            # NBT 数据 (小端序)
            from ..format_parsers.nbt_parser import nbt_marshal, LITTLE_ENDIAN
            nbt_bytes = data.get("nbt_bytes", b"")
            if not nbt_bytes and "nbt_data" in data:
                nbt_bytes = nbt_marshal(
                    data["nbt_data"],
                    encoding=LITTLE_ENDIAN,
                    root_name=data.get("nbt_root_name", ""),
                )
            self._buf.write(nbt_bytes)

        elif name == "NoOperation":
            pass

        elif name == "AssignDebugData":
            debug_data = data.get("debug_data", b"")
            self._buf.write(struct.pack(">I", len(debug_data)))
            self._buf.write(debug_data)

        elif name == "Terminate":
            pass

        else:
            logger.warning("unknown command during encode: %s (id=%d)",
                           name, cmd.command_id)

    def _encode_command_block_data(self, data: dict[str, Any]) -> None:
        """编码命令方块数据 (逆向自 merry-memory/protocol/encoding.CommandBlockData)。"""
        self._buf.write(struct.pack(">I", data.get("mode", CB_MODE_IMPULSE)))
        # command (NUL 结尾)
        self._buf.write(data.get("command", "").encode("utf-8"))
        self._buf.write(b"\x00")
        # custom_name (NUL 结尾)
        self._buf.write(data.get("custom_name", "").encode("utf-8"))
        self._buf.write(b"\x00")
        # last_output (NUL 结尾)
        self._buf.write(data.get("last_output", "").encode("utf-8"))
        self._buf.write(b"\x00")
        # tick_delay
        self._buf.write(struct.pack(">i", data.get("tick_delay", 0)))
        # flags
        flags = 0
        if data.get("execute_on_first_tick", False):
            flags |= 0x01
        if data.get("track_output", False):
            flags |= 0x02
        if data.get("conditional", False):
            flags |= 0x04
        if data.get("needs_redstone", False):
            flags |= 0x08
        self._buf.write(struct.pack("B", flags))

    def _encode_chest_data(self, data: dict[str, Any]) -> None:
        """编码容器数据 (逆向自 merry-memory/protocol/encoding.ChestData)。"""
        slots = data.get("slots", [])
        self._buf.write(struct.pack("B", len(slots)))
        # unknown_field
        self._buf.write(struct.pack("B", data.get("unknown_field", 0)))
        for slot in slots:
            network_id = slot.get("network_id", 0)
            self._buf.write(struct.pack(">i", network_id))
            if network_id != 0:
                self._buf.write(struct.pack(">H", slot.get("count", 1)))
                self._buf.write(struct.pack(">i", slot.get("aux_value", 0)))
                nbt_bytes = slot.get("nbt_bytes", b"")
                self._buf.write(struct.pack(">I", len(nbt_bytes) if nbt_bytes else 0))
                if nbt_bytes:
                    self._buf.write(nbt_bytes)
                # can_place_on
                can_place_on = slot.get("can_place_on", [])
                self._buf.write(struct.pack(">i", len(can_place_on)))
                for s in can_place_on:
                    self._buf.write(s.encode("utf-8"))
                    self._buf.write(b"\x00")
                # can_destroy
                can_destroy = slot.get("can_destroy", [])
                self._buf.write(struct.pack(">i", len(can_destroy)))
                for s in can_destroy:
                    self._buf.write(s.encode("utf-8"))
                    self._buf.write(b"\x00")


# -------------------------------------------------------------------- #
# 解码器
# -------------------------------------------------------------------- #


class BDumpDecoder:
    """BDump 命令流解码器。

    逆向自 merry-memory/protocol/encoding 包中的解码器。
    """

    def __init__(self) -> None:
        self._buf = BytesIO()
        self._context = BDumpCommandContext()

    def decode_stream(self, data: bytes,
                       has_header: bool = True) -> BDumpCommandStream:
        """解码命令流。

        Args:
            data: 编码后的字节数据。
            has_header: 数据是否包含 BDX 内层头。

        Returns:
            :class:`BDumpCommandStream`。
        """
        self._buf = BytesIO(data)
        stream = BDumpCommandStream(context=self._context)

        if has_header:
            # BDX 内层头
            sig = self._read(3)
            if sig != b"BDX":
                raise BDumpDecodeError(f"invalid signature: {sig!r}")
            stream.version = self._read_byte()
            stream.author = self._read_string()

        # 解码命令
        while True:
            cmd_id = self._read_byte()
            if cmd_id is None:
                break
            cmd = self._decode_command(cmd_id)
            stream.append(cmd)
            if cmd.command_id == ID_TERMINATE:
                break

        return stream

    def _read(self, n: int) -> bytes:
        data = self._buf.read(n)
        if len(data) < n:
            if len(data) == 0:
                return b""
            raise BDumpDecodeError(
                f"unexpected EOF: wanted {n} bytes, got {len(data)}"
            )
        return data

    def _read_byte(self) -> int | None:
        data = self._buf.read(1)
        if not data:
            return None
        return data[0]

    def _read_uint8(self) -> int:
        b = self._read(1)
        return b[0] if b else 0

    def _read_int8(self) -> int:
        return struct.unpack("b", self._read(1))[0]

    def _read_uint16(self) -> int:
        return struct.unpack(">H", self._read(2))[0]

    def _read_int16(self) -> int:
        return struct.unpack(">h", self._read(2))[0]

    def _read_int32(self) -> int:
        return struct.unpack(">i", self._read(4))[0]

    def _read_uint32(self) -> int:
        return struct.unpack(">I", self._read(4))[0]

    def _read_string(self) -> str:
        chars: list[bytes] = []
        while True:
            b = self._read(1)
            if not b or b == b"\x00":
                break
            chars.append(b)
        return b"".join(chars).decode("utf-8", errors="replace")

    def _decode_command(self, cmd_id: int) -> BDumpCommand:
        """解码单条命令。"""
        name = COMMAND_ID_TO_NAME.get(cmd_id, f"Unknown_{cmd_id}")
        data: dict[str, Any] = {}

        if name == "CreateConstantString":
            data["string"] = self._read_string()

        elif name == "PlaceBlockWithBlockStates":
            data["block_id"] = self._read_uint16()
            data["block_states_id"] = self._read_uint16()

        elif name == "PlaceBlock":
            data["block_id"] = self._read_uint16()
            data["block_data"] = self._read_uint16()

        elif name in ("AddInt8XValue", "AddInt8YValue", "AddInt8ZValue"):
            data["value"] = self._read_int8()

        elif name in ("AddInt16XValue", "AddInt16YValue", "AddInt16ZValue",
                      "AddInt16ZValue0"):
            data["value"] = self._read_int16()

        elif name in ("AddInt32XValue", "AddInt32YValue", "AddInt32ZValue",
                      "AddInt32ZValue0"):
            data["value"] = self._read_int32()

        elif name == "SetCommandBlockData":
            data.update(self._decode_command_block_data())

        elif name == "PlaceBlockWithCommandBlockData":
            data["block_id"] = self._read_uint16()
            data["block_states_id"] = self._read_uint16()
            data.update(self._decode_command_block_data())

        elif name == "PlaceCommandBlockWithCommandBlockData":
            data.update(self._decode_command_block_data())

        elif name == "UseRuntimeIDPool":
            data["pool_id"] = self._read_uint8()

        elif name == "PlaceRuntimeBlock":
            data["runtime_id"] = self._read_uint16()

        elif name == "PlaceRuntimeBlockWithUint32RuntimeID":
            data["runtime_id"] = self._read_uint32()

        elif name == "PlaceRuntimeBlockWithCommandBlockData":
            data["runtime_id"] = self._read_uint16()
            data.update(self._decode_command_block_data())

        elif name == "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID":
            data["runtime_id"] = self._read_uint32()
            data.update(self._decode_command_block_data())

        elif name == "PlaceRuntimeBlockWithChestData":
            data["runtime_id"] = self._read_uint16()
            data["chest_data"] = self._decode_chest_data()

        elif name == "PlaceRuntimeBlockWithChestDataAndUint32RuntimeID":
            data["runtime_id"] = self._read_uint32()
            data["chest_data"] = self._decode_chest_data()

        elif name == "PlaceBlockWithChestData":
            data["block_id"] = self._read_uint16()
            data["block_states_id"] = self._read_uint16()
            data["unknown_byte"] = self._read_uint8()
            data["chest_data"] = self._decode_chest_data()

        elif name == "PlaceBlockWithNBTData":
            data["block_id"] = self._read_uint16()
            data["block_states_id"] = self._read_uint16()
            data["block_states_id_duplicate"] = self._read_uint16()
            # 剩余作为 NBT 数据
            remaining = self._buf.read()
            if remaining:
                try:
                    from ..format_parsers.nbt_parser import (
                        nbt_unmarshal, LITTLE_ENDIAN,
                    )
                    data["nbt_data"] = nbt_unmarshal(remaining, LITTLE_ENDIAN)
                except Exception as exc:
                    logger.warning("NBT decode failed: %s", exc)
                    data["nbt_bytes"] = remaining

        elif name == "AssignDebugData":
            length = self._read_uint32()
            data["debug_data"] = self._read(length)

        elif name == "Terminate":
            pass

        elif name in ("AddXValue", "SubtractXValue",
                      "AddYValue", "SubtractYValue",
                      "AddZValue", "SubtractZValue",
                      "NoOperation"):
            pass

        else:
            logger.warning("unknown command id during decode: %d", cmd_id)

        return BDumpCommand(command_id=cmd_id, name=name, data=data)

    def _decode_command_block_data(self) -> dict[str, Any]:
        """解码命令方块数据。"""
        data: dict[str, Any] = {}
        data["mode"] = self._read_uint32()
        data["command"] = self._read_string()
        data["custom_name"] = self._read_string()
        data["last_output"] = self._read_string()
        data["tick_delay"] = self._read_int32()
        flags = self._read_uint8()
        data["execute_on_first_tick"] = bool(flags & 0x01)
        data["track_output"] = bool(flags & 0x02)
        data["conditional"] = bool(flags & 0x04)
        data["needs_redstone"] = bool(flags & 0x08)
        return data

    def _decode_chest_data(self) -> dict[str, Any]:
        """解码容器数据。"""
        data: dict[str, Any] = {}
        chest_size = self._read_uint8()
        data["chest_size"] = chest_size
        data["unknown_field"] = self._read_uint8()
        slots: list[dict[str, Any]] = []
        for _ in range(chest_size):
            slot: dict[str, Any] = {}
            slot["network_id"] = self._read_int32()
            if slot["network_id"] != 0:
                slot["count"] = self._read_uint16()
                slot["aux_value"] = self._read_int32()
                nbt_len = self._read_uint32()
                if nbt_len > 0 and nbt_len != 0xFFFFFFFF:
                    slot["nbt_bytes"] = self._read(nbt_len)
                slot["can_place_on"] = [
                    self._read_string()
                    for _ in range(self._read_int32())
                ]
                slot["can_destroy"] = [
                    self._read_string()
                    for _ in range(self._read_int32())
                ]
            slots.append(slot)
        data["slots"] = slots
        return data


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def create_command_stream(author: str = "",
                            version: int = BDUMP_ENCODER_VERSION) -> BDumpCommandStream:
    """创建空的 BDump 命令流。

    Args:
        author: 作者名。
        version: 编码器版本。

    Returns:
        空的 :class:`BDumpCommandStream`。
    """
    return BDumpCommandStream(author=author, version=version)


def encode_command_stream(stream: BDumpCommandStream,
                            write_header: bool = True) -> bytes:
    """编码命令流为字节数据。

    Args:
        stream: BDump 命令流。
        write_header: 是否写入 BDX 内层头。

    Returns:
        编码后的字节数据。
    """
    encoder = BDumpEncoder()
    return encoder.encode_stream(stream, write_header=write_header)


def decode_command_stream(data: bytes,
                            has_header: bool = True) -> BDumpCommandStream:
    """解码字节数据为命令流。

    Args:
        data: 编码后的字节数据。
        has_header: 数据是否包含 BDX 内层头。

    Returns:
        :class:`BDumpCommandStream`。
    """
    decoder = BDumpDecoder()
    return decoder.decode_stream(data, has_header=has_header)


def get_command_stream_stats(stream: BDumpCommandStream) -> dict[str, Any]:
    """获取命令流的统计信息。

    Args:
        stream: BDump 命令流。

    Returns:
        统计信息字典, 包含各类命令的计数。
    """
    stats: dict[str, Any] = {
        "total": stream.total_commands,
        "terminate": stream.terminate_count,
        "by_name": {},
        "by_id": {},
        "place_block_count": 0,
        "command_block_count": 0,
        "container_count": 0,
        "nbt_count": 0,
        "move_count": 0,
    }
    place_block_names = {
        "PlaceBlock", "PlaceBlockWithBlockStates",
        "PlaceBlockWithBlockStatesDeprecated",
        "PlaceBlockWithChestData", "PlaceBlockWithNBTData",
        "PlaceBlockWithCommandBlockData",
        "PlaceRuntimeBlock", "PlaceRuntimeBlockWithUint32RuntimeID",
        "PlaceRuntimeBlockWithChestData",
        "PlaceRuntimeBlockWithChestDataAndUint32RuntimeID",
        "PlaceRuntimeBlockWithCommandBlockData",
        "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID",
        "PlaceCommandBlockWithCommandBlockData",
    }
    move_names = {
        "AddXValue", "SubtractXValue", "AddYValue", "SubtractYValue",
        "AddZValue", "SubtractZValue",
        "AddInt8XValue", "AddInt8YValue", "AddInt8ZValue",
        "AddInt16XValue", "AddInt16YValue", "AddInt16ZValue",
        "AddInt32XValue", "AddInt32YValue", "AddInt32ZValue",
        "AddZValue0", "AddInt16ZValue0", "AddInt32ZValue0",
    }
    for cmd in stream.commands:
        stats["by_name"][cmd.name] = stats["by_name"].get(cmd.name, 0) + 1
        stats["by_id"][cmd.command_id] = stats["by_id"].get(cmd.command_id, 0) + 1
        if cmd.name in place_block_names:
            stats["place_block_count"] += 1
        if "CommandBlockData" in cmd.name:
            stats["command_block_count"] += 1
        if "ChestData" in cmd.name:
            stats["container_count"] += 1
        if cmd.name == "PlaceBlockWithNBTData":
            stats["nbt_count"] += 1
        if cmd.name in move_names:
            stats["move_count"] += 1
    return stats


__all__ = [
    # 常量
    "BDUMP_ENCODER_VERSION",
    "BDUMP_MODE_BLOCK_STATES", "BDUMP_MODE_RUNTIME_ID_POOL",
    "BDUMP_MODE_COMMAND_BLOCK_DATA", "BDUMP_MODE_CHEST_DATA",
    "BDUMP_MODE_NBT_DATA",
    "CB_MODE_IMPULSE", "CB_MODE_REPEAT", "CB_MODE_CHAIN",
    "COMMAND_ID_TO_NAME", "COMMAND_NAME_TO_ID", "ID_TERMINATE",
    # 异常
    "BDUMPError", "BDumpEncodeError", "BDumpDecodeError",
    # 数据结构
    "BDumpCommand", "BDumpCommandContext", "BDumpCommandStream",
    # 编码器/解码器
    "BDumpEncoder", "BDumpDecoder",
    # 顶层函数
    "create_command_stream", "encode_command_stream",
    "decode_command_stream", "get_command_stream_stats",
]
