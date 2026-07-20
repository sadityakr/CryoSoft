# cryosoft/session — Session Management (L6)

## Purpose

Manage complete experiments: who is measuring (**User**), which sample under
which per-experiment safety bounds (**ExperimentRecord** + **session
envelope**), and which runs were produced (**RunRecord**, recorded
automatically from the Orchestrator's run manifests). This is the layer the
eLab publishing track (`session/eln/`, planned) and the Agent Gateway
(`session/gateway/`, planned) will build on — see
`docs/plans/session-management-layer.md` and
`docs/plans/agent-native-architecture.md`.

Also hosts the **Servicing Log** framework (`servicing_log.py`): per-setup,
typed, human-editable logs of servicing events (**log kind**, e.g. the
**cryogenics log**), the machine-recorded **helium record**, and the
`CryogenicsRecorder` automatic writer — independent of experiments, what
technical staff consult and maintain. See
`docs/plans/cryogenics-logbook.md` §3/§6 and the **Servicing log** / **Log
kind** / **Cryogenics log** / **Entry revision** / **Helium record** entries
in `GLOSSARY.md`.

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
  `states_updated` (full station state, polled into `CryogenicsRecorder`).
- GUI lifecycle calls on the `SessionManager`: `start_experiment`,
  `close_experiment`, `set_findings`, `set_attended`, `set_queue`,
  `switch_experiment`.
- The active config identity (from `main.py`) and the station's cached state
  (settings snapshot at each run start).
- Servicing-log writes: `ServicingLogStore.add_entry`/`revise_entry`/
  `delete_entry` (manual, from the GUI's add/edit dialogs) and
  `append_machine_entry` (machine-only kinds, e.g. `CryogenicsRecorder`'s
  `"operations"` stream).

## Exit (what goes out)

- Persisted records: `<data_dir>/experiments/<experiment_id>/experiment.json`
  (+ the `active.json` resume pointer), and the setup-local `users.json`
  roster next to the app settings. Each experiment folder also holds
  `gui_state.json` (GUI-authored, opaque to this layer) and a `data/` folder
  where the experiment's HDF5 files live (sub-folders allowed, e.g.
  `data/heating_runs/`) — see **Format rules** below.
- `Orchestrator.set_session_envelope()` — the experiment's sample bounds,
  enforced in the Orchestrator for every writer.
- `experiment_context()` — the dict the GUI passes as `experiment_info` when
  constructing procedures, stamped into every HDF5 file's
  `/metadata/experiment_info`.
- Signals for the GUI: `experiment_changed(dict)`, `run_recorded(dict)`,
  `store_health_changed(dict)` (`{"ok": bool, "detail": str}` — a save
  failure/recovery, emitted once per transition), `CryogenicsRecorder.cryo_warning(str)`.
- Servicing-log storage: `<store root>/<config_name>/<kind>.jsonl` (one file
  per declared log kind) and `<store root>/<config_name>/helium_record.jsonl`.

## Format rules

These shape every file this layer writes; a change to any of them is a
file-format change, not a routine edit.

- **`schema_version`.** `experiment.json`, `gui_state.json`, and
  `active.json` all carry a top-level `"schema_version"` int
  (`models.SCHEMA_VERSION`, currently `1`), written on every save. Absent on
  disk (an old file) is treated as version `1`. On load, a value *greater*
  than the running app's `SCHEMA_VERSION` logs a WARNING; the record still
  loads (tolerant-parse — one bad field must never brick the app), but it is
  never written back: `SessionManager.switch_experiment()` refuses to make
  such a record the live experiment, and `_save_current()` refuses to
  overwrite one even if it somehow became `self._experiment` (belt and
  suspenders). This is what makes "a newer app wrote this" and "an older app
  read this" mutually safe.
- **Bundle-relative data paths.** `RunRecord.data_file` is stored relative to
  the experiment's session folder whenever the file lives under it (normally
  inside `data/`, sub-folders included, e.g. `data/heating_runs/xyz.h5`) —
  `ExperimentStore.relativize_data_file()` does this before
  `SessionManager` records a `run_started` manifest. A file saved
  deliberately outside the session folder is stored as an absolute path,
  unchanged.
- **Resolution order** (`ExperimentStore.resolve_data_file()`, the read
  side): a relative stored path joins the session folder; an absolute path
  is used as-is if it still exists; a *dangling* absolute path (an older
  record whose whole session folder was later moved or copied) falls back to
  a recursive basename search under `<experiment_id>/data/`; if nothing
  matches, the original path is returned unchanged. This is what makes
  "copy or move the experiment folder elsewhere and it still opens" true.
- **`queue`** on `ExperimentRecord` is GUI-authored, opaque JSON — a list of
  dicts whose shape only `gui.form_autosave.QueueItemState` (planned) knows.
  The session layer stores and round-trips it via `SessionManager.set_queue()`
  but never inspects or interprets its contents (contract C11: this package
  never imports `cryosoft.gui`).

## Interface contract

- **Single writer.** All experiment-record mutations go through
  `SessionManager` — the GUI (and the future Agent Gateway) call its methods
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
- **Qt-widget-free.** `SessionManager`/`CryogenicsRecorder` are `QObject`s
  (signals only); the package never imports Qt widgets or `cryosoft.gui`
  (contract C11).
- **Entry-revision model** (`servicing_log.py`): `ServicingLogStore` is
  append-only — `add_entry`/`revise_entry`/`delete_entry` always append a new
  `ServiceLogEntry` sharing the earlier one's `entry_id`, never rewrite an
  existing line. `entries()` returns only the latest, non-deleted revision
  per `entry_id`; `revisions()` returns the full history. Writes are
  validated/coerced against the log kind's `ParamSpec` fields (unknown field
  or wrong type → `ValueError`); reads tolerate a corrupt line (skipped with
  a WARNING, never raised) — same discipline as `store.py`. A kind with
  `editable=False` (e.g. `"operations"`) refuses `add_entry`/`revise_entry`/
  `delete_entry`; only `append_machine_entry` may write it.
- **Log kinds are declarations.** Adding a servicing log for a new setup is
  one `LogKindSpec` in `DECLARED_LOG_KINDS`, never new store or GUI code —
  see `LogKindSpec`'s docstring and `docs/plans/cryogenics-logbook.md` §6.1.

## How to add a new module

1. Keep the dependency direction: session modules may import `core.*` and
   each other, never `gui`/`main`/drivers/VIs/procedures (C11 will fail the
   build otherwise).
2. New persisted state = a new tolerant-parse dataclass in `models.py` (it is
   covered by conformance automatically) + store methods following the atomic
   write/tolerant read pattern.
3. New behavior needs its own tests in `tests/test_session_layer.py`;
   conformance coverage is necessary but not sufficient.
4. The planned sub-packages live here too: `session/eln/` (ELN adapters —
   every real adapter with a `sim_` twin) and `session/gateway/` (agent MCP
   API). Follow their plans in `docs/plans/`.
5. **New servicing-log kind:** add one `LogKindSpec` to `DECLARED_LOG_KINDS`
   in `servicing_log.py` (fields as `ParamSpec`s, every one with a usable
   default) — storage, revision handling, and (once Phase 5 lands) the GUI
   table/add/edit dialogs all follow automatically. Covered immediately by
   the `test_log_kind_spec_is_valid` conformance test in
   `tests/test_conformance.py`; add behavior tests to
   `tests/test_servicing_log.py`.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `models.py` | Tolerant-parse records: users, runs, experiments (incl. `queue` and `schema_version`), ELN links, servicing-log entries; envelope (de)serialisation. | `SCHEMA_VERSION`, `User`, `RunRecord`, `ExperimentRecord`, `ElnLink`, `ServiceLogEntry`, `envelope_to_dict`, `envelope_from_dict` | `tests/test_session_layer.py` / `tests/test_servicing_log.py` + conformance |
| `store.py` | Disk persistence: per-experiment folders (`experiment.json`, `gui_state.json`, `data/`) + active pointer; user roster; bundle-relative data-path (de)resolution. | `ExperimentStore` (`list_experiments`, `load`, `save`, `get_active`, `set_active`, `make_experiment_id`, `data_dir`, `gui_state_path`, `relativize_data_file`, `resolve_data_file`), `UserRoster` (`list_users`, `get`, `add`) | `tests/test_session_layer.py` |
| `manager.py` | The L6 façade: experiment lifecycle (incl. switching between open experiments and the run queue), automatic run recording from manifests, envelope installation, HDF5 context, save-health surfacing. | `SessionManager` (`start_experiment`, `close_experiment`, `set_findings`, `set_attended`, `set_queue`, `switch_experiment`, `current_data_dir`, `current_gui_state_path`, `experiment_context`, `current_experiment`; signals `experiment_changed`, `run_recorded`, `store_health_changed`) | `tests/test_session_layer.py` |
| `servicing_log.py` | The Servicing Log framework: declared log kinds, revisioned per-kind storage, the hourly helium record, consumption fit, and the automatic recorder. | `LogKindSpec`, `DECLARED_LOG_KINDS`, `ServicingLogStore` (`add_entry`, `revise_entry`, `delete_entry`, `append_machine_entry`, `entries`, `revisions`), `HeliumRecordStore` (`append`, `samples`), `consumption_rate_pct_per_h`, `CryogenicsRecorder` (`on_states_updated`, `on_run_started`, `on_run_finished`; signal `cryo_warning`) | `tests/test_servicing_log.py` + conformance |
