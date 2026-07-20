# ---
# description: |
#   Behavior tests for the L6 Session Management layer (cryosoft/session/):
#   model round-trips and envelope (de)serialisation, ExperimentStore /
#   UserRoster disk behavior (atomicity, tolerance, lazy creation, active
#   pointer), and the SessionManager lifecycle — experiment start/close,
#   automatic run recording from real Orchestrator manifests, envelope
#   installation, attendance, crash resume, and an end-to-end run whose
#   RunRecord is cross-checked against the HDF5 file on disk.
# last_updated: 2026-07-17
# ---

import json

import h5py
import pytest

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.plan import EnvelopeBound, SessionEnvelope
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep import FieldSweep
from cryosoft.session.manager import SessionManager
from cryosoft.session.models import (
    EXPERIMENT_STATUS_CLOSED,
    RUN_STATUS_DONE,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    ElnLink,
    ExperimentRecord,
    RunRecord,
    User,
    envelope_from_dict,
    envelope_to_dict,
)
from cryosoft.session.store import ExperimentStore, UserRoster

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {"sample_name": "Hall bar A3", "sample_id": "A3", "comments": "test"}

FAST_PARAMS = {
    "measurement_vi": "keithley_delta_mode",
    "field_start": -0.1,
    "field_end": 0.1,
    "field_steps": 3,
    "temperature": 300.0,
    "current": 1e-6,
    "n_readings": 5,
    "init_wait": 0.0,
    "step_wait": 0.0,
}


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


@pytest.fixture
def orchestrator(station, qtbot):
    return Orchestrator(station, tick_interval_ms=10)


@pytest.fixture
def store(tmp_path):
    return ExperimentStore(tmp_path / "experiments")


@pytest.fixture
def roster(tmp_path):
    r = UserRoster(tmp_path / "users.json")
    r.add(User(user_id="jdoe", name="J. Doe", email="jdoe@example.org"))
    return r


@pytest.fixture
def manager(store, roster, orchestrator, station):
    return SessionManager(
        store=store,
        roster=roster,
        orchestrator=orchestrator,
        station=station,
        config_name="sim_cryostat",
    )


# ── Models ───────────────────────────────────────────────────────────────────

def test_experiment_record_round_trips_with_content():
    """A populated record survives to_dict()/from_dict() unchanged."""
    record = ExperimentRecord(
        experiment_id="20260717_test",
        title="Test",
        user_id="jdoe",
        sample_info=dict(SAMPLE_INFO),
        config_name="sim_cryostat",
        created_utc="2026-07-17T12:00:00+00:00",
        attended=False,
        envelope={"magnet_z": {"min_value": -2.0, "max_value": 2.0, "state_key": ""}},
        runs=[RunRecord(run_id="r1", procedure="Field Sweep", status=RUN_STATUS_DONE)],
        findings="looks superconducting",
        eln_link=ElnLink(backend="elabftw", entry_id="42", url="https://eln/42"),
    )
    assert ExperimentRecord.from_dict(record.to_dict()) == record


def test_run_record_untrusted_status_degrades_to_failed():
    """An unknown status must not masquerade as live or successful work."""
    run = RunRecord.from_dict({"run_id": "r1", "status": "totally-bogus"})
    assert run.status == RUN_STATUS_FAILED


def test_experiment_record_untrusted_status_degrades_to_closed():
    """A record with an unknown status must not resume as the live experiment."""
    record = ExperimentRecord.from_dict({"experiment_id": "x", "status": "bogus"})
    assert record.status == EXPERIMENT_STATUS_CLOSED


def test_envelope_round_trip_and_junk_tolerance():
    """envelope_to_dict()/envelope_from_dict() round-trip; junk drops to None."""
    envelope = SessionEnvelope(
        bounds={
            "magnet_z": EnvelopeBound(min_value=-2.0, max_value=2.0),
            "temperature_sample": EnvelopeBound(min_value=4.0, state_key="temperature"),
        }
    )
    rebuilt = envelope_from_dict(envelope_to_dict(envelope))
    assert rebuilt == envelope
    assert envelope_to_dict(None) == {}
    assert envelope_from_dict({}) is None
    assert envelope_from_dict("junk") is None
    # Structurally dict-like but invalid bounds -> dropped with a warning.
    assert envelope_from_dict({"magnet_z": {"min_value": 5.0, "max_value": 1.0}}) is None


# ── ExperimentStore / UserRoster ─────────────────────────────────────────────

def test_store_creates_nothing_until_save(tmp_path):
    """Construction and reads must not create directories (lazy creation)."""
    root = tmp_path / "experiments"
    store = ExperimentStore(root)
    assert store.list_experiments() == []
    assert store.get_active() is None
    assert store.load("nope") is None
    assert not root.exists()


def test_store_save_load_list_and_active_pointer(store):
    record = ExperimentRecord(experiment_id="20260717_x", title="X")
    store.save(record)
    store.set_active("20260717_x")
    assert store.list_experiments() == ["20260717_x"]
    assert store.load("20260717_x") == record
    assert store.get_active() == "20260717_x"
    store.set_active(None)
    assert store.get_active() is None
    # No stray .tmp files after atomic writes.
    assert not list(store.root.rglob("*.tmp"))


def test_store_load_tolerates_corrupt_file(store):
    path = store.root / "bad" / "experiment.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.load("bad") is None
    assert "bad" in store.list_experiments()  # listed (folder exists) but unloadable


def test_store_make_experiment_id_slug_and_collisions(store):
    created = "2026-07-17T12:00:00+00:00"
    first = store.make_experiment_id("Hall bar A3 — SOT!", created)
    assert first == "20260717_hall_bar_a3_sot"
    store.save(ExperimentRecord(experiment_id=first))
    assert store.make_experiment_id("Hall bar A3 — SOT!", created) == f"{first}_2"


def test_roster_add_get_replace(tmp_path):
    roster = UserRoster(tmp_path / "users.json")
    assert roster.list_users() == []
    roster.add(User(user_id="jdoe", name="J. Doe"))
    roster.add(User(user_id="asmith", name="A. Smith"))
    assert {u.user_id for u in roster.list_users()} == {"jdoe", "asmith"}
    roster.add(User(user_id="jdoe", name="Jay Doe"))  # replace, not duplicate
    assert roster.get("jdoe").name == "Jay Doe"
    assert len(roster.list_users()) == 2
    assert roster.get("nobody") is None


# ── SessionManager lifecycle ─────────────────────────────────────────────────

def test_experiment_context_setup_tier_present_with_no_experiment_open(manager):
    """The setup tier is available even before any experiment is ever started."""
    context = manager.experiment_context()
    assert context == {"setup": {"config_name": "sim_cryostat", "instruments": {}}, "experiment": {}}


def test_experiment_context_includes_instrument_metadata_from_config(
    store, roster, orchestrator, station
):
    """config_path wires read_instrument_metadata() into the setup tier."""
    manager = SessionManager(
        store=store,
        roster=roster,
        orchestrator=orchestrator,
        station=station,
        config_name="sim_cryostat",
        config_path=CONFIG_PATH,
    )
    instruments = manager.experiment_context()["setup"]["instruments"]
    assert instruments  # sim_cryostat/devices.yaml carries metadata for every VI
    assert instruments["magnet_z"]["role"] == "X-axis magnet"


def test_experiment_context_tolerates_missing_config_path(store, roster, orchestrator, station):
    """A bad/absent config_path degrades to no instrument metadata, never raises."""
    manager = SessionManager(
        store=store,
        roster=roster,
        orchestrator=orchestrator,
        station=station,
        config_path="/no/such/config",
    )
    assert manager.experiment_context()["setup"]["instruments"] == {}


def test_start_experiment_persists_and_installs_envelope(manager, orchestrator, store):
    envelope = SessionEnvelope(
        bounds={"magnet_z": EnvelopeBound(min_value=-2.0, max_value=2.0)}
    )
    changed: list[dict] = []
    manager.experiment_changed.connect(changed.append)

    record = manager.start_experiment(
        "SOT switching vs T", "jdoe", SAMPLE_INFO, envelope=envelope
    )

    assert store.get_active() == record.experiment_id
    assert store.load(record.experiment_id) == record
    assert record.config_name == "sim_cryostat"
    assert orchestrator._session_envelope == envelope
    assert changed and changed[-1]["experiment_id"] == record.experiment_id
    context = manager.experiment_context()
    assert context["setup"]["config_name"] == "sim_cryostat"
    assert context["experiment"]["experiment_id"] == record.experiment_id
    assert context["experiment"]["user_name"] == "J. Doe"
    assert context["experiment"]["attended"] is True
    assert context["experiment"]["eln_link"] == {}


def test_start_experiment_rejects_unknown_user_and_double_open(manager):
    with pytest.raises(ValueError, match="Unknown user"):
        manager.start_experiment("X", "nobody", SAMPLE_INFO)
    manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    with pytest.raises(ValueError, match="still open"):
        manager.start_experiment("Y", "jdoe", SAMPLE_INFO)


def test_close_experiment_clears_envelope_and_context(manager, orchestrator, store):
    envelope = SessionEnvelope(bounds={"magnet_z": EnvelopeBound(max_value=2.0)})
    record = manager.start_experiment("X", "jdoe", SAMPLE_INFO, envelope=envelope)
    manager.close_experiment()

    assert manager.current_experiment() is None
    context = manager.experiment_context()
    assert context["experiment"] == {}
    assert context["setup"]["config_name"] == "sim_cryostat"
    assert orchestrator._session_envelope is None
    assert store.get_active() is None
    stored = store.load(record.experiment_id)
    assert stored.status == EXPERIMENT_STATUS_CLOSED
    assert stored.closed_utc


def test_set_findings_and_attendance_persist(manager, store):
    record = manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    manager.set_findings("R(T) shows a clean transition at 9.1 K")
    manager.set_attended(False)
    stored = store.load(record.experiment_id)
    assert stored.findings.startswith("R(T)")
    assert stored.attended is False


def test_runs_outside_experiment_are_not_recorded(manager, orchestrator):
    orchestrator.run_started.emit({"run_id": "r1", "procedure": "Field Sweep"})
    assert manager.current_experiment() is None


def test_run_recording_from_manifests(manager, orchestrator, store):
    record = manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    recorded: list[dict] = []
    manager.run_recorded.connect(recorded.append)

    orchestrator.run_started.emit(
        {
            "run_id": "r1",
            "procedure": "Field Sweep",
            "kind": "run",
            "params": {"field_steps": 3},
            "data_file": "/data/x.h5",
            "started_utc": "2026-07-17T12:00:00+00:00",
        }
    )
    stored = store.load(record.experiment_id)
    assert len(stored.runs) == 1
    assert stored.runs[0].status == RUN_STATUS_RUNNING

    orchestrator.run_finished.emit(
        {
            "run_id": "r1",
            "finished_utc": "2026-07-17T12:05:00+00:00",
            "status": "done",
            "reason": "",
        }
    )
    stored = store.load(record.experiment_id)
    run = stored.runs[0]
    assert run.status == RUN_STATUS_DONE
    assert run.finished_utc
    assert len(recorded) == 2


def test_resume_marks_stale_running_runs_failed(
    manager, store, roster, station, qtbot
):
    """A new manager resumes the active experiment; crashed runs become failed."""
    record = manager.start_experiment(
        "X",
        "jdoe",
        SAMPLE_INFO,
        envelope=SessionEnvelope(bounds={"magnet_z": EnvelopeBound(max_value=2.0)}),
    )
    # Simulate the app dying mid-run: a run stuck in "running" on disk.
    record.runs.append(RunRecord(run_id="r1", status=RUN_STATUS_RUNNING))
    store.save(record)

    fresh_orchestrator = Orchestrator(station, tick_interval_ms=10)
    resumed = SessionManager(
        store=store,
        roster=roster,
        orchestrator=fresh_orchestrator,
        station=station,
        config_name="sim_cryostat",
    )
    experiment = resumed.current_experiment()
    assert experiment is not None
    assert experiment.experiment_id == record.experiment_id
    run = experiment.find_run("r1")
    assert run.status == RUN_STATUS_FAILED
    assert "restart" in run.reason
    # The stored envelope was re-installed on the new orchestrator.
    assert fresh_orchestrator._session_envelope is not None
    # And the failure was persisted, not just held in memory.
    assert store.load(record.experiment_id).find_run("r1").status == RUN_STATUS_FAILED


def test_resume_with_missing_record_clears_pointer(store, roster, orchestrator, station):
    store.set_active("ghost")
    manager = SessionManager(
        store=store,
        roster=roster,
        orchestrator=orchestrator,
        station=station,
    )
    assert manager.current_experiment() is None
    assert store.get_active() is None


# ── End-to-end: a real run recorded and cross-checked against HDF5 ───────────

def test_end_to_end_run_recorded_and_stamped(
    manager, orchestrator, station, store, tmp_path, qtbot
):
    """A real FieldSweep run produces a RunRecord matching the HDF5 on disk."""
    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    record = manager.start_experiment(
        "SOT switching vs T",
        "jdoe",
        SAMPLE_INFO,
        envelope=SessionEnvelope(
            bounds={"magnet_z": EnvelopeBound(min_value=-2.0, max_value=2.0)}
        ),
    )
    # Exactly what the GUI does when building a procedure: stamp the context.
    procedure = FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        experiment_info=manager.experiment_context(),
        **FAST_PARAMS,
    )
    orchestrator.run_procedure(procedure)
    with qtbot.waitSignal(orchestrator.procedure_finished, timeout=10000):
        pass

    stored = store.load(record.experiment_id)
    assert len(stored.runs) == 1
    run = stored.runs[0]
    assert run.status == RUN_STATUS_DONE
    assert run.kind == "run"
    assert run.procedure == "Field Sweep"
    assert run.params["field_steps"] == 3
    assert run.settings_snapshot, "expected an initiate-time settings snapshot"
    assert "magnet_z" in run.settings_snapshot

    # The record's data_file is the real HDF5 file, stamped with the context.
    with h5py.File(run.data_file, "r") as f:
        info = json.loads(f["metadata"].attrs["experiment_info"])
    assert info["experiment"]["experiment_id"] == record.experiment_id
    assert info["experiment"]["user_id"] == "jdoe"
    assert info["experiment"]["experiment_title"] == "SOT switching vs T"
    assert info["setup"]["config_name"] == "sim_cryostat"
