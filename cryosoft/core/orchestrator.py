# ---
# description: |
#   Orchestrator class: cooperative state machine driven by a single QTimer.
#   Manages procedure lifecycle, runs the monitor cycle, routes GUI actions,
#   and handles safety/emergency states. Single-threaded via PyQt6.
# entry_point: Not run directly. Instantiated dynamically.
# dependencies:
#   - PyQt6.QtCore (QObject, QTimer, pyqtSignal)
#   - enum.Enum
#   - cryosoft.core.station.Station
# input: |
#   Constructed with a Station instance and tick interval.
# process: |
#   On _tick() (inside an exception boundary that degrades to ERROR instead
#   of crashing the app): gets state from station, evaluates safety flags on
#   that same snapshot (any tripped flag -> one-shot EMERGENCY entry: abort
#   procedure, stop ramps, standby_all once), checks for stale active system
#   VIs, processes IDLE gui actions, and runs the state machine. STANDBY is a
#   two-phase wait: first for any ramp already in flight when SWEEPING ended,
#   then (after dispatching procedure.standby()'s own targets) for whatever
#   ramp standby() itself started, before declaring the procedure finished.
#   abort/pause/ERROR hold hardware via Station.stop_ramps(); resume
#   re-dispatches the last targets. acknowledge_emergency() is refused while
#   the safety condition persists; recover_from_error() exits ERROR.
# output: |
#   Emits signals: states_updated, state_changed, procedure_progress,
#   procedure_finished, error_occurred, action_blocked, action_succeeded,
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
from enum import Enum
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from cryosoft.core.operational_status import build_operational_status
from cryosoft.core.plan import Command, Target
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
    MEASURING = "MEASURING"
    SWEEPING = "SWEEPING"
    STANDBY = "STANDBY"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    EMERGENCY = "EMERGENCY"


class Orchestrator(QObject):
    """State machine driving measurements and monitoring safety.

    Signals:
        states_updated (dict): Full station state emitted every tick.
        state_changed (str): Emitted when orchestrator state changes.
        procedure_progress (float): 0.0 to 1.0 progress of current procedure.
        procedure_finished (): Emitted when a procedure ends cleanly.
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
    state_changed = pyqtSignal(str)
    procedure_progress = pyqtSignal(float)
    procedure_finished = pyqtSignal()
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
        self._gui_action_queue: list[dict[str, Any]] = []
        self._active_system_vis: set[str] = set()

        self._wait_started = False
        self._wait_start_time = 0.0
        self._current_wait_time = 0.0
        self._standby_dispatched = False

        # Operational-status reporting (runtime troubleshooting signal).
        self._state_entered_at = time.time()
        self._prev_gaps: dict[str, float] = {}
        self._operational_status: dict = {}
        self._status_logger = logging.getLogger("cryosoft.status")
        self._watchdog_state = WatchdogState()
        self._watchdog_config = WatchdogConfig()

        self._pre_pause_state = OrchestratorState.IDLE
        self._paused_wait_elapsed = 0.0
        # Last targets dispatched to the Station — re-dispatched on resume,
        # because pause_procedure() holds the hardware (which forgets its ramp).
        self._last_system_targets: dict[str, Target] = {}

        self._timer = QTimer(self)
        self._timer.setInterval(tick_interval_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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

        self._procedure = procedure
        self._standby_dispatched = False
        self._wait_started = False
        try:
            plan = self._procedure.initiate()
            # The frozen-dataclass repr is the permanent record of exactly what
            # the procedure requested — logged once, at INFO, on receipt.
            logger.info("Procedure plan (initiate): %r", plan)

            # Track active system VIs for stale monitoring
            self._active_system_vis = set(plan.targets.keys())

            self._dispatch_targets(plan.targets)
            self._station.send_measurement_commands(plan.commands)
            self._current_wait_time = plan.wait_s

            self._change_state(OrchestratorState.INITIATING)
            self._emit_initiation_status(plan.targets, plan.commands)
        except Exception as exc:
            logger.exception("run_procedure() failed during setup")
            self._fail_to_error(f"Could not start procedure: {exc}")

    def _dispatch_targets(self, targets: dict[str, Target]) -> None:
        """Forward targets to the Station, remembering them for resume."""
        self._last_system_targets = dict(targets)
        self._station.process_system_targets(targets)

    def queue_procedure(self, procedure: Any) -> None:
        """Add procedure to queue."""
        self._procedure_queue.append(procedure)

    def run_queue(self) -> None:
        """Run the next procedure in the queue if IDLE."""
        if self._state == OrchestratorState.IDLE and self._procedure_queue:
            proc = self._procedure_queue.pop(0)
            self.run_procedure(proc)

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
        self._change_state(OrchestratorState.IDLE)
        self._emit_status("Aborted by user")
        self.run_queue()

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
        # 1. Always monitor
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
        safety = self._station.check_safety(state)
        active_flags = sorted(flag for flag, tripped in safety.items() if tripped)
        if active_flags and self._state != OrchestratorState.EMERGENCY:
            self._enter_emergency(", ".join(active_flags))
            return  # emergency entry already cleaned up; nothing else this tick

        # 2. Stale check during procedure
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
                    if not self._wait_started:
                        self._wait_started = True
                        self._wait_start_time = time.time()
                        if self._current_wait_time > 0:
                            self._emit_status(
                                f"Waiting {self._current_wait_time:g} s at setpoint"
                            )

                    if time.time() - self._wait_start_time >= self._current_wait_time:
                        self._wait_started = False
                        self._change_state(OrchestratorState.MEASURING)
        elif self._state == OrchestratorState.MEASURING:
            if self._procedure:
                self._emit_status(self._measure_status_line())
                self._procedure.measure()
                if hasattr(self._procedure, "get_progress"):
                    self.procedure_progress.emit(self._procedure.get_progress())
                dm = getattr(self._procedure, "_data_manager", None)
                if dm is not None and dm.last_datapoint:
                    self.measurement_ready.emit(dict(dm.last_datapoint))
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
                        self._station.send_measurement_commands(plan.commands)
                        if plan.targets or plan.commands:
                            self._emit_status("Parking hardware")
                            self._emit_setup_actions(plan.targets, plan.commands, verb="Ramping")
                    self._standby_dispatched = True
            else:
                # Wait for the ramp standby() itself just started (if any)
                # before declaring the procedure finished.
                if self._station.check_ramps():
                    self._emit_status("Procedure finished")
                    self.procedure_finished.emit()
                    self._procedure = None
                    self._active_system_vis.clear()
                    self._standby_dispatched = False
                    self._change_state(OrchestratorState.IDLE)
                    self.run_queue()
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
                    self._station.send_measurement_commands(commands)
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
        self._last_system_targets = {}

    def _fail_to_error(self, message: str) -> None:
        """Contain a failure: clean up the run and degrade to ERROR."""
        self._error(message)
        try:
            self._abort_active_procedure()
        except Exception:
            logger.exception("Cleanup while entering ERROR also failed")
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
