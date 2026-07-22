"""split_cuboid - 大体积切分算法 (splitCuboid)。

逆向自 NexusEgo v1.6.5 的大体积切分算法, 来源:

    - WaterStructure/structure/split_cuboid.go
    - import_algo.txt
    - strings: "splitCuboid" / "fillBounds"

splitCuboid 算法用于将大体积的 fill 操作切分为多个小操作:

    1. 当 fill 操作的体积超过服务器限制时, 需要切分
    2. Minecraft 服务器对单次 fill 操作有体积限制 (通常 32768)
    3. NexusE 将大长方体切分为多个小长方体

切分策略 (逆向自 strings: "splitCuboid"):
    - 优先沿最长轴切分
    - 保持切分后的子长方体大小均衡
    - 支持递归切分 (子长方体仍可切分)

字符串证据 (逆向自 strings_import.txt):
    "splitCuboid"            -- 切分长方体
    "fillBounds"             -- 填充边界
    "fill %d %d %d %d %d %d" -- fill 命令
    "max fill volume"        -- 最大填充体积
    "too large to fill"      -- 太大无法填充
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pocketterm.protocol.import_algorithms.split_cuboid")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: Minecraft 服务器单次 fill 最大体积 (逆向自 strings: "max fill volume")
DEFAULT_MAX_FILL_VOLUME: int = 32768  # 32 * 32 * 32

#: 最小切分尺寸 (低于此尺寸不再切分)
MIN_SPLIT_SIZE: int = 4


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class CuboidSplitError(Exception):
    """长方体切分错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class Cuboid:
    """长方体。"""
    pos1: tuple[int, int, int]  # 最小角
    pos2: tuple[int, int, int]  # 最大角

    @property
    def size(self) -> tuple[int, int, int]:
        """尺寸 (width, height, length)。"""
        return (
            self.pos2[0] - self.pos1[0] + 1,
            self.pos2[1] - self.pos1[1] + 1,
            self.pos2[2] - self.pos1[2] + 1,
        )

    @property
    def volume(self) -> int:
        """体积。"""
        w, h, l = self.size
        return w * h * l

    @property
    def longest_axis(self) -> int:
        """最长轴索引 (0=X, 1=Y, 2=Z)。"""
        w, h, l = self.size
        if w >= h and w >= l:
            return 0
        if h >= w and h >= l:
            return 1
        return 2

    def contains(self, pos: tuple[int, int, int]) -> bool:
        """判断点是否在长方体内。"""
        return (
            self.pos1[0] <= pos[0] <= self.pos2[0] and
            self.pos1[1] <= pos[1] <= self.pos2[1] and
            self.pos1[2] <= pos[2] <= self.pos2[2]
        )

    def __repr__(self) -> str:
        return f"Cuboid({self.pos1} -> {self.pos2}, size={self.size}, vol={self.volume})"


@dataclass
class CuboidSplitConfig:
    """长方体切分配置。"""
    max_volume: int = DEFAULT_MAX_FILL_VOLUME
    min_split_size: int = MIN_SPLIT_SIZE
    prefer_axis: int = -1  # 优先切分轴 (-1 = 自动)


# -------------------------------------------------------------------- #
# 长方体切分器
# -------------------------------------------------------------------- #


class CuboidSplitter:
    """长方体切分器。

    逆向自 WaterStructure/structure/split_cuboid.go 的 splitCuboid。
    """

    def __init__(self, config: CuboidSplitConfig | None = None) -> None:
        self.config = config or CuboidSplitConfig()
        logger.debug(
            "CuboidSplitter initialized: max_volume=%d",
            self.config.max_volume,
        )

    def split(self, cuboid: Cuboid) -> list[Cuboid]:
        """切分长方体。

        递归地将大长方体切分为多个小长方体, 每个小长方体的体积
        不超过 max_volume。

        Args:
            cuboid: 要切分的长方体。

        Returns:
            切分后的长方体列表。
        """
        if cuboid.volume <= self.config.max_volume:
            return [cuboid]

        result: list[Cuboid] = []
        self._split_recursive(cuboid, result)
        logger.info(
            "Cuboid split: %s -> %d cuboids",
            cuboid, len(result),
        )
        return result

    def _split_recursive(self, cuboid: Cuboid,
                            result: list[Cuboid]) -> None:
        """递归切分长方体。"""
        if cuboid.volume <= self.config.max_volume:
            result.append(cuboid)
            return

        # 选择切分轴
        axis = self.config.prefer_axis
        if axis < 0:
            axis = cuboid.longest_axis

        size = cuboid.size[axis]
        if size < self.config.min_split_size * 2:
            # 沿最长轴也无法切分, 尝试其他轴
            for alt_axis in range(3):
                if alt_axis == axis:
                    continue
                if cuboid.size[alt_axis] >= self.config.min_split_size * 2:
                    axis = alt_axis
                    size = cuboid.size[alt_axis]
                    break
            else:
                # 所有轴都太短, 直接添加
                result.append(cuboid)
                return

        # 计算切分点 (中点)
        mid = size // 2
        # 创建两个子长方体
        left, right = self._split_along_axis(cuboid, axis, mid)
        # 递归切分
        self._split_recursive(left, result)
        self._split_recursive(right, result)

    def _split_along_axis(self, cuboid: Cuboid, axis: int,
                            split_point: int) -> tuple[Cuboid, Cuboid]:
        """沿指定轴切分长方体。

        Args:
            cuboid: 要切分的长方体。
            axis: 切分轴 (0=X, 1=Y, 2=Z)。
            split_point: 切分点 (相对于 pos1[axis] 的偏移)。

        Returns:
            (左半部分, 右半部分) 元组。
        """
        pos1 = list(cuboid.pos1)
        pos2 = list(cuboid.pos2)
        # 左半部分: pos1 到 pos1[axis] + split_point - 1
        left_pos2 = list(pos2)
        left_pos2[axis] = pos1[axis] + split_point - 1
        # 右半部分: pos1[axis] + split_point 到 pos2
        right_pos1 = list(pos1)
        right_pos1[axis] = pos1[axis] + split_point

        return (
            Cuboid(tuple(pos1), tuple(left_pos2)),
            Cuboid(tuple(right_pos1), tuple(pos2)),
        )


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def split_cuboid(cuboid: Cuboid,
                   config: CuboidSplitConfig | None = None) -> list[Cuboid]:
    """切分长方体。

    逆向自 strings: "splitCuboid"。

    Args:
        cuboid: 要切分的长方体。
        config: 切分配置。

    Returns:
        切分后的长方体列表。
    """
    splitter = CuboidSplitter(config)
    return splitter.split(cuboid)


def split_large_fill(pos1: tuple[int, int, int],
                       pos2: tuple[int, int, int],
                       block_name: str = "minecraft:stone",
                       block_states: str = "",
                       config: CuboidSplitConfig | None = None
                       ) -> list[dict[str, Any]]:
    """将大体积 fill 操作切分为多个小操作。

    逆向自 strings: "fillBounds" + "splitCuboid"。

    Args:
        pos1: 起点坐标。
        pos2: 终点坐标。
        block_name: 方块名。
        block_states: 方块状态。
        config: 切分配置。

    Returns:
        切分后的 fill 命令数据列表, 每项包含:
            - pos1, pos2, block_name, block_states, volume
    """
    # 确保 pos1 是最小角, pos2 是最大角
    min_pos = (
        min(pos1[0], pos2[0]),
        min(pos1[1], pos2[1]),
        min(pos1[2], pos2[2]),
    )
    max_pos = (
        max(pos1[0], pos2[0]),
        max(pos1[1], pos2[1]),
        max(pos1[2], pos2[2]),
    )
    cuboid = Cuboid(min_pos, max_pos)
    splitter = CuboidSplitter(config)
    sub_cuboids = splitter.split(cuboid)

    result: list[dict[str, Any]] = []
    for sub in sub_cuboids:
        result.append({
            "pos1": sub.pos1,
            "pos2": sub.pos2,
            "block_name": block_name,
            "block_states": block_states,
            "volume": sub.volume,
        })
    return result


__all__ = [
    "DEFAULT_MAX_FILL_VOLUME", "MIN_SPLIT_SIZE",
    "CuboidSplitError",
    "Cuboid", "CuboidSplitConfig", "CuboidSplitter",
    "split_cuboid", "split_large_fill",
]
