# ---
# description: |
#   Logging configuration for CryoSoft. Sets up a rotating file handler
#   that writes to <AppData>/CryoSoft/logs/cryosoft.log, plus a console
#   handler for development.
# last_updated: 2026-07-20
# ---

"""CryoSoft logging setup.

Call setup_logging() once at application startup. All modules use
logging.getLogger(__name__) — never print().
"""

import logging
import logging.handlers
from pathlib import Path

from PyQt6.QtCore import QCoreApplication, QStandardPaths

# Foundation module (import-linter contract C1: zero cryosoft-internal
# imports), so the AppData directory is resolved with the same Qt API
# gui/app_settings.py uses rather than importing that module. Only
# applicationName is set (never organizationName) so this resolves to the
# same "%APPDATA%/CryoSoft/" root app_settings.py already uses for
# last_session.json/users.json/configs, not a nested "CryoSoft/CryoSoft/".
# QStandardPaths.AppDataLocation keys off applicationName, which main.py
# normally sets on the QApplication instance — but setup_logging() runs
# before that instance exists, so it is set here too (idempotent; harmless
# if main.py sets the same value again later).
_APPLICATION = "CryoSoft"


def _default_log_dir() -> Path:
    """Return the default log directory: <AppData>/CryoSoft/logs/.

    Returns:
        The platform per-installation application-data directory's ``logs``
        subfolder, separate from both the repo/install location and the
        user's measurement data directory.
    """
    if not QCoreApplication.applicationName():
        QCoreApplication.setApplicationName(_APPLICATION)
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    return Path(base) / "logs"


def setup_logging(log_dir: str | Path | None = None, level: int = logging.DEBUG) -> None:
    """Configure CryoSoft logging with rotating file + console output.

    Args:
        log_dir: Directory for log files. Defaults to
            ``<AppData>/CryoSoft/logs/`` (see :func:`_default_log_dir`).
        level: Root logger level. DEBUG for development, INFO for production.
    """
    if log_dir is None:
        log_dir = _default_log_dir()
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
