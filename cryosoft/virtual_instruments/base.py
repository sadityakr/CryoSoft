# ---
# description: |
#   BaseVirtualInstrument: the root class for all CryoSoft Virtual Instruments.
#   Provides __init_subclass__ auto-wrapping of @monitored/@control methods with
#   structured logging, get_state() auto-build, and typed sub-bases for each VI
#   category (MagnetBase, TemperatureControllerBase, LevelMeterBase,
#   MeasurementInstrumentBase).
# entry_point: Not run directly; imported by all concrete VI modules.
# dependencies:
#   - cryosoft.core.exceptions
#   - cryosoft.core.decorators
# input: |
#   Subclasses receive drivers dict and arbitrary init_params from Station
#   factory. All @monitored and @control methods are auto-discovered.
# process: |
#   __init_subclass__ iterates vars(cls) to wrap only the new methods defined
#   in each subclass, preserving attributes. get_state() calls all @monitored
#   methods via get_monitored_methods() helper and returns a flat dict.
# output: |
#   Logged method calls (DEBUG/ERROR), structured state dicts from get_state().
# last_updated: 2026-04-06
# ---

"""BaseVirtualInstrument and category base classes.

All VIs inherit from BaseVirtualInstrument (and possibly one of the typed
sub-bases: MagnetBase, TemperatureControllerBase, LevelMeterBase,
MeasurementInstrumentBase).

Do NOT import from Station, Orchestrator, or Procedure here.
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from cryosoft.core.exceptions import CryoSoftCommunicationError


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
    """

    vi_type: str = "unknown"
    # vi_name is set by the Station factory after instantiation, not in __init__.
    vi_name: str = ""

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


# ── Typed category bases ──────────────────────────────────────────────────────
# Directive §"Common Mistakes": all category bases live in base.py.

class MagnetBase(BaseVirtualInstrument):
    """Base class for all magnet-type VIs."""
    vi_type: str = "magnet"


class TemperatureControllerBase(BaseVirtualInstrument):
    """Base class for all temperature-controller VIs."""
    vi_type: str = "temperature"


class LevelMeterBase(BaseVirtualInstrument):
    """Base class for all cryogen-level-meter VIs."""
    vi_type: str = "level"


class MeasurementInstrumentBase(BaseVirtualInstrument):
    """Base class for all measurement-instrument VIs."""
    vi_type: str = "measurement"
