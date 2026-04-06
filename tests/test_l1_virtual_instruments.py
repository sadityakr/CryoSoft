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
    from cryosoft.virtual_instruments.magnet_ips120 import IPS120MagnetVI
    
    driver = SimOxfordIPS120("SIM")
    vi = IPS120MagnetVI({"main": driver}, default_ramp_rate=1200.0, amperes_per_tesla=10.0)
    
    vi.start_ramp(1.0)
    assert vi.ramp_status() == "RAMPING"
    
    for _ in range(50):
        vi.advance_ramp()
        driver._last_update = time.time() - 0.5
        driver._update_simulation()
    
    assert vi.ramp_status() in ("TARGET_REACHED", "IDLE")
    assert vi.get_field() == pytest.approx(1.0, abs=0.1)

def test_magnet_vi_ramp_segments():
    from cryosoft.virtual_instruments.magnet_ips120 import IPS120MagnetVI
    
    driver = SimOxfordIPS120("SIM")
    segments = [
        {"max_current_A": 20.0, "rate_A_per_min": 600.0},
        {"max_current_A": float('inf'), "rate_A_per_min": 100.0}
    ]
    vi = IPS120MagnetVI({"main": driver}, default_ramp_rate=5.0, amperes_per_tesla=10.0, ramp_segments=segments)
    
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
    from cryosoft.virtual_instruments.magnet_ips120 import IPS120MagnetVI
    
    driver = SimOxfordIPS120("SIM")
    vi = IPS120MagnetVI({"main": driver}, max_current=50.0, min_current=-50.0)
    
    vi.start_ramp(6.0)
    for _ in range(10):
        vi.advance_ramp()
    
    assert driver.get_current_setpoint() <= 50.0

# 10. Temperature VI tests
def test_temperature_vi_ramp():
    from cryosoft.virtual_instruments.temperature_itc503 import ITC503TemperatureVI
    driver = SimOxfordITC503("SIM")
    
    vi = ITC503TemperatureVI({"main": driver}, default_ramp_rate=6000.0, tolerance=2.0)
    vi.start_ramp(200.0)
    
    for _ in range(20):
        time.sleep(0.01)
        vi.advance_ramp()
        driver._last_update = time.time() - 1.0
        driver._update_simulation()
        
    assert vi.ramp_status() in ("RAMPING", "TARGET_REACHED")

# 13. Level meter tests
def test_level_vi_buffer():
    from cryosoft.virtual_instruments.level_ilm200 import ILM200LevelVI
    driver = SimOxfordILM200("SIM")
    vi = ILM200LevelVI({"main": driver}, low_threshold=20.0, buffer_size=3)
    
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
    from cryosoft.virtual_instruments.measurement_delta_mode import DeltaModeMeasurementVI
    
    source = SimKeithley6221("SIM")
    meter = SimKeithley2182A("SIM")
    source._paired_meter = meter
    
    vi = DeltaModeMeasurementVI({"source": source, "meter": meter})
    
    with pytest.raises(RuntimeError):
        vi.read_datapoint()
        
    vi.configure("delta_mode", current=1e-6, n_readings=10, delay=0.001)
    
    data = vi.read_datapoint()
    assert "voltage_V" in data
    assert "current_A" in data
    assert len(data["voltage_V"]) == 10
    assert len(data["current_A"]) == 10
