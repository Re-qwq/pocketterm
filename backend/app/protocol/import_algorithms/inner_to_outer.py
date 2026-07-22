"""inner_to_outer - 从内向外扩张算法 (FillFromInnerToOuter)。

逆向自 NexusEgo v1.6.5 的从内向外扩张算法, 来源:

    - WaterStructure/structure/fill_inner_outer.go
    - import_algo.txt
    - strings: "FillFromInnerToOuter" / "from inner to outer"

FillFromInnerToOuter 算法用于优化方块放置顺序:
    1. 计算所有方块的中心点
    2. 从中心点开始, 螺旋向外扩张
    3. 按距离排序方块
    4. 依次放置方块

这种算法的优势:
    - 玩家始终能看到正在建造的部分 (中心可见)
    - 减少远距离放置导致的视觉跳跃
    - 适合大型建筑的逐步展示

字符串证据 (逆向自 strings_import.txt):
    "FillFromInnerToOuter"     -- 从内向外填充
    "from inner to outer"      -- 从内到外
    "spiral"                   -- 螺旋
    "center"                   -- 中心点
    "distance"                 -- 距离
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger("pocketterm.protocol.import_algorithms.inner_to_outer")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class InnerToOuterError(Exception):
    """从内向外填充错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class InnerToOuterConfig:
    """从内向外填充配置。"""
    center: tuple[float, float, float] | None = None  # 自定义中心点
    use_spiral: bool = True  # 使用螺旋排序
    spiral_step: int = 1     # 螺旋步长
    max_distance: float = float("inf")  # 最大距离


@dataclass
class InnerToOuterResult:
    """从内向外填充结果。"""
    sorted_blocks: list[dict[str, Any]] = field(default_factory=list)
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    total_blocks: int = 0
    max_distance: float = 0.0
    min_distance: float = 0.0


# -------------------------------------------------------------------- #
# 工具函数
# -------------------------------------------------------------------- #


def calculate_center(blocks: list[dict[str, Any]]) -> tuple[float, float, float]:
    """计算方块列表的中心点。

    使用所有方块坐标的平均值作为中心点。

    Args:
        blocks: 方块列表。

    Returns:
        (cx, cy, cz) 中心点坐标。
    """
    if not blocks:
        return (0.0, 0.0, 0.0)
    total_x = sum(b["position"][0] for b in blocks)
    total_y = sum(b["position"][1] for b in blocks)
    total_z = sum(b["position"][2] for b in blocks)
    count = len(blocks)
    return (total_x / count, total_y / count, total_z / count)


def calculate_distance(pos1: tuple[float, float, float],
                         pos2: tuple[float, float, float]) -> float:
    """计算两点之间的欧几里得距离。

    Args:
        pos1: 第一个点。
        pos2: 第二个点。

    Returns:
        欧几里得距离。
    """
    return math.sqrt(
        (pos1[0] - pos2[0]) ** 2 +
        (pos1[1] - pos2[1]) ** 2 +
        (pos1[2] - pos2[2]) ** 2
    )


def spiral_order(center: tuple[float, float, float],
                   blocks: list[dict[str, Any]],
                   step: int = 1) -> list[dict[str, Any]]:
    """按螺旋顺序排序方块。

    从中心点开始, 按距离螺旋向外排序。
    相同距离的方块按角度排序 (形成螺旋)。

    Args:
        center: 中心点。
        blocks: 方块列表。
        step: 螺旋步长 (每步增加多少距离)。

    Returns:
        排序后的方块列表。
    """
    if not blocks:
        return []

    # 计算每个方块的距离和角度
    block_info: list[tuple[float, float, dict[str, Any]]] = []
    for block in blocks:
        pos = block["position"]
        dx = pos[0] - center[0]
        dy = pos[1] - center[1]
        dz = pos[2] - center[2]
        distance = math.sqrt(dx * dx + dy * dy + dz * dz)
        # 计算角度 (XZ 平面)
        angle = math.atan2(dz, dx)
        # 加上 Y 分量影响 (使 Y 方向也有螺旋)
        if dy != 0:
            angle += math.atan2(dy, math.sqrt(dx * dx + dz * dz)) * 0.5
        block_info.append((distance, angle, block))

    # 按距离分层, 每层按角度排序
    block_info.sort(key=lambda x: (x[0] // step, x[1]))
    return [info[2] for info in block_info]


# -------------------------------------------------------------------- #
# 从内向外填充器
# -------------------------------------------------------------------- #


class InnerToOuterFiller:
    """从内向外填充器。

    逆向自 WaterStructure/structure/fill_inner_outer.go 的 FillFromInnerToOuter。
    """

    def __init__(self, config: InnerToOuterConfig | None = None) -> None:
        self.config = config or InnerToOuterConfig()
        logger.debug("InnerToOuterFiller initialized: use_spiral=%s",
                      self.config.use_spiral)

    def fill(self, blocks: list[dict[str, Any]]) -> InnerToOuterResult:
        """执行从内向外填充。

        Args:
            blocks: 方块列表。

        Returns:
            :class:`InnerToOuterResult`。
        """
        result = InnerToOuterResult()
        result.total_blocks = len(blocks)

        if not blocks:
            return result

        # 计算中心点
        center = self.config.center or calculate_center(blocks)
        result.center = center

        # 计算距离范围
        distances = [
            calculate_distance(b["position"], center) for b in blocks
        ]
        result.min_distance = min(distances) if distances else 0.0
        result.max_distance = max(distances) if distances else 0.0

        # 过滤超出最大距离的方块
        if self.config.max_distance < float("inf"):
            blocks = [
                b for b, d in zip(blocks, distances)
                if d <= self.config.max_distance
            ]

        # 排序
        if self.config.use_spiral:
            result.sorted_blocks = spiral_order(
                center, blocks, self.config.spiral_step
            )
        else:
            # 简单按距离排序
            sorted_pairs = sorted(
                zip(distances, blocks),
                key=lambda x: x[0],
            )
            result.sorted_blocks = [b for _, b in sorted_pairs]

        logger.info(
            "InnerToOuter fill: blocks=%d, center=%s, "
            "distance_range=[%.1f, %.1f]",
            result.total_blocks, center,
            result.min_distance, result.max_distance,
        )
        return result


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def fill_from_inner_to_outer(blocks: list[dict[str, Any]],
                                config: InnerToOuterConfig | None = None
                                ) -> list[dict[str, Any]]:
    """从内向外排序方块。

    逆向自 strings: "FillFromInnerToOuter"。

    Args:
        blocks: 方块列表。
        config: 填充配置。

    Returns:
        排序后的方块列表。
    """
    filler = InnerToOuterFiller(config)
    result = filler.fill(blocks)
    return result.sorted_blocks


__all__ = [
    "InnerToOuterError",
    "InnerToOuterConfig", "InnerToOuterResult", "InnerToOuterFiller",
    "calculate_center", "calculate_distance", "spiral_order",
    "fill_from_inner_to_outer",
]
