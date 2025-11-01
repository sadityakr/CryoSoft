"""
Action Layer Base Interfaces
=============================

Abstract interfaces defining high-level operations for each device type.

These interfaces follow the Interface Segregation Principle (ISP),
ensuring that action classes only implement relevant operations for
their device type.

The action layer sits between the driver layer (low-level I/O) and
the cryostat layer (logical instrument), providing intelligent
multi-step procedures.
"""

from abc import ABC, abstractmethod


class ITemperatureActions(ABC):
    """Abstract interface for temperature controller actions.

    Defines high-level operations that combine multiple driver commands
    into intelligent temperature control procedures.
    """

    @abstractmethod
    def initiate(self):
        """Initialize temperature controller for operation.

        Typical actions:
        - Set control mode to remote & unlocked
        - Enable auto-PID if available
        - Set heater/gas to auto mode
        - Verify communication

        Returns:
            bool: True if initialization successful
        """
        pass

    @abstractmethod
    def ramp_to_temperature(self, target, rate=1.0):
        """Ramp temperature to target value at specified rate.

        Args:
            target: Target temperature in Kelvin
            rate: Ramp rate in K/min (default: 1.0)

        Returns:
            float: Final temperature in Kelvin

        Raises:
            TimeoutError: If temperature doesn't stabilize within timeout
        """
        pass

    @abstractmethod
    def hold_temperature(self):
        """Hold current temperature setpoint.

        Ensures controller is in stable hold mode at current setpoint.

        Returns:
            float: Current temperature in Kelvin
        """
        pass

    @abstractmethod
    def standby(self):
        """Put temperature controller in standby mode.

        Typical actions:
        - Set control to local & locked
        - Reduce heater power if safe
        - Maintain current temperature

        Returns:
            bool: True if standby successful
        """
        pass


class IMagnetActions(ABC):
    """Abstract interface for magnet power supply actions.

    Defines high-level operations for safe magnet control including
    field ramping, persistent mode management, and emergency procedures.
    """

    @abstractmethod
    def initiate(self):
        """Initialize magnet power supply for operation.

        Typical actions:
        - Set control mode to remote & unlocked
        - Release clamp if active
        - Enable switch heater if at zero field
        - Verify status

        Returns:
            bool: True if initialization successful
        """
        pass

    @abstractmethod
    def ramp_to_field(self, target, rate=None):
        """Ramp magnetic field to target value.

        Handles persistent mode automatically:
        - Disables persistent mode if needed
        - Ramps field to target
        - Re-enables persistent mode after reaching target

        Args:
            target: Target field in Tesla
            rate: Ramp rate in T/min (None = use current setting)

        Returns:
            float: Final field in Tesla

        Raises:
            MagnetError: If unsafe operation attempted
            TimeoutError: If field doesn't stabilize
        """
        pass

    @abstractmethod
    def hold(self):
        """Hold at current field.

        Sets activity to 'hold' to maintain current field value.

        Returns:
            float: Current field in Tesla
        """
        pass

    @abstractmethod
    def go_to_zero(self, rate=None):
        """Safely ramp field to zero.

        Handles persistent mode and switch heater automatically.

        Args:
            rate: Ramp rate in T/min (None = use current setting)

        Returns:
            float: Final field (should be ~0.0T)
        """
        pass

    @abstractmethod
    def enable_persistent_mode(self):
        """Enable persistent mode (if not already enabled).

        Procedure:
        - Verify at stable field
        - Turn off switch heater
        - Wait for cooling
        - Ramp current to zero

        Returns:
            bool: True if successful

        Raises:
            MagnetError: If magnet not at rest or unsafe
        """
        pass

    @abstractmethod
    def disable_persistent_mode(self):
        """Disable persistent mode (if currently enabled).

        Procedure:
        - Match setpoint to persistent field
        - Ramp current to match persistent field
        - Turn on switch heater
        - Wait for heating

        Returns:
            bool: True if successful

        Raises:
            MagnetError: If magnet not at rest or unsafe
        """
        pass

    @abstractmethod
    def emergency_stop(self):
        """Emergency stop - immediately hold at current field.

        Sets activity to 'hold' and logs emergency event.

        Returns:
            float: Current field in Tesla
        """
        pass


class ILevelMeterActions(ABC):
    """Abstract interface for level meter actions.

    Defines high-level operations for level monitoring and calibration.
    """

    @abstractmethod
    def initiate(self):
        """Initialize level meter for operation.

        Typical actions:
        - Set control mode to remote & unlocked
        - Set measurement mode to continuous
        - Verify channels operational

        Returns:
            bool: True if initialization successful
        """
        pass

    @abstractmethod
    def measure_all_levels(self):
        """Measure all available channel levels.

        Returns:
            dict: Dictionary mapping channel names to levels (%)
                  Example: {'helium': 75.5, 'nitrogen': 50.3}
        """
        pass

    @abstractmethod
    def monitor_levels(self, duration=60, interval=10):
        """Monitor levels over time period.

        Args:
            duration: Total monitoring time in seconds
            interval: Time between measurements in seconds

        Returns:
            list: List of measurement dictionaries with timestamps
                  Example: [
                      {'time': 0, 'helium': 75.5, 'nitrogen': 50.3},
                      {'time': 10, 'helium': 75.4, 'nitrogen': 50.2},
                      ...
                  ]
        """
        pass

    @abstractmethod
    def calibrate_all_channels(self):
        """Calibrate all available channels.

        Returns:
            dict: Dictionary mapping channel numbers to calibration status
                  Example: {1: True, 2: True}
        """
        pass

    @abstractmethod
    def check_low_level_warning(self, threshold=20.0):
        """Check if any channel is below warning threshold.

        Args:
            threshold: Warning threshold as percentage (default: 20%)

        Returns:
            dict: Dictionary with warning status for each channel
                  Example: {
                      'helium': {'level': 15.5, 'warning': True},
                      'nitrogen': {'level': 45.2, 'warning': False}
                  }
        """
        pass


# Export all interfaces
__all__ = [
    'ITemperatureActions',
    'IMagnetActions',
    'ILevelMeterActions',
]
