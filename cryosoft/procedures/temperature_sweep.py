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
    1. ``initiate()``: ramp ``temperature_vti`` (and, when enabled,
       ``temperature_sample``) to the first temperature at
       ``ramp_rate_K_per_min``, ramp ``magnet_z`` to its held field, arm the
       selected measurement VI.
    2. ``measure()``: read the VI, tag on the temperature read-back, save.
    3. ``change_sweep_step()``: ramp both temperature channels to the next step.
    4. ``standby()``: close the data file; temperature holds at the last point.

    The sample stage follows the sweep: its setpoint at every point is the swept
    temperature plus ``sample_offset``. The ramp rate is passed per-step via
    each temperature ``Target``, so it takes effect immediately at each step
    without a YAML config change. After each point settles the procedure waits
    ``dwell_time`` seconds before measuring.

    Required VIs in Station:
        ``temperature_vti`` (system) and at least one measurement VI.
        ``magnet_z`` is optional — used when present; a nonzero field on a
        missing magnet is refused at construction. ``temperature_sample`` is
        required only when ``set_sample_temperature`` is on.
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
        "ramp_rate_K_per_min": ParamSpec(
            type=float,
            default=2.0,
            unit="K/min",
            description="Sweep rate: ramp rate of the VTI and sample stage between steps",
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
            description=(
                "Also control the sample-stage temperature, following the sweep "
                "at 'sample_offset' from the swept temperature"
            ),
        ),
        "sample_offset": ParamSpec(
            type=float,
            default=0.0,
            unit="K",
            description=(
                "Sample setpoint minus swept temperature "
                "(ignored when 'set_sample_temperature' is off)"
            ),
        ),
        "dwell_time": ParamSpec(
            type=float,
            default=60.0,
            unit="s",
            description="Dwell: how long to wait at each temperature before measuring",
        ),
    }

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Resolve the measurement VI, then validate the magnet and temperature channels.

        The applied field is optional: a station without ``magnet_z`` can still
        run this sweep at zero field. But a NONZERO field requested on a missing
        magnet is refused here, at construction — silently measuring at 0 T
        while the metadata claims 0.5 T would corrupt a dataset without anyone
        noticing.

        Raises:
            CryoSoftConfigError: If a nonzero field is requested on a station
                without ``magnet_z``, if an enabled temperature channel has no
                VI (or, via the base class, if the station has no measurement VI
                or a parameter collision occurs).
        """
        super().__init__(*args, **kwargs)
        self._magnet_targets: dict[str, Target] = {}
        field = float(self._params["field_z"])
        if self._station.has_vi("magnet_z"):
            self._magnet_targets["magnet_z"] = Target(field)
        elif field != 0.0:
            raise CryoSoftConfigError(
                f"field_z={field} T requested, but this station has no "
                f"'magnet_z' VI. Set field_z to 0 or configure the magnet."
            )
        else:
            logger.info(
                "TemperatureSweep: station has no 'magnet_z' — running without it "
                "(field_z=0)."
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

    # ------------------------------------------------------------------
    # Axis-specific hooks (SweepMeasureProcedure owns the four-method loop)
    # ------------------------------------------------------------------

    def _sweep_targets(self, index: int) -> dict[str, Target]:
        """Build the temperature targets at *index*, honouring the on/off toggles.

        The VTI goes to the swept value; the sample stage, when enabled, goes to
        the swept value plus ``sample_offset``. Both carry the sweep rate. With
        both toggles off the dict is empty: the Orchestrator never ramps either
        controller and the sweep measures whatever temperature the cryostat
        happens to be at (the read-back still records the true value).
        """
        value = self._sweep[index]
        rate = self._params["ramp_rate_K_per_min"]
        targets: dict[str, Target] = {}
        if self._params["set_vti_temperature"]:
            targets["temperature_vti"] = Target(value, rate=rate)
        if self._params["set_sample_temperature"]:
            targets["temperature_sample"] = Target(
                value + self._params["sample_offset"], rate=rate
            )
        return targets

    def _initial_system_targets(self) -> dict[str, Target]:
        """Ramp the enabled temperature channels to their first values, plus the magnet."""
        return {
            **self._sweep_targets(0),
            **self._magnet_targets,  # only if the station has magnet_z
        }

    def _step_targets(self, index: int) -> dict[str, Target]:
        """Ramp the enabled temperature channels to the values at *index*."""
        return self._sweep_targets(index)

    def _standby_targets(self) -> dict[str, Target]:
        """No system targets — temperature holds at the last set point."""
        return {}

    def _axis_readback(self) -> float:
        """Read the current temperature from ``temperature_vti``."""
        return self._station.temperature_vti.temperature()

    def _initiate_wait_s(self) -> float:
        """Dwell after the initial ramp (``dwell_time``)."""
        return float(self._params["dwell_time"])

    def _step_wait_s(self) -> float:
        """Dwell at each temperature step (``dwell_time``)."""
        return float(self._params["dwell_time"])
