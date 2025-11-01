"""
Unit Tests for Action Layer
============================

Tests for all action implementations (Temperature, Magnet, Level Meter).

Run tests:
    pytest cryostat/tests/test_actions.py -v
"""

import pytest
import time
from cryostat.drivers.mock.mock_itc503 import MockITC503
from cryostat.drivers.mock.mock_ips120 import MockIPS120
from cryostat.drivers.mock.mock_ilm import MockILM
from cryostat.actions.temperature_actions import TemperatureActions
from cryostat.actions.magnet_actions import MagnetActions
from cryostat.actions.level_meter_actions import LevelMeterActions


class TestTemperatureActions:
    """Test suite for TemperatureActions."""

    @pytest.fixture
    def setup(self):
        """Create driver and action handler."""
        driver = MockITC503()
        actions = TemperatureActions(driver)
        return driver, actions

    def test_initialization(self, setup):
        """Test action handler initializes correctly."""
        driver, actions = setup
        assert actions is not None
        assert actions.driver is driver

    def test_initiate(self, setup):
        """Test device initialization."""
        driver, actions = setup

        result = actions.initiate()

        assert result is True
        assert driver.control_mode == "RU"

    def test_ramp_to_temperature(self, setup):
        """Test temperature ramping."""
        driver, actions = setup

        # Ramp to 10K
        final_temp = actions.ramp_to_temperature(10.0, rate=1.0)

        assert isinstance(final_temp, float)
        assert abs(final_temp - 10.0) < 0.5  # Within tolerance
        assert abs(driver.temperature_1 - 10.0) < 0.5

    def test_ramp_to_temperature_invalid(self, setup):
        """Test ramping to invalid temperature raises error."""
        driver, actions = setup

        # Test negative temperature
        with pytest.raises(ValueError):
            actions.ramp_to_temperature(-10.0, rate=1.0)

        # Test temperature beyond range
        with pytest.raises(ValueError):
            actions.ramp_to_temperature(2000.0, rate=1.0)

    def test_hold_temperature(self, setup):
        """Test temperature hold."""
        driver, actions = setup

        # Set a specific temperature first
        driver.temperature_setpoint = 4.2

        # Hold at current temperature
        current = actions.hold_temperature()

        assert isinstance(current, float)
        assert current > 0

    def test_standby(self, setup):
        """Test standby mode."""
        driver, actions = setup

        result = actions.standby()

        assert result is True
        assert driver.control_mode == "LL"

    def test_get_status(self, setup):
        """Test status retrieval."""
        driver, actions = setup

        status = actions.get_status()

        assert isinstance(status, dict)
        assert 'temperature_1' in status
        assert 'temperature_2' in status
        assert 'temperature_3' in status
        assert 'setpoint' in status
        assert 'heater' in status
        assert 'control_mode' in status

    def test_multiple_ramps(self, setup):
        """Test sequential temperature ramps."""
        driver, actions = setup

        # Ramp to 10K
        temp1 = actions.ramp_to_temperature(10.0, rate=2.0)
        assert abs(temp1 - 10.0) < 0.5

        # Ramp to 5K
        temp2 = actions.ramp_to_temperature(5.0, rate=2.0)
        assert abs(temp2 - 5.0) < 0.5

        # Ramp back to 300K
        temp3 = actions.ramp_to_temperature(300.0, rate=2.0)
        assert abs(temp3 - 300.0) < 0.5


class TestMagnetActions:
    """Test suite for MagnetActions."""

    @pytest.fixture
    def setup(self):
        """Create driver and action handler."""
        driver = MockIPS120(field_range=[-7, 7])
        actions = MagnetActions(driver)
        return driver, actions

    def test_initialization(self, setup):
        """Test action handler initializes correctly."""
        driver, actions = setup
        assert actions is not None
        assert actions.driver is driver

    def test_initiate(self, setup):
        """Test magnet initialization."""
        driver, actions = setup

        result = actions.initiate()

        assert result is True
        assert driver.control_mode == "RU"

    def test_ramp_to_field(self, setup):
        """Test field ramping."""
        driver, actions = setup

        # Initialize first
        actions.initiate()

        # Ramp to 0.5T
        final_field = actions.ramp_to_field(0.5, rate=0.5)

        assert isinstance(final_field, float)
        assert abs(final_field - 0.5) < 0.02  # Within 20mT
        assert abs(driver.field - 0.5) < 0.02

    def test_ramp_to_field_negative(self, setup):
        """Test ramping to negative field."""
        driver, actions = setup

        # Initialize
        actions.initiate()

        # Ramp to -0.3T
        final_field = actions.ramp_to_field(-0.3, rate=0.5)

        assert abs(final_field - (-0.3)) < 0.02

    def test_hold(self, setup):
        """Test field hold."""
        driver, actions = setup

        # Initialize and set a field
        actions.initiate()
        actions.ramp_to_field(0.5, rate=0.5)

        # Hold
        field = actions.hold()

        assert isinstance(field, float)
        assert driver.activity == "hold"

    def test_go_to_zero(self, setup):
        """Test ramping to zero field."""
        driver, actions = setup

        # Initialize and set a non-zero field
        actions.initiate()
        actions.ramp_to_field(0.5, rate=0.5)
        assert abs(driver.field - 0.5) < 0.02

        # Go to zero
        final_field = actions.go_to_zero(rate=0.5)

        assert abs(final_field) < 0.01  # Within 10mT
        assert abs(driver.field) < 0.01

    def test_enable_persistent_mode(self, setup):
        """Test enabling persistent mode."""
        driver, actions = setup

        # Initialize and set a field
        actions.initiate()
        actions.ramp_to_field(0.5, rate=0.5)

        # Enable persistent mode
        result = actions.enable_persistent_mode()

        assert result is True

    def test_disable_persistent_mode(self, setup):
        """Test disabling persistent mode."""
        driver, actions = setup

        # Initialize, set field, enable persistent mode
        actions.initiate()
        actions.ramp_to_field(0.5, rate=0.5)
        actions.enable_persistent_mode()

        # Disable persistent mode
        result = actions.disable_persistent_mode()

        assert result is True

    def test_emergency_stop(self, setup):
        """Test emergency stop."""
        driver, actions = setup

        # Initialize and start ramping
        actions.initiate()
        driver.activity = "to setpoint"
        driver.field_setpoint = 0.5

        # Emergency stop
        field = actions.emergency_stop()

        assert isinstance(field, float)
        assert driver.activity == "hold"

    def test_get_status(self, setup):
        """Test status retrieval."""
        driver, actions = setup

        status = actions.get_status()

        assert isinstance(status, dict)
        assert 'field' in status
        assert 'demand_field' in status
        assert 'persistent_field' in status
        assert 'current_measured' in status
        assert 'field_setpoint' in status
        assert 'sweep_rate' in status
        assert 'activity' in status
        assert 'sweep_status' in status

    def test_sequential_field_changes(self, setup):
        """Test multiple field changes in sequence."""
        driver, actions = setup

        # Initialize
        actions.initiate()

        # Ramp up
        field1 = actions.ramp_to_field(0.3, rate=0.5)
        assert abs(field1 - 0.3) < 0.02

        # Ramp higher
        field2 = actions.ramp_to_field(0.6, rate=0.5)
        assert abs(field2 - 0.6) < 0.02

        # Ramp down
        field3 = actions.ramp_to_field(0.2, rate=0.5)
        assert abs(field3 - 0.2) < 0.02

        # Go to zero
        field4 = actions.go_to_zero(rate=0.5)
        assert abs(field4) < 0.01


class TestLevelMeterActions:
    """Test suite for LevelMeterActions."""

    @pytest.fixture
    def setup(self):
        """Create driver and action handler."""
        driver = MockILM(num_channels=2)
        actions = LevelMeterActions(driver)
        return driver, actions

    def test_initialization(self, setup):
        """Test action handler initializes correctly."""
        driver, actions = setup
        assert actions is not None
        assert actions.driver is driver

    def test_initiate(self, setup):
        """Test level meter initialization."""
        driver, actions = setup

        result = actions.initiate()

        assert result is True
        assert driver.control_mode == "RU"
        assert driver.measurement_mode == "continuous"

    def test_measure_all_levels(self, setup):
        """Test measuring all levels."""
        driver, actions = setup

        levels = actions.measure_all_levels()

        assert isinstance(levels, dict)
        assert 'helium' in levels
        assert 'nitrogen' in levels
        assert 0 <= levels['helium'] <= 100
        assert 0 <= levels['nitrogen'] <= 100

    def test_measure_all_levels_single_channel(self):
        """Test measuring levels with single channel."""
        driver = MockILM(num_channels=1)
        actions = LevelMeterActions(driver)

        levels = actions.measure_all_levels()

        assert 'helium' in levels
        assert 'nitrogen' not in levels  # Only one channel

    def test_monitor_levels(self, setup):
        """Test level monitoring over time."""
        driver, actions = setup

        # Monitor for 1 second with 0.5s interval
        data = actions.monitor_levels(duration=1, interval=0.5)

        assert isinstance(data, list)
        assert len(data) >= 2  # Should have at least 2 measurements
        assert 'time' in data[0]
        assert 'helium' in data[0]
        assert data[0]['time'] >= 0
        assert data[-1]['time'] <= 1.5  # Allow some margin

    def test_calibrate_all_channels(self, setup):
        """Test calibrating all channels."""
        driver, actions = setup

        results = actions.calibrate_all_channels()

        assert isinstance(results, dict)
        assert 1 in results
        assert 2 in results
        assert results[1] is True
        assert results[2] is True

    def test_check_low_level_warning_no_warning(self, setup):
        """Test low level warning check when levels are OK."""
        driver, actions = setup

        # Set levels high enough
        driver._helium_level = 50.0
        driver._nitrogen_level = 40.0

        warnings = actions.check_low_level_warning(threshold=20.0)

        assert isinstance(warnings, dict)
        assert 'helium' in warnings
        assert 'nitrogen' in warnings
        assert warnings['helium']['warning'] is False
        assert warnings['nitrogen']['warning'] is False

    def test_check_low_level_warning_with_warning(self, setup):
        """Test low level warning check when levels are low."""
        driver, actions = setup

        # Set levels below threshold
        driver._helium_level = 15.0
        driver._nitrogen_level = 10.0

        warnings = actions.check_low_level_warning(threshold=20.0)

        assert warnings['helium']['warning'] is True
        assert warnings['nitrogen']['warning'] is True
        assert abs(warnings['helium']['level'] - 15.0) < 0.1
        assert abs(warnings['nitrogen']['level'] - 10.0) < 0.1

    def test_get_status(self, setup):
        """Test status retrieval."""
        driver, actions = setup

        status = actions.get_status()

        assert isinstance(status, dict)
        assert 'helium_level' in status
        assert 'nitrogen_level' in status
        assert 'active_channel' in status
        assert 'measurement_mode' in status
        assert 'control_mode' in status
        assert 'num_channels' in status


# ==================== Integration Tests ====================

class TestActionInterfaces:
    """Test that all action classes implement required interfaces."""

    def test_temperature_actions_implements_interface(self):
        """Verify TemperatureActions implements ITemperatureActions."""
        from cryostat.actions.base import ITemperatureActions

        driver = MockITC503()
        actions = TemperatureActions(driver)

        assert isinstance(actions, ITemperatureActions)

        # Check required methods exist
        assert hasattr(actions, 'initiate')
        assert hasattr(actions, 'ramp_to_temperature')
        assert hasattr(actions, 'hold_temperature')
        assert hasattr(actions, 'standby')

    def test_magnet_actions_implements_interface(self):
        """Verify MagnetActions implements IMagnetActions."""
        from cryostat.actions.base import IMagnetActions

        driver = MockIPS120()
        actions = MagnetActions(driver)

        assert isinstance(actions, IMagnetActions)

        # Check required methods exist
        assert hasattr(actions, 'initiate')
        assert hasattr(actions, 'ramp_to_field')
        assert hasattr(actions, 'hold')
        assert hasattr(actions, 'go_to_zero')
        assert hasattr(actions, 'enable_persistent_mode')
        assert hasattr(actions, 'disable_persistent_mode')
        assert hasattr(actions, 'emergency_stop')

    def test_level_meter_actions_implements_interface(self):
        """Verify LevelMeterActions implements ILevelMeterActions."""
        from cryostat.actions.base import ILevelMeterActions

        driver = MockILM()
        actions = LevelMeterActions(driver)

        assert isinstance(actions, ILevelMeterActions)

        # Check required methods exist
        assert hasattr(actions, 'initiate')
        assert hasattr(actions, 'measure_all_levels')
        assert hasattr(actions, 'monitor_levels')
        assert hasattr(actions, 'calibrate_all_channels')
        assert hasattr(actions, 'check_low_level_warning')


class TestActionDriverCompatibility:
    """Test that actions work with any conforming driver."""

    def test_temperature_actions_with_different_drivers(self):
        """Test TemperatureActions works with any temperature driver."""
        # This demonstrates the Liskov Substitution Principle
        driver = MockITC503()
        actions = TemperatureActions(driver)

        # Should work regardless of driver implementation
        assert actions.initiate() is True
        temp = actions.ramp_to_temperature(10.0, rate=2.0)
        assert isinstance(temp, float)

    def test_magnet_actions_with_different_field_ranges(self):
        """Test MagnetActions works with different field ranges."""
        # Test with 7T magnet
        driver_7t = MockIPS120(field_range=[-7, 7])
        actions_7t = MagnetActions(driver_7t)
        actions_7t.initiate()
        field = actions_7t.ramp_to_field(0.5, rate=0.5)
        assert abs(field - 0.5) < 0.02

        # Test with 2T magnet
        driver_2t = MockIPS120(field_range=[-2, 2])
        actions_2t = MagnetActions(driver_2t)
        actions_2t.initiate()
        field = actions_2t.ramp_to_field(0.5, rate=0.5)
        assert abs(field - 0.5) < 0.02

    def test_level_meter_actions_with_different_channels(self):
        """Test LevelMeterActions works with different channel counts."""
        # Single channel
        driver_1ch = MockILM(num_channels=1)
        actions_1ch = LevelMeterActions(driver_1ch)
        levels_1ch = actions_1ch.measure_all_levels()
        assert 'helium' in levels_1ch
        assert 'nitrogen' not in levels_1ch

        # Dual channel
        driver_2ch = MockILM(num_channels=2)
        actions_2ch = LevelMeterActions(driver_2ch)
        levels_2ch = actions_2ch.measure_all_levels()
        assert 'helium' in levels_2ch
        assert 'nitrogen' in levels_2ch


# Run tests with: pytest cryostat/tests/test_actions.py -v
if __name__ == '__main__':
    pytest.main([__file__, '-v'])
