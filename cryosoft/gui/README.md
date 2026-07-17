# gui/

## Purpose

The GUI is the top layer of CryoSoft: the PyQt6 desktop interface through
which a lab user monitors live instrument state, configures and queues
measurement procedures, and views live data during a run. It talks only to the
Orchestrator's public API and never touches drivers or Virtual Instruments to
drive hardware.

## Architecture layer

**GUI (top layer).** Depends downward on: L3 Orchestrator (Qt signals + public
action methods), L4 Procedures (via `BaseProcedure.get_param_groups()` returning
`ParamGroup`/`ParamSpec` from `cryosoft.core.plan`, and `SweepAxis` from
`cryosoft.core.sweep_builder`), and L2 Station (VI names, VI types, VI instances
for decorator introspection only). Nothing depends on the GUI.

## Entry (what comes in)

- A `Station` instance: the registered VI names, each VI's `vi_type`
  (`system` / `level` / `measurement` / `switch`), and the VI instances (passed
  to `InstrumentPanel` only to read `@monitored`/`@control` decorator metadata,
  never to call hardware methods).
- An `Orchestrator` instance. Signals the GUI connects to: `states_updated`,
  `state_changed`, `procedure_progress`, `procedure_finished`,
  `measurement_ready`, `error_occurred`, `action_blocked`, `action_succeeded`,
  `action_failed`, `monitoring_changed` (plus `operational_status` /
  `status_message` for troubleshooting). Actions the GUI submits:
  `submit_vi_action`, `submit_global_action`, `start_monitoring`,
  `stop_monitoring`, `run_procedure`, `queue_procedure`, `run_queue`,
  `pause_procedure`, `resume_procedure`, `abort_procedure`,
  `acknowledge_emergency`.

## Exit (what goes out)

- Long-lived `QMainWindow`s (MonitorWindow owns ProcedureWindow and the config
  editor). User actions are submitted to the Orchestrator; nothing is written to
  hardware directly. Session content (sample metadata, data dir, last procedure
  and params, run queue) is persisted to a JSON file; window geometry and
  splitter state go to `QSettings`.

## Interface contract

- **Never** import from `cryosoft.drivers.*`, and import from
  `cryosoft.virtual_instruments.*` only for the `BaseVirtualInstrument` type.
  Never call a VI method directly; route every hardware effect through an
  Orchestrator action.
- **Never** block the Qt event loop (no `time.sleep`, no synchronous I/O). Data
  arrives via Orchestrator signals; do not poll instruments.
- **`param_form.py` is the single ParamSpec-to-Qt-widget mapping.** It is the
  only module that names widget classes for procedure parameters
  (`choices` -> `QComboBox`, `bool` -> `QCheckBox`, else `QLineEdit`). All
  parameter forms build through it; L4 declares `ParamSpec`s and never mentions
  a widget.
- **GUI changes require offscreen screenshot verification** (run with
  `QT_QPA_PLATFORM=offscreen`; see the `gui-edit` skill). GUI tests must assert
  **visible geometry within a realistic viewport width**, not mere `findChild`
  existence: a past bug shipped because a widget existed in the tree but was
  laid out off-screen to the right. `test_gui.py`'s
  `_fully_inside_param_viewport` helper is the pattern to copy.

## How to add a new module

1. Create a file in this folder. Import only from `PyQt6`, `cryosoft.core.*`
   value objects and the Orchestrator/Station, and other `cryosoft.gui.*`
   widgets. Do not import drivers or call VIs.
2. If it introduces a new parameter input kind, add the branch to
   `param_form.py` and nowhere else.
3. Connect to Orchestrator signals for live data.
4. Add a behavior test in `tests/` (a dedicated `test_<widget>.py` for a
   reusable widget, or a case in `tests/test_gui.py`) using the `qtbot`
   fixture. Assert on-screen geometry, not just widget existence.
5. Update the Files table below in the same commit.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `__init__.py` | Package marker. | — | none |
| `app_settings.py` | `QSettings` factory (a test seam) plus resolver for the session-file path, shipped/user config dirs, and the active-config identity stored as `(name, source)` so it survives running from another clone/worktree. | `get_settings`, `session_file_path`, `shipped_config_dir`, `user_config_dir`, `config_active`, `set_config_active` | `tests/test_gui.py` |
| `form_autosave.py` | Qt-free form-autosave model (sample metadata, data dir, last procedure + params, run queue), serialised to one JSON file; never raises on a corrupt file. Historically `session.py` — renamed so "session" is free for the L6 Session Management layer; classes and the JSON file keep their old names for compatibility. | `SessionState`, `load`, `save` | `tests/test_form_autosave.py` |
| `theme.py` | Light "lab" colour palette constants and the application-wide QSS string. | `build_stylesheet`, colour/class constants | `tests/test_gui.py` |
| `param_form.py` | The single `ParamSpec`-to-Qt-widget mapping for the procedure form; builds labelled/tooltipped `QFormLayout` rows and the inverse read helpers. | `build_param_widget`, `build_form_layout`, `build_group_box`, `build_param_tooltip`, `collect_value`, `get_widget_raw`, `set_widget_raw` | `tests/test_gui.py` |
| `sweep_axis_widget.py` | Sweep-shape editor for a Procedure's declared `SweepAxis`: mode selector (Linear / Segments / CSV) over a stacked sub-form, a 2-column segment breakpoint table (`field_segments`), and a hysteresis checkbox. The only GUI code sweep-shape support needs. | `SweepAxisWidget`, `get_params` | `tests/test_sweep_axis_widget.py` |
| `instrument_panel.py` | Auto-generated per-VI `QGroupBox`: `@monitored` methods become live `QLabel`s, `@control` methods become button + input rows; a `LifecycleToggleButton` sits in the header. Updates on each `states_updated` tick; flips a QSS `status` property on ok/stale/disconnected change. | `InstrumentPanel` | `tests/test_gui.py` |
| `lifecycle_toggle.py` | One state-dependent Initiate/Standby button with a status glow dot, shared by `InstrumentPanel` and the Other Devices rows. State changes only on `action_succeeded`, never optimistically on click. | `LifecycleToggleButton`, `set_initiated`, `is_initiated` | `tests/test_lifecycle_toggle.py` |
| `notification_banner.py` | Hidden-by-default inline strip for non-modal `warning`/`error` messages; a repeated identical message bumps a counter instead of stacking. Replaced the old modal `QMessageBox` storms. | `NotificationBanner`, `show_message` | `tests/test_gui.py` |
| `live_plot_panel.py` | Reusable live X/Y plot panel (X + Y selectors, themed `pyqtgraph` curve); ProcedureWindow hosts two, driven by `measurement_ready`. | `LivePlotPanel`, `set_available_keys`, `redraw`, `clear` | `tests/test_gui.py` (via ProcedureWindow `_plot1`/`_plot2`) |
| `monitor_history.py` | Qt-free ring-buffer of time-series readings; flattens nested state dicts like `Station.last_state_flat()` into per-key bounded deques. Feeds the trend plots. | `MonitorHistory`, `record`, `series`, `keys` | `tests/test_monitor_history.py` |
| `trend_plot_panel.py` | Reusable trend plot: one variable vs wall-clock time (`DateAxisItem`), reading from a shared `MonitorHistory`; Y-variable + time-window selectors and a remove button. | `TrendPlotPanel`, `refresh`, `remove_requested` signal | `tests/test_trend_plot_panel.py`, `tests/test_gui.py` |
| `window_geometry.py` | Shared window-geometry persistence: restore a saved geometry (rejecting one that landed off-screen), fall back to a centered screen-fraction default, save on close. Used by both windows. | `restore_or_center`, `save_geometry`, `geometry_on_screen` | `tests/test_gui.py` |
| `log_panel.py` | The read-only real-time log view (`log_panel`) plus `QtLogHandler`, the coloured-HTML logging handler it owns; `attach()`/`detach()` manage the handler's lifetime on the shared "cryosoft" logger. | `LogPanel`, `QtLogHandler` | `tests/test_gui.py` |
| `sample_info_panel.py` | The Sample Info quadrant: sample name/ID/comments and the data-directory field with Browse. Single GUI owner of session-level sample metadata, surfaced to ProcedureWindow through MonitorWindow's accessors. | `SampleInfoPanel`, `get_sample_info`, `get_data_dir`, `apply_session` | `tests/test_gui.py` |
| `other_devices.py` | Compact Other Devices rows: measurement VIs get dot + status + Check button + `LifecycleToggleButton`; switch VIs get display-only rows with a live active-route label refreshed each tick via `on_states_updated`. | `OtherDevicesPanel` | `tests/test_gui.py` |
| `trends_quadrant.py` | The Trends quadrant: 1-4 `TrendPlotPanel`s auto-arranged into a `ceil(sqrt(N))` grid, backed by the `MonitorHistory` it owns; Add button (cap 4), per-panel remove (floor 1), opportunistic temperature/level default keys, and QSettings persistence of the panel list. | `TrendsQuadrant`, `on_states_updated`, `save_settings`, `restore_settings` | `tests/test_gui.py` |
| `config_menu.py` | The Config menu: checkable shipped/user config list, the confirm-switch-persist-restart flow, and the lazy config-editor launcher. Built by MonitorWindow only when a `ConfigCatalog` is provided. | `ConfigMenuController` | `tests/test_gui.py` |
| `procedure_discovery.py` | Qt-free procedure auto-discovery: imports every `cryosoft.procedures` module and returns the named `BaseProcedure` subclasses at any depth. | `discover_procedures`, `all_subclasses` | `tests/test_gui.py` (via ProcedureWindow) |
| `procedure_params_panel.py` | The parameter quadrant of ProcedureWindow: procedure selector row (Add to Queue / Run Now), filename-prefix field, and the auto-generated form — Sweep column with `SweepAxisWidget`, composite Measurement column (method drop-down + selected VI's sub-form), narrow Scanner (mux) column; structural params trigger a keyed diff re-render. Owns the per-procedure raw-text param cache behind session persistence. Signals `structure_changed`/`routes_changed` let the window sync its plot selectors. | `ProcedureParamsPanel`, `collect_values`, `current_selections`, `export_session_state`, `restore_session` | `tests/test_gui.py` |
| `queue_panel.py` | The run-queue group box: list + reorder/remove/Run Queue buttons, per-item lifecycle status (pending/running/done/failed), Orchestrator pending-queue resync after reorders (the GUI queue is the source of truth), and session restore/export of queued procedures. | `QueuePanel`, `QueueEntry`, `add_entry`, `notify_finished`, `notify_aborted`, `restore_items`, `export_items` | `tests/test_gui.py` |
| `monitor_window.py` | Main live-monitor window — a composition shell. Fixed 2x2 quadrant grid of nested `QSplitter`s (draggable, nothing closable/floatable): top-left a 2-column `InstrumentPanel` list for system/level VIs, top-right a `TrendsQuadrant`, bottom-left a `SampleInfoPanel`, bottom-right an `OtherDevicesPanel` / `LogPanel` pair behind a `QComboBox`. Hosts the Start/Stop Monitoring toggle (mirrors `monitoring_changed`; monitoring is off at launch until instruments are initiated), Initiate/Standby All, the state-driven status bar, the notification banner, the Session/Config/Procedures menus, and session/splitter persistence. The window is deliberately the `states_updated` receiver, forwarding each tick to the panels — Qt severs a receiver's connections at the start of its destruction, so no tick can reach a partially destroyed child tree. | `MonitorWindow` | `tests/test_gui.py` |
| `procedure_window.py` | Procedure builder, run queue, and live-data window — a composition shell over `ProcedureParamsPanel`, `QueuePanel`, and two `LivePlotPanel`s. Same 2x2 splitter grid (params / queue-over-status / Plot 1 / Plot 2); single procedure-construction path shared by Run Now and the queue; progress bar from `procedure_progress`; emergency-acknowledge button visible only in EMERGENCY. | `ProcedureWindow` | `tests/test_gui.py` |
| `config_editor.py` | Interactive editor for device/instrument configs: lists shipped (read-only) and user configs, edits `devices.yaml`/`monitor.yaml` behind a hard validation gate, forks shipped configs to user copies, keeps versioned history, and applies a config (restart via injected callback). Opened from MonitorWindow's Config menu. | `ConfigEditorWindow` | `tests/test_config_editor.py` |
