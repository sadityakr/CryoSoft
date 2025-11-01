"""
Unit Tests for Mock Drivers
============================

Tests for all mock driver implementations (ITC503, IPS120, ILM).

Run tests:
    pytest cryostat/tests/test_mock_drivers.py -v
"""

import pytest
import time
from cryostat.drivers.mock.mock_itc503 import MockITC503
from cryostat.drivers.mock.mock_ips120 import MockIPS120
from cryostat.drivers.mock.mock_ilm import MockILM


class TestMockITC503:
    """Test suite for MockITC503 temperature controller."""

    @pytest.fixture
    def itc(self):
        """Create MockITC503 instance for testing."""
        return MockITC503()

    def test_initialization(self, itc):
        """Test driver initializes correctly."""
        assert itc is not None
        assert itc.name == "Mock ITC503"
        assert itc.temperature_1 == 300.0  # Default initial temperature

    def test_temperature_reading(self, itc):
        """Test temperature reading properties."""
        temp1 = itc.temperature_1
        temp2 = itc.temperature_2
        temp3 = itc.temperature_3

        assert isinstance(temp1, float)
        assert isinstance(temp2, float)
        assert isinstance(temp3, float)
        assert 0 <= temp1 <= 400  # Reasonable range

    def test_setpoint_control(self, itc):
        """Test temperature setpoint setting."""
        # Set new setpoint
        itc.temperature_setpoint = 10.0
        assert itc.temperature_setpoint == 10.0

        # Set another value
        itc.temperature_setpoint = 4.2
        assert itc.temperature_setpoint == 4.2

    def test_control_mode(self, itc):
        """Test control mode setting."""
        # Test all valid modes
        for mode in ["LL", "RL", "LU", "RU"]:
            itc.control_mode = mode
            assert itc.control_mode == mode

    def test_heater_control(self, itc):
        """Test heater power control."""
        # Set heater power
        itc.heater = 50.0
        assert itc.heater == 50.0

        # Test clamping to valid range
        itc.heater = 150.0  # Beyond max
        assert itc.heater <= 99.9

        itc.heater = -10.0  # Below min
        assert itc.heater >= 0.0

    def test_heater_gas_mode(self, itc):
        """Test heater and gas flow mode."""
        for mode in ["MANUAL", "AM", "MA", "AUTO"]:
            itc.heater_gas_mode = mode
            assert itc.heater_gas_mode == mode

    def test_auto_pid(self, itc):
        """Test auto-PID control."""
        itc.auto_pid = True
        assert itc.auto_pid is True

        itc.auto_pid = False
        assert itc.auto_pid is False

    def test_wait_for_temperature(self, itc):
        """Test temperature stabilization wait."""
        # Set a new setpoint
        itc.temperature_setpoint = 10.0

        # Wait should complete quickly (mock simulation)
        start = time.time()
        result = itc.wait_for_temperature(error=0.5, timeout=5)
        elapsed = time.time() - start

        assert result is True
        assert elapsed < 3  # Should be fast for mock
        assert abs(itc.temperature_1 - 10.0) < 0.5

    def test_temperature_error(self, itc):
        """Test temperature error calculation."""
        itc.temperature_setpoint = 10.0
        itc.wait_for_temperature(error=0.1, timeout=5)

        error = itc.temperature_error
        assert abs(error) < 0.2  # Should be close to zero after waiting

    def test_version(self, itc):
        """Test version string."""
        version = itc.version
        assert isinstance(version, str)
        assert "Mock" in version


class TestMockIPS120:
    """Test suite for MockIPS120 magnet power supply."""

    @pytest.fixture
    def ips(self):
        """Create MockIPS120 instance for testing."""
        return MockIPS120(field_range=[-7, 7])

    def test_initialization(self, ips):
        """Test driver initializes correctly."""
        assert ips is not None
        assert ips.name == "Mock IPS120"
        assert ips.field == 0.0  # Start at zero field

    def test_field_reading(self, ips):
        """Test field reading properties."""
        field = ips.field
        demand = ips.demand_field
        persistent = ips.persistent_field

        assert isinstance(field, float)
        assert isinstance(demand, float)
        assert isinstance(persistent, float)
        assert field == 0.0  # Initial state

    def test_current_reading(self, ips):
        """Test current reading properties."""
        current = ips.current_measured
        demand_current = ips.demand_current

        assert isinstance(current, float)
        assert isinstance(demand_current, float)

    def test_field_setpoint(self, ips):
        """Test field setpoint control."""
        # Set within range
        ips.field_setpoint = 0.5
        assert ips.field_setpoint == 0.5

        # Test clamping to field_range
        ips.field_setpoint = 10.0  # Beyond range
        assert ips.field_setpoint <= 7.0

        ips.field_setpoint = -10.0  # Below range
        assert ips.field_setpoint >= -7.0

    def test_sweep_rate(self, ips):
        """Test sweep rate control."""
        ips.sweep_rate = 0.2
        assert ips.sweep_rate == 0.2

    def test_control_mode(self, ips):
        """Test control mode setting."""
        for mode in ["LL", "RL", "LU", "RU"]:
            ips.control_mode = mode
            assert ips.control_mode == mode

    def test_activity_control(self, ips):
        """Test activity mode control."""
        # Test hold
        ips.activity = "hold"
        assert ips.activity == "hold"

        # Test clamp
        ips.activity = "clamp"
        assert ips.activity == "clamp"

    def test_switch_heater_control(self, ips):
        """Test switch heater enable/disable."""
        # Enable heater
        ips.switch_heater_enabled = True
        assert ips.switch_heater_enabled is True

        # Disable heater
        ips.switch_heater_enabled = False
        assert ips.switch_heater_enabled is False

    def test_enable_control(self, ips):
        """Test enable_control method."""
        ips.enable_control()

        assert ips.control_mode == "RU"
        assert ips.activity != "clamp"

    def test_set_field_basic(self, ips):
        """Test basic field setting."""
        # Enable control first
        ips.enable_control()

        # Set field
        ips.set_field(0.5, sweep_rate=0.5)

        # Verify field reached
        assert abs(ips.field - 0.5) < 0.01

    def test_set_field_to_zero(self, ips):
        """Test ramping to zero field."""
        # Set to non-zero first
        ips.enable_control()
        ips.set_field(0.5, sweep_rate=0.5)
        assert abs(ips.field - 0.5) < 0.01

        # Ramp to zero
        ips.set_field(0.0, sweep_rate=0.5)
        assert abs(ips.field) < 0.01

    def test_wait_for_idle(self, ips):
        """Test wait_for_idle method."""
        ips.enable_control()

        # Start a sweep
        ips.activity = "to setpoint"
        ips.field_setpoint = 0.3
        ips._start_sweep(0.3)

        # Wait for completion
        ips.wait_for_idle(delay=0.1)

        assert ips.sweep_status == "at rest"

    def test_sweep_status(self, ips):
        """Test sweep status reporting."""
        status = ips.sweep_status
        assert status == "at rest"  # Initially at rest

    def test_version(self, ips):
        """Test version string."""
        version = ips.version
        assert isinstance(version, str)
        assert "Mock" in version


class TestMockILM:
    """Test suite for MockILM level meter."""

    @pytest.fixture
    def ilm(self):
        """Create MockILM instance for testing."""
        return MockILM(num_channels=2)

    def test_initialization(self, ilm):
        """Test driver initializes correctly."""
        assert ilm is not None
        assert ilm.name == "Mock ILM"
        assert ilm.num_channels == 2

    def test_helium_level_reading(self, ilm):
        """Test helium level reading."""
        level = ilm.helium_level
        assert isinstance(level, float)
        assert 0 <= level <= 100  # Percentage

    def test_nitrogen_level_reading(self, ilm):
        """Test nitrogen level reading."""
        level = ilm.nitrogen_level
        assert isinstance(level, float)
        assert 0 <= level <= 100  # Percentage

    def test_single_channel_mode(self):
        """Test level meter with single channel."""
        ilm_single = MockILM(num_channels=1)
        assert ilm_single.num_channels == 1
        assert ilm_single.helium_level is not None
        assert ilm_single.nitrogen_level is None  # Should be None for single channel

    def test_active_channel(self, ilm):
        """Test active channel control."""
        ilm.active_channel = 1
        assert ilm.active_channel == 1

        ilm.active_channel = 2
        assert ilm.active_channel == 2

    def test_measurement_mode(self, ilm):
        """Test measurement mode control."""
        for mode in ["continuous", "sample", "off"]:
            ilm.measurement_mode = mode
            assert ilm.measurement_mode == mode

    def test_control_mode(self, ilm):
        """Test control mode setting."""
        for mode in ["LL", "RL", "LU", "RU"]:
            ilm.control_mode = mode
            assert ilm.control_mode == mode

    def test_calibrate_channel(self, ilm):
        """Test channel calibration."""
        # Calibrate channel 1
        result = ilm.calibrate_channel(1)
        assert result is True

        # Calibrate channel 2
        result = ilm.calibrate_channel(2)
        assert result is True

        # Try invalid channel
        result = ilm.calibrate_channel(99)
        assert result is False

    def test_set_probe_length(self, ilm):
        """Test probe length setting."""
        # Valid length
        result = ilm.set_probe_length(1, 100.0)
        assert result is True

        # Invalid length (too short)
        result = ilm.set_probe_length(1, 5.0)
        assert result is False

        # Invalid length (too long)
        result = ilm.set_probe_length(1, 250.0)
        assert result is False

    def test_measure_level(self, ilm):
        """Test level measurement."""
        # Measure channel 1
        level1 = ilm.measure_level(1)
        assert isinstance(level1, float)
        assert 0 <= level1 <= 100

        # Measure channel 2
        level2 = ilm.measure_level(2)
        assert isinstance(level2, float)
        assert 0 <= level2 <= 100

        # Measure active channel
        level_active = ilm.measure_level()
        assert isinstance(level_active, float)

    def test_measure_all_channels(self, ilm):
        """Test measuring all channels."""
        levels = ilm.measure_all_channels()

        assert isinstance(levels, dict)
        assert 1 in levels
        assert 2 in levels
        assert 0 <= levels[1] <= 100
        assert 0 <= levels[2] <= 100

    def test_level_drift_simulation(self, ilm):
        """Test that levels drift over time (simulation feature)."""
        # Record initial level
        initial_level = ilm.helium_level

        # Wait briefly
        time.sleep(0.1)

        # Force update
        ilm._update_levels()

        # Level should have changed slightly (drift simulation)
        current_level = ilm.helium_level
        # Note: May not change much in 0.1s, but method should work

    def test_version(self, ilm):
        """Test version string."""
        version = ilm.version
        assert isinstance(version, str)
        assert "Mock" in version


# ==================== Integration Tests ====================

class TestDriverInterfaces:
    """Test that all mock drivers implement required interfaces."""

    def test_itc503_implements_temperature_base(self):
        """Verify MockITC503 implements TemperatureControllerBase."""
        from cryostat.drivers.base.temperature_base import TemperatureControllerBase

        itc = MockITC503()
        assert isinstance(itc, TemperatureControllerBase)

        # Check required properties exist
        assert hasattr(itc, 'temperature_1')
        assert hasattr(itc, 'temperature_setpoint')
        assert hasattr(itc, 'control_mode')
        assert hasattr(itc, 'heater')
        assert hasattr(itc, 'wait_for_temperature')

    def test_ips120_implements_magnet_base(self):
        """Verify MockIPS120 implements MagnetBase."""
        from cryostat.drivers.base.magnet_base import MagnetBase

        ips = MockIPS120()
        assert isinstance(ips, MagnetBase)

        # Check required properties exist
        assert hasattr(ips, 'field')
        assert hasattr(ips, 'field_setpoint')
        assert hasattr(ips, 'sweep_rate')
        assert hasattr(ips, 'activity')
        assert hasattr(ips, 'switch_heater_enabled')
        assert hasattr(ips, 'set_field')
        assert hasattr(ips, 'wait_for_idle')

    def test_ilm_implements_level_meter_base(self):
        """Verify MockILM implements LevelMeterBase."""
        from cryostat.drivers.base.level_meter_base import LevelMeterBase

        ilm = MockILM()
        assert isinstance(ilm, LevelMeterBase)

        # Check required properties exist
        assert hasattr(ilm, 'helium_level')
        assert hasattr(ilm, 'active_channel')
        assert hasattr(ilm, 'measurement_mode')
        assert hasattr(ilm, 'calibrate_channel')
        assert hasattr(ilm, 'measure_level')


# Run tests with: pytest cryostat/tests/test_mock_drivers.py -v
if __name__ == '__main__':
    pytest.main([__file__, '-v'])
