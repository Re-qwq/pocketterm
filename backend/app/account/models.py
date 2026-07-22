"""PocketTerm 账号数据模型

本模块定义了网易账号管理所需的全部数据结构，包括:

    - :class:`AccountStatus`   账号状态枚举
    - :class:`NeteaseAccount`  网易账号数据模型

所有 dataclass 均使用 ``from __future__ import annotations`` 以支持
前向引用，且对可变默认值使用 ``field(default_factory=...)``。

.. note::

    :meth:`NeteaseAccount.to_dict` 不会返回 ``password`` 字段，因此可以
    安全地用于 API 响应、WebSocket 推送等对外场景。密码字段的加解密
    由 :mod:`account.manager` 中的 :class:`AccountManager` 统一负责。
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


# ======================================================================
# 枚举
# ======================================================================


class AccountStatus(enum.Enum):
    """账号状态。

    状态流转::

        ACTIVE ──封禁──► BANNED ──申诉──► ACTIVE
          │                  │
          ├──过期──► EXPIRED │
          │                  └──累计──► (ban_count + 1)
          └──异常──► ERROR ──修复──► ACTIVE

    各状态含义:
        - ``ACTIVE``   可用，可分配给机器人
        - ``BANNED``   被封禁，不可使用
        - ``EXPIRED``  已过期（如会员到期、token 失效）
        - ``ERROR``    异常状态（登录失败、数据损坏等）
    """

    ACTIVE = "active"
    BANNED = "banned"
    EXPIRED = "expired"
    ERROR = "error"


# ======================================================================
# 账号数据模型
# ======================================================================


@dataclass
class NeteaseAccount:
    """网易账号数据模型。

    一个 :class:`NeteaseAccount` 对应一个网易账号（手机号 / 邮箱 / 用户名），
    可被分配给一个或多个机器人用于登录游戏服务器。

    Attributes:
        account_id: 账号唯一标识（UUID）。
        username: 网易账号用户名 / 邮箱 / 手机号。
        password: 加密后的密码（base64 + salt，可逆）。
            明文密码不应出现在内存外的任何地方，加解密由
            :class:`AccountManager` 统一处理。
        player_name: 游戏内玩家名称。
        status: 账号当前状态，见 :class:`AccountStatus`。
        created_at: 创建时间戳（Unix 秒）。
        last_used_at: 最近一次使用时间戳，未使用过时为 ``None``。
        ban_count: 累计被封禁次数（每次进入 ``BANNED`` 状态时 +1）。
        notes: 备注（用户自定义文本）。
        metadata: 附加元数据，用于存储 token、cookie、分配信息等。
    """

    account_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    username: str = ""
    password: str = ""
    player_name: str = ""
    status: AccountStatus = AccountStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    last_used_at: Optional[float] = None
    ban_count: int = 0
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """将账号信息序列化为字典。

        .. warning::

            出于安全考虑，本方法 **不会** 返回 ``password`` 字段。
            如需导出包含加密密码的完整数据（用于备份 / 迁移），
            请使用 :meth:`AccountManager.export_accounts`。

        Returns:
            不含密码的账号信息字典。
        """
        return {
            "account_id": self.account_id,
            "username": self.username,
            "player_name": self.player_name,
            "status": self.status.value,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "ban_count": self.ban_count,
            "notes": self.notes,
            "metadata": dict(self.metadata),
        }


__all__ = [
    "AccountStatus",
    "NeteaseAccount",
]
