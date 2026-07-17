# core/

## Purpose

`core/` holds the instrument-agnostic infrastructure every other layer depends
on: the typed vocabulary that all layers exchange, the L2 `Station` and L3
`Orchestrator` runtime, the L4 procedure base classes, the L5 data manager, and
the cross-cutting utilities (exceptions, decorators, logging, config catalog,
runtime status). Nothing here knows about a specific instrument.

## Architecture layer

Cross-cutting infrastructure spanning L2 to L5 plus shared utilities. The
runtime classes are layered: `Station` (L2) builds and polls Virtual
Instruments; `Orchestrator` (L3) is the single tick loop and the sole writer to
hardware; `BaseProcedure` / `SweepMeasureProcedure` (L4) are measurement
recipes; `DataManager` (L5) writes HDF5. `plan.py` is the typed currency shared
by all of them.

## Entry (how control/data enters this folder)

- `build_station(config_path)` reads a YAML config directory (`devices.yaml`,
  `monitor.yaml`) and constructs the driver + VI stack into a `Station`.
- `Orchestrator(station, tick_interval_ms)` receives a `Station`; the GUI then
  submits `Procedure` objects and VI/global action requests to it.
- A `Procedure` is constructed with a `Station`, `sample_info`,
  `data_directory`, and GUI param values; it builds `plan.py` value objects.
- `Orchestrator` calls `build_operational_status()` then `apply_watchdog()` each
  tick from the already-polled station snapshot.

## Exit (what it hands to other layers)

- `Station` returns state snapshots `{vi_name: {field: value}}` from
  `get_state()`, ramp progress from `check_ramps()` / `get_ramp_status()`, and
  aggregated safety verdicts from `check_safety(state)` (reuses the tick
  snapshot, no extra poll).
- `Orchestrator` emits Qt signals to the GUI: `states_updated`, `state_changed`,
  `procedure_progress`, `procedure_finished`, `measurement_ready`,
  `error_occurred`, `action_blocked`, and the per-action verdict pair
  `action_succeeded` / `action_failed`, plus `status_message`.
- `DataManager` writes one HDF5 file to disk per procedure run.
- `plan.py` hands immutable value objects to every layer; a malformed plan
  raises at construction, at the guilty module, not deep in the tick loop.

## Interface contract

- Dependencies point strictly downward; `core/` never imports from `drivers/`,
  `virtual_instruments/`, `procedures/`, or `gui/` (except `station.py`, which
  imports the VI base classes it constructs).
- The `Orchestrator` is the only writer to hardware; procedures and the GUI
  submit requests and never touch VIs or drivers directly.
- Every plan object (`Target`, `Command`, `PhasePlan`, `StepPlan`, `ParamSpec`,
  `DataSchema`) validates eagerly at construction.
- Limits and constants live in config, never hardcoded here.

## How to add a new module

1. Create `core/your_module.py` with the PEP 257 header docstring (Input /
   Process / Output; see Workspace Rule 1 in CLAUDE.md).
2. Keep it instrument-agnostic: no imports from `drivers/` or
   `virtual_instruments/` (aside from the VI base classes `station.py` needs).
3. Write its tests in `tests/` before the module is considered done.
4. Add a row to the Files map below, including its owning test file.

## Files

Each row: responsibility, key public API, and the test file(s) in `tests/` that
verify it. "tests: none" means no dedicated coverage exists (not a suggestion to
skip it).

| File | Responsibility | Key public API | Tests |
|------|----------------|----------------|-------|
| `__init__.py` | Package marker | (none) | none |
| `plan.py` | Typed vocabulary of frozen dataclasses shared across every layer | `Target`, `Command`, `PhasePlan`, `StepPlan`, `ParamSpec`, `ParamGroup`, `DataSchema` (`.multiplexed()` per-route suffixing, `.validate()` raising `DataSchemaError`) | `test_plan.py` |
| `exceptions.py` | The exception hierarchy every layer catches by subtype | `CryoSoftError`, `CryoSoftCommunicationError`, `CryoSoftSafetyError`, `CryoSoftConfigError`, `DataSchemaError` | `test_foundation.py` |
| `decorators.py` | Marker decorators that tag VI methods for discovery and GUI generation | `monitored`, `control`, `get_monitored_methods()`, `get_control_methods()` | `test_foundation.py` |
| `station.py` | L2 registry: builds VIs from config, polls state with stale-value caching, dispatches ramps and measurement commands, aggregates safety | `Station` (`get_vi`, `get_vi_names`, `measurement_vi_names`, `switch_vi_names`, `measurement_selector_label`, `get_state`, `process_system_targets`, `send_measurement_commands`, `check_ramps`, `stop_ramps`, `get_ramp_status`, `check_safety`); `build_station()`, `build_station_with_fallback()`, `validate_config_dir()` | `test_l2_station.py`, `test_config_validation.py` |
| `orchestrator.py` | L3 single-threaded cooperative state machine; the sole hardware writer; runs the monitor + status cycle each tick inside an exception boundary that degrades to ERROR. Monitoring is OFF at construction (nothing polled until `start_monitoring()`); `run_procedure()` auto-starts it, stopping is refused outside IDLE/ERROR, `shutdown()` stops the tick timer | `Orchestrator` (`start_monitoring`, `stop_monitoring`, `is_monitoring`, `shutdown`, `run_procedure`, `queue_procedure`, `run_queue`, `pause_procedure`, `resume_procedure`, `abort_procedure`, `recover_from_error`, `acknowledge_emergency`, `submit_vi_action`, `submit_global_action`, `get_operational_status`); `OrchestratorState` enum; `monitoring_changed` signal | `test_l3_orchestrator.py` |
| `procedure.py` | L4 base classes: the Orchestrator-driven lifecycle and the generic sweep engine | `BaseProcedure` (`initiate` -> `PhasePlan`, `change_sweep_step` -> `StepPlan \| None`, `measure`, `standby` -> `PhasePlan`, `abort` -> `tuple[Command, ...]`, `get_param_groups()` classmethod); `SweepMeasureProcedure` (GUI-selected measurement VI, mux route selection, `DataSchema` assembly, per-route measure loop; concrete axes supply six hooks) | `test_l4_procedure.py`, `test_new_procedures.py` |
| `data_manager.py` | L5 HDF5 file lifecycle for one procedure run: pre-allocated datasets, per-point save, abort trimming | `DataManager` (`save_datapoint`, `close`) | `test_l5_data_manager.py` |
| `sweep_builder.py` | Reusable sweep-array construction and the declarative `SweepAxis` used by procedures | `SweepSegment`, `build_piecewise_sweep()`, `load_custom_sweep_csv()`, `apply_hysteresis()`, `SweepAxis`, `sweep_axis_param_specs()`, `build_axis_sweep()` | `test_sweep_builder.py` |
| `operational_status.py` | Pure builder of the per-tick runtime "why is the run slow/stuck" status record | `build_operational_status()`, `RunFaultCode`, `worst_code()`, `VIHealth` | `test_operational_status.py` |
| `watchdog.py` | Deterministic stall detection layered on the status record (RAMP_STALLED, STALLED_RUN) | `apply_watchdog()`, `WatchdogState`, `WatchdogConfig` | `test_watchdog.py` |
| `config_catalog.py` | Qt-free discovery and versioning of shipped vs user config directories (copy-on-edit fork, named history) | `ConfigCatalog`, `ConfigEntry`, `ConfigVersion` | `test_config_catalog.py` |
| `logging_config.py` | Configures the rotating file + console handlers and the `cryosoft.status` JSONL stream | `setup_logging()` | none |
