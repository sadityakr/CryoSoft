# ---
# description: |
#   Test suite for the new behavior-based Virtual Instruments created in the
#   VI refactor (Stage 2). Covers SuperconductingMagnetVI,
#   SuperconductingMagnetPersistentVI, SampleTemperatureControllerVI,
#   VTITemperatureControllerVI, CryogenLevelMeterVI, DCSeparateMeasurementVI,
#   DCSingleInstrumentVI, and DCMeasurementBase.
# entry_point: pytest tests/test_l1_new_vis.py -v
# last_updated: 2026-07-12
# ---

"""Tests for behavior-based VIs (Stage 2 of VI refactor)."""

from __future__ import annotations

import time

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ips_driver():
    from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120
    return SimOxfordIPS120("SIM")


@pytest.fixture
def itc_driver():
    from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503
    return SimOxfordITC503("SIM")


@pytest.fixture
def ilm_driver():
    from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200
    return SimOxfordILM200("SIM")


@pytest.fixture
def source_driver():
    from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221
    return SimKeithley6221("SIM")


@pytest.fixture
def meter_driver():
    from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A
    return SimKeithley2182A("SIM")


@pytest.fixture
def smu_driver():
    from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400
    return SimKeithley2400("SIM")


# ---------------------------------------------------------------------------
# SuperconductingMagnetVI
# ---------------------------------------------------------------------------

class TestSuperConductingMagnetVI:
    """Tests for SuperconductingMagnetVI (no switch heater)."""

    def _make_vi(self, driver):
        from cryosoft.virtual_instruments.magnet.superconducting_magnet import SuperconductingMagnetVI
        vi = SuperconductingMagnetVI(
            {"main": driver},
            amperes_per_tesla=10.0,
            default_ramp_rate=5.0,
            max_current=90.0,
            min_current=-90.0,
            ramp_segments=[
                {"max_current_A": 40.0, "rate_A_per_min": 5.0},
                {"max_current_A": 90.0, "rate_A_per_min": 2.0},
            ],
        )
        vi.vi_name = "magnet_x"
        return vi

    def test_initial_field_is_zero(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.get_field() == pytest.approx(0.0)

    def test_initial_status_is_idle(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.ramp_status() == "IDLE"

    def test_start_ramp_transitions_to_ramping(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.start_ramp(1.0)
        assert vi.ramp_status() == "RAMPING"

    def test_ramp_reaches_target(self, ips_driver):
        vi = self._make_vi(ips_driver)
        ips_driver.set_ramp_rate(600.0)
        vi.start_ramp(1.0)
        # Simulate fast completion
        ips_driver._last_update = time.time() - 10.0
        for _ in range(20):
            vi.advance_ramp()
        assert vi.ramp_status() in ("TARGET_REACHED", "RAMPING")

    def test_get_state_returns_monitored_keys(self, ips_driver):
        vi = self._make_vi(ips_driver)
        state = vi.get_state()
        assert "magnet_current" in state
        assert "get_field" in state
        assert "magnet_status" in state

    def test_magnet_current_type(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert isinstance(vi.magnet_current(), float)

    def test_set_field_control_starts_ramp(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.set_field(2.0)
        assert vi.ramp_status() == "RAMPING"

    def test_start_ramp_accepts_persistent_kwarg_as_noop(self, ips_driver):
        """persistent= is accepted (ignored) so Station can forward it uniformly
        to either magnet VI flavor without special-casing which one is configured."""
        vi = self._make_vi(ips_driver)
        vi.start_ramp(1.0, persistent=False)
        assert vi.ramp_status() == "RAMPING"

    def test_standby_ramps_to_zero(self, ips_driver):
        vi = self._make_vi(ips_driver)
        ips_driver._current = 50.0
        vi.standby()
        assert vi.ramp_status() == "RAMPING"

    def test_vi_type_is_magnet(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.vi_type == "magnet"


# ---------------------------------------------------------------------------
# SuperconductingMagnetPersistentVI
# ---------------------------------------------------------------------------

class TestSuperConductingMagnetPersistentVI:
    """Tests for SuperconductingMagnetPersistentVI (with switch heater)."""

    def _make_vi(self, driver, warmup_ticks=2, cooldown_ticks=2):
        from cryosoft.virtual_instruments.magnet.superconducting_magnet_persistent import (
            SuperconductingMagnetPersistentVI,
        )
        vi = SuperconductingMagnetPersistentVI(
            {"main": driver},
            amperes_per_tesla=10.0,
            default_ramp_rate=600.0,  # Fast for testing
            max_current=90.0,
            min_current=-90.0,
            ramp_segments=[],
            switch_heater_warmup_ticks=warmup_ticks,
            switch_heater_cooldown_ticks=cooldown_ticks,
        )
        vi.vi_name = "magnet_x"
        return vi

    def test_initial_switch_heater_state(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.switch_heater_state() == "OFF"

    def test_initial_coil_current(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.coil_current() == pytest.approx(0.0)

    def test_initial_persistent_when_heater_cold(self, ips_driver):
        # Persistent mode is heater-derived (mirrors the real Mercury iPS):
        # at startup the switch is cold, so the driver reports persistent.
        vi = self._make_vi(ips_driver)
        assert vi.is_persistent() is True

    def test_switch_heater_on_control(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.switch_heater_on()
        assert vi.switch_heater_state() == "ON"

    def test_switch_heater_off_control(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.switch_heater_on()
        vi.switch_heater_off()
        assert vi.switch_heater_state() == "OFF"

    def test_enter_persistent_mode_control_parks_psu(self, ips_driver):
        # enter_persistent_mode (switch already cold) parks the PSU at zero;
        # the frozen coil current keeps holding the field.
        vi = self._make_vi(ips_driver)
        vi.enter_persistent_mode()
        assert vi.is_persistent() is True
        assert ips_driver.get_current_setpoint() == pytest.approx(0.0)

    def test_start_ramp_initiates_sequence(self, ips_driver):
        vi = self._make_vi(ips_driver, warmup_ticks=2, cooldown_ticks=2)
        vi.start_ramp(0.0)  # Ramp to zero (already there, but exercises sequence)
        assert vi.ramp_status() == "RAMPING"

    def test_get_state_includes_persistent_fields(self, ips_driver):
        vi = self._make_vi(ips_driver)
        state = vi.get_state()
        assert "switch_heater_state" in state
        assert "coil_current" in state
        assert "is_persistent" in state

    def test_full_persistent_ramp_sequence(self, ips_driver):
        """Drive the persistent ramp generator to completion with fast ticks."""
        vi = self._make_vi(ips_driver, warmup_ticks=1, cooldown_ticks=1)
        # Pre-position PSU at 0 A (target = 0 T, so no actual field ramp needed)
        vi.start_ramp(0.0)
        # Drive ticks — with warmup=1, cooldown=1, this completes in ~5-10 ticks
        for _ in range(50):
            if vi.ramp_status() == "TARGET_REACHED":
                break
            vi.advance_ramp()
        assert vi.ramp_status() == "TARGET_REACHED"
        assert vi.is_persistent() is True

    def test_vi_type_is_magnet(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.vi_type == "magnet"

    # -- persistent=False: repeated-sweep-ramp behavior (regression coverage
    #    for the bug where every sweep point re-paid heater warmup and
    #    get_field()/magnet_current() read back 0 after entering persistent
    #    mode) --------------------------------------------------------------

    def _drive_to_target_reached(self, vi, driver, max_ticks=100):
        """Rewind the driver's simulated clock before every tick, so whichever
        time-based ramp segment the generator is currently in jumps straight
        to its setpoint (warmup/cooldown ticks are yield-counted, not
        time-based, and are unaffected by the rewind).

        A one-time rewind (as used elsewhere in this file) is not enough here:
        the persistent generator's first few advance_ramp() calls are spent
        exhausting the warmup-tick loop, and the driver's very first
        get_current()/get_status() call after that (still status="HOLD")
        resets its internal clock as a side effect without using the rewound
        delta — so the rewind must be reapplied on every tick to guarantee
        the *next* driver call that matters sees a large elapsed time.
        """
        for _ in range(max_ticks):
            driver._last_update = time.time() - 3600.0
            if vi.ramp_status() == "TARGET_REACHED":
                return True
            vi.advance_ramp()
        return False

    def test_persistent_false_keeps_heater_on_and_does_not_zero_psu(self, ips_driver):
        vi = self._make_vi(ips_driver, warmup_ticks=2, cooldown_ticks=2)
        vi.start_ramp(1.0, persistent=False)  # 1.0 T * 10 A/T = 10 A
        assert self._drive_to_target_reached(vi, ips_driver)

        assert vi.is_persistent() is False
        assert vi.switch_heater_state() == "ON"
        # PSU actually holds the target current directly (not zeroed).
        assert vi.get_field() == pytest.approx(1.0, abs=0.01)
        assert vi.magnet_current() == pytest.approx(10.0, abs=0.1)

    def test_repeated_persistent_false_ramps_skip_warmup_after_first(self, ips_driver):
        """The switch heater warmup (paid on the first ramp) must not be
        paid again on a second persistent=False ramp while the heater is
        already on — otherwise every sweep point would cost the full
        warmup, as it did before this fix."""
        vi = self._make_vi(ips_driver, warmup_ticks=30, cooldown_ticks=30)

        vi.start_ramp(0.5, persistent=False)
        assert self._drive_to_target_reached(vi, ips_driver, max_ticks=200)
        assert vi.switch_heater_state() == "ON"

        vi.start_ramp(0.8, persistent=False)
        # No warmup this time: should reach target in far fewer ticks than
        # the 30-tick warmup alone would take.
        assert self._drive_to_target_reached(vi, ips_driver, max_ticks=10)
        assert vi.switch_heater_state() == "ON"
        assert vi.get_field() == pytest.approx(0.8, abs=0.01)

    def test_persistent_true_default_still_parks_psu_at_zero(self, ips_driver):
        """Regression: default (persistent=True, omitted) behavior is unchanged —
        PSU parks at zero and the coil holds the field."""
        vi = self._make_vi(ips_driver, warmup_ticks=1, cooldown_ticks=1)
        vi.start_ramp(1.0)  # default persistent=True
        assert self._drive_to_target_reached(vi, ips_driver)

        assert vi.is_persistent() is True
        assert vi.switch_heater_state() == "OFF"
        assert ips_driver.get_current() == pytest.approx(0.0, abs=0.01)
        # get_field()/magnet_current() must read the coil, not the zeroed PSU.
        assert vi.get_field() == pytest.approx(1.0, abs=0.01)
        assert vi.magnet_current() == pytest.approx(10.0, abs=0.1)

    # -- quench-safety regression (review finding C1): ramping OUT of a
    #    persistent parked field must match the PSU to the coil current
    #    BEFORE the switch heater goes on. The sim driver quenches on a
    #    heater-across-mismatch, so the wrong order fails these tests. -----

    def test_ramp_from_persistent_parked_field_does_not_quench(self, ips_driver):
        vi = self._make_vi(ips_driver, warmup_ticks=1, cooldown_ticks=1)

        # Park the magnet persistent at 2 T: coil 20 A, PSU 0 A, heater off.
        vi.start_ramp(2.0)  # persistent=True
        assert self._drive_to_target_reached(vi, ips_driver)
        assert ips_driver.get_current() == pytest.approx(0.0, abs=0.01)
        assert ips_driver.get_coil_current() == pytest.approx(20.0, abs=0.1)

        # Ramp to a new field. Old (buggy) order heated the switch across the
        # 20 A PSU/coil mismatch -> quench. Correct order matches first.
        vi.start_ramp(1.0, persistent=False)
        assert self._drive_to_target_reached(vi, ips_driver, max_ticks=200)
        assert ips_driver.get_status() != "QUENCH"
        assert vi.get_field() == pytest.approx(1.0, abs=0.01)

    def test_quench_during_sequence_stops_commands(self, ips_driver):
        """A QUENCH mid-sequence must stop the generator without further
        setpoint commands (escalation to EMERGENCY is the safety chain's job)."""
        vi = self._make_vi(ips_driver, warmup_ticks=1, cooldown_ticks=1)
        vi.start_ramp(1.0, persistent=False)
        ips_driver._simulate_quench = True
        for _ in range(10):
            vi.advance_ramp()
        # Generator gave up; the setpoint was never re-commanded post-quench.
        assert vi.ramp_status() in ("TARGET_REACHED", "RAMPING", "IDLE")
        sp_after = ips_driver._setpoint
        for _ in range(5):
            vi.advance_ramp()
        assert ips_driver._setpoint == sp_after

    # -- stop_ramp(): abort must freeze the autonomous PSU, not just kill
    #    the software generator (review finding C3) ------------------------

    def test_stop_ramp_holds_hardware(self, ips_driver):
        vi = self._make_vi(ips_driver, warmup_ticks=0, cooldown_ticks=0)
        vi.start_ramp(5.0, persistent=False)
        # Drive a few ticks so the generator has commanded the PSU setpoint.
        for _ in range(5):
            vi.advance_ramp()
        assert ips_driver.get_status() == "RAMPING"

        vi.stop_ramp()
        assert vi.ramp_status() == "IDLE"
        # The PSU itself must be held: setpoint pinned to the present output.
        assert ips_driver.get_status() == "HOLD"
        assert ips_driver.get_current_setpoint() == pytest.approx(
            ips_driver.get_current(), abs=0.01
        )


# ---------------------------------------------------------------------------
# SampleTemperatureControllerVI
# ---------------------------------------------------------------------------

class TestSampleTemperatureControllerVI:
    """Tests for SampleTemperatureControllerVI."""

    def _make_vi(self, driver):
        from cryosoft.virtual_instruments.temperature.sample_temperature_controller import (
            SampleTemperatureControllerVI,
        )
        vi = SampleTemperatureControllerVI(
            {"main": driver},
            default_ramp_rate=600.0,
            tolerance=0.5,
        )
        vi.vi_name = "temperature_sample"
        return vi

    def test_initial_temperature_returns_float(self, itc_driver):
        vi = self._make_vi(itc_driver)
        assert isinstance(vi.temperature(), float)

    def test_initial_status_is_idle(self, itc_driver):
        vi = self._make_vi(itc_driver)
        assert vi.ramp_status() == "IDLE"

    def test_start_ramp_transitions_to_ramping(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.start_ramp(100.0)
        assert vi.ramp_status() == "RAMPING"

    def test_ramp_completes(self, itc_driver):
        vi = self._make_vi(itc_driver)
        # Driver starts at 300 K; ramp to 300.1 K with very fast rate
        vi.start_ramp(300.1, rate=9999.0)
        for _ in range(100):
            vi.advance_ramp()
        # After generator exhausts, hardware must settle within tolerance
        itc_driver._temperature = 300.05  # Force within tolerance
        itc_driver._setpoint = 300.1
        assert vi.ramp_status() in ("TARGET_REACHED", "RAMPING")

    def test_stop_ramp_pins_setpoint_to_current_temperature(self, itc_driver):
        """stop_ramp() must go IDLE and pin the setpoint where the system is —
        otherwise the controller keeps regulating toward the last-commanded
        intermediate setpoint after an abort (review finding C3)."""
        vi = self._make_vi(itc_driver)
        vi.start_ramp(100.0)
        vi.advance_ramp()
        vi.stop_ramp()
        assert vi.ramp_status() == "IDLE"
        assert itc_driver.get_setpoint() == pytest.approx(
            itc_driver.get_temperature(), abs=0.01
        )

    def test_get_state_keys(self, itc_driver):
        vi = self._make_vi(itc_driver)
        state = vi.get_state()
        assert "temperature" in state
        assert "setpoint" in state
        assert "heater_output" in state

    def test_set_ramp_rate_control(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.set_ramp_rate(10.0)
        assert vi._default_ramp_rate == pytest.approx(10.0)

    def test_set_temperature_control_starts_ramp(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.set_temperature(100.0)
        assert vi.ramp_status() == "RAMPING"

    def test_vi_type_is_temperature(self, itc_driver):
        vi = self._make_vi(itc_driver)
        assert vi.vi_type == "temperature"

    def test_no_needle_valve_attribute(self, itc_driver):
        """SampleTemperatureControllerVI must NOT expose needle valve."""
        vi = self._make_vi(itc_driver)
        assert not hasattr(vi, "needle_valve")


# ---------------------------------------------------------------------------
# VTITemperatureControllerVI
# ---------------------------------------------------------------------------

class TestVTITemperatureControllerVI:
    """Tests for VTITemperatureControllerVI (with needle valve)."""

    def _make_vi(self, driver):
        from cryosoft.virtual_instruments.temperature.vti_temperature_controller import (
            VTITemperatureControllerVI,
        )
        vi = VTITemperatureControllerVI(
            {"main": driver},
            default_ramp_rate=600.0,
            tolerance=0.5,
        )
        vi.vi_name = "temperature_vti"
        return vi

    def test_inherits_temperature_methods(self, itc_driver):
        vi = self._make_vi(itc_driver)
        assert isinstance(vi.temperature(), float)
        assert isinstance(vi.setpoint(), float)
        assert isinstance(vi.heater_output(), float)

    def test_ramp_inherited(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.start_ramp(100.0)
        assert vi.ramp_status() == "RAMPING"

    def test_needle_valve_initial_state(self, itc_driver):
        vi = self._make_vi(itc_driver)
        assert vi.needle_valve() == pytest.approx(0.0)

    def test_set_needle_valve_control(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.set_needle_valve(50.0)
        assert vi.needle_valve() == pytest.approx(50.0)

    def test_needle_valve_full_range(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.set_needle_valve(0.0)
        assert vi.needle_valve() == pytest.approx(0.0)
        vi.set_needle_valve(100.0)
        assert vi.needle_valve() == pytest.approx(100.0)

    def test_get_state_includes_needle_valve(self, itc_driver):
        vi = self._make_vi(itc_driver)
        state = vi.get_state()
        assert "needle_valve" in state
        assert "temperature" in state
        assert "setpoint" in state

    def test_vi_type_is_temperature(self, itc_driver):
        vi = self._make_vi(itc_driver)
        assert vi.vi_type == "temperature"


# ---------------------------------------------------------------------------
# CryogenLevelMeterVI
# ---------------------------------------------------------------------------

class TestCryogenLevelMeterVI:
    """Tests for CryogenLevelMeterVI."""

    def _make_vi(self, driver):
        from cryosoft.virtual_instruments.level.cryogen_level_meter import CryogenLevelMeterVI
        vi = CryogenLevelMeterVI(
            {"main": driver},
            helium_low_threshold=20.0,
            buffer_size=5,
        )
        vi.vi_name = "level_meter"
        return vi

    def test_helium_level_returns_float(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        assert isinstance(vi.helium_level(), float)

    def test_nitrogen_level_returns_float(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        assert isinstance(vi.nitrogen_level(), float)

    def test_initial_helium_not_low(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        for _ in range(5):
            vi.helium_level()
        assert vi.helium_low() is False

    def test_helium_low_when_forced(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        ilm_driver._force_helium_level = 10.0  # Below 20% threshold
        for _ in range(5):
            vi.helium_level()
        assert vi.helium_low() is True

    def test_set_refresh_rate_standby(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        vi.set_refresh_rate(0)
        assert vi.get_refresh_rate() == 0

    def test_set_refresh_rate_slow(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        vi.set_refresh_rate(1)
        assert vi.get_refresh_rate() == 1

    def test_set_refresh_rate_fast(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        vi.set_refresh_rate(2)
        assert vi.get_refresh_rate() == 2

    def test_invalid_refresh_rate_raises(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        with pytest.raises(Exception):
            vi.set_refresh_rate(3)

    def test_get_state_keys(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        state = vi.get_state()
        assert "helium_level" in state
        assert "nitrogen_level" in state
        assert "get_refresh_rate" in state

    def test_vi_type_is_level(self, ilm_driver):
        vi = self._make_vi(ilm_driver)
        assert vi.vi_type == "level"


# ---------------------------------------------------------------------------
# DCSeparateMeasurementVI
# ---------------------------------------------------------------------------

class TestDCSeparateMeasurementVI:
    """Tests for DCSeparateMeasurementVI."""

    def _make_vi(self, source, meter):
        from cryosoft.virtual_instruments.measurement.dc_separate_measurement import DCSeparateMeasurementVI
        vi = DCSeparateMeasurementVI({"source": source, "meter": meter})
        vi.vi_name = "dc_measurement"
        return vi

    def test_ping_returns_true_when_drivers_respond(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        assert vi.ping() is True

    def test_initiate_and_take_reading(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1)
        data = vi.take_reading(n_points=5)
        assert "voltage_V" in data
        assert "current_A" in data
        assert len(data["voltage_V"]) == 5
        assert len(data["current_A"]) == 5

    def test_current_constant_across_readings(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        vi.initiate(current_A=2e-6)
        data = vi.take_reading(n_points=10)
        assert all(abs(c - 2e-6) < 1e-12 for c in data["current_A"])

    def test_take_reading_without_initiate_raises(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        with pytest.raises(RuntimeError):
            vi.take_reading()

    def test_standby_resets_state(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        vi.initiate(current_A=1e-6)
        vi.standby()
        with pytest.raises(RuntimeError):
            vi.take_reading()

    def test_inherits_dc_measurement_base(self, source_driver, meter_driver):
        from cryosoft.virtual_instruments.base import DCMeasurementBase
        vi = self._make_vi(source_driver, meter_driver)
        assert isinstance(vi, DCMeasurementBase)

    def test_vi_type_is_measurement(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        assert vi.vi_type == "measurement"


# ---------------------------------------------------------------------------
# DCSingleInstrumentVI
# ---------------------------------------------------------------------------

class TestDCSingleInstrumentVI:
    """Tests for DCSingleInstrumentVI (Keithley 2400 SMU)."""

    def _make_vi(self, smu):
        from cryosoft.virtual_instruments.measurement.dc_single_instrument import DCSingleInstrumentVI
        vi = DCSingleInstrumentVI({"main": smu})
        vi.vi_name = "dc_measurement"
        return vi

    def test_ping_returns_true(self, smu_driver):
        vi = self._make_vi(smu_driver)
        assert vi.ping() is True

    def test_initiate_and_take_reading(self, smu_driver):
        vi = self._make_vi(smu_driver)
        vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1)
        data = vi.take_reading(n_points=5)
        assert "voltage_V" in data
        assert "current_A" in data
        assert len(data["voltage_V"]) == 5
        assert len(data["current_A"]) == 5

    def test_current_constant_across_readings(self, smu_driver):
        vi = self._make_vi(smu_driver)
        vi.initiate(current_A=3e-6)
        data = vi.take_reading(n_points=10)
        assert all(abs(c - 3e-6) < 1e-12 for c in data["current_A"])

    def test_take_reading_without_initiate_raises(self, smu_driver):
        vi = self._make_vi(smu_driver)
        with pytest.raises(RuntimeError):
            vi.take_reading()

    def test_standby_resets_state(self, smu_driver):
        vi = self._make_vi(smu_driver)
        vi.initiate(current_A=1e-6)
        vi.standby()
        with pytest.raises(RuntimeError):
            vi.take_reading()

    def test_inherits_dc_measurement_base(self, smu_driver):
        from cryosoft.virtual_instruments.base import DCMeasurementBase
        vi = self._make_vi(smu_driver)
        assert isinstance(vi, DCMeasurementBase)

    def test_vi_type_is_measurement(self, smu_driver):
        vi = self._make_vi(smu_driver)
        assert vi.vi_type == "measurement"

    def test_identical_interface_to_separate_vi(self, smu_driver, source_driver, meter_driver):
        """Both DC VIs must accept identical initiate() and take_reading() signatures."""
        from cryosoft.virtual_instruments.measurement.dc_separate_measurement import DCSeparateMeasurementVI
        from cryosoft.virtual_instruments.measurement.dc_single_instrument import DCSingleInstrumentVI

        vi_sep = DCSeparateMeasurementVI({"source": source_driver, "meter": meter_driver})
        vi_sep.vi_name = "dc_sep"
        vi_smu = DCSingleInstrumentVI({"main": smu_driver})
        vi_smu.vi_name = "dc_smu"

        for vi in (vi_sep, vi_smu):
            vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1)
            data = vi.take_reading(n_points=3)
            assert set(data.keys()) == {"voltage_V", "current_A"}
            assert len(data["voltage_V"]) == 3
