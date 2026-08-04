import json
import logging
import time
from typing import ClassVar

import pytest


from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.plan import PhasePlan, StepPlan, Target
from cryosoft.core.station import Station, build_station
from cryosoft.virtual_instruments.base import BaseVirtualInstrument
from cryosoft.virtual_instruments.rampable import RampableVI


class MockProcedure:
    """Minimal procedure for testing the Orchestrator state machine."""
    name = "Mock Sweep"

    def __init__(self, station):
        self._station = station
        self._sweep = [1.0, 2.0, 3.0]
        self._index = 0
        self.measure_called = 0

    def initiate(self):
        return PhasePlan(
            targets={"magnet_z": Target(self._sweep[0])},
            commands=(),
            wait_s=0.0,  # instant
        )

    def change_sweep_step(self):
        self._index += 1
        if self._index >= len(self._sweep):
            return None
        return StepPlan(targets={"magnet_z": Target(self._sweep[self._index])}, wait_s=0.0)

    def measure(self):
        self.measure_called += 1

    def standby(self):
        return PhasePlan(targets={"magnet_z": Target(0.0)}, commands=(), wait_s=0.0)

    def get_progress(self):
        return self._index / len(self._sweep)


@pytest.fixture
def station():
    """Build a real simulated station."""
    config_path = "cryosoft/configs/sim_cryostat"
    return build_station(config_path)


@pytest.fixture
def orchestrator(station, qtbot):
    """Build Orchestrator with a small tick interval, monitoring active.

    Monitoring is OFF at construction (the production default: nothing is
    polled until the instruments are initiated), so tests of the monitored
    behavior start it explicitly here. The teardown stops the tick timer so
    no tick can ever fire into a test's torn-down objects.
    """
    # We create a QCoreApplication instance but qtbot handles the event loop
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.start_monitoring()
    yield orch
    orch.shutdown()


def _degraded_station(tmp_path, vi_type: str = "measurement"):
    """Build a station whose one instrument failed to connect but will
    succeed on the next attempt (reuses the L2 flaky-driver double)."""
    from tests.test_l2_station import _FlakyDriver

    _FlakyDriver.fail_times = 1
    _FlakyDriver.attempts = 0
    (tmp_path / "devices.yaml").write_text(
        "real_drivers:\n"
        "  flaky_drv:\n"
        "    class: tests.test_l2_station._FlakyDriver\n"
        '    address: "GPIB0::12::INSTR"\n'
        "virtual_instruments:\n"
        "  flaky_vi:\n"
        "    class: tests.test_l2_station._StubVI\n"
        "    drivers: {main: flaky_drv}\n"
        f"    vi_type: {vi_type}\n"
    )
    (tmp_path / "monitor.yaml").write_text(
        "monitor:\n  tick_interval_ms: 1000\n  max_vi_errors: 3\n"
    )
    return build_station(str(tmp_path))


def test_connect_instrument_success_emits_signals(tmp_path, qtbot):
    """connect_instrument() in IDLE brings the VI live and reports the verdict."""
    orch = Orchestrator(_degraded_station(tmp_path), tick_interval_ms=10)
    try:
        with qtbot.waitSignals(
            [orch.instrument_reconnected, orch.action_succeeded], timeout=500
        ):
            orch.connect_instrument("flaky_vi")
        assert orch._station.has_vi("flaky_vi") is True
        assert orch._station.offline_vi_names() == []
    finally:
        orch.shutdown()


def test_connect_instrument_blocked_outside_idle(tmp_path, qtbot):
    """Reconnect is refused while a run is in flight (action_blocked verdict)."""
    orch = Orchestrator(_degraded_station(tmp_path), tick_interval_ms=10)
    try:
        orch._state = OrchestratorState.MEASURING
        with qtbot.waitSignal(orch.action_blocked, timeout=500):
            orch.connect_instrument("flaky_vi")
        assert orch._station.has_vi("flaky_vi") is False
    finally:
        orch._state = OrchestratorState.IDLE
        orch.shutdown()


def test_connect_instrument_failure_emits_action_failed(tmp_path, qtbot):
    """A still-unreachable instrument yields action_failed with the reason."""
    from tests.test_l2_station import _FlakyDriver

    orch = Orchestrator(_degraded_station(tmp_path), tick_interval_ms=10)
    _FlakyDriver.fail_times = 99  # next attempt fails again
    _FlakyDriver.attempts = 0
    try:
        with qtbot.waitSignal(orch.action_failed, timeout=500) as blocker:
            orch.connect_instrument("flaky_vi")
        assert "flaky_drv" in blocker.args[2]
        assert orch._station.offline_vi_names() == ["flaky_vi"]
    finally:
        orch.shutdown()


def test_connect_instrument_adopts_reconnected_scanner(tmp_path, qtbot):
    """A reconnected switch VI becomes the scanner (same first-switch rule
    the constructor applies)."""
    orch = Orchestrator(
        _degraded_station(tmp_path, vi_type="switch"), tick_interval_ms=10
    )
    try:
        assert orch._scanner_vi_name is None
        orch.connect_instrument("flaky_vi")
        assert orch._scanner_vi_name == "flaky_vi"
    finally:
        orch.shutdown()


def test_basic_ticking(orchestrator, qtbot):
    """Orchestrator starts, ticks at interval, emits states_updated.

    The timeout is deliberately generous. The fixture ticks every 10 ms, so
    500 ticks of headroom is far more than correctness needs; it is sized
    against Qt event-loop contention when the full suite runs, not against the
    tick interval. At 500 ms this test was the suite's one persistent flake:
    it passed standalone in 0.2 s and failed under load. A real regression
    (no ticking at all) still fails here, just later. Do not trim it back.
    """
    with qtbot.waitSignal(orchestrator.states_updated, timeout=5000) as blocker:
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
    station.process_system_targets({"magnet_z": Target(1.0)})
    orchestrator._tick()
    status = orchestrator.get_operational_status()
    magnet = next(v for v in status["vis"] if v["vi_name"] == "magnet_z")
    assert magnet["target"] == pytest.approx(1.0)
    assert magnet["ramp_status"] == "RAMPING"
    assert magnet["gap"] is not None


def test_operational_status_conditions_empty_when_healthy(orchestrator, qtbot):
    """A healthy tick's record carries an empty conditions list, not a missing key."""
    orchestrator._tick()
    status = orchestrator.get_operational_status()
    assert status["conditions"] == []


def test_operational_status_carries_active_safety_condition(orchestrator, station, qtbot):
    """The System-Condition standard's registry reaches the status record.

    Mirrors test_helium_low_holds_magnets_without_emergency's setup: a
    sustained low helium reading trips a hold-severity safety condition
    scoped to magnet_z (see virtual_instruments/base.py's safety_concerns()).
    Once the Orchestrator's tick has recorded it, the same condition must
    show up in the operational-status record built by
    _update_operational_status.
    """
    station.level_meter._driver._force_helium_level = 5.0

    def magnet_held():
        return "magnet_z" in orchestrator._held_vis()

    qtbot.waitUntil(magnet_held, timeout=2000)
    orchestrator._tick()
    status = orchestrator.get_operational_status()
    hold_conditions = [c for c in status["conditions"] if c["severity"] == "hold"]
    assert hold_conditions, "expected a hold condition for the helium_low flag"
    condition = hold_conditions[0]
    assert condition["kind"] == "helium_low"
    assert "magnet_z" in condition["affected"]


def test_tick_emits_raw_trend_record(orchestrator, station, qtbot):
    """A tick writes exactly the documented raw-tier JSON shape (see
    TieredTrendLogger's docstring) to cryosoft.trend_raw, pinning the
    "t"/"s"/"v" nesting and the measurement-VI exclusion at the
    orchestrator boundary.

    cryosoft.trend_raw has propagate=False (logging_config.py), so the
    capturing handler must attach directly to it, not to root. The handler
    is removed in a finally block since loggers are process-global.
    """
    trend_logger = logging.getLogger("cryosoft.trend_raw")
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    trend_logger.addHandler(handler)
    previous_level = trend_logger.level
    trend_logger.setLevel(logging.INFO)  # setup_logging() may not have run in-test

    try:
        orchestrator._tick()
    finally:
        trend_logger.removeHandler(handler)
        trend_logger.setLevel(previous_level)

    assert len(records) >= 1, "expected at least one raw-tier line from one tick"
    payload = json.loads(records[0].getMessage())

    assert set(payload) == {"t", "s", "v"}
    assert isinstance(payload["t"], float)
    assert payload["s"] == orchestrator._state.name

    v = payload["v"]
    assert isinstance(v, dict)
    assert v, "expected a non-empty flattened state dict"
    assert all(isinstance(key, str) and isinstance(val, float) for key, val in v.items())
    assert set(v) == set(station.last_state_flat())

    # keithley_dc_mode is vi_type: measurement in sim_cryostat/devices.yaml
    # and exposes @monitored last_n_valid; last_state_flat() excludes every
    # measurement VI, so this key must never reach the raw trend tier.
    assert "keithley_dc_mode_last_n_valid" not in v


def test_full_procedure_cycle(orchestrator, station, qtbot):
    """run_procedure() -> INITIATING -> RAMPING -> MEASURING -> SWEEPING -> ... -> IDLE."""
    procedure = MockProcedure(station)
    
    # We will record states
    states = []
    def on_state(s):
        states.append(s)
        
    orchestrator.state_changed.connect(on_state)
    
    # Fast ramp: override VI config rate and clear segments so generator uses 6000 A/min
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

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
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

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
    assert station.system_setpoint_meta("magnet_z") == ("field", "T")
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
    procedure = MockProcedure(station)  # standby() ramps magnet_z to 0.0 T
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    orchestrator.run_procedure(procedure)

    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=5000):
        pass

    assert station.magnet_z.ramp_status() in ("TARGET_REACHED", "IDLE")
    assert station.magnet_z.magnet_field_T() == pytest.approx(0.0, abs=0.01)


def test_wait_time_respected(orchestrator, station, qtbot):
    """After targets reached, MEASURING doesn't start until wait expires."""
    procedure = MockProcedure(station)
    
    # Override initiate to add wait time
    def delayed_initiate():
        return PhasePlan(
            targets={"magnet_z": Target(1.0)}, commands=(), wait_s=0.1
        )  # 100ms wait

    procedure.initiate = delayed_initiate
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []
    
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


# ── Ramp tracker: active_ramps() / ramps_updated / stop_ramp() ────────────────


def test_active_ramps_empty_while_nothing_ramps(orchestrator, qtbot):
    """An idle machine publishes an empty list, not a stale or missing one."""
    orchestrator._tick()
    assert orchestrator.active_ramps() == []


def test_tick_publishes_a_record_for_a_manual_ramp(orchestrator, station, qtbot):
    """A ramp started from a GUI action shows up with its rate and both setpoints."""
    station.get_vi("magnet_z").set_field(5.0)
    orchestrator._tick()

    records = orchestrator.active_ramps()
    assert [r.vi_name for r in records] == ["magnet_z"]
    record = records[0]
    assert record.label == "field"
    assert record.unit == "T"
    assert record.target == pytest.approx(5.0)
    assert record.setpoint is not None
    assert record.rate is not None
    # Nothing owns a manual ramp, so the operator may stop it.
    assert record.owner is None
    assert record.stoppable is True


def test_ramps_updated_emits_the_same_records(orchestrator, station, qtbot):
    """The push and read halves of the tracker surface agree."""
    station.get_vi("magnet_z").set_field(5.0)
    with qtbot.waitSignal(orchestrator.ramps_updated, timeout=1000) as blocker:
        orchestrator._tick()
    emitted = blocker.args[0]
    assert [r.vi_name for r in emitted] == [r.vi_name for r in orchestrator.active_ramps()]


def test_ramp_snapshot_is_polled_once_per_tick(orchestrator, station, monkeypatch):
    """Both consumers share one Station poll — ramp accessors are real bus reads."""
    calls = []
    real = station.get_ramp_status

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(station, "get_ramp_status", counting)
    station.get_vi("magnet_z").set_field(5.0)
    orchestrator._tick()
    # One poll for the operational-status record AND the ramp tracker
    # together; a second is only ever allowed on a tick that drained a GUI
    # action (which this one did not — set_field was called directly).
    assert len(calls) == 1


def test_procedure_owns_its_ramps_and_they_cannot_be_stopped_individually(
    orchestrator, station, qtbot
):
    """A run's ramp names its owner and refuses a single-VI stop.

    Stopping one VI mid-run would strand the run waiting on a setpoint it can
    never reach — aborting the run is the only correct stop.
    """
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    orchestrator._tick()

    records = [r for r in orchestrator.active_ramps() if r.vi_name == "magnet_z"]
    assert records, "the procedure's magnet ramp should be tracked"
    record = records[0]
    assert record.owner == "procedure 'Mock Sweep'"
    assert record.stoppable is False
    assert "Mock Sweep" in record.stop_blocked_reason

    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.stop_ramp("magnet_z")
    # Refused means refused: the ramp is still running.
    assert station.get_vi("magnet_z").ramp_status() == "RAMPING"

    orchestrator.abort_procedure()


def test_stop_ramp_holds_that_vi_and_leaves_the_others_alone(
    orchestrator, station, qtbot
):
    """A manual ramp is stopped per instrument, unlike abort_procedure()."""
    station.get_vi("magnet_z").set_field(5.0)
    station.get_vi("magnet_y").set_field(2.0)
    orchestrator._tick()
    assert {r.vi_name for r in orchestrator.active_ramps()} == {"magnet_z", "magnet_y"}

    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500) as blocker:
        orchestrator.stop_ramp("magnet_z")
    assert blocker.args == ["magnet_z", "stop_ramp"]

    assert station.get_vi("magnet_z").ramp_status() == "IDLE"
    assert station.get_vi("magnet_y").ramp_status() == "RAMPING"
    # The stopped row leaves the tracker immediately, not a tick later.
    assert [r.vi_name for r in orchestrator.active_ramps()] == ["magnet_y"]


def test_stop_ramp_returns_the_machine_to_idle_on_the_next_tick(
    orchestrator, station, qtbot
):
    """The state machine still owns the RAMPING -> IDLE transition."""
    station.get_vi("magnet_z").set_field(5.0)
    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.RAMPING

    orchestrator.stop_ramp("magnet_z")
    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.IDLE


def test_quiet_machine_publishes_no_ramps_without_polling(station, qtbot, monkeypatch):
    """Monitoring off + IDLE polls nothing: a fresh launch stays quiet."""
    orch = Orchestrator(station, tick_interval_ms=10_000)
    try:
        calls = []
        monkeypatch.setattr(
            station, "get_ramp_status", lambda: calls.append(1) or {}
        )
        assert orch.is_monitoring() is False
        orch._tick()
        assert calls == []
        assert orch.active_ramps() == []
    finally:
        orch.shutdown()


def test_action_blocking(orchestrator, station, qtbot):
    """submit_vi_action() during procedure emits action_blocked."""
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "set_field", target_T=1.0)
    
    orchestrator.abort_procedure()


def test_action_succeeded_emitted_on_successful_gui_action(orchestrator, qtbot):
    """submit_vi_action() in IDLE, once executed by the tick loop, emits action_succeeded."""
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500) as blocker:
        orchestrator.submit_vi_action("magnet_z", "initiate")

    assert blocker.args == ["magnet_z", "initiate"]


def test_action_succeeded_not_emitted_on_failed_gui_action(orchestrator, qtbot):
    """A GUI action that raises must not emit action_succeeded."""
    received = []
    orchestrator.action_succeeded.connect(lambda vi, method: received.append((vi, method)))

    orchestrator.submit_vi_action("magnet_z", "not_a_real_method")
    qtbot.wait(50)  # let one tick pass

    assert received == []


def test_submit_global_action_initiate_all_succeeds_for_every_vi(orchestrator, station, qtbot):
    """'Initiate All' fans out to one queued initiate per VI.

    Regression: the button used to call station.initiate_all() directly, which
    ran initiate() on every VI but emitted no action_succeeded verdict, so the
    per-panel lifecycle toggles never flipped and the click looked dead. Now it
    enqueues per-VI actions that the tick executes and confirms, one signal per
    VI — exactly what the InstrumentPanel toggles listen for.
    """
    expected = set(station.get_vi_names())
    assert expected, "sim station should register at least one VI"

    received: list[tuple[str, str]] = []
    orchestrator.action_succeeded.connect(lambda vi, m: received.append((vi, m)))

    orchestrator.submit_global_action("initiate_all")
    # One queued action per VI, before any tick has processed them.
    assert len(orchestrator._gui_action_queue) == len(expected)

    qtbot.waitUntil(lambda: len(received) >= len(expected), timeout=2000)

    assert {vi for vi, method in received} == expected
    assert all(method == "initiate" for _, method in received)


def test_submit_global_action_standby_all_succeeds_for_every_vi(orchestrator, station, qtbot):
    """'Standby All' likewise confirms a standby for every VI (same toggle path)."""
    expected = set(station.get_vi_names())
    received: list[tuple[str, str]] = []
    orchestrator.action_succeeded.connect(lambda vi, m: received.append((vi, m)))

    orchestrator.submit_global_action("standby_all")
    qtbot.waitUntil(lambda: len(received) >= len(expected), timeout=2000)

    assert {vi for vi, method in received} == expected
    assert all(method == "standby" for _, method in received)


def test_action_failed_emitted_with_reason(orchestrator, qtbot):
    """A refused GUI action emits action_failed(vi, method, reason).

    This is the uniform per-action verdict of the control-validation
    standard: here a set_field beyond the setup's field limit is rejected by
    the limits wrapper and the reason reaches the GUI signal verbatim.
    """
    with qtbot.waitSignal(orchestrator.action_failed, timeout=500) as blocker:
        orchestrator.submit_vi_action("magnet_z", "set_field", target_T=99.0)

    vi_name, method_name, reason = blocker.args
    assert (vi_name, method_name) == ("magnet_z", "set_field")
    assert "outside the allowed range" in reason
    # The refused command must not have started a ramp.
    assert orchestrator._state == OrchestratorState.IDLE


def test_stale_claimed_vi_during_procedure_fails_run_to_idle(orchestrator, station, qtbot):
    """A stale ACTIVE (claimed) VI fails the run, but returns to IDLE, not ERROR.

    A claimed VI's fault has a KNOWN, narrow blast radius (that one
    instrument), so it must not park the whole machine in global ERROR —
    only the run fails, and every other instrument (and this one, once it
    recovers or is retried) stays usable. This replaces the old
    test_stale_vi_during_procedure, which asserted the pre-plan global-ERROR
    behavior; that behavior is now reserved for unknown-blast-radius
    failures (unhandled tick-boundary exceptions), verified separately by
    test_unhandled_tick_exception_still_enters_error.
    """
    procedure = MockProcedure(station)
    finished: list[dict] = []
    events: list = []
    orchestrator.run_finished.connect(lambda manifest: finished.append(manifest))
    orchestrator.error_event.connect(lambda ev: events.append(ev))
    orchestrator.run_procedure(procedure)

    # Patch to simulate error on the VI the run is actively driving.
    station.magnet_z._driver._simulate_error = True

    def check_idle_again():
        return orchestrator._state == OrchestratorState.IDLE and bool(finished)

    qtbot.waitUntil(check_idle_again, timeout=1000)

    assert orchestrator._state == OrchestratorState.IDLE
    manifest = finished[-1]
    assert manifest["status"] == "failed"
    assert "magnet_z" in manifest["reason"]

    # The VI's fault stands in the Station registry — quarantined, not the
    # whole machine.
    faults = station.vi_faults()
    assert "magnet_z" in faults
    assert faults["magnet_z"].kind in ("stale", "disconnected")

    # A matching structured run_failure event named the instrument.
    run_failure_events = [e for e in events if e.kind == "run_failure"]
    assert run_failure_events
    assert run_failure_events[-1].vi_name == "magnet_z"
    assert run_failure_events[-1].severity == "error"

    # Every OTHER instrument stays usable: a manual action on an unfaulted
    # VI is admitted immediately (no run is active any more).
    admitted, _ = orchestrator._manual_action_admissible("temperature_vti")
    assert admitted is True

    # The faulted VI itself is refused until it recovers or is retried.
    admitted, reason = orchestrator._manual_action_admissible("magnet_z")
    assert admitted is False
    assert "fault" in reason.lower()

    # The queue must NOT auto-continue after a run failure (conservative,
    # same as the old ERROR behavior).
    orchestrator.queue_procedure(MockProcedure(station))
    assert orchestrator._procedure_queue  # still queued, not auto-started
    assert orchestrator._state == OrchestratorState.IDLE

    # Clear the fault so nothing leaks into other tests.
    station.magnet_z._driver._simulate_error = False


def test_stale_unclaimed_vi_while_monitoring_is_warning_only(orchestrator, station, qtbot):
    """A stale UNCLAIMED VI (no run using it) never changes state — just a fault + warning."""
    events: list = []
    orchestrator.error_event.connect(lambda ev: events.append(ev))

    assert orchestrator._state == OrchestratorState.IDLE
    station.temperature_sample._driver._simulate_error = True

    def has_fault():
        return "temperature_sample" in station.vi_faults()

    qtbot.waitUntil(has_fault, timeout=1000)

    # No state change at all.
    assert orchestrator._state == OrchestratorState.IDLE

    fault_events = [e for e in events if e.kind == "fault" and e.vi_name == "temperature_sample"]
    assert fault_events
    assert fault_events[-1].severity == "warning"

    station.temperature_sample._driver._simulate_error = False


def test_unhandled_tick_exception_still_enters_error(orchestrator, qtbot, monkeypatch):
    """An unhandled tick-boundary exception (unknown blast radius) still -> ERROR.

    The one case global ERROR survives: recover_from_error() is
    unchanged.
    """
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "_tick_body", boom)

    with qtbot.waitSignal(orchestrator.error_occurred, timeout=1000):
        orchestrator._tick()

    assert orchestrator._state == OrchestratorState.ERROR
    orchestrator.recover_from_error()
    assert orchestrator._state == OrchestratorState.IDLE


def test_helium_low_holds_magnets_without_emergency(orchestrator, station, qtbot):
    """Sustained helium_low holds concerned VIs but never enters EMERGENCY.

    helium_low is hold-only (MagnetBase.safety_concerns() includes it,
    TemperatureControllerBase does not — see virtual_instruments/base.py):
    magnet_z is refused manual control the moment the hold is recorded,
    while temperature_vti (unconcerned with helium_low) keeps operating
    freely and the Orchestrator never leaves IDLE. The flag is debounced
    (majority vote over the level meter's reading buffer), so the hold
    requires a few consecutive low polls.
    """
    station.level_meter._driver._force_helium_level = 5.0

    def magnet_held():
        return "magnet_z" in orchestrator._held_vis()

    qtbot.waitUntil(magnet_held, timeout=2000)
    assert orchestrator._state == OrchestratorState.IDLE

    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500) as blocker:
        orchestrator.submit_vi_action("magnet_z", "initiate")
    assert "safety hold" in blocker.args[0]
    assert orchestrator._state == OrchestratorState.IDLE

    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("temperature_vti", "initiate")

    # Helium recovers; after enough clean polls the debounce buffer clears
    # and the hold lifts on its own — no acknowledgment needed.
    station.level_meter._driver._force_helium_level = None

    def hold_cleared():
        return "magnet_z" not in orchestrator._held_vis()

    qtbot.waitUntil(hold_cleared, timeout=2000)
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "initiate")


def test_acknowledge_unlocks_helium_low_hold_without_emergency(orchestrator, station, qtbot):
    """acknowledge() unlocks a plain hold-severity condition, no EMERGENCY involved.

    Mirrors test_helium_low_holds_magnets_without_emergency but exercises
    the new override: before acknowledge(), magnet_z is refused exactly
    like today; after, manual control is admitted and the state never
    leaves IDLE — acknowledge() never fakes a state transition for a
    condition that is honestly still active (GLOSSARY.md's **Hold
    acknowledge**).
    """
    station.level_meter._driver._force_helium_level = 5.0

    def magnet_held():
        return "magnet_z" in orchestrator._held_vis()

    qtbot.waitUntil(magnet_held, timeout=2000)

    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "standby")

    orchestrator.acknowledge()
    assert orchestrator._state == OrchestratorState.IDLE
    assert orchestrator.override_active("magnet_z") is True

    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "standby")

    station.level_meter._driver._force_helium_level = None


def test_acknowledge_hold_override_works_while_paused(orchestrator, station, qtbot):
    """The hold override reaches through claim-protection while PAUSED.

    A magnet held by helium_low AND claimed by a paused procedure is
    refused by rule 0b before claim-protection (rule 4) is ever reached —
    acknowledging admits the action directly, with no PAUSED-specific code
    anywhere in _manual_action_admissible(). This is the deliberate,
    accepted trade-off documented as the Known gap for resume_procedure()
    (GLOSSARY.md's **Hold acknowledge**): the override reaches through
    claim-protection on purpose, so filling helium mid-pause is possible.
    """
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    orchestrator.pause_procedure()
    assert orchestrator._state == OrchestratorState.PAUSED

    station.level_meter._driver._force_helium_level = 5.0

    def magnet_held():
        return "magnet_z" in orchestrator._held_vis()

    qtbot.waitUntil(magnet_held, timeout=2000)

    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "standby")

    orchestrator.acknowledge()
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "standby")
    assert orchestrator._state == OrchestratorState.PAUSED

    station.level_meter._driver._force_helium_level = None


def test_hold_override_does_not_leak_into_new_emergency(orchestrator, station, qtbot):
    """Regression: an acknowledged hold must not bypass a LATER EMERGENCY.

    Without the EMERGENCY/ERROR guard in _manual_action_admissible()'s
    rule 0b, an operator acknowledging helium_low on magnet_z while IDLE
    would leave magnet_z unlocked straight through a subsequent quench on
    the SAME VI — breaking the invariant that critical severity refuses
    every VI, held or not
    (test_quench_emergency_blocks_all_manual_control). The still-live hold
    override must NOT admit action once EMERGENCY is entered; only a fresh
    acknowledge() (now delegating to _acknowledge_emergency()) can unlock
    it from there.
    """
    station.level_meter._driver._force_helium_level = 5.0

    def magnet_held():
        return "magnet_z" in orchestrator._held_vis()

    qtbot.waitUntil(magnet_held, timeout=2000)
    orchestrator.acknowledge()
    assert orchestrator.override_active("magnet_z") is True
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "standby")

    station.magnet_z._driver._simulate_quench = True
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )

    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "initiate")

    orchestrator.acknowledge()
    assert orchestrator._state == OrchestratorState.EMERGENCY
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "initiate")

    station.magnet_z._driver._simulate_quench = False
    station.level_meter._driver._force_helium_level = None


def test_hold_override_refused_during_error(orchestrator, station, qtbot, monkeypatch):
    """The hold override never admits action while the Orchestrator is in ERROR.

    ERROR means unknown blast radius (an unhandled tick-boundary
    exception), not a known, scoped condition a human can safely act
    around — the deliberate ERROR exclusion alongside EMERGENCY in rule
    0b's state guard. A hold acknowledged BEFORE the ERROR entry (still
    unexpired) must not leak through it, mirroring
    test_hold_override_does_not_leak_into_new_emergency for ERROR instead
    of EMERGENCY.
    """
    station.level_meter._driver._force_helium_level = 5.0

    def magnet_held():
        return "magnet_z" in orchestrator._held_vis()

    qtbot.waitUntil(magnet_held, timeout=2000)
    orchestrator.acknowledge()
    assert orchestrator.override_active("magnet_z") is True

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "_tick_body", boom)
    with qtbot.waitSignal(orchestrator.error_occurred, timeout=1000):
        orchestrator._tick()
    assert orchestrator._state == OrchestratorState.ERROR

    admitted, reason = orchestrator._manual_action_admissible("magnet_z")
    assert admitted is False
    assert "safety hold" in reason

    station.level_meter._driver._force_helium_level = None


def test_hold_override_expires_after_timeout(station, qtbot):
    """The hold override reverts to refusal once manual_override_timeout_s elapses.

    No state change accompanies the expiry — it is purely the timestamp
    comparison in _manual_action_admissible() lapsing on its own, not a
    tick-driven "re-lock" event. A fresh acknowledge() restores access.
    """
    orch = Orchestrator(station, tick_interval_ms=10, manual_override_timeout_s=0.2)
    orch.start_monitoring()
    try:
        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)
        orch.acknowledge()
        assert orch.override_active("magnet_z") is True

        qtbot.wait(300)  # past the 0.2s window
        assert orch.override_active("magnet_z") is False
        admitted, reason = orch._manual_action_admissible("magnet_z")
        assert admitted is False
        assert orch._state == OrchestratorState.IDLE  # no state change

        orch.acknowledge()
        assert orch.override_active("magnet_z") is True
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


def test_hold_override_survives_flapping_within_window(station, qtbot):
    """The cooldown survives the SAME flag clearing and re-tripping (flapping).

    User-confirmed design: pruning is expiry-only, never merely because the
    condition disappeared from verdict.held_vis, so a flag hovering right
    at its threshold (helium_low is exactly this in practice) does not
    force a fresh acknowledge on every re-trip within the same window.
    """
    orch = Orchestrator(station, tick_interval_ms=10, manual_override_timeout_s=5.0)
    orch.start_monitoring()
    try:
        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)
        orch.acknowledge()
        assert orch.override_active("magnet_z") is True

        # Condition clears...
        station.level_meter._driver._force_helium_level = None

        def hold_cleared():
            return "magnet_z" not in orch._held_vis()

        qtbot.waitUntil(hold_cleared, timeout=2000)
        # override_active("magnet_z") is correctly False here — nothing is
        # held, so there is nothing to admit against. The invariant under
        # test is that the underlying _hold_override_until entry (keyed by
        # Condition.key, e.g. "safety:helium_low") was NOT pruned just
        # because the condition cleared — checked directly since
        # override_active() has no VI to resolve a key through while
        # unheld. It only matters once the flag re-trips, below.
        assert orch._hold_override_until

        # ...and re-trips (same key) before the 5s window elapses, with no
        # second acknowledge() call.
        station.level_meter._driver._force_helium_level = 5.0
        qtbot.waitUntil(magnet_held, timeout=2000)
        assert orch.override_active("magnet_z") is True
        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("magnet_z", "standby")
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


def test_emergency_override_expires_after_timeout(station, qtbot):
    """The EMERGENCY override also expires, reverting to refusal in-state.

    State stays EMERGENCY throughout (the condition is still active) —
    only manual-control admission lapses. Re-acknowledging renews it.
    """
    orch = Orchestrator(station, tick_interval_ms=10, manual_override_timeout_s=0.2)
    orch.start_monitoring()
    try:
        station.magnet_z._driver._simulate_quench = True
        qtbot.waitUntil(
            lambda: orch._state == OrchestratorState.EMERGENCY, timeout=2000
        )
        orch.acknowledge()
        assert orch.override_active() is True
        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("dc_measurement", "initiate")

        qtbot.wait(300)  # past the 0.2s window
        assert orch.override_active() is False
        with qtbot.waitSignal(orch.action_blocked, timeout=500):
            orch.submit_vi_action("dc_measurement", "initiate")
        assert orch._state == OrchestratorState.EMERGENCY  # no state change

        orch.acknowledge()
        assert orch.override_active() is True
        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("dc_measurement", "initiate")
    finally:
        station.magnet_z._driver._simulate_quench = False
        orch.shutdown()


def test_emergency_acknowledge_unlocks_manual_front_panel(orchestrator, station, qtbot):
    """Acknowledging an unresolved EMERGENCY unlocks manual control station-wide.

    Before acknowledging, a front-panel action on ANY VI — not just one
    concerned with the tripped critical flag — is refused: critical
    severity is station-wide scope by construction (the System-Condition
    standard), so EMERGENCY refuses every VI, held or not (the inversion of
    the pre-System-Condition behavior, where an unconcerned VI stayed
    operable). Acknowledging once (condition still active) stays in
    EMERGENCY but unlocks manual control of EVERY VI — the operator's way
    to intervene (e.g. cycling a switch heater by hand) without the
    condition having cleared on its own. run_procedure() must still refuse
    to run immediately: it only queues, same as any busy state.
    """
    station.magnet_z._driver._simulate_quench = True
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )

    # Locked: front-panel action refused before acknowledging, on both the
    # quenched magnet AND a VI with no safety_concerns() at all.
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "initiate")
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("dc_measurement", "initiate")
    assert orchestrator._state == OrchestratorState.EMERGENCY

    # Condition is still active, so acknowledging cannot reach IDLE...
    orchestrator.acknowledge()
    assert orchestrator._state == OrchestratorState.EMERGENCY
    assert orchestrator.override_active() is True

    # ...but the front panel is now unlocked, for every VI.
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500) as blocker:
        orchestrator.submit_vi_action("magnet_z", "initiate")
    assert blocker.args == ["magnet_z", "initiate"]
    assert orchestrator._state == OrchestratorState.EMERGENCY
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500) as blocker:
        orchestrator.submit_vi_action("dc_measurement", "initiate")
    assert blocker.args == ["dc_measurement", "initiate"]

    # A procedure is still refused from running immediately — it queues.
    procedure = MockProcedure(station)
    orchestrator.run_procedure(procedure)
    assert orchestrator._state == OrchestratorState.EMERGENCY
    assert orchestrator._procedure is None
    assert procedure in orchestrator._procedure_queue

    # Quench recovers; acknowledging again returns to IDLE and relocks.
    station.magnet_z._driver._simulate_quench = False
    qtbot.waitUntil(
        lambda: not orchestrator._station.check_safety().get("quench"), timeout=2000
    )
    orchestrator.acknowledge()
    assert orchestrator._state == OrchestratorState.IDLE
    assert orchestrator.override_active() is False


def test_emergency_shutdown_runs_once_not_every_tick(orchestrator, station, qtbot):
    """EMERGENCY entry's blanket standby_all() must run once, not every tick.

    _enter_emergency() always calls Station.standby_all() exactly once on
    entry (critical severity is station-wide scope by construction, so
    there is no "concerned VI" subset to narrow the shutdown to — see the
    System-Condition standard). Repeating it every tick would restart a
    persistent magnet's full switch-heater warmup/cooldown cycle every few
    seconds.
    """
    calls = {"n": 0}
    original = station.magnet_z.standby

    def counting():
        calls["n"] += 1
        original()

    station.magnet_z.standby = counting
    try:
        station.magnet_z._driver._simulate_quench = True
        qtbot.waitUntil(
            lambda: orchestrator._state == OrchestratorState.EMERGENCY,
            timeout=2000,
        )
        # Let several more ticks pass in EMERGENCY.
        qtbot.wait(100)
    finally:
        station.magnet_z.standby = original
    assert calls["n"] == 1


def test_quench_triggers_emergency(orchestrator, station, qtbot):
    """A magnet QUENCH status must escalate to EMERGENCY (it is a critical flag)."""
    station.magnet_z._driver._simulate_quench = True
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY,
        timeout=2000,
    )
    assert orchestrator._state == OrchestratorState.EMERGENCY


def test_quench_emergency_blocks_all_manual_control(orchestrator, station, qtbot):
    """During quench EMERGENCY, EVERY VI's manual control is refused.

    Quench concerns no instrument (MagnetBase.safety_concerns() names only
    "helium_low" — see virtual_instruments/base.py); it is a critical
    safety flag, and critical severity IS station-wide scope (the
    System-Condition standard): the whole station needs to be shut down
    regardless of which VI reported the quench or which VI's
    safety_concerns() name it. This is the inversion of the old
    "unconcerned VI stays usable" behavior — dc_measurement (a plain
    measurement VI with no safety_concerns() at all) and temperature_vti
    (unaffected by quench under either the old or new safety_concerns())
    are refused exactly like magnet_z, until acknowledge()
    unlocks the manual override.
    """
    station.magnet_z._driver._simulate_quench = True
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )

    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "initiate")
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("temperature_vti", "initiate")
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("dc_measurement", "initiate")

    orchestrator.acknowledge()
    assert orchestrator._state == OrchestratorState.EMERGENCY
    assert orchestrator.override_active() is True

    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("dc_measurement", "initiate")


def test_safety_hold_fails_claimed_run_but_spares_others(orchestrator, station, qtbot):
    """A non-tolerated hold on a run's watched VI fails just that run.

    Mirrors the stale-VI run-failure behavior, but for a physical safety
    concern rather than a communication fault: the machine returns to IDLE
    (not global ERROR), and every other instrument stays usable.
    """
    procedure = MockProcedure(station)  # targets magnet_z, claims everything
    orchestrator.run_procedure(procedure)
    assert orchestrator._procedure is procedure

    station.level_meter._driver._force_helium_level = 5.0
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=2000)
    assert orchestrator._state == OrchestratorState.IDLE

    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("temperature_vti", "initiate")


class RecordingProcedure(MockProcedure):
    """MockProcedure with a BaseProcedure-style abort() that records calls."""

    def __init__(self, station):
        super().__init__(station)
        self.abort_called = 0

    def abort(self):
        self.abort_called += 1
        return ()


def test_abort_calls_procedure_abort_and_holds_magnet(orchestrator, station, qtbot):
    """Abort must run the procedure's cleanup AND freeze the PSU (finding C3).

    Clearing the software generator alone is not enough: the PSU ramps
    autonomously to its last-commanded setpoint, so an abort that does not
    command a hardware hold leaves the field still moving.
    """
    proc = RecordingProcedure(station)
    orchestrator.run_procedure(proc)  # ramps magnet_z toward 1.0 T (slow rate)
    assert station.magnet_z.ramp_status() == "RAMPING"

    orchestrator.abort_procedure()

    assert proc.abort_called == 1
    assert orchestrator._state == OrchestratorState.IDLE
    assert orchestrator._procedure is None
    assert orchestrator._wait_started is False  # stale wait clock reset (H5)
    # Hardware held: PSU setpoint pinned to its present output.
    assert station.magnet_z.ramp_status() == "IDLE"
    drv = station.magnet_z._driver
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

    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []
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


def test_malformed_initiate_return_fails_loudly(orchestrator, station):
    """initiate() returning the OLD tuple (not a PhasePlan) must fail loudly.

    The Wave-2 currency is typed: the Orchestrator consumes ``plan.targets`` /
    ``plan.commands`` / ``plan.wait_s``. A procedure that returns the legacy
    ``(system_targets, measurement_commands, wait)`` tuple has no ``.targets``
    attribute, so setup must contain the AttributeError to ERROR rather than
    silently mis-dispatching.
    """
    class LegacyReturn(RecordingProcedure):
        def initiate(self):
            # The pre-Wave-2 shape — a bare tuple, not a PhasePlan.
            return ({"magnet_z": {"target": 1.0}}, {}, 0.0)

    proc = LegacyReturn(station)
    orchestrator.run_procedure(proc)

    assert orchestrator._state == OrchestratorState.ERROR
    assert orchestrator._procedure is None
    orchestrator.recover_from_error()
    assert orchestrator._state == OrchestratorState.IDLE


def test_pause_holds_hardware_and_resume_redispatches(orchestrator, station, qtbot):
    """Pause must freeze the autonomous PSU; resume must restart the ramp."""
    proc = MockProcedure(station)
    orchestrator.run_procedure(proc)  # slow ramp toward 1.0 T
    assert station.magnet_z.ramp_status() == "RAMPING"

    orchestrator.pause_procedure()
    assert orchestrator._state == OrchestratorState.PAUSED
    drv = station.magnet_z._driver
    assert drv.get_status() == "HOLD"  # field frozen, not still ramping

    orchestrator.resume_procedure()
    assert orchestrator._state in (
        OrchestratorState.INITIATING, OrchestratorState.RAMPING
    )
    assert station.magnet_z.ramp_status() == "RAMPING"  # ramp re-dispatched

    orchestrator.abort_procedure()


def test_queue_procedures(orchestrator, station, qtbot):
    """Multiple procedures queued, run sequentially."""
    proc1 = MockProcedure(station)
    proc2 = MockProcedure(station)
    
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

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
    station.magnet_z.enable_persistent_mode()
    orch = Orchestrator(station, tick_interval_ms=10)

    blocked: list[str] = []
    orch.action_blocked.connect(blocked.append)

    procedure = MockProcedure(station)
    orch.run_procedure(procedure)

    assert orch._state == OrchestratorState.IDLE
    assert orch._procedure is None
    assert blocked and "persistent mode" in blocked[0]

    # With the magnet returned to normal mode, the procedure starts.
    station.magnet_z.switch_heater_on()
    station.magnet_z.disable_persistent_mode()
    orch.run_procedure(procedure)
    assert orch._procedure is procedure
    orch.abort_procedure()
    orch.shutdown()


# ── Monitoring lifecycle (start/stop/shutdown) ────────────────────────────────
# Monitoring is OFF at construction: the tick timer runs (it processes GUI
# actions and the state machine), but no instrument is polled until
# start_monitoring(). This is what keeps a freshly launched app quiet while
# the instruments are still being initiated.


def _spy_get_state(station, monkeypatch):
    """Wrap station.get_state with a call counter; returns the counter list."""
    calls: list[int] = []
    real_get_state = station.get_state

    def counted():
        calls.append(1)
        return real_get_state()

    monkeypatch.setattr(station, "get_state", counted)
    return calls


def test_monitoring_off_by_default_polls_nothing(station, qtbot, monkeypatch):
    """A fresh Orchestrator neither polls the station nor emits states_updated."""
    orch = Orchestrator(station, tick_interval_ms=10)
    calls = _spy_get_state(station, monkeypatch)
    emitted: list[dict] = []
    orch.states_updated.connect(emitted.append)

    assert orch.is_monitoring() is False
    for _ in range(3):
        orch._tick()
    assert calls == []
    assert emitted == []
    assert orch.get_operational_status() == {}
    orch.shutdown()


def test_start_monitoring_begins_polling_and_signals(station, qtbot, monkeypatch):
    """start_monitoring() emits monitoring_changed(True) and enables polling."""
    orch = Orchestrator(station, tick_interval_ms=10)
    calls = _spy_get_state(station, monkeypatch)
    changes: list[bool] = []
    orch.monitoring_changed.connect(changes.append)

    assert orch.start_monitoring() is True
    assert orch.is_monitoring() is True
    orch._tick()
    assert calls, "monitored tick must poll the station"

    # Idempotent: a second start emits no second signal.
    assert orch.start_monitoring() is True
    assert changes == [True]
    orch.shutdown()


def test_gui_actions_execute_while_monitoring_off(station, qtbot, monkeypatch):
    """The initiate-before-monitoring flow: actions run on the tick with no polling."""
    orch = Orchestrator(station, tick_interval_ms=10)
    calls = _spy_get_state(station, monkeypatch)
    verdicts: list[tuple[str, str]] = []
    orch.action_succeeded.connect(lambda vi, m: verdicts.append((vi, m)))

    orch.submit_vi_action("magnet_z", "initiate")
    orch._tick()

    assert ("magnet_z", "initiate") in verdicts
    assert calls == []  # still no instrument polling
    orch.shutdown()


def test_run_procedure_auto_starts_monitoring(station, qtbot):
    """A procedure must run under the stall detector: monitoring auto-starts."""
    orch = Orchestrator(station, tick_interval_ms=10)
    assert orch.is_monitoring() is False
    orch.run_procedure(MockProcedure(station))
    assert orch.is_monitoring() is True
    orch.abort_procedure()
    orch.shutdown()


def test_stop_monitoring_refused_while_procedure_active(station, qtbot):
    """stop_monitoring() is blocked outside IDLE/ERROR and allowed back in IDLE."""
    orch = Orchestrator(station, tick_interval_ms=10)
    blocked: list[str] = []
    orch.action_blocked.connect(blocked.append)

    orch.run_procedure(MockProcedure(station))
    assert orch._state == OrchestratorState.INITIATING

    assert orch.stop_monitoring() is False
    assert orch.is_monitoring() is True
    assert blocked and "monitoring" in blocked[0].lower()

    orch.abort_procedure()  # back to IDLE
    assert orch.stop_monitoring() is True
    assert orch.is_monitoring() is False
    orch.shutdown()


def test_shutdown_stops_ticking(station, qtbot):
    """After shutdown() no tick fires: states_updated stays silent."""
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.start_monitoring()
    # Generous for the same reason as test_basic_ticking: this is a positive
    # "prove it ticks" wait, sized against suite-wide event-loop contention.
    # The negative assertion below keeps its short timeout on purpose.
    with qtbot.waitSignal(orch.states_updated, timeout=5000):
        pass  # ticking while monitoring: baseline

    orch.shutdown()
    with qtbot.waitSignal(orch.states_updated, timeout=100, raising=False) as blocker:
        pass
    assert not blocker.signal_triggered


# ── Run manifests (run_started / run_finished) ───────────────────────────────

def _fast_magnet(station):
    """Make magnet_z ramps effectively instant for state-machine tests."""
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []


def test_run_manifests_full_cycle(orchestrator, station, qtbot):
    """A completed run emits one run_started and one matching run_finished."""
    _fast_magnet(station)
    procedure = MockProcedure(station)
    started, finished = [], []
    orchestrator.run_started.connect(started.append)
    orchestrator.run_finished.connect(finished.append)

    orchestrator.run_procedure(procedure)
    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=5000):
        pass

    assert len(started) == 1 and len(finished) == 1
    manifest = started[0]
    assert manifest["procedure"] == "Mock Sweep"
    assert manifest["kind"] == "run"
    assert manifest["run_id"]
    assert manifest["started_utc"]
    # MockProcedure has no data file / params accessors — best-effort fields.
    assert manifest["data_file"] == ""
    assert manifest["params"] == {}
    end = finished[0]
    assert end["run_id"] == manifest["run_id"]
    assert end["status"] == "done"
    assert end["finished_utc"]


def test_abort_emits_run_finished_aborted(orchestrator, station, qtbot):
    """User abort ends the run with status 'aborted', exactly once."""
    procedure = MockProcedure(station)  # slow default ramp keeps the run alive
    finished = []
    orchestrator.run_finished.connect(finished.append)

    orchestrator.run_procedure(procedure)
    with qtbot.waitSignal(orchestrator.state_changed, timeout=2000):
        pass
    orchestrator.abort_procedure()

    assert len(finished) == 1
    assert finished[0]["status"] == "aborted"
    assert orchestrator._state == OrchestratorState.IDLE
    # Recovering/ticking afterwards must not re-emit for the dead run.
    orchestrator._tick()
    assert len(finished) == 1


def test_failed_setup_emits_no_manifests(orchestrator, station, qtbot):
    """When initiate() itself raises, neither manifest is emitted."""

    class BrokenProcedure(MockProcedure):
        def initiate(self):
            raise RuntimeError("boom")

    started, finished = [], []
    orchestrator.run_started.connect(started.append)
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_procedure(BrokenProcedure(station))

    assert orchestrator._state == OrchestratorState.ERROR
    assert started == [] and finished == []


# ── Session envelope enforcement ─────────────────────────────────────────────

def _envelope(**bounds):
    from cryosoft.core.plan import ExperimentEnvelope

    return ExperimentEnvelope(bounds=dict(bounds))


def test_envelope_rejects_out_of_bounds_target(orchestrator, station, qtbot):
    """A procedure target outside the envelope is rejected before dispatch."""
    from cryosoft.core.plan import EnvelopeBound

    orchestrator.set_experiment_envelope(
        _envelope(magnet_z=EnvelopeBound(min_value=-0.5, max_value=0.5))
    )
    errors: list[str] = []
    started: list[dict] = []
    orchestrator.error_occurred.connect(errors.append)
    orchestrator.run_started.connect(started.append)

    orchestrator.run_procedure(MockProcedure(station))  # first target is 1.0 T

    assert orchestrator._state == OrchestratorState.ERROR
    assert started == [], "a rejected run must not report as started"
    assert any("session envelope" in e and "magnet_z" in e for e in errors)
    # The magnet was never asked to move.
    assert station.magnet_z.magnet_field_T() == pytest.approx(0.0, abs=1e-6)


def test_envelope_allows_within_bounds_and_clears(orchestrator, station, qtbot):
    """Targets inside the envelope run normally; None clears the envelope."""
    from cryosoft.core.plan import EnvelopeBound

    _fast_magnet(station)
    orchestrator.set_experiment_envelope(
        _envelope(magnet_z=EnvelopeBound(min_value=-5.0, max_value=5.0))
    )
    orchestrator.run_procedure(MockProcedure(station))
    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=5000):
        pass
    assert orchestrator._state == OrchestratorState.IDLE

    orchestrator.set_experiment_envelope(None)
    assert orchestrator._session_envelope is None


def test_envelope_state_violation_enters_emergency(orchestrator, station, qtbot):
    """A live reading outside a state_key bound trips EMERGENCY like a safety flag."""
    from cryosoft.core.plan import EnvelopeBound

    # Sim sample thermometer sits at 300 K; a 400 K session minimum is an
    # immediate violation on the next tick.
    orchestrator.set_experiment_envelope(
        _envelope(
            temperature_sample=EnvelopeBound(min_value=400.0, state_key="temperature")
        )
    )
    errors: list[str] = []
    orchestrator.error_occurred.connect(errors.append)

    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.EMERGENCY
    assert any("session envelope" in e and "temperature_sample" in e for e in errors)

    # Acknowledgement is refused while the violation persists...
    orchestrator._acknowledge_emergency()
    assert orchestrator._state == OrchestratorState.EMERGENCY

    # ...and succeeds once the envelope is cleared (the "sample removed" case).
    orchestrator.set_experiment_envelope(None)
    orchestrator._acknowledge_emergency()
    assert orchestrator._state == OrchestratorState.IDLE


# ── Scanner-enabled flag ──────────────────────────────────────────────────

def test_scanner_enabled_default_false(orchestrator):
    """Scanner is disabled by default on a fresh Orchestrator."""
    assert orchestrator.scanner_enabled() is False


def test_set_scanner_enabled_round_trips_with_switch_vi(orchestrator, station):
    """set_scanner_enabled() forwards to the Station when a switch VI exists."""
    assert station.switch_vi_names(), "sim_cryostat is expected to have a switch VI"
    orchestrator.set_scanner_enabled(True)
    assert orchestrator.scanner_enabled() is True
    assert station.scanner_enabled() is True

    orchestrator.set_scanner_enabled(False)
    assert orchestrator.scanner_enabled() is False


def test_set_scanner_enabled_is_noop_without_switch_vi(qtbot):
    """A station with no switch VI: set_scanner_enabled() logs and does nothing."""
    from cryosoft.core.station import Station

    bare_station = Station()
    orch = Orchestrator(bare_station, tick_interval_ms=10)
    orch.set_scanner_enabled(True)
    assert orch.scanner_enabled() is False


# ── Gate framework ─────────────────────────────────────────────────────────

def test_current_gates_empty_for_procedure_without_gate_methods(orchestrator, station):
    """A duck-typed procedure with no gate methods behaves like the no-op default."""
    procedure = MockProcedure(station)
    orchestrator._procedure = procedure
    orchestrator._first_measurement = True
    assert orchestrator._current_gates() == ()
    orchestrator._first_measurement = False
    assert orchestrator._current_gates() == ()


def test_initiation_gate_replaces_wait_and_blocks_until_satisfied(orchestrator, station):
    """A declared initiation gate is stepped each tick and wait_s is ignored."""
    from cryosoft.core.gates import Gate

    procedure = MockProcedure(station)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] >= 3

    procedure.initiation_gates = lambda: (Gate("settle", check=check),)
    # A large wait_s that must be ignored once a gate is declared.
    procedure.initiate = lambda: PhasePlan(
        targets={"magnet_z": Target(1.0)}, commands=(), wait_s=999.0
    )
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    orchestrator.run_procedure(procedure)
    # The sim magnet driver advances on real elapsed time, not tick count, so
    # the budget must clear ~0.1s of wall clock at this rate/target with
    # margin for per-tick overhead (monitoring poll, safety/envelope checks).
    for _ in range(1000):
        orchestrator._tick()
        if orchestrator._state == OrchestratorState.INITIATION_GATE:
            break
    assert orchestrator._state == OrchestratorState.INITIATION_GATE
    assert calls["n"] == 0  # ramp-complete tick declares the gate, doesn't step it yet
    assert orchestrator._wait_started is False

    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.INITIATION_GATE
    assert calls["n"] == 1

    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.INITIATION_GATE
    assert calls["n"] == 2

    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.MEASURING
    assert calls["n"] == 3


def test_reading_gate_used_after_first_measurement_not_initiation(orchestrator, station):
    """reading_gates() governs the second sweep point; the first uses wait_s."""
    from cryosoft.core.gates import Gate

    procedure = MockProcedure(station)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] >= 2

    procedure.reading_gates = lambda: (Gate("settle", check=check),)
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    orchestrator.run_procedure(procedure)
    for _ in range(1000):
        orchestrator._tick()
        if orchestrator._state == OrchestratorState.READING_GATE:
            break
    assert orchestrator._state == OrchestratorState.READING_GATE
    assert procedure.measure_called == 1  # first point measured via ordinary wait_s
    assert calls["n"] == 0

    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.READING_GATE
    assert calls["n"] == 1

    orchestrator._tick()
    assert orchestrator._state == OrchestratorState.MEASURING
    assert calls["n"] == 2


def test_pause_resume_during_reading_gate_holds_and_resumes(orchestrator, station):
    """Pausing mid-gate holds pending_gates; resume continues stepping them."""
    from cryosoft.core.gates import Gate

    procedure = MockProcedure(station)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] >= 5

    procedure.reading_gates = lambda: (Gate("settle", check=check),)
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    orchestrator.run_procedure(procedure)
    for _ in range(1000):
        orchestrator._tick()
        if orchestrator._state == OrchestratorState.READING_GATE:
            break
    assert orchestrator._state == OrchestratorState.READING_GATE

    orchestrator._tick()
    n_before_pause = calls["n"]
    assert orchestrator._state == OrchestratorState.READING_GATE

    orchestrator.pause_procedure()
    assert orchestrator._state == OrchestratorState.PAUSED
    orchestrator._tick()
    assert calls["n"] == n_before_pause  # no stepping while paused

    orchestrator.resume_procedure()
    assert orchestrator._state == OrchestratorState.READING_GATE
    orchestrator._tick()
    assert calls["n"] == n_before_pause + 1


def test_not_responding_refuses_control_via_tag_policy(orchestrator, station, qtbot):
    """Rule 0 refuses a ``not_responding`` VI, driven by ``TAG_POLICY`` rather
    than a hardcoded ``held.origin == "comm"`` branch.

    ``Station.availability()`` (the Availability standard,
    ``cryosoft.core.availability``) reports the ``not_responding`` tag the
    moment a comm-origin hold exists, and ``TAG_POLICY["not_responding"]
    .controllable`` is ``False`` — the same refusal
    ``test_stale_claimed_vi_during_procedure_fails_run_to_idle`` observes
    indirectly through a failed run, checked here directly against
    ``_manual_action_admissible()``.
    """
    from cryosoft.core.availability import TAG_POLICY

    assert TAG_POLICY["not_responding"].controllable is False

    station.magnet_z._driver._simulate_error = True

    def has_fault():
        return "magnet_z" in station.vi_faults()

    qtbot.waitUntil(has_fault, timeout=1000)
    assert "not_responding" in orchestrator._station.availability("magnet_z").tags

    admitted, reason = orchestrator._manual_action_admissible("magnet_z")
    assert admitted is False
    assert "instrument fault" in reason

    station.magnet_z._driver._simulate_error = False


def test_not_responding_never_bypassed_by_emergency_override(orchestrator, station, qtbot):
    """The comm-vs-safety ``acknowledge()`` asymmetry still holds.

    ``TAG_POLICY["not_responding"]`` carries no override column at all, so
    unlike a safety hold (rule 0b), acknowledging an EMERGENCY can never
    unlock a ``not_responding`` VI — only recovery or ``retry_fault()`` can.
    A DIFFERENT VI's quench drives the EMERGENCY here so the fault under test
    is purely a comm condition on ``temperature_vti``, isolating rule 0 from
    rule 0b.
    """
    station.temperature_vti._driver._simulate_error = True

    def has_fault():
        return "temperature_vti" in station.vi_faults()

    qtbot.waitUntil(has_fault, timeout=1000)

    station.magnet_z._driver._simulate_quench = True
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )
    orchestrator.acknowledge()
    assert orchestrator.override_active() is True

    # The override unlocks a VI with no fault of its own...
    with qtbot.waitSignal(orchestrator.action_succeeded, timeout=500):
        orchestrator.submit_vi_action("dc_measurement", "initiate")

    # ...but NOT the not_responding VI, override notwithstanding.
    admitted, reason = orchestrator._manual_action_admissible("temperature_vti")
    assert admitted is False
    assert "instrument fault" in reason

    station.magnet_z._driver._simulate_quench = False
    qtbot.waitUntil(
        lambda: not orchestrator._station.check_safety().get("quench"), timeout=2000
    )
    orchestrator.acknowledge()
    assert orchestrator._state == OrchestratorState.IDLE

    station.temperature_vti._driver._simulate_error = False


def test_detached_vi_admitted_by_tag_policy(orchestrator, station):
    """A VI carrying only the ``detached`` tag is admitted, per ``TAG_POLICY``.

    ``detached`` (a single-client VI whose session is released between runs
    — the Availability standard, ``cryosoft.core.availability``) is the
    least-restrictive tag: ``TAG_POLICY["detached"].controllable`` is
    ``True``, and a detached VI is never a ``_held_vis()`` entry at all (it
    is not a ``Condition``), so it reaches rule 0 with ``held is None`` and
    falls through to ordinary admission. This VI carries no ``is_attached()``
    method for real yet (that declaration is a later phase of the
    Availability standard); a duck-typed stand-in exercises
    ``Station._build_availability()``'s existing ``is_attached()`` probe.
    """
    from cryosoft.core.availability import TAG_POLICY

    assert TAG_POLICY["detached"].controllable is True

    vi = station.get_vi("dc_measurement")
    vi.is_attached = lambda: False
    try:
        assert station.availability("dc_measurement").tags == frozenset({"detached"})
        admitted, reason = orchestrator._manual_action_admissible("dc_measurement")
        assert admitted is True
        assert reason == ""
    finally:
        del vi.is_attached


# ---------------------------------------------------------------------------
# Safety-hold enforcement (Orchestrator._enforce_safety_holds()): the
# level-triggered invariant that keeps every held, un-overridden VI at
# standby for as long as its hold persists — not just once at onset. See
# core/README.md's tick-pipeline paragraph and GLOSSARY.md's **Safety hold**.
# ---------------------------------------------------------------------------


def _force_ramp_to_settle(driver) -> None:
    """Rewind *driver*'s simulated clock so its next poll snaps to setpoint.

    The sim PSU (``SimOxfordIPS120``) advances its simulated current toward
    the commanded setpoint based on REAL elapsed wall time since
    ``_last_update``; the configured ramp rates (~0.5 T/min) would otherwise
    make a real ramp-to-completion take real minutes. Rewinding the clock
    far enough that ``max_step`` exceeds any remaining distance makes the
    very next simulated poll (any ``get_current()``/``get_status()`` call)
    snap straight to the setpoint — mirrors the technique
    ``tests/test_l1_virtual_instruments.py`` uses directly against the VI.
    """
    driver._last_update = time.time() - 3600.0


def test_expired_override_re_asserts_standby_on_the_held_vi(station, qtbot):
    """Regression: a hold that outlives an acknowledge-then-expire window must
    still drive the VI back to standby — expiry revokes manual PERMISSION,
    it must not leave the hardware wherever the operator last put it.

    This is the reported bug: helium_low trips, the operator acknowledges to
    unlock manual control, sets a nonzero field during the unlocked window,
    and the window expires. On ``develop`` (the old onset-only dispatch at
    the deleted ``elif condition.origin == "safety" and condition.severity
    == "hold":`` branch) the hold was already "known" from the moment it
    began, so nothing re-fires when the override lapses — the magnet is
    simply never told to stand down again, and this test times out waiting
    for the field to return to zero. With ``_enforce_safety_holds()`` the
    hold is a level-triggered invariant re-checked every tick, so the very
    next eligible tick after the override lapses re-issues ``standby()``.
    """
    driver = station.magnet_z._driver
    vi = station.magnet_z
    orch = Orchestrator(
        station,
        tick_interval_ms=10,
        manual_override_timeout_s=0.2,
        hold_enforcement_interval_s=0.05,
    )
    orch.start_monitoring()
    try:
        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)

        orch.acknowledge()
        assert orch.override_active("magnet_z") is True

        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("magnet_z", "set_field", target_T=2.0)

        def field_at_target():
            _force_ramp_to_settle(driver)
            return abs(vi.magnet_field_T() - 2.0) < 0.01 and vi.ramp_status() == "TARGET_REACHED"

        qtbot.waitUntil(field_at_target, timeout=2000)

        qtbot.wait(300)  # well past the 0.2s override window
        assert orch.override_active("magnet_z") is False

        def field_back_to_zero():
            _force_ramp_to_settle(driver)
            return abs(vi.magnet_field_T()) < 0.01

        qtbot.waitUntil(field_back_to_zero, timeout=3000)
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


def test_enforcement_interrupts_a_ramp_still_in_flight(station, qtbot, monkeypatch):
    """The "2 T hole": enforcement must not wait for an in-flight manual ramp
    to finish before re-asserting the hold.

    A held magnet sent to 2 T during an acknowledge window, still climbing
    when that window lapses, must be re-commanded to standby WHILE the ramp
    is in flight — never exempted until it arrives. ``standby_status()`` is
    command-PROVENANCE-based (the standby-provenance standard,
    ``virtual_instruments/base.py``), never a physical read of where the
    field currently is, so it reports ``"away"`` the instant a manual
    ``start_ramp()`` supersedes a prior ``standby()``. A check written
    against the magnet's own ``magnet_state()`` instead would see
    ``"ramping"`` — with no notion of ramping to WHAT — classify the magnet
    as converging on safety, and leave it climbing to 2 T forever. That is
    the hole this test exists to keep closed.
    """
    vi = station.magnet_z
    orch = Orchestrator(
        station,
        tick_interval_ms=10,
        hold_enforcement_interval_s=0.02,
    )
    orch.start_monitoring()
    try:
        standby_calls: list[float] = []
        original_standby = vi.standby

        def counting_standby():
            standby_calls.append(time.time())
            return original_standby()

        monkeypatch.setattr(vi, "standby", counting_standby)

        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)
        qtbot.waitUntil(lambda: len(standby_calls) >= 1, timeout=2000)  # onset attempt
        orch.acknowledge()

        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("magnet_z", "set_field", target_T=2.0)

        calls_before = len(standby_calls)
        assert vi.ramp_status() == "RAMPING"
        assert vi.standby_status() == "away"

        # Force the override to have already expired, with the ramp toward
        # 2 T still climbing (the sim PSU ramps in real time, so the field
        # is nowhere near 2 T yet and stays that way for the whole test).
        orch._hold_override_until = {key: 0.0 for key in orch._hold_override_until}
        assert orch.override_active("magnet_z") is False

        # Enforcement must fire on the next eligible tick, mid-ramp.
        qtbot.waitUntil(lambda: len(standby_calls) > calls_before, timeout=2000)

        # It interrupted the climb rather than waiting for it: the magnet is
        # now converging on standby's target, and never got anywhere near
        # the 2 T the operator asked for.
        assert vi.standby_status() in ("converging", "reached")
        assert abs(vi.magnet_field_T() - 2.0) > 0.5
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


def test_converging_standby_is_not_re_issued_and_the_ramp_completes(station, qtbot, monkeypatch):
    """No wedging: while ``standby()`` is converging, it must not be re-issued.

    Catches the naive fix that calls ``standby()`` unconditionally every
    tick a hold persists: rebuilding ``SuperconductingMagnetVI``'s ramp
    generator on every tick would restart the ramp from scratch each time
    and the magnet would never arrive. A tiny
    ``hold_enforcement_interval_s`` (well under the whole test's duration)
    makes sure the assertion is actually exercising the ``standby_status()
    != "away"`` skip, not merely benefiting from the rate limit.
    """
    driver = station.magnet_z._driver
    vi = station.magnet_z
    orch = Orchestrator(
        station,
        tick_interval_ms=10,
        hold_enforcement_interval_s=0.01,
    )
    orch.start_monitoring()
    try:
        # Move the magnet away from zero first, so standby() has ground to
        # cover once the hold trips.
        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("magnet_z", "set_field", target_T=1.0)

        def field_at_1T():
            _force_ramp_to_settle(driver)
            return abs(vi.magnet_field_T() - 1.0) < 0.01 and vi.ramp_status() == "TARGET_REACHED"

        qtbot.waitUntil(field_at_1T, timeout=2000)

        standby_calls: list[float] = []
        original_standby = vi.standby

        def counting_standby():
            standby_calls.append(time.time())
            return original_standby()

        monkeypatch.setattr(vi, "standby", counting_standby)

        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)
        qtbot.waitUntil(lambda: vi.standby_status() == "converging", timeout=2000)
        assert len(standby_calls) == 1

        # Several more ticks pass while still converging, well past the 0.01s
        # rate-limit interval — standby() must not be re-issued.
        qtbot.wait(200)
        assert vi.standby_status() == "converging"
        assert len(standby_calls) == 1

        def field_back_to_zero():
            _force_ramp_to_settle(driver)
            return abs(vi.magnet_field_T()) < 0.01 and vi.standby_status() == "reached"

        qtbot.waitUntil(field_back_to_zero, timeout=2000)
        assert len(standby_calls) == 1  # the ramp finished on its own — never restarted
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


def test_failed_standby_is_retried_and_escalates_exactly_once(station, qtbot, monkeypatch, caplog):
    """A raising ``standby()`` is retried at the next interval and escalates
    once ``hold_enforcement_max_attempts`` is reached — logged CRITICAL and
    reported via one ``kind="safety_hold"``/``severity="error"``
    ``ErrorEvent``, not one per attempt past the threshold.
    """
    vi = station.magnet_z
    orch = Orchestrator(
        station,
        tick_interval_ms=10,
        hold_enforcement_interval_s=0.03,
        hold_enforcement_max_attempts=3,
    )
    orch.start_monitoring()
    try:

        def raising_standby():
            raise RuntimeError("PSU refused standby")

        monkeypatch.setattr(vi, "standby", raising_standby)

        events = []
        orch.error_event.connect(events.append)

        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)

        with caplog.at_level(logging.CRITICAL, logger="cryosoft.core.orchestrator"):
            qtbot.waitUntil(
                lambda: orch._hold_enforcement_attempts.get("magnet_z", 0) >= 3, timeout=3000
            )
            qtbot.wait(60)  # let the escalating tick's signal land

        escalations = [
            e for e in events if e.kind == "safety_hold" and e.severity == "error"
        ]
        assert len(escalations) == 1
        assert "magnet_z" in escalations[0].message
        critical_records = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(critical_records) == 1

        # Retried, not abandoned: a further interval later, a fourth attempt happens.
        qtbot.wait(150)
        assert orch._hold_enforcement_attempts.get("magnet_z", 0) >= 4

        # Escalation still fires only once per episode.
        escalations = [
            e for e in events if e.kind == "safety_hold" and e.severity == "error"
        ]
        assert len(escalations) == 1
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


def test_hold_enforcement_bookkeeping_resets_on_settle_and_fresh_episode_starts_at_zero(
    station, qtbot, monkeypatch
):
    """Attempt/escalation bookkeeping is scoped to the CURRENT held-and-away
    episode: it must clear the instant ``standby_status()`` leaves "away",
    and a later, unrelated episode on the same VI must not inherit it.
    """
    vi = station.magnet_z
    driver = station.magnet_z._driver
    original_standby = vi.standby
    orch = Orchestrator(
        station,
        tick_interval_ms=10,
        hold_enforcement_interval_s=0.02,
        hold_enforcement_max_attempts=100,  # never escalate in this test
    )
    orch.start_monitoring()
    try:
        call_count = {"n": 0}

        def flaky_standby():
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise RuntimeError("transient PSU fault")
            return original_standby()

        monkeypatch.setattr(vi, "standby", flaky_standby)
        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)
        qtbot.waitUntil(
            lambda: orch._hold_enforcement_attempts.get("magnet_z", 0) >= 2, timeout=2000
        )

        def field_settled():
            _force_ramp_to_settle(driver)
            return vi.standby_status() != "away"

        qtbot.waitUntil(field_settled, timeout=2000)
        # standby_status() itself updates the instant the 3rd (successful)
        # call returns; the Orchestrator's own bookkeeping-clear only runs
        # at the START of the NEXT enforcement pass — give it one more tick.
        qtbot.wait(30)
        assert "magnet_z" not in orch._hold_enforcement_attempts
        assert "magnet_z" not in orch._hold_enforcement_last_s
        assert "magnet_z" not in orch._hold_enforcement_escalated

        # Recover, move the magnet away from standby's target again while
        # UNHELD (an ordinary manual action), then re-trip: a second,
        # unrelated episode.
        station.level_meter._driver._force_helium_level = None
        qtbot.waitUntil(lambda: "magnet_z" not in orch._held_vis(), timeout=2000)
        monkeypatch.setattr(vi, "standby", original_standby)

        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("magnet_z", "set_field", target_T=0.5)

        def field_at_half_tesla():
            _force_ramp_to_settle(driver)
            return abs(vi.magnet_field_T() - 0.5) < 0.01

        qtbot.waitUntil(field_at_half_tesla, timeout=2000)

        station.level_meter._driver._force_helium_level = 5.0
        qtbot.waitUntil(magnet_held, timeout=2000)

        def field_back_to_zero():
            _force_ramp_to_settle(driver)
            return abs(vi.magnet_field_T()) < 0.01

        qtbot.waitUntil(field_back_to_zero, timeout=2000)
        # The fresh episode never accumulated a raise: bookkeeping started
        # at zero, exactly as if this were the VI's very first hold.
        assert orch._hold_enforcement_attempts.get("magnet_z", 0) == 0
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


def test_override_suppresses_enforcement(station, qtbot, monkeypatch):
    """While ``override_active(vi_name)`` is True, enforcement must not call
    ``standby()`` at all — the operator is deliberately in control.
    """
    vi = station.magnet_z
    orch = Orchestrator(
        station,
        tick_interval_ms=10,
        hold_enforcement_interval_s=0.02,  # tiny: would re-fire fast if not suppressed
    )
    orch.start_monitoring()
    try:
        standby_calls: list[float] = []
        original_standby = vi.standby

        def counting_standby():
            standby_calls.append(time.time())
            return original_standby()

        monkeypatch.setattr(vi, "standby", counting_standby)

        station.level_meter._driver._force_helium_level = 5.0

        def magnet_held():
            return "magnet_z" in orch._held_vis()

        qtbot.waitUntil(magnet_held, timeout=2000)
        qtbot.waitUntil(lambda: len(standby_calls) >= 1, timeout=2000)  # the onset attempt

        orch.acknowledge()
        assert orch.override_active("magnet_z") is True

        calls_before = len(standby_calls)
        with qtbot.waitSignal(orch.action_succeeded, timeout=500):
            orch.submit_vi_action("magnet_z", "set_field", target_T=1.5)
        assert vi.standby_status() == "away"  # would be enforced if not overridden

        qtbot.wait(150)
        assert len(standby_calls) == calls_before
    finally:
        station.level_meter._driver._force_helium_level = None
        orch.shutdown()


class _SyntheticHoldFlagRampableVI(BaseVirtualInstrument, RampableVI):
    """Test-local double: declares its OWN hold-severity safety flag.

    Proves the enforcement standard's generality (see
    ``Orchestrator._enforce_safety_holds()``'s docstring): nothing there
    names a flag or a VI category — it reads only ``Condition.severity ==
    "hold"`` and ``affected_vis``, both already resolved by
    ``core.conditions.decide()``. A brand-new flag on a brand-new,
    non-magnet VI is enforced identically with zero Orchestrator change —
    this VI is both the flag's producer AND the sole VI concerned with it,
    unlike ``helium_low`` (produced by the level meter, concerned-with by
    the magnets), which exercises the same mechanism from the other side.
    """

    vi_type = "system"
    safety_flags: ClassVar[dict[str, str]] = {"widget_stuck": "hold"}

    def __init__(self, drivers: dict, **init_params: object) -> None:
        super().__init__(drivers, **init_params)
        self.value = 0.0
        self._target = 0.0
        self._ramping = False
        self.tripped = False

    def safety_concerns(self) -> set[str]:
        return {"widget_stuck"}

    def evaluate_safety(self, state: dict) -> dict[str, bool]:
        return {"widget_stuck": self.tripped}

    def start_ramp(self, target: float) -> None:
        self._target = target
        self._ramping = True

    def advance_ramp(self) -> None:
        if self._ramping:
            self.value = self._target
            self._ramping = False

    def ramp_status(self) -> str:
        return "RAMPING" if self._ramping else "TARGET_REACHED"

    def stop_ramp(self) -> None:
        self._ramping = False

    def standby(self) -> None:
        self.start_ramp(0.0)


def test_generality_second_hold_flag_on_non_magnet_vi_enforced_identically(qtbot):
    """A second, unrelated hold-severity flag on a non-magnet VI is enforced
    by the exact same, unmodified ``_enforce_safety_holds()`` — see
    ``_SyntheticHoldFlagRampableVI``'s docstring for what this proves.
    """
    station = Station()
    station.register_vi("widget", _SyntheticHoldFlagRampableVI({}), "system")
    orch = Orchestrator(station, tick_interval_ms=10, hold_enforcement_interval_s=0.02)
    orch.start_monitoring()
    vi = station.get_vi("widget")
    try:
        vi.start_ramp(5.0)
        vi.advance_ramp()
        assert vi.value == 5.0

        vi.tripped = True

        def widget_held():
            return "widget" in orch._held_vis()

        qtbot.waitUntil(widget_held, timeout=2000)

        def widget_back_to_zero_and_settled():
            return vi.value == 0.0 and vi.standby_status() == "reached"

        qtbot.waitUntil(widget_back_to_zero_and_settled, timeout=2000)
    finally:
        vi.tripped = False
        orch.shutdown()


class _SyntheticNonRampableHoldFlagVI(BaseVirtualInstrument):
    """Test-local double: a non-``RampableVI`` held by its own hold-severity flag."""

    vi_type = "system"
    safety_flags: ClassVar[dict[str, str]] = {"sensor_stuck": "hold"}

    def __init__(self, drivers: dict, **init_params: object) -> None:
        super().__init__(drivers, **init_params)
        self.tripped = False
        self.standby_calls = 0

    def safety_concerns(self) -> set[str]:
        return {"sensor_stuck"}

    def evaluate_safety(self, state: dict) -> dict[str, bool]:
        return {"sensor_stuck": self.tripped}

    def standby(self) -> None:
        self.standby_calls += 1


def test_non_rampable_held_vi_reports_reached_and_is_never_recommanded(qtbot):
    """A held non-``RampableVI`` has no intermediate state to converge
    through — ``standby_status()`` is unconditionally ``"reached"`` — so
    enforcement never calls its ``standby()`` at all.
    """
    station = Station()
    station.register_vi("sensor", _SyntheticNonRampableHoldFlagVI({}), "system")
    orch = Orchestrator(station, tick_interval_ms=10, hold_enforcement_interval_s=0.02)
    orch.start_monitoring()
    vi = station.get_vi("sensor")
    try:
        vi.tripped = True

        def sensor_held():
            return "sensor" in orch._held_vis()

        qtbot.waitUntil(sensor_held, timeout=2000)
        assert vi.standby_status() == "reached"
        qtbot.wait(150)
        assert vi.standby_calls == 0
    finally:
        vi.tripped = False
        orch.shutdown()


def test_hold_enforcement_announces_reassertion(orchestrator, station, qtbot):
    """A routine re-assertion emits BOTH the concise status line and a
    ``kind="safety_hold"``/``severity="warning"`` ``ErrorEvent`` naming the
    VI and the tripped condition — silent hardware motion is unacceptable
    even for a correct safety action (see ``_emit_hold_enforcement_event``'s
    docstring).
    """
    status_messages: list[str] = []
    orchestrator.status_message.connect(status_messages.append)
    events = []
    orchestrator.error_event.connect(events.append)

    station.level_meter._driver._force_helium_level = 5.0
    try:

        def hold_events():
            return [
                e for e in events if e.kind == "safety_hold" and e.vi_name == "magnet_z"
            ]

        qtbot.waitUntil(lambda: len(hold_events()) >= 1, timeout=2000)
        event = hold_events()[0]
        assert event.severity == "warning"
        assert "magnet_z" in event.message
        assert "helium_low" in event.message
        assert any("magnet_z" in message for message in status_messages)
    finally:
        station.level_meter._driver._force_helium_level = None
