# ---
# description: |
#   SwitchMatrixVI: a Virtual Instrument over a matrix-switch / scanner driver
#   (e.g. the Keithley 705). Multiplexes measurement channels by named routes:
#   a route maps to a list of instrument-format channel specs, and selecting a
#   route connects exactly those channels. Implements the EXCLUSIVE-MUX policy:
#   only one route is connected at a time, so selecting a route always opens
#   every channel first, then closes the route's channels.
# entry_point: Not run directly; instantiated by the Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (BaseVirtualInstrument)
#   - cryosoft.core.decorators (monitored, control)
#   - cryosoft.core.exceptions (CryoSoftConfigError)
# input: |
#   drivers = {"main": <705-style switch driver>}. init_params:
#   routes (dict[str, list[str]] — route name -> channel-spec list) and
#   settle_time_s (float, default 0.0). Config owns the wiring; the VI never
#   hardcodes channels.
# process: |
#   __init__ validates every route name (non-empty, unique, no "__" or "/") and
#   channel-spec list (non-empty) and settle_time_s (>= 0), raising
#   CryoSoftConfigError naming the offender. select_route() opens all, closes the
#   route's channels, sleeps settle_time_s, records the active route. open_all()
#   clears the active route.
# output: |
#   get_state() reports the active route as active_route (str, "" when none) and
#   active_route_index (int, -1 when none). routes() lists the configured route
#   names in config order.
# last_updated: 2026-07-13
# ---

"""SwitchMatrixVI — matrix-switch / scanner virtual instrument (exclusive mux)."""

from __future__ import annotations

import time
from typing import Any

from cryosoft.core.decorators import control, monitored
from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.virtual_instruments.base import BaseVirtualInstrument


class SwitchMatrixVI(BaseVirtualInstrument):
    """Matrix-switch / scanner VI that multiplexes measurement channels by route.

    A **route** is a named connection (its name comes verbatim from config)
    mapping to a list of instrument-format channel specs (e.g. ``["1!1"]``). The
    VI implements the **EXCLUSIVE-MUX** policy: at most one route is connected at
    a time, so :meth:`select_route` always opens every channel first and then
    closes the selected route's channels. This models a scanner where a single
    device is connected at a time.

    A future *true-matrix* policy (multiple routes closed simultaneously) would
    be a **config flag**, not a code fork: the same VI would skip the leading
    ``open_all()`` when the flag is set. It is deliberately not implemented here.

    Drivers:
        ``"main"`` — a 705-style switch driver exposing ``get_idn`` /
        ``close_channels(specs)`` / ``open_channels(specs)`` / ``open_all``.

    Config ``init_params``:
        ``routes``: ``dict[str, list[str]]`` — route name -> channel-spec list.
        ``settle_time_s``: ``float`` — dwell after a route change (default 0.0).

    The VI never hardcodes channels; the config owns the wiring.
    """

    vi_type: str = "switch"
    display_label: str = "switch matrix"

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        """Validate the route table and settle time from config.

        Args:
            drivers: ``{"main": <switch driver>}``.
            **init_params: Must provide ``routes`` (route name -> channel-spec
                list) and may provide ``settle_time_s`` (default 0.0).

        Raises:
            CryoSoftConfigError: If a route name is empty, duplicated, or
                contains "__" or "/"; if a channel-spec list is empty; or if
                ``settle_time_s`` is negative or not a real number.
        """
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        routes = init_params.get("routes", {}) or {}
        if not isinstance(routes, dict):
            raise CryoSoftConfigError(
                f"SwitchMatrixVI 'routes' must be a mapping, got {routes!r}"
            )

        validated: dict[str, list[str]] = {}
        seen: set[str] = set()
        for name, specs in routes.items():
            if not isinstance(name, str) or not name:
                raise CryoSoftConfigError(
                    f"SwitchMatrixVI route name must be a non-empty str, got {name!r}"
                )
            if "__" in name or "/" in name:
                raise CryoSoftConfigError(
                    f"SwitchMatrixVI route name {name!r} must not contain '__' "
                    "(the array/route separator) or '/' (illegal in an HDF5 "
                    "dataset name)"
                )
            if name in seen:
                raise CryoSoftConfigError(
                    f"SwitchMatrixVI route name {name!r} is duplicated"
                )
            seen.add(name)
            if not isinstance(specs, (list, tuple)) or not specs:
                raise CryoSoftConfigError(
                    f"SwitchMatrixVI route {name!r} must map to a non-empty "
                    f"channel-spec list, got {specs!r}"
                )
            validated[name] = [str(s) for s in specs]

        settle = init_params.get("settle_time_s", 0.0)
        if isinstance(settle, bool) or not isinstance(settle, (int, float)):
            raise CryoSoftConfigError(
                f"SwitchMatrixVI 'settle_time_s' must be a real number, got {settle!r}"
            )
        if settle < 0:
            raise CryoSoftConfigError(
                f"SwitchMatrixVI 'settle_time_s' must be >= 0, got {settle!r}"
            )

        self._routes: dict[str, list[str]] = validated
        self._settle_time_s: float = float(settle)
        self._active_route: str = ""

    # ------------------------------------------------------------------
    # Route table
    # ------------------------------------------------------------------

    def routes(self) -> list[str]:
        """Return the configured route names, in config order."""
        return list(self._routes)

    # ------------------------------------------------------------------
    # @monitored — polled into get_state() every tick
    # ------------------------------------------------------------------

    @monitored
    def active_route(self) -> str:
        """Return the currently-selected route name, or "" if none is active."""
        return self._active_route

    @monitored
    def active_route_index(self) -> int:
        """Return the active route's index in config order, or -1 if none.

        The numeric companion to :meth:`active_route`, so the flat state cache
        (numeric scalars only) can carry which route is connected.
        """
        if not self._active_route:
            return -1
        return list(self._routes).index(self._active_route)

    # ------------------------------------------------------------------
    # @control — user/procedure actions
    # ------------------------------------------------------------------

    @control
    def select_route(self, route: str) -> None:
        """Connect exactly one route (exclusive mux): open all, then close it.

        Opens every channel first (enforcing exclusivity), closes the route's
        channels, dwells ``settle_time_s`` for the relays to settle, then records
        the active route. ``time.sleep`` is acceptable here because ``measure()``
        is the tick's designated blocking phase by design.

        Args:
            route: Name of a configured route.

        Raises:
            ValueError: If ``route`` is not a configured route name.
        """
        if route not in self._routes:
            raise ValueError(
                f"select_route: unknown route {route!r}; configured routes are "
                f"{list(self._routes)}"
            )
        self._driver.open_all()  # type: ignore[attr-defined]
        self._driver.close_channels(self._routes[route])  # type: ignore[attr-defined]
        time.sleep(self._settle_time_s)
        self._active_route = route

    @control
    def open_all(self) -> None:
        """Open every channel and clear the active route."""
        self._driver.open_all()  # type: ignore[attr-defined]
        self._active_route = ""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def standby(self) -> None:
        """Put the switch in a safe idle state: open every channel."""
        self.open_all()

    def ping(self) -> bool:
        """Return True if the switch driver answers ``get_idn()``.

        Returns:
            True if the driver responds; False on any exception.
        """
        try:
            self._driver.get_idn()  # type: ignore[attr-defined]
            return True
        except Exception:
            return False
