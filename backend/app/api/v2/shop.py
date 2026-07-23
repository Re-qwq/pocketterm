"""商店 API - 商品浏览、余额、购买、订单管理。

路由前缀: ``/api/v2/shop``

商品分类:

    - ``panel_card``     面板卡密 (card-type, 生成真实卡密)
    - ``register_card`` 注册卡密 (card-type, 生成真实卡密)
    - ``plugin_file``    插件文件 (file-type, 创建文件下载记录)
    - ``building_file``  建筑文件 (file-type, 创建文件下载记录)

购买卡密类商品时，卡密在后端生成并写入 ``card_keys`` 表，
随后在 ``shop_orders`` 中记录该卡密。所有金额操作均使用事务保证原子性。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import get_db
from app.database.storage import _generate_card_key

router = APIRouter(prefix="/api/v2/shop", tags=["shop"])


# ============================================================================
# 常量
# ============================================================================

#: 合法的商品分类
VALID_CATEGORIES = ("panel_card", "register_card", "plugin_file", "building_file")

#: 卡密类商品分类 (购买时需生成真实卡密)
CARD_CATEGORIES = ("panel_card", "register_card")

#: 文件类商品分类 (购买时创建文件下载记录)
FILE_CATEGORIES = ("plugin_file", "building_file")


# ============================================================================
# 请求模型
# ============================================================================

class PurchaseRequest(BaseModel):
    product_id: str = Field(..., description="商品 ID")


class SetBalanceRequest(BaseModel):
    balance: float = Field(..., description="新的余额")


class CreateProductRequest(BaseModel):
    category: str = Field(..., description="商品分类: panel_card/register_card/plugin_file/building_file")
    name: str = Field(..., max_length=64, description="商品名称")
    description: str = Field("", max_length=255, description="商品描述")
    price: float = Field(..., ge=0, description="价格 (余额)")
    duration_days: Optional[float] = Field(None, description="时长(天), None=永久 (卡密类商品)")
    card_type: str = Field("", description="卡密类型: panel/register (卡密类商品)")
    file_path: str = Field("", description="关联文件 ID (文件类商品)")


# ============================================================================
# 辅助函数
# ============================================================================

def _now() -> float:
    return time.time()


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _product_to_dict(p: Any) -> dict:
    return {
        "product_id": p["product_id"],
        "category": p["category"],
        "name": p["name"],
        "description": p["description"],
        "price": p["price"],
        "duration_days": p["duration_days"],
        "card_type": p["card_type"],
        "file_path": p["file_path"],
        "status": p["status"],
        "created_by": p["created_by"],
        "created_at": p["created_at"],
    }


def _order_to_dict(o: Any, username: Optional[str] = None) -> dict:
    data = {
        "order_id": o["order_id"],
        "user_id": o["user_id"],
        "product_id": o["product_id"],
        "product_name": o["product_name"],
        "price": o["price"],
        "status": o["status"],
        "card_key": o["card_key"],
        "file_id": o["file_id"],
        "created_at": o["created_at"],
    }
    if username is not None:
        data["username"] = username
    return data


# ============================================================================
# 1. 商品列表 (按分类分组)
# ============================================================================

@router.get("/products")
async def list_products(request: Request, category: Optional[str] = None):
    """列出所有上架商品 (按分类分组)。

    Query 参数 ``category`` 可选，用于只返回某一分类的商品。
    """
    await _optional_user(request)  # 商品列表允许匿名浏览，但记录调用者
    db = await get_db()

    if category:
        if category not in VALID_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"无效的商品分类，可选: {', '.join(VALID_CATEGORIES)}")
        rows = await (await db.conn.execute(
            "SELECT * FROM shop_products WHERE status = 'active' AND category = ? "
            "ORDER BY price ASC, created_at DESC",
            (category,),
        )).fetchall()
    else:
        rows = await (await db.conn.execute(
            "SELECT * FROM shop_products WHERE status = 'active' "
            "ORDER BY category ASC, price ASC, created_at DESC"
        )).fetchall()

    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["category"], []).append(_product_to_dict(r))

    return {"success": True, "data": grouped}


# ============================================================================
# 2. 余额查询
# ============================================================================

@router.get("/balance")
async def get_balance(request: Request):
    """获取当前用户余额。"""
    from .auth import require_user
    user = await require_user(request)
    return {"success": True, "data": {"balance": user.get("balance", 0) if isinstance(user, dict) else user["balance"]}}


# ============================================================================
# 3. 购买商品
# ============================================================================

@router.post("/purchase")
async def purchase_product(req: PurchaseRequest, request: Request):
    """购买商品。

    卡密类商品: 在后端生成真实卡密并写入 card_keys 表，订单记录该卡密。
    文件类商品: 创建文件下载记录 (订单 file_id 关联商品文件)。

    所有金额操作在单个事务内完成，失败则回滚。
    """
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 查找商品
    product = await (await db.conn.execute(
        "SELECT * FROM shop_products WHERE product_id = ?", (req.product_id,)
    )).fetchone()
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    if product["status"] != "active":
        raise HTTPException(status_code=400, detail="商品已下架")

    price = float(product["price"])
    user_id = user["user_id"]
    cat = product["category"]

    # 校验商品类型与必要字段
    if cat in CARD_CATEGORIES:
        if not product["card_type"]:
            raise HTTPException(status_code=400, detail="卡密类商品缺少 card_type 配置")
    elif cat in FILE_CATEGORIES:
        if not product["file_path"]:
            raise HTTPException(status_code=400, detail="文件类商品缺少关联文件")
    else:
        raise HTTPException(status_code=400, detail="未知的商品分类")

    # 事务: 余额校验 -> 扣款 -> 生成卡密/文件记录 -> 创建订单
    try:
        # 1. 余额校验 (事务内重新读取以避免竞态)
        bal_row = await (await db.conn.execute(
            "SELECT balance FROM users WHERE user_id = ?", (user_id,)
        )).fetchone()
        if bal_row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        current_balance = float(bal_row["balance"])
        if current_balance < price:
            raise HTTPException(status_code=400, detail="余额不足")

        # 2. 扣减余额 (条件更新，确保余额充足时才扣款)
        cur = await db.conn.execute(
            "UPDATE users SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (price, user_id, price),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=400, detail="余额不足，扣款失败")

        card_key = ""
        file_id = ""

        if cat in CARD_CATEGORIES:
            # 3a. 生成真实卡密 (确保唯一)
            key = _generate_card_key()
            while await db.get_card_by_key(key) is not None:
                key = _generate_card_key()
            card_id = _gen_id("c")
            await db.conn.execute(
                """INSERT INTO card_keys
                   (card_id, key, key_type, status, duration_days,
                    bound_user_id, created_by, created_at, expires_at)
                   VALUES (?, ?, ?, 'unused', ?, ?, ?, ?, ?)""",
                (
                    card_id, key, product["card_type"],
                    product["duration_days"], user_id,
                    user_id, _now(), None,
                ),
            )
            card_key = key
        else:
            # 3b. 文件类商品: 记录关联文件 ID，供后续下载
            file_id = product["file_path"]

        # 4. 创建订单
        order_id = _gen_id("o")
        await db.conn.execute(
            """INSERT INTO shop_orders
               (order_id, user_id, product_id, product_name, price,
                status, card_key, file_id, created_at)
               VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)""",
            (
                order_id, user_id, product["product_id"], product["name"],
                price, card_key, file_id, _now(),
            ),
        )

        await db.conn.commit()
    except HTTPException:
        # 回滚事务，避免扣款后未生成订单等不一致状态
        await db.conn.rollback()
        raise
    except Exception as exc:
        await db.conn.rollback()
        raise HTTPException(status_code=500, detail=f"购买失败: {exc}")

    # 记录日志
    try:
        await db.add_log(
            target_type="user", target_id=user_id,
            level="success",
            message=f"用户 {user['username']} 购买商品: {product['name']} (余额 {price})",
            created_by=user_id,
        )
    except Exception:
        pass

    return {
        "success": True,
        "message": "购买成功",
        "data": {
            "order_id": order_id,
            "product_id": product["product_id"],
            "product_name": product["name"],
            "price": price,
            "card_key": card_key,
            "file_id": file_id,
        },
    }


# ============================================================================
# 4. 订单列表
# ============================================================================

@router.get("/orders")
async def list_my_orders(request: Request):
    """列出当前用户的订单。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    rows = await (await db.conn.execute(
        "SELECT * FROM shop_orders WHERE user_id = ? ORDER BY created_at DESC",
        (user["user_id"],),
    )).fetchall()

    return {"success": True, "data": [_order_to_dict(r) for r in rows]}


# ============================================================================
# 5. 订单搜索 (管理员) -- 必须在 /orders/{order_id} 之前注册
# ============================================================================

@router.get("/orders/search")
async def search_orders(request: Request, q: str = ""):
    """管理员搜索订单 (按 order_id 或用户名)。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    keyword = f"%{q}%" if q else "%"
    rows = await (await db.conn.execute(
        """SELECT o.*, u.username
           FROM shop_orders o
           LEFT JOIN users u ON o.user_id = u.user_id
           WHERE o.order_id LIKE ? OR u.username LIKE ?
           ORDER BY o.created_at DESC
           LIMIT 200""",
        (keyword, keyword),
    )).fetchall()

    return {
        "success": True,
        "data": [_order_to_dict(r, username=r["username"]) for r in rows],
    }


# ============================================================================
# 6. 订单详情
# ============================================================================

@router.get("/orders/{order_id}")
async def get_order(order_id: str, request: Request):
    """获取订单详情。普通用户只能查看自己的订单。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    row = await (await db.conn.execute(
        "SELECT o.*, u.username FROM shop_orders o "
        "LEFT JOIN users u ON o.user_id = u.user_id "
        "WHERE o.order_id = ?",
        (order_id,),
    )).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 普通用户只能查看自己的订单
    if user["role"] == "user" and row["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权查看此订单")

    return {
        "success": True,
        "data": _order_to_dict(row, username=row["username"]),
    }


# ============================================================================
# 7. 管理员: 所有订单 (含用户信息)
# ============================================================================

@router.get("/admin/orders")
async def list_all_orders(request: Request, limit: int = 200):
    """管理员查看所有订单 (含用户信息)。"""
    from .auth import require_admin
    await require_admin(request)
    db = await get_db()

    rows = await (await db.conn.execute(
        """SELECT o.*, u.username
           FROM shop_orders o
           LEFT JOIN users u ON o.user_id = u.user_id
           ORDER BY o.created_at DESC
           LIMIT ?""",
        (limit,),
    )).fetchall()

    return {
        "success": True,
        "data": [_order_to_dict(r, username=r["username"]) for r in rows],
    }


# ============================================================================
# 8. 管理员: 设置用户余额
# ============================================================================

@router.post("/balance/{user_id}")
async def set_user_balance(user_id: str, req: SetBalanceRequest, request: Request):
    """管理员设置某用户的余额。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    target = await db.get_user_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    await db.conn.execute(
        "UPDATE users SET balance = ? WHERE user_id = ?",
        (req.balance, user_id),
    )
    await db.conn.commit()

    await db.add_log(
        target_type="user", target_id=user_id,
        level="warn",
        message=f"管理员 {admin['username']} 设置用户 {target['username']} 余额为 {req.balance}",
        created_by=admin["user_id"],
    )

    return {"success": True, "message": "余额已更新"}


# ============================================================================
# 9. 管理员: 创建商品
# ============================================================================

@router.post("/products")
async def create_product(req: CreateProductRequest, request: Request):
    """管理员创建商品。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    if req.category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"无效的商品分类，可选: {', '.join(VALID_CATEGORIES)}",
        )

    # 卡密类商品必须提供 card_type
    if req.category in CARD_CATEGORIES:
        if req.card_type not in ("panel", "register"):
            raise HTTPException(status_code=400, detail="卡密类商品 card_type 必须为 panel 或 register")

    product_id = _gen_id("p")
    await db.conn.execute(
        """INSERT INTO shop_products
           (product_id, category, name, description, price, duration_days,
            card_type, file_path, status, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            product_id, req.category, req.name, req.description,
            req.price, req.duration_days, req.card_type, req.file_path,
            admin["user_id"], _now(),
        ),
    )
    await db.conn.commit()

    await db.add_log(
        target_type="system", target_id="shop_product",
        level="success",
        message=f"管理员 {admin['username']} 创建商品: {req.name} ({req.category})",
        created_by=admin["user_id"],
    )

    return {"success": True, "data": {"product_id": product_id}}


# ============================================================================
# 10. 管理员: 下架商品
# ============================================================================

@router.delete("/products/{product_id}")
async def deactivate_product(product_id: str, request: Request):
    """管理员下架商品 (软删除，标记为 inactive)。"""
    from .auth import require_admin
    admin = await require_admin(request)
    db = await get_db()

    product = await (await db.conn.execute(
        "SELECT * FROM shop_products WHERE product_id = ?", (product_id,)
    )).fetchone()
    if product is None:
        raise HTTPException(status_code=404, detail="商品不存在")

    await db.conn.execute(
        "UPDATE shop_products SET status = 'inactive' WHERE product_id = ?",
        (product_id,),
    )
    await db.conn.commit()

    await db.add_log(
        target_type="system", target_id="shop_product",
        level="warn",
        message=f"管理员 {admin['username']} 下架商品: {product['name']}",
        created_by=admin["user_id"],
    )

    return {"success": True, "message": "商品已下架"}


# ============================================================================
# 内部辅助: 商品列表允许匿名访问，但仍尝试解析用户
# ============================================================================

async def _optional_user(request: Request):
    """尝试解析当前用户，不强制要求登录。"""
    from .auth import get_current_user
    return await get_current_user(request)


__all__ = ["router"]
