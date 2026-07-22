"""PocketTerm Python 插件加载器

:mod:`app.plugins.python_loader` 负责动态加载 Python 语言插件。

支持的入口形式
--------------
1. **包式插件** —— 插件目录含 ``__init__.py``，整个目录作为一个包导入。
2. **单文件插件** —— 插件目录含 ``main.py`` 或单个 ``.py`` 文件。
3. **ToolDelta 兼容** —— 使用 ``@plugin_entry("名称")`` 装饰器标记的类，
   或继承 :class:`Plugin` (``= PluginBase`` 别名) 的类。
4. **工厂函数** —— 模块中定义 ``create_plugin(info, context) -> PluginBase``。

元数据来源（按优先级）
----------------------
1. ``plugin.json`` / ``plugin.yaml`` 清单文件
2. 模块级变量（``__plugin_name__`` / ``__author__`` / ``__version__`` 等）
3. ``@plugin_entry(name)`` 装饰器提供的名称
4. 类属性 ``name`` / ``author`` / ``version``
5. 默认值（目录名作为名称）

ToolDelta 兼容性
-----------------
本模块导出 ``plugin_entry`` 装饰器与 ``Plugin`` 基类别名。若环境中未安装
真实的 ``tooldelta`` 包，加载器会在 ``sys.modules`` 中注册一个轻量兼容垫片，
使 ``from tooldelta import plugin_entry, Plugin`` 风格的既有插件可直接运行。
若已安装真实 ``tooldelta``，则不覆盖。
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type, Union

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

from .base import PluginBase, PluginContext, PluginDataFolder, PluginEvent
from .models import PluginInfo, PluginLanguage, PluginStatus

logger = logging.getLogger("pocketterm.plugins.python_loader")


# ======================================================================
# 异常
# ======================================================================


class PluginLoadError(Exception):
    """Python 插件加载失败异常。"""


# ======================================================================
# ToolDelta 兼容层：plugin_entry 装饰器与 Plugin 别名
# ======================================================================

#: 标记被 ``plugin_entry`` 装饰的类所设置的属性名。
_PLUGIN_ENTRY_ATTR = "__plugin_entry__"
#: 装饰器提供的插件显示名称所设置的属性名。
_PLUGIN_NAME_ATTR = "__plugin_name__"


def plugin_entry(name: Optional[str] = None) -> Callable[[Type[Any]], Type[Any]]:
    """ToolDelta 风格的插件入口装饰器。

    用法::

        from app.plugins.python_loader import plugin_entry, Plugin

        @plugin_entry("我的插件")
        class MyPlugin(Plugin):
            async def on_load(self) -> bool:
                ...
            async def on_unload(self) -> bool:
                ...

    装饰器本身不实例化插件，仅在类上打标记（``__plugin_entry__`` /
    ``__plugin_name__``），由 :class:`PythonPluginLoader` 在扫描模块时识别。

    Args:
        name: 插件显示名称。为 ``None`` 时使用类名。

    Returns:
        类装饰器。
    """

    def decorator(cls: Type[Any]) -> Type[Any]:
        setattr(cls, _PLUGIN_ENTRY_ATTR, True)
        setattr(cls, _PLUGIN_NAME_ATTR, name or cls.__name__)
        return cls

    return decorator


#: ToolDelta 兼容的插件基类别名（等价于 :class:`PluginBase`）。
Plugin = PluginBase


def _install_tooldelta_shim() -> None:
    """若未安装真实 ``tooldelta``，注册一个轻量兼容模块。

    这样既有的 ``from tooldelta import plugin_entry, Plugin`` 风格插件
    无需修改即可在 PocketTerm 中加载。已安装真实 tooldelta 时不覆盖。
    """
    if "tooldelta" in sys.modules:
        return
    # 探测是否存在真实 tooldelta 包
    spec = importlib.util.find_spec("tooldelta")
    if spec is not None:
        return  # 真实包存在，不覆盖

    import types

    shim = types.ModuleType("tooldelta")
    shim.plugin_entry = plugin_entry
    shim.Plugin = Plugin
    shim.PluginBase = PluginBase
    shim.PluginEvent = PluginEvent
    shim.__all__ = ["plugin_entry", "Plugin", "PluginBase", "PluginEvent"]
    sys.modules["tooldelta"] = shim
    logger.debug("已注册 tooldelta 兼容垫片")


# 在模块导入时安装垫片（幂等）。
_install_tooldelta_shim()


# ======================================================================
# Python 插件加载器
# ======================================================================


class PythonPluginLoader:
    """Python 插件加载器。

    负责发现入口文件、读取元数据、动态导入模块、定位插件类并实例化。

    Args:
        plugins_dir: Python 插件根目录（含若干插件子目录）。
            为 ``None`` 时使用默认 ``<PLUGINS_DIR>/python``。
    """

    #: 支持的清单文件名（按优先级）。
    MANIFEST_FILES: List[str] = ["plugin.json", "plugin.yaml", "plugin.yml"]

    #: 包式入口文件名。
    PACKAGE_ENTRY = "__init__.py"
    #: 单文件入口候选。
    SINGLE_FILE_ENTRIES: List[str] = ["main.py", "plugin.py", "index.py"]

    def __init__(self, plugins_dir: Optional[Union[str, Path]] = None) -> None:
        if plugins_dir is None:
            from app.config import PLUGINS_DIR

            plugins_dir = Path(PLUGINS_DIR) / "python"
        self.plugins_dir: Path = Path(plugins_dir)

    # ------------------------------------------------------------------ #
    # 入口与元数据发现
    # ------------------------------------------------------------------ #

    def can_load(self, plugin_dir: Union[str, Path]) -> bool:
        """判断目录是否为合法的 Python 插件目录。"""
        path = Path(plugin_dir)
        if not path.is_dir():
            return False
        return self._find_entry_file(path) is not None

    def _find_entry_file(self, plugin_dir: Path) -> Optional[Path]:
        """在插件目录中定位入口文件。

        优先级:
            1. 清单中指定的 ``main_file``
            2. ``__init__.py``（包式）
            3. ``main.py`` / ``plugin.py`` / ``index.py``
            4. 目录中唯一的 ``.py`` 文件
        """
        # 1. 清单指定
        manifest = self._read_manifest(plugin_dir)
        if manifest and manifest.get("main_file"):
            candidate = plugin_dir / manifest["main_file"]
            if candidate.is_file():
                return candidate

        # 2. 包式
        pkg_entry = plugin_dir / self.PACKAGE_ENTRY
        if pkg_entry.is_file():
            return pkg_entry

        # 3. 单文件入口候选
        for name in self.SINGLE_FILE_ENTRIES:
            candidate = plugin_dir / name
            if candidate.is_file():
                return candidate

        # 4. 唯一 .py 文件
        py_files = sorted(p for p in plugin_dir.glob("*.py") if p.is_file())
        if len(py_files) == 1:
            return py_files[0]

        return None

    def _read_manifest(self, plugin_dir: Path) -> Dict[str, Any]:
        """读取清单文件（plugin.json / plugin.yaml），不存在则返回空字典。"""
        for fname in self.MANIFEST_FILES:
            fpath = plugin_dir / fname
            if not fpath.is_file():
                continue
            try:
                with open(fpath, "r", encoding="utf-8") as handle:
                    raw = handle.read()
                if fname.endswith(".json"):
                    data = json.loads(raw)
                else:
                    if yaml is None:
                        continue
                    data = yaml.safe_load(raw)
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"读取清单 {fpath} 失败: {exc}")
        return {}

    def build_info(self, plugin_dir: Union[str, Path]) -> PluginInfo:
        """根据插件目录构建 :class:`PluginInfo`（不导入模块，仅静态分析）。

        Args:
            plugin_dir: 插件目录路径。

        Returns:
            插件元信息。无法识别入口时抛出 :class:`PluginLoadError`。
        """
        plugin_dir = Path(plugin_dir).resolve()
        if not plugin_dir.is_dir():
            raise PluginLoadError(f"插件目录不存在: {plugin_dir}")

        entry = self._find_entry_file(plugin_dir)
        if entry is None:
            raise PluginLoadError(
                f"在 {plugin_dir} 中未找到 Python 插件入口文件"
            )

        folder_name = plugin_dir.name
        manifest = self._read_manifest(plugin_dir)

        # 数据目录与日志文件
        data_folder = plugin_dir / "datas"
        logs_dir = plugin_dir / "logs"
        log_file = logs_dir / "plugin.log"

        info = PluginInfo(
            plugin_id=f"{PluginLanguage.PYTHON.value}:{folder_name}",
            name=manifest.get("name", folder_name),
            author=manifest.get("author", ""),
            version=manifest.get("version", "0.0.0"),
            description=manifest.get("description", ""),
            language=PluginLanguage.PYTHON,
            status=PluginStatus.UNLOADED,
            folder=str(plugin_dir),
            main_file=entry.name,
            dependencies=list(manifest.get("dependencies", []) or []),
            permissions=list(manifest.get("permissions", []) or []),
            data_folder=str(data_folder),
            log_file=str(log_file),
            created_at=plugin_dir.stat().st_mtime if plugin_dir.exists() else time.time(),
        )
        return info

    # ------------------------------------------------------------------ #
    # 模块导入与插件类定位
    # ------------------------------------------------------------------ #

    def _import_module(self, plugin_dir: Path, entry: Path) -> Any:
        """动态导入插件模块。

        使用唯一模块名以避免 ``sys.modules`` 冲突，并支持重复加载（重载时
        先移除旧模块）。包式入口会正确设置 ``__package__`` 与子模块搜索路径。

        Args:
            plugin_dir: 插件目录。
            entry: 入口文件路径。

        Returns:
            导入的模块对象。

        Raises:
            PluginLoadError: 导入失败。
        """
        is_package = entry.name == self.PACKAGE_ENTRY
        # 唯一模块名，避免冲突并支持重载
        suffix = uuid.uuid4().hex[:8]
        if is_package:
            module_name = f"_pocketterm_plugin_{plugin_dir.name}_{suffix}"
        else:
            stem = entry.stem
            module_name = f"_pocketterm_plugin_{plugin_dir.name}_{stem}_{suffix}"

        # 重载：清理可能存在的同名旧模块（按前缀）
        prefix = f"_pocketterm_plugin_{plugin_dir.name}_"
        for stale in list(sys.modules):
            if stale.startswith(prefix):
                try:
                    del sys.modules[stale]
                except KeyError:
                    pass

        try:
            if is_package:
                spec = importlib.util.spec_from_file_location(
                    module_name,
                    entry,
                    submodule_search_locations=[str(plugin_dir)],
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, entry)
        except (ValueError, ImportError) as exc:
            raise PluginLoadError(f"创建模块 spec 失败 {entry}: {exc}") from exc

        if spec is None or spec.loader is None:
            raise PluginLoadError(f"无法为 {entry} 创建模块 spec")

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module

        # 包式：把目录加入 sys.path 以支持相对/子模块导入
        path_added = False
        if is_package:
            path_str = str(plugin_dir)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
                path_added = True

        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            # 导入失败：清理已注册模块
            sys.modules.pop(module_name, None)
            raise PluginLoadError(
                f"执行插件模块 {entry} 失败: {exc}\n{traceback.format_exc()}"
            ) from exc
        finally:
            if path_added:
                try:
                    sys.path.remove(str(plugin_dir))
                except ValueError:
                    pass

        # 在模块上记录所用模块名，便于卸载时清理
        setattr(module, "__pocketterm_module_name__", module_name)
        return module

    def _find_plugin_class(self, module: Any) -> Optional[Type[PluginBase]]:
        """在模块中定位插件类。

        识别策略（按优先级）:
            1. ``@plugin_entry`` 装饰的类（``__plugin_entry__`` 标记）
            2. 模块中定义的 :class:`PluginBase` 子类
            3. 模块级 ``__plugin_class__`` 变量
        """
        # 1. plugin_entry 装饰的类
        entry_classes: List[Type[Any]] = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if not isinstance(attr, type):
                continue
            # 仅取在该模块中定义的类
            if getattr(attr, "__module__", None) != getattr(module, "__name__", None):
                continue
            if getattr(attr, _PLUGIN_ENTRY_ATTR, False):
                entry_classes.append(attr)

        if entry_classes:
            # 多个时取第一个
            return entry_classes[0]  # type: ignore[return-value]

        # 2. PluginBase 子类
        subclasses: List[Type[PluginBase]] = []
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if not isinstance(attr, type):
                continue
            if getattr(attr, "__module__", None) != getattr(module, "__name__", None):
                continue
            if attr is PluginBase:
                continue
            try:
                if issubclass(attr, PluginBase):
                    subclasses.append(attr)
            except TypeError:
                continue

        if subclasses:
            return subclasses[0]

        # 3. 模块级 __plugin_class__
        explicit = getattr(module, "__plugin_class__", None)
        if isinstance(explicit, type) and issubclass(explicit, PluginBase):
            return explicit

        return None

    # ------------------------------------------------------------------ #
    # 加载 / 卸载
    # ------------------------------------------------------------------ #

    async def load(
        self,
        plugin_dir: Union[str, Path],
        context: Optional[PluginContext] = None,
    ) -> PluginBase:
        """加载 Python 插件。

        流程:
            1. 构建插件信息（静态分析）
            2. 导入入口模块
            3. 定位插件类
            4. 合并模块级元数据到 :class:`PluginInfo`
            5. 实例化插件（注入上下文与数据目录）
            6. 调用 ``on_load()``（协程方法，会被 await）

        Args:
            plugin_dir: 插件目录路径。
            context: 可选的插件上下文。为 ``None`` 时创建默认上下文。

        Returns:
            已加载的 :class:`PluginBase` 实例。

        Raises:
            PluginLoadError: 任何步骤失败。
        """
        plugin_dir = Path(plugin_dir).resolve()
        info = self.build_info(plugin_dir)
        entry = plugin_dir / info.main_file
        if not entry.is_file():
            raise PluginLoadError(f"入口文件不存在: {entry}")

        module = self._import_module(plugin_dir, entry)

        plugin_cls = self._find_plugin_class(module)
        factory = getattr(module, "create_plugin", None)

        plugin: PluginBase
        if plugin_cls is not None:
            # 合并装饰器/类属性元数据
            self._merge_class_metadata(info, plugin_cls, module)
            # 准备上下文与数据目录
            data_folder = PluginDataFolder(info.data_folder, info.log_file)
            ctx = context or PluginContext(data_folder=data_folder, plugin_info=info)
            ctx._data_folder = data_folder  # type: ignore[attr-defined]
            ctx._plugin_info = info  # type: ignore[attr-defined]
            try:
                plugin = plugin_cls(info, ctx)
            except TypeError:
                # 兼容只接受 info 的构造器
                try:
                    plugin = plugin_cls(info)
                    plugin.context = ctx
                except Exception as exc:
                    raise PluginLoadError(
                        f"实例化插件类 {plugin_cls.__name__} 失败: {exc}"
                    ) from exc
        elif callable(factory):
            data_folder = PluginDataFolder(info.data_folder, info.log_file)
            ctx = context or PluginContext(data_folder=data_folder, plugin_info=info)
            try:
                plugin = factory(info, ctx)
            except TypeError:
                try:
                    plugin = factory(info)
                    plugin.context = ctx
                except Exception as exc:
                    raise PluginLoadError(
                        f"调用 create_plugin 失败: {exc}"
                    ) from exc
            if not isinstance(plugin, PluginBase):
                raise PluginLoadError(
                    f"create_plugin 返回的不是 PluginBase 实例: {type(plugin)!r}"
                )
        else:
            raise PluginLoadError(
                f"在插件 {info.plugin_id} 中未找到插件类 "
                "（需继承 PluginBase 或使用 @plugin_entry，或定义 create_plugin）"
            )

        # 记录模块引用，便于卸载
        plugin.__module__ = module  # type: ignore[attr-defined]

        # 调用 on_load（兼容同步/协程实现）
        try:
            ok = await await_or_call(plugin.on_load)
        except PluginLoadError:
            raise
        except Exception as exc:
            raise PluginLoadError(
                f"插件 {info.plugin_id} on_load 失败: {exc}\n{traceback.format_exc()}"
            ) from exc

        if not _coerce_bool(ok):
            raise PluginLoadError(f"插件 {info.plugin_id} on_load 返回 False")

        info.status = PluginStatus.LOADED
        info.loaded_at = time.time()
        info.error = ""
        logger.info(f"已加载 Python 插件: {info.plugin_id} ({info.name})")
        return plugin

    def _merge_class_metadata(
        self, info: PluginInfo, cls: Type[Any], module: Any
    ) -> None:
        """将装饰器名称、类属性、模块级变量合并进插件信息。"""
        # 装饰器名称优先于清单名（若清单未提供 name 则用装饰器名）
        decorator_name = getattr(cls, _PLUGIN_NAME_ATTR, None)
        if decorator_name and not info.name:
            info.name = decorator_name
        elif decorator_name:
            # 清单名优先，但若清单名为目录名则采用装饰器更可读的名称
            if info.name == info.folder.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]:
                info.name = decorator_name

        # 类属性
        for attr, field_name in (
            ("name", "name"),
            ("author", "author"),
            ("version", "version"),
            ("description", "description"),
        ):
            value = getattr(cls, attr, None)
            if isinstance(value, str) and value and not getattr(info, field_name):
                setattr(info, field_name, value)

        # 模块级变量（ToolDelta / 约定）
        module_vars = {
            "__plugin_name__": "name",
            "__plugin_author__": "author",
            "__plugin_version__": "version",
            "__plugin_description__": "description",
            "__author__": "author",
            "__version__": "version",
        }
        for mvar, fname in module_vars.items():
            value = getattr(module, mvar, None)
            if isinstance(value, str) and value and not getattr(info, fname):
                setattr(info, fname, value)

        # 依赖与权限
        deps = getattr(module, "__plugin_dependencies__", None) or getattr(cls, "dependencies", None)
        if isinstance(deps, (list, tuple)) and not info.dependencies:
            info.dependencies = list(deps)
        perms = getattr(module, "__plugin_permissions__", None) or getattr(cls, "permissions", None)
        if isinstance(perms, (list, tuple)) and not info.permissions:
            info.permissions = list(perms)

    async def unload(self, plugin: PluginBase) -> bool:
        """卸载 Python 插件。

        调用 ``on_unload()``（协程方法，会被 await）并清理 ``sys.modules``
        中的插件模块。

        Args:
            plugin: 插件实例。

        Returns:
            ``True`` 卸载成功。
        """
        try:
            ok = await await_or_call(plugin.on_unload)
        except Exception as exc:
            logger.error(f"插件 {plugin.plugin_id} on_unload 异常: {exc}")
            ok = False

        # 清理模块
        module = getattr(plugin, "__module__", None)
        module_name = getattr(module, "__pocketterm_module_name__", None) if module else None
        if module_name:
            sys.modules.pop(module_name, None)
            # 清理该插件的全部子模块/旧模块
            prefix = f"_pocketterm_plugin_{plugin.info.folder_name}_"
            for stale in list(sys.modules):
                if stale.startswith(prefix):
                    sys.modules.pop(stale, None)

        plugin.info.status = PluginStatus.UNLOADED
        plugin.info.loaded_at = None
        return _coerce_bool(ok)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"PythonPluginLoader(plugins_dir={self.plugins_dir!s})"


# ======================================================================
# 同步/协程通用调用辅助
# ======================================================================


async def await_or_call(func: Callable, *args: Any, **kwargs: Any) -> Any:
    """调用 ``func``，若返回协程则 await 它。

    用于兼容同步与异步的 ``on_load`` / ``on_unload`` 实现。
    """
    import asyncio as _asyncio

    result = func(*args, **kwargs)
    if _asyncio.iscoroutine(result):
        return await result
    return result


def _coerce_bool(value: Any) -> bool:
    """将任意返回值转为布尔（``None`` 视为 ``True`` 以兼容无返回值实现）。"""
    if value is None:
        return True
    return bool(value)


__all__ = [
    "PythonPluginLoader",
    "PluginLoadError",
    "plugin_entry",
    "Plugin",
    "await_or_call",
]
