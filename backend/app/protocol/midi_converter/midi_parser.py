"""midi_parser - MIDI 文件解析器。

逆向自 NexusEgo v1.6.5 的 MIDI 解析模块。
来源 Go 源码路径: NexusEgo_v1.6.5/utils/convert/midi/

逆向证据 (来自 REPORT.txt 5.3 节):
    核心类型:
        - midi.Song       -- MIDI 歌曲
        - midi.Timeline   -- 时间线
        - midi.NoteEvent  -- 音符事件

    核心函数:
        - BuildTimeline       -- 构建时间线
        - buildTempoSegments  -- 构建速度段

逆向证据 (来自 strings_exclusive.txt):
    - song is nil
    - note.guitar
    - invalid vlq (MIDI Variable-Length Quantity 解析)
    - read midi: %w
    - read header length: %w
    - read note velocity: %w
    - read track header: %w
    - read track length: %w
    - read sysex length: %w
    - read channel pressure: %w
    - unknown midi status: 0x%X

MIDI 文件格式:
    1. 文件头 (MThd): format + num_tracks + division
    2. 轨道 (MTrk): delta-time + event 序列
    3. 事件类型:
       - Note On (0x90-0x9F)
       - Note Off (0x80-0x8F)
       - Program Change (0xC0-0xCF)
       - Tempo (0xFF 0x51 0x03)
       - End of Track (0xFF 0x2F 0x00)
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any, Iterator

logger = logging.getLogger("pocketterm.protocol.midi_converter.midi_parser")


# ======================================================================
# 常量
# ======================================================================

#: MIDI 文件头签名
MIDI_HEADER_SIGNATURE: bytes = b"MThd"

#: MIDI 轨道头签名
MIDI_TRACK_SIGNATURE: bytes = b"MTrk"

#: MIDI 默认速度 (BPM)
DEFAULT_BPM: int = 120

#: MIDI 默认 PPQ (Pulses Per Quarter note, 每四分音符的 tick 数)
DEFAULT_PPQ: int = 480

#: 微秒每分钟
MICROSECONDS_PER_MINUTE: int = 60_000_000

#: MIDI 音符范围
MIDI_NOTE_MIN: int = 0
MIDI_NOTE_MAX: int = 127

#: MIDI 通道数
MIDI_CHANNEL_COUNT: int = 16

#: MIDI 程序数 (每通道 128 个)
MIDI_PROGRAM_COUNT: int = 128

#: 鼓组通道 (通用 MIDI 标准)
DRUM_CHANNEL: int = 9

#: MIDI Meta 事件类型
META_EVENT_TYPE: int = 0xFF
META_END_OF_TRACK: int = 0x2F
META_TEMPO: int = 0x51
META_TIME_SIGNATURE: int = 0x58
META_TRACK_NAME: int = 0x03
META_INSTRUMENT_NAME: int = 0x04


# ======================================================================
# 异常
# ======================================================================


class MidiParseError(Exception):
    """MIDI 解析错误的基类。"""

    def __init__(self, message: str, offset: int = -1) -> None:
        self.offset = offset
        msg = f"read midi: {message}"
        if offset >= 0:
            msg += f" at offset {offset}"
        super().__init__(msg)


class InvalidHeaderError(MidiParseError):
    """MIDI 文件头无效。"""


class InvalidVLQError(MidiParseError):
    """MIDI Variable-Length Quantity 无效。

    逆向自 strings: "invalid vlq"
    """


class UnexpectedEndOfTrackError(MidiParseError):
    """意外的轨道结束。"""


class UnknownMidiStatusError(MidiParseError):
    """未知的 MIDI 状态字节。

    逆向自 strings: "unknown midi status: 0x%X"
    """

    def __init__(self, status: int, offset: int = -1) -> None:
        self.status = status
        super().__init__(
            f"unknown midi status: 0x{status:02X}", offset
        )


# ======================================================================
# 数据类
# ======================================================================


@dataclass
class NoteEvent:
    """音符事件 (midi.NoteEvent)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    表示一个音符的按下或释放事件。

    Attributes:
        tick: 事件发生的 tick 位置 (相对于歌曲开始)。
        track: 轨道编号。
        channel: MIDI 通道 (0-15)。
        note: 音符编号 (0-127, 60 = 中央 C)。
        velocity: 力度 (0-127, 0 表示音符释放)。
        is_on: 是否为音符按下 (True=Note On, False=Note Off)。
        duration: 音符持续时间 (tick, 仅在 Note Off 时填充)。
    """

    tick: int = 0
    track: int = 0
    channel: int = 0
    note: int = 60
    velocity: int = 0
    is_on: bool = True
    duration: int = 0

    def __post_init__(self) -> None:
        """校验数据。"""
        if not (MIDI_NOTE_MIN <= self.note <= MIDI_NOTE_MAX):
            raise MidiParseError(f"invalid note number: {self.note}")
        if not (0 <= self.velocity <= 127):
            raise MidiParseError(f"invalid velocity: {self.velocity}")
        if not (0 <= self.channel < MIDI_CHANNEL_COUNT):
            raise MidiParseError(f"invalid channel: {self.channel}")

    @property
    def is_drum(self) -> bool:
        """是否为鼓组音符 (通道 9)。"""
        return self.channel == DRUM_CHANNEL

    @property
    def is_off(self) -> bool:
        """是否为音符释放。"""
        return not self.is_on

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "tick": self.tick,
            "track": self.track,
            "channel": self.channel,
            "note": self.note,
            "velocity": self.velocity,
            "is_on": self.is_on,
            "duration": self.duration,
            "is_drum": self.is_drum,
        }


@dataclass
class TempoEvent:
    """速度事件。

    表示 MIDI 歌曲中速度的变化。

    Attributes:
        tick: 事件发生的 tick 位置。
        tempo: 速度 (微秒 per quarter note, 500000 = 120 BPM)。
        bpm: 速度 (BPM, 每分钟节拍数)。
    """

    tick: int = 0
    tempo: int = 500000  # 120 BPM

    @property
    def bpm(self) -> float:
        """速度 (BPM)。"""
        if self.tempo <= 0:
            return float(DEFAULT_BPM)
        return MICROSECONDS_PER_MINUTE / self.tempo

    @classmethod
    def from_bpm(cls, tick: int, bpm: float) -> "TempoEvent":
        """从 BPM 构建。

        Args:
            tick: tick 位置。
            bpm: BPM 值。

        Returns:
            TempoEvent。
        """
        tempo = int(MICROSECONDS_PER_MINUTE / max(1.0, bpm))
        return cls(tick=tick, tempo=tempo)


@dataclass
class Track:
    """MIDI 轨道 (midi.Track)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    Attributes:
        index: 轨道编号。
        name: 轨道名称。
        instrument: 乐器名称。
        events: 原始事件列表 (delta_time, status, data)。
        note_events: 解析后的音符事件列表。
        program_changes: 程序变更事件列表 (channel, program)。
    """

    index: int = 0
    name: str = ""
    instrument: str = ""
    events: list[tuple[int, int, bytes]] = field(default_factory=list)
    note_events: list[NoteEvent] = field(default_factory=list)
    program_changes: list[tuple[int, int]] = field(default_factory=list)

    @property
    def note_count(self) -> int:
        """音符事件数。"""
        return len(self.note_events)

    @property
    def duration_ticks(self) -> int:
        """轨道持续 tick 数。"""
        if not self.note_events:
            return 0
        return max(e.tick + e.duration for e in self.note_events)


@dataclass
class Timeline:
    """时间线 (midi.Timeline)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。
    逆向自函数: BuildTimeline

    表示整个 MIDI 歌曲的时间线, 包含所有音符事件和速度变化。

    Attributes:
        events: 按时间排序的音符事件列表。
        tempo_events: 速度事件列表。
        total_ticks: 总 tick 数。
        ppq: PPQ (Pulses Per Quarter note)。
    """

    events: list[NoteEvent] = field(default_factory=list)
    tempo_events: list[TempoEvent] = field(default_factory=list)
    total_ticks: int = 0
    ppq: int = DEFAULT_PPQ

    def add_event(self, event: NoteEvent) -> None:
        """添加音符事件。"""
        self.events.append(event)
        self.total_ticks = max(self.total_ticks, event.tick + event.duration)

    def add_tempo(self, tempo: TempoEvent) -> None:
        """添加速度事件。"""
        self.tempo_events.append(tempo)
        self.tempo_events.sort(key=lambda t: t.tick)

    def sort(self) -> None:
        """按 tick 排序所有事件。"""
        self.events.sort(key=lambda e: (e.tick, 0 if e.is_on else 1))

    @property
    def duration_seconds(self) -> float:
        """总时长 (秒)。"""
        if not self.tempo_events:
            return self.total_ticks / DEFAULT_PPQ * (500000 / 1_000_000)
        # 按速度段计算
        total_us = 0.0
        last_tick = 0
        last_tempo = self.tempo_events[0].tempo if self.tempo_events else 500000
        for tempo in self.tempo_events:
            delta_ticks = tempo.tick - last_tick
            total_us += delta_ticks * (last_tempo / self.ppq)
            last_tick = tempo.tick
            last_tempo = tempo.tempo
        # 剩余部分
        delta_ticks = self.total_ticks - last_tick
        total_us += delta_ticks * (last_tempo / self.ppq)
        return total_us / 1_000_000

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "event_count": len(self.events),
            "tempo_count": len(self.tempo_events),
            "total_ticks": self.total_ticks,
            "duration_seconds": round(self.duration_seconds, 3),
            "ppq": self.ppq,
        }


@dataclass
class Song:
    """MIDI 歌曲 (midi.Song)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    逆向自 strings: "song is nil"

    Attributes:
        format: MIDI 文件格式 (0/1/2)。
        tracks: 轨道列表。
        timeline: 时间线。
        ppq: PPQ。
        division: 原始 division 值。
    """

    format: int = 1
    tracks: list[Track] = field(default_factory=list)
    timeline: Timeline = field(default_factory=Timeline)
    ppq: int = DEFAULT_PPQ
    division: int = DEFAULT_PPQ

    def __post_init__(self) -> None:
        """后处理: 同步 PPQ。"""
        self.timeline.ppq = self.ppq

    @property
    def track_count(self) -> int:
        """轨道数。"""
        return len(self.tracks)

    @property
    def is_empty(self) -> bool:
        """是否为空歌曲。"""
        return not self.tracks or not self.timeline.events

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "format": self.format,
            "track_count": self.track_count,
            "ppq": self.ppq,
            "timeline": self.timeline.to_dict(),
            "tracks": [
                {"index": t.index, "name": t.name, "notes": t.note_count}
                for t in self.tracks
            ],
        }


# ======================================================================
# TempoSegment - 速度段
# ======================================================================


@dataclass
class TempoSegment:
    """速度段 (buildTempoSegments 的输出)。

    表示一段时间内速度恒定的片段。

    Attributes:
        start_tick: 起始 tick。
        end_tick: 结束 tick。
        tempo: 速度 (微秒 per quarter note)。
        bpm: 速度 (BPM)。
    """

    start_tick: int = 0
    end_tick: int = 0
    tempo: int = 500000

    @property
    def bpm(self) -> float:
        """速度 (BPM)。"""
        return MICROSECONDS_PER_MINUTE / self.tempo if self.tempo > 0 else 0.0

    @property
    def duration_ticks(self) -> int:
        """持续 tick 数。"""
        return self.end_tick - self.start_tick

    @property
    def duration_seconds(self) -> float:
        """持续秒数。"""
        if self.tempo <= 0:
            return 0.0
        return self.duration_ticks * (self.tempo / 1_000_000) / DEFAULT_PPQ


def build_tempo_segments(
    timeline: Timeline,
    default_tempo: int = 500000,
) -> list[TempoSegment]:
    """构建速度段 (buildTempoSegments)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    将时间线按速度变化划分为多个恒定速度的片段。

    Args:
        timeline: 时间线。
        default_tempo: 默认速度 (微秒 per quarter note)。

    Returns:
        速度段列表。
    """
    if not timeline.tempo_events:
        return [TempoSegment(
            start_tick=0,
            end_tick=timeline.total_ticks,
            tempo=default_tempo,
        )]

    segments: list[TempoSegment] = []
    tempo_events = sorted(timeline.tempo_events, key=lambda t: t.tick)

    # 第一段: 0 -> 第一个 tempo 事件
    if tempo_events[0].tick > 0:
        segments.append(TempoSegment(
            start_tick=0,
            end_tick=tempo_events[0].tick,
            tempo=default_tempo,
        ))

    # 中间段
    for i, tempo in enumerate(tempo_events):
        start = tempo.tick
        if i + 1 < len(tempo_events):
            end = tempo_events[i + 1].tick
        else:
            end = timeline.total_ticks
        segments.append(TempoSegment(
            start_tick=start,
            end_tick=end,
            tempo=tempo.tempo,
        ))

    logger.debug(
        "build_tempo_segments: %d segments for %d ticks",
        len(segments), timeline.total_ticks,
    )
    return segments


def build_timeline(song: Song) -> Timeline:
    """构建时间线 (BuildTimeline)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    从所有轨道的音符事件构建统一的时间线。

    Args:
        song: MIDI 歌曲。

    Returns:
        构建的时间线。

    Raises:
        MidiParseError: 如果 song 为 None (逆向自 strings: "song is nil")。
    """
    if song is None:
        raise MidiParseError("song is nil")

    timeline = Timeline(ppq=song.ppq)

    for track in song.tracks:
        for event in track.note_events:
            timeline.add_event(event)
        # 收集速度事件 (假设存储在 events 中)
        # 实际应在解析时提取

    timeline.sort()

    logger.info(
        "build_timeline: %d events, %d ticks",
        len(timeline.events), timeline.total_ticks,
    )
    return timeline


# ======================================================================
# MidiParser - MIDI 文件解析器
# ======================================================================


class MidiParser:
    """MIDI 文件解析器 (MidiParser)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    解析标准 MIDI 文件 (Format 0/1/2), 输出 Song 对象。

    解析流程:
        1. 读取文件头 (MThd): format, num_tracks, division
        2. 逐个读取轨道 (MTrk)
        3. 解析每个轨道的事件序列 (delta-time + event)
        4. 处理 Note On/Off, Program Change, Tempo 等事件
        5. 构建 Song + Timeline

    用法::

        parser = MidiParser()
        song = parser.parse_file("music.mid")
        timeline = build_timeline(song)
        print(f"Notes: {len(timeline.events)}")
    """

    def __init__(self) -> None:
        self._data: bytes = b""
        self._offset: int = 0

    # ---- 文件解析入口 ----

    def parse_file(self, path: str) -> Song:
        """解析 MIDI 文件。

        逆向自 strings: "read midi: %w"

        Args:
            path: MIDI 文件路径。

        Returns:
            Song 对象。

        Raises:
            MidiParseError: 解析失败。
        """
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            raise MidiParseError(f"failed to read file {path}: {exc}") from exc

        logger.info("parse_file: %s (%d bytes)", path, len(data))
        return self.parse_bytes(data)

    def parse_bytes(self, data: bytes) -> Song:
        """解析 MIDI 字节数据。

        Args:
            data: MIDI 文件字节数据。

        Returns:
            Song 对象。

        Raises:
            MidiParseError: 解析失败。
        """
        self._data = data
        self._offset = 0

        # 1. 解析文件头
        fmt, num_tracks, division = self._parse_header()

        # 2. 解析轨道
        tracks: list[Track] = []
        for i in range(num_tracks):
            track = self._parse_track(i)
            tracks.append(track)

        # 3. 构建 Song
        ppq = division if division > 0 else DEFAULT_PPQ
        song = Song(
            format=fmt,
            tracks=tracks,
            ppq=ppq,
            division=division,
        )

        # 4. 构建时间线
        song.timeline = build_timeline(song)
        song.timeline.ppq = ppq

        # 5. 从轨道事件提取速度事件
        for track in tracks:
            for delta, status, event_data in track.events:
                if status == META_EVENT_TYPE and len(event_data) >= 2:
                    if event_data[0] == META_TEMPO and len(event_data) >= 5:
                        # tempo: 3 字节大端
                        tempo = (event_data[2] << 16) | (event_data[3] << 8) | event_data[4]
                        song.timeline.add_tempo(TempoEvent(tick=0, tempo=tempo))

        logger.info(
            "parse_bytes: format=%d tracks=%d ppq=%d events=%d",
            fmt, num_tracks, ppq, len(song.timeline.events),
        )
        return song

    # ---- 内部: 文件头解析 ----

    def _parse_header(self) -> tuple[int, int, int]:
        """解析 MIDI 文件头 (MThd)。

        逆向自 strings: "read header length: %w"

        Returns:
            (format, num_tracks, division)。
        """
        if self._read_bytes(4) != MIDI_HEADER_SIGNATURE:
            raise InvalidHeaderError(
                f"invalid MIDI header signature (expected {MIDI_HEADER_SIGNATURE!r})",
                self._offset,
            )

        header_length = self._read_uint32()
        if header_length < 6:
            raise InvalidHeaderError(
                f"invalid header length: {header_length}", self._offset
            )

        fmt = self._read_uint16()
        num_tracks = self._read_uint16()
        division = self._read_uint16()

        # 跳过额外头数据
        if header_length > 6:
            self._offset += header_length - 6

        logger.debug(
            "header: format=%d tracks=%d division=%d",
            fmt, num_tracks, division,
        )
        return (fmt, num_tracks, division)

    # ---- 内部: 轨道解析 ----

    def _parse_track(self, track_index: int) -> Track:
        """解析单个 MIDI 轨道 (MTrk)。

        逆向自 strings: "read track header: %w" / "read track length: %w"

        Args:
            track_index: 轨道编号。

        Returns:
            Track 对象。
        """
        if self._read_bytes(4) != MIDI_TRACK_SIGNATURE:
            raise MidiParseError(
                f"invalid track header (expected {MIDI_TRACK_SIGNATURE!r})",
                self._offset,
            )

        track_length = self._read_uint32()
        track_end = self._offset + track_length

        track = Track(index=track_index)

        # 解析事件
        running_status = 0
        absolute_tick = 0
        active_notes: dict[tuple[int, int], NoteEvent] = {}

        while self._offset < track_end:
            # 读取 delta-time (VLQ)
            delta = self._read_vlq()
            absolute_tick += delta

            # 读取状态字节
            status_byte = self._data[self._offset]
            if status_byte < 0x80:
                # Running status: 使用上次的 status
                if running_status == 0:
                    raise MidiParseError(
                        "running status without previous status", self._offset
                    )
                status = running_status
            else:
                status = status_byte
                self._offset += 1
                running_status = status if status < 0xF0 else 0

            event_type = status & 0xF0
            channel = status & 0x0F

            if status == META_EVENT_TYPE:
                # Meta 事件
                meta_type = self._data[self._offset]
                self._offset += 1
                meta_length = self._read_vlq()
                meta_data = self._data[self._offset:self._offset + meta_length]
                self._offset += meta_length

                track.events.append((absolute_tick, status, bytes([meta_type]) + meta_data))

                if meta_type == META_END_OF_TRACK:
                    break
                elif meta_type == META_TRACK_NAME:
                    try:
                        track.name = meta_data.decode("utf-8")
                    except UnicodeDecodeError:
                        track.name = meta_data.decode("latin-1", errors="replace")
                elif meta_type == META_INSTRUMENT_NAME:
                    try:
                        track.instrument = meta_data.decode("utf-8")
                    except UnicodeDecodeError:
                        track.instrument = meta_data.decode("latin-1", errors="replace")
                elif meta_type == META_TEMPO:
                    if len(meta_data) >= 3:
                        tempo = (meta_data[0] << 16) | (meta_data[1] << 8) | meta_data[2]
                        # 速度事件存储在 track.events 中

            elif status in (0xF0, 0xF7):
                # SysEx 事件
                sysex_length = self._read_vlq()
                self._offset += sysex_length
                track.events.append((absolute_tick, status, b""))

            elif event_type == 0x80:
                # Note Off
                note = self._data[self._offset]
                velocity = self._data[self._offset + 1]
                self._offset += 2

                key = (channel, note)
                if key in active_notes:
                    note_event = active_notes.pop(key)
                    note_event.duration = absolute_tick - note_event.tick
                track.events.append((absolute_tick, status, bytes([note, velocity])))

            elif event_type == 0x90:
                # Note On
                note = self._data[self._offset]
                velocity = self._data[self._offset + 1]
                self._offset += 2

                if velocity == 0:
                    # velocity=0 等同于 Note Off
                    key = (channel, note)
                    if key in active_notes:
                        note_event = active_notes.pop(key)
                        note_event.duration = absolute_tick - note_event.tick
                else:
                    note_event = NoteEvent(
                        tick=absolute_tick,
                        track=track_index,
                        channel=channel,
                        note=note,
                        velocity=velocity,
                        is_on=True,
                    )
                    active_notes[(channel, note)] = note_event
                    track.note_events.append(note_event)

                track.events.append((absolute_tick, status, bytes([note, velocity])))

            elif event_type == 0xA0:
                # Polyphonic Aftertouch
                self._offset += 2
                track.events.append((absolute_tick, status, b""))

            elif event_type == 0xB0:
                # Control Change
                self._offset += 2
                track.events.append((absolute_tick, status, b""))

            elif event_type == 0xC0:
                # Program Change
                program = self._data[self._offset]
                self._offset += 1
                track.program_changes.append((channel, program))
                track.events.append((absolute_tick, status, bytes([program])))

            elif event_type == 0xD0:
                # Channel Pressure
                self._offset += 1
                track.events.append((absolute_tick, status, b""))

            elif event_type == 0xE0:
                # Pitch Bend
                self._offset += 2
                track.events.append((absolute_tick, status, b""))

            else:
                raise UnknownMidiStatusError(status, self._offset)

        # 跳到轨道结束
        self._offset = track_end

        logger.debug(
            "track %d: %d events, %d notes, name=%s",
            track_index, len(track.events), track.note_count, track.name,
        )
        return track

    # ---- 内部: 基本读取 ----

    def _read_bytes(self, n: int) -> bytes:
        """读取 n 个字节。"""
        if self._offset + n > len(self._data):
            raise MidiParseError(
                f"unexpected end of data: needed {n} bytes", self._offset
            )
        result = self._data[self._offset:self._offset + n]
        self._offset += n
        return result

    def _read_uint16(self) -> int:
        """读取 2 字节大端无符号整数。"""
        return struct.unpack(">H", self._read_bytes(2))[0]

    def _read_uint32(self) -> int:
        """读取 4 字节大端无符号整数。"""
        return struct.unpack(">I", self._read_bytes(4))[0]

    def _read_vlq(self) -> int:
        """读取 Variable-Length Quantity (VLQ)。

        MIDI VLQ 编码: 每字节 7 位, 最高位为 continuation bit。
        逆向自 strings: "invalid vlq"

        Returns:
            解码后的整数值。
        """
        value = 0
        for i in range(4):  # VLQ 最多 4 字节
            if self._offset >= len(self._data):
                raise InvalidVLQError("unexpected end of VLQ", self._offset)
            byte = self._data[self._offset]
            self._offset += 1
            value = (value << 7) | (byte & 0x7F)
            if not (byte & 0x80):
                return value
        raise InvalidVLQError("VLQ too long (>4 bytes)", self._offset)


# ======================================================================
# 便捷函数
# ======================================================================


def parse_midi_file(path: str) -> Song:
    """解析 MIDI 文件 (便捷函数)。

    Args:
        path: MIDI 文件路径。

    Returns:
        Song 对象。
    """
    parser = MidiParser()
    return parser.parse_file(path)


def parse_midi_bytes(data: bytes) -> Song:
    """解析 MIDI 字节数据 (便捷函数)。

    Args:
        data: MIDI 文件字节数据。

    Returns:
        Song 对象。
    """
    parser = MidiParser()
    return parser.parse_bytes(data)


# ======================================================================
# __all__
# ======================================================================

__all__ = [
    # 常量
    "MIDI_HEADER_SIGNATURE", "MIDI_TRACK_SIGNATURE",
    "DEFAULT_BPM", "DEFAULT_PPQ", "MICROSECONDS_PER_MINUTE",
    "MIDI_NOTE_MIN", "MIDI_NOTE_MAX",
    "MIDI_CHANNEL_COUNT", "MIDI_PROGRAM_COUNT",
    "DRUM_CHANNEL",
    "META_EVENT_TYPE", "META_END_OF_TRACK", "META_TEMPO",
    "META_TIME_SIGNATURE", "META_TRACK_NAME", "META_INSTRUMENT_NAME",
    # 异常
    "MidiParseError", "InvalidHeaderError", "InvalidVLQError",
    "UnexpectedEndOfTrackError", "UnknownMidiStatusError",
    # 数据类
    "NoteEvent", "TempoEvent", "Track", "Timeline", "Song", "TempoSegment",
    # 主类
    "MidiParser",
    # 函数
    "build_timeline", "build_tempo_segments",
    "parse_midi_file", "parse_midi_bytes",
]
