"""nbt_placer - NBT 方块放置器 (合并 NovaBuilder + 适配 PocketTerm)。

逆向自 NovaBuilder 的 NBT 方块放置逻辑, 来源:
    - nbt_platform.txt (11x11 平台模式)
    - nbt_timing.txt (放置时序)
    - nbt_packets.txt (数据包序列)
    - PhoenixBuilder/fastbuilder/builder/builder.go

双模式放置 (逆向自 nbt_platform.txt):
    1. STRUCTURE 模式:
        - 在原位置放置方块
        - 使用 BlockEntityData 数据包直接设置 NBT
        - 适用于: 普通服务器 (官方/网易非反作弊)

    2. REPLACEITEM 模式:
        - 先在原位置放置海晶灯 (作为临时方块)
        - 然后在 11x11 范围内放置海晶灯平台 (支撑结构)
        - 使用 InventoryContent 数据包设置 NBT (通过容器操作)
        - 最后清除 11x11 海晶灯平台
        - 适用于: 网易反作弊服务器 (BlockEntityData 被拦截)

11x11 海晶灯平台 (逆向自 nbt_platform.txt):
    平台尺寸: 11x11 (121 个海晶灯)
    平台方块: minecraft:sea_lantern
    平台持续时间: 200 ticks (10 秒)
    平台偏移: 中心在 (target_x - 5, target_y - 1, target_z - 5)
              到 (target_x + 5, target_y - 1, target_z + 5)
    用途: 提供容器物品操作的临时支撑结构

.. important::

    **网易 3.8 协议限制**: replaceitem 命令已被阉割, 只能放耐久/特殊值/数量/
    NBT 标签 (keep_on_death, item_lock), **不能放附魔/自定义名字**。
    因此默认推荐使用 STRUCTURE 模式 (11x11 海晶灯平台 + structure save/load
    搬运), replaceitem 模式仅作为可选保留。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger("pocketterm.protocol.nbt_handler.nbt_placer")


# -------------------------------------------------------------------- #
# 常量 (逆向自 nbt_platform.txt)
# -------------------------------------------------------------------- #

#: 11x11 平台尺寸 (半径 5, 11x11=121 个方块)
PLATFORM_SIZE: int = 11

#: 平台半径
PLATFORM_RADIUS: int = 5

#: 平台方块 (海晶灯)
PLATFORM_BLOCK: str = "minecraft:sea_lantern"

#: 平台方块运行时 ID (Bedrock 默认)
PLATFORM_BLOCK_RUNTIME_ID: int = 169

#: 平台持续时间 (ticks, 1 tick = 50ms)
PLATFORM_DURATION_TICKS: int = 200

#: 平台持续时间 (秒)
PLATFORM_DURATION_SECONDS: float = PLATFORM_DURATION_TICKS / 20

#: 单 tick 最大 NBT 方块数 (网易限制)
MAX_NBT_BLOCKS_PER_TICK: int = 30

#: 单次批量最大方块数
MAX_BLOCKS_PER_BATCH: int = 256

#: NBT 放置冷却 (ms, 防止刷屏)
NBT_PLACE_COOLDOWN_MS: int = 50

#: 失败重试次数
MAX_RETRY_COUNT: int = 3

#: 重试间隔 (ms)
RETRY_INTERVAL_MS: int = 100


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class NBTPlacementMode(Enum):
    """NBT 方块放置模式 (逆向自 nbt_platform.txt)。

    AUTO: 自动检测服务器类型, 选择最佳模式
    STRUCTURE: 直接放置 (使用 BlockEntityData) -- 网易 3.8 推荐
    REPLACEITEM: 替换物品放置 (使用 InventoryContent) -- 3.8 阉割后受限

    .. important::

        **网易 3.8 阉割了 replaceitem 命令**, 只能放耐久/特殊值/数量/NBT 标签
        (keep_on_death, item_lock), 不能放附魔/自定义名字。
        默认推荐 STRUCTURE 模式。
    """
    AUTO = auto()
    STRUCTURE = auto()
    REPLACEITEM = auto()


class PlacementStatus(Enum):
    """放置状态。"""
    PENDING = auto()
    PLACING_PLATFORM = auto()
    PLACING_BLOCK = auto()
    PLACING_NBT = auto()
    CLEARING_PLATFORM = auto()
    SUCCESS = auto()
    FAILED = auto()
    RETRY = auto()


# -------------------------------------------------------------------- #
# 配置
# -------------------------------------------------------------------- #


@dataclass
class PlatformConfig:
    """11x11 海晶灯平台配置 (逆向自 nbt_platform.txt)。

    Attributes:
        size: 平台尺寸 (默认 11)。
        radius: 平台半径 (默认 5)。
        block_name: 平台方块名 (默认 minecraft:sea_lantern)。
        block_runtime_id: 平台方块运行时 ID。
        duration_ticks: 持续时间 (ticks)。
        auto_clear: 是否自动清除平台。
        clear_with_air: 是否用空气清除 (False 用 replaceitem)。
    """
    size: int = PLATFORM_SIZE
    radius: int = PLATFORM_RADIUS
    block_name: str = PLATFORM_BLOCK
    block_runtime_id: int = PLATFORM_BLOCK_RUNTIME_ID
    duration_ticks: int = PLATFORM_DURATION_TICKS
    auto_clear: bool = True
    clear_with_air: bool = True

    @property
    def total_blocks(self) -> int:
        """平台总方块数 (size * size)。"""
        return self.size * self.size

    def get_platform_positions(
        self, center: tuple[int, int, int]
    ) -> list[tuple[int, int, int]]:
        """计算平台所有方块位置。

        Args:
            center: 中心坐标 (目标方块位置)。

        Returns:
            平台方块位置列表 (size * size 个)。
            位置范围:
                x: center[0] - radius 到 center[0] + radius
                y: center[1] - 1 (在目标方块下方)
                z: center[2] - radius 到 center[2] + radius
        """
        positions: list[tuple[int, int, int]] = []
        cx, cy, cz = center
        platform_y = cy - 1  # 平台在目标方块下方
        for dx in range(-self.radius, self.radius + 1):
            for dz in range(-self.radius, self.radius + 1):
                positions.append((cx + dx, platform_y, cz + dz))
        return positions

    def get_platform_bounds(
        self, center: tuple[int, int, int]
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
        """获取平台边界 (min, max)。"""
        cx, cy, cz = center
        platform_y = cy - 1
        return (
            (cx - self.radius, platform_y, cz - self.radius),
            (cx + self.radius, platform_y, cz + self.radius),
        )


@dataclass
class PlacementResult:
    """NBT 放置结果。

    Attributes:
        success: 是否成功。
        mode_used: 使用的放置模式。
        status: 放置状态。
        position: 方块位置。
        elapsed_ms: 耗时 (毫秒)。
        retry_count: 重试次数。
        error: 错误信息。
        platform_cleared: 平台是否已清除。
    """
    success: bool = False
    mode_used: NBTPlacementMode = NBTPlacementMode.STRUCTURE
    status: PlacementStatus = PlacementStatus.PENDING
    position: tuple[int, int, int] = (0, 0, 0)
    elapsed_ms: float = 0.0
    retry_count: int = 0
    error: Optional[str] = None
    platform_cleared: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "success": self.success,
            "mode_used": self.mode_used.name,
            "status": self.status.name,
            "position": self.position,
            "elapsed_ms": self.elapsed_ms,
            "retry_count": self.retry_count,
            "error": self.error,
            "platform_cleared": self.platform_cleared,
        }


# -------------------------------------------------------------------- #
# NBT 方块放置器
# -------------------------------------------------------------------- #


class NBTPlacer:
    """NBT 方块放置器 (逆向自 NovaBuilder 的 nbt_platform 实现)。

    支持双模式:
        - STRUCTURE 模式: 直接 BlockEntityData (网易 3.8 推荐)
        - REPLACEITEM 模式: 11x11 海晶灯平台 + InventoryContent (3.8 受限)

    .. important::

        **网易 3.8 阉割了 replaceitem 命令**, 只能放耐久/特殊值/数量/NBT 标签
        (keep_on_death, item_lock), 不能放附魔/自定义名字。
        默认推荐 STRUCTURE 模式。

    使用方式::

        placer = NBTPlacer(game_interface=interface)
        result = placer.place_block_with_nbt(
            position=(100, 64, 100),
            block_name="minecraft:chest",
            block_states={},
            nbt={"Items": [...]},
            mode=NBTPlacementMode.AUTO,
        )

    Args:
        game_interface: 游戏接口 (GameInterface 实例, None 启用模拟模式)。
        platform_config: 平台配置 (None 使用默认)。
        default_mode: 默认放置模式。
    """

    def __init__(
        self,
        game_interface: Optional[Any] = None,
        platform_config: Optional[PlatformConfig] = None,
        default_mode: NBTPlacementMode = NBTPlacementMode.AUTO,
    ) -> None:
        """初始化 NBT 放置器。

        Args:
            game_interface: 游戏接口 (GameInterface 实例)。
            platform_config: 平台配置 (None 使用默认)。
            default_mode: 默认放置模式。
        """
        self.logger = logging.getLogger(
            "pocketterm.protocol.nbt_handler.nbt_placer.placer"
        )
        self.game_interface = game_interface
        self.platform_config = platform_config if platform_config else PlatformConfig()
        self.default_mode = default_mode
        self._active_platforms: dict[tuple[int, int, int], list[tuple[int, int, int]]] = {}

    def place_block_with_nbt(
        self,
        position: tuple[int, int, int],
        block_name: str,
        block_states: dict[str, Any],
        nbt: dict[str, Any],
        mode: Optional[NBTPlacementMode] = None,
    ) -> PlacementResult:
        """放置带 NBT 的方块。

        Args:
            position: 方块位置 (绝对坐标)。
            block_name: 方块名 (如 "minecraft:chest")。
            block_states: 方块状态。
            nbt: NBT 数据。
            mode: 放置模式 (None 使用默认)。

        Returns:
            :class:`PlacementResult`。
        """
        if mode is None:
            mode = self.default_mode

        if mode == NBTPlacementMode.AUTO:
            mode = self._detect_mode()

        self.logger.info(
            "Placing NBT block at %s, name=%s, mode=%s",
            position, block_name, mode.name,
        )

        start_time = time.time()
        result = PlacementResult(
            mode_used=mode,
            position=position,
        )

        # 重试循环
        for retry in range(MAX_RETRY_COUNT + 1):
            result.retry_count = retry
            try:
                if mode == NBTPlacementMode.STRUCTURE:
                    success = self._place_with_structure_mode(
                        position, block_name, block_states, nbt
                    )
                    result.status = PlacementStatus.PLACING_NBT
                else:
                    success = self._place_with_replaceitem_mode(
                        position, block_name, block_states, nbt
                    )

                if success:
                    result.success = True
                    result.status = PlacementStatus.SUCCESS
                    break

                result.status = PlacementStatus.RETRY
                time.sleep(RETRY_INTERVAL_MS / 1000)
            except Exception as e:
                self.logger.exception("Exception during NBT placement: %s", e)
                result.error = str(e)
                result.status = PlacementStatus.RETRY
                time.sleep(RETRY_INTERVAL_MS / 1000)
        else:
            result.success = False
            result.status = PlacementStatus.FAILED
            if not result.error:
                result.error = f"Failed after {MAX_RETRY_COUNT} retries"

        result.elapsed_ms = (time.time() - start_time) * 1000

        self.logger.info(
            "NBT placement at %s: success=%s, mode=%s, retries=%d, elapsed=%.2fms",
            position, result.success, result.mode_used.name,
            result.retry_count, result.elapsed_ms,
        )
        return result

    def _place_with_structure_mode(
        self,
        position: tuple[int, int, int],
        block_name: str,
        block_states: dict[str, Any],
        nbt: dict[str, Any],
    ) -> bool:
        """STRUCTURE 模式放置 (直接 BlockEntityData)。

        逆向自 nbt_platform.txt 的标准流程:
            1. 发送 UpdateBlock 数据包 (放置方块)
            2. 发送 BlockEntityData 数据包 (设置 NBT)

        .. important::

            网易 3.8 推荐方案, 因为 replaceitem 已被阉割。
        """
        self.logger.debug("STRUCTURE mode: placing block at %s", position)

        if self.game_interface is None:
            self.logger.debug("Simulation mode: success")
            return True

        # 1. 放置方块 (UpdateBlock)
        try:
            runtime_id = self._get_runtime_id(block_name, block_states)
            if runtime_id is None:
                self.logger.error("Failed to get runtime ID for %s", block_name)
                return False

            self.game_interface.send_update_block(
                position=position,
                runtime_id=runtime_id,
            )
        except Exception as e:
            self.logger.error("Failed to send UpdateBlock: %s", e)
            return False

        # 等待一小段时间 (避免服务器未处理完)
        time.sleep(NBT_PLACE_COOLDOWN_MS / 1000)

        # 2. 设置 NBT (BlockEntityData)
        try:
            nbt_with_id = self._wrap_nbt_with_id(nbt, block_name, position)
            self.game_interface.send_block_entity_data(
                position=position,
                nbt=nbt_with_id,
            )
        except Exception as e:
            self.logger.error("Failed to send BlockEntityData: %s", e)
            return False

        return True

    def _place_with_replaceitem_mode(
        self,
        position: tuple[int, int, int],
        block_name: str,
        block_states: dict[str, Any],
        nbt: dict[str, Any],
    ) -> bool:
        """REPLACEITEM 模式放置 (11x11 海晶灯平台 + InventoryContent)。

        逆向自 nbt_platform.txt 的 REPLACEITEM 流程:
            1. 在 position 放置海晶灯 (临时方块)
            2. 在 11x11 范围内放置海晶灯平台
            3. 打开 position 处的容器 (此时是海晶灯, 失败)
               实际: 先放置目标方块, 然后用 InventoryContent
            4. 使用 InventoryContent 数据包设置 NBT (通过容器操作)
            5. 清除 11x11 海晶灯平台

        .. warning::

            网易 3.8 阉割了 replaceitem, 只能放耐久/特殊值/数量/NBT 标签,
            不能放附魔/自定义名字。使用此模式需注意限制。
        """
        self.logger.debug("REPLACEITEM mode: placing block at %s", position)

        if self.game_interface is None:
            self.logger.debug("Simulation mode: success")
            return True

        # 1. 放置目标方块 (UpdateBlock)
        try:
            runtime_id = self._get_runtime_id(block_name, block_states)
            if runtime_id is None:
                self.logger.error("Failed to get runtime ID for %s", block_name)
                return False

            self.game_interface.send_update_block(
                position=position,
                runtime_id=runtime_id,
            )
        except Exception as e:
            self.logger.error("Failed to send UpdateBlock: %s", e)
            return False

        time.sleep(NBT_PLACE_COOLDOWN_MS / 1000)

        # 2. 放置 11x11 海晶灯平台 (在 position 下方)
        platform_positions = self.platform_config.get_platform_positions(position)
        try:
            self._place_platform(platform_positions)
            self._active_platforms[position] = platform_positions
            result_status = PlacementStatus.PLACING_PLATFORM
        except Exception as e:
            self.logger.error("Failed to place platform: %s", e)
            return False

        # 3. 打开容器并设置 NBT
        try:
            container_id = self.game_interface.open_container(position)
            if container_id is None:
                self.logger.error("Container open timeout")
                self._clear_platform(platform_positions)
                return False

            # 4. 使用 InventoryContent 设置 NBT
            nbt_with_id = self._wrap_nbt_with_id(nbt, block_name, position)
            self.game_interface.send_inventory_content(
                container_id=container_id,
                nbt=nbt_with_id,
            )

            # 5. 关闭容器
            self.game_interface.close_container(container_id)
        except Exception as e:
            self.logger.error("Failed to set NBT via container: %s", e)
            self._clear_platform(platform_positions)
            return False

        # 6. 清除 11x11 海晶灯平台
        if self.platform_config.auto_clear:
            try:
                self._clear_platform(platform_positions)
                if position in self._active_platforms:
                    del self._active_platforms[position]
            except Exception as e:
                self.logger.error("Failed to clear platform: %s", e)

        return True

    def _place_platform(
        self, positions: list[tuple[int, int, int]]
    ) -> None:
        """放置 11x11 海晶灯平台。

        使用 UpdateBlock 批量发送 (每 tick 最多 MAX_NBT_BLOCKS_PER_TICK 个)。
        """
        self.logger.debug(
            "Placing platform with %d sea lanterns", len(positions)
        )

        if self.game_interface is None:
            return

        # 批量发送, 每批 MAX_NBT_BLOCKS_PER_TICK 个
        for i, pos in enumerate(positions):
            try:
                self.game_interface.send_update_block(
                    position=pos,
                    runtime_id=self.platform_config.block_runtime_id,
                )
            except Exception as e:
                self.logger.warning("Failed to place platform block at %s: %s", pos, e)

            # 每批暂停一下 (避免服务器拒绝)
            if (i + 1) % MAX_NBT_BLOCKS_PER_TICK == 0:
                time.sleep(0.05)  # 1 tick

    def _clear_platform(
        self, positions: list[tuple[int, int, int]]
    ) -> None:
        """清除 11x11 海晶灯平台 (替换为空气)。"""
        self.logger.debug(
            "Clearing platform with %d blocks", len(positions)
        )

        if self.game_interface is None:
            return

        if self.platform_config.clear_with_air:
            # 使用空气方块清除
            air_runtime_id = 0  # minecraft:air runtime_id = 0
            for i, pos in enumerate(positions):
                try:
                    self.game_interface.send_update_block(
                        position=pos,
                        runtime_id=air_runtime_id,
                    )
                except Exception as e:
                    self.logger.warning("Failed to clear platform block at %s: %s", pos, e)

                if (i + 1) % MAX_NBT_BLOCKS_PER_TICK == 0:
                    time.sleep(0.05)
        else:
            # 使用 /fill 命令清除 (如果有权限)
            if positions:
                min_pos = (
                    min(p[0] for p in positions),
                    min(p[1] for p in positions),
                    min(p[2] for p in positions),
                )
                max_pos = (
                    max(p[0] for p in positions),
                    max(p[1] for p in positions),
                    max(p[2] for p in positions),
                )
                cmd = (
                    f"/fill {min_pos[0]} {min_pos[1]} {min_pos[2]} "
                    f"{max_pos[0]} {max_pos[1]} {max_pos[2]} air"
                )
                try:
                    self.game_interface.send_command(cmd)
                except Exception as e:
                    self.logger.warning("Failed to clear platform with /fill: %s", e)

    def _wrap_nbt_with_id(
        self,
        nbt: dict[str, Any],
        block_name: str,
        position: tuple[int, int, int],
    ) -> dict[str, Any]:
        """为 NBT 添加 id 字段和坐标字段。

        逆向自 nbt_platform.txt:
            BlockEntityData 需要包含:
                - id: 方块实体 ID (如 "Chest", "Sign", "Beacon")
                - x, y, z: 方块坐标
                - 其他业务字段
        """
        # 从 block_name 提取 id (minecraft:chest -> Chest)
        block_id = block_name.replace("minecraft:", "").replace("_", " ").title().replace(" ", "")
        if block_id == "Air":
            block_id = ""

        result = dict(nbt)  # 浅拷贝
        result.setdefault("id", block_id)
        result["x"] = position[0]
        result["y"] = position[1]
        result["z"] = position[2]
        return result

    def _detect_mode(self) -> NBTPlacementMode:
        """自动检测服务器类型, 选择最佳放置模式。

        检测逻辑 (逆向自 nbt_mode_selector):
            - 如果服务器是网易中国版: 使用 REPLACEITEM (3.8 阉割后受限)
            - 如果服务器是官方: 使用 STRUCTURE (网易 3.8 推荐)
            - 如果无法判断: 使用 STRUCTURE (默认, 网易 3.8 推荐)

        .. important::

            网易 3.8 阉割了 replaceitem, 默认推荐 STRUCTURE 模式。
        """
        if self.game_interface is None:
            return NBTPlacementMode.STRUCTURE

        # 检测服务器类型
        server_type = getattr(self.game_interface, "server_type", "unknown")
        if server_type in ("netease", "china", "163"):
            self.logger.debug("Detected NetEase server, using REPLACEITEM mode")
            return NBTPlacementMode.REPLACEITEM
        return NBTPlacementMode.STRUCTURE

    def _get_runtime_id(
        self, block_name: str, block_states: dict[str, Any]
    ) -> Optional[int]:
        """获取方块的运行时 ID。

        通过 ToNEMCConvertor 转换 (网易服务器) 或通过 GameInterface (官方服务器)。
        """
        if self.game_interface is None:
            return None

        convertor = getattr(self.game_interface, "block_convertor", None)
        if convertor is None:
            return None

        try:
            return convertor.convert(block_name, block_states)
        except Exception as e:
            self.logger.warning("Failed to convert block: %s", e)
            return None

    def batch_place(
        self,
        blocks: list[dict[str, Any]],
        mode: Optional[NBTPlacementMode] = None,
        progress_callback: Optional[Any] = None,
    ) -> list[PlacementResult]:
        """批量放置带 NBT 的方块。

        Args:
            blocks: 方块列表, 每个包含:
                - position: (x, y, z) 绝对坐标
                - block_name: 方块名
                - block_states: 方块状态
                - nbt: NBT 数据
            mode: 放置模式 (None 使用默认)。
            progress_callback: 进度回调 (current, total)。

        Returns:
            :class:`PlacementResult` 列表。
        """
        if mode is None:
            mode = self.default_mode

        if mode == NBTPlacementMode.AUTO:
            mode = self._detect_mode()

        results: list[PlacementResult] = []
        total = len(blocks)

        self.logger.info("Batch placing %d NBT blocks, mode=%s", total, mode.name)

        for i, block_data in enumerate(blocks):
            pos = block_data.get("position", (0, 0, 0))
            name = block_data.get("block_name", "minecraft:air")
            states = block_data.get("block_states", {})
            nbt = block_data.get("nbt", {})

            result = self.place_block_with_nbt(pos, name, states, nbt, mode)
            results.append(result)

            if progress_callback:
                try:
                    progress_callback(i + 1, total)
                except Exception:
                    pass

            # 速率限制
            time.sleep(NBT_PLACE_COOLDOWN_MS / 1000)

        succeeded = sum(1 for r in results if r.success)
        self.logger.info(
            "Batch placed %d/%d blocks successfully",
            succeeded, total,
        )
        return results

    def clear_all_platforms(self) -> None:
        """清除所有活动平台 (紧急清理)。"""
        self.logger.warning(
            "Clearing %d active platforms", len(self._active_platforms)
        )
        for pos, positions in list(self._active_platforms.items()):
            try:
                self._clear_platform(positions)
            except Exception as e:
                self.logger.error("Failed to clear platform at %s: %s", pos, e)
        self._active_platforms.clear()


__all__ = [
    # 常量
    "PLATFORM_SIZE", "PLATFORM_RADIUS", "PLATFORM_BLOCK",
    "PLATFORM_BLOCK_RUNTIME_ID", "PLATFORM_DURATION_TICKS",
    "PLATFORM_DURATION_SECONDS", "MAX_NBT_BLOCKS_PER_TICK",
    "MAX_BLOCKS_PER_BATCH", "NBT_PLACE_COOLDOWN_MS",
    "MAX_RETRY_COUNT", "RETRY_INTERVAL_MS",
    # 枚举
    "NBTPlacementMode", "PlacementStatus",
    # 数据结构
    "PlatformConfig", "PlacementResult",
    # 放置器
    "NBTPlacer",
]
