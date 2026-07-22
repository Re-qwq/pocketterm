"""PocketTerm 导入任务管理 API

路由前缀: ``/api/imports``

提供建筑 / 结构文件导入任务的管理接口:

    - ``GET    ""``                       获取导入任务列表
    - ``POST   ""``                       创建导入任务
    - ``POST   "/{task_id}/pause"``       暂停任务
    - ``POST   "/{task_id}/restart"``     恢复 / 重启任务
    - ``POST   "/{task_id}/cancel"``      取消任务
    - ``DELETE "/{task_id}"``             删除任务

实现说明:
    当前版本使用 **内存存储 + Mock 数据** 的简单实现，
    后续接入真实导入系统时只需替换 :func:`_store` 相关操作即可。

任务状态机::

    pending  --> running  --> completed
        |          |  |
        |          |  +--> failed
        |          +--> paused --> running (restart)
        |          |
        +----------+--> cancelled

响应格式说明:
    为了同时兼容前端（直接读取顶层字段）和统一响应规范，
    本模块的响应在保留 ``success`` / ``message`` 字段的同时，
    将业务字段平铺到顶层。
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..logger import get_logger
from .deps import get_current_user

logger = get_logger("api.imports")

router = APIRouter(prefix="/api/imports", tags=["导入任务"])


# ---------------------------------------------------------------------------
# 任务状态常量
# ---------------------------------------------------------------------------
class TaskStatus:
    """导入任务状态枚举（字符串常量）。"""

    PENDING: str = "pending"
    RUNNING: str = "running"
    PAUSED: str = "paused"
    COMPLETED: str = "completed"
    FAILED: str = "failed"
    CANCELLED: str = "cancelled"


#: 支持的文件格式（用于从文件名推断 format 字段）
SUPPORTED_FORMATS: List[str] = [
    "bdx",
    "schem",
    "schematic",
    "mcstructure",
    "mcworld",
    "nbt",
    "mcfunction",
    "kbdx",
    "fuhong",
    "gangban",
    "axiombp",
    "cdump",
    "building",
]


# ---------------------------------------------------------------------------
# 内存存储（线程安全）
# ---------------------------------------------------------------------------
class _TaskStore:
    """线程安全的导入任务内存存储。"""

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock: threading.Lock = threading.Lock()

    def list_tasks(self) -> List[Dict[str, Any]]:
        """返回所有任务（按创建时间降序）。"""
        with self._lock:
            tasks = list(self._tasks.values())
        tasks.sort(key=lambda t: t.get("created_at", 0), reverse=True)
        return tasks

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """按 ID 获取任务。"""
        with self._lock:
            return self._tasks.get(task_id)

    def add_task(self, task: Dict[str, Any]) -> Dict[str, Any]:
        """添加任务。"""
        with self._lock:
            self._tasks[task["id"]] = task
        return task

    def update_task(
        self, task_id: str, updates: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """更新任务字段，自动刷新 ``updated_at``。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.update(updates)
            task["updated_at"] = time.time()
            return task

    def delete_task(self, task_id: str) -> bool:
        """删除任务。"""
        with self._lock:
            return self._tasks.pop(task_id, None) is not None

    def clear(self) -> int:
        """清空所有任务。"""
        with self._lock:
            count = len(self._tasks)
            self._tasks.clear()
            return count


#: 全局任务存储实例
_store: _TaskStore = _TaskStore()


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------
class ImportPosition(BaseModel):
    """导入起始坐标。"""

    x: int = Field(0, description="X 坐标")
    y: int = Field(64, description="Y 坐标（高度）")
    z: int = Field(0, description="Z 坐标")


class ImportOptions(BaseModel):
    """导入选项（前端实际发送的结构）。"""

    chunk_size: int = Field(32, description="N×N 区块大小")
    block_speed: int = Field(20, description="方块放置速度（块/秒）")
    command_speed: int = Field(10, description="命令方块速度（条/秒）")
    nbt_delay: float = Field(0.5, description="NBT 操作延迟（秒）")
    import_nbt: bool = Field(True, description="是否导入 NBT 数据")
    import_command_block: bool = Field(True, description="是否导入命令方块")
    experimental: bool = Field(False, description="是否启用实验性功能")
    # 兼容任务规范中的字段名
    command_block_rate: Optional[int] = Field(None, description="命令方块速率")
    block_rate: Optional[int] = Field(None, description="方块速率")
    patch_mode: Optional[bool] = Field(None, description="补丁模式")


class CreateImportTaskRequest(BaseModel):
    """创建导入任务请求体。

    同时兼容两种字段命名:
        - 前端实际发送: ``file_id`` / ``bot_id`` / ``algorithm`` / ``position`` / ``options``
        - 任务规范: ``file_name`` / ``bot_id`` / ``algorithm`` / ``chunk_size`` /
          ``import_nbt`` / ``import_command_blocks`` / ``command_block_rate`` /
          ``block_rate`` / ``patch_mode`` / ``start_chunk``
    """

    # 文件相关
    file_id: str = Field("", description="文件 ID（前端使用）")
    file_name: str = Field("", description="文件名（规范字段）")
    # 机器人相关
    bot_id: str = Field(..., description="机器人 ID")
    bot_name: str = Field("", description="机器人名称（可选）")
    # 算法
    algorithm: str = Field("chunk_fill", description="导入算法")
    # 起始坐标（前端使用 position 对象）
    position: Optional[ImportPosition] = Field(None, description="起始坐标")
    start_chunk: Optional[List[int]] = Field(None, description="起始区块坐标 [x, y, z]")
    # 导入选项（前端使用 options 对象）
    options: Optional[ImportOptions] = Field(None, description="导入选项")
    # 任务规范中的扁平字段（与 options 互为兼容）
    chunk_size: Optional[int] = Field(None, description="N×N 区块大小")
    import_nbt: Optional[bool] = Field(None, description="是否导入 NBT")
    import_command_blocks: Optional[bool] = Field(None, description="是否导入命令方块")
    command_block_rate: Optional[int] = Field(None, description="命令方块速率")
    block_rate: Optional[int] = Field(None, description="方块速率")
    patch_mode: Optional[bool] = Field(None, description="补丁模式")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _detect_format(file_name: str) -> str:
    """从文件名推断文件格式（扩展名，小写）。

    Args:
        file_name: 文件名。

    Returns:
        小写的扩展名（不含 ``.``），无法识别时返回 ``"unknown"``。
    """
    if not file_name or "." not in file_name:
        return "unknown"
    ext: str = file_name.rsplit(".", 1)[-1].lower()
    return ext if ext in SUPPORTED_FORMATS else ext or "unknown"


def _normalize_task(task: Dict[str, Any]) -> Dict[str, Any]:
    """规整任务字典，确保前端期望的字段都存在。"""
    # 兼容前端读取 filename / name
    name = task.get("file_name") or task.get("filename") or task.get("name") or ""
    task["file_name"] = name
    task["filename"] = name  # 前端 imports.js 使用 filename
    task["name"] = name
    # 兼容前端读取 bot_name / bot
    bot_name = task.get("bot_name") or task.get("bot") or ""
    task["bot_name"] = bot_name
    task["bot"] = bot_name
    # 兼容前端读取 progress
    task.setdefault("progress", 0)
    task.setdefault("status", TaskStatus.PENDING)
    return task


def _build_task_from_request(body: CreateImportTaskRequest) -> Dict[str, Any]:
    """根据请求体构造任务字典。"""
    # 解析文件名（兼容 file_name 与 file_id）
    file_name: str = body.file_name or body.file_id or "unknown.bdx"
    # 解析选项
    options: Dict[str, Any] = {}
    if body.options is not None:
        options = body.options.model_dump(exclude_none=True)
    # 合并任务规范中的扁平字段
    if body.chunk_size is not None:
        options.setdefault("chunk_size", body.chunk_size)
    if body.import_nbt is not None:
        options.setdefault("import_nbt", body.import_nbt)
    if body.import_command_blocks is not None:
        options.setdefault(
            "import_command_block",
            body.import_command_blocks,
        )
        options.setdefault("import_command_blocks", body.import_command_blocks)
    if body.command_block_rate is not None:
        options.setdefault("command_block_rate", body.command_block_rate)
    if body.block_rate is not None:
        options.setdefault("block_rate", body.block_rate)
    if body.patch_mode is not None:
        options.setdefault("patch_mode", body.patch_mode)

    # 解析起始坐标
    position: Dict[str, int]
    if body.position is not None:
        position = {
            "x": body.position.x,
            "y": body.position.y,
            "z": body.position.z,
        }
    elif body.start_chunk is not None and len(body.start_chunk) >= 3:
        position = {
            "x": int(body.start_chunk[0]),
            "y": int(body.start_chunk[1]),
            "z": int(body.start_chunk[2]),
        }
    else:
        position = {"x": 0, "y": 64, "z": 0}

    now: float = time.time()
    task_id: str = uuid.uuid4().hex[:12]
    task: Dict[str, Any] = {
        "id": task_id,
        "file_id": body.file_id,
        "file_name": file_name,
        "format": _detect_format(file_name),
        "bot_id": body.bot_id,
        "bot_name": body.bot_name,
        "algorithm": body.algorithm,
        "progress": 0,
        "status": TaskStatus.PENDING,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "position": position,
        "options": options,
        "placed_blocks": 0,
        "total_blocks": 0,
        "error": None,
    }
    return task


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("")
async def list_import_tasks(
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取导入任务列表。

    返回顶层字段:
        - ``tasks``: 任务数组（规范字段名）
        - ``items``: 同 ``tasks``，兼容前端字段名
        - ``total``: 任务总数
    """
    try:
        tasks = [_normalize_task(t) for t in _store.list_tasks()]
        return {
            "success": True,
            "message": f"共 {len(tasks)} 个导入任务",
            "tasks": tasks,
            "items": tasks,  # 兼容前端
            "total": len(tasks),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("获取导入任务列表失败")
        return {
            "success": False,
            "message": f"获取导入任务列表失败: {exc}",
            "error": "list_failed",
            "tasks": [],
            "items": [],
            "total": 0,
        }


@router.post("")
async def create_import_task(
    body: CreateImportTaskRequest,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """创建导入任务。

    请求体同时兼容前端实际发送的格式和任务规范定义的格式，
    详见 :class:`CreateImportTaskRequest`。

    返回:
        ``{"success": True, "task": {...}, ...}``
    """
    try:
        if not body.bot_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="缺少 bot_id",
            )

        task = _build_task_from_request(body)
        _store.add_task(task)
        normalized = _normalize_task(dict(task))

        logger.info(
            f"创建导入任务: id={task['id']}, file={task['file_name']}, "
            f"bot={task['bot_id']}, algorithm={task['algorithm']}"
        )

        return {
            "success": True,
            "message": "导入任务已创建",
            "task": normalized,
            "id": task["id"],
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建导入任务失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建导入任务失败: {exc}",
        )


@router.post("/{task_id}/pause")
async def pause_import_task(
    task_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """暂停导入任务。

    仅 ``running`` / ``pending`` 状态的任务可暂停。
    """
    try:
        task = _store.get_task(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在: {task_id}",
            )

        current_status = task.get("status")
        if current_status not in (TaskStatus.RUNNING, TaskStatus.PENDING):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"当前状态 {current_status} 不支持暂停",
            )

        updated = _store.update_task(
            task_id,
            {"status": TaskStatus.PAUSED},
        )
        return {
            "success": True,
            "message": f"任务已暂停: {task_id}",
            "task": _normalize_task(dict(updated or {})),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"暂停任务失败: {task_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"暂停任务失败: {exc}",
        )


@router.post("/{task_id}/restart")
async def restart_import_task(
    task_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """恢复 / 重启导入任务。

    可恢复的状态: ``paused`` / ``failed`` / ``cancelled`` / ``completed``。
    恢复后状态变为 ``running``，进度保留（若为已完成则重置为 0）。
    """
    try:
        task = _store.get_task(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在: {task_id}",
            )

        current_status = task.get("status")
        if current_status not in (
            TaskStatus.PAUSED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.COMPLETED,
            TaskStatus.PENDING,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"当前状态 {current_status} 不支持重启",
            )

        updates: Dict[str, Any] = {
            "status": TaskStatus.RUNNING,
            "started_at": time.time(),
            "error": None,
        }
        # 已完成的任务重启时重置进度
        if current_status == TaskStatus.COMPLETED:
            updates["progress"] = 0
            updates["placed_blocks"] = 0
            updates["finished_at"] = None

        updated = _store.update_task(task_id, updates)
        return {
            "success": True,
            "message": f"任务已恢复: {task_id}",
            "task": _normalize_task(dict(updated or {})),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"重启任务失败: {task_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"重启任务失败: {exc}",
        )


@router.post("/{task_id}/cancel")
async def cancel_import_task(
    task_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """取消导入任务。

    仅 ``running`` / ``pending`` / ``paused`` 状态的任务可取消。
    """
    try:
        task = _store.get_task(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在: {task_id}",
            )

        current_status = task.get("status")
        if current_status not in (
            TaskStatus.RUNNING,
            TaskStatus.PENDING,
            TaskStatus.PAUSED,
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"当前状态 {current_status} 不支持取消",
            )

        updated = _store.update_task(
            task_id,
            {
                "status": TaskStatus.CANCELLED,
                "finished_at": time.time(),
            },
        )
        return {
            "success": True,
            "message": f"任务已取消: {task_id}",
            "task": _normalize_task(dict(updated or {})),
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"取消任务失败: {task_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"取消任务失败: {exc}",
        )


@router.delete("/{task_id}")
async def delete_import_task(
    task_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """删除导入任务记录。"""
    try:
        task = _store.get_task(task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"任务不存在: {task_id}",
            )
        _store.delete_task(task_id)
        return {
            "success": True,
            "message": f"任务已删除: {task_id}",
            "task_id": task_id,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"删除任务失败: {task_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除任务失败: {exc}",
        )


__all__ = ["router", "TaskStatus"]
