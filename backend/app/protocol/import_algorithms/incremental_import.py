"""incremental_import - 增量导入 (tickingarea + 断点续传)。

逆向自 NexusEgo v1.6.5 的增量导入系统, 来源:

    - WaterStructure/structure/incremental_import.go
    - import_algo.txt
    - strings: "tickingarea" / "resume" / "checkpoint"

增量导入系统用于处理大型建筑的长时间导入:

    1. tickingarea (常加载区域):
       - 在导入区域创建 tickingarea, 确保命令方块始终执行
       - 即使玩家离开区域, 命令方块也能继续工作
       - 导入完成后移除 tickingarea

    2. 断点续传:
       - 定期保存导入进度 (checkpoint)
       - 中断后可以从上次的位置继续
       - 支持手动暂停和恢复

    3. 进度追踪:
       - 记录已导入的方块数
       - 估算剩余时间
       - 提供进度回调

字符串证据 (逆向自 strings_import.txt):
    "tickingarea"              -- 常加载区域
    "tickingarea add %d %d %d %d %s" -- 添加 tickingarea 命令
    "tickingarea remove %d"    -- 移除 tickingarea 命令
    "resume"                   -- 恢复
    "checkpoint"               -- 检查点
    "SaveCheckpoint: %v"       -- 保存检查点
    "LoadCheckpoint: %v"       -- 加载检查点
    "import progress: %d/%d"   -- 导入进度
    "estimated time: %v"       -- 估算时间
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("pocketterm.protocol.import_algorithms.incremental_import")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 默认检查点保存间隔 (秒)
DEFAULT_CHECKPOINT_INTERVAL: int = 30

#: 默认 tickingarea 名称前缀
TICKINGAREA_NAME_PREFIX: str = "nexus_import_"

#: 默认 tickingarea 半径 (区块)
DEFAULT_TICKINGAREA_RADIUS: int = 4


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class IncrementalImportError(Exception):
    """增量导入错误。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class IncrementalImportConfig:
    """增量导入配置。"""
    use_tickingarea: bool = True
    tickingarea_radius: int = DEFAULT_TICKINGAREA_RADIUS
    checkpoint_interval: int = DEFAULT_CHECKPOINT_INTERVAL
    checkpoint_dir: str = "/data/user/work/nexus_checkpoints"
    auto_resume: bool = True  # 自动恢复
    max_retries: int = 3      # 最大重试次数
    progress_callback: Callable[["ImportProgress"], None] | None = None


@dataclass
class ImportCheckpoint:
    """导入检查点。"""
    task_id: str = ""
    timestamp: float = 0.0
    completed_blocks: int = 0
    total_blocks: int = 0
    current_index: int = 0
    current_position: tuple[int, int, int] = (0, 0, 0)
    tickingarea_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def progress_percent(self) -> float:
        """进度百分比。"""
        if self.total_blocks == 0:
            return 0.0
        return (self.completed_blocks / self.total_blocks) * 100.0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "completed_blocks": self.completed_blocks,
            "total_blocks": self.total_blocks,
            "current_index": self.current_index,
            "current_position": list(self.current_position),
            "tickingarea_name": self.tickingarea_name,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ImportCheckpoint":
        """从字典创建。"""
        pos = data.get("current_position", [0, 0, 0])
        return cls(
            task_id=data.get("task_id", ""),
            timestamp=data.get("timestamp", 0.0),
            completed_blocks=data.get("completed_blocks", 0),
            total_blocks=data.get("total_blocks", 0),
            current_index=data.get("current_index", 0),
            current_position=tuple(pos) if isinstance(pos, list) else (0, 0, 0),
            tickingarea_name=data.get("tickingarea_name", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ImportProgress:
    """导入进度。"""
    task_id: str = ""
    completed_blocks: int = 0
    total_blocks: int = 0
    elapsed_seconds: float = 0.0
    estimated_remaining_seconds: float = 0.0
    blocks_per_second: float = 0.0
    current_position: tuple[int, int, int] = (0, 0, 0)
    is_paused: bool = False
    is_completed: bool = False

    @property
    def progress_percent(self) -> float:
        """进度百分比。"""
        if self.total_blocks == 0:
            return 0.0
        return (self.completed_blocks / self.total_blocks) * 100.0


# -------------------------------------------------------------------- #
# tickingarea 命令
# -------------------------------------------------------------------- #


def create_tickingarea(center: tuple[int, int, int],
                         radius: int = DEFAULT_TICKINGAREA_RADIUS,
                         name: str | None = None) -> str:
    """生成创建 tickingarea 的命令。

    逆向自 strings: "tickingarea add %d %d %d %d %s"。

    Args:
        center: 中心坐标 (x, z, 实际只用 x 和 z)。
        radius: 半径 (区块)。
        name: tickingarea 名称。

    Returns:
        tickingarea add 命令字符串。
    """
    if name is None:
        name = f"{TICKINGAREA_NAME_PREFIX}{center[0]}_{center[2]}"
    x, _, z = center
    return f"tickingarea add {x} {z} {radius} {name}"


def remove_tickingarea(name: str) -> str:
    """生成移除 tickingarea 的命令。

    逆向自 strings: "tickingarea remove %d"。

    Args:
        name: tickingarea 名称。

    Returns:
        tickingarea remove 命令字符串。
    """
    return f"tickingarea remove {name}"


# -------------------------------------------------------------------- #
# 检查点管理
# -------------------------------------------------------------------- #


def save_checkpoint(checkpoint: ImportCheckpoint,
                      config: IncrementalImportConfig | None = None) -> str:
    """保存检查点。

    逆向自 strings: "SaveCheckpoint: %v"。

    Args:
        checkpoint: 检查点数据。
        config: 导入配置。

    Returns:
        检查点文件路径。
    """
    config = config or IncrementalImportConfig()
    os.makedirs(config.checkpoint_dir, exist_ok=True)
    file_path = os.path.join(config.checkpoint_dir, f"{checkpoint.task_id}.json")
    checkpoint.timestamp = time.time()
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(checkpoint.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(
            "Checkpoint saved: task=%s, progress=%d/%d (%.1f%%)",
            checkpoint.task_id, checkpoint.completed_blocks,
            checkpoint.total_blocks, checkpoint.progress_percent,
        )
    except OSError as exc:
        raise IncrementalImportError(f"failed to save checkpoint: {exc}") from exc
    return file_path


def load_checkpoint(task_id: str,
                      config: IncrementalImportConfig | None = None) -> ImportCheckpoint | None:
    """加载检查点。

    逆向自 strings: "LoadCheckpoint: %v"。

    Args:
        task_id: 任务 ID。
        config: 导入配置。

    Returns:
        检查点数据, 如果不存在则返回 None。
    """
    config = config or IncrementalImportConfig()
    file_path = os.path.join(config.checkpoint_dir, f"{task_id}.json")
    if not os.path.exists(file_path):
        return None
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        checkpoint = ImportCheckpoint.from_dict(data)
        logger.info(
            "Checkpoint loaded: task=%s, progress=%d/%d (%.1f%%)",
            checkpoint.task_id, checkpoint.completed_blocks,
            checkpoint.total_blocks, checkpoint.progress_percent,
        )
        return checkpoint
    except (OSError, json.JSONDecodeError) as exc:
        raise IncrementalImportError(f"failed to load checkpoint: {exc}") from exc


# -------------------------------------------------------------------- #
# 增量导入器
# -------------------------------------------------------------------- #


class IncrementalImporter:
    """增量导入器。

    逆向自 WaterStructure/structure/incremental_import.go。
    支持断点续传的大型建筑导入。
    """

    def __init__(self, config: IncrementalImportConfig | None = None) -> None:
        self.config = config or IncrementalImportConfig()
        self._start_time: float = 0.0
        self._checkpoint: ImportCheckpoint | None = None
        self._paused: bool = False

    def import_blocks(self, blocks: list[dict[str, Any]],
                        task_id: str,
                        execute_command: Callable[[str], None] | None = None
                        ) -> ImportProgress:
        """执行增量导入。

        Args:
            blocks: 方块列表。
            task_id: 任务 ID (用于检查点)。
            execute_command: 命令执行回调。

        Returns:
            :class:`ImportProgress` 最终进度。
        """
        self._start_time = time.time()

        # 尝试加载已有检查点
        if self.config.auto_resume:
            self._checkpoint = load_checkpoint(task_id, self.config)

        if self._checkpoint is None:
            self._checkpoint = ImportCheckpoint(
                task_id=task_id,
                total_blocks=len(blocks),
            )

        start_index = self._checkpoint.current_index

        # 创建 tickingarea
        if self.config.use_tickingarea and not self._checkpoint.tickingarea_name:
            if blocks:
                center = blocks[0]["position"]
                ta_name = f"{TICKINGAREA_NAME_PREFIX}{task_id}"
                self._checkpoint.tickingarea_name = ta_name
                if execute_command:
                    execute_command(
                        create_tickingarea(center, self.config.tickingarea_radius, ta_name)
                    )

        # 导入方块
        last_checkpoint_time = time.time()
        for i in range(start_index, len(blocks)):
            if self._paused:
                # 保存检查点并退出
                self._checkpoint.current_index = i
                save_checkpoint(self._checkpoint, self.config)
                return self._make_progress(i)

            block = blocks[i]
            # 执行方块放置命令
            if execute_command:
                cmd = self._make_place_command(block)
                execute_command(cmd)

            self._checkpoint.completed_blocks = i + 1
            self._checkpoint.current_index = i + 1
            self._checkpoint.current_position = block["position"]

            # 定期保存检查点
            now = time.time()
            if now - last_checkpoint_time >= self.config.checkpoint_interval:
                save_checkpoint(self._checkpoint, self.config)
                last_checkpoint_time = now

            # 进度回调
            if self.config.progress_callback:
                progress = self._make_progress(i + 1)
                self.config.progress_callback(progress)

        # 完成
        self._checkpoint.metadata["completed"] = True
        save_checkpoint(self._checkpoint, self.config)

        # 移除 tickingarea
        if self.config.use_tickingarea and self._checkpoint.tickingarea_name:
            if execute_command:
                execute_command(remove_tickingarea(self._checkpoint.tickingarea_name))

        progress = self._make_progress(len(blocks))
        progress.is_completed = True
        logger.info(
            "Import completed: task=%s, blocks=%d, time=%.1fs",
            task_id, len(blocks), time.time() - self._start_time,
        )
        return progress

    def pause(self) -> None:
        """暂停导入。"""
        self._paused = True
        logger.info("Import paused")

    def resume(self) -> None:
        """恢复导入。"""
        self._paused = False
        logger.info("Import resumed")

    def _make_progress(self, completed: int) -> ImportProgress:
        """构建进度对象。"""
        elapsed = time.time() - self._start_time
        bps = completed / elapsed if elapsed > 0 else 0.0
        remaining = (
            (self._checkpoint.total_blocks - completed) / bps
            if bps > 0 else 0.0
        )
        return ImportProgress(
            task_id=self._checkpoint.task_id,
            completed_blocks=completed,
            total_blocks=self._checkpoint.total_blocks,
            elapsed_seconds=elapsed,
            estimated_remaining_seconds=remaining,
            blocks_per_second=bps,
            current_position=self._checkpoint.current_position,
            is_paused=self._paused,
            is_completed=completed >= self._checkpoint.total_blocks,
        )

    def _make_place_command(self, block: dict[str, Any]) -> str:
        """生成方块放置命令。"""
        x, y, z = block["position"]
        name = block.get("block_name", "minecraft:stone")
        if not name.startswith("minecraft:"):
            name = f"minecraft:{name}"
        states = block.get("block_states", "")
        cmd = f"setblock {x} {y} {z} {name}"
        if states:
            cmd += f" {states}"
        return cmd


# -------------------------------------------------------------------- #
# 顶层函数
# -------------------------------------------------------------------- #


def resume_import(task_id: str,
                    blocks: list[dict[str, Any]],
                    config: IncrementalImportConfig | None = None,
                    execute_command: Callable[[str], None] | None = None
                    ) -> ImportProgress:
    """恢复导入任务。

    逆向自 strings: "resume"。

    Args:
        task_id: 任务 ID。
        blocks: 方块列表。
        config: 导入配置。
        execute_command: 命令执行回调。

    Returns:
        :class:`ImportProgress`。
    """
    importer = IncrementalImporter(config)
    return importer.import_blocks(blocks, task_id, execute_command)


__all__ = [
    "DEFAULT_CHECKPOINT_INTERVAL", "TICKINGAREA_NAME_PREFIX",
    "DEFAULT_TICKINGAREA_RADIUS",
    "IncrementalImportError",
    "IncrementalImportConfig", "ImportCheckpoint", "ImportProgress",
    "IncrementalImporter",
    "create_tickingarea", "remove_tickingarea",
    "save_checkpoint", "load_checkpoint",
    "resume_import",
]
