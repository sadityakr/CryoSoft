# System primer — what a diagnosing agent must know about I2AS

## The stack, and which fault class lives where

I2AS is a seven-layer PyQt6 measurement-station framework. Faults localize
to layers, and the whole point of triage is to name the layer:

| Layer | What it is | Typical fault class |
|---|---|---|
| L0 Driver (`cryosoft/drivers/`) | One Python class per instrument, PyVISA underneath; `sim_*` twins mirror the API | wrong command string, parsing bug, missing waits |
| L1 Virtual Instrument (`cryosoft/virtual_instruments/`) | Physics-level device built on driver(s); `@monitored`/`@control` methods | unit conversion, ramp logic, tolerance settings |
| L2 Station + Config (`cryosoft/core/station.py`, `cryosoft/configs/<name>/`) | Builds everything from `devices.yaml`; stale-cache on comm errors | wrong address, wrong class, bad init_params |
| L3 Orchestrator | Tick loop (polling, ramps, safety) | tick interval vs instrument response time |
| L4 Procedures | Measurement logic | parameter/sweep logic |
| L5 Data manager | HDF5 | not instrument-facing |
| L6 Session + GUI | experiments, runs, notebook, agent gateway, windows | not instrument-facing |
| — Physical | cables, power, address switches, the instrument itself | everything software cannot see |

Below L0 sits the physical world. `ADDRESS_NOT_ON_BUS`, `NO_RESPONSE`, and
`WRONG_IDN` usually live there; `DRIVER_ERROR` and `GARBLED_RESPONSE` usually
live in L0; `CONFIG_INVALID` lives in L2.

## Where evidence lives

The log directory is `cryosoft.core.paths.log_directory()` — the per-user
state directory's `logs/` folder unless `CRYOSOFT_LOG_DIR` overrides it.

| Artifact | Path | What it holds |
|---|---|---|
| Runtime log | `<log directory>/cryosoft.log` (+ rotated `.1`…`.5`) | DEBUG-level everything: VI calls, comm errors, state changes, safety events |
| Operational-status log | `<log directory>/status.jsonl` | one record per tick of the RUNNING app; `troubleshoot status` reads it |
| Troubleshoot transcript | `<log directory>/troubleshoot.jsonl` | one JSON line per past diagnostic command (ts, argv, ok, payload) |
| Incident reports | `<log directory>/incidents/*.md` | the "cannot conclude" exits of earlier diagnoses |
| Setup documentation | `<config dir>/setup.md` | instrument purposes, wiring, known quirks, safe-test limits |
| Instrument cheat sheets | `<config dir>/manuals/notes/<instrument>.md` | command set, timing requirements, limits (from the manual) |
| Full manuals | `<config dir>/manuals/*.pdf` | escalation path when the cheat sheet is silent |
| Vocabulary | `GLOSSARY.md` | canonical terms (two meanings of `vi_type`, FaultCode, etc.) |
| Working log | `LOGBOOK.md` (project root, optional, untracked) | what changed recently, if the contributor keeps one |

## The troubleshoot CLI

`python -m cryosoft.troubleshoot <subcommand> [--json]` — full table in
`cryosoft/troubleshoot/README.md`. Key facts: one-shot commands, exit 0 =
all OK / 1 = any fault, `--json` for parsing, every invocation appends to the
transcript. Run only while the main app is closed, except `status` and
`session`, which read files and are safe with the app open. Read-only
verbs: `scan`, `probe`, `check`, `bench-l0`, `methods`, `idn`, `read`,
`trends`. Gated verbs (permission prompt + safe-testing rules): `write`,
`query`, `send`.

The `FaultCode` taxonomy is documented in the README and in
`cryosoft/troubleshoot/engine.py` (each code's likely physical causes).

## Rules for software fixes

1. `make check` (ruff + import contracts + pytest in both thread modes)
   must pass before a fix is done. CI runs the same targets.
2. Fix at the layer where the fault is. A driver bug is fixed in the driver,
   not worked around in a VI or procedure.
3. Conformance tests (`tests/test_conformance.py`) auto-cover every driver,
   VI, procedure, config and recipe. If one fails on your fix, the fix is
   wrong — never the test.
4. The numbered import contracts in `pyproject.toml` are inviolable; a fix
   that needs a contract change is a design question for the human.
5. Sim/real driver API parity is enforced: changing a real driver's public
   API means changing its `sim_` twin identically.
6. Every fix ends with a dated entry in the config's `setup.md` if the cause
   was a property of the setup, and a `LOGBOOK.md` entry if one is kept,
   including the actual test result.
