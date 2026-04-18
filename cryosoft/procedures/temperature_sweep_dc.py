# ---
# description: |
#   TemperatureSweepDC procedure: sweeps temperature from temp_start to temp_end
#   through n_points steps, measuring DC resistance at each stable temperature via
#   DCMeasurementVI (Keithley 6221 + 2182A). The ramp rate between steps is set
#   per-sweep so it can be changed without editing the YAML config.
# entry_point: Not run directly. Instantiated via GUI or tests.
# dependencies:
#   - numpy
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.core.data_manager (DataManager)
#   - Station must have: temperature_vti (system VI),
#     dc_measurement (measurement VI with initiate() and take_reading()).
# input: |
#   station, sample_info, data_directory, and keyword params matching the
#   parameters dict: temp_start, temp_end, n_points, ramp_rate_K_per_min,
#   current_A, compliance_A, voltmeter_range_V, readings_per_point, point_wait.
# process: |
#   initiate() ramps to temp_start at ramp_rate_K_per_min, arms dc_measurement.
#   change_sweep_step() ramps to the next temperature at ramp_rate_K_per_min.
#   measure() calls take_reading() and saves via DataManager.
#   standby() closes the data file; temperature holds at last set point.
# output: |
#   HDF5 file with /data/temperature_K[N], /data/voltage_V[N,M],
#   /data/current_A[N,M], /data/timestamp[N], /snapshots/ and /metadata/.
# last_updated: 2026-04-18
# ---

"""TemperatureSweepDC — temperature sweep with DC resistance measurement."""

from __future__ import annotations

import logging

import numpy as np

from cryosoft.core.data_manager import DataManager
from cryosoft.core.procedure import BaseProcedure

logger = logging.getLogger(__name__)


class TemperatureSweepDC(BaseProcedure):
    """Sweep temperature and measure DC resistance at each stable point.

    Procedure flow:
    1. ``initiate()``: ramp temperature_vti to temp_start at ramp_rate_K_per_min,
       arm dc_measurement. Create DataManager.
    2. ``measure()``: call take_reading() on dc_measurement, snapshot station
       state, save via DataManager.
    3. ``change_sweep_step()``: ramp to next temperature at ramp_rate_K_per_min.
       Return None when all points measured.
    4. ``standby()``: close data file. Temperature holds at last point.

    The ramp rate is passed per-step via system_targets so it takes effect
    immediately at each step without requiring a YAML config change. It is also
    exposed on the temperature VI as a ``@control`` for GUI use.

    Required VIs in Station:
        ``temperature_vti`` (system), ``dc_measurement`` (measurement).
    """

    name = "Temperature Sweep DC"
    description = "Sweep temperature, measure DC resistance at each stable point"
    sweep_data_keys = ["temperature_K"]
    measurement_data_keys = ["voltage_V", "current_A"]
    default_x_key = "temperature_K"

    parameters = {
        "temp_start": {
            "type": float,
            "default": 10.0,
            "unit": "K",
            "description": "Starting temperature",
        },
        "temp_end": {
            "type": float,
            "default": 300.0,
            "unit": "K",
            "description": "Final temperature",
        },
        "n_points": {
            "type": int,
            "default": 30,
            "min": 2,
            "description": "Number of temperature steps",
        },
        "ramp_rate_K_per_min": {
            "type": float,
            "default": 2.0,
            "unit": "K/min",
            "description": "Temperature ramp rate between steps",
        },
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
            "description": "DC voltage readings per temperature point",
        },
        "point_wait": {
            "type": float,
            "default": 60.0,
            "unit": "s",
            "description": "Wait after reaching each temperature (thermal equilibration)",
        },
    }

    def _build_sweep_array(self) -> list:
        return np.linspace(
            self._params["temp_start"],
            self._params["temp_end"],
            int(self._params["n_points"]),
        ).tolist()

    def _temp_target(self, index: int) -> dict:
        """Build a system_targets entry for temperature_vti at *index*."""
        return {
            "target": self._sweep[index],
            "rate": self._params["ramp_rate_K_per_min"],
        }

    # ------------------------------------------------------------------
    # Four-method interface
    # ------------------------------------------------------------------

    def initiate(self) -> tuple[dict, dict, float]:
        """Ramp to initial temperature, arm dc_measurement.

        Returns:
            ``(system_targets, measurement_commands, point_wait)``
        """
        system_targets = {"temperature_vti": self._temp_target(0)}

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
            "sweep_columns": {"temperature_K": "float"},
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
            "TemperatureSweepDC.initiate(): %d steps from %.1f K to %.1f K "
            "at %.2f K/min",
            len(self._sweep),
            self._params["temp_start"],
            self._params["temp_end"],
            self._params["ramp_rate_K_per_min"],
        )

        return system_targets, measurement_commands, float(self._params["point_wait"])

    def change_sweep_step(self) -> tuple[dict, float] | None:
        """Ramp to the next temperature step.

        Returns:
            ``({"temperature_vti": {"target": T, "rate": R}}, point_wait)``
            or ``None`` when all points have been measured.
        """
        self._index += 1
        if self._index >= len(self._sweep):
            return None

        system_targets = {"temperature_vti": self._temp_target(self._index)}
        return system_targets, float(self._params["point_wait"])

    def measure(self) -> None:
        """Take a DC reading at the current temperature, save to HDF5.

        Raises:
            RuntimeError: If called before ``initiate()``.
        """
        if self._data_manager is None:
            raise RuntimeError("measure() called before initiate()")

        n = int(self._params["readings_per_point"])
        measured_data: dict = self._station.dc_measurement.take_reading(n_points=n)
        measured_data["temperature_K"] = self._station.temperature_vti.temperature()
        self._save_datapoint(measured_data)

        logger.debug(
            "TemperatureSweepDC.measure(): index=%d, T=%.3f K",
            self._index,
            measured_data["temperature_K"],
        )

    def standby(self) -> tuple[dict, dict, float]:
        """Close data file. Temperature holds at last set point.

        Returns:
            ``(system_targets, measurement_commands, 0.0)``
        """
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None

        measurement_commands = {"dc_measurement": {"standby": {}}}
        return {}, measurement_commands, 0.0
