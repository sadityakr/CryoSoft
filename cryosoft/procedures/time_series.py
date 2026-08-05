"""TimeSeries — repeated measurement versus elapsed time, commanding no hardware."""

from __future__ import annotations

import logging
import time
from typing import Any

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.plan import ParamSpec, PhasePlan, StepPlan, Target
from cryosoft.core.procedure import SweepMeasureProcedure

logger = logging.getLogger(__name__)

# The quantities an end condition can watch, and how to read each one.
# ``{choice: (vi_name, reader_method, unit)}``. Reading through the VI's own
# monitored method is the same route ``FieldSweep._axis_readback`` and
# ``TemperatureSweep._axis_readback`` take, so the condition sees exactly the
# value the operator sees on the monitor panel.
_END_CHANNELS: dict[str, tuple[str, str, str]] = {
    "temperature_vti": ("temperature_vti", "temperature", "K"),
    "temperature_sample": ("temperature_sample", "temperature", "K"),
    "magnet_z": ("magnet_z", "magnet_field_T", "T"),
}


class TimeSeries(SweepMeasureProcedure):
    """Measure repeatedly against elapsed time while the operator drives the cryostat.

    The simplest and most permissive shape a procedure can have: it commands
    NO system hardware at all. ``initiate()`` arms the selected measurement
    VI and nothing else, the first reading is taken on the next tick, and
    readings repeat on a fixed cadence until an end condition fires. There
    are no temperature or field parameters here by design — the operator sets
    those by hand on the monitor panel, before or during the run, and the
    series records whatever the sample is doing while they do.

    Two mechanisms make that manual freedom real:

    * **A narrowed claim.** ``claimed_vi_names()`` returns only the
      measurement VI plus any VI taking part in the reading loop, so the
      Orchestrator's admission gate keeps every magnet and temperature front
      panel live for the whole run. Every other procedure claims the whole
      station.
    * **An empty ramp scope.** The run sends no targets, so it owns no ramps
      (see ``Orchestrator._run_ramp_scope``): a manual ramp neither delays
      the next reading nor is stopped when the run ends.

    End conditions:
        ``elapsed time`` — stop after ``max_duration_s``.
        A watched channel (VTI temperature, sample temperature, magnet Z
        field) — stop when it reaches ``end_value``. The approach direction
        is taken from the channel's value at ``initiate()``: starting below
        the threshold stops on the way up, starting above stops on the way
        down, so there is no separate rising/falling parameter to get wrong.
        A channel already at or past the threshold when the run starts
        yields ONE measurement and then stops.

        ``max_duration_s`` always applies as well — it is the length of the
        sweep array, so it caps every run regardless of the condition, and
        keeps the progress bar and "Point n/N" honest. A run ended by its
        watched channel simply stops short of that cap.

    Cadence:
        ``step_time_s`` is measured from the scheduled instant, not from the
        end of the previous reading, so measurement time does not accumulate
        into drift. It is a floor, not a guarantee: one datapoint costs three
        Orchestrator ticks (measure, advance, settle), so the fastest
        achievable cadence is three times the setup's ``tick_interval_ms``
        (about 9 s on a 3 s tick, 3 s on a 1 s tick). A shorter
        ``step_time_s`` than that simply measures every third tick.

    Required VIs in Station:
        At least one measurement VI. A watched end-condition channel must
        exist on the station; it is refused at construction otherwise. No
        system VI is required at all for an elapsed-time run.
    """

    name = "Time Series"
    description = "Measure repeatedly versus elapsed time; command nothing, watch for an end condition"

    # No sweep_axis: the axis is elapsed time, which is not a ramped
    # setpoint, so the GUI's linear/segments/CSV shape editor would be
    # meaningless here. The axis column is declared through axis_data_key()
    # instead, and the point schedule is built below.
    sweep_axis = None
    sweep_data_keys = ["elapsed_s"]
    default_x_key = "elapsed_s"

    sweep_parameters = {
        "step_time_s": ParamSpec(
            type=float,
            default=10.0,
            unit="s",
            min=0.0,
            description=(
                "Time between measurements, counted from the scheduled instant. "
                "Cannot go below three Orchestrator ticks"
            ),
        ),
        "max_duration_s": ParamSpec(
            type=float,
            default=3600.0,
            unit="s",
            min=0.0,
            description=(
                "Hard limit on the run length. Always applies, including when "
                "an end-condition channel is being watched"
            ),
        ),
        "end_condition": ParamSpec(
            type=str,
            default="time",
            choices={
                "Elapsed time only": "time",
                "VTI temperature (K)": "temperature_vti",
                "Sample temperature (K)": "temperature_sample",
                "Field, magnet Z (T)": "magnet_z",
            },
            description=(
                "What ends the run: the maximum duration alone, or a channel "
                "reaching the end value below"
            ),
        ),
        "end_value": ParamSpec(
            type=float,
            default=300.0,
            description=(
                "Threshold the watched channel must reach (K for a temperature "
                "channel, T for a field channel); ignored for elapsed time"
            ),
        ),
        "end_tolerance": ParamSpec(
            type=float,
            default=0.0,
            description=(
                "Stop this far short of the end value (same units). Set it "
                "when the approach is asymptotic and would never quite cross"
            ),
        ),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the procedure and check the watched end-condition channel exists.

        A watched channel whose VI the station does not have is refused here,
        at construction. Running anyway would mean a series with no working
        end condition, stopping only at ``max_duration_s`` while its metadata
        claims it watched a temperature.

        Raises:
            CryoSoftConfigError: If ``end_condition`` names a channel this
                station has no VI for, if ``step_time_s`` is not positive, or
                (via the base class) if the station has no measurement VI.
        """
        super().__init__(*args, **kwargs)

        # Set in initiate(): the run's time origin, and the end-condition
        # baseline. None until then — a procedure that was built but never
        # started has no elapsed time and no approach direction.
        self._t0: float | None = None
        self._approach_sign: float = 1.0

        channel = str(self._params["end_condition"])
        if channel not in _END_CHANNELS:
            return  # "time": no channel to resolve
        vi_name, _reader, unit = _END_CHANNELS[channel]
        if not self._station.has_vi(vi_name):
            raise CryoSoftConfigError(
                f"end_condition={channel!r} watches '{vi_name}', but this "
                f"station has no such VI. Choose another end condition, or "
                f"configure that instrument."
            )
        logger.info(
            "TimeSeries: watching %s until it reaches %g %s (tolerance %g %s)",
            vi_name,
            float(self._params["end_value"]),
            unit,
            float(self._params["end_tolerance"]),
            unit,
        )

    # ------------------------------------------------------------------
    # The axis: scheduled elapsed time
    # ------------------------------------------------------------------

    @classmethod
    def axis_data_key(cls) -> str:
        """Return ``"elapsed_s"`` — this procedure's axis is elapsed time."""
        return "elapsed_s"

    def _build_sweep_array(self) -> list:
        """Build the schedule of measurement instants, in seconds from the start.

        The array is the run's hard length: ``max_duration_s`` divided into
        ``step_time_s`` intervals, first point at zero. An end condition can
        stop the run early but never extends it past this schedule, which is
        what keeps ``get_progress()`` and the HDF5 ``n_sweep_points`` meaningful.

        Returns:
            ``[0.0, step, 2*step, ...]``, the last entry at or below
            ``max_duration_s``.

        Raises:
            CryoSoftConfigError: If ``step_time_s`` is not positive — a
                zero or negative cadence has no schedule.
        """
        step = float(self._params["step_time_s"])
        if step <= 0.0:
            raise CryoSoftConfigError(
                f"TimeSeries: step_time_s must be positive, got {step}."
            )
        duration = max(0.0, float(self._params["max_duration_s"]))
        return [i * step for i in range(int(duration // step) + 1)]

    # ------------------------------------------------------------------
    # Lifecycle: arm the measurement VI, stamp the clock, watch the channel
    # ------------------------------------------------------------------

    def initiate(self) -> PhasePlan:
        """Arm the measurement VI, start the clock, and fix the approach direction.

        Extends the base plan only with bookkeeping — the plan itself carries
        no targets, so nothing on the cryostat moves because this run started.

        Returns:
            The base ``SweepMeasureProcedure`` plan: no targets, the VI's
            arming commands, and zero settle time.
        """
        plan = super().initiate()
        self._t0 = time.monotonic()
        channel = str(self._params["end_condition"])
        if channel in _END_CHANNELS:
            start_value = self._read_end_channel()
            end_value = float(self._params["end_value"])
            # Direction is inferred once, from where the channel sits at the
            # start: approaching from below stops on the way up, from above
            # on the way down. One baseline read, no rising/falling parameter.
            self._approach_sign = 1.0 if end_value > start_value else -1.0
            logger.info(
                "TimeSeries: end channel starts at %g, target %g (approaching %s)",
                start_value,
                end_value,
                "upward" if self._approach_sign > 0 else "downward",
            )
        return plan

    def change_sweep_step(self) -> StepPlan | None:
        """Advance to the next scheduled instant, unless the run should end.

        Returns:
            ``None`` when the watched channel has reached its threshold or
            the schedule is exhausted (``max_duration_s``); otherwise the
            next ``StepPlan`` — no targets, only the wait to the next instant.
        """
        if self._end_condition_met():
            logger.info(
                "TimeSeries: end condition met after %d point(s) — stopping.",
                self._index + 1,
            )
            return None
        return super().change_sweep_step()

    # ------------------------------------------------------------------
    # End condition
    # ------------------------------------------------------------------

    def _read_end_channel(self) -> float:
        """Read the watched channel's current value through its own VI method."""
        vi_name, reader, _unit = _END_CHANNELS[str(self._params["end_condition"])]
        return float(getattr(self._station.get_vi(vi_name), reader)())

    def _end_condition_met(self) -> bool:
        """Return whether the watched channel has reached its threshold.

        Always ``False`` for an elapsed-time run: that condition is the
        schedule itself, enforced by the sweep array's length.

        Returns:
            ``True`` when the channel has reached ``end_value`` (within
            ``end_tolerance``) from the direction it started out on.
        """
        if str(self._params["end_condition"]) not in _END_CHANNELS:
            return False
        value = self._read_end_channel()
        end_value = float(self._params["end_value"])
        tolerance = abs(float(self._params["end_tolerance"]))
        return self._approach_sign * (value - end_value) >= -tolerance

    # ------------------------------------------------------------------
    # Concurrency scope: claim the reading path only
    # ------------------------------------------------------------------

    def claimed_vi_names(self) -> set[str]:
        """Return only the VIs this run actually drives: the reading path.

        The measurement VI plus any VI taking part in a reading-loop slot
        (a switch's route, for instance). Every other VI stays under manual
        front-panel control for the whole run, which is the entire point of
        this procedure — the operator ramps temperature or field by hand
        while the series records the result.

        Returns:
            The measurement VI's name plus each loop slot's VI name.
        """
        return {self._measurement_vi} | {
            slot["vi_name"] for slot in self._loop_slots
        }

    # ------------------------------------------------------------------
    # Axis-specific hooks (SweepMeasureProcedure owns the four-method loop)
    # ------------------------------------------------------------------

    def _initial_system_targets(self) -> dict[str, Target]:
        """No targets — this procedure commands no system hardware."""
        return {}

    def _step_targets(self, index: int) -> dict[str, Target]:
        """No targets — each step only waits for the next scheduled instant."""
        return {}

    def _standby_targets(self) -> dict[str, Target]:
        """No targets — the cryostat is left exactly as the operator has it."""
        return {}

    def _axis_readback(self) -> float:
        """Return the true elapsed time in seconds since ``initiate()``.

        The measured value, not the scheduled one: a point delayed by a slow
        reading records when it actually happened.
        """
        if self._t0 is None:
            return 0.0
        return time.monotonic() - self._t0

    def _initiate_wait_s(self) -> float:
        """No settle time — the first reading is taken as soon as the VI is armed."""
        return 0.0

    def _step_wait_s(self) -> float:
        """Return the wait until the next scheduled instant.

        Counted from the run's time origin rather than from the end of the
        last reading, so a slow measurement is absorbed instead of pushing
        every later point further behind.

        Returns:
            Seconds until this point's scheduled instant, or ``0.0`` if that
            instant has already passed (the reading then happens as soon as
            the tick loop reaches it).
        """
        if self._t0 is None:
            return float(self._params["step_time_s"])
        due = self._t0 + float(self._sweep[self._index])
        return max(0.0, due - time.monotonic())
