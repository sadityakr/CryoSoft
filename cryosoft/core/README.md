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
sample change — see `docs/plans/cryogenics-logbook.md` §4), detected by the
Orchestrator via duck-typing (`command_scope == "operation"`) rather than
import, so contract C5 stays clean; `DataManager` (L5) writes HDF5. `plan.py`
is the typed currency shared by all of them.

## Entry (how control/data enters this folder)

- `build_station(config_path)` reads a YAML config directory (`devices.yaml`,
  `monitor.yaml`) and constructs the driver + VI stack into a `Station`. The
  build is degraded-tolerant: an instrument that fails to *connect* lands in
  the Station's offline registry (`OfflineInstrument`, `offline_vi_names()`)
  instead of aborting, and `Station.retry_instrument()` /
  `Orchestrator.retry_reconnect()` can bring it live later; only *config*
  errors abort the build (and trigger the startup fallback chain).
- `Orchestrator(station, tick_interval_ms)` receives a `Station`; the GUI then
  submits `Procedure` objects and VI/global action requests to it.
- A `Procedure` is constructed with a `Station`, `sample_info`,
  `data_directory`, and GUI param values; it builds `plan.py` value objects.
- `Orchestrator` calls `build_operational_status()` then `apply_watchdog()` each
  tick from the already-polled station snapshot.
- The GUI (`gui/operations_panel.py`'s `OperationsPanel`/`OperationCard`)
  submits an `OperationBase` instance to `Orchestrator.run_operation()` /
  `queue_operation()` — a second, higher-priority request type driven by the
  same tick loop and state machine as `run_procedure()`/`queue_procedure()`.
  The same GUI panel also drives an operation's `readiness_conditions()`/
  `next_due()` (plan §12) directly against per-tick state snapshots — read
  only, never through the Orchestrator.

## Exit (what it hands to other layers)

- `Station` returns state snapshots `{vi_name: {field: value}}` from
  `get_state()`, ramp progress from `check_ramps()` / `get_ramp_status()`, and
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
- `Orchestrator.vi_faults()` / `acknowledge_fault()` / `retry_fault()` are the
  GUI-facing surface of the Station's runtime fault registry (plan §3) — the
  RUNTIME sibling of `offline_vi_names()` / `retry_reconnect()` for a VI that
  DID connect but has since gone stale/disconnected.
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
- The **claim** standard (plan operation-concurrency-and-error-scoping.md §1;
  GLOSSARY.md's **Claim**): every procedure/operation declares
  `claimed_vi_names() -> set[str] | None` (default `None` = claim every
  system VI); the Orchestrator captures it at run start and refuses a
  manual VI action only for a VI actually claimed by the active run,
  through the single `_manual_action_admissible()` predicate shared by
  `submit_vi_action()` and the tick's GUI-action drain gate.
- The **runtime fault tiering** standard (plan operation-concurrency-and-
  error-scoping.md §3; GLOSSARY.md's **Instrument fault**): a VI-scoped
  comm/stale/disconnected fault (`Station.FaultRecord`, populated by
  `get_state()`) quarantines only that VI (`_manual_action_admissible()`
  refuses it outright, before any other rule, in every state including
  IDLE); a stale VI CLAIMED by the active run additionally fails the run
  (`_fail_run_for_fault()` — `run_finished` "failed", the machine returns to
  IDLE, the queue does NOT auto-continue); a tripped safety flag stays
  global EMERGENCY (unchanged); an unhandled tick-boundary exception is the
  only case that still degrades to global ERROR (unknown blast radius).
  `core/events.py`'s `ErrorEvent` carries the structured payload
  (`vi_name`, `kind`, `severity`, `message`, `timestamp`) on the
  `error_event` signal.

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
| `plan.py` | Typed vocabulary of frozen dataclasses shared across every layer | `Target`, `Command`, `PhasePlan`, `StepPlan`, `ParamSpec`, `ParamGroup`, `DataSchema` (`.multiplexed()` per-label suffixing — the reading loop's per-slot index labels, `.validate()` raising `DataSchemaError`) | `test_plan.py` |
| `exceptions.py` | The exception hierarchy every layer catches by subtype | `CryoSoftError`, `CryoSoftCommunicationError`, `CryoSoftSafetyError`, `CryoSoftConfigError`, `DataSchemaError` | `test_foundation.py` |
| `events.py` | The structured `ErrorEvent` payload (plan operation-concurrency-and-error-scoping.md §3) — a tiny, dependency-free module so both `orchestrator.py` (emitter) and `gui/` (consumer) can import it | `ErrorEvent` (frozen dataclass: `vi_name`, `kind`, `severity`, `message`, `timestamp`) | `test_l2_station.py`, `test_l3_orchestrator.py`, `test_gui.py` |
| `decorators.py` | Marker decorators that tag VI methods for discovery and GUI generation; `@control` also carries the capability-scope standard | `monitored`, `control` (bare or `control(scope=...)`), `get_monitored_methods()`, `get_control_methods()`, `get_control_scope()`, `VALID_CONTROL_SCOPES` | `test_foundation.py`, `test_conformance.py` |
| `station.py` | L2 registry: builds VIs from config, polls state with stale-value caching, dispatches ramps and measurement commands, aggregates safety, enforces the capability-scope standard at command dispatch. `get_state()` also populates/clears a structured runtime fault registry (plan §3, `FaultRecord`) in the same pass as its existing stale/disconnected detection | `Station` (`get_vi`, `get_vi_names`, `measurement_vi_names`, `switch_vi_names`, `magnet_vi_names`, `measurement_selector_label`, `get_state`, `process_system_targets`, `send_measurement_commands(commands, allowed_scope=...)`, `check_ramps`, `stop_ramps`, `get_ramp_status`, `check_safety`, `safety_flag_sources`, `vi_faults`, `acknowledge_fault`, `clear_fault`, `retry_fault`); `FaultRecord`; `build_station()`, `build_station_with_fallback()`, `validate_config_dir()`, `read_instrument_metadata()`, `read_cryogenics_config()`, `read_servicing_logs_config()` | `test_l2_station.py`, `test_config_validation.py`, `test_operations.py`, `test_helium_fill.py` |
| `orchestrator.py` | L3 single-threaded cooperative state machine; the sole hardware writer; runs the monitor + status cycle each tick inside an exception boundary that degrades to ERROR. Monitoring is OFF at construction (nothing polled until `start_monitoring()`); `run_procedure()`/`run_operation()` auto-start it, stopping is refused outside IDLE/ERROR, `shutdown()` stops the tick timer. `INITIATION_GATE`/`READING_GATE` states hold the state machine on a procedure's/operation's declared `Gate`s between "targets dispatched" and "take a measurement"; the STANDBY state's postcondition sub-phase (duck-typed via `postcondition_gates()`, inert for a plain procedure) holds completion until an operation's declared gates all hold, timing out to ERROR. Operations (detected via `command_scope == "operation"`, never imported — keeps contract C5 clean) get queue-jumping priority over procedures and a narrow EMERGENCY-entry carve-out gated by `tolerated_safety_flags`. Claims + admission gate (plan operation-concurrency-and-error-scoping.md §1): `_active_claims` is captured from the active run's `claimed_vi_names()` at `_start_run()` and cleared on every teardown path (`_abort_active_procedure()`, `_finish_run()`); `_manual_action_admissible(vi_name)` is the single admission predicate shared by `submit_vi_action()` (what may be queued) and the `_tick_body()` drain gate (what may be drained, evaluated per action) — it now ALSO refuses a VI with an active runtime fault, checked first, before the claim/state rules (plan §3). A stale VI the active run claims fails the run via `_fail_run_for_fault()` (IDLE, not ERROR; queue not auto-continued); `_fail_to_error()` is reserved for unknown-blast-radius failures (tick-boundary exceptions, run-setup failures) | `Orchestrator` (`start_monitoring`, `stop_monitoring`, `is_monitoring`, `shutdown`, `run_procedure`, `queue_procedure`, `run_operation`, `queue_operation`, `finish_operation`, `confirm_operation`, `run_queue`, `pause_procedure`, `resume_procedure`, `abort_procedure`, `recover_from_error`, `acknowledge_emergency`, `submit_vi_action`, `submit_global_action`, `get_operational_status`, `vi_faults`, `acknowledge_fault`, `retry_fault`); `OrchestratorState` enum; `monitoring_changed` signal | `test_l3_orchestrator.py`, `test_operations.py` |
| `gates.py` | Generic tick-driven wait primitive: a one-shot action optionally followed by a windowed stability check, declared by a procedure via `initiation_gates()`/`reading_gates()` or by an operation via `initiation_gates()`/`postcondition_gates()`. `step()` is polled each tick while in `INITIATION_GATE`/`READING_GATE`. `postcondition_gates()` is evaluated differently — once, via `check_once()`, as an operation's run ends (plan operation-concurrency-and-error-scoping.md §2 — no holding, no timeout). | `Gate` (`step() -> bool`, `check_once() -> bool`) | `test_core_gates.py` |
| `procedure.py` | L4 base classes: the Orchestrator-driven lifecycle and the generic sweep engine | `BaseProcedure` (`initiate` -> `PhasePlan`, `change_sweep_step` -> `StepPlan \| None`, `measure`, `standby` -> `PhasePlan`, `abort` -> `tuple[Command, ...]`, `get_param_groups()` classmethod, `initiation_gates()`/`reading_gates()` -> `tuple[Gate, ...]`, `claimed_vi_names()` -> `set[str] \| None`); `SweepMeasureProcedure` (GUI-selected measurement VI, the reading loop — up to two generic slots of `reading_setters` parameters, switch route and source current alike, per datapoint; `A{i}`/`B{j}` column labels + HDF5 `loop_labels` metadata, `DataSchema` assembly; concrete axes supply six hooks) | `test_l4_procedure.py`, `test_new_procedures.py` |
| `operation.py` | L4 base class for cryostat-servicing operations (plan §4): the same `PhasePlan`/`StepPlan`/`Gate` currency as a procedure, plus `tolerated_safety_flags`, `command_scope = "operation"`, `postcondition_gates()`, `claimed_vi_names()` (the concurrency-scope hook, plan operation-concurrency-and-error-scoping.md §1), and `run_summary()` (the same plan, §4 — a duck-typed, JSON-safe data hand-off to the session layer via the run manifest's `summary` key, for an operation with no HDF5 file). `measure()`/`change_sweep_step()` are final adapters over `sample()`/`step()` so the Orchestrator drives an operation with the same state machine as a procedure. Also declares the GUI-facing readiness/next-due contract (plan §12): `readiness_conditions()`/`next_due()` hooks and `ready_message`/`config_key` class attributes, read only by the Operations panel, never the Orchestrator | `OperationBase` (`initiate`, `step`, `sample`, `standby`, `abort`, `initiation_gates`, `postcondition_gates`, `claimed_vi_names`, `get_progress`, `get_params`, `run_summary`, `request_finish`, `readiness_conditions`, `next_due`), `ReadinessCondition`, `NextDue` | `test_operations.py`, `test_operation_readiness.py` |
| `data_manager.py` | L5 HDF5 file lifecycle for one procedure run: pre-allocated datasets, per-point save, abort trimming | `DataManager` (`save_datapoint`, `close`) | `test_l5_data_manager.py` |
| `sweep_builder.py` | Reusable sweep-array construction and the declarative `SweepAxis` used by procedures | `SweepSegment`, `build_piecewise_sweep()`, `load_custom_sweep_csv()`, `apply_hysteresis()`, `SweepAxis`, `sweep_axis_param_specs()`, `build_axis_sweep()` | `test_sweep_builder.py` |
| `operational_status.py` | Pure builder of the per-tick runtime "why is the run slow/stuck" status record | `build_operational_status()`, `RunFaultCode`, `worst_code()`, `VIHealth` | `test_operational_status.py` |
| `watchdog.py` | Deterministic stall detection layered on the status record (RAMP_STALLED, STALLED_RUN) | `apply_watchdog()`, `WatchdogState`, `WatchdogConfig` | `test_watchdog.py` |
| `config_catalog.py` | Qt-free discovery and versioning of shipped vs user config directories (copy-on-edit fork, named history) | `ConfigCatalog`, `ConfigEntry`, `ConfigVersion` | `test_config_catalog.py` |
| `logging_config.py` | Configures the rotating file + console handlers and the `cryosoft.status` JSONL stream | `setup_logging()` | none |
