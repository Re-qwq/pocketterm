"""PocketTerm 网易认证异常类

所有异常均继承自 ``AuthError``。调用方可根据具体异常类型进行差异化处理
(例如账号封禁、版本过低、服务器满员等)。
"""
from __future__ import annotations


class AuthError(Exception):
    """认证基础异常,所有 mc_auth 模块异常的基类。"""
    pass


class NetworkError(AuthError):
    """网络错误(连接超时、DNS 失败、TCP 重置等)。"""
    pass


class ServerRejectedError(AuthError):
    """服务器拒绝请求,携带 HTTP 状态码。"""

    def __init__(self, message: str, http_status: int = 0):
        super().__init__(message)
        self.http_status = http_status


class AccountBannedError(AuthError):
    """账号已被封禁。"""
    pass


class InvalidSauthJsonError(AuthError):
    """sauth_json 构造或解析失败。"""
    pass


class VersionTooLowError(AuthError):
    """客户端版本过低,服务器拒绝登录。"""
    pass


class AuthenticationFailedError(AuthError):
    """认证失败(通用,未匹配到更具体的异常时使用)。"""
    pass


class InvalidCredentialsError(AuthError):
    """凭证无效(服务器号/密码/API Key 错误)。"""
    pass


class ServerFullError(AuthError):
    """服务器已满,无法登录。"""
    pass


class ServerNotFoundError(AuthError):
    """服务器不存在或已下线。"""
    pass


class RateLimitError(AuthError):
    """请求频率超限,被限流。"""
    pass


class MaintenanceError(AuthError):
    """服务器维护中,暂时不可用。"""
    pass
