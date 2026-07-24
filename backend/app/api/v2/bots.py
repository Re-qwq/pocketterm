"""机器人实例管理 API - 创建、启动、停止、删除。"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.database import get_db

logger = logging.getLogger("pocketterm.bots")

router = APIRouter(prefix="/api/v2/bots", tags=["bots"])


# ============================================================================
# 请求模型
# ============================================================================

class CreateBotRequest(BaseModel):
    panel_id: str = Field(..., max_length=64, description="所属面板 ID")
    name: str = Field(..., min_length=1, max_length=50, description="实例名称")
    account_id: str = Field("", max_length=64, description="关联的游戏账号 ID")
    server_code: str = Field("", max_length=64, description="租赁服编号")
    server_type: str = Field("rental", max_length=32, description="服务器类型: rental/private")
    access_point_type: str = Field("neomega", max_length=32, description="接入点类型")
    platform_type: str = Field("pc", max_length=8, description="客户端平台: pc/pe (PE=手机端)")
    config: dict = Field(default_factory=dict, description="额外配置")


class UpdateBotConfigRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=50, description="实例名称")
    account_id: Optional[str] = Field(None, max_length=64, description="关联的游戏账号 ID")
    server_code: Optional[str] = Field(None, max_length=64, description="租赁服编号")
    server_type: Optional[str] = Field(None, max_length=32, description="服务器类型")
    access_point_type: Optional[str] = Field(None, max_length=32, description="接入点类型")
    platform_type: Optional[str] = Field(None, max_length=8, description="客户端平台: pc/pe")
    game_version: Optional[str] = Field(None, max_length=32, description="游戏版本")
    config: Optional[dict] = None


# ============================================================================
# 权限辅助
# ============================================================================

async def _check_panel_access(panel_id: str, user: dict, db) -> None:
    """检查用户对面板的访问权限。"""
    panel = await db.get_panel(panel_id)
    if panel is None:
        raise HTTPException(status_code=404, detail="面板不存在")

    if user["role"] == "user" and panel["user_id"] != user["user_id"]:
        raise HTTPException(status_code=403, detail="无权操作此面板")

    # 超级管理员面板保护：面板所有者是 superadmin 时，仅允许
    # 超级管理员或面板所有者本人操作，其他用户一律拒绝
    owner = await db.get_user_by_id(panel["user_id"])
    if (
        owner is not None
        and owner["role"] == "superadmin"
        and user["role"] != "superadmin"
        and panel["user_id"] != user["user_id"]
    ):
        raise HTTPException(status_code=403, detail="无权操作超级管理员的面板")

    # 检查面板是否过期
    now = time.time()
    if panel["expire_at"] is not None and panel["expire_at"] < now:
        if panel["status"] == "active":
            await db.update_panel_status(panel_id, "expired")
        raise HTTPException(status_code=403, detail="面板已到期，请续费后继续使用")


async def _get_bot_with_access(bot_id: str, user: dict, db):
    """获取机器人并验证访问权限。"""
    bot = await db.get_bot(bot_id)
    if bot is None:
        raise HTTPException(status_code=404, detail="机器人实例不存在")

    # 验证面板权限
    await _check_panel_access(bot["panel_id"], user, db)
    return bot


async def _build_bot_config(bot, db):
    """从数据库机器人记录构建 BotConfig 对象。

    合并数据库中的 config JSON 与结构化字段 (name / account_id 等),
    若关联了游戏账号则额外从账号库读取 ``sauth_json``。
    """
    from app.bot.models import BotConfig, ServerType, AccessPointType

    bot_config_data = json.loads(bot["config"]) if bot["config"] else {}
    bot_config_data.update({
        "name": bot["name"],
        "account_id": bot["account_id"],
        "server_code": bot["server_code"],
        "server_type": bot["server_type"],
        "access_point_type": bot["access_point_type"],
    })
    # 如果有 account_id, 从账号库获取 sauth_json
    sauth_updated_at = 0.0
    account_metadata: dict = {}
    if bot["account_id"]:
        account = await db.get_account(bot["account_id"])
        if account:
            try:
                account_metadata = json.loads(account["metadata"]) if isinstance(account["metadata"], str) else account["metadata"]
                if isinstance(account_metadata, dict):
                    bot_config_data["sauth_json"] = account_metadata.get("sauth_json", "")
                    sauth_updated_at = account_metadata.get("sauth_updated_at", 0.0) or 0.0
            except (json.JSONDecodeError, KeyError, TypeError):
                account_metadata = {}
                bot_config_data["sauth_json"] = bot_config_data.get("sauth_json", "")

    # 回退: 若账号未提供时间戳, 使用 bot config 中的时间戳
    if not sauth_updated_at:
        sauth_updated_at = bot_config_data.get("sauth_updated_at", 0.0) or 0.0

    # 自动刷新: 若 sauth_json 缺失或已过期 (> 2 小时),
    # 通过 SauthRefresher 获取新鲜凭证
    current_sauth = bot_config_data.get("sauth_json", "")
    need_refresh = bool(
        not current_sauth
        or (sauth_updated_at and (time.time() - sauth_updated_at) > 2 * 3600)
    )
    if need_refresh:
        try:
            from app.auth.sauth_refresh import sauth_refresher
            fresh_sauth = await sauth_refresher.get_fresh_sauth()
            if fresh_sauth:
                bot_config_data["sauth_json"] = fresh_sauth
                logger.info(
                    f"机器人 {bot['name']} sauth_json 已通过 "
                    f"SauthRefresher 自动刷新"
                )
                # 持久化回账号 metadata (附带时间戳), 便于下次判断
                if bot["account_id"]:
                    try:
                        new_meta = dict(account_metadata) if isinstance(account_metadata, dict) else {}
                        new_meta["sauth_json"] = fresh_sauth
                        new_meta["sauth_updated_at"] = time.time()
                        await db.conn.execute(
                            "UPDATE accounts SET metadata = ? WHERE account_id = ?",
                            (json.dumps(new_meta, ensure_ascii=False), bot["account_id"]),
                        )
                        await db.conn.commit()
                    except Exception:
                        logger.debug("回写刷新后的 sauth_json 到账号 metadata 失败", exc_info=True)
            else:
                logger.warning(
                    f"机器人 {bot['name']} sauth_json 已过期或缺失, 但自动刷新失败"
                    f" (无可用 4399 账号或登录失败)"
                )
        except Exception as e:
            logger.warning(f"自动刷新 sauth_json 异常: {e}")

    # 如果 bot config 中没有 api_key, 从 NV1 管理器注入
    if not bot_config_data.get("api_key"):
        try:
            from app.auth.nv1_manager import nv1_manager
            nv1_key = nv1_manager.get_key()
            if nv1_key:
                bot_config_data["api_key"] = nv1_key
                logger.info(f"已从 NV1 管理器注入 API Key (前8位: {nv1_key[:8]}...)")
            # 同时注入正确的认证服务器 URL
            auth_server = nv1_manager.get_auth_server()
            if auth_server:
                bot_config_data["auth_server"] = auth_server
                logger.info(f"已从 NV1 管理器注入认证服务器: {auth_server}")
        except Exception as e:
            logger.warning(f"获取 NV1 API Key 失败: {e}")

    _sauth = bot_config_data.get("sauth_json", "")
    _svcode = bot_config_data.get("server_code", "")
    _aptype = bot_config_data.get("access_point_type", "?")
    _ptype = bot_config_data.get("platform_type", "pc")

    # PE 端转换: 如果 platform_type == "pe", 将 sauth_json 转换为 PE 格式
    # PE: platform=android, sdk_version=5.2.0, source_platform=android
    if _ptype == "pe" and _sauth:
        try:
            _outer = json.loads(_sauth)
            _inner_str = _outer.get("sauth_json", "")
            if not _inner_str:
                _inner_str = _sauth
                _outer = {"sauth_json": _sauth}
            _inner = json.loads(_inner_str)
            _orig_platform = _inner.get("platform", "?")
            # 只在 PC 格式时转换 (避免重复转换)
            if _orig_platform.lower() in ("pc", "ad"):
                _inner["platform"] = "android"
                _inner["sdk_version"] = "5.2.0"
                _inner["source_platform"] = "android"
                _new_inner_str = json.dumps(_inner, ensure_ascii=False, separators=(",", ":"))
                _new_outer = {"sauth_json": _new_inner_str}
                bot_config_data["sauth_json"] = json.dumps(_new_outer, ensure_ascii=False, separators=(",", ":"))
                logger.info(
                    f"PE 端 sauth_json 转换完成: "
                    f"platform { _orig_platform}→android, sdk_version→5.2.0"
                )
        except Exception as e:
            logger.warning(f"PE 端 sauth_json 转换失败: {e}")

    logger.debug(f"_build_bot_config 完成: server_code={_svcode!r}, ap_type={_aptype!r}, platform={_ptype!r}, sauth_json长度={len(_sauth) if _sauth else 0}, auth_method={bot_config_data.get('auth_method','?')}")

    return BotConfig(
        name=bot_config_data.get("name", ""),
        server_code=bot_config_data.get("server_code", ""),
        server_password=bot_config_data.get("server_password", ""),
        server_type=ServerType(bot_config_data.get("server_type", "rental")),
        server_address=bot_config_data.get("server_address", ""),
        server_port=bot_config_data.get("server_port", 19132),
        auth_server=bot_config_data.get("auth_server", "https://nv1.nethard.pro"),
        api_key=bot_config_data.get("api_key", ""),
        sauth_json=bot_config_data.get("sauth_json", ""),
        cookie=bot_config_data.get("cookie", ""),
        auth_method=bot_config_data.get("auth_method", "auto"),
        device_model=bot_config_data.get("device_model", "Xiaomi 13"),
        access_point_type=AccessPointType(bot_config_data.get("access_point_type", "neomega")),
        auto_reconnect=bot_config_data.get("auto_reconnect", True),
        max_reconnect_attempts=bot_config_data.get("max_reconnect_attempts", 3),
        reconnect_delay=bot_config_data.get("reconnect_delay", 30),
        account_id=bot_config_data.get("account_id", ""),
        fb_token=bot_config_data.get("fb_token", ""),
        username=bot_config_data.get("username", ""),
        password=bot_config_data.get("password", ""),
    )


async def _ensure_bot_in_memory(bot_id: str, config, bot_manager) -> None:
    """确保机器人实例存在于 bot_manager 内存中, 且 bot_id 与数据库一致。

    如果内存中已有实例但处于错误/僵尸状态 (_running=True 但 DB 状态为 error),
    则先停止旧实例再重新创建, 确保 start() 可以正常启动。
    """
    if bot_id in bot_manager._bots:
        existing = bot_manager._bots[bot_id]
        # 如果旧实例的 _running 为 True, 说明 _run_loop 仍在运行 (可能在重连等待中)
        # 此时 start() 会返回 False, 所以必须先停止旧实例
        if getattr(existing, '_running', False):
            try:
                await bot_manager.stop_bot(bot_id)
            except Exception:
                pass
            bot_manager._bots.pop(bot_id, None)
        else:
            return
    new_bot = await bot_manager.create_bot(config)
    # 用数据库 bot_id 覆盖 create_bot 自动生成的 bot_id, 并重建索引
    bot_manager._bots.pop(new_bot.bot_id, None)
    new_bot.info.bot_id = bot_id
    bot_manager._bots[bot_id] = new_bot


# ============================================================================
# 创建机器人实例
# ============================================================================

@router.post("")
async def create_bot(req: CreateBotRequest, request: Request):
    """创建机器人实例。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 验证面板权限
    await _check_panel_access(req.panel_id, user, db)

    bot_id = await db.create_bot_instance(
        panel_id=req.panel_id,
        name=req.name,
        account_id=req.account_id,
        server_code=req.server_code,
        server_type=req.server_type,
        access_point_type=req.access_point_type,
        config=json.dumps({**req.config, "platform_type": req.platform_type}, ensure_ascii=False),
    )

    await db.add_log(
        target_type="bot", target_id=bot_id,
        level="success",
        message=f"创建机器人实例: {req.name}",
        created_by=user["user_id"],
    )

    return {
        "success": True,
        "data": {"bot_id": bot_id, "name": req.name},
    }


# ============================================================================
# 查询机器人
# ============================================================================

@router.get("")
async def list_bots(
    request: Request,
    panel_id: Optional[str] = None,
):
    """列出机器人实例。用户看自己面板的, 管理员看所有。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    if panel_id:
        await _check_panel_access(panel_id, user, db)
        bots = await db.list_bots_by_panel(panel_id)
    elif user["role"] in ("superadmin", "admin"):
        bots = await db.list_all_bots()
    else:
        # 普通用户: 获取自己的所有面板, 然后列出对应机器人
        panels = await db.list_panels_by_user(user["user_id"])
        bots = []
        for p in panels:
            panel_bots = await db.list_bots_by_panel(p["panel_id"])
            bots.extend(panel_bots)

    return {
        "success": True,
        "data": [
            {
                "bot_id": b["bot_id"],
                "panel_id": b["panel_id"],
                "name": b["name"],
                "account_id": b["account_id"],
                "server_code": b["server_code"],
                "server_type": b["server_type"],
                "access_point_type": b["access_point_type"],
                "platform_type": (json.loads(b["config"]) if b["config"] else {}).get("platform_type", "pc"),
                "status": b["status"],
                "created_at": b["created_at"],
                "last_started_at": b["last_started_at"],
                "config": json.loads(b["config"]) if b["config"] else {},
            }
            for b in bots
        ],
    }


# ============================================================================
# 账号列表 (Cookie 池)
# ============================================================================

@router.get("/accounts")
async def list_accounts(request: Request):
    """获取可用的游戏账号列表（Cookie池）。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 查询所有活跃状态的账号
    rows = await (await db.conn.execute(
        "SELECT account_id, username, player_name, status, last_used_at "
        "FROM accounts WHERE status = 'active' "
        "ORDER BY last_used_at IS NULL, last_used_at DESC"
    )).fetchall()

    # 如果没有 accounts 表数据，返回空列表
    accounts = []
    for row in rows:
        accounts.append({
            "account_id": row["account_id"] if hasattr(row, "keys") else row[0],
            "username": row["username"] if hasattr(row, "keys") else row[1],
            "player_name": row["player_name"] if hasattr(row, "keys") else row[2],
            "status": row["status"] if hasattr(row, "keys") else row[3],
        })

    return {"success": True, "data": accounts}


# ============================================================================
# 从 Cookie 池 / 新 4399 账号创建机器人
# ============================================================================

class CreateBotFromPoolRequest(BaseModel):
    name: str = "Bot"  # 机器人名称，默认为 Bot
    account_source: str = "pool"  # pool / new / manual
    username_4399: str = ""
    password_4399: str = ""
    captcha_answer: str = ""
    cookie: str = ""
    sauth_json: str = ""
    server_code: str = ""


@router.post("/create")
async def create_bot_from_pool(req: CreateBotFromPoolRequest, request: Request):
    """从Cookie池或新4399账号创建机器人。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    # 检查用户机器人数量限制
    user_panels = await db.list_panels_by_user(user["user_id"])
    user_bots = []
    for p in user_panels:
        bots = await db.list_bots_by_panel(p["panel_id"])
        user_bots.extend(bots)

    if user["role"] == "user" and len(user_bots) >= 1:
        raise HTTPException(status_code=403, detail="普通用户最多创建1个机器人")

    # 如果 name 为默认值，加上时间戳让名字唯一
    bot_name = req.name
    if bot_name == "Bot":
        bot_name = f"PT_{int(time.time()) % 100000}"

    # 获取或创建账号
    account_id = ""
    new_account_sauth = ""  # 自动注册的 sauth_json (如果有)
    pool_account_sauth = ""  # 从 Cookie 池获取的 sauth_json (如果有)
    if req.account_source == "pool":
        # 从Cookie池获取一个未使用的活跃账号 (优先选择有 sauth_json 的)
        row = await (await db.conn.execute(
            "SELECT account_id, username, metadata FROM accounts WHERE status = 'active' "
            "AND metadata LIKE '%sauth_json%' "
            "ORDER BY last_used_at IS NULL DESC, last_used_at ASC LIMIT 1"
        )).fetchone()
        if row:
            account_id = row["account_id"] if hasattr(row, "keys") else row[0]
            # 提取 metadata 中的 sauth_json
            meta_str = row["metadata"] if hasattr(row, "keys") else row[2]
            if meta_str:
                try:
                    meta = json.loads(meta_str) if isinstance(meta_str, str) else meta_str
                    if isinstance(meta, dict) and meta.get("sauth_json"):
                        pool_account_sauth = meta["sauth_json"]
                except Exception:
                    pass
            # 标记账号为已使用
            await db.conn.execute(
                "UPDATE accounts SET last_used_at = ? WHERE account_id = ?",
                (time.time(), account_id)
            )
            await db.conn.commit()

        # Cookie池中没有可用的 sauth_json (无账号或提取失败),
        # 尝试通过 SauthRefresher 自动获取新鲜凭证
        if not pool_account_sauth:
            try:
                from app.auth.sauth_refresh import sauth_refresher
                fresh_sauth = await sauth_refresher.get_fresh_sauth()
                if fresh_sauth:
                    pool_account_sauth = fresh_sauth
                    logger.info(
                        "Cookie池无可用 sauth_json, 已通过 SauthRefresher 自动获取"
                    )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Cookie池中没有可用账号, 且自动刷新 sauth_json 失败"
                        " (无可用 4399 账号或登录均失败)",
                    )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=500, detail=f"自动刷新 sauth_json 异常: {e}",
                )
    elif req.account_source == "new":
        # 自动注册新4399账号 (使用 AccountRegistrar 自动完成注册+验证码OCR+登录)
        try:
            from app.auth.netease_direct.account_register import AccountRegistrar, CaptchaHandler
            # 使用 OCR 自动识别验证码, 避免人工输入
            captcha_handler = CaptchaHandler(use_ocr=True)
            async with AccountRegistrar(captcha_handler=captcha_handler) as reg:
                result = await reg.register_and_get_sauth()
            if not result.get("success"):
                raise HTTPException(
                    status_code=400,
                    detail=f"4399账号自动注册失败: {result.get('message', '未知错误')}",
                )
            # 保存账号到数据库
            import uuid as _uuid
            account_id = f"a_{_uuid.uuid4().hex[:12]}"
            sauth_json = result.get("sauth_json", "")
            new_account_sauth = sauth_json
            metadata = json.dumps({"sauth_json": sauth_json}, ensure_ascii=False) if sauth_json else None
            await db.conn.execute(
                "INSERT INTO accounts (account_id, username, password, player_name, status, created_at, metadata) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (
                    account_id,
                    result.get("username", f"auto_{account_id[:8]}"),
                    result.get("password", ""),
                    bot_name,
                    time.time(),
                    metadata,
                )
            )
            await db.conn.commit()
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"4399账号自动注册异常: {str(e)}")

    elif req.account_source == "manual":
        # 手动输入凭证 (cookie/sauth_json 或 4399 账号密码)
        import uuid as _uuid
        account_id = f"a_{_uuid.uuid4().hex[:12]}"

        # 构建账号元数据
        metadata = {}
        if req.cookie:
            metadata["sauth_json"] = req.cookie
        if req.sauth_json:
            metadata["sauth_json"] = req.sauth_json

        # 保存账号到数据库
        display_name = req.username_4399 or bot_name
        await db.conn.execute(
            "INSERT INTO accounts (account_id, username, password, player_name, status, created_at, metadata) "
            "VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (
                account_id,
                req.username_4399 or f"manual_{account_id[:8]}",
                req.password_4399 or "",
                display_name,
                time.time(),
                json.dumps(metadata, ensure_ascii=False),
            )
        )
        await db.conn.commit()

    # 获取用户的第一个面板，如果没有则报错 (不允许无卡密创建面板)
    if user_panels:
        panel_id = user_panels[0]["panel_id"]
        # 检查面板权限和过期状态
        await _check_panel_access(panel_id, user, db)
    else:
        # 用户没有面板 - 不允许自动创建 (需要卡密)
        raise HTTPException(
            status_code=403,
            detail="您还没有面板, 请先使用卡密激活面板后再创建机器人",
        )

    # 创建机器人 - 使用默认配置，用户可在面板设置中修改
    config = {
        "server_type": "rental",
        "server_code": req.server_code or "",
        "game_version": "1.21.93",
        "access_point_type": "purepython",
    }
    # 如果手动输入了 4399 账号密码，保存到 config 供启动时使用
    if req.account_source == "manual" and req.username_4399 and req.password_4399:
        config["username"] = req.username_4399
        config["password"] = req.password_4399
        config["auth_method"] = "4399"
    # 如果手动输入了 cookie/sauth_json，保存到 config
    if req.account_source == "manual" and (req.cookie or req.sauth_json):
        config["sauth_json"] = req.sauth_json or req.cookie
        config["sauth_updated_at"] = time.time()
        config["auth_method"] = "direct"
    # 如果自动注册了 4399 账号, 从注册结果中提取 sauth_json
    if req.account_source == "new" and new_account_sauth:
        config["sauth_json"] = new_account_sauth
        config["sauth_updated_at"] = time.time()
        config["auth_method"] = "direct"
    # 如果从 Cookie 池获取了 sauth_json, 保存到 config
    if req.account_source == "pool" and pool_account_sauth:
        config["sauth_json"] = pool_account_sauth
        config["sauth_updated_at"] = time.time()
        config["auth_method"] = "direct"

    # 确保 config 中有 platform_type (默认 pc)
    if not config.get("platform_type"):
        config["platform_type"] = "pc"

    bot_id = await db.create_bot_instance(
        panel_id=panel_id,
        name=bot_name,
        account_id=account_id,
        server_code=req.server_code or "",
        server_type="rental",
        access_point_type="purepython",
        config=json.dumps(config),
    )

    # 记录日志
    await db.add_log(
        target_type="bot", target_id=bot_id,
        level="success", message=f"机器人创建成功: {bot_name}",
        ip=request.client.host if request.client else "",
        created_by=user["user_id"],
    )

    return {
        "success": True,
        "data": {
            "bot_id": bot_id,
            "name": bot_name,
            "account_id": account_id,
            "panel_id": panel_id,
        },
    }


@router.get("/{bot_id}")
async def get_bot(bot_id: str, request: Request):
    """获取机器人实例详情。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    # 尝试从内存中获取实时状态和错误信息
    last_error = bot["last_error"] if "last_error" in bot.keys() else ""
    runtime_logs = []
    try:
        from app.bot.manager import bot_manager
        mem_bot = bot_manager.get_bot(bot_id)
        if mem_bot:
            if mem_bot.info.last_error:
                last_error = mem_bot.info.last_error
            runtime_logs = mem_bot.info.logs[-20:] if mem_bot.info.logs else []
    except Exception:
        pass

    return {
        "success": True,
        "data": {
            "bot_id": bot["bot_id"],
            "panel_id": bot["panel_id"],
            "name": bot["name"],
            "account_id": bot["account_id"],
            "server_code": bot["server_code"],
            "server_type": bot["server_type"],
            "access_point_type": bot["access_point_type"],
            "status": bot["status"],
            "created_at": bot["created_at"],
            "last_started_at": bot["last_started_at"],
            "config": json.loads(bot["config"]) if bot["config"] else {},
            "last_error": last_error,
            "runtime_logs": runtime_logs,
        },
    }


# ============================================================================
# 启动/停止/重启
# ============================================================================

@router.get("/{bot_id}/debug")
async def debug_bot(bot_id: str, request: Request):
    """调试端点: 返回机器人完整配置和诊断信息。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    # 尝试构建 BotConfig 并返回关键字段
    config_info = {}
    try:
        config = await _build_bot_config(bot, db)
        config_info = {
            "name": config.name,
            "server_code": config.server_code,
            "server_type": config.server_type.value,
            "access_point_type": config.access_point_type.value,
            "auth_method": config.auth_method,
            "auth_server": config.auth_server,
            "api_key": (config.api_key[:12] + "...") if config.api_key else "(empty)",
            "sauth_json_length": len(config.sauth_json) if config.sauth_json else 0,
            "sauth_json_preview": (config.sauth_json[:80] + "...") if config.sauth_json else "(empty)",
            "cookie": "(set)" if config.cookie else "(empty)",
            "device_model": config.device_model,
            "auto_reconnect": config.auto_reconnect,
            "max_reconnect_attempts": config.max_reconnect_attempts,
            "account_id": config.account_id,
        }
    except Exception as e:
        config_info = {"error": f"_build_bot_config failed: {type(e).__name__}: {e}"}

    # 内存中的 bot 实例状态
    mem_info = {}
    try:
        from app.bot.manager import bot_manager
        mem_bot = bot_manager.get_bot(bot_id)
        if mem_bot:
            mem_info = {
                "found": True,
                "status": mem_bot.info.status.value,
                "last_error": mem_bot.info.last_error,
                "logs_count": len(mem_bot.info.logs),
                "recent_logs": mem_bot.info.logs[-10:] if mem_bot.info.logs else [],
                "connected_at": mem_bot.info.connected_at,
                "_running": getattr(mem_bot, '_running', None),
                "_reconnect_count": getattr(mem_bot, '_reconnect_count', None),
                "has_access_point": mem_bot._access_point is not None,
                "ap_status": mem_bot._access_point.info.status.value if mem_bot._access_point else None,
                "ap_last_error": mem_bot._access_point.info.last_error if mem_bot._access_point else None,
            }
        else:
            mem_info = {"found": False}
    except Exception as e:
        mem_info = {"error": str(e)}

    return {
        "success": True,
        "data": {
            "bot_id": bot["bot_id"],
            "name": bot["name"],
            "db_status": bot["status"],
            "db_last_error": bot["last_error"] if "last_error" in bot.keys() else "",
            "db_config": json.loads(bot["config"]) if bot["config"] else {},
            "config_info": config_info,
            "memory_info": mem_info,
        },
    }

@router.get("/{bot_id}/ban-status")
async def get_ban_status(bot_id: str, request: Request):
    """获取机器人关联账号的封号状态。"""
    from .auth import get_current_user
    user = await get_current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    from app.ban_detection import ban_detector
    account_id = bot["account_id"]

    if not account_id:
        return {
            "success": True,
            "data": {
                "bot_id": bot_id,
                "account_id": "",
                "suspected_ban": False,
                "failure_count": 0,
                "message": "未关联游戏账号",
            },
        }

    record = ban_detector.get_record(account_id)
    return {
        "success": True,
        "data": {
            "bot_id": bot_id,
            "account_id": account_id,
            "suspected_ban": ban_detector.is_suspected_ban(account_id),
            "failure_count": ban_detector.get_failure_count(account_id),
            "threshold": 3,
            "record": record,
        },
    }


@router.post("/{bot_id}/start")
async def start_bot(bot_id: str, request: Request):
    """启动机器人实例。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    # 检查面板是否过期
    panel = await db.get_panel(bot["panel_id"])
    now = time.time()
    if panel and panel["expire_at"] and panel["expire_at"] < now:
        raise HTTPException(status_code=403, detail="面板已到期，请续费后继续使用")

    if bot["status"] in ("running", "starting", "connecting"):
        raise HTTPException(status_code=400, detail="机器人已在运行中")

    # 封号检测: 检查关联账号是否被标记为封号
    if bot["account_id"]:
        from app.ban_detection import ban_detector
        if ban_detector.is_suspected_ban(bot["account_id"]):
            record = ban_detector.get_record(bot["account_id"])
            fail_count = record["failure_count"] if record else 0
            raise HTTPException(
                status_code=403,
                detail=f"账号疑似被封号 (连续 {fail_count} 次登录失败), 请解除封号标记后再启动"
            )

    # 尝试通过现有 bot_manager 启动
    started = False
    error_msg = ""
    try:
        from app.bot.manager import bot_manager

        # 构建 BotConfig, 并确保内存中存在 bot_id 一致的实例, 再启动
        config = await _build_bot_config(bot, db)
        await _ensure_bot_in_memory(bot_id, config, bot_manager)
        started = await bot_manager.start_bot(bot_id)
        if not started:
            raise RuntimeError("start_bot 返回 False, 机器人启动失败")
    except Exception as e:
        error_msg = str(e)

    # 更新状态 - 启动时设为 starting，实际状态由 bot 运行循环同步
    new_status = "starting" if started else "error"
    await db.update_bot_status(bot_id, new_status)

    await db.add_log(
        target_type="bot", target_id=bot_id,
        level="success" if started else "error",
        message=f"启动机器人: {bot['name']}" + ("" if started else f" (失败: {error_msg})"),
        created_by=user["user_id"],
    )

    if not started:
        raise HTTPException(status_code=500, detail=f"启动失败: {error_msg}")

    return {"success": True, "message": f"机器人 {bot['name']} 已启动"}


@router.post("/{bot_id}/stop")
async def stop_bot(bot_id: str, request: Request):
    """停止机器人实例。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    # 允许从 running/starting/connecting/error 状态停止
    # (error 状态下可能仍有重连尝试在运行, 需要能停止)
    if bot["status"] not in ("running", "starting", "connecting", "error"):
        raise HTTPException(status_code=400, detail="机器人未在运行")

    try:
        from app.bot.manager import bot_manager
        await bot_manager.stop_bot(bot_id)
    except Exception:
        pass  # 即使停止失败也更新状态

    await db.update_bot_status(bot_id, "stopped")
    await db.add_log(
        target_type="bot", target_id=bot_id,
        level="info",
        message=f"停止机器人: {bot['name']}",
        created_by=user["user_id"],
    )

    return {"success": True, "message": f"机器人 {bot['name']} 已停止"}


@router.post("/{bot_id}/restart")
async def restart_bot(bot_id: str, request: Request):
    """重启机器人实例。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    # 先停止 (卸载插件 + 停止运行), 并移除内存中的旧实例以便用最新配置重新创建
    try:
        from app.bot.manager import bot_manager
        await bot_manager.stop_bot(bot_id)
        bot_manager._bots.pop(bot_id, None)
    except Exception:
        pass

    await db.update_bot_status(bot_id, "stopped")

    # 再启动
    started = False
    error_msg = ""
    try:
        from app.bot.manager import bot_manager

        # 构建 BotConfig, 重新创建机器人实例, 再启动
        config = await _build_bot_config(bot, db)
        await _ensure_bot_in_memory(bot_id, config, bot_manager)
        started = await bot_manager.start_bot(bot_id)
        if not started:
            raise RuntimeError("start_bot 返回 False, 机器人启动失败")
    except Exception as e:
        error_msg = str(e)

    # 更新状态 - 重启时设为 starting，实际状态由 bot 运行循环同步
    await db.update_bot_status(bot_id, "starting" if started else "error")
    await db.add_log(
        target_type="bot", target_id=bot_id,
        level="success" if started else "error",
        message=f"重启机器人: {bot['name']}" + ("" if started else f" (失败: {error_msg})"),
        created_by=user["user_id"],
    )

    if not started:
        raise HTTPException(status_code=500, detail=f"重启失败: {error_msg}")

    return {"success": True, "message": f"机器人 {bot['name']} 已重启"}


# ============================================================================
# 更新配置
# ============================================================================

@router.put("/{bot_id}/config")
async def update_bot_config(bot_id: str, req: UpdateBotConfigRequest, request: Request):
    """更新机器人实例配置。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    if bot["status"] in ("running", "starting", "connecting"):
        raise HTTPException(status_code=400, detail="请先停止机器人再修改配置")

    # 合并配置
    old_config = json.loads(bot["config"]) if bot["config"] else {}
    config_modified = False
    if req.config:
        old_config.update(req.config)
        config_modified = True
    if req.game_version:
        old_config["game_version"] = req.game_version
        config_modified = True
    # 同步 server_code 和 server_type 到 config, 供接入点读取
    if req.server_code is not None:
        old_config["server_code"] = req.server_code
        config_modified = True
    if req.server_type is not None:
        old_config["server_type"] = req.server_type
        config_modified = True
    if req.access_point_type is not None:
        old_config["access_point_type"] = req.access_point_type
        config_modified = True
    if req.platform_type is not None:
        old_config["platform_type"] = req.platform_type
        config_modified = True

    config_json = json.dumps(old_config, ensure_ascii=False)

    # 更新数据库 (直接 SQL)
    updates = []
    params = []
    if req.name is not None:
        updates.append("name = ?")
        params.append(req.name)
    if req.account_id is not None:
        updates.append("account_id = ?")
        params.append(req.account_id)
    if req.server_code is not None:
        updates.append("server_code = ?")
        params.append(req.server_code)
    if req.server_type is not None:
        updates.append("server_type = ?")
        params.append(req.server_type)
    if req.access_point_type is not None:
        updates.append("access_point_type = ?")
        params.append(req.access_point_type)
    # 只要 config_json 被修改过 (有合并操作)，就应更新 config 列，避免丢失
    if config_modified:
        updates.append("config = ?")
        params.append(config_json)

    if updates:
        params.append(bot_id)
        sql = f"UPDATE bot_instances SET {', '.join(updates)} WHERE bot_id = ?"
        await db.conn.execute(sql, params)
        await db.conn.commit()

    await db.add_log(
        target_type="bot", target_id=bot_id,
        level="info",
        message=f"更新机器人配置: {bot['name']}",
        created_by=user["user_id"],
    )

    return {"success": True, "message": "配置已更新"}


# ============================================================================
# 删除机器人
# ============================================================================

@router.delete("/{bot_id}")
async def delete_bot(bot_id: str, request: Request):
    """删除机器人实例。"""
    from .auth import require_user
    user = await require_user(request)
    db = await get_db()

    bot = await _get_bot_with_access(bot_id, user, db)

    # 如果正在运行或启动中, 先停止
    if bot["status"] in ("running", "starting", "connecting"):
        try:
            from app.bot.manager import bot_manager
            await bot_manager.stop_bot(bot_id)
        except Exception:
            pass

    bot_name = bot["name"]
    await db.delete_bot(bot_id)

    await db.add_log(
        target_type="bot", target_id=bot_id,
        level="warn",
        message=f"删除机器人: {bot_name}",
        created_by=user["user_id"],
    )
    return {"success": True, "message": "机器人已删除"}
