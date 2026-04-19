# virtual_instruments/temperature/

## Purpose
Virtual instruments for temperature controllers. Abstracts away the model-specific
driver (ITC503, ITC5, …) so procedures interact with a behaviour-named VI
regardless of which controller is wired to which zone.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
A driver dict `{"main": <temperature controller real driver>}` and optional
`init_params`: `default_ramp_rate` (K/min), `tolerance` (K).

## Exit (what goes out)
`@monitored` readings: `temperature() → float (K)`, `setpoint() → float (K)`,
`heater_output() → float (%)`. `VTITemperatureControllerVI` adds
`needle_valve() → float (%)`.
`@control` actions: `set_temperature(K)`, `set_ramp_rate(K/min)`.
`RampableVI` interface: `start_ramp()`, `advance_ramp()`, `ramp_status()`.

## Interface contract
All classes here extend `SampleTemperatureControllerVI` (itself inheriting from
`TemperatureControllerBase` and `RampableVI`).

## How to add a new temperature VI
1. Subclass `SampleTemperatureControllerVI`.
2. Add new `@monitored` / `@control` methods for extra hardware (e.g. heater zones).
3. Register in `devices.yaml` with the full dotted class path.
4. Add tests to `tests/test_l1_new_vis.py`.

## Files
- `sample_temperature_controller.py` — `SampleTemperatureControllerVI`: time-based
  ramp generator with tolerance-based settle detection.
- `vti_temperature_controller.py` — `VTITemperatureControllerVI`: extends above with
  needle valve `@monitored` and `@control` (same ITC503 auxiliary output).
