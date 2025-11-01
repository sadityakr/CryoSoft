"""
Integration Tests for Cryostat System
======================================

Tests for the complete cryostat system including configuration loading,
device access, and system-wide operations.

Run tests:
    pytest cryostat/tests/test_cryostat.py -v
"""

import pytest
from pathlib import Path
from cryostat.core.cryostat import Cryostat


@pytest.fixture
def config_path():
    """Get path to mock configuration file."""
    test_dir = Path(__file__).parent
    config_file = test_dir.parent / 'config' / 'cryostat_mock.yaml'
    return str(config_file)


@pytest.fixture
def cryo(config_path):
    """Create Cryostat instance for testing."""
    return Cryostat(config_path)


class TestCryostatInitialization:
    """Test cryostat initialization and configuration loading."""

    def test_initialization(self, cryo):
        """Test cryostat initializes correctly."""
        assert cryo is not None
        assert cryo.name == "Cryostat"

    def test_device_count(self, cryo):
        """Test correct number of devices loaded."""
        assert len(cryo) > 0
        # Mock config has: 2 temp controllers, 3 magnets, 1 level meter = 6 devices
        assert len(cryo) == 6

    def test_config_loaded(self, cryo):
        """Test configuration is loaded."""
        assert cryo.config is not None
        assert 'cryostat' in cryo.config
        assert 'devices' in cryo.config['cryostat']

    def test_config_path_stored(self, cryo):
        """Test configuration path is stored."""
        assert cryo.config_path is not None
        assert cryo.config_path.exists()

    def test_lock_created(self, cryo):
        """Test global lock is created."""
        assert cryo.lock is not None
        assert cryo.lock.timeout == 1.0


class TestDeviceAccess:
    """Test device access methods."""

    def test_list_devices(self, cryo):
        """Test listing all devices."""
        devices = cryo.list_devices()
        assert isinstance(devices, list)
        assert len(devices) == 6
        assert 'vti_temp_controller' in devices
        assert 'magnet1' in devices

    def test_dict_style_access(self, cryo):
        """Test dict-style device access."""
        # Access temperature controller
        temp_ctrl = cryo["vti_temp_controller"]
        assert temp_ctrl is not None
        assert temp_ctrl.name == "vti_temp_controller"

        # Access magnet
        magnet = cryo["magnet1"]
        assert magnet is not None
        assert magnet.name == "magnet1"

    def test_dict_access_invalid_device(self, cryo):
        """Test accessing non-existent device raises KeyError."""
        with pytest.raises(KeyError):
            _ = cryo["nonexistent_device"]

    def test_contains_operator(self, cryo):
        """Test 'in' operator."""
        assert "vti_temp_controller" in cryo
        assert "magnet1" in cryo
        assert "nonexistent_device" not in cryo

    def test_iteration(self, cryo):
        """Test iterating over device names."""
        device_names = list(cryo)
        assert len(device_names) == 6
        assert 'vti_temp_controller' in device_names

    def test_get_device(self, cryo):
        """Test get_device method (returns None for missing)."""
        # Valid device
        device = cryo.get_device("magnet1")
        assert device is not None

        # Invalid device
        device = cryo.get_device("nonexistent")
        assert device is None

    def test_get_devices_by_type(self, cryo):
        """Test getting devices by type."""
        # Get all magnets
        magnets = cryo.get_devices_by_type('magnet')
        assert isinstance(magnets, dict)
        assert len(magnets) == 3
        assert 'magnet1' in magnets
        assert 'magnet2' in magnets
        assert 'magnet3' in magnets

        # Get all temperature controllers
        temp_ctrls = cryo.get_devices_by_type('temperature')
        assert len(temp_ctrls) == 2

        # Get all level meters
        level_meters = cryo.get_devices_by_type('level_meter')
        assert len(level_meters) == 1


class TestDeviceOperations:
    """Test operations on devices through cryostat."""

    def test_temperature_controller_access(self, cryo):
        """Test accessing and using temperature controller."""
        temp_ctrl = cryo["vti_temp_controller"]

        # Read temperature
        temp = temp_ctrl.temperature_1
        assert isinstance(temp, float)
        assert temp > 0

        # Set setpoint
        temp_ctrl.temperature_setpoint = 10.0
        assert temp_ctrl.temperature_setpoint == 10.0

    def test_magnet_access(self, cryo):
        """Test accessing and using magnet."""
        magnet = cryo["magnet1"]

        # Read field
        field = magnet.field
        assert isinstance(field, float)

        # Read sweep status
        status = magnet.sweep_status
        assert status is not None

    def test_level_meter_access(self, cryo):
        """Test accessing and using level meter."""
        level_meter = cryo["helium_level_meter"]

        # Read helium level
        he_level = level_meter.helium_level
        assert isinstance(he_level, float)
        assert 0 <= he_level <= 100

        # Read nitrogen level
        n2_level = level_meter.nitrogen_level
        assert isinstance(n2_level, float)
        assert 0 <= n2_level <= 100


class TestSystemWideOperations:
    """Test system-wide operations."""

    def test_get_all_status(self, cryo):
        """Test getting status of all devices."""
        status = cryo.get_all_status()

        assert isinstance(status, dict)
        assert len(status) == 6

        # Check specific devices
        assert 'vti_temp_controller' in status
        assert 'magnet1' in status
        assert isinstance(status['vti_temp_controller'], dict)

    def test_initiate_all(self, cryo):
        """Test initializing all devices."""
        results = cryo.initiate_all()

        assert isinstance(results, dict)
        assert len(results) == 6

        # All should succeed with mock drivers
        for device_name, success in results.items():
            assert success is True, f"Device {device_name} failed to initialize"

    def test_get_lock_stats(self, cryo):
        """Test getting lock statistics."""
        # Do some operations to generate lock activity
        temp_ctrl = cryo["vti_temp_controller"]
        _ = temp_ctrl.temperature_1

        # Get stats
        stats = cryo.get_lock_stats()

        assert isinstance(stats, dict)
        assert 'acquisition_count' in stats
        assert 'timeout_count' in stats
        assert stats['acquisition_count'] > 0


class TestDeviceWrapper:
    """Test DeviceWrapper functionality."""

    def test_wrapper_properties(self, cryo):
        """Test DeviceWrapper properties."""
        device = cryo["vti_temp_controller"]

        # Check wrapper properties
        assert device.name == "vti_temp_controller"
        assert device.driver is not None
        assert device.actions is not None

    def test_driver_property_access(self, cryo):
        """Test accessing driver properties through wrapper."""
        device = cryo["vti_temp_controller"]

        # Access driver properties (should be locked automatically)
        temp = device.temperature_1
        assert isinstance(temp, float)

        setpoint = device.temperature_setpoint
        assert isinstance(setpoint, float)

    def test_action_method_access(self, cryo):
        """Test accessing action methods through wrapper."""
        device = cryo["vti_temp_controller"]

        # Call action methods
        result = device.initiate()
        assert result is True

        status = device.get_status()
        assert isinstance(status, dict)

    def test_high_level_actions(self, cryo):
        """Test high-level action methods."""
        # Temperature controller
        temp_ctrl = cryo["vti_temp_controller"]
        temp_ctrl.initiate()
        final_temp = temp_ctrl.ramp_to_temperature(10.0, rate=2.0)
        assert abs(final_temp - 10.0) < 0.5

        # Magnet
        magnet = cryo["magnet1"]
        magnet.initiate()
        final_field = magnet.ramp_to_field(0.5, rate=0.5)
        assert abs(final_field - 0.5) < 0.02

        # Level meter
        level_meter = cryo["helium_level_meter"]
        level_meter.initiate()
        levels = level_meter.measure_all_levels()
        assert 'helium' in levels


class TestThreadSafety:
    """Test thread-safe operations."""

    def test_lock_acquisition(self, cryo):
        """Test manual lock acquisition."""
        lock = cryo.lock

        # Acquire lock
        acquired = lock.acquire()
        assert acquired is True

        # Release lock
        lock.release()

    def test_lock_context_manager(self, cryo):
        """Test lock as context manager."""
        lock = cryo.lock

        with lock:
            # Do something in locked context
            temp_ctrl = cryo["vti_temp_controller"]
            _ = temp_ctrl.temperature_1

        # Lock should be released

    def test_device_access_is_locked(self, cryo):
        """Test that device access uses lock."""
        # Get initial lock count
        initial_count = cryo.lock.get_stats()['acquisition_count']

        # Access device
        temp_ctrl = cryo["vti_temp_controller"]
        _ = temp_ctrl.temperature_1

        # Lock count should have increased
        final_count = cryo.lock.get_stats()['acquisition_count']
        assert final_count > initial_count


class TestStringRepresentation:
    """Test string representations."""

    def test_cryo_repr(self, cryo):
        """Test Cryostat __repr__."""
        repr_str = repr(cryo)
        assert 'Cryostat' in repr_str
        assert str(len(cryo)) in repr_str

    def test_cryo_str(self, cryo):
        """Test Cryostat __str__."""
        str_repr = str(cryo)
        assert 'Cryostat System' in str_repr
        assert 'Devices' in str_repr

    def test_device_repr(self, cryo):
        """Test DeviceWrapper __repr__."""
        device = cryo["vti_temp_controller"]
        repr_str = repr(device)
        assert 'DeviceWrapper' in repr_str
        assert 'vti_temp_controller' in repr_str


class TestErrorHandling:
    """Test error handling and edge cases."""

    def test_invalid_config_path(self):
        """Test loading non-existent config raises error."""
        with pytest.raises(FileNotFoundError):
            Cryostat('nonexistent_config.yaml')

    def test_accessing_invalid_attribute(self, cryo):
        """Test accessing non-existent attribute raises error."""
        device = cryo["vti_temp_controller"]

        with pytest.raises(AttributeError):
            _ = device.nonexistent_attribute

    def test_setting_invalid_attribute(self, cryo):
        """Test setting non-existent attribute raises error."""
        device = cryo["vti_temp_controller"]

        with pytest.raises(AttributeError):
            device.nonexistent_attribute = 42


class TestConfigurationSettings:
    """Test configuration-specific settings."""

    def test_system_settings_loaded(self, cryo):
        """Test system settings from config."""
        # Lock timeout should be from config
        assert cryo.lock.timeout == 1.0

    def test_device_specific_settings(self, cryo):
        """Test device-specific settings from config."""
        # Magnets should have field ranges from config
        magnet1 = cryo["magnet1"]
        # Check field range was applied (can set within range)
        magnet1.field_setpoint = 5.0  # Within [-7, 7]

        magnet2 = cryo["magnet2"]
        magnet2.field_setpoint = 1.5  # Within [-2, 2]


# Run tests with: pytest cryostat/tests/test_cryostat.py -v
if __name__ == '__main__':
    pytest.main([__file__, '-v'])
