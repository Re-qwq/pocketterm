"""建筑控制台交互系统。

集成建筑导入、导出、备份、恢复于一体的控制台交互模块。
通过聊天关键词触发操作 (类似 ToolDelta 插件的方式)。

触发关键词:
    - ``导入 <文件名> [x y z]``     导入建筑文件
    - ``导出 <文件名> <x1 y1 z1> <x2 y2 z2> [格式]``  导出区域为文件
    - ``备份 <名称> <x1 y1 z1> <x2 y2 z2>``  备份区域
    - ``恢复 <名称> [x y z]``      恢复备份
    - ``列表``                     列出可用建筑文件
    - ``停止``                     停止当前操作

逆向来源:
    - Retalcer导入器 menu.py (菜单交互)
    - ToolDelta 插件框架 (关键词触发)
    - NexusEgo (进度显示和批量操作)

基本用法::

    from app.protocol.connection import BedrockClient
    from app.protocol.magic_command import MagicCommandSender
    from app.protocol.console import BuildingConsole

    client = BedrockClient(sauth_json="...", device_fingerprint={...})
    await client.connect("example.com", 19132)

    sender = MagicCommandSender(client)
    console = BuildingConsole(sender, file_dir="/path/to/buildings")
    await console.start()

    # 在聊天事件回调中:
    await console.handle_chat("玩家名", "导入 house.mcstructure 0 64 0")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import DATA_DIR, PROJECT_ROOT
from .magic_command import (
    MagicCommandSender,
    apply_speed_preset,
    SPEED_PRESETS,
)
from .blocks import BlockState
from .structure_parser import StructureParser, ParsedStructure, SUPPORTED_FORMATS
from .batch_optimizer import (
    BatchOptimizer,
    BlockEntry,
    IncrementalImporter,
    ProgressTracker,
)
from .nbt_placer import NBTBlockPlacer, NBTPlacementMode
from .exporter import StructureExporter, ExportConfig, ExportResult

# PhoenixBuilder 模块导入 (专业 BDump 引擎)
from .phoenix_builder import PhoenixPlanner, PhoenixExecutor, BDumpWriter
from .phoenix_omega import OmegaImporter, detect_format, get_parser_for_file

logger = logging.getLogger("pocketterm.protocol.console")


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------

#: 支持的导出格式
EXPORT_FORMATS: tuple[str, ...] = ("mcstructure", "schematic", "mcworld")

#: 默认搜索目录的文件扩展名
BUILDING_EXTENSIONS: tuple[str, ...] = (
    ".mcstructure",
    ".nbt",
    ".schematic",
    ".schem",
    ".bdx",
    ".mcworld",
    ".litematic",
)

#: 控制台关键词前缀
PREFIX_IMPORT: str = "导入"
PREFIX_EXPORT: str = "导出"
PREFIX_BACKUP: str = "备份"
PREFIX_RESTORE: str = "恢复"
PREFIX_LIST: str = "列表"
PREFIX_STOP: str = "停止"
PREFIX_HELP: str = "帮助"


# ----------------------------------------------------------------------
# 数据类
# ----------------------------------------------------------------------


@dataclass
class ImportConfig:
    """导入配置。

    .. important::

        **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
        不能放附魔/自定义名字), 因此 ``nbt_mode`` 默认值已从 ``"replaceitem"``
        改为 ``"structure"`` (平台模式, 通过 structure save/load 搬运 NBT 方块)。

    Attributes:
        origin_x, origin_y, origin_z: 导入原点坐标
        include_air: 是否包含空气方块
        incremental: 是否使用增量导入 (多区块合并)
        group_size: 增量导入的区块组大小
        optimize_mode: 优化模式 ("z_merge" / "cube_expand" / "none")
        process_nbt: 是否处理 NBT 方块 (告示牌、容器等)
        build_platform: 是否建造海晶灯平台 (NBT 处理需要)
        nbt_mode: NBT 放置模式 ("structure" / "replaceitem" / "auto")
            默认 "structure" (网易 3.8 推荐方案)
        nbt_auto_detect: 是否启用自动检测 (仅在 nbt_mode="auto" 时生效)
        speed_preset: 速度预设 (slow/medium/fast/turbo), 设为 "custom" 使用自定义值
        block_speed: 方块放置速度 (方块/秒), 当 speed_preset="custom" 时生效
        command_speed: 命令方块加载速度 (命令/秒), 当 speed_preset="custom" 时生效
        container_speed: 容器物品速度 (物品/秒), 当 speed_preset="custom" 时生效
        nbt_delay: NBT操作延迟 (秒), 当 speed_preset="custom" 时生效
        group_wait: 组间等待时间 (秒), 当 speed_preset="custom" 时生效
    """

    origin_x: int = 0
    origin_y: int = 64
    origin_z: int = 0
    include_air: bool = False
    incremental: bool = True
    group_size: int = 3
    optimize_mode: str = "z_merge"
    process_nbt: bool = True
    build_platform: bool = True
    # NBT 模式设置
    # 默认 "structure" (网易 3.8 推荐方案, replaceitem 已被阉割)
    # 可选 "replaceitem" (用户明确知道 3.8 风险时可选, 只能放耐久/特殊值/数量/NBT标签)
    nbt_mode: str = "structure"  # structure / replaceitem / auto
    nbt_auto_detect: bool = True   # 自动检测 (仅在 auto 模式生效)
    # 速度设置
    speed_preset: str = "medium"  # slow/medium/fast/turbo/custom
    block_speed: int = 20        # 方块/秒 (custom 模式生效)
    command_speed: int = 10      # 命令方块/秒 (custom 模式生效)
    container_speed: int = 5     # 容器物品/秒 (custom 模式生效)
    nbt_delay: float = 0.5       # NBT操作延迟(秒) (custom 模式生效)
    group_wait: float = 1.0      # 组间等待(秒) (custom 模式生效)


@dataclass
class BackupInfo:
    """备份信息。"""

    name: str
    x1: int
    y1: int
    z1: int
    x2: int
    y2: int
    z2: int
    created_at: float = field(default_factory=time.time)
    structure_name: str = ""


# ----------------------------------------------------------------------
# 控制台
# ----------------------------------------------------------------------


class BuildingConsole:
    """建筑控制台 - 集成导入/导出/备份/恢复。

    通过聊天关键词触发操作,集成文件解析、批量优化、NBT处理、导出等功能。

    Args:
        sender: 魔法指令发送器
        file_dir: 建筑文件存放目录
        platform_center: 海晶灯平台中心坐标 (用于NBT处理)
    """

    def __init__(
        self,
        sender: MagicCommandSender,
        file_dir: str = "./buildings",
        platform_center: tuple[int, int, int] = (0, 200, 0),
        send_packet: Optional[callable] = None,
    ):
        self.sender = sender
        self.file_dir = Path(file_dir)
        self.file_dir.mkdir(parents=True, exist_ok=True)

        # 子模块实例
        self.parser = StructureParser()
        self.optimizer = BatchOptimizer(sender)
        self.importer = IncrementalImporter(sender, self.optimizer)
        self.nbt_placer = NBTBlockPlacer(
            sender,
            send_packet=send_packet,
            # 网易 3.8 阉割了 replaceitem, 默认使用 STRUCTURE 平台模式
            # (通过 structure save/load 搬运 NBT 方块)
            nbt_mode=NBTPlacementMode.STRUCTURE,
            auto_detect=True,
        )
        self.exporter = StructureExporter()

        # PhoenixBuilder Omega 导入器 (专业 BDump 引擎, 可选增强)
        # 使用 sender 的 wocmd 和 cmd 方法作为回调
        self.phoenix_importer = OmegaImporter(
            block_cmd_sender=sender.send_wo_command,
            normal_cmd_sender=sender.send_ai_command,
        )

        # NBT 平台坐标
        self.platform_center = platform_center

        # 运行状态
        self._running: bool = False
        self._current_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # 备份记录
        self._backups: dict[str, BackupInfo] = {}

        # 消息回调 (用于向聊天框发送反馈)
        self._message_callback: Optional[Callable[[str], Any]] = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动控制台。"""
        self._running = True
        logger.info("建筑控制台已启动")
        await self._send_feedback("§a建筑控制台已启动! 输入 '帮助' 查看用法")

    async def stop(self) -> None:
        """停止控制台。"""
        self._running = False
        self._stop_event.set()
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
        logger.info("建筑控制台已停止")

    def set_message_callback(self, callback: Callable[[str], Any]) -> None:
        """设置消息回调函数 (用于向聊天框发送反馈)。

        Args:
            callback: 回调函数,接收一个字符串参数 (消息内容)
        """
        self._message_callback = callback

    # ------------------------------------------------------------------
    # 聊天处理
    # ------------------------------------------------------------------

    async def handle_chat(self, player_name: str, message: str) -> None:
        """处理聊天消息 (关键词触发)。

        Args:
            player_name: 玩家名
            message: 聊天消息
        """
        if not self._running:
            return

        message = message.strip()
        if not message:
            return

        try:
            # 解析命令
            cmd = self._parse_command(message)
            if cmd is None:
                return

            cmd_type = cmd["type"]

            # 停止命令立即处理
            if cmd_type == "stop":
                await self._handle_stop()
                return

            # 检查是否有任务正在运行
            if self._current_task and not self._current_task.done():
                await self._send_feedback("§c有任务正在运行,请先输入 '停止'")
                return

            # 启动异步任务
            if cmd_type == "import":
                self._current_task = asyncio.create_task(self._handle_import(cmd))
            elif cmd_type == "export":
                self._current_task = asyncio.create_task(self._handle_export(cmd))
            elif cmd_type == "backup":
                self._current_task = asyncio.create_task(self._handle_backup(cmd))
            elif cmd_type == "restore":
                self._current_task = asyncio.create_task(self._handle_restore(cmd))
            elif cmd_type == "list":
                await self._handle_list()
            elif cmd_type == "help":
                await self._handle_help()

        except Exception as e:
            logger.error(f"处理聊天命令失败: {e}", exc_info=True)
            await self._send_feedback(f"§c命令执行失败: {e}")

    def _parse_command(self, message: str) -> Optional[dict]:
        """解析聊天命令。

        Args:
            message: 聊天消息

        Returns:
            命令字典,无法识别时返回 None
        """
        parts = message.split()

        if not parts:
            return None

        prefix = parts[0]

        if prefix == PREFIX_IMPORT and len(parts) >= 2:
            # 导入 <文件名> [x y z]
            result = {"type": "import", "filename": parts[1]}
            if len(parts) >= 5:
                try:
                    result["x"] = int(parts[2])
                    result["y"] = int(parts[3])
                    result["z"] = int(parts[4])
                except ValueError:
                    return None
            return result

        if prefix == PREFIX_EXPORT and len(parts) >= 8:
            # 导出 <文件名> <x1 y1 z1> <x2 y2 z2> [格式]
            result = {
                "type": "export",
                "filename": parts[1],
                "x1": int(parts[2]),
                "y1": int(parts[3]),
                "z1": int(parts[4]),
                "x2": int(parts[5]),
                "y2": int(parts[6]),
                "z2": int(parts[7]),
            }
            if len(parts) >= 9:
                result["format"] = parts[8]
            else:
                result["format"] = "mcstructure"
            return result

        if prefix == PREFIX_BACKUP and len(parts) >= 8:
            # 备份 <名称> <x1 y1 z1> <x2 y2 z2>
            return {
                "type": "backup",
                "name": parts[1],
                "x1": int(parts[2]),
                "y1": int(parts[3]),
                "z1": int(parts[4]),
                "x2": int(parts[5]),
                "y2": int(parts[6]),
                "z2": int(parts[7]),
            }

        if prefix == PREFIX_RESTORE and len(parts) >= 2:
            # 恢复 <名称> [x y z]
            result = {"type": "restore", "name": parts[1]}
            if len(parts) >= 5:
                try:
                    result["x"] = int(parts[2])
                    result["y"] = int(parts[3])
                    result["z"] = int(parts[4])
                except ValueError:
                    return None
            return result

        if prefix == PREFIX_LIST:
            return {"type": "list"}

        if prefix == PREFIX_STOP:
            return {"type": "stop"}

        if prefix == PREFIX_HELP:
            return {"type": "help"}

        return None

    # ------------------------------------------------------------------
    # 导入处理
    # ------------------------------------------------------------------

    async def _handle_import(self, cmd: dict) -> None:
        """处理导入命令。

        流程:
            1. 查找建筑文件
            2. 自动检测文件格式
            3. 根据 import_engine 设置选择引擎:
               - "phoenix": 使用 PhoenixBuilder Omega 导入管线
               - "retalcer": 使用 Retalcer 增量导入器
               - "auto": 根据文件格式自动选择 (BDX -> phoenix, 其他 -> retalcer)
            4. 解析文件
            5. 构建方块列表
            6. 增量导入 (多区块合并)
            7. 处理 NBT 方块
            8. 显示进度
        """
        filename = cmd["filename"]
        origin_x = cmd.get("x", 0)
        origin_y = cmd.get("y", 64)
        origin_z = cmd.get("z", 0)

        # 1. 查找文件
        file_path = self._find_file(filename)
        if file_path is None:
            await self._send_feedback(f"§c文件未找到: {filename}")
            return

        await self._send_feedback(f"§e正在解析文件: {os.path.basename(file_path)}")

        # 1.5. 加载速度设置 (需要先加载以获取 import_engine)
        speed_settings = self._load_speed_settings()
        import_engine = speed_settings.get("import_engine", "phoenix")

        # 2. 自动检测文件格式
        file_ext = file_path.suffix.lower()
        detected_format = None
        try:
            with open(file_path, "rb") as f:
                header = f.read(1024)
            detected_format = detect_format(header)
        except Exception:
            pass

        await self._send_feedback(
            f"§7文件格式: {file_ext} (检测: {detected_format or '未知'})"
        )

        # 2.5. 引擎选择逻辑
        if import_engine == "auto":
            # 自动模式: BDX 文件使用 PhoenixBuilder, 其他使用 Retalcer
            if detected_format == ".bdx" or file_ext == ".bdx":
                use_phoenix = True
            else:
                use_phoenix = False
        elif import_engine == "phoenix":
            use_phoenix = True
        else:
            use_phoenix = False

        # 3. PhoenixBuilder 引擎路径
        if use_phoenix:
            await self._send_feedback(
                "§a使用 PhoenixBuilder 引擎导入..."
            )
            try:
                await self.phoenix_importer.import_file(
                    file_path=str(file_path),
                    offset=(origin_x, origin_y, origin_z),
                )
                await self._send_feedback("§a§lPhoenixBuilder 导入完成!")
            except asyncio.CancelledError:
                await self._send_feedback("§e导入已停止")
                return
            except Exception as e:
                await self._send_feedback(
                    f"§cPhoenixBuilder 导入失败: {e}, 回退到 Retalcer 引擎"
                )
                logger.warning(f"PhoenixBuilder 导入失败, 回退 Retalcer: {e}")
                use_phoenix = False  # 回退
            else:
                return  # PhoenixBuilder 成功, 直接返回

        # 4. Retalcer 引擎路径 (原始逻辑, 保持不变)
        # 解析文件
        try:
            structure = await self.parser.parse_file(str(file_path))
        except Exception as e:
            await self._send_feedback(f"§c解析失败: {e}")
            return

        block_count = structure.get_block_count()
        await self._send_feedback(
            f"§a解析成功! 尺寸: {structure.size}, 方块数: {block_count}"
        )

        if block_count == 0:
            await self._send_feedback("§e警告: 没有方块需要导入")
            return

        # 构建方块列表
        blocks: list[BlockEntry] = []
        nbt_blocks: list[dict] = []

        for x, y, z, block in structure.iter_blocks():
            entry = BlockEntry(x=x, y=y, z=z, block=block)
            blocks.append(entry)

            # 检查是否有 NBT 数据
            block_entity = structure.get_block_entity_at(x, y, z)
            if block_entity is not None:
                nbt_blocks.append(
                    {
                        "x": x,
                        "y": y,
                        "z": z,
                        "block": block,
                        "nbt": block_entity,
                    }
                )

        # 增量导入
        config = ImportConfig(
            origin_x=origin_x,
            origin_y=origin_y,
            origin_z=origin_z,
            incremental=True,
            process_nbt=len(nbt_blocks) > 0,
            # 网易 3.8 阉割了 replaceitem, 默认 "structure" (平台模式)
            nbt_mode=speed_settings.get("nbt_mode", "structure"),
            nbt_auto_detect=speed_settings.get("nbt_auto_detect", True),
            speed_preset=speed_settings.get("speed_preset", "medium"),
            block_speed=speed_settings.get("block_speed", 20),
            command_speed=speed_settings.get("command_speed", 10),
            container_speed=speed_settings.get("container_speed", 5),
            nbt_delay=speed_settings.get("nbt_delay", 0.5),
            group_wait=speed_settings.get("group_wait", 1.0),
        )

        # 应用速度设置到 sender 的 rate_limiter
        if config.speed_preset != "custom":
            apply_speed_preset(self.sender.rate_limiter, config.speed_preset)
        else:
            self.sender.rate_limiter.update_speeds(
                block_speed=config.block_speed,
                command_speed=config.command_speed,
                container_speed=config.container_speed,
                nbt_delay=config.nbt_delay,
                group_wait=config.group_wait,
            )

        # 应用 NBT 模式设置到 nbt_placer
        nbt_mode = NBTPlacementMode(config.nbt_mode)
        self.nbt_placer.set_nbt_mode(nbt_mode)
        self.nbt_placer.auto_detect = config.nbt_auto_detect
        logger.info(
            "NBT 放置模式: %s, 自动检测: %s",
            nbt_mode.value, config.nbt_auto_detect,
        )

        await self._send_feedback(
            f"§e开始导入 {block_count} 个方块 "
            f"(原点: {origin_x}, {origin_y}, {origin_z})..."
        )

        # 进度跟踪
        tracker = ProgressTracker(self.sender, block_count)

        # 导入方块
        try:
            placed = await self.importer.import_blocks(
                blocks=blocks,
                origin_x=origin_x,
                origin_y=origin_y,
                origin_z=origin_z,
                include_air=config.include_air,
                progress_callback=tracker.update,
            )

            await tracker.complete()
            await self._send_feedback(f"§a方块导入完成! 成功放置 {placed} 个方块")

        except asyncio.CancelledError:
            await self._send_feedback("§e导入已停止")
            return
        except Exception as e:
            await self._send_feedback(f"§c导入失败: {e}")
            logger.error("导入失败", exc_info=True)
            return

        # 5. 处理 NBT 方块
        if config.process_nbt and nbt_blocks:
            await self._send_feedback(
                f"§e开始处理 {len(nbt_blocks)} 个 NBT 方块..."
            )

            try:
                await self._process_nbt_blocks(nbt_blocks, origin_x, origin_y, origin_z)
                await self._send_feedback("§aNBT 方块处理完成!")
            except asyncio.CancelledError:
                await self._send_feedback("§eNBT 处理已停止")
                return
            except Exception as e:
                await self._send_feedback(f"§cNBT 处理失败: {e}")
                logger.error("NBT处理失败", exc_info=True)

        await self._send_feedback("§a§l全部导入完成!")

    async def _process_nbt_blocks(
        self,
        nbt_blocks: list[dict],
        origin_x: int,
        origin_y: int,
        origin_z: int,
    ) -> None:
        """处理 NBT 方块 (使用智能双模式放置系统, 顺序执行, 无时长限制)。

        .. important::

            **用户反馈 (NBT 制作无时长限制 + 顺序执行)**:
                "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个 NBT 制作完,
                不管多长时间, 然后第一个制作完开始制作第二个"

            本方法**移除所有超时设置**, NBT 方块**顺序执行**:
                - 第一个 NBT 方块完全制作完成 (包括 structure load 成功) 后
                - 才开始第二个 NBT 方块的制作
                - 每个方块放置后, 通过 :meth:`NBTBlockPlacer._wait_for_nbt_completion`
                  等待服务器确认 (无超时)

            **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
            不能放附魔/自定义名字), 因此本方法**默认建造平台** (structure 模式),
            通过 structure save/load 搬运 NBT 方块。仅在用户显式选择
            replaceitem 模式时才不建平台。

            **用户反馈 (普通物品 vs NBT 物品)**:
                "如果容器里面有普通的可以用 rep 指令放入的物品, 应该用 rep 指令放置"
                容器中的普通物品用 replaceitem (快速), NBT 物品用平台模式。
                此分流逻辑在 :meth:`NBTBlockPlacer.place_container` 内部实现。

        根据当前 NBT 模式设置:
            - **structure 模式 (默认, 网易 3.8 推荐)**: 在 11x11 海晶灯平台
              生成 NBT 方块, 通过 structure save/load 搬运到目标位置。
            - **replaceitem 模式 (可选, 3.8 风险)**: 直接在目标位置放置方块
              并写入 NBT 数据, 无需建造平台和 structure save/load 搬运。
              (3.8 只能放耐久/特殊值/数量/NBT标签)

        流程:
            - structure 模式 (默认):
              1. 建造 11x11 海晶灯平台 (先清空 11x11x5, 再填充 11x1x11)
              2. 建造工作方块 (铁砧/织布机/合成台/切石机/锻造台)
              3. **顺序处理**每个 NBT 方块 (无超时, 等完成再继续):
                 a. 在平台生成 NBT 方块
                 b. structure save (等待完成, 无超时)
                 c. tp 到目标位置
                 d. structure load (等待完成, 无超时) -- 确认成功
                 e. 清理临时方块
                 f. 等待服务器确认后再处理下一个
              4. 清理平台

        Args:
            nbt_blocks: NBT 方块列表, 每个元素包含:
                - ``block`` (BlockState): 方块对象
                - ``nbt`` (dict): NBT 数据
                - ``x``, ``y``, ``z`` (int): 相对坐标
            origin_x, origin_y, origin_z: 导入原点坐标。
        """
        px, py, pz = self.platform_center

        # 检查当前模式是否需要建造平台
        # 网易 3.8 阉割了 replaceitem, 默认建造平台 (structure 模式)
        # 仅在用户显式选择 REPLACEITEM 时才不建平台
        current_mode = self.nbt_placer.nbt_mode
        # STRUCTURE 和 AUTO 模式都建平台:
        # - STRUCTURE: 直接走平台搬运
        # - AUTO: detect() 默认推荐 STRUCTURE (网易 3.8), 因此也建平台
        # - REPLACEITEM: 用户显式选择, 不建平台 (用户需知晓 3.8 风险)
        needs_platform = current_mode != NBTPlacementMode.REPLACEITEM

        if needs_platform:
            # structure 模式 (默认): 建造 11x11 平台和工作方块
            await self.nbt_placer.build_platform(px, py, pz)
            await self.nbt_placer.build_work_blocks(px, py, pz)

        # === 顺序处理每个 NBT 方块 (无时长限制, 等完成再继续) ===
        # 用户反馈: "应该不会说是制作 NBT 时候有时长限制, 应该是等第一个
        # NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"
        total = len(nbt_blocks)
        success_count = 0
        fail_count = 0

        for i, nbt_block in enumerate(nbt_blocks):
            if self._stop_event.is_set():
                logger.info("NBT 处理已被用户停止 (已处理 %d/%d)", i, total)
                break

            block = nbt_block["block"]
            nbt_data = nbt_block["nbt"]
            target_x = nbt_block["x"] + origin_x
            target_y = nbt_block["y"] + origin_y
            target_z = nbt_block["z"] + origin_z

            # 进度显示 (每个方块都显示, 强调顺序执行)
            await self._send_feedback(
                f"§eNBT 处理进度: {i + 1}/{total} "
                f"({(i + 1) * 100 // total}%) - 正在处理第 {i + 1} 个 "
                "[顺序执行, 无时长限制]"
            )

            # 根据方块类型选择处理方式
            block_name = block.name

            # 确定方块类型
            if "sign" in block_name:
                block_type = "sign"
            elif any(k in block_name for k in ["chest", "barrel", "shulker", "hopper",
                                                  "furnace", "dispenser", "dropper",
                                                  "jukebox", "brewing_stand", "lectern"]):
                block_type = "container"
            elif "command_block" in block_name:
                block_type = "command_block"
            elif "banner" in block_name:
                block_type = "banner"
            else:
                block_type = "other"

            # === 处理当前 NBT 方块 (顺序执行, 等完成再继续下一个) ===
            logger.info(
                "NBT 处理: 开始处理第 %d/%d 个方块 %s @ 目标 (%d, %d, %d) "
                "[等完成再继续下一个]",
                i + 1, total, block_name, target_x, target_y, target_z,
            )

            block_success = False
            try:
                # 使用智能放置方法
                if block_type in ("sign", "container", "command_block", "banner"):
                    # 构建 nbt_data 字典
                    smart_nbt_data: dict[str, Any] = {}

                    if block_type == "sign":
                        text_lines = self._extract_sign_text(nbt_data)
                        text = "\n".join(text_lines)
                        facing = block.states.get("direction", "north")
                        is_wall = "wall" in block_name
                        smart_nbt_data = {
                            "text": text,
                            "facing": facing,
                            "is_wall": is_wall,
                            "text_lines": text_lines,
                        }

                    elif block_type == "container":
                        # 容器物品分流 (普通物品 replaceitem / NBT 物品 平台模式)
                        # 在 NBTBlockPlacer.place_container 内部实现
                        items = self._extract_container_items(nbt_data)
                        smart_nbt_data = {
                            "block_name": block_name,
                            "items": items,
                        }

                    elif block_type == "command_block":
                        cmd_text = nbt_data.get("Command", "")
                        mode = "impulse"
                        if "chain" in block_name:
                            mode = "chain"
                        elif "repeating" in block_name:
                            mode = "repeat"
                        conditional = nbt_data.get("conditional_bit", False)
                        smart_nbt_data = {
                            "command": cmd_text,
                            "mode": mode,
                            "conditional": conditional,
                            "redstone": "always_active",
                        }

                    elif block_type == "banner":
                        patterns = nbt_data.get("Patterns", [])
                        base_color = block.states.get("banner_color", 0)
                        smart_nbt_data = {
                            "patterns": patterns,
                            "base_color": base_color,
                        }

                    # place_nbt_block_smart 内部会调用 _save_structure / _load_structure,
                    # 这两个方法现在使用 _wait_for_nbt_completion (无超时, 顺序执行)
                    block_success = await self.nbt_placer.place_nbt_block_smart(
                        block_type=block_type,
                        block_name=block_name,
                        x=px, y=py + 1, z=pz,
                        nbt_data=smart_nbt_data,
                        target_x=target_x,
                        target_y=target_y,
                        target_z=target_z,
                        platform_x=px,
                        platform_y=py,
                        platform_z=pz,
                    )

                else:
                    # 其他 NBT 方块: 直接用 setblock + NBT
                    nbt_str = self._nbt_to_command_str(nbt_data)
                    await self.sender.send_any_command(
                        f"setblock {target_x} {target_y} {target_z} {block_name} {nbt_str}"
                    )
                    block_success = True

            except asyncio.CancelledError:
                logger.info("NBT 处理被取消 (第 %d/%d 个)", i + 1, total)
                raise
            except Exception as e:
                logger.error(
                    "NBT 处理第 %d/%d 个方块 %s 失败: %s",
                    i + 1, total, block_name, e, exc_info=True,
                )
                block_success = False

            # === 确认机制: 检查当前 NBT 方块是否处理成功 ===
            # 用户反馈: "等第一个 NBT 制作完, 不管多长时间, 然后第一个制作完开始制作第二个"
            # place_nbt_block_smart 内部的 _load_structure 已等待服务器确认,
            # 此处仅做日志记录和计数, 不再添加额外等待
            if block_success:
                success_count += 1
                logger.info(
                    "NBT 处理: 第 %d/%d 个方块 %s 处理成功, 继续下一个 "
                    "[顺序执行]",
                    i + 1, total, block_name,
                )
            else:
                fail_count += 1
                logger.warning(
                    "NBT 处理: 第 %d/%d 个方块 %s 处理失败, 继续下一个 "
                    "[顺序执行, 不中断]",
                    i + 1, total, block_name,
                )

            # 清理临时方块 (仅在 structure 模式)
            # 注: 清理不阻塞下一个方块的开始 (清理是异步的)
            if needs_platform:
                await self.sender.send_any_command(
                    f"setblock {px} {py + 1} {pz} air 0 destroy"
                )

        # 处理完成汇总
        await self._send_feedback(
            f"§eNBT 方块处理汇总: 成功 {success_count}, 失败 {fail_count}, "
            f"总计 {total} [顺序执行, 无时长限制]"
        )

        # 清理平台 (仅在 structure 模式)
        if needs_platform:
            await self.nbt_placer.cleanup_platform(px, py, pz)

    # ------------------------------------------------------------------
    # 导出处理
    # ------------------------------------------------------------------

    async def _handle_export(self, cmd: dict) -> None:
        """处理导出命令。

        流程:
            1. 从游戏中读取区域方块
            2. 转换为目标格式
            3. 保存到文件
        """
        filename = cmd["filename"]
        x1, y1, z1 = cmd["x1"], cmd["y1"], cmd["z1"]
        x2, y2, z2 = cmd["x2"], cmd["y2"], cmd["z2"]
        fmt = cmd.get("format", "mcstructure")

        if fmt not in EXPORT_FORMATS:
            await self._send_feedback(
                f"§c不支持的导出格式: {fmt}, 支持: {', '.join(EXPORT_FORMATS)}"
            )
            return

        # 确保文件名有正确的扩展名
        if not any(filename.endswith(ext) for ext in BUILDING_EXTENSIONS):
            filename += f".{fmt}"

        file_path = self.file_dir / filename

        await self._send_feedback(
            f"§e开始导出区域 ({x1},{y1},{z1})-({x2},{y2},{z2}) 为 {fmt}..."
        )

        # 规范化坐标
        min_x, max_x = min(x1, x2), max(x1, x2)
        min_y, max_y = min(y1, y2), max(y1, y2)
        min_z, max_z = min(z1, z2), max(z1, z2)
        size_x = max_x - min_x + 1
        size_y = max_y - min_y + 1
        size_z = max_z - min_z + 1

        total = size_x * size_y * size_z
        if total > 1000000:
            await self._send_feedback(
                f"§c区域太大 ({total} 方块), 最多支持 100 万方块"
            )
            return

        try:
            # 读取区域方块
            await self._send_feedback("§e正在读取区域方块...")

            blocks_data, block_entities = await self._read_region(
                min_x, min_y, min_z, size_x, size_y, size_z
            )

            await self._send_feedback("§e正在转换为文件...")

            # 导出
            config = ExportConfig(format=fmt, include_block_entities=True)

            if fmt == "mcstructure":
                data = await self.exporter.export_to_mcstructure(
                    blocks_data,
                    (size_x, size_y, size_z),
                    offset=(min_x, min_y, min_z),
                    block_entities=block_entities,
                )
            elif fmt == "schematic":
                data = await self.exporter.export_to_schematic(
                    blocks_data,
                    (size_x, size_y, size_z),
                    offset=(min_x, min_y, min_z),
                    block_entities=block_entities,
                )
            elif fmt == "mcworld":
                data = await self.exporter.export_to_mcworld(
                    blocks_data,
                    (size_x, size_y, size_z),
                    offset=(min_x, min_y, min_z),
                )

            # 保存文件
            await self.exporter.save_to_file(data, str(file_path))

            await self._send_feedback(
                f"§a导出完成! 文件: {filename} ({len(data)} 字节)"
            )

        except asyncio.CancelledError:
            await self._send_feedback("§e导出已停止")
            return
        except Exception as e:
            await self._send_feedback(f"§c导出失败: {e}")
            logger.error("导出失败", exc_info=True)

    async def _read_region(
        self,
        min_x: int,
        min_y: int,
        min_z: int,
        size_x: int,
        size_y: int,
        size_z: int,
    ) -> tuple[list[list[list[BlockState]]], dict]:
        """从游戏中读取区域方块。

        使用 /getblock 逐个读取方块。

        Args:
            min_x, min_y, min_z: 区域最小坐标
            size_x, size_y, size_z: 区域尺寸

        Returns:
            (blocks_data, block_entities) 元组
            blocks_data 是 3D 数组 [x][y][z]
            block_entities 是 {(x,y,z): nbt_dict}
        """
        blocks_data: list[list[list[BlockState]]] = []
        block_entities: dict[tuple[int, int, int], dict] = {}

        total = size_x * size_y * size_z
        read_count = 0
        last_read_count = 0
        last_update = time.monotonic()

        for x in range(size_x):
            yz_plane: list[list[BlockState]] = []
            for y in range(size_y):
                z_line: list[BlockState] = []
                for z in range(size_z):
                    if self._stop_event.is_set():
                        return blocks_data, block_entities

                    abs_x = min_x + x
                    abs_y = min_y + y
                    abs_z = min_z + z

                    # 使用 /getblock 读取方块 (需要控制台命令)
                    response = await self.sender.send_wo_command(
                        f"getblock {abs_x} {abs_y} {abs_z}"
                    )

                    block = self._parse_getblock_response(response)
                    z_line.append(block)
                    read_count += 1

                    # 进度显示 (每秒更新一次)
                    now = time.monotonic()
                    if now - last_update >= 1.0:
                        pct = read_count * 100 // total
                        # 计算自上次更新以来的增量, 避免把累计总数当成每秒速度
                        delta = read_count - last_read_count
                        speed = delta / (now - last_update + 0.001)
                        last_read_count = read_count
                        await self._send_feedback(
                            f"§e读取进度: {pct}% ({read_count}/{total}) "
                            f"速度: {speed:.0f}/s"
                        )
                        last_update = now

                yz_plane.append(z_line)
            blocks_data.append(yz_plane)

        return blocks_data, block_entities

    def _parse_getblock_response(self, response: Optional[str]) -> BlockState:
        """解析 /getblock 命令响应。

        Args:
            response: 命令响应文本

        Returns:
            BlockState 对象
        """
        if not response:
            return BlockState(name="minecraft:air")

        # 响应格式: "block_name [states_json]"
        # 例如: "minecraft:stone ["stone_type":"granite"]"
        # 或者: "minecraft:air"
        try:
            response = response.strip()
            if " " in response:
                name, states_str = response.split(" ", 1)
                states_str = states_str.strip("[]")
                states = {}
                if states_str:
                    for pair in states_str.split(","):
                        if ":" in pair:
                            k, v = pair.split(":", 1)
                            k = k.strip().strip('"')
                            v = v.strip().strip('"')
                            states[k] = v
                return BlockState(name=name.strip(), states=states)
            else:
                return BlockState(name=response)
        except Exception:
            return BlockState(name="minecraft:air")

    # ------------------------------------------------------------------
    # 备份/恢复
    # ------------------------------------------------------------------

    async def _handle_backup(self, cmd: dict) -> None:
        """处理备份命令。

        使用 /structure save 保存区域。
        """
        name = cmd["name"]
        x1, y1, z1 = cmd["x1"], cmd["y1"], cmd["z1"]
        x2, y2, z2 = cmd["x2"], cmd["y2"], cmd["z2"]

        structure_name = f"backup_{name}"

        await self._send_feedback(
            f"§e正在备份区域 ({x1},{y1},{z1})-({x2},{y2},{z2})..."
        )

        # 使用 /structure save (走控制台命令)
        result = await self.sender.send_wo_command(
            f'structure save "{structure_name}" {x1} {y1} {z1} {x2} {y2} {z2} true disk'
        )

        # 检查备份是否成功 (result 为 None 表示命令执行失败)
        if result is None:
            await self._send_feedback(f"§c备份失败: structure save 命令执行失败")
            return

        # 记录备份信息
        info = BackupInfo(
            name=name,
            x1=x1, y1=y1, z1=z1,
            x2=x2, y2=y2, z2=z2,
            structure_name=structure_name,
        )
        self._backups[name] = info

        await self._send_feedback(f"§a备份完成! 名称: {name}")

    async def _handle_restore(self, cmd: dict) -> None:
        """处理恢复命令。

        使用 /structure load 加载备份。
        """
        name = cmd["name"]

        info = self._backups.get(name)
        if info is None:
            await self._send_feedback(f"§c备份不存在: {name}")
            return

        # 恢复坐标 (默认使用原坐标)
        x = cmd.get("x", info.x1)
        y = cmd.get("y", info.y1)
        z = cmd.get("z", info.z1)

        await self._send_feedback(
            f"§e正在恢复备份 {name} 到 ({x},{y},{z})..."
        )

        # 使用 /structure load (走控制台命令)
        await self.sender.send_wo_command(
            f'structure load "{info.structure_name}" {x} {y} {z}'
        )

        await self._send_feedback(f"§a恢复完成! 名称: {name}")

    # ------------------------------------------------------------------
    # 列表/帮助
    # ------------------------------------------------------------------

    async def _handle_list(self) -> None:
        """列出可用建筑文件。"""
        files = self._list_building_files()
        if not files:
            await self._send_feedback("§e没有可用的建筑文件")
            return

        lines = ["§a=== 可用建筑文件 ==="]
        for i, f in enumerate(files[:20], 1):
            size_str = self._format_file_size(f["size"])
            lines.append(f"§f{i}. {f['name']} §7({size_str})")

        if len(files) > 20:
            lines.append(f"§7... 共 {len(files)} 个文件")

        await self._send_feedback("\n".join(lines))

    async def _handle_help(self) -> None:
        """显示帮助信息。"""
        help_text = (
            "§a=== 建筑控制台帮助 ===\n"
            "§f导入 <文件名> [x y z] §7- 导入建筑文件\n"
            "§f导出 <文件名> <x1 y1 z1> <x2 y2 z2> [格式] §7- 导出区域\n"
            "§f备份 <名称> <x1 y1 z1> <x2 y2 z2> §7- 备份区域\n"
            "§f恢复 <名称> [x y z] §7- 恢复备份\n"
            "§f列表 §7- 列出建筑文件\n"
            "§f停止 §7- 停止当前操作\n"
            "§f帮助 §7- 显示此帮助\n"
            "§7支持的格式: " + ", ".join(SUPPORTED_FORMATS) + "\n"
            "§7导出格式: " + ", ".join(EXPORT_FORMATS)
        )
        await self._send_feedback(help_text)

    async def _handle_stop(self) -> None:
        """停止当前操作。"""
        if self._current_task and not self._current_task.done():
            self._stop_event.set()
            self._current_task.cancel()
            try:
                await self._current_task
            except asyncio.CancelledError:
                pass
            self._current_task = None
            await self._send_feedback("§e操作已停止")
        else:
            await self._send_feedback("§7当前没有运行中的操作")

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _load_speed_settings(self) -> dict:
        """从 JSON 文件加载已保存的导入速度设置和 NBT 模式设置。

        读取 ``data/import_settings.json`` 文件,返回用户通过 API 配置的速度设置。
        如果文件不存在或解析失败,返回默认值。

        .. important::

            **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
            不能放附魔/自定义名字), 因此 ``nbt_mode`` 默认值已从 ``"replaceitem"``
            改为 ``"structure"`` (平台模式, 通过 structure save/load 搬运 NBT 方块)。

        Returns:
            包含速度和 NBT 模式设置的字典,键为:
            - import_engine: 导入引擎选择 (phoenix/retalcer/auto)
            - speed_preset: 速度预设 (slow/medium/fast/turbo/custom)
            - block_speed: 方块放置速度 (方块/秒)
            - command_speed: 命令方块加载速度 (命令/秒)
            - container_speed: 容器物品速度 (物品/秒)
            - nbt_delay: NBT 操作延迟 (秒)
            - group_wait: 组间等待时间 (秒)
            - nbt_mode: NBT 放置模式 (structure/replaceitem/auto)
                默认 "structure" (网易 3.8 推荐方案)
            - nbt_auto_detect: 是否启用自动检测 (仅在 auto 模式生效)
        """
        defaults = {
            "import_engine": "phoenix",
            "speed_preset": "medium",
            "block_speed": 20,
            "command_speed": 10,
            "container_speed": 5,
            "nbt_delay": 0.5,
            "group_wait": 1.0,
            # 网易 3.8 阉割了 replaceitem, 默认 "structure" (平台模式)
            "nbt_mode": "structure",
            "nbt_auto_detect": True,
        }

        settings_path = DATA_DIR / "import_settings.json"
        if not settings_path.is_file():
            return defaults

        try:
            with settings_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"读取速度设置文件失败,使用默认值: {e}")
            return defaults

        if not isinstance(data, dict):
            logger.warning("速度设置文件格式无效 (非字典),使用默认值")
            return defaults

        # 合并用户配置与默认值,确保所有键都存在
        result = dict(defaults)
        for key in defaults:
            if key in data and data[key] is not None:
                result[key] = data[key]
        return result

    def _find_file(self, filename: str) -> Optional[Path]:
        """查找建筑文件。

        Args:
            filename: 文件名 (可以是完整路径或相对名称)

        Returns:
            文件路径,未找到返回 None
        """
        # 尝试完整路径
        p = Path(filename)
        if p.is_file():
            return p

        # 在文件目录中查找
        search_path = self.file_dir / filename
        if search_path.is_file():
            return search_path

        # 添加扩展名尝试
        name_without_ext = Path(filename).stem
        for ext in BUILDING_EXTENSIONS:
            search_path = self.file_dir / f"{name_without_ext}{ext}"
            if search_path.is_file():
                return search_path

        # 模糊匹配
        for ext in BUILDING_EXTENSIONS:
            search_path = self.file_dir / f"{filename}{ext}"
            if search_path.is_file():
                return search_path

        return None

    def _list_building_files(self) -> list[dict]:
        """列出建筑文件目录中的所有建筑文件。

        Returns:
            文件信息列表 [{name, size, path}, ...]
        """
        files: list[dict] = []
        if not self.file_dir.is_dir():
            return files

        for entry in self.file_dir.iterdir():
            if entry.is_file() and entry.suffix.lower() in BUILDING_EXTENSIONS:
                files.append(
                    {
                        "name": entry.name,
                        "size": entry.stat().st_size,
                        "path": str(entry),
                    }
                )

        files.sort(key=lambda f: f["name"])
        return files

    def _format_file_size(self, size: int) -> str:
        """格式化文件大小。"""
        if size < 1024:
            return f"{size} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        else:
            return f"{size / (1024 * 1024):.1f} MB"

    def _extract_sign_text(self, nbt_data: dict) -> list[str]:
        """从 NBT 数据中提取告示牌文字。

        Args:
            nbt_data: 方块实体 NBT 数据

        Returns:
            4行文字列表
        """
        lines = []
        for key in ("Text", "Text2", "Text3", "Text4"):
            text = nbt_data.get(key, "")
            if isinstance(text, str):
                # 处理 JSON 文本格式
                if text.startswith("{"):
                    try:
                        import json

                        parsed = json.loads(text)
                        text = parsed.get("text", "")
                    except (json.JSONDecodeError, ValueError):
                        pass
                elif text.startswith('"') and text.endswith('"'):
                    text = text[1:-1]
            lines.append(str(text))
        return lines

    def _extract_container_items(self, nbt_data: dict) -> list[dict]:
        """从 NBT 数据中提取容器物品。

        Args:
            nbt_data: 方块实体 NBT 数据

        Returns:
            物品列表 [{slot, item_name, count, nbt}, ...]
        """
        items: list[dict] = []
        nbt_items = nbt_data.get("Items", [])
        if isinstance(nbt_items, list):
            for item in nbt_items:
                if isinstance(item, dict):
                    items.append(
                        {
                            "slot": item.get("Slot", 0),
                            "item_name": item.get("Name", "minecraft:air"),
                            "count": item.get("Count", 1),
                            "nbt": item.get("tag", {}),
                        }
                    )
        return items

    def _nbt_to_command_str(self, nbt_data: dict) -> str:
        """将 NBT 数据转换为命令字符串参数。

        Args:
            nbt_data: NBT 数据字典

        Returns:
            命令字符串中的 NBT 参数 (JSON 格式)
        """
        import json

        if not nbt_data:
            return ""

        # 转换为 JSON 字符串 (Bedrock 命令使用 JSON 格式的 NBT)
        try:
            return json.dumps(nbt_data, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""

    async def _send_feedback(self, message: str) -> None:
        """发送反馈消息。

        Args:
            message: 消息内容 (支持 Minecraft 颜色代码)
        """
        # 通过魔法指令发送 say 或 titleraw
        try:
            # 使用 say 命令发送反馈
            await self.sender.send_ai_command(f"say {message}")
        except Exception:
            pass

        # 同时调用回调
        if self._message_callback is not None:
            try:
                result = self._message_callback(message)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"消息回调失败: {e}")


# ----------------------------------------------------------------------
# 文件监视器
# ----------------------------------------------------------------------


class FileWatcher:
    """建筑文件目录监视器。

    监视文件目录的变化,当有新文件添加时通知控制台。

    Args:
        file_dir: 监视的文件目录
        callback: 文件变化回调
    """

    def __init__(
        self,
        file_dir: str,
        callback: Optional[Callable[[str, str], Any]] = None,
    ):
        self.file_dir = Path(file_dir)
        self.callback = callback
        self._known_files: set[str] = set()
        self._running = False

    async def start(self) -> None:
        """启动文件监视器。"""
        self._running = True
        self._scan_files()
        logger.info(f"文件监视器已启动: {self.file_dir}")

    async def stop(self) -> None:
        """停止文件监视器。"""
        self._running = False

    def _scan_files(self) -> None:
        """扫描当前文件列表。"""
        if not self.file_dir.is_dir():
            return
        self._known_files = {
            f.name
            for f in self.file_dir.iterdir()
            if f.is_file() and f.suffix.lower() in BUILDING_EXTENSIONS
        }

    async def check_changes(self) -> list[tuple[str, str]]:
        """检查文件变化。

        Returns:
            变化列表 [(事件类型, 文件名), ...]
            事件类型: "added" / "removed"
        """
        if not self.file_dir.is_dir():
            return []

        current_files = {
            f.name
            for f in self.file_dir.iterdir()
            if f.is_file() and f.suffix.lower() in BUILDING_EXTENSIONS
        }

        changes: list[tuple[str, str]] = []

        # 新增的文件
        for name in current_files - self._known_files:
            changes.append(("added", name))
            if self.callback:
                result = self.callback("added", name)
                if asyncio.iscoroutine(result):
                    await result

        # 删除的文件
        for name in self._known_files - current_files:
            changes.append(("removed", name))
            if self.callback:
                result = self.callback("removed", name)
                if asyncio.iscoroutine(result):
                    await result

        self._known_files = current_files
        return changes


# ----------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------

__all__ = [
    "EXPORT_FORMATS",
    "BUILDING_EXTENSIONS",
    "ImportConfig",
    "BackupInfo",
    "BuildingConsole",
    "FileWatcher",
    "NBTPlacementMode",
    "apply_speed_preset",
    "SPEED_PRESETS",
]
