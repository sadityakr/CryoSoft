# scripts/

## Purpose

Standalone developer tools that drive the package from outside it —
launchers and one-off utilities, not part of the application or the test
suite.

## Architecture layer

None — outside the seven-layer stack. Free to import both `cryosoft.*` and
`tests.*`, which neither of those may do to each other.

## Entry

Run directly from the repo root with the project `.venv`, e.g.:
`.venv/Scripts/python.exe scripts/run_scenario.py --help`.

## Exit

Whatever the individual script produces — `run_scenario.py` launches a GUI
window and returns nothing until it's closed.

## Interface contract

A script here may import `cryosoft.*` (production code) and `tests.*`
(shared test helpers, e.g. `tests/scenarios.py`) freely — the reverse is
forbidden: `cryosoft/` must never import from `tests/` or `scripts/`.

## Files

| File | Role |
|------|------|
| `soak_instrument_thread.py` | Runs the real Monitor and Procedure windows against a real `InstrumentHost` under a scripted click storm for N minutes and reports the worst GUI-thread stall (`--mode both` runs inline and threaded one after the other for comparison). The long-running counterpart to `tests/test_instrument_thread.py`'s frozen-GUI detector: that proves the property in two seconds, this is what catches a stall that only appears after a thousand ticks. Needs `CRYOSOFT_MEASUREMENT_ROOT` set, because the windows read one. |
| `run_scenario.py` | Launches the real GUI with a `tests/scenarios.py` hazard/fault scenario pre-armed on the just-built Station, via `cryosoft.main.main()`'s `on_station_built` hook — for watching what the app actually allows/refuses in a given state, live, instead of only asserting it in `tests/test_scenarios.py`. |

## How to add a new module

A new dev tool goes here as its own script with the standard module
docstring (Input/Process/Output). If it needs a scenario `tests/scenarios.py`
doesn't have yet, add the scenario there first (it's the shared vocabulary
both the test suite and these scripts draw from), then use it here.
