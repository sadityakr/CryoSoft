# Quick Reference Card - Cryostat Control System

## File Locations

| Component | Location | Example |
|-----------|----------|---------|
| Mock Drivers | `cryostat/drivers/mock/` | `mock_itc503.py` |
| Real Drivers | `cryostat/drivers/real/` | `itc503.py` |
| Base Interfaces | `cryostat/drivers/base/` | `temperature_base.py` |
| Action Classes | `cryostat/actions/` | `temperature_actions.py` |
| Configurations | `cryostat/config/` | `cryostat_mock.yaml` |
| Tests | `cryostat/tests/` | `test_mock_drivers.py` |

---

## Adding a New Device (5 Steps)

### 1. Create Driver

```python
# cryostat/drivers/mock/mock_mydevice.py
from pymeasure.adapters import FakeAdapter
from pymeasure.instruments import Instrument
from ..base.temperature_base import TemperatureControllerBase

class MockMyDevice(Instrument, TemperatureControllerBase):
    def __init__(self, adapter=None, name="Mock MyDevice", **kwargs):
        if adapter is None:
            adapter = FakeAdapter()
        super().__init__(adapter, name, includeSCPI=False, **kwargs)
        self._property = 0.0

    @property
    def property_name(self):
        return self._property

    @property_name.setter
    def property_name(self, value):
        self._property = value

    # Implement all required methods from base class
```

### 2. Register in Factory

```python
# cryostat/config/factory.py
DRIVER_MAP = {
    'mock_mydevice': ('cryostat.drivers.mock.mock_mydevice', 'MockMyDevice'),
}

driver_specific_params = {
    'mock_mydevice': {'custom_param1', 'custom_param2'},
}
```

### 3. Add to Config

```yaml
# cryostat/config/cryostat_mock.yaml
cryostat:
  devices:
    my_device:
      type: temperature
      driver: mock_mydevice
      resource: "mock"
      actions: TemperatureActions
      description: "My Device Description"
```

### 4. Create Tests

```python
# cryostat/tests/test_mydevice.py
import pytest
from cryostat.drivers.mock.mock_mydevice import MockMyDevice

class TestMockMyDevice:
    @pytest.fixture
    def device(self):
        return MockMyDevice()

    def test_initialization(self, device):
        assert device.name == "Mock MyDevice"
```

### 5. Run Tests

```bash
pytest cryostat/tests/test_mydevice.py -v
```

---

## Base Interface Requirements

### TemperatureControllerBase

**Required Properties:**
- `temperature_1`, `temperature_2`, `temperature_3` (read-only)
- `temperature_setpoint` (read/write)
- `control_mode` (read/write): "LL", "RL", "LU", "RU"
- `heater` (read/write): 0-99.9%
- `heater_gas_mode` (read/write): "MANUAL", "AM", "MA", "AUTO"

**Required Methods:**
- `wait_for_temperature(error, timeout, check_interval, **kwargs) -> bool`

### MagnetBase

**Required Properties:**
- `field`, `demand_field`, `persistent_field` (read-only)
- `current_measured`, `demand_current` (read-only)
- `field_setpoint`, `sweep_rate` (read/write)
- `control_mode`, `activity` (read/write)
- `sweep_status` (read-only)
- `switch_heater_enabled` (read/write)

**Required Methods:**
- `enable_control()`
- `disable_control()`
- `set_field(field, sweep_rate, persistent_mode_control)`
- `wait_for_idle(delay, max_wait_time, should_stop)`

### LevelMeterBase

**Required Properties:**
- `helium_level`, `nitrogen_level` (read-only)
- `active_channel` (read/write): 1 or 2
- `measurement_mode` (read/write): "continuous", "sample", "off"
- `control_mode` (read/write): "LL", "RL", "LU", "RU"
- `num_channels` (read-only): 1 or 2

**Required Methods:**
- `calibrate_channel(channel) -> bool`
- `set_probe_length(channel, length_cm) -> bool`
- `measure_level(channel=None) -> float`
- `measure_all_channels() -> dict`

---

## Action Class Template

```python
from .base import ITemperatureActions
import logging

log = logging.getLogger(__name__)

class TemperatureActions(ITemperatureActions):
    def __init__(self, driver):
        self.driver = driver

    def initiate(self):
        """Initialize device."""
        log.info("Initiating device")
        self.driver.control_mode = "RU"
        return True

    def ramp_to_temperature(self, target, rate=1.0):
        """Ramp to target temperature."""
        log.info(f"Ramping to {target}K at {rate}K/min")
        self.driver.temperature_setpoint = target
        self.driver.wait_for_temperature(error=0.1)
        return self.driver.temperature_1

    def get_status(self):
        """Get device status."""
        return {
            'temperature_1': self.driver.temperature_1,
            'setpoint': self.driver.temperature_setpoint,
            'heater': self.driver.heater,
        }
```

---

## Configuration File Template

```yaml
cryostat:
  devices:
    device_name:
      type: <temperature|magnet|level_meter>
      driver: <driver_name>
      resource: "<GPIB::24::INSTR|mock>"
      actions: <ActionClass>
      description: "Description"
      # Optional device-specific params
      field_range: [-7, 7]
      num_channels: 2

logging:
  level: INFO
  console: true
  file: "cryostat.log"

system:
  lock_timeout: 1.0
```

---

## Common Commands

### Testing

```bash
# All tests
pytest cryostat/tests/ -v

# Specific file
pytest cryostat/tests/test_mock_drivers.py -v

# Specific test
pytest cryostat/tests/test_mock_drivers.py::TestClass::test_method -v

# With coverage
pytest cryostat/tests/ --cov=cryostat --cov-report=html

# Stop on first failure
pytest cryostat/tests/ -x
```

### Running Examples

```bash
# Basic usage
python -m cryostat.examples.basic_usage

# From Python REPL
python
>>> from cryostat.core.cryostat import Cryostat
>>> cryo = Cryostat('cryostat/config/cryostat_mock.yaml')
>>> device = cryo["vti_temp_controller"]
>>> print(device.temperature_1)
```

---

## Logging Levels

```python
import logging
log = logging.getLogger(__name__)

log.debug("Detailed diagnostic info")      # Development only
log.info("Major state changes")            # Normal operations
log.warning("Unexpected but recoverable")  # Issues to note
log.error("Serious problems")              # Operation failures
```

---

## Property Patterns

### Read-Only Property
```python
@property
def temperature(self):
    """Current temperature in Kelvin."""
    return self._temperature
```

### Read-Write Property
```python
@property
def setpoint(self):
    """Temperature setpoint in Kelvin."""
    return self._setpoint

@setpoint.setter
def setpoint(self, value):
    """Set temperature setpoint."""
    if not 0 <= value <= 400:
        raise ValueError(f"Invalid setpoint: {value}K")
    self._setpoint = value
    log.debug(f"Setpoint set to {value}K")
```

---

## Error Handling

```python
# Raise specific exceptions
if value < 0:
    raise ValueError(f"Value must be positive, got {value}")

# Catch specific exceptions
try:
    result = self.driver.operation()
except CommunicationError as e:
    log.error(f"Communication failed: {e}")
    raise
except TimeoutError:
    log.warning("Operation timed out, retrying...")
    # Retry logic
```

---

## Thread Safety

The system handles thread safety automatically. No locks needed in your code:

```python
# This is automatically thread-safe
device = cryo["magnet1"]
device.field_setpoint = 0.5  # Locked automatically
field = device.field         # Locked automatically
```

---

## Useful Functions

### Wait Pattern
```python
import time

def wait_for_condition(self, timeout=60):
    start = time.time()
    while True:
        if self.check_condition():
            return True
        if time.time() - start > timeout:
            raise TimeoutError("Condition not met")
        time.sleep(0.5)
```

### Validation Pattern
```python
def validate_parameter(self, value, min_val, max_val, name):
    if not min_val <= value <= max_val:
        raise ValueError(
            f"{name} must be between {min_val} and {max_val}, "
            f"got {value}"
        )
```

---

## Debugging Tips

```python
# Enable debug logging
import logging
logging.basicConfig(level=logging.DEBUG)

# Test driver directly
from cryostat.drivers.mock.mock_itc503 import MockITC503
itc = MockITC503()
print(itc.temperature_1)

# Test with actions
from cryostat.actions.temperature_actions import TemperatureActions
actions = TemperatureActions(itc)
actions.initiate()

# Check configuration
from cryostat.config.loader import load_config
config = load_config('cryostat/config/cryostat_mock.yaml')
print(config)
```

---

## Common Errors & Solutions

| Error | Solution |
|-------|----------|
| `Unknown driver: 'name'` | Add to DRIVER_MAP in factory.py |
| `Module not found` | Check import paths, add __init__.py |
| `AttributeError: no attribute 'x'` | Implement missing property/method |
| `VISA implementation not found` | Install pyvisa: `pip install pyvisa pyvisa-py` |
| Tests timeout | Reduce simulation delays in mock drivers |

---

## Resources

- **Full Guide**: `DEVELOPER_GUIDE.md`
- **Main README**: `README.md`
- **Examples**: `cryostat/examples/`
- **PyMeasure Docs**: https://pymeasure.readthedocs.io/

---

**Quick Checklist for New Device:**

- [ ] Driver file created
- [ ] Registered in factory.py
- [ ] Added to config YAML
- [ ] Tests created
- [ ] All tests pass
- [ ] Documentation updated

---

**Need Help?** See `DEVELOPER_GUIDE.md` for detailed instructions!
