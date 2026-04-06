# core/

## Purpose

The `core/` package contains all infrastructure that the higher layers (Procedures, GUI) and lower layers (VIs, drivers) depend on. It defines the data structures, base classes, decorators, and runtime machinery that glue the system together. Nothing in `core/` knows about specific instruments — it is instrument-agnostic by design.

## Architecture layer

Cross-cutting infrastructure — used by every layer from L1 to GUI. The key runtime classes (`Station`, `Orchestrator`) are L2 and L3 respectively.

## Entry (what comes in)

- `station.py`: receives a YAML config directory path and constructs driver + VI instances.
- `orchestrator.py`: receives a `Station` instance and a tick interval; receives `Procedure` objects from the GUI.
- `data_manager.py`: receives experiment metadata and a `data_config` dict from a `Procedure`.
- `procedure.py`: receives a `Station`, sample info, data directory, and param values from the GUI.

## Exit (what goes out)

- `station.py` → station state dicts `{vi_name: {field: value}}` via `get_state()`; ramp completion signals via `check_ramps()`.
- `orchestrator.py` → Qt signals: `states_updated`, `state_changed`, `procedure_progress`, `procedure_finished`, `error_occurred`, `action_blocked`.
- `data_manager.py` → HDF5 file written to disk.
- `procedure.py` → abstract interface consumed by `Orchestrator`.

## Interface contract

| File | Key class / function | Role |
|------|---------------------|------|
| `exceptions.py` | `CryoSoftError`, `CryoSoftCommunicationError`, `CryoSoftSafetyError`, `CryoSoftConfigError` | All custom exceptions |
| `decorators.py` | `@monitored`, `@control` | VI method markers for GUI auto-generation and state polling |
| `station.py` | `Station`, `build_station(config_path)` | VI registry, state polling, ramp dispatch, safety |
| `orchestrator.py` | `Orchestrator(station, tick_interval_ms)` | Cooperative state machine (QObject + QTimer) |
| `procedure.py` | `BaseProcedure` | Abstract base for all measurement procedures |
| `data_manager.py` | `DataManager(...)` | HDF5 file lifecycle for one procedure run |
| `logging_config.py` | `setup_logging()` | Configures file + console logging |

## How to add a new module

1. Create `core/your_module.py` with a front-matter block (see Workspace Rule 1 in CLAUDE.md).
2. Keep the module instrument-agnostic — no imports from `drivers/` or `virtual_instruments/`.
3. Add it to this README's file list below.
4. Write tests in `tests/` before considering the module done.

## Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker (empty) |
| `exceptions.py` | All CryoSoft exception classes |
| `decorators.py` | `@monitored` and `@control` marker decorators; `get_monitored_methods()`, `get_control_methods()` discovery helpers |
| `station.py` | `Station` — VI registry, state polling with stale-value caching, ramp dispatch, safety aggregation; `build_station()` factory |
| `orchestrator.py` | `Orchestrator` — single-threaded cooperative state machine driven by `QTimer`; `OrchestratorState` enum |
| `procedure.py` | `BaseProcedure` — abstract base class defining the 4-method interface all procedures must implement |
| `data_manager.py` | `DataManager` — HDF5 file creation, metadata storage, pre-allocated dataset management, per-point save, abort trimming |
| `logging_config.py` | `setup_logging()` — configures rotating file handler + console handler |
