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
#   assembly, the shared four-method loop, and the reading loop (up to two
#   generic slots, each a loopable parameter any reading-path VI advertises
#   via reading_setters — the switch's route and a source's current alike —
#   with a user-entered value list; columns are suffixed {name}__A{i}__B{j}
#   and the label->value map is stored in the HDF5 metadata); a concrete axis
#   procedure supplies only the ramp targets, the axis read-back, and the
#   settle times.
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
# last_updated: 2026-07-17
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
from cryosoft.core.gates import Gate
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

    # What kind of run this procedure execution is, recorded in the
    # Orchestrator's run manifests: "run" for a normal science run. The session
    # layer's future probe runs (a miniature pre-flight execution of the same
    # procedure) will override this with "probe" so probe data files and run
    # records are never confused with science data.
    run_kind: str = "run"

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

    @classmethod
    def live_plot_loop_labels(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> tuple[dict[str, str] | None, dict[str, str] | None]:
        """Return the label maps for the plot panels' two Loop selectors.

        The GUI gives every plot a "Loop 1" and a "Loop 2" selector that pick
        WHICH reading of a datapoint is plotted, one per reading-loop slot.
        The default (no reading loop concept) returns ``(None, None)`` — both
        selectors hidden. A procedure with a reading loop overrides this (see
        ``SweepMeasureProcedure``) to return, per slot, ``{}`` (slot off /
        static — selector visible, disabled) or an ordered
        ``{suffix_label: display_text}`` map (e.g.
        ``{"A1": "A1 = Mux-Ch1", ...}`` — selector enabled).

        Args:
            station: The active Station (unused by the default).
            selections: Current structural-parameter values, or ``None``
                (unused by the default).

        Returns:
            ``(None, None)`` by default.
        """
        _ = (station, selections)
        return (None, None)

    def __init__(
        self,
        station: Station,
        sample_info: dict[str, str],
        data_directory: str,
        file_prefix: str = "",
        experiment_info: dict[str, Any] | None = None,
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
            experiment_info: Optional experiment-level context from the session
                layer (experiment id/title, user identity, ELN link), forwarded
                verbatim to every ``DataManager`` this procedure creates and
                stored as ``/metadata/experiment_info``. ``None`` means "no
                experiment open" and is recorded as ``{}``.
            **param_values: Procedure-specific parameter values from the GUI form.
                Any declared parameter with a ``default`` that the caller omits
                is filled in automatically; caller-supplied values always win.
        """
        self._station = station
        self._sample_info = sample_info
        self._data_directory = data_directory
        self._file_prefix = file_prefix
        self._experiment_info: dict[str, Any] = dict(experiment_info or {})
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

    # ------------------------------------------------------------------
    # Public read-only surface (consumed by the Orchestrator's run manifests
    # and the GUI — no caller should ever reach into _data_manager directly)
    # ------------------------------------------------------------------

    @property
    def data_filepath(self) -> str | None:
        """Absolute path of this run's HDF5 file, or ``None`` before ``initiate()``.

        Remains available while the run's ``DataManager`` is open; after
        ``standby()``/``abort()`` close the file the property returns ``None``
        again, so consumers that need the path across the whole run (the
        Orchestrator's run manifests) must capture it at start.

        Returns:
            The file path as a string, or ``None`` when no data file is open.
        """
        if self._data_manager is None:
            return None
        return str(self._data_manager.filepath)

    @property
    def last_datapoint(self) -> dict:
        """A copy of the most recently saved datapoint, or ``{}``.

        The Orchestrator reads this after each ``measure()`` to emit
        ``measurement_ready`` for the live plot.

        Returns:
            The last saved datapoint dict (copied), or ``{}`` when no data
            file is open or nothing has been saved yet.
        """
        if self._data_manager is None or not self._data_manager.last_datapoint:
            return {}
        return dict(self._data_manager.last_datapoint)

    def get_params(self) -> dict[str, Any]:
        """Return a copy of this instance's merged parameter values.

        Declared defaults merged with caller-supplied values (and, for the
        generic sweep procedures, the resolved measurement-VI selection and its
        parameters). This is what the run manifests and HDF5 metadata record.

        Returns:
            A shallow copy of the parameter dict.
        """
        return dict(self._params)

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

    # ------------------------------------------------------------------
    # Gates — no-op defaults; override to require settling before a reading
    # ------------------------------------------------------------------

    def initiation_gates(self) -> tuple[Gate, ...]:
        """Gates that must pass once, before the run's first measurement.

        Checked by the Orchestrator after the initial targets ramp completes
        (``initiate()``'s targets), in place of ``PhasePlan.wait_s`` when
        non-empty. The base implementation declares no gates, so ``wait_s``
        governs unchanged.

        Returns:
            An ordered ``tuple[Gate, ...]``; empty by default.
        """
        return ()

    def reading_gates(self) -> tuple[Gate, ...]:
        """Gates that must pass before every measurement after the first.

        Checked by the Orchestrator after each sweep step's targets ramp
        completes (``change_sweep_step()``'s targets), in place of
        ``StepPlan.wait_s`` when non-empty. The base implementation declares
        no gates, so ``wait_s`` governs unchanged.

        Returns:
            An ordered ``tuple[Gate, ...]``; empty by default.
        """
        return ()


# Form-parameter names the generic sweep procedure owns structurally (they are
# never a measurement VI's own knobs, so a VI param may not shadow them). The
# dynamic per-choice pick checkboxes live in the "loopN_pick_" namespace.
_STRUCTURAL_PARAM_NAMES = frozenset(
    {
        "measurement_vi",
        "loop1_parameter",
        "loop1_values",
        "loop2_parameter",
        "loop2_values",
    }
)


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
      ``measure`` runs the reading loop (below), tags on the axis read-back,
      and saves (the schema is validated per datapoint by
      ``BaseProcedure._save_datapoint``). ``standby`` / ``abort`` disarm the VI.
    * **The reading loop.** One datapoint may comprise several readings,
      taken by up to TWO generic loop slots nested inside ``measure()`` (slot
      1 outer, slot 2 inner). A slot is a loopable parameter — anything a
      reading-path VI advertises via ``reading_setters``: the switch VI's
      ``route`` and a measurement VI's ``current_A`` are the same concept —
      plus an ordered value list from the auto-rendered "Reading loop" form
      group (checkboxes for an enumerated parameter, comma-separated text
      otherwise). A slot with ONE value is a static setting (dispatched once
      at ``initiate()``, no suffix); with two or more it loops: value *i* is
      measured under index label ``A{i}`` / ``B{i}``, columns compose as
      ``{name}__A{i}__B{j}``, and the label -> value map is stored in the
      HDF5 metadata (``procedure_params["loop_labels"]``). All per-reading
      setup is dispatched as ``Command``s through the Station, never by
      direct VI calls; participating non-measurement VIs get their
      ``reading_safe_off`` (e.g. the switch's ``open_all``) at
      standby/abort.

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
        experiment_info: dict[str, Any] | None = None,
        **param_values: Any,
    ) -> None:
        """Resolve the selected measurement VI and merge its parameter defaults.

        Args:
            station: The active Station; must expose at least one measurement VI.
            sample_info: ``{"sample_name", "sample_id", "comments"}``.
            data_directory: Base directory for the HDF5 output file.
            file_prefix: Optional filename prefix (see ``BaseProcedure``).
            experiment_info: Optional session-layer experiment context (see
                ``BaseProcedure``), forwarded to the ``DataManager``.
            **param_values: Form values. ``measurement_vi`` selects the
                measurement VI by name (defaults to the first registered one);
                ``loop_parameter`` / ``loop_values`` define the optional
                reading loop (a loopable parameter of the selected VI and its
                comma-separated value list); the remaining keys are the
                sweep/system params and the selected VI's measurement params.

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
        super().__init__(
            station,
            sample_info,
            data_directory,
            file_prefix,
            experiment_info,
            **param_values,
        )

        vi = station.get_vi(selected)
        vi_specs: dict[str, ParamSpec] = vi.measurement_parameters
        collisions = sorted(
            (set(type(self).parameters) | _STRUCTURAL_PARAM_NAMES) & set(vi_specs)
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

        # ── Reading-loop resolution: up to two generic slots ─────────────────
        # Every loop level is the SAME thing: a loopable parameter (advertised
        # by a VI's reading_setters — the switch's route and a source's
        # current alike) plus an ordered value list. Slot 1 (labels A1, A2, …)
        # is the outer level, slot 2 (labels B1, B2, …) the inner one. A slot
        # with a single value is a STATIC setting (its setter is dispatched
        # once at initiate, no loop, no suffix); with two or more values it
        # loops and suffixes the columns. Resolved and validated here so a bad
        # selection fails at the procedure boundary, never in the tick loop.
        registry = self._loopable_registry(station, selected)
        self._loop_slots: list[dict[str, Any]] = []
        used_qualified: set[str] = set()
        for slot, prefix in (("loop1", "A"), ("loop2", "B")):
            qualified = str(param_values.get(f"{slot}_parameter") or "")
            self._params[f"{slot}_parameter"] = qualified
            if not qualified:
                self._params[f"{slot}_values"] = []
                continue
            entry = registry.get(qualified)
            if entry is None:
                raise CryoSoftConfigError(
                    f"{type(self).name!r}: {slot}_parameter={qualified!r} is "
                    f"not a loopable parameter of this station "
                    f"(loopable: {', '.join(registry) or 'none'})."
                )
            if qualified in used_qualified:
                raise CryoSoftConfigError(
                    f"{type(self).name!r}: {qualified!r} is selected in both "
                    f"loop slots; each parameter may be looped once."
                )
            used_qualified.add(qualified)
            slot_vi_name, param_name, spec, setter = entry
            if spec.choices is not None:
                # Enumerated parameter: values come from the pick checkboxes,
                # kept in choices order. An unknown pick is a form/config
                # error — fail loudly rather than silently dropping it.
                valid_picks = {
                    f"{slot}_pick_{value}" for value in spec.choices.values()
                }
                for key, value in param_values.items():
                    if key.startswith(f"{slot}_pick_") and value and key not in valid_picks:
                        raise CryoSoftConfigError(
                            f"{type(self).name!r}: {key!r} does not name a "
                            f"choice of {qualified!r} "
                            f"(have: {', '.join(map(str, spec.choices.values()))})."
                        )
                values = [
                    value
                    for value in spec.choices.values()
                    if param_values.get(f"{slot}_pick_{value}")
                ]
                self._params.update(
                    {
                        f"{slot}_pick_{value}": (value in values)
                        for value in spec.choices.values()
                    }
                )
            else:
                values = self._parse_loop_values(
                    param_name, spec, str(param_values.get(f"{slot}_values") or "")
                )
            if not values:
                raise CryoSoftConfigError(
                    f"{type(self).name!r}: the reading loop over {qualified!r} "
                    f"has no values selected."
                )
            labels = (
                [f"{prefix}{i}" for i in range(1, len(values) + 1)]
                if len(values) >= 2
                else []
            )
            self._loop_slots.append(
                {
                    "qualified": qualified,
                    "vi_name": slot_vi_name,
                    "param": param_name,
                    "setter": setter,
                    "values": values,
                    "labels": labels,
                }
            )
            self._params[f"{slot}_values"] = list(values)

        # The levels that actually loop (>=2 values), in slot order.
        self._loop_levels: list[dict[str, Any]] = [
            s for s in self._loop_slots if s["labels"]
        ]
        # Label -> value map for the HDF5 metadata (procedure_params).
        self._params["loop_labels"] = {
            label: value
            for level in self._loop_levels
            for label, value in zip(level["labels"], level["values"])
        }

        # Live-plot / data keys: each looping level suffixes the measurement
        # columns with its index labels, slot 1 (outer) first:
        # {key}__A{i}__B{j}. No looping level keeps the plain columns.
        keys = list(vi.measurement_data_keys) + list(vi.measurement_scalar_columns)
        for level in self._loop_levels:
            keys = [f"{key}__{label}" for key in keys for label in level["labels"]]
        self.measurement_data_keys = keys

    # ------------------------------------------------------------------
    # Reading-loop plumbing (shared by __init__, the form, and the plots)
    # ------------------------------------------------------------------

    @staticmethod
    def _loopable_registry(
        station: Station, measurement_vi_name: str
    ) -> dict[str, tuple[str, str, ParamSpec, str]]:
        """Collect every loopable parameter the station's reading path offers.

        A loopable parameter is any ``reading_setters`` entry of a VI in the
        reading path: the station's switch VI (when the scanner is enabled —
        its ``route`` is a loopable parameter like any other) and the selected
        measurement VI. Switch parameters come first, matching the typical
        outer-level use. A setup with no switch and a measurement VI without
        setters yields an empty registry — no Reading loop group, no loop.

        Args:
            station: The active Station.
            measurement_vi_name: The selected measurement VI's name.

        Returns:
            Ordered ``{"vi_name.param": (vi_name, param, spec, setter)}``.
        """
        switch_names = station.switch_vi_names() if station.scanner_enabled() else []
        vi_names = ([switch_names[0]] if switch_names else []) + [measurement_vi_name]
        registry: dict[str, tuple[str, str, ParamSpec, str]] = {}
        for vi_name in vi_names:
            vi = station.get_vi(vi_name)
            specs = vi.reading_parameters
            for param_name, setter in vi.reading_setters.items():
                spec = specs.get(param_name)
                if spec is None:  # e.g. a switch configured with no routes
                    continue
                registry[f"{vi_name}.{param_name}"] = (
                    vi_name,
                    param_name,
                    spec,
                    setter,
                )
        return registry

    @staticmethod
    def _parse_loop_values(param_name: str, spec: ParamSpec, text: str) -> list:
        """Parse a comma-separated loop-value list against a parameter's spec.

        Each entry is converted with the spec's ``type`` and checked against
        its ``min``/``max`` bounds or ``choices``, so a loop value obeys
        exactly the constraints the single-value form field would enforce.

        Args:
            param_name: The looped parameter's name (for error messages).
            spec: The looped parameter's ``ParamSpec``.
            text: The user-entered comma-separated list (e.g. ``"1e-6, -1e-6"``).

        Returns:
            The parsed values, in entry order (empty for blank *text*).

        Raises:
            CryoSoftConfigError: If the parameter is a bool (not loopable), an
                entry does not parse as the spec's type, or a parsed value
                violates the spec's bounds/choices.
        """
        if spec.type is bool:
            raise CryoSoftConfigError(
                f"reading loop: parameter {param_name!r} is a bool and cannot "
                f"be looped over a value list."
            )
        values: list = []
        for entry in (e.strip() for e in text.split(",")):
            if not entry:
                continue
            try:
                value = spec.type(entry)
            except (TypeError, ValueError) as exc:
                raise CryoSoftConfigError(
                    f"reading loop: {entry!r} is not a valid "
                    f"{spec.type.__name__} for parameter {param_name!r}."
                ) from exc
            if spec.choices is not None and value not in spec.choices.values():
                raise CryoSoftConfigError(
                    f"reading loop: {value!r} is not one of {param_name!r}'s "
                    f"allowed choices {list(spec.choices.values())}."
                )
            if spec.min is not None and value < spec.min:
                raise CryoSoftConfigError(
                    f"reading loop: {value!r} is below {param_name!r}'s "
                    f"minimum {spec.min!r}."
                )
            if spec.max is not None and value > spec.max:
                raise CryoSoftConfigError(
                    f"reading loop: {value!r} is above {param_name!r}'s "
                    f"maximum {spec.max!r}."
                )
            values.append(value)
        return values

    # ------------------------------------------------------------------
    # Parameter groups: measurement-method selection + selected VI's params
    # ------------------------------------------------------------------

    @classmethod
    def get_param_groups(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> list[ParamGroup]:
        """Build the form: the static Sweep/System groups + measurement select.

        Appends the dynamic groups after the class's static groups: a
        "Measurement method" group with the structural ``measurement_vi``
        selector, a single "Reading loop" group holding the two generic loop
        slots (each a ``{slot}_parameter`` drop-down over every loopable
        parameter the station's reading path advertises, plus that
        parameter's values input — per-choice ``{slot}_pick_{value}``
        checkboxes when enumerated, a ``{slot}_values`` text field otherwise),
        and a group carrying the selected VI's own ``measurement_parameters``.
        Nothing loopable on this station means no Reading loop group at all.

        Args:
            station: The active Station (supplies the measurement VIs).
            selections: Current structural-param values; ``measurement_vi`` picks
                the VI whose parameters group is shown (defaults to the first),
                the ``loopN_*`` values carry the reading loop.

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

        # (a) The measurement-method selector. Labels are the VI's SHORT
        # selector_label (keeps the drop-down / column narrow); the
        # collected/mapped value is the bare VI name. The GUI carries the
        # vi_name in a per-item tooltip for disambiguation when two VIs share a
        # label. Falls back to the VI name if two selector labels collide (a
        # dict key would otherwise be lost).
        choices: dict[str, str] = {}
        for name in names:
            label = station.measurement_selector_label(name)
            key = label if label and label not in choices else name
            choices[key] = name
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
        vi = station.get_vi(selected)

        # (b) The reading loop, rendered ABOVE the VI's own parameter group.
        # Two generic slots, each a loopable parameter (anything the station's
        # reading path advertises via reading_setters — the switch's route and
        # the measurement VI's parameters alike) plus its values input. The
        # values input is spec-driven: an enumerated parameter renders one
        # checkbox per choice ("{slot}_pick_{value}"), a free parameter a
        # comma-separated text field ("{slot}_values"). Everything here is
        # structural — changing any of it changes which columns the run
        # produces. Nothing loopable on this station -> no group, zero change.
        registry = cls._loopable_registry(station, selected)
        if registry:
            loop_group_params: dict[str, ParamSpec] = {}
            slot_choices: dict[str, str] = {"Off": ""}
            for qualified, (slot_vi, param_name, spec, _setter) in registry.items():
                display = (
                    f"{param_name} ({spec.unit})"
                    if spec.unit
                    else f"{param_name} ({slot_vi})"
                )
                if display in slot_choices:
                    display = qualified
                slot_choices[display] = qualified
            for slot, ordinal in (("loop1", "1"), ("loop2", "2")):
                selected_slot = selections.get(f"{slot}_parameter")
                if selected_slot not in registry:
                    selected_slot = ""
                loop_group_params[f"{slot}_parameter"] = ParamSpec(
                    type=str,
                    default=selected_slot,
                    choices=dict(slot_choices),
                    structural=True,
                    description=(
                        f"Loop {ordinal} parameter, repeated at every sweep "
                        f"point (slot 1 is the outer level)"
                    ),
                )
                if not selected_slot:
                    continue
                _vi_name, param_name, spec, _setter = registry[selected_slot]
                if spec.choices is not None:
                    for choice_label, value in spec.choices.items():
                        loop_group_params[f"{slot}_pick_{value}"] = ParamSpec(
                            type=bool,
                            default=bool(selections.get(f"{slot}_pick_{value}")),
                            structural=True,
                            description=f"Include {choice_label}",
                        )
                else:
                    loop_group_params[f"{slot}_values"] = ParamSpec(
                        type=str,
                        default=str(selections.get(f"{slot}_values") or ""),
                        structural=True,
                        widget_hint="array",
                        description=(
                            "Comma-separated values; one value sets it once, "
                            "two or more loop it at every sweep point"
                        ),
                    )
            groups.append(
                ParamGroup(
                    key="reading_loop",
                    title="Reading loop",
                    params=loop_group_params,
                )
            )

        # (c) The selected VI's own parameters.
        vi_specs: dict[str, ParamSpec] = dict(vi.measurement_parameters)
        collisions = sorted(
            (set(cls.parameters) | _STRUCTURAL_PARAM_NAMES) & set(vi_specs)
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

        return groups

    @classmethod
    def live_plot_measurement_keys(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> list[str]:
        """Return the selected measurement VI's array + scalar column names.

        Keys stay the PLAIN column names even when a reading loop is defined:
        the per-plot Loop and Route selectors (see ``live_plot_loop_labels``)
        pick which reading of the datapoint is drawn, and the panel composes
        the ``{key}__L{i}__{route}`` suffixes at draw time.
        """
        names = station.measurement_vi_names()
        if not names:
            return []
        selections = selections or {}
        selected = selections.get("measurement_vi")
        if selected not in names:
            selected = names[0]
        vi = station.get_vi(selected)
        return list(vi.measurement_data_keys) + list(vi.measurement_scalar_columns)

    @classmethod
    def live_plot_loop_labels(
        cls, station: Station, selections: Mapping[str, Any] | None = None
    ) -> tuple[dict[str, str] | None, dict[str, str] | None]:
        """Return the two Loop-selector label maps for the current selections.

        One entry per loop slot, in slot order. ``(None, None)`` when the
        station's reading path offers nothing loopable (selectors hidden).
        Per slot: ``{}`` when the slot is off, static (one value), or its
        values are empty/invalid (selector visible, disabled; construction
        refuses a bad list loudly); otherwise an ordered ``{label: display}``
        map like ``{"A1": "A1 = Mux-Ch1", ...}`` / ``{"B1": "B1 = 1e-06",
        ...}``. All loop params are ``structural=True`` precisely so their
        values reach ``selections`` and their changes re-derive the selectors.
        """
        names = station.measurement_vi_names()
        if not names:
            return (None, None)
        selections = selections or {}
        selected = selections.get("measurement_vi")
        if selected not in names:
            selected = names[0]
        registry = cls._loopable_registry(station, selected)
        if not registry:
            return (None, None)

        maps: list[dict[str, str]] = []
        for slot, prefix in (("loop1", "A"), ("loop2", "B")):
            qualified = selections.get(f"{slot}_parameter") or ""
            entry = registry.get(qualified)
            if entry is None:
                maps.append({})
                continue
            _vi_name, param_name, spec, _setter = entry
            if spec.choices is not None:
                values = [
                    value
                    for value in spec.choices.values()
                    if selections.get(f"{slot}_pick_{value}")
                ]
            else:
                try:
                    values = cls._parse_loop_values(
                        param_name,
                        spec,
                        str(selections.get(f"{slot}_values") or ""),
                    )
                except CryoSoftConfigError:
                    values = []
            if len(values) < 2:
                maps.append({})
                continue
            maps.append(
                {
                    f"{prefix}{i}": (
                        f"{prefix}{i} = {value:g}"
                        if isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        else f"{prefix}{i} = {value}"
                    )
                    for i, value in enumerate(values, start=1)
                }
            )
        return (maps[0], maps[1])

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

        # The reading loop expands the schema once per looping level, slot 1
        # (outer) first, so suffixes compose as "<name>__A<i>__B<j>". Each
        # level expands every measurement array and the VI's per-point scalar
        # columns (e.g. n_valid). A slot that is off or static leaves the
        # schema untouched, exactly as before that level existed.
        scalar_names = tuple(vi.measurement_scalar_columns.keys())
        for level in self._loop_levels:
            base = base.multiplexed(level["labels"], scalar_columns=scalar_names)
            scalar_names = tuple(
                f"{name}__{label}"
                for name in scalar_names
                for label in level["labels"]
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
            self._measurement_vi, "initiate_measurement", dict(self._measurement_params)
        )
        # Loop-slot setup around the arm (command order is semantically
        # meaningful). Slots on OTHER VIs (e.g. the switch's route) dispatch
        # their first value BEFORE the measurement VI arms, so arming happens
        # with the channel connected — with a static (1-value) slot that
        # single dispatch is the whole story, with a looping slot measure()
        # re-dispatches per reading. Static slots on the measurement VI itself
        # dispatch AFTER the arm (their setters require an armed instrument);
        # looping measurement-VI slots need nothing here because measure()
        # sets the value before every reading.
        pre_arm: list[Command] = []
        post_arm: list[Command] = []
        for slot in self._loop_slots:
            command = Command(
                slot["vi_name"], slot["setter"], {slot["param"]: slot["values"][0]}
            )
            if slot["vi_name"] != self._measurement_vi:
                pre_arm.append(command)
            elif not slot["labels"]:
                post_arm.append(command)
        commands = (*pre_arm, arm_command, *post_arm)

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
            experiment_info=self._experiment_info,
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
        """Run the reading loop, tag on the axis read-back, and save.

        The reading loop takes every reading of one datapoint by nesting the
        looping slots: slot 1 (outer) x slot 2 (inner). Before each reading it
        dispatches the level's setter (as a ``Command`` through the Station —
        the same channel ``initiate()`` and the Orchestrator use, never a
        direct call on another VI) and suffixes the returned columns with the
        level's index labels, composing as ``{name}__A{i}__B{j}``. A slot
        that is off or static contributes no level, so with no looping slots
        this is a single plain ``take_reading()``.

        Raises:
            RuntimeError: If called before ``initiate()``.
            DataSchemaError: If the datapoint does not match the declared schema
                (raised by ``_save_datapoint``); nothing is written.
        """
        if self._data_manager is None:
            raise RuntimeError("measure() called before initiate()")
        vi = self._station.get_vi(self._measurement_vi)
        measured_data: dict = {}

        def _dispatch(level: dict[str, Any], value: Any) -> None:
            self._station.send_measurement_commands(
                (Command(level["vi_name"], level["setter"], {level["param"]: value}),)
            )

        def _read(suffix: str) -> None:
            for key, value in vi.take_reading().items():
                measured_data[f"{key}{suffix}"] = value

        levels = self._loop_levels
        if not levels:
            _read("")
        else:
            outer = levels[0]
            inner = levels[1] if len(levels) > 1 else None
            for outer_label, outer_value in zip(outer["labels"], outer["values"]):
                _dispatch(outer, outer_value)
                if inner is None:
                    _read(f"__{outer_label}")
                else:
                    for inner_label, inner_value in zip(
                        inner["labels"], inner["values"]
                    ):
                        _dispatch(inner, inner_value)
                        _read(f"__{outer_label}__{inner_label}")

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
        """Close the data file and disarm the measurement VI (+ loop safe-offs).

        Returns:
            The measurement safe-off command(s) for the Orchestrator to
            dispatch, plus each participating loop VI's ``reading_safe_off``.
        """
        super().abort()
        return self._safe_off_commands()

    def _safe_off_commands(self) -> tuple[Command, ...]:
        """Return the measurement standby command plus the loop VIs' safe-offs.

        Shared by ``standby()`` and ``abort()``: disarm the measurement VI
        and, for every OTHER VI that took part in a loop slot (static or
        looping) and declares a ``reading_safe_off`` method (e.g. the switch's
        ``open_all``), dispatch it so nothing is left connected.

        Returns:
            Ordered ``tuple[Command, ...]`` — the measurement ``standby``
            first, then one safe-off per participating loop VI.
        """
        commands = [Command(self._measurement_vi, "standby", {})]
        seen: set[str] = set()
        for slot in self._loop_slots:
            slot_vi = slot["vi_name"]
            if slot_vi == self._measurement_vi or slot_vi in seen:
                continue
            seen.add(slot_vi)
            safe_off = self._station.get_vi(slot_vi).reading_safe_off
            if safe_off:
                commands.append(Command(slot_vi, safe_off, {}))
        return tuple(commands)
        if self._data_manager is not None:
            self._data_manager.close()
            self._data_manager = None
        return PhasePlan(
            targets=self._standby_targets(),
            commands=self._safe_off_commands(),
            wait_s=0.0,
        )

    def abort(self) -> tuple[Command, ...]:
        """Close the data file and disarm the measurement VI (+ loop safe-offs).

        Returns:
            The measurement safe-off command(s) for the Orchestrator to
            dispatch, plus each participating loop VI's ``reading_safe_off``.
        """
        super().abort()
        return self._safe_off_commands()

    def _safe_off_commands(self) -> tuple[Command, ...]:
        """Return the measurement standby command plus the loop VIs' safe-offs.

        Shared by ``standby()`` and ``abort()``: disarm the measurement VI
        and, for every OTHER VI that took part in a loop slot (static or
        looping) and declares a ``reading_safe_off`` method (e.g. the switch's
        ``open_all``), dispatch it so nothing is left connected.

        Returns:
            Ordered ``tuple[Command, ...]`` — the measurement ``standby``
            first, then one safe-off per participating loop VI.
        """
        commands = [Command(self._measurement_vi, "standby", {})]
        seen: set[str] = set()
        for slot in self._loop_slots:
            slot_vi = slot["vi_name"]
            if slot_vi == self._measurement_vi or slot_vi in seen:
                continue
            seen.add(slot_vi)
            safe_off = self._station.get_vi(slot_vi).reading_safe_off
            if safe_off:
                commands.append(Command(slot_vi, safe_off, {}))
        return tuple(commands)
