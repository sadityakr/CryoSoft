"""CryogenLevelMeterVI — behavior-based VI for any cryogen level meter."""

from __future__ import annotations

from collections import deque
from statistics import mode
from typing import Any

from cryosoft.core.decorators import control, monitored
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import LevelMeterBase


# Three-mode standard constants
STANDBY = 0
SLOW = 1
FAST = 2

# The three-mode standard as a GUI/manifest drop-down (see the VI's "Refresh
# mode standard"): the labels an operator picks from, mapped to the values
# set_refresh_rate() accepts.
REFRESH_MODE_CHOICES: dict[str, int] = {
    "Standby (measurements paused)": STANDBY,
    "Slow (normal operation)": SLOW,
    "Fast (helium fill)": FAST,
}


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
    transient single-point dips. A tick where the driver could not be read
    at all (``evaluate_safety()``'s ``state["_disconnected"]``) feeds a
    synthetic "low" reading into the SAME buffer rather than force-tripping
    the flag outright — a momentary comms glitch (one bad ISOBUS round-trip)
    is smoothed away exactly like a momentary low-value glitch, while a
    genuinely dead meter still trips once disconnection persists long enough
    to win the majority vote.
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
        # Identity of the last disconnected state dict already folded into
        # the buffer by evaluate_safety() — Station.get_state() builds one
        # fresh dict per tick, so comparing identity (not equality) dedupes
        # the case where evaluate_safety() is called more than once against
        # the same tick's snapshot (e.g. the per-tick safety check and an
        # operation's end-of-run check) without under-weighting a genuinely
        # new disconnected tick.
        self._last_disconnect_state_seen: dict | None = None

    # ------------------------------------------------------------------
    # @monitored methods
    # ------------------------------------------------------------------

    @monitored(
        unit="%",
        description="Liquid helium level in the cryostat reservoir",
    )
    def helium_level(self) -> float:
        """Return the current helium level in percent and update safety buffer."""
        level = self._driver.get_helium_level()  # type: ignore[attr-defined]
        self._helium_buffer.append(level < self._helium_low_threshold)
        return level

    @monitored(
        unit="%",
        description="Liquid nitrogen level in the cryostat jacket",
    )
    def nitrogen_level(self) -> float:
        """Return the current nitrogen level in percent."""
        return self._driver.get_nitrogen_level()  # type: ignore[attr-defined]

    # Dimensionless: a mode code from the three-mode standard, not a
    # measured quantity.
    @monitored(
        unit="",
        description="Refresh mode code: 0 standby, 1 slow, 2 fast",
    )
    def get_refresh_rate(self) -> int:
        """Return the current refresh rate mode (0=STANDBY, 1=SLOW, 2=FAST)."""
        return self._driver.get_refresh_rate()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods
    # ------------------------------------------------------------------

    @control(
        scope="operation",
        params={
            "mode": ParamSpec(
                type=int,
                default=SLOW,
                choices=REFRESH_MODE_CHOICES,
                description="Level-meter refresh mode (three-mode standard)",
            ),
        },
    )
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

        On a normal tick the buffer was already filled by this tick's
        ``helium_level()`` poll, so no hardware is touched here. On a tick
        where the driver could not be read at all, ``state["_disconnected"]``
        is set (Station.get_state()'s comm-error streak) and
        ``helium_level()`` was never called to append anything — a synthetic
        "low" reading is folded into the buffer here instead, so a dead or
        merely-glitching meter is judged by the same majority vote as a
        genuine low reading rather than force-tripping on one bad
        round-trip. ``state`` is the exact dict object Station cached for
        this tick, reused as an identity key so a second same-tick call
        (e.g. an operation's end-of-run safety recheck) does not double-count
        the one physical event.
        """
        if state.get("_disconnected") and state is not self._last_disconnect_state_seen:
            self._helium_buffer.append(True)
            self._last_disconnect_state_seen = state
        return {"helium_low": self.helium_low()}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Initialise; no special startup command needed."""

    def standby(self) -> None:
        """Level meter is read-only — no standby action required."""
