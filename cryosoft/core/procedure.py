# ---
# description: |
#   BaseProcedure abstract base class for all CryoSoft measurement procedures.
#   Defines the four-method interface (initiate, change_sweep_step, measure,
#   standby) consumed by the Orchestrator state machine. Also defines the
#   sweep_axis mechanism: a subclass that declares one SweepAxis gets a
#   working _build_sweep_array() (linear / segments / CSV, with optional
#   hysteresis) for free, with no override needed.
# entry_point: Not run directly. Subclassed by concrete procedures.
# dependencies:
#   - cryosoft.core.station (Station)
#   - cryosoft.core.data_manager (DataManager, created by subclasses)
#   - cryosoft.core.sweep_builder (SweepAxis, sweep_axis_param_specs, build_axis_sweep)
# input: |
#   Constructor receives a Station, sample_info dict, data_directory string,
#   and **param_values matching the procedure's parameters class attribute.
#   Declared parameter defaults are merged in for any omitted param_values.
# process: |
#   _build_sweep_array() is called at construction to build the sweep points list.
#   If the subclass declares sweep_axis, the default implementation delegates
#   to sweep_builder.build_axis_sweep(); otherwise a subclass must override it.
#   The Orchestrator calls initiate(), then alternates change_sweep_step() /
#   measure() until done, then calls standby(). On user abort or ERROR/
#   EMERGENCY entry it calls abort() instead, which closes the data file
#   (default implementation) and returns measurement safe-off commands.
# output: |
#   initiate() and standby() return (system_targets, measurement_commands, wait_time).
#   change_sweep_step() returns (system_targets, wait_time) or None when done.
# last_updated: 2026-07-12
# ---

"""BaseProcedure — abstract base class for all CryoSoft procedures."""

from __future__ import annotations

import time
from typing import Any

from cryosoft.core.station import Station
from cryosoft.core.sweep_builder import SweepAxis, build_axis_sweep, sweep_axis_param_specs


class BaseProcedure:
    """Abstract base class for declarative measurement procedures.

    Procedures declare *what* the system should do; the Orchestrator handles
    *how* it executes. A procedure never calls driver or VI methods directly
    during ramping — it returns dicts and the Orchestrator dispatches them.

    Class attributes:
        name: Human-readable display name (shown in GUI procedure browser).
        description: One-line description.
        sweep_axis: Optional ``SweepAxis`` declaring the one quantity this
            procedure sweeps over (e.g. field, temperature). When set, its
            hidden parameters (``{key}_mode``, ``{key}_start``, etc. — see
            ``sweep_builder.sweep_axis_param_specs()``) are merged into
            ``parameters`` automatically, ``_build_sweep_array()`` needs no
            override, and the GUI renders a mode-selector widget (linear /
            segments / CSV, with hysteresis) instead of flat text fields.
            Most sweep procedures should use this instead of hand-writing
            ``sweep_parameters`` + ``_build_sweep_array()``.
        sweep_parameters: Parameters that define *what* to sweep over, for
            procedures that are not using ``sweep_axis`` (or that need extra
            sweep-related parameters alongside it). The GUI renders these
            in the Sweep column of the parameter form.
        system_parameters: Parameters that set system state during the sweep
            (e.g. temperature, ramp rates, wait times). Rendered in the
            System column.
        measurement_parameters: Parameters that configure the measurement VI
            (e.g. current_A, compliance_A, readings_per_point). Rendered in
            the Measurement column.
        parameters: Union of sweep_axis's hidden params (if any),
            sweep_parameters, system_parameters, and measurement_parameters,
            auto-built by ``__init_subclass__``. Read-only — do not set
            directly.

    Each parameter value dict accepts keys: ``type``, ``default``,
    optionally ``unit``, ``min``, ``max``, ``description``.

    Example::

        class FieldSweepIV(BaseProcedure):
            name = "Field Sweep IV"
            description = "Sweep magnetic field, measure IV at each point"
            sweep_axis = SweepAxis(
                key="field", unit="T", data_key="field_T",
                description="Magnetic field",
                default_start=-1.0, default_end=1.0, default_steps=101,
            )
    """

    name: str = ""
    description: str = ""

    # Parameter groups — sweep_axis (optional) plus the three dicts below.
    # ``parameters`` is auto-built as their union by __init_subclass__ so
    # existing code that iterates cls.parameters continues to work.
    sweep_axis: SweepAxis | None = None
    sweep_parameters: dict[str, dict] = {}
    system_parameters: dict[str, dict] = {}
    measurement_parameters: dict[str, dict] = {}
    parameters: dict[str, dict] = {}

    sweep_data_keys: list[str] = []        # procedure's own scalar sweep columns (e.g. field_T)
    measurement_data_keys: list[str] = []  # measurement array columns (e.g. voltage_V, current_A)
    default_x_key: str = ""                # default X axis for live plots

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        axis_params = sweep_axis_param_specs(cls.sweep_axis) if cls.sweep_axis else {}
        cls.parameters = {
            **axis_params,
            **cls.sweep_parameters,
            **cls.system_parameters,
            **cls.measurement_parameters,
        }

    def __init__(
        self,
        station: Station,
        sample_info: dict[str, str],
        data_directory: str,
        file_prefix: str = "",
        **param_values: Any,
    ) -> None:
        """Initialise the procedure.

        Args:
            station: The active Station instance.
            sample_info: ``{"sample_name": str, "sample_id": str, "comments": str}``.
            data_directory: Base directory for HDF5 output files.
            file_prefix: User-chosen filename prefix for the HDF5 output file
                (still suffixed with a unique timestamp by ``DataManager``).
                Empty string falls back to ``self.name``. Frozen into the
                procedure instance at construction time, so queued entries
                each keep the prefix that was set when they were added.
            **param_values: Procedure-specific parameter values from the GUI form.
                Any declared parameter with a ``default`` that the caller omits
                is filled in automatically; caller-supplied values always win.
        """
        self._station = station
        self._sample_info = sample_info
        self._data_directory = data_directory
        self._file_prefix = file_prefix
        merged_params: dict[str, Any] = {
            param_name: spec["default"]
            for param_name, spec in type(self).parameters.items()
            if "default" in spec
        }
        merged_params.update(param_values)
        self._params: dict[str, Any] = merged_params
        self._data_manager = None
        self._sweep: list = self._build_sweep_array()
        self._index: int = 0

    # ------------------------------------------------------------------
    # Override in subclass
    # ------------------------------------------------------------------

    def _build_sweep_array(self) -> list:
        """Build the list of sweep points from ``self._params``.

        Called once at construction. If the subclass declares ``sweep_axis``,
        this delegates to ``sweep_builder.build_axis_sweep()`` and needs no
        override. A subclass without ``sweep_axis`` must override this.

        Returns:
            List of sweep point values (e.g. field values in tesla).
        """
        if type(self).sweep_axis is not None:
            return build_axis_sweep(type(self).sweep_axis, self._params)
        return []

    # ------------------------------------------------------------------
    # System-state enrichment helpers (called by subclass measure/initiate)
    # ------------------------------------------------------------------

    def _build_data_config(self, base_config: dict) -> dict:
        """Inject unix_time and all cached system-VI readings into *base_config*.

        Merges ``unix_time`` plus every numeric scalar from
        ``station.last_state_flat()`` into ``base_config["sweep_columns"]``.
        The procedure's own columns (already in *base_config*) take priority on
        any name collision.

        Args:
            base_config: Dict with ``sweep_columns`` and ``measurement_arrays``
                keys as accepted by DataManager.

        Returns:
            New dict with system columns prepended to sweep_columns.
        """
        system_keys = self._station.last_state_flat().keys()
        extra: dict[str, str] = {"unix_time": "float", **{k: "float" for k in system_keys}}
        result = dict(base_config)
        result["sweep_columns"] = {**extra, **base_config.get("sweep_columns", {})}
        return result

    def _save_datapoint(self, measured_data: dict) -> None:
        """Enrich *measured_data* with timestamp + system state, then save.

        Merges ``unix_time`` (UTC epoch float) and all cached system-VI readings
        into *measured_data* before forwarding to the DataManager. Uses the
        monitor-tick cache — no extra hardware poll. Procedure-specific keys in
        *measured_data* overwrite system-state keys on collision (e.g. a
        procedure's explicit ``field_T`` read takes precedence).

        Args:
            measured_data: Dict of measurement results from the measurement VI.

        Raises:
            RuntimeError: If called before ``initiate()`` (data_manager is None).
        """
        if self._data_manager is None:
            raise RuntimeError("_save_datapoint() called before initiate()")
        enriched = {
            "unix_time": time.time(),
            **self._station.last_state_flat(),
            **measured_data,
        }
        self._data_manager.save_datapoint(
            sweep_index=self._index,
            measured_data=enriched,
            station_snapshot=self._station.cached_state,
        )

    def get_data_keys(self) -> list[str]:
        """Return all datapoint keys available for live-plot axis selection.

        Called by ProcedureWindow before a run starts (i.e. before initiate())
        to populate the axis selectors. Reads system state from the monitor
        cache — no hardware poll.

        Returns:
            Ordered list: ``["unix_time"] + sweep_data_keys + system_keys
            + measurement_data_keys``.
        """
        system_keys = list(self._station.last_state_flat().keys())
        return (
            ["unix_time"]
            + self.sweep_data_keys
            + system_keys
            + self.measurement_data_keys
        )

    # ------------------------------------------------------------------
    # Progress tracking (used by Orchestrator for GUI progress bar)
    # ------------------------------------------------------------------

    def get_sweep_array(self) -> list:
        """Return the full sweep array."""
        return self._sweep

    def get_progress(self) -> float:
        """Return fractional progress from 0.0 to 1.0.

        Returns:
            0.0 at the start; 1.0 when the sweep is complete.
        """
        if not self._sweep:
            return 1.0
        return self._index / len(self._sweep)

    def get_sweep_position(self) -> tuple[int, int]:
        """Return ``(current_point, total_points)`` as human 1-based counts.

        The Orchestrator uses this to compose concise status lines such as
        "Point 13/101" without reaching into ``_index``. Returns ``(0, 0)``
        for an empty sweep.

        Returns:
            ``(index + 1, len(sweep))``; ``(0, 0)`` when the sweep is empty.
        """
        total = len(self._sweep)
        if total == 0:
            return (0, 0)
        return (self._index + 1, total)

    # ------------------------------------------------------------------
    # Four-method procedure interface — must override in subclass
    # ------------------------------------------------------------------

    def initiate(self) -> tuple[dict, dict, float]:
        """Set up the experiment and return initial targets.

        Jobs:
        1. Create the DataManager and HDF5 file.
        2. Return the initial system targets (first sweep point).
        3. Return measurement configuration commands.
        4. Return the wait time (seconds) after reaching initial targets.

        Returns:
            ``(system_targets, measurement_commands, wait_time)``

            * system_targets: ``{"vi_name": {"target": value}, ...}``
            * measurement_commands: ``{"vi_name": {"method": kwargs}, ...}``
            * wait_time: seconds to wait after targets reached

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement initiate()")

    def change_sweep_step(self) -> tuple[dict, float] | None:
        """Advance the sweep index and return the next targets.

        Called by the Orchestrator in SWEEPING state.

        Returns:
            ``(system_targets, wait_time)`` for the next sweep point, or
            ``None`` when the sweep is complete.

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement change_sweep_step()"
        )

    def measure(self) -> None:
        """Read data, snapshot station state, save to HDF5.

        Called by the Orchestrator in MEASURING state, after the system is
        stable at the current sweep point.

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement measure()")

    def standby(self) -> tuple[dict, dict, float]:
        """Close the data file and return safe parking targets.

        Called by the Orchestrator in STANDBY state after the sweep completes
        (or after an abort).

        Returns:
            ``(system_targets, measurement_commands, wait_time)``

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement standby()")

    # ------------------------------------------------------------------
    # Abort — has a working default; override to add measurement safe-off
    # ------------------------------------------------------------------

    def abort(self) -> dict:
        """Clean up after an abort: close the data file, keep partial data.

        Called by the Orchestrator on user abort and on ERROR/EMERGENCY
        entry. Unlike ``standby()`` it must not ramp anything — the
        Orchestrator holds the hardware separately via ``stop_ramps()``.

        Subclasses should extend this to also safe their measurement VI::

            def abort(self) -> dict:
                super().abort()
                return {"dc_measurement": {"standby": {}}}

        Returns:
            ``measurement_commands`` dict (``{vi_name: {method: kwargs}}``)
            the Orchestrator dispatches to safe the measurement hardware.
            The base implementation returns ``{}``.
        """
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None
        return {}
