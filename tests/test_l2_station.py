# ---
# description: |
#   Integration tests for the Station class and build_station factory using the
#   simulated cryostat YAML configuration.
# last_updated: 2026-04-06
# ---

import os
from pathlib import Path

import pytest
from cryosoft.core.station import Station, build_station
from cryosoft.core.exceptions import CryoSoftCommunicationError


@pytest.fixture
def sim_station():
    """Fixture to build a Station from the sim_cryostat configuration."""
    config_path = Path(__file__).parent.parent / "cryosoft" / "configs" / "sim_cryostat"
    return build_station(str(config_path))


def test_build_station_success(sim_station: Station):
    """build_station('cryosoft/configs/sim_cryostat') works without errors."""
    assert sim_station is not None
    # Check that expected VIs are registered
    vi_names = sim_station.get_vi_names()
    expected = ["magnet_x", "magnet_y", "temperature_vti", "temperature_sample", "level_meter", "iv_measurement"]
    for name in expected:
        assert name in vi_names


def test_station_getattr(sim_station: Station):
    """station.magnet_x returns correct VI instance."""
    magnet_x = sim_station.magnet_x
    assert magnet_x.vi_name == "magnet_x"
    assert magnet_x.__class__.__name__ == "IPS120MagnetVI"
    
    # Check another one to be sure
    temp_vti = sim_station.temperature_vti
    assert temp_vti.vi_name == "temperature_vti"
    assert temp_vti.__class__.__name__ == "ITC503TemperatureVI"


def test_get_state_format(sim_station: Station):
    """get_state() returns dict with all VI states."""
    state = sim_station.get_state()
    
    # Assert top-level keys are the VI names
    for name in sim_station.get_vi_names():
        assert name in state
    
    # Assert a specific VI state contains its @monitored variables
    magnet_state = state["magnet_x"]
    assert "magnet_current" in magnet_state
    assert "get_field" in magnet_state
    assert "magnet_status" in magnet_state


def test_get_state_error_handling(sim_station: Station):
    """Stale values with _stale: True on communication error, _disconnected after max."""
    # Run once to get good values in the cache
    sim_station.get_state()
    
    # Force the simulated magnet driver to simulate an error
    magnet_x = sim_station.magnet_x
    magnet_x._driver._simulate_error = True
    
    # 1st error -> should return stale data with _stale: True
    state = sim_station.get_state()
    assert state["magnet_x"]["_stale"] is True
    assert "_disconnected" not in state["magnet_x"]
    assert sim_station._error_counts["magnet_x"] == 1
    
    # 2nd error
    sim_station.get_state()
    assert sim_station._error_counts["magnet_x"] == 2
    
    # 3rd error -> should now also have _disconnected: True
    state = sim_station.get_state()
    assert state["magnet_x"]["_stale"] is True
    assert state["magnet_x"].get("_disconnected") is True
    assert sim_station._error_counts["magnet_x"] == 3


def test_process_system_targets_dispatch(sim_station: Station):
    """process_system_targets dispatches to correct VIs only."""
    targets = {
        "magnet_x": {"target": 1.0},
        "temperature_vti": {"target": 150.0}
    }
    
    sim_station.process_system_targets(targets)
    
    # Verify that the ramps have started
    assert sim_station.magnet_x.ramp_status() == "RAMPING"
    assert sim_station.temperature_vti.ramp_status() == "RAMPING"
    
    # Verify that un-targeted system VIs are NOT ramping
    assert sim_station.magnet_y.ramp_status() == "IDLE"
    assert sim_station.temperature_sample.ramp_status() == "IDLE"

    # process_system_targets should raise if we pass a non-system VI
    with pytest.raises(ValueError):
        sim_station.process_system_targets({"level_meter": {"target": 10.0}})


def test_check_ramps(sim_station: Station):
    """check_ramps() returns False while ramping, True after done."""
    # Ensure initially True (all are IDLE)
    assert sim_station.check_ramps() is True
    
    # Start a ramp
    sim_station.process_system_targets({"magnet_x": {"target": 1.0}})
    
    # While ramping, should return False
    assert sim_station.check_ramps() is False
    
    # Force the ramp to complete
    # For magnet_x, it uses a generator and advances the actual value. We can force the target.
    # We will simulate enough ticks until the magnet reaches the setpoint.
    # The sim driver has a ramp_rate (5.0 A/min = 0.083 A/s).
    # Target 1.0 T = 10 A. By setting the driver's current to the target, we make it reach HOLD immediately.
    magnet_driver = sim_station.magnet_x._driver
    magnet_driver._current = 10.0
    magnet_driver._setpoint = 10.0
    magnet_driver._status = "HOLD"
    
    # The VI's generator needs to be ticked to recognize it reached the target
    sim_station.check_ramps()
    
    # Now it should be True
    assert sim_station.check_ramps() is True


def test_check_safety(sim_station: Station):
    """check_safety() returns {'helium_low': bool} accurately."""
    # Warm up get_state cache
    sim_station.get_state()
    safety = sim_station.check_safety()
    assert safety["helium_low"] is False
    
    # Simulate a low helium condition (below 10%)
    level_driver = sim_station.level_meter._driver
    level_driver._force_helium_level = 5.0
    
    sim_station.get_state()  # pull new low value
    safety = sim_station.check_safety()
    assert safety["helium_low"] is True

    # Simulate disconnected level meter -> assumes unsafe
    level_driver._simulate_error = True
    for _ in range(3):
        sim_station.get_state()  # Trigger _disconnected
    
    safety = sim_station.check_safety()
    # It should still be True because of disconnection assumption
    assert safety["helium_low"] is True

