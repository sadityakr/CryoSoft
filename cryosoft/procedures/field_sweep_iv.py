# ---
# description: |
#   FieldSweepIV procedure: sweeps the magnetic field (linear, piecewise-
#   segmented with a fine subfield, or a custom CSV list — see sweep_axis),
#   measuring IV (delta-mode) at each point. Stores data and station
#   snapshots in an HDF5 file via DataManager. This is the CryoSoft port of
#   the old field_voltage_logic.py script (Mercury iPS-M + Keithley
#   6221/2182A delta mode).
# entry_point: Not run directly. Instantiated via GUI or tests.
# dependencies:
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.core.data_manager (DataManager)
#   - cryosoft.core.plan (Command, PhasePlan, StepPlan, Target)
#   - cryosoft.core.sweep_builder (SweepAxis) — sweep-shape construction is
#     handled entirely by BaseProcedure's default _build_sweep_array()
#   - Station must have: magnet_x (system VI), temperature_vti (system VI),
#     keithley_delta_mode (measurement VI with configure() and read_datapoint()).
# input: |
#   station, sample_info, data_directory, and keyword params matching the
#   parameters dict: temperature, init_wait, step_wait; delta-mode measurement
#   params current, n_readings, voltmeter_range_V (enumerated 2182A range),
#   compliance_V, delay_s, compliance_abort (bool), cold_switch (bool); plus
#   the sweep_axis-generated field_mode/field_start/field_end/field_steps/
#   field_segments/field_csv_path/field_hysteresis (see
#   core.sweep_builder.sweep_axis_param_specs()).
# process: |
#   initiate() ramps to first field point + target temperature, configures
#   delta-mode. change_sweep_step() steps through fields. measure() reads
#   IV data and snapshots state. standby() parks magnet at 0 T.
# output: |
#   initiate()/standby() return a PhasePlan, change_sweep_step() a StepPlan|None,
#   abort() a tuple[Command, ...]. Side effect: an HDF5 file with /data/field_T[N],
#   /data/voltage_V[N,M], /data/current_A[N,M], /data/timestamp[N], /snapshots/
#   and /metadata/.
# last_updated: 2026-07-13
# ---

"""FieldSweepIV — magnetic field sweep with IV delta-mode measurement."""

from __future__ import annotations

import dataclasses
import logging

from cryosoft.core.data_manager import DataManager
from cryosoft.core.plan import Command, PhasePlan, StepPlan, Target
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.sweep_builder import SweepAxis

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

    name = "Field Sweep IV (Delta Mode)"
    description = "Sweep magnetic field, measure delta-mode IV at each point"
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
    measurement_data_keys = ["voltage_V", "current_A"]
    default_x_key = sweep_axis.data_key

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
            "description": "Peak delta current (±I, reversed each cycle)",
        },
        "n_readings": {
            "type": int,
            "default": 100,
            "min": 1,
            "description": "Readings per point (delta mode)",
        },
        "voltmeter_range_V": {
            "type": float,
            "default": 0.01,
            "unit": "V",
            "choices": {
                "10 mV": 0.01,
                "100 mV": 0.1,
                "1 V": 1.0,
                "10 V": 10.0,
                "100 V": 100.0,
            },
            "description": "Keithley 2182A voltmeter measurement range",
        },
        "compliance_V": {
            "type": float,
            "default": 1.0,
            "unit": "V",
            "description": "Source voltage compliance limit",
        },
        "delay_s": {
            "type": float,
            "default": 0.01,
            "unit": "s",
            "description": "Delta inter-transition delay (0 = hardware minimum)",
        },
        "compliance_abort": {
            "type": bool,
            "default": True,
            "description": "Abort the delta run if the source reaches compliance",
        },
        "cold_switch": {
            "type": bool,
            "default": False,
            "description": "Cold-switch between current reversals (lower thermal EMF)",
        },
    }

    # ------------------------------------------------------------------
    # Four-method interface
    # ------------------------------------------------------------------
    # _build_sweep_array() is not overridden here: BaseProcedure's default
    # implementation delegates to sweep_builder.build_axis_sweep() using the
    # sweep_axis declared above (linear / segments / CSV, with hysteresis).

    def initiate(self) -> PhasePlan:
        """Set initial field + temperature targets, configure measurement VI.

        Creates the DataManager and HDF5 file for this run.

        Returns:
            A ``PhasePlan`` with the initial field/temperature ``targets``, the
            ``keithley_delta_mode`` configure ``Command``, and
            ``wait_s=init_wait``.
        """
        n_readings = int(self._params["n_readings"])

        targets = {
            # In normal (non-persistent) mode the magnet VI energises the switch
            # heater once and keeps it on across the whole sweep, so the ramps
            # here are plain field targets — no per-point re-heat/re-cool.
            "magnet_x": Target(self._sweep[0]),
            "temperature_vti": Target(self._params["temperature"]),
        }

        commands = (
            Command(
                "keithley_delta_mode",
                "configure",
                {
                    "method": "delta_mode",
                    "current": self._params["current"],
                    "n_readings": n_readings,
                    "delay": self._params["delay_s"],
                    "compliance": self._params["compliance_V"],
                    "range_2182a": self._params["voltmeter_range_V"],
                    "compliance_abort": self._params["compliance_abort"],
                    "cold_switch": self._params["cold_switch"],
                },
            ),
        )

        base_config: dict = {
            "sweep_columns": {type(self).sweep_axis.data_key: "float"},
            "measurement_arrays": {
                "voltage_V": n_readings,
                "current_A": n_readings,
            },
        }

        self._data_manager = DataManager(
            data_directory=self._data_directory,
            procedure_name=self.name,
            file_prefix=self._file_prefix,
            procedure_params=self._params,
            sample_info=self._sample_info,
            instrument_state=self._station.get_state(),
            # DataManager stays dict-based (contract C7): convert the typed plan
            # to plain JSON-ready dicts at this call-site boundary only.
            system_targets={name: dataclasses.asdict(t) for name, t in targets.items()},
            measurement_commands=[dataclasses.asdict(c) for c in commands],
            data_config=self._build_data_config(base_config),
            n_sweep_points=len(self._sweep),
        )

        logger.info(
            "FieldSweepIV.initiate(): %d steps from %.3f T to %.3f T, T=%.1f K",
            len(self._sweep),
            self._sweep[0],
            self._sweep[-1],
            self._params["temperature"],
        )

        return PhasePlan(
            targets=targets, commands=commands, wait_s=float(self._params["init_wait"])
        )

    def change_sweep_step(self) -> StepPlan | None:
        """Advance to the next field step.

        Returns:
            A ``StepPlan`` ramping ``magnet_x`` to the next field with
            ``wait_s=step_wait``, or ``None`` when all sweep points have been
            measured.
        """
        self._index += 1
        if self._index >= len(self._sweep):
            return None

        return StepPlan(
            targets={"magnet_x": Target(self._sweep[self._index])},
            wait_s=float(self._params["step_wait"]),
        )

    def measure(self) -> None:
        """Read IV data, snapshot station state, save to HDF5.

        Reads from ``keithley_delta_mode.read_datapoint()`` (must have been
        configured via ``initiate()``). Also reads the current field from
        ``magnet_x.get_field()`` and saves it as the sweep column.
        """
        if self._data_manager is None:
            raise RuntimeError("measure() called before initiate()")

        measured_data: dict = self._station.keithley_delta_mode.read_datapoint()
        measured_data[type(self).sweep_axis.data_key] = self._station.magnet_x.get_field()
        self._save_datapoint(measured_data)

        logger.debug(
            "FieldSweepIV.measure(): index=%d, field=%.4f T",
            self._index,
            measured_data[type(self).sweep_axis.data_key],
        )

    def standby(self) -> PhasePlan:
        """Close data file and park magnet at 0 T.

        Runs in normal mode, so the field ramps to 0 T with the switch heater
        left on (procedures never enter persistent mode — that is a manual
        Monitor-window action). The magnet ends at zero field, ready for the
        next run.

        Returns:
            A ``PhasePlan`` ramping ``magnet_x`` to 0 T, disarming
            ``keithley_delta_mode``, with ``wait_s=0.0``.
        """
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None

        return PhasePlan(
            targets={"magnet_x": Target(0.0)},
            commands=(Command("keithley_delta_mode", "standby", {}),),
            wait_s=0.0,
        )

    def abort(self) -> tuple[Command, ...]:
        """Close the data file and stop the delta engine (no ramping).

        Returns:
            Measurement safe-off commands for the Orchestrator to dispatch.
        """
        super().abort()
        return (Command("keithley_delta_mode", "standby", {}),)
