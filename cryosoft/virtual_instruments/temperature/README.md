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
`heater_output() → float (%)`, `heater_mode() → str` ('AUTO'/'MANUAL', shared
base — both the Lakeshore 335 and Oxford ITC503 drivers implement it).
`VTITemperatureControllerVI` adds `needle_valve() → float (%)` and
`needle_valve_mode() → str` ('AUTO'/'MANUAL' — the ITC503's gas-flow control
loop drives the valve in AUTO and ignores explicit position commands).
`Lakeshore335SampleTemperatureControllerVI` adds `curve() → int` (the
assigned calibration curve number) and `heater_range() → str`
('OFF'/'LOW'/'MEDIUM'/'HIGH' — the Lakeshore 335's heater powers up in range
Off and delivers no power, regardless of `heater_mode` or setpoint, until
this is set to Low/Medium/High; the ITC503 has no equivalent range concept,
so it stays Lakeshore-specific).
`@control` actions: `set_temperature(K)`, `set_ramp_rate(K/min)` — both
bounded by the config limits via `control_limits`; `set_heater_mode(str)` is
a `panel=False` front-panel-only drop-down (choices AUTO/MANUAL, shared
base); `set_heater_output(%)` (also `panel=False`, shared base) commands
manual heater power and is refused with `CryoSoftSafetyError` unless
`heater_mode` is MANUAL — the closed-loop PID would otherwise silently
override it. `set_needle_valve(%)` on the VTI is bounded to the physical
0–100 % and is likewise refused unless `needle_valve_mode` is MANUAL (the
real-hardware bug this fixes: the ITC503's gas-flow loop ignores position
commands while in AUTO); `set_needle_valve_mode(str)` (`panel=False`) is the
AUTO/MANUAL drop-down that unlocks it. `set_curve(int)` and
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
commands zero heater output, so no
closed-loop setpoint can drive power while idle; on `VTITemperatureControllerVI`,
`standby()` additionally switches needle valve mode to MANUAL before closing
it (0 % open), cutting bath helium flow — the mode switch must happen first,
since the ITC503's gas-flow control loop ignores a bare position command
while gas flow is in AUTO (its power-up default). `Lakeshore335SampleTemperatureControllerVI`
does not touch `heater_range` on standby/initiate — it is a separate,
driver-specific power gate (see below) left as the operator set it.

`TemperatureControllerBase` does not override `safety_concerns()` — it
keeps the empty-set default, so a temperature controller is never subject
to a safety hold: not for `helium_low` (only magnets, via `MagnetBase`,
depend on it — see GLOSSARY.md's **Safety concern**) and not for `quench`
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
  here (not on a driver-specific subclass) because both the Lakeshore 335
  and Oxford ITC503 drivers implement `get_heater_mode`/`set_heater_mode`
  with the same 'AUTO'/'MANUAL' vocabulary. `initiate()` pins the setpoint to
  `round(temperature())` before setting heater mode AUTO; `standby()` sets
  heater mode MANUAL and zeroes heater output — the lifecycle standard.
  tests: `tests/test_l1_new_vis.py`
  (`TestSampleTemperatureControllerVI`), `tests/test_l1_virtual_instruments.py`.
- `vti_temperature_controller.py` — `VTITemperatureControllerVI`: extends above with
  needle valve `@monitored needle_valve` / `needle_valve_mode` and
  `@control set_needle_valve` (refused unless `needle_valve_mode` is MANUAL)
  / `set_needle_valve_mode` (`panel=False`, AUTO/MANUAL drop-down) — the same
  ITC503 auxiliary output and its own control-mode half of the combined
  heater/gas register. `standby()` extends the inherited heater lifecycle by
  switching needle valve mode to MANUAL, then closing it. tests:
  `tests/test_l1_new_vis.py` (`TestVTITemperatureControllerVI`).
- `lakeshore_335_sample_temperature_controller.py` —
  `Lakeshore335SampleTemperatureControllerVI`: extends
  `SampleTemperatureControllerVI` with `@monitored curve` / `@control
  set_curve` over the Lakeshore 335's `INCRV` command (sensor input A), and
  `@monitored heater_range` / `@control set_heater_range` (`panel=False`)
  over the `RANGE` command — the heater's power-up default is Off, so no
  heater power is delivered (regardless of `heater_mode` or setpoint) until
  this is set to Low/Medium/High. Both are driver-specific (only the
  Lakeshore 335 driver/sim implement `get_sensor_curve`/`set_sensor_curve`
  and `get_heater_range`/`set_heater_range`; the Oxford ITC503 has neither
  concept), so they stay on this subclass rather than the shared base. tests:
  `tests/test_l1_new_vis.py` (`TestLakeshore335SampleTemperatureControllerVI`).
- `__init__.py` — package marker. tests: none.
