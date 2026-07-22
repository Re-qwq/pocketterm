"""PocketTerm 机器人数据模型

本模块定义了机器人核心运行所需的全部数据结构，包括:

    - :class:`BotStatus`        机器人运行状态枚举
    - :class:`ServerType`       服务器类型枚举
    - :class:`AccessPointType`  接入点类型枚举
    - :class:`BotConfig`        机器人配置
    - :class:`BotInfo`          机器人运行时信息
    - :class:`ChatMessage`      聊天消息
    - :class:`InventorySlot`    物品栏槽位
    - :class:`WindowInfo`       容器窗口信息

所有 dataclass 均使用 ``from __future__ import annotations`` 以支持
前向引用，且对可变默认值使用 ``field(default_factory=...)``。
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


class BotStatus(enum.Enum):
    """机器人运行状态。

    状态流转::

        IDLE ──start()──► CONNECTING ──► AUTHENTICATING ──► CONNECTED ──► SPAWNED
                                                                          │
            ┌───────────────────────────────────────────────────────────────┘
            ▼
        ERROR / DISCONNECTED / KICKED / BANNED

    各状态含义:
        - ``IDLE``          空闲，尚未启动
        - ``CONNECTING``    正在连接服务器
        - ``AUTHENTICATING`` 正在进行认证
        - ``CONNECTED``     已连接到游戏服务器
        - ``SPAWNED``       机器人已在游戏中生成（可操作）
        - ``ERROR``         发生错误
        - ``DISCONNECTED``  已断开连接
        - ``KICKED``        被踢出服务器
        - ``BANNED``        被封禁
    """

    IDLE = "idle"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    SPAWNED = "spawned"
    ERROR = "error"
    DISCONNECTED = "disconnected"
    KICKED = "kicked"
    BANNED = "banned"


class ServerType(enum.Enum):
    """服务器类型。

    - ``RENTAL``  网易租赁服（需认证服务器获取 IP）
    - ``LOBBY``   网易联机大厅
    - ``LOCAL``   局域网本地联机
    - ``CUSTOM``  自定义服务器地址（直接 IP:Port）
    """

    RENTAL = "rental"
    LOBBY = "lobby"
    LOCAL = "local"
    CUSTOM = "custom"


class AccessPointType(enum.Enum):
    """接入点类型。

    接入点是 PocketTerm 与 Minecraft 网易版服务器之间的通信桥梁，
    不同接入点使用不同的底层协议:

        - ``PUREPYTHON`` 纯Python协议，内置免安装，直接连接
        - ``NEOMEGA``  通过 NeOmega 进程的 WebSocket 接口通信
        - ``FATEARK``  通过 FateArk 进程的 stdin/stdout 通信
        - ``CUSTOM``   自建接入点（直接 RakNet / python-bedrock）
    """

    PUREPYTHON = "purepython"
    NEOMEGA = "neomega"
    FATEARK = "fateark"
    CUSTOM = "custom"


# ======================================================================
# 配置
# ======================================================================


@dataclass
class BotConfig:
    """机器人配置。

    所有字段均有默认值，可在创建时按需覆盖。

    Attributes:
        name: 机器人游戏内名称。为空时由 PocketBot 自动生成 ``PT_<6位随机数字>``。
        server_code: 租赁服号 / 房间号（如 ``"123456"``）。
        server_password: 服务器密码（可为空串）。
        server_type: 服务器类型，决定连接流程。
        server_address: 自定义服务器地址（仅 ``ServerType.CUSTOM`` 时使用）。
        server_port: 服务器端口（默认 19132，MCBE 标准 RakNet 端口）。
        auth_server: 认证服务器 URL（如 ``"https://nv1.nethard.pro"``）。
        api_key: 认证服务器 API Key。
        device_model: 设备型号字符串（用于设备指纹生成）。
        access_point_type: 接入点类型，决定底层通信方式。
        auto_reconnect: 断开后是否自动重连。
        max_reconnect_attempts: 最大重连次数（超过后停止）。
        reconnect_delay: 重连基础延迟（秒），实际延迟 = delay * 当前重连次数。
        account_id: 账号 ID（用于多账号管理时区分）。
    """

    name: str = ""
    server_code: str = ""
    server_password: str = ""
    server_type: ServerType = ServerType.RENTAL
    server_address: str = ""
    server_port: int = 19132
    auth_server: str = ""
    api_key: str = ""
    cookie: str = ""
    sauth_json: str = ""
    auth_method: str = "auto"  # auto / direct / fatalder / cookie / fbauth
    device_model: str = "Xiaomi 13"
    access_point_type: AccessPointType = AccessPointType.PUREPYTHON
    auto_reconnect: bool = True
    max_reconnect_attempts: int = 3
    reconnect_delay: int = 30  # 基础延迟30秒,避免快速重连触发反作弊
    account_id: str = ""


# ======================================================================
# 运行时信息
# ======================================================================


@dataclass
class BotInfo:
    """机器人运行时信息。

    在机器人创建时初始化，随运行不断更新。
    ``to_dict()`` 方法将关键字段序列化为字典，供 API / WebSocket 推送使用。

    Attributes:
        bot_id: 机器人唯一标识（UUID 前 8 位）。
        config: 关联的 :class:`BotConfig`。
        status: 当前运行状态。
        created_at: 创建时间戳。
        connected_at: 成功连接时间戳（未连接时为 ``None``）。
        last_error: 最近一次错误信息。
        server_ip: 已连接的游戏服务器 IP。
        player_list: 在线玩家列表。
        position: 机器人世界坐标 ``(x, y, z)``。
        health: 生命值（0-20）。
        hunger: 饥饿值（0-20）。
        logs: 运行日志列表（每条 ``{"time", "level", "message"}``）。
        metadata: 附加元数据（可自由扩展）。
    """

    bot_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    config: BotConfig = field(default_factory=BotConfig)
    status: BotStatus = BotStatus.IDLE
    created_at: float = field(default_factory=time.time)
    connected_at: Optional[float] = None
    last_error: str = ""
    server_ip: str = ""
    player_list: list[str] = field(default_factory=list)
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    health: float = 20.0
    hunger: float = 20.0
    logs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_log(self, level: str, message: str) -> None:
        """添加一条运行日志。

        Args:
            level: 日志级别 (``"debug"`` / ``"info"`` / ``"warning"`` / ``"error"``)。
            message: 日志内容。
        """
        self.logs.append(
            {
                "time": time.time(),
                "level": level,
                "message": message,
            }
        )
        # 只保留最近 1000 条日志，防止内存无限增长
        if len(self.logs) > 1000:
            self.logs = self.logs[-1000:]

    def to_dict(self) -> dict[str, Any]:
        """将关键信息序列化为字典。

        用于 API 响应、WebSocket 推送等场景。
        日志列表不包含在内（太大），请使用 :meth:`BotInfo.logs` 直接访问。
        """
        return {
            "bot_id": self.bot_id,
            "name": self.config.name,
            "status": self.status.value,
            "server_code": self.config.server_code,
            "server_type": self.config.server_type.value,
            "access_point_type": self.config.access_point_type.value,
            "account_id": self.config.account_id,
            "created_at": self.created_at,
            "connected_at": self.connected_at,
            "last_error": self.last_error,
            "server_ip": self.server_ip,
            "player_count": len(self.player_list),
            "player_list": list(self.player_list),
            "health": self.health,
            "hunger": self.hunger,
            "position": {
                "x": self.position[0],
                "y": self.position[1],
                "z": self.position[2],
            },
            "metadata": dict(self.metadata),
            "log_count": len(self.logs),
        }


# ======================================================================
# 游戏数据结构
# ======================================================================


@dataclass
class ChatMessage:
    """聊天消息。

    Attributes:
        sender: 发送者名称（系统消息为 ``"System"``）。
        message: 消息内容。
        timestamp: 时间戳。
        is_system: 是否为系统消息。
    """

    sender: str
    message: str
    timestamp: float = field(default_factory=time.time)
    is_system: bool = False


@dataclass
class InventorySlot:
    """物品栏槽位。

    Attributes:
        slot_id: 槽位编号（0=快捷栏第一格，以此类推）。
        item_id: 物品数字 ID。
        item_count: 物品堆叠数量。
        item_damage: 物品耐久损伤值（0 = 满耐久）。
        nbt: 物品 NBT 数据（附魔、自定义名称等）。
        name: 物品显示名称。
    """

    slot_id: int
    item_id: int
    item_count: int
    item_damage: int = 0
    nbt: dict[str, Any] = field(default_factory=dict)
    name: str = ""


@dataclass
class WindowInfo:
    """容器窗口信息（箱子、铁砧、工作台等）。

    Attributes:
        window_id: 窗口 ID（0=玩家自身背包，1+=服务器分配的容器窗口）。
        window_type: 窗口类型标识（如 ``"minecraft:chest"``）。
        title: 窗口标题（中文显示名）。
        slots: 窗口内所有槽位的物品信息。
    """

    window_id: int
    window_type: str
    title: str
    slots: list[InventorySlot] = field(default_factory=list)


__all__ = [
    "BotStatus",
    "ServerType",
    "AccessPointType",
    "BotConfig",
    "BotInfo",
    "ChatMessage",
    "InventorySlot",
    "WindowInfo",
]
