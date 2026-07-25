# ---
# description: |
#   Logging configuration for CryoSoft. Resolves the log directory
#   (log_directory()) and sets up a rotating file handler that writes to
#   <log_dir>/cryosoft.log, plus a console handler for development.
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


def setup_logging(log_dir: str | Path | None = None, level: int = logging.DEBUG) -> None:
    """Configure CryoSoft logging with rotating file + console output.

    Args:
        log_dir: Directory for log files. Defaults to ``log_directory()``.
        level: Root logger level. DEBUG for development, INFO for production.
    """
    if log_dir is None:
        log_dir = log_directory()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "cryosoft.log"

    # Structured operational-status stream: one JSON object per line, consumed
    # by the troubleshoot layer. Kept OFF the human handlers (propagate=False)
    # so JSON never clutters the console or GUI log. Its own idempotency guard,
    # so it survives the root-handler early-return on repeated setup_logging().
    status_logger = logging.getLogger("cryosoft.status")
    status_logger.setLevel(logging.INFO)
    status_logger.propagate = False
    if not status_logger.handlers:
        status_handler = logging.handlers.RotatingFileHandler(
            log_dir / "status.jsonl", maxBytes=10 * 1024 * 1024,
            backupCount=3, encoding="utf-8",
        )
        status_handler.setFormatter(logging.Formatter("%(message)s"))
        status_logger.addHandler(status_handler)

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
