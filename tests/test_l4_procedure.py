# ---
# description: |
#   Integration tests for Layer 4 (Procedures). Tests BaseProcedure subclassing,
#   FieldSweepIV method contracts, DataManager integration, and a full
#   Orchestrator end-to-end loop with FieldSweepIV using simulated instruments.
# last_updated: 2026-04-06
# ---

import json

import h5py
import numpy as np
import pytest

from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import build_station
from cryosoft.procedures.field_sweep_iv import FieldSweepIV

CONFIG_PATH = "cryosoft/configs/sim_cryostat"

SAMPLE_INFO = {
    "sample_name": "Test Sample",
    "sample_id": "T-001",
    "comments": "automated test",
}

# Minimal params that make the sweep fast in tests
FAST_PARAMS = {
    "field_start": -0.1,
    "field_end": 0.1,
    "field_steps": 3,
    "temperature": 300.0,  # Room temperature — sim driver starts at 300 K (instant settle)
    "current": 1e-6,
    "n_readings": 5,
    "init_wait": 0.0,
    "step_wait": 0.0,
}


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


@pytest.fixture
def procedure(station, tmp_path):
    return FieldSweepIV(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_PARAMS,
    )


# ── BaseProcedure contract ────────────────────────────────────────────────────

def test_base_procedure_not_instantiable_without_methods():
    """BaseProcedure.initiate() etc. raise NotImplementedError."""
    class NullProc(BaseProcedure):
        pass

    proc = NullProc(station=None, sample_info={}, data_directory="/tmp")
    with pytest.raises(NotImplementedError):
        proc.initiate()
    with pytest.raises(NotImplementedError):
        proc.change_sweep_step()
    with pytest.raises(NotImplementedError):
        proc.measure()
    with pytest.raises(NotImplementedError):
        proc.standby()


def test_base_procedure_empty_sweep():
    """Default _build_sweep_array returns [] and get_progress returns 1.0."""
    class NullProc(BaseProcedure):
        pass

    proc = NullProc(station=None, sample_info={}, data_directory="/tmp")
    assert proc.get_sweep_array() == []
    assert proc.get_progress() == 1.0


# ── FieldSweepIV instantiation ────────────────────────────────────────────────

def test_instantiation(procedure):
    """FieldSweepIV instantiates without error."""
    assert procedure is not None


def test_sweep_array_length(procedure):
    """Sweep array has the correct number of points."""
    sweep = procedure.get_sweep_array()
    assert len(sweep) == 3


def test_sweep_array_values(procedure):
    """Sweep array spans the correct field range."""
    sweep = procedure.get_sweep_array()
    assert sweep[0] == pytest.approx(-0.1)
    assert sweep[-1] == pytest.approx(0.1)
    assert sweep[1] == pytest.approx(0.0)


def test_initial_progress(procedure):
    """Progress is 0.0 at construction."""
    assert procedure.get_progress() == pytest.approx(0.0)


# ── initiate() ────────────────────────────────────────────────────────────────

def test_initiate_returns_correct_structure(procedure, tmp_path):
    """initiate() returns (system_targets, measurement_commands, wait_time)."""
    sys_targets, meas_cmds, wait = procedure.initiate()
    procedure.standby()  # Close file

    assert "magnet_x" in sys_targets
    assert "target" in sys_targets["magnet_x"]
    assert sys_targets["magnet_x"]["target"] == pytest.approx(-0.1)

    assert "temperature_vti" in sys_targets
    assert sys_targets["temperature_vti"]["target"] == pytest.approx(300.0)

    assert "keithley_delta_mode" in meas_cmds
    assert "configure" in meas_cmds["keithley_delta_mode"]

    assert wait == pytest.approx(0.0)


def test_initiate_creates_hdf5_file(procedure, tmp_path):
    """initiate() creates an HDF5 file in data_directory."""
    procedure.initiate()
    procedure.standby()

    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    assert h5_files[0].stat().st_size > 0


def test_initiate_hdf5_metadata(procedure, tmp_path):
    """HDF5 file created by initiate() contains correct metadata."""
    procedure.initiate()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    with h5py.File(filepath, "r") as f:
        meta = f["metadata"].attrs
        assert meta["procedure_name"] == "Field Sweep IV"
        params = json.loads(meta["procedure_params"])
        assert params["field_steps"] == 3
        si = json.loads(meta["sample_info"])
        assert si["sample_name"] == "Test Sample"


# ── change_sweep_step() ───────────────────────────────────────────────────────

def test_change_sweep_step_returns_targets(procedure, tmp_path):
    """change_sweep_step() returns (system_targets, wait_time) for each step."""
    procedure.initiate()

    result = procedure.change_sweep_step()
    assert result is not None
    targets, wait = result
    assert "magnet_x" in targets
    assert targets["magnet_x"]["target"] == pytest.approx(0.0)
    assert wait == pytest.approx(0.0)

    procedure.standby()


def test_change_sweep_step_returns_none_at_end(procedure, tmp_path):
    """change_sweep_step() returns None after all steps are exhausted.

    With field_steps=3, sweep=[A, B, C]:
      initiate() → index=0, target=A
      change_sweep_step() → index=1, returns B
      change_sweep_step() → index=2, returns C
      change_sweep_step() → index=3 ≥ 3 → returns None
    """
    procedure.initiate()
    procedure.change_sweep_step()  # index 0→1
    procedure.change_sweep_step()  # index 1→2
    result = procedure.change_sweep_step()  # index 2→3 → None
    assert result is None

    procedure.standby()


def test_progress_updates(procedure, tmp_path):
    """get_progress() returns the correct fraction after each step."""
    procedure.initiate()
    assert procedure.get_progress() == pytest.approx(0.0)

    procedure.change_sweep_step()
    assert procedure.get_progress() == pytest.approx(1 / 3)

    procedure.change_sweep_step()
    assert procedure.get_progress() == pytest.approx(2 / 3)

    procedure.standby()


# ── measure() ─────────────────────────────────────────────────────────────────

def test_measure_without_initiate_raises(procedure):
    """measure() before initiate() raises RuntimeError."""
    with pytest.raises(RuntimeError):
        procedure.measure()


def test_measure_saves_datapoint(procedure, tmp_path):
    """measure() writes data to the HDF5 file at the correct sweep index."""
    procedure.initiate()
    # Configure the measurement VI (normally done via station.send_measurement_commands)
    procedure._station.keithley_delta_mode.configure(
        method="delta_mode", current=1e-6, n_readings=5
    )

    procedure.measure()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    with h5py.File(filepath, "r") as f:
        # Only 1 point was saved → standby() trims datasets to shape (1, 5)
        assert not np.isnan(f["data"]["field_T"][0])
        assert not np.any(np.isnan(f["data"]["voltage_V"][0]))
        assert f["data"]["voltage_V"].shape == (1, 5)


def test_measure_stores_snapshot(procedure, tmp_path):
    """measure() stores a JSON station snapshot."""
    procedure.initiate()
    procedure._station.keithley_delta_mode.configure(
        method="delta_mode", current=1e-6, n_readings=5
    )
    procedure.measure()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    with h5py.File(filepath, "r") as f:
        snap = json.loads(f["snapshots"]["0"][()])
        assert isinstance(snap, dict)
        assert len(snap) > 0


# ── standby() ─────────────────────────────────────────────────────────────────

def test_standby_returns_correct_structure(procedure, tmp_path):
    """standby() returns (system_targets, measurement_commands, 0.0)."""
    procedure.initiate()
    sys_targets, meas_cmds, wait = procedure.standby()

    assert "magnet_x" in sys_targets
    assert sys_targets["magnet_x"]["target"] == pytest.approx(0.0)
    assert "keithley_delta_mode" in meas_cmds
    assert wait == pytest.approx(0.0)


def test_standby_closes_data_file(procedure, tmp_path):
    """standby() closes the HDF5 file (DataManager is set to None)."""
    procedure.initiate()
    filepath = procedure._data_manager.filepath
    procedure.standby()

    assert procedure._data_manager is None
    # File should be valid and readable
    with h5py.File(filepath, "r") as f:
        assert f["metadata"].attrs["end_time"] != ""


def test_standby_double_call_safe(procedure, tmp_path):
    """standby() called twice does not raise."""
    procedure.initiate()
    procedure.standby()
    procedure.standby()  # Should not raise


# ── Full Orchestrator loop ────────────────────────────────────────────────────

def test_full_orchestrator_loop(station, tmp_path, qtbot):
    """FieldSweepIV runs through a full Orchestrator cycle without errors."""
    from cryosoft.core.orchestrator import Orchestrator, OrchestratorState

    # Fast ramp rates so the test completes quickly
    station.magnet_x._default_ramp_rate = 6000.0
    station.magnet_x._ramp_segments = []
    # temperature_vti starts at 300 K; target is also 300 K → instant settle

    procedure = FieldSweepIV(
        station=station,
        sample_info=SAMPLE_INFO,
        data_directory=str(tmp_path),
        **FAST_PARAMS,
    )

    orch = Orchestrator(station, tick_interval_ms=10)

    # Pre-configure keithley_delta_mode (normally done by orchestrator via station)
    # The Orchestrator calls station.send_measurement_commands from run_procedure,
    # which dispatches configure(). That happens inside run_procedure → initiate().
    orch.run_procedure(procedure)

    with qtbot.waitSignal(orch.procedure_finished, timeout=10000):
        pass

    # All 3 sweep points should have been measured.
    # _index is 3 after the final change_sweep_step() call returns None (0→1→2→3).
    assert procedure._index == 3
    assert orch._state == OrchestratorState.IDLE

    # HDF5 file must exist and be valid
    h5_files = list(tmp_path.glob("*.h5"))
    assert len(h5_files) == 1
    with h5py.File(h5_files[0], "r") as f:
        assert f["metadata"].attrs["procedure_name"] == "Field Sweep IV"
