"""PocketTerm 插件文件管理器

:mod:`app.plugins.file_manager` 负责插件目录的文件级操作:浏览、上传、
下载、删除、建目录、获取文件树。

目录结构
--------
每个插件拥有独立目录，按语言分根::

    plugins/
    ├── python/<plugin_name>/
    │   ├── __init__.py (或 main.py)
    │   ├── datas.json / datas/
    │   ├── logs/
    │   ├── config.yaml
    │   └── ...
    ├── go/<plugin_name>/
    │   ├── main.go
    │   ├── go.mod
    │   ├── logs/
    │   └── bin/
    └── java/<plugin_name>/
        ├── main.jar (或 main.java)
        ├── logs/
        └── classes/

插件 ID 解析
------------
``plugin_id`` 支持两种形式:

    - **带语言前缀**: ``"python:hello"`` / ``"go:hello"`` / ``"java:hello"``
      直接解析为 ``<plugins_dir>/<language>/<name>``。
    - **裸名称**: ``"hello"`` —— 依次在 ``python`` / ``go`` / ``java``
      子目录中查找匹配的文件夹，取第一个命中。

安全性
------
所有路径操作均校验解析后的绝对路径必须位于对应插件目录之内，
防止 ``..`` 路径穿越攻击。
"""
from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from .models import PluginLanguage

logger = logging.getLogger("pocketterm.plugins.file_manager")


# ======================================================================
# 异常
# ======================================================================


class PluginFileError(Exception):
    """插件文件操作异常（路径不存在、越权访问、IO 错误等）。"""


# ======================================================================
# 插件文件管理器
# ======================================================================


class PluginFileManager:
    """插件文件管理器。

    Args:
        plugins_dir: 插件根目录（含 ``python`` / ``go`` / ``java`` 子目录）。
            为 ``None`` 时使用配置中的 ``PLUGINS_DIR``。
    """

    #: 支持的语言子目录名。
    LANGUAGE_DIRS: List[str] = [
        PluginLanguage.PYTHON.value,
        PluginLanguage.GO.value,
        PluginLanguage.JAVA.value,
    ]

    def __init__(self, plugins_dir: Optional[Union[str, Path]] = None) -> None:
        if plugins_dir is None:
            from app.config import PLUGINS_DIR

            plugins_dir = PLUGINS_DIR
        self.plugins_dir: Path = Path(plugins_dir).resolve()

    # ------------------------------------------------------------------ #
    # 插件目录解析
    # ------------------------------------------------------------------ #

    def _resolve_plugin_dir(self, plugin_id: str) -> Path:
        """将 ``plugin_id`` 解析为插件目录绝对路径。

        Args:
            plugin_id: 插件 ID（带语言前缀或裸名称）。

        Returns:
            插件目录 :class:`Path`。

        Raises:
            PluginFileError: 插件目录不存在。
        """
        if not plugin_id:
            raise PluginFileError("插件 ID 为空")

        # 带语言前缀: "python:hello"
        if ":" in plugin_id:
            lang, name = plugin_id.split(":", 1)
            lang = lang.strip().lower()
            name = name.strip()
            if not name:
                raise PluginFileError(f"插件 ID 格式无效: {plugin_id!r}")
            candidate = self.plugins_dir / lang / name
            if candidate.is_dir():
                return candidate.resolve()
            raise PluginFileError(f"插件目录不存在: {candidate}")

        # 裸名称: 在三个语言目录中查找
        for lang in self.LANGUAGE_DIRS:
            candidate = self.plugins_dir / lang / plugin_id
            if candidate.is_dir():
                return candidate.resolve()

        raise PluginFileError(
            f"未在 {self.plugins_dir} 的 python/go/java 子目录中找到插件: {plugin_id}"
        )

    def _safe_path(self, plugin_dir: Path, relative_path: str) -> Path:
        """返回插件目录下 ``relative_path`` 的绝对路径，并校验不越界。

        Raises:
            PluginFileError: 路径越出插件目录。
        """
        if not relative_path:
            return plugin_dir
        # 拼接并规范化
        target = (plugin_dir / relative_path).resolve()
        try:
            target.relative_to(plugin_dir)
        except ValueError as exc:
            raise PluginFileError(
                f"路径越权访问（超出插件目录）: {relative_path!r}"
            ) from exc
        return target

    # ------------------------------------------------------------------ #
    # 列出文件
    # ------------------------------------------------------------------ #

    def list_files(
        self, plugin_id: str, sub_path: str = ""
    ) -> List[Dict[str, Any]]:
        """列出插件目录（或子目录）下的文件与文件夹。

        Args:
            plugin_id: 插件 ID。
            sub_path: 相对插件目录的子路径（为空表示插件根目录）。

        Returns:
            文件信息字典列表，每项含 ``name`` / ``path`` / ``type`` /
            ``size`` / ``modified``。按文件夹在前、名称升序排列。

        Raises:
            PluginFileError: 目录不存在或越权。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        target = self._safe_path(plugin_dir, sub_path)
        if not target.exists():
            raise PluginFileError(f"路径不存在: {sub_path!r}")
        if not target.is_dir():
            raise PluginFileError(f"不是目录: {sub_path!r}")

        result: List[Dict[str, Any]] = []
        for entry in target.iterdir():
            rel = entry.relative_to(plugin_dir).as_posix()
            try:
                stat = entry.stat()
                size = stat.st_size
                modified = stat.st_mtime
            except OSError:
                size = 0
                modified = 0.0
            result.append(
                {
                    "name": entry.name,
                    "path": rel,
                    "type": "folder" if entry.is_dir() else "file",
                    "size": size,
                    "modified": modified,
                }
            )
        # 文件夹优先，再按名称排序
        result.sort(key=lambda x: (x["type"] != "folder", x["name"].lower()))
        return result

    # ------------------------------------------------------------------ #
    # 上传 / 写入
    # ------------------------------------------------------------------ #

    def upload_file(
        self,
        plugin_id: str,
        filename: str,
        content: Union[str, bytes],
    ) -> Dict[str, Any]:
        """写入（上传）文件到插件目录。

        若文件所在子目录不存在会自动创建。已有同名文件会被覆盖。

        Args:
            plugin_id: 插件 ID。
            filename: 相对插件目录的文件路径（如 ``"config.yaml"`` 或
                ``"logs/note.txt"``）。
            content: 文件内容（字符串或字节）。

        Returns:
            写入结果字典，含 ``path`` / ``size`` / ``modified``。

        Raises:
            PluginFileError: 越权或写入失败。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        target = self._safe_path(plugin_dir, filename)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                target.write_text(content, encoding="utf-8")
            else:
                target.write_bytes(content)
        except OSError as exc:
            raise PluginFileError(f"写入文件失败 {filename!r}: {exc}") from exc

        stat = target.stat()
        rel = target.relative_to(plugin_dir).as_posix()
        logger.info(f"上传插件文件: {plugin_id}/{rel} ({stat.st_size} bytes)")
        return {
            "path": rel,
            "size": stat.st_size,
            "modified": stat.st_mtime,
        }

    # ------------------------------------------------------------------ #
    # 下载 / 读取
    # ------------------------------------------------------------------ #

    def download_file(self, plugin_id: str, filename: str) -> bytes:
        """读取插件目录下的文件为字节。

        Args:
            plugin_id: 插件 ID。
            filename: 相对插件目录的文件路径。

        Returns:
            文件字节内容。

        Raises:
            PluginFileError: 文件不存在、是目录、越权或读取失败。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        target = self._safe_path(plugin_dir, filename)
        if not target.exists():
            raise PluginFileError(f"文件不存在: {filename!r}")
        if target.is_dir():
            raise PluginFileError(f"目标是目录而非文件: {filename!r}")
        try:
            return target.read_bytes()
        except OSError as exc:
            raise PluginFileError(f"读取文件失败 {filename!r}: {exc}") from exc

    def read_file_text(self, plugin_id: str, filename: str, encoding: str = "utf-8") -> str:
        """读取插件文件为文本（便捷方法）。"""
        return self.download_file(plugin_id, filename).decode(encoding, errors="replace")

    # ------------------------------------------------------------------ #
    # 删除
    # ------------------------------------------------------------------ #

    def delete_file(self, plugin_id: str, filename: str) -> bool:
        """删除插件目录下的文件或空目录。

        为安全起见，删除目录时仅允许删除空目录。如需递归删除，使用
        :meth:`delete_tree`。

        Args:
            plugin_id: 插件 ID。
            filename: 相对插件目录的路径。

        Returns:
            ``True`` 删除成功。

        Raises:
            PluginFileError: 不存在、越权、目录非空或删除失败。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        target = self._safe_path(plugin_dir, filename)
        # 禁止删除插件根目录本身
        if target == plugin_dir:
            raise PluginFileError("不允许删除插件根目录")
        if not target.exists():
            raise PluginFileError(f"路径不存在: {filename!r}")
        try:
            if target.is_dir():
                if any(target.iterdir()):
                    raise PluginFileError(
                        f"目录非空，拒绝删除（使用 delete_tree 递归删除）: {filename!r}"
                    )
                target.rmdir()
            else:
                target.unlink()
        except OSError as exc:
            raise PluginFileError(f"删除失败 {filename!r}: {exc}") from exc
        logger.info(f"删除插件文件: {plugin_id}/{filename}")
        return True

    def delete_tree(self, plugin_id: str, folder_name: str) -> bool:
        """递归删除插件目录下的子目录或文件。

        Args:
            plugin_id: 插件 ID。
            folder_name: 相对插件目录的路径。

        Returns:
            ``True`` 删除成功。

        Raises:
            PluginFileError: 不存在、越权或删除失败。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        target = self._safe_path(plugin_dir, folder_name)
        if target == plugin_dir:
            raise PluginFileError("不允许删除插件根目录")
        if not target.exists():
            raise PluginFileError(f"路径不存在: {folder_name!r}")
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            raise PluginFileError(f"递归删除失败 {folder_name!r}: {exc}") from exc
        logger.info(f"递归删除: {plugin_id}/{folder_name}")
        return True

    # ------------------------------------------------------------------ #
    # 创建目录
    # ------------------------------------------------------------------ #

    def create_folder(self, plugin_id: str, folder_name: str) -> Dict[str, Any]:
        """在插件目录下创建子目录（支持多级）。

        Args:
            plugin_id: 插件 ID。
            folder_name: 相对插件目录的目录路径。

        Returns:
            含 ``path`` / ``type`` 的字典。

        Raises:
            PluginFileError: 越权、已存在同名文件或创建失败。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        target = self._safe_path(plugin_dir, folder_name)
        if target.exists():
            if target.is_dir():
                rel = target.relative_to(plugin_dir).as_posix()
                return {"path": rel, "type": "folder", "exists": True}
            raise PluginFileError(f"已存在同名文件: {folder_name!r}")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PluginFileError(f"创建目录失败 {folder_name!r}: {exc}") from exc
        rel = target.relative_to(plugin_dir).as_posix()
        logger.info(f"创建插件目录: {plugin_id}/{rel}")
        return {"path": rel, "type": "folder", "exists": False}

    # ------------------------------------------------------------------ #
    # 文件树
    # ------------------------------------------------------------------ #

    def get_file_tree(
        self, plugin_id: str, sub_path: str = "", max_depth: int = 10
    ) -> Dict[str, Any]:
        """获取插件目录的递归文件树。

        Args:
            plugin_id: 插件 ID。
            sub_path: 起始子路径（为空表示插件根目录）。
            max_depth: 最大递归深度（防止过深目录栈溢出）。

        Returns:
            嵌套字典::

                {
                    "name": "<plugin_name>",
                    "path": ".",
                    "type": "folder",
                    "children": [
                        {"name": "...", "path": "...", "type": "file", "size": 123},
                        {"name": "logs", "path": "logs", "type": "folder", "children": [...]}
                    ]
                }

        Raises:
            PluginFileError: 越权或路径不存在。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        root = self._safe_path(plugin_dir, sub_path)
        if not root.exists():
            raise PluginFileError(f"路径不存在: {sub_path!r}")

        tree = self._build_tree(plugin_dir, root, depth=0, max_depth=max_depth)
        return tree

    def _build_tree(
        self, plugin_dir: Path, current: Path, depth: int, max_depth: int
    ) -> Dict[str, Any]:
        """递归构造文件树节点。"""
        rel = (
            current.relative_to(plugin_dir).as_posix()
            if current != plugin_dir
            else "."
        )
        try:
            stat = current.stat()
            size = stat.st_size
            modified = stat.st_mtime
        except OSError:
            size = 0
            modified = 0.0

        node: Dict[str, Any] = {
            "name": current.name or plugin_dir.name,
            "path": rel,
            "type": "folder" if current.is_dir() else "file",
            "size": size,
            "modified": modified,
        }

        if current.is_dir() and depth < max_depth:
            children: List[Dict[str, Any]] = []
            for entry in sorted(current.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                children.append(
                    self._build_tree(plugin_dir, entry, depth + 1, max_depth)
                )
            node["children"] = children
        elif current.is_dir():
            node["children"] = []
        return node

    # ------------------------------------------------------------------ #
    # 便捷：插件目录初始化
    # ------------------------------------------------------------------ #

    def ensure_plugin_dirs(self, plugin_id: str) -> Dict[str, str]:
        """确保插件的标准子目录（``logs`` / ``datas``）存在。

        Args:
            plugin_id: 插件 ID。

        Returns:
            各标准目录的相对路径字典。
        """
        plugin_dir = self._resolve_plugin_dir(plugin_id)
        standard = ["logs", "datas"]
        result: Dict[str, str] = {}
        for sub in standard:
            path = plugin_dir / sub
            path.mkdir(parents=True, exist_ok=True)
            result[sub] = sub
        return result

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"PluginFileManager(plugins_dir={self.plugins_dir!s})"


__all__ = [
    "PluginFileManager",
    "PluginFileError",
]
