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
L3 (Orchestrator) and L2 (Station); an operation that wants a dataset (the
fill's level curve) uses `DataManager` (L5) exactly like a procedure does.

```
GUI -> Orchestrator.run_operation()/queue_operation() -> Operation -> Station -> Virtual Instruments -> Drivers
                                                                \-> DataManager -> HDF5 (optional)
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
| `step()` | `StepPlan` or `None` | Every tick after the first sample |
| `sample()` | nothing (optional HDF5 write) | Once per step, before `step()` |
| `standby()` | `PhasePlan` | Operation ending — park hardware |
| `abort()` | `tuple[Command, ...]` | User abort / ERROR / EMERGENCY |
| `initiation_gates()` | `tuple[Gate, ...]` | Once, before the first `sample()` |
| `postcondition_gates()` | `tuple[Gate, ...]` | After `standby()`'s ramp completes |

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
  procedure contract: gates stepped once parking completes, verifying the
  cryostat actually reached the promised state (not just that the commands
  were sent) before the run is declared `done`.
- An operation that wants a dataset creates its own `DataManager` in
  `initiate()` exactly as `BaseProcedure` does, and exposes `data_filepath`
  so the Orchestrator's run manifest captures the path; a data file is never
  required (`OperationBase` has no default `DataManager`).

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
- `postcondition_gates()` includes a gate that reads `confirmed(key)` — the
  run cannot reach `done` until the flag is set.
- `Orchestrator.confirm_operation(key)` (mirroring `finish_operation()`,
  same duck-typed-active-operation / `action_blocked` pattern) is the single
  entry point a caller uses to set it.
- The GUI (Phase 5, `docs/plans/cryogenics-logbook.md` §10) renders one
  checkbox per declared `operator_confirmations` entry and forwards a click
  through `Orchestrator.confirm_operation(key)`; the entry is recorded in
  the **operations stream** as human-confirmed rather than machine-verified.

A future machine-verifiable capability (e.g. an ITC503 `close_needle_valve()`
VI method) replaces the confirmation with a real `Gate` and drops the
`operator_confirmations` declaration entirely — the postcondition contract
already supports both, which is why this is declared, not hardcoded.

## Files

| File | Responsibility | Key public API | Tests |
|------|----------------|-----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `helium_fill.py` | Ramps every magnet (`Station.magnet_vi_names()`) to zero field, switches the level meter to FAST refresh, samples the helium level once per `sample_period_s` into an HDF5 curve, and finishes once the level holds at/above `fill_target_pct` for `fill_complete_window_s` (or `max_fill_duration_s` elapses); restores SLOW refresh on standby/abort and verifies it via `postcondition_gates()`. Tolerates `helium_low` (its whole purpose). `readiness_conditions()` exposes one aggregate `zero_field` row; `next_due()` predicts time-to-`helium_warning_pct` from the panel-supplied consumption rate (plan §12). `claimed_vi_names()` returns just the configured level meter — the VTI and everything else stays manually controllable during a fill. | `HeliumFillOperation` | `tests/test_helium_fill.py`, `tests/test_operation_readiness.py`, `tests/test_operations.py` |
| `sample_change.py` | "Verify the cryostat is safe to open": ramps every magnet (`Station.magnet_vi_names()`) to zero field and the configured VTI VI to `target_temperature_K` (default 300 K), opens the first switch VI (if any), and sends `standby` to every measurement VI (`Station.measurement_vi_names()`). No sampling loop (`step()` returns `None` immediately) and no data file. `postcondition_gates()` verifies `zero_field`, `heater_off` (only for magnets whose cached state exposes `switch_heater_state`), `vti_at_target`, and — for the only supported `needle_valve: manual` mode — an **operator confirmation** (`needle_valve_confirmed`). `tolerated_safety_flags` is empty. `readiness_conditions()` mirrors the same four checks as live checklist rows; `config_key = "sample_change"` (plan §12). `claimed_vi_names()` returns exactly the magnets, VTI, switch (if any), and measurement VIs it commands in `initiate()`. | `SampleChangeOperation` | `tests/test_sample_change.py`, `tests/test_operation_readiness.py` |
