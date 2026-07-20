# ---
# description: |
#   TemperatureSweep procedure: sweeps temperature (linear, piecewise-segmented
#   with a fine sub-range, or a custom CSV list — see sweep_axis) and runs ANY
#   station measurement VI at each stable temperature, selected in the GUI. The
#   ramp rate between steps is set per-sweep so it can be changed without editing
#   the YAML config. All generic machinery lives in
#   core.procedure.SweepMeasureProcedure; this file supplies the temperature-axis
#   specifics and the optional held-field magnets. It replaces the former
#   temperature_sweep_dc.py (one procedure per measurement method).
# entry_point: Not run directly. Instantiated via GUI or tests.
# dependencies:
#   - cryosoft.core.procedure (SweepMeasureProcedure)
#   - cryosoft.core.exceptions (CryoSoftConfigError)
#   - cryosoft.core.plan (Target)
#   - cryosoft.core.sweep_builder (SweepAxis)
#   - Station must have: temperature_vti (system VI) and at least one measurement
#     VI. magnet_z / magnet_y are OPTIONAL: a missing magnet is skipped when its
#     requested field is 0, and refused at construction when the field is nonzero.
# input: |
#   station, sample_info, data_directory, and keyword params: measurement_vi
#   (name of the measurement VI; defaults to the first registered one), field_z,
#   field_y, ramp_rate_K_per_min, point_wait, the selected VI's own measurement
#   parameters, and the sweep_axis-generated temperature_mode/temperature_start/
#   temperature_end/temperature_steps/temperature_segments/temperature_csv_path/
#   temperature_hysteresis.
# process: |
#   initiate() ramps temperature_vti to the first sweep point (at ramp_rate),
#   magnet_z/magnet_y to their held fields, and arms the selected measurement VI.
#   change_sweep_step() ramps only temperature_vti to the next step. measure()
#   reads the VI, tags on the temperature read-back, validates, and saves.
#   standby() closes the data file; temperature holds at the last set point.
# output: |
#   initiate()/standby() return a PhasePlan, change_sweep_step() a StepPlan|None,
#   abort() a tuple[Command, ...]. Side effect: an HDF5 file with
#   /data/temperature_K[N], the selected VI's arrays and scalar columns,
#   /data/timestamp[N], /snapshots/ and /metadata/.
# last_updated: 2026-07-13
# ---

"""TemperatureSweep — temperature sweep running any selected measurement method."""

from __future__ import annotations

import logging
from typing import Any

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.plan import ParamSpec, Target
from cryosoft.core.procedure import SweepMeasureProcedure
from cryosoft.core.sweep_builder import SweepAxis

logger = logging.getLogger(__name__)


class TemperatureSweep(SweepMeasureProcedure):
    """Sweep temperature and measure with any selected measurement VI.

    This is a generic sweep procedure (see ``SweepMeasureProcedure``): the
    measurement method is chosen in the GUI, so the same procedure runs a DC
    resistance measurement, a delta-mode IV, or any future measurement VI with
    no new code.

    Procedure flow:
    1. ``initiate()``: ramp ``temperature_vti`` to the first temperature at
       ``ramp_rate_K_per_min``, ramp any present magnets to their held fields,
       arm the selected measurement VI.
    2. ``measure()``: read the VI, tag on the temperature read-back, save.
    3. ``change_sweep_step()``: ramp ``temperature_vti`` to the next step.
    4. ``standby()``: close the data file; temperature holds at the last point.

    The ramp rate is passed per-step via the temperature ``Target``, so it takes
    effect immediately at each step without a YAML config change.

    Required VIs in Station:
        ``temperature_vti`` (system) and at least one measurement VI.
        ``magnet_z`` / ``magnet_y`` are optional — used when present; a nonzero
        field on a missing magnet is refused at construction.
    """

    name = "Temperature Sweep"
    description = "Sweep temperature, measure with the selected method at each stable point"
    sweep_axis = SweepAxis(
        key="temperature",
        unit="K",
        data_key="temperature_K",
        description="VTI temperature",
        default_start=10.0,
        default_end=300.0,
        default_steps=30,
    )
    sweep_data_keys = [sweep_axis.data_key]
    default_x_key = sweep_axis.data_key

    system_parameters = {
        "field_z": ParamSpec(
            type=float,
            default=0.0,
            unit="T",
            description="Applied field (magnet Z, held constant)",
        ),
        "field_y": ParamSpec(
            type=float,
            default=0.0,
            unit="T",
            description="Applied field (magnet Y, held constant)",
        ),
        "ramp_rate_K_per_min": ParamSpec(
            type=float,
            default=2.0,
            unit="K/min",
            description="Temperature ramp rate between steps",
        ),
        "set_vti_temperature": ParamSpec(
            type=bool,
            default=True,
            description=(
                "Set the VTI temperature (the swept axis). Off = measure at each "
                "sweep point without commanding the VTI, e.g. following a passive drift"
            ),
        ),
        "set_sample_temperature": ParamSpec(
            type=bool,
            default=False,
            description="Hold the sample-stage temperature during this run",
        ),
        "sample_temperature": ParamSpec(
            type=float,
            default=10.0,
            unit="K",
            description=(
                "Sample-stage temperature setpoint, held constant "
                "(ignored when 'set_sample_temperature' is off)"
            ),
        ),
        "point_wait": ParamSpec(
            type=float,
            default=60.0,
            unit="s",
            description="Wait after reaching each temperature (thermal equilibration)",
        ),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Resolve the measurement VI, then validate the magnet configuration.

        The applied fields are optional: a station without ``magnet_z`` /
        ``magnet_y`` can still run this sweep at zero field. But a NONZERO field
        requested on a missing magnet is refused here, at construction — silently
        measuring at 0 T while the metadata claims 0.5 T would corrupt a dataset
        without anyone noticing.

        Raises:
            CryoSoftConfigError: If a nonzero field is requested on a magnet the
                station does not have (or, via the base class, if the station has
                no measurement VI or a parameter collision occurs).
        """
        super().__init__(*args, **kwargs)
        self._magnet_targets: dict[str, Target] = {}
        for magnet, param in (("magnet_z", "field_z"), ("magnet_y", "field_y")):
            field = float(self._params[param])
            if self._station.has_vi(magnet):
                self._magnet_targets[magnet] = Target(field)
            elif field != 0.0:
                raise CryoSoftConfigError(
                    f"{param}={field} T requested, but this station has no "
                    f"'{magnet}' VI. Set {param} to 0 or configure the magnet."
                )
            else:
                logger.info(
                    "TemperatureSweep: station has no '%s' — running without it "
                    "(%s=0).", magnet, param,
                )
        for toggle, vi_name in (
            ("set_vti_temperature", "temperature_vti"),
            ("set_sample_temperature", "temperature_sample"),
        ):
            if self._params[toggle] and not self._station.has_vi(vi_name):
                raise CryoSoftConfigError(
                    f"'{toggle}' is on, but this station has no '{vi_name}' VI. "
                    f"Switch {toggle} off, or configure that controller."
                )

    def _temp_target(self, index: int) -> Target:
        """Build the ``temperature_vti`` ``Target`` at *index* (with ramp rate)."""
        return Target(
            self._sweep[index],
            rate=self._params["ramp_rate_K_per_min"],
        )

    # ------------------------------------------------------------------
    # Axis-specific hooks (SweepMeasureProcedure owns the four-method loop)
    # ------------------------------------------------------------------

    def _sweep_targets(self, index: int) -> dict[str, Target]:
        """Build the swept-channel target at *index*, honouring its on/off toggle.

        With ``set_vti_temperature`` off the dict is empty: the Orchestrator
        never ramps the VTI, and the sweep walks its points measuring whatever
        temperature the cryostat happens to be at (the read-back still records
        the true value). Monitoring is unaffected either way.
        """
        if not self._params["set_vti_temperature"]:
            return {}
        return {"temperature_vti": self._temp_target(index)}

    def _initial_system_targets(self) -> dict[str, Target]:
        """Ramp the enabled temperature channels to their first values, plus magnets."""
        targets = {
            **self._sweep_targets(0),
            **self._magnet_targets,  # only magnets the station actually has
        }
        if self._params["set_sample_temperature"]:
            targets["temperature_sample"] = Target(self._params["sample_temperature"])
        return targets

    def _step_targets(self, index: int) -> dict[str, Target]:
        """Ramp ``temperature_vti`` to the temperature at *index* (with rate).

        The sample stage is held at its fixed setpoint from ``initiate()`` and is
        deliberately not re-sent each step.
        """
        return self._sweep_targets(index)

    def _standby_targets(self) -> dict[str, Target]:
        """No system targets — temperature holds at the last set point."""
        return {}

    def _axis_readback(self) -> float:
        """Read the current temperature from ``temperature_vti``."""
        return self._station.temperature_vti.temperature()

    def _initiate_wait_s(self) -> float:
        """Settle time after the initial ramp (``point_wait``)."""
        return float(self._params["point_wait"])

    def _step_wait_s(self) -> float:
        """Settle time after each temperature step (``point_wait``)."""
        return float(self._params["point_wait"])
