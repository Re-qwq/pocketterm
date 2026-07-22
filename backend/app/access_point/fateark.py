"""FateArk 接入点

FateArk 是网易我的世界联机大厅 / 租赁服的接入点之一。
与 NeOmega 通过 WebSocket 通信不同，FateArk 通过 **stdin / stdout**
进行进程间通信（JSON 行协议，每行一个 JSON 对象）。

二进制文件命名规则::

    FateArk_<os>_<arch>(.exe)
    例:
        FateArk_windows_amd64.exe
        FateArk_linux_amd64
        FateArk_android_aarch64   (Termux)

启动参数::

    FateArk_<os>_<arch> [-s <secondary-auth-proxy>]

FateArk 启动后:
    - 从 **stdin** 读取 JSON 命令（每行一个）
    - 向 **stdout** 输出 JSON 响应 / 事件（每行一个）
    - 向 **stderr** 输出日志信息

通信协议（JSON 行协议）::

    发送 (stdin):
        {"action": "login", "server_code": "123456", "server_password": "...", ...}
        {"action": "send_command", "command": "say hello"}
        {"action": "move", "x": 100, "y": 64, "z": 200}

    接收 (stdout):
        {"type": "login_result", "status": 0, "payload": "..."}
        {"type": "chat", "sender": "Player1", "message": "hi"}
        {"type": "position_update", "x": 100, "y": 64, "z": 200}

典型用法::

    from .fateark import FateArkAccessPoint

    config = {
        "server_code": "123456",
        "server_password": "",
        "auth_server": "https://nv1.nethard.pro",
        "api_key": "...",
        "binary_dir": "/opt/pocketterm/bin",
    }
    ap = FateArkAccessPoint(config)
    await ap.start()
    await ap.send_packet({"action": "login", "server_code": "123456"})
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .base import (
    AccessPoint,
    AccessPointInfo,
    AccessPointStatus,
    BinaryNotFoundError,
    Colors,
    ConnectionTimeoutError,
    PacketHandler,
    SubprocessCrashedError,
)

logger = logging.getLogger("pocketterm.access_point.fateark")

# ======================================================================
# 常量
# ======================================================================

#: FateArk GitHub Release 下载地址模板
FATEARK_DOWNLOAD_URL: str = (
    "https://github.com/ToolDelta/FateArk/releases/download/"
    "{version}/FateArk_{system}_{arch}{ext}"
)

#: FateArk 默认下载版本
DEFAULT_VERSION: str = "v1.0.0"

#: 等待 FateArk 就绪的超时（秒）
DEFAULT_READY_TIMEOUT: float = 30.0


# ======================================================================
# 主类
# ======================================================================


class FateArkAccessPoint(AccessPoint):
    """FateArk 接入点。

    通过 **stdin/stdout** 与 FateArk 子进程通信（JSON 行协议）。

    工作流程::

        1. find_binary()         -> 在 binary_dir 中查找 FateArk 二进制
        2. start()               -> 拉起子进程 (FateArk)
        3. _read_stdout_loop()   -> 后台协程持续读 stdout, 按行解析 JSON
        4. _read_stderr_loop()   -> 后台协程持续读 stderr (日志)
        5. send_packet()          -> 将 JSON 写入子进程 stdin
        6. stop()                -> 关闭 stdin + 终止子进程

    通信格式:
        - **stdin**:  每行一个 JSON 对象（命令）
        - **stdout**: 每行一个 JSON 对象（响应 / 事件）
        - **stderr**: 自由格式文本（日志）

    Args:
        config: 接入点配置字典。常用键:
            - ``server_code``:      租赁服号 / 房间号
            - ``server_password``:  服务器密码
            - ``auth_server``:      认证服务器 URL
            - ``api_key``:          API Key
            - ``fbtoken``:          FastBuilder token
            - ``binary_dir``:       二进制所在目录
            - ``extra_args``:       额外子进程参数列表
            - ``env``:              环境变量字典
        status_callback: 状态变更回调。
    """

    launch_type: str = "FateArk"
    binary_name_patterns: list[str] = [
        "FateArk_{system}_{arch}",
        "FateArk_{system}_{arch}{ext}",
        "FateArk_{system}_{arch}.exe",
    ]
    default_start_port: int = 0  # FateArk 不需要端口，通过 stdin/stdout 通信

    def __init__(
        self,
        config: dict[str, Any],
        status_callback=None,
    ) -> None:
        super().__init__(config=config, status_callback=status_callback)

        # 后台读取协程
        self._stdout_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        # stdin 写锁（防止并发写入交错）- H-8 修复: 懒加载
        self._write_lock: Optional[asyncio.Lock] = None
        # 等待 FateArk 输出就绪标志
        self._ready_event = asyncio.Event()

    def _get_write_lock(self) -> asyncio.Lock:
        """获取写锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    # ------------------------------------------------------------------ #
    # 公开接口实现
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """启动 FateArk 接入点。

        查找二进制 -> 拉起子进程 -> 启动后台读取协程 -> 等待就绪。

        Raises:
            BinaryNotFoundError: 找不到 FateArk 二进制。
            SubprocessCrashedError: 子进程启动后立即退出。
            ConnectionTimeoutError: 就绪超时。
        """
        if self.info.status not in (
            AccessPointStatus.IDLE,
            AccessPointStatus.CRASHED,
            AccessPointStatus.DISCONNECTED,
        ):
            self._log(f"已在 {self.info.status.value} 状态，跳过重复 start()", "warning")
            return

        self.update_status(AccessPointStatus.LAUNCHING)
        self.info.started_at = time.time()

        # 启动子进程
        await self._start_subprocess()

        # 启动后台读取协程
        if self.proc is not None:
            if self.proc.stdout is not None:
                self._stdout_task = asyncio.create_task(self._read_stdout_loop())
            if self.proc.stderr is not None:
                self._stderr_task = asyncio.create_task(self._read_stderr_loop())

        # 等待就绪（子进程稳定运行 + 输出第一行）
        timeout = float(self.config.get("ready_timeout", DEFAULT_READY_TIMEOUT))
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # 超时不一定失败，只要子进程还活着就行
            if self.proc is not None and self.proc.poll() is not None:
                rc = self.proc.returncode
                stderr_text = self._stderr_buf.decode("utf-8", errors="replace")
                self.update_status(AccessPointStatus.CRASHED)
                raise SubprocessCrashedError(
                    f"FateArk 子进程在就绪前退出 (rc={rc})",
                    returncode=rc,
                    stderr=stderr_text,
                    ap_name=self.launch_type,
                )
            self._log(f"就绪等待超时({timeout}s)，但子进程仍在运行", "warning")
        except SubprocessCrashedError:
            raise

        self.update_status(AccessPointStatus.RUNNING)
        self._log(
            f"FateArk 已就绪 (PID={self.info.pid})",
            "info",
        )

    async def stop(self) -> None:
        """停止 FateArk 接入点。

        1. 取消后台读取协程
        2. 关闭子进程 stdin
        3. 终止子进程（SIGTERM -> SIGKILL）
        """
        self._log("正在停止 FateArk...", "info")

        # 取消读取协程
        for task in (self._stdout_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._stdout_task = None
        self._stderr_task = None

        # 关闭 stdin
        if self.proc is not None and self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass

        # 终止子进程
        if self.proc is not None:
            await self._terminate_subprocess()

        self.update_status(AccessPointStatus.IDLE)
        self._log("FateArk 已停止", "info")

    async def send_packet(self, packet: dict[str, Any]) -> bool:
        """通过 stdin 向 FateArk 发送 JSON 命令。

        将 ``packet`` 序列化为 JSON 字符串，追加换行符后写入子进程 stdin。

        Args:
            packet: JSON 可序列化的命令字典。

        Returns:
            ``True`` 发送成功;``False`` 发送失败（子进程未运行或写入异常）。
        """
        if self.proc is None or self.proc.stdin is None:
            self._log("FateArk 子进程未运行，无法发送数据包", "error")
            return False

        if self.proc.poll() is not None:
            self._log("FateArk 子进程已退出，无法发送数据包", "error")
            return False

        try:
            data = json.dumps(packet, ensure_ascii=False) + "\n"
            async with self._get_write_lock():
                self.proc.stdin.write(data.encode("utf-8"))
                await self.proc.stdin.drain()
            self.info.packet_count_sent += 1
            action = packet.get("action") or packet.get("type", "unknown")
            self._log(
                f"发送命令: {Colors.colorize(action, Colors.CYAN)}",
                "debug",
            )
            return True
        except Exception as exc:
            self._log(f"发送数据包失败: {exc}", "error")
            self.info.last_error = str(exc)
            return False

    async def on_packet(self, handler: PacketHandler) -> None:
        """注册数据包接收回调。

        FateArk 从 stdout 输出的每一行 JSON 都会被解析并分发给
        所有已注册的处理器。

        Args:
            handler: 回调函数，接收一个 ``dict`` 参数。
        """
        self.register_packet_handler(handler)
        self._log(f"已注册数据包处理器 (共 {len(self._packet_handlers)} 个)", "debug")

    def get_status(self) -> AccessPointInfo:
        """获取 FateArk 接入点信息。"""
        if self.proc is not None and self.proc.poll() is None:
            self.info.pid = self.proc.pid
        elif self.proc is not None and self.proc.poll() is not None:
            if self.info.status == AccessPointStatus.RUNNING:
                self.update_status(AccessPointStatus.CRASHED)
                self.info.last_error = (
                    f"子进程意外退出 (rc={self.proc.returncode})"
                )
        return self.info

    # ------------------------------------------------------------------ #
    # 子进程管理
    # ------------------------------------------------------------------ #

    async def _start_subprocess(self) -> None:
        """查找二进制并启动 FateArk 子进程。

        Raises:
            BinaryNotFoundError: 找不到二进制文件。
            SubprocessCrashedError: 启动失败。
        """
        binary = self.find_binary()
        if binary is None:
            raise BinaryNotFoundError(
                "找不到 FateArk 二进制; binary_dir="
                f"{self.config.get('binary_dir')!r}, os={sys.platform}",
                ap_name=self.launch_type,
            )

        self.info.binary_path = str(binary)

        # 构造启动参数
        args: list[str] = [str(binary)]

        # 额外参数
        extra_args: Any = self.config.get("extra_args", [])
        if isinstance(extra_args, list):
            args.extend(str(a) for a in extra_args)
        elif isinstance(extra_args, dict):
            sec_proxy = extra_args.get("sec-auth-proxy")
            if sec_proxy:
                args.extend(["-s", str(sec_proxy)])

        # 环境变量
        env = os.environ.copy()
        custom_env = self.config.get("env")
        if isinstance(custom_env, dict):
            env.update(custom_env)

        self._log(
            f"启动子进程: {Colors.colorize(str(binary), Colors.GREEN)}",
            "info",
        )

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=str(binary.parent),
            )
        except FileNotFoundError as exc:
            raise BinaryNotFoundError(
                f"无法执行 FateArk 二进制 {binary}: {exc}",
                ap_name=self.launch_type,
            ) from exc
        except OSError as exc:
            raise SubprocessCrashedError(
                f"启动 FateArk 失败: {exc}",
                ap_name=self.launch_type,
            ) from exc

        self.info.pid = self.proc.pid
        self.info.bind_address = "stdin/stdout"

        # 快速检测：子进程是否立即退出
        await asyncio.sleep(0.5)
        if self.proc.poll() is not None:
            rc = self.proc.returncode
            stderr_text = self._stderr_buf.decode("utf-8", errors="replace")
            self.update_status(AccessPointStatus.CRASHED)
            raise SubprocessCrashedError(
                f"FateArk 子进程启动后立即退出 (rc={rc})",
                returncode=rc,
                stderr=stderr_text,
                ap_name=self.launch_type,
            )

    async def _read_stdout_loop(self) -> None:
        """后台协程:持续读取子进程 stdout，按行解析 JSON 并分发。

        FateArk 的 stdout 输出格式为每行一个 JSON 对象。
        此协程逐行读取，解析后调用 :meth:`_dispatch_packet` 分发给处理器。
        """
        self._log("stdout 读取循环已启动", "debug")
        assert self.proc is not None
        assert self.proc.stdout is not None

        try:
            while True:
                line_bytes = await self.proc.stdout.readline()
                if not line_bytes:
                    # EOF: 子进程关闭了 stdout
                    self._log("FateArk stdout 已关闭 (EOF)", "warning")
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                # 标记就绪
                if not self._ready_event.is_set():
                    self._ready_event.set()
                    self._log("收到首条输出，FateArk 已就绪", "info")

                # 尝试解析 JSON
                try:
                    packet = json.loads(line)
                    if isinstance(packet, dict):
                        await self._dispatch_packet(packet)
                    else:
                        self._log(f"非字典 JSON: {line[:200]}", "warning")
                except json.JSONDecodeError:
                    # 非 JSON 行，可能是纯文本输出
                    self._log(f"[stdout] {line}", "debug")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"stdout 读取循环异常: {exc}", "error")
            self.info.last_error = str(exc)
            self.update_status(AccessPointStatus.DISCONNECTED)

    async def _read_stderr_loop(self) -> None:
        """后台协程:持续读取子进程 stderr（日志信息）。

        FateArk 的 stderr 输出运行日志。此协程逐行读取并打印。
        """
        self._log("stderr 读取循环已启动", "debug")
        assert self.proc is not None
        assert self.proc.stderr is not None

        try:
            while True:
                line_bytes = await self.proc.stderr.readline()
                if not line_bytes:
                    break

                line = line_bytes.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_buf += (line + "\n").encode("utf-8")
                    # 缓存上限 4 MiB
                    if len(self._stderr_buf) > 4 * 1024 * 1024:
                        self._stderr_buf = self._stderr_buf[-4 * 1024 * 1024 :]
                    # 打印 stderr 日志
                    # 根据内容判断级别
                    lower = line.lower()
                    if any(k in lower for k in ("error", "fatal", "panic")):
                        self._log(f"[stderr] {line}", "error")
                    elif any(k in lower for k in ("warn", "warning")):
                        self._log(f"[stderr] {line}", "warning")
                    else:
                        self._log(f"[stderr] {line}", "debug")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"stderr 读取循环异常: {exc}", "debug")

    async def _terminate_subprocess(self) -> None:
        """终止 FateArk 子进程。

        先发 SIGTERM，等待 3 秒；若未退出则发 SIGKILL。
        """
        proc = self.proc
        self.proc = None
        self.info.pid = None
        if proc is None or proc.returncode is not None:
            return

        self._log(f"终止子进程 (PID={proc.pid})...", "info")

        try:
            if os.name == "nt":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
        except (OSError, ProcessLookupError):
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            self._log("SIGTERM 超时，发送 SIGKILL...", "warning")
            try:
                if os.name == "nt":
                    proc.kill()
                else:
                    proc.send_signal(signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                self._log(f"子进程未响应 SIGKILL (PID={proc.pid})", "error")

    # ------------------------------------------------------------------ #
    # 二进制下载
    # ------------------------------------------------------------------ #

    @staticmethod
    async def download(binary_dir: str, version: str = DEFAULT_VERSION) -> Path:
        """下载 FateArk 二进制文件。

        从 GitHub Release 下载对应平台的 FateArk 二进制到指定目录。

        Args:
            binary_dir: 二进制存放目录。
            version: FateArk 版本号 (如 ``"v1.0.0"``)。

        Returns:
            下载后的二进制文件路径。

        Note:
            此方法为静态方法，可直接通过类名调用::

                await FateArkAccessPoint.download("/opt/pocketterm/bin")
        """
        from .base import _system_name, _arch_name

        system = _system_name()
        arch = _arch_name()
        ext = ".exe" if os.name == "nt" else ""

        filename = f"FateArk_{system}_{arch}{ext}"
        url = FATEARK_DOWNLOAD_URL.format(
            version=version,
            system=system,
            arch=arch,
            ext=ext,
        )

        bin_dir = Path(binary_dir)
        bin_dir.mkdir(parents=True, exist_ok=True)
        target = bin_dir / filename

        print(
            f"{Colors.colorize('[FateArk]', Colors.BOLD, Colors.MAGENTA)} "
            f"下载二进制: {url}",
            flush=True,
        )

        loop = asyncio.get_running_loop()

        def _download() -> Path:
            import urllib.request

            urllib.request.urlretrieve(url, str(target))
            if ext == "":
                target.chmod(0o755)
            return target

        result = await loop.run_in_executor(None, _download)

        print(
            f"{Colors.colorize('[FateArk]', Colors.BOLD, Colors.GREEN)} "
            f"下载完成: {target}",
            flush=True,
        )
        return result


__all__ = [
    "FateArkAccessPoint",
    "FATEARK_DOWNLOAD_URL",
    "DEFAULT_VERSION",
    "DEFAULT_READY_TIMEOUT",
]
