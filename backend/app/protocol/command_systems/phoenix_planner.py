"""phoenix_planner - PhoenixBuilder 规划器 (PlacePlan)。

逆向自 PhoenixBuilder 的 PlacePlanner, 来源:
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/builder/builder.go
    - /workspace/phoenixbuilder_source/PhoenixBuilder/fastbuilder/types/block.go

PlacePlanner 职责:
    1. 接收抽象结构 (mcstructure/schematic/bdx) 的方块列表
    2. 按游戏规则排序方块 (自底向上, 自内向外的螺旋顺序)
    3. 计算运行时方块 ID (使用 ToNEMCConvertor)
    4. 输出 PlacePlan (可执行的方块放置计划)

PlacePlan 结构 (逆向自 builder.go):
    type PlacePlan struct {
        Offset    [3]int32          // 整体偏移
        Range     [3]int32          // 范围
        Operations []PlaceOperation  // 操作列表
    }

PlaceOperation 类型 (逆向自 builder.go):
    - PlaceBlock (含 NBT / ChestSlots)
    - PlaceCommandBlock
    - 等

排序算法 (逆向自 builder.go RearrangeBlocks):
    1. 按 Y 升序 (自底向上)
    2. 同 Y 时, 按距中心距离升序 (由内向外)
    3. 同距时, 按索引稳定排序
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Iterator, Optional

logger = logging.getLogger("pocketterm.protocol.command_systems.phoenix_planner")


# -------------------------------------------------------------------- #
# 枚举与常量
# -------------------------------------------------------------------- #


class PlaceOperationType(Enum):
    """放置操作类型 (逆向自 builder.go)。"""
    PLACE_BLOCK = auto()
    PLACE_BLOCK_WITH_STATES = auto()
    PLACE_BLOCK_WITH_NBT = auto()
    PLACE_BLOCK_WITH_CHEST = auto()
    PLACE_BLOCK_WITH_COMMAND_BLOCK = auto()
    PLACE_COMMAND_BLOCK = auto()
    NO_OPERATION = auto()


#: 默认排序方式 (逆向自 builder.go)
SORT_BY_Y_THEN_DISTANCE: str = "y_then_distance"

#: 单区块高度 (Bedrock)
SUBCHUNK_SIZE: int = 16

#: 区块大小
CHUNK_SIZE: int = 16

#: 最大单次操作数 (限流)
MAX_OPERATIONS_PER_TICK: int = 256


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class PlannedBlock:
    """规划后的方块 (逆向自 phoenixbuilder/types/block.go)。

    逆向源码:
        type Block struct {
            Name        string
            States      map[string]interface{}
            Version     int32
            Position    [3]int32
            NBT         map[string]interface{}  // 可选
            ChestSlots  []ChestSlot             // 可选
        }
    """
    name: str = "minecraft:air"
    states: dict[str, Any] = field(default_factory=dict)
    version: int = 0
    position: tuple[int, int, int] = (0, 0, 0)
    nbt: Optional[dict[str, Any]] = None
    chest_slots: list[Any] = field(default_factory=list)
    command_block_data: Optional[Any] = None
    runtime_id: Optional[int] = None

    @property
    def has_nbt(self) -> bool:
        return self.nbt is not None and bool(self.nbt)

    @property
    def has_chest_slots(self) -> bool:
        return bool(self.chest_slots)

    @property
    def has_command_block_data(self) -> bool:
        return self.command_block_data is not None

    def distance_to_center(self, center: tuple[float, float, float]) -> float:
        """计算到中心的距离 (XY 平面)。"""
        dx = self.position[0] - center[0]
        dz = self.position[2] - center[2]
        return math.sqrt(dx * dx + dz * dz)


@dataclass
class PlacePlanItem:
    """PlacePlan 中的一项操作 (逆向自 builder.go PlaceOperation)。

    Attributes:
        operation_type: 操作类型
        block: 关联的方块
        priority: 优先级 (数字越小越优先)
        estimated_runtime_id: 预估的运行时 ID
    """
    operation_type: PlaceOperationType = PlaceOperationType.PLACE_BLOCK
    block: PlannedBlock = field(default_factory=PlannedBlock)
    priority: int = 0
    estimated_runtime_id: Optional[int] = None

    @property
    def position(self) -> tuple[int, int, int]:
        return self.block.position


@dataclass
class PlacePlan:
    """PlacePlan (逆向自 phoenixbuilder/builder/builder.go)。

    逆向源码:
        type PlacePlan struct {
            Offset    [3]int32
            Range     [3]int32
            Operations []PlaceOperation
        }
    """
    offset: tuple[int, int, int] = (0, 0, 0)
    range: tuple[int, int, int] = (0, 0, 0)
    operations: list[PlacePlanItem] = field(default_factory=list)
    center: tuple[float, float, float] = (0.0, 0.0, 0.0)
    total_blocks: int = 0
    non_air_blocks: int = 0

    @property
    def operation_count(self) -> int:
        return len(self.operations)

    def get_blocks_by_y(self, y: int) -> list[PlacePlanItem]:
        """获取指定 Y 坐标的所有操作。"""
        return [op for op in self.operations if op.block.position[1] == y]

    def get_blocks_in_range(
        self,
        min_pos: tuple[int, int, int],
        max_pos: tuple[int, int, int],
    ) -> list[PlacePlanItem]:
        """获取指定范围内的所有操作。"""
        result: list[PlacePlanItem] = []
        for op in self.operations:
            pos = op.block.position
            if (min_pos[0] <= pos[0] <= max_pos[0] and
                    min_pos[1] <= pos[1] <= max_pos[1] and
                    min_pos[2] <= pos[2] <= max_pos[2]):
                result.append(op)
        return result

    def iter_operations(self) -> Iterator[PlacePlanItem]:
        """迭代所有操作。"""
        return iter(self.operations)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "offset": self.offset,
            "range": self.range,
            "center": self.center,
            "total_blocks": self.total_blocks,
            "non_air_blocks": self.non_air_blocks,
            "operation_count": self.operation_count,
        }


# -------------------------------------------------------------------- #
# 规划器
# -------------------------------------------------------------------- #


class PlacePlanner:
    """PhoenixBuilder 规划器 (逆向自 phoenixbuilder/builder/builder.go)。

    使用方式:
        planner = PlacePlanner()
        plan = planner.plan(
            blocks=[
                {"position": (0, 0, 0), "block_name": "minecraft:stone"},
                {"position": (1, 0, 0), "block_name": "minecraft:dirt"},
            ],
            offset=(100, 64, 100),
        )
        for op in plan.iter_operations():
            print(op)
    """

    def __init__(self) -> None:
        self.logger = logging.getLogger("pocketterm.protocol.command_systems.phoenix_planner.planner")

    def plan(
        self,
        blocks: list[dict[str, Any]],
        offset: tuple[int, int, int] = (0, 0, 0),
        sort_method: str = SORT_BY_Y_THEN_DISTANCE,
        skip_air: bool = True,
    ) -> PlacePlan:
        """规划方块放置。

        Args:
            blocks: 方块列表, 每个方块包含:
                - position: (x, y, z) 相对坐标
                - block_name: 方块名 (如 "minecraft:stone")
                - block_states: 方块状态字典 (可选)
                - block_version: 方块版本 (可选)
                - nbt: 方块实体 NBT (可选)
                - chest_slots: 箱子槽位列表 (可选)
            offset: 整体偏移
            sort_method: 排序方式
            skip_air: 是否跳过空气方块

        Returns:
            PlacePlan
        """
        self.logger.info(
            "Planning %d blocks with offset %s, sort=%s",
            len(blocks), offset, sort_method,
        )

        plan = PlacePlan(offset=offset)
        planned_blocks: list[PlannedBlock] = []
        range_min = [math.inf, math.inf, math.inf]
        range_max = [-math.inf, -math.inf, -math.inf]

        for block_data in blocks:
            pos = block_data.get("position", (0, 0, 0))
            if not isinstance(pos, (tuple, list)) or len(pos) != 3:
                continue

            block = PlannedBlock(
                name=block_data.get("block_name", "minecraft:air"),
                states=block_data.get("block_states", {}),
                version=block_data.get("block_version", 0),
                position=(pos[0] + offset[0], pos[1] + offset[1], pos[2] + offset[2]),
                nbt=block_data.get("nbt"),
                chest_slots=block_data.get("chest_slots", []),
                command_block_data=block_data.get("command_block_data"),
            )

            plan.total_blocks += 1

            if skip_air and block.name in ("minecraft:air", "minecraft:air_block"):
                continue

            plan.non_air_blocks += 1
            planned_blocks.append(block)

            # 更新范围
            for i in range(3):
                range_min[i] = min(range_min[i], block.position[i])
                range_max[i] = max(range_max[i], block.position[i])

        # 计算中心
        if planned_blocks:
            plan.center = (
                (range_min[0] + range_max[0]) / 2,
                (range_min[1] + range_max[1]) / 2,
                (range_min[2] + range_max[2]) / 2,
            )
            plan.range = (
                int(range_max[0] - range_min[0] + 1),
                int(range_max[1] - range_min[1] + 1),
                int(range_max[2] - range_min[2] + 1),
            )

        # 排序
        if sort_method == SORT_BY_Y_THEN_DISTANCE:
            planned_blocks = self._sort_by_y_then_distance(planned_blocks, plan.center)
        else:
            self.logger.warning("Unknown sort method: %s, using default", sort_method)

        # 转换为 PlacePlanItem
        for block in planned_blocks:
            op_type = self._determine_operation_type(block)
            plan.operations.append(PlacePlanItem(
                operation_type=op_type,
                block=block,
                priority=self._calculate_priority(block, plan.center),
            ))

        self.logger.info(
            "Plan created: %d operations, range=%s, center=%s",
            plan.operation_count, plan.range, plan.center,
        )
        return plan

    def _sort_by_y_then_distance(
        self,
        blocks: list[PlannedBlock],
        center: tuple[float, float, float],
    ) -> list[PlannedBlock]:
        """按 Y 升序, 然后按到中心距离升序排序 (逆向自 builder.go RearrangeBlocks)。

        逆向源码 (伪代码):
            sort.SliceStable(blocks, func(i, j int) bool {
                a, b := blocks[i], blocks[j]
                if a.Position[1] != b.Position[1] {
                    return a.Position[1] < b.Position[1]  // Y 升序
                }
                da := distToCenter(a.Position, center)
                db := distToCenter(b.Position, center)
                if da != db {
                    return da < db  // 距离升序
                }
                return i < j  // 稳定排序
            })
        """
        # 添加原始索引以保持稳定排序
        indexed_blocks = list(enumerate(blocks))

        def sort_key(item: tuple[int, PlannedBlock]) -> tuple[float, float, int]:
            idx, block = item
            y = block.position[1]
            dist = block.distance_to_center(center)
            return (float(y), dist, idx)

        indexed_blocks.sort(key=sort_key)
        return [b for _, b in indexed_blocks]

    def _determine_operation_type(self, block: PlannedBlock) -> PlaceOperationType:
        """确定方块的操作类型。"""
        if block.has_command_block_data:
            if "command_block" in block.name or "repeating_command_block" in block.name:
                return PlaceOperationType.PLACE_COMMAND_BLOCK
            return PlaceOperationType.PLACE_BLOCK_WITH_COMMAND_BLOCK
        if block.has_chest_slots:
            return PlaceOperationType.PLACE_BLOCK_WITH_CHEST
        if block.has_nbt:
            return PlaceOperationType.PLACE_BLOCK_WITH_NBT
        if block.states:
            return PlaceOperationType.PLACE_BLOCK_WITH_STATES
        return PlaceOperationType.PLACE_BLOCK

    def _calculate_priority(
        self, block: PlannedBlock, center: tuple[float, float, float]
    ) -> int:
        """计算优先级 (数字越小越优先)。

        优先级规则:
            - Y 越小优先级越高 (自底向上)
            - 距中心越近优先级越高 (由内向外)
        """
        y_priority = block.position[1] * 1000
        dist = int(block.distance_to_center(center))
        return y_priority + dist

    def split_by_chunks(
        self, plan: PlacePlan, chunk_size: int = CHUNK_SIZE
    ) -> list[PlacePlan]:
        """按区块切分 PlacePlan (用于 chunk_fill 算法)。

        Args:
            plan: 原 PlacePlan
            chunk_size: 区块大小 (默认 16)

        Returns:
            切分后的 PlacePlan 列表, 每个 PlacePlan 对应一个区块
        """
        chunks: dict[tuple[int, int], list[PlacePlanItem]] = {}

        for op in plan.operations:
            pos = op.block.position
            chunk_x = pos[0] // chunk_size
            chunk_z = pos[2] // chunk_size
            key = (chunk_x, chunk_z)
            chunks.setdefault(key, []).append(op)

        sub_plans: list[PlacePlan] = []
        for (cx, cz), ops in chunks.items():
            sub_plan = PlacePlan(
                offset=(cx * chunk_size, 0, cz * chunk_size),
                operations=ops,
                center=plan.center,
                total_blocks=len(ops),
                non_air_blocks=len(ops),
            )
            sub_plans.append(sub_plan)

        self.logger.info("Split plan into %d chunks", len(sub_plans))
        return sub_plans

    def split_by_subchunks(
        self, plan: PlacePlan, subchunk_size: int = SUBCHUNK_SIZE
    ) -> list[PlacePlan]:
        """按子区块切分 PlacePlan (16x16x16)。"""
        subchunks: dict[tuple[int, int, int], list[PlacePlanItem]] = {}

        for op in plan.operations:
            pos = op.block.position
            sub_x = pos[0] // subchunk_size
            sub_y = pos[1] // subchunk_size
            sub_z = pos[2] // subchunk_size
            key = (sub_x, sub_y, sub_z)
            subchunks.setdefault(key, []).append(op)

        sub_plans: list[PlacePlan] = []
        for (sx, sy, sz), ops in subchunks.items():
            sub_plan = PlacePlan(
                offset=(sx * subchunk_size, sy * subchunk_size, sz * subchunk_size),
                operations=ops,
                center=plan.center,
                total_blocks=len(ops),
                non_air_blocks=len(ops),
            )
            sub_plans.append(sub_plan)

        return sub_plans

    def filter_by_y_range(
        self, plan: PlacePlan, min_y: int, max_y: int
    ) -> PlacePlan:
        """过滤指定 Y 范围的操作。"""
        filtered_ops = [
            op for op in plan.operations
            if min_y <= op.block.position[1] <= max_y
        ]
        return PlacePlan(
            offset=plan.offset,
            range=plan.range,
            operations=filtered_ops,
            center=plan.center,
            total_blocks=len(filtered_ops),
            non_air_blocks=len(filtered_ops),
        )

    def to_bdump_commands(
        self, plan: PlacePlan, use_runtime_ids: bool = True
    ) -> list[Any]:
        """将 PlacePlan 转换为 BDump 命令列表。

        Args:
            plan: PlacePlan
            use_runtime_ids: 是否使用运行时 ID (True 使用 PlaceRuntimeBlock,
                             False 使用 PlaceBlockWithBlockStates)

        Returns:
            BDump 命令列表 (BDumpCommandBase 实例)
        """
        # 延迟导入避免循环依赖
        from .bdump_commands import (
            CreateConstantString, PlaceBlockWithBlockStates,
            PlaceRuntimeBlock, AddInt32XValue, AddInt32YValue, AddInt32ZValue,
            Terminate,
        )

        commands: list[Any] = []
        # 创建字符串池 (方块名 -> 字符串 ID)
        string_pool: dict[str, int] = {}
        next_id = 0

        current_pos: list[int] = [
            plan.operations[0].block.position[0] if plan.operations else 0,
            plan.operations[0].block.position[1] if plan.operations else 0,
            plan.operations[0].block.position[2] if plan.operations else 0,
        ]
        # 起始位置移动 (使用 Int32 移动)
        commands.append(AddInt32XValue(current_pos[0]))
        commands.append(AddInt32YValue(current_pos[1]))
        commands.append(AddInt32ZValue(current_pos[2]))

        for op in plan.operations:
            # 计算相对位移
            target_pos = op.block.position
            dx = target_pos[0] - current_pos[0]
            dy = target_pos[1] - current_pos[1]
            dz = target_pos[2] - current_pos[2]

            if dx != 0:
                commands.append(AddInt32XValue(dx))
                current_pos[0] = target_pos[0]
            if dy != 0:
                commands.append(AddInt32YValue(dy))
                current_pos[1] = target_pos[1]
            if dz != 0:
                commands.append(AddInt32ZValue(dz))
                current_pos[2] = target_pos[2]

            # 注册方块名字符串
            block_name = op.block.name
            if block_name not in string_pool:
                string_pool[block_name] = next_id
                commands.append(CreateConstantString(constant_string=block_name))
                next_id += 1

            if use_runtime_ids and op.block.runtime_id is not None:
                commands.append(PlaceRuntimeBlock(block_runtime_id=op.block.runtime_id))
            else:
                states_str = ""  # 简化处理
                states_id = string_pool.get(states_str, 0)
                commands.append(PlaceBlockWithBlockStates(
                    block_constant_string_id=string_pool[block_name],
                    block_states_constant_string_id=states_id,
                ))

        commands.append(Terminate())
        return commands
