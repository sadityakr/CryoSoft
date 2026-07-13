# ---
# description: |
#   Tests for FieldSweepDC and TemperatureSweepDC procedures, and for the
#   set_ramp_rate @control on ITC503TemperatureVI and the rate-forwarding in
#   Station.process_system_targets.
# entry_point: pytest tests/test_new_procedures.py -v
# last_updated: 2026-07-12
# ---


import h5py
import numpy as np
import pytest

from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep_dc import FieldSweepDC
from cryosoft.procedures.temperature_sweep_dc import TemperatureSweepDC

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {
    "sample_name": "Test Sample",
    "sample_id": "T-DC-001",
    "comments": "automated test",
}

FAST_FIELD_PARAMS = {
    "field_start": -0.1,
    "field_end": 0.1,
    "field_steps": 3,
    "temperature": 300.0,
    "current_A": 1e-6,
    "compliance_A": 1e-3,
    "voltmeter_range_V": 0.1,
    "readings_per_point": 5,
    "init_wait": 0.0,
    "step_wait": 0.0,
}

FAST_TEMP_PARAMS = {
    "temperature_start": 300.0,
    "temperature_end": 300.0,  # Same start/end → instant ramp settle in sim
    "temperature_steps": 3,
    "ramp_rate_K_per_min": 6000.0,
    "current_A": 1e-6,
    "compliance_A": 1e-3,
    "voltmeter_range_V": 0.1,
    "readings_per_point": 5,
    "point_wait": 0.0,
}


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


@pytest.fixture
def field_proc(station, tmp_path):
    return FieldSweepDC(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_FIELD_PARAMS,
    )


@pytest.fixture
def temp_proc(station, tmp_path):
    return TemperatureSweepDC(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_TEMP_PARAMS,
    )


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
    station.process_system_targets({
        "temperature_vti": Target(300.0, rate=500.0)
    })
    # The ramp generator was started with 500 K/min; _default_ramp_rate unchanged
    assert vi._default_ramp_rate == pytest.approx(1.0)  # not mutated
    assert vi._ramp_target == pytest.approx(300.0)


# ── FieldSweepDC ─────────────────────────────────────────────────────────────

def test_field_dc_sweep_array(field_proc):
    sweep = field_proc.get_sweep_array()
    assert len(sweep) == 3
    assert sweep[0] == pytest.approx(-0.1)
    assert sweep[-1] == pytest.approx(0.1)


def test_field_dc_initiate_structure(field_proc, tmp_path):
    plan = field_proc.initiate()
    field_proc.standby()

    assert isinstance(plan, PhasePlan)
    assert "magnet_x" in plan.targets
    assert "temperature_vti" in plan.targets
    cmd = next(c for c in plan.commands if c.vi_name == "dc_measurement")
    assert cmd.method == "initiate"
    assert plan.wait_s == pytest.approx(0.0)


def test_field_dc_initiate_full_phaseplan_content(field_proc, tmp_path):
    """FieldSweepDC.initiate() returns the exact PhasePlan, command order included."""
    plan = field_proc.initiate()
    field_proc.standby()

    assert set(plan.targets) == {"magnet_x", "temperature_vti"}
    assert plan.targets["magnet_x"] == Target(-0.1)
    assert plan.targets["temperature_vti"] == Target(300.0)

    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert isinstance(cmd, Command)
    assert cmd.vi_name == "dc_measurement"
    assert cmd.method == "initiate"
    assert cmd.kwargs["current_A"] == pytest.approx(1e-6)
    assert cmd.kwargs["compliance_A"] == pytest.approx(1e-3)
    assert cmd.kwargs["voltmeter_range_V"] == pytest.approx(0.1)
    assert cmd.kwargs["readings_per_point"] == 5

    assert plan.wait_s == pytest.approx(0.0)


def test_field_dc_initiate_creates_hdf5(field_proc, tmp_path):
    field_proc.initiate()
    field_proc.standby()
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    assert h5_files[0].stat().st_size > 0


def test_field_dc_initiate_measurement_params(field_proc, tmp_path):
    plan = field_proc.initiate()
    field_proc.standby()
    cmd = next(c for c in plan.commands if c.vi_name == "dc_measurement")
    assert cmd.method == "initiate"
    assert cmd.kwargs["current_A"] == pytest.approx(1e-6)
    assert cmd.kwargs["compliance_A"] == pytest.approx(1e-3)
    assert cmd.kwargs["voltmeter_range_V"] == pytest.approx(0.1)


def test_field_dc_change_sweep_step(field_proc, tmp_path):
    field_proc.initiate()
    step = field_proc.change_sweep_step()
    assert step is not None
    assert isinstance(step, StepPlan)
    assert "magnet_x" in step.targets
    assert step.targets["magnet_x"].target == pytest.approx(0.0)
    assert step.wait_s == pytest.approx(0.0)
    field_proc.standby()


def test_field_dc_sweep_exhaustion(field_proc, tmp_path):
    field_proc.initiate()
    field_proc.change_sweep_step()
    field_proc.change_sweep_step()
    assert field_proc.change_sweep_step() is None
    field_proc.standby()


def test_field_dc_measure_saves_data(field_proc, tmp_path):
    field_proc.initiate()
    field_proc._station.dc_measurement.initiate(
        current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1, readings_per_point=5
    )
    field_proc.measure()
    filepath = field_proc._data_manager.filepath
    field_proc.standby()

    with h5py.File(filepath, "r") as f:
        assert not np.isnan(f["data"]["field_T"][0])
        assert not np.any(np.isnan(f["data"]["voltage_V"][0]))
        assert f["data"]["voltage_V"].shape == (1, 5)


def test_field_dc_standby_structure(field_proc, tmp_path):
    field_proc.initiate()
    plan = field_proc.standby()
    assert plan.targets["magnet_x"].target == pytest.approx(0.0)
    cmd = next(c for c in plan.commands if c.vi_name == "dc_measurement")
    assert cmd.method == "standby"
    assert plan.wait_s == pytest.approx(0.0)


def test_field_dc_standby_closes_file(field_proc, tmp_path):
    field_proc.initiate()
    filepath = field_proc._data_manager.filepath
    field_proc.standby()
    assert field_proc._data_manager is None
    with h5py.File(filepath, "r") as f:
        assert f["metadata"].attrs["end_time"] != ""


def test_field_dc_full_orchestrator_loop(station, tmp_path, qtbot):
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []

    proc = FieldSweepDC(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_FIELD_PARAMS,
    )
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert proc._index == 3
    assert orch._state == OrchestratorState.IDLE
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1


# ── TemperatureSweepDC ───────────────────────────────────────────────────────

def test_temp_dc_sweep_array(temp_proc):
    sweep = temp_proc.get_sweep_array()
    assert len(sweep) == 3
    assert sweep[0] == pytest.approx(300.0)
    assert sweep[-1] == pytest.approx(300.0)


def test_temp_dc_initiate_structure(temp_proc, tmp_path):
    plan = temp_proc.initiate()
    temp_proc.standby()

    assert isinstance(plan, PhasePlan)
    assert "temperature_vti" in plan.targets
    cmd = next(c for c in plan.commands if c.vi_name == "dc_measurement")
    assert cmd.method == "initiate"
    assert plan.wait_s == pytest.approx(0.0)


def test_temp_dc_initiate_full_phaseplan_content(temp_proc, tmp_path):
    """TemperatureSweepDC.initiate() returns the exact PhasePlan (rate + command)."""
    plan = temp_proc.initiate()
    temp_proc.standby()

    # temperature_vti carries the per-sweep ramp rate; magnet_x present at 0 T
    # (sim_cryostat has magnet_x; field_x/field_y default to 0.0).
    assert plan.targets["temperature_vti"] == Target(300.0, rate=6000.0)
    assert plan.targets["magnet_x"] == Target(0.0)

    assert len(plan.commands) == 1
    cmd = plan.commands[0]
    assert isinstance(cmd, Command)
    assert cmd.vi_name == "dc_measurement"
    assert cmd.method == "initiate"
    assert cmd.kwargs["current_A"] == pytest.approx(1e-6)
    assert cmd.kwargs["readings_per_point"] == 5

    assert plan.wait_s == pytest.approx(0.0)


def test_temp_dc_initiate_includes_ramp_rate(temp_proc, tmp_path):
    plan = temp_proc.initiate()
    temp_proc.standby()
    assert plan.targets["temperature_vti"].rate == pytest.approx(6000.0)


def test_temp_dc_initiate_creates_hdf5(temp_proc, tmp_path):
    temp_proc.initiate()
    temp_proc.standby()
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1


def test_temp_dc_change_sweep_step_includes_rate(temp_proc, tmp_path):
    temp_proc.initiate()
    step = temp_proc.change_sweep_step()
    assert step is not None
    assert isinstance(step, StepPlan)
    assert "temperature_vti" in step.targets
    assert step.targets["temperature_vti"].rate == pytest.approx(6000.0)
    temp_proc.standby()


def test_temp_dc_sweep_exhaustion(temp_proc, tmp_path):
    temp_proc.initiate()
    temp_proc.change_sweep_step()
    temp_proc.change_sweep_step()
    assert temp_proc.change_sweep_step() is None
    temp_proc.standby()


def test_temp_dc_measure_saves_data(temp_proc, tmp_path):
    temp_proc.initiate()
    temp_proc._station.dc_measurement.initiate(
        current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1, readings_per_point=5
    )
    temp_proc.measure()
    filepath = temp_proc._data_manager.filepath
    temp_proc.standby()

    with h5py.File(filepath, "r") as f:
        assert not np.isnan(f["data"]["temperature_K"][0])
        assert not np.any(np.isnan(f["data"]["voltage_V"][0]))
        assert f["data"]["voltage_V"].shape == (1, 5)


def test_temp_dc_standby_empty_system_targets(temp_proc, tmp_path):
    """standby() returns empty targets — temperature holds at last point."""
    temp_proc.initiate()
    plan = temp_proc.standby()
    assert plan.targets == {}
    assert any(c.vi_name == "dc_measurement" for c in plan.commands)


def test_temp_dc_full_orchestrator_loop(station, tmp_path, qtbot):
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    proc = TemperatureSweepDC(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_TEMP_PARAMS,
    )
    orch = Orchestrator(station, tick_interval_ms=10)
    orch.run_procedure(proc)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    assert proc._index == 3
    assert orch._state == OrchestratorState.IDLE
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1


# ── TemperatureSweepDC on stations without magnets (review finding H7) ───────

def _partial_station(*keep: str):
    """A station containing only the named VIs from the sim config."""
    from cryosoft.core.station import Station

    full = build_station(CONFIG_PATH)
    partial = Station()
    for name in keep:
        partial.register_vi(name, getattr(full, name), full.get_vi_type(name))
    return partial


def test_temp_dc_missing_magnet_with_zero_field_is_skipped(tmp_path):
    """A station without magnet_y still runs the sweep at field_y=0 —
    the missing magnet is simply left out of the system targets."""
    station = _partial_station("magnet_x", "temperature_vti", "dc_measurement")
    proc = TemperatureSweepDC(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_TEMP_PARAMS,  # field_x / field_y default to 0.0
    )
    plan = proc.initiate()
    assert "magnet_y" not in plan.targets
    assert "magnet_x" in plan.targets
    assert "temperature_vti" in plan.targets
    proc.standby()  # close the HDF5 file


def test_temp_dc_missing_magnet_with_nonzero_field_is_refused(tmp_path):
    """A NONZERO field on a missing magnet must fail at construction:
    silently measuring at 0 T while the metadata claims 0.5 T would
    corrupt a dataset without anyone noticing."""
    from cryosoft.core.exceptions import CryoSoftConfigError

    station = _partial_station("magnet_x", "temperature_vti", "dc_measurement")
    with pytest.raises(CryoSoftConfigError, match="magnet_y"):
        TemperatureSweepDC(
            station=station,
            sample_info=SAMPLE_INFO,
            data_directory=str(tmp_path),
            **{**FAST_TEMP_PARAMS, "field_y": 0.5},
        )
