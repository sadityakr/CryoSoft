# ---
# description: |
#   Complete test suite for Layer 1 Virtual Instruments.
# entry_point: pytest tests/test_l1_virtual_instruments.py -v
# last_updated: 2026-04-06
# ---

import pytest
import time
from cryosoft.core.exceptions import CryoSoftCommunicationError

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
    assert vi.get_field() == pytest.approx(1.0, abs=0.1)

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
    assert vi.get_field() == pytest.approx(3.0, abs=0.1)

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

    vi.initiate(current=1e-6, n_readings=10, delay_s=0.001)

    data = vi.take_reading()
    assert "voltage_V" in data
    assert "current_A" in data
    assert len(data["voltage_V"]) == 10
    assert len(data["current_A"]) == 10
    assert data["n_valid"] == 10


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

    vi.initiate(
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
    vi.initiate(current=1e-6, n_readings=10, delay_s=0.001)
    data = vi.take_reading()

    assert len(data["voltage_V"]) == 10
    assert len(data["current_A"]) == 10
    assert data["n_valid"] == 3
    # First 3 are real; the padded tail is NaN in both arrays.
    assert all(not math.isnan(v) for v in data["voltage_V"][:3])
    assert all(math.isnan(v) for v in data["voltage_V"][3:])
    assert all(math.isnan(c) for c in data["current_A"][3:])
