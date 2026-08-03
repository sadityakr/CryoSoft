# Session tier and terminology cleanup

**Status: proposal, not started.** A new middle tier in the measurement
filesystem hierarchy (root → session → experiment → data files), plus a
rename cascade that resolves the fact that "session" already names four
unrelated things in this codebase. Companion to
`docs/plans/config-directory-migration.md` (same root-fixing principle, one
tier over) and `cryosoft/core/paths.py` (this session's earlier work).

## Why: the current collision

"Session" means four different things today, none of them what a new
"per-user folder holding multiple experiments" tier would need to mean:

- **Form-autosave** (`gui/form_autosave.py:SessionState`,
  `app_settings.py:session_file_path(user_id)` → `%APPDATA%/CryoSoft/sessions/
  <user_id>.json`) — a snapshot of GUI form field values, unrelated to
  experiment identity. `session/README.md` already flags this collision.
- **The L6 experiment-management layer, addressed as "Session"**
  (`session/manager.py:SessionManager`, `gui/session_dialogs.py:
  LoadSessionDialog`, `gui/session_info_panel.py:SessionInfoPanel`,
  `monitor_window.py:_switch_session()`) even though its actual persisted
  unit is `ExperimentRecord`/`experiment_id` (GLOSSARY: **Experiment**).
- **`sessions_root`** (`app_settings.py:sessions_root()`/`set_sessions_root()`)
  — the base directory itself. This is what becomes the fixed
  **measurement root** below.
- **`SessionEnvelope`** (`core/plan.py`) — per-experiment safety bounds.
  Nothing to do with folders or files; a fourth, unrelated reuse of the word.

## Terminology (old → new)

| Old | New | Why |
|---|---|---|
| `sessions_root()` | `measurement_root()` | Moves out of QSettings into a fixed, machine-level, config-file-sourced value — see **Root fixing** below. |
| *(new)* | `Session` | The new tier: a named, resumable, per-user folder holding multiple experiments. One user can have several. |
| `SessionManager` | `ExperimentManager` | Its unit was always the experiment; the name now says so, and frees "Session" for the new tier. |
| `LoadSessionDialog` | `OpenExperimentDialog` | |
| `SessionInfoPanel`, `_current_session_folder()`, `_OUTSIDE_SESSION_NOTE_TEXT` | `ExperimentInfoPanel`, `_current_experiment_folder()`, (removed — see **Enforcement**, the warning becomes a hard block) | |
| `_switch_session(experiment_id)` | `_switch_experiment(experiment_id)` | |
| `SessionState` / `session_file_path()` | `FormAutosaveState` / `autosave_file_path()` | Unrelated to identity/hierarchy — pure UI field memory. |
| `SessionEnvelope` | `ExperimentEnvelope` | Per-experiment safety bounds; ties the name to the tier it belongs to. |

After this, "Session" means exactly one thing across the codebase: the new
tier. No other renames are needed to achieve that — this list is exhaustive
against the current grep of every `Session`-prefixed symbol in `cryosoft/`.

## Filesystem layout

```
<measurement_root>/                          fixed, admin-set, not GUI-editable
├── users.json                               unchanged (UserRoster)
├── servicing/<config_name>/...              unchanged — setup-wide, not session-scoped
└── sessions/
    └── <session_id>/                        NEW tier
        ├── session.json                     NEW record
        └── <experiment_id>/                 unchanged shape, one level deeper
            ├── experiment.json
            ├── gui_state.json                unchanged — per-experiment state, not split
            └── data/                         subfolders still allowed (data/heating_runs/)
                └── *.h5
```

## New data model

`Session` (`session/models.py`, tolerant-parse dataclass, same discipline as
every other L6 model — `to_dict()`/`from_dict()`, missing keys take
defaults, unknown keys ignored):

- `session_id` — stable, generated (mirrors `ExperimentStore.
  make_experiment_id()`'s slug+disambiguator scheme).
- `user_id` — owner, foreign key into `UserRoster`. Not the session's
  identity on its own — a user can own several.
- `name` — display name, user-chosen at creation.
- `default_experiment_dir` — the saved default parent folder offered when
  starting a new experiment in this session; user-editable; must resolve
  under `<session_id>/` once containment is enforced (see below).
- `last_open_experiment_id` — for resume (see **Resume scope**).
- `created_utc`, `last_opened_utc`, `schema_version`.

`SessionStore` (`session/store.py`, parallel to `ExperimentStore`):
`list_sessions(user_id: str | None = None)` (the filter the Resume dialog
needs), `create_session`, `load`, `save`, `set_active`/`get_active` (an
`active.json` one level up the existing per-experiment resume pointer
already uses).

## Login → resume flow

1. `UserRoster`/`current_user_id()` picks the person — unchanged, already built.
2. **Resume Session** dialog (new, replaces nothing — sits above today's
   `OpenExperimentDialog`) lists sessions via `SessionStore.
   list_sessions()`, filterable by user, plus "New Session."
3. `SessionStore.set_active()` restores the session — see **Resume scope**
   for exactly what that means.
4. Inside a session, today's experiment flow runs unchanged, one folder deeper.
5. Starting a new experiment offers `default_experiment_dir` (or a subfolder
   the user browses to within it); anything outside the session folder is
   rejected outright (see **Enforcement**).

### Resume scope (decided)

Resuming a session restores:
- `default_experiment_dir`.
- Which experiment was last open in it (`last_open_experiment_id`) —
  auto-reopens it.
- GUI layout / window state — **no split needed**: `gui_state.json` stays
  exactly where it is today, one per experiment, unchanged in shape.
  Reopening the last-open experiment (above) brings its `gui_state.json`
  along for free. `session.json` itself carries no GUI-state field beyond
  `last_open_experiment_id`.

## Enforcement

Experiment directory containment is **hard**, not a warning: creating an
experiment whose target directory does not resolve under the active
session's folder is rejected (mirrors `_update_data_dir_note()`'s existing
`is_relative_to()` check, but the caller refuses instead of showing a note).
This removes today's "save straight to a network drive" escape hatch —
confirmed acceptable.

## Root fixing

`measurement_root()` moves into `cryosoft/core/paths.py`, following the
established `log_directory()`/`config_directory()` precedence shape:

1. `CRYOSOFT_MEASUREMENT_ROOT` env var, if set.
2. A dedicated machine-level settings file, **`App-config.yaml`** — **not**
   `devices.yaml` (that's per-station and already ruled out) and **not**
   today's QSettings/registry entry (per-user, live-editable through the
   GUI, which is exactly what this change removes). Location:
   `%ProgramData%\CryoSoft\App-config.yaml` on Windows (outside any single
   user's profile, so it's genuinely machine-wide, and normally requires
   elevated rights to write — the access-control property that makes
   "fixed" actually stick rather than being fixed by convention only), the
   POSIX analogue (`/etc/cryosoft/App-config.yaml`) elsewhere. One key for
   now: `measurement_root`. YAML rather than ini both because the rest of
   CryoSoft's config surface is already YAML (`devices.yaml`/`monitor.yaml`)
   and because this file is meant to grow — a second app-wide, fixed,
   admin-set value later is a second key, not a second file or format.
3. No fallback beyond that. If neither resolves, refuse to start rather
   than invent a default — a "fixed" root nobody configured is worse than
   an app that won't launch until it's set.

The GUI's "Sessions Folder…" live-relocate action is removed or turned into
a read-only "measurement root: `<path>` (edit `<settings file>` to change)"
display, since ordinary users can no longer relocate it at runtime.

## Migration

**Decided: no automatic migration.** Existing installations have
`<experiment_id>/` folders directly under the old flat `sessions_root`;
those are left in place — nothing auto-moves them into the new
`sessions/<session_id>/` layout, and only new experiments created after this
ships use the session tier.

**Still needed**: a short, user-facing note explaining how to manually move
old experiment folders into a session folder if someone wants their history
inside the new hierarchy. **Decided**: lives in a new `docs/user-docs/`
folder (sibling to `docs/plans/`; `docs/` previously had only `plans/`) —
end-user how-tos, as opposed to `docs/plans/`'s developer-facing design
docs. `docs/user-docs/README.md` establishes the convention. The actual
migration note (`docs/user-docs/upgrading-to-session-folders.md` or similar)
is written once the session tier's real folder-naming details are final,
not before — its steps depend on the implementation, not the design.

**Explicit consequence of "no migration" (confirmed acceptable):** each
`ExperimentStore` becomes scoped to one session's subtree
(`measurement_root() / "sessions" / session_id`, not `measurement_root()`
itself — see **Files this touches**). Old, pre-migration experiment folders
sitting flat under `measurement_root()` are therefore not just "left in
place" but **unreachable through the new session-scoped Resume/Open
dialogs** after this ships — there is no legacy flat-layout store kept
around to list them. Anyone who needs an old experiment uses the filesystem
directly, or follows the manual migration note above to move it under a
session folder first.

## Format-rule / schema implications

`session/README.md`'s existing **Format rules** section treats any change to
what `experiment.json`/`gui_state.json`/`active.json` mean as "a file-format
change, not a routine edit," gated by `schema_version`. This plan:
- Adds a new file type (`session.json`) and a new resume pointer
  (`active.json` one level up) — new files, no version-compatibility issue
  on their own.
- Does **not** change `experiment.json`'s own schema, only its *location*
  (one directory deeper).

**Checked against actual behavior** (`session/store.py`): `ExperimentStore`
has no built-in knowledge of `sessions_root` — every method
(`resolve_data_file`, `data_dir`, `gui_state_path`, `relativize_data_file`,
`list_experiments`, `load`, `save`) keys off whatever `root` its caller
passed at construction. `resolve_data_file()`'s recursive-basename fallback
is already deep enough for sub-foldered *data files within one experiment*;
that part needs no change. The real change is at the one construction site,
`main.py:161` (`ExperimentStore(app_settings.sessions_root())`), which must
become a **per-session** store: `ExperimentStore(measurement_root() /
"sessions" / session_id)`. See **Migration** above for the resulting
consequence for pre-migration experiments (they become unreachable through
the new UI — confirmed acceptable, no legacy fallback store).

`main.py:189`'s `servicing_root = app_settings.sessions_root() /
"servicing"` stays flat at `measurement_root()` per the filesystem layout
above — confirmed correct as-is, no change needed there.

**ObjectName strings** (`session_info_quadrant`, `data_dir_input`,
`browse_btn`, etc., on the panel being renamed to `ExperimentInfoPanel`):
**stay as literal strings, do not rename alongside the class.**
`tests/test_gui.py` accesses these almost entirely via Python attribute
(`panel._data_dir_input`), not objectName lookup, with one exception
(`findChild(QScrollArea, "session_info_scroll")` at test_gui.py:1871) that
needs no change either, since the string itself isn't changing. Precedent
already exists elsewhere (`sweep_axis_widget.py`) for widget ObjectNames
staying stable independent of their owning class's name. Renaming the
strings would only add rename-cascade risk for no benefit.

## Files this touches (non-exhaustive, from the current grep survey)

Renames: `session/manager.py`, `gui/session_dialogs.py`,
`gui/session_info_panel.py`, `gui/monitor_window.py`,
`gui/procedure_window.py`, `gui/procedure_params_panel.py`,
`gui/form_autosave.py`, `core/plan.py`, `core/orchestrator.py`
(`set_session_envelope`).

New: `session/models.py` (`Session`), `session/store.py` (`SessionStore`),
a `paths.py` addition (`measurement_root()`).

Docs: `GLOSSARY.md` (**Session Manager (L6)**, **Experiment**, **Session
envelope** entries), `session/README.md`, `gui/README.md`'s `app_settings.py`
row, `core/README.md` if `paths.py`'s row needs updating.

Tests: `tests/test_session_layer.py`, `tests/test_gui.py` — note
`session_info_panel.py`'s own docstring calls its Qt `ObjectName`s
(`session_info_quadrant`, `data_dir_input`, `browse_btn`, etc.) "preserved
API — tests and muscle memory rely on them." Whether those ObjectNames
rename alongside the Python class names, or stay stable strings
independent of them, is a call to make at implementation time — not
addressed by this plan.

## Out of scope here

- Writing the actual `docs/user-docs/upgrading-to-session-folders.md`
  content — location is decided, content waits for implementation details.
- Any change to `config_directory()`/`data_directory()` — that's
  `config-directory-migration.md`'s territory, unrelated tier.
