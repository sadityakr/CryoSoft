"""The agent gateway's permission model (L6).

Two halves, tested against a real simulated station's declaration snapshot
rather than a hand-built one, so a classification that names a capability the
station does not have fails here rather than in front of an agent:

* the classification (`action_classes.py`) — which class an action IS;
* the matrix (`roles.py`) — who may take an action of that class.
"""

from __future__ import annotations

import pytest

from cryosoft.core import events as ev
from cryosoft.core.station import build_station
from cryosoft.session.gateway import (
    ActionClass,
    Permission,
    Role,
    UnclassifiedActionError,
    authorize,
    classify_command,
)

OBSERVER = ev.Actor(kind=ev.ActorKind.AGENT, id="watcher", role=Role.OBSERVER.value)
DEBUG = ev.Actor(kind=ev.ActorKind.AGENT, id="fixer", role=Role.DEBUG.value)
SESSION = ev.Actor(kind=ev.ActorKind.AGENT, id="runner", role=Role.SESSION.value)


@pytest.fixture(scope="module")
def station_info():
    """The declaration snapshot of a real simulated station."""
    return build_station("cryosoft/configs/sim_cryostat").station_info()


def _command(name, actor=SESSION, **args):
    return ev.Command(name=name, actor=actor, args=args)


def _authorize(command, station_info, *, attended=True, gate=ev.AgentGate.ACTIVE):
    return authorize(command.actor, command, station_info, attended, gate)


# ── Classification ────────────────────────────────────────────────────────


def test_a_command_is_classified_from_the_table(station_info):
    """Every command but submit_vi_action reads its class straight off a row."""
    assert (
        classify_command(_command(ev.CommandName.RUN_PROCEDURE), station_info).action_class
        is ActionClass.RUN_CONTROL
    )
    assert (
        classify_command(_command(ev.CommandName.PAUSE_PROCEDURE), station_info).action_class
        is ActionClass.RECOVERY
    )
    assert (
        classify_command(
            _command(ev.CommandName.SET_EXPERIMENT_ENVELOPE), station_info
        ).action_class
        is ActionClass.ENVELOPE
    )


def test_a_vi_action_is_classified_from_its_target(station_info):
    """The one command whose class depends on what it points at."""
    ramp = _command(
        ev.CommandName.SUBMIT_VI_ACTION,
        vi_name="magnet_z",
        method_name="set_field",
        target_T=0.1,
    )
    assert classify_command(ramp, station_info).action_class is ActionClass.RUN_CONTROL

    lifecycle = _command(
        ev.CommandName.SUBMIT_VI_ACTION, vi_name="magnet_z", method_name="standby"
    )
    assert classify_command(lifecycle, station_info).action_class is ActionClass.RECOVERY


def test_an_unclassified_capability_is_refused_by_name(station_info):
    """No silent default: the refusal names the capability that has no row."""
    command = _command(
        ev.CommandName.SUBMIT_VI_ACTION,
        vi_name="magnet_z",
        method_name="no_such_capability",
    )
    with pytest.raises(UnclassifiedActionError, match="no_such_capability"):
        classify_command(command, station_info)

    verdict = _authorize(command, station_info)
    assert verdict is not None
    assert verdict.code is ev.VerdictCode.BLOCKED_ROLE
    assert "no_such_capability" in verdict.reason
    assert verdict.detail["rule"] == "unclassified_action"


def test_an_unconfigured_instrument_is_refused_by_name(station_info):
    """An action aimed at an instrument this station does not have."""
    command = _command(
        ev.CommandName.SUBMIT_VI_ACTION, vi_name="ghost", method_name="set_field"
    )
    with pytest.raises(UnclassifiedActionError, match="ghost"):
        classify_command(command, station_info)


# ── The matrix ────────────────────────────────────────────────────────────


def test_observer_is_refused_run_control_with_a_structured_reason(station_info):
    """The lowest role reads and nothing else; the refusal is machine-readable."""
    command = _command(ev.CommandName.RUN_PROCEDURE, actor=OBSERVER)

    verdict = _authorize(command, station_info)

    assert verdict is not None
    assert verdict.code is ev.VerdictCode.BLOCKED_ROLE
    assert verdict.actor == OBSERVER
    assert verdict.request_id == command.request_id
    assert verdict.detail["rule"] == "role_matrix"
    assert verdict.detail["role"] == "observer"
    assert verdict.detail["action_class"] == "run_control"
    assert verdict.detail["rationale"]
    assert not verdict.ok


def test_observer_may_read(station_info):
    """A read-class capability is permitted to every role."""
    command = _command(
        ev.CommandName.SUBMIT_VI_ACTION,
        actor=OBSERVER,
        vi_name="keithley_dc_mode",
        method_name="read_now",
    )

    assert _authorize(command, station_info) is None


def test_debug_takes_recovery_only_while_unattended(station_info):
    """The attendance rule: with a human present the agent reports instead."""
    command = _command(ev.CommandName.PAUSE_PROCEDURE, actor=DEBUG)

    refused = _authorize(command, station_info, attended=True)
    assert refused is not None
    assert refused.code is ev.VerdictCode.BLOCKED_ROLE
    assert refused.detail["rule"] == "attendance"
    assert refused.detail["attended"] is True
    assert "unattended" in refused.reason

    assert _authorize(command, station_info, attended=False) is None


def test_debug_is_refused_run_control_even_unattended(station_info):
    """Attendance widens recovery only — it never grants run control."""
    command = _command(ev.CommandName.ABORT_PROCEDURE, actor=DEBUG)

    verdict = _authorize(command, station_info, attended=False)

    assert verdict is not None
    assert verdict.detail["rule"] == "role_matrix"
    assert verdict.detail["action_class"] == "run_control"


def test_session_runs_the_experiment_but_never_changes_the_rules(station_info):
    """Run control yes; the envelope, attendance and the gate are the human's."""
    assert _authorize(_command(ev.CommandName.RUN_PROCEDURE), station_info) is None
    assert (
        _authorize(_command(ev.CommandName.PAUSE_PROCEDURE), station_info) is None
    )

    for name in (
        ev.CommandName.SET_EXPERIMENT_ENVELOPE,
        ev.CommandName.SET_ATTENDANCE,
        ev.CommandName.SET_AGENT_GATE,
    ):
        verdict = _authorize(_command(name), station_info)
        assert verdict is not None, f"{name.value} must be refused to every role"
        assert verdict.detail["action_class"] == "envelope"
        assert verdict.detail["rule"] == "role_matrix"


def test_an_unknown_role_is_refused_by_name(station_info):
    """A role that is not in the enum grants nothing, rather than defaulting."""
    stranger = ev.Actor(kind=ev.ActorKind.AGENT, id="x", role="superuser")
    command = _command(ev.CommandName.PAUSE_PROCEDURE, actor=stranger)

    verdict = _authorize(command, station_info)

    assert verdict is not None
    assert verdict.detail == {"rule": "unknown_role", "role": "superuser"}


# ── The kill switch ───────────────────────────────────────────────────────


def test_a_revoked_gate_refuses_even_the_session_role(station_info):
    """The gate subtracts; it can never be talked round by a role."""
    command = _command(ev.CommandName.RUN_PROCEDURE)

    verdict = _authorize(command, station_info, gate=ev.AgentGate.REVOKED)

    assert verdict is not None
    assert verdict.detail["rule"] == "kill_switch"
    assert verdict.detail["gate"] == "revoked"


def test_a_read_only_gate_leaves_reads_alone(station_info):
    """read_only is the middle rung: observe, but write nothing."""
    read = _command(
        ev.CommandName.SUBMIT_VI_ACTION,
        vi_name="keithley_dc_mode",
        method_name="read_now",
    )
    write = _command(ev.CommandName.PAUSE_PROCEDURE)

    assert _authorize(read, station_info, gate=ev.AgentGate.READ_ONLY) is None
    refused = _authorize(write, station_info, gate=ev.AgentGate.READ_ONLY)
    assert refused is not None
    assert refused.detail["gate"] == "read_only"

    assert _authorize(read, station_info, gate=ev.AgentGate.REVOKED) is not None


def test_the_human_path_is_never_judged_here(station_info):
    """An operator or system actor passes every check, at every gate setting."""
    for actor in (ev.OPERATOR, ev.Actor(kind=ev.ActorKind.SYSTEM, id="tick")):
        for gate in ev.AgentGate:
            command = _command(ev.CommandName.SET_AGENT_GATE, actor=actor)
            assert authorize(actor, command, station_info, True, gate) is None


def test_emergency_standby_is_permitted_to_every_role_at_every_gate(station_info):
    """Nobody who can see a problem may be unable to make the station safe."""
    for actor in (OBSERVER, DEBUG, SESSION):
        for gate in ev.AgentGate:
            for attended in (True, False):
                command = _command(
                    ev.CommandName.EMERGENCY_STANDBY,
                    actor=actor,
                    reason="coil voltage climbing",
                )
                assert (
                    authorize(actor, command, station_info, attended, gate) is None
                ), f"{actor.role} refused emergency standby at gate {gate.value}"


def test_the_matrix_is_the_only_thing_that_decides(station_info):
    """Spot-check that the table drives the answer, cell by cell."""
    from cryosoft.session.gateway import PERMISSION_MATRIX

    assert PERMISSION_MATRIX[ActionClass.RECOVERY][Role.DEBUG] is (
        Permission.UNATTENDED_ONLY
    )
    assert PERMISSION_MATRIX[ActionClass.ENVELOPE][Role.SESSION] is Permission.REFUSED
    assert PERMISSION_MATRIX[ActionClass.READ][Role.OBSERVER] is Permission.PERMITTED
