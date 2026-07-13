# drivers/

## Purpose
Layer-0 hardware adapters: one plain Python class per physical instrument that
turns a method call into raw bus I/O (SCPI, DDC, ISOBUS, pymeasure). Every real
driver has a **sim twin** (`sim_*.py`) with an identical public API that models
the instrument's physics, including failure modes (quench on a bad ramp order,
short delta returns, communication errors), so a wrong command sequence fails in
a test instead of on hardware. Nothing above this layer ever talks to a bus
directly.

## Architecture layer
L0 — Drivers. The lowest layer; depends only on `cryosoft.core.exceptions`
(and `pyvisa` / `pymeasure` for the real drivers). Sim drivers have no third-party
dependency.

## Entry (what comes in)
`__init__` takes a single VISA resource string and nothing else (e.g.
`"GPIB0::19::INSTR"`, `"ASRL10::INSTR"`, or a `"SIM::..."` placeholder ignored by
sims). The Station factory constructs drivers from the `real_drivers` block of a
config `devices.yaml` and injects them into VIs. Method arguments are SI-ish raw
values (amperes, kelvin, volts, channel-spec strings).

## Exit (what goes out)
Return values are plain floats, bools, strings, and `list[float]` (e.g. delta
readings). Every driver exposes `get_idn() -> str` for reachability checks. State
mutators return `None`. Drivers raise `CryoSoftCommunicationError` on bus failure;
they do not raise safety errors (that is the VI layer's job).

## Interface contract
Enforced mechanically by `tests/test_conformance.py`:
- Each module defines **exactly one public class** whose `__init__` takes one
  required argument (the resource string) and is importable from
  `cryosoft.drivers.*`.
- Every driver exposes `get_idn()` taking no arguments.
- Sim drivers must construct with a dummy resource string (no hardware).
- A real driver and its `sim_<name>.py` twin must expose **identical public
  APIs** (`test_sim_real_driver_api_parity`). This parity test pairs twins by the
  `sim_<name>` filename only. Two shipped pairs match by shared API but not by
  filename and are therefore NOT auto-parity-checked: `oxford_mercury_ips` ↔
  `sim_oxford_ips120`, and `lakeshore_335` (which reuses `SimOxfordITC503`, minus
  the needle valve). See "code-vs-doc notes" implications below.

## How to add a new module
1. Write the real driver: one public class, `__init__(self, resource: str)`,
   `get_idn()`, and the instrument methods the VI needs. Add the PEP 257 header
   docstring (Input/Process/Output).
2. Write the sim twin as `sim_<name>.py` with the identical public API, modelling
   the physics and the failure modes that matter for testing.
3. Add behaviour tests for the sim to `tests/test_l0_simulated.py` (or a focused
   file); the conformance tests cover the contract automatically the moment the
   files exist.
4. Reference the driver from a config `devices.yaml` `real_drivers` block.

## Files
Real / sim twins are grouped; each `.py` lists its key methods and owning tests.

- `keithley_6221.py` — `Keithley6221`: real AC/DC current source; DC
  `set_current` / `set_source_enabled` / `set_compliance` plus the full delta-mode
  SCPI sequence (`configure_and_start_delta`, `acquire_delta_readings`,
  `stop_delta_mode`). tests: `tests/test_conformance.py`.
- `sim_keithley_6221.py` — `SimKeithley6221`: same API; generates delta readings
  from a paired `SimKeithley2182A`; `_delta_return_count` hook forces short
  returns to exercise NaN-padding. tests: `tests/test_l0_simulated.py`.
- `keithley_2182a.py` — `Keithley2182A`: real nanovoltmeter; `get_voltage`,
  `set_range` / `get_range`. tests: `tests/test_conformance.py`.
- `sim_keithley_2182a.py` — `SimKeithley2182A`: same API; returns base voltage +
  Gaussian noise. tests: `tests/test_l0_simulated.py`.
- `sim_keithley_2400.py` — `SimKeithley2400`: single-instrument SMU that both
  sources current and measures voltage (`set_current`, `get_voltage`,
  `set_compliance`, `set_range`). Sim-only: no real `keithley_2400.py` twin yet.
  tests: `tests/test_l0_new_drivers.py`.
- `keithley_705.py` — `Keithley705`: real scanner / matrix switch over Keithley
  DDC command language (`close_channels`, `open_channels`, `open_all`,
  `closed_channels`). **Command strings (C / N / R / U0) are UNVERIFIED against
  hardware** — must be checked against the 705 manual at bench commissioning
  before first use. tests: `tests/test_conformance.py`.
- `sim_keithley_705.py` — `SimKeithley705`: same API; exclusive-mux model as a
  closed-channel-spec set; `_simulate_error` hook for error injection. tests:
  `tests/test_l0_switch_driver.py`.
- `oxford_mercury_ips.py` — `OxfordMercuryiPS`: real magnet PSU over the Oxford
  SCPI READ:/SET: hierarchy (GRPZ module); `set_current_setpoint` auto-issues
  ACTN:RTOS, plus switch-heater / persistent-mode / `get_status`. tests:
  `tests/test_conformance.py`.
- `sim_oxford_ips120.py` — `SimOxfordIPS120`: API-compatible sim of the IPS 120-10
  PSU; models ramping, heater-derived persistent mode, coil-current freeze, and a
  QUENCH when the heater energises across a PSU/coil current mismatch;
  `reset_quench` test hook. tests: `tests/test_l0_simulated.py`,
  `tests/test_l0_new_drivers.py`.
- `oxford_itc503.py` — `OxfordITC503`: real temperature controller (pymeasure
  wrapper); `get_temperature`, `get/set_setpoint`, `get_heater_output`,
  `get/set_needle_valve` (gas-flow output). tests: `tests/test_conformance.py`.
- `sim_oxford_itc503.py` — `SimOxfordITC503`: same API; exponential thermal
  settling toward setpoint plus needle-valve output. Also serves as the sim
  stand-in for `Lakeshore335`. tests: `tests/test_l0_simulated.py`,
  `tests/test_l0_new_drivers.py`.
- `lakeshore_335.py` — `Lakeshore335`: real temperature controller (pure PyVISA
  SCPI); shares the `SimOxfordITC503` public API minus the needle valve. No
  dedicated sim twin. tests: `tests/test_conformance.py`.
- `oxford_ilm200.py` — `OxfordILM200`: real cryogen level meter over the Oxford
  ISOBUS protocol; `get_helium_level`, `get_nitrogen_level`,
  `get/set_refresh_rate`. tests: `tests/test_conformance.py`.
- `sim_oxford_ilm200.py` — `SimOxfordILM200`: same API; slowly drifting levels
  and a 3-mode refresh rate; `_force_helium_level` hook for low-helium tests.
  tests: `tests/test_l0_simulated.py`, `tests/test_l0_new_drivers.py`.
- `__init__.py` — package marker (docstring only). tests: none.
