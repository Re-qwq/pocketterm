"""安全模块 - 密码哈希、JWT、登录保护

提供以下能力:
- PBKDF2-HMAC-SHA256 密码哈希与验证
- 基于 python-jose 的 JWT 令牌创建与验证
- LoginAttemptTracker 登录尝试追踪器(防暴力破解)
- 全局 login_tracker 实例
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

# ---------------------------------------------------------------------------
# JWT 配置
# ---------------------------------------------------------------------------
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24  # 24 小时

# ---------------------------------------------------------------------------
# PBKDF2 配置
# ---------------------------------------------------------------------------
PBKDF2_ALGORITHM = "sha256"
PBKDF2_ITERATIONS = 100000
PBKDF2_SALT_SIZE = 32          # 字节数,序列化为 hex 后为 64 字符
PBKDF2_KEY_LENGTH = 32         # 派生密钥长度(字节),hex 后 64 字符
PBKDF2_FORMAT_PREFIX = "pbkdf2_sha256"


# ---------------------------------------------------------------------------
# 密码哈希
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    """使用 PBKDF2-HMAC-SHA256 哈希密码。

    返回格式: ``pbkdf2_sha256$100000$salt_hex$hash_hex``
    其中:
      - ``100000`` 为迭代次数
      - ``salt_hex`` 为 64 字符的十六进制盐值
      - ``hash_hex`` 为 64 字符的十六进制派生密钥
    """
    salt_bytes = secrets.token_bytes(PBKDF2_SALT_SIZE)
    salt_hex = salt_bytes.hex()
    derived = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt_bytes,
        PBKDF2_ITERATIONS,
        dklen=PBKDF2_KEY_LENGTH,
    )
    return f"{PBKDF2_FORMAT_PREFIX}${PBKDF2_ITERATIONS}${salt_hex}${derived.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码是否与已哈希的密码匹配。

    支持验证 ``pbkdf2_sha256$iterations$salt_hex$hash_hex`` 格式。
    任何解析或验证异常都会返回 ``False``(常量时间比较)。
    """
    if not hashed_password or not plain_password:
        return False

    if not hashed_password.startswith(PBKDF2_FORMAT_PREFIX + "$"):
        # 不支持的哈希格式
        return False

    parts = hashed_password.split("$")
    # pbkdf2_sha256$iterations$salt_hex$hash_hex -> 4 段
    if len(parts) != 4:
        return False

    _, iterations_str, salt_hex, hash_hex = parts
    try:
        iterations = int(iterations_str)
        salt_bytes = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False

    derived = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        plain_password.encode("utf-8"),
        salt_bytes,
        iterations,
        dklen=len(expected) or PBKDF2_KEY_LENGTH,
    )
    return hmac.compare_digest(derived, expected)


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def generate_jwt_secret() -> str:
    """生成随机 JWT 密钥(用于首次初始化)。"""
    return secrets.token_urlsafe(48)


def create_access_token(
    data: dict,
    secret_key: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """创建 JWT 访问令牌。

    Args:
        data: 需要写入 payload 的声明(如 ``{"sub": username}``)
        secret_key: JWT 签名密钥
        expires_delta: 自定义过期时长;未指定则使用 ``ACCESS_TOKEN_EXPIRE_MINUTES``
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, secret_key, algorithm=ALGORITHM)


def verify_token(token: str, secret_key: str) -> Optional[dict]:
    """验证 JWT 令牌,成功返回 payload 字典,失败返回 ``None``。"""
    try:
        payload = jwt.decode(token, secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# 登录尝试追踪器
# ---------------------------------------------------------------------------
class LoginAttemptTracker:
    """登录尝试追踪器 - 防止暴力破解。

    每个 IP 在达到 ``max_attempts`` 次失败后被锁定 ``lockout_minutes`` 分钟。
    记录每个 IP 的:失败次数、首次失败时间、锁定到期时间戳。
    """

    def __init__(self, max_attempts: int = 5, lockout_minutes: int = 15):
        self.max_attempts = max_attempts
        self.lockout_minutes = lockout_minutes
        # ip -> {"count": int, "first_attempt": float, "locked_until": float}
        self._attempts: dict[str, dict] = {}

    def is_locked(self, ip: str) -> tuple[bool, float]:
        """检查 IP 是否被锁定。

        Returns:
            (是否锁定, 剩余锁定秒数)
        """
        record = self._attempts.get(ip)
        if not record:
            return False, 0.0

        locked_until = record.get("locked_until", 0.0)
        now = time.time()
        if locked_until > now:
            return True, locked_until - now

        # 锁定时间已过,清理记录
        if locked_until and locked_until <= now:
            del self._attempts[ip]
        return False, 0.0

    def record_failure(self, ip: str) -> tuple[int, float]:
        """记录一次失败登录尝试。

        Returns:
            (当前失败次数, 锁定剩余秒数; 未锁定则为 0)
        """
        now = time.time()
        record = self._attempts.get(ip)

        if not record:
            record = {"count": 0, "first_attempt": now, "locked_until": 0.0}
            self._attempts[ip] = record

        # 距离首次失败超过 1 小时,重置计数窗口
        if now - record["first_attempt"] > 3600:
            record = {"count": 0, "first_attempt": now, "locked_until": 0.0}
            self._attempts[ip] = record

        record["count"] += 1

        # 达到最大失败次数 -> 锁定
        if record["count"] >= self.max_attempts:
            lockout_seconds = self.lockout_minutes * 60
            record["locked_until"] = now + lockout_seconds
            return record["count"], lockout_seconds

        return record["count"], 0.0

    def record_success(self, ip: str) -> None:
        """记录成功登录,清除该 IP 的失败记录。"""
        self._attempts.pop(ip, None)

    def get_attempts(self, ip: str) -> int:
        """获取当前 IP 的失败次数。"""
        record = self._attempts.get(ip)
        if not record:
            return 0
        return record.get("count", 0)

    def get_lockout_until(self, ip: str) -> float:
        """获取 IP 的锁定到期时间戳(未锁定则为 0)。"""
        record = self._attempts.get(ip)
        if not record:
            return 0.0
        return record.get("locked_until", 0.0)


# 全局登录追踪器实例
# max_attempts 从 config.max_login_attempts 读取，使 attempts_left 计算
# (auth.py 中使用 config.max_login_attempts) 与实际锁定阈值保持同步。
# 读取配置失败时回退到默认值 (5 次失败锁定 15 分钟)。
def _create_login_tracker() -> "LoginAttemptTracker":
    try:
        # 延迟导入避免在 config 尚未就绪时引发循环依赖
        from ..config import get_config as _get_config
        _cfg = _get_config()
        return LoginAttemptTracker(
            max_attempts=_cfg.max_login_attempts,
            lockout_minutes=15,
        )
    except Exception:  # noqa: BLE001
        return LoginAttemptTracker(max_attempts=5, lockout_minutes=15)


login_tracker = _create_login_tracker()
