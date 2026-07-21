# virtual_instruments/switch/

## Purpose
Virtual instruments for matrix-switch / scanner hardware. A **Switch VI**
multiplexes measurement channels by named **routes**: a route maps to a list of
instrument-format channel specs, and selecting a route connects exactly those
channels. `close_channel()`/`open_channel()` give the same exclusive control
over any raw channel number directly, bypassing the route table — bench work
that doesn't have a named route yet. The shipped class, `SwitchMatrixVI`,
implements the **exclusive-mux** policy (one route or channel connected at a
time). This is the capability that lets a sweep measure several devices per
datapoint, each into its own per-route data column.

Every exclusive connect honours a runtime-toggleable **hot/cold switching**
order (see GLOSSARY.md): hot (default) closes the new channel before opening
the old one, so a live current source never sees an open circuit and trips
compliance; cold opens first, always exclusive but with a brief open circuit
on every change. The scanner's **pole mode** (1/2/4) is also runtime-settable,
in addition to the config-owned default.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
Driver dict and `init_params` from the config YAML:
- `SwitchMatrixVI`: `{"main": <705-style switch driver>}`.
- `init_params.routes`: `dict[str, list[str]]` — route name -> channel-spec
  list (e.g. `{"Mux-Ch1": ["1"], "Mux-Ch2": ["2"]}` for a Keithley 705, whose
  channels are plain numbers). Route names come
  verbatim from config and must contain neither `__` nor `/`.
- `init_params.settle_time_s`: `float` dwell after a route/channel change
  (default 0.0).
- `init_params.hot_switching`: `bool` (default `True`) — switching order for
  every exclusive connect. See GLOSSARY.md "Hot switching / cold switching".
  Runtime-toggleable via `@control set_hot_switching(enabled)` without
  reconstructing the VI.
- `init_params.pole_mode`: optional `int` (1, 2 or 4), applied to the
  instrument at construction. On a scanner the pole mode decides how card
  terminals group into channels, so it changes both the channel count and what
  a channel number physically connects — a route table is only meaningful
  alongside the mode it was written for. Use 4 for four-wire measurements,
  where one channel switches all four leads together. On the Keithley 705 the
  counts are 1-pole 40, 2-pole 20, 4-pole 10; a route naming channel 15 works
  in 2-pole and silently never connects in 4-pole. Omit to leave the
  instrument in whatever mode it powered up in. Also runtime-settable via
  `@control(panel=False) set_pole_mode(poles)` — front-panel only, since it can
  silently invalidate the whole route table.

The VI never hardcodes channels; the config owns the wiring.

## Exit (what goes out)
- `routes() -> list[str]` — configured route names, in config order.
- `@control select_route(route)` — exclusive-mux select of a configured route.
- `@control close_channel(channel)` — exclusive-mux select of any raw channel
  number, independent of the route table.
- `@control open_channel(channel)` — open exactly one channel; no exclusivity
  or ordering concern.
- `@control open_all()` — open every channel, clear the active route/channels.
- `@control set_hot_switching(enabled)` — toggle hot/cold switching order at
  runtime.
- `@control(panel=False) set_pole_mode(poles)` — change pole mode at runtime;
  always opens every channel first.
- `standby()` — open every channel (safe idle).
- `ping() -> bool` — `get_idn()` reachability check.
- `get_state()` reports `active_route` (str, `""` when none),
  `active_route_index` (int, `-1` when none), `active_channels` (comma-joined
  closed channel specs, read back from the driver's `closed_channels()` when
  it has one), `hot_switching_enabled` (bool), and `pole_mode` (int, `0` when
  never set).

## Interface contract
`SwitchMatrixVI` subclasses `BaseVirtualInstrument` with `vi_type = "switch"`.
It is registered in `devices.yaml` with `vi_type: switch` (a registry role
distinct from `system` / `measurement` / `level`). Route names, channel specs,
and settle time are validated at construction — a bad name (empty, duplicated,
or containing `__` / `/`), an empty channel-spec list, or a negative
`settle_time_s` raises `CryoSoftConfigError` naming the offender, so a malformed
switch config fails at build time, not mid-run.

`SweepMeasureProcedure` uses the first switch VI a station exposes: it renders
one checkbox per route ("Measure route <name>"), commands `select_route` before
arming the measurement VI at `initiate_measurement()`, and — when two or more routes are
selected — loops the routes at each datapoint, suffixing every measurement key
with `__{route}` (e.g. `voltage_V__Mux-Ch1`). Procedures reach the switch only
through the `Station` instance (contract C6).

## How to add a new switch VI
1. Subclass `BaseVirtualInstrument`, set `vi_type = "switch"` and a
   `display_label`, and take `drivers = {"main": <driver>}`.
2. Validate the route table and any dwell/settle/hot_switching parameters from
   `init_params` at `__init__`, raising `CryoSoftConfigError` naming the
   offender.
3. Expose `routes()`, `@control select_route(route)`, `@control
   close_channel(channel)` / `open_channel(channel)`, `@control open_all()`,
   `@control set_hot_switching(enabled)`, `standby()`, `ping()`, and
   `@monitored active_route()` / `active_route_index()` / `active_channels()`
   / `hot_switching_enabled()`. Implement any exclusive connect through one
   shared helper (mirroring `_connect_exclusive()`) so hot/cold ordering is
   applied uniformly, not reimplemented per method.
4. Register in `devices.yaml` with `vi_type: switch`; add behaviour tests to
   `tests/test_l1_switch_vi.py`.

## Files
- `switch_matrix.py` — `SwitchMatrixVI`: exclusive-mux matrix switch over a
  705-style scanner driver. Key API: `routes()`, `@control select_route` /
  `close_channel` / `open_channel` / `open_all` / `set_hot_switching` /
  `set_pole_mode`, `standby()`, `ping()`, `@monitored active_route` /
  `active_route_index` / `active_channels` / `hot_switching_enabled` /
  `pole_mode`. tests: `tests/test_l1_switch_vi.py`.
- `__init__.py` — package marker. tests: none.
