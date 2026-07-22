"""container_handler - 容器 NBT 处理 (合并 NexusEgo + NovaBuilder)。

本模块合并了两个逆向来源的容器处理层:

来源 1 - NexusEgo v1.6.5 (三模式容器 NBT 生成):
    - WaterStructure/modules/nbt_assigner/  (容器 NBT 分配)
    - WaterStructure/modules/bdump/         (PlaceBlockWithChestData)
    - strings_nbt.txt                       (Container / ContainerNBT)

    三种容器放置模式:
        1. CONTAINER_MODE_STRUCTURE (structure):
           使用结构方块一次性放置带容器内容的方块。
           生成 ContainerNBT 复合标签, 包含 Items 列表。
        2. CONTAINER_MODE_REPLACEITEM (replaceitem):
           先放置空容器, 然后使用 /replaceitem 命令填充物品。
           生成一组 replaceitem 命令, 每个槽位一条。
        3. CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA (PlaceBlockWithChestData):
           使用 BDump 的 PlaceBlockWithChestData 命令 (ID=40/37/38),
           一次性放置容器及其内容物。生成 ChestData 二进制载荷。

来源 2 - NovaBuilder (运行时容器操作):
    - nbt_packets.txt (容器数据包序列)
    - PhoenixBuilder/fastbuilder/builder/builder.go

    运行时容器操作流程:
        1. 放置容器方块 (UpdateBlock)
        2. 发送 ContainerOpen 请求 (客户端 -> 服务器)
        3. 等待 ContainerOpen 回包 (服务器 -> 客户端)
        4. 验证容器已正确放置 (verifyPlacedContainer)
        5. 发送 ItemStackRequest (填充物品)
        6. 等待 ItemStackResponse (确认填充成功)
        7. 发送 ContainerClose (关闭容器)

.. important::

    **网易 3.8 协议限制**: replaceitem 命令已被阉割, 只能放耐久/特殊值/数量/
    NBT 标签 (keep_on_death, item_lock), 不能放附魔/自定义名字。
    因此 CONTAINER_MODE_REPLACEITEM 模式仅作为可选保留,
    默认推荐使用 CONTAINER_MODE_STRUCTURE (structure save/load 搬运)。

容器容量 (逆向自 strings):
    chest: 27 槽, trapped_chest: 27 槽, shulker_box: 27 槽, barrel: 27 槽,
    hopper: 5 槽, dispenser: 9 槽, dropper: 9 槽, furnace: 3 槽,
    brewing_stand: 5 槽 (4 药水槽 + 1 材料槽), crafter: 9 槽,
    chiseled_bookshelf: 6 槽
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger("pocketterm.protocol.nbt_handler.container_handler")


# -------------------------------------------------------------------- #
# 常量 (NexusEgo 三模式)
# -------------------------------------------------------------------- #

#: 容器放置模式 (NexusEgo)
CONTAINER_MODE_STRUCTURE: int = 0
CONTAINER_MODE_REPLACEITEM: int = 1
CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA: int = 2

#: 模式名称
MODE_NAMES: dict[int, str] = {
    CONTAINER_MODE_STRUCTURE: "structure",
    CONTAINER_MODE_REPLACEITEM: "replaceitem",
    CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA: "PlaceBlockWithChestData",
}

#: 容器容量 (逆向自 strings)
CONTAINER_CAPACITY: dict[str, int] = {
    "minecraft:chest": 27,
    "minecraft:trapped_chest": 27,
    "minecraft:shulker_box": 27,
    "minecraft:barrel": 27,
    "minecraft:hopper": 5,
    "minecraft:dispenser": 9,
    "minecraft:dropper": 9,
    "minecraft:furnace": 3,
    "minecraft:blast_furnace": 3,
    "minecraft:smoker": 3,
    "minecraft:brewing_stand": 5,
    "minecraft:crafter": 9,
    "minecraft:chiseled_bookshelf": 6,
    "minecraft:decorated_pot": 1,
    "minecraft:lectern": 1,
    "minecraft:jukebox": 1,
    "minecraft:ender_chest": 0,  # 末影箱不可放置物品
}


# -------------------------------------------------------------------- #
# 常量 (NovaBuilder 运行时操作)
# -------------------------------------------------------------------- #

#: 容器打开超时 (秒, 逆向自 nbt_packets.txt)
CONTAINER_OPEN_TIMEOUT: float = 5.0

#: 容器验证超时 (秒, 逆向自 nbt_packets.txt)
CONTAINER_VERIFY_TIMEOUT: float = 3.0

#: 容器关闭超时 (秒)
CONTAINER_CLOSE_TIMEOUT: float = 2.0

#: 物品填充超时 (秒)
ITEM_FILL_TIMEOUT: float = 5.0

#: 最大槽位数 (一般容器 27 格)
MAX_CONTAINER_SLOTS: int = 27

#: 大箱子槽位数 (54 格)
LARGE_CHEST_SLOTS: int = 54

#: 末影箱槽位数 (27 格)
ENDER_CHEST_SLOTS: int = 27

#: 漏斗槽位数 (5 格)
HOPPER_SLOTS: int = 5


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class ContainerHandlerError(Exception):
    """容器处理错误。"""


# -------------------------------------------------------------------- #
# 容器类型枚举 (NovaBuilder, 逆向自 minecraft/protocol/container.go)
# -------------------------------------------------------------------- #


class ContainerType(Enum):
    """容器类型 (逆向自 minecraft/protocol/container.go)。

    数值与 Bedrock 协议中的 container_type 字段对应。
    """
    NONE = 0
    CHEST = 1
    CRAFTING = 2
    FURNACE = 3
    DISPENSER = 4
    ENCHANT = 5
    BREWING = 6
    VILLAGER = 7
    BEACON = 8
    ANVIL = 9
    HOPPER = 10
    SHULKER_BOX = 11
    BARREL = 12
    BLAST_FURNACE = 13
    SMOKER = 14
    LECTERN = 15
    GRINDSTONE = 16
    STONECUTTER = 17
    CARTOGRAPHY = 18
    LOOM = 19

    @classmethod
    def from_block_name(cls, block_name: str) -> "ContainerType":
        """从方块名推断容器类型。

        Args:
            block_name: 方块名 (如 "minecraft:chest")。

        Returns:
            对应的 :class:`ContainerType`。
        """
        name = block_name.lower().replace("minecraft:", "").replace("_", "")
        mapping = {
            "chest": cls.CHEST,
            "trappedchest": cls.CHEST,
            "enderchest": cls.CHEST,
            "furnace": cls.FURNACE,
            "blastfurnace": cls.BLAST_FURNACE,
            "smoker": cls.SMOKER,
            "dispenser": cls.DISPENSER,
            "dropper": cls.DISPENSER,
            "enchantingtable": cls.ENCHANT,
            "enchanttable": cls.ENCHANT,
            "brewingstand": cls.BREWING,
            "beacon": cls.BEACON,
            "anvil": cls.ANVIL,
            "hopper": cls.HOPPER,
            "shulkerbox": cls.SHULKER_BOX,
            "barrel": cls.BARREL,
            "lectern": cls.LECTERN,
            "grindstone": cls.GRINDSTONE,
            "stonecutter": cls.STONECUTTER,
            "cartographytable": cls.CARTOGRAPHY,
            "loom": cls.LOOM,
        }
        return mapping.get(name, cls.NONE)

    @property
    def slot_count(self) -> int:
        """获取容器槽位数。"""
        counts = {
            ContainerType.CHEST: MAX_CONTAINER_SLOTS,
            ContainerType.FURNACE: 3,
            ContainerType.BLAST_FURNACE: 3,
            ContainerType.SMOKER: 3,
            ContainerType.DISPENSER: 9,
            ContainerType.ENCHANT: 2,
            ContainerType.BREWING: 5,
            ContainerType.BEACON: 1,
            ContainerType.ANVIL: 3,
            ContainerType.HOPPER: HOPPER_SLOTS,
            ContainerType.SHULKER_BOX: MAX_CONTAINER_SLOTS,
            ContainerType.BARREL: MAX_CONTAINER_SLOTS,
            ContainerType.LECTERN: 1,
            ContainerType.GRINDSTONE: 3,
            ContainerType.STONECUTTER: 2,
            ContainerType.CARTOGRAPHY: 3,
            ContainerType.LOOM: 4,
        }
        return counts.get(self, 0)


# -------------------------------------------------------------------- #
# 数据结构 (NexusEgo)
# -------------------------------------------------------------------- #


@dataclass
class ContainerContent:
    """容器内容物。

    逆向自 strings_nbt.txt 中的 ContainerNBT 结构。

    Attributes:
        container_type: 容器方块名 (如 "minecraft:chest")。
        items: 物品列表, 每项包含 slot/id/count/damage(可选)/tag(可选)。
    """
    container_type: str = "minecraft:chest"
    items: list[dict[str, Any]] = field(default_factory=list)
    #: items 中每项:
    #:   slot: int (0-26 for chest)
    #:   id: str (如 "minecraft:diamond")
    #:   count: int (1-64)
    #:   damage: int (可选, 工具耐久)
    #:   tag: dict (可选, 物品 NBT)

    def add_item(self, slot: int, item_id: str, count: int = 1,
                  damage: int | None = None,
                  tag: dict[str, Any] | None = None) -> None:
        """添加物品到指定槽位。

        Args:
            slot: 槽位编号。
            item_id: 物品 ID。
            count: 物品数量。
            damage: 物品耐久 (可选)。
            tag: 物品 NBT 标签 (可选)。

        Raises:
            ContainerHandlerError: 槽位超出范围。
        """
        if slot < 0:
            raise ContainerHandlerError(f"invalid slot: {slot}")
        capacity = get_container_capacity(self.container_type)
        if capacity > 0 and slot >= capacity:
            raise ContainerHandlerError(
                f"slot {slot} out of range (capacity={capacity})"
            )
        item: dict[str, Any] = {
            "slot": slot,
            "id": item_id,
            "count": count,
        }
        if damage is not None:
            item["damage"] = damage
        if tag is not None:
            item["tag"] = tag
        self.items.append(item)

    def to_nbt_items(self) -> list[dict[str, Any]]:
        """转换为 NBT Items 列表格式。"""
        nbt_items: list[dict[str, Any]] = []
        for item in self.items:
            nbt_item: dict[str, Any] = {
                "Slot": item["slot"],
                "id": item["id"],
                "Count": item["count"],
            }
            if "damage" in item:
                nbt_item["Damage"] = item["damage"]
            if "tag" in item:
                nbt_item["tag"] = item["tag"]
            nbt_items.append(nbt_item)
        return nbt_items


@dataclass
class ContainerMode:
    """容器放置模式信息。"""
    mode: int = CONTAINER_MODE_STRUCTURE
    name: str = "structure"


# -------------------------------------------------------------------- #
# 数据结构 (NovaBuilder)
# -------------------------------------------------------------------- #


@dataclass
class ContainerOpenResult:
    """容器打开结果 (逆向自 nbt_packets.txt ContainerOpen 回包)。

    Attributes:
        container_id: 容器 ID。
        container_type: 容器类型。
        position: 容器位置。
        size: 容器大小 (槽位数)。
        success: 是否成功打开。
        error: 错误信息。
    """
    container_id: int = 0
    container_type: ContainerType = ContainerType.NONE
    position: tuple[int, int, int] = (0, 0, 0)
    size: int = 0
    success: bool = False
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "container_id": self.container_id,
            "container_type": self.container_type.name,
            "position": self.position,
            "size": self.size,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class ItemStackEntry:
    """物品槽位 (逆向自 minecraft/protocol/item_stack.go)。

    用于 NovaBuilder 运行时容器填充操作。

    Attributes:
        id: 运行时物品 ID。
        count: 物品数量。
        damage: 物品耐久。
        name: 物品名。
        block_runtime_id: 方块运行时 ID。
        nbt: 物品 NBT 数据。
        can_place_on: 可放置于的方块列表。
        can_destroy: 可破坏的方块列表。
    """
    id: int = 0
    count: int = 0
    damage: int = 0
    name: str = ""
    block_runtime_id: int = 0
    nbt: dict[str, Any] = field(default_factory=dict)
    can_place_on: list[str] = field(default_factory=list)
    can_destroy: list[str] = field(default_factory=list)


# -------------------------------------------------------------------- #
# 容器处理器 (合并 NexusEgo 三模式 + NovaBuilder 运行时操作)
# -------------------------------------------------------------------- #


class ContainerHandler:
    """容器处理器。

    合并 NexusEgo 的三模式容器 NBT 生成和 NovaBuilder 的运行时容器操作。

    **NexusEgo 三模式** (通过 ``mode`` 参数选择, 使用 :meth:`process`):
        - CONTAINER_MODE_STRUCTURE: 生成结构方块 NBT
        - CONTAINER_MODE_REPLACEITEM: 生成 replaceitem 命令列表
        - CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA: 生成 ChestData 载荷

    **NovaBuilder 运行时操作** (通过 game_interface, 使用
    :meth:`open_container` / :meth:`fill_items` / :meth:`close_container`):
        - 放置容器方块后, 打开容器并填充物品

    .. important::

        **网易 3.8 限制**: replaceitem 模式已被阉割, 只能放耐久/特殊值/数量/
        NBT 标签, 不能放附魔/自定义名字。默认推荐 STRUCTURE 模式。

    使用方式 (NexusEgo 三模式)::

        handler = ContainerHandler(mode=CONTAINER_MODE_STRUCTURE)
        content = ContainerContent(container_type="minecraft:chest")
        content.add_item(slot=0, item_id="minecraft:diamond", count=64)
        result = handler.process(content, position=(100, 64, 100))

    使用方式 (NovaBuilder 运行时操作)::

        handler = ContainerHandler(game_interface=interface)
        result = handler.open_container(position=(100, 64, 100))
        if result.success:
            handler.fill_items(result.container_id, items)
            handler.close_container(result.container_id)
    """

    def __init__(
        self,
        mode: int = CONTAINER_MODE_STRUCTURE,
        game_interface: Optional[Any] = None,
        open_timeout: float = CONTAINER_OPEN_TIMEOUT,
        verify_timeout: float = CONTAINER_VERIFY_TIMEOUT,
    ) -> None:
        """初始化容器处理器。

        Args:
            mode: NexusEgo 容器放置模式。
            game_interface: NovaBuilder 游戏接口 (None 启用模拟模式)。
            open_timeout: 容器打开超时 (秒)。
            verify_timeout: 容器验证超时 (秒)。

        Raises:
            ContainerHandlerError: 无效的放置模式。
        """
        if mode not in MODE_NAMES:
            raise ContainerHandlerError(f"invalid mode: {mode}")
        self.mode = mode
        self.game_interface = game_interface
        self.open_timeout = open_timeout
        self.verify_timeout = verify_timeout
        self._open_containers: dict[int, ContainerOpenResult] = {}
        self._next_container_id: int = 1
        logger.debug("ContainerHandler initialized: mode=%s", MODE_NAMES[mode])

    # ---------------------------------------------------------------- #
    # NexusEgo 三模式处理
    # ---------------------------------------------------------------- #

    def process(self, content: ContainerContent,
                  position: tuple[int, int, int] = (0, 0, 0)) -> dict[str, Any]:
        """处理容器内容物 (NexusEgo 三模式)。

        Args:
            content: 容器内容物。
            position: 容器位置。

        Returns:
            处理结果字典, 包含 mode / data 字段。

        Raises:
            ContainerHandlerError: 不支持的放置模式。
        """
        if self.mode == CONTAINER_MODE_STRUCTURE:
            return self._process_structure(content, position)
        if self.mode == CONTAINER_MODE_REPLACEITEM:
            return self._process_replaceitem(content, position)
        if self.mode == CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA:
            return self._process_chest_data(content, position)
        raise ContainerHandlerError(f"unsupported mode: {self.mode}")

    def _process_structure(self, content: ContainerContent,
                              position: tuple[int, int, int]) -> dict[str, Any]:
        """模式 1: 结构方块模式。

        生成 ContainerNBT 复合标签, 包含 Items 列表。
        使用 /setblock 或 /structure 命令放置。
        """
        nbt_data = build_structure_container_nbt(content)
        return {
            "mode": "structure",
            "position": position,
            "container_type": content.container_type,
            "nbt_data": nbt_data,
        }

    def _process_replaceitem(self, content: ContainerContent,
                                position: tuple[int, int, int]) -> dict[str, Any]:
        """模式 2: replaceitem 模式。

        生成一组 /replaceitem 命令, 每个槽位一条。
        命令格式: replaceitem block <x> <y> <z> slot.container <slot> <item> <count>

        .. warning::

            网易 3.8 阉割了 replaceitem, 只能放耐久/特殊值/数量/NBT 标签,
            不能放附魔/自定义名字。
        """
        commands = build_replaceitem_commands(content, position)
        return {
            "mode": "replaceitem",
            "position": position,
            "container_type": content.container_type,
            "commands": commands,
            "command_count": len(commands),
        }

    def _process_chest_data(self, content: ContainerContent,
                                position: tuple[int, int, int]) -> dict[str, Any]:
        """模式 3: PlaceBlockWithChestData 模式。

        生成 ChestData 二进制载荷, 用于 BDump 命令 40/37/38。
        一次性放置容器及其内容物。
        """
        chest_data = build_chest_data_payload(content)
        return {
            "mode": "PlaceBlockWithChestData",
            "position": position,
            "container_type": content.container_type,
            "chest_data": chest_data,
        }

    # ---------------------------------------------------------------- #
    # NovaBuilder 运行时容器操作
    # ---------------------------------------------------------------- #

    def open_container(
        self,
        position: tuple[int, int, int],
        container_type: Optional[ContainerType] = None,
    ) -> ContainerOpenResult:
        """打开容器 (NovaBuilder 运行时操作)。

        逆向自 nbt_packets.txt:
            发送 ContainerOpen 数据包, 等待回包。

        Args:
            position: 容器位置。
            container_type: 容器类型 (None 自动推断为 CHEST)。

        Returns:
            :class:`ContainerOpenResult`。
        """
        self.logger = logging.getLogger(
            "pocketterm.protocol.nbt_handler.container_handler"
        )
        self.logger.info("Opening container at %s", position)

        if container_type is None:
            container_type = ContainerType.CHEST

        result = ContainerOpenResult(
            container_type=container_type,
            position=position,
            size=container_type.slot_count,
        )

        if self.game_interface is None:
            # 模拟模式
            result.container_id = self._next_container_id
            self._next_container_id += 1
            result.success = True
            self._open_containers[result.container_id] = result
            return result

        # 发送 ContainerOpen 请求
        try:
            container_id = self.game_interface.send_container_open(
                position=position,
                container_type=container_type.value,
            )
        except Exception as e:
            self.logger.error("Failed to send ContainerOpen: %s", e)
            result.error = str(e)
            return result

        # 等待服务器响应
        start_time = time.time()
        while time.time() - start_time < self.open_timeout:
            if container_id is not None:
                result.container_id = container_id
                result.success = True
                self._open_containers[container_id] = result
                self.logger.info("Container opened: id=%d", container_id)
                return result
            time.sleep(0.05)

        result.error = f"Container open timeout ({self.open_timeout}s)"
        self.logger.warning("Container open timeout at %s", position)
        return result

    def verify_placed_container(
        self,
        position: tuple[int, int, int],
        expected_block: str,
    ) -> bool:
        """验证容器已正确放置 (逆向自 verifyPlacedContainer)。

        Args:
            position: 容器位置。
            expected_block: 期望的方块名。

        Returns:
            True 如果验证成功。
        """
        self.logger = logging.getLogger(
            "pocketterm.protocol.nbt_handler.container_handler"
        )
        self.logger.debug(
            "Verifying container at %s, expected=%s", position, expected_block
        )

        if self.game_interface is None:
            return True

        start_time = time.time()
        while time.time() - start_time < self.verify_timeout:
            try:
                actual_block = self.game_interface.get_block_at(position)
                if actual_block == expected_block:
                    self.logger.debug("Container verified at %s", position)
                    return True
            except Exception as e:
                self.logger.warning("Failed to query block: %s", e)
            time.sleep(0.1)

        self.logger.warning("Container verification failed at %s", position)
        return False

    def fill_items(
        self,
        container_id: int,
        items: list[tuple[int, ItemStackEntry]],
        batch_size: int = 10,
    ) -> bool:
        """填充容器物品 (逆向自 sendItemStackRequest)。

        Args:
            container_id: 容器 ID。
            items: (slot, ItemStackEntry) 列表。
            batch_size: 每批发送的物品数。

        Returns:
            True 如果全部成功。
        """
        self.logger = logging.getLogger(
            "pocketterm.protocol.nbt_handler.container_handler"
        )
        self.logger.info(
            "Filling container %d with %d items", container_id, len(items)
        )

        if self.game_interface is None:
            return True

        success = True
        # 分批发送 (避免数据包过大)
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            try:
                self.game_interface.send_item_stack_request(
                    container_id=container_id,
                    items=batch,
                )
            except Exception as e:
                self.logger.error("Failed to fill items: %s", e)
                success = False

            # 等待响应
            time.sleep(0.05)

        return success

    def close_container(self, container_id: int) -> bool:
        """关闭容器。

        逆向自 nbt_packets.txt:
            发送 ContainerClose 数据包。

        Args:
            container_id: 容器 ID。

        Returns:
            True 如果成功关闭。
        """
        self.logger = logging.getLogger(
            "pocketterm.protocol.nbt_handler.container_handler"
        )
        self.logger.info("Closing container %d", container_id)

        if container_id in self._open_containers:
            del self._open_containers[container_id]

        if self.game_interface is None:
            return True

        try:
            self.game_interface.send_container_close(container_id)
            return True
        except Exception as e:
            self.logger.error("Failed to close container: %s", e)
            return False

    def fill_chest_with_items(
        self,
        position: tuple[int, int, int],
        items: list[tuple[int, ItemStackEntry]],
        expected_block: str = "minecraft:chest",
    ) -> bool:
        """一键填充箱子 (放置 -> 打开 -> 填充 -> 关闭)。

        Args:
            position: 容器位置。
            items: (slot, ItemStackEntry) 列表。
            expected_block: 期望的方块名。

        Returns:
            True 如果全部成功。
        """
        # 1. 验证容器已放置
        if not self.verify_placed_container(position, expected_block):
            return False

        # 2. 打开容器
        result = self.open_container(position)
        if not result.success:
            return False

        # 3. 填充物品
        if not self.fill_items(result.container_id, items):
            self.close_container(result.container_id)
            return False

        # 4. 关闭容器
        return self.close_container(result.container_id)

    @property
    def open_container_count(self) -> int:
        """当前打开的容器数。"""
        return len(self._open_containers)

    def close_all_containers(self) -> None:
        """关闭所有打开的容器 (紧急清理)。"""
        self.logger = logging.getLogger(
            "pocketterm.protocol.nbt_handler.container_handler"
        )
        self.logger.warning(
            "Closing %d open containers", len(self._open_containers)
        )
        for cid in list(self._open_containers.keys()):
            self.close_container(cid)

    @staticmethod
    def create_item_stack(
        name: str,
        count: int = 1,
        damage: int = 0,
        nbt: Optional[dict[str, Any]] = None,
    ) -> ItemStackEntry:
        """创建物品栈。"""
        return ItemStackEntry(
            name=name,
            count=count,
            damage=damage,
            nbt=nbt if nbt else {},
        )


# -------------------------------------------------------------------- #
# 顶层函数 (NexusEgo)
# -------------------------------------------------------------------- #


def build_structure_container_nbt(content: ContainerContent) -> dict[str, Any]:
    """构建结构方块模式的容器 NBT。

    逆向自 strings_nbt.txt 中的 ContainerNBT 结构。

    Args:
        content: 容器内容物。

    Returns:
        NBT 复合标签字典, 包含:
            - id: "Chest" (或其他容器 ID)
            - Items: 物品列表
            - Findable: bool (是否可被漏斗查找)
    """
    nbt: dict[str, Any] = {
        "id": _get_container_entity_id(content.container_type),
        "Items": content.to_nbt_items(),
        "Findable": False,
    }
    return nbt


def build_replaceitem_commands(content: ContainerContent,
                                  position: tuple[int, int, int]) -> list[str]:
    """构建 replaceitem 命令列表。

    逆向自 strings: "replaceitem block %d %d %d slot.container %d %s %d"

    .. warning::

        网易 3.8 阉割了 replaceitem 命令, 只能放耐久/特殊值/数量/NBT 标签,
        不能放附魔/自定义名字。使用此函数时请注意此限制。

    Args:
        content: 容器内容物。
        position: 容器位置 (x, y, z)。

    Returns:
        命令字符串列表。
    """
    x, y, z = position
    commands: list[str] = []
    for item in content.items:
        slot = item["slot"]
        item_id = item["id"]
        count = item.get("count", 1)
        cmd = f"replaceitem block {x} {y} {z} slot.container {slot} {item_id} {count}"
        # 如果有 damage, 添加 data 值
        if "damage" in item:
            cmd += f" {item['damage']}"
        commands.append(cmd)
    return commands


def build_chest_data_payload(content: ContainerContent) -> dict[str, Any]:
    """构建 PlaceBlockWithChestData 的 ChestData 载荷。

    逆向自 merry-memory/protocol/encoding.ChestData 结构。
    用于 BDump 命令 40 (PlaceBlockWithChestData) /
    37 (PlaceRuntimeBlockWithChestData) /
    38 (PlaceRuntimeBlockWithChestDataAndUint32RuntimeID)。

    Args:
        content: 容器内容物。

    Returns:
        ChestData 字典, 包含:
            - chest_size: 槽位数
            - slots: 槽位列表
    """
    capacity = get_container_capacity(content.container_type)
    # ChestData 包含所有槽位 (含空槽)
    slots: list[dict[str, Any]] = []
    items_by_slot = {item["slot"]: item for item in content.items}
    total_slots = max(capacity, max(items_by_slot.keys(), default=-1) + 1)
    for slot_idx in range(total_slots):
        if slot_idx in items_by_slot:
            item = items_by_slot[slot_idx]
            slot_data: dict[str, Any] = {
                "network_id": _item_id_to_network(item["id"]),
                "count": item.get("count", 1),
                "aux_value": item.get("damage", 0),
                "can_place_on": [],
                "can_destroy": [],
            }
            if "tag" in item:
                slot_data["nbt_data"] = item["tag"]
            slots.append(slot_data)
        else:
            slots.append({"network_id": 0})  # 空槽

    return {
        "chest_size": len(slots),
        "unknown_field": 0,
        "slots": slots,
    }


def get_container_capacity(container_type: str) -> int:
    """获取容器容量。

    Args:
        container_type: 容器方块名。

    Returns:
        槽位数。
    """
    return CONTAINER_CAPACITY.get(container_type.lower(), 27)


def _get_container_entity_id(container_type: str) -> str:
    """获取容器的方块实体 ID。"""
    name = container_type.lower()
    if "shulker_box" in name:
        return "ShulkerBox"
    if name.endswith(":barrel"):
        return "Barrel"
    if name.endswith(":hopper"):
        return "Hopper"
    if name.endswith(":dispenser"):
        return "Dispenser"
    if name.endswith(":dropper"):
        return "Dropper"
    if "furnace" in name:
        return "Furnace"
    if "brewing_stand" in name:
        return "BrewingStand"
    if name.endswith(":chiseled_bookshelf"):
        return "ChiseledBookshelf"
    if name.endswith(":lectern"):
        return "Lectern"
    if name.endswith(":jukebox"):
        return "Jukebox"
    if name.endswith(":crafter"):
        return "Crafter"
    if name.endswith(":decorated_pot"):
        return "DecoratedPot"
    # 默认为 Chest
    return "Chest"


def _item_id_to_network(item_id: str) -> int:
    """将物品名转换为网络 ID (简化版)。

    实际的网络 ID 转换需要完整的物品 ID 映射表。
    这里使用一个简化的映射, 仅用于演示。

    Args:
        item_id: 物品名 (如 "minecraft:diamond")。

    Returns:
        网络 ID。
    """
    # 简化映射 (实际应使用完整的物品 ID 表)
    known_ids: dict[str, int] = {
        "minecraft:air": 0,
        "minecraft:stone": 1,
        "minecraft:grass": 2,
        "minecraft:dirt": 3,
        "minecraft:cobblestone": 4,
        "minecraft:planks": 5,
        "minecraft:sapling": 6,
        "minecraft:bedrock": 7,
        "minecraft:flowing_water": 8,
        "minecraft:water": 9,
        "minecraft:diamond": 264,
        "minecraft:diamond_sword": 276,
        "minecraft:diamond_pickaxe": 278,
        "minecraft:diamond_axe": 279,
        "minecraft:diamond_shovel": 277,
        "minecraft:diamond_hoe": 293,
        "minecraft:diamond_helmet": 310,
        "minecraft:diamond_chestplate": 311,
        "minecraft:diamond_leggings": 312,
        "minecraft:diamond_boots": 313,
    }
    return known_ids.get(item_id.lower(), -1)


__all__ = [
    # NexusEgo 常量
    "CONTAINER_MODE_STRUCTURE", "CONTAINER_MODE_REPLACEITEM",
    "CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA",
    "MODE_NAMES", "CONTAINER_CAPACITY",
    # NovaBuilder 常量
    "CONTAINER_OPEN_TIMEOUT", "CONTAINER_VERIFY_TIMEOUT",
    "CONTAINER_CLOSE_TIMEOUT", "ITEM_FILL_TIMEOUT",
    "MAX_CONTAINER_SLOTS", "LARGE_CHEST_SLOTS",
    "ENDER_CHEST_SLOTS", "HOPPER_SLOTS",
    # 异常
    "ContainerHandlerError",
    # NexusEgo 数据结构
    "ContainerContent", "ContainerMode",
    # NovaBuilder 数据结构
    "ContainerType", "ContainerOpenResult", "ItemStackEntry",
    # 容器处理器
    "ContainerHandler",
    # 顶层函数
    "build_structure_container_nbt", "build_replaceitem_commands",
    "build_chest_data_payload", "get_container_capacity",
]
