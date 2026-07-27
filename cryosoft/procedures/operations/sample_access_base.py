# ---
# description: |
#   _SampleAccessOperationBase: shared L4 logic for the two "verify the
#   cryostat is safe to open" operations, SampleLoadOperation and
#   SampleUnloadOperation. Loading a sample and unloading it are separate
#   physical events, often hours or days apart, but both require the
#   identical system state — every magnet in standby and the VTI at
#   target_temperature_K (default 290 K) — so the two concrete classes below
#   share every behavior and differ only in their identity attributes
#   (name/description/ready_message/config_key). Splitting the state (rather
#   than one long-lived hold-phase run spanning both events) frees the
#   Orchestrator between load and unload for other procedures/operations.
#   Checks every magnet is in standby via its standby() lifecycle method,
#   ramps the VTI to target_temperature_K, and — unless the operator has
#   unchecked the Operations panel's "Disarm measurement instruments"
#   toggle (disarm_measurement_vis, default True) — disarms every
#   measurement VI via standby() too, so a measurement instrument already
#   armed for something else can be left alone and the operation still
#   runs. No HDF5 dataset — instead, once every sample_period_s the
#   run records the VTI temperature and every magnet's field into
#   OperationBase's shared recorder, so the run's servicing-log entry
#   carries the actual conditions spanning the whole hold, not just the
#   moment the ramps finished. The run then stays open (step() never returns
#   None on its own) until the operator clicks Finish or Abort — that is
#   when the physical sample load/unload happens. Completion is gated on
#   verified postconditions only: every magnet in standby (magnet_state() ==
#   "standby" — for a persistent-capable magnet this already implies the
#   switch heater is off, so no separate heater check is needed), the VTI
#   within tolerance held, and — for a manual needle valve, the only
#   supported mode today — an explicit operator confirmation.
# entry_point: Not run directly. Subclassed by SampleLoadOperation /
#   SampleUnloadOperation, constructed by the GUI's Operations panel or a
#   test, submitted via Orchestrator.run_operation()/queue_operation(); the
#   needle-valve confirmation flows through
#   Orchestrator.confirm_operation("needle_valve"); the hold phase ends via
#   Orchestrator.finish_operation() (the card's Finish click) or
#   Orchestrator.abort_procedure() (the card's Abort click).
# dependencies:
#   - cryosoft.core.exceptions (CryoSoftConfigError)
#   - cryosoft.core.gates (Gate)
#   - cryosoft.core.operation (OperationBase)
#   - cryosoft.core.plan (Command, PhasePlan, StepPlan, Target)
#   - cryosoft.core.station (Station) — VI access only through this, never a
#     direct virtual_instruments import (contract C6)
# input: |
#   Constructor: station (positional), person (keyword, default ""), and
#   **config carrying the operations.<config_key>: keys (vti_vi,
#   target_temperature_K, temperature_tolerance_K, temperature_window_s,
#   needle_valve, sample_period_s), each with a class-matching default so
#   this constructs from a sim station alone. magnet_vi_names()/
#   measurement_vi_names() resolve the VI lists; vti_vi (default
#   "temperature_vti") must be a registered VI.
# process: |
#   initiate() sends standby to every magnet (via the magnet VI's standby()
#   lifecycle method), ramps the VTI to target_temperature_K, and sends
#   standby to every measurement VI, and resets the shared recording. No
#   initiation_gates() (the default empty tuple is exactly right — nothing
#   must hold before parking begins). sample() (called once per tick,
#   throttled to sample_period_s by step()'s wait_s, exactly like
#   HeliumFillOperation's sampling loop) records the VTI temperature and
#   every magnet's field. step() always returns a StepPlan (never None on
#   its own): the run holds indefinitely — carried by the VTI ramp
#   (RAMPING) at first, then by the sampling loop — until
#   Orchestrator.finish_operation() or abort_procedure() sets
#   finish_requested / ends the run, at which point the OperationBase
#   adapter (change_sweep_step()) returns None on the very next tick
#   regardless of what step() would return, ending the hold.
#   postcondition_gates() is then evaluated once, immediately, as the run
#   ends (Finish only — an abort skips postcondition evaluation, mirroring
#   every other Orchestrator run).
#   standby() is an empty PhasePlan — initiate() already parked everything.
#   postcondition_gates() reads only cached state: zero_field (every
#   magnet's magnet_state() == "standby" — see GLOSSARY.md's Magnet state;
#   this already covers the switch heater being off wherever one exists, so
#   there is no separate heater_off gate), vti_at_target, and — only when
#   needle_valve == "manual" — needle_valve_confirmed, reading the
#   confirm()/confirmed() operator-ack flag the GUI renders as a checkbox
#   per declared operator_confirmations entry. An unmet gate never blocks
#   completion; it is named in the run manifest's postconditions_unmet list.
# output: |
#   PhasePlan/StepPlan/Command/Gate objects consumed by the Orchestrator. No
#   HDF5 side effect — the manifest's data_file stays empty, exactly as for
#   any run with no DataManager. run_summary() -> {"recording": {...}} in
#   OperationBase's generic shape, so CryogenicsRecorder writes it as this
#   run's recordings/<run_id>.json sidecar exactly like the helium fill's.
# known_limitations: |
#   Magnets are dispatched via PhasePlan.commands (standby()), not
#   PhasePlan.targets, so Orchestrator._active_system_vis (built only from
#   plan.targets — see orchestrator.py's _start_procedure) does not include
#   them. Two consequences, accepted as-is: (1) pause_procedure()/
#   abort_procedure() hold only the VTI in place; a magnet mid-ramp to
#   standby keeps ramping to zero field (a safe direction, just inconsistent
#   with the documented hold-in-place guarantee every other ramp gets).
#   (2) The status feed's initiation line for each magnet reads "Disarming
#   <magnet> measurement" (Orchestrator._describe_measurement_command()
#   assumes every Command is a measurement arm/disarm) instead of a "Ramping
#   <magnet> to 0 T" line; cosmetic only — get_ramp_status()/troubleshoot
#   status's ETA and progress reporting are unaffected since they scan all
#   system VIs regardless of dispatch path. A general fix (widen
#   _active_system_vis to include any system VI named in plan.commands) was
#   proposed and declined; revisit if a future operation needs the same
#   pattern.
# last_updated: 2026-07-27
# ---

"""_SampleAccessOperationBase — shared logic for Sample Load / Sample Unload."""

from __future__ import annotations

import logging
import time
from typing import Any

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.gates import Gate
from cryosoft.core.operation import OperationBase, ReadinessCondition
from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.station import Station

logger = logging.getLogger(__name__)

# The only needle-valve mode implemented today: a manual valve
# becomes an operator confirmation. A VI-capability reference (an ITC503
# close_needle_valve()-style machine-verified close) is explicitly future
# work — any other value is rejected at construction with a clear message
# rather than silently dropping the postcondition.
_NEEDLE_VALVE_MANUAL = "manual"


class _SampleAccessOperationBase(OperationBase):
    """Shared base: magnets in standby, VTI at target, measurement VIs disarmed, valve closed.

    Concrete subclasses (``SampleLoadOperation``, ``SampleUnloadOperation``)
    set only ``name``/``description``/``ready_message``/``config_key`` —
    everything else here is identical for both, since loading and unloading
    a sample require the same cryostat state, just at different, often far
    apart, points in time.

    ``initiate()`` sends a ``standby()`` lifecycle command to every magnet
    (``Station.magnet_vi_names()``), ramps the configured VTI VI to
    ``target_temperature_K``, and — while ``disarm_measurement_vis`` is True
    (the default; see ``pre_run_toggles``) — disarms every measurement VI
    (``Station.measurement_vi_names()``) via ``standby()`` too, freed again
    for manual use or another procedure the instant this run ends, so the
    (potentially long) gap between a Sample Load and the matching Sample
    Unload is not idle time for those instruments. Unchecking the panel's
    toggle for one run skips this entirely (no standby command, no claim —
    see ``claimed_vi_names()``), for when a measurement VI is already armed
    for something unrelated the operator does not want disturbed.

    ``step()`` never returns
    ``None`` on its own: once the ramps land, the run holds — ``sample()``
    records the VTI temperature and every magnet's field once per
    ``sample_period_s`` into the shared recorder (``run_summary()`` hands it
    off in the generic ``"recording"`` shape) — until the operator clicks
    Finish or Abort (``Orchestrator.finish_operation()`` or
    ``abort_procedure()``), at which point the ``OperationBase`` adapter ends
    the loop and, for Finish, ``postcondition_gates()`` (the STANDBY
    sub-phase) evaluates once, immediately. ``hold_for_operator = True``
    tells the Operations panel to show the ready banner mid-run, the instant
    every readiness condition holds, since for this operation "ready" means
    "you may open the cryostat now" — true well before Finish.

    ``tolerated_safety_flags`` is deliberately left at the ``OperationBase``
    default (empty): neither loading nor unloading a sample has any business
    running under an active safety condition — unlike the helium fill,
    nothing about opening the cryostat *fixes* a tripped flag.

    Operator confirmations (the "needle-valve reality check")
    -------------------------------------------------------------------
    No needle-valve/gas-flow capability exists anywhere in the stack today,
    so with a manual valve (``needle_valve == "manual"``, the only supported
    value) the valve-closed postcondition cannot be machine-verified. The
    class-level ``operator_confirmations`` dict declares one key
    (``"needle_valve"``) mapped to its human-readable checkbox label; the
    instance methods ``confirm(key)`` / ``confirmed(key)`` set and read the
    flag. The GUI renders one checkbox per declared confirmation and
    forwards a click through ``Orchestrator.confirm_operation(key)``
    (mirroring ``finish_operation()``); ``postcondition_gates()`` blocks the
    ``needle_valve_confirmed`` gate until ``confirmed("needle_valve")`` is
    True. A future VI-capability needle valve would instead add a
    machine-checked gate and skip the confirmation declaration entirely —
    the postcondition contract already supports both, which is why gates
    and confirmations are declared, not hardcoded.

    Readiness (Operations panel): ``readiness_conditions()``
    mirrors the three ``postcondition_gates()`` checks as live checklist
    rows -- ``zero_field`` (every magnet's ``magnet_state() == "standby"``,
    which already implies the switch heater is off wherever one exists —
    see GLOSSARY.md's **Magnet state**), ``vti_at_target``,
    ``needle_valve_confirmed``. No ``next_due()`` override -- neither
    operation has a schedule.
    """

    #: Hold-phase operation: the ready banner may show mid-run, not only after
    #: Finish — see ``OperationBase.hold_for_operator``'s docstring.
    hold_for_operator = True

    #: Declared operator confirmations: {key: human-readable checkbox label}.
    #: Only "needle_valve" exists today because "manual" is the only
    #: supported needle_valve mode (see module docstring / class docstring).
    operator_confirmations: dict[str, str] = {"needle_valve": "Needle valve closed"}

    #: Declared pre-run toggles: {config key: human-readable checkbox label}.
    #: Unlike operator_confirmations (shown only while running, one-way,
    #: confirmed via Orchestrator.confirm_operation()), these render as
    #: persistent checkboxes the operator can flip before clicking Start;
    #: their checked state at that moment is passed straight through as the
    #: matching **config keyword (mirroring a devices.yaml-declared default,
    #: just supplied per-run instead) — see __init__'s disarm_measurement_vis.
    pre_run_toggles: dict[str, str] = {
        "disarm_measurement_vis": "Disarm measurement instruments",
    }

    def __init__(
        self,
        station: Station,
        *,
        person: str = "",
        **config: Any,
    ) -> None:
        """Resolve the VI lists and merge the sample-access config.

        Args:
            station: The active Station; must have the VI named by
                ``config["vti_vi"]`` (default ``"temperature_vti"``).
            person: Who is performing the sample load/unload (recorded via
                ``get_params()``, mirroring the helium fill's ``person``).
            **config: ``operations.<config_key>:`` keys — ``vti_vi``,
                ``target_temperature_K`` (default 290.0 K), ``temperature_tolerance_K``,
                ``temperature_window_s``, ``needle_valve``, ``sample_period_s``
                (how often the hold phase records station state; default
                10.0 s, matching the helium fill's own default),
                ``disarm_measurement_vis`` (default ``True`` — whether
                ``initiate()`` stands by every measurement VI and
                ``claimed_vi_names()`` claims them; the Operations panel
                exposes this as a persistent checkbox, see
                ``pre_run_toggles``, so an operator can leave it unchecked to
                run this operation while a measurement VI is already armed
                for something else) — each with a sane default so this
                constructs from a sim station alone. Unrecognised keys are
                silently ignored, so
                ``**read_operations_config(config_path)[config_key]`` can be
                passed verbatim.

        Raises:
            CryoSoftConfigError: If ``vti_vi`` does not name a VI registered
                on this station, or ``needle_valve`` is not ``"manual"``.
        """
        super().__init__()
        self._station = station
        self._person = str(person)

        self._vti_vi_name: str = str(config.get("vti_vi", "temperature_vti"))
        self._target_temperature_K: float = float(
            config.get("target_temperature_K", 290.0)
        )
        self._temperature_tolerance_K: float = float(
            config.get("temperature_tolerance_K", 2.0)
        )
        self._temperature_window_s: float = float(
            config.get("temperature_window_s", 60.0)
        )
        self._needle_valve: str = str(config.get("needle_valve", _NEEDLE_VALVE_MANUAL))
        self._sample_period_s: float = float(config.get("sample_period_s", 10.0))
        self._disarm_measurement_vis: bool = bool(config.get("disarm_measurement_vis", True))

        if not station.has_vi(self._vti_vi_name):
            raise CryoSoftConfigError(
                f"{type(self).__name__}: vti_vi={self._vti_vi_name!r} is not "
                f"a registered VI on this station."
            )
        if self._needle_valve != _NEEDLE_VALVE_MANUAL:
            raise CryoSoftConfigError(
                f"{type(self).__name__}: needle_valve={self._needle_valve!r} "
                f"is not supported; only {_NEEDLE_VALVE_MANUAL!r} is "
                f"implemented today (a VI-capability reference is future "
                f"work —)."
            )

        self._magnets: list[str] = station.magnet_vi_names()
        self._measurement_vis: list[str] = station.measurement_vi_names()

        #: Operator-confirmation flags, keyed by ``operator_confirmations``
        #: key. Set via ``confirm()``, read via ``confirmed()``.
        self._confirmations: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Operator confirmations
    # ------------------------------------------------------------------

    def confirm(self, key: str) -> None:
        """Record an operator confirmation for a declared checkbox.

        Called by ``Orchestrator.confirm_operation(key)`` (a GUI checkbox
        click) — never sets hardware, purely a human attestation consumed
        by ``postcondition_gates()``.

        Args:
            key: One of ``operator_confirmations``' keys.

        Raises:
            ValueError: If ``key`` is not a declared confirmation.
        """
        if key not in self.operator_confirmations:
            raise ValueError(
                f"{type(self).__name__}.confirm: unknown confirmation key "
                f"{key!r}; declared keys are "
                f"{sorted(self.operator_confirmations)}"
            )
        self._confirmations[key] = True
        logger.info("%s: operator confirmed %r", type(self).__name__, key)

    def confirmed(self, key: str) -> bool:
        """Return whether ``key`` has been confirmed (default: not yet).

        Args:
            key: One of ``operator_confirmations``' keys.

        Returns:
            True once ``confirm(key)`` has been called; False otherwise
            (including for an unknown key — this is a read, never raises).
        """
        return self._confirmations.get(key, False)

    # ------------------------------------------------------------------
    # OperationBase lifecycle
    # ------------------------------------------------------------------

    def claimed_vi_names(self) -> set[str]:
        """Claim every VI this operation actually commands in ``initiate()``.

        Loading/unloading a sample commands every magnet to standby and the
        VTI to ramp, and — while ``disarm_measurement_vis`` is True, the
        default — stands by every measurement VI too; on a typical station
        that is everything except the level meter and switch, so this
        narrowing yields little extra concurrency; it is still exact (a
        station with an instrument this operation never touches, e.g. a
        rotator, stays manually controllable during a sample load/unload)
        and cheaper to keep correct than a hand-picked subset. When
        ``disarm_measurement_vis`` is False (the operator unchecked the
        panel's toggle), measurement VIs are excluded here too — they are
        never commanded, so claiming them would only block manual use / a
        concurrent measurement for no reason.

        Returns:
            The magnets and the configured VTI VI, plus every measurement VI
            unless ``disarm_measurement_vis`` is False.
        """
        claimed = set(self._magnets) | {self._vti_vi_name}
        if self._disarm_measurement_vis:
            claimed |= set(self._measurement_vis)
        return claimed

    def get_params(self) -> dict[str, Any]:
        """Return this operation's parameters, for the run manifest.

        Returns:
            ``person`` plus every resolved config value.
        """
        return {
            "person": self._person,
            "vti_vi": self._vti_vi_name,
            "target_temperature_K": self._target_temperature_K,
            "temperature_tolerance_K": self._temperature_tolerance_K,
            "temperature_window_s": self._temperature_window_s,
            "needle_valve": self._needle_valve,
            "sample_period_s": self._sample_period_s,
            "disarm_measurement_vis": self._disarm_measurement_vis,
        }

    def run_summary(self) -> dict[str, Any]:
        """Return the recorded VTI-temperature/magnet-field series, for the run manifest.

        Called once by the Orchestrator on ``run_finished``; ``CryogenicsRecorder``
        reads this back off ``manifest["summary"]`` and writes it as this
        run's ``recordings/<run_id>.json`` sidecar, referenced from the run's
        single ``servicing`` log entry.

        Returns:
            ``{"recording": {"unix_time": [...], "channels": {"<vi>.<value>":
            [...], ...}}}`` — ``OperationBase._recording_dict()`` verbatim
            (empty series if the hold phase never sampled, e.g. the run
            finished before ``sample()`` was ever called).
        """
        return {"recording": self._recording_dict()}

    # ------------------------------------------------------------------
    # Operations panel: readiness — no next_due() override, neither
    # operation has a schedule (the OperationBase default, None, is
    # exactly right).
    # ------------------------------------------------------------------

    def readiness_conditions(self) -> tuple[ReadinessCondition, ...]:
        """Return the three postcondition checks as live checklist rows.

        Every ``check``/``detail`` closure reads only the state snapshot
        passed to it (never ``self._station.cached_state`` directly), per
        the readiness-condition contract. ``zero_field`` depends solely on
        ``magnet_state() == "standby"`` (GLOSSARY.md's **Magnet state**),
        which already implies the switch heater is off wherever a magnet has
        one — there is no separate heater check.

        Returns:
            ``(zero_field, vti_at_target, needle_valve_confirmed)`` — the
            last one included only while ``needle_valve == "manual"`` (the
            only supported mode today, so effectively always).
        """

        def _magnet_not_standby(state: dict[str, Any]) -> tuple[str | None, str | None]:
            """Return the first magnet not in standby, or (None, None) if all standby."""
            for magnet in self._magnets:
                magnet_state = state.get(magnet, {}).get("magnet_state")
                if magnet_state != "standby":
                    return magnet, magnet_state
            return None, None

        def _zero_field_holds(state: dict[str, Any]) -> bool:
            if not self._magnets:
                return True
            _name, _state = _magnet_not_standby(state)
            return _name is None

        def _zero_field_detail(state: dict[str, Any]) -> str:
            if not self._magnets:
                return "no magnets on this station"
            name, magnet_state = _magnet_not_standby(state)
            if name is None:
                return "all magnets standby"
            if magnet_state is None:
                return f"{name} state unavailable"
            return f"{name} {magnet_state}"

        def _vti_holds(state: dict[str, Any]) -> bool:
            temperature = state.get(self._vti_vi_name, {}).get("temperature")
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                return False
            return abs(float(temperature) - self._target_temperature_K) <= (
                self._temperature_tolerance_K
            )

        def _vti_detail(state: dict[str, Any]) -> str:
            temperature = state.get(self._vti_vi_name, {}).get("temperature")
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                return "reading unavailable"
            return f"currently {float(temperature):.1f} K"

        conditions = [
            ReadinessCondition(
                key="zero_field",
                label="All magnets at zero field",
                check=_zero_field_holds,
                detail=_zero_field_detail,
            ),
            ReadinessCondition(
                key="vti_at_target",
                label=f"VTI at {self._target_temperature_K:.0f} K",
                check=_vti_holds,
                detail=_vti_detail,
            ),
        ]
        if self._needle_valve == _NEEDLE_VALVE_MANUAL:
            conditions.append(
                ReadinessCondition(
                    key="needle_valve_confirmed",
                    label="Needle valve closed",
                    check=lambda _state: self.confirmed("needle_valve"),
                    detail=None,
                )
            )
        return tuple(conditions)

    def initiate(self) -> PhasePlan:
        """Command magnets to standby, ramp VTI to target, disarm measurement VIs.

        Also resets the shared recording (``OperationBase._reset_recording()``)
        so a fresh run starts with an empty series — ``sample()`` appends to
        it for the whole hold phase. Each magnet is commanded to standby (via
        ``standby()`` lifecycle call) before the VTI ramp begins.

        Returns:
            A ``PhasePlan`` with every magnet commanded to standby, the VTI
            targeted, plus ``standby`` on every measurement VI — unless
            ``disarm_measurement_vis`` is False, in which case measurement
            VIs are left untouched (e.g. one is already armed for an
            unrelated test the operator does not want this run to disturb).
        """
        self._reset_recording()
        targets: dict[str, Target] = {
            self._vti_vi_name: Target(self._target_temperature_K)
        }

        commands: list[Command] = []
        for magnet in self._magnets:
            commands.append(Command(magnet, "standby", {}))
        if self._disarm_measurement_vis:
            for vi_name in self._measurement_vis:
                commands.append(Command(vi_name, "standby", {}))

        logger.info(
            "%s.initiate(): %d magnet(s) to standby, %s to %.1f K, "
            "%d measurement VI(s) to standby (disarm_measurement_vis=%s)",
            type(self).__name__,
            len(self._magnets),
            self._vti_vi_name,
            self._target_temperature_K,
            len(self._measurement_vis) if self._disarm_measurement_vis else 0,
            self._disarm_measurement_vis,
        )
        return PhasePlan(targets=targets, commands=tuple(commands), wait_s=0.0)

    # initiation_gates() is deliberately NOT overridden: the OperationBase
    # default (empty tuple) is exactly right here — nothing must hold before
    # parking begins, unlike the helium fill's zero-field-before-sampling
    # gate.

    def sample(self) -> None:
        """Record the VTI temperature and every magnet's field (the hold-phase recording).

        Called once per tick (``measure()`` adapter), throttled to
        ``sample_period_s`` by ``step()``'s ``wait_s`` — mirrors
        ``HeliumFillOperation.sample()``'s cadence exactly. Reads live VI
        values through the Station (never ``cached_state`` directly — this
        is a genuine per-tick reading, like any other ``sample()``), so the
        recording spans the whole hold, from the ramps landing to Finish.
        """
        now = time.time()
        values: dict[str, float] = {
            f"{self._vti_vi_name}.temperature": float(
                self._station.get_vi(self._vti_vi_name).temperature()
            )
        }
        for magnet in self._magnets:
            values[f"{magnet}.magnet_field_T"] = float(
                self._station.get_vi(magnet).magnet_field_T()
            )
        self._record_sample(now, values)

    def step(self) -> StepPlan | None:
        """Keep the run open — the hold phase.

        Never returns ``None`` on its own: once the ramps land, the run
        holds — ``sample()`` keeps recording every ``sample_period_s`` —
        until ``Orchestrator.finish_operation()`` sets ``finish_requested``,
        at which point the ``OperationBase`` adapter
        (``change_sweep_step()``) returns ``None`` on the very next tick
        regardless of what this method returns, ending the hold and
        proceeding to ``standby()``/``postcondition_gates()``.

        Returns:
            ``StepPlan(targets={}, wait_s=sample_period_s)``, always.
        """
        return StepPlan(targets={}, wait_s=self._sample_period_s)

    def standby(self) -> PhasePlan:
        """Return an empty plan — everything was already parked by ``initiate()``.

        Returns:
            An empty ``PhasePlan`` (no targets, no commands).
        """
        return PhasePlan(targets={}, commands=(), wait_s=0.0)

    def postcondition_gates(self) -> tuple[Gate, ...]:
        """Verify zero field, VTI at target, valve confirmed.

        All three checks read only cached state (or, for the valve, the
        operator-confirmation flag) — no extra hardware poll. The
        Orchestrator evaluates each gate exactly once, immediately, as the
        run ends — an unmet gate is recorded on the run manifest's
        ``postconditions_unmet`` list, never held or timed out. The
        ``window_s`` each ``Gate`` still declares below has no effect there
        (it only matters if this method's gates are ever stepped instead —
        they are not, by any current caller).

        Returns:
            ``zero_field`` (always — every magnet's ``magnet_state() ==
            "standby"``, which already implies the switch heater is off
            wherever a magnet has one, so there is no separate heater gate);
            ``vti_at_target`` (always); and ``needle_valve_confirmed`` (only
            when ``needle_valve == "manual"`` — the only supported mode
            today, so effectively always).
        """
        gates: list[Gate] = []

        def _all_magnets_standby() -> bool:
            """Check that all magnets are in standby state (PSU ≈ 0, coil ≈ 0, heater off)."""
            state = self._station.cached_state
            for magnet in self._magnets:
                if state.get(magnet, {}).get("magnet_state") != "standby":
                    return False
            return True

        gates.append(
            Gate("zero_field", check=_all_magnets_standby, window_s=10.0)
        )

        def _vti_at_target() -> bool:
            state = self._station.cached_state
            temperature = state.get(self._vti_vi_name, {}).get("temperature")
            if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
                return False
            return abs(float(temperature) - self._target_temperature_K) <= (
                self._temperature_tolerance_K
            )

        gates.append(
            Gate(
                "vti_at_target",
                check=_vti_at_target,
                window_s=self._temperature_window_s,
            )
        )

        if self._needle_valve == _NEEDLE_VALVE_MANUAL:
            gates.append(
                Gate(
                    "needle_valve_confirmed",
                    check=lambda: self.confirmed("needle_valve"),
                    window_s=0.0,
                )
            )

        return tuple(gates)
