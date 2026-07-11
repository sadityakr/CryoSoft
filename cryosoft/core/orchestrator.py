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
#   On _tick(): gets state from station, checks for stale active system VIs,
#   processes IDLE gui actions, and runs the state machine. STANDBY is a
#   two-phase wait: first for any ramp already in flight when SWEEPING ended,
#   then (after dispatching procedure.standby()'s own targets) for whatever
#   ramp standby() itself started, before declaring the procedure finished.
# output: |
#   Emits signals: states_updated, state_changed, procedure_progress,
#   procedure_finished, error_occurred, action_blocked
# last_updated: 2026-07-12
# ---

"""Orchestrator — cooperative state machine for CryoSoft.

The Orchestrator is single-threaded. The Qt event loop is the only
concurrency mechanism. It drives procedures via a state machine and
continually monitors the system.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from cryosoft.core.station import Station
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
    """

    states_updated = pyqtSignal(dict)
    state_changed = pyqtSignal(str)
    procedure_progress = pyqtSignal(float)
    procedure_finished = pyqtSignal()
    error_occurred = pyqtSignal(str)
    action_blocked = pyqtSignal(str)
    measurement_ready = pyqtSignal(dict)  # emitted after each measure() with last_datapoint

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

        self._timer = QTimer(self)
        self._timer.setInterval(tick_interval_ms)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_procedure(self, procedure: Any) -> None:
        """Start a procedure immediately if IDLE or during a manual ramp; else queue it."""
        manual_ramping = (
            self._state == OrchestratorState.RAMPING and self._procedure is None
        )
        if self._state != OrchestratorState.IDLE and not manual_ramping:
            self.queue_procedure(procedure)
            return

        # Cancel any manual ramp generators before starting the procedure.
        if manual_ramping:
            for vi_name, vi in self._station._virtual_instruments.items():
                if self._station._vi_registry.get(vi_name) == "system":
                    if hasattr(vi, "_ramp_gen"):
                        vi._ramp_gen = None
                        vi._ramp_exhausted = True
            logger.info("Manual ramp cancelled — procedure starting.")

        self._procedure = procedure
        self._standby_dispatched = False
        system_targets, meas_commands, wait_time = self._procedure.initiate()
        
        # Track active system VIs for stale monitoring
        self._active_system_vis = set(system_targets.keys())
        
        self._station.process_system_targets(system_targets)
        self._station.send_measurement_commands(meas_commands)
        self._current_wait_time = wait_time
        
        self._change_state(OrchestratorState.INITIATING)

    def queue_procedure(self, procedure: Any) -> None:
        """Add procedure to queue."""
        self._procedure_queue.append(procedure)

    def run_queue(self) -> None:
        """Run the next procedure in the queue if IDLE."""
        if self._state == OrchestratorState.IDLE and self._procedure_queue:
            proc = self._procedure_queue.pop(0)
            self.run_procedure(proc)

    def pause_procedure(self) -> None:
        """Pause current procedure."""
        if self._state in (OrchestratorState.INITIATING, OrchestratorState.RAMPING, 
                           OrchestratorState.MEASURING, OrchestratorState.SWEEPING, 
                           OrchestratorState.STANDBY):
            # We don't save previous state explicitly here, logic depends on IDLE resuming etc.
            # Wait, the spec says "transition back to pre-pause state". So we should store it.
            self._pre_pause_state = self._state
            self._change_state(OrchestratorState.PAUSED)

    def resume_procedure(self) -> None:
        """Resume procedure from PAUSED."""
        if self._state == OrchestratorState.PAUSED:
            self._change_state(self._pre_pause_state)

    def abort_procedure(self) -> None:
        """Abort without ramping to zero."""
        if self._procedure:
            # We assume procedure has a close or abort method?
            # Architecture doc says: "Close data file (partial data preserved)."
            # DataManager does this or procedure might do this. If procedure has an abort, call it.
            if hasattr(self._procedure, "abort"):
                self._procedure.abort()

        # Clear ramp generators on active VIs (set vi._ramp = None, but we don't directly access _ramp
        # We can call vi.advance_ramp? No, the directive says: "instruments hold at current position (no ramp-to-zero)."
        # But `station` has no clear_ramps. The architecture doc says "Clear ramp generators on active VIs (set vi._ramp = None)".
        # We'll try to reach inside if needed, or if start_ramp is sufficient.
        for vi_name in self._active_system_vis:
            try:
                vi = getattr(self._station, vi_name)
                # Hack to stop generators per the directive string
                if hasattr(vi, "_ramp_gen"):
                    vi._ramp_gen = None
            except Exception:
                pass
        
        self._procedure = None
        self._active_system_vis.clear()
        self._standby_dispatched = False
        self._change_state(OrchestratorState.IDLE)
        self.run_queue()

    def acknowledge_emergency(self) -> None:
        """Return to IDLE after an EMERGENCY is acknowledged."""
        if self._state == OrchestratorState.EMERGENCY:
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
        """Submit a global action like initiate_all or standby_all."""
        if action == "standby_all":
            if self._state not in (OrchestratorState.IDLE, OrchestratorState.ERROR, OrchestratorState.EMERGENCY):
                self.abort_procedure()
            self._station.standby_all()
        elif action == "initiate_all":
            self._station.initiate_all()

    # ------------------------------------------------------------------
    # Private / internal
    # ------------------------------------------------------------------

    def _change_state(self, new_state: OrchestratorState) -> None:
        logger.info("Orchestrator state: %s -> %s", self._state.name, new_state.name)
        self._state = new_state
        self.state_changed.emit(self._state.value)

    def _tick(self) -> None:
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

        # Safety check
        safety = self._station.check_safety()
        if safety.get("helium_low") and self._state != OrchestratorState.EMERGENCY:
            self._error("Emergency: Safety condition triggered (helium_low)")
            self._change_state(OrchestratorState.EMERGENCY)
            # EMERGENCY: call station.standby_all() is done in the state machine below 
            # but wait, let's just abort immediately
        
        # 2. Stale check during procedure
        if self._state not in (OrchestratorState.IDLE, OrchestratorState.PAUSED, 
                               OrchestratorState.ERROR, OrchestratorState.EMERGENCY):
            for vi_name in self._active_system_vis:
                vi_state = state.get(vi_name, {})
                if vi_state.get("_stale"):
                    self._error(f"Active VI '{vi_name}' became stale during procedure.")
                    self._change_state(OrchestratorState.ERROR)
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
                except Exception as e:
                    logger.error("Error executing GUI action on %s: %s", action["vi_name"], e)
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
                    import time
                    if not self._wait_started:
                        self._wait_started = True
                        self._wait_start_time = time.time()

                    if time.time() - self._wait_start_time >= self._current_wait_time:
                        self._wait_started = False
                        self._change_state(OrchestratorState.MEASURING)
        elif self._state == OrchestratorState.MEASURING:
            if self._procedure:
                self._procedure.measure()
                if hasattr(self._procedure, "get_progress"):
                    self.procedure_progress.emit(self._procedure.get_progress())
                dm = getattr(self._procedure, "_data_manager", None)
                if dm is not None and dm.last_datapoint:
                    self.measurement_ready.emit(dict(dm.last_datapoint))
            self._change_state(OrchestratorState.SWEEPING)
        elif self._state == OrchestratorState.SWEEPING:
            if self._procedure:
                sys_targ_wait = self._procedure.change_sweep_step()
                if sys_targ_wait is None:
                    # done, go to standby
                    self._change_state(OrchestratorState.STANDBY)
                else:
                    sys_targets, wait_time = sys_targ_wait
                    self._station.process_system_targets(sys_targets)
                    self._current_wait_time = wait_time
                    self._change_state(OrchestratorState.RAMPING)
        elif self._state == OrchestratorState.STANDBY:
            if not self._standby_dispatched:
                # Wait for whatever ramp was already in flight when SWEEPING
                # ended, then call standby() exactly once and dispatch
                # whatever targets it returns (e.g. ramp magnet to 0 T).
                if self._station.check_ramps():
                    if self._procedure and hasattr(self._procedure, "standby"):
                        sys_targ, meas_cmd, wait = self._procedure.standby()
                        self._station.process_system_targets(sys_targ)
                        self._station.send_measurement_commands(meas_cmd)
                    self._standby_dispatched = True
            else:
                # Wait for the ramp standby() itself just started (if any)
                # before declaring the procedure finished.
                if self._station.check_ramps():
                    self.procedure_finished.emit()
                    self._procedure = None
                    self._active_system_vis.clear()
                    self._standby_dispatched = False
                    self._change_state(OrchestratorState.IDLE)
                    self.run_queue()
        elif self._state == OrchestratorState.PAUSED:
            pass # Monitor continues, no ramp advancement
        elif self._state == OrchestratorState.ERROR:
            pass # Awaiting user interaction
        elif self._state == OrchestratorState.EMERGENCY:
            self._station.standby_all()
            self._error("Emergency shutdown executed.")
    
    def _error(self, message: str) -> None:
        logger.error(message)
        self.error_occurred.emit(message)
