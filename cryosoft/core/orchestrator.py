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
#   station, evaluates safety flags on that same snapshot — subtracting the
#   active operation's tolerated_safety_flags first, if one is running — (any
#   remaining tripped flag -> one-shot EMERGENCY entry: abort procedure, stop
#   ramps, standby_all once) and checks for stale active system VIs; then
#   (monitoring or not) processes IDLE gui actions and runs the state
#   machine. STANDBY is a three-phase wait: first for any ramp already in
#   flight when SWEEPING ended, then (after dispatching procedure.standby()'s
#   own targets) for whatever ramp standby() itself started, then — a
#   duck-typed postcondition_gates() phase, inert for a plain procedure —
#   stepping the active procedure's declared postcondition gates (a timeout
#   degrades to ERROR naming the unmet gate) before declaring the run
#   finished. abort/pause/ERROR hold hardware via Station.stop_ramps();
#   resume re-dispatches the last targets. acknowledge_emergency() is refused
#   while the safety condition persists (its own check, NOT tolerance-aware —
#   unchanged); recover_from_error() exits ERROR. A finishing operation
#   returns to EMERGENCY instead of IDLE when it was started via the
#   carve-out or a safety flag is still tripped.
# output: |
#   Emits signals: states_updated, state_changed, procedure_progress,
#   procedure_finished, run_started/run_finished (run manifests: id, procedure,
#   kind, params, data file path, timestamps, terminal status — consumed by the
#   session layer; kind is "operation" for an operation via its run_kind class
#   attribute), error_occurred, action_blocked, action_succeeded,
#   action_failed (vi, method, reason — the uniform per-action verdict),
#   status_message (concise human-readable procedure milestones for the
#   Procedure window's status log; also written to the cryosoft.procedure_status
#   logger, which propagates to the main log — distinct from the machine-only
#   cryosoft.status JSONL stream)
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

from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.core.operational_status import build_operational_status
from cryosoft.core.plan import Command, SessionEnvelope, Target
from cryosoft.core.station import Station
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
        state_changed (str): Emitted when orchestrator state changes.
        procedure_progress (float): 0.0 to 1.0 progress of current procedure.
        procedure_finished (): Emitted when a procedure ends cleanly.
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
            ``aborted`` / ``failed``), and ``reason`` (error text, empty for
            ``done``/``aborted``) added.
        error_occurred (str): Emitted when ERROR or EMERGENCY state entered.
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
            procedure. Initiation is broken into one line per distinct setup
            action ("Ramping temperature to 300 K", "Ramping field to -1 T",
            "Arming DC resistance measurement"), followed by "Waiting N s at
            setpoint", "Measuring point 13/101", "Point 14/101: ramping field
            -> 0.55 T", etc. Labels/units come from each VI's setpoint metadata
            via the Station, so every procedure gets a status feed with no
            per-procedure code; consumed by the Procedure window's status log.
            Distinct from the per-tick detail stream on the Monitor log.
    """

    states_updated = pyqtSignal(dict)
    monitoring_changed = pyqtSignal(bool)
    state_changed = pyqtSignal(str)
    procedure_progress = pyqtSignal(float)
    procedure_finished = pyqtSignal()
    run_started = pyqtSignal(dict)  # run manifest at successful setup
    run_finished = pyqtSignal(dict)  # same manifest + finished_utc/status/reason
    error_occurred = pyqtSignal(str)
    action_blocked = pyqtSignal(str)
    action_succeeded = pyqtSignal(str, str)
    action_failed = pyqtSignal(str, str, str)
    measurement_ready = pyqtSignal(dict)  # emitted after each measure() with last_datapoint
    operational_status = pyqtSignal(dict)  # per-tick runtime status record (troubleshooting)
    status_message = pyqtSignal(str)  # concise, human-readable procedure milestone line

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

        # Postcondition phase (operations only, duck-typed via
        # postcondition_gates() — a plain procedure has none, so this stays
        # inert for every existing run). Stepped in STANDBY after standby()'s
        # own ramp completes, before the run is declared "done".
        self._postcondition_active: bool = False
        self._postcondition_gates: list = []
        self._postcondition_deadline: float | None = None

        # Set by run_operation() when the EMERGENCY carve-out (plan §4.2) was
        # used to start the active operation; read by _operation_end_state()
        # so a finishing operation returns to EMERGENCY rather than IDLE when
        # appropriate. Meaningless (and unread) for a plain procedure.
        self._operation_started_from_emergency: bool = False

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
        self._standby_dispatched = False
        self._wait_started = False
        self._first_measurement = True
        self._pending_gates = []
        self._postcondition_active = False
        self._postcondition_gates = []
        self._postcondition_deadline = None
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
        is_operation = (
            self._procedure is not None
            and getattr(self._procedure, "command_scope", "measurement") == "operation"
        )
        if not is_operation:
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
        is_operation = (
            self._procedure is not None
            and getattr(self._procedure, "command_scope", "measurement") == "operation"
        )
        if not is_operation:
            msg = "Cannot confirm operation step: no operation is currently running."
            logger.info("Blocked confirm_operation: %s", msg)
            self.action_blocked.emit(msg)
            return
        confirm = getattr(self._procedure, "confirm", None)
        if callable(confirm):
            confirm(key)
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
        """Return to IDLE after an EMERGENCY, once the cause has cleared.

        Refused (with an error signal) while any safety condition is still
        active — acknowledging an ongoing emergency would bounce straight
        back on the next tick.
        """
        if self._state != OrchestratorState.EMERGENCY:
            return
        safety = self._station.check_safety()
        active = sorted(flag for flag, tripped in safety.items() if tripped)
        # A still-violated session envelope blocks acknowledgement for the same
        # reason a safety flag does — the next tick would bounce straight back.
        if self._session_envelope is not None:
            active.extend(self._session_envelope.check_state(self._station.cached_state))
        if active:
            self._error(
                "Cannot acknowledge emergency: condition still active "
                f"({', '.join(active)})"
            )
            return
        self._change_state(OrchestratorState.IDLE)

    def submit_vi_action(self, vi_name: str, method_name: str, **kwargs: Any) -> None:
        """Submit a GUI action to a specific VI."""
        # Allow actions in IDLE or during a manual ramp (RAMPING with no active procedure).
        manual_ramping = (
            self._state == OrchestratorState.RAMPING and self._procedure is None
        )
        if self._state != OrchestratorState.IDLE and not manual_ramping:
            msg = f"Cannot control {vi_name}: procedure is running in state {self._state.name}"
            logger.info("Blocked action: %s", msg)
            self.action_blocked.emit(msg)
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

    def _emit_run_finished(self, status: str, reason: str = "") -> None:
        """Emit ``run_finished`` for the active run, exactly once.

        Idempotent: the captured manifest is cleared on emission, so the
        overlapping cleanup paths (user abort, error containment, emergency
        entry) cannot double-report a run. A no-op when no run ever started
        (e.g. ``initiate()`` itself failed, or a manual-ramp-only session).

        Args:
            status: Terminal status — ``done``, ``aborted``, or ``failed``.
            reason: Error text for ``failed``; empty otherwise.
        """
        if self._active_run_manifest is None:
            return
        manifest = dict(self._active_run_manifest)
        self._active_run_manifest = None
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        manifest["status"] = status
        manifest["reason"] = reason
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
                # Initiation/reading gates and postcondition gates are never
                # both non-empty at once (different sub-phases), so a plain
                # concatenation surfaces whichever is active — postcondition
                # gates the same way initiation gates already are.
                active_gates=(
                    [g.name for g in self._pending_gates]
                    + [g.name for g in self._postcondition_gates]
                ),
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

        The milestone text goes to the ``cryosoft.procedure_status`` logger
        (propagates to the main log for history) — deliberately NOT the
        ``cryosoft.status`` logger, which carries the machine-only JSONL
        operational-status stream and must stay pure JSON.
        """
        try:
            logging.getLogger("cryosoft.procedure_status").info(text)
            self.status_message.emit(text)
        except Exception:  # noqa: BLE001 — status must never disrupt the run
            logger.exception("status_message emit failed")

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
                self._enter_emergency(", ".join(active_flags))
                return  # emergency entry already cleaned up; nothing else this tick

            # Session-envelope check — same snapshot, same consequence as a tripped
            # safety flag: the envelope protects the mounted sample, so a live
            # reading outside it is treated exactly like an instrument safety event.
            if self._session_envelope is not None and self._state != OrchestratorState.EMERGENCY:
                envelope_violations = self._session_envelope.check_state(state)
                if envelope_violations:
                    self._enter_emergency("; ".join(envelope_violations))
                    return

            # Stale check during procedure
            if self._state not in (OrchestratorState.IDLE, OrchestratorState.PAUSED,
                                   OrchestratorState.ERROR, OrchestratorState.EMERGENCY):
                for vi_name in self._active_system_vis:
                    vi_state = state.get(vi_name, {})
                    if vi_state.get("_stale"):
                        self._fail_to_error(
                            f"Active VI '{vi_name}' became stale during procedure."
                        )
                        break

        # 3. GUI Actions — processed in IDLE or during a manual ramp (no active procedure).
        _manual_ramping = (
            self._state == OrchestratorState.RAMPING and self._procedure is None
        )
        if self._state == OrchestratorState.IDLE or _manual_ramping:
            for action in self._gui_action_queue:
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
            self._gui_action_queue.clear()
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
                self._emit_status(self._measure_status_line())
                self._procedure.measure()
                if hasattr(self._procedure, "get_progress"):
                    self.procedure_progress.emit(self._procedure.get_progress())
                last_datapoint = getattr(self._procedure, "last_datapoint", None)
                if last_datapoint:
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
            if not self._standby_dispatched:
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
            elif not self._postcondition_active:
                # Wait for the ramp standby() itself just started (if any)
                # before stepping postconditions (plan §4.1). A plain
                # BaseProcedure declares no postcondition_gates(), so the
                # gate list below comes back empty and the run finishes on
                # the very next tick — unchanged behavior for every existing
                # procedure.
                if self._station.check_ramps():
                    self._postcondition_active = True
                    gates_fn = getattr(self._procedure, "postcondition_gates", None)
                    self._postcondition_gates = list(gates_fn()) if gates_fn else []
                    if self._postcondition_gates:
                        timeout = getattr(
                            self._procedure, "postcondition_timeout_s", 600.0
                        )
                        self._postcondition_deadline = time.time() + float(timeout)
                        self._emit_status("Verifying postconditions")
                    else:
                        self._postcondition_deadline = None
            else:
                # Postcondition sub-phase: step declared gates each tick.
                if self._postcondition_gates:
                    if (
                        self._postcondition_deadline is not None
                        and time.time() >= self._postcondition_deadline
                    ):
                        unmet = ", ".join(g.name for g in self._postcondition_gates)
                        self._fail_to_error(
                            f"Postcondition timeout: unmet gate(s) ({unmet})"
                        )
                    else:
                        self._postcondition_gates = [
                            g for g in self._postcondition_gates if not g.step()
                        ]
                if not self._postcondition_gates and self._state == OrchestratorState.STANDBY:
                    # Re-check state: the timeout branch above may have
                    # already degraded to ERROR this same tick.
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
        instrument) cannot prevent the others.
        """
        procedure = self._procedure
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
        self._active_system_vis.clear()
        self._standby_dispatched = False
        self._wait_started = False
        self._first_measurement = True
        self._pending_gates = []
        self._postcondition_active = False
        self._postcondition_gates = []
        self._postcondition_deadline = None
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

    def _finish_run(self) -> None:
        """Declare the active run done: emit finished signals and return home.

        Called once the STANDBY postcondition sub-phase holds (or, for a
        procedure with no postcondition_gates(), immediately). Home is IDLE
        for a plain procedure, or for an operation whose safety condition has
        cleared; an operation returns to EMERGENCY instead when appropriate
        (see ``_operation_end_state()``).
        """
        procedure = self._procedure
        self._emit_status("Procedure finished")
        self._emit_run_finished("done")
        self.procedure_finished.emit()
        end_state = self._operation_end_state(procedure)
        self._procedure = None
        self._active_system_vis.clear()
        self._standby_dispatched = False
        self._postcondition_active = False
        self._postcondition_gates = []
        self._postcondition_deadline = None
        self._operation_started_from_emergency = False
        self._change_state(end_state)
        if end_state == OrchestratorState.IDLE:
            self.run_queue()
        else:
            self._emit_status(
                "Operation finished; a safety condition is still active — "
                "remaining in EMERGENCY."
            )

    def _fail_to_error(self, message: str) -> None:
        """Contain a failure: clean up the run and degrade to ERROR."""
        self._error(message)
        try:
            self._abort_active_procedure()
        except Exception:
            logger.exception("Cleanup while entering ERROR also failed")
        self._emit_run_finished("failed", reason=message)
        self._change_state(OrchestratorState.ERROR)

    def _enter_emergency(self, reason: str) -> None:
        """One-shot emergency entry: clean up the run, then safe shutdown.

        The shutdown runs exactly once here (not every tick): repeating
        standby_all() each tick would, for a persistent magnet, restart the
        full switch-heater warmup/cooldown cycle every few seconds.
        """
        self._error(f"EMERGENCY: safety condition triggered ({reason})")
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

    def _error(self, message: str) -> None:
        logger.error(message)
        self.error_occurred.emit(message)
        # Also surface in the concise status log as a persistent history line.
        # logger.error above already wrote it to file, so emit the signal
        # directly (bypassing _emit_status) to avoid double file logging.
        try:
            self.status_message.emit(message)
        except Exception:  # noqa: BLE001 — status must never disrupt the run
            logger.exception("status_message emit failed in _error")
