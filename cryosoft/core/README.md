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
sample change — see GLOSSARY.md's **Operation**), detected by the
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
  each tick from the already-polled station snapshot, emits the record on
  `operational_status`, and appends it as one JSON line to `status.jsonl`.
  **The record standard** — every field, its type, and the schema version —
  is the module docstring of `operational_status.py`; the log is the contract
  for its readers (`troubleshoot/status_reader.py`,
  `gui/diagnostics_window.py`), so a field is only ever added, never renamed,
  retyped, or given a new meaning, and an unknown value is `null` rather than
  a missing key. `RunFaultCode`'s values carry the same guarantee: they are a
  stable API that consumers map for display, so a member is added but never
  renamed or repurposed (`STALLED_RUN` is kept, and no longer produced, for
  exactly that reason).
  One record is written on **every** tick, whether monitoring is on or off,
  so that silence in `status.jsonl` unambiguously means the process stopped
  ticking — the property `troubleshoot status --max-age` gates on. A tick
  with monitoring off polls nothing (the same quiet guard `_publish_ramps()`
  uses), so its instrument payload is empty while every header field is
  written as usual.
- The GUI (`gui/operations_panel.py`'s `OperationsPanel`/`OperationCard`)
  submits an `OperationBase` instance to `Orchestrator.run_operation()` /
  `queue_operation()` — a second, higher-priority request type driven by the
  same tick loop and state machine as `run_procedure()`/`queue_procedure()`.
  The same GUI panel also drives an operation's `readiness_conditions()`/
  `next_due()` (**Readiness condition** / **Next due** in GLOSSARY.md)
  directly against per-tick state snapshots — read only, never through the
  Orchestrator.

- **The engine pulls the run queue.** The queue itself lives outside the
  engine, as data (`session/run_queue.py`, contract C12 keeps `core` from
  importing it), and `Orchestrator.run_queue()` reaches it through two
  injected callables: `next_procedure()` returns the next built run (or
  `None`), and `queue_snapshot()` returns the waiting entries as JSON-safe
  dicts for `QueueChanged`. `main.py` sets both. Runs handed over directly
  through `queue_procedure()`/`queue_operation()` still drain first, and
  operations still drain before procedures either way. The direction is the
  point: a client that watched `state_changed` for IDLE and started the next
  run itself would advance re-entrantly inside the engine's own synchronous
  emit, would starve queued operations, and — seeing only a state name —
  could not tell a clean finish from a **hold acknowledge**, so it would
  auto-start a run straight after an emergency standby. Six transitions reach
  IDLE and only two chain (`abort_procedure()` and `_finish_run()`'s IDLE end
  state); the other four — error recovery, the emergency acknowledge, a run
  failed for an instrument fault, and a completed *manual* ramp — deliberately
  do not. `run_queue()`'s docstring is the list. Every queue mutation is
  broadcast as a `QueueChanged` event naming the actor behind it;
  `publish_queue()` is how a client that changed the queue outside the engine
  asks for that broadcast.

- `Orchestrator.submit(Command) -> request_id` is the **engine port**: the
  single entry point for a client that speaks the control contract
  (`events.py`), as opposed to the public methods the GUI and the tests call
  directly. Dispatch is a lookup, not a table — every `CommandName`'s value IS
  the name of the method implementing it. `Command.args` is JSON, so the few
  commands whose methods take objects name them instead: `RUN_PROCEDURE` /
  `QUEUE_PROCEDURE` carry `procedure` (a class name resolved through the
  `run_catalog` given at construction) plus `params`, `sample_info`,
  `data_directory`, `file_prefix` and `experiment_info`, which
  `run_builder.build_procedure()` assembles into a run, plus the optional
  `probe_spec` (`{"n_points", "averaging", "max_wait_s"}`, any key optional)
  that reduces that same run to a **probe run** — same class, same
  instruments, a few points — whose manifest, `RunStarted` event and data file
  all declare `kind == "probe"`; `RUN_OPERATION` / `QUEUE_OPERATION` carry
  `operation` plus `params` and are built by
  `run_builder.build_operation()`; `SET_EXPERIMENT_ENVELOPE` carries the envelope's
  dict form (`ExperimentEnvelope.from_dict()`) or `null`; `SUBMIT_VI_ACTION`
  carries `vi_name`, `method_name` and the capability's own parameters as flat
  scalars beside them. The run catalog is injected rather than discovered
  because the Orchestrator may not import `cryosoft.procedures` (contract C5);
  a client that hands the engine a run object needs no catalog at all.

## Exit (what it hands to other layers)

- `Station` returns state snapshots `{vi_name: {field: value}}` from
  `get_state()`, ramp progress from `advance_ramps()` (steps every active ramp
  generator by one tick and returns the names still ramping — the only thing
  that makes a ramp progress, so every non-PAUSED tick must reach it),
  `check_ramps(vi_names)` (that advance plus a completion verdict scoped to the
  caller's own ramps — see GLOSSARY.md's **Ramp scope**) / `nominal_ramp_rates()`
  (the declaration-only counterpart: what every ramping VI WOULD ramp at, from
  config alone, which is what lets a **duration estimate** be computed before
  any run exists) / `get_ramp_status()`
  (the single aggregation point for the **ramp-introspection standard** —
  value, NEXT setpoint, END setpoint, rate, phase, status per system VI,
  polled once per tick and shared by the operational-status record and the
  ramp tracker), and
  aggregated safety verdicts from `check_safety(state)` (reuses the tick
  snapshot, no extra poll).
- `Orchestrator` emits Qt signals to the GUI: `states_updated`, `state_changed`,
  `error_occurred`, `error_event` (the structured `ErrorEvent` counterpart
  of the per-VI **Instrument fault** model, see GLOSSARY.md — every
  `error_occurred` emission has a matching `error_event`; a
  plain per-VI fault warning emits ONLY `error_event`, deliberately not
  `error_occurred`), `action_blocked`, and the per-action verdict pair
  `action_succeeded` / `action_failed`. Run-scoped signals are routed by run
  kind (**Hard status separation**, see GLOSSARY.md):
  `procedure_progress`, `procedure_finished`,
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
- `Orchestrator.emergency_standby(reason)` is the unconditional safe-off
  route (GLOSSARY.md's **Emergency standby**): permitted in EVERY state,
  including EMERGENCY and ERROR where every other manual route is refused,
  deliberately outside `_manual_action_admissible()`, logged at CRITICAL with
  the reason and routed into the same emergency flow a tripped critical
  condition takes. It runs on the caller's stack like `stop_ramp()`; because
  the tick is single-threaded and cooperative, a call arriving mid-tick lands
  after the current `measure()` returns — accepted latency, bounded by one
  reading, not an oversight.
- `Station.station_info()` returns the **station info** snapshot
  (GLOSSARY.md's **Station info**): the frozen, JSON-safe declaration of
  every configured instrument — live and offline alike — assembled from the
  VI classes' `@monitored` / `@control` / `ui_groups` / `safety_flags`
  declarations and the config the `control_limits` bounds come from.
  `capability_manifest.build_manifest(station)` is its JSON rendering (the
  **capability manifest**), with each instrument's capabilities resolved
  into its declared groups. Both are built from declarations and config
  alone and send NO command to any instrument, which is what lets them
  describe an unreachable instrument and be read outside the tick loop; the
  matching obligation on the VI layer is `control_param_specs()`'s purity
  rule (`virtual_instruments/base.py`).
- `Orchestrator.verdict_emitted` / `event_emitted` are the control contract's
  two channels (GLOSSARY.md's **Verdict standard** / **Event stream**), added
  alongside the per-purpose signals above rather than replacing them.
  `verdict_emitted` carries exactly one `events.Verdict` per submitted
  `Command`; `event_emitted` carries `StateChange` (every transition, with its
  cause and the actor behind it), `StatusSnapshot` (once per tick and on every
  state change — one field per engine read, so a client answers every query
  from its own mirror), `StationInfo` (at construction and after every
  connect/disconnect, iff the Station declares one — looked up with `getattr`,
  so a Station without it is simply silent), `Readings` (each monitored poll),
  `Datapoint` (each measured point) and `RunStarted`/`RunFinished`. One
  monotonic `seq` orders both channels together, and every emitted payload is
  a copy.
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
  `ParamGroup`, `UIGroup`, `DataSchema`) validates eagerly at construction.
- Limits and constants live in config, never hardcoded here.
- The **declaration standard's read side**: a client — the GUI's instrument
  panels, a future agent gateway's tool list — builds its whole instrument
  surface from `station_info()` / `build_manifest()`, never from
  `Station.get_vi()` and never from prose of its own. `core/events.py`
  DEFINES the shape (`StationInfo` and its nested `InstrumentInfo` /
  `MonitoredInfo` / `ControlInfo` / `GroupInfo`) and imports nothing
  (import-linter contract C16); `station.py` BUILDS it; `capability_manifest.py`
  RENDERS it and owns `MANIFEST_SCHEMA`. Three modules, three jobs, one
  declaration.
- The capability-scope standard: every `@control` method carries a scope
  (`"measurement"`, the default, or `"operation"`); `Station.
  send_measurement_commands(commands, allowed_scope=...)` enforces it — an
  operation-scope command in a measurement-scope batch raises
  `CryoSoftSafetyError` before anything is dispatched. The Orchestrator passes
  `allowed_scope="operation"` only when the active procedure is an operation
  (`command_scope == "operation"`).
- The **direct action path** standard (GLOSSARY.md's **Direct action path**;
  full text in `Station.execute_vi_action()`): a manual action — a GUI click,
  an agent call — reaches an instrument only through
  `Orchestrator.submit_vi_action()` → the tick's queue drain →
  `Station.execute_vi_action()`, and five independent checks refuse it before
  any hardware command is sent, each with its own reason: an
  underscore-prefixed name (`CryoSoftPrivateActionError`), a method that is
  neither `@control` nor one of `LIFECYCLE_ACTIONS` (`initiate`/`standby`,
  `CryoSoftUndeclaredActionError`), a capability whose scope exceeds the
  caller's `allowed_scope` (`CryoSoftActionScopeError` — the default is the
  restrictive `"measurement"`, and the Orchestrator's manual path opts into
  `MANUAL_ACTION_SCOPE` explicitly, in one named place), an out-of-range value
  (the control-validation standard's limit wrapper), and a setpoint outside
  the active envelope (the Orchestrator, at submission AND again at drain).
  The refusal set mirrors `troubleshoot.engine.DriverBench.call()`'s one layer
  down; the whole path is asserted, over every VI of every sim config, by
  `test_conformance.py` and `test_direct_action_path.py`.
- The **session envelope** binds every writer, not only plans: a manual
  action's setpoint parameter — identified by the setpoint-parameter
  convention (`plan.SETPOINT_PARAM_PREFIX`, resolved by
  `Station.setpoint_parameters()`), so no per-VI table lives in the
  Orchestrator — is checked against `ExperimentEnvelope` exactly as a
  `Target` is. `Station.envelope_variables()` is the matching read side: per
  VI, the capability that commands its enveloped quantity plus the setup's
  own `control_limits` bounds on it, which the Start Experiment dialog's
  envelope editor pre-fills from so an operator narrows rather than composes.
- The **verdict standard** (GLOSSARY.md's **Verdict standard**; full text in
  `Orchestrator`'s class docstring): every command is answered exactly once.
  Every refusal inside a command method goes through one of
  `_action_blocked()` / `_action_failed()` / `_action_succeeded()`, which emit
  the legacy `action_*` signal AND close the pending verdict; a method that
  returns having emitted no refusal is an acceptance, so silence is `OK`. The
  refusal CODE is produced next to the rule that decides
  (`_manual_action_admission()` returns it beside the reason), never by
  parsing prose, and a `CryoSoftSafetyError`'s structured
  `param`/`value`/`lo`/`hi`/`limit_name` become the verdict's `detail`. Every
  method `CommandName` enumerates takes a keyword-only `actor` (the `command`
  decorator adds it, defaulting to `events.OPERATOR`), held for the call's
  duration so the verdict and every `StateChange` it causes name who asked;
  transitions the engine makes on its own are attributed to `SYSTEM_ACTOR`.
  `test_conformance.py` holds both halves: every `CommandName` has a method,
  every method takes an `actor`, and every engine read has a `StatusSnapshot`
  field to answer it.
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
- **One vocabulary for live and stored runs** (`data_reader.py`'s module
  docstring is the full text; GLOSSARY.md's **Run source**): a run answers
  the same four questions — `list_columns()`, `read_slice()`,
  `summary_stats()`, `read_metadata()` — through the same frozen, JSON-safe
  `ColumnInfo` / `Stats` types, whether it is a finished HDF5 file or the run
  in flight. `RunSource` is that vocabulary as a `Protocol` — `RunHandle`
  (`data_reader.py`) implements it over an HDF5 file, `RunBuffer`
  (`run_buffer.py`) over the `Datapoint` events of the run being measured —
  and every implementation shares one `summarise_values()` so two sources of
  the same run cannot disagree about what "mean" means. A consumer — the
  GUI's live view today, an agent gateway later — depends on the protocol,
  never on which source it holds. `test_conformance.py` discovers run sources
  by their methods, so a third one is held to the same signatures the moment
  its file exists, and `test_run_buffer.py` drives a sim procedure through the
  real `DataManager` while feeding the same points to a `RunBuffer` and
  requires the two to answer identically. The reader is standalone by contract C17 (only
  `events`/`exceptions` from the package), so an analysis process can import
  it without a Station, an Orchestrator, or Qt.

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
| `plan.py` | Typed vocabulary of frozen dataclasses shared across every layer | `Target`, `Command`, `PhasePlan`, `StepPlan`, `ParamSpec`, `ParamGroup`, `UIGroup` (one titled group of ONE VI's own `@monitored`/`@control` methods, named in an explicit ordered `members` tuple — the VI-side counterpart of the procedure-side `ParamGroup`, presentation and description only), `DataSchema` (`sweep_columns` + `measurement_scalars` + `measurement_arrays` + `loop_shape` — the reading loop's real `(n_loop1, n_loop2)` axis, `.validate()` raising `DataSchemaError`), `EnvelopeBound`, `ExperimentEnvelope` (with `from_dict()`, the strict constructor a JSON-speaking client's envelope arrives through), `EnvelopeVariable`, `StepCost` + `DurationEstimate` (the **duration estimate**'s currency — what a run contributes per point, and the total/phases/assumptions that come back), `ProbeSpec` (the **probe-run** reduction rules — sweep subsampling keeping the extremes, wait caps, averaging caps — written as a standard in its docstring and applied by `BaseProcedure.apply_probe()`), `SETPOINT_PARAM_PREFIX` (the setpoint-parameter convention) | `test_plan.py`, `test_direct_action_path.py`, `test_probe_runs.py` |
| `conditions.py` | The System-Condition standard's pure policy core (origin × severity, scope follows severity): the `Condition`/`Verdict` value objects and the deterministic `decide()` verdict function. Stdlib-only — no other `cryosoft` import, machine-enforced by import-linter contract C13 | `Condition`, `Verdict`, `decide()`, `envelope_conditions()`, `SEVERITIES`, `ORIGINS` | `test_conditions.py` |
| `availability.py` | The Availability standard's pure policy core (GLOSSARY.md's **Availability** / **Availability tag**): a closed tag vocabulary, a declared per-tag policy table, and the deterministic `state_for()`/`decide_availability()` functions that turn a VI's tag set into one of four mutually exclusive states plus a `TagPolicy` verdict. Stdlib-only — no other `cryosoft` import, machine-enforced by import-linter contract C14, mirroring C13's isolation of `conditions.py` above | `AVAILABILITY_TAGS`, `AVAILABILITY_STATES`, `TAG_PRECEDENCE`, `TagPolicy`, `TAG_POLICY`, `Availability`, `state_for()`, `decide_availability()` | `test_availability.py` |
| `exceptions.py` | The exception hierarchy every layer catches by subtype, including the direct action path's typed refusals (each admission check has its own subclass, so a caller tells them apart without parsing prose) | `CryoSoftError`, `CryoSoftCommunicationError`, `CryoSoftSafetyError`, `CryoSoftActionRefusedError`, `CryoSoftPrivateActionError`, `CryoSoftUndeclaredActionError`, `CryoSoftActionScopeError`, `CryoSoftConfigError`, `DataSchemaError` | `test_foundation.py`, `test_direct_action_path.py` |
| `run_builder.py` | The single headless construction path for a run: `build_procedure()` turns a procedure class plus plain values (params, sample info, data directory, prefix, experiment context, and an optional `ProbeSpec` that reduces the built run to a **probe run**) into a ready instance, and `build_operation()` does the same for an operation's `cls(station, **params)` shape — so the queue, the engine port's dict payloads and a headless client all build the same object, and `validate_run()` covers both kinds. Imports no Qt, so a run can be built from a test, a script, or any non-GUI client (the Orchestrator's `submit()` builds a dict-payload run through it); construction refusal is raised, never swallowed, and `PROCEDURE_BUILD_ERRORS` names the one tuple every caller catches. Generic in the class being built (`RunT`) rather than typed against `BaseProcedure`: an import of L4 here would make every importer of the builder an importer of the data manager under contract C5 | `build_procedure()`, `build_operation()`, `PROCEDURE_BUILD_ERRORS`, `RunT` | `test_run_builder.py`, `test_probe_runs.py` |
| `estimates.py` | The **duration-estimate standard**'s pure policy core (GLOSSARY.md's **Duration estimate**): plain functions over declared values — no Station, no Orchestrator, no Qt, no hardware — that combine a run's own per-point cost (`estimate_step_seconds() -> StepCost`) with the setup's nominal ramp rates (`Station.nominal_ramp_rates()`) and the run's `planned_targets()` into a total plus its `setup`/`ramp`/`settle`/`measure` breakdown. Duck-typed on the run so it stays below L4. Every unknown — a VI with no declared rate, a measurement nothing times, a run with no cost model — becomes an explicit assumption, never a silent zero | `estimate_duration()`, `PHASE_SETUP`/`PHASE_RAMP`/`PHASE_SETTLE`/`PHASE_MEASURE` | `test_estimates.py` |
| `ramps.py` | The `RampRecord` payload for `Orchestrator.ramps_updated`/`active_ramps()` — one entry per ramp running right now (label, unit, value, NEXT setpoint, END setpoint, rate, phase, owning run, stoppable + refusal reason, stale) — plus the pure `build_ramp_records()` builder that filters and assembles one `Station.get_ramp_status()` snapshot into it. Dependency-free, like `events.py`, so `orchestrator.py` (emitter) and `gui/ramp_tracker_panel.py` (consumer) can both import it | `RampRecord`, `build_ramp_records()`, `ACTIVE_RAMP_STATUS` | `test_ramps.py`, `test_l3_orchestrator.py` |
| `capability_manifest.py` | The **capability manifest**: the JSON rendering of `Station.station_info()`, resolving each instrument's capabilities into its declared UI groups (declared order, ungrouped items after) and adding no fact of its own. `MANIFEST_SCHEMA` is a real JSON Schema draft 2020-12 document; `validate_manifest()` checks against it with a small structural validator, since `jsonschema` is deliberately not a project dependency. `python -m cryosoft.core.capability_manifest <config_dir>` prints one, building a Station and nothing else — no `QApplication` | `MANIFEST_SCHEMA`, `MANIFEST_SCHEMA_ID`, `build_manifest()`, `validate_manifest()`, `main()` | `test_capability_manifest.py`, `test_conformance.py` |
| `events.py` | The structured `ErrorEvent` payload AND the **control contract** — the frozen, JSON-safe message families the engine and its clients (the GUI today, an agent gateway later) exchange: one `Command` in, exactly one `Verdict` out, consequences as `Event`s, every message naming its `Actor`. Dependency-free, like `ramps.py`, so emitter and consumers can all import it. `Command`/`Verdict` here are the control-contract pair, distinct from `plan.Command` (a VI method call) and `conditions.Verdict` (an enforcement decision) | `ErrorEvent`; `Actor`, `ActorKind`, `OPERATOR`; `Command`, `CommandName` (every public Orchestrator command); `Verdict`, `VerdictCode`; the `Event` union (`StateChange`, `StatusSnapshot`, `StationInfo`, `Readings`, `Datapoint`, `RunStarted`, `RunFinished`, `QueueChanged`) with `event_from_json()`; the **station info** declaration snapshot `StationInfo` and its nested `InstrumentInfo`/`MonitoredInfo`/`ControlInfo`/`GroupInfo`; `to_json()`/`from_json()` on every type | `test_conformance.py`, `test_l2_station.py`, `test_l3_orchestrator.py`, `test_gui.py` |
| `decorators.py` | Marker decorators that tag VI methods for discovery, GUI generation and the capability manifest. Both take the declaration keywords the manifest is built from — `@monitored(unit=, description=, group=)` and `@control(scope=, params=, panel=, group=)` — stored as plain strings/opaque values, since contract C1 forbids this module from importing `plan` or any spec type; the VI base class resolves and type-checks them at class creation | `monitored` / `control` (both bare or parametrized), `get_monitored_methods()`, `get_control_methods()`, `get_monitored_unit()`, `get_monitored_description()`, `get_ui_group()`, `get_control_specs()`, `get_control_panel()`, `get_control_scope()`, `VALID_CONTROL_SCOPES` | `test_foundation.py`, `test_conformance.py` |
| `station.py` | L2 registry: builds VIs from config, polls state with stale-value caching, dispatches ramps and measurement commands, aggregates safety, enforces the capability-scope standard at command dispatch. Owns the ONE unified condition registry (the System-Condition standard, see `conditions.py` and GLOSSARY.md's **System condition**): `get_state()` records a comm-origin hold `Condition` per stale/disconnected VI in the same pass as its existing detection; `update_conditions(safety, tolerated_flags=...)` refreshes every safety-origin `Condition` from a `check_safety()` snapshot the Orchestrator passes in, reading each flag's severity off its producer's `safety_flags` manifest and — for hold-severity flags only — applying `tolerated_flags` and scoping to `get_concerned_vis(flag)`; a critical/advisory flag's condition is built unconditionally, station-wide. `publish_conditions(origin, conditions)` is the public, origin-scoped counterpart usable by any producer outside `Station` itself — today only `trend_check_runner.TrendCheckRunner` (the `"trend"` origin) — reusing the same prune-then-upsert shape as the inline safety block, generalized over `origin` so refreshing one origin never disturbs another's entries; `conditions()` / `active_critical_conditions()` / `acknowledge_condition(key)` are the origin-agnostic read/acknowledge surface; `vi_faults()`/`acknowledge_fault()`/`clear_fault()`/`retry_fault()` are the permanent GUI adapter synthesizing a `FaultRecord` per comm-origin condition, preserving the pre-standard field shape for callers that want it instead of the typed `Condition`. `availability(vi_name)` / `availabilities()` are the Availability standard's single accessor (GLOSSARY.md's **Availability**): a DERIVED VIEW, never a fourth registry, assembled from the offline registry (`_offline_vis`, `OfflineInstrument.tags`), the `not_responding` slice of the same unified condition registry, and each VI's own `is_attached()` | `Station` (`setup_name` — the config directory's name, the setup identity reported by the operational-status record; `get_vi`, `get_vi_names`, `measurement_vi_names`, `switch_vi_names`, `magnet_vi_names`, `measurement_selector_label`, `get_state`, `process_system_targets`, `send_measurement_commands(commands, allowed_scope=...)`, `advance_ramps`, `check_ramps(vi_names=None)`, `stop_ramps(vi_names=None)`, `get_ramp_status`, `check_safety`, `safety_flag_sources`, `get_concerned_vis`, `conditions`, `active_critical_conditions`, `acknowledge_condition`, `update_conditions`, `publish_conditions`, `vi_faults`, `acknowledge_fault`, `clear_fault`, `retry_fault`, `availability`, `availabilities`, `execute_vi_action(vi, method, allowed_scope=..., **kwargs)`, `setpoint_parameters`, `envelope_variables`, `station_info` — the **station info** declaration snapshot, built from declarations and config alone, cached and rebuilt on every connect and disconnect); `LIFECYCLE_ACTIONS`; `FaultRecord`; `build_station()`, `build_station_with_fallback()`, `validate_config_dir()`, `read_instrument_metadata()`, `read_cryogenics_config()`, `read_safety_config()`, `read_trends_config()`, `read_tick_interval_ms()`, `read_servicing_logs_config()` | `test_l2_station.py`, `test_config_validation.py`, `test_operations.py`, `test_helium_fill.py`, `test_l3_orchestrator.py` |
| `orchestrator.py` | L3 single-threaded cooperative state machine; the sole hardware writer; runs the monitor cycle each tick while monitoring is on, and writes the operational-status record on EVERY tick regardless (a quiet tick polls nothing and carries an empty instrument payload), all inside an exception boundary that degrades to ERROR; each tick also feeds `Station.last_state_flat()` to a `TieredTrendLogger` (non-fatal) for the raw/3-min/hourly trend-history tiers. Monitoring is OFF at construction (nothing polled until `start_monitoring()`); `run_procedure()`/`run_operation()` auto-start it, stopping is refused outside IDLE/ERROR, `shutdown()` stops the tick timer. `INITIATION_GATE`/`READING_GATE` states hold the state machine on a procedure's/operation's declared `Gate`s between "targets dispatched" and "take a measurement"; `pause_procedure()` holds the hardware immediately from every state EXCEPT `MEASURING`, where it raises a request that the `SWEEPING` branch honours once `measure()` has saved its datapoint (GLOSSARY.md's **Pause boundary**) — a point being read is never stranded, and resume from there starts at the ramp to the next point; an operation's `postcondition_gates()` are evaluated exactly once, immediately, as the run ends (`Gate.check_once()` — the state snapshot is refreshed first so standby-command effects are visible; unmet gates land in the manifest's `postconditions_unmet`, never blocking). Operations (detected via `command_scope == "operation"`, never imported — keeps contract C5 clean) get queue-jumping priority over procedures and a narrow EMERGENCY-entry carve-out gated by `tolerated_safety_flags`. Claims + admission gate: `_active_claims` is captured from the active run's `claimed_vi_names()` at `_start_run()` and cleared on every teardown path (`_abort_active_procedure()`, `_finish_run()`); `_run_ramp_scope()` is the parallel answer for hardware motion rather than permission (GLOSSARY.md's **Ramp scope**) — the VIs the run has targeted, so it waits for, pauses, and stops only its own ramps, while `Station.advance_ramps()` keeps every ramp moving on every non-PAUSED tick; `_manual_action_admissible(vi_name)` is the single admission predicate shared by `submit_vi_action()` (what may be queued) and the `_tick_body()` drain gate (what may be drained, evaluated per action) — it refuses a VI carrying the Availability standard's `not_responding` tag first (`Station.availability(vi_name)` against `TAG_POLICY["not_responding"].controllable` — GLOSSARY.md's **Instrument fault** / **Availability tag**, no hardcoded origin check), then one with an active safety-origin hold (GLOSSARY.md's **Safety hold**), before the state/claim rules, and refuses EVERY VI once in EMERGENCY unless the manual override is unlocked. The unified tick pipeline (`_tick_body()`, the System-Condition standard): `check_safety()` exactly once, `Station.update_conditions()`, merge in this tick's `envelope_conditions()`, one onset diff over the merged condition-key set (fires fault events for new comm conditions on unwatched VIs, dispatches one-shot `standby()` to every VI a new hold condition affects), one `core.conditions.decide()` call over the merged list, then execute its `Verdict` — `emergency` → `_enter_emergency()` (always a blanket `standby_all()`; critical severity is station-wide by construction, so there is no concerned subset to narrow to), `run_failure` → `_fail_run_for_fault(vi_name, reason=...)` exactly for whichever origin `decide()` found. `_fail_to_error()` is reserved for unknown-blast-radius failures (tick-boundary exceptions, run-setup failures). The **engine port**: `submit(Command) -> request_id` dispatches by `CommandName` onto those same public methods (converting dict payloads into runs via `run_builder.build_procedure()` and the injected `run_catalog`, and into an `ExperimentEnvelope` via its `from_dict()`), answering each with exactly one `Verdict` on `verdict_emitted` — the **verdict standard**, whose full text is the class docstring; every consequence goes out on the one `event_emitted` stream (`StateChange`, `StatusSnapshot`, `StationInfo`, `Readings`, `Datapoint`, `RunStarted`, `RunFinished`), and `QueueChanged`), sharing one monotonic `seq` with the verdicts; the run queue is PULLED, not held — `next_procedure`/`queue_snapshot` are injected callables (see the engine-pull note above) | `Orchestrator` (`submit(Command)`, `start_monitoring`, `stop_monitoring`, `is_monitoring`, `shutdown`, `run_procedure`, `queue_procedure`, `run_operation`, `queue_operation`, `finish_operation`, `confirm_operation`, `run_queue`, `publish_queue`, `pause_procedure`, `pause_pending`, `resume_procedure`, `abort_procedure`, `recover_from_error`, `acknowledge` (unified EMERGENCY/hold-severity override, time-boxed — GLOSSARY.md's **Hold acknowledge**), `submit_vi_action`, `submit_global_action`, `get_operational_status`, `held_vi_names`, `override_active`, `manual_override_expires_at`, `vi_faults`, `acknowledge_fault`, `retry_fault`, `availability`, `availabilities`, `active_ramps`, `stop_ramp`, `emergency_standby(reason)`, `set_experiment_envelope`, `envelope_variables`; every one of them taking a keyword-only `actor` defaulting to the operator sentinel); `MANUAL_ACTION_SCOPE`; `SYSTEM_ACTOR`; the `command` decorator; `OrchestratorState` enum; `monitoring_changed`, `ramps_updated`, `verdict_emitted`, `event_emitted` signals | `test_l3_orchestrator.py`, `test_operations.py` |
| `gates.py` | Generic tick-driven wait primitive: a one-shot action optionally followed by a windowed stability check, declared by a procedure via `initiation_gates()`/`reading_gates()` or by an operation via `initiation_gates()`/`postcondition_gates()`. `step()` is polled each tick while in `INITIATION_GATE`/`READING_GATE`. `postcondition_gates()` is evaluated differently — once, via `check_once()`, as an operation's run ends — no holding, no timeout, because an operation finishes immediately (**Postcondition gate**, GLOSSARY.md). | `Gate` (`step() -> bool`, `check_once() -> bool`) | `test_core_gates.py` |
| `procedure.py` | L4 base classes: the Orchestrator-driven lifecycle and the generic sweep engine | `BaseProcedure` (`initiate` -> `PhasePlan`, `change_sweep_step` -> `StepPlan \| None`, `measure`, `standby` -> `PhasePlan`, `abort` -> `tuple[Command, ...]`, `get_param_groups()` classmethod, `initiation_gates()`/`reading_gates()` -> `tuple[Gate, ...]`, `claimed_vi_names()` -> `set[str] \| None`, `planned_targets()` -> `dict[str, list[float]]` — the pre-dispatch declaration of every system setpoint the run would command, so a queued run can be validated before anything reaches hardware; `{}` by default, derived by `SweepMeasureProcedure` from the same target hooks its plans are built from); `SweepMeasureProcedure` (GUI-selected measurement VI, the reading loop — up to two generic slots of `reading_setters` parameters, switch route and source current alike, per datapoint; each a real `loop_shape` axis 0/1 on every measurement column + HDF5 `loop1_values`/`loop2_values` metadata, `DataSchema` assembly; `axis_data_key()` names the axis column, defaulting to the declared `sweep_axis` so an axis that is not a ramped setpoint — elapsed time — overrides it instead of faking a `SweepAxis`; concrete axes supply six hooks); the two run-economics hooks every procedure inherits — `apply_probe(ProbeSpec)`, which reduces a built run to a **probe run** in place (`run_kind = "probe"`), and `estimate_step_seconds()` -> `StepCost`, the one thing a run contributes to its **duration estimate** (points, setup/settle waits, measurement time), both defaulted on `BaseProcedure` and made real by `SweepMeasureProcedure` from the same hooks the tick loop uses | `test_l4_procedure.py`, `test_new_procedures.py`, `test_time_series_procedure.py`, `test_probe_runs.py`, `test_estimates.py` |
| `operation.py` | L4 base class for cryostat-servicing operations (**Operation**, GLOSSARY.md): the same `PhasePlan`/`StepPlan`/`Gate` currency as a procedure, plus `tolerated_safety_flags`, `command_scope = "operation"`, `postcondition_gates()`, `claimed_vi_names()` (the concurrency-scope hook — the **Claim** standard, GLOSSARY.md), and `run_summary()` (a duck-typed, JSON-safe data hand-off to the session layer via the run manifest's `summary` key, for an operation with no HDF5 file). `measure()`/`change_sweep_step()` are final adapters over `sample()`/`step()` so the Orchestrator drives an operation with the same state machine as a procedure. Also declares the GUI-facing readiness/next-due contract (**Readiness condition** / **Next due** / **config_key**, GLOSSARY.md): `readiness_conditions()`/`next_due()` hooks and `ready_message`/`config_key` class attributes, read only by the Operations panel, never the Orchestrator | `OperationBase` (`initiate`, `step`, `sample`, `standby`, `abort`, `initiation_gates`, `postcondition_gates`, `claimed_vi_names`, `get_progress`, `get_params`, `run_summary`, `request_finish`, `readiness_conditions`, `next_due`), `ReadinessCondition`, `NextDue` | `test_operations.py`, `test_operation_readiness.py` |
| `data_manager.py` | L5 HDF5 file lifecycle for one procedure run: pre-allocated datasets, per-point save, abort trimming. A **Raw diagnostic block**'s dataset is self-describing: `axes` names every dimension in order and, when `measurement_block_labels` declares them, `channel_names` gives the channel-axis column names — both written as HDF5 attributes directly on `/data/<block_name>`, never only in the JSON `data_config` metadata blob. The run's own `run_kind` is written as `/metadata.run_kind` (`run` / `probe` / `operation`), so a **probe run**'s file can never be mistaken for science data | `DataManager` (`save_datapoint`, `close`) | `test_l5_data_manager.py`, `test_probe_runs.py` |
| `data_reader.py` | L5 read-only access to a run's HDF5 file: the analysis sibling of `data_manager.py`, standalone by import-linter contract C17 (only `events`/`exceptions` from the package). Answers the **one vocabulary for live and stored runs** standard above over a finished or in-flight file; reads never pass the written prefix (the `timestamp` column is the point counter, since the writer pre-allocates with NaN and trims only on a clean close) and every statistic is NaN-aware. `read_metadata()`'s `run_kind` is answered from the file's own `/metadata.run_kind` (a pre-`run_kind` file answers `""`); `run_id`/`status`/`reason` stay empty for a file, since only the engine's manifest carries them | `open_run()` -> `RunHandle` (context manager, `n_points`, `list_columns`, `read_slice`, `summary_stats`, `read_metadata`, `mode`); `RunSource`; `ColumnInfo`, `Stats` (frozen, `to_json()`/`from_json()`); `COLUMN_ROLES`, `RUN_METADATA_KEYS`; `summarise_values()`, `loop_axis_column_infos()`, `roles_from_data_config()`, `resolve_slice()`, `column_unit()`; the module-level `list_columns()` / `read_slice()` / `summary_stats()` / `read_metadata()` over any `RunSource` | `test_l5_data_reader.py` |
| `run_buffer.py` | The live half of the **one vocabulary for live and stored runs** standard: an in-memory view of the run in flight, fed by the run's control-contract events and read through the same `ColumnInfo`/`Stats` vocabulary as a finished file. Pure Python — no Qt, no h5py, no Station. `Datapoint.values` is exactly the `measured_data` mapping `DataManager.save_datapoint()` receives and `Datapoint.index` its `sweep_index`, so columns are stored in the writer's own shapes; the buffer adds the `timestamp` column itself, since the writer stamps rather than receives it. Column roles come from the run manifest's `data_config` when it declares one, and are otherwise inferred from the values (a raw block then reads as a measurement — the one thing a buffer reports less precisely than a file) | `RunBuffer` (`start(RunStarted)`, `append(Datapoint)`, `finish(RunFinished)`; `n_points`, `list_columns`, `read_slice`, `summary_stats`, `read_metadata`; `run_id`, `is_running`) | `test_run_buffer.py`, `test_conformance.py` |
| `sweep_builder.py` | Reusable sweep-array construction and the declarative `SweepAxis` used by procedures | `SweepSegment`, `build_piecewise_sweep()`, `load_custom_sweep_csv()`, `apply_hysteresis()`, `SweepAxis`, `sweep_axis_param_specs()`, `build_axis_sweep()` | `test_sweep_builder.py` |
| `operational_status.py` | Pure builder of the per-tick runtime "why is the run slow/stuck" status record, and the written record standard it conforms to (module docstring: field list, types, schema version, and the add-only rule its readers depend on) | `build_operational_status()`, `SCHEMA_VERSION`, `next_sequence_number()`, `RunFaultCode`, `worst_code()`, `VIHealth` | `test_operational_status.py` |
| `stall_detection.py` | Deterministic per-VI ramp-stall detection layered on the status record (RAMP_STALLED); thresholds taken in seconds and converted to a tick count once at construction via the setup's `tick_interval_ms` | `apply_stall_verdict()`, `StallState`, `StallConfig` | `test_stall_detection.py` |
| `config_catalog.py` | Qt-free discovery and versioning of shipped vs user config directories (copy-on-edit fork, named history) | `ConfigCatalog`, `ConfigEntry`, `ConfigVersion` | `test_config_catalog.py` |
| `paths.py` | Resolves machine-local, per-installation directories and settings outside source control: the log directory (`CRYOSOFT_LOG_DIR` env var, then the platform user-data location, then `cryosoft/logs/`) and the fixed measurement root (`CRYOSOFT_MEASUREMENT_ROOT` env var, else the `measurement_root` key in the machine-level `App-config.yaml` settings file, else refuse to start — see GLOSSARY.md's **Measurement root**). `config_directory()`/`data_directory()` for site-specific configs and incident reports are planned, not yet implemented. Stdlib-only, import-linter contract C1 foundation module | `log_directory()`, `measurement_root()` | `test_paths.py` |
| `logging_config.py` | Configures the rotating file + console handlers and four time-rotated JSONL streams — `cryosoft.status` (`status.jsonl`, daily/UTC, 7 backups) and the tiered trend-history streams `cryosoft.trend_raw`/`trend_3min`/`trend_hourly` (`trend_history_{raw,3min,hourly}.jsonl`, daily/daily/weekly, all UTC, 2/8/53 backups); log directory resolution delegates to `paths.log_directory()` | `setup_logging()` | `test_logging_config.py` |
| `tiered_trend_logger.py` | Qt-free live incremental writer for the tiered trend-history store: one throttled raw-tier JSONL line per `record()` call plus two independent bucket accumulators (3-min, 1-hour) flushed to their own JSONL streams on a bucket-boundary crossing. Wired into the Orchestrator tick, called with `Station.last_state_flat()`'s output right after the operational-status record. Two known live/disk asymmetries versus `gui.monitor_history.MonitorHistory` (its docstring has the full writeup): `last_state_flat()` excludes measurement VIs, so those never reach disk; it also has no `bool` guard (`bool` is an `int` subclass), so a boolean `@monitored` field IS coerced to `1.0`/`0.0` and persisted here even though `MonitorHistory.record()` explicitly excludes bools — the opposite direction, gaining a key on disk that the live view never had | `TieredTrendLogger` (`record(flat, timestamp, orch_state=None)`) | `test_tiered_trend_logger.py` |
| `trend_history.py` | Qt-free read/query side of the tiered trend-history store: merges each tier's live + rotated JSONL files (excluding sync-conflict-copy filenames), and turns them into plottable series (GUI) or agent-facing evidence — count-weighted aggregate recombination, `persisted` flag distinguishing "no data in window" from "never persisted" (measurement-VI keys), threshold-crossing detection | `TIERS`, `TierSpec`, `KeySummary`, `pick_tier()`, `persisted_keys()`, `read_tier()`, `read_window()`, `summarize()`, `find_crossings()` | `test_trend_history.py` |
| `trend_checks.py` | The **Trend check** standard's pure policy core: `TrendCheck` declarations evaluated against `trend_history.summarize()` and `read_window()` (never re-deriving mean/std, re-picking a tier, or adding endpoint values to `KeySummary`), a `CheckResult` verdict whose `passed` is `True`/`False`/`None` (`None` = no data in the window, distinct from a definite fail), and the `Condition`-publication adapter for a definite failure. A `Predicate` receives both the window's `{key: KeySummary}` aggregates and its `{key: [(t, value), ...]}` series (`run_check()` computes both once per evaluation), so aggregate-only checks (`sample_temperature_stable`) and rate-of-change checks (`helium_consumption_normal`) share one runner with no per-check branch. `declared_checks()` returns both; `trend_store_live` is deliberately NOT among them — see its own docstring and `cryosoft/troubleshoot/engine.py`'s `check_trend_store_live()`, the pull-only CLI-side sibling. Qt-free, Station-free, import-linter contract C15 | `TrendCheck`, `CheckOutcome`, `CheckResult`, `Predicate`, `WindowedSeries`, `run_check()`, `run_checks()`, `no_data_outcome()`, `to_condition()`, `conditions_for()`, `declared_checks()` | `test_trend_checks.py` |
| `trend_check_runner.py` | The ONE Qt-aware piece of the Trend check standard: a small `QObject` on its own slow timer (independent of the Orchestrator's tick timer) that evaluates `trend_checks.declared_checks()` against the resolved log directory and publishes failing checks via `Station.publish_conditions("trend", ...)`. Holds a `Station` reference only, never an `Orchestrator` | `TrendCheckRunner` (`run_once()`, `stop()`) | `test_trend_check_runner.py` |
