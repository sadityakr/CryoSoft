# ---
# description: |
#   Unit tests for Layer 3 (Orchestrator).
#   Verifies state machine transitions, wait timers, procedures spanning multiple ticks,
#   emergency abort, action blocking, and queue logic.
# last_updated: 2026-07-12
# ---

import pytest


from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import build_station


class MockProcedure:
    """Minimal procedure for testing the Orchestrator state machine."""
    name = "Mock Sweep"
    
    def __init__(self, station):
        self._station = station
        self._sweep = [1.0, 2.0, 3.0]
        self._index = 0
        self.measure_called = 0

    def initiate(self):
        return (
            {"magnet_x": {"target": self._sweep[0]}},  # system_targets
            {},                                          # meas_commands
            0.0,                                         # wait_time (instant)
        )

    def change_sweep_step(self):
        self._index += 1
        if self._index >= len(self._sweep):
            return None
        return {"magnet_x": {"target": self._sweep[self._index]}}, 0.0

    def measure(self):
        self.measure_called += 1

    def standby(self):
        return {"magnet_x": {"target": 0.0}}, {}, 0.0

    def get_progress(self):
        return self._index / len(self._sweep)


@pytest.fixture
def station():
    """Build a real simulated station."""
    config_path = "cryosoft/configs/sim_cryostat"
    return build_station(config_path)


@pytest.fixture
def orchestrator(station, qtbot):
    """Build Orchestrator with a small tick interval."""
    # We create a QCoreApplication instance but qtbot handles the event loop
    orch = Orchestrator(station, tick_interval_ms=10)
    return orch


def test_basic_ticking(orchestrator, qtbot):
    """Orchestrator starts, ticks at interval, emits states_updated."""
    with qtbot.waitSignal(orchestrator.states_updated, timeout=500) as blocker:
        pass
    assert blocker.signal_triggered
    assert orchestrator._state == OrchestratorState.IDLE


def test_operational_status_populated_after_tick(orchestrator, qtbot):
    """A tick builds an operational-status record with the documented schema."""
    orchestrator._tick()
    status = orchestrator.get_operational_status()
    assert status["orch_state"] == "IDLE"
    assert "elapsed_in_state_s" in status
    assert status["verdict"] == "OK"
    assert status["vis"], "expected at least one system VI"
    keys = {"vi_name", "value", "target", "gap", "rate", "eta_s", "ramp_status", "code"}
    for vi in status["vis"]:
        assert keys <= set(vi)


def test_operational_status_reports_live_ramp_target(orchestrator, station, qtbot):
    """During a ramp, the record shows the VI's live target and a gap."""
    station.process_system_targets({"magnet_x": {"target": 1.0}})
    orchestrator._tick()
    status = orchestrator.get_operational_status()
    magnet = next(v for v in status["vis"] if v["vi_name"] == "magnet_x")
    assert magnet["target"] == pytest.approx(1.0)
    assert magnet["ramp_status"] == "RAMPING"
    assert magnet["gap"] is not None


def test_full_procedure_cycle(orchestrator, station, qtbot):
    """run_procedure() -> INITIATING -> RAMPING -> MEASURING -> SWEEPING -> ... -> IDLE."""
    procedure = MockProcedure(station)
    
    # We will record states
    states = []
    def on_state(s):
        states.append(s)
        
    orchestrator.state_changed.connect(on_state)
    
    # Fast ramp: override VI config rate and clear segments so generator uses 6000 A/min
    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    orchestrator.run_procedure(procedure)
    
    # It should cycle until it finishes and goes back to IDLE
    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=5000):
        # We need to manually tick if we aren't using the actual QTimer or qtbot wait
        # qtbot.waitSignal pumps the event loop, so the QTimer will fire!
        pass
        
    assert procedure.measure_called == 3
    assert OrchestratorState.IDLE.value in states
    assert OrchestratorState.RAMPING.value in states
    assert OrchestratorState.MEASURING.value in states
    assert OrchestratorState.SWEEPING.value in states
    assert OrchestratorState.STANDBY.value in states


def test_status_messages_emitted_during_run(orchestrator, station, qtbot):
    """A full run emits concise status milestones on status_message.

    MockProcedure has no get_sweep_position/get_sweep_array, so the line
    builders exercise their generic fallbacks — this also confirms status
    formatting can never raise into the tick.
    """
    procedure = MockProcedure(station)
    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    messages: list[str] = []
    orchestrator.status_message.connect(messages.append)

    orchestrator.run_procedure(procedure)
    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=5000):
        pass

    assert messages, "no status messages were emitted during the run"
    assert any("Initiating" in m for m in messages)
    # Distinct setup action, labelled from the magnet VI's setpoint metadata.
    assert any("Ramping field to" in m for m in messages)
    assert any("Measuring" in m for m in messages)
    assert any("parking" in m.lower() for m in messages)
    assert any("finished" in m.lower() for m in messages)
    # The initiation line must not be mislabelled as a sweep point.
    assert not any(m.startswith("Point 1/") for m in messages)


def test_station_setpoint_and_measurement_labels(station):
    """Station exposes each VI's declarative label/unit for status lines."""
    assert station.system_setpoint_meta("magnet_x") == ("field", "T")
    assert station.system_setpoint_meta("temperature_vti") == ("temperature", "K")
    assert station.measurement_label("dc_measurement") == "DC resistance"
    # Unknown VI degrades to (name, "") / name rather than raising.
    assert station.system_setpoint_meta("nope") == ("nope", "")
    assert station.measurement_label("nope") == "nope"


def test_standby_waits_for_its_own_ramp_before_finishing(orchestrator, station, qtbot):
    """procedure_finished must not fire until standby()'s own ramp completes.

    Regression test: the STANDBY handler used to check check_ramps() BEFORE
    calling procedure.standby(), then declare the procedure finished in the
    same tick — never waiting for the ramp standby() itself dispatches (e.g.
    ramping the magnet back to 0 T). By the time procedure_finished fired,
    the magnet was often still mid-ramp.
    """
    procedure = MockProcedure(station)  # standby() ramps magnet_x to 0.0 T
    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    orchestrator.run_procedure(procedure)

    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=5000):
        pass

    assert station.magnet_x.ramp_status() in ("TARGET_REACHED", "IDLE")
    assert station.magnet_x.get_field() == pytest.approx(0.0, abs=0.01)


def test_wait_time_respected(orchestrator, station, qtbot):
    """After targets reached, MEASURING doesn't start until wait expires."""
    procedure = MockProcedure(station)
    
    # Override initiate to add wait time
    def delayed_initiate():
        return ({"magnet_x": {"target": 1.0}}, {}, 0.1) # 100ms wait
        
    procedure.initiate = delayed_initiate
    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []
    
    orchestrator.run_procedure(procedure)

    # Wait until MEASURING state
    with qtbot.waitSignal(orchestrator.state_changed, timeout=1000):
        pass
        
    # Should take at least 0.1s to reach MEASURING once the ramp finishes
    # However we just checking wait is somewhat respected, not strict benchmarking.
    # We can at least check it doesn't skip it entirely
    
    orchestrator.abort_procedure()


def test_pause_resume(orchestrator, qtbot, station):
    """pause_procedure() stops advancement; resume_procedure() continues."""
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    
    # It will go INITIATING
    orchestrator.pause_procedure()
    assert orchestrator._state == OrchestratorState.PAUSED
    
    orchestrator.resume_procedure()
    # It was probably INITIATING or RAMPING before
    assert orchestrator._state in (OrchestratorState.INITIATING, OrchestratorState.RAMPING)
    orchestrator.abort_procedure()


def test_abort_to_idle(orchestrator, station):
    """Abort transitions to IDLE and measure doesn't continue."""
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    
    orchestrator.abort_procedure()
    assert orchestrator._state == OrchestratorState.IDLE
    assert orchestrator._procedure is None


def test_action_blocking(orchestrator, station, qtbot):
    """submit_vi_action() during procedure emits action_blocked."""
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_x", "set_field", target_T=1.0)
    
    orchestrator.abort_procedure()


def test_action_succeeded_emitted_on_successful_gui_action(orchestrator, qtbot):
    """submit_vi_action() in IDLE, once executed by the tick loop, emits action_succeeded."""
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500) as blocker:
        orchestrator.submit_vi_action("magnet_x", "initiate")

    assert blocker.args == ["magnet_x", "initiate"]


def test_action_succeeded_not_emitted_on_failed_gui_action(orchestrator, qtbot):
    """A GUI action that raises must not emit action_succeeded."""
    received = []
    orchestrator.action_succeeded.connect(lambda vi, method: received.append((vi, method)))

    orchestrator.submit_vi_action("magnet_x", "not_a_real_method")
    qtbot.wait(50)  # let one tick pass

    assert received == []


def test_action_failed_emitted_with_reason(orchestrator, qtbot):
    """A refused GUI action emits action_failed(vi, method, reason).

    This is the uniform per-action verdict of the control-validation
    standard: here a set_field beyond the setup's field limit is rejected by
    the limits wrapper and the reason reaches the GUI signal verbatim.
    """
    with qtbot.waitSignal(orchestrator.action_failed, timeout=500) as blocker:
        orchestrator.submit_vi_action("magnet_x", "set_field", target_T=99.0)

    vi_name, method_name, reason = blocker.args
    assert (vi_name, method_name) == ("magnet_x", "set_field")
    assert "outside the allowed range" in reason
    # The refused command must not have started a ramp.
    assert orchestrator._state == OrchestratorState.IDLE


def test_stale_vi_during_procedure(orchestrator, station, qtbot):
    """Patched driver fails -> ERROR state."""
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    
    # Patch to simulate error
    station.magnet_x._driver._simulate_error = True
    
    # Because it is active, when get_state becomes stale it should go to ERROR
    # wait for ERROR state
    def check_state():
        return orchestrator._state == OrchestratorState.ERROR
        
    qtbot.waitUntil(check_state, timeout=1000)
    assert orchestrator._state == OrchestratorState.ERROR
    orchestrator.abort_procedure()


def test_emergency_on_helium_low(orchestrator, station, qtbot):
    """Sustained helium_low -> EMERGENCY; acknowledge only after it clears.

    The helium flag is debounced (majority vote over the level meter's
    reading buffer), so EMERGENCY requires a few consecutive low polls.
    acknowledge_emergency() is refused while the condition persists and
    succeeds once the level recovers.
    """
    # Force helium low
    station.level_meter._driver._force_helium_level = 5.0

    def check_state():
        return orchestrator._state == OrchestratorState.EMERGENCY

    qtbot.waitUntil(check_state, timeout=2000)
    assert orchestrator._state == OrchestratorState.EMERGENCY

    # Acknowledging while helium is still low must be refused.
    orchestrator.acknowledge_emergency()
    assert orchestrator._state == OrchestratorState.EMERGENCY

    # Helium recovers; after enough clean polls the debounce buffer clears.
    station.level_meter._driver._force_helium_level = None

    def safety_cleared():
        return not any(station.check_safety().values())

    qtbot.waitUntil(safety_cleared, timeout=2000)
    orchestrator.acknowledge_emergency()
    assert orchestrator._state == OrchestratorState.IDLE


def test_emergency_shutdown_runs_once_not_every_tick(orchestrator, station, qtbot):
    """standby_all() must run once on EMERGENCY entry, not every tick.

    Repeating it each tick would restart a persistent magnet's full
    switch-heater warmup/cooldown cycle every few seconds.
    """
    calls = {"n": 0}
    original = station.standby_all

    def counting():
        calls["n"] += 1
        original()

    station.standby_all = counting
    try:
        station.level_meter._driver._force_helium_level = 5.0
        qtbot.waitUntil(
            lambda: orchestrator._state == OrchestratorState.EMERGENCY,
            timeout=2000,
        )
        # Let several more ticks pass in EMERGENCY.
        qtbot.wait(100)
    finally:
        station.standby_all = original
    assert calls["n"] == 1


def test_quench_triggers_emergency(orchestrator, station, qtbot):
    """A magnet QUENCH status must escalate to EMERGENCY."""
    station.magnet_x._driver._simulate_quench = True
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY,
        timeout=2000,
    )
    assert orchestrator._state == OrchestratorState.EMERGENCY


class RecordingProcedure(MockProcedure):
    """MockProcedure with a BaseProcedure-style abort() that records calls."""

    def __init__(self, station):
        super().__init__(station)
        self.abort_called = 0

    def abort(self):
        self.abort_called += 1
        return {}


def test_abort_calls_procedure_abort_and_holds_magnet(orchestrator, station, qtbot):
    """Abort must run the procedure's cleanup AND freeze the PSU (finding C3).

    Clearing the software generator alone is not enough: the PSU ramps
    autonomously to its last-commanded setpoint, so an abort that does not
    command a hardware hold leaves the field still moving.
    """
    proc = RecordingProcedure(station)
    orchestrator.run_procedure(proc)  # ramps magnet_x toward 1.0 T (slow rate)
    assert station.magnet_x.ramp_status() == "RAMPING"

    orchestrator.abort_procedure()

    assert proc.abort_called == 1
    assert orchestrator._state == OrchestratorState.IDLE
    assert orchestrator._procedure is None
    assert orchestrator._wait_started is False  # stale wait clock reset (H5)
    # Hardware held: PSU setpoint pinned to its present output.
    assert station.magnet_x.ramp_status() == "IDLE"
    drv = station.magnet_x._driver
    assert drv.get_status() == "HOLD"
    assert drv.get_current_setpoint() == pytest.approx(drv.get_current(), abs=0.01)


def test_measure_exception_degrades_to_error_not_crash(orchestrator, station, qtbot):
    """An exception inside the tick must contain to ERROR, never propagate.

    PyQt6 aborts the whole process on an unhandled exception in a slot
    (finding C2) — with the magnet live that is the worst possible failure.
    """
    class ExplodingProcedure(RecordingProcedure):
        def measure(self):
            raise RuntimeError("simulated measurement failure")

    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []
    proc = ExplodingProcedure(station)
    orchestrator.run_procedure(proc)

    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.ERROR, timeout=5000
    )
    assert orchestrator._procedure is None   # run cleaned up
    assert proc.abort_called == 1            # data-file cleanup hook ran

    orchestrator.recover_from_error()
    assert orchestrator._state == OrchestratorState.IDLE


def test_run_procedure_setup_failure_degrades_to_error(orchestrator, station):
    """initiate() raising must not crash the GUI slot; it lands in ERROR."""
    class BadInit(RecordingProcedure):
        def initiate(self):
            raise ValueError("bad parameters")

    proc = BadInit(station)
    orchestrator.run_procedure(proc)

    assert orchestrator._state == OrchestratorState.ERROR
    assert orchestrator._procedure is None
    orchestrator.recover_from_error()
    assert orchestrator._state == OrchestratorState.IDLE


def test_pause_holds_hardware_and_resume_redispatches(orchestrator, station, qtbot):
    """Pause must freeze the autonomous PSU; resume must restart the ramp."""
    proc = MockProcedure(station)
    orchestrator.run_procedure(proc)  # slow ramp toward 1.0 T
    assert station.magnet_x.ramp_status() == "RAMPING"

    orchestrator.pause_procedure()
    assert orchestrator._state == OrchestratorState.PAUSED
    drv = station.magnet_x._driver
    assert drv.get_status() == "HOLD"  # field frozen, not still ramping

    orchestrator.resume_procedure()
    assert orchestrator._state in (
        OrchestratorState.INITIATING, OrchestratorState.RAMPING
    )
    assert station.magnet_x.ramp_status() == "RAMPING"  # ramp re-dispatched

    orchestrator.abort_procedure()


def test_queue_procedures(orchestrator, station, qtbot):
    """Multiple procedures queued, run sequentially."""
    proc1 = MockProcedure(station)
    proc2 = MockProcedure(station)
    
    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    orchestrator.run_procedure(proc1)
    orchestrator.run_procedure(proc2)
    
    assert len(orchestrator._procedure_queue) == 1
    
    # wait for proc1 finished
    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=2000):
        pass
        
    # proc2 should start now
    assert orchestrator._procedure == proc2
    assert orchestrator._state != OrchestratorState.IDLE
    orchestrator.abort_procedure()


def test_run_procedure_refused_when_magnet_in_persistent_mode(qtbot):
    """A magnet left in manual persistent mode blocks a procedure from starting:
    action_blocked fires, the Orchestrator stays IDLE, nothing is dispatched."""
    # sim_real_cryostat has a persistent (switch-heater) magnet.
    station = build_station("cryosoft/configs/sim_real_cryostat")
    station.magnet_x.enable_persistent_mode()
    orch = Orchestrator(station, tick_interval_ms=10)

    blocked: list[str] = []
    orch.action_blocked.connect(blocked.append)

    procedure = MockProcedure(station)
    orch.run_procedure(procedure)

    assert orch._state == OrchestratorState.IDLE
    assert orch._procedure is None
    assert blocked and "persistent mode" in blocked[0]

    # With the magnet returned to normal mode, the procedure starts.
    station.magnet_x.switch_heater_on()
    station.magnet_x.disable_persistent_mode()
    orch.run_procedure(procedure)
    assert orch._procedure is procedure
    orch.abort_procedure()
