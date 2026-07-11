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
| `app_settings.py` | `get_settings() -> QSettings` — single factory for the app's `QSettings("CryoSoft", "CryoSoft")` store. Exists as a *dependency seam*: GUI tests monkeypatch this factory so window-geometry persistence never touches the real Windows registry. Both windows import the module and call `app_settings.get_settings()` (not the function directly) so the patch is seen at every call site. |
| `instrument_panel.py` | `InstrumentPanel(QGroupBox)` — auto-generated per-VI panel. Discovers `@monitored` methods and renders them as live `QLabel` displays; discovers `@control` methods and renders them as `QPushButton` + `QLineEdit` input rows. Connected to `orchestrator.states_updated`. Sets a QSS `status` property (`stale`/`disconnected`) for the amber/red border defined in `theme.py`, only when the status changes. |
| `live_plot_panel.py` | `LivePlotPanel(QGroupBox)` — reusable live X/Y plot panel (X-axis selector, Y-axis selector, themed `pyqtgraph` curve). A *widget extraction* of ProcedureWindow's formerly duplicated Plot 1 / Plot 2 code. `set_available_keys(keys, default_x, default_y)` repopulates the selectors (preserving a still-valid choice); `redraw(datapoints)` plots scalar X/Y series from a datapoint history it retains for selector-driven redraws; `clear()` empties the curve. Legacy objectNames (`x1_axis_selector`, `y_axis_selector`, `live_plot`, etc.) are passed through so `findChild` still resolves them. |
| `notification_banner.py` | `NotificationBanner(QWidget)` — hidden-by-default inline strip for non-modal `warning`/`error` messages. `show_message(message, severity)` shows a severity-coloured strip with a dismiss button; a repeated identical message bumps a `(N×)` counter instead of stacking a second banner. Styled via a dynamic `severity` QSS property in `theme.py`. Used by MonitorWindow and ProcedureWindow to replace the old `QMessageBox` storms. |
| `monitor_window.py` | `MonitorWindow(QMainWindow)` — main live-monitor window. Creates one `InstrumentPanel` per VI in a scrollable grid. Hosts global "Initiate All" / "Standby All" buttons. Status bar reflects Orchestrator state via `state_changed` signal. Persists window geometry via `app_settings.get_settings()`. |
| `procedure_window.py` | `ProcedureWindow(QMainWindow)` — procedure builder and live-data window. Auto-discovers `BaseProcedure` subclasses from `cryosoft.procedures.*`. Auto-generates parameter forms from `BaseProcedure.parameters` dicts. Manages a local queue (mirrored into the Orchestrator via `queue_procedure`). Both the run-now and queue paths construct instances through the single `_build_procedure_instance` path. Two `LivePlotPanel`s (driven by `orchestrator.measurement_ready`) show live data. Progress bar driven by `orchestrator.procedure_progress`. Emergency-acknowledge button visible only in EMERGENCY state. |

## Signal → widget mapping

| Orchestrator signal | Receiver | Effect |
|---------------------|----------|--------|
| `states_updated(dict)` | `InstrumentPanel._on_states_updated` | Updates value labels every tick; updates the status-border property only when the stale/disconnected status changes. |
| `state_changed(str)` | `MonitorWindow._on_state_changed` | Updates status bar label and its state-driven `level` colour property (default / `active` / `error`). |
| `state_changed(str)` | `ProcedureWindow._on_state_changed` | Shows/hides emergency-acknowledge button. |
| `procedure_progress(float)` | `ProcedureWindow._on_progress` | Fills progress bar (0–100%). |
| `measurement_ready(dict)` | `ProcedureWindow._on_measurement_ready` | Appends point to live plot buffers and redraws curve. |
| `procedure_finished()` | `ProcedureWindow._on_procedure_finished` | Sets progress bar to 100%, clears active procedure ref. |
| `error_occurred(str)` | `MonitorWindow._on_error`, `ProcedureWindow` (lambda) | Logs the error and shows it in the window's `NotificationBanner` as an `error` (non-modal; no dialog). |
| `action_blocked(str)` | `MonitorWindow._on_action_blocked`, `ProcedureWindow` (lambda) | Shows the reason in the window's `NotificationBanner` as a `warning`. Replaces the old per-`InstrumentPanel` `QMessageBox.warning` and its substring-matching bug. |
