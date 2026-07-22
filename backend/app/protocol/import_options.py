"""导入选项配置系统。

逆向来源: NexusE / NovaBuilder 导入配置系统
- NexusE v1.6.5: game_control/game_interface/import_config.go
- NovaBuilder: galaxy/import/options.go

功能:
    - 速度控制: DelayMode, DelayThreshold, rate.Limiter, Burst
    - 移动物品速度: MoveItemSpeed, TransferCooldown, TargetCooldownLength
    - 客户端节流: ClientThrottle, ClientThrottleScalar, ClientThrottleThreshold
    - 导入模式: HighImportSetting, buildImportTaskConfig, DontFillCache
    - 重连: shouldAutoReconnectImport, importReconnectWatchdogDrain
    - 运行时命令: [setdelay], skip-mcpc-check-challenges
    - 所有选项支持从JSON配置文件加载和保存
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("pocketterm.protocol.import_options")

# ----------------------------------------------------------------------
# 枚举
# ----------------------------------------------------------------------


class DelayMode(Enum):
    """延迟模式。

    逆向自 NexusE game_control/game_interface/import_config.go
    """

    NONE = "none"
    """无延迟 (最快速度)"""

    DISCRETE = "discrete"
    """离散延迟 (每N个方块后插入延迟)"""


class ImportAlgorithm(Enum):
    """导入算法选择。

    逆向自 NovaBuilder galaxy/import/options.go
    """

    CUBE_EXPAND = "cube_expand"
    """立方体扩展 (X→Z→Y 三轴扩展, 最大化 fill 体积)"""

    INNER_TO_OUTER = "inner_to_outer"
    """从内向外 (曼哈顿距离排序, 从建筑中心向外扩展)"""

    SNAKE = "snake"
    """蛇形扫描 (区块内蛇形路径)"""

    AUTO = "auto"
    """自动选择 (根据体积和方块类型智能选择)"""


class HighImportMode(Enum):
    """高速导入模式。

    逆向自 NexusE game_control/game_interface/import_config.go
    """

    NORMAL = "normal"
    """普通模式"""

    HIGH_SPEED = "high_speed"
    """高速模式 (更激进的速度控制)"""

    ULTRA = "ultra"
    """极速模式 (最大速度, 可能触发反作弊)"""


# ----------------------------------------------------------------------
# 配置数据类
# ----------------------------------------------------------------------


@dataclass
class SpeedControl:
    """速度控制配置。

    逆向自 NexusE game_control/game_interface/import_config.go

    控制方块放置命令的发送速率。

    Attributes:
        delay_mode: 延迟模式 (none/discrete)
        delay_threshold: 离散延迟阈值 (每N个方块后插入延迟)
        block_speed: 每秒放置方块数 (默认 20)
        command_speed: 每秒发送命令数 (默认 10, 用于命令方块)
        container_speed: 每秒放置容器物品数 (默认 5)
        burst: 是否启用突发模式
        burst_density: 突发密度 (每次突发发送的方块数)
        burst_duration: 突发持续时间 (秒)
        group_wait: 组间等待时间 (秒)
        nbt_delay: NBT操作间延迟 (秒)
    """

    delay_mode: DelayMode = DelayMode.DISCRETE
    """延迟模式"""

    delay_threshold: int = 100
    """离散延迟阈值 (每N个方块后插入延迟)"""

    block_speed: int = 20
    """每秒放置方块数"""

    command_speed: int = 10
    """每秒发送命令数 (命令方块)"""

    container_speed: int = 5
    """每秒放置容器物品数"""

    burst: bool = False
    """是否启用突发模式"""

    burst_density: int = 500
    """突发密度 (每次突发发送的方块数)"""

    burst_duration: float = 1.0
    """突发持续时间 (秒)"""

    group_wait: float = 1.0
    """组间等待时间 (秒)"""

    nbt_delay: float = 0.5
    """NBT操作间延迟 (秒)"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "delay_mode": self.delay_mode.value,
            "delay_threshold": self.delay_threshold,
            "block_speed": self.block_speed,
            "command_speed": self.command_speed,
            "container_speed": self.container_speed,
            "burst": self.burst,
            "burst_density": self.burst_density,
            "burst_duration": self.burst_duration,
            "group_wait": self.group_wait,
            "nbt_delay": self.nbt_delay,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SpeedControl":
        return cls(
            delay_mode=DelayMode(data.get("delay_mode", "discrete")),
            delay_threshold=data.get("delay_threshold", 100),
            block_speed=data.get("block_speed", 20),
            command_speed=data.get("command_speed", 10),
            container_speed=data.get("container_speed", 5),
            burst=data.get("burst", False),
            burst_density=data.get("burst_density", 500),
            burst_duration=data.get("burst_duration", 1.0),
            group_wait=data.get("group_wait", 1.0),
            nbt_delay=data.get("nbt_delay", 0.5),
        )


@dataclass
class ThrottleControl:
    """客户端节流控制。

    逆向自 NexusE game_control/game_interface/import_config.go

    节流控制用于防止客户端因发送过多命令而卡顿。

    Attributes:
        client_throttle: 是否启用客户端节流
        client_throttle_scalar: 节流比例因子 (0.0-1.0)
        client_throttle_threshold: 节流阈值 (每秒命令数, 超过此值开始节流)
    """

    client_throttle: bool = False
    """是否启用客户端节流"""

    client_throttle_scalar: float = 0.5
    """节流比例因子 (0.0-1.0)"""

    client_throttle_threshold: int = 50
    """节流阈值 (每秒命令数)"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "client_throttle": self.client_throttle,
            "client_throttle_scalar": self.client_throttle_scalar,
            "client_throttle_threshold": self.client_throttle_threshold,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThrottleControl":
        return cls(
            client_throttle=data.get("client_throttle", False),
            client_throttle_scalar=data.get("client_throttle_scalar", 0.5),
            client_throttle_threshold=data.get("client_throttle_threshold", 50),
        )


@dataclass
class ReconnectOptions:
    """重连选项。

    逆向自 NexusE game_control/game_interface/import_config.go

    处理导入过程中断线重连的逻辑。

    Attributes:
        auto_reconnect: 是否自动重连
        reconnect_watchdog_drain: 重连看门狗排空时间 (秒)
        max_reconnect_attempts: 最大重连次数
        reconnect_delay: 重连间隔 (秒)
    """

    auto_reconnect: bool = True
    """是否自动重连"""

    reconnect_watchdog_drain: float = 5.0
    """重连看门狗排空时间 (秒)"""

    max_reconnect_attempts: int = 3
    """最大重连次数"""

    reconnect_delay: float = 2.0
    """重连间隔 (秒)"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "auto_reconnect": self.auto_reconnect,
            "reconnect_watchdog_drain": self.reconnect_watchdog_drain,
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "reconnect_delay": self.reconnect_delay,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReconnectOptions":
        return cls(
            auto_reconnect=data.get("auto_reconnect", True),
            reconnect_watchdog_drain=data.get("reconnect_watchdog_drain", 5.0),
            max_reconnect_attempts=data.get("max_reconnect_attempts", 3),
            reconnect_delay=data.get("reconnect_delay", 2.0),
        )


@dataclass
class PatchOptions:
    """修补选项。

    逆向自 NexusE game_control/game_interface/import_config.go

    修补模式仅导入与现有世界差异的部分。

    Attributes:
        patch_mode: 是否启用修补模式
        no_import_bar: 是否跳过屏障方块 (No_Import_bar)
        unbuilder: 是否启用Unbuilder模式 (magma/water清除)
        close_sign: 是否关闭告示牌
        skip_air: 是否跳过空气方块
    """

    patch_mode: bool = False
    """是否启用修补模式"""

    no_import_bar: bool = False
    """是否跳过屏障方块 (No_Import_bar)"""

    unbuilder: bool = False
    """是否启用Unbuilder模式 (magma/water清除)"""

    close_sign: bool = False
    """是否关闭告示牌"""

    skip_air: bool = True
    """是否跳过空气方块"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_mode": self.patch_mode,
            "no_import_bar": self.no_import_bar,
            "unbuilder": self.unbuilder,
            "close_sign": self.close_sign,
            "skip_air": self.skip_air,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PatchOptions":
        return cls(
            patch_mode=data.get("patch_mode", False),
            no_import_bar=data.get("no_import_bar", False),
            unbuilder=data.get("unbuilder", False),
            close_sign=data.get("close_sign", False),
            skip_air=data.get("skip_air", True),
        )


@dataclass
class MoveItemOptions:
    """移动物品速度选项。

    逆向自 NexusE game_control/game_interface/import_config.go

    控制容器物品 (replaceitem) 的放置速度。

    Attributes:
        move_item_speed: 物品移动速度 (物品/秒)
        transfer_cooldown: 传输冷却时间 (秒)
        target_cooldown_length: 目标冷却长度 (物品数)
    """

    move_item_speed: int = 5
    """物品移动速度 (物品/秒)"""

    transfer_cooldown: float = 0.2
    """传输冷却时间 (秒)"""

    target_cooldown_length: int = 10
    """目标冷却长度 (物品数)"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "move_item_speed": self.move_item_speed,
            "transfer_cooldown": self.transfer_cooldown,
            "target_cooldown_length": self.target_cooldown_length,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MoveItemOptions":
        return cls(
            move_item_speed=data.get("move_item_speed", 5),
            transfer_cooldown=data.get("transfer_cooldown", 0.2),
            target_cooldown_length=data.get("target_cooldown_length", 10),
        )


@dataclass
class CacheOptions:
    """缓存选项。

    逆向自 NexusE game_control/game_interface/import_config.go

    Attributes:
        dont_fill_cache: 是否跳过填充缓存
        cache_file: 缓存文件路径
    """

    dont_fill_cache: bool = False
    """是否跳过填充缓存"""

    cache_file: str = "import_cache.json"
    """缓存文件路径"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dont_fill_cache": self.dont_fill_cache,
            "cache_file": self.cache_file,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CacheOptions":
        return cls(
            dont_fill_cache=data.get("dont_fill_cache", False),
            cache_file=data.get("cache_file", "import_cache.json"),
        )


@dataclass
class RuntimeCommands:
    """运行时命令选项。

    逆向自 NexusE game_control/game_interface/import_config.go

    Attributes:
        setdelay: 动态设置延迟 (格式: "[setdelay] 数值")
        skip_mcpc_check_challenges: 是否跳过MCPC检查挑战
    """

    setdelay: Optional[int] = None
    """动态设置延迟 (None表示不设置)"""

    skip_mcpc_check_challenges: bool = False
    """是否跳过MCPC检查挑战"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "setdelay": self.setdelay,
            "skip_mcpc_check_challenges": self.skip_mcpc_check_challenges,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeCommands":
        return cls(
            setdelay=data.get("setdelay"),
            skip_mcpc_check_challenges=data.get("skip_mcpc_check_challenges", False),
        )


# ----------------------------------------------------------------------
# 总配置类
# ----------------------------------------------------------------------


@dataclass
class ImportOptions:
    """导入选项总配置类。

    逆向自 NexusE / NovaBuilder 导入配置系统

    聚合所有导入相关的配置选项, 支持从JSON配置文件加载和保存。

    使用示例::

        # 默认配置
        opts = ImportOptions()

        # 从JSON加载
        opts = ImportOptions.from_json("/path/to/config.json")

        # 保存到JSON
        opts.to_json("/path/to/config.json")

        # 运行时修改
        opts.speed.block_speed = 50
        opts.patch.patch_mode = True
        opts.algorithm = ImportAlgorithm.INNER_TO_OUTER
    """

    speed: SpeedControl = field(default_factory=SpeedControl)
    """速度控制"""

    throttle: ThrottleControl = field(default_factory=ThrottleControl)
    """客户端节流控制"""

    reconnect: ReconnectOptions = field(default_factory=ReconnectOptions)
    """重连选项"""

    patch: PatchOptions = field(default_factory=PatchOptions)
    """修补选项"""

    move_item: MoveItemOptions = field(default_factory=MoveItemOptions)
    """移动物品选项"""

    cache: CacheOptions = field(default_factory=CacheOptions)
    """缓存选项"""

    runtime: RuntimeCommands = field(default_factory=RuntimeCommands)
    """运行时命令"""

    high_import_mode: HighImportMode = HighImportMode.NORMAL
    """高速导入模式"""

    algorithm: ImportAlgorithm = ImportAlgorithm.AUTO
    """导入算法选择"""

    chunk_size: int = 1
    """多区块合并大小 (N×N区块合并, 1=单区块)"""

    include_nbt: bool = True
    """是否包含NBT数据"""

    include_command_blocks: bool = True
    """是否包含命令方块"""

    command_block_speed: int = 10
    """命令方块处理速度 (命令/秒)"""

    start_chunk: Optional[tuple[int, int]] = None
    """起始区块坐标 (用于断点续传, None=从头开始)"""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。

        Returns:
            配置字典。
        """
        return {
            "speed": self.speed.to_dict(),
            "throttle": self.throttle.to_dict(),
            "reconnect": self.reconnect.to_dict(),
            "patch": self.patch.to_dict(),
            "move_item": self.move_item.to_dict(),
            "cache": self.cache.to_dict(),
            "runtime": self.runtime.to_dict(),
            "high_import_mode": self.high_import_mode.value,
            "algorithm": self.algorithm.value,
            "chunk_size": self.chunk_size,
            "include_nbt": self.include_nbt,
            "include_command_blocks": self.include_command_blocks,
            "command_block_speed": self.command_block_speed,
            "start_chunk": list(self.start_chunk) if self.start_chunk else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportOptions":
        """从字典创建配置。

        Args:
            data: 配置字典。

        Returns:
            ImportOptions 实例。
        """
        start_chunk_raw = data.get("start_chunk")
        start_chunk = tuple(start_chunk_raw) if start_chunk_raw else None

        return cls(
            speed=SpeedControl.from_dict(data.get("speed", {})),
            throttle=ThrottleControl.from_dict(data.get("throttle", {})),
            reconnect=ReconnectOptions.from_dict(data.get("reconnect", {})),
            patch=PatchOptions.from_dict(data.get("patch", {})),
            move_item=MoveItemOptions.from_dict(data.get("move_item", {})),
            cache=CacheOptions.from_dict(data.get("cache", {})),
            runtime=RuntimeCommands.from_dict(data.get("runtime", {})),
            high_import_mode=HighImportMode(data.get("high_import_mode", "normal")),
            algorithm=ImportAlgorithm(data.get("algorithm", "auto")),
            chunk_size=data.get("chunk_size", 1),
            include_nbt=data.get("include_nbt", True),
            include_command_blocks=data.get("include_command_blocks", True),
            command_block_speed=data.get("command_block_speed", 10),
            start_chunk=start_chunk,
        )

    def to_json(self, path: str | Path) -> None:
        """保存配置到JSON文件。

        Args:
            path: 文件路径。
        """
        path = Path(path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            logger.info("导入配置已保存到: %s", path)
        except Exception as e:
            logger.error("保存导入配置失败: %s", e)
            raise

    @classmethod
    def from_json(cls, path: str | Path) -> "ImportOptions":
        """从JSON文件加载配置。

        Args:
            path: 文件路径。

        Returns:
            ImportOptions 实例。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: JSON解析失败。
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"配置文件不存在: {path}")

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info("导入配置已从 %s 加载", path)
            return cls.from_dict(data)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON解析失败: {path} -> {e}") from e

    @classmethod
    def create_default(cls) -> "ImportOptions":
        """创建默认配置。

        Returns:
            默认的 ImportOptions 实例。
        """
        return cls()

    @classmethod
    def create_fast(cls) -> "ImportOptions":
        """创建高速配置 (预设)。

        用于快速导入, 减少延迟。

        Returns:
            高速配置的 ImportOptions 实例。
        """
        return cls(
            speed=SpeedControl(
                delay_mode=DelayMode.NONE,
                block_speed=50,
                burst=True,
                burst_density=1000,
            ),
            high_import_mode=HighImportMode.HIGH_SPEED,
            throttle=ThrottleControl(client_throttle=False),
        )

    @classmethod
    def create_safe(cls) -> "ImportOptions":
        """创建安全配置 (预设)。

        用于稳定导入, 避免触发反作弊。

        Returns:
            安全配置的 ImportOptions 实例。
        """
        return cls(
            speed=SpeedControl(
                delay_mode=DelayMode.DISCRETE,
                delay_threshold=50,
                block_speed=10,
                group_wait=2.0,
            ),
            high_import_mode=HighImportMode.NORMAL,
            throttle=ThrottleControl(client_throttle=True, client_throttle_scalar=0.3),
        )


__all__ = [
    "DelayMode",
    "ImportAlgorithm",
    "HighImportMode",
    "SpeedControl",
    "ThrottleControl",
    "ReconnectOptions",
    "PatchOptions",
    "MoveItemOptions",
    "CacheOptions",
    "RuntimeCommands",
    "ImportOptions",
]