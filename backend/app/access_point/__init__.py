# PocketTerm - 接入点管理模块

# 导入主要类
from .base import (
    AccessPoint,
    AccessPointInfo,
    AccessPointStatus,
    AccessPointError,
    NetworkError,
    AccountBannedError,
    Colors,
)
from .pure_python import PurePythonAccessPoint
from .custom import CustomAccessPoint

# 条件导入（可能依赖外部二进制）
try:
    from .neomega import NeOmegaAccessPoint
except ImportError:
    NeOmegaAccessPoint = None  # type: ignore

try:
    from .fateark import FateArkAccessPoint
except ImportError:
    FateArkAccessPoint = None  # type: ignore

__all__ = [
    "AccessPoint",
    "AccessPointInfo",
    "AccessPointStatus",
    "AccessPointError",
    "NetworkError",
    "AccountBannedError",
    "Colors",
    "PurePythonAccessPoint",
    "CustomAccessPoint",
    "NeOmegaAccessPoint",
    "FateArkAccessPoint",
]
