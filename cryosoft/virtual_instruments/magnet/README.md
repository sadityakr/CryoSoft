# virtual_instruments/magnet/

## Purpose
Virtual instruments for superconducting magnet power supplies. Abstracts away the
model-specific driver (IPS120, IPS180, …) so procedures interact with
`SuperconductingMagnetVI` regardless of which PSU is installed.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
A driver dict `{"main": <PSU real driver>}` and optional `init_params`:
`amperes_per_tesla`, `max_current`, `min_current`, `default_ramp_rate`,
`ramp_segments`.

## Exit (what goes out)
`@monitored` readings: `get_field() → float (T)`, `magnet_current() → float (A)`,
`magnet_status() → str`.
`@control` actions: `set_field(target_T)`.
`RampableVI` interface: `start_ramp()`, `advance_ramp()`, `ramp_status()`.

## Interface contract
All classes here extend `SuperconductingMagnetVI` (itself inheriting from
`MagnetBase` and `RampableVI` defined in `virtual_instruments/base.py`
and `virtual_instruments/rampable.py`).

## How to add a new magnet VI
1. Subclass `SuperconductingMagnetVI` (or `SuperconductingMagnetPersistentVI`).
2. Override only the methods that differ from the base behaviour.
3. Add the new class to `devices.yaml` using the full dotted path.
4. Add tests to `tests/test_l1_new_vis.py`.

## Files
- `superconducting_magnet.py` — `SuperconductingMagnetVI`: status-driven field ramp,
  optional segment-based rate scheduling.
- `superconducting_magnet_persistent.py` — `SuperconductingMagnetPersistentVI`:
  extends above with switch heater control and tick-count persistent-mode sequence.
