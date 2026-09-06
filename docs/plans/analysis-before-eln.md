# Analysis before the notebook — a dedicated analysis stage between a finished run and its eLab entry

**Status:** Implemented — 2026-09-05 (`cryosoft/analysis/`, `session/eln/` + `session/analysis_runner.py`, `gui/analysis_panel.py` + `gui/eln_settings_dialog.py`, five gateway tools; import contracts C22/C23). Builds on the shipped ELN track
(`cryosoft/session/eln/`) and the control contract.

## Problem

Today a finished run is rendered straight into an eLab entry: the run
manifest's fact tables (parameters, per-column statistics, setup) plus the
raw HDF5 file as an attachment. Most of that is not what a physicist wants
in the notebook. What belongs there is the *analysed* result: one or two
figures, a handful of derived numbers, a few sentences, and a compact
pointer back to the data. There is no place in the framework for the code
that turns a run into that — not for the user, and not for an agent.

There is also no in-app way to set the notebook up: the settings file is
edited by hand.

## Decisions taken (with the user, 2026-09-05)

1. **Analysis code lives in two places under one contract.** Package
   recipes in `cryosoft/analysis/recipes/` (auto-discovered, conformance
   checked, one per procedure family), and per-experiment scripts in
   `<experiment folder>/analysis/recipes/*.py` that the user or an agent
   writes at runtime and that override or extend the package ones by name.
2. **matplotlib is an optional `analysis` extra.** A checkout without it
   imports, lints and tests unchanged; a recipe that asks for a figure gets
   one clear error naming the install command. It is added to the `dev`
   extra so CI exercises figures.
3. **The entry body is analysis output plus a short provenance block.**
   Recipe prose, results table and figure captions first; then run id,
   procedure, parameter digest, data-file name and link. The full fact
   tables and the raw data attachment become opt-in per report.
4. **Run ends → analysis → preview in the panel → publish on approval.**
   A failed recipe leaves a facts-only entry pending, never lost; nothing
   is published silently while analysis is switched on.

## Design

### The analysis stage in the layer picture

```
Orchestrator ── RunFinished ──▶ ElnPublisher (session/eln)
                                    │ analysis off: render facts, enqueue (today)
                                    │ analysis on:  ask the AnalysisRunner
                                    ▼
                           AnalysisRunner (session/analysis_runner.py, QProcess)
                                    │ spec.json ──▶  python -m cryosoft.analysis run
                                    │                 (separate process: data_reader + recipe + matplotlib)
                                    ◀── report.json + figures in <experiment>/analysis/<run_id>/
                                    ▼
                    pending entry parked on the run record (manager, single writer)
                                    ▼
                  Procedure window "eLab" tab: preview, Re-run, Publish / Discard
                                    ▼
                         ElnPublisher.export_entry → Outbox job (+ figure attachments) → drain
```

`cryosoft/analysis/` is a new package **beside** the session layer:
it imports only `cryosoft.core.data_reader`, `cryosoft.core.events`,
`cryosoft.core.exceptions` (plus numpy, h5py, stdlib, and matplotlib
lazily). It never imports the Station, the Orchestrator, drivers, VIs,
procedures, the session layer or the GUI. Only `session`, `gui`, `ctl` and
`main` may import it. Two new import contracts (C22, C23) say so.

**Recipes run in a separate process**, never on the instrument thread and
never on the GUI thread: a recipe is user code that may take seconds and
may crash, and the single hardware thread standard already puts every
network call and analysis on the client side of the control contract. A
subprocess is the same idea one step further — like the MCP adapter, it
cannot reach an instrument because it cannot import one.

### The recipe contract (`cryosoft/analysis/base.py`)

```python
class AnalysisRecipe:
    name: str                      # unique snake_case id, e.g. "field_sweep_overview"
    procedures: tuple[str, ...]    # procedure class names it serves; ("*",) = any
    description: str               # one line, shown in the panel and the tool list
    def analyse(self, run: RunSource, context: AnalysisContext) -> AnalysisReport: ...
```

- `run` is the standalone data reader's `RunSource` (`RunHandle` for a
  file), so a recipe reads exactly what an agent's `read_run_*` tools read.
- `context` carries the run manifest, the experiment (id, title, sample
  info, findings), the setup context, the output directory, the requested
  options, and two helpers: `context.figure(name, fig, caption)` saves a
  matplotlib figure as PNG under the output directory and returns a
  `FigureRef`; `context.table(caption, columns, rows)` builds a capped
  `TableSpec`.
- `AnalysisReport` (`report.py`, frozen, JSON-safe, `to_dict/from_dict`)
  is the only thing a recipe returns: `summary` (plain-text paragraphs),
  `results` (`ResultValue` rows: name, value, unit, uncertainty, note),
  `figures`, `tables`, `tags`, `include_fact_tables`, `attach_data_file`,
  `warnings`, plus `recipe`, `recipe_digest` (SHA-256 of the recipe source),
  `status` (`ok`/`failed`) and `error`. Everything is length-capped by the
  renderer, as the fact tables already are.
- **Discovery**: `discover_recipes(extra_dirs)` returns package recipes
  (every `AnalysisRecipe` subclass under `cryosoft/analysis/recipes/`) and
  experiment recipes (every `*.py` under each extra dir, loaded by file
  path). An experiment recipe with the same `name` as a package recipe
  replaces it. `recipe_for(procedure, recipes, preferred="")` picks the
  preferred name, else the first recipe naming the procedure, else the
  first `("*",)` recipe.
- **Shipped recipes**: `generic_sweep` (`("*",)`): one overview figure of
  every measured column against the sweep axis (or against time), a results
  table of the key columns' min/max/mean, and a two-sentence summary
  (points, duration, terminal status). `facts_only` (`("*",)`): no figure,
  `include_fact_tables=True` — the behaviour of today's entry, selectable.
- **Scaffold**: `python -m cryosoft.analysis new-recipe <name> --dir <dir>
  [--procedure X]` writes a commented template recipe; the panel's "New
  recipe…" button calls the same function and opens the file.
- **Worker CLI**: `python -m cryosoft.analysis run --spec <spec.json>`.
  The spec (`AnalysisSpec` in `report.py`) names the data file, the
  manifest, the experiment and setup dicts, the recipe name, the extra
  recipe directories, the output directory and the options. The worker
  writes `<output_dir>/report.json` and the figures, always — a crashing
  recipe yields a `failed` report carrying the traceback, exit code 0. A
  missing spec, an unreadable data file, or an unknown recipe are the only
  non-zero exits, and they also write a failed report when they can.

### Trust boundary

A recipe is code, trusted like a procedure. An agent-written recipe is
written through a `RUN_CONTROL` tool (session role only), stamped with a
header naming the actor and time, recorded in the **Agent feed**, and
visible in the panel before anybody runs it. Running any recipe is also a
`RUN_CONTROL` action for an agent. A human runs whatever is in the folder
with one click; that is the same trust a human extends to the procedures
they start.

### The ELN track changes (`cryosoft/session/eln/`)

- `ElnSettings.analysis: AnalysisSettings` — `enabled` (default False),
  `timeout_s` (default 120), `include_fact_tables` (default False),
  `attach_data_file` (default False), `recipes: dict[str, str]` (procedure
  → preferred recipe name). Same file, same tolerant load.
- `save_eln_settings(settings, path=None)` writes the file back with the
  secrets (`include_secret=True`), creating the directory, `0o600` on
  POSIX. `ElnPublisher.reload_settings(settings)` swaps settings and
  restarts or stops the drain timer.
- `DraftEntry` gains `attachments: list[dict]` (`{"path", "comment"}`) and
  `source: str` (`"model"` | `"analysis"` | `"facts"`), tolerant in
  `from_dict`. The run record's `pending_eln_draft` therefore holds ANY
  entry awaiting approval — a model draft, an analysed entry, or a
  facts-only fallback. Its GLOSSARY entry is widened to **Pending entry**
  (the field name stays, for the record schema).
- `OutboxJob.attachments: list[dict]` and `Outbox._attach_data` attaches
  each (same size caps, link fallback), after the data file. The data file
  is attached only when the job says so (`attach_data_file`; today's path
  keeps `True`).
- `templates.render_analysed_body(report, facts, *, experiment_id,
  experiment_title, setup, data_path, findings)` — summary, results table,
  figure captions (with the attached file names), tables, warnings, then
  the compact provenance block; fact tables appended only when the report
  asks. Self-contained HTML like every other body.
- `ElnPublisher.on_run_finished`: when `settings.analysis.enabled` and an
  experiment is open, do NOT enqueue; emit `analysis_requested(run_id,
  manifest, data_path)` instead (the runner is connected to it). New
  `park_facts_entry(run_id, warning)` renders today's body and parks it as
  a pending entry with `source="facts"` — the fallback the runner calls on
  a failed report or a missing runner. `export_draft` stays and carries
  attachments through; `export_report(run_id, report)` renders an analysed
  entry and parks it (attended) — approval remains
  `ExperimentManager.approve_eln_draft(run_id)`, unchanged.
- `ExperimentStore.analysis_dir(experiment_id)` →
  `<root>/<experiment_id>/analysis`; recipes under `analysis/recipes/`,
  reports under `analysis/<run_id>/`. Same portable-folder rule as the
  outbox and the feed.

### The runner (`cryosoft/session/analysis_runner.py`)

`AnalysisRunner(QObject)`: `start(run_id, manifest, data_path, recipe="")`
builds the spec from the manager (experiment context, store paths), writes
it under the report directory, launches `sys.executable -m
cryosoft.analysis run --spec …` with `QProcess`, and bounds it with the
settings' `timeout_s` (a QTimer kills a runaway worker; the report is then
a synthesized `failed` one). On finish it reads `report.json`, hands it to
`publisher.export_report()` or, on failure, `publisher.park_facts_entry()`,
and emits `analysis_finished(run_id, report_dict)` /
`analysis_failed(run_id, error)`. One worker at a time; further requests
queue in order. No thread, no blocking wait, nothing on the tick.

### The GUI (`cryosoft/gui/`)

- `eln_settings_dialog.py` — **eLab setup**: enabled, backend (from
  `discover_backends()`), base URL, API key (password echo, "leave blank to
  keep"), team id, template (combo filled by "Fetch templates" after a
  successful "Test connection", which calls `adapter.verify()` and shows the
  identity the backend reports), verify TLS, auto-publish, tags, attachment
  cap; an **Analysis** group: on/off, timeout, include fact tables, attach
  data file. Save → `save_eln_settings` → `publisher.reload_settings`. The
  key never appears in a label, a log line or a tooltip. Reachable from the
  procedure window's eLab tab and from the Monitor window's Setup menu.
- `analysis_panel.py` — the procedure window's top-right quadrant becomes a
  `QTabWidget`: "Queue" (today's queue over status log) and **"eLab"**. The
  eLab tab shows: the publish state chip (synced/pending/offline/disabled,
  from `publish_state_changed`), an "Analysis on" toggle bound to the
  setting, "eLab setup…", the run under review (last finished run by
  default; a small combo of the open experiment's finished runs), the recipe
  combo (package + experiment recipes for that procedure, with the
  experiment ones marked), "New recipe…", "Run analysis", a preview
  (`QTextBrowser` rendering the pending entry's body, with the figures shown
  from their local files — preview only; the published body never embeds an
  image), the recipe's warnings/error, and "Publish" / "Discard". Publish
  calls `approve_eln_draft`; Discard clears the pending entry through the
  manager. The panel follows the GUI rules in `gui/README.md` and the
  offscreen screenshot check.
- `ProcedureWindow` gains optional `session_manager`, `eln_publisher`,
  `analysis_runner` constructor arguments; `MonitorWindow` passes them;
  `main.py` builds the runner beside the publisher.

### The agent surface (`cryosoft/session/gateway/`)

New session tools, hand-declared like the ELN ones:

| tool | class | what |
|---|---|---|
| `list_analysis_recipes` | READ | package + experiment recipes for the open experiment, with the one that would run for each procedure |
| `read_analysis_recipe` | READ | one experiment recipe's source |
| `write_analysis_recipe` | RUN_CONTROL, recorded | write/overwrite `<experiment>/analysis/recipes/<name>.py`; stamped header; refuses names that are not identifiers |
| `run_analysis` | RUN_CONTROL, recorded | start the runner for a run (optional recipe); returns `{started, report_path}` — the answer arrives later, poll `read_analysis_report` |
| `read_analysis_report` | READ | the latest `report.json` for a run, or `{"status": "running"/"none"}` |

`publish_eln_entry` is unchanged and already parks for approval when the
experiment is attended. `ToolContext` gains `analysis_runner` (duck-typed,
`start(...)`, `is_running(run_id)`).

### Standards to write down

- Recipe contract: `cryosoft/analysis/base.py` docstring + `README.md`
  (Purpose, layer, Entry, Exit, Interface contract, How to add, Files).
- Conformance: every package recipe declares a snake_case `name`, a
  non-empty `description`, a `procedures` tuple, and `analyse` with the
  contract's signature; runs to an `ok` report against a small synthetic
  run file; `AnalysisReport`/`AnalysisSpec` dict round-trip; the analysed
  body is self-contained HTML; `cryosoft/analysis` has a README; the new
  tools have schemas and action classes.
- GLOSSARY: **Analysis recipe**, **Analysis report**, **Analysis worker**,
  **Pending entry** (widened from Draft entry), **eLab setup**.

### Out of scope (say so, do not do)

Live analysis during a run (RunBuffer-fed), notebook-side templates,
recipes in a language other than Python, sandboxing the worker beyond
process isolation, an experiment-level (not per-run) entry.
