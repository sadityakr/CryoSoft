# drivers/

## Purpose
Layer-0 hardware adapters: one plain Python class per physical instrument that
turns a method call into raw bus I/O (SCPI, ISOBUS, and whatever else an
instrument speaks). Every real
driver has a **sim twin** (`sim_*.py`) with an identical public API that models
the instrument's physics, including failure modes (quench on a bad ramp order,
short delta returns, communication errors), so a wrong command sequence fails in
a test instead of on hardware. Nothing above this layer ever talks to a bus
directly.

## Architecture layer
L0 — Drivers. The lowest layer; depends only on `i2as.core.exceptions`
(and `pyvisa` for the real drivers), plus `i2as.core.sim_environment`
for the coupled sims (the **sim-coupling standard** below). Sim drivers
have no third-party dependency beyond numpy, which the camera sim uses for
its frames.

## Entry (what comes in)
`__init__` takes a single VISA resource string and nothing else (e.g.
`"GPIB0::19::INSTR"`, `"ASRL10::INSTR"`, or a `"SIM::..."` placeholder ignored by
sims). The Station factory constructs drivers from the `real_drivers` block of a
config `devices.yaml` and injects them into VIs. Method arguments are SI-ish raw
values (amperes, kelvin, volts, channel-spec strings).

## Exit (what goes out)
Return values are plain floats, bools, strings, and `list[float]` (e.g. delta
readings). Every driver exposes `get_idn() -> str` for reachability checks. State
mutators return `None`. Drivers raise `I2ASCommunicationError` on bus failure
and `I2ASInstrumentError` (a subclass of it) when the instrument itself
refused the command — see the **driver error-reporting standard** below. They do
not raise safety errors (that is the VI layer's job).

## Interface contract
Enforced mechanically by `tests/test_conformance.py`:
- Each module defines **exactly one public class** whose `__init__` takes one
  required argument (the resource string) and is importable from
  `i2as.drivers.*`.
- Every driver exposes `get_idn()` taking no arguments.
- Every driver exposes `close()` taking no arguments — the driver half of the
  **connection lifecycle** (GLOSSARY.md): release the bus session, hand the
  instrument back to LOCAL where it has that concept (GPIB `GTL`, Oxford
  `C2`/`LU`), send no instrument-state command, and never raise
  — a disconnect must always succeed. Idempotent. A closed driver is never
  reopened in place; the Station builds a fresh instance to reconnect, so sim
  twins model the release by failing every command afterwards (`_check_error`).
- `ensure_connected()` is deliberately **NOT** part of this contract — it
  would contradict the rule directly above. It is an OPT-IN capability,
  present only on firmware that genuinely supports resuming a session in
  place, duck-typed via `getattr` by
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
  `I2ASInstrumentError` with the instrument's own code.
- Sim drivers must construct with a dummy resource string (no hardware).
- A real driver and its `sim_<name>.py` twin must expose **identical public
  APIs** (`test_sim_real_driver_api_parity`). This parity test pairs twins by
  the `sim_<name>` filename only, so a sim with no real twin of the same name
  (`sim_keithley_6221`, `sim_oxford_ips120`) is simply not
  auto-paired (`sim_camera`, `sim_xy_stage` likewise); its own L0 tests
  are what hold its API in place.

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
panel, with nothing in `i2as.log`.

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
   not carry it out; an instrument that echoes an accepted setting back as a
   state update is free to coerce the value, so the echo — not the value sent
   — is the truth about what is in force.
4. **Explicit readback** — the last resort, for an instrument that reports
   nothing at all. Read the state back and compare it with what was asked
   for.

A verified refusal is raised as `I2ASInstrumentError`
(`core/exceptions.py`), carrying the instrument's own `code` and
`instrument_message` verbatim plus the `context` — the driver call that was
refused, which the instrument cannot know and the reader always needs. It
subclasses `I2ASCommunicationError` so every layer that already treats a
driver call as fallible keeps working unchanged. Two rules keep the checks
from becoming a hazard of their own: **the checker never fails a working
call** (a bus failure while reading the queue/register is swallowed), and
**recovery paths drain rather than raise** (`safe_shutdown()`,
`stop_delta_mode()`), so an error queued by an abandoned sequence is never
charged to the next, innocent command.

| Driver | Verification | Refusal `code` |
|---|---|---|
| `sim_keithley_6221.py` | Error queue (`:SYST:ERR?`) after output/current/compliance/range writes, after the delta programming sequence, and after both `:SOUR:DELT:ARM` and `:INIT:IMM` | the SCPI code, e.g. `-221` |
| `keithley_2182a.py` | Error queue (`:SYST:ERR?`) after the range and continuous-initiation writes | the SCPI code, e.g. `-222` |
| `lakeshore_335.py` | Status byte (`*ESR?`) after every setter | `ESR:0x<bits>` |
| `sim_camera.py` (sim-only) | Explicit range check, as a camera SDK reports it, on exposure, binning and ROI writes and on a frame requested while disarmed | `EXPOSURE_RANGE`, `BINNING_UNSUPPORTED`, `ROI_RANGE`, `NOT_ARMED` |
| `sim_xy_stage.py` (sim-only) | Explicit range check, as a stage controller reports it, on a move beyond the travel (nothing moves) and a speed outside the controller's range | `TRAVEL_LIMIT`, `SPEED_RANGE` |

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
| Lakeshore 335 | Heater range `OFF`, manual output 0 % | the setpoint (heats nothing with the range off) |
| Oxford IPS 120 (sim-only) | `HOLD` — the magnet stays at field | the field: a fast dump is how magnets quench |
| Sim camera (sim-only) | Sensor disarmed — no exposure can be triggered | exposure, binning and ROI: settings, not hazards |
| Sim XY stage (sim-only) | Both axes stopped where they are | the position: a drive home is a move like any other and can collide with whatever is in the way |

## The sim-coupling standard

**A sim models one instrument. Two sims that share a physical quantity
exchange it through a `SimEnvironment` and never import each other.** The
simulated magnet applies a field; the simulated camera images a sample
that responds to it. Neither knows the other exists: the PSU publishes the
one thing a PSU knows — its output current — into the environment its
resource string names, the environment holds the physics that relates the
two (the coil constant `amperes_per_tesla`, the same number the shipped
configs give the magnet VI), and the camera reads the field at the sample
from that same environment when it takes a frame. `SimKeithley6221`'s
`_paired_meter` is the older, narrower form of the same idea — two halves
of one delta measurement wired together by a test — and stays as it is;
the environment is the form to use whenever the coupling is a physical
quantity rather than a cable.

Three rules:

- **Opt in through the resource string.** A sim joins the environment its
  resource string's `@<name>` suffix names (`"SIM::IPS_Z@imaging"` and
  `"SIM::CAMERA@imaging"` share `imaging`); a string without a suffix gets
  a private world, so two sims built independently in a test stay
  independent unless the test says otherwise. The resource string is the
  one argument every driver takes, so the coupling needs no second
  constructor argument and no config key: the config's `address` is the
  whole declaration.
- **Producers publish, consumers read, the environment knows the
  physics.** A producer publishes whenever the quantity changes and once at
  construction (a fresh PSU sits at zero, and a world shared with an
  earlier instance must not carry that instance's last value forward). A
  consumer reads at the moment it observes (the camera when it exposes a
  frame). The conversion between what is published and what is observed is
  the environment's, never either sim's, so it is written down once.
- **Tests drive the world directly.** `SimEnvironment.applied_field_T` is
  settable, so an L0 test sweeps the field without building a magnet, and
  the L0 suite pins the environment's coil constant to every shipped
  config's `amperes_per_tesla`.

The registry lives in `i2as/core/sim_environment.py` rather than here
because every module in this folder is checked as a driver (one class, one
resource argument, `get_idn`/`close`/`safe_shutdown`), and an environment
is not an instrument.

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
   so `make check` and CI skip it.
4. Reference the driver from a config `devices.yaml` `real_drivers` block.

## Files
Real / sim twins are grouped; each `.py` lists its key methods and owning tests.

- `keithley_2182a.py` — `Keithley2182A`: real nanovoltmeter; `get_voltage`,
  `set_range` / `get_range`. tests: `tests/test_conformance.py`,
  `tests/test_l0_driver_errors.py`.
- `sim_keithley_2182a.py` — `SimKeithley2182A`: same API; returns base voltage +
  Gaussian noise. tests: `tests/test_l0_simulated.py`.
- `lakeshore_335.py` — `Lakeshore335`: real temperature controller (pure PyVISA
  SCPI, IEEE-488.2 status-byte verification); temperature, setpoint, heater
  mode/output/range, PID and sensor-curve access. tests:
  `tests/test_conformance.py`, `tests/test_l0_driver_errors.py`.
- `sim_lakeshore_335.py` — `SimLakeshore335`: exponential thermal settling plus
  PID/heater/sensor-curve state; the heater RANGE gates power delivery exactly
  as the instrument does (Off delivers nothing whatever the mode), and
  `_loaded_user_curves` models which USER curve slots actually hold a curve,
  so assigning an empty one is refused.
  tests: `tests/test_l0_lakeshore_335.py`, `tests/test_l0_driver_errors.py`.
- `sim_keithley_6221.py` — `SimKeithley6221`: AC/DC current source; DC
  `set_current` / `set_source_enabled` / `set_compliance` plus the delta-mode
  sequence (`configure_and_start_delta`, `acquire_delta_readings`,
  `stop_delta_mode`), generating readings from a paired `SimKeithley2182A`;
  `_delta_return_count` hook forces short returns to exercise NaN-padding.
  Sim-only. tests: `tests/test_l0_simulated.py`,
  `tests/test_l0_driver_errors.py`.
- `sim_oxford_ips120.py` — `SimOxfordIPS120`: API-compatible sim of the IPS 120-10
  magnet PSU; models ramping, heater-derived persistent mode, coil-current
  freeze, and a QUENCH when the heater energises across a PSU/coil current
  mismatch; `reset_quench` test hook; publishes its output current to
  its `SimEnvironment` (the sim-coupling standard). Sim-only. tests:
  `tests/test_l0_simulated.py`, `tests/test_l0_driver_errors.py`,
  `tests/test_l0_sim_camera_stage.py`.
- `sim_camera.py` — `SimCamera`: sim widefield camera imaging a magnetic
  domain pattern; `set_exposure_s`/`set_binning`/`set_roi` and their reads,
  `arm`/`disarm`/`is_armed`, `get_frame() -> uint16 (height, width)`,
  `get_sensor_size`. Frame physics: per-pixel switching fields (coercive
  field plus spatially correlated disorder from a fixed seed) so domains
  nucleate and grow with the applied field and remember their state — a
  field sweep gives a hysteresis loop of mean intensity; Gaussian
  illumination, exposure scaling, Poisson shot noise, full-well
  saturation. Reads the field from its `SimEnvironment` (the sim-coupling
  standard). Sim-only. tests: `tests/test_l0_sim_camera_stage.py`.
- `sim_xy_stage.py` — `SimXYStage`: sim two-axis sample stage; per-axis
  `move_to(x_m=None, y_m=None)` (an omitted axis keeps its own move),
  `get_position`/`get_target`, `set_speed`/`get_speed`, `is_moving(axis)`,
  `stop(axis)`; wall-clock motion at the set speed, travel-limit and
  speed-range refusals. Sim-only. tests:
  `tests/test_l0_sim_camera_stage.py`.
- `__init__.py` — package marker (docstring only). tests: none.
