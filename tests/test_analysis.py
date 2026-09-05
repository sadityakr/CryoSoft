"""The analysis stage: the recipe contract, discovery, the runner and the worker.

Every test here writes its run file with the production ``DataManager`` and
reads it back through the production reader, exactly as ``test_l5_data_reader``
does, because a recipe's whole job is to agree with the file the application
actually writes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import cryosoft
from cryosoft.analysis.base import (
    MATPLOTLIB_INSTALL_HINT,
    AnalysisContext,
    AnalysisError,
    AnalysisRecipe,
    axis_label,
    choose_x_column,
    measured_columns,
)
from cryosoft.analysis.discovery import (
    ORIGIN_EXPERIMENT,
    ORIGIN_PACKAGE,
    RECIPE_TEMPLATE,
    RecipeInfo,
    discover_recipes,
    load_recipe,
    recipe_for,
    scaffold_recipe,
)
from cryosoft.analysis.report import (
    ANY_PROCEDURE,
    REPORT_FAILED,
    REPORT_FILENAME,
    REPORT_OK,
    AnalysisReport,
    AnalysisSpec,
)
from cryosoft.analysis.runner import read_report, run_spec, write_report
from cryosoft.core.data_manager import DataManager
from cryosoft.core.data_reader import open_run

REPO_ROOT = Path(cryosoft.__file__).parent.parent

N_POINTS = 5

DATA_CONFIG = {
    # The writer's own order: the clock, then the system read-backs, then the
    # procedure's own axis column LAST — which is what choose_x_column() reads.
    "sweep_columns": {"unix_time": "float", "temperature_K": "float", "field_T": "float"},
    "measurement_scalars": {"voltage_V": "float"},
    "measurement_arrays": {},
    "measurement_blocks": {},
    "loop_shape": [2, 1],
}

MANIFEST = {
    "run_id": "run-1",
    "procedure": "FieldSweep",
    "kind": "run",
    "params": {"field_start": -1.0, "field_end": 1.0},
    "status": "done",
    "started_utc": "2026-01-01T00:00:00+00:00",
    "finished_utc": "2026-01-01T00:05:00+00:00",
}


# ── Fixtures ──────────────────────────────────────────────────────────────


def _writer(directory: Path, n_sweep_points: int = N_POINTS) -> DataManager:
    """Return a ``DataManager`` writing the fixture layout into *directory*.

    Args:
        directory: Where the run file is created.
        n_sweep_points: How many points to pre-allocate.

    Returns:
        The open writer; the caller closes it.
    """
    return DataManager(
        data_directory=str(directory),
        procedure_name="FieldSweep",
        procedure_params={"field_start": -1.0, "field_end": 1.0, "loop1_values": [1e-6, -1e-6]},
        sample_info={"sample_name": "Test Sample", "sample_id": "TST-001"},
        instrument_state={"magnet_z": {"field": 0.0}},
        system_targets={"magnet_z": {"target": -1.0}},
        measurement_commands=[],
        data_config=DATA_CONFIG,
        n_sweep_points=n_sweep_points,
        experiment_info={
            "setup": {"config_name": "sim_setup"},
            "experiment": {"experiment_id": "EXP-1", "user_name": "A. Operator"},
        },
    )


@pytest.fixture
def run_file(tmp_path) -> Path:
    """A closed run file with five points of one sweep and one measurement."""
    writer = _writer(tmp_path / "data")
    for index in range(N_POINTS):
        writer.save_datapoint(
            index,
            {
                "unix_time": 1_000.0 + index,
                "temperature_K": 4.2 + 0.01 * index,
                "field_T": -1.0 + 0.5 * index,
                "voltage_V": [[1.0 + index], [2.0 + index]],
            },
            {},
        )
    writer.close()
    return Path(writer.filepath)


@pytest.fixture
def empty_run_file(tmp_path) -> Path:
    """A closed run file that never received a datapoint."""
    writer = _writer(tmp_path / "empty")
    writer.close()
    return Path(writer.filepath)


def _spec(run_file: Path, output_dir: Path, **overrides) -> AnalysisSpec:
    """Build a spec for the fixture run.

    Args:
        run_file: The run's HDF5 file.
        output_dir: Where the report goes.
        **overrides: Any ``AnalysisSpec`` field to override.

    Returns:
        The spec.
    """
    fields = {
        "run_id": "run-1",
        "data_path": str(run_file),
        "manifest": dict(MANIFEST),
        "experiment": {"experiment_id": "EXP-1", "experiment_title": "Sample A"},
        "setup": {"config_name": "sim_setup"},
        "output_dir": str(output_dir),
    }
    fields.update(overrides)
    return AnalysisSpec(**fields)


EXPERIMENT_RECIPE = '''
from cryosoft.analysis.base import AnalysisRecipe
from cryosoft.analysis.report import AnalysisReport


class LocalRecipe(AnalysisRecipe):
    name = "local_overview"
    procedures = ("FieldSweep",)
    description = "This experiment's own overview"

    def analyse(self, run, context):
        return AnalysisReport(summary=(f"local recipe saw {run.n_points} points",))
'''

OVERRIDING_RECIPE = '''
from cryosoft.analysis.base import AnalysisRecipe
from cryosoft.analysis.report import AnalysisReport


class MyGenericSweep(AnalysisRecipe):
    name = "generic_sweep"
    procedures = ("*",)
    description = "This experiment's replacement for the shipped overview"

    def analyse(self, run, context):
        return AnalysisReport(summary=("overridden",))
'''

BROKEN_RECIPE = "this is not python at all ((("

RAISING_RECIPE = '''
from cryosoft.analysis.base import AnalysisRecipe


class Boom(AnalysisRecipe):
    name = "boom"
    procedures = ("*",)
    description = "Always raises"

    def analyse(self, run, context):
        raise RuntimeError("the fit did not converge")
'''


# ── The context helpers ───────────────────────────────────────────────────


def test_context_figure_saves_a_png_and_closes_it(tmp_path):
    """figure() writes <output_dir>/<name>.png and returns a relative ref."""
    context = AnalysisContext(run_id="r", output_dir=tmp_path / "report")
    plt = context.pyplot()
    fig, axis = plt.subplots()
    axis.plot([0, 1], [0, 1])

    ref = context.figure("overview", fig, caption="Everything", width_px=600)

    assert ref.file == "overview.png"
    assert ref.caption == "Everything"
    assert ref.width_px == 600
    assert (tmp_path / "report" / "overview.png").is_file()
    assert not plt.get_fignums(), "figure() must close the figure it saved"


def test_context_figure_refuses_a_name_that_is_not_a_file_stem(tmp_path):
    """A name with a path separator or an extension is refused, not sanitised."""
    context = AnalysisContext(output_dir=tmp_path)
    plt = context.pyplot()
    fig = plt.figure()
    for bad in ("../escape", "sub/dir", "overview.png", ""):
        with pytest.raises(AnalysisError):
            context.figure(bad, fig)
    plt.close(fig)


def test_context_pyplot_names_the_install_command_when_matplotlib_is_absent(monkeypatch, tmp_path):
    """matplotlib is an optional extra, so its absence is one clear sentence."""
    monkeypatch.setitem(sys.modules, "matplotlib", None)
    context = AnalysisContext(output_dir=tmp_path)
    with pytest.raises(AnalysisError) as excinfo:
        context.pyplot()
    assert MATPLOTLIB_INSTALL_HINT in str(excinfo.value)


def test_context_table_applies_the_report_caps(tmp_path):
    """table() is TableSpec.build: capped rows, marked truncated."""
    context = AnalysisContext(output_dir=tmp_path)
    table = context.table("Big", ["a", "b"], [[i, i * 2] for i in range(200)])
    assert table.caption == "Big"
    assert table.columns == ("a", "b")
    assert len(table.rows) < 200
    assert table.truncated


def test_base_recipe_refuses_to_be_used_directly(tmp_path):
    """The base class is a contract, not a default implementation."""
    with pytest.raises(NotImplementedError):
        AnalysisRecipe().analyse(None, AnalysisContext(output_dir=tmp_path))


# ── The column conventions ────────────────────────────────────────────────


def test_choose_x_column_prefers_the_runs_own_sweep_axis(run_file):
    """The file's last-declared sweep column is the procedure's axis read-back."""
    with open_run(run_file) as run:
        assert choose_x_column(run, MANIFEST).name == "field_T"


def test_choose_x_column_honours_a_manifest_declaration(run_file):
    """A manifest naming the axis (default_x_key) wins over the file's guess."""
    with open_run(run_file) as run:
        chosen = choose_x_column(run, {**MANIFEST, "default_x_key": "temperature_K"})
    assert chosen.name == "temperature_K"


def test_measured_columns_leaves_out_the_axis_and_the_clocks(run_file):
    """The clocks are axes, not readings; so is whatever the caller excluded."""
    with open_run(run_file) as run:
        names = [info.name for info in measured_columns(run, exclude=["field_T"])]
    assert names == ["temperature_K", "voltage_V"]
    assert axis_label(None) == "point index"


# ── Discovery ─────────────────────────────────────────────────────────────


def test_discover_recipes_finds_the_shipped_recipes():
    """The package's own recipes are discovered, the overview first."""
    infos = discover_recipes()
    names = [info.name for info in infos]
    assert names[0] == "generic_sweep", "the default overview must be the first match"
    assert "facts_only" in names
    for info in infos:
        assert info.origin == ORIGIN_PACKAGE
        assert info.digest and len(info.digest) == 64
        assert Path(info.source_path).is_file()
        assert info.to_dict()["procedures"] == list(info.procedures)


def test_discover_recipes_loads_experiment_scripts(tmp_path):
    """A *.py in an extra directory becomes a recipe, marked as the experiment's."""
    (tmp_path / "local_overview.py").write_text(EXPERIMENT_RECIPE, encoding="utf-8")
    (tmp_path / "_helper.py").write_text(EXPERIMENT_RECIPE, encoding="utf-8")

    infos = discover_recipes([tmp_path])
    local = [info for info in infos if info.name == "local_overview"]

    assert len(local) == 1, "an underscore-prefixed script must be skipped"
    assert local[0].origin == ORIGIN_EXPERIMENT
    assert local[0].procedures == ("FieldSweep",)
    assert local[0].source_path == str(tmp_path / "local_overview.py")


def test_an_experiment_recipe_overrides_the_package_one_of_the_same_name(tmp_path):
    """The local answer wins, and the name is not listed twice."""
    (tmp_path / "mine.py").write_text(OVERRIDING_RECIPE, encoding="utf-8")

    infos = discover_recipes([tmp_path])
    matching = [info for info in infos if info.name == "generic_sweep"]

    assert len(matching) == 1
    assert matching[0].origin == ORIGIN_EXPERIMENT
    assert load_recipe(matching[0]).description.startswith("This experiment's replacement")


def test_a_broken_script_is_skipped_with_a_warning(tmp_path, caplog):
    """One half-written file must not hide every other recipe."""
    (tmp_path / "broken.py").write_text(BROKEN_RECIPE, encoding="utf-8")
    (tmp_path / "local_overview.py").write_text(EXPERIMENT_RECIPE, encoding="utf-8")

    with caplog.at_level("WARNING"):
        names = [info.name for info in discover_recipes([tmp_path])]

    assert "local_overview" in names
    assert "generic_sweep" in names
    assert any("broken.py" in record.getMessage() for record in caplog.records)


def test_discover_recipes_tolerates_a_missing_directory(tmp_path):
    """An experiment that never wrote a recipe is the normal case."""
    assert discover_recipes([tmp_path / "nope"]) == discover_recipes()


def test_recipe_for_preference_order(tmp_path):
    """preferred name, else the procedure's own recipe, else the wildcard one."""
    (tmp_path / "local_overview.py").write_text(EXPERIMENT_RECIPE, encoding="utf-8")
    infos = discover_recipes([tmp_path])

    assert recipe_for("FieldSweep", infos, preferred="facts_only").name == "facts_only"
    assert recipe_for("FieldSweep", infos).name == "local_overview"
    assert recipe_for("TimeSeries", infos).name == "generic_sweep"
    assert recipe_for("", infos).name == "generic_sweep"
    assert recipe_for("FieldSweep", ()) is None

    only_specific = (RecipeInfo(name="x", procedures=("FieldSweep",)),)
    assert recipe_for("TimeSeries", only_specific) is None


def test_load_recipe_refuses_a_vanished_source(tmp_path):
    """A recipe whose file has gone is an AnalysisError, not an ImportError."""
    with pytest.raises(AnalysisError):
        load_recipe(RecipeInfo(name="gone", source_path=str(tmp_path / "gone.py")))


# ── The scaffold ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "1bad", "class", "_private", "has-dash", "two words"])
def test_scaffold_recipe_refuses_a_name_that_is_not_an_identifier(tmp_path, bad):
    """The name becomes both an id and a file name, so it must be an identifier."""
    with pytest.raises(AnalysisError):
        scaffold_recipe(bad, tmp_path)


def test_scaffold_recipe_refuses_to_overwrite(tmp_path):
    """A scaffold must never destroy an analysis somebody wrote."""
    path = scaffold_recipe("keeper", tmp_path)
    path.write_text("# edited by a physicist\n", encoding="utf-8")

    with pytest.raises(AnalysisError):
        scaffold_recipe("keeper", tmp_path)

    assert path.read_text(encoding="utf-8") == "# edited by a physicist\n"


def test_scaffolded_recipe_imports_and_runs(tmp_path, run_file):
    """The template is runnable as written: discovered, loaded, and it analyses."""
    recipes_dir = tmp_path / "recipes"
    path = scaffold_recipe(
        "sample_overview",
        recipes_dir,
        procedure="FieldSweep",
        header="written by agent runner-7 at 2026-09-05T10:00:00Z",
    )
    written = path.read_text(encoding="utf-8")
    assert written.startswith("# written by agent runner-7")
    assert "$" not in written, "every template placeholder must have been substituted"
    assert "$name" in RECIPE_TEMPLATE, "the template itself is the unsubstituted source"

    infos = discover_recipes([recipes_dir])
    scaffolded = next(info for info in infos if info.name == "sample_overview")
    assert scaffolded.procedures == ("FieldSweep",)

    report = run_spec(
        _spec(run_file, tmp_path / "report", recipe="sample_overview", recipe_dirs=(str(recipes_dir),))
    )

    assert report.status == REPORT_OK, report.error
    assert report.recipe == "sample_overview"
    assert report.summary
    assert report.figures and (tmp_path / "report" / report.figures[0].file).is_file()


# ── The runner ────────────────────────────────────────────────────────────


def test_run_spec_ok_path(tmp_path, run_file):
    """The happy path: an ok report, stamped with its provenance, plus a figure."""
    output_dir = tmp_path / "report"
    report = run_spec(_spec(run_file, output_dir, options={"note": "hello"}))

    assert report.status == REPORT_OK, report.error
    assert report.ok
    assert report.run_id == "run-1"
    assert report.recipe == "generic_sweep"
    assert len(report.recipe_digest) == 64
    assert report.started_utc.endswith("+00:00")
    assert report.duration_s >= 0.0
    assert report.options == {"note": "hello"}
    assert report.summary and "FieldSweep" in report.summary[0]
    assert report.figures and (output_dir / report.figures[0].file).is_file()
    assert report.tables and report.tables[0].columns[0] == "Column"


def test_run_spec_applies_the_spec_defaults(tmp_path, run_file):
    """A recipe that left the two flags False inherits the spec's answer."""
    report = run_spec(
        _spec(run_file, tmp_path / "report", include_fact_tables=True, attach_data_file=True)
    )
    assert report.include_fact_tables
    assert report.attach_data_file


def test_run_spec_reports_a_raising_recipe_instead_of_raising(tmp_path, run_file):
    """A crashing recipe costs a visible failure, never a lost run."""
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "boom.py").write_text(RAISING_RECIPE, encoding="utf-8")

    report = run_spec(
        _spec(run_file, tmp_path / "report", recipe="boom", recipe_dirs=(str(recipes_dir),))
    )

    assert report.status == REPORT_FAILED
    assert report.recipe == "boom"
    assert report.recipe_digest
    assert "the fit did not converge" in report.error
    assert "Traceback" in report.error
    assert report.started_utc and report.run_id == "run-1"


def test_run_spec_reports_an_unknown_recipe(tmp_path, run_file):
    """A name nothing answers to is a failed report naming what does exist."""
    report = run_spec(_spec(run_file, tmp_path / "report", recipe="no_such_recipe"))

    assert report.status == REPORT_FAILED
    assert "no_such_recipe" in report.error
    assert "generic_sweep" in report.error


def test_run_spec_reports_a_missing_data_file(tmp_path):
    """An unreadable data file is a failed report, not an exception."""
    report = run_spec(_spec(tmp_path / "gone.h5", tmp_path / "report"))

    assert report.status == REPORT_FAILED
    assert "gone.h5" in report.error


def test_run_spec_reports_a_recipe_returning_junk(tmp_path, run_file):
    """A recipe that answers with something other than a report fails cleanly."""
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "junk.py").write_text(
        'from cryosoft.analysis.base import AnalysisRecipe\n\n\n'
        'class Junk(AnalysisRecipe):\n'
        '    name = "junk"\n'
        '    procedures = ("*",)\n'
        '    description = "returns a dict"\n\n'
        '    def analyse(self, run, context):\n'
        '        return {"summary": "nope"}\n',
        encoding="utf-8",
    )

    report = run_spec(
        _spec(run_file, tmp_path / "report", recipe="junk", recipe_dirs=(str(recipes_dir),))
    )

    assert report.status == REPORT_FAILED
    assert "AnalysisReport" in report.error


def test_run_spec_collects_context_warnings(tmp_path, run_file):
    """A note a recipe left on the context survives into the report."""
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir()
    (recipes_dir / "noisy.py").write_text(
        'from cryosoft.analysis.base import AnalysisRecipe\n'
        'from cryosoft.analysis.report import AnalysisReport\n\n\n'
        'class Noisy(AnalysisRecipe):\n'
        '    name = "noisy"\n'
        '    procedures = ("*",)\n'
        '    description = "warns"\n\n'
        '    def analyse(self, run, context):\n'
        '        context.warnings.append("a channel was missing")\n'
        '        return AnalysisReport(summary=("done",))\n',
        encoding="utf-8",
    )

    report = run_spec(
        _spec(run_file, tmp_path / "report", recipe="noisy", recipe_dirs=(str(recipes_dir),))
    )

    assert report.status == REPORT_OK
    assert "a channel was missing" in report.warnings


def test_report_round_trip_through_the_file(tmp_path, run_file):
    """write_report/read_report preserve the report; a missing one reads as None."""
    output_dir = tmp_path / "report"
    assert read_report(output_dir) is None

    report = run_spec(_spec(run_file, output_dir))
    path = write_report(report, output_dir)

    assert path == output_dir / REPORT_FILENAME
    assert read_report(output_dir) == report
    assert json.loads(path.read_text(encoding="utf-8"))["recipe"] == "generic_sweep"


def test_read_report_answers_none_for_corrupt_json(tmp_path):
    """A half-written report is 'no report yet', never an exception."""
    tmp_path.joinpath(REPORT_FILENAME).write_text("{not json", encoding="utf-8")
    assert read_report(tmp_path) is None


# ── The shipped recipes ───────────────────────────────────────────────────


def test_generic_sweep_on_a_normal_run(tmp_path, run_file):
    """One figure, one statistics table, one paragraph with span and duration."""
    report = run_spec(_spec(run_file, tmp_path / "report"))

    assert report.status == REPORT_OK
    assert len(report.figures) == 1
    table = report.tables[0]
    assert [row[0] for row in table.rows] == ["temperature_K", "voltage_V"]
    assert dict(zip(table.columns, table.rows[1]))["Unit"] == "V"
    assert {result.name for result in report.results} == {
        "field_T at first point",
        "field_T at last point",
    }
    summary = report.summary[0]
    assert "5 point(s)" in summary
    assert "-1 T to 1 T" in summary
    assert "300 s" in summary
    assert "'done'" in summary


def test_generic_sweep_on_a_run_with_no_points(tmp_path, empty_run_file):
    """An aborted-before-the-first-point run still yields an ok report."""
    report = run_spec(_spec(empty_run_file, tmp_path / "report"))

    assert report.status == REPORT_OK, report.error
    assert not report.figures
    assert any("no points" in warning for warning in report.warnings)
    assert report.summary


def test_generic_sweep_without_matplotlib(tmp_path, run_file, monkeypatch):
    """No matplotlib: an ok report, no figure, and the install command as a warning."""

    def _refuse() -> None:
        raise AnalysisError(f"matplotlib is not installed: {MATPLOTLIB_INSTALL_HINT}")

    monkeypatch.setattr("cryosoft.analysis.base._import_pyplot", _refuse)

    report = run_spec(_spec(run_file, tmp_path / "report"))

    assert report.status == REPORT_OK, report.error
    assert not report.figures
    assert any(MATPLOTLIB_INSTALL_HINT in warning for warning in report.warnings)
    assert report.tables, "everything that is not a figure is still produced"


def test_facts_only(tmp_path, run_file):
    """No figure, both flags set, one sentence."""
    report = run_spec(_spec(run_file, tmp_path / "report", recipe="facts_only"))

    assert report.status == REPORT_OK
    assert report.recipe == "facts_only"
    assert not report.figures
    assert not report.tables
    assert report.include_fact_tables
    assert report.attach_data_file
    assert len(report.summary) == 1
    assert "FieldSweep" in report.summary[0]


def test_every_shipped_recipe_serves_every_procedure():
    """Both shipped recipes are wildcards, so no run is left unanalysed."""
    for info in discover_recipes():
        assert ANY_PROCEDURE in info.procedures


# ── The worker CLI ────────────────────────────────────────────────────────


def _worker(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    """Run ``python -m cryosoft.analysis`` in a real subprocess.

    Args:
        *args: The command line after the module name.
        cwd: Working directory for the child.

    Returns:
        The completed process, with stdout and stderr captured as text.
    """
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), "MPLBACKEND": "Agg"}
    return subprocess.run(
        [sys.executable, "-m", "cryosoft.analysis", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_worker_run_writes_the_report_and_exits_zero(tmp_path, run_file):
    """End to end: spec file in, report.json and a PNG out, path on stdout."""
    output_dir = tmp_path / "report"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(_spec(run_file, output_dir).to_dict()), encoding="utf-8")

    result = _worker("run", "--spec", str(spec_path), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(output_dir / REPORT_FILENAME)
    report = read_report(output_dir)
    assert report is not None and report.status == REPORT_OK
    assert (output_dir / report.figures[0].file).is_file()


def test_worker_run_exits_zero_for_a_failed_report(tmp_path, run_file):
    """A failed analysis is a complete answer; the caller reads the report."""
    output_dir = tmp_path / "report"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps(_spec(run_file, output_dir, recipe="no_such_recipe").to_dict()),
        encoding="utf-8",
    )

    result = _worker("run", "--spec", str(spec_path), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    report = read_report(output_dir)
    assert report is not None and report.status == REPORT_FAILED


def test_worker_run_exits_two_for_an_unreadable_spec(tmp_path):
    """No spec means no answer to write — the one non-zero exit."""
    result = _worker("run", "--spec", str(tmp_path / "nope.json"), cwd=tmp_path)
    assert result.returncode == 2, result.stderr
    assert "nope.json" in result.stderr


def test_worker_list_and_new_recipe(tmp_path):
    """list prints one row per recipe; new-recipe writes one and list sees it."""
    listing = _worker("list", cwd=tmp_path)
    assert listing.returncode == 0, listing.stderr
    assert "generic_sweep" in listing.stdout
    assert "facts_only" in listing.stdout

    created = _worker(
        "new-recipe", "sample_overview", "--dir", str(tmp_path / "recipes"), cwd=tmp_path
    )
    assert created.returncode == 0, created.stderr
    assert Path(created.stdout.strip()).is_file()

    again = _worker(
        "new-recipe", "sample_overview", "--dir", str(tmp_path / "recipes"), cwd=tmp_path
    )
    assert again.returncode == 2
    assert "already exists" in again.stderr

    listing = _worker("list", "--dir", str(tmp_path / "recipes"), cwd=tmp_path)
    assert "sample_overview" in listing.stdout
    assert "experiment" in listing.stdout


# ── Report values ─────────────────────────────────────────────────────────


def test_report_dict_round_trip_is_json_safe(tmp_path, run_file):
    """A real report survives json.dumps and AnalysisReport.from_dict unchanged."""
    report = run_spec(_spec(run_file, tmp_path / "report"))
    payload = json.loads(json.dumps(report.to_dict()))
    assert AnalysisReport.from_dict(payload) == report
    assert np.isfinite(report.duration_s)
