# ---
# description: |
#   End-to-end behavior tests for the sample-access operation pair
#   (cryosoft/procedures/operations/sample_access_base.py's
#   _SampleAccessOperationBase, plus the two thin concrete classes
#   SampleLoadOperation / SampleUnloadOperation), driven by a real
#   Orchestrator (ticked directly, not via the QTimer) against the
#   sim_cryostat station. The two classes differ only in identity attributes
#   (name/config_key/ready_message) — every behavioral test below is
#   parametrized over both rather than duplicated. This is a "hold phase"
#   operation: the run no longer finishes on its own once the ramps land —
#   every test that wants the run to end now calls
#   orchestrator.finish_operation() (or abort_procedure()) explicitly,
#   mirroring what the OperationCard's Finish/Abort clicks do. Covers: the
#   run staying active (never IDLE) after the ramps settle; sample()
#   recording the VTI temperature + magnet fields into the shared recorder
#   every tick; Finish producing a "done" manifest with every postcondition
#   held (empty postconditions_unmet) and the recording in run_summary();
#   the needle-valve operator-confirmation gate — one-shot evaluated as the
#   run ends: unconfirmed finishes promptly with "needle_valve_confirmed"
#   named in postconditions_unmet, never blocking — measurement-VI standby
#   dispatch (no switch-VI dispatch — dropped when this operation split into
#   load/unload), the disarm_measurement_vis pre-run toggle (default True;
#   False skips both the standby() commands and the claimed_vi_names()
#   claim on every measurement VI, so one already armed for something else
#   is left alone), an end-to-end run through a real CryogenicsRecorder
#   (writing exactly one unified "servicing" entry per class, entry_kind
#   matching each class's config_key, with its recording written as a
#   sidecar), refusal while a procedure is running, construction-time
#   validation, and the operator-confirmation declaration standard itself
#   (confirm()/confirmed()).
#
#   The sim ITC503 (cryosoft/drivers/sim_oxford_itc503.py) starts at 300 K
#   already (its "room temperature" default) with a 60 s thermal time
#   constant — note this operation's own default target_temperature_K is
#   290 K, not 300 K, so tests relying on "VTI already at target" pass an
#   explicit target_temperature_K=300.0 override rather than assuming the
#   defaults coincide. To actually exercise the ramp/settle path (rather
#   than a test that starts already-at-target and never proves anything)
#   tests that care about the ramp first knock the VTI's simulated
#   temperature away from the target, then use `_fast_vti()` below (a large
#   ramp rate plus a shrunk `driver._tau`) so the settle completes in test
#   time — the same monkeypatch-the-sim-internals idiom test_helium_fill.py
#   uses for the ILM's `_force_helium_level`.
# last_updated: 2026-07-27
# ---

from __future__ import annotations

import json
import time

import pytest

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.operation import (
    STEP_KIND_AUTO_RAMP,
    STEP_KIND_OPERATOR_ACK,
    STEP_STATUS_DONE,
    STEP_STATUS_SKIPPED,
)
from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.plan import PhasePlan, Target
from cryosoft.core.station import build_station
from cryosoft.procedures.operations.sample_load import SampleLoadOperation
from cryosoft.procedures.operations.sample_unload import SampleUnloadOperation
from cryosoft.session.servicing_log import (
    CryogenicsRecorder,
    HeliumRecordStore,
    ServicingLogStore,
)

# Both concrete classes share 100% of their behavior (_SampleAccessOperationBase);
# every test in this file is parametrized over the pair rather than duplicated.
_OPERATION_CLASSES = [SampleLoadOperation, SampleUnloadOperation]
_OPERATION_IDS = ["load", "unload"]


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
    time constant, so a test that perturbs it away from its target needs
    both a fast ramp (setpoint reaches target immediately) and a shrunk
    thermal time constant (the simulated temperature actually catches up to
    the setpoint within a tick or two) to finish in test time.
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


def _make_op(op_cls, station, *, person: str = "Alex Tech", **overrides):
    """Build a sample-access operation with fast, test-friendly timing defaults."""
    config = dict(
        temperature_window_s=0.03,
        sample_period_s=0.0,  # tight tick-to-tick hold loop for test speed
    )
    config.update(overrides)
    return op_cls(station, person=person, **config)


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


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_constructs_from_defaults(op_cls, station):
    """The conformance suite's own check, made explicit: station alone suffices."""
    op = op_cls(station)
    assert op.name == op_cls.name
    assert op.tolerated_safety_flags == frozenset()


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_construction_rejects_missing_vti_vi(op_cls, station):
    """A vti_vi naming no registered VI is refused at construction, not later."""
    with pytest.raises(CryoSoftConfigError):
        op_cls(station, vti_vi="does_not_exist")


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_construction_rejects_non_manual_needle_valve(op_cls, station):
    """Only needle_valve == 'manual' is implemented today."""
    with pytest.raises(CryoSoftConfigError):
        op_cls(station, needle_valve="auto")


# ── The declared step sequence (the step standard itself) ────────────────────


_EXPECTED_STEP_KEYS = [
    "warm_vti",
    "close_needle_valve",
    "open_access_valve",
    "move_rod",
    "close_access_valve",
    "flush",
]


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_steps_declared_in_physical_order(op_cls, station):
    """The sequence is the physical procedure, in the order it is performed."""
    op = op_cls(station)
    assert [s.key for s in op.steps()] == _EXPECTED_STEP_KEYS

    kinds = {s.key: s.kind for s in op.steps()}
    assert kinds["warm_vti"] == STEP_KIND_AUTO_RAMP
    assert all(
        kind == STEP_KIND_OPERATOR_ACK
        for key, kind in kinds.items()
        if key != "warm_vti"
    ), "everything except the VTI ramp is a physical act the software cannot do"

    # Every step is skippable — a sample change must never be blocked.
    assert all(s.skippable for s in op.steps())


def test_rod_step_label_is_the_only_difference_between_load_and_unload(station):
    load = SampleLoadOperation(station)
    unload = SampleUnloadOperation(station)

    load_steps = {s.key: s.label for s in load.steps()}
    unload_steps = {s.key: s.label for s in unload.steps()}

    assert load_steps["move_rod"] == "Insert the sample rod"
    assert unload_steps["move_rod"] == "Withdraw the sample rod"
    assert {k: v for k, v in load_steps.items() if k != "move_rod"} == {
        k: v for k, v in unload_steps.items() if k != "move_rod"
    }


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_current_step_advances_only_as_each_is_recorded(op_cls, station):
    """Sequential, one at a time: current_step() is the first without an outcome."""
    op = op_cls(station)
    assert op.current_step().key == "warm_vti"

    op.confirm("warm_vti")
    assert op.current_step().key == "close_needle_valve"

    # Skipping advances too — an override is an outcome, not a failure.
    op.skip_step("close_needle_valve")
    assert op.current_step().key == "open_access_valve"

    for key in ("open_access_valve", "move_rod", "close_access_valve", "flush"):
        op.confirm(key)
    assert op.current_step() is None


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_confirm_roundtrip_and_skipped_step_is_not_confirmed(op_cls, station):
    op = op_cls(station)
    assert op.confirmed("close_needle_valve") is False

    op.confirm("close_needle_valve")
    assert op.confirmed("close_needle_valve") is True

    # A skipped step is recorded, but is NOT "confirmed" — the distinction
    # is what makes the override visible in postconditions_unmet.
    op.skip_step("flush")
    assert op.step_records()["flush"].status == STEP_STATUS_SKIPPED
    assert op.confirmed("flush") is False


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_confirm_unknown_key_raises(op_cls, station):
    op = op_cls(station)
    with pytest.raises(ValueError):
        op.confirm("not_a_declared_key")


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_skip_unknown_key_raises(op_cls, station):
    op = op_cls(station)
    with pytest.raises(ValueError):
        op.skip_step("not_a_declared_key")


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_recording_an_outcome_twice_keeps_the_first(op_cls, station):
    """A double-click must not rewrite the time something already happened."""
    op = op_cls(station)
    op.confirm("flush")
    first = op.step_records()["flush"]

    op.confirm("flush")
    op.skip_step("flush")
    assert op.step_records()["flush"] == first


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_skipping_the_warm_up_asks_for_the_ramp_to_stop(op_cls, station):
    """Only an auto_ramp step raises the flag the Orchestrator acts on."""
    op = op_cls(station)
    assert op.skip_ramp_requested is False

    op.skip_step("flush")
    assert op.skip_ramp_requested is False, "an operator_ack step drives no hardware"

    op.skip_step("warm_vti")
    assert op.skip_ramp_requested is True


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_step_record_captures_conditions_including_non_numeric(op_cls, station):
    """The step record is where string-valued readings land; the trace cannot hold them."""
    station.get_state()
    op = op_cls(station)
    op.confirm("close_needle_valve")

    conditions = op.step_records()["close_needle_valve"].conditions
    assert conditions["temperature_vti.needle_valve_mode"] in {"AUTO", "MANUAL"}
    assert isinstance(conditions["temperature_vti.temperature"], (int, float))
    for magnet in station.magnet_vi_names():
        assert isinstance(conditions[f"{magnet}.magnet_state"], str)


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_steps_summary_lists_every_step_including_pending(op_cls, station):
    op = op_cls(station)
    op.confirm("warm_vti")
    op.skip_step("close_needle_valve")

    summary = {row["key"]: row for row in op.steps_summary()}
    assert [row["key"] for row in op.steps_summary()] == _EXPECTED_STEP_KEYS
    assert summary["warm_vti"]["status"] == STEP_STATUS_DONE
    assert summary["close_needle_valve"]["status"] == STEP_STATUS_SKIPPED
    assert summary["flush"]["status"] == "pending"
    assert summary["flush"]["unix_time"] is None
    assert summary["warm_vti"]["unix_time"] > 0
    # JSON-plain: it round-trips through the run manifest into a sidecar.
    json.dumps(op.steps_summary())


# ── Full happy-path run ────────────────────────────────────────────────────


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_holds_until_finish_then_all_postconditions_held(op_cls, orchestrator, station, qtbot):
    """Zero-field + target-temperature ramps, run holds, Finish -> done manifest, no data file."""
    _fast_magnets(station)
    _fast_vti(station)
    station.magnet_z._driver._current = 5.0
    station.magnet_z._driver._setpoint = 5.0
    station.temperature_vti._driver._temperature = 250.0
    station.temperature_vti._driver._setpoint = 250.0

    op = _make_op(op_cls, station, target_temperature_K=290.0)

    started: list[dict] = []
    finished: list[dict] = []
    orchestrator.run_started.connect(started.append)
    orchestrator.run_finished.connect(finished.append)

    orchestrator.run_operation(op)
    assert orchestrator._procedure is op
    assert started and started[0]["kind"] == "operation"
    assert started[0]["procedure"] == op_cls.name

    for key in _EXPECTED_STEP_KEYS:
        orchestrator.confirm_operation(key)

    # Let the ramps settle to zero field / target temperature, then run a
    # further batch of ticks — the run must NOT finish on its own (the hold
    # phase).
    _tick_until(
        orchestrator,
        lambda: abs(station.temperature_vti.temperature() - 290.0) <= 2.0,
        max_ticks=2000,
        sleep_s=0.01,
    )
    for _ in range(50):
        orchestrator._tick()
    assert not finished
    assert orchestrator._state != OrchestratorState.IDLE
    assert orchestrator._procedure is op

    orchestrator.finish_operation()
    _tick_until(orchestrator, lambda: bool(finished), max_ticks=2000, sleep_s=0.01)

    assert finished[0]["status"] == "done"
    assert finished[0]["kind"] == "operation"
    assert finished[0]["procedure"] == op_cls.name
    assert not finished[0]["data_file"]  # no DataManager -> manifest data_file stays empty
    assert finished[0]["postconditions_unmet"] == []  # every gate held, confirmed in time

    for name in station.magnet_vi_names():
        assert abs(station.get_vi(name).magnet_field_T()) < 0.01, f"{name} did not reach zero field"

    assert abs(station.temperature_vti.temperature() - 290.0) <= 2.0
    assert orchestrator._state == OrchestratorState.IDLE


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_records_every_numeric_monitored_channel_on_the_station(
    op_cls, orchestrator, station, qtbot
):
    """The recording spans the whole station, not just the VTI and the magnets."""
    _fast_magnets(station)
    _fast_vti(station)

    op = _make_op(op_cls, station, sample_period_s=0.0)
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)

    orchestrator.run_operation(op)
    orchestrator.confirm_operation("close_needle_valve")

    # A handful of hold-phase ticks (sample() runs once per MEASURING state).
    for _ in range(50):
        orchestrator._tick()
    assert not finished

    orchestrator.finish_operation()
    _tick_until(orchestrator, lambda: bool(finished), max_ticks=2000, sleep_s=0.01)

    recording = finished[0]["summary"]["recording"]
    channels = recording["channels"]
    assert "temperature_vti.temperature" in channels
    for magnet in station.magnet_vi_names():
        assert f"{magnet}.magnet_field_T" in channels

    # The point of the widened trace: instruments that have nothing to do
    # with the sample access itself are recorded too, because "what was the
    # system doing while the cryostat was open" is the question the log
    # entry has to answer. Cryogen levels are the case that matters most —
    # a sample change is when helium gets lost.
    assert "level_meter.helium_level" in channels
    assert "level_meter.nitrogen_level" in channels
    assert "temperature_sample.temperature" in channels
    assert "temperature_vti.needle_valve" in channels

    # No string-valued channel leaked into the numeric trace.
    assert "temperature_vti.needle_valve_mode" not in channels
    assert "magnet_z.magnet_state" not in channels

    assert len(recording["unix_time"]) >= 1
    for series in channels.values():
        assert len(series) == len(recording["unix_time"])


# ── Needle-valve operator-confirmation gate: one-shot evaluation (plan
# operation-concurrency-and-error-scoping.md §2 — never held, never timed
# out) ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_needle_valve_not_confirmed_finishes_promptly_with_unmet_postcondition(
    op_cls, orchestrator, station, qtbot
):
    """An unconfirmed needle valve does not block finish; it is named unmet."""
    _fast_magnets(station)
    _fast_vti(station)
    # target_temperature_K=300.0 (not this operation's 290 K default) matches
    # the sim ITC503's actual start temperature, and magnets already sit at
    # 0 T -> zero_field and vti_at_target hold immediately, so
    # needle_valve_confirmed is the only gate that can be unmet, isolating
    # it in the assertion below.
    op = _make_op(op_cls, station, temperature_window_s=0.0, target_temperature_K=300.0)

    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    # The hold phase: the run stays active even with every postcondition
    # already holding — never confirming needle_valve does not end it.
    for _ in range(30):
        orchestrator._tick()
    assert not finished

    orchestrator.finish_operation()
    _tick_until(orchestrator, lambda: bool(finished), max_ticks=1000, sleep_s=0.005)
    assert finished[0]["status"] == "done"
    # warm_vti completed on its own (the VTI already reads the target), so
    # every remaining unmet gate is an unconfirmed operator step.
    assert finished[0]["postconditions_unmet"] == [
        "step_close_needle_valve",
        "step_open_access_valve",
        "step_move_rod",
        "step_close_access_valve",
        "step_flush",
    ]
    assert orchestrator._state == OrchestratorState.IDLE


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_warm_up_step_completes_itself_when_the_vti_reaches_target(
    op_cls, orchestrator, station, qtbot
):
    """The one step the system performs is recorded by the system, not the operator."""
    _fast_magnets(station)
    _fast_vti(station)
    station.temperature_vti._driver._temperature = 250.0
    station.temperature_vti._driver._setpoint = 250.0

    op = _make_op(op_cls, station, target_temperature_K=290.0, sample_period_s=0.0)
    orchestrator.run_operation(op)
    assert op.current_step().key == "warm_vti"

    _tick_until(
        orchestrator,
        lambda: "warm_vti" in op.step_records(),
        max_ticks=2000,
        sleep_s=0.01,
    )
    assert op.step_records()["warm_vti"].status == STEP_STATUS_DONE
    assert op.current_step().key == "close_needle_valve"
    assert abs(station.temperature_vti.temperature() - 290.0) <= 2.0


# ── Skipping: a sample change is never blocked, only recorded ──────────────


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_skipping_the_warm_up_stops_the_ramp_and_clamps_the_vti(
    op_cls, orchestrator, station, qtbot
):
    """The headline behaviour: change a sample at base temperature, on the record."""
    _fast_magnets(station)
    # Deliberately NOT _fast_vti: the VTI keeps its real 2 K/min ramp rate,
    # so the run is genuinely stuck in RAMPING far from 290 K when the
    # operator skips. That is the situation this exists for — a sample that
    # has to come out now, at base temperature.
    vti = station.temperature_vti
    vti._driver._temperature = 4.0
    vti._driver._setpoint = 4.0

    op = _make_op(op_cls, station, target_temperature_K=290.0, sample_period_s=0.0)
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)

    # The run parks in RAMPING: without the skip it would sit here for the
    # ~2.4 hours the warm-up actually takes.
    for _ in range(20):
        orchestrator._tick()
    assert orchestrator._state == OrchestratorState.RAMPING
    assert vti.ramp_status() == "RAMPING"
    assert vti.temperature() < 290.0

    orchestrator.skip_operation_step("warm_vti")
    assert op.skip_ramp_requested is True

    # The Orchestrator stops the ramp on the tick, not in the GUI call —
    # the single-writer rule. One tick is enough, and it also releases the
    # run from RAMPING, which is the part the operation cannot do itself.
    orchestrator._tick()
    assert op.skip_ramp_requested is False
    assert vti.ramp_status() == "IDLE", "the VTI ramp was not stopped"
    assert orchestrator._state != OrchestratorState.RAMPING

    for _ in range(200):
        orchestrator._tick()

    clamped_at = vti.temperature()
    assert clamped_at == pytest.approx(4.0, abs=1.0), (
        f"VTI drifted to {clamped_at} K after the warm-up was skipped; it "
        "should be clamped where it was"
    )
    assert op.step_records()["warm_vti"].status == STEP_STATUS_SKIPPED
    assert op.current_step().key == "close_needle_valve", (
        "the sequence must carry on past a skipped step"
    )

    orchestrator.finish_operation()
    _tick_until(orchestrator, lambda: bool(finished), max_ticks=2000, sleep_s=0.005)

    assert finished[0]["status"] == "done", "a skip must never fail the run"
    unmet = finished[0]["postconditions_unmet"]
    assert "step_warm_vti" in unmet
    assert "vti_at_target" in unmet
    steps = {row["key"]: row for row in finished[0]["summary"]["steps"]}
    assert steps["warm_vti"]["status"] == STEP_STATUS_SKIPPED
    assert steps["warm_vti"]["conditions"]["temperature_vti.temperature"] < 290.0


def test_skip_operation_step_is_blocked_when_no_operation_runs(orchestrator, qtbot):
    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    orchestrator.skip_operation_step("warm_vti")
    assert blocked and "no operation" in blocked[0].lower()


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_skip_operation_step_rejects_an_undeclared_key_as_a_verdict(
    op_cls, orchestrator, station, qtbot
):
    """An unknown key must be a verdict, never an exception in a Qt slot."""
    _fast_magnets(station)
    _fast_vti(station)
    op = _make_op(op_cls, station)
    orchestrator.run_operation(op)

    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)
    orchestrator.skip_operation_step("not_a_declared_key")

    assert blocked and "not_a_declared_key" in blocked[0]
    assert op.skip_ramp_requested is False


# ── initiate() dispatch: measurement standby (no switch dispatch — dropped
# when this operation split into load/unload) ────────────────────────────


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_measurement_vis_get_standby(op_cls, orchestrator, station, qtbot):
    """Every measurement VI is disarmed on initiate()."""
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

    op = _make_op(op_cls, station)
    orchestrator.run_operation(op)

    # initiate()'s commands are dispatched synchronously, before the first
    # tick even runs (mirrors HeliumFillOperation's FAST-refresh assertion).
    assert all(count == 1 for count in standby_calls.values()), standby_calls

    orchestrator.confirm_operation("needle_valve")
    orchestrator.abort_procedure()


def test_claimed_vi_names_excludes_switch(station):
    """claimed_vi_names() no longer names the switch matrix (dropped from initiate())."""
    op = SampleLoadOperation(station)
    assert "switch_matrix" not in op.claimed_vi_names()


# ── disarm_measurement_vis pre-run toggle: default on, skippable per run ─────


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_disarm_measurement_vis_defaults_true(op_cls, station):
    op = op_cls(station)
    assert op.get_params()["disarm_measurement_vis"] is True
    assert set(station.measurement_vi_names()) <= op.claimed_vi_names()


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_disarm_measurement_vis_false_excludes_measurement_vis_from_claim(op_cls, station):
    op = op_cls(station, disarm_measurement_vis=False)
    assert op.get_params()["disarm_measurement_vis"] is False
    claimed = op.claimed_vi_names()
    for name in station.measurement_vi_names():
        assert name not in claimed


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_disarm_measurement_vis_false_skips_measurement_standby_commands(
    op_cls, orchestrator, station, qtbot
):
    """With the toggle off, initiate() never calls standby() on any measurement VI."""
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

    op = _make_op(op_cls, station, disarm_measurement_vis=False)
    orchestrator.run_operation(op)

    assert all(count == 0 for count in standby_calls.values()), standby_calls

    orchestrator.confirm_operation("needle_valve")
    orchestrator.abort_procedure()


def test_pre_run_toggles_declares_disarm_measurement_vis():
    assert SampleLoadOperation.pre_run_toggles == {
        "disarm_measurement_vis": "Disarm measurement instruments"
    }
    assert SampleUnloadOperation.pre_run_toggles == SampleLoadOperation.pre_run_toggles


# ── End-to-end with a real CryogenicsRecorder ─────────────────────────────


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_cryogenics_recorder_records_one_servicing_entry(op_cls, orchestrator, station, tmp_path, qtbot):
    """A finished run produces exactly ONE "servicing" entry, keyed by this class's entry_kind."""
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

    op = _make_op(op_cls, station, person="Dr. Change")
    finished: list[dict] = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.run_operation(op)
    orchestrator.confirm_operation("close_needle_valve")
    orchestrator.confirm_operation("open_access_valve")
    orchestrator.skip_operation_step("flush")

    # The hold phase: several ticks pass with the run still active before
    # Finish is clicked, so sample() has a chance to record.
    for _ in range(30):
        orchestrator._tick()
    assert not finished

    orchestrator.finish_operation()
    _tick_until(orchestrator, lambda: bool(finished), max_ticks=2000, sleep_s=0.01)
    assert finished[0]["status"] == "done"

    entries = servicing_store.entries("servicing")
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == "operation"
    assert entry.values["entry_kind"] == op_cls.config_key
    assert entry.values["person"] == "Dr. Change"
    # A confirmed step's gate passes -> no "unmet: ..." trace in notes.
    notes = entry.values["notes"]
    assert "step_close_needle_valve" not in notes
    # A SKIPPED step is the opposite: the override must be visible in the
    # log entry a human reads, not only in the sidecar.
    assert "step_flush" in notes
    # The recorded station-wide series was written as a sidecar, referenced
    # from this entry.
    assert entry.values["recording"]
    sidecar_path = servicing_store.recordings_path(entry.values["recording"])
    assert sidecar_path.exists()
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert "temperature_vti.temperature" in sidecar["channels"]
    assert "level_meter.helium_level" in sidecar["channels"]

    # The step timeline rides along in the same sidecar — this is the whole
    # point of the operation: when each part of the sample change happened.
    steps = {row["key"]: row for row in sidecar["steps"]}
    assert [row["key"] for row in sidecar["steps"]] == _EXPECTED_STEP_KEYS
    assert steps["close_needle_valve"]["status"] == STEP_STATUS_DONE
    assert steps["close_needle_valve"]["unix_time"] > 0
    assert steps["flush"]["status"] == STEP_STATUS_SKIPPED
    assert steps["move_rod"]["status"] == "pending"
    # Non-numeric conditions, which the numeric series cannot hold, are
    # preserved on the step record instead.
    assert steps["close_needle_valve"]["conditions"]["temperature_vti.needle_valve_mode"]

    # Neither legacy kind is written by the recorder anymore (Phase 2).
    assert servicing_store.entries("operations") == []
    assert servicing_store.entries("cryogenics") == []


# ── Refused while a procedure runs ────────────────────────────────────────


@pytest.mark.parametrize("op_cls", _OPERATION_CLASSES, ids=_OPERATION_IDS)
def test_refused_while_procedure_runs(op_cls, orchestrator, station, qtbot):
    """A running procedure is never auto-aborted by run_operation()."""
    proc = _BlockingProcedure(station)
    orchestrator.run_procedure(proc)
    assert orchestrator._procedure is proc
    assert station.magnet_z.ramp_status() == "RAMPING"

    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    op = _make_op(op_cls, station)
    orchestrator.run_operation(op)

    assert blocked, "run_operation must be refused with action_blocked"
    assert "abort" in blocked[0].lower()
    assert orchestrator._procedure is proc  # untouched

    orchestrator.abort_procedure()
