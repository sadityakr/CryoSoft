---
name: setup-commission
description: Bring a new cryostat setup (or a changed instrument rack) into CryoSoft - interview the human, write the config folder's setup.md, build/extend devices.yaml with expect_idn identity checks, iterate the troubleshoot preflight until green, bench each instrument at safe excitation levels, and deliver a signed-off preflight report. Use when the user says "set up CryoSoft on this system", "commission the new setup", "we added/replaced an instrument", or a fresh install must be verified before first measurements.
---

# setup-commission — bringing a setup into CryoSoft

Goal: after this workflow, the setup has a green preflight, a `setup.md` that
future agents can trust, and an honest record of what was and was not
verified. The human does hardware and answers questions; you do everything
else.

**Prerequisites:** read `../setup-supervisor/references/system-primer.md` and
`../setup-supervisor/references/safe-testing.md`. The safe-testing ladder
binds every step here. The main app must be closed while commissioning.

## Workflow

### 1. Interview
Ask the human, in one batch: which instruments (make + model), what each one
does physically (which magnet axis, which thermometer, sample vs VTI), how
each is connected (GPIB address / COM port, as far as known), what is
currently wired downstream of every source (sample? shorting plug? open
leads?), and any known oddities. Missing answers become explicit TODOs, not
guesses.

### 2. Draft setup.md — the human signs it
Copy `references/setup-md-template.md` into the config directory as
`setup.md` and fill it from the interview. Present it to the human for
correction and explicit confirmation before proceeding: this file becomes
ground truth for every future agent, so an error here propagates forever.
Record the confirmation date in the file header.

### 3. Manuals
Ask for PDF manuals for each instrument → `<config>/manuals/` (gitignored).
For each manual, offer to extract a cheat sheet to
`<config>/manuals/notes/<instrument>.md` (command set, serial parameters,
identify command, timing requirements, hard limits). Cheat-sheet extraction
is a supervised step: the human spot-checks it, because a wrong cheat sheet
misleads every later diagnosis.

### 4. Config
Create or extend `devices.yaml` (copy the nearest shipped config as the
starting point; addresses and limits belong in YAML, never in code). For
every driver entry add `expect_idn` — but only after step 5 has shown the
real identity string; never invent one. New instrument without a driver?
That is the separate driver-development workflow (sim first, tests, then
real; see project CLAUDE.md §8) — commission pauses until the driver exists.

### 5. Preflight loop
`python -m cryosoft.troubleshoot check --config <name> --json`, then fix
faults one at a time, following the supervisor triage tree
(`../setup-supervisor/references/triage-tree.md`): config errors yourself,
hardware steps as one plain-language instruction at a time to the human.
After each fix, re-run. Record each real IDN string as the entry's
`expect_idn` (substring, e.g. "2182A"). Loop until every driver reports OK.

### 6. Per-instrument bench, safe levels only
For each instrument, in this order and stopping at the lowest rung that
answers it:
- L0: `idn`, `read` every getter once; values physically plausible?
- L1: one set-then-read-back on a harmless setting (range, setpoint echo).
- L2 (only where a signal path must be proven, with the human aware of what
  is downstream): the safe-testing table values — ≤10 mA magnet lead
  current, ≤1 nA on precision sources, ≤1 K setpoint moves. Restore and
  verify restoration after every test.
- `read <getter> --repeat 10` on the slowest instrument to catch timing
  flakiness early — better now than mid-cooldown.

### 7. Preflight report and handover
Write the report to `cryosoft/logs/incidents/YYYY-MM-DD-commissioning-<name>.md`
(same folder as incident reports, it is the positive counterpart):
- table of instruments: address, IDN, FaultCode history during commissioning,
  bench rung reached;
- **verified** vs **not verified** — be explicit: "responds with correct IDN"
  is necessary, not sufficient; sample wiring, field calibration, and
  thermometry accuracy are NOT proven by this workflow;
- open TODOs (missing manuals, placeholder addresses, L2 tests skipped).
Then: LOGBOOK.md entry, commit the config + setup.md (manuals stay
gitignored), and tell the human the setup's honest status in two sentences.

## Failure discipline

If commissioning stalls (an instrument never comes up), do not park it
silently: write the incident report, mark the instrument `not commissioned`
in setup.md, and make sure `check` output shows the human the same truth.
