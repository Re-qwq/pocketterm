"""纯 Python 网易 Minecraft Bedrock 协议实现。

本包提供不依赖任何外部二进制 (NeOmega / FateArk / Go) 的纯 Python 协议实现,
包括:

- Varint 编码 (protocol/varint.py)
- NBT 编解码 (protocol/nbt.py) — 12 种 Tag 类型 + 4 种字节序
- RakNet UDP 协议 (protocol/raknet.py) — 连接、握手、分片重组
- JWT 登录链 (protocol/jwt_chain.py) — 身份声明、客户端数据
- 压缩 (protocol/compression.py) — 网易 flate/zlib 压缩
- 连接管理 (protocol/connection.py) — 完整登录流程
- 命令系统 (protocol/commands.py) — 常用 Minecraft 命令封装
- 游戏事件监听 (protocol/game_events.py) — 类似 ToolDelta 的事件监听
- 聊天命令系统 (protocol/chat_commands.py) — 游戏内聊天命令解析
- 事件管理器 (protocol/event_manager.py) — 事件系统顶层集成
- NBT 方块放置器 (protocol/nbt_placer.py) — 双模式 NBT 放置
  (网易 3.8 默认 STRUCTURE 平台模式, 11x11 海晶灯平台 + structure save/load)
- 建筑控制台 (protocol/console.py) — 导入/导出/备份/恢复集成
- PhoenixBuilder NBT 容器处理 (protocol/phoenix_nbt.py) — NBT 容器处理系统
- PhoenixBuilder BDump 引擎 (protocol/phoenix_builder.py) — BDump 引擎和构建器
- PhoenixBuilder Omega 导入管线 (protocol/phoenix_omega.py) — Omega 导入管线
- LinkConnection 加密协议栈 (protocol/link_connection.py) — RSA+ChaCha8 端到端加密 (NovaBuilder 逆向)

.. important::

    **网易 3.8 阉割了 replaceitem 命令** (只能放耐久/特殊值/数量/NBT标签,
    不能放附魔/自定义名字), 因此 NBT 放置默认使用 STRUCTURE 平台模式
    (11x11 海晶灯平台, 逆向自 NovaBuilder/NexusE)。
    replaceitem 模式仅作为可选保留 (用户明确知道 3.8 风险时可选)。

逆向来源:
    - NovaBuilder_windows_amd64.exe (PhoenixBuilder/StarShuttler)
    - NexusE (NexusEgo v1.6.5) — nbt_assigner 模块 (FindOrGenerateNewAnvil 等)
    - DependencyLibrary-main (neomega-core + FateArk)
    - Drug.NetEase (pc进pe源码)
    - CYXHSJ 永久Cookies获取.exe
    - ToolDelta 插件框架 (事件监听机制)
"""

from __future__ import annotations

__version__ = "0.1.0"

from .multi_chunk_importer import MultiChunkImporter, ImportConfig as MultiChunkImportConfig
from .import_options import ImportOptions, ImportAlgorithm
from .cdump_parser import CDumpParser
from .pixel_art_importer import PixelArtImporter

# 新增模块导出 (逆向工程集成)
from . import nbt_handler
from . import block_mapping
from . import import_algorithms
from . import command_systems
from . import mapbuilder
from . import midi_converter