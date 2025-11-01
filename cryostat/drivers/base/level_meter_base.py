"""
Level Meter Base Interface
===========================

Abstract base class defining the interface for all level meters.

This ensures that any level meter driver (mock or real) implements
the required properties and methods, enabling interchangeability.
"""

from abc import ABC, abstractmethod


class LevelMeterBase(ABC):
    """Abstract interface for level meter drivers.

    All level meter drivers must implement these properties and methods
    to ensure compatibility with the action layer.
    """

    # ==================== Level Readings ====================

    @property
    @abstractmethod
    def helium_level(self):
        """Float: Helium level as percentage (0-100%).

        Returns the current helium level in the dewar.
        """
        pass

    @property
    @abstractmethod
    def nitrogen_level(self):
        """Float: Nitrogen level as percentage (0-100%).

        Returns the current nitrogen level in the dewar.
        May return None if nitrogen channel not available.
        """
        pass

    # ==================== Channel Control ====================

    @property
    @abstractmethod
    def active_channel(self):
        """Int: Currently active measurement channel (1 or 2).

        1: Helium channel
        2: Nitrogen channel (if available)
        """
        pass

    @active_channel.setter
    @abstractmethod
    def active_channel(self, value):
        """Set active measurement channel."""
        pass

    # ==================== Measurement Mode ====================

    @property
    @abstractmethod
    def measurement_mode(self):
        """String: Current measurement mode.

        Values:
            'continuous': Continuous measurement
            'sample': Sample-and-hold measurement
            'off': Measurement off
        """
        pass

    @measurement_mode.setter
    @abstractmethod
    def measurement_mode(self, value):
        """Set measurement mode."""
        pass

    # ==================== Control Properties ====================

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

    # ==================== Calibration ====================

    @abstractmethod
    def calibrate_channel(self, channel):
        """Calibrate specified channel.

        Args:
            channel: Channel number to calibrate (1 or 2)

        Returns:
            bool: True if calibration successful
        """
        pass

    @abstractmethod
    def set_probe_length(self, channel, length_cm):
        """Set probe length for specified channel.

        Args:
            channel: Channel number (1 or 2)
            length_cm: Probe length in centimeters

        Returns:
            bool: True if successful
        """
        pass

    # ==================== Measurement Methods ====================

    @abstractmethod
    def measure_level(self, channel=None):
        """Measure level on specified channel.

        Args:
            channel: Channel to measure (1 or 2). If None, use active channel.

        Returns:
            float: Level as percentage (0-100%)
        """
        pass

    @abstractmethod
    def measure_all_channels(self):
        """Measure levels on all available channels.

        Returns:
            dict: Dictionary with channel numbers as keys, levels as values
                  Example: {1: 75.5, 2: 50.3}
        """
        pass

    # ==================== Device Info ====================

    @property
    @abstractmethod
    def num_channels(self):
        """Int: Number of available channels (typically 1 or 2)."""
        pass
