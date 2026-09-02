"""The control contract — the typed currency between the engine and its clients.

Two modules' worth of payload live here, both frozen and dependency-free so
that the Orchestrator (emitter) and every client (the GUI today, an agent
gateway later) can import them without dragging a layer along:

* ``ErrorEvent`` — the structured error/fault notification.
* The **control contract**: ``Actor``, ``Command``, ``Verdict`` and the
  ``Event`` union. The Orchestrator has exactly two clients, the GUI and the
  agent, and they must see the same system and be seen doing the same things.
  So the boundary is one contract with two renderings rather than a proxy for
  one client and a gateway for the other: every action is a ``Command``
  answered by exactly one ``Verdict``, every consequence is an ``Event``, and
  every message names the ``Actor`` behind it, so a client reflects what the
  other one did for free.

Every contract type is a frozen dataclass whose fields are JSON-safe (``str``,
``int``, ``float``, ``bool``, ``None``, list/tuple/dict of those, enums
rendered as their values, and nested contract types) and carries
``to_json()`` / ``from_json()``. A dict round trip is the contract: the value
survives ``to_json()`` → ``json.dumps`` → ``json.loads`` → ``from_json()``
unchanged, which is what lets the same declaration cross a thread boundary
today and a process boundary later with no second contract. Every event also
carries a class-level ``kind`` discriminator, emitted into its JSON, so
``event_from_json()`` can rebuild the right type without the caller knowing
which one it holds.

``Command`` and ``Verdict`` here are the *control-contract* pair — a client's
request to the engine and the engine's answer. They are deliberately distinct
from ``core.plan.Command`` (one method call a procedure hands the Orchestrator
to dispatch on a VI) and ``core.conditions.Verdict`` (the enforcement decision
``decide()`` computes from a set of conditions); see ``GLOSSARY.md``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, ClassVar, Self


@dataclass(frozen=True)
class ErrorEvent:
    """One structured error/fault notification.

    Attributes:
        vi_name: The VI the event concerns, or ``None`` for a machine-wide
            event with no single originating instrument (e.g. an unhandled
            tick-boundary exception). May also be a comma-joined list of
            names when more than one VI is implicated (e.g. an EMERGENCY
            tripped by more than one instrument's safety flag).
        kind: The blast-radius tier this event belongs to:
            ``"fault"`` (a VI-scoped comm/stale/disconnected fault that
            quarantines only that VI), ``"run_failure"`` (an active run's
            claimed VI faulted — the run fails, the machine returns to
            IDLE), ``"safety"`` (a tripped safety flag — global EMERGENCY),
            ``"internal"`` (an unhandled tick-boundary exception —
            global ERROR, unknown blast radius), or ``"safety_hold"`` (a
            VI-scoped safety-hold enforcement action — the Orchestrator
            re-asserting or failing to re-assert ``standby()`` on a VI held
            by a hold-severity safety condition; scoped to that one VI,
            never a blast radius beyond it, unlike ``"safety"`` above).
        severity: ``"warning"``, ``"error"``, or ``"emergency"``.
        message: Human-readable description, suitable for direct display.
        timestamp: Unix time the event was created (``time.time()``).
    """

    vi_name: str | None
    kind: str
    severity: str
    message: str
    timestamp: float


# ══════════════════════════════════════════════════════════════════════
# The control contract
# ══════════════════════════════════════════════════════════════════════

# The JSON-safe scalar leaves every contract field bottoms out in.
_JSON_SCALARS = (str, int, float, bool)


def _jsonable(value: Any) -> Any:
    """Render one contract field as a JSON-safe value.

    Args:
        value: The field value. Enums render as their value, nested contract
            types through their own ``to_json()``, tuples as lists, mappings
            as plain dicts, and JSON scalars unchanged.

    Returns:
        A value made only of ``str``/``int``/``float``/``bool``/``None``/
        ``list``/``dict``, i.e. one ``json.dumps`` accepts.

    Raises:
        TypeError: If the value is of a type the contract cannot carry. This
            fires at construction (every contract type validates eagerly), so
            a non-serialisable payload fails at the boundary that built it
            rather than at the boundary that has to send it.
    """
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, _JSON_SCALARS):
        return value
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    raise TypeError(
        f"control-contract fields must be JSON-safe; got {type(value).__name__}"
    )


def _checked_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a defensive, JSON-checked copy of a contract mapping field.

    Args:
        value: The mapping to copy, or ``None`` for an empty one.

    Returns:
        A new ``dict`` whose contents are known JSON-safe.

    Raises:
        TypeError: If the value is not a mapping, or holds a non-JSON-safe
            value (raised by ``_jsonable``).
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"expected a mapping, got {type(value).__name__}")
    return {str(key): _jsonable(item) for key, item in value.items()}


class _ContractMessage:
    """JSON round trip shared by every control-contract message.

    Mixed into the frozen dataclasses below. ``to_json()`` renders the
    declared fields plus, for events, the class-level ``kind`` discriminator;
    ``from_json()`` rebuilds from that dict, ignoring keys it does not declare
    so a newer producer never breaks an older consumer. Field coercion (a
    plain string back into its enum, a list back into a tuple, a dict back
    into a nested ``Actor``) happens in each class's ``__post_init__``, so the
    same coercion serves construction and deserialisation.
    """

    def to_json(self) -> dict[str, Any]:
        """Render this message as a JSON-safe dict.

        Returns:
            A dict of the declared fields, plus ``"kind"`` for event types.
        """
        payload = {f.name: _jsonable(getattr(self, f.name)) for f in fields(self)}
        kind = getattr(type(self), "kind", None)
        if isinstance(kind, str):
            payload["kind"] = kind
        return payload

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> Self:
        """Rebuild a message from its ``to_json()`` dict.

        Args:
            payload: A mapping as produced by ``to_json()`` (or the result of
                sending one through ``json.dumps``/``json.loads``). Unknown
                keys — including the ``"kind"`` discriminator — are ignored.

        Returns:
            An instance of the class this is called on.

        Raises:
            TypeError: If a declared field is missing or carries a value the
                type rejects.
        """
        declared = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in payload.items() if k in declared})


# ── Actor ─────────────────────────────────────────────────────────────

class ActorKind(str, Enum):
    """Who is acting: a human at the GUI, an autonomous agent, or the system.

    A ``str`` enum so the value is JSON-safe as it stands and compares equal
    to its own wire form.
    """

    OPERATOR = "operator"
    AGENT = "agent"
    SYSTEM = "system"


@dataclass(frozen=True)
class Actor(_ContractMessage):
    """Who issued a command, or on whose behalf an event happened.

    Accountability is a value, not an ambient fact: every command and every
    event it causes names its actor, so the status bar can say "paused by
    agent", the queue can show who queued a run, and a run record can tell an
    agent's self-confirmation apart from the physicist's.

    Attributes:
        kind: ``operator``, ``agent`` or ``system`` (see ``ActorKind``).
            Accepts the enum member or its string value.
        id: Stable identifier for this actor — a username, an agent
            deployment name, or a subsystem name for ``system``.
        role: The authority this actor holds, free-form here; the agent
            gateway that grants roles owns their vocabulary.
    """

    kind: ActorKind
    id: str
    role: str = ""

    def __post_init__(self) -> None:
        """Coerce ``kind`` to its enum member and validate the string fields.

        Raises:
            ValueError: If ``kind`` is not a known ``ActorKind``.
            TypeError: If ``id`` or ``role`` is not a string.
        """
        object.__setattr__(self, "kind", ActorKind(self.kind))
        for name in ("id", "role"):
            if not isinstance(getattr(self, name), str):
                raise TypeError(f"Actor.{name} must be a str")


#: The default actor for every entry point: the human at the GUI. Public
#: methods take ``actor=OPERATOR`` so no existing call site has to change.
OPERATOR = Actor(kind=ActorKind.OPERATOR, id="operator", role="operator")


def _as_actor(value: Actor | Mapping[str, Any]) -> Actor:
    """Coerce an actor field, which may arrive as its JSON dict.

    Args:
        value: An ``Actor`` or the mapping ``Actor.to_json()`` produced.

    Returns:
        An ``Actor``.

    Raises:
        TypeError: If the value is neither.
    """
    if isinstance(value, Actor):
        return value
    if isinstance(value, Mapping):
        return Actor.from_json(value)
    raise TypeError(f"expected an Actor, got {type(value).__name__}")


# ── Command ───────────────────────────────────────────────────────────

class CommandName(str, Enum):
    """Every Orchestrator command a client may issue.

    The command half of the control contract is enumerated exactly once,
    here: the GUI's action buttons and the agent's tool list are both
    rendered from this enum, so neither client can offer an action the other
    cannot see. Each value is the name of the ``Orchestrator`` method that
    implements it, which is what makes dispatch a lookup rather than a table.

    Read-only accessors are deliberately absent — a client answers those from
    its latest ``StatusSnapshot``, never by calling into the engine — as is
    ``shutdown()``, which is process lifecycle owned by ``main.py``.
    ``tests/test_conformance.py`` diffs this enum against the Orchestrator's
    public methods in both directions, with those exemptions named and
    justified there.
    """

    # Runs and the queue
    RUN_PROCEDURE = "run_procedure"
    RUN_OPERATION = "run_operation"
    QUEUE_PROCEDURE = "queue_procedure"
    QUEUE_OPERATION = "queue_operation"
    RUN_QUEUE = "run_queue"
    PAUSE_PROCEDURE = "pause_procedure"
    RESUME_PROCEDURE = "resume_procedure"
    ABORT_PROCEDURE = "abort_procedure"

    # Operation steps
    CONFIRM_OPERATION = "confirm_operation"
    SKIP_OPERATION_STEP = "skip_operation_step"
    FINISH_OPERATION = "finish_operation"

    # Instrument actions
    SUBMIT_VI_ACTION = "submit_vi_action"
    SUBMIT_GLOBAL_ACTION = "submit_global_action"
    STOP_RAMP = "stop_ramp"
    CONNECT_INSTRUMENT = "connect_instrument"
    DISCONNECT_INSTRUMENT = "disconnect_instrument"

    # Faults, safety and recovery
    ACKNOWLEDGE = "acknowledge"
    ACKNOWLEDGE_FAULT = "acknowledge_fault"
    RETRY_FAULT = "retry_fault"
    RECOVER_FROM_ERROR = "recover_from_error"
    EMERGENCY_STANDBY = "emergency_standby"

    # Monitoring and policy
    START_MONITORING = "start_monitoring"
    STOP_MONITORING = "stop_monitoring"
    SET_SCANNER_ENABLED = "set_scanner_enabled"
    SET_EXPERIMENT_ENVELOPE = "set_experiment_envelope"


@dataclass(frozen=True)
class Command(_ContractMessage):
    """One client's request for the engine to act.

    The control-contract request type — not ``core.plan.Command``, which is a
    procedure's request to dispatch one method on one VI.

    Attributes:
        name: Which command, from ``CommandName``. Accepts the enum member or
            its string value.
        actor: Who is asking. Defaults to the ``OPERATOR`` sentinel.
        args: The command's arguments, JSON-safe and shaped by the target's
            ``ParamSpec``s. Defensively copied and validated at construction.
        request_id: Correlation id; the ``Verdict`` answering this command and
            every event it causes carry it back. Defaults to a fresh hex UUID.
        issued_at: Unix time the command was created (``time.time()``).
    """

    name: CommandName
    actor: Actor = OPERATOR
    args: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    issued_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Coerce the enum, actor and args fields and validate them eagerly.

        Raises:
            ValueError: If ``name`` is not a known ``CommandName``.
            TypeError: If ``actor`` is not an ``Actor`` (or its dict), if
                ``args`` is not a mapping or holds a non-JSON-safe value, or
                if ``request_id``/``issued_at`` has the wrong type.
        """
        object.__setattr__(self, "name", CommandName(self.name))
        object.__setattr__(self, "actor", _as_actor(self.actor))
        object.__setattr__(self, "args", _checked_mapping(self.args))
        if not isinstance(self.request_id, str):
            raise TypeError("Command.request_id must be a str")
        if not isinstance(self.issued_at, (int, float)):
            raise TypeError("Command.issued_at must be a number")


# ── Verdict ───────────────────────────────────────────────────────────

class VerdictCode(str, Enum):
    """Why a command was accepted or refused.

    The machine-readable half of a verdict: a client decides what to do next
    from the code, never by parsing ``reason``, which exists for humans.

    Members:
        OK: The command was accepted and carried out.
        BLOCKED_STATE: The state machine forbids it right now.
        BLOCKED_CLAIM: An active run claims the instrument.
        BLOCKED_FAULT: The target instrument is faulted or offline.
        BLOCKED_LIMIT: A declared control limit rejected a parameter; the
            ``detail`` dict carries ``param``, ``value``, ``lo``, ``hi`` and
            ``limit_name``.
        BLOCKED_ENVELOPE: The active session envelope forbids it.
        BLOCKED_ROLE: The actor's role does not grant this action.
        FAILED: Accepted, attempted, and it failed.
    """

    OK = "OK"
    BLOCKED_STATE = "BLOCKED_STATE"
    BLOCKED_CLAIM = "BLOCKED_CLAIM"
    BLOCKED_FAULT = "BLOCKED_FAULT"
    BLOCKED_LIMIT = "BLOCKED_LIMIT"
    BLOCKED_ENVELOPE = "BLOCKED_ENVELOPE"
    BLOCKED_ROLE = "BLOCKED_ROLE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Verdict(_ContractMessage):
    """The engine's single answer to one ``Command``.

    Exactly one verdict answers every command, including every refusal — a
    broadcast signal a client may or may not be listening to is not an answer
    an agent can await. The control-contract answer type, not
    ``core.conditions.Verdict``, which is the enforcement decision computed
    from a set of system conditions.

    Attributes:
        request_id: The ``Command.request_id`` this answers.
        command: Which command was asked for.
        code: The machine-readable outcome (see ``VerdictCode``).
        actor: The actor of the command being answered.
        reason: Human-readable explanation, suitable for a banner. Never
            parsed by a client.
        detail: Optional structured explanation of the code — for
            ``BLOCKED_LIMIT`` the rejected ``param``, ``value``, ``lo``,
            ``hi`` and ``limit_name``. JSON-safe.
        result: Optional JSON-safe return value of the underlying call.
        seq: Monotonic sequence number of the emitting engine.
        ts: Unix time the verdict was created (``time.time()``).
    """

    request_id: str
    command: CommandName
    code: VerdictCode
    actor: Actor = OPERATOR
    reason: str = ""
    detail: dict[str, Any] | None = None
    result: Any = None
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Coerce the enum, actor and payload fields and validate them.

        Raises:
            ValueError: If ``command`` or ``code`` is not a known member.
            TypeError: If a field carries a non-JSON-safe or wrongly typed
                value.
        """
        object.__setattr__(self, "command", CommandName(self.command))
        object.__setattr__(self, "code", VerdictCode(self.code))
        object.__setattr__(self, "actor", _as_actor(self.actor))
        if self.detail is not None:
            object.__setattr__(self, "detail", _checked_mapping(self.detail))
        object.__setattr__(self, "result", _jsonable(self.result))
        if not isinstance(self.request_id, str) or not isinstance(self.reason, str):
            raise TypeError("Verdict.request_id and Verdict.reason must be str")
        if not isinstance(self.seq, int) or isinstance(self.seq, bool):
            raise TypeError("Verdict.seq must be an int")

    @property
    def ok(self) -> bool:
        """Whether the command was accepted and carried out.

        Returns:
            ``True`` exactly when ``code`` is ``VerdictCode.OK``. Derived, not
            stored, so no verdict can claim success and a blocking code at
            once.
        """
        return self.code is VerdictCode.OK


# ── Events ────────────────────────────────────────────────────────────
#
# Everything the engine broadcasts. Each event is frozen, carries ``seq``
# and ``ts``, and — where it is the consequence of a command — the
# ``request_id`` and ``actor`` of that command, which is what lets one
# client see what the other one did. Each declares a class-level ``kind``
# discriminator, emitted into its JSON so ``event_from_json()`` can
# dispatch. (``ErrorEvent.kind`` above is a different thing: a blast-radius
# tier, not a type tag.)


@dataclass(frozen=True)
class StateChange(_ContractMessage):
    """The engine's state machine moved.

    Attributes:
        state: The new state's name (an ``OrchestratorState`` value).
        previous: The state left behind, or ``""`` for the first transition.
        cause: Short machine-readable reason for the transition (e.g.
            ``"run_started"``, ``"fault"``, ``"operator_abort"``).
        actor: Who caused it; the ``system`` actor for a transition the engine
            made on its own.
        request_id: The command that caused it, or ``""``.
        seq: Monotonic sequence number.
        ts: Unix time of the transition.
    """

    kind: ClassVar[str] = "state_change"

    state: str
    previous: str = ""
    cause: str = ""
    actor: Actor = OPERATOR
    request_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Coerce the actor field.

        Raises:
            TypeError: If ``actor`` is neither an ``Actor`` nor its dict.
        """
        object.__setattr__(self, "actor", _as_actor(self.actor))


@dataclass(frozen=True)
class StatusSnapshot(_ContractMessage):
    """Everything a client needs to answer a read without calling the engine.

    Emitted once per tick and on every state change; a client's mirror of it
    is what makes every read local. The shape here is the minimal one the
    contract needs — state, the run in flight, and a per-instrument mapping —
    and is meant to be extended field by field as the engine's queries move
    onto it; ``instruments`` carries the live half (availability, faults,
    holds) of what ``StationInfo`` declares statically.

    Attributes:
        state: The engine's current state name.
        run: The active run's summary, or ``None`` when idle. JSON-safe.
        instruments: ``{vi_name: {...}}`` of live per-instrument status.
        seq: Monotonic sequence number.
        ts: Unix time the snapshot was taken.
    """

    kind: ClassVar[str] = "status_snapshot"

    state: str
    run: dict[str, Any] | None = None
    instruments: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Defensively copy and JSON-check the two mapping fields.

        Raises:
            TypeError: If either field is not a mapping of JSON-safe values.
        """
        if self.run is not None:
            object.__setattr__(self, "run", _checked_mapping(self.run))
        object.__setattr__(self, "instruments", _checked_mapping(self.instruments))


@dataclass(frozen=True)
class StationInfo(_ContractMessage):
    """What the station is, as declared — the static half of the picture.

    Captured by the Station at build and re-emitted on connect and disconnect.
    ``instruments`` is the JSON rendering of the VI declarations themselves:
    the name, type and offline state of each VI plus its ``@monitored`` fields
    and ``@control`` methods with their parameter specs, bounds, units and
    grouping. Both clients render *this*, never a hand-written description of
    any instrument — the GUI into instrument panels, the agent gateway into
    its capability manifest. The per-instrument dict's exact keys are the
    manifest builder's business, which is why this type constrains only that
    each entry is a JSON-safe dict.

    Attributes:
        instruments: One JSON-safe dict per VI, in station order.
        seq: Monotonic sequence number.
        ts: Unix time the declaration was captured.
    """

    kind: ClassVar[str] = "station_info"

    instruments: tuple[dict[str, Any], ...] = ()
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Freeze ``instruments`` into a tuple of JSON-checked dicts.

        Raises:
            TypeError: If it is not a sequence of mappings of JSON-safe
                values.
        """
        if isinstance(self.instruments, Mapping) or isinstance(self.instruments, str):
            raise TypeError("StationInfo.instruments must be a sequence of dicts")
        object.__setattr__(
            self,
            "instruments",
            tuple(_checked_mapping(entry) for entry in self.instruments),
        )


@dataclass(frozen=True)
class Readings(_ContractMessage):
    """One poll of the monitored fields of every instrument.

    Attributes:
        values: ``{vi_name: {field_name: value}}`` as monitored this tick.
        seq: Monotonic sequence number.
        ts: Unix time of the poll.
    """

    kind: ClassVar[str] = "readings"

    values: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Defensively copy and JSON-check ``values``.

        Raises:
            TypeError: If it is not a mapping of JSON-safe values.
        """
        object.__setattr__(self, "values", _checked_mapping(self.values))


@dataclass(frozen=True)
class Datapoint(_ContractMessage):
    """One measured point of the run in flight.

    Attributes:
        run_id: The run this point belongs to.
        index: The point's ordinal within the run, from zero.
        values: The datapoint's columns, JSON-safe.
        seq: Monotonic sequence number.
        ts: Unix time the point was measured.
    """

    kind: ClassVar[str] = "datapoint"

    run_id: str
    index: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Defensively copy and JSON-check ``values``.

        Raises:
            TypeError: If it is not a mapping of JSON-safe values.
        """
        object.__setattr__(self, "values", _checked_mapping(self.values))


@dataclass(frozen=True)
class RunStarted(_ContractMessage):
    """A run reached successful setup and is now producing data.

    Attributes:
        run_id: Identifier of the run that started.
        manifest: The run manifest, JSON-safe.
        actor: Who started it.
        request_id: The command that started it, or ``""``.
        seq: Monotonic sequence number.
        ts: Unix time of the start.
    """

    kind: ClassVar[str] = "run_started"

    run_id: str
    manifest: dict[str, Any] = field(default_factory=dict)
    actor: Actor = OPERATOR
    request_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Coerce the actor and JSON-check the manifest.

        Raises:
            TypeError: If ``actor`` or ``manifest`` has the wrong shape.
        """
        object.__setattr__(self, "actor", _as_actor(self.actor))
        object.__setattr__(self, "manifest", _checked_mapping(self.manifest))


@dataclass(frozen=True)
class RunFinished(_ContractMessage):
    """A run ended, however it ended.

    Attributes:
        run_id: Identifier of the run that finished.
        status: How it ended (``"completed"``, ``"aborted"``, ``"failed"``).
        reason: Human-readable explanation, ``""`` for a clean completion.
        manifest: The run manifest as finished, JSON-safe.
        seq: Monotonic sequence number.
        ts: Unix time of the end.
    """

    kind: ClassVar[str] = "run_finished"

    run_id: str
    status: str = ""
    reason: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """JSON-check the manifest.

        Raises:
            TypeError: If it is not a mapping of JSON-safe values.
        """
        object.__setattr__(self, "manifest", _checked_mapping(self.manifest))


@dataclass(frozen=True)
class QueueChanged(_ContractMessage):
    """The run queue's contents changed.

    Attributes:
        entries: One JSON-safe dict per queued run, in run order.
        actor: Who changed it.
        request_id: The command that changed it, or ``""``.
        seq: Monotonic sequence number.
        ts: Unix time of the change.
    """

    kind: ClassVar[str] = "queue_changed"

    entries: tuple[dict[str, Any], ...] = ()
    actor: Actor = OPERATOR
    request_id: str = ""
    seq: int = 0
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Freeze ``entries`` and coerce the actor.

        Raises:
            TypeError: If ``entries`` is not a sequence of JSON-safe dicts, or
                ``actor`` is neither an ``Actor`` nor its dict.
        """
        if isinstance(self.entries, Mapping) or isinstance(self.entries, str):
            raise TypeError("QueueChanged.entries must be a sequence of dicts")
        object.__setattr__(
            self, "entries", tuple(_checked_mapping(entry) for entry in self.entries)
        )
        object.__setattr__(self, "actor", _as_actor(self.actor))


#: Everything the engine broadcasts on its one event channel. A tagged union
#: rather than a base class: the members share no behaviour, only the
#: contract that each is frozen, JSON-safe and carries ``seq``/``ts``.
Event = (
    StateChange
    | StatusSnapshot
    | StationInfo
    | Readings
    | Datapoint
    | RunStarted
    | RunFinished
    | QueueChanged
)

# The discriminator table `event_from_json()` dispatches on. Built from the
# classes themselves so a new event type is registered by adding it here and
# nowhere else.
_EVENT_TYPES: dict[str, type] = {
    cls.kind: cls
    for cls in (
        StateChange,
        StatusSnapshot,
        StationInfo,
        Readings,
        Datapoint,
        RunStarted,
        RunFinished,
        QueueChanged,
    )
}


def event_from_json(payload: Mapping[str, Any]) -> Event:
    """Rebuild whichever event a JSON payload holds.

    Args:
        payload: A mapping as produced by an event's ``to_json()``, carrying
            the ``"kind"`` discriminator.

    Returns:
        The event, of the type ``"kind"`` names.

    Raises:
        KeyError: If the payload carries no ``"kind"``.
        ValueError: If ``"kind"`` names no known event type.
    """
    kind = payload["kind"]
    event_type = _EVENT_TYPES.get(kind)
    if event_type is None:
        raise ValueError(
            f"unknown event kind {kind!r}; known kinds: "
            f"{sorted(_EVENT_TYPES)}"
        )
    return event_type.from_json(payload)
