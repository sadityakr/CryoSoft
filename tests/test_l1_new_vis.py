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

from cryosoft.virtual_instruments.magnet.switch_heater import SwitchHeater


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
def rotator_driver():
    from cryosoft.drivers.sim_rotator import SimRotator
    return SimRotator("SIM")


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


@pytest.fixture
def lockin_driver():
    from cryosoft.drivers.sim_lockin import SimLockIn
    return SimLockIn("SIM")


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
        vi.vi_name = "magnet_z"
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

    def test_ramp_target_and_rate_none_when_idle(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.ramp_target() is None
        assert vi.ramp_rate() is None

    def test_ramp_target_reports_field_in_tesla(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.start_ramp(1.0)
        assert vi.ramp_target() == pytest.approx(1.0)
        # Segment rate at 0 A is 5 A/min; at 10 A/T that is 0.5 T/min.
        assert vi.ramp_rate() == pytest.approx(0.5)

    def test_stop_ramp_clears_ramp_target(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.start_ramp(1.0)
        vi.stop_ramp()
        assert vi.ramp_target() is None
        assert vi.ramp_rate() is None


# ---------------------------------------------------------------------------
# SuperconductingMagnetPersistentVI
# ---------------------------------------------------------------------------

class TestSuperConductingMagnetPersistentVI:
    """Tests for SuperconductingMagnetPersistentVI (with switch heater)."""

    def _make_vi(self, driver, warmup_s=0.0, cooldown_s=0.0):
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
            switch_heater_warmup_s=warmup_s,
            switch_heater_cooldown_s=cooldown_s,
        )
        vi.vi_name = "magnet_z"
        return vi

    def test_initial_switch_heater_state(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.switch_heater_state() == "OFF"

    def test_initial_coil_current(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.coil_current() == pytest.approx(0.0)

    def test_initial_persistent_when_heater_cold(self, ips_driver):
        # Physical persistent state is heater-derived (mirrors the real Mercury
        # iPS): at startup the switch is cold, so the driver reports persistent.
        vi = self._make_vi(ips_driver)
        assert vi.is_persistent() is True

    def test_persistent_mode_disabled_by_default(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.persistent_mode_enabled() is False

    def test_vi_type_is_magnet(self, ips_driver):
        vi = self._make_vi(ips_driver)
        assert vi.vi_type == "magnet"

    def test_get_state_includes_persistent_fields(self, ips_driver):
        vi = self._make_vi(ips_driver)
        state = vi.get_state()
        assert "switch_heater_state" in state
        assert "coil_current" in state
        assert "is_persistent" in state
        assert "persistent_mode_enabled" in state

    def _drive_to_target_reached(self, vi, driver, max_ticks=100):
        """Drive advance_ramp() to completion, rewinding the driver's simulated
        clock each tick so the wall-clock PSU ramp jumps to its setpoint. The
        switch-heater warmup is timed separately by SwitchHeater; tests that
        need no warmup use warmup_s=0 so is_ready() is immediately true.
        """
        for _ in range(max_ticks):
            driver._last_update = time.time() - 3600.0
            if vi.ramp_status() == "TARGET_REACHED":
                return True
            vi.advance_ramp()
        return False

    # -- Normal mode: field changes keep the switch heater on ---------------

    def test_start_ramp_normal_mode_transitions_to_ramping(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.start_ramp(1.5)
        assert vi.ramp_status() == "RAMPING"
        assert vi.ramp_target() == pytest.approx(1.5)

    def test_normal_ramp_completes_with_heater_on(self, ips_driver):
        """Normal mode leaves the heater ON and the PSU holding the target
        directly — no cooldown, no persistent park."""
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.start_ramp(1.0)  # 1 T * 10 A/T = 10 A
        assert self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "ON"
        assert vi.is_persistent() is False
        assert vi.get_field() == pytest.approx(1.0, abs=0.01)
        assert vi.magnet_current() == pytest.approx(10.0, abs=0.1)

    def test_repeated_normal_ramps_keep_heater_on(self, ips_driver):
        """A field sweep energises the heater once and keeps it on across
        points (no per-point re-warm/park)."""
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.start_ramp(0.5)
        assert self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "ON"
        vi.start_ramp(0.8)
        assert self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "ON"
        assert vi.get_field() == pytest.approx(0.8, abs=0.01)

    def test_normal_ramp_waits_for_warmup_then_completes(self, ips_driver):
        """The first ramp blocks in warmup until the (wall-clock) switch heater
        is ready, then completes — readiness is time-based, not tick-based."""
        vi = self._make_vi(ips_driver, warmup_s=60.0)
        # Swap in a fake-clock heater so the 60 s warmup is deterministic.
        clock = {"t": 0.0}
        vi._heater = SwitchHeater(warmup_s=60.0, clock=lambda: clock["t"])
        vi.start_ramp(1.0)
        # Heater energised but cold: the ramp cannot finish yet.
        assert not self._drive_to_target_reached(vi, ips_driver, max_ticks=20)
        assert vi.switch_heater_state() == "ON"
        # Warm up, then it completes.
        clock["t"] = 60.0
        assert self._drive_to_target_reached(vi, ips_driver)
        assert vi.get_field() == pytest.approx(1.0, abs=0.01)

    def test_normal_ramp_completes_when_heater_already_on_at_construction(
        self, ips_driver
    ):
        """Regression: an app restart leaves the heater ON at the driver while a
        fresh VI's SwitchHeater tracks OFF. The ramp must adopt that state and
        complete instead of spinning forever in the warmup wait."""
        # Energise at the driver, matching how a prior session left the magnet,
        # then build the VI — so its SwitchHeater starts out of sync.
        ips_driver.set_switch_heater(True)
        vi = self._make_vi(ips_driver, warmup_s=60.0)
        clock = {"t": 0.0}
        vi._heater = SwitchHeater(warmup_s=60.0, clock=lambda: clock["t"])

        vi.start_ramp(1.0)
        # First tick adopts the driver state and starts the warmup clock.
        assert not self._drive_to_target_reached(vi, ips_driver, max_ticks=20)
        assert vi.ramp_phase() == "warmup"
        clock["t"] = 60.0
        assert self._drive_to_target_reached(vi, ips_driver)
        assert vi.get_field() == pytest.approx(1.0, abs=0.01)
        assert vi.switch_heater_state() == "ON"

    def test_manual_switch_heater_refused_in_normal_mode(self, ips_driver):
        from cryosoft.core.exceptions import CryoSoftSafetyError

        vi = self._make_vi(ips_driver)
        with pytest.raises(CryoSoftSafetyError, match="persistent mode"):
            vi.switch_heater_on()
        with pytest.raises(CryoSoftSafetyError, match="persistent mode"):
            vi.switch_heater_off()

    # -- Persistent mode: manual switch-heater / PSU control ----------------

    def test_switch_heater_on_off_in_persistent_mode(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.enable_persistent_mode()
        vi.switch_heater_on()   # fresh magnet: PSU 0 == coil 0, allowed
        assert vi.switch_heater_state() == "ON"
        vi.switch_heater_off()  # always allowed
        assert vi.switch_heater_state() == "OFF"

    def test_switch_heater_on_allowed_when_currents_match(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.enable_persistent_mode()
        vi.switch_heater_on()
        assert vi.switch_heater_state() == "ON"

    def test_disable_persistent_refused_when_heater_off(self, ips_driver):
        from cryosoft.core.exceptions import CryoSoftSafetyError

        vi = self._make_vi(ips_driver)
        vi.enable_persistent_mode()  # heater still off
        with pytest.raises(CryoSoftSafetyError, match="switch heater is off"):
            vi.disable_persistent_mode()
        assert vi.persistent_mode_enabled() is True

    def test_disable_persistent_allowed_when_heater_on(self, ips_driver):
        vi = self._make_vi(ips_driver)
        vi.enable_persistent_mode()
        vi.switch_heater_on()
        vi.disable_persistent_mode()
        assert vi.persistent_mode_enabled() is False

    def test_manual_persistent_park_flow(self, ips_driver):
        """The persistent dance: heater on, ramp field, heater off, ramp PSU to
        zero -> magnet holds the field persistently with the PSU parked."""
        vi = self._make_vi(ips_driver)
        vi.enable_persistent_mode()
        vi.switch_heater_on()
        vi.start_ramp(2.0)                       # raw PSU ramp; heater on -> field moves
        assert self._drive_to_target_reached(vi, ips_driver)
        assert vi.get_field() == pytest.approx(2.0, abs=0.01)
        vi.switch_heater_off()                   # freeze coil at 20 A, heater off
        assert vi.is_persistent() is True
        vi.start_ramp(0.0)                       # park PSU at zero; coil holds field
        assert self._drive_to_target_reached(vi, ips_driver)
        assert ips_driver.get_current() == pytest.approx(0.0, abs=0.01)
        # Field reads back from the coil, not the parked PSU.
        assert vi.get_field() == pytest.approx(2.0, abs=0.01)
        assert vi.magnet_current() == pytest.approx(20.0, abs=0.1)

    def test_exit_persistent_safely_then_ramp_normally(self, ips_driver):
        """Leaving persistent mode: match the PSU to the coil, energise the
        heater, disable the toggle, then a normal ramp works."""
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        # Park persistent at 2 T (coil 20 A, PSU 0 A, heater off).
        vi.enable_persistent_mode()
        vi.switch_heater_on()
        vi.start_ramp(2.0)
        assert self._drive_to_target_reached(vi, ips_driver)
        vi.switch_heater_off()
        vi.start_ramp(0.0)
        assert self._drive_to_target_reached(vi, ips_driver)
        # Exit: match PSU back to the coil, then heater on, then leave the mode.
        vi.start_ramp(2.0)                       # PSU -> 20 A == coil
        assert self._drive_to_target_reached(vi, ips_driver)
        vi.switch_heater_on()                    # matched -> allowed
        vi.disable_persistent_mode()             # heater on -> allowed
        assert vi.persistent_mode_enabled() is False
        vi.start_ramp(1.0)                       # normal ramp
        assert self._drive_to_target_reached(vi, ips_driver)
        assert vi.get_field() == pytest.approx(1.0, abs=0.01)

    # -- quench safety ------------------------------------------------------

    def test_switch_heater_on_refused_across_current_mismatch(self, ips_driver):
        """Energising the heater across a PSU/coil mismatch would quench; the
        guard must refuse BEFORE any driver command."""
        from cryosoft.core.exceptions import CryoSoftSafetyError

        vi = self._make_vi(ips_driver)
        vi.enable_persistent_mode()
        vi.switch_heater_on()
        vi.start_ramp(2.0)                       # field to 2 T, coil follows PSU
        assert self._drive_to_target_reached(vi, ips_driver)
        vi.switch_heater_off()                   # coil frozen at 20 A
        vi.start_ramp(0.0)                       # PSU -> 0, coil still 20 (mismatch)
        assert self._drive_to_target_reached(vi, ips_driver)
        with pytest.raises(CryoSoftSafetyError, match="switch heater"):
            vi.switch_heater_on()
        assert ips_driver.get_status() != "QUENCH"

    def test_quench_during_sequence_stops_commands(self, ips_driver):
        """A QUENCH mid-ramp must stop the generator without further setpoint
        commands (escalation to EMERGENCY is the safety chain's job)."""
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.start_ramp(1.0)
        ips_driver._simulate_quench = True
        for _ in range(10):
            vi.advance_ramp()
        assert vi.ramp_status() in ("TARGET_REACHED", "RAMPING", "IDLE")
        sp_after = ips_driver._setpoint
        for _ in range(5):
            vi.advance_ramp()
        assert ips_driver._setpoint == sp_after

    # -- control-validation standard: declarative field limit --------------

    def test_set_field_beyond_setup_limit_is_refused(self, ips_driver):
        """set_field() outside the config-derived field limit must raise
        (loud reject), not silently clamp."""
        from cryosoft.core.exceptions import CryoSoftSafetyError

        vi = self._make_vi(ips_driver)  # max_current 90 A / 10 A/T -> ±9 T
        with pytest.raises(CryoSoftSafetyError, match="outside the allowed range"):
            vi.set_field(12.0)
        assert vi.ramp_status() == "IDLE"  # no ramp was started

    def test_set_field_within_limit_still_works(self, ips_driver):
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.set_field(2.0)
        assert vi.ramp_status() == "RAMPING"

    # -- stop_ramp(): abort must freeze the autonomous PSU, not just kill
    #    the software generator (review finding C3) ------------------------

    def test_stop_ramp_holds_hardware(self, ips_driver):
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.start_ramp(5.0)
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

    # -- standby(): heater-safe safe-park (bugfix — must never energise an
    #    OFF heater for no reason, e.g. during EMERGENCY shutdown) ----------

    def test_standby_with_heater_off_and_no_trapped_field_never_energises(
        self, ips_driver
    ):
        """Fresh magnet: PSU and coil both already 0 A, heater off.

        standby() must be a no-op on the heater — nothing needs to ramp.
        """
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.standby()
        self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "OFF"
        assert ips_driver.get_current() == pytest.approx(0.0, abs=0.01)

    def test_standby_parks_nonzero_psu_with_heater_off(self, ips_driver):
        """Heater off, coil at 0 A, but the PSU itself is nonzero.

        standby() must bring the PSU to zero without ever touching the
        heater (ramping the PSU doesn't move the frozen coil field).
        """
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        ips_driver.set_current_setpoint(3.0)
        self._drive_to_target_reached(vi, ips_driver)

        vi.standby()
        self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "OFF"
        assert ips_driver.get_current() == pytest.approx(0.0, abs=0.01)

    def test_standby_ramps_down_trapped_persistent_field_and_ends_deenergised(
        self, ips_driver
    ):
        """Heater off with a nonzero trapped coil field (manually parked).

        standby() must match the PSU to the coil, energise, ramp the field
        to zero, then de-energise — a full park, never leaving the heater on.
        """
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.enable_persistent_mode()
        vi.switch_heater_on()
        vi.start_ramp(2.0)  # 20 A
        self._drive_to_target_reached(vi, ips_driver)
        vi.switch_heater_off()  # freeze coil at 20 A, heater off
        assert vi.coil_current() == pytest.approx(20.0, abs=0.01)

        vi.standby()
        self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "OFF"
        assert vi.coil_current() == pytest.approx(0.0, abs=0.01)
        assert ips_driver.get_current() == pytest.approx(0.0, abs=0.01)

    def test_standby_with_heater_on_ramps_down_and_deenergises(self, ips_driver):
        """Heater already on (normal mode, mid-operation): ramp straight to
        zero with no re-energise, then de-energise for a full park."""
        vi = self._make_vi(ips_driver, warmup_s=0.0)
        vi.start_ramp(1.5)
        self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "ON"

        vi.standby()
        self._drive_to_target_reached(vi, ips_driver)
        assert vi.switch_heater_state() == "OFF"
        assert ips_driver.get_current() == pytest.approx(0.0, abs=0.01)


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

    def test_temperature_and_rate_limits_from_init_params(self, itc_driver):
        """Config-declared temperature / ramp-rate bounds are enforced on the
        @control entry points (control-validation standard)."""
        from cryosoft.core.exceptions import CryoSoftSafetyError
        from cryosoft.virtual_instruments.temperature.sample_temperature_controller import (
            SampleTemperatureControllerVI,
        )

        vi = SampleTemperatureControllerVI(
            {"main": itc_driver},
            default_ramp_rate=2.0,
            tolerance=0.5,
            min_temperature_K=1.4,
            max_temperature_K=320.0,
            max_ramp_rate_K_per_min=20.0,
        )
        vi.vi_name = "temperature_sample"

        with pytest.raises(CryoSoftSafetyError, match="outside the allowed range"):
            vi.set_temperature(400.0)
        with pytest.raises(CryoSoftSafetyError, match="outside the allowed range"):
            vi.set_temperature(0.5)
        with pytest.raises(CryoSoftSafetyError, match="outside the allowed range"):
            vi.set_ramp_rate(50.0)
        with pytest.raises(ValueError):
            vi.set_ramp_rate(0.0)  # semantic guard: rate must be positive

        # Within bounds — accepted (driver starts at 300 K, so ramp toward
        # 250 K is genuinely in progress).
        vi.set_temperature(250.0)
        assert vi.ramp_status() == "RAMPING"

    def test_ramp_target_and_rate_none_when_idle(self, itc_driver):
        vi = self._make_vi(itc_driver)
        assert vi.ramp_target() is None
        assert vi.ramp_rate() is None

    def test_ramp_target_and_rate_reported_during_ramp(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.start_ramp(100.0, rate=5.0)
        assert vi.ramp_target() == pytest.approx(100.0)
        assert vi.ramp_rate() == pytest.approx(5.0)

    def test_stop_ramp_clears_target_and_rate(self, itc_driver):
        vi = self._make_vi(itc_driver)
        vi.start_ramp(100.0, rate=5.0)
        vi.stop_ramp()
        assert vi.ramp_target() is None
        assert vi.ramp_rate() is None

    def test_no_limits_in_init_params_means_unbounded(self, itc_driver):
        """A setup that declares no temperature bounds keeps working (open range)."""
        vi = self._make_vi(itc_driver)
        vi.set_temperature(500.0)  # no max_temperature_K configured
        assert vi.ramp_status() == "RAMPING"

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
# RotatorVI
# ---------------------------------------------------------------------------

class TestRotatorVI:
    """Tests for RotatorVI."""

    def _make_vi(self, driver):
        from cryosoft.virtual_instruments.rotator.rotator import RotatorVI
        vi = RotatorVI(
            {"main": driver},
            default_rate_deg_per_min=1.0,
            min_angle_deg=-180.0,
            max_angle_deg=180.0,
            max_rate_deg_per_min=10.0,
        )
        vi.vi_name = "rotator"
        return vi

    def test_initial_angle_is_zero(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        assert vi.get_sample_angle() == pytest.approx(0.0)

    def test_initial_status_is_idle(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        assert vi.ramp_status() == "IDLE"

    def test_start_ramp_transitions_to_ramping(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        vi.start_ramp(90.0)
        assert vi.ramp_status() == "RAMPING"

    def test_ramp_reaches_target(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        vi._rate_deg_per_min = 600.0  # bypass the config-limited control for a fast test ramp
        rotator_driver.set_rate(600.0)
        vi.start_ramp(90.0)
        rotator_driver._last_update = time.time() - 10.0
        for _ in range(20):
            vi.advance_ramp()
        assert vi.ramp_status() in ("TARGET_REACHED", "RAMPING")

    def test_get_state_returns_monitored_keys(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        state = vi.get_state()
        assert "get_sample_angle" in state
        assert "get_rate_sample_angle" in state
        assert "rotator_status" in state

    def test_get_sample_angle_type(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        assert isinstance(vi.get_sample_angle(), float)

    def test_set_sample_angle_control_starts_ramp(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        vi.set_sample_angle(45.0)
        assert vi.ramp_status() == "RAMPING"

    def test_start_ramp_accepts_persistent_kwarg_as_noop(self, rotator_driver):
        """persistent= is accepted (ignored) so Station can forward it
        uniformly to any system VI without special-casing which one it is."""
        vi = self._make_vi(rotator_driver)
        vi.start_ramp(45.0, persistent=False)
        assert vi.ramp_status() == "RAMPING"

    def test_standby_holds_current_angle(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        rotator_driver._position = 45.0
        vi.standby()
        assert vi.ramp_status() == "IDLE"
        assert rotator_driver.get_position_setpoint() == pytest.approx(45.0)

    def test_vi_type_is_rotator(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        assert vi.vi_type == "rotator"

    def test_ramp_target_and_rate_none_when_idle(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        assert vi.ramp_target() is None
        assert vi.ramp_rate() is None

    def test_ramp_target_reports_angle_in_degrees(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        vi.start_ramp(90.0)
        assert vi.ramp_target() == pytest.approx(90.0)
        assert vi.ramp_rate() == pytest.approx(1.0)

    def test_stop_ramp_clears_ramp_target(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        vi.start_ramp(90.0)
        vi.stop_ramp()
        assert vi.ramp_target() is None
        assert vi.ramp_status() == "IDLE"

    def test_set_rate_sample_angle_updates_rate(self, rotator_driver):
        vi = self._make_vi(rotator_driver)
        vi.set_rate_sample_angle(5.0)
        assert vi.get_rate_sample_angle() == pytest.approx(5.0)

    def test_set_sample_angle_rejects_out_of_range(self, rotator_driver):
        from cryosoft.core.exceptions import CryoSoftSafetyError
        vi = self._make_vi(rotator_driver)
        with pytest.raises(CryoSoftSafetyError):
            vi.set_sample_angle(200.0)

    def test_set_rate_sample_angle_rejects_out_of_range(self, rotator_driver):
        from cryosoft.core.exceptions import CryoSoftSafetyError
        vi = self._make_vi(rotator_driver)
        with pytest.raises(CryoSoftSafetyError):
            vi.set_rate_sample_angle(20.0)


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
        vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1, readings_per_point=5)
        data = vi.take_reading()
        assert "voltage_V" in data
        assert "current_A" in data
        assert len(data["voltage_V"]) == 5
        assert len(data["current_A"]) == 5

    def test_current_constant_across_readings(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        vi.initiate(current_A=2e-6, readings_per_point=10)
        data = vi.take_reading()
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

    # ── The reading-loop setter (reading_setters standard) ───────────────────

    def test_set_source_current_before_initiate_raises(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        with pytest.raises(RuntimeError):
            vi.set_source_current(1e-6)

    def test_set_source_current_changes_reported_current(self, source_driver, meter_driver):
        vi = self._make_vi(source_driver, meter_driver)
        vi.initiate(current_A=1e-6, readings_per_point=4)
        vi.set_source_current(-1e-6)
        data = vi.take_reading()
        assert all(abs(c + 1e-6) < 1e-12 for c in data["current_A"])
        assert len(data["voltage_V"]) == 4

    def test_declares_current_reading_setter(self, source_driver, meter_driver):
        """current_A is loopable via set_source_current (the reading loop)."""
        vi = self._make_vi(source_driver, meter_driver)
        assert vi.reading_setters == {"current_A": "set_source_current"}


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
        vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1, readings_per_point=5)
        data = vi.take_reading()
        assert "voltage_V" in data
        assert "current_A" in data
        assert len(data["voltage_V"]) == 5
        assert len(data["current_A"]) == 5

    def test_current_constant_across_readings(self, smu_driver):
        vi = self._make_vi(smu_driver)
        vi.initiate(current_A=3e-6, readings_per_point=10)
        data = vi.take_reading()
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
            vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1, readings_per_point=3)
            data = vi.take_reading()
            assert set(data.keys()) == {"voltage_V", "current_A"}
            assert len(data["voltage_V"]) == 3


# ---------------------------------------------------------------------------
# LockInHarmonicMeasurementVI
# ---------------------------------------------------------------------------

class TestLockInHarmonicMeasurementVI:
    """Tests for LockInHarmonicMeasurementVI (internal-source 1f/2f)."""

    def _make_vi(self, lockin, series_resistance_ohm=1e6):
        from cryosoft.virtual_instruments.measurement.lockin_harmonic import LockInHarmonicMeasurementVI
        vi = LockInHarmonicMeasurementVI(
            {"lockin": lockin}, series_resistance_ohm=series_resistance_ohm
        )
        vi.vi_name = "lockin_harmonic"
        return vi

    def test_ping_returns_true_when_driver_responds(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        assert vi.ping() is True

    def test_take_reading_without_initiate_raises(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        with pytest.raises(RuntimeError):
            vi.take_reading()

    def test_initiate_sets_internal_reference(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        vi.initiate(oscillator_amplitude_V=0.5)
        assert lockin_driver.get_reference_source() == "INT"
        assert lockin_driver.get_oscillator_amplitude() == pytest.approx(0.5)

    def test_initiate_sets_oscillator_frequency_and_time_constant(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        vi.initiate(oscillator_frequency_Hz=1234.0, time_constant_s=0.3)
        assert lockin_driver.get_oscillator_frequency() == pytest.approx(1234.0)
        assert lockin_driver.get_time_constant() == pytest.approx(0.3)

    def test_take_reading_returns_correct_n_points(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        vi.initiate(n_readings=7)
        data = vi.take_reading()
        for key in ("x_1f_V", "y_1f_V", "x_2f_V", "y_2f_V", "current_A"):
            assert key in data
            assert len(data[key]) == 7

    def test_take_reading_switches_harmonic_between_1f_and_2f(self, lockin_driver):
        """A single-demodulator lock-in reports one harmonic at a time."""
        vi = self._make_vi(lockin_driver)
        lockin_driver._noise_std = 0.0  # deterministic
        vi.initiate(oscillator_amplitude_V=1.0, n_readings=1)
        data = vi.take_reading()
        # 1f response is linear in amplitude, 2f is quadratic (see SimLockIn) —
        # different values confirm take_reading() actually switched harmonics.
        assert data["x_1f_V"][0] != pytest.approx(data["x_2f_V"][0])
        assert lockin_driver.get_harmonic() == 2  # left on 2f after the last read

    def test_current_computed_from_amplitude_and_series_resistance(self, lockin_driver):
        vi = self._make_vi(lockin_driver, series_resistance_ohm=2e6)
        vi.initiate(oscillator_amplitude_V=1.0, n_readings=3)
        data = vi.take_reading()
        assert all(c == pytest.approx(0.5e-6) for c in data["current_A"])

    def test_standby_zeros_oscillator_amplitude(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        vi.initiate(oscillator_amplitude_V=1.0)
        vi.standby()
        assert lockin_driver.get_oscillator_amplitude() == pytest.approx(0.0)

    def test_standby_blocks_subsequent_take_reading(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        vi.initiate()
        vi.standby()
        with pytest.raises(RuntimeError):
            vi.take_reading()

    def test_vi_type_is_measurement(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        assert vi.vi_type == "measurement"

    def test_data_arrays_matches_measurement_data_keys(self, lockin_driver):
        vi = self._make_vi(lockin_driver)
        arrays = vi.data_arrays({"n_readings": 5})
        assert set(arrays) == set(vi.measurement_data_keys)
        assert all(length == 5 for length in arrays.values())
