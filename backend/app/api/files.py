"""PocketTerm 文件管理 API

路由前缀: ``/api/files``

提供针对插件目录内文件的操作:

    - ``GET    "/{plugin_id}"``                    列出插件文件
    - ``POST   "/{plugin_id}/upload"``             上传文件
    - ``GET    "/{file_id}/preview"``              文件预览（解析元信息）
    - ``GET    "/{plugin_id}/{filename:path}"``    下载文件
    - ``DELETE "/{plugin_id}/{filename:path}"``    删除文件
    - ``POST   "/{plugin_id}/folder"``             创建目录

所有操作都被限制在 ``PocketTerm/plugins/{plugin_id}/`` 目录内，
通过 ``resolve()`` + ``is_relative_to()`` 双重校验防止路径穿越攻击。

文件预览路由 ``GET /{file_id}/preview`` 会调用
:mod:`app.protocol.phoenix_omega` 中的格式解析器解析文件，
只返回元信息（方块数、尺寸、是否含 NBT/命令方块等），
不返回原始解析数据，防止泄露技术内容。
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse

from ..config import PLUGINS_DIR
from ..logger import get_logger
from .deps import error_response, get_current_user, success_response

# 格式解析器（延迟导入以避免循环依赖 / 减少启动开销）
def _load_phoenix_omega():
    """延迟导入 phoenix_omega 模块（仅在文件预览时需要）。"""
    from ..protocol.phoenix_omega import (
        EXTENSION_PARSER_MAP,
        FormatNotSupportedError,
        FormatParseError,
        get_parser_for_file,
    )
    return {
        "EXTENSION_PARSER_MAP": EXTENSION_PARSER_MAP,
        "FormatNotSupportedError": FormatNotSupportedError,
        "FormatParseError": FormatParseError,
        "get_parser_for_file": get_parser_for_file,
    }

logger = get_logger("api.files")

router = APIRouter(prefix="/api/files", tags=["文件管理"])

#: 单个上传文件大小上限（50 MiB）
MAX_UPLOAD_BYTES: int = 50 * 1024 * 1024


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _plugin_root(plugin_id: str) -> Path:
    """返回插件根目录（``plugins/{plugin_id}``），自动创建。"""
    # 拒绝包含路径分隔符 / 模糊形式的 plugin_id
    if not plugin_id or "/" in plugin_id or "\\" in plugin_id or ".." in plugin_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的 plugin_id",
        )
    root = (PLUGINS_DIR / plugin_id).resolve()
    # 确保最终路径仍位于 PLUGINS_DIR 之下
    try:
        root.relative_to(PLUGINS_DIR.resolve())
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的 plugin_id",
        )
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_join(root: Path, relative: str) -> Path:
    """将 ``relative`` 拼接到 ``root`` 之下并校验未越界。

    Args:
        root: 插件根目录。
        relative: 相对路径（可含子目录）。

    Returns:
        解析后的绝对路径。

    Raises:
        HTTPException 400: 路径穿越检测失败。
    """
    if not relative:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件名不能为空",
        )
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件路径（路径穿越检测失败）",
        )
    return target


def _entry_info(path: Path, root: Path) -> Dict[str, Any]:
    """构造文件 / 目录信息字典。"""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    info: Dict[str, Any] = {
        "name": path.name,
        "path": rel,
        "type": "directory" if path.is_dir() else "file",
    }
    try:
        stat = path.stat()
        info["size"] = stat.st_size if path.is_file() else 0
        info["modified_at"] = stat.st_mtime
    except OSError:
        info["size"] = 0
        info["modified_at"] = 0
    return info


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@router.get("/{plugin_id}")
async def list_plugin_files(
    plugin_id: str,
    path: str = Query("", description="相对子目录路径（默认根目录）"),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """列出插件目录下的文件与子目录。"""
    root = _plugin_root(plugin_id)
    target_dir = _safe_join(root, path) if path else root

    if not target_dir.exists():
        return success_response(
            data={"entries": [], "path": path or "/", "total": 0},
            message="目录不存在",
        )
    if not target_dir.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="指定路径不是目录",
        )

    entries: List[Dict[str, Any]] = []
    for entry in sorted(target_dir.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
        entries.append(_entry_info(entry, root))

    return success_response(
        data={
            "entries": entries,
            "path": path or "/",
            "total": len(entries),
        },
        message=f"共 {len(entries)} 项",
    )


@router.post("/{plugin_id}/upload")
async def upload_plugin_file(
    plugin_id: str,
    file: UploadFile = File(..., description="要上传的文件"),
    path: str = Query("", description="目标子目录（不存在自动创建）"),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """上传文件到插件目录。"""
    root = _plugin_root(plugin_id)
    target_dir = _safe_join(root, path) if path else root
    target_dir.mkdir(parents=True, exist_ok=True)

    # 校验文件名安全性
    filename = os.path.basename(file.filename or "")
    if not filename or filename in (".", ".."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件名",
        )

    dest = _safe_join(target_dir, filename)

    # 流式写入，限制大小
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件过大，单文件上限 {MAX_UPLOAD_BYTES // (1024*1024)} MiB",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        logger.exception(f"上传文件失败 (plugin={plugin_id}, file={filename})")
        return error_response(error="upload_failed", message=f"写入失败: {exc}")
    finally:
        await file.close()

    logger.info(f"上传文件 {filename} -> {plugin_id} ({written} bytes)")
    return success_response(
        data={
            "name": filename,
            "path": dest.relative_to(root).as_posix(),
            "size": written,
            "uploaded_at": time.time(),
        },
        message=f"文件 {filename} 上传成功",
    )


# ---------------------------------------------------------------------------
# 文件预览相关工具
# ---------------------------------------------------------------------------
def _resolve_file_id(file_id: str) -> Path:
    """根据 ``file_id`` 解析出实际的文件路径。

    支持以下 ``file_id`` 形式:
        1. ``plugin_id/filename``  —— 在指定插件目录下查找
        2. ``filename``            —— 在所有插件目录中递归查找
        3. ``plugin_id``           —— 若插件目录下存在同名文件，则返回该文件

    所有路径都会经过 ``is_relative_to(PLUGINS_DIR)`` 校验，防止路径穿越。

    Args:
        file_id: 文件标识符。

    Returns:
        解析后的绝对文件路径。

    Raises:
        HTTPException 404: 文件未找到。
        HTTPException 400: file_id 非法。
    """
    if not file_id or "/" in file_id[:1] or "\\" in file_id[:1]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的 file_id",
        )

    plugins_root = PLUGINS_DIR.resolve()

    # 1) 形如 "plugin_id/filename"
    if "/" in file_id or "\\" in file_id:
        parts = file_id.replace("\\", "/").split("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="非法的 file_id",
            )
        plugin_id, filename = parts
        # 复用 _plugin_root / _safe_join 的安全校验
        root = _plugin_root(plugin_id)
        target = _safe_join(root, filename)
        if not target.is_file():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"文件不存在: {file_id}",
            )
        return target

    # 2) file_id 本身就是插件目录下同名的文件
    direct = (PLUGINS_DIR / file_id).resolve()
    try:
        direct.relative_to(plugins_root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的 file_id",
        )
    if direct.is_file():
        return direct

    # 3) 在所有插件目录中递归查找同名文件
    if PLUGINS_DIR.is_dir():
        for entry in PLUGINS_DIR.rglob(file_id):
            try:
                resolved = entry.resolve()
                resolved.relative_to(plugins_root)
                if resolved.is_file():
                    return resolved
            except (ValueError, OSError):
                continue

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"文件不存在: {file_id}",
    )


async def _parse_file_for_preview(file_path: Path) -> Dict[str, Any]:
    """解析建筑文件并返回预览元信息。

    使用 :mod:`app.protocol.phoenix_omega` 中的格式解析器解析文件，
    然后聚合以下信息:
        - ``block_count``: 非空气方块数
        - ``chunk_range``: 方块坐标的最小/最大值
        - ``has_nbt``: 是否包含 NBT 方块实体
        - ``has_command_blocks``: 是否包含命令方块
        - ``block_types``: 方块类型统计（按运行时 ID 聚合，前 N 个）
        - ``dimensions``: X/Y/Z 三维尺寸

    若解析失败，返回 ``parse_error`` 字段描述错误，其余字段使用默认值。

    Args:
        file_path: 文件路径。

    Returns:
        预览元信息字典。
    """
    result: Dict[str, Any] = {
        "block_count": 0,
        "chunk_range": {"min": None, "max": None},
        "has_nbt": False,
        "has_command_blocks": False,
        "block_types": [],
        "dimensions": {"x": 0, "y": 0, "z": 0},
        "parse_error": None,
    }

    try:
        omega = _load_phoenix_omega()
        get_parser_for_file = omega["get_parser_for_file"]
        FormatNotSupportedError = omega["FormatNotSupportedError"]
        FormatParseError = omega["FormatParseError"]
    except Exception as exc:  # noqa: BLE001
        result["parse_error"] = f"解析器加载失败: {exc}"
        return result

    # 读取文件内容
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        result["parse_error"] = f"读取文件失败: {exc}"
        return result

    # 获取解析器
    try:
        parser = get_parser_for_file(str(file_path))
    except FormatNotSupportedError as exc:
        result["parse_error"] = f"不支持的格式: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        result["parse_error"] = f"获取解析器失败: {exc}"
        return result

    # 解析文件
    try:
        parse_result = await parser.decode(data)
    except (FormatNotSupportedError, FormatParseError) as exc:
        result["parse_error"] = f"解析失败: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001
        result["parse_error"] = f"解析异常: {exc}"
        return result

    # 聚合方块信息
    block_count: int = 0
    has_nbt: bool = False
    has_command_blocks: bool = False
    min_x = min_y = min_z = None
    max_x = max_y = max_z = None
    block_type_counts: Dict[int, int] = {}

    # 命令方块相关运行时 ID 集合（来自 BDXParser._lookup_legacy_block）
    _COMMAND_BLOCK_RTIDS = {137, 188, 189}

    feeder = parse_result.block_feeder
    try:
        async for block in feeder:
            block_count += 1
            # 坐标范围
            try:
                bx, by, bz = block.pos.x, block.pos.y, block.pos.z
            except (AttributeError, TypeError):
                try:
                    bx, by, bz = block.pos[0], block.pos[1], block.pos[2]
                except (IndexError, TypeError):
                    bx = by = bz = 0

            if min_x is None or bx < min_x:
                min_x = bx
            if max_x is None or bx > max_x:
                max_x = bx
            if min_y is None or by < min_y:
                min_y = by
            if max_y is None or by > max_y:
                max_y = by
            if min_z is None or bz < min_z:
                min_z = bz
            if max_z is None or bz > max_z:
                max_z = bz

            # NBT 检测
            if block.nbt:
                has_nbt = True
                # 命令方块检测（通过 NBT 中的 CommandBlock 标识）
                nbt_id = ""
                if isinstance(block.nbt, dict):
                    nbt_id = str(block.nbt.get("id", ""))
                if "CommandBlock" in nbt_id or "command_block" in nbt_id:
                    has_command_blocks = True

            # 命令方块检测（通过运行时 ID）
            if block.rtid in _COMMAND_BLOCK_RTIDS:
                has_command_blocks = True

            # 方块类型统计
            rtid = block.rtid
            block_type_counts[rtid] = block_type_counts.get(rtid, 0) + 1
    except Exception as exc:  # noqa: BLE001
        result["parse_error"] = f"遍历方块流失败: {exc}"

    # 填充结果
    result["block_count"] = block_count
    result["has_nbt"] = has_nbt
    result["has_command_blocks"] = has_command_blocks
    if min_x is not None:
        result["chunk_range"] = {
            "min": [int(min_x), int(min_y), int(min_z)],
            "max": [int(max_x), int(max_y), int(max_z)],
        }
        result["dimensions"] = {
            "x": int(max_x) - int(min_x) + 1 if max_x is not None else 0,
            "y": int(max_y) - int(min_y) + 1 if max_y is not None else 0,
            "z": int(max_z) - int(min_z) + 1 if max_z is not None else 0,
        }

    # 方块类型统计（按数量降序，取前 20 个）
    # 注: 此处运行时 ID 仅作内部统计，名称字段留空（完整映射表较大，
    # 此处不展开）。前端可基于运行时 ID 自行映射。
    sorted_types = sorted(
        block_type_counts.items(), key=lambda kv: kv[1], reverse=True
    )[:20]
    result["block_types"] = [
        {"name": f"rtid:{rtid}", "count": count, "rtid": rtid}
        for rtid, count in sorted_types
    ]

    return result


@router.get("/{file_id}/preview")
async def preview_file(
    file_id: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """获取文件预览信息。

    后端解析建筑/结构文件，返回元信息（方块数、尺寸、是否含 NBT/命令方块等）。
    不返回原始解析数据，防止泄露技术内容。

    Path 参数:
        file_id: 文件标识符，支持 ``plugin_id/filename`` 或纯 ``filename`` 形式。

    返回格式::

        {
            "file_name": "xxx.bdx",
            "format": "bdx",
            "file_size": 12345,
            "block_count": 1000,
            "chunk_range": {"min": [0,0,0], "max": [15,255,15]},
            "has_nbt": true,
            "has_command_blocks": false,
            "block_types": [{"name": "rtid:1", "count": 500}, ...],
            "dimensions": {"x": 16, "y": 256, "z": 16},
            "preview_image": null
        }
    """
    try:
        file_path = _resolve_file_id(file_id)

        # 基本文件信息
        try:
            stat = file_path.stat()
            file_size = stat.st_size
        except OSError:
            file_size = 0

        file_name = file_path.name
        ext = file_path.suffix.lower().lstrip(".")
        format_name = ext or "unknown"

        # 解析文件获取预览信息
        preview = await _parse_file_for_preview(file_path)

        data: Dict[str, Any] = {
            "file_name": file_name,
            "format": format_name,
            "file_size": file_size,
            "block_count": preview.get("block_count", 0),
            "chunk_range": preview.get("chunk_range", {"min": None, "max": None}),
            "has_nbt": preview.get("has_nbt", False),
            "has_command_blocks": preview.get("has_command_blocks", False),
            "block_types": preview.get("block_types", []),
            "dimensions": preview.get("dimensions", {"x": 0, "y": 0, "z": 0}),
            "preview_image": None,
        }
        if preview.get("parse_error"):
            data["parse_error"] = preview["parse_error"]

        return success_response(
            data=data,
            message=f"文件预览: {file_name}",
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"文件预览失败: {file_id}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"文件预览失败: {exc}",
        )


@router.get("/{plugin_id}/{filename:path}")
async def download_plugin_file(
    plugin_id: str,
    filename: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> FileResponse:
    """下载插件文件。"""
    root = _plugin_root(plugin_id)
    target = _safe_join(root, filename)

    if not target.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件不存在: {filename}",
        )
    if target.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目标路径是目录，无法下载",
        )
    return FileResponse(path=str(target), filename=target.name)


@router.delete("/{plugin_id}/{filename:path}")
async def delete_plugin_file(
    plugin_id: str,
    filename: str,
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """删除插件文件或目录。"""
    root = _plugin_root(plugin_id)
    target = _safe_join(root, filename)

    if not target.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件不存在: {filename}",
        )

    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        logger.exception(f"删除文件失败 (plugin={plugin_id}, file={filename})")
        return error_response(error="delete_failed", message=f"删除失败: {exc}")

    logger.info(f"删除 {plugin_id}/{filename}")
    return success_response(message=f"{filename} 已删除")


@router.post("/{plugin_id}/folder")
async def create_folder(
    plugin_id: str,
    name: str = Query(..., description="目录名（可含子路径）"),
    _user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """在插件目录下创建子目录。"""
    root = _plugin_root(plugin_id)
    target = _safe_join(root, name)

    if target.exists():
        if target.is_dir():
            return success_response(message=f"目录已存在: {name}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"已存在同名文件: {name}",
        )

    try:
        target.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        logger.exception(f"创建目录失败 (plugin={plugin_id}, name={name})")
        return error_response(error="mkdir_failed", message=f"创建失败: {exc}")

    logger.info(f"创建目录 {plugin_id}/{name}")
    return success_response(
        data={"path": name, "created_at": time.time()},
        message=f"目录 {name} 已创建",
    )


__all__ = ["router"]
