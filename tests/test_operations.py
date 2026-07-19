# ---
# description: |
#   Behavior tests for the operation substrate (Phase 2, plan §4/§5/§7):
#   OperationBase driven by the real Orchestrator against a real simulated
#   Station. Covers the helium_low-tolerated vs quench safety matrix, the
#   EMERGENCY carve-out and its end-state, run_operation refusal while a
#   procedure is active, operation-before-procedure queue priority,
#   postcondition gates (hold + timeout), finish_operation()'s graceful
#   STANDBY path, and capability-scope enforcement at command dispatch.
# last_updated: 2026-07-19
# ---

import pytest

from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.core.gates import Gate
from cryosoft.core.operation import OperationBase
from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.station import build_station


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
        postcondition_timeout_s=600.0,
        open_ended=True,
    ) -> None:
        super().__init__()
        self._station = station
        self.tolerated_safety_flags = frozenset(tolerated_safety_flags)
        self._initiate_commands = tuple(initiate_commands)
        self._standby_commands = tuple(standby_commands)
        self._postcondition_gates_factory = postcondition_gates_factory
        self.postcondition_timeout_s = postcondition_timeout_s
        self._open_ended = open_ended
        self.sample_calls = 0
        self.postcondition_gates_calls = 0
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
            targets={"magnet_x": Target(0.0)},
            commands=self._initiate_commands,
            wait_s=0.0,
        )

    def step(self) -> StepPlan | None:
        if not self._open_ended:
            return None
        return StepPlan(targets={"magnet_x": Target(0.0)}, wait_s=0.0)

    def sample(self) -> None:
        self.sample_calls += 1

    def standby(self) -> PhasePlan:
        return PhasePlan(
            targets={"magnet_x": Target(0.0)}, commands=self._standby_commands, wait_s=0.0
        )

    def postcondition_gates(self):
        self.postcondition_gates_calls += 1
        if self._postcondition_gates_factory is None:
            return ()
        return self._postcondition_gates_factory()


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
        return PhasePlan(targets={"magnet_x": Target(1.0)}, commands=(), wait_s=0.0)

    def change_sweep_step(self):
        return None

    def measure(self):
        pass

    def standby(self):
        return PhasePlan(targets={"magnet_x": Target(0.0)}, commands=(), wait_s=0.0)


def _fast_magnet(station):
    """Make magnet_x ramps effectively instant."""
    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []


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

    station.magnet_x._driver._simulate_quench = True
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
    assert station.magnet_x.ramp_status() == "RAMPING"

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
    station.magnet_x._default_ramp_rate = 5.0  # keep the blocker mid-ramp
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


# ── Postcondition gates ──────────────────────────────────────────────────────


def test_postcondition_gate_holds_completion_until_satisfied(orchestrator, station, qtbot):
    """The run only reaches 'done' once every postcondition gate holds."""
    _fast_magnet(station)
    calls = {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] >= 3

    op = SimpleOperation(
        station,
        open_ended=False,
        postcondition_gates_factory=lambda: (Gate("settled", check=check),),
    )
    orchestrator.run_operation(op)

    finished = []
    orchestrator.run_finished.connect(finished.append)

    qtbot.waitUntil(lambda: op.postcondition_gates_calls >= 1, timeout=2000)
    assert orchestrator._state == OrchestratorState.STANDBY
    assert finished == []

    qtbot.waitUntil(lambda: bool(finished), timeout=2000)
    assert finished[0]["status"] == "done"
    assert orchestrator._state == OrchestratorState.IDLE


def test_postcondition_timeout_degrades_to_error_naming_the_gate(orchestrator, station, qtbot):
    """An unmet postcondition gate past its timeout degrades the run to ERROR."""
    _fast_magnet(station)

    op = SimpleOperation(
        station,
        open_ended=False,
        postcondition_gates_factory=lambda: (Gate("never_settles", check=lambda: False),),
        postcondition_timeout_s=0.05,
    )
    orchestrator.run_operation(op)

    errors: list[str] = []
    orchestrator.error_occurred.connect(errors.append)

    qtbot.waitUntil(
        lambda: orchestrator._state == OrchestratorState.ERROR, timeout=3000
    )
    assert any("never_settles" in e for e in errors)
    assert orchestrator._procedure is None

    orchestrator.recover_from_error()
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
