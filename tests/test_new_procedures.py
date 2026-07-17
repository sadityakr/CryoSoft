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
#   The reading-loop section covers the two generic loop slots (the switch's
#   route and the DC VI's current_A are the same loopable-parameter concept):
#   index-label schema/columns ({name}__A{i}__B{j}), static single-value
#   slots, the HDF5 loop_labels metadata, command dispatch through the
#   Station, slot composition and ordering, construction-time refusals, the
#   auto-rendered Reading loop form group, the live-plot label maps, and
#   end-to-end Orchestrator runs for both slot kinds.
# entry_point: pytest tests/test_new_procedures.py -v
# last_updated: 2026-07-17
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
    assert plan.commands[1].method == "initiate"


def test_channel_slot_schema_uses_index_labels(station, tmp_path):
    """Arrays AND scalar columns are expanded per index label (A1, A2)."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    proc.initiate()
    schema = proc._data_schema
    proc.standby()

    assert set(schema.arrays) == {
        "voltage_V__A1", "voltage_V__A2", "current_A__A1", "current_A__A2",
    }
    assert "n_valid" not in schema.sweep_columns
    assert "n_valid__A1" in schema.sweep_columns
    assert "n_valid__A2" in schema.sweep_columns
    assert "field_T" in schema.sweep_columns
    # The label -> value map ties A1/A2 back to the routes in the metadata.
    assert proc._params["loop_labels"] == {"A1": "Mux-Ch1", "A2": "Mux-Ch2"}


def test_channel_slot_measure_writes_labelled_keys(station, tmp_path):
    """measure() loops the routes and writes per-label suffixed datasets."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES2)
    proc.initiate()
    _arm(station, DELTA, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V__A1"].shape == (1, 5)
        assert f["data"]["voltage_V__A2"].shape == (1, 5)
        assert not np.any(np.isnan(f["data"]["voltage_V__A1"][0]))
        assert f["data"]["n_valid__A1"][0] == 5


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
    """One value = static setting: dispatched once at initiate, no suffix."""
    proc = _field_proc_scanner(station, tmp_path, DELTA, ROUTES1)
    plan = proc.initiate()
    schema = proc._data_schema

    # Selected once before arming; schema is plain.
    assert plan.commands[0].vi_name == "switch_matrix"
    assert plan.commands[0].method == "select_route"
    assert set(schema.arrays) == {"voltage_V", "current_A"}
    assert "n_valid" in schema.sweep_columns
    assert proc._params["loop_labels"] == {}

    # measure() takes a plain reading (no re-selection, no suffix).
    _arm(station, DELTA, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()
    with h5py.File(filepath, "r") as f:
        assert "voltage_V" in f["data"]
        assert "voltage_V__A1" not in f["data"]


def test_static_measurement_slot_dispatches_after_arm(station, tmp_path):
    """A static single-value slot on the measurement VI dispatches AFTER arming."""
    proc = _field_proc(
        station, tmp_path,
        {**DC, "loop1_parameter": "dc_measurement.current_A", "loop1_values": "2e-6"},
    )
    plan = proc.initiate()
    proc.standby()

    assert [(c.vi_name, c.method) for c in plan.commands] == [
        ("dc_measurement", "initiate"),
        ("dc_measurement", "set_source_current"),
    ]
    assert plan.commands[1].kwargs == {"current_A": 2e-6}
    assert set(proc._data_schema.arrays) == {"voltage_V", "current_A"}


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
    assert set(proc._data_schema.arrays) == {"voltage_V", "current_A"}


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
    # And the form offers no channel parameter (delta VI: no group at all).
    groups = FieldSweep.get_param_groups(station)
    assert not any(g.key == "reading_loop" for g in groups)


# ── Value-list slot alone (± current) ────────────────────────────────────────

def test_value_slot_labels_and_suffixed_keys(station, tmp_path):
    """A two-value current loop resolves A1/A2 and suffixes the data keys."""
    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    assert [s["qualified"] for s in proc._loop_slots] == ["dc_measurement.current_A"]
    assert proc.measurement_data_keys == [
        "voltage_V__A1", "voltage_V__A2", "current_A__A1", "current_A__A2",
    ]
    assert proc._params["loop_labels"] == {"A1": 1e-6, "A2": -1e-6}


def test_value_slot_measure_writes_signed_current(station, tmp_path):
    """measure() loops the values; the A2 reading carries -current_A."""
    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    proc.initiate()
    _arm(station, DC, proc)
    proc.measure()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        assert f["data"]["voltage_V__A1"].shape == (1, 5)
        assert np.allclose(f["data"]["current_A__A1"][0], 1e-6)
        assert np.allclose(f["data"]["current_A__A2"][0], -1e-6)


def test_value_slot_metadata_carries_label_map(station, tmp_path):
    """The HDF5 metadata's procedure_params records the slots and labels."""
    import json

    proc = _field_proc(station, tmp_path, {**DC, **CURRENTS2})
    proc.initiate()
    filepath = proc._data_manager.filepath
    proc.standby()

    with h5py.File(filepath, "r") as f:
        params = json.loads(f["metadata"].attrs["procedure_params"])
    assert params["loop1_parameter"] == "dc_measurement.current_A"
    assert params["loop1_values"] == [1e-6, -1e-6]
    assert params["loop2_parameter"] == ""
    assert params["loop_labels"] == {"A1": 1e-6, "A2": -1e-6}


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
    """Slot 1 x slot 2: columns {name}__A{i}__B{j}, route outer, value inner."""
    proc = _field_proc_scanner(station, tmp_path, DC, BOTH)
    proc.initiate()
    schema = proc._data_schema
    _arm(station, DC, proc)

    assert set(schema.arrays) == {
        f"{name}__{a}__{b}"
        for name in ("voltage_V", "current_A")
        for a in ("A1", "A2")
        for b in ("B1", "B2")
    }
    assert proc._params["loop_labels"] == {
        "A1": "Mux-Ch1", "A2": "Mux-Ch2", "B1": 1e-6, "B2": -1e-6,
    }

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
        assert f["data"]["voltage_V__A1__B1"].shape == (1, 5)
        assert np.allclose(f["data"]["current_A__A2__B2"][0], -1e-6)


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
    """Scanner off + a VI without setters (delta): no Reading loop group."""
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "keithley_delta_mode"}
    )
    assert not any(g.key == "reading_loop" for g in groups)
    # Scanner on: even delta gets the group (the route is loopable).
    station.set_scanner_enabled(True)
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "keithley_delta_mode"}
    )
    assert any(g.key == "reading_loop" for g in groups)


def test_live_plot_keys_stay_plain_and_loop_labels_drive_the_selectors(station):
    """Axis keys stay plain; live_plot_loop_labels() feeds the two selectors."""
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
        "voltage_V", "current_A",
    ]
    labels1, labels2 = FieldSweep.live_plot_loop_labels(station, on)
    assert labels1 == {"A1": "A1 = Mux-Ch1", "A2": "A2 = Mux-Ch2"}
    assert labels2 == {"B1": "B1 = 1e-06", "B2": "B2 = -1e-06"}
    # Slots off -> ({}, {}) (selectors visible, disabled).
    assert FieldSweep.live_plot_loop_labels(
        station, {"measurement_vi": "dc_measurement"}
    ) == ({}, {})


def test_live_plot_loop_labels_none_when_nothing_loopable(station):
    """No switch (scanner off) + no setters (delta): (None, None) — hidden."""
    assert FieldSweep.live_plot_loop_labels(
        station, {"measurement_vi": "keithley_delta_mode"}
    ) == (None, None)


# ── End-to-end Orchestrator runs ─────────────────────────────────────────────

def test_full_orchestrator_run_channel_slot(station, tmp_path, qtbot):
    """A 2-route channel-slot sweep completes to IDLE with per-label datasets."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

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
        assert f["data"]["voltage_V__A1"].shape == (3, 5)
        assert f["data"]["voltage_V__A2"].shape == (3, 5)
        assert not np.any(np.isnan(f["data"]["voltage_V__A1"][:]))


def test_full_orchestrator_run_value_slot(station, tmp_path, qtbot):
    """A +/- current sweep completes to IDLE with per-label datasets."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

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
        assert f["data"]["voltage_V__A1"].shape == (3, 5)
        assert np.allclose(f["data"]["current_A__A2"][:], -1e-6)
