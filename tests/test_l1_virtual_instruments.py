import pytest
import time
from cryosoft.core.exceptions import CryoSoftCommunicationError, CryoSoftSafetyError

# Assuming we have valid sim drivers from L0 test phase
from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120
from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503
from cryosoft.drivers.sim_oxford_ilm200 import SimOxfordILM200
from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221
from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

from cryosoft.virtual_instruments.base import BaseVirtualInstrument
from cryosoft.core.decorators import monitored, control

# 1. BaseVirtualInstrument subclass logging/state tests
class MockBaseVI(BaseVirtualInstrument):
    vi_type = "mock"
    def __init__(self):
        super().__init__({})
        self.calls = 0

    @monitored
    def mon_test(self):
        self.calls += 1
        return "monitored_val"

    @control
    def ctrl_test(self, val):
        self.calls += 1
        return val

    @control
    def error_test(self):
        raise ValueError("test error")

def test_base_vi_logging_and_state():
    vi = MockBaseVI()
    
    state = vi.get_state()
    assert "mon_test" in state
    assert state["mon_test"] == "monitored_val"
    assert vi.calls == 1

def test_control_specs_and_panel_survive_subclass_wrapping():
    """__init_subclass__ must propagate _control_specs/_control_panel onto the
    limit+logging wrappers, or the GUI would see empty metadata on every VI."""
    from cryosoft.core.decorators import get_control_panel, get_control_specs
    from cryosoft.core.plan import ParamSpec

    spec = ParamSpec(type=float, default=0.0, unit="W", min=0.0, max=40.0)

    class SpecVI(BaseVirtualInstrument):
        vi_type = "mock"

        @control(params={"power_W": spec}, panel=False)
        def set_heater_power(self, power_W: float = 0.0):
            return power_W

    vi = SpecVI({})
    assert get_control_specs(vi.set_heater_power) == {"power_W": spec}
    assert get_control_panel(vi.set_heater_power) is False
    # Legacy bare @control: no specs, panel defaults True.
    mock = MockBaseVI()
    assert get_control_specs(mock.ctrl_test) == {}
    assert get_control_panel(mock.ctrl_test) is True


def test_control_specs_must_be_paramspec_instances():
    """A non-ParamSpec spec value fails at class creation, not on click."""
    with pytest.raises(TypeError, match="must be a ParamSpec"):
        class BadSpecVI(BaseVirtualInstrument):
            vi_type = "mock"

            @control(params={"power_W": {"type": float, "default": 0.0}})
            def set_heater_power(self, power_W: float = 0.0):
                return power_W


class LimitedVI(BaseVirtualInstrument):
    """A VI declaring one control limit, for the limit-wrapper tests below."""

    vi_type = "limited"
    control_limits = {"set_current": {"current_A": "max_current"}}

    def __init__(self):
        super().__init__({})
        self._limits["max_current"] = (-1e-3, 1e-3)

    @control
    def set_current(self, current_A):
        return current_A


def test_limit_wrapper_allows_a_value_inside_the_declared_range():
    """A control call within its declared limit reaches the method untouched."""
    vi = LimitedVI()
    vi.vi_name = "source"

    assert vi.set_current(5e-4) == 5e-4


def test_limit_wrapper_refusal_carries_structured_fields():
    """An out-of-range control call is refused with fields AND the same prose.

    Two assertions, deliberately together: a verdict is built from the
    structured fields, never by parsing the message, and the message itself is
    the operator's banner, so it must not drift when the fields are added.
    """
    vi = LimitedVI()
    vi.vi_name = "source"

    with pytest.raises(CryoSoftSafetyError) as excinfo:
        vi.set_current(0.05)
    err = excinfo.value

    assert str(err) == (
        "source.set_current: current_A=0.05 is outside the allowed range "
        "[-0.001, 0.001] for this setup (limit 'max_current' from the station "
        "config). Command refused."
    )
    assert err.param == "current_A"
    assert err.value == 0.05
    assert err.lo == -1e-3
    assert err.hi == 1e-3
    assert err.limit_name == "max_current"


def test_base_vi_error_pass_through():
    vi = MockBaseVI()
    with pytest.raises(ValueError):
        vi.error_test()

def test_communication_error_wrapping(monkeypatch):
    class FakeVisaIOError(Exception):
        pass

    import sys
    import types
    fake_pyvisa = types.ModuleType("pyvisa")
    fake_pyvisa.errors = types.ModuleType("errors")
    fake_pyvisa.errors.VisaIOError = FakeVisaIOError
    sys.modules["pyvisa"] = fake_pyvisa

    class MockCommVI(BaseVirtualInstrument):
        __module__ = "test"
        @monitored
        def fail(self):
            raise FakeVisaIOError("timeout")

    vi = MockCommVI({"main": None})
    with pytest.raises(CryoSoftCommunicationError):
        vi.fail()


# 3b. MeasurementInstrumentBase mean/error/array convention helpers
def test_mean_and_sem_multiple_samples():
    from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

    mean, sem = MeasurementInstrumentBase.mean_and_sem([1.0, 2.0, 3.0])
    assert mean == pytest.approx(2.0)
    # stdev([1,2,3], ddof=1) == 1.0; sem == 1.0 / sqrt(3)
    assert sem == pytest.approx(1.0 / 3**0.5)


def test_mean_and_sem_single_sample_has_zero_error():
    from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

    mean, sem = MeasurementInstrumentBase.mean_and_sem([5.0])
    assert mean == pytest.approx(5.0)
    assert sem == 0.0


def test_mean_and_sem_no_samples_is_nan():
    from cryosoft.virtual_instruments.base import MeasurementInstrumentBase
    import math

    mean, sem = MeasurementInstrumentBase.mean_and_sem([])
    assert math.isnan(mean)
    assert math.isnan(sem)


def test_quantity_columns_derives_array_mean_error_keys():
    from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

    array_keys, scalar_columns = MeasurementInstrumentBase.quantity_columns(
        "voltage_V", "current_A"
    )
    assert array_keys == ["voltage_V_array", "current_A_array"]
    assert scalar_columns == {
        "voltage_V": "float",
        "voltage_V_error": "float",
        "current_A": "float",
        "current_A_error": "float",
    }


def test_quantity_columns_rejects_colliding_base_name():
    from cryosoft.core.exceptions import CryoSoftConfigError
    from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

    with pytest.raises(CryoSoftConfigError, match="_array' or '_error'"):
        MeasurementInstrumentBase.quantity_columns("voltage_V_error")


# 4. Magnet VI tests
def test_magnet_vi_ramp_cycle():
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import SuperconductingMagnetVI

    driver = SimOxfordIPS120("SIM")
    vi = SuperconductingMagnetVI({"main": driver}, default_ramp_rate=1200.0, amperes_per_tesla=10.0)

    vi.start_ramp(1.0)
    assert vi.ramp_status() == "RAMPING"

    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()

    assert vi.ramp_status() in ("TARGET_REACHED", "IDLE")
    assert vi.magnet_field_T() == pytest.approx(1.0, abs=0.1)

def test_magnet_vi_ramp_segments():
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import SuperconductingMagnetVI

    driver = SimOxfordIPS120("SIM")
    segments = [
        {"max_current_A": 20.0, "rate_A_per_min": 600.0},
        {"max_current_A": float('inf'), "rate_A_per_min": 100.0}
    ]
    vi = SuperconductingMagnetVI({"main": driver}, default_ramp_rate=5.0, amperes_per_tesla=10.0, ramp_segments=segments)

    vi.start_ramp(3.0)

    rates_used = set()
    for _ in range(100):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()
        rates_used.add(driver._ramp_rate)
        if vi.ramp_status() == "TARGET_REACHED":
            break

    assert 600.0 in rates_used
    assert 100.0 in rates_used
    assert vi.magnet_field_T() == pytest.approx(3.0, abs=0.1)

def test_magnet_vi_safety_clamping():
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import SuperconductingMagnetVI

    driver = SimOxfordIPS120("SIM")
    vi = SuperconductingMagnetVI({"main": driver}, max_current=50.0, min_current=-50.0)

    vi.start_ramp(6.0)
    for _ in range(10):
        vi.advance_ramp()

    assert driver.get_current_setpoint() <= 50.0

# 10. Temperature VI tests
def test_temperature_vi_ramp():
    from cryosoft.virtual_instruments.temperature.sample_temperature_controller import SampleTemperatureControllerVI
    driver = SimOxfordITC503("SIM")

    vi = SampleTemperatureControllerVI({"main": driver}, default_ramp_rate=6000.0, tolerance=2.0)
    vi.start_ramp(200.0)

    for _ in range(20):
        time.sleep(0.01)
        vi.advance_ramp()
        driver._last_update = time.time() - 1.0
        driver._update_simulation()

    assert vi.ramp_status() in ("RAMPING", "TARGET_REACHED")

def test_temperature_vi_set_pid_forwards_to_driver_and_hides_from_card():
    """set_pid programs all three PID values on the driver; front-panel only.

    Covers both temperature VIs (the VTI VI inherits set_pid unchanged).
    """
    from cryosoft.core.decorators import get_control_panel
    from cryosoft.virtual_instruments.temperature.sample_temperature_controller import (
        SampleTemperatureControllerVI,
    )

    driver = SimOxfordITC503("SIM")
    vi = SampleTemperatureControllerVI({"main": driver})

    vi.set_pid(p_K=25.0, i_min=2.5, d_min=0.5)
    assert driver.get_proportional_band() == pytest.approx(25.0)
    assert driver.get_integral_action_time() == pytest.approx(2.5)
    assert driver.get_derivative_action_time() == pytest.approx(0.5)

    # panel=False: shown in the instrument front panel, never on the card.
    assert get_control_panel(vi.set_pid) is False


# 13. Level meter tests
def test_level_vi_buffer():
    from cryosoft.virtual_instruments.level.cryogen_level_meter import CryogenLevelMeterVI
    driver = SimOxfordILM200("SIM")
    vi = CryogenLevelMeterVI({"main": driver}, helium_low_threshold=20.0, buffer_size=3)

    assert vi.helium_low() is False

    driver._force_helium_level = 15.0
    vi.helium_level()
    assert vi.helium_low() is False

    vi.helium_level()
    vi.helium_level()
    assert vi.helium_low() is True

    driver._force_helium_level = 50.0
    vi.helium_level()
    vi.helium_level()
    assert vi.helium_low() is False

# 15. Delta-mode tests
def test_delta_mode_vi():
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI
    
    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    
    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})

    with pytest.raises(RuntimeError):
        vi.take_reading()

    vi.initiate_measurement(current=1e-6, n_readings=10, delay_s=0.001)

    data = vi.take_reading()
    assert "voltage_V" in data
    assert "current_A" in data
    assert len(data["voltage_V_array"]) == 10
    assert len(data["current_A_array"]) == 10
    assert data["n_valid"] == 10
    assert data["voltage_V"] == pytest.approx(
        sum(data["voltage_V_array"]) / 10
    )
    assert data["voltage_V_error"] >= 0.0


def test_delta_mode_vi_forwards_all_config_params():
    """initiate() forwards the full delta parameter set to the source driver.

    Regression guard: compliance, range, compliance_abort and cold_switch must
    reach configure_and_start_delta() (a wrong/skipped delta command errors on
    the real 6221), so the sim driver records them for inspection here.
    """
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})

    vi.initiate_measurement(
        current=2e-6,
        n_readings=5,
        delay_s=0.002,
        compliance_V=3.0,
        voltmeter_range_V=0.1,
        compliance_abort=False,
        cold_switch=True,
    )

    assert source._delta_high_current == pytest.approx(2e-6)
    assert source._delta_compliance == pytest.approx(3.0)
    assert source._delta_range_2182a == pytest.approx(0.1)
    assert source._delta_compliance_abort is False
    assert source._delta_cold_switch is True


def test_delta_mode_initiate_raises_when_meter_not_detected():
    """initiate() surfaces a clear error when the 2182A isn't on the relay.

    Regression guard for the 12t-cryo delta-mode hang incident: silently
    arming without the 2182A produced confusing overflow/timeout symptoms
    several calls later instead of an immediate, actionable failure.
    """
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    source._meter_present = False
    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})

    with pytest.raises(CryoSoftCommunicationError):
        vi.initiate_measurement(current=1e-6, n_readings=10, delay_s=0.001)


def test_delta_mode_declares_current_reading_setter():
    """current is loopable via set_delta_current (the reading-loop standard)."""
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI

    assert DeltaModeMeasurementVI.reading_setters == {"current": "set_delta_current"}


def test_delta_mode_set_current_before_initiate_raises():
    """The reading-loop setter refuses to run against an unarmed engine."""
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})

    with pytest.raises(RuntimeError):
        vi.set_delta_current(1e-6)


def test_delta_mode_set_current_rearms_and_reports_new_current():
    """set_delta_current() re-arms the engine and take_reading() reports it.

    Delta mode latches its peak current at arm time, so the setter must leave
    the sim in DELTA mode at the new amplitude — a plain stop (or a stop with
    no re-arm) would leave the source in DC and yield no delta readings.
    """
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})

    vi.initiate_measurement(current=1e-6, n_readings=4, delay_s=0.001)
    vi.set_delta_current(5e-6)

    assert source._mode == "DELTA"
    assert source._delta_high_current == pytest.approx(5e-6)

    data = vi.take_reading()
    assert all(c == pytest.approx(5e-6) for c in data["current_A_array"])
    assert data["current_A"] == pytest.approx(5e-6)
    assert data["n_valid"] == 4


def test_delta_mode_set_current_preserves_other_armed_params():
    """Re-arming changes only the current; every other armed parameter holds.

    Regression guard for the reading loop: rebuilding the delta configuration
    from defaults instead of the armed values would silently reset compliance,
    range and the abort/cold-switch flags partway through a sweep.
    """
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})

    vi.initiate_measurement(
        current=2e-6,
        n_readings=5,
        delay_s=0.002,
        compliance_V=3.0,
        voltmeter_range_V=0.1,
        compliance_abort=False,
        cold_switch=True,
    )
    vi.set_delta_current(-7e-6)

    assert source._delta_high_current == pytest.approx(-7e-6)
    assert source._delta_n_readings == 5
    assert source._delta_delay == pytest.approx(0.002)
    assert source._delta_compliance == pytest.approx(3.0)
    assert source._delta_range_2182a == pytest.approx(0.1)
    assert source._delta_compliance_abort is False
    assert source._delta_cold_switch is True


def test_delta_mode_short_return_is_nan_padded():
    """A short delta acquisition is padded to n_readings with NaN + n_valid.

    The real Keithley 6221 can return fewer than n_readings samples (compliance
    abort / repeated read failures). The VI must always return arrays of exactly
    n_readings, padding the missing tail with NaN, and report the true count in
    the n_valid scalar column — the fixed-shape contract the HDF5 layout needs.
    """
    import math

    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import DeltaModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    # Force the sim engine to return only 3 of the 10 requested samples.
    source._delta_return_count = 3

    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})
    vi.initiate_measurement(current=1e-6, n_readings=10, delay_s=0.001)
    data = vi.take_reading()

    assert len(data["voltage_V_array"]) == 10
    assert len(data["current_A_array"]) == 10
    assert data["n_valid"] == 3
    # First 3 are real; the padded tail is NaN in both arrays.
    assert all(not math.isnan(v) for v in data["voltage_V_array"][:3])
    assert all(math.isnan(v) for v in data["voltage_V_array"][3:])
    assert all(math.isnan(c) for c in data["current_A_array"][3:])
    # Mean/error are computed only from the 3 valid samples, not the NaN pad.
    assert not math.isnan(data["voltage_V"])
    assert not math.isnan(data["voltage_V_error"])


def test_dc_mode_measurement_vi_lifecycle():
    from cryosoft.virtual_instruments.measurement.measurement_dc_mode import DCModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DCModeMeasurementVI({"source": source, "meter": meter})

    assert vi.ping() is True

    # Before initiate, take_reading/set_dc_current should raise RuntimeError
    with pytest.raises(RuntimeError):
        vi.take_reading()

    with pytest.raises(RuntimeError):
        vi.set_dc_current(1e-6)

    # Initiate
    vi.initiate_measurement(
        current=2e-6,
        n_readings=10,
        voltmeter_range_V=0.1,
        compliance_V=2.0,
        delay_s=0.001,
        compliance_abort=True
    )
    assert source._mode == "DC"
    assert source.get_current() == pytest.approx(2e-6)
    assert source.get_compliance() == pytest.approx(2.0)
    assert meter.get_range() == pytest.approx(0.1)

    # Take reading
    data = vi.take_reading()
    assert len(data["voltage_V_array"]) == 10
    assert len(data["current_A_array"]) == 10
    assert data["n_valid"] == 10
    assert all(c == pytest.approx(2e-6) for c in data["current_A_array"])
    assert data["current_A"] == pytest.approx(2e-6)
    assert data["current_A_error"] == pytest.approx(0.0)

    # Test reading-loop setter
    vi.set_dc_current(5e-6)
    assert source.get_current() == pytest.approx(5e-6)
    data = vi.take_reading()
    assert all(c == pytest.approx(5e-6) for c in data["current_A_array"])

    # Test standby
    vi.standby()
    assert source.get_current() == pytest.approx(0.0)
    with pytest.raises(RuntimeError):
        vi.take_reading()


def test_dc_mode_initiate_resets_source_before_compliance():
    """initiate_measurement() must force a known-idle source before arming.

    Live commissioning against a real 6221 (2026-07-22) hit -221 "Settings
    conflict" on the very first command of a second run of the same DC-mode
    loop, even though the prior run's standby() had already zeroed the
    source — initiate_measurement() set compliance first, with nothing of
    its own to abort a state standby() might have left armed. Pins the fix:
    a reset-to-zero call (which drives the real driver's :SOUR:SWE:ABORt)
    must land before compliance is (re)programmed on every initiate_measurement()
    call, not just relying on the previous run's standby() to have done it.
    """
    from cryosoft.virtual_instruments.measurement.measurement_dc_mode import DCModeMeasurementVI

    class RecordingSource(SimKeithley6221):
        def __init__(self, resource_string: str) -> None:
            super().__init__(resource_string)
            self.calls: list[str] = []

        def set_current(self, current: float) -> None:
            self.calls.append(f"set_current({current!r})")
            super().set_current(current)

        def set_compliance(self, compliance_v: float) -> None:
            self.calls.append(f"set_compliance({compliance_v!r})")
            super().set_compliance(compliance_v)

    source = RecordingSource("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DCModeMeasurementVI({"source": source, "meter": meter})

    vi.initiate_measurement(current=2e-6, n_readings=1, compliance_V=2.0)

    # A reset-to-zero must precede compliance being (re)armed, then the real
    # target current is set last.
    reset_idx = source.calls.index("set_current(0.0)")
    compliance_idx = source.calls.index("set_compliance(2.0)")
    target_idx = source.calls.index("set_current(2e-06)")
    assert reset_idx < compliance_idx < target_idx


def test_dc_mode_measurement_compliance_abort():
    import math
    from cryosoft.virtual_instruments.measurement.measurement_dc_mode import DCModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DCModeMeasurementVI({"source": source, "meter": meter})

    # Trigger compliance abort. Since paired meter simulates 1500 Ohm load,
    # sourcing 1 mA (1e-3 A) yields 1.5 V. With compliance at 1.0 V, it will
    # trigger compliance abort.
    vi.initiate_measurement(
        current=1e-3,
        n_readings=5,
        voltmeter_range_V=1.0,
        compliance_V=1.0,
        delay_s=0.001,
        compliance_abort=True
    )

    data = vi.take_reading()
    # It should abort at the first reading since compliance is checked before the read
    assert data["n_valid"] == 0
    assert all(math.isnan(v) for v in data["voltage_V_array"])
    assert all(math.isnan(c) for c in data["current_A_array"])
    # Zero valid samples -> mean/error is NaN, not computed from the NaN pad.
    assert math.isnan(data["voltage_V"])
    assert math.isnan(data["voltage_V_error"])


def test_dc_mode_read_now_bench_test():
    """read_now() is the front-panel bench-test hook for manual readings.

    Unlike take_reading() (Procedure-only), read_now() is a @control the GUI
    can click, and caches its result into the last_voltage_V /
    last_mean_voltage_V / last_n_valid @monitored fields so an operator can
    see what the Keithley returned without running a full procedure.
    """
    from cryosoft.virtual_instruments.measurement.measurement_dc_mode import DCModeMeasurementVI

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DCModeMeasurementVI({"source": source, "meter": meter})

    # Before any read_now(), the monitored fields report None ("—" in the GUI).
    assert vi.last_voltage_V() is None
    assert vi.last_mean_voltage_V() is None
    assert vi.last_n_valid() is None

    # read_now() before initiate_measurement() must raise, same as take_reading().
    with pytest.raises(RuntimeError):
        vi.read_now()

    vi.initiate_measurement(
        current=2e-6,
        n_readings=10,
        voltmeter_range_V=0.1,
        compliance_V=2.0,
        delay_s=0.001,
        compliance_abort=True,
    )

    vi.read_now()
    assert vi.last_n_valid() == 10
    assert isinstance(vi.last_voltage_V(), float)
    assert isinstance(vi.last_mean_voltage_V(), float)
    # Sim meter noise is tiny (1e-8 std) relative to the ~mV-scale reading, so
    # the last sample and the 10-sample mean should sit close together.
    assert vi.last_voltage_V() == pytest.approx(vi.last_mean_voltage_V(), abs=1e-6)

    # read_now() is a @control (GUI-discoverable) and takes no parameters.
    assert getattr(vi.read_now, "_is_control", False) is True
    assert getattr(vi.read_now, "_control_params", {}) == {}

    # The last_* fields are @monitored (polled + displayed every tick).
    assert getattr(vi.last_voltage_V, "_is_monitored", False) is True
    assert getattr(vi.last_mean_voltage_V, "_is_monitored", False) is True
    assert getattr(vi.last_n_valid, "_is_monitored", False) is True


# 16. Shared-instrument mode discipline (see GLOSSARY.md and
#     virtual_instruments/measurement/README.md): DCSeparateMeasurementVI and
#     DeltaModeMeasurementVI can be wired to the same physical Keithley 6221
#     (devices.yaml's real_drivers.keithley_6221), so a VI must never assume
#     the instrument was left in a compatible mode by whichever measurement
#     method ran previously.
def test_dc_separate_initiate_recovers_from_stale_delta_arm():
    """DC-separate VI's initiate() must not depend on a prior standby().

    Arms delta mode on a shared 6221 WITHOUT calling standby() (the Orchestrator
    normally would, but this proves DC-separate is safe even if it weren't —
    the shared-instrument mode discipline standard). initiate() must still
    leave the instrument correctly in plain DC mode at the requested current.
    """
    from cryosoft.virtual_instruments.measurement.dc_separate_measurement import (
        DCSeparateMeasurementVI,
    )
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import (
        DeltaModeMeasurementVI,
    )

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter

    delta_vi = DeltaModeMeasurementVI({"source": source, "meter": meter})
    delta_vi.initiate_measurement(current=1e-6, n_readings=5)
    assert source._mode == "DELTA"

    dc_vi = DCSeparateMeasurementVI({"source": source, "meter": meter})
    dc_vi.initiate_measurement(current_A=5e-6)

    assert source._mode == "DC"
    assert source.get_current() == pytest.approx(5e-6)


def test_dc_mode_initiate_recovers_from_stale_delta_arm():
    """DCModeMeasurementVI's initiate() must not depend on a prior standby().

    Mirrors test_dc_separate_initiate_recovers_from_stale_delta_arm above,
    but for the VI actually wired to real hardware in devices.yaml
    (keithley_dc_mode: source=keithley_6220, meter=keithley_2182a on its own
    GPIB address). Arms delta mode on the shared 6221 WITHOUT calling
    standby() first — the Orchestrator normally would, and closeEvent() now
    also forces an abort on window close, but this proves DC mode's
    initiate_measurement() is safe even if BOTH of those were skipped (e.g.
    the app was killed rather than closed cleanly). See the live-hardware
    finding this session (2026-07-22): -221 "Settings conflict" on a real
    6221 traced to exactly this — a run left un-aborted, then a fresh
    initiate_measurement() assuming a clean starting state.
    """
    from cryosoft.virtual_instruments.measurement.measurement_dc_mode import DCModeMeasurementVI
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import (
        DeltaModeMeasurementVI,
    )

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter

    delta_vi = DeltaModeMeasurementVI({"source": source, "meter": source})
    delta_vi.initiate_measurement(current=1e-6, n_readings=5)
    assert source._mode == "DELTA"

    dc_vi = DCModeMeasurementVI({"source": source, "meter": meter})
    dc_vi.initiate_measurement(current=5e-6, n_readings=3, compliance_V=1.0)

    assert source._mode == "DC"
    assert source.get_current() == pytest.approx(5e-6)

    data = dc_vi.take_reading()
    assert data["n_valid"] == 3


def test_delta_mode_initiate_recovers_from_prior_dc_current():
    """Delta-mode VI's initiate() must not depend on the instrument idling at 0 A.

    The reverse handoff: DC-separate leaves a nonzero current set, then
    delta mode is armed directly on the same shared 6221 without an
    intervening standby(). configure_and_start_delta() already always leads
    with :SOUR:SWE:ABOR, so this should already hold — this test pins it.
    """
    from cryosoft.virtual_instruments.measurement.dc_separate_measurement import (
        DCSeparateMeasurementVI,
    )
    from cryosoft.virtual_instruments.measurement.measurement_delta_mode import (
        DeltaModeMeasurementVI,
    )

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter

    dc_vi = DCSeparateMeasurementVI({"source": source, "meter": meter})
    dc_vi.initiate_measurement(current_A=5e-6)
    assert source._mode == "DC"

    delta_vi = DeltaModeMeasurementVI({"source": source, "meter": meter})
    delta_vi.initiate_measurement(current=1e-6, n_readings=5, delay_s=0.001)
    assert source._mode == "DELTA"

    data = delta_vi.take_reading()
    assert data["n_valid"] == 5


# 4b. Ramp-introspection standard: ramp_setpoint() — the NEXT setpoint
def test_rampable_default_ramp_setpoint_is_none():
    """RampableVI's optional hook has a safe default, like every other one."""
    from cryosoft.virtual_instruments.rampable import RampableVI

    class BareRampable(RampableVI):
        def start_ramp(self, target): ...
        def advance_ramp(self): ...
        def ramp_status(self): return "IDLE"
        def stop_ramp(self): ...

    assert BareRampable().ramp_setpoint() is None


def test_magnet_ramp_setpoint_is_the_segment_boundary_not_the_target():
    """Mid-ramp the magnet drives to the next segment boundary, not the end setpoint.

    This distinction is the whole reason the tracker shows two numbers: the
    PSU is heading for 2 T right now while the ramp finishes at 3 T.
    """
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import (
        SuperconductingMagnetVI,
    )

    driver = SimOxfordIPS120("SIM")
    segments = [
        {"max_current_A": 20.0, "rate_A_per_min": 600.0},
        {"max_current_A": float("inf"), "rate_A_per_min": 100.0},
    ]
    vi = SuperconductingMagnetVI(
        {"main": driver},
        default_ramp_rate=5.0,
        amperes_per_tesla=10.0,
        ramp_segments=segments,
    )

    assert vi.ramp_setpoint() is None  # idle: nothing commanded

    vi.start_ramp(3.0)
    assert vi.ramp_setpoint() == pytest.approx(2.0)  # the 20 A boundary
    assert vi.ramp_target() == pytest.approx(3.0)

    for _ in range(100):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()
        if vi.ramp_status() == "TARGET_REACHED":
            break
    # Past the boundary the setpoint becomes the end setpoint itself.
    assert vi.ramp_setpoint() == pytest.approx(3.0)


def test_magnet_stop_ramp_clears_the_setpoint():
    """Implementations MUST clear ramp_setpoint() in stop_ramp(), like the target."""
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import (
        SuperconductingMagnetVI,
    )

    driver = SimOxfordIPS120("SIM")
    vi = SuperconductingMagnetVI({"main": driver}, amperes_per_tesla=10.0)
    vi.start_ramp(1.0)
    assert vi.ramp_setpoint() is not None

    vi.stop_ramp()
    assert vi.ramp_setpoint() is None
    assert vi.ramp_target() is None


def test_temperature_ramp_setpoint_trails_the_target():
    """A time-based ramp's commanded setpoint walks from start toward target."""
    from cryosoft.virtual_instruments.temperature.sample_temperature_controller import (
        SampleTemperatureControllerVI,
    )

    driver = SimOxfordITC503("SIM")
    vi = SampleTemperatureControllerVI(
        {"main": driver}, default_ramp_rate=1.0, tolerance=2.0
    )
    start_T = driver.get_temperature()
    vi.start_ramp(start_T - 100.0)

    setpoint = vi.ramp_setpoint()
    assert setpoint is not None
    assert vi.ramp_target() == pytest.approx(start_T - 100.0)
    # A 1 K/min ramp has barely moved on its first tick, so the commanded
    # setpoint is still near the start and nowhere near the end setpoint.
    assert setpoint == pytest.approx(start_T, abs=1.0)
    assert setpoint != vi.ramp_target()

    vi.stop_ramp()
    assert vi.ramp_setpoint() is None


def test_rotator_ramp_setpoint_equals_its_target():
    """A one-shot generator's next setpoint IS the end setpoint — also valid."""
    from cryosoft.drivers.sim_rotator import SimRotator
    from cryosoft.virtual_instruments.rotator.rotator import RotatorVI

    driver = SimRotator("SIM")
    vi = RotatorVI({"main": driver}, max_angle_deg=360.0, min_angle_deg=-360.0)
    vi.start_ramp(45.0)
    assert vi.ramp_setpoint() == pytest.approx(45.0)
    assert vi.ramp_target() == pytest.approx(45.0)

    vi.stop_ramp()
    assert vi.ramp_setpoint() is None


# 17. Standby-provenance standard: standby_status() (see BaseVirtualInstrument's
#     __init_subclass__ wrap of standby()/start_ramp()/stop_ramp())
def test_standby_status_fresh_rampable_vi_is_away():
    """Nothing has commanded standby() yet on a freshly built rampable VI."""
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import (
        SuperconductingMagnetVI,
    )

    driver = SimOxfordIPS120("SIM")
    vi = SuperconductingMagnetVI({"main": driver}, amperes_per_tesla=10.0)

    assert vi.standby_status() == "away"


def test_standby_status_lifecycle_converging_then_reached():
    """standby() -> converging while the ramp-to-zero is still running, reached once it finishes."""
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import (
        SuperconductingMagnetVI,
    )

    driver = SimOxfordIPS120("SIM")
    vi = SuperconductingMagnetVI(
        {"main": driver}, default_ramp_rate=1200.0, amperes_per_tesla=10.0
    )

    # Move away from the safe idle state first, so standby() has ground to cover.
    vi.start_ramp(1.0)
    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()
    assert vi.standby_status() == "away"  # start_ramp, not standby, was last commanded

    vi.standby()
    assert vi.standby_status() == "converging"

    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()
    assert vi.ramp_status() == "TARGET_REACHED"
    assert vi.standby_status() == "reached"


def test_standby_status_start_ramp_after_standby_is_away_even_mid_ramp():
    """start_ramp() invalidates standby provenance even while ramp_status() still reports RAMPING.

    Load-bearing: a check based on magnet_state() alone (rather than the
    _standby_commanded flag) would still see the field converging on some
    setpoint here and misreport "converging".
    """
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import (
        SuperconductingMagnetVI,
    )

    driver = SimOxfordIPS120("SIM")
    vi = SuperconductingMagnetVI(
        {"main": driver}, default_ramp_rate=1200.0, amperes_per_tesla=10.0
    )

    vi.start_ramp(1.0)
    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()

    vi.standby()
    assert vi.standby_status() == "converging"

    vi.start_ramp(0.5)
    assert vi.ramp_status() == "RAMPING"
    assert vi.standby_status() == "away"


def test_standby_status_stop_ramp_after_standby_is_away():
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import (
        SuperconductingMagnetVI,
    )

    driver = SimOxfordIPS120("SIM")
    vi = SuperconductingMagnetVI(
        {"main": driver}, default_ramp_rate=1200.0, amperes_per_tesla=10.0
    )

    vi.start_ramp(1.0)
    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()

    vi.standby()
    assert vi.standby_status() == "converging"

    vi.stop_ramp()
    assert vi.standby_status() == "away"


def test_standby_status_raising_standby_leaves_it_away():
    """A standby() that raises must not be mistaken for a converging/reached VI."""
    from cryosoft.virtual_instruments.base import BaseVirtualInstrument
    from cryosoft.virtual_instruments.rampable import RampableVI

    class RaisingStandbyRampable(BaseVirtualInstrument, RampableVI):
        vi_type = "mock"

        def __init__(self):
            super().__init__({})
            self._ramping = False

        def start_ramp(self, target):
            self._ramping = True

        def advance_ramp(self):
            pass

        def ramp_status(self):
            return "RAMPING" if self._ramping else "IDLE"

        def stop_ramp(self):
            self._ramping = False

        def standby(self):
            raise RuntimeError("standby failed")

    vi = RaisingStandbyRampable()
    with pytest.raises(RuntimeError):
        vi.standby()
    assert vi.standby_status() == "away"


def test_standby_status_non_rampable_vi_always_reached():
    """A measurement VI (or any non-RampableVI) has no intermediate state to converge through."""
    from cryosoft.virtual_instruments.measurement.measurement_dc_mode import (
        DCModeMeasurementVI,
    )

    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    vi = DCModeMeasurementVI({"source": source, "meter": meter})

    assert vi.standby_status() == "reached"
    vi.standby()
    assert vi.standby_status() == "reached"


def test_standby_status_inherited_standby_still_tracked():
    """A subclass that does NOT define its own standby() still gets correct provenance.

    Exercises the inherited-wrap path in __init_subclass__: only a directly
    defined standby()/start_ramp()/stop_ramp() is re-wrapped, so this
    subclass relies entirely on the wrap already applied to its parent.
    """
    from cryosoft.virtual_instruments.magnet.superconducting_magnet import (
        SuperconductingMagnetVI,
    )

    class UnmodifiedMagnetVI(SuperconductingMagnetVI):
        """Adds nothing; standby()/start_ramp()/stop_ramp() are all inherited."""

    driver = SimOxfordIPS120("SIM")
    vi = UnmodifiedMagnetVI(
        {"main": driver}, default_ramp_rate=1200.0, amperes_per_tesla=10.0
    )

    assert vi.standby_status() == "away"

    vi.start_ramp(1.0)
    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()

    vi.standby()
    assert vi.standby_status() == "converging"

    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()
    assert vi.standby_status() == "reached"
