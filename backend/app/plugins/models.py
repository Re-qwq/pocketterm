"""PocketTerm 插件数据模型

定义插件系统使用的核心数据结构:

    - :class:`PluginLanguage`  插件编程语言枚举（PYTHON / GO / JAVA）
    - :class:`PluginStatus`    插件运行状态枚举（LOADED / UNLOADED / ERROR / RUNNING）
    - :class:`PluginInfo`      插件元信息 dataclass

所有 dataclass 使用 ``from __future__ import annotations`` 以支持前向引用，
可变默认值使用 ``field(default_factory=...)``。

``PluginInfo`` 既可由 :mod:`app.plugins.manager` 在扫描目录时构建，
也可由各语言加载器在加载完成后回填 ``loaded_at`` / ``status`` 等字段。
``to_dict()`` 方法将所有字段序列化为 JSON 兼容字典，供 API / WebSocket 使用。
"""
from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ======================================================================
# 枚举
# ======================================================================


class PluginLanguage(enum.Enum):
    """插件编程语言。

    每种语言由对应的加载器负责加载:

        - ``PYTHON``  由 :class:`~app.plugins.python_loader.PythonPluginLoader` 加载
        - ``GO``      由 :class:`~app.plugins.go_loader.GoPluginLoader` 加载
        - ``JAVA``    由 :class:`~app.plugins.java_loader.JavaPluginLoader` 加载
    """

    PYTHON = "python"
    GO = "go"
    JAVA = "java"


class PluginStatus(enum.Enum):
    """插件运行状态。

    状态流转::

        UNLOADED ──load()──► LOADED ──(事件触发)──► RUNNING
            ▲                   │
            │                   └──出错──► ERROR
            └────unload()────────────────────────┘

    各状态含义:
        - ``LOADED``    已加载到内存，可接收事件
        - ``UNLOADED``  已卸载，不再接收事件
        - ``ERROR``     加载或运行过程中出错
        - ``RUNNING``   正在处理事件（瞬时状态，主要用于进程类插件）
    """

    LOADED = "loaded"
    UNLOADED = "unloaded"
    ERROR = "error"
    RUNNING = "running"


# ======================================================================
# 插件信息
# ======================================================================


@dataclass
class PluginInfo:
    """插件元信息。

    一个 ``PluginInfo`` 实例完整描述了磁盘上的一个插件目录，包括其身份
    信息、入口文件、依赖、权限以及运行时状态。

    Attributes:
        plugin_id: 插件唯一标识。格式为 ``"<language>:<folder_name>"``，
            例如 ``"python:hello"`` / ``"go:hello"`` / ``"java:hello"``。
            该格式保证跨语言不冲突，并便于 :class:`PluginFileManager`
            直接定位插件目录。
        name: 插件显示名称（人类可读）。
        author: 插件作者。
        version: 插件版本号（语义化版本字符串，如 ``"1.0.0"``）。
        description: 插件功能描述。
        language: 插件编程语言（:class:`PluginLanguage`）。
        status: 插件当前运行状态（:class:`PluginStatus`）。
        folder: 插件目录的绝对路径。
        main_file: 插件入口文件名（相对 ``folder``），如 ``"__init__.py"``
            / ``"main.go"`` / ``"main.jar"``。
        dependencies: 依赖的其他插件 ID 列表。
        permissions: 插件申请的权限列表（如 ``"send_command"``、
            ``"read_chat"``）。
        data_folder: 插件数据目录绝对路径（存放 ``datas.json`` 等数据）。
        log_file: 插件日志文件绝对路径。
        created_at: 插件目录创建时间戳（扫描时取目录 mtime）。
        loaded_at: 插件成功加载的时间戳，未加载时为 ``None``。
        error: 最近的错误信息（无错误时为空串）。
    """

    plugin_id: str = ""
    name: str = ""
    author: str = ""
    version: str = "0.0.0"
    description: str = ""
    language: PluginLanguage = PluginLanguage.PYTHON
    status: PluginStatus = PluginStatus.UNLOADED
    folder: str = ""
    main_file: str = ""
    dependencies: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)
    data_folder: str = ""
    log_file: str = ""
    created_at: float = field(default_factory=time.time)
    loaded_at: Optional[float] = None
    error: str = ""

    # ------------------------------------------------------------------ #
    # 便捷属性
    # ------------------------------------------------------------------ #

    @property
    def folder_name(self) -> str:
        """插件目录名（``folder`` 的最后一级）。"""
        return self.folder.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if self.folder else ""

    @property
    def is_loaded(self) -> bool:
        """插件是否已成功加载。"""
        return self.status == PluginStatus.LOADED and self.loaded_at is not None

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        """将插件信息序列化为 JSON 兼容字典。

        用于 API 响应、WebSocket 推送、配置持久化等场景。枚举字段转换
        为其 ``value``，时间戳原样输出（``None`` 保持为 ``None``）。
        """
        return {
            "plugin_id": self.plugin_id,
            "name": self.name,
            "author": self.author,
            "version": self.version,
            "description": self.description,
            "language": self.language.value,
            "status": self.status.value,
            "folder": self.folder,
            "main_file": self.main_file,
            "dependencies": list(self.dependencies),
            "permissions": list(self.permissions),
            "data_folder": self.data_folder,
            "log_file": self.log_file,
            "created_at": self.created_at,
            "loaded_at": self.loaded_at,
            "error": self.error,
            "folder_name": self.folder_name,
            "is_loaded": self.is_loaded,
        }

    # ------------------------------------------------------------------ #
    # 工厂方法
    # ------------------------------------------------------------------ #

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PluginInfo":
        """从字典构造 :class:`PluginInfo`（与 :meth:`to_dict` 互逆）。

        枚举字段同时接受枚举成员、字符串值。未知键被忽略，缺失键使用默认值。
        """
        def _to_language(value: Any) -> PluginLanguage:
            if isinstance(value, PluginLanguage):
                return value
            if isinstance(value, str):
                try:
                    return PluginLanguage(value.lower())
                except ValueError:
                    return PluginLanguage.PYTHON
            return PluginLanguage.PYTHON

        def _to_status(value: Any) -> PluginStatus:
            if isinstance(value, PluginStatus):
                return value
            if isinstance(value, str):
                try:
                    return PluginStatus(value.lower())
                except ValueError:
                    return PluginStatus.UNLOADED
            return PluginStatus.UNLOADED

        return cls(
            plugin_id=data.get("plugin_id", ""),
            name=data.get("name", ""),
            author=data.get("author", ""),
            version=data.get("version", "0.0.0"),
            description=data.get("description", ""),
            language=_to_language(data.get("language", "python")),
            status=_to_status(data.get("status", "unloaded")),
            folder=data.get("folder", ""),
            main_file=data.get("main_file", ""),
            dependencies=list(data.get("dependencies", []) or []),
            permissions=list(data.get("permissions", []) or []),
            data_folder=data.get("data_folder", ""),
            log_file=data.get("log_file", ""),
            created_at=float(data.get("created_at", time.time()) or time.time()),
            loaded_at=data.get("loaded_at"),
            error=data.get("error", ""),
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"PluginInfo(plugin_id={self.plugin_id!r}, "
            f"language={self.language.value!r}, status={self.status.value!r})"
        )


__all__ = [
    "PluginLanguage",
    "PluginStatus",
    "PluginInfo",
]
