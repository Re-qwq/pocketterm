"""PESignCount 原生签名桥接器 (Python 端)

调用 Go 编译的 pesign_bridge 二进制, 实现 PESignCount 原生签名。

方案一 (PESignCount 原生签名) 的 Python 端实现。

工作流程:
    1. Python 端构造 message (engineVersion + LibMinecraftPE + ...)
    2. 调用 pesign_bridge 二进制 (mode="sign")
    3. 在 Windows 上, Go 通过 cgo 调用 Auth.Sign.dll 的 CountSign 函数
    4. 返回 Base64 编码的 16 字节签名

如果 pesign_bridge 不可用 (Linux 或无 DLL), 回退到 fbauth 模式:
    1. Python 端发送 sauth_json + server_code 给 pesign_bridge (mode="fbauth")
    2. pesign_bridge 通过 FastBuilder 认证服务器代做 PE 认证
    3. 返回 chainInfo + server_address

用法::

    from .pesign_bridge import PESignBridge

    bridge = PESignBridge()
    sign = await bridge.sign(message, offset=2, rounds=9)
    if sign:
        # 使用 sign 完成 PE 认证
        ...
    else:
        # 回退到 fbauth 模式
        result = await bridge.fbauth(sauth_json, server_code)
        chain_info = result.chain_info
        server_address = result.server_address
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("pocketterm.auth.pesign_bridge")


@dataclass
class SignResult:
    """PESignCount 签名结果。"""
    success: bool = False
    sign: str = ""                # Base64 编码的 16 字节签名
    method: str = ""              # "dll" / "fbauth_proxy" / "stub" / "none"
    error: str = ""


@dataclass
class FBAuthResult:
    """FastBuilder 认证结果。"""
    success: bool = False
    chain_info: str = ""
    server_address: str = ""
    uid: str = ""
    username: str = ""
    error: str = ""


def _find_pesign_binary() -> Optional[str]:
    """查找 pesign_bridge 或 pocketterm_ap 二进制文件。

    查找顺序:
        1. PocketTerm/access_point_go/pesign_bridge/pesign_bridge (Linux)
        2. PocketTerm/access_point_go/pesign_bridge/pesign_bridge.exe (Windows)
        3. PocketTerm/access_point_go/pocketterm_ap (已编译的主接入点, 支持 fbauth)
        4. PocketTerm/access_point_go/pocketterm_ap.exe (Windows)
        5. PATH 中的 pesign_bridge 或 pocketterm_ap
    """
    base = Path(__file__).resolve().parent.parent.parent.parent / "access_point_go"
    pesign_dir = base / "pesign_bridge"

    candidates = []
    # pesign_bridge (独立二进制, 优先)
    if sys.platform == "win32":
        candidates.append(pesign_dir / "pesign_bridge.exe")
    else:
        candidates.append(pesign_dir / "pesign_bridge")
    # pocketterm_ap (已编译的主接入点, 包含 fbauth 模式)
    if sys.platform == "win32":
        candidates.append(base / "pocketterm_ap.exe")
    else:
        candidates.append(base / "pocketterm_ap")
    # PATH
    candidates.append("pesign_bridge")
    candidates.append("pocketterm_ap")

    for path in candidates:
        p = Path(path)
        if p.exists() and p.is_file():
            try:
                if sys.platform != "win32":
                    os.chmod(str(p), 0o755)
                return str(p)
            except Exception:
                pass

    return None


class PESignBridge:
    """PESignCount 原生签名桥接器。

    通过调用 Go 编译的 pesign_bridge 二进制实现 PESignCount 原生签名。
    在 Windows 上可通过 cgo 调用 Auth.Sign.dll; 在 Linux 上回退到 fbauth 模式。

    Args:
        binary_path: pesign_bridge 二进制路径; ``None`` 时自动查找。
        dll_path: Auth.Sign.dll 路径 (Windows); ``None`` 时使用默认路径。
        timeout: 调用超时秒数。
    """

    def __init__(
        self,
        binary_path: Optional[str] = None,
        dll_path: Optional[str] = None,
        timeout: float = 30.0,
    ) -> None:
        self._binary_path = binary_path or _find_pesign_binary()
        self._dll_path = dll_path
        self._timeout = timeout

    @property
    def is_available(self) -> bool:
        """pesign_bridge 二进制是否可用。"""
        return self._binary_path is not None

    @property
    def binary_path(self) -> Optional[str]:
        return self._binary_path

    async def sign(
        self,
        message: str,
        offset: int = 2,
        rounds: int = 9,
    ) -> SignResult:
        """调用 PESignCount 生成签名。

        Args:
            message: 要签名的消息 (engineVersion + LibMinecraftPE + patchVersion + PatchHash + seed)。
            offset: PESignCount offset 参数 (默认 2)。
            rounds: PESignCount rounds 参数 (默认 9)。

        Returns:
            :class:`SignResult`, success=True 时 sign 字段为 Base64 编码的签名。
        """
        if not self.is_available:
            return SignResult(
                success=False,
                method="none",
                error="pesign_bridge 二进制不可用",
            )

        config = {
            "mode": "sign",
            "message": message,
            "offset": offset,
            "rounds": rounds,
        }
        if self._dll_path:
            config["dll_path"] = self._dll_path

        result = await self._call_binary(config)
        if result is None:
            return SignResult(
                success=False,
                method="none",
                error="调用 pesign_bridge 失败",
            )

        data = result.get("data", {})
        return SignResult(
            success=data.get("success", False),
            sign=data.get("sign", ""),
            method=data.get("method", ""),
            error=data.get("error", ""),
        )

    async def fbauth(
        self,
        sauth_json: str,
        server_code: str,
        server_password: str = "",
        auth_server: str = "https://nv1.nethard.pro",
        fb_token: str = "",
        username: str = "",
        password: str = "",
        public_key: str = "",
    ) -> FBAuthResult:
        """通过 FastBuilder 认证服务器代做 PE 认证。

        这是 PESignCount 不可用时的回退方案 (方案二)。

        Args:
            sauth_json: 网易 sauth_json。
            server_code: 租赁服编号。
            server_password: 租赁服密码。
            auth_server: FastBuilder 认证服务器地址。
            fb_token: FastBuilder Token (如有)。
            username: FastBuilder 用户名 (用于换取 fb_token)。
            password: FastBuilder 密码 (用于换取 fb_token)。
            public_key: 客户端 ECDH 公钥 (Base64)。

        Returns:
            :class:`FBAuthResult`, success=True 时 chain_info 和 server_address 有值。
        """
        if not self.is_available:
            return FBAuthResult(
                success=False,
                error="pesign_bridge 二进制不可用",
            )

        config = {
            "mode": "fbauth",
            "sauth_json": sauth_json,
            "auth_server": auth_server,
            "server_code": server_code,
            "server_password": server_password,
            "fb_token": fb_token,
            "username": username,
            "password": password,
            "public_key": public_key,
        }

        result = await self._call_binary(config)
        if result is None:
            return FBAuthResult(
                success=False,
                error="调用 pesign_bridge 失败",
            )

        data = result.get("data", {})
        return FBAuthResult(
            success=data.get("success", False),
            chain_info=data.get("chain_info", ""),
            server_address=data.get("server_address", ""),
            uid=data.get("uid", ""),
            username=data.get("username", ""),
            error=data.get("error", ""),
        )

    async def info(self) -> dict:
        """获取 pesign_bridge 二进制的平台信息。

        Returns:
            包含 platform / arch / supports_dll / modes 的字典。
        """
        if not self.is_available:
            return {
                "available": False,
                "platform": sys.platform,
                "reason": "pesign_bridge 二进制未找到",
            }

        config = {"mode": "info"}
        result = await self._call_binary(config)
        if result is None:
            return {
                "available": True,
                "binary_path": self._binary_path,
                "error": "调用 info 模式失败",
            }

        data = result.get("data", {})
        return {
            "available": True,
            "binary_path": self._binary_path,
            **data,
        }

    async def _call_binary(self, config: dict) -> Optional[dict]:
        """调用 pesign_bridge 二进制, 发送配置, 读取结果。

        H-4 修复: 添加 try/finally 确保子进程在任何情况下都被清理,
        避免超时 / 异常 / 错误返回时子进程泄漏。

        Args:
            config: 配置字典。

        Returns:
            解析后的 JSON 输出 (第一条 type=result 的消息); 失败返回 None。
        """
        if not self._binary_path:
            return None

        proc: Optional[asyncio.subprocess.Process] = None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary_path,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # 发送配置
            config_json = json.dumps(config) + "\n"
            proc.stdin.write(config_json.encode())
            await proc.stdin.drain()
            # 关闭 stdin 通知子进程输入结束 (防止子进程等待更多输入)
            proc.stdin.close()

            # 读取输出 (寻找 type=result 的行)
            result = None
            timed_out = False
            while True:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=self._timeout
                    )
                except asyncio.TimeoutError:
                    logger.error("pesign_bridge 调用超时")
                    timed_out = True
                    break

                if not line:
                    break

                line_str = line.decode().strip()
                if not line_str:
                    continue

                try:
                    msg = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.warning(f"无法解析 pesign_bridge 输出: {line_str}")
                    continue

                msg_type = msg.get("type", "")
                if msg_type == "result":
                    result = msg
                    break
                elif msg_type == "log":
                    level = msg.get("level", "info")
                    message = msg.get("message", "")
                    if level == "error":
                        logger.error(f"[pesign_bridge] {message}")
                    elif level == "warn":
                        logger.warning(f"[pesign_bridge] {message}")
                    else:
                        logger.info(f"[pesign_bridge] {message}")
                elif msg_type == "error":
                    error_msg = msg.get("message", "")
                    detail = msg.get("detail", "")
                    logger.error(f"[pesign_bridge] {error_msg}: {detail}")
                    return None

            # 等待进程退出 (超时则强制终止)
            if timed_out:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
            else:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    logger.warning("pesign_bridge 退出超时, 强制终止")
                    try:
                        proc.kill()
                        await proc.wait()
                    except ProcessLookupError:
                        pass

            return result

        except FileNotFoundError:
            logger.error(f"pesign_bridge 二进制不存在: {self._binary_path}")
            return None
        except Exception as exc:
            logger.error(f"调用 pesign_bridge 失败: {exc}")
            return None
        finally:
            # H-4 修复: 确保子进程在任何情况下都被清理
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except (ProcessLookupError, asyncio.TimeoutError):
                    pass


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------
_global_bridge: Optional[PESignBridge] = None


def get_pesign_bridge(
    binary_path: Optional[str] = None,
    dll_path: Optional[str] = None,
) -> PESignBridge:
    """返回全局 :class:`PESignBridge` 单例。"""
    global _global_bridge
    if _global_bridge is None:
        _global_bridge = PESignBridge(binary_path=binary_path, dll_path=dll_path)
    return _global_bridge


def reset_pesign_bridge() -> None:
    """重置全局单例 (用于测试)。"""
    global _global_bridge
    _global_bridge = None


__all__ = [
    "SignResult",
    "FBAuthResult",
    "PESignBridge",
    "get_pesign_bridge",
    "reset_pesign_bridge",
]
