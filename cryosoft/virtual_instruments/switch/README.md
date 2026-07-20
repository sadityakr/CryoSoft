# virtual_instruments/switch/

## Purpose
Virtual instruments for matrix-switch / scanner hardware. A **Switch VI**
multiplexes measurement channels by named **routes**: a route maps to a list of
instrument-format channel specs, and selecting a route connects exactly those
channels. The shipped class, `SwitchMatrixVI`, implements the **exclusive-mux**
policy (one route connected at a time; selecting a route always opens every
channel first). This is the capability that lets a sweep measure several devices
per datapoint, each into its own per-route data column.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
Driver dict and `init_params` from the config YAML:
- `SwitchMatrixVI`: `{"main": <705-style switch driver>}`.
- `init_params.routes`: `dict[str, list[str]]` — route name -> channel-spec
  list (e.g. `{"Mux-Ch1": ["1"], "Mux-Ch2": ["2"]}` for a Keithley 705, whose
  channels are plain numbers). Route names come
  verbatim from config and must contain neither `__` nor `/`.
- `init_params.settle_time_s`: `float` dwell after a route change (default 0.0).
- `init_params.pole_mode`: optional `int` (1, 2 or 4), applied to the
  instrument at construction. On a scanner the pole mode decides how card
  terminals group into channels, so it changes both the channel count and what
  a channel number physically connects — a route table is only meaningful
  alongside the mode it was written for. Use 4 for four-wire measurements,
  where one channel switches all four leads together. On the Keithley 705 the
  counts are 1-pole 40, 2-pole 20, 4-pole 10; a route naming channel 15 works
  in 2-pole and silently never connects in 4-pole. Omit to leave the
  instrument in whatever mode it powered up in.

The VI never hardcodes channels; the config owns the wiring.

## Exit (what goes out)
- `routes() -> list[str]` — configured route names, in config order.
- `@control select_route(route)` — exclusive-mux select: `open_all()`, close the
  route's channels, sleep `settle_time_s`, record the active route.
- `@control open_all()` — open every channel, clear the active route.
- `standby()` — open every channel (safe idle).
- `ping() -> bool` — `get_idn()` reachability check.
- `get_state()` reports `active_route` (str, `""` when none) and
  `active_route_index` (int, `-1` when none) so the numeric flat-state cache can
  carry which route is connected.

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
arming the measurement VI at `initiate()`, and — when two or more routes are
selected — loops the routes at each datapoint, suffixing every measurement key
with `__{route}` (e.g. `voltage_V__Mux-Ch1`). Procedures reach the switch only
through the `Station` instance (contract C6).

## How to add a new switch VI
1. Subclass `BaseVirtualInstrument`, set `vi_type = "switch"` and a
   `display_label`, and take `drivers = {"main": <driver>}`.
2. Validate the route table and any dwell/settle parameters from `init_params`
   at `__init__`, raising `CryoSoftConfigError` naming the offender.
3. Expose `routes()`, `@control select_route(route)`, `@control open_all()`,
   `standby()`, `ping()`, and `@monitored active_route()` /
   `active_route_index()`.
4. Register in `devices.yaml` with `vi_type: switch`; add behaviour tests to
   `tests/test_l1_switch_vi.py`.

## Files
- `switch_matrix.py` — `SwitchMatrixVI`: exclusive-mux matrix switch over a
  705-style scanner driver. Key API: `routes()`, `@control select_route` /
  `open_all`, `standby()`, `ping()`, `@monitored active_route` /
  `active_route_index`. tests: `tests/test_l1_switch_vi.py`.
- `__init__.py` — package marker. tests: none.
