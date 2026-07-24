"""用户文件管理 API - 上传、下载、审核、购买。

路由前缀: ``/api/v2/files``

功能:

    - 普通用户可上传插件 / 建筑文件 (默认 ``pending``，需管理员审核)
    - 管理员上传的文件立即 ``approved``
    - 文件可设置价格，付费文件需购买后才能下载
    - 管理员可审核 (approve / reject) 待处理文件

文件保存路径: ``backend/data/uploads/{file_id}_{original_filename}``

路径安全: 所有文件操作都被限制在 uploads 目录内，
通过 ``resolve()`` + ``is_relative_to()`` 双重校验防止路径穿越攻击。
"""
from __future__ import annotations

import os
import time
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel

from app.config import DATA_DIR
from app.database import get_db
from app.logger import get_logger

logger = get_logger("api.user_files")

router = APIRouter(prefix="/api/v2/files", tags=["user_files"])


# ============================================================================
# 常量
# ============================================================================

#: 上传根目录
UPLOADS_DIR: Path = DATA_DIR / "uploads"

#: 合法的文件分类
VALID_CATEGORIES = ("plugin", "building")

#: 管理员单文件大小上限 (10 MiB)
ADMIN_MAX_UPLOAD_BYTES: int = 10 * 1024 * 1024

#: 名称 / 描述长度限制
MAX_NAME_LEN: int = 18
MAX_DESC_LEN: int = 60

#: 上传频率限制 (秒): 每用户每小时一次
UPLOAD_RATE_LIMIT_SECONDS: int = 3600


# ============================================================================
# 路径安全辅助函数 (借鉴 app/api/files.py 的 _safe_join / _plugin_root)
# ============================================================================

def _uploads_root() -> Path:
    """返回上传根目录 (自动创建)。"""
    root = UPLOADS_DIR.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(filename: str) -> str:
    """清洗文件名，只保留 basename 并拒绝路径穿越。"""
    if not filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件名不能为空",
        )
    # 仅取 basename，剥离任何目录前缀
    name = os.path.basename(filename)
    if not name or name in (".", ".."):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件名",
        )
    # 拒绝包含路径分隔符 / 模糊形式的文件名
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件名",
        )
    return name


def _resolve_stored_path(file_id: str, original_filename: str) -> Path:
    """拼接种储路径 ``{file_id}_{original_filename}`` 并校验未越界。"""
    root = _uploads_root()
    safe_name = _safe_filename(original_filename)
    stored_name = f"{file_id}_{safe_name}"
    target = (root / stored_name).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法的文件路径（路径穿越检测失败）",
        )
    return target


def _gen_file_id() -> str:
    return f"f_{uuid.uuid4().hex[:12]}"


def _now() -> float:
    return time.time()


def _file_to_dict(f) -> dict:
    return {
        "file_id": f["file_id"],
        "user_id": f["user_id"],
        "category": f["category"],
        "name": f["name"],
        "description": f["description"],
        "price": f["price"],
        "file_path": f["file_path"],
        "file_size": f["file_size"],
        "status": f["status"],
        "reject_reason": f["reject_reason"],
        "download_count": f["download_count"],
        "created_at": f["created_at"],
    }


# ============================================================================
# 1. 文件列表 (仅已审核通过)
# ============================================================================

@router.get("/list")
async def list_files(request: Request, category: Optional[str] = None):
    """列出已审核通过的文件 (普通用户不可见 pending/rejected)。

    返回每条文件附带 ``uploader`` (上传者用户名) 和 ``purchased`` (当前用户是否已购买)。
    """
    current_user = await _optional_user(request)
    db = await get_db()

    if category:
        if category not in VALID_CATEGORIES:
            raise HTTPException(
                status_code=400,
                detail=f"无效的分类，可选: {', '.join(VALID_CATEGORIES)}",
            )
        rows = await (await db.conn.execute(
            """SELECT uf.*, u.username AS uploader_name
               FROM user_files uf
               LEFT JOIN users u ON uf.user_id = u.user_id
               WHERE uf.status = 'approved' AND uf.category = ?
               ORDER BY uf.created_at DESC""",
            (category,),
        )).fetchall()
    else:
        rows = await (await db.conn.execute(
            """SELECT uf.*, u.username AS uploader_name
               FROM user_files uf
               LEFT JOIN users u ON uf.user_id = u.user_id
               WHERE uf.status = 'approved'
               ORDER BY uf.created_at DESC"""
        )).fetchall()

    # 查询当前用户已购买的文件 ID 集合
    purchased_ids: set[str] = set()
    if current_user:
        pur_rows = await (await db.conn.execute(
            "SELECT file_id FROM shop_orders WHERE user_id = ? AND file_id != '' AND status = 'completed'",
            (current_user["user_id"],),
        )).fetchall()
        purchased_ids = {r["file_id"] for r in pur_rows}

    result = []
    for r in rows:
        d = _file_to_dict(r)
        d["uploader"] = r["uploader_name"] or ""
        d["purchased"] = r["file_id"] in purchased_ids or r["user_id"] == (current_user["user_id"] if current_user else -1)
        result.append(d)

    return {"success": True, "data": result}


# ============================================================================
# 2. 上传文件
# ============================================================================

@router.post("/upload")
async def upload_file(
    request: Request,
    name: str = Form(..., max_length=MAX_NAME_LEN, description="文件名称 (最多18字符)"),
    description: str = Form("", max_length=MAX_DESC_LEN, description="文件描述 (最多60字符)"),
    price: float = Form(0, ge=0, description="价格 (余额，0=免费)"),
    category: str = Form(..., description="分类: plugin/building"),
    file: UploadFile = File(..., description="要上传的文件"),
):
    """上传文件 (流式写入)。

    - 普通用户: 单文件上限 ``user.max_storage`` (默认 512KB)，上传后状态为 ``pending``
    - 管理员: 单文件上限 10MiB，上传后状态为 ``approved``
    - 频率限制: 每用户每小时 1 次上传
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 分类校验
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"无效的分类，可选: {', '.join(VALID_CATEGORIES)}",
        )

    # 名称 / 描述长度校验 (Form 的 max_length 已做基本校验，这里二次确认)
    if not name or len(name) > MAX_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"名称长度需在 1-{MAX_NAME_LEN} 字符之间",
        )
    if len(description) > MAX_DESC_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"描述最多 {MAX_DESC_LEN} 字符",
        )

    # 频率限制: 每用户每小时 1 次上传
    last_upload = await (await db.conn.execute(
        "SELECT created_at FROM user_files WHERE user_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (user["user_id"],),
    )).fetchone()
    if last_upload and (_now() - float(last_upload["created_at"])) < UPLOAD_RATE_LIMIT_SECONDS:
        remaining = int(UPLOAD_RATE_LIMIT_SECONDS - (_now() - float(last_upload["created_at"])))
        raise HTTPException(
            status_code=429,
            detail=f"上传过于频繁，请在 {remaining} 秒后再试 (每小时限 1 次)",
        )

    # 文件名清洗
    original_name = _safe_filename(file.filename or "")

    # 大小上限: 普通用户 -> user.max_storage，管理员 -> 10MiB
    is_admin = user["role"] in ("admin", "superadmin")
    if is_admin:
        max_bytes = ADMIN_MAX_UPLOAD_BYTES
    else:
        # max_storage 默认 524288 (512KB)
        max_bytes = int(user.get("max_storage") or 524288)

    # 生成 file_id 与存储路径
    file_id = _gen_file_id()
    dest = _resolve_stored_path(file_id, original_name)

    # 流式写入并累计大小，超限即拒绝并清理
    written = 0
    try:
        with open(dest, "wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    out.close()
                    dest.unlink(missing_ok=True)
                    limit_kb = max_bytes // 1024
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件过大，单文件上限 {limit_kb} KB",
                    )
                out.write(chunk)
    except HTTPException:
        raise
    except OSError as exc:
        logger.exception(f"上传文件失败 (user={user['user_id']}, file={original_name})")
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"写入失败: {exc}")
    finally:
        await file.close()

    # 状态: 管理员 -> approved，普通用户 -> pending
    file_status = "approved" if is_admin else "pending"

    # 写入数据库
    stored_rel = dest.name  # 仅文件名，存储在 uploads 目录下
    try:
        await db.conn.execute(
            """INSERT INTO user_files
               (file_id, user_id, category, name, description, price,
                file_path, file_size, status, reject_reason, download_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '', 0, ?)""",
            (
                file_id, user["user_id"], category, name, description, price,
                stored_rel, written, file_status, _now(),
            ),
        )
        await db.conn.commit()
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"保存文件记录失败: {exc}")

    logger.info(
        f"用户 {user['username']} 上传文件 {original_name} ({written} bytes, status={file_status})"
    )
    try:
        await db.add_log(
            target_type="user", target_id=user["user_id"],
            level="info",
            message=f"用户 {user['username']} 上传文件: {name} ({category})",
            created_by=user["user_id"],
        )
    except Exception:
        pass

    return {
        "success": True,
        "message": "上传成功" + ("" if is_admin else "，等待管理员审核"),
        "data": {
            "file_id": file_id,
            "name": name,
            "category": category,
            "file_size": written,
            "status": file_status,
        },
    }


# ============================================================================
# 3. 下载文件
# ============================================================================

@router.get("/{file_id}/download")
async def download_file(file_id: str, request: Request):
    """下载文件。

    - 价格 > 0 的文件需先购买 (检查 shop_orders 中是否有该用户的购买记录)
    - 文件所有者与管理员可直接下载
    - 每次成功下载 ``download_count`` +1
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE file_id = ?", (file_id,)
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row["status"] != "approved":
        # 文件所有者可下载自己未审核的文件? 这里禁止，仅允许 approved
        # 但管理员可下载任意文件
        if user["role"] not in ("admin", "superadmin"):
            raise HTTPException(status_code=403, detail="文件未通过审核，暂不可下载")

    is_owner = row["user_id"] == user["user_id"]
    is_admin = user["role"] in ("admin", "superadmin")

    # 付费文件校验
    if not is_owner and not is_admin and float(row["price"]) > 0:
        purchased = await (await db.conn.execute(
            "SELECT 1 FROM shop_orders WHERE user_id = ? AND file_id = ? AND status = 'completed' LIMIT 1",
            (user["user_id"], file_id),
        )).fetchone()
        if not purchased:
            raise HTTPException(status_code=403, detail="此为付费文件，请先购买")

    # 解析实际路径 (路径安全校验)
    root = _uploads_root()
    target = (root / row["file_path"]).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的文件路径")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在于磁盘")

    # 下载计数 +1
    try:
        await db.conn.execute(
            "UPDATE user_files SET download_count = download_count + 1 WHERE file_id = ?",
            (file_id,),
        )
        await db.conn.commit()
    except Exception:
        pass

    # 还原原始文件名 (去掉 file_id_ 前缀)
    display_name = row["file_path"]
    if display_name.startswith(f"{file_id}_"):
        display_name = display_name[len(file_id) + 1:]

    return FileResponse(path=str(target), filename=display_name)


# ============================================================================
# 4. 购买文件
# ============================================================================

@router.post("/{file_id}/purchase")
async def purchase_file(file_id: str, request: Request):
    """购买付费文件。

    扣减余额 -> 创建 shop_order (file_id 关联) -> 完成购买。事务保证原子性。
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE file_id = ?", (file_id,)
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row["status"] != "approved":
        raise HTTPException(status_code=400, detail="文件未通过审核，暂不可购买")

    price = float(row["price"])
    if price <= 0:
        raise HTTPException(status_code=400, detail="免费文件无需购买")

    user_id = user["user_id"]

    # 已购买则直接返回
    already = await (await db.conn.execute(
        "SELECT 1 FROM shop_orders WHERE user_id = ? AND file_id = ? AND status = 'completed' LIMIT 1",
        (user_id, file_id),
    )).fetchone()
    if already:
        return {"success": True, "message": "已购买过该文件", "data": {"file_id": file_id}}

    # 自己的文件无需购买
    if row["user_id"] == user_id:
        return {"success": True, "message": "这是您自己的文件", "data": {"file_id": file_id}}

    # 事务: 扣款 + 增加卖家余额 + 创建订单
    seller_id = row["user_id"]
    try:
        bal_row = await (await db.conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )).fetchone()
        if bal_row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if float(bal_row["balance"]) < price:
            raise HTTPException(status_code=400, detail="余额不足")

        cur = await db.conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (price, user_id, price),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=400, detail="余额不足，扣款失败")

        # 卖家获得对应余额
        await db.conn.execute(
            "UPDATE users SET balance = balance + ? WHERE user_id = ?",
            (price, seller_id),
        )

        order_id = f"o_{uuid.uuid4().hex[:12]}"
        await db.conn.execute(
            """INSERT INTO shop_orders
               (order_id, user_id, product_id, product_name, price,
                status, card_key, file_id, created_at)
               VALUES (?, ?, '', ?, ?, 'completed', '', ?, ?)""",
            (order_id, user_id, row["name"], price, file_id, _now()),
        )
        await db.conn.commit()
    except HTTPException:
        await db.conn.rollback()
        raise
    except Exception as exc:
        await db.conn.rollback()
        raise HTTPException(status_code=500, detail=f"购买失败: {exc}")

    try:
        await db.add_log(
            target_type="user", target_id=user_id,
            level="success",
            message=f"用户 {user['username']} 购买文件: {row['name']} (余额 {price})",
            created_by=user_id,
        )
    except Exception:
        pass

    return {
        "success": True,
        "message": "购买成功",
        "data": {"order_id": order_id, "file_id": file_id, "price": price},
    }


# ============================================================================
# 5. 待审核文件列表 (管理员)
# ============================================================================

@router.get("/pending")
async def list_pending_files(request: Request):
    """管理员查看待审核文件列表。"""
    from .auth import require_admin
    await require_admin(request)
    db = await get_db()

    rows = await (await db.conn.execute(
        """SELECT uf.*, u.username AS uploader_name
           FROM user_files uf
           LEFT JOIN users u ON uf.user_id = u.user_id
           WHERE uf.status = 'pending' ORDER BY uf.created_at DESC"""
    )).fetchall()

    result = []
    for r in rows:
        d = _file_to_dict(r)
        d["uploader"] = r["uploader_name"] or ""
        result.append(d)

    return {"success": True, "data": result}


# ============================================================================
# 6. 审核通过 (管理员)
# ============================================================================

@router.post("/{file_id}/approve")
async def approve_file(file_id: str, request: Request):
    """管理员审核通过文件。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    row = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE file_id = ?", (file_id,)
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row["status"] != "pending":
        raise HTTPException(status_code=400, detail="文件不在待审核状态")

    await db.conn.execute(
        "UPDATE user_files SET status = 'approved', reject_reason = '' WHERE file_id = ?",
        (file_id,),
    )
    await db.conn.commit()

    await db.add_log(
        target_type="system", target_id="user_files",
        level="success",
        message=f"管理员 {admin['username']} 审核通过文件: {row['name']}",
        created_by=admin["user_id"],
    )

    return {"success": True, "message": "文件已审核通过"}


# ============================================================================
# 7. 审核拒绝 (管理员)
# ============================================================================

class RejectRequest:
    """拒绝请求体 (Form 解析)。"""

    pass


@router.post("/{file_id}/reject")
async def reject_file(
    file_id: str,
    request: Request,
    reason: str = Form("", max_length=255, description="拒绝原因"),
):
    """管理员拒绝文件 (附带拒绝原因)。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    row = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE file_id = ?", (file_id,)
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    if row["status"] != "pending":
        raise HTTPException(status_code=400, detail="文件不在待审核状态")

    await db.conn.execute(
        "UPDATE user_files SET status = 'rejected', reject_reason = ? WHERE file_id = ?",
        (reason, file_id),
    )
    await db.conn.commit()

    await db.add_log(
        target_type="system", target_id="user_files",
        level="warn",
        message=f"管理员 {admin['username']} 拒绝文件: {row['name']} (原因: {reason})",
        created_by=admin["user_id"],
    )

    return {"success": True, "message": "文件已拒绝"}


# ============================================================================
# 8. 我的文件 (上传 + 购买)
# ============================================================================

@router.get("/my")
async def my_files(request: Request):
    """列出当前用户上传的文件与已购买的文件。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()
    user_id = user["user_id"]

    # 我上传的文件 (含所有状态)
    uploaded_rows = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )).fetchall()
    uploaded = [_file_to_dict(r) for r in uploaded_rows]

    # 我购买的文件 (shop_orders.file_id 关联 user_files)
    purchased_rows = await (await db.conn.execute(
        """SELECT uf.*
           FROM shop_orders o
           JOIN user_files uf ON o.file_id = uf.file_id
           WHERE o.user_id = ? AND o.file_id != '' AND o.status = 'completed'
           ORDER BY o.created_at DESC""",
        (user_id,),
    )).fetchall()
    purchased = [_file_to_dict(r) for r in purchased_rows]

    return {
        "success": True,
        "data": {
            "uploaded": uploaded,
            "purchased": purchased,
        },
    }


# ============================================================================
# 9. 删除文件
# ============================================================================

@router.delete("/{file_id}")
async def delete_file(file_id: str, request: Request):
    """删除文件 (仅文件所有者或管理员)。

    - 同时删除数据库记录与磁盘文件
    - 磁盘文件删除失败仅记录日志，不阻断数据库记录删除
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await _get_file_or_404(db, file_id)

    # 权限检查: 所有者或管理员
    if not _can_modify(row, user):
        raise HTTPException(status_code=403, detail="无权删除此文件")

    # 路径安全: 解析并校验磁盘文件路径未越出 uploads 目录
    root = _uploads_root()
    target = (root / row["file_path"]).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的文件路径")

    # 删除磁盘文件 (容忍不存在 / 失败仅告警)
    if target.is_file():
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(f"删除磁盘文件失败 (file_id={file_id}, path={target}): {exc}")

    # 删除数据库记录
    await db.conn.execute(
        "DELETE FROM user_files WHERE file_id = ?", (file_id,)
    )
    await db.conn.commit()

    try:
        await db.add_log(
            target_type="user", target_id=user["user_id"],
            level="warn",
            message=f"用户 {user['username']} 删除文件: {row['name']} ({file_id})",
            created_by=user["user_id"],
        )
    except Exception:
        pass

    return {"success": True, "message": "文件已删除"}


# ============================================================================
# 10. 重命名文件
# ============================================================================

class RenameRequest(BaseModel):
    """重命名请求体。"""
    name: str


@router.patch("/{file_id}/rename")
async def rename_file(file_id: str, body: RenameRequest, request: Request):
    """重命名文件 (仅文件所有者或管理员)。

    名称长度限制 1-18 字符。
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await _get_file_or_404(db, file_id)

    if not _can_modify(row, user):
        raise HTTPException(status_code=403, detail="无权重命名此文件")

    # 名称长度校验
    new_name = (body.name or "").strip()
    if not new_name or len(new_name) > MAX_NAME_LEN:
        raise HTTPException(
            status_code=400,
            detail=f"名称长度需在 1-{MAX_NAME_LEN} 字符之间",
        )

    await db.conn.execute(
        "UPDATE user_files SET name = ? WHERE file_id = ?",
        (new_name, file_id),
    )
    await db.conn.commit()

    updated = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE file_id = ?", (file_id,)
    )).fetchone()

    return {"success": True, "message": "重命名成功", "data": _file_to_dict(updated)}


# ============================================================================
# 11. 压缩文件为 zip
# ============================================================================

@router.post("/{file_id}/compress")
async def compress_file(file_id: str, request: Request):
    """将文件压缩为 zip 并返回下载 (FileResponse)。

    - 付费文件需先购买 (所有者/管理员可直接压缩)
    - 压缩产物为临时文件，响应发送后自动清理
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await _get_file_or_404(db, file_id)
    await _assert_download_allowed(row, user, db)

    # 下载计数 +1
    try:
        await db.conn.execute(
            "UPDATE user_files SET download_count = download_count + 1 WHERE file_id = ?",
            (file_id,),
        )
        await db.conn.commit()
    except Exception:
        pass

    return _build_zip_response(row)


# ============================================================================
# 12. 下载压缩版 (zip)
# ============================================================================

@router.get("/{file_id}/download-zip")
async def download_zip(file_id: str, request: Request):
    """下载文件的 zip 压缩版。

    - 付费文件需先购买 (所有者/管理员可直接下载)
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await _get_file_or_404(db, file_id)
    await _assert_download_allowed(row, user, db)

    # 下载计数 +1
    try:
        await db.conn.execute(
            "UPDATE user_files SET download_count = download_count + 1 WHERE file_id = ?",
            (file_id,),
        )
        await db.conn.commit()
    except Exception:
        pass

    return _build_zip_response(row)


# ============================================================================
# 13. 更新文件信息
# ============================================================================

class UpdateFileRequest(BaseModel):
    """更新文件信息请求体 (所有字段可选)。"""
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None


@router.patch("/{file_id}/update")
async def update_file(file_id: str, body: UpdateFileRequest, request: Request):
    """更新文件信息 (仅文件所有者或管理员)。

    可更新字段: name (1-18 字符)、description (最多 60 字符)、price (>=0)。
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await _get_file_or_404(db, file_id)

    if not _can_modify(row, user):
        raise HTTPException(status_code=403, detail="无权更新此文件")

    # 收集待更新字段并逐项校验
    updates: dict = {}
    if body.name is not None:
        name = body.name.strip()
        if not name or len(name) > MAX_NAME_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"名称长度需在 1-{MAX_NAME_LEN} 字符之间",
            )
        updates["name"] = name
    if body.description is not None:
        if len(body.description) > MAX_DESC_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"描述最多 {MAX_DESC_LEN} 字符",
            )
        updates["description"] = body.description
    if body.price is not None:
        if body.price < 0:
            raise HTTPException(status_code=400, detail="价格不能为负数")
        updates["price"] = float(body.price)

    if updates:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [file_id]
        await db.conn.execute(
            f"UPDATE user_files SET {set_clause} WHERE file_id = ?",
            params,
        )
        await db.conn.commit()

    updated = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE file_id = ?", (file_id,)
    )).fetchone()

    return {"success": True, "message": "更新成功", "data": _file_to_dict(updated)}


# ============================================================================
# 内部辅助
# ============================================================================

async def _optional_user(request: Request):
    """尝试解析当前用户，不强制要求登录。"""
    from .auth import get_current_user
    return await get_current_user(request)


def _can_modify(row, user) -> bool:
    """判断当前用户是否有权修改/删除该文件 (所有者或管理员)。"""
    return row["user_id"] == user["user_id"] or user["role"] in ("admin", "superadmin")


async def _get_file_or_404(db, file_id: str):
    """获取文件记录，不存在则抛 404。"""
    row = await (await db.conn.execute(
        "SELECT * FROM user_files WHERE file_id = ?", (file_id,)
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    return row


async def _assert_download_allowed(row, user, db) -> None:
    """校验下载/压缩权限 (与 download_file 逻辑一致)。

    - 非 approved 文件仅管理员可下载
    - 付费文件需购买 (所有者/管理员直接放行)
    """
    if row["status"] != "approved":
        if user["role"] not in ("admin", "superadmin"):
            raise HTTPException(status_code=403, detail="文件未通过审核，暂不可下载")

    is_owner = row["user_id"] == user["user_id"]
    is_admin = user["role"] in ("admin", "superadmin")

    if not is_owner and not is_admin and float(row["price"]) > 0:
        purchased = await (await db.conn.execute(
            "SELECT 1 FROM shop_orders WHERE user_id = ? AND file_id = ? AND status = 'completed' LIMIT 1",
            (user["user_id"], row["file_id"]),
        )).fetchone()
        if not purchased:
            raise HTTPException(status_code=403, detail="此为付费文件，请先购买")


def _build_zip_response(row) -> FileResponse:
    """将文件打包为 zip (临时文件) 并返回 FileResponse。

    - 路径安全: 解析并校验未越出 uploads 目录
    - 临时文件在响应发送后通过 BackgroundTask 自动清理
    """
    root = _uploads_root()
    target = (root / row["file_path"]).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="非法的文件路径")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在于磁盘")

    file_id = row["file_id"]
    # 还原原始文件名 (去掉 file_id_ 前缀)
    display_name = row["file_path"]
    if display_name.startswith(f"{file_id}_"):
        display_name = display_name[len(file_id) + 1:]

    # 写入临时 zip 文件 (uuid 后缀避免并发冲突)
    tmp_dir = DATA_DIR / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    zip_path = tmp_dir / f"{file_id}_{uuid.uuid4().hex[:8]}.zip"
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(target, arcname=display_name)
    except OSError as exc:
        zip_path.unlink(missing_ok=True)
        logger.exception(f"压缩文件失败 (file_id={file_id})")
        raise HTTPException(status_code=500, detail=f"压缩失败: {exc}")

    return FileResponse(
        path=str(zip_path),
        filename=f"{display_name}.zip",
        media_type="application/zip",
        background=BackgroundTask(_cleanup_tmp, zip_path),
    )


def _cleanup_tmp(path) -> None:
    """删除临时文件 (容忍不存在)。"""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["router"]
