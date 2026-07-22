"""MIDI 转换器 (MIDI -> Minecraft 命令方块链)。

逆向自 NexusEgo 的 MIDI 转换模块, 适配到 PocketTerm 项目。
提供 MIDI 文件解析、音符映射和命令方块链构建能力。

工作流程:
    1. 解析 MIDI 文件 (midi.Song, midi.Timeline, midi.NoteEvent)
    2. 构建时间线 (BuildTimeline)
    3. 划分速度段 (buildTempoSegments)
    4. 将音符映射到 Minecraft 声音 (noteToPitch/soundForEvent)
    5. 生成命令方块链 (buildChainBlocks)
    6. 导出为 MCWorld 或 mcfunction

主要组件:
    - MidiParser:          MIDI 文件解析器
    - NoteMapper:          音符映射器
    - CommandBlockChain:   命令方块链
"""

from __future__ import annotations

from .midi_parser import (
    Song, Timeline, NoteEvent, TempoEvent, Track, MidiParser,
    MidiParseError, build_timeline, build_tempo_segments,
    parse_midi_file, parse_midi_bytes,
)
from .note_mapper import (
    NoteMapper, NoteSound, InstrumentMapping,
    note_to_pitch, percussion_sound, melodic_sound, sound_for_event,
    DEFAULT_INSTRUMENT_MAPPING,
    DEFAULT_OPTIONS, ConvertOptions,
)
from .command_block_chain import (
    CommandBlock, CommandBlockChain, CommandBlockNBT,
    build_chain_blocks, flatten_commands, build_positions,
    apply_facing, facing_between, bounds_for_blocks,
    count_chunk_span, chunk_coord,
    convert_file_to_mcworld, export_to_mcworld,
    CommandBlockError,
)

__all__ = [
    # midi_parser
    "Song", "Timeline", "NoteEvent", "TempoEvent", "Track",
    "MidiParser", "MidiParseError",
    "build_timeline", "build_tempo_segments",
    "parse_midi_file", "parse_midi_bytes",
    # note_mapper
    "NoteMapper", "NoteSound", "InstrumentMapping",
    "note_to_pitch", "percussion_sound", "melodic_sound", "sound_for_event",
    "DEFAULT_INSTRUMENT_MAPPING", "DEFAULT_OPTIONS", "ConvertOptions",
    # command_block_chain
    "CommandBlock", "CommandBlockChain", "CommandBlockNBT",
    "build_chain_blocks", "flatten_commands", "build_positions",
    "apply_facing", "facing_between", "bounds_for_blocks",
    "count_chunk_span", "chunk_coord",
    "convert_file_to_mcworld", "export_to_mcworld",
    "CommandBlockError",
]
