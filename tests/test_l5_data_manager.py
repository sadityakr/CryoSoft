# ---
# description: |
#   Unit tests for Layer 5 (DataManager). Verifies HDF5 file creation,
#   metadata storage, dataset pre-allocation, save_datapoint(), snapshot
#   storage, close() with end_time and early-abort trimming.
# last_updated: 2026-04-06
# ---

import json

import h5py
import numpy as np
import pytest

from cryosoft.core.data_manager import DataManager


# ── Shared fixtures ───────────────────────────────────────────────────────────

DATA_CONFIG = {
    "sweep_columns": {"field_T": "float"},
    "measurement_arrays": {
        "voltage_V": 10,
        "current_A": 10,
    },
}

SAMPLE_INFO = {"sample_name": "Test Sample", "sample_id": "TST-001", "comments": "ci run"}

PROCEDURE_PARAMS = {
    "field_start": -1.0,
    "field_end": 1.0,
    "field_steps": 5,
    "temperature": 10.0,
}


@pytest.fixture
def dm(tmp_path):
    """A fresh DataManager writing to a temp directory."""
    manager = DataManager(
        data_directory=str(tmp_path),
        procedure_name="Test_Sweep",
        procedure_params=PROCEDURE_PARAMS,
        sample_info=SAMPLE_INFO,
        instrument_state={"magnet_x": {"field": 0.0}},
        system_targets={"magnet_x": {"target": -1.0}},
        measurement_commands={"keithley_delta_mode": {"configure": {}}},
        data_config=DATA_CONFIG,
        n_sweep_points=5,
    )
    yield manager
    # Ensure file is closed even if a test fails mid-way.
    if not manager._closed:
        manager.close()


@pytest.fixture
def saved_dm(dm):
    """DataManager with 3 of 5 points saved."""
    for i in range(3):
        dm.save_datapoint(
            sweep_index=i,
            measured_data={
                "field_T": float(i) * 0.5,
                "voltage_V": [float(j) * 1e-6 for j in range(10)],
                "current_A": [1e-6] * 10,
            },
            station_snapshot={"magnet_x": {"field": float(i) * 0.5}},
        )
    return dm


# ── File creation ─────────────────────────────────────────────────────────────

def test_file_created(tmp_path):
    """DataManager creates an HDF5 file at the expected path."""
    dm = DataManager(
        data_directory=str(tmp_path),
        procedure_name="MySweep",
        procedure_params={},
        sample_info=SAMPLE_INFO,
        instrument_state={},
        system_targets={},
        measurement_commands={},
        data_config=DATA_CONFIG,
        n_sweep_points=3,
    )
    assert dm.filepath.exists()
    assert dm.filepath.suffix == ".h5"
    assert "MySweep" in dm.filepath.name
    dm.close()


def test_invalid_n_sweep_points(tmp_path):
    """n_sweep_points < 1 raises ValueError."""
    with pytest.raises(ValueError):
        DataManager(
            data_directory=str(tmp_path),
            procedure_name="Bad",
            procedure_params={},
            sample_info={},
            instrument_state={},
            system_targets={},
            measurement_commands={},
            data_config=DATA_CONFIG,
            n_sweep_points=0,
        )


# ── Metadata ──────────────────────────────────────────────────────────────────

def test_metadata_attributes(dm):
    """All metadata attributes are stored correctly as JSON strings."""
    with h5py.File(dm.filepath, "r") as f:
        meta = f["metadata"].attrs
        assert meta["procedure_name"] == "Test_Sweep"

        params = json.loads(meta["procedure_params"])
        assert params["field_start"] == -1.0

        si = json.loads(meta["sample_info"])
        assert si["sample_name"] == "Test Sample"

        assert meta["start_time"] != ""
        assert meta["end_time"] == ""  # Not yet closed

        dc = json.loads(meta["data_config"])
        assert "sweep_columns" in dc
        assert "measurement_arrays" in dc


def test_end_time_written_on_close(dm):
    """close() writes a non-empty end_time."""
    dm.close()
    with h5py.File(dm.filepath, "r") as f:
        assert f["metadata"].attrs["end_time"] != ""


# ── Pre-allocation ────────────────────────────────────────────────────────────

def test_dataset_shapes(dm):
    """Datasets are pre-allocated with correct shapes."""
    with h5py.File(dm.filepath, "r") as f:
        assert f["data"]["field_T"].shape == (5,)
        assert f["data"]["voltage_V"].shape == (5, 10)
        assert f["data"]["current_A"].shape == (5, 10)
        assert f["data"]["timestamp"].shape == (5,)


def test_initial_fill_is_nan(dm):
    """Numeric datasets are pre-filled with NaN."""
    with h5py.File(dm.filepath, "r") as f:
        assert np.all(np.isnan(f["data"]["field_T"][:]))
        assert np.all(np.isnan(f["data"]["voltage_V"][:]))


# ── save_datapoint ────────────────────────────────────────────────────────────

def test_save_single_datapoint(dm):
    """save_datapoint() writes correct values at a given index."""
    voltages = [float(i) * 1e-6 for i in range(10)]
    dm.save_datapoint(
        sweep_index=2,
        measured_data={
            "field_T": 0.5,
            "voltage_V": voltages,
            "current_A": [1e-6] * 10,
        },
        station_snapshot={"magnet_x": {"field": 0.5}},
    )
    with h5py.File(dm.filepath, "r") as f:
        assert f["data"]["field_T"][2] == pytest.approx(0.5)
        assert list(f["data"]["voltage_V"][2]) == pytest.approx(voltages)
        assert f["data"]["timestamp"][2] != ""


def test_save_multiple_datapoints(saved_dm):
    """Multiple save_datapoint() calls write at the correct indices."""
    with h5py.File(saved_dm.filepath, "r") as f:
        assert f["data"]["field_T"][0] == pytest.approx(0.0)
        assert f["data"]["field_T"][1] == pytest.approx(0.5)
        assert f["data"]["field_T"][2] == pytest.approx(1.0)
        # Indices 3 and 4 should still be NaN (not yet saved)
        assert np.isnan(f["data"]["field_T"][3])
        assert np.isnan(f["data"]["field_T"][4])


def test_save_out_of_range_raises(dm):
    """save_datapoint() with index >= n_sweep_points raises IndexError."""
    with pytest.raises(IndexError):
        dm.save_datapoint(10, {"field_T": 1.0}, {})


def test_save_on_closed_raises(dm):
    """save_datapoint() after close() raises RuntimeError."""
    dm.close()
    with pytest.raises(RuntimeError):
        dm.save_datapoint(0, {"field_T": 1.0}, {})


def test_unknown_column_ignored(dm):
    """save_datapoint() with an unknown column logs a warning and doesn't crash."""
    dm.save_datapoint(
        sweep_index=0,
        measured_data={"field_T": 0.0, "unknown_key": 42.0},
        station_snapshot={},
    )
    # field_T should still be written
    with h5py.File(dm.filepath, "r") as f:
        assert f["data"]["field_T"][0] == pytest.approx(0.0)


# ── Snapshots ─────────────────────────────────────────────────────────────────

def test_snapshots_stored_as_json(saved_dm):
    """Snapshots for saved indices exist and are valid JSON."""
    saved_dm.close()
    with h5py.File(saved_dm.filepath, "r") as f:
        for i in range(3):
            raw = f["snapshots"][str(i)][()]
            snap = json.loads(raw)
            assert "magnet_x" in snap


def test_no_snapshot_for_unsaved_index(saved_dm):
    """Snapshot group only has datasets for saved indices."""
    saved_dm.close()
    with h5py.File(saved_dm.filepath, "r") as f:
        assert "3" not in f["snapshots"]
        assert "4" not in f["snapshots"]


# ── close() and trim on abort ─────────────────────────────────────────────────

def test_close_full_sweep(dm):
    """Full sweep: no trimming, shapes unchanged."""
    for i in range(5):
        dm.save_datapoint(i, {"field_T": float(i)}, {"state": i})
    dm.close()
    with h5py.File(dm.filepath, "r") as f:
        assert f["data"]["field_T"].shape == (5,)


def test_close_trims_on_abort(saved_dm):
    """Partial sweep: close() trims datasets to actual saved points (3 of 5)."""
    saved_dm.close()
    with h5py.File(saved_dm.filepath, "r") as f:
        assert f["data"]["field_T"].shape == (3,)
        assert f["data"]["voltage_V"].shape == (3, 10)
        assert f["data"]["timestamp"].shape == (3,)


def test_double_close_is_safe(dm):
    """close() called twice doesn't raise."""
    dm.close()
    dm.close()  # Should not raise


def test_file_readable_after_close(saved_dm):
    """Closed file can be reopened and data is intact."""
    saved_dm.close()
    with h5py.File(saved_dm.filepath, "r") as f:
        assert f["data"]["field_T"][0] == pytest.approx(0.0)
        assert f["data"]["field_T"][1] == pytest.approx(0.5)
        assert f["data"]["field_T"][2] == pytest.approx(1.0)
        assert json.loads(f["metadata"].attrs["procedure_params"])["field_start"] == -1.0
