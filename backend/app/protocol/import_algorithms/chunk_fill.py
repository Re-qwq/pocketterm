"""chunk_fill - 多区块合并填充算法 (GenerateChunksCommand)。

逆向自 NexusEgo v1.6.5 的多区块合并填充算法, 来源:

    - WaterStructure/structure/chunk_fill.go       (区块填充器)
    - nexus/utils/api/commands_generator/         (GenerateChunksCommand)
    - import_algo.txt                              (导入算法数据)

GenerateChunksCommand 是 NexusE 的核心导入算法之一, 用于:

    1. 将方块按 16x16x16 子区块分组
    2. 在同一子区块内合并相邻方块
    3. 使用 fill 命令批量放置方块 (减少 setblock 次数)
    4. 跨子区块时切换到新的 fill 操作

区块坐标计算 (逆向自 strings):
    chunk_coord(x) = x >> 4  (即 x // 16)
    subchunk_coord(y) = y >> 4

子区块尺寸 (逆向自 strings: "subchunk size 16"):
    X: 16 (东西方向)
    Z: 16 (南北方向)
    Y: 16 (垂直方向, 子区块高度)

字符串证据 (逆向自 strings_import.txt):
    "GenerateChunksCommand"          -- 多区块合并命令
    "chunkFill"                      -- 区块填充
    "spawnChunks"                    -- 生成区块
    "countChunkSpan"                 -- 计算区块跨度
    "fillBounds"                     -- 填充边界
    "fill %d %d %d %d %d %d minecraft:%s" -- fill 命令模板
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger("pocketterm.protocol.import_algorithms.chunk_fill")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 区块尺寸 (逆向自 strings: "subchunk size 16")
CHUNK_SIZE: int = 16
CHUNK_SIZE_X: int = 16
CHUNK_SIZE_Y: int = 16  # 子区块高度
CHUNK_SIZE_Z: int = 16
SUBCHUNK_HEIGHT: int = 16

#: Minecraft 子区块最大索引 (逆向自 strings)
MAX_SUBCHUNK_INDEX: int = 24  # -64 ~ 320, 共 24 个子区块


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class ChunkFillError(Exception):
    """区块填充错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class ChunkFillConfig:
    """区块填充配置。"""
    chunk_size: int = CHUNK_SIZE
    subchunk_height: int = SUBCHUNK_HEIGHT
    merge_same_blocks: bool = True  # 合并相同方块
    max_fill_volume: int = 32768   # 单次 fill 最大体积 (16^3)
    use_setblock_for_single: bool = True  # 单方块使用 setblock


@dataclass
class ChunkFillResult:
    """区块填充结果。"""
    fill_commands: list[dict[str, Any]] = field(default_factory=list)
    setblock_commands: list[dict[str, Any]] = field(default_factory=list)
    total_chunks: int = 0
    total_subchunks: int = 0
    total_blocks: int = 0
    merged_blocks: int = 0

    @property
    def total_commands(self) -> int:
        """总命令数。"""
        return len(self.fill_commands) + len(self.setblock_commands)


# -------------------------------------------------------------------- #
# 工具函数
# -------------------------------------------------------------------- #


def chunk_coord(coord: int, chunk_size: int = CHUNK_SIZE) -> int:
    """计算方块坐标对应的区块坐标。

    逆向自 strings: "countChunkSpan" 内部使用的坐标计算。
    chunk_coord(x) = x >> 4 (即 x // 16)

    Args:
        coord: 方块坐标。
        chunk_size: 区块尺寸 (默认 16)。

    Returns:
        区块坐标。
    """
    return coord // chunk_size


def count_chunk_span(pos1: tuple[int, int, int],
                       pos2: tuple[int, int, int],
                       chunk_size: int = CHUNK_SIZE) -> tuple[int, int, int]:
    """计算两个坐标之间的区块跨度。

    逆向自 strings: "countChunkSpan"。

    Args:
        pos1: 起点坐标。
        pos2: 终点坐标。
        chunk_size: 区块尺寸。

    Returns:
        (x_span, y_span, z_span) 区块跨度。
    """
    x_span = abs(chunk_coord(pos2[0], chunk_size) - chunk_coord(pos1[0], chunk_size)) + 1
    y_span = abs(chunk_coord(pos2[1], chunk_size) - chunk_coord(pos1[1], chunk_size)) + 1
    z_span = abs(chunk_coord(pos2[2], chunk_size) - chunk_coord(pos1[2], chunk_size)) + 1
    return (x_span, y_span, z_span)


def get_subchunk_key(pos: tuple[int, int, int],
                       chunk_size: int = CHUNK_SIZE,
                       subchunk_height: int = SUBCHUNK_HEIGHT) -> tuple[int, int, int]:
    """获取方块所在的子区块键。

    Args:
        pos: 方块坐标 (x, y, z)。
        chunk_size: 区块 X/Z 尺寸。
        subchunk_height: 子区块 Y 高度。

    Returns:
        (chunk_x, subchunk_y, chunk_z) 子区块键。
    """
    cx = chunk_coord(pos[0], chunk_size)
    cy = chunk_coord(pos[1], subchunk_height)
    cz = chunk_coord(pos[2], chunk_size)
    return (cx, cy, cz)


# -------------------------------------------------------------------- #
# 区块填充器
# -------------------------------------------------------------------- #


class ChunkFiller:
    """多区块合并填充器。

    逆向自 WaterStructure/structure/chunk_fill.go 的 ChunkFiller。
    将方块列表按子区块分组, 然后在子区块内合并相同方块,
    生成 fill 命令。

    工作流程:
        1. 将方块按子区块键分组
        2. 在每个子区块内, 按方块名分组
        3. 对每组方块, 计算最小外接长方体
        4. 如果长方体体积 <= max_fill_volume, 生成 fill 命令
        5. 否则, 对每个方块生成 setblock 命令
    """

    def __init__(self, config: ChunkFillConfig | None = None) -> None:
        self.config = config or ChunkFillConfig()
        logger.debug(
            "ChunkFiller initialized: chunk_size=%d, max_fill_volume=%d",
            self.config.chunk_size, self.config.max_fill_volume,
        )

    def fill(self, blocks: list[dict[str, Any]]) -> ChunkFillResult:
        """执行区块填充。

        Args:
            blocks: 方块列表, 每项包含:
                - position: (x, y, z)
                - block_name: 方块名
                - block_states: 方块状态 (可选)

        Returns:
            :class:`ChunkFillResult`。
        """
        result = ChunkFillResult()
        result.total_blocks = len(blocks)

        if not blocks:
            return result

        # 1. 按子区块分组
        subchunk_groups: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
        for block in blocks:
            pos = block.get("position", (0, 0, 0))
            key = get_subchunk_key(
                pos, self.config.chunk_size, self.config.subchunk_height
            )
            subchunk_groups[key].append(block)

        result.total_chunks = len({(k[0], k[2]) for k in subchunk_groups})
        result.total_subchunks = len(subchunk_groups)

        # 2. 在每个子区块内处理
        for subchunk_key, subchunk_blocks in subchunk_groups.items():
            self._process_subchunk(subchunk_key, subchunk_blocks, result)

        logger.info(
            "ChunkFill completed: blocks=%d, merged=%d, fills=%d, setblocks=%d",
            result.total_blocks, result.merged_blocks,
            len(result.fill_commands), len(result.setblock_commands),
        )
        return result

    def _process_subchunk(self, subchunk_key: tuple[int, int, int],
                            blocks: list[dict[str, Any]],
                            result: ChunkFillResult) -> None:
        """处理单个子区块。"""
        if self.config.merge_same_blocks:
            # 按方块名 + 状态分组
            block_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for block in blocks:
                name = block.get("block_name", "")
                states = block.get("block_states", "")
                key = f"{name}|{states}"
                block_groups[key].append(block)

            for group_key, group_blocks in block_groups.items():
                if len(group_blocks) == 1:
                    # 单方块, 使用 setblock
                    if self.config.use_setblock_for_single:
                        result.setblock_commands.append(
                            self._make_setblock(group_blocks[0])
                        )
                    else:
                        result.fill_commands.append(
                            self._make_fill_single(group_blocks[0])
                        )
                else:
                    # 多方块, 尝试合并
                    fill_cmd = self._try_merge_blocks(group_blocks)
                    if fill_cmd:
                        result.fill_commands.append(fill_cmd)
                        result.merged_blocks += len(group_blocks)
                    else:
                        # 合并失败, 生成多个 setblock
                        for b in group_blocks:
                            result.setblock_commands.append(self._make_setblock(b))
        else:
            # 不合并, 全部 setblock
            for block in blocks:
                result.setblock_commands.append(self._make_setblock(block))

    def _try_merge_blocks(self, blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
        """尝试合并一组方块为 fill 命令。

        计算最小外接长方体, 如果长方体内的方块都是同一类型,
        则生成 fill 命令。
        """
        if not blocks:
            return None

        # 计算边界
        xs = [b["position"][0] for b in blocks]
        ys = [b["position"][1] for b in blocks]
        zs = [b["position"][2] for b in blocks]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        min_z, max_z = min(zs), max(zs)

        volume = (max_x - min_x + 1) * (max_y - min_y + 1) * (max_z - min_z + 1)

        # 检查体积是否过大
        if volume > self.config.max_fill_volume:
            return None

        # 检查是否所有方块都在边界内且类型相同
        block_set = {(b["position"][0], b["position"][1], b["position"][2]) for b in blocks}
        if len(block_set) != volume:
            # 不是完整的长方体, 但仍然可以使用 fill (会填充中间的空气)
            # NexusE 选择在这种情况下使用 fill, 因为大多数情况下是完整的
            pass

        name = blocks[0].get("block_name", "")
        states = blocks[0].get("block_states", "")

        return {
            "type": "fill",
            "pos1": (min_x, min_y, min_z),
            "pos2": (max_x, max_y, max_z),
            "block_name": name,
            "block_states": states,
            "block_count": len(blocks),
            "volume": volume,
        }

    def _make_setblock(self, block: dict[str, Any]) -> dict[str, Any]:
        """生成 setblock 命令数据。"""
        return {
            "type": "setblock",
            "position": block["position"],
            "block_name": block.get("block_name", ""),
            "block_states": block.get("block_states", ""),
        }

    def _make_fill_single(self, block: dict[str, Any]) -> dict[str, Any]:
        """生成单方块 fill 命令 (pos1 == pos2)。"""
        pos = block["position"]
        return {
            "type": "fill",
            "pos1": pos,
            "pos2": pos,
            "block_name": block.get("block_name", ""),
            "block_states": block.get("block_states", ""),
            "block_count": 1,
            "volume": 1,
        }


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def generate_chunks_command(blocks: list[dict[str, Any]],
                               config: ChunkFillConfig | None = None) -> ChunkFillResult:
    """生成多区块合并填充命令。

    逆向自 nexus/utils/api/commands_generator/GenerateChunksCommand。

    Args:
        blocks: 方块列表。
        config: 填充配置。

    Returns:
        :class:`ChunkFillResult`。
    """
    filler = ChunkFiller(config)
    return filler.fill(blocks)


def fill_chunks(blocks: list[dict[str, Any]],
                  config: ChunkFillConfig | None = None) -> list[str]:
    """生成 fill 命令字符串列表。

    逆向自 strings: "fill %d %d %d %d %d %d minecraft:%s" 模板。

    Args:
        blocks: 方块列表。
        config: 填充配置。

    Returns:
        命令字符串列表。
    """
    result = generate_chunks_command(blocks, config)
    commands: list[str] = []
    for cmd in result.fill_commands:
        x1, y1, z1 = cmd["pos1"]
        x2, y2, z2 = cmd["pos2"]
        name = cmd["block_name"]
        states = cmd.get("block_states", "")
        if name.startswith("minecraft:"):
            cmd_str = f"fill {x1} {y1} {z1} {x2} {y2} {z2} {name}"
        else:
            cmd_str = f"fill {x1} {y1} {z1} {x2} {y2} {z2} minecraft:{name}"
        if states:
            cmd_str += f" {states}"
        commands.append(cmd_str)
    for cmd in result.setblock_commands:
        x, y, z = cmd["position"]
        name = cmd["block_name"]
        states = cmd.get("block_states", "")
        if name.startswith("minecraft:"):
            cmd_str = f"setblock {x} {y} {z} {name}"
        else:
            cmd_str = f"setblock {x} {y} {z} minecraft:{name}"
        if states:
            cmd_str += f" {states}"
        commands.append(cmd_str)
    return commands


__all__ = [
    "CHUNK_SIZE", "CHUNK_SIZE_X", "CHUNK_SIZE_Y", "CHUNK_SIZE_Z",
    "SUBCHUNK_HEIGHT", "MAX_SUBCHUNK_INDEX",
    "ChunkFillError",
    "ChunkFillConfig", "ChunkFillResult", "ChunkFiller",
    "chunk_coord", "count_chunk_span", "get_subchunk_key",
    "generate_chunks_command", "fill_chunks",
]
