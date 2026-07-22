# ---
# description: |
#   End-to-end behavior tests for SampleChangeOperation
#   (cryosoft/procedures/operations/sample_change.py, plan §8.2), driven by a
#   real Orchestrator (ticked directly, not via the QTimer) against the
#   sim_cryostat station: full run to zero field + 300 K with every
#   postcondition held (empty postconditions_unmet), the needle-valve
#   operator-confirmation gate — one-shot evaluated as the run ends
#   (docs/plans/operation-concurrency-and-error-scoping.md §2): unconfirmed
#   finishes promptly with "needle_valve_confirmed" named in
#   postconditions_unmet, never blocking — measurement-VI standby + switch-VI
#   open_all dispatch, an end-to-end run through a real CryogenicsRecorder
#   (writing exactly one unified "servicing" entry, entry_kind=
#   "sample_change", never the legacy "cryogenics"/"operations" kinds — see
#   docs/plans/unified-servicing-log-and-run-recording.md §2), refusal while
#   a procedure is running, construction-time validation, and the
#   operator-confirmation declaration standard itself (confirm()/confirmed()).
#
#   The sim ITC503 (cryosoft/drivers/sim_oxford_itc503.py) starts at 300 K
#   already (its "room temperature" default) with a 60 s thermal time
#   constant. To actually exercise the ramp/settle path (rather than a test
#   that starts already-at-target and never proves anything) tests that care
#   about the ramp first knock the VTI's simulated temperature away from
#   300 K, then use `_fast_vti()` below (a large ramp rate plus a shrunk
#   `driver._tau`) so the settle completes in test time — the same
#   monkeypatch-the-sim-internals idiom test_helium_fill.py uses for the
#   ILM's `_force_helium_level`.
# last_updated: 2026-07-21
# ---

from __future__ import annotations

import time

import pytest

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.plan import PhasePlan, Target
from cryosoft.core.station import build_station
from cryosoft.procedures.operations.sample_change import SampleChangeOperation
from cryosoft.session.servicing_log import (
    CryogenicsRecorder,
    HeliumRecordStore,
    ServicingLogStore,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def station():
    """Build a real simulated station (sim_cryostat: magnet_z, magnet_y, ...)."""
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


def _fast_vti(station) -> None:
    """Make the VTI's ramp instant and its thermal settling near-instant.

    See the module docstring: the sim ITC503 starts at 300 K with a 60 s
    time constant, so a test that perturbs it away from 300 K needs both a
    fast ramp (setpoint reaches target immediately) and a shrunk thermal
    time constant (the simulated temperature actually catches up to the
    setpoint within a tick or two) to finish in test time.
    """
    vti = station.temperature_vti
    vti._default_ramp_rate = 6000.0
    vti._driver._tau = 0.01


def _tick_until(orchestrator, predicate, *, max_ticks: int = 2000, sleep_s: float = 0.0) -> None:
    """Advance the Orchestrator by calling _tick() directly until *predicate* holds.

    Mirrors tests/test_helium_fill.py's helper of the same name.
    """
    for _ in range(max_ticks):
        if predicate():
            return
        if sleep_s:
            time.sleep(sleep_s)
        orchestrator._tick()
    raise AssertionError(f"condition not satisfied within {max_ticks} ticks")


def _make_op(station, *, person: str = "Alex Tech", **overrides) -> SampleChangeOperation:
    """Build a SampleChangeOperation with fast, test-friendly timing defaults."""
    config = dict(
        zero_field_window_s=0.0,
        temperature_window_s=0.03,
    )
    config.update(overrides)
    return SampleChangeOperation(station, person=person, **config)


class _BlockingProcedure:
    """A duck-typed BaseProcedure-shaped test double that stays RAMPING.

    Mirrors ``BlockingProcedure`` in ``test_operations.py``: the sim
    magnet's default (slow) ramp rate keeps ``ramp_status()`` at "RAMPING"
    long enough for a test to submit a competing request mid-run.
    """

    name = "Blocking Procedure"

    def __init__(self, station):
        self._station = station

    def initiate(self):
        return PhasePlan(targets={"magnet_z": Target(1.0)}, commands=(), wait_s=0.0)

    def change_sweep_step(self):
        return None

    def measure(self):
        pass

    def standby(self):
        return PhasePlan(targets={"magnet_z": Target(0.0)}, commands=(), wait_s=0.0)


# ── Construction ─────────────────────────────────────────────────────────────


def test_constructs_from_defaults(station):
    """The conformance suite's own check, made explicit: station alone suffices."""
    op = SampleChangeOperation(station)
    assert op.name == "Sample Change"
    assert op.tolerated_safety_flags == frozenset()


def test_construction_rejects_missing_vti_vi(station):
    """A vti_vi naming no registered VI is refused at construction, not later."""
    with pytest.raises(CryoSoftConfigError):
        SampleChangeOperation(station, vti_vi="does_not_exist")


def test_construction_rejects_non_manual_needle_valve(station):
    """Only needle_valve == 'manual' is implemented today (plan §8.2)."""
    with pytest.raises(CryoSoftConfigError):
        SampleChangeOperation(station, needle_valve="auto")


# ── Operator confirmations (the declaration standard itself) ─────────────────


def test_operator_confirmations_declared_and_confirm_confirmed_roundtrip(station):
    op = SampleChangeOperation(station)
    assert op.operator_confirmations == {"needle_valve": "Needle valve closed"}
    assert op.confirmed("needle_valve") is False

    op.confirm("needle_valve")
    assert op.confirmed("needle_valve") is True


def test_confirm_unknown_key_raises(station):
    op = SampleChangeOperation(station)
    with pytest.raises(ValueError):
        op.confirm("not_a_declared_key")


# ── Full happy-path run ────────────────────────────────────────────────────


def test_sample_change_end_to_end(orchestrator, station, qtbot):
    """Zero-field + 300 K ramps, all postconditions held, done manifest, no data file."""
    _fast_magnets(station)
    _fast_vti(station)
    station.magnet_z._driver._current = 5.0
    station.magnet_z._driver._setpoint = 5.0
    station.temperature_vti._driver._temperature = 250.0
    station.temperature_vti._driver._setpoint = 250.0

    op = _make_op(station)

    started: list[dict] = []
    finished: list[dict] = []
    orchestrator.run_started.connect(started.append)
    orchestrator.run_finished.connect(finished.append)

    orchestrator.run_operation(op)
    assert orchestrator._procedure is op
    assert started and started[0]["kind"] == "operation"
    assert started[0]["procedure"] == "Sample Change"

    orchestrator.confirm_operation("needle_valve")

    _tick_until(orchestrator, lambda: bool(finished), max_ticks=2000, sleep_s=0.01)

    assert finished[0]["status"] == "done"
    assert finished[0]["kind"] == "operation"
    assert finished[0]["procedure"] == "Sample Change"
    assert not finished[0]["data_file"]  # no DataManager -> manifest data_file stays empty
    assert finished[0]["postconditions_unmet"] == []  # every gate held, confirmed in time

    for name in station.magnet_vi_names():
        assert abs(station.get_vi(name).get_field()) < 0.01, f"{name} did not reach zero field"

    assert abs(station.temperature_vti.temperature() - 300.0) <= 2.0
    assert orchestrator._state == OrchestratorState.IDLE


# ── Needle-valve operator-confirmation gate: one-shot evaluation (plan
# operation-concurrency-and-error-scoping.md §2 — never held, never timed
# out) ─────────────────────────────────────────────────────────────────────


def test_needle_valve_not_confirmed_finishes_promptly_with_unmet_postcondition(
    orchestrator, station, qtbot
):
    """An unconfirmed needle valve does not block finish; it is named unmet."""
    _fast_magnets(station)
    _fast_vti(station)
    # Defaults: magnets already at 0 T, VTI already at 300 K (the sim
    # ITC503's start temperature) -> zero_field and vti_at_target hold
    # immediately, so needle_valve_confirmed is the only gate that can be
    # unmet, isolating it in the assertion below.
    op = _make_op(station, temperature_window_s=0.0, zero_field_window_s=0.0)

    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.005)
    assert finished[0]["status"] == "done"
    assert finished[0]["postconditions_unmet"] == ["needle_valve_confirmed"]
    assert orchestrator._state == OrchestratorState.IDLE


# ── initiate() dispatch: measurement standby + switch open_all ───────────────


def test_measurement_vis_get_standby_and_switch_gets_open_all(orchestrator, station, qtbot):
    """Every measurement VI is disarmed and the switch VI's active route is cleared."""
    _fast_magnets(station)
    _fast_vti(station)

    standby_calls = {name: 0 for name in station.measurement_vi_names()}
    for name in station.measurement_vi_names():
        vi = station.get_vi(name)
        original_standby = vi.standby

        def _wrap(original, name):
            def wrapper():
                standby_calls[name] += 1
                return original()

            return wrapper

        vi.standby = _wrap(original_standby, name)

    station.switch_matrix.select_route("Mux-Ch1")
    assert station.switch_matrix.get_state()["active_route"] == "Mux-Ch1"

    op = _make_op(station)
    orchestrator.run_operation(op)

    # initiate()'s commands are dispatched synchronously, before the first
    # tick even runs (mirrors HeliumFillOperation's FAST-refresh assertion).
    assert all(count == 1 for count in standby_calls.values()), standby_calls
    assert station.switch_matrix.get_state()["active_route"] == ""

    orchestrator.confirm_operation("needle_valve")
    orchestrator.abort_procedure()


# ── End-to-end with a real CryogenicsRecorder ─────────────────────────────


def test_cryogenics_recorder_records_one_servicing_entry(orchestrator, station, tmp_path, qtbot):
    """A finished sample change produces exactly ONE "servicing" entry, never a legacy-kind one."""
    _fast_magnets(station)
    _fast_vti(station)
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

    op = _make_op(station, person="Dr. Change")
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)
    orchestrator.confirm_operation("needle_valve")

    _tick_until(orchestrator, lambda: bool(finished), max_ticks=2000, sleep_s=0.01)
    assert finished[0]["status"] == "done"

    entries = servicing_store.entries("servicing")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == "operation"
    assert entry.values["entry_kind"] == "sample_change"
    assert entry.values["person"] == "Dr. Change"
    # needle_valve was confirmed -> its postcondition gate passes -> no
    # "unmet: ..." trace in notes (see GLOSSARY.md's Operator confirmation).
    assert "needle_valve_confirmed" not in entry.values["notes"]

    # Neither legacy kind is written by the recorder anymore (Phase 2).
    assert servicing_store.entries("operations") == []
    assert servicing_store.entries("cryogenics") == []


# ── Refused while a procedure runs ────────────────────────────────────────


def test_sample_change_refused_while_procedure_runs(orchestrator, station, qtbot):
    """A running procedure is never auto-aborted by run_operation()."""
    proc = _BlockingProcedure(station)
    orchestrator.run_procedure(proc)
    assert orchestrator._procedure is proc
    assert station.magnet_z.ramp_status() == "RAMPING"

    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    op = _make_op(station)
    orchestrator.run_operation(op)

    assert blocked, "run_operation must be refused with action_blocked"
    assert "abort" in blocked[0].lower()
    assert orchestrator._procedure is proc  # untouched

    orchestrator.abort_procedure()
