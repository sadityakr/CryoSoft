"""
Mock ILM Level Meter
====================

Mock implementation of the Oxford Instruments ILM (Intelligent Level Meter).

This driver simulates the behavior of a real ILM for helium and nitrogen
level monitoring without requiring hardware.

Example:
    ilm = MockILM()
    ilm.control_mode = "RU"
    he_level = ilm.helium_level
    n2_level = ilm.nitrogen_level
    print(f"Helium: {he_level}%, Nitrogen: {n2_level}%")
"""

from pymeasure.adapters import FakeAdapter
from pymeasure.instruments import Instrument
from ..base.level_meter_base import LevelMeterBase
import logging
import time

log = logging.getLogger(__name__)


class MockILM(Instrument, LevelMeterBase):
    """Mock Oxford ILM Level Meter.

    Simulates a real ILM with realistic behavior including:
    - Two-channel operation (helium and nitrogen)
    - Continuous and sample measurement modes
    - Probe length calibration
    - Slow level drift simulation
    """

    def __init__(self, adapter=None, name="Mock ILM", num_channels=2, **kwargs):
        """Initialize mock ILM.

        Args:
            adapter: Communication adapter (uses FakeAdapter if None)
            name: Device name
            num_channels: Number of channels (1 or 2)
            **kwargs: Additional arguments passed to Instrument
        """
        if adapter is None:
            adapter = FakeAdapter()

        super().__init__(adapter, name, includeSCPI=False, **kwargs)

        # Configuration
        self._num_channels = num_channels

        # Internal state
        self._helium_level = 75.0  # Start at 75%
        self._nitrogen_level = 50.0 if num_channels == 2 else None
        self._active_channel = 1
        self._measurement_mode = "continuous"
        self._control_mode = "RU"  # Remote & Unlocked
        self._probe_length = {1: 100.0, 2: 100.0}  # cm

        # Simulation parameters (slow drift)
        self._last_update = time.time()
        self._drift_rate = {1: -0.05, 2: -0.03}  # %/hour

        log.info(f"{name} initialized ({num_channels} channel(s))")
        log.info(f"Initial levels: He={self._helium_level:.1f}%, "
                f"N2={self._nitrogen_level}%")

    # ==================== Level Measurements ====================

    @property
    def helium_level(self):
        """Helium level as percentage (0-100%)."""
        self._update_levels()
        return self._helium_level

    @property
    def nitrogen_level(self):
        """Nitrogen level as percentage (0-100%).

        Returns None if channel 2 not available.
        """
        if self._num_channels < 2:
            return None
        self._update_levels()
        return self._nitrogen_level

    def _update_levels(self):
        """Update levels based on time drift (simulation)."""
        current_time = time.time()
        elapsed_hours = (current_time - self._last_update) / 3600.0

        # Apply drift
        self._helium_level += self._drift_rate[1] * elapsed_hours
        self._helium_level = max(0.0, min(100.0, self._helium_level))

        if self._num_channels == 2 and self._nitrogen_level is not None:
            self._nitrogen_level += self._drift_rate[2] * elapsed_hours
            self._nitrogen_level = max(0.0, min(100.0, self._nitrogen_level))

        self._last_update = current_time

    # ==================== Channel Control ====================

    @property
    def active_channel(self):
        """Active measurement channel (1 or 2)."""
        return self._active_channel

    @active_channel.setter
    def active_channel(self, value):
        """Set active channel with validation."""
        if not (1 <= value <= 2):
            raise ValueError(f"Invalid channel: {value}. Must be 1 or 2")
        if value > self._num_channels:
            log.warning(f"Channel {value} not available (only {self._num_channels} channels)")
            return
        self._active_channel = value
        log.debug(f"Active channel: {value}")

    # ==================== Measurement Mode ====================

    @property
    def measurement_mode(self):
        """Measurement mode (continuous/sample/off)."""
        return self._measurement_mode

    @measurement_mode.setter
    def measurement_mode(self, value):
        """Set measurement mode with logging."""
        valid_modes = ["continuous", "sample", "off"]
        if value not in valid_modes:
            raise ValueError(f"Invalid measurement mode: {value}. Must be one of {valid_modes}")
        self._measurement_mode = value
        log.debug(f"Measurement mode: {value}")

    # ==================== Control Mode ====================

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

    # ==================== Device Info ====================

    @property
    def num_channels(self):
        """Number of available channels (1 or 2)."""
        return self._num_channels

    @property
    def version(self):
        """Device version string."""
        return "ILM Mock v1.0"

    # ==================== Calibration Methods ====================

    def calibrate_channel(self, channel):
        """Calibrate specified channel.

        Args:
            channel: Channel number to calibrate (1 or 2)

        Returns:
            bool: True if calibration successful
        """
        if channel < 1 or channel > 2:
            log.error(f"Invalid channel: {channel}")
            return False

        if channel > self._num_channels:
            log.error(f"Channel {channel} not available")
            return False

        log.info(f"Calibrating channel {channel}...")
        time.sleep(0.1)  # Simulate calibration time
        log.info(f"Channel {channel} calibrated successfully")
        return True

    def set_probe_length(self, channel, length_cm):
        """Set probe length for specified channel.

        Args:
            channel: Channel number (1 or 2)
            length_cm: Probe length in centimeters

        Returns:
            bool: True if successful
        """
        if channel < 1 or channel > self._num_channels:
            log.error(f"Invalid channel: {channel}")
            return False

        if length_cm < 10 or length_cm > 200:
            log.error(f"Invalid probe length: {length_cm}cm (must be 10-200cm)")
            return False

        self._probe_length[channel] = length_cm
        log.info(f"Channel {channel} probe length set to {length_cm}cm")
        return True

    # ==================== Measurement Methods ====================

    def measure_level(self, channel=None):
        """Measure level on specified channel.

        Args:
            channel: Channel to measure (1 or 2). If None, use active channel.

        Returns:
            float: Level as percentage (0-100%)
        """
        if channel is None:
            channel = self._active_channel

        if channel < 1 or channel > self._num_channels:
            log.error(f"Invalid channel: {channel}")
            return 0.0

        self._update_levels()

        if channel == 1:
            return self._helium_level
        elif channel == 2:
            return self._nitrogen_level if self._nitrogen_level is not None else 0.0

    def measure_all_channels(self):
        """Measure levels on all available channels.

        Returns:
            dict: Dictionary with channel numbers as keys, levels as values
                  Example: {1: 75.5, 2: 50.3}
        """
        self._update_levels()
        result = {1: self._helium_level}

        if self._num_channels == 2 and self._nitrogen_level is not None:
            result[2] = self._nitrogen_level

        return result

    # ==================== Device Info ====================

    def __repr__(self):
        return (f"<MockILM(channels={self._num_channels}, "
                f"He={self._helium_level:.1f}%, N2={self._nitrogen_level}%)>")
