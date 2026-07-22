"""cube_expand - 立方体扩展算法 (X→Z→Y 三轴填充顺序)。

逆向自 NovaBuilder 的立方体扩展算法, 来源:
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/geometry.go
    - /workspace/fatalder_source/utils/chunk_fill/chunk_fill.go (X→Z→Y 三轴扫描)

算法核心:
    给定一个 3D 体素数组, 在三轴方向上扩展连续相同方块的范围,
    生成最小数量的 fill 命令。

扩展顺序 (X→Z→Y):
    1. X 轴: 在同一 (y, z) 行内, 找到最长连续相同方块的 X 范围
    2. Z 轴: 在同一 y 平面内, 找到所有行都匹配的 Z 范围
    3. Y 轴: 在所有 (x, z) 都匹配的 Y 范围

与 chunk_fill 的区别:
    - chunk_fill: 处理 ChunkData (Bedrock 区块格式)
    - cube_expand: 处理通用的 3D 体素数组 (任意尺寸)

应用场景:
    - 从 schematic/mcstructure 导入方块
    - 优化 BDX 文件大小 (合并连续方块)
    - 减少服务器数据包数量
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Iterator, Optional

logger = logging.getLogger("pocketterm.protocol.import_algorithms.cube_expand")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 单次 fill 命令的最大方块数 (Minecraft 协议限制)
MAX_FILL_VOLUME: int = 32768

#: 默认填充顺序
DEFAULT_ORDER: tuple[str, str, str] = ("x", "z", "y")


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class CubeExpandOrder(Enum):
    """立方体扩展顺序 (逆向自 geometry.go)。

    X_THEN_Z_THEN_Y: X→Z→Y (默认, Bedrock 友好)
    Y_THEN_X_THEN_Z: Y→X→Z (Java 友好, 自底向上)
    X_THEN_Y_THEN_Z: X→Y→Z (横向优先)
    """
    X_THEN_Z_THEN_Y = auto()
    Y_THEN_X_THEN_Z = auto()
    X_THEN_Y_THEN_Z = auto()

    @classmethod
    def from_tuple(cls, order: tuple[str, str, str]) -> "CubeExpandOrder":
        """从字符串元组创建枚举。"""
        order_str = "_".join(o.upper() for o in order)
        for member in cls:
            if member.name == f"{order_str[0]}_THEN_{order_str[2]}_THEN_{order_str[4]}":
                return member
        return cls.X_THEN_Z_THEN_Y


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class CubeExpandConfig:
    """立方体扩展配置。"""
    order: CubeExpandOrder = CubeExpandOrder.X_THEN_Z_THEN_Y
    max_fill_volume: int = MAX_FILL_VOLUME
    skip_air: bool = True
    use_setblock_for_single: bool = True
    base_position: tuple[int, int, int] = (0, 0, 0)

    @property
    def axes(self) -> tuple[int, int, int]:
        """返回轴索引顺序 (0=X, 1=Y, 2=Z)。"""
        if self.order == CubeExpandOrder.X_THEN_Z_THEN_Y:
            return (0, 2, 1)
        if self.order == CubeExpandOrder.Y_THEN_X_THEN_Z:
            return (1, 0, 2)
        return (0, 1, 2)


@dataclass
class FillRegion:
    """填充区域 (一个 fill 命令对应一个区域)。"""
    min_pos: tuple[int, int, int]
    max_pos: tuple[int, int, int]
    block_name: str = "minecraft:air"
    block_state: str = "[]"
    is_single_block: bool = False
    volume: int = 0

    def __post_init__(self) -> None:
        if self.volume == 0:
            self.volume = (
                (self.max_pos[0] - self.min_pos[0] + 1)
                * (self.max_pos[1] - self.min_pos[1] + 1)
                * (self.max_pos[2] - self.min_pos[2] + 1)
            )

    def to_command(self, base_position: tuple[int, int, int] = (0, 0, 0)) -> str:
        """转换为 fill 或 setblock 命令。"""
        min_x = base_position[0] + self.min_pos[0]
        min_y = base_position[1] + self.min_pos[1]
        min_z = base_position[2] + self.min_pos[2]

        if self.is_single_block:
            return f"setblock {min_x} {min_y} {min_z} {self.block_name} {self.block_state}"

        max_x = base_position[0] + self.max_pos[0]
        max_y = base_position[1] + self.max_pos[1]
        max_z = base_position[2] + self.max_pos[2]
        return (
            f"fill {min_x} {min_y} {min_z} "
            f"{max_x} {max_y} {max_z} "
            f"{self.block_name} {self.block_state}"
        )

    def __repr__(self) -> str:
        return (
            f"FillRegion(min={self.min_pos}, max={self.max_pos}, "
            f"block={self.block_name}, volume={self.volume})"
        )


# -------------------------------------------------------------------- #
# 立方体扩展器
# -------------------------------------------------------------------- #


class CubeExpander:
    """立方体扩展器 (逆向自 PhoenixBuilder geometry.go 的合并逻辑)。

    使用方式:
        expander = CubeExpander()
        # 创建 3D 体素数组 (size_x, size_y, size_z)
        voxels = [[[1 for _ in range(size_z)] for _ in range(size_y)] for _ in range(size_x)]
        regions = expander.expand(voxels, block_name="minecraft:stone")
        for region in regions:
            print(region.to_command())
    """

    def __init__(self, config: Optional[CubeExpandConfig] = None) -> None:
        self.logger = logging.getLogger("pocketterm.protocol.import_algorithms.cube_expand.expander")
        self.config = config if config else CubeExpandConfig()

    def expand(
        self,
        voxels: list[list[list[int]]],
        block_info_map: Optional[dict[int, tuple[str, str, bool]]] = None,
    ) -> list[FillRegion]:
        """扩展 3D 体素数组, 生成最小数量的 fill 区域。

        Args:
            voxels: 3D 体素数组 voxels[x][y][z] -> block_id
            block_info_map: block_id -> (name, state, is_air) 映射

        Returns:
            FillRegion 列表
        """
        if not voxels or not voxels[0] or not voxels[0][0]:
            return []

        size_x = len(voxels)
        size_y = len(voxels[0])
        size_z = len(voxels[0][0])

        self.logger.info(
            "Expanding voxels: size=(%d, %d, %d), order=%s",
            size_x, size_y, size_z, self.config.order.name,
        )

        # 默认方块信息映射
        if block_info_map is None:
            block_info_map = self._default_block_info_map()

        # 标记已处理
        processed: list[list[list[bool]]] = [
            [[False for _ in range(size_z)] for _ in range(size_y)]
            for _ in range(size_x)
        ]

        regions: list[FillRegion] = []
        axes = self.config.axes

        # 按轴顺序遍历
        for axis0 in range(size_x if axes[0] == 0 else (size_y if axes[0] == 1 else size_z)):
            for axis1 in range(size_y if axes[1] == 1 else (size_z if axes[1] == 2 else size_x)):
                for axis2 in range(size_z if axes[2] == 2 else (size_y if axes[2] == 1 else size_x)):
                    # 转换回 (x, y, z)
                    coords = [0, 0, 0]
                    coords[axes[0]] = axis0
                    coords[axes[1]] = axis1
                    coords[axes[2]] = axis2
                    x, y, z = coords

                    if processed[x][y][z]:
                        continue

                    block_id = voxels[x][y][z]
                    name, state, is_air = block_info_map.get(
                        block_id, ("minecraft:air", "[]", True)
                    )

                    if is_air and self.config.skip_air:
                        processed[x][y][z] = True
                        continue

                    # 三轴扩展
                    end = self._expand_region(
                        voxels, processed, x, y, z, block_id, size_x, size_y, size_z
                    )

                    # 标记已处理
                    self._mark_processed(
                        processed, x, y, z, end, size_x, size_y, size_z
                    )

                    ex, ey, ez = end
                    is_single = (x == ex and y == ey and z == ez)
                    region = FillRegion(
                        min_pos=(x, y, z),
                        max_pos=(ex, ey, ez),
                        block_name=name,
                        block_state=state,
                        is_single_block=is_single,
                    )
                    regions.append(region)

        self.logger.info("Expanded to %d regions", len(regions))
        return regions

    def _expand_region(
        self,
        voxels: list[list[list[int]]],
        processed: list[list[list[bool]]],
        x: int, y: int, z: int,
        block_id: int,
        size_x: int, size_y: int, size_z: int,
    ) -> tuple[int, int, int]:
        """三轴扩展区域。

        逆向自 chunk_fill.go 的三轴扫描逻辑:
            1. X 轴扩展
            2. Z 轴扩展
            3. Y 轴扩展
        """
        # X 轴扩展
        ex = x
        while ex + 1 < size_x and self._can_fill(
            voxels, processed, ex + 1, y, z, block_id
        ):
            ex += 1

        # Z 轴扩展
        ez = z
        while ez + 1 < size_z:
            all_match = True
            for xx in range(x, ex + 1):
                if not self._can_fill(
                    voxels, processed, xx, y, ez + 1, block_id
                ):
                    all_match = False
                    break
            if not all_match:
                break
            ez += 1

        # Y 轴扩展
        ey = y
        while ey + 1 < size_y:
            all_match = True
            for xx in range(x, ex + 1):
                if not all_match:
                    break
                for zz in range(z, ez + 1):
                    if not self._can_fill(
                        voxels, processed, xx, ey + 1, zz, block_id
                    ):
                        all_match = False
                        break
            if not all_match:
                break
            ey += 1

        return (ex, ey, ez)

    def _can_fill(
        self,
        voxels: list[list[list[int]]],
        processed: list[list[list[bool]]],
        x: int, y: int, z: int,
        block_id: int,
    ) -> bool:
        """检查是否可以填充。"""
        if not (0 <= x < len(voxels) and 0 <= y < len(voxels[0]) and 0 <= z < len(voxels[0][0])):
            return False
        return not processed[x][y][z] and voxels[x][y][z] == block_id

    def _mark_processed(
        self,
        processed: list[list[list[bool]]],
        x: int, y: int, z: int,
        end: tuple[int, int, int],
        size_x: int, size_y: int, size_z: int,
    ) -> None:
        """标记区域为已处理。"""
        ex, ey, ez = end
        for xx in range(x, ex + 1):
            for yy in range(y, ey + 1):
                for zz in range(z, ez + 1):
                    if 0 <= xx < size_x and 0 <= yy < size_y and 0 <= zz < size_z:
                        processed[xx][yy][zz] = True

    def expand_to_commands(
        self,
        voxels: list[list[list[int]]],
        block_info_map: Optional[dict[int, tuple[str, str, bool]]] = None,
    ) -> list[str]:
        """扩展并生成命令列表。"""
        regions = self.expand(voxels, block_info_map)
        base = self.config.base_position
        return [region.to_command(base) for region in regions]

    def _default_block_info_map(self) -> dict[int, tuple[str, str, bool]]:
        """默认方块信息映射。"""
        return {
            0: ("minecraft:air", "[]", True),
            1: ("minecraft:stone", "[]", False),
            2: ("minecraft:grass", "[]", False),
            3: ("minecraft:dirt", "[]", False),
            4: ("minecraft:cobblestone", "[]", False),
            7: ("minecraft:bedrock", "[]", False),
            17: ("minecraft:log", '[{"name":"old_log_type","value":"oak"}]', False),
            41: ("minecraft:gold_block", "[]", False),
            42: ("minecraft:iron_block", "[]", False),
            49: ("minecraft:obsidian", "[]", False),
            57: ("minecraft:diamond_block", "[]", False),
            89: ("minecraft:glowstone", "[]", False),
            121: ("minecraft:end_stone", "[]", False),
            152: ("minecraft:redstone_block", "[]", False),
            169: ("minecraft:sea_lantern", "[]", False),
        }

    def split_large_region(
        self, region: FillRegion, max_volume: int = MAX_FILL_VOLUME
    ) -> list[FillRegion]:
        """切分过大的区域 (体积超过 max_volume)。

        逆向自 chunk_fill.go 的 splitCuboid 算法。
        """
        if region.volume <= max_volume:
            return [region]

        import math

        l = region.max_pos[0] - region.min_pos[0] + 1
        h = region.max_pos[1] - region.min_pos[1] + 1
        w = region.max_pos[2] - region.min_pos[2] + 1

        if l <= 0:
            l = 1
        if h <= 0:
            h = 1
        if w <= 0:
            w = 1

        # 计算 a1 = ceil(sqrt(max_volume / h))
        a1 = max(1, int(math.ceil(math.sqrt(max_volume / h))))

        def ceil_div(a: int, b: int) -> int:
            if b <= 0:
                return a
            if a <= 0:
                return 0
            return (a + b - 1) // b

        split_l = max(l // ceil_div(l, a1), 1)
        split_w = max(w // ceil_div(w, a1), 1)

        # 循环扩张
        while True:
            can_spread_x = False
            can_spread_z = False

            if split_l < l and h * (split_l + 1) * split_w <= max_volume:
                split_l += 1
                can_spread_x = True

            if split_w < w and h * split_l * (split_w + 1) <= max_volume:
                split_w += 1
                can_spread_z = True

            if not can_spread_x and not can_spread_z:
                break

        # 生成子区域
        sub_regions: list[FillRegion] = []
        x = region.min_pos[0]
        while x <= region.max_pos[0]:
            max_x = min(x + split_l - 1, region.max_pos[0])
            y = region.min_pos[1]
            while y <= region.max_pos[1]:
                max_y = min(y + h - 1, region.max_pos[1])
                z = region.min_pos[2]
                while z <= region.max_pos[2]:
                    max_z = min(z + split_w - 1, region.max_pos[2])
                    sub_region = FillRegion(
                        min_pos=(x, y, z),
                        max_pos=(max_x, max_y, max_z),
                        block_name=region.block_name,
                        block_state=region.block_state,
                        is_single_block=False,
                    )
                    sub_regions.append(sub_region)
                    z += split_w
                y += h
            x += split_l

        return sub_regions
