"""hop_planner - 16x16x16 HopPos 填充优化。

逆向自 NovaBuilder 的 HopPos 填充优化算法, 来源:
    - /workspace/novuilder_reverse/import_algo.txt
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/builder.go

HopPos 概念:
    将 3D 空间划分为 16x16x16 的立方体单元 (HopPos), 每个 HopPos:
        - 大小: 16x16x16 = 4096 个方块
        - 坐标: HopPos 坐标 (hop_x, hop_y, hop_z) = (world_x // 16, world_y // 16, world_z // 16)
        - 优势: 服务器按子区块加载, 命中同一 HopPos 的方块放置更高效

HopPos 填充优化:
    1. 将 PlacePlan 中的方块按 HopPos 分组
    2. 同一 HopPos 内的方块按 Y 升序, 然后按 X, Z 顺序
    3. 优先填充同一 HopPos 的方块 (减少服务器子区块切换)
    4. 跨 HopPos 时按 HopPos 之间的最优路径排序

HopPos 排序规则:
    - HopPos Y 升序 (自底向上)
    - HopPos X 升序 (从西到东)
    - HopPos Z 升序 (从北到南)
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

logger = logging.getLogger("pocketterm.protocol.import_algorithms.hop_planner")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: HopPos 尺寸 (16x16x16)
HOP_SIZE: int = 16

#: HopPos 体积 (4096)
HOP_VOLUME: int = HOP_SIZE * HOP_SIZE * HOP_SIZE  # 4096

#: 单次最大方块数 (与 chunk_fill 一致)
MAX_BLOCKS_PER_HOP: int = 4096

#: 跳跃阈值 (距离过远时拆分为多个 HopPos)
HOP_SPLIT_THRESHOLD: int = 32


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass(frozen=True)
class HopPos:
    """HopPos 坐标 (16x16x16 立方体单元)。

    属性:
        x: HopPos X 坐标 (world_x // 16)
        y: HopPos Y 坐标 (world_y // 16)
        z: HopPos Z 坐标 (world_z // 16)

    世界坐标范围:
        world_x in [x*16, x*16+15]
        world_y in [y*16, y*16+15]
        world_z in [z*16, z*16+15]
    """
    x: int
    y: int
    z: int

    @classmethod
    def from_world_pos(cls, world_pos: tuple[int, int, int]) -> "HopPos":
        """从世界坐标创建 HopPos。"""
        return cls(
            x=world_pos[0] // HOP_SIZE,
            y=world_pos[1] // HOP_SIZE,
            z=world_pos[2] // HOP_SIZE,
        )

    def to_world_min(self) -> tuple[int, int, int]:
        """转换为世界坐标最小值。"""
        return (self.x * HOP_SIZE, self.y * HOP_SIZE, self.z * HOP_SIZE)

    def to_world_max(self) -> tuple[int, int, int]:
        """转换为世界坐标最大值。"""
        return (
            self.x * HOP_SIZE + HOP_SIZE - 1,
            self.y * HOP_SIZE + HOP_SIZE - 1,
            self.z * HOP_SIZE + HOP_SIZE - 1,
        )

    def contains(self, world_pos: tuple[int, int, int]) -> bool:
        """检查世界坐标是否在此 HopPos 内。"""
        min_pos = self.to_world_min()
        max_pos = self.to_world_max()
        return (
            min_pos[0] <= world_pos[0] <= max_pos[0]
            and min_pos[1] <= world_pos[1] <= max_pos[1]
            and min_pos[2] <= world_pos[2] <= max_pos[2]
        )

    def distance_to(self, other: "HopPos") -> int:
        """计算到另一个 HopPos 的曼哈顿距离。"""
        return abs(self.x - other.x) + abs(self.y - other.y) + abs(self.z - other.z)

    def __iter__(self) -> Iterator[int]:
        yield self.x
        yield self.y
        yield self.z


@dataclass
class HopConfig:
    """HopPos 填充配置。"""
    hop_size: int = HOP_SIZE
    max_blocks_per_hop: int = MAX_BLOCKS_PER_HOP
    sort_within_hop: str = "y_then_x_then_z"
    sort_hops: str = "y_then_x_then_z"
    skip_empty_hops: bool = True


@dataclass
class HopPlan:
    """HopPos 填充计划。"""
    hops: dict[HopPos, list[Any]] = field(default_factory=dict)
    sorted_hops: list[HopPos] = field(default_factory=list)
    total_blocks: int = 0
    total_hops: int = 0

    @property
    def hop_count(self) -> int:
        return len(self.hops)

    def get_blocks_in_hop(self, hop: HopPos) -> list[Any]:
        """获取 HopPos 内的所有方块。"""
        return self.hops.get(hop, [])

    def iter_hops(self) -> Iterator[tuple[HopPos, list[Any]]]:
        """按排序顺序迭代所有 HopPos。"""
        for hop in self.sorted_hops:
            yield hop, self.hops.get(hop, [])


# -------------------------------------------------------------------- #
# HopPlanner
# -------------------------------------------------------------------- #


class HopPlanner:
    """HopPos 填充规划器 (逆向自 NovaBuilder 的 HopPos 算法)。

    使用方式:
        planner = HopPlanner()
        plan = planner.plan(blocks=[
            {"position": (0, 0, 0), "block_name": "minecraft:stone"},
            {"position": (1, 0, 0), "block_name": "minecraft:dirt"},
        ])
        for hop, blocks in plan.iter_hops():
            print(f"Hop {hop}: {len(blocks)} blocks")
    """

    def __init__(self, config: Optional[HopConfig] = None) -> None:
        self.logger = logging.getLogger("pocketterm.protocol.import_algorithms.hop_planner.planner")
        self.config = config if config else HopConfig()

    def plan(self, blocks: list[Any]) -> HopPlan:
        """规划 HopPos 填充。

        Args:
            blocks: 方块列表 (每个方块应包含 position 属性)

        Returns:
            HopPlan
        """
        self.logger.info("Planning HopPos fill for %d blocks", len(blocks))

        plan = HopPlan()

        # 1. 按 HopPos 分组
        for block in blocks:
            pos = self._get_position(block)
            if pos is None:
                continue

            hop = HopPos.from_world_pos(pos)
            if hop not in plan.hops:
                plan.hops[hop] = []
            plan.hops[hop].append(block)
            plan.total_blocks += 1

        # 2. 过滤空 HopPos
        if self.config.skip_empty_hops:
            empty_hops = [h for h, bs in plan.hops.items() if not bs]
            for hop in empty_hops:
                del plan.hops[hop]

        # 3. 排序 HopPos 内的方块
        for hop, hop_blocks in plan.hops.items():
            hop_blocks.sort(key=self._make_sort_key(self.config.sort_within_hop))

        # 4. 排序 HopPos
        plan.sorted_hops = sorted(
            plan.hops.keys(),
            key=self._make_hop_sort_key(self.config.sort_hops),
        )

        plan.total_hops = len(plan.hops)

        self.logger.info(
            "HopPos plan: %d hops, %d blocks",
            plan.total_hops, plan.total_blocks,
        )
        return plan

    def optimize_path(self, plan: HopPlan) -> list[HopPos]:
        """优化 HopPos 路径 (使用最近邻算法)。

        Args:
            plan: HopPlan

        Returns:
            优化后的 HopPos 排序列表
        """
        if not plan.sorted_hops:
            return []

        result: list[HopPos] = []
        remaining: list[HopPos] = list(plan.sorted_hops)

        # 起点为 Y 最小的 HopPos
        remaining.sort(key=lambda h: (h.y, h.x, h.z))
        current = remaining.pop(0)
        result.append(current)

        # 最近邻算法
        while remaining:
            # 找到最近的 HopPos
            min_dist = math.inf
            min_idx = 0
            for i, hop in enumerate(remaining):
                dist = current.distance_to(hop)
                if dist < min_dist:
                    min_dist = dist
                    min_idx = i

            current = remaining.pop(min_idx)
            result.append(current)

        self.logger.info(
            "Optimized path: %d hops (path length=%d)",
            len(result),
            sum(result[i].distance_to(result[i + 1]) for i in range(len(result) - 1)),
        )
        return result

    def split_large_hop(
        self, hop: HopPos, blocks: list[Any], max_size: int = MAX_BLOCKS_PER_HOP
    ) -> list[list[Any]]:
        """切分过大的 HopPos (方块数超过 max_size)。

        Args:
            hop: HopPos
            blocks: HopPos 内的方块列表
            max_size: 单次最大方块数

        Returns:
            切分后的方块列表列表
        """
        if len(blocks) <= max_size:
            return [blocks]

        result: list[list[Any]] = []
        for i in range(0, len(blocks), max_size):
            result.append(blocks[i:i + max_size])
        return result

    def estimate_time(
        self, plan: HopPlan, rate_per_second: int = 30
    ) -> float:
        """估算填充时间 (秒)。"""
        if rate_per_second <= 0:
            return float("inf")
        # 时间 = 方块数 / 速率 + HopPos 切换开销
        block_time = plan.total_blocks / rate_per_second
        hop_switch_time = plan.total_hops * 0.05  # 每次 HopPos 切换 50ms
        return block_time + hop_switch_time

    def _get_position(self, block: Any) -> Optional[tuple[int, int, int]]:
        """从方块获取位置。"""
        if hasattr(block, "position"):
            pos = block.position
            if isinstance(pos, (tuple, list)) and len(pos) == 3:
                return tuple(int(p) for p in pos)
        if isinstance(block, dict):
            pos = block.get("position")
            if isinstance(pos, (tuple, list)) and len(pos) == 3:
                return tuple(int(p) for p in pos)
        return None

    def _make_sort_key(self, method: str) -> Any:
        """创建方块排序键函数。"""
        def key(block: Any) -> tuple:
            pos = self._get_position(block)
            if pos is None:
                return (0, 0, 0)
            if method == "y_then_x_then_z":
                return (pos[1], pos[0], pos[2])
            if method == "x_then_y_then_z":
                return (pos[0], pos[1], pos[2])
            if method == "z_then_x_then_y":
                return (pos[2], pos[0], pos[1])
            # 默认: Y 升序, 然后距中心距离
            return (pos[1], pos[0], pos[2])
        return key

    def _make_hop_sort_key(self, method: str) -> Any:
        """创建 HopPos 排序键函数。"""
        def key(hop: HopPos) -> tuple:
            if method == "y_then_x_then_z":
                return (hop.y, hop.x, hop.z)
            if method == "x_then_y_then_z":
                return (hop.x, hop.y, hop.z)
            if method == "z_then_x_then_y":
                return (hop.z, hop.x, hop.y)
            return (hop.y, hop.x, hop.z)
        return key

    def get_hop_bounds(self, hop: HopPos) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """获取 HopPos 的世界坐标边界。"""
        return (hop.to_world_min(), hop.to_world_max())

    def get_hop_center(self, hop: HopPos) -> tuple[int, int, int]:
        """获取 HopPos 的中心坐标。"""
        min_pos = hop.to_world_min()
        return (
            min_pos[0] + HOP_SIZE // 2,
            min_pos[1] + HOP_SIZE // 2,
            min_pos[2] + HOP_SIZE // 2,
        )

    def find_adjacent_hops(self, hop: HopPos, all_hops: list[HopPos]) -> list[HopPos]:
        """找到与给定 HopPos 相邻的 HopPos (6 个方向)。"""
        all_set = set(all_hops)
        adjacent: list[HopPos] = []
        for dx, dy, dz in [
            (1, 0, 0), (-1, 0, 0),
            (0, 1, 0), (0, -1, 0),
            (0, 0, 1), (0, 0, -1),
        ]:
            neighbor = HopPos(hop.x + dx, hop.y + dy, hop.z + dz)
            if neighbor in all_set:
                adjacent.append(neighbor)
        return adjacent
