# procedures/

## Purpose

`procedures/` holds the concrete measurement procedures: one thin subclass per
**sweep axis** (the swept quantity). Each describes *what* the experiment does
(which axis to sweep, which system VIs to hold); the `Orchestrator` decides
*how* it runs (ramping, settling, monitoring, saving). The generic
sweep-and-measure engine itself lives one layer down in
`cryosoft.core.procedure.SweepMeasureProcedure`; the files here supply only the
axis specifics.

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

A `PhasePlan` carries `targets` (VI name -> `Target`), ordered `commands`, and a
`wait_s` settle time; a `StepPlan` carries the next point's `targets` and its
`wait_s`. A `Target` carries an optional `rate` (forwarded to the VI's
`start_ramp()` only when set) and a `persistent` flag; `Command` order is
meaningful and never reordered. Every plan object validates at construction, so
a malformed plan fails at the procedure boundary, not in the tick loop.

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
- `claimed_vi_names() -> set[str] | None` (`BaseProcedure`, docs/plans/
  operation-concurrency-and-error-scoping.md §1's **Claim** — see
  GLOSSARY.md): declares which VIs a running procedure exclusively owns, so
  the Orchestrator knows what a manual front-panel action may touch while
  it runs. Default `None` (claim everything) — procedures stay exclusive in
  this iteration; no shipped procedure overrides it.

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
attribute: the switch VI's `route` (setter `select_route`, safe-off
`open_all`) and the DC measurement VI's `current_A` (setter
`set_source_current`) are the *same concept*, so a setup with no scanner
simply has one fewer loopable parameter and no special case anywhere. Slot 1
(index labels `A1, A2, …`) is the outer level, slot 2 (`B1, B2, …`) the inner
one.

The **Reading loop** form group renders automatically whenever anything is
loopable: one `{slot}_parameter` drop-down per slot plus that parameter's
values input — per-choice `{slot}_pick_{value}` checkboxes when the ParamSpec
is enumerated (tick the channels), a `{slot}_values` comma-separated text
field otherwise (e.g. `1e-6, -1e-6`), each value validated against the
parameter's own spec at construction. A slot with ONE value is a static
setting (dispatched once at `initiate()`, no suffix); with two or more it
loops: the setter is dispatched as a `Command` through the Station before
every reading and columns compose as `{name}__A{i}__B{j}`. The label -> value
map is stored in the HDF5 metadata (`procedure_params["loop_labels"]`);
participating non-measurement VIs get their `reading_safe_off` at
standby/abort. The live plots mirror the slots with per-plot Loop 1 / Loop 2
selectors (fed by `live_plot_loop_labels()`, items like "A1 = Mux-Ch1"); axis
keys stay the plain column names and the panel composes the suffix at draw
time.

## Operations

`procedures/operations/` (its own `README.md`) holds a DIFFERENT kind of
request: cryostat-**servicing** actions (helium fill, sample change), each a
subclass of `cryosoft.core.operation.OperationBase` rather than
`BaseProcedure`. An operation is declarative like a procedure — it returns
the same `PhasePlan`/`StepPlan`/`Target`/`Command`/`Gate` currency and is
driven by the same Orchestrator tick loop — but carries operation-scope
command access, tolerated safety flags, a verified `postcondition_gates()`
phase, and higher submission priority; it is never returned among the
measurement procedures discovered from this folder. See
`docs/plans/cryogenics-logbook.md` §2/§4 for the design and
`procedures/operations/README.md` for its own entry/exit/interface contract.

## How to add a new module

Add a procedure only for a new **sweep axis**. To add a new *measurement*
instead, add a measurement VI and register it with `vi_type: measurement`; both
shipped sweeps pick it up with zero procedure change. To add a new
cryostat-servicing action, see `procedures/operations/README.md` instead —
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
`temperature_vti` and `temperature_sample` as literal strings. A setup that
names its instruments differently cannot run the shipped procedures without
editing them — which is what forced the 2026-07-20 global `magnet_x` ->
`magnet_z` rename across every config and test.

This violates the "config files are the single source of truth" principle: the
VI a procedure drives is a *setup* property and belongs in the config, exactly
as `sample_change` already does it (`operations.sample_change.vti_vi`, defaulted
in `station.py`). The intended fix is to derive the names from the Station
(a `magnet_vi_names()` / `temperature_vi_names()` discovery pair, mirroring the
existing `measurement_vi_names()` / `switch_vi_names()`) and expose them as
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
| `field_sweep.py` | Sweeps magnetic field (`magnet_z`), optionally holding `temperature_vti` and/or `temperature_sample` (see Temperature channels below), running any selected measurement VI at each point; parks `magnet_z` at 0 T on standby. Requires `magnet_z`, at least one measurement VI, and a VI for each switched-on temperature channel. | `FieldSweep` (axis hooks over `SweepMeasureProcedure`) | `test_new_procedures.py`, `test_l4_procedure.py`, `test_field_voltage_procedure.py` |
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
