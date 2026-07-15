# diagnose-connections — Non-Locking GPIB/Serial Diagnostics

Standalone CLI tools for diagnosing GPIB and serial instrument connections without the device-locking problems of NIMAX. Includes comprehensive Oxford Instruments (IPS120, ITC503, Mercury iPS-M) support with protocol-aware command handling.

## Problem

NIMAX acquires an **exclusive lock** when you open a device through NI-VISA. This prevents:
- Running diagnostics while CryoSoft is connected to instruments
- Parallel connectivity checks by agents and the user
- Quick "is the device there?" checks without affecting measurement runs

## Solution

These tools use **non-exclusive, timeout-based queries**:
1. Open a VISA resource with a short timeout (default 500ms)
2. Send a read-only query like `*IDN?`
3. Close immediately and release the resource
4. No exclusive lock is acquired; CryoSoft can continue running

## Installation

Requires two packages (install in `.venv`):

```bash
pip install pyvisa pyserial
```

For NI-VISA backend (recommended for GPIB):
```bash
pip install pyvisa-ni
```

## Quick Start

From the activated `.venv`:

```bash
# Scan GPIB bus 0
python .claude/skills/diagnose-connections/gpib_scan.py

# Scan serial ports
python .claude/skills/diagnose-connections/serial_scan.py

# Query a specific device
python .claude/skills/diagnose-connections/device_query.py gpib0::5::INSTR

# Get JSON status snapshot
python .claude/skills/diagnose-connections/connection_status.py --json
```

## Tools

### `gpib_scan.py`
Probes GPIB addresses 0–30, reports device presence.

```bash
python .claude/skills/diagnose-connections/gpib_scan.py
python .claude/skills/diagnose-connections/gpib_scan.py --json
python .claude/skills/diagnose-connections/gpib_scan.py --verbose --timeout 1000
```

**Output**: Device list at each address with identity string.
**Exit code**: 0 if devices found, 1 if none found, 3 if VISA not available.

### `serial_scan.py`
Enumerates serial ports, optionally tests connectivity with `*IDN?`.

```bash
python .claude/skills/diagnose-connections/serial_scan.py
python .claude/skills/diagnose-connections/serial_scan.py --port COM3 --json
python .claude/skills/diagnose-connections/serial_scan.py --no-query  # Just list ports
```

**Output**: Available ports with device identity (if responding).
**Exit code**: 0 if ports available, 1 if none, 3 if pyserial not available.

### `device_query.py`
Send a read-only query to a specific GPIB or serial device.

```bash
python .claude/skills/diagnose-connections/device_query.py gpib0::5::INSTR
python .claude/skills/diagnose-connections/device_query.py COM3 --baudrate 115200 --json
python .claude/skills/diagnose-connections/device_query.py gpib0::24::INSTR --query "*IDN?"
```

**Output**: Device response or error message.
**Exit code**: 0 if response received, 1 if timeout/error, 2 if bad resource format.

### `connection_status.py`
Generate a persistent JSON snapshot of instrument connectivity.

```bash
# Scan known addresses and write to file
python .claude/skills/diagnose-connections/connection_status.py \
  --addresses "gpib0::5 gpib0::24 COM3"

# Full GPIB+serial scan
python .claude/skills/diagnose-connections/connection_status.py --full-scan

# Print to stdout instead of file
python .claude/skills/diagnose-connections/connection_status.py --stdout
```

**Output**: `cryosoft/logs/connection_status.json` with timestamp and per-device status.
**Exit code**: 0 if at least one device connected, 1 if none, 3 if VISA/serial unavailable.

## Usage from Agents

Example: Agent checking if a Keithley 2614B is available before running a test.

```python
import subprocess
import json

result = subprocess.run(
    ["python", ".claude/skills/diagnose-connections/device_query.py", "gpib0::5::INSTR", "--json"],
    cwd="C:\\Users\\sadit\\OneDrive - JGU\\Projects\\Tools\\Cryosoft",
    capture_output=True,
    text=True,
)
data = json.loads(result.stdout)
if data["success"]:
    print(f"Device found: {data['response']}")
else:
    print(f"Device unavailable: {data['error']}")
```

Or check connection status before a procedure:

```python
result = subprocess.run(
    ["python", ".claude/skills/diagnose-connections/connection_status.py", "--stdout", "--addresses", "gpib0::5"],
    capture_output=True,
    text=True,
)
status = json.loads(result.stdout)
if status["summary"]["connected"] > 0:
    print("Ready to run")
else:
    print("Instrument not responding")
```

## Instrument-Specific Notes

### Oxford Instruments (IPS120, ITC503, Mercury iPS-M)

Oxford instruments use **non-standard command sets** and **different terminators**. These tools include full protocol support.

#### IPS 120-10 (Magnet Power Supply)
- **Interface**: RS-232 (9600 baud), GPIB (addr 25), or TCP-IP (port 7020)
- **Protocol**: SCPI (Standard Commands for Programmable Instruments)
- **Terminator**: LF (line feed, `\n`)
- **Key commands**:
  - `*IDN?` → Device identification
  - `READ:DEV:GRPZ:PSU:SIG:CURR?` → Magnet current (A)
  - `READ:DEV:GRPZ:PSU:SIG:PCUR?` → Persistent current (A)
  - `READ:DEV:GRPZ:PSU:ACTN?` → Status (HOLD/RTOS/RTOZ/CLMP)
  - `READ:DEV:GRPZ:PSU:SIG:SWHT?` → Switch heater (ON/OFF)

**Diagnostic:**
```bash
# Scan for IPS120 on all serial ports
python .claude/skills/diagnose-connections/oxford_scan.py --type IPS120

# Query specific command
python .claude/skills/diagnose-connections/oxford_query.py COM3 --type IPS120 --command current --json

# List all available commands
python .claude/skills/diagnose-connections/oxford_query.py COM3 --type IPS120 --list-commands
```

#### ITC 503 (Temperature Controller)
- **Interface**: GPIB (addr 24) or RS-232 ISOBUS (addr 1, 9600 baud)
- **Protocol**: Legacy (non-SCPI)
- **Terminator**: CR (carriage return, `\r`)
- **RS-232 prefix**: `@1` (ISOBUS addressing)
- **Key commands**:
  - `V` → Firmware version (e.g., `VITC503 1.07`)
  - `X` → Full status byte
  - `R0` → Setpoint temperature (K)
  - `R1`, `R2`, `R3` → Sensor 1-3 temperatures (K)
  - `R5` → Heater output (%)
  - `R6` → Heater voltage

**Diagnostic:**
```bash
# Scan for ITC503 on all serial ports
python .claude/skills/diagnose-connections/oxford_scan.py --type ITC503

# Query specific command
python .claude/skills/diagnose-connections/oxford_query.py COM3 --type ITC503 --command setpoint --json

# List all available commands
python .claude/skills/diagnose-connections/oxford_query.py COM3 --type ITC503 --list-commands
```

#### Mercury iPS-M (Magnet Power Supply)
- **Interface**: RS-232 (9600 baud) or TCP-IP (port 7020)
- **Protocol**: SCPI (identical to IPS120)
- **Terminator**: LF (`\n`)
- **Commands**: Same as IPS120 (READ:DEV:GRPZ:PSU:*)

**Diagnostic:**
```bash
# Scan for Mercury iPS-M
python .claude/skills/diagnose-connections/oxford_scan.py --type MERCURY_PSU

# Query magnet current
python .claude/skills/diagnose-connections/oxford_query.py COM3 --type MERCURY_PSU --command current
```

#### Comprehensive Oxford Scan
```bash
# Try all Oxford instrument types on all ports
python .claude/skills/diagnose-connections/oxford_scan.py --scan-all-types --verbose

# Output as JSON for agent parsing
python .claude/skills/diagnose-connections/oxford_scan.py --scan-all-types --json
```

### Mercury Oxford IPS (Legacy name for IPS120)
See "IPS 120-10" section above.

### Keithley Instruments
Available in both GPIB and serial variants.
- **GPIB versions** (2614B, 2400, etc.): `gpib_scan.py` or `device_query.py gpib0::X::INSTR`
- **Serial versions**: `serial_scan.py` or `device_query.py COMX`
- **Protocol**: IEEE 488.2 (standard SCPI)
- **Command**: `*IDN?` (identity query)
- **Response**: e.g., `Keithley Instruments Inc., Model 2614B, <SN>, v<version>`

## Troubleshooting

**"No devices found" but hardware is connected:**
- Try increasing `--timeout` (e.g., `--timeout 2000` for slow instruments)
- Check USB/serial driver installation (`Device Manager` on Windows)
- Verify GPIB board is recognized: `pyvisa-info` (from PyVISA)

**"VISA not installed":**
```bash
pip install pyvisa pyvisa-ni
```

**"pyserial not installed":**
```bash
pip install pyserial
```

**NIMAX still locks the device:**
- Close NIMAX before running these tools
- These tools should not conflict with CryoSoft (different PyVISA sessions)
- If CryoSoft has a device open, these tools will skip it (timeout, not hang)

**Mercury IPS not responding:**
- Check baud rate (default 9600; verify with instrument manual)
- Try: `device_query.py COM3 --baudrate 9600 --query "*IDN?"`
- Serial timeout is 500ms by default; some instruments may need `--timeout 2000`

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success; at least one device found/queried |
| 1 | Scan completed but no devices found/connected |
| 2 | Command-line error (bad args, invalid resource) |
| 3 | VISA or serial drivers not available |

## Design Notes

- **No exclusive locks**: Each query opens, reads identity, closes immediately
- **Parallel-safe**: Multiple processes (CryoSoft + diagnostic tools) can coexist
- **Non-blocking**: Default timeouts are short (500ms GPIB, 1s serial) to avoid hangs
- **NIMAX-safe**: These are separate PyVISA sessions; NIMAX locks do not affect them
- **Agents-friendly**: JSON output mode for programmatic parsing

## Oxford Instruments Protocol Details

### Protocol Differences

| Aspect | ITC503 | IPS120 / Mercury iPS-M |
|--------|--------|----------------------|
| **Protocol** | Legacy | SCPI |
| **Terminator** | CR (`\r`) | LF (`\n`) |
| **Version query** | `V` | `*IDN?` |
| **Response prefix** | Echo command | STAT: (optional) |
| **ISOBUS prefix** | `@1` (RS-232 only) | None |
| **Timeout** | 3 seconds | 10 seconds |
| **Interface** | GPIB/RS-232 | GPIB/RS-232/TCP-IP |

### Safe Diagnostic Commands

All commands listed below are **read-only** and safe to send continuously:

**IPS120 / Mercury iPS-M (SCPI):**
```
*IDN?                              Device identification
READ:DEV:GRPZ:PSU:SIG:CURR?       Magnet current (A)
READ:DEV:GRPZ:PSU:SIG:CSET?       Current setpoint (A)
READ:DEV:GRPZ:PSU:SIG:PCUR?       Persistent current (A)
READ:DEV:GRPZ:PSU:ACTN?           Action state (HOLD/RTOS/RTOZ/CLMP)
READ:DEV:GRPZ:PSU:SIG:SWHT?       Switch heater (ON/OFF)
READ:DEV:GRPZ:PSU:SIG:VOLT?       Output voltage (V)
READ:DEV:GRPZ:PSU:FVER?           Firmware version
READ:DEV:GRPZ:PSU:SERL?           Serial number
READ:DEV:GRPZ:PSU:CLIM?           Current limit (A)
```

**ITC503 (Legacy):**
```
V                  Firmware version
X                  Full status byte (XnAnCnSnnHnLn)
R0                 Setpoint temperature (K)
R1 – R3            Sensor 1-3 temperatures (K)
R4                 Temperature error (K)
R5                 Heater output (%)
R6                 Heater voltage (V)
R7                 Gas flow
R8 – R10           PID parameters (P, I, D)
R11 – R13          Channel diagnostics
```

### Critical Safety Notes

**IPS120 / Mercury iPS-M:**
- **Heater OFF** = superconducting switch active = persistent mode (no field ramp possible)
- **Heater ON** = resistive switch = PSU can ramp field
- ⚠️ Turning heater ON with PSU current ≠ coil current causes **quench**
- Read `PCUR?` to verify persistent current before enabling heater

**ITC503:**
- Temperature controller is passive (read-only diagnostics are safe)
- Do NOT send write commands (like `S0<value>`) during diagnostics

## Files

- `SKILL.md` — Full skill documentation
- `diagnostic_utils.py` — Shared VISA and serial utilities; Oxford protocol handling
- `gpib_scan.py` — GPIB enumeration CLI
- `serial_scan.py` — Serial port enumeration CLI
- `device_query.py` — Single-device query CLI (IEEE 488.2 standard)
- `oxford_scan.py` — Oxford Instruments scanner (IPS120, ITC503, Mercury iPS-M)
- `oxford_query.py` — Oxford Instruments detailed query tool with command reference
- `connection_status.py` — Persistent status snapshot CLI
- `README.md` — This file
