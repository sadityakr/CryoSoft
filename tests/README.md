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

## How agents run the suite

The suite is large; run the narrow slice while iterating and the full gate only
before handing work back. Paths use the project `.venv`.

1. **While iterating, run only the owning test file:**
   `./.venv/Scripts/python.exe -m pytest tests/test_<area>.py -q`.
2. **Full check before handing back:**
   `./.venv/Scripts/python.exe -m pytest -m "not hardware" -q --tb=no`, and read
   only the tail: the FAILED list and the summary line.
3. **On failure:** `./.venv/Scripts/python.exe -m pytest --lf --tb=short -q`
   reruns only the failed tests with short tracebacks.
4. **Report the summary line plus failed test IDs upward**; never paste full
   passing output.

### Which test file owns which source folder (routing table)

Editing a source file? Run its owner first. Every driver / VI / procedure /
config also has automatic `test_conformance.py` coverage on top of these.

| Editing... | Owning test file(s) |
|------------|---------------------|
| `cryosoft/drivers/*` | `tests/test_l0_simulated.py`, `tests/test_l0_new_drivers.py`, `tests/test_l0_switch_driver.py` |
| `cryosoft/virtual_instruments/*` | `tests/test_l1_virtual_instruments.py`, `tests/test_l1_new_vis.py`, `tests/test_l1_switch_vi.py`, `tests/test_measurement_dc_vi.py` |
| `cryosoft/core/station.py`, config loading | `tests/test_l2_station.py`, `tests/test_config_validation.py`, `tests/test_config_catalog.py` |
| `cryosoft/core/orchestrator.py` | `tests/test_l3_orchestrator.py` |
| `cryosoft/core/procedure.py`, `cryosoft/procedures/*` | `tests/test_l4_procedure.py`, `tests/test_new_procedures.py`, `tests/test_field_voltage_procedure.py` |
| `cryosoft/core/plan.py` | `tests/test_plan.py` |
| `cryosoft/core/sweep_builder.py` | `tests/test_sweep_builder.py` |
| `cryosoft/core/data_manager.py` (L5) | `tests/test_l5_data_manager.py` |
| `cryosoft/gui/param_form.py`, `monitor_window.py`, `procedure_window.py`, `instrument_panel.py`, `notification_banner.py`, `theme.py`, `live_plot_panel.py`, `app_settings.py` | `tests/test_gui.py` |
| `cryosoft/gui/sweep_axis_widget.py` | `tests/test_sweep_axis_widget.py` |
| `cryosoft/gui/lifecycle_toggle.py` | `tests/test_lifecycle_toggle.py` |
| `cryosoft/gui/session.py` | `tests/test_session.py` |
| `cryosoft/gui/monitor_history.py` | `tests/test_monitor_history.py` |
| `cryosoft/gui/trend_plot_panel.py` | `tests/test_trend_plot_panel.py` |
| `cryosoft/gui/config_editor.py` | `tests/test_config_editor.py` |
| `cryosoft/troubleshoot/*`, operational status / watchdog | `tests/test_troubleshoot_cli.py`, `tests/test_troubleshoot_engine.py`, `tests/test_operational_status.py`, `tests/test_status_reader.py`, `tests/test_watchdog.py` |

## Files

- `conftest.py` — shared fixtures (logging setup).
- `mocks/` — shared mock objects.
- `test_foundation.py` — core exceptions, decorators, logging config.
- `test_conformance.py` — auto-discovering interface conformance (see above).
- **L0 drivers:** `test_l0_simulated.py`, `test_l0_new_drivers.py`, `test_l0_switch_driver.py`.
- **L1 virtual instruments:** `test_l1_virtual_instruments.py`, `test_l1_new_vis.py`, `test_l1_switch_vi.py`, `test_measurement_dc_vi.py`, `test_switch_heater.py`.
- **L2 station + config:** `test_l2_station.py`, `test_config_validation.py`, `test_config_catalog.py`.
- **L3 orchestrator:** `test_l3_orchestrator.py`.
- **L4 procedures + planning:** `test_l4_procedure.py`, `test_new_procedures.py`, `test_field_voltage_procedure.py`, `test_plan.py`, `test_sweep_builder.py`.
- **L5 data manager:** `test_l5_data_manager.py`.
- **GUI (pytest-qt, offscreen):** `test_gui.py`, `test_sweep_axis_widget.py`, `test_lifecycle_toggle.py`, `test_session.py`, `test_monitor_history.py`, `test_trend_plot_panel.py`, `test_config_editor.py`.
- **Troubleshooting / operational status:** `test_troubleshoot_cli.py`, `test_troubleshoot_engine.py`, `test_operational_status.py`, `test_status_reader.py`, `test_watchdog.py`.
