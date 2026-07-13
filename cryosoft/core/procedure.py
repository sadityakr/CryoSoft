# ---
# description: |
#   BaseProcedure abstract base class for all CryoSoft measurement procedures,
#   plus SweepMeasureProcedure, the generic "sweep one axis, run any selected
#   measurement VI" intermediate base. BaseProcedure defines the four-method
#   interface (initiate, change_sweep_step, measure, standby) consumed by the
#   Orchestrator, the sweep_axis mechanism (a SweepAxis declaration gets a
#   working _build_sweep_array() for free), and per-datapoint DataSchema
#   validation in _save_datapoint. SweepMeasureProcedure adds measurement-VI
#   selection (a structural ``measurement_vi`` parameter whose choices come from
#   the station), station-driven measurement-parameter merging, DataSchema
#   assembly, and the shared four-method loop; a concrete axis procedure supplies
#   only the ramp targets, the axis read-back, and the settle times.
# entry_point: Not run directly. Subclassed by concrete procedures.
# dependencies:
#   - cryosoft.core.station (Station)
#   - cryosoft.core.data_manager (DataManager)
#   - cryosoft.core.exceptions (CryoSoftConfigError)
#   - cryosoft.core.plan (Command, Target, PhasePlan, StepPlan — the typed
#     currency; ParamSpec, ParamGroup, DataSchema — typed declarations/layout)
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
#   initiate() and standby() return a PhasePlan (targets, commands, wait_s).
#   change_sweep_step() returns a StepPlan (targets, wait_s) or None when done.
#   abort() returns a tuple[Command, ...] of measurement safe-off commands.
# last_updated: 2026-07-13
# ---

"""BaseProcedure — abstract base class for all CryoSoft procedures."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Mapping
from typing import Any

from cryosoft.core.data_manager import DataManager
from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.plan import (
    Command,
    DataSchema,
    ParamGroup,
    ParamSpec,
    PhasePlan,
    StepPlan,
    Target,
)
from cryosoft.core.station import Station
from cryosoft.core.sweep_builder import SweepAxis, build_axis_sweep, sweep_axis_param_specs

logger = logging.getLogger(__name__)


class BaseProcedure:
    """Abstract base class for declarative measurement procedures.

    Procedures declare *what* the system should do; the Orchestrator handles
    *how* it executes. A procedure never calls driver or VI methods directly
    during ramping — it returns typed plans (``PhasePlan`` / ``StepPlan``,
    built from ``Target`` and ``Command`` objects from ``cryosoft.core.plan``)
    and the Orchestrator dispatches them.

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

    Each parameter is declared as a ``ParamSpec`` (see ``cryosoft.core.plan``):
    ``type`` and ``default`` are required; ``unit``, ``min``, ``max``,
    ``description``, ``choices`` are optional. ``ParamSpec`` validates the
    declaration eagerly at construction (choices non-empty and correctly typed,
    default among the choices / within the bounds), so a malformed parameter
    fails when the class body runs rather than at form-render time.

    Input-widget types (how the GUI renders a parameter):

    * Plain number / text (default): any ``type`` (``float``, ``int``, ``str``)
      without ``choices`` renders as a free-text field parsed by ``type``.
    * Enumerated choice: set ``choices`` to a **label -> value dict**,
      e.g. ``{"10 mV": 0.01, "100 mV": 0.1}``. The GUI renders a drop-down
      showing the labels; the collected value is the mapped value (``0.01``),
      so the procedure never translates. ``default`` must be one of the
      mapped values, and every value must be an instance of ``type``.
    * Boolean toggle: set ``type=bool``. The GUI renders a checkbox; the
      collected value is ``True`` / ``False``.

    The ParamSpec -> Qt-widget mapping lives entirely in
    ``cryosoft.gui.param_form``; a procedure never names a widget class.
    ``tests/test_conformance.py`` checks every procedure declares ParamSpecs so
    they all inherit the same rules.

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
    sweep_parameters: dict[str, ParamSpec] = {}
    system_parameters: dict[str, ParamSpec] = {}
    measurement_parameters: dict[str, ParamSpec] = {}
    parameters: dict[str, ParamSpec] = {}

    sweep_data_keys: list[str] = []        # procedure's own scalar sweep columns (e.g. field_T)
    measurement_data_keys: list[str] = []  # measurement array columns (e.g. voltage_V, current_A)
    default_x_key: str = ""                # default X axis for live plots

    # Whether this procedure resolves its measurement VI (and that VI's
    # parameters) from the Station at construction, so it CANNOT be built from
    # an empty Station. Static procedures leave this False; the generic sweep
    # procedures (``SweepMeasureProcedure``) set it True. The conformance tests
    # read it to decide whether to hand the procedure a populated sim Station.
    requires_measurement_vi: bool = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        axis_params = sweep_axis_param_specs(cls.sweep_axis) if cls.sweep_axis else {}
        cls.parameters = {
            **axis_params,
            **cls.sweep_parameters,
            **cls.system_parameters,
            **cls.measurement_parameters,
        }

    @classmethod
    def get_param_groups(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> list[ParamGroup]:
        """Return the ordered ``ParamGroup`` list the GUI renders for this procedure.

        This is the hook the GUI form builder calls instead of reading the three
        class dicts directly. The default implementation returns the static
        Sweep / System / Measurement groups, in that order, SKIPPING any that
        declare no parameters. The ``sweep_axis`` hidden parameters are
        deliberately NOT in any group: the GUI renders them through the separate
        ``SweepAxisWidget`` (linear / segments / CSV), exactly as before.

        ``station`` and ``selections`` are unused by this default, but are part
        of the signature so a Wave-5 subclass can override this method to
        compute its groups dynamically — e.g. deriving measurement groups from
        the station's configured measurement VI, or re-deriving the whole form
        from a ``structural`` selection the user has changed (hence
        ``selections``, the current values of any structural parameters).

        Args:
            station: The active Station instance (unused by the default).
            selections: Current values of the form's structural parameters, or
                ``None`` before any have been chosen (unused by the default).

        Returns:
            The non-empty parameter groups, in Sweep, System, Measurement order.
        """
        candidates = (
            ("sweep", "Sweep", cls.sweep_parameters),
            ("system", "System", cls.system_parameters),
            ("measurement", "Measurement", cls.measurement_parameters),
        )
        return [
            ParamGroup(key=key, title=title, params=params)
            for key, title, params in candidates
            if params
        ]

    @classmethod
    def live_plot_measurement_keys(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> list[str]:
        """Return the measurement array/scalar keys available for live-plot axes.

        The GUI populates its plot axis selectors before a run starts, so it
        needs the measurement columns without an instance. The default returns
        the static ``measurement_data_keys`` class attribute. A procedure whose
        measurement columns depend on a station-selected VI overrides this to
        read them from that VI (see ``SweepMeasureProcedure``).

        Args:
            station: The active Station (unused by the default).
            selections: Current structural-parameter values, or ``None``
                (unused by the default).

        Returns:
            Ordered list of measurement column names for the plot selectors.
        """
        _ = (station, selections)
        return list(cls.measurement_data_keys)

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
        # Every ParamSpec carries a default (enforced at ParamSpec construction),
        # so the merge is unconditional — no "default present?" guard needed.
        merged_params: dict[str, Any] = {
            param_name: spec.default
            for param_name, spec in type(self).parameters.items()
        }
        merged_params.update(param_values)
        self._params: dict[str, Any] = merged_params
        self._data_manager = None
        # Optional declared HDF5 layout. A procedure that assembles a DataSchema
        # in initiate() (the generic sweep procedures do) stores it here, and
        # _save_datapoint() then validates every datapoint against it. Left None
        # by hand-written procedures, which keep working unvalidated.
        self._data_schema: DataSchema | None = None
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
            DataSchemaError: If a ``self._data_schema`` is set and the enriched
                datapoint does not match it (missing/extra columns or a
                wrong-length array). Propagates so the Orchestrator contains the
                run to ERROR before any malformed datapoint is written.
        """
        if self._data_manager is None:
            raise RuntimeError("_save_datapoint() called before initiate()")
        enriched = {
            "unix_time": time.time(),
            **self._station.last_state_flat(),
            **measured_data,
        }
        # Validate BEFORE writing: on a mismatch nothing is saved and the
        # DataSchemaError propagates. Skipped when no schema was declared.
        if self._data_schema is not None:
            self._data_schema.validate(enriched)
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

    def initiate(self) -> PhasePlan:
        """Set up the experiment and return the initial plan.

        Jobs:
        1. Create the DataManager and HDF5 file.
        2. Return the initial system targets (first sweep point).
        3. Return measurement configuration commands.
        4. Return the wait time (seconds) after reaching initial targets.

        Returns:
            A ``PhasePlan`` bundling ``targets`` (a ``{"vi_name": Target(...)}``
            mapping), ``commands`` (an ordered ``tuple[Command, ...]`` of
            measurement-VI calls), and ``wait_s`` (seconds to settle after the
            targets are reached).

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement initiate()")

    def change_sweep_step(self) -> StepPlan | None:
        """Advance the sweep index and return the next plan.

        Called by the Orchestrator in SWEEPING state.

        Returns:
            A ``StepPlan`` (``targets`` mapping plus ``wait_s``) for the next
            sweep point, or ``None`` when the sweep is complete.

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

    def standby(self) -> PhasePlan:
        """Close the data file and return the safe-parking plan.

        Called by the Orchestrator in STANDBY state after the sweep completes
        (or after an abort).

        Returns:
            A ``PhasePlan`` (``targets`` mapping, ordered ``commands`` tuple,
            and ``wait_s``) describing where to park the system.

        Raises:
            NotImplementedError: If not overridden in subclass.
        """
        raise NotImplementedError(f"{type(self).__name__} must implement standby()")

    # ------------------------------------------------------------------
    # Abort — has a working default; override to add measurement safe-off
    # ------------------------------------------------------------------

    def abort(self) -> tuple[Command, ...]:
        """Clean up after an abort: close the data file, keep partial data.

        Called by the Orchestrator on user abort and on ERROR/EMERGENCY
        entry. Unlike ``standby()`` it must not ramp anything — the
        Orchestrator holds the hardware separately via ``stop_ramps()``.

        Subclasses should extend this to also safe their measurement VI::

            def abort(self) -> tuple[Command, ...]:
                super().abort()
                return (Command("dc_measurement", "standby", {}),)

        Returns:
            An ordered ``tuple[Command, ...]`` the Orchestrator dispatches to
            safe the measurement hardware. The base implementation returns an
            empty tuple ``()``.
        """
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None
        return ()


class SweepMeasureProcedure(BaseProcedure):
    """Generic base for "sweep one axis, run any measurement method" procedures.

    This is the heart of the modular-procedure design: ONE procedure per sweep
    axis (field, temperature) that can run ANY measurement VI the station
    exposes, chosen in the GUI at build time. It owns every part that is the
    same across sweep axes:

    * **Measurement-VI selection.** ``get_param_groups()`` builds a
      "Measurement method" group whose single ``measurement_vi`` parameter is a
      ``ParamSpec(type=str, structural=True, choices=<station measurement VIs>)``
      plus a group carrying the *selected* VI's own ``measurement_parameters``.
      Because the choices depend on the station, the class declares NO static
      measurement parameters.
    * **Construction.** ``__init__`` resolves the selected VI, merges its
      parameter defaults (``BaseProcedure`` only merges class-declared params),
      and records the selection + the instance-aware live-plot keys.
    * **The four-method loop.** ``initiate`` assembles a ``DataSchema`` (sweep
      axis column + system columns + the VI's arrays and scalar columns),
      derives the DataManager ``data_config`` from it, and arms the VI.
      ``measure`` reads the VI, tags on the axis read-back, and saves (the
      schema is validated per datapoint by ``BaseProcedure._save_datapoint``).
      ``standby`` / ``abort`` disarm the VI.

    A concrete axis procedure (``FieldSweep``, ``TemperatureSweep``) subclasses
    this and supplies only the axis-specific pieces via the hooks below:
    ``_initial_system_targets`` / ``_step_targets`` / ``_standby_targets`` (the
    ramp targets), ``_axis_readback`` (the value stored in the sweep column each
    point), and ``_initiate_wait_s`` / ``_step_wait_s`` (settle times). It never
    imports a VI (contract C6): all measurement self-description is read through
    the ``Station`` instance.
    """

    # Generic sweep procedures resolve their measurement VI from the station at
    # construction, so they need a populated Station (not an empty one).
    requires_measurement_vi: bool = True

    def __init__(
        self,
        station: Station,
        sample_info: dict[str, str],
        data_directory: str,
        file_prefix: str = "",
        **param_values: Any,
    ) -> None:
        """Resolve the selected measurement VI and merge its parameter defaults.

        Args:
            station: The active Station; must expose at least one measurement VI.
            sample_info: ``{"sample_name", "sample_id", "comments"}``.
            data_directory: Base directory for the HDF5 output file.
            file_prefix: Optional filename prefix (see ``BaseProcedure``).
            **param_values: Form values. ``measurement_vi`` selects the
                measurement VI by name (defaults to the first registered one);
                the remaining keys are the sweep/system params and the selected
                VI's measurement params.

        Raises:
            CryoSoftConfigError: If the station has no measurement VIs, if
                ``measurement_vi`` names a VI that is not a measurement VI, or if
                a measurement param name collides with a sweep/system param.
        """
        names = station.measurement_vi_names()
        if not names:
            raise CryoSoftConfigError(
                f"{type(self).name!r} needs a measurement VI, but this station "
                f"has none. Add a measurement VI to the config."
            )
        selected = param_values.get("measurement_vi") or names[0]
        if selected not in names:
            raise CryoSoftConfigError(
                f"{type(self).name!r}: measurement_vi={selected!r} is not a "
                f"measurement VI on this station (have: {', '.join(names)})."
            )
        self._measurement_vi: str = selected

        # BaseProcedure merges class-declared param defaults (axis + system) and
        # builds the sweep array. It does NOT know the measurement params (those
        # are the selected VI's, resolved below).
        super().__init__(station, sample_info, data_directory, file_prefix, **param_values)

        vi = station.get_vi(selected)
        vi_specs: dict[str, ParamSpec] = vi.measurement_parameters
        collisions = sorted(
            (set(type(self).parameters) | {"measurement_vi"}) & set(vi_specs)
        )
        if collisions:
            raise CryoSoftConfigError(
                f"{type(self).name!r}: measurement VI {selected!r} parameter(s) "
                f"{collisions} collide with the procedure's own sweep/system "
                f"parameters. Rename the measurement VI parameter(s)."
            )

        # Merge the VI's parameter defaults with any caller-supplied values —
        # this is the merge BaseProcedure could not do for dynamic params.
        merged_meas: dict[str, Any] = {
            name: spec.default for name, spec in vi_specs.items()
        }
        merged_meas.update(
            {name: param_values[name] for name in vi_specs if name in param_values}
        )
        self._measurement_params: dict[str, Any] = merged_meas

        # Record the resolved measurement selection + params in the params dict
        # so the HDF5 metadata captures exactly what ran, and expose the
        # selected VI's columns as this instance's live-plot keys.
        self._params.update(merged_meas)
        self._params["measurement_vi"] = selected

        # ── Switch / multiplexing resolution ──────────────────────────────────
        # A switch VI is optional. When the station exposes one, the form carries
        # a per-route "mux_<route>" bool (see get_param_groups); here we resolve
        # which routes the user selected. No switch VI -> no mux, zero behaviour
        # change. (Multiple switch VIs are out of scope: the first is used.)
        self._switch_vi: str | None = None
        self._selected_routes: list[str] = []
        switch_names = station.switch_vi_names()
        if switch_names:
            self._switch_vi = switch_names[0]
            switch = station.get_vi(self._switch_vi)
            routes = switch.routes()
            valid_route_names = set(routes)
            # A mux_<route> naming a route the switch does not have is a config /
            # form error — fail loudly rather than silently dropping it.
            for key, value in param_values.items():
                if key.startswith("mux_") and value:
                    route_name = key[len("mux_") :]
                    if route_name not in valid_route_names:
                        raise CryoSoftConfigError(
                            f"{type(self).name!r}: selected route {route_name!r} "
                            f"is not a route of switch VI {self._switch_vi!r} "
                            f"(have: {', '.join(routes)})."
                        )
            # Merge mux defaults (all False) with the supplied selections and
            # record them so the HDF5 metadata captures the routing.
            mux_params: dict[str, Any] = {f"mux_{r}": False for r in routes}
            mux_params.update(
                {key: param_values[key] for key in mux_params if key in param_values}
            )
            self._params.update(mux_params)
            self._selected_routes = [r for r in routes if mux_params[f"mux_{r}"]]

        # Live-plot / data keys: suffix per route only when multiplexing (>=2
        # routes). 0 or 1 route keeps the plain measurement columns.
        base_keys = list(vi.measurement_data_keys) + list(vi.measurement_scalar_columns)
        if len(self._selected_routes) >= 2:
            self.measurement_data_keys = [
                f"{key}__{route}"
                for key in base_keys
                for route in self._selected_routes
            ]
        else:
            self.measurement_data_keys = base_keys

    # ------------------------------------------------------------------
    # Parameter groups: measurement-method selection + selected VI's params
    # ------------------------------------------------------------------

    @classmethod
    def get_param_groups(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> list[ParamGroup]:
        """Build the form: the static Sweep/System groups + measurement select.

        Appends two dynamic groups after the class's static groups: a
        "Measurement method" group with the structural ``measurement_vi``
        selector, and a group carrying the selected VI's own
        ``measurement_parameters``.

        Args:
            station: The active Station (supplies the measurement VIs).
            selections: Current structural-param values; ``measurement_vi`` picks
                the VI whose parameters group is shown (defaults to the first).

        Returns:
            The ordered ``ParamGroup`` list the GUI renders.

        Raises:
            CryoSoftConfigError: If the station has no measurement VIs, or a
                measurement param name collides with a procedure param.
        """
        names = station.measurement_vi_names()
        if not names:
            raise CryoSoftConfigError(
                f"{cls.name!r} needs a measurement VI, but this station has none."
            )
        selections = selections or {}
        selected = selections.get("measurement_vi")
        if selected not in names:
            selected = names[0]

        # Static Sweep/System groups (Measurement is empty for a generic proc).
        groups = super().get_param_groups(station, selections)

        # (a) The measurement-method selector. Labels carry the VI's display
        # label; the collected/mapped value is the bare VI name.
        choices: dict[str, str] = {}
        for name in names:
            label = station.measurement_label(name)
            choices[f"{name} — {label}" if label and label != name else name] = name
        select_spec = ParamSpec(
            type=str,
            default=selected,
            choices=choices,
            structural=True,
            description="Measurement method to run at each sweep point",
        )
        groups.append(
            ParamGroup(
                key="measurement_select",
                title="Measurement method",
                params={"measurement_vi": select_spec},
            )
        )

        # (b) The selected VI's own parameters.
        vi = station.get_vi(selected)
        vi_specs: dict[str, ParamSpec] = dict(vi.measurement_parameters)
        collisions = sorted(
            (set(cls.parameters) | {"measurement_vi"}) & set(vi_specs)
        )
        if collisions:
            raise CryoSoftConfigError(
                f"{cls.name!r}: measurement VI {selected!r} parameter(s) "
                f"{collisions} collide with the procedure's own parameters."
            )
        groups.append(
            ParamGroup(
                key=f"measurement:{selected}",
                title=station.measurement_label(selected),
                params=vi_specs,
            )
        )

        # (c) Multiplexing: when the station has a switch VI, one checkbox per
        # route. The "mux_" prefix keeps route names (which can collide with
        # measurement/system parameter names) in their own namespace. No switch
        # VI -> no group, zero behaviour change. (Multiple switches out of
        # scope: the first switch VI is used.)
        switch_names = station.switch_vi_names()
        if switch_names:
            switch = station.get_vi(switch_names[0])
            mux_specs = {
                f"mux_{route}": ParamSpec(
                    type=bool,
                    default=False,
                    description=f"Measure route {route}",
                )
                for route in switch.routes()
            }
            if mux_specs:
                groups.append(
                    ParamGroup(
                        key="mux",
                        title=getattr(switch, "display_label", "") or switch_names[0],
                        params=mux_specs,
                    )
                )
        return groups

    @classmethod
    def live_plot_measurement_keys(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> list[str]:
        """Return the selected measurement VI's array + scalar column names."""
        names = station.measurement_vi_names()
        if not names:
            return []
        selections = selections or {}
        selected = selections.get("measurement_vi")
        if selected not in names:
            selected = names[0]
        vi = station.get_vi(selected)
        return list(vi.measurement_data_keys) + list(vi.measurement_scalar_columns)

    # ------------------------------------------------------------------
    # Axis-specific hooks — implemented by concrete axis procedures
    # ------------------------------------------------------------------

    def _initial_system_targets(self) -> dict[str, Target]:
        """Return the system ramp targets for ``initiate()`` (first sweep point)."""
        raise NotImplementedError

    def _step_targets(self, index: int) -> dict[str, Target]:
        """Return the system ramp targets for sweep point *index*."""
        raise NotImplementedError

    def _standby_targets(self) -> dict[str, Target]:
        """Return the system ramp targets for ``standby()`` (park the system)."""
        raise NotImplementedError

    def _axis_readback(self) -> float:
        """Return the measured value of the swept quantity at the current point."""
        raise NotImplementedError

    def _initiate_wait_s(self) -> float:
        """Return the settle time (s) after the initial ramp."""
        raise NotImplementedError

    def _step_wait_s(self) -> float:
        """Return the settle time (s) after each sweep-step ramp."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared four-method loop
    # ------------------------------------------------------------------

    def _build_data_schema(self, vi: Any) -> DataSchema:
        """Assemble this run's ``DataSchema`` from the axis, station, and VI.

        Sweep columns: ``unix_time`` + every system column
        (``station.last_state_flat()``, populated by the ``get_state()`` poll in
        ``initiate()``) + the axis data-key + the VI's scalar columns. Arrays:
        the VI's ``data_arrays`` for the selected measurement params.

        Args:
            vi: The selected measurement VI instance.

        Returns:
            The declared HDF5 layout for this run.
        """
        sweep_columns: dict[str, str] = {"unix_time": "float"}
        for key in self._station.last_state_flat():
            sweep_columns[key] = "float"
        sweep_columns[type(self).sweep_axis.data_key] = "float"
        for name, dtype in vi.measurement_scalar_columns.items():
            sweep_columns[name] = dtype
        arrays = dict(vi.data_arrays(self._measurement_params))
        base = DataSchema(sweep_columns=sweep_columns, arrays=arrays)

        # Multiplex only when two or more routes are selected: every array (and
        # the VI's per-point scalar columns, e.g. n_valid) is expanded per route
        # into "<name>__<route>". With 0 or 1 route the schema is unsuffixed,
        # exactly as before Wave 6.
        if len(self._selected_routes) >= 2:
            return base.multiplexed(
                self._selected_routes,
                scalar_columns=tuple(vi.measurement_scalar_columns.keys()),
            )
        return base

    def initiate(self) -> PhasePlan:
        """Ramp to the first sweep point, arm the selected VI, open the file.

        Returns:
            A ``PhasePlan`` with the initial system ``targets``, the selected
            VI's arming ``Command``, and ``wait_s`` from ``_initiate_wait_s()``.
        """
        vi = self._station.get_vi(self._measurement_vi)
        targets = self._initial_system_targets()
        arm_command = Command(
            self._measurement_vi, "initiate", dict(self._measurement_params)
        )
        # When routes are selected, connect the FIRST route before arming the
        # measurement VI (command order is semantically meaningful). With >=2
        # routes, measure() re-selects each route per datapoint; with exactly 1
        # route this single select at initiate is the whole story.
        if self._selected_routes:
            assert self._switch_vi is not None  # set whenever routes are selected
            commands = (
                Command(
                    self._switch_vi,
                    "select_route",
                    {"route": self._selected_routes[0]},
                ),
                arm_command,
            )
        else:
            commands = (arm_command,)

        # Poll once so last_state_flat() is populated BEFORE the schema reads the
        # system columns — otherwise the columns declared here would not match
        # what measure() enriches with, and the per-point validate() would fail.
        instrument_state = self._station.get_state()
        self._data_schema = self._build_data_schema(vi)
        data_config = {
            "sweep_columns": dict(self._data_schema.sweep_columns),
            "measurement_arrays": dict(self._data_schema.arrays),
        }

        self._data_manager = DataManager(
            data_directory=self._data_directory,
            procedure_name=self.name,
            file_prefix=self._file_prefix,
            procedure_params=self._params,
            sample_info=self._sample_info,
            instrument_state=instrument_state,
            # DataManager stays dict-based (contract C7): convert the typed plan
            # to plain JSON-ready dicts at this call-site boundary only.
            system_targets={
                name: dataclasses.asdict(t) for name, t in targets.items()
            },
            measurement_commands=[dataclasses.asdict(c) for c in commands],
            data_config=data_config,
            n_sweep_points=len(self._sweep),
        )

        logger.info(
            "%s.initiate(): %d points, measurement=%s",
            type(self).__name__,
            len(self._sweep),
            self._measurement_vi,
        )
        return PhasePlan(targets=targets, commands=commands, wait_s=self._initiate_wait_s())

    def change_sweep_step(self) -> StepPlan | None:
        """Advance to the next sweep point.

        Returns:
            A ``StepPlan`` ramping to the next point with ``_step_wait_s()``
            settle, or ``None`` when the sweep is exhausted.
        """
        self._index += 1
        if self._index >= len(self._sweep):
            return None
        return StepPlan(
            targets=self._step_targets(self._index), wait_s=self._step_wait_s()
        )

    def measure(self) -> None:
        """Read the measurement VI, tag on the axis read-back, and save.

        Raises:
            RuntimeError: If called before ``initiate()``.
            DataSchemaError: If the datapoint does not match the declared schema
                (raised by ``_save_datapoint``); nothing is written.
        """
        if self._data_manager is None:
            raise RuntimeError("measure() called before initiate()")
        vi = self._station.get_vi(self._measurement_vi)

        if len(self._selected_routes) >= 2:
            # Multiplexed datapoint: connect each route in turn, take one reading
            # per route, and suffix every returned key with "__<route>". The
            # switch is reached only through the Station (contract C6).
            switch = self._station.get_vi(self._switch_vi)
            measured_data: dict = {}
            for route in self._selected_routes:
                switch.select_route(route)
                for key, value in vi.take_reading().items():
                    measured_data[f"{key}__{route}"] = value
        else:
            # 0 routes: plain path, no switch calls. 1 route: the switch was
            # already selected at initiate(), so a plain reading is correct.
            measured_data = vi.take_reading()

        measured_data[type(self).sweep_axis.data_key] = self._axis_readback()
        self._save_datapoint(measured_data)

    def standby(self) -> PhasePlan:
        """Close the data file and return the safe-parking plan.

        Returns:
            A ``PhasePlan`` with ``_standby_targets()``, disarming the selected
            measurement VI, with ``wait_s=0.0``.
        """
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None
        return PhasePlan(
            targets=self._standby_targets(),
            commands=self._safe_off_commands(),
            wait_s=0.0,
        )

    def abort(self) -> tuple[Command, ...]:
        """Close the data file and disarm the measurement VI (+ open the switch).

        Returns:
            The measurement safe-off command(s) for the Orchestrator to
            dispatch, plus a switch ``open_all`` when routes were selected.
        """
        super().abort()
        return self._safe_off_commands()

    def _safe_off_commands(self) -> tuple[Command, ...]:
        """Return the measurement standby command, plus switch open_all if muxed.

        Shared by ``standby()`` and ``abort()``: disarm the measurement VI and,
        when any route was selected, open every switch channel so no route is
        left connected.

        Returns:
            Ordered ``tuple[Command, ...]`` — the measurement ``standby`` first,
            then a switch ``open_all`` when routes were selected.
        """
        commands = [Command(self._measurement_vi, "standby", {})]
        if self._selected_routes:
            assert self._switch_vi is not None  # set whenever routes are selected
            commands.append(Command(self._switch_vi, "open_all", {}))
        return tuple(commands)
