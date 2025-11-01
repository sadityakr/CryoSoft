# Developer's Guide to Extending the Cryostat Control System

**Version:** 1.0
**Last Updated:** 2025-11-01

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Writing Drivers](#writing-drivers)
3. [Writing Action Classes](#writing-action-classes)
4. [Creating Configuration Files](#creating-configuration-files)
5. [Registering New Components](#registering-new-components)
6. [Testing Your Code](#testing-your-code)
7. [Best Practices](#best-practices)
8. [Common Patterns](#common-patterns)
9. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

The cryostat system uses a **three-layer architecture**:

```
Layer 3: Cryostat (Logical Instrument)
    ↓ manages
Layer 2: Actions (High-Level Operations)
    ↓ uses
Layer 1: Drivers (Device Communication)
```

### When to Add Each Component

| Component | When to Add |
|-----------|-------------|
| **Driver** | Adding support for a new hardware device |
| **Action Class** | Adding high-level operations for a device type |
| **Config Entry** | Making a device available in the system |

---

## Writing Drivers

Drivers handle low-level communication with hardware. They inherit from PyMeasure's `Instrument` and implement device-specific base interfaces.

### Step 1: Choose Your Driver Type

Determine which base interface your driver should implement:

- **TemperatureControllerBase** - For temperature controllers (ITC503, Lakeshore 336, etc.)
- **MagnetBase** - For magnet power supplies (IPS120, etc.)
- **LevelMeterBase** - For level meters (ILM, etc.)

### Step 2: Create Driver File Structure

**Location**:
- Mock drivers: `cryostat/drivers/mock/`
- Real drivers: `cryostat/drivers/real/` (or organize by manufacturer)

**File naming convention**: `<device_model>.py` (e.g., `itc503.py`, `lakeshore336.py`)

### Step 3: Implement the Driver

#### Template for Mock Driver

```python
"""
Mock <Device Name> <Device Type>
=================================

Mock implementation of the <Manufacturer> <Model> <Device Type>.

Example:
    device = Mock<ClassName>()
    device.property_name = value
    result = device.method_name()
"""

from pymeasure.adapters import FakeAdapter
from pymeasure.instruments import Instrument
from ..base.<type>_base import <TypeBase>
import logging
import time

log = logging.getLogger(__name__)


class Mock<ClassName>(Instrument, <TypeBase>):
    """Mock <Device Name>.

    Simulates a real <Device Name> with realistic behavior including:
    - <Feature 1>
    - <Feature 2>
    - <Feature 3>
    """

    def __init__(self, adapter=None, name="Mock <Device>", **kwargs):
        """Initialize mock device.

        Args:
            adapter: Communication adapter (uses FakeAdapter if None)
            name: Device name
            **kwargs: Additional device-specific parameters
        """
        if adapter is None:
            adapter = FakeAdapter()

        super().__init__(adapter, name, includeSCPI=False, **kwargs)

        # Internal state
        self._property1 = 0.0
        self._property2 = "value"

        log.info(f"{name} initialized")

    # ==================== Required Properties ====================

    @property
    def property1(self):
        """Description of property1."""
        return self._property1

    @property1.setter
    def property1(self, value):
        """Set property1."""
        self._property1 = value
        log.debug(f"Property1 set to {value}")

    # ==================== Required Methods ====================

    def required_method(self, param1, param2=None):
        """Description of required method.

        Args:
            param1: Description
            param2: Optional description

        Returns:
            Description of return value
        """
        log.info(f"Calling required_method({param1}, {param2})")
        # Implementation
        return result

    # ==================== Device Info ====================

    @property
    def version(self):
        """Device version string."""
        return "<Device> Mock v1.0"

    def __repr__(self):
        return f"<Mock<ClassName>(property1={self._property1})>"
```

#### Template for Real Driver

```python
"""
<Manufacturer> <Model> <Device Type>
====================================

Driver for the <Manufacturer> <Model> <Device Type>.

Communication: VISA/GPIB/Serial
Protocol: <Protocol description>

Example:
    device = <ClassName>("GPIB::24::INSTR")
    device.property_name = value
    result = device.method_name()
"""

from pymeasure.instruments import Instrument
from ..base.<type>_base import <TypeBase>
import logging

log = logging.getLogger(__name__)


class <ClassName>(Instrument, <TypeBase>):
    """<Manufacturer> <Model> <Device Type>.

    Implements control for:
    - <Feature 1>
    - <Feature 2>
    - <Feature 3>
    """

    def __init__(self, adapter, name="<Device Name>", **kwargs):
        """Initialize device.

        Args:
            adapter: VISA resource string or adapter instance
            name: Device name
            **kwargs: Additional parameters (timeout, baud_rate, etc.)
        """
        super().__init__(
            adapter,
            name,
            includeSCPI=False,
            **kwargs
        )

        log.info(f"{name} initialized on {adapter}")

    # ==================== Properties Using PyMeasure Descriptors ====================

    property1 = Instrument.control(
        "COMMAND?", "COMMAND %s",
        """Description of property1.""",
        validator=strict_range,
        values=[min_val, max_val],
        cast=float
    )

    property2 = Instrument.measurement(
        "QUERY?",
        """Description of property2 (read-only).""",
        cast=float
    )

    # ==================== Required Methods ====================

    def required_method(self, param1, param2=None):
        """Description of required method.

        Args:
            param1: Description
            param2: Optional description

        Returns:
            Description of return value

        Raises:
            DeviceError: If operation fails
        """
        log.info(f"Calling required_method({param1}, {param2})")

        # Send command
        self.write(f"COMMAND {param1}")

        # Wait for completion
        self.wait_for_completion()

        # Read result
        result = self.ask("RESULT?")
        return float(result)

    def wait_for_completion(self, timeout=60):
        """Wait for device to complete operation.

        Args:
            timeout: Maximum wait time in seconds

        Raises:
            TimeoutError: If timeout exceeded
        """
        import time
        start_time = time.time()

        while True:
            status = self.ask("STATUS?")
            if status == "READY":
                return

            if time.time() - start_time > timeout:
                raise TimeoutError("Device operation timed out")

            time.sleep(0.5)
```

### Step 4: Implement All Required Interface Methods

Check the base class to see which methods and properties are required:

**For TemperatureControllerBase:**
- Properties: `temperature_1`, `temperature_2`, `temperature_3`, `temperature_setpoint`, `control_mode`, `heater`, `heater_gas_mode`
- Methods: `wait_for_temperature(error, timeout, check_interval, **kwargs)`

**For MagnetBase:**
- Properties: `field`, `demand_field`, `persistent_field`, `current_measured`, `demand_current`, `field_setpoint`, `current_setpoint`, `sweep_rate`, `control_mode`, `activity`, `sweep_status`, `switch_heater_enabled`
- Methods: `enable_control()`, `disable_control()`, `set_field(field, sweep_rate, persistent_mode_control)`, `wait_for_idle(delay, max_wait_time, should_stop)`

**For LevelMeterBase:**
- Properties: `helium_level`, `nitrogen_level`, `active_channel`, `measurement_mode`, `control_mode`, `num_channels`
- Methods: `calibrate_channel(channel)`, `set_probe_length(channel, length_cm)`, `measure_level(channel)`, `measure_all_channels()`

### Step 5: Add Device-Specific Features

Beyond the required interface, add device-specific features as needed:

```python
# Device-specific properties
@property
def custom_feature(self):
    """Device-specific feature description."""
    return self._custom_feature

# Device-specific methods
def special_operation(self, param):
    """Device-specific operation."""
    log.info(f"Performing special operation with {param}")
    # Implementation
```

---

## Writing Action Classes

Action classes provide high-level, intelligent operations that work across different driver implementations.

### Step 1: Choose Action Interface

Determine which action interface to implement:

- **ITemperatureActions** - For temperature control operations
- **IMagnetActions** - For magnet control operations
- **ILevelMeterActions** - For level monitoring operations

### Step 2: Create Action File

**Location**: `cryostat/actions/`

**File naming**: `<device_type>_actions.py` (e.g., `temperature_actions.py`)

### Step 3: Implement Action Class

#### Template

```python
"""
<Device Type> Action Layer
==========================

High-level operations for <device type> devices.

Works with any driver implementing <DriverBase>.
"""

from .base import I<Type>Actions
import logging
import time

log = logging.getLogger(__name__)


class <Type>Actions(I<Type>Actions):
    """High-level actions for <device type> control.

    This class provides intelligent, multi-step operations that work
    with any driver implementing <Type>ControllerBase.

    Features:
    - <Feature 1>
    - <Feature 2>
    - <Feature 3>
    """

    def __init__(self, driver):
        """Initialize action handler.

        Args:
            driver: Driver instance implementing <Type>ControllerBase
        """
        self.driver = driver
        log.debug(f"<Type>Actions initialized for {driver.name}")

    # ==================== Initialization ====================

    def initiate(self):
        """Initialize device for operation.

        Performs standard initialization sequence:
        1. Set control mode to remote
        2. Configure default parameters
        3. Verify device ready

        Returns:
            bool: True if successful
        """
        log.info(f"Initiating {self.driver.name}")

        # Step 1: Set control mode
        self.driver.control_mode = "RU"  # Remote & Unlocked
        log.debug("Control mode set to RU")

        # Step 2: Configure defaults
        # ... device-specific configuration ...

        log.info("Device initiated successfully")
        return True

    # ==================== High-Level Operations ====================

    def complex_operation(self, target, rate=1.0):
        """Perform complex multi-step operation.

        This method demonstrates a typical high-level action that:
        1. Validates parameters
        2. Performs pre-operation checks
        3. Executes the operation
        4. Verifies completion
        5. Returns meaningful results

        Args:
            target: Target value
            rate: Rate of change (units/min)

        Returns:
            float: Final achieved value

        Raises:
            ValueError: If parameters invalid
        """
        log.info(f"Starting complex operation: target={target}, rate={rate}")

        # Step 1: Validate parameters
        if target < 0 or target > 1000:
            raise ValueError(f"Invalid target: {target}")

        # Step 2: Pre-operation checks
        current = self.driver.current_value
        log.debug(f"Current value: {current}")

        # Step 3: Execute operation
        self.driver.setpoint = target

        # Step 4: Wait for completion
        log.info("Waiting for operation to complete...")
        self.driver.wait_for_completion(error=0.1, timeout=600)

        # Step 5: Verify and return
        final_value = self.driver.current_value
        log.info(f"Operation complete: achieved {final_value}")

        return final_value

    # ==================== Status ====================

    def get_status(self):
        """Get comprehensive device status.

        Returns:
            dict: Dictionary with all relevant status information
        """
        log.debug("Getting device status")

        status = {
            'property1': self.driver.property1,
            'property2': self.driver.property2,
            'property3': self.driver.property3,
            # ... more status fields ...
        }

        return status
```

### Design Patterns for Actions

#### 1. Initialization Pattern

```python
def initiate(self):
    """Standard initialization sequence."""
    log.info("Initiating device")

    # 1. Set control mode
    self.driver.control_mode = "RU"

    # 2. Configure parameters
    # ...

    # 3. Verify ready
    # ...

    return True
```

#### 2. Ramp-and-Wait Pattern

```python
def ramp_to_value(self, target, rate):
    """Ramp to target value and wait for stabilization."""
    log.info(f"Ramping to {target} at {rate}/min")

    # Set target
    self.driver.setpoint = target

    # Wait for completion
    self.driver.wait_for_completion(error=0.1)

    # Return achieved value
    return self.driver.current_value
```

#### 3. Status Collection Pattern

```python
def get_status(self):
    """Collect all relevant status information."""
    return {
        'value1': self.driver.property1,
        'value2': self.driver.property2,
        'timestamp': time.time()
    }
```

---

## Creating Configuration Files

Configuration files define which devices are available and how to access them.

### Configuration File Structure

**Location**: `cryostat/config/`

**Naming**: `cryostat_<environment>.yaml` (e.g., `cryostat_mock.yaml`, `cryostat_lab.yaml`)

### Basic Configuration Template

```yaml
# Cryostat Configuration File
# ===========================
# Environment: <Development/Production/Lab Name>
# Last Updated: YYYY-MM-DD

cryostat:
  devices:
    # Device Name (must be unique)
    device_identifier:
      # Required Fields
      type: <device_type>              # temperature, magnet, level_meter
      driver: <driver_name>             # Must match DRIVER_MAP in factory.py
      resource: "<resource_string>"     # VISA/GPIB/Serial resource or "mock"
      actions: <ActionClassName>        # Action class name (e.g., TemperatureActions)

      # Optional Fields
      description: "Human-readable description"

      # Driver-Specific Parameters
      field_range: [-7, 7]             # For magnets (Tesla)
      num_channels: 2                   # For multi-channel devices
      min_temperature: 0.0              # For temperature controllers
      max_temperature: 400.0

      # Communication Parameters (optional)
      timeout: 5000                     # milliseconds
      baud_rate: 9600                   # For serial devices
      read_termination: "\n"
      write_termination: "\n"

# Logging Configuration
logging:
  level: INFO                           # DEBUG, INFO, WARNING, ERROR
  format: "%(asctime)s [%(levelname)8s] %(name)s - %(message)s"
  file: "cryostat.log"                  # Log file path or null
  console: true                         # Output to console

# System Configuration
system:
  lock_timeout: 1.0                     # Global I/O lock timeout (seconds)
```

### Example: Mock Configuration

```yaml
cryostat:
  devices:
    vti_temp_controller:
      type: temperature
      driver: mock_itc503
      resource: "mock"
      actions: TemperatureActions
      description: "VTI Temperature Controller (Mock)"

    magnet1:
      type: magnet
      driver: mock_ips120
      resource: "mock"
      actions: MagnetActions
      description: "Main Magnet - 7T (Mock)"
      field_range: [-7, 7]

    helium_level_meter:
      type: level_meter
      driver: mock_ilm
      resource: "mock"
      actions: LevelMeterActions
      description: "Helium Level Meter (Mock)"
      num_channels: 2

logging:
  level: INFO
  console: true
  file: "cryostat_mock.log"

system:
  lock_timeout: 1.0
```

### Example: Production Configuration

```yaml
cryostat:
  devices:
    vti_temp_controller:
      type: temperature
      driver: itc503
      resource: "GPIB0::24::INSTR"
      actions: TemperatureActions
      description: "VTI Temperature Controller"
      timeout: 5000

    magnet1:
      type: magnet
      driver: ips120_10
      resource: "GPIB0::25::INSTR"
      actions: MagnetActions
      description: "Main Magnet - 7T"
      field_range: [-7, 7]
      switch_heater_heating_delay: 20   # seconds
      switch_heater_cooling_delay: 20

    helium_level_meter:
      type: level_meter
      driver: ilm211
      resource: "GPIB0::26::INSTR"
      actions: LevelMeterActions
      description: "Helium Level Meter"
      num_channels: 2

logging:
  level: WARNING
  console: false
  file: "/var/log/cryostat/cryostat.log"

system:
  lock_timeout: 2.0
```

### Configuration Best Practices

1. **Use Descriptive Names**: Device identifiers should be clear and meaningful
2. **Include Descriptions**: Help future users understand each device's purpose
3. **Document Custom Parameters**: Add comments for non-standard settings
4. **Separate Environments**: Create different configs for dev/test/production
5. **Version Control**: Track configuration changes in git
6. **Validate Before Use**: Test configuration files before deploying

---

## Registering New Components

### Step 1: Register Driver in Factory

Edit `cryostat/config/factory.py` and add your driver to `DRIVER_MAP`:

```python
DRIVER_MAP = {
    # Existing drivers...

    # Your new driver
    'your_driver_name': ('path.to.module', 'ClassName'),

    # Example:
    'lakeshore336': ('cryostat.drivers.real.lakeshore336', 'LakeShore336'),
}
```

### Step 2: Update Driver-Specific Parameters

In the same file, add valid parameters for your driver:

```python
driver_specific_params = {
    # Existing drivers...

    # Your new driver
    'your_driver_name': {'param1', 'param2', 'param3'},

    # Example:
    'lakeshore336': {'num_loops', 'scanner_channels', 'display_resolution'},
}
```

### Step 3: Create Configuration Entry

Add your device to the appropriate YAML configuration file (see previous section).

### Step 4: Import Action Class

Ensure your action class can be imported. In `cryostat/actions/__init__.py`:

```python
from .temperature_actions import TemperatureActions
from .magnet_actions import MagnetActions
from .level_meter_actions import LevelMeterActions
from .your_new_actions import YourNewActions  # Add this

__all__ = [
    'TemperatureActions',
    'MagnetActions',
    'LevelMeterActions',
    'YourNewActions',  # Add this
]
```

---

## Testing Your Code

### Unit Tests for Drivers

Create `test_<driver_name>.py` in `cryostat/tests/`:

```python
"""
Unit Tests for <Driver Name>
=============================

Tests for <Driver Name> driver implementation.
"""

import pytest
from cryostat.drivers.mock.<driver_file> import <DriverClass>


class Test<DriverClass>:
    """Test suite for <Driver>."""

    @pytest.fixture
    def driver(self):
        """Create driver instance for testing."""
        return <DriverClass>()

    def test_initialization(self, driver):
        """Test driver initializes correctly."""
        assert driver is not None
        assert driver.name == "Expected Name"

    def test_property_read(self, driver):
        """Test reading properties."""
        value = driver.property_name
        assert isinstance(value, float)
        assert value >= 0

    def test_property_write(self, driver):
        """Test writing properties."""
        driver.property_name = 10.0
        assert driver.property_name == 10.0

    def test_method_execution(self, driver):
        """Test method execution."""
        result = driver.method_name(param=5.0)
        assert result is True

    def test_error_handling(self, driver):
        """Test error conditions."""
        with pytest.raises(ValueError):
            driver.property_name = -100  # Invalid value
```

### Unit Tests for Actions

```python
"""
Unit Tests for <Action Class>
==============================

Tests for <Action> implementation.
"""

import pytest
from cryostat.drivers.mock.<driver> import <Driver>
from cryostat.actions.<action_file> import <ActionClass>


class Test<ActionClass>:
    """Test suite for <Action>."""

    @pytest.fixture
    def setup(self):
        """Create driver and action handler."""
        driver = <Driver>()
        actions = <ActionClass>(driver)
        return driver, actions

    def test_initialization(self, setup):
        """Test action handler initializes correctly."""
        driver, actions = setup
        assert actions is not None
        assert actions.driver is driver

    def test_initiate(self, setup):
        """Test device initialization."""
        driver, actions = setup
        result = actions.initiate()
        assert result is True

    def test_complex_operation(self, setup):
        """Test complex operation."""
        driver, actions = setup
        result = actions.complex_operation(target=10.0, rate=1.0)
        assert isinstance(result, float)
        assert abs(result - 10.0) < 0.5

    def test_get_status(self, setup):
        """Test status retrieval."""
        driver, actions = setup
        status = actions.get_status()
        assert isinstance(status, dict)
        assert 'property1' in status
```

### Integration Tests

```python
"""
Integration Tests
=================

Tests for complete system integration.
"""

import pytest
from pathlib import Path
from cryostat.core.cryostat import Cryostat


@pytest.fixture
def cryo():
    """Create Cryostat instance."""
    config_path = Path(__file__).parent.parent / 'config' / 'cryostat_mock.yaml'
    return Cryostat(str(config_path))


def test_device_access(cryo):
    """Test accessing device through cryostat."""
    device = cryo["your_device_name"]
    assert device is not None

    # Test property access
    value = device.property_name
    assert isinstance(value, float)

    # Test method execution
    result = device.method_name()
    assert result is not None


def test_high_level_operation(cryo):
    """Test high-level operation through cryostat."""
    device = cryo["your_device_name"]
    device.initiate()

    final_value = device.complex_operation(target=10.0)
    assert abs(final_value - 10.0) < 0.5
```

### Running Tests

```bash
# Run all tests
pytest cryostat/tests/ -v

# Run specific test file
pytest cryostat/tests/test_your_driver.py -v

# Run with coverage
pytest cryostat/tests/ --cov=cryostat --cov-report=html

# Run specific test
pytest cryostat/tests/test_your_driver.py::TestClass::test_method -v
```

---

## Best Practices

### Code Style

1. **Follow PEP 8**: Use consistent Python style
2. **Use Type Hints**: Add type annotations where helpful
3. **Write Docstrings**: Document all public methods and classes
4. **Log Appropriately**: Use different log levels correctly
   - `DEBUG`: Detailed diagnostic information
   - `INFO`: Major state changes and operations
   - `WARNING`: Unexpected but recoverable situations
   - `ERROR`: Serious problems that prevent operation

### Error Handling

```python
# Good: Specific exceptions with informative messages
if value < 0:
    raise ValueError(f"Value must be positive, got {value}")

# Good: Catch specific exceptions
try:
    result = self.driver.risky_operation()
except CommunicationError as e:
    log.error(f"Communication failed: {e}")
    raise

# Bad: Bare except
try:
    something()
except:  # Don't do this!
    pass
```

### Logging

```python
# Good: Informative log messages
log.info(f"Ramping temperature to {target}K at {rate}K/min")
log.debug(f"Current temperature: {current}K, error: {error}K")

# Good: Log at appropriate levels
log.error(f"Failed to communicate with device: {e}")
log.warning(f"Temperature drift detected: {drift}K")

# Bad: Not enough context
log.info("Starting")  # Starting what?
log.debug(f"{x}")     # What is x?
```

### Thread Safety

```python
# The system provides automatic locking through DeviceWrapper
# You don't need to add locks in your driver or action code

# However, if implementing internal state management:
import threading

class MyDriver:
    def __init__(self):
        self._internal_lock = threading.Lock()
        self._state = {}

    def thread_safe_operation(self):
        with self._internal_lock:
            # Modify internal state safely
            self._state['key'] = 'value'
```

### Documentation

```python
def complex_method(self, param1, param2=None, timeout=60):
    """One-line summary of what method does.

    More detailed description can go here, explaining:
    - What the method does
    - When to use it
    - Any important caveats

    Args:
        param1 (float): Description of param1
        param2 (str, optional): Description of param2. Defaults to None.
        timeout (int, optional): Maximum wait time in seconds. Defaults to 60.

    Returns:
        float: Description of return value

    Raises:
        ValueError: If param1 is negative
        TimeoutError: If operation exceeds timeout

    Example:
        >>> driver = MyDriver()
        >>> result = driver.complex_method(10.0, param2="test")
        >>> print(result)
        10.5
    """
    # Implementation
```

---

## Common Patterns

### Pattern 1: Waiting for Completion

```python
def wait_for_stable(self, target, error=0.1, timeout=600, check_interval=0.5):
    """Wait for value to stabilize at target.

    Typical use case: Waiting for temperature/field to reach setpoint.
    """
    import time
    start_time = time.time()

    while True:
        current = self.get_current_value()

        # Check if within tolerance
        if abs(current - target) < error:
            log.info(f"Value stabilized at {current}")
            return current

        # Check timeout
        elapsed = time.time() - start_time
        if elapsed > timeout:
            raise TimeoutError(
                f"Failed to stabilize after {timeout}s "
                f"(target={target}, current={current})"
            )

        # Wait before next check
        time.sleep(check_interval)
```

### Pattern 2: Multi-Step Procedure

```python
def complex_procedure(self, param1, param2):
    """Execute multi-step procedure with validation.

    Typical use case: Enabling persistent mode, ramping field, etc.
    """
    log.info(f"Starting complex procedure with {param1}, {param2}")

    try:
        # Step 1: Pre-checks
        if not self.is_ready():
            raise RuntimeError("Device not ready")

        # Step 2: First operation
        log.info("Step 1: Initial setup")
        self.setup_operation(param1)

        # Step 3: Wait for completion
        log.info("Step 2: Waiting for initial setup")
        self.wait_for_completion()

        # Step 4: Second operation
        log.info("Step 3: Main operation")
        result = self.main_operation(param2)

        # Step 5: Finalize
        log.info("Step 4: Finalizing")
        self.finalize()

        log.info("Complex procedure completed successfully")
        return result

    except Exception as e:
        log.error(f"Complex procedure failed: {e}")
        # Cleanup if needed
        self.emergency_cleanup()
        raise
```

### Pattern 3: Resource Management

```python
class DeviceWithResources:
    """Device that manages external resources."""

    def __init__(self):
        self._resource = None

    def connect(self):
        """Acquire resources."""
        log.info("Acquiring resources")
        self._resource = acquire_resource()

    def disconnect(self):
        """Release resources."""
        if self._resource:
            log.info("Releasing resources")
            self._resource.release()
            self._resource = None

    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()
        return False  # Don't suppress exceptions

# Usage
with DeviceWithResources() as device:
    device.do_something()
# Resources automatically released
```

---

## Troubleshooting

### Common Issues

#### Issue 1: Driver Not Found

**Error**: `ValueError: Unknown driver: 'my_driver'`

**Solution**: Register driver in `factory.py` `DRIVER_MAP`

#### Issue 2: Module Import Error

**Error**: `ImportError: Driver module not found`

**Solution**:
- Check module path in DRIVER_MAP is correct
- Ensure `__init__.py` exists in all package directories
- Verify Python path includes cryostat package

#### Issue 3: VISA Communication Error (Real Drivers)

**Error**: `Could not locate a VISA implementation`

**Solution**:
- Install PyVISA: `pip install pyvisa`
- Install VISA backend: `pip install pyvisa-py` (pure Python) or NI-VISA (binary)

#### Issue 4: Property Not Found

**Error**: `AttributeError: 'MockDriver' object has no attribute 'property_name'`

**Solution**:
- Ensure property is defined in driver with `@property` decorator
- Check spelling matches base interface
- Verify property is not marked as abstract without implementation

#### Issue 5: Test Timeout

**Error**: Tests hang or timeout

**Solution**:
- Check for infinite loops in wait methods
- Reduce simulation times for mock drivers
- Adjust timeout parameters in tests

### Debugging Tips

```python
# Add detailed logging
import logging
logging.basicConfig(level=logging.DEBUG)

# Test driver in isolation
from cryostat.drivers.mock.my_driver import MyDriver
driver = MyDriver()
print(f"Property: {driver.my_property}")

# Test action in isolation
from cryostat.actions.my_actions import MyActions
actions = MyActions(driver)
result = actions.my_method()

# Test configuration loading
from cryostat.config.loader import load_config
config = load_config('path/to/config.yaml')
print(config)

# Test driver creation
from cryostat.config.factory import create_driver
driver = create_driver('mock_itc503', 'mock')
```

---

## Checklist for Adding New Device

Use this checklist when adding support for a new device:

### Driver Implementation
- [ ] Created driver file in appropriate directory
- [ ] Inherited from correct base interface
- [ ] Implemented all required properties
- [ ] Implemented all required methods
- [ ] Added device-specific features
- [ ] Added docstrings to all public methods
- [ ] Added logging statements
- [ ] Tested manually in Python REPL

### Action Implementation
- [ ] Created action file
- [ ] Inherited from correct action interface
- [ ] Implemented `initiate()` method
- [ ] Implemented high-level operations
- [ ] Implemented `get_status()` method
- [ ] Added error handling
- [ ] Added logging statements
- [ ] Documented all methods

### Configuration
- [ ] Registered driver in `factory.py` DRIVER_MAP
- [ ] Added driver-specific parameters to factory
- [ ] Created/updated YAML configuration file
- [ ] Tested configuration loading

### Testing
- [ ] Created unit tests for driver
- [ ] Created unit tests for actions
- [ ] Created integration test
- [ ] All tests pass
- [ ] Test coverage > 80%

### Documentation
- [ ] Updated README if needed
- [ ] Added example usage
- [ ] Documented any caveats or limitations

---

## Example: Complete Workflow

Let's walk through adding support for a Lakeshore 336 Temperature Controller:

### 1. Create Mock Driver

```bash
touch cryostat/drivers/mock/mock_lakeshore336.py
```

```python
# cryostat/drivers/mock/mock_lakeshore336.py
from pymeasure.adapters import FakeAdapter
from pymeasure.instruments import Instrument
from ..base.temperature_base import TemperatureControllerBase
import logging

log = logging.getLogger(__name__)

class MockLakeShore336(Instrument, TemperatureControllerBase):
    def __init__(self, adapter=None, name="Mock LakeShore 336", **kwargs):
        if adapter is None:
            adapter = FakeAdapter()
        super().__init__(adapter, name, includeSCPI=False, **kwargs)

        self._temp_1 = 300.0
        # ... more initialization

    @property
    def temperature_1(self):
        return self._temp_1

    # ... implement all required properties and methods
```

### 2. Register in Factory

```python
# cryostat/config/factory.py
DRIVER_MAP = {
    # ... existing entries ...
    'mock_lakeshore336': ('cryostat.drivers.mock.mock_lakeshore336', 'MockLakeShore336'),
}

driver_specific_params = {
    # ... existing entries ...
    'mock_lakeshore336': {'num_loops', 'scanner_enabled'},
}
```

### 3. Add to Configuration

```yaml
# cryostat/config/cryostat_mock.yaml
cryostat:
  devices:
    sample_temp_controller:
      type: temperature
      driver: mock_lakeshore336
      resource: "mock"
      actions: TemperatureActions
      description: "Sample Temperature Controller - LakeShore 336"
      num_loops: 4
```

### 4. Create Tests

```python
# cryostat/tests/test_lakeshore336.py
import pytest
from cryostat.drivers.mock.mock_lakeshore336 import MockLakeShore336

class TestMockLakeShore336:
    @pytest.fixture
    def ls336(self):
        return MockLakeShore336()

    def test_initialization(self, ls336):
        assert ls336.name == "Mock LakeShore 336"

    # ... more tests
```

### 5. Test Everything

```bash
# Run tests
pytest cryostat/tests/test_lakeshore336.py -v

# Test in REPL
python
>>> from cryostat.drivers.mock.mock_lakeshore336 import MockLakeShore336
>>> ls = MockLakeShore336()
>>> print(ls.temperature_1)
300.0

# Test through cryostat
>>> from cryostat.core.cryostat import Cryostat
>>> cryo = Cryostat('cryostat/config/cryostat_mock.yaml')
>>> device = cryo["sample_temp_controller"]
>>> print(device.temperature_1)
300.0
```

---

## Summary

This guide covered:

✅ **Architecture**: Understanding the three-layer design
✅ **Drivers**: Writing mock and real device drivers
✅ **Actions**: Implementing high-level operations
✅ **Configuration**: Creating and managing YAML configs
✅ **Registration**: Adding components to the factory
✅ **Testing**: Writing comprehensive tests
✅ **Best Practices**: Code quality and patterns
✅ **Troubleshooting**: Common issues and solutions

**Next Steps:**
1. Review existing drivers/actions as examples
2. Follow the checklist when adding new devices
3. Write tests before implementing (TDD)
4. Ask for code review before merging

**Resources:**
- PyMeasure Documentation: https://pymeasure.readthedocs.io/
- Project README: `cryostat/README.md`
- Example Scripts: `cryostat/examples/`
- Test Files: `cryostat/tests/`

---

**Questions or Issues?**

Check the troubleshooting section or contact the development team.

**Happy Coding! 🔧❄️**
