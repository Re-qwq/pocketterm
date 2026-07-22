"""player_kit - PlayerKit 玩家操作能力模块。

逆向自 NovaBuilder 的 WavesAccess PlayerKit, 来源:
    - /workspace/novuilder_reverse/player_options.txt
    - /workspace/novuilder_reverse/strings_commands.txt
    - /workspace/novuilder_reverse/REPORT.txt

PlayerKit 是 WavesAccess 的玩家操作能力组件, 提供高级玩家操作接口:

    1. 方块放置 (setblock / fill)
    2. 物品栏操作 (give / clear / replaceitem)
    3. 玩家传送 (tp)
    4. 聊天消息 (say / tell / msg)
    5. 玩家状态查询 (获取位置、血量等)
    6. 容器交互 (打开箱子、取放物品)

字符串证据 (逆向自 player_options.txt):
    "PlayerAddRoom"          -- 玩家加入房间
    "PlayerRemoveRoom"       -- 玩家离开房间
    "playerOptions"          -- 玩家选项
    "setPlayerOption"        -- 设置玩家选项
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.waves_access.player_kit")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 默认命令超时 (秒)
DEFAULT_TIMEOUT: float = 10.0

#: 物品栏槽位数量
INVENTORY_SIZE: int = 36
HOTBAR_SIZE: int = 9
ARMOR_SIZE: int = 4

#: 槽位类型
SLOT_HOTBAR: str = "slot.hotbar"
SLOT_INVENTORY: str = "slot.inventory"
SLOT_ARMOR: str = "slot.armor"
SLOT_ENDERCHEST: str = "slot.enderchest"
SLOT_EQUIPMENT: str = "slot.equips"

#: 玩家选择器
SELECTOR_SELF: str = "@s"
SELECTOR_ALL_PLAYERS: str = "@a"
SELECTOR_NEAREST_PLAYER: str = "@p"
SELECTOR_RANDOM_PLAYER: str = "@r"
SELECTOR_ALL_ENTITIES: str = "@e"


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class PlayerAction(Enum):
    """玩家操作类型。"""

    SET_BLOCK = auto()         # 设置方块
    FILL_BLOCKS = auto()       # 填充方块
    GIVE_ITEM = auto()         # 给予物品
    CLEAR_ITEM = auto()        # 清除物品
    REPLACE_ITEM = auto()      # 替换物品
    TELEPORT = auto()          # 传送
    SAY = auto()               # 说话
    TELL = auto()              # 私聊
    TITLE = auto()             # 标题
    QUERY_POSITION = auto()    # 查询位置
    QUERY_HEALTH = auto()      # 查询血量
    OPEN_CONTAINER = auto()    # 打开容器
    CLOSE_CONTAINER = auto()   # 关闭容器


class GameMode(Enum):
    """游戏模式。"""

    SURVIVAL = 0       # 生存
    CREATIVE = 1       # 创造
    ADVENTURE = 2      # 冒险
    SPECTATOR = 3      # 旁观


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class PlayerInfo:
    """玩家信息。"""

    name: str = ""                                          # 玩家名称
    uuid: str = ""                                          # 玩家 UUID
    xuid: str = ""                                          # Xbox Live ID
    position: tuple[float, float, float] = (0.0, 0.0, 0.0)  # 位置
    rotation: tuple[float, float] = (0.0, 0.0)              # 旋转 (yaw, pitch)
    health: float = 20.0                                    # 血量
    max_health: float = 20.0                                # 最大血量
    food: int = 20                                          # 饥饿值
    game_mode: GameMode = GameMode.SURVIVAL                 # 游戏模式
    dimension: int = 0                                      # 维度 (0=主世界, 1=下界, 2=末地)
    online: bool = True                                     # 是否在线
    ping: int = 0                                           # 延迟 (ms)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "name": self.name,
            "uuid": self.uuid,
            "xuid": self.xuid,
            "position": self.position,
            "rotation": self.rotation,
            "health": self.health,
            "max_health": self.max_health,
            "food": self.food,
            "game_mode": self.game_mode.name,
            "dimension": self.dimension,
            "online": self.online,
            "ping": self.ping,
        }


@dataclass
class ItemInfo:
    """物品信息。"""

    name: str = ""          # 物品名称
    count: int = 1          # 数量
    data: int = 0           # 数据值
    nbt: dict[str, Any] = field(default_factory=dict)  # NBT 数据
    slot: int = -1          # 槽位

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "name": self.name,
            "count": self.count,
            "data": self.data,
            "nbt": self.nbt,
            "slot": self.slot,
        }


@dataclass
class ActionResult:
    """操作结果。"""

    success: bool = False
    action: PlayerAction = PlayerAction.SET_BLOCK
    command: str = ""
    result_data: Any = None
    error: str = ""
    time_used: float = 0.0


# -------------------------------------------------------------------- #
# PlayerKit 核心
# -------------------------------------------------------------------- #


class PlayerKit:
    """PlayerKit 玩家操作能力。

    逆向自 NovaBuilder 的 WavesAccess PlayerKit。

    提供高级玩家操作接口, 封装底层 GameInterface 和 CmdSender。

    功能:
        1. 方块操作 (setblock / fill)
        2. 物品栏操作 (give / clear / replaceitem)
        3. 玩家传送 (tp)
        4. 聊天消息 (say / tell / title)
        5. 玩家状态查询
        6. 容器交互
        7. 游戏模式切换

    使用示例::

        kit = PlayerKit(cmd_sender=sender)
        kit.set_block((100, 64, 200), "minecraft:stone")
        kit.give_item("minecraft:diamond", count=64)
        kit.teleport((100, 64, 200))
        kit.say("Hello world!")
    """

    def __init__(
        self,
        cmd_sender: Any | None = None,
        game_interface: Any | None = None,
    ) -> None:
        """初始化 PlayerKit。

        Args:
            cmd_sender: 命令发送器。
            game_interface: 游戏接口。
        """
        self._cmd_sender = cmd_sender
        self._game_interface = game_interface
        self._self_info: PlayerInfo = PlayerInfo(name="")
        self._known_players: dict[str, PlayerInfo] = {}
        self._stats: dict[str, int] = {
            "total_actions": 0,
            "successful_actions": 0,
            "failed_actions": 0,
        }

        logger.debug("PlayerKit initialized")

    @property
    def cmd_sender(self) -> Any | None:
        """命令发送器。"""
        return self._cmd_sender

    @cmd_sender.setter
    def cmd_sender(self, value: Any) -> None:
        self._cmd_sender = value

    @property
    def game_interface(self) -> Any | None:
        """游戏接口。"""
        return self._game_interface

    @game_interface.setter
    def game_interface(self, value: Any) -> None:
        self._game_interface = value

    @property
    def self_info(self) -> PlayerInfo:
        """自身玩家信息。"""
        return self._self_info

    @property
    def stats(self) -> dict[str, int]:
        """统计信息。"""
        return dict(self._stats)

    # ---------------------------------------------------------------- #
    # 内部方法
    # ---------------------------------------------------------------- #

    def _send_command(self, command: str, timeout: float = DEFAULT_TIMEOUT) -> bool:
        """发送命令并检查结果。

        Args:
            command: 命令字符串。
            timeout: 超时。

        Returns:
            True 如果成功。
        """
        self._stats["total_actions"] += 1
        try:
            if self._cmd_sender:
                output = self._cmd_sender.send_command_with_resp(command, timeout=timeout)
                if output and output.success:
                    self._stats["successful_actions"] += 1
                    return True
                else:
                    self._stats["failed_actions"] += 1
                    error = output.error if output else "no response"
                    logger.warning("Command failed: %s: %s", command, error)
                    return False
            else:
                logger.error("No command sender configured")
                self._stats["failed_actions"] += 1
                return False
        except Exception as exc:
            self._stats["failed_actions"] += 1
            logger.exception("Command exception: %s: %s", command, exc)
            return False

    def _ensure_minecraft_prefix(self, name: str) -> str:
        """确保方块/物品名有 minecraft: 前缀。"""
        if name.startswith("minecraft:"):
            return name
        return f"minecraft:{name}"

    # ---------------------------------------------------------------- #
    # 方块操作
    # ---------------------------------------------------------------- #

    def set_block(
        self,
        position: tuple[int, int, int],
        block_name: str,
        block_states: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """设置方块。

        Args:
            position: 坐标。
            block_name: 方块名称。
            block_states: 方块状态。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if self._game_interface:
            result = self._game_interface.set_block(
                position, block_name, block_states, timeout
            )
            return ActionResult(
                success=result.success,
                action=PlayerAction.SET_BLOCK,
                command=result.command_used,
                error=result.error,
                time_used=result.time_used,
            )

        block_name = self._ensure_minecraft_prefix(block_name)
        x, y, z = position
        command = f"setblock {x} {y} {z} {block_name}"
        if block_states:
            command += f" {block_states}"

        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.SET_BLOCK,
            command=command,
            time_used=time.time() - start_time,
        )

    def fill_blocks(
        self,
        pos1: tuple[int, int, int],
        pos2: tuple[int, int, int],
        block_name: str,
        block_states: str = "",
        mode: str = "replace",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """填充方块区域。

        Args:
            pos1: 起点。
            pos2: 终点。
            block_name: 方块名称。
            block_states: 方块状态。
            mode: 填充模式。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if self._game_interface:
            result = self._game_interface.fill_blocks(
                pos1, pos2, block_name, block_states, mode, timeout
            )
            return ActionResult(
                success=result.success,
                action=PlayerAction.FILL_BLOCKS,
                command=result.command_used,
                error=result.error,
                time_used=result.time_used,
            )

        block_name = self._ensure_minecraft_prefix(block_name)
        x1, y1, z1 = pos1
        x2, y2, z2 = pos2
        command = f"fill {x1} {y1} {z1} {x2} {y2} {z2} {block_name}"
        if block_states:
            command += f" {block_states}"
        if mode and mode != "replace":
            command += f" {mode}"

        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.FILL_BLOCKS,
            command=command,
            time_used=time.time() - start_time,
        )

    # ---------------------------------------------------------------- #
    # 物品栏操作
    # ---------------------------------------------------------------- #

    def give_item(
        self,
        item_name: str,
        count: int = 1,
        data: int = 0,
        target: str = SELECTOR_SELF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """给予物品。

        Args:
            item_name: 物品名称。
            count: 数量。
            data: 数据值。
            target: 目标选择器。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        item_name = self._ensure_minecraft_prefix(item_name)
        command = f"give {target} {item_name} {count}"
        if data:
            command += f" {data}"

        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.GIVE_ITEM,
            command=command,
            time_used=time.time() - start_time,
        )

    def clear_item(
        self,
        item_name: str = "",
        data: int = -1,
        max_count: int = -1,
        target: str = SELECTOR_SELF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """清除物品。

        Args:
            item_name: 物品名称 (空表示清除所有)。
            data: 数据值 (-1 表示所有)。
            max_count: 最大数量 (-1 表示所有)。
            target: 目标选择器。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if item_name:
            item_name = self._ensure_minecraft_prefix(item_name)
            command = f"clear {target} {item_name}"
            if data >= 0:
                command += f" {data}"
                if max_count >= 0:
                    command += f" {max_count}"
        else:
            command = f"clear {target}"

        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.CLEAR_ITEM,
            command=command,
            time_used=time.time() - start_time,
        )

    def replace_item(
        self,
        slot_type: str,
        slot: int,
        item_name: str,
        count: int = 1,
        data: int = 0,
        target: str = SELECTOR_SELF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """替换物品栏物品。

        Args:
            slot_type: 槽位类型。
            slot: 槽位索引。
            item_name: 物品名称。
            count: 数量。
            data: 数据值。
            target: 目标选择器。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if self._game_interface:
            success = self._game_interface.replaceitem(
                slot_type, slot, item_name, count, data, timeout
            )
            command = f"replaceitem entity {target} {slot_type} {slot} {item_name} {count}"
            return ActionResult(
                success=success,
                action=PlayerAction.REPLACE_ITEM,
                command=command,
            )

        item_name = self._ensure_minecraft_prefix(item_name)
        command = f"replaceitem entity {target} {slot_type} {slot} {item_name} {count}"
        if data:
            command += f" {data}"

        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.REPLACE_ITEM,
            command=command,
            time_used=time.time() - start_time,
        )

    # ---------------------------------------------------------------- #
    # 传送
    # ---------------------------------------------------------------- #

    def teleport(
        self,
        destination: tuple[float, float, float] | str,
        target: str = SELECTOR_SELF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """传送玩家。

        Args:
            destination: 目标 (坐标或玩家名)。
            target: 要传送的玩家选择器。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if isinstance(destination, tuple):
            x, y, z = destination
            command = f"tp {target} {x} {y} {z}"
        else:
            command = f"tp {target} {destination}"

        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.TELEPORT,
            command=command,
            time_used=time.time() - start_time,
        )

    def teleport_to_player(
        self,
        player_name: str,
        target: str = SELECTOR_SELF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """传送到指定玩家。

        Args:
            player_name: 目标玩家名。
            target: 要传送的玩家选择器。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        return self.teleport(player_name, target, timeout)

    # ---------------------------------------------------------------- #
    # 聊天消息
    # ---------------------------------------------------------------- #

    def say(self, message: str, timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
        """发送公共消息 (say 命令)。

        Args:
            message: 消息内容。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        command = f"say {message}"
        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.SAY,
            command=command,
            time_used=time.time() - start_time,
        )

    def tell(self, target: str, message: str, timeout: float = DEFAULT_TIMEOUT) -> ActionResult:
        """发送私聊消息 (tell 命令)。

        Args:
            target: 目标玩家。
            message: 消息内容。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        command = f"tell {target} {message}"
        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.TELL,
            command=command,
            time_used=time.time() - start_time,
        )

    def title(
        self,
        target: str,
        text: str,
        title_type: str = "title",
        fade_in: int = 10,
        stay: int = 70,
        fade_out: int = 20,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ActionResult:
        """显示标题。

        Args:
            target: 目标选择器。
            text: 标题文本。
            title_type: 类型 (title/subtitle/actionbar)。
            fade_in: 淡入时间 (tick)。
            stay: 停留时间 (tick)。
            fade_out: 淡出时间 (tick)。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if title_type in ("title", "subtitle", "actionbar"):
            command = f"title {target} {title_type} {text}"
        else:
            command = f"title {target} {title_type} {fade_in} {stay} {fade_out}"

        start_time = time.time()
        success = self._send_command(command, timeout)
        return ActionResult(
            success=success,
            action=PlayerAction.TITLE,
            command=command,
            time_used=time.time() - start_time,
        )

    # ---------------------------------------------------------------- #
    # 游戏模式
    # ---------------------------------------------------------------- #

    def set_game_mode(
        self,
        mode: GameMode | int | str,
        target: str = SELECTOR_SELF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bool:
        """设置游戏模式。

        Args:
            mode: 游戏模式。
            target: 目标选择器。
            timeout: 超时。

        Returns:
            True 如果成功。
        """
        if isinstance(mode, GameMode):
            mode_str = mode.name.lower()
        elif isinstance(mode, int):
            mode_str = GameMode(mode).name.lower()
        else:
            mode_str = str(mode).lower()

        command = f"gamemode {mode_str} {target}"
        return self._send_command(command, timeout)

    # ---------------------------------------------------------------- #
    # 玩家状态查询
    # ---------------------------------------------------------------- #

    def query_position(
        self,
        target: str = SELECTOR_SELF,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> tuple[float, float, float] | None:
        """查询玩家位置。

        Args:
            target: 目标选择器。
            timeout: 超时。

        Returns:
            (x, y, z) 坐标, 失败返回 None。
        """
        command = f"execute {target} ~ ~ ~ detect ~ ~ ~ air 0 queryposition"
        if self._cmd_sender:
            output = self._cmd_sender.send_command_with_resp(
                f"testfor {target}", timeout=timeout
            )
            if output and output.success:
                # 实际位置需要从数据包获取
                if target == SELECTOR_SELF:
                    return self._self_info.position
        return None

    def query_health(
        self,
        target: str = SELECTOR_SELF,
    ) -> float:
        """查询玩家血量。

        Args:
            target: 目标选择器。

        Returns:
            血量值。
        """
        if target == SELECTOR_SELF:
            return self._self_info.health
        return 0.0

    def get_self_position(self) -> tuple[float, float, float]:
        """获取自身位置。

        Returns:
            (x, y, z) 坐标。
        """
        return self._self_info.position

    def update_self_info(self, info: PlayerInfo | dict[str, Any]) -> None:
        """更新自身信息。

        Args:
            info: 玩家信息。
        """
        if isinstance(info, dict):
            self._self_info = PlayerInfo(
                name=info.get("name", self._self_info.name),
                uuid=info.get("uuid", self._self_info.uuid),
                position=info.get("position", self._self_info.position),
                rotation=info.get("rotation", self._self_info.rotation),
                health=info.get("health", self._self_info.health),
                food=info.get("food", self._self_info.food),
                game_mode=GameMode[info.get("game_mode", "SURVIVAL")],
                dimension=info.get("dimension", 0),
            )
        else:
            self._self_info = info
        logger.debug("Self info updated: pos=%s", self._self_info.position)

    # ---------------------------------------------------------------- #
    # 玩家列表管理
    # ---------------------------------------------------------------- #

    def add_known_player(self, info: PlayerInfo) -> None:
        """添加已知玩家。

        Args:
            info: 玩家信息。
        """
        self._known_players[info.name] = info
        logger.debug("Added known player: %s", info.name)

    def remove_known_player(self, name: str) -> None:
        """移除已知玩家。

        Args:
            name: 玩家名称。
        """
        self._known_players.pop(name, None)
        logger.debug("Removed known player: %s", name)

    def get_known_player(self, name: str) -> PlayerInfo | None:
        """获取已知玩家。

        Args:
            name: 玩家名称。

        Returns:
            :class:`PlayerInfo`, 不存在返回 None。
        """
        return self._known_players.get(name)

    def get_all_known_players(self) -> list[PlayerInfo]:
        """获取所有已知玩家。

        Returns:
            玩家信息列表。
        """
        return list(self._known_players.values())

    def clear_known_players(self) -> None:
        """清空已知玩家列表。"""
        count = len(self._known_players)
        self._known_players.clear()
        logger.info("Cleared %d known players", count)

    # ---------------------------------------------------------------- #
    # 容器交互
    # ---------------------------------------------------------------- #

    def open_container(
        self,
        position: tuple[int, int, int],
        container_type: int = 0,
        timeout: float = 15.0,
    ) -> ActionResult:
        """打开容器。

        Args:
            position: 容器位置。
            container_type: 容器类型。
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if self._game_interface:
            result = self._game_interface.container_open(position, container_type, timeout)
            return ActionResult(
                success=result.success,
                action=PlayerAction.OPEN_CONTAINER,
                error=result.error,
                time_used=result.time_used,
                result_data=result,
            )
        return ActionResult(
            success=False,
            action=PlayerAction.OPEN_CONTAINER,
            error="no game interface",
        )

    def close_container(self, timeout: float = 15.0) -> ActionResult:
        """关闭容器。

        Args:
            timeout: 超时。

        Returns:
            :class:`ActionResult`。
        """
        if self._game_interface:
            result = self._game_interface.container_close(timeout)
            return ActionResult(
                success=result.success,
                action=PlayerAction.CLOSE_CONTAINER,
                error=result.error,
                time_used=result.time_used,
            )
        return ActionResult(
            success=False,
            action=PlayerAction.CLOSE_CONTAINER,
            error="no game interface",
        )

    # ---------------------------------------------------------------- #
    # 便捷操作
    # ---------------------------------------------------------------- #

    def place_block_at_self(
        self,
        block_name: str,
        offset: tuple[int, int, int] = (0, 0, 0),
    ) -> ActionResult:
        """在自身位置放置方块。

        Args:
            block_name: 方块名称。
            offset: 偏移量。

        Returns:
            :class:`ActionResult`。
        """
        x, y, z = self._self_info.position
        ox, oy, oz = offset
        return self.set_block((int(x + ox), int(y + oy), int(z + oz)), block_name)

    def fill_floor(
        self,
        center: tuple[int, int, int],
        radius: int,
        block_name: str,
    ) -> ActionResult:
        """填充地板。

        以中心点为中心, 填充指定半径的方形区域。

        Args:
            center: 中心点。
            radius: 半径。
            block_name: 方块名称。

        Returns:
            :class:`ActionResult`。
        """
        cx, cy, cz = center
        pos1 = (cx - radius, cy, cz - radius)
        pos2 = (cx + radius, cy, cz + radius)
        return self.fill_blocks(pos1, pos2, block_name)


__all__ = [
    # 常量
    "DEFAULT_TIMEOUT", "INVENTORY_SIZE", "HOTBAR_SIZE", "ARMOR_SIZE",
    "SLOT_HOTBAR", "SLOT_INVENTORY", "SLOT_ARMOR",
    "SLOT_ENDERCHEST", "SLOT_EQUIPMENT",
    "SELECTOR_SELF", "SELECTOR_ALL_PLAYERS", "SELECTOR_NEAREST_PLAYER",
    "SELECTOR_RANDOM_PLAYER", "SELECTOR_ALL_ENTITIES",
    # 枚举
    "PlayerAction", "GameMode",
    # 数据结构
    "PlayerInfo", "ItemInfo", "ActionResult",
    # 核心
    "PlayerKit",
]
