# virtual_instruments/temperature/

## Purpose
Virtual instruments for temperature controllers. Abstracts away the model-specific
driver (Lakeshore 335, …) so procedures interact with a behaviour-named VI
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
`heater_output() → float (%)`, `heater_mode() → str` ('AUTO'/'MANUAL', on
the shared base — the vocabulary every controller driver implements).
`Lakeshore335SampleTemperatureControllerVI` adds `curve() → int` (the
assigned calibration curve number) and `heater_range() → str`
('OFF'/'LOW'/'MEDIUM'/'HIGH' — the Lakeshore 335's heater powers up in range
Off and delivers no power, regardless of `heater_mode` or setpoint, until
this is set to Low/Medium/High; a controller with no range concept simply
has no such reading, so it stays Lakeshore-specific).
`@control` actions: `set_temperature(K)`, `set_ramp_rate(K/min)` — both
bounded by the config limits via `control_limits`; `set_heater_mode(str)` is
a `panel=False` front-panel-only drop-down (choices AUTO/MANUAL, shared
base); `set_heater_output(%)` (also `panel=False`, shared base) commands
manual heater power and is refused with `I2ASSafetyError` unless
`heater_mode` is MANUAL — the closed-loop PID would otherwise silently
override it. `set_curve(int)` and
`set_heater_range(str)` on the Lakeshore 335 variant are `panel=False`
front-panel-only drop-downs (curve choices = the instrument's 0–59 curve
numbering; heater range choices = OFF/LOW/MEDIUM/HIGH).
`RampableVI` interface: `start_ramp()`, `advance_ramp()`, `ramp_status()`,
`stop_ramp()` (pins the setpoint to the current temperature — used by the
Orchestrator on abort/pause/error).
**Lifecycle** (standard, mirrored across VI types — see the magnet VI's
README): `initiate()` first pins the setpoint to the nearest whole kelvin of
the current reading (`round(temperature())`), THEN sets heater mode AUTO
(closed-loop PID to setpoint) — the ordering means AUTO never inherits a
stale setpoint left over from before the heater was switched to MANUAL, so
initiating never kicks off a surprise ramp to some old target; a subsequent
`set_temperature()`/`start_ramp()` starts its ramp from this pinned point
like any other resting state. `standby()` sets heater mode MANUAL and
commands zero heater output, so no closed-loop setpoint can drive power
while idle. `Lakeshore335SampleTemperatureControllerVI` EXTENDS `initiate()`
with the one piece of operating state the 335 adds: its heater RANGE, taken
from the setup's `initiate_heater_range` (default MEDIUM). Off delivers no
power whatever the mode or setpoint is, so a 335 handed the loop without a
range would be under closed-loop control and still not heat. `standby()`
deliberately does NOT mirror it — switching heater power back off is the
operator's call, and the inherited standby has already taken the loop out of
circuit at zero output.

`TemperatureControllerBase` does not override `safety_concerns()` — it
keeps the empty-set default, so a temperature controller is never subject
to a safety hold: no shipped VI declares a hold-severity flag (see
GLOSSARY.md's **Safety concern**), and not for `quench`
either, since `quench` is critical severity and therefore station-wide by
construction (GLOSSARY.md's **Critical safety flag**) — a quench stops
every instrument, including the temperature controller, via EMERGENCY
rather than a per-VI hold; naming it in `safety_concerns()` would be
meaningless. See GLOSSARY.md's **Safety hold**.

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
  `@monitored temperature` / `setpoint` / `heater_output` / `heater_mode`,
  `@control set_temperature` / `set_ramp_rate` / `set_heater_mode`
  (`panel=False`) / `set_heater_output` (`panel=False`, refused unless
  `heater_mode` is MANUAL), the `RampableVI` methods. `heater_mode` lives
  here (not on a driver-specific subclass) because every controller driver
  implements `get_heater_mode`/`set_heater_mode` with the same
  'AUTO'/'MANUAL' vocabulary. `initiate()` pins the setpoint to
  `round(temperature())` before setting heater mode AUTO; `standby()` sets
  heater mode MANUAL and zeroes heater output — the lifecycle standard.
  Declares two **UI groups** (`ui_groups`, tagged `group=` per method),
  inherited by the subclass below: `temperature_control` (the sensor
  reading, the setpoint, and the target/rate controls that drive it) and
  `heater` (loop mode, manual output, and the PID gains behind them).
  tests: `tests/test_l1_new_vis.py`
  (`TestSampleTemperatureControllerVI`), `tests/test_l1_virtual_instruments.py`.
- `lakeshore_335_sample_temperature_controller.py` —
  `Lakeshore335SampleTemperatureControllerVI`: extends
  `SampleTemperatureControllerVI` with `@monitored curve` / `@control
  set_curve` over the Lakeshore 335's `INCRV` command (sensor input A), and
  `@monitored heater_range` / `@control set_heater_range` (`panel=False`)
  over the `RANGE` command — the heater's power-up default is Off, so no
  heater power is delivered (regardless of `heater_mode` or setpoint) until
  this is set to Low/Medium/High — which is why `initiate()` is extended
  here to select the setup's `initiate_heater_range` (default MEDIUM). Both
  readings are driver-specific (only the Lakeshore 335 driver/sim implement
  `get_sensor_curve`/`set_sensor_curve` and
  `get_heater_range`/`set_heater_range`), so they stay on this subclass
  rather than the shared base. It is also the shipped example of dynamic
  `control_param_specs()` choices under the purity rule. tests:
  `tests/test_l1_new_vis.py` (`TestLakeshore335SampleTemperatureControllerVI`).
- `__init__.py` — package marker. tests: none.
