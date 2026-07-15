---
name: diagnose-connections
description: Non-locking CLI diagnostics for GPIB and serial instrument connections. Enumerate devices, check availability, and query identity without acquiring exclusive locks (unlike NIMAX).
---

# diagnose-connections — GPIB/serial diagnostics without device locking

NIMAX acquires exclusive locks when you open a device, blocking concurrent connections. These standalone CLI tools enumerate buses and query instruments without claiming ownership, so you and agents can verify connectivity in parallel.

## Commands

All tools output JSON (with `--json`) for agent parsing, or human-readable text by default. All run from the activated `.venv`:

### `gpib-scan` — enumerate GPIB devices

```bash
# List all GPIB boards and connected devices
python .claude/skills/diagnose-connections/gpib_scan.py

# Machine-readable output
python .claude/skills/diagnose-connections/gpib_scan.py --json

# Verbose: show every address tested
python .claude/skills/diagnose-connections/gpib_scan.py --verbose
```

Returns: board list, device presence at each address (0–30), device identity if queried successfully.

### `serial-scan` — enumerate serial ports and test connectivity

```bash
# List all serial ports and try basic queries
python .claude/skills/diagnose-connections/serial_scan.py

# Test a specific port
python .claude/skills/diagnose-connections/serial_scan.py --port COM3

# Machine-readable output
python .claude/skills/diagnose-connections/serial_scan.py --json
```

Returns: available ports, device identity (if responds to `*IDN?`), open/unavailable status.

### `device-query` — send a read-only command to a specific instrument

```bash
# Query device identity via GPIB address
python .claude/skills/diagnose-connections/device_query.py gpib0::5::INSTR --query "*IDN?"

# Via serial port
python .claude/skills/diagnose-connections/device_query.py COM3 --query "*IDN?" --baudrate 9600

# Return JSON
python .claude/skills/diagnose-connections/device_query.py gpib0::5::INSTR --query "*IDN?" --json
```

Returns: device response, or connection error with reason.

### `connection-status` — JSON snapshot of all instruments

```bash
# Write a status file (cryosoft/logs/connection_status.json)
python .claude/skills/diagnose-connections/connection_status.py

# Print to stdout instead
python .claude/skills/diagnose-connections/connection_status.py --stdout

# Check only specific addresses (e.g., your known instruments)
python .claude/skills/diagnose-connections/connection_status.py --addresses "gpib0::5 gpib0::24 COM3"
```

Returns: a JSON object with timestamp, each instrument's status (connected/unavailable), and last successful query.

## How to use from agents

Each tool returns machine-readable JSON when you pass `--json`. For example, in a skill or agent:

```python
import subprocess
import json

result = subprocess.run(
    ["python", ".claude/skills/diagnose-connections/serial_scan.py", "--json"],
    cwd="C:\Users\sadit\OneDrive - JGU\Projects\Tools\Cryosoft",
    capture_output=True,
    text=True,
)
ports = json.loads(result.stdout)
for port in ports:
    if port["available"]:
        print(f"Port {port['name']} is open and responding")
```

## Non-locking design

- **PyVISA in timeout mode**: opens resources with a short timeout and closes immediately after reading identity. No exclusive lock is acquired.
- **Serial enumeration**: uses `pyserial`'s `comports()` to list ports without opening them; optional read test is non-blocking.
- **No permanent connections**: all diagnostics are fire-and-forget queries; resources are released immediately.

## Limitations

- **Mercury Oxford IPS** (often serial/LAN): tests serial ports; LAN connections require a different approach (see `/connection-status` with `--addresses`).
- **Keithley instruments** (GPIB/serial variants): both `gpib-scan` and `serial-scan` will detect them if they respond to `*IDN?`.
- **Speed**: GPIB scans can take 5–10 seconds (timeout per address); use `--verbose` to monitor progress.
- **Shared buses**: if another process (like CryoSoft itself) is using a GPIB resource, the scan skips that address to avoid conflicts.

## Exit codes

All tools return:
- `0`: scan completed, at least one device found
- `1`: scan completed, no devices found (or all unavailable)
- `2`: command-line error or invalid resource string
- `3`: VISA/port enumeration unavailable (NI-VISA not installed, or no serial drivers)
