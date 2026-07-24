"""PocketTerm FastAPI 应用入口。

本模块负责:

    * 通过 ``lifespan`` 上下文管理器完成应用启动 / 停止的全生命周期编排
    * 注册所有 API 路由 (auth / bots / accounts / plugins / files / system /
      access_points / ws / cookies / imports / settings)
    * 配置 CORS 中间件
    * 挂载前端静态资源目录 (``frontend/css``、``frontend/js``)
    * 提供根路由 ``/`` 返回 ``frontend/index.html``
    * 注册全局异常处理器，统一未处理异常的 JSON 响应格式
    * 启动时打印彩色 ASCII Logo

统一 JSON 响应格式 (与 :mod:`app.api.deps` 保持一致)::

    {
        "success": true | false,
        "message": "...",
        "data": ...,      # success=True 时存在
        "error": "..."    # success=False 时存在
    }
"""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# -- 配置 / 路径 -----------------------------------------------------------
from app.config import (
    BASE_DIR,
    PROJECT_ROOT,
    DATA_DIR,
    FRONTEND_DIR,
    PLUGINS_DIR,
    ensure_directories,
    get_config,
)
# 注: logger 模块实际导出的是 ``setup_logger``，这里按需求以
# ``setup_logging`` 别名导入，便于在入口中按习惯命名调用。
from app.logger import get_logger, setup_logger as setup_logging
from app.bot.manager import bot_manager
from app.account.storage import AccountStorage
from app.plugins.manager import plugin_manager

# -- API 路由 --------------------------------------------------------------
from app.api.auth import router as auth_router
from app.api.bots import router as bots_router
from app.api.accounts import router as accounts_router
from app.api.plugins import router as plugins_router
from app.api.files import router as files_router
from app.api.system import router as system_router
from app.api.access_points import router as access_points_router
from app.api.ws import router as ws_router
from app.api.ws_events import router as ws_events_router
from app.api.events import router as events_router
from app.api.devices import router as devices_router
from app.api.cookies import router as cookies_router
from app.api.imports import router as imports_router
from app.api.settings import router as settings_router

# v2 API (多用户系统)
from app.api.v2.users import router as v2_users_router
from app.api.v2.cards import router as v2_cards_router
from app.api.v2.panels import router as v2_panels_router
from app.api.v2.bots import router as v2_bots_router
from app.api.v2.logs import router as v2_logs_router
from app.api.v2.system_admin import router as v2_system_admin_router
from app.api.v2.announcements import router as v2_announcements_router
from app.api.v2.sauth_refresh import router as v2_sauth_refresh_router
from app.api.v2.shop import router as v2_shop_router
from app.api.v2.user_files import router as v2_user_files_router
from app.api.v2.runner import router as v2_runner_router


__all__ = ["app", "lifespan"]


#: 应用版本号
APP_VERSION: str = "2.4.0"

#: 应用入口日志器
logger = get_logger("app")

#: 全局账号存储实例 (在 lifespan 启动阶段初始化，保持引用避免被回收)
_account_storage: Optional[AccountStorage] = None


# ---------------------------------------------------------------------------
# 彩色 ASCII Logo
# ---------------------------------------------------------------------------
def _print_logo() -> None:
    """打印彩色 ASCII Logo "PocketTerm"。

    使用 ANSI 转义码着色 (亮青色 Logo + 亮绿色副标题)，在支持彩色
    的终端中显示，不支持彩色的终端会原样输出转义序列 (无害)。
    """
    cyan = "\033[96m\033[1m"
    green = "\033[92m"
    dim = "\033[2m"
    reset = "\033[0m"

    logo = (
        cyan
        + "██╗  ██████╗ ███████╗██████╗  ██████╗ ███╗   ██╗███████╗███████╗██╗  ██╗\n"
        + "██║ ██╔═══██╗██╔════╝██╔══██╗██╔═══██╗████╗  ██║██╔════╝██╔════╝╚██╗██╔╝\n"
        + "██║ ██║   ██║███████╗██████╔╝██║   ██║██╔██╗ ██║███████╗█████╗   ╚███╔╝ \n"
        + "██║ ██║   ██║╚════██║██╔══██╗██║   ██║██║╚██╗██║╚════██║██╔══╝   ██╔██╗ \n"
        + "██║ ╚██████╔╝███████║██████╔╝╚██████╔╝██║ ╚████║███████║███████╗██╔╝ ██╗\n"
        + "╚═╝  ╚═════╝ ╚══════╝╚═════╝  ╚═════╝ ╚═╝  ╚═══╝╚══════╝╚══════╝╚═╝  ╚═╝\n"
        + reset
    )
    subtitle = f"{green}  Minecraft Bedrock Bot Management Platform v{APP_VERSION}{reset}"
    separator = f"{dim}  " + "-" * 64 + f"{reset}"

    print()
    print(logo)
    print(subtitle)
    print(separator)


# ---------------------------------------------------------------------------
# 生命周期管理 (lifespan)
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期上下文管理器。

    启动阶段:
        1. 打印彩色 ASCII Logo
        2. 根据配置初始化日志系统
        3. 初始化账号数据库 (``AccountStorage.init_db``)
        4. 初始化插件管理器 (扫描并发现插件)
        5. 切换工作目录到 ``backend``

    停止阶段:
        * 停止所有正在运行的机器人
    """
    global _account_storage

    # -- 启动 ---------------------------------------------------------------
    _print_logo()

    config = get_config()

    # 1. 初始化日志 (按 config.yaml 中的 level / file 重新配置)
    setup_logging(level=config.log_level, log_file=str(config.log_file))
    logger.info("=" * 60)
    logger.info(f"PocketTerm v{APP_VERSION} 启动中...")
    logger.info(f"PROJECT_ROOT = {PROJECT_ROOT}")
    logger.info(f"BASE_DIR     = {BASE_DIR}")
    logger.info(f"DATA_DIR     = {DATA_DIR}")
    logger.info(f"FRONTEND_DIR = {FRONTEND_DIR}")
    logger.info(f"PLUGINS_DIR  = {PLUGINS_DIR}")

    # 确保运行时目录存在 (data / plugins)
    ensure_directories()

    # 2. 初始化账号数据库 (旧)
    try:
        _account_storage = AccountStorage()
        await _account_storage.init_db()
        logger.info("账号数据库初始化完成")
    except Exception:  # noqa: BLE001
        logger.exception("账号数据库初始化失败 (将继续启动)")
        _account_storage = None

    # 2b. 初始化新数据库 (多用户/卡密/面板/日志)
    try:
        from app.database import get_db, close_db
        db = await get_db()
        logger.info("多用户数据库初始化完成")

        # JWT 密钥现在由 app.security 模块在导入时从环境变量
        # POCKETTERM_JWT_SECRET 读取 (生产环境未设置会直接抛错)，
        # 此处不再从 config.yaml 覆盖，避免弱密钥泄露。

        # 默认超级管理员账号 (admin/admin123456, owner/Owner@2026)
        # 已在数据库初始化阶段 (Database.init_db -> _ensure_default_admins)
        # 自动创建。此处仅对历史遗留的 admin 账号做角色升级, 保证其为 superadmin。
        admin_user = await db.get_user_by_username("admin")
        if admin_user is not None and admin_user["role"] != "superadmin":
            await db.update_user_role(admin_user["user_id"], "superadmin")
            logger.info("已将 admin 升级为 superadmin")
    except Exception:  # noqa: BLE001
        logger.exception("多用户数据库初始化失败 (将继续启动)")

    # 重置所有机器人状态为 stopped (服务器重启后没有机器人实际在运行)
    try:
        await db.conn.execute("UPDATE bot_instances SET status = 'stopped' WHERE status != 'stopped'")
        await db.conn.commit()
        logger.info("已重置所有机器人状态为 stopped")
    except Exception:
        logger.exception("重置机器人状态失败 (将继续启动)")

    # 3. 初始化插件管理器 (扫描 python / go / java 三个语言目录)
    try:
        infos = plugin_manager.discover_plugins()
        logger.info(f"插件管理器初始化完成，共发现 {len(infos)} 个插件")
    except Exception:  # noqa: BLE001
        logger.exception("插件管理器初始化失败 (将继续启动)")

    # 4. 切换工作目录到 backend (使相对路径 / 配置写入落在 backend 下)
    backend_dir: Path = BASE_DIR.parent
    try:
        os.chdir(str(backend_dir))
        logger.info(f"工作目录切换到: {backend_dir}")
    except OSError:
        logger.warning(f"无法切换工作目录到 {backend_dir}，保持当前目录")

    # 5. 加载设备指纹库 (确保账号设备指纹稳定, 防封禁)
    try:
        from app.auth.device_fingerprint import get_fingerprint_manager
        fp_mgr = get_fingerprint_manager()
        fp_stats = fp_mgr.stats()
        logger.info(
            f"设备指纹库已加载: 共 {fp_stats.get('total', 0)} 条指纹 "
            f"(来源: {fp_stats.get('file_path', 'N/A')})"
        )
    except Exception:  # noqa: BLE001
        logger.exception("设备指纹库加载失败 (将继续启动)")

    # 6. 初始化 nv1 SAuth Key (模拟模式)
    try:
        from app.auth.nv1_manager import nv1_manager
        await nv1_manager.init_mock_if_needed()
        logger.info(f"nv1 SAuth Key 已初始化 (模式: {'模拟' if nv1_manager.is_mock_mode() else '真实'})")
    except Exception:  # noqa: BLE001
        logger.exception("nv1 SAuth Key 初始化失败 (将继续启动)")

    # 7. 启动后台任务
    import asyncio as _asyncio
    _background_tasks: list = []
    try:
        from app.tasks import (
            check_expired_panels_loop,
            nv1_auto_refresh_loop,
            ban_detection_cleanup_loop,
            sauth_auto_refresh_loop,
        )
        loop = _asyncio.get_event_loop()
        _background_tasks.append(loop.create_task(check_expired_panels_loop()))
        _background_tasks.append(loop.create_task(nv1_auto_refresh_loop()))
        _background_tasks.append(loop.create_task(ban_detection_cleanup_loop()))
        _background_tasks.append(loop.create_task(sauth_auto_refresh_loop()))
        logger.info(f"已启动 {len(_background_tasks)} 个后台任务")
    except Exception:  # noqa: BLE001
        logger.exception("后台任务启动失败 (将继续启动)")

    logger.info("PocketTerm 启动完成，等待请求")
    logger.info("=" * 60)

    yield

    # -- 停止 ---------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("PocketTerm 正在停止...")

    # 取消后台任务
    for task in _background_tasks:
        task.cancel()
    if _background_tasks:
        logger.info(f"已取消 {len(_background_tasks)} 个后台任务")

    try:
        await bot_manager.stop_all()
        logger.info("所有机器人已停止")
    except Exception:  # noqa: BLE001
        logger.exception("停止机器人时发生异常")

    # 停止事件监听器
    try:
        from app.protocol.event_manager import event_manager
        await event_manager.stop_all()
        logger.info("所有事件监听器已停止")
    except Exception:  # noqa: BLE001
        logger.exception("停止事件监听器时发生异常")

    # 停止游戏事件广播器
    try:
        from app.api.ws_events import events_manager
        await events_manager.cancel_broadcaster()
        logger.info("游戏事件广播器已停止")
    except Exception:  # noqa: BLE001
        logger.exception("停止游戏事件广播器时发生异常")

    # 优雅关闭账号数据库连接
    if _account_storage is not None:
        try:
            await _account_storage.close()
            logger.info("账号数据库连接已关闭")
        except Exception:  # noqa: BLE001
            logger.exception("关闭账号数据库连接时发生异常")

    # 关闭新数据库
    try:
        from app.database import close_db
        await close_db()
        logger.info("多用户数据库连接已关闭")
    except Exception:  # noqa: BLE001
        logger.exception("关闭多用户数据库时发生异常")

    logger.info("PocketTerm 已停止")
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# 创建 FastAPI 应用
# ---------------------------------------------------------------------------
app = FastAPI(
    title="PocketTerm",
    description="Minecraft Bedrock 机器人管理平台",
    version=APP_VERSION,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# CORS 中间件
# ---------------------------------------------------------------------------
def _get_cors_origins() -> list[str]:
    """从环境变量 ``POCKETTERM_CORS_ORIGINS`` 读取允许的跨域来源列表。

    加载规则:

        - 若环境变量已设置 (逗号分隔，如
          ``https://example.com,http://localhost:3000``)，按逗号分割、去
          空白、过滤空串后返回。
        - 若环境变量未设置:
            * 生产环境 (``POCKETTERM_ENV=production``) 打印严重警告并使用
              ``["*"]`` (允许所有来源) 降级启动, 避免服务无法启动。
            * 开发环境返回 ``["http://localhost:8000"]`` 并打印警告日志。

    Returns:
        允许的来源 Origin 列表。
    """
    raw = os.environ.get("POCKETTERM_CORS_ORIGINS", "").strip()
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        if origins:
            return origins

    env = os.environ.get("POCKETTERM_ENV", "").strip().lower()
    if env == "production":
        # 生产环境未设置 CORS: 降级允许所有来源, 避免服务崩溃
        logger.error(
            "生产环境未设置 POCKETTERM_CORS_ORIGINS！"
            "已降级为允许所有来源 (*)。请尽快设置该环境变量以保证安全性。"
        )
        return ["*"]

    logger.warning(
        "未设置 POCKETTERM_CORS_ORIGINS 环境变量，开发环境默认允许 "
        "http://localhost:8000。生产环境部署前请务必配置该变量。"
    )
    return ["http://localhost:8000"]


# 注意: allow_credentials=True 时不能使用 allow_origins=["*"]，
# 否则浏览器会拒绝携带 Cookie 的跨域请求。这里从环境变量显式读取允许的
# 前端来源列表，确保生产环境只放行可信域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=_get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# 全局异常处理 (捕获所有未处理异常，返回统一 JSON 格式)
# ---------------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """捕获所有未被路由显式处理的异常，返回统一 JSON 错误响应。

    FastAPI 内置的 ``HTTPException`` / ``RequestValidationError`` 处理器
    优先级更高，因此本处理器只接管真正"未处理"的异常 (通常对应 500)。
    """
    logger.exception(
        f"未处理异常: {request.method} {request.url.path} -> {exc!r}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "message": "服务器内部错误",
            "error": "internal_server_error",
            "detail": str(exc),
        },
    )


# ---------------------------------------------------------------------------
# 静态文件服务 (frontend/css, frontend/js)
# ---------------------------------------------------------------------------
# 静态目录可能尚未创建 (例如首次运行、前端尚未构建)，挂载前先检查存在性，
# 避免因目录缺失导致应用启动失败。
_css_dir: Path = FRONTEND_DIR / "css"
_js_dir: Path = FRONTEND_DIR / "js"

if _css_dir.is_dir():
    app.mount("/css", StaticFiles(directory=str(_css_dir)), name="static-css")
    logger.debug(f"已挂载静态目录 /css -> {_css_dir}")
else:
    logger.debug(f"静态目录不存在，跳过挂载: {_css_dir}")

if _js_dir.is_dir():
    app.mount("/js", StaticFiles(directory=str(_js_dir)), name="static-js")
    logger.debug(f"已挂载静态目录 /js -> {_js_dir}")
else:
    logger.debug(f"静态目录不存在，跳过挂载: {_js_dir}")


# ---------------------------------------------------------------------------
# 根路由 -> frontend/index.html
# ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """根路由，返回前端入口页面 ``frontend/index.html``。

    若前端文件不存在，返回 404 JSON (统一格式) 以便前端可据此提示。
    """
    dashboard: Path = FRONTEND_DIR / "index.html"
    if dashboard.is_file():
        return FileResponse(str(dashboard))
    return JSONResponse(
        status_code=404,
        content={
            "success": False,
            "message": "前端页面未找到",
            "error": "frontend_not_found",
            "detail": f"期望位于: {dashboard}",
        },
    )


# ---------------------------------------------------------------------------
# 注册所有 API 路由
# ---------------------------------------------------------------------------
app.include_router(auth_router)
app.include_router(bots_router)
app.include_router(accounts_router)
app.include_router(plugins_router)
app.include_router(files_router)
app.include_router(system_router)
app.include_router(access_points_router)
app.include_router(ws_router)
app.include_router(ws_events_router)
app.include_router(events_router)
app.include_router(devices_router)
app.include_router(cookies_router)
app.include_router(imports_router)
app.include_router(settings_router)

# v2 API 路由 (多用户/卡密/面板/日志/系统管理/公告)
app.include_router(v2_users_router)
app.include_router(v2_cards_router)
app.include_router(v2_panels_router)
app.include_router(v2_bots_router)
app.include_router(v2_logs_router)
app.include_router(v2_system_admin_router)
app.include_router(v2_announcements_router)
app.include_router(v2_sauth_refresh_router)
app.include_router(v2_shop_router)
app.include_router(v2_user_files_router)
app.include_router(v2_runner_router)


if __name__ == "__main__":
    # 直接 ``python -m app.main`` 启动时的入口 (开发用)。
    import uvicorn

    config = get_config()
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=False,
    )
