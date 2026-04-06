# ---
# description: |
#   ILM200LevelVI: Virtual Instrument wrapping the Oxford ILM 200 cryogen level
#   meter.  Monitoring-only VI (no ramp API).  Implements a readings buffer for
#   the helium_low() safety check to filter single-point dips.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (LevelMeterBase)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <ILM200 driver instance>}
#   init_params keys: helium_low_threshold (float, default 20.0),
#   buffer_size (int, default 5).
# process: |
#   Each call to helium_level() appends to a circular buffer (deque).
#   helium_low() returns True if the statistical mode of the buffer is below
#   the threshold (majority-vote approach to avoid reacting to single dips).
# output: |
#   Logged helium_level (%), nitrogen_level (%), get_refresh_rate (int)
#   via @monitored; set_refresh_rate available as @control.
# last_updated: 2026-04-06
# ---

"""ILM200LevelVI — Oxford ILM 200 cryogen level meter Virtual Instrument."""

from __future__ import annotations

from collections import deque
from statistics import mode
from typing import Any

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.base import LevelMeterBase


class ILM200LevelVI(LevelMeterBase):
    """Virtual Instrument for the Oxford ILM 200 cryogen level meter.

    Safety buffer
    -------------
    ``helium_level()`` appends each reading to a fixed-size deque.
    ``helium_low()`` computes the statistical mode of the buffer to suppress
    transient single-point dips that would otherwise trigger a false alarm.
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        # Threshold below which helium is considered low (%).
        self._helium_low_threshold: float = float(
            init_params.get("helium_low_threshold", 20.0)
        )

        # Number of readings to keep in the buffer for helium_low() decision.
        self._buffer_size: int = int(init_params.get("buffer_size", 5))

        # Circular buffer of helium readings (booleans: below threshold?).
        self._helium_buffer: deque[bool] = deque([False] * self._buffer_size, maxlen=self._buffer_size)

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
        """Return the current refresh rate mode (0 = slow, 1 = fast)."""
        return self._driver.get_refresh_rate()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods
    # ------------------------------------------------------------------

    @control
    def set_refresh_rate(self, mode: int) -> None:
        """Set the ILM refresh rate mode.

        Args:
            mode: 0 for slow (continuous), 1 for fast (pulsed).
        """
        self._driver.set_refresh_rate(mode)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Safety helper (not @monitored — called by Station safety checks)
    # ------------------------------------------------------------------

    def helium_low(self) -> bool:
        """Return True if the helium level is critically low.

        Uses a majority vote of recent readings (mode of the buffer) to avoid
        reacting to isolated single-point dips.  Returns ``False`` until the
        buffer has at least one reading.
        """
        if not self._helium_buffer:
            return False
        return bool(mode(self._helium_buffer))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Initialise; no special startup command needed for ILM 200."""

    def standby(self) -> None:
        """ILM 200 is read-only — no standby action required."""
