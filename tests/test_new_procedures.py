# ---
# description: |
#   Behavioral tests for the generic sweep procedures FieldSweep and
#   TemperatureSweep (core.procedure.SweepMeasureProcedure), parametrized over
#   the measurement VIs they can run (dc_measurement, keithley_delta_mode).
#   Covers sweep construction, initiate PhasePlan content + command order,
#   step/standby/abort plans, the missing-magnet refusal, the delta n_valid
#   column, an end-to-end Orchestrator run, and the DataSchema negative case
#   (a wrong-shaped reading degrades the Orchestrator to ERROR, unwritten).
#   Also keeps the set_ramp_rate @control and Station rate-forwarding checks.
# entry_point: pytest tests/test_new_procedures.py -v
# last_updated: 2026-07-13
# ---


import h5py
import numpy as np
import pytest

from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep import FieldSweep
from cryosoft.procedures.temperature_sweep import TemperatureSweep

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {
    "sample_name": "Test Sample",
    "sample_id": "T-GEN-001",
    "comments": "automated test",
}

# ── Per-measurement-VI parameter sets ────────────────────────────────────────
# Each dict names the measurement VI plus its own measurement parameters.
DELTA = {
    "measurement_vi": "keithley_delta_mode",
    "current": 1e-6,
    "n_readings": 5,
    "voltmeter_range_V": 0.01,
    "compliance_V": 1.0,
    "delay_s": 0.01,
    "compliance_abort": True,
    "cold_switch": False,
}
DC = {
    "measurement_vi": "dc_measurement",
    "current_A": 1e-6,
    "compliance_A": 1e-3,
    "voltmeter_range_V": 0.1,
    "readings_per_point": 5,
}
# The current parameter name and per-VI expectations, keyed by measurement VI.
MEAS_META = {
    "keithley_delta_mode": {"current_key": "current", "n": 5, "has_n_valid": True},
    "dc_measurement": {"current_key": "current_A", "n": 5, "has_n_valid": False},
}

FAST_FIELD = {
    "field_start": -0.1,
    "field_end": 0.1,
    "field_steps": 3,
    "temperature": 300.0,
    "init_wait": 0.0,
    "step_wait": 0.0,
}
FAST_TEMP = {
    "temperature_start": 300.0,
    "temperature_end": 300.0,  # same start/end → instant ramp settle in sim
    "temperature_steps": 3,
    "ramp_rate_K_per_min": 6000.0,
    "point_wait": 0.0,
}

FIELD_MEAS = [pytest.param(DELTA, id="delta"), pytest.param(DC, id="dc")]
TEMP_MEAS = [pytest.param(DC, id="dc"), pytest.param(DELTA, id="delta")]


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


def _field_proc(station, tmp_path, meas):
    return FieldSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_FIELD,
        **meas,
    )


def _temp_proc(station, tmp_path, meas):
    return TemperatureSweep(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_TEMP,
        **meas,
    )


def _arm(station, meas, proc):
    """Arm the measurement VI directly (normally done via the Orchestrator)."""
    station.get_vi(meas["measurement_vi"]).initiate(**proc._measurement_params)


# ── set_ramp_rate @control on temperature VI ─────────────────────────────────

def test_set_ramp_rate_changes_default(station):
    vi = station.temperature_vti
    vi.set_ramp_rate(10.0)
    assert vi._default_ramp_rate == pytest.approx(10.0)


def test_set_ramp_rate_is_control(station):
    vi = station.temperature_vti
    assert getattr(vi.set_ramp_rate, "_is_control", False) is True


# ── process_system_targets with rate ─────────────────────────────────────────

def test_process_system_targets_forwards_rate(station):
    """Passing 'rate' in system_targets changes the ramp rate used."""
    vi = station.temperature_vti
    vi._default_ramp_rate = 1.0  # base rate
    station.process_system_targets({"temperature_vti": Target(300.0, rate=500.0)})
    assert vi._default_ramp_rate == pytest.approx(1.0)  # not mutated
    assert vi._ramp_target == pytest.approx(300.0)


# ── Measurement-VI selection / defaults ──────────────────────────────────────

def test_field_sweep_defaults_to_first_measurement_vi(station, tmp_path):
    """With no measurement_vi given, the first registered measurement VI is used."""
    proc = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **FAST_FIELD,
    )
    assert proc._measurement_vi == station.measurement_vi_names()[0] == "keithley_delta_mode"


def test_field_sweep_rejects_non_measurement_vi(station, tmp_path):
    """Selecting a non-measurement VI is refused at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    with pytest.raises(CryoSoftConfigError, match="magnet_x"):
        FieldSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            measurement_vi="magnet_x", **FAST_FIELD,
        )


# ── FieldSweep ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_array(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    sweep = proc.get_sweep_array()
    assert len(sweep) == 3
    assert sweep[0] == pytest.approx(-0.1)
    assert sweep[-1] == pytest.approx(0.1)


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_initiate_full_phaseplan(station, tmp_path, meas):
    """initiate() returns the exact PhasePlan, including command order + kwargs."""
    proc = _field_proc(station, tmp_path, meas)
    plan = proc.initiate()
    proc.standby()

    assert isinstance(plan, PhasePlan)
    assert set(plan.targets) == {"magnet_x", "temperature_vti"}
    assert plan.targets["magnet_x"] == Target(-0.1)
    assert plan.targets["temperature_vti"] == Target(300.0)

    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert isinstance(cmd, Command)
    assert cmd.vi_name == meas["measurement_vi"]
    assert cmd.method == "initiate"
    current_key = MEAS_META[meas["measurement_vi"]]["current_key"]
    assert cmd.kwargs[current_key] == pytest.approx(1e-6)

    assert plan.wait_s == pytest.approx(0.0)


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_initiate_creates_hdf5(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    proc.initiate()
    proc.standby()
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    assert h5_files[0].stat().st_size > 0


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_change_step(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    proc.initiate()
    step = proc.change_sweep_step()
    assert isinstance(step, StepPlan)
    assert step.targets["magnet_x"].target == pytest.approx(0.0)
    assert step.wait_s == pytest.approx(0.0)
    proc.standby()


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_exhaustion(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    proc.initiate()
    proc.change_sweep_step()
    proc.change_sweep_step()
    assert proc.change_sweep_step() is None
    proc.standby()


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_measure_saves_data(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    proc.initiate()
    _arm(station, meas, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    n = MEAS_META[meas["measurement_vi"]]["n"]
    with h5py.File(filepath, "r") as f:
        assert not np.isnan(f["data"]["field_T"][0])
        assert not np.any(np.isnan(f["data"]["voltage_V"][0]))
        assert f["data"]["voltage_V"].shape == (1, n)
        # The delta VI contributes an n_valid scalar column; the DC VI does not.
        if MEAS_META[meas["measurement_vi"]]["has_n_valid"]:
            assert "n_valid" in f["data"]
            assert f["data"]["n_valid"][0] == n
        else:
            assert "n_valid" not in f["data"]


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_standby_parks_magnet(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    proc.initiate()
    plan = proc.standby()
    assert plan.targets["magnet_x"].target == pytest.approx(0.0)
    cmd = next(c for c in plan.commands if c.vi_name == meas["measurement_vi"])
    assert cmd.method == "standby"
    assert proc._data_manager is None


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_abort_disarms_selected_vi(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    proc.initiate()
    cmds = proc.abort()
    assert len(cmds) == 1
    assert cmds[0].vi_name == meas["measurement_vi"]
    assert cmds[0].method == "standby"
    assert proc._data_manager is None


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_full_orchestrator_loop(station, tmp_path, qtbot, meas):
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    proc = _field_proc(station, tmp_path, meas)
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert proc._index == 3
    assert orch._state == OrchestratorState.IDLE
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as f:
        assert f["data"]["field_T"].shape[0] == 3
        assert not np.any(np.isnan(f["data"]["field_T"][:]))


# ── TemperatureSweep ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("meas", TEMP_MEAS)
def test_temp_sweep_initiate_full_phaseplan(station, tmp_path, meas):
    """initiate() ramps temperature (with rate) + present magnets, arms the VI."""
    proc = _temp_proc(station, tmp_path, meas)
    plan = proc.initiate()
    proc.standby()

    assert plan.targets["temperature_vti"] == Target(300.0, rate=6000.0)
    # sim_cryostat has magnet_x + magnet_y; field_x/field_y default 0.0.
    assert plan.targets["magnet_x"] == Target(0.0)
    assert plan.targets["magnet_y"] == Target(0.0)

    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert cmd.vi_name == meas["measurement_vi"]
    assert cmd.method == "initiate"
    assert plan.wait_s == pytest.approx(0.0)


@pytest.mark.parametrize("meas", TEMP_MEAS)
def test_temp_sweep_change_step_includes_rate(station, tmp_path, meas):
    proc = _temp_proc(station, tmp_path, meas)
    proc.initiate()
    step = proc.change_sweep_step()
    assert isinstance(step, StepPlan)
    assert step.targets["temperature_vti"].rate == pytest.approx(6000.0)
    proc.standby()


@pytest.mark.parametrize("meas", TEMP_MEAS)
def test_temp_sweep_standby_holds_temperature(station, tmp_path, meas):
    """standby() returns empty targets — temperature holds at the last point."""
    proc = _temp_proc(station, tmp_path, meas)
    proc.initiate()
    plan = proc.standby()
    assert plan.targets == {}
    assert any(c.vi_name == meas["measurement_vi"] for c in plan.commands)


@pytest.mark.parametrize("meas", TEMP_MEAS)
def test_temp_sweep_measure_saves_data(station, tmp_path, meas):
    proc = _temp_proc(station, tmp_path, meas)
    proc.initiate()
    _arm(station, meas, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()
    with h5py.File(filepath, "r") as f:
        assert not np.isnan(f["data"]["temperature_K"][0])
        assert not np.any(np.isnan(f["data"]["voltage_V"][0]))


def test_temp_sweep_full_orchestrator_loop(station, tmp_path, qtbot):
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    proc = _temp_proc(station, tmp_path, DC)
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert proc._index == 3
    assert orch._state == OrchestratorState.IDLE
    assert len(list(tmp_path.glob("*.h5"))) == 1


# ── TemperatureSweep on stations without magnets ─────────────────────────────

def _partial_station(*keep: str):
    """A station containing only the named VIs from the sim config."""
    from cryosoft.core.station import Station

    full = build_station(CONFIG_PATH)
    partial = Station()
    for name in keep:
        partial.register_vi(name, full.get_vi(name), full.get_vi_type(name))
    return partial


def test_temp_sweep_missing_magnet_with_zero_field_is_skipped(tmp_path):
    """A station without magnet_y still runs the sweep at field_y=0."""
    station = _partial_station("magnet_x", "temperature_vti", "dc_measurement")
    proc = TemperatureSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **FAST_TEMP, **DC,
    )
    plan = proc.initiate()
    assert "magnet_y" not in plan.targets
    assert "magnet_x" in plan.targets
    assert "temperature_vti" in plan.targets
    proc.standby()


def test_temp_sweep_missing_magnet_with_nonzero_field_is_refused(tmp_path):
    """A NONZERO field on a missing magnet must fail at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station = _partial_station("magnet_x", "temperature_vti", "dc_measurement")
    with pytest.raises(CryoSoftConfigError, match="magnet_y"):
        TemperatureSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **{**FAST_TEMP, **DC, "field_y": 0.5},
        )


# ── DataSchema negative case (wrong-shaped reading → ERROR, unwritten) ────────

def test_wrong_shape_reading_degrades_to_error(station, tmp_path, qtbot, monkeypatch):
    """A measurement VI returning a wrong-length array must not corrupt the file.

    The per-datapoint DataSchema.validate() in _save_datapoint raises
    DataSchemaError before anything is written; the Orchestrator's tick boundary
    contains it to ERROR and cleans up. The datapoint is never saved.
    """
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    proc = _field_proc(station, tmp_path, DC)
    vi = station.get_vi("dc_measurement")
    good_take_reading = vi.take_reading

    def bad_take_reading():
        data = good_take_reading()
        data["voltage_V"] = list(data["voltage_V"]) + [0.0]  # one element too long
        return data

    monkeypatch.setattr(vi, "take_reading", bad_take_reading)

    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    qtbot.waitUntil(lambda: orch._state == OrchestratorState.ERROR, timeout=10000)
    assert orch._state == OrchestratorState.ERROR

    # The file exists but nothing was written — every field_T is still NaN.
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as f:
        assert np.all(np.isnan(f["data"]["field_T"][:]))


# ── Switch multiplexing (Wave 6) ─────────────────────────────────────────────

MUX2 = {"mux_Mux-Ch1": True, "mux_Mux-Ch2": True}   # two routes -> multiplexed
MUX1 = {"mux_Mux-Ch1": True}                          # one route -> unsuffixed


def _field_proc_mux(station, tmp_path, meas, mux):
    station.set_scanner_enabled(True)
    return FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **FAST_FIELD, **meas, **mux,
    )


def test_mux_two_routes_initiate_command_order(station, tmp_path):
    """With 2 routes, initiate() selects the FIRST route BEFORE arming the VI."""
    proc = _field_proc_mux(station, tmp_path, DELTA, MUX2)
    assert proc._selected_routes == ["Mux-Ch1", "Mux-Ch2"]
    plan = proc.initiate()
    proc.standby()

    assert len(plan.commands) == 2
    assert plan.commands[0].vi_name == "switch_matrix"
    assert plan.commands[0].method == "select_route"
    assert plan.commands[0].kwargs == {"route": "Mux-Ch1"}
    assert plan.commands[1].vi_name == "keithley_delta_mode"
    assert plan.commands[1].method == "initiate"


def test_mux_two_routes_schema_has_suffixed_arrays_and_scalars(station, tmp_path):
    """The DataSchema arrays AND scalar columns are expanded per route."""
    proc = _field_proc_mux(station, tmp_path, DELTA, MUX2)
    proc.initiate()
    schema = proc._data_schema
    proc.standby()

    assert set(schema.arrays) == {
        "voltage_V__Mux-Ch1", "voltage_V__Mux-Ch2",
        "current_A__Mux-Ch1", "current_A__Mux-Ch2",
    }
    # The delta VI's n_valid scalar is expanded per route; the bare name is gone.
    assert "n_valid" not in schema.sweep_columns
    assert "n_valid__Mux-Ch1" in schema.sweep_columns
    assert "n_valid__Mux-Ch2" in schema.sweep_columns
    # Non-mux system columns (e.g. field_T) are NOT suffixed.
    assert "field_T" in schema.sweep_columns


def test_mux_two_routes_measure_writes_suffixed_keys(station, tmp_path):
    """measure() loops the routes and writes per-route suffixed datasets."""
    proc = _field_proc_mux(station, tmp_path, DELTA, MUX2)
    proc.initiate()
    station.get_vi("keithley_delta_mode").initiate(**proc._measurement_params)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V__Mux-Ch1"].shape == (1, 5)
        assert f["data"]["voltage_V__Mux-Ch2"].shape == (1, 5)
        assert not np.any(np.isnan(f["data"]["voltage_V__Mux-Ch1"][0]))
        assert f["data"]["n_valid__Mux-Ch1"][0] == 5
        assert f["data"]["n_valid__Mux-Ch2"][0] == 5


def test_mux_two_routes_standby_and_abort_open_all(station, tmp_path):
    """standby() and abort() both append a switch open_all command."""
    proc = _field_proc_mux(station, tmp_path, DELTA, MUX2)
    proc.initiate()
    standby_plan = proc.standby()
    open_cmds = [
        c for c in standby_plan.commands
        if c.vi_name == "switch_matrix" and c.method == "open_all"
    ]
    assert len(open_cmds) == 1

    proc2 = _field_proc_mux(station, tmp_path, DELTA, MUX2)
    proc2.initiate()
    abort_cmds = proc2.abort()
    assert any(
        c.vi_name == "switch_matrix" and c.method == "open_all" for c in abort_cmds
    )
    assert any(
        c.vi_name == "keithley_delta_mode" and c.method == "standby" for c in abort_cmds
    )


def test_mux_one_route_unsuffixed_schema_and_select_at_initiate(station, tmp_path):
    """One route: schema is unsuffixed, switch selected once at initiate only."""
    proc = _field_proc_mux(station, tmp_path, DELTA, MUX1)
    assert proc._selected_routes == ["Mux-Ch1"]
    plan = proc.initiate()
    schema = proc._data_schema

    # Switch is selected at initiate (before arming), but the schema is plain.
    assert plan.commands[0].vi_name == "switch_matrix"
    assert plan.commands[0].method == "select_route"
    assert set(schema.arrays) == {"voltage_V", "current_A"}
    assert "n_valid" in schema.sweep_columns

    # measure() takes a plain reading (no per-route re-selection, no suffix).
    station.get_vi("keithley_delta_mode").initiate(**proc._measurement_params)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()
    with h5py.File(filepath, "r") as f:
        assert "voltage_V" in f["data"]
        assert "voltage_V__Mux-Ch1" not in f["data"]


def test_mux_zero_routes_is_unchanged(station, tmp_path):
    """No route selected: behaviour is identical to pre-Wave-6 (no switch calls)."""
    proc = _field_proc_mux(station, tmp_path, DELTA, {})
    assert proc._selected_routes == []
    plan = proc.initiate()
    proc.standby()
    # Only the measurement initiate command — no switch select_route.
    assert len(plan.commands) == 1
    assert plan.commands[0].vi_name == "keithley_delta_mode"
    assert set(proc._data_schema.arrays) == {"voltage_V", "current_A"}


def test_mux_unknown_route_selection_refused(station, tmp_path):
    """A mux_ selection naming a route the switch lacks fails at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station.set_scanner_enabled(True)
    with pytest.raises(CryoSoftConfigError, match="Mux-Ch9"):
        FieldSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **FAST_FIELD, **DELTA, **{"mux_Mux-Ch9": True},
        )


def test_mux_two_routes_measure_dispatches_select_route_as_command(station, tmp_path):
    """measure()'s per-route switching goes through send_measurement_commands.

    Regression guard for the layering fix: measure() must not call
    select_route() directly on the switch VI (a hardware write bypassing the
    Orchestrator's dispatch channel); it must go through the same
    Command/send_measurement_commands path initiate() uses.
    """
    proc = _field_proc_mux(station, tmp_path, DELTA, MUX2)
    proc.initiate()
    station.get_vi("keithley_delta_mode").initiate(**proc._measurement_params)

    calls = []
    original = station.send_measurement_commands

    def spy(commands):
        calls.append(list(commands))
        return original(commands)

    station.send_measurement_commands = spy
    try:
        proc.measure()
    finally:
        station.send_measurement_commands = original
        proc.standby()

    route_calls = [
        c for batch in calls for c in batch
        if c.vi_name == "switch_matrix" and c.method == "select_route"
    ]
    assert [c.kwargs["route"] for c in route_calls] == ["Mux-Ch1", "Mux-Ch2"]


def test_scanner_disabled_behaves_as_no_switch_vi(station, tmp_path):
    """With scanner_enabled() False, a switch-VI-equipped station acts switch-less."""
    assert station.scanner_enabled() is False  # default, no set_scanner_enabled() call
    proc = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **FAST_FIELD, **DELTA, **MUX2,
    )
    assert proc._selected_routes == []
    assert proc._switch_vi is None
    plan = proc.initiate()
    proc.standby()
    assert len(plan.commands) == 1
    assert plan.commands[0].vi_name == "keithley_delta_mode"

    groups = FieldSweep.get_param_groups(station)
    assert not any(g.key == "mux" for g in groups)


def test_mux_full_orchestrator_loop_two_routes(station, tmp_path, qtbot):
    """A 2-route mux sweep completes to IDLE with per-route datasets written."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    proc = _field_proc_mux(station, tmp_path, DELTA, MUX2)
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert proc._index == 3
    assert orch._state == OrchestratorState.IDLE
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as f:
        assert f["data"]["field_T"].shape[0] == 3
        assert f["data"]["voltage_V__Mux-Ch1"].shape == (3, 5)
        assert f["data"]["voltage_V__Mux-Ch2"].shape == (3, 5)
        assert not np.any(np.isnan(f["data"]["voltage_V__Mux-Ch1"][:]))
