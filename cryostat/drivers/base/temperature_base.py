"""
Temperature Controller Base Interface
======================================

Abstract base class defining the interface for all temperature controllers.

This ensures that any temperature controller driver (mock or real) implements
the required properties and methods, enabling interchangeability.
"""

from abc import ABC, abstractmethod


class TemperatureControllerBase(ABC):
    """Abstract interface for temperature controller drivers.

    All temperature controller drivers must implement these properties
    and methods to ensure compatibility with the action layer.
    """

    # ==================== Temperature Readings ====================

    @property
    @abstractmethod
    def temperature_1(self):
        """Float: Temperature reading from sensor 1 in Kelvin."""
        pass

    @property
    @abstractmethod
    def temperature_2(self):
        """Float: Temperature reading from sensor 2 in Kelvin."""
        pass

    @property
    @abstractmethod
    def temperature_3(self):
        """Float: Temperature reading from sensor 3 in Kelvin."""
        pass

    # ==================== Control Properties ====================

    @property
    @abstractmethod
    def temperature_setpoint(self):
        """Float: Temperature setpoint in Kelvin."""
        pass

    @temperature_setpoint.setter
    @abstractmethod
    def temperature_setpoint(self, value):
        """Set temperature setpoint in Kelvin."""
        pass

    @property
    @abstractmethod
    def control_mode(self):
        """String: Control mode (LL/RL/LU/RU).

        Values:
            'LL': Local & Locked
            'RL': Remote & Locked
            'LU': Local & Unlocked
            'RU': Remote & Unlocked
        """
        pass

    @control_mode.setter
    @abstractmethod
    def control_mode(self, value):
        """Set control mode."""
        pass

    # ==================== Heater Control ====================

    @property
    @abstractmethod
    def heater(self):
        """Float: Heater output power as percentage (0-99.9%)."""
        pass

    @heater.setter
    @abstractmethod
    def heater(self, value):
        """Set heater output power (0-99.9%)."""
        pass

    @property
    @abstractmethod
    def heater_gas_mode(self):
        """String: Heater and gas flow control mode.

        Values:
            'MANUAL': Heater & gas manual
            'AM': Heater auto, gas manual
            'MA': Heater manual, gas auto
            'AUTO': Heater & gas auto
        """
        pass

    @heater_gas_mode.setter
    @abstractmethod
    def heater_gas_mode(self, value):
        """Set heater and gas flow control mode."""
        pass

    # ==================== Wait Methods ====================

    @abstractmethod
    def wait_for_temperature(self, error=0.01, timeout=3600,
                            check_interval=0.5, **kwargs):
        """Wait for temperature to stabilize at setpoint.

        Args:
            error: Maximum temperature error in Kelvin to consider stable
            timeout: Maximum time to wait in seconds
            check_interval: Time between checks in seconds
            **kwargs: Additional device-specific parameters

        Returns:
            bool: True if temperature stabilized, False if timeout
        """
        pass
