"""PocketTerm Go 插件加载器

:mod:`app.plugins.go_loader` 负责加载 Go 语言插件。

工作原理
--------
Go 插件以独立进程方式运行，与宿主通过 **stdin/stdout JSON 行协议** 通信
（协议详见 :class:`app.plugins.base.SubprocessPluginBase`）。

加载流程:
    1. 在插件目录定位 ``main.go``（与可选 ``go.mod``）
    2. 读取 ``plugin.json`` / ``plugin.yaml`` 清单获取元数据
    3. 使用 ``go build`` 编译为二进制（缓存于 ``<plugin_dir>/bin/``）
    4. 以 :class:`SubprocessPluginBase` 包装子进程
    5. ``on_load()`` 启动子进程并等待 ``ready``

优雅降级
--------
若环境中未安装 Go 工具链（``go`` 不在 ``PATH``），加载器:
    - :meth:`GoPluginLoader.is_available` 返回 ``False``
    - :meth:`load` 抛出 :class:`PluginLoadError`，附带清晰提示
    - :meth:`build_info` 仍可正常工作（仅静态分析，不依赖 Go）

这样即使没有 Go 编译环境，插件管理器仍能发现并展示 Go 插件，
只是在尝试加载时给出明确错误。

Go 插件目录结构::

    plugins/go/<plugin_name>/
    ├── main.go          # 入口源文件
    ├── go.mod           # Go 模块定义（可选）
    ├── plugin.json      # 插件清单（可选）
    ├── config.yaml      # 插件配置（可选）
    ├── datas/           # 插件数据目录
    ├── logs/            # 日志目录
    │   └── plugin.log
    └── bin/             # 编译产物缓存（自动生成）
        └── <plugin_name>(.exe)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover
    yaml = None  # type: ignore

from .base import PluginContext, PluginDataFolder, SubprocessPluginBase
from .models import PluginInfo, PluginLanguage, PluginStatus

logger = logging.getLogger("pocketterm.plugins.go_loader")


# ======================================================================
# 异常
# ======================================================================


class PluginLoadError(Exception):
    """Go 插件加载失败异常。"""


# ======================================================================
# Go 插件进程包装器
# ======================================================================


class GoPluginProcess(SubprocessPluginBase):
    """Go 插件进程包装器。

    本类仅用于类型清晰，全部 IPC 逻辑继承自
    :class:`~app.plugins.base.SubprocessPluginBase`。加载器构造时传入
    编译后的二进制路径作为启动命令。
    """

    pass


# ======================================================================
# Go 插件加载器
# ======================================================================


class GoPluginLoader:
    """Go 插件加载器。

    Args:
        plugins_dir: Go 插件根目录（含若干插件子目录）。
            为 ``None`` 时使用默认 ``<PLUGINS_DIR>/go``。
        go_bin: 自定义 ``go`` 可执行文件路径。为 ``None`` 时从 ``PATH``
            查找。
    """

    #: 清单文件名（按优先级）。
    MANIFEST_FILES: List[str] = ["plugin.json", "plugin.yaml", "plugin.yml"]
    #: Go 源文件入口名。
    MAIN_FILE = "main.go"
    #: 编译超时秒数。
    COMPILE_TIMEOUT: float = 120.0

    def __init__(
        self,
        plugins_dir: Optional[Union[str, Path]] = None,
        go_bin: Optional[str] = None,
    ) -> None:
        if plugins_dir is None:
            from app.config import PLUGINS_DIR

            plugins_dir = Path(PLUGINS_DIR) / "go"
        self.plugins_dir: Path = Path(plugins_dir)
        self._go_bin: Optional[str] = go_bin

    # ------------------------------------------------------------------ #
    # 环境检测
    # ------------------------------------------------------------------ #

    @property
    def go_executable(self) -> Optional[str]:
        """返回 ``go`` 可执行文件路径，未安装时为 ``None``。"""
        if self._go_bin:
            return self._go_bin
        return shutil.which("go")

    def is_available(self) -> bool:
        """Go 工具链是否可用。"""
        return self.go_executable is not None

    # ------------------------------------------------------------------ #
    # 入口与元数据发现
    # ------------------------------------------------------------------ #

    def can_load(self, plugin_dir: Union[str, Path]) -> bool:
        """判断目录是否为合法的 Go 插件目录（含 ``main.go``）。"""
        path = Path(plugin_dir)
        if not path.is_dir():
            return False
        return (path / self.MAIN_FILE).is_file()

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

    def _read_module_name(self, plugin_dir: Path) -> str:
        """从 ``go.mod`` 读取模块名，失败时返回目录名。"""
        go_mod = plugin_dir / "go.mod"
        if not go_mod.is_file():
            return plugin_dir.name
        try:
            with open(go_mod, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line.startswith("module "):
                        return line[len("module "):].strip()
        except OSError:
            pass
        return plugin_dir.name

    def build_info(self, plugin_dir: Union[str, Path]) -> PluginInfo:
        """根据插件目录构建 :class:`PluginInfo`（静态分析，不依赖 Go 工具链）。"""
        plugin_dir = Path(plugin_dir).resolve()
        if not plugin_dir.is_dir():
            raise PluginLoadError(f"插件目录不存在: {plugin_dir}")
        if not (plugin_dir / self.MAIN_FILE).is_file():
            raise PluginLoadError(f"未找到 Go 入口文件 {self.MAIN_FILE}: {plugin_dir}")

        folder_name = plugin_dir.name
        manifest = self._read_manifest(plugin_dir)
        module_name = self._read_module_name(plugin_dir)

        data_folder = plugin_dir / "datas"
        logs_dir = plugin_dir / "logs"
        log_file = logs_dir / "plugin.log"

        return PluginInfo(
            plugin_id=f"{PluginLanguage.GO.value}:{folder_name}",
            name=manifest.get("name", folder_name),
            author=manifest.get("author", ""),
            version=manifest.get("version", "0.0.0"),
            description=manifest.get("description", ""),
            language=PluginLanguage.GO,
            status=PluginStatus.UNLOADED,
            folder=str(plugin_dir),
            main_file=self.MAIN_FILE,
            dependencies=list(manifest.get("dependencies", []) or []),
            permissions=list(manifest.get("permissions", []) or []),
            data_folder=str(data_folder),
            log_file=str(log_file),
            created_at=plugin_dir.stat().st_mtime if plugin_dir.exists() else time.time(),
        )

    # ------------------------------------------------------------------ #
    # 编译
    # ------------------------------------------------------------------ #

    def _binary_path(self, plugin_dir: Path) -> Path:
        """返回编译产物路径。"""
        bin_dir = plugin_dir / "bin"
        exe_name = plugin_dir.name + (".exe" if sys.platform == "win32" else "")
        return bin_dir / exe_name

    def _needs_compile(self, plugin_dir: Path, binary: Path) -> bool:
        """判断是否需要（重新）编译。

        二进制不存在，或任一 ``.go`` 源文件比二进制新时需要编译。
        """
        if not binary.exists():
            return True
        bin_mtime = binary.stat().st_mtime
        for go_file in plugin_dir.rglob("*.go"):
            if go_file.stat().st_mtime > bin_mtime:
                return True
        # go.mod 变更也触发重编译
        go_mod = plugin_dir / "go.mod"
        if go_mod.exists() and go_mod.stat().st_mtime > bin_mtime:
            return True
        return False

    async def _compile(self, plugin_dir: Path) -> Path:
        """编译 Go 插件为二进制。

        Returns:
            编译产物路径。

        Raises:
            PluginLoadError: Go 不可用或编译失败。
        """
        go = self.go_executable
        if not go:
            raise PluginLoadError(
                "Go 工具链不可用（未在 PATH 中找到 'go'），"
                "无法编译 Go 插件。请安装 Go 或移除该 Go 插件。"
            )

        binary = self._binary_path(plugin_dir)
        binary.parent.mkdir(parents=True, exist_ok=True)

        if not self._needs_compile(plugin_dir, binary):
            logger.debug(f"Go 插件 {plugin_dir.name} 使用缓存二进制: {binary}")
            return binary

        cmd = [go, "build", "-o", str(binary)]
        # 有 go.mod 时在目录内构建；无 go.mod 时直接构建单文件
        if (plugin_dir / "go.mod").exists():
            cmd.append(".")
        else:
            cmd.append(self.MAIN_FILE)

        logger.info(f"编译 Go 插件 {plugin_dir.name}: {' '.join(cmd)}")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(plugin_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise PluginLoadError(f"启动 go build 失败: {exc}") from exc

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
                f"编译 Go 插件 {plugin_dir.name} 超时（>{self.COMPILE_TIMEOUT}s）"
            )

        if process.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace") if stderr else ""
            out_text = stdout.decode("utf-8", errors="replace") if stdout else ""
            detail = (err_text or out_text).strip() or "未知错误"
            raise PluginLoadError(
                f"编译 Go 插件 {plugin_dir.name} 失败 (exit={process.returncode}): {detail}"
            )

        if not binary.exists():
            raise PluginLoadError(
                f"编译完成但未找到产物二进制: {binary}"
            )
        # 设置可执行权限（非 Windows）
        if sys.platform != "win32":
            try:
                binary.chmod(0o755)
            except OSError:  # pragma: no cover - 最佳努力
                pass
        return binary

    # ------------------------------------------------------------------ #
    # 加载 / 卸载
    # ------------------------------------------------------------------ #

    async def load(
        self,
        plugin_dir: Union[str, Path],
        context: Optional[PluginContext] = None,
    ) -> SubprocessPluginBase:
        """加载 Go 插件。

        Args:
            plugin_dir: 插件目录路径。
            context: 可选的插件上下文。

        Returns:
            已启动的 :class:`GoPluginProcess` 实例。

        Raises:
            PluginLoadError: Go 不可用、编译失败或启动失败。
        """
        plugin_dir = Path(plugin_dir).resolve()
        info = self.build_info(plugin_dir)

        # 编译
        binary = await self._compile(plugin_dir)

        # 准备上下文与数据目录
        data_folder = PluginDataFolder(info.data_folder, info.log_file)
        ctx = context or PluginContext(data_folder=data_folder, plugin_info=info)
        ctx._data_folder = data_folder  # type: ignore[attr-defined]
        ctx._plugin_info = info  # type: ignore[attr-defined]

        command = [str(binary)]
        plugin = GoPluginProcess(
            info=info,
            context=ctx,
            command=command,
            cwd=str(plugin_dir),
            ready_timeout=15.0,
            stop_timeout=5.0,
        )

        # 启动子进程（on_load）
        try:
            ok = await plugin.on_load()
        except Exception as exc:
            raise PluginLoadError(
                f"Go 插件 {info.plugin_id} 启动失败: {exc}"
            ) from exc

        if not ok:
            raise PluginLoadError(
                f"Go 插件 {info.plugin_id} 启动失败: {plugin._start_error}"
            )

        info.status = PluginStatus.LOADED
        info.loaded_at = time.time()
        info.error = ""
        logger.info(f"已加载 Go 插件: {info.plugin_id} ({info.name})")
        return plugin

    async def unload(self, plugin: SubprocessPluginBase) -> bool:
        """卸载 Go 插件（停止子进程）。"""
        try:
            ok = await plugin.on_unload()
        except Exception as exc:
            logger.error(f"Go 插件 {plugin.plugin_id} 卸载异常: {exc}")
            ok = False
        plugin.info.status = PluginStatus.UNLOADED
        plugin.info.loaded_at = None
        return bool(ok)

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"GoPluginLoader(plugins_dir={self.plugins_dir!s}, available={self.is_available()})"


__all__ = [
    "GoPluginLoader",
    "GoPluginProcess",
    "PluginLoadError",
]
