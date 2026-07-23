# procedures/operations/

## Purpose

`procedures/operations/` holds the concrete cryostat-**servicing** actions —
one class per operation (helium fill, sample change) — each a subclass of
`cryosoft.core.operation.OperationBase`. An operation is declarative like a
procedure (it returns plans, it never touches a VI directly), but is a
different request type: operation-scope command access, tolerated safety
flags, verified postconditions, an optional (not required) data file, and
higher submission priority than a queued measurement procedure. See
`docs/plans/cryogenics-logbook.md` §2/§4 for the full design and
`OperationBase`'s own docstring for the Orchestrator adapter contract.

## Architecture layer

L4 (Operations — same layer as Procedures, a parallel contract). Sits above
L3 (Orchestrator) and L2 (Station); an operation that wants an HDF5 dataset
may still use `DataManager` (L5) exactly like a procedure does, but a small
bounded in-memory series handed to the session layer via `run_summary()`
(docs/plans/operation-concurrency-and-error-scoping.md §4 — e.g. the helium
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
  `docs/plans/cryogenics-logbook.md` §9 `cryogenics:` block keys, or the
  sample change's `operations.sample_change:` block keys), each with a
  class-level default so the conformance suite's
  `test_operation_constructs_from_defaults` can build the class from a sim
  station alone.

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
  stays under manual control during a fill. `SampleChangeOperation` claims exactly the
  VIs its `initiate()` commands (magnets, VTI, switch, measurement VIs) —
  narrower than "everything" only on a station with instruments it never
  touches (e.g. a rotator).
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
  key (docs/plans/unified-servicing-log-and-run-recording.md §3):
  `{"recording": {"unix_time": [...], "channels": {"<vi>.<value>": [...],
  ...}}, ...}` — `cryosoft.session.servicing_log.CryogenicsRecorder` writes
  it as this run's `recordings/<run_id>.json` sidecar and stamps that
  filename into the run's single `servicing` log entry, whatever operation
  produced it. `HeliumFillOperation.run_summary()` is the reference
  implementation: `{"recording": {"unix_time": [...], "channels":
  {"<level_vi>.helium_pct": [...]}}, "start_pct": float, "end_pct": float}`.
- A recording is built with `OperationBase`'s shared, opt-in recorder helper
  (docs/plans/unified-servicing-log-and-run-recording.md §3): call
  `self._record_sample(unix_time, {channel_name: value, ...})` once per
  sample from `sample()` (every channel must be the SAME set on every call
  within one run — the shared time axis and every channel decimate
  together) and return `{"recording": self._recording_dict()}` (or fold
  extra keys around it, as the fill does with `start_pct`/`end_pct`) from
  `run_summary()`. `_MAX_RECORDING_POINTS` (class attribute, default 4000)
  bounds memory via stride-doubling decimation (`series[::2]`, generalising
  the fill's original `_MAX_CURVE_POINTS`); call `self._reset_recording()`
  from `initiate()` so a fresh run starts with an empty series.
  `HeliumFillOperation` (one channel) and `SampleChangeOperation` (VTI
  temperature + every magnet's field) both use it.

## Hold phase (plan §1)

`step()`'s normal contract is "return a `StepPlan` to keep sampling, or
`None` to end the run" — that "or `None`" part is a choice, not a
requirement. An operation whose physical action happens WHILE the run is
active rather than after a fixed condition (e.g. `SampleChangeOperation`:
the operator opens the cryostat during the hold, not after some elapsed
time or level threshold) declares a **hold phase**: `step()` never returns
`None` on its own once its setup work (ramps, etc.) is done — it keeps
returning a fresh `StepPlan` (mirroring an open-ended sampling loop like the
fill's) — so the run stays active indefinitely. The run ends only when the
operator clicks Finish (`Orchestrator.finish_operation()` ->
`request_finish()`): the very next `change_sweep_step()` (the
`OperationBase` adapter) then returns `None` regardless of what `step()`
would return, exactly the existing graceful-finish mechanism — no new
Orchestrator code needed.

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

## Operator confirmations

Some postconditions cannot be machine-verified because no capability exists
for them yet (plan §8.2's "needle-valve reality check": no needle-valve/
gas-flow capability exists anywhere in the stack today). Rather than skip
the postcondition or hardcode a GUI checkbox, an operation **declares** it:

- A class-level `operator_confirmations: dict[str, str]` maps a stable key
  (e.g. `"needle_valve"`) to a human-readable checkbox label (e.g.
  `"Needle valve closed"`).
- Instance methods `confirm(key)` (raises `ValueError` on an undeclared key)
  and `confirmed(key) -> bool` set and read the per-run flag.
- `postcondition_gates()` includes a gate that reads `confirmed(key)` —
  unconfirmed at the one-shot evaluation, the gate is simply named in
  `postconditions_unmet` rather than blocking `done`.
- `Orchestrator.confirm_operation(key)` (mirroring `finish_operation()`,
  same duck-typed-active-operation / `action_blocked` pattern) is the single
  entry point a caller uses to set it.
- The GUI (Phase 5, `docs/plans/cryogenics-logbook.md` §10) renders one
  checkbox per declared `operator_confirmations` entry and forwards a click
  through `Orchestrator.confirm_operation(key)`. An unconfirmed key still
  fails its postcondition gate at the one-shot evaluation, so it surfaces in
  the run's single `servicing` log entry via `postconditions_unmet`
  (`CryogenicsRecorder` folds it into `notes`, e.g. `"unmet:
  needle_valve_confirmed"`); a confirmed key leaves the gate passing and no
  trace in `notes` (the legacy `operations` stream's per-run `verified`
  column is gone with that kind).

A future machine-verifiable capability (e.g. an ITC503 `close_needle_valve()`
VI method) replaces the confirmation with a real `Gate` and drops the
`operator_confirmations` declaration entirely — the postcondition contract
already supports both, which is why this is declared, not hardcoded.

## Files

| File | Responsibility | Key public API | Tests |
|------|----------------|-----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `helium_fill.py` | Ramps every magnet (`Station.magnet_vi_names()`) to zero field, switches the level meter to FAST refresh, samples the helium level once per `sample_period_s` into the shared `OperationBase` recorder (no HDF5 file — plan operation-concurrency-and-error-scoping.md §4), and finishes once the level holds at/above `fill_target_pct` for `fill_complete_window_s` (or `max_fill_duration_s` elapses); restores SLOW refresh on standby/abort and verifies it via `postcondition_gates()`. Tolerates `helium_low` (its whole purpose). `readiness_conditions()` exposes one aggregate `zero_field` row; `next_due()` predicts time-to-`helium_warning_pct` from the panel-supplied consumption rate (plan §12). `run_summary()` hands the recorded level curve, in the generic `"recording"` shape (docs/plans/unified-servicing-log-and-run-recording.md §3), plus start/end level to the run manifest. `claimed_vi_names()` returns the configured level meter AND every magnet (it holds zero field as an invariant for the whole fill) — the VTI and everything else stays manually controllable during a fill. Not a **Hold phase** operation (`hold_for_operator` stays the default `False`) — its own completion condition ends the run, not the operator. | `HeliumFillOperation` | `tests/test_helium_fill.py`, `tests/test_operation_readiness.py`, `tests/test_operations.py` |
| `sample_change.py` | "Verify the cryostat is safe to open": ramps every magnet (`Station.magnet_vi_names()`) to zero field and the configured VTI VI to `target_temperature_K` (default 300 K), opens the first switch VI (if any), and sends `standby` to every measurement VI (`Station.measurement_vi_names()`). The reference **Hold phase** operation (`hold_for_operator = True`, plan §1): once the ramps land, `step()` never returns `None` on its own — the run holds while `sample()` records the VTI temperature and every magnet's field once per `sample_period_s` (new config key, default 10 s) into the shared recorder — until the operator clicks Finish; `run_summary()` hands the series off as `{"recording": {...}}`. No data file. `postcondition_gates()`, evaluated once as the run ends, verifies `zero_field`, `heater_off` (only for magnets whose cached state exposes `switch_heater_state`), `vti_at_target`, and — for the only supported `needle_valve: manual` mode — an **operator confirmation** (`needle_valve_confirmed`). `tolerated_safety_flags` is empty. `readiness_conditions()` mirrors the same four checks as live checklist rows, shown mid-run by the Operations panel's ready banner because of `hold_for_operator`; `config_key = "sample_change"` (plan §12). `claimed_vi_names()` returns exactly the magnets, VTI, switch (if any), and measurement VIs it commands in `initiate()`. | `SampleChangeOperation` | `tests/test_sample_change.py`, `tests/test_operation_readiness.py` |
