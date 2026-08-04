# scripts/

## Purpose

Standalone developer tools that drive the CryoSoft package from outside it —
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
| `run_scenario.py` | Launches the real CryoSoft GUI with a `tests/scenarios.py` hazard/fault scenario pre-armed on the just-built Station, via `cryosoft.main.main()`'s `on_station_built` hook — for watching what the app actually allows/refuses in a given state, live, instead of only asserting it in `tests/test_scenarios.py`. |

## How to add a new module

A new dev tool goes here as its own script with the standard module
docstring (Input/Process/Output). If it needs a scenario `tests/scenarios.py`
doesn't have yet, add the scenario there first (it's the shared vocabulary
both the test suite and these scripts draw from), then use it here.
