"""What each action a client can take actually DOES, as one declarative table.

The permission matrix in ``roles.py`` decides who may take an action of a
given class. This module decides which class an action IS — and it does so
in tables, not in code, because the classification is a physics judgement
about a particular instrument rack rather than a rule that can be derived
from a method signature.

**PROVISIONAL — to be confirmed by the physicist.** Every rationale below is
a first pass written from the VI docstrings. The recovery-versus-run-control
line in particular is a decision that belongs to whoever owns the cryostat,
not to whoever wrote the gateway, and it is expected to move. Nothing here
is load-bearing for safety on its own — the Orchestrator's own admission
rules, the control-validation standard's limits and the session envelope all
still bind every writer regardless of what this table says. What this table
decides is how much autonomy an agent is granted BEFORE those checks run.

**Three tables, one rule each.**

* ``COMMAND_ACTION_CLASSES`` — one row per ``CommandName``, the engine's own
  command surface.
* ``CONTROL_ACTION_CLASSES`` — one row per ``(VI kind, @control name)``, for
  the one command whose class depends on its target, ``submit_vi_action``.
  The key's first half is ``InstrumentInfo.kind``, the VI CLASS's ``vi_type``
  (``magnet``, ``temperature``, ``level``, ``rotator``, ``measurement``,
  ``switch``), so a row is written once per capability rather than once per
  configured instrument.
* ``LIFECYCLE_ACTION_CLASSES`` — ``initiate`` / ``standby``, the two
  non-``@control`` methods the direct action path admits, for every VI kind.

**The default rule the rows were derived from** (stated so a reviewer can
see where judgement overrode it, and so a new row has somewhere to start):

1. A ``@control`` whose **capability scope** is ``operation``, and each
   lifecycle action, is ``recovery`` — instrument housekeeping a debug agent
   may do alone to keep a run alive.
2. Anything that sets a setpoint, ramps, arms a measurement or sources
   current is ``run_control``.
3. Anything that only reads is ``read``.

Four rows deliberately DEVIATE from rule 1, and say so in their rationale:
the magnet's persistent-mode and switch-heater capabilities are
operation-scope but command the largest stored energy on the station, which
is run control however the decorator is spelled.

**No silent default.** An action with no row is not guessed at: it is
refused, by name, with a reason saying the classification is missing.
A conformance test asserts every control every shipped config declares has a
row, so the refusal is a bug report about a table that was not updated, never
the normal path.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from cryosoft.core.events import Command, CommandName, StationInfo

logger = logging.getLogger(__name__)


class ActionClass(str, Enum):
    """How much authority an action needs, independent of who is asking.

    The four classes of the gateway's permission matrix (``roles.py``). A
    ``str`` enum so the value is JSON-safe as it stands and travels in a
    refusal's ``detail`` unchanged.

    Members:
        READ: Observes the system and changes nothing.
        RECOVERY: Instrument housekeeping that keeps a run alive — the
            framework's "pause/resume, VI initiate/standby, re-send config,
            adjust waits".
        RUN_CONTROL: Starts, stops, or redirects the measurement itself, or
            commands energy into the cryostat.
        ENVELOPE: Changes the rules the other three are judged by — the
            session envelope, attendance, and the kill switch. Reserved to
            the human.
    """

    READ = "read"
    RECOVERY = "recovery"
    RUN_CONTROL = "run_control"
    ENVELOPE = "envelope"


@dataclass(frozen=True)
class ClassifiedAction:
    """One row of a classification table: the class, and why.

    The rationale is not decoration. It is what a physicist reviews when
    confirming this provisional table, and what a refusal quotes back to the
    agent that was refused.

    Attributes:
        action_class: Which class this action belongs to.
        rationale: One line saying why, in the reviewer's terms.
    """

    action_class: ActionClass
    rationale: str


class UnclassifiedActionError(ValueError):
    """An action reached the gateway with no row in any classification table.

    Raised by ``classify_command()`` rather than defaulted, so that a table
    somebody forgot to update refuses the agent by name instead of silently
    granting or withholding authority. ``roles.authorize()`` turns it into a
    ``BLOCKED_ROLE`` verdict carrying this message.
    """


# ── The engine's command surface ──────────────────────────────────────
#
# One row per CommandName; conformance diffs the two, so a command added to
# the contract cannot reach an agent unclassified. SUBMIT_VI_ACTION is the
# one command whose class is not fixed here — it depends on the capability it
# targets, so it is resolved through CONTROL_ACTION_CLASSES below.

COMMAND_ACTION_CLASSES: dict[CommandName, ClassifiedAction] = {
    # ── Runs and the queue ──
    CommandName.RUN_PROCEDURE: ClassifiedAction(
        ActionClass.RUN_CONTROL, "Starts a measurement on the mounted sample."
    ),
    CommandName.RUN_OPERATION: ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Starts an operation — cryogen handling and sample access, the "
        "physical procedures with a person at the cryostat.",
    ),
    CommandName.QUEUE_PROCEDURE: ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Commits the station to a measurement it will start unattended.",
    ),
    CommandName.QUEUE_OPERATION: ClassifiedAction(
        ActionClass.RUN_CONTROL, "As queue_procedure, for an operation."
    ),
    CommandName.RUN_QUEUE: ClassifiedAction(
        ActionClass.RUN_CONTROL, "Pulls the next queued run and starts it."
    ),
    CommandName.PAUSE_PROCEDURE: ClassifiedAction(
        ActionClass.RECOVERY,
        "Holds the hardware at the pause boundary without ending the run — "
        "the framework's canonical recovery action.",
    ),
    CommandName.RESUME_PROCEDURE: ClassifiedAction(
        ActionClass.RECOVERY, "The inverse of pause; the run it resumes is its own."
    ),
    CommandName.ABORT_PROCEDURE: ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Ends the run and discards what it would still have measured; "
        "recovery keeps a run alive rather than ending it.",
    ),
    # ── Operation steps ──
    CommandName.CONFIRM_OPERATION: ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Asserts that a physical step at the cryostat was done. An agent "
        "self-confirming a step nobody performed is the accountability case "
        "this whole layer exists for.",
    ),
    CommandName.SKIP_OPERATION_STEP: ClassifiedAction(
        ActionClass.RUN_CONTROL, "Declares a physical step unnecessary."
    ),
    CommandName.FINISH_OPERATION: ClassifiedAction(
        ActionClass.RUN_CONTROL, "Ends the operation and records its outcome."
    ),
    # ── Instrument actions ──
    CommandName.SUBMIT_GLOBAL_ACTION: ClassifiedAction(
        ActionClass.RECOVERY,
        "Fans out to the lifecycle actions (initiate_all / standby_all), "
        "which are recovery. PROVISIONAL: standby_all aborts an active run "
        "first, so it is the widest recovery action there is.",
    ),
    CommandName.STOP_RAMP: ClassifiedAction(
        ActionClass.RECOVERY,
        "Stops motion and holds where it is — always the direction of safety.",
    ),
    CommandName.CONNECT_INSTRUMENT: ClassifiedAction(
        ActionClass.RECOVERY,
        "Re-establishes communication with a dropped instrument: the "
        "archetypal unattended recovery.",
    ),
    CommandName.DISCONNECT_INSTRUMENT: ClassifiedAction(
        ActionClass.RECOVERY, "Its inverse — releases the bus, commands nothing."
    ),
    # ── Faults, safety and recovery ──
    CommandName.EMERGENCY_STANDBY: ClassifiedAction(
        ActionClass.RECOVERY,
        "Classified for the record only: authorize() exempts it before the "
        "matrix is consulted, so it is permitted to every role in every "
        "state and at every kill-switch setting.",
    ),
    CommandName.ACKNOWLEDGE: ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "PROVISIONAL: it grants a time-boxed manual override OVER a safety "
        "hold, which is authority over the interlocks rather than gentle "
        "recovery. The physicist may want to split the EMERGENCY "
        "acknowledge from the hold unlock.",
    ),
    CommandName.ACKNOWLEDGE_FAULT: ClassifiedAction(
        ActionClass.RECOVERY,
        "Clears a latched instrument fault record; commands no hardware.",
    ),
    CommandName.RETRY_FAULT: ClassifiedAction(
        ActionClass.RECOVERY, "Retries the communication path to a faulted instrument."
    ),
    CommandName.RECOVER_FROM_ERROR: ClassifiedAction(
        ActionClass.RECOVERY, "Returns the state machine to IDLE after an error."
    ),
    # ── Monitoring and policy ──
    CommandName.START_MONITORING: ClassifiedAction(
        ActionClass.RECOVERY,
        "Starts the polling half of the tick. It reads instruments rather "
        "than driving them, but it changes the engine's operating mode, so "
        "it is not a read.",
    ),
    CommandName.STOP_MONITORING: ClassifiedAction(
        ActionClass.RECOVERY,
        "Its inverse — stops polling and blinds every trend check with it.",
    ),
    CommandName.SET_SCANNER_ENABLED: ClassifiedAction(
        ActionClass.RECOVERY,
        "A procedure-availability policy toggle; writes to no instrument.",
    ),
    CommandName.SET_EXPERIMENT_ENVELOPE: ClassifiedAction(
        ActionClass.ENVELOPE,
        "Sets the sample's own safety bounds. The human mounts the sample "
        "and the human says what it may see.",
    ),
    CommandName.SET_ATTENDANCE: ClassifiedAction(
        ActionClass.ENVELOPE,
        "Declares whether a human is watching — an input to this very "
        "matrix, so no agent may set it about itself.",
    ),
    CommandName.SET_AGENT_GATE: ClassifiedAction(
        ActionClass.ENVELOPE,
        "The kill switch. An agent that could reopen its own gate would not "
        "be gated at all.",
    ),
}


# ── Per-VI-kind capabilities: the physicist's pass ────────────────────
#
# Keyed by (InstrumentInfo.kind, @control name). Every control every shipped
# config's manifest declares has a row, asserted by conformance.

CONTROL_ACTION_CLASSES: dict[tuple[str, str], ClassifiedAction] = {
    # ── level: cryogen level meters ──
    ("level", "set_refresh_rate"): ClassifiedAction(
        ActionClass.RECOVERY,
        "Operation-scope meter housekeeping: how often the level is sampled, "
        "never anything about the cryostat's own state.",
    ),
    # ── magnet: superconducting magnets ──
    ("magnet", "set_field"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Ramps the magnet to a new field — the archetypal setpoint, and the "
        "largest stored energy on the station.",
    ),
    ("magnet", "enable_persistent_mode"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "DEVIATES from the operation-scope default rule: it drives the "
        "switch heater and ramps the leads, which is control over the "
        "magnet's energy path, not housekeeping.",
    ),
    ("magnet", "disable_persistent_mode"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "DEVIATES from the operation-scope default rule, as its inverse "
        "does: re-energising the leads to the coil's trapped current.",
    ),
    ("magnet", "switch_heater_on"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "DEVIATES from the operation-scope default rule: a mistimed switch "
        "heater command across a PSU/coil mismatch is a quench, which is "
        "the opposite of a recovery action.",
    ),
    ("magnet", "switch_heater_off"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "DEVIATES from the operation-scope default rule, for the same reason "
        "as switch_heater_on: it commits the coil to a persistent state.",
    ),
    # ── measurement: source/measure electronics ──
    ("measurement", "initiate_measurement"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Arms the instrument and, in DC and delta modes, begins sourcing "
        "current into the sample.",
    ),
    ("measurement", "read_now"): ClassifiedAction(
        ActionClass.READ,
        "Takes one datapoint from an already-armed instrument and caches it "
        "for display; changes no setting and sources nothing new.",
    ),
    ("measurement", "set_dc_current"): ClassifiedAction(
        ActionClass.RUN_CONTROL, "Sets the current sourced through the sample."
    ),
    ("measurement", "set_delta_current"): ClassifiedAction(
        ActionClass.RUN_CONTROL, "As set_dc_current, for delta mode."
    ),
    ("measurement", "set_source_current"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "As set_dc_current, for a separate source/voltmeter pair.",
    ),
    # ── rotator: sample rotation stages ──
    ("rotator", "set_sample_angle"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Starts a ramp that physically moves the sample in the field.",
    ),
    ("rotator", "set_rate_sample_angle"): ClassifiedAction(
        ActionClass.RECOVERY,
        "Sets how fast a later rotation goes, never where it goes — the "
        "framework's 'adjust waits' recovery action.",
    ),
    # ── switch: scanner / switch matrices ──
    ("switch", "select_route"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Chooses which contacts the measurement flows through — it redefines "
        "what is being measured.",
    ),
    ("switch", "close_channel"): ClassifiedAction(
        ActionClass.RUN_CONTROL, "As select_route, addressed by raw channel."
    ),
    ("switch", "open_channel"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Breaks the path the run is measuring through. It de-energises, but "
        "it does not keep the run alive — it ends its signal.",
    ),
    ("switch", "open_all"): ClassifiedAction(
        ActionClass.RUN_CONTROL, "As open_channel, for every channel at once."
    ),
    ("switch", "set_hot_switching"): ClassifiedAction(
        ActionClass.RECOVERY,
        "A make-before-break / break-before-make policy setting: it selects "
        "no route and energises nothing.",
    ),
    ("switch", "set_pole_mode"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Opens every channel and renumbers them, silently invalidating the "
        "whole configured route table.",
    ),
    # ── temperature: VTI and sample temperature controllers ──
    ("temperature", "set_temperature"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Ramps the sample or VTI to a new temperature — a setpoint.",
    ),
    ("temperature", "set_ramp_rate"): ClassifiedAction(
        ActionClass.RECOVERY,
        "How fast a later ramp approaches its setpoint, never the setpoint "
        "itself — the framework's 'adjust waits' recovery action.",
    ),
    ("temperature", "set_pid"): ClassifiedAction(
        ActionClass.RECOVERY,
        "Retunes the closed loop to settle an oscillation; commands no new "
        "setpoint. The framework's 're-send config'.",
    ),
    ("temperature", "set_heater_mode"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "AUTO to MANUAL removes the closed loop from the sample's "
        "temperature and leaves it under open-loop power.",
    ),
    ("temperature", "set_heater_output"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Open-loop heater power straight into the sample space.",
    ),
    ("temperature", "set_heater_range"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Selects the decade of heater power, OFF included — it switches the "
        "heater on and off.",
    ),
    ("temperature", "set_needle_valve"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "Commands helium flow through the VTI: the cryostat's temperature "
        "and its cryogen consumption at once.",
    ),
    ("temperature", "set_needle_valve_mode"): ClassifiedAction(
        ActionClass.RUN_CONTROL,
        "AUTO to MANUAL takes the instrument's own gas-flow loop out of the "
        "circuit, leaving the valve wherever it was.",
    ),
    ("temperature", "set_curve"): ClassifiedAction(
        ActionClass.RECOVERY,
        "PROVISIONAL: assigning a calibration curve changes how the sensor "
        "is READ, which is 're-send config' — but the closed loop then "
        "interprets its setpoint through it, so the physicist may move this.",
    ),
}


# ── Lifecycle actions: the same two on every VI ───────────────────────

LIFECYCLE_ACTION_CLASSES: dict[str, ClassifiedAction] = {
    "initiate": ClassifiedAction(
        ActionClass.RECOVERY,
        "Re-sends an instrument's configured operating state — named "
        "verbatim as a recovery action by the permission model.",
    ),
    "standby": ClassifiedAction(
        ActionClass.RECOVERY,
        "Drives one instrument to its safe idle state; the direction of "
        "safety, and named verbatim as a recovery action.",
    ),
}


def _instrument_kind(station_info: StationInfo, vi_name: str) -> str:
    """Return the VI CLASS kind of one configured instrument.

    Args:
        station_info: The station's declaration snapshot.
        vi_name: The instrument the action targets.

    Returns:
        ``InstrumentInfo.kind`` — the VI class's ``vi_type``.

    Raises:
        UnclassifiedActionError: If no configured instrument has that name.
    """
    for instrument in station_info.instruments:
        if instrument.name == vi_name:
            return instrument.kind
    raise UnclassifiedActionError(
        f"no instrument named {vi_name!r} is configured on this station, so "
        f"the action cannot be classified"
    )


def classify_control(station_info: StationInfo, vi_name: str, method_name: str) -> ClassifiedAction:
    """Classify one capability of one configured instrument.

    Args:
        station_info: The station's declaration snapshot, which is where the
            instrument's ``kind`` comes from.
        vi_name: The instrument the action targets.
        method_name: The ``@control`` or lifecycle action being called.

    Returns:
        The table's row for it, class and rationale.

    Raises:
        UnclassifiedActionError: If the instrument is not configured, or the
            capability has no row — never a guessed default.
    """
    if method_name in LIFECYCLE_ACTION_CLASSES:
        return LIFECYCLE_ACTION_CLASSES[method_name]
    kind = _instrument_kind(station_info, vi_name)
    classified = CONTROL_ACTION_CLASSES.get((kind, method_name))
    if classified is None:
        raise UnclassifiedActionError(
            f"{vi_name}.{method_name}() (VI kind {kind!r}) has no row in the "
            f"gateway's action-class table, so no role can be granted it"
        )
    return classified


def classify_command(command: Command, station_info: StationInfo) -> ClassifiedAction:
    """Classify one submitted command into its action class.

    Every ``CommandName`` is classified by ``COMMAND_ACTION_CLASSES``, except
    ``SUBMIT_VI_ACTION``, whose class depends on the capability it targets
    and is resolved through ``classify_control()``.

    Args:
        command: The command a client wants to submit.
        station_info: The station's declaration snapshot.

    Returns:
        The action class this command belongs to, with the rationale behind
        the classification.

    Raises:
        UnclassifiedActionError: If the command, its target instrument, or
            its target capability has no row.
    """
    if command.name is CommandName.SUBMIT_VI_ACTION:
        args: Mapping[str, object] = command.args
        vi_name = str(args.get("vi_name", ""))
        method_name = str(args.get("method_name", ""))
        if not vi_name or not method_name:
            raise UnclassifiedActionError(
                "submit_vi_action needs both 'vi_name' and 'method_name' "
                "before its action class can be decided"
            )
        return classify_control(station_info, vi_name, method_name)
    classified = COMMAND_ACTION_CLASSES.get(command.name)
    if classified is None:
        raise UnclassifiedActionError(
            f"command {command.name.value!r} has no row in the gateway's "
            f"action-class table, so no role can be granted it"
        )
    return classified
