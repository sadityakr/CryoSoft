# Plan: the Session Management layer (L6)

**Status:** proposal — no code yet.
**Scope:** a new top layer that manages complete experiments: who is measuring,
which sample, which runs belong together, what came out of them — and pushes
the findings into an electronic lab notebook (eLab) from templates.
**Date:** 2026-07-17

---

## 1. Where we are today

A survey of the codebase (2026-07-17) established the starting point:

- **`gui/session.py` is form autosave, not experiment management.** Its
  `SessionState` persists exactly what the physicist typed — sample name/id,
  comments, data dir, last procedure + params, and the run queue — to one JSON
  file so a reopened app restores the forms. It knows nothing about runs that
  completed, output files, users, or grouping. It is Qt-free, loads tolerantly,
  saves atomically — patterns worth keeping, but it is a different concept from
  what this plan builds.
- **Sample metadata already flows end-to-end.** `sample_info =
  {sample_name, sample_id, comments}` travels GUI → `BaseProcedure.__init__` →
  `DataManager` → the HDF5 `/metadata/sample_info` attribute. This is the
  proven channel for getting *more* metadata (user, experiment id, eLab link)
  into every data file.
- **The Orchestrator announces almost everything except the one thing we need
  most.** It emits `states_updated`, `measurement_ready`,
  `procedure_progress`, `procedure_finished`, `error_occurred`,
  `action_succeeded/failed`, `status_message` — but `procedure_finished()`
  carries **no payload**: no procedure name, no parameters, no output file
  path. Nothing outside the procedure object knows which HDF5 file a run
  produced. This is the single genuine gap in the core.
- **There is no eLab/ELN, user-identity, or experiment-grouping code
  anywhere.** Greenfield.
- **Strong precedents exist for everything else we need:**
  - persistence: `ConfigCatalog`'s shipped/user split + versioned history,
    and `gui/session.py`'s atomic-write/tolerant-load JSON discipline;
  - swappable backends: the driver contract's real/sim twin rule;
  - wiring: `main.py` constructs Station → Orchestrator → MonitorWindow and
    injects collaborators (`catalog=...`) — a new manager slots in the same way;
  - enforcement: import-linter contracts C1–C10 and the auto-discovering
    conformance tests.

## 2. The concept model

Four new first-class terms (to be added to `GLOSSARY.md` in the first
implementation commit):

| Term | Definition |
|---|---|
| **Run** | One execution of one procedure, producing exactly one HDF5 file. Today this exists only informally; it becomes a typed record (`RunRecord`). |
| **Experiment** | A named group of runs on one sample toward one scientific question (e.g. "Hall bar A3 — SOT switching vs T"). Holds the user, the sample, the config identity it ran under, the run list, and the eLab linkage. One experiment ↔ one eLab entry. |
| **User** | The person measuring: display name, email, optional ORCID, plus their eLab identity. A setup-local roster; not authentication. |
| **ELN adapter** | A backend-neutral interface to an electronic lab notebook. First concrete backend: eLabFTW (REST API v2). Every real adapter has a sim twin, exactly like drivers. |

The user's requested split maps onto two sub-packages of one layer:

- **Part A — experiment operations** (`cryosoft/session/`): user + sample +
  experiment records, run capture, instrument-settings snapshots, stamping all
  of it into every HDF5 file.
- **Part B — science findings / eLab** (`cryosoft/session/eln/`): templates,
  publishing experiment findings and results (files, plots, tables) to the
  eLab, keeping the eLab entry in sync as runs accumulate.

## 3. Where the layer sits

A new package **`cryosoft/session/` — L6, between core and GUI**:

```
GUI  (PyQt6)  ──────────────► may import session (display/trigger)
main.py       ──────────────► constructs SessionManager, injects into GUI
L6  Session Manager (NEW)  ─► imports Orchestrator (signals), Station
                              (identity/state), h5py (read-back for publishing)
L5  Data manager ◄─ unchanged except one optional metadata param
L4…L0            ◄─ unchanged
```

Rules, mirroring the existing style:

- `cryosoft.session` may import: `core.orchestrator` (to connect signals),
  `core.station` (config identity, snapshots), `core.plan`,
  foundation modules, h5py/numpy, stdlib, and — for the eLab client — the HTTP
  stack (see §7 on threading).
- `cryosoft.session` must **never** import `cryosoft.gui` (it stays
  Qt-widget-free; Qt *signals* are fine since it observes a `QObject`),
  drivers, or concrete VIs.
- Nothing in core/drivers/VIs/procedures may import `cryosoft.session`.

Two new import-linter contracts in `pyproject.toml`:

- **C11**: `cryosoft.session` is forbidden from importing
  `cryosoft.gui`, `cryosoft.main`, `cryosoft.drivers`,
  `cryosoft.virtual_instruments` (base excepted if ever needed — start fully
  forbidden), `cryosoft.troubleshoot`.
- **C12**: everything below the GUI (`core`, `drivers`,
  `virtual_instruments`, `procedures`, `troubleshoot`) is forbidden from
  importing `cryosoft.session` (mirror of C9).

## 4. Part A — experiment operations (`cryosoft/session/`)

### 4.1 `models.py` — the typed records

Plain dataclasses with `to_dict`/`from_dict` following the tolerant-parse
standard of `gui/session.py` (missing keys → defaults, unknown keys ignored,
never raise on bad input):

```python
@dataclass
class User:
    user_id: str          # slug, unique in the roster
    name: str
    email: str = ""
    orcid: str = ""
    eln_user_id: str = "" # backend-side identity, optional

@dataclass
class RunRecord:
    run_id: str                       # e.g. "20260717_143201_field_sweep"
    procedure: str                    # canonical procedure name
    params: dict[str, object]
    data_file: str                    # absolute path of the HDF5 file
    started_utc: str
    finished_utc: str = ""
    status: str = "running"           # running | done | failed | aborted
    outcome: str = ""                 # error text when failed
    settings_snapshot: dict = ...     # full station snapshot at initiate
    published: bool = False           # mirrored to the eLab entry yet?

@dataclass
class ExperimentRecord:
    experiment_id: str                # slug + date, unique in the store
    title: str
    user_id: str
    sample_info: dict[str, object]    # same shape the GUI already collects
    config_name: str                  # active config identity at creation
    created_utc: str
    status: str = "open"              # open | closed
    runs: list[RunRecord] = ...
    findings: str = ""                # free-text science notes (markdown)
    eln_link: ElnLink | None = None   # backend, entry id, URL, template used
```

`settings_snapshot` is the instrument-settings half of the user's request: the
full `station.cached_state` (every VI's `get_state()`) plus the run's
`system_targets`, captured at `initiate`. It is already written per-point into
the HDF5 `/snapshots` group; the RunRecord keeps the initiate-time copy so the
experiment file answers "what were the instrument settings for run N" without
opening HDF5.

### 4.2 `store.py` — persistence

`ExperimentStore`: one folder per experiment **inside the data directory**,
so the record archives with the data it describes:

```
<data_dir>/experiments/<experiment_id>/experiment.json
```

- Atomic writes (`.tmp` + `os.replace`), tolerant loads — same discipline as
  `gui/session.py`.
- A user roster `users.json` lives in the app-settings user dir
  (`%APPDATA%/CryoSoft/`), because users belong to the setup, not to one data
  folder.
- API sketch: `list_experiments()`, `load(experiment_id)`,
  `save(record)`, `create(title, user, sample_info, config_name)`,
  `active_experiment_id` (persisted so a restart resumes the open experiment).
- No versioned history in v1 (append-only run list makes clobbering unlikely);
  if it proves needed, copy the `.versions/<id>/` pattern from `ConfigCatalog`.

### 4.3 `manager.py` — the `SessionManager`

The layer's façade and the only object `main.py`/GUI touch:

```python
class SessionManager(QObject):
    experiment_changed = pyqtSignal(dict)   # record as dict, for GUI display
    run_recorded = pyqtSignal(dict)         # a RunRecord as dict
    publish_state_changed = pyqtSignal(dict)  # eLab sync status, per §5

    def __init__(self, store: ExperimentStore, orchestrator: Orchestrator,
                 station: Station, eln: ElnAdapter | None = None) -> None: ...
    # experiment lifecycle
    def start_experiment(self, title: str, user_id: str,
                         sample_info: dict) -> ExperimentRecord: ...
    def close_experiment(self) -> None: ...
    def set_findings(self, text: str) -> None: ...
    # context injection (see §4.4)
    def experiment_context(self) -> dict[str, str]: ...
    # publishing (Part B; no-ops when eln is None)
    def publish_experiment(self) -> None: ...
    def publish_run(self, run_id: str) -> None: ...
```

It connects to the Orchestrator's signals in `__init__` and records runs
automatically: `run_started` opens a `RunRecord`, `run_finished` completes it
(status, end time, file path), `error_occurred` during a run marks it failed.
The GUI never writes records itself — it only calls the lifecycle methods and
renders the signals, keeping the manager the single writer of experiment state
(the same single-writer principle the Orchestrator applies to hardware).

Construction in `main.py`, matching the existing injection style:

```python
store = ExperimentStore(app_settings. ...)
session_mgr = SessionManager(store, orchestrator, station, eln=eln_or_none)
monitor = MonitorWindow(station, orchestrator, catalog=catalog,
                        session_manager=session_mgr, ...)
```

### 4.4 Small, surgical changes to existing code

These are the only edits below the new layer, all backward compatible:

1. **Orchestrator: run manifests.** Add two signals and emit them where the
   state machine already knows the facts:
   - `run_started(dict)` — on successful `initiate()`: procedure name, params,
     data file path, start time.
   - `run_finished(dict)` — on natural finish, abort, or error-out: the same
     plus end time and terminal status.
   To expose the file path without touching privates, give `BaseProcedure` a
   public read-only property `data_filepath -> str | None` delegating to its
   data manager (the Orchestrator already programs against the BaseProcedure
   interface, so C5 is untouched; today it reads `procedure._data_manager`
   directly for `last_datapoint`, and that access can switch to a public
   property in the same commit).
2. **DataManager: one optional metadata field.** Add
   `experiment_info: dict | None = None` to the constructor and write it as
   `/metadata/experiment_info` (JSON attr, `{}` default). Carries
   `experiment_id`, `experiment_title`, `user_id`, `user_name`, `eln_url`.
3. **BaseProcedure: pass-through.** Accept optional `experiment_info` in
   `__init__` and forward it to every `DataManager` it constructs.
4. **GUI: merge the context.** Where `ProcedureWindow` builds procedure
   instances (queue construction), merge
   `session_manager.experiment_context()` into the `experiment_info` argument.
   Every HDF5 file produced during an experiment is then permanently stamped
   with who/what/why, and carries the eLab URL of its experiment.
5. **Rename the collision.** `gui/session.py` → `gui/form_autosave.py`
   (module + docstrings + `gui/README.md` row + `tests/test_session.py`
   rename; JSON schema and file location unchanged, so nothing breaks for
   existing installs). "Session" then unambiguously means the L6 concept.
   GLOSSARY gains rows for Run / Experiment / User / ELN adapter / Form
   autosave.

### 4.5 GUI surface (minimal, follows the `gui-edit` skill when built)

- The Sample Info quadrant grows an **Experiment header**: user selector
  (roster + "add user"), experiment title, Start/Close experiment buttons, and
  an eLab status chip (linked / pending / offline / not configured).
  Sample fields stay exactly as they are — an open experiment simply snapshots
  them at `start_experiment`.
- A **run log list** (experiment's runs with status and file path) fits the
  existing "Other devices / Log" selector combo in the bottom-right quadrant.
- Publishing controls: "Publish to eLab" on the experiment header;
  per-run auto-publish toggle (see §5.4).

## 5. Part B — science findings and the eLab (`cryosoft/session/eln/`)

### 5.1 The ELN adapter contract (a new written standard)

Backend-neutral abstract base, docstring = the standard, enforced by
conformance (§6):

```python
class ElnAdapter(ABC):
    """Contract: stateless wrapper over one ELN backend.  All methods are
    synchronous and raise ElnError on failure; queuing/retry live in the
    Publisher, never in adapters.  Every real adapter has a sim twin with an
    identical public API (drivers rule, applied to ELNs)."""
    def verify(self) -> str: ...                     # reachability + identity
    def list_templates(self) -> list[ElnTemplate]: ...
    def create_entry(self, title: str, template_id: str | None,
                     body_html: str, tags: list[str],
                     metadata: dict) -> ElnEntryRef: ...
    def update_entry(self, ref: ElnEntryRef, body_html: str,
                     metadata: dict) -> None: ...
    def attach_file(self, ref: ElnEntryRef, path: Path,
                    comment: str = "") -> None: ...
```

Concrete backends:

- **`elabftw.py`** — eLabFTW REST API v2 (`/api/v2/experiments`,
  `/api/v2/experiments/{id}/uploads`, experiment templates). Host + team +
  certificate settings in config; the API key **never** in a git-tracked file —
  read from the `CRYOSOFT_ELAB_APIKEY` environment variable or the OS keyring,
  with the settings dialog storing to the keyring.
- **`sim_eln.py`** — in-memory twin recording every call; the workhorse for
  all tests, mirroring `sim_` drivers.

### 5.2 Templates

The user asked for "given templates". Two cooperating levels:

1. **eLab-side experiment templates** (categories, team defaults): the adapter
   creates entries *from* a template id chosen in settings — the lab's
   existing eLabFTW templates keep working untouched.
2. **CryoSoft-side body renderers**: what CryoSoft writes *into* the entry.
   Plain Python renderers (no new template-language dependency in v1) in
   `session/eln/render.py`:
   - `render_experiment(record) -> str` — header table (user, sample, config,
     dates), findings section, and a run table (procedure, key params, status,
     data file name);
   - `render_run(run) -> str` — the per-run block: parameters, instrument
     settings snapshot summary, link to the attached data file.
   Rendered HTML is deterministic and fully covered by unit tests. If labs
   later need customizable layouts, promote to Jinja templates under a
   shipped/user split copied from `ConfigCatalog` — explicitly out of scope
   for v1.

### 5.3 The publisher: offline-first, never in the tick path

Lab networks and eLab servers go down; measurements must not care.
`publisher.py`:

- A **journaled outbox**: publish requests (create/update/attach) append to
  `<data_dir>/experiments/<id>/outbox.jsonl`; completed items are marked, so
  a crash or offline day never loses an update.
- A drain loop on its **own QTimer, decoupled from the Orchestrator tick**,
  sending at most one outbox item per firing with a short HTTP timeout.
  This is the same cooperative single-threaded philosophy as the tick loop —
  no thread, no lock, no shared-bus risk (see Open question §8.1 for the
  thread alternative if real-world uploads prove too slow for this).
- Status surfaces through `SessionManager.publish_state_changed` → the GUI
  chip: `synced / n pending / offline (retrying) / auth error`.

### 5.4 What gets published when

- **Experiment start** → create the eLab entry from the configured template;
  store `ElnLink` in the record (from then on, every HDF5 file stamps that
  URL via `experiment_info`).
- **Run finished** (if auto-publish is on, default on) → update the entry
  body (run table row) and attach the HDF5 file. Attachment of large files is
  size-capped in settings; above the cap, publish metadata + file path only.
- **Findings edited / experiment closed** → update the body; on close, also
  attach a rendered summary and (later phase) PNG exports of the final plots.
- Manual **Publish now** always available; nothing is ever published without
  an experiment explicitly started (no silent uploads of ad-hoc runs).

## 6. Standards, conformance, and docs (the harness)

Per the repository's standards-over-one-off-code principle, the layer ships
with its enforcement:

- **Contracts C11 + C12** as in §3 (never weakening C1–C10).
- **Conformance tests** in `tests/test_conformance.py`, following the existing
  auto-discovery pattern:
  - every module in `cryosoft/session/eln/` that subclasses `ElnAdapter` has a
    `sim_` twin with an identical public API (reuse the driver-twin checker);
  - every adapter's `__init__` takes a single settings mapping (the analogue
    of the one-resource-string driver rule);
  - all `models.py` dataclasses round-trip `to_dict`/`from_dict` and tolerate
    junk input without raising;
  - `render_*` output is valid, self-contained HTML (no external resources).
- **Folder READMEs** for `cryosoft/session/` and `cryosoft/session/eln/` in
  the standard 7-section shape (Purpose, Architecture layer, Entry, Exit,
  Interface contract, How to add a new module, Files).
- **GLOSSARY rows**: Session Manager (L6), Experiment, Run, User, ELN adapter,
  Publisher/outbox, Form autosave — in the same commits that introduce them.

## 7. Phased delivery (bottom-up, tests before the next layer)

Each phase ends with `make check` green; no phase builds on an untested one.

**Phase 0 — groundwork (core + gui touch-ups)**
Rename `gui/session.py` → `gui/form_autosave.py`; add
`BaseProcedure.data_filepath`; Orchestrator `run_started`/`run_finished`
manifests; DataManager `experiment_info`; glossary rows.
*Tests:* extend the existing orchestrator/data-manager suites; a sim-config
integration test asserting a full run emits both manifests with a real file
path and the HDF5 carries `/metadata/experiment_info`.

**Phase 1 — records and store (no GUI, no eLab)**
`cryosoft/session/` with `models.py`, `store.py`, `manager.py` (eln=None);
contracts C11/C12; conformance for models; wire into `main.py`.
*Tests:* store round-trip/atomicity/corrupt-file tolerance; SessionManager
against a real Orchestrator + sim station — run a sim FieldSweep end-to-end,
assert the RunRecord matches the HDF5 on disk.

**Phase 2 — ELN adapter standard + eLabFTW backend**
`ElnAdapter`, `sim_eln.py`, `elabftw.py`, settings + keyring/env secret
handling; adapter conformance (twin rule).
*Tests:* everything against the sim adapter; eLabFTW client tested against
canned HTTP responses (no live server in CI); a `hardware`-marker-style
optional live test (`elab` marker) for use against a real instance.

**Phase 3 — renderers + publisher**
`render.py`, `publisher.py` (outbox + QTimer drain), SessionManager publish
API and auto-publish policy.
*Tests:* renderer snapshots; outbox crash/offline/retry simulations with the
sim adapter; end-to-end sim run → sim eLab entry with correct body + attachment.

**Phase 4 — GUI**
Experiment header, run log, status chip, publish controls, settings page —
built under the `gui-edit` skill rules (theme tokens, offscreen screenshot
verification).
*Tests:* GUI suite additions mirroring existing window tests.

**Phase 5 — polish (optional / later)**
Plot PNG export on close; experiment record versioning if needed; Jinja
body templates (shipped/user split); additional ELN backends (the adapter
standard makes each one a leaf module + sim twin, zero core change).

## 8. Open questions (decide before Phase 2/3)

1. **Upload transport** (§5.3): the QTimer drain keeps the no-threads
   philosophy but serializes uploads with the GUI event loop; multi-MB HDF5
   attachments over slow lab links may stutter the GUI. Fallback options, in
   preference order: chunked uploads per firing → `QNetworkAccessManager`
   (async, still single-threaded) → one dedicated upload QThread that touches
   only HTTP + files (never Station/Orchestrator — the no-thread rule guards
   the tick/hardware path). Recommendation: start with the QTimer drain and
   measure.
2. **eLabFTW as the assumed backend**: the plan hard-codes nothing (adapter
   standard), but Phase 2 builds eLabFTW first. Confirm the lab's actual ELN
   and API version before Phase 2.
3. **Experiment ↔ eLab granularity**: this plan maps one experiment → one
   eLab entry with per-run body sections. The alternative (one entry per run,
   linked to a parent) fits labs whose templates are per-measurement. The
   record model supports either; the choice only shapes `render.py` and the
   publish policy.
4. **Where the user roster lives**: app-settings dir (proposed, per-setup) vs
   fetched from the eLab (users already exist there). Proposed: local roster
   with an optional `eln_user_id` link — works offline, no auth complexity.
