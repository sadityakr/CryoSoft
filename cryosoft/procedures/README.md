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
  selected VI's own `measurement_parameters`, and, when a switch VI is present,
  the multiplexing checkboxes.
- SI units everywhere: tesla, kelvin, amperes, volts, seconds.

### Generic sweep and the reading loop (owned by the base, no per-procedure code)

`SweepMeasureProcedure` runs ANY measurement VI the station exposes, chosen in
the GUI, so a new *measurement method* is a new measurement VI, not a new
procedure. `initiate()` assembles a `DataSchema` (axis column + system columns +
the VI's arrays/scalars) and arms the VI; `measure()` runs the **reading loop**
(below), tags on the axis read-back, validates per datapoint, and saves;
`standby()` / `abort()` disarm the VI.

The reading loop is the standard for taking multiple readings at a single
sweep point. It has two nested, independently optional levels, each dispatching
its setup as `Command`s through the Station before every reading and suffixing
that reading's columns via `DataSchema.multiplexed(...)`:

- **Routes (outer level — channels).** When the station exposes a switch VI
  (`vi_type: switch`) and the scanner is enabled, `get_param_groups()` appends
  one `mux_<route>` checkbox per route; with two or more routes selected,
  `measure()` connects each route in turn and suffixes `{name}__{route}`
  (e.g. `voltage_V__Mux-Ch1`). `standby()` / `abort()` append a switch
  `open_all` when routes were used.
- **Reading variants (inner level — reading settings).** The selected
  measurement VI may declare, via its `reading_variants(vi_name, params)` hook,
  that every point comprises several readings under different configurations
  (e.g. the DC VI's `bipolar` checkbox expands into `pos` / `neg` +/- current
  variants). Each variant's commands run before its reading and its columns
  are suffixed `{name}__{variant}`. Adding per-point reading settings is
  therefore a measurement-VI change, never a procedure change.

Suffixes compose inner level first: `{name}__{variant}__{route}`. A level that
does not loop (no variants, 0 or 1 route) adds no suffix and no commands; the
axis, system columns, and `unix_time` are never suffixed.

## How to add a new module

Add a procedure only for a new **sweep axis**. To add a new *measurement*
instead, add a measurement VI and register it with `vi_type: measurement`; both
shipped sweeps pick it up with zero procedure change.

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

## Files

Each row: responsibility, key public class, and the test file(s) in `tests/`.

| File | Responsibility | Key public API | Tests |
|------|----------------|----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `field_sweep.py` | Sweeps magnetic field (`magnet_x`), holding `temperature_vti`, running any selected measurement VI at each point; parks `magnet_x` at 0 T on standby. Requires `magnet_x`, `temperature_vti`, and at least one measurement VI. | `FieldSweep` (axis hooks over `SweepMeasureProcedure`) | `test_new_procedures.py`, `test_l4_procedure.py`, `test_field_voltage_procedure.py` |
| `temperature_sweep.py` | Sweeps temperature (`temperature_vti`) at a per-sweep ramp rate, holding optional `magnet_x` / `magnet_y` fields, running any selected measurement VI at each stable point. Requires `temperature_vti` and at least one measurement VI; magnets optional (skipped at 0, refused at nonzero when absent). | `TemperatureSweep` (axis hooks over `SweepMeasureProcedure`) | `test_new_procedures.py` |
