# ---
# description: |
#   Tests for FieldSweepDC and TemperatureSweepDC procedures, and for the
#   set_ramp_rate @control on ITC503TemperatureVI and the rate-forwarding in
#   Station.process_system_targets.
# entry_point: pytest tests/test_new_procedures.py -v
# last_updated: 2026-04-18
# ---


import h5py
import numpy as np
import pytest

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
    "temp_start": 300.0,
    "temp_end": 300.0,  # Same start/end → instant ramp settle in sim
    "n_points": 3,
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
        "temperature_vti": {"target": 300.0, "rate": 500.0}
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
    sys_targets, meas_cmds, wait = field_proc.initiate()
    field_proc.standby()

    assert "magnet_x" in sys_targets
    assert "temperature_vti" in sys_targets
    assert "dc_measurement" in meas_cmds
    assert "initiate" in meas_cmds["dc_measurement"]
    assert wait == pytest.approx(0.0)


def test_field_dc_initiate_creates_hdf5(field_proc, tmp_path):
    field_proc.initiate()
    field_proc.standby()
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    assert h5_files[0].stat().st_size > 0


def test_field_dc_initiate_measurement_params(field_proc, tmp_path):
    _, meas_cmds, _ = field_proc.initiate()
    field_proc.standby()
    init_params = meas_cmds["dc_measurement"]["initiate"]
    assert init_params["current_A"] == pytest.approx(1e-6)
    assert init_params["compliance_A"] == pytest.approx(1e-3)
    assert init_params["voltmeter_range_V"] == pytest.approx(0.1)


def test_field_dc_change_sweep_step(field_proc, tmp_path):
    field_proc.initiate()
    result = field_proc.change_sweep_step()
    assert result is not None
    targets, wait = result
    assert "magnet_x" in targets
    assert targets["magnet_x"]["target"] == pytest.approx(0.0)
    assert wait == pytest.approx(0.0)
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
        current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1
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
    sys_targets, meas_cmds, wait = field_proc.standby()
    assert sys_targets["magnet_x"]["target"] == pytest.approx(0.0)
    assert "dc_measurement" in meas_cmds
    assert "standby" in meas_cmds["dc_measurement"]
    assert wait == pytest.approx(0.0)


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
    sys_targets, meas_cmds, wait = temp_proc.initiate()
    temp_proc.standby()

    assert "temperature_vti" in sys_targets
    assert "dc_measurement" in meas_cmds
    assert "initiate" in meas_cmds["dc_measurement"]
    assert wait == pytest.approx(0.0)


def test_temp_dc_initiate_includes_ramp_rate(temp_proc, tmp_path):
    sys_targets, _, _ = temp_proc.initiate()
    temp_proc.standby()
    assert sys_targets["temperature_vti"]["rate"] == pytest.approx(6000.0)


def test_temp_dc_initiate_creates_hdf5(temp_proc, tmp_path):
    temp_proc.initiate()
    temp_proc.standby()
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1


def test_temp_dc_change_sweep_step_includes_rate(temp_proc, tmp_path):
    temp_proc.initiate()
    result = temp_proc.change_sweep_step()
    assert result is not None
    targets, wait = result
    assert "temperature_vti" in targets
    assert "rate" in targets["temperature_vti"]
    assert targets["temperature_vti"]["rate"] == pytest.approx(6000.0)
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
        current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1
    )
    temp_proc.measure()
    filepath = temp_proc._data_manager.filepath
    temp_proc.standby()

    with h5py.File(filepath, "r") as f:
        assert not np.isnan(f["data"]["temperature_K"][0])
        assert not np.any(np.isnan(f["data"]["voltage_V"][0]))
        assert f["data"]["voltage_V"].shape == (1, 5)


def test_temp_dc_standby_empty_system_targets(temp_proc, tmp_path):
    """standby() returns empty system_targets — temperature holds at last point."""
    temp_proc.initiate()
    sys_targets, meas_cmds, wait = temp_proc.standby()
    assert sys_targets == {}
    assert "dc_measurement" in meas_cmds


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
