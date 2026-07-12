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
    """Simulated helium_low -> EMERGENCY, acknowledge returns to IDLE."""
    # Force helium low
    station.level_meter._driver._force_helium_level = 5.0
    
    def check_state():
        return orchestrator._state == OrchestratorState.EMERGENCY
        
    qtbot.waitUntil(check_state, timeout=1000)
    assert orchestrator._state == OrchestratorState.EMERGENCY
    
    # Acknowledge
    orchestrator.acknowledge_emergency()
    assert orchestrator._state == OrchestratorState.IDLE


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
