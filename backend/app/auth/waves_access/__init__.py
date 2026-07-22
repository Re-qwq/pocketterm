"""WavesAccess - 机器人管理。

逆向自 NovaBuilder 的 WavesAccess 模块, 适配到 PocketTerm 项目。
提供机器人信息管理、Micro Omega 核心、玩家工具包和响应核心能力。

主要组件:
    - MicroOmega:          主控接口 (协调所有子系统)
    - ReactCore:           反应核心 (事件驱动反应系统)
    - PlayerKit:           玩家操作能力 (方块/容器/物品操作)
    - BotBasicInfoHolder:  机器人信息 (玩家/世界/位置信息)
"""

from __future__ import annotations

from .micro_omega import (
    MicroOmega, MicroOmegaConfig, MicroOmegaState,
)
from .react_core import (
    ReactCore, ReactRule, ReactTrigger, ReactAction,
)
from .player_kit import (
    PlayerKit, PlayerInfo, PlayerAction,
)
from .bot_info import (
    BotBasicInfoHolder, BotBasicInfo, WorldInfo,
)

__all__ = [
    # micro_omega
    "MicroOmega", "MicroOmegaConfig", "MicroOmegaState",
    # react_core
    "ReactCore", "ReactRule", "ReactTrigger", "ReactAction",
    # player_kit
    "PlayerKit", "PlayerInfo", "PlayerAction",
    # bot_info
    "BotBasicInfoHolder", "BotBasicInfo", "WorldInfo",
]
