# Synchronized sweep variables

**Status: proposal, not started.** Supersedes and renames the earlier
"synchronized secondary sweep axes" proposal (this file was
`synchronized-secondary-sweep-axes.md`) — re-modeled, not just renamed,
because no swept quantity is architecturally secondary. `TemperatureSweep`'s
VTI/sample lockstep, a future field-angle sweep, and a future vector-magnet
or magnet+rotator composition are all instances of one general pattern
(below), just with different transforms. Two examples are worked through:
`TemperatureSweep` (Part 1, ready to build now) and a future
`FieldAngleSweep` (Part 2, framework-only — no concrete procedure yet, no
real vector-magnet or rotator-equipped config exists to build one against).

## Vocabulary

Used throughout this document and, once implemented, throughout the
codebase:

- **Sweep variable**: a logical, user-dialed quantity that gets its own
  shape — linear / segments / CSV / hysteresis — via the existing
  `SweepAxis` machinery (`cryosoft/core/sweep_builder.py`). A procedure
  declares a collection of these; in the overwhelming majority of
  procedures it's exactly one (`FieldSweep`'s field, `TemperatureSweep`'s
  temperature), but the architecture supports more than one from day one so
  a genuine multi-variable raster (sweeping theta and phi together) is not
  a special case later. No new Python class: a sweep variable **is** a
  `SweepAxis` instance — only the vocabulary is new, not the type.
- **Target variable**: a physical, VI-mapped quantity — what actually gets
  ramped. Computed from the active sweep variable(s) plus any fixed
  **sweep setup parameters** via a `forward()` function; the reverse,
  `inverse()`, recovers a sweep variable's logical value from physical
  readbacks. New dataclass, `TargetVariable` (below).
- **Sweep setup parameter**: a fixed (not swept) value that parametrizes
  `forward()`/`inverse()` — a calibration offset, a held-constant
  magnitude, a fixed angle. Declared as an ordinary `ParamSpec` in the
  existing `sweep_parameters` class attribute (already exists, already
  renders in the Sweep GUI column, unused by any shipped procedure today).
- **Coupling mode**: when a procedure supports more than one way of
  relating its sweep/target variables (`TemperatureSweep`'s independent
  VTI+sample vs. one temperature + a VTI offset), the choice between them
  is an ordinary `structural=True` `ParamSpec` — the exact mechanism that
  already drives the Measurement column's VI selector today
  (`cryosoft/gui/procedure_params_panel.py`'s `current_selections()` at
  `:421` and `_on_structural_changed()` at `:441`) — not a new framework
  concept. It is declared in `system_parameters`, not `sweep_parameters`:
  a coupling mode decides *which VIs get commanded and how they're
  claimed* (the same category as the existing `set_vti_temperature`/
  `set_sample_temperature` presence toggles), not a fixed value
  parametrizing the transform (that's what a sweep setup parameter is
  for). It therefore renders in the System column, alongside its
  presence-toggle siblings — categorization, not column order, is why it
  lives there: `get_param_groups(station, selections)` receives the
  complete `selections` dict every time regardless of which group a
  structural param happens to render in or which order groups are built
  in, so a `coupling_mode` living in Sweep instead would work exactly the
  same mechanically. System-first (see "GUI" below) is a separate,
  independently-motivated change.
- **`forward()` / `inverse()`**: not a mandated abstract method with a
  fixed signature — a *naming convention* for the pair of functions that
  do the actual math (linear offset, spherical-to-Cartesian, …), called
  from inside a procedure's axis hooks (`_initial_system_targets`,
  `_step_targets`, `_standby_targets`, `_axis_readback`,
  `_auxiliary_readbacks` — five hooks that consume `forward()`/`inverse()`
  results; `_initiate_wait_s`/`_step_wait_s` are the other two of
  `SweepMeasureProcedure`'s six axis hooks and are unrelated to either
  transform). A procedure's `_forward()`/`_inverse()` are themselves
  *adapters*: they resolve `self._sweep_values[...][index]` and
  `self._params[...]` (impure, procedure-state-reading) and then either
  compute directly inline, when the math is trivial (Part 1's
  `TemperatureSweep`, a one-line subtraction), or delegate to a pure free
  function in a shared module when it isn't (Part 2's
  `vector_geometry.spherical_to_cartesian()`). Same convention, same two
  names, in both cases — only whether the math lives inline or in a
  shared pure function changes with its complexity. They contain **zero**
  GUI code — the GUI only collects the raw sweep-variable/setup-param
  values and displays computed readbacks; it never evaluates a transform.
  `_inverse()` is the optional half of the pair — only needed when a
  procedure must recover a *logical* sweep-variable value from a
  *physical* readback (Part 2's achieved-angle computation). Part 1's
  `TemperatureSweep` has no `_inverse()`: its sample readback is a direct
  VI read with no coordinate recovery involved, so nothing in Part 1
  actually instantiates the inverse half of the convention — Part 2 is
  where it is first demonstrated.

Trivial, existing procedures are unaffected by any of this: `sweep_axis`
(singular), the `SweepAxis` class, `axis_data_key()`, and `_axis_readback()`
stay exactly as they are today. They already describe the degenerate case —
one sweep variable whose `forward()` is the identity function, so target
variable and sweep variable are the same thing and no transform is ever
written down. `FieldSweep` and `TimeSeries` need zero changes to their
sweep-variable declarations or behavior. `TimeSeries` already narrows
`claimed_vi_names()` itself and needs nothing further; `FieldSweep` gets
one small, independent addition on this front — see "Claim narrowing"
under Part 1's concrete applications below — unrelated to the
sweep-variable mechanism itself.

## Problem

`TemperatureSweep` (`cryosoft/procedures/temperature_sweep.py`) sweeps only
the VTI temperature. The sample-stage temperature is a separate,
held-constant "System" parameter (`sample_temperature`, set once at
`initiate()` via `set_sample_temperature`, never re-sent per step). The real
operating pattern is to ramp **both** VTI and sample temperature together,
index-for-index, and there are two ways operators actually want to dial
this in:

1. **Independent**: separate start/end/steps for VTI and sample (e.g. VTI
   98 K -> 298 K while sample runs 100 K -> 300 K) — full control, two
   numbers to keep in sync by hand.
2. **Offset-coupled**: one temperature range plus a single offset and rate
   — e.g. "Temperature -200, offset 2, rate 10" ramps the *sample* through
   -200 K (and whatever range is dialed) at 10 K/min, with VTI
   automatically held 2 K below it at every point (`VTI = sample - offset`)
   so the VTI always has cooling headroom relative to the sample loop. One
   number to dial, the physically-important invariant (VTI stays below
   sample) held automatically instead of by operator arithmetic.

The VTI controller must always be present in both modes (it is the
mandatory bath control); the sample controller is optional in Independent
mode (not every cryostat has a sample-stage heater) but is required
whenever Offset-coupled mode is selected — coupling to an absent channel is
meaningless.

This should not be one-off code in `temperature_sweep.py`. Two separate
generalizations are riding on this one procedure:

- **N synchronized sweep variables** — `SweepMeasureProcedure` currently
  supports exactly one `SweepAxis` per procedure. `FieldSweep` will
  eventually need the same lockstep mechanism to sweep two or three magnets
  together.
- **A sweep variable related to a target variable by a formula, selectable
  at run-setup time** — the offset-coupled mode is the simplest possible
  instance of the same pattern a future field-angle sweep needs (theta,
  phi, magnitude -> Cartesian magnet components). Building the general
  mechanism here, sized against a formula anyone can sanity-check
  (subtraction), is deliberately easier ground to prove the framework on
  before Part 2 tackles trigonometry.

Two narrower, real bugs ride along with this and should be fixed in the
same change:

- `TemperatureSweep.__init__` only checks that `temperature_vti` exists
  when `set_vti_temperature` is `True` — but `_axis_readback()` reads it
  unconditionally every measurement, regardless of that toggle. This fix
  folds naturally into the construction-time validation being rewritten
  here.
- Neither `FieldSweep` nor `TemperatureSweep` overrides `claimed_vi_names()`
  (`cryosoft/core/procedure.py:429-452`), so both inherit the `None`
  default — "claim every VI on the station" — regardless of what their
  `set_vti_temperature`/`set_sample_temperature` toggles are set to. With a
  toggle off, no `Target` is ever sent to that channel (`_sweep_targets()`
  in `temperature_sweep.py:163-165` returns `{}`), but the Orchestrator
  still refuses any manual front-panel action on it for the whole run — it
  captures `claimed_vi_names()` once into `self._active_claims` at run
  start (`orchestrator.py:599-600`) and `_manual_action_admissible()`
  checks every manual action against that captured set — so the operator
  cannot touch a controller the procedure has explicitly chosen not to
  drive. Magnets are
  unaffected (both procedures only put a magnet in their targets dict when
  the station actually has it, and always target it when present, so
  claiming it is already correct — the same reasoning
  `HeliumFillOperation.claimed_vi_names()` uses). This is not a new
  pattern to invent: `cryosoft/procedures/operations/README.md`'s
  "Pre-run toggles" section already documents exactly this fix (a toggle
  drives both whether a VI is commanded and whether it's claimed, in
  lockstep), demonstrated by `_SampleAccessOperationBase`'s
  `disarm_measurement_vis`. See "Claim narrowing" under each procedure
  below.

## What already exists (don't rebuild this)

Investigated in full before designing (all file:line citations verified by
reading the actual source, not assumed):

- `cryosoft/core/sweep_builder.py` — `SweepAxis` (frozen dataclass),
  `sweep_axis_param_specs()`, `build_axis_sweep()`. Already per-variable and
  reusable; no single-variable assumption is baked into these three
  functions themselves — only their single-variable *callers* are. Reused
  as-is for every sweep variable, whether a procedure has one or several.
- `cryosoft/core/procedure.py` — `BaseProcedure.sweep_axis: SweepAxis | None`
  (`:129`), `__init_subclass__` merging its hidden params (`:153-161`),
  `get_param_groups()` (`:163-200`, already `station`/`selections`-aware —
  see below), `_build_sweep_array()` (`:306-318`, an existing per-subclass
  override point), `self._sweep: list` built once in `__init__` (`:299`),
  `axis_data_key()` (`:919-943`, already has a documented override path for
  procedures without a single obvious axis — `TimeSeries` uses it today),
  the six `SweepMeasureProcedure` hooks (`:1317-1339`), `measure()`'s
  one-column write (`:1646`), `_build_data_schema()`'s one-column add
  (`:1444`), `change_sweep_step()`'s `len(self._sweep)` bound (`:1551`).
- **The `structural` ParamSpec mechanism is real and already proven**, not
  a documented-but-unused hook. `cryosoft/gui/procedure_params_panel.py`
  already drives the Measurement column's VI selector through it end to
  end: `_connect_structural()` (`:412`) wires a structural widget's change
  signal, `current_selections()` (`:421`) reads every rendered structural
  param's current value, `_on_structural_changed()` (`:441`) re-derives
  `cls.get_param_groups(station, selections)` and diffs the rendered
  columns against the new result. A `coupling_mode` structural param is a
  second user of a mechanism the codebase already exercises daily, not a
  new one.
- `cryosoft/gui/sweep_axis_widget.py` — `SweepAxisWidget(axis)` is already
  fully self-contained per variable: every objectName is keyed by
  `axis.key`, so multiple instances with distinct keys already coexist
  with zero collisions. Its whole public contract is
  `param_keys()` / `get_params()`.
- `cryosoft/gui/procedure_params_panel.py`'s `_build_param_form()`
  (`:187-280`) currently builds exactly **one** `SweepAxisWidget`,
  unconditionally read off the class attribute `cls.sweep_axis`
  (`:245-247`) and added to the row *before* the loop over
  `get_param_groups()`'s other groups (`:234-254` happens first,
  unconditionally, ahead of the `for group in groups:` loop at `:259`).
  This is the actual reason today's column order is always Sweep-first,
  and it is why the Sweep column is also the one column excluded from the
  structural re-render diff (`_is_column_key()`, `:506-511`, explicitly
  filters out `key == "sweep"`) — it was never folded into the generic
  per-group machinery the System/Reading-loop/Measurement columns already
  share. Both facts matter directly for this proposal — see "GUI" below,
  where the fix is to fold Sweep into that shared machinery rather than
  special-case it further.
- `cryosoft/core/plan.py`'s `ParamGroup`/`ParamSpec` are frozen,
  `__post_init__`-validated dataclasses with an established pattern for
  adding a new field (validate type, defensively copy, reject collisions)
  — the precedent this proposal follows when it adds a new `axes` field to
  `ParamGroup` (see "Framework" below) rather than inventing a
  parallel, unvalidated side-channel for sweep-axis data.
- **The data layer needs no change.** `DataSchema.sweep_columns`
  (`cryosoft/core/plan.py:629`) is already an arbitrary `dict[str, str]`,
  and `DataManager.save_datapoint()`
  (`cryosoft/core/data_manager.py:319-321`) just writes whatever named
  float column shows up in the datapoint dict that is declared in the
  schema — multi-variable is purely an L4 (procedure) + GUI concern,
  confirmed by reading both call sites.
- Magnet/rotator landscape (for sizing Part 2): only two magnet VI classes
  exist (`SuperconductingMagnetVI`, `SuperconductingMagnetPersistentVI`),
  both single-axis under `MagnetBase`; `sim_cryostat` is the only shipped
  config with two magnets (`magnet_z` + `magnet_y`) — every real-cryostat
  config has only `magnet_z`. `RotatorBase`/`RotatorVI` exists (sim only)
  but is unrelated to magnets and unused by any procedure today.
- `cryosoft/procedures/README.md`'s existing "Known issues" section
  (`:202-224`) already flags hardcoded VI names across
  `FieldSweep`/`TemperatureSweep` as a separately-scoped gap (a future
  `magnet_vi_names()`/`temperature_vi_names()`-style discovery pair on
  `Station`). This proposal does not fix that gap — it only adds the
  multi-variable mechanism, which is orthogonal — but Part 2 leans on it
  explicitly for the vector-magnet-vs-rotator interchangeability question.
- **The claim/admission-gate mechanism is real and already has a narrowing
  precedent**, same story as the `structural` mechanism above.
  `BaseProcedure.claimed_vi_names()` (`:429-452`) defaults to `None` ("claim
  every VI"); the Orchestrator captures it once at run start
  (`orchestrator.py:599-600`, `self._active_claims`) and consults it on
  every manual front-panel action for the run's duration. Narrowing it is
  an established, documented pattern, not a new one:
  `cryosoft/procedures/operations/README.md`'s "Pre-run toggles" section,
  `_SampleAccessOperationBase`'s `disarm_measurement_vis`
  (`sample_access_base.py:399`), and `HeliumFillOperation.claimed_vi_names()`
  (`helium_fill.py:199-213`, narrows to the level meter + magnets, leaving
  "the VTI and everything else... manually controllable while the fill
  runs") all demonstrate the same shape: a toggle drives both what gets
  commanded and what gets claimed, in lockstep, off one flag. `FieldSweep`/
  `TemperatureSweep` just never got this treatment for their temperature
  toggles.

## Proposed change

### Framework

**Where `SweepAxis` and `TargetVariable` live.** `ParamGroup.axes` (below)
needs `SweepAxis` importable from `plan.py` — but `sweep_builder.py`
(`SweepAxis`'s current home) already imports `ParamSpec` from `plan.py`
(`sweep_builder.py:9`), so adding an `isinstance(entry, SweepAxis)` check
to `plan.py` would close a `plan.py -> sweep_builder.py -> plan.py`
cycle: a direct violation of CLAUDE.md's "never create a circular
import; if one seems necessary, the design is wrong and needs
refactoring." The fix is not a validation workaround (e.g. duck-typing
around the `isinstance` check) — it's moving `SweepAxis` to where it
already conceptually belongs. `plan.py`'s own module docstring calls it
"Typed vocabulary of frozen dataclasses shared across all CryoSoft
layers," and `SweepAxis` is exactly that: a frozen dataclass every layer
above L2 needs to reference, same as `ParamSpec`/`ParamGroup` already
are. So, as part of this change:

- `SweepAxis` moves from `sweep_builder.py` to `plan.py`, alongside
  `ParamSpec`/`ParamGroup`/`Target` (dataclass body unchanged).
- The new `TargetVariable` dataclass (below) is added directly to
  `plan.py`, not `sweep_builder.py` — it's the same category of object as
  `SweepAxis`, and putting one typed-vocabulary dataclass in `plan.py`
  while its matched pair stays in `sweep_builder.py` would just relocate
  the inconsistency instead of resolving it.
- `sweep_builder.py` keeps everything that operates ON a `SweepAxis` —
  `sweep_axis_param_specs()`, `build_axis_sweep()`, `SweepSegment`,
  `build_piecewise_sweep()`, `load_custom_sweep_csv()`,
  `apply_hysteresis()` — and its import becomes `from cryosoft.core.plan
  import ParamSpec, SweepAxis`: the same dependency edge it already has on
  `plan.py`, just carrying one more name, not a new edge.
  `plan.py` itself continues to import nothing from `sweep_builder.py` or
  anywhere else in `cryosoft` — dependency flows strictly one way, and
  `lint-imports` (`make check`) is the mechanical check that this stays
  true.
- Every existing `from cryosoft.core.sweep_builder import SweepAxis`
  elsewhere (`procedure.py`, `field_sweep.py`, `temperature_sweep.py`,
  `sweep_axis_widget.py`, …) becomes `from cryosoft.core.plan import
  SweepAxis` — a mechanical rename, called out explicitly here so it
  isn't missed during implementation.

```python
@dataclass(frozen=True)
class TargetVariable:
    """A physical, VI-mapped quantity produced by a forward() transform.

    Attributes:
        key: The dict key forward() uses for this quantity (and inverse()
            expects it under). Deliberately NOT the VI name — which VI
            realizes a target variable stays a procedure/Station concern,
            resolved in the axis hooks exactly as today (see the
            hardcoded-VI-names known issue), not baked into this
            dataclass. Keeping the two separate is what lets Part 2 stay
            agnostic to vector-magnet-vs-rotator composition.
        unit: Physical unit, e.g. "T", "K", "deg".
        data_key: The data-column name for this quantity's readback.
        description: Human-readable label.
    """
    key: str
    unit: str
    data_key: str
    description: str
```

New `BaseProcedure` class attributes, alongside (not replacing)
`sweep_axis`:

```python
sweep_variables: tuple[SweepAxis, ...] = ()
target_variables: tuple[TargetVariable, ...] = ()
```

A procedure picks **one** mechanism: `sweep_axis` (singular) for the
trivial one-variable/identity-transform case exactly as today, or
`sweep_variables`/`target_variables` for anything with more than one
variable or an actual `forward()`. Declaring both is a class-definition
mistake, not a supported combination — the same new conformance test
mentioned above (auto-discovering every procedure, per the "Conformance
tests" standard) asserts `sweep_axis is None` XOR `sweep_variables` is
non-empty for every concrete procedure, so this cannot silently ship.
`sweep_variables` declares the union of
every shape a procedure's coupling modes can use — e.g.
`TemperatureSweep` declares three (`temperature`, `temperature_vti`,
`temperature_sample`), all always present in `cls.parameters`, and a
`coupling_mode` structural param decides at render/construction time which
subset is actually active. This deliberately keeps `cls.parameters`
static and built once, exactly as `__init_subclass__` does today — no
change needed to make parameter existence itself selection-dependent, only
which of them render and get used.

Changes in `cryosoft/core/procedure.py`:

- `__init_subclass__` (`:153-161`): merge `sweep_axis_param_specs()` for
  `sweep_axis` **and** every entry of `sweep_variables` into
  `cls.parameters` (each variable's hidden params are already namespaced
  by its own `key`, so no collisions). Unconditional, regardless of
  coupling mode — unused hidden params for an inactive mode just sit there
  with their defaults, exactly like any other declared-but-currently-
  irrelevant `ParamSpec`.
- **No new classmethod.** Which `SweepAxis`(es) render is folded directly
  into `get_param_groups()` — already the ONE hook a procedure overrides
  to make its form's shape depend on a structural selection (the
  Measurement column's VI-parameter sub-form is the existing proof this
  works). Adding a second, parallel dynamic-shape hook (`get_sweep_variables()`)
  just for the Sweep column would mean two hooks governing two different
  parts of the same form through two different code paths — exactly the
  "one-off condition next to a standard" the framework is trying to avoid.
  Instead, `ParamGroup` (`cryosoft/core/plan.py:449-495`) gains a new
  field:
  ```python
  axes: tuple[SweepAxis, ...] = ()
  ```
  validated the same way `params` already is (`__post_init__` checks every
  entry is a `SweepAxis` — a plain `isinstance` check, with no import
  cycle, now that `SweepAxis` lives in `plan.py` too, per "Where
  `SweepAxis` and `TargetVariable` live" above — and that no `axis.key`
  collides directly with a `params` key in the same group). That
  same-group collision check is shallow by design and not the safeguard
  against the deeper hazard — a sweep variable's own namespaced hidden
  params (`temperature_vti_start`, …) colliding with an unrelated plain
  `ParamSpec` — which remains `__init_subclass__`'s existing dict-merge
  responsibility (`:153-161`), unchanged by this addition. A `ParamGroup`
  can now carry axes, params, or both — the Sweep
  group for `TemperatureSweep`'s Offset-coupled mode is one axis
  (`temperature`) plus one param (`vti_offset_K`); Independent mode is two
  axes and zero params; `FieldSweep`'s Sweep group is one axis
  (`magnet_z`) and zero params, same as today.
  `BaseProcedure.get_param_groups()`'s default (`:163-200`) builds the
  Sweep candidate's `axes` from the existing `sweep_axis`/`sweep_variables`
  attributes — `(cls.sweep_axis,) if cls.sweep_axis is not None else
  cls.sweep_variables` — and the empty-group skip (`if params` at `:199`)
  becomes `if params or axes`, so a procedure with neither (there are
  none today, but the rule stays correct if one is ever added) renders no
  Sweep column at all, exactly like any other empty group.
  A procedure with a `coupling_mode` structural param overrides
  `get_param_groups()` directly (calling `super().get_param_groups(station,
  selections)` and replacing the returned Sweep group's `axes`/`params`
  based on `selections.get("coupling_mode")`) — this is the one place any
  new logic is written, and it is the *existing* override contract, not a
  new one.
- `_build_sweep_array()` (`:306-318`, existing per-subclass override
  point): a `sweep_variables`-based procedure overrides this itself,
  branching on its coupling-mode param, calling the existing
  `build_axis_sweep()` once per active variable, validating equal lengths
  across whichever variables are active (raising `CryoSoftConfigError`
  naming both variables and their step counts on mismatch), and storing
  the per-variable arrays on an instance attribute it defines itself
  (`self._sweep_values: dict[str, list[float]]`, keyed by `SweepAxis.key`)
  — `self._sweep` (the base class's list, used for `len()`/step-count and
  `change_sweep_step()`'s bound) is set to whichever array's length drives
  the step count (by construction, all active arrays are the same length,
  so any one of them works). No base-class change is needed here: this is
  exactly the override contract `_build_sweep_array()` already documents
  ("A subclass without `sweep_axis` must override this").
- `SweepMeasureProcedure` (`:1317-1339`): `_auxiliary_readbacks(self) ->
  dict[str, float]` — any extra data_key -> value pairs a procedure wants
  recorded alongside `_axis_readback()`'s single column. Covers both a
  sibling sweep variable's readback (`TemperatureSweep`'s sample
  temperature) and a purely computed value with no declared variable at
  all (Part 2's achieved angle). One hook, two uses. **Contract: it must
  return a value for every key in `type(self).sweep_data_keys`, every
  call, with no exceptions** — `float("nan")` only when the underlying VI
  is genuinely **absent** from the station, never merely because a
  commanding toggle is off. This keeps the schema (below) fully static —
  every procedure declares a fixed set of columns once, matching today's
  behavior, and no `measure()` call can ever raise `DataSchemaError`
  because a toggle was off. Absent-vs-untoggled matters: reading a
  *present* VI's state is a monitor read, not a claim-violating command,
  so it happens unconditionally — exactly how `_axis_readback()` already
  reads the mandatory VTI channel regardless of `set_vti_temperature`
  (the bug this proposal fixes). A key resolves to `NaN` only when there
  is truly nothing to read; a present-but-uncommanded channel still
  reports its real (possibly passively drifting) value, which is data an
  operator running "sweep one channel, watch the other passively" wants
  recorded, not discarded. Building `sweep_data_keys` from "whichever
  keys happen to be active this run" was considered and rejected: it
  would make the schema itself conditional per-run, the exact kind of
  one-off state this proposal is trying to standardize away from.
- `measure()` (`:1646`): one new line after the existing axis write —
  `measured_data.update(self._auxiliary_readbacks())`.
- `_build_data_schema()` (`:1444`): after the existing
  `sweep_columns[axis_data_key()] = "float"`, loop
  `for key in type(self).sweep_data_keys: sweep_columns.setdefault(key,
  "float")`. `sweep_data_keys` (`:135`) already exists and every sweep
  procedure already populates it with its own axis's `data_key`; this
  makes it the one declared list that feeds both the schema AND the GUI
  plot-axis selector (`procedure_window.py:539`), instead of adding a
  second, narrower registration path. A procedure lists every extra
  column `_auxiliary_readbacks()` can return (sibling sweep variable, or
  Part 2's computed derived value) here — otherwise `_save_datapoint()`
  rejects it as an undeclared column. `setdefault` so a name already added
  via `axis_data_key()` or `station.last_state_flat()` is never silently
  overwritten. Because `_auxiliary_readbacks()`'s contract (above) always
  covers every declared key, this loop's set never depends on which mode
  or toggle is active — one static schema per procedure class, matching
  today's behavior.
- `target_variables` (declarative in this proposal): nothing computes from
  it directly — schema columns still come from `sweep_data_keys` and
  targets from each hook's own `_forward()` call. Its role here is
  documentation plus a **static** conformance check — `_forward()` has no
  base-class interface, branches at runtime per coupling mode, and isn't
  defined at all on procedures that don't use `sweep_variables`, so a
  check that has to execute `_forward()` across every mode/index to
  enumerate its possible return keys is not the declarative, auto-
  discoverable shape the rest of `test_conformance.py` uses. Instead, the
  new case asserts what's actually checkable without instantiating
  anything: every `TargetVariable.data_key` is a member of the
  procedure's own `sweep_data_keys`, and every `TargetVariable.key` within
  a procedure is unique. This catches the class of typo/mismatch bug that
  matters (a target's declared data column disagreeing with what the
  schema actually declares) without pretending to trace `_forward()`'s
  runtime behavior. Wiring `target_variables` into schema/validation more
  deeply is deferred — Part 2 is the first place a `target_variables`
  declaration would need to drive actual behavior beyond documentation,
  and this keeps that scope out of Part 1.
- Everything else (`change_sweep_step()`, `get_sweep_array()`,
  `get_progress()`, `axis_data_key()`, `_axis_readback()`) is untouched —
  a `sweep_variables`-based procedure still overrides `axis_data_key()`
  to name one representative column (exactly the escape hatch
  `TimeSeries` already uses today), typically the mandatory/always-present
  variable, and reserves `_auxiliary_readbacks()` for the rest.

### GUI

One standardization, not two special-cased changes: **fold the Sweep
column into the same generic per-group pipeline every other column
already uses**, instead of patching its current special-cased treatment.
Today `_build_param_form()` builds the Sweep box in a separate block
*before* the `for group in groups:` loop (`:234-254`, ahead of `:259`),
and `_rerender_groups()`'s diff explicitly excludes it
(`_is_column_key()`, `:506-511`, `key != "sweep"`) — two places where
Sweep is the one exception to how every other column is built, diffed,
and rebuilt. With `ParamGroup.axes` (above), that exception has no reason
to exist:

- `_build_param_form()`'s hardcoded pre-loop Sweep block (`:234-254`) is
  **deleted**. Every group — System, Sweep, Measurement, Reading loop —
  is built by the same `for group in groups:` loop, in exactly the order
  `get_param_groups()` returns (`get_param_groups()`'s default
  `candidates` tuple at `:191-195` is reordered to `(("system", ...),
  ("sweep", ...), ("measurement", ...))`, which is the entire "System
  first" change — no special-casing needed in the GUI at all, since
  nothing pins Sweep's position anymore). A group whose key is `"sweep"`
  (or, generically, any group carrying `axes`) is built by a new shared
  box-builder — `_build_axis_group_box(group)`, parallel to the existing
  `_build_reading_loop_box(group)` — that renders one `SweepAxisWidget`
  per entry in `group.axes` (tracked in `self._axis_widgets: dict[str,
  SweepAxisWidget]`, replacing today's singular `self._axis_widget`),
  then `param_form.build_form_layout(group.params)` beneath them for any
  plain fields (e.g. `vti_offset_K`) — same box, same width cap
  (`_SWEEP_COLUMN_MAX_WIDTH`) as today, keyed off `group.key == "sweep"`
  exactly like the Reading-loop column's own width cap is keyed off its
  key. The box is added to `self._group_boxes["sweep"]` like every other
  column — no separate tracking variable.
- `_is_column_key()`'s `key != "sweep"` exclusion (`:506-511`) is
  **removed**. Sweep now participates in the standard diff: staleness
  extends from "params set changed" to "params set OR axes tuple
  changed" (`set(old_group.params) != set(new_group.params) or
  old_group.axes != new_group.axes`), so selecting Offset-coupled vs.
  Independent in `TemperatureSweep`'s `coupling_mode` swaps the axis
  widgets through the exact same stale-box-removed/new-box-appended path
  that already handles a Reading-loop slot change or a Measurement-VI
  switch — not a new re-render mechanism invented for Sweep specifically.
  Both halves of that path need the same new dispatch: the *removal* half
  needs no change (a stale box is just dropped, regardless of what it
  contained), but the *append* half (`_rerender_groups()`'s "newly-
  appearing independent column" loop, currently `reading_loop ->
  _build_reading_loop_box`, everything else ->
  `param_form.build_group_box`) needs an explicit `axes`-aware branch —
  `key == "sweep"` (or generically, `group.axes` non-empty) ->
  `_build_axis_group_box(group)` — mirroring the same dispatch added to
  `_build_param_form()`'s first-render loop. Without it, a rebuilt Sweep
  box after a coupling-mode switch would fall through to
  `build_group_box` (params-only) and render `vti_offset_K` with no axis
  widgets. Both loops call the same `_build_axis_group_box`, so there is
  still exactly one function that knows how to render an axes-carrying
  group — but it must be wired into both call sites, not assumed to fall
  out of "the same path" automatically.
- The empty-group skip generalizes from "if params" to "if params or
  axes" (see "Framework" above), so a procedure with neither renders no
  Sweep column, using the same rule every other empty group already uses.
  `FieldSweep`'s Sweep group (one axis, zero params) is non-empty and
  renders unchanged. `TimeSeries` is **not** an example of the empty case
  — it declares four `sweep_parameters` (`step_time_s`, `max_duration_s`,
  …), so its Sweep group has non-empty `params` and empty `axes`; it
  renders a Sweep column with those timing fields and **no axis widget**,
  both today and after this change — this proposal does not add or
  remove anything from `TimeSeries`'s rendered form. No procedure in the
  shipped set actually has an all-empty Sweep group (zero `params` AND
  zero `axes`), so the "renders nothing" branch of the rule has no
  shipped exemplar; coverage for it needs a throwaway test procedure (see
  "Tests" below), not an existing one misdescribed as covering it.
- `collect_values()` (`:590-632`) iterates `self._axis_widgets` (plural)
  instead of the singular widget. `cache_current_params()`/
  `_apply_cached_params()` keep deliberately **not** touching axis-widget
  state at all, exactly as documented today (`:649-650`) — that
  design choice is unaffected by this change, not "generalized" to a
  plural form.
- `cryosoft/gui/queue_panel.py`'s `_queue_summary_parts` (`:262-287`)
  updates its single-widget read to iterate `self._axis_widgets`.

This is a real restructuring of `_build_param_form()`/`_rerender_groups()`,
not a reorder plus a new parallel add/remove path — the Sweep column
becomes one more instance of the generic group machinery, so a future
group type that also needs `axes` (there is none today) gets this
behavior for free too. Applies to every procedure immediately (`FieldSweep`,
`TimeSeries`, `TemperatureSweep`, and anything built later), since
`get_param_groups()`'s default candidate order is shared framework code,
not per-procedure — this is a GUI change with real blast radius, so it
goes through the gui-edit skill's mandatory offscreen-screenshot
verification (see "Verification" below) across every procedure shape in
the shipped set: plain `sweep_axis` with axes and no params (`FieldSweep`),
plain `sweep_axis = None` with params and no axes (`TimeSeries`), and
`TemperatureSweep`'s new `sweep_variables`.

### Concrete application: `TemperatureSweep`

`cryosoft/procedures/temperature_sweep.py`:

```python
sweep_axis = None  # replaced by sweep_variables below
default_x_key = "temperature_K"  # was sweep_axis.data_key; now a literal,
                                  # same convention TimeSeries already uses
                                  # for its own sweep_axis = None
sweep_variables = (
    SweepAxis(
        key="temperature", unit="K", data_key="sample_temperature_K",
        description="Sample-stage temperature (offset-coupled mode)",
        default_start=10.0, default_end=300.0, default_steps=30,
    ),
    SweepAxis(
        key="temperature_vti", unit="K", data_key="temperature_K",
        description="VTI temperature (independent mode)",
        default_start=10.0, default_end=300.0, default_steps=30,
    ),
    SweepAxis(
        key="temperature_sample", unit="K", data_key="sample_temperature_K",
        description="Sample-stage temperature (independent mode)",
        default_start=10.0, default_end=300.0, default_steps=30,
    ),
)
target_variables = (
    TargetVariable(
        key="temperature_vti", unit="K", data_key="temperature_K",
        description="VTI temperature",
    ),
    TargetVariable(
        key="temperature_sample", unit="K", data_key="sample_temperature_K",
        description="Sample-stage temperature",
    ),
)
sweep_data_keys = ["temperature_K", "sample_temperature_K"]
```

`temperature` and `temperature_sample` deliberately share
`data_key="sample_temperature_K"` — they are the same physical column
under two different coupling modes, and the two are never active
simultaneously (`get_param_groups()`'s override below returns exactly one
or the other, never both), so there is no collision at write time.

`sweep_parameters` (new — was empty before, renders in the Sweep column,
holds only the actual transform parameter, not the mode choice):

```python
sweep_parameters = {
    "vti_offset_K": ParamSpec(
        type=float, default=2.0, unit="K",
        description=(
            "Offset-coupled mode only: VTI setpoint = sample setpoint - this "
            "offset. Positive keeps VTI cooler than the sample for headroom."
        ),
    ),
}
```

`system_parameters` gains `coupling_mode` (structural, same choices as
before) alongside the existing `set_vti_temperature`/
`set_sample_temperature` toggles — all three are "which VIs get commanded"
decisions, not transform parameters, so all three now live together in
the System column, rendered first:

```python
system_parameters = {
    ...  # existing entries unchanged
    "coupling_mode": ParamSpec(
        type=str, default="independent", structural=True,
        choices={
            "Independent (separate VTI / sample ranges)": "independent",
            "Offset-coupled (one range + VTI offset)": "coupled",
        },
        description="How VTI and sample-stage temperature are related during the sweep",
    ),
}
```
`sample_temperature` (the old held-constant setpoint) is dropped entirely
— any cached form value under that key from a prior session is simply
never looked up again (`_apply_cached_params()` only restores keys the
currently-rendered groups declare), so no autosave-state migration is
needed.

`get_param_groups(cls, station, selections)` override — the one place
mode-dependent logic is written, using the *existing* override contract
(no new hook):

```python
@classmethod
def get_param_groups(cls, station, selections=None):
    groups = super().get_param_groups(station, selections)
    coupled = (selections or {}).get("coupling_mode") == "coupled"
    by_key = {axis.key: axis for axis in cls.sweep_variables}
    if coupled:
        axes = (by_key["temperature"],)
        params = dict(cls.sweep_parameters)
    else:
        axes = (by_key["temperature_vti"], by_key["temperature_sample"])
        params = {}
    return [
        ParamGroup(key="sweep", title="Sweep", params=params, axes=axes)
        if g.key == "sweep" else g
        for g in groups
    ]
```
Selecting by `axis.key` rather than by tuple position (`sweep_variables[0]`,
`sweep_variables[1:]`) deliberately decouples the mode mapping from
`sweep_variables`' declaration order — reordering the three declared axes,
or adding a fourth later, cannot silently change which axes a mode
selects. This is also where `vti_offset_K`'s visibility is decided —
included only in `coupled` mode's `params`, using the same cross-group
selection lookup `get_param_groups()` already performs for the
Measurement column, not a new conditional-rendering mechanism.

`__init__`: keep the existing magnet-presence loop unchanged.
`temperature_vti` presence checked **unconditionally** (the bug fix —
`_axis_readback()` always needs it regardless of `set_vti_temperature`).
`temperature_sample` presence required if `set_sample_temperature` is on
**or** `coupling_mode == "coupled"` (coupling to an absent channel raises
`CryoSoftConfigError` at construction, naming the mode as the reason).
`coupling_mode == "coupled"` additionally requires `set_vti_temperature`
be on: coupled mode's entire point is commanding VTI as a function of
sample (`VTI = sample - offset`), so selecting it while leaving VTI
uncommanded silently defeats the mode's physical invariant (VTI stays
below sample) without any error — `CryoSoftConfigError` at construction,
naming the mode as the reason, symmetric with the sample-presence check
above.

`_build_sweep_array()` override: branches on `coupling_mode`, builds one
or two arrays via `build_axis_sweep()`, validates equal length across
whichever are active, stores them in `self._sweep_values`, returns the
length-driving array.

`_forward(index)` — the impure adapter called from the axis hooks; it
resolves the active sweep values and `vti_offset_K` from procedure state,
then does the (trivial, inline) math itself, per the reconciled
`forward()`/`inverse()` convention in Vocabulary above:
```python
def _forward(self, index: int) -> dict[str, float]:
    if self._params["coupling_mode"] == "coupled":
        sample = self._sweep_values["temperature"][index]
        return {
            "temperature_sample": sample,
            "temperature_vti": sample - self._params["vti_offset_K"],
        }
    return {
        "temperature_vti": self._sweep_values["temperature_vti"][index],
        "temperature_sample": self._sweep_values["temperature_sample"][index],
    }
```
`_initial_system_targets()` (index 0, plus magnets) and `_step_targets(index)`
both call `self._forward(index)` and wrap each present, toggled-on entry
into a `Target`, keyed by its real VI name — sample is now genuinely
**re-sent every step** in both modes, which is the behavioral fix. Both use
the shared `ramp_rate_K_per_min`.

`axis_data_key()` override returns `"temperature_K"` (VTI — the one
channel guaranteed present in every mode/config, matching today's
behavior and column name, so existing analysis scripts keyed on
`temperature_K` keep working). `_axis_readback()` is unchanged (reads
VTI). `_auxiliary_readbacks()` always returns a `sample_temperature_K`
key, per the `_auxiliary_readbacks()` contract above:
`station.temperature_sample.temperature()` whenever the station **has**
a `temperature_sample` VI (regardless of `set_sample_temperature`/
`coupling_mode` — a monitor read, not a command, so it is never gated on
a toggle, matching how VTI's own mandatory readback already works
unconditionally), `float("nan")` only when the station genuinely has no
such VI — never an omitted key, so the schema stays static and no
`measure()` call can raise `DataSchemaError` regardless of which
toggle/mode combination is active.

**Claim narrowing** (new): a `sample_active` helper property
(`set_sample_temperature` or `coupling_mode == "coupled"` — the same
condition already used by `_auxiliary_readbacks()` above, not
re-derived) backs a `claimed_vi_names()` override. `self._params` is
frozen at construction time (`collect_values()` runs once, before the run
starts) and the Orchestrator reads `claimed_vi_names()` exactly once at
`_start_run()` (`orchestrator.py:599-600`) — so a `coupling_mode` change
made in the GUI *before* a run is started is always reflected, and there
is no run-time window where a stale claim set from a since-changed mode
could persist:

```python
def claimed_vi_names(self) -> set[str]:
    claimed = {self._measurement_vi} | set(self._magnet_targets)
    if self._params["set_vti_temperature"]:
        claimed.add("temperature_vti")
    if self._sample_active:
        claimed.add("temperature_sample")
    return claimed
```

With both temperature toggles off, this claims only the measurement VI and
whatever magnets are present — the operator can drive VTI and sample by
hand for the whole run, exactly as `HeliumFillOperation` leaves the VTI
free during a fill.

### `FieldSweep`: claim narrowing (its only change in this proposal)

No change to `FieldSweep`'s sweep-axis declaration, targets, or behavior —
it still sweeps `magnet_z` alone, with `temperature`/`sample_temperature`
as held-constant System params exactly as today. The one addition is the
same `claimed_vi_names()` narrowing as `TemperatureSweep`, since the
identical gap exists here independently of the sweep-variable rework:

```python
def claimed_vi_names(self) -> set[str]:
    claimed = {"magnet_z", self._measurement_vi}
    if self._params["set_vti_temperature"]:
        claimed.add("temperature_vti")
    if self._params["set_sample_temperature"]:
        claimed.add("temperature_sample")
    return claimed
```

`magnet_z` is unconditional — it is `FieldSweep`'s mandatory swept axis,
always targeted, never optional (unlike `TemperatureSweep`'s held-constant
magnets, which are only claimed when the station has them at all).

### Docs to update in the same change

- `GLOSSARY.md`'s "Sweep axis" entry — add "Sweep variable", "Target
  variable", "Sweep setup parameter", "Coupling mode", and
  `forward()`/`inverse()` alongside the existing `sweep_axis` description,
  per the Vocabulary section above.
- `cryosoft/procedures/README.md` — the "How to add a new module" recipe
  and the "Temperature channels (on/off)" section, now superseded for
  `TemperatureSweep` by the coupling-mode description above.
- `cryosoft/gui/README.md`'s `sweep_axis_widget.py` row and
  `procedure_params_panel.py` row (Sweep column folded into the generic
  group pipeline, reordered columns).
- `cryosoft/core/README.md`'s `plan.py` row (`ParamGroup.axes`,
  `SweepAxis` and `TargetVariable` relocated here — see "Where `SweepAxis`
  and `TargetVariable` live" above), `sweep_builder.py` row (drop the
  `SweepAxis` mention, note its functions now import it from `plan.py`),
  and `procedure.py` row (`sweep_variables`, `target_variables`, the
  `get_param_groups()` override contract for coupling modes).

### Tests

- `tests/test_plan.py`: `SweepAxis` and `TargetVariable` construction/
  validation, relocated here from wherever they were previously tested
  (`SweepAxis`'s existing tests move with it). `ParamGroup.axes`
  construction/validation (accepts a tuple of `SweepAxis`; rejects a
  non-`SweepAxis` entry; rejects an `axis.key` colliding directly with a
  `params` key); a group with both `params` and `axes` non-empty.
- `tests/test_sweep_builder.py`: no `SweepAxis` construction tests remain
  here (moved to `test_plan.py`); confirm `sweep_axis_param_specs()` and
  `build_axis_sweep()` still work unchanged against a `SweepAxis` imported
  from `plan.py`, proving the relocation is behavior-preserving for the
  functions that consume it.
- `tests/test_l4_procedure.py`: a throwaway `BaseProcedure` subclass with
  `sweep_variables` (no coupling mode) — cover `__init_subclass__`'s
  merged params and the default `get_param_groups()` producing a Sweep
  group whose `axes` is the full `sweep_variables` tuple. A second subclass
  with a `coupling_mode`-style structural param overriding
  `get_param_groups()` — cover that the override's returned Sweep group
  `axes`/`params` actually change with `selections`. A third throwaway
  subclass declaring neither `sweep_axis`, `sweep_variables`, nor
  `sweep_parameters` — the genuinely-empty-group case with no exemplar in
  the shipped procedure set — confirms `get_param_groups()` omits a Sweep
  group entirely (`if params or axes` both false).
- `tests/test_conformance.py`: auto-discovered over every concrete
  procedure — `sweep_axis is None` XOR `sweep_variables` non-empty
  (static); every `TargetVariable.data_key` is a member of the
  procedure's own `sweep_data_keys`, and `TargetVariable.key` is unique
  within a procedure (also static — see "Framework" above for why this
  check does not attempt to introspect `_forward()`'s runtime behavior).
- `tests/test_new_procedures.py`:
  - Replace `test_temp_sweep_holds_sample_setpoint_only_on_initiate`: sample
    is now re-sent every step in both modes; assert `change_sweep_step()`'s
    target reflects the sample variable's own value at that index.
  - New: mismatched step counts between `temperature_vti`/`temperature_sample`
    in Independent mode -> `CryoSoftConfigError` at construction.
  - New: `coupling_mode="coupled"` with `temperature_sample` absent ->
    `CryoSoftConfigError` naming the mode as the reason.
  - New: `coupling_mode="coupled"` with `set_vti_temperature` off ->
    `CryoSoftConfigError` naming the mode as the reason (coupled mode
    requires VTI actually be commanded).
  - New: station missing `temperature_vti` -> `CryoSoftConfigError`
    regardless of `set_vti_temperature`'s value (the bug fix).
  - Confirm Independent mode with `set_sample_temperature` off and no
    `temperature_sample` on station still constructs fine.
  - New: Independent mode, different start/end per variable (VTI 10->100,
    sample 12->102, 3 steps) — assert index-for-index Target values at
    steps 0/1/2 for both VIs, proving the lockstep sync.
  - New: Coupled mode, `temperature` 100->300 3 steps, `vti_offset_K=2` —
    assert VTI targets are exactly 2 K below sample targets at every step,
    and `_auxiliary_readbacks()["sample_temperature_K"]` matches the
    sample sweep value.
  - New: Independent mode, `temperature_sample` present on station but
    `set_sample_temperature` off — assert
    `_auxiliary_readbacks()["sample_temperature_K"]` is the VI's real
    (present, not omitted) reading, not `NaN` — an untoggled-but-present
    channel is still monitored.
  - New: Independent mode, `temperature_sample` genuinely absent from the
    station and `set_sample_temperature` off — assert
    `_auxiliary_readbacks()["sample_temperature_K"]` is `NaN` (present
    key, not omitted), and a full `measure()` call does not raise
    `DataSchemaError`.
  - New (`TemperatureSweep`): both temperature toggles off ->
    `claimed_vi_names()` contains only the measurement VI and present
    magnets, NOT `temperature_vti`/`temperature_sample`; each toggle on
    individually adds exactly that VI to the claim set; Coupled mode adds
    `temperature_sample` to the claim set even with `set_sample_temperature`
    left at its default.
  - New (`FieldSweep`): both temperature toggles off ->
    `claimed_vi_names()` is `{"magnet_z", <measurement_vi>}` only; each
    toggle on individually adds exactly that VI.
- `tests/test_gui.py`: update the `_axis_widget is not None` assertions
  for the new `_axis_widgets` (plural) structure; add a check that
  selecting "Offset-coupled" in `TemperatureSweep`'s `coupling_mode`
  dropdown (in the System column) swaps the two independent-mode
  `SweepAxisWidget`s for the one coupled-mode widget plus the offset
  field, and vice versa — this specifically exercises the append loop's
  new `_build_axis_group_box` dispatch (see "GUI" above), so assert the
  rebuilt widgets are actually present after the swap, not just that the
  old ones are gone; a general check that System renders left of Sweep
  for at least one plain-`sweep_axis` procedure (`FieldSweep`) and one
  `sweep_variables` procedure (`TemperatureSweep`); a regression check
  that `TimeSeries` still renders its Sweep column (`step_time_s` etc.)
  with zero axis widgets, unchanged by this proposal; a check on the new
  fully-empty throwaway procedure (`test_l4_procedure.py`, above) that no
  Sweep box renders at all — the one case no shipped procedure covers.

### Verification

1. `& ".venv\Scripts\python.exe" -m pytest tests/ -q` and `-m "not hardware"`.
2. `ruff check .` and `lint-imports` (the `make check` targets).
3. Offscreen GUI screenshot smoke (gui-edit skill's mandatory step):
   - `FieldSweep` in a `ProcedureWindow` built against `sim_cryostat`:
     confirm System now renders left of Sweep.
   - `TimeSeries`: confirm the Sweep column still renders its timing
     fields (`step_time_s`, `max_duration_s`, …) with no axis widget,
     unchanged from today.
   - `TemperatureSweep`: confirm both coupling-mode layouts render
     correctly (Independent shows two axis widgets, Coupled shows one axis
     widget + the offset field, `coupling_mode` itself appears in the
     System column), and that switching the `coupling_mode` dropdown
     live-swaps them without breaking any other column.
4. Manually construct a `TemperatureSweep` against `sim_cryostat` in each
   mode with a construction-time error case (mismatched steps in
   Independent; coupled mode with sample absent; coupled mode with
   `set_vti_temperature` off) to confirm the `CryoSoftConfigError`
   messages are clear and actionable.

---

# Angle / vector composition (derived target variables)

**Status: proposal, not started. Framework-only — no concrete procedure in
this section.** Depends on nothing in Part 1 being implemented first (it
reuses the *vocabulary* — sweep variable, target variable, `forward()`/
`inverse()`, coupling mode — not any code from Part 1), but is the natural
next application of the same pattern once both exist, sized against a
harder transform (spherical trigonometry vs. Part 1's linear offset) and a
harder hardware-composition question (which VIs realize an angle at all).

## Problem

Every procedure today sweeps a quantity that maps 1:1 onto one physical
VI's setpoint: `FieldSweep` walks `magnet_z` in tesla. Nothing walks a
quantity that has to be *computed into* one or more physical setpoints
before it can be commanded — concretely, a field **angle**.

The moment the project has a vector-capable magnet (today's only
two-magnet config, `sim_cryostat`'s `magnet_z` + `magnet_y`, or a real
third axis later) or a mechanical `RotatorVI` (already exists, sim-only,
unused by any procedure — `cryosoft/virtual_instruments/rotator/rotator.py`),
the natural measurements are:

- Sweep **theta** (polar angle from the primary field axis) at any fixed
  **phi**, holding the field **magnitude** constant.
- Sweep **phi** (azimuthal angle) at any fixed **theta**.
- Sweep the field **magnitude** along any fixed **(theta, phi)** — this one
  is almost `FieldSweep` as it stands, just at a commanded angle instead of
  along a single physical axis.

Two hardware compositions can realize the same logical sweep:

1. **Vector magnet**: the angle is a ratio of two or three magnet fields
   (`magnet_z`, `magnet_y`, and eventually `magnet_x`), commanded via
   Cartesian components computed from the requested spherical coordinate.
2. **Fixed-axis magnet + mechanical rotator**: the field stays on one
   physical magnet axis at the requested magnitude; the *sample* is rotated
   to the requested angle via `RotatorVI.set_sample_angle()`.

A procedure hardcoded to one composition cannot run on the other — this is
the vector-magnet analogue of Part 1's coupling-mode toggle, except the
choice here is a hardware-composition fact of the station, not a per-run
operator preference (see "Deferred" below for why it stays out of scope).
And every angle is measured relative to how the sample happens to be glued
into its mount, not the magnet's physical axes, so every angle sweep needs
an operator-settable calibration offset (`theta_offset_deg`,
`phi_offset_deg`) between the *logical* angle the operator dials in and the
*physical* angle actually commanded or read back — structurally the same
kind of setup parameter as Part 1's `vti_offset_K`, just feeding a
trigonometric `forward()` instead of a subtraction.

## What already exists (don't rebuild this)

- **Part 1's sweep-variable/target-variable/coupling-mode mechanism** —
  reusable the day a scan genuinely sweeps two angles together (a theta+phi
  raster: declare both as `sweep_variables`), but not needed for the common
  case of one swept angle at fixed others, which is just one sweep variable
  (`theta`) plus ordinary `sweep_parameters` (the fixed `phi`/`magnitude`
  and the offsets) — exactly `FieldSweep`'s existing pattern of one swept
  field vs. held-constant temperature, generalized.
- **`Target`/`PhasePlan`/`StepPlan`** (`cryosoft/core/plan.py`) already
  accept an arbitrary `dict[str, Target]` keyed by VI name.
  `FieldSweep._initial_system_targets()`
  (`cryosoft/procedures/field_sweep.py:121-134`) already builds a
  multi-entry targets dict by hand from one swept value plus toggled
  constants — a `forward()` emitting two or three `Target`s
  (`magnet_z`+`magnet_y`, or `magnet_z`+`rotator`) from one swept angle
  needs *no* plan- or data-layer change, confirmed by reading both the
  dataclass and this call site.
- **`RotatorVI`** (`cryosoft/virtual_instruments/rotator/rotator.py`) is a
  complete, working second physical realization of "angle": a plain
  `angle_deg` setpoint/readback pair (`set_sample_angle`/`get_sample_angle`,
  ramped via the standard `RampableVI` contract). Confirms `forward()` must
  be pluggable per station composition, not hardcoded to the two-magnet
  case — there are already two real shapes it has to support.
- **`cryosoft/procedures/README.md`'s "Known issues" section**
  (`:202-224`) already scopes the fix for hardcoded VI names as a future
  `magnet_vi_names()`/`temperature_vi_names()`-style discovery pair on
  `Station`. That is the natural mechanism by which a derived-angle
  procedure would one day discover *which* VIs realize an angle on a given
  station (two magnets? one magnet + a rotator?) — explicitly out of scope
  for this change too, same reasoning as Part 1.
- Only `sim_cryostat` has more than one magnet, and no shipped config
  attaches an angle-related `init_params` block to any magnet or rotator —
  there is no real vector-magnet or rotator-equipped setup to build a
  concrete procedure against yet. This is exactly why this section stays
  framework-only.

## Proposed change

Reuses Part 1's pattern directly, with a trigonometric `forward()`/
`inverse()` pair instead of a linear one:

- The swept quantity (theta, phi, or magnitude, whichever variant) is
  declared as a single-entry `sweep_variables` (or `sweep_axis`, they're
  interchangeable for one variable) — `unit="deg"` for an angle, `unit="T"`
  for magnitude.
- The spherical coordinates held fixed for that variant (`phi` and
  `magnitude` when sweeping `theta`) plus the calibration offsets go in
  `sweep_parameters`, exactly like Part 1's `vti_offset_K`.
- `target_variables` declares the physical outputs
  (`TargetVariable(key="magnet_z", ...)`, `TargetVariable(key="magnet_y", ...)`,
  or `TargetVariable(key="rotator", ...)` depending on station composition).

What's genuinely new is the shared math connecting these — a pure,
reusable coordinate transform, in the spirit of "standards over one-off
code": without it, every future angle procedure reinvents its own trig,
and readback/offset bugs get fixed once per procedure instead of once in a
tested module.

### New module: `cryosoft/core/vector_geometry.py`

Pure math, no VI/Station/hardware knowledge — imports nothing from
`plan.py`, `station.py`, or any VI package, so it stays usable from GUI
code too (e.g. a future polar sweep-trajectory plot) and never risks an
import-linter layer violation:

```python
def spherical_to_cartesian(
    magnitude: float, theta_deg: float, phi_deg: float = 0.0,
    theta_offset_deg: float = 0.0, phi_offset_deg: float = 0.0,
) -> tuple[float, float, float]:
    """Convert a logical (magnitude, theta, phi) to physical (x, y, z).

    theta is measured from the physical z-axis, phi from the physical
    x-axis in the x-y plane, both in degrees. The offsets are added to
    theta/phi *before* conversion — the forward() direction, logical
    (sample-relative) angle to physical (magnet-relative) angle.
    """

def cartesian_to_spherical(
    x: float, y: float, z: float,
    theta_offset_deg: float = 0.0, phi_offset_deg: float = 0.0,
) -> tuple[float, float, float]:
    """The inverse() direction: physical (x, y, z) readbacks to logical
    (magnitude, theta, phi), offsets subtracted after conversion.
    """
```

A two-magnet station (today's only real case) only ever needs
`magnitude * cos(theta)` / `magnitude * sin(theta)` with `phi` fixed at
`0` — the 2-D degenerate case of the same formula. A concrete two-magnet
`FieldAngleSweep` may call `spherical_to_cartesian` directly (simplest,
consistent with the eventual three-magnet case) or inline the 2-D trig;
`vector_geometry` is written for the general three-axis case so it is
ready the day a third magnet axis exists, not because the two-axis case
needs it.

### Readback: reuse `_auxiliary_readbacks()`

A theta-sweep run declares only `theta` as its sweep variable, but still
wants `achieved_phi_deg` and `achieved_magnitude_T` columns — the
*physical* values actually reached, computed via `cartesian_to_spherical()`
on the magnet (or magnet+rotator) readbacks — logged every point as a
cross-check against the fixed settings. This is `_auxiliary_readbacks()`
(Part 1) again, no new hook: its contract was already written generically
enough to cover a purely computed value with no declared sweep variable of
its own. The computed names must also be listed in `sweep_data_keys`,
exactly as Part 1's sample-temperature column is.

No other `procedure.py` or GUI change is needed beyond what Part 1 already
adds. The swept angle gets its mode-selector editor for free from the
existing `SweepAxisWidget`; the fixed magnitude/phi/offsets get plain
fields for free from ordinary `sweep_parameters` rendering.

### Why offsets are a run parameter, not a config constant

CLAUDE.md's "constants and limits in config, not in code" governs
setup-level hardware facts — instrument addresses, safety limits — that are
wrong to hardcode into Python because they describe the *installation*, not
the *experiment*. A sample's mounting offset is neither: it changes every
time a sample is remounted, by the operator, mid-campaign, not by whoever
edits the setup's YAML. It belongs alongside `TemperatureSweep`'s
`vti_offset_K` — an ordinary run parameter with a sane default, recorded
in that run's own metadata (so the dataset is self-describing about which
offset produced it), not a setup constant that would need a config edit
and app restart to change between samples.

## Deferred (explicitly out of scope for this framework change)

- **A concrete `FieldAngleSweep` (or `ThetaSweep`/`PhiSweep`) procedure.**
  This section adds only the shared `vector_geometry` module and documents
  the pattern; writing the actual procedure is a follow-on change, and
  wants a real vector-magnet or magnet+rotator config to test against —
  `sim_cryostat`'s `magnet_z`+`magnet_y` pair is the only candidate today
  and has no angle-related `init_params` behind it yet.
- **Vector-magnet vs. rotator interchangeability** — running the same
  `FieldAngleSweep` unmodified against either hardware composition.
  Structurally this is a coupling-mode choice exactly like Part 1's
  Independent/Offset-coupled toggle, EXCEPT it's a *station* fact (which
  hardware exists), not an *operator* preference each run — so it should
  be resolved by the deferred Station VI-name-discovery mechanism
  (`procedures/README.md`'s Known Issues) auto-selecting the right
  `forward()`, not by a `structural` param the operator picks by hand
  every run. Until that discovery mechanism lands, a concrete
  `FieldAngleSweep` hardcodes its VI names exactly like `FieldSweep` /
  `TemperatureSweep` do today — consistent with current practice, not a
  new gap this change introduces.
- **Magnet + rotator co-operation** (rotator supplies the angle while a
  single magnet supplies the magnitude) — a Station-composition question
  (which VI is asked for which coordinate), layered on top of the same
  `vector_geometry` transforms; no new math, just a different choice of
  which physical output each `(x, y, z)` component maps onto. Left for the
  same follow-on as above.
- **Default-offset sourcing** (should `theta_offset_deg` default from a
  per-sample or per-config value instead of a flat `0.0`?) — an open
  question for whoever implements `FieldAngleSweep`; nothing today blocks
  shipping with a flat default the operator dials in each run.
- **GUI polar/vector visualization** (e.g. a plot of the sweep trajectory
  on a theta/phi disk) — independent GUI enhancement, not a framework
  prerequisite.

## Tests (when implemented)

- `tests/test_vector_geometry.py`:
  `cartesian_to_spherical(*spherical_to_cartesian(m, theta, phi))`
  round-trips to `(m, theta, phi)` for representative angles, including
  degenerate cases (`theta=0`, `theta=180`, `phi` wrap at `+/-180`);
  applying an offset then un-applying it (offset into the forward
  conversion, negated offset into the inverse) cancels back to the
  original angle.
- Whichever `tests/test_*_procedures.py` covers the eventual
  `FieldAngleSweep`: index-for-index `magnet_z`/`magnet_y` targets at a
  few `(theta, phi=const, magnitude=const)` points against known trig
  values; readback recovers the same `theta` from mocked field readings; a
  nonzero `theta_offset_deg` changes the physical target commanded without
  changing the logical swept value recorded in the sweep array.

## Verification (when implemented)

1. `& ".venv\Scripts\python.exe" -m pytest tests/test_vector_geometry.py -q`.
2. `ruff check .` and `lint-imports` — confirm `vector_geometry.py` imports
   nothing from `station.py`, `plan.py`, or any VI package (keeps it usable
   from GUI code, and keeps import-linter's layer contracts clean).
