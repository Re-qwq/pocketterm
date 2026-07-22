"""note_mapper - 音符映射器 (音符 -> Minecraft 音高)。

逆向自 NexusEgo v1.6.5 的 MIDI 音符映射模块。
来源 Go 源码路径: NexusEgo_v1.6.5/utils/convert/midi/

逆向证据 (来自 REPORT.txt 5.3 节):
    核心函数:
        - soundForEvent       -- 事件转声音
        - percussionSound     -- 打击乐声音
        - melodicSound        -- 旋律声音
        - noteToPitch         -- 音符转音高
        - DefaultOptions      -- 默认选项

逆向证据 (来自 strings_exclusive.txt):
    - note.guitar
    - note.didgeridoo
    - note.cow_bell
    - note.basedrum
    - note.harp
    - note.bass
    - note.bell
    - note.flute
    - note.hat
    - note.snare
    - note.xylophone
    - note.iron_xylophone
    - note.pling
    - note.bit
    - note.banjo
    - note.chime
    - note.cow_bell

Minecraft 音符盒音域:
    - 每个音符盒有 25 个音高 (0-24), 对应 2 个八度
    - 音高 0 = F#3 (1.185 Hz 实际)
    - 音高 24 = F#5 (2936.6 Hz 实际)
    - 每 +1 升高半音

Minecraft 音符盒乐器 (由下方方块决定):
    - 音符盒本体在方块上方时, 乐器由下方方块决定
    - 下方方块类型 -> 乐器声音:
        木头/木板     -> note.harp (竖琴)
        石头           -> note.basedrum (底鼓)
        玻璃           -> note.hat (击鼓)
        沙子/砂砾      -> note.snare (军鼓)
        金块           -> note.bell (钟)
        粘土           -> note.flute (长笛)
        打包冰         -> note.chime (风铃)
        羊毛           -> note.guitar (吉他)
        骨块           -> note.xylophone (木琴)
        铁块           -> note.iron_xylophone (铁琴)
        灵魂沙         -> note.cow_bell (牛铃)
        南瓜           -> note.didgeridoo (迪吉里杜管)
        绿宝石块       -> note.bit (比特)
        干草块         -> note.banjo (班卓琴)
        荧石           -> note.pling (电子琴)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

# 导入同模块的类型
try:
    from .midi_parser import NoteEvent, DEFAULT_BPM, DRUM_CHANNEL, MIDI_NOTE_MIN, MIDI_NOTE_MAX
except ImportError:
    from midi_parser import NoteEvent, DEFAULT_BPM, DRUM_CHANNEL, MIDI_NOTE_MIN, MIDI_NOTE_MAX  # type: ignore

logger = logging.getLogger("pocketterm.protocol.midi_converter.note_mapper")


# ======================================================================
# 常量
# ======================================================================

#: Minecraft 音符盒最低音高 (0 = F#3)
MC_NOTE_MIN: int = 0

#: Minecraft 音符盒最高音高 (24 = F#5)
MC_NOTE_MAX: int = 24

#: Minecraft 音符盒音域中心 (12 = F#4)
MC_NOTE_CENTER: int = 12

#: MIDI 中央 C (C4) 的编号
MIDI_MIDDLE_C: int = 60

#: Minecraft 音符盒基础音 (F#3, MIDI 编号 54)
MC_BASE_NOTE_MIDI: int = 54

#: 默认乐器 (harp/竖琴)
DEFAULT_INSTRUMENT: str = "harp"

#: 打击乐默认乐器 (basedrum/底鼓)
DEFAULT_PERCUSSION: str = "basedrum"


# ======================================================================
# 乐器映射表 (下方方块 -> 音符盒乐器)
# ======================================================================


@dataclass(frozen=True)
class InstrumentMapping:
    """乐器映射 (InstrumentMapping)。

    逆向自 NexusEgo_v1.6.5 的音符盒乐器配置。

    通用 MIDI (GM) 程序号到 Minecraft 音符盒乐器的映射。

    Attributes:
        gm_program: 通用 MIDI 程序号 (0-127)。
        mc_instrument: Minecraft 音符盒乐器名 (如 "harp")。
        mc_block: 下方方块名 (如 "minecraft:planks")。
        sound_prefix: 声音前缀 (如 "note.harp")。
    """

    gm_program: int = 0
    mc_instrument: str = "harp"
    mc_block: str = "minecraft:planks"
    sound_prefix: str = "note.harp"


#: 通用 MIDI 程序号 -> Minecraft 乐器映射表
#: 逆向自 NexusEgo_v1.6.5 的乐器映射逻辑
DEFAULT_INSTRUMENT_MAPPING: dict[int, InstrumentMapping] = {
    # Piano family (0-7)
    0: InstrumentMapping(0, "harp", "minecraft:planks", "note.harp"),          # Acoustic Grand Piano
    1: InstrumentMapping(1, "harp", "minecraft:planks", "note.harp"),          # Bright Acoustic Piano
    2: InstrumentMapping(2, "harp", "minecraft:planks", "note.harp"),          # Electric Grand Piano
    3: InstrumentMapping(3, "harp", "minecraft:planks", "note.harp"),          # Honky-tonk Piano
    4: InstrumentMapping(4, "pling", "minecraft:glowstone", "note.pling"),     # Electric Piano 1
    5: InstrumentMapping(5, "pling", "minecraft:glowstone", "note.pling"),     # Electric Piano 2
    6: InstrumentMapping(6, "harp", "minecraft:planks", "note.harp"),          # Harpsichord
    7: InstrumentMapping(7, "harp", "minecraft:planks", "note.harp"),          # Clavinet

    # Chromatic Percussion (8-15)
    8: InstrumentMapping(8, "bell", "minecraft:gold_block", "note.bell"),      # Celesta
    9: InstrumentMapping(9, "bell", "minecraft:gold_block", "note.bell"),      # Glockenspiel
    10: InstrumentMapping(10, "iron_xylophone", "minecraft:iron_block", "note.iron_xylophone"),  # Music Box
    11: InstrumentMapping(11, "xylophone", "minecraft:bone_block", "note.xylophone"),  # Vibraphone
    12: InstrumentMapping(12, "xylophone", "minecraft:bone_block", "note.xylophone"),  # Marimba
    13: InstrumentMapping(13, "xylophone", "minecraft:bone_block", "note.xylophone"),  # Xylophone
    14: InstrumentMapping(14, "iron_xylophone", "minecraft:iron_block", "note.iron_xylophone"),  # Tubular Bells
    15: InstrumentMapping(15, "harp", "minecraft:planks", "note.harp"),        # Dulcimer

    # Organ (16-23)
    16: InstrumentMapping(16, "harp", "minecraft:planks", "note.harp"),        # Drawbar Organ
    17: InstrumentMapping(17, "harp", "minecraft:planks", "note.harp"),        # Percussive Organ
    18: InstrumentMapping(18, "harp", "minecraft:planks", "note.harp"),        # Rock Organ
    19: InstrumentMapping(19, "harp", "minecraft:planks", "note.harp"),        # Church Organ
    20: InstrumentMapping(20, "harp", "minecraft:planks", "note.harp"),        # Reed Organ
    21: InstrumentMapping(21, "harp", "minecraft:planks", "note.harp"),        # Accordion
    22: InstrumentMapping(22, "harp", "minecraft:planks", "note.harp"),        # Harmonica
    23: InstrumentMapping(23, "harp", "minecraft:planks", "note.harp"),        # Tango Accordion

    # Guitar (24-31)
    24: InstrumentMapping(24, "guitar", "minecraft:wool", "note.guitar"),      # Acoustic Guitar (nylon)
    25: InstrumentMapping(25, "guitar", "minecraft:wool", "note.guitar"),      # Acoustic Guitar (steel)
    26: InstrumentMapping(26, "guitar", "minecraft:wool", "note.guitar"),      # Electric Guitar (jazz)
    27: InstrumentMapping(27, "guitar", "minecraft:wool", "note.guitar"),      # Electric Guitar (clean)
    28: InstrumentMapping(28, "guitar", "minecraft:wool", "note.guitar"),      # Electric Guitar (muted)
    29: InstrumentMapping(29, "guitar", "minecraft:wool", "note.guitar"),      # Overdriven Guitar
    30: InstrumentMapping(30, "guitar", "minecraft:wool", "note.guitar"),      # Distortion Guitar
    31: InstrumentMapping(31, "guitar", "minecraft:wool", "note.guitar"),      # Guitar Harmonics

    # Bass (32-39)
    32: InstrumentMapping(32, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Acoustic Bass
    33: InstrumentMapping(33, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Electric Bass (finger)
    34: InstrumentMapping(34, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Electric Bass (pick)
    35: InstrumentMapping(35, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Fretless Bass
    36: InstrumentMapping(36, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Slap Bass 1
    37: InstrumentMapping(37, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Slap Bass 2
    38: InstrumentMapping(38, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Synth Bass 1
    39: InstrumentMapping(39, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Synth Bass 2

    # Strings (40-47)
    40: InstrumentMapping(40, "harp", "minecraft:planks", "note.harp"),        # Violin
    41: InstrumentMapping(41, "harp", "minecraft:planks", "note.harp"),        # Viola
    42: InstrumentMapping(42, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Cello
    43: InstrumentMapping(43, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Contrabass
    44: InstrumentMapping(44, "harp", "minecraft:planks", "note.harp"),        # Tremolo Strings
    45: InstrumentMapping(45, "harp", "minecraft:planks", "note.harp"),        # Pizzicato Strings
    46: InstrumentMapping(46, "harp", "minecraft:planks", "note.harp"),        # Orchestral Harp
    47: InstrumentMapping(47, "harp", "minecraft:planks", "note.harp"),        # Timpani

    # Ensemble (48-55)
    48: InstrumentMapping(48, "harp", "minecraft:planks", "note.harp"),        # String Ensemble 1
    49: InstrumentMapping(49, "harp", "minecraft:planks", "note.harp"),        # String Ensemble 2
    50: InstrumentMapping(50, "harp", "minecraft:planks", "note.harp"),        # Synth Strings 1
    51: InstrumentMapping(51, "harp", "minecraft:planks", "note.harp"),        # Synth Strings 2
    52: InstrumentMapping(52, "harp", "minecraft:planks", "note.harp"),        # Choir Aahs
    53: InstrumentMapping(53, "harp", "minecraft:planks", "note.harp"),        # Voice Oohs
    54: InstrumentMapping(54, "harp", "minecraft:planks", "note.harp"),        # Synth Voice
    55: InstrumentMapping(55, "harp", "minecraft:planks", "note.harp"),        # Orchestra Hit

    # Brass (56-63)
    56: InstrumentMapping(56, "harp", "minecraft:planks", "note.harp"),        # Trumpet
    57: InstrumentMapping(57, "harp", "minecraft:planks", "note.harp"),        # Trombone
    58: InstrumentMapping(58, "bass", "minecraft:stripped_oak_log", "note.bass"),  # Tuba
    59: InstrumentMapping(59, "harp", "minecraft:planks", "note.harp"),        # Muted Trumpet
    60: InstrumentMapping(60, "harp", "minecraft:planks", "note.harp"),        # French Horn
    61: InstrumentMapping(61, "harp", "minecraft:planks", "note.harp"),        # Brass Section
    62: InstrumentMapping(62, "harp", "minecraft:planks", "note.harp"),        # Synth Brass 1
    63: InstrumentMapping(63, "harp", "minecraft:planks", "note.harp"),        # Synth Brass 2

    # Reed (64-71)
    64: InstrumentMapping(64, "flute", "minecraft:clay", "note.flute"),        # Soprano Sax
    65: InstrumentMapping(65, "flute", "minecraft:clay", "note.flute"),        # Alto Sax
    66: InstrumentMapping(66, "flute", "minecraft:clay", "note.flute"),        # Tenor Sax
    67: InstrumentMapping(67, "flute", "minecraft:clay", "note.flute"),        # Baritone Sax
    68: InstrumentMapping(68, "flute", "minecraft:clay", "note.flute"),        # Oboe
    69: InstrumentMapping(69, "flute", "minecraft:clay", "note.flute"),        # English Horn
    70: InstrumentMapping(70, "flute", "minecraft:clay", "note.flute"),        # Bassoon
    71: InstrumentMapping(71, "flute", "minecraft:clay", "note.flute"),        # Clarinet

    # Pipe (72-79)
    72: InstrumentMapping(72, "flute", "minecraft:clay", "note.flute"),        # Piccolo
    73: InstrumentMapping(73, "flute", "minecraft:clay", "note.flute"),        # Flute
    74: InstrumentMapping(74, "flute", "minecraft:clay", "note.flute"),        # Recorder
    75: InstrumentMapping(75, "flute", "minecraft:clay", "note.flute"),        # Pan Flute
    76: InstrumentMapping(76, "flute", "minecraft:clay", "note.flute"),        # Blown Bottle
    77: InstrumentMapping(77, "flute", "minecraft:clay", "note.flute"),        # Shakuhachi
    78: InstrumentMapping(78, "flute", "minecraft:clay", "note.flute"),        # Whistle
    79: InstrumentMapping(79, "flute", "minecraft:clay", "note.flute"),        # Ocarina

    # Synth Lead (80-87)
    80: InstrumentMapping(80, "pling", "minecraft:glowstone", "note.pling"),   # Lead 1 (square)
    81: InstrumentMapping(81, "pling", "minecraft:glowstone", "note.pling"),   # Lead 2 (sawtooth)
    82: InstrumentMapping(82, "pling", "minecraft:glowstone", "note.pling"),   # Lead 3 (calliope)
    83: InstrumentMapping(83, "pling", "minecraft:glowstone", "note.pling"),   # Lead 4 (chiff)
    84: InstrumentMapping(84, "pling", "minecraft:glowstone", "note.pling"),   # Lead 5 (charang)
    85: InstrumentMapping(85, "pling", "minecraft:glowstone", "note.pling"),   # Lead 6 (voice)
    86: InstrumentMapping(86, "pling", "minecraft:glowstone", "note.pling"),   # Lead 7 (fifths)
    87: InstrumentMapping(87, "pling", "minecraft:glowstone", "note.pling"),   # Lead 8 (bass+lead)

    # Synth Pad (88-95)
    88: InstrumentMapping(88, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 1 (new age)
    89: InstrumentMapping(89, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 2 (warm)
    90: InstrumentMapping(90, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 3 (polysynth)
    91: InstrumentMapping(91, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 4 (choir)
    92: InstrumentMapping(92, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 5 (bowed)
    93: InstrumentMapping(93, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 6 (metallic)
    94: InstrumentMapping(94, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 7 (halo)
    95: InstrumentMapping(95, "bit", "minecraft:emerald_block", "note.bit"),   # Pad 8 (sweep)

    # Synth Effects (96-103)
    96: InstrumentMapping(96, "bit", "minecraft:emerald_block", "note.bit"),
    97: InstrumentMapping(97, "bit", "minecraft:emerald_block", "note.bit"),
    98: InstrumentMapping(98, "bit", "minecraft:emerald_block", "note.bit"),
    99: InstrumentMapping(99, "bit", "minecraft:emerald_block", "note.bit"),
    100: InstrumentMapping(100, "bit", "minecraft:emerald_block", "note.bit"),
    101: InstrumentMapping(101, "bit", "minecraft:emerald_block", "note.bit"),
    102: InstrumentMapping(102, "bit", "minecraft:emerald_block", "note.bit"),
    103: InstrumentMapping(103, "bit", "minecraft:emerald_block", "note.bit"),

    # Ethnic (104-111)
    104: InstrumentMapping(104, "banjo", "minecraft:hay_block", "note.banjo"),  # Sitar
    105: InstrumentMapping(105, "banjo", "minecraft:hay_block", "note.banjo"),  # Banjo
    106: InstrumentMapping(106, "banjo", "minecraft:hay_block", "note.banjo"),  # Shamisen
    107: InstrumentMapping(107, "banjo", "minecraft:hay_block", "note.banjo"),  # Koto
    108: InstrumentMapping(108, "banjo", "minecraft:hay_block", "note.banjo"),  # Kalimba
    109: InstrumentMapping(109, "banjo", "minecraft:hay_block", "note.banjo"),  # Bag pipe
    110: InstrumentMapping(110, "didgeridoo", "minecraft:pumpkin", "note.didgeridoo"),  # Fiddle
    111: InstrumentMapping(111, "didgeridoo", "minecraft:pumpkin", "note.didgeridoo"),  # Shanai

    # Percussive (112-119)
    112: InstrumentMapping(112, "bell", "minecraft:gold_block", "note.bell"),   # Tinkle Bell
    113: InstrumentMapping(113, "guitar", "minecraft:wool", "note.guitar"),
    114: InstrumentMapping(114, "harp", "minecraft:planks", "note.harp"),
    115: InstrumentMapping(115, "cow_bell", "minecraft:soul_sand", "note.cow_bell"),  # Woodblock
    116: InstrumentMapping(116, "cow_bell", "minecraft:soul_sand", "note.cow_bell"),
    117: InstrumentMapping(117, "cow_bell", "minecraft:soul_sand", "note.cow_bell"),
    118: InstrumentMapping(118, "cow_bell", "minecraft:soul_sand", "note.cow_bell"),
    119: InstrumentMapping(119, "cow_bell", "minecraft:soul_sand", "note.cow_bell"),

    # Sound effects (120-127)
    120: InstrumentMapping(120, "bit", "minecraft:emerald_block", "note.bit"),
    121: InstrumentMapping(121, "bit", "minecraft:emerald_block", "note.bit"),
    122: InstrumentMapping(122, "bit", "minecraft:emerald_block", "note.bit"),
    123: InstrumentMapping(123, "bit", "minecraft:emerald_block", "note.bit"),
    124: InstrumentMapping(124, "bit", "minecraft:emerald_block", "note.bit"),
    125: InstrumentMapping(125, "bit", "minecraft:emerald_block", "note.bit"),
    126: InstrumentMapping(126, "bit", "minecraft:emerald_block", "note.bit"),
    127: InstrumentMapping(127, "bit", "minecraft:emerald_block", "note.bit"),
}


#: 打击乐映射 (MIDI 鼓组音符 -> Minecraft 音符盒打击乐)
#: 通用 MIDI 鼓组映射 (Channel 9)
PERCUSSION_MAPPING: dict[int, str] = {
    35: "note.basedrum",  # Acoustic Bass Drum
    36: "note.basedrum",  # Bass Drum 1
    37: "note.snare",     # Side Stick
    38: "note.snare",     # Acoustic Snare
    39: "note.hat",       # Hand Clap
    40: "note.snare",     # Electric Snare
    41: "note.hat",       # Low Floor Tom
    42: "note.hat",       # Closed Hi-Hat
    43: "note.hat",       # High Floor Tom
    44: "note.hat",       # Pedal Hi-Hat
    45: "note.hat",       # Low Tom
    46: "note.hat",       # Open Hi-Hat
    47: "note.hat",       # Low-Mid Tom
    48: "note.hat",       # Hi-Mid Tom
    49: "note.basedrum",  # Crash Cymbal 1
    50: "note.hat",       # High Tom
    51: "note.basedrum",  # Ride Cymbal 1
    52: "note.basedrum",  # Chinese Cymbal
    53: "note.bell",      # Ride Bell
    54: "note.bell",      # Tambourine
    55: "note.basedrum",  # Splash Cymbal
    56: "note.bell",      # Cow Bell
    57: "note.basedrum",  # Crash Cymbal 2
    58: "note.hat",       # Vibraslap
    59: "note.basedrum",  # Ride Cymbal 2
    60: "note.hat",       # Hi Bongo
    61: "note.hat",       # Low Bongo
    62: "note.hat",       # Mute Hi Conga
    63: "note.hat",       # Open Hi Conga
    64: "note.hat",       # Low Conga
    65: "note.hat",       # High Timbale
    66: "note.hat",       # Low Timbale
    67: "note.hat",       # High Agogo
    68: "note.hat",       # Low Agogo
    69: "note.bell",      # Cabasa
    70: "note.bell",      # Maracas
    71: "note.hat",       # Short Whistle
    72: "note.hat",       # Long Whistle
    73: "note.hat",       # Short Guiro
    74: "note.hat",       # Long Guiro
    75: "note.hat",       # Claves
    76: "note.hat",       # Hi Wood Block
    77: "note.hat",       # Low Wood Block
    78: "note.hat",       # Mute Cuica
    79: "note.hat",       # Open Cuica
    80: "note.hat",       # Mute Triangle
    81: "note.hat",       # Open Triangle
}


# ======================================================================
# 异常
# ======================================================================


class NoteMapError(Exception):
    """音符映射错误的基类。"""


class OutOfRangeError(NoteMapError):
    """音符超出 Minecraft 音符盒音域。"""


# ======================================================================
# 数据类 - NoteSound
# ======================================================================


@dataclass
class NoteSound:
    """音符声音 (NoteSound)。

    表示一个 MIDI 音符事件映射到 Minecraft 的声音。

    Attributes:
        sound: 声音名称 (如 "note.harp")。
        pitch: 音高 (0-24)。
        instrument: 乐器名称 (如 "harp")。
        block: 下方方块名 (如 "minecraft:planks")。
        volume: 音量 (0.0-1.0)。
        note_event: 原始 MIDI 音符事件。
    """

    sound: str = "note.harp"
    pitch: int = 12
    instrument: str = "harp"
    block: str = "minecraft:planks"
    volume: float = 1.0
    note_event: NoteEvent | None = None

    def __post_init__(self) -> None:
        """校验音高。"""
        if not (MC_NOTE_MIN <= self.pitch <= MC_NOTE_MAX):
            raise OutOfRangeError(
                f"pitch must be {MC_NOTE_MIN}-{MC_NOTE_MAX}, got {self.pitch}"
            )

    @property
    def frequency_hz(self) -> float:
        """实际频率 (Hz)。

        Minecraft 音符盒的频率: 2^((pitch - 12) / 12) * 880
        (中央音高 12 = 880 Hz ≈ A5)
        """
        return 2 ** ((self.pitch - 12) / 12.0) * 880.0

    def to_play_command(self, position: tuple[int, int, int] | None = None) -> str:
        """生成 playsound 命令。

        Args:
            position: 播放位置 (x, y, z)。None 表示 @a。

        Returns:
            playsound 命令字符串。
        """
        target = " @a" if position is None else f" {position[0]} {position[1]} {position[2]}"
        return f"playsound {self.sound} master{target} ~ ~ ~ {self.volume} {self.pitch / 24.0:.4f}"

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "sound": self.sound,
            "pitch": self.pitch,
            "instrument": self.instrument,
            "block": self.block,
            "volume": self.volume,
            "frequency_hz": round(self.frequency_hz, 2),
        }


# ======================================================================
# 数据类 - ConvertOptions
# ======================================================================


@dataclass
class ConvertOptions:
    """转换选项 (ConvertOptions)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/ 的 DefaultOptions。

    Attributes:
        default_instrument: 默认乐器 (当无法识别 GM 程序时使用)。
        transpose: 音高移调 (半音数, 可正可负)。
        velocity_to_volume: 力度到音量转换比例 (0.0-1.0)。
        ignore_drums: 是否忽略鼓组。
        min_velocity: 最小力度 (低于此值忽略)。
        max_concurrent_notes: 最大并发音符数 (0=不限)。
        tick_to_redstone: tick 到红石延迟的转换比例。
    """

    default_instrument: str = "harp"
    transpose: int = 0
    velocity_to_volume: float = 1.0 / 127.0
    ignore_drums: bool = False
    min_velocity: int = 1
    max_concurrent_notes: int = 0
    tick_to_redstone: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return {
            "default_instrument": self.default_instrument,
            "transpose": self.transpose,
            "velocity_to_volume": self.velocity_to_volume,
            "ignore_drums": self.ignore_drums,
            "min_velocity": self.min_velocity,
            "max_concurrent_notes": self.max_concurrent_notes,
            "tick_to_redstone": self.tick_to_redstone,
        }


#: 默认转换选项 (逆向自 DefaultOptions)
DEFAULT_OPTIONS: ConvertOptions = ConvertOptions()


# ======================================================================
# NoteMapper - 音符映射器
# ======================================================================


class NoteMapper:
    """音符映射器 (NoteMapper)。

    逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

    将 MIDI 音符事件映射到 Minecraft 音符盒声音:
        - noteToPitch:       MIDI 音符 -> MC 音高 (0-24)
        - melodicSound:      旋律乐器 -> MC 乐器
        - percussionSound:   打击乐 -> MC 打击乐
        - soundForEvent:     音符事件 -> NoteSound

    用法::

        mapper = NoteMapper()
        for event in timeline.events:
            sound = mapper.sound_for_event(event, program=0)
            print(sound.sound, sound.pitch)
    """

    def __init__(
        self,
        options: ConvertOptions | None = None,
        instrument_mapping: dict[int, InstrumentMapping] | None = None,
    ) -> None:
        self._options: ConvertOptions = options or DEFAULT_OPTIONS
        self._instrument_mapping: dict[int, InstrumentMapping] = (
            instrument_mapping or DEFAULT_INSTRUMENT_MAPPING
        )
        logger.debug(
            "NoteMapper init: default_instrument=%s transpose=%d",
            self._options.default_instrument, self._options.transpose,
        )

    @property
    def options(self) -> ConvertOptions:
        """获取选项。"""
        return self._options

    def set_options(self, options: ConvertOptions) -> None:
        """设置选项。"""
        self._options = options

    # ---- 核心映射函数 ----

    def note_to_pitch(self, midi_note: int) -> int:
        """音符转音高 (noteToPitch)。

        逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

        将 MIDI 音符编号 (0-127) 映射到 Minecraft 音符盒音高 (0-24)。
        Minecraft 音符盒音高 0 = F#3 (MIDI 54), 每 +1 升半音。
        MIDI 54 -> MC 0, MIDI 78 -> MC 24。

        Args:
            midi_note: MIDI 音符编号 (0-127)。

        Returns:
            Minecraft 音高 (0-24)。

        Raises:
            OutOfRangeError: 音符超出可映射范围。
        """
        if not (MIDI_NOTE_MIN <= midi_note <= MIDI_NOTE_MAX):
            raise OutOfRangeError(
                f"MIDI note must be {MIDI_NOTE_MIN}-{MIDI_NOTE_MAX}, got {midi_note}"
            )

        # 应用移调
        adjusted = midi_note + self._options.transpose

        # MIDI 54 (F#3) -> MC 0
        pitch = adjusted - MC_BASE_NOTE_MIDI

        # 限制到 0-24 范围
        if pitch < MC_NOTE_MIN:
            # 移调后仍低于 0, 尝试升高八度
            while pitch < MC_NOTE_MIN and pitch + 12 <= MC_NOTE_MAX:
                pitch += 12
            if pitch < MC_NOTE_MIN:
                pitch = MC_NOTE_MIN
                logger.debug(
                    "note_to_pitch: clamped %d (midi %d) to min %d",
                    pitch, midi_note, MC_NOTE_MIN,
                )
        elif pitch > MC_NOTE_MAX:
            # 移调后仍高于 24, 尝试降低八度
            while pitch > MC_NOTE_MAX and pitch - 12 >= MC_NOTE_MIN:
                pitch -= 12
            if pitch > MC_NOTE_MAX:
                pitch = MC_NOTE_MAX
                logger.debug(
                    "note_to_pitch: clamped %d (midi %d) to max %d",
                    pitch, midi_note, MC_NOTE_MAX,
                )

        return pitch

    def melodic_sound(
        self,
        midi_note: int,
        program: int = 0,
    ) -> NoteSound:
        """旋律声音 (melodicSound)。

        逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

        将旋律乐器音符映射到 Minecraft 音符盒声音。

        Args:
            midi_note: MIDI 音符编号。
            program: GM 程序号 (0-127)。

        Returns:
            NoteSound 对象。
        """
        # 获取乐器映射
        mapping = self._instrument_mapping.get(
            program,
            InstrumentMapping(
                gm_program=program,
                mc_instrument=self._options.default_instrument,
                mc_block="minecraft:planks",
                sound_prefix=f"note.{self._options.default_instrument}",
            ),
        )

        pitch = self.note_to_pitch(midi_note)

        logger.debug(
            "melodic_sound: midi=%d program=%d -> instrument=%s pitch=%d",
            midi_note, program, mapping.mc_instrument, pitch,
        )

        return NoteSound(
            sound=mapping.sound_prefix,
            pitch=pitch,
            instrument=mapping.mc_instrument,
            block=mapping.mc_block,
            volume=1.0,
        )

    def percussion_sound(self, midi_note: int) -> NoteSound:
        """打击乐声音 (percussionSound)。

        逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

        将打击乐音符 (通道 9) 映射到 Minecraft 音符盒打击乐。

        Args:
            midi_note: MIDI 音符编号 (打击乐音符)。

        Returns:
            NoteSound 对象。
        """
        # 查找打击乐映射
        sound_name = PERCUSSION_MAPPING.get(midi_note, f"note.{DEFAULT_PERCUSSION}")

        # 打击乐音高通常固定为 12 (中央)
        pitch = MC_NOTE_CENTER

        # 根据声音名确定方块
        if "basedrum" in sound_name:
            block = "minecraft:stone"
            instrument = "basedrum"
        elif "snare" in sound_name:
            block = "minecraft:sand"
            instrument = "snare"
        elif "hat" in sound_name:
            block = "minecraft:glass"
            instrument = "hat"
        elif "bell" in sound_name:
            block = "minecraft:gold_block"
            instrument = "bell"
        else:
            block = "minecraft:stone"
            instrument = DEFAULT_PERCUSSION

        logger.debug(
            "percussion_sound: midi=%d -> sound=%s pitch=%d",
            midi_note, sound_name, pitch,
        )

        return NoteSound(
            sound=sound_name,
            pitch=pitch,
            instrument=instrument,
            block=block,
            volume=1.0,
        )

    def sound_for_event(
        self,
        event: NoteEvent,
        program: int = 0,
    ) -> NoteSound | None:
        """事件转声音 (soundForEvent)。

        逆向自 NexusEgo_v1.6.5/utils/convert/midi/。

        将一个 NoteEvent 转换为 NoteSound。
        如果是 Note Off 或力度过低, 返回 None。

        Args:
            event: MIDI 音符事件。
            program: GM 程序号。

        Returns:
            NoteSound 对象, 或 None (如果不需要播放)。
        """
        if event.is_off:
            return None

        if event.velocity < self._options.min_velocity:
            return None

        if event.is_drum:
            if self._options.ignore_drums:
                return None
            sound = self.percussion_sound(event.note)
        else:
            sound = self.melodic_sound(event.note, program)

        # 应用力度到音量
        sound.volume = event.velocity * self._options.velocity_to_volume
        sound.note_event = event

        logger.debug(
            "sound_for_event: tick=%d note=%d -> %s pitch=%d vol=%.2f",
            event.tick, event.note, sound.sound, sound.pitch, sound.volume,
        )
        return sound

    # ---- 批量映射 ----

    def map_events(
        self,
        events: list[NoteEvent],
        track_programs: dict[int, int] | None = None,
    ) -> list[tuple[NoteEvent, NoteSound]]:
        """批量映射音符事件。

        Args:
            events: 音符事件列表。
            track_programs: 轨道编号 -> GM 程序号 的映射。

        Returns:
            (NoteEvent, NoteSound) 元组列表。
        """
        track_programs = track_programs or {}
        result: list[tuple[NoteEvent, NoteSound]] = []

        for event in events:
            program = track_programs.get(event.track, 0)
            sound = self.sound_for_event(event, program)
            if sound is not None:
                result.append((event, sound))

        logger.info(
            "map_events: %d events -> %d sounds",
            len(events), len(result),
        )
        return result


# ======================================================================
# 便捷函数
# ======================================================================


def note_to_pitch(midi_note: int, transpose: int = 0) -> int:
    """音符转音高 (便捷函数)。

    Args:
        midi_note: MIDI 音符编号 (0-127)。
        transpose: 移调 (半音数)。

    Returns:
        Minecraft 音高 (0-24)。
    """
    mapper = NoteMapper(options=ConvertOptions(transpose=transpose))
    return mapper.note_to_pitch(midi_note)


def percussion_sound(midi_note: int) -> NoteSound:
    """打击乐声音 (便捷函数)。

    Args:
        midi_note: MIDI 打击乐音符编号。

    Returns:
        NoteSound。
    """
    mapper = NoteMapper()
    return mapper.percussion_sound(midi_note)


def melodic_sound(midi_note: int, program: int = 0) -> NoteSound:
    """旋律声音 (便捷函数)。

    Args:
        midi_note: MIDI 音符编号。
        program: GM 程序号。

    Returns:
        NoteSound。
    """
    mapper = NoteMapper()
    return mapper.melodic_sound(midi_note, program)


def sound_for_event(event: NoteEvent, program: int = 0) -> NoteSound | None:
    """事件转声音 (便捷函数)。

    Args:
        event: MIDI 音符事件。
        program: GM 程序号。

    Returns:
        NoteSound 或 None。
    """
    mapper = NoteMapper()
    return mapper.sound_for_event(event, program)


# ======================================================================
# __all__
# ======================================================================

__all__ = [
    # 常量
    "MC_NOTE_MIN", "MC_NOTE_MAX", "MC_NOTE_CENTER",
    "MIDI_MIDDLE_C", "MC_BASE_NOTE_MIDI",
    "DEFAULT_INSTRUMENT", "DEFAULT_PERCUSSION",
    "DEFAULT_INSTRUMENT_MAPPING", "PERCUSSION_MAPPING",
    # 异常
    "NoteMapError", "OutOfRangeError",
    # 数据类
    "InstrumentMapping", "NoteSound", "ConvertOptions",
    "DEFAULT_OPTIONS",
    # 主类
    "NoteMapper",
    # 便捷函数
    "note_to_pitch", "percussion_sound", "melodic_sound", "sound_for_event",
]
