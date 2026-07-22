"""PocketTerm 账号存储层 (SQLite + aiosqlite)

本模块提供账号数据的持久化能力，所有数据库操作均为 **异步**，
基于 :mod:`aiosqlite` 实现。

数据库布局::

    backend/data/accounts.db
        └── accounts 表
              account_id   TEXT PRIMARY KEY   账号 UUID
              username     TEXT               用户名 / 邮箱 / 手机号
              password     TEXT               加密后的密码
              player_name  TEXT               游戏内名称
              status       TEXT               AccountStatus.value
              created_at   REAL               创建时间戳
              last_used_at REAL               最近使用时间戳 (可空)
              ban_count    INTEGER            封禁次数
              notes        TEXT               备注
              metadata     TEXT               JSON 字符串 (token/cookie 等)

线程安全说明:
    :class:`aiosqlite.Connection` 内部使用独立线程串行执行 SQL，
    因此同一连接上的并发 ``await`` 调用会自动序列化，无需额外加锁。
    初始化阶段使用 ``asyncio.Lock`` 保护 ``_initialized`` 标志，避免
    重复建表。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from ..config import DATA_DIR
from .models import AccountStatus, NeteaseAccount

logger = logging.getLogger("pocketterm.account_storage")

#: 默认数据库文件路径: ``backend/data/accounts.db``
DEFAULT_DB_PATH: Path = DATA_DIR / "accounts.db"

#: accounts 表允许写入的列名集合（用于 :meth:`AccountStorage.update_account` 校验）
_ALLOWED_COLUMNS: frozenset[str] = frozenset(
    {
        "account_id",
        "username",
        "password",
        "player_name",
        "status",
        "created_at",
        "last_used_at",
        "ban_count",
        "notes",
        "metadata",
    }
)


# ======================================================================
# AccountStorage 主类
# ======================================================================


class AccountStorage:
    """账号存储层。

    基于 aiosqlite 的异步 SQLite 存储，负责账号数据的增删改查。

    Args:
        db_path: 数据库文件路径，默认为 :data:`DEFAULT_DB_PATH`。

    典型用法::

        storage = AccountStorage()
        await storage.init_db()
        await storage.save_account(account)
        accounts = await storage.get_all_accounts()
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self._db_path: Path = Path(db_path)
        self._db: Optional[aiosqlite.Connection] = None
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None
        self._initialized: bool = False

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # ------------------------------------------------------------------ #
    # 初始化 / 关闭
    # ------------------------------------------------------------------ #

    async def init_db(self) -> None:
        """初始化数据库。

        创建 ``accounts`` 表及索引。若已初始化则直接返回（幂等）。
        会自动创建数据库文件所在的父目录。
        """
        async with self._get_lock():
            if self._initialized:
                return
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(str(self._db_path))
            self._db.row_factory = aiosqlite.Row
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id   TEXT PRIMARY KEY,
                    username     TEXT NOT NULL,
                    password     TEXT NOT NULL,
                    player_name  TEXT NOT NULL DEFAULT '',
                    status       TEXT NOT NULL,
                    created_at   REAL NOT NULL,
                    last_used_at REAL,
                    ban_count    INTEGER NOT NULL DEFAULT 0,
                    notes        TEXT NOT NULL DEFAULT '',
                    metadata     TEXT NOT NULL DEFAULT '{}'
                )
                """
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_accounts_status "
                "ON accounts(status)"
            )
            await self._db.commit()
            self._initialized = True
            logger.info(f"账号数据库已初始化: {self._db_path}")

    async def close(self) -> None:
        """关闭数据库连接。

        关闭后 ``_initialized`` 被重置，下次操作会自动重新初始化。
        """
        async with self._get_lock():
            if self._db is not None:
                await self._db.close()
                self._db = None
            self._initialized = False
            logger.info("账号数据库连接已关闭")

    async def _ensure(self) -> aiosqlite.Connection:
        """确保数据库已初始化并返回连接。"""
        if not self._initialized or self._db is None:
            await self.init_db()
        assert self._db is not None
        return self._db

    # ------------------------------------------------------------------ #
    # 行 <-> 模型 转换
    # ------------------------------------------------------------------ #

    @staticmethod
    def _row_to_account(row: aiosqlite.Row) -> NeteaseAccount:
        """将数据库行转换为 :class:`NeteaseAccount`。"""
        try:
            status = AccountStatus(row["status"])
        except (ValueError, KeyError):
            status = AccountStatus.ERROR

        raw_metadata = row["metadata"] if "metadata" in row.keys() else "{}"
        try:
            metadata = json.loads(raw_metadata) if raw_metadata else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}

        return NeteaseAccount(
            account_id=row["account_id"],
            username=row["username"],
            password=row["password"],
            player_name=row["player_name"],
            status=status,
            created_at=row["created_at"],
            last_used_at=row["last_used_at"],
            ban_count=row["ban_count"],
            notes=row["notes"],
            metadata=metadata,
        )

    @staticmethod
    def _account_to_params(account: NeteaseAccount) -> tuple[Any, ...]:
        """将 :class:`NeteaseAccount` 转换为 SQL 占位符参数元组。"""
        return (
            account.account_id,
            account.username,
            account.password,
            account.player_name,
            account.status.value,
            account.created_at,
            account.last_used_at,
            account.ban_count,
            account.notes,
            json.dumps(account.metadata, ensure_ascii=False),
        )

    # ------------------------------------------------------------------ #
    # 增 / 改
    # ------------------------------------------------------------------ #

    async def save_account(self, account: NeteaseAccount) -> None:
        """保存账号（存在则覆盖，不存在则插入）。

        采用 ``INSERT OR REPLACE`` 语义，便于导入与更新。

        Args:
            account: 要保存的账号实例。
        """
        db = await self._ensure()
        await db.execute(
            """
            INSERT OR REPLACE INTO accounts
                (account_id, username, password, player_name, status,
                 created_at, last_used_at, ban_count, notes, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._account_to_params(account),
        )
        await db.commit()
        logger.debug(f"已保存账号 {account.username} (id={account.account_id[:8]})")

    async def update_account(self, account_id: str, fields: dict[str, Any]) -> bool:
        """局部更新账号字段。

        Args:
            account_id: 账号 ID。
            fields: 待更新字段字典。允许的键见 :data:`_ALLOWED_COLUMNS`。
                - ``status`` 若为 :class:`AccountStatus` 会自动取 ``.value``
                - ``metadata`` 若为 ``dict`` 会自动序列化为 JSON 字符串
                非法列名会被静默忽略。

        Returns:
            ``True`` 更新成功（至少有一行受影响）;``False`` 账号不存在或无有效字段。
        """
        if not fields:
            return False

        set_clauses: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in _ALLOWED_COLUMNS or key == "account_id":
                continue
            if key == "status" and isinstance(value, AccountStatus):
                value = value.value
            elif key == "metadata" and isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            set_clauses.append(f"{key} = ?")
            values.append(value)

        if not set_clauses:
            return False

        values.append(account_id)
        sql = (
            f"UPDATE accounts SET {', '.join(set_clauses)} "
            "WHERE account_id = ?"
        )
        db = await self._ensure()
        cursor = await db.execute(sql, values)
        await db.commit()
        affected = cursor.rowcount
        await cursor.close()
        return affected > 0

    # ------------------------------------------------------------------ #
    # 查
    # ------------------------------------------------------------------ #

    async def get_account(self, account_id: str) -> Optional[NeteaseAccount]:
        """根据 ID 获取单个账号。

        Args:
            account_id: 账号 ID。

        Returns:
            账号实例;不存在时返回 ``None``。
        """
        db = await self._ensure()
        async with db.execute(
            "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return self._row_to_account(row) if row else None

    async def get_all_accounts(self) -> list[NeteaseAccount]:
        """获取全部账号，按创建时间升序排列。

        Returns:
            账号实例列表（可能为空）。
        """
        db = await self._ensure()
        async with db.execute(
            "SELECT * FROM accounts ORDER BY created_at ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [self._row_to_account(r) for r in rows]

    # ------------------------------------------------------------------ #
    # 删
    # ------------------------------------------------------------------ #

    async def delete_account(self, account_id: str) -> bool:
        """删除账号。

        Args:
            account_id: 账号 ID。

        Returns:
            ``True`` 删除成功;``False`` 账号不存在。
        """
        db = await self._ensure()
        cursor = await db.execute(
            "DELETE FROM accounts WHERE account_id = ?", (account_id,)
        )
        await db.commit()
        affected = cursor.rowcount
        await cursor.close()
        return affected > 0


__all__ = [
    "AccountStorage",
    "DEFAULT_DB_PATH",
]
