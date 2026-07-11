# ---
# description: |
#   FieldSweepIV procedure: sweeps the magnetic field from field_start to
#   field_end in field_steps steps, measuring IV (delta-mode) at each point.
#   Stores data and station snapshots in an HDF5 file via DataManager. This
#   is the CryoSoft port of the old field_voltage_logic.py script (Mercury
#   iPS-M + Keithley 6221/2182A delta mode).
# entry_point: Not run directly. Instantiated via GUI or tests.
# dependencies:
#   - numpy
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.core.data_manager (DataManager)
#   - cryosoft.core.sweep_builder (SweepSegment, build_piecewise_sweep,
#     load_custom_sweep_csv, apply_hysteresis) — optional sweep-shape overrides
#   - Station must have: magnet_x (system VI), temperature_vti (system VI),
#     keithley_delta_mode (measurement VI with configure() and read_datapoint()).
# input: |
#   station, sample_info, data_directory, and keyword params matching the
#   parameters dict: field_start, field_end, field_steps, temperature,
#   current, n_readings, init_wait, step_wait. A caller that instantiates
#   this procedure directly (not through the GUI form) may additionally pass
#   sweep_segments, sweep_csv_path, and/or sweep_hysteresis to change the
#   sweep shape — see _build_sweep_array().
# process: |
#   initiate() ramps to first field point + target temperature, configures
#   delta-mode. change_sweep_step() steps through fields. measure() reads
#   IV data and snapshots state. standby() parks magnet at 0 T.
# output: |
#   HDF5 file with /data/field_T[N], /data/voltage_V[N,M],
#   /data/current_A[N,M], /data/timestamp[N], /snapshots/ and /metadata/.
# last_updated: 2026-07-11
# ---

"""FieldSweepIV — magnetic field sweep with IV delta-mode measurement."""

from __future__ import annotations

import logging

import numpy as np

from cryosoft.core.data_manager import DataManager
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.sweep_builder import (
    SweepSegment,
    apply_hysteresis,
    build_piecewise_sweep,
    load_custom_sweep_csv,
)

logger = logging.getLogger(__name__)


class FieldSweepIV(BaseProcedure):
    """Sweep magnetic field and measure IV at each point.

    Procedure flow:
    1. ``initiate()``: ramp magnet_x to first field, temperature_vti to target T,
       configure delta-mode on keithley_delta_mode. Create DataManager.
    2. ``measure()``: read IV data from keithley_delta_mode, snapshot station state,
       save via DataManager.
    3. ``change_sweep_step()``: step to next field. Return None when done.
    4. ``standby()``: close data file, park magnet at 0 T.

    Required VIs in Station:
        ``magnet_x`` (system), ``temperature_vti`` (system),
        ``keithley_delta_mode`` (measurement).
    """

    name = "Field Sweep IV"
    description = "Sweep magnetic field, measure IV at each point"
    sweep_data_keys = ["field_T"]
    measurement_data_keys = ["voltage_V", "current_A"]
    default_x_key = "field_T"

    sweep_parameters = {
        "field_start": {
            "type": float,
            "default": -1.0,
            "unit": "T",
            "description": "Starting field",
        },
        "field_end": {
            "type": float,
            "default": 1.0,
            "unit": "T",
            "description": "Ending field",
        },
        "field_steps": {
            "type": int,
            "default": 101,
            "min": 2,
            "description": "Number of field steps",
        },
    }

    system_parameters = {
        "temperature": {
            "type": float,
            "default": 10.0,
            "unit": "K",
            "description": "Sample temperature",
        },
        "init_wait": {
            "type": float,
            "default": 300.0,
            "unit": "s",
            "description": "Wait after initial ramp (thermal equilibration)",
        },
        "step_wait": {
            "type": float,
            "default": 5.0,
            "unit": "s",
            "description": "Wait between sweep points",
        },
    }

    measurement_parameters = {
        "current": {
            "type": float,
            "default": 1e-6,
            "unit": "A",
            "description": "Measurement current",
        },
        "n_readings": {
            "type": int,
            "default": 100,
            "min": 1,
            "description": "Readings per point (delta mode)",
        },
    }

    # ------------------------------------------------------------------
    # Sweep array
    # ------------------------------------------------------------------

    def _build_sweep_array(self) -> list:
        """Build the field sweep array.

        Default (GUI-driven) behavior is unchanged: a linear sweep from
        field_start to field_end in field_steps points. A caller that
        instantiates this procedure directly — a test, a script, or a future
        GUI extension — can override the sweep shape with extra keyword
        arguments that are *not* part of ``sweep_parameters`` and therefore
        never appear in the GUI form:

        - ``sweep_csv_path`` (str): path to a single-column CSV of field
          values in tesla, for an arbitrary non-uniform field list. Highest
          precedence.
        - ``sweep_segments`` (list of ``SweepSegment`` or ``{"start", "end",
          "step"}`` dicts): a piecewise sweep with a different step size per
          sub-range (e.g. coarse steps outside a region, fine steps inside
          it). Used if ``sweep_csv_path`` is not given.
        - ``sweep_hysteresis`` (bool): if True, extend whichever array was
          built above into a forward+backward loop.

        Returns:
            List of field values in tesla.
        """
        if "sweep_csv_path" in self._params:
            base = load_custom_sweep_csv(self._params["sweep_csv_path"])
        elif "sweep_segments" in self._params:
            segments = [
                seg if isinstance(seg, SweepSegment) else SweepSegment(**seg)
                for seg in self._params["sweep_segments"]
            ]
            # Normalize to plain dicts: self._params is JSON-serialized into
            # the HDF5 metadata by DataManager, and SweepSegment (a dataclass
            # instance) is not JSON-serializable.
            self._params["sweep_segments"] = [
                {"start": s.start, "end": s.end, "step": s.step} for s in segments
            ]
            base = build_piecewise_sweep(segments)
        else:
            base = np.linspace(
                self._params["field_start"],
                self._params["field_end"],
                int(self._params["field_steps"]),
            ).tolist()

        if self._params.get("sweep_hysteresis", False):
            base = apply_hysteresis(base)

        return base

    # ------------------------------------------------------------------
    # Four-method interface
    # ------------------------------------------------------------------

    def initiate(self) -> tuple[dict, dict, float]:
        """Set initial field + temperature targets, configure measurement VI.

        Creates the DataManager and HDF5 file for this run.

        Returns:
            ``(system_targets, measurement_commands, init_wait)``
        """
        n_readings = int(self._params["n_readings"])

        system_targets = {
            # persistent=False: keep the switch heater energised across the
            # whole sweep (paid once here) instead of re-heating/re-cooling
            # it on every sweep point. See standby() for the final park.
            "magnet_x": {"target": self._sweep[0], "persistent": False},
            "temperature_vti": {"target": self._params["temperature"]},
        }

        measurement_commands = {
            "keithley_delta_mode": {
                "configure": {
                    "method": "delta_mode",
                    "current": self._params["current"],
                    "n_readings": n_readings,
                }
            }
        }

        base_config: dict = {
            "sweep_columns": {"field_T": "float"},
            "measurement_arrays": {
                "voltage_V": n_readings,
                "current_A": n_readings,
            },
        }

        self._data_manager = DataManager(
            data_directory=self._data_directory,
            procedure_name=self.name,
            procedure_params=self._params,
            sample_info=self._sample_info,
            instrument_state=self._station.get_state(),
            system_targets=system_targets,
            measurement_commands=measurement_commands,
            data_config=self._build_data_config(base_config),
            n_sweep_points=len(self._sweep),
        )

        logger.info(
            "FieldSweepIV.initiate(): %d steps from %.3f T to %.3f T, T=%.1f K",
            len(self._sweep),
            self._params["field_start"],
            self._params["field_end"],
            self._params["temperature"],
        )

        return system_targets, measurement_commands, float(self._params["init_wait"])

    def change_sweep_step(self) -> tuple[dict, float] | None:
        """Advance to the next field step.

        Returns:
            ``({"magnet_x": {"target": next_field, "persistent": False}},
            step_wait)`` or ``None`` when all sweep points have been measured.
        """
        self._index += 1
        if self._index >= len(self._sweep):
            return None

        system_targets = {
            "magnet_x": {"target": self._sweep[self._index], "persistent": False},
        }
        return system_targets, float(self._params["step_wait"])

    def measure(self) -> None:
        """Read IV data, snapshot station state, save to HDF5.

        Reads from ``keithley_delta_mode.read_datapoint()`` (must have been
        configured via ``initiate()``). Also reads the current field from
        ``magnet_x.get_field()`` and saves it as the sweep column.
        """
        if self._data_manager is None:
            raise RuntimeError("measure() called before initiate()")

        measured_data: dict = self._station.keithley_delta_mode.read_datapoint()
        measured_data["field_T"] = self._station.magnet_x.get_field()
        self._save_datapoint(measured_data)

        logger.debug(
            "FieldSweepIV.measure(): index=%d, field=%.4f T",
            self._index,
            measured_data["field_T"],
        )

    def standby(self) -> tuple[dict, dict, float]:
        """Close data file and park magnet at 0 T.

        No ``"persistent"`` key here, so this ramp uses the default
        (``persistent=True``): switch heater cools and the PSU parks at zero
        once the field reaches 0 T, matching the old script's end-of-run
        cleanup (zero field, then switch heater off).

        Returns:
            ``(system_targets, measurement_commands, 0.0)``
        """
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None

        system_targets = {
            "magnet_x": {"target": 0.0},
        }
        measurement_commands = {
            "keithley_delta_mode": {"standby": {}},
        }
        return system_targets, measurement_commands, 0.0
