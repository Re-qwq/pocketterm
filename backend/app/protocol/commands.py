"""Minecraft Bedrock 命令系统。

本模块封装了常用的 Minecraft Bedrock Edition 命令, 提供类型安全的异步 API。
通过 :class:`CommandManager` 可以方便地发送 setblock、fill、give、tp 等命令,
并获取结构化的 :class:`CommandResponse` 响应。

逆向来源: NovaBuilder ``game_control/game_interface/commands.go``

基本用法::

    from app.protocol.connection import BedrockClient
    from app.protocol.commands import CommandManager

    client = BedrockClient(sauth_json="...", device_fingerprint={...})
    await client.connect("example.com", 19132)

    cmd = CommandManager(client)

    # 设置方块
    resp = await cmd.setblock(0, 64, 0, "minecraft:stone")
    if resp.success:
        print("方块设置成功")

    # 广播消息
    await cmd.say("Hello, World!")

    await client.disconnect()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .connection import BedrockClient

logger = logging.getLogger("pocketterm.commands")


# ======================================================================
# 命令响应
# ======================================================================


@dataclass
class CommandResponse:
    """命令响应数据。

    封装命令执行的结果, 无论成功或失败都返回此对象 (不抛出异常)。

    Attributes:
        request_id: 请求 ID (本地递增计数器, 用于追踪和关联)。
        success: 命令是否执行成功。
        output: 命令输出文本。成功时为服务器返回的输出消息,
            失败时为错误描述信息。
        data: 附加结构化数据 (如解析后的命令返回值), 默认为空字典。
    """

    request_id: int
    success: bool
    output: str
    data: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        status = "OK" if self.success else "FAIL"
        preview = self.output[:80] if self.output else ""
        return (
            f"CommandResponse(id={self.request_id}, {status}, "
            f"output={preview!r})"
        )


# ======================================================================
# 命令管理器
# ======================================================================


class CommandManager:
    """命令管理器 — 封装常用 Minecraft Bedrock 命令。

    本类通过底层的 :class:`BedrockClient` 发送命令, 并将结果包装为
    :class:`CommandResponse`。所有方法均为异步协程, 不会抛出异常
    (网络错误、超时等均通过 ``CommandResponse.success=False`` 返回)。

    Args:
        client: 已连接的 :class:`BedrockClient` 实例。

    Example::

        cmd = CommandManager(client)
        resp = await cmd.setblock(0, 64, 0, "minecraft:stone")
        if not resp.success:
            print(f"失败: {resp.output}")
    """

    def __init__(self, client: BedrockClient) -> None:
        """初始化命令管理器。

        Args:
            client: 已连接 (或待连接) 的 :class:`BedrockClient` 实例。
        """
        self.client: BedrockClient = client
        self._request_id: int = 0

    def _next_request_id(self) -> int:
        """生成下一个本地请求 ID (递增计数器)。

        Returns:
            递增后的请求 ID。
        """
        self._request_id += 1
        return self._request_id

    # ------------------------------------------------------------------
    # 通用命令发送
    # ------------------------------------------------------------------

    async def send(self, command: str, timeout: float = 30.0) -> CommandResponse:
        """发送任意命令并等待响应。

        这是最基础的命令发送方法, 其他高级方法 (setblock、fill 等) 都基于此。
        命令字符串开头的 ``/`` 会被自动去掉。

        Args:
            command: 命令字符串 (如 ``"say hello"`` 或 ``"/list"``)。
            timeout: 响应超时时间 (秒), 默认 30 秒。

        Returns:
            :class:`CommandResponse` 对象。成功时 ``success=True``,
            ``output`` 为服务器返回的文本; 超时或出错时 ``success=False``,
            ``output`` 为错误描述。
        """
        command = command.lstrip("/")
        request_id = self._next_request_id()
        logger.debug("发送命令 (request_id=%d, timeout=%.1fs): %s",
                     request_id, timeout, command)

        try:
            output = await asyncio.wait_for(
                self.client.send_command(command),
                timeout=timeout,
            )
            logger.debug("命令完成 (request_id=%d): %s", request_id, command)
            return CommandResponse(
                request_id=request_id,
                success=True,
                output=output or "",
            )
        except asyncio.TimeoutError:
            logger.warning("命令执行超时 (%.1fs): %s", timeout, command)
            return CommandResponse(
                request_id=request_id,
                success=False,
                output=f"命令执行超时 ({timeout}s): {command}",
            )
        except Exception as exc:
            logger.warning("命令执行失败: %s -> %s", command, exc)
            return CommandResponse(
                request_id=request_id,
                success=False,
                output=str(exc),
            )

    # ------------------------------------------------------------------
    # 方块相关命令
    # ------------------------------------------------------------------

    async def setblock(
        self,
        x: int,
        y: int,
        z: int,
        block: str,
        block_states: str = "",
        mode: str = "replace",
    ) -> CommandResponse:
        """设置指定坐标的方块。

        Args:
            x, y, z: 方块坐标。
            block: 方块名称 (如 ``"minecraft:stone"``)。
            block_states: 方块状态 JSON 字符串 (如 ``'{"stone_type":"granite"}'``),
                为空则不指定状态。
            mode: 放置模式, 可选 ``"replace"`` (替换)、``"keep"`` (仅替换空气)、
                ``"destroy"`` (破坏原有方块后放置)。默认 ``"replace"``。

        Returns:
            :class:`CommandResponse` 对象。
        """
        cmd = f"setblock {x} {y} {z} {block}"
        if block_states:
            cmd += f" {block_states}"
        cmd += f" {mode}"
        return await self.send(cmd)

    async def fill(
        self,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
        block: str,
        block_states: str = "",
        mode: str = "replace",
    ) -> CommandResponse:
        """用指定方块填充一个长方体区域。

        Args:
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。
            block: 方块名称 (如 ``"minecraft:stone"``)。
            block_states: 方块状态 JSON 字符串, 为空则不指定状态。
            mode: 填充模式, 可选 ``"replace"``、``"keep"``、``"destroy"``
                ``"hollow"`` (仅外框, 内部填充空气)、``"outline"`` (仅外框)。
                默认 ``"replace"``。

        Returns:
            :class:`CommandResponse` 对象。
        """
        cmd = f"fill {x1} {y1} {z1} {x2} {y2} {z2} {block}"
        if block_states:
            cmd += f" {block_states}"
        cmd += f" {mode}"
        return await self.send(cmd)

    async def clone(
        self,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
        x: int,
        y: int,
        z: int,
        mode: str = "replace",
    ) -> CommandResponse:
        """克隆 (复制) 一个区域的方块到目标位置。

        Args:
            x1, y1, z1: 源区域起始角坐标。
            x2, y2, z2: 源区域结束角坐标。
            x, y, z: 目标区域起始角坐标 (克隆目的地)。
            mode: 克隆模式, 可选 ``"replace"``、``"masked"`` (仅非空气方块)、
                ``"filtered"`` (仅指定方块)。默认 ``"replace"``。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.send(
            f"clone {x1} {y1} {z1} {x2} {y2} {z2} {x} {y} {z} {mode}"
        )

    async def getblock(self, x: int, y: int, z: int) -> CommandResponse:
        """获取指定坐标的方块信息。

        Args:
            x, y, z: 方块坐标。

        Returns:
            :class:`CommandResponse` 对象。成功时 ``output`` 包含方块名称和状态。
        """
        return await self.send(f"getblock {x} {y} {z}")

    async def testforblock(
        self,
        x: int,
        y: int,
        z: int,
        block: str,
    ) -> CommandResponse:
        """检测指定坐标是否为特定方块。

        Args:
            x, y, z: 方块坐标。
            block: 期望的方块名称 (如 ``"minecraft:stone"``)。

        Returns:
            :class:`CommandResponse` 对象。方块匹配时 ``success=True``。
        """
        return await self.send(f"testforblock {x} {y} {z} {block}")

    # ------------------------------------------------------------------
    # 物品相关命令
    # ------------------------------------------------------------------

    async def give(
        self,
        player: str,
        item: str,
        count: int = 1,
        data: int = 0,
        components: str = "",
    ) -> CommandResponse:
        """给予玩家物品。

        Args:
            player: 目标玩家选择器 (如 ``"@s"``、``"Steve"``)。
            item: 物品名称 (如 ``"minecraft:diamond"``)。
            count: 物品数量, 默认 1。
            data: 物品数据值 (辅助数据), 默认 0。
            components: 物品组件 JSON 字符串 (可选, 用于自定义物品属性)。

        Returns:
            :class:`CommandResponse` 对象。
        """
        cmd = f"give {player} {item} {count} {data}"
        if components:
            cmd += f" {components}"
        return await self.send(cmd)

    async def replaceitem(
        self,
        target: str,
        slot: str,
        item: str,
        count: int = 1,
        data: int = 0,
    ) -> CommandResponse:
        """替换目标物品栏中的物品。

        Args:
            target: 目标描述, 如 ``"block x y z"`` (方块容器) 或
                ``"entity @s"`` (实体物品栏)。
            slot: 物品栏位置, 如 ``"slot.container.0"``、
                ``"slot.hotbar.0"``、``"slot.inventory.0"``、
                ``"slot.armor.head"`` 等。
            item: 物品名称 (如 ``"minecraft:diamond_sword"``)。
            count: 物品数量, 默认 1。
            data: 物品数据值, 默认 0。

        Returns:
            :class:`CommandResponse` 对象。
        """
        cmd = f"replaceitem {target} {slot} {item} {count} {data}"
        return await self.send(cmd)

    # ------------------------------------------------------------------
    # 传送命令
    # ------------------------------------------------------------------

    async def teleport(
        self,
        target: str,
        x: float,
        y: float,
        z: float,
    ) -> CommandResponse:
        """传送目标到指定坐标。

        Args:
            target: 传送目标选择器 (如 ``"@s"``、``"@a"``、``"Steve"``)。
            x, y, z: 目标坐标。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.send(f"tp {target} {x} {y} {z}")

    # ------------------------------------------------------------------
    # 消息与标题命令
    # ------------------------------------------------------------------

    async def say(self, message: str) -> CommandResponse:
        """广播消息给所有玩家。

        消息会以 ``[发送者] message`` 的格式显示在聊天框中。

        Args:
            message: 要广播的消息内容。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.send(f"say {message}")

    async def tell(self, target: str, message: str) -> CommandResponse:
        """向指定玩家发送私聊消息。

        Args:
            target: 目标玩家选择器 (如 ``"@s"``、``"Steve"``)。
            message: 私聊消息内容。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.send(f"tell {target} {message}")

    async def title(
        self,
        target: str,
        title_type: str,
        text: str,
    ) -> CommandResponse:
        """在目标屏幕上显示标题文本。

        Args:
            target: 目标玩家选择器 (如 ``"@a"``)。
            title_type: 标题类型, 可选:
                - ``"title"``: 主标题 (屏幕中央大字)
                - ``"subtitle"``: 副标题 (主标题下方小字)
                - ``"actionbar"``: 动作栏 (物品栏上方提示)
            text: 要显示的文本内容。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.send(f"title {target} {title_type} {text}")

    # ------------------------------------------------------------------
    # 游戏规则命令
    # ------------------------------------------------------------------

    async def gamerule(self, rule: str, value: Any) -> CommandResponse:
        """设置游戏规则。

        Args:
            rule: 规则名称 (如 ``"doDaylightCycle"``、``"pvp"``、
                ``"mobGriefing"`` 等)。
            value: 规则值, 布尔值会转换为 ``"true"``/``"false"``。

        Returns:
            :class:`CommandResponse` 对象。
        """
        if isinstance(value, bool):
            value_str = "true" if value else "false"
        else:
            value_str = str(value)
        return await self.send(f"gamerule {rule} {value_str}")

    # ------------------------------------------------------------------
    # 结构操作命令
    # ------------------------------------------------------------------

    async def structure_load(
        self,
        name: str,
        x: int,
        y: int,
        z: int,
        rotation: str = "0_degrees",
        mirror: str = "none",
        include_entities: bool = True,
    ) -> CommandResponse:
        """加载已保存的结构到指定坐标。

        Args:
            name: 结构文件名 (无需扩展名)。
            x, y, z: 加载目标坐标 (结构原点放置位置)。
            rotation: 旋转角度, 可选 ``"0_degrees"``、``"90_degrees"``
                ``"180_degrees"``、``"270_degrees"``。默认 ``"0_degrees"``。
            mirror: 镜像方式, 可选 ``"none"``、``"x"``、``"z"``、``"xz"``。
                默认 ``"none"``。
            include_entities: 是否包含实体, 默认 True。

        Returns:
            :class:`CommandResponse` 对象。
        """
        cmd = f'structure load "{name}" {x} {y} {z}'
        cmd += f" {rotation} {mirror}"
        cmd += f" {'true' if include_entities else 'false'}"
        return await self.send(cmd)

    async def structure_save(
        self,
        name: str,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
        include_entities: bool = True,
        include_blocks: bool = True,
    ) -> CommandResponse:
        """保存指定区域的方块为结构文件。

        Args:
            name: 结构文件名 (无需扩展名)。
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。
            include_entities: 是否包含实体, 默认 True。
            include_blocks: 是否包含方块, 默认 True。

        Returns:
            :class:`CommandResponse` 对象。
        """
        cmd = f'structure save "{name}" {x1} {y1} {z1} {x2} {y2} {z2}'
        cmd += f" {'true' if include_entities else 'false'}"
        cmd += f" {'true' if include_blocks else 'false'}"
        return await self.send(cmd)

    async def structure_delete(self, name: str) -> CommandResponse:
        """删除已保存的结构文件。

        Args:
            name: 结构文件名 (无需扩展名)。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.send(f'structure delete "{name}"')

    # ------------------------------------------------------------------
    # 常加载区域命令
    # ------------------------------------------------------------------

    async def tickingarea_add(
        self,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
        name: str = "",
    ) -> CommandResponse:
        """添加常加载区域。

        常加载区域内的方块即使没有玩家附近也会持续更新 (红石、农作物生长等)。

        Args:
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。
            name: 区域名称 (可选, 用于后续引用和移除)。

        Returns:
            :class:`CommandResponse` 对象。
        """
        cmd = f"tickingarea add {x1} {y1} {z1} {x2} {y2} {z2}"
        if name:
            cmd += f' "{name}"'
        return await self.send(cmd)

    async def tickingarea_remove(self, name: str) -> CommandResponse:
        """移除指定名称的常加载区域。

        Args:
            name: 要移除的区域名称。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.send(f'tickingarea remove "{name}"')


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    "CommandResponse",
    "CommandManager",
]
