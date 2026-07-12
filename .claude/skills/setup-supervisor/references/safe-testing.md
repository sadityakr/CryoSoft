# Safe testing — nondestructive diagnosis rules

**Core principle: unlike code, instrument and sample damage is irreversible.**
Every diagnostic must use the minimum energy that answers the question, and
most questions can be answered with zero energy. Applying an operational-scale
current or field to a system whose state you have not verified is never a
diagnostic — it is a gamble with someone's experiment.

## The excitation ladder

Climb one rung at a time. Each rung requires that the rung below passed. Most
diagnoses end at L0 or L1.

| Rung | What it is | Examples | Approval |
|---|---|---|---|
| **L0 — passive** | Read-only queries; nothing changes on the instrument | `check`, `scan`, `idn`, `read get_*`, `read --repeat` | none |
| **L1 — zero-excitation functional** | Change a *setting*, verify by read-back, output stays off/zero | `write set_range 0.1` then `read get_range`; configure a source with output disabled | permission prompt |
| **L2 — minimal excitation** | Smallest meaningful output to prove a signal path | values table below | permission prompt + state the value and why in chat first |
| **L3 — operational values** | Real currents/fields/temperatures | ramp tests, heater tests | explicit human confirmation of value AND system state, every time |

L1 is the workhorse: a successful set-then-read-back proves communication,
parsing, and the instrument's command handling without energizing anything.
Prefer it over any excitation whenever it answers the question.

## L2 values per instrument category

Defaults, deliberately conservative. A config's `setup.md` may override them
under "Safe testing limits" (human-approved); the more conservative value wins
when in doubt.

| Category (example) | L2 test value | Rationale |
|---|---|---|
| Superconducting magnet PSU (Mercury iPS, IPS120) | ≤ 10 mA lead current | ~1.3 mT on this setup (7.954 A/T) — negligible field, proves the PSU sources and reads back |
| Precision current source (Keithley 6221) | ≤ 1 nA, smallest range | proves sourcing five orders below full scale; sample-safe for most devices |
| Nanovoltmeter (Keithley 2182A) | none — passive | reading voltage is already zero-excitation |
| Temperature controller (ITC503, Lakeshore 335) | setpoint change ≤ 1 K *from the current temperature*, then restore | proves the control loop path without a thermal excursion |
| Needle valve / gas flow | ≤ 5 % step from current position, then restore | avoids pressure/flow transients |
| Cryogen level meter (ILM200) | none — passive | |
| Switch heater (persistent magnets) | **never in diagnostics** | heater cycling has quench implications; L3 only, human present |

## Rules

1. **Zero first.** If the question is "does it communicate / is it the right
   instrument / does the driver parse correctly", the answer never requires
   output. L0/L1 only.
2. **No excitation without a reason.** Before any L2 command, state in chat:
   the question being answered, the value, and why a lower rung cannot answer
   it. If you cannot articulate that, you do not need the test.
3. **Know what is downstream.** Before sourcing anything, ask the human what
   is connected (sample? shorting plug? open leads?). A 1 nA test is safe for
   the instrument but may not be safe for a fragile junction. If the human is
   unsure, do not source.
4. **Return to safe state, verified.** After every L1/L2 test: source to
   zero, output off, restored setpoints — confirmed by read-back, not
   assumed. A diagnostic session must leave the instrument exactly as found.
5. **One write per approval.** Never bundle several state-changing commands
   behind one permission prompt; each prompt is the human's veto point.
6. **Raw `query`/`send` count as writes.** Arbitrary bytes can change state
   (and a wrong-baud byte stream can confuse a serial instrument), so they sit
   on the gated side even when the command "should" be a read.
7. **When unsure, stop.** An unanswered diagnostic question is recoverable;
   a quenched magnet or a cooked sample is not. Write the incident report and
   hand the decision to the human.
