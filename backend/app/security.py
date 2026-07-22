"""PocketTerm 安全模块。

功能:
    - 密码哈希 (bcrypt)
    - JWT Token 生成/验证
    - 卡密验证
    - 请求频率限制
    - CSRF Token
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import Any, Optional

logger = logging.getLogger("pocketterm.security")

# ============================================================================
# 密码哈希 (PBKDF2-HMAC-SHA256, 兼容旧系统)
# ============================================================================

_PBKDF2_ITERATIONS = 100000


def hash_password(password: str) -> str:
    """哈希密码，格式: pbkdf2_sha256$iterations$salt$hash"""
    import base64
    import os
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码。"""
    try:
        if stored_hash.startswith("pbkdf2_sha256$"):
            parts = stored_hash.split("$")
            if len(parts) != 4:
                return False
            iterations = int(parts[1])
            salt = bytes.fromhex(parts[2])
            expected = parts[3]
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
            return hmac.compare_digest(dk.hex(), expected)
        # 兼容旧格式
        return False
    except Exception:
        return False


# ============================================================================
# JWT Token
# ============================================================================

#: 开发环境默认 JWT 密钥（仅用于本地测试，生产环境必须设置环境变量）
_DEV_JWT_SECRET: str = "dev-only-insecure-jwt-secret-do-not-use-in-production"


def _load_jwt_secret() -> str:
    """从环境变量 ``POCKETTERM_JWT_SECRET`` 加载 JWT 密钥。

    加载规则:

        - 若环境变量已设置且非空，直接使用该值。
        - 若环境变量未设置:
            * 生产环境 (``POCKETTERM_ENV=production``) 抛出 ``RuntimeError``，
              拒绝启动，强制要求显式配置密钥。
            * 开发环境使用内置的弱默认密钥并打印警告日志。

    Returns:
        JWT 签名密钥字符串。
    """
    import os

    secret = os.environ.get("POCKETTERM_JWT_SECRET", "").strip()
    if secret:
        return secret

    env = os.environ.get("POCKETTERM_ENV", "").strip().lower()
    if env == "production":
        raise RuntimeError(
            "生产环境 (POCKETTERM_ENV=production) 必须设置环境变量 "
            "POCKETTERM_JWT_SECRET，拒绝以弱密钥启动。"
        )

    logger.warning(
        "未设置 POCKETTERM_JWT_SECRET 环境变量，开发环境使用弱默认密钥。"
        "生产环境部署前请务必设置 POCKETTERM_JWT_SECRET。"
    )
    return _DEV_JWT_SECRET


_JWT_SECRET: str = _load_jwt_secret()


def set_jwt_secret(secret: str) -> None:
    """设置 JWT 密钥。

    .. deprecated::
        JWT 密钥现在优先从 ``POCKETTERM_JWT_SECRET`` 环境变量读取。
        此函数保留用于向后兼容 (例如测试中动态替换密钥)，但不应在生产
        环境中用于覆盖环境变量配置的密钥。
    """
    global _JWT_SECRET
    _JWT_SECRET = secret


def create_jwt_token(payload: dict, expires_in: int = 86400) -> str:
    """创建 JWT Token (HS256)。"""
    import base64

    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + expires_in}

    def _b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    header_b64 = _b64(json.dumps(header, separators=(",", ":")).encode())
    body_b64 = _b64(json.dumps(body, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{body_b64}".encode()
    sig = hmac.new(_JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    sig_b64 = _b64(sig)
    return f"{header_b64}.{body_b64}.{sig_b64}"


def verify_jwt_token(token: str) -> Optional[dict]:
    """验证 JWT Token，返回 payload 或 None。"""
    import base64

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, body_b64, sig_b64 = parts

        def _unb64(s: str) -> bytes:
            padding = 4 - len(s) % 4
            if padding != 4:
                s += "=" * padding
            return base64.urlsafe_b64decode(s)

        signing_input = f"{header_b64}.{body_b64}".encode()
        expected_sig = hmac.new(_JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
        actual_sig = _unb64(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        body = json.loads(_unb64(body_b64))
        if body.get("exp", 0) < time.time():
            return None
        return body
    except Exception:
        return None


# ============================================================================
# CSRF Token
# ============================================================================

_csrf_tokens: dict[str, float] = {}  # token -> expire_time


def create_csrf_token(expires_in: int = 3600) -> str:
    """创建 CSRF Token。"""
    token = secrets.token_hex(16)
    _csrf_tokens[token] = time.time() + expires_in
    # 清理过期 token
    now = time.time()
    expired = [k for k, v in _csrf_tokens.items() if v < now]
    for k in expired:
        del _csrf_tokens[k]
    return token


def verify_csrf_token(token: str) -> bool:
    """验证 CSRF Token。"""
    expire = _csrf_tokens.get(token)
    if expire is None or expire < time.time():
        return False
    return True


# ============================================================================
# 请求频率限制
# ============================================================================

class RateLimiter:
    """简单的内存频率限制器。"""

    def __init__(self):
        self._requests: dict[str, list[float]] = {}

    def check(self, key: str, max_requests: int, window: int) -> bool:
        """检查是否允许请求。"""
        now = time.time()
        if key not in self._requests:
            self._requests[key] = []
        # 清理过期记录
        self._requests[key] = [t for t in self._requests[key] if t > now - window]
        if len(self._requests[key]) >= max_requests:
            return False
        self._requests[key].append(now)
        return True


rate_limiter = RateLimiter()


# ============================================================================
# 异常行为检测
# ============================================================================

_suspicious_count: dict[str, int] = {}  # user_id/ip -> count
_suspended_users: set[str] = set()


def report_suspicious(identifier: str) -> int:
    """报告可疑行为，返回累计次数。"""
    _suspicious_count[identifier] = _suspicious_count.get(identifier, 0) + 1
    return _suspicious_count[identifier]


def is_suspended(identifier: str) -> bool:
    """检查是否被暂停。"""
    return identifier in _suspended_users


def suspend(identifier: str) -> None:
    """暂停用户。"""
    _suspended_users.add(identifier)


def unsuspend(identifier: str) -> None:
    """恢复用户。"""
    _suspended_users.discard(identifier)
    _suspicious_count.pop(identifier, None)


def get_suspicion_level(identifier: str) -> int:
    """获取可疑等级 (0-3)。"""
    count = _suspicious_count.get(identifier, 0)
    if count == 0:
        return 0
    elif count < 3:
        return 1  # 低
    elif count < 5:
        return 2  # 中
    else:
        return 3  # 高


def check_anomaly(identifier: str, action: str = "") -> dict:
    """检查异常行为，返回 {suspended: bool, level: int, reason: str}。"""
    level = get_suspicion_level(identifier)
    if level >= 3:
        suspend(identifier)
        return {
            "suspended": True,
            "level": level,
            "reason": f"异常行为检测: 累计 {level} 次可疑操作",
        }
    return {"suspended": False, "level": level, "reason": ""}
