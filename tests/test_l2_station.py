# ---
# description: |
#   Integration tests for the Station class and build_station factory using the
#   simulated cryostat YAML configuration.
# last_updated: 2026-07-12
# ---

from pathlib import Path

import pytest
from cryosoft.core.plan import Target
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


def test_read_panels_config_well_formed(tmp_path):
    """read_panels_config returns per-VI control allowlists from monitor.yaml."""
    from cryosoft.core.station import read_panels_config

    (tmp_path / "monitor.yaml").write_text(
        "monitor:\n  tick_interval_ms: 1000\n"
        "panels:\n"
        "  temperature_vti:\n"
        "    controls: [set_temperature]\n"
        "  magnet_z:\n"
        "    controls: [set_field, set_ramp_rate]\n"
    )
    assert read_panels_config(str(tmp_path)) == {
        "temperature_vti": ["set_temperature"],
        "magnet_z": ["set_field", "set_ramp_rate"],
    }


def test_read_panels_config_tolerates_absent_or_malformed(tmp_path):
    """Absent block, malformed entries, or a missing file yield {} / skip, never raise."""
    from cryosoft.core.station import read_panels_config

    # No monitor.yaml at all.
    assert read_panels_config(str(tmp_path / "nowhere")) == {}
    # No panels block.
    (tmp_path / "monitor.yaml").write_text("monitor:\n  tick_interval_ms: 1000\n")
    assert read_panels_config(str(tmp_path)) == {}
    # Malformed entries are skipped; the well-formed one survives.
    (tmp_path / "monitor.yaml").write_text(
        "monitor:\n  tick_interval_ms: 1000\n"
        "panels:\n"
        "  bad_scalar: just_a_string\n"
        "  bad_no_controls:\n"
        "    other_key: 1\n"
        "  good_vi:\n"
        "    controls: [set_x]\n"
    )
    assert read_panels_config(str(tmp_path)) == {"good_vi": ["set_x"]}


def test_build_station_success(sim_station: Station):
    """build_station('cryosoft/configs/sim_cryostat') works without errors."""
    assert sim_station is not None
    # Check that expected VIs are registered
    vi_names = sim_station.get_vi_names()
    expected = ["magnet_z", "magnet_y", "temperature_vti", "temperature_sample", "level_meter", "keithley_delta_mode", "dc_measurement"]
    for name in expected:
        assert name in vi_names


def test_station_getattr(sim_station: Station):
    """station.magnet_z returns correct VI instance."""
    magnet_z = sim_station.magnet_z
    assert magnet_z.vi_name == "magnet_z"
    assert magnet_z.__class__.__name__ == "SuperconductingMagnetVI"

    # Check another one to be sure
    temp_vti = sim_station.temperature_vti
    assert temp_vti.vi_name == "temperature_vti"
    assert temp_vti.__class__.__name__ == "VTITemperatureControllerVI"


def _registry_stub():
    """A minimal VI-like stub for registry-only tests (never polled here)."""

    class _Stub:
        vi_name = ""

        def get_state(self) -> dict:
            return {}

    return _Stub()


def test_station_get_vi_returns_named_instance(sim_station: Station):
    """get_vi(name) returns the same instance as attribute access."""
    assert sim_station.get_vi("magnet_z") is sim_station.magnet_z
    with pytest.raises(KeyError):
        sim_station.get_vi("no_such_vi")


def test_station_measurement_vi_names_registration_order(sim_station: Station):
    """measurement_vi_names() returns only measurement VIs, in registration order.

    sim_cryostat registers keithley_delta_mode, then dc_measurement, then
    lockin_harmonic (all vi_type=measurement); the system/level VIs are
    excluded.
    """
    assert sim_station.measurement_vi_names() == [
        "keithley_delta_mode", "dc_measurement", "lockin_harmonic",
    ]


def test_station_measurement_vi_names_empty_when_none_registered():
    """A station with no measurement VIs reports an empty list."""
    station = Station()
    station.register_vi("magnet_z", _registry_stub(), "system")
    assert station.measurement_vi_names() == []


def test_station_switch_vi_names_registration_order(sim_station: Station):
    """switch_vi_names() returns only switch VIs (sim_cryostat has switch_matrix)."""
    assert sim_station.switch_vi_names() == ["switch_matrix"]


def test_station_switch_vi_names_empty_when_none_registered():
    """A station with no switch VIs reports an empty list."""
    station = Station()
    station.register_vi("magnet_z", _registry_stub(), "system")
    assert station.switch_vi_names() == []


def test_get_ramp_status_covers_system_rampables(sim_station: Station):
    """get_ramp_status() returns a target/rate/ramp_status entry for every system
    VI that can ramp, excludes measurement VIs, and reports idle VIs as IDLE with
    a None target."""
    ramps = sim_station.get_ramp_status()

    assert "magnet_z" in ramps
    assert "temperature_sample" in ramps
    for entry in ramps.values():
        assert {"target", "rate", "ramp_status"} <= set(entry)

    # Nothing commanded yet: idle, no target.
    assert ramps["magnet_z"]["ramp_status"] == "IDLE"
    assert ramps["magnet_z"]["target"] is None

    # Measurement VIs are not ramp targets and must not appear.
    assert "dc_measurement" not in ramps


def test_get_ramp_status_reports_active_target(sim_station: Station):
    """After a system VI starts ramping, its live target shows up in the aggregate."""
    sim_station.process_system_targets({"magnet_z": Target(1.0)})
    ramps = sim_station.get_ramp_status()
    assert ramps["magnet_z"]["ramp_status"] == "RAMPING"
    assert ramps["magnet_z"]["target"] == pytest.approx(1.0)


def test_get_state_format(sim_station: Station):
    """get_state() returns dict with all VI states."""
    state = sim_station.get_state()
    
    # Assert top-level keys are the VI names
    for name in sim_station.get_vi_names():
        assert name in state
    
    # Assert a specific VI state contains its @monitored variables
    magnet_state = state["magnet_z"]
    assert "magnet_current" in magnet_state
    assert "get_field" in magnet_state
    assert "magnet_status" in magnet_state


def test_get_state_error_handling(sim_station: Station):
    """Stale values with _stale: True on communication error, _disconnected after max."""
    # Run once to get good values in the cache
    sim_station.get_state()
    
    # Force the simulated magnet driver to simulate an error
    magnet_z = sim_station.magnet_z
    magnet_z._driver._simulate_error = True
    
    # 1st error -> should return stale data with _stale: True
    state = sim_station.get_state()
    assert state["magnet_z"]["_stale"] is True
    assert "_disconnected" not in state["magnet_z"]
    assert sim_station._error_counts["magnet_z"] == 1
    
    # 2nd error
    sim_station.get_state()
    assert sim_station._error_counts["magnet_z"] == 2
    
    # 3rd error -> should now also have _disconnected: True
    state = sim_station.get_state()
    assert state["magnet_z"]["_stale"] is True
    assert state["magnet_z"].get("_disconnected") is True
    assert sim_station._error_counts["magnet_z"] == 3


def test_process_system_targets_forwards_persistent_key(sim_station: Station):
    """An optional 'persistent' key in a target dict is forwarded to start_ramp().

    sim_cryostat's magnet_z is a plain SuperconductingMagnetVI, which accepts
    persistent= as a no-op — this must not raise, so any procedure can include
    'persistent' in a magnet target regardless of which magnet VI flavor a
    config wires up.
    """
    sim_station.process_system_targets({"magnet_z": Target(1.0, persistent=False)})
    assert sim_station.magnet_z.ramp_status() == "RAMPING"


def test_process_system_targets_dispatch(sim_station: Station):
    """process_system_targets dispatches to correct VIs only."""
    targets = {
        "magnet_z": Target(1.0),
        "temperature_vti": Target(150.0)
    }

    sim_station.process_system_targets(targets)

    # Verify that the ramps have started
    assert sim_station.magnet_z.ramp_status() == "RAMPING"
    assert sim_station.temperature_vti.ramp_status() == "RAMPING"

    # Verify that un-targeted system VIs are NOT ramping
    assert sim_station.magnet_y.ramp_status() == "IDLE"
    assert sim_station.temperature_sample.ramp_status() == "IDLE"

    # process_system_targets should raise if we pass a non-system VI
    with pytest.raises(ValueError):
        sim_station.process_system_targets({"level_meter": Target(10.0)})


def test_check_ramps(sim_station: Station):
    """check_ramps() returns False while ramping, True after done."""
    # Ensure initially True (all are IDLE)
    assert sim_station.check_ramps() is True
    
    # Start a ramp
    sim_station.process_system_targets({"magnet_z": Target(1.0)})

    # While ramping, should return False
    assert sim_station.check_ramps() is False
    
    # Force the ramp to complete
    # For magnet_z, it uses a generator and advances the actual value. We can force the target.
    # We will simulate enough ticks until the magnet reaches the setpoint.
    # The sim driver has a ramp_rate (5.0 A/min = 0.083 A/s).
    # Target 1.0 T = 10 A. By setting the driver's current to the target, we make it reach HOLD immediately.
    magnet_driver = sim_station.magnet_z._driver
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

    sim_station.magnet_z._driver._simulate_quench = True
    state = sim_station.get_state()
    safety = sim_station.check_safety(state)
    assert safety["quench"] is True


# ---------------------------------------------------------------------------
# Runtime fault registry (docs/plans/operation-concurrency-and-error-scoping.md §3)
# ---------------------------------------------------------------------------

def test_fault_recorded_on_stale_then_disconnected(sim_station: Station):
    """A comm-error streak records a FaultRecord: 'stale' then 'disconnected'."""
    sim_station.get_state()
    assert sim_station.vi_faults() == {}

    sim_station.magnet_z._driver._simulate_error = True
    sim_station.get_state()
    faults = sim_station.vi_faults()
    assert faults["magnet_z"].kind == "stale"
    assert faults["magnet_z"].acknowledged is False
    since_first = faults["magnet_z"].since

    sim_station.get_state()
    sim_station.get_state()  # 3rd consecutive error -> disconnected
    faults = sim_station.vi_faults()
    assert faults["magnet_z"].kind == "disconnected"
    # Escalating the SAME incident preserves 'since'.
    assert faults["magnet_z"].since == since_first


def test_fault_auto_clears_on_successful_poll(sim_station: Station):
    """A successful poll removes the fault record entirely — ack or not."""
    sim_station.magnet_z._driver._simulate_error = True
    sim_station.get_state()
    assert "magnet_z" in sim_station.vi_faults()
    sim_station.acknowledge_fault("magnet_z")
    assert sim_station.vi_faults()["magnet_z"].acknowledged is True

    sim_station.magnet_z._driver._simulate_error = False
    sim_station.get_state()
    assert "magnet_z" not in sim_station.vi_faults()


def test_acknowledge_fault(sim_station: Station):
    """acknowledge_fault() flags an existing record; no-ops on a healthy VI."""
    assert sim_station.acknowledge_fault("magnet_z") is False

    sim_station.magnet_z._driver._simulate_error = True
    sim_station.get_state()
    assert sim_station.acknowledge_fault("magnet_z") is True
    assert sim_station.vi_faults()["magnet_z"].acknowledged is True


def test_retry_fault_both_outcomes(sim_station: Station):
    """retry_fault() resets the counter and forces one poll, verdict both ways."""
    sim_station.magnet_z._driver._simulate_error = True
    sim_station.get_state()
    sim_station.get_state()
    sim_station.get_state()
    assert sim_station.vi_faults()["magnet_z"].kind == "disconnected"

    # Still broken: retry fails, fault stands (downgraded to 'stale' — the
    # counter was reset and only failed once).
    ok, message = sim_station.retry_fault("magnet_z")
    assert ok is False
    assert "magnet_z" in message
    assert sim_station.vi_faults()["magnet_z"].kind == "stale"

    # Recovers: retry succeeds and clears the fault.
    sim_station.magnet_z._driver._simulate_error = False
    ok, message = sim_station.retry_fault("magnet_z")
    assert ok is True
    assert "magnet_z" not in sim_station.vi_faults()

    # Unknown VI: explicit refusal, not a KeyError.
    ok, message = sim_station.retry_fault("no_such_vi")
    assert ok is False


def test_level_meter_disconnect_still_force_trips_helium_low_with_fault(sim_station: Station):
    """A disconnected level meter still force-trips helium_low AND records a fault.

    Guards the interplay called out in plan §3: the runtime fault registry
    must not have displaced check_safety()'s existing disconnected-level-
    meter safety guard.
    """
    level_driver = sim_station.level_meter._driver
    level_driver._simulate_error = True
    state = sim_station.get_state()
    state = sim_station.get_state()
    state = sim_station.get_state()

    assert sim_station.vi_faults()["level_meter"].kind == "disconnected"
    safety = sim_station.check_safety(state)
    assert safety["helium_low"] is True
    sources = sim_station.safety_flag_sources(state)
    assert "level_meter" in sources.get("helium_low", [])



def test_scanner_enabled_defaults_false(sim_station: Station):
    """A freshly built Station has the scanner disabled by default."""
    assert sim_station.scanner_enabled() is False


def test_scanner_enabled_round_trip(sim_station: Station):
    """set_scanner_enabled() is reflected by scanner_enabled()."""
    sim_station.set_scanner_enabled(True)
    assert sim_station.scanner_enabled() is True

    sim_station.set_scanner_enabled(False)
    assert sim_station.scanner_enabled() is False


# ---------------------------------------------------------------------------
# Degraded build: offline instruments and reconnection
# ---------------------------------------------------------------------------


class _UnreachableDriver:
    """Test double for a driver whose instrument never answers."""

    def __init__(self, resource_string: str) -> None:
        from cryosoft.core.exceptions import CryoSoftCommunicationError

        raise CryoSoftCommunicationError(
            f"Cannot open instrument at {resource_string}"
        )


class _FlakyDriver:
    """Test double that fails construction ``fail_times`` times, then succeeds.

    Models "the user plugged the cable back in between startup and retry".
    Class-level counter so build_station's import-by-dotted-path sees the
    same state as the test; reset it in each test that uses this class.
    """

    fail_times: int = 0
    attempts: int = 0

    def __init__(self, resource_string: str) -> None:
        from cryosoft.core.exceptions import CryoSoftCommunicationError

        type(self).attempts += 1
        if type(self).attempts <= type(self).fail_times:
            raise CryoSoftCommunicationError(
                f"Cannot open instrument at {resource_string}"
            )


class _StubVI:
    """Minimal VI test double satisfying the build contract."""

    def __init__(self, drivers: dict, **init_params) -> None:
        self._drivers = drivers
        self._init_params = init_params


class _CommFailVI(_StubVI):
    """VI test double whose own bring-up cannot talk to the hardware."""

    def __init__(self, drivers: dict, **init_params) -> None:
        from cryosoft.core.exceptions import CryoSoftCommunicationError

        raise CryoSoftCommunicationError("VI bring-up query got no response")


def _write_degraded_config(
    tmp_path: Path, driver_class: str, vi_class: str = "tests.test_l2_station._StubVI"
) -> str:
    """Write a two-driver / two-VI config: one healthy pair, one under test."""
    (tmp_path / "devices.yaml").write_text(
        "real_drivers:\n"
        "  good_drv:\n"
        "    class: tests.test_l2_station._AddressCapturingDriver\n"
        '    address: "GPIB0::10::INSTR"\n'
        "  bad_drv:\n"
        f"    class: {driver_class}\n"
        '    address: "GPIB0::12::INSTR"\n'
        "virtual_instruments:\n"
        "  good_vi:\n"
        "    class: tests.test_l2_station._StubVI\n"
        "    drivers: {main: good_drv}\n"
        "    vi_type: system\n"
        "  bad_vi:\n"
        f"    class: {vi_class}\n"
        "    drivers: {main: bad_drv}\n"
        "    vi_type: measurement\n"
    )
    (tmp_path / "monitor.yaml").write_text(
        "monitor:\n  tick_interval_ms: 1000\n  max_vi_errors: 3\n"
    )
    return str(tmp_path)


def test_build_station_degrades_on_unreachable_driver(tmp_path):
    """One unreachable instrument must not abort the build: it goes offline."""
    station = build_station(
        _write_degraded_config(tmp_path, "tests.test_l2_station._UnreachableDriver")
    )

    assert station.get_vi_names() == ["good_vi"]
    assert station.offline_vi_names() == ["bad_vi"]
    info = station.get_offline_info("bad_vi")
    assert info.vi_type == "measurement"
    assert "bad_drv" in info.reason
    assert "GPIB0::12::INSTR" in info.reason
    assert info.failed_drivers == ("bad_drv",)
    # Offline VIs are invisible to the live enumerators.
    assert station.has_vi("bad_vi") is False
    assert station.measurement_vi_names() == []


def test_build_station_degrades_on_vi_communication_error(tmp_path):
    """A VI whose own bring-up raises a communication error goes offline too."""
    station = build_station(
        _write_degraded_config(
            tmp_path,
            "tests.test_l2_station._AddressCapturingDriver",
            vi_class="tests.test_l2_station._CommFailVI",
        )
    )

    assert station.offline_vi_names() == ["bad_vi"]
    info = station.get_offline_info("bad_vi")
    assert "no response" in info.reason
    assert info.failed_drivers == ()


def test_build_station_still_raises_on_unknown_driver_reference(tmp_path):
    """Config errors must still abort the build (they are not connection faults)."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    (tmp_path / "devices.yaml").write_text(
        "real_drivers: {}\n"
        "virtual_instruments:\n"
        "  broken_vi:\n"
        "    class: tests.test_l2_station._StubVI\n"
        "    drivers: {main: no_such_driver}\n"
    )
    (tmp_path / "monitor.yaml").write_text("monitor:\n  tick_interval_ms: 1000\n")

    with pytest.raises(CryoSoftConfigError, match="no_such_driver"):
        build_station(str(tmp_path))


def test_fallback_keeps_config_with_unreachable_instrument(tmp_path):
    """An unreachable instrument must NOT trigger the config fallback chain."""
    from cryosoft.core.station import build_station_with_fallback

    real_cfg = tmp_path / "real"
    real_cfg.mkdir()
    _write_degraded_config(real_cfg, "tests.test_l2_station._UnreachableDriver")
    sim_cfg = str(
        Path(__file__).parent.parent / "cryosoft" / "configs" / "sim_cryostat"
    )

    station, used_path, warnings = build_station_with_fallback(
        [str(real_cfg), sim_cfg]
    )

    assert used_path == str(real_cfg)
    assert warnings == []
    assert station.offline_vi_names() == ["bad_vi"]


def test_retry_instrument_reconnects_after_transient_failure(tmp_path):
    """retry_instrument() brings a VI live once its driver becomes reachable."""
    _FlakyDriver.fail_times = 1
    _FlakyDriver.attempts = 0
    station = build_station(
        _write_degraded_config(tmp_path, "tests.test_l2_station._FlakyDriver")
    )
    assert station.offline_vi_names() == ["bad_vi"]

    ok, message = station.retry_instrument("bad_vi")

    assert ok is True
    assert "bad_vi" in message
    assert station.offline_vi_names() == []
    assert station.has_vi("bad_vi") is True
    assert station.measurement_vi_names() == ["bad_vi"]


def test_retry_instrument_failure_updates_reason_and_stays_offline(tmp_path):
    """A failed retry keeps the VI offline and refreshes the failure reason."""
    station = build_station(
        _write_degraded_config(tmp_path, "tests.test_l2_station._UnreachableDriver")
    )

    ok, message = station.retry_instrument("bad_vi")

    assert ok is False
    assert "bad_drv" in message
    assert station.offline_vi_names() == ["bad_vi"]
    assert station.has_vi("bad_vi") is False
    assert "bad_drv" in station.get_offline_info("bad_vi").reason


def test_retry_instrument_rejects_non_offline_name(sim_station: Station):
    """Retrying a live or unknown VI returns an explicit failure verdict."""
    ok, message = sim_station.retry_instrument("magnet_z")
    assert ok is False
    assert "not offline" in message

    ok, message = sim_station.retry_instrument("no_such_vi")
    assert ok is False
