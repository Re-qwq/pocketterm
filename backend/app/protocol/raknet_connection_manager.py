"""RakNet 连接管理器 — 支持协议版本自动协商与自动重连。

本模块在 :mod:`app.protocol.raknet` 的 :class:`RakNetConnection` 之上封装一层
**面向业务**的高层连接管理器, 整合 Community-Bot 逆向发现的三项核心能力:

1. **协议版本自动协商** — 对应 Community-Bot 的
   ``GetRakNetProtocolVersion`` / ``SetRakNetProtocolVersion`` 函数与
   ``[Fail] Incompatible protocol, trying next version...`` 重试逻辑。
   RakNet 握手失败时按优先级逐个尝试候选协议版本, 直至成功或全部耗尽。

2. **自动重连** — 对应 Community-Bot 的 ``IsReconnect`` 属性。连接丢失
   (``ID_CONNECTION_LOST``) 时按退避策略自动重新建立连接。

3. **连接状态追踪** — 对应 Community-Bot 的
   ``ID_CONNECTION_ATTEMPT_FAILED`` / ``ID_CONNECTION_LOST`` /
   ``ID_CONNECTION_REQUEST_ACCEPTED`` / ``ID_DISCONNECTION_NOTIFICATION``
   四种连接状态。

设计原则
========

- **不修改既有模块**: 本模块仅 *使用* :class:`RakNetConnection`, 不修改
  :mod:`app.protocol.raknet` / :mod:`app.protocol.version_manager` 等既有文件。
- **可独立 import**: import 本模块不会发起任何网络请求, 仅惰性创建连接。
- **双版本兼容**: 通过 :class:`~app.protocol.version_manager.MinecraftVersion`
  同时支持网易 3.8 / 3.9 (Bedrock 1.21.80 / 1.21.90)。

逆向来源
========

- ``Community_Bot.exe`` (用户上传) — strings 分析:
  - ``GetRakNetProtocolVersion`` / ``SetRakNetProtocolVersion``
  - ``[Fail] Incompatible protocol, trying next version...``
  - ``[Error] Failed to connect, all protocol versions tried.``
  - ``ID_CONNECTION_ATTEMPT_FAILED`` / ``ID_CONNECTION_LOST`` /
    ``ID_CONNECTION_REQUEST_ACCEPTED`` / ``ID_DISCONNECTION_NOTIFICATION``
  - ``IsReconnect`` (自动重连属性)
  - ``RakNetManagement`` (管理类名)
- ``SLikeNet_DLL_Release_x64.dll`` — Community-Bot 使用的 RakNet 库 (SLikeNet)。
- PocketTerm ``app/protocol/raknet.py`` — 纯 Python RakNet 实现 (被本模块复用)。

典型用法
========

::

    import asyncio
    from app.protocol.raknet_connection_manager import RakNetConnectionManager
    from app.protocol.version_manager import MinecraftVersion

    async def main():
        mgr = RakNetConnectionManager(MinecraftVersion.V3_9)
        mgr.is_reconnect = True  # 启用自动重连
        ok = await mgr.connect("1.2.3.4", 19132)
        if ok:
            await mgr.send_packet(b"\\x00hello")
            data = await mgr.recv_packet()
            await mgr.disconnect()

    asyncio.run(main())
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import List, Optional, Tuple

from app.protocol.version_manager import (
    MinecraftVersion,
    VersionInfo,
    VersionManager,
)

# 复用既有 RakNet 实现 (不修改既有模块, 仅 import)
from app.protocol.raknet import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_PING_INTERVAL,
    DEFAULT_PROTOCOL_VERSION,
    RakNetConnection,
    Reliability,
)

# ---------------------------------------------------------------------------
# Logger (用户指定命名空间 pocketterm.protocol.* )
# ---------------------------------------------------------------------------
_LOGGER_NAME: str = "pocketterm.protocol.raknet_connection_manager"
logger: logging.Logger = logging.getLogger(_LOGGER_NAME)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class IncompatibleProtocolError(ConnectionError):
    """RakNet 协议版本不兼容 (对应 Community-Bot 的
    ``[Fail] Incompatible protocol, trying next version...``)。

    当 RakNet 握手时服务器拒绝当前协议版本, 由 :meth:`RakNetConnectionManager._try_connect`
    抛出, 上层 :meth:`RakNetConnectionManager.connect` 捕获后尝试下一个候选版本。
    """


class AllProtocolsExhaustedError(ConnectionError):
    """所有候选协议版本均已尝试且全部失败 (对应 Community-Bot 的
    ``[Error] Failed to connect, all protocol versions tried.``)。

    由 :meth:`RakNetConnectionManager.connect` 在候选版本列表耗尽后抛出。
    """


class RakNetNotConnectedError(RuntimeError):
    """在未建立连接时调用 send/recv 等方法抛出。"""


# ---------------------------------------------------------------------------
# 连接状态枚举 (对应 Community-Bot 的四种 ID_* 状态)
# ---------------------------------------------------------------------------
class RakNetConnectionState(Enum):
    """RakNet 连接状态 (对应 Community-Bot strings 中的 ``ID_*`` 常量)。

    每个枚举成员的 ``value`` 是 Community-Bot strings 中的原始标识符,
    便于日志对齐与逆向溯源。
    """

    DISCONNECTED = "ID_DISCONNECTION_NOTIFICATION"
    """断开连接 (主动断开或服务器通知)。"""

    ATTEMPT_FAILED = "ID_CONNECTION_ATTEMPT_FAILED"
    """连接尝试失败 (握手被拒 / 协议不兼容 / 超时)。"""

    CONNECTING = "CONNECTING"
    """正在连接 (握手进行中)。"""

    CONNECTED = "ID_CONNECTION_REQUEST_ACCEPTED"
    """连接请求已接受 (握手完成, 可收发数据)。"""

    CONNECTION_LOST = "ID_CONNECTION_LOST"
    """连接丢失 (非主动断开, 触发自动重连的前提)。"""

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# 连接状态字典 (向后兼容 Community-Bot strings 的字面量)
# ---------------------------------------------------------------------------
#: Community-Bot strings 中出现的四种连接状态字面量 → 中文描述。
#: 保留此字典以便上层模块按字面量查询状态。
CONNECTION_STATES: dict[str, str] = {
    "ID_CONNECTION_ATTEMPT_FAILED": "连接尝试失败",
    "ID_CONNECTION_LOST": "连接丢失",
    "ID_CONNECTION_REQUEST_ACCEPTED": "连接请求已接受",
    "ID_DISCONNECTION_NOTIFICATION": "断开连接通知",
}


# ---------------------------------------------------------------------------
# RakNetConnectionManager
# ---------------------------------------------------------------------------
class RakNetConnectionManager:
    """RakNet 连接管理器 — 支持协议版本自动协商和自动重连。

    本类对应 Community-Bot 的 ``RakNetManagement`` 类, 在
    :class:`~app.protocol.raknet.RakNetConnection` 之上提供:

    - **协议版本协商**: :meth:`connect` 在握手失败时逐个尝试
      :data:`PROTOCOL_VERSIONS` 中的候选版本 (对应
      ``[Fail] Incompatible protocol, trying next version...``)。
    - **运行时切换**: :meth:`get_raknet_protocol_version` /
      :meth:`set_raknet_protocol_version` 对应 Community-Bot 的
      ``GetRakNetProtocolVersion`` / ``SetRakNetProtocolVersion``。
    - **自动重连**: :attr:`is_reconnect` 启用后, 连接丢失自动重建。
    - **心跳保活**: :meth:`start_heartbeat` 后台发送 ConnectedPing。

    Parameters
    ----------
    version:
        目标网易版本枚举。若为 ``None``, 使用
        :meth:`~app.protocol.version_manager.VersionManager.get_default`。
    protocol_versions:
        候选协议版本列表 (按优先级排序, 默认 ``[10]`` 即 Bedrock 1.21.x)。
        对应 Community-Bot 的内置候选列表。握手失败时按此顺序逐个尝试。

    Attributes
    ----------
    version : MinecraftVersion
        构造时确定的目标版本 (不可变)。
    info : VersionInfo
        版本元数据快照 (不可变)。
    is_reconnect : bool
        是否启用自动重连 (对应 Community-Bot 的 ``IsReconnect`` 属性)。
        默认 ``False``。
    """

    #: 候选 RakNet 协议版本列表 (按优先级排序)。
    #: Bedrock 1.21.x 全系列使用 ``10`` (来源:
    #: :mod:`app.protocol.raknet` ``DEFAULT_PROTOCOL_VERSION``)。
    #: 若未来需要协议降级协商, 可在此列表追加历史版本 (如 ``11``, ``9``)。
    PROTOCOL_VERSIONS: List[int] = [10]

    def __init__(
        self,
        version: Optional[MinecraftVersion] = None,
        *,
        protocol_versions: Optional[List[int]] = None,
    ) -> None:
        self.version: MinecraftVersion = (
            version if version is not None else VersionManager.get_default()
        )
        self.info: VersionInfo = VersionManager.get_version_info(self.version)

        # 候选协议版本: 优先使用显式传入, 其次使用类常量, 最后用版本配置中的值
        if protocol_versions is not None and len(protocol_versions) > 0:
            self._candidate_protocols: List[int] = list(protocol_versions)
        else:
            base_proto: int = self.info.protocol_version
            self._candidate_protocols = list(self.PROTOCOL_VERSIONS)
            if base_proto not in self._candidate_protocols:
                # 确保版本配置中的协议版本位于候选列表首位
                self._candidate_protocols.insert(0, base_proto)

        # 运行时状态
        self.protocol_version: Optional[int] = None
        self.is_connected: bool = False
        self.is_reconnect: bool = False
        self.connection_attempts: int = 0
        self._state: RakNetConnectionState = RakNetConnectionState.DISCONNECTED

        # 当前底层 RakNet 连接 (每次 _try_connect 重建)
        self._conn: Optional[RakNetConnection] = None

        # 最近一次连接的目标地址 (供 reconnect 使用)
        self._last_endpoint: Optional[Tuple[str, int]] = None

        # 后台任务
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None

        # 重连退避配置
        self.reconnect_base_delay: float = 2.0
        self.reconnect_max_delay: float = 60.0
        self.reconnect_max_attempts: int = 5

        logger.debug(
            "RakNetConnectionManager(version=%s, engine=%s, candidates=%s)",
            self.version,
            self.info.engine_version,
            self._candidate_protocols,
        )

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def state(self) -> RakNetConnectionState:
        """当前连接状态 (对应 Community-Bot 的 ``ID_*`` 常量)。"""
        return self._state

    @property
    def candidate_protocols(self) -> List[int]:
        """候选协议版本列表 (只读副本)。"""
        return list(self._candidate_protocols)

    # ------------------------------------------------------------------
    # RakNet 协议版本 (对应 Community-Bot Get/Set 函数)
    # ------------------------------------------------------------------
    def get_raknet_protocol_version(self) -> int:
        """返回当前 (已协商成功的) RakNet 协议版本。

        对应 Community-Bot 的 ``GetRakNetProtocolVersion`` 函数。

        Returns
        -------
        int
            当前生效的协议版本。若尚未连接, 返回候选列表首项。

        Raises
        ------
        RuntimeError
            候选列表为空时抛出 (理论上不应发生)。
        """
        if self.protocol_version is not None:
            return self.protocol_version
        if self._candidate_protocols:
            return self._candidate_protocols[0]
        raise RuntimeError("候选协议版本列表为空, 无法获取 RakNet 协议版本")

    def set_raknet_protocol_version(self, version: int) -> None:
        """设置当前 RakNet 协议版本。

        对应 Community-Bot 的 ``SetRakNetProtocolVersion`` 函数。
        用于在 RakNet 握手失败时切换到下一个候选协议版本
        (参考 ``[Fail] Incompatible protocol, trying next version...`` 逻辑)。

        Parameters
        ----------
        version:
            新的 RakNet 协议版本 (非负整数, Bedrock 1.21.x 通常为 ``10``)。

        Raises
        ------
        ValueError
            ``version`` 不是非负整数时抛出。
        """
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError(
                f"RakNet 协议版本必须是非负整数, 收到: {version!r}"
            )
        old = self.protocol_version
        self.protocol_version = version
        if old != version:
            logger.info(
                "RakNet 协议版本已切换: %s -> %d (version=%s)",
                old if old is not None else "None",
                version,
                self.version,
            )

    # ------------------------------------------------------------------
    # 连接 (含协议版本自动协商)
    # ------------------------------------------------------------------
    async def connect(self, host: str, port: int) -> bool:
        """连接到游戏服务器, 支持协议版本自动协商。

        对应 Community-Bot 的连接流程: 逐个尝试
        :data:`PROTOCOL_VERSIONS` 中的候选协议版本, 每次失败打印
        ``[Fail] Incompatible protocol, trying next version...`` 并切换版本;
        全部失败后打印 ``[Error] Failed to connect, all protocol versions tried.``
        并返回 ``False``。

        Parameters
        ----------
        host:
            服务器主机 (IP 或域名)。
        port:
            服务器端口 (Bedrock 默认 19132)。

        Returns
        -------
        bool
            ``True`` 表示连接成功; ``False`` 表示所有候选协议版本均失败。
        """
        self._last_endpoint = (host, port)
        self.connection_attempts = 0
        self._state = RakNetConnectionState.CONNECTING

        for pv in self._candidate_protocols:
            self.set_raknet_protocol_version(pv)
            self.connection_attempts += 1
            logger.info(
                "[Connect] 尝试协议版本 %d (attempt=%d/%d, host=%s:%d)",
                pv,
                self.connection_attempts,
                len(self._candidate_protocols),
                host,
                port,
            )
            try:
                result = await self._try_connect(host, port)
            except IncompatibleProtocolError:
                # 协议不兼容 — 对应 Community-Bot 的 [Fail] 日志
                logger.warning(
                    "[Fail] Incompatible protocol, trying next version... "
                    "(tried=%d, host=%s:%d)",
                    pv,
                    host,
                    port,
                )
                # 清理当前失败的连接, 准备下一轮尝试
                await self._cleanup_connection()
                continue
            except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
                # 网络层错误 (超时 / 拒绝 / 不可达) — 同样尝试下一个版本
                logger.warning(
                    "[Fail] 连接错误 (protocol=%d): %s, trying next version...",
                    pv,
                    exc,
                )
                await self._cleanup_connection()
                continue

            if result:
                self.is_connected = True
                self._state = RakNetConnectionState.CONNECTED
                logger.info(
                    "[Success] RakNet 连接已建立 (protocol=%d, host=%s:%d, "
                    "engine=%s)",
                    pv,
                    host,
                    port,
                    self.info.engine_version,
                )
                return True

            # _try_connect 返回 False 但未抛异常 — 视为协议不兼容
            logger.warning(
                "[Fail] Incompatible protocol, trying next version... "
                "(tried=%d, host=%s:%d)",
                pv,
                host,
                port,
            )
            await self._cleanup_connection()

        # 全部候选协议版本耗尽 — 对应 Community-Bot 的 [Error] 日志
        logger.error(
            "[Error] Failed to connect, all protocol versions tried. "
            "(tried=%s, host=%s:%d)",
            self._candidate_protocols,
            host,
            port,
        )
        self._state = RakNetConnectionState.ATTEMPT_FAILED
        self.is_connected = False
        return False

    async def _try_connect(self, host: str, port: int) -> bool:
        """使用当前 :attr:`protocol_version` 尝试建立单次 RakNet 连接。

        内部方法, 由 :meth:`connect` 在协商循环中调用。

        Raises
        ------
        IncompatibleProtocolError
            RakNet 离线握手返回协议版本不兼容时抛出。
        asyncio.TimeoutError
            握手超时。
        """
        # 每次尝试重建底层连接 (不复用失败的 socket)
        await self._cleanup_connection()
        conn = RakNetConnection(
            protocol_version=self.get_raknet_protocol_version(),
            connect_timeout=DEFAULT_CONNECT_TIMEOUT,
            ping_interval=DEFAULT_PING_INTERVAL,
        )
        self._conn = conn
        try:
            await conn.connect(host, port)
        except ConnectionError as exc:
            # RakNet 离线握手失败通常表现为 ConnectionError;
            # 若消息含 "incompatible" / "protocol" 则判定为协议不兼容
            msg = str(exc).lower()
            if "incompatible" in msg or "protocol" in msg:
                raise IncompatibleProtocolError(
                    f"协议版本 {self.get_raknet_protocol_version()} 不兼容: {exc}"
                ) from exc
            raise
        except asyncio.TimeoutError:
            raise
        except OSError:
            raise

        # 连接成功
        return True

    # ------------------------------------------------------------------
    # 重连
    # ------------------------------------------------------------------
    async def reconnect(self) -> bool:
        """重新建立到最近一次连接目标的连接。

        对应 Community-Bot 的 ``IsReconnect`` 自动重连逻辑。
        使用指数退避策略 (base_delay * 2^n, 上限 max_delay)。

        Returns
        -------
        bool
            ``True`` 表示重连成功; ``False`` 表示达到最大重试次数仍失败。
        """
        if self._last_endpoint is None:
            logger.error("[Reconnect] 无可重连的目标地址 (从未成功连接过)")
            return False

        host, port = self._last_endpoint
        logger.info(
            "[Reconnect] 开始自动重连 (host=%s:%d, is_reconnect=%s)",
            host,
            port,
            self.is_reconnect,
        )

        for attempt in range(1, self.reconnect_max_attempts + 1):
            delay = min(
                self.reconnect_base_delay * (2 ** (attempt - 1)),
                self.reconnect_max_delay,
            )
            logger.info(
                "[Reconnect] 第 %d/%d 次尝试, 等待 %.1fs",
                attempt,
                self.reconnect_max_attempts,
                delay,
            )
            await asyncio.sleep(delay)
            ok = await self.connect(host, port)
            if ok:
                logger.info("[Reconnect] 重连成功 (attempt=%d)", attempt)
                return True

        logger.error(
            "[Reconnect] 达到最大重试次数 %d, 放弃重连",
            self.reconnect_max_attempts,
        )
        self._state = RakNetConnectionState.CONNECTION_LOST
        return False

    async def _auto_reconnect_loop(self) -> None:
        """自动重连后台协程 (当 :attr:`is_reconnect` 为 True 时由
        :meth:`_on_connection_lost` 启动)。"""
        try:
            await self.reconnect()
        except asyncio.CancelledError:
            logger.debug("[Reconnect] 自动重连协程已取消")
            raise
        except Exception as exc:  # noqa: BLE001 — 后台任务不应向上抛出
            logger.exception("[Reconnect] 自动重连协程异常: %s", exc)

    def _on_connection_lost(self) -> None:
        """连接丢失时的内部回调 (对应 ``ID_CONNECTION_LOST``)。

        若 :attr:`is_reconnect` 为 True, 启动后台重连协程。
        """
        self.is_connected = False
        self._state = RakNetConnectionState.CONNECTION_LOST
        logger.warning("[Lost] RakNet 连接丢失 (ID_CONNECTION_LOST)")
        if self.is_reconnect and self._reconnect_task is None:
            self._reconnect_task = asyncio.create_task(
                self._auto_reconnect_loop(),
                name="raknet-auto-reconnect",
            )

    # ------------------------------------------------------------------
    # 断开
    # ------------------------------------------------------------------
    async def disconnect(self) -> None:
        """主动断开连接 (发送 ``ID_DISCONNECTION_NOTIFICATION`` 后关闭)。

        对应 Community-Bot 的 ``ID_DISCONNECTION_NOTIFICATION`` 状态。
        断开后不会触发自动重连 (即使 :attr:`is_reconnect` 为 True)。
        """
        # 取消后台任务
        await self._cancel_background_tasks()

        if self._conn is not None:
            try:
                await self._conn.disconnect()
            except Exception as exc:  # noqa: BLE001 — 断开时忽略底层异常
                logger.debug("[Disconnect] 底层 disconnect 异常 (已忽略): %s", exc)
            finally:
                self._conn = None

        self.is_connected = False
        self._state = RakNetConnectionState.DISCONNECTED
        logger.info("[Disconnect] RakNet 连接已断开 (ID_DISCONNECTION_NOTIFICATION)")

    # ------------------------------------------------------------------
    # 收发
    # ------------------------------------------------------------------
    async def send_packet(
        self,
        packet: bytes,
        *,
        reliability: Reliability = Reliability.RELIABLE_ORDERED,
    ) -> int:
        """发送一个数据包 (封装在 RakNet 数据报中)。

        Parameters
        ----------
        packet:
            已序列化的 Bedrock 数据包字节。
        reliability:
            RakNet 可靠性等级 (默认 ``RELIABLE_ORDERED``)。

        Returns
        -------
        int
            RakNet 数据报序列号。

        Raises
        ------
        RakNetNotConnectedError
            未建立连接时抛出。
        """
        if self._conn is None or not self.is_connected:
            raise RakNetNotConnectedError(
                "RakNet 未连接, 无法发送数据包 (state=%s)" % self._state
            )
        return await self._conn.send(packet, reliability)

    async def recv_packet(self) -> bytes:
        """接收下一条应用层数据包。

        阻塞等待直到有数据可读。若连接中途丢失, 触发自动重连回调。

        Returns
        -------
        bytes
            接收到的应用层数据。

        Raises
        ------
        RakNetNotConnectedError
            未建立连接时抛出。
        """
        if self._conn is None or not self.is_connected:
            raise RakNetNotConnectedError(
                "RakNet 未连接, 无法接收数据包 (state=%s)" % self._state
            )
        try:
            return await self._conn.recv()
        except (ConnectionError, asyncio.IncompleteReadError) as exc:
            logger.warning("[Recv] 连接中断: %s", exc)
            self._on_connection_lost()
            raise

    # ------------------------------------------------------------------
    # 心跳
    # ------------------------------------------------------------------
    async def start_heartbeat(self) -> None:
        """启动后台心跳保活协程。

        定期触发底层 :class:`RakNetConnection` 的 ConnectedPing 机制
        (由 ``ping_interval`` 控制)。对应 Community-Bot 的
        ``http://127.0.0.1:8081/client/HeartBeat?mess=`` 业务心跳概念,
        但此处为 RakNet 层保活。

        若已有心跳任务在运行, 本方法为幂等 (不重复启动)。
        """
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            logger.debug("[Heartbeat] 心跳任务已在运行, 跳过")
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="raknet-heartbeat",
        )
        logger.info("[Heartbeat] 心跳保活协程已启动")

    async def _heartbeat_loop(self) -> None:
        """心跳保活循环 (后台协程)。

        :class:`RakNetConnection` 内部已实现 ConnectedPing/Pong 自动保活,
        本循环主要负责 *监视* 连接健康状态: 周期性检查 ``connected`` 属性,
        一旦发现连接丢失则触发自动重连。
        """
        try:
            while self.is_connected:
                await asyncio.sleep(DEFAULT_PING_INTERVAL)
                if self._conn is not None and not self._conn.connected:
                    logger.warning(
                        "[Heartbeat] 检测到底层连接已断开, 触发重连检查"
                    )
                    self._on_connection_lost()
                    break
        except asyncio.CancelledError:
            logger.debug("[Heartbeat] 心跳协程已取消")
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Heartbeat] 心跳协程异常: %s", exc)

    # ------------------------------------------------------------------
    # 内部清理
    # ------------------------------------------------------------------
    async def _cleanup_connection(self) -> None:
        """清理当前底层连接 (不复用 socket)。"""
        if self._conn is not None:
            try:
                await self._conn.disconnect()
            except Exception:  # noqa: BLE001 — 清理时忽略所有异常
                pass
            self._conn = None

    async def _cancel_background_tasks(self) -> None:
        """取消所有后台任务 (心跳 / 自动重连)。"""
        tasks: List[asyncio.Task] = []
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            tasks.append(self._heartbeat_task)
        if self._reconnect_task is not None and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            tasks.append(self._reconnect_task)
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._heartbeat_task = None
        self._reconnect_task = None

    # ------------------------------------------------------------------
    # 调试 / repr
    # ------------------------------------------------------------------
    def __repr__(self) -> str:
        return (
            f"RakNetConnectionManager(version={self.version}, "
            f"engine={self.info.engine_version!r}, "
            f"protocol={self.protocol_version}, state={self._state}, "
            f"connected={self.is_connected}, reconnect={self.is_reconnect})"
        )


__all__ = [
    "RakNetConnectionManager",
    "RakNetConnectionState",
    "IncompatibleProtocolError",
    "AllProtocolsExhaustedError",
    "RakNetNotConnectedError",
    "CONNECTION_STATES",
]
