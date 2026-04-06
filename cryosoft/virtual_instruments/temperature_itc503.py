# ---
# description: |
#   ITC503TemperatureVI: Virtual Instrument wrapping the Oxford ITC 503
#   temperature controller.  Implements a time-based ramp generator that sends
#   a new setpoint each tick based on elapsed time and the configured rate.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (TemperatureControllerBase)
#   - cryosoft.virtual_instruments.rampable (RampableVI)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <ITC503 driver instance>}
#   init_params keys: default_ramp_rate (K/min), tolerance (K),
#   settling_time (s, optional).
# process: |
#   _ramp_generator yields each tick, computing the next intermediate setpoint
#   from time.monotonic(). ramp_status() checks generator exhaustion AND hardware
#   temperature proximity to setpoint within tolerance.
# output: |
#   Logged temperature (K), setpoint (K), heater_output (%) via @monitored;
#   set_temperature available as @control.
# last_updated: 2026-04-06
# ---

"""ITC503TemperatureVI — Oxford ITC 503 temperature controller Virtual Instrument."""

from __future__ import annotations

import time
from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.base import TemperatureControllerBase
from cryosoft.virtual_instruments.rampable import RampableVI


class ITC503TemperatureVI(TemperatureControllerBase, RampableVI):
    """Virtual Instrument for the Oxford ITC 503 temperature controller.

    Ramp behaviour
    --------------
    Uses a **time-based** ramp generator:

    1. ``start_ramp(target_K)`` records the start time and starting temperature,
       then calculates an intermediate setpoint each ``advance_ramp()`` tick.
    2. ``advance_ramp()`` sends the next ``driver.set_setpoint()`` command.
    3. ``ramp_status()`` reports ``"TARGET_REACHED"`` only when the generator is
       exhausted *and* the hardware temperature is within ``tolerance`` of the
       target.
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        # Ramp rate in K/min.
        self._default_ramp_rate: float = float(init_params.get("default_ramp_rate", 5.0))

        # Temperature tolerance for TARGET_REACHED decision (kelvin).
        self._tolerance: float = float(init_params.get("tolerance", 0.5))

        # Internal ramp generator state.
        self._ramp_gen: Generator | None = None
        self._ramp_exhausted: bool = True
        self._ramp_target: float | None = None

    # ------------------------------------------------------------------
    # RampableVI implementation
    # ------------------------------------------------------------------

    def start_ramp(self, target: float) -> None:
        """Begin a time-based temperature ramp to *target* kelvin.

        Args:
            target: Target temperature in kelvin.
        """
        self._ramp_target = float(target)
        rate_per_min = self._default_ramp_rate

        self._ramp_gen = self._ramp_generator(self._ramp_target, rate_per_min)
        self._ramp_exhausted = False
        # Prime the generator.
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
            ``"IDLE"``           — no active ramp.
            ``"RAMPING"``        — generator running or hardware still settling.
            ``"TARGET_REACHED"`` — generator finished and hardware within tolerance.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if not self._ramp_exhausted:
            return "RAMPING"
        # Generator exhausted — check if hardware has settled within tolerance.
        if self._ramp_target is None:
            return "IDLE"
        current_T = self._driver.get_temperature()  # type: ignore[attr-defined]
        if abs(current_T - self._ramp_target) <= self._tolerance:
            return "TARGET_REACHED"
        return "RAMPING"

    # ------------------------------------------------------------------
    # Internal generator
    # ------------------------------------------------------------------

    def _ramp_generator(self, target: float, rate_per_min: float) -> Generator:
        """Time-based ramp generator.

        Computes a new intermediate setpoint from elapsed time each tick and
        sends it to the driver.  Finishes when the computed setpoint equals
        *target*.

        Args:
            target: Final target temperature in kelvin.
            rate_per_min: Ramp rate in K/min.
        """
        driver = self._driver  # type: ignore[attr-defined]
        start_time = time.monotonic()
        start_T: float = driver.get_temperature()

        direction = 1.0 if target > start_T else -1.0
        rate_per_s = rate_per_min / 60.0

        while True:
            elapsed_s = time.monotonic() - start_time
            new_setpoint = start_T + direction * rate_per_s * elapsed_s

            # Clamp to target so we don't overshoot.
            if direction > 0:
                new_setpoint = min(new_setpoint, target)
            else:
                new_setpoint = max(new_setpoint, target)

            driver.set_setpoint(new_setpoint)

            if new_setpoint == target:
                return  # Generator exhausted after sending final setpoint.
            yield  # Return control to Orchestrator tick.

    # ------------------------------------------------------------------
    # @monitored methods
    # ------------------------------------------------------------------

    @monitored
    def temperature(self) -> float:
        """Return the current temperature in kelvin."""
        return self._driver.get_temperature()  # type: ignore[attr-defined]

    @monitored
    def setpoint(self) -> float:
        """Return the current temperature setpoint in kelvin."""
        return self._driver.get_setpoint()  # type: ignore[attr-defined]

    @monitored
    def heater_output(self) -> float:
        """Return the heater output percentage (0–100%)."""
        return self._driver.get_heater_output()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods
    # ------------------------------------------------------------------

    @control
    def set_temperature(self, target_K: float) -> None:
        """Manually command a temperature ramp (GUI use; blocked during procedures).

        Args:
            target_K: Desired temperature in kelvin.
        """
        self.start_ramp(target_K)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Initialise; no special startup command needed for ITC 503."""

    def standby(self) -> None:
        """Put temperature controller in a safe idle state (no action required)."""
