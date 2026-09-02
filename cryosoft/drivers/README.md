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
mutators return `None`. Drivers raise `CryoSoftCommunicationError` on bus failure
and `CryoSoftInstrumentError` (a subclass of it) when the instrument itself
refused the command — see the **driver error-reporting standard** below. They do
not raise safety errors (that is the VI layer's job).

## Interface contract
Enforced mechanically by `tests/test_conformance.py`:
- Each module defines **exactly one public class** whose `__init__` takes one
  required argument (the resource string) and is importable from
  `cryosoft.drivers.*`.
- Every driver exposes `get_idn()` taking no arguments.
- Every driver exposes `close()` taking no arguments — the driver half of the
  **connection lifecycle** (GLOSSARY.md): release the bus session, hand the
  instrument back to LOCAL where it has that concept (GPIB `GTL`, Oxford `C2`/
  `LU`, the 705's REN drop), send no instrument-state command, and never raise
  — a disconnect must always succeed. Idempotent. A closed driver is never
  reopened in place; the Station builds a fresh instance to reconnect, so sim
  twins model the release by failing every command afterwards (`_check_error`).
- `ensure_connected()` is deliberately **NOT** part of this contract — it
  would contradict the rule directly above. It is an OPT-IN capability,
  present only on firmware that genuinely supports resuming a session in
  place (e.g. `tensormeter_rtm2.py`), duck-typed via `getattr` by
  `BaseVirtualInstrument._attach()` (the declared `detach_when_idle`
  standard, GLOSSARY.md's **Availability**) — never called on a driver that
  does not implement it, and never a substitute for the Station rebuilding a
  fresh instance on a real reconnect (`Station.connect_instrument()`).
- Every driver exposes `safe_shutdown()` taking no arguments — the
  **safe-shutdown standard** below. On a sim it must additionally be
  idempotent and land in the state its own `_is_in_safe_state()` predicate
  declares.
- Every state-changing method verifies its write and documents how — the
  **driver error-reporting standard** below — raising
  `CryoSoftInstrumentError` with the instrument's own code.
- Sim drivers must construct with a dummy resource string (no hardware).
- A real driver and its `sim_<name>.py` twin must expose **identical public
  APIs** (`test_sim_real_driver_api_parity`). This parity test pairs twins by the
  `sim_<name>` filename only. Two shipped pairs match by shared API but not by
  filename and are therefore NOT auto-parity-checked: `oxford_mercury_ips` ↔
  `sim_oxford_ips120`, and `lakeshore_335` (which reuses `SimOxfordITC503`, minus
  the needle valve). See "code-vs-doc notes" implications below.

## The driver error-reporting standard

**Every method that programs a mode or a setpoint must verify its write, and
document how.** A bus write that returns without an exception proves only
that the bytes left the computer. Most instruments here accept a command they
cannot execute, record the reason somewhere only a deliberate read will find,
and answer the bus normally — so without verification the caller believes it
set a current, the instrument disagrees, and every number downstream is
fiction. This is not hypothetical: live commissioning on 2026-07-22 found a
real 6221 rejecting *every* DC-mode current-set call with `-221 "Settings
conflict"` for an entire session, visible only on the instrument's own front
panel, with nothing in `cryosoft.log`.

Verification comes in three forms. A driver uses whichever its protocol
offers and says so in its docstring:

1. **Error queue** — a SCPI instrument queues the refusal. Poll `:SYST:ERR?`
   after the write; `0,"No error"` is the only clean answer. An unparseable
   reply is reported as an error, never assumed clean: the standard does not
   guess in the instrument's favour.
2. **Status byte** — no queue, but an IEEE-488.2 Standard Event Status
   Register. Read `*ESR?` after the write and treat the command/execution/
   query-error bits as a refusal. Reading clears it, so each check leaves a
   clean slate.
3. **Protocol acknowledgement** — every command is answered. An Oxford
   ISOBUS instrument echoes the command and replies `?`+command when it will
   not carry it out; a Mercury iPS answers each `SET:` with a `STAT:` line
   carrying `DENIED`/`INVALID`; the RTM2 echoes an accepted setting back as a
   state update (and is free to coerce the value, so the echo — not the value
   sent — is the truth about what is in force).
4. **Explicit readback** — the last resort, for an instrument that reports
   nothing at all. Read the state back and compare it with what was asked
   for. This is the whole of the standard the Keithley 705 can support.

A verified refusal is raised as `CryoSoftInstrumentError`
(`core/exceptions.py`), carrying the instrument's own `code` and
`instrument_message` verbatim plus the `context` — the driver call that was
refused, which the instrument cannot know and the reader always needs. It
subclasses `CryoSoftCommunicationError` so every layer that already treats a
driver call as fallible keeps working unchanged. Two rules keep the checks
from becoming a hazard of their own: **the checker never fails a working
call** (a bus failure while reading the queue/register is swallowed), and
**recovery paths drain rather than raise** (`safe_shutdown()`,
`stop_delta_mode()`), so an error queued by an abandoned sequence is never
charged to the next, innocent command.

| Driver | Verification | Refusal `code` |
|---|---|---|
| `keithley_6221.py` | Error queue (`:SYST:ERR?`) after output/current/compliance/range writes, after the delta programming sequence, and after both `:SOUR:DELT:ARM` and `:INIT:IMM` | the SCPI code, e.g. `-221` |
| `keithley_2182a.py` | Error queue (`:SYST:ERR?`) after the range and continuous-initiation writes | the SCPI code, e.g. `-222` |
| `lakeshore_335.py` | Status byte (`*ESR?`) after every setter | `ESR:0x<bits>` |
| `keithley_705.py` | Explicit readback of the G2 buffer dump after every open/close | `READBACK_MISMATCH` |
| `oxford_ilm200.py` / `oxford_ilm210.py` | ISOBUS acknowledgement on every command | `?` |
| `oxford_itc503.py` | ISOBUS acknowledgement, surfaced by pymeasure as `OxfordVISAError` | `?` |
| `oxford_mercury_ips.py` | `STAT:` acknowledgement on every `SET:` | `DENIED` / `INVALID` |
| `tensormeter_rtm2.py` | Echoed state update on every setting | `PROTOCOL` / `NO_CONFIRMATION` |

**Sim twins model the refusal, not just the physics.** Each sim raises the
same typed error with the same code its real twin would raise after reading
the instrument, so a wrong command sequence fails in a test instead of on
hardware. The sims deliberately keep the *silence* of the real failure too:
the refused value does not change, exactly as on the bench. Tests live in
`tests/test_l0_driver_errors.py`, one pair per instrument (real driver with a
mocked session, sim twin driven through the wrong sequence).

Model only what the instrument actually does. A refusal the hardware has
never been observed to produce is fiction in the opposite direction, and it
will fail honest callers: the 6221 sim deliberately does *not* refuse a
compliance write while delta is armed, because the real driver's own delta
sequence writes `:SOUR:CURR:COMP` in exactly that state.

## The safe-shutdown standard

**Every driver exposes `safe_shutdown()`**, taking no arguments: one
unconditional call that leaves the instrument in a documented safe idle
state, so anything that has to abandon a sequence — a failed procedure, an
emergency stop, an agent that stopped answering — has one call to make on
every instrument without knowing which one it is talking to. Duck-typed like
`get_idn()`/`close()`; there is no `DriverBase`, so
`tests/test_conformance.py` is the contract.

Four rules:

- **Idempotent.** A second call changes nothing. Machine-checked on the sims
  by comparing the whole instance state across two calls.
- **Never raises.** A caller reaching for this is already handling a failure
  and must not be handed a second one; per-command failures are logged at
  WARNING and the sequence continues.
- **Callable from any leftover state**, and the docstring says which ones it
  recovers from.
- **Safe is not off.** Safe idle means *this instrument stops being able to
  do harm*, which is instrument-specific and often not the tidiest-looking
  state. Each driver documents its own, and each sim declares it as an
  executable predicate `_is_in_safe_state()` (private, so the real/sim public
  API parity contract stays intact) that the conformance test asserts.

| Instrument | Safe idle state | Deliberately left alone |
|---|---|---|
| Keithley 6221 | Engine aborted, autorange back on, 0 A, output off, error queue drained | — |
| Keithley 2182A | Single-shot (`:INIT:CONT OFF`), error queue drained | the measurement range (a setting, not a hazard) |
| Keithley 705 | Every channel open, pole mode re-asserted | — |
| Lakeshore 335 | Heater range `OFF`, manual output 0 % | the setpoint (heats nothing with the range off) |
| Oxford ITC 503 | Heater `MANUAL` at 0 % | the needle valve (slamming it shut can strand VTI cooling) and the setpoint |
| Oxford ILM 200 / 210 | Pulsed refresh rate, front panel handed back | the level reading itself, which is a safety input and must keep arriving |
| Oxford Mercury iPS / IPS 120 | `HOLD` — the magnet stays at field | the switch heater, and the field: a fast dump is how magnets quench |
| Tensormeter RTM2 | All four source setpoints (AC/DC current, AC/DC voltage) at zero | ranges, analysis mode, averaging, switch matrix |
| Keithley 2400 (sim-only) | 0 A sourced | compliance and range |
| Lock-in (sim-only) | Oscillator amplitude 0 V | frequency, harmonic, time constant, reference source |
| Rotator (sim-only) | Stopped where it is | the angle — an unattended move home is itself unwatched motion |

## How to add a new module
1. Write the real driver: one public class, `__init__(self, resource: str)`,
   `get_idn()`, `close()`, `safe_shutdown()`, and the instrument methods the
   VI needs. Verify every state-changing write (error queue, status byte,
   protocol acknowledgement, or readback) and say which in the docstring. Add the PEP
   257 header docstring (Input/Process/Output). `__init__` may configure the
   *link* (timeouts, terminations, serial parameters, and the remote-access
   enablement without which the link carries no commands at all — always the
   front-panel-*unlocked* variant) but must send nothing that changes what the
   instrument is doing: that is a setup command and belongs in the VI's
   `initiate()`. See the **connection lifecycle** in GLOSSARY.md.
2. Write the sim twin as `sim_<name>.py` with the identical public API, modelling
   the physics and the failure modes that matter for testing — including the
   refusals its real twin's verification would surface, and a private
   `_is_in_safe_state()` declaring where `safe_shutdown()` lands.
3. Add behaviour tests for the sim to `tests/test_l0_simulated.py` (or a focused
   file) and an error-reporting pair to `tests/test_l0_driver_errors.py`; the
   conformance tests cover the contract automatically the moment the files
   exist. A bench test that opens a real resource gets the `hardware` marker
   and goes in `tests/test_bench_hardware.py`.
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
  SCPI); shares the `SimOxfordITC503` public API minus the needle valve, and
  also has its own `sim_lakeshore_335.py`. tests: `tests/test_conformance.py`,
  `tests/test_l0_driver_errors.py`.
- `sim_lakeshore_335.py` — `SimLakeshore335`: exponential thermal settling plus
  PID/heater/sensor-curve state; `_loaded_user_curves` models which USER curve
  slots actually hold a curve, so assigning an empty one is refused.
  tests: `tests/test_l0_lakeshore_335.py`.
- `oxford_ilm200.py` — `OxfordILM200`: real cryogen level meter over the Oxford
  ISOBUS protocol; `get_helium_level`, `get_nitrogen_level`,
  `get/set_refresh_rate`. tests: `tests/test_conformance.py`.
- `sim_oxford_ilm200.py` — `SimOxfordILM200`: same API; slowly drifting levels
  and a 3-mode refresh rate; `_force_helium_level` hook for low-helium tests.
  tests: `tests/test_l0_simulated.py`, `tests/test_l0_new_drivers.py`.
- `oxford_ilm210.py` / `sim_oxford_ilm210.py` — `OxfordILM210` /
  `SimOxfordILM210`: the 210's ISOBUS level meter, same API and same ISOBUS
  `?` refusal handling as the 200; the sim's `_channels_fitted` models a
  channel with no probe. tests: `tests/test_l0_ilm210.py`,
  `tests/test_l0_driver_errors.py`.
- `tensormeter_rtm2.py` / `sim_tensormeter_rtm2.py` — `TensormeterRTM2` /
  `SimTensormeterRTM2`: the RTM2 measurement box over its own protocol
  library (not PyVISA); four independent source knobs, a switch matrix, and
  the echoed-state-update acknowledgement that is its verification. tests:
  `tests/test_tensormeter_measurement_vi.py`, `tests/test_l0_driver_errors.py`.
- `sim_lockin.py` — `SimLockIn`: internal-oscillator, single-demodulator
  lock-in with a harmonic response model. Sim-only. tests:
  `tests/test_l0_new_drivers.py`.
- `sim_rotator.py` — `SimRotator`: motorised sample-rotation stage, modelling
  travel toward a setpoint at a finite rate. Sim-only. tests:
  `tests/test_l0_new_drivers.py`.
- `__init__.py` — package marker (docstring only). tests: none.
