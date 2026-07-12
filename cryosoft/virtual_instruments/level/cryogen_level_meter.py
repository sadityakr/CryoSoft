# ---
# description: |
#   CryogenLevelMeterVI: behavior-based VI for any cryogen level meter.
#   Standardizes the refresh rate to a three-mode contract: STANDBY (0),
#   SLOW (1), FAST (2). Any real driver that maps its native modes to these
#   three integers can back this VI. Implements a readings buffer for the
#   helium_low() safety check to filter single-point dips.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (LevelMeterBase)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <level meter driver instance>}
#   init_params keys: helium_low_threshold (float, default 20.0),
#   buffer_size (int, default 5).
# process: |
#   Each call to helium_level() appends to a circular buffer (deque).
#   helium_low() returns True if the statistical mode of the buffer is below
#   the threshold. Three-mode standard: 0=STANDBY, 1=SLOW, 2=FAST.
# output: |
#   Logged helium_level (%), nitrogen_level (%), get_refresh_rate (int)
#   via @monitored; set_refresh_rate available as @control.
# last_updated: 2026-04-19
# ---

"""CryogenLevelMeterVI — behavior-based VI for any cryogen level meter."""

from __future__ import annotations

from collections import deque
from statistics import mode
from typing import Any

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.base import LevelMeterBase


# Three-mode standard constants
STANDBY = 0
SLOW = 1
FAST = 2


class CryogenLevelMeterVI(LevelMeterBase):
    """Virtual Instrument for a cryogen level meter.

    Refresh mode standard
    ---------------------
    The VI enforces a three-mode interface that any driver can be mapped to:

    * ``0`` — STANDBY: measurements paused, lowest power.
    * ``1`` — SLOW: continuous slow-rate polling (normal operation).
    * ``2`` — FAST: rapid polling used during a helium fill.

    The mapping from native instrument modes to these three values is the
    driver's responsibility.

    Safety buffer
    -------------
    ``helium_level()`` appends each reading to a fixed-size deque.
    ``helium_low()`` computes the statistical mode of the buffer to suppress
    transient single-point dips.
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        self._helium_low_threshold: float = float(
            init_params.get("helium_low_threshold", 20.0)
        )
        self._buffer_size: int = int(init_params.get("buffer_size", 5))
        self._helium_buffer: deque[bool] = deque(
            [False] * self._buffer_size, maxlen=self._buffer_size
        )

    # ------------------------------------------------------------------
    # @monitored methods
    # ------------------------------------------------------------------

    @monitored
    def helium_level(self) -> float:
        """Return the current helium level in percent and update safety buffer."""
        level = self._driver.get_helium_level()  # type: ignore[attr-defined]
        self._helium_buffer.append(level < self._helium_low_threshold)
        return level

    @monitored
    def nitrogen_level(self) -> float:
        """Return the current nitrogen level in percent."""
        return self._driver.get_nitrogen_level()  # type: ignore[attr-defined]

    @monitored
    def get_refresh_rate(self) -> int:
        """Return the current refresh rate mode (0=STANDBY, 1=SLOW, 2=FAST)."""
        return self._driver.get_refresh_rate()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods
    # ------------------------------------------------------------------

    @control
    def set_refresh_rate(self, mode: int) -> None:
        """Set the refresh rate mode.

        Args:
            mode: 0 (STANDBY), 1 (SLOW), or 2 (FAST).

        Raises:
            ValueError: If mode is not 0, 1, or 2.
        """
        if mode not in (STANDBY, SLOW, FAST):
            raise ValueError(f"Refresh rate mode must be 0, 1, or 2, got {mode}")
        self._driver.set_refresh_rate(mode)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Safety helper (not @monitored — called by Station safety checks)
    # ------------------------------------------------------------------

    def helium_low(self) -> bool:
        """Return True if the helium level is critically low.

        Uses majority vote of recent readings to avoid reacting to single dips.
        Returns False until the buffer has at least one reading.
        """
        if not self._helium_buffer:
            return False
        return bool(mode(self._helium_buffer))

    def evaluate_safety(self, state: dict) -> dict[str, bool]:
        """Report the debounced helium verdict to Station.check_safety().

        The buffer was already filled by this tick's ``helium_level()`` poll,
        so no hardware is touched here. The majority vote suppresses
        single-reading glitches that would otherwise trigger a full
        EMERGENCY shutdown of a running measurement.
        """
        _ = state
        return {"helium_low": self.helium_low()}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Initialise; no special startup command needed."""

    def standby(self) -> None:
        """Level meter is read-only — no standby action required."""
