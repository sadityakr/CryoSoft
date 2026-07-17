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
  `params` `initiate()` will receive, computed before arming the hardware.
- `initiate(**params) → None` — arm/configure the hardware. Accepts the
  `measurement_parameters` keys, all defaulted (so `initiate()` is valid for a
  bulk Initiate-All). `@control`-decorated where the VI exposes arming to the GUI.
- `take_reading() → dict` — take ONE datapoint. No arguments. Returns exactly
  `measurement_data_keys` (arrays sized as `data_arrays` declared) plus every
  `measurement_scalar_columns` key. A VI whose instrument can return fewer points
  pads the arrays to the declared length with `float("nan")` and reports the true
  count in its scalar column. This fixed-shape guarantee prevents HDF5 layout
  mismatches mid-run.
- `standby() → None` — safe-off idle state.
- `ping() → bool` — IDN check on all drivers.
- `reading_variants(vi_name, params) → tuple[ReadingVariant, ...]` — OPTIONAL
  reading-loop hook (default `()`). Return two or more `ReadingVariant`s (a
  suffix `key` + the `Command`s to run before that reading, all targeting
  `vi_name`) to make the generic sweep procedure take one reading per variant
  at every sweep point, columns suffixed `{name}__{key}` (composing with switch
  routes as `{name}__{key}__{route}`). Variants never change the reading's
  shape. A parameter whose value changes the variants (e.g. a `bipolar`
  checkbox) must be declared `structural=True` so the GUI re-derives the
  live-plot keys. Full contract in `MeasurementInstrumentBase`'s docstring.

## Interface contract
DC measurement classes inherit `DCMeasurementBase` (which fixes the DC-resistance
shape: `readings_per_point` samples of `voltage_V` and `current_A`). Other
methods inherit `MeasurementInstrumentBase` directly. Both bases live in
`virtual_instruments/base.py`; `MeasurementInstrumentBase` carries the full
written standard in its docstring. `tests/test_conformance.py` enforces the
standard (declaration validity, lifecycle presence, and a sim round-trip) for
every measurement VI automatically.

## How to add a new measurement VI
1. Subclass `DCMeasurementBase` (for a DC-resistance method) or
   `MeasurementInstrumentBase` (any other protocol).
2. Declare `measurement_parameters` (ParamSpecs), `measurement_data_keys`, and —
   only if the instrument can return fewer readings than requested —
   `measurement_scalar_columns`.
3. Implement `data_arrays(params)`, `initiate(**params)`, `take_reading()`,
   `standby()` (and `ping()`). Keep `@control` on `initiate()` if the GUI should
   be able to arm it. Pad short returns to the declared length with
   `float("nan")` and report the true count in a scalar column. Override
   `reading_variants(vi_name, params)` only if a point needs several readings
   under different configurations (see the Exit section above).
4. If the VI needs a driver role not already in
   `tests/test_conformance.py::_SIM_MEASUREMENT_DRIVER_CLASSES`, add its sim
   driver there so the round-trip conformance test can build it.
5. Register in `devices.yaml`; add behaviour tests to `tests/test_l1_new_vis.py`.

## Files
- `dc_separate_measurement.py` — `DCSeparateMeasurementVI`: Keithley 6221 source +
  2182A nanovoltmeter, simple DC mode. Its `bipolar` parameter is the reference
  `reading_variants` implementation: each sweep point measured at +current and
  -current (`set_source_current` commands, columns `__pos` / `__neg`). tests:
  `tests/test_measurement_dc_vi.py`, `tests/test_l1_new_vis.py`
  (`TestDCSeparateMeasurementVI`), `tests/test_new_procedures.py` (reading loop).
- `dc_single_instrument.py` — `DCSingleInstrumentVI`: Keithley 2400 SMU,
  single-instrument DC mode with the same method contract. tests:
  `tests/test_l1_new_vis.py` (`TestDCSingleInstrumentVI`).
- `measurement_delta_mode.py` — `DeltaModeMeasurementVI`: Keithley 6221 + 2182A in
  delta-mode (reverses current polarity each reading for offset cancellation).
  Pads short delta returns to `n_readings` with NaN and reports `n_valid`. tests:
  `tests/test_l1_virtual_instruments.py`.
- `__init__.py` — package marker. tests: none.
