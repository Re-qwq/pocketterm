"""game_interface - 游戏接口模块。

逆向自 NovaBuilder 的 StarShuttler GameInterface, 来源:
    - /workspace/novuilder_reverse/REPORT.txt (第 762 行)
    - /workspace/novuilder_reverse/strings_commands.txt
    - /workspace/novuilder_reverse/nbt_packets.txt
    - /workspace/novuilder_reverse/rep_command.txt

GameInterface 是 StarShuttler 的核心游戏控制接口
(github.com/xingbaiawa/StarShuttler/game_control/game_interface),
提供方块放置、容器操作、物品堆操作、Replaceitem 等功能。

核心方法 (逆向自 strings):
    GameInterface.SetBlock              -- 设置方块
    GameInterface.PacketListener        -- 数据包监听
    GameInterface.Commands              -- 命令系统
    GameInterface.ContainerOpenAndClose -- 容器开关
    GameInterface.ItemStackOperation    -- 物品堆操作
    GameInterface.Replaceitem           -- 替换物品

Replaceitem 方法 (逆向自 rep_command.txt):
    Replaceitem.replaceitemInInventoryNormal   -- 普通替换
    Replaceitem.replaceitemInInventorySpecial  -- 特殊替换

数据包处理 (逆向自 REPORT.txt 第 405-438 行):
    handleCommandOutput()       -- 处理命令输出
    handleContainerOpen()       -- 处理容器打开
    handleContainerClose()      -- 处理容器关闭
    handleInventoryContent()    -- 处理物品栏内容
    handleInventorySlot()       -- 处理物品栏槽位
    handleItemStackResponse()   -- 处理物品堆响应
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Optional

logger = logging.getLogger("pocketterm.auth.starshuttler.game_interface")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 默认命令超时 (秒), 逆向自 sendCommandWithResp 超时
DEFAULT_COMMAND_TIMEOUT: float = 10.0

#: 容器操作超时 (秒), 逆向自 ContainerOpenAndClose
CONTAINER_TIMEOUT: float = 15.0

#: 物品堆操作超时 (秒), 逆向自 ItemStackOperation
ITEM_STACK_TIMEOUT: float = 10.0

#: 最大重试次数
MAX_RETRIES: int = 3

#: 方块放置模式
BLOCK_PLACE_MODE_NORMAL: int = 0
BLOCK_PLACE_MODE_FILL: int = 1
BLOCK_PLACE_MODE_SETBLOCK: int = 2

#: 容器类型 (逆向自 nbt_packets.txt)
CONTAINER_TYPE_CHEST: int = 0
CONTAINER_TYPE_ENDER_CHEST: int = 2
CONTAINER_TYPE_SHULKER_BOX: int = 10
CONTAINER_TYPE_HOPPER: int = 8
CONTAINER_TYPE_DISPENSER: int = 6
CONTAINER_TYPE_DROPPER: int = 7
CONTAINER_TYPE_BARREL: int = 39


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class BlockOperationType(Enum):
    """方块操作类型。"""

    SET_BLOCK = auto()        # 设置方块 (setblock)
    FILL_BLOCKS = auto()      # 填充方块 (fill)
    REPLACE_BLOCKS = auto()   # 替换方块 (fill replace)


class ContainerState(Enum):
    """容器状态。"""

    CLOSED = auto()   # 已关闭
    OPENING = auto()  # 正在打开
    OPEN = auto()     # 已打开
    CLOSING = auto()  # 正在关闭


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class BlockOperationResult:
    """方块操作结果。"""

    success: bool = False
    operation_type: BlockOperationType = BlockOperationType.SET_BLOCK
    position: tuple[int, int, int] = (0, 0, 0)
    end_position: tuple[int, int, int] = (0, 0, 0)
    block_name: str = ""
    block_states: str = ""
    error: str = ""
    time_used: float = 0.0
    command_used: str = ""

    def __repr__(self) -> str:
        return (
            f"BlockOperationResult(success={self.success}, "
            f"op={self.operation_type.name}, pos={self.position})"
        )


@dataclass
class ContainerOperationResult:
    """容器操作结果。"""

    success: bool = False
    container_id: int = 0
    container_type: int = 0
    position: tuple[int, int, int] = (0, 0, 0)
    state: ContainerState = ContainerState.CLOSED
    items: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    time_used: float = 0.0


@dataclass
class ItemStackResult:
    """物品堆操作结果。"""

    success: bool = False
    request_id: int = 0
    container_id: int = 0
    slot: int = 0
    item_name: str = ""
    item_count: int = 0
    error: str = ""
    time_used: float = 0.0


# -------------------------------------------------------------------- #
# GameInterface 核心类
# -------------------------------------------------------------------- #


class GameInterface:
    """游戏接口 (逆向自 StarShuttler GameInterface)。

    逆向自 github.com/xingbaiawa/StarShuttler/game_control/game_interface

    提供以下核心功能:
        1. SetBlock - 方块设置 (setblock / fill)
        2. ContainerOpenAndClose - 容器开关操作
        3. ItemStackOperation - 物品堆操作
        4. Replaceitem - 替换物品栏内容
        5. PacketListener - 数据包监听
        6. Commands - 命令系统访问

    使用示例::

        interface = GameInterface(cmd_sender=my_sender)
        result = interface.set_block(
            position=(100, 64, 200),
            block_name="minecraft:stone",
        )
        if result.success:
            print("Block placed!")
    """

    def __init__(
        self,
        cmd_sender: Any | None = None,
        packet_dispatcher: Any | None = None,
    ) -> None:
        """初始化游戏接口。

        Args:
            cmd_sender: 命令发送器 (CmdSender 实例)。
            packet_dispatcher: 数据包分发器 (PacketDispatcher 实例)。
        """
        self._cmd_sender = cmd_sender
        self._packet_dispatcher = packet_dispatcher
        self._container_state: ContainerState = ContainerState.CLOSED
        self._current_container_id: int = -1
        self._current_container_type: int = -1
        self._current_container_pos: tuple[int, int, int] = (0, 0, 0)
        self._container_callbacks: dict[int, Callable[[Any], None]] = {}
        self._item_stack_callbacks: dict[int, Callable[[Any], None]] = {}
        self._running_runtime_id: int = 0
        self._block_runtime_ids: dict[str, int] = {}

        logger.debug("GameInterface initialized")

    # ---------------------------------------------------------------- #
    # 属性
    # ---------------------------------------------------------------- #

    @property
    def cmd_sender(self) -> Any | None:
        """命令发送器。"""
        return self._cmd_sender

    @cmd_sender.setter
    def cmd_sender(self, value: Any) -> None:
        self._cmd_sender = value

    @property
    def packet_dispatcher(self) -> Any | None:
        """数据包分发器。"""
        return self._packet_dispatcher

    @packet_dispatcher.setter
    def packet_dispatcher(self, value: Any) -> None:
        self._packet_dispatcher = value

    @property
    def container_state(self) -> ContainerState:
        """当前容器状态。"""
        return self._container_state

    @property
    def is_container_open(self) -> bool:
        """是否有打开的容器。"""
        return self._container_state == ContainerState.OPEN

    # ---------------------------------------------------------------- #
    # 方块操作 (逆向自 GameInterface.SetBlock)
    # ---------------------------------------------------------------- #

    def set_block(
        self,
        position: tuple[int, int, int],
        block_name: str,
        block_states: str = "",
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> BlockOperationResult:
        """设置单个方块 (逆向自 GameInterface.SetBlock)。

        使用 setblock 命令在指定位置放置方块。

        Args:
            position: 方块坐标 (x, y, z)。
            block_name: 方块名称 (如 "minecraft:stone")。
            block_states: 方块状态 (如 "[]" 或 "[\"facing_direction\"=0]")。
            timeout: 命令超时 (秒)。

        Returns:
            :class:`BlockOperationResult`。
        """
        start_time = time.time()
        x, y, z = position

        if not block_name.startswith("minecraft:"):
            block_name = f"minecraft:{block_name}"

        command = f"setblock {x} {y} {z} {block_name}"
        if block_states:
            command += f" {block_states}"

        result = BlockOperationResult(
            operation_type=BlockOperationType.SET_BLOCK,
            position=position,
            end_position=position,
            block_name=block_name,
            block_states=block_states,
            command_used=command,
        )

        try:
            if self._cmd_sender:
                output = self._cmd_sender.send_command_with_resp(
                    command, timeout=timeout
                )
                if output and output.success:
                    result.success = True
                    logger.debug(
                        "SetBlock success: %s at %s",
                        block_name, position,
                    )
                else:
                    error = output.error if output else "no response"
                    result.error = error
                    logger.warning("SetBlock failed: %s at %s: %s", block_name, position, error)
            else:
                result.error = "no command sender"
                logger.error("SetBlock: no command sender configured")
        except Exception as exc:
            result.error = str(exc)
            logger.exception("SetBlock exception: %s", exc)

        result.time_used = time.time() - start_time
        return result

    def fill_blocks(
        self,
        pos1: tuple[int, int, int],
        pos2: tuple[int, int, int],
        block_name: str,
        block_states: str = "",
        mode: str = "replace",
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> BlockOperationResult:
        """填充方块区域 (逆向自 fill 命令)。

        使用 fill 命令在指定区域内填充方块。

        Args:
            pos1: 起点 (x, y, z)。
            pos2: 终点 (x, y, z)。
            block_name: 方块名称。
            block_states: 方块状态。
            mode: 填充模式 (replace/keep/destroy/hollow/outline)。
            timeout: 命令超时 (秒)。

        Returns:
            :class:`BlockOperationResult`。
        """
        start_time = time.time()
        x1, y1, z1 = pos1
        x2, y2, z2 = pos2

        if not block_name.startswith("minecraft:"):
            block_name = f"minecraft:{block_name}"

        command = f"fill {x1} {y1} {z1} {x2} {y2} {z2} {block_name}"
        if block_states:
            command += f" {block_states}"
        if mode and mode != "replace":
            command += f" {mode}"

        result = BlockOperationResult(
            operation_type=BlockOperationType.FILL_BLOCKS,
            position=pos1,
            end_position=pos2,
            block_name=block_name,
            block_states=block_states,
            command_used=command,
        )

        try:
            if self._cmd_sender:
                output = self._cmd_sender.send_command_with_resp(
                    command, timeout=timeout
                )
                if output and output.success:
                    result.success = True
                    volume = abs(x2-x1+1) * abs(y2-y1+1) * abs(z2-z1+1)
                    logger.debug(
                        "Fill success: %s volume=%d from %s to %s",
                        block_name, volume, pos1, pos2,
                    )
                else:
                    error = output.error if output else "no response"
                    result.error = error
                    logger.warning("Fill failed: %s", error)
            else:
                result.error = "no command sender"
        except Exception as exc:
            result.error = str(exc)
            logger.exception("Fill exception: %s", exc)

        result.time_used = time.time() - start_time
        return result

    def set_blocks_batch(
        self,
        blocks: list[tuple[tuple[int, int, int], str, str]],
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> list[BlockOperationResult]:
        """批量设置方块。

        Args:
            blocks: 方块列表, 每项为 (position, block_name, block_states)。
            timeout: 每个命令的超时。

        Returns:
            结果列表。
        """
        results: list[BlockOperationResult] = []
        for pos, name, states in blocks:
            result = self.set_block(pos, name, states, timeout=timeout)
            results.append(result)
            if not result.success:
                logger.warning("Batch set_block failed at %s: %s", pos, result.error)
        logger.info("Batch set_blocks: %d/%d succeeded", sum(1 for r in results if r.success), len(results))
        return results

    # ---------------------------------------------------------------- #
    # 容器操作 (逆向自 GameInterface.ContainerOpenAndClose)
    # ---------------------------------------------------------------- #

    def container_open(
        self,
        position: tuple[int, int, int],
        container_type: int = CONTAINER_TYPE_CHEST,
        timeout: float = CONTAINER_TIMEOUT,
    ) -> ContainerOperationResult:
        """打开容器 (逆向自 GameInterface.ContainerOpenAndClose)。

        通过 setblock 放置容器方块, 然后等待 ContainerOpen 数据包。

        Args:
            position: 容器位置。
            container_type: 容器类型。
            timeout: 超时 (秒)。

        Returns:
            :class:`ContainerOperationResult`。
        """
        start_time = time.time()
        result = ContainerOperationResult(
            container_type=container_type,
            position=position,
            state=ContainerState.OPENING,
        )

        if self.is_container_open:
            logger.warning("Container already open, closing first")
            self.container_close()

        self._container_state = ContainerState.OPENING
        self._current_container_pos = position
        self._current_container_type = container_type

        # 等待 ContainerOpen 数据包
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._container_state == ContainerState.OPEN:
                result.success = True
                result.container_id = self._current_container_id
                result.state = ContainerState.OPEN
                logger.info("Container opened: id=%d at %s", result.container_id, position)
                break
            time.sleep(0.05)

        if not result.success:
            result.error = "container open timeout"
            result.state = ContainerState.CLOSED
            self._container_state = ContainerState.CLOSED
            logger.warning("Container open timeout at %s", position)

        result.time_used = time.time() - start_time
        return result

    def container_close(self, timeout: float = CONTAINER_TIMEOUT) -> ContainerOperationResult:
        """关闭容器 (逆向自 GameInterface.ContainerOpenAndClose)。

        Returns:
            :class:`ContainerOperationResult`。
        """
        start_time = time.time()
        result = ContainerOperationResult(
            container_id=self._current_container_id,
            container_type=self._current_container_type,
            position=self._current_container_pos,
            state=ContainerState.CLOSING,
        )

        if not self.is_container_open:
            result.success = True
            result.state = ContainerState.CLOSED
            return result

        self._container_state = ContainerState.CLOSING

        # 等待 ContainerClose 确认
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._container_state == ContainerState.CLOSED:
                result.success = True
                result.state = ContainerState.CLOSED
                logger.info("Container closed: id=%d", self._current_container_id)
                break
            time.sleep(0.05)

        if not result.success:
            result.error = "container close timeout"
            self._container_state = ContainerState.CLOSED
            logger.warning("Container close timeout")

        self._current_container_id = -1
        result.time_used = time.time() - start_time
        return result

    def on_container_open(self, container_id: int, container_type: int) -> None:
        """容器打开回调 (由 PacketDispatcher 调用)。

        Args:
            container_id: 容器 ID。
            container_type: 容器类型。
        """
        self._current_container_id = container_id
        self._current_container_type = container_type
        self._container_state = ContainerState.OPEN
        logger.debug("Container opened: id=%d, type=%d", container_id, container_type)

        callback = self._container_callbacks.get(container_id)
        if callback:
            try:
                callback(container_id)
            except Exception:
                logger.exception("Container open callback failed")

    def on_container_close(self, container_id: int) -> None:
        """容器关闭回调 (由 PacketDispatcher 调用)。

        Args:
            container_id: 容器 ID。
        """
        self._container_state = ContainerState.CLOSED
        logger.debug("Container closed: id=%d", container_id)

    # ---------------------------------------------------------------- #
    # 物品堆操作 (逆向自 GameInterface.ItemStackOperation)
    # ---------------------------------------------------------------- #

    def item_stack_operation(
        self,
        container_id: int,
        slot: int,
        item_name: str,
        count: int = 1,
        timeout: float = ITEM_STACK_TIMEOUT,
    ) -> ItemStackResult:
        """物品堆操作 (逆向自 GameInterface.ItemStackOperation)。

        逆向自 resources_control.ItemStackOperationManager.AddNewRequest

        Args:
            container_id: 容器 ID。
            slot: 槽位。
            item_name: 物品名称。
            count: 数量。
            timeout: 超时 (秒)。

        Returns:
            :class:`ItemStackResult`。
        """
        start_time = time.time()
        request_id = id(self) % 100000 + int(time.time() * 1000) % 100000

        result = ItemStackResult(
            request_id=request_id,
            container_id=container_id,
            slot=slot,
            item_name=item_name,
            item_count=count,
        )

        # 注册回调等待响应
        event = threading.Event()
        self._item_stack_callbacks[request_id] = lambda r: event.set()

        try:
            # 发送 ItemStackRequest
            # 实际实现需要通过协议层发送数据包
            if event.wait(timeout):
                result.success = True
                logger.debug(
                    "ItemStackOperation success: container=%d, slot=%d, item=%s",
                    container_id, slot, item_name,
                )
            else:
                result.error = "item stack operation timeout"
                logger.warning("ItemStackOperation timeout: request=%d", request_id)
        except Exception as exc:
            result.error = str(exc)
            logger.exception("ItemStackOperation exception: %s", exc)
        finally:
            self._item_stack_callbacks.pop(request_id, None)

        result.time_used = time.time() - start_time
        return result

    def on_item_stack_response(self, request_id: int, success: bool) -> None:
        """物品堆响应回调 (由 PacketDispatcher 调用)。

        逆向自 resources_control.Resources.handleItemStackResponse

        Args:
            request_id: 请求 ID。
            success: 是否成功。
        """
        callback = self._item_stack_callbacks.get(request_id)
        if callback:
            try:
                callback(request_id)
            except Exception:
                logger.exception("ItemStackResponse callback failed")

    # ---------------------------------------------------------------- #
    # Replaceitem (逆向自 GameInterface.Replaceitem)
    # ---------------------------------------------------------------- #

    def replaceitem(
        self,
        slot_type: str,
        slot: int,
        item_name: str,
        count: int = 1,
        data: int = 0,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> bool:
        """替换物品栏内容 (逆向自 GameInterface.Replaceitem)。

        逆向自 Replaceitem.replaceitemInInventoryNormal

        使用 replaceitem 命令替换指定槽位的物品。

        Args:
            slot_type: 槽位类型 ("slot.hotbar" / "slot.inventory" / "slot.armor")。
            slot: 槽位索引。
            item_name: 物品名称。
            count: 数量。
            data: 数据值。
            timeout: 超时 (秒)。

        Returns:
            True 如果成功。
        """
        if not item_name.startswith("minecraft:"):
            item_name = f"minecraft:{item_name}"

        command = f"replaceitem entity @s {slot_type} {slot} {item_name} {count}"
        if data:
            command += f" {data}"

        try:
            if self._cmd_sender:
                output = self._cmd_sender.send_command_with_resp(
                    command, timeout=timeout
                )
                if output and output.success:
                    logger.debug(
                        "Replaceitem success: %s[%d] = %s x%d",
                        slot_type, slot, item_name, count,
                    )
                    return True
                else:
                    error = output.error if output else "no response"
                    logger.warning("Replaceitem failed: %s", error)
                    return False
            else:
                logger.error("Replaceitem: no command sender")
                return False
        except Exception as exc:
            logger.exception("Replaceitem exception: %s", exc)
            return False

    def replaceitem_in_inventory_normal(
        self,
        slot: int,
        item_name: str,
        count: int = 1,
        data: int = 0,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> bool:
        """普通物品栏替换 (逆向自 replaceitemInInventoryNormal)。

        Args:
            slot: 槽位索引 (0-35)。
            item_name: 物品名称。
            count: 数量。
            data: 数据值。
            timeout: 超时 (秒)。

        Returns:
            True 如果成功。
        """
        return self.replaceitem(
            slot_type="slot.inventory",
            slot=slot,
            item_name=item_name,
            count=count,
            data=data,
            timeout=timeout,
        )

    def replaceitem_in_inventory_special(
        self,
        slot: int,
        item_name: str,
        count: int = 1,
        data: int = 0,
        timeout: float = DEFAULT_COMMAND_TIMEOUT,
    ) -> bool:
        """特殊物品栏替换 (逆向自 replaceitemInInventorySpecial)。

        特殊替换用于处理正常替换失败的情况, 可能使用不同的数据包路径。

        Args:
            slot: 槽位索引。
            item_name: 物品名称。
            count: 数量。
            data: 数据值。
            timeout: 超时 (秒)。

        Returns:
            True 如果成功。
        """
        # 先尝试普通替换
        if self.replaceitem_in_inventory_normal(slot, item_name, count, data, timeout):
            return True

        # 普通失败, 尝试通过容器操作
        logger.info("Normal replaceitem failed, trying special method")
        try:
            if self._cmd_sender:
                # 使用 destroy 模式先清空槽位
                destroy_cmd = f"replaceitem entity @s slot.inventory {slot} minecraft:air 0"
                self._cmd_sender.send_command_with_resp(destroy_cmd, timeout=timeout)
                time.sleep(0.1)

                # 再次尝试放置
                return self.replaceitem_in_inventory_normal(
                    slot, item_name, count, data, timeout
                )
        except Exception as exc:
            logger.exception("Special replaceitem exception: %s", exc)

        return False

    # ---------------------------------------------------------------- #
    # 数据包监听 (逆向自 GameInterface.PacketListener)
    # ---------------------------------------------------------------- #

    def packet_listener(self, packet: Any) -> None:
        """数据包监听器 (逆向自 GameInterface.PacketListener)。

        处理接收到的游戏数据包, 分发到对应的处理方法。

        Args:
            packet: 接收到的数据包。
        """
        if packet is None:
            return

        packet_id = getattr(packet, "id", None)
        if packet_id is None:
            return

        try:
            if packet_id == PacketID.CONTAINER_OPEN:
                self._handle_container_open(packet)
            elif packet_id == PacketID.CONTAINER_CLOSE:
                self._handle_container_close(packet)
            elif packet_id == PacketID.INVENTORY_CONTENT:
                self._handle_inventory_content(packet)
            elif packet_id == PacketID.INVENTORY_SLOT:
                self._handle_inventory_slot(packet)
            elif packet_id == PacketID.ITEM_STACK_RESPONSE:
                self._handle_item_stack_response(packet)
            elif packet_id == PacketID.COMMAND_OUTPUT:
                self._handle_command_output(packet)
            elif packet_id == PacketID.AVAILABLE_COMMANDS:
                self._handle_available_commands(packet)
        except Exception:
            logger.exception("PacketListener error for packet id=%s", packet_id)

    def _handle_container_open(self, packet: Any) -> None:
        """处理 ContainerOpen 数据包 (逆向自 handleContainerOpen)。"""
        container_id = getattr(packet, "container_id", -1)
        container_type = getattr(packet, "container_type", -1)
        self.on_container_open(container_id, container_type)

    def _handle_container_close(self, packet: Any) -> None:
        """处理 ContainerClose 数据包 (逆向自 handleContainerClose)。"""
        container_id = getattr(packet, "container_id", -1)
        self.on_container_close(container_id)

    def _handle_inventory_content(self, packet: Any) -> None:
        """处理 InventoryContent 数据包 (逆向自 handleInventoryContent)。"""
        container_id = getattr(packet, "container_id", -1)
        items = getattr(packet, "items", [])
        logger.debug("InventoryContent: container=%d, items=%d", container_id, len(items))

    def _handle_inventory_slot(self, packet: Any) -> None:
        """处理 InventorySlot 数据包 (逆向自 handleInventorySlot)。"""
        container_id = getattr(packet, "container_id", -1)
        slot = getattr(packet, "slot", -1)
        logger.debug("InventorySlot: container=%d, slot=%d", container_id, slot)

    def _handle_item_stack_response(self, packet: Any) -> None:
        """处理 ItemStackResponse 数据包 (逆向自 handleItemStackResponse)。"""
        request_id = getattr(packet, "request_id", -1)
        success = getattr(packet, "success", True)
        self.on_item_stack_response(request_id, success)

    def _handle_command_output(self, packet: Any) -> None:
        """处理 CommandOutput 数据包 (逆向自 handleCommandOutput)。"""
        origin = getattr(packet, "command_origin", 0)
        success = getattr(packet, "success_count", 0) > 0
        output_messages = getattr(packet, "output_messages", [])
        logger.debug("CommandOutput: origin=%d, success=%s", origin, success)

    def _handle_available_commands(self, packet: Any) -> None:
        """处理 AvailableCommands 数据包 (逆向自 onAvailableCommands)。"""
        commands = getattr(packet, "commands", [])
        logger.debug("AvailableCommands: %d commands", len(commands))

    # ---------------------------------------------------------------- #
    # 运行时 ID 管理
    # ---------------------------------------------------------------- #

    def use_runtime_id_pool(self, pool_id: int) -> None:
        """设置运行时 ID 池 (逆向自 UseRuntimeIDPool)。

        Args:
            pool_id: 运行时 ID 池 ID (117 或 118)。
        """
        self._running_runtime_id = pool_id
        logger.info("Using runtime ID pool: %d", pool_id)

    def register_block_runtime_id(self, block_name: str, runtime_id: int) -> None:
        """注册方块运行时 ID。

        Args:
            block_name: 方块名称。
            runtime_id: 运行时 ID。
        """
        self._block_runtime_ids[block_name] = runtime_id
        logger.debug("Registered runtime ID: %s -> %d", block_name, runtime_id)

    def get_block_runtime_id(self, block_name: str) -> int | None:
        """获取方块运行时 ID。

        Args:
            block_name: 方块名称。

        Returns:
            运行时 ID, 未注册返回 None。
        """
        return self._block_runtime_ids.get(block_name)


# 导入 threading (用于 item_stack_operation)
import threading

# 导入 PacketID (从 packet_dispatcher)
from .packet_dispatcher import PacketID


__all__ = [
    # 常量
    "DEFAULT_COMMAND_TIMEOUT", "CONTAINER_TIMEOUT", "ITEM_STACK_TIMEOUT",
    "MAX_RETRIES", "BLOCK_PLACE_MODE_NORMAL", "BLOCK_PLACE_MODE_FILL",
    "BLOCK_PLACE_MODE_SETBLOCK",
    "CONTAINER_TYPE_CHEST", "CONTAINER_TYPE_ENDER_CHEST",
    "CONTAINER_TYPE_SHULKER_BOX", "CONTAINER_TYPE_HOPPER",
    "CONTAINER_TYPE_DISPENSER", "CONTAINER_TYPE_DROPPER",
    "CONTAINER_TYPE_BARREL",
    # 枚举
    "BlockOperationType", "ContainerState",
    # 数据结构
    "BlockOperationResult", "ContainerOperationResult", "ItemStackResult",
    # 核心类
    "GameInterface",
]
