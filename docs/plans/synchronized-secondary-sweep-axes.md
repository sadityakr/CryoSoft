# Synchronized secondary sweep axes

**Status: proposal, not started.**

## Problem

`TemperatureSweep` (`cryosoft/procedures/temperature_sweep.py`) sweeps only
the VTI temperature. The sample-stage temperature is a separate, held-constant
"System" parameter (`sample_temperature`, set once at `initiate()` via
`set_sample_temperature`, never re-sent per step). The real operating pattern
is to ramp **both** VTI and sample temperature together, index-for-index, with
independent start/end values — typically the VTI trailing a few kelvin below
the sample setpoint (e.g. VTI 98 K -> 298 K while sample runs 100 K -> 300 K),
so the VTI always has cooling headroom relative to the sample loop. The VTI
controller must always be present (it is the swept axis and the mandatory
bath control); the sample controller must remain fully optional, since not
every cryostat has a sample-stage heater.

This should not be one-off code in `temperature_sweep.py`: `SweepMeasureProcedure`
currently supports exactly **one** `SweepAxis` per procedure
(`cryosoft/core/sweep_builder.py`, `cryosoft/core/procedure.py`), and
`FieldSweep` will eventually need the same mechanism to sweep two or three
magnets together (e.g. an angular sweep walking `magnet_z`/`magnet_y` in
lockstep to trace a fixed-magnitude field at varying angle). The design below
adds a "secondary sweep axis" concept to the shared framework — reusable, not
temperature-specific — and applies it concretely to `TemperatureSweep`.
`FieldSweep` needs no functional change today (neither of its temperature
channels is swept there), but gets the same mechanism for free whenever a
future angular/multi-magnet sweep is built.

A narrower, real bug rides along with this and should be fixed in the same
change: `TemperatureSweep.__init__` only checks that `temperature_vti` exists
when `set_vti_temperature` is `True` — but `_axis_readback()` reads it
unconditionally every measurement, regardless of that toggle. This fix folds
naturally into the construction-time validation being rewritten here.

## What already exists (don't rebuild this)

Investigated in full before designing (all file:line citations verified by
reading the actual source, not assumed):

- `cryosoft/core/sweep_builder.py` — `SweepAxis` (frozen dataclass),
  `sweep_axis_param_specs()`, `build_axis_sweep()`. Already per-axis and
  reusable; no multi-axis assumption is baked into these three functions
  themselves — only their single-axis *callers* are.
- `cryosoft/core/procedure.py` — `BaseProcedure.sweep_axis: SweepAxis | None`
  (`:129`), `__init_subclass__` merging its hidden params (`:153-161`),
  `get_param_groups()` (`:163-200`), `_build_sweep_array()` (`:306-318`),
  `self._sweep: list` built once in `__init__` (`:299`), `axis_data_key()`
  (`:919-943`), the six `SweepMeasureProcedure` hooks (`:1317-1339`),
  `measure()`'s one-column write (`:1646`), `_build_data_schema()`'s
  one-column add (`:1444`), `change_sweep_step()`'s `len(self._sweep)` bound
  (`:1551`).
- `cryosoft/gui/sweep_axis_widget.py` — `SweepAxisWidget(axis)` is already
  fully self-contained per axis: every objectName is keyed by `axis.key`, so
  multiple instances with distinct keys already coexist with zero collisions.
  Its whole public contract is `param_keys()` / `get_params()`.
- `cryosoft/gui/procedure_params_panel.py` — `_build_param_form()`
  (`:187-280`) builds exactly one `SweepAxisWidget` when `cls.sweep_axis is
  not None` (`:245-247`); `collect_values()` (`:590-632`) merges its
  `get_params()` in. `cryosoft/gui/queue_panel.py`'s `_queue_summary_parts`
  (`:262-287`) has the same singular pattern for the one-line queue summary.
- **The data layer needs no change.** `DataSchema.sweep_columns`
  (`cryosoft/core/plan.py:629`) is already an arbitrary `dict[str, str]`, and
  `DataManager.save_datapoint()` (`cryosoft/core/data_manager.py:319-321`)
  just writes whatever named float column shows up in the datapoint dict that
  is declared in the schema — multi-axis is purely an L4 (procedure) + GUI
  concern, confirmed by reading both call sites.
- Magnet/rotator landscape (for sizing the future field-sweep use case): only
  two magnet VI classes exist (`SuperconductingMagnetVI`,
  `SuperconductingMagnetPersistentVI`), both single-axis under `MagnetBase`;
  `sim_cryostat` is the only shipped config with two magnets (`magnet_z` +
  `magnet_y`) — every real-cryostat config has only `magnet_z`.
  `RotatorBase`/`RotatorVI` exists (sim only, `sim_cryostat/devices.yaml:137`)
  but is unrelated to magnets and unused by any procedure today — angular /
  multi-magnet sweeping is new design, not an extension of partial work,
  confirming this should land as a framework capability now and be adopted by
  a future field-sweep variant later, not built out fully in this change.
- `cryosoft/procedures/README.md`'s existing "Known issues" section
  (`:202-224`) already flags hardcoded VI names and duplicated toggle-param
  declarations across `FieldSweep`/`TemperatureSweep` as "the same underlying
  gap." This proposal does not fix that gap (Station-level VI-name discovery
  is a separate, already-scoped issue) — it only adds the multi-axis
  mechanism, which is orthogonal.

## Proposed change

### Framework: additive, not a rename

Keep `BaseProcedure.sweep_axis: SweepAxis | None` exactly as it is today —
the one mandatory, always-swept axis. Every existing single-axis procedure
(`FieldSweep`, `TimeSeries`) needs zero changes to its declaration or
behavior. Add two things:

```python
# sweep_builder.py — SweepAxis gains one new optional field
enabled_param: str | None = None
```
Names a `bool` `ParamSpec` (declared normally in the procedure's
`system_parameters`, unchanged) that gates whether this axis is *actively
commanded* each step. `None` (the default) means always-active. Usable by
both the primary `sweep_axis` (optional — `FieldSweep`'s `field` axis leaves
it `None`, since "don't sweep the field" isn't a real mode) and, always, by
any `secondary_sweep_axes` entry.

```python
# procedure.py — BaseProcedure, new class attribute
secondary_sweep_axes: tuple[SweepAxis, ...] = ()
```
Axes swept in lockstep with `sweep_axis`, sharing its index and step count.

Changes in `cryosoft/core/procedure.py`:

- `__init_subclass__` (`:153-161`): merge `sweep_axis_param_specs()` for
  `sweep_axis` **and** every entry of `secondary_sweep_axes` into
  `cls.parameters` (each axis's hidden params are already namespaced by its
  own `key`, so no collisions between axes).
- `get_param_groups()` (`:163-200`): compute `axis_owned = {ax.enabled_param
  for ax in (cls.sweep_axis, *cls.secondary_sweep_axes) if ax and
  ax.enabled_param}` and exclude those names from the rendered
  `system_parameters` dict — they move to the Sweep column instead (see GUI
  below). This is the mechanical fix for "these belong in the sweep box, not
  System."
- `BaseProcedure.__init__` (near `:299`, right after `self._sweep =
  self._build_sweep_array()`): build `self._secondary_sweeps: dict[str,
  list[float]] = {axis.key: build_axis_sweep(axis, self._params) for axis in
  type(self).secondary_sweep_axes}`, then validate every secondary array's
  length equals `len(self._sweep)`, raising `CryoSoftConfigError` naming both
  axes and their step counts otherwise. Validated unconditionally (not gated
  by `enabled_param`) — defaults are chosen so this is a no-op unless the
  operator deliberately mismatches step counts.
- `SweepMeasureProcedure` (`:1317-1339`): add one new optional hook,
  `_secondary_axis_readbacks(self) -> dict[str, float]`, default `{}` — the
  data_key -> value pairs for every active secondary axis (mirrors
  `_axis_readback()` but plural/keyed). Existing procedures need no change
  (the default covers them).
- `measure()` (`:1646`): one new line after the existing axis write —
  `measured_data.update(self._secondary_axis_readbacks())`.
- `_build_data_schema()` (`:1444`): after the existing
  `sweep_columns[axis_data_key()] = "float"`, loop
  `for axis in type(self).secondary_sweep_axes: sweep_columns[axis.data_key] = "float"`.
- Everything else (`_build_sweep_array()`, `change_sweep_step()`'s bound,
  `get_sweep_array()`, `get_progress()`, `axis_data_key()`) is untouched.

Changes in the GUI:

- `cryosoft/gui/procedure_params_panel.py`: factor the existing "build one
  `SweepAxisWidget`" block (`:245-247`) into a helper used for BOTH
  `cls.sweep_axis` and each `cls.secondary_sweep_axes` entry. For an axis with
  `enabled_param` set, wrap it: a `QCheckBox` (built via the existing
  `param_form.build_param_widget`/`build_param_tooltip`, same as any other
  bool param) placed above the `SweepAxisWidget`, wired directly
  (`checkbox.toggled.connect(widget.setEnabled)`) so the range/mode editor
  visibly greys out when the axis is off. Track the new widgets in
  `self._secondary_axis_widgets: dict[str, SweepAxisWidget]` and
  `self._axis_toggle_widgets: dict[str, QCheckBox]`.
- `collect_values()` (`:590-632`): union `param_keys()`/`get_params()` across
  `self._axis_widget` and every `self._secondary_axis_widgets` entry; collect
  each `self._axis_toggle_widgets` entry's value directly via
  `param_form.collect_value` (bool checkboxes never raise, matching the
  existing comment at `:616`), since `get_param_groups()` no longer includes
  these names in any rendered group.
- `cache_current_params()` / `_apply_cached_params()`: extend the existing
  `f"{group.key}::{name}"` caching scheme to also snapshot/restore the axis
  toggle checkboxes (e.g. under a `"sweep::{name}"` key).
- `cryosoft/gui/queue_panel.py`'s `_queue_summary_parts` (`:262-287`): append
  one more `"{key}={start}->{end}"` part per active secondary axis to the
  queue's one-line summary.

### Concrete application: `TemperatureSweep`

`cryosoft/procedures/temperature_sweep.py`:

```python
sweep_axis = SweepAxis(
    key="temperature", unit="K", data_key="temperature_K",
    description="VTI temperature",
    default_start=10.0, default_end=300.0, default_steps=30,
    enabled_param="set_vti_temperature",
)
secondary_sweep_axes = (
    SweepAxis(
        key="sample_temperature", unit="K", data_key="sample_temperature_K",
        description="Sample-stage temperature",
        default_start=10.0, default_end=300.0, default_steps=30,
        enabled_param="set_sample_temperature",
    ),
)
sweep_data_keys = [sweep_axis.data_key, secondary_sweep_axes[0].data_key]
```

`system_parameters` drops the old constant `sample_temperature` ParamSpec
entirely (its start/end/steps are now the secondary axis's hidden params);
`set_vti_temperature` and `set_sample_temperature` stay declared here
(unchanged names) but render in the Sweep column per the GUI change above.
`field_z`, `field_y`, `ramp_rate_K_per_min`, `point_wait` are unchanged.

`__init__`: keep the existing magnet-presence loop unchanged. Replace the
temperature-toggle presence loop with:
- `temperature_vti` presence checked **unconditionally** (the bug fix —
  `_axis_readback()` always needs it regardless of `set_vti_temperature`).
- `temperature_sample` presence checked only `if
  self._params["set_sample_temperature"]` (unchanged semantics).

Hooks: replace the old held-constant-sample logic with a single
`_temperature_targets(index)` building both `temperature_vti` (from
`self._sweep[index]`) and `temperature_sample` (from
`self._secondary_sweeps["sample_temperature"][index]`) Targets, each gated by
its own toggle, both using the shared `ramp_rate_K_per_min`. Used by both
`_initial_system_targets()` (index 0, plus magnets) and `_step_targets(index)`
— sample is now genuinely **re-sent every step**, which is the behavioral
fix. `_axis_readback()` is unchanged. Add `_secondary_axis_readbacks()`
returning `{"sample_temperature_K": station.temperature_sample.temperature()}`
when active, else `{}`.

`FieldSweep`: no functional change. Its `set_vti_temperature`/`temperature`/
`set_sample_temperature`/`sample_temperature` stay exactly as today
(held-constant System params — correct, since neither channel is swept
there); add/confirm test coverage that this remains true.

### Docs to update in the same change

- `GLOSSARY.md`'s "Sweep axis" entry (`:134`) — document
  `secondary_sweep_axes` and `enabled_param` alongside the existing
  `sweep_axis` description.
- `cryosoft/procedures/README.md` — the "How to add a new module" recipe
  (`:185-193`) and the "Temperature channels (on/off)" section (`:237-256`,
  now largely superseded for `TemperatureSweep`).
- `cryosoft/gui/README.md`'s `sweep_axis_widget.py` row (`:183`).
- `cryosoft/core/README.md`'s `sweep_builder.py`/`procedure.py` rows.

### Tests

- `tests/test_sweep_builder.py`: cover `SweepAxis.enabled_param` (default
  `None`, unaffected by `sweep_axis_param_specs()` since it's framework/GUI
  metadata, not a hidden param itself).
- `tests/test_l4_procedure.py`: a throwaway `BaseProcedure` subclass with both
  `sweep_axis` and `secondary_sweep_axes` set — cover `__init_subclass__`'s
  merged params, `_secondary_sweeps` construction, and the equal-length
  `CryoSoftConfigError`.
- `tests/test_new_procedures.py`:
  - Replace `test_temp_sweep_holds_sample_setpoint_only_on_initiate`: sample
    is now re-sent every step; assert `change_sweep_step()`'s target reflects
    the sample axis's own value at that index, distinct from index 0.
  - New: mismatched `temperature_steps`/`sample_temperature_steps` ->
    `CryoSoftConfigError` at construction.
  - New: station missing `temperature_vti` -> `CryoSoftConfigError`
    regardless of `set_vti_temperature`'s value (the bug fix).
  - Confirm station missing `temperature_sample` with the toggle off still
    constructs fine (already implicitly covered by the existing
    `_partial_station("magnet_z", "temperature_vti", "dc_measurement")`
    pattern).
  - New: both axes active with different start/end (e.g. VTI 10->100, sample
    12->102, 3 steps) — assert index-for-index Target values at steps 0/1/2
    for both VIs, proving the lockstep sync.
- `tests/test_gui.py`: update the `_axis_widget is not None` assertions
  (`:655-698`) for the new `_secondary_axis_widgets`/toggle-checkbox
  structure; add a check that the sample axis's widget is disabled when its
  checkbox is unchecked and enabled when checked.

### Verification

1. `& ".venv\Scripts\python.exe" -m pytest tests/ -q` and `-m "not hardware"`.
2. `ruff check .` and `lint-imports` (the `make check` targets).
3. Offscreen GUI screenshot smoke (gui-edit skill's mandatory step): select
   "Temperature Sweep" in a `ProcedureWindow` built against `sim_cryostat`,
   screenshot the Sweep column, confirm both axis editors render with their
   checkboxes and that unchecking "Sweep sample temperature" visibly greys
   out its start/end/steps fields.
4. Manually construct a `TemperatureSweep` against `sim_cryostat` with both
   axes active and mismatched step counts to confirm the new
   `CryoSoftConfigError` message is clear and actionable.
