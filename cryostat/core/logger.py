"""
Logging Configuration
=====================

Centralized logging setup for the cryostat control system.

Provides convenient configuration of logging levels, formats, and
outputs without cluttering the codebase.
"""

import logging
import sys
from pathlib import Path
from typing import Optional


def setup_cryostat_logging(
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    console: bool = True,
    format_string: Optional[str] = None
):
    """Configure logging for the entire cryostat system.

    Sets up hierarchical logging with appropriate levels for different
    modules. Output can go to console, file, or both.

    Args:
        level: Root logging level (logging.DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Path to log file (None = no file logging)
        console: Enable console logging (default: True)
        format_string: Custom format string (None = use default)

    Example:
        >>> # Basic setup
        >>> setup_cryostat_logging(level=logging.INFO)

        >>> # Detailed logging to file and console
        >>> setup_cryostat_logging(
        ...     level=logging.DEBUG,
        ...     log_file='cryostat_debug.log',
        ...     console=True
        ... )

        >>> # Production: only warnings and errors
        >>> setup_cryostat_logging(
        ...     level=logging.WARNING,
        ...     log_file='cryostat_errors.log',
        ...     console=False
        ... )
    """
    # Default format if not provided
    if format_string is None:
        format_string = '%(asctime)s [%(levelname)8s] %(name)s - %(message)s'

    # Create formatter
    formatter = logging.Formatter(
        fmt=format_string,
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Setup handlers
    handlers = []

    # Console handler
    if console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        handlers.append(console_handler)

    # File handler
    if log_file:
        try:
            # Ensure directory exists
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            handlers.append(file_handler)
        except Exception as e:
            print(f"Warning: Could not setup file logging: {e}", file=sys.stderr)

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add new handlers
    for handler in handlers:
        root_logger.addHandler(handler)

    # Set specific levels for modules
    _set_module_levels(level)

    # Log initialization
    log = logging.getLogger(__name__)
    log.info("Cryostat logging initialized")
    log.info(f"Level: {logging.getLevelName(level)}")
    if log_file:
        log.info(f"Log file: {log_file}")


def _set_module_levels(base_level: int):
    """Set logging levels for specific modules.

    Creates a hierarchical logging structure where different components
    can have different verbosity levels.

    Args:
        base_level: Base level for the system
    """
    # Map module paths to levels (relative to base level)
    module_levels = {
        'cryostat.drivers': base_level,  # Driver layer
        'cryostat.actions': min(base_level, logging.INFO),  # Action layer (less verbose)
        'cryostat.core': min(base_level, logging.INFO),  # Core layer
        'cryostat.config': min(base_level, logging.INFO),  # Config loader
    }

    for module_name, level in module_levels.items():
        logger = logging.getLogger(module_name)
        logger.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    """Get a logger for a specific component.

    Convenience function for getting module-specific loggers.

    Args:
        name: Logger name (usually __name__)

    Returns:
        logging.Logger: Configured logger

    Example:
        >>> log = get_logger(__name__)
        >>> log.info("Module initialized")
    """
    return logging.getLogger(name)


def set_level(level: int):
    """Change logging level for entire system.

    Args:
        level: New logging level (logging.DEBUG, INFO, etc.)

    Example:
        >>> # Enable debug logging
        >>> set_level(logging.DEBUG)

        >>> # Reduce to warnings only
        >>> set_level(logging.WARNING)
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Update module levels
    _set_module_levels(level)

    log = logging.getLogger(__name__)
    log.info(f"Logging level changed to: {logging.getLevelName(level)}")


def enable_debug():
    """Enable debug logging (convenience function).

    Example:
        >>> enable_debug()
    """
    set_level(logging.DEBUG)


def disable_debug():
    """Disable debug logging (back to INFO).

    Example:
        >>> disable_debug()
    """
    set_level(logging.INFO)


# Export public API
__all__ = [
    'setup_cryostat_logging',
    'get_logger',
    'set_level',
    'enable_debug',
    'disable_debug',
]
