"""End-to-end: a finished run → the real analysis worker → an analysed entry
→ human approval → the (sim) notebook.

The layer suites test each half against a stand-in (the runner against a fake
worker, the worker against a spec file, the publisher against a fake report).
This test wires the REAL pieces together the way ``cryosoft.main`` does —
``ElnPublisher.analysis_requested`` → ``AnalysisRunner.start`` →
``python -m cryosoft.analysis run`` → ``export_report`` → the pending entry →
``approve_eln_draft`` → the outbox drain — over a real HDF5 run file written
by the data manager, and asserts that only the analysed, concise entry with
its figure reaches the notebook.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from cryosoft.analysis.report import REPORT_FILENAME, AnalysisReport
from cryosoft.core.data_manager import DataManager
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import build_station
from cryosoft.session.analysis_runner import AnalysisRunner
from cryosoft.session.eln.outbox import DRAIN_PUBLISHED
from cryosoft.session.eln.publisher import ElnPublisher
from cryosoft.session.eln.settings import AnalysisSettings, ElnSettings
from cryosoft.session.eln.sim_eln import SimElnAdapter
from cryosoft.session.manager import ExperimentManager
from cryosoft.session.models import User
from cryosoft.session.store import ExperimentStore, UserRoster

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"), reason="POSIX subprocess semantics assumed"
)

_DATA_CONFIG = {
    "sweep_columns": {"unix_time": "float", "field_T": "float"},
    "measurement_scalars": {"voltage_V": "float"},
    "measurement_arrays": {},
    "measurement_blocks": {},
    "loop_shape": [1, 1],
}


def _write_run_file(directory: Path, n_points: int = 6) -> Path:
    """Write a small closed sweep run file and return its path."""
    writer = DataManager(
        data_directory=str(directory),
        procedure_name="Field Sweep",
        procedure_params={"field_start": -1.0, "field_end": 1.0},
        sample_info={"sample_name": "A3"},
        instrument_state={},
        system_targets={},
        measurement_commands=[],
        data_config=_DATA_CONFIG,
        n_sweep_points=n_points,
        experiment_info={"setup": {"config_name": "sim_cryostat"}, "experiment": {}},
    )
    for index in range(n_points):
        writer.save_datapoint(
            index,
            {
                "unix_time": 1_000.0 + index,
                "field_T": -1.0 + 0.4 * index,
                "voltage_V": [[0.1 * index]],
            },
            {},
        )
    writer.close()
    return Path(writer.filepath)


@pytest.fixture
def wired(tmp_path, qtbot, monkeypatch):
    """The production wiring over a real run file and a sim notebook."""
    monkeypatch.setenv("PYTHONPATH", os.getcwd())
    store = ExperimentStore(tmp_path / "experiments")
    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe"))
    orchestrator = Orchestrator(build_station("cryosoft/configs/sim_cryostat"), tick_interval_ms=10)
    manager = ExperimentManager(
        store=store, roster=roster, orchestrator=orchestrator, config_name="sim_cryostat"
    )
    experiment = manager.start_experiment("Sample A", "jdoe", {"sample_name": "A3"})
    data_file = _write_run_file(store.data_dir(experiment.experiment_id))

    started = {
        "run_id": "run-0001",
        "procedure": "Field Sweep",
        "kind": "run",
        "params": {"field_start": -1.0, "field_end": 1.0},
        "data_file": str(data_file),
        "started_utc": "2026-01-01T10:00:00+00:00",
    }
    orchestrator.run_started.emit(started)
    finished = dict(started, finished_utc="2026-01-01T10:05:00+00:00", status="done", reason="")

    settings = ElnSettings(
        enabled=True,
        backend="sim_eln",
        base_url="https://sim.example",
        api_key="k",
        retry_base_s=0.0,
        retry_max_s=0.0,
        analysis=AnalysisSettings(enabled=True, timeout_s=120.0),
    )
    adapter = SimElnAdapter({})
    publisher = ElnPublisher(manager, settings, adapter=adapter)
    manager.attach_eln_publisher(publisher)
    orchestrator.run_finished.connect(publisher.on_run_finished)
    runner = AnalysisRunner(manager, publisher, lambda: publisher.settings)
    publisher.analysis_requested.connect(runner.start)

    yield manager, publisher, adapter, runner, orchestrator, finished, experiment.experiment_id
    runner.cancel()
    publisher.stop()


def test_a_finished_run_reaches_the_notebook_as_an_analysed_entry(wired, qtbot):
    """Run end → worker → parked entry → approval → one entry with one figure."""
    manager, publisher, adapter, runner, orchestrator, finished, experiment_id = wired

    with qtbot.waitSignal(runner.analysis_finished, timeout=90_000) as blocker:
        orchestrator.run_finished.emit(finished)

    # Nothing was queued at run end: analysis is on, so approval is the gate.
    assert publisher.pending_count() == 0
    run_id, payload = blocker.args
    assert run_id == "run-0001"
    report = AnalysisReport.from_dict(payload)
    assert report.ok, report.error
    assert report.recipe == "generic_sweep"
    report_dir = manager.store.report_dir(experiment_id, run_id)
    assert (report_dir / REPORT_FILENAME).is_file()
    assert report.figures, "the generic sweep recipe draws one overview figure"
    figure = report_dir / report.figures[0].file
    assert figure.is_file() and figure.stat().st_size > 0

    pending = manager.pending_eln_draft(run_id)
    assert pending["source"] == "analysis"
    assert "Provenance" in pending["body_html"] or "provenance" in pending["body_html"].lower()
    assert "<img" not in pending["body_html"], "the published body never embeds an image"
    assert "Parameters" not in pending["body_html"], "fact tables are opt-in"
    assert [a["path"] for a in pending["attachments"]] == [str(figure)]

    # The human approves in the eLab tab; the ordinary drain publishes it.
    job_id = manager.approve_eln_draft(run_id)
    assert job_id
    assert publisher.drain_once().state == DRAIN_PUBLISHED
    assert len(adapter.entries) == 1
    entry = next(iter(adapter.entries.values()))
    assert entry["body_html"] == pending["body_html"]
    uploaded = [Path(u["path"]).name for u in adapter.uploads]
    assert uploaded == [figure.name], "the figure is attached, the raw data file is not"
    assert manager.pending_eln_draft(run_id) == {}
    assert manager.current_experiment().find_run(run_id).eln_link is not None


def test_a_failing_recipe_parks_a_facts_only_entry(wired, qtbot):
    """A broken experiment recipe never loses the run: facts fall back, flagged."""
    manager, publisher, _adapter, runner, orchestrator, finished, experiment_id = wired
    recipes_dir = manager.store.recipes_dir(experiment_id)
    recipes_dir.mkdir(parents=True)
    (recipes_dir / "broken.py").write_text(
        "from cryosoft.analysis.base import AnalysisRecipe\n"
        "class Broken(AnalysisRecipe):\n"
        "    name = 'broken'\n"
        "    procedures = ('Field Sweep',)\n"
        "    description = 'raises'\n"
        "    def analyse(self, run, context):\n"
        "        raise ZeroDivisionError('boom')\n",
        encoding="utf-8",
    )

    with qtbot.waitSignal(runner.analysis_failed, timeout=90_000) as blocker:
        orchestrator.run_finished.emit(finished)

    run_id, error = blocker.args
    assert "ZeroDivisionError" in error
    pending = manager.pending_eln_draft(run_id)
    assert pending["source"] == "facts"
    assert "ZeroDivisionError" in pending["body_html"]
    assert publisher.pending_count() == 0, "nothing publishes without approval"
