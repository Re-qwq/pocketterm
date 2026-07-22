"""PocketTerm Java 插件加载器

:mod:`app.plugins.java_loader` 负责加载 Java 语言插件。

工作原理
--------
Java 插件以独立 JVM 进程方式运行，与宿主通过 **stdin/stdout JSON 行协议**
通信（协议详见 :class:`app.plugins.base.SubprocessPluginBase`）。

加载流程:
    1. 在插件目录定位入口：优先 ``main.jar``，其次 ``main.java``
    2. 读取 ``plugin.json`` / ``plugin.yaml`` 清单获取元数据
    3. 若为源文件（``.java``），用 ``javac`` 自动编译到 ``classes/`` 目录
    4. 以 :class:`SubprocessPluginBase` 包装 JVM 子进程
    5. ``on_load()`` 启动子进程并等待 ``ready``

支持的入口形式
---------------
    - **JAR 包** —— ``main.jar``，通过 ``java -jar main.jar`` 运行
    - **源文件** —— ``main.java``（及同目录其他 ``.java``），先编译再运行
      ``java -cp classes <MainClass>``。主类名来自清单 ``main_class`` 字段，
      缺省为 ``Main``。

优雅降级
--------
若环境中未安装 JDK/JRE:
    - 运行 JAR 仅需 ``java``（JRE）
    - 编译源文件需要 ``javac``（JDK）
    - 对应工具缺失时 :meth:`load` 抛出 :class:`PluginLoadError`，附带清晰提示
    - :meth:`build_info` / :meth:`can_load` 不依赖 Java，仍可正常工作

Java 插件目录结构::

    plugins/java/<plugin_name>/
    ├── main.jar          # JAR 入口（二选一）
    ├── main.java         # 源文件入口（二选一）
    ├── plugin.json       # 插件清单（可选）
    ├── config.yaml       # 插件配置（可选）
    ├── datas/            # 插件数据目录
    ├── logs/             # 日志目录
    │   └── plugin.log
    └── classes/          # 编译产物（自动生成，源文件模式）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

from .base import PluginContext, PluginDataFolder, SubprocessPluginBase
from .models import PluginInfo, PluginLanguage, PluginStatus

logger = logging.getLogger("pocketterm.plugins.java_loader")


# ======================================================================
# 异常
# ======================================================================


class PluginLoadError(Exception):
    """Java 插件加载失败异常。"""


# ======================================================================
# Java 插件进程包装器
# ======================================================================


class JavaPluginProcess(SubprocessPluginBase):
    """Java 插件进程包装器。

    全部 IPC 逻辑继承自 :class:`~app.plugins.base.SubprocessPluginBase`，
    本类仅用于类型清晰。
    """

    pass


# ======================================================================
# Java 插件加载器
# ======================================================================


class JavaPluginLoader:
    """Java 插件加载器。

    Args:
        plugins_dir: Java 插件根目录（含若干插件子目录）。
            为 ``None`` 时使用默认 ``<PLUGINS_DIR>/java``。
        java_bin: 自定义 ``java`` 可执行文件路径。
        javac_bin: 自定义 ``javac`` 可执行文件路径。
    """

    #: 清单文件名（按优先级）。
    MANIFEST_FILES: List[str] = ["plugin.json", "plugin.yaml", "plugin.yml"]
    #: JAR 入口名。
    JAR_FILE = "main.jar"
    #: 源文件入口名。
    JAVA_FILE = "main.java"
    #: 编译超时秒数。
    COMPILE_TIMEOUT: float = 120.0
    #: 缺省主类名。
    DEFAULT_MAIN_CLASS = "Main"

    def __init__(
        self,
        plugins_dir: Optional[Union[str, Path]] = None,
        java_bin: Optional[str] = None,
        javac_bin: Optional[str] = None,
    ) -> None:
        if plugins_dir is None:
            from app.config import PLUGINS_DIR

            plugins_dir = Path(PLUGINS_DIR) / "java"
        self.plugins_dir: Path = Path(plugins_dir)
        self._java_bin = java_bin
        self._javac_bin = javac_bin

    # ------------------------------------------------------------------ #
    # 环境检测
    # ------------------------------------------------------------------ #

    @property
    def java_executable(self) -> Optional[str]:
        """返回 ``java`` 可执行文件路径，未安装时为 ``None``。"""
        return self._java_bin or shutil.which("java")

    @property
    def javac_executable(self) -> Optional[str]:
        """返回 ``javac`` 可执行文件路径，未安装时为 ``None``。"""
        return self._javac_bin or shutil.which("javac")

    def is_runnable(self) -> bool:
        """是否具备运行 JAR 的能力（仅需 ``java``）。"""
        return self.java_executable is not None

    def is_compilable(self) -> bool:
        """是否具备编译源文件的能力（需 ``javac``）。"""
        return self.javac_executable is not None

    # ------------------------------------------------------------------ #
    # 入口与元数据发现
    # ------------------------------------------------------------------ #

    def _detect_entry(self, plugin_dir: Path) -> Optional[str]:
        """检测入口文件名，返回 ``main.jar`` / ``main.java`` 或 ``None``。"""
        if (plugin_dir / self.JAR_FILE).is_file():
            return self.JAR_FILE
        if (plugin_dir / self.JAVA_FILE).is_file():
            return self.JAVA_FILE
        # 兼容：目录中唯一的 .jar / .java
        jars = sorted(plugin_dir.glob("*.jar"))
        if len(jars) == 1:
            return jars[0].name
        javas = sorted(plugin_dir.glob("*.java"))
        if len(javas) == 1:
            return javas[0].name
        return None

    def can_load(self, plugin_dir: Union[str, Path]) -> bool:
        """判断目录是否为合法的 Java 插件目录。"""
        path = Path(plugin_dir)
        if not path.is_dir():
            return False
        return self._detect_entry(path) is not None

    def _read_manifest(self, plugin_dir: Path) -> Dict[str, Any]:
        """读取清单文件，不存在则返回空字典。"""
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

    def _guess_main_class(self, plugin_dir: Path, manifest: Dict[str, Any]) -> str:
        """推断主类名。

        优先级: 清单 ``main_class`` > 源文件中的 ``public class`` 名 >
        ``Main``。
        """
        explicit = manifest.get("main_class")
        if isinstance(explicit, str) and explicit:
            return explicit

        # 扫描 main.java 寻找 public class
        main_java = plugin_dir / self.JAVA_FILE
        if main_java.is_file():
            try:
                content = main_java.read_text(encoding="utf-8")
                match = re.search(r"\bpublic\s+(?:final\s+)?class\s+(\w+)", content)
                if match:
                    return match.group(1)
            except OSError:
                pass
        return self.DEFAULT_MAIN_CLASS

    def build_info(self, plugin_dir: Union[str, Path]) -> PluginInfo:
        """根据插件目录构建 :class:`PluginInfo`（静态分析，不依赖 Java）。"""
        plugin_dir = Path(plugin_dir).resolve()
        if not plugin_dir.is_dir():
            raise PluginLoadError(f"插件目录不存在: {plugin_dir}")

        entry = self._detect_entry(plugin_dir)
        if entry is None:
            raise PluginLoadError(
                f"未找到 Java 入口文件 ({self.JAR_FILE} 或 {self.JAVA_FILE}): {plugin_dir}"
            )

        folder_name = plugin_dir.name
        manifest = self._read_manifest(plugin_dir)

        data_folder = plugin_dir / "datas"
        logs_dir = plugin_dir / "logs"
        log_file = logs_dir / "plugin.log"

        return PluginInfo(
            plugin_id=f"{PluginLanguage.JAVA.value}:{folder_name}",
            name=manifest.get("name", folder_name),
            author=manifest.get("author", ""),
            version=manifest.get("version", "0.0.0"),
            description=manifest.get("description", ""),
            language=PluginLanguage.JAVA,
            status=PluginStatus.UNLOADED,
            folder=str(plugin_dir),
            main_file=entry,
            dependencies=list(manifest.get("dependencies", []) or []),
            permissions=list(manifest.get("permissions", []) or []),
            data_folder=str(data_folder),
            log_file=str(log_file),
            created_at=plugin_dir.stat().st_mtime if plugin_dir.exists() else time.time(),
        )

    # ------------------------------------------------------------------ #
    # 编译（源文件模式）
    # ------------------------------------------------------------------ #

    def _classes_dir(self, plugin_dir: Path) -> Path:
        """返回编译产物目录。"""
        return plugin_dir / "classes"

    def _needs_compile(self, plugin_dir: Path, classes_dir: Path, main_class: str) -> bool:
        """判断是否需要（重新）编译。

        主类 ``.class`` 不存在，或任一 ``.java`` 比该 ``.class`` 新时编译。
        """
        class_file = classes_dir / f"{main_class}.class"
        if not class_file.exists():
            return True
        cls_mtime = class_file.stat().st_mtime
        for java_file in plugin_dir.glob("*.java"):
            if java_file.stat().st_mtime > cls_mtime:
                return True
        return False

    async def _compile(
        self, plugin_dir: Path, main_class: str
    ) -> Path:
        """编译 Java 源文件到 ``classes/`` 目录。

        Returns:
            ``classes`` 目录路径。

        Raises:
            PluginLoadError: ``javac`` 不可用或编译失败。
        """
        javac = self.javac_executable
        if not javac:
            raise PluginLoadError(
                "JDK 不可用（未在 PATH 中找到 'javac'），"
                "无法编译 Java 源文件插件。请安装 JDK 或提供预编译的 main.jar。"
            )

        classes_dir = self._classes_dir(plugin_dir)
        classes_dir.mkdir(parents=True, exist_ok=True)

        if not self._needs_compile(plugin_dir, classes_dir, main_class):
            logger.debug(f"Java 插件 {plugin_dir.name} 使用缓存的 class 文件")
            return classes_dir

        java_files = sorted(str(p) for p in plugin_dir.glob("*.java"))
        if not java_files:
            raise PluginLoadError(f"未找到任何 .java 源文件: {plugin_dir}")

        cmd = [javac, "-encoding", "UTF-8", "-d", str(classes_dir), *java_files]
        logger.info(f"编译 Java 插件 {plugin_dir.name}: {' '.join(cmd)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(plugin_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise PluginLoadError(f"启动 javac 失败: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.COMPILE_TIMEOUT
            )
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise PluginLoadError(
                f"编译 Java 插件 {plugin_dir.name} 超时（>{self.COMPILE_TIMEOUT}s）"
            )

        if process.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            out_text = stdout.decode("utf-8", errors="replace") if stdout else ""
            detail = (err_text or out_text).strip() or "未知错误"
            raise PluginLoadError(
                f"编译 Java 插件 {plugin_dir.name} 失败 (exit={process.returncode}): {detail}"
            )
        return classes_dir

    # ------------------------------------------------------------------ #
    # 加载 / 卸载
    # ------------------------------------------------------------------ #

    def _build_run_command(
        self,
        plugin_dir: Path,
        entry: str,
        manifest: Dict[str, Any],
    ) -> List[str]:
        """构造运行 JVM 的命令。

        Returns:
            命令列表。

        Raises:
            PluginLoadError: ``java`` 不可用。
        """
        java = self.java_executable
        if not java:
            raise PluginLoadError(
                "JRE 不可用（未在 PATH 中找到 'java'），无法运行 Java 插件。"
                "请安装 JRE/JDK。"
            )

        if entry == self.JAR_FILE:
            return [java, "-jar", str(plugin_dir / self.JAR_FILE)]
        # 源文件模式
        main_class = self._guess_main_class(plugin_dir, manifest)
        classes_dir = self._classes_dir(plugin_dir)
        # classpath: classes 目录 + 当前目录（资源）
        classpath = f"{classes_dir}{os.pathsep}{plugin_dir}"
        return [java, "-cp", classpath, main_class]

    async def load(
        self,
        plugin_dir: Union[str, Path],
        context: Optional[PluginContext] = None,
    ) -> SubprocessPluginBase:
        """加载 Java 插件。

        Args:
            plugin_dir: 插件目录路径。
            context: 可选的插件上下文。

        Returns:
            已启动的 :class:`JavaPluginProcess` 实例。

        Raises:
            PluginLoadError: Java 不可用、编译失败或启动失败。
        """
        plugin_dir = Path(plugin_dir).resolve()
        info = self.build_info(plugin_dir)
        manifest = self._read_manifest(plugin_dir)
        entry = info.main_file

        # 源文件模式：先编译
        if entry == self.JAVA_FILE:
            main_class = self._guess_main_class(plugin_dir, manifest)
            await self._compile(plugin_dir, main_class)

        # 构造运行命令
        command = self._build_run_command(plugin_dir, entry, manifest)

        # 准备上下文与数据目录
        data_folder = PluginDataFolder(info.data_folder, info.log_file)
        ctx = context or PluginContext(data_folder=data_folder, plugin_info=info)
        ctx._data_folder = data_folder  # type: ignore[attr-defined]
        ctx._plugin_info = info  # type: ignore[attr-defined]

        plugin = JavaPluginProcess(
            info=info,
            context=ctx,
            command=command,
            cwd=str(plugin_dir),
            ready_timeout=20.0,
            stop_timeout=6.0,
        )

        # 启动子进程（on_load）
        try:
            ok = await plugin.on_load()
        except Exception as exc:
            raise PluginLoadError(
                f"Java 插件 {info.plugin_id} 启动失败: {exc}"
            ) from exc

        if not ok:
            raise PluginLoadError(
                f"Java 插件 {info.plugin_id} 启动失败: {plugin._start_error}"
            )

        info.status = PluginStatus.LOADED
        info.loaded_at = time.time()
        info.error = ""
        logger.info(f"已加载 Java 插件: {info.plugin_id} ({info.name})")
        return plugin

    async def unload(self, plugin: SubprocessPluginBase) -> bool:
        """卸载 Java 插件（停止 JVM 子进程）。"""
        try:
            ok = await plugin.on_unload()
        except Exception as exc:
            logger.error(f"Java 插件 {plugin.plugin_id} 卸载异常: {exc}")
            ok = False
        plugin.info.status = PluginStatus.UNLOADED
        plugin.info.loaded_at = None
        return bool(ok)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"JavaPluginLoader(plugins_dir={self.plugins_dir!s}, "
            f"java={self.is_runnable()}, javac={self.is_compilable()})"
        )


__all__ = [
    "JavaPluginLoader",
    "JavaPluginProcess",
    "PluginLoadError",
]
