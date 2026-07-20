# Unify GUI form-autosave into the L6 Experiment/Session record

## Context

Today CryoSoft splits "session" content across three disconnected places:

1. **L6 `ExperimentRecord`** (`cryosoft/session/`) — title, user, sample info,
   runs, findings. Already a named, JSON, one-folder-per-experiment record
   (`<data_dir>/experiments/<experiment_id>/experiment.json`), with a working
   switchable "active" pointer and automatic crash-resume
   (`SessionManager._resume_active_experiment`).
2. **GUI form-autosave** (`gui/form_autosave.py`) — sample form fields,
   procedure param cache, the run queue, plot axes — one JSON file per logged-in
   user in `%APPDATA%/CryoSoft/`, unrelated to any experiment.
3. **Measurement data** — HDF5 files land flat in whatever the Data Directory
   field says, a sibling of `experiments/`, not nested inside the experiment
   that produced them (only linked by a path string in `RunRecord.data_file`).

The user's request: make a "session" mean the *whole* picture — GUI state,
experiment/run metadata, and the data itself — bundled in one place, so (a)
the GUI can load/resume a named session, (b) a user can keep several
independently-named sessions and switch between them, (c) the run queue
survives as part of the record, and (d) an agent (or a human backing up a
folder) gets the complete picture by pointing at one directory. Confirmed
with the user: context-level crash recovery only (no Orchestrator/procedure
changes — a run in flight at crash time is marked interrupted, not resumed
mid-step), the L6 experiment record becomes the canonical "session," one
session active at a time, a configurable-but-overridable sessions root, and
the run queue is promoted into the record.

This is directly the "records & store" foundation `docs/plans/session-management-layer.md`
already called Phase 1 for, and the substrate `docs/plans/agent-native-architecture.md`'s
planned Agent Gateway (`list_experiments()/get_experiment()/read_run_data()`)
is meant to read from — this change makes that substrate actually complete
(today it's missing GUI state, the queue, and co-located data) without
building the Gateway itself.

**Naming**: keep every existing Python identifier (`ExperimentRecord`,
`ExperimentStore`, `SessionManager`, `experiment_id`, `start_experiment`/
`close_experiment`) — GLOSSARY.md already documents "session" as the intended
user-facing word for this L6 concept (`gui/session.py` was renamed to
`form_autosave.py` specifically so "session" would mean this). New UI text
(menu items, dialog titles) says "Session"; no class/file renames.

## New on-disk layout

```
<sessions_root>/<experiment_id>/
    experiment.json      # unchanged file, gains a `queue` field
    gui_state.json        # NEW — was %APPDATA%/CryoSoft/(sessions/<user>.json
    data/                 # NEW — HDF5 files nest here instead of landing flat
<sessions_root>/active.json   # unchanged (already exists)
```

`sessions_root` replaces the implicit `<data_dir>/experiments` root — it
becomes its own explicit, app-settings-backed path (default preserved,
user-overridable), decoupled from the Data Directory field. When no session
is open, GUI state keeps falling back to today's per-user
`%APPDATA%/CryoSoft/...` file — this is purely additive, nothing breaks for
someone who never starts an experiment.

## Changes, bottom-up

### 1. `cryosoft/session/models.py`
Add `queue: list[dict[str, Any]] = field(default_factory=list)` to
`ExperimentRecord` (tolerant `to_dict`/`from_dict`, same defensive-dict-filter
pattern already used for `RunRecord.params`/`settings_snapshot`). Stored as
opaque JSON dicts — the session layer never imports `gui.form_autosave`
(contract C11); the GUI is the only place that knows `QueueItemState`.

### 2. `cryosoft/session/store.py`
`ExperimentStore` gains two path helpers (no change to existing save/load):
`data_dir(experiment_id) -> Path` (`<root>/<experiment_id>/data`) and
`gui_state_path(experiment_id) -> Path` (`<root>/<experiment_id>/gui_state.json`).

### 3. `cryosoft/session/manager.py`
- `set_queue(items: list[dict]) -> None` — no-op if nothing open, else
  `self._experiment.queue = items; self._save_current()`. Same atomic-write
  discipline as every other mutator here.
- `switch_experiment(experiment_id: str) -> ExperimentRecord` — loads an
  **open** experiment (raises `ValueError` if unknown or `status != open`),
  deactivates the current in-memory experiment *without closing it*
  (`close_experiment()`'s finalize-and-prompt-findings semantics are
  untouched and still the only way to actually close one), re-installs the
  target's envelope, updates `active.json`, emits `experiment_changed`.
  Switching to a *closed* experiment (read-only history browsing) is
  explicitly out of scope for this change — the record is still readable
  directly as JSON by anyone (including a future agent) regardless.
- `current_data_dir() -> Path | None` / `current_gui_state_path() -> Path | None`
  — thin passthroughs to the store helpers for the current experiment, so
  GUI code never reaches into `store` internals directly.

Tests (`tests/test_session_layer.py`): extend
`test_experiment_record_round_trips_with_content` for `queue`; new tests for
`set_queue`, `switch_experiment` (happy path, rejects closed target, rejects
unknown id, deactivate-without-closing verified via `store.load()` still
showing `status == open`).

### 4. `cryosoft/gui/app_settings.py`
`sessions_root() -> Path` / `set_sessions_root(path) -> None` — QSettings-backed
(same tier as `config_active`/`current_user_id`: machine-level, changes
rarely), default equal to today's `_DEFAULT_DATA_DIR` value so a fresh
install's default location is unchanged.

### 5. `cryosoft/gui/session_info_panel.py`
Data Dir field becomes derived-but-editable: on `experiment_changed`, if an
experiment is now open, set the field to `session_manager.current_data_dir()`;
if none, leave/restore whatever the field held before (today's manual
default). The field stays a plain editable `QLineEdit` + Browse — no
structural widget change, only what drives its text on session switch.

### 6. New `cryosoft/gui/session_dialogs.py`
`LoadSessionDialog(QDialog)` — lists every experiment from
`session_manager.store.list_experiments()` (resolved via `store.load()` for
title/user/status/date), open ones selectable, closed ones shown grayed with
a "(closed)" suffix and disabled. Mirrors `UserPickerWidget`'s
list-plus-accept pattern from `experiment_dialogs.py`. On accept, returns the
chosen `experiment_id`.

### 7. `cryosoft/gui/monitor_window.py`
- `_build_menu()`: add "Load Session…" to the User menu (near "Log in as…"),
  and "Sessions Folder…" (a one-field browse dialog over
  `app_settings.sessions_root()`/`set_sessions_root()`).
- New `_switch_session(experiment_id)`, mirroring the existing
  `_switch_user()` save-outgoing/load-incoming shape: save current GUI state
  (to wherever it currently targets — session or per-user fallback), call
  `session_manager.switch_experiment(id)`, then load the target's
  `gui_state.json` (defaulting to an empty `SessionState` if the target
  session has none yet), apply it to `_session_info` and (if open)
  `_procedure_window`.
- Connect `session_manager.experiment_changed` in `MonitorWindow` itself
  (`SessionInfoPanel` already listens to it for the status label) to trigger
  the Data Dir refresh and, on start/switch, load that session's
  `gui_state.json` over the current in-memory `SessionState`.
- `_save_session()`: when a session is open, write to
  `session_manager.current_gui_state_path()` instead of the per-user AppData
  path, and additionally call
  `session_manager.set_queue([item.to_dict() for item in queue_items])`.
  When no session is open, behavior is unchanged (per-user AppData file).

### 8. `cryosoft/main.py`
`ExperimentStore` rooted at `app_settings.sessions_root()` directly (no more
`Path(autosave.data_dir) / "experiments"` — the extra nesting existed only to
keep flat data files and experiment folders apart, which is no longer needed
once Data Dir is derived from the session).

## Explicitly out of scope (confirmed with user)
- No Orchestrator/BaseProcedure/DataManager changes — no mid-run checkpoint/resume.
- No rename of `ExperimentRecord`/`ExperimentStore`/`SessionManager`/`experiment_id`.
- No Agent Gateway implementation (`session/gateway/`) — separate planned work.
- No change to `servicing_log.py` storage (setup-level, orthogonal to experiments).
- No migration of existing flat HDF5 files into the new `data/` subfolders —
  additive only; old `RunRecord.data_file` absolute paths keep working as-is.
- Switching to a *closed* experiment (read-only review) — not built; the JSON
  stays directly readable regardless.

## Docs (same commits as the code, per the folder-README standard)
- `cryosoft/session/README.md`: Exit section (new `data/`, `gui_state.json`),
  Files table (`manager.py`'s new methods), a line noting `queue` is
  GUI-authored opaque data the session layer stores but never interprets.
- `cryosoft/gui/README.md`: update rows for `session_info_panel.py`,
  `monitor_window.py`, `app_settings.py`; add `session_dialogs.py`.
- `GLOSSARY.md`: extend the **Experiment** entry (queue field, `data/`/
  `gui_state.json` now part of the folder); note **Setup tier**/User menu
  gains "Sessions Folder".

## Verification
- `make check` (ruff, lint-imports/contracts, pytest -m "not hardware") green.
- New/extended tests: `test_session_layer.py` (queue round-trip, `set_queue`,
  `switch_experiment` happy/error paths), `test_gui.py` (LoadSessionDialog
  picker, `_switch_session` save-outgoing/load-incoming round trip mirroring
  `test_switch_user_saves_outgoing_and_loads_incoming_session`, Data Dir
  auto-populate on start/switch, queue persisted through a session switch and
  restored).
- Manual/offscreen GUI smoke: start a session, add to queue, switch to a
  second session, switch back — confirm queue and plot axes reappear exactly
  as left; confirm `<sessions_root>/<id>/data/` receives a run's HDF5 file.
