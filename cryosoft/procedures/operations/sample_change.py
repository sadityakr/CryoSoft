# ---
# description: |
#   SampleChangeOperation: the second concrete OperationBase (L4) subclass —
#   "verify the cryostat is safe to open" (docs/plans/cryogenics-logbook.md
#   §8.2). Ramps every magnet to zero field, ramps the VTI to
#   target_temperature_K (default 300 K, room temperature), opens the first
#   switch VI (if any), and disarms every measurement VI via standby(). No
#   dataset — a sample change produces no measurement curve. Completion is
#   gated on verified postconditions only: zero field held, the switch
#   heater off on any magnet whose cached state exposes one, the VTI within
#   tolerance held, and — for a manual needle valve, the only supported mode
#   today — an explicit operator confirmation.
# entry_point: Not run directly. Constructed by the GUI's sample-change dialog
#   (Phase 5) or a test, submitted via Orchestrator.run_operation()/
#   queue_operation(); the needle-valve confirmation flows through
#   Orchestrator.confirm_operation("needle_valve").
# dependencies:
#   - cryosoft.core.exceptions (CryoSoftConfigError)
#   - cryosoft.core.gates (Gate)
#   - cryosoft.core.operation (OperationBase)
#   - cryosoft.core.plan (Command, PhasePlan, StepPlan, Target)
#   - cryosoft.core.station (Station) — VI access only through this, never a
#     direct virtual_instruments import (contract C6)
# input: |
#   Constructor: station (positional), person (keyword, default ""), and
#   **config carrying the docs/plans/cryogenics-logbook.md §9
#   operations.sample_change: keys (vti_vi, target_temperature_K,
#   temperature_tolerance_K, temperature_window_s, zero_field_eps_T,
#   zero_field_window_s, needle_valve, postcondition_timeout_s), each with a
#   class-matching default so this constructs from a sim station alone.
#   magnet_vi_names()/measurement_vi_names()/switch_vi_names() resolve the
#   VI lists; vti_vi (default "temperature_vti") must be a registered VI.
# process: |
#   initiate() ramps every magnet to 0 T and the VTI to target_temperature_K,
#   sends open_all to the first switch VI (if the station has one) and
#   standby to every measurement VI. No initiation_gates() (the default empty
#   tuple is exactly right — nothing must hold before parking begins).
#   step() returns None immediately: the whole duration is carried by the
#   ramps (RAMPING) and postcondition_gates() (STANDBY), not by an
#   open-ended sampling loop. standby() is an empty PhasePlan — initiate()
#   already parked everything. postcondition_gates() reads only cached
#   state: zero_field (every magnet, held zero_field_window_s), heater_off
#   (only for magnets whose cached state exposes switch_heater_state — plain
#   SuperconductingMagnetVI has no such field and is silently skipped; if no
#   magnet exposes one, no such gate is added at all), vti_at_target (held
#   temperature_window_s), and — only when needle_valve == "manual" —
#   needle_valve_confirmed, reading the confirm()/confirmed() operator-ack
#   flag the GUI (Phase 5) renders as a checkbox per declared
#   operator_confirmations entry.
# output: |
#   PhasePlan/StepPlan/Command/Gate objects consumed by the Orchestrator. No
#   HDF5 side effect — the manifest's data_file stays empty, exactly as for
#   any run with no DataManager.
# last_updated: 2026-07-19
# ---

"""SampleChangeOperation — verify the cryostat is safe to open."""

from __future__ import annotations

import logging
from typing import Any

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.gates import Gate
from cryosoft.core.operation import OperationBase
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

    The second concrete operation (plan §8.2). ``initiate()`` ramps every
    magnet (``Station.magnet_vi_names()``) to 0 T and the configured VTI VI
    to ``target_temperature_K``, opens the first switch VI (if the station
    has one — display-only exclusivity reset, mirrors the helium fill's
    "if it exists" pattern for optional capabilities), and disarms every
    measurement VI (``Station.measurement_vi_names()``) via ``standby()``.
    There is no sampling loop: ``step()`` returns ``None`` immediately, so
    the whole run duration is carried by the ramps (RAMPING) and
    ``postcondition_gates()`` (the STANDBY sub-phase) — exactly the shape
    the Orchestrator already drives for a procedure with no gates.

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
    """

    name = "Sample Change"
    description = "Verify the cryostat is safe to open"

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
            **config: Plan §9 ``operations.sample_change:`` keys —
                ``vti_vi``, ``target_temperature_K``,
                ``temperature_tolerance_K``, ``temperature_window_s``,
                ``zero_field_eps_T``, ``zero_field_window_s``,
                ``needle_valve``, ``postcondition_timeout_s`` — each with a
                sane default matching §9 so this constructs from a sim
                station alone. Unrecognised keys are silently ignored, so
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
        # Not a §9 default of OperationBase's own (600 s) — a sample change
        # legitimately takes a long time (a room-temperature warm-up), so
        # the instance overrides the class attribute here.
        self.postcondition_timeout_s: float = float(
            config.get("postcondition_timeout_s", 7200.0)
        )

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

    def get_params(self) -> dict[str, Any]:
        """Return the sample change's parameters, for the run manifest.

        Returns:
            ``person`` plus every resolved §9 config value.
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
        }

    def initiate(self) -> PhasePlan:
        """Ramp every magnet to zero field and the VTI to target, park everything else.

        Returns:
            A ``PhasePlan`` with every magnet and the VTI targeted, plus
            ``open_all`` on the first switch VI (if any) and ``standby`` on
            every measurement VI.
        """
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

    def step(self) -> StepPlan | None:
        """Return ``None`` immediately — the run has no sampling loop.

        The whole duration is carried by initiate()'s ramps (RAMPING) and
        postcondition_gates() (the STANDBY sub-phase), mirroring how a
        procedure with no ``step()`` work ends its loop on the first call.

        Returns:
            ``None``, always.
        """
        return None

    def standby(self) -> PhasePlan:
        """Return an empty plan — everything was already parked by ``initiate()``.

        Returns:
            An empty ``PhasePlan`` (no targets, no commands).
        """
        return PhasePlan(targets={}, commands=(), wait_s=0.0)

    def postcondition_gates(self) -> tuple[Gate, ...]:
        """Verify zero field, switch heater(s) off, VTI at target, valve confirmed.

        All four checks read only cached state (or, for the valve, the
        operator-confirmation flag) — no extra hardware poll.

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
