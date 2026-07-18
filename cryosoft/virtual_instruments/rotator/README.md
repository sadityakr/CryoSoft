# virtual_instruments/rotator/

## Purpose
Virtual instruments for motorized sample-rotation stages, used with a
uniaxial or 2D magnet to set the sample's orientation relative to the field.
Abstracts away the model-specific driver so procedures interact with
`RotatorVI` regardless of which rotation stage is installed.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
A driver dict `{"main": <rotation stage real driver>}` and optional
`init_params`: `default_rate_deg_per_min`, and the setup limit keys of the
control-validation standard: `min_angle_deg`, `max_angle_deg`,
`max_rate_deg_per_min` (missing keys mean unbounded on that side).

## Exit (what goes out)
`@monitored` readings: `get_sample_angle() → float (deg)`,
`get_rate_sample_angle() → float (deg/min)`, `rotator_status() → str`.
`@control` actions: `set_sample_angle(target_deg)`,
`set_rate_sample_angle(rate_deg_per_min)` — both bounded by the setup's
limits via the control-validation standard (`control_limits`); an
out-of-range value raises `CryoSoftSafetyError` before any hardware command.
`RampableVI` interface: `start_ramp()`, `advance_ramp()`, `ramp_status()`,
`stop_ramp()` (kills the generator AND commands a hardware hold — used by
the Orchestrator on abort/pause/error).

## Interface contract
All classes here extend `RotatorVI` (itself inheriting from `RotatorBase`
and `RampableVI` defined in `virtual_instruments/base.py` and
`virtual_instruments/rampable.py`).

## How to add a new rotator VI
1. Subclass `RotatorVI` (or `RotatorBase` directly for a stage with a
   materially different ramp behaviour).
2. Override only the methods that differ from the base behaviour.
3. Follow the control-validation standard (see `BaseVirtualInstrument`):
   declare `control_limits` for any new bounded `@control` parameter and
   populate `self._limits` from `init_params`.
4. Add the new class to `devices.yaml` (`vi_type: system`) using the full
   dotted path.
5. Add tests to `tests/test_l1_new_vis.py` (the conformance tests cover the
   limits contract automatically).

## Files
- `rotator.py` — `RotatorVI`: status-driven angle ramp; exposes exactly two
  controls/properties, sample angle and its rotation rate. Key API:
  `@monitored get_sample_angle` / `get_rate_sample_angle` / `rotator_status`,
  `@control set_sample_angle` / `set_rate_sample_angle`, the `RampableVI`
  methods. tests: `tests/test_l1_new_vis.py` (`TestRotatorVI`).
- `__init__.py` — package marker. tests: none.
