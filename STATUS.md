# CryoSoft — Implementation Status

> **This is a live document.** The agent reads it before every session and updates it after every task.
> Last updated: 2026-04-19 (live-plot axis redesign: dynamic X/Y selectors, full system-state logging, unix_time per datapoint)

---

## Project phase

**Current phase:** Pre-development (architecture and planning complete)
**Current milestone target:** `v0.1-L0` — Real drivers and base classes
**Architecture version:** v1.1 (docs/architecture_v1.1.docx)

---

## Layer status overview

| Layer | Name                | Status      | Tests | Notes |
|-------|---------------------|-------------|-------|-------|
| L0    | Sim drivers         | Done        | Pass  | Simulated drivers implemented |
| L1    | Virtual instruments | Done        | Pass  | Simulated implementation complete |
| L2    | Station Config      | Done        | Pass  | Ready for Orchestrator |
| L3    | Orchestrator        | Done        | Pass  | 9/9 tests pass |
| L3W   | Monitor / watchdog  | N/A         | N/A   | Merged into L3 (Orchestrator tick + Station.check_safety) in architecture v4.0 |
| L4    | Procedures          | Done        | Pass  | 3 procedures: FieldSweepIV, FieldSweepDC, TemperatureSweepDC. 41/41 tests pass |
| L5    | Data manager        | Done        | Pass  | 17/17 tests pass |
| GUI   | Monitor window      | Done        | Pass  | 24/24 tests pass (redesigned 2026-04-17) |
| GUI   | Procedure window    | Done        | Pass  | Included in test_gui.py (redesigned 2026-04-17) |

---

## Done

- [2026-04-06] L0: Sim Drivers (`sim_oxford_ips120.py`, `sim_oxford_itc503.py`, `sim_oxford_ilm200.py`, `sim_keithley_6221.py`, `sim_keithley_2182a.py`). Unit tests pass (39/39). `tests/test_l0_simulated.py`
- [2026-04-06] L1: Virtual Instruments (`base.py`, `rampable.py`, `magnet_ips120.py`, `temperature_itc503.py`, `level_ilm200.py`, `measurement_delta_mode.py`). Unit tests pass (9/9). `tests/test_l1_virtual_instruments.py`
- [2026-04-06] L2: Station Config (`station.py`, UI configs). Unit tests pass (7/7). `tests/test_l2_station.py`
- [2026-04-06] L3: Orchestrator (`orchestrator.py`). Complete. Tests pass 9/9 (`tests/test_l3_orchestrator.py`). Fix: downgraded PyQt6 to 6.7.1 (6.11.0 DLL conflict with Anaconda on Windows); fixed test ramp rate to set `_default_ramp_rate` on VI rather than `_driver.set_ramp_rate`.
- [2026-04-06] L5: DataManager (`core/data_manager.py`). Complete. Tests pass 17/17 (`tests/test_l5_data_manager.py`).
- [2026-04-06] L4: BaseProcedure (`core/procedure.py`) + FieldSweepIV (`procedures/field_sweep_iv.py`). Complete. Tests pass 19/19 (`tests/test_l4_procedure.py`). Includes full Orchestrator end-to-end loop test.
- [2026-04-06] Architecture clarification: L3W Monitor/Watchdog does not exist in v4.0. Safety monitoring is integrated into Orchestrator._tick() + Station.check_safety().
- [2026-04-06] GUI: MonitorWindow, InstrumentPanel, ProcedureWindow, main.py implemented. 22/22 tests pass. Total suite: 143/143. Added DataManager.last_datapoint and Orchestrator.measurement_ready signal for live plot data pipeline.
- [2026-04-16] GUI refactor — MonitorWindow and ProcedureWindow restructured. Changes (all tests pass, 145/145):
  - Sample Info panel moved from ProcedureWindow → MonitorWindow (session-level metadata belongs on the monitor)
  - ProcedureWindow now opens via Procedures menu (Ctrl+P) from MonitorWindow; lazily created; not shown at startup
  - Real-time Log panel added to MonitorWindow (QTextEdit + _QtLogHandler, colour-coded by level)
  - System/level VI grid pinned always-visible at top of MonitorWindow (no scroll)
  - Other Devices section: measurement VIs shown as status-only cards (Initiate/Standby only; Configure removed — configuration is handled exclusively by procedures)
  - Lower section (Other Devices + Log/Sample Info splitter) wrapped in QScrollArea for small-screen access
  - Log and Sample Info shown side-by-side in a QSplitter (50/50) in the scrollable lower section
  - InstrumentPanel._submit_control: required fields now validated before submission; empty required params show a warning dialog instead of passing None to the VI
- [2026-04-18] L1: DCMeasurementVI (`virtual_instruments/measurement_dc.py`). DC resistance measurement using 6221 + 2182A. `initiate(current_A, compliance_A, voltmeter_range_V)` arms both instruments; `take_reading(n_points)` collects N voltage samples. 14/14 tests pass (`tests/test_measurement_dc_vi.py`). Added `set_compliance()`/`get_compliance()` to `sim_keithley_6221.py`. Total suite: 159/159.
- [2026-04-18] VI rename: `iv_measurement` → `keithley_delta_mode` in `devices.yaml`, `field_sweep_iv.py`, `test_l2_station.py`, `test_l4_procedure.py`, `test_l5_data_manager.py`, `monitor_window.py`. Added `dc_measurement` (DCMeasurementVI) to `devices.yaml`.
- [2026-04-18] L1: `ITC503TemperatureVI.set_ramp_rate(rate_K_per_min)` added as `@control` — appears in GUI panel and can be called per-sweep by procedures. `start_ramp(target, rate=None)` now accepts an optional rate so callers can override `_default_ramp_rate` without mutating it.
- [2026-04-18] L2: `Station.process_system_targets` now reads optional `rate` key from target dict and forwards it to `vi.start_ramp(rate=...)`. Backwards-compatible: existing calls without a rate key are unchanged.
- [2026-04-18] L4: `FieldSweepDC` (`procedures/field_sweep_dc.py`) — field sweep using `dc_measurement` VI. Parameters: field_start, field_end, field_steps, temperature, current_A, compliance_A, voltmeter_range_V, readings_per_point, init_wait, step_wait. Full Orchestrator loop tested.
- [2026-04-18] L4: `TemperatureSweepDC` (`procedures/temperature_sweep_dc.py`) — temperature sweep using `dc_measurement` VI. Parameters: temp_start, temp_end, n_points, ramp_rate_K_per_min, current_A, compliance_A, voltmeter_range_V, readings_per_point, point_wait. Ramp rate passed per-step via system_targets so it takes effect without editing YAML. Full Orchestrator loop tested.
- [2026-04-18] 22 new tests in `tests/test_new_procedures.py` covering both new procedures, ramp-rate forwarding, and set_ramp_rate control. Total suite: 181/181.
- [2026-04-19] Orchestrator: procedure preempts a manual ramp. run_procedure() now recognises RAMPING+procedure=None as a preemptable state: it cancels all system VI ramp generators (_ramp_gen=None, _ramp_exhausted=True) and starts the procedure immediately instead of queuing it. Any other busy state still queues. 9/9 orchestrator tests pass.
- [2026-04-19] Orchestrator: manual ramps now enter RAMPING state. When GUI actions (set_temperature, set_field, etc.) call start_ramp(), the orchestrator detects an active ramp via check_ramps() after draining the queue and transitions to RAMPING. In RAMPING with procedure=None, advance_ramp() runs each tick; orchestrator returns to IDLE when all ramps are done (no wait, no measuring). Previously set_temperature/set_field were fire-and-forget: advance_ramp() was never called, so the generator froze after the first step.
- [2026-04-19] Orchestrator: GUI actions no longer blocked during a manual ramp (RAMPING + procedure=None). submit_vi_action() and the GUI action queue now allow actions in this state, so multiple instruments can be commanded while one is already ramping (e.g. ramp VTI while sample temperature is still settling). Procedure ramps continue to block GUI actions.
- [2026-04-19] @control decorator: fixed type resolution for `from __future__ import annotations`. Previously param.annotation returned the string 'float' instead of the float type, causing InstrumentPanel to log "could not coerce '10' to float" and pass the raw string to the VI. Fix: use typing.get_type_hints(func) which resolves lazy string annotations to actual types before building _control_params. All existing tests pass (181/181).
- [2026-04-19] Live-plot axis redesign + full system-state logging per datapoint. 181/181 tests pass:
  - `Station.cached_state` property: returns `_last_known_state` without any hardware poll (safe to call from procedure `measure()`).
  - `Station.last_state_flat()`: flattens cached system VI state to `{vi_name_field: float}`, skipping strings (e.g. ramp_status) and measurement VIs.
  - `BaseProcedure._build_data_config(base_config)`: merges `unix_time` + all system state keys into HDF5 schema — one call in `initiate()` replaces the manual data_config dict.
  - `BaseProcedure._save_datapoint(measured_data)`: merges `unix_time` + `station.last_state_flat()` + procedure data, calls DataManager — no extra hardware poll. Replaces manual `get_state()` + `save_datapoint(...)` calls in every procedure's `measure()`.
  - `BaseProcedure.get_data_keys()`: returns all plottable keys for GUI axis selectors (`unix_time` + sweep_data_keys + system_keys + measurement_data_keys).
  - Each procedure gains class attrs: `sweep_data_keys`, `measurement_data_keys`, `default_x_key`.
  - `ProcedureWindow`: each plot now has its own X and Y selector (4 selectors total), all populated from `proc.get_data_keys()` at run start. Datapoints stored in `_datapoints` list so changing any axis mid-run replots the full history. Axis labels auto-update on selector change.
- [2026-04-18] GUI improvements — whole-window scrollability, connection check for measurement VIs, log filtering. All 145 tests pass:
  - MonitorWindow: entire content wrapped in outer QScrollArea (central widget); redundant inner lower_scroll removed. Window is now vertically scrollable on small screens while the splitter still works for large screens.
  - Other Devices panel: removed misleading class-name type label ("Delta Mode Measurement" was derived from class name, not live data). Replaced with a coloured dot (●) + "Unknown/Connected/Not reachable" label and a "Check" button.
  - `MeasurementInstrumentBase.ping() -> bool` added to `base.py` (returns False by default).
  - `DeltaModeMeasurementVI.ping()` overrides base: calls `get_idn()` on both source and meter drivers; returns True only if both respond.
  - `SimKeithley6221.get_idn()` and `SimKeithley2182A.get_idn()` added to sim drivers.
  - Qt log widget: `_QtLogHandler` now suppresses `cryosoft.vi.*` DEBUG records (per-method call/return noise). Warnings and errors from VIs still appear.
  - `Orchestrator._tick()`: after `states_updated.emit()`, logs one compact DEBUG summary line per tick (e.g. "Monitor: magnet_x: field=1.234, ... | temperature_vti: ..."). File log unchanged.
- [2026-04-17] GUI redesign — global dark theme, resizable panels, ProcedureWindow restructured. All 24 tests pass:
  - NEW: `cryosoft/gui/theme.py` — central color palette (Material Dark) + `build_stylesheet()` returning full application QSS
  - Global QSS applied in `main.py` (pyqtgraph background/foreground also set via `pg.setConfigOptions`)
  - All panels resizable by dragging: MonitorWindow has QSplitter(Vertical) between VI grid and lower section; ProcedureWindow top section uses QSplitter(Horizontal) for params/queue split; plot section uses QSplitter(Horizontal)
  - ProcedureWindow layout restructured: two-column top (params left ~60%, queue right ~40%); two live plots side-by-side below (Plot 1 cyan, Plot 2 magenta), each with its own Y-axis selector; progress bar full-width below plots
  - Dual plot buffers (_plot_y1, _plot_y2) sharing a common X axis; missing Y keys produce NaN (rendered as plot gaps)
  - Button hierarchy via dynamic `class` properties: primary (accent blue fill), secondary (outline), danger (red fill)
  - InstrumentPanel value readout labels styled 17pt bold via QSS; log handler colors from theme constants

<!-- Example format:
- [2026-04-01] L0: `core/real_driver.py` — RealDriver base class. Unit tests pass (8/8). `tests/test_core/test_real_driver.py`
- [2026-04-02] L0: `drivers/oxford_ips120.py` — OxfordIPS120RealDriver. Unit tests pass (12/12) with MockIPS120.
-->

---

## In progress

_Nothing yet. Items move here when work begins._

<!-- Example format:
- L0: `core/real_driver.py` — Writing base class with connect/disconnect/retry. ~60% complete.
-->

---

## Next up

These are ordered by priority. Work top to bottom.

### Phase 1: Foundation (v0.1-L0)

1. **Project scaffolding**
   - Create folder structure (`cryosoft/`, `tests/`, `docs/`, `configs/`)
   - Create all folder `README.md` files
   - Set up `pyproject.toml` with dependencies
   - Set up `pytest` configuration (`conftest.py`, markers for `@pytest.mark.hardware`)
   - Create `__init__.py` files

2. **Exception hierarchy**
   - `core/exceptions.py` — `CryoSoftDriverError`, `CommunicationError`, `ValueOutOfRangeError`, `HardwareError`, `StabilizationTimeout`
   - Tests: `tests/test_core/test_exceptions.py`

3. **RealDriver base class**
   - `core/real_driver.py` — connection management, `_write`/`_query` with auto-retry, `_validate_range`, `_wait_until_stable`
   - Mock: `tests/mocks/mock_visa_resource.py` — simulates a PyVISA resource
   - Tests: `tests/test_core/test_real_driver.py` — connect, disconnect, retry on failure, reconnect

4. **First real driver: Oxford ITC 503**
   - `drivers/oxford_itc503.py` — temperature read, heater control, set temperature, sensor channels, helium level
   - Mock: `tests/mocks/mock_itc503.py` — simulates ITC 503 responses
   - Tests: `tests/test_drivers/test_itc503_driver.py`
   - Reason: ITC 503 is used on every cryostat and will be decomposed into multiple VIs later (1:N)

5. **Second real driver: Oxford IPS 120**
   - `drivers/oxford_ips120.py` — field read/write, ramp, switch heater, ramp to zero
   - Mock: `tests/mocks/mock_ips120.py` — simulates ramp behavior over time
   - Tests: `tests/test_drivers/test_ips120_driver.py`

6. **Third real driver: Keithley 6221**
   - `drivers/keithley_6221.py` — current source, delta mode arming, output enable
   - Mock: `tests/mocks/mock_keithley_6221.py`
   - Tests: `tests/test_drivers/test_keithley_6221.py`

7. **Fourth real driver: Keithley 2182A**
   - `drivers/keithley_2182a.py` — voltage measurement, range, NPLC
   - Mock: `tests/mocks/mock_keithley_2182a.py`
   - Tests: `tests/test_drivers/test_keithley_2182a.py`

8. **Driver registry**
   - `drivers/driver_registry.yaml`
   - `core/driver_loader.py` — reads registry, imports driver classes by name
   - Tests: `tests/test_core/test_driver_loader.py`

### Phase 2: Virtual instruments (v0.2-L1)

9. **BaseVirtualInstrument**
    - `core/virtual_instrument.py` — base class with standby, initiate, get_state, get_system_value
    - Tests: `tests/test_core/test_virtual_instrument.py`

10. **MagnetVI**
    - `virtual_instruments/magnet/ips120_magnet.py` — wraps IPS 120, set_field (blocking), ramp_to_zero
    - Tests using MockIPS120

11. **TemperatureVI (1:N decomposition)**
    - `virtual_instruments/temperature/itc503_temperature.py` — wraps ITC 503 for one control channel
    - Two instances from same real driver (VTI + sample)
    - Tests using MockITC503

12. **LevelMeterVI (1:N decomposition)**
    - `virtual_instruments/level/itc503_level.py` — wraps ITC 503 for helium/nitrogen level
    - Shares real driver with TemperatureVI
    - Tests using MockITC503

13. **MeasurementVI + DeltaMode (N:1 composition)**
    - `core/virtual_instrument.py` — add MeasurementVI base with initiate_measurement / read_datapoint
    - `virtual_instruments/measurement/delta_mode_6221_2182a.py` — wraps 6221 + 2182A
    - Tests using MockKeithley6221 + MockKeithley2182A

14. **VI registry**
    - `virtual_instruments/vi_registry.yaml`
    - `core/vi_loader.py`
    - Tests: `tests/test_core/test_vi_loader.py`

### Phase 3: Core infrastructure (v0.3-core)

15. **Config loader**
    - `core/config_loader.py` — parses devices.yaml + variables.yaml, validates, wires VIs to drivers
    - Tests with example config files in `tests/fixtures/`

16. **Command queue**
    - `core/command_queue.py` — priority queue with interleaving
    - Tests: concurrent submit from two threads, priority ordering, emergency pre-emption

17. **Instrument manager**
    - `core/instrument_manager.py` — load config, connect all, variable mapping, shared access via queue
    - Integration tests with mocked VIs

18. **Monitor / watchdog**
    - `core/monitor.py` — QThread polling loop, safety rule evaluation
    - `core/safety_engine.py` — YAML rule parser, action dispatcher
    - Tests: safety rule triggers, emergency action

### Phase 4: Measurement (v0.4-procedures)

19. **BaseProcedure**
    - `core/base_procedure.py` — set_state / run / cleanup contract, should_stop flag
    - Tests

20. **Data manager**
    - `core/data_manager.py` — HDF5 creation with enriched start metadata, per-point snapshots
    - `core/metadata.py` — state serialization
    - Tests: verify HDF5 structure, read back metadata

21. **First procedure: FieldSweepIV**
    - `procedures/field_sweep_iv.py`
    - End-to-end integration test with all mocks

22. **Procedure loader**
    - `core/procedure_loader.py` — auto-discover procedures
    - Tests

### Phase 5: GUI (v0.5-gui)

23. **Main window** — connection bar, system state panel, monitor plots
24. **Procedure window** — procedure browser, auto-form, sample metadata, live plot
25. **Integration** — full stack test with mocks, manual hardware test

### Phase 6: Deploy (v1.0)

26. **PyInstaller packaging**
27. **User documentation** — how to write a driver, how to write a VI, how to configure a cryostat
28. **Hardware testing** on first cryostat
29. **Second cryostat config** — copy and edit YAML

---

## Blocked / Issues

_No current blockers._

<!-- Example format:
- [2026-04-05] L1 MagnetVI: Unclear how IPS 120 reports quench status. Need to test on hardware or find manual section. Blocks: quench_detected flag in get_state().
-->

---

## Design decisions and changelog

Record any interface changes, architectural decisions, or deviations from the architecture doc.

<!-- Example format:
- [2026-04-03] Decided to use `pyvisa-sim` for mock VISA resources instead of pure unittest.mock. Reason: pyvisa-sim handles resource string parsing and timeout simulation natively.
- [2026-04-07] Changed MeasurementVI.read_datapoint() return type from numpy array to list[list[float]]. Reason: avoids numpy dependency at the VI layer; conversion to numpy happens in DataManager.
-->

---

## File inventory

Track every file in the project. Update when adding new files.

```
cryosoft/
  __init__.py                          — NOT CREATED
  main.py                              — NOT CREATED
  core/
    __init__.py                        — NOT CREATED
    exceptions.py                      — NOT CREATED
    real_driver.py                     — NOT CREATED
    virtual_instrument.py              — NOT CREATED
    driver_loader.py                   — NOT CREATED
    vi_loader.py                       — NOT CREATED
    config_loader.py                   — NOT CREATED
    instrument_manager.py              — NOT CREATED
    command_queue.py                   — NOT CREATED
    monitor.py                         — NOT CREATED
    safety_engine.py                   — NOT CREATED
    base_procedure.py                  — NOT CREATED
    procedure_loader.py                — NOT CREATED
    data_manager.py                    — NOT CREATED
    metadata.py                        — NOT CREATED
    logger.py                          — NOT CREATED
    README.md                          — NOT CREATED
  drivers/
    oxford_itc503.py                   — NOT CREATED
    oxford_ips120.py                   — NOT CREATED
    keithley_6221.py                   — NOT CREATED
    keithley_2182a.py                  — NOT CREATED
    driver_registry.yaml               — NOT CREATED
    README.md                          — NOT CREATED
  virtual_instruments/
    magnet/
      ips120_magnet.py                 — NOT CREATED
      README.md                        — NOT CREATED
    temperature/
      itc503_temperature.py            — NOT CREATED
      README.md                        — NOT CREATED
    measurement/
      delta_mode_6221_2182a.py         — NOT CREATED
      README.md                        — NOT CREATED
    level/
      itc503_level.py                  — NOT CREATED
      README.md                        — NOT CREATED
    vi_registry.yaml                   — NOT CREATED
    README.md                          — NOT CREATED
  procedures/
    field_sweep_iv.py                  — DONE (delta-mode field sweep; uses keithley_delta_mode VI)
    field_sweep_dc.py                  — DONE (DC field sweep; uses dc_measurement VI)
    temperature_sweep_dc.py            — DONE (DC temperature sweep; uses dc_measurement VI; ramp rate per-step)
    README.md                          — NOT CREATED
  configs/
    example_cryostat/
      devices.yaml                     — NOT CREATED
      variables.yaml                   — NOT CREATED
      safety_rules.yaml                — NOT CREATED
      monitor_config.yaml              — NOT CREATED
    README.md                          — NOT CREATED
  gui/
    __init__.py                        — DONE
    theme.py                           — DONE (central color palette + build_stylesheet(); applied in main.py)
    instrument_panel.py                — DONE (auto-generated per-VI panel; required-field validation; theme-aware)
    monitor_window.py                  — DONE (outer QScrollArea central widget; Other Devices connection-check dot + Check button; VI DEBUG log filter; per-tick monitor summary)
    procedure_window.py                — DONE (two-column layout + dual live plots + draggable splitters; theme-aware)
    README.md                          — NOT CREATED
  main.py                              — DONE (application entry point)
  data/                                — Created at runtime
  logs/                                — Created at runtime

tests/
  conftest.py                          — NOT CREATED
  mocks/
    mock_visa_resource.py              — NOT CREATED
    mock_ips120.py                     — NOT CREATED
    mock_itc503.py                     — NOT CREATED
    mock_keithley_6221.py              — NOT CREATED
    mock_keithley_2182a.py             — NOT CREATED
    README.md                          — NOT CREATED
  test_core/
    test_exceptions.py                 — NOT CREATED
    test_real_driver.py                — NOT CREATED
    test_virtual_instrument.py         — NOT CREATED
    test_driver_loader.py              — NOT CREATED
    test_vi_loader.py                  — NOT CREATED
    test_config_loader.py              — NOT CREATED
    test_instrument_manager.py         — NOT CREATED
    test_command_queue.py              — NOT CREATED
    test_monitor.py                    — NOT CREATED
    test_data_manager.py               — NOT CREATED
  test_drivers/
    test_itc503_driver.py              — NOT CREATED
    test_ips120_driver.py              — NOT CREATED
    test_keithley_6221.py              — NOT CREATED
    test_keithley_2182a.py             — NOT CREATED
  test_virtual_instruments/
    test_magnet_vi.py                  — NOT CREATED
    test_temperature_vi.py             — NOT CREATED
    test_delta_mode_vi.py              — NOT CREATED
    test_level_vi.py                   — NOT CREATED
  test_procedures/
    test_field_sweep_iv.py             — NOT CREATED
  test_l1_virtual_instruments.py       — DONE
  test_l2_station.py                   — DONE (updated: keithley_delta_mode + dc_measurement in expected VI list)
  test_l3_orchestrator.py              — DONE
  test_l4_procedure.py                 — DONE (updated: iv_measurement → keithley_delta_mode)
  test_l5_data_manager.py              — DONE (updated: iv_measurement → keithley_delta_mode)
  test_measurement_dc_vi.py            — DONE (14 tests for DCMeasurementVI)
  test_new_procedures.py               — DONE (22 tests: FieldSweepDC, TemperatureSweepDC, ramp-rate control)
  test_gui.py                          — DONE (145 tests total, all pass)
  fixtures/
    example_devices.yaml               — NOT CREATED
    example_variables.yaml             — NOT CREATED
  README.md                            — NOT CREATED

docs/
  architecture_v1.1.docx               — DONE
  driver_standard_v1.md                — DONE
  STATUS.md                            — THIS FILE
  README.md                            — NOT CREATED

pyproject.toml                         — NOT CREATED
CLAUDE.md                              — DONE
README.md                              — NOT CREATED
```
