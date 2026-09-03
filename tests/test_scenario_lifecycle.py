# ---
# description: |
#   End-to-end simulation scenarios for the measurement lifecycle: a run
#   started through the client boundary (the OrchestratorProxy, in both the
#   threaded and inline instrument modes), driven to completion, and checked
#   at every layer it touches — the control contract's event stream, the
#   HDF5 file core/data_reader.py reads back, the session layer's RunRecord,
#   the run queue's operations-first rule, and the instrument thread's
#   responsiveness and shutdown guarantees.
# last_updated: 2026-09-03
# ---

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from PyQt6.QtCore import QThread, QTimer

from cryosoft.core import events as ev
from cryosoft.core.data_reader import list_columns, open_run, read_metadata
from cryosoft.core.instrument_host import InstrumentHost
from cryosoft.core.plan import PhasePlan, StepPlan, Target
from cryosoft.core.run_builder import build_procedure
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep import FieldSweep
from cryosoft.procedures.temperature_sweep import TemperatureSweep
from cryosoft.procedures.time_series import TimeSeries
from cryosoft.session.manager import ExperimentManager
from cryosoft.session.models import RUN_STATUS_DONE
from cryosoft.session.store import ExperimentStore, User, UserRoster
from tests.instrument_modes import build_host, instrument_mode, shutdown_host

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {"sample_name": "S", "sample_id": "S-1", "comments": ""}

#: The proven-minimal FieldSweep parameter set (tests/test_l3_orchestrator.py's
#: FIELD_SWEEP_PARAMS): a tiny +/-0.1 T sweep, three points, no settle waits.
FIELD_SWEEP_PARAMS: dict[str, Any] = {
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

#: A three-point TemperatureSweep that never actually has to ramp (start ==
#: end == the sim VTI's resting value), measured with the DC source/meter.
TEMPERATURE_SWEEP_PARAMS: dict[str, Any] = {
    "measurement_vi": "dc_measurement",
    "temperature_start": 300.0,
    "temperature_end": 300.0,
    "temperature_steps": 3,
    "ramp_rate_K_per_min": 6000.0,
    "point_wait": 0.0,
    "current_A": 1e-6,
    "compliance_A": 1e-3,
    "voltmeter_range_V": 0.1,
    "readings_per_point": 5,
}

#: A three-point TimeSeries: elapsed-time only, no hardware commanded at all.
TIME_SERIES_PARAMS: dict[str, Any] = {
    "measurement_vi": "dc_measurement",
    "step_time_s": 0.01,
    "max_duration_s": 0.02,
    "end_condition": "time",
    "end_value": 300.0,
    "end_tolerance": 0.0,
    "current_A": 1e-6,
    "compliance_A": 1e-3,
    "voltmeter_range_V": 0.1,
    "readings_per_point": 5,
}

RUN_CATALOG: dict[str, type] = {
    "FieldSweep": FieldSweep,
    "TemperatureSweep": TemperatureSweep,
    "TimeSeries": TimeSeries,
}

#: A queued hop costs one event-loop turn; this bounds every wait generously
#: so a timeout means the boundary is broken, not that the machine was busy.
WAIT_MS = 15000


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def host(qtbot):
    """A started host, in this session's instrument mode, with the run catalog.

    ``instrument_modes.build_host()`` cannot be used unmodified here: it
    fixes ``orchestrator_options`` itself, and every scenario below needs a
    ``run_catalog`` on the engine. The magnet is sped up on the engine's own
    thread before any test touches it (``station.magnet_z`` — a plain
    attribute set, never a hardware call) so a +/-0.1 T sweep settles inside
    a handful of 10 ms ticks instead of the sim's realistic ramp rate.
    """
    built = InstrumentHost(
        lambda: build_station(CONFIG_PATH),
        mode=instrument_mode(),
        orchestrator_options={
            "tick_interval_ms": 10,
            "run_catalog": RUN_CATALOG,
        },
    )
    built.start()

    def _fast_magnet(station: Any) -> None:
        station.magnet_z._default_ramp_rate = 6000.0
        station.magnet_z._ramp_segments = []

    if built.bridge is not None and not built.bridge.on_engine_thread():
        done = []
        built.bridge.post(lambda: (done.append(1), _fast_magnet(built.station)))
        for _ in range(200):
            if done:
                break
            QThread.msleep(5)
    else:
        _fast_magnet(built.station)

    yield built
    shutdown_host(built)


@pytest.fixture
def proxy(host):
    """The client adapter every scenario drives the run through."""
    return host.build_proxy()


def _build(station, cls, params, tmp_path, *, file_prefix="scenario", experiment_info=None):
    """Build a run the way the GUI does: on the client's thread, headlessly."""
    return build_procedure(
        cls,
        station=station,
        params=dict(params),
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        file_prefix=file_prefix,
        experiment_info=experiment_info,
    )


def _run_to_completion(proxy, qtbot, procedure) -> tuple[list[Any], list[Any]]:
    """Start *procedure* through the proxy and wait for it to finish.

    Returns:
        ``(events, started_dicts)`` — every ``Event`` seen on the one event
        stream while the run was in flight, and every ``run_started`` dict
        (usually just one; ``events`` carries the ``RunStarted``/
        ``RunFinished`` objects too).
    """
    events: list[Any] = []
    proxy.event.connect(events.append)
    proxy.run_procedure(procedure)
    qtbot.waitUntil(
        lambda: any(isinstance(e, ev.RunFinished) for e in events), timeout=WAIT_MS
    )
    qtbot.waitUntil(lambda: proxy.state == "IDLE", timeout=WAIT_MS)
    return events, [e for e in events if isinstance(e, ev.RunStarted)]


# ══════════════════════════════════════════════════════════════════════════
# 1. FieldSweep, both entry points, to completion
# ══════════════════════════════════════════════════════════════════════════


def test_field_sweep_via_proxy_run_procedure_completes_and_is_readable(
    host, proxy, qtbot, tmp_path
):
    """A FieldSweep started through ``proxy.run_procedure`` runs to completion.

    Checks every claim of scenario 1 in one pass: exactly one RunStarted and
    one RunFinished("done"), the datapoint count equals the sweep length, the
    HDF5 file (read through ``core/data_reader.py``) has the right columns
    and point count, ``StatusSnapshot.run`` was non-null during the run and
    null after, and the final state is IDLE.
    """
    snapshots: list[ev.StatusSnapshot] = []
    proxy.status_snapshot_event.connect(snapshots.append)
    datapoints: list[ev.Datapoint] = []
    proxy.datapoint_event.connect(datapoints.append)

    procedure = _build(host.station, FieldSweep, FIELD_SWEEP_PARAMS, tmp_path)
    events, started = _run_to_completion(proxy, qtbot, procedure)

    assert len(started) == 1
    finished = [e for e in events if isinstance(e, ev.RunFinished)]
    assert len(finished) == 1
    assert finished[0].status == RUN_STATUS_DONE
    assert finished[0].run_id == started[0].run_id

    assert len(datapoints) == FIELD_SWEEP_PARAMS["field_steps"]

    data_file = finished[0].manifest["data_file"]
    with open_run(data_file) as handle:
        columns = {info.name for info in list_columns(handle)}
        n_points = handle.n_points
    assert {"field_T", "voltage_V"} <= columns
    assert n_points == FIELD_SWEEP_PARAMS["field_steps"]

    mid_run = [s for s in snapshots if s.seq >= started[0].seq and s.run is not None]
    assert mid_run, "StatusSnapshot.run was never populated while the run was live"
    assert snapshots[-1].run is None, "StatusSnapshot.run stayed populated after IDLE"

    assert proxy.state == "IDLE"


def test_field_sweep_via_submitted_command_with_the_run_catalog(
    host, proxy, qtbot, tmp_path
):
    """The same run, started as a JSON ``Command`` through the run catalog."""
    verdicts: list[ev.Verdict] = []
    events: list[Any] = []
    proxy.verdict.connect(verdicts.append)
    proxy.event.connect(events.append)

    command = ev.Command(
        name=ev.CommandName.RUN_PROCEDURE,
        actor=ev.Actor(kind=ev.ActorKind.AGENT, id="scenario-agent", role="operator"),
        args={
            "procedure": "FieldSweep",
            "params": dict(FIELD_SWEEP_PARAMS),
            "sample_info": SAMPLE_INFO,
            "data_directory": str(tmp_path),
            "file_prefix": "submit",
        },
    )
    request_id = proxy.submit(command)
    qtbot.waitUntil(lambda: len(verdicts) >= 1, timeout=WAIT_MS)

    assert request_id == command.request_id
    assert verdicts[0].request_id == request_id
    assert verdicts[0].code is ev.VerdictCode.OK

    qtbot.waitUntil(
        lambda: any(isinstance(e, ev.RunFinished) for e in events), timeout=WAIT_MS
    )
    qtbot.waitUntil(lambda: proxy.state == "IDLE", timeout=WAIT_MS)

    started = [e for e in events if isinstance(e, ev.RunStarted)]
    finished = [e for e in events if isinstance(e, ev.RunFinished)]
    assert len(started) == 1
    assert len(finished) == 1
    assert finished[0].status == RUN_STATUS_DONE
    assert started[0].actor.kind is ev.ActorKind.AGENT
    assert started[0].request_id == request_id


def test_field_sweep_run_record_completes_with_a_data_file_on_disk(
    host, proxy, qtbot, tmp_path
):
    """The session layer's RunRecord agrees with the HDF5 file it names."""
    store = ExperimentStore(tmp_path / "experiments")
    roster = UserRoster(tmp_path / "users.json")
    roster.add(User(user_id="jdoe", name="J. Doe", email="jdoe@example.org"))
    manager = ExperimentManager(
        store=store,
        roster=roster,
        orchestrator=proxy,
        config_name="sim_cryostat",
        station=host.station,
        run_catalog=RUN_CATALOG,
    )
    record = manager.start_experiment("Scenario run", "jdoe", SAMPLE_INFO)

    recorded: list[dict] = []
    manager.run_recorded.connect(recorded.append)
    procedure = _build(
        host.station,
        FieldSweep,
        FIELD_SWEEP_PARAMS,
        tmp_path,
        experiment_info=manager.experiment_context(),
    )
    proxy.run_procedure(procedure)
    qtbot.waitUntil(
        lambda: any(r.get("status") == RUN_STATUS_DONE for r in recorded),
        timeout=WAIT_MS,
    )

    stored = store.load(record.experiment_id)
    assert len(stored.runs) == 1
    run = stored.runs[0]
    assert run.status == RUN_STATUS_DONE
    assert run.data_file
    from pathlib import Path

    assert Path(run.data_file).exists()


# ══════════════════════════════════════════════════════════════════════════
# 2. TemperatureSweep and TimeSeries, the same way
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "cls,params,axis_column,n_points",
    [
        pytest.param(
            TemperatureSweep,
            TEMPERATURE_SWEEP_PARAMS,
            "temperature_K",
            TEMPERATURE_SWEEP_PARAMS["temperature_steps"],
            id="TemperatureSweep",
        ),
        pytest.param(
            TimeSeries,
            TIME_SERIES_PARAMS,
            "elapsed_s",
            3,  # 0.02 // 0.01 + 1
            id="TimeSeries",
        ),
    ],
)
def test_other_procedures_run_to_completion_the_same_way(
    host, proxy, qtbot, tmp_path, cls, params, axis_column, n_points
):
    """TemperatureSweep and TimeSeries, driven to completion the same way.

    The same checks as the FieldSweep scenario, at each procedure's smallest
    working parameter set: one RunStarted, one RunFinished("done"), the
    datapoint count equal to the sweep length, the HDF5 file's axis column
    and point count, and a final state of IDLE.
    """
    snapshots: list[ev.StatusSnapshot] = []
    proxy.status_snapshot_event.connect(snapshots.append)
    datapoints: list[ev.Datapoint] = []
    proxy.datapoint_event.connect(datapoints.append)

    procedure = _build(host.station, cls, params, tmp_path)
    events, started = _run_to_completion(proxy, qtbot, procedure)

    assert len(started) == 1
    finished = [e for e in events if isinstance(e, ev.RunFinished)]
    assert len(finished) == 1
    assert finished[0].status == RUN_STATUS_DONE

    assert len(datapoints) == n_points

    data_file = finished[0].manifest["data_file"]
    with open_run(data_file) as handle:
        columns = {info.name for info in list_columns(handle)}
        found_points = handle.n_points
    assert {axis_column, "voltage_V"} <= columns
    assert found_points == n_points

    mid_run = [s for s in snapshots if s.seq >= started[0].seq and s.run is not None]
    assert mid_run, "StatusSnapshot.run was never populated while the run was live"
    assert snapshots[-1].run is None

    assert proxy.state == "IDLE"
