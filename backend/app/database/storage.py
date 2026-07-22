"""PocketTerm 多用户系统数据库存储层。

表结构::

    users            用户表
    card_keys        卡密表
    panels           面板表
    bot_instances    机器人实例表
    logs             日志表
    accounts         游戏账号表（从旧表迁移）

所有操作异步，基于 aiosqlite。
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import string
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from ..config import DATA_DIR

logger = logging.getLogger("pocketterm.database")

DEFAULT_DB_PATH: Path = DATA_DIR / "pocketterm.db"

# 卡密字符集（大写字母 + 数字，去除易混淆字符）
_CARD_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _generate_card_key() -> str:
    """生成卡密字符串，格式 XXXX-XXXX。"""
    part1 = "".join(secrets.choice(_CARD_CHARS) for _ in range(4))
    part2 = "".join(secrets.choice(_CARD_CHARS) for _ in range(4))
    return f"{part1}-{part2}"


def _generate_panel_id() -> str:
    """生成面板 ID，格式 PT-xxxxxxxx。"""
    return f"PT-{uuid.uuid4().hex[:8]}"


def _now() -> float:
    return time.time()


class Database:
    """PocketTerm 数据库管理器。"""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or DEFAULT_DB_PATH
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()
        self._initialized = False

    async def init_db(self) -> None:
        """初始化数据库，创建所有表。"""
        async with self._lock:
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self._db_path))
            self._conn.row_factory = aiosqlite.Row

            await self._conn.executescript("""
                -- 用户表
                CREATE TABLE IF NOT EXISTS users (
                    user_id         TEXT PRIMARY KEY,
                    username        TEXT NOT NULL UNIQUE,
                    password_hash   TEXT NOT NULL,
                    role            TEXT NOT NULL DEFAULT 'user',
                    status          TEXT NOT NULL DEFAULT 'active',
                    created_at      REAL NOT NULL,
                    last_login_at   REAL,
                    last_login_ip   TEXT NOT NULL DEFAULT '',
                    expire_at       REAL,
                    created_by      TEXT NOT NULL DEFAULT '',
                    must_change_password INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);

                -- 卡密表
                CREATE TABLE IF NOT EXISTS card_keys (
                    card_id         TEXT PRIMARY KEY,
                    key             TEXT NOT NULL UNIQUE,
                    key_type        TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'unused',
                    duration_days   INTEGER,
                    bound_user_id   TEXT NOT NULL DEFAULT '',
                    bound_panel_id  TEXT NOT NULL DEFAULT '',
                    created_by      TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL,
                    used_at         REAL,
                    expires_at      REAL
                );
                CREATE INDEX IF NOT EXISTS idx_card_keys_key ON card_keys(key);
                CREATE INDEX IF NOT EXISTS idx_card_keys_type ON card_keys(key_type);
                CREATE INDEX IF NOT EXISTS idx_card_keys_status ON card_keys(status);
                CREATE INDEX IF NOT EXISTS idx_card_keys_created_by ON card_keys(created_by);

                -- 面板表
                CREATE TABLE IF NOT EXISTS panels (
                    panel_id        TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    name            TEXT NOT NULL,
                    status          TEXT NOT NULL DEFAULT 'active',
                    created_at      REAL NOT NULL,
                    expire_at       REAL,
                    created_by_card TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_panels_user ON panels(user_id);
                CREATE INDEX IF NOT EXISTS idx_panels_status ON panels(status);

                -- 机器人实例表
                CREATE TABLE IF NOT EXISTS bot_instances (
                    bot_id          TEXT PRIMARY KEY,
                    panel_id       TEXT NOT NULL,
                    name            TEXT NOT NULL,
                    account_id     TEXT NOT NULL DEFAULT '',
                    server_code    TEXT NOT NULL DEFAULT '',
                    server_type    TEXT NOT NULL DEFAULT 'rental',
                    access_point_type TEXT NOT NULL DEFAULT 'neomega',
                    status         TEXT NOT NULL DEFAULT 'stopped',
                    created_at     REAL NOT NULL,
                    last_started_at REAL,
                    config         TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_bots_panel ON bot_instances(panel_id);
                CREATE INDEX IF NOT EXISTS idx_bots_status ON bot_instances(status);

                -- 日志表
                CREATE TABLE IF NOT EXISTS logs (
                    log_id          TEXT PRIMARY KEY,
                    target_type     TEXT NOT NULL,
                    target_id       TEXT NOT NULL,
                    level           TEXT NOT NULL,
                    message         TEXT NOT NULL,
                    details         TEXT NOT NULL DEFAULT '',
                    ip              TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL,
                    created_by      TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_logs_target ON logs(target_type, target_id);
                CREATE INDEX IF NOT EXISTS idx_logs_level ON logs(level);
                CREATE INDEX IF NOT EXISTS idx_logs_created ON logs(created_at);

                -- 游戏账号表
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id      TEXT PRIMARY KEY,
                    username        TEXT NOT NULL,
                    password        TEXT NOT NULL DEFAULT '',
                    player_name     TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'active',
                    created_at      REAL NOT NULL,
                    last_used_at    REAL,
                    ban_count       INTEGER NOT NULL DEFAULT 0,
                    notes           TEXT NOT NULL DEFAULT '',
                    metadata        TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
            """)
            await self._conn.commit()

            # -- 数据库迁移: 为旧版 users 表补充 must_change_password 列 ----
            # CREATE TABLE IF NOT EXISTS 不会为已存在的表添加新列，因此需要
            # 手动检查并执行 ALTER TABLE。SQLite 没有直接的 "ADD COLUMN IF
            # NOT EXISTS"，这里通过 pragma table_info 检查列是否存在。
            await self._migrate_users_table()

            self._initialized = True
            logger.info("数据库初始化完成: %s", self._db_path)

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn:
            await self._conn.close()
            self._conn = None
        self._initialized = False

    async def _migrate_users_table(self) -> None:
        """执行 users 表的增量迁移。

        目前处理:

            - ``must_change_password`` 列: 旧版数据库不存在该列时通过
              ``ALTER TABLE`` 补充，默认值为 0 (False)。已存在则跳过。
        """
        cursor = await self.conn.execute("PRAGMA table_info(users)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "must_change_password" not in columns:
            await self.conn.execute(
                "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
            )
            await self.conn.commit()
            logger.info("users 表迁移: 已添加 must_change_password 列")

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("数据库未初始化，请先调用 init_db()")
        return self._conn

    # ========================================================================
    # 用户管理
    # ========================================================================

    async def create_user(
        self,
        username: str,
        password_hash: str,
        role: str = "user",
        created_by: str = "",
        expire_at: Optional[float] = None,
        must_change_password: bool = False,
    ) -> str:
        """创建用户，返回 user_id。

        Args:
            must_change_password: 为 ``True`` 时用户首次登录后将被要求修改
                密码 (默认管理员账号应设为 ``True``)。
        """
        user_id = f"u_{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            """INSERT INTO users
               (user_id, username, password_hash, role, status, created_at,
                expire_at, created_by, must_change_password)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)""",
            (
                user_id, username, password_hash, role, _now(),
                expire_at, created_by, 1 if must_change_password else 0,
            ),
        )
        await self.conn.commit()
        return user_id

    async def get_user_by_username(self, username: str) -> Optional[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        )).fetchone()

    async def get_user_by_id(self, user_id: str) -> Optional[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user_id,)
        )).fetchone()

    async def update_user_login(self, user_id: str, ip: str = "") -> None:
        await self.conn.execute(
            "UPDATE users SET last_login_at = ?, last_login_ip = ? WHERE user_id = ?",
            (_now(), ip, user_id),
        )
        await self.conn.commit()

    async def update_user_status(self, user_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE users SET status = ? WHERE user_id = ?", (status, user_id)
        )
        await self.conn.commit()

    async def update_user_role(self, user_id: str, role: str) -> None:
        await self.conn.execute(
            "UPDATE users SET role = ? WHERE user_id = ?", (role, user_id)
        )
        await self.conn.commit()

    async def update_user_password(
        self, user_id: str, password_hash: str, clear_must_change: bool = True
    ) -> None:
        """更新用户密码哈希。

        Args:
            password_hash: 新的密码哈希字符串。
            clear_must_change: 为 ``True`` (默认) 时同时清除
                ``must_change_password`` 标记，表示用户已完成改密。
        """
        if clear_must_change:
            await self.conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = 0 "
                "WHERE user_id = ?",
                (password_hash, user_id),
            )
        else:
            await self.conn.execute(
                "UPDATE users SET password_hash = ? WHERE user_id = ?",
                (password_hash, user_id),
            )
        await self.conn.commit()

    async def list_users(self) -> list[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC"
        )).fetchall()

    async def delete_user(self, user_id: str) -> None:
        await self.conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await self.conn.commit()

    async def count_admins(self) -> int:
        row = await (await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM users WHERE role IN ('superadmin', 'admin')"
        )).fetchone()
        return row["cnt"] if row else 0

    # ========================================================================
    # 卡密管理
    # ========================================================================

    async def create_card_key(
        self,
        key_type: str,
        duration_days: Optional[float],
        created_by: str,
        expires_at: Optional[float] = None,
    ) -> tuple[str, str]:
        """创建卡密，返回 (card_id, key)。"""
        card_id = f"c_{uuid.uuid4().hex[:12]}"
        key = _generate_card_key()
        # 确保唯一
        while await self.get_card_by_key(key) is not None:
            key = _generate_card_key()
        await self.conn.execute(
            """INSERT INTO card_keys (card_id, key, key_type, status, duration_days, created_by, created_at, expires_at)
               VALUES (?, ?, ?, 'unused', ?, ?, ?, ?)""",
            (card_id, key, key_type, duration_days, created_by, _now(), expires_at),
        )
        await self.conn.commit()
        return card_id, key

    async def get_card_by_key(self, key: str) -> Optional[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM card_keys WHERE key = ?", (key,)
        )).fetchone()

    async def get_card_by_id(self, card_id: str) -> Optional[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM card_keys WHERE card_id = ?", (card_id,)
        )).fetchone()

    async def use_card(
        self,
        card_id: str,
        user_id: str = "",
        panel_id: str = "",
    ) -> None:
        """标记卡密已使用。"""
        await self.conn.execute(
            "UPDATE card_keys SET status = 'used', used_at = ?, bound_user_id = ?, bound_panel_id = ? WHERE card_id = ?",
            (_now(), user_id, panel_id, card_id),
        )
        await self.conn.commit()

    async def revoke_card(self, card_id: str) -> None:
        await self.conn.execute(
            "UPDATE card_keys SET status = 'revoked' WHERE card_id = ?", (card_id,)
        )
        await self.conn.commit()

    async def list_cards(
        self,
        created_by: Optional[str] = None,
        key_type: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM card_keys WHERE 1=1"
        params: list[Any] = []
        if created_by:
            sql += " AND created_by = ?"
            params.append(created_by)
        if key_type:
            sql += " AND key_type = ?"
            params.append(key_type)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        return await (await self.conn.execute(sql, params)).fetchall()

    # ========================================================================
    # 面板管理
    # ========================================================================

    async def create_panel(
        self,
        user_id: str,
        name: str,
        created_by_card: str = "",
        expire_at: Optional[float] = None,
    ) -> str:
        """创建面板，返回 panel_id。"""
        panel_id = _generate_panel_id()
        await self.conn.execute(
            """INSERT INTO panels (panel_id, user_id, name, status, created_at, expire_at, created_by_card)
               VALUES (?, ?, ?, 'active', ?, ?, ?)""",
            (panel_id, user_id, name, _now(), expire_at, created_by_card),
        )
        await self.conn.commit()
        return panel_id

    async def get_panel(self, panel_id: str) -> Optional[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM panels WHERE panel_id = ?", (panel_id,)
        )).fetchone()

    async def list_panels_by_user(self, user_id: str) -> list[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM panels WHERE user_id = ? ORDER BY created_at DESC", (user_id,)
        )).fetchall()

    async def list_all_panels(self) -> list[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM panels ORDER BY created_at DESC"
        )).fetchall()

    async def update_panel_status(self, panel_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE panels SET status = ? WHERE panel_id = ?", (status, panel_id)
        )
        await self.conn.commit()

    async def renew_panel(self, panel_id: str, expire_at: Optional[float]) -> None:
        """续期面板。"""
        await self.conn.execute(
            "UPDATE panels SET expire_at = ?, status = 'active' WHERE panel_id = ?",
            (expire_at, panel_id),
        )
        await self.conn.commit()

    async def delete_panel(self, panel_id: str) -> None:
        await self.conn.execute("DELETE FROM bot_instances WHERE panel_id = ?", (panel_id,))
        await self.conn.execute("DELETE FROM panels WHERE panel_id = ?", (panel_id,))
        await self.conn.commit()

    # ========================================================================
    # 机器人实例管理
    # ========================================================================

    async def create_bot_instance(
        self,
        panel_id: str,
        name: str,
        account_id: str = "",
        server_code: str = "",
        server_type: str = "rental",
        access_point_type: str = "neomega",
        config: str = "{}",
    ) -> str:
        bot_id = f"b_{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            """INSERT INTO bot_instances
               (bot_id, panel_id, name, account_id, server_code, server_type, access_point_type, status, created_at, config)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'stopped', ?, ?)""",
            (bot_id, panel_id, name, account_id, server_code, server_type, access_point_type, _now(), config),
        )
        await self.conn.commit()
        return bot_id

    async def get_bot(self, bot_id: str) -> Optional[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM bot_instances WHERE bot_id = ?", (bot_id,)
        )).fetchone()

    async def list_bots_by_panel(self, panel_id: str) -> list[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM bot_instances WHERE panel_id = ? ORDER BY created_at DESC", (panel_id,)
        )).fetchall()

    async def list_all_bots(self) -> list[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM bot_instances ORDER BY created_at DESC"
        )).fetchall()

    async def update_bot_status(self, bot_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE bot_instances SET status = ? WHERE bot_id = ?", (status, bot_id)
        )
        if status == "running":
            await self.conn.execute(
                "UPDATE bot_instances SET last_started_at = ? WHERE bot_id = ?", (_now(), bot_id)
            )
        await self.conn.commit()

    async def update_bot_config(self, bot_id: str, config: str) -> None:
        await self.conn.execute(
            "UPDATE bot_instances SET config = ? WHERE bot_id = ?", (config, bot_id)
        )
        await self.conn.commit()

    async def delete_bot(self, bot_id: str) -> None:
        await self.conn.execute("DELETE FROM bot_instances WHERE bot_id = ?", (bot_id,))
        await self.conn.commit()

    # ========================================================================
    # 日志管理
    # ========================================================================

    async def add_log(
        self,
        target_type: str,
        target_id: str,
        level: str,
        message: str,
        details: str = "",
        ip: str = "",
        created_by: str = "",
    ) -> str:
        log_id = f"l_{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            """INSERT INTO logs (log_id, target_type, target_id, level, message, details, ip, created_at, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (log_id, target_type, target_id, level, message, details, ip, _now(), created_by),
        )
        await self.conn.commit()
        return log_id

    async def list_logs(
        self,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        level: Optional[str] = None,
        limit: int = 100,
    ) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM logs WHERE 1=1"
        params: list[Any] = []
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id:
            sql += " AND target_id = ?"
            params.append(target_id)
        if level:
            sql += " AND level = ?"
            params.append(level)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return await (await self.conn.execute(sql, params)).fetchall()

    # ========================================================================
    # 游戏账号管理（兼容旧接口）
    # ========================================================================

    async def add_account(
        self,
        account_id: str,
        username: str,
        password: str = "",
        player_name: str = "",
        status: str = "active",
        metadata: str = "{}",
    ) -> None:
        await self.conn.execute(
            """INSERT OR REPLACE INTO accounts
               (account_id, username, password, player_name, status, created_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (account_id, username, password, player_name, status, _now(), metadata),
        )
        await self.conn.commit()

    async def get_account(self, account_id: str) -> Optional[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        )).fetchone()

    async def list_accounts(self) -> list[aiosqlite.Row]:
        return await (await self.conn.execute(
            "SELECT * FROM accounts ORDER BY created_at DESC"
        )).fetchall()

    async def update_account_status(self, account_id: str, status: str) -> None:
        await self.conn.execute(
            "UPDATE accounts SET status = ?, last_used_at = ? WHERE account_id = ?",
            (status, _now(), account_id),
        )
        await self.conn.commit()

    async def delete_account(self, account_id: str) -> None:
        await self.conn.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
        await self.conn.commit()

    # ========================================================================
    # 统计/辅助方法
    # ========================================================================

    async def count_cards_by_admin(self, admin_id: str) -> int:
        """统计某管理员创建的卡密数量。"""
        row = await (await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM card_keys WHERE created_by = ?", (admin_id,)
        )).fetchone()
        return row["cnt"] if row else 0

    async def get_panel_for_user(self, panel_id: str, user_id: str) -> Optional[aiosqlite.Row]:
        """获取面板并验证所有权。"""
        return await (await self.conn.execute(
            "SELECT * FROM panels WHERE panel_id = ? AND user_id = ?", (panel_id, user_id)
        )).fetchone()

    async def count_panels_by_user(self, user_id: str) -> int:
        """统计某用户的面板数量。"""
        row = await (await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM panels WHERE user_id = ?", (user_id,)
        )).fetchone()
        return row["cnt"] if row else 0

    async def list_logs_by_creator(
        self, created_by: str, limit: int = 100
    ) -> list[aiosqlite.Row]:
        """列出某管理员的操作日志。"""
        return await (await self.conn.execute(
            "SELECT * FROM logs WHERE created_by = ? ORDER BY created_at DESC LIMIT ?",
            (created_by, limit),
        )).fetchall()

    async def get_expired_panels(self) -> list[aiosqlite.Row]:
        """获取所有已过期但状态仍为 active 的面板。"""
        return await (await self.conn.execute(
            "SELECT * FROM panels WHERE status = 'active' AND expire_at IS NOT NULL AND expire_at < ?",
            (_now(),),
        )).fetchall()


# 全局单例
_db: Optional[Database] = None


async def get_db() -> Database:
    """获取数据库单例。"""
    global _db
    if _db is None:
        _db = Database()
        await _db.init_db()
    return _db


async def close_db() -> None:
    """关闭数据库单例。"""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
