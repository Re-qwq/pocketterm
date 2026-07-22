"""StarShuttler - 租赁服进入逻辑。

逆向自 NovaBuilder 的 StarShuttler 模块, 适配到 PocketTerm 项目。
提供租赁服进入、命令发送、游戏接口、数据包分发和 PyRPC 通信能力。

主要组件:
    - GameInterface:     游戏接口 (方块/容器/物品操作)
    - CmdSender:         命令发送器 (命令请求/响应/超时)
    - PacketDispatcher:  数据包分发器
    - PyRPC:             Python RPC 事件系统
"""

from __future__ import annotations

from .game_interface import (
    GameInterface, BlockOperationResult, ContainerOperationResult,
)
from .cmd_sender import (
    CmdSender, CommandRequest, CommandOutput, CommandRequestCallback,
)
from .packet_dispatcher import (
    PacketDispatcher, PacketHandler, PacketID,
)
from .py_rpc import (
    PyRPC, PyRPCEvent, PyRPCEventHandler, PyRPCChannel,
)

__all__ = [
    # game_interface
    "GameInterface", "BlockOperationResult", "ContainerOperationResult",
    # cmd_sender
    "CmdSender", "CommandRequest", "CommandOutput", "CommandRequestCallback",
    # packet_dispatcher
    "PacketDispatcher", "PacketHandler", "PacketID",
    # py_rpc
    "PyRPC", "PyRPCEvent", "PyRPCEventHandler", "PyRPCChannel",
]
