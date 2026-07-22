"""nbt_handler - NBT 处理与放置逻辑 (合并 NexusEgo + NovaBuilder)。

本包合并了两个逆向工程代码包的 NBT 处理模块, 并适配到 PocketTerm 项目结构:

来源 1 - NexusEgo v1.6.5:
    - StarShuttler/nbt_parser/             (NBT 解析器)
    - WaterStructure/modules/nbt_assigner/  (NBT 分配器)
    - merry-memory/protocol/encoding       (协议编码)
    - strings_nbt.txt                       (NBT 字符串)

来源 2 - NovaBuilder (PhoenixBuilder 衍生):
    - PhoenixBuilder/fastbuilder/types/block.go
    - PhoenixBuilder/fastbuilder/bdump/command/*.go
    - nbt_platform.txt (11x11 平台模式)
    - nbt_mode_selector.txt (模式选择算法)
    - nbt_packets.txt (数据包序列)

子模块:
    - block_entities: 方块实体 NBT (29 种, 合并 NexusEgo + NovaBuilder)
    - container_handler: 容器 NBT 处理 (三模式 + 运行时操作)
    - item_nbt: 物品 NBT 处理 (14 种)
    - nbt_assigner: NBT 分配器 (决定方块需要什么 NBT)
    - nbt_placer: NBT 放置器 (STRUCTURE / REPLACEITEM 双模式)
    - nbt_mode_selector: NBT 模式选择器 (自动选择最佳放置模式)

.. important::

    **网易 3.8 协议限制**: replaceitem 命令已被阉割, 只能放耐久/特殊值/数量/
    NBT 标签 (keep_on_death, item_lock), **不能放附魔/自定义名字**。
    因此默认推荐使用 STRUCTURE 模式 (11x11 海晶灯平台 + structure save/load
    搬运), 详见 :mod:`.nbt_placer`。

使用示例::

    from app.protocol.nbt_handler import (
        NBTPlacer, NBTPlacementMode,
        NBTModeSelector,
        ContainerHandler, ContainerContent,
        SignNBT, create_block_entity,
    )

    # 创建告示牌 NBT
    sign = SignNBT(text1="Hello", text2="World")
    nbt = sign.to_nbt()

    # 选择放置模式
    selector = NBTModeSelector(server_type="netease")
    result = selector.select_mode("minecraft:standing_sign", nbt)

    # 放置带 NBT 的方块
    placer = NBTPlacer()
    placement = placer.place_block_with_nbt(
        position=(100, 64, 100),
        block_name="minecraft:standing_sign",
        block_states={},
        nbt=nbt,
        mode=result.mode,
    )
"""

from __future__ import annotations

# -------------------------------------------------------------------- #
# 导入所有子模块
# -------------------------------------------------------------------- #

from . import (
    block_entities,
    container_handler,
    item_nbt,
    nbt_assigner,
    nbt_mode_selector,
    nbt_placer,
)

# -------------------------------------------------------------------- #
# block_entities 导出
# -------------------------------------------------------------------- #

from .block_entities import (
    # 异常
    BlockEntityError,
    # 基类
    BlockEntity,
    BlockNBTBase,
    # 常量
    BLOCK_ENTITY_IDS,
    ENTITY_CLASS_MAP,
    BLOCK_NBT_REGISTRY,
    # 容器物品
    ContainerItem,
    # NexusEgo 方块实体 (19 种)
    SignNBT,
    BannerNBT,
    LecternNBT,
    JukeBoxNBT,
    CrafterNBT,
    BrewingStandNBT,
    CommandBlockNBT,
    StructureBlockNBT,
    FrameNBT,
    BeaconNBT,
    HopperNBT,
    DispenserNBT,
    DropperNBT,
    FurnaceNBT,
    BarrelNBT,
    ShulkerBoxNBT,
    ChiseledBookshelfNBT,
    CalibratedSculkSensorNBT,
    DecoratedPotNBT,
    # NovaBuilder 额外方块实体 (10 种)
    BlastFurnaceNBT,
    SmokerNBT,
    EnchantTableNBT,
    EndPortalNBT,
    SpawnerNBT,
    MobSpawnerNBT,
    SkullNBT,
    ComposterNBT,
    CampfireNBT,
    ConduitNBT,
    JigsawNBT,
    # 工厂函数
    create_block_entity,
    get_block_entity_id,
    get_nbt_class_for_block,
    create_nbt_for_block,
)

# -------------------------------------------------------------------- #
# container_handler 导出
# -------------------------------------------------------------------- #

from .container_handler import (
    # NexusEgo 常量
    CONTAINER_MODE_STRUCTURE,
    CONTAINER_MODE_REPLACEITEM,
    CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA,
    MODE_NAMES,
    CONTAINER_CAPACITY,
    # NovaBuilder 常量
    CONTAINER_OPEN_TIMEOUT,
    CONTAINER_VERIFY_TIMEOUT,
    CONTAINER_CLOSE_TIMEOUT,
    ITEM_FILL_TIMEOUT,
    MAX_CONTAINER_SLOTS,
    LARGE_CHEST_SLOTS,
    ENDER_CHEST_SLOTS,
    HOPPER_SLOTS,
    # 异常
    ContainerHandlerError,
    # NexusEgo 数据结构
    ContainerContent,
    ContainerMode,
    # NovaBuilder 数据结构
    ContainerType,
    ContainerOpenResult,
    ItemStackEntry,
    # 容器处理器
    ContainerHandler,
    # 顶层函数
    build_structure_container_nbt,
    build_replaceitem_commands,
    build_chest_data_payload,
    get_container_capacity,
)

# -------------------------------------------------------------------- #
# item_nbt 导出
# -------------------------------------------------------------------- #

from .item_nbt import (
    # 异常
    ItemNBTError,
    # 基类
    ItemNBT,
    ITEM_NBT_TYPES,
    # 物品 NBT 类型 (14 种)
    BookNBT,
    HeadNBT,
    BannerItemNBT,
    ShieldNBT,
    BundleNBT,
    LeatherArmorNBT,
    SmithingArmorNBT,
    SmithingTrimNBT,
    FireworkRocketNBT,
    FireworkStarNBT,
    CrossbowNBT,
    CompassNBT,
    RecoveryCompassNBT,
    GoatHornNBT,
    # 工厂函数
    create_item_nbt,
    get_item_nbt_handler,
)

# -------------------------------------------------------------------- #
# nbt_assigner 导出
# -------------------------------------------------------------------- #

from .nbt_assigner import (
    # 异常
    NBTAssignerError,
    # 数据结构
    NBTAssignmentResult,
    # 注册表
    BLOCK_ENTITY_HANDLERS,
    ITEM_NBT_HANDLERS,
    # 分配器
    NBTAssigner,
    # 便捷函数
    assign_block_nbt,
    assign_item_nbt,
    get_supported_block_entities,
    get_supported_item_types,
)

# -------------------------------------------------------------------- #
# nbt_placer 导出
# -------------------------------------------------------------------- #

from .nbt_placer import (
    # 常量
    PLATFORM_SIZE,
    PLATFORM_RADIUS,
    PLATFORM_BLOCK,
    PLATFORM_BLOCK_RUNTIME_ID,
    PLATFORM_DURATION_TICKS,
    PLATFORM_DURATION_SECONDS,
    MAX_NBT_BLOCKS_PER_TICK,
    MAX_BLOCKS_PER_BATCH,
    NBT_PLACE_COOLDOWN_MS,
    MAX_RETRY_COUNT,
    RETRY_INTERVAL_MS,
    # 枚举
    NBTPlacementMode,
    PlacementStatus,
    # 数据结构
    PlatformConfig,
    PlacementResult,
    # 放置器
    NBTPlacer,
)

# -------------------------------------------------------------------- #
# nbt_mode_selector 导出
# -------------------------------------------------------------------- #

from .nbt_mode_selector import (
    # 常量
    SERVER_OFFICIAL,
    SERVER_NETEASE,
    SERVER_UNKNOWN,
    BLOCK_TYPE_CONTAINER,
    BLOCK_TYPE_ENTITY,
    BLOCK_TYPE_NORMAL,
    SIMPLE_NBT_MAX_FIELDS,
    COMPLEX_NBT_MIN_SIZE,
    # 数据结构
    ModeSelectionResult,
    ModeSelectorConfig,
    # 选择器
    NBTModeSelector,
    # 便捷函数
    select_nbt_mode,
)


__version__ = "1.0.0"

__all__ = [
    # 子模块
    "block_entities",
    "container_handler",
    "item_nbt",
    "nbt_assigner",
    "nbt_mode_selector",
    "nbt_placer",
    # block_entities
    "BlockEntityError", "BlockEntity", "BlockNBTBase",
    "BLOCK_ENTITY_IDS", "ENTITY_CLASS_MAP", "BLOCK_NBT_REGISTRY",
    "ContainerItem",
    "SignNBT", "BannerNBT", "LecternNBT", "JukeBoxNBT", "CrafterNBT",
    "BrewingStandNBT", "CommandBlockNBT", "StructureBlockNBT", "FrameNBT",
    "BeaconNBT", "HopperNBT", "DispenserNBT", "DropperNBT", "FurnaceNBT",
    "BarrelNBT", "ShulkerBoxNBT", "ChiseledBookshelfNBT",
    "CalibratedSculkSensorNBT", "DecoratedPotNBT",
    "BlastFurnaceNBT", "SmokerNBT", "EnchantTableNBT", "EndPortalNBT",
    "SpawnerNBT", "MobSpawnerNBT", "SkullNBT", "ComposterNBT",
    "CampfireNBT", "ConduitNBT", "JigsawNBT",
    "create_block_entity", "get_block_entity_id",
    "get_nbt_class_for_block", "create_nbt_for_block",
    # container_handler
    "CONTAINER_MODE_STRUCTURE", "CONTAINER_MODE_REPLACEITEM",
    "CONTAINER_MODE_PLACE_BLOCK_WITH_CHEST_DATA",
    "MODE_NAMES", "CONTAINER_CAPACITY",
    "CONTAINER_OPEN_TIMEOUT", "CONTAINER_VERIFY_TIMEOUT",
    "CONTAINER_CLOSE_TIMEOUT", "ITEM_FILL_TIMEOUT",
    "MAX_CONTAINER_SLOTS", "LARGE_CHEST_SLOTS",
    "ENDER_CHEST_SLOTS", "HOPPER_SLOTS",
    "ContainerHandlerError",
    "ContainerContent", "ContainerMode",
    "ContainerType", "ContainerOpenResult", "ItemStackEntry",
    "ContainerHandler",
    "build_structure_container_nbt", "build_replaceitem_commands",
    "build_chest_data_payload", "get_container_capacity",
    # item_nbt
    "ItemNBTError", "ItemNBT", "ITEM_NBT_TYPES",
    "BookNBT", "HeadNBT", "BannerItemNBT", "ShieldNBT", "BundleNBT",
    "LeatherArmorNBT", "SmithingArmorNBT", "SmithingTrimNBT",
    "FireworkRocketNBT", "FireworkStarNBT", "CrossbowNBT",
    "CompassNBT", "RecoveryCompassNBT", "GoatHornNBT",
    "create_item_nbt", "get_item_nbt_handler",
    # nbt_assigner
    "NBTAssignerError", "NBTAssignmentResult",
    "BLOCK_ENTITY_HANDLERS", "ITEM_NBT_HANDLERS",
    "NBTAssigner",
    "assign_block_nbt", "assign_item_nbt",
    "get_supported_block_entities", "get_supported_item_types",
    # nbt_placer
    "PLATFORM_SIZE", "PLATFORM_RADIUS", "PLATFORM_BLOCK",
    "PLATFORM_BLOCK_RUNTIME_ID", "PLATFORM_DURATION_TICKS",
    "PLATFORM_DURATION_SECONDS", "MAX_NBT_BLOCKS_PER_TICK",
    "MAX_BLOCKS_PER_BATCH", "NBT_PLACE_COOLDOWN_MS",
    "MAX_RETRY_COUNT", "RETRY_INTERVAL_MS",
    "NBTPlacementMode", "PlacementStatus",
    "PlatformConfig", "PlacementResult",
    "NBTPlacer",
    # nbt_mode_selector
    "SERVER_OFFICIAL", "SERVER_NETEASE", "SERVER_UNKNOWN",
    "BLOCK_TYPE_CONTAINER", "BLOCK_TYPE_ENTITY", "BLOCK_TYPE_NORMAL",
    "SIMPLE_NBT_MAX_FIELDS", "COMPLEX_NBT_MIN_SIZE",
    "ModeSelectionResult", "ModeSelectorConfig",
    "NBTModeSelector",
    "select_nbt_mode",
]
