# ---
# description: |
#   All CryoSoft exception classes. Every exception in the system inherits
#   from CryoSoftError. Layers catch specific subtypes and let the rest
#   propagate upward.
# last_updated: 2026-04-06
# ---

"""CryoSoft exception hierarchy.

Exception tree:
    CryoSoftError
    ├── CryoSoftCommunicationError   — VISA / instrument communication failure
    ├── CryoSoftSafetyError          — safety condition violated
    └── CryoSoftConfigError          — YAML config invalid or missing
"""


class CryoSoftError(Exception):
    """Base exception for all CryoSoft errors."""
    pass


class CryoSoftCommunicationError(CryoSoftError):
    """Raised when instrument communication fails.

    Attributes:
        vi_name: Name of the VI that encountered the error (set by logging wrapper).
        original_error: The underlying exception (e.g., VisaIOError).
        message: Human-readable description.
    """

    def __init__(self, message: str, vi_name: str = "", original_error: Exception | None = None):
        self.vi_name = vi_name
        self.original_error = original_error
        super().__init__(message)


class CryoSoftSafetyError(CryoSoftError):
    """Raised when a safety condition is violated."""
    pass


class CryoSoftConfigError(CryoSoftError):
    """Raised when YAML configuration is invalid or missing."""
    pass
