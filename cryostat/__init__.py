"""
Cryostat Control System
========================

A modular, three-layered architecture for controlling cryogenic experimental setups.

Layers:
    1. Drivers: Low-level device communication
    2. Actions: High-level multi-step operations
    3. Cryostat: Logical instrument providing unified access

Usage:
    from cryostat.core.cryostat import Cryostat

    cryo = Cryostat('cryostat/config/cryostat_mock.yaml')
    cryo["magnet1"].set_field(0.5)
    temp = cryo["temp_controller"].temperature_1

Author: Cryostat Control Team
Version: 0.1.0
"""

__version__ = "0.1.0"
__author__ = "Cryostat Control Team"

# Main exports
from .core.cryostat import Cryostat

__all__ = ['Cryostat']
