"""PhoenixBuilder NBT 容器处理系统 — Python 翻译版

本模块是 PhoenixBuilder Go 源码中 ``blockNBT`` 包的完整 Python 翻译,
用于在 PocketTerm 中处理带有 NBT 数据的方块实体 (容器、命令方块、告示牌等)。

翻译来源:
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Container/define.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Container/decode.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Container/writeDatas.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Container/main.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Global/globalStruct.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Global/supportBlocksPool.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/API/replaceitemToContainer.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/CommandBlock/define.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/CommandBlock/decode.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/CommandBlock/writeDatas.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/CommandBlock/main.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Sign/define.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Sign/decode.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/Sign/writeDatas.go
    - PhoenixBuilder/fastbuilder/bdump/blockNBT/blockNBT/main.go (主分发器)

核心架构:
    1. SupportBlocksPool / SupportContainerPool — 全局常量映射表
    2. GeneralBlock / ContainerItem / BlockEntityDatas — 数据类 (dataclass)
    3. ContainerDecoder / ContainerWriter — 容器解码与写入
    4. CommandBlockDecoder / CommandBlockWriter — 命令方块解码与写入
    5. SignDecoder / SignWriter — 告示牌解码与写入
    6. NBTBlockDispatcher — 主分发器, 根据方块类型分发到对应处理器

基本用法::

    from app.protocol.phoenix_nbt import NBTBlockDispatcher

    dispatcher = NBTBlockDispatcher(send_command_callback)
    await dispatcher.place_block_with_nbt_data(block_entity_datas)
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable, Optional

logger = logging.getLogger("pocketterm.protocol.phoenix_nbt")

# ======================================================================
# 1. SupportBlocksPool — 方块名到类型的映射表
#
# 来源: Global/supportBlocksPool.go (行 8-74)
# ======================================================================

#: 描述现阶段已经支持了的方块实体。
#:
#: 键 (key) 代表方块名 (不含命名空间、全小写),
#: 值 (value) 代表该方块应归属的类型: ``"CommandBlock"`` / ``"Container"`` / ``"Sign"``
SupportBlocksPool: dict[str, str] = {
    # 命令方块 (来源: supportBlocksPool.go 行 9-12)
    "command_block": "CommandBlock",
    "chain_command_block": "CommandBlock",
    "repeating_command_block": "CommandBlock",
    # 容器 — 可被 replaceitem 命令生效 (来源: supportBlocksPool.go 行 13-31)
    "blast_furnace": "Container",
    "lit_blast_furnace": "Container",
    "smoker": "Container",
    "lit_smoker": "Container",
    "furnace": "Container",
    "lit_furnace": "Container",
    "chest": "Container",
    "barrel": "Container",
    "trapped_chest": "Container",
    "hopper": "Container",
    "dispenser": "Container",
    "dropper": "Container",
    "cauldron": "Container",
    "lava_cauldron": "Container",
    "jukebox": "Container",
    "brewing_stand": "Container",
    "undyed_shulker_box": "Container",
    "shulker_box": "Container",
    "lectern": "Container",
    # 告示牌 (来源: supportBlocksPool.go 行 33-73)
    "standing_sign": "Sign",
    "spruce_standing_sign": "Sign",
    "birch_standing_sign": "Sign",
    "jungle_standing_sign": "Sign",
    "acacia_standing_sign": "Sign",
    "darkoak_standing_sign": "Sign",
    "mangrove_standing_sign": "Sign",
    "bamboo_standing_sign": "Sign",
    "crimson_standing_sign": "Sign",
    "warped_standing_sign": "Sign",
    "wall_sign": "Sign",
    "spruce_wall_sign": "Sign",
    "birch_wall_sign": "Sign",
    "jungle_wall_sign": "Sign",
    "acacia_wall_sign": "Sign",
    "darkoak_wall_sign": "Sign",
    "mangrove_wall_sign": "Sign",
    "bamboo_wall_sign": "Sign",
    "crimson_wall_sign": "Sign",
    "warped_wall_sign": "Sign",
    "sign": "Sign",
    "spruce_sign": "Sign",
    "birch_sign": "Sign",
    "jungle_sign": "Sign",
    "acacia_sign": "Sign",
    "darkoak_sign": "Sign",
    "mangrove_sign": "Sign",
    "bamboo_sign": "Sign",
    "crimson_sign": "Sign",
    "warped_sign": "Sign",
    "oak_hanging_sign": "Sign",
    "spruce_hanging_sign": "Sign",
    "birch_hanging_sign": "Sign",
    "jungle_hanging_sign": "Sign",
    "acacia_hanging_sign": "Sign",
    "dark_oak_hanging_sign": "Sign",
    "mangrove_hanging_sign": "Sign",
    "bamboo_hanging_sign": "Sign",
    "crimson_hanging_sign": "Sign",
    "warped_hanging_sign": "Sign",
}

# ======================================================================
# 2. SupportContainerPool — 容器名到 NBT 键的映射表
#
# 来源: Container/define.go (行 27-45)
# ======================================================================

#: 描述可被 replaceitem 命令生效的容器及其对应的 NBT 键。
#:
#: 键 (key) 代表容器的方块名,
#: 值 (value) 代表此容器放置物品所使用的复合标签或列表名称:
#: ``"Items"`` (大多数容器), ``"book"`` (讲台), ``"RecordItem"`` (唱片机)
SupportContainerPool: dict[str, str] = {
    "blast_furnace": "Items",
    "lit_blast_furnace": "Items",
    "smoker": "Items",
    "lit_smoker": "Items",
    "furnace": "Items",
    "lit_furnace": "Items",
    "chest": "Items",
    "barrel": "Items",
    "trapped_chest": "Items",
    "lectern": "book",
    "hopper": "Items",
    "dispenser": "Items",
    "dropper": "Items",
    "jukebox": "RecordItem",
    "brewing_stand": "Items",
    "undyed_shulker_box": "Items",
    "shulker_box": "Items",
}

# 未被支持的容器错误信息 (来源: Container/define.go 行 48-49)
_NOT_A_SUPPORTED_CONTAINER: str = "Not a supported container"
_ERR_NOT_A_SUPPORTED_CONTAINER: str = "replaceNBTMapToContainerList: Not a supported container"

# 用于 decode 中的 NBT key 名称 (来源: Container/define.go 行 52)
_KEY_NAME: str = "datas"

# 命令方块类型常量 (来源: CommandBlock/writeDatas.go 行 10-11, 37-41)
# 在 Go 中使用 packet.CommandBlock* 常量; Python 中使用整数代替
_COMMAND_BLOCK_IMPULSE: int = 0      # 脉冲命令方块 (command_block)
_COMMAND_BLOCK_CHAIN: int = 1        # 连锁命令方块 (chain_command_block)
_COMMAND_BLOCK_REPEATING: int = 2    # 循环命令方块 (repeating_command_block)


# ======================================================================
# 3. 数据类 (dataclass)
#
# 来源: Global/globalStruct.go (行 8-30), Container/define.go (行 9-20)
#       CommandBlock/define.go (行 6-20), Sign/define.go (行 6-17)
# ======================================================================

@dataclass
class GeneralBlock:
    """通用型方块的数据结构。

    来源: Global/globalStruct.go 行 8-13

    Attributes:
        Name: 方块名称 (不含命名空间, 全小写)。
        States: 方块状态 (dict[str, Any])。
        NBT: 当前方块所携带的 NBT 数据 (dict[str, Any])。
    """
    Name: str
    States: dict[str, Any] = field(default_factory=dict)
    NBT: dict[str, Any] = field(default_factory=dict)


@dataclass
class ContainerItem:
    """单个容器物品的数据结构。

    来源: Container/define.go 行 9-14

    Attributes:
        Name: 物品名称 (不含 "minecraft:" 命名空间, 全小写)。
        Count: 物品数量 (uint8, 范围 0-255)。
        Damage: 物品数据值/附加值 (uint16, 范围 0-65535)。
        Slot: 物品所在槽位 (uint8, 范围 0-255)。
    """
    Name: str = ""
    Count: int = 0
    Damage: int = 0
    Slot: int = 0


@dataclass
class CommandBlockData:
    """命令方块 NBT 数据在被解析后的结构。

    来源: CommandBlock/define.go 行 6-15

    Attributes:
        Command: 命令方块中存储的命令字符串。
        CustomName: 命令方块的自定义名称。
        LastOutput: 命令方块的上次输出。
        TickDelay: 命令方块执行的延迟 (tick 数)。
        ExecuteOnFirstTick: 是否在首个 tick 执行。
        TrackOutput: 是否追踪输出。
        ConditionalMode: 是否为条件模式。
        Auto: 是否需要红石激活 (auto 为 True 表示不需要红石)。
    """
    Command: str = ""
    CustomName: str = ""
    LastOutput: str = ""
    TickDelay: int = 0
    ExecuteOnFirstTick: bool = True
    TrackOutput: bool = True
    ConditionalMode: bool = False
    Auto: bool = True


@dataclass
class SignData:
    """告示牌 NBT 数据在被解析后的结构。

    来源: Sign/define.go 行 6-12

    Attributes:
        TextOwner: 告示牌文本的所有者。
        IgnoreLighting: 是否忽略光照 (byte)。
        SignTextColor: 告示牌文本颜色 (int32)。
        TextIgnoreLegacyBugResolved: 是否忽略旧版文本 bug (byte)。
        Text: 告示牌文本内容。
    """
    TextOwner: str = ""
    IgnoreLighting: int = 0
    SignTextColor: int = 0
    TextIgnoreLegacyBugResolved: int = 0
    Text: str = ""


@dataclass
class BlockEntityDatas:
    """包装每个方块实体的完整数据结构。

    来源: Global/globalStruct.go 行 25-30

    这是一个聚合结构, 包含了方块实体的所有信息:
    - Block: 通用方块数据 (名称、状态、NBT)
    - Position: 方块绝对坐标 (x, y, z)
    - Type: 方块类型 (CommandBlock / Container / Sign / "")
    - StatesString: 字符串形式的方块状态, 用于放置方块时使用
    - FastMode: 是否为快速模式 (若为 True, 跳过 NBT 写入)
    """
    Block: GeneralBlock = field(default_factory=lambda: GeneralBlock("", {}, {}))
    Position: tuple[int, int, int] = (0, 0, 0)
    Type: str = ""
    StatesString: str = ""
    FastMode: bool = False


# ======================================================================
# 4. 可注入的方块映射函数
#
# 这些函数在 Go 源码中由 chunk 包提供 (mirror/chunk/mapping.go),
# 依赖于编译时嵌入的 .gob.brotli 映射数据。
# 在 Python 中, 这些函数需要外部注入或使用默认实现。
# ======================================================================

#: 类型别名: 将 (方块名, 属性表) 映射到 runtime ID 的回调
#:
#: 来源: mirror/chunk/mapping.go 行 37
#:
#: Args:
#:     name: 方块名称 (如 "minecraft:stone")
#:     properties: 方块属性 (如 {"stone_type": "granite"})
#: Returns:
#:     (runtime_id, found) 元组
StateToRuntimeID = Callable[[str, dict[str, Any]], tuple[int, bool]]

#: 类型别名: 将 runtime ID 映射到 LegacyBlock 的回调
#:
#: 来源: mirror/chunk/mapping.go 行 35
#:
#: Args:
#:     runtime_id: 标准 runtime ID
#: Returns:
#:     (legacy_block, found) 元组; legacy_block 是一个有 ``Name`` 和 ``Val`` 属性的对象
RuntimeIDToLegacyBlock = Callable[[int], tuple[Any, bool]]


def _default_state_to_runtime_id(name: str, properties: dict[str, Any]) -> tuple[int, bool]:
    """默认的 StateToRuntimeID 实现 (始终返回未找到)。

    当用户未注入实际的映射函数时使用此默认实现。
    来源: mirror/chunk/mapping.go 行 37, 523-529
    """
    return (0, False)


def _default_runtime_id_to_legacy_block(runtime_id: int) -> tuple[Any, bool]:
    """默认的 RuntimeIDToLegacyBlock 实现 (始终返回未找到)。

    当用户未注入实际的映射函数时使用此默认实现。
    来源: mirror/chunk/mapping.go 行 35, 618-624
    """
    return (None, False)


# 全局可注入的映射函数引用
_state_to_runtime_id: StateToRuntimeID = _default_state_to_runtime_id
_runtime_id_to_legacy_block: RuntimeIDToLegacyBlock = _default_runtime_id_to_legacy_block


def set_block_mapping_functions(
    state_to_runtime_id: StateToRuntimeID,
    runtime_id_to_legacy_block: RuntimeIDToLegacyBlock,
) -> None:
    """注入方块映射函数, 用于物品 Damage 值解码的第 4 优先级。

    来源: mirror/chunk/mapping.go 行 37, 35

    在 Go 源码中, 这些函数由 ``chunk.InitMapping()`` 初始化,
    依赖于编译时嵌入的 .gob.brotli 映射数据。
    在 Python 中, 需要在模块加载后手动注入。

    Args:
        state_to_runtime_id: 将 (方块名, 属性) 映射到 runtime ID 的函数。
        runtime_id_to_legacy_block: 将 runtime ID 映射到 LegacyBlock 的函数。
    """
    global _state_to_runtime_id, _runtime_id_to_legacy_block
    _state_to_runtime_id = state_to_runtime_id
    _runtime_id_to_legacy_block = runtime_id_to_legacy_block


# ======================================================================
# 5. ContainerDecoder — 容器 NBT 解码器
#
# 来源: Container/decode.go (行 1-192)
# ======================================================================

class ContainerDecoder:
    """容器 NBT 数据解码器。

    从容器方块的 NBT 数据中提取物品信息, 包括物品名称、数量、
    数据值 (Damage) 和槽位 (Slot)。

    来源: Container/decode.go (行 1-192)
    """

    def __init__(self, block_entity_datas: BlockEntityDatas):
        """初始化容器解码器。

        Args:
            block_entity_datas: 方块实体数据, 包含 NBT 和方块信息。
        """
        self._block_entity_datas = block_entity_datas
        self._items: list[ContainerItem] = []

    @property
    def items(self) -> list[ContainerItem]:
        """获取解码后的容器物品列表。"""
        return self._items

    def _check_if_supported_container(self) -> str:
        """检查当前方块是否为已被支持的容器。

        来源: Container/decode.go 行 14-20

        查询 SupportContainerPool 映射表, 返回对应的 NBT 键名。
        如果不在支持列表中, 返回 ``_NOT_A_SUPPORTED_CONTAINER``。

        Returns:
            容器对应的 NBT 键名 (如 "Items"/"book"/"RecordItem"),
            或 ``_NOT_A_SUPPORTED_CONTAINER`` 表示不支持。
        """
        return SupportContainerPool.get(
            self._block_entity_datas.Block.Name,
            _NOT_A_SUPPORTED_CONTAINER,
        )

    def replace_nbt_map_to_container_list(self) -> None:
        """从 NBT 数据中提取容器物品数据。

        来源: Container/decode.go 行 23-39

        将 ``Block.NBT[key]`` 重组为 ``Block.NBT["datas"]`` 格式,
        其中 key 由 SupportContainerPool 决定。
        对于 唱片机 和 讲台 这类容器, 如果未被放置物品,
        对应的 key 可能不存在, 此时设为空列表, 这并非错误。

        Raises:
            ValueError: 如果容器不被支持 (不在 SupportContainerPool 中)。
        """
        key = self._check_if_supported_container()
        if key == _NOT_A_SUPPORTED_CONTAINER:
            raise ValueError(_ERR_NOT_A_SUPPORTED_CONTAINER)
        # 来源: Container/decode.go 行 29-35
        value = self._block_entity_datas.Block.NBT.get(key)
        if value is None:
            self._block_entity_datas.Block.NBT = {_KEY_NAME: []}
        else:
            self._block_entity_datas.Block.NBT = {_KEY_NAME: value}

    def decode_item_damage(self, container_data: dict[str, Any]) -> int:
        """解码物品的 Damage (数据值/附加值), 实现 4 级优先级链。

        来源: Container/decode.go 行 95-170

        优先级链 (从高到低):
            1. ``containerData["Damage"]`` — 直接 Damage 字段 (int16)
            2. ``containerData["tag"]["Damage"]`` — 物品 tag 中的 Damage (int32), 通常表示工具耐久
            3. ``containerData["Block"]["val"]`` — 方块数据值, 仅限 Netease MC
            4. ``containerData["Block"]["states"]`` — 方块状态转 Legacy val:
               states -> StateToRuntimeID -> RuntimeIDToLegacyBlock -> .Val

        Args:
            container_data: 单个物品的 NBT 复合标签 (dict)。

        Returns:
            物品的数据值 (int, 范围 0-65535)。

        Raises:
            ValueError: 第 4 优先级中状态转换失败时抛出。
        """
        item_data: int = 0

        # 优先级 1: 直接从 containerData["Damage"] 获取 (来源: Container/decode.go 行 95-103)
        if "Damage" in container_data:
            raw = container_data["Damage"]
            if isinstance(raw, int):
                item_data = raw & 0xFFFF

        # 优先级 2: 从 containerData["tag"]["Damage"] 获取 (来源: Container/decode.go 行 105-123)
        # tag 不一定存在, tag 存在时 Damage 也不一定存在
        if "tag" in container_data:
            tag = container_data["tag"]
            if isinstance(tag, dict) and "Damage" in tag:
                raw = tag["Damage"]
                if isinstance(raw, int):
                    item_data = raw & 0xFFFF

        # 优先级 3: 从 containerData["Block"]["val"] 获取 (来源: Container/decode.go 行 124-161)
        # Block 不一定存在, 但如果 Block 存在且 val 存在, 则使用 val
        if "Block" in container_data:
            block = container_data["Block"]
            if isinstance(block, dict):
                if "val" in block:
                    raw = block["val"]
                    if isinstance(raw, int):
                        item_data = raw & 0xFFFF
                else:
                    # 优先级 4: 从 Block["states"] 通过映射获取 (来源: Container/decode.go 行 141-158)
                    if "states" in block:
                        states = block["states"]
                        if isinstance(states, dict):
                            name = container_data.get("Name", "")
                            if isinstance(name, str):
                                name = name.lower().replace("minecraft:", "")
                            runtime_id, found = _state_to_runtime_id(name, states)
                            if not found:
                                # 如果转换失败, 设为 0 (来源: Container/decode.go 行 143-144)
                                item_data = 0
                            else:
                                legacy_block, found = _runtime_id_to_legacy_block(runtime_id)
                                if not found:
                                    # 如果无法获取 LegacyBlock, 设为 0
                                    item_data = 0
                                else:
                                    try:
                                        item_data = legacy_block.Val & 0xFFFF
                                    except AttributeError:
                                        item_data = 0
                        else:
                            item_data = 0
                    else:
                        item_data = 0

        return item_data

    def decode(self) -> list[ContainerItem]:
        """从容器 NBT 数据中提取并解码物品列表。

        来源: Container/decode.go 行 42-192

        执行流程:
            1. 调用 ``replace_nbt_map_to_container_list()`` 重组 NBT
            2. 将 NBT 数据解析为统一的 ``list[dict]`` 格式
            3. 遍历每个物品, 提取 Name/Count/Damage/Slot
            4. 对每个物品调用 ``decode_item_damage()`` 获取最终 Damage 值

        Returns:
            解码后的 ContainerItem 列表。

        Raises:
            ValueError: 当 NBT 数据格式不正确或容器不被支持时。
        """
        self.replace_nbt_map_to_container_list()
        # 来源: Container/decode.go 行 43-49

        raw_data = self._block_entity_datas.Block.NBT.get(_KEY_NAME)
        if raw_data is None:
            return []

        correct: list[dict[str, Any]] = []
        # 来源: Container/decode.go 行 50-59
        if isinstance(raw_data, list):
            correct = raw_data
        elif isinstance(raw_data, dict):
            # 唱片机/讲台: 单个物品, 包装为列表 (来源: Container/decode.go 行 52-56)
            correct = [raw_data]
        else:
            raise ValueError(
                f"Decode: Crashed in Block.NBT[{_KEY_NAME}]; "
                f"Block.NBT = {self._block_entity_datas.Block.NBT!r}"
            )

        self._items = []
        # 来源: Container/decode.go 行 64-188
        for idx, value in enumerate(correct):
            if not isinstance(value, dict):
                raise ValueError(
                    f"Decode: Crashed in correct[{idx}]; "
                    f"correct[{idx}] = {value!r}"
                )

            container_data: dict[str, Any] = value

            # 提取 Count (来源: Container/decode.go 行 75-83)
            if "Count" not in container_data:
                raise ValueError(
                    f"Decode: Crashed in correct[{idx}][\"Count\"]; "
                    f"correct[{idx}] = {container_data!r}"
                )
            count = container_data["Count"]
            if isinstance(count, int):
                count = count & 0xFF
            else:
                count = 0

            # 提取 Name (来源: Container/decode.go 行 85-93)
            if "Name" not in container_data:
                raise ValueError(
                    f"Decode: Crashed in correct[{idx}][\"Name\"]; "
                    f"correct[{idx}] = {container_data!r}"
                )
            name = str(container_data["Name"]).lower().replace("minecraft:", "")

            # 提取 Damage (来源: Container/decode.go 行 95-170)
            item_data = self.decode_item_damage(container_data)

            # 提取 Slot (来源: Container/decode.go 行 171-180)
            slot = 0
            if "Slot" in container_data:
                raw_slot = container_data["Slot"]
                if isinstance(raw_slot, int):
                    slot = raw_slot & 0xFF

            self._items.append(ContainerItem(
                Name=name,
                Count=count,
                Damage=item_data,
                Slot=slot,
            ))

        return self._items


# ======================================================================
# 6. ContainerWriter — 容器写入器
#
# 来源: Container/writeDatas.go (行 1-28)
# ======================================================================

class ContainerWriter:
    """容器方块放置与物品填充写入器。

    来源: Container/writeDatas.go (行 1-28)

    工作流程:
        1. 放置容器方块 (setblock 命令)
        2. 等待 0.2 秒 (方块放置延迟)
        3. 逐个填充物品 (replaceitem 命令), 每个物品间隔 0.1 秒
    """

    #: 方块放置后等待时间 (秒)
    BLOCK_PLACE_DELAY: float = 0.2
    #: 每个物品填充后等待时间 (秒)
    ITEM_FILL_DELAY: float = 0.1

    def __init__(
        self,
        block_entity_datas: BlockEntityDatas,
        items: list[ContainerItem],
        send_command: Callable[..., Awaitable[Any]],
    ):
        """初始化容器写入器。

        Args:
            block_entity_datas: 方块实体数据 (包含位置和方块信息)。
            items: 解码后的容器物品列表。
            send_command: 发送命令的异步回调函数。
        """
        self._block_entity_datas = block_entity_datas
        self._items = items
        self._send_command = send_command

    async def write(self) -> None:
        """放置容器并填充物品。

        来源: Container/writeDatas.go 行 6-28

        执行顺序:
            1. 通过 setblock 命令放置容器方块
            2. 等待 BLOCK_PLACE_DELAY 秒
            3. 对每个物品, 发送 replaceitem 命令并等待 ITEM_FILL_DELAY 秒

        replaceitem 命令格式 (来源: API/replaceitemToContainer.go 行 6-23):
            ``replaceitem block {x} {y} {z} slot.container {slot} {item_name} {count} {damage}``

        Raises:
            Exception: 命令执行失败时抛出。
        """
        pos = self._block_entity_datas.Position
        block_name = self._block_entity_datas.Block.Name
        states_string = self._block_entity_datas.StatesString
        # 来源: Container/writeDatas.go 行 7-11

        # 步骤 1: 放置容器方块
        setblock_cmd = f"setblock {pos[0]} {pos[1]} {pos[2]} {block_name} {states_string}".strip()
        await self._send_command(setblock_cmd)
        await asyncio.sleep(self.BLOCK_PLACE_DELAY)
        # 来源: Container/writeDatas.go 行 12-24

        # 步骤 2: 向容器内填充物品
        for item in self._items:
            replaceitem_cmd = (
                f"replaceitem block {pos[0]} {pos[1]} {pos[2]} "
                f"slot.container {item.Slot} {item.Name} {item.Count} {item.Damage}"
            )
            await self._send_command(replaceitem_cmd)
            await asyncio.sleep(self.ITEM_FILL_DELAY)


# ======================================================================
# 7. CommandBlockDecoder — 命令方块 NBT 解码器
#
# 来源: CommandBlock/decode.go (行 1-113)
# ======================================================================

class CommandBlockDecoder:
    """命令方块 NBT 数据解码器。

    从命令方块方块的 NBT 数据中提取命令、名称、输出、延迟、
    执行模式等字段。

    来源: CommandBlock/decode.go (行 1-113)
    """

    def __init__(self, block_entity_datas: BlockEntityDatas):
        """初始化命令方块解码器。

        Args:
            block_entity_datas: 方块实体数据, 包含 NBT 和方块信息。
        """
        self._block_entity_datas = block_entity_datas
        self._data = CommandBlockData()

    @property
    def data(self) -> CommandBlockData:
        """获取解码后的命令方块数据。"""
        return self._data

    def decode(self) -> CommandBlockData:
        """从 NBT 数据中提取并解码命令方块数据。

        来源: CommandBlock/decode.go 行 6-112

        支持的 NBT 字段:
            - ``Command`` (TAG_String) — 命令字符串
            - ``CustomName`` (TAG_String) — 自定义名称
            - ``LastOutput`` (TAG_String) — 上次输出
            - ``TickDelay`` (TAG_Int) — 延迟 (tick)
            - ``ExecuteOnFirstTick`` (TAG_Byte) — 首 tick 执行
            - ``TrackOutput`` (TAG_Byte) — 追踪输出
            - ``conditionalMode`` (TAG_Byte) — 条件模式
            - ``auto`` (TAG_Byte) — 自动执行 (不需要红石)

        Returns:
            解码后的 CommandBlockData。

        Raises:
            ValueError: 当 NBT 字段类型不匹配时。
        """
        nbt = self._block_entity_datas.Block.NBT
        # 来源: CommandBlock/decode.go 行 7-16

        command = ""
        custom_name = ""
        last_output = ""
        tick_delay = 0
        execute_on_first_tick = True
        track_output = True
        conditional_mode = False
        auto = True

        # Command (来源: CommandBlock/decode.go 行 17-23)
        if "Command" in nbt:
            command = str(nbt["Command"])

        # CustomName (来源: CommandBlock/decode.go 行 25-31)
        if "CustomName" in nbt:
            custom_name = str(nbt["CustomName"])

        # LastOutput (来源: CommandBlock/decode.go 行 33-39)
        if "LastOutput" in nbt:
            last_output = str(nbt["LastOutput"])

        # TickDelay (来源: CommandBlock/decode.go 行 41-47)
        if "TickDelay" in nbt:
            tick_delay = int(nbt["TickDelay"]) if isinstance(nbt["TickDelay"], int) else 0

        # ExecuteOnFirstTick (来源: CommandBlock/decode.go 行 49-60)
        if "ExecuteOnFirstTick" in nbt:
            val = nbt["ExecuteOnFirstTick"]
            if isinstance(val, int):
                execute_on_first_tick = val != 0

        # TrackOutput (来源: CommandBlock/decode.go 行 62-73)
        if "TrackOutput" in nbt:
            val = nbt["TrackOutput"]
            if isinstance(val, int):
                track_output = val != 0

        # conditionalMode (来源: CommandBlock/decode.go 行 75-86)
        if "conditionalMode" in nbt:
            val = nbt["conditionalMode"]
            if isinstance(val, int):
                conditional_mode = val != 0

        # auto (来源: CommandBlock/decode.go 行 88-99)
        if "auto" in nbt:
            val = nbt["auto"]
            if isinstance(val, int):
                auto = val != 0

        self._data = CommandBlockData(
            Command=command,
            CustomName=custom_name,
            LastOutput=last_output,
            TickDelay=tick_delay,
            ExecuteOnFirstTick=execute_on_first_tick,
            TrackOutput=track_output,
            ConditionalMode=conditional_mode,
            Auto=auto,
        )
        # 来源: CommandBlock/decode.go 行 101-111
        return self._data


# ======================================================================
# 8. CommandBlockWriter — 命令方块写入器
#
# 来源: CommandBlock/writeDatas.go (行 1-66)
# ======================================================================

class CommandBlockWriter:
    """命令方块放置与数据写入器。

    来源: CommandBlock/writeDatas.go (行 1-66)

    工作流程:
        1. 放置命令方块 (setblock 命令)
        2. 传送机器人到命令方块位置 (tp 命令)
        3. 通过 CommandBlockUpdate 数据包写入命令方块数据

    注意:
        在 Python 版本中, 由于我们无法直接发送 Minecraft 协议数据包,
        CommandBlockUpdate 数据包需要通过 send_packet 回调发送。
        如果 send_packet 不可用, 将回退到仅放置方块。
    """

    def __init__(
        self,
        block_entity_datas: BlockEntityDatas,
        data: CommandBlockData,
        send_command: Callable[..., Awaitable[Any]],
        send_packet: Optional[Callable[..., Awaitable[Any]]] = None,
        exclude_commands: bool = False,
        invalidate_commands: bool = False,
    ):
        """初始化命令方块写入器。

        Args:
            block_entity_datas: 方块实体数据。
            data: 解码后的命令方块数据。
            send_command: 发送游戏命令的异步回调。
            send_packet: 发送协议数据包的异步回调 (可选, 用于 CommandBlockUpdate)。
            exclude_commands: 是否排除命令数据 (仅放置方块, 不写入命令)。
            invalidate_commands: 是否使命令无效化 (在命令前加 "# ")。
        """
        self._block_entity_datas = block_entity_datas
        self._data = data
        self._send_command = send_command
        self._send_packet = send_packet
        self._exclude_commands = exclude_commands
        self._invalidate_commands = invalidate_commands

    async def write(self, need_to_place_block: bool = True) -> None:
        """放置命令方块并写入命令方块数据。

        来源: CommandBlock/writeDatas.go 行 9-66

        Args:
            need_to_place_block: 是否需要先放置方块 (默认 True)。
        """
        pos = self._block_entity_datas.Position
        block_name = self._block_entity_datas.Block.Name
        states_string = self._block_entity_datas.StatesString
        fast_mode = self._block_entity_datas.FastMode
        # 来源: CommandBlock/writeDatas.go 行 10-11

        # 步骤 1: 放置命令方块 (来源: CommandBlock/writeDatas.go 行 12-26)
        if need_to_place_block:
            if self._exclude_commands or fast_mode:
                # 快速模式: 仅放置方块 (来源: CommandBlock/writeDatas.go 行 13-17)
                setblock_cmd = f"setblock {pos[0]} {pos[1]} {pos[2]} {block_name} {states_string}".strip()
                await self._send_command(setblock_cmd)
            else:
                # 普通模式: 放置方块 (来源: CommandBlock/writeDatas.go 行 19-24)
                setblock_cmd = f"setblock {pos[0]} {pos[1]} {pos[2]} {block_name} {states_string}".strip()
                await self._send_command(setblock_cmd)

        # 步骤 2: 如果不要求写入命令方块数据, 提前返回 (来源: CommandBlock/writeDatas.go 行 28-29)
        if self._exclude_commands:
            return

        # 步骤 3: 传送机器人到命令方块位置 (来源: CommandBlock/writeDatas.go 行 32-35)
        await self._send_command(f"tp {pos[0]} {pos[1]} {pos[2]}")

        # 步骤 4: 确定命令方块类型 (来源: CommandBlock/writeDatas.go 行 37-41)
        mode = _COMMAND_BLOCK_IMPULSE
        if block_name == "chain_command_block":
            mode = _COMMAND_BLOCK_CHAIN
        elif block_name == "repeating_command_block":
            mode = _COMMAND_BLOCK_REPEATING

        # 步骤 5: 命令无效化处理 (来源: CommandBlock/writeDatas.go 行 43-45)
        command = self._data.Command
        if self._invalidate_commands:
            command = "# " + command

        # 步骤 6: 发送 CommandBlockUpdate 数据包 (来源: CommandBlock/writeDatas.go 行 47-59)
        packet_data = {
            "Block": True,
            "Position": {"X": pos[0], "Y": pos[1], "Z": pos[2]},
            "Mode": mode,
            "NeedsRedstone": not self._data.Auto,
            "Conditional": self._data.ConditionalMode,
            "Command": command,
            "LastOutput": self._data.LastOutput,
            "Name": self._data.CustomName,
            "ShouldTrackOutput": self._data.TrackOutput,
            "TickDelay": self._data.TickDelay,
            "ExecuteOnFirstTick": self._data.ExecuteOnFirstTick,
        }

        if self._send_packet is not None:
            await self._send_packet("CommandBlockUpdate", packet_data)
        else:
            logger.warning(
                "CommandBlockWriter: send_packet 回调未提供, "
                "CommandBlockUpdate 数据包未发送; 仅放置了方块"
            )


# ======================================================================
# 9. SignDecoder — 告示牌 NBT 解码器
#
# 来源: Sign/decode.go (行 1-64)
# ======================================================================

class SignDecoder:
    """告示牌 NBT 数据解码器。

    从告示牌方块的 NBT 数据中提取文本、颜色、光照等字段。

    来源: Sign/decode.go (行 1-64)
    """

    def __init__(self, block_entity_datas: BlockEntityDatas):
        """初始化告示牌解码器。

        Args:
            block_entity_datas: 方块实体数据, 包含 NBT 和方块信息。
        """
        self._block_entity_datas = block_entity_datas
        self._data = SignData()

    @property
    def data(self) -> SignData:
        """获取解码后的告示牌数据。"""
        return self._data

    def decode(self) -> SignData:
        """从 NBT 数据中提取并解码告示牌数据。

        来源: Sign/decode.go 行 6-63

        支持的 NBT 字段:
            - ``TextOwner`` (TAG_String) — 文本所有者
            - ``IgnoreLighting`` (TAG_Byte) — 忽略光照
            - ``SignTextColor`` (TAG_Int) — 文本颜色
            - ``TextIgnoreLegacyBugResolved`` (TAG_Byte) — 忽略旧版 bug
            - ``Text`` (TAG_String) — 文本内容

        Returns:
            解码后的 SignData。

        Raises:
            ValueError: 当 NBT 字段类型不匹配时。
        """
        nbt = self._block_entity_datas.Block.NBT
        # 来源: Sign/decode.go 行 7-16

        text_owner = ""
        ignore_lighting = 0
        sign_text_color = 0
        text_ignore_legacy_bug_resolved = 0
        text = ""

        # TextOwner (来源: Sign/decode.go 行 15-21)
        if "TextOwner" in nbt:
            text_owner = str(nbt["TextOwner"])

        # IgnoreLighting (来源: Sign/decode.go 行 23-29)
        if "IgnoreLighting" in nbt:
            val = nbt["IgnoreLighting"]
            if isinstance(val, int):
                ignore_lighting = val & 0xFF

        # SignTextColor (来源: Sign/decode.go 行 30-37)
        if "SignTextColor" in nbt:
            val = nbt["SignTextColor"]
            if isinstance(val, int):
                sign_text_color = val

        # TextIgnoreLegacyBugResolved (来源: Sign/decode.go 行 39-45)
        if "TextIgnoreLegacyBugResolved" in nbt:
            val = nbt["TextIgnoreLegacyBugResolved"]
            if isinstance(val, int):
                text_ignore_legacy_bug_resolved = val & 0xFF

        # Text (来源: Sign/decode.go 行 47-53)
        if "Text" in nbt:
            text = str(nbt["Text"])

        self._data = SignData(
            TextOwner=text_owner,
            IgnoreLighting=ignore_lighting,
            SignTextColor=sign_text_color,
            TextIgnoreLegacyBugResolved=text_ignore_legacy_bug_resolved,
            Text=text,
        )
        # 来源: Sign/decode.go 行 55-62
        return self._data


# ======================================================================
# 10. SignWriter — 告示牌写入器
#
# 来源: Sign/writeDatas.go (行 1-38)
# ======================================================================

class SignWriter:
    """告示牌放置与数据写入器。

    来源: Sign/writeDatas.go (行 1-38)

    工作流程:
        1. 放置告示牌方块 (setblock 命令)
        2. 通过 BlockActorData 数据包写入告示牌 NBT 数据

    注意:
        在 Python 版本中, BlockActorData 数据包需要通过 send_packet 回调发送。
        如果 send_packet 不可用, 将回退到仅放置方块。
    """

    def __init__(
        self,
        block_entity_datas: BlockEntityDatas,
        data: SignData,
        send_command: Callable[..., Awaitable[Any]],
        send_packet: Optional[Callable[..., Awaitable[Any]]] = None,
    ):
        """初始化告示牌写入器。

        Args:
            block_entity_datas: 方块实体数据。
            data: 解码后的告示牌数据。
            send_command: 发送游戏命令的异步回调。
            send_packet: 发送协议数据包的异步回调 (可选, 用于 BlockActorData)。
        """
        self._block_entity_datas = block_entity_datas
        self._data = data
        self._send_command = send_command
        self._send_packet = send_packet

    async def write(self) -> None:
        """放置告示牌并写入告示牌数据。

        来源: Sign/writeDatas.go 行 9-37
        """
        pos = self._block_entity_datas.Position
        block_name = self._block_entity_datas.Block.Name
        states_string = self._block_entity_datas.StatesString
        fast_mode = self._block_entity_datas.FastMode
        # 来源: Sign/writeDatas.go 行 10-20

        # 步骤 1: 放置告示牌方块 (来源: Sign/writeDatas.go 行 10-20)
        if fast_mode:
            # 快速模式: 仅放置方块, 不写 NBT
            setblock_cmd = f"setblock {pos[0]} {pos[1]} {pos[2]} {block_name} {states_string}".strip()
            await self._send_command(setblock_cmd)
        else:
            setblock_cmd = f"setblock {pos[0]} {pos[1]} {pos[2]} {block_name} {states_string}".strip()
            await self._send_command(setblock_cmd)

        # 步骤 2: 写入告示牌 NBT 数据 (来源: Sign/writeDatas.go 行 22-34)
        nbt_data = {
            "TextOwner": self._data.TextOwner,
            "IgnoreLighting": self._data.IgnoreLighting,
            "SignTextColor": self._data.SignTextColor,
            "TextIgnoreLegacyBugResolved": self._data.TextIgnoreLegacyBugResolved,
            "Text": self._data.Text,
        }

        if self._send_packet is not None:
            await self._send_packet("BlockActorData", {
                "Position": {"X": pos[0], "Y": pos[1], "Z": pos[2]},
                "NBTData": nbt_data,
            })
        else:
            logger.warning(
                "SignWriter: send_packet 回调未提供, "
                "BlockActorData 数据包未发送; 仅放置了方块"
            )


# ======================================================================
# 11. NBTBlockDispatcher — 主分发器
#
# 来源: blockNBT/main.go (行 1-114)
# ======================================================================

class NBTBlockDispatcher:
    """NBT 方块放置主分发器。

    来源: blockNBT/main.go (行 1-114)

    负责根据方块类型将处理分发到对应的解码器/写入器:
        - ``CommandBlock`` -> CommandBlockDecoder + CommandBlockWriter
        - ``Container`` -> ContainerDecoder + ContainerWriter
        - ``Sign`` -> SignDecoder + SignWriter
        - 其他类型 -> 快速放置 (setblock 命令)

    使用全局 asyncio.Lock 确保同一时间只有一个放置操作在执行,
    对应 Go 源码中的 ``sync.Mutex`` (来源: blockNBT/main.go 行 86-87)。

    基本用法::

        dispatcher = NBTBlockDispatcher(
            send_command=my_send_command,
            send_packet=my_send_packet,
        )
        block_datas = BlockEntityDatas(
            Block=GeneralBlock(Name="chest", States={}, NBT={"Items": [...]}),
            Position=(100, 64, 200),
            Type="Container",
        )
        await dispatcher.place_block_with_nbt_data(block_datas)
    """

    def __init__(
        self,
        send_command: Callable[..., Awaitable[Any]],
        send_packet: Optional[Callable[..., Awaitable[Any]]] = None,
        exclude_commands: bool = False,
        invalidate_commands: bool = False,
    ):
        """初始化 NBT 方块分发器。

        Args:
            send_command: 发送游戏命令的异步回调 (如 send_wo_command/send_ai_command)。
            send_packet: 发送协议数据包的异步回调 (可选)。
            exclude_commands: 是否排除命令数据 (仅放置方块, 不写入命令)。
            invalidate_commands: 是否使命令无效化 (在命令前加 "# ")。
        """
        self._send_command = send_command
        self._send_packet = send_packet
        self._exclude_commands = exclude_commands
        self._invalidate_commands = invalidate_commands
        # 全局互斥锁 (来源: blockNBT/main.go 行 86-87)
        # H-8 修复: 懒加载 asyncio.Lock (避免跨事件循环崩溃)
        self._lock: Optional[asyncio.Lock] = None

    def _get_lock(self) -> asyncio.Lock:
        """获取锁 (H-8 修复: 懒加载, 绑定到当前事件循环)。"""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @staticmethod
    def check_if_effective_nbt_block(block_name: str) -> str:
        """检查方块实体是否已被支持。

        来源: blockNBT/main.go 行 37-43

        Args:
            block_name: 方块名称 (不含命名空间, 全小写)。

        Returns:
            方块类型字符串, 如 "CommandBlock"/"Container"/"Sign";
            如果不支持, 返回空字符串。
        """
        return SupportBlocksPool.get(block_name, "")

    async def _place_command_block(self, pack: BlockEntityDatas) -> None:
        """处理命令方块放置。

        来源: blockNBT/main.go 行 54-59 (CommandBlock 分支)

        Args:
            pack: 方块实体数据。
        """
        decoder = CommandBlockDecoder(pack)
        data = decoder.decode()
        writer = CommandBlockWriter(
            block_entity_datas=pack,
            data=data,
            send_command=self._send_command,
            send_packet=self._send_packet,
            exclude_commands=self._exclude_commands,
            invalidate_commands=self._invalidate_commands,
        )
        await writer.write(need_to_place_block=True)

    async def _place_container(self, pack: BlockEntityDatas) -> None:
        """处理容器放置。

        来源: blockNBT/main.go 行 61-66 (Container 分支)

        Args:
            pack: 方块实体数据。
        """
        decoder = ContainerDecoder(pack)
        items = decoder.decode()
        writer = ContainerWriter(
            block_entity_datas=pack,
            items=items,
            send_command=self._send_command,
        )
        await writer.write()

    async def _place_sign(self, pack: BlockEntityDatas) -> None:
        """处理告示牌放置。

        来源: blockNBT/main.go 行 68-73 (Sign 分支)

        Args:
            pack: 方块实体数据。
        """
        decoder = SignDecoder(pack)
        data = decoder.decode()
        writer = SignWriter(
            block_entity_datas=pack,
            data=data,
            send_command=self._send_command,
            send_packet=self._send_packet,
        )
        await writer.write()

    async def _place_fast(self, pack: BlockEntityDatas) -> None:
        """快速放置不受支持的方块实体。

        来源: blockNBT/main.go 行 75-80 (default 分支)

        Args:
            pack: 方块实体数据。
        """
        pos = pack.Position
        block_name = pack.Block.Name
        states_string = pack.StatesString
        setblock_cmd = f"setblock {pos[0]} {pos[1]} {pos[2]} {block_name} {states_string}".strip()
        await self._send_command(setblock_cmd)

    async def place_block_with_nbt_data(self, pack: BlockEntityDatas) -> None:
        """带有 NBT 数据放置方块 (主入口)。

        来源: blockNBT/main.go 行 52-84

        根据 ``pack.Type`` 分发到对应的处理器:
            - ``"CommandBlock"`` -> 命令方块处理
            - ``"Container"`` -> 容器处理
            - ``"Sign"`` -> 告示牌处理
            - 其他 -> 快速放置 (setblock 命令)

        使用全局 asyncio.Lock 确保线程安全, 对应 Go 源码中的 sync.Mutex。

        Args:
            pack: 方块实体数据 (包含类型、位置、NBT 等信息)。

        Raises:
            Exception: 处理过程中任何步骤失败时抛出。
        """
        # 获取锁 (来源: blockNBT/main.go 行 90-91)
        async with self._get_lock():
            block_type = pack.Type
            if not block_type:
                # 如果 Type 为空, 尝试从 SupportBlocksPool 自动推断
                block_type = self.check_if_effective_nbt_block(pack.Block.Name)

            if block_type == "CommandBlock":
                await self._place_command_block(pack)
            elif block_type == "Container":
                await self._place_container(pack)
            elif block_type == "Sign":
                await self._place_sign(pack)
            else:
                await self._place_fast(pack)

    async def place_block_with_nbt_data_run(
        self,
        block_info: dict[str, Any],
        position: tuple[int, int, int] = (0, 0, 0),
        states_string: str = "",
        fast_mode: bool = False,
    ) -> None:
        """便捷入口: 从原始数据构建 BlockEntityDatas 并放置。

        来源: blockNBT/main.go 行 89-114

        此方法封装了完整的处理流程:
            1. 从 block_info 构建 GeneralBlock
            2. 构建 BlockEntityDatas
            3. 自动推断方块类型 (通过 SupportBlocksPool)
            4. 调用 place_block_with_nbt_data()

        Args:
            block_info: 方块信息字典, 必须包含 ``"Name"`` 键,
                       可选 ``"States"`` 和 ``"NBT"`` 键。
            position: 方块绝对坐标 (x, y, z)。
            states_string: 字符串形式的方块状态。
            fast_mode: 是否使用快速模式。

        Raises:
            Exception: 处理过程中任何步骤失败时抛出。
        """
        # 构建 GeneralBlock (来源: blockNBT/main.go 行 17-34)
        block_name = str(block_info.get("Name", ""))
        block_name = block_name.lower().replace(" ", "").replace("minecraft:", "")
        states = block_info.get("States", {})
        if not isinstance(states, dict):
            states = {}
        nbt = block_info.get("NBT", {})
        if not isinstance(nbt, dict):
            nbt = {}

        general_block = GeneralBlock(
            Name=block_name,
            States=states,
            NBT=nbt,
        )

        # 推断方块类型 (来源: blockNBT/main.go 行 105)
        block_type = self.check_if_effective_nbt_block(block_name)

        # 构建 BlockEntityDatas (来源: blockNBT/main.go 行 98-106)
        pack = BlockEntityDatas(
            Block=general_block,
            Position=position,
            Type=block_type,
            StatesString=states_string,
            FastMode=fast_mode,
        )

        await self.place_block_with_nbt_data(pack)