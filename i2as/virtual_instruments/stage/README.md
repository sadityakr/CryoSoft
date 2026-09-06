# virtual_instruments/stage/

## Purpose
Virtual instruments for sample positioning. A stage VI turns "move the
sample here" into a ramp the Orchestrator can wait on, so a procedure that
positions its sample before imaging uses the same `Target` it uses for a
field or a temperature, the Ramps panel shows the move, and the stall
detector watches it — with no new machinery anywhere above L1.

**One VI per axis.** The setpoint every rampable VI carries is one scalar
(`Target.target`, the `target_*` parameter the session envelope binds, the
one `start_ramp(target)` argument), so a two-axis stage is two
`StageAxisVI`s over one driver, each declaring its `axis` in the config —
exactly as a vector magnet is one magnet VI per coil. A procedure asks
`Station.stage_vi_names()` for the setup's axes and tells them apart by
their `axis` attribute.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
A driver dict `{"main": <stage driver>}` — the SAME driver instance for
every axis VI of one stage — and `init_params`: `axis` (`"x"` / `"y"`),
`min_position_m` / `max_position_m` (the travel this setup allows on the
axis; the control-validation standard's limit keys, missing means
unbounded on that side), `speed_m_per_s` (applied at `initiate()`),
`tolerance_m` (how close counts as arrived).

## Exit (what goes out)
`@monitored` readings: `position() → float (m)`, `motion_state() → str`
(`"moving"` / `"holding"`).
`@control` actions: `set_position(target_m)` (`run_control`, bounded by the
axis travel via `control_limits`) and `stop()` (`recovery` — halts the axis
where it is).
`RampableVI` interface: `start_ramp()` commands the driver's per-axis move
in one shot, `advance_ramp()` polls the driver's `is_moving(axis)`,
`ramp_status()` reports `TARGET_REACHED` within `tolerance_m`, `stop_ramp()`
kills the generator AND halts the axis; the ramp-introspection hooks report
the position, the commanded position (equal to the target — a move is one
command), the target and the speed in m/min, so the move appears on the
Ramps panel and in the operational-status record.
**Lifecycle**: `initiate()` sets the configured speed (the one setup command
a stage needs); `standby()` stops where it is — a drive home is a move like
any other and can collide with whatever the operator has in the way.

## Interface contract
All classes here extend `StageBase` (in `virtual_instruments/base.py`:
`vi_type = "stage"`, setpoint label/unit `position`/`m`, an `axis`
attribute) and `RampableVI`. The driver contract a stage VI expects is
written on `StageAxisVI`'s docstring: `move_to(x_m=None, y_m=None)` with
an omitted axis keeping its own move (so two axis VIs never cancel each
other), `get_position()`, `is_moving(axis)`, `stop(axis)`, `set_speed()`.
`tests/test_conformance.py` holds the rest automatically: the exactly-one
`target_*` setpoint control, its bound, the `no_motion_phases` declaration,
silent construction.

## How to add a new stage VI
1. Subclass `StageAxisVI` (a rotator or a z-axis is the same shape with a
   different unit: override `setpoint_unit` and the driver calls).
2. Keep the setpoint scalar — one VI per axis — and read every limit from
   `init_params`.
3. Register one VI per axis in `devices.yaml` (`vi_type: system`), all on
   the same driver alias.
4. Add behaviour tests to `tests/test_l1_camera_stage_vis.py`.

## Files
- `stage_axis.py` — `StageAxisVI`: one positioned axis over a two-axis
  stage driver. Key API: `@monitored position` / `motion_state`,
  `@control set_position` / `stop`, the `RampableVI` methods and the
  ramp-introspection hooks. tests: `tests/test_l1_camera_stage_vis.py`,
  `tests/test_conformance.py`.
- `__init__.py` — package marker. tests: none.
