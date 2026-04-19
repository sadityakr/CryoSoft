# ---
# description: |
#   Unit tests for new simulated driver capabilities added in the
#   behavior-based VI refactor: IPS120 switch heater/persistent mode,
#   ITC503 needle valve, ILM200 3-mode, and the new Keithley 2400 SMU.
# entry_point: pytest tests/test_l0_new_drivers.py -v
# last_updated: 2026-04-19
# ---

"""Tests for extended and new simulated drivers (Stage 1 of VI refactor)."""

import pytest


class TestSimOxfordIPS120SwitchHeater:
    """Tests for switch heater and persistent mode methods on SimOxfordIPS120."""

    def test_switch_heater_initial_state(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        assert d.get_switch_heater_state() == "OFF"
        assert d.get_persistent_mode() is False
        assert d.get_coil_current() == pytest.approx(0.0)

    def test_switch_heater_on_off(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d.set_switch_heater(True)
        assert d.get_switch_heater_state() == "ON"
        d.set_switch_heater(False)
        assert d.get_switch_heater_state() == "OFF"

    def test_enter_persistent_mode_captures_current(self):
        """Entering persistent mode fixes the coil current at the PSU current."""
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d._current = 42.5
        d.set_persistent_mode(True)
        assert d.get_persistent_mode() is True
        assert d.get_coil_current() == pytest.approx(42.5)

    def test_exit_persistent_mode(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d._current = 42.5
        d.set_persistent_mode(True)
        d.set_persistent_mode(False)
        assert d.get_persistent_mode() is False
        # Coil current retains its value until VI explicitly resets it
        assert d.get_coil_current() == pytest.approx(42.5)

    def test_return_types(self):
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        assert isinstance(d.get_switch_heater_state(), str)
        assert isinstance(d.get_coil_current(), float)
        assert isinstance(d.get_persistent_mode(), bool)

    def test_simulate_error_blocks_new_methods(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

        d = SimOxfordIPS120("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_switch_heater_state()
        with pytest.raises(CryoSoftCommunicationError):
            d.get_coil_current()
        with pytest.raises(CryoSoftCommunicationError):
            d.get_persistent_mode()


class TestSimOxfordITC503NeedleValve:
    """Tests for needle valve methods on SimOxfordITC503."""

    def test_needle_valve_initial_state(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        assert d.get_needle_valve() == pytest.approx(0.0)

    def test_needle_valve_set_and_get(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d.set_needle_valve(50.0)
        assert d.get_needle_valve() == pytest.approx(50.0)

    def test_needle_valve_clamped_above_100(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d.set_needle_valve(150.0)
        assert d.get_needle_valve() == pytest.approx(100.0)

    def test_needle_valve_clamped_below_0(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d.set_needle_valve(-10.0)
        assert d.get_needle_valve() == pytest.approx(0.0)

    def test_return_type(self):
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        assert isinstance(d.get_needle_valve(), float)

    def test_simulate_error_blocks_needle_valve(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_needle_valve()

    def test_existing_methods_unchanged(self):
        """Verify that adding needle valve did not break existing API."""
        from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

        d = SimOxfordITC503("SIM")
        assert isinstance(d.get_temperature(), float)
        assert isinstance(d.get_setpoint(), float)
        assert isinstance(d.get_heater_output(), float)


class TestSimOxfordILM200ThreeMode:
    """Tests for the extended 3-mode standard on SimOxfordILM200."""

    def test_mode_0_standby(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        d.set_refresh_rate(0)
        assert d.get_refresh_rate() == 0

    def test_mode_1_slow(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        d.set_refresh_rate(1)
        assert d.get_refresh_rate() == 1

    def test_mode_2_fast(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        d.set_refresh_rate(2)
        assert d.get_refresh_rate() == 2

    def test_invalid_mode_raises(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        with pytest.raises(ValueError):
            d.set_refresh_rate(3)

    def test_existing_methods_unchanged(self):
        from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200

        d = SimOxfordILM200("SIM")
        assert isinstance(d.get_helium_level(), float)
        assert isinstance(d.get_nitrogen_level(), float)


class TestSimKeithley2400:
    """Tests for the new SimKeithley2400 SMU driver."""

    def test_contract_single_string_init(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("GPIB0::24::INSTR")
        assert d is not None

    def test_initial_state(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        assert d.get_current() == pytest.approx(0.0)
        assert d.get_compliance() > 0.0
        assert d.get_range() > 0.0

    def test_set_and_get_current(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        d.set_current(1e-6)
        assert d.get_current() == pytest.approx(1e-6)

    def test_set_and_get_compliance(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        d.set_compliance(0.5)
        assert d.get_compliance() == pytest.approx(0.5)

    def test_set_and_get_range(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        d.set_range(0.1)
        assert d.get_range() == pytest.approx(0.1)

    def test_get_voltage_returns_float(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        assert isinstance(d.get_voltage(), float)

    def test_voltage_proportional_to_current(self):
        """Voltage = R * I; ratio should be consistent across multiple readings."""
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        d.set_current(1e-3)
        voltages = [d.get_voltage() for _ in range(50)]
        mean_v = sum(voltages) / len(voltages)
        # With R=1500 Ω and I=1 mA, V ≈ 1.5 V
        assert mean_v == pytest.approx(1.5, rel=0.01)

    def test_zero_current_gives_near_zero_voltage(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        d.set_current(0.0)
        v = d.get_voltage()
        assert abs(v) < 1e-5  # Only noise at zero current

    def test_idn_string(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        idn = d.get_idn()
        assert isinstance(idn, str)
        assert "KEITHLEY" in idn
        assert "2400" in idn

    def test_simulate_error_raises(self):
        from cryosoft.core.exceptions import CryoSoftCommunicationError
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        d._simulate_error = True
        with pytest.raises(CryoSoftCommunicationError):
            d.get_voltage()
        with pytest.raises(CryoSoftCommunicationError):
            d.get_idn()
        with pytest.raises(CryoSoftCommunicationError):
            d.get_current()

    def test_return_types(self):
        from cryosoft.drivers.sim_keithley_2400 import SimKeithley2400

        d = SimKeithley2400("SIM")
        d.set_current(1e-6)
        assert isinstance(d.get_voltage(), float)
        assert isinstance(d.get_current(), float)
        assert isinstance(d.get_compliance(), float)
        assert isinstance(d.get_range(), float)
        assert isinstance(d.get_idn(), str)
