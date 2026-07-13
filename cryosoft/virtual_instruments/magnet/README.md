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
`ramp_segments`, and optionally explicit `min_field_T` / `max_field_T`
(otherwise the field bound is derived as `±max_current / amperes_per_tesla`).

## Exit (what goes out)
`@monitored` readings: `get_field() → float (T)`, `magnet_current() → float (A)`,
`magnet_status() → str`.
`@control` actions: `set_field(target_T)` — bounded by the setup's field limit
via the control-validation standard (`control_limits`); an out-of-range value
raises `CryoSoftSafetyError` before any hardware command.
`RampableVI` interface: `start_ramp()`, `advance_ramp()`, `ramp_status()`,
`stop_ramp()` (kills the generator AND commands a hardware hold — used by the
Orchestrator on abort/pause/error).
`evaluate_safety()` reports `{"quench": ...}` from the polled status to
`Station.check_safety()`; a quench escalates to EMERGENCY.

## Interface contract
All classes here extend `SuperconductingMagnetVI` (itself inheriting from
`MagnetBase` and `RampableVI` defined in `virtual_instruments/base.py`
and `virtual_instruments/rampable.py`).

## How to add a new magnet VI
1. Subclass `SuperconductingMagnetVI` (or `SuperconductingMagnetPersistentVI`).
2. Override only the methods that differ from the base behaviour.
3. Follow the control-validation standard (see `BaseVirtualInstrument`):
   declare `control_limits` for any new bounded `@control` parameter and
   populate `self._limits` from `init_params`; write semantic guards as
   explicit `CryoSoftSafetyError` raises at the top of the method.
4. Add the new class to `devices.yaml` using the full dotted path.
5. Add tests to `tests/test_l1_new_vis.py` (the conformance tests cover the
   limits contract automatically).

## Files
- `superconducting_magnet.py` — `SuperconductingMagnetVI`: status-driven field ramp,
  optional segment-based rate scheduling; aborts the sequence on a QUENCH status.
  Key API: `@monitored get_field` / `magnet_current` / `magnet_status`,
  `@control set_field`, the `RampableVI` methods, `evaluate_safety()`. tests:
  `tests/test_l1_new_vis.py` (`TestSuperConductingMagnetVI`),
  `tests/test_l1_virtual_instruments.py`.
- `superconducting_magnet_persistent.py` — `SuperconductingMagnetPersistentVI`:
  extends above with switch heater control and the tick-count persistent-mode
  sequence. Quench-safe ordering: the PSU is ramped to match the coil current
  while the switch is still cold, and only then is the heater energised —
  heating across a mismatch would quench the magnet. The manual
  `switch_heater_on` control enforces the same rule (refuses on mismatch). tests:
  `tests/test_l1_new_vis.py` (`TestSuperConductingMagnetPersistentVI`),
  `tests/test_switch_heater.py`.
- `switch_heater.py` — `SwitchHeater`: wall-clock state object owned by
  `SuperconductingMagnetPersistentVI` that tracks heater on/off plus warmup /
  cooldown readiness in seconds (tick-rate independent). Key API: `turn_on`,
  `turn_off`, `is_on`, `is_ready`, `is_cold`, `seconds_until_ready`. Not a VI.
  tests: `tests/test_switch_heater.py`.
- `__init__.py` — package marker. tests: none.
