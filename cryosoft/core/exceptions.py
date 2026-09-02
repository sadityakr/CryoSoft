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
    """Raised when a safety condition is violated.

    The control-validation standard's refusal: raised by the ``@control``
    limit wrapper before any hardware call, and by VIs and the Orchestrator
    for the interlocks they own. The optional keyword fields carry the
    *structured* form of a rejected control parameter so a caller building a
    verdict never has to parse the message prose. They default to ``None``,
    so every raise site that only has a sentence keeps working unchanged.

    Attributes:
        param: Name of the rejected control parameter, or ``None``.
        value: The value that was rejected, in SI units.
        lo: Lower bound of the allowed range, ``None`` for unbounded below.
        hi: Upper bound of the allowed range, ``None`` for unbounded above.
        limit_name: The ``control_limits`` limit that rejected it — the key
            the station config populates — or ``None``.
    """

    def __init__(
        self,
        message: str = "",
        *,
        param: str | None = None,
        value: float | None = None,
        lo: float | None = None,
        hi: float | None = None,
        limit_name: str | None = None,
    ):
        """Store the optional structured fields and the message.

        Args:
            message: Human-readable description, suitable for a GUI banner.
            param: Name of the rejected control parameter.
            value: The rejected value, in SI units.
            lo: Lower bound of the allowed range, or ``None``.
            hi: Upper bound of the allowed range, or ``None``.
            limit_name: The ``control_limits`` limit that rejected it.
        """
        self.param = param
        self.value = value
        self.lo = lo
        self.hi = hi
        self.limit_name = limit_name
        super().__init__(message)


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
