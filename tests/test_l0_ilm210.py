# ---
# description: |
#   Unit tests for the SimOxfordILM210 cryogen level meter driver.
#   Covers basic properties, levels, refresh rates, drift simulation,
#   and simulated error injection.
# entry_point: pytest tests/test_l0_ilm210.py -v
# last_updated: 2026-07-16
# ---

"""Unit tests for SimOxfordILM210 driver."""

from __future__ import annotations

import time
import pytest

from cryosoft.core.exceptions import CryoSoftCommunicationError
from cryosoft.drivers.sim_oxford_ilm210 import SimOxfordILM210


def test_idn():
    d = SimOxfordILM210("SIM")
    assert d.get_idn() == "OXFORD,ILM210,SIM,1.0"


def test_helium_level():
    d = SimOxfordILM210("SIM")
    assert d.get_helium_level() == pytest.approx(80.0)
    
    # Test force override
    d._force_helium_level = 45.2
    assert d.get_helium_level() == pytest.approx(45.2)


def test_nitrogen_level():
    d = SimOxfordILM210("SIM")
    assert d.get_nitrogen_level() == pytest.approx(90.0)


def test_refresh_rates():
    d = SimOxfordILM210("SIM")
    assert d.get_refresh_rate() == 0  # Default
    
    d.set_refresh_rate(1)
    assert d.get_refresh_rate() == 1
    
    d.set_refresh_rate(2)
    assert d.get_refresh_rate() == 2
    
    with pytest.raises(ValueError):
        d.set_refresh_rate(3)
    with pytest.raises(ValueError):
        d.set_refresh_rate(-1)


def test_simulation_drift():
    d = SimOxfordILM210("SIM")
    d._helium_level = 80.0
    d._last_update = time.time() - 3600.0  # 1 hour ago
    # 60 mins drift at 0.01 %/min = 0.6 % decrease
    assert d.get_helium_level() == pytest.approx(79.4)


def test_error_injection():
    d = SimOxfordILM210("SIM")
    d._simulate_error = True
    with pytest.raises(CryoSoftCommunicationError):
        d.get_helium_level()
    with pytest.raises(CryoSoftCommunicationError):
        d.get_idn()
