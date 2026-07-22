"""humanize_command - 命令消息人类化 (humanizeCommandMessage)。

逆向自 NovaBuilder 的命令消息人类化逻辑, 来源:
    - /workspace/novuilder_reverse/strings_html.txt
    - /workspace/novuilder_reverse/anticheat.txt
    - /workspace/novabuilder_nexuse_antiban_analysis.md

人类化目的 (逆向自 anticheat.txt):
    网易反作弊会监测玩家发送的聊天消息和命令, 判断是否为机器人:
        1. 消息长度规律性 (总是相同长度)
        2. 发送时间规律性 (固定间隔)
        3. 消息内容相似性 (相同模板)
        4. 命令参数完全相同 (无随机性)

人类化策略 (逆向自 humanizeCommandMessage):
    1. 添加随机前缀/后缀 (如 "!", "lol", "haha")
    2. 修改大小写 (随机大小写)
    3. 添加表情符号 (, 等)
    4. 替换同义词 (如 "stone" -> "rocks")
    5. 添加错别字 (故意拼错)
    6. 添加重复字符 (如 "stonnne")

字符串证据 (逆向自 strings):
    "humanizeCommandMessage"
    "Humanize"
    "chat"
    "message"
    "command"
"""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger("pocketterm.auth.humanize_command")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 人类化前缀 (逆向自 strings)
HUMANIZE_PREFIXES: list[str] = [
    "", "", "",  # 大多数消息不加前缀
    "lol", "haha", "umm", "well", "uh",
    "ok", "yeah", "hmm", "oh",
    "haha", "xD", "lol", "lmao",
]

#: 人类化后缀
HUMANIZE_SUFFIXES: list[str] = [
    "", "", "",  # 大多数消息不加后缀
    "lol", "haha", "xD", "hmm",
    "ok", "yeah", "bro", "mate",
    "...", "!", "?", "!!",
]

#: 表情符号 (逆向自 strings_html.txt)
EMOJIS: list[str] = [
    "", "", "", "",  # 大多数消息不加表情
    "", "", "", "",
    "", "", "", "",
    "", "", "",
]

#: 错别字映射 (故意拼错的常见词)
TYPO_MAP: dict[str, list[str]] = {
    "stone": ["stone", "ston", "stonne", "stone"],
    "dirt": ["dirt", "drit", "dirtt"],
    "wood": ["wood", "wod", "woodd"],
    "hello": ["hello", "helo", "hullo", "hello"],
    "thanks": ["thanks", "thx", "tysm", "thank"],
    "please": ["please", "pls", "plz", "plzz"],
    "yes": ["yes", "ye", "ya", "yeah", "yep"],
    "no": ["no", "nope", "nah", "no"],
}

#: 同义词映射 (替换为同义词)
SYNONYM_MAP: dict[str, list[str]] = {
    "good": ["good", "nice", "great", "cool"],
    "bad": ["bad", "terrible", "awful", "sucks"],
    "fast": ["fast", "quick", "rapid", "speedy"],
    "slow": ["slow", "sluggish", "laggy"],
    "big": ["big", "large", "huge", "massive"],
    "small": ["small", "tiny", "little", "mini"],
}

#: 重复字符概率
REPEAT_CHAR_PROBABILITY: float = 0.1

#: 重复字符数 (1-3)
REPEAT_CHAR_COUNT: tuple[int, int] = (1, 3)

#: 大小写变化概率
CASE_CHANGE_PROBABILITY: float = 0.05

#: 添加错别字概率
TYPO_PROBABILITY: float = 0.1

#: 同义词替换概率
SYNONYM_PROBABILITY: float = 0.2

#: 表情符号概率
EMOJI_PROBABILITY: float = 0.15


# -------------------------------------------------------------------- #
# 枚举
# -------------------------------------------------------------------- #


class HumanizeStrategy(Enum):
    """人类化策略 (逆向自 humanizeCommandMessage)。"""
    PREFIX_SUFFIX = auto()       # 添加前缀/后缀
    CASE_VARIATION = auto()      # 大小写变化
    TYPO = auto()                # 添加错别字
    SYNONYM = auto()             # 同义词替换
    EMOJI = auto()               # 添加表情符号
    REPEAT_CHAR = auto()         # 重复字符
    ALL = auto()                 # 全部策略


# -------------------------------------------------------------------- #
# 配置
# -------------------------------------------------------------------- #


@dataclass
class HumanizeConfig:
    """人类化配置。"""
    enabled: bool = True
    strategies: list[HumanizeStrategy] = field(default_factory=lambda: [
        HumanizeStrategy.PREFIX_SUFFIX,
        HumanizeStrategy.CASE_VARIATION,
        HumanizeStrategy.TYPO,
        HumanizeStrategy.SYNONYM,
        HumanizeStrategy.EMOJI,
        HumanizeStrategy.REPEAT_CHAR,
    ])
    prefix_probability: float = 0.15
    suffix_probability: float = 0.15
    emoji_probability: float = EMOJI_PROBABILITY
    typo_probability: float = TYPO_PROBABILITY
    synonym_probability: float = SYNONYM_PROBABILITY
    case_change_probability: float = CASE_CHANGE_PROBABILITY
    repeat_char_probability: float = REPEAT_CHAR_PROBABILITY
    seed: Optional[int] = None  # 随机种子 (None 使用系统时间)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "strategies": [s.name for s in self.strategies],
            "prefix_probability": self.prefix_probability,
            "suffix_probability": self.suffix_probability,
            "emoji_probability": self.emoji_probability,
            "typo_probability": self.typo_probability,
            "synonym_probability": self.synonym_probability,
            "case_change_probability": self.case_change_probability,
            "repeat_char_probability": self.repeat_char_probability,
        }


@dataclass
class HumanizeResult:
    """人类化结果。"""
    original: str = ""
    humanized: str = ""
    strategies_applied: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "original": self.original,
            "humanized": self.humanized,
            "strategies_applied": self.strategies_applied,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class HumanizeTemplate:
    """人类化模板 (用于自定义人类化规则)。"""
    name: str = ""
    template: str = "{message}"  # 模板字符串, {message} 是占位符
    weight: float = 1.0  # 权重 (用于随机选择)

    def apply(self, message: str) -> str:
        """应用模板。"""
        return self.template.format(message=message)


# -------------------------------------------------------------------- #
# humanizeCommandMessage (逆向自 strings)
# -------------------------------------------------------------------- #


def humanizeCommandMessage(
    message: str,
    config: Optional[HumanizeConfig] = None,
) -> HumanizeResult:
    """人类化命令消息 (逆向自 NovaBuilder 的 humanizeCommandMessage 函数)。

    将机器人发送的命令消息转换为更像人类发送的消息。

    Args:
        message: 原始消息
        config: 人类化配置 (None 使用默认)

    Returns:
        HumanizeResult

    使用示例:
        result = humanizeCommandMessage("/give @s stone 64")
        # result.humanized 可能是 "lol /give @s stonne 64 xD"

        result = humanizeCommandMessage("hello world")
        # result.humanized 可能是 "helo world haha"
    """
    if config is None:
        config = HumanizeConfig()

    start_time = time.time()
    result = HumanizeResult(original=message)

    if not config.enabled or not message:
        result.humanized = message
        result.elapsed_ms = (time.time() - start_time) * 1000
        return result

    # 设置随机种子
    if config.seed is not None:
        random.seed(config.seed)

    humanized = message
    strategies_applied: list[str] = []

    # 应用策略
    for strategy in config.strategies:
        if strategy == HumanizeStrategy.PREFIX_SUFFIX:
            new_text = _apply_prefix_suffix(humanized, config)
            if new_text != humanized:
                strategies_applied.append("prefix_suffix")
                humanized = new_text

        elif strategy == HumanizeStrategy.CASE_VARIATION:
            new_text = _apply_case_variation(humanized, config)
            if new_text != humanized:
                strategies_applied.append("case_variation")
                humanized = new_text

        elif strategy == HumanizeStrategy.TYPO:
            new_text = _apply_typo(humanized, config)
            if new_text != humanized:
                strategies_applied.append("typo")
                humanized = new_text

        elif strategy == HumanizeStrategy.SYNONYM:
            new_text = _apply_synonym(humanized, config)
            if new_text != humanized:
                strategies_applied.append("synonym")
                humanized = new_text

        elif strategy == HumanizeStrategy.EMOJI:
            new_text = _apply_emoji(humanized, config)
            if new_text != humanized:
                strategies_applied.append("emoji")
                humanized = new_text

        elif strategy == HumanizeStrategy.REPEAT_CHAR:
            new_text = _apply_repeat_char(humanized, config)
            if new_text != humanized:
                strategies_applied.append("repeat_char")
                humanized = new_text

    result.humanized = humanized
    result.strategies_applied = strategies_applied
    result.elapsed_ms = (time.time() - start_time) * 1000

    logger.debug(
        "Humanized: %r -> %r (strategies: %s, %.2fms)",
        result.original, result.humanized,
        result.strategies_applied, result.elapsed_ms,
    )
    return result


# -------------------------------------------------------------------- #
# 策略实现
# -------------------------------------------------------------------- #


def _apply_prefix_suffix(message: str, config: HumanizeConfig) -> str:
    """应用前缀/后缀。"""
    result = message

    # 添加前缀
    if random.random() < config.prefix_probability:
        prefix = random.choice(HUMANIZE_PREFIXES)
        if prefix:
            result = f"{prefix} {result}"
            logger.debug("Added prefix: %r", prefix)

    # 添加后缀
    if random.random() < config.suffix_probability:
        suffix = random.choice(HUMANIZE_SUFFIXES)
        if suffix:
            result = f"{result} {suffix}"
            logger.debug("Added suffix: %r", suffix)

    return result


def _apply_case_variation(message: str, config: HumanizeConfig) -> str:
    """应用大小写变化。"""
    result_chars: list[str] = []
    for char in message:
        if char.isalpha() and random.random() < config.case_change_probability:
            # 随机改变大小写
            if char.isupper():
                result_chars.append(char.lower())
            else:
                result_chars.append(char.upper())
        else:
            result_chars.append(char)
    return "".join(result_chars)


def _apply_typo(message: str, config: HumanizeConfig) -> str:
    """应用错别字。"""
    if random.random() >= config.typo_probability:
        return message

    # 查找可替换的词
    words = message.split()
    if not words:
        return message

    result_words: list[str] = []
    replaced = False
    for word in words:
        # 去除标点
        clean_word = re.sub(r"[^\w]", "", word.lower())
        if clean_word in TYPO_MAP and not replaced:
            if random.random() < 0.5:
                # 50% 概率替换
                typo = random.choice(TYPO_MAP[clean_word])
                # 保留原始大小写
                if word[0].isupper():
                    typo = typo.capitalize()
                # 保留标点
                prefix_match = re.match(r"^([^\w]*)", word)
                suffix_match = re.search(r"([^\w]*)$", word)
                prefix = prefix_match.group(1) if prefix_match else ""
                suffix = suffix_match.group(1) if suffix_match else ""
                result_words.append(f"{prefix}{typo}{suffix}")
                replaced = True
                continue

        result_words.append(word)

    return " ".join(result_words)


def _apply_synonym(message: str, config: HumanizeConfig) -> str:
    """应用同义词替换。"""
    if random.random() >= config.synonym_probability:
        return message

    # 查找可替换的词
    words = message.split()
    if not words:
        return message

    result_words: list[str] = []
    replaced = False
    for word in words:
        clean_word = re.sub(r"[^\w]", "", word.lower())
        if clean_word in SYNONYM_MAP and not replaced:
            if random.random() < 0.5:
                synonym = random.choice(SYNONYM_MAP[clean_word])
                # 保留原始大小写
                if word[0].isupper():
                    synonym = synonym.capitalize()
                # 保留标点
                prefix_match = re.match(r"^([^\w]*)", word)
                suffix_match = re.search(r"([^\w]*)$", word)
                prefix = prefix_match.group(1) if prefix_match else ""
                suffix = suffix_match.group(1) if suffix_match else ""
                result_words.append(f"{prefix}{synonym}{suffix}")
                replaced = True
                continue

        result_words.append(word)

    return " ".join(result_words)


def _apply_emoji(message: str, config: HumanizeConfig) -> str:
    """应用表情符号。"""
    if random.random() >= config.emoji_probability:
        return message

    emoji = random.choice(EMOJIS)
    if not emoji:
        return message

    # 随机位置添加
    if random.random() < 0.5:
        return f"{message} {emoji}"
    return f"{emoji} {message}"


def _apply_repeat_char(message: str, config: HumanizeConfig) -> str:
    """应用重复字符。"""
    if random.random() >= config.repeat_char_probability:
        return message

    # 找到字母字符位置
    alpha_positions = [
        i for i, c in enumerate(message) if c.isalpha()
    ]
    if not alpha_positions:
        return message

    # 随机选择一个位置重复
    pos = random.choice(alpha_positions)
    repeat_count = random.randint(REPEAT_CHAR_COUNT[0], REPEAT_CHAR_COUNT[1])
    char = message[pos]

    # 重复字符
    new_message = message[:pos] + char * repeat_count + message[pos + 1:]
    return new_message


# -------------------------------------------------------------------- #
# 批量人类化
# -------------------------------------------------------------------- #


def humanize_batch(
    messages: list[str],
    config: Optional[HumanizeConfig] = None,
) -> list[HumanizeResult]:
    """批量人类化消息列表。"""
    if config is None:
        config = HumanizeConfig()

    results: list[HumanizeResult] = []
    for msg in messages:
        results.append(humanizeCommandMessage(msg, config))
    return results


def humanize_with_template(
    message: str,
    template: HumanizeTemplate,
    config: Optional[HumanizeConfig] = None,
) -> str:
    """使用模板人类化消息。"""
    if config is None:
        config = HumanizeConfig()

    # 先应用标准人类化
    result = humanizeCommandMessage(message, config)
    # 再应用模板
    return template.apply(result.humanized)
