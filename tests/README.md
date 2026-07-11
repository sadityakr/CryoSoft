# tests/

## Purpose

The full test suite for CryoSoft. Two kinds of tests live here:

1. **Layer tests** (`test_l0_*` … `test_l5_*`, `test_gui.py`, and feature
   tests): behavior tests for each architecture layer, written against the
   simulated drivers so everything runs without hardware.
2. **Conformance tests** (`test_conformance.py`): auto-discovering interface
   checks. They iterate over the drivers, VI, procedures, and configs packages
   at runtime, so any *new* module is tested automatically the moment it
   exists. This is the safety net that lets coding agents add drivers and
   procedures without silently breaking the system's contracts.

Layer *import* boundaries are not tested here — they are enforced by
import-linter (`make contracts`, config in `pyproject.toml`).

## Architecture layer

Cross-cutting: tests exist for every layer, L0 through the GUI.

## Entry (what comes in)

`pytest` (or `make test`) discovers everything in this folder per
`[tool.pytest.ini_options]` in `pyproject.toml`. Tests import `cryosoft`
directly and use the simulated drivers plus the `sim_cryostat` config; no
hardware, network, or display is needed (GUI tests run offscreen in CI via
`QT_QPA_PLATFORM=offscreen`).

## Exit (what goes out)

Pass/fail results. Some tests write temporary HDF5 files via pytest's
`tmp_path` fixture; nothing is written into the repository.

## Interface contract

Tests requiring physical instruments must be marked `@pytest.mark.hardware`;
`make test` and CI exclude them. Everything else must pass on a bare machine.
Shared fixtures belong in `conftest.py`.

## How to add a new module

- **Testing a new driver / VI / procedure:** you get conformance coverage for
  free, but still add a behavior test file (`test_<feature>.py`) exercising
  what the module actually does, using its sim driver.
- **A conformance test fails on your new module:** fix the module to match the
  contract (the assertion message says what is expected). Do not weaken or
  special-case the conformance tests.
- **Adding a new contract:** extend `test_conformance.py` with a discovery
  helper + parametrized test, and document the contract in GLOSSARY.md.

## Files

- `conftest.py` — shared fixtures (logging setup).
- `test_foundation.py` — core exceptions, decorators, logging config.
- `test_l0_simulated.py` / `test_l0_new_drivers.py` — L0 driver behavior.
- `test_l1_virtual_instruments.py` / `test_l1_new_vis.py` — L1 VI behavior.
- `test_l2_station.py` — Station registry, config loading, dispatch.
- `test_l3_orchestrator.py` — Orchestrator state machine and tick loop.
- `test_l4_procedure.py` — BaseProcedure interface.
- `test_l5_data_manager.py` — HDF5 data layer.
- `test_measurement_dc_vi.py`, `test_new_procedures.py` — feature tests.
- `test_gui.py` — GUI smoke and interaction tests (pytest-qt).
- `test_conformance.py` — auto-discovering interface conformance (see above).
- `mocks/` — shared mock objects.
