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
  `action_failed` (plus `operational_status` / `status_message` for
  troubleshooting). Actions the GUI submits: `submit_vi_action`,
  `submit_global_action`, `run_procedure`, `queue_procedure`, `run_queue`,
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
| `monitor_window.py` | Main live-monitor window. Fixed 2x2 quadrant grid of nested `QSplitter`s (draggable, nothing closable/floatable): top-left a 2-column `InstrumentPanel` list for system/level VIs, top-right a Trends panel of 1-4 `TrendPlotPanel`s auto-arranged into a `ceil(sqrt(N))` grid, bottom-left Sample Info, bottom-right Other Devices / Log behind a `QComboBox`. Measurement and switch VIs appear in the Other Devices section; switch rows are display-only with a live route label refreshed each `states_updated` tick. Hosts Initiate/Standby All, the state-driven status bar, the notification banner, and the Config/Procedures menus. | `MonitorWindow` | `tests/test_gui.py` |
| `procedure_window.py` | Procedure builder, run queue, and live-data window. Auto-discovers `BaseProcedure` subclasses; renders forms from `get_param_groups()` through `param_form`; structural params trigger form re-derivation. Has a composite Measurement column (method drop-down + selected VI's sub-form), a narrow Scanner (mux) column, and a `SweepAxisWidget` in the Sweep column. Same 2x2 splitter grid; two `LivePlotPanel`s driven by `measurement_ready`; progress bar from `procedure_progress`; emergency-acknowledge button visible only in EMERGENCY. | `ProcedureWindow` | `tests/test_gui.py` |
| `config_editor.py` | Interactive editor for device/instrument configs: lists shipped (read-only) and user configs, edits `devices.yaml`/`monitor.yaml` behind a hard validation gate, forks shipped configs to user copies, keeps versioned history, and applies a config (restart via injected callback). Opened from MonitorWindow's Config menu. | `ConfigEditorWindow` | `tests/test_config_editor.py` |
