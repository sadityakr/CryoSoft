# ---
# description: |
#   Implements the DataManager class, which handles HDF5 file creation,
#   metadata storage, pre-allocated dataset management, and per-point data
#   saving for CryoSoft measurement procedures.
# entry_point: imported by cryosoft.core; not run directly
# dependencies:
#   - h5py >= 3.9
#   - numpy >= 1.24
# input: |
#   Constructor receives procedure name, an optional user filename prefix,
#   parameter dicts (procedure_params, sample_info, instrument_state,
#   system_targets, measurement_commands, data_config), target data
#   directory, and n_sweep_points (int). data_config defines sweep_columns
#   (1-D) and measurement_arrays (2-D).
# process: |
#   Creates an HDF5 file at {data_directory}/{stem}_{YYYYMMDD_HHMMSS}.h5,
#   where stem is file_prefix if given, else procedure_name; writes all
#   metadata as JSON-encoded attributes, pre-allocates resizable datasets,
#   and exposes save_datapoint() and close() for the procedure loop.
# output: |
#   A single HDF5 file written to disk.  Datasets are trimmed to the number of
#   actually-saved points when close() is called after an early abort.
# ---

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import h5py
import numpy as np

logger = logging.getLogger(__name__)

_SENTINEL = -1  # used when no point has been saved yet


class DataManager:
    """Create and manage an HDF5 measurement file for one procedure run.

    Lifecycle
    ---------
    1. Instantiate at ``Procedure.initiate()``.
    2. Call ``save_datapoint()`` once per sweep step inside the measurement
       loop.
    3. Call ``close()`` at ``Procedure.standby()`` (or on abort).  The file
       is trimmed to the number of points actually saved.
    """

    # ------------------------------------------------------------------ #
    #  Construction                                                        #
    # ------------------------------------------------------------------ #

    def __init__(
        self,
        data_directory: str,
        procedure_name: str,
        procedure_params: dict,
        sample_info: dict,
        instrument_state: dict,
        system_targets: dict,
        measurement_commands: dict | list,
        data_config: dict,
        n_sweep_points: int,
        file_prefix: str = "",
    ) -> None:
        """Create the HDF5 file and write all metadata.

        Parameters
        ----------
        data_directory:
            Root directory where the HDF5 file will be written.
        procedure_name:
            Short name stored as metadata (``/metadata/procedure_name``). Also
            used as the filename prefix when ``file_prefix`` is empty.
        file_prefix:
            User-chosen filename prefix. When non-empty, the file is named
            ``{file_prefix}_{timestamp}.h5`` instead of
            ``{procedure_name}_{timestamp}.h5``. Metadata still records the
            true ``procedure_name`` regardless.
        procedure_params:
            Arbitrary procedure parameters (JSON-serialisable dict).
        sample_info:
            Sample description dict (JSON-serialisable).
        instrument_state:
            Snapshot of instrument state at initiation (JSON-serialisable).
        system_targets:
            Physical targets (field, temperature …) for this run.
        measurement_commands:
            JSON-serialisable description of the measurement commands used.
            Since the typed-plan cutover this is an ordered list of Command
            dicts (``[{"vi_name": ..., "method": ..., "kwargs": {...}}, ...]``);
            a plain dict is still accepted (older files / direct callers).
        data_config:
            Specifies datasets.  Expected format::

                {
                    "sweep_columns":      {"field_T": "float", ...},
                    "measurement_arrays": {"voltage_V": 100, "current_A": 100, ...},
                }

        n_sweep_points:
            Total number of sweep points expected (used for pre-allocation).
        """
        if n_sweep_points < 1:
            raise ValueError(f"n_sweep_points must be >= 1, got {n_sweep_points}")

        self._procedure_name = procedure_name
        self._n_sweep_points = n_sweep_points
        self._data_config = data_config
        self._last_saved_index: int = _SENTINEL
        self._closed = False
        self.last_datapoint: dict = {}  # updated by save_datapoint(); read by GUI for live plot

        # Derive column / array names from data_config
        self._sweep_columns: dict[str, str] = data_config.get("sweep_columns", {})
        self._measurement_arrays: dict[str, int] = data_config.get(
            "measurement_arrays", {}
        )

        # Build file path
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        data_dir = Path(data_directory)
        data_dir.mkdir(parents=True, exist_ok=True)
        stem = file_prefix.strip() or procedure_name
        self._filepath = data_dir / f"{stem}_{timestamp_str}.h5"

        logger.info("DataManager: creating HDF5 file at %s", self._filepath)

        self._file = h5py.File(self._filepath, "w")

        # Write metadata
        self._write_metadata(
            procedure_params=procedure_params,
            sample_info=sample_info,
            instrument_state=instrument_state,
            system_targets=system_targets,
            measurement_commands=measurement_commands,
            data_config=data_config,
        )

        # Pre-allocate datasets
        self._allocate_datasets()

        self._file.flush()
        logger.debug("DataManager: initialisation complete (%d points)", n_sweep_points)

    # ------------------------------------------------------------------ #
    #  Private helpers                                                     #
    # ------------------------------------------------------------------ #

    def _write_metadata(
        self,
        procedure_params: dict,
        sample_info: dict,
        instrument_state: dict,
        system_targets: dict,
        measurement_commands: dict | list,
        data_config: dict,
    ) -> None:
        """Write all metadata to `/metadata/` as JSON-encoded HDF5 attributes."""
        meta = self._file.require_group("metadata")
        meta.attrs["procedure_name"] = self._procedure_name
        meta.attrs["procedure_params"] = json.dumps(procedure_params)
        meta.attrs["sample_info"] = json.dumps(sample_info)
        meta.attrs["start_time"] = datetime.now(timezone.utc).isoformat()
        meta.attrs["end_time"] = ""  # filled in at close()
        meta.attrs["instrument_state"] = json.dumps(instrument_state)
        meta.attrs["system_targets"] = json.dumps(system_targets)
        meta.attrs["measurement_commands"] = json.dumps(measurement_commands)
        meta.attrs["data_config"] = json.dumps(data_config)

    def _allocate_datasets(self) -> None:
        """Pre-allocate all datasets in `/data/` with NaN fill values."""
        N = self._n_sweep_points
        data_group = self._file.require_group("data")

        # 1-D sweep columns
        for col_name in self._sweep_columns:
            data_group.create_dataset(
                col_name,
                shape=(N,),
                maxshape=(None,),
                dtype=np.float64,
                fillvalue=np.nan,
            )

        # 2-D measurement arrays
        for arr_name, M in self._measurement_arrays.items():
            data_group.create_dataset(
                arr_name,
                shape=(N, M),
                maxshape=(None, M),
                dtype=np.float64,
                fillvalue=np.nan,
            )

        # Timestamp column (variable-length strings)
        dt = h5py.string_dtype()
        data_group.create_dataset(
            "timestamp",
            shape=(N,),
            maxshape=(None,),
            dtype=dt,
            fillvalue="",
        )

        # Snapshots group (datasets created on demand)
        self._file.require_group("snapshots")

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    @property
    def filepath(self) -> Path:
        """Path to the HDF5 file on disk."""
        return self._filepath

    def save_datapoint(
        self,
        sweep_index: int,
        measured_data: dict,
        station_snapshot: dict,
    ) -> None:
        """Save one sweep point's data and a full station snapshot.

        Parameters
        ----------
        sweep_index:
            Zero-based index of this sweep point (0 … n_sweep_points-1).
        measured_data:
            Dict whose keys match sweep_columns or measurement_arrays names.
            Values may be scalars, lists, or numpy arrays.
        station_snapshot:
            Full instrument-state snapshot to store as a JSON string.
        """
        if self._closed:
            raise RuntimeError("save_datapoint() called on a closed DataManager")
        if not (0 <= sweep_index < self._n_sweep_points):
            raise IndexError(
                f"sweep_index {sweep_index} out of range [0, {self._n_sweep_points})"
            )

        data_group = self._file["data"]

        for col_name, value in measured_data.items():
            if col_name in self._sweep_columns:
                data_group[col_name][sweep_index] = float(value)
            elif col_name in self._measurement_arrays:
                arr = np.asarray(value, dtype=np.float64)
                # Instruments can legitimately return fewer readings than
                # allocated (e.g. the delta engine aborts an acquisition
                # early). Pad with NaN / truncate to the allocated width —
                # a raw shape-mismatch here would crash the whole run.
                expected = int(self._measurement_arrays[col_name])
                if arr.shape != (expected,):
                    logger.warning(
                        "DataManager: column '%s' at index %d has %d values "
                        "(expected %d) — padding/truncating with NaN",
                        col_name, sweep_index, arr.size, expected,
                    )
                    padded = np.full(expected, np.nan)
                    n = min(arr.size, expected)
                    padded[:n] = arr.ravel()[:n]
                    arr = padded
                data_group[col_name][sweep_index, :] = arr
            else:
                logger.warning(
                    "DataManager: unknown column '%s' — skipped", col_name
                )

        # Timestamp
        data_group["timestamp"][sweep_index] = datetime.now(timezone.utc).isoformat()

        # Snapshot as variable-length string dataset
        snap_json = json.dumps(station_snapshot)
        self._file["snapshots"].create_dataset(
            str(sweep_index), data=snap_json
        )

        self._last_saved_index = sweep_index
        self.last_datapoint: dict = measured_data
        self._file.flush()

    def close(self) -> None:
        """Close the HDF5 file, record end_time, and trim on early abort."""
        if self._closed:
            logger.warning("DataManager.close() called on an already-closed file")
            return

        self._file["metadata"].attrs["end_time"] = datetime.now(
            timezone.utc
        ).isoformat()

        # Trim datasets to actual points saved (handles early abort)
        actual_points = self._last_saved_index + 1  # 0 if nothing saved

        if 0 < actual_points < self._n_sweep_points:
            logger.info(
                "DataManager: trimming datasets from %d to %d points",
                self._n_sweep_points,
                actual_points,
            )
            data_group = self._file["data"]
            for name in data_group:
                ds = data_group[name]
                new_shape = (actual_points,) + ds.shape[1:]
                ds.resize(new_shape)

        self._file.flush()
        self._file.close()
        self._closed = True
        logger.info("DataManager: closed %s", self._filepath)
