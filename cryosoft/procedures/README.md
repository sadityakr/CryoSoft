# procedures/

## Purpose

The `procedures/` package contains concrete measurement procedures. Each procedure is a declarative class that describes *what* the experiment should do — which instruments to target, how to configure measurements, and how to step through a sweep. The `Orchestrator` handles *how* it executes (ramping, waiting, monitoring, data saving).

Procedures are the primary extension point for new experiment types. Adding a new measurement type means adding one file here.

## Architecture layer

L4 — Procedures. Sits above L3 (Orchestrator) and L2 (Station). Depends on `DataManager` (L5) for HDF5 output.

```
GUI → Orchestrator → Procedure → Station → Virtual Instruments → Drivers
                              ↘ DataManager → HDF5
```

## Entry (what comes in)

Each procedure receives at construction time:
- `station`: the `Station` instance (access to all VIs).
- `sample_info`: `{"sample_name": str, "sample_id": str, "comments": str}` — entered by the user in the GUI.
- `data_directory`: path where the HDF5 file will be created.
- `**param_values`: all procedure-specific parameters from the GUI form (must match `parameters` class attribute).

## Exit (what goes out)

The Orchestrator calls four methods in sequence. They return the typed plan
objects from `cryosoft.core.plan` (`Target`, `Command`, `PhasePlan`,
`StepPlan`), not bare dicts/tuples:

| Method | Returns | Called when |
|--------|---------|-------------|
| `initiate()` | `PhasePlan` | Procedure starts |
| `change_sweep_step()` | `StepPlan` or `None` | After each measurement |
| `measure()` | nothing (writes to HDF5) | System is stable at current point |
| `standby()` | `PhasePlan` | Sweep complete or aborted |
| `abort()` | `tuple[Command, ...]` | User abort / ERROR / EMERGENCY |

**Plan formats:**

```python
from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target

# PhasePlan — targets to reach, ordered measurement commands, settle time.
PhasePlan(
    targets={"magnet_x": Target(0.5), "temperature_vti": Target(10.0)},
    commands=(Command("iv_measurement", "configure",
                      {"method": "delta_mode", "current": 1e-6, "n_readings": 100}),),
    wait_s=300.0,
)

# StepPlan — targets for the next sweep point, plus its settle time.
StepPlan(targets={"magnet_x": Target(0.55)}, wait_s=5.0)
```

A `Target` carries an optional `rate` (ramp rate, forwarded to the VI's
`start_ramp()` only when not `None`) and `persistent` flag; `Command.commands`
order is meaningful and is never reordered. Each `Target`/`Command`/plan
validates eagerly at construction, so a malformed plan fails at the procedure
boundary rather than deep in the tick loop.

## Interface contract

All procedures must subclass `BaseProcedure` from `cryosoft.core.procedure`:

Parameters are declared as `ParamSpec` value objects (from
`cryosoft.core.plan`), grouped into `sweep_parameters` / `system_parameters` /
`measurement_parameters`. `ParamSpec` validates each declaration eagerly at
class-definition time; the ParamSpec → Qt-widget mapping lives entirely in
`cryosoft.gui.param_form`, so a procedure never names a widget class.

```python
from cryosoft.core.plan import ParamSpec
from cryosoft.core.procedure import BaseProcedure

class MyProcedure(BaseProcedure):
    name = "My Procedure"
    description = "One-line description"
    system_parameters = {
        # Plain field (GUI text box parsed by `type`):
        "param_name": ParamSpec(type=float, default=1.0, unit="T", description="..."),
    }
    measurement_parameters = {
        # Enumerated (GUI drop-down): choices is a label -> value dict. The
        # collected value is the mapped value, so no translation in the procedure.
        "range": ParamSpec(type=float, default=0.01,
                           choices={"10 mV": 0.01, "1 V": 1.0}, description="..."),
        # Boolean (GUI checkbox):
        "enabled": ParamSpec(type=bool, default=True, description="..."),
    }

    def _build_sweep_array(self) -> list: ...
    def initiate(self) -> PhasePlan: ...
    def change_sweep_step(self) -> StepPlan | None: ...
    def measure(self) -> None: ...
    def standby(self) -> PhasePlan: ...
    def abort(self) -> tuple[Command, ...]: ...
```

**Rules:**
- Procedures never import from `drivers/` or `virtual_instruments/` directly.
- Procedures access instruments only through `self._station` (VI methods).
- `measure()` must create a `DataManager` in `initiate()` (stored as `self._data_manager`) and call `self._data_manager.save_datapoint()`.
- `standby()` must call `self._data_manager.close()` to ensure data is flushed and trimmed.
- All parameters must be declared as `ParamSpec`s in the `sweep_parameters` /
  `system_parameters` / `measurement_parameters` group dicts (auto-unioned into
  `parameters`) — no hardcoded values in logic.
- SI units everywhere: tesla, kelvin, amperes, volts, seconds.

## Generic sweep procedures (the common case)

Most measurement procedures are **generic sweep procedures** built on
`core.procedure.SweepMeasureProcedure`: ONE procedure per sweep axis that runs
ANY measurement VI the station exposes, chosen in the GUI. `FieldSweep` and
`TemperatureSweep` are the two shipped today.

The base owns everything that does not depend on the swept quantity:

- **Measurement-VI selection.** `get_param_groups(station, selections)` builds a
  "Measurement method" group whose single `measurement_vi` parameter is a
  `structural` `ParamSpec` (its `choices` are the station's measurement VIs),
  plus a group carrying the *selected* VI's own `measurement_parameters`. The
  GUI re-renders the measurement group when the selection changes.
- **Construction.** `__init__` resolves the selected VI, merges its parameter
  defaults, and records the selection + the instance's live-plot keys.
- **The four-method loop.** `initiate()` assembles a `DataSchema` (axis column +
  system columns + the VI's arrays/scalars) and arms the VI; `measure()` reads
  the VI, tags on the axis read-back, and saves (the schema is validated per
  datapoint); `standby()` / `abort()` disarm the VI.

**So the everyday extension is a measurement method, not a procedure.** To make
a new measurement runnable by both sweeps, add a measurement VI (subclass
`MeasurementInstrumentBase`, declare its self-description, implement the
lifecycle) and register it in the config with `vi_type: measurement` — no
procedure change at all.

## Switch multiplexing (measuring several routes per point)

When the station exposes a **Switch VI** (`vi_type: switch`, e.g. a Keithley 705
matrix switch), a generic sweep procedure gains a **multiplexing** capability
with no procedure code:

- **How routes appear as checkboxes.** `get_param_groups()` appends a "mux"
  group with one `bool` checkbox per switch route, labelled "Measure route
  `<name>`". The form param is `mux_<route>` (the `mux_` prefix keeps route
  names, which can collide with measurement/system params, in their own
  namespace). No switch VI in the station -> no group, zero behaviour change.
- **Command order at `initiate()`.** If any route is selected, the procedure
  commands `select_route(first_route)` on the switch **before** arming the
  measurement VI (the switch must be connected before the source arms).
- **Per-datapoint loop.** With **two or more** routes selected, `measure()`
  connects each route in turn (`switch.select_route(route)` via the Station) and
  takes one reading per route. With exactly **one** route the switch is selected
  once at `initiate()` and each point is a plain reading.
- **Column naming `{array}__{route}`.** With two or more routes, every
  measurement array and per-point scalar is suffixed per route, e.g.
  `voltage_V__Mux-Ch1`, `current_A__Mux-Ch2`, `n_valid__Mux-Ch1`
  (`DataSchema.multiplexed(routes, scalar_columns=...)`). The sweep axis, system
  columns, and `unix_time` are never suffixed. With 0 or 1 route the columns are
  unsuffixed, exactly as without a switch.
- **Safe-off.** `standby()` and `abort()` append a switch `open_all` command
  whenever routes were selected, so no route is left connected.

The switch is reached only through the `Station` instance (contract C6); the
exclusive-mux policy (one route connected at a time) lives in the Switch VI.

## How to add a new procedure

Adding a *procedure* is only needed for a new **sweep axis** (a new swept
quantity). Subclass `SweepMeasureProcedure` and supply just the axis specifics:

1. Create `procedures/your_sweep.py` with the front-matter block (Workspace Rule 1).
2. Subclass `SweepMeasureProcedure`; set `name`, `description`, a `sweep_axis`
   (gives `_build_sweep_array()` and the GUI mode-selector for free),
   `sweep_data_keys`, `default_x_key`, and any `system_parameters`.
3. Implement the axis hooks: `_initial_system_targets`, `_step_targets`,
   `_standby_targets`, `_axis_readback`, `_initiate_wait_s`, `_step_wait_s`.
   The measurement selection, `DataSchema`, DataManager, and the four-method
   loop are all inherited — do NOT re-declare `measurement_parameters` or
   override `initiate`/`measure`/`standby`/`abort` unless the axis truly needs it.
4. Write tests (see `tests/test_new_procedures.py`), parametrized over the
   measurement VIs the sweep should support.
5. Add the file to this README's file list below.

A procedure with a bespoke (non sweep-and-measure) shape can still subclass
`BaseProcedure` directly and implement the four methods and a `DataManager` by
hand; the `SweepMeasureProcedure` route is the recommended default.

## Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker (empty) |
| `field_sweep.py` | `FieldSweep` ("Field Sweep") — sweeps magnetic field (magnet_x, temperature_vti held), runs the GUI-selected measurement VI at each point. Requires: magnet_x, temperature_vti, and ≥1 measurement VI. |
| `temperature_sweep.py` | `TemperatureSweep` ("Temperature Sweep") — sweeps temperature (temperature_vti, optional magnet_x/magnet_y held fields), runs the GUI-selected measurement VI at each stable point. Requires: temperature_vti and ≥1 measurement VI; magnets optional. |
