"""Orchestrator — cooperative state machine for CryoSoft.

The Orchestrator is single-threaded. The Qt event loop is the only
concurrency mechanism. It drives procedures via a state machine and
continually monitors the system.

Failure containment: every tick runs inside an exception boundary. PyQt6
aborts the whole process on an unhandled Python exception in a slot, which
for a cryostat controller would mean vanishing with the magnet still
ramping — so any unexpected exception instead closes the data file, stops
all ramps (hardware hold), and degrades to the ERROR state.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from cryosoft.core.availability import TAG_POLICY, Availability
from cryosoft.core.conditions import Condition, decide, envelope_conditions
from cryosoft.core.events import ErrorEvent
from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.core.operational_status import build_operational_status
from cryosoft.core.plan import Command, ExperimentEnvelope, Target
from cryosoft.core.ramps import RampRecord, build_ramp_records
from cryosoft.core.station import FaultRecord, Station
from cryosoft.core.tiered_trend_logger import TieredTrendLogger
from cryosoft.core.watchdog import WatchdogConfig, WatchdogState, apply_watchdog
# Procedures will be imported/type-checked but for now we expect a BaseProcedure mock.
# We don't import BaseProcedure directly to avoid circular dependency.

logger = logging.getLogger(__name__)


class OrchestratorState(Enum):
    """Possible states for the Orchestrator."""
    IDLE = "IDLE"
    INITIATING = "INITIATING"
    RAMPING = "RAMPING"
    INITIATION_GATE = "INITIATION_GATE"
    READING_GATE = "READING_GATE"
    MEASURING = "MEASURING"
    SWEEPING = "SWEEPING"
    STANDBY = "STANDBY"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    EMERGENCY = "EMERGENCY"


class Orchestrator(QObject):
    """State machine driving measurements and monitoring safety.

    Signals:
        states_updated (dict): Full station state emitted every monitored tick.
        monitoring_changed (bool): Emitted when monitoring starts (True) or
            stops (False) — the source of truth for GUI state like the
            Monitor window's monitoring toggle.
        state_changed (str): Emitted when orchestrator state changes. Not
            run-scoped — fires regardless of run kind.
        procedure_progress (float): 0.0 to 1.0 progress of the current run.
            PROCEDURE-EXCLUSIVE (plan operation-concurrency-and-error-
            scoping.md §2's hard status separation): never fires while an
            operation is the active run — see ``operation_progress``.
        procedure_finished (): Emitted when a PROCEDURE run ends cleanly.
            PROCEDURE-EXCLUSIVE: never emitted for an operation run (the
            Procedure window must stay blind to operation completions).
        operation_status (str): Concise, human-readable milestone of the
            running OPERATION — the same shape of message ``status_message``
            carries for a procedure, but routed here instead whenever the
            active run is an operation. Consumed by the Operations panel's
            OperationCard, never the Procedure window. Also written to the
            ``cryosoft.operation_status`` logger.
        operation_progress (float): 0.0 to 1.0 progress of the current
            OPERATION run — the operation-scoped counterpart of
            ``procedure_progress``.
        run_started (dict): Run manifest emitted once per run, after a
            procedure's/operation's ``initiate()`` succeeded and its plan was
            dispatched. Keys: ``run_id``, ``procedure`` (display name),
            ``kind`` ("run" for a procedure, "probe" for a probe run, and
            "operation" for an operation — its ``run_kind`` class attribute),
            ``params`` (merged parameter values), ``data_file`` (HDF5 path,
            captured here because the procedure closes its file before the
            run ends; empty for a dataset-less operation), and
            ``started_utc`` (ISO 8601). The session layer records runs from
            this signal; a run whose setup fails emits no manifest at all.
        run_finished (dict): The same manifest re-emitted exactly once when the
            run ends, with ``finished_utc``, terminal ``status`` (``done`` /
            ``aborted`` / ``failed``), ``reason`` (error text, empty for
            ``done``/``aborted``), ``postconditions_unmet`` (list of gate
            names an operation's one-shot ``postcondition_gates()``
            evaluation found unmet at finish — always ``[]`` for a procedure,
            or for an operation with none declared/all held), and ``summary``
            (the dict ``procedure.run_summary()`` returned — duck-typed, ``{}``
            for a procedure or an operation that does not override it, and
            ``{}`` rather than propagating if the override raised) added.
        error_occurred (str): Emitted when ERROR or EMERGENCY state entered,
            or a run fails. Not run-scoped — fires regardless of run kind.
            Kept as a
            thin compat wrapper: every emission here has a matching, richer
            ``error_event`` emitted alongside it.
        error_event (ErrorEvent): Structured counterpart of
            ``error_occurred``/a VI-scoped fault (``core.events.
            ErrorEvent``): ``vi_name`` (the originating instrument, or
            ``None``/comma-joined for a machine-wide or multi-VI event),
            ``kind`` (``"fault"`` — VI-scoped, quarantines only that VI;
            ``"run_failure"`` — the active run's claimed VI faulted, the run
            fails and the machine returns to IDLE; ``"safety"`` — a tripped
            safety flag, global EMERGENCY; ``"internal"`` — an unhandled
            tick-boundary exception, global ERROR), ``severity``
            (``"warning"``/``"error"``/``"emergency"``), ``message``, and
            ``timestamp``. A plain per-VI fault (``kind="fault"``,
            ``severity="warning"``) fires ONLY this signal, deliberately NOT
            ``error_occurred`` — mere staleness on an unclaimed VI was never
            an ``error_occurred``-worthy event before this plan and must not
            become banner-noisy in every window that still only listens to
            the compat signal (e.g. ProcedureWindow).
        action_blocked (str): Emitted if GUI action submitted while busy.
        action_succeeded (str, str): Emitted (vi_name, method_name) after a
            submit_vi_action() GUI action executes without raising — the
            source of truth for GUI state like InstrumentPanel's lifecycle
            toggle, which must reflect confirmed instrument state rather
            than an optimistic click.
        action_failed (str, str, str): Emitted (vi_name, method_name, reason)
            when a submitted GUI action raises — including a control-limits
            rejection or a VI safety guard (e.g. switch-heater mismatch).
            The reason string is the exception message, written by the VI to
            be shown to the user verbatim.
        status_message (str): Concise, human-readable milestone of the running
            PROCEDURE. Initiation is broken into one line per distinct setup
            action ("Ramping temperature to 300 K", "Ramping field to -1 T",
            "Arming DC resistance measurement"), followed by "Waiting N s at
            setpoint", "Measuring point 13/101", "Point 14/101: ramping field
            -> 0.55 T", etc. Labels/units come from each VI's setpoint metadata
            via the Station, so every procedure gets a status feed with no
            per-procedure code; consumed by the Procedure window's status log.
            Distinct from the per-tick detail stream on the Monitor log.
            PROCEDURE-EXCLUSIVE — see ``operation_status``.
    """

    states_updated = pyqtSignal(dict)
    monitoring_changed = pyqtSignal(bool)
    state_changed = pyqtSignal(str)
    procedure_progress = pyqtSignal(float)
    procedure_finished = pyqtSignal()
    run_started = pyqtSignal(dict)  # run manifest at successful setup
    run_finished = pyqtSignal(dict)  # same manifest + finished_utc/status/reason
    error_occurred = pyqtSignal(str)
    error_event = pyqtSignal(object)  # ErrorEvent — structured error/fault payload
    action_blocked = pyqtSignal(str)
    action_succeeded = pyqtSignal(str, str)
    action_failed = pyqtSignal(str, str, str)
    instrument_reconnected = pyqtSignal(str)  # offline VI brought live via connect_instrument()
    instrument_disconnected = pyqtSignal(str)  # live VI released via disconnect_instrument()
    measurement_ready = pyqtSignal(dict)  # emitted after each measure() with last_datapoint
    operational_status = pyqtSignal(dict)  # per-tick runtime status record (troubleshooting)
    ramps_updated = pyqtSignal(list)  # list[RampRecord] — every ramp running right now
    status_message = pyqtSignal(str)  # concise, human-readable PROCEDURE milestone line
    operation_status = pyqtSignal(str)  # concise, human-readable OPERATION milestone line
    operation_progress = pyqtSignal(float)  # 0.0-1.0 progress of the current OPERATION run

    def __init__(
        self,
        station: Station,
        tick_interval_ms: int = 3000,
        manual_override_timeout_s: float = 300.0,
    ) -> None:
        super().__init__()
        self._station = station
        self._state = OrchestratorState.IDLE
        self._procedure: Any = None
        self._procedure_queue: list[Any] = []
        # Operations (L4, duck-typed via command_scope == "operation") queue
        # separately and always drain first — see run_operation()/
        # queue_operation()/run_queue() and the "queue-jumping, not
        # preemption".
        self._operation_queue: list[Any] = []
        self._gui_action_queue: list[dict[str, Any]] = []
        self._active_system_vis: set[str] = set()

        self._wait_started = False
        self._wait_start_time = 0.0
        self._current_wait_time = 0.0
        self._standby_dispatched = False

        # Gate framework: procedure-declared waits that replace wait_s for the
        # RAMPING->MEASURING transition when the procedure declares any.
        # _first_measurement distinguishes the run's very first transition
        # (initiation_gates()) from every subsequent one (reading_gates()).
        self._pending_gates: list = []
        self._first_measurement = True

        # Set by run_operation() when the EMERGENCY carve-out was
        # used to start the active operation; read by _operation_end_state()
        # so a finishing operation returns to EMERGENCY rather than IDLE when
        # appropriate. Meaningless (and unread) for a plain procedure.
        self._operation_started_from_emergency: bool = False

        # The single acknowledge()-driven override, time-boxed rather than a
        # standing bypass (GLOSSARY.md's **Hold acknowledge**). Both windows
        # are read by _manual_action_admissible() and reset by acknowledge();
        # _emergency_override_until additionally resets to None on every
        # fresh EMERGENCY entry (_enter_emergency()) and on the eventual
        # return to IDLE (_acknowledge_emergency()), so a new emergency
        # always starts locked.
        #
        # _emergency_override_until: set by acknowledge() while in EMERGENCY
        # and its condition is still active: unlocks submit_vi_action() for
        # manual front-panel recovery (e.g. cycling a switch heater by hand)
        # without leaving EMERGENCY. Procedures and operations stay refused
        # regardless — their gates check self._state, not this window.
        self._emergency_override_until: float | None = None
        # _hold_override_until: condition.key -> expiry unix timestamp, one
        # entry per currently-acknowledged hold-severity condition (e.g.
        # "safety:helium_low"). Pruned by expiry only (never merely because
        # the condition cleared) — see _tick_body()'s pruning step — so a
        # flapping flag doesn't force a fresh acknowledge on every re-trip
        # within the same window.
        self._hold_override_until: dict[str, float] = {}
        self._manual_override_timeout_s = float(manual_override_timeout_s)

        # Scanner (switch VI) availability: an on/off flag procedures check
        # via Station.scanner_enabled() rather than assuming the first switch
        # VI a station exposes is theirs to use. Resolved once at construction
        # (the Station is fully built before the Orchestrator is).
        switch_names = station.switch_vi_names()
        self._scanner_vi_name: str | None = switch_names[0] if switch_names else None

        # Operational-status reporting (runtime troubleshooting signal).
        self._state_entered_at = time.time()
        self._prev_gaps: dict[str, float] = {}
        self._operational_status: dict = {}
        self._status_logger = logging.getLogger("cryosoft.status")
        self._watchdog_state = WatchdogState()
        self._watchdog_config = WatchdogConfig()

        # Tiered trend-history writer: the disk-backed store of per-tick
        # station readings, downsampled live into the raw/3min/hourly tiers
        # (see TieredTrendLogger's docstring and GLOSSARY.md "Trend tier").
        # No arguments: its defaults resolve the cryosoft.trend_* loggers
        # already configured by setup_logging(), so the Orchestrator never
        # needs to know where trend logs live.
        self._tiered_trend_logger = TieredTrendLogger()

        # Session envelope (sample-specific bounds, narrower than config
        # limits) — set by the session layer, enforced here so it binds every
        # writer (GUI and agents alike). None = no envelope active.
        self._session_envelope: ExperimentEnvelope | None = None

        # Active run manifest: captured at run_started (the data file path is
        # gone by the time the run ends) and re-emitted once on run_finished.
        self._active_run_manifest: dict[str, Any] | None = None
        self._run_counter = 0

        # run_summary() hand-off: collected from self._procedure by
        # _emit_run_finished() for the "done" path, where self._procedure is
        # still set. The abort/fail/emergency paths clear self._procedure in
        # _abort_active_procedure() BEFORE calling _emit_run_finished(), so
        # that method caches the summary here first — _emit_run_finished()
        # prefers self._procedure when present, else falls back to this.
        self._pending_run_summary: dict[str, Any] = {}

        # Claims + admission gate: the active run's claimed_vi_names(),
        # captured once at _start_run() and cleared on EVERY teardown path
        # (finish, abort, fail, emergency — see _abort_active_procedure()/
        # _finish_run()). None while no run is active, or while the active
        # run claims everything (the default for every procedure and for an
        # operation that does not override claimed_vi_names()).
        self._active_claims: set[str] | None = None

        # Condition-registry onset tracking (the System-Condition standard,
        # core/conditions.py): the set of Station condition keys
        # (comm:<vi_name>, safety:<flag>) active as of the last tick, used
        # to detect NEW conditions — a new comm condition on an unwatched
        # VI emits one warning error_event (not one per tick); a new
        # hold-severity condition dispatches one standby() to each VI it
        # affects (not one per tick a hold persists). Replaces the old
        # separate _known_fault_vis/_known_safety_holds trackers — one key
        # set now serves both, since a comm condition IS hold-severity
        # exactly like a safety-hold condition. Station is the source of
        # truth; this is only a transition-detection cache.
        self._known_condition_keys: set[str] = set()

        # Ramp tracking (see _publish_ramps): the running-ramp records as of
        # the last tick, and the per-tick memo of the Station snapshot they
        # and the operational-status record are both built from. The memo is
        # cleared at the top of every _tick_body() so a tick polls each VI's
        # ramp state exactly once no matter how many consumers read it.
        self._active_ramps: list[RampRecord] = []
        self._tick_ramp_info: dict[str, dict] | None = None

        self._pre_pause_state = OrchestratorState.IDLE
        self._paused_wait_elapsed = 0.0
        # Last targets dispatched to the Station — re-dispatched on resume,
        # because pause_procedure() holds the hardware (which forgets its ramp).
        self._last_system_targets: dict[str, Target] = {}

        # Monitoring starts OFF: instruments are not polled until
        # start_monitoring() is called (typically from the Monitor window,
        # after the instruments have been initiated), so a fresh launch does
        # not immediately fire communication errors at not-yet-ready hardware.
        # The tick timer itself always runs — it is what processes GUI actions
        # (including "Initiate All") and drives the state machine.
        self._monitoring = False

        self._timer = QTimer(self)
        self._timer.setInterval(tick_interval_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------
    # Public API — monitoring lifecycle
    # ------------------------------------------------------------------

    def is_monitoring(self) -> bool:
        """Return True while the per-tick monitoring cycle is active."""
        return self._monitoring

    @property
    def state(self) -> str:
        """Current state machine value (e.g. ``"IDLE"``, ``"EMERGENCY"``).

        The GUI's only sanctioned way to read current state: widgets whose
        visibility depends on state (e.g. the Acknowledge-Emergency button)
        must read this once at construction time to sync with a state
        entered before they existed — ``state_changed`` alone only reports
        *future* transitions.
        """
        return self._state.value

    def start_monitoring(self) -> bool:
        """Begin the per-tick monitoring cycle (state polling + safety watchdog).

        Idempotent. Until this is called, ticks process GUI actions and the
        state machine but touch no instrument — call it once the instruments
        have been initiated and are ready to be polled.

        Returns:
            True (monitoring is active when this returns).
        """
        if self._monitoring:
            return True
        self._monitoring = True
        logger.info("Monitoring started")
        self._emit_status("Monitoring started")
        self.monitoring_changed.emit(True)
        return True

    def stop_monitoring(self) -> bool:
        """Stop the per-tick monitoring cycle (e.g. to debug an instrument).

        Refused (with an ``action_blocked`` signal) outside IDLE/ERROR: while
        a procedure runs or hardware ramps, the safety watchdog and stale
        detection that live in the monitoring cycle must keep running.
        Idempotent when already stopped.

        Returns:
            True if monitoring is stopped when this returns, False if the
            request was refused.
        """
        if not self._monitoring:
            return True
        if self._state not in (OrchestratorState.IDLE, OrchestratorState.ERROR):
            msg = (
                f"Cannot stop monitoring in state {self._state.name}: "
                "the safety watchdog must keep running while hardware is active."
            )
            logger.info("Blocked stop_monitoring: %s", msg)
            self.action_blocked.emit(msg)
            return False
        self._monitoring = False
        logger.info("Monitoring stopped")
        self._emit_status("Monitoring stopped")
        self.monitoring_changed.emit(False)
        return True

    def shutdown(self) -> None:
        """Stop the tick timer permanently (application exit / test teardown).

        After this no tick ever fires again: no polling, no action
        processing, no state-machine advancement. Used by tests to guarantee
        a tick can never land while the GUI widget tree is being destroyed.
        Idempotent.
        """
        self._timer.stop()
        logger.info("Orchestrator shut down — tick timer stopped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_experiment_envelope(self, envelope: ExperimentEnvelope | None) -> None:
        """Install (or clear) the active experiment's session envelope.

        Called by the session layer on experiment start/close. Config
        ``init_params`` limits protect the instrument; the envelope protects
        the mounted sample with narrower per-experiment bounds. Enforcement
        happens here in the Orchestrator — the single choke point every writer
        goes through — in two places: every submitted ``Target`` for a bounded
        VI is validated before dispatch, and every tick each bound with a
        ``state_key`` is checked against the VI's live reading (a violation
        enters EMERGENCY exactly like a tripped safety flag).

        Args:
            envelope: The bounds to enforce, or ``None`` to clear them.
        """
        self._session_envelope = envelope
        if envelope is None:
            logger.info("Session envelope cleared")
        else:
            logger.info("Session envelope set: %r", envelope)

    def run_procedure(self, procedure: Any) -> None:
        """Start a procedure immediately if IDLE or during a manual ramp; else queue it.

        Any exception during setup (initiate(), target dispatch) is contained:
        the partially-started run is cleaned up (data file closed, ramps
        stopped) and the Orchestrator degrades to ERROR instead of crashing
        the application.
        """
        # A magnet in manual persistent mode means the user is driving its
        # switch heater / PSU by hand; a procedure must not run over that.
        persistent_magnets = self._station.persistent_mode_magnets()
        if persistent_magnets:
            msg = (
                "Cannot start a procedure while a magnet is in persistent mode "
                f"({', '.join(persistent_magnets)}). Disable persistent mode first."
            )
            logger.info("Blocked run_procedure: %s", msg)
            self.action_blocked.emit(msg)
            return

        manual_ramping = (
            self._state == OrchestratorState.RAMPING and self._procedure is None
        )
        if self._state != OrchestratorState.IDLE and not manual_ramping:
            self.queue_procedure(procedure)
            return

        # Cancel any manual ramp before starting the procedure (hardware
        # holds; the procedure's own targets take over immediately below).
        if manual_ramping:
            self._station.stop_ramps()
            logger.info("Manual ramp cancelled — procedure starting.")

        # A run without the per-tick safety watchdog and stale detection would
        # be blind to a quench or a dead controller, so monitoring is mandatory
        # while a procedure executes.
        if not self._monitoring:
            logger.info("run_procedure: monitoring was off — starting it (required during a run)")
            self.start_monitoring()

        self._operation_started_from_emergency = False
        self._start_run(procedure, kind="procedure")

    def run_operation(self, operation: Any) -> None:
        """Start an operation immediately if permitted; else refuse it.

        Allowed from IDLE, from a manual ramp (cancelled first, exactly like
        ``run_procedure()``), and — the narrow EMERGENCY carve-out — from
        EMERGENCY iff every currently tripped safety flag (from
        ``Station.check_safety()``) is in the operation's
        ``tolerated_safety_flags``. This check alone decides whether the
        operation is allowed to START; it does not exempt the operation
        from the tick's own EMERGENCY-entry check afterwards, which reads
        the unconditional System-Condition registry (``core/
        conditions.py``: critical severity is never tolerated, station-wide
        by construction) rather than this method's tolerance comparison. In
        practice this means the carve-out only lets an operation keep
        running past the start if the flag it tolerated is hold-severity
        (e.g. ``helium_low``) or the EMERGENCY cause has actually cleared —
        tolerating a still-tripped CRITICAL flag (e.g. ``quench``) here gets
        the operation started, but the very next tick's EMERGENCY check
        aborts it again, since that check answers "is anything critical
        active", never "does the active run tolerate it".

        Unlike ``run_procedure()``, a busy Orchestrator never auto-queues the
        request: a running procedure (or operation) is NEVER auto-aborted, so
        the refusal (``action_blocked``) tells the caller to abort it first.
        Use ``queue_operation()`` to queue explicitly — queued operations
        always run ahead of queued procedures (see ``run_queue()``).

        Any exception during setup is contained exactly like
        ``run_procedure()`` — degrades to ERROR rather than crashing.
        """
        manual_ramping = (
            self._state == OrchestratorState.RAMPING and self._procedure is None
        )
        started_from_emergency = False

        if self._state == OrchestratorState.EMERGENCY:
            tolerated = frozenset(getattr(operation, "tolerated_safety_flags", frozenset()))
            safety = self._station.check_safety()
            active = {flag for flag, tripped in safety.items() if tripped}
            untolerated = sorted(active - tolerated)
            if untolerated:
                msg = (
                    "Cannot start operation from EMERGENCY: active safety "
                    f"condition(s) not tolerated by this operation "
                    f"({', '.join(untolerated)}). Resolve them first."
                )
                logger.info("Blocked run_operation: %s", msg)
                self.action_blocked.emit(msg)
                return
            started_from_emergency = True
        elif not (self._state == OrchestratorState.IDLE or manual_ramping):
            running_label = "procedure"
            if self._procedure is not None and (
                getattr(self._procedure, "command_scope", "measurement") == "operation"
            ):
                running_label = "operation"
            msg = (
                f"Cannot start operation: a {running_label} is running "
                f"(state {self._state.name}). Abort it first, then start the "
                "operation."
            )
            logger.info("Blocked run_operation: %s", msg)
            self.action_blocked.emit(msg)
            return

        if manual_ramping:
            self._station.stop_ramps()
            logger.info("Manual ramp cancelled — operation starting.")

        # A run without the per-tick safety watchdog and stale detection would
        # be blind to a quench or a dead controller, so monitoring is mandatory
        # while an operation executes.
        if not self._monitoring:
            logger.info("run_operation: monitoring was off — starting it (required during a run)")
            self.start_monitoring()

        self._operation_started_from_emergency = started_from_emergency
        self._start_run(operation, kind="operation")

    def _start_run(self, procedure: Any, *, kind: str = "procedure") -> None:
        """Shared setup path for ``run_procedure()``/``run_operation()``.

        Dispatches ``procedure.initiate()``'s plan (scope-checked via the
        procedure's own ``command_scope``, defaulting to "measurement" for a
        plain procedure), enters INITIATING, and emits the run-started
        manifest. Any exception is contained to ERROR — the caller must have
        already confirmed permission to start (queueing, EMERGENCY
        carve-out, monitoring) before calling this.

        Args:
            procedure: The procedure or operation to start.
            kind: ``"procedure"`` or ``"operation"``, used only for logging
                and the error message on setup failure.
        """
        self._procedure = procedure
        # Claims + admission gate: captured here, duck-typed (never
        # importing BaseProcedure/OperationBase — contract C5) so a test
        # double without claimed_vi_names() behaves exactly like the
        # claim-everything default.
        claimed_vi_names = getattr(procedure, "claimed_vi_names", None)
        self._active_claims = claimed_vi_names() if callable(claimed_vi_names) else None
        self._standby_dispatched = False
        self._wait_started = False
        self._first_measurement = True
        self._pending_gates = []
        try:
            plan = procedure.initiate()
            # The frozen-dataclass repr is the permanent record of exactly what
            # was requested — logged once, at INFO, on receipt.
            logger.info("%s plan (initiate): %r", kind.capitalize(), plan)

            # Track active system VIs for stale monitoring
            self._active_system_vis = set(plan.targets.keys())

            self._dispatch_targets(plan.targets)
            allowed_scope = getattr(procedure, "command_scope", "measurement")
            self._station.send_measurement_commands(plan.commands, allowed_scope=allowed_scope)
            self._current_wait_time = plan.wait_s

            self._change_state(OrchestratorState.INITIATING)
            self._emit_run_started()
            self._emit_initiation_status(plan.targets, plan.commands)
        except Exception as exc:
            logger.exception("%s setup failed", kind)
            self._fail_to_error(f"Could not start {kind}: {exc}")

    def _current_gates(self) -> tuple:
        """Return the gates for the current RAMPING->MEASURING transition.

        ``initiation_gates()`` for the run's first transition,
        ``reading_gates()`` for every one after. Looked up defensively
        (``getattr`` with an empty-tuple default) so a duck-typed procedure
        test double without these methods behaves exactly like the no-op
        ``BaseProcedure`` default.
        """
        if self._procedure is None:
            return ()
        method_name = "initiation_gates" if self._first_measurement else "reading_gates"
        method = getattr(self._procedure, method_name, None)
        if method is None:
            return ()
        return tuple(method())

    def _dispatch_targets(self, targets: dict[str, Target]) -> None:
        """Forward targets to the Station, remembering them for resume.

        Raises:
            CryoSoftSafetyError: If a target for a bounded VI falls outside the
                active session envelope. Nothing is dispatched — the whole
                plan is rejected before any hardware is touched, and the tick
                boundary (or ``run_procedure``'s setup guard) contains the run
                to ERROR with the reason.
        """
        if self._session_envelope is not None:
            for vi_name, target in targets.items():
                message = self._session_envelope.check_target(vi_name, target.target)
                if message is not None:
                    raise CryoSoftSafetyError(message)
        self._last_system_targets = dict(targets)
        self._station.process_system_targets(targets)

    def queue_procedure(self, procedure: Any) -> None:
        """Add procedure to queue."""
        self._procedure_queue.append(procedure)

    def queue_operation(self, operation: Any) -> None:
        """Queue an operation to run once the Orchestrator returns to IDLE.

        Operations queue separately from procedures and always drain first
        (see ``run_queue()``) — the queueing half of "queue-jumping, not
        preemption".
        """
        self._operation_queue.append(operation)

    def run_queue(self) -> None:
        """Run the next queued operation, else the next queued procedure, if IDLE.

        Operations always drain before procedures.
        """
        if self._state != OrchestratorState.IDLE:
            return
        if self._operation_queue:
            self.run_operation(self._operation_queue.pop(0))
            return
        if self._procedure_queue:
            self.run_procedure(self._procedure_queue.pop(0))

    def pause_procedure(self) -> None:
        """Pause the current procedure and hold all hardware where it is.

        Pausing stops the physical ramps (not just the schedule): a magnet
        PSU ramps autonomously to its last setpoint, so without a hardware
        hold "pause" would only stop the software while the field kept
        moving. The wait clock is also frozen and restored on resume.
        """
        if self._procedure is None:
            return
        if self._state in (OrchestratorState.INITIATING, OrchestratorState.RAMPING,
                           OrchestratorState.INITIATION_GATE, OrchestratorState.READING_GATE,
                           OrchestratorState.MEASURING, OrchestratorState.SWEEPING,
                           OrchestratorState.STANDBY):
            self._pre_pause_state = self._state
            if self._wait_started:
                self._paused_wait_elapsed = time.time() - self._wait_start_time
            self._station.stop_ramps(self._active_system_vis or None)
            self._change_state(OrchestratorState.PAUSED)
            self._emit_status("Paused - hardware held")

    def resume_procedure(self) -> None:
        """Resume from PAUSED: restart held ramps and unfreeze the wait clock."""
        if self._state != OrchestratorState.PAUSED:
            return
        # pause_procedure() held the hardware, which forgot its ramp — states
        # that were mid-ramp need their targets re-dispatched to continue.
        if self._pre_pause_state in (
            OrchestratorState.INITIATING,
            OrchestratorState.RAMPING,
            OrchestratorState.STANDBY,
        ) and self._last_system_targets:
            self._dispatch_targets(self._last_system_targets)
        if self._wait_started:
            self._wait_start_time = time.time() - self._paused_wait_elapsed
        self._change_state(self._pre_pause_state)
        self._emit_status("Resumed")

    def abort_procedure(self) -> None:
        """Abort the run: hold instruments where they are (no ramp-to-zero).

        Closes the data file (partial data preserved), sends the procedure's
        measurement safe-off commands, stops all active ramps with a hardware
        hold, and returns to IDLE. Ignored during EMERGENCY — the emergency
        flow owns cleanup there and is exited via acknowledge().
        """
        if self._state == OrchestratorState.EMERGENCY:
            logger.info("abort_procedure ignored during EMERGENCY")
            return
        self._abort_active_procedure()
        self._emit_run_finished("aborted")
        self._change_state(OrchestratorState.IDLE)
        self._emit_status("Aborted by user")
        self.run_queue()

    def active_ramps(self) -> list[RampRecord]:
        """Return the ramps running as of the last tick, ordered by VI name.

        The read half of the ramp-tracker surface (``ramps_updated`` is the
        push half — same payload, same objects). Returns the CACHED records
        built during the tick, so calling this from GUI code costs nothing
        and, crucially, touches no instrument: the tick is the only thing
        that polls hardware.

        Returns:
            One ``RampRecord`` per system VI whose ``ramp_status()`` was
            ``"RAMPING"`` at the last tick, ordered by VI name. Empty when
            nothing is ramping.
        """
        return list(self._active_ramps)

    def stop_ramp(self, vi_name: str) -> None:
        """Stop one VI's ramp, holding that instrument where it is.

        The per-instrument counterpart of ``abort_procedure()``: it stops a
        single ramp instead of tearing down the whole run, so an operator can
        cancel a manual field or temperature ramp started from the Monitor
        window without stopping anything else. Hardware is held where it is
        — there is no ramp-to-zero, exactly like an abort.

        Admission goes through ``_manual_action_admissible()``, the same
        predicate that governs every other manual action: a faulted or
        safety-held VI is refused, ERROR and (unless unlocked) EMERGENCY are
        refused, and — the case that matters here — a VI claimed by an active
        run is refused naming that run. Stopping one VI's ramp mid-run would
        strand the run waiting on a setpoint it can never reach, so aborting
        the run is the only correct way to stop a run's ramp.

        Unlike ``submit_vi_action()`` this is not queued for the next tick:
        a stop is a safety action and follows ``abort_procedure()``'s
        precedent of holding the hardware immediately. The state machine
        still owns the RAMPING → IDLE transition, which happens on the next
        tick once every remaining ramp has settled.

        Args:
            vi_name: The system VI whose ramp to stop.
        """
        admitted, reason = self._manual_action_admissible(vi_name)
        if not admitted:
            logger.info("Blocked stop_ramp on %s: %s", vi_name, reason)
            self.action_blocked.emit(reason)
            return
        try:
            self._station.stop_ramps({vi_name})
        except Exception as exc:  # noqa: BLE001 — every action gets a verdict
            logger.exception("stop_ramp failed on VI '%s'", vi_name)
            self.action_failed.emit(vi_name, "stop_ramp", str(exc))
            return
        logger.info("Ramp on '%s' stopped by user — hardware held", vi_name)
        self.action_succeeded.emit(vi_name, "stop_ramp")
        self._emit_status(f"Ramp on {vi_name} stopped by user")
        # Drop the row from the tracker immediately rather than leaving a
        # stopped ramp on screen until the next tick rebuilds the snapshot.
        self._active_ramps = [r for r in self._active_ramps if r.vi_name != vi_name]
        self.ramps_updated.emit(list(self._active_ramps))

    def finish_operation(self) -> None:
        """Request a graceful stop of the active operation.

        Calls ``request_finish()`` on the active operation so its next
        ``change_sweep_step()`` (the ``OperationBase`` adapter) returns
        ``None`` regardless of what ``step()`` would return, ending an
        open-ended operation and running the normal
        STANDBY -> postcondition path. Refused with ``action_blocked`` if no
        operation is currently active (a duck-typed procedure without
        ``command_scope == "operation"`` does not count).
        """
        if not self._is_operation_active():
            msg = "Cannot finish operation: no operation is currently running."
            logger.info("Blocked finish_operation: %s", msg)
            self.action_blocked.emit(msg)
            return
        request_finish = getattr(self._procedure, "request_finish", None)
        if callable(request_finish):
            request_finish()
        self._emit_status("Finish requested — completing operation")

    def confirm_operation(self, key: str) -> None:
        """Record an operator confirmation on the active operation.

        Mirrors ``finish_operation()``: calls ``confirm(key)`` on the active
        operation (duck-typed — a plain procedure or an operation without a
        ``confirm`` method is simply ignored) so a subsequent
        ``postcondition_gates()`` check reading ``confirmed(key)`` sees the
        flag. Refused with ``action_blocked`` if no operation is currently
        active (a duck-typed procedure without ``command_scope ==
        "operation"`` does not count).

        Args:
            key: The confirmation key (e.g. ``"needle_valve"``), forwarded
                verbatim to the operation's ``confirm()``.
        """
        if not self._is_operation_active():
            msg = "Cannot confirm operation step: no operation is currently running."
            logger.info("Blocked confirm_operation: %s", msg)
            self.action_blocked.emit(msg)
            return
        confirm = getattr(self._procedure, "confirm", None)
        if callable(confirm):
            # Guarded: this is called directly from GUI code, where an
            # unhandled exception in a Qt slot would abort the process. An
            # undeclared key is refused with a verdict, never raised.
            try:
                confirm(key)
            except Exception as exc:  # noqa: BLE001 — verdict, not crash
                logger.error("confirm_operation(%r) rejected: %s", key, exc)
                self.action_blocked.emit(f"Cannot confirm {key!r}: {exc}")
                return
        self._emit_status(f"Confirmed: {key}")

    def skip_operation_step(self, key: str) -> None:
        """Record that the operator deliberately skipped a step of the active operation.

        The counterpart to ``confirm_operation()``: calls ``skip_step(key)``
        on the active operation (duck-typed, so an operation that declares
        no steps is simply ignored), which records the skip as an override
        rather than a failure. The GUI is responsible for warning the
        operator first; by the time this is called the decision has been
        made, and it always succeeds for a declared, skippable step.

        Skipping a step the *system* was carrying out has a second half that
        only the Orchestrator can do. An ``auto_ramp`` step is a ramp this
        operation dispatched, and while that ramp runs the state is RAMPING,
        where the operation's ``step()`` is never called — so the operation
        cannot retarget or stop its own ramp, and ``stop_ramp()`` refuses a
        VI claimed by an active run. The operation therefore raises a flag
        (``skip_ramp_requested``) and ``_tick_body()``'s RAMPING branch
        stops the ramp in place, leaving the instrument clamped where it had
        reached. Doing it there rather than here keeps every hardware write
        on the tick, which is the single-writer rule.

        Args:
            key: The step key, forwarded verbatim to the operation's
                ``skip_step()``.
        """
        if not self._is_operation_active():
            msg = "Cannot skip operation step: no operation is currently running."
            logger.info("Blocked skip_operation_step: %s", msg)
            self.action_blocked.emit(msg)
            return
        skip_step = getattr(self._procedure, "skip_step", None)
        if callable(skip_step):
            # Guarded exactly like confirm_operation(): called straight from
            # a Qt slot, so an undeclared or unskippable key becomes a
            # verdict, never an unhandled exception in the GUI thread.
            try:
                skip_step(key)
            except Exception as exc:  # noqa: BLE001 — verdict, not crash
                logger.error("skip_operation_step(%r) rejected: %s", key, exc)
                self.action_blocked.emit(f"Cannot skip {key!r}: {exc}")
                return
        logger.warning(
            "Operator skipped step %r of %s — recorded as an override.",
            key,
            getattr(self._procedure, "name", type(self._procedure).__name__),
        )
        self._emit_status(f"Skipped: {key}")

    def _stop_ramps_for_skipped_step(self) -> bool:
        """Stop the active run's ramps in place if a skipped step asked for it.

        Reads and clears the active operation's ``skip_ramp_requested``
        flag. ``Station.stop_ramps()`` is a hold-in-place — the same call
        ``pause_procedure()`` makes — so the instrument stays wherever the
        ramp had reached rather than returning anywhere, which is exactly
        what "skip the warm-up" should mean: stop climbing, hold here.

        Only VIs in ``_active_system_vis`` are stopped. That set is built
        from the run's plan *targets* (see ``_start_procedure``), so for a
        sample-access operation it is the VTI alone; magnets, dispatched as
        commands, keep ramping down to zero field, which is both safe and
        wanted — skipping the warm-up is not a reason to leave the magnet
        energised while the cryostat is opened.

        Returns:
            True if a skip was pending and the ramps were stopped, False
            otherwise (the overwhelmingly common case, one attribute read).
        """
        if not self._is_operation_active():
            return False
        if not getattr(self._procedure, "skip_ramp_requested", False):
            return False
        self._procedure.skip_ramp_requested = False
        self._station.stop_ramps(self._active_system_vis or None)
        logger.warning(
            "Stopped the active run's ramps in place: the operator skipped "
            "the step that started them."
        )
        self._emit_status("Ramp stopped — step skipped, holding here")
        return True

    def recover_from_error(self) -> None:
        """Return to IDLE after the user has reviewed an ERROR condition.

        The failed procedure was already cleaned up on ERROR entry. Queued
        procedures are NOT auto-started — after an error the queue's
        assumptions may no longer hold; the user restarts explicitly.
        """
        if self._state == OrchestratorState.ERROR:
            self._change_state(OrchestratorState.IDLE)

    def acknowledge(self) -> None:
        """Single GUI entry point: acknowledge EMERGENCY, or unlock held VIs.

        Grants manual control for ``self._manual_override_timeout_s`` seconds
        from this call, then automatically re-locks — never a standing
        bypass (GLOSSARY.md's **Hold acknowledge**). While in EMERGENCY,
        also runs ``_acknowledge_emergency()``'s existing two-stage
        state-recovery logic (return to IDLE once the triggering condition
        has cleared); the state itself is untouched otherwise — a hold-only
        acknowledge never fakes IDLE while a hold is still genuinely active,
        it only grants temporary manual access on top of whatever state is
        honestly still true. Re-acknowledging while already unlocked simply
        extends the window from this call; it does not stack, and (for the
        hold case) survives the condition flapping within the window — see
        ``_tick_body()``'s pruning step.

        No-op if nothing is held and the station isn't in EMERGENCY.
        """
        now = time.time()
        if self._state == OrchestratorState.EMERGENCY:
            self._emergency_override_until = now + self._manual_override_timeout_s
            self._acknowledge_emergency()
            return
        held = self._held_vis()
        if not held:
            return
        until = now + self._manual_override_timeout_s
        for condition in held.values():
            self._hold_override_until[condition.key] = until
        self._emit_status(
            "Holds acknowledged — manual control unlocked for "
            f"{self._manual_override_timeout_s:.0f}s: {', '.join(sorted(held))}"
        )

    def _acknowledge_emergency(self) -> None:
        """Acknowledge an EMERGENCY: unlock manual control, or return to IDLE.

        Implementation detail of ``acknowledge()`` — not called directly
        except by tests exercising EMERGENCY-only two-stage semantics in
        isolation.

        If the condition that triggered EMERGENCY (a critical safety flag —
        ``Station.active_critical_conditions()``, the System-Condition
        standard's own live registry, see ``core/conditions.py`` — or a
        session-envelope violation) is still active, acknowledging cannot
        return to IDLE (the next tick would bounce straight back), but it
        does unlock manual control of held VIs (via
        ``_manual_action_admissible()``'s override bypass) for front-panel
        recovery — e.g. cycling a switch heater by hand — while remaining in
        EMERGENCY. Starting a procedure or operation stays refused
        throughout: those gates check the state itself, which is unchanged
        here.

        A merely hold-only flag (e.g. ``helium_low``) never blocks this
        return: it was never why EMERGENCY was entered, and it keeps
        governing its concerned VIs identically in IDLE (the System-
        Condition standard applies in every state, not only EMERGENCY).

        Once the condition has cleared, acknowledging again (the same
        button, via ``acknowledge()``) returns to IDLE and relocks the
        override for the next emergency.
        """
        if self._state != OrchestratorState.EMERGENCY:
            return
        active = [c.kind for c in self._station.active_critical_conditions()]
        # A still-violated session envelope blocks the return to IDLE for the
        # same reason a critical safety flag does — the next tick would
        # bounce straight back.
        if self._session_envelope is not None:
            active.extend(self._session_envelope.check_state(self._station.cached_state))
        if active:
            self._emit_status(
                "Emergency acknowledged — front-panel manual control "
                f"unlocked. Condition still active: {', '.join(active)}"
            )
            return
        self._emergency_override_until = None
        self._change_state(OrchestratorState.IDLE)

    def held_vi_names(self) -> frozenset[str]:
        """Every VI currently under a hold-severity safety condition."""
        return frozenset(self._held_vis())

    def override_active(self, vi_name: str | None = None) -> bool:
        """True if a manual override currently admits action on *vi_name*.

        With ``vi_name=None``, True if the EMERGENCY override is currently
        unlocked. Tests and GUI code should assert/poll through this (or
        ``manual_override_expires_at()``) rather than reading
        ``_emergency_override_until``/``_hold_override_until`` directly.
        """
        now = time.time()
        if vi_name is None:
            return (
                self._emergency_override_until is not None
                and now < self._emergency_override_until
            )
        held = self._held_vis().get(vi_name)
        if held is None:
            return False
        return now < self._hold_override_until.get(held.key, 0.0)

    def manual_override_expires_at(self) -> float | None:
        """Soonest-expiring active override (EMERGENCY or hold), or ``None``."""
        candidates = list(self._hold_override_until.values())
        if self._emergency_override_until is not None:
            candidates.append(self._emergency_override_until)
        return min(candidates) if candidates else None

    def active_run_kind(self) -> str | None:
        """Return the active run's kind, or ``None`` if no run is active.

        The public, duck-type-free accessor GUI code uses to tell a
        procedure run from an operation run (hard status separation, plan
        operation-concurrency-and-error-scoping.md §2) without reaching into
        ``self._procedure`` or importing ``OperationBase``/``BaseProcedure``
        (contracts C5/C8).

        Returns:
            ``"operation"`` while an operation is the active run,
            ``"procedure"`` while anything else (a plain procedure, or a
            test double without ``command_scope``) is, or ``None`` while no
            run is active.
        """
        if self._procedure is None:
            return None
        return "operation" if self._is_operation_active() else "procedure"

    def _is_operation_active(self) -> bool:
        """Return True while the active run is an operation (duck-typed).

        Never imports ``OperationBase`` (contract C5) — reads
        ``command_scope`` exactly like every other operation/procedure
        branch in this module.
        """
        return (
            self._procedure is not None
            and getattr(self._procedure, "command_scope", "measurement") == "operation"
        )

    def _active_run_label(self) -> str:
        """Return a human-readable ``"<kind> '<name>'"`` label for the active run.

        Used only to compose admission-refusal messages — never
        called with no active run.

        Returns:
            E.g. ``"operation 'Helium Fill'"`` or ``"procedure 'Field Sweep'"``.
        """
        procedure = self._procedure
        kind = (
            "operation"
            if getattr(procedure, "command_scope", "measurement") == "operation"
            else "procedure"
        )
        name = getattr(procedure, "name", "") or type(procedure).__name__
        return f"{kind} {name!r}"

    def _held_vis(self) -> dict[str, Condition]:
        """Return ``{vi_name: Condition}`` for every VI currently held.

        Reads the Station's unified condition registry (the
        System-Condition standard, ``core/conditions.py``) through
        ``decide()`` — the same pure policy the tick pipeline uses to
        collapse the same information — rather than keeping a second,
        parallel bookkeeping structure. ``watched_vis``/``run_active`` are
        irrelevant to ``held_vis`` (only ``run_failure`` depends on them),
        so dummy values are passed.

        Returns:
            The alphabetically-first (by ``Condition.key``) hold-severity
            condition affecting each held VI, comm- or safety-origin alike.
        """
        return decide(
            self._station.conditions().values(), watched_vis=frozenset(), run_active=False
        ).held_vis

    def _manual_action_admissible(self, vi_name: str) -> tuple[bool, str]:
        """Decide whether a manual action on *vi_name* may be admitted right now.

        The single admission predicate (the "Claims + admission gate"),
        shared verbatim by ``submit_vi_action()`` (what may be *queued*) and
        the ``_tick_body()`` GUI-action drain gate (what may be *drained*) —
        they must agree, or a queued action could sit forever without a
        verdict.

        Admission rules, in order:

        0. A VI carrying the Availability standard's ``not_responding`` tag
           (``cryosoft.core.availability`` — a comm-origin condition, an
           instrument fault, GLOSSARY.md's **Instrument fault**) is ALWAYS
           refused, regardless of state — including IDLE, and NEVER bypassed
           by the EMERGENCY manual override — until it recovers or
           ``retry_fault()`` succeeds. ``TAG_POLICY["not_responding"]
           .controllable`` is ``False`` and carries no override column, which
           is what makes the refusal unconditional; a safety hold (rule 0b)
           is a separate mechanism with its own override. Checked first, and
           here (not as a parallel check) so every caller
           (``submit_vi_action()``, the drain gate) inherits it for free.
        0b. A VI with an active safety-origin hold condition (the
            System-Condition standard: its ``safety_concerns()`` names a
            currently-tripped, non-tolerated flag — see ``Station.
            update_conditions()``) is refused next, ALSO regardless of
            state, UNLESS ``acknowledge()`` has unlocked it — either via the
            per-condition hold override (``_hold_override_until``) or the
            EMERGENCY override (``_emergency_override_until``, which admits
            every held VI once unlocked, not just the one that triggered
            EMERGENCY). Either override, once unlocked, ADMITS IMMEDIATELY
            here — this is what lets it also reach through claim-protection
            (rule 4) for a VI a paused procedure still claims, deliberately
            (GLOSSARY.md's **Hold acknowledge**; the known gap this creates
            for ``resume_procedure()`` is documented there). The hold
            override never admits while the station is in EMERGENCY or
            ERROR — those require their own (EMERGENCY) or no (ERROR)
            override; see the code comment below for why. A VI with no hold
            is entirely unaffected by this rule, whatever flags are tripped
            elsewhere.
        1. IDLE, or a manual ramp (RAMPING with no active run): always
           admitted.
        2. EMERGENCY: refused for EVERY VI, held or not, UNLESS the
           EMERGENCY override is unlocked — critical severity is
           station-wide scope by construction (the System-Condition
           standard): there is no "unconcerned VI" to admit once the whole
           station is in EMERGENCY. This is the inversion of the
           pre-System-Condition behavior, where a VI unconcerned with the
           tripped flag stayed operable.
        3. ERROR: always refused, naming the state.
        4. Otherwise a run is active. Admitted iff the active run's
           ``claimed_vi_names()`` is not "claim everything" (``None``) AND
           *vi_name* is not in it. A claimed VI, or a run that claims
           everything (every procedure today), is refused naming the owning
           run.

        Args:
            vi_name: The VI the action targets.

        Returns:
            ``(True, "")`` when admitted; ``(False, reason)`` when refused,
            with a human-readable reason naming why (and, for a claim
            refusal, the owning run).
        """
        now = time.time()
        emergency_unlocked = (
            self._emergency_override_until is not None and now < self._emergency_override_until
        )
        held = self._held_vis().get(vi_name)
        if held is not None:
            not_responding = "not_responding" in self._station.availability(vi_name).tags
            if not_responding:
                if not TAG_POLICY["not_responding"].controllable:
                    return False, (
                        f"Cannot control {vi_name}: instrument fault ({held.kind}) — "
                        f"{held.message}. Retry the instrument or wait for it to recover."
                    )
            else:
                hold_unlocked = now < self._hold_override_until.get(held.key, 0.0)
                if emergency_unlocked:
                    return True, ""
                if hold_unlocked and self._state not in (
                    OrchestratorState.EMERGENCY, OrchestratorState.ERROR
                ):
                    return True, ""
                return False, (
                    f"Cannot control {vi_name}: safety hold active "
                    f"({held.kind}). Resolve the condition, or acknowledge to "
                    "unlock manual control."
                )
        if self._state == OrchestratorState.IDLE:
            return True, ""
        manual_ramping = (
            self._state == OrchestratorState.RAMPING and self._procedure is None
        )
        if manual_ramping:
            return True, ""
        if self._state == OrchestratorState.EMERGENCY:
            if emergency_unlocked:
                return True, ""
            return False, (
                f"Cannot control {vi_name}: EMERGENCY — acknowledge the "
                "emergency to unlock manual front-panel recovery."
            )
        if self._state == OrchestratorState.ERROR:
            return False, (
                f"Cannot control {vi_name}: procedure is running in state {self._state.name}"
            )
        if self._procedure is None:
            # Defensive: no other non-IDLE, non-manual-ramp state should be
            # reachable with no active run. Refuse conservatively rather than
            # admit on an assumption that turned out false.
            return False, (
                f"Cannot control {vi_name}: procedure is running in state {self._state.name}"
            )
        if self._active_claims is None:
            return False, f"Cannot control {vi_name}: {self._active_run_label()} is running"
        if vi_name in self._active_claims:
            return False, (
                f"Cannot control {vi_name}: claimed by running {self._active_run_label()}"
            )
        return True, ""

    def submit_vi_action(self, vi_name: str, method_name: str, **kwargs: Any) -> None:
        """Submit a GUI action to a specific VI.

        Admission is decided by ``_manual_action_admissible()``: a faulted
        or safety-held VI is always refused (the latter unless the
        EMERGENCY manual override is unlocked); IDLE / a manual ramp /
        EMERGENCY (for an unheld VI) always admit; ERROR always refuses;
        otherwise a run is active and the action is admitted iff *vi_name*
        is not one of the active run's claimed VIs.
        """
        admitted, reason = self._manual_action_admissible(vi_name)
        if not admitted:
            logger.info("Blocked action: %s", reason)
            self.action_blocked.emit(reason)
            return

        self._gui_action_queue.append({
            "vi_name": vi_name,
            "method_name": method_name,
            "kwargs": kwargs
        })

    def submit_global_action(self, action: str) -> None:
        """Fan a global lifecycle action out into one queued action per VI.

        ``"initiate_all"`` / ``"standby_all"`` enqueue an ``initiate`` /
        ``standby`` for every registered VI onto the same GUI-action queue the
        per-panel lifecycle toggles use. Each then runs on the tick (the single
        hardware writer) and emits ``action_succeeded`` / ``action_failed`` —
        the per-VI verdict that flips each InstrumentPanel's lifecycle toggle.

        Calling ``station.initiate_all()`` / ``standby_all()`` directly here
        (the previous behaviour) ran the methods but emitted no verdict, so the
        toggles never updated and the click looked like it did nothing.

        Args:
            action: ``"initiate_all"`` or ``"standby_all"``. Anything else is
                ignored.
        """
        method = {"initiate_all": "initiate", "standby_all": "standby"}.get(action)
        if method is None:
            return
        # Standby is also a safety action: if a run is in flight, abort it first
        # so the enqueued standby actions run once the Orchestrator is back in IDLE.
        if action == "standby_all" and self._state not in (
            OrchestratorState.IDLE,
            OrchestratorState.ERROR,
            OrchestratorState.EMERGENCY,
        ):
            self.abort_procedure()
        for vi_name in self._station.get_vi_names():
            self._gui_action_queue.append(
                {"vi_name": vi_name, "method_name": method, "kwargs": {}}
            )

    def connect_instrument(self, vi_name: str) -> None:
        """Bring an offline instrument online (the GUI's Connect action).

        The ``connect`` half of the connection-lifecycle standard (see
        ``BaseVirtualInstrument``) at the Orchestrator's public API — the one
        entry point for both cases that leave a VI offline: it never
        connected at startup, or the operator disconnected it deliberately.
        Delegates to ``Station.connect_instrument()`` and reports the verdict
        through the standard action signals: ``action_succeeded(vi_name,
        "connect")`` plus ``instrument_reconnected(vi_name)`` on success,
        ``action_failed`` with the reason otherwise.

        Allowed only in IDLE: a VI joining the station mid-procedure would
        bypass the run's safety review. Runs synchronously rather than via the
        GUI action queue — the queue dispatches to *registered* VIs, which an
        offline one is not, and everything is on the one thread anyway, so no
        tick can interleave with the connect (the single-writer guarantee
        holds).

        Args:
            vi_name: Name of the offline VI to connect.
        """
        if self._state != OrchestratorState.IDLE:
            msg = (
                f"Cannot connect {vi_name}: Orchestrator is in state "
                f"{self._state.name}, connecting requires IDLE"
            )
            logger.info("Blocked connect: %s", msg)
            self.action_blocked.emit(msg)
            return
        ok, message = self._station.connect_instrument(vi_name)
        if ok:
            # A reconnected switch VI must be adoptable as the scanner —
            # re-run the same first-switch resolution done at construction.
            if self._scanner_vi_name is None:
                switch_names = self._station.switch_vi_names()
                self._scanner_vi_name = switch_names[0] if switch_names else None
            logger.info("Connect succeeded for '%s'", vi_name)
            self.instrument_reconnected.emit(vi_name)
            self.action_succeeded.emit(vi_name, "connect")
        else:
            logger.warning("Connect failed for '%s': %s", vi_name, message)
            self.action_failed.emit(vi_name, "connect", message)

    def disconnect_instrument(self, vi_name: str) -> None:
        """Release a live instrument to its front panel (the GUI's Disconnect action).

        The ``disconnect`` half of the connection-lifecycle standard (see
        ``BaseVirtualInstrument``): delegates to
        ``Station.disconnect_instrument()``, which hands the instrument back
        WITHOUT standing it down — a magnet at field stays at field — and
        degrades the VI into the offline registry, exactly as if it had never
        connected. Reports through ``action_succeeded(vi_name, "disconnect")``
        plus ``instrument_disconnected(vi_name)``, or ``action_failed``.

        Allowed only in IDLE, the same restriction ``connect_instrument()``
        uses, for the same reason: a station that loses an instrument
        mid-procedure has escaped the run's safety review. Since a run is
        never active while IDLE, this alone keeps a claimed VI from being
        disconnected out from under it — no separate claims check is needed
        (contrast ``_manual_action_admissible()``, which also runs outside
        IDLE and does check claims explicitly). A disconnected instrument is
        not a fault, so no ``ErrorEvent`` is raised: the operator asked for
        this.

        Args:
            vi_name: Name of the live VI to disconnect.
        """
        if self._state != OrchestratorState.IDLE:
            msg = (
                f"Cannot disconnect {vi_name}: Orchestrator is in state "
                f"{self._state.name}, disconnecting requires IDLE"
            )
            logger.info("Blocked disconnect: %s", msg)
            self.action_blocked.emit(msg)
            return
        ok, message = self._station.disconnect_instrument(vi_name)
        if ok:
            # The scanner slot must not keep naming a VI that is gone; the
            # next connect re-resolves it (see connect_instrument()).
            if self._scanner_vi_name == vi_name:
                self._scanner_vi_name = None
            logger.info("Instrument '%s' disconnected by the operator", vi_name)
            self.instrument_disconnected.emit(vi_name)
            self.action_succeeded.emit(vi_name, "disconnect")
        else:
            logger.warning("Disconnect failed for '%s': %s", vi_name, message)
            self.action_failed.emit(vi_name, "disconnect", message)

    def offline_reason(self, vi_name: str) -> str:
        """Return the current failure reason for an offline VI, GUI-safe.

        Args:
            vi_name: Name of the VI to look up.

        Returns:
            The offline record's human-readable reason, or ``""`` when the VI
            is not offline (e.g. it has just been reconnected).
        """
        try:
            return self._station.get_offline_info(vi_name).reason
        except KeyError:
            return ""

    def vi_faults(self) -> dict[str, FaultRecord]:
        """Return the Station's current runtime fault registry, GUI-safe.

        Returns:
            ``{vi_name: FaultRecord}`` for every VI with an active
            stale/disconnected fault.
        """
        return self._station.vi_faults()

    def availability(self, vi_name: str) -> Availability:
        """Return the Availability standard's unified record for one VI, GUI-safe.

        Args:
            vi_name: Name of a configured VI — live or offline.

        Returns:
            The :class:`~cryosoft.core.availability.Availability` record.

        Raises:
            KeyError: If `vi_name` is not a configured VI at all.
        """
        return self._station.availability(vi_name)

    def availabilities(self) -> dict[str, Availability]:
        """Return the Availability standard's unified record for every VI, GUI-safe.

        Returns:
            ``{vi_name: Availability}`` for every configured VI, live and
            offline alike.
        """
        return self._station.availabilities()

    def acknowledge_fault(self, vi_name: str) -> None:
        """Acknowledge a VI's active runtime fault (calms the Monitor UI).

        A no-op (logged) if the VI has no active fault — acknowledging
        something already clear is harmless. Emits ``action_succeeded`` so
        the Monitor's per-panel Acknowledge button gets the same confirmed-
        state feedback as every other GUI action.

        Args:
            vi_name: Name of the faulted VI.
        """
        if self._station.acknowledge_fault(vi_name):
            logger.info("Fault on '%s' acknowledged", vi_name)
            self.action_succeeded.emit(vi_name, "acknowledge_fault")
        else:
            logger.info("acknowledge_fault('%s') ignored: no active fault", vi_name)

    def retry_fault(self, vi_name: str) -> None:
        """Retry a VI's active runtime fault: reset counters, poll once.

        The runtime counterpart of ``connect_instrument()``: it never rebuilds
        a driver (the VI is already live) — only ``Station.retry_fault()``'s
        counter-reset-and-repoll. Unlike ``connect_instrument()`` this is not
        restricted to IDLE: an unclaimed VI's fault (the common case this
        exists for) does not require aborting whatever run is in progress
        to retry it, and everything still runs on the one tick-driven
        thread so there is no concurrency hazard in doing this synchronously
        mid-run.

        Args:
            vi_name: Name of the faulted VI to retry.
        """
        ok, message = self._station.retry_fault(vi_name)
        if ok:
            logger.info("Retry succeeded for faulted VI '%s'", vi_name)
            self.action_succeeded.emit(vi_name, "retry_fault")
        else:
            logger.warning("Retry failed for faulted VI '%s': %s", vi_name, message)
            self.action_failed.emit(vi_name, "retry_fault", message)

    def set_scanner_enabled(self, enabled: bool) -> None:
        """Toggle scanner availability for scanner-sensitive procedures.

        A no-op (logged at INFO) when the station has no switch VI, so
        stations without a scanner can call this unconditionally.

        Args:
            enabled: True to make the scanner available to procedures.
        """
        if self._scanner_vi_name is None:
            logger.info("set_scanner_enabled ignored: no switch VI in station")
            return
        self._station.set_scanner_enabled(bool(enabled))

    def scanner_enabled(self) -> bool:
        """Return whether scanner-sensitive procedures may use the switch VI."""
        return self._station.scanner_enabled()

    # ------------------------------------------------------------------
    # Run manifests (consumed by the session layer)
    # ------------------------------------------------------------------

    def _emit_run_started(self) -> None:
        """Capture the active run's manifest and emit ``run_started``.

        Called exactly once per run, after ``initiate()`` succeeded and its
        plan was dispatched. The data file path must be captured here: the
        procedure closes its ``DataManager`` (and forgets the path) in
        ``standby()``/``abort()``, before the run-finished emission.
        Best-effort on the optional fields — a minimal procedure (e.g. a test
        mock) without the public accessors still gets a manifest.
        """
        procedure = self._procedure
        name = getattr(procedure, "name", "") or type(procedure).__name__
        slug = "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")
        self._run_counter += 1
        params: dict[str, Any] = {}
        get_params = getattr(procedure, "get_params", None)
        if callable(get_params):
            try:
                params = get_params()
            except Exception:  # noqa: BLE001 — manifest must never abort a run
                logger.exception("run manifest: get_params() failed")
        self._active_run_manifest = {
            "run_id": f"{time.strftime('%Y%m%d_%H%M%S')}_{self._run_counter:03d}_{slug}",
            "procedure": name,
            "kind": getattr(procedure, "run_kind", "run"),
            "params": params,
            "data_file": str(getattr(procedure, "data_filepath", None) or ""),
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.run_started.emit(dict(self._active_run_manifest))

    def _collect_run_summary(self, procedure: Any) -> dict[str, Any]:
        """Return ``procedure.run_summary()``'s result, or ``{}`` on any problem.

        Duck-typed: looked up via ``getattr`` so this module never imports
        ``OperationBase`` (contract C5) — a plain ``BaseProcedure`` or a test
        double without ``run_summary()`` simply yields ``{}``. Guarded by a
        broad try/except plus a return-type check, so a broken or
        misbehaving override can never prevent the run from finishing.

        Args:
            procedure: The procedure/operation to query (may be ``None``).

        Returns:
            The dict ``run_summary()`` returned, or ``{}`` if the method is
            absent, raises, or does not return a dict.
        """
        run_summary_fn = getattr(procedure, "run_summary", None)
        if not callable(run_summary_fn):
            return {}
        try:
            summary = run_summary_fn()
        except Exception:
            logger.exception("run_summary() raised")
            return {}
        if not isinstance(summary, dict):
            logger.warning(
                "run_summary() returned %r (expected a dict); ignoring", type(summary)
            )
            return {}
        return summary

    def _emit_run_finished(
        self,
        status: str,
        reason: str = "",
        postconditions_unmet: list[str] | None = None,
    ) -> None:
        """Emit ``run_finished`` for the active run, exactly once.

        Idempotent: the captured manifest is cleared on emission, so the
        overlapping cleanup paths (user abort, error containment, emergency
        entry) cannot double-report a run. A no-op when no run ever started
        (e.g. ``initiate()`` itself failed, or a manual-ramp-only session).

        Args:
            status: Terminal status — ``done``, ``aborted``, or ``failed``.
            reason: Error text for ``failed``; empty otherwise.
            postconditions_unmet: Gate names an operation's one-shot
                postcondition evaluation found unmet at finish, or
                ``None`` — recorded as ``[]``, which is always the case for
                a procedure/abort/failure path.
        """
        if self._active_run_manifest is None:
            return
        manifest = dict(self._active_run_manifest)
        self._active_run_manifest = None
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["status"] = status
        manifest["reason"] = reason
        manifest["postconditions_unmet"] = list(postconditions_unmet or ())
        # run_summary() hand-off: self._procedure is still set on
        # the "done" path (_finish_run() clears it AFTER this call); the
        # abort/fail/emergency paths already cleared it via
        # _abort_active_procedure(), which cached the summary into
        # self._pending_run_summary first — see that method's docstring.
        if self._procedure is not None:
            summary = self._collect_run_summary(self._procedure)
        else:
            summary = self._pending_run_summary
        self._pending_run_summary = {}
        manifest["summary"] = summary
        self.run_finished.emit(manifest)

    # ------------------------------------------------------------------
    # Private / internal
    # ------------------------------------------------------------------

    def _change_state(self, new_state: OrchestratorState) -> None:
        logger.info("Orchestrator state: %s -> %s", self._state.name, new_state.name)
        self._state = new_state
        self._state_entered_at = time.time()
        self.state_changed.emit(self._state.value)

    def get_operational_status(self) -> dict:
        """Return the most recent operational-status record.

        The runtime troubleshooting signal: orchestrator state, elapsed time in
        it, and per-system-VI gap-to-target / rate / ETA / verdict. See
        ``cryosoft.core.operational_status`` for the schema; the same record is
        emitted on ``operational_status`` and appended to ``logs/status.jsonl``.
        """
        return dict(self._operational_status)

    def _update_operational_status(self, state: dict) -> None:
        """Build this tick's operational-status record, emit it, and log it.

        Guarded: operational-status reporting is non-critical, so a failure here
        must never degrade a running procedure to ERROR via the tick boundary.
        """
        try:
            ramp_info = self._ramp_info()
            wait_target = self._current_wait_time if self._wait_started else None
            wait_elapsed = (
                time.time() - self._wait_start_time if self._wait_started else None
            )
            progress = None
            if self._procedure is not None and hasattr(self._procedure, "get_progress"):
                try:
                    progress = self._procedure.get_progress()
                except Exception:
                    progress = None
            # This tick's System-Condition standard registry (core/conditions.py),
            # mirroring how the unified condition pipeline below (~1602-1607)
            # builds its list: the Station's comm/safety conditions plus, when a
            # session envelope is active and we are not already in EMERGENCY, its
            # envelope conditions for this same state snapshot. Sorted by key so
            # the record is stable across ticks with the same conditions.
            conditions: list[Condition] = list(self._station.conditions().values())
            if self._session_envelope is not None and self._state != OrchestratorState.EMERGENCY:
                conditions.extend(
                    envelope_conditions(self._session_envelope.check_state(state), time.time())
                )
            conditions.sort(key=lambda c: c.key)
            record, self._prev_gaps = build_operational_status(
                orch_state=self._state.value,
                elapsed_in_state_s=time.time() - self._state_entered_at,
                state=state,
                ramp_info=ramp_info,
                prev_gaps=self._prev_gaps,
                wait_target_s=wait_target,
                wait_elapsed_s=wait_elapsed,
                progress=progress,
                # Postcondition gates are no longer a multi-tick wait phase
                # (evaluated once, immediately, as the run ends), so only the
                # initiation/reading gates can ever be "active" across ticks.
                active_gates=[g.name for g in self._pending_gates],
                conditions=conditions,
            )
            record, self._watchdog_state = apply_watchdog(
                record, self._watchdog_state, self._watchdog_config
            )
            self._operational_status = record
            self.operational_status.emit(record)
            self._status_logger.info(json.dumps(record))
        except Exception:
            logger.exception("operational-status update failed (non-fatal)")

    # ------------------------------------------------------------------
    # Ramp tracking
    # ------------------------------------------------------------------

    def _ramp_info(self) -> dict[str, dict]:
        """Return this tick's ``Station.get_ramp_status()`` snapshot, polled once.

        Memoised for the duration of one tick (``_tick_body()`` clears the
        memo on entry) because two consumers want the same answer — the
        operational-status record and the ramp tracker — and each VI's ramp
        accessors are real instrument reads. Polling twice per tick would
        double that bus traffic for no new information.
        """
        if self._tick_ramp_info is None:
            self._tick_ramp_info = self._station.get_ramp_status()
        return self._tick_ramp_info

    def _publish_ramps(self) -> None:
        """Rebuild this tick's running-ramp records, cache them, and emit them.

        Guarded exactly like ``_update_operational_status()``: ramp tracking
        is a reporting surface, so a failure here must never degrade a
        running procedure to ERROR via the tick's exception boundary.

        Nothing is polled while the machine is quiet (monitoring off AND
        IDLE): a freshly launched app polls no instrument until its
        instruments are initiated, and the tracker must not be the one thing
        that breaks that silence. Any ramp — manual or run-driven — leaves
        IDLE on the tick it starts, so nothing that is actually ramping is
        ever missed by this guard.
        """
        try:
            if not self._monitoring and self._state == OrchestratorState.IDLE:
                records: list[RampRecord] = []
            else:
                run_active = self._procedure is not None
                records = build_ramp_records(
                    self._ramp_info(),
                    setpoint_meta=self._station.system_setpoint_meta,
                    # The SAME predicate stop_ramp() itself uses, so a row's
                    # Abort button can never look enabled for a stop the
                    # action would refuse (or vice versa).
                    stop_policy=self._manual_action_admissible,
                    run_label=self._active_run_label() if run_active else None,
                    run_claims=self._active_claims if run_active else None,
                )
            self._active_ramps = records
            self.ramps_updated.emit(list(records))
        except Exception:
            logger.exception("ramp-tracker update failed (non-fatal)")

    # ------------------------------------------------------------------
    # Concise status feed (Procedure-window status log)
    # ------------------------------------------------------------------

    def _emit_status(self, text: str) -> None:
        """Emit one concise milestone line to listeners and the status logger.

        Wrapped so a formatting or signal error can never abort a run: the tick
        runs inside an exception boundary that degrades to ERROR, and a
        cosmetic status line must not be able to trip it.

        Routed by the active run's kind (hard status separation, plan
        operation-concurrency-and-error-scoping.md §2): while an operation is
        active this goes to ``operation_status``/``cryosoft.operation_status``
        instead of ``status_message``/``cryosoft.procedure_status`` — the
        Procedure window must never see operation chatter. Neither logger is
        the ``cryosoft.status`` logger, which carries the machine-only JSONL
        operational-status stream and must stay pure JSON.
        """
        try:
            if self._is_operation_active():
                logging.getLogger("cryosoft.operation_status").info(text)
                self.operation_status.emit(text)
            else:
                logging.getLogger("cryosoft.procedure_status").info(text)
                self.status_message.emit(text)
        except Exception:  # noqa: BLE001 — status must never disrupt the run
            logger.exception("status emit failed")

    def _describe_system_target(self, vi_name: str, target: Target, *, verb: str) -> str:
        """Compose "<verb> <label> to <value> <unit>" for one system ramp target.

        Label and unit come from the VI's declarative setpoint metadata via the
        Station (e.g. magnet -> "field"/"T"), so any procedure's targets render
        without per-procedure code. Best-effort: degrades to the raw VI name
        rather than raising into the tick.
        """
        try:
            label, unit = self._station.system_setpoint_meta(vi_name)
            value = float(target.target)
            unit_suffix = f" {unit}" if unit else ""
            return f"{verb} {label} to {value:g}{unit_suffix}"
        except Exception:  # noqa: BLE001 — degrade, never raise into the tick
            return f"{verb} {vi_name}"

    def _describe_measurement_command(self, command: Command) -> str | None:
        """Compose "Arming/Disarming <label> measurement" for one ``Command``.

        Returns None if the command cannot be described.
        """
        try:
            label = self._station.measurement_label(command.vi_name)
            if command.method in ("standby", "disarm"):
                return f"Disarming {label} measurement"
            return f"Arming {label} measurement"
        except Exception:  # noqa: BLE001
            return None

    def _emit_setup_actions(
        self,
        system_targets: dict[str, Target],
        commands: Sequence[Command],
        *,
        verb: str,
    ) -> None:
        """Emit one status line per distinct setup action (ramps, then measurement).

        Used for both initiation and standby/parking so each thing being done
        (set temperature, ramp field, arm the measurement) shows separately.
        """
        for vi_name, target in system_targets.items():
            self._emit_status(self._describe_system_target(vi_name, target, verb=verb))
        for command in commands:
            line = self._describe_measurement_command(command)
            if line:
                self._emit_status(line)

    def _emit_initiation_status(
        self, system_targets: dict[str, Target], commands: Sequence[Command]
    ) -> None:
        """Emit the initiation header plus one line per distinct setup action.

        Initiation is a distinct, often slow phase: the procedure brings the
        NON-swept system state to its setpoints (target temperature in a field
        sweep, target field in a temperature sweep) AND moves the swept quantity
        to its start value AND arms the measurement instrument. Each is shown as
        its own line — none of these setpoints are reached yet, and a magnet or
        temperature ramp can take a long time.
        """
        try:
            name = getattr(self._procedure, "name", "") or type(self._procedure).__name__
            _, n = self._procedure.get_sweep_position()
            self._emit_status(f'Initiating "{name}" ({n} points)')
        except Exception:  # noqa: BLE001
            self._emit_status("Initiating procedure")
        self._emit_setup_actions(system_targets, commands, verb="Ramping")

    def _ramp_status_line(self, system_targets: dict[str, Target]) -> str:
        """Compose "Point i/n: ramping <label> -> <value> <unit>" for a sweep step.

        Describes the (usually single) target the sweep step ramps, using the
        VI's setpoint metadata via the Station. Best-effort.
        """
        try:
            i, n = self._procedure.get_sweep_position()
            parts = []
            for vi_name, target in system_targets.items():
                label, unit = self._station.system_setpoint_meta(vi_name)
                unit_suffix = f" {unit}" if unit else ""
                parts.append(f"{label} -> {float(target.target):g}{unit_suffix}")
            detail = "; ".join(parts) if parts else "next setpoint"
            return f"Point {i}/{n}: ramping {detail}"
        except Exception:  # noqa: BLE001 — degrade, never raise into the tick
            return "Ramping to next setpoint"

    def _measure_status_line(self) -> str:
        """Compose "Measuring point i/n" for the current point (best-effort)."""
        try:
            i, n = self._procedure.get_sweep_position()
            return f"Measuring point {i}/{n}"
        except Exception:  # noqa: BLE001
            return "Measuring"

    def _tick(self) -> None:
        """One cooperative cycle, inside the exception boundary.

        PyQt6 aborts the process on an unhandled exception in a slot; here
        that would mean dying with the magnet mid-ramp and the data file
        open. Anything unexpected instead cleans up and degrades to ERROR.
        """
        try:
            self._tick_body()
        except Exception as exc:  # noqa: BLE001 — boundary must be broad
            logger.exception("Unhandled exception in orchestrator tick")
            if self._state == OrchestratorState.EMERGENCY:
                # Already in the most severe state; just report.
                self._error(f"Internal error during EMERGENCY: {exc}")
                return
            self._fail_to_error(f"Internal error: {exc}")

    def _tick_body(self) -> None:
        # Fresh tick: whatever ramp snapshot the last one memoised is stale.
        # See _ramp_info() — one poll per tick, shared by every consumer.
        self._tick_ramp_info = None

        # 1.+2. Monitor cycle — only while monitoring is active (see
        # start_monitoring()). While it is off, no instrument is polled at
        # all: a freshly launched app stays quiet until the instruments have
        # been initiated. run_procedure() auto-starts monitoring and
        # stop_monitoring() is refused outside IDLE/ERROR, so the safety
        # watchdog and stale detection below are guaranteed to run whenever
        # a procedure is active.
        if self._monitoring:
            state = self._station.get_state()
            self.states_updated.emit(state)

            # One-line summary per tick (full per-method detail stays in the file log)
            parts = []
            for vi_name, vi_state in state.items():
                readable = {k: v for k, v in vi_state.items() if not k.startswith("_")}
                if readable:
                    kv = ", ".join(
                        f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}"
                        for k, v in readable.items()
                    )
                    parts.append(f"{vi_name}: {kv}")
            logger.debug("Monitor: %s", " | ".join(parts))

            # Operational-status record (runtime troubleshooting signal): assembled
            # from this tick's snapshot, emitted, and appended to the resolved
            # log directory's status.jsonl (see cryosoft.core.paths.log_directory()).
            self._update_operational_status(state)

            # Tiered trend-history sample: non-fatal, mirrors the
            # operational-status guard above — a logging failure here must
            # never fail the tick. record() is itself internally
            # exception-safe; this guard is belt-and-braces around
            # constructing its arguments (e.g. last_state_flat()).
            try:
                self._tiered_trend_logger.record(
                    self._station.last_state_flat(), time.time(), self._state.name
                )
            except Exception:
                logger.exception("tiered trend-history record failed (non-fatal)")

            run_active = self._state not in (
                OrchestratorState.IDLE,
                OrchestratorState.PAUSED,
                OrchestratorState.ERROR,
                OrchestratorState.EMERGENCY,
            )
            # The run's watched VIs: its system (target-receiving) VIs plus
            # its EXPLICIT claims — a claimed non-system VI (e.g. the helium
            # fill's level meter) faulting must fail the run too, not merely
            # warn while the run keeps trusting a dead instrument. Claims of
            # None (claim-everything, every plain procedure) deliberately do
            # NOT widen this beyond the system VIs: a procedure's unrelated
            # VI going stale was never a run-failure before and stays a
            # warning-only fault.
            watched_vis = set(self._active_system_vis) | (self._active_claims or set())

            # Safety check — reuses this tick's snapshot (no second hardware
            # poll), called exactly once regardless of how many decisions
            # below consult it. An active operation's tolerated_safety_flags
            # are resolved once here and applied uniformly by
            # update_conditions() (a tolerated flag, e.g. helium_low during
            # a helium-fill operation, must not hold the magnets that same
            # operation claims and ramps to zero). Only the ACTIVE
            # procedure's tolerance applies here — a plain procedure (or
            # IDLE) tolerates nothing, unchanged.
            safety = self._station.check_safety(state)
            tolerated: frozenset[str] = frozenset()
            if self._procedure is not None and (
                getattr(self._procedure, "command_scope", "measurement") == "operation"
            ):
                tolerated = frozenset(
                    getattr(self._procedure, "tolerated_safety_flags", frozenset())
                )

            # ONE unified condition pipeline (the System-Condition standard,
            # core/conditions.py): get_state() above already recorded this
            # tick's comm conditions; update_conditions() refreshes the
            # safety-origin ones from this tick's check_safety() result,
            # honoring tolerated_safety_flags exactly as before. A
            # session-envelope violation is folded into the same list — same
            # snapshot, same consequence as a tripped safety flag — gated
            # exactly like today: not even computed once already EMERGENCY.
            self._station.update_conditions(safety, tolerated_flags=tolerated)
            conditions: list[Condition] = list(self._station.conditions().values())
            if self._session_envelope is not None and self._state != OrchestratorState.EMERGENCY:
                conditions.extend(
                    envelope_conditions(self._session_envelope.check_state(state), time.time())
                )

            # Onset diff: ONE registry-key set replaces the old separate
            # fault-VI and safety-hold-VI trackers. A NEW comm-origin key on
            # an unwatched VI is a per-instrument warning (a watched VI's
            # fault is handled by the run-failure verdict below instead,
            # with a matching, more severe event, so it must not ALSO get a
            # warning here). A NEW hold-severity SAFETY key (the safety-hold
            # standard: some VI's safety_concerns() names a newly-tripped,
            # non-tolerated flag) dispatches one standby() to every VI it
            # holds — not repeated every tick the hold persists, and
            # naturally re-fired if the key disappears (a tolerance window)
            # and later reappears, since it is then "new" again. Comm
            # conditions never get this standby dispatch — a stale VI was
            # never told to stand by, only warned about or failed.
            by_key = {c.key: c for c in conditions}
            current_keys = set(by_key)
            new_keys = current_keys - self._known_condition_keys
            self._known_condition_keys = current_keys
            for key in sorted(new_keys):
                condition = by_key[key]
                if condition.origin == "comm":
                    vi_name = condition.source_vis[0]
                    if run_active and vi_name in watched_vis:
                        continue  # handled as a run failure below, this same tick
                    self._emit_fault_event(vi_name, condition.kind, condition.message)
                elif condition.origin == "safety" and condition.severity == "hold":
                    for vi_name in sorted(condition.affected_vis or ()):
                        try:
                            self._station.get_vi(vi_name).standby()
                        except Exception:
                            logger.exception("standby failed on held VI '%s'", vi_name)

            # Single verdict over every condition (comm + safety + envelope):
            # decide() partitions by severity — critical -> emergency,
            # hold -> held_vis, and (when a run is active) the
            # alphabetically-first watched VI landing in held_vis ->
            # run_failure. This collapses what used to be four separate
            # checks (EMERGENCY entry, envelope violation, held-claimed run
            # failure, stale-claimed run failure) into one policy call.
            verdict = decide(conditions, watched_vis=watched_vis, run_active=run_active)

            # Prune expired hold-override entries — by expiry only, never
            # because the condition disappeared from verdict.held_vis (that
            # would force a fresh acknowledge on every flap of the same
            # flag, defeating the point of the cooldown; see acknowledge()
            # and GLOSSARY.md's **Hold acknowledge**). _emergency_override_
            # until needs no equivalent tick-driven pruning: it is read live
            # via a timestamp comparison in _manual_action_admissible(), and
            # _enter_emergency() already resets it unconditionally on every
            # fresh EMERGENCY entry.
            if self._hold_override_until:
                now = time.time()
                self._hold_override_until = {
                    key: until for key, until in self._hold_override_until.items()
                    if now < until
                }

            if verdict.emergency and self._state != OrchestratorState.EMERGENCY:
                safety_conditions = [c for c in verdict.emergency if c.origin == "safety"]
                envelope_conds = [c for c in verdict.emergency if c.origin == "envelope"]
                reason_parts = []
                vi_names: set[str] = set()
                if safety_conditions:
                    reason_parts.append(", ".join(sorted({c.kind for c in safety_conditions})))
                    vi_names |= {vi for c in safety_conditions for vi in c.source_vis}
                if envelope_conds:
                    reason_parts.append("; ".join(c.message for c in envelope_conds))
                self._enter_emergency("; ".join(reason_parts), tuple(sorted(vi_names)))
                return  # emergency entry already cleaned up; nothing else this tick

            # Run failure: the watched VI's hold — comm-origin (stale) or
            # safety-origin (a non-tolerated concern) — that decide() found.
            # Either way the run fails and returns to IDLE, not global
            # ERROR: the blast radius is this one VI, every other instrument
            # (including this one once it recovers) stays usable.
            if verdict.run_failure is not None:
                vi_name, condition = verdict.run_failure
                if condition.origin == "safety":
                    self._fail_run_for_fault(
                        vi_name, reason=f"safety hold on '{vi_name}' ({condition.kind})"
                    )
                else:
                    self._fail_run_for_fault(vi_name)
                return

        # 3. GUI Actions — each queued action gets the SAME verdict
        # submit_vi_action() would give it right now, via the shared
        # _manual_action_admissible() predicate: the run may have
        # started/finished/changed claims since it was queued, and a claim
        # refusal during an active run only refuses the CLAIMED VIs, not the
        # whole queue — so admission is decided per action, not once for the
        # batch. Every action gets a verdict this tick; none is left queued.
        pending_actions = list(self._gui_action_queue)
        self._gui_action_queue.clear()
        for action in pending_actions:
            admitted, reason = self._manual_action_admissible(action["vi_name"])
            if not admitted:
                logger.info("Blocked queued action on %s: %s", action["vi_name"], reason)
                self.action_blocked.emit(reason)
                continue
            try:
                self._station.execute_vi_action(
                    action["vi_name"],
                    action["method_name"],
                    **action["kwargs"]
                )
                self.action_succeeded.emit(action["vi_name"], action["method_name"])
            except Exception as e:
                # Every user action gets an explicit verdict: rejections
                # (limit violations, safety guards) and failures surface
                # to the GUI with the reason, never silently.
                logger.error("Error executing GUI action on %s: %s", action["vi_name"], e)
                self.action_failed.emit(
                    action["vi_name"], action["method_name"], str(e)
                )
        if pending_actions:
            # A drained action may have started, retargeted, or stopped a
            # ramp, which the snapshot taken at the top of this tick predates
            # — so the tracker re-polls rather than showing the operator a
            # tick-old view of the click they just made. Only on ticks that
            # actually ran an action: an ordinary tick still polls once.
            self._tick_ramp_info = None

        # If a GUI action started (or restarted) a manual ramp, enter RAMPING.
        if self._state == OrchestratorState.IDLE and not self._station.check_ramps():
            self._change_state(OrchestratorState.RAMPING)

        # 4. State Machine matching
        if self._state == OrchestratorState.IDLE:
            pass
        elif self._state == OrchestratorState.INITIATING:
            self._change_state(OrchestratorState.RAMPING)
        elif self._state == OrchestratorState.RAMPING:
            # A skipped auto_ramp step ends this ramp early. The operation
            # cannot do it itself — step() is not called in RAMPING — so the
            # flag it raised is honoured here, on the tick, where every
            # other hardware write happens. stop_ramps() holds in place, so
            # check_ramps() reports complete on this same tick and the run
            # proceeds exactly as if the ramp had landed.
            self._stop_ramps_for_skipped_step()
            if self._station.check_ramps():  # True = all ramps complete
                if self._procedure is None:
                    # Manual ramp from GUI — return to IDLE.
                    self._change_state(OrchestratorState.IDLE)
                else:
                    gates = self._current_gates()
                    if gates:
                        # Gates replace wait_s entirely for this transition.
                        self._pending_gates = list(gates)
                        gate_state = (
                            OrchestratorState.INITIATION_GATE
                            if self._first_measurement
                            else OrchestratorState.READING_GATE
                        )
                        self._change_state(gate_state)
                    else:
                        if not self._wait_started:
                            self._wait_started = True
                            self._wait_start_time = time.time()
                            if self._current_wait_time > 0:
                                self._emit_status(
                                    f"Waiting {self._current_wait_time:g} s at setpoint"
                                )

                        if time.time() - self._wait_start_time >= self._current_wait_time:
                            self._wait_started = False
                            self._first_measurement = False
                            self._change_state(OrchestratorState.MEASURING)
        elif self._state in (OrchestratorState.INITIATION_GATE, OrchestratorState.READING_GATE):
            if self._is_operation_active() and getattr(
                self._procedure, "finish_requested", False
            ):
                # A gate (e.g. HeliumFillOperation's zero_field wait) can hold
                # forever if its check() never turns true. finish_operation()
                # only flips finish_requested — it is not itself wired into
                # this branch — so without this check a Finish click here
                # would appear to do nothing and the run would look stuck.
                # Abandon the gate wait and go straight to STANDBY, exactly
                # like a SWEEPING-state finish.
                self._pending_gates = []
                self._change_state(OrchestratorState.STANDBY)
            else:
                self._pending_gates = [g for g in self._pending_gates if not g.step()]
                if not self._pending_gates:
                    self._first_measurement = False
                    self._change_state(OrchestratorState.MEASURING)
        elif self._state == OrchestratorState.MEASURING:
            if self._procedure:
                is_operation = self._is_operation_active()
                self._emit_status(self._measure_status_line())
                self._procedure.measure()
                if hasattr(self._procedure, "get_progress"):
                    progress = self._procedure.get_progress()
                    if is_operation:
                        self.operation_progress.emit(progress)
                    else:
                        self.procedure_progress.emit(progress)
                # measurement_ready is PROCEDURE-EXCLUSIVE (the hard
                # status separation) — an operation's sample() has no
                # equivalent GUI consumer today (the fill curve is an
                # internal detail until phase 4 moves it to the cryogenics
                # log), so it is withheld even if a future operation grows a
                # last_datapoint attribute.
                last_datapoint = getattr(self._procedure, "last_datapoint", None)
                if last_datapoint and not is_operation:
                    self.measurement_ready.emit(dict(last_datapoint))
            self._change_state(OrchestratorState.SWEEPING)
        elif self._state == OrchestratorState.SWEEPING:
            if self._procedure:
                step_plan = self._procedure.change_sweep_step()
                if step_plan is None:
                    # done, go to standby
                    self._change_state(OrchestratorState.STANDBY)
                else:
                    logger.info("Procedure plan (step): %r", step_plan)
                    self._dispatch_targets(step_plan.targets)
                    self._current_wait_time = step_plan.wait_s
                    self._change_state(OrchestratorState.RAMPING)
                    self._emit_status(self._ramp_status_line(step_plan.targets))
        elif self._state == OrchestratorState.STANDBY:
            if self._is_operation_active():
                # Immediate finish (plan operation-concurrency-and-error-
                # scoping.md §2): no waiting phase at all — see
                # _standby_operation_immediate()'s docstring.
                self._standby_operation_immediate()
            elif not self._standby_dispatched:
                # Wait for whatever ramp was already in flight when SWEEPING
                # ended, then call standby() exactly once and dispatch
                # whatever targets it returns (e.g. ramp magnet to 0 T).
                if self._station.check_ramps():
                    self._emit_status("Sweep complete - closing data file")
                    if self._procedure and hasattr(self._procedure, "standby"):
                        plan = self._procedure.standby()
                        logger.info("Procedure plan (standby): %r", plan)
                        self._dispatch_targets(plan.targets)
                        allowed_scope = getattr(
                            self._procedure, "command_scope", "measurement"
                        )
                        self._station.send_measurement_commands(
                            plan.commands, allowed_scope=allowed_scope
                        )
                        if plan.targets or plan.commands:
                            self._emit_status("Parking hardware")
                            self._emit_setup_actions(plan.targets, plan.commands, verb="Ramping")
                    self._standby_dispatched = True
            else:
                # Wait for the ramp standby() itself just started (if any),
                # then finish. A plain BaseProcedure declares no
                # postcondition_gates() — that hook is operation-only, and an
                # operation never reaches this branch (see the fork above) —
                # so a procedure always finishes as soon as this ramp settles.
                if self._station.check_ramps():
                    self._finish_run()
        elif self._state == OrchestratorState.PAUSED:
            pass # Monitor continues, no ramp advancement
        elif self._state == OrchestratorState.ERROR:
            pass # Awaiting user interaction (recover_from_error)
        elif self._state == OrchestratorState.EMERGENCY:
            # Shutdown already ran once on entry (_enter_emergency).
            # Monitoring continues; awaiting acknowledge().
            pass

        # 5. Ramp tracker — last, so the run/claim context the records carry
        # is the state machine's verdict for this tick (a run that just
        # started or just ended) rather than the one it was in on entry.
        # Reads this tick's memoised snapshot; no extra poll.
        self._publish_ramps()

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _abort_active_procedure(self) -> None:
        """Clean up the running procedure: data file, measurement VI, ramps.

        Safe to call with no procedure active (stops manual ramps then).
        Each cleanup step is individually guarded so one failure (e.g. a dead
        instrument) cannot prevent the others. Also clears ``_active_claims``
        — the shared teardown path for user abort, ``_fail_to_error``,
        and ``_enter_emergency``, so a claim can never outlive its run.

        Caches ``procedure.run_summary()`` into
        ``self._pending_run_summary`` BEFORE clearing ``self._procedure``
        below: the subsequent ``_emit_run_finished()`` call on every one of
        these teardown paths (abort/fail/emergency) runs with
        ``self._procedure`` already ``None``, so it could not call
        ``run_summary()`` itself.
        """
        procedure = self._procedure
        self._pending_run_summary = self._collect_run_summary(procedure)
        if procedure is not None and hasattr(procedure, "abort"):
            try:
                commands = procedure.abort()
                if commands:
                    allowed_scope = getattr(procedure, "command_scope", "measurement")
                    self._station.send_measurement_commands(
                        commands, allowed_scope=allowed_scope
                    )
            except Exception:
                logger.exception("Procedure abort cleanup failed")
        try:
            # Hold hardware where it is: clearing generators alone would let
            # autonomous hardware (magnet PSU) keep ramping to its last setpoint.
            self._station.stop_ramps(self._active_system_vis or None)
        except Exception:
            logger.exception("Stopping ramps during abort failed")

        self._procedure = None
        self._active_claims = None
        self._active_system_vis.clear()
        self._standby_dispatched = False
        self._wait_started = False
        self._first_measurement = True
        self._pending_gates = []
        self._operation_started_from_emergency = False
        self._last_system_targets = {}

    def _operation_end_state(self, procedure: Any) -> OrchestratorState:
        """Return the state a finishing run should return to.

        A plain procedure always returns to IDLE. An operation returns to
        EMERGENCY instead when it was started via the EMERGENCY carve-out
        (``_operation_started_from_emergency`` — a sticky bit set at start,
        independent of what is tripped right now), or when ``Station.
        active_critical_conditions()`` — the System-Condition standard's own
        live registry, see ``core/conditions.py`` — is non-empty at finish.
        Critical severity is never tolerated (scope follows from severity
        alone, unconditionally), so a critical condition still active here
        would already have been caught by this same tick's own EMERGENCY
        check in ``_tick_body()`` before ``_finish_run()`` could ever be
        reached; this is the defensive backstop for that invariant, not a
        path expected to fire in the ordinary case.

        A merely hold-only flag (e.g. ``helium_low``) still tripped at
        finish does NOT send the operation to EMERGENCY: it never causes
        EMERGENCY in the first place (see the System-Condition standard),
        and it keeps governing its concerned VIs identically whether the
        machine lands in IDLE or EMERGENCY — the common case for
        HeliumFillOperation finishing before the level has fully recovered.

        Args:
            procedure: The procedure/operation that just finished (captured
                by the caller before clearing ``self._procedure``).

        Returns:
            ``OrchestratorState.EMERGENCY`` or ``OrchestratorState.IDLE``.
        """
        if getattr(procedure, "command_scope", "measurement") != "operation":
            return OrchestratorState.IDLE
        if self._operation_started_from_emergency:
            return OrchestratorState.EMERGENCY
        if self._station.active_critical_conditions():
            return OrchestratorState.EMERGENCY
        return OrchestratorState.IDLE

    def _standby_operation_immediate(self) -> None:
        """Immediate-finish STANDBY handling for an operation.

        Runs exactly once, on the tick after SWEEPING enters STANDBY (the
        ``elif`` state-machine dispatch in ``_tick_body()`` guarantees this —
        by the time this method returns, ``_finish_run()`` has already moved
        the state out of STANDBY). Unlike a procedure, this never waits for
        any ramp — neither the one already in flight when SWEEPING ended, nor
        one ``standby()`` itself starts — to complete: dispatching
        ``standby()``'s plan, evaluating ``postcondition_gates()`` once, and
        ending the run all happen in this single tick. Any ramp still moving
        when the run ends continues under the ordinary manual-ramp handling
        (the IDLE/EMERGENCY->RAMPING transition ``_tick_body()`` already
        applies to any unfinished ramp with no active procedure) — exactly as
        if the operator had started it by hand.
        """
        procedure = self._procedure
        plan = None
        if procedure is not None and hasattr(procedure, "standby"):
            try:
                plan = procedure.standby()
            except Exception:
                logger.exception("Operation standby() raised during immediate finish")
        if plan is not None:
            logger.info("Operation plan (standby): %r", plan)
            self._dispatch_targets(plan.targets)
            allowed_scope = getattr(procedure, "command_scope", "measurement")
            self._station.send_measurement_commands(plan.commands, allowed_scope=allowed_scope)
            if plan.targets or plan.commands:
                self._emit_status("Parking hardware")
                self._emit_setup_actions(plan.targets, plan.commands, verb="Ramping")

        # Refresh the state snapshot before the one-shot evaluation: the
        # standby commands went out within THIS tick, after the last
        # monitoring poll, so a gate reading cached_state would otherwise
        # verify against pre-standby values and report spuriously unmet
        # (e.g. the fill's restore-SLOW-refresh gate). Ramps standby() just
        # started are still honestly mid-flight — only command effects
        # become visible, which is exactly what the gates verify.
        try:
            self._station.get_state()
        except Exception:
            logger.exception("State refresh before postcondition evaluation failed")

        unmet = self._evaluate_postconditions_once(procedure)
        if unmet:
            message = f"Postcondition(s) not met at finish: {', '.join(unmet)}"
            logger.warning(message)
            self._emit_status(f"WARNING: {message}")

        self._finish_run(postconditions_unmet=unmet)

    def _evaluate_postconditions_once(self, procedure: Any) -> list[str]:
        """Evaluate ``procedure.postcondition_gates()`` exactly once.

        Each gate's one-shot ``action`` (if any) runs once and its ``check``
        (if any) is read a single time via ``Gate.check_once()`` — no
        holding, no timeout. A gate that raises, or a ``postcondition_gates()``
        call that raises, is treated as unmet rather than propagating into
        the tick boundary (a broken postcondition check must never prevent
        the run from finishing).

        Args:
            procedure: The operation whose declared gates to evaluate
                (duck-typed — a procedure or test double without
                ``postcondition_gates()`` yields no gates at all).

        Returns:
            The names of every gate whose one-shot check did not hold; ``[]``
            if every gate held (or none were declared).
        """
        gates_fn = getattr(procedure, "postcondition_gates", None)
        if gates_fn is None:
            return []
        try:
            gates = list(gates_fn())
        except Exception:
            logger.exception("postcondition_gates() raised during one-shot evaluation")
            return []
        unmet: list[str] = []
        for gate in gates:
            name = getattr(gate, "name", "unknown")
            try:
                if not gate.check_once():
                    unmet.append(name)
            except Exception:
                logger.exception("postcondition gate %r raised during one-shot evaluation", name)
                unmet.append(name)
        return unmet

    def _finish_run(self, postconditions_unmet: list[str] | None = None) -> None:
        """Declare the active run done: emit finished signals and return home.

        Called once the STANDBY wait settles for a procedure, or immediately
        by ``_standby_operation_immediate()`` for an operation. Home is IDLE
        for a plain procedure, or for an operation whose safety condition has
        cleared; an operation returns to EMERGENCY instead when appropriate
        (see ``_operation_end_state()``).

        Args:
            postconditions_unmet: Gate names an operation's one-shot
                postcondition evaluation found unmet, or ``None``
                (recorded as ``[]`` — always the case for a procedure, which
                has no postcondition_gates() phase at all).
        """
        procedure = self._procedure
        is_operation = self._is_operation_active()
        label = "Operation" if is_operation else "Procedure"
        self._emit_status(f"{label} finished")
        self._emit_run_finished("done", postconditions_unmet=postconditions_unmet)
        # procedure_finished is PROCEDURE-EXCLUSIVE (the hard status
        # separation) — the Procedure window's queue-advance/progress-reset
        # handler must never fire for an operation's completion.
        if not is_operation:
            self.procedure_finished.emit()
        end_state = self._operation_end_state(procedure)
        if end_state != OrchestratorState.IDLE:
            # Emitted while self._procedure is still set (below), so this
            # correctly routes through operation_status — only an operation
            # can reach a non-IDLE end_state (see _operation_end_state()).
            self._emit_status(
                "Operation finished; a safety condition is still active — "
                "remaining in EMERGENCY."
            )
        self._procedure = None
        self._active_claims = None
        self._active_system_vis.clear()
        self._standby_dispatched = False
        self._operation_started_from_emergency = False
        self._change_state(end_state)
        if end_state == OrchestratorState.IDLE:
            self.run_queue()

    def _fail_to_error(self, message: str) -> None:
        """Contain a failure: clean up the run and degrade to ERROR.

        Reserved for unknown-blast-radius failures: an unhandled
        exception at the tick boundary, or a run whose ``initiate()``/setup
        itself raised (the run never got far enough to know which VI, if
        any, is to blame). A stale CLAIMED VI mid-run has a KNOWN, narrow
        blast radius and uses ``_fail_run_for_fault()`` instead — it does
        not degrade to global ERROR.
        """
        self._error(message)
        try:
            self._abort_active_procedure()
        except Exception:
            logger.exception("Cleanup while entering ERROR also failed")
        self._emit_run_finished("failed", reason=message)
        self._change_state(OrchestratorState.ERROR)

    def _fail_run_for_fault(self, vi_name: str, reason: str | None = None) -> None:
        """Fail the active run because its claimed/watched VI faulted or was held.

        Unlike ``_fail_to_error()``, this does NOT degrade to global ERROR:
        the blast radius is known (one VI, already recorded as a
        comm- or safety-origin ``Condition`` in the Station's unified
        registry — the System-Condition standard, ``core/conditions.py``),
        so only the run ends — every other instrument, including this one
        once it recovers, stays usable.
        Deliberately does NOT call ``run_queue()`` afterward: a run failing
        for an instrument fault or safety hold must not silently
        auto-continue to the next queued run, the same conservative
        behavior the old global-ERROR path had.

        Args:
            vi_name: The claimed/watched VI that went stale, or was held by
                a safety concern, during the run.
            reason: Human-readable description of why the run is failing.
                ``None`` (the default) assumes a stale VI — every other
                caller (the safety-hold path) passes its own message.
        """
        if reason is None:
            message = (
                f"Run failed: active VI '{vi_name}' became stale. The instrument "
                "is quarantined; every other instrument stays usable."
            )
        else:
            message = f"Run failed: {reason}"
        self._error(message, vi_name=vi_name, kind="run_failure", severity="error")
        try:
            self._abort_active_procedure()
        except Exception:
            logger.exception("Cleanup while failing run for VI fault also failed")
        self._emit_run_finished("failed", reason=message)
        self._change_state(OrchestratorState.IDLE)

    def _enter_emergency(self, reason: str, vi_names: tuple[str, ...] = ()) -> None:
        """One-shot emergency entry: clean up the run, then safe shutdown.

        The shutdown runs exactly once here (not every tick): repeating it
        each tick would, for a persistent magnet, restart the full
        switch-heater warmup/cooldown cycle every few seconds.

        Args:
            reason: Human-readable description of the tripped condition(s)
                (e.g. flag names or an envelope-violation message).
            vi_names: The VI(s) that originated the condition (from each
                critical ``Condition.source_vis``), so the reason and its
                ``ErrorEvent`` name the instrument. Empty when no per-VI
                attribution is available (e.g. a session-envelope
                violation, which is checked against a live reading rather
                than a VI-tagged safety flag).
        """
        message = f"EMERGENCY: safety condition triggered ({reason})"
        if vi_names:
            message += f" — instrument(s): {', '.join(vi_names)}"
        self._error(
            message,
            vi_name=", ".join(vi_names) if vi_names else None,
            kind="safety",
            severity="emergency",
        )
        # Defense in depth alongside _manual_action_admissible()'s own
        # EMERGENCY/ERROR guard on the hold override: a fresh EMERGENCY
        # starts with a clean slate for BOTH override kinds, so a hold
        # acknowledged moments earlier (e.g. a helium_low ack still live on
        # magnet_z) cannot be mistaken for an EMERGENCY acknowledge once a
        # quench trips this same tick.
        self._emergency_override_until = None
        self._hold_override_until = {}
        try:
            self._abort_active_procedure()
        except Exception:
            logger.exception("Cleanup while entering EMERGENCY failed")
        self._emit_run_finished("failed", reason=f"EMERGENCY: {reason}")
        self._change_state(OrchestratorState.EMERGENCY)
        # Always a blanket standby_all(): critical severity is station-wide
        # scope by construction (the System-Condition standard, core/
        # conditions.py) — there is no "concerned VI" subset to narrow the
        # shutdown to, whether the cause was a safety flag (vi_names
        # attributed) or a session-envelope violation (vi_names empty).
        try:
            self._station.standby_all()
            self._error("Emergency shutdown executed.")
        except Exception:
            logger.exception("standby_all during emergency entry failed")
            self._error("Emergency shutdown could not complete — check instruments.")

    def _error(
        self,
        message: str,
        *,
        vi_name: str | None = None,
        kind: str = "internal",
        severity: str = "error",
    ) -> None:
        """Report an error: log it, emit the compat + structured signals.

        Every call here emits BOTH ``error_occurred`` (compat, unchanged
        shape) and the richer ``error_event`` — see the class
        docstring's ``error_event`` entry for why a plain per-VI fault
        (``kind="fault"``, severity ``"warning"``) does NOT go through this
        method (see ``_emit_fault_event()`` instead).

        Args:
            message: Human-readable description.
            vi_name: The originating VI, if any (``None`` for a machine-wide
                event, or a comma-joined list for more than one VI).
            kind: ``"internal"`` (default, unhandled tick-boundary
                exception), ``"run_failure"``, or ``"safety"``.
            severity: ``"error"`` (default) or ``"emergency"``.
        """
        logger.error(message)
        self.error_occurred.emit(message)
        try:
            self.error_event.emit(
                ErrorEvent(
                    vi_name=vi_name,
                    kind=kind,
                    severity=severity,
                    message=message,
                    timestamp=time.time(),
                )
            )
        except Exception:  # noqa: BLE001 — a signal-emit failure must never disrupt the run
            logger.exception("error_event emit failed in _error")
        # Also surface in the concise status log as a persistent history line.
        # logger.error above already wrote it to file, so emit the signal
        # directly (bypassing _emit_status's logger) to avoid double file
        # logging — but keep _emit_status's run-kind ROUTING (the hard
        # status separation): an operation's failure line belongs on its
        # card, never in the Procedure window's status log.
        try:
            if self._is_operation_active():
                self.operation_status.emit(message)
            else:
                self.status_message.emit(message)
        except Exception:  # noqa: BLE001 — status must never disrupt the run
            logger.exception("status emit failed in _error")

    def _emit_fault_event(self, vi_name: str, kind: str, message: str) -> None:
        """Emit a warning-severity ``ErrorEvent`` for an unclaimed VI's runtime fault.

        Deliberately does NOT call ``_error()`` — a stale/disconnected VI
        outside the active run's claim (or with no run active at all) is a
        per-instrument warning, not a run- or machine-wide error, and mere
        staleness never fired ``error_occurred`` before this plan. Keeping
        this a separate, quieter path preserves that for any window still
        only listening to the compat signal (ProcedureWindow) while giving
        fault-aware Monitor UI (instrument panels, banner) the structured
        event.

        Args:
            vi_name: The faulted VI.
            kind: ``"stale"`` or ``"disconnected"`` (the Station
                ``FaultRecord.kind``).
            message: Human-readable description of the fault.
        """
        logger.warning("VI fault: %s (%s) — %s", vi_name, kind, message)
        try:
            self.error_event.emit(
                ErrorEvent(
                    vi_name=vi_name,
                    kind="fault",
                    severity="warning",
                    message=message,
                    timestamp=time.time(),
                )
            )
        except Exception:  # noqa: BLE001 — a signal-emit failure must never disrupt the run
            logger.exception("error_event emit failed in _emit_fault_event")
