"""PocketTerm 认证与防封禁模块。

模块组织
--------

- :mod:`anti_ban`             -- 基础防封禁 (JitterDelay / RateLimit / Behavior)
- :mod:`anti_ban_enhanced`     -- 增强防封禁 (ToolDelta / NexusE 逆向策略)
- :mod:`auto_connect`          -- 自动化连接 (MCPC / 重连 / 监控)
- :mod:`device_fingerprint`    -- 设备指纹管理 (uqholder 风格)
- :mod:`security`              -- 安全工具
- :mod:`mc_auth`               -- MC 认证
- :mod:`netease_direct`        -- 网易直连认证
"""
from .anti_ban import (
    AntiBanConfig,
    AntiBanController,
    get_anti_ban_controller,
    reset_anti_ban_controller,
)
from .anti_ban_enhanced import (
    EnhancedAntiBan,
    EnhancedAntiBanConfig,
    get_enhanced_anti_ban,
    reset_enhanced_anti_ban,
    safe_writer,
)
from .auto_connect import (
    AutoConnectManager,
    ConnectionMonitor,
    ConnectionState,
    MCPCChallenge,
    MCPCChallengeHandler,
    MCPCChallengeType,
    ReconnectPolicy,
    get_auto_connect_manager,
    remove_auto_connect_manager,
)
from .device_fingerprint import (
    DEFAULT_BUILD_PLATFORM,
    DEFAULT_GAME_VERSION,
    BuildPlatform,
    DeviceFingerprint,
    DeviceFingerprintManager,
    InputMode,
    UIProfile,
    get_fingerprint_manager,
    reset_fingerprint_manager,
)
from .uqholder import (
    UQHolder,
    BotBasicInfo,
    PlayerRecord,
)
from .heartbeat_monitor import (
    BusinessHeartbeat,
    HeartbeatConfig,
)
from .reconnect_fsm import (
    ReconnectFSM,
    ReconnectLayer,
    ReconnectReason,
)
from .mcpc_solver import (
    MCPCChallengeSolver,
    OperatorChallengeMonitor as MCPCOperatorChallengeMonitor,
    PostponeActionQueue as MCPCPostponeActionQueue,
)

# 新增模块导出 (逆向工程集成)
from .postpone_actions import PostponeAction, PostponeActions
from .rate_limiter import RateLimiter, RateLimitConfig
from .humanize_command import HumanizeConfig, HumanizeResult, HumanizeStrategy
from . import starshuttler
from . import waves_access

__all__ = [
    # 基础防封禁
    "AntiBanConfig",
    "AntiBanController",
    "get_anti_ban_controller",
    "reset_anti_ban_controller",
    # 增强防封禁 (ToolDelta / NexusE)
    "EnhancedAntiBan",
    "EnhancedAntiBanConfig",
    "get_enhanced_anti_ban",
    "reset_enhanced_anti_ban",
    "safe_writer",
    # 自动化连接
    "AutoConnectManager",
    "ConnectionMonitor",
    "ConnectionState",
    "MCPCChallenge",
    "MCPCChallengeHandler",
    "MCPCChallengeType",
    "ReconnectPolicy",
    "get_auto_connect_manager",
    "remove_auto_connect_manager",
    # 设备指纹
    "DEFAULT_BUILD_PLATFORM",
    "DEFAULT_GAME_VERSION",
    "BuildPlatform",
    "DeviceFingerprint",
    "DeviceFingerprintManager",
    "InputMode",
    "UIProfile",
    "get_fingerprint_manager",
    "reset_fingerprint_manager",
    # UQHolder 设备指纹持久化
    "UQHolder",
    "BotBasicInfo",
    "PlayerRecord",
    # 业务心跳监控
    "BusinessHeartbeat",
    "HeartbeatConfig",
    # 重连 FSM
    "ReconnectFSM",
    "ReconnectLayer",
    "ReconnectReason",
    # MCPC 挑战求解
    "MCPCChallengeSolver",
    "MCPCOperatorChallengeMonitor",
    "MCPCPostponeActionQueue",
    # 逆向工程新增
    "PostponeAction",
    "PostponeActions",
    "RateLimiter",
    "RateLimitConfig",
    "HumanizeConfig",
    "HumanizeResult",
    "HumanizeStrategy",
    "starshuttler",
    "waves_access",
]
