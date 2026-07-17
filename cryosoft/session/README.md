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
  procedure, kind, params, data file path, timestamps, terminal status).
- GUI lifecycle calls on the `SessionManager`: `start_experiment`,
  `close_experiment`, `set_findings`, `set_attended`.
- The active config identity (from `main.py`) and the station's cached state
  (settings snapshot at each run start).

## Exit (what goes out)

- Persisted records: `<data_dir>/experiments/<experiment_id>/experiment.json`
  (+ the `active.json` resume pointer), and the setup-local `users.json`
  roster next to the app settings.
- `Orchestrator.set_session_envelope()` — the experiment's sample bounds,
  enforced in the Orchestrator for every writer.
- `experiment_context()` — the dict the GUI passes as `experiment_info` when
  constructing procedures, stamped into every HDF5 file's
  `/metadata/experiment_info`.
- Signals for the GUI: `experiment_changed(dict)`, `run_recorded(dict)`.

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
- **Qt-widget-free.** `SessionManager` is a `QObject` (signals only); the
  package never imports Qt widgets or `cryosoft.gui` (contract C11).

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

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `models.py` | Tolerant-parse records: users, runs, experiments, ELN links; envelope (de)serialisation. | `User`, `RunRecord`, `ExperimentRecord`, `ElnLink`, `envelope_to_dict`, `envelope_from_dict` | `tests/test_session_layer.py` + conformance |
| `store.py` | Disk persistence: per-experiment folders + active pointer; user roster. | `ExperimentStore` (`list_experiments`, `load`, `save`, `get_active`, `set_active`, `make_experiment_id`), `UserRoster` (`list_users`, `get`, `add`) | `tests/test_session_layer.py` |
| `manager.py` | The L6 façade: experiment lifecycle, automatic run recording from manifests, envelope installation, HDF5 context. | `SessionManager` (`start_experiment`, `close_experiment`, `set_findings`, `set_attended`, `experiment_context`, `current_experiment`; signals `experiment_changed`, `run_recorded`) | `tests/test_session_layer.py` |
