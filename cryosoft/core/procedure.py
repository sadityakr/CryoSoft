# ---
# description: |
#   BaseProcedure abstract base class for all CryoSoft measurement procedures.
#   Defines the four-method interface (initiate, change_sweep_step, measure,
#   standby) consumed by the Orchestrator state machine.
# entry_point: Not run directly. Subclassed by concrete procedures.
# dependencies:
#   - cryosoft.core.station (Station)
#   - cryosoft.core.data_manager (DataManager, created by subclasses)
# input: |
#   Constructor receives a Station, sample_info dict, data_directory string,
#   and **param_values matching the procedure's parameters class attribute.
# process: |
#   _build_sweep_array() is called at construction to build the sweep points list.
#   The Orchestrator calls initiate(), then alternates change_sweep_step() /
#   measure() until done, then calls standby().
# output: |
#   initiate() and standby() return (system_targets, measurement_commands, wait_time).
#   change_sweep_step() returns (system_targets, wait_time) or None when done.
# last_updated: 2026-04-06
# ---

"""BaseProcedure — abstract base class for all CryoSoft procedures."""

from __future__ import annotations

import time
from typing import Any

from cryosoft.core.station import Station


class BaseProcedure:
    """Abstract base class for declarative measurement procedures.

    Procedures declare *what* the system should do; the Orchestrator handles
    *how* it executes. A procedure never calls driver or VI methods directly
    during ramping — it returns dicts and the Orchestrator dispatches them.

    Class attributes:
        name: Human-readable display name (shown in GUI procedure browser).
        description: One-line description.
        parameters: Dict describing GUI form fields. Each key is a parameter
            name; the value is a dict with keys: ``type``, ``default``,
            optionally ``unit``, ``min``, ``max``, ``description``.

    Example::

        class FieldSweepIV(BaseProcedure):
            name = "Field Sweep IV"
            description = "Sweep magnetic field, measure IV at each point"
            parameters = {
                "field_start": {"type": float, "default": -1.0, "unit": "T"},
            }
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, dict] = {}
    sweep_data_keys: list[str] = []        # procedure's own scalar sweep columns (e.g. field_T)
    measurement_data_keys: list[str] = []  # measurement array columns (e.g. voltage_V, current_A)
    default_x_key: str = ""                # default X axis for live plots

    def __init__(
        self,
        station: Station,
        sample_info: dict[str, str],
        data_directory: str,
        **param_values: Any,
    ) -> None:
        """Initialise the procedure.

        Args:
            station: The active Station instance.
            sample_info: ``{"sample_name": str, "sample_id": str, "comments": str}``.
            data_directory: Base directory for HDF5 output files.
            **param_values: Procedure-specific parameter values from the GUI form.
        """
        self._station = station
        self._sample_info = sample_info
        self._data_directory = data_directory
        self._params: dict[str, Any] = param_values
        self._data_manager = None
        self._sweep: list = self._build_sweep_array()
        self._index: int = 0

    # ------------------------------------------------------------------
    # Override in subclass
    # ------------------------------------------------------------------

    def _build_sweep_array(self) -> list:
        """Build the list of sweep points from ``self._params``.

        Called once at construction. Override in every concrete subclass.

        Returns:
            List of sweep point values (e.g. field values in tesla).
        """
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
