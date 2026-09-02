# core/

## Purpose

`core/` holds the instrument-agnostic infrastructure every other layer depends
on: the typed vocabulary that all layers exchange, the L2 `Station` and L3
`Orchestrator` runtime, the L4 procedure and operation base classes, the L5
data manager, and the cross-cutting utilities (exceptions, decorators,
logging, config catalog, runtime status). Nothing here knows about a specific
instrument.

## Architecture layer

Cross-cutting infrastructure spanning L2 to L5 plus shared utilities. The
runtime classes are layered: `Station` (L2) builds and polls Virtual
Instruments; `Orchestrator` (L3) is the single tick loop and the sole writer to
hardware, driving both procedures and operations; `BaseProcedure` /
`SweepMeasureProcedure` (L4) are measurement recipes; `OperationBase` (L4) is
the parallel contract for multi-step cryostat-servicing actions (helium fill,
sample change — see `docs/plans/archive/cryogenics-logbook.md` §4), detected by the
Orchestrator via duck-typing (`command_scope == "operation"`) rather than
import, so contract C5 stays clean; `DataManager` (L5) writes HDF5. `plan.py`
is the typed currency shared by all of them.

## Entry (how control/data enters this folder)

- `build_station(config_path)` reads a YAML config directory (`devices.yaml`,
  `monitor.yaml`) and constructs the driver + VI stack into a `Station`. The
  build is degraded-tolerant: an instrument that fails to *connect* lands in
  the Station's offline registry (`OfflineInstrument`, `offline_vi_names()`)
  instead of aborting, and `Station.connect_instrument()` /
  `Orchestrator.connect_instrument()` can bring it live later; only *config*
  errors abort the build (and trigger the startup fallback chain). The build
  sends exactly one command per instrument — an identity query
  (`_identity_check()` -> `BaseVirtualInstrument.ping()`); an instrument whose
  session opens but which never answers is degraded too, since an open session
  is no proof anything is on the other end of the cable. See the **connection
  lifecycle** in GLOSSARY.md.
- `Station.disconnect_instrument()` is that standard's other half: it releases
  a live VI to its own front panel (or vendor software) WITHOUT standing it
  down — a magnet at field stays at field — and degrades it into the same
  offline registry, closing only the driver sessions no other live VI still
  needs (`_exclusive_aliases()`). `Orchestrator.connect_instrument()` /
  `disconnect_instrument()` are the IDLE-gated public surface, reporting
  through `action_succeeded`/`action_failed` plus
  `instrument_reconnected`/`instrument_disconnected`.
- `Orchestrator(station, tick_interval_ms)` receives a `Station`; the GUI then
  submits `Procedure` objects and VI/global action requests to it.
- A `Procedure` is constructed with a `Station`, `sample_info`,
  `data_directory`, and GUI param values; it builds `plan.py` value objects.
- `Orchestrator` calls `build_operational_status()` then `apply_stall_verdict()`
  each tick from the already-polled station snapshot.
- The GUI (`gui/operations_panel.py`'s `OperationsPanel`/`OperationCard`)
  submits an `OperationBase` instance to `Orchestrator.run_operation()` /
  `queue_operation()` — a second, higher-priority request type driven by the
  same tick loop and state machine as `run_procedure()`/`queue_procedure()`.
  The same GUI panel also drives an operation's `readiness_conditions()`/
  `next_due()` (plan §12) directly against per-tick state snapshots — read
  only, never through the Orchestrator.

## Exit (what it hands to other layers)

- `Station` returns state snapshots `{vi_name: {field: value}}` from
  `get_state()`, ramp progress from `advance_ramps()` (steps every active ramp
  generator by one tick and returns the names still ramping — the only thing
  that makes a ramp progress, so every non-PAUSED tick must reach it),
  `check_ramps(vi_names)` (that advance plus a completion verdict scoped to the
  caller's own ramps — see GLOSSARY.md's **Ramp scope**) / `get_ramp_status()`
  (the single aggregation point for the **ramp-introspection standard** —
  value, NEXT setpoint, END setpoint, rate, phase, status per system VI,
  polled once per tick and shared by the operational-status record and the
  ramp tracker), and
  aggregated safety verdicts from `check_safety(state)` (reuses the tick
  snapshot, no extra poll).
- `Orchestrator` emits Qt signals to the GUI: `states_updated`, `state_changed`,
  `error_occurred`, `error_event` (the structured `ErrorEvent` counterpart,
  plan §3 — every `error_occurred` emission has a matching `error_event`; a
  plain per-VI fault warning emits ONLY `error_event`, deliberately not
  `error_occurred`), `action_blocked`, and the per-action verdict pair
  `action_succeeded` / `action_failed`. Run-scoped signals are routed by run
  kind (**Hard status separation**, plan operation-concurrency-and-error-
  scoping.md §2): `procedure_progress`, `procedure_finished`,
  `measurement_ready`, and `status_message` fire ONLY for a procedure run;
  `operation_status` / `operation_progress` fire instead for an operation
  run. `active_run_kind()` is the public accessor GUI code uses to tell them
  apart without duck-typing.
- `Orchestrator.ramps_updated` (every tick) / `active_ramps()` (the cached
  read) are the ramp-tracker surface: `list[core.ramps.RampRecord]`, one per
  system VI actually ramping, each naming its rate, next setpoint, end
  setpoint, the run that owns it, and whether the operator may stop it.
  `stop_ramp(vi_name)` is the matching action — the per-instrument
  counterpart of `abort_procedure()`, admitted through the same
  `_manual_action_admissible()` predicate (so a VI claimed by a running
  procedure is refused; abort the run instead) and holding the instrument
  where it is, immediately, exactly like an abort.
- `Orchestrator.vi_faults()` / `acknowledge_fault()` / `retry_fault()` are the
  GUI-facing surface of the comm-origin slice of the Station's unified
  condition registry (the System-Condition standard; GLOSSARY.md's
  **Instrument fault**) — the RUNTIME sibling of `offline_vi_names()` /
  `connect_instrument()` for a VI that DID connect but has since gone
  stale/disconnected. Deliberately NOT the same thing as an operator
  disconnect: a fault means the instrument stopped answering while CryoSoft
  still held it, so it refuses manual control and can fail a run, whereas a
  disconnect is expected and `disconnect_instrument()` clears any standing
  fault on the way out.
- `Orchestrator.availability(vi_name)` / `availabilities()` are the single
  GUI-facing accessor for the Availability standard (`cryosoft.core.
  availability`; GLOSSARY.md's **Availability** / **Availability tag**) —
  thin passthroughs to `Station.availability()` / `availabilities()`. They
  answer "why can't I use this instrument?" for every VI, live or offline,
  as ONE derived record (`state` plus a `tags: frozenset[str]`) instead of
  three separate lookups: the offline registry, the comm-origin fault
  condition above, and a VI's own attachment state. A future degraded mode
  is a new tag plus a `TAG_POLICY` row, never a fourth accessor.
- `DataManager` writes one HDF5 file to disk per procedure run.
- `plan.py` hands immutable value objects to every layer; a malformed plan
  raises at construction, at the guilty module, not deep in the tick loop.

## Interface contract

- Dependencies point strictly downward; `core/` never imports from `drivers/`,
  `virtual_instruments/`, `procedures/`, or `gui/` (except `station.py`, which
  imports the VI base classes it constructs).
- The `Orchestrator` is the only writer to hardware; procedures and the GUI
  submit requests and never touch VIs or drivers directly.
- Every plan object (`Target`, `Command`, `PhasePlan`, `StepPlan`, `ParamSpec`,
  `DataSchema`) validates eagerly at construction.
- Limits and constants live in config, never hardcoded here.
- The capability-scope standard: every `@control` method carries a scope
  (`"measurement"`, the default, or `"operation"`); `Station.
  send_measurement_commands(commands, allowed_scope=...)` enforces it — an
  operation-scope command in a measurement-scope batch raises
  `CryoSoftSafetyError` before anything is dispatched. The Orchestrator passes
  `allowed_scope="operation"` only when the active procedure is an operation
  (`command_scope == "operation"`).
- The **claim** standard (GLOSSARY.md's **Claim**): every procedure/operation
  declares `claimed_vi_names() -> set[str] | None` (default `None` = claim
  every system VI); the Orchestrator captures it at run start and refuses a
  manual VI action only for a VI actually claimed by the active run,
  through the single `_manual_action_admissible()` predicate shared by
  `submit_vi_action()` and the tick's GUI-action drain gate. A second
  consumer: `BaseProcedure._claim_initiate_commands()` turns the same
  declaration into one `initiate()` `Command` per claimed VI, carried in
  `PhasePlan.claim_commands` and dispatched by `Orchestrator._start_run()`
  BEFORE that plan's own `targets`/`commands` — so every VI a run claims is
  already in its standard operating state before the run's first target or
  command reaches it.
- The **System-Condition standard** (GLOSSARY.md's **System condition** /
  **Severity ladder**; full text in `core/conditions.py`'s module
  docstring): every "something is wrong" signal in CryoSoft — a VI's
  `evaluate_safety()` flag, the Station's comm-fault detection, a
  `ExperimentEnvelope.check_state()` violation, a failing **Trend check** —
  is a `Condition` from one of exactly four producers (`"comm"`, `"safety"`,
  `"envelope"`, `"trend"`), and scope follows from severity alone, never
  from which producer reported it:
  `"advisory"` (reported, no enforcement — every `"trend"`-origin condition
  is this severity today), `"hold"` (scoped to
  `affected_vis` — those VIs are stood by once on onset, refused manual
  control, and fail any run watching one of them), `"critical"`
  (station-wide by construction — EMERGENCY, `standby_all()`, every manual
  control refused until acknowledged). Every condition, of every origin,
  lives in ONE registry (`Station._conditions`, read via `conditions()` /
  `active_critical_conditions()`, acknowledged via `acknowledge_condition()`
  — see GLOSSARY.md's **Instrument fault** / **Safety hold** / **Critical
  safety flag**). The tick pipeline (`Orchestrator._tick_body()`) runs the
  whole standard once per tick: one `check_safety()` call feeds
  `Station.update_conditions()` (which alone applies **Tolerated safety
  flags** — the single application point for a hold-severity flag's
  tolerance), the result is merged with this tick's `envelope_conditions()`,
  an onset diff over the merged condition-key set fires per-instrument
  fault events for a NEW comm-origin condition (comm-origin only — a
  hold-severity safety condition's enforcement no longer lives in this
  diff), one `decide()` call turns the merged list into a `Verdict`,
  and the Orchestrator executes it: `_enforce_safety_holds(verdict)` keeps
  every VI in `verdict.held_vis` at standby for as long as it is held and
  not acknowledged — a LEVEL-TRIGGERED invariant (re-checked and, if
  needed, re-asserted every tick, rate-limited per VI by
  `hold_enforcement_interval_s`) rather than a one-shot onset action, so a
  hold that survives an acknowledge-then-expire cycle is still enforced
  after the override lapses; a VI whose `standby()` keeps failing past
  `hold_enforcement_max_attempts` is escalated once per episode (CRITICAL
  log + a `kind="safety_hold"` `ErrorEvent`) without transitioning the
  state machine — then `emergency` → `_enter_emergency()` (always a
  blanket `standby_all()`, which is why `_enforce_safety_holds()` runs
  only when not already handled by that block — see its call site's
  comment) and `run_failure` → `_fail_run_for_fault()`. EMERGENCY refuses
  every manual action station-wide — there is no "unconcerned VI" once
  critical severity has stopped the whole station — until
  `Orchestrator.acknowledge()` unlocks the front-panel override (time-boxed,
  GLOSSARY.md's **Hold acknowledge**); the same `acknowledge()` also unlocks
  a plain hold-severity condition (e.g. `helium_low`) without ever entering
  EMERGENCY — and `_enforce_safety_holds()` treats an acknowledged hold
  exactly like `_manual_action_admissible()` does, skipping enforcement for
  as long as the override is live. `core/conditions.py` holds the pure
  policy (the
  `Condition`/`Verdict` value objects and the deterministic `decide()`
  function) with no dependency on the Station or Orchestrator (import-linter
  contract C13), so the policy is unit-testable without a running system.
  `Station.vi_faults()` (GLOSSARY.md's **Instrument fault**) is the
  permanent GUI adapter synthesizing a `FaultRecord` per comm-origin
  condition — the one place the pre-standard fault-registry shape is
  preserved for callers that want it instead of the typed `Condition`.
  `"trend"` is the one origin that does NOT refresh on the tick pipeline
  above: `trend_check_runner.TrendCheckRunner` is a small, Orchestrator-free
  `QObject` on its own slow timer (default 60 s) that evaluates
  `trend_checks.declared_checks()` and calls the public
  `Station.publish_conditions("trend", conditions)` directly — the
  origin-scoped prune-then-upsert counterpart of `update_conditions()`'s
  inline safety-specific one, reusable by any future origin with the same
  "refresh my own slice on my own cadence" need. The Orchestrator never
  imports `trend_checks`/`trend_check_runner` and never calls into either;
  it learns about a trend condition only because `_update_operational_status()`
  already reads the WHOLE registry via `Station.conditions()` every tick,
  regardless of which origin most recently refreshed which key.

## How to add a new module

1. Create `core/your_module.py` with the PEP 257 header docstring (Input /
   Process / Output; see Workspace Rule 1 in CLAUDE.md).
2. Keep it instrument-agnostic: no imports from `drivers/` or
   `virtual_instruments/` (aside from the VI base classes `station.py` needs).
3. Write its tests in `tests/` before the module is considered done.
4. Add a row to the Files map below, including its owning test file.

## Files

Each row: responsibility, key public API, and the test file(s) in `tests/` that
verify it. "tests: none" means no dedicated coverage exists (not a suggestion to
skip it).

| File | Responsibility | Key public API | Tests |
|------|----------------|----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `plan.py` | Typed vocabulary of frozen dataclasses shared across every layer | `Target`, `Command`, `PhasePlan`, `StepPlan`, `ParamSpec`, `ParamGroup`, `DataSchema` (`sweep_columns` + `measurement_scalars` + `measurement_arrays` + `loop_shape` — the reading loop's real `(n_loop1, n_loop2)` axis, `.validate()` raising `DataSchemaError`) | `test_plan.py` |
| `conditions.py` | The System-Condition standard's pure policy core (origin × severity, scope follows severity): the `Condition`/`Verdict` value objects and the deterministic `decide()` verdict function. Stdlib-only — no other `cryosoft` import, machine-enforced by import-linter contract C13 | `Condition`, `Verdict`, `decide()`, `envelope_conditions()`, `SEVERITIES`, `ORIGINS` | `test_conditions.py` |
| `availability.py` | The Availability standard's pure policy core (GLOSSARY.md's **Availability** / **Availability tag**): a closed tag vocabulary, a declared per-tag policy table, and the deterministic `state_for()`/`decide_availability()` functions that turn a VI's tag set into one of four mutually exclusive states plus a `TagPolicy` verdict. Stdlib-only — no other `cryosoft` import, machine-enforced by import-linter contract C14, mirroring C13's isolation of `conditions.py` above | `AVAILABILITY_TAGS`, `AVAILABILITY_STATES`, `TAG_PRECEDENCE`, `TagPolicy`, `TAG_POLICY`, `Availability`, `state_for()`, `decide_availability()` | `test_availability.py` |
| `exceptions.py` | The exception hierarchy every layer catches by subtype | `CryoSoftError`, `CryoSoftCommunicationError`, `CryoSoftSafetyError`, `CryoSoftConfigError`, `DataSchemaError` | `test_foundation.py` |
| `run_builder.py` | The single headless construction path for a procedure: turns a procedure class plus plain values (params, sample info, data directory, prefix, experiment context) into a ready `BaseProcedure`. Imports no Qt, so a run can be built from a test, a script, or any non-GUI client; construction refusal is raised, never swallowed, and `PROCEDURE_BUILD_ERRORS` names the one tuple every caller catches | `build_procedure()`, `PROCEDURE_BUILD_ERRORS` | `test_run_builder.py` |
| `ramps.py` | The `RampRecord` payload for `Orchestrator.ramps_updated`/`active_ramps()` — one entry per ramp running right now (label, unit, value, NEXT setpoint, END setpoint, rate, phase, owning run, stoppable + refusal reason, stale) — plus the pure `build_ramp_records()` builder that filters and assembles one `Station.get_ramp_status()` snapshot into it. Dependency-free, like `events.py`, so `orchestrator.py` (emitter) and `gui/ramp_tracker_panel.py` (consumer) can both import it | `RampRecord`, `build_ramp_records()`, `ACTIVE_RAMP_STATUS` | `test_ramps.py`, `test_l3_orchestrator.py` |
| `events.py` | The structured `ErrorEvent` payload AND the **control contract** — the frozen, JSON-safe message families the engine and its clients (the GUI today, an agent gateway later) exchange: one `Command` in, exactly one `Verdict` out, consequences as `Event`s, every message naming its `Actor`. Dependency-free, like `ramps.py`, so emitter and consumers can all import it. `Command`/`Verdict` here are the control-contract pair, distinct from `plan.Command` (a VI method call) and `conditions.Verdict` (an enforcement decision) | `ErrorEvent`; `Actor`, `ActorKind`, `OPERATOR`; `Command`, `CommandName` (every public Orchestrator command); `Verdict`, `VerdictCode`; the `Event` union (`StateChange`, `StatusSnapshot`, `StationInfo`, `Readings`, `Datapoint`, `RunStarted`, `RunFinished`, `QueueChanged`) with `event_from_json()`; `to_json()`/`from_json()` on every type | `test_conformance.py`, `test_l2_station.py`, `test_l3_orchestrator.py`, `test_gui.py` |
| `decorators.py` | Marker decorators that tag VI methods for discovery and GUI generation; `@control` also carries the capability-scope standard | `monitored`, `control` (bare or `control(scope=...)`), `get_monitored_methods()`, `get_control_methods()`, `get_control_scope()`, `VALID_CONTROL_SCOPES` | `test_foundation.py`, `test_conformance.py` |
| `station.py` | L2 registry: builds VIs from config, polls state with stale-value caching, dispatches ramps and measurement commands, aggregates safety, enforces the capability-scope standard at command dispatch. Owns the ONE unified condition registry (the System-Condition standard, see `conditions.py` and GLOSSARY.md's **System condition**): `get_state()` records a comm-origin hold `Condition` per stale/disconnected VI in the same pass as its existing detection; `update_conditions(safety, tolerated_flags=...)` refreshes every safety-origin `Condition` from a `check_safety()` snapshot the Orchestrator passes in, reading each flag's severity off its producer's `safety_flags` manifest and — for hold-severity flags only — applying `tolerated_flags` and scoping to `get_concerned_vis(flag)`; a critical/advisory flag's condition is built unconditionally, station-wide. `publish_conditions(origin, conditions)` is the public, origin-scoped counterpart usable by any producer outside `Station` itself — today only `trend_check_runner.TrendCheckRunner` (the `"trend"` origin) — reusing the same prune-then-upsert shape as the inline safety block, generalized over `origin` so refreshing one origin never disturbs another's entries; `conditions()` / `active_critical_conditions()` / `acknowledge_condition(key)` are the origin-agnostic read/acknowledge surface; `vi_faults()`/`acknowledge_fault()`/`clear_fault()`/`retry_fault()` are the permanent GUI adapter synthesizing a `FaultRecord` per comm-origin condition, preserving the pre-standard field shape for callers that want it instead of the typed `Condition`. `availability(vi_name)` / `availabilities()` are the Availability standard's single accessor (GLOSSARY.md's **Availability**): a DERIVED VIEW, never a fourth registry, assembled from the offline registry (`_offline_vis`, `OfflineInstrument.tags`), the `not_responding` slice of the same unified condition registry, and each VI's own `is_attached()` | `Station` (`get_vi`, `get_vi_names`, `measurement_vi_names`, `switch_vi_names`, `magnet_vi_names`, `measurement_selector_label`, `get_state`, `process_system_targets`, `send_measurement_commands(commands, allowed_scope=...)`, `advance_ramps`, `check_ramps(vi_names=None)`, `stop_ramps(vi_names=None)`, `get_ramp_status`, `check_safety`, `safety_flag_sources`, `get_concerned_vis`, `conditions`, `active_critical_conditions`, `acknowledge_condition`, `update_conditions`, `publish_conditions`, `vi_faults`, `acknowledge_fault`, `clear_fault`, `retry_fault`, `availability`, `availabilities`); `FaultRecord`; `build_station()`, `build_station_with_fallback()`, `validate_config_dir()`, `read_instrument_metadata()`, `read_cryogenics_config()`, `read_safety_config()`, `read_trends_config()`, `read_tick_interval_ms()`, `read_servicing_logs_config()` | `test_l2_station.py`, `test_config_validation.py`, `test_operations.py`, `test_helium_fill.py`, `test_l3_orchestrator.py` |
| `orchestrator.py` | L3 single-threaded cooperative state machine; the sole hardware writer; runs the monitor + status cycle each tick inside an exception boundary that degrades to ERROR; each tick also feeds `Station.last_state_flat()` to a `TieredTrendLogger` (non-fatal) for the raw/3-min/hourly trend-history tiers. Monitoring is OFF at construction (nothing polled until `start_monitoring()`); `run_procedure()`/`run_operation()` auto-start it, stopping is refused outside IDLE/ERROR, `shutdown()` stops the tick timer. `INITIATION_GATE`/`READING_GATE` states hold the state machine on a procedure's/operation's declared `Gate`s between "targets dispatched" and "take a measurement"; `pause_procedure()` holds the hardware immediately from every state EXCEPT `MEASURING`, where it raises a request that the `SWEEPING` branch honours once `measure()` has saved its datapoint (GLOSSARY.md's **Pause boundary**) — a point being read is never stranded, and resume from there starts at the ramp to the next point; an operation's `postcondition_gates()` are evaluated exactly once, immediately, as the run ends (`Gate.check_once()` — the state snapshot is refreshed first so standby-command effects are visible; unmet gates land in the manifest's `postconditions_unmet`, never blocking). Operations (detected via `command_scope == "operation"`, never imported — keeps contract C5 clean) get queue-jumping priority over procedures and a narrow EMERGENCY-entry carve-out gated by `tolerated_safety_flags`. Claims + admission gate: `_active_claims` is captured from the active run's `claimed_vi_names()` at `_start_run()` and cleared on every teardown path (`_abort_active_procedure()`, `_finish_run()`); `_run_ramp_scope()` is the parallel answer for hardware motion rather than permission (GLOSSARY.md's **Ramp scope**) — the VIs the run has targeted, so it waits for, pauses, and stops only its own ramps, while `Station.advance_ramps()` keeps every ramp moving on every non-PAUSED tick; `_manual_action_admissible(vi_name)` is the single admission predicate shared by `submit_vi_action()` (what may be queued) and the `_tick_body()` drain gate (what may be drained, evaluated per action) — it refuses a VI carrying the Availability standard's `not_responding` tag first (`Station.availability(vi_name)` against `TAG_POLICY["not_responding"].controllable` — GLOSSARY.md's **Instrument fault** / **Availability tag**, no hardcoded origin check), then one with an active safety-origin hold (GLOSSARY.md's **Safety hold**), before the state/claim rules, and refuses EVERY VI once in EMERGENCY unless the manual override is unlocked. The unified tick pipeline (`_tick_body()`, the System-Condition standard): `check_safety()` exactly once, `Station.update_conditions()`, merge in this tick's `envelope_conditions()`, one onset diff over the merged condition-key set (fires fault events for new comm conditions on unwatched VIs, dispatches one-shot `standby()` to every VI a new hold condition affects), one `core.conditions.decide()` call over the merged list, then execute its `Verdict` — `emergency` → `_enter_emergency()` (always a blanket `standby_all()`; critical severity is station-wide by construction, so there is no concerned subset to narrow to), `run_failure` → `_fail_run_for_fault(vi_name, reason=...)` exactly for whichever origin `decide()` found. `_fail_to_error()` is reserved for unknown-blast-radius failures (tick-boundary exceptions, run-setup failures) | `Orchestrator` (`start_monitoring`, `stop_monitoring`, `is_monitoring`, `shutdown`, `run_procedure`, `queue_procedure`, `run_operation`, `queue_operation`, `finish_operation`, `confirm_operation`, `run_queue`, `pause_procedure`, `pause_pending`, `resume_procedure`, `abort_procedure`, `recover_from_error`, `acknowledge` (unified EMERGENCY/hold-severity override, time-boxed — GLOSSARY.md's **Hold acknowledge**), `submit_vi_action`, `submit_global_action`, `get_operational_status`, `held_vi_names`, `override_active`, `manual_override_expires_at`, `vi_faults`, `acknowledge_fault`, `retry_fault`, `availability`, `availabilities`, `active_ramps`, `stop_ramp`); `OrchestratorState` enum; `monitoring_changed`, `ramps_updated` signals | `test_l3_orchestrator.py`, `test_operations.py` |
| `gates.py` | Generic tick-driven wait primitive: a one-shot action optionally followed by a windowed stability check, declared by a procedure via `initiation_gates()`/`reading_gates()` or by an operation via `initiation_gates()`/`postcondition_gates()`. `step()` is polled each tick while in `INITIATION_GATE`/`READING_GATE`. `postcondition_gates()` is evaluated differently — once, via `check_once()`, as an operation's run ends (plan operation-concurrency-and-error-scoping.md §2 — no holding, no timeout). | `Gate` (`step() -> bool`, `check_once() -> bool`) | `test_core_gates.py` |
| `procedure.py` | L4 base classes: the Orchestrator-driven lifecycle and the generic sweep engine | `BaseProcedure` (`initiate` -> `PhasePlan`, `change_sweep_step` -> `StepPlan \| None`, `measure`, `standby` -> `PhasePlan`, `abort` -> `tuple[Command, ...]`, `get_param_groups()` classmethod, `initiation_gates()`/`reading_gates()` -> `tuple[Gate, ...]`, `claimed_vi_names()` -> `set[str] \| None`); `SweepMeasureProcedure` (GUI-selected measurement VI, the reading loop — up to two generic slots of `reading_setters` parameters, switch route and source current alike, per datapoint; each a real `loop_shape` axis 0/1 on every measurement column + HDF5 `loop1_values`/`loop2_values` metadata, `DataSchema` assembly; `axis_data_key()` names the axis column, defaulting to the declared `sweep_axis` so an axis that is not a ramped setpoint — elapsed time — overrides it instead of faking a `SweepAxis`; concrete axes supply six hooks) | `test_l4_procedure.py`, `test_new_procedures.py`, `test_time_series_procedure.py` |
| `operation.py` | L4 base class for cryostat-servicing operations (plan §4): the same `PhasePlan`/`StepPlan`/`Gate` currency as a procedure, plus `tolerated_safety_flags`, `command_scope = "operation"`, `postcondition_gates()`, `claimed_vi_names()` (the concurrency-scope hook, plan operation-concurrency-and-error-scoping.md §1), and `run_summary()` (the same plan, §4 — a duck-typed, JSON-safe data hand-off to the session layer via the run manifest's `summary` key, for an operation with no HDF5 file). `measure()`/`change_sweep_step()` are final adapters over `sample()`/`step()` so the Orchestrator drives an operation with the same state machine as a procedure. Also declares the GUI-facing readiness/next-due contract (plan §12): `readiness_conditions()`/`next_due()` hooks and `ready_message`/`config_key` class attributes, read only by the Operations panel, never the Orchestrator | `OperationBase` (`initiate`, `step`, `sample`, `standby`, `abort`, `initiation_gates`, `postcondition_gates`, `claimed_vi_names`, `get_progress`, `get_params`, `run_summary`, `request_finish`, `readiness_conditions`, `next_due`), `ReadinessCondition`, `NextDue` | `test_operations.py`, `test_operation_readiness.py` |
| `data_manager.py` | L5 HDF5 file lifecycle for one procedure run: pre-allocated datasets, per-point save, abort trimming. A **Raw diagnostic block**'s dataset is self-describing: `axes` names every dimension in order and, when `measurement_block_labels` declares them, `channel_names` gives the channel-axis column names — both written as HDF5 attributes directly on `/data/<block_name>`, never only in the JSON `data_config` metadata blob | `DataManager` (`save_datapoint`, `close`) | `test_l5_data_manager.py` |
| `sweep_builder.py` | Reusable sweep-array construction and the declarative `SweepAxis` used by procedures | `SweepSegment`, `build_piecewise_sweep()`, `load_custom_sweep_csv()`, `apply_hysteresis()`, `SweepAxis`, `sweep_axis_param_specs()`, `build_axis_sweep()` | `test_sweep_builder.py` |
| `operational_status.py` | Pure builder of the per-tick runtime "why is the run slow/stuck" status record | `build_operational_status()`, `RunFaultCode`, `worst_code()`, `VIHealth` | `test_operational_status.py` |
| `stall_detection.py` | Deterministic per-VI ramp-stall detection layered on the status record (RAMP_STALLED); thresholds taken in seconds and converted to a tick count once at construction via the setup's `tick_interval_ms` | `apply_stall_verdict()`, `StallState`, `StallConfig` | `test_stall_detection.py` |
| `config_catalog.py` | Qt-free discovery and versioning of shipped vs user config directories (copy-on-edit fork, named history) | `ConfigCatalog`, `ConfigEntry`, `ConfigVersion` | `test_config_catalog.py` |
| `paths.py` | Resolves machine-local, per-installation directories and settings outside source control: the log directory (`CRYOSOFT_LOG_DIR` env var, then the platform user-data location, then `cryosoft/logs/`) and the fixed measurement root (`CRYOSOFT_MEASUREMENT_ROOT` env var, else the `measurement_root` key in the machine-level `App-config.yaml` settings file, else refuse to start — see GLOSSARY.md's **Measurement root**). `config_directory()`/`data_directory()` for site-specific configs and incident reports are planned, see `docs/plans/config-directory-migration.md`. Stdlib-only, import-linter contract C1 foundation module | `log_directory()`, `measurement_root()` | `test_paths.py` |
| `logging_config.py` | Configures the rotating file + console handlers and four time-rotated JSONL streams — `cryosoft.status` (`status.jsonl`, daily/UTC, 7 backups) and the tiered trend-history streams `cryosoft.trend_raw`/`trend_3min`/`trend_hourly` (`trend_history_{raw,3min,hourly}.jsonl`, daily/daily/weekly, all UTC, 2/8/53 backups); log directory resolution delegates to `paths.log_directory()` | `setup_logging()` | `test_logging_config.py` |
| `tiered_trend_logger.py` | Qt-free live incremental writer for the tiered trend-history store: one throttled raw-tier JSONL line per `record()` call plus two independent bucket accumulators (3-min, 1-hour) flushed to their own JSONL streams on a bucket-boundary crossing. Wired into the Orchestrator tick, called with `Station.last_state_flat()`'s output right after the operational-status record. Two known live/disk asymmetries versus `gui.monitor_history.MonitorHistory` (its docstring has the full writeup): `last_state_flat()` excludes measurement VIs, so those never reach disk; it also has no `bool` guard (`bool` is an `int` subclass), so a boolean `@monitored` field IS coerced to `1.0`/`0.0` and persisted here even though `MonitorHistory.record()` explicitly excludes bools — the opposite direction, gaining a key on disk that the live view never had | `TieredTrendLogger` (`record(flat, timestamp, orch_state=None)`) | `test_tiered_trend_logger.py` |
| `trend_history.py` | Qt-free read/query side of the tiered trend-history store: merges each tier's live + rotated JSONL files (excluding sync-conflict-copy filenames), and turns them into plottable series (GUI) or agent-facing evidence — count-weighted aggregate recombination, `persisted` flag distinguishing "no data in window" from "never persisted" (measurement-VI keys), threshold-crossing detection | `TIERS`, `TierSpec`, `KeySummary`, `pick_tier()`, `persisted_keys()`, `read_tier()`, `read_window()`, `summarize()`, `find_crossings()` | `test_trend_history.py` |
| `trend_checks.py` | The **Trend check** standard's pure policy core: `TrendCheck` declarations evaluated against `trend_history.summarize()` and `read_window()` (never re-deriving mean/std, re-picking a tier, or adding endpoint values to `KeySummary`), a `CheckResult` verdict whose `passed` is `True`/`False`/`None` (`None` = no data in the window, distinct from a definite fail), and the `Condition`-publication adapter for a definite failure. A `Predicate` receives both the window's `{key: KeySummary}` aggregates and its `{key: [(t, value), ...]}` series (`run_check()` computes both once per evaluation), so aggregate-only checks (`sample_temperature_stable`) and rate-of-change checks (`helium_consumption_normal`) share one runner with no per-check branch. `declared_checks()` returns both; `trend_store_live` is deliberately NOT among them — see its own docstring and `cryosoft/troubleshoot/engine.py`'s `check_trend_store_live()`, the pull-only CLI-side sibling. Qt-free, Station-free, import-linter contract C15 | `TrendCheck`, `CheckOutcome`, `CheckResult`, `Predicate`, `WindowedSeries`, `run_check()`, `run_checks()`, `no_data_outcome()`, `to_condition()`, `conditions_for()`, `declared_checks()` | `test_trend_checks.py` |
| `trend_check_runner.py` | The ONE Qt-aware piece of the Trend check standard: a small `QObject` on its own slow timer (independent of the Orchestrator's tick timer) that evaluates `trend_checks.declared_checks()` against the resolved log directory and publishes failing checks via `Station.publish_conditions("trend", ...)`. Holds a `Station` reference only, never an `Orchestrator` | `TrendCheckRunner` (`run_once()`, `stop()`) | `test_trend_check_runner.py` |
