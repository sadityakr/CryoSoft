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
  hardware call (out-of-range raises `CryoSoftSafetyError`).
- `evaluate_safety()` interlock verdicts reported to `Station.check_safety()`.
- For rampable VIs, the `start_ramp` / `advance_ramp` / `ramp_status` /
  `stop_ramp` generator API the Orchestrator drives each tick.

## Interface contract
The written standards all live in this root and are enforced by
`tests/test_conformance.py`, which auto-discovers and checks every concrete VI:
- `__init__(self, drivers, **init_params)` with no required args beyond `drivers`.
- A `vi_type` class attribute (`system` / `measurement` / `level` / `switch`).
- The control-validation standard: bounded `@control` parameters declared in
  `control_limits`, limit values populated from `init_params`, enforced by the
  base class before the hardware call.
- Measurement VIs additionally obey the self-describing measurement-method
  standard (`measurement_parameters` / `measurement_data_keys` /
  `measurement_scalar_columns` plus the `data_arrays` / `initiate` /
  `take_reading` / `standby` lifecycle).

## How to add a new module
1. Pick the `vi_type` and open that subfolder's README for the local recipe.
2. Subclass the right base (`MagnetBase`, `TemperatureControllerBase`,
   `LevelMeterBase`, `RotatorBase`, `MeasurementInstrumentBase` /
   `DCMeasurementBase`, or `BaseVirtualInstrument` directly for a switch),
   adding `RampableVI` if it ramps.
3. Tag reads `@monitored` and actions `@control`; declare `control_limits` for any
   bounded parameter and read the value from `init_params`.
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
