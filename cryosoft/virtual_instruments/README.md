# virtual_instruments/

## Purpose
Layer-1 standardised capability interfaces over the raw drivers. A **Virtual
Instrument (VI)** presents behaviour-named methods (`set_field`, `set_temperature`,
`take_reading`, `select_route`) to the layers above, so procedures and the
Orchestrator interact with a magnet or a thermometer regardless of which physical
model is wired in. This root holds the shared contracts; each `vi_type` gets its
own subfolder of concrete classes.

## Architecture layer
L1 — Virtual Instruments. Depends downward on `cryosoft.drivers` and
`cryosoft.core` (exceptions, decorators, plan). VIs never import Station,
Orchestrator, or Procedure.

## Entry (what comes in)
The Station factory calls `__init__(self, drivers, **init_params)` on every VI:
a `drivers` dict of role → driver instance (e.g. `{"main": ...}`,
`{"source": ..., "meter": ...}`) and the `init_params` from `devices.yaml`
(addresses already bound, plus limits, ramp segments, routes).

## Exit (what goes out)
- `@monitored` read-only methods, auto-collected by `get_state()` into a flat
  numeric state snapshot each tick.
- `@control` action methods, validated against `control_limits` before any
  hardware call (out-of-range raises `CryoSoftSafetyError`), carrying a
  capability scope (see below) and optional GUI metadata: `params={name:
  ParamSpec}` (widget shape, unit, bounds, choices) and `panel=` (default
  monitor-card placement — see "GUI presentation" below).
- `evaluate_safety()` interlock verdicts reported to `Station.check_safety()`.
- For rampable VIs, the `start_ramp` / `advance_ramp` / `ramp_status` /
  `stop_ramp` generator API the Orchestrator drives each tick.

## GUI presentation: who decides what a card shows
Read this before adding or "hiding" a control — the split trips people up:

- **The VI decides the DEFAULT.** `@control` = shown on the compact monitor
  card; `@control(panel=False)` = front-panel window only. This is the
  author's judgment of what operators commonly use (e.g. `set_temperature`
  ships shown, `set_pid` ships hidden). Changing a default means editing the
  VI — but that is the only case that does.
- **The setup config decides the ACTUAL card.** A `panels:` entry in the
  setup's `monitor.yaml` is a per-VI allowlist that REPLACES the defaults
  entirely — it can surface a `panel=False` control or hide a `panel=True`
  one. A user or lab customizes what their cards show by editing config,
  never VI code. See `cryosoft/configs/README.md` for the block's shape.
- **Neither layer removes capability.** Every `@control`, shown or hidden,
  remains available in the per-VI instrument front panel (the sliders icon),
  and `control_limits` enforcement is untouched by visibility. Hiding is
  presentation, never a safety mechanism.
- **Dynamic choices**: when a control's valid values only exist after
  construction (a switch's config-named routes), override the instance hook
  `control_param_specs(method_name)` to inject a ParamSpec with `choices` —
  the GUI consults the hook, not the raw decorator metadata
  (`SwitchMatrixVI.select_route` is the reference example).

## Interface contract
The written standards all live in this root and are enforced by
`tests/test_conformance.py`, which auto-discovers and checks every concrete VI:
- `__init__(self, drivers, **init_params)` with no required args beyond `drivers`.
- A `vi_type` class attribute (`system` / `measurement` / `level` / `switch`).
- The control-validation standard: bounded `@control` parameters declared in
  `control_limits`, limit values populated from `init_params`, enforced by the
  base class before the hardware call.
- The capability-scope standard: `@control` (bare, or `@control(scope=...)`)
  carries a scope — `"measurement"` (default, usable by any plan) or
  `"operation"` (usable only by an operation's plan; a human in IDLE can still
  click either from the GUI, this only gates *plan dispatch*). Enforcement
  lives one layer up, in `Station.send_measurement_commands(commands,
  allowed_scope=...)` (`cryosoft.core.station`) — this folder only declares
  the scope. Give a method `scope="operation"` when automated misuse is
  dangerous (switch-heater on/off, persistent-mode entry/exit, a future
  needle-valve control); leave it at the default otherwise. Every
  `reading_setters` target and the measurement lifecycle
  (`initiate_measurement`/`standby`) must stay measurement-scope —
  conformance-checked.
- The control-declaration standard: `params=` ParamSpecs must match the
  method signature exactly (checked at import) and agree with its type
  annotations (conformance-checked); `panel=` must be a bool.
- Measurement VIs additionally obey the self-describing measurement-method
  standard (`measurement_parameters` / `measurement_data_keys` /
  `measurement_scalar_columns` plus the `data_arrays` /
  `initiate_measurement` / `take_reading` / `standby` lifecycle; plain
  `initiate()` on a measurement VI is a harmless connection check, never an
  arming action).

## How to add a new module
1. Pick the `vi_type` and open that subfolder's README for the local recipe.
2. Subclass the right base (`MagnetBase`, `TemperatureControllerBase`,
   `LevelMeterBase`, `RotatorBase`, `MeasurementInstrumentBase` /
   `DCMeasurementBase`, or `BaseVirtualInstrument` directly for a switch),
   adding `RampableVI` if it ramps.
3. Tag reads `@monitored` and actions `@control`; declare `control_limits` for any
   bounded parameter and read the value from `init_params`. Give each control
   its GUI metadata: `params={name: ParamSpec}` for typed widgets (unit,
   bounds, choices, tooltips) and `panel=False` for anything that belongs in
   the front panel rather than the compact card (see "GUI presentation"
   above).
4. Register the VI in a config `devices.yaml`; add behaviour tests to the
   subfolder's test file. Conformance covers the contract automatically.

## Files
Shared contracts at the root; concrete classes live in the subfolders.

- `base.py` — `BaseVirtualInstrument` plus the typed sub-bases `MagnetBase`,
  `TemperatureControllerBase`, `LevelMeterBase`, `RotatorBase`,
  `MeasurementInstrumentBase`, `DCMeasurementBase`. Provides `__init_subclass__` auto-wrapping of
  `@monitored`/`@control` (structured logging + declarative limit enforcement),
  `get_state()`, `evaluate_safety()`, and the full measurement-method standard in
  `MeasurementInstrumentBase`'s docstring. (`@monitored`/`@control` decorators
  themselves are defined in `cryosoft.core.decorators`.) tests:
  `tests/test_conformance.py`, `tests/test_l1_virtual_instruments.py`.
- `rampable.py` — `RampableVI` mixin: the abstract ramp API
  (`start_ramp`, `advance_ramp`, `ramp_status`, `stop_ramp`) the Orchestrator
  calls each tick; `stop_ramp` on abort/ERROR/EMERGENCY kills the generator and
  holds the hardware. Mixed into magnet and temperature VIs. tests:
  `tests/test_l1_new_vis.py` (via the concrete rampable VIs).
- `__init__.py` — package marker (docstring only). tests: none.
- `magnet/` — superconducting magnet PSU VIs (field ramp, persistent mode).
- `temperature/` — temperature controller VIs (sample and VTI).
- `level/` — cryogen level meter VIs.
- `rotator/` — motorized sample-rotation stage VIs (uniaxial/2D magnet sample
  orientation).
- `measurement/` — electrical transport measurement-method VIs (DC, delta-mode).
- `switch/` — matrix-switch / scanner VIs (exclusive-mux routing).

Each subfolder has its own `README.md` with the per-file map for that `vi_type`.
