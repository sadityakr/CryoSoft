# ---
# description: |
#   SampleChangeOperation: the second concrete OperationBase (L4) subclass —
#   "verify the cryostat is safe to open" (docs/plans/cryogenics-logbook.md
#   §8.2), now a true servicing run with a hold phase (docs/plans/unified-
#   servicing-log-and-run-recording.md §1). Ramps every magnet to zero
#   field, ramps the VTI to target_temperature_K (default 300 K, room
#   temperature), opens the first switch VI (if any), and disarms every
#   measurement VI via standby(). No HDF5 dataset — instead, once every
#   sample_period_s the run records the VTI temperature and every magnet's
#   field into OperationBase's shared recorder, so the run's servicing-log
#   entry carries the actual conditions spanning the whole hold, not just
#   the moment the ramps finished. The run then stays open (step() never
#   returns None on its own) until the operator clicks Finish — that is when
#   the physical sample change happens. Completion is gated on verified
#   postconditions only: zero field held, the switch heater off on any
#   magnet whose cached state exposes one, the VTI within tolerance held,
#   and — for a manual needle valve, the only supported mode today — an
#   explicit operator confirmation.
# entry_point: Not run directly. Constructed by the GUI's sample-change dialog
#   (Phase 5) or a test, submitted via Orchestrator.run_operation()/
#   queue_operation(); the needle-valve confirmation flows through
#   Orchestrator.confirm_operation("needle_valve"); the hold phase ends via
#   Orchestrator.finish_operation() (the card's Finish click).
# dependencies:
#   - cryosoft.core.exceptions (CryoSoftConfigError)
#   - cryosoft.core.gates (Gate)
#   - cryosoft.core.operation (OperationBase)
#   - cryosoft.core.plan (Command, PhasePlan, StepPlan, Target)
#   - cryosoft.core.station (Station) — VI access only through this, never a
#     direct virtual_instruments import (contract C6)
# input: |
#   Constructor: station (positional), person (keyword, default ""), and
#   **config carrying the docs/plans/unified-servicing-log-and-run-
#   recording.md §1 operations.sample_change: keys (vti_vi,
#   target_temperature_K, temperature_tolerance_K, temperature_window_s,
#   zero_field_eps_T, zero_field_window_s, needle_valve, sample_period_s —
#   new in Phase 3), each with a class-matching default so this constructs
#   from a sim station alone. magnet_vi_names()/measurement_vi_names()/
#   switch_vi_names() resolve the VI lists; vti_vi (default
#   "temperature_vti") must be a registered VI.
# process: |
#   initiate() ramps every magnet to 0 T and the VTI to target_temperature_K,
#   sends open_all to the first switch VI (if the station has one) and
#   standby to every measurement VI, and resets the shared recording. No
#   initiation_gates() (the default empty tuple is exactly right — nothing
#   must hold before parking begins). sample() (called once per tick,
#   throttled to sample_period_s by step()'s wait_s, exactly like
#   HeliumFillOperation's sampling loop) records the VTI temperature and
#   every magnet's field. step() always returns a StepPlan (never None on
#   its own): the run holds indefinitely — carried by the ramps (RAMPING) at
#   first, then by the sampling loop — until
#   Orchestrator.finish_operation() sets finish_requested, at which point
#   the OperationBase adapter (change_sweep_step()) returns None on the very
#   next tick regardless of what step() would return, ending the hold.
#   postcondition_gates() is then evaluated once, immediately, as the run
#   ends (docs/plans/operation-concurrency-and-error-scoping.md §2).
#   standby() is an empty PhasePlan — initiate() already parked everything.
#   postcondition_gates() reads only cached state: zero_field (every
#   magnet), heater_off (only for magnets whose cached state exposes
#   switch_heater_state — plain SuperconductingMagnetVI has no such field
#   and is silently skipped; if no magnet exposes one, no such gate is added
#   at all), vti_at_target, and — only when needle_valve == "manual" —
#   needle_valve_confirmed, reading the confirm()/confirmed() operator-ack
#   flag the GUI (Phase 5) renders as a checkbox per declared
#   operator_confirmations entry. An unmet gate never blocks completion; it
#   is named in the run manifest's postconditions_unmet list.
# output: |
#   PhasePlan/StepPlan/Command/Gate objects consumed by the Orchestrator. No
#   HDF5 side effect — the manifest's data_file stays empty, exactly as for
#   any run with no DataManager. run_summary() -> {"recording": {...}} in
#   OperationBase's generic shape, so CryogenicsRecorder writes it as this
#   run's recordings/<run_id>.json sidecar exactly like the helium fill's.
# last_updated: 2026-07-23
# ---

"""SampleChangeOperation — verify the cryostat is safe to open."""

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

# The only needle-valve mode implemented today (plan §8.2): a manual valve
# becomes an operator confirmation. A VI-capability reference (an ITC503
# close_needle_valve()-style machine-verified close) is explicitly future
# work — any other value is rejected at construction with a clear message
# rather than silently dropping the postcondition.
_NEEDLE_VALVE_MANUAL = "manual"


class SampleChangeOperation(OperationBase):
    """Verify the cryostat is safe to open: magnets off, VTI at 300 K, valve closed.

    The second concrete operation (plan §8.2), and the reference "hold
    phase" operation (docs/plans/unified-servicing-log-and-run-recording.md
    §1). ``initiate()`` ramps every magnet (``Station.magnet_vi_names()``)
    to 0 T and the configured VTI VI to ``target_temperature_K``, opens the
    first switch VI (if the station has one — display-only exclusivity
    reset, mirrors the helium fill's "if it exists" pattern for optional
    capabilities), and disarms every measurement VI
    (``Station.measurement_vi_names()``) via ``standby()``. ``step()``
    never returns ``None`` on its own: once the ramps land, the run holds —
    ``sample()`` records the VTI temperature and every magnet's field once
    per ``sample_period_s`` into the shared recorder (``run_summary()``
    hands it off in the generic ``"recording"`` shape) — until the operator
    clicks Finish (``Orchestrator.finish_operation()``), at which point the
    ``OperationBase`` adapter ends the loop and ``postcondition_gates()``
    (the STANDBY sub-phase) evaluates once, immediately.
    ``hold_for_operator = True`` tells the Operations panel to show the
    ready banner mid-run, the instant every readiness condition holds,
    since for this operation "ready" means "you may open the cryostat now" —
    true well before Finish.

    ``tolerated_safety_flags`` is deliberately left at the ``OperationBase``
    default (empty): a sample change has no business running under an
    active safety condition — unlike the helium fill, nothing about opening
    the cryostat *fixes* a tripped flag.

    Operator confirmations (plan §8.2's "needle-valve reality check")
    -------------------------------------------------------------------
    No needle-valve/gas-flow capability exists anywhere in the stack today,
    so with a manual valve (``needle_valve == "manual"``, the only supported
    value) the valve-closed postcondition cannot be machine-verified. The
    class-level ``operator_confirmations`` dict declares one key
    (``"needle_valve"``) mapped to its human-readable checkbox label; the
    instance methods ``confirm(key)`` / ``confirmed(key)`` set and read the
    flag. Phase 5's GUI renders one checkbox per declared confirmation and
    forwards a click through ``Orchestrator.confirm_operation(key)``
    (mirroring ``finish_operation()``); ``postcondition_gates()`` blocks the
    ``needle_valve_confirmed`` gate until ``confirmed("needle_valve")`` is
    True. A future VI-capability needle valve would instead add a
    machine-checked gate and skip the confirmation declaration entirely —
    the postcondition contract already supports both, which is why gates
    and confirmations are declared, not hardcoded.

    Readiness (Operations panel, plan §12): ``readiness_conditions()``
    mirrors the four ``postcondition_gates()`` checks as live checklist
    rows -- ``zero_field``, ``heater_off`` (only meaningful on a station
    with a magnet exposing ``switch_heater_state``; vacuously holds
    otherwise), ``vti_at_target``, ``needle_valve_confirmed``. ``config_key
    = "sample_change"`` maps the ``operations.sample_change:`` config block
    to this class for the GUI's generic card-building discovery. No
    ``next_due()`` override -- a sample change has no schedule.
    """

    name = "Sample Change"
    description = "Verify the cryostat is safe to open"
    ready_message = "Ready — sample can be taken out"
    config_key = "sample_change"
    #: Hold-phase operation (docs/plans/unified-servicing-log-and-run-
    #: recording.md §1): the ready banner may show mid-run, not only after
    #: Finish — see ``OperationBase.hold_for_operator``'s docstring.
    hold_for_operator = True

    #: Declared operator confirmations: {key: human-readable checkbox label}.
    #: Only "needle_valve" exists today because "manual" is the only
    #: supported needle_valve mode (see module docstring / class docstring).
    operator_confirmations: dict[str, str] = {"needle_valve": "Needle valve closed"}

    def __init__(
        self,
        station: Station,
        *,
        person: str = "",
        **config: Any,
    ) -> None:
        """Resolve the VI lists and merge the sample-change config.

        Args:
            station: The active Station; must have the VI named by
                ``config["vti_vi"]`` (default ``"temperature_vti"``).
            person: Who is performing the sample change (recorded via
                ``get_params()``, mirroring the helium fill's ``person``).
            **config: docs/plans/unified-servicing-log-and-run-recording.md
                §1 ``operations.sample_change:`` keys — ``vti_vi``,
                ``target_temperature_K``, ``temperature_tolerance_K``,
                ``temperature_window_s``, ``zero_field_eps_T``,
                ``zero_field_window_s``, ``needle_valve``,
                ``sample_period_s`` (new in Phase 3 — how often the hold
                phase records station state; default 10.0 s, matching the
                helium fill's own default) — each with a sane default so
                this constructs from a sim station alone. Unrecognised keys
                are silently ignored, so
                ``**read_operations_config(config_path)["sample_change"]``
                can be passed verbatim.

        Raises:
            CryoSoftConfigError: If ``vti_vi`` does not name a VI registered
                on this station, or ``needle_valve`` is not ``"manual"``.
        """
        super().__init__()
        self._station = station
        self._person = str(person)

        self._vti_vi_name: str = str(config.get("vti_vi", "temperature_vti"))
        self._target_temperature_K: float = float(
            config.get("target_temperature_K", 300.0)
        )
        self._temperature_tolerance_K: float = float(
            config.get("temperature_tolerance_K", 2.0)
        )
        self._temperature_window_s: float = float(
            config.get("temperature_window_s", 60.0)
        )
        self._zero_field_eps_T: float = float(config.get("zero_field_eps_T", 0.005))
        self._zero_field_window_s: float = float(
            config.get("zero_field_window_s", 10.0)
        )
        self._needle_valve: str = str(config.get("needle_valve", _NEEDLE_VALVE_MANUAL))
        self._sample_period_s: float = float(config.get("sample_period_s", 10.0))

        if not station.has_vi(self._vti_vi_name):
            raise CryoSoftConfigError(
                f"SampleChangeOperation: vti_vi={self._vti_vi_name!r} is not "
                f"a registered VI on this station."
            )
        if self._needle_valve != _NEEDLE_VALVE_MANUAL:
            raise CryoSoftConfigError(
                f"SampleChangeOperation: needle_valve={self._needle_valve!r} "
                f"is not supported; only {_NEEDLE_VALVE_MANUAL!r} is "
                f"implemented today (a VI-capability reference is future "
                f"work — plan §8.2)."
            )

        self._magnets: list[str] = station.magnet_vi_names()
        switch_vis = station.switch_vi_names()
        self._switch_vi_name: str | None = switch_vis[0] if switch_vis else None
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
        click, Phase 5) — never sets hardware, purely a human attestation
        consumed by ``postcondition_gates()``.

        Args:
            key: One of ``operator_confirmations``' keys.

        Raises:
            ValueError: If ``key`` is not a declared confirmation.
        """
        if key not in self.operator_confirmations:
            raise ValueError(
                f"SampleChangeOperation.confirm: unknown confirmation key "
                f"{key!r}; declared keys are "
                f"{sorted(self.operator_confirmations)}"
            )
        self._confirmations[key] = True
        logger.info("SampleChangeOperation: operator confirmed %r", key)

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

        A sample change ramps every magnet and the VTI, opens the switch (if
        any), and stands by every measurement VI — on a typical station that
        is everything except the level meter, so this narrowing yields
        little extra concurrency; it is still exact (a station with an
        instrument this operation never touches, e.g. a rotator, stays
        manually controllable during a sample change) and cheaper to keep
        correct than a hand-picked subset.

        Returns:
            The magnets, the configured VTI VI, the first switch VI (if the
            station has one), and every measurement VI.
        """
        claimed = set(self._magnets) | {self._vti_vi_name} | set(self._measurement_vis)
        if self._switch_vi_name is not None:
            claimed.add(self._switch_vi_name)
        return claimed

    def get_params(self) -> dict[str, Any]:
        """Return the sample change's parameters, for the run manifest.

        Returns:
            ``person`` plus every resolved config value.
        """
        return {
            "person": self._person,
            "vti_vi": self._vti_vi_name,
            "target_temperature_K": self._target_temperature_K,
            "temperature_tolerance_K": self._temperature_tolerance_K,
            "temperature_window_s": self._temperature_window_s,
            "zero_field_eps_T": self._zero_field_eps_T,
            "zero_field_window_s": self._zero_field_window_s,
            "needle_valve": self._needle_valve,
            "sample_period_s": self._sample_period_s,
        }

    def run_summary(self) -> dict[str, Any]:
        """Return the recorded VTI-temperature/magnet-field series, for the run manifest.

        Called once by the Orchestrator on ``run_finished``; ``CryogenicsRecorder``
        reads this back off ``manifest["summary"]`` and writes it as this
        run's ``recordings/<run_id>.json`` sidecar (docs/plans/unified-
        servicing-log-and-run-recording.md §3), referenced from the run's
        single ``servicing`` log entry.

        Returns:
            ``{"recording": {"unix_time": [...], "channels": {"<vi>.<value>":
            [...], ...}}}`` — ``OperationBase._recording_dict()`` verbatim
            (empty series if the hold phase never sampled, e.g. the run
            finished before ``sample()`` was ever called).
        """
        return {"recording": self._recording_dict()}

    # ------------------------------------------------------------------
    # Operations panel: readiness (plan §12) — no next_due() override, a
    # sample change has no schedule (the OperationBase default, None, is
    # exactly right).
    # ------------------------------------------------------------------

    def readiness_conditions(self) -> tuple[ReadinessCondition, ...]:
        """Return the four postcondition checks as live checklist rows.

        Every ``check``/``detail`` closure reads only the state snapshot
        passed to it (never ``self._station.cached_state`` directly), per
        the readiness-condition contract. ``heater_off`` is always present
        (unlike ``postcondition_gates()``, which omits the gate entirely
        when no magnet's *cached* state exposes ``switch_heater_state`` —
        readiness_conditions() is built once, before any tick may have
        populated that cache, so presence is instead decided per call from
        the live snapshot passed to ``check()``): if the current snapshot
        shows no magnet exposing the field, the row holds vacuously (there
        is nothing to check on this station).

        Returns:
            ``(zero_field, heater_off, vti_at_target,
            needle_valve_confirmed)`` — the last one included only while
            ``needle_valve == "manual"`` (the only supported mode today, so
            effectively always).
        """

        def _worst_offender(state: dict[str, Any]) -> tuple[str | None, float | None]:
            if not self._magnets:
                return None, None
            worst_name = self._magnets[0]
            worst_field: float | None = None
            worst_abs = -1.0
            for magnet in self._magnets:
                field = state.get(magnet, {}).get("get_field")
                if isinstance(field, bool) or not isinstance(field, (int, float)):
                    return magnet, None
                if abs(float(field)) > worst_abs:
                    worst_abs = abs(float(field))
                    worst_name = magnet
                    worst_field = float(field)
            return worst_name, worst_field

        def _zero_field_holds(state: dict[str, Any]) -> bool:
            if not self._magnets:
                return True
            _name, field = _worst_offender(state)
            return field is not None and abs(field) < self._zero_field_eps_T

        def _zero_field_detail(state: dict[str, Any]) -> str:
            if not self._magnets:
                return "no magnets on this station"
            name, field = _worst_offender(state)
            if field is None:
                return f"{name} field reading unavailable"
            return f"{name} at {field:.2f} T"

        def _heater_relevant_magnets(state: dict[str, Any]) -> list[str]:
            return [
                magnet
                for magnet in self._magnets
                if "switch_heater_state" in state.get(magnet, {})
            ]

        def _heater_off_holds(state: dict[str, Any]) -> bool:
            relevant = _heater_relevant_magnets(state)
            if not relevant:
                return True  # nothing on this station exposes a heater state
            return all(
                state.get(magnet, {}).get("switch_heater_state") == "OFF"
                for magnet in relevant
            )

        def _heater_off_detail(state: dict[str, Any]) -> str:
            relevant = _heater_relevant_magnets(state)
            if not relevant:
                return "no switch heater on this station"
            offenders = [
                magnet
                for magnet in relevant
                if state.get(magnet, {}).get("switch_heater_state") != "OFF"
            ]
            if not offenders:
                return "all switch heaters off"
            offender = offenders[0]
            return f"{offender} heater {state.get(offender, {}).get('switch_heater_state')}"

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
                key="heater_off",
                label="Switch heater off",
                check=_heater_off_holds,
                detail=_heater_off_detail,
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
        """Ramp every magnet to zero field and the VTI to target, park everything else.

        Also resets the shared recording (``OperationBase._reset_recording()``)
        so a fresh run starts with an empty series — ``sample()`` appends to
        it for the whole hold phase.

        Returns:
            A ``PhasePlan`` with every magnet and the VTI targeted, plus
            ``open_all`` on the first switch VI (if any) and ``standby`` on
            every measurement VI.
        """
        self._reset_recording()
        targets: dict[str, Target] = {magnet: Target(0.0) for magnet in self._magnets}
        targets[self._vti_vi_name] = Target(self._target_temperature_K)

        commands: list[Command] = []
        if self._switch_vi_name is not None:
            commands.append(Command(self._switch_vi_name, "open_all", {}))
        for vi_name in self._measurement_vis:
            commands.append(Command(vi_name, "standby", {}))

        logger.info(
            "SampleChangeOperation.initiate(): %d magnet(s) to zero field, "
            "%s to %.1f K, switch_vi=%s, %d measurement VI(s) to standby",
            len(self._magnets),
            self._vti_vi_name,
            self._target_temperature_K,
            self._switch_vi_name,
            len(self._measurement_vis),
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
            values[f"{magnet}.get_field"] = float(self._station.get_vi(magnet).get_field())
        self._record_sample(now, values)

    def step(self) -> StepPlan | None:
        """Keep the run open — the hold phase (docs/plans/unified-servicing-log-and-run-recording.md §1).

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
        """Verify zero field, switch heater(s) off, VTI at target, valve confirmed.

        All four checks read only cached state (or, for the valve, the
        operator-confirmation flag) — no extra hardware poll. The
        Orchestrator evaluates each gate exactly once, immediately, as the
        run ends (docs/plans/operation-concurrency-and-error-scoping.md
        §2) — an unmet gate is recorded on the run manifest's
        ``postconditions_unmet`` list, never held or timed out. The
        ``window_s`` each ``Gate`` still declares below has no effect there
        (it only matters if this method's gates are ever stepped instead —
        they are not, by any current caller).

        Returns:
            ``zero_field`` (always); ``heater_off`` (only if at least one
            magnet's cached state exposes ``switch_heater_state`` — plain
            ``SuperconductingMagnetVI`` has no such field and is silently
            excluded from the check; the gate itself is omitted entirely if
            no magnet exposes the field); ``vti_at_target`` (always); and
            ``needle_valve_confirmed`` (only when ``needle_valve ==
            "manual"`` — the only supported mode today, so effectively
            always).
        """
        gates: list[Gate] = []

        def _all_zero_field() -> bool:
            state = self._station.cached_state
            for magnet in self._magnets:
                field = state.get(magnet, {}).get("get_field")
                if isinstance(field, bool) or not isinstance(field, (int, float)):
                    return False
                if abs(float(field)) >= self._zero_field_eps_T:
                    return False
            return True

        gates.append(
            Gate("zero_field", check=_all_zero_field, window_s=self._zero_field_window_s)
        )

        heater_magnets = [
            magnet
            for magnet in self._magnets
            if "switch_heater_state" in self._station.cached_state.get(magnet, {})
        ]
        if heater_magnets:

            def _all_heaters_off() -> bool:
                state = self._station.cached_state
                for magnet in heater_magnets:
                    if state.get(magnet, {}).get("switch_heater_state") != "OFF":
                        return False
                return True

            gates.append(Gate("heater_off", check=_all_heaters_off, window_s=0.0))

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
