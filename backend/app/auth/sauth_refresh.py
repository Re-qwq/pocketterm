"""4399 账号 sauth_json 自动刷新管理器。

功能:
    - 管理 4399 账号池 (用户名/密码), 持久化到数据库 ``sauth_accounts`` 表
    - 当机器人需要 sauth_json 时, 使用一个可用的 4399 账号通过 OAuth2 登录
      获取全新的 sauth_json
    - 缓存 sauth_json 与时间戳, 超过 2 小时自动刷新
    - 支持多账号轮询 (round-robin), 单个账号失败自动尝试下一个
    - 记录所有刷新尝试到日志

使用方式::

    from app.auth.sauth_refresh import sauth_refresher

    # 添加 4399 账号
    await sauth_refresher.add_4399_account("user1", "pass1")

    # 获取新鲜的 sauth_json (缓存有效则返回缓存, 否则刷新)
    sauth_json = await sauth_refresher.get_fresh_sauth()

    # 手动刷新
    sauth_json = await sauth_refresher.refresh_sauth("user1", "pass1")

    # 测试账号
    result = await sauth_refresher.test_4399_account("user1", "pass1")

底层登录流程复用 :class:`app.auth.netease_direct.login_4399_oauth2.Login4399OAuth2`,
sauth_json 结构由 :func:`app.auth.netease_direct.login_4399_oauth2.build_sauth_json` 构建。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from app.auth.netease_direct.login_4399_oauth2 import (
    Login4399OAuth2,
    build_sauth_json,
)

logger = logging.getLogger("pocketterm.sauth_refresh")

#: sauth_json 缓存有效期 (2 小时)。超过该时间认为凭证已过期, 需要重新登录。
SAUTH_CACHE_TTL: int = 2 * 3600

#: 账号状态: 可用
STATUS_ACTIVE: str = "active"
#: 账号状态: 失败 (登录失败被临时标记)
STATUS_FAILED: str = "failed"
#: 账号状态: 已禁用
STATUS_DISABLED: str = "disabled"


class SauthRefresher:
    """4399 账号 sauth_json 自动刷新管理器。

    管理一个 4399 账号池, 当需要 sauth_json 时使用轮询方式依次尝试登录,
    成功后缓存 sauth_json 与时间戳, 2 小时内复用缓存, 超时自动刷新。

    单例实例 :data:`sauth_refresher` 在模块导入时创建, 与
    :class:`app.auth.nv1_manager.NV1Manager` 保持一致的使用风格。
    """

    def __init__(self) -> None:
        # 内存缓存
        self._cached_sauth: str = ""
        self._cached_at: float = 0.0
        self._cached_uid: str = ""
        self._cached_username: str = ""
        # 轮询索引
        self._rr_index: int = 0
        # 刷新锁: 避免并发刷新
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    def get_status(self) -> dict:
        """获取刷新状态 (内存缓存信息)。"""
        now = time.time()
        has_cache = bool(self._cached_sauth)
        age = (now - self._cached_at) if has_cache else None
        return {
            "cached": has_cache,
            "cached_at": self._cached_at if has_cache else None,
            "cached_age_seconds": age,
            "is_valid": bool(
                has_cache and (now - self._cached_at) < SAUTH_CACHE_TTL
            ),
            "cached_uid": self._cached_uid,
            "cached_username": self._cached_username,
            "cache_ttl_seconds": SAUTH_CACHE_TTL,
        }

    def _is_cache_valid(self) -> bool:
        """缓存是否仍然有效 (< 2 小时)。"""
        return bool(
            self._cached_sauth
            and (time.time() - self._cached_at) < SAUTH_CACHE_TTL
        )

    # ------------------------------------------------------------------
    # 核心: 获取新鲜的 sauth_json
    # ------------------------------------------------------------------
    async def get_fresh_sauth(self) -> Optional[str]:
        """返回新鲜的 sauth_json 字符串。

        - 若缓存的 sauth_json 仍然有效 (< 2 小时), 直接返回缓存。
        - 否则使用 4399 账号池中的账号轮询登录, 获取全新 sauth_json。
        - 若所有账号均登录失败, 返回最近一次缓存的 sauth_json (可能已过期)
          作为降级, 没有缓存则返回 None。
        """
        # 快速路径: 缓存有效直接返回 (无需加锁)
        if self._is_cache_valid():
            logger.debug("sauth_json 缓存有效, 直接返回缓存")
            return self._cached_sauth

        async with self._lock:
            # 双重检查: 等锁期间可能已被其他协程刷新
            if self._is_cache_valid():
                return self._cached_sauth

            db = await self._get_db()
            accounts = await db.get_active_sauth_accounts()
            if not accounts:
                # 没有可用账号时，尝试将 failed 账号重置为 active 并重试一次
                # (disabled 账号属于主动禁用，不参与重置)
                logger.warning(
                    "没有可用的 active 4399 账号, 尝试重置 failed 账号后重试"
                )
                all_accounts = await db.list_sauth_accounts()
                reset_count = 0
                for acc in all_accounts:
                    if acc["status"] == STATUS_FAILED:
                        await db.update_sauth_account_status(
                            acc["id"], STATUS_ACTIVE
                        )
                        reset_count += 1
                if reset_count > 0:
                    logger.info(
                        f"已重置 {reset_count} 个 failed 4399 账号为 active, 重试刷新"
                    )
                    accounts = await db.get_active_sauth_accounts()
                if not accounts:
                    logger.warning("没有可用的 4399 账号, 无法刷新 sauth_json")
                    await self._log_refresh(
                        db, success=False,
                        message="刷新 sauth_json 失败: 没有可用的 4399 账号",
                        username="",
                    )
                    return self._cached_sauth or None

            # 轮询尝试每个账号
            n = len(accounts)
            start = self._rr_index % n
            for i in range(n):
                idx = (start + i) % n
                account = accounts[idx]
                # 推进轮询索引到下一个账号
                self._rr_index = (idx + 1) % n

                username = account["username"]
                password = account["password"]
                logger.info(
                    f"尝试使用 4399 账号刷新 sauth_json: {username} "
                    f"({i + 1}/{n})"
                )

                sauth_str = await self.refresh_sauth(username, password)
                if sauth_str:
                    self._cached_sauth = sauth_str
                    self._cached_at = time.time()
                    self._cached_uid = account["uid"] or ""
                    self._cached_username = username
                    logger.info(
                        f"sauth_json 刷新成功 (账号: {username}, "
                        f"uid: {self._cached_uid})"
                    )
                    return sauth_str

                # 该账号失败, 标记为 failed
                await db.update_sauth_account_status(account["id"], STATUS_FAILED)

            logger.error("所有 4399 账号登录均失败, 无法刷新 sauth_json")
            await self._log_refresh(
                db, success=False,
                message="刷新 sauth_json 失败: 所有 4399 账号登录均失败",
                username="",
            )
            # 降级: 返回过期缓存
            return self._cached_sauth or None

    # ------------------------------------------------------------------
    # 刷新: 使用指定账号登录
    # ------------------------------------------------------------------
    async def refresh_sauth(
        self, account_username: str, account_password: str
    ) -> Optional[str]:
        """使用指定 4399 账号登录, 获取全新的 sauth_json 字符串。

        Args:
            account_username: 4399 用户名。
            account_password: 4399 密码。

        Returns:
            sauth_json 字符串 (JSON 序列化), 登录失败返回 None。
        """
        logger.info(f"开始刷新 sauth_json (4399 账号: {account_username})")
        db = await self._get_db()

        client = Login4399OAuth2()
        try:
            result = await client.login(account_username, account_password)
            if result is None:
                logger.error(f"4399 登录失败 (账号: {account_username})")
                await self._log_refresh(
                    db, success=False,
                    message=f"4399 登录失败 (账号: {account_username})",
                    username=account_username,
                )
                return None

            # 使用 build_sauth_json 显式构建 sauth_json (与 Login4399OAuth2
            # 内部一致, 此处显式调用以明确依赖)。
            sauth_dict = build_sauth_json(result.uid, result.sessionid)
            # 包装成 {"sauth_json": "<json>"} 格式 (与 generate_sauth_json 一致)
            sauth_str = json.dumps(
                {"sauth_json": json.dumps(sauth_dict, ensure_ascii=False)},
                ensure_ascii=False,
            )

            # 更新账号记录: uid / sauth_json / last_refresh_at, 恢复 active
            account = await db.get_sauth_account_by_username(account_username)
            if account:
                await db.update_sauth_account_refresh(
                    account["id"], result.uid, sauth_str
                )

            await self._log_refresh(
                db, success=True,
                message=(
                    f"sauth_json 刷新成功 (账号: {account_username}, "
                    f"uid: {result.uid})"
                ),
                username=account_username,
                details={"uid": result.uid},
            )
            return sauth_str

        except Exception as e:  # noqa: BLE001
            logger.exception(
                f"刷新 sauth_json 异常 (账号: {account_username}): {e}"
            )
            await self._log_refresh(
                db, success=False,
                message=(
                    f"刷新 sauth_json 异常 (账号: {account_username}): {e}"
                ),
                username=account_username,
            )
            return None
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 账号池管理
    # ------------------------------------------------------------------
    async def add_4399_account(self, username: str, password: str) -> bool:
        """添加一个 4399 账号到账号池。

        若用户名已存在则更新密码并恢复为 active 状态。

        Returns:
            True 表示新增, False 表示更新了已存在的账号。
        """
        db = await self._get_db()
        existing = await db.get_sauth_account_by_username(username)
        if existing:
            await db.update_sauth_account_password(existing["id"], password)
            await db.update_sauth_account_status(existing["id"], STATUS_ACTIVE)
            logger.info(f"4399 账号已存在, 已更新密码: {username}")
            return False

        account_id = f"sa_{uuid.uuid4().hex[:12]}"
        await db.add_sauth_account(account_id, username, password)
        logger.info(f"已添加 4399 账号: {username} (id={account_id})")
        return True

    async def list_4399_accounts(self) -> list:
        """列出所有存储的 4399 账号 (不含密码)。"""
        db = await self._get_db()
        rows = await db.list_sauth_accounts()
        return [
            {
                "id": r["id"],
                "username": r["username"],
                "uid": r["uid"],
                "status": r["status"],
                "last_refresh_at": r["last_refresh_at"],
                "has_sauth": bool(r["sauth_json"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    async def delete_4399_account(self, account_id: str) -> bool:
        """删除指定 4399 账号。"""
        db = await self._get_db()
        success = await db.delete_sauth_account(account_id)
        if success:
            logger.info(f"已删除 4399 账号 (id={account_id})")
        return success

    async def get_4399_account(self, account_id: str) -> Optional[dict]:
        """获取单个 4399 账号 (含密码, 供测试使用)。"""
        db = await self._get_db()
        row = await db.get_sauth_account(account_id)
        if row is None:
            return None
        return dict(row)

    async def test_4399_account(
        self, username: str, password: str
    ) -> dict:
        """测试 4399 账号能否登录。

        Returns:
            ``{"success": bool, "uid": str, "message": str}``
        """
        logger.info(f"测试 4399 账号登录: {username}")
        client = Login4399OAuth2()
        try:
            result = await client.login(username, password)
            if result is None:
                return {
                    "success": False,
                    "uid": "",
                    "message": "登录失败 (账号/密码错误或验证码识别失败)",
                }
            return {
                "success": True,
                "uid": result.uid,
                "message": "登录成功",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception(f"测试 4399 账号异常: {username}")
            return {
                "success": False,
                "uid": "",
                "message": f"登录异常: {e}",
            }
        finally:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    async def _get_db(self):
        """惰性获取数据库单例。"""
        from app.database import get_db
        return await get_db()

    async def _log_refresh(
        self,
        db,
        *,
        success: bool,
        message: str,
        username: str = "",
        details: Optional[dict] = None,
    ) -> None:
        """记录刷新日志到数据库。"""
        try:
            await db.add_log(
                target_type="system",
                target_id="sauth_refresh",
                level="success" if success else "error",
                message=message,
                details=json.dumps(
                    {"username": username, **(details or {})},
                    ensure_ascii=False,
                ),
                created_by="system",
            )
        except Exception:  # noqa: BLE001
            logger.debug("记录 sauth_refresh 日志失败", exc_info=True)


#: 全局单例 (与 nv1_manager 风格一致)
sauth_refresher: SauthRefresher = SauthRefresher()


__all__ = [
    "SauthRefresher",
    "sauth_refresher",
    "SAUTH_CACHE_TTL",
]
