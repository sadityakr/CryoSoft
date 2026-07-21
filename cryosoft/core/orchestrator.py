# ---
# description: |
#   Orchestrator class: cooperative state machine driven by a single QTimer.
#   Manages procedure AND operation lifecycle, runs the monitor cycle, routes
#   GUI actions, and handles safety/emergency states. Single-threaded via
#   PyQt6. An operation (cryosoft.core.operation.OperationBase — detected by
#   duck-typing via command_scope == "operation", never imported here, to
#   keep import-linter contract C5 clean) is a second request type driven by
#   the SAME state machine: run_operation()/queue_operation() give it
#   queue-jumping priority and a narrow EMERGENCY carve-out
#   (docs/plans/cryogenics-logbook.md §4.2), and its plans may carry
#   operation-scope @control commands a procedure's may not (§5).
# entry_point: Not run directly. Instantiated dynamically.
# dependencies:
#   - PyQt6.QtCore (QObject, QTimer, pyqtSignal)
#   - enum.Enum
#   - cryosoft.core.station.Station
# input: |
#   Constructed with a Station instance and tick interval. Monitoring starts
#   OFF: the tick timer runs from construction (it processes GUI actions and
#   the state machine), but no instrument is polled until start_monitoring()
#   — so a freshly launched app does not fire errors while instruments are
#   still being initiated. run_procedure()/run_operation() auto-start
#   monitoring (the safety watchdog is mandatory during a run);
#   stop_monitoring() is refused outside IDLE/ERROR; shutdown() stops the
#   tick timer for good.
# process: |
#   On _tick() (inside an exception boundary that degrades to ERROR instead
#   of crashing the app): while monitoring is active, gets state from
#   station (which also populates/clears its runtime FaultRecord registry —
#   docs/plans/operation-concurrency-and-error-scoping.md §3), emits a
#   warning-severity error_event for each newly faulted VI the active run
#   does not claim (no state change), evaluates safety flags on that same
#   snapshot — subtracting the active operation's tolerated_safety_flags
#   first, if one is running — (any remaining tripped flag -> one-shot
#   EMERGENCY entry, its reason/ErrorEvent naming the originating VI(s) via
#   Station.safety_flag_sources(): abort procedure, stop ramps, standby_all
#   once), and checks for stale ACTIVE (claimed) system VIs — a stale
#   claimed VI fails the run (_fail_run_for_fault(): run_finished "failed",
#   error_event kind="run_failure") and returns the machine to IDLE (NOT
#   global ERROR — only that VI is quarantined; the queue does not
#   auto-continue). _manual_action_admissible() refuses any VI with an
#   active fault outright, before its other admission rules, including in
#   IDLE. Then (monitoring or not) processes IDLE gui actions and runs the
#   state machine. STANDBY forks on the active run's kind (duck-typed via
#   command_scope, docs/plans/operation-concurrency-and-error-scoping.md §2):
#   a PROCEDURE keeps the original two-phase wait (for any ramp already in
#   flight when SWEEPING ended, then — after dispatching procedure.standby()'s
#   own targets — for whatever ramp standby() itself started) before declaring
#   the run finished; an OPERATION finishes immediately instead — on the next
#   tick after entering STANDBY, standby()'s plan is dispatched, its declared
#   postcondition_gates() are each evaluated exactly once (no holding, no
#   timeout — unmet gates are recorded on the manifest and logged, never
#   blocking), and the run ends right there, without waiting for any ramp to
#   complete (a still-moving ramp continues under the ordinary manual-ramp
#   handling). abort/pause/ERROR hold hardware via Station.stop_ramps();
#   resume re-dispatches the last targets. acknowledge_emergency() is refused
#   while the safety condition persists (its own check, NOT tolerance-aware —
#   unchanged); recover_from_error() exits ERROR. A finishing operation
#   returns to EMERGENCY instead of IDLE when it was started via the
#   carve-out or a safety flag is still tripped.
# output: |
#   Emits signals: states_updated, state_changed, run_started/run_finished
#   (run manifests: id, procedure, kind, params, data file path, timestamps,
#   terminal status, postconditions_unmet, summary (duck-typed
#   procedure.run_summary(), {} by default — plan operation-concurrency-and-
#   error-scoping.md §4) — consumed by the session layer; kind is
#   "operation" for an operation via its run_kind class attribute),
#   error_occurred, action_blocked, action_succeeded, action_failed (vi,
#   method, reason — the uniform per-action verdict). Run-scoped UI signals
#   are routed by run kind (hard status separation, plan §2): status_message,
#   procedure_progress, procedure_finished, and measurement_ready fire ONLY
#   for a procedure run (the Procedure window's status log/progress
#   bar/plots); operation_status/operation_progress fire instead for an
#   operation run (the Operations panel's OperationCard). status_message is
#   also written to the cryosoft.procedure_status logger (propagates to the
#   main log), operation_status to cryosoft.operation_status — distinct from
#   the machine-only cryosoft.status JSONL stream.
# ---

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

from cryosoft.core.events import ErrorEvent
from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.core.operational_status import build_operational_status
from cryosoft.core.plan import Command, SessionEnvelope, Target
from cryosoft.core.station import FaultRecord, Station
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
            (docs/plans/operation-concurrency-and-error-scoping.md §4: the
            dict ``procedure.run_summary()`` returned — duck-typed, ``{}``
            for a procedure or an operation that does not override it, and
            ``{}`` rather than propagating if the override raised) added.
        error_occurred (str): Emitted when ERROR or EMERGENCY state entered,
            or a run fails (plan operation-concurrency-and-error-scoping.md
            §3). Not run-scoped — fires regardless of run kind. Kept as a
            thin compat wrapper: every emission here has a matching, richer
            ``error_event`` emitted alongside it.
        error_event (ErrorEvent): Structured counterpart of
            ``error_occurred``/a VI-scoped fault (plan §3, ``core.events.
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
    error_event = pyqtSignal(object)  # ErrorEvent — structured error/fault payload (plan §3)
    action_blocked = pyqtSignal(str)
    action_succeeded = pyqtSignal(str, str)
    action_failed = pyqtSignal(str, str, str)
    instrument_reconnected = pyqtSignal(str)  # offline VI brought live via retry_reconnect()
    measurement_ready = pyqtSignal(dict)  # emitted after each measure() with last_datapoint
    operational_status = pyqtSignal(dict)  # per-tick runtime status record (troubleshooting)
    status_message = pyqtSignal(str)  # concise, human-readable PROCEDURE milestone line
    operation_status = pyqtSignal(str)  # concise, human-readable OPERATION milestone line
    operation_progress = pyqtSignal(float)  # 0.0-1.0 progress of the current OPERATION run

    def __init__(self, station: Station, tick_interval_ms: int = 3000) -> None:
        super().__init__()
        self._station = station
        self._state = OrchestratorState.IDLE
        self._procedure: Any = None
        self._procedure_queue: list[Any] = []
        # Operations (L4, duck-typed via command_scope == "operation") queue
        # separately and always drain first — see run_operation()/
        # queue_operation()/run_queue() and plan §4.2's "queue-jumping, not
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

        # Set by run_operation() when the EMERGENCY carve-out (plan §4.2) was
        # used to start the active operation; read by _operation_end_state()
        # so a finishing operation returns to EMERGENCY rather than IDLE when
        # appropriate. Meaningless (and unread) for a plain procedure.
        self._operation_started_from_emergency: bool = False

        # Set by acknowledge_emergency() when the operator acknowledges an
        # EMERGENCY whose safety condition is still active: unlocks
        # submit_vi_action() for manual front-panel recovery (e.g. cycling a
        # switch heater by hand) without leaving EMERGENCY. Procedures and
        # operations stay refused regardless — their gates check self._state,
        # not this flag. Reset on every fresh EMERGENCY entry and on the
        # eventual return to IDLE, so a new emergency always starts locked.
        self._emergency_manual_override: bool = False

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

        # Session envelope (sample-specific bounds, narrower than config
        # limits) — set by the session layer, enforced here so it binds every
        # writer (GUI and agents alike). None = no envelope active.
        self._session_envelope: SessionEnvelope | None = None

        # Active run manifest: captured at run_started (the data file path is
        # gone by the time the run ends) and re-emitted once on run_finished.
        self._active_run_manifest: dict[str, Any] | None = None
        self._run_counter = 0

        # run_summary() hand-off (docs/plans/operation-concurrency-and-
        # error-scoping.md §4): collected from self._procedure by
        # _emit_run_finished() for the "done" path, where self._procedure is
        # still set. The abort/fail/emergency paths clear self._procedure in
        # _abort_active_procedure() BEFORE calling _emit_run_finished(), so
        # that method caches the summary here first — _emit_run_finished()
        # prefers self._procedure when present, else falls back to this.
        self._pending_run_summary: dict[str, Any] = {}

        # Claims + admission gate (docs/plans/operation-concurrency-and-
        # error-scoping.md §1): the active run's claimed_vi_names(), captured
        # once at _start_run() and cleared on EVERY teardown path (finish,
        # abort, fail, emergency — see _abort_active_procedure()/
        # _finish_run()). None while no run is active, or while the active
        # run claims everything (the default for every procedure and for an
        # operation that does not override claimed_vi_names()).
        self._active_claims: set[str] | None = None

        # Runtime fault registry tracking (plan §3): the set of VI names
        # with an active Station fault as of the last tick, used to detect
        # NEW faults (emit one warning error_event, not one per tick) and
        # recoveries. Station is the source of truth; this is only a
        # transition-detection cache.
        self._known_fault_vis: set[str] = set()

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

    def set_session_envelope(self, envelope: SessionEnvelope | None) -> None:
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
        """Start an operation immediately if permitted; else refuse it (plan §4.2).

        Allowed from IDLE, from a manual ramp (cancelled first, exactly like
        ``run_procedure()``), and — the narrow EMERGENCY carve-out — from
        EMERGENCY iff every currently tripped safety flag (from
        ``Station.check_safety()``) is in the operation's
        ``tolerated_safety_flags``.

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
        # Claims + admission gate (plan §1): captured here, duck-typed (never
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
        (see ``run_queue()``) — the queueing half of plan §4.2's
        "queue-jumping, not preemption".
        """
        self._operation_queue.append(operation)

    def run_queue(self) -> None:
        """Run the next queued operation, else the next queued procedure, if IDLE.

        Operations always drain before procedures (plan §4.2).
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
        flow owns cleanup there and is exited via acknowledge_emergency().
        """
        if self._state == OrchestratorState.EMERGENCY:
            logger.info("abort_procedure ignored during EMERGENCY")
            return
        self._abort_active_procedure()
        self._emit_run_finished("aborted")
        self._change_state(OrchestratorState.IDLE)
        self._emit_status("Aborted by user")
        self.run_queue()

    def finish_operation(self) -> None:
        """Request a graceful stop of the active operation (plan §4.3).

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
        """Record an operator confirmation on the active operation (plan §8.2).

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

    def recover_from_error(self) -> None:
        """Return to IDLE after the user has reviewed an ERROR condition.

        The failed procedure was already cleaned up on ERROR entry. Queued
        procedures are NOT auto-started — after an error the queue's
        assumptions may no longer hold; the user restarts explicitly.
        """
        if self._state == OrchestratorState.ERROR:
            self._change_state(OrchestratorState.IDLE)

    def acknowledge_emergency(self) -> None:
        """Acknowledge an EMERGENCY: unlock manual control, or return to IDLE.

        If the safety condition is still active, acknowledging cannot return
        to IDLE (the next tick would bounce straight back), but it does
        unlock ``submit_vi_action`` for manual front-panel recovery — e.g.
        cycling a switch heater by hand — while remaining in EMERGENCY.
        Starting a procedure or operation stays refused throughout: those
        gates check the state itself, which is unchanged here.

        Once the condition has cleared, acknowledging again (the same
        button) returns to IDLE and relocks the override for the next
        emergency.
        """
        if self._state != OrchestratorState.EMERGENCY:
            return
        safety = self._station.check_safety()
        active = sorted(flag for flag, tripped in safety.items() if tripped)
        # A still-violated session envelope blocks the return to IDLE for the
        # same reason a safety flag does — the next tick would bounce straight
        # back.
        if self._session_envelope is not None:
            active.extend(self._session_envelope.check_state(self._station.cached_state))
        if active:
            if not self._emergency_manual_override:
                self._emergency_manual_override = True
                self._emit_status(
                    "Emergency acknowledged — front-panel manual control "
                    f"unlocked. Condition still active: {', '.join(active)}"
                )
            else:
                self._error(
                    "Cannot return to IDLE: condition still active "
                    f"({', '.join(active)})"
                )
            return
        self._emergency_manual_override = False
        self._change_state(OrchestratorState.IDLE)

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

        Used only to compose admission-refusal messages (plan §1) — never
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

    def _manual_action_admissible(self, vi_name: str) -> tuple[bool, str]:
        """Decide whether a manual action on *vi_name* may be admitted right now.

        The single admission predicate (plan §1's "Claims + admission gate"),
        shared verbatim by ``submit_vi_action()`` (what may be *queued*) and
        the ``_tick_body()`` GUI-action drain gate (what may be *drained*) —
        they must agree, or a queued action could sit forever without a
        verdict.

        Admission rules, in order:

        0. A VI with an active runtime fault (plan §3) is ALWAYS refused,
           regardless of state — including IDLE — until it recovers or
           ``retry_fault()`` succeeds. Checked first, and here (not as a
           parallel check) so every caller (``submit_vi_action()``, the
           drain gate) inherits it for free.
        1. IDLE, or a manual ramp (RAMPING with no active run), or EMERGENCY
           once ``acknowledge_emergency()`` unlocked manual override: always
           admitted (unchanged from before claims existed).
        2. ERROR, or EMERGENCY without the override: always refused, naming
           the state.
        3. Otherwise a run is active. Admitted iff the active run's
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
        fault = self._station.vi_faults().get(vi_name)
        if fault is not None:
            return False, (
                f"Cannot control {vi_name}: instrument fault ({fault.kind}) — "
                f"{fault.message}. Retry the instrument or wait for it to recover."
            )
        if self._state == OrchestratorState.IDLE:
            return True, ""
        manual_ramping = (
            self._state == OrchestratorState.RAMPING and self._procedure is None
        )
        if manual_ramping:
            return True, ""
        emergency_override = (
            self._state == OrchestratorState.EMERGENCY and self._emergency_manual_override
        )
        if emergency_override:
            return True, ""
        if self._state in (OrchestratorState.ERROR, OrchestratorState.EMERGENCY):
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

        Admission is decided by ``_manual_action_admissible()`` (plan §1):
        IDLE / a manual ramp / an EMERGENCY manual override always admit;
        ERROR / EMERGENCY (without override) always refuse; otherwise a run
        is active and the action is admitted iff *vi_name* is not one of the
        active run's claimed VIs.
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

    def retry_reconnect(self, vi_name: str) -> None:
        """Try to bring an offline instrument online (GUI reconnect action).

        Delegates to ``Station.retry_instrument()`` and reports the verdict
        through the standard action signals: ``action_succeeded(vi_name,
        "reconnect")`` plus ``instrument_reconnected(vi_name)`` on success,
        ``action_failed`` with the reason otherwise.

        Allowed only in IDLE: a VI joining the station mid-procedure would
        bypass the run's safety review. Runs synchronously rather than via the
        GUI action queue — the queue dispatches to *registered* VIs, which an
        offline one is not, and everything is on the one thread anyway, so no
        tick can interleave with the reconnect (the single-writer guarantee
        holds).

        Args:
            vi_name: Name of the offline VI to reconnect.
        """
        if self._state != OrchestratorState.IDLE:
            msg = (
                f"Cannot reconnect {vi_name}: Orchestrator is in state "
                f"{self._state.name}, reconnect requires IDLE"
            )
            logger.info("Blocked reconnect: %s", msg)
            self.action_blocked.emit(msg)
            return
        ok, message = self._station.retry_instrument(vi_name)
        if ok:
            # A reconnected switch VI must be adoptable as the scanner —
            # re-run the same first-switch resolution done at construction.
            if self._scanner_vi_name is None:
                switch_names = self._station.switch_vi_names()
                self._scanner_vi_name = switch_names[0] if switch_names else None
            logger.info("Reconnect succeeded for '%s'", vi_name)
            self.instrument_reconnected.emit(vi_name)
            self.action_succeeded.emit(vi_name, "reconnect")
        else:
            logger.warning("Reconnect failed for '%s': %s", vi_name, message)
            self.action_failed.emit(vi_name, "reconnect", message)

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
        """Retry a VI's active runtime fault: reset counters, poll once (plan §3).

        The runtime counterpart of ``retry_reconnect()``: it never rebuilds
        a driver (the VI is already live) — only ``Station.retry_fault()``'s
        counter-reset-and-repoll. Unlike ``retry_reconnect()`` this is not
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

        Duck-typed (docs/plans/operation-concurrency-and-error-scoping.md
        §4): looked up via ``getattr`` so this module never imports
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
                postcondition evaluation found unmet at finish (plan §2), or
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
        # run_summary() hand-off (plan §4): self._procedure is still set on
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
            ramp_info = self._station.get_ramp_status()
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
                # (plan operation-concurrency-and-error-scoping.md §2 —
                # evaluated once, immediately, as the run ends), so only the
                # initiation/reading gates can ever be "active" across ticks.
                active_gates=[g.name for g in self._pending_gates],
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
            # from this tick's snapshot, emitted, and appended to logs/status.jsonl.
            self._update_operational_status(state)

            # Runtime fault registry (plan §3): Station.get_state() (just
            # called above) already populated/cleared FaultRecords for
            # anything stale/disconnected this tick. Detect NEW faults (one
            # warning event each, not one per tick) for VIs the active run
            # does not claim — a claimed VI's fault is handled by the
            # run-failure path below instead, with a matching, more severe
            # event, so it must not ALSO get a warning here.
            current_faults = self._station.vi_faults()
            new_fault_names = set(current_faults) - self._known_fault_vis
            self._known_fault_vis = set(current_faults)
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
            for vi_name in sorted(new_fault_names):
                if run_active and vi_name in watched_vis:
                    continue  # handled as a run failure below, this same tick
                record = current_faults[vi_name]
                self._emit_fault_event(vi_name, record.kind, record.message)

            # Safety check — reuses this tick's snapshot (no second hardware poll).
            # An active operation's tolerated_safety_flags (plan §7) are
            # subtracted before deciding on EMERGENCY: a tolerated flag (e.g.
            # helium_low during a helium-fill operation) must not abort the
            # very operation that exists to fix it. A non-tolerated flag
            # (e.g. quench) still enters EMERGENCY exactly as for any
            # procedure. Only the ACTIVE procedure's tolerance applies here —
            # a plain procedure (or IDLE) tolerates nothing, unchanged.
            safety = self._station.check_safety(state)
            tripped_flags = {flag for flag, tripped in safety.items() if tripped}
            if self._procedure is not None and (
                getattr(self._procedure, "command_scope", "measurement") == "operation"
            ):
                tolerated = frozenset(
                    getattr(self._procedure, "tolerated_safety_flags", frozenset())
                )
                tripped_flags = tripped_flags - tolerated
            active_flags = sorted(tripped_flags)
            if active_flags and self._state != OrchestratorState.EMERGENCY:
                sources = self._station.safety_flag_sources(state)
                vi_names = tuple(sorted({
                    vi_name for flag in active_flags for vi_name in sources.get(flag, [])
                }))
                self._enter_emergency(", ".join(active_flags), vi_names)
                return  # emergency entry already cleaned up; nothing else this tick

            # Session-envelope check — same snapshot, same consequence as a tripped
            # safety flag: the envelope protects the mounted sample, so a live
            # reading outside it is treated exactly like an instrument safety event.
            if self._session_envelope is not None and self._state != OrchestratorState.EMERGENCY:
                envelope_violations = self._session_envelope.check_state(state)
                if envelope_violations:
                    self._enter_emergency("; ".join(envelope_violations))
                    return

            # Stale ACTIVE (claimed/system) VI during a run (plan §3): the
            # run fails and its VI's fault stands in the Station registry,
            # but — unlike the old behavior — the machine returns to IDLE
            # rather than global ERROR, so every other instrument stays
            # usable. A stale UNCLAIMED VI never reaches here at all: it was
            # already handled (as a warning-severity fault, no state change)
            # by the fault-transition block above.
            if run_active:
                for vi_name in sorted(watched_vis):
                    vi_state = state.get(vi_name, {})
                    if vi_state.get("_stale"):
                        self._fail_run_for_fault(vi_name)
                        return

        # 3. GUI Actions — each queued action gets the SAME verdict
        # submit_vi_action() would give it right now, via the shared
        # _manual_action_admissible() predicate (plan §1): the run may have
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
        # If a GUI action started (or restarted) a manual ramp, enter RAMPING.
        if self._state == OrchestratorState.IDLE and not self._station.check_ramps():
            self._change_state(OrchestratorState.RAMPING)

        # 4. State Machine matching
        if self._state == OrchestratorState.IDLE:
            pass
        elif self._state == OrchestratorState.INITIATING:
            self._change_state(OrchestratorState.RAMPING)
        elif self._state == OrchestratorState.RAMPING:
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
                # measurement_ready is PROCEDURE-EXCLUSIVE (plan §2's hard
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
            # Monitoring continues; awaiting acknowledge_emergency().
            pass

    # ------------------------------------------------------------------
    # Failure handling
    # ------------------------------------------------------------------

    def _abort_active_procedure(self) -> None:
        """Clean up the running procedure: data file, measurement VI, ramps.

        Safe to call with no procedure active (stops manual ramps then).
        Each cleanup step is individually guarded so one failure (e.g. a dead
        instrument) cannot prevent the others. Also clears ``_active_claims``
        (plan §1) — the shared teardown path for user abort, ``_fail_to_error``,
        and ``_enter_emergency``, so a claim can never outlive its run.

        Caches ``procedure.run_summary()`` (plan §4) into
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
        """Return the state a finishing run should return to (plan §4.2/§7).

        A plain procedure always returns to IDLE. An operation returns to
        EMERGENCY instead when it was started via the EMERGENCY carve-out, or
        when any safety flag — even one this operation tolerated — is still
        tripped at finish: a tolerated flag was tolerated for THIS operation
        only, it was never cleared. An operation could not reach this "done"
        path with a non-tolerated flag active, because the tick safety check
        would already have escalated it to EMERGENCY and aborted the run.

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
        safety = self._station.check_safety()
        if any(safety.values()):
            return OrchestratorState.EMERGENCY
        return OrchestratorState.IDLE

    def _standby_operation_immediate(self) -> None:
        """Immediate-finish STANDBY handling for an operation (plan §2).

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

        unmet = self._evaluate_postconditions_once(procedure)
        if unmet:
            message = f"Postcondition(s) not met at finish: {', '.join(unmet)}"
            logger.warning(message)
            self._emit_status(f"WARNING: {message}")

        self._finish_run(postconditions_unmet=unmet)

    def _evaluate_postconditions_once(self, procedure: Any) -> list[str]:
        """Evaluate ``procedure.postcondition_gates()`` exactly once (plan §2).

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
        # procedure_finished is PROCEDURE-EXCLUSIVE (plan §2's hard status
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

        Reserved for unknown-blast-radius failures (plan §3): an unhandled
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

    def _fail_run_for_fault(self, vi_name: str) -> None:
        """Fail the active run because its claimed VI faulted (plan §3).

        Unlike ``_fail_to_error()``, this does NOT degrade to global ERROR:
        the blast radius is known (one VI, already recorded in the Station's
        fault registry by ``get_state()``), so only the run ends — every
        other instrument, including this one once it recovers or is
        retried, stays usable. Deliberately does NOT call ``run_queue()``
        afterward: a run failing for an instrument fault must not silently
        auto-continue to the next queued run, the same conservative
        behavior the old global-ERROR path had.

        Args:
            vi_name: The claimed VI that went stale during the run.
        """
        message = (
            f"Run failed: active VI '{vi_name}' became stale. The instrument "
            "is quarantined; every other instrument stays usable."
        )
        self._error(message, vi_name=vi_name, kind="run_failure", severity="error")
        try:
            self._abort_active_procedure()
        except Exception:
            logger.exception("Cleanup while failing run for VI fault also failed")
        self._emit_run_finished("failed", reason=message)
        self._change_state(OrchestratorState.IDLE)

    def _enter_emergency(self, reason: str, vi_names: tuple[str, ...] = ()) -> None:
        """One-shot emergency entry: clean up the run, then safe shutdown.

        The shutdown runs exactly once here (not every tick): repeating
        standby_all() each tick would, for a persistent magnet, restart the
        full switch-heater warmup/cooldown cycle every few seconds.

        Args:
            reason: Human-readable description of the tripped condition(s)
                (e.g. flag names or an envelope-violation message).
            vi_names: The VI(s) that originated the condition (plan §3,
                from ``Station.safety_flag_sources()``), so the reason and
                its ``ErrorEvent`` name the instrument. Empty when no
                per-VI attribution is available (e.g. a session-envelope
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
        self._emergency_manual_override = False
        try:
            self._abort_active_procedure()
        except Exception:
            logger.exception("Cleanup while entering EMERGENCY failed")
        self._emit_run_finished("failed", reason=f"EMERGENCY: {reason}")
        self._change_state(OrchestratorState.EMERGENCY)
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
        shape) and the richer ``error_event`` (plan §3) — see the class
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
        # logging — but keep _emit_status's run-kind ROUTING (plan §2's hard
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
