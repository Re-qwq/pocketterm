"""bdx_parser - BDX (BDump) 格式解析器。

逆向自 NexusEgo v1.6.5 的 BDX 解析层, 来源:

    - WaterStructure/structure/bdx.go                          (BDX 文件结构定义)
    - WaterStructure/modules/bdump/command/*.go                (33 种 BDump 命令)
    - embedded_source/merry_memory_command.go                  (merry-memory 命令 ID 表)
    - embedded_source/merry_memory_place_block_with_nbt_data.go (PlaceBlockWithNBTData 实现)

BDX 文件格式 (PhoenixBuilder 定义):

    外层:
        "BD@" (3 字节签名)
        + Brotli 压缩数据 (内层)

    内层 (Brotli 解压后):
        "BDX" (3 字节内层签名)
        "\\x00" (1 字节版本标记)
        作者名 (UTF-8 字符串, 以 "\\x00" 结尾)
        命令流 (每条命令以 1 字节 commandId 开头)
        "XE" (无签名) 或 签名数据

命令流:
    每条命令以 1 字节 commandId 开头 (大端序):
        - commandId == 88 (Terminate): 结束
        - 其他: 对应操作符的 payload

注意:
    PlaceBlockWithNBTData (ID=41) 在源码中有一个 bug:
    BlockStatesConstantStringID 被写入两次 ("This is a mistake")。
    本解析器兼容此 bug。
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from .nbt_parser import nbt_unmarshal, LITTLE_ENDIAN, NBTError

logger = logging.getLogger("pocketterm.protocol.format_parsers.bdx_parser")

try:
    import brotli
    _HAS_BROTLI = True
except ImportError:
    _HAS_BROTLI = False
    logger.warning("brotli module not available; BDX decompression will fail")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: BDX 外层签名 (逆向自 strings: "BD@")
BDX_OUTER_SIGNATURE: bytes = b"BD@"

#: BDX 内层签名 (逆向自 strings: "BDX")
BDX_INNER_SIGNATURE: bytes = b"BDX"

#: BDX 无签名标记 (逆向自 strings: "XE")
BDX_NO_SIGNATURE: bytes = b"XE"

#: BDX 版本标记
BDX_VERSION_BYTE: int = 0

#: Terminate 命令 ID (显式赋值, 逆向自 merry_memory_command.go)
BDUMP_ID_TERMINATE: int = 88

#: BDump 命令 ID 常量 (逆向自 embedded_source/merry_memory_command.go 的 iota + 1 起始)
BDUMP_COMMAND_IDS: dict[int, str] = {
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
BDUMP_COMMAND_NAMES: dict[str, int] = {v: k for k, v in BDUMP_COMMAND_IDS.items()}

#: 命令方块模式常量 (逆向自 merry-memory/protocol/encoding.CommandBlockData)
COMMAND_BLOCK_MODE_REPEAT: int = 1
COMMAND_BLOCK_MODE_CHAIN: int = 2
COMMAND_BLOCK_MODE_IMPULSE: int = 0  # 默认

#: 命令方块模式名称
COMMAND_BLOCK_MODE_NAMES: dict[int, str] = {
    0: "Impulse",
    1: "Repeat",
    2: "Chain",
}


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class BDXError(Exception):
    """BDX 文件解析错误基类。"""


class BDXHeaderError(BDXError):
    """BDX 文件头错误 (签名不匹配)。"""


class BDXUnknownCommandError(BDXError):
    """未知的 BDump 命令 ID。"""

    def __init__(self, command_id: int) -> None:
        self.command_id = command_id
        super().__init__(f"unknown BDump command id: {command_id}")


class BDXReadError(BDXError):
    """BDX 文件读取错误 (数据不完整)。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class BDXCommand:
    """BDump 命令。

    逆向自 WaterStructure/modules/bdump/command/command.go 的 Command 接口。
    每个命令实现 ID() / Name() / Marshal() 三个方法。
    """
    id: int
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    raw_payload: bytes = b""

    def __repr__(self) -> str:
        return f"BDXCommand(id={self.id}, name={self.name!r}, data={self.data})"


@dataclass
class BDXSignature:
    """BDX 文件签名。

    BDX 文件可以包含 RSA 签名 (逆向自字符串 "XE" 表示无签名)。
    NexusE 的 BDump 实现使用 RSA 进行签名验证。
    """
    has_signature: bool = False
    signature_data: bytes = b""
    public_key: bytes = b""

    def __repr__(self) -> str:
        return (
            f"BDXSignature(has_signature={self.has_signature}, "
            f"sig_len={len(self.signature_data)})"
        )


@dataclass
class BDXResult:
    """BDX 文件解析结果。

    逆向自 WaterStructure/structure/bdx.go 的 BDX 结构体。
    """
    author: str = ""
    version: int = 0
    commands: list[BDXCommand] = field(default_factory=list)
    signature: BDXSignature = field(default_factory=BDXSignature)
    raw_size: int = 0
    compressed_size: int = 0
    decompressed_size: int = 0
    is_compressed: bool = False

    @property
    def total_commands(self) -> int:
        """命令总数 (不含 Terminate)。"""
        return sum(1 for c in self.commands if c.id != BDUMP_ID_TERMINATE)

    @property
    def place_block_count(self) -> int:
        """放置方块类命令数。"""
        place_ids = {
            BDUMP_COMMAND_NAMES["PlaceBlock"],
            BDUMP_COMMAND_NAMES["PlaceBlockWithBlockStates"],
            BDUMP_COMMAND_NAMES["PlaceBlockWithBlockStatesDeprecated"],
            BDUMP_COMMAND_NAMES["PlaceBlockWithChestData"],
            BDUMP_COMMAND_NAMES["PlaceBlockWithNBTData"],
            BDUMP_COMMAND_NAMES["PlaceBlockWithCommandBlockData"],
            BDUMP_COMMAND_NAMES["PlaceRuntimeBlock"],
            BDUMP_COMMAND_NAMES["PlaceRuntimeBlockWithUint32RuntimeID"],
            BDUMP_COMMAND_NAMES["PlaceRuntimeBlockWithChestData"],
            BDUMP_COMMAND_NAMES["PlaceRuntimeBlockWithChestDataAndUint32RuntimeID"],
            BDUMP_COMMAND_NAMES["PlaceRuntimeBlockWithCommandBlockData"],
            BDUMP_COMMAND_NAMES["PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID"],
            BDUMP_COMMAND_NAMES["PlaceCommandBlockWithCommandBlockData"],
        }
        return sum(1 for c in self.commands if c.id in place_ids)

    @property
    def has_nbt_data(self) -> bool:
        """是否包含 NBT 数据 (PlaceBlockWithNBTData 或 PlaceBlockWithChestData)。"""
        return any(
            c.id in (
                BDUMP_COMMAND_NAMES["PlaceBlockWithNBTData"],
                BDUMP_COMMAND_NAMES["PlaceBlockWithChestData"],
                BDUMP_COMMAND_NAMES["PlaceRuntimeBlockWithChestData"],
                BDUMP_COMMAND_NAMES["PlaceRuntimeBlockWithChestDataAndUint32RuntimeID"],
            )
            for c in self.commands
        )


# -------------------------------------------------------------------- #
# 内部读取器
# -------------------------------------------------------------------- #


class _BDXReader:
    """BDX 二进制数据读取器 (大端序, 逆向自 merry-memory/encoding.IO)。"""

    def __init__(self, data: bytes) -> None:
        self._buf = BytesIO(data)
        self._pos = 0

    def remaining(self) -> int:
        cur = self._buf.tell()
        self._buf.seek(0, 2)
        end = self._buf.tell()
        self._buf.seek(cur)
        return end - cur

    def read(self, n: int) -> bytes:
        data = self._buf.read(n)
        if len(data) != n:
            raise BDXReadError(
                f"unexpected EOF: wanted {n} bytes, got {len(data)}"
            )
        return data

    def read_byte(self) -> int:
        """读取 int8。"""
        return struct.unpack("b", self.read(1))[0]

    def read_uint8(self) -> int:
        """读取 uint8。"""
        return self.read(1)[0]

    def read_uint16(self) -> int:
        """读取 uint16 (大端序)。"""
        return struct.unpack(">H", self.read(2))[0]

    def read_int16(self) -> int:
        """读取 int16 (大端序)。"""
        return struct.unpack(">h", self.read(2))[0]

    def read_int32(self) -> int:
        """读取 int32 (大端序)。"""
        return struct.unpack(">i", self.read(4))[0]

    def read_uint32(self) -> int:
        """读取 uint32 (大端序)。"""
        return struct.unpack(">I", self.read(4))[0]

    def read_string(self) -> str:
        """读取以 NUL 结尾的 UTF-8 字符串。"""
        chars: list[bytes] = []
        while True:
            b = self.read(1)
            if b == b"\x00":
                break
            chars.append(b)
        return b"".join(chars).decode("utf-8", errors="replace")

    def read_bytes(self, n: int) -> bytes:
        """读取原始字节。"""
        return self.read(n)


# -------------------------------------------------------------------- #
# 解析主流程
# -------------------------------------------------------------------- #


def parse_bdx_bytes(data: bytes) -> BDXResult:
    """解析 BDX 文件的字节数据。

    逆向自 WaterStructure/structure/bdx.go 的 BDX 解析入口。

    Args:
        data: BDX 文件的完整字节数据。

    Returns:
        :class:`BDXResult` 解析结果。

    Raises:
        BDXHeaderError: 文件头签名不匹配。
        BDXUnknownCommandError: 遇到未知的命令 ID。
        BDXReadError: 文件数据不完整。
    """
    if not data:
        raise BDXHeaderError("empty data")

    result = BDXResult(raw_size=len(data))

    # 检查外层签名 "BD@"
    if data[:3] != BDX_OUTER_SIGNATURE:
        # 可能是已解压的内层数据, 检查 "BDX"
        if data[:3] == BDX_INNER_SIGNATURE:
            logger.debug("BDX data appears to be already decompressed")
            return _parse_inner_bdx(data, result)
        raise BDXHeaderError(
            f"invalid outer signature: {data[:3]!r}, expected {BDX_OUTER_SIGNATURE!r}"
        )

    # Brotli 解压
    if not _HAS_BROTLI:
        raise BDXError("brotli module required to decompress BDX file")
    compressed_payload = data[3:]
    result.compressed_size = len(compressed_payload)
    try:
        decompressed = brotli.decompress(compressed_payload)
    except brotli.Error as exc:
        raise BDXError(f"brotli decompression failed: {exc}") from exc
    result.decompressed_size = len(decompressed)
    result.is_compressed = True
    logger.debug(
        "BDX decompressed: %d -> %d bytes",
        result.compressed_size, result.decompressed_size,
    )

    return _parse_inner_bdx(decompressed, result)


def _parse_inner_bdx(data: bytes, result: BDXResult) -> BDXResult:
    """解析已解压的 BDX 内层数据。

    Args:
        data: BDX 内层字节数据 (以 "BDX" 开头)。
        result: 要填充的 :class:`BDXResult` 对象。

    Returns:
        填充后的 :class:`BDXResult`。
    """
    reader = _BDXReader(data)

    # 内层签名 "BDX"
    sig = reader.read_bytes(3)
    if sig != BDX_INNER_SIGNATURE:
        raise BDXHeaderError(
            f"invalid inner signature: {sig!r}, expected {BDX_INNER_SIGNATURE!r}"
        )

    # 版本字节
    version = reader.read_uint8()
    result.version = version

    # 作者名 (NUL 结尾)
    author = reader.read_string()
    result.author = author
    logger.debug("BDX author=%r, version=%d", author, version)

    # 命令流
    commands: list[BDXCommand] = []
    while reader.remaining() > 0:
        cmd_id = reader.read_uint8()
        if cmd_id == BDUMP_ID_TERMINATE:
            commands.append(BDXCommand(
                id=cmd_id, name="Terminate",
                data={}, raw_payload=b"",
            ))
            break
        cmd = _parse_command(cmd_id, reader)
        commands.append(cmd)
    result.commands = commands

    # 检查签名
    if reader.remaining() >= 2:
        sig_marker = reader.read_bytes(2)
        if sig_marker == BDX_NO_SIGNATURE:
            result.signature = BDXSignature(has_signature=False)
        else:
            # 提取剩余作为签名数据
            sig_data = sig_marker + reader.read(reader.remaining())
            result.signature = BDXSignature(
                has_signature=True,
                signature_data=sig_data,
            )
            logger.debug("BDX signature: %d bytes", len(sig_data))

    logger.info(
        "BDX parsed: author=%r, commands=%d, place_blocks=%d, has_nbt=%s",
        result.author, result.total_commands,
        result.place_block_count, result.has_nbt_data,
    )
    return result


def _parse_command(cmd_id: int, reader: _BDXReader) -> BDXCommand:
    """解析单条 BDump 命令。

    Args:
        cmd_id: 命令 ID。
        reader: BDX 读取器。

    Returns:
        :class:`BDXCommand` 对象。

    Raises:
        BDXUnknownCommandError: 未知的命令 ID。
    """
    name = BDUMP_COMMAND_IDS.get(cmd_id)
    if name is None:
        raise BDXUnknownCommandError(cmd_id)

    data: dict[str, Any] = {}
    start_pos = reader._buf.tell()

    if name == "CreateConstantString":
        data["string"] = reader.read_string()

    elif name == "PlaceBlockWithBlockStates":
        data["block_constant_string_id"] = reader.read_uint16()
        data["block_states_constant_string_id"] = reader.read_uint16()

    elif name == "AddInt16ZValue0":
        data["value"] = reader.read_int16()

    elif name == "PlaceBlock":
        data["block_constant_string_id"] = reader.read_uint16()
        data["block_data"] = reader.read_uint16()

    elif name == "AddZValue0":
        data["value"] = 0  # 固定 +0

    elif name == "NoOperation":
        pass  # 无载荷

    elif name == "AddInt32ZValue0":
        data["value"] = reader.read_int32()

    elif name == "PlaceBlockWithBlockStatesDeprecated":
        data["block_constant_string_id"] = reader.read_uint16()
        data["block_states_constant_string_id"] = reader.read_uint16()

    elif name == "AddXValue":
        data["value"] = 1  # 固定 +1
    elif name == "SubtractXValue":
        data["value"] = -1  # 固定 -1
    elif name == "AddYValue":
        data["value"] = 1
    elif name == "SubtractYValue":
        data["value"] = -1
    elif name == "AddZValue":
        data["value"] = 1
    elif name == "SubtractZValue":
        data["value"] = -1

    elif name in ("AddInt16XValue", "AddInt16YValue", "AddInt16ZValue"):
        data["value"] = reader.read_int16()
    elif name in ("AddInt32XValue", "AddInt32YValue", "AddInt32ZValue"):
        data["value"] = reader.read_int32()
    elif name in ("AddInt8XValue", "AddInt8YValue", "AddInt8ZValue"):
        data["value"] = reader.read_byte()

    elif name == "SetCommandBlockData":
        data.update(_read_command_block_data(reader))

    elif name == "PlaceBlockWithCommandBlockData":
        data["block_constant_string_id"] = reader.read_uint16()
        data["block_states_constant_string_id"] = reader.read_uint16()
        data.update(_read_command_block_data(reader))

    elif name == "UseRuntimeIDPool":
        data["pool_id"] = reader.read_uint8()

    elif name == "PlaceRuntimeBlock":
        data["runtime_id"] = reader.read_uint16()

    elif name == "PlaceRuntimeBlockWithUint32RuntimeID":
        data["runtime_id"] = reader.read_uint32()

    elif name == "PlaceRuntimeBlockWithCommandBlockData":
        data["runtime_id"] = reader.read_uint16()
        data.update(_read_command_block_data(reader))

    elif name == "PlaceRuntimeBlockWithCommandBlockDataAndUint32RuntimeID":
        data["runtime_id"] = reader.read_uint32()
        data.update(_read_command_block_data(reader))

    elif name == "PlaceCommandBlockWithCommandBlockData":
        data.update(_read_command_block_data(reader))

    elif name == "PlaceRuntimeBlockWithChestData":
        data["runtime_id"] = reader.read_uint16()
        data["chest_data"] = _read_chest_data(reader)

    elif name == "PlaceRuntimeBlockWithChestDataAndUint32RuntimeID":
        data["runtime_id"] = reader.read_uint32()
        data["chest_data"] = _read_chest_data(reader)

    elif name == "AssignDebugData":
        length = reader.read_uint32()
        data["debug_data"] = reader.read_bytes(length)

    elif name == "PlaceBlockWithChestData":
        data["block_constant_string_id"] = reader.read_uint16()
        data["block_states_constant_string_id"] = reader.read_uint16()
        data["unknown_byte"] = reader.read_uint8()
        data["chest_data"] = _read_chest_data(reader)

    elif name == "PlaceBlockWithNBTData":
        # 注意: BlockStatesConstantStringID 在源码中被写入了两次
        # (逆向自 embedded_source/merry_memory_place_block_with_nbt_data.go:
        #  "This is a mistake.")
        data["block_constant_string_id"] = reader.read_uint16()
        data["block_states_constant_string_id"] = reader.read_uint16()
        # 第二次读取 (源码 bug)
        data["block_states_constant_string_id_duplicate"] = reader.read_uint16()
        # NBT 数据 (小端序)
        try:
            remaining = reader.read(reader.remaining())
            data["nbt_data"] = nbt_unmarshal(remaining, LITTLE_ENDIAN)
        except NBTError as exc:
            logger.warning("failed to parse NBT in PlaceBlockWithNBTData: %s", exc)
            data["nbt_data"] = None

    else:
        raise BDXUnknownCommandError(cmd_id)

    end_pos = reader._buf.tell()
    # 记录原始载荷 (仅用于调试)
    raw_payload = b""
    try:
        reader._buf.seek(start_pos)
        raw_payload = reader._buf.read(end_pos - start_pos)
        reader._buf.seek(end_pos)
    except Exception:
        pass

    return BDXCommand(id=cmd_id, name=name, data=data, raw_payload=raw_payload)


def _read_command_block_data(reader: _BDXReader) -> dict[str, Any]:
    """读取 SetCommandBlockData 的载荷。

    逆向自 merry-memory/protocol/encoding.CommandBlockData 结构:
        struct {
            Mode uint32
            Command string
            CustomName string
            LastOutput string
            TickDelay int32
            ExecuteOnFirstTick bool
            TrackOutput bool
            Conditional bool
            NeedsRedstone bool
        }
    """
    data: dict[str, Any] = {}
    data["mode"] = reader.read_uint32()
    data["command"] = reader.read_string()
    data["custom_name"] = reader.read_string()
    data["last_output"] = reader.read_string()
    data["tick_delay"] = reader.read_int32()
    data["execute_on_first_tick"] = bool(reader.read_uint8())
    data["track_output"] = bool(reader.read_uint8())
    data["conditional"] = bool(reader.read_uint8())
    data["needs_redstone"] = bool(reader.read_uint8())
    return data


def _read_chest_data(reader: _BDXReader) -> dict[str, Any]:
    """读取 ChestData 的载荷。

    逆向自 merry-memory/protocol/encoding.ChestData 结构。
    NexusEgo 的 PlaceBlockWithChestData 命令用于一次性放置容器及其内容物。
    """
    data: dict[str, Any] = {}
    data["chest_size"] = reader.read_uint8()
    data["unknown_field"] = reader.read_uint8()
    # 读取物品槽位 (每个槽位: NetworkItemStack)
    slots: list[dict[str, Any]] = []
    for _ in range(data["chest_size"]):
        slot: dict[str, Any] = {}
        slot["network_id"] = reader.read_int32()
        if slot["network_id"] != 0:
            slot["count"] = reader.read_uint16()
            slot["aux_value"] = reader.read_int32()
            nbt_len = reader.read_uint32()
            if nbt_len > 0 and nbt_len != 0xFFFFFFFF:
                slot["nbt_length"] = nbt_len
                slot["nbt_data"] = reader.read_bytes(nbt_len)
            slot["can_place_on_count"] = reader.read_int32()
            if slot["can_place_on_count"] > 0:
                slot["can_place_on"] = [
                    reader.read_string() for _ in range(slot["can_place_on_count"])
                ]
            slot["can_destroy_count"] = reader.read_int32()
            if slot["can_destroy_count"] > 0:
                slot["can_destroy"] = [
                    reader.read_string() for _ in range(slot["can_destroy_count"])
                ]
        slots.append(slot)
    data["slots"] = slots
    return data


# -------------------------------------------------------------------- #
# 文件解析入口
# -------------------------------------------------------------------- #


def parse_bdx_file(file_path: str) -> BDXResult:
    """解析 BDX 文件。

    逆向自 WaterStructure/structure/bdx.go 的文件读取入口。

    Args:
        file_path: BDX 文件路径。

    Returns:
        :class:`BDXResult` 解析结果。
    """
    with open(file_path, "rb") as f:
        data = f.read()
    return parse_bdx_bytes(data)


# -------------------------------------------------------------------- #
# 辅助函数
# -------------------------------------------------------------------- #


def reconstruct_blocks(result: BDXResult,
                        start_pos: tuple[int, int, int] = (0, 0, 0)
                        ) -> list[dict[str, Any]]:
    """根据 BDX 命令流重建方块放置列表。

    逆向自 NexusE 的 BDX 命令执行器。BDump 命令流使用相对坐标:
        - AddXValue / AddYValue / AddZValue: 固定步进 (+1 / -1)
        - AddInt8/16/32 X/Y/Z Value: 可变步进
        - Place* 命令: 在当前坐标放置方块

    Args:
        result: BDX 解析结果。
        start_pos: 起始坐标 (x, y, z)。

    Returns:
        方块放置列表, 每项包含 position / block_name / block_states / nbt_data 等字段。
    """
    x, y, z = start_pos
    constant_strings: dict[int, str] = {}
    blocks: list[dict[str, Any]] = []

    for cmd in result.commands:
        if cmd.name == "Terminate":
            break
        if cmd.name == "CreateConstantString":
            constant_strings[cmd.data.get("string", "")] = cmd.data.get("string", "")
            # 也按出现顺序索引
            idx = len(constant_strings)
            constant_strings[idx] = cmd.data.get("string", "")
            continue

        # 处理坐标增量
        if cmd.name in ("AddXValue", "AddYValue", "AddZValue",
                        "SubtractXValue", "SubtractYValue", "SubtractZValue",
                        "AddInt8XValue", "AddInt8YValue", "AddInt8ZValue",
                        "AddInt16XValue", "AddInt16YValue", "AddInt16ZValue",
                        "AddInt32XValue", "AddInt32YValue", "AddInt32ZValue",
                        "AddZValue0", "AddInt16ZValue0", "AddInt32ZValue0"):
            value = cmd.data.get("value", 0)
            if "X" in cmd.name:
                x += value
            elif "Y" in cmd.name:
                y += value
            elif "Z" in cmd.name:
                z += value
            continue

        if cmd.name == "NoOperation" or cmd.name == "UseRuntimeIDPool":
            continue

        if cmd.name == "AssignDebugData":
            continue

        # Place* 命令
        block_name = ""
        block_states = ""
        nbt_data: Any = None
        runtime_id: int | None = None

        if "block_constant_string_id" in cmd.data:
            csid = cmd.data["block_constant_string_id"]
            block_name = constant_strings.get(csid, f"<constant:{csid}>")
        if "block_states_constant_string_id" in cmd.data:
            csid = cmd.data["block_states_constant_string_id"]
            block_states = constant_strings.get(csid, "")
        if "runtime_id" in cmd.data:
            runtime_id = cmd.data["runtime_id"]
        if "nbt_data" in cmd.data:
            nbt_data = cmd.data["nbt_data"]
        if "chest_data" in cmd.data:
            nbt_data = cmd.data["chest_data"]
        if "mode" in cmd.data:
            # 命令方块数据
            nbt_data = {
                "CommandBlockData": {
                    "mode": cmd.data["mode"],
                    "command": cmd.data.get("command", ""),
                    "customName": cmd.data.get("custom_name", ""),
                    "lastOutput": cmd.data.get("last_output", ""),
                    "tickDelay": cmd.data.get("tick_delay", 0),
                    "executeOnFirstTick": cmd.data.get("execute_on_first_tick", False),
                    "trackOutput": cmd.data.get("track_output", False),
                    "conditional": cmd.data.get("conditional", False),
                    "needsRedstone": cmd.data.get("needs_redstone", False),
                }
            }

        blocks.append({
            "position": (x, y, z),
            "block_name": block_name,
            "block_states": block_states,
            "runtime_id": runtime_id,
            "nbt_data": nbt_data,
            "command_name": cmd.name,
        })

    logger.info("reconstructed %d blocks from BDX", len(blocks))
    return blocks


def get_command_statistics(result: BDXResult) -> dict[str, int]:
    """统计 BDX 文件中各命令的出现次数。

    Args:
        result: BDX 解析结果。

    Returns:
        命令名 -> 出现次数 的映射。
    """
    stats: dict[str, int] = {}
    for cmd in result.commands:
        stats[cmd.name] = stats.get(cmd.name, 0) + 1
    return stats


__all__ = [
    # 常量
    "BDX_OUTER_SIGNATURE", "BDX_INNER_SIGNATURE", "BDX_NO_SIGNATURE",
    "BDUMP_ID_TERMINATE",
    "BDUMP_COMMAND_IDS", "BDUMP_COMMAND_NAMES",
    "COMMAND_BLOCK_MODE_NAMES",
    # 异常
    "BDXError", "BDXHeaderError", "BDXUnknownCommandError", "BDXReadError",
    # 数据结构
    "BDXCommand", "BDXSignature", "BDXResult",
    # 解析函数
    "parse_bdx_bytes", "parse_bdx_file",
    # 辅助
    "reconstruct_blocks", "get_command_statistics",
]
