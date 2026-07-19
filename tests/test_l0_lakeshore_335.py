# ---
# description: |
#   Unit tests for the SimLakeshore335 temperature controller driver.
#   Covers basic properties, setpoints, manual output limits, heater control
#   modes, PID parameter clamping, autotune toggles, and temperature simulation.
# entry_point: pytest tests/test_l0_lakeshore_335.py -v
# last_updated: 2026-07-16
# ---

"""Unit tests for SimLakeshore335 driver."""

from __future__ import annotations

import time
import pytest

from cryosoft.core.exceptions import CryoSoftCommunicationError
from cryosoft.drivers.sim_lakeshore_335 import SimLakeshore335


def test_idn():
    d = SimLakeshore335("SIM")
    assert d.get_idn() == "LSCI,MODEL335,SIM,1.0"


def test_setpoint():
    d = SimLakeshore335("SIM")
    assert d.get_setpoint() == 0.0
    d.set_setpoint(150.5)
    assert d.get_setpoint() == 150.5
    with pytest.raises(ValueError):
        d.set_setpoint(-5.0)


def test_heater_mode():
    d = SimLakeshore335("SIM")
    assert d.get_heater_mode() == "AUTO"  # Default
    d.set_heater_mode("MANUAL")
    assert d.get_heater_mode() == "MANUAL"
    d.set_heater_mode("AUTO")
    assert d.get_heater_mode() == "AUTO"
    with pytest.raises(ValueError):
        d.set_heater_mode("INVALID")


def test_heater_output_limits():
    d = SimLakeshore335("SIM")
    d.set_heater_output(50.0)
    # Settle simulation should update
    d.set_heater_mode("MANUAL")
    assert d.get_heater_output() == pytest.approx(50.0)
    
    d.set_heater_output(120.0)
    assert d.get_heater_output() == pytest.approx(99.9)
    d.set_heater_output(-10.0)
    assert d.get_heater_output() == pytest.approx(0.0)


def test_pid_clamping():
    d = SimLakeshore335("SIM")
    
    # Proportional
    assert d.get_proportional_band() == pytest.approx(90.0)
    d.set_proportional_band(120.5)
    assert d.get_proportional_band() == pytest.approx(120.5)
    d.set_proportional_band(2000.0)
    assert d.get_proportional_band() == pytest.approx(1000.0)
    d.set_proportional_band(-5.0)
    assert d.get_proportional_band() == pytest.approx(0.0)

    # Integral
    assert d.get_integral_action_time() == pytest.approx(50.0)
    d.set_integral_action_time(150.0)
    assert d.get_integral_action_time() == pytest.approx(150.0)
    d.set_integral_action_time(1200.0)
    assert d.get_integral_action_time() == pytest.approx(1000.0)
    d.set_integral_action_time(-10.0)
    assert d.get_integral_action_time() == pytest.approx(0.0)

    # Derivative
    assert d.get_derivative_action_time() == pytest.approx(0.0)
    d.set_derivative_action_time(85.0)
    assert d.get_derivative_action_time() == pytest.approx(85.0)
    d.set_derivative_action_time(300.0)
    assert d.get_derivative_action_time() == pytest.approx(200.0)
    d.set_derivative_action_time(-5.0)
    assert d.get_derivative_action_time() == pytest.approx(0.0)


def test_auto_pid():
    d = SimLakeshore335("SIM")
    assert d.get_auto_pid() is False
    d.set_auto_pid(True)
    assert d.get_auto_pid() is True
    d.set_auto_pid(False)
    assert d.get_auto_pid() is False


def test_simulation_evolution():
    d = SimLakeshore335("SIM")
    d.set_heater_mode("MANUAL")
    d.set_heater_output(50.0)
    # T_target = 4.2 + (50 / 99.9) * 295.8 = ~152.2 K
    d._temperature = 300.0
    d._last_update = time.time() - 3600.0  # 1 hour
    assert d.get_temperature() == pytest.approx(152.2, abs=0.5)


def test_error_injection():
    d = SimLakeshore335("SIM")
    d._simulate_error = True
    with pytest.raises(CryoSoftCommunicationError):
        d.get_temperature()
    with pytest.raises(CryoSoftCommunicationError):
        d.get_idn()


def test_sensor_curve_selection():
    d = SimLakeshore335("SIM")
    assert d.get_sensor_curve("A") == 22
    assert d.get_sensor_curve("B") == 2
    
    d.set_sensor_curve(21, "A")
    assert d.get_sensor_curve("A") == 21
    
    d.set_sensor_curve(23, "B")
    assert d.get_sensor_curve("B") == 23
    
    with pytest.raises(ValueError):
        d.set_sensor_curve(60, "A")
    with pytest.raises(ValueError):
        d.set_sensor_curve(-1, "A")
    with pytest.raises(ValueError):
        d.set_sensor_curve(21, "C")
