"""CryoSoft exception hierarchy.

Exception tree:
    CryoSoftError
    ├── CryoSoftCommunicationError   — VISA / instrument communication failure
    │   └── CryoSoftInstrumentError  — the instrument itself refused a command
    ├── CryoSoftSafetyError          — safety condition violated
    │   └── CryoSoftActionRefusedError    — the direct action path refused a call
    │       ├── CryoSoftPrivateActionError    — underscore-prefixed method name
    │       ├── CryoSoftUndeclaredActionError — method carries no @control
    │       └── CryoSoftActionScopeError      — capability outside the caller's scope
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


class CryoSoftInstrumentError(CryoSoftCommunicationError):
    """Raised when the instrument itself reports that it refused a command.

    The typed error of the **driver error-reporting standard** (see
    ``drivers/README.md``): the bus transaction succeeded — bytes went out
    and, where the protocol answers, bytes came back — but the instrument
    then told CryoSoft it did not carry the command out. That is a
    different fact from "the link is broken", and silently discarding it is
    the failure mode the standard exists to prevent: the caller believes it
    set a current, the instrument disagrees, and everything downstream is
    fiction.

    A subclass of ``CryoSoftCommunicationError`` on purpose: every layer
    that already treats a driver call as fallible (``Station.get_state()``'s
    stale-value handling, a VI's guarded control path) keeps working
    unchanged, while code that cares about the distinction can catch this
    type specifically and read the instrument's own words out of it.

    Attributes:
        code: The instrument's own refusal code as a string, verbatim where
            the instrument emits one (``"-221"`` from a SCPI error queue,
            ``"?"`` from an Oxford ISOBUS reply, ``"DENIED"`` from a Mercury
            acknowledgement) and, for the protocols that report a refusal
            without a code, the standard's own token for how the refusal was
            detected (``"READBACK_MISMATCH"``, ``"PROTOCOL"``). See the
            per-driver table in ``drivers/README.md``.
        instrument_message: The instrument's own message text, verbatim
            (e.g. ``'Settings conflict'``), empty when the protocol sends
            none.
        context: What the driver was doing when the instrument refused
            (e.g. ``"set_current(0.0001)"``) — the half the instrument
            cannot know and the reader always needs.
    """

    def __init__(
        self,
        message: str,
        code: str = "",
        instrument_message: str = "",
        context: str = "",
        vi_name: str = "",
        original_error: Exception | None = None,
    ):
        """Build the typed instrument-refusal error.

        Args:
            message: Human-readable description, normally assembled from the
                other fields by the driver raising it.
            code: The instrument's refusal code (see the class docstring).
            instrument_message: The instrument's own message text.
            context: The driver call that was refused.
            vi_name: Name of the driver/VI that encountered the error.
            original_error: The underlying exception, when there was one.
        """
        self.code = code
        self.instrument_message = instrument_message
        self.context = context
        super().__init__(message, vi_name=vi_name, original_error=original_error)


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


class CryoSoftActionRefusedError(CryoSoftSafetyError):
    """Base for a refusal on the direct action path.

    The direct action path is ``Station.execute_vi_action()`` — the single
    entry point through which the Orchestrator dispatches a manual (GUI or
    agent) action to one VI, as opposed to a procedure's ``Command`` batch
    (see GLOSSARY.md's **Direct action path**). Its admission checks each
    raise their own subclass with its own reason string, so a caller can tell
    "you named a private method", "that method is not a capability" and "that
    capability is out of your scope" apart without parsing prose.

    A refusal is a ``CryoSoftSafetyError``: nothing was sent to the
    instrument, exactly like a ``control_limits`` violation or the
    capability-scope refusal ``Station.send_measurement_commands()`` already
    raises for a plan.
    """
    pass


class CryoSoftPrivateActionError(CryoSoftActionRefusedError):
    """Raised when a direct action names an underscore-prefixed method.

    A leading underscore is a VI's internal API. It is never a capability,
    so the direct action path refuses it before it is even resolved.
    """
    pass


class CryoSoftUndeclaredActionError(CryoSoftActionRefusedError):
    """Raised when a direct action names a method that is not a capability.

    Only a ``@control`` method, or one of the two connection-lifecycle
    operating-state methods (``initiate``/``standby``), may be dispatched on
    the direct action path. Anything else — a ``@monitored`` poller, a
    procedure-only helper such as ``take_reading()``, a plain public utility
    — is refused.
    """
    pass


class CryoSoftActionScopeError(CryoSoftActionRefusedError):
    """Raised when a direct action's capability scope exceeds the caller's.

    The capability-scope standard (GLOSSARY.md's **Capability scope**): an
    ``@control(scope="operation")`` method is refused for a caller holding
    only ``"measurement"`` scope, mirroring
    ``Station.send_measurement_commands()``'s pre-dispatch check for a plan.
    """
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
