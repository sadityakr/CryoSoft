# gui/

## Purpose

The GUI layer is the top layer of CryoSoft. It provides the PyQt6 desktop interface through which a lab user monitors live instrument state, configures and queues measurement procedures, and views live data during a run. It never talks to drivers or Virtual Instruments directly — all communication goes through the Orchestrator.

## Architecture layer

**GUI — Layer 6 (top layer)**
Depends on: Layer 3 (Orchestrator signals), Layer 4 (BaseProcedure.parameters dict), Layer 2 (Station.get_vi_names()).

## Entry (what comes in)

- `Station` instance — provides the list of registered VI names and their instances (for introspection only, never for calling driver methods).
- `Orchestrator` instance — provides Qt signals (`states_updated`, `state_changed`, `procedure_progress`, `measurement_ready`, `procedure_finished`, `error_occurred`, `action_blocked`) and public methods (`submit_vi_action`, `submit_global_action`, `run_procedure`, `queue_procedure`, `run_queue`, `pause_procedure`, `resume_procedure`, `abort_procedure`, `acknowledge_emergency`).

## Exit (what goes out)

- User-visible windows that stay open for the lifetime of the application.
- UI actions are submitted to the Orchestrator via its public API — nothing is written to hardware directly.

## Interface contract

The GUI must **never**:
- Import from `cryosoft.drivers.*`.
- Import from `cryosoft.virtual_instruments.*` (except for type annotations via `BaseVirtualInstrument`).
- Call any method on a VI instance directly (only pass the instance to `InstrumentPanel` for introspection of decorator metadata).
- Block the Qt event loop (no `time.sleep`, no synchronous I/O).

## How to add a new panel or window

1. Create a new file in this folder (`my_panel.py`).
2. Import only from `PyQt6`, `cryosoft.core.orchestrator`, `cryosoft.core.station`, and `cryosoft.core.decorators`.
3. Connect to Orchestrator signals for data — do not poll instruments manually.
4. Add tests in `tests/test_gui.py` using the `qtbot` fixture from `pytest-qt`.

## Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker. |
| `instrument_panel.py` | `InstrumentPanel(QGroupBox)` — auto-generated per-VI panel. Discovers `@monitored` methods and renders them as live `QLabel` displays; discovers `@control` methods and renders them as `QPushButton` + `QLineEdit` input rows. Connected to `orchestrator.states_updated`. Applies orange/red border for stale/disconnected state. |
| `monitor_window.py` | `MonitorWindow(QMainWindow)` — main live-monitor window. Creates one `InstrumentPanel` per VI in a scrollable grid. Hosts global "Initiate All" / "Standby All" buttons. Status bar reflects Orchestrator state via `state_changed` signal. |
| `procedure_window.py` | `ProcedureWindow(QMainWindow)` — procedure builder and live-data window. Auto-discovers `BaseProcedure` subclasses from `cryosoft.procedures.*`. Auto-generates parameter forms from `BaseProcedure.parameters` dicts. Manages a local queue (backed by `orchestrator._procedure_queue`). Live `pyqtgraph` plot driven by `orchestrator.measurement_ready`. Progress bar driven by `orchestrator.procedure_progress`. Emergency-acknowledge button visible only in EMERGENCY state. |

## Signal → widget mapping

| Orchestrator signal | Receiver | Effect |
|---------------------|----------|--------|
| `states_updated(dict)` | `InstrumentPanel._on_states_updated` | Updates value labels and border style every tick. |
| `state_changed(str)` | `MonitorWindow._on_state_changed` | Updates status bar label. |
| `state_changed(str)` | `ProcedureWindow._on_state_changed` | Shows/hides emergency-acknowledge button. |
| `procedure_progress(float)` | `ProcedureWindow._on_progress` | Fills progress bar (0–100%). |
| `measurement_ready(dict)` | `ProcedureWindow._on_measurement_ready` | Appends point to live plot buffers and redraws curve. |
| `procedure_finished()` | `ProcedureWindow._on_procedure_finished` | Sets progress bar to 100%, clears active procedure ref. |
| `error_occurred(str)` | `MonitorWindow._on_error` | Opens a `QMessageBox.critical` dialog. |
| `action_blocked(str)` | `InstrumentPanel._on_action_blocked` | Opens a `QMessageBox.warning` dialog (only if VI name is in the message). |
