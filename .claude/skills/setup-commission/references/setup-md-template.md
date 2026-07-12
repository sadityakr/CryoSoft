# setup.md template

Copy into the config directory as `setup.md`. This file is the per-setup
ground truth agents read before touching anything. Keep it current: every
diagnosed quirk gets a dated entry; stale information here misleads every
future diagnosis.

```markdown
# Setup: <cryostat name>  <!-- confirmed by <name> on YYYY-MM-DD -->

## Identity
- Cryostat: <model, location, room>
- CryoSoft config: <config dir name>
- Responsible humans: <name(s), contact>

## Instruments and their purposes
One row per real_drivers entry. "Purpose" is the physics role, phrased so a
newcomer understands what breaks when this instrument fails.

| Alias (devices.yaml) | Instrument | Purpose | Address | Physical location/cabling |
|---|---|---|---|---|
| magnet_x | Oxford Mercury iPS-M | powers the x-axis superconducting coil | ASRL10::INSTR | rack top; serial via USB adapter #2 |

## Wiring and cabling notes
Anything an agent cannot see but needs to reason about hardware handoffs:
which GPIB chain order, shared ISOBUS lines, which USB-serial adapter maps to
which COM port, sample wiring status.

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
Quench behavior, cryogen handling contacts, anything that must be known
before energizing outputs.

## Not commissioned / open TODOs
Instruments or checks that are not verified; placeholder addresses; missing
manuals.
```
