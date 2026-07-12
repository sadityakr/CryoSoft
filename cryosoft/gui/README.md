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
| `app_settings.py` | `get_settings() -> QSettings` — single factory for the app's `QSettings("CryoSoft", "CryoSoft")` store. Exists as a *dependency seam*: GUI tests monkeypatch this factory so window-geometry persistence never touches the real Windows registry. Both windows import the module and call `app_settings.get_settings()` (not the function directly) so the patch is seen at every call site. Also resolves the session file (`session_file_path()`), the shipped/user config dirs (`shipped_config_dir()`, `user_config_dir()`), and the active-config pointer (`config_active_path()` / `set_config_active_path()`). |
| `config_editor.py` | `ConfigEditorWindow(QMainWindow)` — interactive editor for device/instrument configs. Lists shipped (read-only) and user configs, edits `devices.yaml`/`monitor.yaml` as text behind a hard validation gate (`core.station.validate_config_dir` dry-run; an invalid config cannot be saved), forks shipped configs to editable user copies (copy-on-edit), keeps a named version history per user config (`ConfigCatalog`, browse + restore), and Apply persists the config + triggers a restart via an injected callback. Opened from MonitorWindow's Config menu. |
| `instrument_panel.py` | `InstrumentPanel(QGroupBox)` — auto-generated per-VI panel. Discovers `@monitored` methods and renders them as live `QLabel` displays; discovers `@control` methods and renders them as `QPushButton` + `QLineEdit` input rows. Connected to `orchestrator.states_updated`. Sets a QSS `status` property (`stale`/`disconnected`) for the amber/red border defined in `theme.py`, only when the status changes. |
| `live_plot_panel.py` | `LivePlotPanel(QGroupBox)` — reusable live X/Y plot panel (X-axis selector, Y-axis selector, themed `pyqtgraph` curve). A *widget extraction* of ProcedureWindow's formerly duplicated Plot 1 / Plot 2 code. `set_available_keys(keys, default_x, default_y)` repopulates the selectors (preserving a still-valid choice); `redraw(datapoints)` plots scalar X/Y series from a datapoint history it retains for selector-driven redraws; `clear()` empties the curve. Legacy objectNames (`x1_axis_selector`, `y_axis_selector`, `live_plot`, etc.) are passed through so `findChild` still resolves them. |
| `notification_banner.py` | `NotificationBanner(QWidget)` — hidden-by-default inline strip for non-modal `warning`/`error` messages. `show_message(message, severity)` shows a severity-coloured strip with a dismiss button; a repeated identical message bumps a `(N×)` counter instead of stacking a second banner. Styled via a dynamic `severity` QSS property in `theme.py`. Used by MonitorWindow and ProcedureWindow to replace the old `QMessageBox` storms. |
| `monitor_history.py` | `MonitorHistory` — Qt-free, pure-Python ring-buffer (stdlib only) that accumulates time-series history of instrument readings for the Monitor window's trend plots. `record(state, timestamp=None)` flattens a nested `{vi_name: {field: value}}` state dict the same way `Station.last_state_flat()` does and appends to a per-key `deque(maxlen=...)`; `series(key, window_s=None, now=None)` returns `(times, values)`. One instance is owned by `MonitorWindow`, fed from `orchestrator.states_updated`. |
| `trend_plot_panel.py` | `TrendPlotPanel(QGroupBox)` — reusable trend plot panel showing one variable vs wall-clock time, reading from a `MonitorHistory`. A Y-variable selector, a time-window selector (15 min / 1 h / 6 h / 24 h), and a remove button sit above a `pyqtgraph` curve with a `DateAxisItem` X axis. `refresh()` repopulates the Y combo from `history.keys()` (preserving selection) and redraws from `history.series(key, window_s=...)`; called on every Orchestrator tick. Emits `remove_requested(panel_id)` when the remove button is clicked. Hosted 1–4 at a time by `monitor_window.py`'s Trends section. |
| `monitor_window.py` | `MonitorWindow(QMainWindow)` — main live-monitor window. Everything below the header/banner lives inside an inner `QMainWindow` used purely as a dock host (`dock_host`, `setDockNestingEnabled(True)`). Every panel is a `QDockWidget`: one `dock_{vi_name}` per system/level `InstrumentPanel`, one `dock_trend_{n}` per `TrendPlotPanel` (1–4, backed by one shared `MonitorHistory`), and one each for Other Devices (`dock_other_devices`, a compact one-row-per-VI status list), Log (`dock_log`), and Sample Info (`dock_sample_info`). The DEFAULT layout (built on first launch, and by "Restore default layout") puts two instrument columns on the left, two stacked trend docks on the right, and a tabbed Other Devices/Log/Sample Info dock along the bottom (Log active), with proportions enforced via `resizeDocks`. A View menu exposes every dock's `toggleViewAction()` (collapse/restore) plus "Add trend plot" (up to 4, floor of 1 via each panel's own remove button — a dock's own close button only hides it), "Save layout" (persists `dock_host.saveState()` + trend selections explicitly, not on close), and "Restore default layout". Hosts global "Initiate All" / "Standby All" buttons. Status bar reflects Orchestrator state via `state_changed` signal. Persists window geometry always; dock layout and Trends panel key/window selections only on explicit "Save layout", via `app_settings.get_settings()`. |
| `procedure_window.py` | `ProcedureWindow(QMainWindow)` — procedure builder and live-data window. Auto-discovers `BaseProcedure` subclasses from `cryosoft.procedures.*`. Auto-generates parameter forms from `BaseProcedure.parameters` dicts. Manages a local queue (mirrored into the Orchestrator via `queue_procedure`). Both the run-now and queue paths construct instances through the single `_build_procedure_instance` path. Two `LivePlotPanel`s (driven by `orchestrator.measurement_ready`) show live data. Progress bar driven by `orchestrator.procedure_progress`. Emergency-acknowledge button visible only in EMERGENCY state. |

## Signal → widget mapping

| Orchestrator signal | Receiver | Effect |
|---------------------|----------|--------|
| `states_updated(dict)` | `InstrumentPanel._on_states_updated` | Updates value labels every tick; updates the status-border property only when the stale/disconnected status changes. |
| `states_updated(dict)` | `MonitorWindow._on_states_updated_for_history` | Records the snapshot into the window's `MonitorHistory`, then calls `refresh()` on every live `TrendPlotPanel` (repopulates its Y combo, redraws its curve). Independent of the `InstrumentPanel` connections above — each panel connects itself. |
| `state_changed(str)` | `MonitorWindow._on_state_changed` | Updates status bar label and its state-driven `level` colour property (default / `active` / `error`). |
| `state_changed(str)` | `ProcedureWindow._on_state_changed` | Shows/hides emergency-acknowledge button. |
| `procedure_progress(float)` | `ProcedureWindow._on_progress` | Fills progress bar (0–100%). |
| `measurement_ready(dict)` | `ProcedureWindow._on_measurement_ready` | Appends point to live plot buffers and redraws curve. |
| `procedure_finished()` | `ProcedureWindow._on_procedure_finished` | Sets progress bar to 100%, clears active procedure ref. |
| `error_occurred(str)` | `MonitorWindow._on_error`, `ProcedureWindow` (lambda) | Logs the error and shows it in the window's `NotificationBanner` as an `error` (non-modal; no dialog). |
| `action_blocked(str)` | `MonitorWindow._on_action_blocked`, `ProcedureWindow` (lambda) | Shows the reason in the window's `NotificationBanner` as a `warning`. Replaces the old per-`InstrumentPanel` `QMessageBox.warning` and its substring-matching bug. |
| `action_failed(str, str, str)` | `MonitorWindow._on_action_failed` | Shows `"{vi}.{method} failed: {reason}"` in the `NotificationBanner` as an `error` — the failure half of the control-validation standard's per-action verdict (limit rejections and VI safety guards arrive here with the VI's reason verbatim). |
| `action_succeeded(str, str)` | `MonitorWindow._on_action_confirmed`; `InstrumentPanel`/Other-Devices lifecycle toggles | Transient status-bar confirmation (`"{vi}.{method} ✓ done"`, self-expiring — success must not demand a dismissal click); also flips `LifecycleToggleButton` state for `initiate`/`standby` actions. |
