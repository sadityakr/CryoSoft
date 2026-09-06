# procedures/

## Purpose

`procedures/` holds the concrete measurement procedures: one thin subclass per
**sweep axis** (the swept quantity). Each describes *what* the experiment does
(which axis to sweep, which system VIs to hold); the `Orchestrator` decides
*how* it runs (ramping, settling, monitoring, saving). The generic
sweep-and-measure engine itself lives one layer down in
`cryosoft.core.procedure.SweepMeasureProcedure`; the files here supply only the
axis specifics.

The axis need not be a ramped setpoint. `time_series.py` sweeps elapsed time
and commands no hardware at all, which is why the base class asks for the axis
column through `axis_data_key()` rather than assuming every procedure declares
a `SweepAxis` (see Time series below).

## Architecture layer

L4 (Procedures). Sits above L3 (Orchestrator) and L2 (Station); uses
`DataManager` (L5) for HDF5 output.

```
GUI -> Orchestrator -> Procedure -> Station -> Virtual Instruments -> Drivers
                              \-> DataManager -> HDF5
```

## Entry (how control/data enters this folder)

Each procedure is constructed with:

- `station`: the `Station` instance (the only path to any VI).
- `sample_info`: `{"sample_name", "sample_id", "comments"}` from the GUI.
- `data_directory`: where the HDF5 file is created.
- `**param_values`: the GUI form values, matching the declared `ParamSpec`s
  (declared defaults are merged in for any omitted key).

## Exit (what it hands to other layers)

The Orchestrator drives the lifecycle by calling these methods, which return the
typed plan objects from `cryosoft.core.plan` (never bare dicts/tuples):

| Method | Returns | Called when |
|--------|---------|-------------|
| `initiate()` | `PhasePlan` | Procedure starts |
| `change_sweep_step()` | `StepPlan` or `None` | After each measurement |
| `measure()` | nothing (writes HDF5) | System stable at current point |
| `standby()` | `PhasePlan` | Sweep complete or aborted |
| `abort()` | `tuple[Command, ...]` | User abort / ERROR / EMERGENCY |

A `PhasePlan` carries `targets` (VI name -> `Target`), ordered `commands`, a
`wait_s` settle time, and `claim_commands` — one `initiate()` `Command` per
claimed VI (`BaseProcedure._claim_initiate_commands()`), dispatched by the
Orchestrator BEFORE `targets`/`commands` so every claimed VI is already in its
standard operating state (see **Claim** below) when this plan's own
targets/commands reach it; a `StepPlan` carries the next point's `targets` and
its `wait_s` (no `claim_commands` — only `initiate()`'s plan claim-initiates).
A `Target` carries an optional `rate` (forwarded to the VI's `start_ramp()`
only when set); `Command` order is meaningful and never reordered. Every plan object validates at construction, so a malformed
plan fails at the procedure boundary, not in the tick loop.

## Interface contract

Every procedure subclasses `BaseProcedure` (from `cryosoft.core.procedure`); an
axis procedure subclasses `SweepMeasureProcedure` and overrides nothing but the
axis hooks.

- Procedures never import from `drivers/` or `virtual_instruments/`; instruments
  are reached only through `self._station` (contract C6).
- Parameters are declared as `ParamSpec` value objects (from
  `cryosoft.core.plan`), grouped into `sweep_parameters` / `system_parameters` /
  `measurement_parameters` (auto-unioned into `parameters`). `ParamSpec`
  supports plain fields, `choices` drop-downs (the collected value is the mapped
  value, so no translation in the procedure), and `bool` checkboxes. The
  ParamSpec -> Qt-widget mapping lives entirely in `cryosoft.gui.param_form`.
- `get_param_groups(station, selections)` (classmethod) drives GUI form
  generation. `SweepMeasureProcedure` uses it to add a structural
  `measurement_vi` selector (choices are the station's measurement VIs) plus the
  selected VI's own `measurement_parameters`, and, when anything is loopable,
  the Reading loop slot group (see below).
- SI units everywhere: tesla, kelvin, amperes, volts, seconds.
- `claimed_vi_names() -> set[str] | None` (`BaseProcedure`; the **Claim**
  standard — see GLOSSARY.md): declares which VIs a running procedure exclusively owns, so
  the Orchestrator knows what a manual front-panel action may touch while
  it runs. Default `None` (claim everything) — procedures stay exclusive in
  this iteration; only `TimeSeries` narrows it (see below). The same
  declaration drives `_claim_initiate_commands()`: one `initiate()` `Command`
  per claimed VI, carried in `initiate()`'s returned `PhasePlan.claim_commands`
  and dispatched first — so a claimed VI an operator left in a non-standard
  state (heater switched to MANUAL, say) is always reset to standard before
  this run's own targets/commands assume it.

- `planned_targets() -> dict[str, list[float]]` (`BaseProcedure`): every system
  setpoint the built run would command, per VI, so a queued run is validated
  before anything reaches hardware. `SweepMeasureProcedure` derives it from the
  same target hooks its plans are built from, so it cannot drift.
- `apply_probe(ProbeSpec)` and `estimate_step_seconds() -> StepCost`
  (`BaseProcedure`) are the two run-economics hooks, and a procedure normally
  inherits both. `apply_probe()` reduces a *built* run in place to a **probe
  run** (GLOSSARY.md) — the sweep subsampled keeping first and last, declared
  seconds-valued parameters capped, averaging cut, `run_kind = "probe"` — by
  the rules written in `ProbeSpec`'s docstring; `estimate_step_seconds()`
  reports the points, waits and measurement time behind a **duration
  estimate**, defaulting to the built sweep length with the omission named as
  an assumption. `SweepMeasureProcedure` implements both fully from the hooks
  the tick loop already uses (`_initiate_wait_s()`, `_step_wait_s()`,
  `_loop_shape`, the selected VI's `data_arrays()`), so an axis procedure that
  supplies only its axis hooks gets a probe and an estimate for free. Override
  `estimate_step_seconds()` only when the run's cost does not come from those
  hooks; override `apply_probe()` only to reduce something the base cannot see.

### Generic sweep and the reading loop (owned by the base, no per-procedure code)

`SweepMeasureProcedure` runs ANY measurement VI the station exposes, chosen in
the GUI, so a new *measurement method* is a new measurement VI, not a new
procedure. `initiate()` assembles a `DataSchema` (axis column + system columns +
the VI's arrays/scalars) and arms the VI; `measure()` runs the **reading loop**
(below), tags on the axis read-back, validates per datapoint, and saves;
`standby()` / `abort()` disarm the VI.

The reading loop is the standard for taking multiple readings at a single
sweep point. It has up to TWO generic slots, each a **loopable parameter** —
anything a reading-path VI advertises via its `reading_setters` class
attribute — today the DC measurement VI's `current_A` (setter
`set_source_current`). Every such parameter is the *same concept*, so a setup
whose measurement VI declares none simply has no loopable parameter and no
special case anywhere. Slot 1
(loop1) is axis 0 (outer) of every measurement column's real loop axis, slot 2
(loop2) axis 1 (inner).

The **Reading loop** form group renders automatically whenever anything is
loopable: one `{slot}_parameter` drop-down per slot plus that parameter's
values input — per-choice `{slot}_pick_{value}` checkboxes when the ParamSpec
is enumerated (tick the channels), a `{slot}_values` comma-separated text
field otherwise (e.g. `1e-6, -1e-6`), each value validated against the
parameter's own spec at construction. A slot with ONE value is a static
setting (dispatched once at `initiate()`, trivial length-1 axis); with two or
more it loops: the setter is dispatched as a `Command` through the Station
before every reading at that axis index. Every measurement column (mean,
error, raw-sample array, `n_valid`, …) carries the real `(n_loop1, n_loop2)`
axis in HDF5 — column names are never suffixed. Axis index -> physical value
is stored in the HDF5 metadata (`procedure_params["loop1_values"]` /
`["loop2_values"]`); participating non-measurement VIs get their
`reading_safe_off` at standby/abort. The live plots mirror the slots with
per-plot Loop 1 / Loop 2 selectors (fed by `live_plot_loop_labels()`, items
like "A1 = Mux-Ch1" with the axis index as item data); axis keys stay the
plain column names and the panel indexes directly into the grid at draw time.

### Time series: an axis that is not a setpoint, and a run that commands nothing

`time_series.py` is the third shape a procedure can take, and the most
permissive one. It sends NO system targets: `initiate()` arms the selected
measurement VI and nothing else, the first reading is taken on the next tick,
and readings repeat on a fixed cadence until an end condition fires. There are
deliberately no temperature or field parameters — the operator sets those by
hand on the monitor panel, before or during the run.

Three pieces of the framework make that work, and each is reusable by a future
procedure of the same shape:

| Piece | What it does |
|---|---|
| `sweep_axis = None` + `axis_data_key()` | The axis is elapsed time (`elapsed_s`), not a ramped quantity, so the GUI renders no linear/segments/CSV shape editor. `_build_sweep_array()` returns the schedule of measurement instants; its length is `max_duration_s / step_time_s`, which is what keeps the progress bar, `n_sweep_points`, and "Point n/N" meaningful. |
| A narrowed `claimed_vi_names()` | Returns only the measurement VI plus any reading-loop VI, so the Orchestrator's admission gate leaves every magnet and temperature front panel live for the whole run. The only procedure that does not claim the whole station. This also narrows `_claim_initiate_commands()` — a Time Series run never calls `initiate()` on a magnet or temperature controller it does not claim, so it never disturbs whatever state the operator has it in. |
| An empty **ramp scope** (GLOSSARY.md) | A run owns the ramps it targeted; this one targets nothing. A manual front-panel ramp therefore neither delays its next reading nor is stopped when the run ends. Ramp *advancement* is unaffected: every non-PAUSED tick still steps every ramp generator. |

The end condition is either the schedule alone or a watched channel reaching a
threshold. Direction is inferred from the channel's value at `initiate()` —
starting below the threshold stops on the way up, starting above stops on the
way down — so there is no rising/falling parameter to set wrong. `end_tolerance`
exists for an asymptotic approach that would never quite cross. `max_duration_s`
always applies as well.

Cadence is measured from each scheduled instant rather than from the end of the
previous reading, so measurement time is absorbed instead of accumulating into
drift. It is a floor, not a guarantee: one datapoint costs three Orchestrator
ticks (measure, advance, settle), so the fastest achievable cadence is three
times the setup's `tick_interval_ms`.

## How to add a new module

Add a procedure only for a new **sweep axis**. To add a new *measurement*
instead, add a measurement VI and register it with `vi_type: measurement`; both
shipped sweeps pick it up with zero procedure change.
that is a different contract, not a sweep axis.

1. Create `procedures/your_sweep.py` with the PEP 257 header docstring
   (Workspace Rule 1).
2. Subclass `SweepMeasureProcedure`; set `name`, `description`, a `sweep_axis`
   (this gives `_build_sweep_array()` and the GUI mode selector for free),
   `sweep_data_keys`, `default_x_key`, and any `system_parameters`.
3. Implement the six axis hooks only: `_initial_system_targets`,
   `_step_targets`, `_standby_targets`, `_axis_readback`, `_initiate_wait_s`,
   `_step_wait_s`. Do NOT re-declare `measurement_parameters` or override
   `initiate` / `measure` / `standby` / `abort` unless the axis truly needs it.
4. Write tests in `tests/test_new_procedures.py`, parametrized over the
   measurement VIs the sweep should support.
5. Add the file to the Files map below with its owning test file.

A procedure with a non sweep-and-measure shape can subclass `BaseProcedure`
directly and implement the five lifecycle methods and its own `DataManager`;
`SweepMeasureProcedure` is the recommended default.

## Instrument roles (role discovery)

A procedure never names a configured instrument. It declares the **roles** it
needs in `role_parameters` — one `RoleParam` per role, each carrying a
`candidates` callable over the Station's role accessors (`magnet_vi_names()`,
`temperature_vi_names()`, the thin fronts of `Station.vi_names_by_base()`), a
one-line description, and a `required` flag:

```python
role_parameters = {
    "field_vi": RoleParam(
        candidates=lambda station: station.magnet_vi_names(),
        description="Magnet this run sweeps",
    ),
    "temperature_vi": RoleParam(
        candidates=lambda station: station.temperature_vi_names(),
        description="Temperature controller this run sets",
        required=False,
    ),
}
```

Each role becomes one ordinary parameter (`field_vi`, `temperature_vi`) in an
"Instruments" `ParamGroup` that `get_param_groups(station)` fills against the
live Station: the choices are the configured instruments of that role, the
default is the first (which IS the value when a setup has exactly one), and a
role with no candidate contributes no widget. The GUI form and the agent tool
schema pick the choices up automatically because both render `ParamSpec`.

At construction every role is resolved once (`BaseProcedure._resolve_role`)
and read back through `self.role_vi("field_vi")`, which the axis hooks use
in place of a literal name. Resolution is strict: a **required** role with no
candidate refuses the run with `CryoSoftConfigError` (the message names the
role and what to configure), a name the caller passed that is not a candidate
is refused the same way (a typo, or a config that changed under a queued
run), and an **optional** role with no candidate resolves to `""`, which the
procedure treats as "emit no target for it". `TimeSeries` uses the same
discovery for the channels its end condition can watch: every magnet and
temperature controller the setup configures, labelled with the VI's own
setpoint label and unit.

Renaming a magnet or adding a second temperature controller therefore needs
no procedure change — the config stays the single source of truth for what
the setup has, and the run form asks only when there is a choice to make.

## Files

Each row: responsibility, key public class, and the test file(s) in `tests/`.

| File | Responsibility | Key public API | Tests |
|------|----------------|----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `field_imaging.py` | Sweeps magnetic field on the discovered magnet (`field_vi`, required) from a **saturated start** — the saturation pre-step below — positioning the discovered stage axes (`stage_x_vi` / `stage_y_vi`, optional, told apart by each VI's `axis`) to `stage_x_m` / `stage_y_m`, and running any selected measurement VI at each point; hands the magnet to its own `standby()` on standby and leaves the stage where it is. Built for the camera (a frame per point), runs any measurement method. | `FieldImaging` (axis hooks + `initiation_gates` over `SweepMeasureProcedure`) | `test_field_imaging_procedure.py` |
| `field_sweep.py` | Sweeps magnetic field on the discovered magnet (`field_vi`, required), optionally setting the discovered temperature controller (`temperature_vi`, optional; gated by `set_temperature`), running any selected measurement VI at each point; hands the magnet to its own `standby()` on standby. Requires a magnet and at least one measurement VI. | `FieldSweep` (axis hooks over `SweepMeasureProcedure`) | `test_new_procedures.py`, `test_l4_procedure.py` |
| `time_series.py` | Measures repeatedly against elapsed time, commanding no system hardware and claiming only the reading path, so the operator keeps manual control of the whole cryostat during the run. Ends on `max_duration_s`, or when a watched channel (any magnet or temperature controller the setup configures, discovered at construction) reaches `end_value`. Requires at least one measurement VI; a watched channel's VI must exist. | `TimeSeries` (axis hooks + `axis_data_key`/`claimed_vi_names`/`get_param_groups` over `SweepMeasureProcedure`) | `test_time_series_procedure.py`, `test_l3_orchestrator.py` (ramp scope) |
| `temperature_sweep.py` | Sweeps temperature on the discovered controller (`temperature_vi`, required) at a per-sweep ramp rate, optionally holding the discovered magnet (`field_vi`, optional) at `field`, running any selected measurement VI at each stable point. Requires a temperature controller and at least one measurement VI; a magnet is optional (a nonzero `field` with no magnet is refused at construction). | `TemperatureSweep` (axis hooks over `SweepMeasureProcedure`) | `test_new_procedures.py` |

### The saturation pre-step (Field Imaging)

`FieldImaging` differs from `FieldSweep` in its magnetic history, which is
what makes the first frame a **reference frame** (GLOSSARY.md): with
`saturate` on, `initiate()`'s field target is `saturation_field_T` (signed,
default -1.5 T — normally beyond the sweep's start on the same side), and
the procedure's `initiation_gates()` then take over from `wait_s`: the
first gate's one-shot action dispatches the magnet's own `set_field` to the
first sweep field as a `Command` through the Station and holds until the
magnet reports `TARGET_REACHED` (the Orchestrator advances ramps while a
gate waits), and a second gate is the `init_wait` settle. The first
`measure()` therefore images the sample at the first sweep point after
arriving from saturation — a fully magnetised, known state — and every
later frame is compared against it. `saturate` off means no gates and the
run starts at the first sweep point like a plain field sweep. The stage
targets ride in the same `initiate()` plan as ordinary `Target`s, one per
axis VI (`virtual_instruments/stage/README.md`), so a setup without a stage
simply emits none.

### The temperature toggle (on/off)

Both temperature-aware sweep procedures gate their temperature target on
one bool parameter (`FieldImaging` sets no temperature — the imaging setup
has no controller — and would declare the same toggle if it did):

| Parameter | Default | Effect when on |
|---|---|---|
| `set_temperature` | `True` | Emits a target for the discovered `temperature_vi` — the fixed `temperature` in `FieldSweep`, the swept value in `TemperatureSweep` |

"Off" means the procedure emits **no `Target`** for that VI, so the Orchestrator
never calls `start_ramp` on it and the controller holds exactly where the operator
left it. Reading is unaffected: monitoring, logging and trends come from the tick
loop's monitor pass, not from targets. In `FieldSweep` the role is optional, so a
station with no temperature controller also emits no target, toggle or not; in
`TemperatureSweep` the controller is the swept axis and its absence is refused at
construction.

Both procedures declare the toggle and build the conditional target dict
themselves — there is deliberately no shared framework mechanism, so a new
procedure that wants the same toggle must declare it too.
