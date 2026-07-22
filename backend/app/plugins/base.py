"""PocketTerm 插件基类、事件系统、上下文与数据目录

本模块是插件系统的核心抽象层，定义了所有语言插件共享的契约:

    - :class:`PluginEvent`       事件名称常量
    - :class:`PluginBase`        插件抽象基类（所有 Python 插件直接继承；
                                  Go / Java 加载器生成的包装器也实现该接口）
    - :class:`PluginContext`     提供给插件访问机器人能力的上下文
    - :class:`PluginDataFolder`  管理插件的数据文件（``datas.json``）、
                                  日志（``logs/``）与配置（``config.yaml``）

设计要点
--------
1. **事件驱动** —— 插件通过 :meth:`PluginBase.register_event` 注册回调，
   管理器在事件发生时调用 :meth:`PluginBase.dispatch_event` 分发。
   回调可以是同步函数或协程函数，分发器会自动判断。

2. **上下文隔离** —— 插件不直接持有机器人引用，而是通过
   :class:`PluginContext` 访问受限能力（发命令、发聊天、查询玩家等）。
   这使得同一插件可为不同机器人复用，也便于权限控制。

3. **进程透明** —— 对 Python 插件，事件是进程内函数调用；对 Go / Java
   插件，加载器构造的 :class:`SubprocessPluginBase` 子类会把事件序列化
   为 JSON 行通过 stdin 发给子进程。对管理器而言两者接口一致。
"""
from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - yaml 在 requirements 中，防御性处理
    yaml = None  # type: ignore

from .models import PluginInfo, PluginStatus

if TYPE_CHECKING:  # 仅用于类型注解，避免运行时循环导入
    from app.bot.bot import PocketBot

logger = logging.getLogger("pocketterm.plugins.base")


# ======================================================================
# 事件系统
# ======================================================================


class PluginEvent:
    """插件事件名称常量。

    管理器与插件之间约定的标准事件。所有事件回调均接收一个 ``data``
    字典参数（事件相关数据），部分事件会在 ``data`` 中携带额外字段。

    事件列表:
        - ``PLUGIN_LOAD``     插件被加载后触发，``data={}``
        - ``PLUGIN_UNLOAD``   插件被卸载前触发，``data={}``
        - ``PLAYER_JOIN``     玩家加入服务器，``data={"player": "<name>"}``
        - ``PLAYER_LEAVE``    玩家离开服务器，``data={"player": "<name>"}``
        - ``CHAT``            收到聊天消息，``data={"sender","message","is_system"}``
        - ``COMMAND``         收到命令，``data={"sender","command"}``
        - ``PACKET``          收到原始数据包，``data={"packet_id","payload"}``
        - ``TICK``            主循环心跳，``data={"delta","timestamp"}``
    """

    PLUGIN_LOAD = "plugin_load"
    PLUGIN_UNLOAD = "plugin_unload"
    PLAYER_JOIN = "player_join"
    PLAYER_LEAVE = "player_leave"
    CHAT = "chat"
    COMMAND = "command"
    PACKET = "packet"
    TICK = "tick"

    #: 全部标准事件集合，用于校验。
    ALL: List[str] = [
        PLUGIN_LOAD,
        PLUGIN_UNLOAD,
        PLAYER_JOIN,
        PLAYER_LEAVE,
        CHAT,
        COMMAND,
        PACKET,
        TICK,
    ]


# 回调类型：同步函数或协程函数，接收任意参数。
PluginCallback = Callable[..., Any]


# ======================================================================
# 插件数据目录管理
# ======================================================================


class PluginDataFolder:
    """管理单个插件的数据文件、日志与配置。

    每个插件拥有独立的数据目录结构::

        <data_folder>/
        ├── datas.json      # 插件持久化数据（键值对）
        ├── config.yaml     # 插件配置
        └── logs/
            └── plugin.log  # 插件运行日志（追加写入）

    本类提供线程安全的读写接口，所有方法在文件缺失时返回安全默认值
    而非抛出异常，便于插件直接使用。

    Args:
        data_folder: 插件数据目录绝对路径。
        log_file: 插件日志文件绝对路径（通常为
            ``<data_folder>/logs/plugin.log``）。
    """

    #: 日志文件最大保留条数（防止无限增长）。
    MAX_LOG_ENTRIES: int = 2000

    def __init__(self, data_folder: Union[str, Path], log_file: Union[str, Path]) -> None:
        self.data_folder: Path = Path(data_folder)
        self.log_file: Path = Path(log_file)
        self._logs_dir: Path = self.log_file.parent

        # 确保目录存在
        self.data_folder.mkdir(parents=True, exist_ok=True)
        self._logs_dir.mkdir(parents=True, exist_ok=True)

        # 数据与配置文件路径
        self._data_file: Path = self.data_folder / "datas.json"
        self._config_file: Path = self.data_folder / "config.yaml"

        # 内存缓存（懒加载）
        self._data_cache: Optional[Dict[str, Any]] = None
        self._config_cache: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    # 数据文件 (datas.json)
    # ------------------------------------------------------------------ #

    def read_data(self) -> Dict[str, Any]:
        """读取插件数据（``datas.json``）。

        文件不存在或损坏时返回空字典。结果会被缓存，后续调用直接返回缓存。
        """
        if self._data_cache is not None:
            return self._data_cache
        if not self._data_file.exists():
            self._data_cache = {}
            return self._data_cache
        try:
            with open(self._data_file, "r", encoding="utf-8") as handle:
                self._data_cache = json.load(handle)
                if not isinstance(self._data_cache, dict):
                    self._data_cache = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"读取插件数据失败 {self._data_file}: {exc}")
            self._data_cache = {}
        return self._data_cache

    def write_data(self, data: Dict[str, Any]) -> bool:
        """写入插件数据（覆盖 ``datas.json``）。

        Args:
            data: 要持久化的键值对字典。

        Returns:
            ``True`` 写入成功;``False`` 写入失败。
        """
        try:
            self._data_cache = dict(data)
            with open(self._data_file, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            return True
        except (TypeError, OSError) as exc:
            logger.error(f"写入插件数据失败 {self._data_file}: {exc}")
            return False

    def set_data(self, key: str, value: Any) -> bool:
        """设置单个数据键并持久化。"""
        data = self.read_data()
        data[key] = value
        return self.write_data(data)

    def get_data(self, key: str, default: Any = None) -> Any:
        """读取单个数据键。"""
        return self.read_data().get(key, default)

    # ------------------------------------------------------------------ #
    # 配置文件 (config.yaml)
    # ------------------------------------------------------------------ #

    def read_config(self) -> Dict[str, Any]:
        """读取插件配置（``config.yaml``）。

        文件不存在或 yaml 未安装 / 损坏时返回空字典。结果会被缓存。
        """
        if self._config_cache is not None:
            return self._config_cache
        if not self._config_file.exists() or yaml is None:
            self._config_cache = {}
            return self._config_cache
        try:
            with open(self._config_file, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
                self._config_cache = loaded if isinstance(loaded, dict) else {}
        except (yaml.YAMLError, OSError) as exc:
            logger.warning(f"读取插件配置失败 {self._config_file}: {exc}")
            self._config_cache = {}
        return self._config_cache

    def write_config(self, config: Dict[str, Any]) -> bool:
        """写入插件配置（覆盖 ``config.yaml``）。"""
        if yaml is None:
            logger.error("PyYAML 未安装，无法写入配置")
            return False
        try:
            self._config_cache = dict(config)
            with open(self._config_file, "w", encoding="utf-8") as handle:
                yaml.safe_dump(
                    config,
                    handle,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            return True
        except (yaml.YAMLError, OSError) as exc:
            logger.error(f"写入插件配置失败 {self._config_file}: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # 日志 (logs/plugin.log)
    # ------------------------------------------------------------------ #

    def write_log(self, level: str, message: str) -> bool:
        """追加一条日志到插件日志文件。

        每行格式: ``[YYYY-MM-DD HH:MM:SS] [LEVEL] message``

        Args:
            level: 日志级别（``"info"`` / ``"warning"`` / ``"error"`` / ``"debug"``）。
            message: 日志内容。

        Returns:
            ``True`` 写入成功。
        """
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] [{level.upper()}] {message}\n"
        try:
            with open(self.log_file, "a", encoding="utf-8") as handle:
                handle.write(line)
            return True
        except OSError as exc:
            logger.error(f"写入插件日志失败 {self.log_file}: {exc}")
            return False

    def read_logs(self, limit: int = 200) -> List[str]:
        """读取最近的插件日志行。

        Args:
            limit: 返回最近 N 行。

        Returns:
            日志行列表（最早的在前面），文件不存在时返回空列表。
        """
        if not self.log_file.exists():
            return []
        try:
            with open(self.log_file, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
            return [line.rstrip("\n") for line in lines[-limit:]]
        except OSError as exc:
            logger.warning(f"读取插件日志失败 {self.log_file}: {exc}")
            return []

    def clear_logs(self) -> bool:
        """清空插件日志文件。"""
        try:
            with open(self.log_file, "w", encoding="utf-8") as handle:
                handle.write("")
            return True
        except OSError as exc:
            logger.error(f"清空插件日志失败 {self.log_file}: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # 通用文件操作
    # ------------------------------------------------------------------ #

    def get_file_path(self, relative_path: str) -> Path:
        """返回数据目录下指定相对路径的绝对路径（不做存在性检查）。"""
        return (self.data_folder / relative_path).resolve()

    def read_file(self, relative_path: str, binary: bool = False) -> Optional[Union[str, bytes]]:
        """读取数据目录下的文件。

        Args:
            relative_path: 相对数据目录的路径。
            binary: 是否以二进制模式读取。

        Returns:
            文件内容;文件不存在或读取失败时返回 ``None``。
        """
        path = self.get_file_path(relative_path)
        try:
            mode = "rb" if binary else "r"
            with open(path, mode, encoding=None if binary else "utf-8") as handle:
                return handle.read()
        except OSError as exc:
            logger.warning(f"读取插件文件失败 {path}: {exc}")
            return None

    def write_file(self, relative_path: str, content: Union[str, bytes]) -> bool:
        """写入数据目录下的文件（自动创建父目录）。"""
        path = self.get_file_path(relative_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "wb" if isinstance(content, bytes) else "w"
            with open(path, mode, encoding=None if isinstance(content, bytes) else "utf-8") as handle:
                handle.write(content)
            return True
        except OSError as exc:
            logger.error(f"写入插件文件失败 {path}: {exc}")
            return False

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"PluginDataFolder(data_folder={self.data_folder!s})"


# ======================================================================
# 插件上下文
# ======================================================================


class PluginContext:
    """提供给插件访问机器人能力的上下文。

    插件通过 ``self.context`` 访问本类实例，从而与宿主机器人交互。
    所有改变游戏状态的方法（``send_command`` / ``send_chat`` / ``say_to``）
    均为协程，需在异步环境中 ``await``。

    上下文在 ``bot`` 为 ``None`` 时所有写操作安全降级（返回 ``False`` /
    空集合），使得插件可在「未绑定机器人」的情况下被加载与测试。

    Args:
        bot: 宿主机器人实例（``PocketBot``）。可为 ``None``。
        data_folder: 插件数据目录管理器。
        plugin_info: 插件元信息。
    """

    def __init__(
        self,
        bot: Optional["PocketBot"] = None,
        data_folder: Optional[PluginDataFolder] = None,
        plugin_info: Optional[PluginInfo] = None,
    ) -> None:
        self._bot = bot
        self._data_folder = data_folder
        self._plugin_info = plugin_info

    # ------------------------------------------------------------------ #
    # 绑定管理
    # ------------------------------------------------------------------ #

    def bind_bot(self, bot: "PocketBot") -> None:
        """绑定（或切换）宿主机器人。"""
        self._bot = bot

    def unbind_bot(self) -> None:
        """解绑宿主机器人。"""
        self._bot = None

    @property
    def bot(self) -> Optional["PocketBot"]:
        """当前绑定的机器人实例（可能为 ``None``）。"""
        return self._bot

    @property
    def has_bot(self) -> bool:
        """是否已绑定机器人。"""
        return self._bot is not None

    @property
    def data_folder(self) -> Optional[PluginDataFolder]:
        """插件数据目录管理器。"""
        return self._data_folder

    @property
    def plugin_info(self) -> Optional[PluginInfo]:
        """插件元信息。"""
        return self._plugin_info

    # ------------------------------------------------------------------ #
    # 游戏操作（异步）
    # ------------------------------------------------------------------ #

    async def send_command(self, command: str) -> bool:
        """向游戏发送命令。

        Args:
            command: 命令字符串（如 ``"time set day"`` 或 ``"/say hi"``）。

        Returns:
            ``True`` 发送成功;``False`` 未绑定机器人或未连接。
        """
        if self._bot is None:
            return False
        try:
            return await self._bot.send_command(command)
        except Exception as exc:
            logger.error(f"插件 send_command 失败: {exc}")
            return False

    async def send_chat(self, message: str) -> bool:
        """发送聊天消息（不以 ``/`` 开头时自动加 ``/say``）。"""
        if self._bot is None:
            return False
        try:
            return await self._bot.send_chat(message)
        except Exception as exc:
            logger.error(f"插件 send_chat 失败: {exc}")
            return False

    async def say_to(self, target: str, message: str) -> bool:
        """向指定玩家发送私聊。"""
        if self._bot is None:
            return False
        try:
            return await self._bot.say_to(target, message)
        except Exception as exc:
            logger.error(f"插件 say_to 失败: {exc}")
            return False

    async def move_to(self, x: float, y: float, z: float) -> bool:
        """移动机器人到指定坐标。"""
        if self._bot is None:
            return False
        try:
            return await self._bot.move_to(x, y, z)
        except Exception as exc:
            logger.error(f"插件 move_to 失败: {exc}")
            return False

    # ------------------------------------------------------------------ #
    # 查询接口（同步）
    # ------------------------------------------------------------------ #

    def get_bot(self) -> Optional[Dict[str, Any]]:
        """获取宿主机器人信息字典。

        Returns:
            ``BotInfo.to_dict()`` 结果;未绑定时返回 ``None``。
        """
        if self._bot is None:
            return None
        try:
            return self._bot.info.to_dict()
        except Exception as exc:
            logger.error(f"插件 get_bot 失败: {exc}")
            return None

    def get_bot_name(self) -> str:
        """获取机器人名称;未绑定时返回空串。"""
        if self._bot is None:
            return ""
        return getattr(self._bot, "name", "") or ""

    def get_bot_id(self) -> str:
        """获取机器人 ID;未绑定时返回空串。"""
        if self._bot is None:
            return ""
        return getattr(self._bot, "bot_id", "") or ""

    def get_players(self) -> List[str]:
        """获取在线玩家名称列表;未绑定时返回空列表。"""
        if self._bot is None:
            return []
        try:
            return list(self._bot.info.player_list)
        except Exception:
            return []

    def get_player(self, name: str) -> Optional[Dict[str, Any]]:
        """查询单个玩家信息。

        当前实现在线玩家仅维护名称列表，因此返回 ``{"name": ...}``。
        若机器人信息中存有更详细的玩家数据可在此扩展。

        Args:
            name: 玩家名称。

        Returns:
            玩家信息字典;玩家不在线或未绑定时返回 ``None``。
        """
        if self._bot is None:
            return None
        players = self.get_players()
        if name not in players:
            return None
        return {"name": name, "online": True}

    def get_chat_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        """获取最近的聊天历史。"""
        if self._bot is None:
            return []
        try:
            return self._bot.get_chat_history(limit)
        except Exception:
            return []

    def get_position(self) -> tuple:
        """获取机器人坐标 ``(x, y, z)``;未绑定时返回 ``(0.0, 0.0, 0.0)``。"""
        if self._bot is None:
            return (0.0, 0.0, 0.0)
        try:
            return tuple(self._bot.info.position)
        except Exception:
            return (0.0, 0.0, 0.0)

    def get_health(self) -> float:
        """获取机器人生命值;未绑定时返回 ``0.0``。"""
        if self._bot is None:
            return 0.0
        try:
            return float(self._bot.info.health)
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ #
    # 日志便捷方法
    # ------------------------------------------------------------------ #

    def log(self, level: str, message: str) -> bool:
        """写入插件日志文件。

        同时通过 Python ``logging`` 输出到主日志，便于控制台查看。
        """
        log_msg = (
            f"[plugin:{self._plugin_info.plugin_id if self._plugin_info else 'unknown'}] "
            f"{message}"
        )
        level_upper = (level or "info").upper()
        if level_upper == "ERROR":
            logger.error(log_msg)
        elif level_upper == "WARNING":
            logger.warning(log_msg)
        elif level_upper == "DEBUG":
            logger.debug(log_msg)
        else:
            logger.info(log_msg)

        if self._data_folder is not None:
            return self._data_folder.write_log(level, message)
        return False

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        bot_id = self.get_bot_id() or "none"
        return f"PluginContext(bot={bot_id})"


# ======================================================================
# 插件抽象基类
# ======================================================================


class PluginBase(abc.ABC):
    """插件抽象基类。

    所有 Python 插件应继承本类并实现 :meth:`on_load` / :meth:`on_unload`。
    Go / Java 加载器会构造本类的子类（进程包装器），将方法调用代理到子进程。

    子类典型写法::

        from app.plugins.base import PluginBase, PluginEvent

        class MyPlugin(PluginBase):
            async def on_load(self) -> bool:
                self.context.log("info", "Hello!")
                self.register_event(PluginEvent.PLAYER_JOIN, self.on_join)
                return True

            async def on_join(self, data: dict) -> None:
                await self.context.send_chat(f"欢迎 {data['player']}!")

    事件分发流程:
        1. 管理器调用 ``await plugin.dispatch_event(PluginEvent.CHAT, data)``
        2. ``dispatch_event`` 调用 ``on_event``（子类可重写以转发到子进程）
        3. 默认 ``on_event`` 触发通过 ``register_event`` 注册的回调

    Args:
        info: 插件元信息。
        context: 插件上下文（提供机器人访问能力）。
    """

    def __init__(self, info: PluginInfo, context: Optional[PluginContext] = None) -> None:
        self.info: PluginInfo = info
        self.context: PluginContext = context or PluginContext(plugin_info=info)
        self._event_handlers: Dict[str, List[PluginCallback]] = {}
        self._loaded: bool = False

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def plugin_id(self) -> str:
        """插件唯一 ID。"""
        return self.info.plugin_id

    @property
    def name(self) -> str:
        """插件显示名称。"""
        return self.info.name

    @property
    def is_loaded(self) -> bool:
        """插件是否已加载。"""
        return self._loaded

    # ------------------------------------------------------------------ #
    # 事件注册与分发
    # ------------------------------------------------------------------ #

    def register_event(self, event: str, callback: PluginCallback) -> None:
        """注册事件回调。

        Args:
            event: 事件名称（使用 :class:`PluginEvent` 常量）。
            callback: 回调函数，可为同步函数或协程函数。回调签名
                通常为 ``callback(data: dict) -> None``。
        """
        if not callable(callback):
            raise TypeError(f"事件回调必须是可调用对象，得到 {type(callback)!r}")
        self._event_handlers.setdefault(event, []).append(callback)

    def unregister_event(self, event: str, callback: PluginCallback) -> bool:
        """取消注册某个事件回调。

        Returns:
            ``True`` 成功移除;``False`` 未找到。
        """
        handlers = self._event_handlers.get(event)
        if not handlers:
            return False
        try:
            handlers.remove(callback)
            return True
        except ValueError:
            return False

    def clear_events(self, event: Optional[str] = None) -> None:
        """清除事件回调。

        Args:
            event: 指定事件名则只清除该事件;``None`` 清除全部。
        """
        if event is None:
            self._event_handlers.clear()
        else:
            self._event_handlers.pop(event, None)

    def list_events(self) -> Dict[str, int]:
        """返回各事件已注册的回调数量。"""
        return {evt: len(cbs) for evt, cbs in self._event_handlers.items() if cbs}

    async def dispatch_event(self, event: str, data: Optional[Dict[str, Any]] = None) -> None:
        """分发事件。

        先调用 :meth:`on_event`（子类可重写以转发到子进程），再触发通过
        :meth:`register_event` 注册的本地回调。

        Args:
            event: 事件名称。
            data: 事件数据字典。
        """
        data = data or {}
        # 1. 子类钩子（默认实现触发本地回调）
        try:
            await self.on_event(event, data)
        except Exception as exc:
            logger.error(
                f"插件 {self.plugin_id} on_event({event}) 异常: {exc}\n"
                f"{traceback.format_exc()}"
            )

    async def on_event(self, event: str, data: Dict[str, Any]) -> None:
        """事件钩子（子类可重写）。

        默认实现：触发通过 :meth:`register_event` 注册的本地回调。
        Go / Java 进程包装器会重写本方法，将事件序列化为 JSON 行
        发送给子进程，而本地回调为空。

        Args:
            event: 事件名称。
            data: 事件数据字典。
        """
        await self._invoke_local_callbacks(event, data)

    async def _invoke_local_callbacks(self, event: str, data: Dict[str, Any]) -> None:
        """调用本地注册的回调（同步 / 协程均可）。"""
        handlers = self._event_handlers.get(event, [])
        for handler in list(handlers):
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(data)
                else:
                    result = handler(data)
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                logger.error(
                    f"插件 {self.plugin_id} 事件回调 {event} 异常: {exc}\n"
                    f"{traceback.format_exc()}"
                )

    # ------------------------------------------------------------------ #
    # 生命周期（抽象方法）
    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    async def on_load(self) -> bool:
        """插件加载时调用。

        子类应在此完成初始化（注册事件、读取配置等）。

        Returns:
            ``True`` 加载成功;``False`` 加载失败（将标记为 ERROR 状态）。
        """
        ...

    @abc.abstractmethod
    async def on_unload(self) -> bool:
        """插件卸载时调用。

        子类应在此完成资源释放（关闭连接、保存数据等）。

        Returns:
            ``True`` 卸载成功;``False`` 卸载失败。
        """
        ...

    # ------------------------------------------------------------------ #
    # 便捷的默认生命周期实现（子类可按需覆盖）
    # ------------------------------------------------------------------ #

    async def _default_on_load(self) -> bool:
        """默认加载实现：派发 PLUGIN_LOAD 事件并标记已加载。

        Python 插件若不想实现 on_load 可在子类中 ``on_load = _default_on_load``，
        或直接调用 ``return await super().on_load()`` 之外的便捷方式。
        """
        self._loaded = True
        await self.dispatch_event(PluginEvent.PLUGIN_LOAD, {})
        return True

    async def _default_on_unload(self) -> bool:
        """默认卸载实现：派发 PLUGIN_UNLOAD 事件并清理回调。"""
        await self.dispatch_event(PluginEvent.PLUGIN_UNLOAD, {})
        self.clear_events()
        self._loaded = False
        return True

    # ------------------------------------------------------------------ #
    # 同步事件便捷注册（装饰器风格，兼容 ToolDelta）
    # ------------------------------------------------------------------ #

    def on(self, event: str) -> Callable[[PluginCallback], PluginCallback]:
        """装饰器：注册事件回调。

        用法::

            plugin = MyPlugin(info, context)

            @plugin.on(PluginEvent.CHAT)
            def handle_chat(data: dict) -> None:
                ...

        Args:
            event: 事件名称。

        Returns:
            装饰器函数。
        """
        def decorator(callback: PluginCallback) -> PluginCallback:
            self.register_event(event, callback)
            return callback

        return decorator

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"{self.__class__.__name__}("
            f"plugin_id={self.plugin_id!r}, "
            f"language={self.info.language.value!r}, "
            f"loaded={self._loaded})"
        )


# ======================================================================
# 进程类插件基类（Go / Java 共用）
# ======================================================================


class SubprocessPluginBase(PluginBase):
    """进程类插件基类，供 Go / Java 加载器共用。

    通过子进程的 stdin/stdout 交换 **JSON 行协议**（每行一条 JSON）。

    Python -> 子进程 (stdin):
        - ``{"type":"event","name":"player_join","data":{...}}``
          分发事件给插件
        - ``{"type":"response","id":"<req_id>","success":true,"data":{"result":...}}``
          对插件请求的响应

    子进程 -> Python (stdout):
        - ``{"type":"request","id":"<req_id>","name":"send_command","data":{"command":"..."}}``
          插件请求宿主执行操作
        - ``{"type":"log","level":"info","message":"..."}``
          插件日志
        - ``{"type":"ready"}``
          插件初始化完成
        - ``{"type":"error","message":"..."}``
          插件报告错误

    本类实现 :meth:`on_load` / :meth:`on_unload` / :meth:`on_event`，
    将调用转换为 IPC 消息。子类（由加载器构造）只需提供启动命令。

    Args:
        info: 插件元信息。
        context: 插件上下文。
        command: 启动子进程的命令列表（如 ``["./plugin"]``）。
        cwd: 子进程工作目录。
        env: 子进程环境变量（``None`` 表示继承父进程）。
        ready_timeout: 等待插件 ``ready`` 的超时秒数（``None`` 表示不等待）。
        stop_timeout: 卸载时等待子进程退出的超时秒数。
    """

    def __init__(
        self,
        info: PluginInfo,
        context: Optional[PluginContext] = None,
        command: Optional[List[str]] = None,
        cwd: Optional[Union[str, Path]] = None,
        env: Optional[Dict[str, str]] = None,
        ready_timeout: Optional[float] = 15.0,
        stop_timeout: float = 5.0,
    ) -> None:
        super().__init__(info, context)
        self._command: List[str] = list(command or [])
        self._cwd: Optional[str] = str(cwd) if cwd else None
        self._env: Optional[Dict[str, str]] = env
        self._ready_timeout: Optional[float] = ready_timeout
        self._stop_timeout: float = stop_timeout

        self._process: Optional["asyncio.subprocess.Process"] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._write_lock: Optional[asyncio.Lock] = None
        self._ready_event: asyncio.Event = asyncio.Event()
        self._start_error: str = ""

        self._request_handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = self._build_request_handlers()

    def _get_write_lock(self) -> asyncio.Lock:
        """获取写锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    # ------------------------------------------------------------------ #
    # 请求处理器：将插件对宿主能力的请求映射到 PluginContext
    # ------------------------------------------------------------------ #

    def _build_request_handlers(
        self,
    ) -> Dict[str, Callable[[Dict[str, Any]], Any]]:
        """构造请求名 -> 处理函数的映射。

        每个处理器接收完整的 ``data`` 字典，返回（或协程返回）结果。
        结果会被包装进响应 ``{"result": <result>}``。
        """
        ctx = self.context

        def _position(d: Dict[str, Any]) -> List[float]:
            return list(ctx.get_position())

        return {
            "send_command": lambda d: ctx.send_command(d.get("command", "")),
            "send_chat": lambda d: ctx.send_chat(d.get("message", "")),
            "say_to": lambda d: ctx.say_to(d.get("target", ""), d.get("message", "")),
            "move_to": lambda d: ctx.move_to(
                float(d.get("x", 0.0)), float(d.get("y", 0.0)), float(d.get("z", 0.0))
            ),
            "get_bot": lambda d: ctx.get_bot(),
            "get_bot_name": lambda d: ctx.get_bot_name(),
            "get_bot_id": lambda d: ctx.get_bot_id(),
            "get_players": lambda d: ctx.get_players(),
            "get_player": lambda d: ctx.get_player(d.get("name", "")),
            "get_chat_history": lambda d: ctx.get_chat_history(int(d.get("limit", 50))),
            "get_position": _position,
            "get_health": lambda d: ctx.get_health(),
            "log": lambda d: ctx.log(d.get("level", "info"), d.get("message", "")),
        }

    # ------------------------------------------------------------------ #
    # 进程生命周期
    # ------------------------------------------------------------------ #

    async def start_process(self) -> bool:
        """启动子进程并开始读取输出。

        Returns:
            ``True`` 启动成功;``False`` 启动失败（``_start_error`` 记录原因）。
        """
        if not self._command:
            self._start_error = "未配置子进程启动命令"
            return False
        try:
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=self._env,
            )
        except FileNotFoundError as exc:
            self._start_error = f"可执行文件不存在: {exc}"
            logger.error(f"插件 {self.plugin_id} 启动失败: {self._start_error}")
            return False
        except OSError as exc:
            self._start_error = f"启动子进程失败: {exc}"
            logger.error(f"插件 {self.plugin_id} 启动失败: {self._start_error}")
            return False

        # 启动读取任务
        self._reader_task = asyncio.create_task(self._reader_loop())
        self._stderr_task = asyncio.create_task(self._stderr_loop())
        return True

    async def stop_process(self) -> None:
        """停止子进程。

        先尝试优雅关闭（关闭 stdin 并等待退出），超时后强制终止。
        """
        proc = self._process
        if proc is None:
            return

        # 取消读取任务
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()

        # 优雅关闭：关闭 stdin
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:  # pragma: no cover - 最佳努力
            pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=self._stop_timeout)
        except asyncio.TimeoutError:
            logger.warning(
                f"插件 {self.plugin_id} 子进程未在 {self._stop_timeout}s 内退出，强制终止"
            )
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:  # pragma: no cover
                pass

        self._process = None
        self._reader_task = None
        self._stderr_task = None

    # ------------------------------------------------------------------ #
    # IPC 读写
    # ------------------------------------------------------------------ #

    async def _send_message(self, message: Dict[str, Any]) -> bool:
        """向子进程 stdin 写入一行 JSON。"""
        proc = self._process
        if proc is None or proc.stdin is None:
            return False
        try:
            line = json.dumps(message, ensure_ascii=False) + "\n"
            async with self._get_write_lock():
                proc.stdin.write(line.encode("utf-8"))
                await proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            logger.warning(f"插件 {self.plugin_id} 写入子进程失败: {exc}")
            return False

    async def _send_response(
        self, req_id: Optional[str], success: bool, result: Any = None, error: str = ""
    ) -> None:
        """发送对插件请求的响应。"""
        msg: Dict[str, Any] = {"type": "response", "id": req_id, "success": success}
        if success:
            msg["data"] = {"result": _json_safe(result)}
        else:
            msg["error"] = error
        await self._send_message(msg)

    async def _reader_loop(self) -> None:
        """读取子进程 stdout，按行解析 JSON 并分发。"""
        proc = self._process
        if proc is None or proc.stdout is None:
            return
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break  # EOF
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        f"插件 {self.plugin_id} 输出非 JSON 行: {line_str!r} ({exc})"
                    )
                    continue
                await self._handle_message(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            logger.error(f"插件 {self.plugin_id} 读取循环异常: {exc}")
        finally:
            self._ready_event.set()  # 避免永久阻塞

    async def _stderr_loop(self) -> None:
        """读取子进程 stderr，转发到日志。"""
        proc = self._process
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.info(f"[{self.plugin_id} stderr] {text}")
                    if self.context is not None:
                        self.context.log("info", f"[stderr] {text}")
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover
            pass

    async def _handle_message(self, msg: Dict[str, Any]) -> None:
        """处理来自子进程的一条消息。"""
        msg_type = msg.get("type")
        if msg_type == "ready":
            self._ready_event.set()
            logger.debug(f"插件 {self.plugin_id} 子进程就绪")
        elif msg_type == "log":
            level = str(msg.get("level", "info"))
            message = str(msg.get("message", ""))
            if self.context is not None:
                self.context.log(level, message)
        elif msg_type == "error":
            message = str(msg.get("message", ""))
            logger.error(f"插件 {self.plugin_id} 报告错误: {message}")
            if self.context is not None:
                self.context.log("error", message)
            self.info.error = message
        elif msg_type == "request":
            await self._handle_request(msg)
        else:
            logger.debug(f"插件 {self.plugin_id} 未知消息类型: {msg_type}")

    async def _handle_request(self, msg: Dict[str, Any]) -> None:
        """处理子进程发起的请求。"""
        req_id = msg.get("id")
        name = msg.get("name")
        data = msg.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        handler = self._request_handlers.get(str(name))
        if handler is None:
            await self._send_response(req_id, False, error=f"未知请求: {name}")
            return
        try:
            result = handler(data)
            if asyncio.iscoroutine(result):
                result = await result
            await self._send_response(req_id, True, result=result)
        except Exception as exc:
            logger.error(f"插件 {self.plugin_id} 处理请求 {name} 异常: {exc}")
            await self._send_response(req_id, False, error=str(exc))

    # ------------------------------------------------------------------ #
    # PluginBase 实现
    # ------------------------------------------------------------------ #

    async def on_load(self) -> bool:
        """启动子进程，等待就绪，并分发 PLUGIN_LOAD 事件。"""
        if not await self.start_process():
            return False

        # 等待 ready（可选）
        if self._ready_timeout is not None:
            try:
                await asyncio.wait_for(self._ready_event.wait(), timeout=self._ready_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    f"插件 {self.plugin_id} 未在 {self._ready_timeout}s 内就绪，继续加载"
                )

        self._loaded = True
        await self._send_message(
            {"type": "event", "name": PluginEvent.PLUGIN_LOAD, "data": {}}
        )
        return True

    async def on_unload(self) -> bool:
        """分发 PLUGIN_UNLOAD 事件并停止子进程。"""
        if self._process is not None:
            await self._send_message(
                {"type": "event", "name": PluginEvent.PLUGIN_UNLOAD, "data": {}}
            )
            # 给子进程一点时间处理卸载事件
            await asyncio.sleep(0.1)
        await self.stop_process()
        self._loaded = False
        return True

    async def on_event(self, event: str, data: Dict[str, Any]) -> None:
        """重写：将事件转发到子进程，而非触发本地回调。"""
        await self._send_message({"type": "event", "name": event, "data": data})


def _json_safe(value: Any) -> Any:
    """将值转换为 JSON 安全类型（tuple -> list，其余原样）。"""
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = [
    "PluginEvent",
    "PluginBase",
    "SubprocessPluginBase",
    "PluginContext",
    "PluginDataFolder",
    "PluginCallback",
]
