"""
Magnet Power Supply Actions
============================

High-level magnet control operations with safety features.

This class handles complex magnet operations including field ramping,
persistent mode management, and safety interlocks. Works with any
driver that implements MagnetBase.
"""

import logging
from .base import IMagnetActions


class MagnetActions(IMagnetActions):
    """High-level magnet control actions with safety management.

    Provides intelligent procedures for safe magnet operation including:
    - Automatic persistent mode handling
    - Safety interlocks and status checks
    - Emergency stop capability

    Example:
        driver = MockIPS120()
        actions = MagnetActions(driver)
        actions.initiate()
        final_field = actions.ramp_to_field(0.5, rate=0.1)
    """

    def __init__(self, driver):
        """Initialize magnet actions with a driver.

        Args:
            driver: Magnet power supply driver (must implement
                   MagnetBase interface)
        """
        self.driver = driver
        self.log = logging.getLogger(f"{__name__}.{driver.name}")

    def initiate(self):
        """Initialize magnet power supply for operation.

        Prepares the magnet for safe operation:
        - Enables remote control
        - Releases clamp if active
        - Enables switch heater if at zero field
        - Verifies system status

        Returns:
            bool: True if initialization successful

        Example:
            >>> actions.initiate()
            True
        """
        self.log.info("Initiating magnet power supply")

        try:
            # Use driver's enable_control method
            self.driver.enable_control()

            # Verify status
            field = self.driver.field
            status = self.driver.sweep_status
            heater = self.driver.switch_heater_enabled

            self.log.info(f"Magnet ready: field={field:.3f}T, "
                         f"status={status}, heater={'ON' if heater else 'OFF'}")

            return True

        except Exception as e:
            self.log.error(f"Initialization failed: {e}")
            return False

    def ramp_to_field(self, target, rate=None):
        """Ramp magnetic field to target value.

        Performs intelligent field ramping with automatic persistent
        mode management:
        1. Checks current state
        2. Disables persistent mode if needed
        3. Ramps to target field
        4. Re-enables persistent mode (if target != 0)
        5. Verifies final field

        Args:
            target: Target field in Tesla
            rate: Ramp rate in T/min (None = use current setting)

        Returns:
            float: Final field in Tesla

        Raises:
            MagnetError: If unsafe operation attempted
            TimeoutError: If field doesn't stabilize

        Example:
            >>> final = actions.ramp_to_field(0.5, rate=0.1)
            >>> print(f"Reached {final:.3f}T")
            Reached 0.500T
        """
        self.log.info(f"Ramping to {target:.3f}T" +
                     (f" at {rate:.3f}T/min" if rate else ""))

        try:
            # Record initial state
            initial_field = self.driver.field
            self.log.debug(f"Initial field: {initial_field:.3f}T")

            # Use driver's set_field method (handles persistent mode)
            self.driver.set_field(
                field=target,
                sweep_rate=rate,
                persistent_mode_control=True
            )

            # Verify final field
            final_field = self.driver.field
            error = abs(final_field - target)

            if error > 0.01:  # 10mT tolerance
                self.log.warning(f"Field error: {error:.4f}T "
                                f"(target={target:.3f}T, actual={final_field:.3f}T)")

            self.log.info(f"Ramp complete: {final_field:.3f}T "
                         f"(target: {target:.3f}T)")

            return final_field

        except Exception as e:
            current = self.driver.field
            self.log.error(f"Ramp failed: {e} (current field: {current:.3f}T)")
            raise

    def hold(self):
        """Hold at current field.

        Sets activity to 'hold' to maintain current field value.
        Useful for pausing during measurements.

        Returns:
            float: Current field in Tesla

        Example:
            >>> field = actions.hold()
            >>> print(f"Holding at {field:.3f}T")
            Holding at 0.500T
        """
        self.log.info("Holding field at current value")

        try:
            # Set activity to hold
            self.driver.activity = "hold"

            # Read current field
            field = self.driver.field
            setpoint = self.driver.field_setpoint

            self.log.info(f"Field hold: current={field:.3f}T, "
                         f"setpoint={setpoint:.3f}T")

            return field

        except Exception as e:
            self.log.error(f"Hold failed: {e}")
            raise

    def go_to_zero(self, rate=None):
        """Safely ramp field to zero.

        Performs controlled ramp to zero field with automatic
        persistent mode handling. Switch heater is managed safely.

        Args:
            rate: Ramp rate in T/min (None = use current setting)

        Returns:
            float: Final field (should be ~0.0T)

        Example:
            >>> final = actions.go_to_zero(rate=0.2)
            >>> print(f"Field at {final:.4f}T")
            Field at 0.0000T
        """
        self.log.info("Ramping field to zero" +
                     (f" at {rate:.3f}T/min" if rate else ""))

        try:
            initial_field = self.driver.field
            self.log.debug(f"Initial field: {initial_field:.3f}T")

            # Use set_field with target=0
            # This automatically handles persistent mode
            self.driver.set_field(
                field=0.0,
                sweep_rate=rate,
                persistent_mode_control=True
            )

            # Verify at zero
            final_field = self.driver.field

            if abs(final_field) > 0.001:  # 1mT tolerance
                self.log.warning(f"Field not quite zero: {final_field:.4f}T")
            else:
                self.log.info("Successfully reached zero field")

            return final_field

        except Exception as e:
            current = self.driver.field
            self.log.error(f"Go to zero failed: {e} (current: {current:.3f}T)")
            raise

    def enable_persistent_mode(self):
        """Enable persistent mode (if not already enabled).

        Explicitly enables persistent mode. Useful for advanced
        control scenarios.

        Procedure:
        - Verify magnet is at rest
        - Turn off switch heater
        - Wait for cooling
        - Ramp current to zero

        Returns:
            bool: True if successful

        Raises:
            MagnetError: If magnet not at rest or unsafe

        Example:
            >>> actions.enable_persistent_mode()
            True
        """
        self.log.info("Enabling persistent mode")

        try:
            # Check if already in persistent mode
            if not self.driver.switch_heater_enabled:
                self.log.debug("Already in persistent mode")
                return True

            # Check if at rest
            if self.driver.sweep_status != "at rest":
                raise ValueError("Cannot enable persistent mode while sweeping")

            # Enable persistent mode using driver method
            if hasattr(self.driver, 'enable_persistent_mode'):
                self.driver.enable_persistent_mode()
            else:
                # Manual procedure
                self.log.debug("Manual persistent mode enable")
                self.driver.activity = "hold"
                self.driver.switch_heater_enabled = False
                self.driver.wait_for_idle()
                self.driver.activity = "to zero"
                self.driver.wait_for_idle()

            self.log.info("Persistent mode enabled")
            return True

        except Exception as e:
            self.log.error(f"Enable persistent mode failed: {e}")
            raise

    def disable_persistent_mode(self):
        """Disable persistent mode (if currently enabled).

        Explicitly disables persistent mode. Useful for advanced
        control scenarios.

        Procedure:
        - Match setpoint to persistent field
        - Ramp current to match persistent field
        - Turn on switch heater
        - Wait for heating

        Returns:
            bool: True if successful

        Raises:
            MagnetError: If magnet not at rest or unsafe

        Example:
            >>> actions.disable_persistent_mode()
            True
        """
        self.log.info("Disabling persistent mode")

        try:
            # Check if already in demand mode
            if self.driver.switch_heater_enabled:
                self.log.debug("Already in demand mode")
                return True

            # Check if at rest
            if self.driver.sweep_status != "at rest":
                raise ValueError("Cannot disable persistent mode while sweeping")

            # Disable persistent mode using driver method
            if hasattr(self.driver, 'disable_persistent_mode'):
                self.driver.disable_persistent_mode()
            else:
                # Manual procedure
                self.log.debug("Manual persistent mode disable")
                current_field = self.driver.field
                self.driver.field_setpoint = current_field
                self.driver.activity = "to setpoint"
                self.driver.wait_for_idle()
                self.driver.activity = "hold"
                self.driver.switch_heater_enabled = True

            self.log.info("Persistent mode disabled")
            return True

        except Exception as e:
            self.log.error(f"Disable persistent mode failed: {e}")
            raise

    def emergency_stop(self):
        """Emergency stop - immediately hold at current field.

        Sets activity to 'hold' and logs emergency event.
        Does NOT ramp to zero - maintains current field for safety.

        Returns:
            float: Current field in Tesla

        Example:
            >>> field = actions.emergency_stop()
            >>> print(f"Emergency stop at {field:.3f}T")
            Emergency stop at 0.350T
        """
        self.log.warning("EMERGENCY STOP INITIATED")

        try:
            # Immediately set to hold
            self.driver.activity = "hold"

            field = self.driver.field
            self.log.warning(f"Emergency stop complete: holding at {field:.3f}T")

            return field

        except Exception as e:
            self.log.error(f"Emergency stop failed: {e}")
            # Try to read field anyway
            try:
                field = self.driver.field
                self.log.error(f"Current field: {field:.3f}T")
                return field
            except:
                self.log.error("Cannot read field")
                raise

    def get_status(self):
        """Get current status of magnet power supply.

        Returns:
            dict: Status information including field, current, activity,
                  sweep status, and heater status

        Example:
            >>> status = actions.get_status()
            >>> print(status)
            {'field': 0.5, 'current': 5.0, ...}
        """
        try:
            status = {
                'field': self.driver.field,
                'demand_field': self.driver.demand_field,
                'persistent_field': self.driver.persistent_field,
                'current_measured': self.driver.current_measured,
                'field_setpoint': self.driver.field_setpoint,
                'sweep_rate': self.driver.sweep_rate,
                'activity': self.driver.activity,
                'sweep_status': self.driver.sweep_status,
                'switch_heater_enabled': self.driver.switch_heater_enabled,
                'control_mode': self.driver.control_mode,
            }

            return status

        except Exception as e:
            self.log.error(f"Failed to get status: {e}")
            return {}

    def __repr__(self):
        return f"<MagnetActions(driver={self.driver.name})>"
