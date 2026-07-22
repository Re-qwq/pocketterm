"""mcfunction_parser - Minecraft 函数文件 (.mcfunction) 解析器。

逆向自 NexusEgo v1.6.5 的 MCFunction 解析层:

    - WaterStructure/structure/mcfunction.go

MCFunction 格式:
    - 每行一条 Minecraft 命令
    - 以 # 开头的行是注释
    - 空行被忽略
    - 支持execute as|at|align|anchored|facing|in|positioned|rotated|if|unless|run

字符串证据 (逆向自 strings):
    "execute in %s run %s"           -- execute in <维度> run <命令>
    "execute in the_end run setblock" -- 下界 setblock
    "setblock %d %d %d %s"           -- setblock 命令模板
    "fill %d %d %d %d %d %d minecraft:%s" -- fill 命令模板
    "gamerule commandblocksenabled false"  -- 关闭命令方块
    "tp @s \"%s\""                   -- 传送命令
    "tellraw %v %v"                  -- 原始消息
    "testfor @a[name=\"%s\"%s]"      -- 测试玩家

NexusE 特殊命令模式:
    - "nexus_build_*"               -- NexusE 构建任务标记
    - "setblock mcworld_*"          -- MCWorld 标记
    - "execute in %v run %v"        -- 跨维度执行
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("pocketterm.protocol.format_parsers.mcfunction_parser")


# -------------------------------------------------------------------- #
# 常量
# -------------------------------------------------------------------- #

#: 命令名 -> 类型 (逆向自 strings 中的命令模板)
BLOCK_COMMANDS: set[str] = {
    "setblock", "fill", "clone", "testforblocks",
}

#: 已知的 execute 子命令关键字 (逆向自 strings)
EXECUTE_SUBCOMMANDS: set[str] = {
    "as", "at", "align", "anchored", "facing",
    "in", "positioned", "rotated", "if", "unless", "run",
}

#: Minecraft 维度名称 (逆向自 strings: "unknown dimension %q")
MC_DIMENSIONS: set[str] = {"overworld", "the_end", "the_nether", "nether"}

#: NexusE 特殊命令前缀
NEXUSE_COMMAND_PREFIXES: tuple[str, ...] = (
    "nexus_build_", "nexus_mcstructure_", "setblock mcworld_",
    "nexusego_onedragon_", "ws_world_",
)

#: 坐标提取正则 (逆向自 strings: "%s@\\[(-?\\d+),(-?\\d+),(-?\\d+)\\]")
COORD_PATTERN: re.Pattern[str] = re.compile(
    r"~?\[?\s*(-?\d+)\s*,\s*(-?\d+)\s*,\s*(-?\d+)\s*\]?"
)

#: 选择器正则 (@s, @a, @p, @e, @r 及带参数的 @a[name=...])
SELECTOR_PATTERN: re.Pattern[str] = re.compile(
    r"@[saper](?:\[([^\]]*)\])?"
)


# -------------------------------------------------------------------- #
# 异常
# -------------------------------------------------------------------- #


class MCFunctionError(Exception):
    """MCFunction 文件解析错误基类。"""


# -------------------------------------------------------------------- #
# 数据结构
# -------------------------------------------------------------------- #


@dataclass
class MCFunctionCommand:
    """MCFunction 命令条目。"""
    line_number: int = 0
    raw_text: str = ""
    name: str = ""                # 命令名 (如 setblock, fill, execute)
    args: list[str] = field(default_factory=list)
    is_comment: bool = False
    is_empty: bool = False
    is_execute: bool = False
    execute_subcommands: list[dict[str, Any]] = field(default_factory=list)
    execute_run_command: str = ""
    dimension: str = ""
    coordinates: list[tuple[int, int, int]] = field(default_factory=list)
    selectors: list[str] = field(default_factory=list)
    is_block_command: bool = False
    is_nexuse_command: bool = False

    def __repr__(self) -> str:
        if self.is_comment:
            return f"MCFunctionCommand(line={self.line_number}, comment)"
        if self.is_empty:
            return f"MCFunctionCommand(line={self.line_number}, empty)"
        return f"MCFunctionCommand(line={self.line_number}, name={self.name!r})"


@dataclass
class MCFunctionResult:
    """MCFunction 文件解析结果。"""
    commands: list[MCFunctionCommand] = field(default_factory=list)
    total_lines: int = 0
    command_count: int = 0
    comment_count: int = 0
    empty_count: int = 0
    nexuse_command_count: int = 0
    block_command_count: int = 0
    execute_count: int = 0
    dimensions_used: set[str] = field(default_factory=set)

    @property
    def has_nexuse_commands(self) -> bool:
        """是否包含 NexusE 特殊命令。"""
        return self.nexuse_command_count > 0


# -------------------------------------------------------------------- #
# 解析主流程
# -------------------------------------------------------------------- #


def parse_mcfunction_text(text: str) -> MCFunctionResult:
    """解析 MCFunction 文本。

    逆向自 WaterStructure/structure/mcfunction.go 的 Parse 函数。

    Args:
        text: MCFunction 文件内容。

    Returns:
        :class:`MCFunctionResult` 解析结果。
    """
    result = MCFunctionResult()
    lines = text.splitlines()
    result.total_lines = len(lines)

    for line_num, raw_line in enumerate(lines, start=1):
        cmd = _parse_line(raw_line, line_num)
        result.commands.append(cmd)

        if cmd.is_comment:
            result.comment_count += 1
        elif cmd.is_empty:
            result.empty_count += 1
        else:
            result.command_count += 1
            if cmd.is_nexuse_command:
                result.nexuse_command_count += 1
            if cmd.is_block_command:
                result.block_command_count += 1
            if cmd.is_execute:
                result.execute_count += 1
            if cmd.dimension:
                result.dimensions_used.add(cmd.dimension)

    logger.info(
        "MCFunction parsed: lines=%d, commands=%d, comments=%d, nexuse=%d, blocks=%d",
        result.total_lines, result.command_count, result.comment_count,
        result.nexuse_command_count, result.block_command_count,
    )
    return result


def _parse_line(raw_line: str, line_num: int) -> MCFunctionCommand:
    """解析单行命令。"""
    stripped = raw_line.strip()
    cmd = MCFunctionCommand(line_number=line_num, raw_text=raw_line)

    # 空行
    if not stripped:
        cmd.is_empty = True
        return cmd

    # 注释
    if stripped.startswith("#"):
        cmd.is_comment = True
        return cmd

    # 分词 (简单的空格分词, 不处理引号内的空格)
    # NexusE 使用简单的空格分词, 复杂的引号处理在命令执行时进行
    tokens = _tokenize(stripped)
    if not tokens:
        cmd.is_empty = True
        return cmd

    cmd.name = tokens[0].lower()
    cmd.args = tokens[1:]

    # 检查 NexusE 特殊命令
    for prefix in NEXUSE_COMMAND_PREFIXES:
        if stripped.startswith(prefix):
            cmd.is_nexuse_command = True
            break

    # 检查是否是方块命令
    if cmd.name in BLOCK_COMMANDS:
        cmd.is_block_command = True

    # 解析 execute
    if cmd.name == "execute":
        cmd.is_execute = True
        _parse_execute(cmd)

    # 提取坐标
    cmd.coordinates = extract_coordinates(stripped)

    # 提取选择器
    cmd.selectors = extract_selectors(stripped)

    return cmd


def _tokenize(text: str) -> list[str]:
    """简单的命令分词器。

    支持双引号包裹的参数 (引号内的空格不分词)。
    """
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    quote_char = ""

    for ch in text:
        if not in_quotes and ch in "\"'":
            in_quotes = True
            quote_char = ch
            current.append(ch)
        elif in_quotes and ch == quote_char:
            in_quotes = False
            quote_char = ""
            current.append(ch)
        elif not in_quotes and ch in " \t":
            if current:
                tokens.append("".join(current))
                current = []
        else:
            current.append(ch)

    if current:
        tokens.append("".join(current))
    return tokens


def _parse_execute(cmd: MCFunctionCommand) -> None:
    """解析 execute 命令的子命令链。

    逆向自 strings: "execute in %s run %s" 模板。
    NexusE 大量使用 execute in <维度> run setblock 模式。
    """
    args = cmd.args
    i = 0
    while i < len(args):
        sub = args[i].lower()
        if sub not in EXECUTE_SUBCOMMANDS:
            i += 1
            continue

        if sub == "run":
            # 剩余部分是实际命令
            cmd.execute_run_command = " ".join(args[i + 1:])
            break

        if sub == "in":
            # 维度
            if i + 1 < len(args):
                cmd.dimension = args[i + 1]
                cmd.execute_subcommands.append({
                    "type": "in",
                    "dimension": args[i + 1],
                })
                i += 2
                continue

        # 其他子命令 (as, at, positioned, rotated 等)
        if i + 1 < len(args):
            cmd.execute_subcommands.append({
                "type": sub,
                "arg": args[i + 1],
            })
            i += 2
        else:
            i += 1


# -------------------------------------------------------------------- #
# 辅助函数
# -------------------------------------------------------------------- #


def extract_coordinates(text: str) -> list[tuple[int, int, int]]:
    """从命令文本中提取所有坐标。

    逆向自 strings: "%s@\\[(-?\\d+),(-?\\d+),(-?\\d+)\\]"
    和 "~\\[(-?\\d+),(-?\\d+),(-?\\d+)\\]"

    Args:
        text: 命令文本。

    Returns:
        坐标元组列表 [(x, y, z), ...]。
    """
    coords: list[tuple[int, int, int]] = []
    for m in COORD_PATTERN.finditer(text):
        try:
            x = int(m.group(1))
            y = int(m.group(2))
            z = int(m.group(3))
            coords.append((x, y, z))
        except (ValueError, IndexError):
            continue
    return coords


def extract_selectors(text: str) -> list[str]:
    """从命令文本中提取所有目标选择器。

    支持 @s, @a, @p, @e, @r 及带参数的形式如 @a[name=...].

    Args:
        text: 命令文本。

    Returns:
        选择器字符串列表。
    """
    selectors: list[str] = []
    for m in SELECTOR_PATTERN.finditer(text):
        selectors.append(m.group(0))
    return selectors


def is_block_command(cmd_name: str) -> bool:
    """判断命令名是否是方块相关命令。

    Args:
        cmd_name: 命令名。

    Returns:
        True 如果是 setblock / fill / clone / testforblocks。
    """
    return cmd_name.lower() in BLOCK_COMMANDS


def parse_mcfunction_bytes(data: bytes) -> MCFunctionResult:
    """解析 MCFunction 字节。"""
    text = data.decode("utf-8", errors="replace")
    return parse_mcfunction_text(text)


def parse_mcfunction_file(file_path: str) -> MCFunctionResult:
    """解析 MCFunction 文件。"""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return parse_mcfunction_text(text)


__all__ = [
    "BLOCK_COMMANDS", "EXECUTE_SUBCOMMANDS", "MC_DIMENSIONS",
    "NEXUSE_COMMAND_PREFIXES",
    "MCFunctionError",
    "MCFunctionCommand", "MCFunctionResult",
    "parse_mcfunction_text", "parse_mcfunction_bytes", "parse_mcfunction_file",
    "extract_coordinates", "extract_selectors", "is_block_command",
]
