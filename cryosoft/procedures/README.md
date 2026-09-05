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
only when set) and a `persistent` flag; `Command` order is meaningful and
never reordered. Every plan object validates at construction, so a malformed
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

## Known issues

**Magnet and temperature VI names are hardcoded in the procedures.**
`FieldSweep` and `TemperatureSweep` address `magnet_z`, `magnet_y`,
`temperature_vti` and `temperature_sample` as literal strings, and
`TimeSeries` does the same for the channels its end condition can
watch (`_END_CHANNELS`). A setup that
names its instruments differently cannot run the shipped procedures without
editing them — which is what forced the 2026-07-20 global `magnet_x` ->
`magnet_z` rename across every config and test.

This violates the "config files are the single source of truth" principle: the
VI a procedure drives is a *setup* property and belongs in the config. The
intended fix is to derive the names from the Station
(a `magnet_vi_names()` / `temperature_vi_names()` discovery pair, mirroring the
existing `measurement_vi_names()`) and expose them as
procedure parameters, so adding an axis or renaming a magnet needs no procedure
change.

The same applies to the temperature on/off toggles below: both procedures
declare them independently, so a third procedure wanting them must repeat the
declaration. Both are the same underlying gap and should be fixed together.

## Files

Each row: responsibility, key public class, and the test file(s) in `tests/`.

| File | Responsibility | Key public API | Tests |
|------|----------------|----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `field_sweep.py` | Sweeps magnetic field (`magnet_z`), optionally holding `temperature_vti` and/or `temperature_sample` (see Temperature channels below), running any selected measurement VI at each point; parks `magnet_z` at 0 T on standby. Requires `magnet_z`, at least one measurement VI, and a VI for each switched-on temperature channel. | `FieldSweep` (axis hooks over `SweepMeasureProcedure`) | `test_new_procedures.py`, `test_l4_procedure.py` |
| `time_series.py` | Measures repeatedly against elapsed time, commanding no system hardware and claiming only the reading path, so the operator keeps manual control of the whole cryostat during the run. Ends on `max_duration_s`, or when a watched channel (`temperature_vti`, `magnet_z`) reaches `end_value`. Requires at least one measurement VI; a watched channel's VI must exist. | `TimeSeries` (axis hooks + `axis_data_key`/`claimed_vi_names` over `SweepMeasureProcedure`) | `test_time_series_procedure.py`, `test_l3_orchestrator.py` (ramp scope) |
| `temperature_sweep.py` | Sweeps temperature (`temperature_vti`) at a per-sweep ramp rate, optionally holding `temperature_sample` and optional `magnet_z` / `magnet_y` fields, running any selected measurement VI at each stable point. Requires at least one measurement VI; magnets optional (skipped at 0, refused at nonzero when absent). | `TemperatureSweep` (axis hooks over `SweepMeasureProcedure`) | `test_new_procedures.py` |

### Temperature channels (on/off)

Both sweep procedures control the VTI and the sample stage **independently**, each
gated by a bool parameter:

| Parameter | Default | Effect when on |
|---|---|---|
| `set_vti_temperature` | `True` | Emits a `temperature_vti` target — the fixed `temperature` in `FieldSweep`, the swept value in `TemperatureSweep` |
| `set_sample_temperature` | `False` | Emits a `temperature_sample` target at `sample_temperature`, set once in `initiate()` and held |

"Off" means the procedure emits **no `Target`** for that VI, so the Orchestrator
never calls `start_ramp` on it and the controller holds exactly where the operator
left it. Reading is unaffected: monitoring, logging and trends come from the tick
loop's monitor pass, not from targets. A channel that is switched on but has no VI
on the station is refused at construction (`CryoSoftConfigError`); a switched-off
channel is not required to exist.

Both procedures declare these parameters and build the conditional target dicts
themselves — there is deliberately no shared framework mechanism yet, so a new
procedure that wants the same toggles must declare them too.
