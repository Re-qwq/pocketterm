"""PocketTerm configuration management.

This module is responsible for:

* Resolving the on-disk layout of the project (paths).
* Loading the YAML configuration file (``backend/config.yaml``).
* Auto-generating a default configuration on first run.
* Hashing and verifying user passwords using PBKDF2-HMAC-SHA256.

The password hash format is::

    pbkdf2_sha256$iterations$salt$hash

where ``iterations`` is the iteration count, ``salt`` is a hex-encoded random
salt and ``hash`` is the hex-encoded derived key.  PBKDF2 is used (rather than
bcrypt) to avoid the native build issues that ``bcrypt`` regularly introduces
across platforms.
"""

from __future__ import annotations

import copy
import hashlib
import secrets
from pathlib import Path
from typing import Any, Dict, Optional, Union

import yaml

# ---------------------------------------------------------------------------
# Path configuration
# ---------------------------------------------------------------------------
# This file lives at:  <PROJECT_ROOT>/backend/app/config.py
# Therefore:
#   _APP_DIR      = backend/app/        (parent of this file)
#   _BACKEND_DIR  = backend/            (parent of _APP_DIR)
#   _PROJECT_ROOT = PocketTerm/         (parent of _BACKEND_DIR)
_APP_DIR = Path(__file__).resolve().parent
_BACKEND_DIR = _APP_DIR.parent
_PROJECT_ROOT = _BACKEND_DIR.parent

#: Path to the ``app`` package directory:  ``backend/app/``
BASE_DIR: Path = _APP_DIR

#: Path to the project root directory:  ``PocketTerm/``
PROJECT_ROOT: Path = _PROJECT_ROOT

#: Path to the data directory:  ``backend/data/``
DATA_DIR: Path = _BACKEND_DIR / "data"

#: Path to the frontend directory:  ``PocketTerm/frontend/``
FRONTEND_DIR: Path = _PROJECT_ROOT / "frontend"

#: Path to the plugins directory:  ``PocketTerm/plugins/``
PLUGINS_DIR: Path = _PROJECT_ROOT / "plugins"

#: Path to the YAML configuration file:  ``backend/config.yaml``
CONFIG_FILE: Path = _BACKEND_DIR / "config.yaml"

#: Path to the default log file:  ``backend/data/pocketterm.log``
DEFAULT_LOG_FILE: Path = DATA_DIR / "pocketterm.log"

#: Default password for a freshly generated configuration.
DEFAULT_PASSWORD: str = "admin123"

#: PBKDF2 iteration count used when hashing passwords.
PBKDF2_ITERATIONS: int = 100_000

#: Hash algorithm name stored inside the password-hash string.
PBKDF2_ALGORITHM: str = "pbkdf2_sha256"

#: Underlying hashlib algorithm name passed to ``pbkdf2_hmac``.
PBKDF2_HASH_NAME: str = "sha256"

#: Salt size in bytes (serialised to hex -> 64 characters).
PBKDF2_SALT_SIZE: int = 32

#: Derived key length in bytes (serialised to hex -> 64 characters).
PBKDF2_KEY_LENGTH: int = 32

#: Placeholder used in the shipped ``config.yaml`` template for values that must
#: be generated uniquely on first run.
SECRET_PLACEHOLDER: str = "GENERATED_ON_FIRST_RUN"

#: Known hardcoded JWT secret shipped in earlier versions of ``config.yaml``.
#: If the loaded config still uses this value it MUST be regenerated to avoid
#: token forgery. See Bug 2 (默认弱凭据+硬编码JWT密钥).
KNOWN_HARDCODED_JWT_SECRET: str = (
    "8eaaa4fb1ff611818a45187f7afc8792518e2e7de1802f175d02f4a2774e40a9"
)


def generate_jwt_secret() -> str:
    """Generate a cryptographically secure JWT secret (64 hex characters)."""
    return secrets.token_hex(32)


def _is_valid_hash(stored_hash: str) -> bool:
    """Return ``True`` if ``stored_hash`` looks like a PBKDF2 password hash.

    Accepts the ``pbkdf2_sha256$iterations$salt_hex$hash_hex`` format produced
    by both this module and :mod:`app.auth.security`.
    """
    if not isinstance(stored_hash, str) or not stored_hash:
        return False
    parts = stored_hash.split("$")
    if len(parts) != 4:
        return False
    algorithm, iterations_str, salt_hex, hash_hex = parts
    if algorithm != PBKDF2_ALGORITHM:
        return False
    try:
        int(iterations_str)
        bytes.fromhex(salt_hex)
        bytes.fromhex(hash_hex)
    except ValueError:
        return False
    return bool(salt_hex) and bool(hash_hex)


# ---------------------------------------------------------------------------
# Password hashing helpers (PBKDF2-HMAC-SHA256)
# ---------------------------------------------------------------------------
# These functions intentionally mirror :mod:`app.auth.security` so that hashes
# produced by either module are interchangeable. The on-disk format is::
#
#     pbkdf2_sha256$iterations$salt_hex$hash_hex
#
# where ``salt_hex``/``hash_hex`` are the hex encoding of the raw 32-byte salt
# and the 32-byte derived key respectively.
def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Hash ``password`` using PBKDF2-HMAC-SHA256.

    Returns a string of the form ``pbkdf2_sha256$iterations$salt_hex$hash_hex``
    where ``salt_hex`` and ``hash_hex`` are hex encoded.
    """
    if not isinstance(password, str):
        raise TypeError("password must be a string")
    salt_bytes = secrets.token_bytes(PBKDF2_SALT_SIZE)
    salt_hex = salt_bytes.hex()
    derived = hashlib.pbkdf2_hmac(
        PBKDF2_HASH_NAME,
        password.encode("utf-8"),
        salt_bytes,
        iterations,
        dklen=PBKDF2_KEY_LENGTH,
    )
    return f"{PBKDF2_ALGORITHM}${iterations}${salt_hex}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify ``password`` against a previously generated ``stored_hash``.

    Uses :func:`secrets.compare_digest` for a constant-time comparison. Any
    malformed ``stored_hash`` returns ``False`` instead of raising.
    """
    if not stored_hash or not password:
        return False
    if not stored_hash.startswith(PBKDF2_ALGORITHM + "$"):
        return False
    parts = stored_hash.split("$")
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
        PBKDF2_HASH_NAME,
        password.encode("utf-8"),
        salt_bytes,
        iterations,
        dklen=len(expected) or PBKDF2_KEY_LENGTH,
    )
    return secrets.compare_digest(derived, expected)


def needs_rehash(stored_hash: str, iterations: int = PBKDF2_ITERATIONS) -> bool:
    """Return ``True`` if ``stored_hash`` should be re-hashed (weak iterations)."""
    try:
        algorithm, iterations_str, _salt, _hash = stored_hash.split("$")
    except (ValueError, AttributeError):
        return True
    if algorithm != PBKDF2_ALGORITHM:
        return True
    try:
        return int(iterations_str) < iterations
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Default configuration template
# ---------------------------------------------------------------------------
def _default_config() -> Dict[str, Any]:
    """Return a fresh copy of the default configuration dictionary.

    A new random JWT secret and a freshly hashed default password are generated
    on every call so that each installation gets unique credentials.
    """
    return {
        "server": {
            "host": "0.0.0.0",
            "port": 8000,
        },
        "security": {
            "username": "admin",
            "password_hash": hash_password(DEFAULT_PASSWORD),
            "jwt_secret": secrets.token_hex(32),
            "max_login_attempts": 5,
            "lockout_duration": 300,
        },
        "auth_server": {
            "url": "https://nv1.nethard.pro",
        },
        "bot": {
            "name_prefix": "Bot",
            "game_version": "1.21.93",
            "device_os": "Android",
            "device_model": "Samsung Galaxy S21",
            "max_bots": 10,
        },
        "access_point": {
            "default": "wlan0",
        },
        "network": {
            # 是否校验 HTTPS 证书。生产环境应设为 true。
            # 网易 API 的证书有时不被系统信任, 因此默认关闭。
            "verify_ssl": False,
        },
        "plugins": {
            "directory": str(PLUGINS_DIR),
            "auto_load": True,
        },
        "logging": {
            "level": "INFO",
            "file": str(DEFAULT_LOG_FILE),
        },
    }


# ---------------------------------------------------------------------------
# Deep-merge helper
# ---------------------------------------------------------------------------
def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``override`` into ``base`` and return the result.

    Values in ``override`` take precedence. Dicts are merged recursively; for
    every other type the ``override`` value replaces the ``base`` value.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Configuration object
# ---------------------------------------------------------------------------
class Config:
    """Application configuration loaded from (and persisted to) a YAML file.

    On construction the configuration file is read. If it does not exist a
    default configuration is generated and written to disk so that the very
    first run produces a sane, editable ``config.yaml``.
    """

    def __init__(self, config_path: Optional[Union[str, Path]] = None) -> None:
        self.config_path: Path = Path(config_path) if config_path else CONFIG_FILE
        self._data: Dict[str, Any] = {}
        self.load()

    # -- loading / saving -------------------------------------------------
    def load(self) -> Dict[str, Any]:
        """Load configuration from disk, generating defaults if absent."""
        if not self.config_path.exists():
            self._data = _default_config()
            self.save()
            return self._data

        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
        except yaml.YAMLError as exc:
            # A corrupt config should not crash the whole app: fall back to
            # the defaults but keep the broken file untouched so the user can
            # repair it.
            print(f"[config] Failed to parse {self.config_path}: {exc}")
            self._data = _default_config()
            return self._data

        if not isinstance(loaded, dict):
            loaded = {}

        # Merge against defaults so new keys added in upgrades are present.
        self._data = _deep_merge(_default_config(), loaded)

        # Make sure the security secrets actually exist: a hand-written or
        # template config.yaml may ship with empty/placeholder values that we
        # must fill in on first load.
        self._ensure_secrets()

        # Persist the merged result back so the file always contains every key.
        self.save()
        return self._data

    def _ensure_secrets(self) -> None:
        """Generate password_hash / jwt_secret if they are missing or placeholder.

        This lets us ship a ``config.yaml`` template with placeholder values
        (``GENERATED_ON_FIRST_RUN`` or empty) while still producing a unique,
        working set of secrets the first time the app reads the file.

        Additionally, if the ``jwt_secret`` still matches the known hardcoded
        value shipped in earlier versions, it is regenerated to prevent token
        forgery (Bug 2). The password hash is NOT touched if it is already a
        valid hash, since the user may have already changed the password.
        """
        security = self._data.setdefault("security", {})
        password_hash = security.get("password_hash", "")
        if (
            not password_hash
            or password_hash == SECRET_PLACEHOLDER
            or not _is_valid_hash(password_hash)
        ):
            security["password_hash"] = hash_password(DEFAULT_PASSWORD)
        jwt_secret = security.get("jwt_secret", "")
        if (
            not jwt_secret
            or jwt_secret == SECRET_PLACEHOLDER
            or jwt_secret == KNOWN_HARDCODED_JWT_SECRET
        ):
            security["jwt_secret"] = generate_jwt_secret()

    def save(self) -> None:
        """Persist the current configuration to ``config_path``."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as handle:
            yaml.safe_dump(
                self._data,
                handle,
                default_flow_style=False,
                allow_unicode=True,
                sort_keys=False,
            )

    # -- access helpers ---------------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        """Retrieve a nested configuration value.

        Example::

            config.get("server", "port")          # -> 8000
            config.get("security", "jwt_secret")  # -> "<hex>"
            config.get("missing", "key", default="fallback")
        """
        node: Any = self._data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def set(self, *keys_and_value: Any) -> None:
        """Set a nested configuration value.

        The final positional argument is the value to store, every preceding
        argument is a key path. Example::

            config.set("server", "port", 9000)
        """
        if len(keys_and_value) < 2:
            raise ValueError("set() requires at least one key and a value")
        *keys, value = keys_and_value
        node = self._data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    def section(self, name: str) -> Dict[str, Any]:
        """Return a whole top-level configuration section as a dict."""
        section = self._data.get(name)
        if not isinstance(section, dict):
            return {}
        return copy.deepcopy(section)

    def as_dict(self) -> Dict[str, Any]:
        """Return a deep copy of the entire configuration dictionary."""
        return copy.deepcopy(self._data)

    # -- convenience properties ------------------------------------------
    @property
    def server(self) -> Dict[str, Any]:
        return self.section("server")

    @property
    def host(self) -> str:
        return self.get("server", "host", default="0.0.0.0")

    @property
    def port(self) -> int:
        return int(self.get("server", "port", default=8000))

    @property
    def security(self) -> Dict[str, Any]:
        return self.section("security")

    @property
    def username(self) -> str:
        return self.get("security", "username", default="admin")

    @property
    def password_hash(self) -> str:
        return self.get("security", "password_hash", default="")

    @property
    def jwt_secret(self) -> str:
        return self.get("security", "jwt_secret", default="")

    @property
    def max_login_attempts(self) -> int:
        return int(self.get("security", "max_login_attempts", default=5))

    @property
    def lockout_duration(self) -> int:
        return int(self.get("security", "lockout_duration", default=300))

    @property
    def auth_server_url(self) -> str:
        return self.get("auth_server", "url", default="")

    @property
    def verify_ssl(self) -> bool:
        """是否校验 HTTPS 证书。

        对应 ``network.verify_ssl`` 配置项。生产环境应设为 ``True``。
        默认 ``False`` (网易 API 证书有时不被系统信任)。
        """
        return bool(self.get("network", "verify_ssl", default=False))

    @property
    def bot(self) -> Dict[str, Any]:
        return self.section("bot")

    @property
    def plugins(self) -> Dict[str, Any]:
        return self.section("plugins")

    @property
    def log_level(self) -> str:
        return self.get("logging", "level", default="INFO")

    @property
    def log_file(self) -> Path:
        raw = self.get("logging", "file", default=str(DEFAULT_LOG_FILE))
        path = Path(raw)
        if not path.is_absolute():
            path = _BACKEND_DIR / path
        return path

    # -- password helpers -------------------------------------------------
    def check_password(self, password: str) -> bool:
        """Return ``True`` if ``password`` matches the configured hash."""
        return verify_password(password, self.password_hash)

    def set_password(self, password: str, iterations: int = PBKDF2_ITERATIONS) -> None:
        """Hash and store a new password, then persist the configuration."""
        self.set("security", "password_hash", hash_password(password, iterations))
        self.save()

    # -- repr -------------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Config(path={self.config_path!s})"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
_config_instance: Optional[Config] = None


def get_config(reload: bool = False) -> Config:
    """Return the shared :class:`Config` singleton.

    The configuration is loaded once and cached. Pass ``reload=True`` to force
    a fresh read from disk (useful after the user edits ``config.yaml``).
    """
    global _config_instance
    if _config_instance is None or reload:
        _config_instance = Config()
    return _config_instance


def ensure_directories() -> None:
    """Create the runtime directories used by the application."""
    for directory in (DATA_DIR, PLUGINS_DIR):
        directory.mkdir(parents=True, exist_ok=True)


__all__ = [
    # paths
    "BASE_DIR",
    "PROJECT_ROOT",
    "DATA_DIR",
    "FRONTEND_DIR",
    "PLUGINS_DIR",
    "CONFIG_FILE",
    "DEFAULT_LOG_FILE",
    # password helpers
    "hash_password",
    "verify_password",
    "needs_rehash",
    "DEFAULT_PASSWORD",
    "PBKDF2_ITERATIONS",
    "PBKDF2_ALGORITHM",
    "PBKDF2_HASH_NAME",
    "PBKDF2_SALT_SIZE",
    "PBKDF2_KEY_LENGTH",
    "SECRET_PLACEHOLDER",
    "KNOWN_HARDCODED_JWT_SECRET",
    "generate_jwt_secret",
    # config
    "Config",
    "get_config",
    "ensure_directories",
]
