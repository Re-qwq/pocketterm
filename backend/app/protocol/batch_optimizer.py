"""批量优化器和增量导入系统。

逆向来源: Retalcer导入器 chunk_painter.py

功能:
    1. 方块合并: 沿Z轴扫描连续相同方块, 合并为单条 /fill 命令
    2. 立方体扩展: 沿X→Z→Y三轴扩展立方体, 最大化 /fill 体积
    3. 增量导入: 多个区块合并到一起导入
    4. 区块分组: 蛇形顺序排序, 按组发送

优化策略:
    - 单方块 → /setblock
    - 连续相同方块 → /fill (Z轴合并)
    - 立方体相同方块 → /fill (3轴扩展, schematic专用)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from .blocks import BlockState
from .magic_command import MagicCommandSender

logger = logging.getLogger("pocketterm.protocol.batch_optimizer")

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 区块大小
CHUNK_SIZE: int = 16

#: 最大 /fill 体积 (网易限制)
MAX_FILL_VOLUME: int = 32768  # 32x32x32

#: 默认区块组大小
DEFAULT_GROUP_SIZE: int = 3


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class BlockEntry:
    """方块条目"""

    x: int
    y: int
    z: int
    block: BlockState


@dataclass
class FillCommand:
    """fill 命令 (合并后的方块组)"""

    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int
    block: BlockState
    mode: str = "replace"

    def to_command(self) -> str:
        """转换为命令字符串"""
        cmd = f"fill {self.x1} {self.y1} {self.z1} {self.x2} {self.y2} {self.z2} {self.block.name}"
        if self.block.states:
            import json

            cmd += f' {json.dumps(self.block.states)}'
        cmd += f" {self.mode}"
        return cmd


@dataclass
class SetBlockCommand:
    """setblock 命令 (单个方块)"""

    x: int
    y: int
    z: int
    block: BlockState
    mode: str = "replace"

    def to_command(self) -> str:
        """转换为命令字符串"""
        cmd = f"setblock {self.x} {self.y} {self.z} {self.block.name}"
        if self.block.states:
            import json

            cmd += f' {json.dumps(self.block.states)}'
        cmd += f" {self.mode}"
        return cmd


# ----------------------------------------------------------------------
# 批量优化器
# ----------------------------------------------------------------------


class BatchOptimizer:
    """批量优化器 - 合并方块为 /fill 命令。

    逆向来源: Retalcer chunk_painter.py:598-614 (Z轴合并)
              Retalcer chunk_painter.py:768-816 (立方体扩展)
    """

    def __init__(self, sender: MagicCommandSender):
        """
        Args:
            sender: 魔法指令发送器
        """
        self.sender = sender

    def merge_z_axis(self, blocks: list[BlockEntry]) -> list[FillCommand | SetBlockCommand]:
        """沿Z轴合并连续相同方块。

        逆向自 Retalcer chunk_painter.py:598-614

        扫描同一Y层的方块, 将Z轴上连续相同的方块合并为一条 /fill 命令。
        单个方块使用 /setblock。

        Args:
            blocks: 方块列表 (已排序)

        Returns:
            合并后的命令列表
        """
        if not blocks:
            return []

        commands: list[FillCommand | SetBlockCommand] = []
        current_start: Optional[BlockEntry] = None
        current_block: Optional[BlockState] = None
        current_end_z: int = 0

        for block in blocks:
            if (
                current_start is not None
                and current_block is not None
                and block.x == current_start.x
                and block.y == current_start.y
                and block.z == current_end_z + 1
                and block.block.name == current_block.name
                and block.block.states == current_block.states
            ):
                # 继续当前 fill
                current_end_z = block.z
            else:
                # 结束当前 fill
                if current_start is not None and current_block is not None:
                    if current_start.z == current_end_z:
                        commands.append(
                            SetBlockCommand(
                                current_start.x,
                                current_start.y,
                                current_start.z,
                                current_block,
                            )
                        )
                    else:
                        commands.append(
                            FillCommand(
                                current_start.x,
                                current_start.y,
                                current_start.z,
                                current_start.x,
                                current_start.y,
                                current_end_z,
                                current_block,
                            )
                        )

                # 开始新的 fill
                current_start = block
                current_block = block.block
                current_end_z = block.z

        # 处理最后一个 fill
        if current_start is not None and current_block is not None:
            if current_start.z == current_end_z:
                commands.append(
                    SetBlockCommand(
                        current_start.x, current_start.y, current_start.z, current_block
                    )
                )
            else:
                commands.append(
                    FillCommand(
                        current_start.x,
                        current_start.y,
                        current_start.z,
                        current_start.x,
                        current_start.y,
                        current_end_z,
                        current_block,
                    )
                )

        return commands

    def expand_cube(
        self, blocks: list[BlockEntry], size_x: int, size_y: int, size_z: int
    ) -> list[FillCommand | SetBlockCommand]:
        """沿X→Z→Y三轴扩展立方体, 最大化 /fill 体积。

        逆向自 Retalcer chunk_painter.py:768-816 (schematic专用)

        尝试将相同方块扩展为最大的立方体, 减少命令数量。

        Args:
            blocks: 方块列表
            size_x: X轴最大尺寸
            size_y: Y轴最大尺寸
            size_z: Z轴最大尺寸

        Returns:
            合并后的命令列表
        """
        if not blocks:
            return []

        # 构建3D方块网格
        grid: dict[tuple[int, int, int], BlockState] = {}
        for b in blocks:
            grid[(b.x, b.y, b.z)] = b.block

        processed: set[tuple[int, int, int]] = set()
        commands: list[FillCommand | SetBlockCommand] = []

        # 按 Y→X→Z 顺序遍历
        sorted_blocks = sorted(blocks, key=lambda b: (b.y, b.x, b.z))

        for block in sorted_blocks:
            pos = (block.x, block.y, block.z)
            if pos in processed:
                continue

            block_state = block.block
            if block_state.name == "minecraft:air":
                processed.add(pos)
                continue

            # 尝试扩展立方体
            best_w, best_d, best_h = 1, 1, 1

            # 沿X轴扩展
            for w in range(2, min(size_x, 33) + 1):
                can_expand = True
                for dz in range(best_d):
                    for dy in range(best_h):
                        check_pos = (block.x + w - 1, block.y + dy, block.z + dz)
                        if (
                            check_pos not in grid
                            or grid[check_pos].name != block_state.name
                            or grid[check_pos].states != block_state.states
                        ):
                            can_expand = False
                            break
                    if not can_expand:
                        break
                if can_expand:
                    best_w = w
                else:
                    break

            # 沿Z轴扩展
            for d in range(2, min(size_z, 33) + 1):
                can_expand = True
                for dx in range(best_w):
                    for dy in range(best_h):
                        check_pos = (block.x + dx, block.y + dy, block.z + d - 1)
                        if (
                            check_pos not in grid
                            or grid[check_pos].name != block_state.name
                            or grid[check_pos].states != block_state.states
                        ):
                            can_expand = False
                            break
                    if not can_expand:
                        break
                if can_expand:
                    best_d = d
                else:
                    break

            # 沿Y轴扩展
            for h in range(2, min(size_y, 33) + 1):
                can_expand = True
                for dx in range(best_w):
                    for dz in range(best_d):
                        check_pos = (block.x + dx, block.y + h - 1, block.z + dz)
                        if (
                            check_pos not in grid
                            or grid[check_pos].name != block_state.name
                            or grid[check_pos].states != block_state.states
                        ):
                            can_expand = False
                            break
                    if not can_expand:
                        break
                if can_expand:
                    best_h = h
                else:
                    break

            # 标记已处理的方块
            for dx in range(best_w):
                for dy in range(best_h):
                    for dz in range(best_d):
                        processed.add((block.x + dx, block.y + dy, block.z + dz))

            # 生成命令
            if best_w == 1 and best_d == 1 and best_h == 1:
                commands.append(
                    SetBlockCommand(block.x, block.y, block.z, block_state)
                )
            else:
                commands.append(
                    FillCommand(
                        block.x,
                        block.y,
                        block.z,
                        block.x + best_w - 1,
                        block.y + best_h - 1,
                        block.z + best_d - 1,
                        block_state,
                    )
                )

        return commands

    async def send_commands(
        self,
        commands: list[FillCommand | SetBlockCommand],
        progress_callback: Optional[Any] = None,
    ) -> int:
        """发送合并后的命令列表。

        Args:
            commands: 命令列表
            progress_callback: 进度回调函数 (current, total)

        Returns:
            成功发送的命令数
        """
        total = len(commands)
        sent = 0

        for cmd in commands:
            await self.sender.send_any_command(cmd.to_command())
            sent += 1

            if progress_callback and sent % 100 == 0:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(sent, total)
                else:
                    progress_callback(sent, total)

        return sent


# ----------------------------------------------------------------------
# 增量导入器
# ----------------------------------------------------------------------


class IncrementalImporter:
    """增量导入器 - 多个区块合并到一起导入。

    逆向来源: Retalcer chunk_painter.py:296-397 (_paint_incremental)

    增量导入模式:
        1. 将所有方块按区块(CZ%2)排序
        2. 按组大小分组
        3. 每组: TP到中心 → 添加tickingarea → 遍历方块 → 发送命令 → 移除tickingarea
        4. 组间等待 GROUP_WAIT 秒

    优势:
        - 减少TP次数 (同组只TP一次)
        - 减少tickingarea切换次数
        - 批量发送命令, 效率更高
    """

    def __init__(self, sender: MagicCommandSender, optimizer: BatchOptimizer):
        """
        Args:
            sender: 魔法指令发送器
            optimizer: 批量优化器
        """
        self.sender = sender
        self.optimizer = optimizer
        self.group_size: int = DEFAULT_GROUP_SIZE

    def sort_chunks_snake(self, chunks: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """蛇形排序区块。

        逆向自 Retalcer chunk_painter.py:513-522

        将区块按蛇形顺序排序 (CZ%2 反向),
        减少TP移动距离。

        Args:
            chunks: 区块坐标列表 [(cx, cz), ...]

        Returns:
            排序后的区块列表
        """
        return sorted(chunks, key=lambda c: (c[1], c[0] if c[1] % 2 == 0 else -c[0]))

    def group_chunks(
        self, chunks: list[tuple[int, int]]
    ) -> list[list[tuple[int, int]]]:
        """将区块分组。

        Args:
            chunks: 区块坐标列表

        Returns:
            分组后的区块列表 [[(cx,cz),...], ...]
        """
        sorted_chunks = self.sort_chunks_snake(chunks)
        groups = []
        for i in range(0, len(sorted_chunks), self.group_size):
            groups.append(sorted_chunks[i : i + self.group_size])
        return groups

    async def import_blocks(
        self,
        blocks: list[BlockEntry],
        origin_x: int = 0,
        origin_y: int = 0,
        origin_z: int = 0,
        include_air: bool = False,
        progress_callback: Optional[Any] = None,
    ) -> int:
        """增量导入方块。

        Args:
            blocks: 方块列表 (相对坐标)
            origin_x, origin_y, origin_z: 原点坐标 (方块坐标会加上原点)
            include_air: 是否包含空气方块 (会先清空区域)
            progress_callback: 进度回调

        Returns:
            成功放置的方块数
        """
        if not blocks:
            return 0

        # 转换为绝对坐标
        abs_blocks = [
            BlockEntry(
                x=b.x + origin_x,
                y=b.y + origin_y,
                z=b.z + origin_z,
                block=b.block,
            )
            for b in blocks
            if include_air or b.block.name != "minecraft:air"
        ]

        # 按区块分组
        chunk_map: dict[tuple[int, int], list[BlockEntry]] = {}
        for block in abs_blocks:
            cx, cz = block.x // CHUNK_SIZE, block.z // CHUNK_SIZE
            if (cx, cz) not in chunk_map:
                chunk_map[(cx, cz)] = []
            chunk_map[(cx, cz)].append(block)

        # 蛇形排序并分组
        chunks = list(chunk_map.keys())
        groups = self.group_chunks(chunks)

        total_placed = 0
        total_chunks = len(chunks)
        processed_chunks = 0

        for group in groups:
            # TP到组中心
            center_cx = sum(c[0] for c in group) // len(group)
            center_cz = sum(c[1] for c in group) // len(group)
            center_x = center_cx * CHUNK_SIZE + CHUNK_SIZE // 2
            center_z = center_cz * CHUNK_SIZE + CHUNK_SIZE // 2

            await self.sender.send_wo_command(f"tp @s {center_x} ~ {center_z}")
            # 等待传送完成, 区块需要加载 (逆向自 Retalcer chunk_painter.py)
            await asyncio.sleep(1.2)

            # 添加tickingarea
            area_name = f"import_{center_cx}_{center_cz}"
            await self.sender.send_wo_command(
                f"tickingarea add {center_cx * CHUNK_SIZE} 0 {center_cz * CHUNK_SIZE} "
                f"{(center_cx + 1) * CHUNK_SIZE - 1} 255 "
                f"{(center_cz + 1) * CHUNK_SIZE - 1} \"{area_name}\""
            )
            # 等待tickingarea加载, 服务器需要时间加载区域 (逆向自 Retalcer chunk_painter.py)
            await asyncio.sleep(0.6)

            # 遍历组内每个区块
            for cx, cz in group:
                chunk_blocks = chunk_map.get((cx, cz), [])
                if not chunk_blocks:
                    continue

                # 按Y层排序
                chunk_blocks.sort(key=lambda b: (b.y, b.x, b.z))

                # 合并方块
                commands = self.optimizer.merge_z_axis(chunk_blocks)

                # 发送命令
                for cmd in commands:
                    await self.sender.send_any_command(cmd.to_command())
                    total_placed += 1

                # 等待服务器处理本区块的方块命令 (逆向自 Retalcer chunk_painter.py)
                await asyncio.sleep(0.1)

                processed_chunks += 1
                if progress_callback:
                    if asyncio.iscoroutinefunction(progress_callback):
                        await progress_callback(processed_chunks, total_chunks)
                    else:
                        progress_callback(processed_chunks, total_chunks)

            # 移除tickingarea
            await self.sender.send_wo_command(f"tickingarea remove \"{area_name}\"")

            # 组间等待
            await self.sender.rate_limiter.wait_group()

        return total_placed


# ----------------------------------------------------------------------
# 进度显示
# ----------------------------------------------------------------------


@dataclass
class ProgressInfo:
    """进度信息"""

    current: int = 0
    total: int = 0
    speed: float = 0.0  # 方块/秒
    elapsed: float = 0.0  # 已用时间
    remaining: float = 0.0  # 预计剩余时间
    current_pos: tuple[int, int, int] = (0, 0, 0)
    size: tuple[int, int, int] = (0, 0, 0)

    @property
    def percentage(self) -> float:
        """百分比"""
        return (self.current / self.total * 100) if self.total > 0 else 0.0

    def progress_bar(self, length: int = 20) -> str:
        """生成进度条字符串"""
        filled = int(self.percentage / 100 * length)
        bar = "█" * filled + "░" * (length - filled)
        return f"§a{bar}§r"

    def to_actionbar(self) -> str:
        """转换为 actionbar 命令文本"""
        return (
            f"{self.progress_bar()} {self.percentage:.1f}% "
            f"({self.current}/{self.total}) "
            f"速度:{self.speed:.0f}/s "
            f"剩余:{self.remaining:.0f}s"
        )


class ProgressTracker:
    """进度跟踪器 - 通过 /titleraw 显示进度。

    逆向来源: Retalcer chunk_painter.py:137-180
    """

    def __init__(self, sender: MagicCommandSender, total: int):
        """
        Args:
            sender: 魔法指令发送器
            total: 总方块数
        """
        self.sender = sender
        self.total = total
        self.current = 0
        self.start_time = time.monotonic()
        self._last_update: float = 0.0
        self._update_interval: float = 1.0  # 1秒更新一次

    async def update(self, count: int = 1) -> None:
        """更新进度。

        Args:
            count: 新完成的数量
        """
        self.current += count
        now = time.monotonic()

        # 1秒节流
        if now - self._last_update < self._update_interval:
            return

        self._last_update = now
        elapsed = now - self.start_time
        speed = self.current / elapsed if elapsed > 0 else 0
        remaining = (self.total - self.current) / speed if speed > 0 else 0

        info = ProgressInfo(
            current=self.current,
            total=self.total,
            speed=speed,
            elapsed=elapsed,
            remaining=remaining,
        )

        # 发送 actionbar 进度
        await self.sender.send_ai_command(
            f'titleraw @a actionbar {{"rawtext":[{{"text":"{info.to_actionbar()}"}}]}}'
        )

    async def complete(self) -> None:
        """显示完成信息"""
        elapsed = time.monotonic() - self.start_time
        msg = f"§a导入完成! §r共 {self.current} 方块, 用时 {elapsed:.1f}s"
        await self.sender.send_ai_command(
            f'titleraw @a title {{"rawtext":[{{"text":"{msg}"}}]}}'
        )


# ----------------------------------------------------------------------
# 多区块合并功能 (Multi-Chunk Merge)
# ----------------------------------------------------------------------

import numpy as _np


@dataclass
class Cuboid:
    """立方体区域 (最小/最大坐标)。"""

    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int

    @property
    def volume(self) -> int:
        """立方体体积。"""
        return (
            (self.x2 - self.x1 + 1)
            * (self.y2 - self.y1 + 1)
            * (self.z2 - self.z1 + 1)
        )


def split_cuboid(cuboid: Cuboid, max_volume: int = 32768) -> list[Cuboid]:
    """智能切分立方体, 将体积超过 max_volume 的区域切分为多个子立方体。

    逆向自 NovaBuilder galaxy/import/optimizer.go splitCuboid 函数。

    切分策略:
        1. 如果体积 <= max_volume, 直接返回
        2. 优先沿最长的轴切分
        3. 每个子立方体递归切分直到体积 <= max_volume

    Args:
        cuboid: 待切分的立方体区域。
        max_volume: 最大允许体积 (网易限制 32768 = 32x32x32)。

    Returns:
        切分后的子立方体列表。
    """
    if cuboid.volume <= max_volume:
        return [cuboid]

    # 计算各轴长度
    dx = cuboid.x2 - cuboid.x1 + 1
    dy = cuboid.y2 - cuboid.y1 + 1
    dz = cuboid.z2 - cuboid.z1 + 1

    results: list[Cuboid] = []

    # 沿最长轴切分
    if dx >= dy and dx >= dz:
        # 沿 X 轴切分
        mid = cuboid.x1 + (dx // 2)
        left = Cuboid(cuboid.x1, cuboid.y1, cuboid.z1, mid - 1, cuboid.y2, cuboid.z2)
        right = Cuboid(mid, cuboid.y1, cuboid.z1, cuboid.x2, cuboid.y2, cuboid.z2)
        results.extend(split_cuboid(left, max_volume))
        results.extend(split_cuboid(right, max_volume))
    elif dy >= dx and dy >= dz:
        # 沿 Y 轴切分
        mid = cuboid.y1 + (dy // 2)
        bottom = Cuboid(cuboid.x1, cuboid.y1, cuboid.z1, cuboid.x2, mid - 1, cuboid.z2)
        top = Cuboid(cuboid.x1, mid, cuboid.z1, cuboid.x2, cuboid.y2, cuboid.z2)
        results.extend(split_cuboid(bottom, max_volume))
        results.extend(split_cuboid(top, max_volume))
    else:
        # 沿 Z 轴切分
        mid = cuboid.z1 + (dz // 2)
        near = Cuboid(cuboid.x1, cuboid.y1, cuboid.z1, cuboid.x2, cuboid.y2, mid - 1)
        far = Cuboid(cuboid.x1, cuboid.y1, mid, cuboid.x2, cuboid.y2, cuboid.z2)
        results.extend(split_cuboid(near, max_volume))
        results.extend(split_cuboid(far, max_volume))

    return results


def merge_multi_chunk(
    chunk_data: dict[tuple[int, int], list[BlockEntry]],
    size_x: int,
    size_y: int,
    size_z: int,
) -> _np.ndarray:
    """将多个区块的方块数据合并到单个 3D numpy 数组。

    逆向自 NovaBuilder galaxy/import/multi_chunk.go mergeMultiChunk 函数。

    功能:
        1. 创建 size_x * size_y * size_z 的 numpy 数组
        2. 将每个区块的方块数据填充到对应位置
        3. 数组元素为方块名称字符串, 空位为 None

    Args:
        chunk_data: 区块数据映射, 键为 (cx, cz) 区块坐标, 值为方块列表。
        size_x: X 轴总尺寸 (方块数)。
        size_y: Y 轴总尺寸 (方块数)。
        size_z: Z 轴总尺寸 (方块数)。

    Returns:
        3D numpy 数组 (dtype=object), 形状为 (size_y, size_z, size_x)。
    """
    # 创建 3D 数组 (Y, Z, X 顺序以匹配 Minecraft 世界坐标)
    # 使用 object dtype 存储方块名称字符串
    grid = _np.empty((size_y, size_z, size_x), dtype=object)
    grid.fill(None)

    for (cx, cz), blocks in chunk_data.items():
        offset_x = cx * CHUNK_SIZE
        offset_z = cz * CHUNK_SIZE
        for block in blocks:
            bx = block.x - offset_x
            by = block.y
            bz = block.z - offset_z
            if 0 <= bx < size_x and 0 <= by < size_y and 0 <= bz < size_z:
                grid[by, bz, bx] = block.block.name

    return grid


def generate_fill_commands(
    grid: _np.ndarray,
    origin_x: int = 0,
    origin_y: int = 0,
    origin_z: int = 0,
    max_volume: int = 32768,
) -> list[FillCommand | SetBlockCommand]:
    """从 3D numpy 数组生成 /fill 和 /setblock 命令。

    逆向自 NovaBuilder galaxy/import/multi_chunk.go generateFillCommands 函数。

    功能:
        1. 扫描 3D 数组, 查找连续相同方块区域
        2. 使用立方体扩展算法最大化 /fill 体积
        3. 单个方块使用 /setblock
        4. 体积超过 max_volume 的区域自动切分

    Args:
        grid: 3D numpy 数组 (dtype=object), 形状为 (Y, Z, X)。
        origin_x: X 轴原点偏移 (世界坐标基准)。
        origin_y: Y 轴原点偏移 (世界坐标基准)。
        origin_z: Z 轴原点偏移 (世界坐标基准)。
        max_volume: 最大 /fill 体积。

    Returns:
        命令列表 (FillCommand / SetBlockCommand)。
    """
    shape = grid.shape
    size_y, size_z, size_x = shape

    visited: set[tuple[int, int, int]] = set()
    commands: list[FillCommand | SetBlockCommand] = []

    for y in range(size_y):
        for z in range(size_z):
            for x in range(size_x):
                if (y, z, x) in visited:
                    continue

                block_name = grid[y, z, x]
                if block_name is None:
                    visited.add((y, z, x))
                    continue

                # 尝试扩展立方体
                # 沿 X 轴扩展
                max_w = 1
                for w in range(2, min(size_x - x + 1, 33)):
                    can_expand = True
                    for dy in range(1):
                        for dz in range(1):
                            check_y = y + dy
                            check_z = z + dz
                            check_x = x + w - 1
                            if (
                                check_y >= size_y
                                or check_z >= size_z
                                or check_x >= size_x
                                or (check_y, check_z, check_x) in visited
                                or grid[check_y, check_z, check_x] != block_name
                            ):
                                can_expand = False
                                break
                        if not can_expand:
                            break
                    if can_expand:
                        max_w = w
                    else:
                        break

                # 沿 Z 轴扩展
                max_d = 1
                for d in range(2, min(size_z - z + 1, 33)):
                    can_expand = True
                    for dx in range(max_w):
                        for dy in range(1):
                            check_y = y + dy
                            check_z = z + d - 1
                            check_x = x + dx
                            if (
                                check_y >= size_y
                                or check_z >= size_z
                                or check_x >= size_x
                                or (check_y, check_z, check_x) in visited
                                or grid[check_y, check_z, check_x] != block_name
                            ):
                                can_expand = False
                                break
                        if not can_expand:
                            break
                    if can_expand:
                        max_d = d
                    else:
                        break

                # 沿 Y 轴扩展
                max_h = 1
                for h in range(2, min(size_y - y + 1, 33)):
                    can_expand = True
                    for dx in range(max_w):
                        for dz in range(max_d):
                            check_y = y + h - 1
                            check_z = z + dz
                            check_x = x + dx
                            if (
                                check_y >= size_y
                                or check_z >= size_z
                                or check_x >= size_x
                                or (check_y, check_z, check_x) in visited
                                or grid[check_y, check_z, check_x] != block_name
                            ):
                                can_expand = False
                                break
                        if not can_expand:
                            break
                    if can_expand:
                        max_h = h
                    else:
                        break

                # 标记已处理
                for dy in range(max_h):
                    for dz in range(max_d):
                        for dx in range(max_w):
                            visited.add((y + dy, z + dz, x + dx))

                # 生成命令
                wx = origin_x + x
                wy = origin_y + y
                wz = origin_z + z

                if max_w == 1 and max_d == 1 and max_h == 1:
                    # 单方块
                    commands.append(
                        SetBlockCommand(
                            wx, wy, wz,
                            BlockState(name=str(block_name), states={}),
                        )
                    )
                else:
                    cuboid = Cuboid(
                        wx, wy, wz,
                        wx + max_w - 1, wy + max_h - 1, wz + max_d - 1,
                    )
                    # 如果体积过大, 切分
                    if cuboid.volume > max_volume:
                        sub_cuboids = split_cuboid(cuboid, max_volume)
                        for sub in sub_cuboids:
                            commands.append(
                                FillCommand(
                                    sub.x1, sub.y1, sub.z1,
                                    sub.x2, sub.y2, sub.z2,
                                    BlockState(name=str(block_name), states={}),
                                )
                            )
                    else:
                        commands.append(
                            FillCommand(
                                wx, wy, wz,
                                wx + max_w - 1, wy + max_h - 1, wz + max_d - 1,
                                BlockState(name=str(block_name), states={}),
                            )
                        )

    return commands


# ----------------------------------------------------------------------
# 从内向外排序器 (Inner-to-Outer Sorter)
# ----------------------------------------------------------------------


class InnerToOuterSorter:
    """从内向外排序器 — 按曼哈顿距离从建筑中心向外排序。

    逆向自 NovaBuilder galaxy/import/sorter.go innerToOuterSorter 函数。

    排序策略:
        1. 计算建筑中心坐标
        2. 按曼哈顿距离 (|x-cx| + |y-cy| + |z-cz|) 排序
        3. 距离相同的方块按 Y 轴优先 (自底向上)

    优势:
        - 从内部填充, 减少方块冲突
        - 适合大型建筑, 尤其是中空结构
    """

    def __init__(
        self,
        center_x: float = 0.0,
        center_y: float = 0.0,
        center_z: float = 0.0,
    ) -> None:
        """
        Args:
            center_x: 中心 X 坐标 (浮点, 支持非整数中心)。
            center_y: 中心 Y 坐标。
            center_z: 中心 Z 坐标。
        """
        self._center = (center_x, center_y, center_z)

    def sort(self, blocks: list[BlockEntry]) -> list[BlockEntry]:
        """按从内向外的顺序排序。

        Args:
            blocks: 方块列表。

        Returns:
            排序后的方块列表。
        """
        cx, cy, cz = self._center

        def _key(b: BlockEntry) -> tuple[float, int]:
            dx = b.x - cx
            dy = b.y - cy
            dz = b.z - cz
            # 曼哈顿距离 (主要排序键)
            dist = abs(dx) + abs(dy) + abs(dz)
            # Y 轴优先 (次要排序键, 负号使 Y 大的先排)
            return (dist, -b.y)

        return sorted(blocks, key=_key)

    def sort_with_priority(
        self,
        blocks: list[BlockEntry],
        priority_blocks: set[str],
    ) -> list[BlockEntry]:
        """按从内向外的顺序排序, 优先放置特定方块类型。

        Args:
            blocks: 方块列表。
            priority_blocks: 优先放置的方块名称集合。

        Returns:
            排序后的方块列表 (优先方块排在前面)。
        """
        priority_list: list[BlockEntry] = []
        normal_list: list[BlockEntry] = []

        for block in blocks:
            if block.block.name in priority_blocks:
                priority_list.append(block)
            else:
                normal_list.append(block)

        return self.sort(priority_list) + self.sort(normal_list)


# ----------------------------------------------------------------------
# IncrementalImporter 扩展方法
# ----------------------------------------------------------------------


def _patch_incremental_importer() -> None:
    """为 IncrementalImporter 类添加新方法 (多区块合并相关)。

    在模块加载时自动调用, 为 IncrementalImporter 动态添加以下方法:
        - import_multi_chunk: 多区块合并导入
        - import_inner_to_outer: 从内向外导入
        - set_chunk_grid_size: 设置 NxN 区块网格
        - set_algorithm: 选择算法
    """

    async def import_multi_chunk(
        self: IncrementalImporter,
        chunk_data: dict[tuple[int, int], list[BlockEntry]],
        size_x: int,
        size_y: int,
        size_z: int,
        origin_x: int = 0,
        origin_y: int = 0,
        origin_z: int = 0,
        progress_callback: Optional[Any] = None,
    ) -> int:
        """多区块合并导入 — 将多个区块合并为 3D 数组后批量导入。

        功能:
            1. 合并所有区块数据到 3D numpy 数组
            2. 生成 /fill 和 /setblock 命令
            3. 批量发送命令

        Args:
            chunk_data: 区块数据映射, 键为 (cx, cz), 值为方块列表。
            size_x: X 轴总尺寸。
            size_y: Y 轴总尺寸。
            size_z: Z 轴总尺寸。
            origin_x, origin_y, origin_z: 原点偏移。
            progress_callback: 进度回调。

        Returns:
            成功放置的方块数。
        """
        if not chunk_data:
            return 0

        # 合并到 3D 数组
        grid = merge_multi_chunk(chunk_data, size_x, size_y, size_z)

        # 生成命令
        commands = generate_fill_commands(
            grid,
            origin_x=origin_x,
            origin_y=origin_y,
            origin_z=origin_z,
        )

        # 发送命令
        total = len(commands)
        sent = 0
        for i, cmd in enumerate(commands):
            await self.sender.send_any_command(cmd.to_command())
            sent += 1
            if progress_callback and i % 100 == 0:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(i, total)
                else:
                    progress_callback(i, total)

        return sent

    async def import_inner_to_outer(
        self: IncrementalImporter,
        blocks: list[BlockEntry],
        size_x: int,
        size_y: int,
        size_z: int,
        origin_x: int = 0,
        origin_y: int = 0,
        origin_z: int = 0,
        priority_blocks: Optional[set[str]] = None,
        progress_callback: Optional[Any] = None,
    ) -> int:
        """从内向外导入 — 按曼哈顿距离从中心向外放置方块。

        功能:
            1. 计算建筑中心
            2. 按曼哈顿距离排序 (从内向外)
            3. 优化命令生成 (Z 轴合并)
            4. 发送命令

        Args:
            blocks: 方块列表 (相对坐标)。
            size_x, size_y, size_z: 建筑尺寸。
            origin_x, origin_y, origin_z: 原点偏移。
            priority_blocks: 优先放置的方块类型集合。
            progress_callback: 进度回调。

        Returns:
            成功放置的方块数。
        """
        if not blocks:
            return 0

        # 计算中心
        center_x = size_x / 2.0
        center_y = size_y / 2.0
        center_z = size_z / 2.0

        sorter = InnerToOuterSorter(center_x, center_y, center_z)

        if priority_blocks:
            sorted_blocks = sorter.sort_with_priority(blocks, priority_blocks)
        else:
            sorted_blocks = sorter.sort(blocks)

        # 转换为绝对坐标
        abs_blocks = [
            BlockEntry(
                x=b.x + origin_x,
                y=b.y + origin_y,
                z=b.z + origin_z,
                block=b.block,
            )
            for b in sorted_blocks
        ]

        # 合并 Z 轴
        commands = self.optimizer.merge_z_axis(abs_blocks)

        # 发送命令
        total = len(commands)
        sent = 0
        for i, cmd in enumerate(commands):
            await self.sender.send_any_command(cmd.to_command())
            sent += 1
            if progress_callback and i % 100 == 0:
                if asyncio.iscoroutinefunction(progress_callback):
                    await progress_callback(i, total)
                else:
                    progress_callback(i, total)

        return sent

    def set_chunk_grid_size(self: IncrementalImporter, grid_size: int) -> None:
        """设置 NxN 区块网格大小。

        Args:
            grid_size: 网格大小 (如 3 表示 3x3=NxN 区块)。

        Raises:
            ValueError: grid_size 不是正整数。
        """
        if grid_size < 1:
            raise ValueError(f"grid_size 必须为正整数, 实际: {grid_size}")
        self.group_size = grid_size * grid_size
        logger.debug(
            f"IncrementalImporter: 区块网格大小设置为 {grid_size}x{grid_size} "
            f"(group_size={self.group_size})"
        )

    def set_algorithm(
        self: IncrementalImporter,
        algorithm: str,
    ) -> None:
        """选择导入算法。

        Args:
            algorithm: 算法名称, 可选:
                - "z_merge" (默认): Z 轴合并
                - "cube_expand": 立方体扩展
                - "inner_to_outer": 从内向外
                - "multi_chunk": 多区块合并

        Raises:
            ValueError: 不支持的算法。
        """
        valid = {"z_merge", "cube_expand", "inner_to_outer", "multi_chunk"}
        if algorithm not in valid:
            raise ValueError(
                f"不支持的算法: '{algorithm}', 可选: {', '.join(sorted(valid))}"
            )
        self._algorithm = algorithm
        logger.debug(f"IncrementalImporter: 算法设置为 '{algorithm}'")

    # 动态绑定方法
    IncrementalImporter.import_multi_chunk = import_multi_chunk
    IncrementalImporter.import_inner_to_outer = import_inner_to_outer
    IncrementalImporter.set_chunk_grid_size = set_chunk_grid_size
    IncrementalImporter.set_algorithm = set_algorithm
    # 初始化算法属性
    if not hasattr(IncrementalImporter, '_algorithm'):
        IncrementalImporter._algorithm = "z_merge"


# 在模块加载时自动执行补丁
_patch_incremental_importer()


__all__ = [
    "CHUNK_SIZE",
    "MAX_FILL_VOLUME",
    "DEFAULT_GROUP_SIZE",
    "BlockEntry",
    "FillCommand",
    "SetBlockCommand",
    "BatchOptimizer",
    "IncrementalImporter",
    "ProgressInfo",
    "ProgressTracker",
    "InnerToOuterSorter",
    "split_cuboid",
    "merge_multi_chunk",
    "generate_fill_commands",
]
