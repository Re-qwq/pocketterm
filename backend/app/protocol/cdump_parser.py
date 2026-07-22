"""CDump命令系统解析器。

逆向来源: NexusE cdump/command/ 系统
- NexusE v1.6.5: cdump/command/command.go
- NexusE v1.6.5: cdump/parameter.go

功能:
    - 解析CDump结构化命令 (参数化设计, 比BDump更灵活)
    - 支持20+种命令: 坐标操作, 方块放置, 数据操作, 控制流
    - cdump.Parameter系统: cdump_name, cdump_type, cdump_description
    - 支持 No_Import_bar, Unbuilder, Close_Sign 等特殊参数
    - 延迟导入块 (late_import_block) 支持
    - 将CDump命令转换为PocketTerm内部格式
"""

from __future__ import annotations

import json
import logging
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Any, ClassVar, Optional

from .blocks import BlockState

logger = logging.getLogger("pocketterm.protocol.cdump_parser")

# ----------------------------------------------------------------------
# 枚举
# ----------------------------------------------------------------------


class CDumpParameterType(IntEnum):
    """CDump参数类型枚举。

    逆向自 NexusE cdump/parameter.go:cdump_type
    """

    BOOL = 0
    """布尔类型"""

    INT = 1
    """整数类型"""

    STRING = 2
    """字符串类型"""

    BLOCK = 3
    """方块类型"""

    POS = 4
    """坐标类型"""

    NBT = 5
    """NBT数据类型"""

    FLOAT = 6
    """浮点类型"""


class CDumpCommandID(IntEnum):
    """CDump命令ID枚举。

    逆向自 NexusE cdump/command/command.go
    """

    END = 0
    """结束标记"""

    ZERO = 1
    """空操作 (Zero)"""

    X_PLUS = 10
    """X轴正方向移动"""

    X_MINUS = 11
    """X轴负方向移动"""

    Y_PLUS = 12
    """Y轴正方向移动"""

    Y_MINUS = 13
    """Y轴负方向移动"""

    Z_PLUS = 14
    """Z轴正方向移动"""

    Z_MINUS = 15
    """Z轴负方向移动"""

    X_PLUS_N = 20
    """X轴正方向移动N格"""

    X_MINUS_N = 21
    """X轴负方向移动N格"""

    Y_PLUS_N = 22
    """Y轴正方向移动N格"""

    Y_MINUS_N = 23
    """Y轴负方向移动N格"""

    Z_PLUS_N = 24
    """Z轴正方向移动N格"""

    Z_MINUS_N = 25
    """Z轴负方向移动N格"""

    PLACE_BLOCK = 30
    """放置方块"""

    PLACE_BLOCK_WITH_BLOCK_STATES = 31
    """放置带状态的方块"""

    PLACE_RUNTIME_BLOCK = 32
    """放置运行时方块"""

    PLACE_RUNTIME_BLOCK_U32 = 33
    """放置运行时方块 (Uint32 ID)"""

    CHEST_DATA = 40
    """容器数据"""

    COMMAND_BLOCK_DATA = 41
    """命令方块数据"""

    NBT_DATA = 42
    """NBT数据"""

    CREATE_CONSTANT_STRING = 50
    """创建常量字符串"""

    LATE_IMPORT_BLOCK = 60
    """延迟导入方块"""

    NO_IMPORT_BAR = 70
    """不导入屏障方块"""

    UNBUILDER = 71
    """Unbuilder模式"""

    CLOSE_SIGN = 72
    """关闭告示牌"""


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class CDumpParameter:
    """CDump参数定义。

    逆向自 NexusE cdump/parameter.go

    Attributes:
        name: 参数名称 (cdump_name)
        param_type: 参数类型 (cdump_type)
        description: 参数描述 (cdump_description)
        default_value: 默认值
        required: 是否必需
    """

    name: str
    param_type: CDumpParameterType
    description: str = ""
    default_value: Any = None
    required: bool = False


@dataclass
class CDumpCommand:
    """CDump命令基类。

    逆向自 NexusE cdump/command/command.go

    Attributes:
        command_id: 命令ID
        command_name: 命令名称
        parameters: 参数定义列表
        values: 参数值 (解析后填充)
    """

    command_id: CDumpCommandID
    command_name: str = ""
    parameters: list[CDumpParameter] = field(default_factory=list)
    values: dict[str, Any] = field(default_factory=dict)

    def get_value(self, name: str, default: Any = None) -> Any:
        """获取参数值。

        Args:
            name: 参数名称
            default: 默认值

        Returns:
            参数值。
        """
        return self.values.get(name, default)

    def has_nbt(self) -> bool:
        """检查是否有NBT数据。"""
        return self.command_id in (
            CDumpCommandID.CHEST_DATA,
            CDumpCommandID.COMMAND_BLOCK_DATA,
            CDumpCommandID.NBT_DATA,
        )

    def is_place_block(self) -> bool:
        """检查是否是放置方块命令。"""
        return self.command_id in (
            CDumpCommandID.PLACE_BLOCK,
            CDumpCommandID.PLACE_BLOCK_WITH_BLOCK_STATES,
            CDumpCommandID.PLACE_RUNTIME_BLOCK,
            CDumpCommandID.PLACE_RUNTIME_BLOCK_U32,
        )

    def is_coordinate_op(self) -> bool:
        """检查是否是坐标操作命令。"""
        return CDumpCommandID.X_PLUS <= self.command_id <= CDumpCommandID.Z_MINUS_N

    def to_block_state(self) -> Optional[BlockState]:
        """转换为BlockState (如果可能)。

        Returns:
            BlockState 或 None。
        """
        if not self.is_place_block():
            return None

        name = self.get_value("block_name", "minecraft:air")
        states_raw = self.get_value("block_states", "{}")

        if isinstance(states_raw, str):
            try:
                states = json.loads(states_raw)
            except (json.JSONDecodeError, TypeError):
                states = {}
        elif isinstance(states_raw, dict):
            states = states_raw
        else:
            states = {}

        return BlockState(name=name, states=states)


# ----------------------------------------------------------------------
# CDump 解析器
# ----------------------------------------------------------------------


class CDumpParser:
    """CDump命令系统解析器。

    逆向自 NexusE cdump/ 系统

    解析CDump结构化命令流, 支持20+种命令类型。

    使用示例::

        parser = CDumpParser()
        commands = parser.parse(data_bytes)
        for cmd in commands:
            if cmd.is_place_block():
                block = cmd.to_block_state()
                print(f"放置方块: {block}")
    """

    #: 命令注册表: 命令ID -> 命令类
    _command_registry: ClassVar[dict[int, type["_CDumpCommandImpl"]]] = {}

    def __init__(self) -> None:
        """初始化解析器。"""
        self._string_pool: list[str] = []
        """字符串池 (通过CreateConstantString命令填充)"""

        self._brush_pos: tuple[int, int, int] = (0, 0, 0)
        """画笔位置 (x, y, z)"""

        self._no_import_bar: bool = False
        """是否跳过屏障方块"""

        self._unbuilder: bool = False
        """是否启用Unbuilder模式"""

        self._close_sign: bool = False
        """是否关闭告示牌"""

        self._runtime_pool: dict[int, str] = {}
        """运行时ID池"""

    def _get_constant_string(self, index: int) -> str:
        """从字符串池获取字符串。

        Args:
            index: 字符串索引

        Returns:
            字符串内容。
        """
        if 0 <= index < len(self._string_pool):
            return self._string_pool[index]
        return ""

    def _move_brush(
        self,
        dx: int = 0,
        dy: int = 0,
        dz: int = 0,
    ) -> None:
        """移动画笔位置。

        Args:
            dx, dy, dz: 各轴增量。
        """
        x, y, z = self._brush_pos
        self._brush_pos = (x + dx, y + dy, z + dz)

    def _set_brush(self, x: int, y: int, z: int) -> None:
        """设置画笔位置。

        Args:
            x, y, z: 新位置。
        """
        self._brush_pos = (x, y, z)

    def parse(
        self,
        data: bytes,
        start_offset: int = 0,
    ) -> list[CDumpCommand]:
        """解析CDump数据流。

        CDump格式:
            每条命令: [1字节 命令ID] [变长参数]

        Args:
            data: CDump数据字节
            start_offset: 起始偏移量

        Returns:
            解析后的命令列表。

        Raises:
            ValueError: 解析失败。
        """
        offset = start_offset
        commands: list[CDumpCommand] = []
        length = len(data)

        self._string_pool = []
        self._brush_pos = (0, 0, 0)

        try:
            while offset < length:
                cmd_id = data[offset]
                offset += 1

                if cmd_id == CDumpCommandID.END:
                    break

                cmd = self._parse_command(cmd_id, data, offset)
                if cmd is None:
                    break

                offset = cmd.values.get("_next_offset", offset)
                commands.append(cmd)

                # 处理特殊效果
                self._apply_special_flags(cmd)

        except IndexError:
            logger.warning("CDump数据在偏移 %d 处意外结束", offset)
        except Exception as e:
            logger.error("CDump解析错误: %s (偏移: %d)", e, offset)
            raise ValueError(f"CDump解析失败: {e}") from e

        logger.info("CDump解析完成: %d 条命令, 字符串池: %d 个", len(commands), len(self._string_pool))
        return commands

    def _parse_command(
        self,
        cmd_id: int,
        data: bytes,
        offset: int,
    ) -> Optional[CDumpCommand]:
        """解析单条命令。

        Args:
            cmd_id: 命令ID
            data: 数据字节
            offset: 当前偏移量

        Returns:
            解析后的CDumpCommand。
        """
        # 坐标操作命令
        if cmd_id == CDumpCommandID.X_PLUS:
            self._move_brush(dx=1)
            return CDumpCommand(
                command_id=CDumpCommandID.X_PLUS,
                command_name="XPlus",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.X_MINUS:
            self._move_brush(dx=-1)
            return CDumpCommand(
                command_id=CDumpCommandID.X_MINUS,
                command_name="XMinus",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.Y_PLUS:
            self._move_brush(dy=1)
            return CDumpCommand(
                command_id=CDumpCommandID.Y_PLUS,
                command_name="YPlus",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.Y_MINUS:
            self._move_brush(dy=-1)
            return CDumpCommand(
                command_id=CDumpCommandID.Y_MINUS,
                command_name="YMinus",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.Z_PLUS:
            self._move_brush(dz=1)
            return CDumpCommand(
                command_id=CDumpCommandID.Z_PLUS,
                command_name="ZPlus",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.Z_MINUS:
            self._move_brush(dz=-1)
            return CDumpCommand(
                command_id=CDumpCommandID.Z_MINUS,
                command_name="ZMinus",
                values={"_next_offset": offset},
            )

        # N步坐标操作
        elif CDumpCommandID.X_PLUS_N <= cmd_id <= CDumpCommandID.Z_MINUS_N:
            return self._parse_move_n(cmd_id, data, offset)

        # 放置方块
        elif cmd_id == CDumpCommandID.PLACE_BLOCK:
            return self._parse_place_block(data, offset)

        elif cmd_id == CDumpCommandID.PLACE_BLOCK_WITH_BLOCK_STATES:
            return self._parse_place_block_with_states(data, offset)

        elif cmd_id == CDumpCommandID.PLACE_RUNTIME_BLOCK:
            return self._parse_place_runtime_block(data, offset)

        elif cmd_id == CDumpCommandID.PLACE_RUNTIME_BLOCK_U32:
            return self._parse_place_runtime_block_u32(data, offset)

        # 创建常量字符串
        elif cmd_id == CDumpCommandID.CREATE_CONSTANT_STRING:
            return self._parse_create_constant_string(data, offset)

        # 数据命令
        elif cmd_id == CDumpCommandID.CHEST_DATA:
            return self._parse_chest_data(data, offset)

        elif cmd_id == CDumpCommandID.COMMAND_BLOCK_DATA:
            return self._parse_command_block_data(data, offset)

        elif cmd_id == CDumpCommandID.NBT_DATA:
            return self._parse_nbt_data(data, offset)

        # 特殊标志
        elif cmd_id == CDumpCommandID.NO_IMPORT_BAR:
            self._no_import_bar = True
            return CDumpCommand(
                command_id=CDumpCommandID.NO_IMPORT_BAR,
                command_name="NoImportBar",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.UNBUILDER:
            self._unbuilder = True
            return CDumpCommand(
                command_id=CDumpCommandID.UNBUILDER,
                command_name="Unbuilder",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.CLOSE_SIGN:
            self._close_sign = True
            return CDumpCommand(
                command_id=CDumpCommandID.CLOSE_SIGN,
                command_name="CloseSign",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.ZERO:
            return CDumpCommand(
                command_id=CDumpCommandID.ZERO,
                command_name="Zero",
                values={"_next_offset": offset},
            )

        elif cmd_id == CDumpCommandID.LATE_IMPORT_BLOCK:
            return self._parse_late_import_block(data, offset)

        else:
            logger.warning("未知CDump命令ID: %d (偏移: %d)", cmd_id, offset)
            return CDumpCommand(
                command_id=CDumpCommandID(cmd_id),
                command_name=f"Unknown_{cmd_id}",
                values={"_next_offset": offset},
            )

    def _parse_move_n(
        self,
        cmd_id: int,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析N步移动命令。

        格式: [1字节 N]

        Args:
            cmd_id: 命令ID
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        if offset >= len(data):
            raise ValueError("N步移动命令: 数据不足")

        n = data[offset]
        offset += 1

        move_map = {
            CDumpCommandID.X_PLUS_N: (1, 0, 0),
            CDumpCommandID.X_MINUS_N: (-1, 0, 0),
            CDumpCommandID.Y_PLUS_N: (0, 1, 0),
            CDumpCommandID.Y_MINUS_N: (0, -1, 0),
            CDumpCommandID.Z_PLUS_N: (0, 0, 1),
            CDumpCommandID.Z_MINUS_N: (0, 0, -1),
        }

        dx, dy, dz = move_map.get(CDumpCommandID(cmd_id), (0, 0, 0))
        self._move_brush(dx=dx * n, dy=dy * n, dz=dz * n)

        name_map = {
            CDumpCommandID.X_PLUS_N: "XPlusN",
            CDumpCommandID.X_MINUS_N: "XMinusN",
            CDumpCommandID.Y_PLUS_N: "YPlusN",
            CDumpCommandID.Y_MINUS_N: "YMinusN",
            CDumpCommandID.Z_PLUS_N: "ZPlusN",
            CDumpCommandID.Z_MINUS_N: "ZMinusN",
        }

        return CDumpCommand(
            command_id=CDumpCommandID(cmd_id),
            command_name=name_map.get(CDumpCommandID(cmd_id), f"MoveN_{cmd_id}"),
            values={"n": n, "_next_offset": offset},
        )

    def _parse_place_block(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析PlaceBlock命令。

        格式: [2字节 方块名索引 (uint16)]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        if offset + 2 > len(data):
            raise ValueError("PlaceBlock: 数据不足")

        block_index = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2

        block_name = self._get_constant_string(block_index)
        x, y, z = self._brush_pos

        return CDumpCommand(
            command_id=CDumpCommandID.PLACE_BLOCK,
            command_name="PlaceBlock",
            values={
                "block_name": block_name if block_name else "minecraft:stone",
                "block_index": block_index,
                "pos": (x, y, z),
                "_next_offset": offset,
            },
        )

    def _parse_place_block_with_states(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析PlaceBlockWithBlockStates命令。

        格式: [2字节 方块名索引 (uint16)] [2字节 状态索引 (uint16)]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        if offset + 4 > len(data):
            raise ValueError("PlaceBlockWithBlockStates: 数据不足")

        block_index = struct.unpack(">H", data[offset:offset + 2])[0]
        states_index = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        offset += 4

        block_name = self._get_constant_string(block_index)
        states_str = self._get_constant_string(states_index)

        try:
            states = json.loads(states_str) if states_str else {}
        except (json.JSONDecodeError, TypeError):
            states = {}

        x, y, z = self._brush_pos

        return CDumpCommand(
            command_id=CDumpCommandID.PLACE_BLOCK_WITH_BLOCK_STATES,
            command_name="PlaceBlockWithBlockStates",
            values={
                "block_name": block_name if block_name else "minecraft:stone",
                "block_states": states,
                "block_index": block_index,
                "states_index": states_index,
                "pos": (x, y, z),
                "_next_offset": offset,
            },
        )

    def _parse_place_runtime_block(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析PlaceRuntimeBlock命令。

        格式: [2字节 运行时ID (uint16)]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        if offset + 2 > len(data):
            raise ValueError("PlaceRuntimeBlock: 数据不足")

        rtid = struct.unpack(">H", data[offset:offset + 2])[0]
        offset += 2

        block_name = self._runtime_pool.get(rtid, f"minecraft:unknown_rtid_{rtid}")
        x, y, z = self._brush_pos

        return CDumpCommand(
            command_id=CDumpCommandID.PLACE_RUNTIME_BLOCK,
            command_name="PlaceRuntimeBlock",
            values={
                "block_name": block_name,
                "runtime_id": rtid,
                "pos": (x, y, z),
                "_next_offset": offset,
            },
        )

    def _parse_place_runtime_block_u32(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析PlaceRuntimeBlockU32命令。

        格式: [4字节 运行时ID (uint32)]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        if offset + 4 > len(data):
            raise ValueError("PlaceRuntimeBlockU32: 数据不足")

        rtid = struct.unpack(">I", data[offset:offset + 4])[0]
        offset += 4

        block_name = self._runtime_pool.get(rtid, f"minecraft:unknown_rtid_{rtid}")
        x, y, z = self._brush_pos

        return CDumpCommand(
            command_id=CDumpCommandID.PLACE_RUNTIME_BLOCK_U32,
            command_name="PlaceRuntimeBlockU32",
            values={
                "block_name": block_name,
                "runtime_id": rtid,
                "pos": (x, y, z),
                "_next_offset": offset,
            },
        )

    def _parse_create_constant_string(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析CreateConstantString命令。

        格式: [以\0结尾的UTF-8字符串]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        end = data.find(b"\x00", offset)
        if end == -1:
            value = data[offset:].decode("utf-8", errors="replace")
            offset = len(data)
        else:
            value = data[offset:end].decode("utf-8", errors="replace")
            offset = end + 1

        self._string_pool.append(value)
        string_index = len(self._string_pool) - 1

        return CDumpCommand(
            command_id=CDumpCommandID.CREATE_CONSTANT_STRING,
            command_name="CreateConstantString",
            values={
                "value": value,
                "index": string_index,
                "_next_offset": offset,
            },
        )

    def _parse_chest_data(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析ChestData命令。

        格式: [变长 容器数据]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        # 简化实现: 读取剩余数据作为NBT (实际格式更复杂)
        if offset >= len(data):
            return CDumpCommand(
                command_id=CDumpCommandID.CHEST_DATA,
                command_name="ChestData",
                values={"_next_offset": offset},
            )

        x, y, z = self._brush_pos
        # 简单跳过 (实际实现需要解析NBT)
        end = data.find(b"\x00", offset)
        if end == -1:
            end = len(data)

        return CDumpCommand(
            command_id=CDumpCommandID.CHEST_DATA,
            command_name="ChestData",
            values={
                "pos": (x, y, z),
                "nbt_data": data[offset:end],
                "_next_offset": end,
            },
        )

    def _parse_command_block_data(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析CommandBlockData命令。

        格式: [变长 NBT数据]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        x, y, z = self._brush_pos
        end = data.find(b"\x00", offset)
        if end == -1:
            end = len(data)

        return CDumpCommand(
            command_id=CDumpCommandID.COMMAND_BLOCK_DATA,
            command_name="CommandBlockData",
            values={
                "pos": (x, y, z),
                "nbt_data": data[offset:end],
                "_next_offset": end,
            },
        )

    def _parse_nbt_data(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析NBTData命令。

        格式: [变长 NBT数据]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        x, y, z = self._brush_pos
        end = data.find(b"\x00", offset)
        if end == -1:
            end = len(data)

        return CDumpCommand(
            command_id=CDumpCommandID.NBT_DATA,
            command_name="NBTData",
            values={
                "pos": (x, y, z),
                "nbt_data": data[offset:end],
                "_next_offset": end,
            },
        )

    def _parse_late_import_block(
        self,
        data: bytes,
        offset: int,
    ) -> CDumpCommand:
        """解析LateImportBlock命令。

        格式: [2字节 方块名索引 (uint16)] [2字节 状态索引 (uint16)]

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            CDumpCommand。
        """
        if offset + 4 > len(data):
            return CDumpCommand(
                command_id=CDumpCommandID.LATE_IMPORT_BLOCK,
                command_name="LateImportBlock",
                values={"_next_offset": offset},
            )

        block_index = struct.unpack(">H", data[offset:offset + 2])[0]
        states_index = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        offset += 4

        block_name = self._get_constant_string(block_index)
        states_str = self._get_constant_string(states_index)

        try:
            states = json.loads(states_str) if states_str else {}
        except (json.JSONDecodeError, TypeError):
            states = {}

        x, y, z = self._brush_pos

        return CDumpCommand(
            command_id=CDumpCommandID.LATE_IMPORT_BLOCK,
            command_name="LateImportBlock",
            values={
                "block_name": block_name if block_name else "minecraft:stone",
                "block_states": states,
                "pos": (x, y, z),
                "late_import": True,
                "_next_offset": offset,
            },
        )

    def _apply_special_flags(self, cmd: CDumpCommand) -> None:
        """应用特殊标志到命令。

        Args:
            cmd: CDump命令。
        """
        if self._no_import_bar:
            cmd.values["no_import_bar"] = True
        if self._unbuilder:
            cmd.values["unbuilder"] = True
        if self._close_sign:
            cmd.values["close_sign"] = True

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def extract_blocks(
        self,
        commands: list[CDumpCommand],
    ) -> list[tuple[int, int, int, BlockState]]:
        """从命令列表中提取方块放置信息。

        Args:
            commands: CDump命令列表

        Returns:
            (x, y, z, BlockState) 元组列表。
        """
        blocks: list[tuple[int, int, int, BlockState]] = []

        for cmd in commands:
            if not cmd.is_place_block():
                continue

            pos = cmd.get_value("pos", (0, 0, 0))
            block = cmd.to_block_state()
            if block is None:
                continue

            # 跳过屏障方块
            if cmd.get_value("no_import_bar") and block.name == "minecraft:barrier":
                continue

            x, y, z = pos
            blocks.append((x, y, z, block))

        return blocks

    def extract_nbt_blocks(
        self,
        commands: list[CDumpCommand],
    ) -> list[tuple[int, int, int, BlockState, bytes]]:
        """从命令列表中提取带NBT的方块。

        Args:
            commands: CDump命令列表

        Returns:
            (x, y, z, BlockState, nbt_data) 元组列表。
        """
        nbt_blocks: list[tuple[int, int, int, BlockState, bytes]] = []

        for cmd in commands:
            if not cmd.has_nbt():
                continue

            pos = cmd.get_value("pos", (0, 0, 0))
            nbt_data = cmd.get_value("nbt_data", b"")
            block = cmd.to_block_state()

            if block is None:
                block = BlockState(name="minecraft:stone")

            x, y, z = pos
            nbt_blocks.append((x, y, z, block, nbt_data))

        return nbt_blocks

    def get_bounds(
        self,
        commands: list[CDumpCommand],
    ) -> tuple[int, int, int, int, int, int]:
        """获取命令列表中方块的包围盒。

        Args:
            commands: CDump命令列表

        Returns:
            (min_x, min_y, min_z, max_x, max_y, max_z)。
        """
        blocks = self.extract_blocks(commands)
        if not blocks:
            return (0, 0, 0, 0, 0, 0)

        min_x = min(b[0] for b in blocks)
        min_y = min(b[1] for b in blocks)
        min_z = min(b[2] for b in blocks)
        max_x = max(b[0] for b in blocks)
        max_y = max(b[1] for b in blocks)
        max_z = max(b[2] for b in blocks)

        return (min_x, min_y, min_z, max_x, max_y, max_z)


# ----------------------------------------------------------------------
# 命令实现基类 (内部使用)
# ----------------------------------------------------------------------


class _CDumpCommandImpl(ABC):
    """CDump命令实现基类 (内部使用)。

    逆向自 NexusE cdump/command/command.go
    """

    COMMAND_ID: ClassVar[int] = 0
    COMMAND_NAME: ClassVar[str] = "BaseCommand"

    @classmethod
    def command_id(cls) -> int:
        return cls.COMMAND_ID

    @classmethod
    def command_name(cls) -> str:
        return cls.COMMAND_NAME

    @abstractmethod
    def parse(self, data: bytes, offset: int) -> tuple[CDumpCommand, int]:
        """解析命令数据。

        Args:
            data: 数据字节
            offset: 当前偏移量

        Returns:
            (CDumpCommand, 下一个偏移量) 元组。
        """
        ...


__all__ = [
    "CDumpParameterType",
    "CDumpCommandID",
    "CDumpParameter",
    "CDumpCommand",
    "CDumpParser",
]