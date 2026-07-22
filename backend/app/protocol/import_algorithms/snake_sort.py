"""snake_sort - 蛇形区块排序 (CZ%2)。

逆向自 NexusEgo v1.6.5 的蛇形区块排序算法, 来源:

    - WaterStructure/structure/snake_sort.go
    - import_algo.txt
    - strings: "CZ %% 2" / "snake"

蛇形排序算法用于优化区块遍历顺序:
    1. 将方块按区块分组
    2. 按 (CX, CZ) 排序区块
    3. 当 CZ % 2 == 0 时, CX 递增 (从左到右)
    4. 当 CZ % 2 == 1 时, CX 递减 (从右到左)
    5. 形成蛇形遍历路径

这种算法的优势:
    - 减少区块切换时的距离跳跃
    - 适合大型建筑的逐行构建
    - 在 XZ 平面上形成连续的扫描线

字符串证据 (逆向自 strings_import.txt):
    "CZ %% 2"           -- 区块 Z 坐标取模 2
    "snake"             -- 蛇形
    "sortBlocksSnake"   -- 蛇形方块排序
    "sortBlocksByChunk" -- 按区块排序方块
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .chunk_fill import chunk_coord, CHUNK_SIZE

logger = logging.getLogger("pocketterm.protocol.import_algorithms.snake_sort")


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class SnakeSortError(Exception):
    """蛇形排序错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class SnakeSortConfig:
    """蛇形排序配置。"""
    chunk_size: int = CHUNK_SIZE
    reverse_x_on_odd_z: bool = True  # 奇数 Z 行反向 X
    sort_y_ascending: bool = True    # Y 升序


# -------------------------------------------------------------------- #
# 蛇形排序器
# -------------------------------------------------------------------- #


class SnakeSorter:
    """蛇形区块排序器。

    逆向自 WaterStructure/structure/snake_sort.go 的 sortBlocksSnake。
    """

    def __init__(self, config: SnakeSortConfig | None = None) -> None:
        self.config = config or SnakeSortConfig()
        logger.debug("SnakeSorter initialized: chunk_size=%d",
                      self.config.chunk_size)

    def sort_blocks(self, blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对方块列表进行蛇形排序。

        逆向自 strings: "sortBlocksSnake"。

        Args:
            blocks: 方块列表。

        Returns:
            排序后的方块列表。
        """
        if not blocks:
            return []

        # 1. 按区块分组
        chunk_groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for block in blocks:
            pos = block["position"]
            cx = chunk_coord(pos[0], self.config.chunk_size)
            cz = chunk_coord(pos[2], self.config.chunk_size)
            chunk_groups[(cx, cz)].append(block)

        # 2. 按蛇形顺序遍历区块
        # CZ 升序, CX 在奇数 CZ 行反向
        sorted_chunks = sorted(chunk_groups.keys(), key=lambda c: (c[1], c[0]))

        # 3. 重新排序, 奇数 CZ 行 CX 降序
        snake_chunks: list[tuple[int, int]] = []
        # 按 CZ 分组
        cz_groups: dict[int, list[int]] = defaultdict(list)
        for cx, cz in sorted_chunks:
            cz_groups[cz].append(cx)
        # 蛇形遍历
        for cz in sorted(cz_groups.keys()):
            cxs = sorted(cz_groups[cz])
            if self.config.reverse_x_on_odd_z and cz % 2 == 1:
                cxs = list(reversed(cxs))
            for cx in cxs:
                snake_chunks.append((cx, cz))

        # 4. 在每个区块内, 按 Y 升序 (或降序) 排序方块
        result: list[dict[str, Any]] = []
        for cx, cz in snake_chunks:
            chunk_blocks = chunk_groups[(cx, cz)]
            if self.config.sort_y_ascending:
                chunk_blocks.sort(key=lambda b: b["position"][1])
            else:
                chunk_blocks.sort(key=lambda b: -b["position"][1])
            # 在 Y 相同时, 按 X/Z 排序
            # 蛇形: 在偶数 CZ 中 X 升序, 奇数 CZ 中 X 降序
            if cz % 2 == 1:
                chunk_blocks.sort(key=lambda b: (-b["position"][1], -b["position"][0], b["position"][2]))
            else:
                chunk_blocks.sort(key=lambda b: (b["position"][1], b["position"][0], b["position"][2]))
            result.extend(chunk_blocks)

        logger.info(
            "SnakeSort: blocks=%d, chunks=%d, snake_chunks=%d",
            len(blocks), len(chunk_groups), len(snake_chunks),
        )
        return result

    def sort_chunks(self, chunks: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """对区块坐标列表进行蛇形排序。

        逆向自 strings: "sortBlocksByChunk"。

        Args:
            chunks: 区块坐标列表 [(cx, cz), ...]。

        Returns:
            排序后的区块坐标列表。
        """
        if not chunks:
            return []
        # 按 CZ 分组
        cz_groups: dict[int, list[int]] = defaultdict(list)
        for cx, cz in chunks:
            cz_groups[cz].append(cx)
        # 蛇形遍历
        result: list[tuple[int, int]] = []
        for cz in sorted(cz_groups.keys()):
            cxs = sorted(cz_groups[cz])
            if self.config.reverse_x_on_odd_z and cz % 2 == 1:
                cxs = list(reversed(cxs))
            for cx in cxs:
                result.append((cx, cz))
        return result


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def sort_blocks_snake(blocks: list[dict[str, Any]],
                        config: SnakeSortConfig | None = None) -> list[dict[str, Any]]:
    """蛇形排序方块。

    逆向自 strings: "sortBlocksSnake"。

    Args:
        blocks: 方块列表。
        config: 排序配置。

    Returns:
        排序后的方块列表。
    """
    sorter = SnakeSorter(config)
    return sorter.sort_blocks(blocks)


def sort_chunks_snake(chunks: list[tuple[int, int]],
                        config: SnakeSortConfig | None = None) -> list[tuple[int, int]]:
    """蛇形排序区块坐标。

    Args:
        chunks: 区块坐标列表。
        config: 排序配置。

    Returns:
        排序后的区块坐标列表。
    """
    sorter = SnakeSorter(config)
    return sorter.sort_chunks(chunks)


__all__ = [
    "SnakeSortError",
    "SnakeSortConfig", "SnakeSorter",
    "sort_blocks_snake", "sort_chunks_snake",
]
