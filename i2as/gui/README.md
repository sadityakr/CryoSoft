# gui/

## Purpose

The GUI is the top layer of I2AS: the PyQt6 desktop interface through
which a lab user monitors live instrument state, configures and queues
measurement procedures, and views live data during a run. It talks only to the
Orchestrator's public API and never touches drivers or Virtual Instruments to
drive hardware.

## Architecture layer

**GUI (top layer).** Depends downward on: L3 Orchestrator (Qt signals + public
action methods), L4 Procedures (via `BaseProcedure.get_param_groups()`
returning `ParamGroup`/`ParamSpec` from `i2as.core.plan`, and `SweepAxis` from
`i2as.core.sweep_builder`; contract C8 forbids only drivers and concrete
VI subpackages, not `i2as.procedures`), L2 Station
(VI names, VI types, VI instances for decorator introspection only), and L6
Session (`i2as.session.models`/`manager` — contract
C11/C12 name the GUI and `main.py` as the only importers), and the analysis
package (`i2as.analysis.report` for the **Analysis report** the eLab tab
previews, and `i2as.analysis.discovery` for the recipe catalogue, imported
lazily so a build without it degrades rather than fails). Nothing depends on
the GUI.

### Pages (MonitorWindow)

`MonitorWindow` is paged: a slim `QTabBar` in the header (`page_tab_bar`)
switches a central `QStackedWidget` (`page_stack`) between two pages built
once in `_build_ui()`. **Page 1 (Monitor)** is the fixed 2x2
quadrant grid — the page split only moved that grid's root
splitter into the stack, it did not touch its internal layout. **Page 2
(Logs)** holds the `LogPanel` and nothing else: it is a bare log page, with
no table or record view of its own. Page 1's bottom-right quadrant is a VERTICAL splitter
(`agents_splitter`): `RampTrackerPanel` on top (always built — every ramp
running right now, each with its own Abort), the `AgentPanel` (always built)
underneath — stacked because each agent row is one wide line that a column
would only wrap. The header carries the `TakeoverStrip` (kill switch,
attendance, "agents active", and the **Run owner** of the run in flight).
Measurement VIs are full, role-tagged `InstrumentPanel` cards in
the instrument grid. Child panels keep the established single-receiver `states_updated`
forwarding pattern (`MonitorWindow` receives the tick and forwards to
`TrendsQuadrant` / `RampTrackerPanel`; see the
destruction-order rule above);
signals that fire only at run boundaries
(`run_started`/`run_finished`/`action_succeeded`) are connected directly by
the panel that needs them — there is no teardown race for a signal that only fires while the window
is alive and not mid-tick.

## Entry (what comes in)

- A `Station` instance: the registered VI names and each VI's `vi_type`
  (`system` / `measurement`), plus the object procedures
  are constructed against. No widget holds a VI: the
  instrument panels build from the **Station info** declaration snapshot
  (GLOSSARY.md), read off the `StatusMirror`, and `Station.get_vi()` is never
  called from here.
- A `StatusMirror` (`core/status_mirror.py`) — the proxy's own, reached as
  `proxy.status` and passed down to every panel. It is the GUI's ONLY read surface: `state`,
  `is_monitoring()`, `active_run_kind()`, `held_vi_names()`,
  `manual_override_expires_at()`, `availability_tags()`,
  `vi_faults()`, `offline_reason()`, `lifecycle_state()`,
  `get_operational_status()` and
  `station_info()`/`instrument_info()` all answer from the last event the
  engine broadcast, never by calling into it. Its own signals
  (`status_updated`, `state_changed`, `station_updated`,
  `operational_status_updated`) are what a widget reacts to when a read it
  displays can change with no state transition behind it — the ACKNOWLEDGE
  button's held-VI set, for instance.
- An `OrchestratorProxy` (`core/orchestrator_proxy.py`), built by the
  **Instrument host** and handed in where the `Orchestrator` itself used to
  be. It exposes the engine's signals 1:1 and one typed method per command,
  so every call site below reads the same as it always did; what changed is
  that the object on the other side of it can live on another thread. Signals
  the GUI connects to: `states_updated`,
  `state_changed`, `run_started`, `run_finished`, `error_occurred`,
  `error_event` (the structured `core.events.ErrorEvent` counterpart of the
  per-VI **Instrument fault** model, see GLOSSARY.md — MonitorWindow's banner
  shows/clears a per-VI fault warning from it), `action_blocked`,
  `action_succeeded`, `action_failed`, `monitoring_changed`, and
  `operational_status`. Run-scoped
  signals fire while a run is in flight:
  `procedure_progress`, `procedure_finished`, `measurement_ready` and
  `status_message` (`ProcedureWindow`'s
  progress bar/queue-advance/plots/status log). Actions the GUI
  submits: `submit_vi_action`, `submit_global_action`, `start_monitoring`,
  `stop_monitoring`, `run_procedure`, `queue_procedure`, `run_queue`
  (the queue panel ASKS the engine to start the next run; the engine pulls the
  run itself from the session layer's queue and decides when it starts),
  `pause_procedure`, `resume_procedure`, `abort_procedure`,
  `acknowledge` (unified EMERGENCY/hold-severity override, time-boxed —
  GLOSSARY.md's **Hold acknowledge**),
  `stop_ramp` (stop ONE VI's ramp — the per-instrument counterpart of
  `abort_procedure`; the Orchestrator refuses it for a VI a run claims),
  `acknowledge_fault`, `retry_fault` (the runtime fault registry's GUI
  surface — **Instrument fault** / **Fault acknowledge** in GLOSSARY.md; the
  RUNTIME sibling of `connect_instrument()` for a VI that DID connect but has
  since gone stale/disconnected),
  `ping_instrument` (the connection check the instrument front panel's
  "Check connection" button submits — an identity query is bus traffic, so
  the engine sends it and answers with a verdict),
  `connect_instrument`, `disconnect_instrument` (the **connection lifecycle**
  in GLOSSARY.md: one `ConnectionButton` on every card, next to the
  Initiate/Standby toggle, and a card SWAP when it is pressed — the live
  `InstrumentPanel` becomes an `OfflineInstrumentPanel` and back; Connect is
  refused outside IDLE, while Disconnect follows the **Claim** gate, so a
  card the running run does not claim swaps mid-run and one it claims is
  refused into the banner). Every
  `submit_vi_action` is a **direct action path** call and can come back
  refused for any of five reasons (GLOSSARY.md) — the GUI shows the reason on
  the notification banner and never retries on the operator's behalf.

## Exit (what goes out)

- Long-lived `QMainWindow`s (MonitorWindow owns ProcedureWindow, and each VI's
  `InstrumentFrontPanel`). User actions are submitted to the Orchestrator;
  nothing is written to hardware directly. Session content (sample metadata, data dir, last procedure
  and params, run queue) is persisted to a JSON file — the open L6 session's own
  `gui_state.json` when an experiment is open (also promoting the queue into the
  experiment record via `ExperimentManager.set_queue`), else the per-user AppData
  file; window geometry and splitter state go to `QSettings`.

## Interface contract

- **Never** import from `i2as.drivers.*`, and import from
  `i2as.virtual_instruments.*` only for the `BaseVirtualInstrument` type.
  Never call a VI method directly; route every hardware effect through an
  Orchestrator action.
- **Never** block the Qt event loop (no `time.sleep`, no synchronous I/O). Data
  arrives via Orchestrator signals; do not poll instruments.
- **A widget RENDERS state, it never reconstructs it from action history.**
  Anything an action changes, some other client (or the engine itself) can
  change too, so a widget that remembers "the last action I saw" is showing a
  fiction the moment a stand-down arrives by another route. The
  Initiate/Standby toggle is the worked example (the **lifecycle-state
  standard**, GLOSSARY.md's **Lifecycle state**): it renders
  `StatusMirror.lifecycle_state(vi_name)` on every snapshot, so an emergency's
  blanket `Station.standby_all()` — which dispatches no per-VI action — an
  agent standing a VI down through the gateway, and an operator initiating
  from the CLI all reach the card. `InstrumentPanel._on_action_succeeded()`
  still flips the toggle the moment this card's own action lands, but as an
  OPTIMISTIC answer to the click, which the next snapshot confirms or
  corrects. Tick-rate signals reach a card through its owning WINDOW
  (`MonitorWindow._on_status_snapshot()`, `InstrumentFrontPanel._on_status_
  snapshot()` → `InstrumentPanel.on_status_snapshot()`), never by the panel
  connecting itself — the destruction-order rule below.
- **`core/status_mirror.py`'s `StatusMirror` is the single read surface** — the
  **status-mirror standard** (GLOSSARY.md's **Status mirror**). No widget calls
  a read on the engine; every widget answers `state`, `is_monitoring()`,
  `active_run_kind()`, `availability_tags()`, `vi_faults()`, `held_vi_names()`,
  `station_info()` … from the mirror, which is fed by the event stream and is
  therefore always exactly one event-loop hop behind. Two rules follow. A read
  can never block on an engine that is deep inside one `measure()`. And every
  guard clause a widget writes from a mirror read is **advisory**, never
  authoritative: it may word a button or hide a control, but the engine is the
  only authority on whether an action happens — the client asks, and the engine
  refuses with a verdict. The mirror is built and primed by whoever builds the
  engine and passed in; a widget given none takes the one its client already
  carries (`StatusMirror.of()`), building a fresh one only for a bare engine,
  which is the inline construction path tests use.
- **A click's effect arrives one event-loop hop later.** The engine lives on
  the **Instrument thread** — the default for every setup that does not
  explicitly refuse it (`monitor.yaml`'s `instrument_thread: false`, or
  `I2AS_INSTRUMENT_THREAD=0` for one launch, both of which select the
  temporary **Inline mode**): a command is POSTED to it and the event that
  proves it happened comes back queued. Nothing about a widget's wiring
  changes — the same proxy, the same signals — but two habits stop being safe.
  A handler must never assume the mirror already reflects the command it just
  sent (read the confirmed state from the signal that carries it, as the
  monitoring toggle does with `monitoring_changed`), and a widget that rebuilds
  itself from a broadcast must survive that rebuild landing AFTER the handler
  that caused it returned — which is why `queue_panel.py` remembers the entry a
  reorder selected and re-applies it when the list is rebuilt, instead of
  selecting once and trusting the order. `tests/test_gui.py` runs in both
  modes (`tests/instrument_modes.py`: threaded by default,
  `make test-instrument-inline` for the other leg), which is what keeps that
  true. Write every handler for the threaded default; inline only forgives.
- **`ramp_tracker_panel.py`'s `RampRow` is the single per-ramp row
  standard.** It contains no per-instrument logic:
  every visible detail renders from a `core.ramps.RampRecord`, and the Abort
  button's enabled state comes from that record's `stoppable` flag — which
  the Orchestrator computed with the very predicate `stop_ramp()` uses, so
  the button can never look enabled for a stop the action would refuse. A
  new rampable VI appears here for free the moment it implements the
  ramp-introspection standard (`virtual_instruments/README.md`); this file
  never changes.
- **`widget_lifecycle.py` owns both widget-lifetime standards.** Widgets are
  destroyed by two different mechanisms — Qt's deferred delete and PyQt's
  "the C++ object dies with its last Python reference" — and mixing them up
  segfaults the process, so both rules live in one module.
  - The **window-liveness standard**: every top-level window calls
    `hold_window(self)` at the end of its `__init__` and `release_window(self)`
    from its `closeEvent` (once the close is accepted). A shown window whose
    creator kept no reference is otherwise destroyed by whichever generational
    garbage-collection pass reaches it first — including one triggered by an
    allocation inside that same window's `paintEvent`, which destroys the paint
    device mid-paint ("Cannot destroy paint device that is being painted") and
    leaves Qt painting a freed pyqtgraph `AxisItem`. A window that holds itself
    can only be destroyed where the code says so.
  - The **card-retirement standard** (GLOSSARY.md's **Card retirement**): a
    widget swapped out of a live layout leaves through
    `retire_widget(widget, layout)`, which hides it, takes it out of the
    layout, closes any pyqtgraph plot it owns, and only then calls
    `deleteLater()`. Never call `deleteLater()` on a card directly: a widget
    merely dropped from a layout stays a visible child of its parent and paints
    over its replacement until the deferred delete lands — which, in a test run
    with no event loop, is never. The retired widget keeps its Qt parent on
    purpose; retirement runs inside a signal emitted by one of the widget's own
    children (the Disconnect button on the card being swapped away), so its
    destruction has to stay deferred. Used by both instrument-card swaps in
    `monitor_window.py`.
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

1. Create a file in this folder. Import only from `PyQt6`, `i2as.core.*`
   value objects and the Orchestrator/Station, `i2as.session.*` (L6),
   `i2as.procedures.*` (never their VI/driver dependencies), and other `i2as.gui.*` widgets. Do not import drivers or
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
| `app_settings.py` | `QSettings` factory (a test seam) plus machine-level identity persisted through it: the per-user autosave-file path resolver, shipped/user config dirs, the active-config identity `(name, source)` (survives running from another clone/worktree), and who is currently logged in. The fixed measurement root L6 session/experiment folders live under is resolved by `i2as.core.paths.measurement_root()` instead — a machine-level, admin-set value, not GUI-editable through this module. | `get_settings`, `autosave_file_path`, `shipped_config_dir`, `user_config_dir`, `config_active`, `set_config_active`, `current_user_id`, `set_current_user_id` | `tests/test_gui.py` |
| `form_autosave.py` | Qt-free form-autosave model (sample metadata, data dir, last procedure + params, run queue), serialised to one JSON file; never raises on a corrupt file. Historically `session.py` — renamed so "session" is free for the L6 Session Management layer; its class was renamed too (`FormAutosaveState`, formerly `SessionState`), while the on-disk JSON filename is unchanged. | `FormAutosaveState`, `load`, `save` | `tests/test_form_autosave.py` |
| `theme.py` | Light "lab" colour palette constants and the application-wide QSS string. Includes `BTN_DANGER_DISABLED` and its `QPushButton[class="danger"]:disabled` rule — deliberately low contrast (WCAG 1.4.3 exempts disabled controls), so a destructive action the app is currently refusing does not read as available. | `build_stylesheet`, colour/class constants | `tests/test_gui.py` |
| `param_form.py` | The single `ParamSpec`-to-Qt-widget mapping, shared by every parameter form (the procedure form, the instrument cards, the envelope editor); builds labelled/tooltipped `QFormLayout` rows and the inverse read helpers. A `widget_hint="datetime"` (`str`-typed) field still gets a `QLineEdit` (an ISO 8601 string) with a placeholder showing the expected format — no dedicated date-picker widget yet. | `build_param_widget`, `build_form_layout`, `build_group_box`, `build_param_tooltip`, `collect_value`, `get_widget_raw`, `set_widget_raw` | `tests/test_gui.py` |
| `sweep_axis_widget.py` | Sweep-shape editor for a Procedure's declared `SweepAxis`: mode selector (Linear / Segments / CSV) over a stacked sub-form, a 2-column segment breakpoint table (`field_segments`), and a hysteresis checkbox. The only GUI code sweep-shape support needs. | `SweepAxisWidget`, `get_params` | `tests/test_sweep_axis_widget.py` |
| `instrument_panel.py` | Auto-generated per-VI `QGroupBox`, built entirely from the station's **declaration snapshot** (`InstrumentInfo`, read off the `StatusMirror`) — it holds no VI object: declared `@monitored` readings become live `QLabel`s, declared `@control` actions become button + input rows (a parameter marked `declared` has its `ParamSpec` rebuilt from the declaration's JSON and rendered via `param_form` as a combo/checkbox/tooltipped field; one known only from its signature keeps a plain line edit). Card visibility: a `panels:` config allowlist wins, else each control's `panel=` default. Header holds a `LifecycleToggleButton` and the front-panel icon, and that toggle RENDERS `StatusMirror.lifecycle_state(vi_name)` on every snapshot the owning window forwards to `on_status_snapshot()` (the **lifecycle-state standard**, GLOSSARY.md's **Lifecycle state**) — `_on_action_succeeded()` keeps an optimistic flip for this card's own click, which the next snapshot confirms or corrects. Updates on each `states_updated` tick; flips a QSS `status` property on ok/stale/disconnected change, and shows/hides a fault row (message + Acknowledge + Retry, disabling every `@control` row) from the Availability standard's unified record (`StatusMirror.availability_tags(vi_name)`'s `not_responding` tag, GLOSSARY.md's **Availability**) — the row's message still reads the mirror's `vi_fault()` for `kind`/`message`/`acknowledged`, fields the unified record does not carry, since acknowledge/retry are comm-specific actions. The RUNTIME sibling of `offline_panel.py`'s build-time fault card. | `InstrumentPanel` | `tests/test_gui.py` |
| `instrument_front_panel.py` | Per-VI child window showing the FULL capability surface (every declared `@monitored` value + every declared `@control`, panel-hidden ones included) by embedding an all-controls `InstrumentPanel` in a scroll area; the allowlist comes from the declaration, so this window holds no VI object either. Its "Check connection" button submits `Orchestrator.ping_instrument()` and reports the answer inline from `action_succeeded`/`action_failed` — the identity query is bus traffic and belongs to the engine, never to the GUI thread. It is also the receiver for its embedded card's tick-rate mirror signal (`status_updated` → `InstrumentPanel.on_status_snapshot()`), so the lifecycle toggle here can never disagree with the one on the monitor card. Opened from the sliders icon on cards and switch rows; lazily created, reused. | `InstrumentFrontPanel` | `tests/test_gui.py` |
| `offline_panel.py` | Grid card + detail window for a VI I2AS is not holding — either it failed to connect at startup (degraded build) or the operator released it (**connection lifecycle**); both land in the Station's one offline registry, and `OfflineInstrument.tags` (the Availability standard's absence tags, `{"connect_failed"}`/`{"operator"}`/both — GLOSSARY.md's **Availability tag**) selects the wording ([OFFLINE] vs [DISCONNECTED], or both facts stated together for the two-tag case) via a frozenset-keyed wording table, and nothing else. The card carries the reason and the Connect button; the detail window repeats it with the full reason and a hint; both re-derive their wording (via `StatusMirror.availability_tags(vi_name)`) on a failed reconnect, since that can change an already-offline VI's tags with no card swap to hang a refresh on. Both go through `Orchestrator.connect_instrument()`. MonitorWindow swaps the card for a live `InstrumentPanel` on success. Distinct from `instrument_panel.py`'s runtime fault row (a VI that DID connect but has since stopped answering — badged [NOT RESPONDING] so it never claims the operator's word "disconnected") — same idiom, no shared code. | `OfflineInstrumentPanel`, `OfflineFrontPanel` | `tests/test_gui.py` |
| `lifecycle_toggle.py` | The two per-instrument lifecycle controls every card carries side by side — the two axes of the **connection lifecycle** (GLOSSARY.md). `LifecycleToggleButton`: one state-dependent Initiate/Standby button with a status glow dot (what the instrument is *doing*); a dumb renderer — `set_initiated()` is the only way its state changes, and `InstrumentPanel` drives it from the lifecycle state on each `StatusSnapshot` (plus an optimistic flip on this card's own `action_succeeded`), never from the click. `ConnectionButton`: a one-way Connect or Disconnect button (who *owns* the instrument) — one-way rather than a toggle because the card itself is swapped when the state flips; `compact=True` renders it icon-only for the narrow monitor card, where a labelled button would clip the VI name and the Initiate toggle. | `LifecycleToggleButton`, `ConnectionButton`, `set_initiated`, `is_initiated` | `tests/test_lifecycle_toggle.py` |
| `agent_panel.py` | The **Agent panel** (`agent_panel`), the bottom-right quadrant's full-width bottom sub-panel, always built. A FILTER of the engine's event stream, not a second stream: every `Verdict` and every `StateChange` whose `Actor.kind` is not `operator` becomes one row (`AgentAction` — ts, actor id/role/kind, what, verdict code, reason, kind), newest at the bottom, capped at `MAX_ROWS`, auto-scrolled only while the reader is already at the tail, with an "Include system" filter and an empty state. A refusal is visually distinct through a dynamic `outcome` property (theme tokens only; the row's own TEXT still names the code and the rule, so colour is never the only signal), and so is a **Takeover** — an action the engine ACCEPTED on another actor's run, read off the verdict's `detail.takeover` and rendered as "took over &lt;owner&gt;'s run: &lt;reason&gt;" in the warning triple, bold. A run-owner line beside the filter says whose run is in flight (`set_run_owner()`, forwarded from the window off the **Status mirror** like every other per-tick payload). Three sources, one row model: the live stream (forwarded in by `MonitorWindow` — the panel connects to no engine signal itself, the destruction-order rule), the open experiment's **Agent feed** re-read on open/switch (so the trail survives a restart), and each pending **Draft entry**, rendered as the one row that asks a question — an Approve button calling `ExperimentManager.approve_eln_draft(run_id)`. Owns the "agents active" ledger the takeover strip renders (`agents_active_changed`, `active_agent_count()`), because every agent action already arrives here. | `AgentPanel`, `AgentAction`, `on_verdict`, `on_event`, `seed_from_feed`, `reload_experiment`, `active_agent_count`, `set_run_owner` | `tests/test_agent_panel.py` |
| `takeover_strip.py` | The **Takeover strip** (`takeover_strip`), in `MonitorWindow`'s header. The **Kill switch** as three radios (`active` / `read-only` / `revoked`) applied through the client's `set_agent_gate()` — the engine is the enforcement point — and REFLECTED from the `StatusMirror` on every snapshot, so a gate an agent or `i2as.ctl` changed shows here. The **Attendance** toggle, one fact with two homes: with an experiment open it goes through `ExperimentManager.set_attended()` (which persists it on the record AND pushes it down into the engine, so writing it twice would submit the same command twice), and with none open straight into the engine. And two reads off the same mirror: "agents active: N" from the `AgentPanel`'s ledger, and "run owned by &lt;id&gt;" — the **Run owner** of the run in flight, because whose run it is decides who may end it (GLOSSARY.md's **Run owner**), and that belongs beside the controls that decide how far agents may go at all. The owner line is the one widget here that yields width when the header is narrow: the controls are the human's way of taking the machine back, and its own text is repeated in its tooltip. Nothing here is ever disabled by the gate — a kill switch that could lock the human out would be a hazard, not a safeguard. | `TakeoverStrip`, `sync_from_mirror`, `set_agents_active`, `GATE_CHOICES` | `tests/test_agent_panel.py` |
| `notification_banner.py` | Hidden-by-default inline strip for non-modal `warning`/`error` messages; a repeated identical message bumps a counter instead of stacking. Replaced the old modal `QMessageBox` storms. | `NotificationBanner`, `show_message` | `tests/test_gui.py` |
| `live_plot_panel.py` | Reusable live X/Y plot panel (X + Y selectors, optional per-slot Loop 1 / Loop 2 selectors for looped measurements, themed `pyqtgraph` curve); axis keys stay plain and the panel indexes directly into each measurement column's `(n_loop1, n_loop2)` grid at draw time (selector item data is the 0-based axis index). Every plottable value is already a scalar — computing it is the measurement method's job, never the panel's. ProcedureWindow hosts two, driven by `measurement_ready`. | `LivePlotPanel`, `set_available_keys`, `set_available_loop_labels`, `redraw`, `clear` | `tests/test_gui.py` (via ProcedureWindow `_plot1`/`_plot2`) |
| `monitor_history.py` | Qt-free ring-buffer of time-series readings, one bounded deque per flat key. Two entry points: `record()` (live path, flattens nested state dicts like `Station.last_state_flat()`, includes measurement VIs) and `record_flat()` (replay path for disk-persisted trend history, takes an already-flat dict, excludes measurement VIs — the accepted asymmetry, see class docstring). Feeds the trend plots. | `MonitorHistory`, `record`, `record_flat`, `series`, `keys` | `tests/test_monitor_history.py` |
| `trend_plot_panel.py` | Reusable trend plot: one variable vs wall-clock time (`DateAxisItem`), reading from a shared `MonitorHistory`; Y-variable + time-window selectors and a remove button. `TIME_WINDOWS` runs 15 min through 1 y; windows up to 24 h read `MonitorHistory` in RAM as before, "7 d"/"1 y" read the disk-backed tiered trend-history store via `i2as.core.trend_history.read_window()` (tier choice stays in that module, not here — see GLOSSARY.md's **Trend tier**), synchronously (no thread — single-threaded cooperative scheduling). A disk read failure/emptiness renders an empty curve, never raises — including the deliberate measurement-VI asymmetry, where a measurement VI's key is present in the live in-RAM history but never in the disk tiers, so a disk-backed window on it comes back empty. | `TrendPlotPanel`, `refresh`, `remove_requested` signal, `TIME_WINDOWS` | `tests/test_trend_plot_panel.py`, `tests/test_gui.py` |
| `window_geometry.py` | Shared window-geometry persistence: restore a saved geometry (rejecting one that landed off-screen), fall back to a centered screen-fraction default, save on close. Used by both windows. | `restore_or_center`, `save_geometry`, `geometry_on_screen` | `tests/test_gui.py` |
| `widget_lifecycle.py` | The two widget-lifetime standards (see "Interface contract" above): `hold_window`/`release_window` keep a shown top-level window out of the garbage collector's reach between its `__init__` and its `closeEvent`, and `retire_widget` takes a swapped-out card out of a live layout in the order Qt needs — hide, remove from the layout, close any pyqtgraph plot, then `deleteLater()`. Qt-only: it takes widgets, never I2AS objects. | `hold_window`, `release_window`, `held_windows`, `retire_widget` | `tests/test_gui.py` |
| `log_panel.py` | The read-only real-time log view (`log_panel`) plus `QtLogHandler`, the coloured-HTML logging handler it owns; `attach()`/`detach()` manage the handler's lifetime on the shared "i2as" logger. | `LogPanel`, `QtLogHandler` | `tests/test_gui.py` |
| `experiment_info_panel.py` | The GUI surface for the Experiment tier: an experiment status/Start-Close control (ExperimentManager, optional), the **Session envelope** editor for the OPEN experiment (the same `EnvelopeEditorWidget` class the Start Experiment dialog uses, so the two can never diverge; hidden while no experiment is open, pre-filled from `envelope_variables()` and switched to the envelope already in force via `set_bounds()`, applied through `ExperimentManager.set_experiment_envelope()` — record and engine, one writer — with an `envelope_verdict_label` badge showing the editor's own refusal live and the engine's verdict for its own Apply, matched by `request_id` and forwarded in by `MonitorWindow`), sample name/ID/comments and the derived-but-editable data-directory field with Browse (forced to the open experiment's own `data/` folder on open/switch, restored to its pre-experiment text on close; a plain `data_dir_note` label is live typing feedback whenever the field points outside the experiment folder), and an eLab status line (reflects `ElnLink`; publish controls land with Track B). Containment is hard: `_on_browse_dir()` refuses a selection outside the open experiment's folder outright, and `is_data_dir_contained()` is the read `MonitorWindow.get_data_dir_for_run()` enforces before a run is allowed to start. Setup-tier concerns (config identity, instrument metadata, user login) live in the menu bar instead — see `monitor_window.py`/`setup_dialogs.py`. Sample fields stay free-editable per run regardless of experiment state; whatever they hold at "Start Experiment" time is snapshotted onto the `ExperimentRecord`. | `ExperimentInfoPanel`, `get_sample_info`, `get_data_dir`, `is_data_dir_contained`, `apply_session` | `tests/test_gui.py` |
| `experiment_dialogs.py` | Modal dialogs for the experiment lifecycle, plus `EnvelopeEditorWidget`, which is hosted BOTH here and in the experiment header (`experiment_info_panel.py`): `StartExperimentDialog` (title, an optional Folder name override for the experiment's directory — auto-filled from the title until hand-edited, same pattern as `AddUserDialog`'s id field — user picker with inline "New user…", attendance checkbox, and the `EnvelopeEditorWidget`) and `CloseExperimentDialog` (findings text), plus the shared `UserPickerWidget` (roster combo + inline "New user…" → `AddUserDialog`) reused by `setup_dialogs.LoginDialog`. The envelope editor renders one min/max row per enveloped quantity (`ExperimentManager.envelope_variables()`), PRE-FILLED with the setup's own `control_limits` bounds so the operator narrows rather than composes; it reads the **dict form** and only the dict form — the JSON-safe rendering a `StatusSnapshot` carries, parsed into the private `_EnvelopeVariable` (the unit label is re-derived from the setpoint parameter's name, since a property does not survive that rendering) — because the GUI builds from declarations, never from engine objects like `core.plan.EnvelopeVariable`; `set_bounds()` replaces the pre-filled defaults with an envelope already in force (what the experiment header shows for an open experiment); it refuses a bound that would widen a setup limit or is not a number, showing the reason on `envelope_error_label` (the validated `verdict_badge` QSS class) and keeping OK disabled. Opened only by `ExperimentInfoPanel`; every `ExperimentManager` mutation happens in the panel after a dialog accepts. | `StartExperimentDialog` (`result_values()`, `envelope()`), `EnvelopeEditorWidget`, `CloseExperimentDialog`, `AddUserDialog`, `UserPickerWidget` | `tests/test_gui.py` |
| `setup_dialogs.py` | Modal dialogs for the Setup tier: `LoginDialog` (pick/create who's using the app, via the shared `UserPickerWidget`) and `InstrumentInfoDialog` (read-only view of each VI's `devices.yaml` `metadata:` block). Both opened from MonitorWindow's User menu. | `LoginDialog`, `InstrumentInfoDialog` | `tests/test_gui.py` |
| `open_experiment_dialog.py` | `OpenExperimentDialog`: lists every experiment from `session_manager.store.list_experiments()` (title/user/status/created date resolved via `store.load()`); open ones selectable, closed ones grayed out with a "(closed)" suffix and disabled via item flags (never a stylesheet). Mirrors `UserPickerWidget`'s list-plus-accept pattern. Opened from MonitorWindow's User menu ("Load Session…"), which drives the actual switch via `_switch_experiment`. | `OpenExperimentDialog`, `selected_experiment_id` | `tests/test_gui.py` |
| `session_dialogs.py` | `ResumeSessionDialog`: lists every Session owned by a required `user_id` (the L6 tier above `session_manager` — a named, resumable, per-user folder holding multiple experiments, nested at `sessions/<user_id>/`) from `SessionStore.list_sessions(user_id)`, plus an inline "New session…" name field and Create button (mirrors `AddUserDialog`'s simplest create-new-named-thing pattern). Opened from MonitorWindow's User menu ("Resume Session…") — nobody-logged-in resolves to the Guest user first — which persists the pick via `SessionStore.set_active(user_id, session_id)` — takes effect on next launch, `session_manager`'s own `ExperimentStore` is never rebound live. | `ResumeSessionDialog`, `selected_session_id` | `tests/test_gui.py` |
| `ramp_tracker_panel.py` | Bottom-right quadrant's TOP sub-panel (page 1), always built. One generic `RampRow` per ramp running right now — group-box title `"<setpoint label> · <vi_name>"`, a `value → next setpoint` readout, an `End setpoint … · rate · phase` detail line, an `Owned by <run>` line for a run-driven ramp, and an Abort button that confirms via `QMessageBox` then calls `Orchestrator.stop_ramp(vi_name)`. Fed by `on_ramps_updated()` (forwarded from `ramps_updated` via `MonitorWindow`, which fires every tick); rows are added/updated in place/removed rather than rebuilt, so objectNames stay stable and a button is never destroyed mid-click. Abort is disabled with the refusal as its tooltip whenever the record is not `stoppable` — the case where a running procedure claims the VI, for which the correct stop is aborting the run. Zero per-instrument code. | `RampTrackerPanel`, `RampRow`, `on_ramps_updated`, `row_names` | `tests/test_ramp_tracker_panel.py` |
| `trends_quadrant.py` | The Trends quadrant: 1-4 `TrendPlotPanel`s auto-arranged into a `ceil(sqrt(N))` grid, backed by the `MonitorHistory` it owns; Add button (cap 4), per-panel remove (floor 1), an opportunistic temperature default key (a hint that degrades to the first key the setup has), and QSettings persistence of the panel list. On construction, rehydrates `MonitorHistory` by replaying the raw trend-history tier (`log_dir`, defaulting to `logging_config.log_directory()`) through `record_flat()`, non-fatally — a missing/corrupt store degrades to an empty history rather than blocking GUI startup. | `TrendsQuadrant`, `on_states_updated`, `save_settings`, `restore_settings` | `tests/test_gui.py`, `tests/test_trends_quadrant.py` |
| `procedure_discovery.py` | Qt-free procedure auto-discovery: imports every `i2as.procedures` module and returns the named `BaseProcedure` subclasses at any depth. | `discover_procedures`, `all_subclasses` | `tests/test_gui.py` (via ProcedureWindow) |
| `procedure_params_panel.py` | The parameter quadrant of ProcedureWindow: procedure selector row (Add to Queue / Run Now), filename-prefix field, and the auto-generated form — Sweep column with `SweepAxisWidget`, composite Measurement column (method drop-down + selected VI's sub-form), Reading loop column (two generic slots: a loopable-parameter drop-down each, with per-choice pick checkboxes or a value-list text field); structural params trigger a keyed diff re-render. Owns the per-procedure raw-text param cache behind session persistence. Signals `structure_changed`/`routes_changed` let the window sync its plot selectors. | `ProcedureParamsPanel`, `collect_values`, `current_selections`, `export_session_state`, `restore_session` | `tests/test_gui.py` |
| `queue_panel.py` | The run-queue group box: list + reorder/remove/**Probe first**/Run Queue buttons, per-item lifecycle status (pending/running/done/failed), and session restore/export. A VIEW, not an owner — the waiting runs are immutable **run specs** in the session layer's `RunQueueHost` (GLOSSARY.md's **Run queue**), the panel renders `snapshot()` and re-renders on every `QueueChanged` taken off its client's event stream (`event_emitted` on a bare engine, `event` on the proxy, which renames the two contract channels because a client consumes them), and every mutation goes through the host (which validates an add before it can enter the queue and refuses it with its findings in a modal). Nothing here holds a built procedure or touches the engine's private lists. Without an `ExperimentManager` (a bare unit test) it builds a standalone host so the window still works, adopting the engine's pull seam only if nobody has claimed it. A reorder keeps its selection through a rebuild that lands later than the call that caused it (`_keep_selected`), which is what a `QueueChanged` crossing the **Instrument thread** does. **Probe first** (`queue_probe_btn`) is the per-row action: it queues a **probe run** of the selected waiting procedure — the same class and params, plus the default `ProbeSpec` — through the same `RunQueueHost.add()` every other run takes (so the setup limits and the session envelope judge it identically), then moves it to sit immediately before the run it probes; a refusal is a modal like every other refusal answering a direct click, while an accepted probe's findings and **duration estimate** are shown inline (`queue_probe_label`) and hung on the row as a tooltip, and its row is labelled `(probe)`. A class this window never discovered is refused by name rather than probed. | `QueuePanel`, `QueueEntry` (`spec` + `status`), `add_run`, `notify_finished`, `notify_aborted`, `restore_items`, `export_items`, `reset` | `tests/test_gui.py` |
| `eln_settings_dialog.py` | The **eLab setup dialog** (`eln_settings_dialog`): one modal form over the user-level `ElnSettings` — enabled, backend (from `discover_backends()`), base URL, API key, team id, template (combo filled by "Fetch templates"), verify TLS, auto-publish, tags, attachment cap, and an Analysis group (on/off, timeout, fact tables, data file). The key field is a password field that is never pre-filled and never rendered into a label, tooltip or log line; blank means "keep the stored key". `settings_from_form()` builds its answer with `dataclasses.replace` on the record it was given, so every field it does not show (the drafting assistant, the price table, the retry timings) survives a save; a record with no `analysis` block is shown with defaults and left alone. "Test connection" builds the adapter from the form and shows `verify()`'s identity or the `ElnError`. `persist_eln_settings()` is the shared save half (write the file, reload the publisher) both openers use. | `ElnSettingsDialog`, `persist_eln_settings` | `tests/test_eln_settings_dialog.py` |
| `analysis_panel.py` | The **eLab tab** (`analysis_panel`), the procedure window's top-right "eLab" tab. The human half of the analysis track: the publish-state chip (`publish_state_changed` → the `state` dynamic property), an "Analysis on" toggle bound to `settings.analysis.enabled`, "eLab setup…", the run under review (the open experiment's finished runs, newest first), the recipe that serves that run's procedure (package + experiment recipes, the experiment ones suffixed `(experiment)`, the one `recipe_for` would pick preselected), "New recipe…" (scaffolds into the experiment's `analysis/recipes/` and opens the file), "Run analysis", a `QTextBrowser` preview showing the **pending entry**'s title and body with the report's figures rendered ABOVE it from their local files — preview only, the published body never embeds an image — the recipe's warnings/error, and Publish / Discard, which go through `ExperimentManager.approve_eln_draft()` / `discard_pending_eln_draft()`. Every collaborator is optional and `i2as.analysis.discovery` is imported lazily, so a build without either degrades to a status line rather than a window that will not open. | `AnalysisPanel`, `reload`, `set_run`, `on_run_finished` | `tests/test_analysis_panel.py` |
| `monitor_window.py` | Main live-monitor window — a composition shell. A header `QTabBar` (`page_tab_bar`) switches a central `QStackedWidget` (`page_stack`) between Page 1 (Monitor: the fixed 2x2 quadrant grid of nested `QSplitter`s, draggable/not closable — top-left a 2-column `InstrumentPanel` list for system VIs, top-right a `TrendsQuadrant`, bottom-left an `ExperimentInfoPanel`, bottom-right a vertical splitter of a `RampTrackerPanel` over an `AgentPanel`) and Page 2 (Logs: the `LogPanel` and nothing else, see "Pages" above). Hosts the Start/Stop Monitoring toggle (mirrors `monitoring_changed`; monitoring is off at launch until instruments are initiated), Initiate/Standby All, the state-driven status bar (which reads `MEASURING · Pausing` while a pause has been requested but not yet honoured — GLOSSARY.md's **Pause boundary** — since that request changes no state and reaches the window only as a fresh `StatusSnapshot`), the notification banner (also used by `ExperimentManager.store_health_changed` — a save failure/recovery — and, per-VI, `Orchestrator.error_event`, the **Instrument fault** surface), the single-home ACKNOWLEDGE button (moved off `procedure_window.py`, visible in EMERGENCY or whenever a hold-severity condition has a VI held, synced at construction so a pre-existing condition is not missed) and its top-bar "Acknowledged (mm:ss)" countdown while an override is active, session/splitter persistence, and the menu bar — including the Setup tier's surfaces: the User menu (`Log in as…` switches which per-user form-autosave file is loaded/saved, via `setup_dialogs.LoginDialog`; `Load Session…` opens `open_experiment_dialog.OpenExperimentDialog` and switches the open L6 experiment via `_switch_experiment`; `Resume Session…` opens `session_dialogs.ResumeSessionDialog` and persists the pick as the active L6 Session via `SessionStore.set_active()`, taking effect on next launch; a header label reflects who's logged in) the User menu's `eLab notebook…` action, which opens `eln_settings_dialog.ElnSettingsDialog` over the publisher's settings — a notebook account is a property of the person, exactly like the login beside it — and its `Instrument Info…` action (`setup_dialogs.InstrumentInfoDialog`, reading `core.station.read_instrument_metadata()`), which describes the rack the person in front of the app is using and writes nothing. The menu bar is exactly those two menus: User and Procedures. `eln_publisher`/`analysis_runner` are held only to hand on to the procedure window's eLab tab. Also connects `ExperimentManager.experiment_changed` itself, loading a newly opened/switched session's own `gui_state.json` over the in-memory `FormAutosaveState` (skipped for a brand-new experiment that has none yet, so Start Experiment never wipes just-typed fields). The window is deliberately the `states_updated` (and, likewise every tick, `ramps_updated`) receiver, forwarding each to the panels — Qt severs a receiver's connections at the start of its destruction, so no tick can reach a partially destroyed child tree. | `MonitorWindow` | `tests/test_gui.py` |
| `procedure_window.py` | Procedure builder, run queue, and live-data window — a composition shell over `ProcedureParamsPanel`, `QueuePanel`, and two `LivePlotPanel`s. Same 2x2 splitter grid (params / top-right tabs / Plot 1 / Plot 2); the top-right quadrant (`queue_quadrant`) is a two-tab `QTabWidget` (`right_tabs`): **Queue** holds the queue-over-status splitter unchanged, **eLab** holds the `AnalysisPanel`. The window owns the eLab setup dialog (`open_eln_settings`, shared with the Monitor window's User-menu action) and forwards `run_finished` to the panel through a window slot (destruction-order rule). Its three eLab collaborators — `session_manager`, `eln_publisher`, `analysis_runner` — are optional, so a window built without a session layer still shows the tab, saying in one line that nothing is wired. Run Now builds a procedure; Add to Queue builds nothing at all and queues the values as a **run spec** instead; progress bar from `procedure_progress`. The Pause button captions itself `Pausing…` from `StatusMirror.pause_pending()` while a pause is deferred to the **Pause boundary** (GLOSSARY.md), so a click taken during a datapoint is acknowledged on the button rather than reading as ignored until the state moves. No longer connects `state_changed` at all — the emergency-acknowledge button moved to `monitor_window.py` (single home) and nothing else here needed it. | `ProcedureWindow` | `tests/test_gui.py` |
