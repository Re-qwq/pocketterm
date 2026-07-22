"""bot_info - BotBasicInfoHolder 机器人信息模块。

逆向自 NovaBuilder 的 WavesAccess BotBasicInfoHolder, 来源:
    - /workspace/novuilder_reverse/device_full.txt
    - /workspace/novuilder_reverse/device_fingerprint.txt
    - /workspace/novuilder_reverse/player_options.txt
    - /workspace/novuilder_reverse/strings_security.txt

BotBasicInfoHolder 持有机器人的基本信息, 包括:

    1. BotBasicInfo -- 机器人基本信息 (名称/UUID/位置等)
    2. WorldInfo    -- 世界信息 (维度/难度/游戏规则等)
    3. ServerInfo   -- 服务器信息 (名称/版本/在线人数等)

这些信息在登录时初始化, 并随着游戏事件实时更新。

字符串证据 (逆向自 device_full.txt):
    "ClientGUID"          -- 客户端全局唯一 ID
    "ClientRandomId"      -- 客户端随机 ID
    "DeviceId"            -- 设备 ID
    "DeviceOS"            -- 设备操作系统
    "PlayerUUID"          -- 玩家 UUID
    "GameVersion"         -- 游戏版本
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.waves_access.bot_info")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 默认游戏版本
DEFAULT_GAME_VERSION: str = "1.20.80"

#: 维度名称
DIMENSION_OVERWORLD: int = 0
DIMENSION_NETHER: int = 1
DIMENSION_END: int = 2

#: 维度名称映射
DIMENSION_NAMES: dict[int, str] = {
    DIMENSION_OVERWORLD: "Overworld",
    DIMENSION_NETHER: "Nether",
    DIMENSION_END: "End",
}

#: 难度
DIFFICULTY_PEACEFUL: int = 0
DIFFICULTY_EASY: int = 1
DIFFICULTY_NORMAL: int = 2
DIFFICULTY_HARD: int = 3

#: 难度名称映射
DIFFICULTY_NAMES: dict[int, str] = {
    DIFFICULTY_PEACEFUL: "Peaceful",
    DIFFICULTY_EASY: "Easy",
    DIFFICULTY_NORMAL: "Normal",
    DIFFICULTY_HARD: "Hard",
}


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class Dimension(Enum):
    """维度枚举。"""

    OVERWORLD = DIMENSION_OVERWORLD
    NETHER = DIMENSION_NETHER
    END = DIMENSION_END


class Difficulty(Enum):
    """难度枚举。"""

    PEACEFUL = DIFFICULTY_PEACEFUL
    EASY = DIFFICULTY_EASY
    NORMAL = DIFFICULTY_NORMAL
    HARD = DIFFICULTY_HARD


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class BotBasicInfo:
    """机器人基本信息。

    逆向自 device_full.txt 中的设备字段和玩家信息。
    """

    # 身份信息 (逆向自 device_full.txt)
    name: str = ""                                          # 玩家名称
    uuid: str = ""                                          # 玩家 UUID (逆向自 "PlayerUUID")
    xuid: str = ""                                          # Xbox Live ID
    client_guid: str = ""                                   # 客户端 GUID (逆向自 "ClientGUID")
    client_random_id: int = 0                               # 客户端随机 ID (逆向自 "ClientRandomId")
    device_id: str = ""                                     # 设备 ID (逆向自 "DeviceId")
    device_os: int = 15                                     # 设备 OS (逆向自 "DeviceOS")

    # 位置信息
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)  # 位置 (x, y, z)
    rotation: tuple[float, float] = (0.0, 0.0)              # 旋转 (yaw, pitch)
    dimension: int = DIMENSION_OVERWORLD                    # 当前维度
    spawn_point: tuple[int, int, int] = (0, 64, 0)          # 出生点

    # 状态信息
    health: float = 20.0                                    # 血量
    max_health: float = 20.0                                # 最大血量
    food: int = 20                                          # 饥饿值
    saturation: float = 5.0                                 # 饱和度
    oxygen: int = 300                                       # 氧气值
    experience_level: int = 0                               # 经验等级
    experience_progress: float = 0.0                        # 经验进度
    game_mode: int = 0                                      # 游戏模式 (0=生存 1=创造 2=冒险 3=旁观)

    # 连接信息
    server_address: str = ""                                # 服务器地址
    server_port: int = 19132                                # 服务器端口
    ping: int = 0                                           # 延迟 (ms)
    connected_at: float = 0.0                               # 连接时间戳
    last_update: float = field(default_factory=time.time)   # 最后更新时间

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "name": self.name,
            "uuid": self.uuid,
            "xuid": self.xuid,
            "client_guid": self.client_guid,
            "client_random_id": self.client_random_id,
            "device_id": self.device_id,
            "device_os": self.device_os,
            "position": self.position,
            "rotation": self.rotation,
            "dimension": self.dimension,
            "dimension_name": DIMENSION_NAMES.get(self.dimension, "Unknown"),
            "spawn_point": self.spawn_point,
            "health": self.health,
            "max_health": self.max_health,
            "food": self.food,
            "saturation": self.saturation,
            "oxygen": self.oxygen,
            "experience_level": self.experience_level,
            "experience_progress": self.experience_progress,
            "game_mode": self.game_mode,
            "server_address": self.server_address,
            "server_port": self.server_port,
            "ping": self.ping,
            "connected_at": self.connected_at,
            "last_update": self.last_update,
        }

    @property
    def uptime(self) -> float:
        """在线时长 (秒)。"""
        if self.connected_at == 0:
            return 0.0
        return time.time() - self.connected_at

    @property
    def is_alive(self) -> bool:
        """是否存活。"""
        return self.health > 0


@dataclass
class WorldInfo:
    """世界信息。"""

    level_name: str = ""                                    # 世界名称
    game_version: str = DEFAULT_GAME_VERSION                # 游戏版本 (逆向自 "GameVersion")
    difficulty: int = DIFFICULTY_NORMAL                     # 难度
    time_of_day: int = 0                                    # 一天中的时间 (tick)
    world_time: int = 0                                     # 世界时间 (tick)
    spawn_point: tuple[int, int, int] = (0, 64, 0)          # 世界出生点
    world_spawn: tuple[int, int, int] = (0, 64, 0)          # 世界出生点 (别名)

    # 游戏规则
    do_daylight_cycle: bool = True                          # 昼夜循环
    do_mob_spawning: bool = True                            # 生物生成
    do_weather_cycle: bool = True                           # 天气循环
    do_fire_tick: bool = True                               # 火焰传播
    keep_inventory: bool = False                            # 死亡保留物品
    mob_griefing: bool = True                               # 生物破坏
    pvp: bool = True                                        # 玩家对战
    show_coordinates: bool = False                          # 显示坐标
    show_death_messages: bool = True                        # 显示死亡消息

    # 天气
    is_raining: bool = False                                # 是否下雨
    is_thundering: bool = False                             # 是否雷暴
    rain_level: float = 0.0                                 # 雨量
    thunder_level: float = 0.0                              # 雷暴量

    # 更新时间
    last_update: float = field(default_factory=time.time)   # 最后更新时间

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "level_name": self.level_name,
            "game_version": self.game_version,
            "difficulty": self.difficulty,
            "difficulty_name": DIFFICULTY_NAMES.get(self.difficulty, "Unknown"),
            "time_of_day": self.time_of_day,
            "world_time": self.world_time,
            "spawn_point": self.spawn_point,
            "game_rules": {
                "doDaylightCycle": self.do_daylight_cycle,
                "doMobSpawning": self.do_mob_spawning,
                "doWeatherCycle": self.do_weather_cycle,
                "doFireTick": self.do_fire_tick,
                "keepInventory": self.keep_inventory,
                "mobGriefing": self.mob_griefing,
                "pvp": self.pvp,
                "showCoordinates": self.show_coordinates,
                "showDeathMessages": self.show_death_messages,
            },
            "weather": {
                "is_raining": self.is_raining,
                "is_thundering": self.is_thundering,
                "rain_level": self.rain_level,
                "thunder_level": self.thunder_level,
            },
            "last_update": self.last_update,
        }

    @property
    def is_daytime(self) -> bool:
        """是否白天。"""
        return 0 <= self.time_of_day < 13000

    @property
    def is_nighttime(self) -> bool:
        """是否夜晚。"""
        return 13000 <= self.time_of_day < 24000


@dataclass
class ServerInfo:
    """服务器信息。"""

    name: str = ""                                          # 服务器名称
    motd: str = ""                                          # 服务器描述
    version: str = DEFAULT_GAME_VERSION                     # 服务器版本
    protocol_version: int = 0                               # 协议版本
    max_players: int = 20                                   # 最大玩家数
    online_players: int = 0                                 # 在线玩家数
    server_id: str = ""                                     # 服务器 ID

    # 连接信息
    address: str = ""                                       # 服务器地址
    port: int = 19132                                       # 服务器端口

    # 网易特化信息
    is_netease: bool = False                                # 是否网易服务器
    netease_room_id: str = ""                               # 网易房间 ID
    netease_token: str = ""                                 # 网易令牌

    last_update: float = field(default_factory=time.time)   # 最后更新时间

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "name": self.name,
            "motd": self.motd,
            "version": self.version,
            "protocol_version": self.protocol_version,
            "max_players": self.max_players,
            "online_players": self.online_players,
            "server_id": self.server_id,
            "address": self.address,
            "port": self.port,
            "is_netease": self.is_netease,
            "netease_room_id": self.netease_room_id,
            "last_update": self.last_update,
        }


# -------------------------------------------------------------------- #
# BotBasicInfoHolder 核心
# -------------------------------------------------------------------- #


class BotBasicInfoHolder:
    """BotBasicInfoHolder 机器人信息持有者。

    逆向自 NovaBuilder 的 WavesAccess BotBasicInfoHolder。

    功能:
        1. 持有机器人基本信息 (BotBasicInfo)
        2. 持有世界信息 (WorldInfo)
        3. 持有服务器信息 (ServerInfo)
        4. 线程安全的读写访问
        5. 信息变更通知

    使用示例::

        holder = BotBasicInfoHolder()
        holder.set_bot_info(name="MyBot", uuid="...")
        holder.update_position(100.0, 64.0, 200.0)
        info = holder.get_bot_info()
        print(info.name, info.position)
    """

    def __init__(self) -> None:
        """初始化信息持有者。"""
        self._bot_info: BotBasicInfo = BotBasicInfo()
        self._world_info: WorldInfo = WorldInfo()
        self._server_info: ServerInfo = ServerInfo()
        self._lock: threading.RLock = threading.RLock()
        self._change_listeners: list[Callable[[str, Any], None]] = []
        self._known_players: dict[str, dict[str, Any]] = {}

        logger.debug("BotBasicInfoHolder initialized")

    # ---------------------------------------------------------------- #
    # 机器人信息
    # ---------------------------------------------------------------- #

    def get_bot_info(self) -> BotBasicInfo:
        """获取机器人信息。

        Returns:
            :class:`BotBasicInfo` 副本。
        """
        with self._lock:
            return self._bot_info

    def set_bot_info(self, **kwargs: Any) -> None:
        """设置机器人信息字段。

        Args:
            **kwargs: 要更新的字段。
        """
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._bot_info, key):
                    setattr(self._bot_info, key, value)
                else:
                    logger.warning("Unknown bot info field: %s", key)
            self._bot_info.last_update = time.time()

        self._notify_change("bot_info", kwargs)
        logger.debug("Bot info updated: %s", list(kwargs.keys()))

    def update_position(
        self,
        x: float,
        y: float,
        z: float,
        yaw: float | None = None,
        pitch: float | None = None,
    ) -> None:
        """更新位置。

        Args:
            x: X 坐标。
            y: Y 坐标。
            z: Z 坐标。
            yaw: 偏航角。
            pitch: 俯仰角。
        """
        with self._lock:
            self._bot_info.position = (x, y, z)
            if yaw is not None and pitch is not None:
                self._bot_info.rotation = (yaw, pitch)
            self._bot_info.last_update = time.time()

        self._notify_change("position", {"x": x, "y": y, "z": z})

    def update_health(self, health: float, max_health: float | None = None) -> None:
        """更新血量。

        Args:
            health: 当前血量。
            max_health: 最大血量。
        """
        with self._lock:
            self._bot_info.health = health
            if max_health is not None:
                self._bot_info.max_health = max_health
            self._bot_info.last_update = time.time()

        self._notify_change("health", {"health": health, "max_health": max_health})

    def update_dimension(self, dimension: int) -> None:
        """更新维度。

        Args:
            dimension: 维度 ID。
        """
        with self._lock:
            self._bot_info.dimension = dimension
            self._bot_info.last_update = time.time()

        dim_name = DIMENSION_NAMES.get(dimension, "Unknown")
        logger.info("Dimension changed: %s (%s)", dimension, dim_name)
        self._notify_change("dimension", {"dimension": dimension, "name": dim_name})

    def update_game_mode(self, mode: int) -> None:
        """更新游戏模式。

        Args:
            mode: 游戏模式 ID。
        """
        with self._lock:
            self._bot_info.game_mode = mode
            self._bot_info.last_update = time.time()

        self._notify_change("game_mode", {"game_mode": mode})

    def update_ping(self, ping: int) -> None:
        """更新延迟。

        Args:
            ping: 延迟 (ms)。
        """
        with self._lock:
            self._bot_info.ping = ping
            self._bot_info.last_update = time.time()

    def mark_connected(self, server_address: str = "", server_port: int = 19132) -> None:
        """标记已连接。

        Args:
            server_address: 服务器地址。
            server_port: 服务器端口。
        """
        with self._lock:
            self._bot_info.connected_at = time.time()
            self._bot_info.server_address = server_address
            self._bot_info.server_port = server_port
            self._bot_info.last_update = time.time()

        logger.info(
            "Bot connected to %s:%d",
            server_address or "(unknown)", server_port,
        )
        self._notify_change("connected", {
            "address": server_address,
            "port": server_port,
            "timestamp": self._bot_info.connected_at,
        })

    def mark_disconnected(self) -> None:
        """标记已断开。"""
        with self._lock:
            uptime = self._bot_info.uptime
            self._bot_info.connected_at = 0
            self._bot_info.last_update = time.time()

        logger.info("Bot disconnected (uptime: %.0fs)", uptime)
        self._notify_change("disconnected", {"uptime": uptime})

    @property
    def is_connected(self) -> bool:
        """是否已连接。"""
        with self._lock:
            return self._bot_info.connected_at > 0

    @property
    def uptime(self) -> float:
        """在线时长 (秒)。"""
        with self._lock:
            return self._bot_info.uptime

    # ---------------------------------------------------------------- #
    # 世界信息
    # ---------------------------------------------------------------- #

    def get_world_info(self) -> WorldInfo:
        """获取世界信息。

        Returns:
            :class:`WorldInfo`。
        """
        with self._lock:
            return self._world_info

    def set_world_info(self, **kwargs: Any) -> None:
        """设置世界信息字段。

        Args:
            **kwargs: 要更新的字段。
        """
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._world_info, key):
                    setattr(self._world_info, key, value)
                else:
                    logger.warning("Unknown world info field: %s", key)
            self._world_info.last_update = time.time()

        self._notify_change("world_info", kwargs)
        logger.debug("World info updated: %s", list(kwargs.keys()))

    def update_time(self, time_of_day: int, world_time: int | None = None) -> None:
        """更新时间。

        Args:
            time_of_day: 一天中的时间 (tick)。
            world_time: 世界时间 (tick)。
        """
        with self._lock:
            self._world_info.time_of_day = time_of_day
            if world_time is not None:
                self._world_info.world_time = world_time
            self._world_info.last_update = time.time()

    def update_weather(
        self,
        is_raining: bool | None = None,
        is_thundering: bool | None = None,
        rain_level: float | None = None,
        thunder_level: float | None = None,
    ) -> None:
        """更新天气。

        Args:
            is_raining: 是否下雨。
            is_thundering: 是否雷暴。
            rain_level: 雨量。
            thunder_level: 雷暴量。
        """
        with self._lock:
            if is_raining is not None:
                self._world_info.is_raining = is_raining
            if is_thundering is not None:
                self._world_info.is_thundering = is_thundering
            if rain_level is not None:
                self._world_info.rain_level = rain_level
            if thunder_level is not None:
                self._world_info.thunder_level = thunder_level
            self._world_info.last_update = time.time()

        self._notify_change("weather", {
            "is_raining": is_raining,
            "is_thundering": is_thundering,
        })

    def update_game_rule(self, rule_name: str, value: bool) -> None:
        """更新游戏规则。

        Args:
            rule_name: 规则名称 (如 "keepInventory")。
            value: 规则值。
        """
        rule_map: dict[str, str] = {
            "doDaylightCycle": "do_daylight_cycle",
            "doMobSpawning": "do_mob_spawning",
            "doWeatherCycle": "do_weather_cycle",
            "doFireTick": "do_fire_tick",
            "keepInventory": "keep_inventory",
            "mobGriefing": "mob_griefing",
            "pvp": "pvp",
            "showCoordinates": "show_coordinates",
            "showDeathMessages": "show_death_messages",
        }

        attr_name = rule_map.get(rule_name, rule_name)
        with self._lock:
            if hasattr(self._world_info, attr_name):
                setattr(self._world_info, attr_name, value)
                self._world_info.last_update = time.time()
            else:
                logger.warning("Unknown game rule: %s", rule_name)

        self._notify_change("game_rule", {"rule": rule_name, "value": value})

    # ---------------------------------------------------------------- #
    # 服务器信息
    # ---------------------------------------------------------------- #

    def get_server_info(self) -> ServerInfo:
        """获取服务器信息。

        Returns:
            :class:`ServerInfo`。
        """
        with self._lock:
            return self._server_info

    def set_server_info(self, **kwargs: Any) -> None:
        """设置服务器信息字段。

        Args:
            **kwargs: 要更新的字段。
        """
        with self._lock:
            for key, value in kwargs.items():
                if hasattr(self._server_info, key):
                    setattr(self._server_info, key, value)
                else:
                    logger.warning("Unknown server info field: %s", key)
            self._server_info.last_update = time.time()

        self._notify_change("server_info", kwargs)
        logger.debug("Server info updated: %s", list(kwargs.keys()))

    def update_player_count(self, online: int, max_players: int | None = None) -> None:
        """更新在线玩家数。

        Args:
            online: 在线人数。
            max_players: 最大玩家数。
        """
        with self._lock:
            self._server_info.online_players = online
            if max_players is not None:
                self._server_info.max_players = max_players
            self._server_info.last_update = time.time()

    # ---------------------------------------------------------------- #
    # 玩家列表管理
    # ---------------------------------------------------------------- #

    def add_player(self, name: str, info: dict[str, Any]) -> None:
        """添加玩家到已知列表。

        逆向自 "PlayerAddRoom"。

        Args:
            name: 玩家名称。
            info: 玩家信息。
        """
        with self._lock:
            self._known_players[name] = info
        logger.debug("Player added: %s", name)
        self._notify_change("player_add", {"name": name, "info": info})

    def remove_player(self, name: str) -> None:
        """从已知列表移除玩家。

        逆向自 "PlayerRemoveRoom"。

        Args:
            name: 玩家名称。
        """
        with self._lock:
            self._known_players.pop(name, None)
        logger.debug("Player removed: %s", name)
        self._notify_change("player_remove", {"name": name})

    def get_player(self, name: str) -> dict[str, Any] | None:
        """获取玩家信息。

        Args:
            name: 玩家名称。

        Returns:
            玩家信息字典, 不存在返回 None。
        """
        with self._lock:
            return self._known_players.get(name)

    def get_all_players(self) -> dict[str, dict[str, Any]]:
        """获取所有已知玩家。

        Returns:
            玩家名到信息的映射。
        """
        with self._lock:
            return dict(self._known_players)

    def get_player_count(self) -> int:
        """获取已知玩家数。"""
        with self._lock:
            return len(self._known_players)

    def clear_players(self) -> None:
        """清空玩家列表。"""
        with self._lock:
            count = len(self._known_players)
            self._known_players.clear()
        logger.info("Cleared %d players", count)
        self._notify_change("players_cleared", {"count": count})

    # ---------------------------------------------------------------- #
    # 变更监听
    # ---------------------------------------------------------------- #

    def add_change_listener(
        self, listener: Callable[[str, Any], None]
    ) -> None:
        """添加变更监听器。

        Args:
            listener: 监听函数 (change_type, data)。
        """
        with self._lock:
            self._change_listeners.append(listener)
        logger.debug("Added change listener: %s", type(listener).__name__)

    def remove_change_listener(
        self, listener: Callable[[str, Any], None]
    ) -> None:
        """移除变更监听器。

        Args:
            listener: 监听函数。
        """
        with self._lock:
            try:
                self._change_listeners.remove(listener)
            except ValueError:
                pass

    def _notify_change(self, change_type: str, data: Any) -> None:
        """通知变更。

        Args:
            change_type: 变更类型。
            data: 变更数据。
        """
        with self._lock:
            listeners = list(self._change_listeners)

        for listener in listeners:
            try:
                listener(change_type, data)
            except Exception:
                logger.exception("Change listener error for type=%s", change_type)

    # ---------------------------------------------------------------- #
    # 汇总信息
    # ---------------------------------------------------------------- #

    def get_summary(self) -> dict[str, Any]:
        """获取信息汇总。

        Returns:
            包含所有信息的字典。
        """
        with self._lock:
            return {
                "bot": self._bot_info.to_dict(),
                "world": self._world_info.to_dict(),
                "server": self._server_info.to_dict(),
                "known_players": len(self._known_players),
                "uptime": self._bot_info.uptime,
            }

    def reset(self) -> None:
        """重置所有信息。"""
        with self._lock:
            self._bot_info = BotBasicInfo()
            self._world_info = WorldInfo()
            self._server_info = ServerInfo()
            self._known_players.clear()
        logger.info("BotBasicInfoHolder reset")


__all__ = [
    # 常量
    "DEFAULT_GAME_VERSION",
    "DIMENSION_OVERWORLD", "DIMENSION_NETHER", "DIMENSION_END",
    "DIMENSION_NAMES",
    "DIFFICULTY_PEACEFUL", "DIFFICULTY_EASY", "DIFFICULTY_NORMAL", "DIFFICULTY_HARD",
    "DIFFICULTY_NAMES",
    # 枚举
    "Dimension", "Difficulty",
    # 数据结构
    "BotBasicInfo", "WorldInfo", "ServerInfo",
    # 核心
    "BotBasicInfoHolder",
]
