"""PocketTerm 多用户系统数据模型。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional


class UserRole(enum.Enum):
    """用户角色。"""
    SUPERADMIN = "superadmin"  # 主管理员（不可删除/降级）
    ADMIN = "admin"            # 管理员
    USER = "user"              # 普通用户


class UserStatus(enum.Enum):
    """用户状态。"""
    ACTIVE = "active"
    SUSPENDED = "suspended"   # 被降级/暂停
    BANNED = "banned"         # 被封禁


class CardKeyType(enum.Enum):
    """卡密类型。"""
    REGISTER = "register"  # 注册卡密（创建账号用）
    PANEL = "panel"        # 面板卡密（创建面板用）
    RENEWAL = "renewal"    # 续期卡密（续费用）


class CardKeyStatus(enum.Enum):
    """卡密状态。"""
    UNUSED = "unused"      # 未使用
    USED = "used"          # 已使用
    EXPIRED = "expired"    # 已过期
    REVOKED = "revoked"    # 已撤销


class PanelStatus(enum.Enum):
    """面板状态。"""
    ACTIVE = "active"      # 活跃
    EXPIRED = "expired"    # 已过期
    SUSPENDED = "suspended" # 被暂停


class LogLevel(enum.Enum):
    """日志级别。"""
    INFO = "info"
    SUCCESS = "success"
    WARN = "warn"
    ERROR = "error"
    SYSTEM = "system"


class LogTarget(enum.Enum):
    """日志目标。"""
    USER = "user"          # 用户日志
    PANEL = "panel"        # 面板日志
    BOT = "bot"            # 机器人日志
    SYSTEM = "system"      # 系统日志


@dataclass
class User:
    """用户。"""
    user_id: str
    username: str
    password_hash: str
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE
    created_at: float = 0.0
    last_login_at: Optional[float] = None
    last_login_ip: str = ""
    expire_at: Optional[float] = None  # None = 永久
    created_by: str = ""  # 创建者 user_id
    must_change_password: bool = False  # 首次登录/重置后需强制修改密码


@dataclass
class CardKey:
    """卡密。"""
    card_id: str           # 内部 ID
    key: str               # 卡密字符串 (XXXX-XXXX)
    key_type: CardKeyType
    status: CardKeyStatus = CardKeyStatus.UNUSED
    duration_days: Optional[float] = None  # None = 永久, float 支持小时级别
    bound_user_id: str = ""    # 绑定的用户
    bound_panel_id: str = ""   # 绑定的面板
    created_by: str = ""       # 创建者 user_id
    created_at: float = 0.0
    used_at: Optional[float] = None
    expires_at: Optional[float] = None  # 卡密本身过期时间


@dataclass
class Panel:
    """面板。"""
    panel_id: str           # 面板 ID (PT-xxxxxxxx)
    user_id: str            # 所属用户
    name: str               # 面板名称
    status: PanelStatus = PanelStatus.ACTIVE
    created_at: float = 0.0
    expire_at: Optional[float] = None  # None = 永久
    created_by_card: str = ""  # 创建时使用的卡密


@dataclass
class LogEntry:
    """日志条目。"""
    log_id: str
    target_type: LogTarget
    target_id: str          # user_id / panel_id / bot_id / "system"
    level: LogLevel
    message: str
    details: str = ""       # JSON 附加信息
    ip: str = ""
    created_at: float = 0.0
    created_by: str = ""    # 操作者 user_id


@dataclass
class BotInstance:
    """机器人实例（数据库持久化）。"""
    bot_id: str
    panel_id: str           # 所属面板
    name: str               # 实例名称
    account_id: str = ""    # 关联的游戏账号
    server_code: str = ""  # 租赁服编号
    server_type: str = "rental"
    access_point_type: str = "neomega"
    status: str = "stopped"  # stopped/running/error
    created_at: float = 0.0
    last_started_at: Optional[float] = None
    config: str = "{}"       # JSON 配置
