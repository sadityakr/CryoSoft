# ---
# description: |
#   IPS120MagnetVI: Virtual Instrument wrapping the Oxford IPS 120-10 magnet
#   power supply.  Works internally in amperes; user-facing units are tesla.
#   Implements a status-driven ramp generator: waits for the driver to report
#   HOLD before sending the next intermediate setpoint.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MagnetBase)
#   - cryosoft.virtual_instruments.rampable (RampableVI)
#   - cryosoft.core.decorators (monitored, control)
#   - cryosoft.core.exceptions (CryoSoftSafetyError)
# input: |
#   drivers = {"main": <IPS120 driver instance>}
#   init_params keys: amperes_per_tesla, default_ramp_rate (A/min),
#   ramp_segments (list of {max_current_A: float, rate_A_per_min: float}
#   sorted ascending), max_current (A), min_current (A).
# process: |
#   start_ramp converts tesla -> amperes, clamps to safety limits, then creates
#   a status-driven generator. advance_ramp() drives the generator. ramp_status()
#   inspects generator exhaustion and hardware status.
# output: |
#   Logged magnet_current (A), get_field (T), magnet_status strings via
#   @monitored; set_field available as @control for manual GUI use.
# last_updated: 2026-04-06
# ---

"""IPS120MagnetVI — Oxford IPS 120-10 magnet power supply Virtual Instrument."""

from __future__ import annotations

from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.virtual_instruments.base import MagnetBase
from cryosoft.virtual_instruments.rampable import RampableVI


class IPS120MagnetVI(MagnetBase, RampableVI):
    """Virtual Instrument for the Oxford IPS 120-10 superconducting magnet PS.

    Ramp behaviour
    --------------
    The IPS 120 ramps to a *current* setpoint continuously. This VI implements
    a status-driven ramp:

    1. ``start_ramp(target_T)`` converts to amperes, clamps, and creates a
       generator that walks through intermediate segments.
    2. ``advance_ramp()`` calls ``next()`` on the generator each Orchestrator
       tick, sending a new setpoint when the driver reports ``"HOLD"``.
    3. ``ramp_status()`` reports ``"RAMPING"``/``"TARGET_REACHED"``/``"IDLE"``.
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        # Conversion factor: how many amperes per tesla for this magnet.
        self._amperes_per_tesla: float = float(init_params.get("amperes_per_tesla", 10.0))

        # Default ramp rate used when no matching segment is found.
        self._default_ramp_rate: float = float(init_params.get("default_ramp_rate", 5.0))

        # Optional list of segment dicts: sorted by max_current_A ascending.
        # Example: [{"max_current_A": 30.0, "rate_A_per_min": 10.0}, ...]
        self._ramp_segments: list[dict] = list(init_params.get("ramp_segments", []))

        # Safety limits (amperes).
        self._max_current: float = float(init_params.get("max_current", 90.0))
        self._min_current: float = float(init_params.get("min_current", -90.0))

        # Ramp generator state.
        self._ramp_gen: Generator | None = None
        self._ramp_exhausted: bool = True   # True when no active ramp

    # ------------------------------------------------------------------
    # RampableVI implementation
    # ------------------------------------------------------------------

    def start_ramp(self, target: float) -> None:
        """Begin ramping to *target* tesla.

        Converts to amperes, clamps to [``min_current``, ``max_current``],
        then initialises the status-driven generator.

        Args:
            target: Target field in tesla.
        """
        target_A = target * self._amperes_per_tesla
        target_A = max(self._min_current, min(self._max_current, target_A))

        self._ramp_gen = self._ramp_generator(target_A)
        self._ramp_exhausted = False
        # Prime the generator to the first yield point.
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    def advance_ramp(self) -> None:
        """Advance the ramp generator by one tick.

        Does nothing if no ramp is active.
        """
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
            ``"RAMPING"``        — generator still running  or hardware ramping.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if self._ramp_exhausted:
            hw_status = self._driver.get_status()  # type: ignore[attr-defined]
            if hw_status == "HOLD":
                return "TARGET_REACHED"
            return "RAMPING"
        return "RAMPING"

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

        Looks up the first segment whose ``max_current_A`` exceeds
        ``abs(current_A)``.  Falls back to ``default_ramp_rate``.

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
        """Return the current magnet output current in amperes."""
        return self._driver.get_current()  # type: ignore[attr-defined]

    @monitored
    def get_field(self) -> float:
        """Return the current magnetic field in tesla."""
        current_A = self._driver.get_current()  # type: ignore[attr-defined]
        return current_A / self._amperes_per_tesla

    @monitored
    def magnet_status(self) -> str:
        """Return the hardware status string (e.g. HOLD, RAMPING, QUENCH)."""
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
        """Put the magnet in HOLD mode on startup."""
        # Most real IPS drivers need an explicit go-to-HOLD command.
        # Sim driver starts in HOLD automatically.

    def standby(self) -> None:
        """Ramp to zero field and hold."""
        self.start_ramp(0.0)
