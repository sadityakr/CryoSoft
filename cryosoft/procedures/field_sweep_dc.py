# ---
# description: |
#   FieldSweepDC procedure: sweeps the magnetic field from field_start to
#   field_end in field_steps steps, measuring DC resistance at each point via
#   DCMeasurementVI (Keithley 6221 + 2182A in simple DC mode).
# entry_point: Not run directly. Instantiated via GUI or tests.
# dependencies:
#   - numpy
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.core.data_manager (DataManager)
#   - Station must have: magnet_x (system VI), temperature_vti (system VI),
#     dc_measurement (measurement VI with initiate() and take_reading()).
# input: |
#   station, sample_info, data_directory, and keyword params matching the
#   parameters dict: field_start, field_end, field_steps, temperature,
#   current_A, compliance_A, voltmeter_range_V, readings_per_point,
#   init_wait, step_wait.
# process: |
#   initiate() ramps to first field and target temperature, arms dc_measurement.
#   change_sweep_step() steps through fields. measure() calls take_reading()
#   and saves via DataManager. standby() parks magnet at 0 T.
# output: |
#   HDF5 file with /data/field_T[N], /data/voltage_V[N,M],
#   /data/current_A[N,M], /data/timestamp[N], /snapshots/ and /metadata/.
# last_updated: 2026-04-18
# ---

"""FieldSweepDC — magnetic field sweep with DC resistance measurement."""

from __future__ import annotations

import logging

import numpy as np

from cryosoft.core.data_manager import DataManager
from cryosoft.core.procedure import BaseProcedure

logger = logging.getLogger(__name__)


class FieldSweepDC(BaseProcedure):
    """Sweep magnetic field and measure DC resistance at each point.

    Procedure flow:
    1. ``initiate()``: ramp magnet_x to first field, temperature_vti to target T,
       arm dc_measurement. Create DataManager.
    2. ``measure()``: call take_reading() on dc_measurement, snapshot station
       state, save via DataManager.
    3. ``change_sweep_step()``: step to next field. Return None when done.
    4. ``standby()``: close data file, park magnet at 0 T, disarm dc_measurement.

    Required VIs in Station:
        ``magnet_x`` (system), ``temperature_vti`` (system),
        ``dc_measurement`` (measurement).
    """

    name = "Field Sweep DC"
    description = "Sweep magnetic field, measure DC resistance at each point"
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
            "description": "Wait between field steps",
        },
    }

    measurement_parameters = {
        "current_A": {
            "type": float,
            "default": 1e-6,
            "unit": "A",
            "description": "DC source current",
        },
        "compliance_A": {
            "type": float,
            "default": 1e-3,
            "unit": "A",
            "description": "Current compliance on voltmeter",
        },
        "voltmeter_range_V": {
            "type": float,
            "default": 0.1,
            "unit": "V",
            "description": "Voltmeter full-scale range",
        },
        "readings_per_point": {
            "type": int,
            "default": 10,
            "min": 1,
            "description": "DC voltage readings averaged per field point",
        },
    }

    def _build_sweep_array(self) -> list:
        return np.linspace(
            self._params["field_start"],
            self._params["field_end"],
            int(self._params["field_steps"]),
        ).tolist()

    # ------------------------------------------------------------------
    # Four-method interface
    # ------------------------------------------------------------------

    def initiate(self) -> tuple[dict, dict, float]:
        """Ramp to initial field and temperature, arm dc_measurement.

        Returns:
            ``(system_targets, measurement_commands, init_wait)``
        """
        system_targets = {
            "magnet_x": {"target": self._sweep[0]},
            "temperature_vti": {"target": self._params["temperature"]},
        }

        measurement_commands = {
            "dc_measurement": {
                "initiate": {
                    "current_A": self._params["current_A"],
                    "compliance_A": self._params["compliance_A"],
                    "voltmeter_range_V": self._params["voltmeter_range_V"],
                }
            }
        }

        n = int(self._params["readings_per_point"])
        base_config: dict = {
            "sweep_columns": {"field_T": "float"},
            "measurement_arrays": {
                "voltage_V": n,
                "current_A": n,
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
            "FieldSweepDC.initiate(): %d steps from %.3f T to %.3f T, T=%.1f K",
            len(self._sweep),
            self._params["field_start"],
            self._params["field_end"],
            self._params["temperature"],
        )

        return system_targets, measurement_commands, float(self._params["init_wait"])

    def change_sweep_step(self) -> tuple[dict, float] | None:
        """Advance to the next field step.

        Returns:
            ``({"magnet_x": {"target": next_field}}, step_wait)`` or ``None``
            when all sweep points have been measured.
        """
        self._index += 1
        if self._index >= len(self._sweep):
            return None

        system_targets = {"magnet_x": {"target": self._sweep[self._index]}}
        return system_targets, float(self._params["step_wait"])

    def measure(self) -> None:
        """Take a DC reading, snapshot station state, save to HDF5.

        Raises:
            RuntimeError: If called before ``initiate()``.
        """
        if self._data_manager is None:
            raise RuntimeError("measure() called before initiate()")

        n = int(self._params["readings_per_point"])
        measured_data: dict = self._station.dc_measurement.take_reading(n_points=n)
        measured_data["field_T"] = self._station.magnet_x.get_field()
        self._save_datapoint(measured_data)

        logger.debug(
            "FieldSweepDC.measure(): index=%d, field=%.4f T",
            self._index,
            measured_data["field_T"],
        )

    def standby(self) -> tuple[dict, dict, float]:
        """Close data file, park magnet at 0 T, disarm dc_measurement.

        Returns:
            ``(system_targets, measurement_commands, 0.0)``
        """
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None

        system_targets = {"magnet_x": {"target": 0.0}}
        measurement_commands = {"dc_measurement": {"standby": {}}}
        return system_targets, measurement_commands, 0.0
