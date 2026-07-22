"""block_mapping - 方块映射模块 (合并自 NexusEgo + NovaBuilder)。

本包合并了两个逆向包的方块映射能力:

NexusEgo v1.6.5:
    - block_states: 方块状态解析/序列化/映射器
    - nemc_convertor: NEMC 方块 ID 转换器 (NEMC RID <-> 名 <-> 旧版值 <-> MC RID)

NovaBuilder:
    - block_state: BlockStateDiff / BlockStateParser + 状态常量
    - to_nemc_convertor: ToNEMCConvertor + RuntimeIDPool (方块名+状态 -> 运行时 ID)
    - schematic_mapping: Schematic 旧版 ID -> Bedrock 方块名映射

子模块:
    - :mod:`block_states`: 方块状态映射与管理 (NexusEgo + NovaBuilder 合并)
    - :mod:`nemc_convertor`: NEMC 方块 ID 转换 (NexusEgo + NovaBuilder 合并)
    - :mod:`schematic_mapping`: Schematic 方块 ID 映射 (NovaBuilder)

快速使用::

    from app.protocol.block_mapping import (
        BlockState, NEMCConvertor, ToNEMCConvertor,
        SchematicBlockMapping, to_nemc_runtime_id,
    )

适配网易我的世界 3.8 版本协议。
"""

from __future__ import annotations

from .block_states import (
    BlockStateError,
    BlockState,
    BlockStateMapping,
    BlockStateDiff,
    BlockStateParser,
    BlockStateMapper,
    parse_block_states,
    serialize_block_states,
    get_block_state_property,
    set_block_state_property,
    compare_block_states,
    merge_block_states,
    load_block_mapping_file,
    get_block_mapping,
    DEFAULT_MAPPING_FILE,
    FACING_DIRECTIONS,
    FACING_DIRECTION_NAMES,
    COLOR_VALUES,
    LOG_TYPES,
    LEAF_TYPES,
    PILLAR_AXES,
    SLAB_TYPES,
    STAIR_DIRECTIONS,
)

from .nemc_convertor import (
    NEMCConvertorError,
    NEMC_AIR_RID,
    NEMC_AIR_LEGACY_ID,
    NEMC_AIR_NAME,
    NEMCBlockAnchor,
    NEMCConvertRecord,
    NEMCBlock,
    NEMCConvertor,
    nemc_to_name,
    nemc_rid_to_value,
    nemc_rid_to_mc_rid,
    mc_rid_to_nemc_rid,
    nemc_legacy_to_rid,
    add_anchor_by_legacy_value,
    add_anchor_by_state,
    fuzzy_search_by_state,
    fuzzy_search_by_legacy_value,
    try_best_search_by_legacy_value,
    try_best_search_by_state,
    load_convert_record,
    load_target_block,
    init_nemc_blocks,
    # NovaBuilder
    POOL_ID_117,
    POOL_ID_118,
    AIR_RUNTIME_ID,
    AIR_BLOCK_NAME,
    DEFAULT_BLOCK_VERSION,
    MAX_BLOCK_NAME_LENGTH,
    MAX_BLOCK_STATES,
    NEMCBlockMapping,
    RuntimeIDPool,
    ToNEMCConvertor,
    to_nemc_runtime_id,
    to_nemc_runtime_id_or_air,
)

from .schematic_mapping import (
    SCHEMATIC_BLOCK_MAPPING,
    SchematicBlockMapping,
    get_block_name_from_legacy_id,
    get_legacy_id_from_block,
)

__all__ = [
    # 方块状态 (NexusEgo + NovaBuilder 合并)
    "BlockStateError",
    "BlockState",
    "BlockStateMapping",
    "BlockStateDiff",
    "BlockStateParser",
    "BlockStateMapper",
    "parse_block_states",
    "serialize_block_states",
    "get_block_state_property",
    "set_block_state_property",
    "compare_block_states",
    "merge_block_states",
    "load_block_mapping_file",
    "get_block_mapping",
    "DEFAULT_MAPPING_FILE",
    "FACING_DIRECTIONS",
    "FACING_DIRECTION_NAMES",
    "COLOR_VALUES",
    "LOG_TYPES",
    "LEAF_TYPES",
    "PILLAR_AXES",
    "SLAB_TYPES",
    "STAIR_DIRECTIONS",
    # NEMC 转换 (NexusEgo + NovaBuilder 合并)
    "NEMCConvertorError",
    "NEMC_AIR_RID",
    "NEMC_AIR_LEGACY_ID",
    "NEMC_AIR_NAME",
    "NEMCBlockAnchor",
    "NEMCConvertRecord",
    "NEMCBlock",
    "NEMCConvertor",
    "nemc_to_name",
    "nemc_rid_to_value",
    "nemc_rid_to_mc_rid",
    "mc_rid_to_nemc_rid",
    "nemc_legacy_to_rid",
    "add_anchor_by_legacy_value",
    "add_anchor_by_state",
    "fuzzy_search_by_state",
    "fuzzy_search_by_legacy_value",
    "try_best_search_by_legacy_value",
    "try_best_search_by_state",
    "load_convert_record",
    "load_target_block",
    "init_nemc_blocks",
    "POOL_ID_117",
    "POOL_ID_118",
    "AIR_RUNTIME_ID",
    "AIR_BLOCK_NAME",
    "DEFAULT_BLOCK_VERSION",
    "MAX_BLOCK_NAME_LENGTH",
    "MAX_BLOCK_STATES",
    "NEMCBlockMapping",
    "RuntimeIDPool",
    "ToNEMCConvertor",
    "to_nemc_runtime_id",
    "to_nemc_runtime_id_or_air",
    # Schematic 映射 (NovaBuilder)
    "SCHEMATIC_BLOCK_MAPPING",
    "SchematicBlockMapping",
    "get_block_name_from_legacy_id",
    "get_legacy_id_from_block",
]
