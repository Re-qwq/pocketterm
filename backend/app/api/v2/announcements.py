"""公告 API - 公告的创建、删除、评论、点赞/点踩、活动日志。

路由前缀: ``/api/v2/announcements``

权限模型::

    * 创建 / 删除公告          -> 管理员 (``require_admin``)
    * 查看公告列表 / 评论 / 反应 -> 任意已登录用户 (``require_user``)
    * 删除评论                  -> 管理员可删除任意评论; 普通用户仅可删除自己的评论
    * 查看活动日志              -> 管理员

统一响应格式::

    {
        "success": true | false,
        "message": "...",
        "data": ...
    }
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import get_db

router = APIRouter(prefix="/api/v2/announcements", tags=["announcements"])


# ============================================================================
# 请求模型
# ============================================================================

class CreateAnnouncementRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200, description="公告标题")
    content: str = Field(..., min_length=1, description="公告内容")
    pinned: bool = Field(False, description="是否置顶")


class AddCommentRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=2000, description="评论内容")


# ============================================================================
# 公告列表 / 创建 / 删除
# ============================================================================

@router.get("")
async def list_announcements(request: Request):
    """获取所有公告 (任意已登录用户)。

    返回公告列表，每条包含点赞数、点踩数，以及当前用户是否已点赞/点踩。
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    announcements = await db.list_announcements()

    data = []
    for ann in announcements:
        # 当前用户对该公告的反应状态
        user_reaction = await db.get_user_reaction(
            ann["announcement_id"], user["user_id"]
        )
        liked = bool(user_reaction and user_reaction["reaction_type"] == "like")
        disliked = bool(user_reaction and user_reaction["reaction_type"] == "dislike")

        data.append({
            "announcement_id": ann["announcement_id"],
            "title": ann["title"],
            "content": ann["content"],
            "created_by": ann["created_by"],
            "created_by_username": ann["created_by_username"],
            "created_at": ann["created_at"],
            "updated_at": ann["updated_at"],
            "pinned": bool(ann.get("pinned", 0)),
            "like_count": ann.get("like_count", 0),
            "dislike_count": ann.get("dislike_count", 0),
            "liked": liked,
            "disliked": disliked,
        })

    return {"success": True, "data": data}


@router.post("")
async def create_announcement(req: CreateAnnouncementRequest, request: Request):
    """创建公告 (仅管理员)。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    announcement_id = await db.create_announcement(
        title=req.title,
        content=req.content,
        user_id=admin["user_id"],
        username=admin["username"],
        pinned=req.pinned,
    )

    # 记录操作日志
    await db.add_log(
        target_type="system",
        target_id="announcement",
        level="success",
        message=f"管理员 {admin['username']} 创建公告: {req.title}",
        details=json.dumps({"announcement_id": announcement_id}),
        created_by=admin["user_id"],
    )

    return {
        "success": True,
        "message": "公告创建成功",
        "data": {"announcement_id": announcement_id},
    }


@router.delete("/{announcement_id}")
async def delete_announcement(announcement_id: str, request: Request):
    """删除公告 (仅管理员)。同时删除该公告的所有评论与反应。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    ann = await db.get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="公告不存在")

    success = await db.delete_announcement(announcement_id)
    if not success:
        raise HTTPException(status_code=404, detail="公告不存在")

    # 记录操作日志
    await db.add_log(
        target_type="system",
        target_id="announcement",
        level="warn",
        message=f"管理员 {admin['username']} 删除公告: {ann['title']}",
        details=json.dumps({"announcement_id": announcement_id}),
        created_by=admin["user_id"],
    )

    return {"success": True, "message": "公告已删除"}


# ============================================================================
# 置顶 / 取消置顶
# ============================================================================

@router.put("/{announcement_id}/pin")
async def toggle_announcement_pin(announcement_id: str, request: Request):
    """切换公告置顶状态 (仅管理员)。

    若公告当前未置顶则置顶, 已置顶则取消置顶。
    """
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    ann = await db.get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="公告不存在")
    ann = dict(ann)

    current_pinned = bool(ann.get("pinned", 0))
    new_pinned = not current_pinned

    success = await db.set_announcement_pin(announcement_id, new_pinned)
    if not success:
        raise HTTPException(status_code=404, detail="公告不存在")

    await db.add_log(
        target_type="system",
        target_id="announcement",
        level="warn",
        message=(
            f"管理员 {admin['username']} 置顶公告: {ann['title']}"
            if new_pinned
            else f"管理员 {admin['username']} 取消置顶公告: {ann['title']}"
        ),
        details=json.dumps(
            {"announcement_id": announcement_id, "pinned": new_pinned}
        ),
        created_by=admin["user_id"],
    )

    return {
        "success": True,
        "message": "已置顶" if new_pinned else "已取消置顶",
        "data": {
            "announcement_id": announcement_id,
            "pinned": new_pinned,
        },
    }


# ============================================================================
# 评论
# ============================================================================

@router.get("/{announcement_id}/comments")
async def list_comments(announcement_id: str, request: Request):
    """获取公告的评论列表 (任意已登录用户)。"""
    from .auth import require_user
    await require_user(request)
    db = await get_db()
    comments = await db.list_comments(announcement_id)
    return {"success": True, "data": comments}


@router.post("/{announcement_id}/comments")
async def add_comment(
    announcement_id: str, req: AddCommentRequest, request: Request
):
    """添加评论 (任意已登录用户)。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    ann = await db.get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="公告不存在")

    comment_id = await db.add_comment(
        announcement_id=announcement_id,
        user_id=user["user_id"],
        username=user["username"],
        content=req.content,
    )

    return {
        "success": True,
        "message": "评论成功",
        "data": {"comment_id": comment_id},
    }


@router.delete("/{announcement_id}/comments/{comment_id}")
async def delete_comment(
    announcement_id: str, comment_id: str, request: Request
):
    """删除评论。

    管理员可删除任意评论; 普通用户只能删除自己的评论。
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    comment = await db.get_comment(comment_id)
    if comment is None or comment["announcement_id"] != announcement_id:
        raise HTTPException(status_code=404, detail="评论不存在")

    is_admin = user["role"] in ("superadmin", "admin")
    if not is_admin and comment["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权删除他人的评论")

    await db.delete_comment(comment_id)

    return {"success": True, "message": "评论已删除"}


# ============================================================================
# 点赞 / 点踩 / 取消反应
# ============================================================================

@router.post("/{announcement_id}/like")
async def like_announcement(announcement_id: str, request: Request):
    """点赞公告。

    * 已点赞 -> 不操作
    * 已点踩 -> 移除点踩后点赞 (由 set_reaction 的 upsert 原子完成)
    * 未反应 -> 点赞
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    ann = await db.get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="公告不存在")

    existing = await db.get_user_reaction(announcement_id, user["user_id"])
    if not (existing and existing["reaction_type"] == "like"):
        # 未反应或当前为点踩 -> upsert 为 like
        await db.set_reaction(announcement_id, user["user_id"], "like")

    counts = await db.get_reaction_counts(announcement_id)
    return {"success": True, "message": "已点赞", "data": counts}


@router.post("/{announcement_id}/dislike")
async def dislike_announcement(announcement_id: str, request: Request):
    """点踩公告。

    * 已点踩 -> 不操作
    * 已点赞 -> 移除点赞后点踩 (由 set_reaction 的 upsert 原子完成)
    * 未反应 -> 点踩
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    ann = await db.get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="公告不存在")

    existing = await db.get_user_reaction(announcement_id, user["user_id"])
    if not (existing and existing["reaction_type"] == "dislike"):
        # 未反应或当前为点赞 -> upsert 为 dislike
        await db.set_reaction(announcement_id, user["user_id"], "dislike")

    counts = await db.get_reaction_counts(announcement_id)
    return {"success": True, "message": "已点踩", "data": counts}


@router.delete("/{announcement_id}/reaction")
async def cancel_reaction(announcement_id: str, request: Request):
    """取消当前用户对该公告的反应 (点赞或点踩)。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    ann = await db.get_announcement(announcement_id)
    if ann is None:
        raise HTTPException(status_code=404, detail="公告不存在")

    await db.remove_reaction(announcement_id, user["user_id"])

    counts = await db.get_reaction_counts(announcement_id)
    return {"success": True, "message": "已取消反应", "data": counts}


# ============================================================================
# 活动日志 (管理员)
# ============================================================================

@router.get("/logs")
async def announcement_logs(request: Request):
    """获取公告活动日志 (仅管理员)。

    返回谁对哪条公告点赞/点踩/评论，以及对应的时间戳。
    """
    from .auth import require_admin
    await require_admin(request)
    db = await get_db()

    logs = await db.list_announcement_logs()
    return {"success": True, "data": logs}
