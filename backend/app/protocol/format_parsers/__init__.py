"""格式解析器包。

逆向来源: NexusE WaterStructure/structure/ 系统
- NexusE v1.6.5: structure/kbdx.go
- NexusE v1.6.5: structure/fuhong.go
- NexusE v1.6.5: structure/gangban.go
- NexusE v1.6.5: structure/axiombp.go

合并自 NexusEgo + NovaBuilder 的通用格式解析器:
    - bdx_parser: BDX 格式解析器 (NexusEgo, 最重要)
    - nbt_parser: 通用 NBT 解析器 (NexusEgo + NovaBuilder NBTParser 兼容层)
    - schematic_parser: Schematic 格式解析器 (NexusEgo + NovaBuilder 常量)
    - mcstructure_parser: MC 结构格式解析器 (NovaBuilder)
    - mcworld_parser: MC 世界格式解析器 (NovaBuilder)
    - mcfunction_parser: MCFunction 命令文件解析器 (NexusEgo)

包含多种专业建筑格式的解析器:
    - KBDX: KBDX格式解析器
    - FuHong: 富宏建筑格式解析器 (V1~V6)
    - GangBan: 钢板建筑格式解析器 (V1~V7)
    - AxiomBP: AxiomBP格式解析器
"""

from __future__ import annotations

from .kbdx_parser import KBDXParser
from .fuhong_parser import FuHongParser
from .gangban_parser import GangBanParser
from .axiombp_parser import AxiomBPParser

# 合并自 NexusEgo + NovaBuilder 的通用格式解析器
from .nbt_parser import (
    NBTError, NBTReader, NBTWriter, NBTParser, parse_snbt,
    nbt_marshal_disk, nbt_unmarshal_disk,
    nbt_marshal_big_endian, nbt_unmarshal_big_endian,
    LITTLE_ENDIAN, BIG_ENDIAN,
)
from .bdx_parser import (
    BDXError, BDXHeaderError, BDXUnknownCommandError, BDXReadError,
    BDXCommand, BDXSignature, BDXResult,
    parse_bdx_bytes, parse_bdx_file,
    reconstruct_blocks, get_command_statistics,
)
from .schematic_parser import (
    SchematicError, SchematicFormatError,
    SchematicBlock, SchematicResult,
    parse_schematic_bytes, parse_schematic_file,
    SCHEMATIC_DEPRECATED_WARNING, SCHEMATIC_MAX_BLOCKS,
    MATERIALS_ALPHA, MATERIALS_POCKET,
)
from .mcstructure_parser import (
    MCStructureParser, MCStructureData, MCStructureBlock, MCStructureEntity,
)
from .mcworld_parser import (
    MCWorldParser, MCWorldData, LevelData, SubChunkData, SubChunkBlock,
)
from .mcfunction_parser import (
    MCFunctionError, MCFunctionCommand, MCFunctionResult,
    parse_mcfunction_text, parse_mcfunction_file, parse_mcfunction_bytes,
)

__all__ = [
    # PocketTerm 原有解析器
    "KBDXParser",
    "FuHongParser",
    "GangBanParser",
    "AxiomBPParser",
    # NBT 解析器 (NexusEgo + NovaBuilder)
    "NBTError", "NBTReader", "NBTWriter", "NBTParser", "parse_snbt",
    "nbt_marshal_disk", "nbt_unmarshal_disk",
    "nbt_marshal_big_endian", "nbt_unmarshal_big_endian",
    "LITTLE_ENDIAN", "BIG_ENDIAN",
    # BDX 解析器 (NexusEgo, 最重要)
    "BDXError", "BDXHeaderError", "BDXUnknownCommandError", "BDXReadError",
    "BDXCommand", "BDXSignature", "BDXResult",
    "parse_bdx_bytes", "parse_bdx_file",
    "reconstruct_blocks", "get_command_statistics",
    # Schematic 解析器 (NexusEgo + NovaBuilder)
    "SchematicError", "SchematicFormatError",
    "SchematicBlock", "SchematicResult",
    "parse_schematic_bytes", "parse_schematic_file",
    "SCHEMATIC_DEPRECATED_WARNING", "SCHEMATIC_MAX_BLOCKS",
    "MATERIALS_ALPHA", "MATERIALS_POCKET",
    # MC 结构/世界解析器 (NovaBuilder)
    "MCStructureParser", "MCStructureData",
    "MCStructureBlock", "MCStructureEntity",
    "MCWorldParser", "MCWorldData", "LevelData",
    "SubChunkData", "SubChunkBlock",
    # MCFunction 解析器 (NexusEgo)
    "MCFunctionError", "MCFunctionCommand", "MCFunctionResult",
    "parse_mcfunction_text", "parse_mcfunction_file", "parse_mcfunction_bytes",
]