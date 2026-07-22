"""接入点管理器

:class:`AccessPointManager` 负责统一管理多个接入点实例，提供:

    - **检测**:  扫描本地目录，检测已下载的接入点二进制
    - **下载**:  从 GitHub Release 下载缺失的接入点二进制
    - **创建**:  根据类型 + 配置创建接入点实例
    - **列举**:  列出所有可用 / 已注册的接入点
    - **生命周期**: 启动、停止、自动选择

接入点类型优先级（默认）::

    FateArk > NeOmega > Custom

    FateArk 通过 stdin/stdout 通信，延迟最低；
    NeOmega 通过 WebSocket 通信，功能最全；
    Custom 为自建框架，无外部依赖但协议尚未完善。

典型用法::

    from .manager import AccessPointManager

    mgr = AccessPointManager(binary_dir="/opt/pocketterm/bin")

    # 检测已下载的接入点
    available = mgr.list_available()

    # 下载缺失的接入点
    await mgr.download("fateark")

    # 创建接入点实例
    ap = mgr.create("neomega", {
        "server_code": "123456",
        "auth_server": "https://...",
    })
    await ap.start()
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .base import (
    AccessPoint,
    AccessPointInfo,
    AccessPointStatus,
    BinaryNotFoundError,
    Colors,
)
from .custom import CustomAccessPoint
from .fateark import FateArkAccessPoint
from .neomega import NeOmegaAccessPoint
from .pure_python import PurePythonAccessPoint

logger = logging.getLogger("pocketterm.access_point.manager")

# ======================================================================
# 常量
# ======================================================================

#: 默认接入点优先级（高 -> 低）
#: PurePython 优先（内置协议层，无需外部二进制，完全自主可控）
#: FateArk 次之（stdin/stdout 通信延迟最低）
#: NeOmega 第三（WebSocket 通信）
#: Custom 最后（自建框架，协议尚不完善）
PRIORITY_ORDER: tuple[str, ...] = ("purepython", "fateark", "neomega", "custom")

#: 接入点类型 -> 类的映射
AP_REGISTRY: dict[str, type[AccessPoint]] = {
    "purepython": PurePythonAccessPoint,
    "neomega": NeOmegaAccessPoint,
    "fateark": FateArkAccessPoint,
    "custom": CustomAccessPoint,
}

#: 接入点中文名称
AP_DISPLAY_NAMES: dict[str, str] = {
    "purepython": "纯Python协议",
    "neomega": "NeOmega",
    "fateark": "FateArk",
    "custom": "自建接入点",
}

#: 接入点默认下载版本
AP_DEFAULT_VERSIONS: dict[str, str] = {
    "purepython": "",  # 纯Python无需下载
    "neomega": "v1.0.0",
    "fateark": "v1.0.0",
    "custom": "",  # 自建接入点无需下载
}


# ======================================================================
# 主类
# ======================================================================


class AccessPointManager:
    """接入点管理器。

    管理多个接入点实例的生命周期，提供检测、下载、创建、列举等功能。

    Args:
        binary_dir: 接入点二进制文件存放目录。
            ``None`` 时仅可使用 Custom 接入点（无需二进制）。
        priority: 接入点优先级顺序（``launch_type`` 列表）。
        auto_detect: 构造时是否自动检测已下载的接入点。
    """

    def __init__(
        self,
        binary_dir: Optional[str] = None,
        *,
        priority: tuple[str, ...] = PRIORITY_ORDER,
        auto_detect: bool = True,
    ) -> None:
        self.binary_dir: Optional[str] = binary_dir
        self.priority: tuple[str, ...] = tuple(priority)
        #: 已创建的接入点实例 {ap_id -> AccessPoint}
        self._instances: dict[str, AccessPoint] = {}
        #: 已检测到的可用接入点类型列表
        self._detected: list[str] = []
        #: 下载锁（防止并发下载同一文件）- H-8 修复: 懒加载
        self._download_lock: Optional[asyncio.Lock] = None

        if auto_detect and binary_dir:
            self._detect_available()

    def _get_download_lock(self) -> asyncio.Lock:
        """获取下载锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._download_lock is None:
            self._download_lock = asyncio.Lock()
        return self._download_lock

    # ------------------------------------------------------------------ #
    # 属性
    # ------------------------------------------------------------------ #

    @property
    def instance_count(self) -> int:
        """已创建的接入点实例数。"""
        return len(self._instances)

    @property
    def instances(self) -> list[AccessPoint]:
        """所有接入点实例列表。"""
        return list(self._instances.values())

    # ------------------------------------------------------------------ #
    # 检测
    # ------------------------------------------------------------------ #

    def detect_available(self) -> list[str]:
        """检测本地已下载的接入点二进制。

        扫描 ``binary_dir`` 目录，查找 NeOmega 和 FateArk 二进制文件。
        Custom 接入点无需二进制，始终可用。

        Returns:
            已检测到的接入点类型列表（按优先级排序）。

        Note:
            此方法同时更新内部 ``_detected`` 列表。
        """
        self._detect_available()
        return list(self._detected)

    def _detect_available(self) -> None:
        """内部检测方法。"""
        self._detected = []

        if self.binary_dir is None:
            # 无二进制目录，仅 Custom 和 PurePython 可用 (均无需二进制)
            self._detected = ["custom", "purepython"]
            self._print_detection_result()
            return

        bin_dir = Path(self.binary_dir)
        if not bin_dir.is_dir():
            self._detected = ["custom", "purepython"]
            self._print_detection_result()
            return

        # 临时配置用于 find_binary
        temp_config: dict[str, Any] = {"binary_dir": str(bin_dir)}

        # 按优先级检测
        for name in self.priority:
            if name in ("custom", "purepython"):
                self._detected.append(name)
                continue

            cls = AP_REGISTRY.get(name)
            if cls is None:
                continue

            # 创建临时实例来调用 find_binary
            ap = cls(temp_config)
            binary = ap.find_binary()
            if binary is not None:
                self._detected.append(name)

        self._print_detection_result()

    def _print_detection_result(self) -> None:
        """打印检测结果到控制台。"""
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{Colors.BOLD}{Colors.CYAN}[AccessPointManager]{Colors.RESET} "
            "检测可用接入点...",
            flush=True,
        )
        for name in self.priority:
            display = AP_DISPLAY_NAMES.get(name, name)
            if name in self._detected:
                print(
                    f"  {Colors.colorize('[+]', Colors.GREEN)} "
                    f"{Colors.colorize(display, Colors.GREEN)} "
                    f"{Colors.DIM}已就绪{Colors.RESET}",
                    flush=True,
            )
            else:
                print(
                    f"  {Colors.colorize('[-]', Colors.RED)} "
                    f"{Colors.colorize(display, Colors.RED)} "
                    f"{Colors.DIM}未安装{Colors.RESET}",
                    flush=True,
                )

    # ------------------------------------------------------------------ #
    # 下载
    # ------------------------------------------------------------------ #

    async def download(self, name: str, version: str = "") -> Path:
        """下载指定接入点的二进制文件。

        目前支持下载 NeOmega 和 FateArk。
        Custom 接入点无需下载。

        Args:
            name: 接入点类型名称 (``"neomega"`` / ``"fateark"``)。
                大小写不敏感。
            version: 指定版本号。为空时使用默认版本。

        Returns:
            下载后的二进制文件路径。

        Raises:
            ValueError: 不支持的接入点类型。
            RuntimeError: ``binary_dir`` 未设置。
        """
        name_lower = name.lower()

        if name_lower == "custom":
            raise ValueError("自建接入点无需下载二进制")

        if self.binary_dir is None:
            raise RuntimeError("binary_dir 未设置，无法下载")

        cls = AP_REGISTRY.get(name_lower)
        if cls is None:
            raise ValueError(f"不支持的接入点类型: {name}")

        if not hasattr(cls, "download"):
            raise ValueError(f"接入点 {name} 不支持下载")

        # 确定版本
        if not version:
            version = AP_DEFAULT_VERSIONS.get(name_lower, "v1.0.0")

        async with self._get_download_lock():
            display = AP_DISPLAY_NAMES.get(name_lower, name_lower)
            timestamp = time.strftime("%H:%M:%S")
            print(
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{Colors.BOLD}{Colors.YELLOW}[AccessPointManager]{Colors.RESET} "
                f"下载 {display} (版本 {version})...",
                flush=True,
            )

            # 调用接入点类的静态 download 方法
            path = await cls.download(self.binary_dir, version)  # type: ignore[attr-defined]

            print(
                f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
                f"{Colors.BOLD}{Colors.GREEN}[AccessPointManager]{Colors.RESET} "
                f"{display} 下载完成: {path}",
                flush=True,
            )

            # 重新检测
            self._detect_available()

            return path

    async def download_all(self, version: str = "") -> dict[str, Any]:
        """下载所有支持的接入点二进制。

        Args:
            version: 指定版本号（所有接入点使用同一版本）。

        Returns:
            下载结果字典 ``{name: {"success": bool, "path": str, "error": str}}``。
        """
        results: dict[str, Any] = {}
        for name in self.priority:
            if name == "custom":
                continue
            try:
                path = await self.download(name, version)
                results[name] = {"success": True, "path": str(path), "error": ""}
            except Exception as exc:
                results[name] = {"success": False, "path": "", "error": str(exc)}
        return results

    # ------------------------------------------------------------------ #
    # 创建 / 注册
    # ------------------------------------------------------------------ #

    def create(
        self,
        ap_type: str,
        config: dict[str, Any],
        status_callback=None,
    ) -> AccessPoint:
        """创建接入点实例。

        根据类型创建对应的接入点实例，并注入 ``binary_dir`` 配置。

        Args:
            ap_type: 接入点类型 (``"neomega"`` / ``"fateark"`` / ``"custom"``)。
                大小写不敏感。
            config: 接入点配置字典。
            status_callback: 状态变更回调（可选）。

        Returns:
            新创建的 :class:`AccessPoint` 实例。

        Raises:
            ValueError: 不支持的接入点类型。
            BinaryNotFoundError: 本地模式下二进制未找到（延迟到 start() 时检查）。
        """
        name_lower = ap_type.lower()
        cls = AP_REGISTRY.get(name_lower)
        if cls is None:
            raise ValueError(
                f"不支持的接入点类型: {ap_type}。"
                f"支持: {', '.join(AP_REGISTRY.keys())}"
            )

        # 注入 binary_dir 到配置
        if self.binary_dir and "binary_dir" not in config:
            config = {**config, "binary_dir": self.binary_dir}

        ap = cls(config, status_callback=status_callback)

        # 注册到实例列表
        self._instances[ap.info.ap_id] = ap

        display = AP_DISPLAY_NAMES.get(name_lower, name_lower)
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{Colors.BOLD}{Colors.CYAN}[AccessPointManager]{Colors.RESET} "
            f"创建 {Colors.colorize(display, Colors.CYAN)} "
            f"(ID: {ap.info.ap_id})",
            flush=True,
        )
        logger.info(f"创建接入点 {display} (ID: {ap.info.ap_id})")

        return ap

    def register(self, ap: AccessPoint) -> None:
        """注册一个已创建的接入点实例。

        Args:
            ap: 接入点实例。
        """
        self._instances[ap.info.ap_id] = ap
        logger.info(f"注册接入点 {ap.launch_type} (ID: {ap.info.ap_id})")

    def get(self, ap_id: str) -> Optional[AccessPoint]:
        """根据 ID 获取接入点实例。

        Args:
            ap_id: 接入点 ID。

        Returns:
            接入点实例;不存在时返回 ``None``。
        """
        return self._instances.get(ap_id)

    def remove(self, ap_id: str) -> bool:
        """移除接入点实例（不会自动停止）。

        Args:
            ap_id: 接入点 ID。

        Returns:
            ``True`` 移除成功;``False`` 不存在。
        """
        if ap_id in self._instances:
            del self._instances[ap_id]
            logger.info(f"移除接入点 (ID: {ap_id})")
            return True
        return False

    # ------------------------------------------------------------------ #
    # 列举
    # ------------------------------------------------------------------ #

    def list_available(self) -> list[dict[str, Any]]:
        """列出所有可用的接入点。

        Returns:
            接入点信息字典列表，每项包含::

                {
                    "type": "neomega",
                    "display_name": "NeOmega",
                    "available": True,
                    "binary_path": "/path/to/binary",
                    "is_custom": False,
                }
        """
        result: list[dict[str, Any]] = []

        for name in self.priority:
            display = AP_DISPLAY_NAMES.get(name, name)
            is_custom = name == "custom"
            available = name in self._detected

            # 查找二进制路径
            binary_path = ""
            if available and not is_custom and self.binary_dir:
                cls = AP_REGISTRY.get(name)
                if cls:
                    temp_config = {"binary_dir": self.binary_dir}
                    ap = cls(temp_config)
                    found = ap.find_binary()
                    if found:
                        binary_path = str(found)

            result.append(
                {
                    "type": name,
                    "display_name": display,
                    "available": available,
                    "binary_path": binary_path,
                    "is_custom": is_custom,
                }
            )

        return result

    def list_instances(self) -> list[dict[str, Any]]:
        """列出所有已创建的接入点实例。

        Returns:
            接入点实例信息列表。
        """
        return [ap.info.to_dict() for ap in self._instances.values()]

    def list_running(self) -> list[AccessPoint]:
        """列出所有正在运行的接入点实例。

        Returns:
            处于 RUNNING 状态的接入点实例列表。
        """
        return [
            ap
            for ap in self._instances.values()
            if ap.info.status == AccessPointStatus.RUNNING
        ]

    # ------------------------------------------------------------------ #
    # 自动选择
    # ------------------------------------------------------------------ #

    def auto_select(self) -> Optional[str]:
        """按优先级自动选择最佳可用接入点。

        遍历 ``priority`` 列表，返回第一个已检测到的接入点类型。

        Returns:
            推荐的接入点类型名称;无可用时返回 ``None``。
        """
        for name in self.priority:
            if name in self._detected:
                return name
        return None

    # ------------------------------------------------------------------ #
    # 生命周期管理
    # ------------------------------------------------------------------ #

    async def start_instance(self, ap_id: str) -> bool:
        """启动指定接入点实例。

        Args:
            ap_id: 接入点 ID。

        Returns:
            ``True`` 启动成功;``False`` 不存在或启动失败。
        """
        ap = self._instances.get(ap_id)
        if ap is None:
            return False
        try:
            await ap.start()
            return True
        except Exception as exc:
            logger.error(f"启动接入点 {ap_id} 失败: {exc}")
            return False

    async def stop_instance(self, ap_id: str) -> bool:
        """停止指定接入点实例。

        Args:
            ap_id: 接入点 ID。

        Returns:
            ``True`` 停止成功;``False`` 不存在。
        """
        ap = self._instances.get(ap_id)
        if ap is None:
            return False
        await ap.stop()
        return True

    async def stop_all(self) -> None:
        """停止所有接入点实例。"""
        timestamp = time.strftime("%H:%M:%S")
        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{Colors.BOLD}{Colors.YELLOW}[AccessPointManager]{Colors.RESET} "
            "停止所有接入点...",
            flush=True,
        )

        tasks = []
        for ap in self._instances.values():
            # Bug 15.1 修复: 之前仅停止 LAUNCHING/RUNNING 状态的实例, 漏掉了
            # DISCONNECTED 状态。处于 DISCONNECTED 的实例可能仍持有子进程或
            # WebSocket 连接资源 (如 neomega.py 的 _terminate_subprocess 需
            # 显式调用才清理), 被漏停会导致资源泄漏。增加 DISCONNECTED 状态。
            if ap.info.status in (
                AccessPointStatus.LAUNCHING,
                AccessPointStatus.RUNNING,
                AccessPointStatus.DISCONNECTED,
            ):
                tasks.append(ap.stop())

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        print(
            f"{Colors.colorize(f'[{timestamp}]', Colors.DIM)} "
            f"{Colors.BOLD}{Colors.GREEN}[AccessPointManager]{Colors.RESET} "
            "所有接入点已停止",
            flush=True,
        )
        logger.info("所有接入点已停止")

    # ------------------------------------------------------------------ #
    # 辅助
    # ------------------------------------------------------------------ #

    def get_status_counts(self) -> dict[str, int]:
        """获取各状态的接入点实例数量。

        Returns:
            ``{status_value: count}`` 字典。
        """
        counts: dict[str, int] = {
            status.value: 0 for status in AccessPointStatus
        }
        for ap in self._instances.values():
            counts[ap.info.status.value] += 1
        return counts


# ======================================================================
# 全局实例
# ======================================================================

#: 全局接入点管理器实例（延迟初始化）
_global_manager: Optional[AccessPointManager] = None

# Bug 15.2 修复: get_manager 中 _global_manager 的 check-then-set 无锁保护,
# 在多线程环境下存在竞态 (两个线程同时通过 is None 检查, 创建两个实例)。
# 加 threading.Lock 双重检查锁定。
_global_manager_lock = threading.Lock()


def get_manager(binary_dir: Optional[str] = None) -> AccessPointManager:
    """获取全局接入点管理器实例。

    首次调用时创建，后续调用返回同一实例。

    Args:
        binary_dir: 二进制目录（仅首次调用时生效）。

    Returns:
        :class:`AccessPointManager` 全局实例。
    """
    global _global_manager
    if _global_manager is None:
        # Bug 15.2 修复: 双重检查锁定, 避免多线程下创建多个实例。
        with _global_manager_lock:
            if _global_manager is None:
                _global_manager = AccessPointManager(binary_dir=binary_dir)
    return _global_manager


__all__ = [
    "AccessPointManager",
    "PRIORITY_ORDER",
    "AP_REGISTRY",
    "AP_DISPLAY_NAMES",
    "get_manager",
]
