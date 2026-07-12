# ---
# description: |
#   SampleTemperatureControllerVI: behavior-based VI for any single-sensor,
#   single-heating-loop temperature controller used on the sample stage.
#   No needle valve. Implements a time-based ramp generator.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (TemperatureControllerBase)
#   - cryosoft.virtual_instruments.rampable (RampableVI)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <temperature controller driver instance>}
#   init_params keys: default_ramp_rate (K/min), tolerance (K).
# process: |
#   _ramp_generator yields each tick, computing the next intermediate setpoint
#   from time.monotonic(). ramp_status() checks generator exhaustion AND hardware
#   temperature proximity to setpoint within tolerance.
# output: |
#   Logged temperature (K), setpoint (K), heater_output (%) via @monitored;
#   set_temperature and set_ramp_rate available as @control.
# last_updated: 2026-04-19
# ---

"""SampleTemperatureControllerVI — behavior-based VI for sample-stage temperature control."""

from __future__ import annotations

import time
from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.base import TemperatureControllerBase
from cryosoft.virtual_instruments.rampable import RampableVI


class SampleTemperatureControllerVI(TemperatureControllerBase, RampableVI):
    """Virtual Instrument for a single-sensor, single-loop sample temperature controller.

    This VI controls the sample stage temperature. It has no needle valve.
    Use ``VTITemperatureControllerVI`` for the VTI (bath) temperature, which
    includes needle valve control.

    Ramp behaviour
    --------------
    Uses a **time-based** ramp generator:

    1. ``start_ramp(target_K)`` records the start time and starting temperature,
       then calculates an intermediate setpoint each ``advance_ramp()`` tick.
    2. ``advance_ramp()`` sends the next ``driver.set_setpoint()`` command.
    3. ``ramp_status()`` reports ``"TARGET_REACHED"`` only when the generator is
       exhausted *and* the hardware temperature is within ``tolerance`` of target.

    Driver contract
    ---------------
    The ``"main"`` driver must implement:
    * ``get_temperature() -> float``  — current temperature in Kelvin
    * ``get_setpoint() -> float``     — current setpoint in Kelvin
    * ``set_setpoint(float)``         — set target temperature
    * ``get_heater_output() -> float`` — heater power 0–100%
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        self._default_ramp_rate: float = float(init_params.get("default_ramp_rate", 5.0))
        self._tolerance: float = float(init_params.get("tolerance", 0.5))

        self._ramp_gen: Generator | None = None
        self._ramp_exhausted: bool = True
        self._ramp_target: float | None = None

    # ------------------------------------------------------------------
    # RampableVI implementation
    # ------------------------------------------------------------------

    def start_ramp(self, target: float, rate: float | None = None) -> None:
        """Begin a time-based temperature ramp to *target* kelvin.

        Args:
            target: Target temperature in kelvin.
            rate: Ramp rate in K/min. If None, uses ``_default_ramp_rate``.
        """
        self._ramp_target = float(target)
        rate_per_min = float(rate) if rate is not None else self._default_ramp_rate

        self._ramp_gen = self._ramp_generator(self._ramp_target, rate_per_min)
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
            ``"IDLE"``           — no active ramp.
            ``"RAMPING"``        — generator running or hardware still settling.
            ``"TARGET_REACHED"`` — generator finished and hardware within tolerance.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if not self._ramp_exhausted:
            return "RAMPING"
        if self._ramp_target is None:
            return "IDLE"
        current_T = self._driver.get_temperature()  # type: ignore[attr-defined]
        if abs(current_T - self._ramp_target) <= self._tolerance:
            return "TARGET_REACHED"
        return "RAMPING"

    def stop_ramp(self) -> None:
        """Stop the ramp: kill the generator and pin the setpoint where we are.

        The controller would otherwise keep regulating toward the
        last-commanded intermediate setpoint; pinning the setpoint to the
        current temperature freezes the system at its present state.
        """
        self._ramp_gen = None
        self._ramp_exhausted = True
        self._ramp_target = None
        driver = self._driver  # type: ignore[attr-defined]
        driver.set_setpoint(driver.get_temperature())

    # ------------------------------------------------------------------
    # Internal generator
    # ------------------------------------------------------------------

    def _ramp_generator(self, target: float, rate_per_min: float) -> Generator:
        driver = self._driver  # type: ignore[attr-defined]
        start_time = time.monotonic()
        start_T: float = driver.get_temperature()

        direction = 1.0 if target > start_T else -1.0
        rate_per_s = rate_per_min / 60.0

        while True:
            elapsed_s = time.monotonic() - start_time
            new_setpoint = start_T + direction * rate_per_s * elapsed_s

            if direction > 0:
                new_setpoint = min(new_setpoint, target)
            else:
                new_setpoint = max(new_setpoint, target)

            driver.set_setpoint(new_setpoint)

            if new_setpoint == target:
                return
            yield

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
    def set_ramp_rate(self, rate_K_per_min: float) -> None:
        """Change the default temperature ramp rate.

        Args:
            rate_K_per_min: New ramp rate in kelvin per minute.
        """
        self._default_ramp_rate = float(rate_K_per_min)

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
        """Initialise; no special startup command needed."""

    def standby(self) -> None:
        """Put temperature controller in a safe idle state (no action required)."""
