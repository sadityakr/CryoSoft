"""
Temperature Controller Actions
===============================

High-level temperature control operations that combine multiple
driver commands into intelligent procedures.

This class works with any driver that implements TemperatureControllerBase,
whether mock or real hardware.
"""

import logging
from .base import ITemperatureActions


class TemperatureActions(ITemperatureActions):
    """High-level temperature control actions.

    Provides intelligent multi-step procedures for temperature control,
    including initialization, ramping, and stabilization.

    Example:
        driver = MockITC503()
        actions = TemperatureActions(driver)
        actions.initiate()
        final_temp = actions.ramp_to_temperature(4.2, rate=1.0)
    """

    def __init__(self, driver):
        """Initialize temperature actions with a driver.

        Args:
            driver: Temperature controller driver (must implement
                   TemperatureControllerBase interface)
        """
        self.driver = driver
        self.log = logging.getLogger(f"{__name__}.{driver.name}")

    def initiate(self):
        """Initialize temperature controller for operation.

        Sets up the controller for remote control with optimal settings:
        - Control mode: Remote & Unlocked (RU)
        - Heater/Gas mode: Auto (if available)
        - Auto-PID: Enabled (if available)

        Returns:
            bool: True if initialization successful

        Example:
            >>> actions.initiate()
            True
        """
        self.log.info("Initiating temperature controller")

        try:
            # Set remote & unlocked control
            self.driver.control_mode = "RU"
            self.log.debug("Control mode: Remote & Unlocked")

            # Enable auto heater/gas control (if available)
            if hasattr(self.driver, 'heater_gas_mode'):
                try:
                    self.driver.heater_gas_mode = "AUTO"
                    self.log.debug("Heater/Gas mode: AUTO")
                except Exception as e:
                    self.log.warning(f"Could not set heater/gas mode: {e}")

            # Enable auto-PID (if available)
            if hasattr(self.driver, 'auto_pid'):
                try:
                    self.driver.auto_pid = True
                    self.log.debug("Auto-PID: Enabled")
                except Exception as e:
                    self.log.warning(f"Could not enable auto-PID: {e}")

            # Verify current temperature reading
            temp = self.driver.temperature_1
            self.log.info(f"Temperature controller ready (current: {temp:.2f}K)")

            return True

        except Exception as e:
            self.log.error(f"Initialization failed: {e}")
            return False

    def ramp_to_temperature(self, target, rate=1.0):
        """Ramp temperature to target value at specified rate.

        Performs intelligent temperature ramping with stabilization:
        1. Sets temperature setpoint
        2. Waits for stabilization using driver's wait method
        3. Verifies final temperature

        Args:
            target: Target temperature in Kelvin
            rate: Ramp rate in K/min (informational, driver-dependent)

        Returns:
            float: Final temperature in Kelvin

        Raises:
            TimeoutError: If temperature doesn't stabilize
            ValueError: If target temperature is invalid

        Example:
            >>> final = actions.ramp_to_temperature(4.2, rate=0.5)
            >>> print(f"Reached {final:.2f}K")
            Reached 4.20K
        """
        self.log.info(f"Ramping to {target:.2f}K at {rate:.2f}K/min")

        try:
            # Validate target
            if target < 0 or target > 1677.7:
                raise ValueError(f"Invalid temperature: {target}K (range: 0-1677.7K)")

            # Record initial temperature
            initial_temp = self.driver.temperature_1
            self.log.debug(f"Initial temperature: {initial_temp:.2f}K")

            # Set new setpoint
            self.driver.temperature_setpoint = target
            self.log.debug(f"Setpoint updated: {target:.2f}K")

            # Calculate estimated time (for logging)
            delta_temp = abs(target - initial_temp)
            estimated_time = (delta_temp / rate) * 60  # seconds
            self.log.info(f"Estimated ramp time: {estimated_time:.0f}s "
                         f"(ΔT={delta_temp:.2f}K)")

            # Wait for stabilization
            self.log.info("Waiting for temperature stabilization...")
            self.driver.wait_for_temperature(
                error=0.1,  # ±0.1K tolerance
                timeout=max(600, estimated_time * 2)  # Generous timeout
            )

            # Read final temperature
            final_temp = self.driver.temperature_1
            self.log.info(f"Ramp complete: {final_temp:.2f}K "
                         f"(target: {target:.2f}K, error: {abs(final_temp-target):.3f}K)")

            return final_temp

        except TimeoutError:
            current = self.driver.temperature_1
            self.log.error(f"Timeout waiting for {target:.2f}K (stuck at {current:.2f}K)")
            raise

        except Exception as e:
            self.log.error(f"Ramp failed: {e}")
            raise

    def hold_temperature(self):
        """Hold current temperature setpoint.

        Ensures the controller maintains the current setpoint.
        Useful after completing a ramp or before system shutdown.

        Returns:
            float: Current temperature in Kelvin

        Example:
            >>> temp = actions.hold_temperature()
            >>> print(f"Holding at {temp:.2f}K")
            Holding at 4.20K
        """
        self.log.info("Holding temperature at current setpoint")

        try:
            setpoint = self.driver.temperature_setpoint
            current = self.driver.temperature_1

            self.log.info(f"Temperature hold: setpoint={setpoint:.2f}K, "
                         f"current={current:.2f}K")

            return current

        except Exception as e:
            self.log.error(f"Hold temperature failed: {e}")
            raise

    def standby(self):
        """Put temperature controller in standby mode.

        Sets the controller to local & locked mode, preventing
        accidental changes from remote control.

        Returns:
            bool: True if standby successful

        Example:
            >>> actions.standby()
            True
        """
        self.log.info("Setting temperature controller to standby")

        try:
            # Record current state
            current_temp = self.driver.temperature_1
            setpoint = self.driver.temperature_setpoint

            # Set to local & locked
            self.driver.control_mode = "LL"
            self.log.debug("Control mode: Local & Locked")

            self.log.info(f"Standby mode active (temp={current_temp:.2f}K, "
                         f"setpoint={setpoint:.2f}K)")

            return True

        except Exception as e:
            self.log.error(f"Standby failed: {e}")
            return False

    def get_status(self):
        """Get current status of temperature controller.

        Returns:
            dict: Status information including temperatures, setpoint,
                  heater power, and control mode

        Example:
            >>> status = actions.get_status()
            >>> print(status)
            {'temperature_1': 4.2, 'temperature_2': 4.25, ...}
        """
        try:
            status = {
                'temperature_1': self.driver.temperature_1,
                'temperature_2': self.driver.temperature_2,
                'temperature_3': self.driver.temperature_3,
                'setpoint': self.driver.temperature_setpoint,
                'heater': self.driver.heater,
                'control_mode': self.driver.control_mode,
            }

            # Optional fields
            if hasattr(self.driver, 'heater_gas_mode'):
                status['heater_gas_mode'] = self.driver.heater_gas_mode

            if hasattr(self.driver, 'auto_pid'):
                status['auto_pid'] = self.driver.auto_pid

            return status

        except Exception as e:
            self.log.error(f"Failed to get status: {e}")
            return {}

    def __repr__(self):
        return f"<TemperatureActions(driver={self.driver.name})>"
