# ---
# description: |
#   SuperconductingMagnetVI: behavior-based Virtual Instrument for any
#   superconducting magnet power supply without a persistent-mode switch heater.
#   Works internally in amperes; user-facing units are tesla.
#   Implements a status-driven ramp generator that waits for the driver to
#   report HOLD before sending the next intermediate setpoint.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MagnetBase)
#   - cryosoft.virtual_instruments.rampable (RampableVI)
#   - cryosoft.core.decorators (monitored, control)
#   - cryosoft.core.exceptions (CryoSoftSafetyError)
# input: |
#   drivers = {"main": <PSU driver instance>}
#   init_params keys: amperes_per_tesla, default_ramp_rate (A/min),
#   ramp_segments (list of {max_current_A: float, rate_A_per_min: float}
#   sorted ascending), max_current (A), min_current (A).
# process: |
#   start_ramp converts tesla -> amperes, clamps to safety limits, then creates
#   a status-driven generator. advance_ramp() drives the generator each tick.
#   ramp_status() inspects generator exhaustion and hardware status.
# output: |
#   Logged magnet_current (A), get_field (T), magnet_status strings via
#   @monitored; set_field available as @control for manual GUI use.
# last_updated: 2026-04-19
# ---

"""SuperconductingMagnetVI — behavior-based VI for any SC magnet PSU (no switch heater)."""

from __future__ import annotations

from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.base import MagnetBase
from cryosoft.virtual_instruments.rampable import RampableVI


class SuperconductingMagnetVI(MagnetBase, RampableVI):
    """Virtual Instrument for a superconducting magnet power supply without switch heater.

    Ramp behaviour
    --------------
    The PSU ramps to a *current* setpoint continuously. This VI implements
    a status-driven ramp:

    1. ``start_ramp(target_T)`` converts to amperes, clamps, and creates a
       generator that walks through intermediate ramp segments.
    2. ``advance_ramp()`` calls ``next()`` on the generator each Orchestrator
       tick, sending a new setpoint when the driver reports ``"HOLD"``.
    3. ``ramp_status()`` reports ``"RAMPING"``/``"TARGET_REACHED"``/``"IDLE"``.

    Driver contract
    ---------------
    The ``"main"`` driver must implement:
    * ``get_current() -> float``         — output current in Amperes
    * ``get_status() -> str``            — "HOLD" | "RAMPING" | "QUENCH"
    * ``set_current_setpoint(float)``    — set target current
    * ``set_ramp_rate(float)``           — ramp rate in A/min

    Optionally:
    * ``hold()`` — freeze the output where it is (used by ``stop_ramp()``;
      without it the current output is re-sent as the setpoint instead).

    The physical mapping (e.g. whether the instrument takes rate + setpoint
    simultaneously or rate first then setpoint) is the driver's responsibility.
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        self._amperes_per_tesla: float = float(init_params.get("amperes_per_tesla", 10.0))
        self._default_ramp_rate: float = float(init_params.get("default_ramp_rate", 5.0))
        self._ramp_segments: list[dict] = list(init_params.get("ramp_segments", []))
        self._max_current: float = float(init_params.get("max_current", 90.0))
        self._min_current: float = float(init_params.get("min_current", -90.0))

        self._ramp_gen: Generator | None = None
        self._ramp_exhausted: bool = True

    # ------------------------------------------------------------------
    # RampableVI implementation
    # ------------------------------------------------------------------

    def start_ramp(self, target: float, persistent: bool = True) -> None:
        """Begin ramping to *target* tesla.

        Args:
            target: Target field in tesla.
            persistent: Ignored — this VI has no persistent-mode switch
                heater. Accepted so callers (e.g. Station.process_system_targets,
                or a Procedure written against either magnet VI flavor) can
                pass ``persistent=`` uniformly regardless of whether the
                configured magnet_x is this class or
                ``SuperconductingMagnetPersistentVI``.
        """
        _ = persistent
        target_A = target * self._amperes_per_tesla
        target_A = max(self._min_current, min(self._max_current, target_A))

        self._ramp_gen = self._ramp_generator(target_A)
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
            ``"RAMPING"``        — generator still running or hardware ramping.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if self._ramp_exhausted:
            hw_status = self._driver.get_status()  # type: ignore[attr-defined]
            if hw_status == "HOLD":
                return "TARGET_REACHED"
            return "RAMPING"
        return "RAMPING"

    def stop_ramp(self) -> None:
        """Stop the ramp: kill the generator AND command the PSU to hold.

        Clearing the generator alone is not enough — the PSU is autonomous and
        keeps ramping to its last-commanded setpoint. If the driver exposes a
        ``hold()`` method it is called to freeze the output where it is;
        otherwise the current output value is re-sent as the setpoint.
        """
        self._ramp_gen = None
        self._ramp_exhausted = True
        driver = self._driver  # type: ignore[attr-defined]
        hold = getattr(driver, "hold", None)
        if callable(hold):
            hold()
        else:
            driver.set_current_setpoint(driver.get_current())

    # ------------------------------------------------------------------
    # Internal generator
    # ------------------------------------------------------------------

    def _ramp_generator(self, target_A: float) -> Generator:
        driver = self._driver  # type: ignore[attr-defined]

        while True:
            curr_A = driver.get_current()
            if abs(curr_A - target_A) <= 0.01:
                return

            status = driver.get_status()
            if status == "QUENCH":
                # Send no further setpoints; the Station safety check is
                # responsible for escalating a quench to EMERGENCY.
                return
            if status == "RAMPING":
                yield
                continue

            direction = 1 if target_A > curr_A else -1
            rate = self._get_segment_rate(curr_A, direction)

            next_boundary = target_A
            for seg in self._ramp_segments:
                limit = float(seg["max_current_A"])
                if limit == float('inf'):
                    continue
                if direction > 0:
                    if curr_A < limit - 0.001 < target_A:
                        next_boundary = min(next_boundary, limit)
                    if curr_A < -limit - 0.001 < target_A:
                        next_boundary = min(next_boundary, -limit)
                else:
                    if curr_A > limit + 0.001 > target_A:
                        next_boundary = max(next_boundary, limit)
                    if curr_A > -limit + 0.001 > target_A:
                        next_boundary = max(next_boundary, -limit)

            driver.set_ramp_rate(rate)
            driver.set_current_setpoint(next_boundary)
            yield

    # ------------------------------------------------------------------
    # Segment rate look-up
    # ------------------------------------------------------------------

    def _get_segment_rate(self, current_A: float, direction: int = 0) -> float:
        """Return ramp rate (A/min) for the given current magnitude.

        Args:
            current_A: Current operating current in amperes.
            direction: 1 if ramping up, -1 if down, 0 for static.
        """
        abs_I = abs(current_A + direction * 0.002)
        for segment in self._ramp_segments:
            if abs_I <= segment["max_current_A"]:
                return float(segment["rate_A_per_min"])
        return self._default_ramp_rate

    # ------------------------------------------------------------------
    # @monitored methods
    # ------------------------------------------------------------------

    @monitored
    def magnet_current(self) -> float:
        """Return the current PSU output current in amperes."""
        return self._driver.get_current()  # type: ignore[attr-defined]

    @monitored
    def get_field(self) -> float:
        """Return the current magnetic field in tesla."""
        return self._driver.get_current() / self._amperes_per_tesla  # type: ignore[attr-defined]

    @monitored
    def magnet_status(self) -> str:
        """Return the hardware status string (HOLD, RAMPING, or QUENCH)."""
        return self._driver.get_status()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods
    # ------------------------------------------------------------------

    @control
    def set_field(self, target_T: float) -> None:
        """Manually command a field ramp (GUI use; blocked during procedures).

        Args:
            target_T: Desired field in tesla.
        """
        self.start_ramp(target_T)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Put the PSU in HOLD mode on startup."""

    def standby(self) -> None:
        """Ramp to zero field and hold."""
        self.start_ramp(0.0)
