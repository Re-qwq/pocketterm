"""Minecraft Bedrock 方块状态和操作。

本模块提供方块状态的表示、转换和高级操作封装。通过 :class:`BlockState`
可以方便地描述一个方块 (名称 + 状态属性), 通过 :class:`BlockManager`
可以执行设置、填充、克隆、备份恢复等批量方块操作。

逆向来源:
    - NovaBuilder ``game_control/game_interface/setblock.go``
    - neomega ``neomega/blocks/convertor/ToNEMCConvertor``
    - bedrock-world-operator ``chunk.blockPaletteEncoding``

基本用法::

    from app.protocol.connection import BedrockClient
    from app.protocol.commands import CommandManager
    from app.protocol.blocks import BlockState, BlockManager

    client = BedrockClient(sauth_json="...", device_fingerprint={...})
    await client.connect("example.com", 19132)

    cmd = CommandManager(client)
    blocks = BlockManager(cmd)

    # 设置花岗岩方块
    granite = BlockState(name="minecraft:stone", states={"stone_type": "granite"})
    await blocks.set_block(0, 64, 0, granite)

    # 清空区域 (填充空气)
    await blocks.clear_area(0, 60, 0, 10, 70, 10)

    # 备份并恢复区域
    await blocks.backup_region("my_backup", 0, 60, 0, 10, 70, 10)
    await blocks.restore_region("my_backup", 20, 60, 20)

    await client.disconnect()
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .nbt import marshal_network, parse_snbt, unmarshal_network
from .commands import CommandManager, CommandResponse

logger = logging.getLogger("pocketterm.blocks")


# ======================================================================
# 方块状态
# ======================================================================


@dataclass
class BlockState:
    """方块状态描述。

    一个方块状态由方块名称和一组状态属性组成。状态属性是键值对,
    键为字符串 (如 ``"stone_type"``), 值为字符串或整数
    (如 ``"granite"`` 或 ``0``)。

    Attributes:
        name: 方块名称 (如 ``"minecraft:stone"``、``"minecraft:dirt"``)。
        states: 状态属性字典 (如 ``{"stone_type": "granite"}``),
            默认为空字典 (表示默认状态)。
    """

    name: str
    states: dict[str, Any] = field(default_factory=dict)

    def to_snbt(self) -> str:
        """转换为 SNBT (Stringified NBT) 复合标签字符串。

        生成的格式为 NBT 复合标签, 可通过 :meth:`from_snbt` 反向解析::

            {"name":"minecraft:stone","states":{"stone_type":"granite"}}

        无状态时::

            {"name":"minecraft:stone"}

        Returns:
            SNBT 格式的字符串。
        """
        if not self.states:
            return f'{{"name":"{self.name}"}}'
        states_str = ",".join(
            f'"{k}":{json.dumps(v)}' for k, v in self.states.items()
        )
        return f'{{"name":"{self.name}","states":{{{states_str}}}}}'

    def to_command_str(self) -> str:
        """转换为命令行参数字符串。

        生成的格式用于 Bedrock 命令中方块参数的指定::

            minecraft:stone {"stone_type":"granite"}

        无状态时仅返回方块名称::

            minecraft:stone

        Returns:
            命令行格式的字符串 (方块名 + 空格 + 状态 JSON)。
        """
        if not self.states:
            return self.name
        states_json = json.dumps(self.states)
        return f"{self.name} {states_json}"

    def to_states_json(self) -> str:
        """返回方块状态的 JSON 字符串 (不含方块名称)。

        用于命令中单独传递状态参数::

            {"stone_type":"granite"}

        Returns:
            状态 JSON 字符串。无状态时返回空字符串。
        """
        if not self.states:
            return ""
        return json.dumps(self.states)

    @classmethod
    def from_snbt(cls, snbt: str) -> BlockState:
        """从 SNBT 字符串解析方块状态。

        支持以下格式:
            - NBT 复合标签: ``{"name":"minecraft:stone","states":{...}}``
            - 带引号的方块名: ``"minecraft:stone"``
            - 不带引号的方块名: ``minecraft:stone``

        注意: 不带引号的方块名 (如 ``minecraft:stone``) 由于包含冒号,
        无法通过标准 SNBT 解析器处理, 此方法会直接将其作为方块名。

        Args:
            snbt: SNBT 格式的字符串。

        Returns:
            解析后的 :class:`BlockState` 对象。

        Raises:
            ValueError: SNBT 解析失败或格式不合法。
        """
        text = snbt.strip()

        # 纯文本方块名 (含冒号, SNBT 解析器无法处理) 直接使用
        if not text.startswith("{") and not text.startswith('"') and not text.startswith("'"):
            return cls(name=text)

        try:
            data = parse_snbt(text)
        except Exception as exc:
            raise ValueError(f"SNBT 解析失败: {snbt!r} -> {exc}") from exc

        if isinstance(data, dict):
            name = str(data.get("name", ""))
            raw_states = data.get("states", {})
            if isinstance(raw_states, dict):
                # 将 NBT 包装类型 (Int, Byte 等) 转换为原生 Python 类型
                states = {str(k): _to_native(v) for k, v in raw_states.items()}
            else:
                states = {}
            return cls(name=name, states=states)
        elif isinstance(data, str):
            return cls(name=data)
        else:
            return cls(name=str(data))

    def __repr__(self) -> str:
        if self.states:
            states_str = ", ".join(f"{k}={v!r}" for k, v in self.states.items())
            return f"BlockState({self.name!r}, {{{states_str}}})"
        return f"BlockState({self.name!r})"


def _to_native(value: Any) -> Any:
    """将 NBT 包装类型 (Byte, Short, Int, Long, Float, Double) 转换为原生 Python 类型。

    Args:
        value: 可能是 NBT 包装类型或原生类型的值。

    Returns:
        转换后的原生 Python 值 (int, float, str, bool)。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, str):
        return str(value)
    return value


# ======================================================================
# Schematic 方块 ID 映射表
# ======================================================================

#: 默认映射表路径 (相对于本模块: backend/data/block_mapping.json)
_BLOCK_MAPPING_PATH = Path(__file__).parent.parent.parent / "data" / "block_mapping.json"


def _parse_schematic_entry(raw: str) -> tuple[str, dict[str, Any]]:
    """解析 schematic 映射表中的方块条目字符串。

    映射表中的值可能包含方块状态, 格式为::

        stone
        dirt ["dirt_type"="normal"]
        dispenser ["triggered_bit"=false,"facing_direction"=0]

    Args:
        raw: 映射表中的原始字符串 (不含 ``minecraft:`` 前缀)。

    Returns:
        ``(block_name, states)`` 二元组, ``block_name`` 为方块名 (如
        ``"dirt"``), ``states`` 为状态字典 (如
        ``{"dirt_type": "normal"}``)。无状态时 ``states`` 为空字典。
    """
    text = raw.strip()
    bracket_idx = text.find("[")

    # 无状态部分
    if bracket_idx == -1:
        return text, {}

    name = text[:bracket_idx].strip()
    close_idx = text.rfind("]")
    if close_idx == -1 or close_idx <= bracket_idx:
        return name, {}

    states_str = text[bracket_idx + 1:close_idx]
    states: dict[str, Any] = {}
    for pair in states_str.split(","):
        pair = pair.strip()
        if not pair or "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip().strip('"')
        value = value.strip()
        # 解析值类型: 布尔 / 带引号字符串 / 整数 / 其他字符串
        if value == "true":
            states[key] = True
        elif value == "false":
            states[key] = False
        elif len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            states[key] = value[1:-1]
        else:
            try:
                states[key] = int(value)
            except ValueError:
                states[key] = value
    return name, states


class SchematicBlockMapping:
    """Schematic 方块 ID 映射表。

    将旧版 schematic 格式的数字 ID + 数据值 (Java 1.12 及更早) 映射到
    新版 Bedrock 方块名 + 状态。映射数据从
    :data:`_BLOCK_MAPPING_PATH` 指向的 JSON 文件加载。

    JSON 文件结构::

        {
          "0": {"0": "air"},
          "1": {"0": "stone", "1": "granite", ...},
          "3": {"0": "dirt [\\"dirt_type\\"=\\"normal\\"]", ...},
          ...
        }

    用于导入 ``.schematic`` 文件时转换方块 ID。

    Example::

        mapping = SchematicBlockMapping()
        block = mapping.resolve_to_block_state(1, 1)
        # -> BlockState("minecraft:granite")
        block = mapping.resolve_to_block_state(3, 0)
        # -> BlockState("minecraft:dirt", {"dirt_type": "normal"})
    """

    def __init__(self, mapping_path: Optional[Path] = None) -> None:
        """初始化并加载映射表。

        Args:
            mapping_path: 映射表 JSON 文件路径。为 ``None`` 时使用
                :data:`_BLOCK_MAPPING_PATH` 默认路径。
        """
        self._mapping: dict[str, dict[str, str]] = {}
        self._load(mapping_path or _BLOCK_MAPPING_PATH)

    def _load(self, path: Path) -> None:
        """加载映射表文件。

        Args:
            path: 映射表 JSON 文件路径。
        """
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    self._mapping = json.load(f)
                logger.info(
                    "已加载 schematic 方块映射表: %d 个方块ID (%s)",
                    len(self._mapping), path,
                )
            else:
                logger.warning("schematic 方块映射表不存在: %s", path)
        except Exception as e:
            logger.error("加载 schematic 方块映射表失败: %s", e)
            self._mapping = {}

    def _get_raw(self, block_id: int, data_value: int = 0) -> str:
        """获取原始映射字符串 (不含 ``minecraft:`` 前缀)。

        Args:
            block_id: schematic 中的方块数字 ID。
            data_value: 方块数据值 (旧版 Minecraft 的 metadata)。

        Returns:
            原始映射字符串 (如 ``"stone"`` 或
            ``'dirt ["dirt_type"="normal"]'``)。未找到返回 ``"air"``。
        """
        id_str = str(block_id)
        if id_str not in self._mapping:
            logger.debug("未知 schematic 方块ID: %d", block_id)
            return "air"

        states = self._mapping[id_str]
        dv_str = str(data_value)
        if dv_str in states:
            return states[dv_str]

        # 回退到数据值 0
        if "0" in states:
            return states["0"]

        return "air"

    def resolve(self, block_id: int, data_value: int = 0) -> str:
        """将 schematic 的数字 ID + 数据值解析为方块名。

        注意: 返回的是方块名 (不含状态), 即使原始映射包含状态字符串,
        也只返回 ``[`` 之前的部分。如需获取完整状态, 请使用
        :meth:`resolve_to_block_state`。

        Args:
            block_id: schematic 中的方块数字 ID。
            data_value: 方块数据值 (旧版 Minecraft 的 metadata)。

        Returns:
            方块名 (如 ``"minecraft:stone"``), 未找到返回
            ``"minecraft:air"``。
        """
        raw = self._get_raw(block_id, data_value)
        name, _ = _parse_schematic_entry(raw)
        return f"minecraft:{name}"

    def resolve_to_block_state(
        self, block_id: int, data_value: int = 0
    ) -> "BlockState":
        """将 schematic 的数字 ID + 数据值解析为 :class:`BlockState` 对象。

        会解析映射表中内嵌的方块状态字符串 (如
        ``'dirt ["dirt_type"="normal"]'``), 生成带状态的 BlockState。

        Args:
            block_id: schematic 中的方块数字 ID。
            data_value: 方块数据值 (旧版 Minecraft 的 metadata)。

        Returns:
            对应的 :class:`BlockState` 对象。未找到返回
            ``BlockState("minecraft:air")``。
        """
        raw = self._get_raw(block_id, data_value)
        name, states = _parse_schematic_entry(raw)
        return BlockState(name=f"minecraft:{name}", states=states)


#: 全局映射表实例 (延迟加载)
_mapping_instance: Optional[SchematicBlockMapping] = None


def get_block_mapping() -> SchematicBlockMapping:
    """获取全局 schematic 方块映射表实例 (单例, 延迟加载)。

    Returns:
        :class:`SchematicBlockMapping` 全局实例。
    """
    global _mapping_instance
    if _mapping_instance is None:
        _mapping_instance = SchematicBlockMapping()
    return _mapping_instance


# ======================================================================
# 方块管理器
# ======================================================================


class BlockManager:
    """方块管理器 — 高级方块操作封装。

    本类通过 :class:`CommandManager` 发送方块相关命令, 提供:
        - 设置/填充/获取方块 (:meth:`set_block`, :meth:`fill_blocks`, :meth:`get_block`)
        - 区域备份与恢复 (:meth:`backup_region`, :meth:`restore_region`)
        - 区域清空 (:meth:`clear_area`)
        - 区域克隆 (:meth:`clone_area`)
        - NBT 方块状态编解码 (:meth:`parse_block_states`, :meth:`encode_block_states`)

    Args:
        cmd: :class:`CommandManager` 实例。

    Example::

        blocks = BlockManager(cmd)
        await blocks.set_block(0, 64, 0, BlockManager.STONE)
        await blocks.fill_blocks(0, 60, 0, 5, 65, 5, BlockManager.GLASS)
    """

    def __init__(self, cmd: CommandManager) -> None:
        """初始化方块管理器。

        Args:
            cmd: :class:`CommandManager` 实例, 用于发送底层命令。
        """
        self.cmd: CommandManager = cmd
        # 方块运行时 ID 映射 (从服务器获取, 暂未使用)
        self._runtime_id_map: dict[str, int] = {}
        self._name_map: dict[int, str] = {}

    # ------------------------------------------------------------------
    # 基本方块操作
    # ------------------------------------------------------------------

    async def set_block(
        self,
        x: int,
        y: int,
        z: int,
        block: BlockState,
        mode: str = "replace",
    ) -> CommandResponse:
        """设置指定坐标的方块。

        Args:
            x, y, z: 方块坐标。
            block: :class:`BlockState` 对象, 描述要设置的方块。
            mode: 放置模式 (``"replace"``/``"keep"``/``"destroy"``),
                默认 ``"replace"``。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.cmd.setblock(
            x, y, z, block.name,
            block.to_states_json(),
            mode,
        )

    async def fill_blocks(
        self,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
        block: BlockState,
        mode: str = "replace",
    ) -> CommandResponse:
        """用指定方块填充一个长方体区域。

        Args:
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。
            block: :class:`BlockState` 对象, 描述要填充的方块。
            mode: 填充模式 (``"replace"``/``"keep"``/``"destroy"``
                /``"hollow"``/``"outline"``), 默认 ``"replace"``。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.cmd.fill(
            x1, y1, z1, x2, y2, z2, block.name,
            block.to_states_json(),
            mode,
        )

    async def get_block(self, x: int, y: int, z: int) -> Optional[BlockState]:
        """获取指定坐标的方块状态。

        发送 ``getblock`` 命令并解析服务器返回的方块信息。

        Args:
            x, y, z: 方块坐标。

        Returns:
            解析后的 :class:`BlockState` 对象。如果命令失败或解析失败,
            返回 ``None``。
        """
        resp = await self.cmd.getblock(x, y, z)
        if not resp.success or not resp.output:
            return None

        try:
            text = resp.output.strip()
            # 响应格式可能是:
            #   1. NBT 复合: {"name":"minecraft:stone","states":{...}}
            #   2. 带引号方块名: "minecraft:stone"
            #   3. 纯方块名: minecraft:stone
            #   4. 带状态的方块名: minecraft:stone[stone_type=granite]
            if text.startswith("{") or text.startswith('"'):
                return BlockState.from_snbt(text)
            else:
                # 尝试解析 "minecraft:stone[stone_type=granite]" 格式
                bracket_idx = text.find("[")
                if bracket_idx != -1:
                    name = text[:bracket_idx]
                    return BlockState(name=name)
                return BlockState(name=text)
        except (ValueError, IndexError) as exc:
            logger.warning("解析方块响应失败: %r -> %s", resp.output, exc)
            return None

    # ------------------------------------------------------------------
    # 区域操作
    # ------------------------------------------------------------------

    async def backup_region(
        self,
        name: str,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
    ) -> CommandResponse:
        """备份一个区域的方块为结构文件。

        使用 ``structure save`` 命令将指定区域的方块保存为命名结构。

        Args:
            name: 结构文件名 (无需扩展名)。
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.cmd.structure_save(name, x1, y1, z1, x2, y2, z2)

    async def restore_region(
        self,
        name: str,
        x: int,
        y: int,
        z: int,
        rotation: str = "0_degrees",
        mirror: str = "none",
    ) -> CommandResponse:
        """从结构文件恢复区域到指定坐标。

        使用 ``structure load`` 命令加载之前备份的结构。

        Args:
            name: 结构文件名 (无需扩展名)。
            x, y, z: 加载目标坐标 (结构原点放置位置)。
            rotation: 旋转角度 (``"0_degrees"``/``"90_degrees"``
                /``"180_degrees"``/``"270_degrees"``), 默认 ``"0_degrees"``。
            mirror: 镜像方式 (``"none"``/``"x"``/``"z"``/``"xz"``),
                默认 ``"none"``。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.cmd.structure_load(name, x, y, z, rotation, mirror)

    async def delete_backup(self, name: str) -> CommandResponse:
        """删除已保存的结构文件。

        Args:
            name: 要删除的结构文件名。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.cmd.structure_delete(name)

    async def clear_area(
        self,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
    ) -> CommandResponse:
        """清空指定区域 (用空气方块填充)。

        Args:
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。

        Returns:
            :class:`CommandResponse` 对象。
        """
        air = BlockState(name="minecraft:air")
        return await self.fill_blocks(x1, y1, z1, x2, y2, z2, air, "replace")

    async def clone_area(
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
    ) -> CommandResponse:
        """克隆 (复制) 一个区域到目标位置。

        Args:
            x1, y1, z1: 源区域起始角坐标。
            x2, y2, z2: 源区域结束角坐标。
            x, y, z: 目标区域起始角坐标。

        Returns:
            :class:`CommandResponse` 对象。
        """
        return await self.cmd.clone(x1, y1, z1, x2, y2, z2, x, y, z)

    # ------------------------------------------------------------------
    # NBT 方块状态编解码
    # ------------------------------------------------------------------

    def parse_block_states(self, nbt_data: bytes) -> Any:
        """解析方块状态 NBT 数据 (网络小端序)。

        使用 :func:`app.protocol.nbt.unmarshal_network` 解码网络 NBT 字节串。

        Args:
            nbt_data: 网络 NBT 编码的字节串。

        Returns:
            解码后的 Python 值 (通常是 dict)。
        """
        return unmarshal_network(nbt_data)

    def encode_block_states(self, states: dict) -> bytes:
        """编码方块状态为 NBT 字节串 (网络小端序)。

        使用 :func:`app.protocol.nbt.marshal_network` 编码为网络 NBT 格式。

        Args:
            states: 要编码的方块状态字典。

        Returns:
            网络 NBT 编码的字节串。
        """
        return marshal_network(states)

    # ------------------------------------------------------------------
    # 常用方块预设
    # ------------------------------------------------------------------

    # 基础方块
    STONE: BlockState = BlockState(name="minecraft:stone")
    GRANITE: BlockState = BlockState(
        name="minecraft:stone", states={"stone_type": "granite"}
    )
    COBBLESTONE: BlockState = BlockState(name="minecraft:cobblestone")
    DIRT: BlockState = BlockState(name="minecraft:dirt")
    GRASS: BlockState = BlockState(name="minecraft:grass")
    AIR: BlockState = BlockState(name="minecraft:air")
    WATER: BlockState = BlockState(name="minecraft:water")
    LAVA: BlockState = BlockState(name="minecraft:lava")
    BEDROCK: BlockState = BlockState(name="minecraft:bedrock")

    # 木材类
    OAK_LOG: BlockState = BlockState(
        name="minecraft:log", states={"old_log_type": "oak"}
    )
    OAK_PLANKS: BlockState = BlockState(
        name="minecraft:planks", states={"wood_type": "oak"}
    )

    # 装饰类
    GLASS: BlockState = BlockState(name="minecraft:glass")
    QUARTZ_BLOCK: BlockState = BlockState(name="minecraft:quartz_block")


# ======================================================================
# 模块导出
# ======================================================================

__all__ = [
    "BlockState",
    "BlockManager",
    "SchematicBlockMapping",
    "get_block_mapping",
]
