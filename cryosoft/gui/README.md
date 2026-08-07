# gui/

## Purpose

The GUI is the top layer of CryoSoft: the PyQt6 desktop interface through
which a lab user monitors live instrument state, configures and queues
measurement procedures, and views live data during a run. It talks only to the
Orchestrator's public API and never touches drivers or Virtual Instruments to
drive hardware.

## Architecture layer

**GUI (top layer).** Depends downward on: L3 Orchestrator (Qt signals + public
action methods), L4 Procedures and Operations (via `BaseProcedure.get_param_groups()`
returning `ParamGroup`/`ParamSpec` from `cryosoft.core.plan`, and `SweepAxis` from
`cryosoft.core.sweep_builder`; also `cryosoft.core.operation` — `OperationBase`,
`ReadinessCondition`, `NextDue` — and every concrete class under
`cryosoft.procedures.operations.*`, constructed directly by `OperationsPanel`/
`OperationCard` — contract C8 forbids only drivers and concrete VI
subpackages, not `cryosoft.procedures`/`cryosoft.core.operation`), L2 Station
(VI names, VI types, VI instances for decorator introspection only), and L6
Session (`cryosoft.session.servicing_log`/`models`/`manager` — contract
C11/C12 name the GUI and `main.py` as the only importers). Nothing depends on
the GUI.

### Pages (MonitorWindow)

`MonitorWindow` is paged: a slim `QTabBar` in the header (`page_tab_bar`)
switches a central `QStackedWidget` (`page_stack`) between two pages built
once in `_build_ui()`. **Page 1 (Monitor)** is the original fixed 2x2
quadrant grid, unchanged — the page split only moved that grid's root
splitter into the stack, it did not touch its internal layout. **Page 2
(Logs)** is `ServicingLogPage`: one table per configured servicing-log kind
(from `ServicingLogStore`/`LogKindSpec`) — the unified `"servicing"` kind
gets a dedicated chronological timeline with filter chips, a recording
detail dialog, and CSV exports (see `servicing_log_page.py`'s Files-table
entry) — the read-only `operations` audit table, and the relocated
`LogPanel`. Page 1's bottom-right quadrant is a horizontal splitter of two
sub-panels: `RampTrackerPanel` on the left (always built — every ramp
running right now, each with its own Abort) and the optional
`OperationsPanel` on the right (present when cryogenics is enabled OR an
`operations:` config block is declared — plan §12; a placeholder label
otherwise). The former Other Devices section is retired: measurement and
switch VIs are full, role-tagged `InstrumentPanel` cards in the instrument
grid. Tables refresh on log-page-shown and `run_finished` — never on a
timer. Child panels keep the established single-receiver `states_updated`
forwarding pattern (`MonitorWindow` receives the tick and forwards to
`TrendsQuadrant` / `OperationsPanel` / `RampTrackerPanel`; see the
destruction-order rule above);
signals that fire only at run boundaries
(`run_started`/`run_finished`/`action_succeeded`) are connected directly by
the panel (or, for `OperationsPanel`, each `OperationCard`) that needs them
— there is no teardown race for a signal that only fires while the window
is alive and not mid-tick.

## Entry (what comes in)

- A `Station` instance: the registered VI names, each VI's `vi_type`
  (`system` / `level` / `measurement` / `switch`), and the VI instances (passed
  to `InstrumentPanel` only to read `@monitored`/`@control` decorator metadata,
  never to call hardware methods).
- An `Orchestrator` instance. Signals the GUI connects to: `states_updated`,
  `state_changed`, `run_started`, `run_finished`, `error_occurred`,
  `error_event` (the structured `core.events.ErrorEvent` counterpart, plan
  operation-concurrency-and-error-scoping.md §3 — MonitorWindow's banner
  shows/clears a per-VI fault warning from it), `action_blocked`,
  `action_succeeded`, `action_failed`, `monitoring_changed`, and
  `operational_status` (consumed live by `DiagnosticsWindow`). Run-scoped
  signals are routed by run kind (**Hard status separation**, docs/plans/
  operation-concurrency-and-error-scoping.md §2 — see GLOSSARY.md):
  `procedure_progress`, `procedure_finished`, `measurement_ready`, and
  `status_message` fire ONLY for a procedure run (`ProcedureWindow`'s
  progress bar/queue-advance/plots/status log); `operation_status` and
  `operation_progress` fire instead for an operation run, routed through
  `MonitorWindow` to the Operations panel's `OperationCard` (never to
  `ProcedureWindow`, which additionally gates its Pause/Resume/Abort on
  `Orchestrator.active_run_kind()`). Actions the GUI
  submits: `submit_vi_action`, `submit_global_action`, `start_monitoring`,
  `stop_monitoring`, `run_procedure`, `queue_procedure`, `run_queue`,
  `pause_procedure`, `resume_procedure`, `abort_procedure`,
  `acknowledge` (unified EMERGENCY/hold-severity override, time-boxed —
  GLOSSARY.md's **Hold acknowledge**), `run_operation`, `finish_operation`,
  `stop_ramp` (stop ONE VI's ramp — the per-instrument counterpart of
  `abort_procedure`; the Orchestrator refuses it for a VI a run claims),
  `acknowledge_fault`, `retry_fault` (the runtime fault registry's GUI
  surface, plan §3 — the RUNTIME sibling of `connect_instrument()` for a VI
  that DID connect but has since gone stale/disconnected),
  `connect_instrument`, `disconnect_instrument` (the **connection lifecycle**
  in GLOSSARY.md: one `ConnectionButton` on every card, next to the
  Initiate/Standby toggle, and a card SWAP when it is pressed — the live
  `InstrumentPanel` becomes an `OfflineInstrumentPanel` and back).
- Optionally, the L6 cryogenics stack (docs/plans/archive/cryogenics-logbook.md §9/§10):
  a resolved `cryogenics:` config block (`Station.read_cryogenics_config()`),
  a `HeliumRecordStore`, a `ServicingLogStore`, the declared servicing-log
  kinds (`Station.read_servicing_logs_config()`), and a `CryogenicsRecorder`
  — all optional keyword params on `MonitorWindow`, wired by `main.py`.
  Every param defaults to `None`, so existing construction sites and tests
  are unaffected when omitted.
- Optionally, a resolved `operations:` config block (`Station.
  read_operations_config()`, `operations_config` keyword param, plan §12) —
  `{config_key: {key: value}}`. The Operations panel is available whenever
  cryogenics is enabled OR this is non-empty; every entry is mapped to a
  discovered `OperationBase` subclass by `config_key` (unknown keys log a
  warning and are skipped, never fatal).

## Exit (what goes out)

- Long-lived `QMainWindow`s (MonitorWindow owns ProcedureWindow and the config
  editor). User actions are submitted to the Orchestrator; nothing is written to
  hardware directly. Session content (sample metadata, data dir, last procedure
  and params, run queue) is persisted to a JSON file — the open L6 session's own
  `gui_state.json` when an experiment is open (also promoting the queue into the
  experiment record via `ExperimentManager.set_queue`), else the per-user AppData
  file; window geometry and splitter state go to `QSettings`.

## Interface contract

- **Never** import from `cryosoft.drivers.*`, and import from
  `cryosoft.virtual_instruments.*` only for the `BaseVirtualInstrument` type.
  Never call a VI method directly; route every hardware effect through an
  Orchestrator action.
- **Never** block the Qt event loop (no `time.sleep`, no synchronous I/O). Data
  arrives via Orchestrator signals; do not poll instruments.
- **`operations_panel.py`'s `OperationCard` is the single per-operation card
  standard.** It contains no per-operation logic — every visible
  detail (checklist rows, next-due line, status line, ready banner,
  confirmations) renders generically from an
  `OperationBase` instance's declarations (`readiness_conditions()`,
  `next_due()`, `ready_message`, `operator_confirmations`) plus a
  panel-supplied factory closure for building a fresh run instance. Finish is
  immediate: the button goes to a disabled "Finishing…" state the instant
  it's clicked, not on `run_finished`. An unmet postcondition at finish is
  recorded on the run manifest and logbook (see `session/README.md`) rather
  than surfaced on the card; the operator reviews it in the log. Adding an
  operation to a setup never touches this
  file; a new operation with none of those declarations still gets a working
  card (just a bare button).
- **`ramp_tracker_panel.py`'s `RampRow` is the single per-ramp row
  standard.** Like `OperationCard`, it contains no per-instrument logic:
  every visible detail renders from a `core.ramps.RampRecord`, and the Abort
  button's enabled state comes from that record's `stoppable` flag — which
  the Orchestrator computed with the very predicate `stop_ramp()` uses, so
  the button can never look enabled for a stop the action would refuse. A
  new rampable VI appears here for free the moment it implements the
  ramp-introspection standard (`virtual_instruments/README.md`); this file
  never changes.
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
   value objects and the Orchestrator/Station, `cryosoft.session.*` (L6),
   `cryosoft.procedures.*` (procedures and operations — never their VI/driver
   dependencies), and other `cryosoft.gui.*` widgets. Do not import drivers or
   call VIs directly.
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
| `app_settings.py` | `QSettings` factory (a test seam) plus machine-level identity persisted through it: the per-user autosave-file path resolver, shipped/user config dirs, the active-config identity `(name, source)` (survives running from another clone/worktree), and who is currently logged in. The fixed measurement root L6 session/experiment folders live under is resolved by `cryosoft.core.paths.measurement_root()` instead — a machine-level, admin-set value, not GUI-editable through this module. | `get_settings`, `autosave_file_path`, `shipped_config_dir`, `user_config_dir`, `config_active`, `set_config_active`, `current_user_id`, `set_current_user_id` | `tests/test_gui.py` |
| `form_autosave.py` | Qt-free form-autosave model (sample metadata, data dir, last procedure + params, run queue), serialised to one JSON file; never raises on a corrupt file. Historically `session.py` — renamed so "session" is free for the L6 Session Management layer; its class was renamed too (`FormAutosaveState`, formerly `SessionState`), while the on-disk JSON filename is unchanged. | `FormAutosaveState`, `load`, `save` | `tests/test_form_autosave.py` |
| `theme.py` | Light "lab" colour palette constants and the application-wide QSS string. Includes `BTN_DANGER_DISABLED` and its `QPushButton[class="danger"]:disabled` rule — deliberately low contrast (WCAG 1.4.3 exempts disabled controls), so a destructive action the app is currently refusing does not read as available. | `build_stylesheet`, colour/class constants | `tests/test_gui.py` |
| `param_form.py` | The single `ParamSpec`-to-Qt-widget mapping, shared by the procedure form and the Logs page's servicing-log dialogs; builds labelled/tooltipped `QFormLayout` rows and the inverse read helpers. A `widget_hint="datetime"` (`str`-typed) field still gets a `QLineEdit` (an ISO 8601 string) with a placeholder showing the expected format — no dedicated date-picker widget yet. | `build_param_widget`, `build_form_layout`, `build_group_box`, `build_param_tooltip`, `collect_value`, `get_widget_raw`, `set_widget_raw` | `tests/test_gui.py` |
| `sweep_axis_widget.py` | Sweep-shape editor for a Procedure's declared `SweepAxis`: mode selector (Linear / Segments / CSV) over a stacked sub-form, a 2-column segment breakpoint table (`field_segments`), and a hysteresis checkbox. The only GUI code sweep-shape support needs. | `SweepAxisWidget`, `get_params` | `tests/test_sweep_axis_widget.py` |
| `instrument_panel.py` | Auto-generated per-VI `QGroupBox`: `@monitored` methods become live `QLabel`s, `@control` methods become button + input rows (spec-declared params render via `param_form`: combo/checkbox/tooltipped fields). Card visibility: a `panels:` config allowlist wins, else each control's `panel=` default. Header holds a `LifecycleToggleButton` and the front-panel icon. Updates on each `states_updated` tick; flips a QSS `status` property on ok/stale/disconnected change, and shows/hides a fault row (message + Acknowledge + Retry, disabling every `@control` row) from the Availability standard's unified record (`Orchestrator.availability(vi_name)`'s `not_responding` tag, GLOSSARY.md's **Availability**) — the row's message still reads `Orchestrator.vi_faults()` for `kind`/`message`/`acknowledged`, fields the unified record does not carry, since acknowledge/retry are comm-specific actions. The RUNTIME sibling of `offline_panel.py`'s build-time fault card. | `InstrumentPanel` | `tests/test_gui.py` |
| `instrument_front_panel.py` | Per-VI child window showing the FULL capability surface (every `@monitored` value + every `@control`, panel-hidden ones included) by embedding an all-controls `InstrumentPanel` in a scroll area. Opened from the sliders icon on cards and switch rows; lazily created, reused. | `InstrumentFrontPanel` | `tests/test_gui.py` |
| `offline_panel.py` | Grid card + detail window for a VI CryoSoft is not holding — either it failed to connect at startup (degraded build) or the operator released it (**connection lifecycle**); both land in the Station's one offline registry, and `OfflineInstrument.tags` (the Availability standard's absence tags, `{"connect_failed"}`/`{"operator"}`/both — GLOSSARY.md's **Availability tag**) selects the wording ([OFFLINE] vs [DISCONNECTED], or both facts stated together for the two-tag case) via a frozenset-keyed wording table, and nothing else. The card carries the reason and the Connect button; the detail window repeats it with the full reason and a hint; both re-derive their wording (via `Orchestrator.availability(vi_name)`) on a failed reconnect, since that can change an already-offline VI's tags with no card swap to hang a refresh on. Both go through `Orchestrator.connect_instrument()`. MonitorWindow swaps the card for a live `InstrumentPanel` on success. Distinct from `instrument_panel.py`'s runtime fault row (a VI that DID connect but has since stopped answering — badged [NOT RESPONDING] so it never claims the operator's word "disconnected") — same idiom, no shared code. | `OfflineInstrumentPanel`, `OfflineFrontPanel` | `tests/test_gui.py` |
| `lifecycle_toggle.py` | The two per-instrument lifecycle controls every card carries side by side — the two axes of the **connection lifecycle** (GLOSSARY.md). `LifecycleToggleButton`: one state-dependent Initiate/Standby button with a status glow dot (what the instrument is *doing*); state changes only on `action_succeeded`, never optimistically on click. `ConnectionButton`: a one-way Connect or Disconnect button (who *owns* the instrument) — one-way rather than a toggle because the card itself is swapped when the state flips; `compact=True` renders it icon-only for the narrow monitor card, where a labelled button would clip the VI name and the Initiate toggle. | `LifecycleToggleButton`, `ConnectionButton`, `set_initiated`, `is_initiated` | `tests/test_lifecycle_toggle.py` |
| `notification_banner.py` | Hidden-by-default inline strip for non-modal `warning`/`error` messages; a repeated identical message bumps a counter instead of stacking. Replaced the old modal `QMessageBox` storms. | `NotificationBanner`, `show_message` | `tests/test_gui.py` |
| `live_plot_panel.py` | Reusable live X/Y plot panel (X + Y selectors, optional per-slot Loop 1 / Loop 2 selectors for looped measurements, themed `pyqtgraph` curve); axis keys stay plain and the panel indexes directly into each measurement column's `(n_loop1, n_loop2)` grid at draw time (selector item data is the 0-based axis index). Every plottable value is already a scalar — computing it is the measurement method's job, never the panel's. ProcedureWindow hosts two, driven by `measurement_ready`. | `LivePlotPanel`, `set_available_keys`, `set_available_loop_labels`, `redraw`, `clear` | `tests/test_gui.py` (via ProcedureWindow `_plot1`/`_plot2`) |
| `monitor_history.py` | Qt-free ring-buffer of time-series readings, one bounded deque per flat key. Two entry points: `record()` (live path, flattens nested state dicts like `Station.last_state_flat()`, includes measurement VIs) and `record_flat()` (replay path for disk-persisted trend history, takes an already-flat dict, excludes measurement VIs — the accepted asymmetry, see class docstring). Feeds the trend plots. | `MonitorHistory`, `record`, `record_flat`, `series`, `keys` | `tests/test_monitor_history.py` |
| `trend_plot_panel.py` | Reusable trend plot: one variable vs wall-clock time (`DateAxisItem`), reading from a shared `MonitorHistory`; Y-variable + time-window selectors and a remove button. `TIME_WINDOWS` runs 15 min through 1 y; windows up to 24 h read `MonitorHistory` in RAM as before, "7 d"/"1 y" read the disk-backed tiered trend-history store via `cryosoft.core.trend_history.read_window()` (tier choice stays in that module, not here — plan §4), synchronously (no thread — single-threaded cooperative scheduling). A disk read failure/emptiness (including the measurement-VI asymmetry, plan §7) renders an empty curve, never raises. | `TrendPlotPanel`, `refresh`, `remove_requested` signal, `TIME_WINDOWS` | `tests/test_trend_plot_panel.py`, `tests/test_gui.py` |
| `window_geometry.py` | Shared window-geometry persistence: restore a saved geometry (rejecting one that landed off-screen), fall back to a centered screen-fraction default, save on close. Used by both windows. | `restore_or_center`, `save_geometry`, `geometry_on_screen` | `tests/test_gui.py` |
| `log_panel.py` | The read-only real-time log view (`log_panel`) plus `QtLogHandler`, the coloured-HTML logging handler it owns; `attach()`/`detach()` manage the handler's lifetime on the shared "cryosoft" logger. | `LogPanel`, `QtLogHandler` | `tests/test_gui.py` |
| `experiment_info_panel.py` | The GUI surface for the Experiment tier: an experiment status/Start-Close control (ExperimentManager, optional), sample name/ID/comments and the derived-but-editable data-directory field with Browse (forced to the open experiment's own `data/` folder on open/switch, restored to its pre-experiment text on close; a plain `data_dir_note` label is live typing feedback whenever the field points outside the experiment folder), and an eLab status line (reflects `ElnLink`; publish controls land with Track B). Containment is hard: `_on_browse_dir()` refuses a selection outside the open experiment's folder outright, and `is_data_dir_contained()` is the read `MonitorWindow.get_data_dir_for_run()` enforces before a run is allowed to start. Setup-tier concerns (config identity, instrument metadata, user login) live in the menu bar instead — see `monitor_window.py`/`setup_dialogs.py`. Sample fields stay free-editable per run regardless of experiment state; whatever they hold at "Start Experiment" time is snapshotted onto the `ExperimentRecord`. | `ExperimentInfoPanel`, `get_sample_info`, `get_data_dir`, `is_data_dir_contained`, `apply_session` | `tests/test_gui.py` |
| `experiment_dialogs.py` | Modal dialogs for the experiment lifecycle: `StartExperimentDialog` (title, an optional Folder name override for the experiment's directory — auto-filled from the title until hand-edited, same pattern as `AddUserDialog`'s id field — user picker with inline "New user…", attendance checkbox) and `CloseExperimentDialog` (findings text), plus the shared `UserPickerWidget` (roster combo + inline "New user…" → `AddUserDialog`) reused by `setup_dialogs.LoginDialog`. Opened only by `ExperimentInfoPanel`; every `ExperimentManager` mutation happens in the panel after a dialog accepts. | `StartExperimentDialog`, `CloseExperimentDialog`, `AddUserDialog`, `UserPickerWidget` | `tests/test_gui.py` |
| `setup_dialogs.py` | Modal dialogs for the Setup tier: `LoginDialog` (pick/create who's using the app, via the shared `UserPickerWidget`) and `InstrumentInfoDialog` (read-only view of each VI's `devices.yaml` `metadata:` block). Opened from MonitorWindow's User and Config menus respectively. | `LoginDialog`, `InstrumentInfoDialog` | `tests/test_gui.py` |
| `open_experiment_dialog.py` | `OpenExperimentDialog`: lists every experiment from `session_manager.store.list_experiments()` (title/user/status/created date resolved via `store.load()`); open ones selectable, closed ones grayed out with a "(closed)" suffix and disabled via item flags (never a stylesheet). Mirrors `UserPickerWidget`'s list-plus-accept pattern. Opened from MonitorWindow's User menu ("Load Session…"), which drives the actual switch via `_switch_experiment`. | `OpenExperimentDialog`, `selected_experiment_id` | `tests/test_gui.py` |
| `session_dialogs.py` | `ResumeSessionDialog`: lists every Session owned by a required `user_id` (the L6 tier above `session_manager` — a named, resumable, per-user folder holding multiple experiments, nested at `sessions/<user_id>/`) from `SessionStore.list_sessions(user_id)`, plus an inline "New session…" name field and Create button (mirrors `AddUserDialog`'s simplest create-new-named-thing pattern). Opened from MonitorWindow's User menu ("Resume Session…") — nobody-logged-in resolves to the Guest user first — which persists the pick via `SessionStore.set_active(user_id, session_id)` — takes effect on next launch, `session_manager`'s own `ExperimentStore` is never rebound live. | `ResumeSessionDialog`, `selected_session_id` | `tests/test_gui.py` |
| `operations_panel.py` | Bottom-right quadrant entry (page 1), built when cryogenics is configured OR an `operations:` config block is declared. A single column of one generic `OperationCard` per available operation — no separate cryogenics status display; when `cryogenics:` is configured, a consumption rate (%/h over a fixed 6h window via `consumption_rate_pct_per_h`) is still computed internally purely to feed the helium-fill card's `next_due()` prediction. Each card: a live readiness checklist (from `readiness_conditions()`), a next-due line (from `next_due()`), a live status line (`on_operation_status()`, forwarded from `operation_status` via `MonitorWindow`), an operator-confirmations row while active, a ready banner once done + all-green, an unmet-postcondition warning badge, and a start/finish button (`OperatorDialog` → fresh instance → `orchestrator.run_operation()`/`finish_operation()`/`confirm_operation()` — Finish immediately disables the button into a "Finishing…" state, docs/plans/archive/operation-concurrency-and-error-scoping.md §2). Zero per-operation code — cards are built from `OperationBase` declarations plus `discover_operations()`. | `OperationsPanel`, `OperationCard`, `OperatorDialog`, `on_states_updated`, `on_operation_status` | `tests/test_operations_panel.py` |
| `ramp_tracker_panel.py` | Bottom-right quadrant's LEFT sub-panel (page 1), always built. One generic `RampRow` per ramp running right now — group-box title `"<setpoint label> · <vi_name>"`, a `value → next setpoint` readout, an `End setpoint … · rate · phase` detail line, an `Owned by <run>` line for a run-driven ramp, and an Abort button that confirms via `QMessageBox` then calls `Orchestrator.stop_ramp(vi_name)`. Fed by `on_ramps_updated()` (forwarded from `ramps_updated` via `MonitorWindow`, which fires every tick); rows are added/updated in place/removed rather than rebuilt, so objectNames stay stable and a button is never destroyed mid-click. Abort is disabled with the refusal as its tooltip whenever the record is not `stoppable` — the case where a running procedure or operation claims the VI, for which the correct stop is aborting the run. Zero per-instrument code. | `RampTrackerPanel`, `RampRow`, `on_ramps_updated`, `row_names` | `tests/test_ramp_tracker_panel.py` |
| `servicing_log_page.py` | Page 2 (Logs): one table per declared servicing-log kind (columns from its `LogKindSpec.fields`, newest first) with Add/Edit/Delete/History for editable kinds (dialogs built from `param_form.py`), the read-only `operations` audit table, and the relocated `LogPanel`. The unified `"servicing"` kind (docs/plans/archive/unified-servicing-log-and-run-recording.md §4) gets a dedicated `_ServicingLogTable` instead of the generic per-kind table: one chronological timeline (sorted by parsed `start_utc`, falling back to `created_utc`, newest first), filter chips derived live from the data's `entry_kind` values, a "View recording" row-detail dialog (`ServicingRecordingDialog`, a lazily-loaded `pyqtgraph` plot of every recorded channel, tolerant of a missing/corrupt sidecar) and two CSV exports ("Export table as CSV" over the filtered rows, "Export recording as CSV" in the detail dialog) built on Qt-free pure helpers (`sorted_servicing_entries`, `distinct_entry_kinds`, `filter_entries_by_kind`, `write_servicing_table_csv`, `load_recording_sidecar`, `write_recording_csv`) so tests never drive a file dialog. `refresh()` is called on log-page-shown and `run_finished` — never on a timer. | `ServicingLogPage`, `ServicingLogEntryDialog`, `RevisionHistoryDialog`, `ServicingRecordingDialog`, `refresh` | `tests/test_servicing_log_page.py` |
| `trends_quadrant.py` | The Trends quadrant: 1-4 `TrendPlotPanel`s auto-arranged into a `ceil(sqrt(N))` grid, backed by the `MonitorHistory` it owns; Add button (cap 4), per-panel remove (floor 1), opportunistic temperature/level default keys, and QSettings persistence of the panel list. On construction, rehydrates `MonitorHistory` by replaying the raw trend-history tier (`log_dir`, defaulting to `logging_config.log_directory()`) through `record_flat()`, non-fatally — a missing/corrupt store degrades to an empty history rather than blocking GUI startup. | `TrendsQuadrant`, `on_states_updated`, `save_settings`, `restore_settings` | `tests/test_gui.py`, `tests/test_trends_quadrant.py` |
| `config_menu.py` | The Config menu: checkable shipped/user config list, the confirm-switch-persist-restart flow, and the lazy config-editor launcher. Built by MonitorWindow only when a `ConfigCatalog` is provided. | `ConfigMenuController` | `tests/test_gui.py` |
| `procedure_discovery.py` | Qt-free procedure/operation auto-discovery: imports every `cryosoft.procedures` (or `cryosoft.procedures.operations`) module and returns the named `BaseProcedure`/`OperationBase` subclasses at any depth. | `discover_procedures`, `discover_operations`, `all_subclasses` | `tests/test_gui.py` (via ProcedureWindow), `tests/test_operations_panel.py` (via `OperationsPanel`) |
| `procedure_params_panel.py` | The parameter quadrant of ProcedureWindow: procedure selector row (Add to Queue / Run Now), filename-prefix field, and the auto-generated form — Sweep column with `SweepAxisWidget`, composite Measurement column (method drop-down + selected VI's sub-form), Reading loop column (two generic slots: a loopable-parameter drop-down each, with per-choice pick checkboxes or a value-list text field); structural params trigger a keyed diff re-render. Owns the per-procedure raw-text param cache behind session persistence. Signals `structure_changed`/`routes_changed` let the window sync its plot selectors. | `ProcedureParamsPanel`, `collect_values`, `current_selections`, `export_session_state`, `restore_session` | `tests/test_gui.py` |
| `queue_panel.py` | The run-queue group box: list + reorder/remove/Run Queue buttons, per-item lifecycle status (pending/running/done/failed), Orchestrator pending-queue resync after reorders (the GUI queue is the source of truth), and session restore/export of queued procedures. | `QueuePanel`, `QueueEntry`, `add_entry`, `notify_finished`, `notify_aborted`, `restore_items`, `export_items` | `tests/test_gui.py` |
| `monitor_window.py` | Main live-monitor window — a composition shell. A header `QTabBar` (`page_tab_bar`) switches a central `QStackedWidget` (`page_stack`) between Page 1 (Monitor: the fixed 2x2 quadrant grid of nested `QSplitter`s, draggable/not closable — top-left a 2-column `InstrumentPanel` list for system/level VIs, top-right a `TrendsQuadrant`, bottom-left an `ExperimentInfoPanel`, bottom-right a horizontal splitter of a `RampTrackerPanel` and an optional `OperationsPanel`) and Page 2 (Logs: a `ServicingLogPage`, see "Pages" above). Hosts the Start/Stop Monitoring toggle (mirrors `monitoring_changed`; monitoring is off at launch until instruments are initiated), Initiate/Standby All, the state-driven status bar, the notification banner (also used by the `CryogenicsRecorder`'s `cryo_warning` signal, `ExperimentManager.store_health_changed` — a save failure/recovery — and, per-VI, `Orchestrator.error_event` — plan §3), the single-home ACKNOWLEDGE button (moved off `procedure_window.py`, visible in EMERGENCY or whenever a hold-severity condition has a VI held, synced at construction so a pre-existing condition is not missed) and its top-bar "Acknowledged (mm:ss)" countdown while an override is active, session/splitter persistence, and the menu bar — including the Setup tier's surfaces: the User menu (`Log in as…` switches which per-user form-autosave file is loaded/saved, via `setup_dialogs.LoginDialog`; `Load Session…` opens `open_experiment_dialog.OpenExperimentDialog` and switches the open L6 experiment via `_switch_experiment`; `Resume Session…` opens `session_dialogs.ResumeSessionDialog` and persists the pick as the active L6 Session via `SessionStore.set_active()`, taking effect on next launch; a header label reflects who's logged in) and the Config menu's `Instrument Info…` action (`setup_dialogs.InstrumentInfoDialog`, reading `core.station.read_instrument_metadata()`). Also connects `ExperimentManager.experiment_changed` itself, loading a newly opened/switched session's own `gui_state.json` over the in-memory `FormAutosaveState` (skipped for a brand-new experiment that has none yet, so Start Experiment never wipes just-typed fields). The window is deliberately the `states_updated` (and, likewise every tick, `operation_status` and `ramps_updated`) receiver, forwarding each to the panels — Qt severs a receiver's connections at the start of its destruction, so no tick can reach a partially destroyed child tree. | `MonitorWindow` | `tests/test_gui.py`, `tests/test_operations_panel.py` |
| `procedure_window.py` | Procedure builder, run queue, and live-data window — a composition shell over `ProcedureParamsPanel`, `QueuePanel`, and two `LivePlotPanel`s. Same 2x2 splitter grid (params / queue-over-status / Plot 1 / Plot 2); single procedure-construction path shared by Run Now and the queue; progress bar from `procedure_progress`. Operation-blind (plan §2): Pause/Resume/Abort no-op while `Orchestrator.active_run_kind() == "operation"`. No longer connects `state_changed` at all — the emergency-acknowledge button moved to `monitor_window.py` (plan §3, single home) and nothing else here needed it. | `ProcedureWindow` | `tests/test_gui.py` |
| `diagnostics_window.py` | Read-only live diagnostics window for connection/progress trouble ("a device stopped responding", "this is taking way longer than expected"). Renders the Orchestrator's `operational_status` tick — verdict badge, per-instrument status table, alerts feed — with the same plain-English fault-code vocabulary as the offline troubleshoot CLI; a Copy Diagnostics button puts a text summary on the clipboard. Polls no hardware and reads no files. Opened from MonitorWindow's Diagnostics menu. | `DiagnosticsWindow` | `tests/test_gui.py` |
| `config_editor.py` | Interactive editor for device/instrument configs: lists shipped (read-only) and user configs, edits `devices.yaml`/`monitor.yaml` behind a hard validation gate, forks shipped configs to user copies, keeps versioned history, and applies a config (restart via injected callback). Opened from MonitorWindow's Config menu. | `ConfigEditorWindow` | `tests/test_config_editor.py` |
