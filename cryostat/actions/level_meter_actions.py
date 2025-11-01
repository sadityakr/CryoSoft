"""
Level Meter Actions
===================

High-level level meter operations for monitoring cryogen levels.

This class provides convenient monitoring and alerting for helium
and nitrogen levels. Works with any driver that implements LevelMeterBase.
"""

import logging
import time
from .base import ILevelMeterActions


class LevelMeterActions(ILevelMeterActions):
    """High-level level meter actions.

    Provides monitoring, calibration, and alerting operations for
    cryogen level management.

    Example:
        driver = MockILM()
        actions = LevelMeterActions(driver)
        actions.initiate()
        levels = actions.measure_all_levels()
    """

    def __init__(self, driver):
        """Initialize level meter actions with a driver.

        Args:
            driver: Level meter driver (must implement LevelMeterBase interface)
        """
        self.driver = driver
        self.log = logging.getLogger(f"{__name__}.{driver.name}")

    def initiate(self):
        """Initialize level meter for operation.

        Sets up the meter for continuous monitoring:
        - Control mode: Remote & Unlocked (RU)
        - Measurement mode: Continuous
        - Verifies all channels operational

        Returns:
            bool: True if initialization successful

        Example:
            >>> actions.initiate()
            True
        """
        self.log.info("Initiating level meter")

        try:
            # Set remote & unlocked control
            self.driver.control_mode = "RU"
            self.log.debug("Control mode: Remote & Unlocked")

            # Set continuous measurement
            self.driver.measurement_mode = "continuous"
            self.log.debug("Measurement mode: Continuous")

            # Verify channels
            levels = self.driver.measure_all_channels()
            self.log.info(f"Level meter ready: {len(levels)} channel(s) active")
            for channel, level in levels.items():
                self.log.debug(f"Channel {channel}: {level:.1f}%")

            return True

        except Exception as e:
            self.log.error(f"Initialization failed: {e}")
            return False

    def measure_all_levels(self):
        """Measure all available channel levels.

        Returns convenient dict with channel names instead of numbers.

        Returns:
            dict: Dictionary mapping channel names to levels (%)
                  Example: {'helium': 75.5, 'nitrogen': 50.3}

        Example:
            >>> levels = actions.measure_all_levels()
            >>> print(f"Helium: {levels['helium']:.1f}%")
            Helium: 75.5%
        """
        self.log.debug("Measuring all levels")

        try:
            # Get raw measurements
            raw_levels = self.driver.measure_all_channels()

            # Convert to named dict
            levels = {}
            if 1 in raw_levels:
                levels['helium'] = raw_levels[1]
            if 2 in raw_levels:
                levels['nitrogen'] = raw_levels[2]

            self.log.debug(f"Levels: {levels}")
            return levels

        except Exception as e:
            self.log.error(f"Measure failed: {e}")
            return {}

    def monitor_levels(self, duration=60, interval=10):
        """Monitor levels over time period.

        Continuously measures levels at specified intervals and
        returns time-series data.

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

        Example:
            >>> data = actions.monitor_levels(duration=60, interval=10)
            >>> for point in data:
            ...     print(f"t={point['time']}s: He={point['helium']:.1f}%")
            t=0s: He=75.5%
            t=10s: He=75.4%
            ...
        """
        self.log.info(f"Starting level monitoring: {duration}s duration, "
                     f"{interval}s interval")

        measurements = []
        start_time = time.time()
        measurement_count = 0

        try:
            while (time.time() - start_time) < duration:
                elapsed = time.time() - start_time

                # Measure all levels
                levels = self.measure_all_levels()

                # Create data point
                data_point = {'time': elapsed}
                data_point.update(levels)
                measurements.append(data_point)

                measurement_count += 1
                self.log.debug(f"Measurement {measurement_count}: {levels}")

                # Sleep until next measurement (if not done)
                remaining_duration = duration - elapsed
                if remaining_duration > interval:
                    time.sleep(interval)
                elif remaining_duration > 0:
                    time.sleep(remaining_duration)
                else:
                    break

            self.log.info(f"Monitoring complete: {measurement_count} measurements")
            return measurements

        except Exception as e:
            self.log.error(f"Monitoring failed: {e}")
            return measurements  # Return partial data

    def calibrate_all_channels(self):
        """Calibrate all available channels.

        Runs calibration procedure on each channel and reports results.

        Returns:
            dict: Dictionary mapping channel numbers to calibration status
                  Example: {1: True, 2: True}

        Example:
            >>> results = actions.calibrate_all_channels()
            >>> if all(results.values()):
            ...     print("All channels calibrated successfully")
            All channels calibrated successfully
        """
        self.log.info("Calibrating all channels")

        results = {}

        try:
            num_channels = self.driver.num_channels

            for channel in range(1, num_channels + 1):
                self.log.info(f"Calibrating channel {channel}")
                success = self.driver.calibrate_channel(channel)
                results[channel] = success

                if success:
                    self.log.info(f"Channel {channel} calibration successful")
                else:
                    self.log.warning(f"Channel {channel} calibration failed")

            return results

        except Exception as e:
            self.log.error(f"Calibration failed: {e}")
            return results

    def check_low_level_warning(self, threshold=20.0):
        """Check if any channel is below warning threshold.

        Useful for automated monitoring and alerting.

        Args:
            threshold: Warning threshold as percentage (default: 20%)

        Returns:
            dict: Dictionary with warning status for each channel
                  Example: {
                      'helium': {'level': 15.5, 'warning': True},
                      'nitrogen': {'level': 45.2, 'warning': False}
                  }

        Example:
            >>> warnings = actions.check_low_level_warning(threshold=20.0)
            >>> for channel, info in warnings.items():
            ...     if info['warning']:
            ...         print(f"WARNING: {channel} low ({info['level']:.1f}%)")
            WARNING: helium low (15.5%)
        """
        self.log.debug(f"Checking levels against threshold: {threshold}%")

        try:
            levels = self.measure_all_levels()
            warnings = {}

            for channel_name, level in levels.items():
                is_low = level < threshold
                warnings[channel_name] = {
                    'level': level,
                    'warning': is_low
                }

                if is_low:
                    self.log.warning(f"{channel_name.capitalize()} level LOW: "
                                   f"{level:.1f}% (threshold: {threshold}%)")
                else:
                    self.log.debug(f"{channel_name.capitalize()}: {level:.1f}% (OK)")

            return warnings

        except Exception as e:
            self.log.error(f"Warning check failed: {e}")
            return {}

    def get_status(self):
        """Get current status of level meter.

        Returns:
            dict: Status information including levels, mode, and channels

        Example:
            >>> status = actions.get_status()
            >>> print(status)
            {'helium_level': 75.5, 'nitrogen_level': 50.3, ...}
        """
        try:
            status = {
                'helium_level': self.driver.helium_level,
                'nitrogen_level': self.driver.nitrogen_level,
                'active_channel': self.driver.active_channel,
                'measurement_mode': self.driver.measurement_mode,
                'control_mode': self.driver.control_mode,
                'num_channels': self.driver.num_channels,
            }

            return status

        except Exception as e:
            self.log.error(f"Failed to get status: {e}")
            return {}

    def __repr__(self):
        return f"<LevelMeterActions(driver={self.driver.name})>"
