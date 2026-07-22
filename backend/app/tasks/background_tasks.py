"""PocketTerm 后台任务。

包含:
    - 面板到期自动检查 (每 60 秒)
    - nv1 SAuth Key 自动刷新 (每小时检查, 到期前 24 小时刷新)
    - 封号检测清理 (每 5 分钟清理过期记录)
    - 4399 账号 sauth_json 自动刷新 (每 2 小时)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

logger = logging.getLogger("pocketterm.tasks")


# ============================================================================
# 面板到期自动检查
# ============================================================================

async def check_expired_panels_loop() -> None:
    """每 60 秒检查一次过期面板, 自动更新状态并停止相关机器人。"""
    logger.info("面板到期检查任务已启动 (间隔 60s)")
    while True:
        try:
            from app.database import get_db
            db = await get_db()
            expired_panels = await db.get_expired_panels()
            if not expired_panels:
                continue

            for panel in expired_panels:
                panel_id = panel["panel_id"]
                panel_name = panel["name"]

                # 1. 更新面板状态为 expired
                await db.update_panel_status(panel_id, "expired")

                # 2. 记录日志
                await db.add_log(
                    target_type="panel",
                    target_id=panel_id,
                    level="warn",
                    message=f"面板已自动过期: {panel_name}",
                    created_by="system",
                )
                logger.info(f"面板已过期: {panel_name} ({panel_id})")

                # 3. 停止该面板下所有运行中的机器人
                bots = await db.list_bots_by_panel(panel_id)
                for bot in bots:
                    if bot["status"] == "running":
                        bot_id = bot["bot_id"]
                        bot_name = bot["name"]
                        try:
                            from app.bot.manager import bot_manager
                            await bot_manager.stop_bot(bot_id)
                        except Exception:
                            pass  # 即使停止失败也更新状态

                        await db.update_bot_status(bot_id, "stopped")
                        await db.add_log(
                            target_type="bot",
                            target_id=bot_id,
                            level="warn",
                            message=f"面板过期, 自动停止机器人: {bot_name}",
                            created_by="system",
                        )
                        logger.info(f"已停止机器人 (面板过期): {bot_name} ({bot_id})")

        except asyncio.CancelledError:
            logger.info("面板到期检查任务已停止")
            break
        except Exception:
            logger.exception("面板到期检查任务出错")
        await asyncio.sleep(60)


# ============================================================================
# nv1 SAuth Key 自动刷新
# ============================================================================

async def nv1_auto_refresh_loop() -> None:
    """每小时检查 nv1 Key 有效期, 到期前 24 小时自动刷新。"""
    logger.info("nv1 Key 自动刷新任务已启动 (间隔 3600s)")
    while True:
        try:
            from app.auth.nv1_manager import nv1_manager
            if not nv1_manager.is_configured():
                continue

            remaining = nv1_manager.get_remaining_seconds()
            if remaining is not None and remaining < 24 * 3600:
                logger.info(f"nv1 Key 即将过期 (剩余 {remaining:.0f}s), 开始自动刷新...")
                result = await nv1_manager.refresh_key()
                if result["success"]:
                    logger.info("nv1 Key 自动刷新成功")
                    # 记录日志
                    from app.database import get_db
                    db = await get_db()
                    await db.add_log(
                        target_type="system",
                        target_id="nv1_refresh",
                        level="success",
                        message="nv1 SAuth Key 自动刷新成功",
                        details=json.dumps({"expires_at": result.get("expires_at")}),
                        created_by="system",
                    )
                else:
                    logger.error(f"nv1 Key 自动刷新失败: {result.get('error')}")
                    from app.database import get_db
                    db = await get_db()
                    await db.add_log(
                        target_type="system",
                        target_id="nv1_refresh",
                        level="error",
                        message=f"nv1 SAuth Key 自动刷新失败: {result.get('error')}",
                        created_by="system",
                    )

        except asyncio.CancelledError:
            logger.info("nv1 Key 自动刷新任务已停止")
            break
        except Exception:
            logger.exception("nv1 Key 自动刷新任务出错")
        await asyncio.sleep(3600)


# ============================================================================
# 封号检测清理
# ============================================================================

async def ban_detection_cleanup_loop() -> None:
    """每 5 分钟清理一次过期的封号检测记录。"""
    logger.info("封号检测清理任务已启动 (间隔 300s)")
    while True:
        await asyncio.sleep(300)
        try:
            from app.ban_detection import ban_detector
            cleaned = ban_detector.cleanup_expired()
            if cleaned > 0:
                logger.debug(f"清理了 {cleaned} 条过期封号检测记录")
        except asyncio.CancelledError:
            logger.info("封号检测清理任务已停止")
            break
        except Exception:
            logger.exception("封号检测清理任务出错")


# ============================================================================
# 4399 账号 sauth_json 自动刷新
# ============================================================================

async def sauth_auto_refresh_loop() -> None:
    """每 2 小时使用存储的 4399 账号刷新一次 sauth_json。

    定期调用 :data:`app.auth.sauth_refresh.sauth_refresher.get_fresh_sauth`,
    使内存缓存始终保持新鲜凭证, 避免机器人在启动/重连时才触发登录
    (登录流程较慢, 提前刷新可减少机器人连接延迟)。
    """
    logger.info("sauth_json 自动刷新任务已启动 (间隔 7200s)")
    while True:
        await asyncio.sleep(7200)
        try:
            from app.auth.sauth_refresh import sauth_refresher
            # 若缓存仍有效则跳过本次刷新
            if sauth_refresher._is_cache_valid():
                logger.debug("sauth_json 缓存仍有效, 跳过本次定时刷新")
                continue

            logger.info("定时任务: 开始刷新 sauth_json (4399 账号池)")
            sauth_str = await sauth_refresher.get_fresh_sauth()

            from app.database import get_db
            db = await get_db()
            if sauth_str:
                await db.add_log(
                    target_type="system",
                    target_id="sauth_refresh",
                    level="success",
                    message="定时任务: sauth_json 自动刷新成功",
                    details=json.dumps(
                        sauth_refresher.get_status(), ensure_ascii=False
                    ),
                    created_by="system",
                )
                logger.info("定时任务: sauth_json 自动刷新成功")
            else:
                await db.add_log(
                    target_type="system",
                    target_id="sauth_refresh",
                    level="error",
                    message="定时任务: sauth_json 自动刷新失败 (无可用 4399 账号或登录均失败)",
                    created_by="system",
                )
                logger.error("定时任务: sauth_json 自动刷新失败")
        except asyncio.CancelledError:
            logger.info("sauth_json 自动刷新任务已停止")
            break
        except Exception:
            logger.exception("sauth_json 自动刷新任务出错")
