"""
Configuration Loader
====================

YAML configuration file loading and validation for the cryostat system.

Handles loading configuration files, validating structure, and setting
up system-wide logging based on configuration.
"""

import yaml
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def load_config(config_path):
    """Load and validate YAML configuration file.

    Args:
        config_path: Path to YAML configuration file (string or Path object)

    Returns:
        dict: Parsed and validated configuration

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If configuration is invalid
        yaml.YAMLError: If YAML parsing fails

    Example:
        >>> config = load_config('cryostat/config/cryostat_mock.yaml')
        >>> devices = config['cryostat']['devices']
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    log.info(f"Loading configuration: {config_path}")

    try:
        # Load YAML
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        if config is None:
            raise ValueError("Configuration file is empty")

        # Validate structure
        _validate_config(config)

        # Setup logging from config
        _setup_logging_from_config(config.get('logging', {}))

        log.info("Configuration loaded successfully")
        log.debug(f"Devices: {list(config['cryostat']['devices'].keys())}")

        return config

    except yaml.YAMLError as e:
        log.error(f"YAML parsing error: {e}")
        raise

    except Exception as e:
        log.error(f"Configuration loading failed: {e}")
        raise


def _validate_config(config):
    """Validate configuration structure.

    Args:
        config: Parsed configuration dictionary

    Raises:
        ValueError: If configuration structure is invalid
    """
    # Check top-level structure
    if 'cryostat' not in config:
        raise ValueError("Configuration must contain 'cryostat' section")

    if 'devices' not in config['cryostat']:
        raise ValueError("Configuration must contain 'cryostat.devices' section")

    devices = config['cryostat']['devices']

    if not isinstance(devices, dict):
        raise ValueError("'cryostat.devices' must be a dictionary")

    if len(devices) == 0:
        raise ValueError("At least one device must be configured")

    # Validate each device
    for device_name, device_config in devices.items():
        _validate_device(device_name, device_config)

    log.debug(f"Configuration validation passed ({len(devices)} devices)")


def _validate_device(device_name, device_config):
    """Validate individual device configuration.

    Args:
        device_name: Name of the device
        device_config: Device configuration dictionary

    Raises:
        ValueError: If device configuration is invalid
    """
    required_fields = ['type', 'driver', 'resource', 'actions']

    # Check required fields
    for field in required_fields:
        if field not in device_config:
            raise ValueError(
                f"Device '{device_name}' missing required field: '{field}'"
            )

    # Validate device type
    valid_types = ['temperature', 'magnet', 'level_meter']
    device_type = device_config['type']

    if device_type not in valid_types:
        raise ValueError(
            f"Device '{device_name}' has invalid type: '{device_type}' "
            f"(valid types: {valid_types})"
        )

    # Validate resource string
    resource = device_config['resource']
    if not isinstance(resource, str) or len(resource) == 0:
        raise ValueError(
            f"Device '{device_name}' has invalid resource string: '{resource}'"
        )

    # Optional: validate field_range for magnets
    if device_type == 'magnet' and 'field_range' in device_config:
        field_range = device_config['field_range']
        if not (isinstance(field_range, (list, tuple)) and len(field_range) == 2):
            raise ValueError(
                f"Device '{device_name}' has invalid field_range: {field_range} "
                "(must be [min, max])"
            )

    log.debug(f"Device '{device_name}' validation passed")


def _setup_logging_from_config(logging_config):
    """Setup logging based on configuration.

    Args:
        logging_config: Logging configuration dictionary

    Note:
        This is a basic setup. For advanced logging, use
        core.logger.setup_cryostat_logging()
    """
    if not logging_config:
        return

    # Get level
    level_str = logging_config.get('level', 'INFO')
    try:
        level = getattr(logging, level_str.upper())
    except AttributeError:
        log.warning(f"Invalid log level: {level_str}, using INFO")
        level = logging.INFO

    # Get format
    format_str = logging_config.get(
        'format',
        '%(asctime)s [%(levelname)8s] %(name)s - %(message)s'
    )

    # Setup basic config
    handlers = []

    # Console handler
    if logging_config.get('console', True):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(logging.Formatter(format_str))
        handlers.append(console_handler)

    # File handler
    if 'file' in logging_config:
        log_file = logging_config['file']
        try:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(logging.Formatter(format_str))
            handlers.append(file_handler)
            log.debug(f"Logging to file: {log_file}")
        except Exception as e:
            log.warning(f"Could not setup file logging: {e}")

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add new handlers
    for handler in handlers:
        root_logger.addHandler(handler)

    log.debug(f"Logging configured: level={level_str}")


def get_device_names(config):
    """Get list of device names from configuration.

    Args:
        config: Loaded configuration dictionary

    Returns:
        list: List of device names

    Example:
        >>> names = get_device_names(config)
        >>> print(names)
        ['vti_temp_controller', 'magnet1', 'magnet2', ...]
    """
    try:
        return list(config['cryostat']['devices'].keys())
    except KeyError:
        return []


def get_device_config(config, device_name):
    """Get configuration for specific device.

    Args:
        config: Loaded configuration dictionary
        device_name: Name of device to retrieve

    Returns:
        dict: Device configuration, or None if not found

    Example:
        >>> device_cfg = get_device_config(config, 'magnet1')
        >>> print(device_cfg['driver'])
        mock_ips120
    """
    try:
        return config['cryostat']['devices'].get(device_name)
    except KeyError:
        return None


def get_system_setting(config, setting_name, default=None):
    """Get system setting from configuration.

    Args:
        config: Loaded configuration dictionary
        setting_name: Name of setting to retrieve
        default: Default value if setting not found

    Returns:
        Setting value, or default if not found

    Example:
        >>> timeout = get_system_setting(config, 'lock_timeout', 1.0)
        >>> print(timeout)
        1.0
    """
    try:
        return config.get('system', {}).get(setting_name, default)
    except Exception:
        return default


# Export public functions
__all__ = [
    'load_config',
    'get_device_names',
    'get_device_config',
    'get_system_setting',
]
