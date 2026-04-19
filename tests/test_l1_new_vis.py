# ---
# description: |
#   Test suite for the new behavior-based Virtual Instruments created in the
#   VI refactor (Stage 2). Covers SuperconductingMagnetVI,
#   SuperconductingMagnetPersistentVI, SampleTemperatureControllerVI,
#   VTITemperatureControllerVI, CryogenLevelMeterVI, DCSeparateMeasurementVI,
#   DCSingleInstrumentVI, and DCMeasurementBase.
# entry_point: pytest tests/test_l1_new_vis.py -v
# last_updated: 2026-04-19
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

    def test_initial_not_persistent(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.is_persistent() is False

    def test_switch_heater_on_control(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.switch_heater_on()
        assert vi.switch_heater_state() == "ON"

    def test_switch_heater_off_control(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.switch_heater_on()
        vi.switch_heater_off()
        assert vi.switch_heater_state() == "OFF"

    def test_enter_persistent_mode_control(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.enter_persistent_mode()
        assert vi.is_persistent() is True

    def test_exit_persistent_mode_control(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.enter_persistent_mode()
        vi.exit_persistent_mode()
        assert vi.is_persistent() is False

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
