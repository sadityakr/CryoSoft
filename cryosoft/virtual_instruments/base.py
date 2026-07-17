# ---
# description: |
#   BaseVirtualInstrument: the root class for all CryoSoft Virtual Instruments.
#   Provides __init_subclass__ auto-wrapping of @monitored/@control methods with
#   structured logging AND declarative limit enforcement (the control-validation
#   standard: control_limits class attr + self._limits populated from config
#   init_params; out-of-range @control calls raise CryoSoftSafetyError before
#   any hardware command). Also get_state() auto-build, evaluate_safety() hook,
#   and typed sub-bases for each VI category (MagnetBase,
#   TemperatureControllerBase, LevelMeterBase, MeasurementInstrumentBase,
#   DCMeasurementBase). MeasurementInstrumentBase also defines the
#   self-describing measurement-method standard (measurement_parameters /
#   measurement_data_keys / measurement_scalar_columns class attrs plus the
#   data_arrays / initiate / take_reading / standby lifecycle and the optional
#   reading_variants reading-loop hook).
# entry_point: Not run directly; imported by all concrete VI modules.
# dependencies:
#   - cryosoft.core.exceptions
#   - cryosoft.core.decorators
#   - cryosoft.core.plan (ParamSpec, ReadingVariant)
# input: |
#   Subclasses receive drivers dict and arbitrary init_params from Station
#   factory. All @monitored and @control methods are auto-discovered.
# process: |
#   __init_subclass__ iterates vars(cls) to wrap only the new methods defined
#   in each subclass, preserving attributes. get_state() calls all @monitored
#   methods via get_monitored_methods() helper and returns a flat dict.
# output: |
#   Logged method calls (DEBUG/ERROR), structured state dicts from get_state().
# last_updated: 2026-07-17
# ---

"""BaseVirtualInstrument and category base classes.

All VIs inherit from BaseVirtualInstrument (and possibly one of the typed
sub-bases: MagnetBase, TemperatureControllerBase, LevelMeterBase,
MeasurementInstrumentBase).

Do NOT import from Station, Orchestrator, or Procedure here.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Mapping
from typing import Any, ClassVar

from cryosoft.core.exceptions import (
    CryoSoftCommunicationError,
    CryoSoftConfigError,
    CryoSoftSafetyError,
)
from cryosoft.core.plan import ParamSpec, ReadingVariant


class BaseVirtualInstrument:
    """Root class for all CryoSoft Virtual Instruments.

    Subclass contract
    -----------------
    * Override ``initiate()`` to open communication and put the instrument in a
      known state.
    * Override ``standby()`` to put the instrument in a safe idle state.
    * Tag read-only polling methods with ``@monitored``.
    * Tag user-callable action methods with ``@control``.
    * The constructor signature MUST be ``__init__(self, drivers, **init_params)``.

    Control-validation standard
    ---------------------------
    Every numeric ``@control`` parameter with a physical bound MUST be covered
    by the declarative limits mechanism:

    1. Declare ``control_limits`` on the class, mapping method name →
       ``{parameter_name: limit_name}``.
    2. In ``__init__``, populate ``self._limits[limit_name] = (lo, hi)`` from
       ``init_params`` (setup-specific values from the config YAML; ``None``
       means unbounded on that side). A limit may be derived (e.g. max field
       from max current), but the *values* always originate from the config,
       because limits are properties of the setup, not the code.
    3. Enforcement is inherited: ``__init_subclass__`` wraps every ``@control``
       method so an out-of-range value raises ``CryoSoftSafetyError`` with the
       reason BEFORE any hardware command is sent. A declared limit_name that
       was never populated raises ``CryoSoftConfigError`` (loud standard
       violation, caught by the conformance tests).

    Rules that cannot be expressed as a numeric range (e.g. "never energise
    the switch heater across a PSU/coil current mismatch") are written as
    explicit checks at the top of the ``@control`` method, raising
    ``CryoSoftSafetyError`` with a human-readable reason.

    Subclasses that ADD limits must merge, not replace::

        control_limits = {**ParentVI.control_limits, "set_x": {"x": "x_lim"}}
    """

    vi_type: str = "unknown"
    # vi_name is set by the Station factory after instantiation, not in __init__.
    vi_name: str = ""

    # Declarative control limits: {method_name: {param_name: limit_name}}.
    # See "Control-validation standard" in the class docstring.
    control_limits: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # __init_subclass__: auto-wrap @monitored / @control methods
    # ------------------------------------------------------------------

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Wrap every @monitored and @control method defined on *cls* with logging.

        Only methods defined directly on *cls* (via ``vars(cls)``) are wrapped,
        so that inherited wrappers are not double-wrapped.
        """
        super().__init_subclass__(**kwargs)
        for attr_name, attr_value in vars(cls).items():
            if not callable(attr_value):
                continue
            is_monitored = getattr(attr_value, "_is_monitored", False)
            is_control = getattr(attr_value, "_is_control", False)
            if is_monitored or is_control:
                if is_control:
                    # Innermost wrapper: declarative limit enforcement (the
                    # control-validation standard). Composed inside the
                    # logging wrapper so rejections are also logged.
                    attr_value = BaseVirtualInstrument._make_limit_wrapper(
                        attr_value, attr_name
                    )
                wrapped = BaseVirtualInstrument._make_logging_wrapper(attr_value, attr_name)
                # Preserve the marker attributes so discovery still works
                if is_monitored:
                    wrapped._is_monitored = True
                    wrapped._display_name = getattr(attr_value, "_display_name", attr_name)
                if is_control:
                    wrapped._is_control = True
                    wrapped._display_name = getattr(attr_value, "_display_name", attr_name)
                    wrapped._control_params = getattr(attr_value, "_control_params", {})
                setattr(cls, attr_name, wrapped)

    # ------------------------------------------------------------------
    # Limit-enforcement wrapper factory (control-validation standard)
    # ------------------------------------------------------------------

    @staticmethod
    def _make_limit_wrapper(method, method_name: str):
        """Return *method* guarded by the class's declarative control limits.

        Looks up ``type(self).control_limits`` at call time, so a subclass can
        declare limits for methods it inherits without re-wrapping them.
        """
        sig = inspect.signature(method)

        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            limit_spec = type(self).control_limits.get(method_name)
            if limit_spec:
                bound = sig.bind(self, *args, **kwargs)
                bound.apply_defaults()
                for param_name, limit_name in limit_spec.items():
                    if param_name not in bound.arguments:
                        continue
                    if limit_name not in getattr(self, "_limits", {}):
                        raise CryoSoftConfigError(
                            f"{type(self).__name__}.{method_name}: "
                            f"control_limits references limit '{limit_name}' "
                            f"but __init__ never populated self._limits with "
                            f"it — define it from init_params."
                        )
                    lo, hi = self._limits[limit_name]
                    value = float(bound.arguments[param_name])
                    if (lo is not None and value < lo) or (
                        hi is not None and value > hi
                    ):
                        lo_txt = "-inf" if lo is None else f"{lo:g}"
                        hi_txt = "+inf" if hi is None else f"{hi:g}"
                        raise CryoSoftSafetyError(
                            f"{self.vi_name or type(self).__name__}."
                            f"{method_name}: {param_name}={value:g} is outside "
                            f"the allowed range [{lo_txt}, {hi_txt}] for this "
                            f"setup (limit '{limit_name}' from the station "
                            f"config). Command refused."
                        )
            return method(self, *args, **kwargs)

        return wrapper

    # ------------------------------------------------------------------
    # Logging wrapper factory
    # ------------------------------------------------------------------

    @staticmethod
    def _make_logging_wrapper(method, method_name: str):
        """Return a logging-instrumented version of *method*."""

        @functools.wraps(method)
        def wrapper(self, *args, **kwargs):
            log = logging.getLogger(f"cryosoft.vi.{self.vi_name}")
            log.debug("%s.%s(%s, %s)", self.vi_name, method_name, args, kwargs)
            try:
                result = method(self, *args, **kwargs)
                log.debug("%s.%s -> %r", self.vi_name, method_name, result)
                return result
            except Exception as exc:
                log.error(
                    "%s.%s raised %s: %s",
                    self.vi_name,
                    method_name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                # Wrap pyvisa VisaIOError → CryoSoftCommunicationError
                try:
                    import pyvisa  # type: ignore
                    if isinstance(exc, pyvisa.errors.VisaIOError):
                        raise CryoSoftCommunicationError(
                            str(exc), vi_name=self.vi_name, original_error=exc
                        ) from exc
                except ImportError:
                    pass
                raise

        return wrapper

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        """Initialise the VI.

        Args:
            drivers: Mapping of role → driver instance.
                Single-driver VIs use ``{"main": driver}``.
                Multi-driver VIs use e.g. ``{"source": k6221, "meter": k2182a}``.
            **init_params: Additional parameters from YAML config (ramp rates,
                conversion factors, safety limits, …).
        """
        self._drivers = drivers
        self._init_params = init_params
        # Numeric control limits: {limit_name: (lo, hi)}, populated by each
        # VI's __init__ from init_params (None = unbounded on that side).
        # Referenced by name from the class's control_limits declaration.
        self._limits: dict[str, tuple[float | None, float | None]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Establish communication and put the instrument in a known state.

        Override in subclasses to send initialisation commands.
        """

    def standby(self) -> None:
        """Put the instrument in a safe idle state.

        Override in subclasses to send safe-idle commands (e.g. disable outputs).
        """

    # ------------------------------------------------------------------
    # State snapshot
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        """Return {method_name: value} for all @monitored methods."""
        from cryosoft.core.decorators import get_monitored_methods

        state: dict = {}
        for method_name in get_monitored_methods(self):
            state[method_name] = getattr(self, method_name)()
        return state

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------

    def evaluate_safety(self, state: dict) -> dict[str, bool]:
        """Judge this VI's own polled state for safety conditions.

        Called by ``Station.check_safety()`` every monitor tick with the
        fragment of the snapshot belonging to this VI. Must NOT poll
        hardware — decide from *state* (and internal buffers filled during
        the poll). Any flag returned True escalates the Orchestrator to
        EMERGENCY, so only report conditions that warrant a full shutdown.

        Args:
            state: This VI's slice of the get_state() snapshot,
                ``{monitored_method_name: value, ...}``.

        Returns:
            ``{flag_name: bool}`` — e.g. ``{"quench": True}``. Empty dict
            (the default) means this VI declares no safety conditions.
        """
        _ = state
        return {}


# ── Typed category bases ──────────────────────────────────────────────────────
# Directive §"Common Mistakes": all category bases live in base.py.

class MagnetBase(BaseVirtualInstrument):
    """Base class for all magnet-type VIs."""
    vi_type: str = "magnet"
    # Human label + unit for this VI's ramp setpoint, read centrally (via the
    # Station) to render concise procedure status lines such as
    # "Ramping field to -1 T". Declared once per instrument category so every
    # magnet VI, procedure, and config inherits it — no per-procedure code.
    setpoint_label: str = "field"
    setpoint_unit: str = "T"
    display_label: str = "magnet"


class TemperatureControllerBase(BaseVirtualInstrument):
    """Base class for all temperature-controller VIs."""
    vi_type: str = "temperature"
    setpoint_label: str = "temperature"
    setpoint_unit: str = "K"
    display_label: str = "temperature"


class LevelMeterBase(BaseVirtualInstrument):
    """Base class for all cryogen-level-meter VIs."""
    vi_type: str = "level"


class MeasurementInstrumentBase(BaseVirtualInstrument):
    """Base class and self-describing standard for all measurement-method VIs.

    A *measurement method* (see GLOSSARY.md) is a measurement VI that describes
    its own GUI knobs and output shape and implements one uniform lifecycle, so
    a generic procedure can run any of them without knowing which instrument or
    protocol is behind it. Every concrete measurement VI MUST honour this
    standard; ``tests/test_conformance.py`` enforces it the moment the file
    exists.

    Self-description (class attributes)
    -----------------------------------
    * ``selector_label: ClassVar[str]`` — the SHORT human name shown in the GUI
      method-selection drop-down (e.g. "Delta mode (6221 + 2182A)"). Optional:
      when empty, the drop-down falls back to ``display_label``. Keep it terse —
      the combo's width tracks its longest label. This is distinct from
      ``display_label``, which is the longer status-line label ("delta-mode
      resistance") and is unchanged by this attribute. ``tests/test_conformance``
      checks it is a ``str``.
    * ``measurement_parameters: ClassVar[dict[str, ParamSpec]]`` — the VI's
      GUI-facing knobs, one ``ParamSpec`` per parameter. This is the single
      owner of those specs (procedures will stop duplicating them in a later
      wave). Must be non-empty on a concrete VI.
    * ``measurement_data_keys: ClassVar[list[str]]`` — the array names
      ``take_reading()`` returns (e.g. ``["voltage_V", "current_A"]``). Must be
      non-empty on a concrete VI.
    * ``measurement_scalar_columns: ClassVar[dict[str, str]]`` — optional extra
      *per-point scalar* columns the VI contributes, mapping name → dtype
      ("float" or "int"). Use this for a VI whose instrument can legitimately
      return fewer readings than requested: it pads the arrays to the declared
      length with ``float("nan")`` and reports the true count via a scalar such
      as ``n_valid``. Empty (the default) means the VI adds no scalars.

    Uniform lifecycle (methods)
    ---------------------------
    * ``data_arrays(params) -> dict[str, int]`` — declared array name → its
      per-point length, computed from the SAME ``params`` mapping ``initiate()``
      will receive. Lets a procedure size its HDF5 layout before arming the
      hardware. Base raises ``NotImplementedError``.
    * ``initiate(**params) -> None`` — arm / configure the hardware. Accepts the
      ``measurement_parameters`` keys as keyword arguments, each with a default
      (so ``initiate()`` with no arguments is valid, e.g. for a bulk
      Initiate-All). Concrete VIs keep the ``@control`` decoration where the VI
      exposes arming to the GUI.
    * ``take_reading() -> dict[str, list[float]]`` — take ONE datapoint. Takes
      NO arguments: everything it needs was fixed at ``initiate()``. It MUST
      return exactly ``measurement_data_keys`` (plus every
      ``measurement_scalar_columns`` key) and each array MUST have exactly the
      length ``data_arrays(params)`` declared for the same ``params`` — always.
      A VI whose instrument may return fewer points pads with ``float("nan")``
      to the declared length and reports the true count in its scalar column.
      This fixed-shape guarantee is the contract that prevents HDF5 layout
      mismatches mid-run.
    * ``standby() -> None`` — put the instrument in a safe-off idle state.
    * ``reading_variants(vi_name, params) -> tuple[ReadingVariant, ...]`` — the
      OPTIONAL reading-loop hook (see below). The base returns ``()``: one
      plain reading per sweep point, no behaviour change.

    The reading loop (optional)
    ---------------------------
    A VI may declare that every sweep point comprises SEVERAL readings under
    different configurations — e.g. one at +I and one at -I source current for
    thermal-offset cancellation — by overriding ``reading_variants()``. The
    generic sweep procedure calls it once at construction with the VI's
    station-registered name and the resolved measurement params, then at every
    sweep point dispatches each variant's ``commands`` (via the Station) before
    that variant's ``take_reading()`` and suffixes the returned columns
    ``__{key}`` (composing with switch routes as ``{name}__{key}__{route}``).

    Contract (machine-enforced by ``tests/test_conformance.py``):

    * Return ``()`` (single plain reading — the default) or TWO OR MORE
      ``ReadingVariant`` objects; exactly one is a declaration error.
    * Variant ``key``s must be unique within the returned tuple.
    * Every command must target ``vi_name`` (a VI configures only itself) and
      name a real method of the VI.
    * Variants must not change the reading's shape: every variant's
      ``take_reading()`` still returns exactly ``measurement_data_keys`` with
      the lengths ``data_arrays(params)`` declared.
    * A measurement parameter whose value changes what ``reading_variants()``
      returns (e.g. a ``bipolar`` checkbox) must be declared
      ``structural=True`` so the GUI re-derives the live-plot keys when it
      changes.

    Adding a new measurement method: subclass this base (or ``DCMeasurementBase``
    for a DC-resistance method), declare the three class attributes, and
    implement ``data_arrays`` / ``initiate`` / ``take_reading`` / ``standby``
    (plus ``reading_variants`` only if the method needs per-point variants).
    """

    vi_type: str = "measurement"
    # Human label for status lines like "Arming DC resistance measurement".
    display_label: str = "measurement"
    # SHORT human name for the GUI method-selection drop-down; falls back to
    # display_label when empty (see the "Self-description" section above).
    selector_label: ClassVar[str] = ""

    # Self-description — overridden (non-empty) by every concrete VI.
    measurement_parameters: ClassVar[dict[str, ParamSpec]] = {}
    measurement_data_keys: ClassVar[list[str]] = []
    measurement_scalar_columns: ClassVar[dict[str, str]] = {}

    def data_arrays(self, params: Mapping[str, Any]) -> dict[str, int]:
        """Return declared array name → per-point length for these *params*.

        Args:
            params: The same parameter mapping ``initiate()`` will be called
                with (the ``measurement_parameters`` keys).

        Returns:
            ``{array_name: length}`` for every name in ``measurement_data_keys``.

        Raises:
            NotImplementedError: If not overridden by a concrete VI.
        """
        raise NotImplementedError

    def take_reading(self) -> dict[str, list[float]]:
        """Acquire one datapoint at the configuration fixed by ``initiate()``.

        Returns:
            A dict containing exactly ``measurement_data_keys`` (arrays sized as
            ``data_arrays`` declared) plus every ``measurement_scalar_columns``
            key.

        Raises:
            NotImplementedError: If not overridden by a concrete VI.
        """
        raise NotImplementedError

    def reading_variants(
        self, vi_name: str, params: Mapping[str, Any]
    ) -> tuple[ReadingVariant, ...]:
        """Return the ordered reading variants for these *params*, or ``()``.

        The reading-loop hook (see the class docstring for the full contract).
        The generic sweep procedure calls this once at construction; a
        non-empty return makes it take one reading per variant at every sweep
        point, dispatching the variant's commands first and suffixing that
        reading's columns ``__{key}``.

        Args:
            vi_name: This VI's station-registered name — the ``vi_name`` every
                returned command must target (a VI configures only itself).
            params: The resolved measurement parameters, the same mapping
                ``initiate()`` receives.

        Returns:
            ``()`` for a single plain reading per sweep point (the default), or
            an ordered tuple of two or more ``ReadingVariant`` objects with
            unique keys.
        """
        _ = (vi_name, params)
        return ()

    def ping(self) -> bool:
        """Send IDN queries to all drivers and return True if all respond.

        Override in subclasses to call ``get_idn()`` on each driver.
        The base implementation always returns False (unknown).

        Returns:
            True if all drivers respond; False on any exception or if not
            overridden.
        """
        return False


class DCMeasurementBase(MeasurementInstrumentBase):
    """Base class for DC resistance measurement methods (defers to the standard).

    Folds the shared DC-resistance self-description into one place so
    DCSeparateMeasurementVI (Keithley 6221 + 2182A) and DCSingleInstrumentVI
    (Keithley 2400 SMU) are interchangeable via the YAML config alone. Both
    inherit the ``measurement_parameters`` / ``measurement_data_keys`` /
    ``data_arrays`` declared here and implement only ``initiate()`` /
    ``take_reading()`` / ``standby()``.

    The full lifecycle contract is documented on ``MeasurementInstrumentBase``;
    this class adds nothing new, it only fixes the DC-resistance shape
    (``readings_per_point`` samples of ``voltage_V`` and ``current_A``). The
    ``initiate`` / ``take_reading`` stubs raise ``NotImplementedError`` so a
    missing implementation fails loudly at first use.
    """

    display_label: str = "DC resistance"

    measurement_data_keys: ClassVar[list[str]] = ["voltage_V", "current_A"]
    measurement_parameters: ClassVar[dict[str, ParamSpec]] = {
        "current_A": ParamSpec(
            type=float, default=1e-6, unit="A", description="DC source current"
        ),
        "compliance_A": ParamSpec(
            type=float,
            default=1e-3,
            unit="A",
            description="Current compliance on voltmeter",
        ),
        "voltmeter_range_V": ParamSpec(
            type=float,
            default=0.1,
            unit="V",
            description="Voltmeter full-scale range",
        ),
        "readings_per_point": ParamSpec(
            type=int,
            default=10,
            min=1,
            description="DC voltage readings averaged per point",
        ),
    }

    def data_arrays(self, params: Mapping[str, Any]) -> dict[str, int]:
        """Return ``{"voltage_V": n, "current_A": n}`` with n = readings_per_point.

        Args:
            params: Parameter mapping containing ``readings_per_point``.

        Returns:
            Per-point length for each DC data array.
        """
        n = int(params["readings_per_point"])
        return {"voltage_V": n, "current_A": n}

    def initiate(
        self,
        current_A: float = 1e-6,
        compliance_A: float = 1e-3,
        voltmeter_range_V: float = 0.1,
        readings_per_point: int = 10,
    ) -> None:
        """Arm the measurement hardware with fixed DC current, range and count.

        Args:
            current_A: DC source current in Amperes.
            compliance_A: Compliance / protection limit in Amperes.
            voltmeter_range_V: Full-scale voltage measurement range in Volts.
            readings_per_point: Number of voltage samples ``take_reading()``
                collects per datapoint.
        """
        raise NotImplementedError

    def take_reading(self) -> dict[str, list[float]]:
        """Acquire ``readings_per_point`` voltage samples at the fixed current.

        Returns:
            ``{"voltage_V": list[float], "current_A": list[float]}`` with length
            ``readings_per_point`` (fixed at ``initiate()``).
        """
        raise NotImplementedError
