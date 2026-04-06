# ---
# description: |
#   Simulated driver for the Oxford Instruments IPS 120-10 magnet power supply.
#   Models current ramping behavior with configurable ramp rate and status transitions.
#   No VISA dependency — pure Python simulation using real wall-clock time.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Public methods control
#   and query the simulated magnet current and status.
# process: |
#   Uses time.time() to advance the simulated current toward the setpoint at
#   the configured ramp rate (A/min). Status transitions HOLD -> RAMPING -> HOLD.
# output: |
#   Returns float current/setpoint values and str status via public API.
# last_updated: 2026-04-06
# ---

"""Simulated Oxford IPS 120-10 Magnet Power Supply driver."""

import time

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimOxfordIPS120:
    """Simulated Oxford IPS 120-10 magnet power supply.

    Models current ramping toward a setpoint at a configurable rate.
    Status transitions: HOLD -> RAMPING -> HOLD.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_oxford_ips120.
    """

    # Physical limits of the real IPS 120-10
    MAX_CURRENT = 90.0   # Amperes
    MIN_CURRENT = -90.0  # Amperes

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated IPS 120.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::25::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._current: float = 0.0       # Current output in Amperes
        self._setpoint: float = 0.0      # Target current in Amperes
        self._ramp_rate: float = 5.0     # A/min
        self._status: str = "HOLD"       # "HOLD", "RAMPING", or "QUENCH"
        self._last_update: float = time.time()

        # Test control flags
        self._simulate_error: bool = False   # Raises CryoSoftCommunicationError on any get_
        self._simulate_quench: bool = False  # Forces status to "QUENCH"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_current(self) -> float:
        """Return the current magnet current in Amperes."""
        self._check_error()
        self._update_simulation()
        return self._current

    def get_current_setpoint(self) -> float:
        """Return the current setpoint in Amperes."""
        self._check_error()
        return self._setpoint

    def set_current_setpoint(self, setpoint: float) -> None:
        """Set the target current.

        Clamps the setpoint to [MIN_CURRENT, MAX_CURRENT].
        If the difference from the current value exceeds 0.01 A, transitions
        to RAMPING status.

        Args:
            setpoint: Desired current in Amperes.
        """
        clamped = max(self.MIN_CURRENT, min(self.MAX_CURRENT, setpoint))
        self._setpoint = clamped

        if abs(self._setpoint - self._current) > 0.01:
            self._status = "RAMPING"

    def set_ramp_rate(self, rate: float) -> None:
        """Set the current ramp rate.

        Args:
            rate: Ramp rate in A/min. Must be positive.
        """
        if rate <= 0:
            raise ValueError(f"Ramp rate must be positive, got {rate}")
        self._ramp_rate = rate

    def get_status(self) -> str:
        """Return the current status string.

        Returns:
            One of "HOLD", "RAMPING", or "QUENCH".
        """
        self._check_error()
        if self._simulate_quench:
            return "QUENCH"
        self._update_simulation()
        return self._status

    # ------------------------------------------------------------------
    # Internal simulation logic
    # ------------------------------------------------------------------

    def _update_simulation(self) -> None:
        """Advance simulated current toward setpoint based on elapsed real time."""
        now = time.time()
        dt_min = (now - self._last_update) / 60.0
        self._last_update = now

        if self._status != "RAMPING":
            return

        max_step = self._ramp_rate * dt_min
        remaining = self._setpoint - self._current
        if abs(remaining) <= max_step:
            self._current = self._setpoint
            self._status = "HOLD"
        else:
            direction = 1 if remaining > 0 else -1
            self._current += direction * max_step

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on IPS 120",
                vi_name="SimOxfordIPS120",
            )
