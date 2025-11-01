"""
Magnet Power Supply Base Interface
===================================

Abstract base class defining the interface for all magnet power supplies.

This ensures that any magnet driver (mock or real) implements the required
properties and methods, enabling interchangeability.
"""

from abc import ABC, abstractmethod


class MagnetBase(ABC):
    """Abstract interface for magnet power supply drivers.

    All magnet drivers must implement these properties and methods
    to ensure compatibility with the action layer.
    """

    # ==================== Field Readings ====================

    @property
    @abstractmethod
    def field(self):
        """Float: Current magnetic field in Tesla.

        Returns persistent field if switch heater is off,
        demand field if switch heater is on.
        """
        pass

    @property
    @abstractmethod
    def demand_field(self):
        """Float: Demand magnetic field in Tesla."""
        pass

    @property
    @abstractmethod
    def persistent_field(self):
        """Float: Persistent magnetic field in Tesla."""
        pass

    # ==================== Current Readings ====================

    @property
    @abstractmethod
    def current_measured(self):
        """Float: Measured magnet current in Amperes."""
        pass

    @property
    @abstractmethod
    def demand_current(self):
        """Float: Demand magnet current in Amperes."""
        pass

    # ==================== Control Properties ====================

    @property
    @abstractmethod
    def field_setpoint(self):
        """Float: Magnetic field setpoint in Tesla."""
        pass

    @field_setpoint.setter
    @abstractmethod
    def field_setpoint(self, value):
        """Set magnetic field setpoint in Tesla."""
        pass

    @property
    @abstractmethod
    def current_setpoint(self):
        """Float: Current setpoint in Amperes."""
        pass

    @current_setpoint.setter
    @abstractmethod
    def current_setpoint(self, value):
        """Set current setpoint in Amperes."""
        pass

    @property
    @abstractmethod
    def sweep_rate(self):
        """Float: Sweep rate in Tesla/minute."""
        pass

    @sweep_rate.setter
    @abstractmethod
    def sweep_rate(self, value):
        """Set sweep rate in Tesla/minute."""
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

    # ==================== Activity Control ====================

    @property
    @abstractmethod
    def activity(self):
        """String: Current activity status.

        Values:
            'hold': Hold at current value
            'to setpoint': Sweep to setpoint
            'to zero': Sweep to zero
            'clamp': Output clamped
        """
        pass

    @activity.setter
    @abstractmethod
    def activity(self, value):
        """Set activity mode."""
        pass

    @property
    @abstractmethod
    def sweep_status(self):
        """String: Current sweep status.

        Values:
            'at rest': Not sweeping
            'sweeping': Currently sweeping
            'sweep limiting': Sweep rate limited
            'sweeping & sweep limiting': Both conditions
        """
        pass

    # ==================== Switch Heater ====================

    @property
    @abstractmethod
    def switch_heater_enabled(self):
        """Bool: Switch heater status.

        True: Heater on, switch open, can control current
        False: Heater off, switch closed, persistent mode
        """
        pass

    @switch_heater_enabled.setter
    @abstractmethod
    def switch_heater_enabled(self, value):
        """Enable/disable switch heater.

        Args:
            value: True to enable, False to disable, 'Force' to force enable
        """
        pass

    # ==================== High-Level Methods ====================

    @abstractmethod
    def enable_control(self):
        """Enable active control of the magnet power supply.

        Sets control to remote and turns off clamp if active.
        """
        pass

    @abstractmethod
    def disable_control(self):
        """Disable active control (only at 0T).

        Turns off switch heater, clamps output, sets control to local.

        Raises:
            MagnetError: If field is not at 0T
        """
        pass

    @abstractmethod
    def set_field(self, field, sweep_rate=None, persistent_mode_control=True):
        """Change magnetic field to specified magnitude.

        Args:
            field: Target field in Tesla
            sweep_rate: Sweep rate in T/min (optional)
            persistent_mode_control: Allow persistent mode switching

        Raises:
            MagnetError: If in persistent mode and control not allowed
        """
        pass

    @abstractmethod
    def wait_for_idle(self, delay=1, max_wait_time=None, should_stop=lambda: False):
        """Wait until magnet is at rest (not ramping).

        Args:
            delay: Time between checks in seconds
            max_wait_time: Maximum wait time in seconds (None = no limit)
            should_stop: Function returning True to stop early

        Raises:
            TimeoutError: If max_wait_time exceeded
        """
        pass
