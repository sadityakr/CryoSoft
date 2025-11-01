# PyMeasure Framework - Complete Reference Guide

**Last Updated**: 2025-10-21
**PyMeasure Version**: Based on local pymeasure-master repository
**Purpose**: Comprehensive reference for implementing cryostat v3.1 features

---

## Table of Contents

1. [Overview](#overview)
2. [Directory Structure](#directory-structure)
3. [Adapters - Communication Layer](#adapters---communication-layer)
4. [Instruments - Device Drivers](#instruments---device-drivers)
5. [Experiment Framework - Procedures](#experiment-framework---procedures)
6. [Display Layer - GUI Components](#display-layer---gui-components)
7. [Common Patterns](#common-patterns)
8. [Testing and Debugging](#testing-and-debugging)
9. [Quick Reference Tables](#quick-reference-tables)

---

## Overview

PyMeasure is a scientific measurement framework for Python that provides:

- **Instrument Abstraction**: Unified interface for hardware communication (GPIB, Serial, VISA, USB)
- **Procedure Framework**: Structured approach to measurement logic with built-in threading, logging, and progress tracking
- **Auto-Generated GUIs**: ManagedWindow automatically creates professional UIs from Procedure definitions
- **Data Management**: Automatic CSV file handling with metadata
- **Testing Support**: FakeAdapter and SwissArmyFake for hardware-free testing

**Key Philosophy**: Write declarative code (define what, not how) and let PyMeasure handle the plumbing.

---

## Directory Structure

```
pymeasure/
├── adapters/              # Communication protocols (VISA, Serial, Fake)
│   ├── adapter.py         # Base Adapter, FakeAdapter
│   ├── visa.py           # VISAAdapter (PyVISA wrapper)
│   ├── serial.py         # Serial communication
│   └── protocol.py       # Protocol adapter base
│
├── display/              # PyQt5 GUI components
│   ├── windows/
│   │   ├── managed_window.py       # Single-plot GUI (most common)
│   │   ├── managed_dock_window.py  # Multi-plot GUI (complex measurements)
│   │   └── managed_image_window.py # 2D array/image display
│   ├── widgets/          # Reusable Qt widgets (15+ types)
│   ├── manager.py        # Manager, Experiment, ExperimentQueue
│   ├── plotter.py        # pyqtgraph plotting utilities
│   └── inputs.py         # Input field generation
│
├── experiment/           # Measurement procedures framework
│   ├── procedure.py      # Procedure base class
│   ├── parameters.py     # Parameter types (Float, Int, Bool, etc.)
│   ├── results.py        # Data file management
│   ├── workers.py        # Worker threads for procedure execution
│   └── sequencer.py      # Parameter sweeping
│
└── instruments/          # Device drivers (100+ instruments)
    ├── instrument.py     # Instrument base class
    ├── common_base.py    # CommonBase (property creators)
    ├── fakes.py         # FakeInstrument, SwissArmyFake
    ├── validators.py    # Validation functions
    ├── channel.py       # Multi-channel support
    └── [manufacturers]/
        ├── oxfordinstruments/
        │   ├── base.py      # OxfordInstrumentsBase
        │   ├── itc503.py    # ITC503 Temperature Controller
        │   └── ips120_10.py # IPS120-10 Magnet Supply
        └── [100+ other manufacturers]
```

---

## Adapters - Communication Layer

Adapters handle low-level hardware communication. All adapters provide a consistent interface regardless of connection type.

### Base Adapter Interface

**Location**: `pymeasure/adapters/adapter.py`

```python
class Adapter:
    """Base class for all adapters"""

    def write(self, command: str):
        """Send command to device (with termination characters)"""

    def read(self) -> str:
        """Read response from device"""

    def ask(self, command: str) -> str:
        """Write command and read response (convenience method)"""

    def values(self, command: str, separator: str = ',', cast=float) -> list:
        """Send command and parse CSV response into list"""

    def close(self):
        """Close connection to device"""
```

**Key Methods**:
- `write()`: Automatically adds termination characters (defaults: `\n` write, `\n` read)
- `read()`: Blocks until response received or timeout
- `ask()`: Equivalent to `write()` + `read()`
- `values()`: Parses responses like `"1.5,2.3,4.7"` into `[1.5, 2.3, 4.7]`

---

### FakeAdapter (Testing)

**Location**: `pymeasure/adapters/adapter.py` (lines 175-240)

**Purpose**: Simulate device communication for testing without hardware.

```python
from pymeasure.adapters import FakeAdapter

adapter = FakeAdapter()
adapter.write("VOLTAGE 5.0")  # Stored in internal buffer
response = adapter.read()      # Returns what was written
```

**How it works**:
- Stores written commands in internal buffer (preloaded responses)
- Returns stored responses on `read()`
- Perfect for unit testing procedures

**Usage in Instruments**:
```python
# Real hardware
device = MyInstrument("GPIB::10")

# Testing mode
device = MyInstrument("mock")  # Automatically uses FakeAdapter
```

---

### VISAAdapter (Real Hardware)

**Location**: `pymeasure/adapters/visa.py`

**Purpose**: PyVISA wrapper supporting GPIB, Serial, TCP/IP, USB.

```python
from pymeasure.adapters import VISAAdapter

# GPIB
adapter = VISAAdapter("GPIB0::10::INSTR")

# Serial
adapter = VISAAdapter("ASRL1::INSTR", baud_rate=9600, parity='none')

# TCP/IP
adapter = VISAAdapter("TCPIP::192.168.1.100::INSTR")

# USB
adapter = VISAAdapter("USB0::0x1234::0x5678::INSTR")
```

**Configuration Options** (passed as kwargs):
- `timeout`: Milliseconds (default: 3000)
- `baud_rate`: For serial (default: 9600)
- `parity`: `'none'`, `'even'`, `'odd'` (serial)
- `stop_bits`: 1 or 2 (serial)
- `data_bits`: 7 or 8 (serial)
- `read_termination`: Character(s) to strip on read (default: `'\n'`)
- `write_termination`: Character(s) to append on write (default: `'\n'`)

---

### Adapter Auto-Creation in Instruments

**Instruments automatically create adapters from strings**:

```python
# These are equivalent:
device = MyInstrument("GPIB::10")
device = MyInstrument(VISAAdapter("GPIB::10"))

# Special keyword "mock" creates FakeAdapter:
device = MyInstrument("mock")
device = MyInstrument(FakeAdapter())
```

---

## Instruments - Device Drivers

Instruments provide high-level interfaces to hardware by wrapping adapters with named properties.

### Instrument Base Class

**Location**: `pymeasure/instruments/instrument.py`

```python
from pymeasure.instruments import Instrument

class MyDevice(Instrument):
    def __init__(self, adapter, name="My Device", **kwargs):
        super().__init__(adapter, name, **kwargs)

    def shutdown(self):
        """Safely close instrument (called on exit)"""
        pass
```

**Key Features**:
- Inherits `write()`, `read()`, `ask()`, `values()` from adapter
- Provides SCPI standard commands if `includeSCPI=True`
- Automatically creates adapter from resource string

**SCPI Properties** (if `includeSCPI=True`):
- `id`: Device identification string (*IDN?)
- `complete`: Operation complete (*OPC?)
- `status`: Status byte (*STB?)
- `options`: Installed options (*OPT?)
- `next_error`: Pop next error from queue

---

### Property Creators (CommonBase)

**Location**: `pymeasure/instruments/common_base.py` (lines 425-753)

PyMeasure uses **declarative properties** instead of getter/setter methods. Three property types:

#### 1. control() - Read/Write Property

**Full Signature**:
```python
property_name = Instrument.control(
    get_command: str,           # Command to read value ("T?" or "TEMP?")
    set_command: str,           # Command to write value ("T %f" or "TEMP %.2f")
    docs: str,                  # Property docstring

    # Value processing:
    validator=None,             # Validation function (strict_range, etc.)
    values=(),                  # Valid range/set for validator
    get_process=lambda v: v,    # Transform read value (str->float)
    set_process=lambda v: v,    # Transform before writing

    # Advanced:
    map_values=False,           # Send index instead of value
    get_process_list=None,      # Process list of values
    dynamic=False,              # Allow runtime parameter changes
    check_set_errors=False,     # Query errors after write
    check_get_errors=False,     # Query errors after read
)
```

**Simple Example**:
```python
temperature = Instrument.control(
    "T?",              # Read command
    "T %f",            # Write command (%.2f formats to 2 decimals)
    "Temperature in Kelvin",
    validator=strict_range,
    values=(0, 400),   # Valid range: 0-400K
    get_process=float, # Convert string response to float
)

# Usage:
device.temperature = 300.5  # Sends "T 300.5"
temp = device.temperature   # Sends "T?", returns float
```

**Format Specifiers in set_command**:
- `%d` - Integer
- `%f` - Float (default precision)
- `%.2f` - Float with 2 decimals
- `%e` - Scientific notation
- `%s` - String

**Advanced Example with Mapping**:
```python
output_mode = Instrument.control(
    "MODE?", "MODE %d",
    "Output mode",
    validator=strict_discrete_set,
    values={'OFF': 0, 'ON': 1, 'AUTO': 2},  # Dictionary mapping
    map_values=True,  # Send 0/1/2 instead of OFF/ON/AUTO
)

# Usage:
device.output_mode = 'AUTO'  # Sends "MODE 2"
mode = device.output_mode     # Sends "MODE?", returns 'AUTO' (not 2)
```

#### 2. measurement() - Read-Only Property

**Signature**:
```python
property_name = Instrument.measurement(
    get_command: str,
    docs: str,
    get_process=lambda v: v,
    values=(),
    map_values=False,
)
```

**Example**:
```python
current = Instrument.measurement(
    "I?",
    "Measured current in Amperes",
    get_process=float,
)

# Usage:
i = device.current       # Sends "I?", returns float
device.current = 5.0     # ERROR - read-only property
```

#### 3. setting() - Write-Only Property

**Signature**:
```python
property_name = Instrument.setting(
    set_command: str,
    docs: str,
    validator=None,
    values=(),
    set_process=lambda v: v,
    map_values=False,
)
```

**Example**:
```python
output_enable = Instrument.setting(
    "OUT %d",
    "Enable/disable output (0=off, 1=on)",
    validator=strict_discrete_set,
    values=[0, 1],
)

# Usage:
device.output_enable = 1   # Sends "OUT 1"
state = device.output_enable  # ERROR - write-only property
```

---

### Validators

**Location**: `pymeasure/instruments/validators.py`

Validators check/transform values before sending to device.

```python
from pymeasure.instruments.validators import (
    strict_range,
    strict_discrete_set,
    strict_discrete_range,
    truncated_range,
    modular_range,
)
```

**1. strict_range** - Value must be in [min, max]
```python
voltage = Instrument.control(
    "V?", "V %f",
    "Voltage (V)",
    validator=strict_range,
    values=(0, 30),  # Min=0, Max=30
)
# device.voltage = 25  ✓ OK
# device.voltage = 35  ✗ Raises ValueError
```

**2. strict_discrete_set** - Value must be in exact set
```python
mode = Instrument.control(
    "MODE?", "MODE %s",
    "Operating mode",
    validator=strict_discrete_set,
    values=['A', 'B', 'C'],
)
# device.mode = 'B'   ✓ OK
# device.mode = 'D'   ✗ Raises ValueError
```

**3. strict_discrete_range** - In range AND multiple of step
```python
frequency = Instrument.control(
    "F?", "F %f",
    "Frequency (Hz)",
    validator=strict_discrete_range,
    values=(100, 1000, 50),  # Min=100, Max=1000, Step=50
)
# device.frequency = 150   ✓ OK (100 + 50*1)
# device.frequency = 125   ✗ Raises ValueError (not multiple of 50)
```

**4. truncated_range** - Clamps to [min, max]
```python
power = Instrument.control(
    "P?", "P %f",
    "Power (W)",
    validator=truncated_range,
    values=(0, 100),
)
# device.power = 150   ✓ Sends "P 100" (clamped)
# device.power = -10   ✓ Sends "P 0" (clamped)
```

**5. modular_range** - Modulo arithmetic (for angles)
```python
angle = Instrument.control(
    "A?", "A %f",
    "Angle (degrees)",
    validator=modular_range,
    values=(0, 360),
)
# device.angle = 370   ✓ Sends "A 10" (370 % 360)
# device.angle = -30   ✓ Sends "A 330" ((-30) % 360)
```

---

### OxfordInstrumentsBase

**Location**: `pymeasure/instruments/oxfordinstruments/base.py`

**Purpose**: Base class for all Oxford Instruments devices (ITC503, Mercury iPS, ILM210, etc.)

```python
from pymeasure.instruments.oxfordinstruments import OxfordInstrumentsBase

class ITC503(OxfordInstrumentsBase):
    def __init__(self, adapter, name="ITC503", max_attempts=5, **kwargs):
        kwargs.setdefault('read_termination', '\r')
        super().__init__(adapter, name, max_attempts, **kwargs)
```

**Built-in Features**:
- **Retry Logic**: `ask()` retries up to `max_attempts` times on invalid responses
- **Response Validation**: `is_valid_response()` checks for device-specific error patterns
- **Delay Support**: `wait_for(delay)` pauses before reading (some devices require this)
- **Default Settings**: Baud=9600, Data=8, Parity=None, Stop=2, Termination=`\r`

**Key Methods**:
```python
def ask(self, command):
    """Ask with retry logic for Oxford protocol quirks"""

def is_valid_response(self, value):
    """Check if response is valid (not error code)"""

def wait_for(self, delay=0.05):
    """Wait before reading response (Oxford devices need this)"""
```

**Example**:
```python
class ITC503(OxfordInstrumentsBase):
    temperature_1 = Instrument.measurement(
        "R1",
        "Temperature of sensor 1 in Kelvin"
    )

    def __init__(self, adapter, **kwargs):
        super().__init__(adapter, "ITC503", max_attempts=5, **kwargs)
        self.wait_for(0.1)  # Initial delay
```

---

### Complete Instrument Example

```python
from pymeasure.instruments import Instrument
from pymeasure.instruments.validators import strict_range, strict_discrete_set

class PowerSupply(Instrument):
    """Generic Power Supply - Model XYZ"""

    # Read/write voltage (0-30V)
    voltage = Instrument.control(
        "VOLT?", "VOLT %.2f",
        "Output voltage in Volts",
        validator=strict_range,
        values=(0, 30),
        get_process=float,
    )

    # Read-only current measurement
    current = Instrument.measurement(
        "CURR?",
        "Measured current in Amperes",
        get_process=float,
    )

    # Write-only output enable
    output = Instrument.setting(
        "OUT %d",
        "Enable/disable output (0=off, 1=on)",
        validator=strict_discrete_set,
        values=[0, 1],
    )

    def __init__(self, adapter, name="Power Supply", **kwargs):
        super().__init__(adapter, name, **kwargs)

    def shutdown(self):
        """Safely shutdown - set voltage to 0 and disable output"""
        self.voltage = 0
        self.output = 0

# Usage:
ps = PowerSupply("GPIB::5")        # Real hardware
ps = PowerSupply("mock")           # Testing

ps.output = 1
ps.voltage = 12.5
print(f"Current: {ps.current} A")
ps.shutdown()
```

---

## Experiment Framework - Procedures

Procedures define measurement logic separately from GUI code. PyMeasure automatically runs them in background threads and generates GUIs.

### Procedure Base Class

**Location**: `pymeasure/experiment/procedure.py`

```python
from pymeasure.experiment import Procedure

class MyProcedure(Procedure):
    # Status constants (automatically managed)
    FINISHED = 0
    FAILED = 1
    ABORTED = 2
    QUEUED = 3
    RUNNING = 4

    # Define data columns (REQUIRED)
    DATA_COLUMNS = ['Time (s)', 'Temperature (K)', 'Voltage (V)']

    def startup(self):
        """Initialize instruments (called once before execute)"""
        pass

    def execute(self):
        """Main measurement loop (REQUIRED)"""
        pass

    def shutdown(self):
        """Cleanup (called even if execute fails)"""
        pass
```

**Key Methods to Implement**:

1. **startup()** - Initialize instruments
   - Connect to devices
   - Verify communication
   - Set initial states
   - Called once before `execute()`

2. **execute()** - Main measurement loop
   - Perform measurements
   - Emit data: `self.emit('results', data_dict)`
   - Emit progress: `self.emit('progress', percent)`
   - Check abort: `if self.should_stop(): break`

3. **shutdown()** - Cleanup
   - Return instruments to safe state
   - Close connections (if needed)
   - Always called, even if `execute()` raises exception

**Built-in Methods** (use in execute()):

```python
# Emit data point (dict keys must match DATA_COLUMNS)
self.emit('results', {'Time (s)': 1.5, 'Temperature (K)': 300})

# Emit progress (0-100)
self.emit('progress', 50)

# Check if user clicked abort
if self.should_stop():
    break

# Verify all parameters are set
self.check_parameters()

# Update status
self.status = self.RUNNING
```

---

### Parameters

**Location**: `pymeasure/experiment/parameters.py`

Parameters define input values for procedures. They automatically generate GUI input fields.

**Available Types**:

```python
from pymeasure.experiment import (
    Parameter,
    FloatParameter,
    IntegerParameter,
    BooleanParameter,
    ListParameter,
    VectorParameter,
)
```

#### FloatParameter

```python
temperature = FloatParameter(
    'Target Temperature',      # Display name (shown in GUI)
    units='K',                 # Units (shown in GUI)
    minimum=0,                 # Min value (GUI enforced)
    maximum=400,               # Max value (GUI enforced)
    decimals=2,                # Decimal places in GUI
    step=0.1,                  # Increment for up/down arrows
    default=300,               # Default value
)
```

**Usage in Procedure**:
```python
class TemperatureRamp(Procedure):
    target_temp = FloatParameter('Target Temperature', units='K', default=300)

    def execute(self):
        print(f"Ramping to {self.target_temp} K")
```

#### IntegerParameter

```python
num_points = IntegerParameter(
    'Number of Points',
    minimum=1,
    maximum=10000,
    step=1,
    default=100,
)
```

#### BooleanParameter

```python
enable_logging = BooleanParameter(
    'Enable Data Logging',
    default=True,
)
```

#### ListParameter

```python
measurement_list = ListParameter(
    'Measurement Sequence',
    default=[1, 2, 3, 4, 5],
)
```

#### VectorParameter

```python
xyz_position = VectorParameter(
    'XYZ Position',
    length=3,              # 3-element vector
    units='mm',
    default=[0, 0, 0],
)
```

---

### Parameter Grouping (Conditional Visibility)

**Show/hide parameters based on other parameter values**:

```python
class AdvancedProcedure(Procedure):
    mode = Parameter('Mode', default='simple')

    # Only show if mode == 'advanced'
    advanced_option = FloatParameter(
        'Advanced Setting',
        group_by='mode',              # Depends on 'mode'
        group_condition='advanced',   # Show if mode == 'advanced'
        default=1.0,
    )

    # Multiple dependencies (AND logic)
    expert_option = FloatParameter(
        'Expert Setting',
        group_by=['mode', 'type'],
        group_condition=['advanced', 'custom'],  # mode=='advanced' AND type=='custom'
        default=0.5,
    )
```

---

### Complete Procedure Example

```python
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter
from time import time, sleep
import numpy as np

class TemperatureRampProcedure(Procedure):
    """Ramp temperature from start to end and log data"""

    # Input parameters (auto-generate GUI fields)
    start_temp = FloatParameter('Start Temperature', units='K', default=300)
    end_temp = FloatParameter('End Temperature', units='K', default=4)
    ramp_rate = FloatParameter('Ramp Rate', units='K/min', default=1)
    num_setpoints = IntegerParameter('Number of Setpoints', default=20)

    # Output data columns
    DATA_COLUMNS = [
        'Time (s)',
        'Temperature 1 (K)',
        'Temperature 2 (K)',
        'Heater Power (%)',
        'Setpoint (K)',
    ]

    def startup(self):
        """Initialize temperature controller"""
        from core.device_manager import device_manager

        self.itc = device_manager.get_instrument('itc503')
        if self.itc is None:
            raise Exception("ITC503 not found in device manager")

        self.itc.control_mode = 'RU'  # Remote & Unlocked
        self.start_time = time()

    def execute(self):
        """Run the temperature ramp"""
        setpoints = np.linspace(self.start_temp, self.end_temp, self.num_setpoints)

        for i, setpoint in enumerate(setpoints):
            # Set new temperature target
            self.itc.temperature_setpoint = setpoint

            # Wait for stabilization
            self.itc.wait_for_temperature(error=0.1, timeout=600)

            # Collect data
            data = {
                'Time (s)': time() - self.start_time,
                'Temperature 1 (K)': self.itc.temperature_1,
                'Temperature 2 (K)': self.itc.temperature_2,
                'Heater Power (%)': self.itc.heater,
                'Setpoint (K)': setpoint,
            }

            # Emit to GUI (updates plot and saves to file)
            self.emit('results', data)

            # Update progress bar
            progress = 100 * (i + 1) / self.num_setpoints
            self.emit('progress', progress)

            # Check if user clicked abort
            if self.should_stop():
                break

    def shutdown(self):
        """Safely shutdown - hold current temperature"""
        if hasattr(self, 'itc') and self.itc is not None:
            # Keep heater at current setpoint (don't ramp to 0)
            pass  # Or set to safe value: self.itc.temperature_setpoint = 300
```

---

## Display Layer - GUI Components

PyMeasure automatically generates GUIs from Procedure definitions. You rarely need to write custom GUI code.

### ManagedWindow (Single Plot)

**Location**: `pymeasure/display/windows/managed_window.py`

**Purpose**: Automatic GUI for simple measurements (one x/y plot).

**Full Signature**:
```python
from pymeasure.display.windows import ManagedWindow

class MyWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class,           # Your Procedure class
            inputs=[],                 # Parameters to show in input form
            displays=[],               # Parameters to show in browser
            x_axis='',                 # X-axis column name
            y_axis='',                 # Y-axis column name

            # Optional customization:
            linewidth=1,              # Plot line width
            log_channel='',           # Logging channel name
            log_level=logging.INFO,   # Logging verbosity
            sequencer=False,          # Enable parameter sequencing
            sequencer_inputs=None,    # Parameters available for sequencing
            directory='.',            # Default data directory
            file_input=True,          # Show file name input
        )
```

**Built-in Features (Free)**:
- ✅ Auto-generated parameter input form
- ✅ Real-time pyqtgraph plotting
- ✅ Data file browser and save dialog
- ✅ Logging widget with color-coded severity
- ✅ Experiment queue (run multiple experiments sequentially)
- ✅ Progress bar and abort button
- ✅ Time estimation widget
- ✅ Parameter sequencing (sweep parameter values)

**Example**:
```python
from pymeasure.display.windows import ManagedWindow
from procedures.simple.temperature_ramp import TemperatureRampProcedure

class TemperatureRampWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=TemperatureRampProcedure,
            inputs=['start_temp', 'end_temp', 'ramp_rate', 'num_setpoints'],
            displays=['start_temp', 'end_temp'],  # Show in browser column
            x_axis='Time (s)',
            y_axis='Temperature 1 (K)',
            linewidth=2,
        )
        self.setWindowTitle('Temperature Ramp Measurement')

# Launch window
if __name__ == '__main__':
    import sys
    from pymeasure.display.Qt import QtWidgets

    app = QtWidgets.QApplication(sys.argv)
    window = TemperatureRampWindow()
    window.show()
    sys.exit(app.exec())
```

**Parameters Explanation**:
- `inputs`: Which Procedure parameters to show in input form (creates input fields automatically)
- `displays`: Which parameters to show in experiment browser (for tracking runs)
- `x_axis`/`y_axis`: Must match column names in Procedure.DATA_COLUMNS

---

### ManagedDockWindow (Multiple Plots)

**Location**: `pymeasure/display/windows/managed_dock_window.py`

**Purpose**: Automatic GUI for complex measurements (multiple dockable plots).

**Key Difference**: `x_axis` and `y_axis` are **lists** that create multiple plots.

```python
from pymeasure.display.windows import ManagedDockWindow

class ComplexMeasurementWindow(ManagedDockWindow):
    def __init__(self):
        super().__init__(
            procedure_class=TempDependentIVProcedure,
            inputs=['start_temp', 'end_temp', 'temp_steps',
                    'min_current', 'max_current', 'iv_steps'],

            # Multiple plots (3 plots created automatically):
            x_axis=['Time (s)', 'Voltage (V)', 'Temperature (K)'],
            y_axis=['Temperature (K)', 'Current (A)', 'Resistance (Ohm)'],

            linewidth=2,
        )
        self.setWindowTitle('Temperature-Dependent IV Characterization')
```

**Result**:
- **Plot 1**: Time (s) vs Temperature (K)
- **Plot 2**: Voltage (V) vs Current (A)
- **Plot 3**: Temperature (K) vs Resistance (Ohm)

Each plot is in a separate dockable widget that can be:
- Dragged to rearrange layout
- Detached into separate window
- Closed/reopened

---

### Available Widgets

**Location**: `pymeasure/display/widgets/`

If you need custom GUIs, these widgets are available:

| Widget | Purpose | File |
|--------|---------|------|
| `PlotWidget` | Real-time pyqtgraph plotting | `plot_widget.py` |
| `LogWidget` | Colored logging output | `log_widget.py` |
| `InputsWidget` | Auto-generated parameter form | `inputs_widget.py` |
| `SequencerWidget` | Parameter sweep configuration | `sequencer_widget.py` |
| `BrowserWidget` | Experiment history browser | `browser_widget.py` |
| `FilenameWidget` | File name input field | `filename_widget.py` |
| `DirectoryWidget` | Directory chooser | `directory_widget.py` |
| `FileInputWidget` | File selector | `file_input.py` |
| `EstimatorWidget` | Time estimation display | `estimator_widget.py` |
| `DockWidget` | Dockable plot container | `dock_widget.py` |
| `TableWidget` | Data table display | `table_widget.py` |
| `ImageWidget` | 2D array/image display | `image_widget.py` |

**Usage** (rarely needed - ManagedWindow provides these):
```python
from pymeasure.display.widgets import LogWidget, PlotWidget

log_widget = LogWidget()
plot_widget = PlotWidget("My Plot", ['Time (s)', 'Voltage (V)'])
```

---

### Manager and Experiment Queue

**Location**: `pymeasure/display/manager.py`

The Manager coordinates procedure execution and queuing.

```python
from pymeasure.display import Manager

manager = Manager()
manager.queue.append(experiment)  # Add to queue
manager.queue.remove(experiment)  # Remove from queue
next_exp = manager.queue.next()   # Get next experiment
```

**Built into ManagedWindow** - you don't normally interact with Manager directly.

---

## Common Patterns

### Pattern 1: Simple Instrument Driver

```python
from pymeasure.instruments import Instrument
from pymeasure.instruments.validators import strict_range

class Multimeter(Instrument):
    """Keithley 2000 Multimeter"""

    voltage = Instrument.measurement(
        ":MEAS:VOLT:DC?",
        "Measured DC voltage (V)",
        get_process=float,
    )

    mode = Instrument.control(
        ":FUNC?", ":FUNC '%s'",
        "Measurement mode",
        validator=strict_discrete_set,
        values=['VOLT:DC', 'CURR:DC', 'RES'],
    )

    def __init__(self, adapter, **kwargs):
        super().__init__(adapter, "Keithley 2000", **kwargs)

    def shutdown(self):
        pass  # No shutdown needed

# Usage:
mm = Multimeter("GPIB::16")
mm.mode = 'VOLT:DC'
voltage = mm.voltage
```

---

### Pattern 2: Oxford Instruments Driver

```python
from pymeasure.instruments.oxfordinstruments import OxfordInstrumentsBase
from pymeasure.instruments.validators import truncated_range

class ITC503(OxfordInstrumentsBase):
    """Oxford Instruments ITC503 Temperature Controller"""

    temperature_1 = Instrument.measurement(
        "R1",
        "Temperature of sensor 1 (K)"
    )

    temperature_setpoint = Instrument.control(
        "R0", "T%f",
        "Temperature setpoint (K)",
        validator=truncated_range,
        values=[0, 400],
    )

    heater = Instrument.measurement(
        "R5",
        "Heater power (%)"
    )

    def __init__(self, adapter, **kwargs):
        kwargs.setdefault('read_termination', '\r')
        super().__init__(adapter, "ITC503", max_attempts=5, **kwargs)

    def wait_for_temperature(self, error=0.1, timeout=600):
        """Wait for temperature to stabilize"""
        import time
        start = time.time()
        while time.time() - start < timeout:
            if abs(self.temperature_1 - self.temperature_setpoint) < error:
                return True
            time.sleep(1)
        return False

    def shutdown(self):
        pass  # Keep temperature at current setpoint
```

---

### Pattern 3: Simple Procedure + ManagedWindow

**Procedure** (`procedures/simple/field_sweep.py`):
```python
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter
from time import time
import numpy as np

class FieldSweepProcedure(Procedure):
    start_field = FloatParameter('Start Field', units='T', default=0)
    end_field = FloatParameter('End Field', units='T', default=1)
    num_points = IntegerParameter('Number of Points', default=50)

    DATA_COLUMNS = ['Time (s)', 'Field (T)', 'Resistance (Ohm)']

    def startup(self):
        from core.device_manager import device_manager
        self.magnet = device_manager.get_instrument('mercury_ips')
        self.start_time = time()

    def execute(self):
        fields = np.linspace(self.start_field, self.end_field, self.num_points)

        for i, field in enumerate(fields):
            self.magnet.field_setpoint = field
            self.magnet.wait_for_field()

            data = {
                'Time (s)': time() - self.start_time,
                'Field (T)': self.magnet.field,
                'Resistance (Ohm)': self.measure_resistance(),  # Implement this
            }

            self.emit('results', data)
            self.emit('progress', 100 * (i + 1) / self.num_points)

            if self.should_stop():
                break

    def shutdown(self):
        pass
```

**Window** (`measurements/field_sweep_window.py`):
```python
from pymeasure.display.windows import ManagedWindow
from procedures.simple.field_sweep import FieldSweepProcedure

class FieldSweepWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=FieldSweepProcedure,
            inputs=['start_field', 'end_field', 'num_points'],
            displays=['start_field', 'end_field'],
            x_axis='Field (T)',
            y_axis='Resistance (Ohm)',
        )
        self.setWindowTitle('Magnetic Field Sweep')

if __name__ == '__main__':
    import sys
    from pymeasure.display.Qt import QtWidgets
    app = QtWidgets.QApplication(sys.argv)
    window = FieldSweepWindow()
    window.show()
    sys.exit(app.exec())
```

---

### Pattern 4: Composite Procedure (Nested)

**Composite procedure that runs multiple simple procedures**:

```python
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter
from procedures.simple.temperature_ramp import TemperatureRampProcedure
from procedures.simple.iv_sweep import IVSweepProcedure
import numpy as np

class TemperatureDependentIVProcedure(Procedure):
    """Run IV sweep at multiple temperatures"""

    start_temp = FloatParameter('Start Temperature', units='K', default=300)
    end_temp = FloatParameter('End Temperature', units='K', default=4)
    temp_steps = IntegerParameter('Temperature Steps', default=10)

    min_current = FloatParameter('Min Current', units='A', default=-0.01)
    max_current = FloatParameter('Max Current', units='A', default=0.01)
    iv_steps = IntegerParameter('IV Steps', default=50)

    DATA_COLUMNS = ['Temperature (K)', 'Current (A)', 'Voltage (V)', 'Resistance (Ohm)']

    def startup(self):
        from core.device_manager import device_manager
        self.itc = device_manager.get_instrument('itc503')

    def execute(self):
        temps = np.linspace(self.start_temp, self.end_temp, self.temp_steps)

        for i, temp in enumerate(temps):
            # Set temperature
            self.itc.temperature_setpoint = temp
            self.itc.wait_for_temperature()

            # Run IV sweep at this temperature
            currents = np.linspace(self.min_current, self.max_current, self.iv_steps)
            for current in currents:
                voltage = self.measure_iv(current)  # Implement this

                data = {
                    'Temperature (K)': self.itc.temperature_1,
                    'Current (A)': current,
                    'Voltage (V)': voltage,
                    'Resistance (Ohm)': voltage / current if current != 0 else 0,
                }

                self.emit('results', data)

                if self.should_stop():
                    return

            # Update progress after each temperature
            self.emit('progress', 100 * (i + 1) / self.temp_steps)

    def shutdown(self):
        pass
```

**Window with multiple plots**:
```python
from pymeasure.display.windows import ManagedDockWindow
from procedures.composite.temp_dependent_iv import TemperatureDependentIVProcedure

class TempDependentIVWindow(ManagedDockWindow):
    def __init__(self):
        super().__init__(
            procedure_class=TemperatureDependentIVProcedure,
            inputs=['start_temp', 'end_temp', 'temp_steps',
                    'min_current', 'max_current', 'iv_steps'],

            # 3 plots automatically created:
            x_axis=['Temperature (K)', 'Current (A)', 'Voltage (V)'],
            y_axis=['Resistance (Ohm)', 'Voltage (V)', 'Current (A)'],
        )
        self.setWindowTitle('Temperature-Dependent IV')
```

---

## Testing and Debugging

### Using FakeAdapter

**Method 1: Direct FakeAdapter**
```python
from pymeasure.adapters import FakeAdapter
from instruments.oi_itc503.oi_itc503 import ITC503

adapter = FakeAdapter()
itc = ITC503(adapter)

# All writes go to internal buffer
# All reads return empty string (unless preloaded)
itc.temperature_setpoint = 300
temp = itc.temperature_1  # Returns empty string (need to mock responses)
```

**Method 2: "mock" keyword**
```python
itc = ITC503("mock")  # Automatically creates FakeAdapter
```

---

### Using SwissArmyFake

**Location**: `pymeasure/instruments/fakes.py`

**Purpose**: Multi-purpose fake instrument for testing procedures.

```python
from pymeasure.instruments.fakes import SwissArmyFake

fake = SwissArmyFake()

# Generates realistic data streams
fake.voltage        # Random voltage
fake.current        # Random current
fake.temperature    # Temperature with drift
fake.frequency      # Frequency sweep
fake.waveform       # Sine/square/triangle waveforms
fake.image_data     # 2D arrays for image display
```

**Use in Procedures**:
```python
def startup(self):
    if testing:
        self.device = SwissArmyFake()
    else:
        self.device = RealInstrument("GPIB::10")
```

---

### Testing Procedures Without GUI

```python
from procedures.simple.temperature_ramp import TemperatureRampProcedure
from pymeasure.experiment import Results
import tempfile

# Create procedure
proc = TemperatureRampProcedure()
proc.start_temp = 300
proc.end_temp = 250
proc.ramp_rate = 1
proc.num_setpoints = 10

# Create temporary results file
with tempfile.NamedTemporaryFile(suffix='.csv', delete=False) as f:
    results = Results(proc, f.name)

    # Run procedure
    proc.startup()
    proc.execute()
    proc.shutdown()

    # Check results
    print(f"Data points collected: {len(results)}")
    print(f"Data file: {f.name}")
```

---

## Quick Reference Tables

### Adapter Quick Reference

| Adapter Type | Resource String | Use Case |
|--------------|----------------|----------|
| VISAAdapter (GPIB) | `"GPIB0::10::INSTR"` | GPIB devices |
| VISAAdapter (Serial) | `"ASRL1::INSTR"` | Serial/RS232 |
| VISAAdapter (TCP/IP) | `"TCPIP::192.168.1.100::INSTR"` | Ethernet devices |
| VISAAdapter (USB) | `"USB0::0x1234::0x5678::INSTR"` | USB devices |
| FakeAdapter | `"mock"` or `FakeAdapter()` | Testing |

---

### Property Type Quick Reference

| Property Type | Read | Write | Use Case |
|---------------|------|-------|----------|
| `control()` | ✓ | ✓ | Setpoints, modes, configs |
| `measurement()` | ✓ | ✗ | Sensor readings, status |
| `setting()` | ✗ | ✓ | Commands, triggers |

---

### Validator Quick Reference

| Validator | Behavior | Example |
|-----------|----------|---------|
| `strict_range` | Raises error if outside [min, max] | `values=(0, 100)` |
| `strict_discrete_set` | Raises error if not in set | `values=['A', 'B', 'C']` |
| `strict_discrete_range` | Raises error if not in range or not multiple of step | `values=(0, 100, 5)` |
| `truncated_range` | Clamps to [min, max] | `values=(0, 100)` |
| `modular_range` | Modulo arithmetic (for angles) | `values=(0, 360)` |

---

### Parameter Type Quick Reference

| Parameter | GUI Widget | Use Case |
|-----------|------------|----------|
| `FloatParameter` | Spin box with decimals | Temperatures, voltages, currents |
| `IntegerParameter` | Spin box (integers only) | Counts, steps, iterations |
| `BooleanParameter` | Checkbox | Enable/disable flags |
| `ListParameter` | Text field (comma-separated) | Measurement sequences |
| `VectorParameter` | Multiple spin boxes | XYZ positions, RGB colors |

---

### Window Type Quick Reference

| Window Type | Plots | Use Case |
|-------------|-------|----------|
| `ManagedWindow` | Single x/y plot | Simple measurements (temp ramp, field sweep) |
| `ManagedDockWindow` | Multiple dockable plots | Complex measurements (multi-variable) |
| `ManagedImageWindow` | 2D image/array | Imaging, 2D scans |

---

## Key File Locations

All paths relative to: `C:\Users\sadit\desktop\cryostat_v3.1\pymeasure-master\pymeasure-master\pymeasure\`

| Component | File |
|-----------|------|
| **Adapters** |
| Base Adapter, FakeAdapter | `adapters/adapter.py` |
| VISAAdapter | `adapters/visa.py` |
| **Instruments** |
| Instrument base class | `instruments/instrument.py` |
| Property creators (control/measurement/setting) | `instruments/common_base.py` (lines 425-753) |
| Validators | `instruments/validators.py` |
| FakeInstrument, SwissArmyFake | `instruments/fakes.py` |
| OxfordInstrumentsBase | `instruments/oxfordinstruments/base.py` |
| ITC503 example | `instruments/oxfordinstruments/itc503.py` |
| **Experiment** |
| Procedure base class | `experiment/procedure.py` |
| Parameter types | `experiment/parameters.py` |
| Results and data files | `experiment/results.py` |
| Worker threads | `experiment/workers.py` |
| **Display** |
| ManagedWindow | `display/windows/managed_window.py` |
| ManagedDockWindow | `display/windows/managed_dock_window.py` |
| Manager and ExperimentQueue | `display/manager.py` |
| Widgets | `display/widgets/` (directory) |

---

## Best Practices for Cryostat v3.1

### ✅ Do:

1. **Inherit from OxfordInstrumentsBase** for all Oxford devices (ITC503, Mercury iPS, ILM210)
2. **Use control() for setpoints**, measurement() for readings
3. **Implement shutdown()** in all instruments (safe close)
4. **Use "mock"** for all development/testing before hardware connection
5. **Create small procedures** (~50-100 lines) that do one thing well
6. **Let ManagedWindow handle GUI** - don't write custom Qt code
7. **Use DATA_COLUMNS** to define output structure clearly
8. **Emit progress regularly** for user feedback
9. **Check should_stop()** in loops to allow graceful abort
10. **Use device_manager** in procedures for centralized device access

### ❌ Don't:

1. Don't write custom GUI code - use ManagedWindow/ManagedDockWindow
2. Don't use getter/setter methods - use declarative properties (control/measurement)
3. Don't access adapters directly from procedures - use instrument abstraction
4. Don't forget shutdown() - it's called even on errors
5. Don't hardcode device addresses - use device_manager
6. Don't test with real hardware first - always test with FakeAdapter/mock
7. Don't create monolithic procedures - break into simple + composite
8. Don't forget to emit 'results' - GUI needs data to plot
9. Don't block the GUI thread - procedures run in Worker threads automatically
10. Don't duplicate PyMeasure functionality - leverage built-in features

---

## Code Templates

### Instrument Template

```python
from pymeasure.instruments import Instrument
from pymeasure.instruments.validators import strict_range, strict_discrete_set

class MyInstrument(Instrument):
    """Manufacturer - Model Name"""

    # Read/write property
    setpoint = Instrument.control(
        "GET_CMD", "SET_CMD %f",
        "Description with units",
        validator=strict_range,
        values=(min, max),
        get_process=float,
    )

    # Read-only property
    measurement = Instrument.measurement(
        "MEAS_CMD",
        "Measurement description with units",
        get_process=float,
    )

    # Write-only property
    command = Instrument.setting(
        "CMD %d",
        "Command description",
        validator=strict_discrete_set,
        values=[0, 1],
    )

    def __init__(self, adapter, **kwargs):
        super().__init__(adapter, "My Instrument", **kwargs)

    def shutdown(self):
        """Safe shutdown procedure"""
        pass
```

---

### Procedure Template

```python
from pymeasure.experiment import Procedure, FloatParameter, IntegerParameter
from time import time

class MyProcedure(Procedure):
    """Description of measurement"""

    # Input parameters
    param1 = FloatParameter('Parameter 1', units='unit', default=0)
    param2 = IntegerParameter('Parameter 2', default=10)

    # Output columns
    DATA_COLUMNS = ['Time (s)', 'Measurement 1', 'Measurement 2']

    def startup(self):
        """Initialize instruments"""
        from core.device_manager import device_manager
        self.device = device_manager.get_instrument('device_name')
        self.start_time = time()

    def execute(self):
        """Main measurement loop"""
        for i in range(100):
            data = {
                'Time (s)': time() - self.start_time,
                'Measurement 1': self.device.measurement1,
                'Measurement 2': self.device.measurement2,
            }

            self.emit('results', data)
            self.emit('progress', i)

            if self.should_stop():
                break

    def shutdown(self):
        """Cleanup"""
        pass
```

---

### Window Template

```python
from pymeasure.display.windows import ManagedWindow
from procedures.my_procedure import MyProcedure

class MyWindow(ManagedWindow):
    def __init__(self):
        super().__init__(
            procedure_class=MyProcedure,
            inputs=['param1', 'param2'],
            displays=['param1'],
            x_axis='Time (s)',
            y_axis='Measurement 1',
        )
        self.setWindowTitle('My Measurement')

if __name__ == '__main__':
    import sys
    from pymeasure.display.Qt import QtWidgets
    app = QtWidgets.QApplication(sys.argv)
    window = MyWindow()
    window.show()
    sys.exit(app.exec())
```

---

## Additional Resources

- **PyMeasure Documentation**: https://pymeasure.readthedocs.io/
- **PyMeasure GitHub**: https://github.com/pymeasure/pymeasure
- **PyMeasure Examples**: `pymeasure-master/examples/` (if available)
- **Instrument Drivers**: `pymeasure-master/pymeasure/instruments/` (100+ examples)

---

**End of Reference Guide**
