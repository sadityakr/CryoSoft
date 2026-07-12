# ---
# description: |
#   Simulated driver for the Oxford Instruments IPS 120-10 magnet power supply.
#   Models current ramping behavior with configurable ramp rate and status
#   transitions, plus physically-faithful switch-heater behavior: persistent
#   mode is heater-derived (like the real Mercury iPS driver), the coil
#   current freezes when the heater turns off, and energising the heater
#   across a PSU/coil current mismatch QUENCHES the simulated magnet so a
#   wrong ramp-command order fails in tests instead of on hardware.
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
# last_updated: 2026-04-19
# ---

"""Simulated Oxford IPS 120-10 Magnet Power Supply driver."""

import time

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimOxfordIPS120:
    """Simulated Oxford IPS 120-10 magnet power supply.

    Models current ramping toward a setpoint at a configurable rate.
    Status transitions: HOLD -> RAMPING -> HOLD, and -> QUENCH if the switch
    heater is energised while the PSU and coil currents are mismatched.
    Persistent mode is heater-derived (heater OFF = persistent), mirroring
    the real Mercury iPS driver; ``set_persistent_mode()`` is a no-op.

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

        # Switch heater / persistent mode state.
        # The coil current is FROZEN at the PSU current whenever the heater
        # turns off (switch superconducting) and follows the PSU while the
        # heater is on (switch resistive). Persistent mode is heater-derived,
        # exactly like the real Mercury iPS driver.
        self._switch_heater_on: bool = False
        self._coil_current: float = 0.0    # Amps — frozen while heater is OFF

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
        to RAMPING status. Ignored while quenched (a real PSU trips and
        requires a reset before accepting ramp commands again).

        Args:
            setpoint: Desired current in Amperes.
        """
        if self._status == "QUENCH":
            return
        clamped = max(self.MIN_CURRENT, min(self.MAX_CURRENT, setpoint))
        self._setpoint = clamped

        if abs(self._setpoint - self._current) > 0.01:
            self._status = "RAMPING"

    def hold(self) -> None:
        """Freeze the output where it is (mirror of the Mercury HOLD action)."""
        self._check_error()
        if self._status == "QUENCH":
            return
        self._update_simulation()
        self._setpoint = self._current
        self._status = "HOLD"

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

    def get_idn(self) -> str:
        """Return simulated identification string."""
        self._check_error()
        return "OXFORD,IPS120,SIM,1.0"

    # ------------------------------------------------------------------
    # Switch heater / persistent mode API
    # ------------------------------------------------------------------

    def get_switch_heater_state(self) -> str:
        """Return 'ON' if the switch heater is energised, 'OFF' otherwise."""
        self._check_error()
        return "ON" if self._switch_heater_on else "OFF"

    def set_switch_heater(self, state: bool) -> None:
        """Energise (True) or de-energise (False) the persistent mode switch heater.

        Models the real physics of the switch:

        * Turning the heater ON while the PSU output differs from the frozen
          coil current QUENCHES the magnet — the stored coil current is forced
          through the now-resistive switch. This is exactly the failure the
          VI's ramp sequence must never trigger; the sim makes it loud so a
          wrong command order fails in tests instead of on hardware.
        * Turning the heater OFF freezes the coil current at the present PSU
          current (switch superconducting; coil current now circulates).

        Args:
            state: True to turn on, False to turn off.
        """
        if state == self._switch_heater_on:
            return
        self._update_simulation()
        if state:
            if abs(self._current - self._coil_current) > 0.05:
                self._quench()
                return
            self._switch_heater_on = True
        else:
            self._coil_current = self._current
            self._switch_heater_on = False

    def get_coil_current(self) -> float:
        """Return the coil current in Amperes.

        While the heater is ON the switch is resistive and the coil follows
        the PSU output; while OFF the coil current is frozen at the value it
        had when the heater was last turned off.
        """
        self._check_error()
        if self._switch_heater_on:
            self._update_simulation()
            return self._current
        return self._coil_current

    def get_persistent_mode(self) -> bool:
        """Return True when the magnet is in persistent mode.

        Heater-derived (switch heater OFF means the switch is superconducting
        and the coil holds its current) — mirrors the real Mercury iPS driver,
        which has no independent persistent-mode flag.
        """
        self._check_error()
        return not self._switch_heater_on

    def set_persistent_mode(self, persistent: bool) -> None:
        """No-op, mirroring the real Mercury iPS driver.

        Persistent mode is managed entirely through the switch heater and
        ramp commands; the VI layer sequences them.

        Args:
            persistent: Ignored.
        """
        _ = persistent

    def reset_quench(self) -> None:
        """Clear a quench (test helper — a real PSU needs a manual reset)."""
        self._status = "HOLD"
        self._simulate_quench = False
        self._setpoint = self._current

    # ------------------------------------------------------------------
    # Internal simulation logic
    # ------------------------------------------------------------------

    def _quench(self) -> None:
        """Model a quench: coil energy dumps, currents collapse, PSU trips."""
        self._status = "QUENCH"
        self._current = 0.0
        self._coil_current = 0.0
        self._setpoint = 0.0
        self._switch_heater_on = False

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
