"""command_block_chain - 命令方块链构建器。

逆向自 NexusEgo v1.6.5 的 MIDI 命令方块链构建模块。
来源 Go 源码路径: NexusEgo_v1.6.5/utils/convert/midi/

逆向证据 (来自 REPORT.txt 5.3 节):
    核心函数:
        - ConvertFileToMCWorld   -- 转换为 MCWorld
        - DefaultOptions         -- 默认选项
        - ExportToMCWorld        -- 导出到 MCWorld
        - countChunkSpan         -- 计算区块跨度
        - chunkCoord             -- 区块坐标
        - commandBlockNBT        -- 命令方块 NBT
        - buildChainBlocks       -- 构建连锁命令方块
        - flattenCommands        -- 展平命令
        - buildPositions         -- 构建位置
        - applyFacing            -- 应用朝向
        - facingBetween          -- 计算朝向
        - boundsForBlocks        -- 方块边界

工作流程:
    1. Timeline + NoteSound 列表 -> 命令列表
    2. flattenCommands: 展平嵌套命令
    3. buildPositions: 计算每个命令方块的位置
    4. applyFacing: 应用朝向 (连锁方向)
    5. buildChainBlocks: 构建连锁命令方块链
    6. commandBlockNBT: 生成命令方块 NBT
    7. ExportToMCWorld: 导出为 MCWorld 文件

命令方块类型:
    - 脉冲 (Impulse):  独立执行, 需红石激活
    - 连锁 (Chain):    连接前一个命令方块, 条件执行
    - 循环 (Repeat):   循环执行
"""

from __future__ import annotations

import io
import json
import logging
import struct
import zipfile
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Iterator

# 导入同模块的类型
try:
    from .midi_parser import Song, Timeline, NoteEvent
    from .note_mapper import NoteSound, ConvertOptions, DEFAULT_OPTIONS
except ImportError:
    from midi_parser import Song, Timeline, NoteEvent  # type: ignore
    from note_mapper import NoteSound, ConvertOptions, DEFAULT_OPTIONS  # type: ignore

logger = logging.getLogger("pocketterm.protocol.midi_converter.command_block_chain")


# ======================================================================
# 常量
# ======================================================================

#: 区块尺寸 (16x16)
CHUNK_SIZE: int = 16

#: 子区块尺寸 (16x16x16)
SUBCHUNK_SIZE: int = 16

#: 命令方块方块名 (脉冲)
IMPULSE_COMMAND_BLOCK: str = "minecraft:command_block"

#: 命令方块方块名 (连锁)
CHAIN_COMMAND_BLOCK: str = "minecraft:chain_command_block"

#: 命令方块方块名 (循环)
REPEATING_COMMAND_BLOCK: str = "minecraft:repeating_command_block"

#: 红石方块名
REDSTONE_BLOCK: str = "minecraft:redstone_block"

#: 方块实体 ID (命令方块)
COMMAND_BLOCK_ENTITY_ID: str = "CommandBlock"

#: 默认命令方块朝向
DEFAULT_FACING: str = "south"

#: 连锁命令方块最大链长度 (实际无限制, 但建议不超过 65536)
MAX_CHAIN_LENGTH: int = 65536

#: MCWorld 中的 level.dat 生成版本
MCWORLD_LEVEL_VERSION: int = 19133

#: 默认每 tick 命令数
DEFAULT_COMMANDS_PER_TICK: int = 1


# ======================================================================
# 异常
# ======================================================================


class CommandBlockError(Exception):
    """命令方块构建错误的基类。"""


class ChainTooLongError(CommandBlockError):
    """命令方块链过长。"""

    def __init__(self, length: int) -> None:
        self.length = length
        super().__init__(
            f"chain length {length} exceeds maximum {MAX_CHAIN_LENGTH}"
        )


class InvalidFacingError(CommandBlockError):
    """无效的朝向。"""


# ======================================================================
# 枚举
# ======================================================================


class CommandBlockType(IntEnum):
    """命令方块类型。

    Attributes:
        IMPULSE:  脉冲命令方块 (mode=0)
        CHAIN:    连锁命令方块 (mode=1)
        REPEAT:   循环命令方块 (mode=2)
    """

    IMPULSE = 0
    CHAIN = 1
    REPEAT = 2

    @property
    def block_name(self) -> str:
        """获取方块名。"""
        names = [
            IMPULSE_COMMAND_BLOCK,
            CHAIN_COMMAND_BLOCK,
            REPEATING_COMMAND_BLOCK,
        ]
        return names[self.value]


class Facing(IntEnum):
    """朝向 (6 个方向 + 朝向观察者)。

    对应 Minecraft 方块 facing_direction 状态。

    Attributes:
        DOWN:  朝下
        UP:    朝上
        NORTH: 朝北
        SOUTH: 朝南
        WEST:  朝西
        EAST:  朝东
    """

    DOWN = 0
    UP = 1
    NORTH = 2
    SOUTH = 3
    WEST = 4
    EAST = 5

    def to_name(self) -> str:
        """转换为名称。"""
        names = ["down", "up", "north", "south", "west", "east"]
        return names[self.value]

    def to_delta(self) -> tuple[int, int, int]:
        """转换为方向增量。"""
        deltas = [
            (0, -1, 0),  # DOWN
            (0, 1, 0),   # UP
            (0, 0, -1),  # NORTH
            (0, 0, 1),   # SOUTH
            (-1, 0, 0),  # WEST
            (1, 0, 0),   # EAST
        ]
        return deltas[self.value]

    @classmethod
    def from_name(cls, name: str) -> "Facing":
        """从名称构建。"""
        mapping = {
            "down": cls.DOWN, "up": cls.UP,
            "north": cls.NORTH, "south": cls.SOUTH,
            "west": cls.WEST, "east": cls.EAST,
        }
        key = name.lower().strip()
        if key not in mapping:
            raise InvalidFacingError(f"invalid facing name: {name!r}")
        return mapping[key]


# ======================================================================
# 数据类 - CommandBlock
# ======================================================================


@dataclass
class CommandBlock:
    """命令方块 (CommandBlock)。

    表示一个命令方块的完整数据。

    Attributes:
        position: 方块位置 (x, y, z)。
        type: 命令方块类型 (IMPULSE/CHAIN/REPEAT)。
        facing: 朝向。
        command: 命令字符串。
        custom_name: 自定义名称。
        last_output: 上次输出。
        tick_delay: 刻延迟。
        execute_on_first_tick: 是否第一刻执行。
        track_output: 是否跟踪输出。
        conditional: 是否条件模式。
        needs_redstone: 是否需要红石。
        auto: 是否自动执行 (不需要红石)。
    """

    position: tuple[int, int, int] = (0, 0, 0)
    type: CommandBlockType = CommandBlockType.IMPULSE
    facing: Facing = Facing.SOUTH
    command: str = ""
    custom_name: str = ""
    last_output: str = ""
    tick_delay: int = 0
    execute_on_first_tick: bool = True
    track_output: bool = False
    conditional: bool = False
    needs_redstone: bool = False
    auto: bool = True

    @property
    def block_name(self) -> str:
        """方块名。"""
        return self.type.block_name

    def to_block_state(self) -> dict[str, Any]:
        """转换为方块状态。"""
        return {
            "name": self.block_name,
            "states": {
                "facing_direction": int(self.facing),
                "conditional_bit": self.conditional,
            },
        }

    def to_nbt(self) -> dict[str, Any]:
        """转换为方块实体 NBT (commandBlockNBT)。

        逆向自 NexusEgo_v1.6.5/utils/convert/midi/ 的 commandBlockNBT 函数。
        """
        x, y, z = self.position
        return {
            "id": COMMAND_BLOCK_ENTITY_ID,
            "x": x,
            "y": y,
            "z": z,
            "Command": self.command,
            "CustomName": self.custom_name,
            "LastOutput": self.last_output,
            "TickDelay": self.tick_delay,
            "ExecuteOnFirstTick": self.execute_on_first_tick,
            "TrackOutput": self.track_output,
            "conditionalMode": 1 if self.conditional else 0,
            "poweredMode": 0 if self.needs_redstone else 1,
            "autoMode": 1 if self.auto else 0,
            "isMovable": True,
        }

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "position": list(self.position),
            "type": self.type.name,
            "facing": self.facing.to_name(),
            "command": self.command,
            "custom_name": self.custom_name,
            "tick_delay": self.tick_delay,
            "conditional": self.conditional,
            "needs_redstone": self.needs_redstone,
            "auto": self.auto,
        }


# ======================================================================
# 数据类 - CommandBlockNBT (别名)
# ======================================================================

#: 命令方块 NBT (commandBlockNBT 的输出类型)
CommandBlockNBT = dict[str, Any]


# ======================================================================
# 数据类 - CommandBlockChain
# ======================================================================


@dataclass
class CommandBlockChain:
    """命令方块链 (CommandBlockChain)。

    表示一个连锁命令方块链:
        - 第一个方块为脉冲或循环 (触发器)
        - 后续方块为连锁 (Chain)
        - 通过 facing 连接

    Attributes:
        blocks: 命令方块列表 (按链顺序)。
        origin: 链起始位置。
        direction: 链方向。
    """

    blocks: list[CommandBlock] = field(default_factory=list)
    origin: tuple[int, int, int] = (0, 0, 0)
    direction: Facing = Facing.SOUTH

    @property
    def length(self) -> int:
        """链长度。"""
        return len(self.blocks)

    @property
    def is_empty(self) -> bool:
        """是否为空链。"""
        return not self.blocks

    def add_block(self, command: str, **kwargs: Any) -> CommandBlock:
        """添加一个命令方块到链尾。

        Args:
            command: 命令字符串。
            **kwargs: 其他 CommandBlock 参数。

        Returns:
            添加的 CommandBlock。
        """
        if self.length >= MAX_CHAIN_LENGTH:
            raise ChainTooLongError(self.length)

        index = self.length
        delta = self.direction.to_delta()
        pos = (
            self.origin[0] + delta[0] * index,
            self.origin[1] + delta[1] * index,
            self.origin[2] + delta[2] * index,
        )

        block_type = kwargs.pop("type", None)
        if block_type is None:
            block_type = CommandBlockType.IMPULSE if index == 0 else CommandBlockType.CHAIN

        block = CommandBlock(
            position=pos,
            type=block_type,
            facing=self.direction,
            command=command,
            **kwargs,
        )
        self.blocks.append(block)
        return block

    def to_nbt_list(self) -> list[CommandBlockNBT]:
        """转换为 NBT 列表。"""
        return [block.to_nbt() for block in self.blocks]

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "length": self.length,
            "origin": list(self.origin),
            "direction": self.direction.to_name(),
            "blocks": [block.to_dict() for block in self.blocks],
        }


# ======================================================================
# 区块相关函数 (countChunkSpan / chunkCoord)
# ======================================================================


def chunk_coord(block_coord: int) -> int:
    """区块坐标 (chunkCoord)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    将方块坐标转换为区块坐标。

    Args:
        block_coord: 方块坐标。

    Returns:
        区块坐标 (block_coord // CHUNK_SIZE)。
    """
    return block_coord // CHUNK_SIZE


def count_chunk_span(
    start: tuple[int, int, int],
    end: tuple[int, int, int],
) -> tuple[int, int, int]:
    """计算区块跨度 (countChunkSpan)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    计算从 start 到 end 跨越的区块数 (每个轴)。

    Args:
        start: 起始方块坐标。
        end: 结束方块坐标。

    Returns:
        (x_span, y_span, z_span) 区块跨度。
    """
    x_span = abs(chunk_coord(end[0]) - chunk_coord(start[0])) + 1
    z_span = abs(chunk_coord(end[2]) - chunk_coord(start[2])) + 1
    # Y 轴按子区块计算
    y_span = abs(end[1] // SUBCHUNK_SIZE - start[1] // SUBCHUNK_SIZE) + 1
    return (x_span, y_span, z_span)


# ======================================================================
# 朝向函数 (facingBetween / applyFacing)
# ======================================================================


def facing_between(
    pos1: tuple[int, int, int],
    pos2: tuple[int, int, int],
) -> Facing:
    """计算朝向 (facingBetween)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    计算从 pos1 指向 pos2 的朝向。

    Args:
        pos1: 起始位置。
        pos2: 目标位置。

    Returns:
        Facing 朝向。

    Raises:
        InvalidFacingError: 两个位置相同或对角。
    """
    dx = pos2[0] - pos1[0]
    dy = pos2[1] - pos1[1]
    dz = pos2[2] - pos1[2]

    if dx > 0 and dy == 0 and dz == 0:
        return Facing.EAST
    elif dx < 0 and dy == 0 and dz == 0:
        return Facing.WEST
    elif dy > 0 and dx == 0 and dz == 0:
        return Facing.UP
    elif dy < 0 and dx == 0 and dz == 0:
        return Facing.DOWN
    elif dz > 0 and dx == 0 and dy == 0:
        return Facing.SOUTH
    elif dz < 0 and dx == 0 and dy == 0:
        return Facing.NORTH
    else:
        if dx == 0 and dy == 0 and dz == 0:
            raise InvalidFacingError(
                f"positions are the same: {pos1} == {pos2}"
            )
        # 对角情况, 优先 Z 轴
        if abs(dz) >= abs(dx) and abs(dz) >= abs(dy):
            return Facing.SOUTH if dz > 0 else Facing.NORTH
        elif abs(dx) >= abs(dy):
            return Facing.EAST if dx > 0 else Facing.WEST
        else:
            return Facing.UP if dy > 0 else Facing.DOWN


def apply_facing(
    blocks: list[CommandBlock],
    direction: Facing,
) -> list[CommandBlock]:
    """应用朝向 (applyFacing)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    将所有命令方块的朝向设置为指定方向, 并调整位置形成链。

    Args:
        blocks: 命令方块列表。
        direction: 链方向。

    Returns:
        调整后的命令方块列表。
    """
    if not blocks:
        return blocks

    delta = direction.to_delta()
    origin = blocks[0].position

    for i, block in enumerate(blocks):
        # 调整位置
        block.position = (
            origin[0] + delta[0] * i,
            origin[1] + delta[1] * i,
            origin[2] + delta[2] * i,
        )
        # 设置朝向
        block.facing = direction
        # 第一个为脉冲/循环, 后续为连锁
        if i == 0:
            if block.type == CommandBlockType.CHAIN:
                block.type = CommandBlockType.IMPULSE
        else:
            block.type = CommandBlockType.CHAIN

    logger.debug(
        "apply_facing: %d blocks, direction=%s",
        len(blocks), direction.to_name(),
    )
    return blocks


# ======================================================================
# 位置函数 (buildPositions / boundsForBlocks)
# ======================================================================


def build_positions(
    count: int,
    origin: tuple[int, int, int] = (0, 0, 0),
    direction: Facing = Facing.SOUTH,
) -> list[tuple[int, int, int]]:
    """构建位置 (buildPositions)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    按 direction 方向生成 count 个连续位置。

    Args:
        count: 位置数量。
        origin: 起始位置。
        direction: 方向。

    Returns:
        位置列表。
    """
    delta = direction.to_delta()
    positions: list[tuple[int, int, int]] = []
    for i in range(count):
        positions.append((
            origin[0] + delta[0] * i,
            origin[1] + delta[1] * i,
            origin[2] + delta[2] * i,
        ))
    return positions


def bounds_for_blocks(
    blocks: list[CommandBlock],
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """方块边界 (boundsForBlocks)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    计算命令方块列表的最小/最大坐标。

    Args:
        blocks: 命令方块列表。

    Returns:
        ((min_x, min_y, min_z), (max_x, max_y, max_z))。
    """
    if not blocks:
        return ((0, 0, 0), (0, 0, 0))

    xs = [b.position[0] for b in blocks]
    ys = [b.position[1] for b in blocks]
    zs = [b.position[2] for b in blocks]

    return (
        (min(xs), min(ys), min(zs)),
        (max(xs), max(ys), max(zs)),
    )


# ======================================================================
# 展平函数 (flattenCommands)
# ======================================================================


def flatten_commands(
    commands: list[str | list[str]],
) -> list[str]:
    """展平命令 (flattenCommands)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    将嵌套的命令列表展平为一维列表。

    Args:
        commands: 命令列表 (可能包含嵌套列表)。

    Returns:
        展平后的命令列表。
    """
    result: list[str] = []

    def _flatten(items: list[str | list[str]]) -> None:
        for item in items:
            if isinstance(item, list):
                _flatten(item)
            else:
                result.append(str(item))

    _flatten(commands)
    logger.debug(
        "flatten_commands: %d items -> %d commands",
        len(commands), len(result),
    )
    return result


# ======================================================================
# 命令方块链构建 (buildChainBlocks)
# ======================================================================


def build_chain_blocks(
    commands: list[str],
    origin: tuple[int, int, int] = (0, 0, 0),
    direction: Facing = Facing.SOUTH,
    conditional: bool = False,
    needs_redstone: bool = False,
) -> CommandBlockChain:
    """构建连锁命令方块 (buildChainBlocks)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    将命令列表转换为连锁命令方块链。

    Args:
        commands: 命令字符串列表。
        origin: 链起始位置。
        direction: 链方向。
        conditional: 是否条件模式 (仅第二个及以后生效)。
        needs_redstone: 是否需要红石 (仅第一个生效)。

    Returns:
        CommandBlockChain。
    """
    if len(commands) > MAX_CHAIN_LENGTH:
        raise ChainTooLongError(len(commands))

    chain = CommandBlockChain(origin=origin, direction=direction)
    delta = direction.to_delta()

    for i, cmd in enumerate(commands):
        pos = (
            origin[0] + delta[0] * i,
            origin[1] + delta[1] * i,
            origin[2] + delta[2] * i,
        )

        if i == 0:
            # 第一个方块: 脉冲命令方块
            block = CommandBlock(
                position=pos,
                type=CommandBlockType.IMPULSE,
                facing=direction,
                command=cmd,
                conditional=False,
                needs_redstone=needs_redstone,
                auto=not needs_redstone,
            )
        else:
            # 后续方块: 连锁命令方块
            block = CommandBlock(
                position=pos,
                type=CommandBlockType.CHAIN,
                facing=direction,
                command=cmd,
                conditional=conditional,
                needs_redstone=False,
                auto=True,
            )

        chain.blocks.append(block)

    logger.info(
        "build_chain_blocks: %d commands, origin=%s, direction=%s",
        len(commands), origin, direction.to_name(),
    )
    return chain


# ======================================================================
# MCWorld 导出 (ConvertFileToMCWorld / ExportToMCWorld)
# ======================================================================


def export_to_mcworld(
    chains: list[CommandBlockChain],
    output_path: str,
    level_name: str = "MIDI_Conversion",
) -> str:
    """导出到 MCWorld (ExportToMCWorld)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    将命令方块链列表导出为 Minecraft World (.mcworld) 文件。
    .mcworld 实际上是 ZIP 压缩包, 包含 level.dat 和 db/ 目录。

    Args:
        chains: 命令方块链列表。
        output_path: 输出文件路径 (.mcworld)。
        level_name: 关卡名称。

    Returns:
        实际输出路径。
    """
    logger.info(
        "export_to_mcworld: %d chains -> %s",
        len(chains), output_path,
    )

    # 收集所有命令方块
    all_blocks: list[CommandBlock] = []
    for chain in chains:
        all_blocks.extend(chain.blocks)

    if not all_blocks:
        logger.warning("export_to_mcworld: no blocks to export")

    # 计算边界
    min_pos, max_pos = bounds_for_blocks(all_blocks)
    logger.debug(
        "export_to_mcworld: bounds min=%s max=%s", min_pos, max_pos
    )

    # 生成 level.dat (简化版 NBT)
    level_dat = _generate_level_dat(
        level_name=level_name,
        spawn=(min_pos[0], min_pos[1] + 10, min_pos[2]),
    )

    # 生成命令方块 NBT
    block_nbt_list = [block.to_nbt() for block in all_blocks]

    # 写入 ZIP (MCWorld)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # level.dat
        zf.writestr("level.dat", level_dat)
        # 命令方块数据 (JSON 格式, 实际应为 NBT)
        zf.writestr(
            "command_blocks.json",
            json.dumps(block_nbt_list, indent=2, ensure_ascii=False),
        )
        # db/ 目录 (空 LevelDB)
        zf.writestr("db/", "")
        # world_icon.jpeg (占位)
        zf.writestr("world_icon.jpeg", b"")

    logger.info(
        "export_to_mcworld: written %d blocks to %s",
        len(all_blocks), output_path,
    )
    return output_path


def convert_file_to_mcworld(
    midi_path: str,
    output_path: str,
    options: ConvertOptions | None = None,
    origin: tuple[int, int, int] = (0, 64, 0),
    direction: Facing = Facing.SOUTH,
) -> str:
    """转换文件到 MCWorld (ConvertFileToMCWorld)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    完整的 MIDI -> MCWorld 转换流程:
        1. 解析 MIDI 文件
        2. 构建时间线
        3. 映射音符到声音
        4. 生成 playsound 命令
        5. 构建命令方块链
        6. 导出 MCWorld

    Args:
        midi_path: MIDI 文件路径。
        output_path: 输出 MCWorld 路径。
        options: 转换选项。
        origin: 命令方块链起始位置。
        direction: 链方向。

    Returns:
        输出文件路径。
    """
    # 延迟导入以避免循环依赖
    from .midi_parser import parse_midi_file, build_timeline
    from .note_mapper import NoteMapper

    options = options or DEFAULT_OPTIONS
    logger.info(
        "convert_file_to_mcworld: %s -> %s", midi_path, output_path
    )

    # 1. 解析 MIDI
    song = parse_midi_file(midi_path)
    logger.info("parsed MIDI: %d tracks", song.track_count)

    # 2. 构建时间线
    timeline = build_timeline(song)
    logger.info("timeline: %d events", len(timeline.events))

    # 3. 映射音符
    mapper = NoteMapper(options=options)
    track_programs = {}
    for track in song.tracks:
        if track.program_changes:
            track_programs[track.index] = track.program_changes[0][1]

    mapped = mapper.map_events(timeline.events, track_programs)
    logger.info("mapped: %d sounds", len(mapped))

    # 4. 生成 playsound 命令
    commands: list[str] = []
    for event, sound in mapped:
        cmd = sound.to_play_command()
        commands.append(cmd)

    # 展平 (以防有嵌套)
    commands = flatten_commands(commands)
    logger.info("commands: %d", len(commands))

    # 5. 构建命令方块链 (按 tick 分组)
    chains: list[CommandBlockChain] = []
    # 简化: 所有命令放一个链
    if commands:
        chain = build_chain_blocks(
            commands=commands,
            origin=origin,
            direction=direction,
        )
        chains.append(chain)

    # 6. 导出 MCWorld
    return export_to_mcworld(chains, output_path, level_name=song.tracks[0].name if song.tracks else "MIDI")


def _generate_level_dat(
    level_name: str = "MIDI_Conversion",
    spawn: tuple[int, int, int] = (0, 64, 0),
) -> bytes:
    """生成 level.dat (简化版)。

    实际 MCWorld 的 level.dat 是 GZIP 压缩的 NBT 数据。
    这里生成一个简化的二进制 NBT。

    Args:
        level_name: 关卡名称。
        spawn: 出生点。

    Returns:
        level.dat 字节数据。
    """
    # 构建一个简单的 NBT compound
    # 实际应使用完整的 NBT 编码器
    nbt_data = {
        "LevelName": level_name,
        "SpawnX": spawn[0],
        "SpawnY": spawn[1],
        "SpawnZ": spawn[2],
        "Version": MCWORLD_LEVEL_VERSION,
        "FlatWorldLayers": "",
        "ForceGameType": False,
        "GameType": 1,  # Creative
        "Difficulty": 2,
        "Time": 0,
        "DayTime": 0,
        "Generator": 2,  # Flat
        "RandomSeed": 0,
    }

    # 简化: 使用 JSON 作为占位 (实际应为 NBT)
    # 加上 NBT 头标记
    json_bytes = json.dumps(nbt_data, ensure_ascii=False).encode("utf-8")
    # 实际应输出 GZIP 压缩的 NBT, 这里用 JSON + 标记
    return b"NEXUSEGO_LEVEL_DAT_V1\x00" + json_bytes


# ======================================================================
# __all__
# ======================================================================

__all__ = [
    # 常量
    "CHUNK_SIZE", "SUBCHUNK_SIZE",
    "IMPULSE_COMMAND_BLOCK", "CHAIN_COMMAND_BLOCK", "REPEATING_COMMAND_BLOCK",
    "REDSTONE_BLOCK", "COMMAND_BLOCK_ENTITY_ID",
    "DEFAULT_FACING", "MAX_CHAIN_LENGTH", "MCWORLD_LEVEL_VERSION",
    "DEFAULT_COMMANDS_PER_TICK",
    # 异常
    "CommandBlockError", "ChainTooLongError", "InvalidFacingError",
    # 枚举
    "CommandBlockType", "Facing",
    # 数据类
    "CommandBlock", "CommandBlockNBT", "CommandBlockChain",
    # 函数
    "chunk_coord", "count_chunk_span",
    "facing_between", "apply_facing",
    "build_positions", "bounds_for_blocks",
    "flatten_commands", "build_chain_blocks",
    "export_to_mcworld", "convert_file_to_mcworld",
]
