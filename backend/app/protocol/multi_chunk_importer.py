"""多区块合并增量导入器。

逆向来源:
    - fatalder_source/utils/chunk_fill/chunk_fill.go (GenerateChunksCommand 算法)
    - NexusE / NovaBuilder: GenerateChunksCommand, splitCuboid, FillFromInnerToOuter
    - merry-memory: BDump 命令系统

核心功能:
    - 多区块合并: 用户输入N，则N×N个区块合并为一个导入单元
    - 3D数组填充: 所有区块数据填充到全局3D数组，计算全局包围盒
    - X→Z→Y三轴扩展: 找到最大相同方块矩形，最大化fill体积
    - splitCuboid算法: 体积>32768时智能切分 (计算最优切分尺寸，greedy expansion)
    - FillFromInnerToOuter: 从建筑中心向外逐层扩展，曼哈顿距离排序
    - 智能算法选择: 小体积直接fill，大体积splitCuboid，单方块setblock，NBT方块走BDump通道
    - 修补模式: 仅导入与现有世界差异部分
    - 从指定区块开始: 支持断点续传
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

from .blocks import BlockState
from .import_options import ImportAlgorithm, ImportOptions, PatchOptions

logger = logging.getLogger("pocketterm.protocol.multi_chunk_importer")

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 区块大小
CHUNK_SIZE: int = 16

#: 最大 /fill 体积 (网易限制: 32768 = 32x32x32)
MAX_FILL_VOLUME: int = 32768

#: 世界高度范围
Y_MIN: int = -64
Y_MAX: int = 319

#: 空气方块名称
AIR_BLOCK_NAME: str = "minecraft:air"

#: 屏障方块名称
BARRIER_BLOCK_NAME: str = "minecraft:barrier"

#: Magma方块名称 (Unbuilder模式)
MAGMA_BLOCK_NAME: str = "minecraft:magma"

#: Water方块名称 (Unbuilder模式)
WATER_BLOCK_NAME: str = "minecraft:water"


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class SplitCuboidResult:
    """切分结果。

    逆向自 fatalder_source/utils/chunk_fill/chunk_fill.go:splitCuboid

    Attributes:
        split_x: X轴切分尺寸
        split_y: Y轴切分尺寸
        split_z: Z轴切分尺寸
    """

    split_x: int
    split_y: int
    split_z: int

    @property
    def volume(self) -> int:
        """切分后的体积。"""
        return self.split_x * self.split_y * self.split_z


@dataclass
class GlobalBlockGrid:
    """全局3D方块网格。

    将所有区块的方块数据填充到连续3D数组中。

    Attributes:
        blocks: 3D方块数据 (x → y → z)
        size_x: X轴尺寸
        size_y: Y轴尺寸
        size_z: Z轴尺寸
        min_x: 全局最小X坐标 (相对)
        min_y: 全局最小Y坐标 (相对)
        min_z: 全局最小Z坐标 (相对)
        has_nbt: 标记哪些位置有NBT数据
    """

    blocks: list[list[list[BlockState]]] = field(default_factory=list)
    size_x: int = 0
    size_y: int = 0
    size_z: int = 0
    min_x: int = 0
    min_y: int = 0
    min_z: int = 0
    has_nbt: set[tuple[int, int, int]] = field(default_factory=set)

    def get_block(self, x: int, y: int, z: int) -> BlockState:
        """获取指定位置的方块。

        Args:
            x, y, z: 相对坐标 (0-based, 相对于网格原点)。

        Returns:
            方块状态。
        """
        if 0 <= x < self.size_x and 0 <= y < self.size_y and 0 <= z < self.size_z:
            return self.blocks[x][y][z]
        return BlockState(name=AIR_BLOCK_NAME)

    def set_block(self, x: int, y: int, z: int, block: BlockState) -> None:
        """设置指定位置的方块 (内部使用, 不检查边界)。"""
        self.blocks[x][y][z] = block

    def is_air(self, x: int, y: int, z: int) -> bool:
        """检查指定位置是否是空气方块。"""
        return self.get_block(x, y, z).name == AIR_BLOCK_NAME

    def has_nbt_at(self, x: int, y: int, z: int) -> bool:
        """检查指定位置是否有NBT数据。"""
        return (x, y, z) in self.has_nbt

    def get_global_pos(self, x: int, y: int, z: int) -> tuple[int, int, int]:
        """获取全局坐标 (相对坐标 + 最小坐标)。

        Args:
            x, y, z: 相对坐标 (0-based)。

        Returns:
            (gx, gy, gz) 全局坐标。
        """
        return (x + self.min_x, y + self.min_y, z + self.min_z)


@dataclass
class FillCommand:
    """fill 命令 (合并后的方块组)。

    Attributes:
        x1, y1, z1: 起始坐标 (相对)
        x2, y2, z2: 结束坐标 (相对)
        block: 方块状态
        mode: 填充模式 (replace/destroy/keep/hollow/outline)
    """

    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int
    block: BlockState
    mode: str = "replace"

    @property
    def volume(self) -> int:
        """填充体积。"""
        return (self.x2 - self.x1 + 1) * (self.y2 - self.y1 + 1) * (self.z2 - self.z1 + 1)

    def to_command(self, offset_x: int = 0, offset_y: int = 0, offset_z: int = 0) -> str:
        """转换为命令字符串。

        Args:
            offset_x, offset_y, offset_z: 世界坐标偏移。

        Returns:
            命令字符串。
        """
        import json

        cmd = (
            f"fill {self.x1 + offset_x} {self.y1 + offset_y} {self.z1 + offset_z} "
            f"{self.x2 + offset_x} {self.y2 + offset_y} {self.z2 + offset_z} "
            f"{self.block.name}"
        )
        if self.block.states:
            cmd += f" {json.dumps(self.block.states)}"
        cmd += f" {self.mode}"
        return cmd


@dataclass
class SetBlockCommand:
    """setblock 命令 (单个方块)。

    Attributes:
        x, y, z: 方块的相对坐标
        block: 方块状态
        mode: 放置模式 (replace/destroy/keep)
    """

    x: int
    y: int
    z: int
    block: BlockState
    mode: str = "replace"

    def to_command(self, offset_x: int = 0, offset_y: int = 0, offset_z: int = 0) -> str:
        """转换为命令字符串。

        Args:
            offset_x, offset_y, offset_z: 世界坐标偏移。

        Returns:
            命令字符串。
        """
        import json

        cmd = (
            f"setblock {self.x + offset_x} {self.y + offset_y} {self.z + offset_z} "
            f"{self.block.name}"
        )
        if self.block.states:
            cmd += f" {json.dumps(self.block.states)}"
        cmd += f" {self.mode}"
        return cmd


@dataclass
class ImportConfig:
    """导入配置。

    Attributes:
        chunk_size: 多区块合并大小 (N×N, 默认1)
        include_nbt: 是否包含NBT数据
        include_command_blocks: 是否包含命令方块
        command_block_speed: 命令方块处理速度 (命令/秒)
        patch_mode: 是否启用修补模式
        start_chunk: 起始区块坐标 (用于断点续传)
        algorithm: 导入算法
        no_import_bar: 是否跳过屏障方块
        unbuilder: 是否启用Unbuilder模式
        close_sign: 是否关闭告示牌
    """

    chunk_size: int = 1
    include_nbt: bool = True
    include_command_blocks: bool = True
    command_block_speed: int = 10
    patch_mode: bool = False
    start_chunk: Optional[tuple[int, int]] = None
    algorithm: ImportAlgorithm = ImportAlgorithm.AUTO
    no_import_bar: bool = False
    unbuilder: bool = False
    close_sign: bool = False

    @classmethod
    def from_options(cls, options: ImportOptions) -> "ImportConfig":
        """从 ImportOptions 创建 ImportConfig。

        Args:
            options: 导入选项。

        Returns:
            ImportConfig 实例。
        """
        return cls(
            chunk_size=options.chunk_size,
            include_nbt=options.include_nbt,
            include_command_blocks=options.include_command_blocks,
            command_block_speed=options.command_block_speed,
            patch_mode=options.patch.patch_mode,
            start_chunk=options.start_chunk,
            algorithm=options.algorithm,
            no_import_bar=options.patch.no_import_bar,
            unbuilder=options.patch.unbuilder,
            close_sign=options.patch.close_sign,
        )


# ----------------------------------------------------------------------
# 多区块合并导入器
# ----------------------------------------------------------------------


class MultiChunkImporter:
    """多区块合并导入器。

    逆向自 fatalder_source/utils/chunk_fill/chunk_fill.go:GenerateChunksCommand

    核心算法:
        1. 将N×N区块合并为一个导入单元
        2. 所有方块填充到全局3D数组
        3. X→Z→Y三轴扩展找到最大相同方块矩形
        4. 体积>32768时使用splitCuboid智能切分
        5. 支持FillFromInnerToOuter从中心向外扩展
        6. 智能选择算法
    """

    def __init__(
        self,
        config: Optional[ImportConfig] = None,
        options: Optional[ImportOptions] = None,
    ) -> None:
        """
        Args:
            config: 导入配置。
            options: 导入选项 (优先级低于config)。
        """
        self.config = config or (
            ImportConfig.from_options(options) if options else ImportConfig()
        )

    # ------------------------------------------------------------------
    # 全局3D数组构建
    # ------------------------------------------------------------------

    def build_global_grid(
        self,
        chunk_blocks: dict[tuple[int, int], list[tuple[int, int, int, BlockState]]],
        nbt_data: Optional[dict[tuple[int, int, int], dict[str, Any]]] = None,
    ) -> GlobalBlockGrid:
        """构建全局3D方块网格。

        将所有区块的方块数据填充到连续3D数组中。

        逆向自 fatalder_source/utils/chunk_fill/chunk_fill.go:38-148

        Args:
            chunk_blocks: 区块坐标 -> 方块列表 (cx, cz) -> [(x, y, z, block), ...]
            nbt_data: NBT数据映射 (gx, gy, gz) -> nbt_dict

        Returns:
            GlobalBlockGrid 实例。
        """
        if not chunk_blocks:
            return GlobalBlockGrid()

        # 计算全局包围盒
        all_blocks: list[tuple[int, int, int, BlockState]] = []
        for chunk_pos, blocks in chunk_blocks.items():
            cx, cz = chunk_pos
            for bx, by, bz, block in blocks:
                gx = cx * CHUNK_SIZE + bx
                gz = cz * CHUNK_SIZE + bz
                all_blocks.append((gx, by, gz, block))

        if not all_blocks:
            return GlobalBlockGrid()

        min_x = min(b[0] for b in all_blocks)
        min_y = min(b[1] for b in all_blocks)
        min_z = min(b[2] for b in all_blocks)
        max_x = max(b[0] for b in all_blocks)
        max_y = max(b[1] for b in all_blocks)
        max_z = max(b[2] for b in all_blocks)

        size_x = max_x - min_x + 1
        size_y = max_y - min_y + 1
        size_z = max_z - min_z + 1

        if size_x <= 0 or size_y <= 0 or size_z <= 0:
            return GlobalBlockGrid()

        # 初始化3D数组
        grid_blocks: list[list[list[BlockState]]] = []
        for x in range(size_x):
            plane_x: list[list[BlockState]] = []
            for y in range(size_y):
                row_y: list[BlockState] = [BlockState(name=AIR_BLOCK_NAME) for _ in range(size_z)]
                plane_x.append(row_y)
            grid_blocks.append(plane_x)

        # 填充方块数据
        for gx, gy, gz, block in all_blocks:
            lx = gx - min_x
            ly = gy - min_y
            lz = gz - min_z
            if 0 <= lx < size_x and 0 <= ly < size_y and 0 <= lz < size_z:
                grid_blocks[lx][ly][lz] = block

        # 标记NBT数据位置
        has_nbt: set[tuple[int, int, int]] = set()
        if nbt_data:
            for (gx, gy, gz) in nbt_data:
                lx = gx - min_x
                ly = gy - min_y
                lz = gz - min_z
                if 0 <= lx < size_x and 0 <= ly < size_y and 0 <= lz < size_z:
                    has_nbt.add((lx, ly, lz))

        logger.info(
            "全局网格构建完成: %d x %d x %d, 方块数: %d, NBT位置: %d",
            size_x, size_y, size_z, len(all_blocks), len(has_nbt),
        )

        return GlobalBlockGrid(
            blocks=grid_blocks,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            min_x=min_x,
            min_y=min_y,
            min_z=min_z,
            has_nbt=has_nbt,
        )

    # ------------------------------------------------------------------
    # splitCuboid 算法
    # ------------------------------------------------------------------

    def split_cuboid(self, length: int, height: int, width: int) -> SplitCuboidResult:
        """智能切分长方体, 使每个子块体积不超过MAX_FILL_VOLUME。

        逆向自 fatalder_source/utils/chunk_fill/chunk_fill.go:165-204

        流程:
            1. 计算初始切分尺寸 (基于 sqrt(maxFillVolume / height))
            2. Greedy expansion: 在体积限制内尽可能扩展X和Z

        Args:
            length: X轴长度
            height: Y轴高度
            width: Z轴宽度

        Returns:
            SplitCuboidResult 切分结果。
        """
        l = max(length, 1)
        h = max(height, 1)
        w = max(width, 1)

        # 计算初始切分尺寸
        a1 = int(math.ceil(math.sqrt(float(MAX_FILL_VOLUME) / float(h))))
        a1 = max(a1, 1)

        # ceil_div
        split_l = max(l // ((l + a1 - 1) // a1), 1)
        split_w = max(w // ((w + a1 - 1) // a1), 1)

        # Greedy expansion
        while True:
            can_spread_x = False
            can_spread_z = False

            if split_l < l and h * (split_l + 1) * split_w <= MAX_FILL_VOLUME:
                split_l += 1
                can_spread_x = True

            if split_w < w and h * split_l * (split_w + 1) <= MAX_FILL_VOLUME:
                split_w += 1
                can_spread_z = True

            if not can_spread_x and not can_spread_z:
                break

        return SplitCuboidResult(split_x=split_l, split_y=h, split_z=split_w)

    # ------------------------------------------------------------------
    # X→Z→Y 三轴扩展
    # ------------------------------------------------------------------

    def expand_cuboid(
        self,
        grid: GlobalBlockGrid,
        start_x: int,
        start_y: int,
        start_z: int,
        block: BlockState,
    ) -> tuple[int, int, int, int, int, int]:
        """沿X→Z→Y三轴扩展, 找到最大相同方块矩形。

        逆向自 fatalder_source/utils/chunk_fill/chunk_fill.go:264-337

        Args:
            grid: 全局方块网格
            start_x, start_y, start_z: 起始位置 (相对坐标)
            block: 目标方块状态

        Returns:
            (x1, y1, z1, x2, y2, z2) 扩展后的包围盒 (相对坐标)
        """
        size_x, size_y, size_z = grid.size_x, grid.size_y, grid.size_z

        # 沿X轴扩展
        ex = start_x + 1
        while ex < size_x and self._can_fill_column(grid, ex, ex, start_y, start_y, start_z, start_z, block):
            ex += 1

        # 沿Z轴扩展
        ez = start_z + 1
        while ez < size_z:
            all_match = True
            for xx in range(start_x, ex):
                if not self._can_fill_column(grid, xx, xx, start_y, start_y, ez, ez, block):
                    all_match = False
                    break
            if not all_match:
                break
            ez += 1

        # 沿Y轴扩展
        ey = start_y + 1
        while ey < size_y:
            all_match = True
            for xx in range(start_x, ex):
                if not all_match:
                    break
                for zz in range(start_z, ez):
                    if not self._can_fill_column(grid, xx, xx, ey, ey, zz, zz, block):
                        all_match = False
                        break
            if not all_match:
                break
            ey += 1

        return (start_x, start_y, start_z, ex - 1, ey - 1, ez - 1)

    def _can_fill_column(
        self,
        grid: GlobalBlockGrid,
        x1: int, x2: int,
        y1: int, y2: int,
        z1: int, z2: int,
        block: BlockState,
    ) -> bool:
        """检查指定区域是否全部为指定方块 (且未处理)。

        Args:
            grid: 全局方块网格
            x1, x2: X轴范围
            y1, y2: Y轴范围
            z1, z2: Z轴范围
            block: 目标方块状态

        Returns:
            True 如果区域内所有方块匹配。
        """
        size_x, size_y, size_z = grid.size_x, grid.size_y, grid.size_z
        if x1 < 0 or x2 >= size_x or y1 < 0 or y2 >= size_y or z1 < 0 or z2 >= size_z:
            return False

        for xx in range(x1, x2 + 1):
            for yy in range(y1, y2 + 1):
                for zz in range(z1, z2 + 1):
                    b = grid.blocks[xx][yy][zz]
                    if b.name != block.name or b.states != block.states:
                        return False
        return True

    # ------------------------------------------------------------------
    # 方块名比较
    # ------------------------------------------------------------------

    @staticmethod
    def _blocks_equal(a: BlockState, b: BlockState) -> bool:
        """检查两个方块状态是否相等。"""
        return a.name == b.name and a.states == b.states

    @staticmethod
    def _should_skip_block(
        block: BlockState,
        config: ImportConfig,
    ) -> bool:
        """检查是否应该跳过此方块。

        Args:
            block: 方块状态
            config: 导入配置

        Returns:
            True 如果应该跳过此方块。
        """
        # 跳过空气
        if block.name == AIR_BLOCK_NAME:
            return True

        # No_Import_bar: 跳过屏障方块
        if config.no_import_bar and block.name == BARRIER_BLOCK_NAME:
            return True

        return False

    @staticmethod
    def _is_nbt_block(block: BlockState) -> bool:
        """检查方块是否需要NBT数据。

        NBT方块包括: 容器(箱子/木桶/潜影盒), 命令方块, 告示牌, 结构方块等。
        """
        nbt_block_prefixes = (
            "minecraft:chest",
            "minecraft:trapped_chest",
            "minecraft:barrel",
            "minecraft:shulker_box",
            "minecraft:command_block",
            "minecraft:chain_command_block",
            "minecraft:repeating_command_block",
            "minecraft:sign",
            "minecraft:structure_block",
            "minecraft:jukebox",
            "minecraft:dispenser",
            "minecraft:dropper",
            "minecraft:hopper",
            "minecraft:furnace",
            "minecraft:blast_furnace",
            "minecraft:smoker",
            "minecraft:brewing_stand",
            "minecraft:beacon",
            "minecraft:lectern",
            "minecraft:campfire",
            "minecraft:beehive",
            "minecraft:bee_nest",
            "minecraft:enchanting_table",
            "minecraft:ender_chest",
        )
        return block.name.startswith(nbt_block_prefixes)

    # ------------------------------------------------------------------
    # 核心算法: 生成命令
    # ------------------------------------------------------------------

    def generate_commands(
        self,
        grid: GlobalBlockGrid,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> tuple[list[FillCommand | SetBlockCommand], list[tuple[int, int, int, BlockState]]]:
        """生成 fill/setblock 命令流。

        核心算法, 逆向自 fatalder_source/utils/chunk_fill/chunk_fill.go:264-371

        流程:
            1. 遍历所有方块 (X→Y→Z顺序)
            2. 跳过空气和已处理方块
            3. X→Z→Y三轴扩展找到最大矩形
            4. 体积<=32768: 直接生成fill命令
            5. 体积>32768: splitCuboid切分
            6. NBT方块: 单独生成setblock命令

        Args:
            grid: 全局方块网格
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (普通命令列表, NBT方块列表) 元组。
        """
        size_x, size_y, size_z = grid.size_x, grid.size_y, grid.size_z

        # 已处理标记
        processed: set[tuple[int, int, int]] = set()
        commands: list[FillCommand | SetBlockCommand] = []
        nbt_blocks: list[tuple[int, int, int, BlockState]] = []

        for x in range(size_x):
            for y in range(size_y):
                for z in range(size_z):
                    pos = (x, y, z)
                    if pos in processed:
                        continue

                    block = grid.blocks[x][y][z]

                    # 跳过空气
                    if block.name == AIR_BLOCK_NAME:
                        processed.add(pos)
                        continue

                    # 跳过屏障方块 (No_Import_bar)
                    if self.config.no_import_bar and block.name == BARRIER_BLOCK_NAME:
                        processed.add(pos)
                        continue

                    # NBT方块单独处理
                    if self._is_nbt_block(block):
                        nbt_blocks.append((x + offset_x - grid.min_x, y + offset_y - grid.min_y, z + offset_z - grid.min_z, block))
                        processed.add(pos)
                        continue

                    # 沿X轴扩展
                    ex = x + 1
                    while ex < size_x and self._can_fill_column(grid, ex, ex, y, y, z, z, block):
                        ex += 1

                    # 沿Z轴扩展
                    ez = z + 1
                    while ez < size_z:
                        all_match = True
                        for xx in range(x, ex):
                            if not self._can_fill_column(grid, xx, xx, y, y, ez, ez, block):
                                all_match = False
                                break
                        if not all_match:
                            break
                        ez += 1

                    # 沿Y轴扩展
                    ey = y + 1
                    while ey < size_y:
                        all_match = True
                        for xx in range(x, ex):
                            if not all_match:
                                break
                            for zz in range(z, ez):
                                if not self._can_fill_column(grid, xx, xx, ey, ey, zz, zz, block):
                                    all_match = False
                                    break
                        if not all_match:
                            break
                        ey += 1

                    # 标记已处理
                    for xx in range(x, ex):
                        for yy in range(y, ey):
                            for zz in range(z, ez):
                                processed.add((xx, yy, zz))

                    x1, y1, z1 = x, y, z
                    x2, y2, z2 = ex - 1, ey - 1, ez - 1

                    # 单方块: setblock
                    if x1 == x2 and y1 == y2 and z1 == z2:
                        commands.append(SetBlockCommand(
                            x=x1 + offset_x - grid.min_x,
                            y=y1 + offset_y - grid.min_y,
                            z=z1 + offset_z - grid.min_z,
                            block=block,
                        ))
                        continue

                    # 体积检查
                    volume = (x2 - x1 + 1) * (y2 - y1 + 1) * (z2 - z1 + 1)
                    if volume <= MAX_FILL_VOLUME:
                        commands.append(FillCommand(
                            x1=x1 + offset_x - grid.min_x,
                            y1=y1 + offset_y - grid.min_y,
                            z1=z1 + offset_z - grid.min_z,
                            x2=x2 + offset_x - grid.min_x,
                            y2=y2 + offset_y - grid.min_y,
                            z2=z2 + offset_z - grid.min_z,
                            block=block,
                        ))
                        continue

                    # 体积>32768: splitCuboid切分
                    split_result = self.split_cuboid(x2 - x1 + 1, y2 - y1 + 1, z2 - z1 + 1)
                    split_x = max(split_result.split_x, 1)
                    split_y = max(split_result.split_y, 1)
                    split_z = max(split_result.split_z, 1)

                    for sx in range(x1, x2 + 1, split_x):
                        max_sx = min(sx + split_x - 1, x2)
                        for sy in range(y1, y2 + 1, split_y):
                            max_sy = min(sy + split_y - 1, y2)
                            for sz in range(z1, z2 + 1, split_z):
                                max_sz = min(sz + split_z - 1, z2)
                                commands.append(FillCommand(
                                    x1=sx + offset_x - grid.min_x,
                                    y1=sy + offset_y - grid.min_y,
                                    z1=sz + offset_z - grid.min_z,
                                    x2=max_sx + offset_x - grid.min_x,
                                    y2=max_sy + offset_y - grid.min_y,
                                    z2=max_sz + offset_z - grid.min_z,
                                    block=block,
                                ))

        logger.info(
            "命令生成完成: %d 条命令, %d 个NBT方块",
            len(commands), len(nbt_blocks),
        )

        return commands, nbt_blocks

    # ------------------------------------------------------------------
    # FillFromInnerToOuter 算法
    # ------------------------------------------------------------------

    def generate_commands_inner_to_outer(
        self,
        grid: GlobalBlockGrid,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> tuple[list[FillCommand | SetBlockCommand], list[tuple[int, int, int, BlockState]]]:
        """从建筑中心向外逐层扩展生成命令。

        逆向自 NexusE: FillFromInnerToOuter 算法

        曼哈顿距离排序, 从中心向外逐层放置方块。
        视觉上从内向外扩展, 更适合直播展示。

        Args:
            grid: 全局方块网格
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (普通命令列表, NBT方块列表) 元组。
        """
        size_x, size_y, size_z = grid.size_x, grid.size_y, grid.size_z

        # 计算中心点
        center_x = size_x // 2
        center_y = size_y // 2
        center_z = size_z // 2

        # 按曼哈顿距离排序
        positions: list[tuple[int, int, int, int]] = []  # (x, y, z, distance)
        for x in range(size_x):
            for y in range(size_y):
                for z in range(size_z):
                    dist = abs(x - center_x) + abs(y - center_y) + abs(z - center_z)
                    positions.append((x, y, z, dist))

        positions.sort(key=lambda p: p[3])

        # 按距离分层生成命令
        processed: set[tuple[int, int, int]] = set()
        commands: list[FillCommand | SetBlockCommand] = []
        nbt_blocks: list[tuple[int, int, int, BlockState]] = []

        for x, y, z, _ in positions:
            pos = (x, y, z)
            if pos in processed:
                continue

            block = grid.blocks[x][y][z]
            if block.name == AIR_BLOCK_NAME:
                processed.add(pos)
                continue

            if self._is_nbt_block(block):
                nbt_blocks.append((
                    x + offset_x - grid.min_x,
                    y + offset_y - grid.min_y,
                    z + offset_z - grid.min_z,
                    block,
                ))
                processed.add(pos)
                continue

            # 尝试扩展 (同距离层内)
            # 简化处理: 单方块setblock
            commands.append(SetBlockCommand(
                x=x + offset_x - grid.min_x,
                y=y + offset_y - grid.min_y,
                z=z + offset_z - grid.min_z,
                block=block,
            ))
            processed.add(pos)

        logger.info(
            "InnerToOuter命令生成完成: %d 条命令, %d 个NBT方块",
            len(commands), len(nbt_blocks),
        )

        return commands, nbt_blocks

    # ------------------------------------------------------------------
    # 蛇形算法
    # ------------------------------------------------------------------

    def generate_commands_snake(
        self,
        grid: GlobalBlockGrid,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> tuple[list[FillCommand | SetBlockCommand], list[tuple[int, int, int, BlockState]]]:
        """蛇形路径生成命令。

        逆向自 NovaBuilder: snake_path 算法

        区块内蛇形路径, 减少TP移动距离。

        Args:
            grid: 全局方块网格
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (普通命令列表, NBT方块列表) 元组。
        """
        # 蛇形算法本质上是标准扫描, 但Z方向交替
        size_x, size_y, size_z = grid.size_x, grid.size_y, grid.size_z

        processed: set[tuple[int, int, int]] = set()
        commands: list[FillCommand | SetBlockCommand] = []
        nbt_blocks: list[tuple[int, int, int, BlockState]] = []

        for x in range(size_x):
            z_range = range(size_z) if x % 2 == 0 else range(size_z - 1, -1, -1)
            for z in z_range:
                for y in range(size_y):
                    pos = (x, y, z)
                    if pos in processed:
                        continue

                    block = grid.blocks[x][y][z]
                    if block.name == AIR_BLOCK_NAME:
                        processed.add(pos)
                        continue

                    if self._is_nbt_block(block):
                        nbt_blocks.append((
                            x + offset_x - grid.min_x,
                            y + offset_y - grid.min_y,
                            z + offset_z - grid.min_z,
                            block,
                        ))
                        processed.add(pos)
                        continue

                    # 尝试扩展
                    ex = x + 1
                    while ex < size_x and self._can_fill_column(grid, ex, ex, y, y, z, z, block):
                        ex += 1

                    for xx in range(x, ex):
                        processed.add((xx, y, z))

                    if ex - 1 == x:
                        commands.append(SetBlockCommand(
                            x=x + offset_x - grid.min_x,
                            y=y + offset_y - grid.min_y,
                            z=z + offset_z - grid.min_z,
                            block=block,
                        ))
                    else:
                        commands.append(FillCommand(
                            x1=x + offset_x - grid.min_x,
                            y1=y + offset_y - grid.min_y,
                            z1=z + offset_z - grid.min_z,
                            x2=ex - 1 + offset_x - grid.min_x,
                            y2=y + offset_y - grid.min_y,
                            z2=z + offset_z - grid.min_z,
                            block=block,
                        ))

        return commands, nbt_blocks

    # ------------------------------------------------------------------
    # 智能算法选择
    # ------------------------------------------------------------------

    def generate_commands_auto(
        self,
        grid: GlobalBlockGrid,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> tuple[list[FillCommand | SetBlockCommand], list[tuple[int, int, int, BlockState]]]:
        """智能算法选择。

        根据体积和方块类型自动选择最优算法。

        Args:
            grid: 全局方块网格
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (普通命令列表, NBT方块列表) 元组。
        """
        total_volume = grid.size_x * grid.size_y * grid.size_z

        # 小体积 (< 100万): 直接使用标准扩展
        if total_volume < 1_000_000:
            return self.generate_commands(grid, offset_x, offset_y, offset_z)

        # 大体积: 使用蛇形算法 (内存友好)
        return self.generate_commands_snake(grid, offset_x, offset_y, offset_z)

    def generate_commands_by_algorithm(
        self,
        grid: GlobalBlockGrid,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> tuple[list[FillCommand | SetBlockCommand], list[tuple[int, int, int, BlockState]]]:
        """根据配置的算法生成命令。

        Args:
            grid: 全局方块网格
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (普通命令列表, NBT方块列表) 元组。
        """
        algorithm = self.config.algorithm

        if algorithm == ImportAlgorithm.CUBE_EXPAND:
            return self.generate_commands(grid, offset_x, offset_y, offset_z)
        elif algorithm == ImportAlgorithm.INNER_TO_OUTER:
            return self.generate_commands_inner_to_outer(grid, offset_x, offset_y, offset_z)
        elif algorithm == ImportAlgorithm.SNAKE:
            return self.generate_commands_snake(grid, offset_x, offset_y, offset_z)
        else:  # AUTO
            return self.generate_commands_auto(grid, offset_x, offset_y, offset_z)

    # ------------------------------------------------------------------
    # 修补模式: 差异导入
    # ------------------------------------------------------------------

    def patch_import(
        self,
        grid: GlobalBlockGrid,
        existing_grid: GlobalBlockGrid,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> tuple[list[FillCommand | SetBlockCommand], list[tuple[int, int, int, BlockState]]]:
        """修补模式: 仅导入与现有世界差异的部分。

        逆向自 NexusE: patch_mode

        比较两个网格, 只生成差异方块的命令。

        Args:
            grid: 目标方块网格 (要导入的)
            existing_grid: 现有世界方块网格
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            (普通命令列表, NBT方块列表) 元组。
        """
        size_x = min(grid.size_x, existing_grid.size_x)
        size_y = min(grid.size_y, existing_grid.size_y)
        size_z = min(grid.size_z, existing_grid.size_z)

        commands: list[FillCommand | SetBlockCommand] = []
        nbt_blocks: list[tuple[int, int, int, BlockState]] = []

        diff_count = 0
        for x in range(size_x):
            for y in range(size_y):
                for z in range(size_z):
                    target_block = grid.blocks[x][y][z]
                    existing_block = existing_grid.blocks[x][y][z]

                    if self._blocks_equal(target_block, existing_block):
                        continue

                    diff_count += 1

                    if self._is_nbt_block(target_block):
                        nbt_blocks.append((
                            x + offset_x - grid.min_x,
                            y + offset_y - grid.min_y,
                            z + offset_z - grid.min_z,
                            target_block,
                        ))
                    else:
                        commands.append(SetBlockCommand(
                            x=x + offset_x - grid.min_x,
                            y=y + offset_y - grid.min_y,
                            z=z + offset_z - grid.min_z,
                            block=target_block,
                        ))

        logger.info("修补模式: 发现 %d 个差异方块", diff_count)
        return commands, nbt_blocks

    # ------------------------------------------------------------------
    # Unbuilder模式
    # ------------------------------------------------------------------

    def unbuilder(
        self,
        grid: GlobalBlockGrid,
        offset_x: int = 0,
        offset_y: int = 0,
        offset_z: int = 0,
    ) -> list[FillCommand | SetBlockCommand]:
        """Unbuilder模式: 将magma和water替换为空气。

        逆向自 NexusE: Unbuilder

        用于清除特定方块。

        Args:
            grid: 全局方块网格
            offset_x, offset_y, offset_z: 世界坐标偏移

        Returns:
            清除命令列表。
        """
        air = BlockState(name=AIR_BLOCK_NAME)
        commands: list[FillCommand | SetBlockCommand] = []

        for x in range(grid.size_x):
            for y in range(grid.size_y):
                for z in range(grid.size_z):
                    block = grid.blocks[x][y][z]
                    if block.name in (MAGMA_BLOCK_NAME, WATER_BLOCK_NAME):
                        commands.append(SetBlockCommand(
                            x=x + offset_x - grid.min_x,
                            y=y + offset_y - grid.min_y,
                            z=z + offset_z - grid.min_z,
                            block=air,
                        ))

        logger.info("Unbuilder模式: 生成 %d 条清除命令", len(commands))
        return commands

    # ------------------------------------------------------------------
    # 区块分组 (用于断点续传)
    # ------------------------------------------------------------------

    def filter_chunks_by_start(
        self,
        chunks: list[tuple[int, int]],
    ) -> list[tuple[int, int]]:
        """根据start_chunk过滤区块列表。

        用于断点续传, 跳过已处理的区块。

        Args:
            chunks: 区块坐标列表

        Returns:
            过滤后的区块列表。
        """
        if self.config.start_chunk is None:
            return chunks

        start_cx, start_cz = self.config.start_chunk
        filtered = [(cx, cz) for cx, cz in chunks
                    if cx > start_cx or (cx == start_cx and cz >= start_cz)]

        logger.info(
            "断点续传: 从区块 (%d, %d) 开始, 跳过 %d 个区块",
            start_cx, start_cz, len(chunks) - len(filtered),
        )

        return filtered

    def sort_chunks_snake(self, chunks: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """蛇形排序区块。

        逆向自 Retalcer chunk_painter.py:513-522

        Args:
            chunks: 区块坐标列表

        Returns:
            排序后的区块列表。
        """
        return sorted(chunks, key=lambda c: (c[1], c[0] if c[1] % 2 == 0 else -c[0]))

    def group_chunks(self, chunks: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
        """将区块按chunk_size分组。

        Args:
            chunks: 区块坐标列表

        Returns:
            分组后的区块列表。
        """
        sorted_chunks = self.sort_chunks_snake(chunks)
        group_size = self.config.chunk_size * self.config.chunk_size
        groups = []
        for i in range(0, len(sorted_chunks), group_size):
            groups.append(sorted_chunks[i:i + group_size])
        return groups


__all__ = [
    "CHUNK_SIZE",
    "MAX_FILL_VOLUME",
    "SplitCuboidResult",
    "GlobalBlockGrid",
    "FillCommand",
    "SetBlockCommand",
    "ImportConfig",
    "MultiChunkImporter",
]