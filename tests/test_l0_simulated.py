# ---
# description: |
#   Unit tests for all simulated drivers. Verifies the 3-rule contract,
#   correct return types, and simulated physics behavior.
# entry_point: pytest tests/test_l0_simulated.py -v
# last_updated: 2026-04-06
# ---

"""Test suite for simulated drivers (Layer 0b)."""

import time

import pytest


class TestSimOxfordIPS120:
    """Tests for SimOxfordIPS120 magnet power supply."""

    def test_contract_single_string_init(self):
        """Driver accepts a single string argument."""
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        driver = SimOxfordIPS120("GPIB0::25::INSTR")
        assert driver is not None

    def test_initial_state(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        assert d.get_current() == pytest.approx(0.0)
        assert d.get_current_setpoint() == pytest.approx(0.0)
        assert d.get_status() == "HOLD"

    def test_return_types(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        assert isinstance(d.get_current(), float)
        assert isinstance(d.get_current_setpoint(), float)
        assert isinstance(d.get_status(), str)

    def test_ramp_starts_on_setpoint_change(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d.set_current_setpoint(10.0)
        assert d.get_status() == "RAMPING"

    def test_ramp_completes(self):
        """Ramp from 0 to a small target completes with enough time."""
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d.set_ramp_rate(600.0)  # Very fast: 600 A/min for testing
        d.set_current_setpoint(1.0)
        # Simulate passage of time
        d._last_update = time.time() - 1.0  # 1 second ago
        d._update_simulation()
        assert d.get_current() == pytest.approx(1.0)
        assert d.get_status() == "HOLD"

    def test_ramp_direction_negative(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d._current = 10.0
        d.set_ramp_rate(600.0)
        d.set_current_setpoint(0.0)
        d._last_update = time.time() - 2.0
        d._update_simulation()
        assert d.get_current() == pytest.approx(0.0)

    def test_hold_when_no_ramp(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d._last_update = time.time() - 10.0
        d._update_simulation()
        assert d.get_current() == pytest.approx(0.0)
        assert d.get_status() == "HOLD"

    def test_setpoint_clamping_above_max(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d.set_current_setpoint(200.0)  # Above 90 A max
        assert d.get_current_setpoint() == pytest.approx(90.0)

    def test_setpoint_clamping_below_min(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d.set_current_setpoint(-200.0)  # Below -90 A min
        assert d.get_current_setpoint() == pytest.approx(-90.0)

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_current()

    def test_quench_status(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d._simulate_quench = True
        assert d.get_status() == "QUENCH"

    def test_no_ramp_when_setpoint_at_current(self):
        """No state transition when setpoint equals current (within 0.01 A)."""
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d.set_current_setpoint(0.005)  # Difference < 0.01
        assert d.get_status() == "HOLD"


class TestSimRotator:
    """Tests for SimRotator sample-rotation stage."""

    def test_contract_single_string_init(self):
        """Driver accepts a single string argument."""
        from cryosoft.drivers.sim_rotator import SimRotator

        driver = SimRotator("GPIB0::26::INSTR")
        assert driver is not None

    def test_initial_state(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        assert d.get_position() == pytest.approx(0.0)
        assert d.get_position_setpoint() == pytest.approx(0.0)
        assert d.get_status() == "HOLD"

    def test_return_types(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        assert isinstance(d.get_position(), float)
        assert isinstance(d.get_position_setpoint(), float)
        assert isinstance(d.get_status(), str)

    def test_ramp_starts_on_setpoint_change(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d.set_position_setpoint(90.0)
        assert d.get_status() == "MOVING"

    def test_ramp_completes(self):
        """Rotation from 0 to a small target completes with enough time."""
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d.set_rate(600.0)  # Very fast: 600 deg/min for testing
        d.set_position_setpoint(1.0)
        d._last_update = time.time() - 1.0  # 1 second ago
        d._update_simulation()
        assert d.get_position() == pytest.approx(1.0)
        assert d.get_status() == "HOLD"

    def test_ramp_direction_negative(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d._position = 10.0
        d.set_rate(600.0)
        d.set_position_setpoint(0.0)
        d._last_update = time.time() - 2.0
        d._update_simulation()
        assert d.get_position() == pytest.approx(0.0)

    def test_hold_when_no_ramp(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d._last_update = time.time() - 10.0
        d._update_simulation()
        assert d.get_position() == pytest.approx(0.0)
        assert d.get_status() == "HOLD"

    def test_setpoint_clamping_above_max(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d.set_position_setpoint(500.0)  # Above 180 deg max
        assert d.get_position_setpoint() == pytest.approx(180.0)

    def test_setpoint_clamping_below_min(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d.set_position_setpoint(-500.0)  # Below -180 deg min
        assert d.get_position_setpoint() == pytest.approx(-180.0)

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_position()

    def test_no_ramp_when_setpoint_at_current(self):
        """No state transition when setpoint equals current (within 0.01 deg)."""
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d.set_position_setpoint(0.005)  # Difference < 0.01
        assert d.get_status() == "HOLD"

    def test_set_rate_rejects_non_positive(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        with pytest.raises(ValueError):
            d.set_rate(0.0)

    def test_hold_freezes_position(self):
        from cryosoft.drivers.sim_rotator import SimRotator

        d = SimRotator("SIM")
        d.set_rate(600.0)
        d.set_position_setpoint(90.0)
        d._last_update = time.time() - 0.05  # small step, mid-ramp
        d.hold()
        assert d.get_status() == "HOLD"
        assert d.get_position_setpoint() == pytest.approx(d.get_position())


class TestSimOxfordITC503:
    """Tests for SimOxfordITC503 temperature controller."""

    def test_contract_single_string_init(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        assert d is not None

    def test_initial_state(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        assert d.get_temperature() == pytest.approx(300.0, abs=1.0)
        assert d.get_setpoint() == pytest.approx(300.0)

    def test_temperature_approaches_setpoint(self):
        """Setpoint change causes gradual approach, not instant jump."""
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d._temperature = 300.0
        d.set_setpoint(200.0)
        # Small time step
        d._last_update = time.time() - 1.0
        d._update_simulation()
        temp = d.get_temperature()
        # Should have moved toward 200 but not reached it
        assert 200.0 < temp < 300.0

    def test_heater_output_range(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        output = d.get_heater_output()
        assert 0.0 <= output <= 100.0

    def test_return_types(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        assert isinstance(d.get_temperature(), float)
        assert isinstance(d.get_setpoint(), float)
        assert isinstance(d.get_heater_output(), float)

    def test_temperature_settles_to_setpoint(self):
        """After very long simulated time, temperature equals setpoint."""
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d._temperature = 300.0
        d.set_setpoint(4.0)
        # Simulate 1 hour elapsed (tau = 60 s, so ~60 time constants)
        d._last_update = time.time() - 3600.0
        d._update_simulation()
        assert d.get_temperature() == pytest.approx(4.0, abs=0.01)

    def test_heater_capped_at_100(self):
        """Heater output never exceeds 100%."""
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d._temperature = 300.0
        d.set_setpoint(4.0)  # Large difference -> heater would be >100% uncapped
        output = d.get_heater_output()
        assert output <= 100.0

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_temperature()


class TestSimOxfordILM200:
    """Tests for SimOxfordILM200 level meter."""

    def test_contract_single_string_init(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        assert d is not None

    def test_helium_in_range(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        level = d.get_helium_level()
        assert 0.0 <= level <= 100.0

    def test_nitrogen_in_range(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        level = d.get_nitrogen_level()
        assert 0.0 <= level <= 100.0

    def test_helium_drifts_down(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        initial = d.get_helium_level()
        d._last_update = time.time() - 600  # 10 minutes ago
        d._update_simulation()
        later = d.get_helium_level()
        assert later < initial

    def test_refresh_rate_modes(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        d.set_refresh_rate(1)
        assert d.get_refresh_rate() == 1
        d.set_refresh_rate(0)
        assert d.get_refresh_rate() == 0

    def test_force_helium_level(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        d._force_helium_level = 5.0
        assert d.get_helium_level() == pytest.approx(5.0)

    def test_return_types(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        assert isinstance(d.get_helium_level(), float)
        assert isinstance(d.get_nitrogen_level(), float)
        assert isinstance(d.get_refresh_rate(), int)

    def test_helium_does_not_go_below_zero(self):
        """Level is clamped at 0% even after very long elapsed time."""
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        d._helium_level = 0.1
        d._last_update = time.time() - 1_000_000  # Very long time
        d._update_simulation()
        assert d.get_helium_level() >= 0.0

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_helium_level()


class TestSimKeithley6221:
    """Tests for SimKeithley6221 current source."""

    def test_contract_single_string_init(self):
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        d = SimKeithley6221("SIM")
        assert d is not None

    def test_source_enable_disable(self):
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        d = SimKeithley6221("SIM")
        d.set_source_enabled(True)
        assert d.get_source_enabled() is True
        d.set_source_enabled(False)
        assert d.get_source_enabled() is False

    def test_configure_delta_mode(self):
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        d = SimKeithley6221("SIM")
        d.configure_delta_mode(high_current=1e-6, n_readings=100, delay=0.01)
        # Should not raise

    def test_delta_readings_returns_list(self):
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        d = SimKeithley6221("SIM")
        d.configure_delta_mode(high_current=1e-6, n_readings=50, delay=0.001)
        d.trigger_delta_mode()
        readings = d.get_delta_readings()
        assert isinstance(readings, list)
        assert len(readings) == 50

    def test_delta_readings_are_floats(self):
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        d = SimKeithley6221("SIM")
        d.configure_delta_mode(high_current=1e-6, n_readings=10, delay=0.001)
        d.trigger_delta_mode()
        readings = d.get_delta_readings()
        assert all(isinstance(v, float) for v in readings)

    def test_current_get_set(self):
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        d = SimKeithley6221("SIM")
        d.set_current(1e-3)
        assert d.get_current() == pytest.approx(1e-3)

    def test_delta_readings_with_paired_meter(self):
        """When a 2182A is paired, readings come from its get_voltage()."""
        from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        meter = SimKeithley2182A("SIM")
        meter._base_voltage = 2.5e-6
        meter._noise_std = 0.0  # Zero noise: deterministic

        source = SimKeithley6221("SIM")
        source._paired_meter = meter
        source.configure_delta_mode(high_current=1e-6, n_readings=5, delay=0.001)
        source.trigger_delta_mode()
        readings = source.get_delta_readings()
        assert all(v == pytest.approx(2.5e-6) for v in readings)

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

        d = SimKeithley6221("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_source_enabled()


class TestSimKeithley2182A:
    """Tests for SimKeithley2182A nanovoltmeter."""

    def test_contract_single_string_init(self):
        from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

        d = SimKeithley2182A("SIM")
        assert d is not None

    def test_voltage_returns_float(self):
        from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

        d = SimKeithley2182A("SIM")
        v = d.get_voltage()
        assert isinstance(v, float)

    def test_range_setting(self):
        from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

        d = SimKeithley2182A("SIM")
        d.set_range(1.0)
        assert d.get_range() == pytest.approx(1.0)

    def test_voltage_has_noise(self):
        """Multiple voltage readings differ due to Gaussian noise."""
        from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

        d = SimKeithley2182A("SIM")
        readings = [d.get_voltage() for _ in range(20)]
        # With non-zero noise_std, readings should not all be identical
        assert len(set(readings)) > 1

    def test_voltage_near_base(self):
        """Voltage readings are within a few standard deviations of base."""
        from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

        d = SimKeithley2182A("SIM")
        readings = [d.get_voltage() for _ in range(50)]
        mean = sum(readings) / len(readings)
        # Mean should be within 10 noise_std of base_voltage
        assert abs(mean - d._base_voltage) < 10 * d._noise_std

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

        d = SimKeithley2182A("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_voltage()


class TestSimLockIn:
    """Tests for SimLockIn phase-sensitive lock-in amplifier."""

    def test_contract_single_string_init(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("GPIB0::8::INSTR")
        assert d is not None

    def test_initial_state(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        assert d.get_reference_source() == "INT"
        assert d.get_oscillator_amplitude() == pytest.approx(0.0)
        assert d.get_harmonic() == 1

    def test_reference_source_round_trip(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d.set_reference_source("EXT")
        assert d.get_reference_source() == "EXT"

    def test_reference_source_rejects_invalid(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        with pytest.raises(ValueError):
            d.set_reference_source("BOGUS")

    def test_oscillator_amplitude_round_trip(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d.set_oscillator_amplitude(0.5)
        assert d.get_oscillator_amplitude() == pytest.approx(0.5)

    def test_oscillator_amplitude_rejects_negative(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        with pytest.raises(ValueError):
            d.set_oscillator_amplitude(-1.0)

    def test_oscillator_frequency_round_trip(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d.set_oscillator_frequency(1234.0)
        assert d.get_oscillator_frequency() == pytest.approx(1234.0)

    def test_oscillator_frequency_rejects_non_positive(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        with pytest.raises(ValueError):
            d.set_oscillator_frequency(0.0)

    def test_harmonic_round_trip(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d.set_harmonic(2)
        assert d.get_harmonic() == 2

    def test_harmonic_rejects_non_positive(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        with pytest.raises(ValueError):
            d.set_harmonic(0)

    def test_time_constant_round_trip(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d.set_time_constant(0.3)
        assert d.get_time_constant() == pytest.approx(0.3)

    def test_time_constant_rejects_non_positive(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        with pytest.raises(ValueError):
            d.set_time_constant(0.0)

    def test_x_scales_with_amplitude_at_1f(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d._noise_std = 0.0  # deterministic
        d.set_harmonic(1)
        d.set_oscillator_amplitude(1.0)
        x1 = d.get_x()
        d.set_oscillator_amplitude(2.0)
        x2 = d.get_x()
        assert x2 == pytest.approx(2 * x1)

    def test_x_scales_quadratically_with_amplitude_at_2f(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d._noise_std = 0.0  # deterministic
        d.set_harmonic(2)
        d.set_oscillator_amplitude(1.0)
        x1 = d.get_x()
        d.set_oscillator_amplitude(2.0)
        x2 = d.get_x()
        assert x2 == pytest.approx(4 * x1)

    def test_return_types(self):
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        assert isinstance(d.get_x(), float)
        assert isinstance(d.get_y(), float)
        assert isinstance(d.get_reference_source(), str)
        assert isinstance(d.get_harmonic(), int)

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_lockin import SimLockIn

        d = SimLockIn("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_x()
