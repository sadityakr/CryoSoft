# Setup: Simulated transport cryostat  <!-- confirmed by the shipped example, 2026-09-06 -->

## Identity
- Station: none — a fully simulated transport station, the first worked example
- Config: `sim_cryostat`
- Responsible humans: whoever runs the sim; nothing here can be damaged

## Instruments and their purposes
One row per `real_drivers` entry. "Purpose" is the physics role, phrased so a
newcomer understands what breaks when this instrument fails.

| Alias (devices.yaml) | Instrument | Purpose | Address | Physical location/cabling |
|---|---|---|---|---|
| ips_z | SimOxfordIPS120 | applies the field the resistance is measured against | `SIM::IPS_Z` | sim; its own private sim environment |
| lakeshore_vti | SimLakeshore335 | holds the sample temperature | `SIM::LS335` | sim |
| keithley_6221 | SimKeithley6221 | sources the DC excitation current | `SIM::K6221` | sim; paired with the 2182A by the DC measurement VI |
| keithley_2182a | SimKeithley2182A | reads the sample voltage | `SIM::K2182A` | sim |

## Wiring and cabling notes
The source and the voltmeter are the two halves of one four-terminal DC
resistance measurement (`dc_measurement`); no other instrument shares a
bus or a sim environment.

## Safe testing limits (overrides)
None: nothing here is physical. The config's `max_source_current_A`
(105 mA, the 6221's own maximum) exists to exercise the excitation-ceiling
standard, not to protect a sample.

| Instrument | Limit | Reason |
|---|---|---|
| — | — | — |

## Known quirks
Dated, newest first.

- 2026-09-06: the sim magnet quenches if its switch heater is energised
  across a PSU/coil current mismatch — deliberate, so a wrong ramp order
  fails in a test rather than on hardware.

## Safety notes
None — simulated. A real setup records here what energising each output
can do, and whom to call.

## Not commissioned / open TODOs
Nothing: the sim exists to be run without commissioning. A real setup
starts from this file with real addresses and the `setup-commission` skill.
