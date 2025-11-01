"""
Mock ITC503 Temperature Controller
===================================

Mock implementation of the Oxford Instruments ITC503 Temperature Controller.

This driver simulates the behavior of a real ITC503 without requiring hardware.
It maintains the exact same interface as the real driver for seamless
transition between development and production environments.

Example:
    itc = MockITC503()
    itc.control_mode = "RU"
    itc.temperature_setpoint = 4.2
    itc.wait_for_temperature()
    print(f"Temperature: {itc.temperature_1}K")
"""

from pymeasure.adapters import FakeAdapter
from pymeasure.instruments import Instrument
from ..base.temperature_base import TemperatureControllerBase
import logging
import time
import threading

log = logging.getLogger(__name__)


class MockITC503(Instrument, TemperatureControllerBase):
    """Mock Oxford ITC503 Temperature Controller.

    Simulates a real ITC503 with realistic behavior including:
    - Temperature ramping with simulated time delays
    - Three temperature sensors
    - Heater control (auto and manual modes)
    - PID control simulation
    - All standard ITC503 properties
    """

    def __init__(self, adapter=None, name="Mock ITC503", **kwargs):
        """Initialize mock ITC503.

        Args:
            adapter: Communication adapter (uses FakeAdapter if None)
            name: Device name
            **kwargs: Additional arguments passed to Instrument
        """
        if adapter is None:
            adapter = FakeAdapter()

        super().__init__(adapter, name, includeSCPI=False, **kwargs)

        # Internal state
        self._temp_setpoint = 300.0
        self._temp_1 = 300.0
        self._temp_2 = 300.0
        self._temp_3 = 300.0
        self._heater = 0.0
        self._heater_voltage = 0.0
        self._gasflow = 0.0
        self._control_mode = "RU"  # Remote & Unlocked
        self._heater_gas_mode = "MANUAL"
        self._auto_pid = False
        self._proportional_band = 0.0
        self._integral_action_time = 0.0
        self._derivative_action_time = 0.0
        self._sweep_status = 0

        # Simulation parameters
        self._ramp_rate = 1.0  # K/min (for simulation)
        self._lock = threading.Lock()

        log.info(f"{name} initialized at {self._temp_1}K")

    # ==================== Temperature Measurements ====================

    @property
    def temperature_1(self):
        """Temperature of sensor 1 in Kelvin."""
        return self._temp_1

    @property
    def temperature_2(self):
        """Temperature of sensor 2 in Kelvin."""
        return self._temp_2

    @property
    def temperature_3(self):
        """Temperature of sensor 3 in Kelvin."""
        return self._temp_3

    @property
    def temperature_error(self):
        """Difference between setpoint and measured temperature (K)."""
        return self._temp_setpoint - self._temp_1

    # ==================== Control Properties ====================

    @property
    def temperature_setpoint(self):
        """Temperature setpoint in Kelvin."""
        return self._temp_setpoint

    @temperature_setpoint.setter
    def temperature_setpoint(self, value):
        """Set temperature setpoint in Kelvin."""
        if value < 0 or value > 1677.7:
            value = max(0, min(1677.7, value))  # Clamp to range
        with self._lock:
            old_value = self._temp_setpoint
            self._temp_setpoint = value
            log.debug(f"Setpoint: {old_value:.2f}K → {value:.2f}K")

    @property
    def control_mode(self):
        """Control mode (LL/RL/LU/RU)."""
        return self._control_mode

    @control_mode.setter
    def control_mode(self, value):
        """Set control mode."""
        valid_modes = ["LL", "RL", "LU", "RU"]
        if value not in valid_modes:
            raise ValueError(f"Invalid control mode: {value}. Must be one of {valid_modes}")
        self._control_mode = value
        log.debug(f"Control mode: {value}")

    @property
    def heater_gas_mode(self):
        """Heater and gas flow control mode."""
        return self._heater_gas_mode

    @heater_gas_mode.setter
    def heater_gas_mode(self, value):
        """Set heater and gas flow control mode."""
        valid_modes = ["MANUAL", "AM", "MA", "AUTO"]
        if value not in valid_modes:
            raise ValueError(f"Invalid heater/gas mode: {value}. Must be one of {valid_modes}")
        self._heater_gas_mode = value
        log.debug(f"Heater/Gas mode: {value}")

    # ==================== Heater Control ====================

    @property
    def heater(self):
        """Heater output power as percentage (0-99.9%)."""
        return self._heater

    @heater.setter
    def heater(self, value):
        """Set heater output power (0-99.9%)."""
        value = max(0, min(99.9, value))  # Clamp to valid range
        self._heater = value
        self._heater_voltage = value * 0.4  # Simulate voltage (max 40V)
        log.debug(f"Heater: {value:.1f}%")

    @property
    def heater_voltage(self):
        """Heater output power in volts."""
        return self._heater_voltage

    @property
    def gasflow(self):
        """Gas flow as percentage (0-99.9%)."""
        return self._gasflow

    @gasflow.setter
    def gasflow(self, value):
        """Set gas flow percentage."""
        self._gasflow = max(0, min(99.9, value))

    # ==================== PID Parameters ====================

    @property
    def proportional_band(self):
        """Proportional band for PID controller (K)."""
        return self._proportional_band

    @proportional_band.setter
    def proportional_band(self, value):
        """Set proportional band."""
        self._proportional_band = max(0, min(1677.7, value))

    @property
    def integral_action_time(self):
        """Integral action time for PID controller (minutes)."""
        return self._integral_action_time

    @integral_action_time.setter
    def integral_action_time(self, value):
        """Set integral action time."""
        self._integral_action_time = max(0, min(140, value))

    @property
    def derivative_action_time(self):
        """Derivative action time for PID controller (minutes)."""
        return self._derivative_action_time

    @derivative_action_time.setter
    def derivative_action_time(self, value):
        """Set derivative action time."""
        self._derivative_action_time = max(0, min(273, value))

    @property
    def auto_pid(self):
        """Auto-PID mode (True=on, False=off)."""
        return self._auto_pid

    @auto_pid.setter
    def auto_pid(self, value):
        """Set auto-PID mode."""
        self._auto_pid = bool(value)

    # ==================== Status ====================

    @property
    def sweep_status(self):
        """Sweep status (0=not running, 1+=sweeping)."""
        return self._sweep_status

    @property
    def version(self):
        """Device version string."""
        return "ITC503 Mock v1.0"

    # ==================== Wait Methods ====================

    def wait_for_temperature(self, error=0.01, timeout=3600,
                            check_interval=0.5, stability_interval=1.0,
                            thermalize_interval=0, should_stop=lambda: False):
        """Wait for temperature to stabilize at setpoint.

        Simulates realistic temperature ramping behavior.

        Args:
            error: Maximum temperature error in K to consider stable
            timeout: Maximum time to wait in seconds
            check_interval: Time between checks in seconds
            stability_interval: Time to remain stable before returning
            thermalize_interval: Additional thermalization time
            should_stop: Function returning True to stop early

        Returns:
            bool: True if temperature stabilized

        Raises:
            TimeoutError: If timeout exceeded
        """
        log.info(f"Waiting for temperature: target={self._temp_setpoint:.2f}K, "
                f"error={error}K")

        start_time = time.time()
        stable_intervals = 0
        required_intervals = int(stability_interval / check_interval)

        # Simulate ramping
        initial_temp = self._temp_1
        target_temp = self._temp_setpoint
        delta = target_temp - initial_temp

        while True:
            elapsed = time.time() - start_time

            # Simulate gradual temperature change
            if abs(delta) > error:
                # Exponential approach to setpoint (sped up 30x for testing)
                progress = min(1.0, elapsed / 2.0)  # Approach over ~2 seconds (30x faster)
                self._temp_1 = initial_temp + delta * (1 - (1 - progress) ** 2)
                self._temp_2 = self._temp_1 + 0.1  # Small offset for sensor 2
                self._temp_3 = self._temp_1 - 0.1  # Small offset for sensor 3
            else:
                # Already at setpoint
                self._temp_1 = target_temp

            # Check stability
            current_error = abs(self._temp_1 - target_temp)
            if current_error < error:
                stable_intervals += 1
            else:
                stable_intervals = 0

            # Check completion
            if stable_intervals >= required_intervals:
                log.info(f"Temperature stabilized at {self._temp_1:.2f}K "
                        f"(elapsed: {elapsed:.1f}s)")
                return True

            # Check timeout
            if elapsed > timeout:
                log.warning(f"Temperature wait timeout after {timeout}s")
                raise TimeoutError(
                    f"Timeout waiting for temperature (target={target_temp}K)"
                )

            # Check early stop
            if should_stop():
                log.debug("Temperature wait stopped early")
                return False

            # Sleep
            time.sleep(check_interval)

    # ==================== Device Info ====================

    def __repr__(self):
        return f"<MockITC503(temp={self._temp_1:.2f}K, setpoint={self._temp_setpoint:.2f}K)>"
