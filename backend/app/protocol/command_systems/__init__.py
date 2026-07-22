"""命令系统集合。

整合自 NexusEgo 和 NovaBuilder 两个逆向包的命令系统模块。

NexusEgo 来源:
    - BDumpCommand:        BDump 命令系统 (Yeah114 + merry-memory 双实现)
    - BDumpCommandStream:  BDump 命令流
    - BDumpEncoder:        BDump 编码器
    - BDumpDecoder:        BDump 解码器
    - CommandUpgrader:     命令升级器 (旧版本命令升级到新版本)

NovaBuilder 来源:
    - PhoenixExecutor:     Phoenix 执行器 (将 PlacePlan 转换为协议操作并执行)
    - PlacePlanner:        Phoenix 规划器 (将抽象结构转换为方块放置计划)
"""

from __future__ import annotations

# NexusEgo 来源
from .bdump_commands import (
    BDumpCommand, BDumpCommandContext, BDumpCommandStream,
    BDumpEncoder, BDumpDecoder,
    BDUMP_ENCODER_VERSION,
    BDUMP_MODE_BLOCK_STATES, BDUMP_MODE_RUNTIME_ID_POOL,
    BDUMP_MODE_COMMAND_BLOCK_DATA, BDUMP_MODE_CHEST_DATA,
    BDUMP_MODE_NBT_DATA,
    create_command_stream, encode_command_stream,
    decode_command_stream, get_command_stream_stats,
    BDUMPError, BDumpEncodeError, BDumpDecodeError,
)
from .command_upgrader import (
    CommandUpgrader, UpgradeResult,
    upgrade_bdx_commands, upgrade_command_block_data,
    upgrade_legacy_block_data,
    COMMAND_UPGRADE_RULES,
    CommandUpgradeError,
)

# NovaBuilder 来源
from .phoenix_planner import PlacePlanner, PlacePlan, PlacePlanItem, PlannedBlock
from .phoenix_executor import (
    PhoenixExecutor, PhoenixExecutorConfig, ExecutionResult, ExecutionStats,
)

__all__ = [
    # BDump (NexusEgo)
    "BDumpCommand", "BDumpCommandContext", "BDumpCommandStream",
    "BDumpEncoder", "BDumpDecoder", "BDUMP_ENCODER_VERSION",
    "BDUMP_MODE_BLOCK_STATES", "BDUMP_MODE_RUNTIME_ID_POOL",
    "BDUMP_MODE_COMMAND_BLOCK_DATA", "BDUMP_MODE_CHEST_DATA",
    "BDUMP_MODE_NBT_DATA",
    "create_command_stream", "encode_command_stream",
    "decode_command_stream", "get_command_stream_stats",
    "BDUMPError", "BDumpEncodeError", "BDumpDecodeError",
    # Upgrader (NexusEgo)
    "CommandUpgrader", "UpgradeResult",
    "upgrade_bdx_commands", "upgrade_command_block_data",
    "upgrade_legacy_block_data",
    "COMMAND_UPGRADE_RULES", "CommandUpgradeError",
    # Phoenix 规划器 (NovaBuilder)
    "PlacePlanner", "PlacePlan", "PlacePlanItem", "PlannedBlock",
    # Phoenix 执行器 (NovaBuilder)
    "PhoenixExecutor", "PhoenixExecutorConfig", "ExecutionResult", "ExecutionStats",
]
