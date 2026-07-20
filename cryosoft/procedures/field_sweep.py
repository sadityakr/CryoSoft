# ---
# description: |
#   FieldSweep procedure: sweeps the magnetic field (linear, piecewise-segmented
#   with a fine subfield, or a custom CSV list — see sweep_axis) and runs ANY
#   station measurement VI at each point, selected in the GUI. All generic
#   machinery (measurement-VI selection, DataSchema assembly, the four-method
#   loop) lives in core.procedure.SweepMeasureProcedure; this file supplies only
#   the field-axis specifics. It replaces the former field_sweep_dc.py /
#   field_sweep_iv.py (one procedure per measurement method).
# entry_point: Not run directly. Instantiated via GUI or tests.
# dependencies:
#   - cryosoft.core.procedure (SweepMeasureProcedure)
#   - cryosoft.core.plan (Target)
#   - cryosoft.core.sweep_builder (SweepAxis)
#   - Station must have: magnet_z (system VI), temperature_vti (system VI), and
#     at least one measurement VI (vi_type == "measurement").
# input: |
#   station, sample_info, data_directory, and keyword params: measurement_vi
#   (name of the measurement VI to run; defaults to the first registered one),
#   temperature, init_wait, step_wait, the selected VI's own measurement
#   parameters, and the sweep_axis-generated field_mode/field_start/field_end/
#   field_steps/field_segments/field_csv_path/field_hysteresis.
# process: |
#   initiate() ramps magnet_z to the first field and temperature_vti to the
#   target temperature, arms the selected measurement VI, and assembles the
#   DataSchema. change_sweep_step() steps magnet_z through the fields. measure()
#   reads the VI, tags on the field read-back, validates, and saves. standby()
#   parks magnet_z at 0 T.
# output: |
#   initiate()/standby() return a PhasePlan, change_sweep_step() a StepPlan|None,
#   abort() a tuple[Command, ...]. Side effect: an HDF5 file with /data/field_T[N],
#   the selected VI's arrays (e.g. voltage_V[N,M], current_A[N,M]) and any scalar
#   columns (e.g. n_valid[N]), /data/timestamp[N], /snapshots/ and /metadata/.
# last_updated: 2026-07-13
# ---

"""FieldSweep — magnetic field sweep running any selected measurement method."""

from __future__ import annotations

from typing import Any

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.plan import ParamSpec, Target
from cryosoft.core.procedure import SweepMeasureProcedure
from cryosoft.core.sweep_builder import SweepAxis


class FieldSweep(SweepMeasureProcedure):
    """Sweep the magnetic field and measure with any selected measurement VI.

    This is a generic sweep procedure (see ``SweepMeasureProcedure``): the
    measurement method is chosen in the GUI, so the same procedure runs a DC
    resistance measurement, a delta-mode IV, or any future measurement VI with
    no new code. Adding a new *measurement* is adding a measurement VI, not a
    procedure; adding a new *sweep axis* is a small subclass like this one that
    supplies the ramp targets and the axis read-back.

    Procedure flow:
    1. ``initiate()``: ramp ``magnet_z`` to the first field, ``temperature_vti``
       to the target temperature, arm the selected measurement VI.
    2. ``measure()``: read the VI, tag on the field read-back, save.
    3. ``change_sweep_step()``: step ``magnet_z`` to the next field.
    4. ``standby()``: park ``magnet_z`` at 0 T, disarm the VI.

    Required VIs in Station:
        ``magnet_z`` (system), ``temperature_vti`` (system), and at least one
        measurement VI. In normal (non-persistent) mode the magnet keeps its
        switch heater energised across the whole sweep, so the per-point ramps
        are plain field targets.
    """

    name = "Field Sweep"
    description = "Sweep magnetic field, measure with the selected method at each point"
    sweep_axis = SweepAxis(
        key="field",
        unit="T",
        data_key="field_T",
        description="Magnetic field",
        default_start=-1.0,
        default_end=1.0,
        default_steps=101,
    )
    sweep_data_keys = [sweep_axis.data_key]
    default_x_key = sweep_axis.data_key

    system_parameters = {
        "set_vti_temperature": ParamSpec(
            type=bool,
            default=True,
            description="Set the VTI temperature during this run (off = leave it alone)",
        ),
        "temperature": ParamSpec(
            type=float,
            default=10.0,
            unit="K",
            description="VTI temperature setpoint (ignored when 'set_vti_temperature' is off)",
        ),
        "set_sample_temperature": ParamSpec(
            type=bool,
            default=False,
            description="Set the sample-stage temperature during this run",
        ),
        "sample_temperature": ParamSpec(
            type=float,
            default=10.0,
            unit="K",
            description=(
                "Sample-stage temperature setpoint "
                "(ignored when 'set_sample_temperature' is off)"
            ),
        ),
        "init_wait": ParamSpec(
            type=float,
            default=300.0,
            unit="s",
            description="Wait after initial ramp (thermal equilibration)",
        ),
        "step_wait": ParamSpec(
            type=float,
            default=5.0,
            unit="s",
            description="Wait between field steps",
        ),
    }

    # ------------------------------------------------------------------
    # Axis-specific hooks (SweepMeasureProcedure owns the four-method loop)
    # ------------------------------------------------------------------

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the procedure, then check the switched-on temperature channels exist.

        A channel the user switched ON must actually be present on the station.
        Failing here, at construction, beats discovering it mid-run: silently
        measuring while a requested setpoint was never applied would corrupt a
        dataset whose metadata claims that temperature. A switched-OFF channel
        is not required to exist, so a station with no sample loop runs fine.

        Raises:
            CryoSoftConfigError: If an enabled temperature channel has no VI.
        """
        super().__init__(*args, **kwargs)
        for toggle, vi_name in (
            ("set_vti_temperature", "temperature_vti"),
            ("set_sample_temperature", "temperature_sample"),
        ):
            if self._params[toggle] and not self._station.has_vi(vi_name):
                raise CryoSoftConfigError(
                    f"'{toggle}' is on, but this station has no '{vi_name}' VI. "
                    f"Switch {toggle} off, or configure that controller."
                )

    def _initial_system_targets(self) -> dict[str, Target]:
        """Ramp ``magnet_z`` to the first field and any enabled temperature channels.

        A channel whose toggle is off is simply absent from the returned dict, so
        the Orchestrator never ramps it and the controller holds wherever the
        operator left it. Monitoring is unaffected — readings come from the tick
        loop's monitor pass, not from targets.
        """
        targets = {"magnet_z": Target(self._sweep[0])}
        if self._params["set_vti_temperature"]:
            targets["temperature_vti"] = Target(self._params["temperature"])
        if self._params["set_sample_temperature"]:
            targets["temperature_sample"] = Target(self._params["sample_temperature"])
        return targets

    def _step_targets(self, index: int) -> dict[str, Target]:
        """Ramp ``magnet_z`` to the field at *index*."""
        return {"magnet_z": Target(self._sweep[index])}

    def _standby_targets(self) -> dict[str, Target]:
        """Park ``magnet_z`` at 0 T (switch heater stays on — see class docstring)."""
        return {"magnet_z": Target(0.0)}

    def _axis_readback(self) -> float:
        """Read the current field from ``magnet_z``."""
        return self._station.magnet_z.get_field()

    def _initiate_wait_s(self) -> float:
        """Settle time after the initial ramp (``init_wait``)."""
        return float(self._params["init_wait"])

    def _step_wait_s(self) -> float:
        """Settle time between field steps (``step_wait``)."""
        return float(self._params["step_wait"])
