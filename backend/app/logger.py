"""PocketTerm logging facility.

Provides a pre-configured :class:`logging.Logger` that writes to several sinks:

* a **colored console handler** (via :mod:`colorlog`) for interactive use, and
* **rotating file handlers** writing to ``backend/data/``:

  - ``pocketterm.log`` -- captures every level (DEBUG and above);
  - ``info.log`` -- INFO and above (INFO / WARNING / ERROR / CRITICAL);
  - ``error.log`` -- ERROR and above (ERROR / CRITICAL).

  Each file is rotated at 10 MiB with 5 backups via
  :class:`logging.handlers.RotatingFileHandler`, so that long-running sessions
  do not produce unbounded log files.

Third-party library loggers (``aiohttp``, ``uvicorn``) are tuned to sensible
levels so that they do not flood the output.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Optional, Union

import colorlog

from app.config import DEFAULT_LOG_FILE, DATA_DIR

#: Name of the application root logger.
LOGGER_NAME: str = "pocketterm"

#: Default log level when none is supplied.
DEFAULT_LEVEL: str = "INFO"

#: Maximum size of a single log file before rotation (10 MiB).
MAX_LOG_BYTES: int = 10 * 1024 * 1024

#: Number of rotated log files to keep.
BACKUP_COUNT: int = 5

#: Default path for the INFO-level log file: ``backend/data/info.log``
DEFAULT_INFO_LOG_FILE: Path = DATA_DIR / "info.log"

#: Default path for the ERROR-level log file: ``backend/data/error.log``
DEFAULT_ERROR_LOG_FILE: Path = DATA_DIR / "error.log"

#: Color mapping used by the console formatter.
LOG_COLORS = {
    "DEBUG": "cyan",
    "INFO": "green",
    "WARNING": "yellow",
    "ERROR": "red",
    "CRITICAL": "red,bg_white",
}

#: Console format with colors.
CONSOLE_FORMAT = (
    "%(log_color)s%(asctime)s "
    "[%(levelname)-8s] "
    "%(name)s: %(message)s"
)

#: Plain format for the log file (no color codes).
FILE_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"

#: Datetime format shared by both handlers.
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _resolve_level(level: Union[str, int, None]) -> int:
    """Translate a level name/number into a numeric logging level."""
    if level is None:
        return logging.INFO
    if isinstance(level, int):
        return level
    return getattr(logging, str(level).upper(), logging.INFO)


def _resolve_log_file(log_file: Union[str, Path, None]) -> Path:
    """Return an absolute :class:`Path` for the given log file."""
    if log_file is None or log_file == "":
        return DEFAULT_LOG_FILE
    path = Path(log_file)
    if not path.is_absolute():
        path = DATA_DIR.parent / path
    return path


def configure_third_party_loggers() -> None:
    """Apply sensible log levels to noisy third-party libraries."""
    # aiohttp is extremely chatty at INFO/DEBUG; WARNING is plenty.
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    # uvicorn's own logger is useful, keep it at INFO.
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    # websockets also tends to be noisy.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    # asyncio debug is rarely useful.
    logging.getLogger("asyncio").setLevel(logging.INFO)


def setup_logger(
    name: str = LOGGER_NAME,
    level: Union[str, int, None] = DEFAULT_LEVEL,
    log_file: Union[str, Path, None] = None,
    to_console: bool = True,
    to_file: bool = True,
) -> logging.Logger:
    """Build and return a configured logger.

    Parameters
    ----------
    name:
        The logger name. Defaults to the application logger ``"pocketterm"``.
    level:
        Logging level for the application logger (e.g. ``"DEBUG"``, ``"INFO"``).
    log_file:
        Path to the main log file (captures every level). Defaults to
        ``backend/data/pocketterm.log``. Two additional level-specific files
        (``info.log`` and ``error.log``) are written to the same directory.
    to_console:
        Whether to attach the colored console handler.
    to_file:
        Whether to attach the rotating file handlers. When enabled, three files
        are produced in the log directory:

        * ``<log_file>`` -- all levels (DEBUG and above);
        * ``info.log`` -- INFO and above;
        * ``error.log`` -- ERROR and above.

        Each file is rotated at 10 MiB and keeps 5 backups.

    The function is idempotent: calling it multiple times for the same logger
    name will not duplicate handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(_resolve_level(level))

    # Remove any handlers we previously installed so re-configuration is clean.
    for handler in list(logger.handlers):
        if getattr(handler, "_pocketterm_handler", False):
            logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # ---- Console (colored) handler -------------------------------------
    if to_console:
        console_formatter = colorlog.ColoredFormatter(
            CONSOLE_FORMAT,
            datefmt=DATE_FORMAT,
            log_colors=LOG_COLORS,
            secondary_log_colors={},
        )
        console_handler = logging.StreamHandler(stream=sys.stdout)
        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(_resolve_level(level))
        console_handler._pocketterm_handler = True  # type: ignore[attr-defined]
        logger.addHandler(console_handler)

    # ---- File (rotating) handlers ---------------------------------------
    if to_file:
        target = _resolve_log_file(log_file)
        # Make sure the parent directory exists (shared by all three files).
        target.parent.mkdir(parents=True, exist_ok=True)
        file_formatter = logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT)

        # 1. 完整日志: 捕获所有级别 (DEBUG 及以上) -> pocketterm.log
        file_handler = logging.handlers.RotatingFileHandler(
            target,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)  # file always captures everything
        file_handler._pocketterm_handler = True  # type: ignore[attr-defined]
        logger.addHandler(file_handler)

        # 2. info.log: INFO 及以上级别 (INFO / WARNING / ERROR / CRITICAL)
        info_target = target.parent / "info.log"
        info_handler = logging.handlers.RotatingFileHandler(
            info_target,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        info_handler.setFormatter(file_formatter)
        info_handler.setLevel(logging.INFO)
        info_handler._pocketterm_handler = True  # type: ignore[attr-defined]
        logger.addHandler(info_handler)

        # 3. error.log: ERROR 及以上级别 (ERROR / CRITICAL), 便于快速定位故障
        error_target = target.parent / "error.log"
        error_handler = logging.handlers.RotatingFileHandler(
            error_target,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        error_handler.setFormatter(file_formatter)
        error_handler.setLevel(logging.ERROR)
        error_handler._pocketterm_handler = True  # type: ignore[attr-defined]
        logger.addHandler(error_handler)

    # Avoid double output through the root logger.
    logger.propagate = False

    # Tune third-party loggers once.
    configure_third_party_loggers()

    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger under the application root logger.

    If no ``name`` is given the application root logger is returned. Otherwise a
    logger named ``"pocketterm.<name>"`` is returned so that all loggers share
    the same handlers configured by :func:`setup_logger`.
    """
    if not name:
        return logging.getLogger(LOGGER_NAME)
    if name.startswith(LOGGER_NAME):
        return logging.getLogger(name)
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


# ---------------------------------------------------------------------------
# Eagerly configure the application logger on import.
# ---------------------------------------------------------------------------
# Doing this at import time means that simply doing
# ``from app.logger import get_logger`` is enough to get a fully configured
# logger. The level/file can be overridden later by calling ``setup_logger``
# with values read from the configuration file.
logger: logging.Logger = setup_logger()

__all__ = [
    "LOGGER_NAME",
    "DEFAULT_LEVEL",
    "DEFAULT_INFO_LOG_FILE",
    "DEFAULT_ERROR_LOG_FILE",
    "setup_logger",
    "get_logger",
    "configure_third_party_loggers",
    "logger",
]
