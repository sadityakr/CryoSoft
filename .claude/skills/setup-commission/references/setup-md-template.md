# setup.md template

Copy into the config directory as `setup.md`. This file is the per-setup
ground truth agents read before touching anything. Keep it current: every
diagnosed quirk gets a dated entry; stale information here misleads every
future diagnosis. The shipped `cryosoft/configs/sim_cryostat/setup.md` and
`cryosoft/configs/sim_imaging/setup.md` are two filled-in examples.

```markdown
# Setup: <station name>  <!-- confirmed by <name> on YYYY-MM-DD -->

## Identity
- Station: <what it is — the cryostat, microscope, probe station; location, room>
- Config: <config dir name>
- Responsible humans: <name(s), contact>

## Instruments and their purposes
One row per real_drivers entry. "Purpose" is the physics role, phrased so a
newcomer understands what breaks when this instrument fails.

| Alias (devices.yaml) | Instrument | Purpose | Address | Physical location/cabling |
|---|---|---|---|---|
| magnet_z | <make, model> | applies the field the sample is measured in | ASRL10::INSTR | rack top; serial via USB adapter #2 |

## Wiring and cabling notes
Anything an agent cannot see but needs to reason about hardware handoffs:
which GPIB chain order, shared bus lines, which USB-serial adapter maps to
which COM port, sample wiring status, what sits above the sample stage.

## Safe testing limits (overrides)
Only where this setup needs values different from the defaults in
setup-supervisor/references/safe-testing.md. The more conservative value
always wins.

| Instrument | Limit | Reason |
|---|---|---|

## Known quirks
Dated, newest first. Every diagnosed setup-property fault lands here.

- YYYY-MM-DD: <quirk> (found while <context>; evidence: <log/transcript ref>)

## Safety notes
What energising each output can do to the sample, the instrument or the
person at the rack; which flags the VIs declare critical; whom to call.

## Not commissioned / open TODOs
Instruments or checks that are not verified; placeholder addresses; missing
manuals.
```
