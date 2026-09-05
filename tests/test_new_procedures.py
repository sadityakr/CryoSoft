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
DC = {
    "measurement_vi": "dc_measurement",
    "current_A": 1e-6,
    "compliance_A": 1e-3,
    "voltmeter_range_V": 0.1,
    "readings_per_point": 5,
}
# The current parameter name and per-VI expectations, keyed by measurement VI.
MEAS_META = {
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

FIELD_MEAS = [pytest.param(DC, id="dc")]
TEMP_MEAS = [pytest.param(DC, id="dc")]


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
    assert proc._measurement_vi == station.measurement_vi_names()[0] == "dc_measurement"


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
    # sim_cryostat has magnet_z; field_z defaults to 0.0.
    assert plan.targets["magnet_z"] == Target(0.0)

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
    """A station without magnet_y still runs the sweep at field_y=0."""
    station = _partial_station("magnet_z", "temperature_vti", "dc_measurement")
    proc = TemperatureSweep(
        station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
        **FAST_TEMP, **DC,
    )
    plan = proc.initiate()
    assert "magnet_y" not in plan.targets
    assert "magnet_z" in plan.targets
    assert "temperature_vti" in plan.targets
    proc.standby()


def test_temp_sweep_missing_magnet_with_nonzero_field_is_refused(tmp_path):
    """A NONZERO field on a missing magnet must fail at construction."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station = _partial_station("magnet_z", "temperature_vti", "dc_measurement")
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

# ── The reading loop: two generic slots ──────────────────────────────────────
# Slot 1 (labels A1, A2, ...) is the outer level, slot 2 (B1, B2, ...) the
# inner one. Every slot is the same concept: a loopable parameter the reading
# path advertises via reading_setters, plus an ordered value list.

CURRENTS2 = {"loop1_parameter": "dc_measurement.current_A", "loop1_values": "1e-6, -1e-6"}


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
    proc = _field_proc(
        station, tmp_path, {**DC, "loop1_values": "1e-6, -1e-6"}
    )
    assert proc._loop_slots == []
    plan = proc.initiate()
    proc.standby()
    assert len(plan.commands) == 1
    assert plan.commands[0].vi_name == "dc_measurement"
    assert set(proc._data_schema.measurement_arrays) == {"voltage_V_array", "current_A_array"}
    assert proc._data_schema.loop_shape == (1, 1)


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

def test_reading_loop_group_offers_all_loopable_parameters(station):
    """One group, two slots; selecting a slot reveals its values input."""
    groups = FieldSweep.get_param_groups(
        station, {"measurement_vi": "dc_measurement"}
    )
    loop = next(g for g in groups if g.key == "reading_loop")
    # Both slot drop-downs offer Off + every loopable parameter on the
    # reading path — here the DC VI's own sourced current.
    spec = loop.params["loop1_parameter"]
    assert set(spec.choices.values()) == {"", "dc_measurement.current_A"}
    assert spec.structural is True
    # No slot selected -> no values inputs yet.
    assert set(loop.params) == {"loop1_parameter", "loop2_parameter"}

    # Selecting the (free) current reveals the comma-separated text field.
    groups = FieldSweep.get_param_groups(
        station,
        {"measurement_vi": "dc_measurement",
         "loop1_parameter": "dc_measurement.current_A"},
    )
    loop = next(g for g in groups if g.key == "reading_loop")
    names = list(loop.params)
    assert names[0] == "loop1_parameter"
    assert "loop1_values" in names
    # The loop group sits ABOVE the selected VI's own parameter group.
    keys = [g.key for g in groups]
    assert keys.index("reading_loop") < keys.index("measurement:dc_measurement")


def test_live_plot_keys_stay_plain_and_loop_labels_drive_the_selectors(station):
    """Axis keys stay plain (arrays excluded); loop_labels map axis index -> display."""
    on = {
        "measurement_vi": "dc_measurement",
        "loop1_parameter": "dc_measurement.current_A",
        "loop1_values": "1e-6, -1e-6",
    }
    assert FieldSweep.live_plot_measurement_keys(station, on) == [
        "voltage_V", "voltage_V_error", "current_A", "current_A_error",
    ]
    labels1, labels2 = FieldSweep.live_plot_loop_labels(station, on)
    assert labels1 == {0: "A1 = 1e-06", 1: "A2 = -1e-06"}
    assert labels2 == {}
    # Slots off -> ({}, {}) (selectors visible, disabled).
    assert FieldSweep.live_plot_loop_labels(
        station, {"measurement_vi": "dc_measurement"}
    ) == ({}, {})


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
        FieldSweep(
            station=station, sample_info=SAMPLE_INFO, data_directory=str(tmp_path),
            **{**FAST_FIELD, **DC, "set_sample_temperature": True},
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
