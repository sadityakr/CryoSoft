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

        Proportional to |setpoint - temperature|, capped at 100%.
        """
        self._check_error()
        return min(100.0, abs(self._setpoint - self._temperature) * 10.0)

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

        # Exponential approach: T(t) = Tsp + (T_old - Tsp) * exp(-dt / tau)
        self._temperature = (
            self._setpoint
            + (self._temperature - self._setpoint) * math.exp(-dt / self._tau)
        )

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on ITC 503",
                vi_name="SimOxfordITC503",
            )
