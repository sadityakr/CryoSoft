# System primer — what a diagnosing agent must know about CryoSoft

## The stack, and which fault class lives where

CryoSoft is a six-layer PyQt6 cryostat control system. Faults localize to
layers, and the whole point of triage is to name the layer:

| Layer | What it is | Typical fault class |
|---|---|---|
| L0 Driver (`cryosoft/drivers/`) | One Python class per instrument, PyVISA underneath; `sim_*` twins mirror the API | wrong command string, parsing bug, missing waits |
| L1 Virtual Instrument (`cryosoft/virtual_instruments/`) | Physics-level device built on driver(s); `@monitored`/`@control` methods | unit conversion, ramp logic, tolerance settings |
| L2 Station + Config (`cryosoft/core/station.py`, `cryosoft/configs/<name>/`) | Builds everything from `devices.yaml`; stale-cache on comm errors | wrong address, wrong class, bad init_params |
| L3 Orchestrator | Tick loop (polling, ramps, safety) | tick interval vs instrument response time |
| L4 Procedures | Measurement logic | parameter/sweep logic |
| L5 Data manager + GUI | HDF5 + display | not instrument-facing |
| — Physical | cables, power, address switches, the instrument itself | everything software cannot see |

Below L0 sits the physical world. `ADDRESS_NOT_ON_BUS`, `NO_RESPONSE`, and
`WRONG_IDN` usually live there; `DRIVER_ERROR` and `GARBLED_RESPONSE` usually
live in L0; `CONFIG_INVALID` lives in L2.

## Where evidence lives

| Artifact | Path | What it holds |
|---|---|---|
| Runtime log | `cryosoft/logs/cryosoft.log` (+ rotated `.1`…`.5`) | DEBUG-level everything: VI calls, comm errors, state changes, safety events |
| Troubleshoot transcript | `cryosoft/logs/troubleshoot.jsonl` | one JSON line per past diagnostic command (ts, argv, ok, payload) |
| Development log | `LOGBOOK.md` (project root) | what changed recently — read before diagnosing, write after |
| Setup documentation | `<config dir>/setup.md` | instrument purposes, wiring, known quirks, safe-test limits |
| Instrument cheat sheets | `<config dir>/manuals/notes/<instrument>.md` | command set, timing requirements, limits (from the manual) |
| Full manuals | `<config dir>/manuals/*.pdf` | escalation path when the cheat sheet is silent |
| Vocabulary | `GLOSSARY.md` | canonical terms (two meanings of `vi_type`, FaultCode, etc.) |

## The troubleshoot CLI

`python -m cryosoft.troubleshoot <subcommand> [--json]` — full table in
`cryosoft/troubleshoot/README.md`. Key facts: one-shot commands, exit 0 =
all OK / 1 = any fault, `--json` for parsing, every invocation appends to the
transcript. Run only while the main app is closed. Read-only verbs: `scan`,
`probe`, `check`, `methods`, `idn`, `read`. Gated verbs (permission prompt +
safe-testing rules): `write`, `query`, `send`.

The `FaultCode` taxonomy is documented in the README and in
`cryosoft/troubleshoot/engine.py` (each code's likely physical causes).

## Rules for software fixes

1. `make check` (ruff + import contracts + pytest) must pass before a fix is
   done. CI runs the same targets.
2. Fix at the layer where the fault is. A driver bug is fixed in the driver,
   not worked around in a VI or procedure.
3. Conformance tests (`tests/test_conformance.py`) auto-cover every driver,
   VI, procedure, and config. If one fails on your fix, the fix is wrong —
   never the test.
4. Import contracts C1–C10 are inviolable; a fix that needs a contract change
   is a design question for the human.
5. Sim/real driver API parity is enforced: changing a real driver's public
   API means changing its `sim_` twin identically.
6. Every fix ends with a LOGBOOK.md entry including the actual test result.
