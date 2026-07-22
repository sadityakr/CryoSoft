# virtual_instruments/measurement/

## Purpose
Virtual instruments for electrical transport measurements. Every class here is a
**measurement method**: a self-describing measurement VI that declares its own
GUI knobs and output shape and implements one uniform lifecycle, so a generic
procedure can run any of them without knowing which instrument or protocol is
behind it. The standard is defined and documented on
`MeasurementInstrumentBase` in `virtual_instruments/base.py`.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
Driver dicts and optional `init_params` (none required for current classes).
- `DCSeparateMeasurementVI`: `{"source": <K6221>, "meter": <K2182A>}`
- `DCSingleInstrumentVI`: `{"main": <K2400 SMU>}`
- `DeltaModeMeasurementVI`: `{"source": <K6221>, "meter": <K2182A>}`
- `LockInHarmonicMeasurementVI`: `{"lockin": <lock-in amplifier>}`,
  `init_params: {series_resistance_ohm}` (the excitation series resistor, a
  setup wiring constant).

## Exit (what goes out)
The measurement-method standard (all classes obey it):

Self-description (class attributes)
- `measurement_parameters: dict[str, ParamSpec]` — the VI's GUI-facing knobs,
  the single owner of those specs.
- `measurement_data_keys: list[str]` — the array names `take_reading()` returns.
- `measurement_scalar_columns: dict[str, str]` — optional extra per-point scalar
  columns (name → "float"/"int"), e.g. `n_valid` for a VI that can return fewer
  readings than requested.

Uniform lifecycle (methods)
- `data_arrays(params) → {array_name: length}` — output shape for the same
  `params` `initiate_measurement()` will receive, computed before arming the hardware.
- `initiate_measurement(**params) → None` — arm/configure the hardware. Accepts
  the `measurement_parameters` keys, all defaulted. `@control(panel=False)`-
  decorated where the VI exposes arming to the GUI (front panel only, never the
  compact card). Deliberately NOT named `initiate`: the plain lifecycle
  `initiate()` on a measurement VI is a harmless connection check (pings the
  drivers, raises `CryoSoftCommunicationError` when unreachable), so a bulk
  Initiate-All can never start a source current.
- `take_reading() → dict` — take ONE datapoint. No arguments. Returns exactly
  `measurement_data_keys` (arrays sized as `data_arrays` declared) plus every
  `measurement_scalar_columns` key. A VI whose instrument can return fewer points
  pads the arrays to the declared length with `float("nan")` and reports the true
  count in its scalar column. This fixed-shape guarantee prevents HDF5 layout
  mismatches mid-run.
- `standby() → None` — safe-off idle state.
- `ping() → bool` — IDN check on all drivers.
- `reading_setters: dict[str, str]` — OPTIONAL reading-loop declaration
  (default `{}`): maps a `measurement_parameters` name to the cheap setter
  method that reprograms just that quantity between readings without
  re-arming (e.g. `{"current_A": "set_source_current"}`). One entry is all a
  VI declares — the generic sweep procedure offers the parameter in its
  Reading loop slots, dispatches the setter before each value's reading,
  suffixes columns with per-slot index labels (`{name}__A{i}__B{j}`), and
  stores the label -> value map in the HDF5 metadata. Setters must accept
  the parameter under its own name and never change the reading's shape.
  The same standard lives on `BaseVirtualInstrument` (plus
  `reading_parameters` / `reading_safe_off`), so non-measurement VIs like
  the switch participate identically. Full contract in the base docstrings.

## Interface contract
DC measurement classes inherit `DCMeasurementBase` (which fixes the DC-resistance
shape: `readings_per_point` samples of `voltage_V` and `current_A`). Other
methods inherit `MeasurementInstrumentBase` directly. Both bases live in
`virtual_instruments/base.py`; `MeasurementInstrumentBase` carries the full
written standard in its docstring. `tests/test_conformance.py` enforces the
standard (declaration validity, lifecycle presence, and a sim round-trip) for
every measurement VI automatically.

## Shared-instrument mode discipline
Several measurement methods here can be wired to the SAME physical driver
instance (e.g. `dc_measurement` and `keithley_delta_mode` both reference the
one `keithley_6221` entry in `devices.yaml`'s `real_drivers`), because only
one measurement VI is armed at a time — but the underlying instrument can have
more than one mutually exclusive SCPI/operating mode (plain DC output vs. the
bipolar delta engine). A driver method that establishes one of these modes
MUST be **idempotent and self-recovering**: it must reassert its own required
mode unconditionally, never assume the instrument is already in a compatible
state left over from whichever VI ran last. This is the primary defense (see
`Keithley6221.set_current()`'s unconditional `:SOUR:CURR:MODE FIX`, mirroring
how `_program_delta_mode()` already always leads with `:SOUR:SWE:ABOR`).
`stop_delta_mode()`-style teardown methods should still also return the
instrument to a documented idle baseline as defense-in-depth (useful for a
human inspecting the instrument between runs), but a VI's
`initiate_measurement()` must
never *rely* on a previous VI's `standby()` having been called correctly. A
sim driver modeling more than one such mode should track it (e.g.
`SimKeithley6221._mode`) so a VI that skips the defensive reassertion fails in
tests, not on hardware — see `tests/test_l1_virtual_instruments.py`'s
shared-6221 handoff test for the pattern.

## How to add a new measurement VI
1. Subclass `DCMeasurementBase` (for a DC-resistance method) or
   `MeasurementInstrumentBase` (any other protocol).
2. Declare `measurement_parameters` (ParamSpecs), `measurement_data_keys`, and —
   only if the instrument can return fewer readings than requested —
   `measurement_scalar_columns`.
3. Implement `data_arrays(params)`, `initiate_measurement(**params)`,
   `take_reading()`, `standby()` (and `ping()`). Keep `@control(panel=False)` on
   `initiate_measurement()` if the GUI should
   be able to arm it. Pad short returns to the declared length with
   `float("nan")` and report the true count in a scalar column. Declare a
   `reading_setters` entry (parameter → setter method) for any parameter the
   reading loop should be able to vary per point (see the Exit section above).
4. If the VI needs a driver role not already in
   `tests/test_conformance.py::_SIM_MEASUREMENT_DRIVER_CLASSES`, add its sim
   driver there so the round-trip conformance test can build it.
5. Register in `devices.yaml`; add behaviour tests to `tests/test_l1_new_vis.py`.

## Files
- `measurement_dc_mode.py` — `DCModeMeasurementVI`: Keithley 6221 source + 2182A
  nanovoltmeter, plain DC mode (current set once, voltage polled repeatedly —
  contrast with `dc_separate_measurement.py`'s reference `reading_setters`
  entry and `measurement_delta_mode.py`'s polarity-reversing delta engine).
  Declares `reading_setters` `{"current": "set_dc_current"}`; the setter
  reprograms the source in place with no re-arm cost. Also exposes
  `read_now()`, a `@control(panel=False)` bench-test hook (front panel only,
  never the compact card) distinct from `take_reading()`: it calls
  `take_reading()` and caches the result in the `last_voltage_V` /
  `last_mean_voltage_V` / `last_n_valid` `@monitored` fields so an operator
  can confirm a configured current yields sane readings before running a
  procedure. tests: `tests/test_l1_virtual_instruments.py`
  (`test_dc_mode_measurement_vi_lifecycle`, `test_dc_mode_read_now_bench_test`).
- `dc_separate_measurement.py` — `DCSeparateMeasurementVI`: Keithley 6221 source +
  2182A nanovoltmeter, simple DC mode. Declares the reference `reading_setters`
  entry `{"current_A": "set_source_current"}`, so the reading loop can measure
  a user-entered current list (e.g. `1e-6, -1e-6`) at every sweep point
  (per-slot index-label columns). tests: `tests/test_measurement_dc_vi.py`,
  `tests/test_l1_new_vis.py` (`TestDCSeparateMeasurementVI`),
  `tests/test_new_procedures.py` (reading loop).
- `dc_single_instrument.py` — `DCSingleInstrumentVI`: Keithley 2400 SMU,
  single-instrument DC mode with the same method contract. tests:
  `tests/test_l1_new_vis.py` (`TestDCSingleInstrumentVI`).
- `measurement_delta_mode.py` — `DeltaModeMeasurementVI`: Keithley 6221 + 2182A in
  delta-mode (reverses current polarity each reading for offset cancellation).
  Pads short delta returns to `n_readings` with NaN and reports `n_valid`.
  Declares `reading_setters` `{"current": "set_delta_current"}`; unlike the DC
  VI the setter **stops and re-arms** the engine (delta latches its peak current
  at arm time), so each loop step pays a delta start-up and its first readings
  include the settling transient. `current` is a peak amplitude that delta
  reverses each cycle, so looping the sign is redundant. tests:
  `tests/test_l1_virtual_instruments.py`.
- `lockin_harmonic.py` — `LockInHarmonicMeasurementVI`: lock-in first/second
  harmonic (1f/2f) measurement, sourced by the lock-in's own internal
  oscillator through a series resistor. A single-demodulator lock-in reports
  one harmonic at a time, so `take_reading()` switches `set_harmonic(1)` /
  `set_harmonic(2)` between reads rather than assuming simultaneous
  multi-harmonic hardware. External-source excitation (Keithley 6221 synced
  to a common reference) is a scoped follow-up, not yet implemented — it
  needs new AC/waveform driver capability on the 6221 that doesn't exist yet.
  tests: `tests/test_l1_new_vis.py` (`TestLockInHarmonicMeasurementVI`).
- `__init__.py` — package marker. tests: none.
