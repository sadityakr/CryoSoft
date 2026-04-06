# ---
# description: |
#   Logging configuration for CryoSoft. Sets up a rotating file handler
#   that writes to cryosoft/logs/cryosoft.log, plus a console handler
#   for development.
# last_updated: 2026-04-06
# ---

"""CryoSoft logging setup.

Call setup_logging() once at application startup. All modules use
logging.getLogger(__name__) — never print().
"""

import logging
import logging.handlers
from pathlib import Path


def setup_logging(log_dir: str | Path | None = None, level: int = logging.DEBUG) -> None:
    """Configure CryoSoft logging with rotating file + console output.

    Args:
        log_dir: Directory for log files. Defaults to cryosoft/logs/.
        level: Root logger level. DEBUG for development, INFO for production.
    """
    if log_dir is None:
        log_dir = Path(__file__).parent.parent / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "cryosoft.log"

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
