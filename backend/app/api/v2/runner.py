"""可执行文件运行器 API - 在服务器上运行脚本 / 命令并查看输出。

路由前缀: ``/api/v2/runner``

功能:

    1. ``POST /execute``  - 执行一条 shell 命令或一段脚本内容
    2. ``GET  /files``    - 列出允许目录下的可执行文件
    3. ``POST /upload``   - 上传脚本文件到脚本目录
    4. ``DELETE /files/{filename}`` - 删除脚本文件
    5. ``WS    /ws``      - WebSocket 实时输出 (支持 Ctrl+C 终止)

安全措施:

    - 仅 ``admin`` / ``superadmin`` 可访问所有端点
    - 命令注入防护: 对 shell 命令做基本危险模式检查
    - 超时保护: 默认 30 秒, 最大 120 秒
    - 路径限制: 只允许在 ``/workspace`` 和 ``/data/user/work`` 下操作
    - 子进程以独立会话启动, 支持向整个进程组发送信号 (Ctrl+C)

统一 JSON 响应格式::

    {
        "success": true | false,
        "message": "...",
        "data": ...,
        "error": "..."   # success=False 时存在
    }
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.logger import get_logger
from app.security import verify_jwt_token

logger = get_logger("api.runner")

router = APIRouter(prefix="/api/v2/runner", tags=["runner"])


# ============================================================================
# 常量与路径配置
# ============================================================================

#: 允许操作的根目录 (所有文件 / 工作目录操作都必须落在这两个目录之内)
ALLOWED_ROOTS: List[Path] = [
    Path("/workspace").resolve(),
    Path("/data/user/work").resolve(),
]

#: 上传脚本的保存目录
SCRIPTS_DIR: Path = Path("/data/user/work/scripts")

#: 默认超时 (秒)
DEFAULT_TIMEOUT: int = 30

#: 最大超时 (秒)
MAX_TIMEOUT: int = 120

#: 单个上传文件大小上限 (10 MiB)
MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024

#: 允许上传的脚本扩展名
ALLOWED_EXTENSIONS: Tuple[str, ...] = (
    ".py", ".go", ".java", ".js", ".sh", ".bash",
    ".exe", ".bin", ".rb", ".pl", ".php", ".ts", ".rs", ".c", ".cpp",
)

#: 文件扩展名 -> 启动命令 (用于 ``filename`` / ``content`` 模式)
#: 值为 ``None`` 表示直接执行该文件 (需可执行权限)
INTERPRETERS: Dict[str, Optional[List[str]]] = {
    ".py": ["python3", "-u"],
    ".sh": ["bash"],
    ".bash": ["bash"],
    ".go": ["go", "run"],
    ".java": ["java"],  # Java 11+ 单文件源码模式: java File.java
    ".js": ["node"],
    ".ts": ["npx", "ts-node"],
    ".rb": ["ruby"],
    ".pl": ["perl"],
    ".php": ["php"],
    ".rs": ["rustc"],  # rustc 编译后需手动运行, 此为简化处理
    ".exe": None,
    ".bin": None,
}

#: 列出文件时显示的最大条目数 (每个目录)
MAX_LIST_ENTRIES: int = 500

#: 危险命令模式 (命令注入 / 破坏性命令的基本拦截)
DANGEROUS_PATTERNS: Tuple[str, ...] = (
    "rm -rf /",
    "rm -rf /*",
    "mkfs",
    "dd if=/dev/zero of=/dev/sd",
    "dd if=/dev/zero of=/dev/nvm",
    ":(){:|:&};:",
    "> /dev/sda",
    "> /dev/nvme",
    "shutdown",
    "reboot",
    "halt",
    "init 0",
    "init 6",
)


# ============================================================================
# 请求模型
# ============================================================================

class ExecuteRequest(BaseModel):
    """执行请求体。

    两种模式二选一:

        1. ``command`` 模式: 直接执行一条 shell 命令字符串
        2. ``filename`` + ``content`` 模式: 先把内容写入临时脚本文件, 再执行

    ``cwd`` 限定子进程工作目录, 必须位于允许的根目录之下。
    """

    command: Optional[str] = Field(
        None, max_length=8192, description="要执行的 shell 命令"
    )
    filename: Optional[str] = Field(
        None, max_length=255, description="脚本文件名 (content 模式下必填)"
    )
    content: Optional[str] = Field(
        None, max_length=4 * 1024 * 1024, description="脚本内容 (content 模式下必填)"
    )
    cwd: Optional[str] = Field(
        None, max_length=1024, description="子进程工作目录 (须在允许根目录下)"
    )
    timeout: int = Field(
        DEFAULT_TIMEOUT, ge=1, le=MAX_TIMEOUT, description="超时秒数 (1-120)"
    )


class RenameRequest(BaseModel):
    """重命名请求体。

    ``new_name`` 为清洗后的新文件名 (仅 basename, 不允许包含路径分隔符)。
    """

    new_name: str = Field(
        ..., max_length=255, description="新文件名 (仅 basename)"
    )


# ============================================================================
# 安全辅助函数
# ============================================================================

def _ensure_scripts_dir() -> Path:
    """确保脚本目录存在并返回其绝对路径。"""
    scripts = SCRIPTS_DIR.resolve()
    scripts.mkdir(parents=True, exist_ok=True)
    return scripts


def _is_safe_path(path: Path, must_exist: bool = False) -> bool:
    """校验 ``path`` 是否位于允许的根目录之下。

    ``must_exist=True`` 时还要求路径真实存在。
    """
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        return False
    for root in ALLOWED_ROOTS:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        if must_exist and not resolved.exists():
            return False
        return True
    return False


def _validate_cwd(cwd: Optional[str]) -> str:
    """校验并返回可用的工作目录字符串。

    若 ``cwd`` 为空则回退到 ``/data/user/work``。校验不通过抛 400。
    """
    if not cwd:
        fallback = ALLOWED_ROOTS[1]
        fallback.mkdir(parents=True, exist_ok=True)
        return str(fallback)
    candidate = Path(cwd)
    if not _is_safe_path(candidate, must_exist=True):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法的工作目录或目录不存在: {cwd} (仅允许在 /workspace 或 /data/user/work 下)",
        )
    return str(candidate.resolve())


def _safe_filename(filename: str) -> str:
    """清洗文件名, 只保留 basename 并拒绝路径穿越。"""
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件名不能为空",
        )
    name = os.path.basename(filename)
    if not name or name in (".", ".."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件名",
        )
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件名 (包含路径分隔符)",
        )
    return name


def _check_command_safety(command: str) -> None:
    """对 shell 命令做基本危险模式检查。

    这并非完整的沙箱, 仅拦截明显的破坏性命令。管理员本身是可信用户,
    此处主要防止误操作 (如 ``rm -rf /``)。
    """
    if not command or not command.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="命令不能为空",
        )
    normalized = command.lower()
    for pattern in DANGEROUS_PATTERNS:
        if pattern in normalized:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"命令包含被禁止的危险模式: {pattern}",
            )


def _build_run_command(filepath: str) -> List[str]:
    """根据文件扩展名构造执行命令 (exec 模式)。

    ``filepath`` 可以是 basename 或完整路径。
    对于需要编译的语言 (rust / c / cpp) 退化为直接执行或交由 shell 处理。
    """
    ext = Path(filepath).suffix.lower()
    prefix = INTERPRETERS.get(ext)
    if prefix is None and ext in INTERPRETERS:
        # 直接执行该文件 (需可执行权限)
        return [filepath]
    if prefix:
        if ext == ".go":
            return ["go", "run", filepath]
        return prefix + [filepath]
    # 未知扩展名: 尝试直接执行
    return [filepath]


def _validate_timeout(timeout: Any) -> int:
    """校验超时参数, 返回 clamp 后的整数秒数。"""
    try:
        t = int(timeout)
    except (TypeError, ValueError):
        t = DEFAULT_TIMEOUT
    if t < 1:
        t = 1
    if t > MAX_TIMEOUT:
        t = MAX_TIMEOUT
    return t


def _write_temp_script(filename: str, content: str, cwd: str) -> Path:
    """将脚本内容写入工作目录下的文件, 返回文件路径。

    文件落在 ``cwd`` (已校验) 下, 若 ``cwd`` 不可写则回退到脚本目录。
    """
    safe_name = _safe_filename(filename)
    # 优先写到 cwd 下, 失败则回退到 SCRIPTS_DIR
    target_dir = Path(cwd)
    if not _is_safe_path(target_dir, must_exist=True) or not os.access(
        str(target_dir), os.W_OK
    ):
        target_dir = _ensure_scripts_dir()
    target = (target_dir / safe_name).resolve()
    # 再次校验写入后的路径未越界
    if not _is_safe_path(target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的目标文件路径",
        )
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入脚本文件失败: {exc}",
        )
    # 对 shell / 可执行文件尝试赋予执行权限
    if Path(safe_name).suffix.lower() in (".sh", ".bash", ".exe", ".bin"):
        try:
            target.chmod(target.stat().st_mode | 0o755)
        except OSError:
            pass
    return target


# ============================================================================
# 子进程执行核心
# ============================================================================

async def _read_stream(stream: asyncio.StreamReader, sink: List[str]) -> None:
    """逐行读取子进程输出流, 追加到 ``sink`` 列表。"""
    while True:
        line = await stream.readline()
        if not line:
            break
        try:
            text = line.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = line.decode("latin-1", errors="replace")
        sink.append(text)


async def _run_subprocess(
    args: List[str],
    cwd: str,
    timeout: int,
    use_shell: bool = False,
) -> Tuple[int, str, str, float, Optional[str]]:
    """运行子进程并实时收集输出。

    Args:
        args: 命令参数列表 (use_shell=False) 或 [command_string] (use_shell=True)
        cwd: 工作目录
        timeout: 超时秒数
        use_shell: 是否通过 shell 执行

    Returns:
        (exit_code, stdout, stderr, duration, error)
        其中 ``error`` 在超时 / 启动失败时为非空字符串。
    """
    start = time.time()
    stdout_lines: List[str] = []
    stderr_lines: List[str] = []
    error: Optional[str] = None
    exit_code: int = -1

    try:
        if use_shell:
            # args 应为 [command_string]
            proc = await asyncio.create_subprocess_shell(
                args[0],
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # 独立会话, 便于向进程组发信号
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
    except FileNotFoundError as exc:
        return -1, "", f"可执行文件未找到: {exc}", time.time() - start, "not_found"
    except OSError as exc:
        return -1, "", f"启动子进程失败: {exc}", time.time() - start, "spawn_failed"

    stdout_task = asyncio.create_task(_read_stream(proc.stdout, stdout_lines))
    stderr_task = asyncio.create_task(_read_stream(proc.stderr, stderr_lines))

    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        exit_code = proc.returncode if proc.returncode is not None else -1
    except asyncio.TimeoutError:
        error = f"timeout ({timeout}s)"
        # 向整个进程组发送 SIGTERM, 再 SIGKILL 兜底
        _terminate_process(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            _kill_process(proc)
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
        exit_code = -1
    finally:
        # 确保读取任务结束
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    duration = round(time.time() - start, 3)
    stdout_text = "".join(stdout_lines)
    stderr_text = "".join(stderr_lines)
    return exit_code, stdout_text, stderr_text, duration, error


def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """向进程组发送 SIGTERM。"""
    try:
        if proc.returncode is None and proc.pid:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
    except Exception:  # noqa: BLE001
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """向进程组发送 SIGKILL。"""
    try:
        if proc.returncode is None and proc.pid:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _signal_process(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """向进程组发送指定信号, 返回是否成功。"""
    try:
        if proc.returncode is not None or not proc.pid:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError):
            proc.send_signal(sig)
        return True
    except Exception:  # noqa: BLE001
        return False


# ============================================================================
# 1. POST /execute - 执行命令或脚本
# ============================================================================

@router.post("/execute")
async def execute(req: ExecuteRequest, request: Request):
    """执行一条 shell 命令或一段脚本内容 (仅管理员)。

    两种模式:

        * ``command`` 模式: 通过 shell 执行命令字符串
        * ``filename`` + ``content`` 模式: 先写入脚本文件, 再以对应解释器执行

    返回实时收集的 stdout / stderr、退出码与耗时。
    """
    from .auth import require_admin
    admin = await require_admin(request)

    timeout = _validate_timeout(req.timeout)
    cwd = _validate_cwd(req.cwd)

    # 模式判定
    if req.command:
        # command 模式
        _check_command_safety(req.command)
        logger.info(
            f"管理员 {admin['username']} 执行命令 (cwd={cwd}, timeout={timeout}s): "
            f"{req.command}"
        )
        exit_code, out, err, duration, error = await _run_subprocess(
            [req.command], cwd, timeout, use_shell=True
        )
    elif req.filename and req.content is not None:
        # content 模式: 写入文件后执行
        safe_name = _safe_filename(req.filename)
        ext = Path(safe_name).suffix.lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"不支持的脚本扩展名: {ext} (允许: {', '.join(ALLOWED_EXTENSIONS)})",
            )
        script_path = _write_temp_script(safe_name, req.content, cwd)
        run_args = _build_run_command(str(script_path))
        logger.info(
            f"管理员 {admin['username']} 执行脚本 "
            f"({script_path}, cwd={cwd}, timeout={timeout}s): {' '.join(run_args)}"
        )
        exit_code, out, err, duration, error = await _run_subprocess(
            run_args, cwd, timeout, use_shell=False
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="必须提供 command 或 (filename + content)",
        )

    # 合并输出: stdout 在前, stderr 在后 (便于查看)
    combined = out
    if err:
        combined = (combined + ("\n" if combined and not combined.endswith("\n") else "")
                    + "[stderr]\n" + err) if combined else err

    success = error is None and exit_code == 0
    return {
        "success": success,
        "output": combined,
        "stdout": out,
        "stderr": err,
        "exit_code": exit_code,
        "duration": duration,
        "error": error,
    }


# ============================================================================
# 2. GET /files - 列出可执行文件目录
# ============================================================================

def _list_dir_files(root: Path) -> List[Dict[str, Any]]:
    """列出 ``root`` 目录下的文件 (非递归, 最多 MAX_LIST_ENTRIES 条)。"""
    entries: List[Dict[str, Any]] = []
    if not root.exists() or not root.is_dir():
        return entries
    try:
        children = sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except (PermissionError, OSError):
        return entries
    for child in children:
        if len(entries) >= MAX_LIST_ENTRIES:
            break
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "path": str(child.resolve()),
            "is_dir": child.is_dir(),
            "size": stat.st_size if child.is_file() else 0,
            "ext": child.suffix.lower() if child.is_file() else "",
            "modified": stat.st_mtime,
            "executable": bool(stat.st_mode & 0o111) if child.is_file() else False,
        })
    return entries


@router.get("/files")
async def list_files(
    request: Request,
    subdir: Optional[str] = Query(None, description="相对允许根目录的子目录"),
):
    """列出 ``/workspace`` 和 ``/data/user/work`` 下的文件 (仅管理员)。

    可通过 ``subdir`` 指定一个相对子目录, 路径必须在允许根目录之下。
    """
    from .auth import require_admin
    await require_admin(request)

    result: Dict[str, Any] = {}

    for root in ALLOWED_ROOTS:
        target = root
        if subdir:
            target = (root / subdir).resolve()
            if not _is_safe_path(target, must_exist=True):
                # 在其它根目录下尝试同样的子目录, 若都不可用则跳过
                continue
        key = str(root)
        result[key] = _list_dir_files(target)

    # 单独标注脚本目录
    scripts = _ensure_scripts_dir()
    result["__scripts_dir__"] = {
        "path": str(scripts),
        "files": _list_dir_files(scripts),
    }

    return {"success": True, "data": result}


# ============================================================================
# 3. POST /upload - 上传脚本文件
# ============================================================================

@router.post("/upload")
async def upload_script(
    request: Request,
    file: UploadFile = File(..., description="要上传的脚本文件"),
    overwrite: bool = Form(False, description="是否覆盖同名文件"),
):
    """上传脚本文件到 ``/data/user/work/scripts/`` (仅管理员)。

    返回保存后的文件绝对路径。
    """
    from .auth import require_admin
    admin = await require_admin(request)

    original_name = _safe_filename(file.filename or "")
    ext = Path(original_name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的脚本扩展名: {ext} (允许: {', '.join(ALLOWED_EXTENSIONS)})",
        )

    scripts = _ensure_scripts_dir()
    dest = (scripts / original_name).resolve()
    if not _is_safe_path(dest):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的目标文件路径",
        )
    if dest.exists() and not overwrite:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"文件已存在: {original_name} (可设置 overwrite=true 覆盖)",
        )

    # 流式写入并累计大小
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
                        detail=f"文件过大, 上限 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"写入文件失败: {exc}",
        )
    finally:
        await file.close()

    # 对可执行类型赋予执行权限
    if ext in (".sh", ".bash", ".exe", ".bin"):
        try:
            dest.chmod(dest.stat().st_mode | 0o755)
        except OSError:
            pass

    logger.info(
        f"管理员 {admin['username']} 上传脚本 {original_name} "
        f"({written} bytes -> {dest})"
    )

    return {
        "success": True,
        "message": "上传成功",
        "data": {
            "filename": original_name,
            "path": str(dest),
            "size": written,
        },
    }


# ============================================================================
# 4. DELETE /files/{filename} - 删除脚本文件
# ============================================================================

@router.delete("/files/{filename}")
async def delete_script(filename: str, request: Request):
    """删除脚本目录下的指定脚本文件 (仅管理员)。

    出于安全考虑, 仅允许删除 ``/data/user/work/scripts/`` 下的文件,
    且文件名经过清洗。
    """
    from .auth import require_admin
    admin = await require_admin(request)

    safe_name = _safe_filename(filename)
    scripts = _ensure_scripts_dir()
    target = (scripts / safe_name).resolve()
    if not _is_safe_path(target):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件路径",
        )
    if not target.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件不存在: {safe_name}",
        )

    try:
        target.unlink()
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"删除文件失败: {exc}",
        )

    logger.info(f"管理员 {admin['username']} 删除脚本 {safe_name} ({target})")

    return {"success": True, "message": f"已删除: {safe_name}"}


# ============================================================================
# 5. PUT /files/{filename}/rename - 重命名文件
# ============================================================================

@router.put("/files/{filename}/rename")
async def rename_script(
    filename: str,
    req: RenameRequest,
    request: Request,
    overwrite: bool = Query(False, description="目标已存在时是否覆盖"),
):
    """重命名脚本目录下的文件 (仅管理员)。

    将 ``filename`` 重命名为 ``req.new_name``, 两者均位于脚本目录下。
    若目标文件已存在, 需显式设置 ``overwrite=true`` 才能覆盖。
    """
    from .auth import require_admin
    admin = await require_admin(request)

    old_name = _safe_filename(filename)
    new_name = _safe_filename(req.new_name)

    scripts = _ensure_scripts_dir()
    src = (scripts / old_name).resolve()
    if not _is_safe_path(src, must_exist=True):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件不存在: {old_name}",
        )
    if not src.is_file():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"仅支持重命名文件: {old_name}",
        )

    dst = (scripts / new_name).resolve()
    if not _is_safe_path(dst):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的目标文件路径",
        )
    if dst != src and dst.exists() and not overwrite:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"目标文件已存在: {new_name} (可设置 overwrite=true 覆盖)",
        )

    try:
        os.rename(src, dst)
    except OSError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"重命名文件失败: {exc}",
        )

    logger.info(f"管理员 {admin['username']} 重命名文件 {old_name} -> {new_name}")

    return {
        "success": True,
        "data": {
            "old_name": old_name,
            "new_name": new_name,
            "path": str(dst),
        },
    }


# ============================================================================
# 6. GET /files/{filename}/download - 下载文件
# ============================================================================

@router.get("/files/{filename}/download")
async def download_script(filename: str, request: Request):
    """下载文件 (仅管理员)。

    在脚本目录及允许根目录下查找 ``filename``, 找到后以附件形式返回文件流。
    """
    from .auth import require_admin
    admin = await require_admin(request)

    safe_name = _safe_filename(filename)

    # 依次在 SCRIPTS_DIR 和 ALLOWED_ROOTS 中查找文件
    target: Optional[Path] = None
    scripts = _ensure_scripts_dir()
    candidates: List[Path] = [(scripts / safe_name).resolve()]
    for root in ALLOWED_ROOTS:
        candidates.append((root / safe_name).resolve())

    for cand in candidates:
        if cand.is_file() and _is_safe_path(cand, must_exist=True):
            target = cand
            break

    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件不存在: {safe_name}",
        )

    logger.info(f"管理员 {admin['username']} 下载文件 {filename}")

    return FileResponse(
        path=str(target),
        filename=safe_name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


# ============================================================================
# 7. POST /files/{filename}/compress - 压缩文件或目录
# ============================================================================

@router.post("/files/{filename}/compress")
async def compress_file(filename: str, request: Request):
    """压缩文件或目录为同名 ``.zip`` 压缩包 (仅管理员)。

    在允许根目录 (含脚本目录) 下查找 ``filename``:

        * 若为文件, 直接打包该文件
        * 若为目录, 递归打包整个目录 (保留相对目录结构)

    压缩包生成在源文件所在目录下, 文件名为 ``{filename}.zip``。
    """
    from .auth import require_admin
    admin = await require_admin(request)

    safe_name = _safe_filename(filename)

    # 在 ALLOWED_ROOTS (含 SCRIPTS_DIR) 下查找文件 / 目录
    target: Optional[Path] = None
    scripts = _ensure_scripts_dir()
    search_roots: List[Path] = [scripts] + list(ALLOWED_ROOTS)
    for root in search_roots:
        cand = (root / safe_name).resolve()
        if cand.exists() and _is_safe_path(cand, must_exist=True):
            target = cand
            break

    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"文件或目录不存在: {safe_name}",
        )

    # 压缩包输出路径 (同名 .zip, 位于源文件同级目录)
    zip_name = f"{safe_name}.zip"
    zip_path = (target.parent / zip_name).resolve()
    if not _is_safe_path(zip_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的压缩包输出路径",
        )

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if target.is_file():
                # 打包单个文件, 归档名为原文件名
                zf.write(target, arcname=safe_name)
            elif target.is_dir():
                # 递归打包目录, 归档路径相对父目录 (保留顶层目录名)
                for child in target.rglob("*"):
                    if child.is_file():
                        arc = child.relative_to(target.parent)
                        zf.write(child, arcname=str(arc))
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"不支持的文件类型: {safe_name}",
                )
    except HTTPException:
        raise
    except OSError as exc:
        # 压缩失败时清理可能生成的半成品文件
        try:
            zip_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"压缩失败: {exc}",
        )

    size = zip_path.stat().st_size
    logger.info(f"管理员 {admin['username']} 压缩文件 {filename}")

    return {
        "success": True,
        "data": {
            "filename": zip_name,
            "path": str(zip_path),
            "size": size,
        },
    }


# ============================================================================
# 8. WebSocket /ws - 实时输出
# ============================================================================

async def _authenticate_ws(token: str) -> Optional[dict]:
    """通过 JWT token 认证 WebSocket 连接, 返回用户字典 (仅管理员通过)。"""
    if not token:
        return None
    payload = verify_jwt_token(token)
    if not payload or not payload.get("user_id"):
        return None
    try:
        from app.database import get_db
        db = await get_db()
        user = await db.get_user_by_id(payload["user_id"])
    except Exception:  # noqa: BLE001
        return None
    if not user or user.get("status") != "active":
        return None
    if user.get("role") not in ("admin", "superadmin"):
        return None
    return dict(user)


async def _ws_stream_output(
    websocket: WebSocket,
    proc: asyncio.subprocess.Process,
) -> None:
    """并发读取 stdout / stderr 并按行推送给客户端。"""

    async def _pump(stream: asyncio.StreamReader, stream_type: str) -> None:
        while True:
            try:
                line = await stream.readline()
            except Exception:  # noqa: BLE001
                break
            if not line:
                break
            try:
                text = line.decode("utf-8", errors="replace").rstrip("\n")
            except Exception:  # noqa: BLE001
                text = line.decode("latin-1", errors="replace").rstrip("\n")
            await websocket.send_json({
                "type": stream_type,
                "data": text,
                "timestamp": time.time(),
            })

    tasks = []
    if proc.stdout:
        tasks.append(asyncio.create_task(_pump(proc.stdout, "stdout")))
    if proc.stderr:
        tasks.append(asyncio.create_task(_pump(proc.stderr, "stderr")))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/ws")
async def runner_ws(
    websocket: WebSocket,
    token: str = Query("", description="JWT token"),
):
    """WebSocket 实时运行器 (仅管理员)。

    客户端发送的消息::

        {"action": "execute", "command": "python3 test.py", "cwd": "/data/user/work", "timeout": 30}
        {"action": "execute", "filename": "test.py", "content": "print(1)", "cwd": "..."}
        {"action": "signal", "signal": "SIGINT"}   # 发送 Ctrl+C
        {"action": "signal", "signal": "SIGKILL"}  # 强制终止
        {"action": "signal", "signal": "SIGTERM"}
        {"action": "ping"}

    服务端推送的消息::

        {"type": "ready"}
        {"type": "started", "pid": 1234}
        {"type": "stdout" | "stderr", "data": "...", "timestamp": ...}
        {"type": "exited", "exit_code": 0, "duration": 1.5}
        {"type": "error", "message": "..."}
        {"type": "pong"}
    """
    # 认证
    user = await _authenticate_ws(token)
    if user is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "认证失败或权限不足"})
        await websocket.close(code=4401)
        return

    await websocket.accept()
    await websocket.send_json({"type": "ready", "user": user["username"]})
    logger.info(f"runner WebSocket 接入: 管理员 {user['username']}")

    current_proc: Optional[asyncio.subprocess.Process] = None

    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "message": "无效的 JSON"})
                continue

            action = msg.get("action")

            if action == "ping":
                await websocket.send_json({"type": "pong"})
                continue

            if action == "signal":
                # 向当前正在运行的子进程发送信号
                if current_proc is None or current_proc.returncode is not None:
                    await websocket.send_json(
                        {"type": "error", "message": "当前没有正在运行的进程"}
                    )
                    continue
                sig_name = str(msg.get("signal", "SIGINT")).upper()
                sig_map = {
                    "SIGINT": signal.SIGINT,
                    "SIGTERM": signal.SIGTERM,
                    "SIGKILL": signal.SIGKILL,
                }
                sig = sig_map.get(sig_name, signal.SIGINT)
                ok = _signal_process(current_proc, sig)
                await websocket.send_json({
                    "type": "signal_sent",
                    "signal": sig_name,
                    "success": ok,
                })
                continue

            if action == "execute":
                # 若已有进程在跑, 先终止
                if current_proc is not None and current_proc.returncode is None:
                    _kill_process(current_proc)
                    try:
                        await asyncio.wait_for(current_proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        pass

                timeout = _validate_timeout(msg.get("timeout", DEFAULT_TIMEOUT))

                command = msg.get("command")
                filename = msg.get("filename")
                content = msg.get("content")

                start = time.time()
                try:
                    cwd = _validate_cwd(msg.get("cwd"))
                    if command:
                        _check_command_safety(str(command))
                        proc = await asyncio.create_subprocess_shell(
                            str(command),
                            cwd=cwd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            start_new_session=True,
                        )
                    elif filename and content is not None:
                        safe_name = _safe_filename(str(filename))
                        ext = Path(safe_name).suffix.lower()
                        if ext not in ALLOWED_EXTENSIONS:
                            await websocket.send_json({
                                "type": "error",
                                "message": f"不支持的脚本扩展名: {ext}",
                            })
                            continue
                        script_path = _write_temp_script(safe_name, str(content), cwd)
                        run_args = _build_run_command(str(script_path))
                        proc = await asyncio.create_subprocess_exec(
                            *run_args,
                            cwd=cwd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            start_new_session=True,
                        )
                    else:
                        await websocket.send_json({
                            "type": "error",
                            "message": "必须提供 command 或 (filename + content)",
                        })
                        continue
                except HTTPException as exc:
                    await websocket.send_json({
                        "type": "error",
                        "message": exc.detail,
                    })
                    continue
                except FileNotFoundError as exc:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"可执行文件未找到: {exc}",
                    })
                    continue
                except OSError as exc:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"启动子进程失败: {exc}",
                    })
                    continue

                current_proc = proc
                await websocket.send_json({
                    "type": "started",
                    "pid": proc.pid,
                    "command": str(command) if command else (filename or ""),
                })

                # 并发: 推送输出 + 监听信号
                stream_task = asyncio.create_task(
                    _ws_stream_output(websocket, proc)
                )

                # 信号监听协程: 在进程运行期间持续接收消息以支持 Ctrl+C
                listener_cancelled = False

                async def _listen_for_signals() -> None:
                    nonlocal listener_cancelled
                    while True:
                        try:
                            inner = await websocket.receive_text()
                        except (WebSocketDisconnect, RuntimeError):
                            # 客户端断开或连接异常: 杀掉进程
                            if current_proc is not None and current_proc.returncode is None:
                                _kill_process(current_proc)
                            return
                        try:
                            inner_msg = json.loads(inner)
                        except json.JSONDecodeError:
                            continue
                        if inner_msg.get("action") == "signal":
                            sig_name = str(inner_msg.get("signal", "SIGINT")).upper()
                            sig_map = {
                                "SIGINT": signal.SIGINT,
                                "SIGTERM": signal.SIGTERM,
                                "SIGKILL": signal.SIGKILL,
                            }
                            sig = sig_map.get(sig_name, signal.SIGINT)
                            _signal_process(proc, sig)
                        elif inner_msg.get("action") == "kill":
                            _kill_process(proc)

                listener_task = asyncio.create_task(_listen_for_signals())

                # 等待进程结束 (带超时)
                timed_out = False
                try:
                    await asyncio.wait_for(proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    timed_out = True
                    _terminate_process(proc)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        _kill_process(proc)
                        try:
                            await proc.wait()
                        except Exception:  # noqa: BLE001
                            pass

                # 停止信号监听
                listener_cancelled = True
                listener_task.cancel()
                try:
                    await listener_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

                # 等待输出推送完成
                try:
                    await asyncio.wait_for(stream_task, timeout=5)
                except asyncio.TimeoutError:
                    stream_task.cancel()
                    try:
                        await stream_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

                duration = round(time.time() - start, 3)
                exit_code = proc.returncode if proc.returncode is not None else -1
                await websocket.send_json({
                    "type": "exited",
                    "exit_code": exit_code,
                    "duration": duration,
                    "timed_out": timed_out,
                })
                current_proc = None
                continue

            # 未知 action
            await websocket.send_json({
                "type": "error",
                "message": f"未知的 action: {action}",
            })

    except WebSocketDisconnect:
        logger.info(f"runner WebSocket 断开: 管理员 {user['username']}")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"runner WebSocket 异常: {exc!r}")
        try:
            await websocket.send_json({"type": "error", "message": f"内部错误: {exc}"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        # 连接关闭前确保子进程被清理
        if current_proc is not None and current_proc.returncode is None:
            _kill_process(current_proc)
            try:
                await asyncio.wait_for(current_proc.wait(), timeout=3)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = ["router"]
