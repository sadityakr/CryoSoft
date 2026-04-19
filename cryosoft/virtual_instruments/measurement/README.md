# virtual_instruments/measurement/

## Purpose
Virtual instruments for electrical transport measurements. All classes share the
`DCMeasurementBase` contract (`initiate()` / `take_reading()`) so procedures can
swap between two-instrument and single-SMU setups via YAML config alone.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
Driver dicts and optional `init_params` (none required for current classes).
- `DCSeparateMeasurementVI`: `{"source": <K6221>, "meter": <K2182A>}`
- `DCSingleInstrumentVI`: `{"main": <K2400 SMU>}`
- `DeltaModeMeasurementVI`: `{"source": <K6221>, "meter": <K2182A>}`

## Exit (what goes out)
`DCMeasurementBase` contract:
- `initiate(current_A, compliance_A, voltmeter_range_V)` — arm the measurement.
- `take_reading(n_points) → {"voltage_V": list, "current_A": list}`.
- `standby()` — zero source and block further readings.
- `ping() → bool`.

`DeltaModeMeasurementVI` uses `configure()` / `read_datapoint()` instead
(delta-mode hardware protocol).

## Interface contract
DC measurement classes inherit `DCMeasurementBase` from
`virtual_instruments/base.py`. Delta-mode inherits `MeasurementInstrumentBase`.

## How to add a new measurement VI
1. Subclass `DCMeasurementBase` (for DC) or `MeasurementInstrumentBase` (other).
2. Implement `initiate()`, `take_reading()`, `standby()`, `ping()`.
3. Register in `devices.yaml`.
4. Add parametrized tests to `tests/test_l1_new_vis.py`.

## Files
- `dc_separate_measurement.py` — `DCSeparateMeasurementVI`: Keithley 6221 source +
  2182A nanovoltmeter, simple DC mode.
- `dc_single_instrument.py` — `DCSingleInstrumentVI`: Keithley 2400 SMU,
  single-instrument DC mode with the same method contract.
- `measurement_delta_mode.py` — `DeltaModeMeasurementVI`: Keithley 6221 + 2182A in
  delta-mode (reverses current polarity each reading for offset cancellation).
