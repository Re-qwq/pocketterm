"""PocketTerm 插件管理器

:mod:`app.plugins.manager` 提供统一的插件生命周期管理，是插件系统的入口。

核心职责
--------
    - **发现**: 扫描 ``python`` / ``go`` / ``java`` 三个语言目录，构建插件清单
    - **加载/卸载/重载**: 调度对应语言加载器，管理插件实例
    - **机器人绑定**: 为每个机器人加载一套独立的插件实例（上下文绑定到该机器人）
    - **事件分发**: 将游戏事件转发给已加载的插件
    - **启用状态持久化**: 记录哪些插件被启用，跨重启保留

设计要点
--------
1. **单例** —— 全局唯一 :class:`PluginManager`，通过模块级 ``plugin_manager``
   暴露。

2. **双层注册** ——
   * 全局加载（:meth:`load_plugin`）：插件不绑定具体机器人，上下文无 bot。
   * 机器人加载（:meth:`load_plugins_for_bot`）：为每个机器人创建独立的
     插件实例，上下文绑定到该机器人。Go/Java 插件会各自启动独立子进程。

3. **优雅降级** —— Go/Java 工具链缺失时，插件仍可被发现与列出，仅在
   加载时返回 ``False`` 并打印彩色错误，不影响其他插件。

4. **彩色控制台** —— 所有操作均通过 ANSI 颜色码输出带时间戳与图标的日志。

典型用法::

    from app.plugins.manager import plugin_manager

    infos = plugin_manager.discover_plugins()
    await plugin_manager.load_plugin("python:hello")
    await plugin_manager.load_plugins_for_bot(bot)
    await plugin_manager.dispatch_event_for_bot(bot, "chat", {...})
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from ..access_point.base import Colors
from ..config import PLUGINS_DIR
from .base import PluginBase, PluginContext, PluginDataFolder, PluginEvent
from .file_manager import PluginFileManager
from .go_loader import GoPluginLoader, PluginLoadError as GoLoadError
from .java_loader import JavaPluginLoader, PluginLoadError as JavaLoadError
from .models import PluginInfo, PluginLanguage, PluginStatus
from .python_loader import PluginLoadError as PyLoadError, PythonPluginLoader

if TYPE_CHECKING:  # 仅类型注解
    from app.bot.bot import PocketBot

logger = logging.getLogger("pocketterm.plugins.manager")


# ======================================================================
# 插件管理器（单例）
# ======================================================================


class PluginManager:
    """插件管理器（单例）。

    Args:
        plugins_dir: 插件根目录。为 ``None`` 时使用配置中的 ``PLUGINS_DIR``。
    """

    _instance: Optional["PluginManager"] = None
    _initialized: bool = False

    def __new__(cls, *args: Any, **kwargs: Any) -> "PluginManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, plugins_dir: Optional[Union[str, Path]] = None) -> None:
        if PluginManager._initialized:
            return
        PluginManager._initialized = True

        self.plugins_dir: Path = Path(plugins_dir or PLUGINS_DIR).resolve()

        # 语言子目录
        self._python_dir: Path = self.plugins_dir / PluginLanguage.PYTHON.value
        self._go_dir: Path = self.plugins_dir / PluginLanguage.GO.value
        self._java_dir: Path = self.plugins_dir / PluginLanguage.JAVA.value

        # 加载器
        self._python_loader: PythonPluginLoader = PythonPluginLoader(self._python_dir)
        self._go_loader: GoPluginLoader = GoPluginLoader(self._go_dir)
        self._java_loader: JavaPluginLoader = JavaPluginLoader(self._java_dir)

        # 文件管理器
        self.file_manager: PluginFileManager = PluginFileManager(self.plugins_dir)

        # 已发现的插件注册表: plugin_id -> PluginInfo
        self._registry: Dict[str, PluginInfo] = {}
        # 全局加载的插件: plugin_id -> PluginBase（无机器人绑定）
        self._plugins: Dict[str, PluginBase] = {}
        # 机器人绑定的插件: bot_id -> {plugin_id -> PluginBase}
        self._bot_plugins: Dict[str, Dict[str, PluginBase]] = {}
        # 启用的插件集合
        self._enabled: set = set()
        # 启用状态持久化文件
        self._state_file: Path = self.plugins_dir / "plugins_state.json"

        # 确保目录存在
        for d in (self._python_dir, self._go_dir, self._java_dir):
            d.mkdir(parents=True, exist_ok=True)

        # 加载启用状态
        self._load_state()

    # ------------------------------------------------------------------ #
    # 单例重置（测试用）
    # ------------------------------------------------------------------ #

    @classmethod
    def _reset_singleton(cls) -> None:
        """重置单例（仅用于测试）。"""
        cls._instance = None
        cls._initialized = False

    # ------------------------------------------------------------------ #
    # 启用状态持久化
    # ------------------------------------------------------------------ #

    def _load_state(self) -> None:
        """从磁盘加载启用状态。"""
        if not self._state_file.exists():
            return
        try:
            with open(self._state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                enabled = data.get("enabled", [])
                if isinstance(enabled, list):
                    self._enabled = {str(x) for x in enabled}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"读取插件启用状态失败: {exc}")

    def _save_state(self) -> None:
        """持久化启用状态到磁盘。"""
        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
            with open(self._state_file, "w", encoding="utf-8") as handle:
                json.dump(
                    {"enabled": sorted(self._enabled)},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError as exc:
            logger.warning(f"保存插件启用状态失败: {exc}")

    # ------------------------------------------------------------------ #
    # 加载器选择
    # ------------------------------------------------------------------ #

    def _loader_for(self, language: PluginLanguage):
        """返回对应语言的加载器。"""
        if language == PluginLanguage.PYTHON:
            return self._python_loader
        if language == PluginLanguage.GO:
            return self._go_loader
        if language == PluginLanguage.JAVA:
            return self._java_loader
        raise ValueError(f"未知插件语言: {language}")

    def _load_error_type(self, language: PluginLanguage):
        """返回对应语言加载器的异常类型。"""
        if language == PluginLanguage.PYTHON:
            return PyLoadError
        if language == PluginLanguage.GO:
            return GoLoadError
        if language == PluginLanguage.JAVA:
            return JavaLoadError
        return Exception

    # ------------------------------------------------------------------ #
    # 发现
    # ------------------------------------------------------------------ #

    def discover_plugins(self) -> List[PluginInfo]:
        """扫描三个语言目录，发现所有插件。

        对每个插件目录调用对应加载器的 ``build_info`` 构建元信息。
        发现失败（如目录不含合法入口）的插件会被跳过并记录警告。
        新发现的插件默认启用（除非持久化状态中显式禁用）。

        Returns:
            已发现插件的信息列表。
        """
        self._registry.clear()
        found: List[PluginInfo] = []

        scan_map = [
            (PluginLanguage.PYTHON, self._python_dir, self._python_loader),
            (PluginLanguage.GO, self._go_dir, self._go_loader),
            (PluginLanguage.JAVA, self._java_dir, self._java_loader),
        ]

        for language, lang_dir, loader in scan_map:
            if not lang_dir.is_dir():
                continue
            for entry in sorted(lang_dir.iterdir()):
                if not entry.is_dir():
                    continue
                if entry.name.startswith("."):
                    continue
                try:
                    if not loader.can_load(entry):
                        continue
                    info = loader.build_info(entry)
                except Exception as exc:
                    logger.warning(f"发现 {language.value} 插件 {entry.name} 失败: {exc}")
                    continue
                self._registry[info.plugin_id] = info
                # 默认启用（若持久化状态中未提及则视为启用）
                if info.plugin_id not in self._enabled and not self._state_file.exists():
                    self._enabled.add(info.plugin_id)
                found.append(info)

        self._print(
            f"发现 {Colors.colorize(str(len(found)), Colors.BOLD)} 个插件 "
            "(Python/Go/Java)",
            Colors.CYAN,
        )
        return found

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def list_plugins(self) -> List[PluginInfo]:
        """返回所有已发现插件的信息列表。"""
        return list(self._registry.values())

    def get_plugin_info(self, plugin_id: str) -> Optional[PluginInfo]:
        """获取指定插件的元信息。"""
        return self._registry.get(plugin_id)

    def get_plugin(self, plugin_id: str) -> Optional[PluginBase]:
        """获取全局加载的插件实例。

        仅返回通过 :meth:`load_plugin` 加载的插件（无机器人绑定）。
        如需获取某机器人的插件实例，使用 :meth:`get_bot_plugin`。
        """
        return self._plugins.get(plugin_id)

    def get_bot_plugin(self, bot_id: str, plugin_id: str) -> Optional[PluginBase]:
        """获取某机器人绑定的插件实例。"""
        return self._bot_plugins.get(bot_id, {}).get(plugin_id)

    def is_enabled(self, plugin_id: str) -> bool:
        """插件是否启用。"""
        return plugin_id in self._enabled

    def list_enabled_plugins(self) -> List[PluginInfo]:
        """返回所有已启用的插件信息。"""
        return [info for pid, info in self._registry.items() if pid in self._enabled]

    # ------------------------------------------------------------------ #
    # 启用 / 禁用
    # ------------------------------------------------------------------ #

    def enable_plugin(self, plugin_id: str) -> bool:
        """启用插件。"""
        if plugin_id not in self._registry:
            return False
        self._enabled.add(plugin_id)
        self._save_state()
        self._print(f"启用插件 {Colors.colorize(plugin_id, Colors.GREEN)}", Colors.GREEN)
        return True

    def disable_plugin(self, plugin_id: str) -> bool:
        """禁用插件。"""
        if plugin_id not in self._registry:
            return False
        self._enabled.discard(plugin_id)
        self._save_state()
        self._print(f"禁用插件 {Colors.colorize(plugin_id, Colors.YELLOW)}", Colors.YELLOW)
        return True

    # ------------------------------------------------------------------ #
    # 状态刷新
    # ------------------------------------------------------------------ #

    def _is_loaded_anywhere(self, plugin_id: str) -> bool:
        """插件是否在全局或任一机器人下已加载。"""
        if plugin_id in self._plugins:
            return True
        for bot_map in self._bot_plugins.values():
            if plugin_id in bot_map:
                return True
        return False

    def _refresh_status(self, plugin_id: str) -> None:
        """根据加载情况刷新注册表中插件的状态。"""
        info = self._registry.get(plugin_id)
        if info is None:
            return
        if self._is_loaded_anywhere(plugin_id):
            if info.status != PluginStatus.ERROR:
                info.status = PluginStatus.LOADED
        else:
            if info.status == PluginStatus.LOADED:
                info.status = PluginStatus.UNLOADED
                info.loaded_at = None

    # ------------------------------------------------------------------ #
    # 加载 / 卸载 / 重载（全局，无机器人绑定）
    # ------------------------------------------------------------------ #

    async def load_plugin(self, plugin_id: str) -> bool:
        """加载一个插件（全局，不绑定机器人）。

        Args:
            plugin_id: 插件 ID（如 ``"python:hello"``）。

        Returns:
            ``True`` 加载成功;``False`` 插件不存在或加载失败。
        """
        info = self._registry.get(plugin_id)
        if info is None:
            # 尝试自动发现
            self.discover_plugins()
            info = self._registry.get(plugin_id)
        if info is None:
            self._print(f"加载失败：插件不存在 {plugin_id!r}", Colors.RED, icon="[!]")
            return False

        if plugin_id in self._plugins:
            self._print(
                f"插件 {Colors.colorize(plugin_id, Colors.YELLOW)} 已加载，跳过",
                Colors.YELLOW,
                icon="[~]",
            )
            return True

        loader = self._loader_for(info.language)
        err_type = self._load_error_type(info.language)

        self._print(
            f"加载插件 {Colors.colorize(info.name, Colors.CYAN)} "
            f"({info.language.value}) ...",
            Colors.CYAN,
            icon="[+]",
        )
        try:
            plugin = await loader.load(info.folder)
        except err_type as exc:
            info.status = PluginStatus.ERROR
            info.error = str(exc)
            self._print(
                f"加载失败 {Colors.colorize(plugin_id, Colors.RED)}: {exc}",
                Colors.RED,
                icon="[!]",
            )
            logger.error(f"加载插件 {plugin_id} 失败: {exc}")
            return False
        except Exception as exc:
            info.status = PluginStatus.ERROR
            info.error = str(exc)
            self._print(
                f"加载失败 {Colors.colorize(plugin_id, Colors.RED)}: {exc}",
                Colors.RED,
                icon="[!]",
            )
            logger.exception(f"加载插件 {plugin_id} 异常")
            return False

        self._plugins[plugin_id] = plugin
        info.status = PluginStatus.LOADED
        info.loaded_at = time.time()
        info.error = ""
        self._print(
            f"已加载 {Colors.colorize(info.name, Colors.GREEN)} "
            f"({info.language.value})",
            Colors.GREEN,
            icon="[+]",
        )
        return True

    async def unload_plugin(self, plugin_id: str) -> bool:
        """卸载一个全局加载的插件。

        Returns:
            ``True`` 卸载成功;``False`` 插件未加载或卸载失败。
        """
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            self._print(
                f"插件 {Colors.colorize(plugin_id, Colors.YELLOW)} 未加载",
                Colors.YELLOW,
                icon="[~]",
            )
            return False

        info = self._registry.get(plugin_id, plugin.info)
        loader = self._loader_for(info.language)

        self._print(
            f"卸载插件 {Colors.colorize(info.name, Colors.MAGENTA)} ...",
            Colors.MAGENTA,
            icon="[-]",
        )
        try:
            await loader.unload(plugin)
        except Exception as exc:
            self._print(
                f"卸载异常 {Colors.colorize(plugin_id, Colors.RED)}: {exc}",
                Colors.RED,
                icon="[!]",
            )
            logger.exception(f"卸载插件 {plugin_id} 异常")

        self._plugins.pop(plugin_id, None)
        self._refresh_status(plugin_id)
        self._print(
            f"已卸载 {Colors.colorize(info.name, Colors.YELLOW)}",
            Colors.YELLOW,
            icon="[-]",
        )
        return True

    async def reload_plugin(self, plugin_id: str) -> bool:
        """重载插件（先卸载再加载）。

        Returns:
            ``True`` 重载成功。
        """
        info = self._registry.get(plugin_id)
        if info is None:
            self.discover_plugins()
            info = self._registry.get(plugin_id)
        if info is None:
            self._print(f"重载失败：插件不存在 {plugin_id!r}", Colors.RED, icon="[!]")
            return False

        self._print(
            f"重载插件 {Colors.colorize(info.name, Colors.BRIGHT_CYAN)} ...",
            Colors.BRIGHT_CYAN,
            icon="[~]",
        )
        # 全局实例
        if plugin_id in self._plugins:
            await self.unload_plugin(plugin_id)
        # 机器人绑定实例
        for bot_id, bot_map in list(self._bot_plugins.items()):
            if plugin_id in bot_map:
                plugin = bot_map.pop(plugin_id)
                loader = self._loader_for(info.language)
                try:
                    await loader.unload(plugin)
                except Exception as exc:
                    logger.warning(f"重载时卸载 {plugin_id} (bot={bot_id}) 失败: {exc}")

        ok = await self.load_plugin(plugin_id)
        # 重新为之前绑定的机器人加载
        for bot_id in list(self._bot_plugins.keys()):
            if plugin_id in self._enabled and not self._bot_plugins[bot_id].get(plugin_id):
                bot = self._get_bot_by_id(bot_id)
                if bot is not None:
                    await self._load_plugin_for_bot(bot, plugin_id)
        if ok:
            self._print(
                f"已重载 {Colors.colorize(info.name, Colors.GREEN)}",
                Colors.GREEN,
                icon="[+]",
            )
        return ok

    # ------------------------------------------------------------------ #
    # 机器人绑定加载
    # ------------------------------------------------------------------ #

    def _get_bot_by_id(self, bot_id: str) -> Optional["PocketBot"]:
        """通过 bot_id 查找机器人实例（借助 BotManager）。"""
        try:
            from app.bot.manager import bot_manager

            return bot_manager.get_bot(bot_id)
        except Exception:
            return None

    async def _load_plugin_for_bot(
        self, bot: "PocketBot", plugin_id: str
    ) -> Optional[PluginBase]:
        """为单个机器人加载单个插件实例。"""
        info = self._registry.get(plugin_id)
        if info is None:
            return None

        bot_id = bot.bot_id
        bot_map = self._bot_plugins.setdefault(bot_id, {})
        if plugin_id in bot_map:
            return bot_map[plugin_id]

        loader = self._loader_for(info.language)
        err_type = self._load_error_type(info.language)

        # 绑定到该机器人的上下文
        data_folder = PluginDataFolder(info.data_folder, info.log_file)
        context = PluginContext(bot=bot, data_folder=data_folder, plugin_info=info)

        try:
            plugin = await loader.load(info.folder, context=context)
        except err_type as exc:
            info.status = PluginStatus.ERROR
            info.error = str(exc)
            self._print(
                f"[{bot.name}] 加载插件 {Colors.colorize(plugin_id, Colors.RED)} 失败: {exc}",
                Colors.RED,
                icon="[!]",
            )
            logger.error(f"为机器人 {bot.name} 加载插件 {plugin_id} 失败: {exc}")
            return None
        except Exception as exc:
            info.status = PluginStatus.ERROR
            info.error = str(exc)
            self._print(
                f"[{bot.name}] 加载插件 {Colors.colorize(plugin_id, Colors.RED)} 异常: {exc}",
                Colors.RED,
                icon="[!]",
            )
            logger.exception(f"为机器人 {bot.name} 加载插件 {plugin_id} 异常")
            return None

        bot_map[plugin_id] = plugin
        info.status = PluginStatus.LOADED
        info.loaded_at = time.time()
        info.error = ""
        return plugin

    async def load_plugins_for_bot(self, bot: "PocketBot") -> int:
        """为机器人加载所有启用的插件。

        为每个启用的插件创建绑定到该机器人的独立实例（含独立上下文）。
        Go/Java 插件会各自启动独立子进程。

        Args:
            bot: 机器人实例。

        Returns:
            成功加载的插件数量。
        """
        bot_id = bot.bot_id
        if not self._registry:
            self.discover_plugins()

        enabled = self.list_enabled_plugins()
        self._print(
            f"[{Colors.colorize(bot.name, Colors.GREEN)}] 加载 "
            f"{Colors.colorize(str(len(enabled)), Colors.BOLD)} 个插件 ...",
            Colors.CYAN,
            icon="[+]",
        )

        # 注册机器人事件转发（聊天 -> 插件 CHAT 事件）
        self._wire_bot_events(bot)

        count = 0
        for info in enabled:
            plugin = await self._load_plugin_for_bot(bot, info.plugin_id)
            if plugin is not None:
                count += 1
                self._print(
                    f"  [{Colors.colorize(bot.name, Colors.GREEN)}] "
                    f"{Colors.colorize('+', Colors.GREEN)} {info.plugin_id}",
                    Colors.GREEN,
                )

        self._print(
            f"[{Colors.colorize(bot.name, Colors.GREEN)}] 插件加载完成 "
            f"(成功 {Colors.colorize(str(count), Colors.GREEN)}/"
            f"{len(enabled)})",
            Colors.GREEN if count == len(enabled) else Colors.YELLOW,
            icon="[+]",
        )
        return count

    async def unload_plugins_for_bot(self, bot: "PocketBot") -> int:
        """卸载机器人绑定的所有插件。

        Args:
            bot: 机器人实例。

        Returns:
            成功卸载的插件数量。
        """
        bot_id = bot.bot_id
        bot_map = self._bot_plugins.pop(bot_id, {})
        if not bot_map:
            self._print(
                f"[{Colors.colorize(bot.name, Colors.YELLOW)}] 无已加载插件",
                Colors.YELLOW,
                icon="[-]",
            )
            return 0

        self._print(
            f"[{Colors.colorize(bot.name, Colors.MAGENTA)}] 卸载 "
            f"{len(bot_map)} 个插件 ...",
            Colors.MAGENTA,
            icon="[-]",
        )

        count = 0
        for plugin_id, plugin in list(bot_map.items()):
            info = self._registry.get(plugin_id, plugin.info)
            loader = self._loader_for(info.language)
            try:
                await loader.unload(plugin)
                count += 1
            except Exception as exc:
                logger.warning(f"卸载 {plugin_id} (bot={bot.name}) 异常: {exc}")
            self._refresh_status(plugin_id)

        self._print(
            f"[{Colors.colorize(bot.name, Colors.YELLOW)}] 插件已全部卸载",
            Colors.YELLOW,
            icon="[-]",
        )
        return count

    # ------------------------------------------------------------------ #
    # 事件转发与分发
    # ------------------------------------------------------------------ #

    def _wire_bot_events(self, bot: "PocketBot") -> None:
        """为机器人注册事件转发器（聊天等 -> 插件事件）。

        转发器在插件全部卸载后自动 no-op，无需显式注销。
        """
        manager = self
        bot_id = bot.bot_id

        async def _on_chat(b: "PocketBot", sender: str, message: str) -> None:
            if not manager._bot_plugins.get(b.bot_id):
                return
            await manager.dispatch_event_for_bot(
                b,
                PluginEvent.CHAT,
                {"sender": sender, "message": message, "is_system": False},
            )

        async def _on_connect(b: "PocketBot") -> None:
            # 机器人连接视为一次 tick/通知，分发 PLUGIN_LOAD 已在加载时完成
            await manager.dispatch_event_for_bot(
                b, PluginEvent.TICK, {"timestamp": time.time(), "event": "connect"}
            )

        try:
            bot.on("chat", _on_chat)
            bot.on("connect", _on_connect)
        except Exception as exc:  # pragma: no cover - 防御性
            logger.warning(f"为机器人 {bot_id} 注册事件转发失败: {exc}")

    async def dispatch_event(
        self, event: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """向所有全局加载的插件分发事件。

        Args:
            event: 事件名称（使用 :class:`PluginEvent` 常量）。
            data: 事件数据。
        """
        data = data or {}
        for plugin in list(self._plugins.values()):
            try:
                await plugin.dispatch_event(event, data)
            except Exception as exc:
                logger.exception(f"分发事件 {event} 到 {plugin.plugin_id} 异常: {exc}")

    async def dispatch_event_for_bot(
        self, bot: "PocketBot", event: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """向某机器人绑定的所有插件分发事件。

        Args:
            bot: 机器人实例。
            event: 事件名称。
            data: 事件数据。
        """
        data = data or {}
        bot_map = self._bot_plugins.get(bot.bot_id, {})
        for plugin in list(bot_map.values()):
            try:
                await plugin.dispatch_event(event, data)
            except Exception as exc:
                logger.exception(
                    f"分发事件 {event} 到 {plugin.plugin_id} (bot={bot.name}) 异常: {exc}"
                )

    async def dispatch_event_all(
        self, event: str, data: Optional[Dict[str, Any]] = None
    ) -> None:
        """向所有已加载插件（全局 + 所有机器人）分发事件。"""
        await self.dispatch_event(event, data)
        try:
            from app.bot.manager import bot_manager

            for bot in bot_manager.bots:
                await self.dispatch_event_for_bot(bot, event, data)
        except Exception:
            # bot_manager 不可用时仅分发全局
            for bot_map in list(self._bot_plugins.values()):
                for plugin in list(bot_map.values()):
                    try:
                        await plugin.dispatch_event(event, data or {})
                    except Exception:
                        pass

    # ------------------------------------------------------------------ #
    # 卸载全部
    # ------------------------------------------------------------------ #

    async def unload_all(self) -> None:
        """卸载所有已加载插件（全局 + 所有机器人）。"""
        self._print("卸载所有插件 ...", Colors.YELLOW, icon="[-]")
        # 全局
        for plugin_id in list(self._plugins.keys()):
            await self.unload_plugin(plugin_id)
        # 机器人绑定
        try:
            from app.bot.manager import bot_manager

            for bot in list(bot_manager.bots):
                await self.unload_plugins_for_bot(bot)
        except Exception:
            for bot_id in list(self._bot_plugins.keys()):
                bot_map = self._bot_plugins.pop(bot_id, {})
                for plugin_id, plugin in list(bot_map.items()):
                    info = self._registry.get(plugin_id, plugin.info)
                    loader = self._loader_for(info.language)
                    try:
                        await loader.unload(plugin)
                    except Exception as exc:
                        logger.warning(f"卸载 {plugin_id} 异常: {exc}")
                    self._refresh_status(plugin_id)
        self._print("所有插件已卸载", Colors.GREEN, icon="[-]")

    # ------------------------------------------------------------------ #
    # 彩色控制台输出
    # ------------------------------------------------------------------ #

    def _print(
        self,
        message: str,
        color: str = Colors.CYAN,
        icon: str = "[*]",
    ) -> None:
        """打印管理器级别的彩色日志。

        格式: ``[HH:MM:SS] icon [PluginManager] message``
        """
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{color}{icon}{Colors.RESET} "
            f"{Colors.BOLD}[PluginManager]{Colors.RESET} "
            f"{color}{message}{Colors.RESET}",
            flush=True,
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"PluginManager(plugins_dir={self.plugins_dir!s}, "
            f"discovered={len(self._registry)}, "
            f"loaded={len(self._plugins)})"
        )


# ======================================================================
# 全局单例
# ======================================================================

#: 全局插件管理器实例（单例）
plugin_manager: PluginManager = PluginManager()


__all__ = ["PluginManager", "plugin_manager"]
