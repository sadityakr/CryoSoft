# Setup: Simulated widefield-imaging station  <!-- confirmed by the shipped example, 2026-09-06 -->

## Identity
- Station: none — a fully simulated imaging station, the second worked example
- Config: `sim_imaging`
- Responsible humans: whoever runs the sim; nothing here can be damaged

## Instruments and their purposes
One row per `real_drivers` entry. "Purpose" is the physics role, phrased so a
newcomer understands what breaks when this instrument fails.

| Alias (devices.yaml) | Instrument | Purpose | Address | Physical location/cabling |
|---|---|---|---|---|
| ips_z | SimOxfordIPS120 | applies the out-of-plane field the sample's domains switch in | `SIM::IPS_Z@imaging` | sim; publishes its current to the `imaging` sim environment |
| xy_stage | SimXYStage | positions the sample under the objective (both axes) | `SIM::XYSTAGE` | sim; `stage_x` and `stage_y` are its two axes |
| widefield_camera | SimCamera | images the domain pattern (magneto-optical contrast) | `SIM::CAMERA@imaging` | sim; reads the field from the `imaging` sim environment |

## Wiring and cabling notes
The `@imaging` suffix on the magnet's and the camera's addresses is the
whole coupling: both join the same sim environment, whose coil constant
(10 A/T, matching `magnet_z`'s `amperes_per_tesla`) turns the PSU current
into the field the simulated sample sees. Drop the suffix from either and
the sample stops responding to the magnet. The stage joins no environment.

## Safe testing limits (overrides)
None: nothing here is physical. The config's limits (±10 mm travel per
stage axis, 10 µs–10 s exposure) exist to exercise the control-validation
standard, not to protect hardware.

| Instrument | Limit | Reason |
|---|---|---|
| — | — | — |

## Known quirks
Dated, newest first.

- 2026-09-06: the simulated sample is born saturated at m = -1 and is
  shared by every SimCamera instance (fixed seed); `FieldImaging`'s
  saturation pre-step is what makes a run's first frame reproducible
  regardless of what an earlier run left behind.

## Safety notes
None — simulated. A real imaging setup would record here what energising
the magnet does to the objective and the stage, and whom to call.

## Not commissioned / open TODOs
Nothing: the sim exists to be run without commissioning. A real setup
starts from this file with real addresses and the `setup-commission` skill.
