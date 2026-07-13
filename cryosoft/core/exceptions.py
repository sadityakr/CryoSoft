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
    ├── CryoSoftConfigError          — YAML config invalid or missing
    └── DataSchemaError              — datapoint does not match its declared HDF5 schema
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


class DataSchemaError(CryoSoftError):
    """Raised when a datapoint does not conform to its declared ``DataSchema``.

    Carried by ``DataSchema.validate()`` when a measurement datapoint is missing
    declared keys, has undeclared extra keys, or has values of the wrong shape or
    scalar type. The message lists *all* detected problems at once so the guilty
    module surfaces the full mismatch in one traceback rather than one error per
    fix-and-rerun cycle.
    """
    pass
