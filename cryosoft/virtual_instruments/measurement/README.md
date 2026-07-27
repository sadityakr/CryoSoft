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
- `measurement_data_keys: list[str]` — the raw-sample array names
  `take_reading()` returns, each ending `_array` (e.g. `"voltage_V_array"`).
- `measurement_scalar_columns: dict[str, str]` — per-point scalar columns
  (name → "float"/"int"). The **mean/error/array convention**: for every
  quantity behind a `"{quantity}_array"` key, this carries `"{quantity}"`
  (the mean — what the GUI plots) and `"{quantity}_error"` (the standard
  error of the mean), both dtype "float"; plus optional VI-specific extras
  unrelated to any array, e.g. `n_valid`. Build both attributes with
  `MeasurementInstrumentBase.quantity_columns(*names)` rather than
  hand-writing the suffixes — see the pattern in `measurement_dc_mode.py`.

Uniform lifecycle (methods)
- `data_arrays(params) → {array_name: length}` — output shape (the `_array`
  keys) for the same `params` `initiate_measurement()` will receive, computed
  before arming the hardware.
- `initiate_measurement(**params) → None` — arm/configure the hardware. Accepts
  the `measurement_parameters` keys, all defaulted. `@control(panel=False)`-
  decorated where the VI exposes arming to the GUI (front panel only, never the
  compact card). Deliberately NOT named `initiate`: the plain lifecycle
  `initiate()` on a measurement VI is a harmless connection check (pings the
  drivers, raises `CryoSoftCommunicationError` when unreachable), so a bulk
  Initiate-All can never start a source current.
- `take_reading() → dict` — take ONE datapoint. No arguments. For every
  quantity, returns the mean/error/array triple: the raw-sample array
  (`"{quantity}_array"`, NaN-padded to the length `data_arrays` declared),
  the mean (`"{quantity}"`), and the standard error of the mean
  (`"{quantity}_error"`) — computed over the VALID samples with
  `self.mean_and_sem(...)`. Also returns every other
  `measurement_scalar_columns` key (e.g. `n_valid`, see below). A VI whose
  instrument can return fewer points pads the array with `float("nan")` and
  computes the mean/error/`n_valid` over the samples actually delivered,
  BEFORE that padding is applied. This fixed-shape guarantee prevents
  HDF5 layout mismatches mid-run.
- `standby() → None` — safe-off idle state.
- `ping() → bool` — IDN check on all drivers.
- `reading_setters: dict[str, str]` — OPTIONAL reading-loop declaration
  (default `{}`): maps a `measurement_parameters` name to the cheap setter
  method that reprograms just that quantity between readings without
  re-arming (e.g. `{"current_A": "set_source_current"}`). One entry is all a
  VI declares — the generic sweep procedure offers the parameter in its
  Reading loop slots, dispatches the setter before each value's reading, and
  every measurement column carries a real `(n_loop1, n_loop2)` array axis in
  HDF5 (never suffixed names) — axis index → value lives in the run's
  metadata as `procedure_params["loop1_values"]` / `["loop2_values"]`.
  Setters must accept the parameter under its own name and never change the
  reading's shape. The same standard lives on `BaseVirtualInstrument` (plus
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

## Under-delivery: the n_valid standard
A VI whose `take_reading()` can deliver fewer raw samples than `data_arrays()`
declared for a quantity MUST report an `n_valid` scalar column (dtype `"int"`,
added to `measurement_scalar_columns`) — the number of samples the mean and
SEM were actually computed over. The returned array preserves the delivered
rows verbatim (including any instrument-emitted NaN, e.g. a ratiometric
divide-by-zero), padded with `float("nan")` out to the declared length. The
mean, SEM, and `n_valid` are computed over the delivered samples BEFORE
padding — never by filtering NaN out of the padded array, which would
conflate CryoSoft's own padding with a NaN the instrument itself emitted. Row
selection when more samples arrived than requested (first-*n* vs last-*n*) is
per-instrument physics — documented on that VI's own `take_reading()`, not
standardised here. A VI whose instrument always delivers exactly the
requested sample count has nothing to report and may omit `n_valid`
entirely. Full text: `MeasurementInstrumentBase`'s docstring, "Under-delivery:
the `n_valid` standard".

## Externally configured instruments
Some instruments expose far more configuration surface than a VI wraps
(analysis modes, pulse trains, reference muxing, preamp modes, …). A VI may
support an operator configuring the instrument with the vendor's own tool and
letting CryoSoft run only the measurement — arm the data path, trigger,
read, and save a fixed-shape data block — without touching that
configuration. This is the `configured_externally` standard on
`MeasurementInstrumentBase`, motivated by single-client instrument firmware
where the vendor tool and CryoSoft are mutually exclusive at the instrument,
and it applies to every externally configured VI, not just the one that
first needed it.

`MeasurementInstrumentBase.__init__` reads the optional init param
`configured_externally: bool` (default `False`) from `init_params` and
stores it as `self._configured_externally` — config-driven, per-VI, via
`devices.yaml`'s `init_params`, with zero per-subclass boilerplate. Omitted
or `False` leaves every existing VI's behavior unchanged.

When `self._configured_externally` is true:
- `initiate_measurement()` MUST NOT write any excitation, analysis, or
  routing parameter to the instrument — the external tool owns them. It
  MUST still: (a) verify connectivity with a TRUE ROUND TRIP — a query that
  fails loudly on a dead or externally-held channel; (b) arm the data path
  (buffers, channel/format selection, anything `take_reading()`'s decode
  depends on); (c) read back from the instrument any value its own timing
  or decoding depends on; and (d) set the internal state `take_reading()` /
  `data_arrays()` require. Inert `measurement_parameters` MUST still be
  accepted (procedures pass them regardless of mode); log one INFO listing
  the ignored parameters.
- `standby()` MUST NOT overwrite externally-owned source state. It resets
  only CryoSoft's own internal arming state and RELEASES the hardware
  resource (the driver's `close()`).

A VI supporting external configuration MUST declare
`externally_owned_parameters: ClassVar[frozenset[str]]` — the
`measurement_parameters` names the external tool owns in that mode
(excitation/analysis/routing). Declare-and-derive: the procedure form
(`active_measurement_parameters`) and the reading-loop registry
(`reading_parameters`) both hide those names automatically once
`configured_externally` is true, with no per-VI form code. Data-path
parameters (e.g. a tensor-component selector, a readings-per-point count —
anything that writes nothing to the instrument) stay off this set, so they
remain operator-controlled, and rendered, in every mode. The empty default
changes nothing for a VI that does not support external configuration;
`tests/test_conformance.py` checks every declared name is a real
`measurement_parameters` key.

A VI that captures a provenance snapshot at arming time SHOULD expose it as
`self.last_settings_snapshot` (a plain `dict`); the sweep procedure
(`SweepMeasureProcedure.measure()`) duck-types this attribute and records it
into the run's HDF5 `/metadata` automatically, once per run, via
`DataManager.record_settings_snapshot()` — no per-VI plumbing required.

**Detached-idle lifecycle**: an externally configured VI holds its
instrument connection only from `initiate_measurement()` to `standby()`.
Born detached — `__init__` releases the connection before returning, so
starting CryoSoft while the vendor tool is open builds cleanly.
`initiate()` / `ping()` verify-and-release (connect, a true round trip,
then close), returning a clean failure verdict rather than raising when the
instrument is currently held by the external tool. `initiate_measurement()`
(re)acquires the connection for the measurement window; `standby()`
releases it again — every run path already ends in `standby()`, so the
instrument frees itself automatically and the operator may attach the
vendor tool at any time between runs. Never reconnect opportunistically in
the background (e.g. from a monitored poll): a wrongly-timed connect can
fail silently against the external tool's session, not loudly. Full text:
`MeasurementInstrumentBase`'s docstring, "Externally configured
instruments".

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
2. Declare `measurement_parameters` (ParamSpecs) and derive
   `measurement_data_keys` / `measurement_scalar_columns` from
   `MeasurementInstrumentBase.quantity_columns(*names)` for each quantity the
   VI measures — plus `n_valid` (dtype `"int"`) if the instrument can return
   fewer readings than requested (MUST, per the "Under-delivery: the
   `n_valid` standard" section above).
3. Implement `data_arrays(params)`, `initiate_measurement(**params)`,
   `take_reading()`, `standby()` (and `ping()`). Keep `@control(panel=False)` on
   `initiate_measurement()` if the GUI should
   be able to arm it. In `take_reading()`, pad short returns to the declared
   length with `float("nan")`, then call `self.mean_and_sem(valid_samples)`
   per quantity to fill in the mean/error and report the true count in a
   scalar column if applicable. Declare a `reading_setters` entry (parameter →
   setter method) for any parameter the reading loop should be able to vary
   per point (see the Exit section above).
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
