"""
Driver Factory
==============

Factory for creating driver instances from configuration.

Handles dynamic importing and instantiation of both mock and real
drivers, enabling seamless switching between development and production
environments.
"""

import logging
import importlib
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


# Driver Registry: Maps driver names to (module_path, class_name)
DRIVER_MAP = {
    # Mock Drivers (for testing/development)
    'mock_itc503': ('cryostat.drivers.mock.mock_itc503', 'MockITC503'),
    'mock_ips120': ('cryostat.drivers.mock.mock_ips120', 'MockIPS120'),
    'mock_ilm': ('cryostat.drivers.mock.mock_ilm', 'MockILM'),

    # Real Drivers (for production)
    'itc503': ('instruments.oxfordinstruments.itc503', 'ITC503'),
    'ips120_10': ('instruments.oxfordinstruments.ips120_10', 'IPS120_10'),
    # Note: Real ILM driver not yet implemented
    # 'ilm211': ('instruments.oxfordinstruments.ilm211', 'ILM211'),

    # Add more drivers as needed
    # 'lakeshore336': ('instruments.lakeshore.lakeshore336', 'LakeShore336'),
}


def create_driver(driver_name: str,
                  resource_string: str,
                  **kwargs: Any) -> Any:
    """Factory function to create driver instances.

    Dynamically imports and instantiates the appropriate driver class
    based on the driver name. Supports both mock and real drivers.

    Args:
        driver_name: Name of driver (must be in DRIVER_MAP)
        resource_string: Resource string for device (e.g., "GPIB::24" or "mock")
        **kwargs: Additional keyword arguments passed to driver constructor
                  (e.g., field_range, clear_buffer, num_channels)

    Returns:
        Driver instance (Instrument subclass)

    Raises:
        ValueError: If driver_name not found in DRIVER_MAP
        ImportError: If driver module cannot be imported
        Exception: If driver instantiation fails

    Example:
        >>> # Create mock driver
        >>> itc = create_driver('mock_itc503', 'mock')
        >>> print(itc.name)
        Mock ITC503

        >>> # Create real driver
        >>> itc = create_driver('itc503', 'GPIB::24')
        >>> print(itc.name)
        Oxford ITC503
    """
    log.info(f"Creating driver: {driver_name}")
    log.debug(f"Resource: {resource_string}, kwargs: {kwargs}")

    # Check if driver exists in registry
    if driver_name not in DRIVER_MAP:
        available = ', '.join(DRIVER_MAP.keys())
        raise ValueError(
            f"Unknown driver: '{driver_name}'. "
            f"Available drivers: {available}"
        )

    module_path, class_name = DRIVER_MAP[driver_name]

    try:
        # Dynamic import
        log.debug(f"Importing: {module_path}.{class_name}")
        module = importlib.import_module(module_path)
        driver_class = getattr(module, class_name)

        # Extract driver-specific kwargs from config
        driver_kwargs = _extract_driver_kwargs(driver_name, kwargs)

        # Instantiate driver (mock vs real)
        if is_mock_driver(driver_name):
            # Mock drivers don't use resource strings, they use FakeAdapter
            log.debug(f"Instantiating mock driver {class_name}")
            driver = driver_class(**driver_kwargs)
        else:
            # Real drivers use resource strings (VISA, Serial, etc.)
            log.debug(f"Instantiating {class_name} with resource '{resource_string}'")
            driver = driver_class(resource_string, **driver_kwargs)

        log.info(f"Driver created successfully: {driver.name}")
        return driver

    except ImportError as e:
        log.error(f"Failed to import driver module '{module_path}': {e}")
        raise ImportError(
            f"Driver module not found: {module_path}. "
            "Ensure the driver is installed or the path is correct."
        ) from e

    except AttributeError as e:
        log.error(f"Driver class '{class_name}' not found in module '{module_path}'")
        raise ImportError(
            f"Driver class '{class_name}' not found in {module_path}"
        ) from e

    except Exception as e:
        log.error(f"Failed to instantiate driver '{driver_name}': {e}")
        raise


def _extract_driver_kwargs(driver_name: str, config_kwargs: Dict) -> Dict:
    """Extract driver-specific kwargs from configuration.

    Filters configuration to only include parameters relevant to the
    specific driver type.

    Args:
        driver_name: Name of the driver
        config_kwargs: All kwargs from device configuration

    Returns:
        dict: Filtered kwargs appropriate for the driver

    Example:
        >>> kwargs = {'field_range': [-7, 7], 'description': 'Main magnet'}
        >>> driver_kwargs = _extract_driver_kwargs('mock_ips120', kwargs)
        >>> print(driver_kwargs)
        {'field_range': [-7, 7]}
    """
    # Common driver parameters (passed to all drivers)
    common_params = {
        'name', 'includeSCPI', 'read_termination', 'write_termination',
        'timeout', 'baud_rate', 'parity', 'data_bits', 'stop_bits'
    }

    # Driver-specific parameters
    driver_specific_params = {
        'mock_ips120': {'field_range'},
        'ips120_10': {
            'field_range', 'clear_buffer',
            'switch_heater_heating_delay', 'switch_heater_cooling_delay'
        },
        'mock_itc503': {'min_temperature', 'max_temperature'},
        'itc503': {'clear_buffer', 'min_temperature', 'max_temperature'},
        'mock_ilm': {'num_channels'},
    }

    # Get valid parameters for this driver
    valid_params = common_params.copy()
    if driver_name in driver_specific_params:
        valid_params.update(driver_specific_params[driver_name])

    # Filter kwargs
    driver_kwargs = {
        k: v for k, v in config_kwargs.items()
        if k in valid_params
    }

    # Exclude metadata fields
    metadata_fields = {'type', 'driver', 'resource', 'actions', 'description'}
    driver_kwargs = {
        k: v for k, v in driver_kwargs.items()
        if k not in metadata_fields
    }

    log.debug(f"Driver kwargs: {driver_kwargs}")
    return driver_kwargs


def register_driver(driver_name: str,
                   module_path: str,
                   class_name: str) -> None:
    """Register a new driver in the factory.

    Allows adding custom drivers at runtime without modifying factory code.

    Args:
        driver_name: Unique name for the driver
        module_path: Python import path to the module
        class_name: Name of the driver class

    Example:
        >>> register_driver(
        ...     'custom_temp_controller',
        ...     'my_drivers.custom_temp',
        ...     'CustomTempController'
        ... )
        >>> driver = create_driver('custom_temp_controller', 'GPIB::30')
    """
    if driver_name in DRIVER_MAP:
        log.warning(f"Overwriting existing driver registration: {driver_name}")

    DRIVER_MAP[driver_name] = (module_path, class_name)
    log.info(f"Registered driver: {driver_name} -> {module_path}.{class_name}")


def list_available_drivers():
    """List all available drivers in the factory.

    Returns:
        list: List of registered driver names

    Example:
        >>> drivers = list_available_drivers()
        >>> print(drivers)
        ['mock_itc503', 'mock_ips120', 'itc503', 'ips120_10', ...]
    """
    return list(DRIVER_MAP.keys())


def is_mock_driver(driver_name: str) -> bool:
    """Check if a driver is a mock driver.

    Args:
        driver_name: Name of the driver

    Returns:
        bool: True if mock driver, False if real driver

    Example:
        >>> is_mock_driver('mock_itc503')
        True
        >>> is_mock_driver('itc503')
        False
    """
    return driver_name.startswith('mock_')


def get_driver_info(driver_name: str) -> Optional[Dict[str, str]]:
    """Get information about a registered driver.

    Args:
        driver_name: Name of the driver

    Returns:
        dict: Dictionary with 'module_path' and 'class_name', or None if not found

    Example:
        >>> info = get_driver_info('mock_itc503')
        >>> print(info)
        {'module_path': 'cryostat.drivers.mock.mock_itc503',
         'class_name': 'MockITC503'}
    """
    if driver_name not in DRIVER_MAP:
        return None

    module_path, class_name = DRIVER_MAP[driver_name]
    return {
        'module_path': module_path,
        'class_name': class_name,
        'is_mock': is_mock_driver(driver_name)
    }


# Export public API
__all__ = [
    'DRIVER_MAP',
    'create_driver',
    'register_driver',
    'list_available_drivers',
    'is_mock_driver',
    'get_driver_info',
]
