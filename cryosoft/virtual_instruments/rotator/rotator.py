# ---
# description: |
#   RotatorVI: behavior-based Virtual Instrument for a motorized sample-rotation
#   stage (uniaxial or 2D magnet sample orientation). Exposes exactly two
#   controls/properties: sample-angle and rate-sample-angle. Implements a
#   status-driven ramp generator that waits for the driver to report HOLD
#   before sending the next setpoint, mirroring SuperconductingMagnetVI.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (RotatorBase)
#   - cryosoft.virtual_instruments.rampable (RampableVI)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <rotation stage driver instance>}
#   init_params keys: default_rate_deg_per_min, min_angle_deg, max_angle_deg,
#   max_rate_deg_per_min.
# process: |
#   start_ramp clamps to the setup's angle limit and creates a status-driven
#   generator; advance_ramp() drives it each tick. ramp_status() inspects
#   generator exhaustion and hardware status.
# output: |
#   Logged get_sample_angle (deg), get_rate_sample_angle (deg/min),
#   rotator_status via @monitored; set_sample_angle / set_rate_sample_angle
#   available as @control.
# last_updated: 2026-07-18
# ---

"""RotatorVI — behavior-based VI for a motorized sample-rotation stage."""

from __future__ import annotations

import logging
from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.base import RotatorBase
from cryosoft.virtual_instruments.rampable import RampableVI

logger = logging.getLogger(__name__)


class RotatorVI(RotatorBase, RampableVI):
    """Virtual Instrument for a motorized sample-rotation stage.

    Used with a uniaxial or 2D magnet to set the sample's orientation relative
    to the field. Exposes exactly two controls/properties: the sample angle
    and its rotation rate.

    Ramp behaviour
    --------------
    The rotation stage drives to an *angle* setpoint continuously. This VI
    implements a status-driven ramp:

    1. ``start_ramp(target_deg)`` clamps to the setup's angle limit and
       creates a generator that drives toward the target.
    2. ``advance_ramp()`` calls ``next()`` on the generator each Orchestrator
       tick, sending a new setpoint when the driver reports ``"HOLD"``.
    3. ``ramp_status()`` reports ``"RAMPING"``/``"TARGET_REACHED"``/``"IDLE"``.

    Driver contract
    ---------------
    The ``"main"`` driver must implement:
    * ``get_position() -> float``            — current sample angle in degrees
    * ``get_status() -> str``                 — "HOLD" | "MOVING"
    * ``set_position_setpoint(float)``        — set target angle
    * ``set_rate(float)``                     — rotation rate in deg/min

    Optionally:
    * ``hold()`` — freeze the stage where it is (used by ``stop_ramp()``;
      without it the current position is re-sent as the setpoint instead).
    """

    # Control-validation standard (see BaseVirtualInstrument): both the
    # sample angle and its rate are bounded by the setup's limits, populated
    # in __init__ from the config's init_params.
    control_limits = {
        "set_sample_angle": {"target_deg": "angle_deg"},
        "set_rate_sample_angle": {"rate_deg_per_min": "rate_deg_per_min"},
    }

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        self._default_rate: float = float(init_params.get("default_rate_deg_per_min", 1.0))

        max_angle = init_params.get("max_angle_deg")
        min_angle = init_params.get("min_angle_deg")
        self._limits["angle_deg"] = (
            float(min_angle) if min_angle is not None else None,
            float(max_angle) if max_angle is not None else None,
        )
        max_rate = init_params.get("max_rate_deg_per_min")
        self._limits["rate_deg_per_min"] = (
            0.0,
            float(max_rate) if max_rate is not None else None,
        )

        self._rate_deg_per_min: float = self._default_rate
        self._driver.set_rate(self._rate_deg_per_min)  # type: ignore[attr-defined]

        self._ramp_gen: Generator | None = None
        self._ramp_exhausted: bool = True
        self._ramp_target_deg: float | None = None

    # ------------------------------------------------------------------
    # RampableVI implementation
    # ------------------------------------------------------------------

    def start_ramp(self, target: float, persistent: bool = True) -> None:
        """Begin rotating to *target* degrees.

        Args:
            target: Target sample angle in degrees.
            persistent: Ignored — a rotator has no persistent mode. Accepted
                so callers (e.g. Station.process_system_targets) can pass
                ``persistent=`` uniformly across system VI types.
        """
        _ = persistent
        self._ramp_target_deg = float(target)

        self._ramp_gen = self._ramp_generator(self._ramp_target_deg)
        self._ramp_exhausted = False
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    def advance_ramp(self) -> None:
        """Advance the ramp generator by one tick."""
        if self._ramp_gen is None or self._ramp_exhausted:
            return
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    def ramp_status(self) -> str:
        """Return current ramp state.

        Returns:
            ``"IDLE"``           — no generator active.
            ``"TARGET_REACHED"`` — generator exhausted and hardware reports HOLD.
            ``"RAMPING"``        — generator still running or hardware moving.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if self._ramp_exhausted:
            if self._driver.get_status() == "HOLD":  # type: ignore[attr-defined]
                return "TARGET_REACHED"
            return "RAMPING"
        return "RAMPING"

    def stop_ramp(self) -> None:
        """Stop the ramp: kill the generator AND command the stage to hold.

        Clearing the generator alone is not enough — the stage is autonomous
        and keeps moving to its last-commanded setpoint. If the driver
        exposes a ``hold()`` method it is called to freeze the position where
        it is; otherwise the current position is re-sent as the setpoint.
        """
        self._ramp_gen = None
        self._ramp_exhausted = True
        driver = self._driver  # type: ignore[attr-defined]
        hold = getattr(driver, "hold", None)
        if callable(hold):
            hold()
        else:
            driver.set_position_setpoint(driver.get_position())
        self._ramp_target_deg = None

    def ramp_target(self) -> float | None:
        """Return the active angle target in degrees, or ``None`` when idle."""
        return self._ramp_target_deg

    def ramp_rate(self) -> float | None:
        """Return the active rotation rate in degrees/min, or ``None`` when idle."""
        if self._ramp_target_deg is None:
            return None
        return self._rate_deg_per_min

    def ramp_value(self) -> float | None:
        """Return the current sample angle in degrees (the value the ramp drives)."""
        return self.get_sample_angle()

    # ------------------------------------------------------------------
    # Internal generator
    # ------------------------------------------------------------------

    def _ramp_generator(self, target_deg: float) -> Generator:
        driver = self._driver  # type: ignore[attr-defined]

        while True:
            curr = driver.get_position()
            if abs(curr - target_deg) <= 0.01:
                return

            if driver.get_status() == "MOVING":
                yield
                continue

            driver.set_rate(self._rate_deg_per_min)
            driver.set_position_setpoint(target_deg)
            yield

    # ------------------------------------------------------------------
    # @monitored methods
    # ------------------------------------------------------------------

    @monitored
    def get_sample_angle(self) -> float:
        """Return the current sample angle in degrees."""
        return self._driver.get_position()  # type: ignore[attr-defined]

    @monitored
    def get_rate_sample_angle(self) -> float:
        """Return the configured rotation rate in degrees per minute."""
        return self._rate_deg_per_min

    @monitored
    def rotator_status(self) -> str:
        """Return the hardware status string (HOLD or MOVING)."""
        return self._driver.get_status()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods
    # ------------------------------------------------------------------

    @control
    def set_sample_angle(self, target_deg: float) -> None:
        """Manually command a rotation (GUI use; blocked during procedures).

        Args:
            target_deg: Desired sample angle in degrees.
        """
        self.start_ramp(target_deg)

    @control
    def set_rate_sample_angle(self, rate_deg_per_min: float) -> None:
        """Set the rotation rate used by subsequent ``set_sample_angle`` calls.

        Args:
            rate_deg_per_min: Desired rotation rate in degrees per minute.
        """
        self._rate_deg_per_min = rate_deg_per_min
        self._driver.set_rate(rate_deg_per_min)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Put the rotation stage in HOLD mode on startup."""

    def standby(self) -> None:
        """Hold the stage at its current angle."""
        self.stop_ramp()
