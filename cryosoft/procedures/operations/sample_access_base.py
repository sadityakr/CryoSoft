"""_SampleAccessOperationBase — shared logic for Sample Load / Sample Unload."""

from __future__ import annotations

import logging
import time
from typing import Any

from cryosoft.core.decorators import get_monitored_methods
from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.gates import Gate
from cryosoft.core.operation import (
    STEP_KIND_AUTO_RAMP,
    STEP_KIND_OPERATOR_ACK,
    STEP_STATUS_DONE,
    STEP_STATUS_SKIPPED,
    OperationBase,
    OperationStep,
    ReadinessCondition,
)
from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.station import Station

logger = logging.getLogger(__name__)

# The only needle-valve mode implemented today: a manual valve becomes an
# operator confirmation step.
#
# This is a property of the setup, not a gap in the stack. The VTI
# temperature controller VI has exposed set_needle_valve() and
# set_needle_valve_mode() as @control methods for a while, and its standby()
# already closes the valve — so an "auto" mode that commands MANUAL then 0 %
# and gates on the position readback is implementable today with no new
# driver or VI work. It is not wired up because the 12 T cryostat this runs
# on has no motor on its needle valve: the controller channel exists, but
# commanding it moves nothing. On that setup the operator's confirmation is
# the only honest verification there is, and pretending otherwise would be
# worse than asking. A setup with a motorised valve should add
# needle_valve: "auto" here — the step and gate machinery below already
# supports a machine-checked step, which is why steps and gates are
# declared rather than hardcoded.
_NEEDLE_VALVE_MANUAL = "manual"

# Step keys. Declared as constants because the GUI, the postcondition gates,
# and the servicing-log notes all key off them.
_STEP_WARM_VTI = "warm_vti"
_STEP_CLOSE_NEEDLE_VALVE = "close_needle_valve"
_STEP_LOAD_UNLOAD_SAMPLE = "load_unload_sample"


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

    ``tolerated_safety_flags = frozenset({"helium_low"})``: unlike the
    helium fill, loading or unloading a sample does nothing to *fix* a
    helium-low condition, but an operator who needs a sample in or out
    should not be locked out of doing so just because the reservoir is
    also running low — the two are independent operational needs. A
    non-tolerated flag (e.g. ``quench``) still aborts the run exactly like
    any other operation.

    The step sequence
    -------------------------------------------------------------------
    A sample change is a sequence, not one undifferentiated wait, and the
    main thing this operation exists to produce is a record of *when* each
    part of it happened. ``steps()`` declares that sequence (see the step
    standard in ``OperationBase``'s class docstring):

    1. ``warm_vti`` (``auto_ramp``) — the VTI ramp to
       ``target_temperature_K``, the one part the software performs. It
       completes on its own, recorded by ``sample()`` the first tick the
       reading is within tolerance.
    2. ``close_needle_valve`` (``operator_ack``) — declared only while
       ``needle_valve == "manual"``; see the module-level note on why that
       is the only mode wired up.
    3. ``load_unload_sample`` (``operator_ack``) — the physical sample
       change itself (valve, rod, flush — the wiki-documented procedure),
       confirmed as one step rather than broken into its own sub-sequence.
       Identical between the two concrete classes: neither needs its own
       wording, unlike the old per-step labels this replaced.

    Every step is skippable, including the warm-up. This is deliberate and
    is the main behavioural change from the operation's first design: a
    sample sometimes has to be changed at base temperature, and an
    operation that refuses to run then does not prevent the sample change,
    it only means the sample change happens with no record of it at all.
    So a skip always succeeds, always after a warning naming the live
    conditions, and is recorded as an override — the skipped step's
    postcondition gate reports unmet, which the run manifest and the
    servicing-log notes both carry.

    Skipping the warm-up additionally has to stop the ramp it started, or
    the VTI would go on climbing to 290 K while the operator works at 4 K.
    The operation cannot do that itself — while the ramp runs the
    Orchestrator is in RAMPING, where ``step()`` is never called — so
    ``skip_step()`` sets ``skip_ramp_requested`` and
    ``Orchestrator.skip_operation_step()`` stops the ramp in place, leaving
    the VTI clamped at whatever temperature it had reached.

    Readiness (Operations panel): ``readiness_conditions()`` returns
    ``zero_field`` (every magnet's ``magnet_state() == "standby"``, which
    already implies the switch heater is off wherever one exists — see
    GLOSSARY.md's **Magnet state**) followed by one live row per declared
    step, so the panel's checklist *is* the sequence. No ``next_due()``
    override -- neither operation has a schedule.
    """

    #: Hold-phase operation: the ready banner may show mid-run, not only after
    #: Finish — see ``OperationBase.hold_for_operator``'s docstring.
    hold_for_operator = True

    #: Sample access does not fix a helium-low condition, but it should not
    #: be blocked by one either — see the class docstring.
    tolerated_safety_flags = frozenset({"helium_low"})

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

        #: Read by ``Orchestrator.skip_operation_step()``: set when the
        #: operator skips an ``auto_ramp`` step, meaning "stop the ramp this
        #: operation started and clamp where you are". The Orchestrator
        #: clears it once it has acted. Public because it is a cross-layer
        #: signal, not internal state.
        self.skip_ramp_requested: bool = False

        #: Frozen at ``initiate()``: every ``<vi>.<monitored>`` channel that
        #: yields a numeric reading, so the recording spans the whole
        #: station. Empty until then.
        self._recording_channels_frozen: list[str] = []

    # ------------------------------------------------------------------
    # Steps — the declared sequence (see the step standard in
    # OperationBase's class docstring)
    # ------------------------------------------------------------------

    def steps(self) -> tuple[OperationStep, ...]:
        """Return the ordered sample-access sequence.

        Returns:
            Three steps for the usual manual-needle-valve setup (``warm_vti``,
            ``close_needle_valve``, ``load_unload_sample``), two if a future
            ``needle_valve`` mode makes the needle-valve step
            machine-verified and it drops out. Identical between the load
            and unload subclasses.
        """

        def _vti_temperature(state: dict[str, Any]) -> float | None:
            """Return the VTI reading from a state snapshot, or None if unusable."""
            value = state.get(self._vti_vi_name, {}).get("temperature")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value)

        def _warm_detail(state: dict[str, Any]) -> str:
            temperature = _vti_temperature(state)
            if temperature is None:
                return "reading unavailable"
            return f"currently {temperature:.1f} K"

        def _warm_skip_warning(state: dict[str, Any]) -> str:
            temperature = _vti_temperature(state)
            where = (
                "The VTI reading is unavailable"
                if temperature is None
                else f"The VTI is at {temperature:.1f} K"
            )
            return (
                f"{where}, not the {self._target_temperature_K:.0f} K this "
                f"step ramps to. Skipping stops the ramp and leaves the VTI "
                f"clamped where it is. Opening the cryostat below room "
                f"temperature will condense air and moisture in the sample "
                f"space. The skip will be recorded on this run."
            )

        steps: list[OperationStep] = [
            OperationStep(
                key=_STEP_WARM_VTI,
                label=f"Warm the VTI to {self._target_temperature_K:.0f} K",
                kind=STEP_KIND_AUTO_RAMP,
                detail=_warm_detail,
                skip_warning=_warm_skip_warning,
            )
        ]
        if self._needle_valve == _NEEDLE_VALVE_MANUAL:
            steps.append(
                OperationStep(
                    key=_STEP_CLOSE_NEEDLE_VALVE,
                    label="Close the needle valve",
                    kind=STEP_KIND_OPERATOR_ACK,
                    skip_warning=lambda _state: (
                        "Leaving the needle valve open keeps bath helium "
                        "flowing into the VTI while the cryostat is open, "
                        "wasting helium and icing the sample space."
                    ),
                )
            )
        steps.extend(
            [
                OperationStep(
                    key=_STEP_LOAD_UNLOAD_SAMPLE,
                    label="Load or unload sample. Follow the steps from the wiki.",
                    kind=STEP_KIND_OPERATOR_ACK,
                )
            ]
        )
        return tuple(steps)

    def step_conditions_snapshot(self) -> dict[str, Any]:
        """Return a flat snapshot of the station's cached state for a step record.

        This is where the *non-numeric* monitored values land — the needle
        valve's AUTO/MANUAL mode, each magnet's state — which the numeric
        recording cannot hold. Cached read only: this runs inside a GUI
        callback, not the tick loop, so it must never touch the bus.

        Returns:
            ``{"<vi>.<field>": value}`` over every VI in the cached state,
            keeping strings and numbers alike, dropping the ``_stale`` /
            ``_disconnected`` bookkeeping keys the Station adds.
        """
        snapshot: dict[str, Any] = {}
        for vi_name, fields in self._station.cached_state.items():
            if not isinstance(fields, dict):
                continue
            for field, value in fields.items():
                if field.startswith("_"):
                    continue
                if isinstance(value, (int, float, str, bool)):
                    snapshot[f"{vi_name}.{field}"] = value
        return snapshot

    def skip_step(self, key: str) -> None:
        """Record a skipped step, and flag a ramp stop when it was the warm-up.

        Extends the base implementation with the one thing an ``auto_ramp``
        step needs that an ``operator_ack`` step does not: the ramp it
        started must actually stop, which only the Orchestrator can do (see
        the class docstring).

        Args:
            key: One of ``steps()``' keys.

        Raises:
            ValueError: If ``key`` is not a declared step.
        """
        already_recorded = key in self.step_records()
        super().skip_step(key)
        if already_recorded:
            return
        if self._step_by_key(key).kind == STEP_KIND_AUTO_RAMP:
            self.skip_ramp_requested = True
            logger.warning(
                "%s: operator skipped %r — requesting the %s ramp be "
                "stopped and clamped where it is.",
                type(self).__name__,
                key,
                self._vti_vi_name,
            )

    # ------------------------------------------------------------------
    # Operator confirmations — the Orchestrator-facing names
    # ------------------------------------------------------------------

    def confirm(self, key: str) -> None:
        """Record an operator confirmation for a declared step.

        The name the Orchestrator calls duck-typed from
        ``confirm_operation(key)``; delegates to the step standard's
        ``confirm_step()``. Never sets hardware — purely a human attestation
        about a physical action, consumed by ``postcondition_gates()``.

        Args:
            key: One of ``steps()``' keys.

        Raises:
            ValueError: If ``key`` is not a declared step.
        """
        self.confirm_step(key)

    def confirmed(self, key: str) -> bool:
        """Return whether ``key`` has been confirmed *done* (not skipped).

        Args:
            key: One of ``steps()``' keys.

        Returns:
            True once the step has been recorded ``done``; False otherwise —
            including when it was skipped, and for an unknown key (this is a
            read, it never raises).
        """
        record = self.step_records().get(key)
        return record is not None and record.status == STEP_STATUS_DONE

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
        """Return the station-wide recording and the step timeline, for the run manifest.

        Called once by the Orchestrator on ``run_finished``; ``CryogenicsRecorder``
        reads this back off ``manifest["summary"]`` and writes it as this
        run's ``recordings/<run_id>.json`` sidecar, referenced from the run's
        single ``servicing`` log entry. The step timeline rides along inside
        the same summary rather than through a second channel, so no
        session-layer change is needed to persist it.

        Returns:
            ``{"recording": {"unix_time": [...], "channels": {...}},
            "steps": [...]}`` — ``OperationBase._recording_dict()`` and
            ``steps_summary()`` verbatim. The series is empty if the hold
            phase never sampled (e.g. the run finished before ``sample()``
            was ever called); every declared step always appears in
            ``steps``, pending ones included.
        """
        return {"recording": self._recording_dict(), "steps": self.steps_summary()}

    # ------------------------------------------------------------------
    # Operations panel: readiness — no next_due() override, neither
    # operation has a schedule (the OperationBase default, None, is
    # exactly right).
    # ------------------------------------------------------------------

    def readiness_conditions(self) -> tuple[ReadinessCondition, ...]:
        """Return the magnet check plus one live row per declared step.

        Every ``check``/``detail`` closure reads only the state snapshot
        passed to it (never ``self._station.cached_state`` directly), per
        the readiness-condition contract — with one deliberate exception:
        the per-step rows read ``step_records()``, which is the operation's
        own in-memory outcome log, not hardware. ``zero_field`` depends
        solely on ``magnet_state() == "standby"`` (GLOSSARY.md's **Magnet
        state**), which already implies the switch heater is off wherever a
        magnet has one — there is no separate heater check.

        Returns:
            ``zero_field`` first, then one row per ``steps()`` entry in
            sequence order, so the panel's checklist reads as the procedure
            the operator is working through.
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

        conditions = [
            ReadinessCondition(
                key="zero_field",
                label="All magnets at zero field",
                check=_zero_field_holds,
                detail=_zero_field_detail,
            )
        ]

        def _step_row(step: OperationStep) -> ReadinessCondition:
            """Build the live checklist row for one declared step."""

            def _holds(_state: dict[str, Any], key: str = step.key) -> bool:
                record = self.step_records().get(key)
                return record is not None and record.status == STEP_STATUS_DONE

            def _detail(state: dict[str, Any], step: OperationStep = step) -> str:
                record = self.step_records().get(step.key)
                if record is not None and record.status == STEP_STATUS_SKIPPED:
                    return "skipped by operator"
                if record is not None:
                    return "done"
                return step.detail(state) if step.detail else "pending"

            return ReadinessCondition(
                key=step.key,
                label=step.label,
                check=_holds,
                detail=_detail,
            )

        conditions.extend(_step_row(step) for step in self.steps())
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
        self._reset_steps()
        self.skip_ramp_requested = False
        self._recording_channels_frozen = self._discover_recording_channels()
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

    def _discover_recording_channels(self) -> list[str]:
        """Return every ``<vi>.<monitored>`` channel that currently reads numeric.

        The recording is deliberately station-wide: the question a sample
        change's log entry has to answer is "what was the whole system doing
        while the cryostat was open", which is not answerable from the VTI
        and the magnets alone. Rather than curate a list that would go stale
        the moment a config gains an instrument, every ``@monitored`` method
        on every registered VI is a candidate, so a newly configured
        instrument — a pressure gauge, say — appears in the record with no
        change here.

        Channels whose current reading is not numeric are excluded, because
        ``_record_sample()`` holds floats only and requires a channel set
        that never changes mid-run. Their values are captured per step
        instead (``step_conditions_snapshot()``).

        Whether a channel reads numerically can only be answered from an
        actual reading, so this needs a populated state. It normally is —
        the Orchestrator has been polling since monitoring started — but a
        station built and run in one go (a test, a headless script) has an
        empty cache at ``initiate()``, which would silently freeze an empty
        channel set and record nothing at all. One explicit poll in that
        case, at run start only, is worth far more than a silently empty
        recording.

        Returns:
            Sorted ``"<vi>.<method>"`` channel names. Sorted, not
            registration-ordered, so the recorded channel set is stable
            across runs regardless of VI registration order.
        """
        state = self._station.cached_state
        if not state:
            state = self._station.get_state()
        channels: list[str] = []
        for vi_name in self._station.get_vi_names():
            try:
                vi = self._station.get_vi(vi_name)
            except KeyError:  # pragma: no cover - registry consistency
                continue
            fields = state.get(vi_name, {})
            for method_name in get_monitored_methods(vi):
                value = fields.get(method_name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                channels.append(f"{vi_name}.{method_name}")
        return sorted(channels)

    def sample(self) -> None:
        """Record every numeric monitored channel on the station (the hold-phase recording).

        Called once per tick (``measure()`` adapter), throttled to
        ``sample_period_s`` by ``step()``'s ``wait_s`` — mirrors
        ``HeliumFillOperation.sample()``'s cadence exactly.

        Reads ``cached_state`` rather than calling each VI live. Every other
        ``sample()`` in the codebase takes a genuine per-tick reading, and
        this one used to as well, but that does not scale to a station-wide
        recording: a live read of every monitored method on every instrument
        would put dozens of extra GPIB transactions in the tick path, which
        the single-threaded cooperative design does not allow. The
        Orchestrator has already polled every VI into ``cached_state`` this
        same tick, so this is the same data at no additional bus cost.

        Also completes the ``warm_vti`` step the first tick the VTI reads
        within tolerance — that step is the one part of the sequence the
        system performs, so the system, not the operator, records it done.
        """
        now = time.time()
        state = self._station.cached_state
        values: dict[str, float] = {}
        for channel in self._recording_channels_frozen:
            vi_name, _, field = channel.partition(".")
            value = state.get(vi_name, {}).get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                # A channel that has stopped reading numerically (a
                # disconnected instrument) must still occupy its slot:
                # _record_sample() rejects a changed channel set, and a
                # gap is more honest than dropping the whole sample.
                values[channel] = float("nan")
                continue
            values[channel] = float(value)
        self._record_sample(now, values)

        if self._vti_at_target(state):
            self._record_step_if_pending(_STEP_WARM_VTI, STEP_STATUS_DONE)

    def _record_step_if_pending(self, key: str, status: str) -> None:
        """Stamp a step outcome only if that step is declared and still pending.

        Args:
            key: A step key that may or may not be declared by this
                instance's ``steps()``.
            status: The outcome to record.
        """
        if key in self.step_records():
            return
        if not any(step.key == key for step in self.steps()):
            return
        self._record_step(key, status)

    def _vti_at_target(self, state: dict[str, Any]) -> bool:
        """Return whether the VTI reading in ``state`` is within tolerance of target.

        Args:
            state: A ``{vi_name: {field: value}}`` snapshot.

        Returns:
            True when the reading exists, is numeric, and is within
            ``temperature_tolerance_K`` of ``target_temperature_K``.
        """
        temperature = state.get(self._vti_vi_name, {}).get("temperature")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            return False
        return abs(float(temperature) - self._target_temperature_K) <= (
            self._temperature_tolerance_K
        )

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
            ``vti_at_target`` (always); and one ``step_<key>`` gate per
            declared step, met only when that step was recorded ``done``.
            ``vti_at_target`` and ``step_warm_vti`` are deliberately both
            present and are not the same assertion: the first is a live
            reading now, the second is whether the warm-up was ever
            completed rather than skipped. A run that warmed up and then
            drifted fails the first only; a run that skipped the warm-up
            fails both.
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

        gates.append(
            Gate(
                "vti_at_target",
                check=lambda: self._vti_at_target(self._station.cached_state),
                window_s=self._temperature_window_s,
            )
        )

        # One gate per declared step, so a skipped step is reported exactly
        # like any other unverified postcondition — named in the manifest's
        # postconditions_unmet list and carried into the servicing-log
        # notes, without ever blocking the run from completing. This is why
        # skipping is safe to offer on every step: the override is recorded,
        # not silently swallowed.
        for step in self.steps():
            gates.append(
                Gate(
                    f"step_{step.key}",
                    check=lambda key=step.key: self.confirmed(key),
                    window_s=0.0,
                )
            )

        return tuple(gates)
