"""CryoSoft exception hierarchy.

Exception tree:
    CryoSoftError
    ├── CryoSoftCommunicationError   — VISA / instrument communication failure
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


class CryoSoftSafetyError(CryoSoftError):
    """Raised when a safety condition is violated."""
    pass


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
