# cryosoft/session — Session Management (L6)

## Purpose

Manage complete experiments: who is measuring (**User**), which sample under
which per-experiment safety bounds (**ExperimentRecord** + **session
envelope**), and which runs were produced (**RunRecord**, recorded
automatically from the Orchestrator's run manifests). This is the layer the
eLab publishing track (`session/eln/` — see its own README) and the **Agent
gateway** (`session/gateway/` — see its own README) build on. The gateway is
also what the **MCP adapter** (`cryosoft/mcp/`, a SEPARATE process with its
own README) reaches, over the socket the **Gateway server** publishes: an
external agent session therefore sees this layer's tool surface and is bound
by its permission model without holding any of it.

Also hosts the **maintenance log** framework (`maintenance_log.py`):
per-setup, typed, human-editable logs of maintenance events (**log kind**),
independent of experiments — what technical staff consult and maintain. See
the **Maintenance log** / **Log kind** / **Entry revision** entries in
`GLOSSARY.md`.

Also hosts the **run queue** (`run_queue.py`): the ordered list of runs
waiting to start, held as immutable **run specs** rather than as live
procedure objects, together with the **run validation** every spec passes
before it may enter it. Headless and Qt-free — no widget, no Orchestrator —
so a queue can be built, ordered and validated in a test, a script, or an
agent gateway with no GUI at all. A spec may also carry a `probe_spec`: the
same run reduced to a **probe run** (a few cheap points, `run_kind =
"probe"`), validated and queued as the reduced run, so what waits in the
queue is the probe itself. See the **Run queue** / **Run spec** /
**Run validation** / **Probe run** entries in `GLOSSARY.md`.

Also hosts the **Agent feed** (`agent_feed.py`): one experiment's
append-only trail of everything a non-operator **Actor** asked for and was
told, written to `agent_actions.jsonl` inside the experiment folder so the
folder stays the complete, portable record. See the **Agent feed** entry in
`GLOSSARY.md` for the record standard.

Also hosts the **Agent gateway** (`gateway/`): the second adapter of the
**Control contract**, letting an autonomous client submit the same
`Command`s the GUI does under a declared **Role**, with every action's
**Action class** decided by a declarative table and the human's
**Attendance** and **Kill switch** as inputs. In-process only — no network,
no thread. It also publishes that contract as a **Tool surface**: one
**Tool spec** per action, rendered from `CommandName` and the station's
declaration snapshot rather than hand-written, so a capability's units,
choices and configured bounds arrive as JSON Schema and an out-of-range
argument is refused before a `Command` is built. `Gateway.call_tool()` is
the single entry point and answers every call, routing a command tool
through `submit()` and a session tool — the reads over the experiment
store, the run files, the operational log and the agent action feed — to
its function. See `session/gateway/README.md` and the **Role** / **Action
class** / **Agent gateway** / **Tool surface** / **Tool spec** entries in
`GLOSSARY.md`.

Not to be confused with `gui/form_autosave.py` (historically "the session
model"): that is form persistence; this layer is experiment management.

## Architecture layer

**L6 — between core and the GUI.** Imported by `cryosoft.gui` and
`cryosoft.main`; imports the Orchestrator, Station, and `core.plan` downward.
Machine-enforced by import-linter contracts **C11** (session never imports
gui/main/drivers/VIs/procedures/troubleshoot) and **C12** (nothing below the
GUI imports session).

## Entry (what comes in)

- Orchestrator signals: `run_started` / `run_finished` manifests (run id,
  procedure, kind, params, data file path, timestamps, terminal status), and
  `states_updated` (the full station state each monitored tick).
- The Orchestrator's `event_emitted` stream, for the one fact the manifests
  do not carry: `RunStarted.actor`, stamped onto the `RunRecord` the
  manifest just opened (the manifest signal is emitted first, so the record
  is already there).
- GUI lifecycle calls on the `ExperimentManager`: `start_experiment`,
  `close_experiment`, `set_findings`, `set_attended`, `set_queue`,
  `switch_experiment`.
- Run-queue calls on the `ExperimentManager`: `queue_run`, `dequeue_run`,
  `move_queued_run`, `clear_run_queue`, `validate_run` — and `next_run()`,
  which is not a GUI call at all but the **pull seam** the Orchestrator asks
  through (`main.py` wires it to `Orchestrator.next_procedure`, and
  `queue_entries()` to `Orchestrator.queue_snapshot`).
- The active config identity (from `main.py`).
- Confirmed ELN entry references from `session/eln/`'s publisher, via
  `ExperimentManager.set_run_eln_link()` — the single write path for the
  publishing track, which never edits a record itself.
- A **Pending entry** waiting for a human, parked on its run record by
  `ExperimentManager.set_pending_eln_draft()` until somebody calls
  `approve_eln_draft()` — which hands it to the publisher attached by
  `attach_eln_publisher()` — or `discard_pending_eln_draft()`. Approval is a
  decision about a record, so it is taken here; queuing is the publisher's, so
  it is delegated back. The entry may be a **draft entry** an agent wrote for
  an ATTENDED experiment, an **analysed entry** the analysis stage produced,
  or the facts-only fallback parked when a recipe failed.
- An **Analysis report** for a finished run, read back from
  `<experiment>/analysis/<run_id>/report.json` by `analysis_runner.py` after
  its worker process exits, and handed straight to the publisher. The report
  is data written by another process; nothing in this layer runs a recipe.
- Maintenance-log writes: `MaintenanceLogStore.add_entry`/`revise_entry`/
  `delete_entry`, from the GUI's add/edit dialogs.

## Exit (what goes out)

- Persisted records: one `Session` tier above the experiment tier —
  `<measurement_root>/sessions/<user_id>/<session_id>/session.json` (+ one
  machine-wide `active.json` resume pointer at `sessions/active.json`,
  tracking the active *(user_id, session_id)* pair) via `SessionStore` —
  nested under the owner's `user_id` so ownership is structural (a
  directory), not just a field inside `session.json` that has to be read to
  be known. One level deeper inside each session folder, the experiment
  tier is unchanged in shape:
  `<measurement_root>/sessions/<user_id>/<session_id>/<experiment_id>/experiment.json`
  (+ `ExperimentStore`'s own `active.json`, tracking that session's active
  *experiment* — a distinct file from `SessionStore`'s). `measurement_root` is
  `cryosoft.core.paths.measurement_root()` — a fixed, machine-level, admin-set
  location (see that module's docstring), decoupled from any GUI form field.
  `users.json` lives directly under `measurement_root` and always has a fixed
  `GUEST_USER_ID`/`GUEST_USER_NAME` roster entry, auto-registered at every
  startup (`cryosoft.main._ensure_guest_user_registered`) — the identity used
  whenever nobody has logged in, so sessions always have a real owner
  directory to nest under. Each experiment folder also holds `gui_state.json`
  (GUI-authored, opaque to this layer) and a `data/` folder where the
  experiment's HDF5 files live (sub-folders allowed, e.g.
  `data/heating_runs/`) — see **Format rules** below.
- `Session.experiments` — this session's index of experiment folders (title,
  owner, status, timestamps), so a session answers "what experiments do I
  contain and where" from `session.json` alone, without a directory scan or
  opening every `experiment.json` — for whatever reads it. It is kept
  accurate by `ExperimentManager._reconcile_session_index()`, which DOES do
  that scan (rebuilding the list from `ExperimentStore.list_experiments()`
  plus every `experiment.json` found), on `start_experiment()`/
  `close_experiment()`/`switch_experiment()` — the three points an
  experiment becomes or stops being the one in view. Because it rebuilds
  rather than patches one entry, an experiment folder moved into or out of a
  session by hand (e.g. handed off to a different user's session to
  continue the project) is picked up or dropped the next time any
  experiment in that session opens or closes — no separate "move" operation
  is needed. Each entry's `user_id` is copied verbatim from its
  `ExperimentRecord`, never re-derived from which session it currently sits
  in, so a move never rewrites who actually ran the experiment.
- `Orchestrator.set_experiment_envelope()` — the experiment's sample bounds,
  enforced in the Orchestrator for every writer (a procedure's `Target`s and,
  equally, a manual action on the **direct action path**).
- `Orchestrator.set_attendance()` — the record's **Attendance** flag, pushed
  down as a value for the same reason the envelope is (contract C12 stops the
  enforcement point from reading this layer's record). Every path that makes
  a record live installs it alongside the envelope — `start_experiment()`,
  `switch_experiment()` and the resume on construction — and `set_attended()`
  pushes each later change, so the record and the engine can never drift.
  `set_attended()` therefore writes BOTH homes of the one fact, which is why
  the GUI's takeover strip calls it rather than pushing the value down
  itself.
- The run queue, and its broadcasts: `queue_snapshot()` / `queue_entries()`
  are what a client renders from, and every mutation asks
  `Orchestrator.publish_queue(actor=...)` for a `QueueChanged` on the engine's
  one event stream rather than adding a second channel of this layer's own.
  `next_run()` pops one **run spec** and constructs the single live object it
  describes, stamped with the experiment context read at BUILD time — a run
  queued before an experiment was opened still belongs to the one open when
  it actually starts.
- `set_experiment_envelope()` — the write side for an experiment that is
  already open (the Start Experiment dialog's envelope goes in through
  `start_experiment()` instead). Writes both homes of the value, the record
  and the engine, and returns the engine command's request id so the caller
  can match the answering **Verdict** to its own request.
- `envelope_variables()` — the matching READ side, a passthrough to
  `Orchestrator.envelope_variables()`: per VI, the capability that commands
  its enveloped quantity and the setup's own bounds on it. The Start
  Experiment dialog pre-fills its envelope editor from these, so the operator
  narrows the setup's limits instead of composing an envelope from nothing.
- `experiment_context()` — the dict the GUI passes as `experiment_info` when
  constructing procedures, stamped into every HDF5 file's
  `/metadata/experiment_info`.
- Signals for the GUI: `experiment_changed(dict)`, `run_recorded(dict)`,
  `store_health_changed(dict)` (`{"ok": bool, "detail": str}` — a save
  failure/recovery, emitted once per transition).
- Maintenance-log storage: `<store root>/<config_name>/<kind>.jsonl`, one
  file per declared log kind.
- The **Agent feed**, `<experiment_id>/agent_actions.jsonl`
  (`ExperimentStore.agent_feed_path()`) — one line per command a
  non-operator actor submitted, per verdict answering one, per `StateChange`
  an agent caused (all joined by `request_id`), and per call of a tool that
  declares `ToolSpec.recorded`, which is how a session tool answered inside
  the client — and what it spent on model tokens — is recorded at all. The `Gateway`
  writes the command records (it alone still holds the arguments); the feed
  itself reads verdicts and events off the engine's streams
  (`AgentFeed.attach()`), so an agent that skips the gateway is still
  recorded.
- The ELN publish journal, `<experiment_id>/outbox.jsonl`
  (`ExperimentStore.outbox_path()`) — written by `session/eln/`'s **Outbox**,
  inside the experiment folder so a copied experiment carries its
  unpublished runs with it. See `session/eln/README.md`.

## Format rules

These shape every file this layer writes; a change to any of them is a
file-format change, not a routine edit.

- **`schema_version`.** `experiment.json`, `session.json`, `gui_state.json`,
  and both `active.json` files all carry a top-level `"schema_version"` int
  (`models.SCHEMA_VERSION`, currently `2` — bumped from `1` when
  `Session.experiments` was added, since an older app's `Session.to_dict()`
  would otherwise silently drop that field on resave), written on every
  save. Absent on disk (an old file) is treated as version `1`. On load, a
  value *greater* than the running app's `SCHEMA_VERSION` logs a WARNING;
  the record still loads (tolerant-parse — one bad field must never brick
  the app), but it is never written back: `ExperimentManager.switch_experiment()`
  refuses to make such a record the live experiment, and `_save_current()`
  refuses to overwrite one even if it somehow became `self._experiment`
  (belt and suspenders). This is what makes "a newer app wrote this" and "an
  older app read this" mutually safe.
- **`SessionStore`'s `active.json`** carries `active_user_id`/
  `active_session_id` (both required to locate a session's nested path). A
  pointer written before per-user nesting (`{"active": "..."}`) has neither
  key, so `get_active()` tolerantly returns `None` — the same "unset
  pointer" bootstrap path as a first-ever launch, not a crash.
- **`Session.experiments` is flat-only.** Each entry's `experiment_id` is a
  single path segment directly under the session folder — no nested
  subfolders. Since reconciliation rebuilds the whole list from the folder
  rather than patching one entry, a session that already had experiments on
  disk before this index existed is NOT stuck with an empty/partial index
  forever — the next `start_experiment()`/`close_experiment()`/
  `switch_experiment()` in that session backfills every experiment folder
  already there.
- **Every record names its actor, and says when it had to guess.**
  `RunRecord.actor` is the `Actor` of the `RunStarted` event that opened the
  run — not of the manifest, which says only what ran — so an agent-started
  run stays distinguishable from the physicist's after the process is gone.
  A record with no readable actor (every run written before actors were
  stamped) loads as the `OPERATOR` sentinel with `actor_legacy` set: "old
  file" must never read as "the physicist did it". Queue entries carry the
  same fact through `RunSpec.actor`, which `queue_entries()` and every
  `QueueChanged` render verbatim.
- **Every record fixes what it was started with.** `RunRecord.params_digest`
  is the **Params digest** (`core.plan.params_digest()`) of the run's params,
  stamped from the manifest when the run opens and stored rather than
  recomputed on read — so it says what the run actually started with even if
  the record is later amended. A record written before digests were stamped carries an
  empty string, never a digest invented on read.
- **Bundle-relative data paths.** `RunRecord.data_file` is stored relative to
  the experiment's session folder whenever the file lives under it (normally
  inside `data/`, sub-folders included, e.g. `data/heating_runs/xyz.h5`) —
  `ExperimentStore.relativize_data_file()` does this before
  `ExperimentManager` records a `run_started` manifest. A file saved
  deliberately outside the session folder is stored as an absolute path,
  unchanged.
- **Resolution order** (`ExperimentStore.resolve_data_file()`, the read
  side): a relative stored path joins the session folder; an absolute path
  is used as-is if it still exists; a *dangling* absolute path (an older
  record whose whole session folder was later moved or copied) falls back to
  a recursive basename search under `<experiment_id>/data/`; if nothing
  matches, the original path is returned unchanged. This is what makes
  "copy or move the experiment folder elsewhere and it still opens" true.
- **The agent feed is a journal of facts, not of entities.** Every line in
  `agent_actions.jsonl` is a distinct thing that happened, ordered by a
  per-file `seq` that continues where an earlier process left off; nothing
  supersedes an earlier line, so the last-line-wins rule the **Outbox** and
  the maintenance log follow does NOT apply here. Every record carries every
  key of the standard, `null` where one does not apply — a reader must never
  have to guess whether an absent key means "no" or "old file". A corrupt
  line is skipped with a WARNING, as everywhere else in this layer.
- **`queue`** on `ExperimentRecord` is GUI-authored, opaque JSON — a list of
  dicts whose shape only `gui.form_autosave.QueueItemState` (planned) knows.
  The session layer stores and round-trips it via `ExperimentManager.set_queue()`
  but never inspects or interprets its contents (contract C11: this package
  never imports `cryosoft.gui`).

## Interface contract

- **The queue is data, and it is validated on the way in.** Nothing waiting
  in the run queue holds a live procedure object, and nothing enters it
  without passing `validate_run()` — so a queued run is never known-unrunnable
  and never holds a data file or a claim on instruments. Validation builds
  the run through `run_builder.build_procedure()` headlessly and throws it
  away. A `probe_spec` is applied before those checks, so a probe is
  validated as what would actually run — its reduced targets, its estimate. The engine PULLS: it
  asks `next_run()` when it is ready, and keeps sole authority over *when* a
  run starts. See `run_queue.py`'s module docstring for why the opposite
  direction was rejected.
- **Single writer.** All experiment-record mutations go through
  `ExperimentManager` — the GUI (and the future Agent Gateway) call its methods
  and render its signals, never editing records or files directly. Exactly
  the Orchestrator's single-writer principle, one level up.
- **Tolerant-parse models.** Every record in `models.py` is a plain
  dataclass with `to_dict()`/`from_dict()`: JSON-safe, missing keys take
  defaults, unknown keys are ignored, `from_dict()` never raises on junk, and
  every model constructs from defaults alone. Machine-checked by the
  session-model conformance tests in `tests/test_conformance.py`.
- **Disk discipline** (`store.py`): atomic writes (`.tmp` + `os.replace`),
  tolerant loads, and lazy directory creation — nothing is created on disk
  until something is saved.
- **Qt-widget-free.** `ExperimentManager` is a `QObject` (signals only); the
  package never imports Qt widgets or `cryosoft.gui` (contract C11).
- **Entry-revision model** (`maintenance_log.py`): `MaintenanceLogStore` is
  append-only — `add_entry`/`revise_entry`/`delete_entry` always append a new
  `MaintenanceLogEntry` sharing the earlier one's `entry_id`, never rewrite an
  existing line. `entries()` returns only the latest, non-deleted revision
  per `entry_id`; `revisions()` returns the full history. Writes are
  validated/coerced against the log kind's `ParamSpec` fields (unknown field
  or wrong type → `ValueError`); reads tolerate a corrupt line (skipped with
  a WARNING, never raised) — same discipline as `store.py`. A kind declared
  `editable=False` refuses every write.
- **Log kinds are declarations.** Adding a log for a new setup is
  one `LogKindSpec` in `DECLARED_LOG_KINDS`, never new store or GUI code —
  see `LogKindSpec`'s docstring. Adding a field to an existing kind is
  backward-compatible by construction: the store never rewrites an existing
  line, so an old entry simply lacks the new key — readers must use
  `.get(field, default)`, never index it directly.

## How to add a new module

1. Keep the dependency direction: session modules may import `core.*` and
   each other, never `gui`/`main`/drivers/VIs/procedures (C11 will fail the
   build otherwise).
2. New persisted state = a new tolerant-parse dataclass in `models.py` (it is
   covered by conformance automatically) + store methods following the atomic
   write/tolerant read pattern.
3. New behavior needs its own tests in `tests/test_session_layer.py`;
   conformance coverage is necessary but not sufficient.
4. The sub-packages live here too: `session/eln/` (the ELN adapter standard,
   the eLabFTW backend, the **Outbox**, and the publisher — see
   `session/eln/README.md`, which owns its own rules) and `session/gateway/`
   (the **Agent gateway**: **Role**s, **Action class**es and the permission
   matrix — see `session/gateway/README.md`, which owns its own rules).
5. **New maintenance-log kind:** add one `LogKindSpec` to
   `DECLARED_LOG_KINDS` in `maintenance_log.py` (fields as `ParamSpec`s,
   every one with a usable default) — storage and revision handling follow
   automatically. Covered immediately by the `test_log_kind_spec_is_valid`
   conformance test in `tests/test_conformance.py`; add behavior tests to
   `tests/test_maintenance_log.py`.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `models.py` | Tolerant-parse records: users, sessions (the L6 tier above an experiment, incl. its `experiments` index), runs (incl. the per-run `eln_link` the publisher stamps, the `pending_eln_draft` an unapproved **draft entry** waits in, the `actor`/`actor_legacy` pair naming who started it, and the `params_digest` fixing what it started with), experiments (incl. `queue` and `schema_version`), ELN links, maintenance-log entries; envelope (de)serialisation. | `SCHEMA_VERSION`, `GUEST_USER_ID`, `GUEST_USER_NAME`, `User`, `Session`, `ExperimentIndexEntry`, `RunRecord`, `ExperimentRecord`, `ElnLink`, `MaintenanceLogEntry`, `envelope_to_dict`, `envelope_from_dict` | `tests/test_session_layer.py` / `tests/test_servicing_log.py` + conformance |
| `store.py` | Disk persistence: per-user, per-session folders (`session.json` + machine-wide active pointer) via `SessionStore`, one level above per-experiment folders (`experiment.json`, `gui_state.json`, `data/`, `analysis/` — recipes and one report folder per run) + their own active pointer via `ExperimentStore`; user roster; bundle-relative data-path (de)resolution. | `SessionStore` (`list_sessions(user_id)`, `create_session`, `load(user_id, session_id)`, `save`, `get_active` → `tuple[str, str] \| None`, `set_active(user_id, session_id)`, `make_session_id`), `ExperimentStore` (`list_experiments`, `load`, `save`, `get_active`, `set_active`, `make_experiment_id`, `data_dir`, `gui_state_path`, `outbox_path`, `agent_feed_path`, `analysis_dir`, `recipes_dir`, `report_dir`, `relativize_data_file`, `resolve_data_file`), `UserRoster` (`list_users`, `get`, `add`) | `tests/test_session_layer.py` |
| `manager.py` | The L6 façade: experiment lifecycle (incl. switching between open experiments, the run queue, and a chosen experiment folder name), automatic run recording from manifests, envelope and attendance installation (both session-owned policy values, pushed down into the engine wherever a record becomes live and on every later change), HDF5 context, save-health surfacing, session experiment-index reconciliation, the single write path for published ELN links, the drafting approval gate, and the run queue (validated adds, ordered mutations, and the engine's pull seam). | `ExperimentManager` (`start_experiment(..., envelope=None, experiment_dirname=None)`, `close_experiment`, `set_findings`, `set_attended`, `set_queue`, `switch_experiment`, `current_data_dir`, `current_gui_state_path`, `experiment_context`, `envelope_variables`, `set_experiment_envelope`, `current_experiment`, `set_run_eln_link`, `attach_eln_publisher`, `set_pending_eln_draft`, `pending_eln_draft`, `approve_eln_draft`, `discard_pending_eln_draft`, `run_queue`, `queue_snapshot`, `queue_entries`, `validate_run`, `queue_run`, `dequeue_run`, `move_queued_run`, `clear_run_queue`, `next_run`; optional `session_store`/`station`/`run_catalog` constructor args; signals `experiment_changed`, `run_recorded`, `store_health_changed`) | `tests/test_session_layer.py` |
| `run_queue.py` | The run queue as data: immutable **run specs**, their ordering, the one construction path from a spec to the live object the engine starts (`core.run_builder`'s `build_procedure()`, with a spec's optional `probe_spec` reducing the built run to a **probe run**), and the add-time **run validation** (declared `ParamSpec` bounds, the headless build, `control_limits` + the **session envelope**, plus the **duration estimate**). Imports no Qt, no Orchestrator, and no `cryosoft.procedures` — the classes a spec names are resolved through an injected run catalog. | `RunSpec`, `RunQueue` (`add`, `remove`, `move`, `clear`, `snapshot`, `entries`, `pop_next`, `find`), `RunFinding`, `RunValidation`, `build_run`, `validate_run`, `KIND_PROCEDURE`, the `FINDING_*` codes | `tests/test_run_queue.py` |
| `analysis_runner.py` | The analysis stage's client half: builds an `AnalysisSpec` from one run record plus the experiment context and the settings, writes it into `<experiment>/analysis/<run_id>/`, runs `python -m cryosoft.analysis run --spec …` as a bounded `QProcess` (one at a time, FIFO queue, QTimer timeout, no blocking wait), and hands the report back to the ELN publisher — an **analysed entry** when it ran, the facts fallback when it did not. | `AnalysisRunner` (`start`, `cancel`, `is_running`, `recipe_dirs`; signals `analysis_started`, `analysis_finished`, `analysis_failed`) | `tests/test_analysis_runner.py` |
| `agent_feed.py` | The **Agent feed**: one experiment's append-only trail of every command a non-operator actor submitted, every verdict answering one, every agent-caused `StateChange`, and every call of a tool declaring `ToolSpec.recorded` (with its cost line) — joined by `request_id`, tolerant on read, and never able to raise into the engine's emit path. | `AgentFeed` (`attach`, `record_command`, `record_verdict`, `record_event`, `record_tool_call`, `set_run_id`, `path`, `run_id`, `experiment_id`), `read_feed`, `SCHEMA_VERSION`, `RECORD_COMMAND`, `RECORD_VERDICT`, `RECORD_EVENT`, `RECORD_TOOL` | `tests/test_agent_feed.py` |
| `gateway/` | The **Agent gateway** (sub-package, own README): the permission model in front of the control contract — `Role` × `ActionClass` as one `PERMISSION_MATRIX`, the PROVISIONAL per-command and per-capability classification tables, `authorize()`, and the in-process `Gateway` client that stamps an agent identity onto every command and answers a refusal on the engine's own verdict stream. Also renders the **Tool surface** from `CommandName` and the station declaration, answers `call_tool()` for every call, and — through the **Gateway server** — carries that same client to another PROCESS over a local socket, one `Gateway` per connection, without a thread. | `Gateway`, `EngineClient`, `GatewayServer`, `Role`, `Permission`, `PERMISSION_MATRIX`, `authorize`, `role_within_ceiling`, `ActionClass`, `ClassifiedAction`, `UnclassifiedActionError`, `COMMAND_ACTION_CLASSES`, `CONTROL_ACTION_CLASSES`, `LIFECYCLE_ACTION_CLASSES`, `classify_command`, `classify_control`, `ToolSpec`, `ToolContext`, `ToolError`, `SESSION_TOOLS`, `render_tools`, `capability_tool_name`, `validate_tool_args` | `tests/test_gateway.py`, `tests/test_gateway_tools.py`, `tests/test_gateway_server.py` + conformance |
| `maintenance_log.py` | The maintenance-log framework: declared log kinds (`maintenance` ships) and revisioned per-kind storage. | `LogKindSpec`, `DECLARED_LOG_KINDS`, `MaintenanceLogStore` (`add_entry`, `revise_entry`, `delete_entry`, `entries`, `revisions`) | `tests/test_maintenance_log.py` + conformance |
