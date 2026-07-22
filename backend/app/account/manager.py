"""PocketTerm 账号管理器

:class:`AccountManager` 是一个单例类，负责统一管理多个
:class:`NeteaseAccount` 实例，是账号模块对外暴露的主要入口。

提供以下功能:

    - **增删改查**:  ``add_account`` / ``remove_account`` / ``get_account``
                     / ``list_accounts`` / ``update_account``
    - **导入导出**:  ``import_account`` / ``export_accounts``（用于备份 / 迁移）
    - **分配调度**:  ``assign_to_bot`` / ``get_available_account``
    - **自动注册**:  ``auto_register``（框架 TODO）
    - **密码加解密**: ``encrypt_password`` / ``decrypt_password``

密码加密策略:
    采用简单的 **base64 + salt** 可逆方案（XOR 混淆 + base64 编码）。
    该方案仅用于避免明文落库，**不具备真正的安全性**，请勿用于
    高安全场景。如需更强保护，应替换为对称加密（如 :mod:`cryptography`
    的 Fernet）。

典型用法::

    from account.manager import account_manager
    from account.models import AccountStatus

    # 添加账号
    acc = await account_manager.add_account(
        username="13800138000",
        password="p@ssw0rd",
        player_name="Steve",
    )

    # 获取可用账号并分配给机器人
    available = await account_manager.get_available_account()
    if available:
        await account_manager.assign_to_bot(available.account_id, "bot_001")

    # 列出全部账号（不含密码）
    for a in await account_manager.list_accounts():
        print(a.to_dict())
"""
from __future__ import annotations

import base64
import json
import logging
import time
import uuid
from typing import Any, Optional

from .models import AccountStatus, NeteaseAccount
from .storage import AccountStorage

logger = logging.getLogger("pocketterm.account_manager")

#: 密码加密使用的固定 salt（base64 + salt 简单方案）。
#: 修改此值会导致已存储的密码无法解密。
_PASSWORD_SALT: str = "pocketterm_account_salt_v1"


# ======================================================================
# AccountManager 主类
# ======================================================================


class AccountManager:
    """账号管理器（单例）。

    管理多个 :class:`NeteaseAccount` 的完整生命周期，并通过内部的
    :class:`AccountStorage` 持久化到 SQLite。

    Args:
        storage: 存储层实例。为 ``None`` 时使用默认的
            :class:`AccountStorage`（``backend/data/accounts.db``）。
    """

    # 单例实例
    _instance: Optional["AccountManager"] = None
    _initialized: bool = False

    def __new__(cls, *args: Any, **kwargs: Any) -> "AccountManager":
        """单例模式:确保全局只有一个 AccountManager 实例。"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, storage: Optional[AccountStorage] = None) -> None:
        # 避免单例模式下重复初始化
        if AccountManager._initialized:
            return
        AccountManager._initialized = True

        self._storage: AccountStorage = storage or AccountStorage()

    # ------------------------------------------------------------------ #
    # 类方法:重置单例（主要用于测试）
    # ------------------------------------------------------------------ #

    @classmethod
    def _reset_singleton(cls) -> None:
        """重置单例实例（仅用于测试）。

        清除单例实例和初始化标志，允许重新创建。
        生产环境中不应调用此方法。
        """
        cls._instance = None
        cls._initialized = False

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def storage(self) -> AccountStorage:
        """底层存储层实例。"""
        return self._storage

    # ------------------------------------------------------------------ #
    # 密码加解密（静态方法，纯函数）
    # ------------------------------------------------------------------ #

    @staticmethod
    def encrypt_password(plain: str) -> str:
        """加密明文密码。

        采用 **XOR + base64** 的简单可逆方案:
            1. 用固定 salt 对明文逐字节 XOR;
            2. 将结果 base64 编码。

        Args:
            plain: 明文密码。

        Returns:
            加密后的字符串。
        """
        if not plain:
            return ""
        salt = _PASSWORD_SALT.encode("utf-8")
        data = plain.encode("utf-8")
        xored = bytes(b ^ salt[i % len(salt)] for i, b in enumerate(data))
        return base64.b64encode(xored).decode("utf-8")

    @staticmethod
    def decrypt_password(encrypted: str) -> str:
        """解密密码。

        :meth:`encrypt_password` 的逆运算。若输入为空或损坏则返回空串。

        Args:
            encrypted: 加密后的字符串。

        Returns:
            明文密码;解码失败时返回 ``""``。
        """
        if not encrypted:
            return ""
        salt = _PASSWORD_SALT.encode("utf-8")
        try:
            xored = base64.b64decode(encrypted)
        except (ValueError, TypeError):
            return ""
        data = bytes(b ^ salt[i % len(salt)] for i, b in enumerate(xored))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return ""

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    async def _ensure_init(self) -> None:
        """确保存储层已初始化。"""
        await self._storage.init_db()

    @staticmethod
    def _parse_status(value: Any) -> AccountStatus:
        """将任意值解析为 :class:`AccountStatus`，失败时回退到 ACTIVE。"""
        if isinstance(value, AccountStatus):
            return value
        try:
            return AccountStatus(value)
        except (ValueError, TypeError):
            return AccountStatus.ACTIVE

    @staticmethod
    def _parse_metadata(value: Any) -> dict[str, Any]:
        """将任意值解析为 metadata 字典。"""
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except (json.JSONDecodeError, TypeError):
                return {}
        return {}

    # ------------------------------------------------------------------ #
    # 增 / 删 / 改 / 查
    # ------------------------------------------------------------------ #

    async def add_account(
        self,
        username: str,
        password: str,
        player_name: str = "",
    ) -> NeteaseAccount:
        """添加新账号。

        密码会自动加密后存储。新建账号状态默认为 :attr:`AccountStatus.ACTIVE`。

        Args:
            username: 网易账号用户名 / 邮箱 / 手机号。
            password: 明文密码（将自动加密）。
            player_name: 游戏内名称，默认为空。

        Returns:
            新创建的 :class:`NeteaseAccount` 实例。
        """
        await self._ensure_init()
        account = NeteaseAccount(
            account_id=str(uuid.uuid4()),
            username=username,
            password=self.encrypt_password(password),
            player_name=player_name,
            status=AccountStatus.ACTIVE,
            created_at=time.time(),
        )
        await self._storage.save_account(account)
        logger.info(
            f"添加账号 {username} (player={player_name}, "
            f"id={account.account_id[:8]})"
        )
        return account

    async def remove_account(self, account_id: str) -> bool:
        """移除账号。

        Args:
            account_id: 账号 ID。

        Returns:
            ``True`` 移除成功;``False`` 账号不存在。
        """
        await self._ensure_init()
        ok = await self._storage.delete_account(account_id)
        if ok:
            logger.info(f"移除账号 id={account_id[:8]}")
        else:
            logger.warning(f"移除账号失败:未找到 id={account_id[:8]}")
        return ok

    async def get_account(self, account_id: str) -> Optional[NeteaseAccount]:
        """获取指定账号。

        Args:
            account_id: 账号 ID。

        Returns:
            账号实例;不存在时返回 ``None``。
        """
        await self._ensure_init()
        return await self._storage.get_account(account_id)

    async def list_accounts(self) -> list[NeteaseAccount]:
        """列出全部账号，按创建时间升序。

        Returns:
            账号实例列表（可能为空）。
        """
        await self._ensure_init()
        return await self._storage.get_all_accounts()

    async def update_account(
        self, account_id: str, **kwargs: Any
    ) -> Optional[NeteaseAccount]:
        """更新账号字段。

        支持的字段: ``username`` / ``password`` / ``player_name`` / ``status``
        / ``created_at`` / ``last_used_at`` / ``ban_count`` / ``notes``
        / ``metadata``。

        特殊处理:
            - ``password`` 视为 **明文**，会自动加密后再存储;
            - ``status`` 可传 :class:`AccountStatus` 或字符串值;
            - ``metadata`` 可传 ``dict``，会自动序列化为 JSON。

        Args:
            account_id: 账号 ID。
            **kwargs: 待更新字段。

        Returns:
            更新后的账号实例;账号不存在时返回 ``None``。
        """
        await self._ensure_init()
        account = await self._storage.get_account(account_id)
        if account is None:
            return None

        valid_keys = {
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

        fields: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key not in valid_keys:
                continue
            if key == "password":
                fields["password"] = self.encrypt_password(str(value))
            else:
                fields[key] = value

        if not fields:
            return account

        await self._storage.update_account(account_id, fields)
        logger.info(f"更新账号 id={account_id[:8]} fields={list(fields.keys())}")
        return await self._storage.get_account(account_id)

    # ------------------------------------------------------------------ #
    # 导入 / 导出
    # ------------------------------------------------------------------ #

    async def import_account(self, data: dict[str, Any]) -> NeteaseAccount:
        """从字典导入单个账号（用于批量导入 / 恢复备份）。

        支持的数据键:
            - ``account_id``: 可选，缺失时自动生成新 UUID;
            - ``username`` / ``player_name`` / ``notes``: 文本字段;
            - ``password``: **明文** 密码，会自动加密;
            - ``encrypted_password``: 已加密密码（当 ``password`` 缺失时使用）;
            - ``status``: :class:`AccountStatus` 或字符串值;
            - ``created_at`` / ``last_used_at`` / ``ban_count``: 数值字段;
            - ``metadata``: ``dict`` 或 JSON 字符串。

        若 ``account_id`` 已存在，则覆盖更新（``INSERT OR REPLACE`` 语义）。

        Args:
            data: 账号数据字典。

        Returns:
            导入后的 :class:`NeteaseAccount` 实例。
        """
        await self._ensure_init()

        account_id = data.get("account_id") or str(uuid.uuid4())

        # 密码:优先使用明文 password（加密），否则使用已加密的 encrypted_password
        if data.get("password"):
            password = self.encrypt_password(str(data["password"]))
        elif data.get("encrypted_password"):
            password = str(data["encrypted_password"])
        else:
            password = ""

        status = self._parse_status(data.get("status", AccountStatus.ACTIVE.value))
        metadata = self._parse_metadata(data.get("metadata", {}))

        account = NeteaseAccount(
            account_id=account_id,
            username=str(data.get("username", "")),
            password=password,
            player_name=str(data.get("player_name", "")),
            status=status,
            created_at=float(data.get("created_at", time.time())),
            last_used_at=(
                float(data["last_used_at"]) if data.get("last_used_at") else None
            ),
            ban_count=int(data.get("ban_count", 0)),
            notes=str(data.get("notes", "")),
            metadata=metadata,
        )
        await self._storage.save_account(account)
        logger.info(
            f"导入账号 {account.username} (id={account.account_id[:8]})"
        )
        return account

    async def export_accounts(self) -> list[dict[str, Any]]:
        """导出全部账号为字典列表（用于备份 / 迁移）。

        .. warning::

            导出结果 **包含加密后的密码**（``password`` 字段），可配合
            :meth:`import_account` 完成完整迁移。请妥善保管导出数据，
            不要在日志或前端展示中暴露。

        Returns:
            账号完整信息字典列表（包含加密密码）。
        """
        await self._ensure_init()
        accounts = await self._storage.get_all_accounts()
        result: list[dict[str, Any]] = []
        for acc in accounts:
            result.append(
                {
                    "account_id": acc.account_id,
                    "username": acc.username,
                    "password": acc.password,  # 已加密
                    "player_name": acc.player_name,
                    "status": acc.status.value,
                    "created_at": acc.created_at,
                    "last_used_at": acc.last_used_at,
                    "ban_count": acc.ban_count,
                    "notes": acc.notes,
                    "metadata": dict(acc.metadata),
                }
            )
        return result

    # ------------------------------------------------------------------ #
    # 分配 / 调度
    # ------------------------------------------------------------------ #

    async def assign_to_bot(self, account_id: str, bot_id: str) -> bool:
        """将账号分配给指定机器人。

        分配信息写入账号 ``metadata`` 的 ``assigned_bot_id`` 与
        ``assigned_at`` 字段。一个账号同一时间只能分配给一个机器人
        （再次分配会覆盖原值）。

        Args:
            account_id: 账号 ID。
            bot_id: 机器人 ID。

        Returns:
            ``True`` 分配成功;``False`` 账号不存在。
        """
        await self._ensure_init()
        account = await self._storage.get_account(account_id)
        if account is None:
            logger.warning(f"分配账号失败:未找到 id={account_id[:8]}")
            return False

        metadata = dict(account.metadata)
        metadata["assigned_bot_id"] = bot_id
        metadata["assigned_at"] = time.time()
        await self._storage.update_account(account_id, {"metadata": metadata})
        logger.info(
            f"账号 id={account_id[:8]} 已分配给机器人 {bot_id}"
        )
        return True

    async def get_available_account(self) -> Optional[NeteaseAccount]:
        """获取一个可用账号。

        筛选条件:
            - 状态为 :attr:`AccountStatus.ACTIVE`;
            - ``metadata`` 中未设置 ``assigned_bot_id``（未被占用）。

        在符合条件的账号中，选择 ``last_used_at`` 最早（最久未使用）的一个，
        以实现简单的轮询调度。

        Returns:
            可用账号实例;无可用账号时返回 ``None``。
        """
        await self._ensure_init()
        accounts = await self._storage.get_all_accounts()
        candidates = [
            a
            for a in accounts
            if a.status == AccountStatus.ACTIVE
            and not a.metadata.get("assigned_bot_id")
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda a: a.last_used_at or 0.0)
        return candidates[0]

    # ------------------------------------------------------------------ #
    # 自动注册（框架 TODO）
    # ------------------------------------------------------------------ #

    async def auto_register(self) -> Optional[NeteaseAccount]:
        """自动注册新账号。

        .. note::

            此方法为框架占位，当前 **未实现**。后续接入自动注册流程
            （如调用第三方注册接口 / 打码平台）后补全。

        Returns:
            注册成功的新账号;当前始终返回 ``None``。
        """
        logger.warning("auto_register 尚未实现 (TODO: 接入自动注册框架)")
        return None


# ======================================================================
# 全局单例
# ======================================================================

#: 全局账号管理器实例
#: 首次访问时通过单例模式创建
account_manager = AccountManager()


__all__ = [
    "AccountManager",
    "account_manager",
]
