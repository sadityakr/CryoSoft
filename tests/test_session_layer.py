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
import shutil

import h5py
import pytest

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.plan import EnvelopeBound, SessionEnvelope
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep import FieldSweep
from cryosoft.session.manager import SessionManager
from cryosoft.session.models import (
    EXPERIMENT_STATUS_CLOSED,
    EXPERIMENT_STATUS_OPEN,
    RUN_STATUS_DONE,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    SCHEMA_VERSION,
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
        queue=[{"procedure": "Field Sweep", "params": {"field_end": 1.0}}],
    )
    assert record.schema_version == SCHEMA_VERSION
    payload = record.to_dict()
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["queue"] == record.queue
    assert ExperimentRecord.from_dict(payload) == record


def test_experiment_record_schema_version_absent_defaults_to_one():
    """A record written before schema_version existed loads as version 1."""
    record = ExperimentRecord.from_dict({"experiment_id": "x"})
    assert record.schema_version == 1


def test_experiment_record_schema_version_tolerates_future_value():
    """A record from a newer app loads (tolerant-parse) with its stated version kept."""
    record = ExperimentRecord.from_dict({"experiment_id": "x", "schema_version": 999})
    assert record.schema_version == 999


def test_experiment_record_queue_tolerates_junk():
    """Non-list/non-dict queue entries degrade to [] / are dropped, never raise."""
    assert ExperimentRecord.from_dict({"queue": "not-a-list"}).queue == []
    assert ExperimentRecord.from_dict({"queue": [{"a": 1}, "junk", 5]}).queue == [{"a": 1}]


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


def test_store_load_warns_on_future_schema_version(store, caplog):
    """A record from a newer app still loads (tolerant), but logs a WARNING."""
    record = ExperimentRecord(experiment_id="20260717_future")
    store.save(record)
    # Hand-edit the file to simulate a newer app's format version.
    path = store.root / "20260717_future" / "experiment.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")

    with caplog.at_level("WARNING"):
        loaded = store.load("20260717_future")
    assert loaded.schema_version == 999
    assert any("newer app" in message for message in caplog.messages)


def test_store_data_dir_and_gui_state_path(store):
    assert store.data_dir("exp1") == store.root / "exp1" / "data"
    assert store.gui_state_path("exp1") == store.root / "exp1" / "gui_state.json"
    # Neither call creates anything on disk.
    assert not store.root.exists()


def test_relativize_and_resolve_data_file_plain_and_subfolder(store):
    store.save(ExperimentRecord(experiment_id="exp1"))
    data_dir = store.data_dir("exp1")
    data_dir.mkdir(parents=True)
    plain_file = data_dir / "run1.h5"
    plain_file.write_text("x", encoding="utf-8")
    sub_dir = data_dir / "heating_runs"
    sub_dir.mkdir()
    sub_file = sub_dir / "run2.h5"
    sub_file.write_text("x", encoding="utf-8")

    rel_plain = store.relativize_data_file("exp1", plain_file)
    assert rel_plain == "data/run1.h5"
    assert store.resolve_data_file("exp1", rel_plain) == plain_file

    rel_sub = store.relativize_data_file("exp1", sub_file)
    assert rel_sub == "data/heating_runs/run2.h5"
    assert store.resolve_data_file("exp1", rel_sub) == sub_file


def test_relativize_and_resolve_data_file_outside_bundle_stays_absolute(store, tmp_path):
    outside = tmp_path / "elsewhere" / "run.h5"
    outside.parent.mkdir(parents=True)
    outside.write_text("x", encoding="utf-8")

    stored = store.relativize_data_file("exp1", outside)
    assert stored == str(outside.resolve())
    resolved = store.resolve_data_file("exp1", stored)
    assert resolved == outside.resolve()


def test_resolve_data_file_survives_session_folder_relocation(tmp_path):
    """A dangling absolute path falls back to a basename search under data/."""
    old_root = tmp_path / "old_root"
    store = ExperimentStore(old_root)
    store.save(ExperimentRecord(experiment_id="exp1"))
    data_dir = store.data_dir("exp1")
    data_dir.mkdir(parents=True)
    data_file = data_dir / "run1.h5"
    data_file.write_text("x", encoding="utf-8")

    # Record the run with its (then-valid) absolute path, as an old-format
    # record would have stored it before bundle-relative paths existed.
    record = store.load("exp1")
    record.runs.append(RunRecord(run_id="r1", data_file=str(data_file)))
    store.save(record)

    # Move the whole session folder elsewhere.
    new_root = tmp_path / "new_root"
    shutil.move(str(old_root), str(new_root))

    new_store = ExperimentStore(new_root)
    stored = new_store.load("exp1")
    resolved = new_store.resolve_data_file("exp1", stored.runs[0].data_file)
    assert resolved == new_root / "exp1" / "data" / "run1.h5"
    assert resolved.is_file()


def test_resolve_data_file_dangling_absolute_no_match_returns_unchanged(store):
    missing = store.root.parent / "gone" / "nope.h5"
    resolved = store.resolve_data_file("exp1", str(missing))
    assert resolved == missing


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


# ── set_queue / current_data_dir / current_gui_state_path ────────────────────

def test_set_queue_persists_and_is_noop_when_nothing_open(manager, store):
    assert manager.current_experiment() is None
    manager.set_queue([{"procedure": "Field Sweep"}])  # no-op, no experiment
    assert manager.current_experiment() is None

    record = manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    queue = [{"procedure": "Field Sweep", "params": {"field_end": 1.0}}]
    manager.set_queue(queue)
    assert store.load(record.experiment_id).queue == queue


def test_current_data_dir_and_gui_state_path(manager, store):
    assert manager.current_data_dir() is None
    assert manager.current_gui_state_path() is None

    record = manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    assert manager.current_data_dir() == store.data_dir(record.experiment_id)
    assert manager.current_gui_state_path() == store.gui_state_path(record.experiment_id)


# ── switch_experiment ─────────────────────────────────────────────────────────

def test_switch_experiment_happy_path(manager, store, orchestrator):
    envelope_a = SessionEnvelope(bounds={"magnet_z": EnvelopeBound(max_value=1.0)})
    envelope_b = SessionEnvelope(bounds={"magnet_z": EnvelopeBound(max_value=2.0)})
    first = manager.start_experiment("First", "jdoe", SAMPLE_INFO, envelope=envelope_a)

    # A second, independently-open experiment exists in the store (as if
    # created in an earlier session) — written directly since start_experiment
    # refuses to open a second one while one is already open.
    second = ExperimentRecord(
        experiment_id="20260717_second",
        title="Second",
        user_id="jdoe",
        status=EXPERIMENT_STATUS_OPEN,
        envelope=envelope_to_dict(envelope_b),
    )
    store.save(second)

    changed: list[dict] = []
    manager.experiment_changed.connect(changed.append)

    result = manager.switch_experiment(second.experiment_id)
    assert result.experiment_id == second.experiment_id
    assert manager.current_experiment().experiment_id == second.experiment_id
    assert store.get_active() == second.experiment_id
    assert changed and changed[-1]["experiment_id"] == second.experiment_id
    assert orchestrator._session_envelope == envelope_b
    # The experiment switched away from is untouched: still "open" on disk.
    assert store.load(first.experiment_id).status == EXPERIMENT_STATUS_OPEN

    back = manager.switch_experiment(first.experiment_id)
    assert back.experiment_id == first.experiment_id
    assert manager.current_experiment().experiment_id == first.experiment_id
    assert store.get_active() == first.experiment_id
    assert orchestrator._session_envelope == envelope_a


def test_switch_experiment_rejects_unknown_id(manager):
    with pytest.raises(ValueError, match="Unknown experiment"):
        manager.switch_experiment("nope")


def test_switch_experiment_rejects_closed_target(manager, store):
    record = manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    manager.close_experiment()
    assert store.load(record.experiment_id).status == EXPERIMENT_STATUS_CLOSED
    with pytest.raises(ValueError, match="not open"):
        manager.switch_experiment(record.experiment_id)


def test_switch_experiment_rejects_future_schema_version(manager, store):
    manager.start_experiment("Current", "jdoe", SAMPLE_INFO)
    # to_dict() always stamps the *current* SCHEMA_VERSION (see models.py), so
    # simulating a newer app's file means hand-editing the JSON, exactly like
    # the store-level future-schema test does.
    store.save(ExperimentRecord(experiment_id="future_one", status=EXPERIMENT_STATUS_OPEN))
    path = store.root / "future_one" / "experiment.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="newer app"):
        manager.switch_experiment("future_one")


# ── Save-failure surfacing (store_health_changed) ─────────────────────────────

def test_store_health_changed_fires_once_on_failure_and_once_on_recovery(manager, store, monkeypatch):
    manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    events: list[dict] = []
    manager.store_health_changed.connect(events.append)

    def _boom(record):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", _boom)
    manager.set_findings("first failure")
    manager.set_findings("second failure")  # still failing — must not re-emit
    assert events == [{"ok": False, "detail": "disk full"}]

    monkeypatch.undo()
    manager.set_findings("recovered")
    manager.set_findings("still fine")  # already ok — must not re-emit
    assert events == [
        {"ok": False, "detail": "disk full"},
        {"ok": True, "detail": ""},
    ]


# ── Future schema_version belt-and-suspenders on _save_current ───────────────

def test_save_current_refuses_to_overwrite_future_schema_version(manager, store, caplog):
    record = manager.start_experiment("X", "jdoe", SAMPLE_INFO)
    on_disk_before = store.load(record.experiment_id)

    # Simulate the in-memory record somehow carrying a future schema_version
    # (belt-and-suspenders: switch_experiment already refuses this at the
    # door, but _save_current must never write one back regardless).
    manager._experiment.schema_version = 999
    with caplog.at_level("WARNING"):
        manager.set_findings("should not be written")
    assert any("Refusing to overwrite" in message for message in caplog.messages)
    assert store.load(record.experiment_id) == on_disk_before


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
