"""魔法指令系统 - 网易版 AI 命令通道。

逆向来源: Retalcer导入器 __init__.py:257-263

网易版 Minecraft 特有的 "魔法指令" (AI Command) 通道,
通过 PyRpc 包发送 ModEventC2S 事件, 绕过 OP 权限检查执行命令。

与标准聊天框命令的区别:
    - 魔法指令: 通过 PyRpc 包, 绕过 OP, 可执行 /setblock /replaceitem 等
    - 控制台命令: 通过 Text 包/聊天框, 需要 OP, 可执行 /tp /fill /execute 等

混合路由策略:
    /tp /fill /tickingarea /execute → 控制台命令 (sendwocmd)
    /setblock /replaceitem /titleraw 等 → 魔法指令 (sendaicmd)

命令限速:
    - 方块放置: 20/秒 (默认, 合理速度不会触发反作弊)
    - 命令方块加载: 10/秒
    - 容器物品: 5/秒 (给服务器足够时间处理)
    - 组间等待: 1秒
    - NBT 操作延迟: 0.5秒 (结构保存/加载之间)

.. important::

    **用户反馈 (NBT 操作不限时)**:
        "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个 NBT 制作完,
        不管多长时间, 然后第一个制作完开始制作第二个"

    **NBT 操作不限时** (用户反馈优化, v2.2):
        - NBT 操作 (structure save/load, NBT 物品制作) **不使用固定超时**,
          而是通过 :class:`NBTBlockPlacer` 的 ``_wait_for_nbt_completion``
          等待服务器实际确认 (无超时, 顺序执行)。
        - 旧的 :meth:`CommandRateLimiter.wait_nbt` (固定 ``nbt_delay``) 已
          **弃用**, 保留仅供向后兼容。
        - **普通方块操作保持速率限制** (block_speed / command_speed /
          container_speed), 防止触发反作弊。

    **网易 3.8 限制**:
        replaceitem 阉割后只能放耐久、特殊值、数量、NBT 标签
        (如 ``minecraft:keep_on_death`` 死亡不掉落、
        ``minecraft:item_lock`` 物品锁定), 不能放附魔、自定义名字。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("pocketterm.protocol.magic_command")

# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 需要走控制台命令(聊天框)的命令前缀
#: 这些命令在魔法指令中无效, 必须走 OP 命令
CONSOLE_ONLY_PREFIXES: tuple[str, ...] = (
    "tp ", "/tp ",
    "fill ", "/fill ",
    "tickingarea ", "/tickingarea ",
    "execute ", "/execute ",
)

#: 默认命令发送速度 (命令/秒)
#: 合理的默认值, 不会触发反作弊
DEFAULT_BLOCK_SPEED: int = 20        # 20方块/秒 (0.05s/方块)
DEFAULT_COMMAND_SPEED: int = 10     # 10命令方块/秒 (0.1s/命令)
DEFAULT_CONTAINER_SPEED: int = 5    # 5物品/秒 (0.2s/物品) - 给服务器足够时间处理
DEFAULT_GROUP_WAIT: float = 1.0
DEFAULT_NBT_DELAY: float = 0.5     # NBT操作(结构保存/加载)之间的延迟

#: PyRpc 数据包 ID
PACKET_ID_PY_RPC: int = 0x4E  # 78


@dataclass
class CommandRateLimiter:
    """命令限速器 - 控制命令发送频率。

    防止发送过快被反作弊系统检测并封禁。

    .. important::

        **用户反馈 (NBT 操作不限时)**:
            "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个 NBT 制作完,
            不管多长时间, 然后第一个制作完开始制作第二个"

        **NBT 操作速率限制策略 (v2.2, 用户反馈优化)**:
            - **NBT 操作** (structure save/load, NBT 物品制作): **不限时**
              -- 不使用 ``nbt_delay`` 固定延迟, 而是通过
              :meth:`NBTBlockPlacer._wait_for_nbt_completion` 等待服务器
              实际确认 (无超时, 顺序执行)。
            - **普通方块操作**: 保持速率限制 (``block_speed`` /
              ``command_speed`` / ``container_speed``), 防止触发反作弊。
            - ``nbt_delay`` 字段保留仅供向后兼容, 不再被 NBT 放置流程使用。

    **网易 3.8 限制**:
        replaceitem 阉割后只能放耐久、特殊值、数量、NBT 标签
        (如 ``minecraft:keep_on_death`` 死亡不掉落、
        ``minecraft:item_lock`` 物品锁定), 不能放附魔、自定义名字。
        普通物品 (无附魔/无自定义名字) 可用 replaceitem 快速放入;
        NBT 物品 (有附魔/自定义名字/复杂 NBT) 需走平台模式。
    """

    #: 方块放置速度 (命令/秒)
    #: 普通方块操作保持速率限制, 防止触发反作弊
    block_speed: int = DEFAULT_BLOCK_SPEED
    #: 命令方块加载速度 (命令/秒)
    #: 普通命令方块操作保持速率限制
    command_speed: int = DEFAULT_COMMAND_SPEED
    #: 容器物品速度 (命令/秒)
    #: 普通物品 replaceitem 保持速率限制 (给服务器足够时间处理)
    container_speed: int = DEFAULT_CONTAINER_SPEED
    #: 组间等待时间 (秒)
    group_wait: float = DEFAULT_GROUP_WAIT
    #: NBT操作(结构保存/加载)之间的延迟 (秒)
    #:
    #: .. deprecated:: v2.2
    #:     用户反馈: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
    #:     NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"
    #:     NBT 操作现已改为**不限时**, 通过 NBTBlockPlacer 的
    #:     ``_wait_for_nbt_completion`` 等待服务器实际确认 (无超时)。
    #:     本字段保留仅供向后兼容, NBT 放置流程不再使用。
    nbt_delay: float = DEFAULT_NBT_DELAY

    def __post_init__(self) -> None:
        self._block_interval = 1.0 / self.block_speed if self.block_speed > 0 else 0
        self._command_interval = 1.0 / self.command_speed if self.command_speed > 0 else 0
        self._container_interval = 1.0 / self.container_speed if self.container_speed > 0 else 0
        self._last_block_time: float = 0.0
        self._last_command_time: float = 0.0
        self._last_container_time: float = 0.0
        self._last_nbt_time: float = 0.0

    async def wait_block(self) -> None:
        """等待方块命令的限速间隔。"""
        now = time.monotonic()
        elapsed = now - self._last_block_time
        if elapsed < self._block_interval:
            await asyncio.sleep(self._block_interval - elapsed)
        self._last_block_time = time.monotonic()

    async def wait_command(self) -> None:
        """等待命令方块的限速间隔。"""
        now = time.monotonic()
        elapsed = now - self._last_command_time
        if elapsed < self._command_interval:
            await asyncio.sleep(self._command_interval - elapsed)
        self._last_command_time = time.monotonic()

    async def wait_container(self) -> None:
        """等待容器物品的限速间隔。"""
        now = time.monotonic()
        elapsed = now - self._last_container_time
        if elapsed < self._container_interval:
            await asyncio.sleep(self._container_interval - elapsed)
        self._last_container_time = time.monotonic()

    async def wait_group(self) -> None:
        """等待组间等待时间。"""
        await asyncio.sleep(self.group_wait)

    async def wait_nbt(self) -> None:
        """等待NBT操作(结构保存/加载)的延迟。

        .. deprecated:: v2.2
            **用户反馈**: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
            NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"

            NBT 操作现已改为**不限时**, 不再使用固定的 ``nbt_delay``。
            请使用 :meth:`NBTBlockPlacer._wait_for_nbt_completion` 等待
            服务器实际确认 (无超时, 顺序执行)。

            本方法保留仅供向后兼容, NBT 放置流程不再调用本方法。
        """
        import warnings
        warnings.warn(
            "CommandRateLimiter.wait_nbt() 已弃用 (v2.2): "
            "NBT 操作现通过 NBTBlockPlacer._wait_for_nbt_completion() "
            "等待服务器确认 (无超时, 用户反馈: NBT 制作无时长限制)。"
            "请使用 NBTBlockPlacer 的完成事件机制。",
            DeprecationWarning,
            stacklevel=2,
        )
        now = time.monotonic()
        elapsed = now - self._last_nbt_time
        if elapsed < self.nbt_delay:
            await asyncio.sleep(self.nbt_delay - elapsed)
        self._last_nbt_time = time.monotonic()

    def update_speeds(
        self,
        block_speed: Optional[int] = None,
        command_speed: Optional[int] = None,
        container_speed: Optional[int] = None,
        group_wait: Optional[float] = None,
        nbt_delay: Optional[float] = None,
    ) -> None:
        """动态更新限速参数。

        Args:
            block_speed: 方块放置速度 (命令/秒)
            command_speed: 命令方块加载速度 (命令/秒)
            container_speed: 容器物品速度 (命令/秒)
            group_wait: 组间等待时间 (秒)
            nbt_delay: NBT操作延迟 (秒)
        """
        if block_speed is not None:
            self.block_speed = block_speed
            self._block_interval = 1.0 / block_speed if block_speed > 0 else 0
        if command_speed is not None:
            self.command_speed = command_speed
            self._command_interval = 1.0 / command_speed if command_speed > 0 else 0
        if container_speed is not None:
            self.container_speed = container_speed
            self._container_interval = 1.0 / container_speed if container_speed > 0 else 0
        if group_wait is not None:
            self.group_wait = group_wait
        if nbt_delay is not None:
            self.nbt_delay = nbt_delay


#: 速度预设
SPEED_PRESETS: dict[str, dict[str, Any]] = {
    "slow": {
        "block_speed": 10,       # 10方块/秒
        "command_speed": 5,      # 5命令/秒
        "container_speed": 3,    # 3物品/秒
        "group_wait": 2.0,
        "nbt_delay": 1.0,
    },
    "medium": {
        "block_speed": 20,       # 20方块/秒
        "command_speed": 10,     # 10命令/秒
        "container_speed": 5,    # 5物品/秒
        "group_wait": 1.0,
        "nbt_delay": 0.5,
    },
    "fast": {
        "block_speed": 50,       # 50方块/秒
        "command_speed": 20,     # 20命令/秒
        "container_speed": 10,   # 10物品/秒
        "group_wait": 0.5,
        "nbt_delay": 0.3,
    },
    "turbo": {
        "block_speed": 100,      # 100方块/秒 (最高,有风险)
        "command_speed": 50,     # 50命令/秒
        "container_speed": 20,   # 20物品/秒
        "group_wait": 0.3,
        "nbt_delay": 0.1,
    },
}


def apply_speed_preset(limiter: CommandRateLimiter, preset: str) -> None:
    """应用速度预设到限速器。

    Args:
        limiter: 要更新的限速器实例
        preset: 预设名称 (slow/medium/fast/turbo), 不存在则回退到 medium
    """
    if preset not in SPEED_PRESETS:
        preset = "medium"
    config = SPEED_PRESETS[preset]
    limiter.update_speeds(**config)


# ----------------------------------------------------------------------
# 魔法指令发送器
# ----------------------------------------------------------------------


class MagicCommandSender:
    """魔法指令发送器 - 通过 PyRpc 发送 AI 命令。

    逆向来源: Retalcer导入器 __init__.py:257-263

    用法::

        sender = MagicCommandSender(client)
        await sender.send_ai_command("setblock 0 64 0 stone")  # 魔法指令
        await sender.send_wo_command("tp @s 0 64 0")  # 控制台命令
        await sender.send_any_command("setblock 0 64 0 stone")  # 自动路由
    """

    def __init__(self, client: Any, rate_limiter: Optional[CommandRateLimiter] = None):
        """
        Args:
            client: BedrockClient 实例 (需要 send_packet 和 send_command 方法)
            rate_limiter: 命令限速器, 默认使用默认配置
        """
        self.client = client
        self.rate_limiter = rate_limiter or CommandRateLimiter()
        # 机器人运行时ID (从服务器获取)
        self._bot_runtime_id: int = 0
        self._command_count: int = 0

    def set_bot_runtime_id(self, runtime_id: int) -> None:
        """设置机器人运行时ID (从 PlayerList 包获取)。"""
        self._bot_runtime_id = runtime_id
        logger.info(f"机器人运行时ID已设置: {runtime_id}")

    def _build_ai_command_packet(self, command: str) -> dict:
        """构建 AI 命令的 PyRpc 数据包。

        逆向来源: Retalcer __init__.py:257-263

        结构::

            {
                "Value": [
                    "ModEventC2S",
                    ["Minecraft", "aiCommand", "ExecuteCommandEvent",
                     {"playerId": "<runtime_id>", "cmd": "<command>", "uuid": "<uuid>"}],
                    None
                ],
                "OperationType": 0
            }
        """
        return {
            "Value": [
                "ModEventC2S",
                [
                    "Minecraft",
                    "aiCommand",
                    "ExecuteCommandEvent",
                    {
                        "playerId": str(self._bot_runtime_id),
                        "cmd": command,
                        "uuid": str(uuid.uuid4()),
                    },
                ],
                None,
            ],
            "OperationType": 0,
        }

    async def send_ai_command(self, command: str) -> Optional[str]:
        """发送魔法指令 (AI Command)。

        通过 PyRpc 包发送, 绕过 OP 权限检查。
        适用于: /setblock, /replaceitem, /titleraw, /say 等

        Args:
            command: 命令文本 (可带或不带 /)

        Returns:
            命令响应文本 (如果有), 失败返回 None
        """
        command = command.lstrip("/")
        await self.rate_limiter.wait_block()

        try:
            # 构建 PyRpc 包
            packet = self._build_ai_command_packet(command)

            # 发送 PyRpc 包
            # 需要将 dict 序列化为 NBT 网络格式
            from .nbt import marshal_network

            nbt_data = marshal_network(packet)
            await self.client.send_packet(PACKET_ID_PY_RPC, nbt_data)

            self._command_count += 1
            logger.debug(f"魔法指令已发送: /{command}")
            return None  # AI 命令通常没有直接响应

        except Exception as e:
            logger.error(f"魔法指令发送失败: /{command} - {e}")
            return None

    async def send_wo_command(self, command: str) -> Optional[str]:
        """发送控制台命令 (World Command)。

        通过 Text 包发送, 需要 OP 权限。
        适用于: /tp, /fill, /tickingarea, /execute 等

        Args:
            command: 命令文本 (可带或不带 /)

        Returns:
            命令响应文本
        """
        command = command.lstrip("/")
        await self.rate_limiter.wait_block()

        try:
            response = await self.client.send_command(command)
            self._command_count += 1
            logger.debug(f"控制台命令已发送: /{command} -> {response[:50] if response else '无响应'}")
            return response
        except Exception as e:
            logger.error(f"控制台命令发送失败: /{command} - {e}")
            return None

    async def send_any_command(self, command: str) -> Optional[str]:
        """自动路由命令 - 根据命令前缀选择发送方式。

        路由规则 (逆向自 Retalcer chunk_painter.py:109-117):
            - /tp /fill /tickingarea /execute → 控制台命令 (send_wo_command)
            - 其他 → 魔法指令 (send_ai_command)

        Args:
            command: 命令文本 (可带或不带 /)

        Returns:
            命令响应文本 (如果有)
        """
        command_lower = command.strip().lower()

        # 检查是否需要走控制台命令
        for prefix in CONSOLE_ONLY_PREFIXES:
            if command_lower.startswith(prefix):
                return await self.send_wo_command(command)

        # 默认走魔法指令
        return await self.send_ai_command(command)

    async def send_command_with_type(
        self, command: str, cmd_type: str = "auto"
    ) -> Optional[str]:
        """发送指定类型的命令。

        Args:
            command: 命令文本
            cmd_type: 命令类型 ("auto"/"ai"/"wo")

        Returns:
            命令响应文本
        """
        if cmd_type == "ai":
            return await self.send_ai_command(command)
        elif cmd_type == "wo":
            return await self.send_wo_command(command)
        else:
            return await self.send_any_command(command)

    @property
    def command_count(self) -> int:
        """已发送的命令总数。"""
        return self._command_count


# ----------------------------------------------------------------------
# PyRpc 数据包编码
# ----------------------------------------------------------------------


def build_py_rpc_packet(event_name: str, args: list, operation_type: int = 0) -> bytes:
    """构建 PyRpc 数据包。

    PyRpc 是网易版 Minecraft 特有的 Python RPC 通信系统,
    用于 MOD 事件、AI 命令、挑战验证等。

    Args:
        event_name: 事件名称 (如 "ModEventC2S")
        args: 事件参数列表
        operation_type: 操作类型 (0=发送)

    Returns:
        NBT 网络格式编码的字节数据

    逆向来源: Retalcer __init__.py:257-263, neomega py_rpc
    """
    from .nbt import marshal_network

    packet = {
        "Value": [event_name, args, None],
        "OperationType": operation_type,
    }
    return marshal_network(packet)


def build_ai_command_packet(
    player_id: int | str, command: str, request_uuid: Optional[str] = None
) -> dict:
    """构建 AI 命令的 PyRpc 数据包结构。

    Args:
        player_id: 玩家(机器人)运行时ID
        command: 要执行的命令
        request_uuid: 请求UUID, 默认随机生成

    Returns:
        PyRpc 数据包字典
    """
    return {
        "Value": [
            "ModEventC2S",
            [
                "Minecraft",
                "aiCommand",
                "ExecuteCommandEvent",
                {
                    "playerId": str(player_id),
                    "cmd": command,
                    "uuid": request_uuid or str(uuid.uuid4()),
                },
            ],
            None,
        ],
        "OperationType": 0,
    }


# ----------------------------------------------------------------------
# PyRpc 事件类型
# ----------------------------------------------------------------------


class PyRpcEventType:
    """PyRpc 事件类型常量。

    逆向来源: neomega py_rpc 模块
    """

    # 客户端 → 服务器
    MOD_EVENT_C2S = "ModEventC2S"  # MOD事件(客户端→服务器)
    HEART_BEAT_C2S = "C2SHeartBeat"  # 心跳
    GET_MCP_CHECK_NUM = "GetMCPCheckNum"  # 获取MCP检查数
    SET_MCP_CHECK_NUM = "SetMCPCheckNum"  # 设置MCP检查数
    SET_OWNER_ID = "SetOwnerId"  # 设置房主
    SYNC_USING_MOD = "SyncUsingMod"  # 同步MOD使用
    SYNC_VIP_SKIN_UUID = "SyncVipSkinUUID"  # 同步VIP皮肤
    GET_START_TYPE = "GetStartType"  # 获取启动类型
    SET_START_TYPE = "SetStartType"  # 设置启动类型

    # 服务器 → 客户端
    MOD_EVENT_S2C = "ModEventS2C"  # MOD事件(服务器→客户端)
    HEART_BEAT_S2C = "S2CHeartBeat"  # 心跳响应
    PLAYER_UI_INIT = "PlayerUiInit"  # 玩家UI初始化
    ARENA_GAME_PLAYER_FINISH_LOAD = "ArenaGamePlayerFinishLoad"  # 竞技场加载完成
    CLIENT_LOAD_ADDONS_FINISHED = "ClientLoadAddonsFinishedFromGac"  # 插件加载完成


__all__ = [
    "CONSOLE_ONLY_PREFIXES",
    "DEFAULT_BLOCK_SPEED",
    "DEFAULT_COMMAND_SPEED",
    "DEFAULT_CONTAINER_SPEED",
    "DEFAULT_GROUP_WAIT",
    "DEFAULT_NBT_DELAY",
    "PACKET_ID_PY_RPC",
    "SPEED_PRESETS",
    "apply_speed_preset",
    "CommandRateLimiter",
    "MagicCommandSender",
    "build_py_rpc_packet",
    "build_ai_command_packet",
    "PyRpcEventType",
]
