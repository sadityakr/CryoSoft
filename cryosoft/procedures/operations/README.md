# procedures/operations/

## Purpose

`procedures/operations/` holds the concrete cryostat-**servicing** actions —
one class per operation (helium fill, sample load, sample unload) — each a
subclass of `cryosoft.core.operation.OperationBase`. An operation is
declarative like a
procedure (it returns plans, it never touches a VI directly), but is a
different request type: operation-scope command access, tolerated safety
flags, verified postconditions, an optional (not required) data file, and
higher submission priority than a queued measurement procedure. See
`docs/plans/archive/cryogenics-logbook.md` §2/§4 for the full design and
`OperationBase`'s own docstring for the Orchestrator adapter contract.

## Architecture layer

L4 (Operations — same layer as Procedures, a parallel contract). Sits above
L3 (Orchestrator) and L2 (Station); an operation that wants an HDF5 dataset
may still use `DataManager` (L5) exactly like a procedure does, but a small
bounded in-memory series handed to the session layer via `run_summary()`
(docs/plans/archive/operation-concurrency-and-error-scoping.md §4 — e.g. the helium
fill's level curve) is preferred when HDF5's column layout is not needed.

```
GUI -> Orchestrator.run_operation()/queue_operation() -> Operation -> Station -> Virtual Instruments -> Drivers
                                                                \-> DataManager -> HDF5 (optional)
                                                                \-> run_summary() -> run manifest "summary" -> session layer
```

## Entry (how control/data enters this folder)

Every operation here is constructed with:

- `station`: the `Station` instance (the only path to any VI — contract C6,
  same as procedures).
- `person` (keyword, default `""`): who is performing the servicing action,
  recorded via `get_params()` so the servicing-log recorder can attribute it.
- `**config`: the operation's own config keys (e.g. the helium fill's
  `docs/plans/archive/cryogenics-logbook.md` §9 `cryogenics:` block keys, or
  the sample-access pair's `operations.sample_load:`/
  `operations.sample_unload:` block keys), each with a class-level default
  so the conformance suite's `test_operation_constructs_from_defaults` can
  build the class from a sim station alone.

## Exit (what it hands to other layers)

The Orchestrator drives the lifecycle via the SAME duck-typed surface a
procedure exposes (see `OperationBase`'s "Orchestrator adapter" docstring
section — `measure()`/`change_sweep_step()` are final adapters over
`sample()`/`step()`):

| Method | Returns | Called when |
|--------|---------|-------------|
| `initiate()` | `PhasePlan` | Operation starts |
| `step()` | `StepPlan` or `None` | Every tick after the first sample — a **Hold phase** operation (below) returns a `StepPlan` indefinitely instead of `None` |
| `sample()` | nothing (optional HDF5 write) | Once per step, before `step()` |
| `standby()` | `PhasePlan` | Operation ending — park hardware |
| `abort()` | `tuple[Command, ...]` | User abort / ERROR / EMERGENCY |
| `initiation_gates()` | `tuple[Gate, ...]` | Once, before the first `sample()` |
| `postcondition_gates()` | `tuple[Gate, ...]` | Evaluated once, immediately, as the run ends (right after `standby()`'s plan is dispatched) |
| `run_summary()` | `dict` (JSON-safe) | Once, by the Orchestrator, when it emits `run_finished` — merged into the run manifest's `summary` key |

An operation's plans may carry BOTH `"measurement"`- and `"operation"`-scope
`@control` commands (`Station.send_measurement_commands(..., allowed_scope=
"operation")`, dispatched automatically by the Orchestrator for a running
operation) — the capability a plain procedure's plan does not have.

## Interface contract

- Every operation subclasses `OperationBase` (from `cryosoft.core.operation`)
  and sets `name`; declares `tolerated_safety_flags` (a `frozenset[str]`,
  empty by default) naming the safety flags that must NOT abort *this*
  operation (e.g. the fill tolerates `"helium_low"` — fixing that condition
  is its whole purpose).
- Readiness / next-due / discovery (plan §12, `gui/operations_panel.py`'s
  `OperationsPanel`): override `readiness_conditions() -> tuple[
  ReadinessCondition, ...]` to declare the live checklist rows the panel
  renders for this operation (each a `key`/`label`/`check(state)`/optional
  `detail(state)` — pure reads against the Orchestrator's per-tick state
  snapshot, no hardware access); override `next_due(context) ->
  NextDue | None` if the operation has a predictable schedule (the helium
  fill does, from the measured consumption rate passed in via
  `context["consumption_rate_pct_per_h"]` — an operation must NOT import
  the session layer itself to compute this, contract C12); set
  `ready_message` (non-empty) to the string shown in the panel's ready
  banner once a run finishes `done` with every condition holding; set
  `config_key` (non-empty, unique across operations — checked by
  conformance) to the `operations:` config-block key this class should be
  built from generically. All four default to "nothing" (`()`, `None`,
  `""`, `""`) — an operation that skips this section still works, it just
  gets no card checklist/next-due/ready-banner beyond the button.
- Operations never import from `drivers/` or `virtual_instruments/`;
  instruments are reached only through `self._station` (contract C6, same
  rule as `cryosoft.procedures`).
- `claimed_vi_names() -> set[str] | None` (docs/plans/operation-concurrency-
  and-error-scoping.md §1's **Claim** — see GLOSSARY.md): declares which VIs
  a running operation exclusively owns. The Orchestrator captures it once at
  run start and refuses a manual front-panel action on a claimed VI (naming
  the owning operation); every VI NOT in the set stays manually controllable
  while the operation runs. Default `None` (claim everything) — narrowing is
  an explicit per-class opt-in. The rule: claim every VI whose state the
  operation commands or holds as an invariant. `HeliumFillOperation` claims
  its configured level meter AND every magnet (it drives them to 0 T and
  holds zero field for the whole fill), so the VTI (and everything else)
  stays under manual control during a fill. `SampleLoadOperation`/
  `SampleUnloadOperation` (both sharing `_SampleAccessOperationBase`) claim
  exactly the VIs their `initiate()` commands (magnets, VTI, measurement
  VIs) — narrower than "everything" only on a station with instruments they
  never touch (e.g. a rotator or a switch matrix).
- `postcondition_gates()` is the operation-specific addition over the
  procedure contract: gates verifying the cryostat actually reached the
  promised state (not just that the commands were sent). The Orchestrator
  evaluates each gate exactly ONCE (`Gate.check_once()`), immediately, as
  the run ends — no holding, no timeout (docs/plans/operation-concurrency-
  and-error-scoping.md §2, "immediate finish"). An unmet gate never blocks
  the run; it is named in the run manifest's `postconditions_unmet` list and
  logged at WARNING.
- An operation that wants a dataset creates its own `DataManager` in
  `initiate()` exactly as `BaseProcedure` does, and exposes `data_filepath`
  so the Orchestrator's run manifest captures the path; a data file is never
  required (`OperationBase` has no default `DataManager`). An operation with
  a small, bounded time series (e.g. the helium fill's level curve) should
  prefer `run_summary() -> dict` instead: no file at all, no
  `data_filepath` property needed — the Orchestrator merges the returned
  dict into the run manifest's `summary` key on `run_finished` (duck-typed,
  default `{}`, guarded so a broken override can never block the run). A
  bounded time series should be handed off under the generic `"recording"`
  key (docs/plans/archive/unified-servicing-log-and-run-recording.md §3):
  `{"recording": {"unix_time": [...], "channels": {"<vi>.<value>": [...],
  ...}}, ...}` — `cryosoft.session.servicing_log.CryogenicsRecorder` writes
  it as this run's `recordings/<run_id>.json` sidecar and stamps that
  filename into the run's single `servicing` log entry, whatever operation
  produced it. `HeliumFillOperation.run_summary()` is the reference
  implementation: `{"recording": {"unix_time": [...], "channels":
  {"<level_vi>.helium_pct": [...]}}, "start_pct": float, "end_pct": float}`.
- A recording is built with `OperationBase`'s shared, opt-in recorder helper
  (docs/plans/archive/unified-servicing-log-and-run-recording.md §3): call
  `self._record_sample(unix_time, {channel_name: value, ...})` once per
  sample from `sample()` (every channel must be the SAME set on every call
  within one run — the shared time axis and every channel decimate
  together) and return `{"recording": self._recording_dict()}` (or fold
  extra keys around it, as the fill does with `start_pct`/`end_pct`) from
  `run_summary()`. `_MAX_RECORDING_POINTS` (class attribute, default 4000)
  bounds memory via stride-doubling decimation (`series[::2]`, generalising
  the fill's original `_MAX_CURVE_POINTS`); call `self._reset_recording()`
  from `initiate()` so a fresh run starts with an empty series.
  `HeliumFillOperation` (one channel) and `_SampleAccessOperationBase` (VTI
  temperature + every magnet's field, shared by `SampleLoadOperation` and
  `SampleUnloadOperation`) both use it.

## Hold phase (plan §1)

`step()`'s normal contract is "return a `StepPlan` to keep sampling, or
`None` to end the run" — that "or `None`" part is a choice, not a
requirement. An operation whose physical action happens WHILE the run is
active rather than after a fixed condition (e.g. `SampleLoadOperation`/
`SampleUnloadOperation`: the operator opens the cryostat during the hold,
not after some elapsed time or level threshold) declares a **hold phase**:
`step()` never returns `None` on its own once its setup work (ramps, etc.)
is done — it keeps returning a fresh `StepPlan` (mirroring an open-ended
sampling loop like the fill's) — so the run stays active indefinitely. The
run ends when the operator clicks Finish (`Orchestrator.finish_operation()`
-> `request_finish()`): the very next `change_sweep_step()` (the
`OperationBase` adapter) then returns `None` regardless of what `step()`
would return, exactly the existing graceful-finish mechanism — no new
Orchestrator code needed. Abort (`Orchestrator.abort_procedure()`) ends it
just as well, skipping `postcondition_gates()` entirely, same as for any
other run.

- Set the class attribute `hold_for_operator = True` to declare this. The
  Operations panel reads it to decide WHEN the ready banner may show: a
  plain operation's banner shows only once the run finished `done` AND
  every readiness condition holds; a hold-phase operation's banner ALSO
  shows mid-run, the instant every condition holds, since for it "ready"
  answers "you may act now" — true well before Finish.
- Use `sample()` (throttled to a config key, e.g. `sample_period_s`, via
  `step()`'s `wait_s`) to record station state for the whole hold via the
  shared recorder helper above, so the run's servicing-log entry reflects
  the true conditions spanning the hold, not just the moment the ramps
  finished.
- Everything else — postconditions, operator confirmations, claims — works
  exactly as for any other operation; Finish evaluates
  `postcondition_gates()` once, immediately, same as always.

## How to add a new module

1. Create `procedures/operations/your_operation.py` with the PEP 257 header
   docstring (Workspace Rule 1).
2. Subclass `OperationBase`; set `name` and, if it applies,
   `tolerated_safety_flags`.
3. Implement `initiate()` / `step()` / `standby()` at minimum; add `sample()`
   / `abort()` / `initiation_gates()` / `postcondition_gates()` /
   `get_progress()` / `get_params()` as the operation needs (see
   `OperationBase`'s docstring for each hook's default).
3a. If the setup should see this operation in the GUI's Operations panel
   (`gui/operations_panel.py`): add `readiness_conditions()`/`next_due()`,
   `ready_message`, and (if config-driven generically rather than wired by
   hand like the helium fill) `config_key` — see "How a new operation
   declares readiness" above. Skipping this section is fine; the operation
   still runs, it just gets a bare button in the panel.
4. Give the constructor a working zero-argument-beyond-`station` default (a
   sim `Station` must be enough to build it) — the conformance suite
   constructs every discovered operation this way.
5. Write tests in `tests/test_<operation>.py`, driving a real `Orchestrator`
   tick loop against the `sim_cryostat` station (mirror
   `tests/test_operations.py`'s fixtures).
6. Add the file to the Files map below with its owning test file.

## Stepped operations (the step standard)

Some operations are not a single wait but a *named sequence* the operator
walks through. A sample change is the case that drove this: warm the VTI,
close the needle valve, open the sample access valve, move the rod, close
the valve, flush. The software performs almost none of it, but it has to
record exactly when each part happened, since that record is the main thing
the operation exists to produce.

An operation opts in by overriding `steps()` to return a non-empty ordered
tuple of `OperationStep` (`core/operation.py`). Everything else follows with
no per-operation GUI code, exactly like the readiness-condition standard:

- **`OperationStep`** declares `key` (stable, snake_case, unique — it is how
  the GUI addresses the step), `label`, `kind`, `skippable`, and two optional
  pure-read callables over the state snapshot: `detail(state)` for a live
  line under the label, `skip_warning(state)` for the text shown when the
  operator asks to skip.
- **Two kinds.** `auto_ramp` is carried out by the system (a ramp the
  operation dispatched) and completes on its own — `sample()` records it done
  the first tick the reading lands. `operator_ack` is a physical act the
  software can neither perform nor verify, and completes only when the
  operator confirms it.
- **`current_step()`** is the first step with no recorded outcome. That one
  rule is what makes the sequence sequential: the GUI offers a Confirm/Skip
  action for that step alone, and it advances the instant an outcome is
  recorded. No stage counter, no explicit advance call.
- **`confirm_step(key)` / `skip_step(key)`** stamp a `StepRecord` — status,
  unix time, and `step_conditions_snapshot()`, a flat cached-state snapshot.
  That snapshot is where the *non-numeric* monitored values live (a needle
  valve's AUTO/MANUAL mode, a magnet's state), which the numeric recording
  cannot hold, and which are exactly what you want to know at the moment a
  step was attested. Recording an already-recorded step is a no-op, so a
  double-click cannot rewrite when something happened.
- **`steps_summary()`** returns the whole timeline, pending steps included,
  for `run_summary()` and thence the servicing log.
- **Reset** with `_reset_steps()` in `initiate()`, beside
  `_reset_recording()`.

Both are reached from the GUI through the Orchestrator, never directly:
`Orchestrator.confirm_operation(key)` and
`Orchestrator.skip_operation_step(key)`, each duck-typed with the same
active-operation / `action_blocked` guard as `finish_operation()`.

### Skipping is always allowed

Every step of a sample change is skippable, including the warm-up. This is
deliberate. A sample sometimes has to be changed at base temperature, and an
operation that refuses to run then does not prevent the sample change — it
only means the sample change happens with no record of it at all. So a skip
always succeeds; the guard is the warning, not a refusal.

A skip is an **override, not a failure**. The GUI warns once, quoting the
live conditions, and on acceptance the step is recorded `skipped` and its
postcondition gate reports unmet — which puts it in the run manifest's
`postconditions_unmet` and, from there, the servicing-log `notes`
(`CryogenicsRecorder` folds it in, e.g. `"unmet: step_warm_vti"`). That is
the existing mechanism for an unverified postcondition, reused rather than
duplicated. The panel gives a skipped step its own amber icon state,
distinct from both met and unmet, because painting a deliberate recorded
decision red would read as a fault.

### Skipping an `auto_ramp` step needs the Orchestrator

An `auto_ramp` step is a ramp the operation dispatched, and while that ramp
runs the Orchestrator is in RAMPING, where the operation's `step()` is never
called. So the operation cannot retarget or stop its own ramp, and
`Orchestrator.stop_ramp()` deliberately refuses a VI claimed by an active
run. The operation therefore raises `skip_ramp_requested` and the
Orchestrator's RAMPING branch honours it on the tick, calling
`Station.stop_ramps()` — the same hold-in-place `pause_procedure()` uses —
which leaves the instrument clamped wherever it had reached. Doing it on the
tick rather than in the GUI call keeps every hardware write on the single
writer.

Only VIs in `_active_system_vis` are stopped, which is built from the run's
plan *targets*. For a sample-access operation that is the VTI alone; the
magnets, dispatched as commands, keep ramping down to zero field. That is
both safe and wanted — skipping the warm-up is not a reason to leave a magnet
energised while the cryostat is opened.

### Conformance

`tests/test_conformance.py::test_operation_steps_contract` checks every
discovered operation automatically: unique non-empty keys, a label per step,
a `kind` in `STEP_KINDS`, every step reachable in order via `current_step()`,
and a JSON-serialisable `steps_summary()`. An operation that declares no
steps passes trivially. This is what makes the pattern a standard the next
operation inherits rather than a shape someone has to remember to copy.

## Pre-run toggles

A sibling declaration to the step standard, for the opposite timing: some
behavior should be *skippable per run*, decided *before* the run starts,
rather than confirmed or skipped during it. `SampleLoadOperation`/
`SampleUnloadOperation` (via `SampleAccessOperationBase`) use this for
`disarm_measurement_vis` — an operator may want to run one of these while a
measurement VI is already armed for something unrelated, so `initiate()`'s
"stand by every measurement VI" step needs to be skippable, and
`claimed_vi_names()` needs to stop claiming them too when it is skipped.

- A class-level `pre_run_toggles: dict[str, str]` maps a config kwarg name
  (e.g. `"disarm_measurement_vis"`) to a human-readable checkbox label. The
  key must be a `**config` keyword the constructor already understands (a
  toggle is just a per-run override of a config default, nothing more).
- The GUI renders one checkbox per declared entry, **persistent** on the
  card (visible and editable whether or not a run is active — unlike the
  current-step action row, which shows only while running), checked by
  default. Its state at the instant Start is clicked is passed to the
  panel's factory closure as an extra `bool` keyword matching the declared
  key, which merges it into the constructed instance's `**config` — an
  operation with no declared toggles receives no extra keywords, so this
  costs nothing for every other operation.
- The constructor resolves the key exactly like any other config value
  (`config.get("disarm_measurement_vis", True)`) and the rest of the class
  reads its own resolved attribute (`self._disarm_measurement_vis`) —
  `initiate()`/`claimed_vi_names()` branch on it directly; there is no
  separate toggle-specific plumbing beyond the declaration and the
  constructor read.

## Files

| File | Responsibility | Key public API | Tests |
|------|----------------|-----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `helium_fill.py` | Ramps every magnet (`Station.magnet_vi_names()`) to zero field, switches the level meter to FAST refresh, samples the helium level once per `sample_period_s` into the shared `OperationBase` recorder (no HDF5 file — plan operation-concurrency-and-error-scoping.md §4), and finishes once the level holds at/above `fill_target_pct` for `fill_complete_window_s` (or `max_fill_duration_s` elapses); restores SLOW refresh on standby/abort and verifies it via `postcondition_gates()`. Tolerates `helium_low` (its whole purpose). `readiness_conditions()` exposes one aggregate `zero_field` row; `next_due()` predicts time-to-`helium_warning_pct` from the panel-supplied consumption rate (plan §12). `run_summary()` hands the recorded level curve, in the generic `"recording"` shape (docs/plans/archive/unified-servicing-log-and-run-recording.md §3), plus start/end level to the run manifest. `claimed_vi_names()` returns the configured level meter AND every magnet (it holds zero field as an invariant for the whole fill) — the VTI and everything else stays manually controllable during a fill. Not a **Hold phase** operation (`hold_for_operator` stays the default `False`) — its own completion condition ends the run, not the operator. | `HeliumFillOperation` | `tests/test_helium_fill.py`, `tests/test_operation_readiness.py`, `tests/test_operations.py` |
| `sample_access_base.py` | Shared base for the sample-access pair: "verify the cryostat is safe to open" — commands every magnet (`Station.magnet_vi_names()`) to `standby()`, ramps the configured VTI VI to `target_temperature_K` (default 290 K), and sends `standby` to every measurement VI (`Station.measurement_vi_names()`). No switch-VI dispatch (dropped when this operation split from the original `SampleChangeOperation`). The reference **Hold phase** logic AND the reference **stepped operation** (`hold_for_operator = True`): once the ramps land, `step()` never returns `None` on its own — the run holds while the operator walks the declared six-step sequence (warm VTI, close needle valve, open access valve, move rod, close access valve, flush; only the rod step's label differs between load and unload) and `sample()` records *every numeric monitored channel on the station* once per `sample_period_s` (default 10 s) into the shared recorder, read from `cached_state` rather than live VI calls so a station-wide trace adds no bus traffic to the tick path — until the operator clicks Finish or Abort; `run_summary()` hands the series and the step timeline off as `{"recording": {...}, "steps": [...]}`. No data file. Every step is skippable; skipping the warm-up sets `skip_ramp_requested`, which the Orchestrator honours by stopping the VTI ramp in place. `postcondition_gates()`, evaluated once as the run ends (Finish only), verifies `zero_field` (every magnet's `magnet_state() == "standby"`, which already covers the switch heater being off wherever a magnet has one — no separate heater gate), `vti_at_target`, and one `step_<key>` gate per declared step. `needle_valve: manual` stays the only wired mode not for lack of stack capability — `VTITemperatureControllerVI.set_needle_valve()`/`set_needle_valve_mode()` exist, and its `standby()` closes the valve — but because the 12 T cryostat's needle valve has no motor, so commanding the channel would move nothing. `tolerated_safety_flags` is empty. `readiness_conditions()` returns the `zero_field` row followed by one live row per step, shown mid-run by the Operations panel because of `hold_for_operator`. `claimed_vi_names()` returns exactly the magnets, VTI, and measurement VIs it commands in `initiate()` — unless the **pre-run toggle** `disarm_measurement_vis` (default `True`, GUI checkbox "Disarm measurement instruments") is unchecked for this run, in which case measurement VIs are neither commanded in `initiate()` nor claimed, so a measurement instrument already armed for something else is left alone and stays manually usable for the whole run. Concrete subclasses set only `name`/`description`/`ready_message`/`config_key`/`rod_step_label`. | `_SampleAccessOperationBase` | `tests/test_sample_access.py`, `tests/test_operation_readiness.py` |
| `sample_load.py` | Identity declaration for the "load a sample" half of the pair: `name = "Sample Load"`, `config_key = "sample_load"`, `rod_step_label = "Insert the sample rod"`. All behavior inherited from `sample_access_base.py`. | `SampleLoadOperation` | `tests/test_sample_access.py` |
| `sample_unload.py` | Identity declaration for the "unload a sample" half of the pair: `name = "Sample Unload"`, `config_key = "sample_unload"`, `rod_step_label = "Withdraw the sample rod"`. All behavior inherited from `sample_access_base.py`. | `SampleUnloadOperation` | `tests/test_sample_access.py` |
