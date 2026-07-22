"""Minecraft Bedrock 客户端连接管理模块。

纯 Python 实现的 Minecraft Bedrock Edition 客户端, 逆向自 neomega 和
NovaBuilder。本模块负责完整的客户端登录流程管理, 包括:

    1. RakNet UDP 连接 (委托给 :mod:`app.protocol.raknet`)
    2. 服务器握手 (ServerToClientHandshake / ClientToServerHandshake)
    3. JWT 登录链构建与 Login 包发送 (委托给 :mod:`app.protocol.jwt_chain`)
    4. PlayStatus 状态检查
    5. 资源包协商 (ResourcePacksInfo / ResourcePackStack / ClientResponse)
    6. 等待 StartGame 进入游戏
    7. 命令收发 (CommandRequest / CommandOutput)
    8. 聊天消息发送 (Text 包)

依赖的基础协议模块 (均已实现, 直接导入使用):

    - :mod:`app.protocol.varint`       — Varint 编解码
    - :mod:`app.protocol.nbt`          — NBT 网络格式编解码
    - :mod:`app.protocol.compression`  — Batch 批量包压缩/解压
    - :mod:`app.protocol.jwt_chain`    — JWT 登录链构建
    - :mod:`app.protocol.raknet`       — RakNet UDP 传输层

典型用法::

    from app.protocol.connection import BedrockClient, PacketID

    client = BedrockClient(sauth_json="...", device_fingerprint=sa_data)
    await client.connect("example.com", 19132)

    # 发送聊天
    await client.send_chat("Hello, World!")

    # 发送命令并获取输出
    output = await client.send_command("/list")

    # 接收服务器数据包
    packet_id, data = await client.recv_packet()

    await client.disconnect()

注意:
    - 网易服务器可能需要 MCPCheckChallenges 验证, 当前版本暂未实现,
      后续迭代补充。
    - 资源包阶段直接回复 "completed" (空资源包列表), 不下载任何资源包。
    - 命令响应通过 CommandOutput 包异步返回, 使用 asyncio Future 匹配。
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Optional

from app.constants.minecraft import GameVersion
from app.protocol.varint import (
    decode_varint32,
    decode_varuint32,
    decode_varuint64,
    encode_varint32,
    encode_varuint32,
    encode_varuint64,
)
from app.protocol.nbt import marshal_network, unmarshal_network  # noqa: F401
from app.protocol.compression import compress_batch, decompress_batch
from app.protocol.jwt_chain import build_login_chain
from app.protocol.raknet import RakNetConnection, Reliability

logger = logging.getLogger("pocketterm.connection")

# ======================================================================
# 常量
# ======================================================================

#: Batch 批量数据包 ID (RakNet 层之上, 所有游戏包都封装在 Batch 中)
BATCH_PACKET_ID: int = 0xFE

#: 命令响应默认超时 (秒)
COMMAND_TIMEOUT: float = 30.0

#: 登录流程各阶段默认超时 (秒)
HANDSHAKE_TIMEOUT: float = 30.0
LOGIN_TIMEOUT: float = 30.0
PLAY_STATUS_TIMEOUT: float = 30.0
RESOURCE_PACK_TIMEOUT: float = 60.0
START_GAME_TIMEOUT: float = 60.0


class PacketID:
    """Bedrock 数据包 ID 常量 (网易协议)。

    注意: 网易版 Bedrock 的部分数据包 ID 与国际版不同,
    此处的常量值来自 neomega / NovaBuilder 逆向结果。
    """

    # --- 登录与连接管理 ---
    LOGIN = 0x01
    PLAY_STATUS = 0x02
    SERVER_TO_CLIENT_HANDSHAKE = 0x03
    CLIENT_TO_SERVER_HANDSHAKE = 0x04
    DISCONNECT = 0x05

    # --- 资源包 ---
    RESOURCE_PACKS_INFO = 0x06
    RESOURCE_PACK_STACK = 0x07
    RESOURCE_PACK_CLIENT_RESPONSE = 0x08

    # --- 文本与标题 ---
    TEXT = 0x09
    SET_TITLE = 0x58

    # --- 游戏世界 ---
    SET_TIME = 0x0A
    START_GAME = 0x0B
    PLAYER_LIST = 0x0C

    # --- 事件 ---
    SIMPLE_EVENT = 0x0E
    EVENT = 0x0F
    STANDARDIZE_EVENT = 0x11

    # --- 命令 ---
    COMMAND_REQUEST = 0x4D
    COMMAND_OUTPUT = 0x4F

    # --- 玩家输入 ---
    PLAYER_AUTH_INPUT = 0x90

    # --- Text 包类型 ---
    TEXT_TYPE_RAW = 0
    TEXT_TYPE_CHAT = 1
    TEXT_TYPE_TRANSLATION = 2
    TEXT_TYPE_POPUP = 3
    TEXT_TYPE_JUKEBOX_POPUP = 4
    TEXT_TYPE_TIP = 5
    TEXT_TYPE_SYSTEM = 6
    TEXT_TYPE_WHISPER = 7
    TEXT_TYPE_ANNOUNCEMENT = 8
    TEXT_TYPE_OBJECTIVE = 9


#: 已知且较罕见的 PacketID 集合 (值 >= 0x40), 用于 Batch 多包启发式检测。
#:
#: 仅匹配值较大的 PacketID, 是因为这些值在随机二进制数据中较少出现,
#: 可显著降低误报率 (0x01-0x0F 这类小值在 payload 中频繁出现, 误报率高)。
_DISTINCTIVE_PACKET_IDS: frozenset[int] = frozenset(
    {
        PacketID.SET_TITLE,        # 0x58
        PacketID.COMMAND_REQUEST,  # 0x4D
        PacketID.COMMAND_OUTPUT,   # 0x4F
        PacketID.PLAYER_AUTH_INPUT,  # 0x90
    }
)


class PlayStatus:
    """PlayStatus 包状态码。"""

    LOGIN_SUCCESS = 0
    CLIENT_VERSION_MISMATCH = 1
    SERVER_FULL = 2
    EDITOR_TO_VANILLA_MISMATCH = 3


class ResourcePackResponseStatus:
    """ResourcePackClientResponse 包响应状态码。"""

    REFUSED = 0
    SEND_PACKS = 1
    HAVE_ALL_PACKS = 2
    COMPLETED = 3


class CommandOriginType:
    """CommandRequest 包命令来源类型。"""

    PLAYER = 0
    BLOCK = 1
    MINECRAFT_BLOCK = 2
    ENTITY = 3
    DEV_CONSOLE = 4
    TEST = 5
    NPC = 6


class InputMode:
    """PlayerAuthInput 输入模式。"""

    UNSPECIFIED = 0
    MOUSE = 1
    TOUCH = 2
    GAME_PAD = 3
    MOTION_CONTROLLER = 4


class PlayMode:
    """PlayerAuthInput 游戏模式。"""

    NORMAL = 0
    SPECTATOR = 6
    REALITY = 7
    PLACEMENT = 8
    PERSISTENT_EDITOR = 9
    VIEW = 10


# ======================================================================
# 异常
# ======================================================================


class BedrockError(Exception):
    """所有 Bedrock 客户端错误的基类。"""


class LoginError(BedrockError):
    """登录流程中的错误。"""


class ServerRejectedError(LoginError):
    """服务器拒绝登录 (版本不匹配、服务器满等)。"""


class DisconnectError(BedrockError):
    """服务器主动断开连接。"""


class CommandTimeoutError(BedrockError):
    """命令响应超时。"""


# ======================================================================
# 辅助编码函数
# ======================================================================


def encode_string(s: str) -> bytes:
    """编码 Bedrock 字符串。

    Bedrock 字符串格式: ``[Varuint32: 长度] [UTF-8 字节]``

    Args:
        s: 要编码的字符串。

    Returns:
        编码后的字节串。
    """
    raw = s.encode("utf-8")
    return encode_varuint32(len(raw)) + raw


def decode_string(data: bytes, offset: int = 0) -> tuple[str, int]:
    """解码 Bedrock 字符串。

    Args:
        data: 包含字符串的字节串。
        offset: 起始偏移量。

    Returns:
        ``(字符串, 新偏移量)`` 元组。
    """
    length, offset = decode_varuint32(data, offset)
    raw = data[offset : offset + length]
    return raw.decode("utf-8"), offset + length


def encode_uint32_le(value: int) -> bytes:
    """编码无符号 32 位小端整数。"""
    return struct.pack("<I", value)


def encode_int64_le(value: int) -> bytes:
    """编码有符号 64 位小端整数。"""
    return struct.pack("<q", value)


# ======================================================================
# BedrockClient
# ======================================================================


class BedrockClient:
    """Minecraft Bedrock 客户端 — 完整登录流程管理。

    本类封装了从 RakNet 连接到游戏内通信的完整客户端逻辑,
    逆向自 neomega 和 NovaBuilder。

    生命周期::

        client = BedrockClient(sauth_json, device_fingerprint)
        await client.connect("host", 19132)   # 完成登录流程
        await client.send_chat("Hello")        # 发送聊天
        output = await client.send_command("/list")  # 发送命令
        await client.disconnect()              # 断开连接

    属性:
        connected: 是否已建立连接并完成登录。
        spawned: 是否已收到 StartGame (游戏已开始)。
        host: 服务器主机名。
        port: 服务器端口。
        game_version: 协议版本字符串 (如 "1.21.90.0")。
    """

    def __init__(
        self,
        sauth_json: str,
        device_fingerprint: dict,
        chain_info: str = "",
    ) -> None:
        """初始化客户端。

        Args:
            sauth_json: 网易 sauth_json (认证 token 字符串)。
            device_fingerprint: 设备指纹 (来自 constants.py 的 sa_data),
                包含设备型号、游戏版本、皮肤等信息。
            chain_info: 认证服务器返回的 chainInfo (JWT 列表 JSON 字符串)。
                传入时使用在线模式登录, 不传时使用离线自签名模式。
        """
        self._sauth_json: str = sauth_json
        # 防封禁: 合并设备指纹缺失字段 (来自 DeviceFingerprintManager 的指纹
        # 含 device_id / client_random_id / uuid / build_platform 等关键字段,
        # 与 NovaBuilder / NexusE 的 uqholder.Player 对齐)
        self._device_fingerprint: dict = self._merge_fingerprint_fields(
            device_fingerprint
        )
        self._chain_info: str = chain_info
        self._raknet: RakNetConnection = RakNetConnection()

        self._connected: bool = False
        self._spawned: bool = False
        self._host: str = ""
        self._port: int = 0

        #: 协议版本字符串 (引用集中常量,网易升级时统一修改)
        self._game_version: str = self._device_fingerprint.get(
            "game_version", GameVersion
        )

        # --- 命令追踪 ---
        self._command_request_id: int = 0
        self._pending_commands: dict[int, asyncio.Future[str]] = {}

        # --- 数据包队列与后台任务 ---
        self._packet_queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()
        self._recv_task: Optional[asyncio.Task[None]] = None

        # --- Batch 缓冲 (一个 Batch 可能包含多个游戏包) ---
        self._batch_buffer: bytes = b""

        # --- 玩家运行时 ID (由 StartGame 包提供, 用于 PlayerAuthInput) ---
        # StartGame 之前为 0; wait_for_spawn 收到 StartGame 后会解析并存储。
        self._player_runtime_id: int = 0

        # --- 游戏刻计数器 (用于 PlayerAuthInput) ---
        self._tick: int = 0

    # ------------------------------------------------------------------
    # 防封禁: 设备指纹集成
    # ------------------------------------------------------------------
    @staticmethod
    def _merge_fingerprint_fields(device_fingerprint: dict) -> dict:
        """合并设备指纹缺失字段, 保证登录链字段完整。

        与 NovaBuilder / NexusE 的 ``uqholder.Player`` 字段对齐, 缺失时
        使用 ``DeviceFingerprintManager`` 中的随机生成器补齐。这样既能
        兼容旧的常量式指纹 (``constants.sa_data``), 也能直接使用
        :class:`DeviceFingerprint.to_dict()` 输出。

        覆盖字段:
            - DeviceID            -> device_id
            - ClientRandomID     -> client_random_id
            - UUID                -> uuid
            - BuildPlatform      -> build_platform
            - DeviceOS            -> device_os
            - LanguageCode       -> language_code
            - CurrentInputMode   -> current_input_mode
            - DefaultInputMode   -> default_input_mode
            - UIProfile           -> ui_profile
            - IsEditorMode       -> is_editor_mode
            - GameVersion         -> game_version
        """
        if not isinstance(device_fingerprint, dict):
            logger.warning("device_fingerprint 非字典, 创建空指纹")
            device_fingerprint = {}

        # 如果调用方已经传入了 DeviceFingerprint.to_dict() 的输出,
        # 则所有字段都已存在, 直接返回。
        required_keys = (
            "device_id",
            "client_random_id",
            "uuid",
            "build_platform",
        )
        if all(k in device_fingerprint for k in required_keys):
            return dict(device_fingerprint)

        # 否则从全局管理器获取/生成一份指纹并合并缺失字段
        try:
            from app.auth.device_fingerprint import (
                DeviceFingerprint,
                get_fingerprint_manager,
            )

            mgr = get_fingerprint_manager()
            # 用传入的 account_id (若存在) 关联指纹; 否则按 device_id 查找
            account_id = device_fingerprint.get("account_id", "")
            if account_id:
                fp = mgr.get_or_create(account_id=account_id)
            else:
                fp = DeviceFingerprint.generate()

            merged = dict(device_fingerprint)
            fp_dict = fp.to_dict()
            for key, value in fp_dict.items():
                if key not in merged or merged.get(key) in (None, "", 0):
                    merged[key] = value

            # 同时补齐 login_chain_identity 使用的别名 (DeviceID / ClientRandomID 等)
            # 供 build_login_chain 直接读取
            merged.setdefault("DeviceID", merged.get("device_id", ""))
            merged.setdefault("ClientRandomID", merged.get("client_random_id", 0))
            merged.setdefault("ClientRandomId", merged.get("client_random_id", 0))
            merged.setdefault("BuildPlatform", merged.get("build_platform", 0))
            merged.setdefault("DeviceOS", merged.get("device_os", "Windows10"))
            merged.setdefault("LanguageCode", merged.get("language_code", "zh_CN"))
            merged.setdefault("CurrentInputMode", merged.get("current_input_mode", 1))
            merged.setdefault("DefaultInputMode", merged.get("default_input_mode", 1))
            merged.setdefault("UIProfile", merged.get("ui_profile", 0))
            merged.setdefault("IsEditorMode", merged.get("is_editor_mode", False))
            merged.setdefault("IdentityUUID", merged.get("uuid", ""))

            logger.info(
                "BedrockClient 设备指纹已合并: "
                "DeviceID=%s Platform=%s OS=%s UUID=%s",
                str(merged.get("device_id", ""))[:10] + "...",
                merged.get("build_platform"),
                merged.get("device_os"),
                str(merged.get("uuid", ""))[:8] + "...",
            )
            return merged
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "无法从 DeviceFingerprintManager 补齐指纹字段 (%s), "
                "使用原始指纹", exc
            )
            return dict(device_fingerprint)

    @classmethod
    def for_account(
        cls,
        account_id: str,
        sauth_json: str,
        chain_info: str = "",
        *,
        build_platform: Optional[int] = None,
        device_model: Optional[str] = None,
        game_version: Optional[str] = None,
        language_code: Optional[str] = None,
    ) -> "BedrockClient":
        """按账号创建客户端 (自动加载/生成设备指纹)。

        与 NovaBuilder / NexusE 的 ``PlayerKit.GetDeviceID`` 接口对应:
        从持久化的 :class:`DeviceFingerprintManager` 取出账号绑定的指纹,
        保证同一账号每次登录使用相同设备特征, 避免反作弊告警。

        Args:
            account_id: 账号 ID (用于设备指纹隔离)。
            sauth_json: 网易 sauth_json 认证 token。
            chain_info: 认证服务器返回的 chainInfo (在线模式)。
            build_platform: 指定平台编号 (None 自动按权重选取)。
            device_model: 指定设备型号 (None 自动选取)。
            game_version: 指定游戏版本 (None 取配置)。
            language_code: 指定语言代码 (None 取默认 zh_CN)。

        Returns:
            已绑定设备指纹的 :class:`BedrockClient` 实例。
        """
        from app.auth.device_fingerprint import get_fingerprint_manager

        mgr = get_fingerprint_manager()
        fp = mgr.get_or_create(
            account_id=account_id,
            build_platform=build_platform,
            device_model=device_model,
            game_version=game_version,
            language_code=language_code,
        )
        logger.info(
            "为账号 %s 加载设备指纹: %s",
            account_id, fp.short_summary(),
        )
        return cls(
            sauth_json=sauth_json,
            device_fingerprint=fp.to_dict(),
            chain_info=chain_info,
        )

    # ------------------------------------------------------------------
    # 公开属性
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """是否已建立连接并完成登录。"""
        return self._connected

    @property
    def spawned(self) -> bool:
        """是否已收到 StartGame (游戏已开始)。"""
        return self._spawned

    @property
    def host(self) -> str:
        """服务器主机名。"""
        return self._host

    @property
    def port(self) -> int:
        """服务器端口。"""
        return self._port

    @property
    def game_version(self) -> str:
        """协议版本字符串。"""
        return self._game_version

    def __repr__(self) -> str:
        status = "connected" if self._connected else "disconnected"
        return f"BedrockClient(host={self._host!r}, port={self._port}, status={status})"

    # ------------------------------------------------------------------
    # 连接 / 断开
    # ------------------------------------------------------------------

    async def connect(self, host: str, port: int = 19132) -> None:
        """连接到 Minecraft 服务器并完成完整登录流程。

        完整流程:

            1.  RakNet 连接 (UDP 握手)
            2.  等待 ServerToClientHandshake (服务器公钥 + 盐)
            3.  发送 ClientToServerHandshake (确认握手)
            4.  发送 Login 包 (JWT 登录链)
            5.  等待 PlayStatus = LOGIN_SUCCESS
            6.  等待 ResourcePacksInfo (服务器资源包信息)
            7.  发送 ResourcePackClientResponse (HAVE_ALL_PACKS)
            8.  等待 ResourcePackStack (资源包栈)
            9.  发送 ResourcePackClientResponse (COMPLETED)
            10. 等待 StartGame (游戏开始, 包含世界信息)
            11. 启动后台接收循环 (用于命令响应路由)

        Args:
            host: 服务器主机名或 IP。
            port: 服务器端口 (默认 19132)。

        Raises:
            ConnectionError: RakNet 握手失败。
            LoginError: 登录流程中的错误 (超时、解析失败等)。
            ServerRejectedError: 服务器拒绝登录 (版本不匹配、服务器满)。
            DisconnectError: 服务器在登录过程中断开连接。
        """
        self._host = host
        self._port = port
        logger.info("开始连接 Minecraft 服务器: %s:%d", host, port)

        # --- 1. RakNet 连接 ---
        await self._raknet.connect(host, port)
        self._connected = True
        logger.info("RakNet 连接已建立")

        try:
            # --- 2. 等待 ServerToClientHandshake ---
            await self._handle_server_handshake()

            # --- 3. 发送 ClientToServerHandshake ---
            await self._send_client_handshake()

            # --- 4. 发送 Login 包 ---
            await self._send_login()

            # --- 5. 等待 PlayStatus = LOGIN_SUCCESS ---
            await self._wait_for_play_status()

            # --- 6-9. 资源包协商 ---
            await self._handle_resource_packs()

            # --- 10. 等待 StartGame ---
            await self.wait_for_spawn()

            # --- 11. 启动后台接收循环 ---
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info("登录完成, 已进入游戏 (后台接收循环已启动)")
        except Exception:
            # 登录失败, 清理资源
            await self._cleanup()
            raise

    async def disconnect(self) -> None:
        """断开与服务器的连接。

        会取消后台接收任务, 断开 RakNet 连接, 并清理所有待处理的命令。
        """
        logger.info("正在断开连接...")
        await self._cleanup()
        logger.info("已断开连接")

    # ------------------------------------------------------------------
    # 数据包收发
    # ------------------------------------------------------------------

    async def send_packet(self, packet_id: int, data: bytes) -> None:
        """发送数据包到服务器。

        将游戏数据包封装在 Batch 批量包中, 经压缩后通过 RakNet 发送。

        Args:
            packet_id: 数据包 ID (见 :class:`PacketID`)。
            data: 数据包载荷 (不包含 packet_id)。

        Raises:
            BedrockError: 未连接到服务器。
        """
        if not self._connected:
            raise BedrockError("未连接到服务器")

        # 构建 Batch: [0xFE] + compress_batch(varuint32(id) + payload)
        game_packet = encode_varuint32(packet_id) + data
        batch = bytes([BATCH_PACKET_ID]) + compress_batch(game_packet)
        await self._raknet.send(batch, Reliability.RELIABLE_ORDERED)

    async def recv_packet(self) -> tuple[int, bytes]:
        """接收数据包。

        从后台接收队列中获取一个数据包。此方法应在 :meth:`connect` 完成
        后调用。

        Returns:
            ``(packet_id, data)`` 元组, data 为数据包载荷 (不包含 packet_id)。

        Raises:
            BedrockError: 连接已关闭。
        """
        # Bug 8.1 修复: 之前直接 await self._packet_queue.get() 会在连接
        # 关闭后 (若无人向队列推送 sentinel) 永久阻塞。现使用 wait_for 超时
        # 轮询, 每次超时后重新检查连接状态, 确保连接关闭时能及时返回。
        while True:
            if not self._connected and self._packet_queue.empty():
                raise BedrockError("连接已关闭, 无可读数据包")
            try:
                return await asyncio.wait_for(
                    self._packet_queue.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                if not self._connected and self._packet_queue.empty():
                    raise BedrockError("连接已关闭, 无可读数据包")
                continue

    # ------------------------------------------------------------------
    # 命令与聊天
    # ------------------------------------------------------------------

    async def send_command(self, command: str) -> str:
        """发送命令并等待响应。

        发送 CommandRequest 包, 并异步等待匹配的 CommandOutput 包。
        使用递增的 request_id 来匹配请求与响应。

        Args:
            command: 命令字符串 (如 ``"/list"`` 或 ``"say hello"``)。

        Returns:
            命令响应文本 (多条输出消息以换行拼接)。

        Raises:
            BedrockError: 未连接到服务器。
            CommandTimeoutError: 命令响应超时 (默认 30 秒)。
        """
        if not self._connected:
            raise BedrockError("未连接到服务器")

        request_id = self._command_request_id
        self._command_request_id += 1

        # 注册等待响应的 Future
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_commands[request_id] = future

        # 发送 CommandRequest
        payload = self._build_command_request(command, request_id)
        await self.send_packet(PacketID.COMMAND_REQUEST, payload)
        logger.debug("发送命令 (request_id=%d): %s", request_id, command)

        # 等待响应
        try:
            result = await asyncio.wait_for(future, timeout=COMMAND_TIMEOUT)
            return result
        except asyncio.TimeoutError:
            self._pending_commands.pop(request_id, None)
            raise CommandTimeoutError(
                f"命令响应超时 ({COMMAND_TIMEOUT}s): {command}"
            )

    async def send_chat(self, message: str) -> None:
        """发送聊天消息。

        发送 Text 包 (text_type = CHAT) 到服务器。

        Args:
            message: 聊天消息内容。

        Raises:
            BedrockError: 未连接到服务器。
        """
        if not self._connected:
            raise BedrockError("未连接到服务器")

        payload = self._build_text_packet(
            text_type=PacketID.TEXT_TYPE_CHAT,
            message=message,
        )
        await self.send_packet(PacketID.TEXT, payload)
        logger.debug("发送聊天消息: %s", message)

    # ------------------------------------------------------------------
    # 游戏阶段
    # ------------------------------------------------------------------

    async def wait_for_spawn(self) -> None:
        """等待游戏开始 (StartGame 包)。

        阻塞等待服务器发送 StartGame 包, 收到后标记游戏已开始。
        如果已经收到过 StartGame, 立即返回。

        同时从 StartGame 包中解析玩家运行时 ID (player_runtime_id),
        存入 ``self._player_runtime_id``, 供后续 PlayerAuthInput 使用。
        StartGame 包开头第一个字段为 varuint64 编码的 EntityRuntimeID
        (玩家运行时实体 ID)。
        """
        if self._spawned:
            return

        packet_id, data = await self._wait_for_packet(
            {PacketID.START_GAME},
            timeout=START_GAME_TIMEOUT,
        )
        self._spawned = True

        # 从 StartGame 包解析玩家运行时 ID
        # StartGame 包开头为 varuint64 编码的 EntityRuntimeID (玩家运行时实体 ID)。
        # 此 ID 用于后续 PlayerAuthInput 包中的 player_runtime_id 字段,
        # 服务器据此识别输入所属的玩家实体。
        runtime_id = self._parse_start_game_runtime_id(data)
        if runtime_id is not None:
            self._player_runtime_id = runtime_id
            logger.info(
                "收到 StartGame, 游戏已开始 (player_runtime_id=%d)",
                runtime_id,
            )
        else:
            # 解析失败时保留 0, 并记录警告 (PlayerAuthInput 仍可发送,
            # 但服务器可能无法正确识别玩家输入)
            logger.warning(
                "收到 StartGame, 但解析 player_runtime_id 失败, "
                "PlayerAuthInput 将使用 runtime_id=0"
            )

    def _parse_start_game_runtime_id(
        self, data: bytes
    ) -> Optional[int]:
        """从 StartGame 包载荷中解析玩家运行时 ID (EntityRuntimeID)。

        StartGame 包开头第一个字段为 varuint64 编码的 EntityRuntimeID
        (玩家运行时实体 ID, 无符号 64 位)。

        Args:
            data: StartGame 包载荷 (去除 packet_id 后的字节)。

        Returns:
            解析出的 runtime_id (int), 解析失败时返回 ``None``。
        """
        if not data:
            return None
        try:
            runtime_id, _ = decode_varuint64(data, 0)
        except (ValueError, IndexError) as exc:
            logger.debug("StartGame runtime_id 解码失败: %s", exc)
            return None
        # 运行时 ID 应为非负值 (decode_varuint64 已保证无符号)
        return int(runtime_id)

    async def send_player_auth_input(
        self,
        position: tuple[float, float, float] = (0.0, 0.0, 0.0),
        yaw: float = 0.0,
        pitch: float = 0.0,
    ) -> None:
        """发送 PlayerAuthInput 包 (玩家位置与输入)。

        在 StartGame 之后, 客户端需要定期发送 PlayerAuthInput 以维持
        连接并上报玩家状态。本方法发送一个最小的 PlayerAuthInput 包。

        Args:
            position: 玩家坐标 (x, y, z)。
            yaw: 水平旋转角 (度)。
            pitch: 俯仰角 (度)。

        Raises:
            BedrockError: 未连接到服务器。
        """
        if not self._connected:
            raise BedrockError("未连接到服务器")

        payload = self._build_player_auth_input(position, yaw, pitch)
        await self.send_packet(PacketID.PLAYER_AUTH_INPUT, payload)

    # ==================================================================
    # 私有方法 — 底层收发
    # ==================================================================

    async def _recv_raw_packet(self) -> tuple[int, bytes]:
        """从 RakNet 接收单个游戏数据包 (处理 Batch 解压)。

        RakNet 层交付的载荷以 0xFE (Batch ID) 开头, 后跟压缩的批量数据。
        解压后得到一个或多个游戏数据包的拼接, 每个包格式为::

            [Varuint32: packet_id] [载荷...]

        本方法假设每个 Batch 中只包含一个游戏数据包 (网易服务器的常见行为)。
        如果包含多个, 第一个包之后的剩余数据会被缓存供下次调用。

        Returns:
            ``(packet_id, payload)`` 元组。

        Raises:
            RuntimeError: RakNet 连接已关闭。
            ConnectionError: RakNet 持续返回空数据, 连接可能已断开。
        """
        # 连续空数据计数器: 防止 RakNet 持续返回空 bytes 时无限循环
        empty_count = 0
        while True:
            # 如果有缓存的 Batch 数据, 从中解析
            if self._batch_buffer:
                data = self._batch_buffer
                self._batch_buffer = b""

                if not data:
                    continue

                return self._handle_batch_packet(data)

            # 从 RakNet 接收
            raw = await self._raknet.recv()

            if not raw:
                # RakNet 返回空 bytes 可能是连接已断开但未通知,
                # 持续返回空会导致死循环, 此处限制连续空数据次数
                empty_count += 1
                if empty_count > 10:
                    raise ConnectionError("连续收到空数据，连接可能已断开")
                # Bug 8.3 修复: 之前直接 continue 会形成忙循环 (CPU 100%),
                # 因为 recv() 在连接断开时可能立即返回空。增加短暂 sleep
                # 让出事件循环, 避免忙等待。
                await asyncio.sleep(0.05)
                continue
            empty_count = 0

            # 检查是否为 Batch 包
            if raw[0] == BATCH_PACKET_ID:
                try:
                    decompressed = decompress_batch(raw[1:])
                except ValueError as exc:
                    logger.warning("Batch 解压失败: %s", exc)
                    continue
                self._batch_buffer = decompressed
            else:
                # 非 Batch 包 (直接是游戏包, 较少见)
                self._batch_buffer = raw

    def _handle_batch_packet(
        self, data: bytes
    ) -> tuple[int, bytes]:
        """处理 Batch 包 - 可能包含多个游戏数据包。

        Bedrock 协议的 Batch 包格式 (解压后)::

            [varuint packet_id][packet payload]
            [varuint packet_id][packet payload]
            ... (循环直到缓冲耗尽)

        注意: 每个子包没有显式长度字段, payload 的长度由包本身的
        结构决定。由于本客户端未实现所有包类型的完整解析器, 无法
        100% 可靠地拆分多个包。

        当前实现 (向后兼容):

            - 解析第一个包的 packet_id
            - 将 offset 之后的所有字节作为第一个包的 payload
            - 启发式检测是否可能含有多包, 若是则记录警告

        之前实现假设每个 Batch 只含一个包, 多包场景下 payload 会
        包含后续包的字节, 导致解析错误。本方法至少会在疑似多包时
        记录警告, 便于排查。

        Args:
            data: 解压后的 Batch 数据 (多个 ``[varuint id][payload]`` 拼接)。

        Returns:
            ``(packet_id, payload)`` — 第一个数据包的 ID 与载荷。
        """
        try:
            packet_id, offset = decode_varuint32(data, 0)
        except (ValueError, IndexError) as exc:
            logger.warning("Batch 数据解析 packet_id 失败: %s", exc)
            return 0, b""

        payload = data[offset:]

        # 多包检测 (启发式): Bedrock Batch 无长度前缀, 无法可靠拆分。
        # 此处扫描 payload, 若发现疑似后续 (已知 PacketID + 数据) 的
        # 结构则记录警告。此检测仅供诊断, 可能误报或漏报, 不改变返回值
        # (保持向后兼容)。
        self._warn_if_possible_multi_packet(packet_id, payload)

        return packet_id, payload

    def _warn_if_possible_multi_packet(
        self, first_packet_id: int, payload: bytes
    ) -> None:
        """启发式检测 Batch 中是否可能包含多个数据包, 若是则记录警告。

        Bedrock Batch 包没有长度前缀, 无法 100% 可靠拆分。本方法尝试
        在 payload 中查找疑似后续包的边界 (varuint 解码出已知且较罕见
        的 PacketID, 且其后还有数据字节), 若发现则记录警告。

        注意:
            - 此检测为启发式, 可能误报 (payload 中的合法数据被误认为
              packet_id) 或漏报 (后续包使用常见的小 ID)。
            - 为降低误报率, 仅匹配值 >= 0x40 的 PacketID (如 0x4D /
              0x4F / 0x58 / 0x90), 这些值在随机二进制数据中较少见。
            - 仅扫描 payload 前 128 字节, 避免性能开销。
            - 仅用于辅助诊断, 不改变 :meth:`_handle_batch_packet` 的返回值。
        """
        if not payload:
            return

        # 收集已知且较罕见的 PacketID (值 >= 0x40) 用于匹配,
        # 避免与 0x01-0x0F 这类在二进制数据中频繁出现的小值冲突。
        distinctive_ids = _DISTINCTIVE_PACKET_IDS
        if not distinctive_ids:
            return

        max_scan = min(len(payload), 128)
        pos = 0
        while pos < max_scan:
            try:
                candidate_id, next_pos = decode_varuint32(payload, pos)
            except (ValueError, IndexError):
                # 无法解码 varuint, 前进 1 字节继续尝试
                pos += 1
                continue

            # 需要满足: 解码值是已知且较罕见的 PacketID,
            # 且其后还有数据字节 (至少 1 字节), 才认为可能是后续包
            if (
                candidate_id in distinctive_ids
                and next_pos < len(payload)
            ):
                logger.warning(
                    "Batch 疑似包含多个数据包: first_packet_id=0x%02X, "
                    "在 payload 偏移 %d 发现疑似 packet_id=0x%02X。"
                    "当前实现仅返回第一个包, 后续包数据可能被混入 payload "
                    "导致解析错误 (Bedrock Batch 无长度前缀, 无法可靠拆分)。",
                    first_packet_id,
                    pos,
                    candidate_id,
                )
                return

            # 前进 1 字节继续扫描 (varuint 可能跨字节, 逐字节扫描以覆盖所有可能)
            pos += 1

    async def _wait_for_packet(
        self,
        expected_ids: set[int],
        timeout: float = 30.0,
    ) -> tuple[int, bytes]:
        """等待特定类型的数据包。

        在登录流程中使用, 直接调用 :meth:`_recv_raw_packet` 读取数据包。
        如果收到非预期类型的包, 会记录日志并继续等待 (但 Disconnect 包
        会立即抛出异常)。

        Args:
            expected_ids: 期望的数据包 ID 集合。
            timeout: 超时时间 (秒)。

        Returns:
            ``(packet_id, payload)`` 元组。

        Raises:
            TimeoutError: 等待超时。
            DisconnectError: 收到 Disconnect 包。
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                expected_hex = ", ".join(
                    f"0x{i:02X}" for i in sorted(expected_ids)
                )
                raise TimeoutError(f"等待数据包 [{expected_hex}] 超时")

            try:
                packet_id, data = await asyncio.wait_for(
                    self._recv_raw_packet(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                expected_hex = ", ".join(
                    f"0x{i:02X}" for i in sorted(expected_ids)
                )
                raise TimeoutError(f"等待数据包 [{expected_hex}] 超时")

            if packet_id in expected_ids:
                return packet_id, data

            # 处理意外收到的包
            if packet_id == PacketID.DISCONNECT:
                reason, _filtered = self._parse_disconnect(data)
                raise DisconnectError(f"服务器断开连接: {reason}")

            logger.debug(
                "登录流程中忽略意外数据包: 0x%02X (期望: %s)",
                packet_id,
                [f"0x{i:02X}" for i in sorted(expected_ids)],
            )

    # ==================================================================
    # 私有方法 — 登录流程各阶段
    # ==================================================================

    async def _handle_server_handshake(self) -> None:
        """接收并处理 ServerToClientHandshake。

        ServerToClientHandshake 包含服务器公钥和盐, 标准协议用于 ECDH
        加密协商。网易服务器可能使用不同的验证机制 (MCPCheckChallenges),
        当前版本暂不实现加密, 仅解析并记录服务器公钥信息。

        Raises:
            TimeoutError: 等待超时。
        """
        logger.debug("等待 ServerToClientHandshake...")
        packet_id, data = await self._wait_for_packet(
            {PacketID.SERVER_TO_CLIENT_HANDSHAKE},
            timeout=HANDSHAKE_TIMEOUT,
        )

        # 解析服务器公钥和盐 (网易可能不使用标准加密)
        try:
            offset = 0
            server_pubkey, offset = decode_string(data, offset)
            salt, offset = decode_string(data, offset)
            logger.debug(
                "收到 ServerToClientHandshake: pubkey=%d 字节, salt=%d 字节",
                len(server_pubkey),
                len(salt),
            )
        except (ValueError, IndexError) as exc:
            logger.warning(
                "ServerToClientHandshake 解析失败 (忽略, 继续登录): %s", exc
            )

        logger.info("已收到服务器握手")

    async def _send_client_handshake(self) -> None:
        """发送 ClientToServerHandshake (空 payload)。

        网易服务器的 ClientToServerHandshake 不需要加密载荷,
        直接发送空 payload 确认握手即可。
        """
        await self.send_packet(PacketID.CLIENT_TO_SERVER_HANDSHAKE, b"")
        logger.debug("发送 ClientToServerHandshake (空 payload)")

    async def _send_login(self) -> None:
        """发送 Login 包 (JWT 登录链)。

        Login 包结构::

            [Varuint32: packet_id=0x01]          # 由 send_packet 添加
            [String: protocol_version]            # 如 "1.21.90.0" (字符串)
            [Uint32LE: chain_json_length]         # 由 build_login_chain 生成
            [String: chain_json]                  # {"chain": ["jwt1", ...]}
            [Uint32LE: client_data_jwt_length]    # 由 build_login_chain 生成
            [String: client_data_jwt]             # 第三段 JWT

        其中 chain_json 和 client_data_jwt 部分由
        :func:`build_login_chain` 直接生成。
        """
        server_address = f"{self._host}:{self._port}"
        logger.debug("构建 JWT 登录链 (server_address=%s)...", server_address)

        # 解析 chain_info (认证服务器返回的 JWT 列表)
        existing_chain: Optional[list[str]] = None
        if self._chain_info:
            try:
                chain_list = json.loads(self._chain_info)
                if isinstance(chain_list, list) and chain_list:
                    existing_chain = [str(j) for j in chain_list]
                    logger.debug("使用在线模式 (%d 个 JWT)", len(existing_chain))
                else:
                    logger.warning("chain_info 不是有效的 JWT 列表, 回退到离线模式")
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning("chain_info 解析失败 (%s), 回退到离线模式", e)
        else:
            logger.debug("无 chain_info, 使用离线自签名模式")

        connection_request = build_login_chain(
            sauth_json=self._sauth_json,
            device_fingerprint=self._device_fingerprint,
            server_address=server_address,
            game_version=self._game_version,
            existing_chain=existing_chain,
        )

        # Login 载荷 = protocol_version(字符串) + connection_request
        login_payload = encode_string(self._game_version) + connection_request
        await self.send_packet(PacketID.LOGIN, login_payload)
        logger.info("发送 Login 包 (protocol_version=%s)", self._game_version)

    async def _wait_for_play_status(self) -> None:
        """等待 PlayStatus 包并检查登录状态。

        PlayStatus 状态码:
            - 0 = LOGIN_SUCCESS (登录成功)
            - 1 = CLIENT_VERSION_MISMATCH (版本不匹配)
            - 2 = SERVER_FULL (服务器已满)

        Raises:
            TimeoutError: 等待超时。
            ServerRejectedError: 服务器拒绝登录。
            LoginError: 未知状态码。
        """
        logger.debug("等待 PlayStatus...")
        packet_id, data = await self._wait_for_packet(
            {PacketID.PLAY_STATUS},
            timeout=PLAY_STATUS_TIMEOUT,
        )

        if len(data) < 4:
            raise LoginError(f"PlayStatus 数据过短 ({len(data)} 字节)")

        status = struct.unpack_from("<i", data, 0)[0]

        if status == PlayStatus.LOGIN_SUCCESS:
            logger.info("登录成功 (PlayStatus = LOGIN_SUCCESS)")
        elif status == PlayStatus.CLIENT_VERSION_MISMATCH:
            raise ServerRejectedError(
                f"客户端版本不匹配 (protocol_version={self._game_version})"
            )
        elif status == PlayStatus.SERVER_FULL:
            raise ServerRejectedError("服务器已满")
        else:
            raise LoginError(f"未知 PlayStatus: {status}")

    async def _handle_resource_packs(self) -> None:
        """处理资源包协商流程。

        完整流程:
            1. 等待 ResourcePacksInfo
            2. 发送 ResourcePackClientResponse (HAVE_ALL_PACKS, 空列表)
            3. 等待 ResourcePackStack
            4. 发送 ResourcePackClientResponse (COMPLETED, 空列表)

        资源包阶段直接回复 "已完成" (空资源包列表), 不下载任何资源包。

        Raises:
            TimeoutError: 等待超时。
        """
        # --- 1. 等待 ResourcePacksInfo ---
        logger.debug("等待 ResourcePacksInfo...")
        await self._wait_for_packet(
            {PacketID.RESOURCE_PACKS_INFO},
            timeout=RESOURCE_PACK_TIMEOUT,
        )
        logger.debug("收到 ResourcePacksInfo")

        # --- 2. 发送 HAVE_ALL_PACKS ---
        await self._send_resource_pack_response(
            ResourcePackResponseStatus.HAVE_ALL_PACKS
        )
        logger.debug("发送 ResourcePackClientResponse (HAVE_ALL_PACKS)")

        # --- 3. 等待 ResourcePackStack ---
        logger.debug("等待 ResourcePackStack...")
        await self._wait_for_packet(
            {PacketID.RESOURCE_PACK_STACK},
            timeout=RESOURCE_PACK_TIMEOUT,
        )
        logger.debug("收到 ResourcePackStack")

        # --- 4. 发送 COMPLETED ---
        await self._send_resource_pack_response(
            ResourcePackResponseStatus.COMPLETED
        )
        logger.debug("发送 ResourcePackClientResponse (COMPLETED)")
        logger.info("资源包协商完成")

    async def _send_resource_pack_response(self, status: int) -> None:
        """发送 ResourcePackClientResponse 包。

        包结构::

            [Varuint32: packet_id=0x08]   # 由 send_packet 添加
            [Byte: response_status]       # 状态码
            [Uint32LE: pack_count]        # 资源包数量 (0 = 空列表)

        Args:
            status: 响应状态码 (见 :class:`ResourcePackResponseStatus`)。
        """
        # [Byte: status] [Uint32LE: pack_count=0]
        payload = bytes([status]) + encode_uint32_le(0)
        await self.send_packet(PacketID.RESOURCE_PACK_CLIENT_RESPONSE, payload)

    # ==================================================================
    # 私有方法 — 包构建
    # ==================================================================

    def _build_text_packet(
        self,
        text_type: int,
        message: str,
        needs_translation: bool = False,
        parameters: Optional[list[str]] = None,
        xuid: str = "",
        platform_chat_id: str = "",
    ) -> bytes:
        """构建 Text 包载荷。

        Text 包结构::

            [Byte: text_type]              # 0=raw, 1=chat, ...
            [Bool: needs_translation]
            [Uint32LE: param_count]        # 参数数量
            [String[]: parameters]         # 参数列表
            [String: message]              # 消息内容
            [String: xuid]                 # XUID (可选, 空字符串)
            [String: platform_chat_id]     # 平台聊天 ID (可选, 空字符串)

        Args:
            text_type: 文本类型 (见 :class:`PacketID` TEXT_TYPE_* 常量)。
            message: 消息内容。
            needs_translation: 是否需要翻译。
            parameters: 翻译参数列表 (默认空)。
            xuid: 玩家 XUID (默认空字符串)。
            platform_chat_id: 平台聊天 ID (默认空字符串)。

        Returns:
            Text 包载荷字节串。
        """
        if parameters is None:
            parameters = []

        buf = bytearray()
        # [Byte: text_type]
        buf.append(text_type)
        # [Bool: needs_translation]
        buf.append(1 if needs_translation else 0)
        # [Uint32LE: param_count]
        buf += encode_uint32_le(len(parameters))
        # [String[]: parameters]
        for param in parameters:
            buf += encode_string(param)
        # [String: message]
        buf += encode_string(message)
        # [String: xuid]
        buf += encode_string(xuid)
        # [String: platform_chat_id]
        buf += encode_string(platform_chat_id)
        return bytes(buf)

    def _build_command_request(self, command: str, request_id: int) -> bytes:
        """构建 CommandRequest 包载荷。

        CommandRequest 包结构::

            [Varuint32: packet_id=0x4D]     # 由 send_packet 添加
            [Varuint32: command_origin_type] # 0=player, 4=dev_console
            [String: command]               # 命令字符串
            [Uint32LE: argument_count]      # 参数数量
            [String[]: arguments]           # 命令参数列表
            [Varint32: internal_origin]     # 内部来源标识
            [Int64LE: request_id]           # 请求 ID (用于匹配响应)

        Args:
            command: 命令字符串。
            request_id: 请求 ID (用于匹配 CommandOutput 响应)。

        Returns:
            CommandRequest 包载荷字节串。
        """
        buf = bytearray()
        # [Varuint32: command_origin_type]
        buf += encode_varuint32(CommandOriginType.PLAYER)
        # [String: command]
        buf += encode_string(command)
        # [Uint32LE: argument_count] + [String[]: arguments] (空列表)
        buf += encode_uint32_le(0)
        # [Varint32: internal_origin]
        buf += encode_varint32(0)
        # [Int64LE: request_id]
        buf += encode_int64_le(request_id)
        return bytes(buf)

    def _build_player_auth_input(
        self,
        position: tuple[float, float, float],
        yaw: float,
        pitch: float,
    ) -> bytes:
        """构建 PlayerAuthInput 包载荷 (最小版本)。

        PlayerAuthInput 用于上报玩家位置和输入状态。本方法构建一个
        最小的 PlayerAuthInput, 仅包含位置和朝向信息。

        包结构 (简化)::

            [Varuint64: player_runtime_id]   # 运行时 ID (由 StartGame 提供)
            [Varint32: tick]                 # 游戏刻
            [Float LE: x] [Float LE: y] [Float LE: z]  # 位置
            [Float LE: move_x] [Float LE: move_z]      # 移动向量
            [Float LE: yaw] [Float LE: head_yaw] [Float LE: pitch]  # 朝向
            [Varuint64: input_flags]         # 输入标志
            [Varuint32: input_mode]          # 输入模式
            [Varuint32: play_mode]           # 游戏模式
            [Varuint64: interaction_mode]    # 交互模式
            [Float LE: interact_pitch]       # 交互俯仰
            [Varuint32: tick_delta]          # 刻增量
            [Byte: inventory_action_type]    # 物品栏动作类型

        Args:
            position: 玩家坐标 (x, y, z)。
            yaw: 水平旋转角 (度)。
            pitch: 俯仰角 (度)。

        Returns:
            PlayerAuthInput 包载荷字节串。
        """
        buf = bytearray()
        x, y, z = position

        # 递增游戏刻计数器
        self._tick += 1

        # [Varuint64: player_runtime_id] — 由 StartGame 包解析获得
        # (wait_for_spawn 中提取并存储到 self._player_runtime_id)。
        # 服务器据此识别输入所属的玩家实体。StartGame 之前为 0。
        # 使用 encode_varuint64 编码以匹配协议字段类型 (Varuint64)。
        buf += encode_varuint64(self._player_runtime_id)
        # [Varint32: tick] — 游戏刻 (相对计数器, 非时间戳)
        buf += encode_varint32(self._tick)
        # [Float LE x3: position]
        buf += struct.pack("<fff", x, y, z)
        # [Float LE x2: move vector]
        buf += struct.pack("<ff", 0.0, 0.0)
        # [Float LE x3: yaw, head_yaw, pitch]
        buf += struct.pack("<fff", yaw, yaw, pitch)
        # [Varuint64: input_flags] — 无输入
        buf += encode_varuint64(0)
        # [Varuint32: input_mode]
        buf += encode_varuint32(InputMode.MOUSE)
        # [Varuint32: play_mode]
        buf += encode_varuint32(PlayMode.NORMAL)
        # [Varuint64: interaction_mode]
        buf += encode_varuint64(0)
        # [Float LE: interact_pitch]
        buf += struct.pack("<f", 0.0)
        # [Varuint32: tick_delta]
        buf += encode_varuint32(0)
        # [Byte: inventory_action_type]
        buf.append(0)
        return bytes(buf)

    # ==================================================================
    # 私有方法 — 包解析
    # ==================================================================

    def _parse_command_output(self, data: bytes) -> tuple[int, str]:
        """解析 CommandOutput 包, 返回 (request_id, output_text)。

        CommandOutput 包结构 (网易简化格式, 对应 CommandRequest)::

            [Varuint32: packet_id=0x4F]      # 已由 _recv_raw_packet 消费
            [Varuint32: command_origin_type] # 命令来源类型
            [Int64LE: request_id]            # 请求 ID (匹配 CommandRequest)
            [Byte: success]                  # 是否成功
            [Uint32LE: message_count]        # 输出消息数量
            [String[]: messages]             # 输出消息列表
            [String: data]                   # 附加数据 (可选)

        Args:
            data: CommandOutput 包载荷 (不含 packet_id)。

        Returns:
            ``(request_id, output_text)`` 元组。如果解析失败,
            request_id 为 -1, output_text 为空字符串。
        """
        try:
            offset = 0
            # [Varuint32: command_origin_type]
            _origin_type, offset = decode_varuint32(data, offset)
            # [Int64LE: request_id]
            if offset + 8 > len(data):
                logger.warning("CommandOutput: request_id 字段不足 8 字节")
                return -1, ""
            request_id = struct.unpack_from("<q", data, offset)[0]
            offset += 8
            # [Byte: success]
            if offset >= len(data):
                return request_id, ""
            _success = data[offset]
            offset += 1
            # [Uint32LE: message_count]
            if offset + 4 > len(data):
                return request_id, ""
            msg_count = struct.unpack_from("<I", data, offset)[0]
            offset += 4
            # [String[]: messages]
            messages: list[str] = []
            for _ in range(msg_count):
                if offset >= len(data):
                    break
                msg, offset = decode_string(data, offset)
                messages.append(msg)
            # 拼接所有消息
            output = "\n".join(messages)
            logger.debug(
                "解析 CommandOutput: request_id=%d, success=%s, messages=%d",
                request_id,
                bool(_success),
                len(messages),
            )
            return request_id, output
        except (ValueError, IndexError, struct.error) as exc:
            logger.warning("CommandOutput 解析失败: %s", exc)
            return -1, ""

    def _parse_disconnect(self, data: bytes) -> tuple[str, str]:
        """解析 Disconnect 包, 返回 (reason, filtered_message)。

        Disconnect 包结构::

            [Bool: skip_message]        # 是否跳过消息 (标准格式)
            [String: reason]            # 断开原因
            [String: filtered_message]  # 过滤后消息 (可选, 标准格式)

        或简化格式 (Bedrock / 网易服务器常用)::

            [String: reason]            # 直接是原因字符串

        此前通过 ``data[0] in (0, 1)`` 启发式判断是否包含 skip_message
        字段, 但对短字符串会失败:

            - 空字符串 (长度 varuint = 0x00) 会被误判为 ``skip_message=0``
            - 长度 1 的字符串 (varuint = 0x01) 会被误判为 ``skip_message=1``

        修复策略: 优先按标准格式 ``[Bool][String][String]`` 解析, 失败后
        回退到简化格式 ``[String]``。解析时对字符串做边界检查, 防止
        :func:`decode_string` 在数据不足时静默截断导致标准格式被误判成功。

        Args:
            data: Disconnect 包载荷 (不含 packet_id)。

        Returns:
            ``(reason, filtered_message)`` 元组。若解析失败, reason 为原始
            字节的 UTF-8 替换解码结果, filtered_message 为空字符串;
            数据为空时返回 ``("未知原因", "")``。
        """
        if not data:
            return "未知原因", ""

        def _decode_str_checked(off: int) -> tuple[str, int]:
            """带边界检查的字符串解码, 防止 decode_string 静默截断。

            失败时抛出 ``ValueError`` / ``IndexError`` /
            ``UnicodeDecodeError`` 供上层捕获以触发格式回退。
            """
            length, new_off = decode_varuint32(data, off)
            if new_off + length > len(data):
                raise ValueError(
                    f"字符串长度超出数据范围: offset={off}, length={length}, "
                    f"data_len={len(data)}"
                )
            raw = data[new_off : new_off + length]
            return raw.decode("utf-8"), new_off + length

        # 先尝试标准格式: [Bool skip_message][String reason][String filtered]
        try:
            offset = 0
            # Bedrock Bool: 单字节, 0=False, 1=True (其他值视为非法)
            skip_byte = data[offset]
            if skip_byte not in (0, 1):
                raise ValueError(f"非法 skip_message 值: 0x{skip_byte:02x}")
            offset += 1
            reason, offset = _decode_str_checked(offset)
            filtered = ""
            if offset < len(data):
                filtered, offset = _decode_str_checked(offset)
            return reason, filtered
        except (ValueError, IndexError, UnicodeDecodeError) as exc:
            logger.debug("Disconnect 标准格式解析失败, 回退到简化格式: %s", exc)

        # 回退到简化格式: [String reason]
        try:
            reason, _ = _decode_str_checked(0)
            return reason, ""
        except (ValueError, IndexError, UnicodeDecodeError):
            pass

        # 最终回退: 原始字节 UTF-8 替换解码
        return data.decode("utf-8", errors="replace"), ""

    # ==================================================================
    # 私有方法 — 后台接收循环
    # ==================================================================

    async def _recv_loop(self) -> None:
        """后台接收循环。

        在 :meth:`connect` 完成后启动, 负责:

            1. 持续接收服务器数据包
            2. 将 CommandOutput 包路由到等待中的命令 Future
            3. 检测 Disconnect 包并触发清理
            4. 将其他包放入 :attr:`_packet_queue` 供 :meth:`recv_packet` 消费

        当连接关闭或发生错误时, 循环退出。
        """
        logger.debug("后台接收循环已启动")
        while self._connected:
            try:
                packet_id, data = await self._recv_raw_packet()
            except RuntimeError:
                # RakNet 连接已关闭
                logger.debug("接收循环: RakNet 连接已关闭")
                break
            except Exception as exc:
                if self._connected:
                    logger.error("接收数据包失败: %s", exc)
                break

            # --- 路由 CommandOutput ---
            if packet_id == PacketID.COMMAND_OUTPUT:
                request_id, output = self._parse_command_output(data)
                if request_id in self._pending_commands:
                    future = self._pending_commands.pop(request_id)
                    if not future.done():
                        future.set_result(output)
                    logger.debug(
                        "CommandOutput 已路由到 request_id=%d", request_id
                    )
                    continue
                else:
                    logger.debug(
                        "收到未匹配的 CommandOutput (request_id=%d), 放入队列",
                        request_id,
                    )

            # --- 检测 Disconnect ---
            if packet_id == PacketID.DISCONNECT:
                reason, _filtered = self._parse_disconnect(data)
                logger.info("服务器主动断开连接: %s", reason)
                # 通知所有等待中的命令
                for fut in self._pending_commands.values():
                    if not fut.done():
                        fut.set_exception(DisconnectError(reason))
                self._pending_commands.clear()
                await self._cleanup()
                return

            # --- 放入队列 ---
            try:
                self._packet_queue.put_nowait((packet_id, data))
            except asyncio.QueueFull:
                logger.warning(
                    "数据包队列已满, 丢弃数据包 0x%02X", packet_id
                )

        logger.debug("后台接收循环已退出")

    # ==================================================================
    # 私有方法 — 清理
    # ==================================================================

    async def _cleanup(self) -> None:
        """清理所有资源。

            1. 标记为已断开
            2. 取消后台接收任务
            3. 断开 RakNet 连接
            4. 通知所有等待中的命令 (抛出异常)
            5. 清空数据包队列和 Batch 缓冲
        """
        self._connected = False
        self._spawned = False
        # 重置玩家运行时 ID (下次连接时由新的 StartGame 重新提供)
        self._player_runtime_id = 0

        # 取消后台接收任务
        if self._recv_task is not None and not self._recv_task.done():
            # Bug 8.2 修复: 若 _cleanup 是从 _recv_loop 内部调用的 (如收到
            # Disconnect 包), self._recv_task 就是当前任务。cancel() 自己再
            # await 自己会导致 CancelledError 异常传播, 行为难以预测。
            # 现检查当前任务是否为 _recv_task, 若是则跳过取消 (任务会自然退出)。
            current_task = asyncio.current_task()
            if self._recv_task is not current_task:
                self._recv_task.cancel()
                try:
                    await self._recv_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.debug("接收任务退出异常: %s", exc)
            self._recv_task = None

        # 断开 RakNet 连接
        try:
            await self._raknet.disconnect()
        except Exception as exc:
            logger.debug("RakNet 断开异常 (忽略): %s", exc)

        # 通知所有等待中的命令
        for fut in self._pending_commands.values():
            if not fut.done():
                fut.set_exception(BedrockError("连接已关闭"))
        self._pending_commands.clear()

        # 清空数据包队列和 Batch 缓冲
        while not self._packet_queue.empty():
            try:
                self._packet_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._batch_buffer = b""


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    # 常量
    "BATCH_PACKET_ID",
    "COMMAND_TIMEOUT",
    # 常量类
    "PacketID",
    "PlayStatus",
    "ResourcePackResponseStatus",
    "CommandOriginType",
    "InputMode",
    "PlayMode",
    # 异常
    "BedrockError",
    "LoginError",
    "ServerRejectedError",
    "DisconnectError",
    "CommandTimeoutError",
    # 辅助函数
    "encode_string",
    "decode_string",
    "encode_uint32_le",
    "encode_int64_le",
    # 主类
    "BedrockClient",
]
