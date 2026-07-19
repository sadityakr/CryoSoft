# ---
# description: |
#   Simulated driver for the Oxford Instruments ITC 503 temperature controller.
#   Models exponential thermal settling toward a setpoint with a configurable
#   time constant. No VISA dependency — pure Python simulation.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Public methods set/query
#   the temperature setpoint and heater output in the simulation.
# process: |
#   Uses time.time() and an exponential decay formula to advance temperature:
#   T(t) = Tsp + (T_old - Tsp) * exp(-dt / tau). Heater output is proportional
#   to |Tsp - T|, capped at 100%.
# output: |
#   Returns float temperature, setpoint, and heater output values via public API.
# last_updated: 2026-04-19
# ---

"""Simulated Oxford ITC 503 Temperature Controller driver."""

import math
import time

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimOxfordITC503:
    """Simulated Oxford ITC 503 temperature controller.

    Models exponential temperature settling toward a setpoint.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_oxford_itc503.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated ITC 503.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::24::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._temperature: float = 300.0  # Kelvin (room temperature start)
        self._setpoint: float = 300.0     # Kelvin
        self._tau: float = 60.0           # Time constant in seconds
        self._last_update: float = time.time()

        # Needle valve state (auxiliary analog output on real ITC503)
        self._needle_valve: float = 0.0  # Percent open, 0.0–100.0

        # Control modes & values
        self._heater_mode: str = "AUTO"
        self._needle_valve_mode: str = "AUTO"
        self._heater_output: float = 0.0
        self._proportional_band: float = 0.0
        self._integral_action_time: float = 0.0
        self._derivative_action_time: float = 0.0
        self._auto_pid: bool = True

        # Test control flags
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_temperature(self) -> float:
        """Return the current temperature in Kelvin."""
        self._check_error()
        self._update_simulation()
        return self._temperature

    def get_setpoint(self) -> float:
        """Return the temperature setpoint in Kelvin."""
        self._check_error()
        return self._setpoint

    def set_setpoint(self, setpoint: float) -> None:
        """Set the target temperature.

        Args:
            setpoint: Desired temperature in Kelvin. Must be >= 0.
        """
        if setpoint < 0.0:
            raise ValueError(f"Temperature setpoint must be >= 0 K, got {setpoint}")
        self._setpoint = setpoint

    def get_heater_output(self) -> float:
        """Return the heater output as a percentage (0–100%).

        Proportional to |setpoint - temperature|, capped at 100% in auto mode;
        otherwise returns the manual heater output setting.
        """
        self._check_error()
        if self._heater_mode == "MANUAL":
            return self._heater_output
        return min(100.0, abs(self._setpoint - self._temperature) * 10.0)

    def get_idn(self) -> str:
        """Return simulated identification string (matches OxfordITC503)."""
        self._check_error()
        return "OXFORD,ITC503,SIM,1.0"

    # ------------------------------------------------------------------
    # Needle valve API (auxiliary analog output on real ITC503)
    # ------------------------------------------------------------------

    def get_needle_valve(self) -> float:
        """Return the needle valve position as a percentage open (0–100).

        Returns:
            Float in [0.0, 100.0].
        """
        self._check_error()
        return self._needle_valve

    def set_needle_valve(self, position: float) -> None:
        """Set the needle valve position.

        Args:
            position: Percent open in [0.0, 100.0]. Clamped silently.
        """
        self._needle_valve = max(0.0, min(100.0, position))

    # ------------------------------------------------------------------
    # Internal simulation logic
    # ------------------------------------------------------------------

    def _update_simulation(self) -> None:
        """Advance simulated temperature using exponential settling."""
        now = time.time()
        dt = now - self._last_update
        self._last_update = now

        if self._heater_mode == "AUTO":
            target = self._setpoint
        else:
            # Settle toward a temperature proportional to the manual heater output.
            # 4.2 K is base cryogenic temperature; max heater power (99.9%) gives 300 K.
            target = 4.2 + (self._heater_output / 99.9) * 295.8

        self._temperature = (
            target
            + (self._temperature - target) * math.exp(-dt / self._tau)
        )

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on ITC 503",
                vi_name="SimOxfordITC503",
            )

    def set_heater_output(self, output: float) -> None:
        """Set the manual heater output percentage.

        Args:
            output: Percent of maximum voltage/power in [0.0, 99.9].
        """
        self._check_error()
        self._heater_output = max(0.0, min(99.9, output))

    def get_heater_mode(self) -> str:
        """Return the heater control mode ('MANUAL' or 'AUTO')."""
        self._check_error()
        return self._heater_mode

    def set_heater_mode(self, mode: str) -> None:
        """Set the heater control mode to 'MANUAL' or 'AUTO'.

        Args:
            mode: Must be 'MANUAL' or 'AUTO'.
        """
        self._check_error()
        if mode not in ("MANUAL", "AUTO"):
            raise ValueError(f"Heater mode must be 'MANUAL' or 'AUTO', got {mode}")
        self._heater_mode = mode

    def get_needle_valve_mode(self) -> str:
        """Return the needle valve control mode ('MANUAL' or 'AUTO')."""
        self._check_error()
        return self._needle_valve_mode

    def set_needle_valve_mode(self, mode: str) -> None:
        """Set the needle valve control mode to 'MANUAL' or 'AUTO'.

        Args:
            mode: Must be 'MANUAL' or 'AUTO'.
        """
        self._check_error()
        if mode not in ("MANUAL", "AUTO"):
            raise ValueError(f"Needle valve mode must be 'MANUAL' or 'AUTO', got {mode}")
        self._needle_valve_mode = mode

    def get_proportional_band(self) -> float:
        """Return the proportional band in Kelvin."""
        self._check_error()
        return self._proportional_band

    def set_proportional_band(self, pb: float) -> None:
        """Set the proportional band in Kelvin.

        Args:
            pb: Proportional band in Kelvin. Must be in [0.0, 1677.7].
        """
        self._check_error()
        self._proportional_band = max(0.0, min(1677.7, pb))

    def get_integral_action_time(self) -> float:
        """Return the integral action time in minutes."""
        self._check_error()
        return self._integral_action_time

    def set_integral_action_time(self, iat: float) -> None:
        """Set the integral action time in minutes.

        Args:
            iat: Integral action time in minutes. Must be in [0.0, 140.0].
        """
        self._check_error()
        self._integral_action_time = max(0.0, min(140.0, iat))

    def get_derivative_action_time(self) -> float:
        """Return the derivative action time in minutes."""
        self._check_error()
        return self._derivative_action_time

    def set_derivative_action_time(self, dat: float) -> None:
        """Set the derivative action time in minutes.

        Args:
            dat: Derivative action time in minutes. Must be in [0.0, 273.0].
        """
        self._check_error()
        self._derivative_action_time = max(0.0, min(273.0, dat))

    def get_auto_pid(self) -> bool:
        """Return whether Auto-PID is enabled."""
        self._check_error()
        return self._auto_pid

    def set_auto_pid(self, enabled: bool) -> None:
        """Enable or disable Auto-PID control.

        Args:
            enabled: True to enable Auto-PID, False to disable.
        """
        self._check_error()
        self._auto_pid = bool(enabled)
