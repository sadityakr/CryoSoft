"""
Configuration System
====================

YAML-based configuration loading and device factory.

This module handles loading configuration files, validating them,
and creating driver instances based on the configuration. It provides
seamless switching between mock and real drivers.

Functions:
    load_config: Load and validate YAML configuration
    create_driver: Factory function for creating driver instances

Constants:
    DRIVER_MAP: Mapping of driver names to implementation classes
"""

from .loader import load_config
from .factory import create_driver, DRIVER_MAP

__all__ = [
    'load_config',
    'create_driver',
    'DRIVER_MAP',
]
