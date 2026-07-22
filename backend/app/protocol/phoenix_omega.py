"""PhoenixBuilder/Omega 建筑导入管线 — Python 翻译版

本模块是 PhoenixBuilder Go 源码中 Omega 导入系统的完整 Python 翻译,
用于在 PocketTerm 中实现高性能建筑导入。

翻译来源:
    - PhoenixBuilder/omega/components/universeImport/entry.go
    - PhoenixBuilder/omega/utils/structure/define.go
    - PhoenixBuilder/omega/utils/structure/builder.go
    - PhoenixBuilder/omega/utils/structure/pos.go
    - PhoenixBuilder/omega/utils/structure/schem.go
    - PhoenixBuilder/omega/utils/structure/schematic.go
    - PhoenixBuilder/omega/utils/structure/bdx.go
    - PhoenixBuilder/omega/utils/structure/block_convert.go
    - PhoenixBuilder/omega/utils/structure/hop_planner.go
    - PhoenixBuilder/fastbuilder/mcstructure/main.go
    - PhoenixBuilder/fastbuilder/bdump/bdump.go

核心架构 (三阶段管线):
    1. **解析阶段 (Parse)**: 解析多种建筑格式 -> 内部统一方块流
    2. **重排阶段 (Rearrange)**: 蛇形路径区块排序 + 按 Y 层分组
    3. **构建阶段 (Build)**: 生成 setblock/fill 指令并发送, 支持进度回调

支持的格式:
    - .schematic  (旧版 MCEdit 格式, NBT + gzip)
    - .schem      (新版 WorldEdit 格式, 含调色板)
    - .bdx        (BDump 格式, Brotli 压缩, 含命令方块)
    - .mcstructure (网易结构格式, NBT 小端序)
    - .building   (Nexus 格式)

基本用法::

    from app.protocol.phoenix_omega import OmegaImporter

    importer = OmegaImporter(
        block_cmd_sender=send_setblock,
        normal_cmd_sender=send_cmd,
        progress_callback=update_progress,
    )
    await importer.import_file(
        file_path="/path/to/building.schematic",
        offset=(0, 64, 0),
    )
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import struct
import time
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Optional,
    AsyncIterator,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)

logger = logging.getLogger("pocketterm.protocol.phoenix_omega")

# ------------------------------------------------------------------
# 新格式解析器导入 (NexusE / NovaBuilder)
# ------------------------------------------------------------------
from .format_parsers import (
    KBDXParser,
    FuHongParser,
    GangBanParser,
    AxiomBPParser,
    MCWorldParser,
    MCFunctionCommand,
    MCStructureParser as NewMCStructureParser,
    NBTParser,
    parse_mcfunction_text as _parse_mcfunction,
)
from .multi_chunk_importer import MultiChunkImporter, ImportConfig as MCI_ImportConfig
from .import_options import ImportOptions, ImportAlgorithm
from .cdump_parser import CDumpParser
from .pixel_art_importer import PixelArtImporter
from .batch_optimizer import BatchOptimizer, BlockEntry
from .blocks import BlockState


# ======================================================================
# 1. 核心数据结构 (Core Data Structures)
# ======================================================================


class CubePos:
    """三维整数坐标 (X, Y, Z)。

    对应 Go 源码中 ``define.CubePos`` 类型, 表示 Minecraft 世界中的
    一个方块位置。实现为命名元组风格, 支持索引访问和解包。

    示例::

        pos = CubePos(10, 64, -5)
        print(pos.x, pos.y, pos.z)  # 10 64 -5
        new_pos = pos + CubePos(1, 0, 0)
    """

    __slots__ = ("_data",)

    def __init__(self, x: int, y: int, z: int) -> None:
        self._data: tuple[int, int, int] = (x, y, z)

    @property
    def x(self) -> int:
        """X 坐标 (东西方向)。"""
        return self._data[0]

    @property
    def y(self) -> int:
        """Y 坐标 (高度)。"""
        return self._data[1]

    @property
    def z(self) -> int:
        """Z 坐标 (南北方向)。"""
        return self._data[2]

    def __getitem__(self, index: int) -> int:
        return self._data[index]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return 3

    def __add__(self, other: "CubePos") -> "CubePos":
        if isinstance(other, CubePos):
            return CubePos(self.x + other.x, self.y + other.y, self.z + other.z)
        return NotImplemented

    def __sub__(self, other: "CubePos") -> "CubePos":
        if isinstance(other, CubePos):
            return CubePos(self.x - other.x, self.y - other.y, self.z - other.z)
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if isinstance(other, CubePos):
            return self._data == other._data
        return False

    def __hash__(self) -> int:
        return hash(self._data)

    def __repr__(self) -> str:
        return f"CubePos({self.x}, {self.y}, {self.z})"

    def out_of_y_bounds(self, y_min: int = -64, y_max: int = 319) -> bool:
        """检查 Y 坐标是否超出高度范围。

        Args:
            y_min: 最低允许高度 (默认 -64, 对应 1.18+ 世界下限)。
            y_max: 最高允许高度 (默认 319, 对应 1.18+ 世界上限)。

        Returns:
            True 如果 Y 坐标超出范围。
        """
        return self.y < y_min or self.y > y_max


@dataclass(frozen=True)
class ChunkPos:
    """区块坐标 (Chunk X, Chunk Z)。

    对应 Go 源码中 ``define.ChunkPos`` 类型。每个区块包含 16x16 个方块。
    区块坐标 = 方块坐标的 X 和 Z 除以 16 (向下取整)。

    示例::

        ch = ChunkPos(0, -1)  # 区块 (0, -1)
        print(ch.x, ch.z)     # 0 -1
    """

    x: int
    z: int

    @staticmethod
    def from_cube_pos(pos: CubePos) -> "ChunkPos":
        """从方块坐标转换为区块坐标。

        Args:
            pos: 方块坐标。

        Returns:
            对应的区块坐标。
        """
        return ChunkPos(pos.x >> 4, pos.z >> 4)

    def __repr__(self) -> str:
        return f"ChunkPos({self.x}, {self.z})"


# ======================================================================
# 2. 方块 IO 数据结构 (Block IO Data Structures)
# ======================================================================


@dataclass
class IOBlockForDecoder:
    """解析器输出的方块数据 (内部中间表示)。

    由格式解析器 (schematic/bdx/mcstructure 解码器) 产生,
    进入重排阶段前使用。

    Attributes:
        pos: 方块的相对坐标 (相对于结构文件原点)。
        rtid: 方块的运行时 ID (Runtime ID, 对应游戏内部方块标识)。
        nbt: 方块的 NBT 数据 (如容器内容、命令方块数据等), 无 NBT 时为 None。
    """

    pos: CubePos
    rtid: int
    nbt: Optional[dict[str, Any]] = None


@dataclass
class IOBlockForBuilder:
    """构建器输入的方块数据 (重排后输出)。

    由重排阶段产生, 进入构建阶段前使用。

    Attributes:
        pos: 方块的世界坐标 (已加上偏移量)。
        rtid: 方块的运行时 ID。
        nbt: 方块的 NBT 数据。
        expand16: 是否使用 fill 指令批量填充 16x16x16 区域 (整子区块优化)。
        hit: 是否已被确认放置 (用于延迟 NBT 放置跟踪)。
    """

    pos: CubePos
    rtid: int
    nbt: Optional[dict[str, Any]] = None
    expand16: bool = False
    hit: bool = False


# ======================================================================
# 3. 命令方块 NBT 结构 (Command Block NBT)
# ======================================================================


@dataclass
class CommandBlockNBT:
    """命令方块 NBT 数据结构。

    对应 Go 源码中 ``CommandBlockNBT`` 结构体。

    Attributes:
        command: 命令方块中存储的命令字符串。
        custom_name: 命令方块的自定义名称。
        execute_on_first_tick: 是否在首个 tick 执行 (0 或 1)。
        tick_delay: 执行延迟 (tick 数)。
        auto: 是否需要红石激活 (0 表示需要红石, 1 表示始终激活)。
        track_output: 是否跟踪输出。
        last_output: 上一次执行输出。
        conditional_mode: 是否为条件模式 (0 无条件, 1 有条件)。
        data: 附加数据值。
    """

    command: str = ""
    custom_name: str = ""
    execute_on_first_tick: int = 0
    tick_delay: int = 0
    auto: int = 0
    track_output: int = 0
    last_output: str = ""
    conditional_mode: int = 0
    data: int = 0


# ======================================================================
# 4. 导入任务与配置 (Import Task & Configuration)
# ======================================================================


@dataclass
class ImportTask:
    """单个导入任务描述。

    对应 Go 源码中 ``universeImportTask`` 结构体。

    Attributes:
        path: 建筑文件的绝对路径。
        offset: 导入基准点 (世界坐标)。
        progress: 当前进度 (已处理的方块数, 用于断点续传)。
    """

    path: str
    offset: CubePos = field(default_factory=lambda: CubePos(0, 0, 0))
    progress: int = 0


@dataclass
class ImportData:
    """导入任务队列数据。

    对应 Go 源码中 ``UniverseImportData`` 结构体。

    Attributes:
        current_task: 当前正在处理的任务 (None 表示无任务)。
        queued_tasks: 排队中的任务列表。
    """

    current_task: Optional[ImportTask] = None
    queued_tasks: list[ImportTask] = field(default_factory=list)


@dataclass
class ImportConfig:
    """导入配置。

    对应 Go 源码中 ``UniverseImport`` 结构体的配置部分。

    Attributes:
        import_speed: 每秒导入普通方块数目 (控制指令发送速率)。
        ignore_nbt: 是否忽略方块 NBT 信息 (如容器内容、命令方块数据)。
        boost_rate: 超频加速比 (控制 fill 批量导入时的速度倍率)。
        auto_continue: 启动时是否自动继续上次未完成的导入。
        checkpoint_file: 断点续传记录文件路径。
    """

    import_speed: int = 100
    ignore_nbt: bool = False
    boost_rate: float = 10.0
    auto_continue: bool = True
    checkpoint_file: str = "omega_import_checkpoint.json"


# ======================================================================
# 5. 方块 ID 常量与映射 (Block ID Constants & Mapping)
# ======================================================================


#: 空气方块的运行时 ID (Air Runtime ID)
#: 对应 Go 源码中 ``chunk.AirRID``
AIR_RUNTIME_ID: int = 0

#: Minecraft 世界高度范围 (1.18+)
#: 对应 Go 源码中 ``define.WorldRange``
WORLD_Y_RANGE: tuple[int, int] = (-64, 319)

#: 子区块在 Y 轴上的层数 (每个子区块 16 层, 24 个子区块覆盖 -64~319)
SUB_CHUNK_COUNT: int = 24

#: 每个子区块的边长 (16x16x16)
SUB_CHUNK_SIZE: int = 16

#: 区块边长 (16x16, 水平方向)
CHUNK_SIZE: int = 16


# ======================================================================
# 6. 格式解析器基类 (Format Parser Base)
# ======================================================================


class FormatParseError(Exception):
    """格式解析错误。

    当解析器无法识别文件格式或文件数据损坏时抛出。
    """

    pass


class FormatNotSupportedError(FormatParseError):
    """格式不支持错误。

    对应 Go 源码中 ``ErrImportFormatNotSupport``。
    """

    pass


@dataclass
class ParseResult:
    """格式解析结果。

    Attributes:
        block_feeder: 方块数据通道 (异步迭代器)。
        cancel_fn: 取消解析的回调函数。
        suggest_min_cache_chunks: 建议的最小缓冲区块数。
        total_blocks: 总方块数 (非空气方块)。
    """

    block_feeder: AsyncIterator[IOBlockForDecoder]
    cancel_fn: Callable[[], None]
    suggest_min_cache_chunks: int
    total_blocks: int


# ======================================================================
# 7. 格式解析器 (Format Parsers)
# ======================================================================


class BaseFormatParser(ABC):
    """格式解析器基类。

    所有格式解析器必须实现 :meth:`decode` 方法。
    """

    @abstractmethod
    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析建筑文件数据。

        Args:
            data: 建筑文件的原始字节数据。
            info_callback: 可选的日志回调, 用于报告解析进度。

        Returns:
            解析结果, 包含方块流和元数据。

        Raises:
            FormatNotSupportedError: 文件格式不支持。
            FormatParseError: 解析过程中发生错误。
        """
        ...


class SchemParser(BaseFormatParser):
    """新版 .schem 格式解析器 (WorldEdit 格式, 含调色板)。

    对应 Go 源码中 ``DecodeSchem`` 函数。

    文件结构:
        - gzip 压缩的 NBT 数据
        - 根标签名: "Schematic"
        - 包含 Palette (调色板), BlockData (变长编码), BlockEntities (实体数据)
    """

    #: .schem 文件调色板中 Java 方块名到运行时 ID 的映射表
    #: 这是一个静态映射表, 在实际使用时需要根据游戏版本填充
    _java_to_runtime_id: dict[str, int] = {}

    @classmethod
    def set_java_mapping(cls, mapping: dict[str, int]) -> None:
        """设置 Java 方块名到运行时 ID 的映射表。

        Args:
            mapping: 键为 Java 方块名字符串 (如 "minecraft:stone"),
                值为对应的运行时 ID。
        """
        cls._java_to_runtime_id = mapping

    @staticmethod
    def _read_var_uint32(data: bytes, offset: int) -> tuple[int, int]:
        """读取变长编码的 uint32 (VarInt 风格, 每字节低 7 位, 最高位为继续标志)。

        对应 Go 源码中 ``writeVarUint32`` 的逆操作。

        Args:
            data: 字节数据。
            offset: 起始偏移量。

        Returns:
            (解码后的整数, 下一个字节偏移量) 元组。
        """
        value = 0
        shift = 0
        while offset < len(data):
            byte = data[offset]
            offset += 1
            value |= (byte & 0x7F) << shift
            if (byte & 0x80) == 0:
                return value, offset
            shift += 7
        raise FormatParseError("VarInt 解码时遇到意外的数据结束")

    @staticmethod
    def _decode_var_uint32_array(data: bytes) -> list[int]:
        """解码整个变长编码的 uint32 数组。

        Args:
            data: 变长编码的字节数据。

        Returns:
            解码后的整数列表。
        """
        result: list[int] = []
        offset = 0
        while offset < len(data):
            value, offset = SchemParser._read_var_uint32(data, offset)
            result.append(value)
        return result

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .schem 文件。

        流程:
            1. 检测 gzip 头并解压
            2. 解析 NBT 数据 (需要 NBT 库支持)
            3. 解码变长 BlockData
            4. 构建调色板映射
            5. 按 Z-Y-X 顺序生成方块流

        Args:
            data: .schem 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的 .schem 文件。
        """
        if info_callback:
            info_callback("正在检测 .schem 格式...")

        # 1. 检测并解压 gzip
        has_gzip = len(data) >= 2 and data[0] == 0x1F and data[1] == 0x8B
        if has_gzip:
            try:
                data = gzip.decompress(data)
            except gzip.BadGzipFile as e:
                raise FormatNotSupportedError(f"gzip 解压失败: {e}") from e
        else:
            # 可能是不带 gzip 的原始 NBT
            pass

        if info_callback:
            info_callback("解压缩数据, 将消耗大量内存")

        # 2. 解析 NBT
        try:
            from .nbt import parse_nbt as _parse_nbt
            schem_data, root_tag = _parse_nbt(data)
        except ImportError:
            raise FormatParseError(
                "需要 NBT 解析库支持, 请确保 app.protocol.nbt 模块可用"
            )
        except Exception as e:
            raise FormatNotSupportedError(f"NBT 解析失败: {e}") from e

        if root_tag != "Schematic":
            raise FormatNotSupportedError(f"根标签名不匹配: 期望 'Schematic', 实际 '{root_tag}'")

        # 3. 提取关键字段
        palette: dict[str, int] = schem_data.get("Palette", {})
        if not palette:
            raise FormatNotSupportedError("缺少 Palette 字段")

        block_data_raw: bytes = schem_data.get("BlockData", b"")
        if not block_data_raw:
            raise FormatNotSupportedError("缺少 BlockData 字段")

        width: int = schem_data.get("Width", 0)
        height: int = schem_data.get("Height", 0)
        length: int = schem_data.get("Length", 0)

        if width <= 0 or height <= 0 or length <= 0:
            raise FormatNotSupportedError(
                f"无效的尺寸: {width}x{height}x{length}"
            )

        if info_callback:
            info_callback("解压缩成功, 正在解码方块数据...")

        # 4. 解码变长 BlockData
        block_indices = self._decode_var_uint32_array(block_data_raw)

        expected_size = height * width * length
        if len(block_indices) != expected_size:
            raise FormatNotSupportedError(
                f"尺寸检查失败: {expected_size} != {len(block_indices)}"
            )

        # 5. 构建调色板映射 (Java 方块名 -> 运行时 ID)
        palette_mapping: dict[int, int] = {}
        for java_name, palette_id in palette.items():
            rtid = self._java_to_runtime_id.get(java_name, AIR_RUNTIME_ID)
            if rtid == AIR_RUNTIME_ID and java_name != "minecraft:air":
                if info_callback:
                    info_callback(f"未知方块 '{java_name}', 视为空气")
            palette_mapping[palette_id] = rtid

        # 6. 提取方块实体 NBT
        block_entities: list[dict[str, Any]] = schem_data.get("BlockEntities", [])
        nbt_map: dict[CubePos, dict[str, Any]] = {}
        for entity in block_entities:
            pos_arr = entity.get("Pos", [0, 0, 0])
            if isinstance(pos_arr, (list, tuple)) and len(pos_arr) >= 3:
                pos = CubePos(int(pos_arr[0]), int(pos_arr[1]), int(pos_arr[2]))
                nbt_map[pos] = entity

        # 7. 统计非空气方块数
        blocks_counter = sum(
            1 for idx in block_indices
            if palette_mapping.get(idx, AIR_RUNTIME_ID) != AIR_RUNTIME_ID
        )

        if info_callback:
            info_callback(
                f"格式匹配成功, 开始解析, 尺寸 [{width}, {height}, {length}] "
                f"方块数量 {blocks_counter}"
            )

        # 8. 构建异步方块流
        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            """按 Z-Y-X 顺序生成方块流 (与 Go 源码一致)。"""
            nonlocal stopped
            for z in range(length):
                for y in range(height):
                    for x in range(width):
                        if stopped:
                            return
                        index = x + z * width + y * length * width
                        palette_idx = block_indices[index]
                        rtid = palette_mapping.get(palette_idx, AIR_RUNTIME_ID)
                        if rtid == AIR_RUNTIME_ID:
                            continue
                        pos = CubePos(x, y, z)
                        nbt = nbt_map.get(pos)
                        yield IOBlockForDecoder(pos=pos, rtid=rtid, nbt=nbt)

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        suggest_chunks = (width // 16) + 2

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=suggest_chunks,
            total_blocks=blocks_counter,
        )


class SchematicParser(BaseFormatParser):
    """旧版 .schematic 格式解析器 (MCEdit 格式)。

    对应 Go 源码中 ``DecodeSchematic`` 函数。

    文件结构:
        - gzip 压缩的 NBT 数据
        - 包含 Blocks (方块 ID 字节数组), Data (附加值字节数组)
        - Width, Height, Length 尺寸字段
    """

    #: 旧版方块 ID 到运行时 ID 的映射
    #: 这是一个静态映射表, 键为 (block_id, data_value) 元组
    _legacy_block_map: dict[tuple[int, int], int] = {}

    @classmethod
    def set_legacy_block_map(cls, mapping: dict[tuple[int, int], int]) -> None:
        """设置旧版方块 ID 到运行时 ID 的映射表。

        Args:
            mapping: 键为 (block_id, data_value) 元组, 值为运行时 ID。
        """
        cls._legacy_block_map = mapping

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .schematic 文件。

        流程:
            1. gzip 解压
            2. 解析 NBT 数据
            3. 按 Z-Y-X 顺序生成方块流

        Args:
            data: .schematic 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的 .schematic 文件。
        """
        if info_callback:
            info_callback("正在检测 .schematic 格式...")

        # 1. gzip 解压
        try:
            data = gzip.decompress(data)
        except gzip.BadGzipFile as e:
            raise FormatNotSupportedError(f"gzip 解压失败: {e}") from e

        if info_callback:
            info_callback("解压缩数据, 将消耗大量内存")

        # 2. 解析 NBT
        try:
            from .nbt import parse_nbt as _parse_nbt
            schem_data, _ = _parse_nbt(data)
        except ImportError:
            raise FormatParseError(
                "需要 NBT 解析库支持, 请确保 app.protocol.nbt 模块可用"
            )
        except Exception as e:
            raise FormatNotSupportedError(f"NBT 解析失败: {e}") from e

        # 3. 提取字段
        blocks: bytes = schem_data.get("Blocks", b"")
        values: bytes = schem_data.get("Data", b"")
        width: int = schem_data.get("Width", 0)
        height: int = schem_data.get("Height", 0)
        length: int = schem_data.get("Length", 0)

        if not blocks or not values:
            raise FormatNotSupportedError("缺少 Blocks 或 Data 字段")

        if width <= 0 or height <= 0 or length <= 0:
            raise FormatNotSupportedError(
                f"无效的尺寸: {width}x{height}x{length}"
            )

        expected_size = width * height * length
        if len(blocks) != expected_size:
            raise FormatNotSupportedError(
                f"尺寸检查失败: {expected_size} != {len(blocks)}"
            )

        # 4. 统计非空气方块
        blocks_counter = sum(1 for b in blocks if b != 0)

        if info_callback:
            info_callback(
                f"格式匹配成功, 开始解析, 尺寸 {[width, height, length]}, "
                f"方块数量 {blocks_counter}"
            )

        # 5. 构建异步方块流
        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            """按 Z-Y-X 顺序生成方块流 (与 Go 源码一致)。"""
            nonlocal stopped
            for z in range(length):
                for y in range(height):
                    for x in range(width):
                        if stopped:
                            return
                        index = x + z * width + y * length * width
                        block_id = blocks[index]
                        if block_id == 0:
                            continue
                        data_val = values[index]
                        rtid = self._legacy_block_map.get(
                            (block_id, data_val), AIR_RUNTIME_ID
                        )
                        if rtid == AIR_RUNTIME_ID:
                            continue
                        yield IOBlockForDecoder(
                            pos=CubePos(x, y, z),
                            rtid=rtid,
                        )

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        suggest_chunks = (width // 16) + 2

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=suggest_chunks,
            total_blocks=blocks_counter,
        )


class BDXParser(BaseFormatParser):
    """BDX (BDump) 格式解析器。

    对应 Go 源码中 ``DecodeBDX`` 和 ``handleBDXCMD`` 函数。

    BDX 格式特点:
        - 文件头: "BD@" (3 字节) 或 "BDX\\0" (4 字节)
        - Brotli 压缩的数据段
        - 基于画笔位置 (brush position) 的指令流
        - 支持命令方块、容器等 NBT 实体

    指令集 (部分):
        - 1: 创建方块名调色板项
        - 7: 放置方块 (含 ID 和附加值)
        - 13: 保留指令
        - 26: 放置命令方块 (含完整 NBT)
        - 31: 切换运行时 ID 池
        - 32/33: 使用运行时 ID 放置方块
        - 88: 终止
    """

    #: 画笔位置 (当前写入位置)
    _brush_pos: CubePos

    #: 方块名调色板 ID 到名称的映射
    _palette_name_map: dict[int, str]

    #: 旧版方块名到运行时 ID 的快速缓存
    _quick_cache: dict[int, int]

    def __init__(self) -> None:
        self._brush_pos = CubePos(0, 0, 0)
        self._palette_name_map = {}
        self._quick_cache = {}

    @staticmethod
    def _read_string(reader: io.BytesIO) -> str:
        """读取以 null 结尾的字符串。

        对应 Go 源码中 ``ReadBrString`` 函数。

        Args:
            reader: 字节流读取器。

        Returns:
            解码后的字符串。
        """
        result = bytearray()
        while True:
            char = reader.read(1)
            if not char or char[0] == 0:
                break
            result.append(char[0])
        return result.decode("utf-8", errors="replace")

    def _get_rtid(self, palette_id: int, data: int) -> int:
        """从调色板 ID 和附加值获取运行时 ID。

        对应 Go 源码中 ``DoubleValueLegacyBlockToRuntimeIDMapper.GetRTID``。

        Args:
            palette_id: 调色板中的方块 ID。
            data: 方块附加值。

        Returns:
            运行时 ID, 如果找不到则返回空气 ID。
        """
        cache_key = (palette_id << 16) | data
        if cache_key in self._quick_cache:
            return self._quick_cache[cache_key]

        block_name = self._palette_name_map.get(palette_id)
        if block_name is None:
            self._quick_cache[cache_key] = AIR_RUNTIME_ID
            return AIR_RUNTIME_ID

        # 尝试映射 (这里使用简化的映射逻辑, 实际需要完整映射表)
        # 在 Go 源码中调用 chunk.LegacyBlockToRuntimeID
        rtid = self._lookup_legacy_block(block_name, data)
        self._quick_cache[cache_key] = rtid
        return rtid

    @staticmethod
    def _lookup_legacy_block(name: str, data: int) -> int:
        """查找旧版方块名对应的运行时 ID。

        这是一个占位方法, 实际使用时需要填充完整的映射表。
        在 Go 源码中对应 ``chunk.LegacyBlockToRuntimeID``。

        Args:
            name: 方块名 (不含 "minecraft:" 前缀)。
            data: 附加值。

        Returns:
            运行时 ID, 如果找不到则返回空气 ID。
        """
        # 简体映射: 仅用于演示, 实际需要完整映射
        _simple_map: dict[str, int] = {
            "stone": 1,
            "grass": 2,
            "dirt": 3,
            "cobblestone": 4,
            "planks": 5,
            "bedrock": 7,
            "sand": 12,
            "gravel": 13,
            "gold_ore": 14,
            "iron_ore": 15,
            "coal_ore": 16,
            "log": 17,
            "leaves": 18,
            "glass": 20,
            "lapis_ore": 21,
            "sandstone": 24,
            "wool": 35,
            "gold_block": 41,
            "iron_block": 42,
            "brick_block": 45,
            "bookshelf": 47,
            "mossy_cobblestone": 48,
            "obsidian": 49,
            "diamond_block": 57,
            "crafting_table": 58,
            "furnace": 61,
            "chest": 54,
            "command_block": 137,
            "repeating_command_block": 188,
            "chain_command_block": 189,
            "air": 0,
        }
        return _simple_map.get(name, AIR_RUNTIME_ID)

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .bdx 文件。

        流程:
            1. 检查文件头 "BD@"
            2. Brotli 解压
            3. 第一遍扫描: 确定方块数
            4. 第二遍解析: 生成方块流

        Args:
            data: .bdx 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的 .bdx 文件。
        """
        if info_callback:
            info_callback("正在检查 BDX 文件...")

        # 1. 检查文件头
        file_buf = io.BytesIO(data)
        header = file_buf.read(3)
        if header != b"BD@":
            raise FormatNotSupportedError(
                f"不是有效的 BDX 文件 (期望 'BD@', 实际 '{header!r}')"
            )

        # 2. Brotli 解压
        try:
            import brotli
            compressed = file_buf.read()
            decompressed = brotli.decompress(compressed)
        except ImportError:
            # 尝试使用系统 brotli 工具
            raise FormatParseError(
                "需要 brotli 库支持, 请安装: pip install brotli"
            )
        except Exception as e:
            raise FormatNotSupportedError(f"Brotli 解压失败: {e}") from e

        if not decompressed:
            raise FormatNotSupportedError("Brotli 解压后数据为空")

        if info_callback:
            info_callback("正在检查 BDX 文件, 需要消耗大量时间")

        # 3. 第一遍扫描: 确定方块数和边界
        reader = io.BytesIO(decompressed)
        self._brush_pos = CubePos(0, 0, 0)
        self._palette_name_map = {}
        self._quick_cache = {}

        block_counter = 0
        min_x = 0
        max_x = 0
        author = ""

        # 读取 BDX 子头
        sub_header = reader.read(4)
        if sub_header == b"BDX\x00":
            author = self._read_string(reader)

        while True:
            cmd_byte = reader.read(1)
            if not cmd_byte:
                break
            cmd = cmd_byte[0]
            if cmd == 88:
                break
            elif cmd == 1:
                # 创建调色板项
                name = self._read_string(reader)
                self._palette_name_map[block_counter] = name
                block_counter = 0  # 重置, 下面会重新计数
            elif cmd == 7:
                # 放置方块
                reader.read(4)  # blockId(2) + blockData(2)
                x = self._brush_pos.x
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                block_counter += 1
            elif cmd == 26:
                # 命令方块
                reader.read(4)  # cbmode
                for _ in range(3):
                    self._read_string(reader)
                reader.read(8)  # tickdelay + flags
                x = self._brush_pos.x
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                block_counter += 1
            elif cmd == 2:
                # 绝对 X 跳转 (uint16)
                reader.read(2)
                self._brush_pos = CubePos(self._brush_pos.x, 0, 0)
            elif cmd == 4:
                # 绝对 Y 跳转 (uint16)
                reader.read(2)
                self._brush_pos = CubePos(self._brush_pos.x, self._brush_pos.y, 0)
            elif cmd == 6:
                # 绝对 Z 跳转 (uint16)
                reader.read(2)
            else:
                # 跳过其他指令
                pass

        if info_callback:
            info_callback(f"文件检查完毕, 方块数 {block_counter}, 作者: {author}")

        # 4. 第二遍解析: 生成方块流
        reader = io.BytesIO(decompressed)
        self._brush_pos = CubePos(0, 0, 0)
        self._palette_name_map = {}
        self._quick_cache = {}
        palette_counter = 0

        sub_header = reader.read(4)
        if sub_header == b"BDX\x00":
            self._read_string(reader)  # 跳过 author

        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            """解析 BDX 指令流并生成方块。"""
            nonlocal stopped, palette_counter
            while True:
                if stopped:
                    return
                cmd_byte = reader.read(1)
                if not cmd_byte:
                    break
                cmd = cmd_byte[0]
                if cmd == 88:
                    break
                elif cmd == 1:
                    name = self._read_string(reader)
                    self._palette_name_map[palette_counter] = name
                    palette_counter += 1
                elif cmd == 7:
                    block_id = struct.unpack(">H", reader.read(2))[0]
                    block_data = struct.unpack(">H", reader.read(2))[0]
                    rtid = self._get_rtid(block_id, block_data)
                    if rtid != AIR_RUNTIME_ID:
                        yield IOBlockForDecoder(
                            pos=CubePos(
                                self._brush_pos.x,
                                self._brush_pos.y,
                                self._brush_pos.z,
                            ),
                            rtid=rtid,
                        )
                elif cmd == 26:
                    cbmode = struct.unpack(">I", reader.read(4))[0]
                    command = self._read_string(reader)
                    custom_name = self._read_string(reader)
                    last_output = self._read_string(reader)
                    tick_delay = struct.unpack(">I", reader.read(4))[0]
                    flags = reader.read(4)
                    block_name = "command_block"
                    if cbmode == 2:
                        block_name = "repeating_command_block"
                    elif cbmode == 3:
                        block_name = "chain_command_block"
                    rtid = self._lookup_legacy_block(block_name, 0)
                    nbt = {
                        "id": "CommandBlock",
                        "Command": command,
                        "CustomName": custom_name,
                        "ExecuteOnFirstTick": flags[0],
                        "TickDelay": tick_delay,
                        "auto": 1 - flags[3],
                        "TrackOutput": flags[1],
                        "LastOutput": last_output,
                        "conditionalMode": flags[2],
                    }
                    yield IOBlockForDecoder(
                        pos=CubePos(
                            self._brush_pos.x,
                            self._brush_pos.y,
                            self._brush_pos.z,
                        ),
                        rtid=rtid,
                        nbt=nbt,
                    )
                elif cmd == 2:
                    self._brush_pos = CubePos(
                        self._brush_pos.x + struct.unpack(">H", reader.read(2))[0],
                        0,
                        0,
                    )
                elif cmd == 3:
                    self._brush_pos = CubePos(
                        self._brush_pos.x + 1, 0, 0
                    )
                elif cmd == 4:
                    self._brush_pos = CubePos(
                        self._brush_pos.x,
                        self._brush_pos.y + struct.unpack(">H", reader.read(2))[0],
                        0,
                    )
                elif cmd == 5:
                    self._brush_pos = CubePos(
                        self._brush_pos.x, self._brush_pos.y + 1, 0
                    )
                elif cmd == 6:
                    self._brush_pos = CubePos(
                        self._brush_pos.x,
                        self._brush_pos.y,
                        self._brush_pos.z + struct.unpack(">H", reader.read(2))[0],
                    )
                elif cmd == 8:
                    self._brush_pos = CubePos(
                        self._brush_pos.x,
                        self._brush_pos.y,
                        self._brush_pos.z + 1,
                    )
                elif cmd == 9:
                    pass  # NOP
                elif cmd == 10:
                    self._brush_pos = CubePos(
                        self._brush_pos.x + struct.unpack(">I", reader.read(4))[0],
                        0,
                        0,
                    )
                elif cmd == 11:
                    self._brush_pos = CubePos(
                        self._brush_pos.x,
                        self._brush_pos.y + struct.unpack(">I", reader.read(4))[0],
                        0,
                    )
                elif cmd == 12:
                    self._brush_pos = CubePos(
                        self._brush_pos.x,
                        self._brush_pos.y,
                        self._brush_pos.z + struct.unpack(">I", reader.read(4))[0],
                    )
                elif cmd == 14:
                    self._brush_pos = CubePos(
                        self._brush_pos.x + 1, self._brush_pos.y, self._brush_pos.z
                    )
                elif cmd == 15:
                    self._brush_pos = CubePos(
                        self._brush_pos.x - 1, self._brush_pos.y, self._brush_pos.z
                    )
                elif cmd == 16:
                    self._brush_pos = CubePos(
                        self._brush_pos.x, self._brush_pos.y + 1, self._brush_pos.z
                    )
                elif cmd == 17:
                    self._brush_pos = CubePos(
                        self._brush_pos.x, self._brush_pos.y - 1, self._brush_pos.z
                    )
                elif cmd == 18:
                    self._brush_pos = CubePos(
                        self._brush_pos.x, self._brush_pos.y, self._brush_pos.z + 1
                    )
                elif cmd == 19:
                    self._brush_pos = CubePos(
                        self._brush_pos.x, self._brush_pos.y, self._brush_pos.z - 1
                    )
                elif cmd in (20, 21, 22, 23, 24, 25, 28, 29, 30):
                    # 有符号跳转指令
                    size = 2 if cmd in (20, 22, 24, 28, 29, 30) else 4
                    jump_raw = reader.read(size)
                    if cmd == 20:
                        self._brush_pos = CubePos(
                            self._brush_pos.x + struct.unpack(">h", jump_raw)[0],
                            self._brush_pos.y,
                            self._brush_pos.z,
                        )
                    elif cmd == 21:
                        self._brush_pos = CubePos(
                            self._brush_pos.x + struct.unpack(">i", jump_raw)[0],
                            self._brush_pos.y,
                            self._brush_pos.z,
                        )
                    elif cmd in (22, 23):
                        fmt = ">h" if cmd == 22 else ">i"
                        self._brush_pos = CubePos(
                            self._brush_pos.x,
                            self._brush_pos.y + struct.unpack(fmt, jump_raw)[0],
                            self._brush_pos.z,
                        )
                    elif cmd in (24, 25):
                        fmt = ">h" if cmd == 24 else ">i"
                        self._brush_pos = CubePos(
                            self._brush_pos.x,
                            self._brush_pos.y,
                            self._brush_pos.z + struct.unpack(fmt, jump_raw)[0],
                        )
                    elif cmd == 28:
                        self._brush_pos = CubePos(
                            self._brush_pos.x + struct.unpack(">b", jump_raw)[0],
                            self._brush_pos.y,
                            self._brush_pos.z,
                        )
                    elif cmd == 29:
                        self._brush_pos = CubePos(
                            self._brush_pos.x,
                            self._brush_pos.y + struct.unpack(">b", jump_raw)[0],
                            self._brush_pos.z,
                        )
                    elif cmd == 30:
                        self._brush_pos = CubePos(
                            self._brush_pos.x,
                            self._brush_pos.y,
                            self._brush_pos.z + struct.unpack(">b", jump_raw)[0],
                        )
                else:
                    # 跳过未知指令
                    pass

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        suggest_chunks = (max_x - min_x) // 16 + 2

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=suggest_chunks,
            total_blocks=block_counter,
        )


class MCStructureParser(BaseFormatParser):
    """网易 .mcstructure 格式解析器。

    对应 Go 源码中 ``GetMCStructureData`` 和 ``DumpBlocks`` 函数。

    网易结构文件使用 NBT 小端序编码, 包含:
        - palette: 调色板 (方块名 + 方块状态 + 附加值)
        - block_indices: 前景层和背景层的方块索引
        - block_position_data: 方块实体数据 (容器、命令方块等)

    特殊处理:
        - 含水方块 (前景非空气 + 背景为水)
        - 箱子/陷阱箱交替放置 (防止连接)
    """

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .mcstructure 文件。

        Args:
            data: .mcstructure 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的 .mcstructure 文件。
        """
        if info_callback:
            info_callback("正在解析 .mcstructure 格式...")

        try:
            from .nbt import parse_nbt_le as _parse_nbt
            structure, _ = _parse_nbt(data)
        except ImportError:
            raise FormatParseError(
                "需要 NBT 解析库支持, 请确保 app.protocol.nbt 模块可用"
            )
        except Exception as e:
            raise FormatNotSupportedError(f"mcstructure NBT 解析失败: {e}") from e

        # 提取结构数据
        structure_data = structure.get("structure", {})
        if not isinstance(structure_data, dict):
            raise FormatNotSupportedError("缺少 structure 字段")

        palette_data = structure_data.get("palette", {})
        default_palette = palette_data.get("default", {})
        if not isinstance(default_palette, dict):
            raise FormatNotSupportedError("缺少 palette.default 字段")

        block_palette = default_palette.get("block_palette", [])
        if not isinstance(block_palette, list) or not block_palette:
            raise FormatNotSupportedError("block_palette 为空")

        # 提取调色板信息
        palette_names: list[str] = []
        palette_states: list[str] = []
        palette_datas: list[int] = []

        for entry in block_palette:
            if isinstance(entry, dict):
                name = entry.get("name", "minecraft:air")
                palette_names.append(name.replace("minecraft:", ""))
                palette_states.append(str(entry.get("states", {})))
                palette_datas.append(entry.get("val", 0))
            else:
                palette_names.append("air")
                palette_states.append("")
                palette_datas.append(0)

        # 提取方块索引
        block_indices = structure_data.get("block_indices", [])
        if not isinstance(block_indices, list) or len(block_indices) < 2:
            raise FormatNotSupportedError("block_indices 格式不正确")

        foreground = block_indices[0] if len(block_indices) > 0 else []
        background = block_indices[1] if len(block_indices) > 1 else []

        if not isinstance(foreground, list):
            foreground = []
        if not isinstance(background, list):
            background = []

        # 提取方块实体数据
        block_nbt: dict[int, dict[str, Any]] = {}
        block_position_data = default_palette.get("block_position_data", {})
        if isinstance(block_position_data, dict):
            for key, value in block_position_data.items():
                try:
                    idx = int(key)
                    if isinstance(value, dict):
                        block_nbt[idx] = {"block_position_data": value}
                except (ValueError, TypeError):
                    pass

        # 确定结构尺寸
        # 从结构数据中提取尺寸信息 (如果存在)
        size_x = structure_data.get("size", [1, 1, 1])
        if isinstance(size_x, list) and len(size_x) >= 3:
            size = (int(size_x[0]), int(size_x[1]), int(size_x[2]))
        else:
            size = (1, 1, len(foreground))

        if info_callback:
            info_callback(
                f"格式匹配成功, 开始解析, 尺寸 {size}, "
                f"调色板大小 {len(palette_names)}"
            )

        # 构建方块流
        stopped = False
        blocks_counter = 0

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            """按索引顺序生成方块流。"""
            nonlocal stopped, blocks_counter
            total = len(foreground)
            for idx in range(total):
                if stopped:
                    return
                fg_id = foreground[idx] if idx < len(foreground) else -1
                bg_id = background[idx] if idx < len(background) else -1

                # 处理前景方块
                if fg_id != -1 and fg_id < len(palette_names):
                    name = palette_names[fg_id]
                    if name not in ("air", "undefined", ""):
                        rtid = self._lookup_block_rtid(name, palette_datas[fg_id])
                        if rtid != AIR_RUNTIME_ID:
                            # 计算坐标
                            x = idx % size[0]
                            z = (idx // size[0]) % size[2]
                            y = idx // (size[0] * size[2])
                            pos = CubePos(x, y, z)
                            nbt = block_nbt.get(idx)
                            yield IOBlockForDecoder(pos=pos, rtid=rtid, nbt=nbt)
                            blocks_counter += 1

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=256,
            total_blocks=blocks_counter,
        )

    @staticmethod
    def _lookup_block_rtid(name: str, data: int) -> int:
        """查找方块名对应的运行时 ID。

        这是一个占位方法, 实际使用时需要填充完整的映射表。

        Args:
            name: 方块名 (不含 "minecraft:" 前缀)。
            data: 附加值。

        Returns:
            运行时 ID, 如果找不到则返回空气 ID。
        """
        _simple_map: dict[str, int] = {
            "stone": 1, "grass": 2, "dirt": 3, "cobblestone": 4,
            "planks": 5, "bedrock": 7, "sand": 12, "gravel": 13,
            "glass": 20, "wool": 35, "chest": 54, "furnace": 61,
            "command_block": 137, "repeating_command_block": 188,
            "chain_command_block": 189, "air": 0,
            "water": 9, "flowing_water": 8, "lava": 11, "flowing_lava": 10,
        }
        return _simple_map.get(name, AIR_RUNTIME_ID)


class BuildingParser(BaseFormatParser):
    """Nexus .building 格式解析器。

    这是 Nexus 工具链使用的自定义建筑格式, 通常为 JSON 编码。
    格式包含方块列表和 NBT 实体数据。

    注意: 这是一个占位实现, 具体格式细节需要根据实际文件调整。
    """

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .building 文件。

        Args:
            data: .building 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。
        """
        if info_callback:
            info_callback("正在解析 .building 格式...")

        try:
            building_data = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise FormatNotSupportedError(f"JSON 解析失败: {e}") from e

        blocks = building_data.get("blocks", [])
        if not isinstance(blocks, list):
            raise FormatNotSupportedError("blocks 字段格式不正确")

        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            """按顺序生成方块流。"""
            nonlocal stopped
            for block_entry in blocks:
                if stopped:
                    return
                if isinstance(block_entry, dict):
                    pos = block_entry.get("pos", [0, 0, 0])
                    name = block_entry.get("name", "")
                    rtid = BDXParser._lookup_legacy_block(
                        name.replace("minecraft:", ""), 0
                    )
                    if rtid != AIR_RUNTIME_ID:
                        yield IOBlockForDecoder(
                            pos=CubePos(
                                int(pos[0]), int(pos[1]), int(pos[2])
                            ),
                            rtid=rtid,
                            nbt=block_entry.get("nbt"),
                        )

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=256,
            total_blocks=len(blocks),
        )


# ======================================================================
# 8. 格式检测与自动解析 (Format Detection & Auto-Parse)
# ======================================================================


#: 文件扩展名到解析器的映射表
EXTENSION_PARSER_MAP: dict[str, type[BaseFormatParser]] = {
    ".schem": SchemParser,
    ".schematic": SchematicParser,
    ".bdx": BDXParser,
    ".mcstructure": MCStructureParser,
    ".building": BuildingParser,
    # 新格式解析器在下方适配器类定义后通过 update 注册
}


def get_parser_for_file(file_path: str) -> BaseFormatParser:
    """根据文件扩展名获取对应的解析器。

    Args:
        file_path: 建筑文件路径。

    Returns:
        对应的格式解析器实例。

    Raises:
        FormatNotSupportedError: 不支持的格式。
    """
    ext = Path(file_path).suffix.lower()
    parser_cls = EXTENSION_PARSER_MAP.get(ext)
    if parser_cls is None:
        raise FormatNotSupportedError(
            f"不支持的文件格式: {ext}, 支持的格式: "
            f"{', '.join(EXTENSION_PARSER_MAP.keys())}"
        )
    return parser_cls()


# ======================================================================
# 9. 方块重排引擎 (Block Rearrangement Engine)
# ======================================================================


@dataclass
class _ChunkBuffer:
    """内部区块缓冲区。

    用于重排阶段: 将流式方块按区块分组, 以便进行蛇形排序。

    Attributes:
        chunk: 区块数据映射 (子区块 Y 索引 -> 16x16x16 方块数组)。
        nbts: 方块 NBT 数据映射。
        chunk_pos: 区块坐标。
    """

    chunk: dict[int, list[list[list[int]]]] = field(default_factory=dict)
    nbts: dict[CubePos, dict[str, Any]] = field(default_factory=dict)
    chunk_pos: ChunkPos = field(default_factory=lambda: ChunkPos(0, 0))


class BlockRearranger:
    """方块重排引擎。

    对应 Go 源码中 ``AlterImportPosStartAndSpeedWithReArrangeOnce`` 函数。

    核心功能:
        1. 将方块流按区块分组缓冲
        2. 蛇形路径 (snake-path) 排序区块
        3. 按 Y 层 (子区块) 分组输出
        4. 支持断点续传 (跳过已处理的方块)
        5. 检测全子区块填充优化 (fill 指令)

    蛇形排序:
        偶数 X 行: Z 递增
        奇数 X 行: Z 递减 (蛇形回头)
        这样可以减少玩家在区块间的移动距离。
    """

    def __init__(
        self,
        offset: CubePos,
        start_from: int = 0,
        suggest_min_cache_chunks: int = 256,
        output_channel_size: int = 16 * 16 * 16 * 24 * 3,
    ):
        """
        Args:
            offset: 坐标偏移量 (将相对坐标转换为世界坐标)。
            start_from: 断点续传起始位置 (已处理的方块数)。
            suggest_min_cache_chunks: 触发重排的缓冲区块数阈值。
            output_channel_size: 输出通道缓冲区大小。
        """
        self._offset = offset
        self._start_from = start_from
        self._suggest_min_cache_chunks = suggest_min_cache_chunks
        self._output_channel_size = output_channel_size

    async def rearrange(
        self,
        block_feeder: AsyncIterator[IOBlockForDecoder],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[IOBlockForBuilder]:
        """对输入方块流进行重排并输出。

        两阶段异步流水线:
            1. 重排协程: 接收方块, 按区块分组, 达到阈值后发送到缓冲通道
            2. 转储协程: 从缓冲通道取区块, 蛇形排序, 按 Y 层输出

        Args:
            block_feeder: 输入方块流 (来自解析器)。
            cancel_check: 可选的取消检查回调 (返回 True 表示取消)。

        Yields:
            重排后的方块 (已添加偏移量, 按优化顺序排列)。
        """
        import asyncio

        # 缓冲区
        chunks: dict[ChunkPos, _ChunkBuffer] = {}
        chunks_queue: asyncio.Queue[dict[ChunkPos, _ChunkBuffer]] = asyncio.Queue(
            maxsize=2
        )
        output_queue: asyncio.Queue[Optional[IOBlockForBuilder]] = asyncio.Queue(
            maxsize=self._output_channel_size
        )

        stopped = False
        counter = 0

        # 当前区块引用
        last_chunk_pos = ChunkPos(0, 0)
        last_chunk = _ChunkBuffer(chunk_pos=last_chunk_pos)
        chunks[last_chunk_pos] = last_chunk

        def _ensure_sub_chunk(chunk_buf: _ChunkBuffer, sub_y: int) -> list[list[list[int]]]:
            """确保子区块存在, 返回 16x16x16 的三维数组 (Z, Y, X 顺序)。"""
            if sub_y not in chunk_buf.chunk:
                # 初始化为空气 (AIR_RUNTIME_ID)
                chunk_buf.chunk[sub_y] = [
                    [[AIR_RUNTIME_ID for _ in range(SUB_CHUNK_SIZE)]
                     for _ in range(SUB_CHUNK_SIZE)]
                    for _ in range(SUB_CHUNK_SIZE)
                ]
            return chunk_buf.chunk[sub_y]

        # ===== 重排协程 =====
        async def _rearranger() -> None:
            nonlocal stopped, counter, last_chunk_pos, last_chunk
            async for block in block_feeder:
                if stopped:
                    return
                if cancel_check and cancel_check():
                    stopped = True
                    return

                # 添加偏移量
                block.pos = block.pos + self._offset

                # 检查 Y 范围
                if block.pos.out_of_y_bounds():
                    logger.warning("位于 %s 的方块超出高度上限", block.pos)
                    continue

                # 确定区块
                chunk_pos = ChunkPos.from_cube_pos(block.pos)
                if chunk_pos != last_chunk_pos:
                    if chunk_pos in chunks:
                        last_chunk = chunks[chunk_pos]
                    else:
                        last_chunk = _ChunkBuffer(chunk_pos=chunk_pos)
                        chunks[chunk_pos] = last_chunk
                    last_chunk_pos = chunk_pos

                # 写入方块到子区块
                sub_y = (block.pos.y - WORLD_Y_RANGE[0]) // SUB_CHUNK_SIZE
                local_x = block.pos.x & 0xF
                local_y = block.pos.y - WORLD_Y_RANGE[0] - sub_y * SUB_CHUNK_SIZE
                local_z = block.pos.z & 0xF

                storage = _ensure_sub_chunk(last_chunk, sub_y)
                if 0 <= local_y < SUB_CHUNK_SIZE:
                    storage[local_z][local_y][local_x] = block.rtid

                # 存储 NBT
                if block.nbt is not None:
                    last_chunk.nbts[block.pos] = block.nbt

                # 缓冲区达到阈值, 发送到转储协程
                if len(chunks) > self._suggest_min_cache_chunks:
                    await chunks_queue.put(chunks)
                    chunks.clear()
                    last_chunk_pos = ChunkPos(0, 0)
                    last_chunk = _ChunkBuffer(chunk_pos=last_chunk_pos)
                    chunks[last_chunk_pos] = last_chunk

            # 发送剩余区块
            if chunks and not stopped:
                await chunks_queue.put(chunks)
            await chunks_queue.put(None)  # 结束信号

        # ===== 转储协程 =====
        async def _dumper() -> None:
            nonlocal stopped, counter
            while True:
                batch = await chunks_queue.get()
                if batch is None:
                    break

                # 收集区块坐标
                chunk_xs = sorted(set(cp.x for cp in batch.keys()))
                chunk_zs = sorted(set(cp.z for cp in batch.keys()))

                # 蛇形排序区块
                reordered: list[ChunkPos] = []
                for i, cx in enumerate(chunk_xs):
                    if i % 2 == 0:
                        # 偶数行: Z 递增
                        for cz in chunk_zs:
                            cp = ChunkPos(cx, cz)
                            if cp in batch:
                                reordered.append(cp)
                    else:
                        # 奇数行: Z 递减 (蛇形回头)
                        for cz in reversed(chunk_zs):
                            cp = ChunkPos(cx, cz)
                            if cp in batch:
                                reordered.append(cp)

                # 按排序后的区块顺序输出
                for cp in reordered:
                    chunk_buf = batch[cp]
                    for sub_y in range(SUB_CHUNK_COUNT):
                        if sub_y not in chunk_buf.chunk:
                            continue
                        storage = chunk_buf.chunk[sub_y]
                        base_y = WORLD_Y_RANGE[0] + sub_y * SUB_CHUNK_SIZE
                        base_x = cp.x * SUB_CHUNK_SIZE
                        base_z = cp.z * SUB_CHUNK_SIZE

                        # 检查是否整个子区块都是同一方块 (fill 优化)
                        first_blk = storage[0][0][0]
                        all_same = True
                        for z in range(SUB_CHUNK_SIZE):
                            for y in range(SUB_CHUNK_SIZE):
                                for x in range(SUB_CHUNK_SIZE):
                                    if storage[z][y][x] != first_blk:
                                        all_same = False
                                        break
                                if not all_same:
                                    break
                            if not all_same:
                                break

                        if all_same and first_blk != AIR_RUNTIME_ID:
                            # 整子区块填充优化
                            if counter < self._start_from:
                                counter += SUB_CHUNK_SIZE * SUB_CHUNK_SIZE * SUB_CHUNK_SIZE
                            else:
                                if stopped:
                                    return
                                await output_queue.put(IOBlockForBuilder(
                                    pos=CubePos(base_x, base_y, base_z),
                                    rtid=first_blk,
                                    expand16=True,
                                ))
                            continue

                        # 逐个方块输出
                        for x in range(SUB_CHUNK_SIZE):
                            for z in range(SUB_CHUNK_SIZE):
                                for sy in range(SUB_CHUNK_SIZE):
                                    if stopped:
                                        return
                                    rtid = storage[z][sy][x]
                                    if rtid == AIR_RUNTIME_ID:
                                        continue
                                    p = CubePos(
                                        base_x + x,
                                        base_y + sy,
                                        base_z + z,
                                    )
                                    if counter < self._start_from:
                                        counter += 1
                                        continue
                                    nbt = chunk_buf.nbts.get(p)
                                    await output_queue.put(IOBlockForBuilder(
                                        pos=p,
                                        rtid=rtid,
                                        nbt=nbt,
                                    ))

            await output_queue.put(None)  # 结束信号

        # 启动两个协程 (保存引用防止 GC 回收)
        _tasks = [
            asyncio.create_task(_rearranger()),
            asyncio.create_task(_dumper()),
        ]

        # 从输出队列读取
        while True:
            block = await output_queue.get()
            if block is None:
                break
            yield block
            if stopped:
                break


# ======================================================================
# 10. 构建器 (Builder)
# ======================================================================


class OmegaBuilder:
    """Omega 构建器 — 将重排后的方块流转换为游戏指令并发送。

    对应 Go 源码中 ``Builder.Build`` 方法。

    功能:
        1. 生成 setblock 指令 (单方块放置)
        2. 生成 fill 指令 (整子区块填充, 16x16x16)
        3. 玩家自动传送 (区块间移动)
        4. 速率控制 (speed 参数控制每秒方块数)
        5. 超频加速 (boost_rate 控制 fill 指令的速度)
        6. 命令方块 NBT 处理 (fallback 机制)
        7. 进度回调
    """

    def __init__(
        self,
        block_cmd_sender: Callable[[str], Any],
        normal_cmd_sender: Optional[Callable[[str], Any]] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
        final_wait_time: int = 3,
        ignore_nbt: bool = False,
        init_pos_getter: Optional[Callable[[], CubePos]] = None,
    ):
        """
        Args:
            block_cmd_sender: 方块指令发送回调 (用于 setblock)。
            normal_cmd_sender: 普通指令发送回调 (用于 tp 等)。
            progress_callback: 进度回调 (参数为当前已处理的方块数)。
            final_wait_time: 最终等待时间 (秒)。
            ignore_nbt: 是否忽略 NBT 数据。
            init_pos_getter: 获取初始玩家位置的回调。
        """
        self._block_sender = block_cmd_sender
        self._normal_sender = normal_cmd_sender or block_cmd_sender
        self._progress_cb = progress_callback
        self._final_wait_time = final_wait_time
        self._ignore_nbt = ignore_nbt
        self._init_pos_getter = init_pos_getter
        self._stop = False

    async def build(
        self,
        blocks: AsyncIterator[IOBlockForBuilder],
        speed: int = 100,
        boost_sleep_time: float = 0.0,
    ) -> None:
        """执行构建。

        遍历方块流, 生成并发送对应的游戏指令。

        Args:
            blocks: 重排后的方块流。
            speed: 每秒导入方块数 (控制指令发送速率)。
            boost_sleep_time: fill 批量导入时的额外等待时间。
        """
        import asyncio

        self._stop = False
        counter = 0
        last_pos = self._init_pos_getter() if self._init_pos_getter else CubePos(0, 0, 0)

        # 速率控制
        if speed > 0:
            delay_per_block = 1.0 / speed
        else:
            delay_per_block = 0.0

        async for block in blocks:
            if self._stop:
                return

            # 检查是否需要传送玩家
            x_move = block.pos.x - last_pos.x
            z_move = block.pos.z - last_pos.z

            if counter == 0:
                # 首个方块: 传送到附近
                self._normal_sender(
                    f"tp @s {block.pos.x} 320 {block.pos.z}"
                )
                last_pos = block.pos
                await asyncio.sleep(3.0)

            if (x_move * x_move) > 256 or (z_move * z_move) > 256:
                # 区块间移动: 传送玩家
                self._normal_sender(
                    f"tp @s {block.pos.x} 320 {block.pos.z}"
                )
                last_pos = block.pos
                await asyncio.sleep(0.05)

            # 进度回调
            if self._progress_cb:
                self._progress_cb(counter)

            # 生成指令
            if block.expand16:
                # 整子区块 fill 优化
                blk_name = self._rtid_to_block_name(block.rtid)
                cmd = (
                    f"fill {block.pos.x} {block.pos.y} {block.pos.z} "
                    f"{block.pos.x + 15} {block.pos.y + 15} {block.pos.z + 15} "
                    f"{blk_name} 0"
                )
                self._normal_sender(cmd)
                counter += 4096
                if boost_sleep_time > 0:
                    await asyncio.sleep(boost_sleep_time)
            else:
                # 单方块 setblock
                blk_name = self._rtid_to_block_name(block.rtid)
                cmd = (
                    f"setblock {block.pos.x} {block.pos.y} {block.pos.z} "
                    f"{blk_name} 0"
                )
                self._block_sender(cmd)
                counter += 1

            # 速率控制
            if delay_per_block > 0:
                await asyncio.sleep(delay_per_block)

        if self._progress_cb:
            self._progress_cb(-1)  # 完成信号

    @staticmethod
    def _rtid_to_block_name(rtid: int) -> str:
        """将运行时 ID 转换为方块名 (简化版)。

        在 Go 源码中对应 ``chunk.RuntimeIDToLegacyBlock``。

        Args:
            rtid: 运行时 ID。

        Returns:
            方块名 (不含 "minecraft:" 前缀, 如 "stone")。
        """
        _reverse_map: dict[int, str] = {
            0: "air",
            1: "stone",
            2: "grass",
            3: "dirt",
            4: "cobblestone",
            5: "planks",
            7: "bedrock",
            12: "sand",
            13: "gravel",
            20: "glass",
            35: "wool",
            41: "gold_block",
            42: "iron_block",
            45: "brick_block",
            47: "bookshelf",
            49: "obsidian",
            54: "chest",
            57: "diamond_block",
            58: "crafting_table",
            61: "furnace",
            137: "command_block",
            188: "repeating_command_block",
            189: "chain_command_block",
        }
        return _reverse_map.get(rtid, "stone")

    def stop(self) -> None:
        """停止构建。"""
        self._stop = True


# ======================================================================
# 11. Omega 导入器 (Omega Importer — 主入口)
# ======================================================================


class OmegaImporter:
    """Omega 建筑导入器 — 三阶段管线主入口。

    对应 Go 源码中 ``UniverseImport`` 组件。

    三阶段管线:
        1. **parse**: 自动检测格式并解析建筑文件
        2. **rearrange**: 蛇形路径排序优化
        3. **build**: 生成并发送游戏指令

    功能:
        - 自动格式检测 (.schem, .schematic, .bdx, .mcstructure, .building)
        - 断点续传 (通过 checkpoint JSON 文件)
        - 进度回调
        - 任务队列管理
        - 取消/停止支持

    基本用法::

        importer = OmegaImporter(
            block_cmd_sender=send_wocmd,
            normal_cmd_sender=send_cmd,
            progress_callback=lambda n: print(f"进度: {n}"),
        )
        await importer.import_file("/path/to/building.schematic", (0, 64, 0))
    """

    def __init__(
        self,
        block_cmd_sender: Callable[[str], Any],
        normal_cmd_sender: Optional[Callable[[str], Any]] = None,
        progress_callback: Optional[Callable[[int], None]] = None,
        config: Optional[ImportConfig] = None,
        init_pos_getter: Optional[Callable[[], CubePos]] = None,
        import_options: Optional[ImportOptions] = None,
    ):
        """
        Args:
            block_cmd_sender: 方块指令发送回调 (用于 setblock, 通过 WOCmd 发送)。
            normal_cmd_sender: 普通指令发送回调 (用于 tp 等)。
            progress_callback: 进度回调 (参数为当前已处理的方块数)。
            config: 导入配置 (如不提供则使用默认值)。
            init_pos_getter: 获取初始玩家位置的回调。
            import_options: 导入选项 (控制 multi_chunk, pixel_art 等高级功能)。
        """
        self._block_sender = block_cmd_sender
        self._normal_sender = normal_cmd_sender or block_cmd_sender
        self._progress_cb = progress_callback
        self._config = config or ImportConfig()
        self._init_pos_getter = init_pos_getter
        self._import_options = import_options
        self._data = ImportData()
        self._current_builder: Optional[OmegaBuilder] = None
        self._file_changed = False

    async def import_file(
        self,
        file_path: str,
        offset: tuple[int, int, int] = (0, 0, 0),
        progress: int = 0,
    ) -> None:
        """导入单个建筑文件。

        Args:
            file_path: 建筑文件的绝对路径。
            offset: 导入基准点 (世界坐标) 的 (x, y, z) 元组。
            progress: 断点续传起始进度 (已处理的方块数)。

        Raises:
            FileNotFoundError: 文件不存在。
            FormatNotSupportedError: 文件格式不支持。
            FormatParseError: 解析过程中发生错误。
        """
        offset_pos = CubePos(*offset)

        logger.info(
            "尝试处理任务 %s 起点(%d %d %d) 从 %d 方块处开始导入",
            file_path, offset_pos.x, offset_pos.y, offset_pos.z, progress,
        )

        # 1. 读取文件
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        with open(file_path, "rb") as fp:
            data = fp.read()

        if not data:
            raise FormatParseError(f"文件为空: {file_path}")

        # 2. 解析 (Parse)
        parser = get_parser_for_file(file_path)
        logger.info("使用解析器: %s", type(parser).__name__)

        file_name = os.path.basename(file_path)

        parse_result = await parser.decode(data, lambda s: logger.info(s))
        logger.info(
            "文件成功被解析, 总方块数 %d, 将开始优化导入顺序",
            parse_result.total_blocks,
        )

        # 3. 重排 (Rearrange)
        rearranger = BlockRearranger(
            offset=offset_pos,
            start_from=progress,
            suggest_min_cache_chunks=parse_result.suggest_min_cache_chunks,
        )

        rearranged_blocks = rearranger.rearrange(parse_result.block_feeder)

        # 4. 构建 (Build)
        boost_sleep_time = (4096.0 / (self._config.boost_rate * self._config.import_speed))

        builder = OmegaBuilder(
            block_cmd_sender=self._block_sender,
            normal_cmd_sender=self._normal_sender,
            progress_callback=self._progress_cb,
            final_wait_time=3,
            ignore_nbt=self._config.ignore_nbt,
            init_pos_getter=self._init_pos_getter,
        )

        self._current_builder = builder

        await builder.build(
            rearranged_blocks,
            speed=self._config.import_speed,
            boost_sleep_time=boost_sleep_time,
        )

        self._current_builder = None
        logger.info("导入完成: %s", file_path)

    def cancel(self) -> None:
        """取消当前导入任务。"""
        if self._current_builder:
            self._current_builder.stop()
            self._current_builder = None

    def queue_task(self, file_path: str, offset: tuple[int, int, int] = (0, 0, 0)) -> None:
        """将导入任务添加到队列。

        Args:
            file_path: 建筑文件路径。
            offset: 导入基准点。
        """
        self._data.queued_tasks.append(ImportTask(
            path=file_path,
            offset=CubePos(*offset),
        ))

    async def process_queue(self) -> None:
        """处理任务队列中的所有任务。"""
        while self._data.queued_tasks:
            self._data.current_task = self._data.queued_tasks.pop(0)
            self._file_changed = True
            try:
                await self.import_file(
                    file_path=self._data.current_task.path,
                    offset=(
                        self._data.current_task.offset.x,
                        self._data.current_task.offset.y,
                        self._data.current_task.offset.z,
                    ),
                    progress=self._data.current_task.progress,
                )
            except Exception as e:
                logger.error("导入任务失败: %s, 错误: %s", self._data.current_task.path, e)
            finally:
                self._data.current_task = None
                self._file_changed = True

    def save_checkpoint(self, file_path: str) -> None:
        """保存断点续传数据。

        Args:
            file_path: 断点文件路径。
        """
        if not self._file_changed:
            return
        data = {
            "current_task": None,
            "queued_tasks": [
                {
                    "path": t.path,
                    "offset": [t.offset.x, t.offset.y, t.offset.z],
                    "progress": t.progress,
                }
                for t in self._data.queued_tasks
            ],
        }
        if self._data.current_task:
            data["current_task"] = {
                "path": self._data.current_task.path,
                "offset": [
                    self._data.current_task.offset.x,
                    self._data.current_task.offset.y,
                    self._data.current_task.offset.z,
                ],
                "progress": self._data.current_task.progress,
            }
        with open(file_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        self._file_changed = False

    def load_checkpoint(self, file_path: str) -> None:
        """加载断点续传数据。

        Args:
            file_path: 断点文件路径。
        """
        if not os.path.isfile(file_path):
            return
        with open(file_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if data.get("current_task"):
            t = data["current_task"]
            self._data.current_task = ImportTask(
                path=t["path"],
                offset=CubePos(*t["offset"]),
                progress=t.get("progress", 0),
            )
        for t in data.get("queued_tasks", []):
            self._data.queued_tasks.append(ImportTask(
                path=t["path"],
                offset=CubePos(*t["offset"]),
                progress=t.get("progress", 0),
            ))

    # ------------------------------------------------------------------
    # 高级导入模式
    # ------------------------------------------------------------------
    async def import_multi_chunk(
        self,
        file_paths: list[str],
        offset: tuple[int, int, int] = (0, 0, 0),
        grid_size: int = 3,
        algorithm: str = "auto",
    ) -> None:
        """多区块合并导入 — 将多个建筑文件合并到 NxN 区块网格中导入。

        Args:
            file_paths: 建筑文件路径列表。
            offset: 导入基准点 (世界坐标)。
            grid_size: 区块网格大小 (如 3 表示 3x3 区块)。
            algorithm: 导入算法 ("auto", "cube_expand", "inner_to_outer", "snake")。

        Raises:
            FileNotFoundError: 文件不存在。
            FormatNotSupportedError: 文件格式不支持。
        """
        offset_pos = CubePos(*offset)

        logger.info(
            "多区块导入: %d 个文件, 网格 %dx%d, 算法 %s",
            len(file_paths), grid_size, grid_size, algorithm,
        )

        multi_importer = MultiChunkImporter(
            chunk_size=CHUNK_SIZE,
            grid_size=grid_size,
        )

        # 配置
        mc_config = MCI_ImportConfig(
            include_nbt=not self._config.ignore_nbt,
            include_command_blocks=True,
        )
        multi_importer.config = mc_config

        # 解析所有文件
        all_chunk_data: dict[tuple[int, int], list[Any]] = {}
        max_x = 0
        max_y = 0
        max_z = 0

        for idx, file_path in enumerate(file_paths):
            if not os.path.isfile(file_path):
                raise FileNotFoundError(f"文件不存在: {file_path}")

            with open(file_path, "rb") as fp:
                data = fp.read()

            parser = get_parser_for_file(file_path)
            parse_result = await parser.decode(data, lambda s: logger.info(s))

            # 收集方块
            blocks: list[Any] = []
            async for block in parse_result.block_feeder:
                blocks.append(block)
                max_x = max(max_x, block.pos.x)
                max_y = max(max_y, block.pos.y)
                max_z = max(max_z, block.pos.z)

            # 确定区块位置
            cx = (idx % grid_size)
            cz = (idx // grid_size)
            all_chunk_data[(cx, cz)] = blocks

            logger.info(
                "文件 %d/%d: %s, 方块数 %d, 区块位置 (%d, %d)",
                idx + 1, len(file_paths),
                os.path.basename(file_path),
                len(blocks), cx, cz,
            )

        # 计算总尺寸
        size_x = max_x + 1 + grid_size * CHUNK_SIZE
        size_y = max_y + 1
        size_z = max_z + 1 + grid_size * CHUNK_SIZE

        # 使用多区块导入器
        result = await multi_importer.import_blocks(
            chunks=all_chunk_data,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            origin_x=offset_pos.x,
            origin_y=offset_pos.y,
            origin_z=offset_pos.z,
            sender=self._block_sender,
            progress_callback=self._progress_cb,
        )

        logger.info(
            "多区块导入完成: %d 个文件, 总方块数 %d",
            len(file_paths), result,
        )

    async def import_pixel_art(
        self,
        image_path: str,
        offset: tuple[int, int, int] = (0, 0, 0),
        dither: bool = True,
        dither_algorithm: str = "floyd_steinberg",
        scale_mode: str = "fit",
        orientation: str = "horizontal",
    ) -> None:
        """像素艺术导入 — 将图片转换为 Minecraft 方块艺术。

        Args:
            image_path: 图片文件路径。
            offset: 导入基准点 (世界坐标)。
            dither: 是否启用抖动 (dithering)。
            dither_algorithm: 抖动算法 ("floyd_steinberg", "ordered", "none")。
            scale_mode: 缩放模式 ("fit", "fill", "stretch")。
            orientation: 朝向 ("horizontal", "vertical")。

        Raises:
            FileNotFoundError: 图片文件不存在。
            ValueError: 参数无效。
        """
        if not os.path.isfile(image_path):
            raise FileNotFoundError(f"图片文件不存在: {image_path}")

        offset_pos = CubePos(*offset)

        logger.info(
            "像素艺术导入: %s, 抖动=%s 算法=%s 缩放=%s 朝向=%s",
            image_path, dither, dither_algorithm, scale_mode, orientation,
        )

        pixel_importer = PixelArtImporter(
            dither_enabled=dither,
            dither_algorithm=dither_algorithm,
            scale_mode=scale_mode,
            orientation=orientation,
        )

        with open(image_path, "rb") as fp:
            image_data = fp.read()

        # 解析图片并生成方块数据
        blocks = await pixel_importer.parse_image(image_data)

        if not blocks:
            logger.warning("像素艺术导入: 未生成任何方块")
            return

        logger.info(
            "像素艺术导入: 生成 %d 个方块", len(blocks)
        )

        # 转换为绝对坐标
        abs_blocks = [
            BlockEntry(
                x=b.x + offset_pos.x,
                y=b.y + offset_pos.y,
                z=b.z + offset_pos.z,
                block=b.block,
            )
            for b in blocks
        ]

        # 使用批量优化器发送
        optimizer = BatchOptimizer(self._block_sender)
        commands = optimizer.merge_z_axis(abs_blocks)
        await optimizer.send_commands(commands, self._progress_cb)

        logger.info("像素艺术导入完成: %s", image_path)

    async def import_cdump(
        self,
        file_path: str,
        offset: tuple[int, int, int] = (0, 0, 0),
    ) -> None:
        """CDump 命令导入 — 直接执行 CDump 格式的命令序列。

        Args:
            file_path: CDump 文件路径。
            offset: 导入基准点 (世界坐标, 用于坐标偏移)。

        Raises:
            FileNotFoundError: 文件不存在。
            FormatNotSupportedError: 格式不支持。
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"文件不存在: {file_path}")

        with open(file_path, "rb") as fp:
            data = fp.read()

        offset_pos = CubePos(*offset)

        logger.info("CDump 导入: %s, 偏移 (%d, %d, %d)",
                     file_path, offset_pos.x, offset_pos.y, offset_pos.z)

        parser = CDumpParser()
        commands = parser.parse(data)

        if not commands:
            logger.warning("CDump 导入: 无命令")
            return

        logger.info("CDump 导入: 共 %d 条命令", len(commands))

        # 执行命令
        for i, cmd in enumerate(commands):
            command_str = cmd.to_command_string(offset=offset_pos)
            await self._normal_sender(command_str)
            if self._progress_cb and i % 10 == 0:
                self._progress_cb(i)

        logger.info("CDump 导入完成: %s", file_path)


# ======================================================================
# 12. 蛇形路径规划器 (Hop Planner)
# ======================================================================


@dataclass
class HopPos:
    """跳跃点 (玩家传送目标)。

    对应 Go 源码中 ``ExportHopPos`` 结构体。

    Attributes:
        pos: 跳跃点世界坐标。
        linked_chunks: 关联的区块列表。
        cached_mark: 缓存标记 (是否已处理)。
    """

    pos: CubePos
    linked_chunks: list["ExportedChunkPos"] = field(default_factory=list)
    cached_mark: bool = False


@dataclass
class ExportedChunkPos:
    """导出区块位置。

    对应 Go 源码中 ``ExportedChunkPos`` 结构体。

    Attributes:
        pos: 区块坐标。
        master_hop: 主跳跃点。
        cached_mark: 缓存标记。
    """

    pos: ChunkPos
    master_hop: Optional[HopPos] = None
    cached_mark: bool = False


def plan_hop_path(
    start_x: int,
    start_z: int,
    end_x: int,
    end_z: int,
    recept_range_by_chunk: int = 4,
) -> tuple[dict[CubePos, HopPos], dict[ChunkPos, ExportedChunkPos]]:
    """规划蛇形路径跳跃点。

    对应 Go 源码中 ``PlanHopSwapPath`` 函数。

    将大区域划分为多个跳跃点, 每个跳跃点覆盖若干个区块。
    玩家在每个跳跃点处停留, 处理完关联区块后移动到下一个跳跃点。

    蛇形排序:
        偶数 X 行: Z 递增
        奇数 X 行: Z 递减

    Args:
        start_x: 起始 X 坐标。
        start_z: 起始 Z 坐标。
        end_x: 结束 X 坐标。
        end_z: 结束 Z 坐标。
        recept_range_by_chunk: 每个跳跃点的区块覆盖范围。

    Returns:
        (跳跃点映射, 区块映射) 元组。
    """
    chunk_size = 16
    recept_range = chunk_size * recept_range_by_chunk

    # 对齐到区块边界
    align_sx = ((start_x - chunk_size + 1) // chunk_size) * chunk_size
    align_sz = ((start_z - chunk_size + 1) // chunk_size) * chunk_size
    align_ex = (end_x // chunk_size) * chunk_size
    align_ez = (end_z // chunk_size) * chunk_size

    hop_x_points = (align_ex - align_sx + chunk_size + recept_range - 1) // recept_range
    hop_z_points = (align_ez - align_sz + chunk_size + recept_range - 1) // recept_range

    if hop_x_points <= 0:
        hop_x_points = 1
    if hop_z_points <= 0:
        hop_z_points = 1

    prefer_half_hop_x = int((align_ex - align_sx + chunk_size) / (hop_x_points * 2))
    prefer_half_hop_z = int((align_ez - align_sz + chunk_size) / (hop_z_points * 2))

    if prefer_half_hop_x <= 0:
        prefer_half_hop_x = 1
    if prefer_half_hop_z <= 0:
        prefer_half_hop_z = 1

    hop_x_start = align_sx + prefer_half_hop_x
    hop_z_start = align_sz + prefer_half_hop_z

    # 生成跳跃点 X 坐标
    hop_x_array: list[int] = []
    for i in range(hop_x_points):
        hop_x_array.append(hop_x_start + i * 2 * prefer_half_hop_x)

    # 生成跳跃点 Z 坐标
    hop_z_array: list[int] = []
    for i in range(hop_z_points):
        hop_z_array.append(hop_z_start + i * 2 * prefer_half_hop_z)

    # 创建跳跃点 (蛇形)
    hop_points: dict[CubePos, HopPos] = {}
    for i, x in enumerate(hop_x_array):
        if i % 2 == 0:
            # 偶数行: Z 递增
            z_iter = hop_z_array
        else:
            # 奇数行: Z 递减 (蛇形)
            z_iter = list(reversed(hop_z_array))
        for z in z_iter:
            p = CubePos(x, 320, z)
            hop_points[p] = HopPos(pos=p)

    # 将区块绑定到跳跃点
    chunk_pos_map: dict[ChunkPos, ExportedChunkPos] = {}
    for xi in range(align_sx // chunk_size, align_ex // chunk_size + 1):
        for zi in range(align_sz // chunk_size, align_ez // chunk_size + 1):
            x, z = xi * chunk_size, zi * chunk_size
            x_half_hops = (x - align_sx) // prefer_half_hop_x
            hop_x = hop_x_start + (x_half_hops // 2) * 2 * prefer_half_hop_x
            z_half_hops = (z - align_sz) // prefer_half_hop_z
            hop_z = hop_z_start + (z_half_hops // 2) * 2 * prefer_half_hop_z
            hop_key = CubePos(hop_x, 320, hop_z)
            cp = ChunkPos(xi, zi)
            if hop_key in hop_points:
                ecp = ExportedChunkPos(pos=cp, master_hop=hop_points[hop_key])
                chunk_pos_map[cp] = ecp
                hop_points[hop_key].linked_chunks.append(ecp)

    return hop_points, chunk_pos_map


# ======================================================================
# 14. 新格式解析器适配器 (New Format Parser Adapters)
# ======================================================================


class KBDXFormatParser(BaseFormatParser):
    """KBDX 格式解析器适配器 (包装 KBDXParser)。

    KBDX 是基于 KubeJS 的建筑格式, 包含方块数据、NBT 和命令方块。
    逆向自 NexusE v1.6.5: structure/kbdx.go
    """

    def __init__(self) -> None:
        self._parser = KBDXParser()

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .kbdx 文件。

        流程:
            1. 使用 KBDXParser 解析原始数据
            2. 将解析结果转换为 Omega 管线格式

        Args:
            data: .kbdx 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的 .kbdx 文件。
        """
        if info_callback:
            info_callback("正在检测 KBDX 格式...")

        try:
            result = self._parser.parse(data)
        except Exception as exc:
            raise FormatNotSupportedError(f"KBDX 解析失败: {exc}") from exc

        if not result or not result.blocks:
            raise FormatNotSupportedError("KBDX 解析结果为空")

        blocks = result.blocks
        total_blocks = len(blocks)

        if info_callback:
            info_callback(
                f"KBDX 格式匹配成功, 方块数量 {total_blocks}"
            )

        # 构建异步方块流
        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            nonlocal stopped
            for block in blocks:
                if stopped:
                    return
                pos = CubePos(block.x, block.y, block.z)
                nbt = block.nbt if hasattr(block, 'nbt') else None
                yield IOBlockForDecoder(
                    pos=pos,
                    rtid=0,  # KBDX 使用方块名而非 RTID
                    nbt=nbt,
                )

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=4,
            total_blocks=total_blocks,
        )


class FuHongFormatParser(BaseFormatParser):
    """富宏建筑格式解析器适配器 (包装 FuHongParser)。

    富宏格式 (V1~V6) 是一个中国 Minecraft 社区的建筑格式,
    逆向自 NexusE v1.6.5: structure/fuhong.go
    """

    def __init__(self) -> None:
        self._parser = FuHongParser()

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .fuhong 文件。

        Args:
            data: .fuhong 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的富宏格式文件。
        """
        if info_callback:
            info_callback("正在检测富宏格式...")

        try:
            result = self._parser.parse(data)
        except Exception as exc:
            raise FormatNotSupportedError(f"富宏格式解析失败: {exc}") from exc

        if not result or not result.blocks:
            raise FormatNotSupportedError("富宏格式解析结果为空")

        blocks = result.blocks
        total_blocks = len(blocks)

        if info_callback:
            info_callback(
                f"富宏格式匹配成功, 方块数量 {total_blocks}"
            )

        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            nonlocal stopped
            for block in blocks:
                if stopped:
                    return
                pos = CubePos(block.x, block.y, block.z)
                yield IOBlockForDecoder(
                    pos=pos,
                    rtid=0,
                    nbt=getattr(block, 'nbt', None),
                )

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=4,
            total_blocks=total_blocks,
        )


class GangBanFormatParser(BaseFormatParser):
    """钢板建筑格式解析器适配器 (包装 GangBanParser)。

    钢板格式 (V1~V7) 是一个中国 Minecraft 社区的建筑格式,
    逆向自 NexusE v1.6.5: structure/gangban.go
    """

    def __init__(self) -> None:
        self._parser = GangBanParser()

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .gangban 文件。

        Args:
            data: .gangban 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的钢板格式文件。
        """
        if info_callback:
            info_callback("正在检测钢板格式...")

        try:
            result = self._parser.parse(data)
        except Exception as exc:
            raise FormatNotSupportedError(f"钢板格式解析失败: {exc}") from exc

        if not result or not result.blocks:
            raise FormatNotSupportedError("钢板格式解析结果为空")

        blocks = result.blocks
        total_blocks = len(blocks)

        if info_callback:
            info_callback(
                f"钢板格式匹配成功, 方块数量 {total_blocks}"
            )

        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            nonlocal stopped
            for block in blocks:
                if stopped:
                    return
                pos = CubePos(block.x, block.y, block.z)
                yield IOBlockForDecoder(
                    pos=pos,
                    rtid=0,
                    nbt=getattr(block, 'nbt', None),
                )

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=4,
            total_blocks=total_blocks,
        )


class AxiomBPFormatParser(BaseFormatParser):
    """AxiomBP 格式解析器适配器 (包装 AxiomBPParser)。

    AxiomBP 是 Axiom 建筑项目使用的格式,
    逆向自 NexusE v1.6.5: structure/axiombp.go
    """

    def __init__(self) -> None:
        self._parser = AxiomBPParser()

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .axiombp 文件。

        Args:
            data: .axiombp 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的 AxiomBP 格式文件。
        """
        if info_callback:
            info_callback("正在检测 AxiomBP 格式...")

        try:
            result = self._parser.parse(data)
        except Exception as exc:
            raise FormatNotSupportedError(f"AxiomBP 格式解析失败: {exc}") from exc

        if not result or not result.blocks:
            raise FormatNotSupportedError("AxiomBP 格式解析结果为空")

        blocks = result.blocks
        total_blocks = len(blocks)

        if info_callback:
            info_callback(
                f"AxiomBP 格式匹配成功, 方块数量 {total_blocks}"
            )

        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            nonlocal stopped
            for block in blocks:
                if stopped:
                    return
                pos = CubePos(block.x, block.y, block.z)
                yield IOBlockForDecoder(
                    pos=pos,
                    rtid=0,
                    nbt=getattr(block, 'nbt', None),
                )

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=4,
            total_blocks=total_blocks,
        )


class CDumpFormatParser(BaseFormatParser):
    """CDump 格式解析器适配器 (包装 CDumpParser)。

    CDump 是 NovaBuilder 的命令导出格式,
    逆向自 NovaBuilder galaxy/import/cdump.go
    """

    def __init__(self) -> None:
        self._parser = CDumpParser()

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .cdump 文件。

        Args:
            data: .cdump 文件的原始字节数据。
            info_callback: 可选的日志回调。

        Returns:
            解析结果。

        Raises:
            FormatNotSupportedError: 不是有效的 CDump 格式文件。
        """
        if info_callback:
            info_callback("正在检测 CDump 格式...")

        try:
            commands = self._parser.parse(data)
        except Exception as exc:
            raise FormatNotSupportedError(f"CDump 解析失败: {exc}") from exc

        if not commands:
            raise FormatNotSupportedError("CDump 解析结果为空")

        total_commands = len(commands)

        if info_callback:
            info_callback(
                f"CDump 格式匹配成功, 命令数量 {total_commands}"
            )

        stopped = False

        async def block_generator() -> AsyncIterator[IOBlockForDecoder]:
            """CDump 格式不产生方块流, 但为了兼容 Omega 管线,
            生成一个空的方块流。实际命令在执行时直接发送。"""
            nonlocal stopped
            if stopped:
                return
            # 产生一个哨兵值表示这是 CDump 模式
            # 在构建阶段由 OmegaImporter 特殊处理
            yield IOBlockForDecoder(
                pos=CubePos(0, 0, 0),
                rtid=-1,  # 哨兵: CDump 模式
                nbt={"cdump_commands": [c.to_dict() for c in commands]},
            )

        def cancel() -> None:
            nonlocal stopped
            stopped = True

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=cancel,
            suggest_min_cache_chunks=1,
            total_blocks=total_commands,
        )


class MCWorldFormatParser(BaseFormatParser):
    """MCWorld 格式解析器适配器 (包装 MCWorldParser)。

    MCWorld 是 Minecraft 基岩版的世界存档格式 (zip 包含 level.dat + chunks)。
    逆向自 NexusE/NovaBuilder: structure/mcworld.go
    """

    def __init__(self) -> None:
        self._parser = MCWorldParser()

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .mcworld 文件。

        Args:
            data: .mcworld 文件的原始字节数据。
            info_callback: 可选的信息回调函数。

        Returns:
            ParseResult 包含方块流和 NBT 数据。
        """
        import tempfile, os

        # MCWorldParser 需要文件路径, 先写入临时文件
        with tempfile.NamedTemporaryFile(suffix=".mcworld", delete=False) as f:
            f.write(data)
            tmp_path = f.name

        try:
            if info_callback:
                info_callback("正在解析 MCWorld 存档...")
            world_data = self._parser.parse_file(tmp_path)
        finally:
            os.unlink(tmp_path)

        if info_callback:
            info_callback(f"解析完成: {len(world_data.subchunks)} 个子区块")

        def block_generator():
            for subchunk in world_data.subchunks:
                for block in subchunk.blocks:
                    yield IOBlockForDecoder(
                        x=block.x,
                        y=block.y,
                        z=block.z,
                        rtid=block.runtime_id,
                        nbt=None,
                    )

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=lambda: None,
            suggest_min_cache_chunks=4,
            total_blocks=sum(len(sc.blocks) for sc in world_data.subchunks),
        )


class MCFunctionFormatParser(BaseFormatParser):
    """MCFunction 格式解析器适配器。

    MCFunction 是 Minecraft 的命令脚本格式, 每行一条命令。
    逆向自 NexusE v1.6.5: structure/mcfunction.go
    """

    def __init__(self) -> None:
        pass

    async def decode(
        self, data: bytes, info_callback: Optional[Callable[[str], None]] = None
    ) -> ParseResult:
        """解析 .mcfunction 文件。

        Args:
            data: .mcfunction 文件的原始字节数据 (UTF-8 文本)。
            info_callback: 可选的信息回调函数。

        Returns:
            ParseResult 包含命令流 (rtid=-1 哨兵, nbt 存命令列表)。
        """
        if info_callback:
            info_callback("正在解析 MCFunction 命令文件...")

        text = data.decode("utf-8", errors="replace")
        result = _parse_mcfunction(text)

        if info_callback:
            info_callback(f"解析完成: {len(result.commands)} 条命令")

        def block_generator():
            for cmd in result.commands:
                if cmd.is_comment or cmd.is_empty:
                    continue
                yield IOBlockForDecoder(
                    x=0, y=0, z=0,
                    rtid=-1,  # 哨兵: MCFunction 模式
                    nbt={"command": cmd.raw_text, "name": cmd.name, "args": cmd.args},
                )

        return ParseResult(
            block_feeder=block_generator(),
            cancel_fn=lambda: None,
            suggest_min_cache_chunks=1,
            total_blocks=len(result.commands),
        )


# 注册新格式解析器到 EXTENSION_PARSER_MAP (适配器类定义后)
EXTENSION_PARSER_MAP.update({
    ".kbdx": KBDXFormatParser,
    ".fuhong": FuHongFormatParser,
    ".gangban": GangBanFormatParser,
    ".axiombp": AxiomBPFormatParser,
    ".cdump": CDumpFormatParser,
    ".mcworld": MCWorldFormatParser,
    ".mcfunction": MCFunctionFormatParser,
})


def detect_format(data: bytes) -> Optional[str]:
    """检测建筑文件的格式。

    通过文件魔数和结构特征自动检测格式。

    Args:
        data: 文件的原始字节数据。

    Returns:
        格式名称 (".schem", ".schematic", ".bdx", ".mcstructure", ".building")
        或 None (无法检测)。
    """
    # 检测 BDX: "BD@" 头
    if data[:3] == b"BD@":
        return ".bdx"

    # 检测 gzip (可能是 .schem 或 .schematic)
    if data[:2] == b"\x1f\x8b":
        try:
            decompressed = gzip.decompress(data)
            # 检查 NBT 根标签
            if decompressed[:10] == b"\x0a\x00\x09Schematic":
                return ".schem"
            # 旧版 .schematic 的 NBT 根标签
            if b"Schematic" in decompressed[:50]:
                return ".schematic"
        except Exception:
            pass

    # 检测 mcstructure (NBT 小端序)
    if len(data) >= 10:
        try:
            # NBT 复合标签 (TAG_Compound = 10) + 名称
            if data[0] == 10:
                from .nbt import parse_nbt_le as _parse_nbt
                parsed, _ = _parse_nbt(data)
                if "structure" in parsed:
                    return ".mcstructure"
        except Exception:
            pass

    # 检测 JSON (.building)
    if data[:1] == b"{":
        try:
            json.loads(data.decode("utf-8"))
            return ".building"
        except Exception:
            pass

    return None


__all__ = [
    # 核心数据结构
    "CubePos",
    "ChunkPos",
    "IOBlockForDecoder",
    "IOBlockForBuilder",
    "CommandBlockNBT",
    # 导入任务
    "ImportTask",
    "ImportData",
    "ImportConfig",
    # 格式解析器
    "BaseFormatParser",
    "SchemParser",
    "SchematicParser",
    "BDXParser",
    "MCStructureParser",
    "BuildingParser",
    "KBDXFormatParser",
    "FuHongFormatParser",
    "GangBanFormatParser",
    "AxiomBPFormatParser",
    "CDumpFormatParser",
    "MCWorldFormatParser",
    "MCFunctionFormatParser",
    "FormatParseError",
    "FormatNotSupportedError",
    "ParseResult",
    # 格式检测
    "get_parser_for_file",
    "detect_format",
    "EXTENSION_PARSER_MAP",
    # 方块重排
    "BlockRearranger",
    # 构建器
    "OmegaBuilder",
    # 导入器
    "OmegaImporter",
    # 路径规划
    "HopPos",
    "ExportedChunkPos",
    "plan_hop_path",
    # 常量
    "AIR_RUNTIME_ID",
    "WORLD_Y_RANGE",
    "SUB_CHUNK_COUNT",
    "SUB_CHUNK_SIZE",
    "CHUNK_SIZE",
]