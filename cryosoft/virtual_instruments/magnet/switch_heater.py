# ---
# description: |
#   SwitchHeater: wall-clock state object for a persistent-magnet switch heater.
#   Models the thermal settling times (warmup after energising, cooldown after
#   de-energising) in SECONDS, independent of the Orchestrator tick rate, and
#   answers "is it ready to use yet?". The physical on/off lives in the driver;
#   this object tracks the on/off state it was told about plus the timestamp of
#   the last transition, so an already-warm heater is not re-warmed.
# entry_point: Not run directly; owned by SuperconductingMagnetPersistentVI.
# dependencies:
#   - time (wall-clock; injectable for tests)
# input: |
#   warmup_s / cooldown_s (seconds), optional clock callable.
# process: |
#   turn_on()/turn_off() stamp the transition time; is_ready()/is_cold() compare
#   elapsed wall-clock time against warmup_s/cooldown_s.
# output: |
#   Boolean readiness (is_ready, is_cold) and seconds_until_ready() for display.
# ---

"""SwitchHeater — wall-clock readiness state object for a switch heater."""

from __future__ import annotations

import time
from collections.abc import Callable


class SwitchHeater:
    """Tracks switch-heater on/off state and wall-clock warmup/cooldown readiness.

    A superconducting-magnet switch heater needs a fixed thermal settling time
    after it is energised before the coil can be driven (warmup), and after it
    is de-energised before the switch is superconducting again (cooldown). Those
    times are physical seconds, so they are timed by wall clock here rather than
    by counting Orchestrator ticks (which would scale wrongly with the tick
    interval).

    Args:
        warmup_s: Seconds after ``turn_on()`` before the heater is ready to ramp.
        cooldown_s: Seconds after ``turn_off()`` before the switch is cold.
        clock: Callable returning the current time in seconds. Injected so tests
            can advance a fake clock deterministically; defaults to ``time.time``.
            (Passing a dependency like this instead of hard-coding ``time.time``
            is "dependency injection" — it keeps the timing logic testable.)
    """

    def __init__(
        self,
        warmup_s: float = 60.0,
        cooldown_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.warmup_s = float(warmup_s)
        self.cooldown_s = float(cooldown_s)
        self._clock = clock
        self._is_on: bool = False
        self._changed_at: float = clock()

    def turn_on(self) -> None:
        """Mark the heater energised, stamping the time.

        A no-op if already on, so re-commanding ``on`` does not restart the
        warmup clock (an already-warm heater stays ready).
        """
        if not self._is_on:
            self._is_on = True
            self._changed_at = self._clock()

    def turn_off(self) -> None:
        """Mark the heater de-energised, stamping the time. No-op if already off."""
        if self._is_on:
            self._is_on = False
            self._changed_at = self._clock()

    @property
    def is_on(self) -> bool:
        """Whether the heater is currently energised."""
        # ``@property`` exposes this method as an attribute (``heater.is_on``),
        # read-only, with no parentheses at the call site.
        return self._is_on

    def is_ready(self) -> bool:
        """True once an energised heater has warmed for ``warmup_s`` seconds."""
        return self._is_on and (self._clock() - self._changed_at) >= self.warmup_s

    def is_cold(self) -> bool:
        """True once a de-energised heater has cooled for ``cooldown_s`` seconds."""
        return (not self._is_on) and (self._clock() - self._changed_at) >= self.cooldown_s

    def seconds_until_ready(self) -> float:
        """Seconds remaining until ``is_ready()`` (0.0 if already ready or off)."""
        if not self._is_on:
            return 0.0
        return max(0.0, self.warmup_s - (self._clock() - self._changed_at))
