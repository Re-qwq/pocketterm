"""导入算法集合。

整合自 NexusEgo 和 NovaBuilder 两个逆向包的导入算法模块。

NexusEgo 来源算法:
    - ChunkFiller:            区块填充算法 (GenerateChunksCommand)
    - IncrementalImporter:    增量导入 (tickingarea + 断点续传)
    - InnerToOuterFiller:     从内到外扩张 (FillFromInnerToOuter)
    - SnakeSorter:            蛇形区块排序 (CZ%2)
    - CuboidSplitter:         立方体分割 (splitCuboid)

NovaBuilder 来源算法:
    - CubeExpander:           立方体扩展 (X->Z->Y 三轴填充顺序)
    - HopPlanner:             HOP 规划器 (16x16x16 HopPos 填充优化)
    - OmegaPipeline:          Omega 流水线 (三阶段管线 Parse->Rearrange->Build)
    - PlaceRateLimiter:       速率限制器 (令牌桶)
"""

from __future__ import annotations

# NexusEgo 来源
from .chunk_fill import (
    ChunkFiller, ChunkFillConfig, ChunkFillResult,
    CHUNK_SIZE, CHUNK_SIZE_X, CHUNK_SIZE_Y, CHUNK_SIZE_Z,
    SUBCHUNK_HEIGHT,
    chunk_coord, count_chunk_span,
    generate_chunks_command, fill_chunks,
    ChunkFillError,
)
from .inner_to_outer import (
    InnerToOuterFiller, InnerToOuterConfig, InnerToOuterResult,
    fill_from_inner_to_outer, spiral_order,
    InnerToOuterError,
)
from .snake_sort import (
    SnakeSorter, SnakeSortConfig,
    sort_blocks_snake, sort_chunks_snake,
    SnakeSortError,
)
from .split_cuboid import (
    CuboidSplitter, CuboidSplitConfig, Cuboid,
    split_cuboid, split_large_fill,
    CuboidSplitError,
)
from .incremental_import import (
    IncrementalImporter, ImportCheckpoint, ImportProgress,
    IncrementalImportConfig,
    create_tickingarea, remove_tickingarea,
    save_checkpoint, load_checkpoint,
    resume_import, IncrementalImportError,
)

# NovaBuilder 来源
from .cube_expand import (
    CubeExpander, CubeExpandOrder, CubeExpandConfig,
)
from .omega_pipeline import (
    OmegaPipeline, PipelineStage, PipelineConfig, PipelineResult,
)
from .hop_planner import (
    HopPlanner, HopPlan, HopConfig, HOP_SIZE, HOP_VOLUME,
)
from .rate_limiter import (
    PlaceRateLimiter, RateLimitConfig, RateLimitResult, TokenBucket,
)

__all__ = [
    # chunk_fill (NexusEgo)
    "ChunkFiller", "ChunkFillConfig", "ChunkFillResult",
    "CHUNK_SIZE", "CHUNK_SIZE_X", "CHUNK_SIZE_Y", "CHUNK_SIZE_Z",
    "SUBCHUNK_HEIGHT",
    "chunk_coord", "count_chunk_span",
    "generate_chunks_command", "fill_chunks",
    "ChunkFillError",
    # inner_to_outer (NexusEgo)
    "InnerToOuterFiller", "InnerToOuterConfig", "InnerToOuterResult",
    "fill_from_inner_to_outer", "spiral_order",
    "InnerToOuterError",
    # snake_sort (NexusEgo)
    "SnakeSorter", "SnakeSortConfig",
    "sort_blocks_snake", "sort_chunks_snake",
    "SnakeSortError",
    # split_cuboid (NexusEgo)
    "CuboidSplitter", "CuboidSplitConfig", "Cuboid",
    "split_cuboid", "split_large_fill",
    "CuboidSplitError",
    # incremental_import (NexusEgo)
    "IncrementalImporter", "ImportCheckpoint", "ImportProgress",
    "IncrementalImportConfig",
    "create_tickingarea", "remove_tickingarea",
    "save_checkpoint", "load_checkpoint",
    "resume_import", "IncrementalImportError",
    # cube_expand (NovaBuilder)
    "CubeExpander", "CubeExpandOrder", "CubeExpandConfig",
    # omega_pipeline (NovaBuilder)
    "OmegaPipeline", "PipelineStage", "PipelineConfig", "PipelineResult",
    # hop_planner (NovaBuilder)
    "HopPlanner", "HopPlan", "HopConfig", "HOP_SIZE", "HOP_VOLUME",
    # rate_limiter (NovaBuilder)
    "PlaceRateLimiter", "RateLimitConfig", "RateLimitResult", "TokenBucket",
]
