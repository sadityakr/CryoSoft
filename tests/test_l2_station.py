# ---
# description: |
#   Integration tests for the Station class and build_station factory using the
#   simulated cryostat YAML configuration.
# last_updated: 2026-07-12
# ---

from pathlib import Path

import pytest
from cryosoft.core.station import Station, build_station


class _AddressCapturingDriver:
    """Test double for build_station(): records the resource string it was built with."""

    last_resource: str | None = None

    def __init__(self, resource_string: str) -> None:
        type(self).last_resource = resource_string

    def get_state(self) -> dict:
        return {}


@pytest.fixture
def sim_station():
    """Fixture to build a Station from the sim_cryostat configuration."""
    config_path = Path(__file__).parent.parent / "cryosoft" / "configs" / "sim_cryostat"
    return build_station(str(config_path))


def test_build_station_passes_address_to_driver(tmp_path):
    """build_station() must pass the YAML 'address:' value to the driver constructor.

    Regression test: build_station() previously read a nonexistent
    'resource_string' key and silently defaulted every real driver to
    'SIM', so real hardware would never receive its actual VISA address.
    """
    (tmp_path / "devices.yaml").write_text(
        "real_drivers:\n"
        "  probe:\n"
        "    class: tests.test_l2_station._AddressCapturingDriver\n"
        '    address: "GPIB0::19::INSTR"\n'
        "virtual_instruments: {}\n"
    )
    (tmp_path / "monitor.yaml").write_text(
        "monitor:\n  tick_interval_ms: 1000\n  max_vi_errors: 3\n"
    )

    build_station(str(tmp_path))

    assert _AddressCapturingDriver.last_resource == "GPIB0::19::INSTR"


def test_build_station_success(sim_station: Station):
    """build_station('cryosoft/configs/sim_cryostat') works without errors."""
    assert sim_station is not None
    # Check that expected VIs are registered
    vi_names = sim_station.get_vi_names()
    expected = ["magnet_x", "magnet_y", "temperature_vti", "temperature_sample", "level_meter", "keithley_delta_mode", "dc_measurement"]
    for name in expected:
        assert name in vi_names


def test_station_getattr(sim_station: Station):
    """station.magnet_x returns correct VI instance."""
    magnet_x = sim_station.magnet_x
    assert magnet_x.vi_name == "magnet_x"
    assert magnet_x.__class__.__name__ == "SuperconductingMagnetVI"

    # Check another one to be sure
    temp_vti = sim_station.temperature_vti
    assert temp_vti.vi_name == "temperature_vti"
    assert temp_vti.__class__.__name__ == "VTITemperatureControllerVI"


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


def test_process_system_targets_forwards_persistent_key(sim_station: Station):
    """An optional 'persistent' key in a target dict is forwarded to start_ramp().

    sim_cryostat's magnet_x is a plain SuperconductingMagnetVI, which accepts
    persistent= as a no-op — this must not raise, so any procedure can include
    'persistent' in a magnet target regardless of which magnet VI flavor a
    config wires up.
    """
    sim_station.process_system_targets({"magnet_x": {"target": 1.0, "persistent": False}})
    assert sim_station.magnet_x.ramp_status() == "RAMPING"


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
    """check_safety() aggregates the level meter's DEBOUNCED helium verdict.

    The helium flag comes from the level-meter VI's majority-vote buffer
    (filled during get_state() polls) — a single glitched low reading must
    NOT trip it, and check_safety() itself never polls hardware.
    """
    # Warm up get_state cache
    sim_station.get_state()
    safety = sim_station.check_safety()
    assert safety["helium_low"] is False

    # Simulate a low helium condition
    level_driver = sim_station.level_meter._driver
    level_driver._force_helium_level = 5.0

    # One low poll is a glitch — debounce must suppress it.
    sim_station.get_state()
    safety = sim_station.check_safety()
    assert safety["helium_low"] is False

    # A sustained low level (buffer majority) must trip the flag.
    for _ in range(3):
        sim_station.get_state()
    safety = sim_station.check_safety()
    assert safety["helium_low"] is True

    # Simulate disconnected level meter -> assumes unsafe
    level_driver._simulate_error = True
    for _ in range(3):
        sim_station.get_state()  # Trigger _disconnected

    safety = sim_station.check_safety()
    # It should still be True because of disconnection assumption
    assert safety["helium_low"] is True


def test_check_safety_uses_snapshot_without_polling(sim_station: Station):
    """check_safety(state) must not poll hardware (review finding H1).

    The old implementation called get_state() internally, doubling GPIB
    traffic every tick and double-counting the error counters.
    """
    state = sim_station.get_state()
    level_driver = sim_station.level_meter._driver
    calls_before = getattr(level_driver, "_get_helium_calls", None)

    # Count driver polls around check_safety via a wrapper.
    call_count = {"n": 0}
    original = level_driver.get_helium_level

    def counting(*args, **kwargs):
        call_count["n"] += 1
        return original(*args, **kwargs)

    level_driver.get_helium_level = counting
    try:
        sim_station.check_safety(state)
        sim_station.check_safety()  # cached-state variant
    finally:
        level_driver.get_helium_level = original
    _ = calls_before
    assert call_count["n"] == 0


def test_check_safety_flags_magnet_quench(sim_station: Station):
    """A magnet reporting QUENCH must trip the 'quench' safety flag."""
    sim_station.get_state()
    assert sim_station.check_safety().get("quench", False) is False

    sim_station.magnet_x._driver._simulate_quench = True
    state = sim_station.get_state()
    safety = sim_station.check_safety(state)
    assert safety["quench"] is True

