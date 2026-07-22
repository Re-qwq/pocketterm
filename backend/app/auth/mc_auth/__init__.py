# PocketTerm - 网易 Minecraft 认证模块
from .client import AuthClient, AuthResult
from .device import DeviceFingerprint
from .exceptions import (
    AccountBannedError,
    AuthError,
    AuthenticationFailedError,
    InvalidCredentialsError,
    InvalidSauthJsonError,
    MaintenanceError,
    NetworkError,
    RateLimitError,
    ServerFullError,
    ServerNotFoundError,
    ServerRejectedError,
    VersionTooLowError,
)
from .sauth import SauthSession, build_sauth_json, parse_sauth_json

__all__ = [
    # 客户端
    "AuthClient",
    "AuthResult",
    # 设备
    "DeviceFingerprint",
    # sauth
    "SauthSession",
    "build_sauth_json",
    "parse_sauth_json",
    # 异常
    "AuthError",
    "NetworkError",
    "ServerRejectedError",
    "AccountBannedError",
    "InvalidSauthJsonError",
    "VersionTooLowError",
    "AuthenticationFailedError",
    "InvalidCredentialsError",
    "ServerFullError",
    "ServerNotFoundError",
    "RateLimitError",
    "MaintenanceError",
]
