# ---
# description: |
#   End-to-end behavior tests for HeliumFillOperation
#   (cryosoft/procedures/operations/helium_fill.py, plan §8.1), driven by a
#   real Orchestrator (ticked directly, not via the QTimer) against the
#   sim_cryostat station: zero-field ramp + initiation gate, FAST/SLOW
#   refresh, datapoint accumulation and HDF5 layout, the completion
#   condition (monkeypatching the sim ILM's private _force_helium_level, not
#   a new sim-only public method), max-duration termination, abort mid-fill,
#   the helium_low-tolerated-but-quench-not safety matrix, and an end-to-end
#   run through a real CryogenicsRecorder.
# last_updated: 2026-07-19
# ---

from __future__ import annotations

import time

import h5py
import pytest

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import build_station
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
from cryosoft.session.servicing_log import CryogenicsRecorder, HeliumRecordStore, ServicingLogStore

_REFRESH_SLOW = 1
_REFRESH_FAST = 2


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def station():
    """Build a real simulated station (sim_cryostat: magnet_z, magnet_y, level_meter)."""
    return build_station("cryosoft/configs/sim_cryostat")


@pytest.fixture
def orchestrator(station, qtbot):
    """Orchestrator ticked directly by the tests, monitoring active."""
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.start_monitoring()
    yield orch
    orch.shutdown()


def _fast_magnets(station) -> None:
    """Make every magnet's ramps effectively instant."""
    for name in station.magnet_vi_names():
        vi = station.get_vi(name)
        vi._default_ramp_rate = 6000.0
        vi._ramp_segments = []


def _tick_until(orchestrator, predicate, *, max_ticks: int = 2000, sleep_s: float = 0.0) -> None:
    """Advance the Orchestrator by calling _tick() directly until *predicate* holds.

    Deterministic alternative to qtbot.waitUntil()'s reliance on the real
    QTimer: the test drives the tick loop itself. An optional per-tick sleep
    lets wall-clock-windowed Gates/step() conditions (fill_complete_window_s,
    fill_zero_field_window_s) actually accumulate real elapsed time.
    """
    for _ in range(max_ticks):
        if predicate():
            return
        if sleep_s:
            time.sleep(sleep_s)
        orchestrator._tick()
    raise AssertionError(f"condition not satisfied within {max_ticks} ticks")


def _make_op(station, tmp_path, *, person: str = "Alex Tech", **overrides) -> HeliumFillOperation:
    """Build a HeliumFillOperation with fast, test-friendly timing defaults."""
    config = dict(
        fill_target_pct=50.0,  # sim ILM starts at 80% helium -> already "at target"
        fill_zero_field_eps_T=0.01,
        fill_zero_field_window_s=0.0,
        fill_complete_window_s=0.03,
        max_fill_duration_s=30.0,
        sample_period_s=0.0,
        data_directory=str(tmp_path),
    )
    config.update(overrides)
    return HeliumFillOperation(station, person=person, **config)


# ── Full happy-path run ────────────────────────────────────────────────────


def test_helium_fill_end_to_end(orchestrator, station, tmp_path, qtbot):
    """Zero-field ramp + gate, FAST->SLOW refresh, datapoints, done manifest, HDF5 layout."""
    _fast_magnets(station)
    station.magnet_z._driver._current = 5.0
    station.magnet_z._driver._setpoint = 5.0
    # Hold the sim ILM level fixed (bypassing its slow natural downward
    # drift) so every sample reads identically and the "level did not fall
    # below its start value" postcondition is unambiguous.
    station.level_meter._driver._force_helium_level = 70.0

    op = _make_op(station, tmp_path)

    started: list[dict] = []
    finished: list[dict] = []
    orchestrator.run_started.connect(started.append)
    orchestrator.run_finished.connect(finished.append)

    orchestrator.run_operation(op)
    assert orchestrator._procedure is op
    assert started and started[0]["kind"] == "operation"
    assert started[0]["procedure"] == "Helium Fill"
    # set_refresh_rate(FAST) is dispatched synchronously by initiate(), before
    # the first tick even runs.
    assert station.level_meter.get_refresh_rate() == _REFRESH_FAST

    _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.005)

    assert finished[0]["status"] == "done"
    assert finished[0]["kind"] == "operation"
    assert finished[0]["procedure"] == "Helium Fill"

    for name in station.magnet_vi_names():
        assert abs(station.get_vi(name).get_field()) < 0.01, f"{name} did not reach zero field"

    assert station.level_meter.get_refresh_rate() == _REFRESH_SLOW
    assert orchestrator._state == OrchestratorState.IDLE
    assert op.data_filepath is None  # closed by standby()

    data_file = finished[0]["data_file"]
    assert data_file
    with h5py.File(data_file, "r") as f:
        data = f["data"]
        assert "unix_time" in data
        assert "helium_pct" in data
        for name in station.magnet_vi_names():
            assert f"field_T_{name}" in data
        # At least one point was actually saved (trimmed to actual count on close).
        assert data["helium_pct"].shape[0] >= 1
        assert data["unix_time"].shape[0] == data["helium_pct"].shape[0]


def test_helium_fill_accumulates_multiple_datapoints(orchestrator, station, tmp_path):
    """A fill that takes a few sample cycles to settle saves more than one point."""
    _fast_magnets(station)
    station.level_meter._driver._force_helium_level = 70.0
    op = _make_op(
        station,
        tmp_path,
        fill_target_pct=50.0,
        fill_complete_window_s=0.05,
        sample_period_s=0.02,
    )

    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.01)

    with h5py.File(finished[0]["data_file"], "r") as f:
        assert f["data"]["helium_pct"].shape[0] >= 2


# ── Completion condition (monkeypatched sim ILM level) ────────────────────


def test_completion_waits_for_target_and_stability(orchestrator, station, tmp_path):
    """The fill does not finish until the level holds at/above target, non-rising."""
    _fast_magnets(station)
    driver = station.level_meter._driver
    driver._force_helium_level = 40.0  # below fill_target_pct=60 -> not yet complete

    op = _make_op(
        station,
        tmp_path,
        fill_target_pct=60.0,
        fill_complete_window_s=0.03,
        sample_period_s=0.0,
        max_fill_duration_s=30.0,
    )
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    # Let a few sample cycles run below target: must not finish yet.
    for _ in range(20):
        orchestrator._tick()
    assert not finished, "fill must not complete before the level reaches target"

    # Raise the (monkeypatched) sim level above target and let it settle.
    driver._force_helium_level = 65.0
    _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.01)
    assert finished[0]["status"] == "done"


def test_rising_level_resets_the_stability_clock(orchestrator, station, tmp_path):
    """A still-rising level (even above target) must not count as settled."""
    _fast_magnets(station)
    driver = station.level_meter._driver
    driver._force_helium_level = 61.0

    op = _make_op(
        station,
        tmp_path,
        fill_target_pct=60.0,
        fill_complete_window_s=0.05,
        sample_period_s=0.0,
    )
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    # Keep nudging the level up on every tick -> "rising", must never settle.
    for _ in range(15):
        driver._force_helium_level += 0.5
        orchestrator._tick()
        time.sleep(0.005)
    assert not finished, "a continuously rising level must never read as settled"

    # Let it go flat: stability clock can now start and complete.
    _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.01)
    assert finished[0]["status"] == "done"


# ── Max-duration termination ──────────────────────────────────────────────


def test_max_fill_duration_terminates_and_logs_warning(orchestrator, station, tmp_path, caplog):
    """An unreachable target ends the fill once max_fill_duration_s elapses."""
    _fast_magnets(station)
    driver = station.level_meter._driver
    driver._force_helium_level = 30.0  # held constant, above helium_low_threshold (20)

    op = _make_op(
        station,
        tmp_path,
        fill_target_pct=999.0,  # unreachable
        max_fill_duration_s=0.05,
        sample_period_s=0.0,
        fill_complete_window_s=0.0,
    )
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)

    with caplog.at_level("WARNING"):
        orchestrator.run_operation(op)
        _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.01)

    assert any("max_fill_duration_s" in rec.message for rec in caplog.records)
    assert station.level_meter.get_refresh_rate() == _REFRESH_SLOW
    assert orchestrator._state == OrchestratorState.IDLE


# ── Abort mid-fill ─────────────────────────────────────────────────────────


def test_abort_mid_fill_restores_slow_and_closes_file(orchestrator, station, tmp_path):
    """Aborting a fill in progress must never leave the level meter in FAST."""
    _fast_magnets(station)
    op = _make_op(
        station,
        tmp_path,
        fill_target_pct=999.0,  # never reached -> stays running until aborted
        max_fill_duration_s=3600.0,
        sample_period_s=0.0,
    )
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    orchestrator._tick()
    assert station.level_meter.get_refresh_rate() == _REFRESH_FAST

    orchestrator.abort_procedure()

    assert station.level_meter.get_refresh_rate() == _REFRESH_SLOW
    assert op.data_filepath is None
    assert orchestrator._state == OrchestratorState.IDLE
    assert finished and finished[0]["status"] == "aborted"


# ── Safety matrix: helium_low tolerated, quench is not ────────────────────


def test_helium_low_does_not_abort_the_fill(orchestrator, station, tmp_path, qtbot):
    """helium_low, the fill's whole reason to exist, must not abort it (plan §7)."""
    _fast_magnets(station)
    op = _make_op(
        station,
        tmp_path,
        fill_target_pct=999.0,
        max_fill_duration_s=3600.0,
        sample_period_s=0.0,
    )
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    station.level_meter._driver._force_helium_level = 5.0
    for _ in range(15):
        orchestrator._tick()
    assert station.check_safety().get("helium_low") is True
    assert orchestrator._state != OrchestratorState.EMERGENCY
    assert orchestrator._procedure is op

    orchestrator.abort_procedure()


def test_quench_still_aborts_the_fill(orchestrator, station, tmp_path):
    """A quench is never tolerated, even by an operation that tolerates helium_low.

    Reuses the Phase-2 sim quench pattern (test_operations.py:
    test_quench_still_enters_emergency_and_aborts_operation) against the real
    HeliumFillOperation instead of the generic SimpleOperation test double.
    """
    _fast_magnets(station)
    op = _make_op(
        station,
        tmp_path,
        fill_target_pct=999.0,
        max_fill_duration_s=3600.0,
        sample_period_s=0.0,
    )
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    station.magnet_z._driver._simulate_quench = True
    _tick_until(
        orchestrator,
        lambda: orchestrator._state == OrchestratorState.EMERGENCY,
        max_ticks=2000,
    )
    assert orchestrator._procedure is None
    assert op.data_filepath is None  # abort() closed the file
    assert station.level_meter.get_refresh_rate() == _REFRESH_SLOW


# ── End-to-end with a real CryogenicsRecorder ─────────────────────────────


def test_cryogenics_recorder_records_the_finished_fill(orchestrator, station, tmp_path, qtbot):
    """A finished fill produces one cryogenics-log entry and one operations entry."""
    _fast_magnets(station)
    station.level_meter._driver._force_helium_level = 70.0
    helium_store = HeliumRecordStore(tmp_path / "servicing", "sim_cryostat")
    servicing_store = ServicingLogStore(tmp_path / "servicing", "sim_cryostat")
    recorder = CryogenicsRecorder(
        helium_store,
        servicing_store,
        level_vi_name="level_meter",
        warning_pct=35.0,
    )
    orchestrator.states_updated.connect(recorder.on_states_updated)
    orchestrator.run_started.connect(recorder.on_run_started)
    orchestrator.run_finished.connect(recorder.on_run_finished)

    op = _make_op(
        tmp_path=tmp_path,
        station=station,
        person="Dr. Fill",
        fill_target_pct=50.0,
        fill_complete_window_s=0.03,
        sample_period_s=0.0,
    )
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.01)
    assert finished[0]["status"] == "done"

    cryo_entries = servicing_store.entries("cryogenics")
    assert len(cryo_entries) == 1
    entry = cryo_entries[0]
    assert entry.source == "operation"
    assert entry.run_id == finished[0]["run_id"]
    assert entry.values["person"] == "Dr. Fill"

    ops_entries = servicing_store.entries("operations")
    assert len(ops_entries) == 1
    assert ops_entries[0].values["operation"] == "Helium Fill"
    assert ops_entries[0].values["status"] == "done"
