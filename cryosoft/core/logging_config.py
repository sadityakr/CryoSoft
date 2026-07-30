# ---
# description: |
#   Logging configuration for CryoSoft. Resolves the log directory
#   (log_directory()) and sets up a rotating file handler that writes to
#   <log_dir>/cryosoft.log, a console handler for development, plus four
#   time-rotated JSONL streams: cryosoft.status (operational status) and the
#   three tiered trend-history streams (raw / 3-min / hourly).
# last_updated: 2026-07-25
# ---

"""CryoSoft logging setup.

Call setup_logging() once at application startup. All modules use
logging.getLogger(__name__) — never print().

This module is import-linter contract C1 foundation: it must import nothing
else from the ``cryosoft`` package, stdlib only.
"""

import logging
import logging.handlers
import os
from pathlib import Path


def log_directory() -> Path:
    """Resolve the CryoSoft log directory without creating it.

    Precedence:

    1. ``CRYOSOFT_LOG_DIR`` environment variable, if set and non-empty.
    2. ``%LOCALAPPDATA%\\CryoSoft\\logs`` on Windows (``os.name == "nt"``),
       or ``~/.local/state/cryosoft/logs`` on other platforms — provided the
       relevant platform variable (``LOCALAPPDATA`` on Windows) is set.
    3. ``cryosoft/logs/`` (next to this package) as the final fallback, used
       when the platform-specific location above is unavailable.

    This is a pure function: it only resolves and returns a path, it never
    creates the directory or any file in it. ``setup_logging()`` is
    responsible for the ``mkdir(parents=True, exist_ok=True)``.

    No migration of existing log files is performed when the resolved
    location changes (e.g. moving off ``CRYOSOFT_LOG_DIR`` or between
    machines). Logs are disposable operational telemetry, not data of
    record: the new location simply starts empty. Do not write a migrator
    for this.

    Returns:
        The resolved log directory path (not guaranteed to exist).
    """
    env_dir = os.environ.get("CRYOSOFT_LOG_DIR")
    if env_dir:
        return Path(env_dir)

    if os.name == "nt":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / "CryoSoft" / "logs"
    else:
        return Path.home() / ".local" / "state" / "cryosoft" / "logs"

    return Path(__file__).parent.parent / "logs"


def _add_jsonl_handler(
    name: str, path: Path, *, when: str, backup_count: int
) -> None:
    """Configure one propagate=False JSONL logger with a timed-rotating handler.

    Shared by ``cryosoft.status`` and the three ``cryosoft.trend_*`` loggers
    (see the module-level table in the docstring of ``setup_logging``) so the
    four near-identical blocks collapse into one place. Each stream is one
    JSON object per line, kept off the human console/file handlers
    (``propagate=False``) and idempotency-guarded so repeated
    ``setup_logging()`` calls never duplicate handlers.

    The idempotency guard asks whether *this* stream's handler is already
    installed, not whether the logger has any handler at all. The weaker
    question is a proxy that a foreign handler satisfies: anything that
    attaches to one of these loggers first (a test harness capturing logs, an
    embedding application, a debugger) would make this function conclude it
    had already run and silently skip installing the writer, leaving the JSONL
    file empty while the app appears healthy. These streams are the input to
    the operational-status and trend-history readers, so that failure surfaces
    only much later, as missing data.

    Args:
        name: Logger name, e.g. ``"cryosoft.status"``.
        path: Full path to the JSONL file this logger writes.
        when: ``TimedRotatingFileHandler`` rotation unit (``"midnight"`` for
            daily, ``"W0"`` for weekly on Monday).
        backup_count: Number of rotated backups to retain.
    """
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    already_installed = any(
        isinstance(existing, logging.handlers.TimedRotatingFileHandler)
        for existing in logger.handlers
    )
    if not already_installed:
        handler = logging.handlers.TimedRotatingFileHandler(
            path, when=when, backupCount=backup_count, encoding="utf-8", utc=True
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)


def setup_logging(log_dir: str | Path | None = None, level: int = logging.DEBUG) -> None:
    """Configure CryoSoft logging with rotating file + console + JSONL streams.

    Four parallel JSONL streams, each one JSON object per line, each its own
    ``propagate=False`` logger so JSON never reaches the console/GUI log
    handlers:

    ============ ==================== ============ ===============
    Logger        File                 Rotation     backupCount
    ============ ==================== ============ ===============
    cryosoft.status         status.jsonl              daily (UTC)   7
    cryosoft.trend_raw      trend_history_raw.jsonl    daily (UTC)   2
    cryosoft.trend_3min     trend_history_3min.jsonl   daily (UTC)   8
    cryosoft.trend_hourly   trend_history_hourly.jsonl weekly (UTC) 53
    ============ ==================== ============ ===============

    ``cryosoft.status`` moved here from a size-based ``RotatingFileHandler``
    (10 MB x 3) to a daily ``TimedRotatingFileHandler``: its old time
    coverage was an accident of VI count and tick rate, whereas "the last N
    days of operational status" needs to be a guarantee independent of how
    busy a given day's ticking was. Its record schema and its readers
    (``status_reader.py``) are unchanged, only the handler is; readers that
    already glob rotated files keep working unmodified. ``utc=True`` on every
    handler avoids a DST-related duplicated/missing rotation boundary against
    the ``time.time()`` epochs the records carry.

    Args:
        log_dir: Directory for log files. Defaults to ``log_directory()``.
        level: Root logger level. DEBUG for development, INFO for production.
    """
    if log_dir is None:
        log_dir = log_directory()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "cryosoft.log"

    _add_jsonl_handler(
        "cryosoft.status", log_dir / "status.jsonl", when="midnight", backup_count=7
    )
    _add_jsonl_handler(
        "cryosoft.trend_raw",
        log_dir / "trend_history_raw.jsonl",
        when="midnight",
        backup_count=2,
    )
    _add_jsonl_handler(
        "cryosoft.trend_3min",
        log_dir / "trend_history_3min.jsonl",
        when="midnight",
        backup_count=8,
    )
    _add_jsonl_handler(
        "cryosoft.trend_hourly",
        log_dir / "trend_history_hourly.jsonl",
        when="W0",
        backup_count=53,
    )

    # Root logger
    root = logging.getLogger("cryosoft")
    root.setLevel(level)

    # Avoid duplicate handlers on repeated calls
    if root.handlers:
        return

    # Rotating file handler: 5 MB per file, keep 5 backups
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    root.addHandler(file_handler)

    # Console handler (for development)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter("%(levelname)-8s | %(name)s | %(message)s")
    console_handler.setFormatter(console_fmt)
    root.addHandler(console_handler)
