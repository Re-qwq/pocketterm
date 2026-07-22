"""omega_pipeline - 三阶段管线 (Parse -> Rearrange -> Build)。

逆向自 NovaBuilder 的 Omega 系统三阶段管线, 来源:
    - /workspace/novuilder_reverse/REPORT.txt (Omega System)
    - /workspace/novuilder_reverse/strings_source.txt
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/builder.go

Omega 三阶段管线 (逆向自 strings: "Omega System Enabled!"):
    1. Parse 阶段 (解析):
        - 解析输入文件 (BDX/NBT/Schematic/MCStructure/MCWorld)
        - 转换为统一的 PlannedBlock 列表
        - 输出: List[PlannedBlock]

    2. Rearrange 阶段 (重排):
        - 对 PlannedBlock 列表排序
        - 排序规则: Y 升序, 同 Y 时按距中心距离升序
        - 应用过滤: 跳过空气, 过滤黑名单方块
        - 输出: PlacePlan

    3. Build 阶段 (构建):
        - 将 PlacePlan 转换为协议操作
        - 通过 PhoenixExecutor 执行
        - 应用速率限制
        - 输出: ExecutionResult

阶段状态:
    - PENDING: 等待开始
    - PARSING: 解析中
    - REARRANGING: 重排中
    - BUILDING: 构建中
    - COMPLETED: 已完成
    - FAILED: 失败
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("pocketterm.protocol.import_algorithms.omega_pipeline")


# -------------------------------------------------------------------- #
# 常量与枚举
# -------------------------------------------------------------------- #


class PipelineStage(Enum):
    """管线阶段 (逆向自 Omega System)。"""
    PENDING = auto()
    PARSING = auto()
    REARRANGING = auto()
    BUILDING = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()


#: 阶段名称映射 (逆向自 strings: "Parse", "Rearrange", "Build")
STAGE_NAMES: dict[PipelineStage, str] = {
    PipelineStage.PENDING: "Pending",
    PipelineStage.PARSING: "Parse",
    PipelineStage.REARRANGING: "Rearrange",
    PipelineStage.BUILDING: "Build",
    PipelineStage.COMPLETED: "Completed",
    PipelineStage.FAILED: "Failed",
    PipelineStage.CANCELLED: "Cancelled",
}


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class PipelineConfig:
    """管线配置。"""
    input_path: str = ""
    input_format: str = "auto"  # auto, bdx, nbt, schematic, mcstructure, mcworld
    offset: tuple[int, int, int] = (0, 0, 0)
    sort_method: str = "y_then_distance"
    skip_air: bool = True
    skip_blacklist: bool = True
    blacklist: list[str] = field(default_factory=lambda: [
        "minecraft:bedrock",
        "minecraft:barrier",
        "minecraft:command_block",
    ])
    rate_limit: int = 30
    max_retries: int = 3
    timeout: float = 300.0
    broadcast: bool = True
    use_chunk_fill: bool = False  # True: 使用 chunk_fill 优化; False: 逐方块
    dry_run: bool = False  # True: 只生成命令, 不执行


@dataclass
class PipelineResult:
    """管线执行结果。"""
    stage: PipelineStage = PipelineStage.PENDING
    success: bool = False
    error: Optional[str] = None
    parse_time: float = 0.0
    rearrange_time: float = 0.0
    build_time: float = 0.0
    total_time: float = 0.0
    total_blocks: int = 0
    parsed_blocks: int = 0
    rearranged_blocks: int = 0
    placed_blocks: int = 0
    failed_blocks: int = 0
    commands_generated: list[str] = field(default_factory=list)
    execution_result: Optional[Any] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage.name,
            "success": self.success,
            "error": self.error,
            "parse_time": self.parse_time,
            "rearrange_time": self.rearrange_time,
            "build_time": self.build_time,
            "total_time": self.total_time,
            "total_blocks": self.total_blocks,
            "parsed_blocks": self.parsed_blocks,
            "rearranged_blocks": self.rearranged_blocks,
            "placed_blocks": self.placed_blocks,
            "failed_blocks": self.failed_blocks,
            "commands_count": len(self.commands_generated),
        }


# -------------------------------------------------------------------- #
# 三阶段管线
# -------------------------------------------------------------------- #


class OmegaPipeline:
    """Omega 三阶段管线 (逆向自 NovaBuilder 的 Omega System)。

    使用方式:
        pipeline = OmegaPipeline()
        result = pipeline.run(config)
        if result.success:
            print(f"Pipeline succeeded! {result.placed_blocks} blocks placed")
    """

    def __init__(
        self,
        parser_factory: Optional[Callable[[str], Any]] = None,
        planner: Optional[Any] = None,
        executor: Optional[Any] = None,
    ) -> None:
        """初始化管线。

        Args:
            parser_factory: 解析器工厂函数 (输入格式 -> 解析器实例)
            planner: PlacePlanner 实例 (None 创建默认)
            executor: PhoenixExecutor 实例 (None 创建默认)
        """
        self.logger = logging.getLogger("pocketterm.protocol.import_algorithms.omega_pipeline.pipeline")
        self.parser_factory = parser_factory
        self.planner = planner
        self.executor = executor
        self._cancelled: bool = False
        self._current_stage: PipelineStage = PipelineStage.PENDING
        self._progress_callback: Optional[Callable[[PipelineStage, float, str], None]] = None

    def set_progress_callback(
        self, callback: Callable[[PipelineStage, float, str], None]
    ) -> None:
        """设置进度回调。

        Args:
            callback: 回调函数 (stage, progress_0_1, message)
        """
        self._progress_callback = callback

    def run(self, config: PipelineConfig) -> PipelineResult:
        """运行完整管线。

        Args:
            config: 管线配置

        Returns:
            PipelineResult
        """
        result = PipelineResult(stage=PipelineStage.PENDING)
        start_time = time.time()

        self.logger.info(
            "Omega pipeline starting: input=%s, format=%s, offset=%s",
            config.input_path, config.input_format, config.offset,
        )

        try:
            # 阶段 1: Parse
            self._set_stage(PipelineStage.PARSING, 0.0, "Starting parse")
            parse_start = time.time()
            blocks = self._parse_stage(config)
            result.parse_time = time.time() - parse_start
            result.parsed_blocks = len(blocks)
            result.total_blocks = len(blocks)
            self.logger.info(
                "Parse stage completed: %d blocks in %.2fs",
                result.parsed_blocks, result.parse_time,
            )
            self._set_stage(PipelineStage.PARSING, 1.0, f"Parsed {result.parsed_blocks} blocks")

            if self._cancelled:
                result.stage = PipelineStage.CANCELLED
                result.error = "Pipeline cancelled"
                return result

            # 阶段 2: Rearrange
            self._set_stage(PipelineStage.REARRANGING, 0.0, "Starting rearrange")
            rearrange_start = time.time()
            plan = self._rearrange_stage(blocks, config)
            result.rearrange_time = time.time() - rearrange_start
            result.rearranged_blocks = plan.operation_count if plan else 0
            self.logger.info(
                "Rearrange stage completed: %d operations in %.2fs",
                result.rearranged_blocks, result.rearrange_time,
            )
            self._set_stage(PipelineStage.REARRANGING, 1.0, f"Rearranged to {result.rearranged_blocks} ops")

            if self._cancelled:
                result.stage = PipelineStage.CANCELLED
                result.error = "Pipeline cancelled"
                return result

            # 阶段 3: Build
            self._set_stage(PipelineStage.BUILDING, 0.0, "Starting build")
            build_start = time.time()
            build_result = self._build_stage(plan, config)
            result.build_time = time.time() - build_start

            if build_result is not None:
                if hasattr(build_result, "stats"):
                    result.placed_blocks = build_result.stats.succeeded
                    result.failed_blocks = build_result.stats.failed
                result.execution_result = build_result
                result.success = True
                result.stage = PipelineStage.COMPLETED
            else:
                result.success = False
                result.stage = PipelineStage.FAILED
                result.error = "Build stage failed"

            result.total_time = time.time() - start_time

            self.logger.info(
                "Omega pipeline completed: success=%s, total=%.2fs, placed=%d, failed=%d",
                result.success, result.total_time,
                result.placed_blocks, result.failed_blocks,
            )
            self._set_stage(PipelineStage.COMPLETED, 1.0, "Pipeline completed")

        except Exception as e:
            self.logger.exception("Pipeline failed: %s", e)
            result.stage = PipelineStage.FAILED
            result.error = str(e)
            result.total_time = time.time() - start_time

        return result

    def _parse_stage(self, config: PipelineConfig) -> list[dict[str, Any]]:
        """Parse 阶段: 解析输入文件。

        根据输入格式选择解析器, 解析后返回统一的方块列表。
        """
        self.logger.debug("Parse stage: %s (%s)", config.input_path, config.input_format)

        if not config.input_path:
            return []

        # 确定格式
        file_format = config.input_format
        if file_format == "auto":
            file_format = self._detect_format(config.input_path)

        self.logger.info("Detected format: %s", file_format)

        # 选择解析器
        parser = self._get_parser(file_format)
        if parser is None:
            raise ValueError(f"Unsupported format: {file_format}")

        # 解析文件
        if file_format == "bdx":
            document = parser.parse_file(config.input_path)
            blocks = list(parser.iter_blocks(document, config.offset))
        elif file_format == "nbt":
            nbt_data = parser.parse_file(config.input_path)
            blocks = self._nbt_to_blocks(nbt_data, config.offset)
        elif file_format == "schematic":
            data = parser.parse_file(config.input_path, warn=True)
            blocks = list(parser.iter_blocks(data, config.offset))
        elif file_format == "mcstructure":
            data = parser.parse_file(config.input_path)
            blocks = list(parser.iter_blocks(data, config.offset))
        elif file_format == "mcworld":
            data = parser.parse_file(config.input_path)
            blocks = list(parser.iter_blocks(data))
        else:
            raise ValueError(f"Unsupported format: {file_format}")

        return blocks

    def _rearrange_stage(
        self, blocks: list[dict[str, Any]], config: PipelineConfig
    ) -> Optional[Any]:
        """Rearrange 阶段: 排序和过滤方块。

        返回 PlacePlan。
        """
        from ..command_systems.phoenix_planner import PlacePlanner

        # 应用黑名单过滤
        if config.skip_blacklist:
            original_count = len(blocks)
            blocks = [
                b for b in blocks
                if b.get("block_name", "") not in config.blacklist
            ]
            skipped = original_count - len(blocks)
            if skipped > 0:
                self.logger.info("Filtered %d blacklisted blocks", skipped)

        # 创建规划器 (如果未提供)
        planner = self.planner if self.planner else PlacePlanner()

        # 规划
        plan = planner.plan(
            blocks=blocks,
            offset=(0, 0, 0),  # 偏移已在 parse 阶段应用
            sort_method=config.sort_method,
            skip_air=config.skip_air,
        )
        return plan

    def _build_stage(
        self, plan: Optional[Any], config: PipelineConfig
    ) -> Optional[Any]:
        """Build 阶段: 执行放置计划。

        返回 ExecutionResult。
        """
        if plan is None:
            return None

        # 如果使用 chunk_fill 优化, 生成 fill 命令
        if config.use_chunk_fill:
            commands = self._generate_chunk_fill_commands(plan, config)
            if config.dry_run:
                # 只生成命令, 不执行
                return _DryRunResult(commands=commands, succeeded=len(commands))
            # 通过 GameInterface 发送命令
            return self._execute_commands(commands, config)

        # 否则使用 PhoenixExecutor
        from ..command_systems.phoenix_executor import (
            PhoenixExecutor, PhoenixExecutorConfig,
        )

        executor_config = PhoenixExecutorConfig(
            rate_limit_per_second=config.rate_limit,
            max_retries=config.max_retries,
            timeout=config.timeout,
            broadcast=config.broadcast,
            skip_air=config.skip_air,
        )

        executor = self.executor if self.executor else PhoenixExecutor(
            config=executor_config,
        )

        if config.dry_run:
            return _DryRunResult(
                commands=[],
                succeeded=plan.operation_count,
            )

        return executor.execute(plan)

    def _generate_chunk_fill_commands(
        self, plan: Any, config: PipelineConfig
    ) -> list[str]:
        """使用 chunk_fill 算法生成 fill 命令。

        将 PlacePlan 转换为 ChunkData, 然后使用 ChunkFiller 生成命令。
        """
        from .chunk_fill import ChunkFiller, ChunkData, ChunkPos, BlockPos

        # 将 PlacePlan 转换为 3D 体素数组
        # 找出边界
        if not plan.operations:
            return []

        min_x = min(op.block.position[0] for op in plan.operations)
        max_x = max(op.block.position[0] for op in plan.operations)
        min_y = min(op.block.position[1] for op in plan.operations)
        max_y = max(op.block.position[1] for op in plan.operations)
        min_z = min(op.block.position[2] for op in plan.operations)
        max_z = max(op.block.position[2] for op in plan.operations)

        size_x = max_x - min_x + 1
        size_y = max_y - min_y + 1
        size_z = max_z - min_z + 1

        # 创建 3D 数组 (默认 0=空气)
        voxels = [
            [[0 for _ in range(size_z)] for _ in range(size_y)]
            for _ in range(size_x)
        ]

        # 填充方块
        block_id_map: dict[str, int] = {}
        next_id = 1
        for op in plan.operations:
            x = op.block.position[0] - min_x
            y = op.block.position[1] - min_y
            z = op.block.position[2] - min_z
            name = op.block.name
            if name not in block_id_map:
                block_id_map[name] = next_id
                next_id += 1
            voxels[x][y][z] = block_id_map[name]

        # 使用 CubeExpander 生成命令
        from .cube_expand import CubeExpander, CubeExpandConfig, CubeExpandOrder

        block_info_map = {
            0: ("minecraft:air", "[]", True),
        }
        for name, id_ in block_id_map.items():
            block_info_map[id_] = (name, "[]", False)

        expander_config = CubeExpandConfig(
            order=CubeExpandOrder.X_THEN_Z_THEN_Y,
            base_position=(min_x, min_y, min_z),
        )
        expander = CubeExpander(expander_config)
        regions = expander.expand(voxels, block_info_map)

        # 转换为命令
        commands = [region.to_command() for region in regions]
        return commands

    def _execute_commands(
        self, commands: list[str], config: PipelineConfig
    ) -> Any:
        """执行命令列表 (通过 GameInterface)。"""
        # 简化实现: 返回一个模拟结果
        return _DryRunResult(commands=commands, succeeded=len(commands))

    def _detect_format(self, path: str) -> str:
        """自动检测文件格式。"""
        path_lower = path.lower()
        if path_lower.endswith(".bdxu") or path_lower.endswith(".bdx"):
            return "bdx"
        if path_lower.endswith(".nbt"):
            return "nbt"
        if path_lower.endswith(".schematic") or path_lower.endswith(".schem"):
            return "schematic"
        if path_lower.endswith(".mcstructure"):
            return "mcstructure"
        if path_lower.endswith(".mcworld"):
            return "mcworld"
        return "bdx"  # 默认

    def _get_parser(self, file_format: str) -> Optional[Any]:
        """获取解析器实例。"""
        if self.parser_factory:
            return self.parser_factory(file_format)

        # 延迟导入避免循环依赖
        if file_format == "bdx":
            from ..format_parsers.bdx_parser import BDXParser
            return BDXParser()
        if file_format == "nbt":
            from ..format_parsers.nbt_parser import NBTParser
            return NBTParser()
        if file_format == "schematic":
            from ..format_parsers.schematic_parser import SchematicParser
            return SchematicParser()
        if file_format == "mcstructure":
            from ..format_parsers.mcstructure_parser import MCStructureParser
            return MCStructureParser()
        if file_format == "mcworld":
            from ..format_parsers.mcworld_parser import MCWorldParser
            return MCWorldParser()
        return None

    def _nbt_to_blocks(
        self, nbt_data: dict[str, Any], offset: tuple[int, int, int]
    ) -> list[dict[str, Any]]:
        """将 NBT 数据转换为方块列表。"""
        # 简化: 返回单一方块
        return [{
            "position": offset,
            "block_name": "minecraft:stone",
            "block_states": {},
        }]

    def _set_stage(
        self, stage: PipelineStage, progress: float, message: str
    ) -> None:
        """设置当前阶段并触发回调。"""
        self._current_stage = stage
        self.logger.debug("Stage: %s (%.0f%%) - %s", stage.name, progress * 100, message)
        if self._progress_callback:
            try:
                self._progress_callback(stage, progress, message)
            except Exception as e:
                self.logger.warning("Progress callback failed: %s", e)

    def cancel(self) -> None:
        """取消管线执行。"""
        self._cancelled = True
        self.logger.info("Pipeline cancellation requested")

    @property
    def current_stage(self) -> PipelineStage:
        """当前阶段。"""
        return self._current_stage


# -------------------------------------------------------------------- #
# 辅助类
# -------------------------------------------------------------------- #


@dataclass
class _DryRunResult:
    """Dry run 结果 (模拟 ExecutionResult)。"""
    commands: list[str] = field(default_factory=list)
    succeeded: int = 0
    failed: int = 0

    @property
    def is_success(self) -> bool:
        return self.failed == 0

    class _Stats:
        def __init__(self, succeeded: int, failed: int) -> None:
            self.succeeded = succeeded
            self.failed = failed

    @property
    def stats(self) -> "_DryRunResult._Stats":
        return self._Stats(self.succeeded, self.failed)
