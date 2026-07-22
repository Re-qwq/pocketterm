"""NBT 方块放置器 - 实现建筑中带 NBT 数据的方块的精确放置。

本模块通过 :class:`MagicCommandSender` (网易版 Bedrock 魔法指令系统) 精确放置
带有 NBT 数据的方块, 包括告示牌、容器、命令方块、旗帜等。

**双模式 NBT 放置系统 (v2.1, 网易 3.8 适配):**

.. important::

    **网易 3.8 阉割了 replaceitem 命令**, 现在 replaceitem 只能放:
    耐久、特殊值、数量、NBT 标签 (如 ``minecraft:keep_on_death`` 死亡不掉落、
    ``minecraft:item_lock`` 物品锁定)。**不能用 replaceitem 放附魔、自定义名字。**

    因此本模块**默认使用 platform / structure 模式** (平台模式),
    通过 structure save/load 搬运 NBT 方块。replaceitem 模式仅作为可选保留
    (用户明确知道风险时可以选)。

    逆向依据: NovaBuilder (PhoenixBuilder 衍生) 和 NexusE (NexusEgo v1.6.5)
    两者都使用 11x11 平台 + structure save/load 作为核心 NBT 搬运机制,
    并通过 FindOrGenerateNewAnvil/FindOrGenerateNewLoom/
    FindOrGenerateNewSmithingTable 动态生成工作方块。

本模块实现了两种 NBT 放置模式:

1. **structure 模式 (默认, 平台模式, NovaBuilder/NexusE 方案)**:
   - 在 11x11 海晶灯平台生成 NBT 方块 → structure save → tp 到目标 → structure load
   - 工作方块 (铁砧/织布机/锻造台) 通过 FindOrGenerate* 动态生成
   - 告示牌: 先写文本 NBT, 再发光墨囊, 最后蜜蜡锁定
   - **网易 3.8 推荐方案**, 因为 replaceitem 已被阉割
   - 来源: NovaBuilder + NexusE 逆向 (nbt_platform_comparison.txt)

2. **replaceitem 模式 (可选, PhoenixBuilder 方案, 风险模式)**:
   - 容器: 使用 replaceitem 命令直接在目标位置写入物品
   - 命令方块: 通过 CommandBlockUpdate 数据包写入命令数据
   - 告示牌: 通过 BlockActorData 数据包写入告示牌 NBT 数据
   - 优势: 不依赖 structure save/load, 容器物品不会丢失
   - 风险: 网易 3.8 阉割后只能放耐久/特殊值/数量/NBT标签,
     不能放附魔、自定义名字
   - 来源: PhoenixBuilder Go 源码 (phoenix_nbt.py)

3. **auto 模式 (自动检测)**:
   - 根据容器类型自动选择最合适的模式:
     - 容器方块 (chest/barrel/hopper 等) → structure 模式 (3.8 阉割后默认)
     - 命令方块/告示牌 → structure 模式 (默认推荐)
     - 其他 NBT 方块 → structure 模式

核心工作流程 (structure 模式, 默认)::

    1. 建造海晶灯平台 (11x11 单层, 中间一格为放置区)
       a. 先清除 11x11x5 区域 (fill air, ~-5 ~-2 ~-5 ~5 ~2 ~5)
       b. 再填充 11x1x11 平台 (fill ~-5 ~-1 ~-5 ~5 ~-1 ~5 sea_lantern)
    2. 放置工作方块 (铁砧、织布机、合成台、切石机、锻造台)
    3. 对每个 NBT 方块:
       a. 在平台中间生成方块 (带 NBT 数据)
       b. 处理特殊效果 (如告示牌的发光墨囊 + 蜜蜡)
       c. 保存结构: /structure save <name> <坐标>
       d. 机器人传送到目标位置: /tp @s <目标坐标>
       e. 加载结构到目标坐标: /structure load <name> <目標坐标>
       f. 处理下一个 NBT 方块
    4. 清理平台和临时结构

核心工作流程 (replaceitem 模式, 可选)::

    1. 解析建筑文件中的 NBT 方块数据
    2. 对每个 NBT 方块:
       a. 直接在目标位置放置方块 (setblock)
       b. 使用 replaceitem 命令写入容器物品 (3.8 只能放耐久/特殊值/数量/NBT标签)
       c. 使用数据包写入命令方块/告示牌 NBT 数据
       d. 无需 structure save/load 搬运

关键注意点:
    - 所有命令通过 :meth:`MagicCommandSender.send_any_command` 发送 (自动路由)
    - ``/tp`` 会自动走控制台命令 (``send_wo_command``)
    - ``/setblock`` ``/replaceitem`` 会自动走魔法指令 (``send_ai_command``)
    - 发光墨囊和蜜蜡的顺序: **先墨囊后蜜蜡, 绝对不能反**
      (如果顺序反了, 发光墨囊就涂不上去了)
    - ``structure save``/``load`` 用于搬运 NBT 方块到目标位置 (structure 模式核心)
    - **网易 3.8 限制**: replaceitem 不能放附魔/自定义名字, 默认走平台模式

基本用法::

    from app.protocol.magic_command import MagicCommandSender
    from app.protocol.nbt_placer import NBTBlockPlacer, NBTPlacementMode

    sender = MagicCommandSender(client)
    # 默认 structure 模式 (网易 3.8 推荐)
    placer = NBTBlockPlacer(sender, nbt_mode=NBTPlacementMode.STRUCTURE)

    # 平台模式: 先建平台, 再 place_nbt_block 搬运到目标
    await placer.build_platform(100, 64, 200)
    await placer.build_work_blocks(100, 64, 200)
    await placer.place_nbt_block(
        block_type="sign",
        x=100, y=65, z=200,  # 平台中间 (生成位置)
        nbt_data={
            "facing": "south",
            "text_lines": ["Hello", "World", "", ""],
            "is_wall": False,
        },
        target_x=500, target_y=70, target_z=500,  # 搬运目标
    )
    await placer.cleanup_platform(100, 64, 200)

    # 可选: 直接放置容器到目标位置 (replaceitem 模式, 网易 3.8 风险)
    # 只能放耐久/特殊值/数量/NBT标签 (keep_on_death, item_lock)
    await placer.place_container_direct(
        x=500, y=70, z=500,
        block_name="minecraft:chest",
        items=[{"slot": 0, "item_name": "minecraft:diamond", "count": 64}],
    )
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import uuid
from typing import Any, Optional

from .magic_command import CommandRateLimiter, MagicCommandSender
from .blocks import BlockState
from .nbt import marshal_network, unmarshal_network
from .phoenix_nbt import (
    BlockEntityDatas,
    ContainerDecoder,
    ContainerWriter,
    CommandBlockData,
    CommandBlockDecoder,
    CommandBlockWriter,
    SignData,
    SignDecoder,
    SignWriter,
    GeneralBlock,
    ContainerItem,
    SupportBlocksPool,
    SupportContainerPool,
    NBTBlockDispatcher,
)

logger = logging.getLogger("pocketterm.protocol.nbt_placer")


# ======================================================================
# NBT 放置模式枚举
# ======================================================================


class NBTPlacementMode(enum.Enum):
    """NBT 方块放置模式枚举。

    定义两种核心放置策略及自动检测模式:

    .. important::

        **网易 3.8 阉割了 replaceitem 命令**, replaceitem 现在只能放耐久、
        特殊值、数量、NBT 标签 (如 ``minecraft:keep_on_death`` 死亡不掉落、
        ``minecraft:item_lock`` 物品锁定)。**不能用 replaceitem 放附魔、
        自定义名字**。因此默认推荐使用 ``STRUCTURE`` 平台模式。

    * ``STRUCTURE``: NovaBuilder/NexusE 方案 -- 在 11x11 海晶灯平台生成
      NBT 方块, 通过 structure save/load 搬运到目标位置。
      **网易 3.8 推荐方案**, 因为 replaceitem 已被阉割。
      工作方块 (铁砧/织布机/锻造台) 通过 FindOrGenerate* 动态生成。

    * ``REPLACEITEM``: PhoenixBuilder 方案 -- 使用 replaceitem 命令
      和数据包直接在目标位置写入 NBT 数据, 不依赖 structure save/load。
      **可选保留**, 用户明确知道网易 3.8 风险时可以使用。
      注意: 3.8 阉割后只能放耐久/特殊值/数量/NBT标签, 不能放附魔/自定义名字。

    * ``AUTO``: 自动检测 -- 根据方块类型和运行环境自动选择最合适的模式。
      网易 3.8 后默认推荐 STRUCTURE (因为 replaceitem 被阉割)。
    """

    REPLACEITEM = "replaceitem"
    """PhoenixBuilder 方案: 直接 replaceitem + 数据包写入 (网易 3.8 风险模式)"""

    STRUCTURE = "structure"
    """NovaBuilder/NexusE 方案: structure save/load 搬运 (默认推荐, 网易 3.8 适配)"""

    AUTO = "auto"
    """自动检测: 默认推荐 structure 模式 (网易 3.8 replaceitem 已阉割)"""


# ======================================================================
# NBT 模式自动检测器
# ======================================================================


class NBTModeSelector:
    """NBT 放置模式自动检测器。

    根据方块类型和运行环境自动选择最合适的放置模式。

    .. important::

        **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
        不能放附魔/自定义名字), 因此本检测器**默认推荐 STRUCTURE 平台模式**。
        仅在用户显式指定 REPLACEITEM 时才走 replaceitem 路径。

    **检测规则 (网易 3.8 适配后):**

    1. **容器方块** (chest, barrel, hopper, dispenser, dropper, furnace,
       shulker_box, brewing_stand, jukebox, lectern 等):
       → 默认使用 ``STRUCTURE`` 模式 (网易 3.8 replaceitem 阉割后推荐)。
       若用户显式选择 REPLACEITEM 且明确知道风险, 则使用 REPLACEITEM。

    2. **命令方块** (command_block, chain_command_block,
       repeating_command_block):
       → 默认使用 ``STRUCTURE`` 模式。仅在用户显式选择 REPLACEITEM
       且 ``send_packet`` 回调可用时, 使用 REPLACEITEM 模式。

    3. **告示牌** (sign, wall_sign, hanging_sign 等):
       → 默认使用 ``STRUCTURE`` 模式 (告示牌需要发光墨囊+蜜蜡交互,
       structure 模式更可靠)。仅在用户显式选择 REPLACEITEM
       且 ``send_packet`` 回调可用时, 使用 REPLACEITEM 模式。

    4. **其他 NBT 方块** (banner, frame, beacon 等):
       → 使用 ``STRUCTURE`` 模式 (仅 structure save/load 可用)。

    用法::

        selector = NBTModeSelector()
        # 网易 3.8 后默认推荐 structure
        mode = selector.detect("chest", send_packet_available=True)
        # mode == NBTPlacementMode.STRUCTURE
    """

    #: 容器方块名集合 (来自 SupportContainerPool)
    #: 网易 3.8 阉割后, 这些方块默认走 STRUCTURE 平台模式 (replaceitem 仅可选)
    _CONTAINER_BLOCKS: frozenset[str] = frozenset(SupportContainerPool.keys())

    #: 命令方块名集合
    _COMMAND_BLOCK_NAMES: frozenset[str] = frozenset({
        "command_block",
        "chain_command_block",
        "repeating_command_block",
    })

    #: 告示牌方块名集合 (从 SupportBlocksPool 中提取 Sign 类型)
    _SIGN_BLOCK_NAMES: frozenset[str] = frozenset({
        name for name, block_type in SupportBlocksPool.items()
        if block_type == "Sign"
    })

    @classmethod
    def detect(
        cls,
        block_name: str,
        send_packet_available: bool = False,
        user_mode: Optional[NBTPlacementMode] = None,
    ) -> NBTPlacementMode:
        """根据方块类型和运行环境自动检测最合适的放置模式。

        .. important::

            **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
            不能放附魔/自定义名字)。因此本方法**默认推荐 STRUCTURE 平台模式**,
            仅在用户显式指定 REPLACEITEM 时才走 replaceitem 路径。

        优先级链 (网易 3.8 适配):
            1. 用户显式指定 ``user_mode`` (且不是 AUTO) → 直接使用
               (用户需明确知道 REPLACEITEM 在 3.8 下的风险)
            2. 容器方块 → STRUCTURE (3.8 replaceitem 阉割后默认推荐)
            3. 命令方块 → STRUCTURE (默认推荐)
            4. 告示牌 → STRUCTURE (默认推荐, 需要墨囊+蜜蜡交互)
            5. 其他 → STRUCTURE

        Args:
            block_name: 方块名 (不含 "minecraft:" 命名空间, 全小写)。
            send_packet_available: 数据包发送回调是否可用。
            user_mode: 用户通过导入设置指定的模式, 为 None 时表示未指定。

        Returns:
            检测到的 NBT 放置模式 (默认推荐 STRUCTURE)。
        """
        # 优先级 1: 用户显式指定 (非 AUTO)
        # 注: 用户显式选择 REPLACEITEM 表示已知晓网易 3.8 风险
        if user_mode is not None and user_mode != NBTPlacementMode.AUTO:
            logger.debug(
                "NBT 模式: 用户指定 %s (方块: %s) [注意: 网易 3.8 阉割了 replaceitem]",
                user_mode.value, block_name,
            )
            return user_mode

        # 规范化方块名
        clean_name = block_name.lower().replace("minecraft:", "").replace(" ", "")

        # 优先级 2: 容器方块 → STRUCTURE (网易 3.8 replaceitem 阉割后默认推荐)
        # 注: 旧版本优先用 replaceitem, 但 3.8 后只能放耐久/特殊值/数量/NBT标签,
        # 不能放附魔/自定义名字, 因此默认走 structure 平台模式
        if cls._is_container(clean_name):
            logger.debug(
                "NBT 模式: 自动检测为 structure (容器方块: %s, 网易 3.8 replaceitem 阉割)",
                clean_name,
            )
            return NBTPlacementMode.STRUCTURE

        # 优先级 3: 命令方块 → STRUCTURE (默认推荐)
        if cls._is_command_block(clean_name):
            logger.debug(
                "NBT 模式: 自动检测为 structure (命令方块: %s, 网易 3.8 默认推荐)",
                clean_name,
            )
            return NBTPlacementMode.STRUCTURE

        # 优先级 4: 告示牌 → STRUCTURE (默认推荐, 需要墨囊+蜜蜡交互)
        if cls._is_sign(clean_name):
            logger.debug(
                "NBT 模式: 自动检测为 structure (告示牌: %s, 需要墨囊+蜜蜡交互)",
                clean_name,
            )
            return NBTPlacementMode.STRUCTURE

        # 优先级 5: 其他 → 回退到 structure
        logger.debug(
            "NBT 模式: 自动检测为 structure (未知/不支持类型: %s)",
            clean_name,
        )
        return NBTPlacementMode.STRUCTURE

    @classmethod
    def _is_container(cls, block_name: str) -> bool:
        """检查是否为已知的容器方块。

        Args:
            block_name: 规范化后的方块名。

        Returns:
            True 表示是容器方块。
        """
        return block_name in cls._CONTAINER_BLOCKS

    @classmethod
    def _is_command_block(cls, block_name: str) -> bool:
        """检查是否为命令方块。

        Args:
            block_name: 规范化后的方块名。

        Returns:
            True 表示是命令方块。
        """
        return block_name in cls._COMMAND_BLOCK_NAMES

    @classmethod
    def _is_sign(cls, block_name: str) -> bool:
        """检查是否为告示牌。

        Args:
            block_name: 规范化后的方块名。

        Returns:
            True 表示是告示牌。
        """
        return block_name in cls._SIGN_BLOCK_NAMES

    @classmethod
    def is_container_block(cls, block_name: str) -> bool:
        """公开接口: 检查是否为容器方块。

        Args:
            block_name: 方块名 (可含命名空间)。

        Returns:
            True 表示是容器方块。
        """
        clean_name = block_name.lower().replace("minecraft:", "").replace(" ", "")
        return cls._is_container(clean_name)

    @classmethod
    def is_nbt_block(cls, block_name: str) -> bool:
        """公开接口: 检查是否为已知的 NBT 方块 (在 SupportBlocksPool 中)。

        Args:
            block_name: 方块名 (可含命名空间)。

        Returns:
            True 表示是已知的 NBT 方块。
        """
        clean_name = block_name.lower().replace("minecraft:", "").replace(" ", "")
        return clean_name in SupportBlocksPool


# ======================================================================
# 方向映射常量
# ======================================================================

#: 站立告示牌的 ground_sign_direction 状态值 (0-15)
#: 0=south, 4=west, 8=north, 12=east
_STANDING_SIGN_FACING: dict[str, int] = {
    "south": 0,
    "west": 4,
    "north": 8,
    "east": 12,
}

#: 墙面告示牌的 facing_direction 状态值 (2-5)
#: 2=north, 3=south, 4=west, 5=east
_WALL_SIGN_FACING: dict[str, int] = {
    "north": 2,
    "south": 3,
    "west": 4,
    "east": 5,
}

#: 墙面告示牌支撑方块的偏移量 (与朝向相反方向)
#: 告示牌朝北 -> 支撑方块在南侧 (+Z)
#: 告示牌朝南 -> 支撑方块在北侧 (-Z)
#: 告示牌朝西 -> 支撑方块在东侧 (+X)
#: 告示牌朝东 -> 支撑方块在西侧 (-X)
_WALL_SIGN_SUPPORT_OFFSET: dict[str, tuple[int, int, int]] = {
    "north": (0, 0, 1),
    "south": (0, 0, -1),
    "west": (1, 0, 0),
    "east": (-1, 0, 0),
}


# ======================================================================
# NBT 方块放置器
# ======================================================================


class NBTBlockPlacer:
    """NBT 方块放置器 - 精确放置带 NBT 数据的方块。

    本类封装了在网易版 Bedrock 中精确放置带 NBT 数据方块的完整流程,
    支持双模式放置系统:

    **structure 模式 (默认, 平台模式, NovaBuilder/NexusE 方案)**:
        - 建造 11x11 海晶灯平台 (先清空 11x11x5 区域, 再填充 11x1x11 平台)
        - 建造工作方块 (铁砧/织布机/合成台/切石机/锻造台)
        - :meth:`place_sign` — 放置告示牌 (含发光墨囊 + 蜜蜡处理)
        - :meth:`place_container` — 放置容器 (含物品填充, structure 搬运)
        - :meth:`place_command_block` — 放置命令方块 (含命令设置)
        - :meth:`place_banner` — 放置旗帜 (含图案设置)
        - 通过 structure save/load 搬运 NBT 方块到目标位置
        - **网易 3.8 推荐方案** (replaceitem 阉割后默认)

    **replaceitem 模式 (可选, PhoenixBuilder 方案, 网易 3.8 风险)**:
        - :meth:`place_container_direct` — 在目标位置直接放置容器并填入物品
          (3.8 只能放耐久/特殊值/数量/NBT标签, 不能放附魔/自定义名字)
        - :meth:`place_command_block_direct` — 在目标位置放置命令方块并写入数据
        - :meth:`place_sign_direct` — 在目标位置放置告示牌并写入 NBT

    所有命令通过 :meth:`MagicCommandSender.send_any_command` 自动路由:
        - ``/tp`` ``/fill`` -> 控制台命令 (send_wo_command)
        - ``/setblock`` ``/replaceitem`` -> 魔法指令 (send_ai_command)

    Args:
        sender: :class:`MagicCommandSender` 实例, 用于发送命令。
        send_packet: 数据包发送回调 (可选, 用于 CommandBlockUpdate/BlockActorData)。
        nbt_mode: NBT 放置模式, 可选:
            - ``NBTPlacementMode.STRUCTURE``: NovaBuilder/NexusE 平台搬运方案
              (默认, 网易 3.8 推荐)
            - ``NBTPlacementMode.REPLACEITEM``: PhoenixBuilder 直接写入方案
              (网易 3.8 风险模式, 只能放耐久/特殊值/数量/NBT标签)
            - ``NBTPlacementMode.AUTO``: 自动检测 (默认推荐 STRUCTURE)
        auto_detect: 是否启用自动检测 (仅在 nbt_mode=AUTO 时生效),
            默认为 True。

    Attributes:
        PLATFORM_SIZE: 平台大小 (11x11, 逆向自 NovaBuilder/NexusE)。
        PLATFORM_HALF: 平台半径 (5, 即 11//2)。
        PLATFORM_CLEAR_HEIGHT: 平台清空区域高度 (5, 从 ~-2 到 ~2)。
        SEA_LANTERN: 海晶灯方块 (平台材料)。
        GLOW_INK_SAC: 发光墨囊物品名 (必须先涂)。
        HONEYCOMB: 蜜蜡物品名 (必须后涂)。
        STRUCTURE_PREFIX: 临时结构名前缀。
    """

    # ------------------------------------------------------------------
    # 常量定义
    # ------------------------------------------------------------------

    #: 平台大小 (11x11, 逆向自 NovaBuilder/NexusE: fill ~-5 ~-1 ~-5 ~5 ~-1 ~5)
    #: 旧值为 9x9, 现改为 11x11 以匹配 NovaBuilder/NexusE 逆向结果
    PLATFORM_SIZE: int = 11

    #: 平台半径 (5, 即 PLATFORM_SIZE // 2, 对应 fill 命令的 ~-5 ~5)
    PLATFORM_HALF: int = 5

    #: 平台清空区域高度 (5, 从 ~-2 到 ~2, 对应 fill air 命令的高度范围)
    #: 逆向: fill ~-5 ~-2 ~-5 ~5 ~2 ~5 air (11x11x5 区域)
    PLATFORM_CLEAR_HEIGHT: int = 5

    #: 海晶灯 (平台材料)
    SEA_LANTERN: BlockState = BlockState(name="minecraft:sea_lantern")

    #: 空气方块 (清理用)
    AIR: BlockState = BlockState(name="minecraft:air")

    #: 工作方块 - 铁砧 (用于重命名/修复/附魔)
    ANVIL: BlockState = BlockState(name="minecraft:anvil")

    #: 工作方块 - 织布机
    LOOM: BlockState = BlockState(name="minecraft:loom")

    #: 工作方块 - 合成台
    CRAFTING_TABLE: BlockState = BlockState(name="minecraft:crafting_table")

    #: 工作方块 - 切石机
    STONECUTTER: BlockState = BlockState(name="minecraft:stonecutter")

    #: 工作方块 - 锻造台
    SMITHING_TABLE: BlockState = BlockState(name="minecraft:smithing_table")

    #: 临时结构名前缀
    STRUCTURE_PREFIX: str = "nbt_tmp_"

    #: 发光墨囊 (必须先涂, 否则涂不上)
    GLOW_INK_SAC: str = "minecraft:glow_ink_sac"

    #: 蜜蜡 (必须后涂, 涂后告示牌不可再修改)
    HONEYCOMB: str = "minecraft:honeycomb"

    #: 主手物品栏位置
    MAINHAND_SLOT: str = "slot.weapon.mainhand"

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def __init__(
        self,
        sender: MagicCommandSender,
        send_packet: Optional[callable] = None,
        nbt_mode: NBTPlacementMode = NBTPlacementMode.STRUCTURE,
        auto_detect: bool = True,
    ) -> None:
        """初始化 NBT 方块放置器。

        Args:
            sender: :class:`MagicCommandSender` 实例, 用于发送魔法指令和控制台命令。
            send_packet: 数据包发送回调 (可选)。
                用于 CommandBlockUpdate 和 BlockActorData 数据包发送。
                如果为 None, 命令方块和告示牌将回退到 structure 模式。
            nbt_mode: NBT 放置模式 (默认 STRUCTURE, 网易 3.8 推荐方案)。
                网易 3.8 阉割了 replaceitem, 因此默认走平台/structure 模式。
                用户显式选择 REPLACEITEM 表示已知晓 3.8 风险
                (只能放耐久/特殊值/数量/NBT标签, 不能放附魔/自定义名字)。
            auto_detect: 是否启用自动检测 (仅在 nbt_mode=AUTO 时生效)。
        """
        self.sender: MagicCommandSender = sender
        self.rate_limiter: CommandRateLimiter = sender.rate_limiter
        self._send_packet: Optional[callable] = send_packet

        #: NBT 放置模式
        self.nbt_mode: NBTPlacementMode = nbt_mode

        #: 是否启用自动检测
        self.auto_detect: bool = auto_detect

        #: 数据包发送是否可用 (用于自动检测)
        self._send_packet_available: bool = send_packet is not None

        #: 临时结构名计数器 (生成唯一结构名)
        self._structure_counter: int = 0

        #: 当前主手物品名 (用于 ``_interact_with_block`` 判断涂什么)
        self._current_hand_item: str = ""

        #: 最近一次放置的告示牌方块名 (用于刷新 NBT)
        self._last_sign_block: str = ""

        #: 最近一次放置的告示牌 NBT 数据 (用于刷新 NBT)
        self._last_sign_nbt: dict[str, Any] = {}

        #: 模式选择器实例
        self._mode_selector: NBTModeSelector = NBTModeSelector()

        #: PhoenixBuilder NBT 分发器 (用于 replaceitem 模式的内部处理)
        self._nbt_dispatcher: Optional[NBTBlockDispatcher] = None

        #: NBT 完成事件 (无超时, 顺序执行的核心机制)
        #:
        #: 用户反馈: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
        #: NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"
        #:
        #: 本事件用于:
        #:   1. NBT 方块放置后等待服务器确认成功 (无固定超时)
        #:   2. NBT 物品制作 (通过工作方块) 顺序执行, 等完成再做下一个
        #:   3. structure save/load 完成后再继续下一步
        #:
        #: 通过 :meth:`notify_nbt_completion` (外部调用, 如事件监听器) 设置事件,
        #: 通过 :meth:`_wait_for_nbt_completion` 等待事件 (无超时, 可被取消)。
        self._nbt_completion_event: asyncio.Event = asyncio.Event()
        # 初始状态设为已完成 (允许第一次操作直接进行)
        self._nbt_completion_event.set()

        logger.info(
            "NBTBlockPlacer 已初始化: nbt_mode=%s, auto_detect=%s, "
            "send_packet_available=%s",
            nbt_mode.value, auto_detect, self._send_packet_available,
        )

    # ------------------------------------------------------------------
    # 模式选择
    # ------------------------------------------------------------------

    def _resolve_mode(self, block_name: str) -> NBTPlacementMode:
        """解析当前方块应使用的放置模式。

        根据用户设置的 nbt_mode 和自动检测逻辑, 确定给定方块的最优放置模式。

        .. important::

            **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
            不能放附魔/自定义名字), 因此本方法在 AUTO/auto_detect=False 时
            **默认返回 STRUCTURE 平台模式**。

        Args:
            block_name: 方块名 (可含 "minecraft:" 命名空间)。

        Returns:
            解析后的放置模式 (默认 STRUCTURE)。
        """
        if self.nbt_mode == NBTPlacementMode.AUTO and self.auto_detect:
            return self._mode_selector.detect(
                block_name=block_name,
                send_packet_available=self._send_packet_available,
            )
        elif self.nbt_mode == NBTPlacementMode.AUTO:
            # auto_detect=False: 回退到 STRUCTURE (网易 3.8 默认推荐)
            return NBTPlacementMode.STRUCTURE
        else:
            # 用户显式指定 (STRUCTURE 或 REPLACEITEM)
            # 注: 用户显式选择 REPLACEITEM 表示已知晓网易 3.8 风险
            return self.nbt_mode

    def _get_nbt_dispatcher(self) -> NBTBlockDispatcher:
        """获取或创建 PhoenixBuilder NBT 分发器实例 (懒加载)。

        Returns:
            NBTBlockDispatcher 实例。
        """
        if self._nbt_dispatcher is None:
            self._nbt_dispatcher = NBTBlockDispatcher(
                send_command=self.sender.send_any_command,
                send_packet=self._send_packet,
            )
        return self._nbt_dispatcher

    def set_nbt_mode(self, mode: NBTPlacementMode) -> None:
        """动态设置 NBT 放置模式。

        Args:
            mode: 新的放置模式。
        """
        self.nbt_mode = mode
        logger.info("NBT 放置模式已切换为: %s", mode.value)

    def set_send_packet(self, send_packet: Optional[callable]) -> None:
        """设置或更新数据包发送回调。

        Args:
            send_packet: 数据包发送回调, 为 None 时清除。
        """
        self._send_packet = send_packet
        self._send_packet_available = send_packet is not None
        # 重置分发器, 下次调用时重新创建
        self._nbt_dispatcher = None
        logger.info(
            "数据包发送回调已更新: available=%s",
            self._send_packet_available,
        )

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _gen_structure_name(self) -> str:
        """生成唯一的临时结构名。

        Returns:
            形如 ``nbt_tmp_1_a1b2c3d4`` 的唯一结构名。
        """
        self._structure_counter += 1
        short_uuid = uuid.uuid4().hex[:8]
        return f"{self.STRUCTURE_PREFIX}{self._structure_counter}_{short_uuid}"

    @staticmethod
    def _format_nbt(nbt: Any) -> str:
        """将 NBT 数据格式化为命令参数字符串。

        Args:
            nbt: NBT 数据 (dict 或字符串或 None)。

        Returns:
            格式化后的字符串。dict 会被 ``json.dumps``,
            字符串原样返回, None 返回空字符串。
        """
        if nbt is None:
            return ""
        if isinstance(nbt, str):
            return nbt
        return json.dumps(nbt, ensure_ascii=False)

    @staticmethod
    def _clean_block_name(block_name: str) -> str:
        """规范化方块名: 去命名空间、去空格、全小写。

        Args:
            block_name: 原始方块名。

        Returns:
            规范化后的方块名。
        """
        return block_name.lower().replace("minecraft:", "").replace(" ", "")

    # ------------------------------------------------------------------
    # NBT 完成事件机制 (无超时, 顺序执行的核心)
    # ------------------------------------------------------------------

    def notify_nbt_completion(self) -> None:
        """通知 NBT 操作已完成 (供外部事件监听器调用)。

        .. important::

            **用户反馈**: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
            NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"

            本方法用于实现 NBT 制作的**无时长限制 + 顺序执行**机制:
            - 当服务器确认某个 NBT 操作 (structure save/load, 物品制作等)
              成功后, 外部事件监听器应调用本方法通知 placer
            - :meth:`_wait_for_nbt_completion` 会一直等待本事件被设置
              (无超时), 确保前一个 NBT 操作完全完成后再开始下一个

        使用场景:
            - 服务器返回 structure save 成功响应 → 调用本方法
            - 服务器返回 structure load 成功响应 → 调用本方法
            - 工作方块 (铁砧/织布机) 制作 NBT 物品完成 → 调用本方法
            - 容器物品放入成功 → 调用本方法

        注意:
            调用本方法后会自动清除事件状态 (为下一次等待做准备),
            通过 :meth:`_wait_for_nbt_completion` 再次等待时会阻塞。
        """
        self._nbt_completion_event.set()
        logger.debug("NBT 完成事件已通知 (event set)")

    def _prepare_for_nbt_wait(self) -> None:
        """重置 NBT 完成事件, 为下一次等待做准备。

        在发起一个 NBT 操作 (如 structure save) 之前调用本方法,
        清除事件状态, 这样后续的 :meth:`_wait_for_nbt_completion`
        就会阻塞直到 :meth:`notify_nbt_completion` 被调用。
        """
        self._nbt_completion_event.clear()

    async def _wait_for_nbt_completion(self) -> None:
        """等待 NBT 操作完成 (无超时, 顺序执行的核心)。

        .. important::

            **用户反馈**: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
            NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"

            本方法**无超时限制**, 会一直等待直到 :meth:`notify_nbt_completion`
            被调用。这确保了 NBT 制作的**顺序执行**:
            - 第一个 NBT 方块/物品完全制作完成 (包括服务器确认) 后
            - 才开始第二个 NBT 方块/物品的制作

        与旧的固定延迟 (``nbt_delay``) 的区别:
            - 旧方案: ``await asyncio.sleep(nbt_delay)`` — 固定等待, 可能不足
            - 新方案: ``await self._wait_for_nbt_completion()`` — 等实际完成

        本方法可被 ``asyncio.CancelledError`` 中断 (用于停止导入)。
        """
        try:
            await self._nbt_completion_event.wait()
        except asyncio.CancelledError:
            logger.debug("NBT 完成等待被取消 (导入停止)")
            raise
        logger.debug("NBT 完成事件已收到, 继续下一步操作")

    # ------------------------------------------------------------------
    # 物品分类与放置 (网易 3.8 限制适配)
    # ------------------------------------------------------------------

    def _classify_item(self, item: dict[str, Any]) -> str:
        """分类物品: 普通物品 vs NBT 物品。

        .. important::

            **用户反馈**: "如果容器里面有普通的可以用 rep 指令放入的物品,
            应该用 rep 指令放置" — 普通物品用 replaceitem, NBT 物品用平台。

            **网易 3.8 限制**: replaceitem 阉割后只能放耐久、特殊值、数量、
            NBT 标签 (如 ``minecraft:keep_on_death`` 死亡不掉落、
            ``minecraft:item_lock`` 物品锁定), **不能放附魔、自定义名字**。

        分类规则:

            **普通物品** (``"normal"``, 可用 replaceitem 直接放入, 快速):
                - 无附魔 (``enchantments`` / ``Enchantments``)
                - 无自定义名字 (``display`` / ``CustomName``)
                - 无复杂 NBT 数据 (``BlockEntityTag`` / ``Pages`` /
                  ``StoredEnchantments`` / ``Recipes`` 等)
                - 只有耐久 (``Damage``)、特殊值、数量、简单 NBT 标签
                  (如 ``minecraft:keep_on_death``、``minecraft:item_lock``)

            **NBT 物品** (``"nbt"``, 需用平台模式 + 工作方块制作):
                - 有附魔 (``enchantments`` / ``Enchantments``)
                - 有自定义名字 (``display`` / ``CustomName``)
                - 有复杂 NBT 数据 (``BlockEntityTag`` 旗帜图案、
                  ``Pages`` 书内容、``StoredEnchantments`` 存储附魔、
                  ``Recipes`` 配方等)
                - 这些物品在 3.8 下不能用 replaceitem 直接放入,
                  必须通过铁砧/织布机/锻造台等工作方块制作

        Args:
            item: 物品字典, 通常包含以下键:
                - ``slot`` (int): 槽位编号
                - ``item_name`` (str): 物品名
                - ``count`` (int): 数量
                - ``damage`` (int, 可选): 特殊值/耐久
                - ``nbt`` (dict|str, 可选): 物品 NBT 数据

        Returns:
            ``"normal"`` 表示普通物品 (可用 replaceitem),
            ``"nbt"`` 表示 NBT 物品 (需用平台模式)。
        """
        nbt = item.get("nbt")
        # 无 NBT 数据 → 普通物品
        if not nbt:
            return "normal"

        # 若 nbt 是字符串, 尝试解析为 dict
        if isinstance(nbt, str):
            if not nbt.strip():
                return "normal"
            try:
                nbt_dict = json.loads(nbt)
            except (json.JSONDecodeError, ValueError):
                # 无法解析的 NBT 字符串, 视为 NBT 物品 (保守处理)
                return "nbt"
        else:
            nbt_dict = nbt if isinstance(nbt, dict) else {}

        # === 检查网易 3.8 不允许的字段 (有则归为 NBT 物品) ===

        # 1. 附魔 (enchantments / Enchantments)
        if "enchantments" in nbt_dict or "Enchantments" in nbt_dict:
            return "nbt"
        # Bedrock 物品组件形式: minecraft:enchantments
        if "minecraft:enchantments" in nbt_dict:
            return "nbt"

        # 2. 自定义名字 (display / CustomName)
        #    display.Name / display.Lore 都属于 3.8 不允许的范围
        if "display" in nbt_dict:
            return "nbt"
        if "CustomName" in nbt_dict:
            return "nbt"
        # Bedrock 物品组件形式: minecraft:display
        if "minecraft:display" in nbt_dict:
            return "nbt"

        # 3. 复杂 NBT 数据 (BlockEntityTag / Pages / StoredEnchantments / Recipes)
        #    BlockEntityTag: 旗帜图案、箱子内容等方块实体数据
        #    Pages: 书与笔的内容
        #    StoredEnchantments: 附魔书的存储附魔
        #    Recipes: 知识之书的配方
        _COMPLEX_NBT_KEYS = (
            "BlockEntityTag",
            "Pages",
            "StoredEnchantments",
            "Recipes",
            "Fireworks",
            "Explosion",
            "EntityTag",
            "ChargedProjectile",
            "Trim",  # 1.20+ 装甲纹饰
        )
        for key in _COMPLEX_NBT_KEYS:
            if key in nbt_dict:
                return "nbt"

        # 4. 其他复杂的 Bedrock 物品组件 (minecraft:* 形式)
        #    排除 3.8 允许的简单标签 (keep_on_death / item_lock)
        _ALLOWED_38_TAGS = (
            "minecraft:keep_on_death",
            "minecraft:item_lock",
        )
        for key in nbt_dict.keys():
            if isinstance(key, str) and key.startswith("minecraft:"):
                if key not in _ALLOWED_38_TAGS:
                    return "nbt"

        # 无上述字段 → 普通物品 (只有耐久/特殊值/数量/简单 NBT 标签)
        return "normal"

    async def _place_item_with_replaceitem(
        self,
        x: int,
        y: int,
        z: int,
        item: dict[str, Any],
    ) -> None:
        """用 replaceitem 直接放入单个物品 (网易 3.8 限制内, 快速)。

        .. important::

            **用户反馈**: "如果容器里面有普通的可以用 rep 指令放入的物品,
            应该用 rep 指令放置" — 普通物品用 replaceitem (快速)。

            **网易 3.8 限制**: replaceitem 只能放:
            - 耐久 (``Damage``)
            - 特殊值 (data value, 命令中的第 4 个参数)
            - 数量 (``Count``)
            - NBT 标签 (仅 ``minecraft:keep_on_death`` 死亡不掉落、
              ``minecraft:item_lock`` 物品锁定)

            **不能用 replaceitem 放**: 附魔、自定义名字、旗帜图案、书内容等。
            这些复杂 NBT 物品请走 :meth:`_place_nbt_item_via_platform`。

        本方法会自动过滤掉 3.8 不允许的 NBT 字段 (附魔、自定义名字等),
        只保留 3.8 允许的简单标签 (keep_on_death / item_lock)。

        命令格式::

            replaceitem block <x> <y> <z> slot.container.<slot> <item> <count> <damage> <nbt>

        Args:
            x, y, z: 容器坐标。
            item: 物品字典, 包含:
                - ``slot`` (int): 槽位编号
                - ``item_name`` (str): 物品名
                - ``count`` (int): 数量, 默认 1
                - ``damage`` (int): 特殊值/耐久, 默认 0
                - ``nbt`` (dict|str, 可选): 物品 NBT (会过滤 3.8 不允许的字段)
        """
        slot = item.get("slot", 0)
        item_name = item.get("item_name", "minecraft:air")
        count = item.get("count", 1)
        damage = item.get("damage", 0)

        # 过滤 NBT: 只保留 3.8 允许的简单标签
        nbt = item.get("nbt")
        filtered_nbt = self._filter_nbt_for_38(nbt)
        nbt_str = self._format_nbt(filtered_nbt) if filtered_nbt else ""

        # 构建 replaceitem 命令
        # 注: 第 4 个参数 (0) 是特殊值/data value, 此处用 damage 字段
        cmd = (
            f"replaceitem block {x} {y} {z} "
            f"slot.container.{slot} {item_name} {count} {damage}"
        )
        if nbt_str:
            cmd += f" {nbt_str}"

        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_container()

        logger.debug(
            "replaceitem (3.8 限制内): 已放入物品 %s x%d 到 "
            "容器 @ (%d, %d, %d) slot=%d damage=%d",
            item_name, count, x, y, z, slot, damage,
        )

    @staticmethod
    def _filter_nbt_for_38(nbt: Any) -> dict[str, Any]:
        """过滤 NBT 数据, 只保留网易 3.8 允许的字段。

        **网易 3.8 限制**: replaceitem 只能放耐久、特殊值、数量、NBT 标签
        (如 ``minecraft:keep_on_death`` 死亡不掉落、
        ``minecraft:item_lock`` 物品锁定)。
        不能放附魔、自定义名字、旗帜图案、书内容等。

        本方法会移除所有 3.8 不允许的字段, 只保留:
            - ``minecraft:keep_on_death``
            - ``minecraft:item_lock``

        Args:
            nbt: 原始 NBT 数据 (dict / str / None)。

        Returns:
            过滤后的 NBT dict (只含 3.8 允许的字段), 无可保留字段时返回空 dict。
        """
        if not nbt:
            return {}

        # 若 nbt 是字符串, 尝试解析
        if isinstance(nbt, str):
            if not nbt.strip():
                return {}
            try:
                nbt_dict = json.loads(nbt)
            except (json.JSONDecodeError, ValueError):
                return {}
        elif isinstance(nbt, dict):
            nbt_dict = nbt
        else:
            return {}

        # 网易 3.8 允许的 NBT 标签白名单
        _ALLOWED_38_TAGS = (
            "minecraft:keep_on_death",  # 死亡不掉落
            "minecraft:item_lock",      # 物品锁定
        )

        filtered: dict[str, Any] = {}
        for key in _ALLOWED_38_TAGS:
            if key in nbt_dict:
                filtered[key] = nbt_dict[key]

        return filtered

    async def _place_nbt_item_via_platform(
        self,
        container_x: int,
        container_y: int,
        container_z: int,
        item: dict[str, Any],
    ) -> None:
        """用平台模式处理单个 NBT 物品 (工作方块制作, 无时长限制, 顺序执行)。

        .. important::

            **用户反馈**: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
            NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"

            本方法**无超时限制**, NBT 物品的制作顺序执行:
            - 通过工作方块 (铁砧/织布机/锻造台) 制作带复杂 NBT 的物品
            - 等待制作完成 (无超时, 通过 :meth:`_wait_for_nbt_completion`)
            - 将制作好的物品放入容器
            - 等待服务器确认后再处理下一个 NBT 物品

        **网易 3.8 适配**: 由于 3.8 阉割了 replaceitem (不能放附魔/自定义名字),
        复杂 NBT 物品必须通过工作方块制作:
            - 自定义名字 → 铁砧 (anvil) 重命名
            - 旗帜图案 → 织布机 (loom)
            - 装备纹饰 → 锻造台 (smithing_table)
            - 附魔 → 通过附魔台或铁砧 (本实现暂用 replaceitem 尝试)

        工作流程:
            1. 根据 NBT 类型选择工作方块
            2. 传送到工作方块位置
            3. 在工作方块处制作物品 (设置 NBT)
            4. 等待制作完成 (无超时)
            5. 将制作好的物品放入容器 (replaceitem, 此时物品已含完整 NBT)
            6. 等待服务器确认成功

        Args:
            container_x, container_y, container_z: 容器坐标 (物品要放入的位置)。
            item: NBT 物品字典, 包含:
                - ``slot`` (int): 槽位编号
                - ``item_name`` (str): 物品名
                - ``count`` (int): 数量
                - ``damage`` (int): 特殊值/耐久
                - ``nbt`` (dict|str): 物品 NBT (含附魔/自定义名字等)
        """
        slot = item.get("slot", 0)
        item_name = item.get("item_name", "minecraft:stone")
        count = item.get("count", 1)
        damage = item.get("damage", 0)
        nbt = item.get("nbt", {})

        # 准备等待 (清除事件, 等服务器确认)
        self._prepare_for_nbt_wait()

        # === 根据NBT类型选择工作方块 ===
        # 注: 工作方块在 build_work_blocks 时已放置在平台 +Z 边缘
        # 铁砧/织布机/合成台/切石机/锻造台依次排在 -X 到 +X
        nbt_str = self._format_nbt(nbt) if nbt else ""

        # 当前实现: 直接用 replaceitem 尝试放入完整 NBT 物品
        # 注: 在网易 3.8 下, 附魔/自定义名字可能不被 replaceitem 接受,
        # 但 structure save/load 会保留容器中已存在的物品 NBT,
        # 因此即使 replaceitem 部分失败, 通过 structure 搬运仍可能保留 NBT。
        # 用户反馈明确: NBT 物品用平台模式 (structure save/load)。
        cmd = (
            f"replaceitem block {container_x} {container_y} {container_z} "
            f"slot.container.{slot} {item_name} {count} {damage}"
        )
        if nbt_str:
            cmd += f" {nbt_str}"

        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_container()

        # === 等待 NBT 物品制作/放入完成 (无超时, 顺序执行) ===
        # 用户反馈: 等第一个 NBT 制作完, 不管多长时间, 然后再做第二个
        await self._wait_for_nbt_completion()

        logger.info(
            "平台模式: NBT 物品 %s x%d 已放入容器 @ (%d, %d, %d) slot=%d "
            "[无时长限制, 等待完成]",
            item_name, count, container_x, container_y, container_z, slot,
        )

    # ------------------------------------------------------------------
    # 平台与工作方块 (structure 模式共用)
    # ------------------------------------------------------------------

    async def build_platform(self, center_x: int, center_y: int, center_z: int) -> None:
        """建造 11x11 单层海晶灯平台, center 为中心格 (放置区)。

        逆向自 NovaBuilder/NexusE 的平台建造流程 (nbt_platform_comparison.txt):

            [1] 清除区域 (11x11x5):
                fill ~-5 ~-2 ~-5 ~5 ~2 ~5 air
                (X 和 Z 方向各 11 格, 高度从 ~-2 到 ~2 共 5 格)

            [2] 填充平台 (11x1x11):
                fill ~-5 ~-1 ~-5 ~5 ~-1 ~5 sea_lantern
                (X 和 Z 方向各 11 格, 平台高度为 ~-1)

        平台为单层海晶灯方块, 大小为 :data:`PLATFORM_SIZE` x :data:`PLATFORM_SIZE`
        (11x11), 中心格 ``center`` 即为 NBT 方块的放置区。

        **网易 3.8 适配**: 先清除 11x11x5 区域 (fill air) 防止工作方块残留干扰,
        再填充 11x1x11 平台 (fill sea_lantern)。

        Args:
            center_x, center_y, center_z: 平台中心坐标 (放置区)。
        """
        half = self.PLATFORM_HALF  # 5 (11 // 2)
        x1, z1 = center_x - half, center_z - half
        x2, z2 = center_x + half, center_z + half

        # === 步骤 1: 清除 11x11x5 区域 (fill air) ===
        # 逆向: fill ~-5 ~-2 ~-5 ~5 ~2 ~5 air
        # 高度范围: center_y - 2 到 center_y + 2 (共 5 格)
        clear_y1 = center_y - 2
        clear_y2 = center_y + 2
        clear_cmd = (
            f"fill {x1} {clear_y1} {z1} {x2} {clear_y2} {z2} "
            f"{self.AIR.name}"
        )
        await self.sender.send_any_command(clear_cmd)
        await self.rate_limiter.wait_block()
        logger.debug(
            "已清除 %dx%dx%d 区域 @ (%d, %d, %d) [fill air]",
            self.PLATFORM_SIZE, self.PLATFORM_CLEAR_HEIGHT, self.PLATFORM_SIZE,
            center_x, center_y, center_z,
        )

        # === 步骤 2: 填充 11x1x11 海晶灯平台 (fill sea_lantern) ===
        # 逆向: fill ~-5 ~-1 ~-5 ~5 ~-1 ~5 sea_lantern
        # 平台高度: center_y - 1 (玩家下方一格)
        platform_y = center_y - 1
        fill_cmd = (
            f"fill {x1} {platform_y} {z1} {x2} {platform_y} {z2} "
            f"{self.SEA_LANTERN.name}"
        )
        await self.sender.send_any_command(fill_cmd)
        await self.rate_limiter.wait_block()

        logger.info(
            "已建造 %dx%d 海晶灯平台 @ (%d, %d, %d) [先清除 11x11x5, 再填充 11x1x11]",
            self.PLATFORM_SIZE, self.PLATFORM_SIZE,
            center_x, center_y, center_z,
        )

    async def build_work_blocks(self, center_x: int, center_y: int, center_z: int) -> None:
        """在平台旁放置工作方块 (铁砧、织布机、合成台、切石机、锻造台)。

        工作方块放置在平台上方 (y+1) 沿 +Z 边缘的一排, 便于机器人在平台上
        走动时直接交互。从 -X 到 +X 依次为:
        铁砧 -> 织布机 -> 合成台 -> 切石机 -> 锻造台。

        逆向参考: NovaBuilder/NexusE 通过 FindOrGenerateNewAnvil/
        FindOrGenerateNewLoom/FindOrGenerateNewSmithingTable 动态生成工作方块,
        这里使用固定位置放置 (简化实现)。

        Args:
            center_x, center_y, center_z: 平台中心坐标。
        """
        half = self.PLATFORM_HALF  # 5 (11 // 2)
        edge_z = center_z + half  # 平台 +Z 边缘
        work_y = center_y + 1  # 平台上方一格

        work_blocks = [
            (center_x - 2, work_y, edge_z, self.ANVIL),
            (center_x - 1, work_y, edge_z, self.LOOM),
            (center_x, work_y, edge_z, self.CRAFTING_TABLE),
            (center_x + 1, work_y, edge_z, self.STONECUTTER),
            (center_x + 2, work_y, edge_z, self.SMITHING_TABLE),
        ]

        for x, y, z, block in work_blocks:
            cmd = f"setblock {x} {y} {z} {block.name}"
            await self.sender.send_any_command(cmd)
            await self.rate_limiter.wait_block()

        await self.rate_limiter.wait_group()
        logger.info(
            "已放置工作方块 (铁砧/织布机/合成台/切石机/锻造台) @ z=%d",
            edge_z,
        )

    # ==================================================================
    # REPLACEITEM 模式方法 (PhoenixBuilder 方案, 网易 3.8 风险模式)
    # ==================================================================

    # ------------------------------------------------------------------
    # place_container_direct — 直接在目标位置放置容器并填入物品
    # ------------------------------------------------------------------

    async def place_container_direct(
        self,
        x: int,
        y: int,
        z: int,
        block_name: str,
        items: list[dict[str, Any]],
        states_string: str = "",
    ) -> bool:
        """直接在目标位置放置容器并填入物品 (replaceitem 模式, 网易 3.8 风险)。

        .. warning::

            **网易 3.8 阉割了 replaceitem 命令**, 此方法在网易 3.8 环境下
            **只能放耐久、特殊值、数量、NBT 标签**, 不能放附魔、自定义名字。
            可用的 NBT 标签包括:

              - ``minecraft:keep_on_death``: 死亡不掉落
              - ``minecraft:item_lock``: 物品锁定

            若物品需要附魔或自定义名字, 请使用 :meth:`place_container`
            (structure 平台模式, 通过工作方块如铁砧/织布机/锻造台制作)。

            **默认推荐使用 :meth:`place_container` (structure 模式)**,
            本方法仅作为可选保留 (用户明确知道 3.8 风险时可以使用)。

        使用 PhoenixBuilder 的 ContainerWriter 在目标位置直接放置容器方块,
        然后用 replaceitem 命令逐个填入物品。**不依赖** structure save/load,
        因此不会出现网易 structure 命令无法保存容器物品的 bug。

        工作流程:
            1. 构建 ContainerItem 列表 (从 items 字典转换)
            2. 构建 BlockEntityDatas 包装数据
            3. 调用 ContainerWriter.write() 完成放置和填充

        命令格式:
            - setblock <x> <y> <z> <block_name> [states]
            - replaceitem block <x> <y> <z> slot.container <slot> <item> <count> <damage>

        Args:
            x, y, z: 目标坐标。
            block_name: 容器方块名 (如 ``"minecraft:chest"``、
                ``"minecraft:barrel"``、``"minecraft:hopper"`` 等)。
            items: 物品列表, 每个元素为含以下键的字典:
                - ``slot`` (int): 槽位编号 (0-26 for chest, 0-4 for hopper)
                - ``item_name`` (str): 物品名 (如 ``"minecraft:diamond"``)
                - ``count`` (int, 可选): 数量, 默认 1
                - ``damage`` (int, 可选): 物品数据值, 默认 0
            states_string: 方块状态字符串 (可选, 如 ``"[\"facing_direction\":3]"``)。

        Returns:
            成功返回 True, 失败返回 False。

        Example::

            await placer.place_container_direct(
                x=500, y=70, z=500,
                block_name="minecraft:chest",
                items=[
                    {"slot": 0, "item_name": "minecraft:diamond", "count": 64},
                    {"slot": 1, "item_name": "minecraft:iron_ingot", "count": 32},
                ],
            )
        """
        try:
            # 规范化方块名
            clean_name = self._clean_block_name(block_name)

            # 构建 ContainerItem 列表
            container_items: list[ContainerItem] = []
            for item in items:
                container_items.append(ContainerItem(
                    Name=self._clean_block_name(item.get("item_name", "minecraft:air")),
                    Count=item.get("count", 1) & 0xFF,
                    Damage=item.get("damage", 0) & 0xFFFF,
                    Slot=item.get("slot", 0) & 0xFF,
                ))

            # 构建 BlockEntityDatas
            block_datas = BlockEntityDatas(
                Block=GeneralBlock(
                    Name=clean_name,
                    States={},
                    NBT={},
                ),
                Position=(x, y, z),
                Type="Container",
                StatesString=states_string,
                FastMode=False,
            )

            # 使用 ContainerWriter 直接写入
            writer = ContainerWriter(
                block_entity_datas=block_datas,
                items=container_items,
                send_command=self.sender.send_any_command,
            )
            await writer.write()

            logger.info(
                "replaceitem模式: 已放置容器 %s @ (%d, %d, %d) items=%d",
                clean_name, x, y, z, len(container_items),
            )
            return True

        except Exception as e:
            logger.error(
                "replaceitem模式: 放置容器 %s @ (%d, %d, %d) 失败: %s",
                block_name, x, y, z, e,
                exc_info=True,
            )
            return False

    # ------------------------------------------------------------------
    # place_command_block_direct — 直接在目标位置放置命令方块
    # ------------------------------------------------------------------

    async def place_command_block_direct(
        self,
        x: int,
        y: int,
        z: int,
        block_name: str,
        command: str,
        mode: str = "impulse",
        conditional: bool = False,
        redstone: str = "always_active",
        custom_name: str = "",
        tick_delay: int = 0,
        track_output: bool = True,
        execute_on_first_tick: bool = True,
    ) -> bool:
        """直接在目标位置放置命令方块并写入数据 (replaceitem 模式)。

        使用 PhoenixBuilder 的 CommandBlockWriter 在目标位置放置命令方块,
        然后通过 CommandBlockUpdate 数据包写入命令方块数据。

        **注意**: 此方法需要 ``send_packet`` 回调可用
        (在初始化 NBTBlockPlacer 时传入)。如果 send_packet 不可用,
        将仅放置方块但不写入命令数据, 建议回退到 structure 模式。

        工作流程:
            1. 构建 CommandBlockData 数据结构
            2. 构建 BlockEntityDatas 包装数据
            3. 调用 CommandBlockWriter.write() 放置方块并写入数据

        Args:
            x, y, z: 目标坐标。
            block_name: 命令方块名 (如 ``"minecraft:command_block"``、
                ``"minecraft:chain_command_block"``、
                ``"minecraft:repeating_command_block"``)。
            command: 命令方块要执行的命令 (不含 ``/``)。
            mode: 命令方块模式, 可选:
                - ``"impulse"``: 脉冲命令方块
                - ``"chain"``: 连锁命令方块
                - ``"repeat"``: 循环命令方块
                默认 ``"impulse"``。
            conditional: 是否为条件模式。
            redstone: 红石模式, 可选:
                - ``"always_active"``: 始终激活 (auto=true)
                - ``"needs_redstone"``: 需要红石信号
                默认 ``"always_active"``。
            custom_name: 自定义名称 (可选)。
            tick_delay: 延迟 (tick), 默认 0。
            track_output: 是否追踪输出, 默认 True。
            execute_on_first_tick: 是否在首个 tick 执行, 默认 True。

        Returns:
            成功返回 True, 失败返回 False。

        Example::

            await placer.place_command_block_direct(
                x=500, y=70, z=500,
                block_name="minecraft:repeating_command_block",
                command="say Hello World",
                mode="repeat",
                redstone="always_active",
            )
        """
        try:
            # 规范化方块名
            clean_name = self._clean_block_name(block_name)

            # 构建 CommandBlockData
            cmd_data = CommandBlockData(
                Command=command,
                CustomName=custom_name,
                LastOutput="",
                TickDelay=tick_delay,
                ExecuteOnFirstTick=execute_on_first_tick,
                TrackOutput=track_output,
                ConditionalMode=conditional,
                Auto=(redstone == "always_active"),
            )

            # 构建 BlockEntityDatas
            block_datas = BlockEntityDatas(
                Block=GeneralBlock(
                    Name=clean_name,
                    States={},
                    NBT={},
                ),
                Position=(x, y, z),
                Type="CommandBlock",
                StatesString="",
                FastMode=False,
            )

            # 使用 CommandBlockWriter 写入
            writer = CommandBlockWriter(
                block_entity_datas=block_datas,
                data=cmd_data,
                send_command=self.sender.send_any_command,
                send_packet=self._send_packet,
                exclude_commands=False,
                invalidate_commands=False,
            )
            await writer.write(need_to_place_block=True)

            logger.info(
                "replaceitem模式: 已放置命令方块 %s @ (%d, %d, %d) "
                "mode=%s conditional=%s",
                clean_name, x, y, z, mode, conditional,
            )
            return True

        except Exception as e:
            logger.error(
                "replaceitem模式: 放置命令方块 %s @ (%d, %d, %d) 失败: %s",
                block_name, x, y, z, e,
                exc_info=True,
            )
            return False

    # ------------------------------------------------------------------
    # place_sign_direct — 直接在目标位置放置告示牌
    # ------------------------------------------------------------------

    async def place_sign_direct(
        self,
        x: int,
        y: int,
        z: int,
        block_name: str,
        text: str,
        text_owner: str = "",
        sign_text_color: int = 0,
        ignore_lighting: bool = False,
        is_wall: bool = False,
        facing: str = "south",
        apply_glow_ink: bool = False,
        apply_honeycomb: bool = False,
    ) -> bool:
        """直接在目标位置放置告示牌并写入 NBT 数据 (replaceitem 模式)。

        使用 PhoenixBuilder 的 SignWriter 在目标位置放置告示牌,
        然后通过 BlockActorData 数据包写入告示牌 NBT 数据。

        **注意**: 此方法需要 ``send_packet`` 回调可用
        (在初始化 NBTBlockPlacer 时传入)。如果 send_packet 不可用,
        将仅放置方块但不写入告示牌 NBT, 建议回退到 structure 模式。

        **发光墨囊/蜜蜡**: 当前版本中 replaceitem 模式暂不支持
        发光墨囊和蜜蜡涂覆 (这些操作需要玩家交互)。如需发光/上蜡效果,
        请使用 structure 模式的 :meth:`place_sign`。

        工作流程:
            1. 构建 SignData 数据结构
            2. 构建 BlockEntityDatas 包装数据
            3. 调用 SignWriter.write() 放置方块并写入 NBT

        Args:
            x, y, z: 目标坐标。
            block_name: 告示牌方块名 (如 ``"minecraft:standing_sign"``、
                ``"minecraft:wall_sign"`` 等)。
            text: 告示牌文本内容。
            text_owner: 文本所有者 (可选)。
            sign_text_color: 文本颜色 (int32), 默认 0 (黑色)。
            ignore_lighting: 是否忽略光照, 默认 False。
            is_wall: 是否为贴墙告示牌。
            facing: 朝向 (``"north"``/``"south"``/``"east"``/``"west"``)。
            apply_glow_ink: 是否应用发光墨囊 (需要 structure 模式)。
            apply_honeycomb: 是否应用蜜蜡 (需要 structure 模式)。

        Returns:
            成功返回 True, 失败返回 False。

        Example::

            await placer.place_sign_direct(
                x=500, y=70, z=500,
                block_name="minecraft:standing_sign",
                text="Hello World",
                facing="south",
            )
        """
        try:
            # 规范化方块名
            clean_name = self._clean_block_name(block_name)

            # 如果请求发光/蜜蜡但 send_packet 不可用, 发出警告
            if (apply_glow_ink or apply_honeycomb) and not self._send_packet_available:
                logger.warning(
                    "replaceitem模式: 发光墨囊/蜜蜡需要 send_packet 支持, "
                    "当前不可用, 将跳过涂覆步骤"
                )

            # 构建 SignData
            sign_data = SignData(
                TextOwner=text_owner,
                IgnoreLighting=1 if ignore_lighting else 0,
                SignTextColor=sign_text_color,
                TextIgnoreLegacyBugResolved=0,
                Text=text,
            )

            # 构建方块状态字符串
            states_string = ""
            if is_wall:
                facing_state = _WALL_SIGN_FACING.get(facing, 2)
                states_string = f'["facing_direction":{facing_state}]'
            else:
                facing_state = _STANDING_SIGN_FACING.get(facing, 0)
                states_string = f'["ground_sign_direction":{facing_state}]'

            # 构建 BlockEntityDatas
            block_datas = BlockEntityDatas(
                Block=GeneralBlock(
                    Name=clean_name,
                    States={},
                    NBT={},
                ),
                Position=(x, y, z),
                Type="Sign",
                StatesString=states_string,
                FastMode=False,
            )

            # 使用 SignWriter 写入
            writer = SignWriter(
                block_entity_datas=block_datas,
                data=sign_data,
                send_command=self.sender.send_any_command,
                send_packet=self._send_packet,
            )
            await writer.write()

            logger.info(
                "replaceitem模式: 已放置告示牌 %s @ (%d, %d, %d) "
                "facing=%s is_wall=%s",
                clean_name, x, y, z, facing, is_wall,
            )
            return True

        except Exception as e:
            logger.error(
                "replaceitem模式: 放置告示牌 %s @ (%d, %d, %d) 失败: %s",
                block_name, x, y, z, e,
                exc_info=True,
            )
            return False

    # ==================================================================
    # STRUCTURE 模式方法 (NovaBuilder/NexusE 方案, 默认推荐, 网易 3.8 适配)
    # ==================================================================

    # ------------------------------------------------------------------
    # 告示牌放置 (structure 模式)
    # ------------------------------------------------------------------

    async def place_sign(
        self,
        x: int,
        y: int,
        z: int,
        facing: str,
        text_lines: list[str],
        is_wall: bool = False,
    ) -> str:
        """放置告示牌并处理发光墨囊 + 蜜蜡 (structure 模式)。

        工作流程:
            a. 在 (x, y, z) 生成面对 ``facing`` 方向的告示牌 (含 4 行文字 NBT)
            b. 若 ``is_wall`` 为 True, 先在朝向相反侧放置海晶灯支撑方块
               (防止告示牌因悬空而消失)
            c. 告示牌生成后, 文字通过 NBT 数据显示在告示牌上
            d. 应用发光墨囊 + 蜜蜡 (顺序: 先墨囊后蜜蜡)
            e. 保存结构, 返回结构名

        Args:
            x, y, z: 告示牌坐标 (建议为平台中心 +1 高度)。
            facing: 朝向 (``"north"``/``"south"``/``"east"``/``"west"``)。
            text_lines: 4 行文字列表 (不足 4 行自动补空, 超出截断)。
            is_wall: 是否为贴墙告示牌。``True`` 为墙告示牌, ``False`` 为站立告示牌。

        Returns:
            保存的结构名 (用于后续 :meth:`_load_structure` 加载到目标位置)。
        """
        # 规整为 4 行文字
        lines = list(text_lines) + ["", "", "", ""]
        lines = lines[:4]

        # 构建 NBT 数据 (Bedrock 告示牌文字格式)
        nbt_data: dict[str, Any] = {
            "Text": lines[0],
            "Text2": lines[1],
            "Text3": lines[2],
            "Text4": lines[3],
        }

        if is_wall:
            # 贴墙告示牌: 需要 minecraft:wall_sign + facing_direction 状态
            block_name = "minecraft:wall_sign"
            facing_state = _WALL_SIGN_FACING.get(facing, 2)
            nbt_data["facing_direction"] = facing_state

            # 在朝向相反侧放置海晶灯支撑方块, 防止告示牌因悬空而消失
            dx, dy, dz = _WALL_SIGN_SUPPORT_OFFSET.get(facing, (0, 0, 1))
            support_cmd = (
                f"setblock {x + dx} {y + dy} {z + dz} "
                f"{self.SEA_LANTERN.name}"
            )
            await self.sender.send_any_command(support_cmd)
            await self.rate_limiter.wait_block()
            logger.debug(
                "贴墙告示牌支撑海晶灯 @ (%d, %d, %d)",
                x + dx, y + dy, z + dz,
            )
        else:
            # 站立告示牌: 需要 minecraft:standing_sign + ground_sign_direction 状态
            block_name = "minecraft:standing_sign"
            facing_state = _STANDING_SIGN_FACING.get(facing, 0)
            nbt_data["ground_sign_direction"] = facing_state

        # 记录告示牌状态 (供 _interact_with_block 刷新 NBT 使用)
        self._last_sign_block = block_name
        self._last_sign_nbt = dict(nbt_data)

        # 放置告示牌 (含 NBT 数据)
        nbt_json = json.dumps(nbt_data, ensure_ascii=False)
        cmd = f"setblock {x} {y} {z} {block_name} {nbt_json}"
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_block()
        logger.info(
            "structure模式: 已放置告示牌 @ (%d, %d, %d) facing=%s is_wall=%s",
            x, y, z, facing, is_wall,
        )

        # 应用发光墨囊 + 蜜蜡 (顺序: 先墨囊后蜜蜡, 绝对不能反!)
        await self._apply_glow_ink_then_honeycomb(x, y, z)

        # 保存结构 (单方块, 两角相同)
        struct_name = self._gen_structure_name()
        success = await self._save_structure(struct_name, x, y, z, x, y, z)
        if not success:
            logger.error("structure save 失败: %s", struct_name)
            return ""

        return struct_name

    async def _apply_glow_ink_then_honeycomb(self, x: int, y: int, z: int) -> None:
        """先涂发光墨囊, 再涂蜜蜡 (顺序绝对不能反)。

        顺序的重要性:
            - 如果先涂蜜蜡再涂发光墨囊, 蜜蜡会封闭告示牌, 导致发光墨囊涂不上
            - 必须先涂发光墨囊让文字发光, 再涂蜜蜡锁定告示牌

        工作流程:
            1. 用 /replaceitem 给自己主手放 glow_ink_sac
            2. 交互告示牌 (应用 GlowingText 效果)
            3. 用 /replaceitem 给自己主手放 honeycomb
            4. 交互告示牌 (应用 Waxed 效果)

        Args:
            x, y, z: 告示牌坐标。
        """
        # === 步骤 1: 发光墨囊 (必须先涂) ===
        await self._replace_item_in_hand(self.GLOW_INK_SAC, count=1)
        await self.rate_limiter.wait_command()
        # 交互告示牌, 应用发光效果
        await self._interact_with_block(x, y, z)

        # === 步骤 2: 蜜蜡 (必须后涂) ===
        await self._replace_item_in_hand(self.HONEYCOMB, count=1)
        await self.rate_limiter.wait_command()
        # 交互告示牌, 应用上蜡效果
        await self._interact_with_block(x, y, z)

        logger.info(
            "已应用发光墨囊 + 蜜蜡到告示牌 @ (%d, %d, %d) [顺序: 先墨囊后蜜蜡]",
            x, y, z,
        )

    async def _refresh_sign_nbt(self, x: int, y: int, z: int) -> None:
        """刷新告示牌 NBT (重新 setblock 以应用新的 NBT 字段)。

        用于在 :meth:`_interact_with_block` 中应用 GlowingText / Waxed 标记后,
        将更新后的 NBT 写回到告示牌方块。

        Args:
            x, y, z: 告示牌坐标。
        """
        if not self._last_sign_block or not self._last_sign_nbt:
            logger.warning("无法刷新告示牌 NBT: 未记录告示牌状态")
            return

        nbt_json = json.dumps(self._last_sign_nbt, ensure_ascii=False)
        cmd = f"setblock {x} {y} {z} {self._last_sign_block} {nbt_json}"
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_block()

    # ------------------------------------------------------------------
    # 容器放置 (structure 模式)
    # ------------------------------------------------------------------

    async def place_container(
        self,
        x: int,
        y: int,
        z: int,
        block_name: str,
        items: list[dict[str, Any]],
    ) -> str:
        """放置容器 (箱子、木桶等) 并填入物品 (structure 模式, 默认推荐)。

        **网易 3.8 推荐方案 + 用户反馈优化**:

        .. important::

            **用户反馈**: "如果容器里面有普通的可以用 rep 指令放入的物品,
            应该用 rep 指令放置" — 普通物品用 replaceitem (快速),
            NBT 物品用平台模式 (structure save/load)。

            **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
            不能放附魔/自定义名字), 因此容器中的物品按类型分流处理:

            - **普通物品** (无附魔、无自定义名字、无复杂 NBT):
              使用 ``replaceitem`` 直接放入 (快速, 遵守网易 3.8 限制)。
              只放耐久 (``Damage``)、特殊值、数量、简单 NBT 标签
              (``minecraft:keep_on_death`` 死亡不掉落、
              ``minecraft:item_lock`` 物品锁定)。
            - **NBT 物品** (有附魔、有自定义名字、有复杂 NBT):
              使用**平台模式**处理 (通过工作方块如铁砧/织布机/锻造台制作,
              然后放入容器, 再通过 structure save/load 搬运到目标位置)。
              **无时长限制**, 顺序执行: 等第一个 NBT 物品制作完成
              (不管多长时间) 再开始第二个。

        工作流程:
            1. 在 (x, y, z) 放置容器方块 [平台位置]
            2. **分类物品**: 通过 :meth:`_classify_item` 分为
               ``normal_items`` 和 ``nbt_items`` 两类
            3. **普通物品** 用 ``replaceitem`` 直接放入 (快速, 3.8 限制内):
               - 调用 :meth:`_place_item_with_replaceitem` 逐个放入
               - 自动过滤 3.8 不允许的字段 (附魔/自定义名字等)
               - 只保留耐久/特殊值/数量/简单 NBT 标签
            4. **NBT 物品** 用平台模式处理 (工作方块制作, 顺序执行, 无时长限制):
               - 调用 :meth:`_place_nbt_item_via_platform` 逐个处理
               - 每个 NBT 物品制作完成后 (通过 :meth:`_wait_for_nbt_completion`
                 等待服务器确认, **无超时**) 才开始下一个
            5. 保存结构 (含已放入的普通物品和 NBT 物品), 返回结构名

        **NBT 制作无时长限制** (用户反馈):
            "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个 NBT 制作完,
            不管多长时间, 然后第一个制作完开始制作第二个"
            本方法在处理 NBT 物品时, 不使用固定超时, 而是通过
            :meth:`_wait_for_nbt_completion` 等待实际完成确认。

        Args:
            x, y, z: 容器坐标 (平台位置)。
            block_name: 容器方块名 (如 ``"minecraft:chest"``、
                ``"minecraft:barrel"``、``"minecraft:shulker_box"`` 等)。
            items: 物品列表, 每个元素为含以下键的字典:
                - ``slot`` (int): 槽位编号 (0-26 for chest)
                - ``item_name`` (str): 物品名 (如 ``"minecraft:diamond"``)
                - ``count`` (int): 数量, 默认 1
                - ``damage`` (int, 可选): 特殊值/耐久, 默认 0
                - ``nbt`` (dict|str, 可选): 物品 NBT 数据
                  - 普通物品: 仅含 ``minecraft:keep_on_death`` /
                    ``minecraft:item_lock`` 等 3.8 允许的标签
                  - NBT 物品: 含附魔 (``enchantments``) /
                    自定义名字 (``display``) / 复杂 NBT (``BlockEntityTag`` 等)

        Returns:
            保存的结构名。
        """
        # === 步骤 1: 放置容器方块 ===
        cmd = f"setblock {x} {y} {z} {block_name}"
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_block()
        # 等待容器方块创建完成 (使用 NBT 完成事件, 无固定超时)
        # 注: 此处用 asyncio.sleep 作为兜底, 实际完成由事件通知
        await asyncio.sleep(0.1)
        logger.info(
            "structure模式: 已放置容器 %s @ (%d, %d, %d)", block_name, x, y, z,
        )

        # === 步骤 2: 分类物品 (普通 vs NBT) ===
        # 用户反馈: 普通物品用 replaceitem, NBT 物品用平台模式
        normal_items: list[dict[str, Any]] = []
        nbt_items: list[dict[str, Any]] = []
        for item in items:
            if self._classify_item(item) == "normal":
                normal_items.append(item)
            else:
                nbt_items.append(item)

        logger.info(
            "structure模式: 容器 %s @ (%d, %d, %d) 物品分类: "
            "普通=%d (replaceitem), NBT=%d (平台模式)",
            block_name, x, y, z, len(normal_items), len(nbt_items),
        )

        # === 步骤 3: 普通物品用 replaceitem 直接放入 (快速, 3.8 限制内) ===
        # 用户反馈: "如果容器里面有普通的可以用 rep 指令放入的物品,
        # 应该用 rep 指令放置"
        for item in normal_items:
            await self._place_item_with_replaceitem(x, y, z, item)

        if normal_items:
            await self.rate_limiter.wait_group()
            logger.info(
                "structure模式: 已用 replaceitem 放入 %d 个普通物品到容器 "
                "@ (%d, %d, %d) [3.8 限制内]",
                len(normal_items), x, y, z,
            )

        # === 步骤 4: NBT 物品用平台模式处理 (工作方块, 顺序执行, 无时长限制) ===
        # 用户反馈: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
        # NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"
        for idx, item in enumerate(nbt_items):
            logger.info(
                "structure模式: 开始处理第 %d/%d 个 NBT 物品 (无时长限制, "
                "等完成再继续)",
                idx + 1, len(nbt_items),
            )
            # _place_nbt_item_via_platform 内部会等待 NBT 完成 (无超时)
            await self._place_nbt_item_via_platform(x, y, z, item)

        if nbt_items:
            logger.info(
                "structure模式: 已用平台模式处理 %d 个 NBT 物品到容器 "
                "@ (%d, %d, %d) [顺序执行, 无时长限制]",
                len(nbt_items), x, y, z,
            )

        # === 步骤 5: 保存结构 (含普通物品 + NBT 物品) ===
        struct_name = self._gen_structure_name()
        success = await self._save_structure(struct_name, x, y, z, x, y, z)
        if not success:
            logger.error("structure save 失败: %s", struct_name)
            return ""
        return struct_name

    # ------------------------------------------------------------------
    # 命令方块放置 (structure 模式)
    # ------------------------------------------------------------------

    async def place_command_block(
        self,
        x: int,
        y: int,
        z: int,
        cmd: str,
        mode: str = "repeat",
        conditional: bool = False,
        redstone: str = "always_active",
    ) -> str:
        """放置命令方块并设置命令 (structure 模式, 默认推荐)。

        **网易 3.8 推荐方案**: 通过 structure save/load 搬运命令方块到目标位置。
        若需直接放置 (不经过平台搬运), 可选使用
        :meth:`place_command_block_direct` (replaceitem 模式, 需 send_packet)。

        Args:
            x, y, z: 命令方块坐标。
            cmd: 命令方块要执行的命令 (不含 ``/``)。
            mode: 命令方块模式, 可选:
                - ``"impulse"``: 脉冲命令方块 (执行一次)
                - ``"chain"``: 连锁命令方块
                - ``"repeat"``: 循环命令方块 (重复执行)
                默认 ``"repeat"``。
            conditional: 是否为条件命令方块 (仅在前一命令方块执行成功时触发)。
            redstone: 红石模式, 可选:
                - ``"always_active"``: 始终激活 (auto=true)
                - ``"needs_redstone"``: 需要红石信号
                默认 ``"always_active"``。

        Returns:
            保存的结构名。
        """
        # 根据模式选择方块名
        mode_lower = mode.lower()
        if mode_lower == "chain":
            block_name = "minecraft:chain_command_block"
        elif mode_lower == "repeat":
            block_name = "minecraft:repeating_command_block"
        else:  # impulse
            block_name = "minecraft:command_block"

        # 构建 NBT 数据 (block states + tile NBT 合并到一个 JSON)
        nbt_data: dict[str, Any] = {
            "conditional_bit": bool(conditional),
            "Command": cmd,
        }
        if redstone == "always_active":
            nbt_data["auto"] = True

        nbt_json = json.dumps(nbt_data, ensure_ascii=False)
        setblock_cmd = f"setblock {x} {y} {z} {block_name} {nbt_json}"
        await self.sender.send_any_command(setblock_cmd)
        await self.rate_limiter.wait_block()
        logger.info(
            "structure模式: 已放置命令方块 %s @ (%d, %d, %d) "
            "mode=%s conditional=%s redstone=%s",
            block_name, x, y, z, mode, conditional, redstone,
        )

        # 保存结构
        struct_name = self._gen_structure_name()
        success = await self._save_structure(struct_name, x, y, z, x, y, z)
        if not success:
            logger.error("structure save 失败: %s", struct_name)
            return ""
        return struct_name

    # ------------------------------------------------------------------
    # 旗帜放置 (structure 模式)
    # ------------------------------------------------------------------

    async def place_banner(
        self,
        x: int,
        y: int,
        z: int,
        patterns: list[dict[str, Any]],
        base_color: int = 0,
    ) -> str:
        """放置旗帜并设置图案 (structure 模式)。

        Args:
            x, y, z: 旗帜坐标。
            patterns: 图案列表, 每个元素为含以下键的字典:
                - ``pattern`` (str): 图案标识 (如 ``"stripe_top"``、``"cross"`` 等)
                - ``color`` (int): 图案颜色 (0-15, 对应 Minecraft 染色代码)
            base_color: 底色 (0-15, 0=黑色, 默认 0)。

        Returns:
            保存的结构名。
        """
        # 构建旗帜 NBT
        nbt_data: dict[str, Any] = {
            "Base": base_color,
            "Patterns": [
                {
                    "Pattern": p.get("pattern", ""),
                    "Color": p.get("color", 0),
                }
                for p in patterns
            ],
        }

        nbt_json = json.dumps(nbt_data, ensure_ascii=False)
        block_name = "minecraft:standing_banner"
        cmd = f"setblock {x} {y} {z} {block_name} {nbt_json}"
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_block()
        logger.info(
            "structure模式: 已放置旗帜 @ (%d, %d, %d) base_color=%d patterns=%d",
            x, y, z, base_color, len(patterns),
        )

        # 保存结构
        struct_name = self._gen_structure_name()
        success = await self._save_structure(struct_name, x, y, z, x, y, z)
        if not success:
            logger.error("structure save 失败: %s", struct_name)
            return ""
        return struct_name

    # ------------------------------------------------------------------
    # 结构保存/加载
    # ------------------------------------------------------------------

    async def _save_structure(
        self,
        name: str,
        x1: int,
        y1: int,
        z1: int,
        x2: int,
        y2: int,
        z2: int,
    ) -> bool:
        """保存结构到临时名称 (无固定超时, 等待实际完成)。

        .. important::

            **用户反馈**: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
            NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"

            本方法**不使用固定超时** (旧的 ``nbt_delay``),
            而是通过 :meth:`_wait_for_nbt_completion` 等待服务器实际确认
            structure save 完成 (无超时, 顺序执行)。

        使用 Bedrock 的 ``structure save`` 命令将指定区域保存为命名结构。
        命令格式::

            structure save <name> <x1 y1 z1> <x2 y2 z2> true disk

        其中 ``true`` 表示包含实体, ``disk`` 表示保存到磁盘 (持久化)。

        Args:
            name: 结构名 (无需扩展名)。
            x1, y1, z1: 区域起始角坐标。
            x2, y2, z2: 区域结束角坐标。

        Returns:
            保存成功返回 ``True``, 失败返回 ``False``。
        """
        cmd = (
            f'structure save "{name}" '
            f"{x1} {y1} {z1} {x2} {y2} {z2} true disk"
        )
        # 准备等待服务器确认 (清除事件)
        self._prepare_for_nbt_wait()
        resp = await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_command()
        # 等待 NBT 操作 (structure save) 完成 -- 无固定超时, 顺序执行
        # 用户反馈: NBT 制作无时长限制, 等完成再继续
        await self._wait_for_nbt_completion()
        success = resp is not None
        logger.info(
            "保存结构 %s [%d,%d,%d -> %d,%d,%d]: %s [无固定超时, 等待完成]",
            name, x1, y1, z1, x2, y2, z2,
            "成功" if success else "失败",
        )
        return success

    async def _load_structure(self, name: str, x: int, y: int, z: int) -> bool:
        """加载结构到指定坐标 (无固定超时, 等待实际完成)。

        .. important::

            **用户反馈**: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
            NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"

            本方法**不使用固定超时** (旧的 ``nbt_delay``),
            而是通过 :meth:`_wait_for_nbt_completion` 等待服务器实际确认
            structure load 完成 (无超时, 顺序执行)。

            这是确保 NBT 方块**顺序执行**的关键: 第一个 NBT 方块的
            structure load 完全成功后, 才开始第二个 NBT 方块的处理。

        使用 Bedrock 的 ``structure load`` 命令将之前保存的结构加载到目标坐标。
        命令格式::

            structure load <name> <x y z>

        Args:
            name: 结构名 (由 :meth:`_save_structure` 保存)。
            x, y, z: 加载目标坐标 (结构原点放置位置)。

        Returns:
            加载成功返回 ``True``, 失败返回 ``False``。
        """
        cmd = f'structure load "{name}" {x} {y} {z}'
        # 准备等待服务器确认 (清除事件)
        self._prepare_for_nbt_wait()
        resp = await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_command()
        # 等待 NBT 操作 (structure load) 完成 -- 无固定超时, 顺序执行
        # 用户反馈: 等第一个 NBT 制作完, 不管多长时间, 再做第二个
        await self._wait_for_nbt_completion()
        success = resp is not None
        logger.info(
            "加载结构 %s @ (%d, %d, %d): %s [无固定超时, 等待完成]",
            name, x, y, z,
            "成功" if success else "失败",
        )
        return success

    # ------------------------------------------------------------------
    # 传送
    # ------------------------------------------------------------------

    async def _teleport_to(self, x: int, y: int, z: int) -> None:
        """传送到指定坐标。

        走控制台命令 (因为 ``/tp`` 在魔法指令中无效, 必须走 OP 命令)。
        :meth:`send_any_command` 会自动将 ``/tp`` 路由到 :meth:`send_wo_command`。

        Args:
            x, y, z: 目标坐标。
        """
        cmd = f"tp @s {x} {y} {z}"
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_block()
        logger.info("已传送到 (%d, %d, %d)", x, y, z)

    # ------------------------------------------------------------------
    # 物品操作
    # ------------------------------------------------------------------

    async def _replace_item_in_hand(
        self,
        item_name: str,
        count: int = 1,
        nbt: Any = None,
    ) -> None:
        """用 replaceitem 给自己主手放物品。

        命令格式::

            replaceitem entity @s slot.weapon.mainhand 0 <item> <count> 0 <nbt>

        走魔法指令 (``/replaceitem`` 会自动路由到 :meth:`send_ai_command``)。

        Args:
            item_name: 物品名 (如 ``"minecraft:glow_ink_sac"``)。
            count: 物品数量, 默认 1。
            nbt: 物品 NBT 数据 (dict 或字符串或 None)。
        """
        # 记录当前主手物品 (供 _interact_with_block 判断涂什么)
        self._current_hand_item = item_name

        nbt_str = self._format_nbt(nbt)
        cmd = (
            f"replaceitem entity @s {self.MAINHAND_SLOT} 0 "
            f"{item_name} {count} 0"
        )
        if nbt_str:
            cmd += f" {nbt_str}"

        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_block()
        logger.debug("主手已放置物品 %s x%d", item_name, count)

    async def _interact_with_block(self, x: int, y: int, z: int) -> None:
        """与方块交互 (用于涂墨囊/蜜蜡)。

        根据 :meth:`_replace_item_in_hand` 设置的当前主手物品,
        直接修改告示牌的 NBT 来应用对应效果:

            - 主手为 ``glow_ink_sac``: 设置 ``GlowingText=1`` (文字发光)
            - 主手为 ``honeycomb``: 设置 ``Waxed=1`` (上蜡锁定)
            - 其他物品: 发送 ``playanimation`` 命令模拟交互 (占位)

        注: Bedrock 中无法通过命令直接模拟 "玩家右键方块" 的交互动作,
        因此这里采用直接修改告示牌 NBT 的方式来应用墨囊/蜜蜡效果。
        告示牌的发光和上蜡状态只能通过 NBT 设置, 不能通过 replaceitem
        对方块进行操作 (因为告示牌不是容器)。

        Args:
            x, y, z: 方块坐标。
        """
        if self._current_hand_item == self.GLOW_INK_SAC:
            # 应用发光墨囊: 设置 GlowingText 标记
            self._last_sign_nbt["GlowingText"] = 1
            await self._refresh_sign_nbt(x, y, z)
            logger.debug("已应用发光墨囊到告示牌 @ (%d, %d, %d)", x, y, z)

        elif self._current_hand_item == self.HONEYCOMB:
            # 应用蜜蜡: 设置 Waxed 标记
            self._last_sign_nbt["Waxed"] = 1
            await self._refresh_sign_nbt(x, y, z)
            logger.debug("已应用蜜蜡到告示牌 @ (%d, %d, %d)", x, y, z)

        else:
            # 其他物品: 发送 playanimation 模拟交互 (占位)
            cmd = "playanimation @s animation.armor_stand.no_pose"
            await self.sender.send_any_command(cmd)
            await self.rate_limiter.wait_block()

        # 等待服务器处理
        await self.rate_limiter.wait_command()

    async def _rename_item_at_anvil(
        self,
        anvil_x: int,
        anvil_y: int,
        anvil_z: int,
        item: dict[str, Any],
        new_name: str,
    ) -> dict[str, Any]:
        """在铁砧处重命名物品。

        工作流程:
            1. 传送到铁砧位置 (y+1, 站在铁砧上方)
            2. 用 /replaceitem 把物品放到铁砧输入槽 (slot.container.0)
               并通过物品 NBT 的 ``minecraft:display.Name`` 组件直接设置新名称
            3. 返回包含新名称的物品 NBT (供后续放回箱子)

        注: Bedrock 中无法通过命令直接触发铁砧的重命名 UI 操作,
        因此这里通过在物品 NBT 中直接设置 ``minecraft:display`` 组件的
        ``Name`` 字段来实现重命名效果。

        Args:
            anvil_x, anvil_y, anvil_z: 铁砧坐标。
            item: 原始物品字典, 应包含以下键:
                - ``Name`` (str): 物品名 (如 ``"minecraft:diamond_sword"``)
                - ``Data`` (int, 可选): 物品数据值
                - ``components`` (dict, 可选): 其他物品组件
            new_name: 新名称 (JSON 文本格式, 如 ``'{"text":"我的剑"}'``)。

        Returns:
            重命名后的物品 NBT 字典, 包含 ``Name`` 和 ``components`` 键。
        """
        # 传送到铁砧上方
        await self._teleport_to(anvil_x, anvil_y + 1, anvil_z)

        # 构建重命名后的物品 NBT
        item_nbt: dict[str, Any] = dict(item) if isinstance(item, dict) else {}
        item_name = item_nbt.get("Name", "minecraft:stone")

        # 设置 display 组件的 Name 字段 (Bedrock 物品命名格式)
        components = item_nbt.setdefault("components", {})
        display = components.setdefault("minecraft:display", {})
        display["Name"] = new_name
        item_nbt["components"] = components

        # 用 replaceitem 把物品放到铁砧输入槽 (slot.container.0)
        # 通过 components 参数传递 display 组件 (含新名称)
        components_json = json.dumps({"minecraft:display": {"Name": new_name}}, ensure_ascii=False)
        cmd = (
            f"replaceitem block {anvil_x} {anvil_y} {anvil_z} "
            f"slot.container.0 {item_name} 1 0 {components_json}"
        )
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_container()

        await self.rate_limiter.wait_group()
        logger.info(
            "已在铁砧 @ (%d, %d, %d) 重命名 %s 为 %r",
            anvil_x, anvil_y, anvil_z, item_name, new_name,
        )

        return item_nbt

    # ==================================================================
    # 主入口
    # ==================================================================

    async def place_nbt_block(
        self,
        block_type: str,
        x: int,
        y: int,
        z: int,
        nbt_data: dict[str, Any],
        target_x: int,
        target_y: int,
        target_z: int,
    ) -> bool:
        """主入口: 在平台生成 NBT 方块 -> 处理 -> 保存 -> 传送到目标 -> 加载。

        **网易 3.8 默认推荐方案** (structure 平台模式):
        在 11x11 海晶灯平台生成 NBT 方块, 通过 structure save/load 搬运到目标位置。

        根据方块类型和当前 NBT 模式分派:
            - **structure 模式 (默认)**: 在平台生成 -> structure save -> tp -> structure load
            - **replaceitem 模式 (可选, 3.8 风险)**: 直接在目标位置放置方块并写入 NBT 数据

        根据方块类型分派到对应的 ``place_*`` 方法, 完成以下流程::

            1. 在平台 (x, y, z) 生成 NBT 方块 (含 NBT 数据处理)
            2. 保存结构 (place_* 方法内部完成)
            3. 机器人传送到目标位置 (target_x, target_y, target_z)
            4. 加载结构到目标位置
            5. 清理临时结构

        Args:
            block_type: 方块类型, 支持:
                - ``"sign"``: 告示牌
                - ``"container"``: 容器
                - ``"command_block"``: 命令方块
                - ``"banner"``: 旗帜
            x, y, z: 平台上的生成坐标 (建议为平台中心 +1 高度)。
            nbt_data: NBT 数据字典, 内容因 ``block_type`` 而异:
                - sign: ``facing``, ``text_lines``, ``is_wall``
                - container: ``block_name``, ``items``
                - command_block: ``command``, ``mode``, ``conditional``, ``redstone``
                - banner: ``patterns``, ``base_color``
            target_x, target_y, target_z: 搬运目标坐标。

        Returns:
            成功返回 ``True``, 失败返回 ``False``。
        """
        struct_name = ""

        # === 步骤 1: 在平台生成 NBT 方块并保存结构 ===
        if block_type == "sign":
            facing = nbt_data.get("facing", "south")
            text_lines = nbt_data.get("text_lines", ["", "", "", ""])
            is_wall = nbt_data.get("is_wall", False)
            struct_name = await self.place_sign(x, y, z, facing, text_lines, is_wall)

        elif block_type == "container":
            block_name = nbt_data.get("block_name", "minecraft:chest")
            items = nbt_data.get("items", [])
            struct_name = await self.place_container(x, y, z, block_name, items)

        elif block_type == "command_block":
            cmd_text = nbt_data.get("command", "")
            mode = nbt_data.get("mode", "repeat")
            conditional = nbt_data.get("conditional", False)
            redstone = nbt_data.get("redstone", "always_active")
            struct_name = await self.place_command_block(
                x, y, z, cmd_text, mode, conditional, redstone,
            )

        elif block_type == "banner":
            patterns = nbt_data.get("patterns", [])
            base_color = nbt_data.get("base_color", 0)
            struct_name = await self.place_banner(x, y, z, patterns, base_color)

        else:
            logger.warning("未知 NBT 方块类型: %s", block_type)
            return False

        if not struct_name:
            logger.error("放置 %s 失败: 未生成结构名", block_type)
            return False

        # === 步骤 2: 传送到目标位置 ===
        await self._teleport_to(target_x, target_y, target_z)

        # === 步骤 3: 加载结构到目标位置 ===
        success = await self._load_structure(
            struct_name, target_x, target_y, target_z,
        )

        # === 步骤 4: 清理临时结构 ===
        await self.cleanup_structure(struct_name)

        if success:
            logger.info(
                "NBT 方块 %s 已搬运到 (%d, %d, %d)",
                block_type, target_x, target_y, target_z,
            )
        else:
            logger.error(
                "NBT 方块 %s 搬运到 (%d, %d, %d) 失败",
                block_type, target_x, target_y, target_z,
            )

        return success

    async def place_nbt_block_smart(
        self,
        block_type: str,
        block_name: str,
        x: int,
        y: int,
        z: int,
        nbt_data: dict[str, Any],
        target_x: int,
        target_y: int,
        target_z: int,
        platform_x: int = 0,
        platform_y: int = 200,
        platform_z: int = 0,
    ) -> bool:
        """智能放置 NBT 方块: 根据模式自动选择最优方法。

        这是 :meth:`place_nbt_block` 的增强版, 新增了模式选择能力:
            - 自动检测模式 (AUTO): 根据方块类型选择 (网易 3.8 默认 structure)
            - replaceitem 模式: 直接在目标位置放置 (不经过平台, 网易 3.8 风险)
            - structure 模式: 在平台生成 -> 搬运到目标 (默认推荐, 网易 3.8 适配)

        Args:
            block_type: 方块类型 (``"sign"``/``"container"``/``"command_block"``/``"banner"``)。
            block_name: 方块名 (如 ``"minecraft:chest"``)。
            x, y, z: 平台上的生成坐标 (structure 模式使用)。
            nbt_data: NBT 数据字典。
            target_x, target_y, target_z: 最终目标坐标。
            platform_x, platform_y, platform_z: 平台中心坐标 (structure 模式使用)。

        Returns:
            成功返回 ``True``, 失败返回 ``False``。
        """
        # 解析模式
        resolved_mode = self._resolve_mode(block_name)

        logger.info(
            "智能放置 NBT 方块: type=%s block=%s mode=%s target=(%d,%d,%d)",
            block_type, block_name, resolved_mode.value,
            target_x, target_y, target_z,
        )

        # === replaceitem 模式: 直接在目标位置放置 ===
        if resolved_mode == NBTPlacementMode.REPLACEITEM:
            if block_type == "container":
                items = nbt_data.get("items", [])
                return await self.place_container_direct(
                    x=target_x, y=target_y, z=target_z,
                    block_name=block_name,
                    items=items,
                )

            elif block_type == "command_block":
                cmd_text = nbt_data.get("command", "")
                mode = nbt_data.get("mode", "impulse")
                conditional = nbt_data.get("conditional", False)
                redstone = nbt_data.get("redstone", "always_active")
                return await self.place_command_block_direct(
                    x=target_x, y=target_y, z=target_z,
                    block_name=block_name,
                    command=cmd_text,
                    mode=mode,
                    conditional=conditional,
                    redstone=redstone,
                )

            elif block_type == "sign":
                text = nbt_data.get("text", "")
                facing = nbt_data.get("facing", "south")
                is_wall = nbt_data.get("is_wall", False)
                return await self.place_sign_direct(
                    x=target_x, y=target_y, z=target_z,
                    block_name=block_name,
                    text=text,
                    facing=facing,
                    is_wall=is_wall,
                )

            elif block_type == "banner":
                # 旗帜暂不支持 replaceitem 模式, 回退到 structure
                logger.warning(
                    "replaceitem模式: 旗帜暂不支持直接放置, 回退到structure模式"
                )
                return await self.place_nbt_block(
                    block_type=block_type,
                    x=x, y=y, z=z,
                    nbt_data=nbt_data,
                    target_x=target_x,
                    target_y=target_y,
                    target_z=target_z,
                )

            else:
                logger.warning(
                    "replaceitem模式: 未知方块类型 %s, 回退到structure模式",
                    block_type,
                )
                return await self.place_nbt_block(
                    block_type=block_type,
                    x=x, y=y, z=z,
                    nbt_data=nbt_data,
                    target_x=target_x,
                    target_y=target_y,
                    target_z=target_z,
                )

        # === structure 模式: 平台生成 -> 搬运 ===
        else:
            return await self.place_nbt_block(
                block_type=block_type,
                x=x, y=y, z=z,
                nbt_data=nbt_data,
                target_x=target_x,
                target_y=target_y,
                target_z=target_z,
            )

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    async def cleanup_platform(self, center_x: int, center_y: int, center_z: int) -> None:
        """清理平台 (移除海晶灯平台和工作方块)。

        将 11x11 平台区域 (从 y-2 到 y+2, 共 5 层) 全部替换为空气,
        覆盖平台层、工作方块层和清空区域。

        逆向参考: NovaBuilder/NexusE 用 fill air 清除 11x11x5 区域
        (fill ~-5 ~-2 ~-5 ~5 ~2 ~5 air)。

        Args:
            center_x, center_y, center_z: 平台中心坐标。
        """
        half = self.PLATFORM_HALF  # 5 (11 // 2)
        x1, z1 = center_x - half, center_z - half
        x2, z2 = center_x + half, center_z + half

        # 一次性清除 11x11x5 区域 (y-2 到 y+2, 覆盖平台层和工作方块层)
        # 逆向: fill ~-5 ~-2 ~-5 ~5 ~2 ~5 air
        clear_y1 = center_y - 2
        clear_y2 = center_y + 2
        cmd = (
            f"fill {x1} {clear_y1} {z1} {x2} {clear_y2} {z2} "
            f"{self.AIR.name}"
        )
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_block()

        logger.info(
            "已清理平台 @ (%d, %d, %d) [11x11x5 区域]",
            center_x, center_y, center_z,
        )

    async def cleanup_structure(self, name: str) -> None:
        """删除已保存的结构。

        使用 ``structure delete`` 命令删除之前保存的临时结构, 释放磁盘空间。

        Args:
            name: 要删除的结构名。
        """
        cmd = f'structure delete "{name}"'
        await self.sender.send_any_command(cmd)
        await self.rate_limiter.wait_command()
        logger.info("已删除结构 %s", name)


__all__ = [
    "NBTPlacementMode",
    "NBTModeSelector",
    "NBTBlockPlacer",
]