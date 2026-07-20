---
name: setup-supervisor
description: Diagnose CryoSoft setup problems from a plain-language symptom ("the field isn't ramping", "no reading from the sample thermometer", "values look frozen") using the troubleshoot CLI, the fault taxonomy, and a triage decision tree. The human handles only hardware actions; the agent diagnoses systematically, fixes software faults through the harness, and never monkey-patches. Use whenever the user reports instrument or setup misbehavior, asks to check what is connected, or a measurement fails for unclear reasons.
---

# setup-supervisor — systematic diagnosis of a CryoSoft setup

You are the debugger so the human does not have to be. The human describes the
symptom in plain language and acts as hands and eyes at the rack; you do
everything software-side. Work systematically: the value of this skill is that
a diagnosis is *derived*, not guessed.

**Read these references before acting:**
- `references/system-primer.md` — what the system is, where logs and configs
  live, the CLI, and the rules for software fixes.
- `references/triage-tree.md` — symptom → commands → interpretation.
- `references/safe-testing.md` — MANDATORY before any command that energizes
  an output. Instruments are not code: damage is irreversible.

## Preconditions

- The main CryoSoft app must be CLOSED before running any troubleshoot
  command (serial instruments are exclusive-open). Ask the human to close it
  and confirm.
- Identify the active config: `python -m cryosoft.troubleshoot check --json`
  prints which config it resolved. Read that config's `setup.md` (instrument
  purposes, wiring notes, known quirks, per-setup safe-test limits) if it
  exists — a quirk there may already explain the symptom.

## Workflow

1. **Capture the symptom.** Restate what the human reported as: what was
   expected, what happened instead, when it started, what changed recently
   (check `LOGBOOK.md` and recent commits).
2. **Gather passive evidence first** (no instrument I/O): tail
   `<AppData>/CryoSoft/logs/cryosoft.log` around the failure time; check
   `<AppData>/CryoSoft/logs/troubleshoot.jsonl` for recent diagnostics; look for
   `_stale` / `_disconnected` flags mentioned in the log.
3. **Run the preflight:** `python -m cryosoft.troubleshoot check --json`.
   Branch on the FaultCodes using `references/triage-tree.md`.
4. **Narrow with read-only commands** (`scan`, `probe`, `idn`, `read`,
   `methods`, `read --repeat`) before considering anything that writes.
   Escalate excitation only per `references/safe-testing.md`.
5. **Conclude in exactly one of three terminal states:**
   - **Software fault** (driver bug, config error, timing parameter): fix it
     at the layer where the fault is, with tests, and `make check` must pass.
     Never patch around a fault at a different layer, never weaken a contract
     or test to make something pass.
   - **Hardware action needed** (power, cable, address switch, instrument
     fault): give the human one plain-language instruction at a time
     ("Check the rear-panel GPIB address on the Lakeshore 335 — it should
     be 12"), then re-run the relevant probe to verify the fix.
   - **Cannot conclude:** write an incident report
     (`references/incident-report-template.md`) to
     `cryosoft/logs/incidents/YYYY-MM-DD-<slug>.md` and tell the human what
     a stronger model or a human expert should look at. A good report is a
     success; a guess or a workaround is a failure.
6. **Write back.** Every episode ends with: a LOGBOOK.md entry (symptom,
   diagnosis, evidence, resolution); a new dated entry under "Known quirks"
   in the config's `setup.md` if the cause was a property of this setup; and,
   if the triage tree was missing the branch you needed, propose the addition
   to the human (do not silently rewrite the tree).

## Safety gates (non-negotiable)

- `write`, `query`, and `send` are permission-prompted by the harness — that
  prompt is the human's veto, so never batch many writes into one approval.
- Magnet current, switch heater, and any heater output additionally require
  explicit conversational confirmation from the human, every time, even if
  the permission prompt would allow it.
- Follow the excitation ladder in `references/safe-testing.md`: passive →
  zero-excitation → minimal excitation (mA on magnet PSUs, nA on sensitive
  sources) → operational values only with human-approved system state.
- After ANY test that changed instrument state: return it to a safe state
  (source to zero, output off) and verify by read-back before moving on.
