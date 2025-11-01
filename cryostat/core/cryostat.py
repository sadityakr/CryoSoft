"""
Cryostat Main Class
===================

The logical cryostat instrument that unifies all devices.

This class acts as a composite instrument, providing dict-like access
to all configured devices while ensuring thread-safe I/O and managing
the complete system lifecycle.
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from pymeasure.instruments import Instrument
from pymeasure.adapters import FakeAdapter

from .lock_manager import ThreadSafeLock
from .device_wrapper import DeviceWrapper
from ..config.loader import load_config, get_system_setting
from ..config.factory import create_driver
from ..actions import TemperatureActions, MagnetActions, LevelMeterActions

log = logging.getLogger(__name__)


# Map device types to action classes
ACTION_MAP = {
    'temperature': TemperatureActions,
    'magnet': MagnetActions,
    'level_meter': LevelMeterActions,
}


class Cryostat(Instrument):
    """Logical cryostat instrument providing unified device access.

    The Cryostat class acts as a composite instrument that:
    - Loads configuration from YAML
    - Creates and manages multiple device drivers
    - Provides thread-safe dict-style access to devices
    - Inherits from PyMeasure Instrument for compatibility

    Features:
    - Dict-style access: cryostat["magnet1"]
    - Thread-safe I/O with 1-second timeout lock
    - Automatic driver creation (mock/real switching)
    - YAML-based configuration
    - Device lifecycle management

    Example:
        >>> # Initialize from configuration
        >>> cryo = Cryostat('cryostat/config/cryostat_mock.yaml')
        >>>
        >>> # List devices
        >>> print(cryo.list_devices())
        ['vti_temp_controller', 'magnet1', 'magnet2', ...]
        >>>
        >>> # Access devices (dict-style)
        >>> temp_ctrl = cryo["vti_temp_controller"]
        >>> temp_ctrl.initiate()
        >>> temp = temp_ctrl.temperature_1
        >>>
        >>> # Access magnet
        >>> magnet = cryo["magnet1"]
        >>> magnet.ramp_to_field(0.5, rate=0.1)
    """

    def __init__(self, config_path: str, **kwargs):
        """Initialize cryostat from configuration file.

        Args:
            config_path: Path to YAML configuration file
            **kwargs: Additional arguments passed to Instrument base class

        Raises:
            FileNotFoundError: If config file doesn't exist
            ValueError: If configuration is invalid
            Exception: If device initialization fails

        Example:
            >>> cryo = Cryostat('cryostat/config/cryostat_mock.yaml')
            >>> # or
            >>> cryo = Cryostat('cryostat/config/cryostat_real.yaml')
        """
        # Initialize as Instrument with FakeAdapter (cryostat is composite)
        super().__init__(FakeAdapter(), "Cryostat", **kwargs)

        log.info("=" * 60)
        log.info("Initializing Cryostat System")
        log.info("=" * 60)

        # Load configuration
        log.info(f"Loading configuration: {config_path}")
        self._config = load_config(config_path)
        self._config_path = Path(config_path)

        # Get system settings
        lock_timeout = get_system_setting(self._config, 'lock_timeout', 1.0)

        # Create global I/O lock
        self._lock = ThreadSafeLock(timeout=lock_timeout)
        log.info(f"Global I/O lock created (timeout={lock_timeout}s)")

        # Device registry
        self._devices: Dict[str, DeviceWrapper] = {}

        # Initialize all devices
        self._initialize_devices()

        log.info("=" * 60)
        log.info(f"Cryostat initialized successfully ({len(self._devices)} devices)")
        log.info("=" * 60)

    def _initialize_devices(self):
        """Initialize all devices from configuration.

        Creates drivers, wraps with actions, and registers in device dict.
        """
        devices_config = self._config.get('cryostat', {}).get('devices', {})

        if not devices_config:
            log.warning("No devices configured")
            return

        log.info(f"Initializing {len(devices_config)} device(s)...")

        for device_name, device_config in devices_config.items():
            try:
                self._initialize_device(device_name, device_config)
            except Exception as e:
                log.error(f"Failed to initialize device '{device_name}': {e}")
                # Continue with other devices
                continue

    def _initialize_device(self, device_name: str, device_config: Dict):
        """Initialize a single device.

        Args:
            device_name: Unique device name
            device_config: Device configuration dictionary
        """
        log.info(f"Initializing device: {device_name}")

        # Extract configuration
        driver_name = device_config['driver']
        resource = device_config['resource']
        device_type = device_config['type']
        description = device_config.get('description', '')

        log.debug(f"  Type: {device_type}")
        log.debug(f"  Driver: {driver_name}")
        log.debug(f"  Resource: {resource}")
        if description:
            log.debug(f"  Description: {description}")

        # Create driver
        driver_kwargs = {k: v for k, v in device_config.items()
                        if k not in ['type', 'driver', 'resource', 'actions', 'description']}

        driver = create_driver(driver_name, resource, **driver_kwargs)

        # Get action handler class
        action_class = ACTION_MAP.get(device_type)
        if action_class is None:
            raise ValueError(f"Unknown device type: {device_type}")

        # Wrap device
        wrapper = DeviceWrapper(device_name, driver, action_class, self._lock)

        # Register
        self._devices[device_name] = wrapper
        log.info(f"Device '{device_name}' registered successfully")

    # ==================== Dict-Style Access ====================

    def __getitem__(self, device_name: str) -> DeviceWrapper:
        """Get device by name (dict-style access).

        Args:
            device_name: Name of device to retrieve

        Returns:
            DeviceWrapper: Device wrapper instance

        Raises:
            KeyError: If device not found

        Example:
            >>> magnet = cryo["magnet1"]
            >>> temp_ctrl = cryo["vti_temp_controller"]
        """
        if device_name not in self._devices:
            available = ', '.join(self._devices.keys())
            raise KeyError(
                f"Device '{device_name}' not found. "
                f"Available devices: {available}"
            )
        return self._devices[device_name]

    def __contains__(self, device_name: str) -> bool:
        """Check if device exists (supports 'in' operator).

        Args:
            device_name: Name of device to check

        Returns:
            bool: True if device exists

        Example:
            >>> if "magnet1" in cryo:
            ...     print("Magnet1 is available")
        """
        return device_name in self._devices

    def __len__(self) -> int:
        """Get number of devices.

        Returns:
            int: Number of configured devices

        Example:
            >>> print(f"Cryostat has {len(cryo)} devices")
        """
        return len(self._devices)

    def __iter__(self):
        """Iterate over device names.

        Yields:
            str: Device names

        Example:
            >>> for device_name in cryo:
            ...     print(device_name)
        """
        return iter(self._devices.keys())

    # ==================== Device Management ====================

    def list_devices(self) -> List[str]:
        """Get list of all device names.

        Returns:
            list: List of device names

        Example:
            >>> devices = cryo.list_devices()
            >>> print(devices)
            ['vti_temp_controller', 'magnet1', 'magnet2', ...]
        """
        return list(self._devices.keys())

    def get_device(self, device_name: str) -> Optional[DeviceWrapper]:
        """Get device by name (returns None if not found).

        Args:
            device_name: Name of device to retrieve

        Returns:
            DeviceWrapper or None: Device wrapper or None if not found

        Example:
            >>> device = cryo.get_device("magnet1")
            >>> if device:
            ...     print("Found magnet1")
        """
        return self._devices.get(device_name)

    def get_devices_by_type(self, device_type: str) -> Dict[str, DeviceWrapper]:
        """Get all devices of specific type.

        Args:
            device_type: Type of devices ('temperature', 'magnet', 'level_meter')

        Returns:
            dict: Dictionary of devices of specified type

        Example:
            >>> magnets = cryo.get_devices_by_type('magnet')
            >>> for name, magnet in magnets.items():
            ...     print(f"{name}: {magnet.field}T")
        """
        devices_config = self._config['cryostat']['devices']
        matching_devices = {}

        for device_name, wrapper in self._devices.items():
            if devices_config[device_name]['type'] == device_type:
                matching_devices[device_name] = wrapper

        return matching_devices

    # ==================== System Status ====================

    def get_all_status(self) -> Dict[str, Dict]:
        """Get status of all devices.

        Returns:
            dict: Dictionary mapping device names to status dicts

        Example:
            >>> status = cryo.get_all_status()
            >>> for device, info in status.items():
            ...     print(f"{device}: {info}")
        """
        status = {}

        for device_name, device in self._devices.items():
            try:
                status[device_name] = device.get_status()
            except Exception as e:
                log.error(f"Failed to get status for '{device_name}': {e}")
                status[device_name] = {'error': str(e)}

        return status

    def initiate_all(self) -> Dict[str, bool]:
        """Initialize all devices.

        Returns:
            dict: Dictionary mapping device names to success status

        Example:
            >>> results = cryo.initiate_all()
            >>> if all(results.values()):
            ...     print("All devices initialized")
        """
        log.info("Initiating all devices...")
        results = {}

        for device_name, device in self._devices.items():
            try:
                success = device.initiate()
                results[device_name] = success
                log.info(f"Device '{device_name}': {'OK' if success else 'FAILED'}")
            except Exception as e:
                log.error(f"Device '{device_name}' initialization failed: {e}")
                results[device_name] = False

        return results

    # ==================== Lock Management ====================

    @property
    def lock(self) -> ThreadSafeLock:
        """Access to global I/O lock.

        Returns:
            ThreadSafeLock: The global lock instance

        Example:
            >>> stats = cryo.lock.get_stats()
            >>> print(f"Lock acquisitions: {stats['acquisition_count']}")
        """
        return self._lock

    def get_lock_stats(self) -> Dict:
        """Get statistics about lock usage.

        Returns:
            dict: Lock statistics

        Example:
            >>> stats = cryo.get_lock_stats()
            >>> print(stats)
            {'acquisition_count': 42, 'timeout_count': 0, ...}
        """
        return self._lock.get_stats()

    # ==================== Configuration ====================

    @property
    def config(self) -> Dict:
        """Access to configuration dictionary.

        Returns:
            dict: Configuration dictionary
        """
        return self._config

    @property
    def config_path(self) -> Path:
        """Path to configuration file.

        Returns:
            Path: Configuration file path
        """
        return self._config_path

    # ==================== Repr ====================

    def __repr__(self):
        device_list = ', '.join(self._devices.keys())
        return f"<Cryostat({len(self._devices)} devices: {device_list})>"

    def __str__(self):
        lines = ["Cryostat System"]
        lines.append(f"  Configuration: {self._config_path}")
        lines.append(f"  Devices ({len(self._devices)}):")
        for name, device in self._devices.items():
            lines.append(f"    - {name}: {device.driver.__class__.__name__}")
        return '\n'.join(lines)


# Export public API
__all__ = ['Cryostat']
