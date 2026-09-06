# i2as/analysis — the analysis stage between a finished run and its notebook entry

## Purpose

Turn one finished **run** into the *analysed* content a physicist actually
wants in a lab notebook — one or two figures, a handful of derived numbers, a
few sentences and a compact pointer back to the data — instead of the run's
raw fact tables and its HDF5 file.

The code that does that is an **analysis recipe**: a small class, either
shipped here in `recipes/` or written at runtime into an experiment's own
`analysis/recipes/` folder by a physicist or an agent. A recipe reads one run
and returns one **analysis report**; it runs in the **analysis worker**, a
separate process (`python -m i2as.analysis run --spec <file>`) that can
reach the run's data file and nothing else.

That separation is the point. A recipe is user code: it may take seconds and
it may crash. The single hardware thread standard already puts every network
call and every analysis on the client side of the control contract; a
subprocess is the same idea one step further — like the MCP adapter, this
package cannot reach an instrument because it cannot import one.

## Architecture layer

**Beside the session layer, above the engine.** It imports exactly three
things from the package — `core.data_reader`, `core.events`,
`core.exceptions` — plus numpy, h5py, the standard library, and matplotlib
lazily. It never imports the Station, the Orchestrator, a driver, a virtual
instrument, a procedure, the session layer or the GUI, and nothing below the
session layer imports it. Import contracts **C22** and **C23** enforce both
directions.

```
Orchestrator ── run finished ──▶ session/eln publisher
                                     │
                                     ▼
                        session/analysis_runner (QProcess)
                                     │  spec.json
                                     ▼
                  python -m i2as.analysis run --spec …      ← this package
                                     │  report.json + figures
                                     ▼
                        pending entry ▸ preview ▸ publish
```

## Entry (what comes in)

- **An `AnalysisSpec`** (`report.py`), as a JSON file named by
  `run --spec`: the run id, the absolute path of its HDF5 file, the run
  manifest, the experiment and setup facts, the recipe name (or `""` to
  choose by procedure), the extra recipe directories, the output directory,
  the options, and the two report defaults. It never carries a credential and
  never a live object.
- **One run's HDF5 file**, opened read-only through
  `core.data_reader.open_run()` and read only through the **Run source**
  vocabulary — the same four questions an agent's run-reading tools ask.
- **Recipe scripts**: this package's own `recipes/*.py`, and every `*.py` in
  each directory the spec names (an experiment's `analysis/recipes/`).

## Exit (what goes out)

- **One `AnalysisReport`** per run, written as `<output_dir>/report.json`
  atomically, and always written — a recipe that raises produces a `failed`
  report carrying its traceback, so the failure is visible where the result
  would have been and the notebook can fall back to a facts-only entry
  instead of losing the run.
- **PNG figures** beside it, referenced by `FigureRef.file` relative to that
  directory, so a report copied with its experiment folder still finds them.
- **A scaffolded recipe file** (`new-recipe`), written into a directory the
  caller names, never overwriting.
- **Log lines on stderr**, and on stdout exactly one answer per command (the
  report path, the scaffolded path, or one row per recipe).

## Interface contract

- **The recipe contract** is written in `base.py`'s module docstring and
  machine-checked by the analysis conformance tests: one class per recipe
  declaring a snake_case `name`, a non-empty `description` and a `procedures`
  tuple of procedure names — class or display name, matched case- and punctuation-insensitively by `procedure_key()` (or `("*",)`); `analyse(self, run, context)
  -> AnalysisReport`; instantiated with no arguments; reading the run only
  through the **Run source** vocabulary; making figures and tables only
  through the context.
- **A failure is data, not an exception.** `run_spec()` catches everything —
  a raising recipe, a recipe returning junk, an unknown recipe name, a
  missing data file — into a `REPORT_FAILED` report whose `error` holds the
  traceback. The worker therefore exits 0 even when the analysis failed;
  exit 2 means there was no answer to write at all.
- **Every report says which code produced it.** `recipe`, `recipe_digest`
  (SHA-256 of the recipe's source file), `started_utc`, `duration_s` and the
  `options` are stamped by the runner, never by the recipe.
- **Recipes are discovered, not listed.** `discover_recipes()` walks
  `recipes/` for `AnalysisRecipe` subclasses and loads every `*.py` in the
  extra directories by file path — the same auto-discovery idiom as drivers,
  VIs and procedures — so a new recipe is selectable the moment its file
  exists, with no core change. Order is the selection order: package recipes
  come back by declared `priority` (highest first, then by name), an
  experiment recipe REPLACES a package recipe of the same `name`, and a
  script that fails to import is skipped with a WARNING rather than hiding
  every other recipe.
- **matplotlib is the optional `analysis` extra**, imported lazily with the
  `Agg` backend selected first. A checkout without it imports, lints and
  tests unchanged; a recipe asking for a figure gets one `AnalysisError`
  naming `pip install i2as[analysis]`, and a well-behaved recipe turns
  that into a warning and returns a report without a figure.
- **Reports are bounded.** `report.py`'s caps apply to every report, and
  `context.table()` truncates and says so. Bulk data stays in the HDF5 file,
  which a report can ask to attach.
- **The scaffold refuses rather than overwrites.** `scaffold_recipe()`
  rejects a name that is not a Python identifier and a file that already
  exists — which is what makes it safe to expose as an agent tool.
- **Nothing here writes an experiment record, publishes an entry, or touches
  hardware.** The report is returned as data; what to do with it is the
  session layer's decision.

## How to add a new module

1. **A new shipped recipe** is one leaf module in `recipes/`, holding one
   `AnalysisRecipe` subclass. Declare `name`, `description` and `procedures`,
   implement `analyse()`, and read the run only through the run source.
   Conformance covers it the moment the file exists, and no core code
   changes. Put it here only if it serves a whole family of runs and carries
   no experiment-specific physics; an analysis only one experiment wants
   belongs in that experiment's own `analysis/recipes/` folder, where it
   overrides a shipped recipe of the same `name`.
2. **A new recipe for one experiment** is `python -m i2as.analysis
   new-recipe <name> --dir <experiment>/analysis/recipes` (the same
   `scaffold_recipe()` the panel's "New recipe…" button and the agent's
   `write_analysis_recipe` tool call), then edit the commented template.
3. **A new shared convention** — how to pick an axis, how to label a column —
   goes in `base.py` beside `choose_x_column()`/`measured_columns()` and into
   the recipe contract's docstring, so every future recipe and the scaffold
   template inherit it.
4. **Never** add an import of the Station, the Orchestrator, a driver, a VI, a
   procedure, the session layer or the GUI (C22 will refuse it), never add a
   required dependency for figures, and never let a recipe write outside
   `context.output_dir`.
5. New behaviour needs its own tests in `tests/test_analysis.py`; conformance
   coverage is necessary but not sufficient.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `base.py` | The recipe contract: what a recipe is, what it may import, the figure/table helpers, and the shared column conventions. | `AnalysisRecipe`, `AnalysisContext`, `AnalysisError`, `choose_x_column`, `measured_columns`, `axis_label`, `is_numeric`, `FIGURE_DPI`, `MATPLOTLIB_INSTALL_HINT` | `tests/test_conformance.py`, `tests/test_analysis.py` |
| `report.py` | The analysis report standard and the worker's request: frozen, JSON-safe, tolerant `from_dict`, size caps applied before the report is written. | `AnalysisReport`, `AnalysisSpec`, `ResultValue`, `FigureRef`, `TableSpec`, `REPORT_OK`/`REPORT_FAILED`, `REPORT_FILENAME`, `SPEC_FILENAME`, `RECIPES_DIRNAME`, `ANY_PROCEDURE`, the `MAX_*` caps | `tests/test_analysis.py` |
| `discovery.py` | Finding recipes (package and experiment), fingerprinting them, choosing one for a procedure, loading it, and scaffolding a new one. | `RecipeInfo`, `discover_recipes`, `recipe_for`, `load_recipe`, `scaffold_recipe`, `RECIPE_TEMPLATE`, `ORIGIN_PACKAGE`/`ORIGIN_EXPERIMENT` | `tests/test_analysis.py` |
| `runner.py` | Running one spec in process and never raising; writing and reading `report.json`. | `run_spec`, `write_report`, `read_report` | `tests/test_analysis.py` |
| `__main__.py` | The analysis worker CLI: `run --spec`, `new-recipe`, `list`; stdout is the answer, stderr the log, exit 2 only for a request that cannot be served. | `main`, `EXIT_BAD_REQUEST` | `tests/test_analysis.py` |
| `recipes/generic_sweep.py` | The shipped overview of any run: stacked subplots against the run's own axis, a per-column statistics table, one paragraph. | `GenericSweepRecipe` (`name="generic_sweep"`) | `tests/test_analysis.py`, `tests/test_conformance.py` |
| `recipes/facts_only.py` | The pre-analysis notebook entry, kept selectable: no figure, fact tables and data file requested. | `FactsOnlyRecipe` (`name="facts_only"`) | `tests/test_analysis.py`, `tests/test_conformance.py` |
| `recipes/field_image_stack.py` | The shipped recipe for one procedure (`FieldImaging`, matched by class or display name): a montage of at most 12 frames against field read through `run.read_image()`, difference images against the **reference frame** (frame 0), and the ROI-mean hysteresis loop with the zero crossings of the normalised loop reported as coercive-field `ResultValue`s; ok without frames, without points and without matplotlib. | `FieldImageStackRecipe` (`name="field_image_stack"`), `coercive_fields()` | `tests/test_analysis.py`, `tests/test_analysis_end_to_end.py`, `tests/test_conformance.py` |
