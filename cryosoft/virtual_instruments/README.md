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
- `evaluate_safety()` interlock verdicts reported to `Station.check_safety()`
  every tick — a per-VI, per-tick JUDGMENT of that VI's own polled state,
  never a declaration. The two declarative halves that classify what a
  judgment MEANS live one level up, on the class, and are the System-
  Condition standard's producer/consumer pair (GLOSSARY.md's **System
  condition** / **Safety-flag manifest** / **Safety concern**): the
  PRODUCER side is the `safety_flags` class attribute, mapping every flag
  this VI's `evaluate_safety()` can report to its severity — `"advisory"`
  (reserved), `"hold"` (scoped to concerned VIs, e.g. `LevelMeterBase`'s
  `"helium_low"`), or `"critical"` (station-wide EMERGENCY by construction,
  e.g. `MagnetBase`'s `"quench"` — no VI may name a critical flag as a
  concern, since a per-VI hold would be meaningless once EMERGENCY has
  already stopped everything) — merged across the MRO by
  `merged_safety_flags()`, exactly like `control_limits`. The CONSUMER side
  is `safety_concerns()`: which HOLD-severity flags — by name, anyone's —
  this VI depends on to operate safely (only `MagnetBase` overrides the
  empty-set default, declaring `{"helium_low"}`; quench-concerned VIs
  declare nothing, `quench` being critical). Both are static, declarative —
  never a live reading — read by `Station.update_conditions()` once per
  tick, with no extra poll, to build that tick's safety-origin `Condition`s
  (GLOSSARY.md's **Safety hold** / **Critical safety flag**).
- For rampable VIs, the `start_ramp` / `advance_ramp` / `ramp_status` /
  `stop_ramp` generator API the Orchestrator drives each tick, plus the
  optional **ramp-introspection standard** — `ramp_value()` /
  `ramp_setpoint()` / `ramp_target()` / `ramp_rate()` / `ramp_phase()`, each
  with a safe `None` default. Implementing them is what puts a VI on the
  Monitor window's Ramps sub-panel (current value, next setpoint, end
  setpoint, rate) and into the operational-status record; no GUI or
  Orchestrator code changes for a new rampable VI. `ramp_setpoint()` in
  particular must be a pure accessor over what the generator last commanded
  — never a hardware read — and must be cleared in `stop_ramp()` alongside
  the target.
- `standby_status()` — whether this VI is at the safe idle state its own
  `standby()` drives it to (`"reached"`), on its way there
  (`"converging"`), or neither (`"away"`). Derived entirely from command
  PROVENANCE by `BaseVirtualInstrument`'s `__init_subclass__` wrap of a
  directly defined `standby()` / `start_ramp()` / `stop_ramp()` (the same
  inherited-enforcement idiom the control-validation standard uses for
  `@control`): a VI author writes nothing to get this, and inherits it even
  when it defines none of the three wrapped methods itself. Not physical
  verification — it knows the standby command was issued and its ramp
  finished, not that the hardware actually arrived — so a VI with its own
  means of checking may override it to add a physics check on top.

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
- `__init__(self, drivers, **init_params)` with no required args beyond
  `drivers`, and **silent**: it validates config and stores state, and sends no
  command that changes what the instrument is doing. The **connection
  lifecycle** (GLOSSARY.md) puts setup commands in `initiate()` — pole mode,
  slew rate, heater mode — so building the Station never disturbs an instrument
  an operator is using. Machine-checked by
  `test_vi_construction_sends_no_commands`, which builds every VI in every
  shipped config against recording drivers.
- The connection lifecycle's VI half, both inherited from
  `BaseVirtualInstrument` and rarely overridden: `ping()` (identity query on
  every driver — the one command a Station build sends) and `disconnect()` (a
  release hook for VI-held state; the *Station* closes the driver sessions,
  because a driver may be shared with a VI that stays online).
- The declared `detach_when_idle` standard (`BaseVirtualInstrument`'s
  "Detach-when-idle declaration"; GLOSSARY.md's **Availability**): a
  single-client VI opts in by overriding the `detach_when_idle` property with
  one line (e.g. `return self._configured_externally`), never by writing
  release behaviour itself. The base does the rest — `is_attached()` (the
  observable half, deliberately NOT `@monitored`; it reaches the GUI only
  through the Availability record's `detached` tag), `_attach()`/`_detach()`
  (idempotent, never-raising helpers over each driver's opt-in
  `ensure_connected()` / contract `close()`), an `__init_subclass__` wrap of a
  directly defined `standby()` so the release fires after the VI's own
  safe-off commands, and a `ping()` verify-and-release path for a VI that has
  opted in. A VI never needs its own `ping()` override or a hand-written
  release branch in `standby()` to get this — see
  `virtual_instruments/measurement/README.md`'s "Externally configured
  instruments" section for the motivating case.
- A `vi_type` class attribute (`system` / `measurement` / `level` / `switch`).
- The control-validation standard: bounded `@control` parameters declared in
  `control_limits`, limit values populated from `init_params`, enforced by the
  base class before the hardware call. Coverage is machine-checked, not
  optional: every numeric (`float`/`int`) `@control` parameter of every VI
  must appear in `control_limits` or in `test_conformance.py`'s
  `CONTROL_LIMIT_EXEMPTIONS` with a one-line physical reason a range cannot
  bound it (an enumerated mode code, a dimensionless count, a dwell time, a
  compliance ceiling that is itself protective). A stale exemption — one whose
  parameter has since gained a limit, been renamed or been deleted — fails
  too, so the list stays as short as honesty allows.
  `BaseVirtualInstrument.limit_bounds(limit_name)` is the standard's public
  READ side: how the Station reports what a setup allows (for an experiment's
  envelope to narrow) without reaching into `_limits`.
- The excitation ceiling: every VI that drives current through the sample
  reads `max_source_current_A` from its config `init_params` — directly (the
  DC and delta-mode VIs bound the sourced current to ±that value, symmetric
  because current reversal is routine) or derived (the voltage-sourced lock-in
  bounds its oscillator amplitude by `max_source_current_A ×
  series_resistance_ohm`). `base._populate_excitation_current_limit()` is the
  one place the key becomes a bound. Every SHIPPED config must declare it —
  conformance-checked per config, real setups included — because the ceiling
  is a property of the sample wiring, not of the code.
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
   bounded parameter and read the value from `init_params` (a numeric control
   with no limit fails conformance unless you write down why a range cannot
   bound it). Give each control
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
  `get_state()`, `evaluate_safety()`/`safety_flags`/`merged_safety_flags()`/
  `safety_concerns()` (the System-Condition standard's producer/consumer
  declarations — GLOSSARY.md's **Safety-flag manifest** / **Safety concern**
  / **Safety hold** / **Critical safety flag**), the full
  measurement-method standard in
  `MeasurementInstrumentBase`'s docstring, and `standby_status()` — the
  command-provenance accessor answering "reached" / "converging" / "away"
  for whether a VI is at, heading to, or away from its own `standby()`'s
  safe idle state, maintained automatically by the same `__init_subclass__`
  wrap of `standby()`/`start_ramp()`/`stop_ramp()`.
  (`@monitored`/`@control` decorators
  themselves are defined in `cryosoft.core.decorators`.) tests:
  `tests/test_conformance.py`, `tests/test_l1_virtual_instruments.py`.
- `rampable.py` — `RampableVI` mixin: the abstract ramp API
  (`start_ramp`, `advance_ramp`, `ramp_status`, `stop_ramp`) the Orchestrator
  calls each tick; `stop_ramp` on abort/ERROR/EMERGENCY kills the generator and
  holds the hardware. Also the optional, bus-free introspection hooks
  (`ramp_value`, `ramp_setpoint`, `ramp_target`, `ramp_rate`, `ramp_phase`)
  that `Station.get_ramp_status()` aggregates once per tick for the ramp
  tracker and the operational-status record — `ramp_setpoint()` is the NEXT
  setpoint (the intermediate value the hardware is driving to), distinct
  from `ramp_target()`'s END setpoint. Mixed into magnet, temperature, and
  rotator VIs. tests: `tests/test_l1_virtual_instruments.py`,
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
