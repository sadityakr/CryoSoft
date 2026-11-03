import h5py
import numpy as np
import pytest

from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.station import build_station
from cryosoft.drivers.sim_tensormeter_rtm2 import SimTensormeterRTM2
from cryosoft.procedures.field_sweep import FieldSweep
from cryosoft.procedures.temperature_sweep import TemperatureSweep
from cryosoft.virtual_instruments.measurement.tensormeter_rtm2_measurement import (
    _RAW_CHANNEL_NAMES,
    TensormeterRTM2MeasurementVI,
)

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
    "dwell_time": 0.0,
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
    station.get_vi(meas["measurement_vi"]).initiate_measurement(**proc._measurement_params)


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

    with pytest.raises(CryoSoftConfigError, match="magnet_z"):
        FieldSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            measurement_vi="magnet_z", **FAST_FIELD,
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
    assert set(plan.targets) == {"magnet_z", "temperature_vti"}
    assert plan.targets["magnet_z"] == Target(-0.1)
    assert plan.targets["temperature_vti"] == Target(300.0)

    # FieldSweep keeps the default claim (claimed_vi_names() -> None), so
    # claim_commands initiate every station VI, in station registration order.
    assert [c.vi_name for c in plan.claim_commands] == station.get_vi_names()
    assert all(c.method == "initiate" and c.kwargs == {} for c in plan.claim_commands)

    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert isinstance(cmd, Command)
    assert cmd.vi_name == meas["measurement_vi"]
    assert cmd.method == "initiate_measurement"
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
    assert step.targets["magnet_z"].target == pytest.approx(0.0)
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
        assert not np.any(np.isnan(f["data"]["voltage_V_array"][0]))
        assert f["data"]["voltage_V_array"].shape == (1, 1, 1, n)
        assert not np.isnan(f["data"]["voltage_V"][0, 0, 0])  # mean
        # The delta VI contributes an n_valid scalar column; the DC VI does not.
        if MEAS_META[meas["measurement_vi"]]["has_n_valid"]:
            assert "n_valid" in f["data"]
            assert f["data"]["n_valid"][0, 0, 0] == n
        else:
            assert "n_valid" not in f["data"]


@pytest.mark.parametrize("meas", FIELD_MEAS)
def test_field_sweep_standby_commands_magnet_standby(station, tmp_path, meas):
    proc = _field_proc(station, tmp_path, meas)
    proc.initiate()
    plan = proc.standby()
    assert plan.targets == {}
    cmd = next(c for c in plan.commands if c.vi_name == meas["measurement_vi"])
    assert cmd.method == "standby"
    magnet_cmd = next(c for c in plan.commands if c.vi_name == "magnet_z")
    assert magnet_cmd.method == "standby"
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

    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

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
    # field_z defaults to 0.0; the sweep never touches magnet_y.
    assert plan.targets["magnet_z"] == Target(0.0)
    assert "magnet_y" not in plan.targets

    # TemperatureSweep keeps the default claim too — every station VI.
    assert [c.vi_name for c in plan.claim_commands] == station.get_vi_names()
    assert all(c.method == "initiate" and c.kwargs == {} for c in plan.claim_commands)

    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert cmd.vi_name == meas["measurement_vi"]
    assert cmd.method == "initiate_measurement"
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


def test_temp_sweep_run_resets_a_stale_manual_heater_to_auto(station, tmp_path):
    """A run resets a heater the operator left in MANUAL back to AUTO.

    Regression test for the scenario claim_commands exists to solve: an
    operator switches temperature_vti's heater to MANUAL by hand (e.g.
    during a bench test) and leaves it that way. Without claim-initiating
    every claimed VI at run start, the closed loop would stay off for the
    whole run and the sweep's ramp target would never actually be reached.
    """
    from cryosoft.core.orchestrator import Orchestrator

    station.temperature_vti.set_heater_mode("MANUAL")
    assert station.temperature_vti.heater_mode() == "MANUAL"

    proc = _temp_proc(station, tmp_path, DC)
    orch = Orchestrator(station, tick_interval_ms=10)
    try:
        orch.run_procedure(proc)
        assert station.temperature_vti.heater_mode() == "AUTO"
    finally:
        orch.shutdown()


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
    """A station without magnet_z still runs the sweep at field_z=0."""
    station = _partial_station("temperature_vti", "dc_measurement")
    proc = TemperatureSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **FAST_TEMP, **DC,
    )
    plan = proc.initiate()
    assert "magnet_z" not in plan.targets
    assert "temperature_vti" in plan.targets
    proc.standby()


def test_temp_sweep_missing_magnet_with_nonzero_field_is_refused(tmp_path):
    """A NONZERO field on a missing magnet must fail at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station = _partial_station("temperature_vti", "dc_measurement")
    with pytest.raises(CryoSoftConfigError, match="magnet_z"):
        TemperatureSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **{**FAST_TEMP, **DC, "field_z": 0.5},
        )


# ── DataSchema negative case (wrong-shaped reading → ERROR, unwritten) ────────

def test_wrong_shape_reading_degrades_to_error(station, tmp_path, qtbot, monkeypatch):
    """A measurement VI returning a wrong-length array must not corrupt the file.

    The per-datapoint DataSchema.validate() in _save_datapoint raises
    DataSchemaError before anything is written; the Orchestrator's tick boundary
    contains it to ERROR and cleans up. The datapoint is never saved.
    """
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    proc = _field_proc(station, tmp_path, DC)
    vi = station.get_vi("dc_measurement")
    good_take_reading = vi.take_reading

    def bad_take_reading():
        data = good_take_reading()
        data["voltage_V_array"] = list(data["voltage_V_array"]) + [0.0]  # one too long
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

# ── The reading loop: two generic slots (channels x value list) ──────────────
# Slot 1 (labels A1, A2, ...) is the outer level, slot 2 (B1, B2, ...) the
# inner one. The switch's route and the DC VI's current are the SAME concept:
# loopable parameters advertised via reading_setters.

ROUTES2 = {
    "loop1_parameter": "switch_matrix.route",
    "loop1_pick_Mux-Ch1": True,
    "loop1_pick_Mux-Ch2": True,
}
ROUTES1 = {"loop1_parameter": "switch_matrix.route", "loop1_pick_Mux-Ch1": True}
CURRENTS2 = {"loop1_parameter": "dc_measurement.current_A", "loop1_values": "1e-6, -1e-6"}
BOTH = {
    "loop1_parameter": "switch_matrix.route",
    "loop1_pick_Mux-Ch1": True,
    "loop1_pick_Mux-Ch2": True,
    "loop2_parameter": "dc_measurement.current_A",
    "loop2_values": "1e-6, -1e-6",
}


def _field_proc_scanner(station, tmp_path, meas, loop):
    station.set_scanner_enabled(True)
    return FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **FAST_FIELD, **meas, **loop,
    )


# ── Channel slot alone (the old mux behaviour, now generic) ──────────────────

def test_channel_slot_initiate_selects_first_route_before_arming(station, tmp_path):
    """A looping channel slot dispatches its first route BEFORE the arm."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    plan = proc.initiate()
    proc.standby()

    assert len(plan.commands) == 2
    assert plan.commands[0].vi_name == "switch_matrix"
    assert plan.commands[0].method == "select_route"
    assert plan.commands[0].kwargs == {"route": "Mux-Ch1"}
    assert plan.commands[1].vi_name == "keithley_delta_mode"
    assert plan.commands[1].method == "initiate_measurement"


def test_channel_slot_schema_uses_index_labels(station, tmp_path):
    """Arrays AND scalar columns carry a real loop1 axis (2 routes), not suffixes."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    proc.initiate()
    schema = proc._data_schema
    proc.standby()

    assert set(schema.measurement_arrays) == {"voltage_V_array", "current_A_array"}
    assert "n_valid" in schema.measurement_scalars
    assert "field_T" in schema.sweep_columns
    assert schema.loop_shape == (2, 1)  # 2 routes on loop1, loop2 off
    # Axis index -> route value ties back through the metadata.
    assert proc._params["loop1_values"] == ["Mux-Ch1", "Mux-Ch2"]


def test_channel_slot_measure_writes_labelled_keys(station, tmp_path):
    """measure() loops the routes and writes them along the loop1 axis."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    proc.initiate()
    _arm(station, DELTA, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V_array"].shape == (1, 2, 1, 5)
        assert not np.any(np.isnan(f["data"]["voltage_V_array"][0, 0, 0]))
        assert not np.any(np.isnan(f["data"]["voltage_V_array"][0, 1, 0]))
        assert f["data"]["n_valid"][0, 0, 0] == 5
        assert f["data"]["n_valid"][0, 1, 0] == 5


def test_channel_slot_standby_and_abort_dispatch_safe_off(station, tmp_path):
    """standby() and abort() append the switch's reading_safe_off (open_all)."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    proc.initiate()
    standby_plan = proc.standby()
    open_cmds = [
        c for c in standby_plan.commands
        if c.vi_name == "switch_matrix" and c.method == "open_all"
    ]
    assert len(open_cmds) == 1

    proc2 = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    proc2.initiate()
    abort_cmds = proc2.abort()
    assert any(
        c.vi_name == "switch_matrix" and c.method == "open_all" for c in abort_cmds
    )
    assert any(
        c.vi_name == "keithley_delta_mode" and c.method == "standby" for c in abort_cmds
    )


def test_single_value_slot_is_static(station, tmp_path):
    """One value = static setting: dispatched once at initiate, trivial axis."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES1)
    plan = proc.initiate()
    schema = proc._data_schema

    # Selected once before arming; schema carries a trivial (1, 1) loop shape.
    assert plan.commands[0].vi_name == "switch_matrix"
    assert plan.commands[0].method == "select_route"
    assert set(schema.measurement_arrays) == {"voltage_V_array", "current_A_array"}
    assert "n_valid" in schema.measurement_scalars
    assert schema.loop_shape == (1, 1)
    assert proc._params["loop1_values"] == ["Mux-Ch1"]

    # measure() takes a plain reading (no re-selection, trivial grid).
    _arm(station, DELTA, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()
    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V_array"].shape == (1, 1, 1, 5)


def test_static_measurement_slot_dispatches_after_arm(station, tmp_path):
    """A static single-value slot on the measurement VI dispatches AFTER arming."""
    proc = _field_proc(
        station, tmp_path,
        {**DC, "loop1_parameter": "dc_measurement.current_A", "loop1_values": "2e-6"},
    )
    plan = proc.initiate()
    proc.standby()

    assert [(c.vi_name, c.method) for c in plan.commands] == [
        ("dc_measurement", "initiate_measurement"),
        ("dc_measurement", "set_source_current"),
    ]
    assert plan.commands[1].kwargs == {"current_A": 2e-6}
    assert set(proc._data_schema.measurement_arrays) == {"voltage_V_array", "current_A_array"}
    assert proc._data_schema.loop_shape == (1, 1)


def test_loop_off_is_unchanged(station, tmp_path):
    """No slot selected: single plain reading; stray values text is ignored."""
    proc = _field_proc_scanner(
        station, tmp_path, DELTA, {"loop1_values": "1e-6, -1e-6"}
    )
    assert proc._loop_slots == []
    plan = proc.initiate()
    proc.standby()
    assert len(plan.commands) == 1
    assert plan.commands[0].vi_name == "keithley_delta_mode"
    assert set(proc._data_schema.measurement_arrays) == {"voltage_V_array", "current_A_array"}
    assert proc._data_schema.loop_shape == (1, 1)


def test_unknown_pick_refused(station, tmp_path):
    """A pick naming a choice the parameter lacks fails at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station.set_scanner_enabled(True)
    with pytest.raises(CryoSoftConfigError, match="Mux-Ch9"):
        FieldSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **FAST_FIELD, **DELTA,
            loop1_parameter="switch_matrix.route", **{"loop1_pick_Mux-Ch9": True},
        )


def test_channel_slot_measure_dispatches_select_route_as_command(station, tmp_path):
    """Per-route switching goes through send_measurement_commands.

    Regression guard for the layering rule: measure() must never call
    select_route() directly on the switch VI; it goes through the same
    Command/send_measurement_commands path initiate() uses.
    """
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    proc.initiate()
    _arm(station, DELTA, proc)

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


def test_scanner_disabled_removes_channel_parameter(station, tmp_path):
    """Scanner disabled: the switch's route is not loopable — refused loudly."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    assert station.scanner_enabled() is False
    with pytest.raises(CryoSoftConfigError, match="switch_matrix.route"):
        FieldSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **FAST_FIELD, **DELTA, **ROUTES2,
        )
    # And the form offers no channel parameter. The delta VI's own current is
    # still loopable, so the group itself remains — what must be absent is the
    # switch route among the slot choices.
    groups = FieldSweep.get_param_groups(station, {"measurement_vi": "keithley_delta_mode"})
    loop_group = next((g for g in groups if g.key == "reading_loop"), None)
    assert loop_group is not None
    choices = loop_group.params["loop1_parameter"].choices
    assert "switch_matrix.route" not in choices.values()
    assert "keithley_delta_mode.current" in choices.values()


# ── Value-list slot alone (± current) ────────────────────────────────────────

def test_value_slot_labels_and_suffixed_keys(station, tmp_path):
    """A two-value current loop resolves a loop1 axis of length 2, plain keys."""
    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    assert [s["qualified"] for s in proc._loop_slots] == ["dc_measurement.current_A"]
    # dc_measurement (DCSeparate/DCSingle) declares no n_valid scalar.
    assert proc.measurement_data_keys == [
        "voltage_V", "voltage_V_error", "current_A", "current_A_error",
    ]
    assert proc._loop_shape == (2, 1)
    assert proc._params["loop1_values"] == [1e-6, -1e-6]


def test_value_slot_measure_writes_signed_current(station, tmp_path):
    """measure() loops the values along axis 0; index 1 carries -current_A."""
    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    proc.initiate()
    _arm(station, DC, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V_array"].shape == (1, 2, 1, 5)
        assert np.allclose(f["data"]["current_A"][0, 0, 0], 1e-6)
        assert np.allclose(f["data"]["current_A"][0, 1, 0], -1e-6)


def test_value_slot_measure_rearms_delta_between_readings(station, tmp_path):
    """The delta VI loops its peak current end-to-end through measure().

    Delta re-arms the engine per loop step rather than setting the current in
    place, so this covers the path the DC test cannot: each label's readings
    must carry that label's current, and the engine must still be armed (in
    DELTA mode) at the last looped value when measure() returns.
    """
    delta_currents = {
        "loop1_parameter": "keithley_delta_mode.current",
        "loop1_values": "1e-6, 5e-6",
    }
    proc = _field_proc(station, tmp_path, {**DELTA, **delta_currents})
    assert proc._params["loop1_values"] == [1e-6, 5e-6]

    proc.initiate()
    _arm(station, DELTA, proc)
    proc.measure()
    filepath = proc._data_manager.filepath

    source = station.get_vi("keithley_delta_mode")._source
    assert source._mode == "DELTA"
    assert source._delta_high_current == pytest.approx(5e-6)

    proc.standby()
    with h5py.File(filepath, "r") as f:
        assert np.allclose(f["data"]["current_A"][0, 0, 0], 1e-6)
        assert np.allclose(f["data"]["current_A"][0, 1, 0], 5e-6)


def test_value_slot_metadata_carries_label_map(station, tmp_path):
    """The HDF5 metadata's procedure_params records each slot's axis values."""
    import json

    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    proc.initiate()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        params = json.loads(f["metadata"].attrs["procedure_params"])
    assert params["loop1_parameter"] == "dc_measurement.current_A"
    assert params["loop1_values"] == [1e-6, -1e-6]  # index 0 -> 1e-6, index 1 -> -1e-6
    assert params["loop2_parameter"] == ""
    assert params["loop2_values"] == []


def test_value_slot_bad_entry_refused(station, tmp_path):
    """An entry that does not parse as the parameter's type fails loudly."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    with pytest.raises(CryoSoftConfigError, match="abc"):
        _field_proc(
            station, tmp_path,
            {**DC, "loop1_parameter": "dc_measurement.current_A",
             "loop1_values": "1e-6, abc"},
        )


def test_non_loopable_parameter_refused(station, tmp_path):
    """Looping a parameter no VI advertised a setter for fails at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    with pytest.raises(CryoSoftConfigError, match="voltmeter_range_V"):
        _field_proc(
            station, tmp_path,
            {**DC, "loop1_parameter": "dc_measurement.voltmeter_range_V",
             "loop1_values": "0.1, 1.0"},
        )


def test_same_parameter_in_both_slots_refused(station, tmp_path):
    """The same loopable parameter cannot occupy both slots."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    with pytest.raises(CryoSoftConfigError, match="both"):
        _field_proc(
            station, tmp_path,
            {**DC,
             "loop1_parameter": "dc_measurement.current_A", "loop1_values": "1e-6, -1e-6",
             "loop2_parameter": "dc_measurement.current_A", "loop2_values": "2e-6, -2e-6"},
        )


# ── Both slots: channels (outer) x currents (inner) ──────────────────────────

def test_both_slots_compose(station, tmp_path):
    """Slot 1 x slot 2: a real (n_loop1, n_loop2) axis, route outer, value inner."""
    proc = _field_proc_scanner(station, tmp_path, DC, BOTH)
    proc.initiate()
    schema = proc._data_schema
    _arm(station, DC, proc)

    assert set(schema.measurement_arrays) == {"voltage_V_array", "current_A_array"}
    assert schema.loop_shape == (2, 2)  # 2 routes (loop1) x 2 currents (loop2)
    assert proc._params["loop1_values"] == ["Mux-Ch1", "Mux-Ch2"]
    assert proc._params["loop2_values"] == [1e-6, -1e-6]

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

    filepath = proc._data_manager.filepath
    proc.standby()

    flat = [
        (c.method, c.kwargs.get("route") or c.kwargs.get("current_A"))
        for batch in calls for c in batch
    ]
    assert flat == [
        ("select_route", "Mux-Ch1"),
        ("set_source_current", 1e-6), ("set_source_current", -1e-6),
        ("select_route", "Mux-Ch2"),
        ("set_source_current", 1e-6), ("set_source_current", -1e-6),
    ]

    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V_array"].shape == (1, 2, 2, 5)
        # loop1 index 1 (Mux-Ch2) x loop2 index 1 (-1e-6).
        assert np.allclose(f["data"]["current_A"][0, 1, 1], -1e-6)


# ── Form group + live-plot hooks ─────────────────────────────────────────────

def test_reading_loop_group_offers_all_loopable_parameters(station):
    """One group, two slots; selecting a slot reveals its values input."""
    station.set_scanner_enabled(True)
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "dc_measurement"}
    )
    loop = next(g for g in groups if g.key == "reading_loop")
    # Both slot drop-downs offer Off + route + current_A.
    spec = loop.params["loop1_parameter"]
    assert set(spec.choices.values()) == {
        "", "switch_matrix.route", "dc_measurement.current_A",
    }
    assert spec.structural is True
    # No slot selected -> no values inputs yet.
    assert set(loop.params) == {"loop1_parameter", "loop2_parameter"}

    # Selecting the (enumerated) route reveals per-choice pick checkboxes;
    # selecting the (free) current reveals the comma-separated text field.
    groups = FieldSweep.get_param_groups(
        station,
        {"measurement_vi": "dc_measurement",
         "loop1_parameter": "switch_matrix.route",
         "loop2_parameter": "dc_measurement.current_A"},
    )
    loop = next(g for g in groups if g.key == "reading_loop")
    names = list(loop.params)
    assert names[0] == "loop1_parameter"
    assert [n for n in names if n.startswith("loop1_pick_")] == [
        "loop1_pick_Mux-Ch1", "loop1_pick_Mux-Ch2",
        "loop1_pick_Mux-Ch3", "loop1_pick_Mux-Ch4",
    ]
    assert "loop2_values" in names
    # The loop group sits ABOVE the selected VI's own parameter group.
    keys = [g.key for g in groups]
    assert keys.index("reading_loop") < keys.index("measurement:dc_measurement")


def test_reading_loop_group_absent_when_nothing_loopable(station):
    """Scanner off + a VI without setters (lock-in): no Reading loop group.

    The lock-in is the station's only measurement VI declaring no
    ``reading_setters``; delta and DC both declare one, so an empty registry
    can only be reached through this VI with the scanner off.
    """
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "lockin_harmonic"}
    )
    assert not any(g.key == "reading_loop" for g in groups)
    # Scanner on: even the setter-less lock-in gets the group (route is loopable).
    station.set_scanner_enabled(True)
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "lockin_harmonic"}
    )
    assert any(g.key == "reading_loop" for g in groups)


def test_live_plot_keys_stay_plain_and_loop_labels_drive_the_selectors(station):
    """Axis keys stay plain (arrays excluded); loop_labels map axis index -> display."""
    station.set_scanner_enabled(True)
    on = {
        "measurement_vi": "dc_measurement",
        "loop1_parameter": "switch_matrix.route",
        "loop1_pick_Mux-Ch1": True,
        "loop1_pick_Mux-Ch2": True,
        "loop2_parameter": "dc_measurement.current_A",
        "loop2_values": "1e-6, -1e-6",
    }
    assert FieldSweep.live_plot_measurement_keys(station, on) == [
        "voltage_V", "voltage_V_error", "current_A", "current_A_error",
    ]
    labels1, labels2 = FieldSweep.live_plot_loop_labels(station, on)
    assert labels1 == {0: "A1 = Mux-Ch1", 1: "A2 = Mux-Ch2"}
    assert labels2 == {0: "B1 = 1e-06", 1: "B2 = -1e-06"}
    # Slots off -> ({}, {}) (selectors visible, disabled).
    assert FieldSweep.live_plot_loop_labels(
        station, {"measurement_vi": "dc_measurement"}
    ) == ({}, {})


def test_live_plot_loop_labels_none_when_nothing_loopable(station):
    """No switch (scanner off) + no setters (lock-in): (None, None) — hidden."""
    assert FieldSweep.live_plot_loop_labels(
        station, {"measurement_vi": "lockin_harmonic"}
    ) == (None, None)


# ── Externally-configured standard: the measurement group self-filters ──────
# The RTM2 measurement VI isn't in sim_cryostat's devices.yaml (its driver
# needs a real TCP address), so it's registered directly onto the station
# fixture with Station.register_vi() — the same dependency-injection path
# build_station() itself uses — rather than editing any config.

_RTM2_OWNED = {
    "current_amplitude_A", "averaging_time_s", "analysis_mode", "switch_sequence",
}


def _register_rtm2(station, configured_externally: bool) -> TensormeterRTM2MeasurementVI:
    vi = TensormeterRTM2MeasurementVI(
        {"tensormeter": SimTensormeterRTM2("SIM")},
        configured_externally=configured_externally,
    )
    station.register_vi("tensormeter_measurement", vi, "measurement")
    return vi


def test_measurement_group_full_set_and_plain_title_when_not_external(station):
    """Default (configured_externally False): every declared param renders, plain title."""
    _register_rtm2(station, False)
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "tensormeter_measurement"}
    )
    meas_group = next(
        g for g in groups if g.key == "measurement:tensormeter_measurement"
    )
    assert set(meas_group.params) == set(TensormeterRTM2MeasurementVI.measurement_parameters)
    assert meas_group.title == "Resistance tensor (RTM2)"


def test_measurement_group_hides_owned_params_and_marks_title_when_external(station):
    """configured_externally True: the group omits the owned four; title says why."""
    _register_rtm2(station, True)
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "tensormeter_measurement"}
    )
    meas_group = next(
        g for g in groups if g.key == "measurement:tensormeter_measurement"
    )
    assert (
        set(meas_group.params)
        == set(TensormeterRTM2MeasurementVI.measurement_parameters) - _RTM2_OWNED
    )
    # Data-path parameters stay visible — they write nothing to the instrument.
    assert "tensor_component" in meas_group.params
    assert "readings_per_point" in meas_group.params
    for name in _RTM2_OWNED:
        assert name not in meas_group.params
    assert meas_group.title == "Resistance tensor (RTM2) — externally configured"


# ── End-to-end Orchestrator runs ─────────────────────────────────────────────

def test_full_orchestrator_run_channel_slot(station, tmp_path, qtbot):
    """A 2-route channel-slot sweep completes to IDLE with a real loop1 axis."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
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
        assert f["data"]["voltage_V_array"].shape == (3, 2, 1, 5)
        assert not np.any(np.isnan(f["data"]["voltage_V_array"][:, 0, 0]))
        assert not np.any(np.isnan(f["data"]["voltage_V_array"][:, 1, 0]))


def test_full_orchestrator_run_value_slot(station, tmp_path, qtbot):
    """A +/- current sweep completes to IDLE with a real loop1 axis."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert orch._state == OrchestratorState.IDLE
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as f:
        assert f["data"]["field_T"].shape[0] == 3
        assert f["data"]["voltage_V_array"].shape == (3, 2, 1, 5)
        assert np.allclose(f["data"]["current_A"][:, 1, 0], -1e-6)


def test_full_orchestrator_run_releases_the_rtm2_session_on_standby(
    station, tmp_path, qtbot
):
    """The RTM2's detach-when-idle release fires through a real Orchestrator run.

    Every other check of this standard (test_tensormeter_measurement_vi.py,
    test_connection_lifecycle.py, test_conformance.py) exercises the VI
    directly. This is the end-to-end gap: SweepMeasureProcedure.standby(),
    dispatched by a real Orchestrator at the end of a real run, must really
    release the externally configured RTM2's driver session — proof the
    declaration (BaseVirtualInstrument.detach_when_idle, see its class
    docstring) is framework behaviour all the way through the tick loop,
    not something the procedure or Orchestrator special-cases for this one
    VI.
    """
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_z._default_ramp_rate = 6000.0
    station.magnet_z._ramp_segments = []

    vi = _register_rtm2(station, configured_externally=True)
    driver = vi._main
    driver._averaging_time_s = 0.0  # keep the test fast
    assert driver._closed is True  # born detached
    assert vi.is_attached() is False

    proc = _field_proc(station, tmp_path, {"measurement_vi": "tensormeter_measurement"})
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert orch._state == OrchestratorState.IDLE
    assert vi.is_attached() is False
    assert driver._closed is True


# ── Raw diagnostic blocks: loop-axis skip standard ───────────────────────────
# See MeasurementInstrumentBase's "Raw diagnostic blocks" standard and
# DataSchema.measurement_blocks: unlike every other measurement column, a
# raw block carries the (n_loop1, n_loop2) loop axis only when a reading
# loop is actually configured for the run.

RTM2_FAST = {
    "measurement_vi": "tensormeter_measurement",
    "averaging_time_s": 0.0,
    "readings_per_point": 2,
}


def test_raw_block_no_loop_axis_when_no_reading_loop(station, tmp_path):
    """No reading loop configured: raw_channels_block is stored bare (N, rows, cols).

    Only sweep_index 0 is ever measure()'d (a single call, not a full sweep
    loop), so standby()'s close() trims the file's declared 3 points down to
    the 1 actually saved — same trimming test_field_sweep_measure_saves_data
    relies on for its own single-point shape assertion.
    """
    _register_rtm2(station, False)
    proc = _field_proc(station, tmp_path, RTM2_FAST)
    proc.initiate()
    _arm(station, RTM2_FAST, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        assert f["data"]["raw_channels_block"].shape == (1, 2, 44)
        assert not np.any(np.isnan(f["data"]["raw_channels_block"][0]))


def test_raw_block_carries_loop_axis_when_reading_loop_active(station, tmp_path):
    """A configured reading loop (switch route): the block gains the (n_loop1, n_loop2) axis."""
    _register_rtm2(station, False)
    proc = _field_proc_scanner(station, tmp_path, RTM2_FAST, ROUTES2)
    proc.initiate()
    _arm(station, RTM2_FAST, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        assert f["data"]["raw_channels_block"].shape == (1, 2, 1, 2, 44)
        assert not np.any(np.isnan(f["data"]["raw_channels_block"][0, 0, 0]))
        assert not np.any(np.isnan(f["data"]["raw_channels_block"][0, 1, 0]))


# ── Raw diagnostic blocks: per-channel plot columns ──────────────────────────
# See MeasurementInstrumentBase's "Raw diagnostic blocks" standard's
# plot-column extension: SweepMeasureProcedure derives one row-mean scalar
# column per declared raw-block channel, alongside the untouched block.

def test_live_plot_keys_include_raw_block_channels(station):
    """Every RTM2 raw-block channel is an independently selectable plot key."""
    _register_rtm2(station, False)
    keys = FieldSweep.live_plot_measurement_keys(
        station, {"measurement_vi": "tensormeter_measurement"}
    )
    assert set(_RAW_CHANNEL_NAMES) <= set(keys)
    assert "res_a_ohm" in keys
    assert "res_a_ohm_array" not in keys  # arrays stay excluded


def test_raw_block_channels_saved_as_scalars_matching_block_mean(station, tmp_path):
    """Each channel is saved as its own (N, 1, 1) scalar, equal to the block's
    row-mean for that channel — the block itself is unchanged."""
    _register_rtm2(station, False)
    proc = _field_proc(station, tmp_path, RTM2_FAST)
    proc.initiate()
    _arm(station, RTM2_FAST, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        block = f["data"]["raw_channels_block"][0]  # (rows, cols)
        assert block.shape == (2, 44)
        for col_index, label in enumerate(_RAW_CHANNEL_NAMES):
            assert f["data"][label].shape == (1, 1, 1)
            expected = np.nanmean(block[:, col_index])
            assert f["data"][label][0, 0, 0] == pytest.approx(expected)


def test_raw_block_channels_carry_loop_axis_when_reading_loop_active(station, tmp_path):
    """Channel columns get the real (n_loop1, n_loop2) grid, same as res_a_ohm —
    unlike the block itself, which only gains that axis conditionally."""
    _register_rtm2(station, False)
    proc = _field_proc_scanner(station, tmp_path, RTM2_FAST, ROUTES2)
    proc.initiate()
    _arm(station, RTM2_FAST, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        label = _RAW_CHANNEL_NAMES[0]
        assert f["data"][label].shape == (1, 2, 1)
        block = f["data"]["raw_channels_block"]
        for i1 in range(2):
            expected = np.nanmean(block[0, i1, 0, :, 0])
            assert f["data"][label][0, i1, 0] == pytest.approx(expected)


def test_raw_block_channel_collision_raises_config_error(station, tmp_path):
    """A raw-block channel label colliding with an existing scalar column is refused."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    vi = _register_rtm2(station, False)
    # Shadow the class-level declaration for this instance only: a raw block
    # whose one channel collides with the VI's own res_a_ohm scalar column.
    vi.measurement_raw_blocks = {"raw_channels_block": ["res_a_ohm"]}
    proc = _field_proc(station, tmp_path, RTM2_FAST)
    with pytest.raises(CryoSoftConfigError, match="res_a_ohm"):
        proc.initiate()


# ── Temperature-channel on/off toggles ───────────────────────────────────────
# Both sweep procedures expose set_vti_temperature / set_sample_temperature.
# "Off" means the procedure emits no Target for that VI, so the Orchestrator
# never ramps it and the controller holds where the operator left it. Reading
# is unaffected — it comes from the monitor pass, not from targets.

def test_field_sweep_sets_vti_and_not_sample_by_default(station, tmp_path):
    """Default toggles preserve the pre-toggle behaviour exactly."""
    proc = _field_proc(station, tmp_path, DC)
    targets = proc.initiate().targets
    assert targets["temperature_vti"].target == pytest.approx(FAST_FIELD["temperature"])
    assert "temperature_sample" not in targets
    proc.standby()


def test_field_sweep_vti_off_emits_no_vti_target(station, tmp_path):
    """set_vti_temperature=False drops the VTI target but keeps the field ramp."""
    proc = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_FIELD, **DC, "set_vti_temperature": False},
    )
    targets = proc.initiate().targets
    assert "temperature_vti" not in targets
    assert "magnet_z" in targets
    proc.standby()


def test_field_sweep_sample_on_emits_sample_target(station, tmp_path):
    """set_sample_temperature=True ramps temperature_sample to its own setpoint."""
    proc = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_FIELD, **DC,
           "set_sample_temperature": True, "sample_temperature": 42.0},
    )
    targets = proc.initiate().targets
    assert targets["temperature_sample"].target == pytest.approx(42.0)
    assert targets["temperature_vti"].target == pytest.approx(FAST_FIELD["temperature"])
    proc.standby()


def test_field_sweep_both_channels_off_emits_no_temperature_targets(station, tmp_path):
    """Both toggles off leaves the field sweep with no temperature targets at all."""
    proc = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_FIELD, **DC, "set_vti_temperature": False,
           "set_sample_temperature": False},
    )
    targets = proc.initiate().targets
    assert not {"temperature_vti", "temperature_sample"} & set(targets)
    proc.standby()


def test_field_sweep_enabled_channel_without_vi_is_refused(tmp_path):
    """Switching a channel ON that the station lacks must fail at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station = _partial_station("magnet_z", "temperature_vti", "dc_measurement")
    with pytest.raises(CryoSoftConfigError, match="temperature_sample"):
        TemperatureSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **{**FAST_TEMP, **DC, "set_sample_temperature": True},
        )


def test_field_sweep_disabled_channel_without_vi_is_allowed(tmp_path):
    """A switched-OFF channel is not required to exist on the station."""
    station = _partial_station("magnet_z", "temperature_vti", "dc_measurement")
    proc = FieldSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_FIELD, **DC, "set_sample_temperature": False},
    )
    assert "temperature_sample" not in proc.initiate().targets
    proc.standby()


def test_temp_sweep_vti_off_emits_no_targets_on_initiate_or_step(station, tmp_path):
    """With the swept channel off, the sweep measures without commanding the VTI."""
    proc = TemperatureSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_TEMP, **DC, "set_vti_temperature": False},
    )
    assert "temperature_vti" not in proc.initiate().targets
    step = proc.change_sweep_step()
    assert step is not None and "temperature_vti" not in step.targets
    proc.standby()


def test_temp_sweep_vti_runs_below_sample_by_offset(station, tmp_path):
    """The sample goes to the swept value; the VTI to the swept value minus vti_offset."""
    proc = TemperatureSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_TEMP, **DC,
           "set_sample_temperature": True, "vti_offset": 2.5},
    )
    initial = proc.initiate().targets
    assert initial["temperature_sample"] == Target(300.0, rate=6000.0)
    assert initial["temperature_vti"] == Target(297.5, rate=6000.0)
    step = proc.change_sweep_step()
    assert step is not None
    assert step.targets["temperature_sample"] == Target(300.0, rate=6000.0)
    assert step.targets["temperature_vti"] == Target(297.5, rate=6000.0)
    proc.standby()


def test_temp_sweep_vti_off_ignores_vti_offset(station, tmp_path):
    """vti_offset does nothing while set_vti_temperature is off."""
    proc = TemperatureSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_TEMP, **DC, "set_vti_temperature": False,
           "set_sample_temperature": True, "vti_offset": 5.0},
    )
    targets = proc.initiate().targets
    assert "temperature_vti" not in targets
    assert targets["temperature_sample"].target == pytest.approx(300.0)
    proc.standby()


def test_temp_sweep_axis_readback_is_the_sample_temperature(station, tmp_path):
    """The recorded temperature is the sample stage's read-back, not the VTI's."""
    proc = _temp_proc(station, tmp_path, DC)
    assert proc._axis_readback() == pytest.approx(station.temperature_sample.temperature())
    proc.standby()


def test_temp_sweep_dwell_time_is_the_wait_at_each_point(station, tmp_path):
    """dwell_time is the wait after the initial ramp and after every step."""
    proc = TemperatureSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **{**FAST_TEMP, **DC, "dwell_time": 12.0},
    )
    assert proc.initiate().wait_s == pytest.approx(12.0)
    step = proc.change_sweep_step()
    assert step is not None and step.wait_s == pytest.approx(12.0)
    proc.standby()


def test_temp_sweep_enabled_sample_channel_without_vi_is_refused(tmp_path):
    """Switching the sample channel ON on a station without it fails at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station = _partial_station("magnet_z", "temperature_vti", "dc_measurement")
    with pytest.raises(CryoSoftConfigError, match="temperature_sample"):
        TemperatureSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **{**FAST_TEMP, **DC, "set_sample_temperature": True},
        )



# ── Per-loop wait: a settle time after each change of a looping parameter ────

class _Clock:
    """A hand-wound monotonic clock, so a wait is crossed without sleeping."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    from cryosoft.core import procedure as procedure_module

    fake = _Clock()
    monkeypatch.setattr(procedure_module.time, "monotonic", fake)
    return fake


def test_reading_loop_group_puts_each_wait_under_its_loop_parameter(station):
    """A selected slot gains a seconds field right after its drop-down."""
    groups = FieldSweep.get_param_groups(
        station,
        {"measurement_vi": "dc_measurement",
         "loop1_parameter": "dc_measurement.current_A"},
    )
    loop = next(g for g in groups if g.key == "reading_loop")
    names = list(loop.params)
    assert names[:3] == ["loop1_parameter", "loop1_wait_s", "loop1_values"]
    assert "loop2_wait_s" not in names  # slot 2 is Off
    spec = loop.params["loop1_wait_s"]
    assert (spec.type, spec.unit, spec.min, spec.structural) == (float, "s", 0.0, False)


def test_loop_wait_defaults_to_zero_and_runs_the_grid_in_one_call(station, tmp_path):
    """No wait set: measure() finishes the whole grid in one call, as before."""
    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    proc.initiate()
    _arm(station, DC, proc)
    proc.measure()
    pending = proc.measure_pending
    proc.standby()
    assert pending is False
    assert proc._params["loop1_wait_s"] == 0.0


def test_loop_wait_is_waited_out_across_calls_without_blocking(station, tmp_path, clock):
    """Each change of a looping value waits, then reads; one datapoint results."""
    proc = _field_proc(
        station, tmp_path, {**DC, **CURRENTS2, "loop1_wait_s": 5.0}
    )
    proc.initiate()
    _arm(station, DC, proc)

    proc.measure()  # dispatches value 1, then must wait
    assert proc.measure_pending is True
    assert proc.last_datapoint is None or proc.last_datapoint == {}
    clock.now += 4.9
    proc.measure()  # still settling
    assert proc.measure_pending is True
    clock.now += 0.2
    proc.measure()  # reads 1, dispatches value 2, waits again
    assert proc.measure_pending is True
    clock.now += 5.0
    proc.measure()  # reads 2, saves
    assert proc.measure_pending is False
    assert proc.last_datapoint
    filepath = proc._data_manager.filepath
    proc.standby()
    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V_array"].shape[1] == 2  # both loop values read


def test_inner_wait_applies_before_every_reading_and_outer_adds_on_row_change(
    station, tmp_path, clock
):
    """Loop 2 waits before each reading; a row's first reading also waits loop 1."""
    proc = _field_proc_scanner(
        station, tmp_path, DC, {**BOTH, "loop1_wait_s": 10.0, "loop2_wait_s": 2.0}
    )
    proc.initiate()
    _arm(station, DC, proc)

    ticks = 0
    proc.measure()
    while proc.measure_pending:
        clock.now += 0.5
        ticks += 1
        assert ticks < 200
        proc.measure()
    proc.standby()
    # 2x2 grid: each row's first reading waits 10 + 2 s, the second 2 s.
    assert (clock.now - 1000.0) == pytest.approx(2 * (12.0 + 2.0), abs=0.5)


def test_negative_loop_wait_is_refused(station, tmp_path):
    from cryosoft.core.exceptions import CryoSoftConfigError

    with pytest.raises(CryoSoftConfigError, match="loop1_wait_s"):
        _field_proc(station, tmp_path, {**DC, **CURRENTS2, "loop1_wait_s": -1.0})


def test_abort_drops_a_part_finished_grid(station, tmp_path, clock):
    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2, "loop1_wait_s": 5.0})
    proc.initiate()
    _arm(station, DC, proc)
    proc.measure()
    assert proc.measure_pending is True
    proc.abort()
    assert proc.measure_pending is False


def test_probe_caps_loop_waits_and_estimate_counts_them(station, tmp_path):
    from cryosoft.core.plan import ProbeSpec

    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2, "loop1_wait_s": 90.0})
    base = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    cost = proc.estimate_step_seconds()
    assert cost.measure_s - base.estimate_step_seconds().measure_s == pytest.approx(180.0)
    proc.apply_probe(ProbeSpec())
    assert proc._params["loop1_wait_s"] <= ProbeSpec().max_wait_s


def test_loop_wait_is_honoured_between_ticks_not_rounded_up_to_one(
    station, tmp_path, qtbot
):
    """A 0.3 s wait on a 1 s tick finishes the grid in ~0.6 s, not at the next ticks."""
    import time

    from cryosoft.core.orchestrator import Orchestrator

    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2, "loop1_wait_s": 0.3})
    calls: list[float] = []
    real_measure = proc.measure

    def timed_measure() -> None:
        calls.append(time.monotonic())
        real_measure()

    proc.measure = timed_measure
    orch = Orchestrator(station, tick_interval_ms=1000)
    orch.run_procedure(proc)
    with qtbot.waitSignal(orch.measurement_ready, timeout=60000):
        pass

    # Two waits of 0.3 s. Rounded up to ticks it would take >= 2 s. The waits
    # are shorter than the tick, so no tick runs inside them: three measure()
    # calls (dispatch+wait, read+dispatch+wait, read+save); a timer firing a
    # millisecond early adds a retry, a tick would add seconds.
    assert 3 <= len(calls) <= 5
    assert 0.55 <= calls[-1] - calls[0] < 1.2
    orch.abort_procedure()


def test_tick_runs_during_a_wait_only_if_more_than_a_tick_period_is_left(
    station, tmp_path
):
    """A pending wait of 5 s on a 3 s tick: tick allowed at 5 s left, skipped at 2 s."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2, "loop1_wait_s": 5.0})
    proc.initiate()
    _arm(station, DC, proc)
    orch = Orchestrator(station, tick_interval_ms=3000)
    orch._timer.stop()
    orch._procedure = proc
    orch._state = OrchestratorState.MEASURING

    proc.measure()  # dispatches value 1, waits 5 s
    assert proc.measure_pending is True
    assert orch._tick_would_disturb_wait() is False  # ~5 s left > 3 s tick
    proc._measure_cursor["ready_at"] -= 2.5  # ~2.5 s left <= 3 s tick
    assert orch._tick_would_disturb_wait() is True
    orch._state = OrchestratorState.RAMPING
    assert orch._tick_would_disturb_wait() is False
    proc.abort()


# ── Route settings: the measurement re-armed per switch route ────────────────
# keithley_dc_mode declares route_parameters (current, voltmeter_range_V,
# compliance_V); with route_settings on, each picked route carries its own.

DCMODE = {"measurement_vi": "keithley_dc_mode", "n_readings": 3, "delay_s": 0.0}
ROUTE_SETTINGS = {
    **ROUTES2,
    "route_settings": True,
    "route_Mux-Ch1__current": 1e-6,
    "route_Mux-Ch1__voltmeter_range_V": 0.01,
    "route_Mux-Ch1__compliance_V": 1.0,
    "route_Mux-Ch2__current": 1e-7,
    "route_Mux-Ch2__voltmeter_range_V": 0.0,  # autorange
    "route_Mux-Ch2__compliance_V": 2.0,
}


def _spy_commands(station):
    """Record every batch sent through send_measurement_commands (and send it)."""
    calls = []
    original = station.send_measurement_commands

    def spy(commands, **kwargs):
        calls.append(list(commands))
        return original(commands, **kwargs)

    station.send_measurement_commands = spy
    return calls


def _flat(calls):
    return [(c.vi_name, c.method, dict(c.kwargs)) for batch in calls for c in batch]


def test_route_settings_first_route_arms_the_measurement(station, tmp_path):
    """initiate() connects the first route, then arms with ITS values."""
    proc = _field_proc_scanner(station, tmp_path, DCMODE, ROUTE_SETTINGS)
    plan = proc.initiate()
    proc.standby()

    assert [(c.vi_name, c.method) for c in plan.commands] == [
        ("switch_matrix", "select_route"),
        ("keithley_dc_mode", "initiate_measurement"),
    ]
    arm = plan.commands[1].kwargs
    assert arm["current"] == 1e-6
    assert arm["voltmeter_range_V"] == 0.01
    assert arm["compliance_V"] == 1.0
    assert arm["n_readings"] == 3


def test_route_settings_switch_off_and_rearm_on_route_change(station, tmp_path):
    """A route whose settings differ: standby, select_route, re-arm with its values."""
    import json

    proc = _field_proc_scanner(station, tmp_path, DCMODE, ROUTE_SETTINGS)
    plan = proc.initiate()
    station.send_measurement_commands(plan.commands)
    calls = _spy_commands(station)
    meter = station.get_vi("keithley_dc_mode")._meter
    filepath = proc._data_manager.filepath
    try:
        proc.measure()
        assert meter._auto_range is True  # Mux-Ch2 armed with autorange
        first = _flat(calls)
        calls.clear()
        proc.change_sweep_step()
        proc.measure()  # next datapoint starts back on Mux-Ch1
        second = _flat(calls)
    finally:
        proc.standby()

    # Mux-Ch1 is already armed -> a plain select; Mux-Ch2 differs -> re-arm.
    assert [(vi, m) for vi, m, _ in first] == [
        ("switch_matrix", "select_route"),
        ("keithley_dc_mode", "standby"),
        ("switch_matrix", "select_route"),
        ("keithley_dc_mode", "initiate_measurement"),
    ]
    rearm = first[3][2]
    assert (rearm["current"], rearm["voltmeter_range_V"], rearm["compliance_V"]) == (
        1e-7, 0.0, 2.0,
    )
    assert second[0][:2] == ("keithley_dc_mode", "standby")
    assert second[2][2]["current"] == 1e-6
    assert second[2][2]["voltmeter_range_V"] == 0.01

    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V_array"].shape[1:] == (2, 1, 3)
        params = json.loads(f["metadata"].attrs["procedure_params"])
    assert params["route_settings_values"]["Mux-Ch2"] == {
        "current": 1e-7, "voltmeter_range_V": 0.0, "compliance_V": 2.0,
    }


def test_route_settings_equal_routes_never_switch_the_source_off(station, tmp_path):
    """Routes with identical settings keep the plain select_route path."""
    same = {
        **ROUTES2,
        "route_settings": True,
        "route_Mux-Ch2__current": 1e-6,  # Mux-Ch1 falls back to the run-wide 1e-6
    }
    proc = _field_proc_scanner(station, tmp_path, {**DCMODE, "current": 1e-6}, same)
    plan = proc.initiate()
    station.send_measurement_commands(plan.commands)
    calls = _spy_commands(station)
    try:
        proc.measure()
    finally:
        proc.standby()
    assert [m for _, m, _ in _flat(calls)] == ["select_route", "select_route"]


def test_route_settings_rearm_restores_an_outer_looped_current(station, tmp_path):
    """Route inner, current outer: re-arming is followed by the outer current."""
    loop = {
        "loop1_parameter": "keithley_dc_mode.current",
        "loop1_values": "1e-6, 2e-6",
        "loop2_parameter": "switch_matrix.route",
        "loop2_pick_Mux-Ch1": True,
        "loop2_pick_Mux-Ch2": True,
        "route_settings": True,
        "route_Mux-Ch2__voltmeter_range_V": 0.1,
    }
    proc = _field_proc_scanner(station, tmp_path, DCMODE, loop)
    # The looped current is not a per-route setting.
    assert set(proc._route_settings["Mux-Ch1"]) == {"voltmeter_range_V", "compliance_V"}
    plan = proc.initiate()
    station.send_measurement_commands(plan.commands)
    calls = _spy_commands(station)
    try:
        proc.measure()
    finally:
        proc.standby()

    flat = _flat(calls)
    rearms = [i for i, (_, m, _) in enumerate(flat) if m == "initiate_measurement"]
    assert rearms
    for i in rearms:
        assert flat[i + 1][1] == "set_dc_current"
    # The second outer row re-arms with the row's current, 2e-6.
    assert flat[rearms[-1] + 1][2] == {"current": 2e-6}


@pytest.mark.parametrize(
    "extra, match",
    [
        ({"route_Mux-Ch2__current": 1.0}, "outside the setup's limits"),
        ({"route_Mux-Ch2__voltmeter_range_V": 0.5}, "allowed choices"),
        ({"route_Mux-Ch2__current": "abc"}, "not a valid float"),
    ],
)
def test_route_settings_bad_value_refused_at_build(station, tmp_path, extra, match):
    """A bad per-route value fails when the run is built, not at a route change."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    with pytest.raises(CryoSoftConfigError, match=match):
        _field_proc_scanner(station, tmp_path, DCMODE, {**ROUTE_SETTINGS, **extra})


def test_route_settings_need_the_route_in_a_slot(station, tmp_path):
    """route_settings without the switch's route in a loop slot is refused."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    loop = {
        "loop1_parameter": "keithley_dc_mode.current",
        "loop1_values": "1e-6, -1e-6",
        "route_settings": True,
    }
    with pytest.raises(CryoSoftConfigError, match="route settings need"):
        _field_proc_scanner(station, tmp_path, DCMODE, loop)


def test_route_settings_form_groups(station):
    """The toggle sits in the Reading loop; its fields get their own group.

    The run-wide fields they replace leave the measurement group.
    """
    station.set_scanner_enabled(True)
    selections = {
        "measurement_vi": "keithley_dc_mode",
        "loop1_parameter": "switch_matrix.route",
        "loop1_pick_Mux-Ch1": True,
        "loop1_pick_Mux-Ch3": True,
    }
    groups = {g.key: g for g in FieldSweep.get_param_groups(station, selections)}
    assert "route_settings" in groups["reading_loop"].params
    assert "route_settings" not in groups
    assert "current" in groups["measurement:keithley_dc_mode"].params

    groups = {
        g.key: g
        for g in FieldSweep.get_param_groups(station, {**selections, "route_settings": True})
    }
    assert list(groups["route_settings"].params) == [
        f"route_{route}__{param}"
        for route in ("Mux-Ch1", "Mux-Ch3")
        for param in ("current", "voltmeter_range_V", "compliance_V")
    ]
    meas = groups["measurement:keithley_dc_mode"].params
    assert not {"current", "voltmeter_range_V", "compliance_V"} & set(meas)
    assert "n_readings" in meas


def test_route_settings_not_offered_for_a_vi_without_route_parameters(station):
    """A measurement VI declaring no route_parameters gets no toggle."""
    station.set_scanner_enabled(True)
    selections = {
        "measurement_vi": "keithley_delta_mode",
        "loop1_parameter": "switch_matrix.route",
        "loop1_pick_Mux-Ch1": True,
    }
    groups = {g.key: g for g in FieldSweep.get_param_groups(station, selections)}
    assert "route_settings" not in groups["reading_loop"].params


# ── SS DC mode: route settings with the heater current held on ───────────────

SSDC = {"measurement_vi": "keithley_ss_dc_mode", "n_readings": 3, "delay_s": 0.0}
SS_ROUTES = {
    **ROUTES2,
    "route_settings": True,
    "route_Mux-Ch1__current": 1e-3,
    "route_Mux-Ch1__voltmeter_range_V": 0.01,
    "route_Mux-Ch2__current": 1e-3,
    "route_Mux-Ch2__voltmeter_range_V": 0.0,  # only the voltmeter differs
}


def test_ss_dc_mode_route_change_applies_settings_without_standby(station, tmp_path):
    """A route_setter VI: select_route then apply_route_settings — no standby, no re-arm."""
    proc = _field_proc_scanner(station, tmp_path, SSDC, SS_ROUTES)
    plan = proc.initiate()
    station.send_measurement_commands(plan.commands)
    calls = _spy_commands(station)
    try:
        proc.measure()
        proc.change_sweep_step()
        proc.measure()
    finally:
        proc.abort()

    flat = _flat(calls)
    methods = [m for _, m, _ in flat]
    assert "standby" not in methods
    assert "initiate_measurement" not in methods
    applied = [kw for _, m, kw in flat if m == "apply_route_settings"]
    assert applied[0] == {"current": 1e-3, "voltmeter_range_V": 0.0, "compliance_V": 1.0}
    assert applied[1]["voltmeter_range_V"] == 0.01  # back on Mux-Ch1


def test_ss_dc_mode_source_never_zeroed_during_the_run(station, tmp_path):
    """Across arming, route changes and two datapoints the 6221 never sees 0 A."""
    source = station.get_vi("keithley_ss_dc_mode")._source
    currents = []
    original = source.set_current
    source.set_current = lambda value: (currents.append(value), original(value))[1]
    proc = _field_proc_scanner(
        station, tmp_path, SSDC, {**SS_ROUTES, "route_Mux-Ch2__current": 2e-3}
    )
    plan = proc.initiate()
    station.send_measurement_commands(plan.commands)
    proc.measure()
    proc.change_sweep_step()
    proc.measure()
    end = proc.standby()
    station.send_measurement_commands(end.commands)
    source.set_current = original

    assert currents and 0.0 not in currents
    assert 2e-3 in currents


def test_ss_dc_mode_normal_end_disarms_abort_switches_off(station, tmp_path):
    """Normal end dispatches run_end_method (disarm); abort always standby."""
    proc = _field_proc_scanner(station, tmp_path, SSDC, SS_ROUTES)
    proc.initiate()
    end = proc.standby()
    assert end.commands[0] == Command("keithley_ss_dc_mode", "disarm", {})

    proc = _field_proc_scanner(station, tmp_path, SSDC, SS_ROUTES)
    proc.initiate()
    aborted = proc.abort()
    assert aborted[0] == Command("keithley_ss_dc_mode", "standby", {})


def test_dc_mode_normal_end_still_switches_off(station, tmp_path):
    """The default run_end_method is standby: DC mode is unchanged."""
    proc = _field_proc_scanner(station, tmp_path, DCMODE, ROUTE_SETTINGS)
    proc.initiate()
    assert proc.standby().commands[0] == Command("keithley_dc_mode", "standby", {})
