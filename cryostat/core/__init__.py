"""
Cryostat Core Layer
===================

The logical instrument layer providing unified access to all devices.

This layer implements the composite pattern, presenting the cryostat
as a single unified instrument while managing multiple physical devices.
It provides thread-safe access and dict-like device access.

Classes:
    Cryostat: Main cryostat logical instrument
    DeviceWrapper: Wraps driver+actions with locking
    ThreadSafeLock: Global I/O lock with 1-second timeout

Functions:
    setup_cryostat_logging: Configure logging for the system
"""

from .cryostat import Cryostat
from .device_wrapper import DeviceWrapper
from .lock_manager import ThreadSafeLock
from .logger import setup_cryostat_logging

__all__ = [
    'Cryostat',
    'DeviceWrapper',
    'ThreadSafeLock',
    'setup_cryostat_logging',
]
