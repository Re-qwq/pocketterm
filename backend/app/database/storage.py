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
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    balance         REAL NOT NULL DEFAULT 0,
                    email           TEXT NOT NULL DEFAULT '',
                    avatar          TEXT NOT NULL DEFAULT '',
                    max_storage     INTEGER NOT NULL DEFAULT 524288
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

                -- 公告表
                CREATE TABLE IF NOT EXISTS announcements (
                    announcement_id      TEXT PRIMARY KEY,
                    title                TEXT NOT NULL,
                    content              TEXT NOT NULL,
                    created_by           TEXT NOT NULL,
                    created_by_username  TEXT NOT NULL,
                    created_at           REAL NOT NULL,
                    updated_at           REAL,
                    pinned               INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_announcements_created ON announcements(created_at);

                -- 公告评论表
                CREATE TABLE IF NOT EXISTS announcement_comments (
                    comment_id      TEXT PRIMARY KEY,
                    announcement_id TEXT NOT NULL,
                    user_id         TEXT NOT NULL,
                    username        TEXT NOT NULL,
                    content         TEXT NOT NULL,
                    created_at      REAL NOT NULL,
                    FOREIGN KEY (announcement_id) REFERENCES announcements(announcement_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ann_comments_ann ON announcement_comments(announcement_id);
                CREATE INDEX IF NOT EXISTS idx_ann_comments_user ON announcement_comments(user_id);

                -- 公告反应表 (点赞 / 点踩)
                CREATE TABLE IF NOT EXISTS announcement_reactions (
                    reaction_id     TEXT PRIMARY KEY,
                    announcement_id TEXT NOT NULL,
                    user_id         TEXT NOT NULL,
                    reaction_type   TEXT NOT NULL,  -- 'like' or 'dislike'
                    created_at      REAL NOT NULL,
                    UNIQUE(announcement_id, user_id),  -- 每个用户对每条公告只能有一个反应
                    FOREIGN KEY (announcement_id) REFERENCES announcements(announcement_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ann_reactions_ann ON announcement_reactions(announcement_id);
                CREATE INDEX IF NOT EXISTS idx_ann_reactions_user ON announcement_reactions(user_id);

                -- 4399 账号池表 (sauth_json 自动刷新)
                CREATE TABLE IF NOT EXISTS sauth_accounts (
                    id              TEXT PRIMARY KEY,
                    username        TEXT NOT NULL UNIQUE,
                    password        TEXT NOT NULL,
                    uid             TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'active',
                    last_refresh_at REAL,
                    sauth_json      TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sauth_accounts_status ON sauth_accounts(status);

                -- 商店商品表
                CREATE TABLE IF NOT EXISTS shop_products (
                    product_id      TEXT PRIMARY KEY,
                    category        TEXT NOT NULL,
                    name            TEXT NOT NULL,
                    description     TEXT NOT NULL DEFAULT '',
                    price           REAL NOT NULL DEFAULT 0,
                    duration_days   REAL,
                    card_type       TEXT NOT NULL DEFAULT '',
                    file_path       TEXT NOT NULL DEFAULT '',
                    status          TEXT NOT NULL DEFAULT 'active',
                    created_by      TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shop_products_category ON shop_products(category);
                CREATE INDEX IF NOT EXISTS idx_shop_products_status ON shop_products(status);

                -- 商店订单表
                CREATE TABLE IF NOT EXISTS shop_orders (
                    order_id        TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    product_id      TEXT NOT NULL,
                    product_name    TEXT NOT NULL DEFAULT '',
                    price           REAL NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL DEFAULT 'completed',
                    card_key        TEXT NOT NULL DEFAULT '',
                    file_id         TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_shop_orders_user ON shop_orders(user_id);
                CREATE INDEX IF NOT EXISTS idx_shop_orders_created ON shop_orders(created_at);

                -- 用户文件表
                CREATE TABLE IF NOT EXISTS user_files (
                    file_id         TEXT PRIMARY KEY,
                    user_id         TEXT NOT NULL,
                    category        TEXT NOT NULL,
                    name            TEXT NOT NULL,
                    description     TEXT NOT NULL DEFAULT '',
                    price           REAL NOT NULL DEFAULT 0,
                    file_path       TEXT NOT NULL,
                    file_size       INTEGER NOT NULL DEFAULT 0,
                    status          TEXT NOT NULL DEFAULT 'pending',
                    reject_reason   TEXT NOT NULL DEFAULT '',
                    download_count  INTEGER NOT NULL DEFAULT 0,
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_user_files_user ON user_files(user_id);
                CREATE INDEX IF NOT EXISTS idx_user_files_category ON user_files(category);
                CREATE INDEX IF NOT EXISTS idx_user_files_status ON user_files(status);

                -- 邮箱验证码表
                CREATE TABLE IF NOT EXISTS email_verifications (
                    id              TEXT PRIMARY KEY,
                    email           TEXT NOT NULL,
                    code            TEXT NOT NULL,
                    expires_at      REAL NOT NULL,
                    used            INTEGER NOT NULL DEFAULT 0,
                    created_at      REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_email_verif_email ON email_verifications(email);
            """)
            await self._conn.commit()

            # -- 系统设置表 (持久化 NovaBuilder 凭据等) --
            await self._conn.execute("""
                CREATE TABLE IF NOT EXISTS system_settings (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    updated_at  REAL NOT NULL
                );
            """)
            await self._conn.commit()

            # -- 数据库迁移: 为旧版 users 表补充 must_change_password 列 ----
            # CREATE TABLE IF NOT EXISTS 不会为已存在的表添加新列，因此需要
            # 手动检查并执行 ALTER TABLE。SQLite 没有直接的 "ADD COLUMN IF
            # NOT EXISTS"，这里通过 pragma table_info 检查列是否存在。
            await self._migrate_users_table()
            await self._migrate_announcements_table()
            await self._migrate_sauth_accounts_table()
            await self._migrate_users_v2_table()
        await self._migrate_bot_last_error_table()

            # -- 初始化默认商店商品 (仅在表为空时插入) -------------------
            await self._init_default_shop_products()

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

    async def _migrate_announcements_table(self) -> None:
        """执行 announcements 表的增量迁移: 补充 pinned 列。

        旧版数据库不存在该列时通过 ``ALTER TABLE`` 补充, 默认值为 0 (未置顶)。
        已存在则跳过 (通过 try/except 捕获 "duplicate column" 错误)。
        """
        try:
            await self.conn.execute(
                "ALTER TABLE announcements ADD COLUMN pinned INTEGER DEFAULT 0"
            )
            await self.conn.commit()
            logger.info("announcements 表迁移: 已添加 pinned 列")
        except aiosqlite.OperationalError as e:
            # 列已存在时 SQLite 抛出 "duplicate column name: pinned"
            logger.debug("announcements 表: pinned 列已存在, 跳过迁移 (%s)", e)

    async def _migrate_sauth_accounts_table(self) -> None:
        """执行 sauth_accounts 表迁移: 确保表存在。

        旧版数据库不存在该表时通过 ``CREATE TABLE IF NOT EXISTS`` 创建。
        表结构与 :meth:`init_db` 中的定义一致, 已存在则跳过。
        """
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS sauth_accounts (
                id              TEXT PRIMARY KEY,
                username        TEXT NOT NULL UNIQUE,
                password        TEXT NOT NULL,
                uid             TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'active',
                last_refresh_at REAL,
                sauth_json      TEXT NOT NULL DEFAULT '',
                created_at      REAL NOT NULL
            );
        """)
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sauth_accounts_status "
            "ON sauth_accounts(status)"
        )
        await self.conn.commit()
        logger.debug("sauth_accounts 表迁移检查完成")

    async def _migrate_bot_last_error_table(self) -> None:
        """迁移: 为 bot_instances 表添加 last_error 列。"""
        cursor = await self.conn.execute("PRAGMA table_info(bot_instances)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "last_error" not in columns:
            await self.conn.execute(
                "ALTER TABLE bot_instances ADD COLUMN last_error TEXT NOT NULL DEFAULT ''"
            )
            await self.conn.commit()
            logger.info("bot_instances 表已添加 last_error 列")

    async def _migrate_users_v2_table(self) -> None:
        """v2 迁移: 为 users 表补充 balance / email / avatar / max_storage 列。"""
        cursor = await self.conn.execute("PRAGMA table_info(users)")
        columns = {row["name"] for row in await cursor.fetchall()}
        new_cols = {
            "balance": "REAL NOT NULL DEFAULT 0",
            "email": "TEXT NOT NULL DEFAULT ''",
            "avatar": "TEXT NOT NULL DEFAULT ''",
            "max_storage": "INTEGER NOT NULL DEFAULT 524288",
        }
        for col_name, col_def in new_cols.items():
            if col_name not in columns:
                await self.conn.execute(
                    f"ALTER TABLE users ADD COLUMN {col_name} {col_def}"
                )
                logger.info("users 表迁移: 已添加 %s 列", col_name)
        await self.conn.commit()

    async def _init_default_shop_products(self) -> None:
        """初始化默认商店商品 (仅在 shop_products 表为空时插入)。

        默认商品:
            - 日卡面板 (5余额, 1天)
            - 周卡面板 (16余额, 7天)
            - 月卡面板 (35余额, 30天)
            - 永久注册卡 (3余额, 永久)
        """
        import time as _time

        count_row = await (await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM shop_products"
        )).fetchone()
        if count_row and count_row["cnt"] > 0:
            return  # 已有商品, 不重复插入

        now = _time.time()
        defaults = [
            ("prod_panel_day",   "panel_card",    "日卡面板",   "面板日卡,有效期1天",       5.0,  1,    "panel"),
            ("prod_panel_week",  "panel_card",    "周卡面板",   "面板周卡,有效期7天",       16.0, 7,    "panel"),
            ("prod_panel_month", "panel_card",    "月卡面板",   "面板月卡,有效期30天",       35.0, 30,   "panel"),
            ("prod_reg_perm",    "register_card", "永久注册卡", "永久可用注册卡密",           3.0,  None, "register"),
        ]
        for pid, cat, name, desc, price, days, ctype in defaults:
            await self.conn.execute(
                """INSERT OR IGNORE INTO shop_products
                   (product_id, category, name, description, price,
                    duration_days, card_type, file_path, status, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, '', 'active', 'system', ?)""",
                (pid, cat, name, desc, price, days, ctype, now),
            )
        await self.conn.commit()
        logger.info("已初始化 %d 个默认商店商品", len(defaults))

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
        email: str = "",
        avatar: str = "",
    ) -> str:
        """创建用户，返回 user_id。

        Args:
            must_change_password: 为 ``True`` 时用户首次登录后将被要求修改
                密码 (默认管理员账号应设为 ``True``)。
            email: 用户邮箱 (可选，QQ 邮箱注册时写入)。
            avatar: 用户头像 URL (可选，QQ 邮箱注册时自动生成)。
        """
        user_id = f"u_{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            """INSERT INTO users
               (user_id, username, password_hash, role, status, created_at,
                expire_at, created_by, must_change_password, email, avatar)
               VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)""",
            (
                user_id, username, password_hash, role, _now(),
                expire_at, created_by, 1 if must_change_password else 0,
                email, avatar,
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
        """删除用户，并清理其关联数据。

        - 删除用户拥有的面板 (delete_panel 会级联删除面板下的机器人)。
        - 清理日志记录中对用户的引用 (保留日志历史，仅将 created_by 置空)。
        - 保留卡密记录 (历史)，但清除 bound_user_id 引用。
        """
        # 1. 删除用户的面板 (含面板下的机器人)
        panels = await self.list_panels_by_user(user_id)
        for panel in panels:
            await self.delete_panel(panel["panel_id"])

        # 2. 清理日志记录中对用户的引用 (保留日志，置空 created_by)
        await self.conn.execute(
            "UPDATE logs SET created_by = '' WHERE created_by = ?", (user_id,)
        )
        # 3. 保留卡密记录 (历史)，清除 bound_user_id 引用
        await self.conn.execute(
            "UPDATE card_keys SET bound_user_id = '' WHERE bound_user_id = ?",
            (user_id,),
        )

        # 4. 删除用户本身
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

    async def update_bot_status(self, bot_id: str, status: str, error: str = "") -> None:
        if error:
            await self.conn.execute(
                "UPDATE bot_instances SET status = ?, last_error = ? WHERE bot_id = ?",
                (status, error, bot_id),
            )
        else:
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
    # 4399 账号池管理 (sauth_json 自动刷新)
    # ========================================================================

    async def add_sauth_account(
        self, account_id: str, username: str, password: str
    ) -> None:
        """添加一个 4399 账号到 sauth_accounts 表。"""
        await self.conn.execute(
            """INSERT INTO sauth_accounts
               (id, username, password, uid, status, last_refresh_at,
                sauth_json, created_at)
               VALUES (?, ?, ?, '', 'active', NULL, '', ?)""",
            (account_id, username, password, _now()),
        )
        await self.conn.commit()

    async def get_sauth_account(self, account_id: str) -> Optional[aiosqlite.Row]:
        """根据 id 获取 4399 账号。"""
        return await (await self.conn.execute(
            "SELECT * FROM sauth_accounts WHERE id = ?", (account_id,)
        )).fetchone()

    async def get_sauth_account_by_username(
        self, username: str
    ) -> Optional[aiosqlite.Row]:
        """根据用户名获取 4399 账号。"""
        return await (await self.conn.execute(
            "SELECT * FROM sauth_accounts WHERE username = ?", (username,)
        )).fetchone()

    async def list_sauth_accounts(self) -> list[aiosqlite.Row]:
        """列出所有 4399 账号 (按创建时间倒序)。"""
        return await (await self.conn.execute(
            "SELECT * FROM sauth_accounts ORDER BY created_at DESC"
        )).fetchall()

    async def get_active_sauth_accounts(self) -> list[aiosqlite.Row]:
        """列出所有状态为 active 的 4399 账号 (按创建时间正序, 用于轮询)。"""
        return await (await self.conn.execute(
            "SELECT * FROM sauth_accounts WHERE status = 'active' "
            "ORDER BY created_at ASC"
        )).fetchall()

    async def update_sauth_account_status(
        self, account_id: str, status: str
    ) -> None:
        """更新 4399 账号状态。"""
        await self.conn.execute(
            "UPDATE sauth_accounts SET status = ? WHERE id = ?",
            (status, account_id),
        )
        await self.conn.commit()

    async def update_sauth_account_password(
        self, account_id: str, password: str
    ) -> None:
        """更新 4399 账号密码。"""
        await self.conn.execute(
            "UPDATE sauth_accounts SET password = ? WHERE id = ?",
            (password, account_id),
        )
        await self.conn.commit()

    async def update_sauth_account_refresh(
        self, account_id: str, uid: str, sauth_json: str
    ) -> None:
        """更新 4399 账号的刷新结果 (uid / sauth_json / last_refresh_at)。

        同时将状态恢复为 active (登录成功即代表账号可用)。
        """
        await self.conn.execute(
            "UPDATE sauth_accounts SET uid = ?, sauth_json = ?, "
            "last_refresh_at = ?, status = 'active' WHERE id = ?",
            (uid, sauth_json, _now(), account_id),
        )
        await self.conn.commit()

    async def delete_sauth_account(self, account_id: str) -> bool:
        """删除 4399 账号, 返回是否删除了行。"""
        cur = await self.conn.execute(
            "DELETE FROM sauth_accounts WHERE id = ?", (account_id,)
        )
        await self.conn.commit()
        return cur.rowcount > 0

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

    # ========================================================================
    # 公告管理
    # ========================================================================

    async def create_announcement(
        self,
        title: str,
        content: str,
        user_id: str,
        username: str,
        pinned: bool = False,
    ) -> str:
        """创建公告，返回 announcement_id。"""
        announcement_id = f"ann_{uuid.uuid4().hex[:12]}"
        now = _now()
        await self.conn.execute(
            """INSERT INTO announcements
               (announcement_id, title, content, created_by, created_by_username,
                created_at, updated_at, pinned)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (announcement_id, title, content, user_id, username, now, now,
             1 if pinned else 0),
        )
        await self.conn.commit()
        return announcement_id

    async def list_announcements(self) -> list[dict]:
        """列出所有公告 (置顶优先, 然后按创建时间倒序)，每条包含 like_count 与 dislike_count。"""
        rows = await (await self.conn.execute(
            """SELECT a.*,
                      (SELECT COUNT(*) FROM announcement_reactions r
                       WHERE r.announcement_id = a.announcement_id
                         AND r.reaction_type = 'like') AS like_count,
                      (SELECT COUNT(*) FROM announcement_reactions r
                       WHERE r.announcement_id = a.announcement_id
                         AND r.reaction_type = 'dislike') AS dislike_count
               FROM announcements a
               ORDER BY a.pinned DESC, a.created_at DESC"""
        )).fetchall()
        return [dict(r) for r in rows]

    async def get_announcement(self, announcement_id: str) -> Optional[aiosqlite.Row]:
        """根据 announcement_id 获取单条公告。"""
        return await (await self.conn.execute(
            "SELECT * FROM announcements WHERE announcement_id = ?",
            (announcement_id,),
        )).fetchone()

    async def set_announcement_pin(self, announcement_id: str, pinned: bool) -> bool:
        """设置公告置顶状态, 返回是否更新了行。"""
        cur = await self.conn.execute(
            "UPDATE announcements SET pinned = ?, updated_at = ? WHERE announcement_id = ?",
            (1 if pinned else 0, _now(), announcement_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def delete_announcement(self, announcement_id: str) -> bool:
        """删除公告及其所有评论与反应 (手动级联删除)。

        Returns:
            是否删除了公告行。
        """
        # 手动级联删除评论与反应 (不依赖 PRAGMA foreign_keys)
        await self.conn.execute(
            "DELETE FROM announcement_reactions WHERE announcement_id = ?",
            (announcement_id,),
        )
        await self.conn.execute(
            "DELETE FROM announcement_comments WHERE announcement_id = ?",
            (announcement_id,),
        )
        cur = await self.conn.execute(
            "DELETE FROM announcements WHERE announcement_id = ?",
            (announcement_id,),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def add_comment(
        self,
        announcement_id: str,
        user_id: str,
        username: str,
        content: str,
    ) -> str:
        """添加评论，返回 comment_id。"""
        comment_id = f"cmt_{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            """INSERT INTO announcement_comments
               (comment_id, announcement_id, user_id, username, content, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (comment_id, announcement_id, user_id, username, content, _now()),
        )
        await self.conn.commit()
        return comment_id

    async def list_comments(self, announcement_id: str) -> list[dict]:
        """列出指定公告的评论 (按时间正序)。"""
        rows = await (await self.conn.execute(
            "SELECT * FROM announcement_comments WHERE announcement_id = ? "
            "ORDER BY created_at ASC",
            (announcement_id,),
        )).fetchall()
        return [dict(r) for r in rows]

    async def delete_comment(self, comment_id: str) -> bool:
        """删除评论，返回是否删除了行。"""
        cur = await self.conn.execute(
            "DELETE FROM announcement_comments WHERE comment_id = ?",
            (comment_id,),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_comment(self, comment_id: str) -> Optional[aiosqlite.Row]:
        """根据 comment_id 获取单条评论。"""
        return await (await self.conn.execute(
            "SELECT * FROM announcement_comments WHERE comment_id = ?",
            (comment_id,),
        )).fetchone()

    async def get_user_reaction(
        self, announcement_id: str, user_id: str
    ) -> Optional[aiosqlite.Row]:
        """获取指定用户对指定公告的反应 (无则 None)。"""
        return await (await self.conn.execute(
            "SELECT * FROM announcement_reactions "
            "WHERE announcement_id = ? AND user_id = ?",
            (announcement_id, user_id),
        )).fetchone()

    async def set_reaction(
        self, announcement_id: str, user_id: str, reaction_type: str
    ) -> str:
        """设置用户对公告的反应 (upsert)。

        利用 ``UNIQUE(announcement_id, user_id)`` 约束实现 upsert: 已存在
        反应时更新 ``reaction_type`` 与 ``created_at``，否则插入新行。返回
        最终生效的 reaction_id。
        """
        reaction_id = f"rxn_{uuid.uuid4().hex[:12]}"
        await self.conn.execute(
            """INSERT INTO announcement_reactions
               (reaction_id, announcement_id, user_id, reaction_type, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(announcement_id, user_id) DO UPDATE SET
                 reaction_type = excluded.reaction_type,
                 created_at = excluded.created_at""",
            (reaction_id, announcement_id, user_id, reaction_type, _now()),
        )
        await self.conn.commit()
        row = await (await self.conn.execute(
            "SELECT reaction_id FROM announcement_reactions "
            "WHERE announcement_id = ? AND user_id = ?",
            (announcement_id, user_id),
        )).fetchone()
        return row["reaction_id"] if row else reaction_id

    async def remove_reaction(
        self, announcement_id: str, user_id: str
    ) -> bool:
        """移除用户对公告的反应，返回是否删除了行。"""
        cur = await self.conn.execute(
            "DELETE FROM announcement_reactions "
            "WHERE announcement_id = ? AND user_id = ?",
            (announcement_id, user_id),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_reaction_counts(self, announcement_id: str) -> dict:
        """获取公告的点赞/点踩计数。

        Returns:
            ``{"likes": int, "dislikes": int}``
        """
        like_row = await (await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM announcement_reactions "
            "WHERE announcement_id = ? AND reaction_type = 'like'",
            (announcement_id,),
        )).fetchone()
        dislike_row = await (await self.conn.execute(
            "SELECT COUNT(*) as cnt FROM announcement_reactions "
            "WHERE announcement_id = ? AND reaction_type = 'dislike'",
            (announcement_id,),
        )).fetchone()
        return {
            "likes": like_row["cnt"] if like_row else 0,
            "dislikes": dislike_row["cnt"] if dislike_row else 0,
        }

    async def list_announcement_logs(self) -> list[dict]:
        """获取公告活动日志 (管理员用)。

        汇总反应与评论，并关联公告标题与用户名，按时间倒序返回。每条记录
        包含 ``type`` 字段 (``reaction`` 或 ``comment``)，标识活动类型。
        """
        reactions = await (await self.conn.execute(
            """SELECT r.reaction_id, r.announcement_id, a.title AS announcement_title,
                      r.user_id, u.username, r.reaction_type, r.created_at
               FROM announcement_reactions r
               LEFT JOIN announcements a ON r.announcement_id = a.announcement_id
               LEFT JOIN users u ON r.user_id = u.user_id
               ORDER BY r.created_at DESC"""
        )).fetchall()
        comments = await (await self.conn.execute(
            """SELECT c.comment_id, c.announcement_id, a.title AS announcement_title,
                      c.user_id, c.username, c.content, c.created_at
               FROM announcement_comments c
               LEFT JOIN announcements a ON c.announcement_id = a.announcement_id
               ORDER BY c.created_at DESC"""
        )).fetchall()

        logs: list[dict] = []
        for r in reactions:
            logs.append({
                "type": "reaction",
                "reaction_id": r["reaction_id"],
                "announcement_id": r["announcement_id"],
                "announcement_title": r["announcement_title"],
                "user_id": r["user_id"],
                "username": r["username"],
                "reaction_type": r["reaction_type"],
                "created_at": r["created_at"],
            })
        for c in comments:
            logs.append({
                "type": "comment",
                "comment_id": c["comment_id"],
                "announcement_id": c["announcement_id"],
                "announcement_title": c["announcement_title"],
                "user_id": c["user_id"],
                "username": c["username"],
                "content": c["content"],
                "created_at": c["created_at"],
            })

        logs.sort(key=lambda x: x["created_at"], reverse=True)
        return logs

    # ========================================================================
    # 系统设置 (持久化 NovaBuilder 凭据等)
    # ========================================================================

    async def get_setting(self, key: str) -> Optional[str]:
        """获取系统设置值, 不存在返回 None。"""
        row = await (await self.conn.execute(
            "SELECT value FROM system_settings WHERE key = ?", (key,)
        )).fetchone()
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        """设置系统设置值 (upsert)。"""
        await self.conn.execute(
            "INSERT INTO system_settings (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _now()),
        )
        await self.conn.commit()

    async def delete_setting(self, key: str) -> bool:
        """删除系统设置, 返回是否删除了行。"""
        cur = await self.conn.execute(
            "DELETE FROM system_settings WHERE key = ?", (key,)
        )
        await self.conn.commit()
        return cur.rowcount > 0


# 全局单例
_db: Optional[Database] = None
_db_lock: Optional[asyncio.Lock] = None


async def get_db() -> Database:
    """获取数据库单例 (协程安全)。

    使用模块级锁串行化初始化，确保:

    - 并发调用时只有一个协程执行 ``init_db``，避免重复创建连接。
    - 在 ``init_db`` 完成之前不会返回未初始化的实例。
    """
    global _db, _db_lock
    # 快速路径: 已初始化直接返回 (无需加锁)
    if _db is not None and _db._initialized:
        return _db
    if _db_lock is None:
        _db_lock = asyncio.Lock()
    async with _db_lock:
        # 双重检查: 等锁期间可能已被其他协程初始化完成
        if _db is not None and _db._initialized:
            return _db
        if _db is None:
            _db = Database()
        # init_db 内部自带幂等保护，确保初始化完成后才返回
        await _db.init_db()
    return _db


async def close_db() -> None:
    """关闭数据库单例。"""
    global _db
    if _db is not None:
        await _db.close()
        _db = None
