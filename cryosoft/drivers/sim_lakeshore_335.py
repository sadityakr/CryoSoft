# ---
# description: |
#   Simulated driver for the Lakeshore 335 temperature controller.
#   Exposes the same public API as Lakeshore335, including PID and manual
#   output control modes. Pure Python simulation.
# entry_point: Not run directly; imported by the Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored).
# process: |
#   Models temperature readings and manual/auto PID heating states.
# output: |
#   Returns float temperature, setpoint, heater output, and PID values.
# last_updated: 2026-07-16
# ---

"""Simulated Lakeshore 335 temperature controller driver."""

from __future__ import annotations

import logging
import math
import time

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)


class SimLakeshore335:
    """Simulated Lakeshore 335 temperature controller.

    Matches the public API of the real Lakeshore335 driver.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated Lakeshore 335.

        Args:
            resource_string: VISA resource string (ignored).
        """
        _ = resource_string  # Explicitly ignored
        self._temperature: float = 295.0
        self._setpoint: float = 0.0
        self._heater_output: float = 0.0
        self._heater_mode: str = "AUTO"
        self._proportional_band: float = 90.0
        self._integral_action_time: float = 50.0
        self._derivative_action_time: float = 0.0
        self._auto_pid: bool = False
        self._sensor_curves: dict[str, int] = {"A": 22, "B": 2}
        
        # Simulation physics
        self._last_update: float = time.time()
        self._tau: float = 30.0  # thermal time constant in seconds
        self._simulate_error: bool = False

    def get_temperature(self) -> float:
        """Return the current simulated temperature in Kelvin."""
        self._check_error()
        self._update_simulation()
        return self._temperature

    def get_setpoint(self) -> float:
        """Return the simulated setpoint in Kelvin."""
        self._check_error()
        return self._setpoint

    def set_setpoint(self, setpoint: float) -> None:
        """Set the simulated temperature setpoint.

        Args:
            setpoint: Target temperature in Kelvin. Must be >= 0.
        """
        self._check_error()
        if setpoint < 0.0:
            raise ValueError(f"Setpoint must be >= 0 K, got {setpoint}")
        self._setpoint = setpoint

    def get_heater_output(self) -> float:
        """Return the simulated heater output percentage."""
        self._check_error()
        self._update_simulation()
        return self._heater_output

    def set_heater_output(self, output: float) -> None:
        """Set the manual heater output percentage.

        Args:
            output: Percent of maximum power in [0.0, 99.9].
        """
        self._check_error()
        self._heater_output = max(0.0, min(99.9, output))

    def get_heater_mode(self) -> str:
        """Return the simulated heater control mode ('MANUAL' or 'AUTO')."""
        self._check_error()
        return self._heater_mode

    def set_heater_mode(self, mode: str) -> None:
        """Set the simulated heater control mode to 'MANUAL' or 'AUTO'.

        Args:
            mode: Must be 'MANUAL' or 'AUTO'.
        """
        self._check_error()
        if mode not in ("MANUAL", "AUTO"):
            raise ValueError(f"Heater mode must be 'MANUAL' or 'AUTO', got {mode}")
        self._heater_mode = mode

    def get_proportional_band(self) -> float:
        """Return the proportional band."""
        self._check_error()
        return self._proportional_band

    def set_proportional_band(self, pb: float) -> None:
        """Set the proportional band."""
        self._check_error()
        self._proportional_band = max(0.0, min(1000.0, pb))

    def get_integral_action_time(self) -> float:
        """Return the integral action time."""
        self._check_error()
        return self._integral_action_time

    def set_integral_action_time(self, iat: float) -> None:
        """Set the integral action time."""
        self._check_error()
        self._integral_action_time = max(0.0, min(1000.0, iat))

    def get_derivative_action_time(self) -> float:
        """Return the derivative action time."""
        self._check_error()
        return self._derivative_action_time

    def set_derivative_action_time(self, dat: float) -> None:
        """Set the derivative action time."""
        self._check_error()
        self._derivative_action_time = max(0.0, min(200.0, dat))

    def get_auto_pid(self) -> bool:
        """Return whether Autotuning is active."""
        self._check_error()
        return self._auto_pid

    def set_auto_pid(self, enabled: bool) -> None:
        """Enable or disable Autotuning."""
        self._check_error()
        self._auto_pid = bool(enabled)

    def get_idn(self) -> str:
        """Return simulated identification string."""
        self._check_error()
        return "LSCI,MODEL335,SIM,1.0"

    def get_sensor_curve(self, sensor_input: str = "A") -> int:
        """Return the curve number assigned to the sensor input."""
        self._check_error()
        ch = str(sensor_input).upper()
        if ch not in ("A", "B"):
            raise ValueError(f"Sensor input must be 'A' or 'B', got {sensor_input}")
        return self._sensor_curves[ch]

    def set_sensor_curve(self, curve: int, sensor_input: str = "A") -> None:
        """Assign a temperature sensor curve to a sensor input."""
        self._check_error()
        ch = str(sensor_input).upper()
        if ch not in ("A", "B"):
            raise ValueError(f"Sensor input must be 'A' or 'B', got {sensor_input}")
        if not (0 <= curve <= 59):
            raise ValueError(f"Curve number must be in [0, 59], got {curve}")
        self._sensor_curves[ch] = int(curve)

    # ------------------------------------------------------------------
    # Simulation & Internal helpers
    # ------------------------------------------------------------------

    def _check_error(self) -> None:
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on Lakeshore 335",
                vi_name="SimLakeshore335",
            )

    def _update_simulation(self) -> None:
        now = time.time()
        dt = now - self._last_update
        self._last_update = now
        if dt <= 0:
            return

        # If AUTO mode, settle towards setpoint.
        # If MANUAL mode, settle towards temperature proportional to heater output.
        if self._heater_mode == "AUTO":
            target = self._setpoint
            # In AUTO, heater output is simulated as proportional to difference
            error = self._setpoint - self._temperature
            if error > 0:
                self._heater_output = min(99.9, error * 10.0)
            else:
                self._heater_output = 0.0
        else:
            # MANUAL: max power (99.9%) reaches 300 K, 0% sits at 4.2 K (base temp)
            target = 4.2 + (self._heater_output / 99.9) * 295.8

        self._temperature = (
            target + (self._temperature - target) * math.exp(-dt / self._tau)
        )
