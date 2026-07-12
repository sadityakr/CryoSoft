# ---
# description: |
#   TemperatureSweepDC procedure: sweeps temperature (linear, piecewise-
#   segmented with a fine sub-range, or a custom CSV list — see sweep_axis),
#   measuring DC resistance at each stable temperature via DCMeasurementVI
#   (Keithley 6221 + 2182A). The ramp rate between steps is set per-sweep so
#   it can be changed without editing the YAML config.
# entry_point: Not run directly. Instantiated via GUI or tests.
# dependencies:
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.core.data_manager (DataManager)
#   - cryosoft.core.sweep_builder (SweepAxis) — sweep-shape construction is
#     handled entirely by BaseProcedure's default _build_sweep_array()
#   - Station must have: temperature_vti (system VI) and dc_measurement
#     (measurement VI with initiate() and take_reading()). magnet_x and
#     magnet_y are OPTIONAL: a missing magnet is skipped when its requested
#     field is 0, and refused at construction when the field is nonzero.
# input: |
#   station, sample_info, data_directory, and keyword params matching the
#   parameters dict: ramp_rate_K_per_min, field_x, field_y, current_A,
#   compliance_A, voltmeter_range_V, readings_per_point, point_wait, plus the
#   sweep_axis-generated temperature_mode/temperature_start/temperature_end/
#   temperature_steps/temperature_segments/temperature_csv_path/
#   temperature_hysteresis (see core.sweep_builder.sweep_axis_param_specs()).
# process: |
#   initiate() ramps temperature_vti to the first sweep point, magnet_x to
#   field_x, and magnet_y to field_y simultaneously, then arms dc_measurement.
#   change_sweep_step() ramps only temperature_vti to the next step.
#   measure() calls take_reading() and saves via DataManager.
#   standby() closes the data file; temperature holds at last set point.
# output: |
#   HDF5 file with /data/temperature_K[N], /data/voltage_V[N,M],
#   /data/current_A[N,M], /data/timestamp[N], /snapshots/ and /metadata/.
# last_updated: 2026-07-12
# ---

"""TemperatureSweepDC — temperature sweep with DC resistance measurement."""

from __future__ import annotations

import logging
from typing import Any

from cryosoft.core.data_manager import DataManager
from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.sweep_builder import SweepAxis

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
        ``magnet_x`` / ``magnet_y`` are optional — used when present; a
        nonzero field on a missing magnet is refused at construction.
    """

    name = "Temperature Sweep DC"
    description = "Sweep temperature, measure DC resistance at each stable point"
    sweep_axis = SweepAxis(
        key="temperature",
        unit="K",
        data_key="temperature_K",
        description="Sample temperature",
        default_start=10.0,
        default_end=300.0,
        default_steps=30,
    )
    sweep_data_keys = [sweep_axis.data_key]
    measurement_data_keys = ["voltage_V", "current_A"]
    default_x_key = sweep_axis.data_key

    system_parameters = {
        "field_x": {
            "type": float,
            "default": 0.0,
            "unit": "T",
            "description": "Applied field (magnet X, held constant)",
        },
        "field_y": {
            "type": float,
            "default": 0.0,
            "unit": "T",
            "description": "Applied field (magnet Y, held constant)",
        },
        "ramp_rate_K_per_min": {
            "type": float,
            "default": 2.0,
            "unit": "K/min",
            "description": "Temperature ramp rate between steps",
        },
        "point_wait": {
            "type": float,
            "default": 60.0,
            "unit": "s",
            "description": "Wait after reaching each temperature (thermal equilibration)",
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
            "description": "DC voltage readings per temperature point",
        },
    }

    # _build_sweep_array() is not overridden here: BaseProcedure's default
    # implementation delegates to sweep_builder.build_axis_sweep() using the
    # sweep_axis declared above (linear / segments / CSV, with hysteresis).

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Validate the magnet configuration against the station up front.

        The applied fields are optional: a station without ``magnet_x`` /
        ``magnet_y`` can still run this procedure at zero field. But a
        NONZERO field requested on a missing magnet is refused here, at
        construction — silently measuring at 0 T while the metadata claims
        0.5 T would corrupt a dataset without anyone noticing.
        """
        super().__init__(*args, **kwargs)
        self._magnet_targets: dict[str, dict] = {}
        for magnet, param in (("magnet_x", "field_x"), ("magnet_y", "field_y")):
            field = float(self._params[param])
            if self._station.has_vi(magnet):
                self._magnet_targets[magnet] = {"target": field}
            elif field != 0.0:
                raise CryoSoftConfigError(
                    f"{param}={field} T requested, but this station has no "
                    f"'{magnet}' VI. Set {param} to 0 or configure the magnet."
                )
            else:
                logger.info(
                    "TemperatureSweepDC: station has no '%s' — running without it "
                    "(%s=0).", magnet, param,
                )

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
        system_targets = {
            "temperature_vti": self._temp_target(0),
            **self._magnet_targets,  # only magnets the station actually has
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
            "sweep_columns": {type(self).sweep_axis.data_key: "float"},
            "measurement_arrays": {
                "voltage_V": n,
                "current_A": n,
            },
        }

        self._data_manager = DataManager(
            data_directory=self._data_directory,
            procedure_name=self.name,
            file_prefix=self._file_prefix,
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
            "at %.2f K/min, Bx=%.3f T, By=%.3f T",
            len(self._sweep),
            self._sweep[0],
            self._sweep[-1],
            self._params["ramp_rate_K_per_min"],
            self._params["field_x"],
            self._params["field_y"],
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
        measured_data[type(self).sweep_axis.data_key] = self._station.temperature_vti.temperature()
        self._save_datapoint(measured_data)

        logger.debug(
            "TemperatureSweepDC.measure(): index=%d, T=%.3f K",
            self._index,
            measured_data[type(self).sweep_axis.data_key],
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

    def abort(self) -> dict:
        """Close the data file and zero the DC source (no ramping).

        Returns:
            Measurement safe-off commands for the Orchestrator to dispatch.
        """
        super().abort()
        return {"dc_measurement": {"standby": {}}}
