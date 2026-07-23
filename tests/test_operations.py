# ---
# description: |
#   Behavior tests for the operation substrate (Phase 2, plan §4/§5/§7), and
#   the immediate-finish/one-shot-postcondition contract (docs/plans/
#   operation-concurrency-and-error-scoping.md §2): OperationBase driven by
#   the real Orchestrator against a real simulated Station. Covers the
#   helium_low-tolerated vs quench safety matrix, the EMERGENCY carve-out and
#   its end-state, run_operation refusal while a procedure is active,
#   operation-before-procedure queue priority, postcondition gates (one-shot
#   evaluation — a never-true gate is recorded as unmet rather than blocking;
#   an all-true set finishes with an empty postconditions_unmet),
#   finish_operation()'s immediate STANDBY path, and capability-scope
#   enforcement at command dispatch.
# last_updated: 2026-07-21
# ---

import time

import pytest

from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.core.gates import Gate
from cryosoft.core.operation import OperationBase
from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.station import build_station
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation


# ── Test doubles ────────────────────────────────────────────────────────────


class SimpleOperation(OperationBase):
    """A minimal, fully-controllable OperationBase test double.

    ``open_ended=True`` (default) makes ``step()`` return a fresh StepPlan
    forever, so the operation stays running until ``request_finish()`` is
    called (via ``Orchestrator.finish_operation()``) — useful for tests that
    need to tamper with safety conditions mid-run. ``open_ended=False`` makes
    ``step()`` return None on its first call (a one-shot operation).
    """

    name = "Simple Operation"

    def __init__(
        self,
        station,
        *,
        tolerated_safety_flags=frozenset(),
        initiate_commands=(),
        standby_commands=(),
        postcondition_gates_factory=None,
        open_ended=True,
        run_summary_factory=None,
    ) -> None:
        super().__init__()
        self._station = station
        self.tolerated_safety_flags = frozenset(tolerated_safety_flags)
        self._initiate_commands = tuple(initiate_commands)
        self._standby_commands = tuple(standby_commands)
        self._postcondition_gates_factory = postcondition_gates_factory
        self._open_ended = open_ended
        self._run_summary_factory = run_summary_factory
        self.sample_calls = 0
        self.postcondition_gates_calls = 0
        self.run_summary_calls = 0
        # Minimal operator-confirmation surface, mirroring
        # SampleChangeOperation's confirm()/confirmed() (plan §8.2) closely
        # enough to exercise Orchestrator.confirm_operation()'s duck-typed
        # dispatch generically, without depending on the concrete operation.
        self._confirmations: set[str] = set()

    def confirm(self, key: str) -> None:
        self._confirmations.add(key)

    def confirmed(self, key: str) -> bool:
        return key in self._confirmations

    def initiate(self) -> PhasePlan:
        return PhasePlan(
            targets={"magnet_z": Target(0.0)},
            commands=self._initiate_commands,
            wait_s=0.0,
        )

    def step(self) -> StepPlan | None:
        if not self._open_ended:
            return None
        return StepPlan(targets={"magnet_z": Target(0.0)}, wait_s=0.0)

    def sample(self) -> None:
        self.sample_calls += 1

    def standby(self) -> PhasePlan:
        return PhasePlan(
            targets={"magnet_z": Target(0.0)}, commands=self._standby_commands, wait_s=0.0
        )

    def postcondition_gates(self):
        self.postcondition_gates_calls += 1
        if self._postcondition_gates_factory is None:
            return ()
        return self._postcondition_gates_factory()

    def run_summary(self):
        self.run_summary_calls += 1
        if self._run_summary_factory is None:
            return {}
        return self._run_summary_factory()


class BlockingProcedure:
    """A duck-typed BaseProcedure-shaped test double that stays RAMPING.

    Mirrors ``MockProcedure`` in ``test_l3_orchestrator.py``: the sim
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


def _fast_magnet(station):
    """Make magnet_z ramps effectively instant."""
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []


def _fast_magnets(station):
    """Make every magnet's ramps effectively instant (mirrors test_helium_fill.py)."""
    for name in station.magnet_vi_names():
        vi = station.get_vi(name)
        vi._default_ramp_rate = 6000.0
        vi._ramp_segments = []


def _make_fill(station, tmp_path, **overrides) -> HeliumFillOperation:
    """Build a fast, test-friendly HeliumFillOperation (mirrors test_helium_fill.py).

    ``tmp_path`` is accepted (and ignored) for call-site compatibility — the
    fill no longer writes a data file.
    """
    config = dict(
        fill_target_pct=50.0,  # sim ILM starts at 80% helium -> already "at target"
        fill_zero_field_eps_T=0.01,
        fill_zero_field_window_s=0.0,
        fill_complete_window_s=0.03,
        max_fill_duration_s=30.0,
        sample_period_s=0.0,
    )
    config.update(overrides)
    return HeliumFillOperation(station, person="Alex Tech", **config)


# ── Fixtures (mirrors tests/test_l3_orchestrator.py) ─────────────────────────


@pytest.fixture
def station():
    """Build a real simulated station."""
    return build_station("cryosoft/configs/sim_cryostat")


@pytest.fixture
def orchestrator(station, qtbot):
    """Orchestrator with a small tick interval, monitoring active."""
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.start_monitoring()
    yield orch
    orch.shutdown()


# ── Safety matrix: tolerated flag vs quench ──────────────────────────────────


def test_tolerated_flag_does_not_abort_operation(orchestrator, station, qtbot):
    """helium_low, when declared tolerated, must not abort a running operation."""
    op = SimpleOperation(station, tolerated_safety_flags=frozenset({"helium_low"}))
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    # Drive the debounce buffer low; the operation must keep running.
    station.level_meter._driver._force_helium_level = 5.0
    qtbot.waitUntil(lambda: station.check_safety().get("helium_low") is True, timeout=2000)

    qtbot.wait(100)  # let several ticks pass with the flag active
    assert orchestrator._state != OrchestratorState.EMERGENCY
    assert orchestrator._procedure is op

    orchestrator.finish_operation()
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=2000)


def test_quench_still_enters_emergency_and_aborts_operation(orchestrator, station, qtbot):
    """A quench is never tolerated — it must still enter EMERGENCY and abort."""
    op = SimpleOperation(station, tolerated_safety_flags=frozenset({"helium_low"}))
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    station.magnet_z._driver._simulate_quench = True
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )
    assert orchestrator._procedure is None


# ── run_operation refusal while a procedure runs ─────────────────────────────


def test_run_operation_refused_while_procedure_runs(orchestrator, station, qtbot):
    """A running procedure is never auto-aborted by run_operation()."""
    proc = BlockingProcedure(station)
    orchestrator.run_procedure(proc)
    assert orchestrator._procedure is proc
    assert station.magnet_z.ramp_status() == "RAMPING"

    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    op = SimpleOperation(station)
    orchestrator.run_operation(op)

    assert blocked, "run_operation must be refused with action_blocked"
    assert "abort" in blocked[0].lower()
    assert orchestrator._procedure is proc  # untouched
    assert orchestrator._state != OrchestratorState.IDLE

    orchestrator.abort_procedure()


# ── EMERGENCY carve-out and end-state ────────────────────────────────────────


def test_run_operation_allowed_from_emergency_iff_flags_tolerated(
    orchestrator, station, qtbot
):
    """run_operation from EMERGENCY succeeds iff active flags <= tolerated."""
    station.level_meter._driver._force_helium_level = 5.0
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )

    # Quench is not tolerated by this operation -> refused.
    untolerant_op = SimpleOperation(station, tolerated_safety_flags=frozenset({"quench"}))
    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)
    orchestrator.run_operation(untolerant_op)
    assert blocked
    assert orchestrator._state == OrchestratorState.EMERGENCY
    assert orchestrator._procedure is None

    # helium_low IS tolerated -> allowed to start, straight from EMERGENCY.
    op = SimpleOperation(station, tolerated_safety_flags=frozenset({"helium_low"}))
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op
    assert orchestrator._state != OrchestratorState.EMERGENCY


def test_operation_end_state_returns_to_emergency_when_flags_persist(
    orchestrator, station, qtbot
):
    """A finishing operation returns to EMERGENCY, not IDLE, if flags persist."""
    station.level_meter._driver._force_helium_level = 5.0
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )

    op = SimpleOperation(
        station, tolerated_safety_flags=frozenset({"helium_low"}), open_ended=False
    )
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    # helium stays low throughout -> the finishing run must land back in
    # EMERGENCY, not IDLE.
    qtbot.waitUntil(
        lambda: orchestrator._procedure is None, timeout=5000
    )
    assert orchestrator._state == OrchestratorState.EMERGENCY


# ── Operation queue priority ──────────────────────────────────────────────


def test_operation_queue_drains_before_procedure_queue(orchestrator, station, qtbot):
    """Queued operations run ahead of queued procedures once IDLE is reached."""
    _fast_magnet(station)
    blocker = BlockingProcedure(station)
    station.magnet_z._default_ramp_rate = 5.0  # keep the blocker mid-ramp
    orchestrator.run_procedure(blocker)
    assert orchestrator._procedure is blocker

    queued_proc = BlockingProcedure(station)
    queued_op = SimpleOperation(station, open_ended=False)
    orchestrator.queue_procedure(queued_proc)
    orchestrator.queue_operation(queued_op)

    orchestrator.abort_procedure()  # returns to IDLE, calls run_queue()

    assert orchestrator._procedure is queued_op
    assert queued_proc in orchestrator._procedure_queue  # procedure untouched, still queued

    orchestrator.abort_procedure()


# ── Postcondition gates: one-shot evaluation (plan operation-concurrency-
# and-error-scoping.md §2 — never held, never timed out) ────────────────────


def test_postcondition_gate_never_true_finishes_promptly_with_unmet_name(
    orchestrator, station, qtbot
):
    """A never-satisfied postcondition gate does not block finish; it is named unmet."""
    _fast_magnet(station)

    op = SimpleOperation(
        station,
        open_ended=False,
        postcondition_gates_factory=lambda: (Gate("never_settles", check=lambda: False),),
    )
    start = time.monotonic()
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)

    qtbot.waitUntil(lambda: bool(finished), timeout=2000)
    # "Well under a second of sim time" — no hold, no wait for the gate.
    assert time.monotonic() - start < 1.0
    assert op.postcondition_gates_calls == 1  # evaluated exactly once
    assert finished[0]["status"] == "done"
    assert finished[0]["postconditions_unmet"] == ["never_settles"]
    assert orchestrator._state == OrchestratorState.IDLE


def test_postcondition_gates_all_true_finish_with_empty_unmet_list(
    orchestrator, station, qtbot
):
    """Every postcondition gate holding -> an empty postconditions_unmet list."""
    _fast_magnet(station)

    op = SimpleOperation(
        station,
        open_ended=False,
        postcondition_gates_factory=lambda: (Gate("settled", check=lambda: True),),
    )
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)

    qtbot.waitUntil(lambda: bool(finished), timeout=2000)
    assert finished[0]["status"] == "done"
    assert finished[0]["postconditions_unmet"] == []
    assert orchestrator._state == OrchestratorState.IDLE


# ── finish_operation() ────────────────────────────────────────────────────


def test_finish_operation_triggers_graceful_standby_and_manifest_done(
    orchestrator, station, qtbot
):
    """finish_operation() ends an open-ended operation via the STANDBY path."""
    _fast_magnet(station)
    op = SimpleOperation(station, open_ended=True)
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)

    qtbot.waitUntil(
        lambda: orchestrator._state
        in (
            OrchestratorState.RAMPING,
            OrchestratorState.MEASURING,
            OrchestratorState.SWEEPING,
        ),
        timeout=2000,
    )
    orchestrator.finish_operation()

    qtbot.waitUntil(lambda: bool(finished), timeout=3000)
    assert finished[0]["status"] == "done"
    assert finished[0]["kind"] == "operation"
    assert orchestrator._state == OrchestratorState.IDLE


def test_finish_operation_blocked_when_no_operation_running(orchestrator, station, qtbot):
    """finish_operation() with nothing active (or a plain procedure) is refused."""
    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    orchestrator.finish_operation()
    assert blocked
    assert "no operation" in blocked[0].lower()


# ── run_summary() hand-off (docs/plans/operation-concurrency-and-error-
# scoping.md §4) ──────────────────────────────────────────────────────────


def test_run_summary_merged_into_manifest_on_done(orchestrator, station, qtbot):
    """A declared run_summary() lands under manifest["summary"] on a "done" finish."""
    _fast_magnet(station)
    op = SimpleOperation(
        station,
        open_ended=False,
        run_summary_factory=lambda: {"level_curve": {"unix_time": [1.0], "helium_pct": [50.0]}},
    )
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)
    qtbot.waitUntil(lambda: bool(finished), timeout=2000)

    assert op.run_summary_calls == 1
    assert finished[0]["summary"] == {
        "level_curve": {"unix_time": [1.0], "helium_pct": [50.0]}
    }


def test_run_summary_absent_yields_empty_summary(orchestrator, station, qtbot):
    """An operation that never overrides run_summary() gets an empty (not missing) summary."""
    _fast_magnet(station)
    op = SimpleOperation(station, open_ended=False)
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)
    qtbot.waitUntil(lambda: bool(finished), timeout=2000)

    assert finished[0]["summary"] == {}


def test_procedure_manifest_has_empty_summary(orchestrator, station, qtbot):
    """A plain BaseProcedure-shaped run (no run_summary() at all) is unaffected."""
    proc = BlockingProcedure(station)
    orchestrator.run_procedure(proc)
    assert orchestrator._procedure is proc

    finished = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.abort_procedure()

    assert finished[0]["summary"] == {}


def test_run_summary_raising_does_not_prevent_run_finished(orchestrator, station, qtbot):
    """A run_summary() override that raises never blocks run_finished; summary is empty."""
    _fast_magnet(station)

    def _broken_summary():
        raise RuntimeError("boom")

    op = SimpleOperation(station, open_ended=False, run_summary_factory=_broken_summary)
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)
    qtbot.waitUntil(lambda: bool(finished), timeout=2000)

    assert finished[0]["status"] == "done"
    assert finished[0]["summary"] == {}


def test_run_summary_non_dict_return_yields_empty_summary(orchestrator, station, qtbot):
    """A run_summary() that returns a non-dict is treated as broken, not propagated."""
    _fast_magnet(station)
    op = SimpleOperation(station, open_ended=False, run_summary_factory=lambda: ["not", "a", "dict"])
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)
    qtbot.waitUntil(lambda: bool(finished), timeout=2000)

    assert finished[0]["summary"] == {}


def test_run_summary_merged_into_manifest_on_abort(orchestrator, station, qtbot):
    """The abort path also collects run_summary() (before self._procedure is cleared)."""
    op = SimpleOperation(
        station,
        open_ended=True,
        run_summary_factory=lambda: {"partial": True},
    )
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    finished = []
    orchestrator.run_finished.connect(finished.append)
    orchestrator.abort_procedure()

    assert finished[0]["status"] == "aborted"
    assert finished[0]["summary"] == {"partial": True}


# ── confirm_operation() (plan §8.2) ───────────────────────────────────────
# Mirrors finish_operation()'s tests exactly: confirm_operation() is the
# same duck-typed-active-operation / action_blocked pattern, generalised
# from request_finish() to confirm(key). SampleChangeOperation's own
# postcondition-gate usage of confirmed() is covered end-to-end in
# tests/test_sample_change.py; these two tests exercise the Orchestrator
# method itself against the generic SimpleOperation double.


def test_confirm_operation_calls_confirm_on_active_operation(orchestrator, station, qtbot):
    """confirm_operation() forwards the key to the active operation's confirm()."""
    op = SimpleOperation(station)
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op
    assert not op.confirmed("needle_valve")

    orchestrator.confirm_operation("needle_valve")
    assert op.confirmed("needle_valve")

    orchestrator.finish_operation()
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=2000)


def test_confirm_operation_blocked_when_no_operation_running(orchestrator, station, qtbot):
    """confirm_operation() with nothing active (or a plain procedure) is refused."""
    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    orchestrator.confirm_operation("needle_valve")
    assert blocked
    assert "no operation" in blocked[0].lower()


def test_confirm_operation_rejected_key_is_a_verdict_not_a_crash(
    orchestrator, station, qtbot
):
    """A confirm() that raises (undeclared key) yields action_blocked, never raises.

    confirm_operation() is called directly from GUI code, where an unhandled
    exception in a Qt slot would abort the whole process — the guard turns a
    rejected key into an explicit verdict instead.
    """

    class RejectingOperation(SimpleOperation):
        def confirm(self, key: str) -> None:
            raise ValueError(f"unknown confirmation key {key!r}")

    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)
    op = RejectingOperation(station)
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    orchestrator.confirm_operation("not_a_declared_key")  # must not raise
    assert blocked
    assert "not_a_declared_key" in blocked[0]

    orchestrator.finish_operation()
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=2000)


# ── Capability-scope enforcement at dispatch ──────────────────────────────


def test_measurement_scope_dispatch_rejects_operation_scope_command(station):
    """An operation-scope command in a measurement-scope batch is refused."""
    before = station.level_meter.get_refresh_rate()
    cmd = Command("level_meter", "set_refresh_rate", {"mode": 2})

    with pytest.raises(CryoSoftSafetyError):
        station.send_measurement_commands((cmd,))

    assert station.level_meter.get_refresh_rate() == before  # nothing dispatched


def test_operation_scope_dispatch_allows_operation_scope_command(station):
    """The same command dispatches fine when allowed_scope='operation'."""
    cmd = Command("level_meter", "set_refresh_rate", {"mode": 2})
    station.send_measurement_commands((cmd,), allowed_scope="operation")
    assert station.level_meter.get_refresh_rate() == 2


def test_operation_scope_command_in_operation_plan_dispatches_via_orchestrator(
    orchestrator, station, qtbot
):
    """An operation whose initiate() plan carries an operation-scope command runs fine."""
    op = SimpleOperation(
        station,
        open_ended=False,
        initiate_commands=(Command("level_meter", "set_refresh_rate", {"mode": 2}),),
    )
    orchestrator.run_operation(op)
    assert orchestrator._state != OrchestratorState.ERROR
    qtbot.wait(50)
    assert station.level_meter.get_refresh_rate() == 2
    orchestrator.abort_procedure()


def test_operation_scope_command_in_procedure_plan_is_rejected_before_dispatch(
    orchestrator, station, qtbot
):
    """The same command in a PROCEDURE's plan is refused before any dispatch."""

    class BadProcedure(BlockingProcedure):
        def initiate(self):
            return PhasePlan(
                targets={},
                commands=(Command("level_meter", "set_refresh_rate", {"mode": 2}),),
                wait_s=0.0,
            )

    before = station.level_meter.get_refresh_rate()
    orchestrator.run_procedure(BadProcedure(station))

    assert orchestrator._state == OrchestratorState.ERROR
    assert orchestrator._procedure is None
    assert station.level_meter.get_refresh_rate() == before  # nothing dispatched

    orchestrator.recover_from_error()


# ── Claims + admission gate (plan operation-concurrency-and-error-scoping.md
# §1) ─────────────────────────────────────────────────────────────────────
# HeliumFillOperation.claimed_vi_names() returns its configured level meter
# plus every magnet (the fill drives them to zero field and holds that as an
# invariant — see the class docstring), so a running fill must admit a
# manual action on any other VI (e.g. the VTI) while refusing one on the
# level meter or a magnet, naming the fill as the owner.


def test_manual_action_on_unclaimed_vi_admitted_during_helium_fill(
    orchestrator, station, tmp_path, qtbot
):
    """A manual action on a VI the fill does NOT claim (the VTI) is admitted and executes."""
    _fast_magnets(station)
    op = _make_fill(station, tmp_path)
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op
    assert orchestrator._active_claims == {"level_meter", *station.magnet_vi_names()}

    blocked: list[str] = []
    succeeded: list[tuple[str, str]] = []
    orchestrator.action_blocked.connect(blocked.append)
    orchestrator.action_succeeded.connect(lambda vi, m: succeeded.append((vi, m)))

    orchestrator.submit_vi_action("temperature_vti", "set_needle_valve", position=25.0)

    qtbot.waitUntil(lambda: bool(succeeded), timeout=2000)
    assert not blocked
    assert succeeded == [("temperature_vti", "set_needle_valve")]

    orchestrator.finish_operation()
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=5000)


def test_manual_action_on_claimed_vi_refused_during_helium_fill(
    orchestrator, station, tmp_path, qtbot
):
    """A manual action on the fill's claimed VI (the level meter) is refused, naming the fill."""
    _fast_magnets(station)
    op = _make_fill(station, tmp_path)
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("level_meter", "set_refresh_rate", mode=1)

    assert blocked
    assert "level_meter" in blocked[0]
    assert "helium fill" in blocked[0].lower()

    # A magnet is claimed too: the fill holds zero field as an invariant, so
    # a manual set_field mid-fill must be refused, not fight the fill.
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_z", "set_field", field_T=1.0)
    assert "magnet_z" in blocked[1]
    assert "helium fill" in blocked[1].lower()

    orchestrator.finish_operation()
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=5000)


def test_manual_action_refused_during_running_procedure_claim_all(
    orchestrator, station, qtbot
):
    """Regression guard: a plain procedure (default claim-all) still refuses every VI."""
    proc = BlockingProcedure(station)
    orchestrator.run_procedure(proc)
    assert orchestrator._procedure is proc
    assert orchestrator._active_claims is None  # claim-everything default

    blocked: list[str] = []
    orchestrator.action_blocked.connect(blocked.append)

    # Even a VI the procedure never touches (magnet_y) is refused: a plain
    # procedure claims everything.
    with qtbot.waitSignal(orchestrator.action_blocked, timeout=500):
        orchestrator.submit_vi_action("magnet_y", "initiate")

    assert blocked
    assert "blocking procedure" in blocked[0].lower()

    orchestrator.abort_procedure()


def test_claims_cleared_after_finish(orchestrator, station, tmp_path, qtbot):
    """Claims are cleared once the run finishes — a claimed VI becomes controllable again."""
    _fast_magnets(station)
    op = _make_fill(station, tmp_path)
    orchestrator.run_operation(op)
    assert orchestrator._active_claims == {"level_meter", *station.magnet_vi_names()}

    orchestrator.finish_operation()
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=5000)
    assert orchestrator._active_claims is None
    assert orchestrator._state == OrchestratorState.IDLE

    succeeded: list[tuple[str, str]] = []
    orchestrator.action_succeeded.connect(lambda vi, m: succeeded.append((vi, m)))
    orchestrator.submit_vi_action("level_meter", "set_refresh_rate", mode=1)
    qtbot.waitUntil(lambda: bool(succeeded), timeout=2000)
    assert succeeded == [("level_meter", "set_refresh_rate")]


def test_claims_cleared_after_abort(orchestrator, station, tmp_path, qtbot):
    """Claims are cleared on abort too — a claimed VI becomes controllable again."""
    op = _make_fill(station, tmp_path, max_fill_duration_s=3600.0)
    orchestrator.run_operation(op)
    assert orchestrator._active_claims == {"level_meter", *station.magnet_vi_names()}

    orchestrator.abort_procedure()
    assert orchestrator._active_claims is None
    assert orchestrator._state == OrchestratorState.IDLE

    succeeded: list[tuple[str, str]] = []
    orchestrator.action_succeeded.connect(lambda vi, m: succeeded.append((vi, m)))
    orchestrator.submit_vi_action("level_meter", "set_refresh_rate", mode=1)
    qtbot.waitUntil(lambda: bool(succeeded), timeout=2000)
    assert succeeded == [("level_meter", "set_refresh_rate")]


def test_stale_claimed_nonsystem_vi_fails_operation_run(
    orchestrator, station, tmp_path, qtbot
):
    """A claimed NON-system VI (the fill's level meter) going stale fails the run.

    Regression for the plan-§3 review fix: the run-failure stale check covers
    the run's system VIs PLUS its explicit claims — the level meter receives
    no system targets, but a fill must not keep "monitoring" a dead level
    meter as a mere warning (its helium_low force-trip is tolerated by the
    fill, so the safety path would not stop it either).
    """
    _fast_magnets(station)
    op = _make_fill(station, tmp_path, max_fill_duration_s=3600.0)
    finished: list[dict] = []
    orchestrator.run_finished.connect(lambda manifest: finished.append(manifest))
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    station.level_meter._driver._simulate_error = True

    qtbot.waitUntil(lambda: bool(finished), timeout=2000)
    assert finished[-1]["status"] == "failed"
    assert "level_meter" in finished[-1]["reason"]

    # With the fill gone, its helium_low toleration is gone too — the still-
    # disconnected level meter force-trips helium_low ("can't monitor it,
    # assume unsafe"), so the machine correctly proceeds to EMERGENCY on a
    # following tick rather than resting in IDLE.
    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.EMERGENCY, timeout=2000
    )

    station.level_meter._driver._simulate_error = False


def test_queued_action_drained_during_operation_gets_verdict(
    orchestrator, station, tmp_path, qtbot
):
    """A queued action is admitted/refused by the SAME predicate when the tick drains it.

    Submits both a claimed-VI action and an unclaimed-VI action while the
    fill is mid-ramp (RAMPING, before monitoring's per-tick state has
    settled) so both land on the GUI-action queue before the very next tick
    drains it — proving the drain gate (not just submit_vi_action) applies
    claims per action.
    """
    _fast_magnets(station)
    op = _make_fill(station, tmp_path)
    orchestrator.run_operation(op)
    assert orchestrator._procedure is op

    blocked: list[str] = []
    succeeded: list[tuple[str, str]] = []
    orchestrator.action_blocked.connect(blocked.append)
    orchestrator.action_succeeded.connect(lambda vi, m: succeeded.append((vi, m)))

    # Queue both directly (bypassing the timer race) so the very next tick
    # must drain them together and give each its own verdict.
    orchestrator._gui_action_queue.append(
        {"vi_name": "level_meter", "method_name": "set_refresh_rate", "kwargs": {"mode": 1}}
    )
    orchestrator._gui_action_queue.append(
        {
            "vi_name": "temperature_vti",
            "method_name": "set_needle_valve",
            "kwargs": {"position": 10.0},
        }
    )

    qtbot.waitUntil(lambda: bool(blocked) and bool(succeeded), timeout=2000)
    assert blocked and "level_meter" in blocked[0]
    assert succeeded == [("temperature_vti", "set_needle_valve")]

    orchestrator.finish_operation()
    qtbot.waitUntil(lambda: orchestrator._procedure is None, timeout=5000)


# ── OperationBase's shared recorder helper (Phase 3, docs/plans/unified-
# servicing-log-and-run-recording.md §3): _record_sample()/_recording_dict(),
# generalised from HeliumFillOperation's original single-channel curve. Unit
# tests only — no Orchestrator needed, these are plain method calls. ────────


def test_recording_dict_empty_before_any_sample(station):
    """_recording_dict() is a valid, empty shape before _record_sample() is ever called."""
    op = SimpleOperation(station)
    assert op._recording_dict() == {"unix_time": [], "channels": {}}


def test_record_sample_multi_channel_stays_consistent(station):
    """Every channel gets one value per _record_sample() call, same length, same time axis."""
    op = SimpleOperation(station)
    op._record_sample(1.0, {"magnet_z.get_field": 0.0, "temperature_vti.temperature": 300.0})
    op._record_sample(2.0, {"magnet_z.get_field": 0.1, "temperature_vti.temperature": 299.0})

    recording = op._recording_dict()
    assert recording["unix_time"] == [1.0, 2.0]
    assert recording["channels"]["magnet_z.get_field"] == [0.0, 0.1]
    assert recording["channels"]["temperature_vti.temperature"] == [300.0, 299.0]
    for series in recording["channels"].values():
        assert len(series) == len(recording["unix_time"])


def test_record_sample_rejects_channel_set_change(station):
    """A later call naming a different channel set than the first raises ValueError."""
    op = SimpleOperation(station)
    op._record_sample(1.0, {"a": 1.0, "b": 2.0})
    with pytest.raises(ValueError):
        op._record_sample(2.0, {"a": 1.0})  # missing "b"


def test_record_sample_decimates_once_bound_exceeded(station, monkeypatch):
    """Once the recording exceeds _MAX_RECORDING_POINTS, every channel halves together."""
    monkeypatch.setattr(SimpleOperation, "_MAX_RECORDING_POINTS", 4)
    op = SimpleOperation(station)

    for i in range(10):
        op._record_sample(float(i), {"a": float(i), "b": float(-i)})

    recording = op._recording_dict()
    assert len(recording["unix_time"]) <= 4
    assert len(recording["channels"]["a"]) == len(recording["unix_time"])
    assert len(recording["channels"]["b"]) == len(recording["unix_time"])
    assert op._recording_stride > 1
    assert op._recording_raw_count == 10  # every raw call is still counted


def test_recording_reset_clears_state(station):
    """_reset_recording() (called by initiate()-style setup) clears every field."""
    op = SimpleOperation(station)
    op._record_sample(1.0, {"a": 1.0})
    op._reset_recording()
    assert op._recording_dict() == {"unix_time": [], "channels": {}}
    assert op._recording_stride == 1
    assert op._recording_raw_count == 0
