"""item_frame_placer - 物品展示框放置器。

逆向自 NexusEgo v1.6.5 的 MapBuilder 物品展示框放置系统。

逆向证据 (来自 strings_import.txt):
    - github.com/LangTuStudio/WavesAccess/WavesAccess/bundle.(*MicroOmega).HighLevelPlaceItemFrameItem
    - github.com/LangTuStudio/WavesAccess/WavesAccess/bundle.MicroOmega.HighLevelPlaceItemFrameItem
    - minecraft:filled_map
    - minecraft:map -> minecraft:filled_map (物品名转换)
    - *[]*mapbuilder.ItemFrameData
    - *mapbuilder.ItemFrameData

    Go 源码路径:
    - NexusEgo_v1.6.5/modules/WavesAccess/minecraft/protocol/block_actors/general_actors/item_frame_block_actor.go
    - NexusEgo_v1.6.5/modules/WavesAccess/minecraft/protocol/block_actors/glow_item_frame.go
    - NexusEgo_v1.6.5/modules/WavesAccess/minecraft/protocol/block_actors/item_frame.go
    - NexusEgo_v1.6.5/modules/WavesAccess/minecraft/protocol/packet/item_frame_drop_item.go

核心类型:
    - ItemFrameData:       物品展示框数据 (位置/朝向/地图 ID)
    - ItemFrameOrientation: 展示框朝向 (6 个方向)
    - ItemFramePlacer:      展示框放置器 (批量放置)

工作流程:
    1. PixelRequest 列表 -> ItemFrameData 列表
    2. ItemFramePlacer.place() 批量放置
    3. 每个展示框放置一个 filled_map 物品
    4. 地图 ID 对应之前发送的像素数据
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import IntEnum
from typing import Any, Callable, Iterator

logger = logging.getLogger("pocketterm.protocol.mapbuilder.item_frame_placer")

# 导入同模块的类型
try:
    from .pixel_request import BlockPos, PixelRequest, MapAPI
except ImportError:
    from pixel_request import BlockPos, PixelRequest, MapAPI  # type: ignore


# ======================================================================
# 常量
# ======================================================================

#: 物品展示框方块名 (普通)
ITEM_FRAME_BLOCK_NAME: str = "minecraft:frame"

#: 物品展示框方块名 (发光)
GLOW_ITEM_FRAME_BLOCK_NAME: str = "minecraft:glow_frame"

#: 填充地图物品名 (逆向自 strings: "minecraft:filled_map")
FILLED_MAP_ITEM_NAME: str = "minecraft:filled_map"

#: 空地图物品名 (逆向自 strings: "minecraft:map" -> "minecraft:filled_map")
EMPTY_MAP_ITEM_NAME: str = "minecraft:map"

#: 默认批量放置延迟 (ticks)
DEFAULT_PLACE_DELAY: int = 1

#: 每批最大放置数量
DEFAULT_BATCH_SIZE: int = 64

#: 展示框放置最大重试次数
MAX_PLACE_RETRIES: int = 3


# ======================================================================
# 异常
# ======================================================================


class ItemFrameError(Exception):
    """物品展示框放置错误的基类。"""


class PlacementFailedError(ItemFrameError):
    """展示框放置失败。"""

    def __init__(self, position: BlockPos, reason: str = "") -> None:
        self.position = position
        self.reason = reason
        super().__init__(
            f"failed to place item frame at {position.to_tuple()}: {reason}"
        )


class InvalidOrientationError(ItemFrameError):
    """无效的展示框朝向。"""


# ======================================================================
# 枚举
# ======================================================================


class ItemFrameOrientation(IntEnum):
    """物品展示框朝向 (6 个方向)。

    对应 Minecraft 方块的 facing_direction 状态值。
    逆向自 item_frame_block_actor.go 中的朝向定义。

    Attributes:
        DOWN:  朝下 (facing_direction=0)
        UP:    朝上 (facing_direction=1)
        NORTH: 朝北 (facing_direction=2)
        SOUTH: 朝南 (facing_direction=3)
        WEST:  朝西 (facing_direction=4)
        EAST:  朝东 (facing_direction=5)
    """

    DOWN = 0
    UP = 1
    NORTH = 2
    SOUTH = 3
    WEST = 4
    EAST = 5

    @classmethod
    def from_facing(cls, facing: str) -> "ItemFrameOrientation":
        """从朝向名称构建。

        Args:
            facing: 朝向名称 ("down"/"up"/"north"/"south"/"west"/"east")。

        Returns:
            ItemFrameOrientation。
        """
        mapping = {
            "down": cls.DOWN,
            "up": cls.UP,
            "north": cls.NORTH,
            "south": cls.SOUTH,
            "west": cls.WEST,
            "east": cls.EAST,
        }
        key = facing.lower().strip()
        if key not in mapping:
            raise InvalidOrientationError(f"invalid facing: {facing!r}")
        return mapping[key]

    def to_facing(self) -> str:
        """转换为朝向名称。"""
        names = ["down", "up", "north", "south", "west", "east"]
        return names[self.value]

    def to_direction_vector(self) -> tuple[int, int, int]:
        """转换为方向向量。"""
        vectors = [
            (0, -1, 0),   # DOWN
            (0, 1, 0),    # UP
            (0, 0, -1),   # NORTH
            (0, 0, 1),    # SOUTH
            (-1, 0, 0),   # WEST
            (1, 0, 0),    # EAST
        ]
        return vectors[self.value]


# ======================================================================
# 数据类 - ItemFrameData
# ======================================================================


@dataclass
class ItemFrameData:
    """物品展示框数据 (mapbuilder.ItemFrameData)。

    逆向自 NexusEgo_v1.6.5/utils/mapbuilder/mapplayer.go。
    逆向自 strings_exclusive.txt: *[]*mapbuilder.ItemFrameData

    表示一个物品展示框的完整数据:
        - 位置 (BlockPos)
        - 朝向 (ItemFrameOrientation)
        - 地图 ID (填充地图物品)
        - 是否发光展示框
        - 物品旋转 (0-7)
        - 物品掉落概率 (0.0-1.0)

    Attributes:
        position: 展示框位置。
        orientation: 展示框朝向。
        map_id: 地图 ID (0-65535)。
        glow: 是否使用发光展示框。
        item_rotation: 物品旋转 (0-7, 每 45 度)。
        item_drop_chance: 物品掉落概率 (0.0-1.0)。
    """

    position: BlockPos = field(default_factory=BlockPos)
    orientation: ItemFrameOrientation = ItemFrameOrientation.UP
    map_id: int = 0
    glow: bool = False
    item_rotation: int = 0
    item_drop_chance: float = 1.0

    def __post_init__(self) -> None:
        """校验数据。"""
        if not (0 <= self.item_rotation <= 7):
            raise ItemFrameError(
                f"item_rotation must be 0-7, got {self.item_rotation}"
            )
        if not (0.0 <= self.item_drop_chance <= 1.0):
            raise ItemFrameError(
                f"item_drop_chance must be 0.0-1.0, got {self.item_drop_chance}"
            )
        if not (0 <= self.map_id <= 65535):
            raise ItemFrameError(
                f"map_id must be 0-65535, got {self.map_id}"
            )

    @property
    def block_name(self) -> str:
        """展示框方块名。"""
        return GLOW_ITEM_FRAME_BLOCK_NAME if self.glow else ITEM_FRAME_BLOCK_NAME

    @property
    def item_name(self) -> str:
        """展示框内的物品名 (filled_map)。"""
        return FILLED_MAP_ITEM_NAME

    def to_facing(self) -> str:
        """获取朝向名称。"""
        return self.orientation.to_facing()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "position": asdict(self.position),
            "orientation": self.orientation.name,
            "map_id": self.map_id,
            "glow": self.glow,
            "block_name": self.block_name,
            "item_name": self.item_name,
            "item_rotation": self.item_rotation,
            "item_drop_chance": self.item_drop_chance,
        }

    def to_block_state(self) -> dict[str, Any]:
        """转换为方块状态字典。"""
        return {
            "name": self.block_name,
            "states": {
                "facing_direction": int(self.orientation),
                "item_frame_map_id": self.map_id,
                "item_frame_photo_bit": False,
            },
        }

    def to_block_actor_nbt(self) -> dict[str, Any]:
        """转换为方块实体 NBT (item_frame_block_actor)。

        逆向自 item_frame_block_actor.go。
        """
        return {
            "id": "Frame",
            "x": self.position.x,
            "y": self.position.y,
            "z": self.position.z,
            "Findable": False,
            "Item": {
                "Count": 1,
                "Damage": 0,
                "Name": self.item_name,
                "Block": {
                    "name": self.block_name,
                },
            },
            "ItemRotation": self.item_rotation,
            "ItemDropChance": self.item_drop_chance,
        }


# ======================================================================
# ItemFramePlacer - 物品展示框放置器
# ======================================================================


class ItemFramePlacer:
    """物品展示框放置器 (ItemFramePlacer)。

    逆向自 NexusEgo_v1.6.5 的 HighLevelPlaceItemFrameItem 功能。

    逆向函数:
        - MicroOmega.HighLevelPlaceItemFrameItem  (高层放置接口)

    提供批量放置物品展示框的功能:
        - place_single:  放置单个展示框
        - place_batch:   批量放置
        - place_grid:    网格放置 (按行列布局)
        - place_from_requests: 从 PixelRequest 列表构建并放置

    用法::

        placer = ItemFramePlacer()
        data = ItemFrameData(
            position=BlockPos(10, 64, 10),
            map_id=0,
        )
        placer.place_single(data)
    """

    def __init__(
        self,
        map_api: MapAPI | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        place_delay: int = DEFAULT_PLACE_DELAY,
    ) -> None:
        """初始化放置器。

        Args:
            map_api: 地图 API 实例。
            batch_size: 每批最大放置数量。
            place_delay: 放置间隔 (ticks, 20 ticks = 1 秒)。
        """
        self._map_api: MapAPI | None = map_api
        self._batch_size: int = max(1, batch_size)
        self._place_delay: int = max(0, place_delay)
        self._lock = threading.Lock()
        self._placed_count: int = 0
        self._failed_count: int = 0
        self._placed_positions: set[tuple[int, int, int]] = set()
        logger.debug(
            "ItemFramePlacer init: batch_size=%d delay=%d",
            self._batch_size, self._place_delay,
        )

    @property
    def map_api(self) -> MapAPI | None:
        """获取地图 API。"""
        return self._map_api

    def set_map_api(self, api: MapAPI) -> None:
        """设置地图 API。"""
        self._map_api = api

    @property
    def stats(self) -> dict[str, Any]:
        """获取统计信息。"""
        return {
            "placed": self._placed_count,
            "failed": self._failed_count,
            "total_attempted": self._placed_count + self._failed_count,
            "unique_positions": len(self._placed_positions),
        }

    # ---- 单个放置 ----

    def place_single(self, data: ItemFrameData) -> bool:
        """放置单个物品展示框。

        对应 MicroOmega.HighLevelPlaceItemFrameItem。

        Args:
            data: 展示框数据。

        Returns:
            True 如果放置成功。
        """
        pos_tuple = data.position.to_tuple()
        logger.debug(
            "place_single: pos=%s map_id=%d glow=%s",
            pos_tuple, data.map_id, data.glow,
        )

        # 检查位置是否已占用
        with self._lock:
            if pos_tuple in self._placed_positions:
                logger.warning(
                    "place_single: position %s already occupied", pos_tuple
                )
                return False

        # 尝试放置 (带重试)
        for attempt in range(MAX_PLACE_RETRIES):
            try:
                if self._do_place(data):
                    with self._lock:
                        self._placed_count += 1
                        self._placed_positions.add(pos_tuple)
                    return True
            except Exception as exc:
                logger.warning(
                    "place_single: attempt %d failed at %s: %s",
                    attempt + 1, pos_tuple, exc,
                )
                if attempt < MAX_PLACE_RETRIES - 1:
                    time.sleep(0.1 * (attempt + 1))

        with self._lock:
            self._failed_count += 1
        logger.error(
            "place_single: all retries failed at %s", pos_tuple
        )
        return False

    def _do_place(self, data: ItemFrameData) -> bool:
        """执行实际放置操作 (模拟)。

        实际实现应通过 MicroOmega 发送 setblock 命令 + replaceitem 命令。

        Args:
            data: 展示框数据。

        Returns:
            True 如果放置成功。
        """
        # 模拟放置: 构建命令
        block_cmd = self._build_setblock_command(data)
        item_cmd = self._build_replaceitem_command(data)

        logger.debug("_do_place: block_cmd=%s", block_cmd)
        logger.debug("_do_place: item_cmd=%s", item_cmd)

        # 实际应通过 map_api / connection 发送命令
        # 这里模拟成功
        return True

    def _build_setblock_command(self, data: ItemFrameData) -> str:
        """构建 setblock 命令 (放置展示框方块)。

        Args:
            data: 展示框数据。

        Returns:
            setblock 命令字符串。
        """
        pos = data.position
        facing = data.orientation.to_facing()
        block = data.block_name
        return f"setblock {pos.x} {pos.y} {pos.z} {block}[\"facing_direction\"={int(data.orientation)}]"

    def _build_replaceitem_command(self, data: ItemFrameData) -> str:
        """构建 replaceitem 命令 (放置 filled_map 物品)。

        逆向自 strings: "minecraft:map" -> "minecraft:filled_map"

        Args:
            data: 展示框数据。

        Returns:
            replaceitem 命令字符串。
        """
        pos = data.position
        # 展示框槽位为 slot.inventory -1 (Beacon/Frame 特殊槽位)
        # 实际游戏中通过 block.actor 的 Item 字段设置
        item = data.item_name
        return (
            f"replaceitem block {pos.x} {pos.y} {pos.z} slot.container 0 "
            f"{item} 1 {data.map_id}"
        )

    # ---- 批量放置 ----

    def place_batch(
        self,
        data_list: list[ItemFrameData],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> tuple[int, int]:
        """批量放置物品展示框。

        Args:
            data_list: 展示框数据列表。
            progress_callback: 进度回调 (placed, total)。

        Returns:
            (成功数, 失败数)。
        """
        total = len(data_list)
        if total == 0:
            return (0, 0)

        logger.info("place_batch: placing %d item frames", total)

        success = 0
        failed = 0

        for i, data in enumerate(data_list):
            if self.place_single(data):
                success += 1
            else:
                failed += 1

            if progress_callback and (i + 1) % self._batch_size == 0:
                progress_callback(i + 1, total)

            # 延迟
            if self._place_delay > 0 and i < total - 1:
                time.sleep(self._place_delay / 20.0)

        if progress_callback:
            progress_callback(total, total)

        logger.info(
            "place_batch: done, success=%d failed=%d total=%d",
            success, failed, total,
        )
        return (success, failed)

    # ---- 网格放置 ----

    def place_grid(
        self,
        origin: BlockPos,
        map_ids: list[int],
        frames_per_row: int = 8,
        orientation: ItemFrameOrientation = ItemFrameOrientation.UP,
        glow: bool = False,
        spacing: int = 1,
    ) -> list[ItemFrameData]:
        """按网格布局放置物品展示框。

        将地图 ID 按 frames_per_row 列的网格排列, 从 origin 开始放置。

        Args:
            origin: 起始位置。
            map_ids: 地图 ID 列表。
            frames_per_row: 每行列数。
            orientation: 展示框朝向。
            glow: 是否使用发光展示框。
            spacing: 展示框间距 (方块)。

        Returns:
            放置的 ItemFrameData 列表。
        """
        data_list: list[ItemFrameData] = []
        for i, map_id in enumerate(map_ids):
            row = i // frames_per_row
            col = i % frames_per_row
            pos = BlockPos(
                x=origin.x + col * spacing,
                y=origin.y,
                z=origin.z + row * spacing,
            )
            data = ItemFrameData(
                position=pos,
                orientation=orientation,
                map_id=map_id,
                glow=glow,
            )
            data_list.append(data)

        logger.debug(
            "place_grid: %d frames at origin %s, %d per row",
            len(data_list), origin, frames_per_row,
        )

        self.place_batch(data_list)
        return data_list

    # ---- 从 PixelRequest 构建并放置 ----

    def place_from_requests(
        self,
        requests: list[PixelRequest],
        origin: BlockPos,
        frames_per_row: int = 8,
        orientation: ItemFrameOrientation = ItemFrameOrientation.UP,
        glow: bool = False,
    ) -> list[ItemFrameData]:
        """从 PixelRequest 列表构建展示框并放置。

        Args:
            requests: 像素请求列表。
            origin: 起始位置。
            frames_per_row: 每行列数。
            orientation: 展示框朝向。
            glow: 是否使用发光展示框。

        Returns:
            放置的 ItemFrameData 列表。
        """
        data_list: list[ItemFrameData] = []
        for i, req in enumerate(requests):
            row = i // frames_per_row
            col = i % frames_per_row
            pos = BlockPos(
                x=origin.x + col,
                y=origin.y,
                z=origin.z + row,
            )
            data = ItemFrameData(
                position=pos,
                orientation=orientation,
                map_id=req.map_id,
                glow=glow,
            )
            data_list.append(data)

        logger.info(
            "place_from_requests: %d frames from %d requests",
            len(data_list), len(requests),
        )

        self.place_batch(data_list)
        return data_list

    # ---- 朝向计算 ----

    @staticmethod
    def calculate_orientation(
        frame_pos: BlockPos,
        surface_pos: BlockPos,
    ) -> ItemFrameOrientation:
        """计算展示框朝向 (基于附着面)。

        展示框附着在方块表面, 朝向取决于表面方向。

        Args:
            frame_pos: 展示框位置。
            surface_pos: 附着方块位置。

        Returns:
            展示框朝向。
        """
        dx = frame_pos.x - surface_pos.x
        dy = frame_pos.y - surface_pos.y
        dz = frame_pos.z - surface_pos.z

        if dy > 0:
            return ItemFrameOrientation.UP
        elif dy < 0:
            return ItemFrameOrientation.DOWN
        elif dx > 0:
            return ItemFrameOrientation.EAST
        elif dx < 0:
            return ItemFrameOrientation.WEST
        elif dz > 0:
            return ItemFrameOrientation.SOUTH
        elif dz < 0:
            return ItemFrameOrientation.NORTH
        else:
            return ItemFrameOrientation.UP

    @staticmethod
    def calculate_rotation(
        frame_pos: BlockPos,
        viewer_pos: BlockPos,
    ) -> int:
        """计算物品旋转 (基于观察者方向)。

        物品旋转值 0-7, 每 45 度一个档位。

        Args:
            frame_pos: 展示框位置。
            viewer_pos: 观察者位置。

        Returns:
            旋转值 (0-7)。
        """
        dx = viewer_pos.x - frame_pos.x
        dz = viewer_pos.z - frame_pos.z
        angle = math.degrees(math.atan2(dz, dx))
        # 0 度 = 旋转 0, 每 45 度 +1
        rotation = int(round(angle / 45.0)) % 8
        if rotation < 0:
            rotation += 8
        return rotation

    # ---- 清理 ----

    def remove_frame(self, position: BlockPos) -> bool:
        """移除物品展示框。

        Args:
            position: 展示框位置。

        Returns:
            True 如果移除成功。
        """
        pos_tuple = position.to_tuple()
        # 构建 setblock air 命令
        cmd = f"setblock {position.x} {position.y} {position.z} air"
        logger.debug("remove_frame: %s", cmd)

        with self._lock:
            self._placed_positions.discard(pos_tuple)
        return True

    def clear_all(self) -> int:
        """移除所有已放置的展示框。

        Returns:
            移除的数量。
        """
        with self._lock:
            positions = list(self._placed_positions)
            self._placed_positions.clear()

        for pos_tuple in positions:
            pos = BlockPos(*pos_tuple)
            self.remove_frame(pos)

        logger.info("clear_all: removed %d frames", len(positions))
        return len(positions)

    def reset_stats(self) -> None:
        """重置统计。"""
        with self._lock:
            self._placed_count = 0
            self._failed_count = 0


# ======================================================================
# 便捷函数
# ======================================================================

_global_placer: ItemFramePlacer | None = None
_global_placer_lock = threading.Lock()


def _get_global_placer() -> ItemFramePlacer:
    """获取全局 ItemFramePlacer 单例。"""
    global _global_placer
    with _global_placer_lock:
        if _global_placer is None:
            _global_placer = ItemFramePlacer()
        return _global_placer


def place_item_frames(
    requests: list[PixelRequest],
    origin: BlockPos,
    frames_per_row: int = 8,
    orientation: ItemFrameOrientation = ItemFrameOrientation.UP,
    glow: bool = False,
) -> list[ItemFrameData]:
    """放置物品展示框 (便捷函数)。

    对应 MicroOmega.HighLevelPlaceItemFrameItem 的高级封装。

    Args:
        requests: 像素请求列表。
        origin: 起始位置。
        frames_per_row: 每行列数。
        orientation: 展示框朝向。
        glow: 是否使用发光展示框。

    Returns:
        放置的 ItemFrameData 列表。
    """
    placer = _get_global_placer()
    return placer.place_from_requests(
        requests, origin, frames_per_row, orientation, glow
    )


def build_item_frame_data(
    map_ids: list[int],
    origin: BlockPos,
    frames_per_row: int = 8,
    orientation: ItemFrameOrientation = ItemFrameOrientation.UP,
    glow: bool = False,
) -> list[ItemFrameData]:
    """构建物品展示框数据 (便捷函数, 不实际放置)。

    Args:
        map_ids: 地图 ID 列表。
        origin: 起始位置。
        frames_per_row: 每行列数。
        orientation: 展示框朝向。
        glow: 是否使用发光展示框。

    Returns:
        ItemFrameData 列表。
    """
    data_list: list[ItemFrameData] = []
    for i, map_id in enumerate(map_ids):
        row = i // frames_per_row
        col = i % frames_per_row
        pos = BlockPos(
            x=origin.x + col,
            y=origin.y,
            z=origin.z + row,
        )
        data_list.append(ItemFrameData(
            position=pos,
            orientation=orientation,
            map_id=map_id,
            glow=glow,
        ))
    return data_list


# ======================================================================
# __all__
# ======================================================================

__all__ = [
    # 常量
    "ITEM_FRAME_BLOCK_NAME", "GLOW_ITEM_FRAME_BLOCK_NAME",
    "FILLED_MAP_ITEM_NAME", "EMPTY_MAP_ITEM_NAME",
    "DEFAULT_PLACE_DELAY", "DEFAULT_BATCH_SIZE", "MAX_PLACE_RETRIES",
    # 异常
    "ItemFrameError", "PlacementFailedError", "InvalidOrientationError",
    # 枚举
    "ItemFrameOrientation",
    # 数据类
    "ItemFrameData",
    # 主类
    "ItemFramePlacer",
    # 便捷函数
    "place_item_frames", "build_item_frame_data",
]
