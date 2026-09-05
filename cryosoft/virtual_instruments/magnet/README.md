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
`@monitored` readings: `magnet_field_T() → float (T)`, `magnet_current() → float (A)`,
`psu_current() → float (A)`, `magnet_status() → str` (the raw **PSU status** —
HOLD/RAMPING/QUENCH/CLAMPED, read straight from the driver), `magnet_state() →
str` (this VI's logical interpretation of PSU status plus the live current
readings — "standby"/"ramping"/"holding"/"quenched"/"clamped"; see
GLOSSARY.md's **Magnet state**). Operations gate readiness on `magnet_state()`, never on
raw current thresholds — see GLOSSARY.md's **Magnet state**.
`@control` actions: `set_field(target_T)` — bounded by the setup's field limit
via the control-validation standard (`control_limits`); an out-of-range value
raises `CryoSoftSafetyError` before any hardware command.
`RampableVI` interface: `start_ramp()`, `advance_ramp()`, `ramp_status()`,
`stop_ramp()` (kills the generator AND commands a hardware hold — used by the
Orchestrator on abort/pause/error).
`evaluate_safety()` reports `{"quench": ...}` from the polled status to
`Station.check_safety()`; `MagnetBase.safety_flags` declares `"quench"`
critical (GLOSSARY.md's **Safety-flag manifest**), so a tripped quench
escalates the whole station to EMERGENCY (see GLOSSARY.md's **Critical
safety flag**) rather than holding any concerned VI — no VI, including this
one, names `quench` in `safety_concerns()`, since a per-VI hold would be
meaningless once EMERGENCY has already stopped everything. `MagnetBase.
safety_concerns()` declares every magnet dependent only on `{"helium_low"}`
— a low-helium condition (reported by the level meter, not this VI) places
a safety hold on every magnet, refusing manual control and failing any run
that claims one, without touching an unconcerned instrument (see
GLOSSARY.md's **Safety hold**).

## Interface contract
All classes here extend `SuperconductingMagnetVI` (itself inheriting from
`MagnetBase` and `RampableVI` defined in `virtual_instruments/base.py`
and `virtual_instruments/rampable.py`).

## How to add a new magnet VI
1. Subclass `SuperconductingMagnetVI`.
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
  Key API: `@monitored magnet_field_T` / `magnet_current` / `psu_current` /
  `magnet_status` / `magnet_state`, `@control set_field`, the `RampableVI`
  methods, `evaluate_safety()`. tests:
  `tests/test_l1_new_vis.py` (`TestSuperConductingMagnetVI`),
  `tests/test_l1_virtual_instruments.py`.
- `__init__.py` — package marker. tests: none.
