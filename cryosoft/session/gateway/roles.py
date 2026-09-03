"""The gateway's permission model: who may take which class of action.

**The standard.** Every connection through the **Agent gateway** declares a
``Role``; every action it can take has an ``ActionClass`` (see
``action_classes.py``); and exactly one table below — ``PERMISSION_MATRIX``
— says which pairs are permitted. Nothing else in the gateway decides
authority: a new command, a new capability or a new role is granted by
adding a row to a table, never by writing a branch.

The matrix:

===============  ==========  ====================  =========  ===============
Action class     observer    debug                 session    operator (human)
===============  ==========  ====================  =========  ===============
read             permitted   permitted             permitted  permitted
recovery         refused     unattended only       permitted  permitted
run_control      refused     refused               permitted  permitted
envelope         refused     refused               refused    permitted
===============  ==========  ====================  =========  ===============

Four properties of that table are the design, not incidental:

* **The human column is not in it.** ``authorize()`` returns ``None`` for
  any actor that is not an ``agent``: the operator's authority comes from
  standing at the cryostat, and a permission model that could refuse them
  would be a hazard rather than a safeguard. The engine's own admission
  rules, the control-validation standard's limits and the session envelope
  still bind the human exactly as before — this layer adds nothing to them.
* **Envelope is nobody's.** The session envelope, **Attendance** and the
  **Kill switch** are the rules the other three rows are judged by. An
  agent that could widen the envelope it is bounded by, declare itself
  unattended, or reopen its own gate would not be bounded, attended or
  gated at all.
* **Recovery is attendance-dependent for ``debug`` alone.** With a human
  present a debug agent diagnoses and REPORTS; the human decides. Enforced
  here from the value ``Orchestrator.set_attendance()`` published, never
  left to an agent's self-restraint.
* **Emergency standby is outside the table.** ``authorize()`` permits it to
  every role, in every state, at every kill-switch setting, before the
  matrix is consulted. An actor that can see a problem must never be unable
  to make the station safe.

The **kill switch** is checked before the matrix and can only ever subtract:
``read_only`` leaves an agent nothing but ``read``-class actions, ``revoked``
leaves it nothing at all. It is enforced a second time inside the engine
(``Orchestrator.submit()``), which is the authority — this check is the front
door, so that an agent gets a specific refusal rather than a generic one.
"""

from __future__ import annotations

import logging
from enum import Enum

from cryosoft.core.events import (
    Actor,
    ActorKind,
    AgentGate,
    Command,
    CommandName,
    StationInfo,
    Verdict,
    VerdictCode,
)
from cryosoft.session.gateway.action_classes import (
    ActionClass,
    UnclassifiedActionError,
    classify_command,
)

logger = logging.getLogger(__name__)


class Role(str, Enum):
    """The authority a gateway connection declares for itself.

    Written into ``Actor.role`` on every command the connection submits, so
    a refusal, a run record and the action feed all name the authority the
    action was taken under. A ``str`` enum so the value is JSON-safe as it
    stands.

    Members:
        OBSERVER: Reads the system and changes nothing.
        DEBUG: Reads, and — only while the experiment is UNATTENDED — takes
            recovery actions to keep a run alive.
        SESSION: Runs the experiment: recovery and run control both, within
            the session envelope the human set.
    """

    OBSERVER = "observer"
    DEBUG = "debug"
    SESSION = "session"


class Permission(str, Enum):
    """One cell of ``PERMISSION_MATRIX``.

    Members:
        PERMITTED: The role may take actions of this class.
        UNATTENDED_ONLY: Permitted only while **Attendance** is ``False``;
            with a human present the agent reports instead of acting.
        REFUSED: The role may never take actions of this class.
    """

    PERMITTED = "permitted"
    UNATTENDED_ONLY = "unattended_only"
    REFUSED = "refused"


#: The permission model, as one table. Read it as
#: ``PERMISSION_MATRIX[action_class][role]``; every (class, role) pair has a
#: cell, which conformance asserts, so authority is never absent by omission.
PERMISSION_MATRIX: dict[ActionClass, dict[Role, Permission]] = {
    ActionClass.READ: {
        Role.OBSERVER: Permission.PERMITTED,
        Role.DEBUG: Permission.PERMITTED,
        Role.SESSION: Permission.PERMITTED,
    },
    ActionClass.RECOVERY: {
        Role.OBSERVER: Permission.REFUSED,
        Role.DEBUG: Permission.UNATTENDED_ONLY,
        Role.SESSION: Permission.PERMITTED,
    },
    ActionClass.RUN_CONTROL: {
        Role.OBSERVER: Permission.REFUSED,
        Role.DEBUG: Permission.REFUSED,
        Role.SESSION: Permission.PERMITTED,
    },
    ActionClass.ENVELOPE: {
        Role.OBSERVER: Permission.REFUSED,
        Role.DEBUG: Permission.REFUSED,
        Role.SESSION: Permission.REFUSED,
    },
}


def _refusal(
    command: Command,
    actor: Actor,
    reason: str,
    detail: dict[str, object],
    seq: int,
) -> Verdict:
    """Build the one refusal shape this module ever emits.

    Args:
        command: The command being refused.
        actor: Who asked.
        reason: The human-readable explanation, for a banner or a log.
        detail: The structured explanation a client decides from.
        seq: Sequence number to stamp on the verdict.

    Returns:
        A ``BLOCKED_ROLE`` verdict answering *command*.
    """
    logger.info("Gateway refusal: %s", reason)
    return Verdict(
        request_id=command.request_id,
        command=command.name,
        code=VerdictCode.BLOCKED_ROLE,
        actor=actor,
        reason=reason,
        detail=dict(detail),
        seq=seq,
    )


def authorize(
    actor: Actor,
    command: Command,
    station_info: StationInfo,
    attendance: bool,
    kill_switch: AgentGate,
    *,
    seq: int = 0,
) -> Verdict | None:
    """Decide whether *actor* may submit *command*, per the standard above.

    The checks run in this order, and the order is the model: emergency
    standby first (always permitted), then the actor kind (only agents are
    judged), then the role's own validity, then the action's class, then the
    kill switch, and only then the matrix. Every refusal is a
    ``BLOCKED_ROLE`` verdict whose ``detail`` names the rule that refused, so
    a client decides from the code and the dict and never by parsing prose.

    Args:
        actor: Who is asking, carrying the declared ``Role`` in ``actor.role``.
        command: The command they want to submit.
        station_info: The station's declaration snapshot, from which
            ``submit_vi_action``'s target capability is classified.
        attendance: Whether a human is watching (the value
            ``Orchestrator.set_attendance()`` published).
        kill_switch: The gate the human set (``Orchestrator.set_agent_gate()``).
        seq: Sequence number to stamp on a refusal verdict.

    Returns:
        ``None`` when the command is permitted and may be forwarded to the
        engine, or the ``BLOCKED_ROLE`` ``Verdict`` that refuses it.
    """
    if command.name is CommandName.EMERGENCY_STANDBY:
        return None
    if actor.kind is not ActorKind.AGENT:
        return None

    try:
        role = Role(actor.role)
    except ValueError:
        return _refusal(
            command,
            actor,
            f"Agent {actor.id!r} declares the unknown role {actor.role!r}; "
            f"the roles that exist are {[member.value for member in Role]}.",
            {"rule": "unknown_role", "role": actor.role},
            seq,
        )

    try:
        classified = classify_command(command, station_info)
    except UnclassifiedActionError as error:
        return _refusal(
            command,
            actor,
            f"Refused: {error}. An action with no declared class is refused "
            f"rather than guessed at.",
            {"rule": "unclassified_action", "role": role.value},
            seq,
        )

    action_class = classified.action_class
    detail: dict[str, object] = {
        "role": role.value,
        "action_class": action_class.value,
        "rationale": classified.rationale,
    }

    if kill_switch is AgentGate.REVOKED or (
        kill_switch is AgentGate.READ_ONLY and action_class is not ActionClass.READ
    ):
        return _refusal(
            command,
            actor,
            f"Agent access is {kill_switch.value}: {command.name.value!r} is "
            f"a {action_class.value} action and is refused. Only emergency "
            f"standby passes while the kill switch is closed.",
            {**detail, "rule": "kill_switch", "gate": kill_switch.value},
            seq,
        )

    permission = PERMISSION_MATRIX[action_class][role]
    if permission is Permission.PERMITTED:
        return None
    if permission is Permission.UNATTENDED_ONLY:
        if not attendance:
            return None
        return _refusal(
            command,
            actor,
            f"The {role.value!r} role may take {action_class.value} actions "
            f"only while the experiment is unattended; a human is watching, "
            f"so {command.name.value!r} is refused — report it instead.",
            {**detail, "rule": "attendance", "attended": True},
            seq,
        )
    return _refusal(
        command,
        actor,
        f"The {role.value!r} role does not grant {action_class.value} "
        f"actions, so {command.name.value!r} is refused.",
        {**detail, "rule": "role_matrix"},
        seq,
    )


#: The order the three cells stand in, from "grants nothing" to "grants
#: everything". Only ever used to compare two ROLES cell by cell — see
#: ``role_within_ceiling()``; the matrix itself is read by identity, never by
#: rank, so a cell added later has to declare its place here deliberately.
_PERMISSION_RANK: dict[Permission, int] = {
    Permission.REFUSED: 0,
    Permission.UNATTENDED_ONLY: 1,
    Permission.PERMITTED: 2,
}


def role_within_ceiling(role: Role, ceiling: Role) -> bool:
    """Answer whether *role* grants no more authority than *ceiling* does.

    **The ceiling standard.** A deployment that opens a connection point to
    agents (the **Gateway server**) sets one role as the most authority it
    will hand out, and a connection asking for more is refused at the
    handshake rather than at its first write. What "more" means is read off
    ``PERMISSION_MATRIX`` cell by cell — a role is within the ceiling when
    for EVERY action class its cell grants no more than the ceiling's does —
    so the ceiling follows the table automatically and no second ordering of
    the roles has to be maintained beside it.

    Args:
        role: The role a connection declares for itself.
        ceiling: The most authority the deployment permits.

    Returns:
        ``True`` when *role* may be granted under *ceiling*.
    """
    return all(
        _PERMISSION_RANK[row[role]] <= _PERMISSION_RANK[row[ceiling]]
        for row in PERMISSION_MATRIX.values()
    )
