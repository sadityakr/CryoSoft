# ---
# description: |
#   SwitchMatrixVI: a Virtual Instrument over a matrix-switch / scanner driver
#   (e.g. the Keithley 705). Multiplexes measurement channels by named routes:
#   a route maps to a list of instrument-format channel specs, and selecting a
#   route connects exactly those channels. Implements the EXCLUSIVE-MUX policy:
#   only one route (or manually chosen channel) is connected at a time.
#   close_channel()/open_channel() give direct, route-table-independent access
#   to any raw channel. Every connect (select_route or close_channel) honours a
#   runtime-toggleable hot_switching setting: hot (default) closes the new
#   channel(s) before opening the old ones (make-before-break — no momentary
#   open circuit, so a live current source can't see an open load and trip
#   compliance); cold opens the old ones first (break-before-make — always
#   exclusive, at the cost of a brief open circuit). Pole mode (1/2/4) is
#   runtime-settable too, always opening every channel first since it renumbers
#   them.
# entry_point: Not run directly; instantiated by the Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (BaseVirtualInstrument)
#   - cryosoft.core.decorators (monitored, control)
#   - cryosoft.core.exceptions (CryoSoftConfigError)
# input: |
#   drivers = {"main": <705-style switch driver>}. init_params:
#   routes (dict[str, list[str]] — route name -> channel-spec list),
#   settle_time_s (float, default 0.0), pole_mode (int, optional), and
#   hot_switching (bool, default True). Config owns the wiring; the VI never
#   hardcodes channels.
# process: |
#   __init__ validates every route name (non-empty, unique, no "__" or "/") and
#   channel-spec list (non-empty), settle_time_s (>= 0), and hot_switching
#   (bool), raising CryoSoftConfigError naming the offender. select_route() and
#   close_channel() both route through _connect_exclusive(), which orders the
#   close/open pair per hot_switching, sleeps settle_time_s, and records the
#   active state. open_channel() opens one channel only, no ordering concerns.
#   open_all() clears the active route/channels. set_pole_mode() opens
#   everything, then reprograms the driver's pole mode.
# output: |
#   get_state() reports active_route (str, "" when none), active_route_index
#   (int, -1 when none), active_channels (comma-joined closed channel specs,
#   read back from the driver when it supports closed_channels()),
#   hot_switching_enabled (bool), and pole_mode (int, 0 when never set).
#   routes() lists the configured route names in config order.
# last_updated: 2026-07-22
# ---

"""SwitchMatrixVI — matrix-switch / scanner virtual instrument (exclusive mux)."""

from __future__ import annotations

import time
from typing import Any, ClassVar

from cryosoft.core.decorators import control, monitored
from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import BaseVirtualInstrument


class SwitchMatrixVI(BaseVirtualInstrument):
    """Matrix-switch / scanner VI that multiplexes measurement channels by route.

    A **route** is a named connection (its name comes verbatim from config)
    mapping to a list of instrument channel specs (e.g. ``["1"]`` for a 705).
    :meth:`close_channel` gives the same exclusive connect for any raw channel
    number, bypassing the route table entirely — useful for bench work where
    no route has been named yet. The VI implements the **EXCLUSIVE-MUX**
    policy: at most one route or channel is connected at a time.

    A future *true-matrix* policy (multiple routes closed simultaneously) would
    be a **config flag**, not a code fork: the same VI would skip opening the
    previously-active channels when the flag is set. It is deliberately not
    implemented here.

    **Hot vs. cold switching** (the ``hot_switching`` setting, runtime
    toggleable via :meth:`set_hot_switching`): every exclusive connect made
    through :meth:`select_route` or :meth:`close_channel` closes the new
    channel(s) and opens the stale ones, in an order this setting controls.

    * **Hot** (default): closes the new channel(s) *before* opening the old
      ones (make-before-break). There is never a moment with nothing closed,
      so a live current source never sees an open load — the failure mode
      this exists to avoid is the source railing to its compliance limit
      mid-sweep, at every route change, because it briefly had nowhere to
      push current. The trade-off: for one instant, both the old and new
      channels are connected together.
    * **Cold**: opens the old channel(s) first, then closes the new ones
      (break-before-make) — classic exclusive-mux behaviour, never two
      channels connected at once, at the cost of a brief open circuit on
      every change.

    :meth:`open_channel` opens exactly one channel and has no ordering
    concern — nothing new is being made active.

    Drivers:
        ``"main"`` — a 705-style switch driver exposing ``get_idn`` /
        ``close_channels(specs)`` / ``open_channels(specs)`` / ``open_all``.
        ``closed_channels() -> list[str]`` is used when present (ground-truth
        readback, as the Keithley 705 driver itself prefers over any local
        model) to compute exactly which channels are stale before a switch;
        a driver without it falls back to this VI's own tracking.

    Config ``init_params``:
        ``routes``: ``dict[str, list[str]]`` — route name -> channel-spec list.
        ``settle_time_s``: ``float`` — dwell after a route/channel change
            (default 0.0).
        ``hot_switching``: ``bool`` — switching order for every exclusive
            connect (default ``True``; see above). Runtime-toggleable via
            :meth:`set_hot_switching` without reconstructing the VI.
        ``pole_mode``: ``int`` — optional wiring mode (1, 2 or 4) applied to the
            instrument at construction. On a scanner the pole mode determines
            how card terminals group into channels, so it changes both the
            channel count and what a channel number physically connects; a
            route table is only meaningful alongside the mode it was written
            for. Use 4 for four-wire measurements, where one channel switches
            all four leads together. Omitted -> the instrument is left in
            whatever mode it powered up in. Also runtime-settable via
            :meth:`set_pole_mode` (front-panel only — see its docstring for
            why this one is never on the compact card).

    The VI never hardcodes channels; the config owns the wiring.
    """

    vi_type: str = "switch"
    display_label: str = "Scanner (mux)"

    # Reading-loop participation: the route is a loopable parameter like any
    # other — the generic sweep procedure can loop it at every sweep point via
    # select_route, and dispatches open_all at standby/abort when this VI took
    # part in the loop. See BaseVirtualInstrument's reading-loop section.
    reading_setters: ClassVar[dict[str, str]] = {"route": "select_route"}
    reading_safe_off: ClassVar[str] = "open_all"

    @property
    def reading_parameters(self) -> dict[str, ParamSpec]:
        """Return the loopable ``route`` parameter's spec.

        The spec is built at runtime because its enumerated ``choices`` are
        the config-owned route table. An enumerated spec renders as one
        checkbox per route in the Reading loop form group.

        Returns:
            ``{"route": ParamSpec}`` with choices ``{route_name: route_name}``,
            or ``{}`` when the config declares no routes.
        """
        route_names = list(self._routes)
        if not route_names:
            return {}
        return {
            "route": ParamSpec(
                type=str,
                default=route_names[0],
                choices={name: name for name in route_names},
                description="Measurement channel (configured switch route)",
            )
        }

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

        hot_switching = init_params.get("hot_switching", True)
        if not isinstance(hot_switching, bool):
            raise CryoSoftConfigError(
                f"SwitchMatrixVI 'hot_switching' must be a bool, got {hot_switching!r}"
            )

        self._routes: dict[str, list[str]] = validated
        self._settle_time_s: float = float(settle)
        self._hot_switching: bool = hot_switching
        self._active_route: str = ""
        # Fallback tracking of the closed channel specs, used only when the
        # driver doesn't expose closed_channels() readback (see
        # _currently_closed()). Kept in whatever spec form the caller gave.
        self._active_specs: list[str] = []

        # Pole mode is a wiring property of the setup, so it lives in config and
        # is applied here — before any route is selected, since it renumbers the
        # channels the route table names.
        pole_mode = init_params.get("pole_mode")
        if pole_mode is not None:
            if isinstance(pole_mode, bool) or not isinstance(pole_mode, int):
                raise CryoSoftConfigError(
                    f"SwitchMatrixVI 'pole_mode' must be an int (1, 2 or 4), "
                    f"got {pole_mode!r}"
                )
            if pole_mode not in (1, 2, 4):
                raise CryoSoftConfigError(
                    f"SwitchMatrixVI 'pole_mode' must be 1, 2 or 4, got {pole_mode!r}"
                )
            self._driver.set_pole_mode(pole_mode)
        self._pole_mode: int | None = pole_mode

    # ------------------------------------------------------------------
    # Route table
    # ------------------------------------------------------------------

    def routes(self) -> list[str]:
        """Return the configured route names, in config order."""
        return list(self._routes)

    def control_param_specs(self, method_name: str) -> dict[str, ParamSpec]:
        """Render ``select_route``'s route as a drop-down of the config's routes.

        The route table only exists after construction (it is a setup
        property), so the choices cannot live on the decorator — this
        instance-level hook injects them, and the GUI renders a selection
        list instead of a free-text field. Presentation only:
        ``select_route`` still validates the name itself.

        Args:
            method_name: The @control method name being rendered.

        Returns:
            The dynamic spec for ``select_route``; the inherited declaration
            for every other control.
        """
        if method_name == "select_route" and self._routes:
            route_names = list(self._routes)
            return {
                "route": ParamSpec(
                    type=str,
                    default=route_names[0],
                    choices={name: name for name in route_names},
                    description="Config-named route to close (exclusive mux)",
                )
            }
        return super().control_param_specs(method_name)

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

    @monitored
    def active_channels(self) -> str:
        """Return the currently-closed raw channel specs, comma-joined.

        Reads the driver's ground-truth readback (``closed_channels()``) when
        the driver exposes it — the same "instrument is authoritative" design
        the Keithley 705 driver itself uses over its own local model — so a
        channel closed by hand at the front panel, or by a previous VI
        instance, still shows up here. Falls back to this VI's own tracking
        for a driver without readback.
        """
        return ",".join(self._currently_closed())

    @monitored
    def hot_switching_enabled(self) -> bool:
        """Return True when hot-switching (make-before-break) is active."""
        return self._hot_switching

    @monitored
    def pole_mode(self) -> int:
        """Return the scanner's current pole mode (1, 2 or 4), or 0 if never set."""
        return self._pole_mode or 0

    # ------------------------------------------------------------------
    # Switching-order internals (hot vs. cold — see class docstring)
    # ------------------------------------------------------------------

    @staticmethod
    def _spec_key(spec: str) -> str:
        """Return a comparison key for a channel spec, tolerant of padding.

        Channel specs are typically numeric ("1" from a route vs. the
        instrument's own zero-padded "001" readback); comparing by int value
        means a route/channel change correctly recognises an already-closed
        channel as unchanged regardless of padding, instead of spuriously
        treating it as stale and reopening it mid-hot-switch. Falls back to
        the raw string for a non-numeric spec format.
        """
        try:
            return str(int(spec))
        except ValueError:
            return spec

    def _currently_closed(self) -> list[str]:
        """Return the closed channel specs, preferring the driver's readback.

        Args: none.

        Returns:
            ``self._driver.closed_channels()`` when the driver exposes it
            (ground truth); otherwise this VI's own ``_active_specs`` tracking.
        """
        reader = getattr(self._driver, "closed_channels", None)
        if reader is not None:
            return list(reader())
        return list(self._active_specs)

    def _connect_exclusive(self, specs: list[str]) -> None:
        """Make *specs* the only closed channels, ordered per ``hot_switching``.

        Hot (default): close *specs* first, then open whatever was closed
        before that isn't part of *specs* — make-before-break, no moment with
        nothing closed. Cold: open everything, then close *specs* —
        break-before-make, the original exclusive-mux behaviour, guaranteed
        exclusive even if a stray channel was closed outside this VI's
        tracking (e.g. left over from a previous session).

        Args:
            specs: Channel specs to make exclusively closed.
        """
        specs = [str(s) for s in specs]
        if self._hot_switching:
            target_keys = {self._spec_key(s) for s in specs}
            stale = [
                s for s in self._currently_closed() if self._spec_key(s) not in target_keys
            ]
            self._driver.close_channels(specs)  # type: ignore[attr-defined]
            if stale:
                self._driver.open_channels(stale)  # type: ignore[attr-defined]
        else:
            self._driver.open_all()  # type: ignore[attr-defined]
            self._driver.close_channels(specs)  # type: ignore[attr-defined]
        time.sleep(self._settle_time_s)
        self._active_specs = specs

    # ------------------------------------------------------------------
    # @control — user/procedure actions
    # ------------------------------------------------------------------

    @control
    def select_route(self, route: str) -> None:
        """Connect exactly one configured route (exclusive mux).

        Dwells ``settle_time_s`` for the relays to settle, then records the
        active route. ``time.sleep`` is acceptable here because ``measure()``
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
        self._connect_exclusive(self._routes[route])
        self._active_route = route

    @control(
        params={
            "channel": ParamSpec(
                type=str,
                default="1",
                description=(
                    "Raw channel number to close (exclusive mux). Bypasses "
                    "the route table entirely — useful for bench work with "
                    "no route configured yet."
                ),
            )
        }
    )
    def close_channel(self, channel: str) -> None:
        """Connect exactly one raw channel by number (exclusive mux).

        Args:
            channel: The channel number to close, as the driver's spec format
                (e.g. ``"5"`` for a Keithley 705).
        """
        self._connect_exclusive([str(channel)])
        self._active_route = ""

    @control(
        params={
            "channel": ParamSpec(
                type=str,
                default="1",
                description="Raw channel number to open. Only this channel is affected.",
            )
        }
    )
    def open_channel(self, channel: str) -> None:
        """Open exactly one channel, leaving any other closed channel alone.

        No switching-order concern — nothing new is being made active, so
        ``hot_switching`` does not apply here.

        Args:
            channel: The channel number to open.
        """
        ch = str(channel)
        self._driver.open_channels([ch])  # type: ignore[attr-defined]
        key = self._spec_key(ch)
        self._active_specs = [s for s in self._active_specs if self._spec_key(s) != key]
        self._active_route = ""

    @control
    def open_all(self) -> None:
        """Open every channel and clear the active route/channels."""
        self._driver.open_all()  # type: ignore[attr-defined]
        self._active_route = ""
        self._active_specs = []

    @control(
        params={
            "enabled": ParamSpec(
                type=bool,
                default=True,
                description=(
                    "Hot-switching: close the new channel before opening the "
                    "old one (make-before-break), so a live current source "
                    "never sees an open circuit and trips compliance. Off = "
                    "cold-switching (break-before-make): always exclusive, "
                    "but every change briefly opens the circuit."
                ),
            )
        }
    )
    def set_hot_switching(self, enabled: bool) -> None:
        """Toggle hot/cold switching order at runtime.

        Args:
            enabled: True for hot (make-before-break), False for cold
                (break-before-make).
        """
        self._hot_switching = bool(enabled)

    @control(
        params={
            "poles": ParamSpec(
                type=int,
                default=4,
                choices={"1-pole": 1, "2-pole": 2, "4-pole": 4},
                description=(
                    "Scanner pole configuration. Changes how card terminals "
                    "group into channels, so it changes both the channel "
                    "count and what a channel number physically connects — "
                    "a route table is only meaningful alongside the mode it "
                    "was written for. Opens every channel first."
                ),
            )
        },
        panel=False,
    )
    def set_pole_mode(self, poles: int) -> None:
        """Change the scanner's pole configuration at runtime.

        Always opens every channel first (via :meth:`open_all`) regardless of
        ``hot_switching`` — the mode change renumbers the channels, so
        make-before-break has no meaning here (there is no "old" channel
        under the new numbering to make continuity with). ``panel=False``:
        this is a wiring-level, rarely-changed setting that can silently
        invalidate the whole route table (a route written for 4-pole means
        something else in 2-pole), so it lives in the instrument's front
        panel, not the compact monitor card — mirroring how arming actions
        elsewhere in the codebase are kept off the compact card.

        Args:
            poles: 1, 2 or 4.

        Raises:
            ValueError: If ``poles`` is not 1, 2 or 4.
        """
        if poles not in (1, 2, 4):
            raise ValueError(f"set_pole_mode: poles must be 1, 2 or 4, got {poles!r}")
        self.open_all()
        self._driver.set_pole_mode(int(poles))  # type: ignore[attr-defined]
        self._pole_mode = int(poles)

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
