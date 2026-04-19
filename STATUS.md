# CryoSoft — Implementation Status

> **This is a live document.** Read it before every session; update it after every task.
> Last updated: 2026-04-19 (VI refactor complete — behavior-based names, subpackages, 3-column procedure form)

---

## Project phase

**Current phase:** Simulation stack complete. All layers implemented and tested against simulated drivers.
**Current milestone:** Simulation-first development done. Ready for Phase 7 — real driver integration.
**Architecture version:** v4.1 (`directives/CryoSoft_Architecture_v4_1.md`)
**Active branch:** `feature/behavior-based-vis` (all refactor work)

---

## Layer status overview

| Layer | Name                | Status | Tests  | Notes |
|-------|---------------------|--------|--------|-------|
| L0    | Sim drivers         | Done   | 54/54  | IPS120 + ITC503 + ILM200 + K6221 + K2182A + K2400 |
| L1    | Virtual instruments | Done   | 133/133| 8 behavior-named VIs in 4 subpackages |
| L2    | Station + Config    | Done   | 10/10  | build_station() factory, YAML config |
| L3    | Orchestrator        | Done   | 9/9    | Full state machine; manual ramp; procedure preempt |
| L4    | Procedures          | Done   | 63/63  | 3 procedures; 3-group parameter dicts |
| L5    | Data manager        | Done   | 17/17  | HDF5; pre-allocation; early abort trim |
| GUI   | Monitor + Procedure | Done   | 32/32  | Dark theme; dual live plots; 3-column param form |

**Total: 271/271 tests passing.**

---

## Done

### Foundation (2026-04-06)
- `core/exceptions.py` — `CryoSoftError`, `CryoSoftCommunicationError`, `CryoSoftSafetyError`, `CryoSoftConfigError`
- `core/decorators.py` — `@monitored`, `@control`; `__init_subclass__` logging wrapper; `get_monitored_methods()`, `get_control_methods()`; type-hint resolution via `typing.get_type_hints()` (fixes `from __future__ import annotations` string-annotation issue)
- `core/logging_config.py` — rotating file handler; `_QtLogHandler` for GUI log widget; VI DEBUG suppression in Qt widget

### L0: Sim Drivers (2026-04-06 → 2026-04-19)
- `drivers/sim_oxford_ips120.py` — current ramp, HOLD/RAMPING status, quench sim, safety clamping. **+** switch heater on/off, coil current tracking, persistent mode flag
- `drivers/sim_oxford_itc503.py` — exponential temperature settling, heater output %. **+** needle valve 0–100% position
- `drivers/sim_oxford_ilm200.py` — drifting He/N2 levels, configurable low-He. **+** 3-mode refresh (STANDBY=0, SLOW=1, FAST=2)
- `drivers/sim_keithley_6221.py` — current source, delta-mode config, `get_idn()`, `set_compliance()`, `get_compliance()`
- `drivers/sim_keithley_2182a.py` — voltage readings with Gaussian noise, delta-mode pairing, `get_idn()`
- `drivers/sim_keithley_2400.py` — **new** SMU: sources current, measures voltage (V = R·I + noise), `_resistance=1500 Ω`, `_simulate_error` flag
- Tests: `tests/test_l0_simulated.py` (39), `tests/test_l0_new_drivers.py` (15)

### L1: Virtual Instruments (2026-04-06 → 2026-04-19)
All VIs are behavior-named and live in subpackages of `virtual_instruments/`:

**`virtual_instruments/magnet/`**
- `superconducting_magnet.py` — `SuperconductingMagnetVI`: status-driven ramp, segment-based rate scheduling, tesla↔ampere conversion. `@monitored`: `magnet_current`, `get_field`, `magnet_status`. `@control`: `set_field`
- `superconducting_magnet_persistent.py` — `SuperconductingMagnetPersistentVI` (extends above): switch heater + persistent mode ramp sequence via tick-count generators (never `time.sleep`). New `init_params`: `switch_heater_warmup_ticks` (default 30), `switch_heater_cooldown_ticks` (default 30). `@monitored`: `switch_heater_state`, `coil_current`, `is_persistent`. `@control`: `switch_heater_on/off`, `enter/exit_persistent_mode`

**`virtual_instruments/temperature/`**
- `sample_temperature_controller.py` — `SampleTemperatureControllerVI`: time-based ramp generator, tolerance-based settle detection. `start_ramp(target, rate=None)`. `@monitored`: `temperature`, `setpoint`, `heater_output`. `@control`: `set_temperature`, `set_ramp_rate`
- `vti_temperature_controller.py` — `VTITemperatureControllerVI` (extends above): needle valve via same ITC503 driver auxiliary output. `@monitored`: `needle_valve`. `@control`: `set_needle_valve`

**`virtual_instruments/level/`**
- `cryogen_level_meter.py` — `CryogenLevelMeterVI`: rolling majority-vote buffer for noise-immune `helium_low()`. Module-level constants `STANDBY=0, SLOW=1, FAST=2`. `@monitored`: `helium_level`, `nitrogen_level`, `get_refresh_rate`. `@control`: `set_refresh_rate(mode: int)`

**`virtual_instruments/measurement/`**
- `dc_separate_measurement.py` — `DCSeparateMeasurementVI`: Keithley 6221 + 2182A, fixed DC current. `@control`: `initiate(current_A, compliance_A, voltmeter_range_V)`. `take_reading(n_points) → {"voltage_V": list, "current_A": list}`. `standby()`, `ping()`
- `dc_single_instrument.py` — `DCSingleInstrumentVI`: Keithley 2400 SMU, same method contract as above. `{"main": K2400}`
- `measurement_delta_mode.py` — `DeltaModeMeasurementVI`: Keithley 6221 + 2182A, delta-mode IV. `configure("delta_mode", current, n_readings, delay)`. `read_datapoint() → {"voltage_V": list, "current_A": list}`

**Base classes** (`virtual_instruments/base.py`):
- `BaseVirtualInstrument` — `__init_subclass__` logging wrapper, `get_state()` auto-build
- `MagnetBase`, `TemperatureControllerBase`, `LevelMeterBase`, `MeasurementInstrumentBase`
- `DCMeasurementBase` — grouping base for all DC measurement VIs; `raise NotImplementedError` (not `@abstractmethod`) for `initiate()` and `take_reading()`, following existing pattern

- Tests: `tests/test_l1_virtual_instruments.py`, `tests/test_l1_new_vis.py` (61), `tests/test_measurement_dc_vi.py` (14)

### L2: Station + Config (2026-04-06 → 2026-04-18)
- `core/station.py` — `Station` + `build_station(config_path)` factory
- Attribute-style VI access, VI type registry, `get_state()` with stale/disconnected error handling
- `process_system_targets()` — dispatches `start_ramp(target, rate=...)` per VI; forwards optional `rate` key
- `send_measurement_commands()` — dispatches method calls on measurement VIs
- `check_ramps()`, `check_safety()`, `execute_vi_action()`, `initiate_all()`, `standby_all()`
- `cached_state` property (no poll), `last_state_flat()` (numeric-only flat dict for system VIs)
- `configs/sim_cryostat/devices.yaml` — 7 VIs with subpackage class paths; `configs/sim_cryostat/monitor.yaml`
- Tests: `tests/test_l2_station.py` (10)

### L3: Orchestrator (2026-04-06 → 2026-04-19)
- `core/orchestrator.py` — cooperative QTimer state machine
- States: `IDLE → INITIATING → RAMPING → MEASURING → SWEEPING → STANDBY → IDLE`; also `PAUSED`, `ERROR`, `EMERGENCY`
- Manual ramps enter RAMPING state; `advance_ramp()` called every tick until done; returns to IDLE (no wait/measure step)
- GUI actions unblocked during manual ramp (RAMPING + `_procedure=None`); blocked during procedure ramps
- `run_procedure()` preempts a manual ramp (clears all generators, starts procedure immediately)
- Abort: holds instruments at current position; monitor continues; ramp generators cleared
- `measurement_ready` signal carries enriched datapoint dict (used by ProcedureWindow live plots)
- Tests: `tests/test_l3_orchestrator.py` (9)

### L4: Procedures (2026-04-06 → 2026-04-19)
- `core/procedure.py` — `BaseProcedure` with three parameter group dicts + `__init_subclass__` union
  - `sweep_parameters` — what to sweep (defines the sweep array)
  - `system_parameters` — cryostat state during sweep (temperatures, wait times, ramp rates)
  - `measurement_parameters` — measurement VI configuration (current, range, n_readings)
  - `parameters` — auto-computed union; all existing code continues to work
  - `_build_data_config(base_config)`, `_save_datapoint(measured_data)`, `get_data_keys()`
  - `sweep_data_keys`, `measurement_data_keys`, `default_x_key` class attributes for GUI axis selectors
- `procedures/field_sweep_iv.py` — `FieldSweepIV`: field sweep + delta-mode IV
- `procedures/field_sweep_dc.py` — `FieldSweepDC`: field sweep + DC resistance
- `procedures/temperature_sweep_dc.py` — `TemperatureSweepDC`: temperature sweep + DC resistance; per-step ramp rate via `system_targets`
- Tests: `tests/test_l4_procedure.py` (19), `tests/test_new_procedures.py` (22)

### L5: Data Manager (2026-04-06)
- `core/data_manager.py` — HDF5 creation, pre-allocated datasets, per-point snapshots
- File structure: `/metadata/`, `/data/` (1D sweep + 2D measurement arrays), `/snapshots/` (JSON)
- `close()` trims datasets on early abort; records `end_time`
- Tests: `tests/test_l5_data_manager.py` (17)

### GUI (2026-04-06 → 2026-04-19)
- `gui/theme.py` — Material Dark palette, `build_stylesheet()`, `BTN_CLASS_PRIMARY/SECONDARY/DANGER`
- `gui/instrument_panel.py` — auto-generated panels from `@monitored`/`@control`; stale/disconnected indicators; required-field validation
- `gui/monitor_window.py` — outer `QScrollArea` central widget; VI grid always-visible; Other Devices connection-check dot + "Check" button; `_QtLogHandler` (VI DEBUG suppressed in widget); per-tick monitor summary log; Sample Info section
- `gui/procedure_window.py` — 3-column parameter form (`QGroupBox` Sweep | System | Measurement); dual live plots (`pyqtgraph`) with 4 independent axis selectors; progress bar; Pause/Resume/Abort; emergency acknowledge; queue management
- `gui/main.py` — application entry point
- Tests: `tests/test_gui.py` (32)

---

## In progress

_Nothing. All simulation-stack work is complete._

---

## Next up

### Phase 7: Real Driver Integration

These are ordered by priority.

1. **Real IPS120 driver** (`drivers/oxford_ips120.py`) — GPIB/RS232 Oxford IPS 120-10. Validate against driver test harness. Swap `sim_oxford_ips120` → `oxford_ips120` in `vector_magnet_cryostat/devices.yaml`.

2. **Real ITC503 driver** (`drivers/oxford_itc503.py`) — GPIB Oxford ITC 503. Same contract as sim. Needle valve via auxiliary analog output (verify channel mapping on hardware).

3. **Real ILM200 driver** (`drivers/oxford_ilm200.py`) — RS232 Oxford ILM 200/210. Verify 3-mode refresh rate command syntax.

4. **Real Keithley 6221 driver** (`drivers/keithley_6221.py`) — GPIB. Delta-mode arming sequence.

5. **Real Keithley 2182A driver** (`drivers/keithley_2182a.py`) — GPIB. Triggered voltage acquisition.

6. **Driver test harness** (`tests/driver_test_harness.py`) — validates L0 contract: single-string init, all expected methods present and return correct types, communication-error recovery. Run against each real driver before connecting to the VI layer.

7. **Production YAML config** (`configs/vector_magnet_cryostat/devices.yaml`) — real driver class paths, real VISA addresses, calibrated `amperes_per_tesla`, production safety limits.

8. **Full system test** — run `FieldSweepIV` with real hardware, verify HDF5 output matches expected structure.

---

## Blocked / Issues

_No current blockers._

---

## Design decisions (v4.0 → v4.1)

- [2026-04-19] **Behavior-based VI naming.** VIs renamed from instrument model (`IPS120MagnetVI`) to behavioral role (`SuperconductingMagnetVI`). Rationale: procedures and the Station should be agnostic to which specific PSU is installed. Swapping hardware requires only a YAML change, not a VI rewrite.
- [2026-04-19] **VI subpackage structure.** `virtual_instruments/` split into `magnet/`, `temperature/`, `measurement/`, `level/` subpackages. `base.py` and `rampable.py` remain at the top level (used across all subpackages). Rationale: flat directory with 10+ files was harder to navigate.
- [2026-04-19] **`DCMeasurementBase` uses `raise NotImplementedError`, not `@abstractmethod`.** Consistent with `MagnetBase`, `TemperatureControllerBase` etc. — documentation/grouping purpose, not enforcement. Procedures type-hint `dc: DCMeasurementBase` and can swap between `DCSeparateMeasurementVI` and `DCSingleInstrumentVI` via YAML.
- [2026-04-19] **Procedure parameters split into 3 group dicts.** `sweep_parameters`, `system_parameters`, `measurement_parameters` are defined in concrete subclasses. `BaseProcedure.__init_subclass__` auto-builds the `parameters` union so all existing code (`cls.parameters.items()`, `FieldSweepIV.parameters`) continues to work without change. Rationale: flat form mixed sweep axis, system state, and measurement config — conceptually distinct, and the separation enables a 3-column GUI layout.
- [2026-04-19] **3-column GUI parameter form.** `ProcedureWindow._build_param_form()` renders 3 `QGroupBox` panels (Sweep | System | Measurement) in a `QHBoxLayout`. Each panel uses a `QFormLayout`. This prepares for a future "custom sweep array" feature: the Sweep panel can be replaced by a file-picker widget without touching the other two.
- [2026-04-19] **Persistent-mode ramp uses tick-count generators, never `time.sleep()`.** Switch heater warm-up and cool-down waits are counted in Orchestrator ticks. This keeps the ramp cooperative with the event loop and avoids freezing the Qt GUI. `switch_heater_warmup_ticks` and `switch_heater_cooldown_ticks` are `init_params` in YAML.

---

## File inventory

```
cryosoft/
  __init__.py
  main.py                              — DONE (application entry point)
  core/
    __init__.py
    exceptions.py                      — DONE (CryoSoftError hierarchy)
    decorators.py                      — DONE (@monitored, @control, get_type_hints fix)
    logging_config.py                  — DONE (rotating file + Qt log handler)
    station.py                         — DONE (Station + build_station() factory)
    orchestrator.py                    — DONE (cooperative state machine)
    data_manager.py                    — DONE (HDF5 storage)
    procedure.py                       — DONE (BaseProcedure + 3-group parameters)
    README.md                          — DONE
  drivers/
    __init__.py
    sim_oxford_ips120.py               — DONE (+ switch heater + persistent mode)
    sim_oxford_itc503.py               — DONE (+ needle valve)
    sim_oxford_ilm200.py               — DONE (+ 3-mode refresh)
    sim_keithley_6221.py               — DONE (+ set/get_compliance, get_idn)
    sim_keithley_2182a.py              — DONE (+ get_idn)
    sim_keithley_2400.py               — DONE (new SMU sim driver)
    oxford_ips120.py                   — NOT CREATED (Phase 7)
    oxford_itc503.py                   — NOT CREATED (Phase 7)
    oxford_ilm200.py                   — NOT CREATED (Phase 7)
    keithley_6221.py                   — NOT CREATED (Phase 7)
    keithley_2182a.py                  — NOT CREATED (Phase 7)
  virtual_instruments/
    __init__.py
    base.py                            — DONE (all base classes + DCMeasurementBase)
    rampable.py                        — DONE (RampableVI mixin)
    magnet/
      __init__.py
      superconducting_magnet.py        — DONE (status-driven ramp, segment rates)
      superconducting_magnet_persistent.py — DONE (switch heater + persistent mode)
      README.md                        — DONE
    temperature/
      __init__.py
      sample_temperature_controller.py — DONE (time-based ramp)
      vti_temperature_controller.py    — DONE (+ needle valve)
      README.md                        — DONE
    measurement/
      __init__.py
      dc_separate_measurement.py       — DONE (6221 + 2182A, DC mode)
      dc_single_instrument.py          — DONE (K2400 SMU)
      measurement_delta_mode.py        — DONE (6221 + 2182A, delta mode)
      README.md                        — DONE
    level/
      __init__.py
      cryogen_level_meter.py           — DONE (majority-vote buffer, 3-mode)
      README.md                        — DONE
  procedures/
    __init__.py
    field_sweep_iv.py                  — DONE (field sweep + delta-mode IV)
    field_sweep_dc.py                  — DONE (field sweep + DC resistance)
    temperature_sweep_dc.py            — DONE (temperature sweep + DC resistance)
    README.md                          — DONE
  configs/
    sim_cryostat/
      devices.yaml                     — DONE (subpackage class paths)
      monitor.yaml                     — DONE
    vector_magnet_cryostat/            — NOT CREATED (Phase 7)
  gui/
    __init__.py
    theme.py                           — DONE (Material Dark; button classes)
    instrument_panel.py                — DONE (auto-generated panels; validation)
    monitor_window.py                  — DONE (scrollable; check dot; log filter)
    procedure_window.py                — DONE (3-column form; dual live plots)
    README.md                          — DONE

tests/
  __init__.py
  conftest.py                          — DONE
  mocks/
    __init__.py
  test_foundation.py                   — DONE
  test_l0_simulated.py                 — DONE (39 tests)
  test_l0_new_drivers.py               — DONE (15 tests: IPS120 heater, ITC503 needle, ILM200 3-mode, K2400)
  test_l1_virtual_instruments.py       — DONE (updated imports to subpackage paths)
  test_l1_new_vis.py                   — DONE (61 tests for all 7 new VIs)
  test_l2_station.py                   — DONE (10 tests)
  test_l3_orchestrator.py              — DONE (9 tests)
  test_l4_procedure.py                 — DONE (19 tests)
  test_l5_data_manager.py              — DONE (17 tests)
  test_measurement_dc_vi.py            — DONE (14 tests for DCSeparateMeasurementVI)
  test_new_procedures.py               — DONE (22 tests: FieldSweepDC, TemperatureSweepDC)
  test_gui.py                          — DONE (32 tests)
  driver_test_harness.py               — NOT CREATED (Phase 7)

directives/
  CryoSoft_Architecture_v4_0.md       — DONE (previous architecture doc)
  CryoSoft_Architecture_v4_1.md       — DONE (current)
  layer_plans/                         — DONE (7 layer plan files)
  deprecated/                          — archived older docs

STATUS.md                              — THIS FILE
CLAUDE.md                              — DONE
README.md                              — DONE
```
