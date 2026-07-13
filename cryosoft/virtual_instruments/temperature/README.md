# virtual_instruments/temperature/

## Purpose
Virtual instruments for temperature controllers. Abstracts away the model-specific
driver (ITC503, ITC5, …) so procedures interact with a behaviour-named VI
regardless of which controller is wired to which zone.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
A driver dict `{"main": <temperature controller real driver>}` and optional
`init_params`: `default_ramp_rate` (K/min), `tolerance` (K), and the setup
limit keys of the control-validation standard: `min_temperature_K`,
`max_temperature_K`, `max_ramp_rate_K_per_min` (missing keys mean unbounded
on that side).

## Exit (what goes out)
`@monitored` readings: `temperature() → float (K)`, `setpoint() → float (K)`,
`heater_output() → float (%)`. `VTITemperatureControllerVI` adds
`needle_valve() → float (%)`.
`@control` actions: `set_temperature(K)`, `set_ramp_rate(K/min)` — both
bounded by the config limits via `control_limits`; `set_needle_valve(%)` on
the VTI is bounded to the physical 0–100 %.
`RampableVI` interface: `start_ramp()`, `advance_ramp()`, `ramp_status()`,
`stop_ramp()` (pins the setpoint to the current temperature — used by the
Orchestrator on abort/pause/error).

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
  ramp generator with tolerance-based settle detection. Key API:
  `@monitored temperature` / `setpoint` / `heater_output`,
  `@control set_temperature` / `set_ramp_rate`, the `RampableVI` methods. tests:
  `tests/test_l1_new_vis.py` (`TestSampleTemperatureControllerVI`),
  `tests/test_l1_virtual_instruments.py`.
- `vti_temperature_controller.py` — `VTITemperatureControllerVI`: extends above with
  needle valve `@monitored needle_valve` and `@control set_needle_valve` (same
  ITC503 auxiliary output). tests: `tests/test_l1_new_vis.py`
  (`TestVTITemperatureControllerVI`).
- `__init__.py` — package marker. tests: none.
