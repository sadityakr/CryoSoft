# ---
# description: |
#   MonitorWindow: the main CryoSoft window showing live instrument state.
#   Content below the header/banner is a fixed 2x2 quadrant grid, built from
#   nested QSplitters (one horizontal, two vertical): top-left is a scrollable
#   2-column instrument monitor/control list (every VI: system/level plus
#   measurement and switch cards, tagged by role), top-right is a
#   TrendsQuadrant (cryosoft.gui.trends_quadrant), bottom-left is a
#   SessionInfoPanel (cryosoft.gui.session_info_panel), and bottom-right is
#   the optional OperationsPanel. Splitter boundaries are draggable but nothing can
#   be closed, detached, or floated. This module is the composition shell:
#   quadrant assembly, menus, status bar, Orchestrator signal wiring, and
#   session/geometry persistence — the quadrant content lives in the
#   component modules above. Also owns the Setup tier's menu surfaces: the
#   User menu (Log in as… — switches which per-user form-autosave file is
#   loaded/saved; Load Session… — cryosoft.gui.session_dialogs.LoadSessionDialog,
#   switches the open L6 experiment via _switch_session; Sessions Folder… —
#   browses app_settings.sessions_root()/set_sessions_root()) and the Config
#   menu's Instrument Info… action (read-only devices.yaml metadata via
#   cryosoft.gui.setup_dialogs). Also connects SessionManager.experiment_changed
#   (loads a newly opened/switched session's own gui_state.json over the
#   in-memory SessionState, skipping a brand-new experiment that has none
#   yet) and store_health_changed (a save failure/recovery banner + status
#   note) — see docs/plans/unified-session-record.md §7.
# entry_point: Not run directly. Instantiated in main.py.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (Station, read_instrument_metadata)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.instrument_panel (InstrumentPanel)
#   - cryosoft.gui.trends_quadrant (TrendsQuadrant)
#   - cryosoft.gui.session_info_panel (SessionInfoPanel)
#   - cryosoft.gui.session_dialogs (LoadSessionDialog)
#   - cryosoft.gui.log_panel (LogPanel)
#   - cryosoft.gui.config_menu (ConfigMenuController)
#   - cryosoft.gui.setup_dialogs (LoginDialog, InstrumentInfoDialog)
#   - cryosoft.gui.window_geometry (geometry persistence helpers)
#   - cryosoft.session.manager (SessionManager, optional — forwarded to
#     SessionInfoPanel, which owns the experiment lifecycle controls; also
#     read here for the User menu's roster, session switching, and the
#     save-health banner)
# input: |
#   Station instance and Orchestrator instance.
# process: |
#   Renders every VI as an InstrumentPanel card in the 2-column instrument
#   grid (top-left): system/level first, then measurement and switch cards
#   tagged by role (the switch card carries the station-wide Enable Scanner
#   checkbox). Connects Orchestrator signals for the state-driven status
#   bar, the notification banner (errors/blocked actions), the
#   TrendsQuadrant's history recording, and the optional Operations panel.
#   Owns ProcedureWindow and opens it lazily via the Procedures menu.
# output: |
#   A QMainWindow that stays open for the lifetime of the application.
# ---

"""MonitorWindow — main CryoSoft monitor window (composition shell)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTabBar,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import Station, read_instrument_metadata
from cryosoft.gui import app_settings  # import the module (not the function) so tests can monkeypatch the factory
from cryosoft.gui import form_autosave as session_store  # module import keeps save/load monkeypatchable
from cryosoft.gui import window_geometry
from cryosoft.gui.config_menu import ConfigMenuController
from cryosoft.gui.diagnostics_window import DiagnosticsWindow
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.log_panel import LogPanel
from cryosoft.gui.notification_banner import NotificationBanner
from cryosoft.gui.operations_panel import OperationsPanel
from cryosoft.gui.servicing_log_page import ServicingLogPage
from cryosoft.gui.session_dialogs import LoadSessionDialog
from cryosoft.gui.session_info_panel import SessionInfoPanel
from cryosoft.gui.setup_dialogs import InstrumentInfoDialog, LoginDialog
from cryosoft.gui.theme import (
    BANNER_SEVERITY_ERROR,
    BANNER_SEVERITY_WARNING,
    BTN_CLASS_PRIMARY,
    BTN_CLASS_SECONDARY,
    TEXT_ON_ACCENT,
    TEXT_PRIMARY,
)
from cryosoft.gui.trends_quadrant import TrendsQuadrant
from cryosoft.session.manager import SessionManager

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryosoft.core.config_catalog import ConfigCatalog
    from cryosoft.session.servicing_log import (
        CryogenicsRecorder,
        HeliumRecordStore,
        ServicingLogStore,
    )

logger = logging.getLogger(__name__)

# QSettings keys for persisted window/layout state.
_GEOMETRY_KEY = "MonitorWindow/geometry"
# Namespaced distinctly from any earlier layout scheme's settings (the
# pre-dock splitter-grid MonitorWindow used a plain "MonitorWindow/main_splitter"
# key; QSplitter.restoreState() restores orientation/child-count from the
# blob too, so applying its leftover registry value here would silently
# reshape this splitter to match a layout that no longer exists).
_MAIN_SPLITTER_KEY = "MonitorWindow/quadrant_main_splitter"
_LEFT_SPLITTER_KEY = "MonitorWindow/quadrant_left_splitter"
_RIGHT_SPLITTER_KEY = "MonitorWindow/quadrant_right_splitter"

# Orchestrator state names that colour the status bar (dynamic 'level' property).
_ACTIVE_STATES = frozenset({
    OrchestratorState.INITIATING.value,
    OrchestratorState.RAMPING.value,
    OrchestratorState.INITIATION_GATE.value,
    OrchestratorState.READING_GATE.value,
    OrchestratorState.MEASURING.value,
    OrchestratorState.SWEEPING.value,
    OrchestratorState.PAUSED.value,
})
_ERROR_STATES = frozenset({
    OrchestratorState.ERROR.value,
    OrchestratorState.EMERGENCY.value,
})


class MonitorWindow(QMainWindow):
    """Main window: live instrument monitor, sample info, global controls, and log.

    A slim page tab bar in the header switches between two pages held in a
    central QStackedWidget. Page 1 (Monitor) is the fixed 2x2 quadrant grid
    built from nested QSplitters: top-left is a scrollable 2-column grid of
    :class:`InstrumentPanel` cards for EVERY VI — system/level first, then
    measurement and switch cards tagged by role (the switch card carries the
    station-wide Enable Scanner checkbox) — top-right is the
    :class:`TrendsQuadrant`, bottom-left is the :class:`SessionInfoPanel`,
    and bottom-right is the optional :class:`OperationsPanel`. Every splitter
    boundary is draggable; nothing in the grid can be closed, detached, or
    floated. Page 2 (Logs) is a :class:`ServicingLogPage` hosting one table
    per configured servicing-log kind plus the relocated :class:`LogPanel`.

    Args:
        station: The active Station instance.
        orchestrator: The active Orchestrator instance.
        parent: Optional Qt parent widget.
        catalog: Optional ConfigCatalog enabling the Config menu.
        active_config_path: Path of the currently-active config, or None.
        restart_callback: Called after a confirmed config switch, or None.
        startup_warning: Startup config-fallback warning to surface, or None.
        session_manager: Optional SessionManager (L6), forwarded to
            SessionInfoPanel and used for attribution prefills.
        cryogenics_config: The active config's resolved ``cryogenics:``
            block (``Station.read_cryogenics_config()``), or None/empty when
            the setup has no such block. Optional — every existing
            construction site keeps working unchanged.
        operations_config: The active config's resolved ``operations:``
            block (``Station.read_operations_config()``), or None/empty when
            the setup declares none. The Operations panel is available when
            cryogenics is enabled OR this is non-empty — a setup with only
            ``sample_change`` still gets the panel, minus the cryo section.
        helium_store: The active setup's HeliumRecordStore, or None.
        servicing_store: The active setup's ServicingLogStore, or None.
        servicing_log_kinds: The declared, editable log-kind keys this setup
            keeps (``Station.read_servicing_logs_config()``), or None/empty.
        cryogenics_recorder: The active CryogenicsRecorder, or None — only
            used to connect its ``cryo_warning`` signal to the banner.
        panels_config: The active config's ``panels:`` block
            (``Station.read_panels_config()``): per-VI allowlists of the
            controls shown on the compact instrument cards. None/empty means
            every VI keeps its declared ``panel=`` defaults.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
        catalog: ConfigCatalog | None = None,
        active_config_path: str | None = None,
        restart_callback: Callable[[], None] | None = None,
        startup_warning: str | None = None,
        session_manager: SessionManager | None = None,
        cryogenics_config: dict[str, Any] | None = None,
        operations_config: dict[str, dict[str, Any]] | None = None,
        helium_store: HeliumRecordStore | None = None,
        servicing_store: ServicingLogStore | None = None,
        servicing_log_kinds: list[str] | None = None,
        cryogenics_recorder: CryogenicsRecorder | None = None,
        panels_config: dict[str, list[str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._station = station
        self._orchestrator = orchestrator
        self._panels_config = dict(panels_config or {})
        self._procedure_window = None  # lazily created
        self._diagnostics_window = None  # lazily created
        # One Enable Scanner checkbox per switch card, kept in sync because
        # scanner_enabled is a single Station-wide bit.
        self._scanner_enable_checks: list[QCheckBox] = []

        # Cryogenics management (docs/plans/cryogenics-logbook.md §9/§10),
        # all optional — every existing construction site (and every prior
        # test) keeps working with these left at their None defaults, which
        # simply builds the Logs page with no tables/no Operations panel.
        self._cryogenics_config = cryogenics_config
        self._operations_config = dict(operations_config or {})
        self._helium_store = helium_store
        self._servicing_store = servicing_store
        self._servicing_log_kinds = list(servicing_log_kinds or [])
        self._cryogenics_recorder = cryogenics_recorder
        self._operations_panel: OperationsPanel | None = None
        self._cryogenics_enabled = bool(
            self._cryogenics_config
            and self._helium_store is not None
            and self._servicing_store is not None
            and self._station.has_vi(str(self._cryogenics_config.get("level_vi", "")))
        )
        # The Operations panel (plan §12) is available when cryogenics is
        # enabled OR an operations: config block is declared — a setup with
        # only sample_change still gets the panel, minus the cryo section.
        self._operations_panel_enabled = self._cryogenics_enabled or bool(
            self._operations_config
        )

        # Session layer (L6, optional — absent in unit tests). experiment_context()
        # stamps built procedures; the experiment start/close/attendance/findings
        # controls live on the SessionInfoPanel, which owns session_manager directly.
        self._session_manager = session_manager
        # Tracks the experiment_id last seen by _on_session_experiment_changed,
        # so that handler (and the initial-resume check just below) only acts
        # on an actual open/switch transition — never on a same-experiment
        # re-emit (attendance/findings edits).
        self._last_session_experiment_id: str | None = None

        # Config management (optional — absent in unit tests that build the
        # window without a catalog). The Config menu is only built when a
        # catalog is provided.
        self._catalog = catalog
        self._active_config_path = active_config_path
        self._restart_callback = restart_callback
        self._startup_warning = startup_warning
        self._config_controller: ConfigMenuController | None = None

        # Who's logged in (Setup tier, User menu). Identity only — governs which
        # form-autosave file the session *content* below is loaded from/saved to,
        # so switching users switches what's remembered instead of one person's
        # fields overwriting another's. None means nobody has logged in yet (or
        # this is a unit test), and everything falls back to the original
        # shared last_session.json.
        self._current_user_id = app_settings.current_user_id()

        # Persistent session *content* (sample metadata, procedure params, run
        # queue) — a second persistence tier separate from the QSettings window
        # state. Loaded here and applied to the fields once they exist; re-saved
        # on close and by the User menu. When the SessionManager already has an
        # experiment resumed from a previous run (crash/close recovery), its own
        # gui_state.json — not the per-user AppData file — is the right source:
        # this is what makes a resumed session's own sample fields/queue reappear
        # rather than whoever's per-user autosave happens to be current. A
        # resumed experiment with no gui_state.json yet (never saved before the
        # app last stopped) starts from a blank SessionState, not the per-user
        # file, so SessionInfoPanel's own data-dir forcing (see
        # session_info_panel.py) is the one source of truth for its Data Dir.
        resumed_experiment = (
            self._session_manager.current_experiment()
            if self._session_manager is not None
            else None
        )
        if resumed_experiment is not None:
            self._last_session_experiment_id = resumed_experiment.experiment_id
            resumed_gui_state_path = self._session_manager.current_gui_state_path()
            self._session = (
                session_store.load(resumed_gui_state_path)
                if resumed_gui_state_path is not None and resumed_gui_state_path.exists()
                else session_store.SessionState()
            )
        else:
            self._session = session_store.load(
                app_settings.session_file_path(self._current_user_id)
            )

        self.setWindowTitle("CryoSoft — Monitor")
        window_geometry.restore_or_center(self, _GEOMETRY_KEY, fraction=0.9)

        self._build_ui()
        self._session_info.apply_session(self._session)
        self._build_menu()
        self._connect_signals()
        self._restore_monitor_state()

        # Attach the log handler after the UI exists (LogPanel guards against
        # a duplicate if the window is ever reconstructed in-process).
        self._log_panel.attach()

        # Surface a startup config fallback (a bad active config was skipped).
        if self._startup_warning:
            self._banner.show_message(
                f"Config fallback in effect — {self._startup_warning}",
                BANNER_SEVERITY_WARNING,
            )

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        """Build the User / Config / Procedures / Diagnostics menus.

        There is no View menu: every quadrant is always visible and nothing
        can be hidden, so there is nothing to toggle. Trend plots are added
        via the button inside the Trends quadrant itself.
        """
        menu_bar = self.menuBar()

        # User menu is added first so it sits leftmost (menu order follows
        # addMenu() call order). Setup-tier concerns: who's logged in, and the
        # per-user form-autosave content that follows (sample info, params,
        # queue) — a "Session" label here would collide with cryosoft.session
        # (L6, the experiment layer), so this menu is named for what it is.
        user_menu = menu_bar.addMenu("User")
        login_action = QAction("Log in as…", self)
        login_action.setToolTip(
            "Pick who's using CryoSoft — switches which saved sample info, "
            "parameters, and queue are loaded"
        )
        login_action.triggered.connect(self._open_login_dialog)
        user_menu.addAction(login_action)

        # L6 session (experiment) surfaces — distinct from the per-user
        # form-autosave content below: these operate on ExperimentRecord
        # folders under app_settings.sessions_root(), not last_session.json.
        load_session_action = QAction("Load Session…", self)
        load_session_action.setToolTip(
            "Switch to another open session (experiment)"
        )
        load_session_action.triggered.connect(self._open_load_session_dialog)
        user_menu.addAction(load_session_action)

        sessions_folder_action = QAction("Sessions Folder…", self)
        sessions_folder_action.setToolTip(
            "Choose where session (experiment) folders are stored — applies "
            "fully on next launch"
        )
        sessions_folder_action.triggered.connect(self._open_sessions_folder_dialog)
        user_menu.addAction(sessions_folder_action)

        user_menu.addSeparator()
        new_session_action = QAction("New Session", self)
        new_session_action.setToolTip(
            "Clear sample info, parameters, and queue and start a fresh session"
        )
        new_session_action.triggered.connect(self._on_new_session)
        user_menu.addAction(new_session_action)
        save_session_action = QAction("Save Session Now", self)
        save_session_action.setToolTip("Write the current session to disk immediately")
        save_session_action.triggered.connect(self._save_session)
        user_menu.addAction(save_session_action)

        # Config menu (only when config management is wired in).
        if self._catalog is not None:
            config_menu = menu_bar.addMenu("Config")
            self._config_controller = ConfigMenuController(
                self,
                config_menu,
                self._catalog,
                self._active_config_path,
                self._restart_callback,
                save_session=self._save_session,
            )
            config_menu.addSeparator()
            instrument_info_action = QAction("Instrument Info…", self)
            instrument_info_action.setToolTip(
                "View each instrument's identity metadata from devices.yaml"
            )
            instrument_info_action.triggered.connect(self._open_instrument_info)
            config_menu.addAction(instrument_info_action)

        proc_menu = menu_bar.addMenu("Procedures")
        open_action = QAction("Open Procedures…", self)
        open_action.setShortcut("Ctrl+P")
        open_action.triggered.connect(self._open_procedures)
        proc_menu.addAction(open_action)

        diagnostics_menu = menu_bar.addMenu("Diagnostics")
        open_diagnostics_action = QAction("Open Diagnostics…", self)
        open_diagnostics_action.setToolTip(
            "Live connection/progress status — for a device that stopped "
            "responding or a run taking longer than expected"
        )
        open_diagnostics_action.triggered.connect(self._open_diagnostics_window)
        diagnostics_menu.addAction(open_diagnostics_action)

    def _open_procedures(self) -> None:
        """Lazily create and show the ProcedureWindow."""
        if self._procedure_window is None:
            from cryosoft.gui.procedure_window import ProcedureWindow
            self._procedure_window = ProcedureWindow(
                self._station,
                self._orchestrator,
                get_sample_info=self.get_sample_info,
                get_data_dir=self.get_data_dir,
                initial_session=self._session,
                get_experiment_info=self.get_experiment_info,
            )
        self._procedure_window.show()
        self._procedure_window.raise_()
        self._procedure_window.activateWindow()

    def _open_diagnostics_window(self) -> None:
        """Lazily create and show the DiagnosticsWindow."""
        if self._diagnostics_window is None:
            self._diagnostics_window = DiagnosticsWindow(self._orchestrator)
        self._diagnostics_window.show()
        self._diagnostics_window.raise_()
        self._diagnostics_window.activateWindow()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        content_widget = QWidget()
        root = QVBoxLayout(content_widget)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        self._system_vi_names = [
            n for n in self._station.get_vi_names()
            if self._station.get_vi_type(n) in {"system", "level"}
        ]
        measurement_vis = [
            n for n in self._station.get_vi_names()
            if self._station.get_vi_type(n) == "measurement"
        ]
        switch_vis = [
            n for n in self._station.get_vi_names()
            if self._station.get_vi_type(n) == "switch"
        ]

        # ── Header ────────────────────────────────────────────────────
        root.addLayout(self._build_header())

        # ── Notification banner (hidden until a warning/error arrives) ─
        self._banner = NotificationBanner()
        root.addWidget(self._banner)

        # ── Fixed 2x2 quadrant grid (Page 1 — Monitor) ───────────────
        top_left = self._build_instruments_quadrant(measurement_vis, switch_vis)
        self._trends = TrendsQuadrant(self._station, parent=self)
        self._session_info = SessionInfoPanel(session_manager=self._session_manager)
        bottom_right = self._build_operations_quadrant()

        self._left_splitter = QSplitter(Qt.Orientation.Vertical)
        self._left_splitter.setObjectName("left_splitter")
        self._left_splitter.setChildrenCollapsible(False)
        self._left_splitter.addWidget(top_left)
        self._left_splitter.addWidget(self._session_info)
        self._left_splitter.setSizes([750, 250])

        self._right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._right_splitter.setObjectName("right_splitter")
        self._right_splitter.setChildrenCollapsible(False)
        self._right_splitter.addWidget(self._trends)
        self._right_splitter.addWidget(bottom_right)
        self._right_splitter.setSizes([750, 250])

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("main_splitter")
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._left_splitter)
        self._main_splitter.addWidget(self._right_splitter)
        self._main_splitter.setSizes([600, 600])

        # ── Page 2 — Logs ─────────────────────────────────────────────
        # The application LogPanel is created here and composed into the
        # Logs page (moved off the bottom-right quadrant); MonitorWindow
        # still owns its attach()/detach() lifecycle (see __init__/closeEvent).
        self._log_panel = LogPanel()
        self._servicing_log_page = ServicingLogPage(
            self._servicing_store,
            self._servicing_log_kinds,
            self._log_panel,
            get_current_person=self._current_person_for_logs,
            parent=self,
        )

        # ── Page switcher: a QStackedWidget driven by the header tab bar ──
        self._page_stack = QStackedWidget()
        self._page_stack.setObjectName("page_stack")
        self._page_stack.addWidget(self._main_splitter)  # page 0: Monitor
        self._page_stack.addWidget(self._servicing_log_page)  # page 1: Logs
        root.addWidget(self._page_stack)
        self._page_tab_bar.currentChanged.connect(self._on_page_changed)

        # ── Content widget is the central widget directly (no outer scroll) ──
        self.setCentralWidget(content_widget)

        # ── Status bar ────────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._state_label = QLabel("State: IDLE")
        self._status_bar.addWidget(self._state_label)
        # Current status-bar 'level' ("", "active", "error"); tracked so the
        # dynamic-property restyle only fires when the level actually changes.
        self._status_level = ""

    def _build_header(self) -> QHBoxLayout:
        """Build the top toolbar with title and global action buttons.

        Returns:
            A QHBoxLayout containing the header widgets.
        """
        row = QHBoxLayout()

        title = QLabel("<b>CryoSoft</b>  — Instrument Monitor")
        row.addWidget(title)

        self._current_user_label = QLabel()
        self._current_user_label.setObjectName("current_user_label")
        self._sync_current_user_label()
        row.addWidget(self._current_user_label)

        # Slim page switcher: Page 1 (Monitor, the quadrant grid, unchanged)
        # / Page 2 (Logs, ServicingLogPage). Not connected here — the pages
        # it switches between are built later in _build_ui(); the connection
        # is made once both exist, at the end of _build_ui().
        self._page_tab_bar = QTabBar()
        self._page_tab_bar.setObjectName("page_tab_bar")
        self._page_tab_bar.addTab("Monitor")
        self._page_tab_bar.addTab("Logs")
        self._page_tab_bar.setExpanding(False)
        row.addWidget(self._page_tab_bar)

        row.addStretch()

        # Monitoring toggle: the Orchestrator polls no instrument until
        # monitoring is started (typically after "Initiate All" has brought
        # the instruments up), and can be stopped again in IDLE to debug an
        # instrument by hand. Checked state mirrors the Orchestrator via
        # monitoring_changed — never set optimistically from the click alone.
        self._monitoring_btn = QPushButton()
        self._monitoring_btn.setObjectName("monitoring_btn")
        self._monitoring_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._monitoring_btn.setCheckable(True)
        self._monitoring_btn.clicked.connect(self._on_monitoring_clicked)
        self._sync_monitoring_btn()
        row.addWidget(self._monitoring_btn)

        initiate_all_btn = QPushButton("Initiate All")
        initiate_all_btn.setObjectName("initiate_all_btn")
        initiate_all_btn.setProperty("class", BTN_CLASS_PRIMARY)
        initiate_all_btn.setIcon(qta.icon("fa5s.play", color=TEXT_ON_ACCENT))
        initiate_all_btn.setToolTip("Bring every instrument to its operating state")
        initiate_all_btn.clicked.connect(
            lambda: self._orchestrator.submit_global_action("initiate_all")
        )

        standby_all_btn = QPushButton("Standby All")
        standby_all_btn.setObjectName("standby_all_btn")
        standby_all_btn.setProperty("class", BTN_CLASS_SECONDARY)
        standby_all_btn.setIcon(qta.icon("fa5s.power-off", color=TEXT_PRIMARY))
        standby_all_btn.setToolTip("Return every instrument to a safe standby state")
        standby_all_btn.clicked.connect(
            lambda: self._orchestrator.submit_global_action("standby_all")
        )

        row.addWidget(initiate_all_btn)
        row.addWidget(standby_all_btn)
        return row

    def _on_monitoring_clicked(self, checked: bool) -> None:
        """Start or stop monitoring from the header toggle.

        The Orchestrator may refuse a stop (outside IDLE/ERROR the safety
        watchdog must keep running; the refusal reason arrives on
        ``action_blocked`` and shows in the banner), so the button is re-synced
        from the confirmed state rather than left at the clicked position.

        Args:
            checked: The button's new checked state after the click.
        """
        if checked:
            self._orchestrator.start_monitoring()
        else:
            self._orchestrator.stop_monitoring()
        self._sync_monitoring_btn()

    def _sync_monitoring_btn(self) -> None:
        """Mirror the Orchestrator's confirmed monitoring state onto the toggle."""
        monitoring = self._orchestrator.is_monitoring()
        btn = self._monitoring_btn
        btn.setChecked(monitoring)
        btn.setText("Stop Monitoring" if monitoring else "Start Monitoring")
        btn.setIcon(
            qta.icon("fa5s.eye-slash" if monitoring else "fa5s.eye", color=TEXT_PRIMARY)
        )
        btn.setToolTip(
            "Stop polling instrument state (allowed only while idle — e.g. to "
            "debug an instrument by hand)"
            if monitoring
            else "Start polling instrument state each tick (do this once the "
            "instruments have been initiated)"
        )

    def _build_instruments_quadrant(
        self, measurement_vis: list[str], switch_vis: list[str]
    ) -> QWidget:
        """Build the top-left quadrant: a scrollable 2-column grid of ALL VI cards.

        System/level VIs come first (config order, untagged), then
        measurement and switch VIs as tagged cards — full citizens of the
        instrument grid since the Other Devices section was retired. The
        switch card carries the station-wide Enable Scanner checkbox as its
        extra widget. Panels are built once and kept in self._panels for the
        lifetime of the window — recreating them would drop their
        Orchestrator signal connections.

        Args:
            measurement_vis: Names of measurement VIs, rendered tagged.
            switch_vis: Names of switch/scanner VIs, rendered tagged.

        Returns:
            A QWidget containing the title, and a QScrollArea of InstrumentPanels.
        """
        container = QWidget()
        container.setObjectName("instruments_quadrant")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Instruments</b>"))

        entries: list[tuple[str, str | None]] = [
            (n, None) for n in self._system_vi_names
        ]
        entries += [(n, "Measurement") for n in measurement_vis]
        entries += [(n, "Scanner") for n in switch_vis]

        self._panels: list[InstrumentPanel] = []
        grid_container = QWidget()
        grid = QGridLayout(grid_container)
        grid.setSpacing(6)
        for idx, (vi_name, type_tag) in enumerate(entries):
            vi = self._station._virtual_instruments[vi_name]
            extra = (
                self._build_scanner_enable_checkbox(vi_name)
                if vi_name in switch_vis
                else None
            )
            panel = InstrumentPanel(
                vi_name,
                vi,
                self._orchestrator,
                parent=self,
                panel_controls=self._panels_config.get(vi_name),
                type_tag=type_tag,
                extra_widget=extra,
            )
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._panels.append(panel)
            row, col = divmod(idx, 2)
            grid.addWidget(panel, row, col)

        scroll = QScrollArea()
        scroll.setObjectName("instruments_scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(grid_container)
        outer.addWidget(scroll)
        return container

    def _build_scanner_enable_checkbox(self, vi_name: str) -> QCheckBox:
        """Build one switch card's Enable Scanner checkbox.

        scanner_enabled is a single Station-owned bit shared by every switch
        VI (see GLOSSARY.md "scanner_enabled"), so with more than one switch
        card every card's checkbox tracks the same state.

        Args:
            vi_name: The switch VI whose card hosts this checkbox.

        Returns:
            The wired QCheckBox.
        """
        enable_chk = QCheckBox("Enable Scanner")
        enable_chk.setObjectName(f"{vi_name}_enable_scanner_chk")
        enable_chk.setToolTip(
            "Off by default. Required before the Procedure window offers "
            "this scanner's routes as loopable measurement parameters."
        )
        enable_chk.setChecked(self._orchestrator.scanner_enabled())
        enable_chk.toggled.connect(self._on_scanner_toggled)
        self._scanner_enable_checks.append(enable_chk)
        return enable_chk

    def _on_scanner_toggled(self, checked: bool) -> None:
        """Apply the station-wide scanner toggle and keep every switch card in sync.

        Args:
            checked: New state of the checkbox that was toggled.
        """
        self._orchestrator.set_scanner_enabled(checked)
        for chk in self._scanner_enable_checks:
            if chk.isChecked() != checked:
                chk.blockSignals(True)
                chk.setChecked(checked)
                chk.blockSignals(False)

    def _build_operations_quadrant(self) -> QWidget:
        """Build the bottom-right quadrant: the optional Operations panel.

        The Other Devices section that used to share this quadrant is
        retired — measurement and switch VIs are full cards in the
        instrument grid now — so the quadrant holds the OperationsPanel when
        ``self._operations_panel_enabled``, else a placeholder label.

        Returns:
            A QWidget containing the title and the panel (or placeholder).
        """
        container = QWidget()
        container.setObjectName("operations_quadrant")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Operations</b>"))

        if self._operations_panel_enabled:
            self._operations_panel = OperationsPanel(
                self._station,
                self._orchestrator,
                dict(self._cryogenics_config or {}) if self._cryogenics_enabled else None,
                self._operations_config,
                self._helium_store,
                self._servicing_store,
                get_data_dir=self.get_data_dir,
                get_current_person=self._current_person_for_logs,
                parent=self,
            )
            operations_scroll = QScrollArea()
            operations_scroll.setObjectName("operations_scroll")
            operations_scroll.setWidgetResizable(True)
            operations_scroll.setWidget(self._operations_panel)
            outer.addWidget(operations_scroll)
        else:
            placeholder = QLabel("No operations configured for this setup.")
            placeholder.setProperty("class", "secondary_label")
            outer.addWidget(placeholder)
            outer.addStretch()
        return container

    def _on_page_changed(self, index: int) -> None:
        """Switch the central QStackedWidget's page and refresh Logs on show.

        Args:
            index: The tab bar's new current index (0 = Monitor, 1 = Logs).
        """
        self._page_stack.setCurrentIndex(index)
        if index == 1:
            self._servicing_log_page.refresh()

    def _current_person_for_logs(self) -> str:
        """Return the active experiment's user name, for attribution prefill.

        Used to prefill the "Edited by" / "Deleted by" fields on the Logs
        page and the operator-name field on the Fill helium dialog.

        Returns:
            The active experiment's user name, or ``""`` when no session
            layer is wired or no experiment is currently open.
        """
        info = self.get_experiment_info()
        return str(info.get("experiment", {}).get("user_name", ""))

    # ------------------------------------------------------------------
    # Public sample-info accessors (used by ProcedureWindow)
    # ------------------------------------------------------------------

    def get_sample_info(self) -> dict[str, str]:
        """Return the current sample info as a dict.

        Returns:
            Dict with keys ``sample_name``, ``sample_id``, ``comments``.
        """
        return self._session_info.get_sample_info()

    def get_data_dir(self) -> str:
        """Return the configured data directory path.

        Returns:
            Absolute path string; falls back to the open session's own data
            folder, or (no session open) ``app_settings.sessions_root()``, if
            the field is empty (``SessionInfoPanel.get_data_dir``).
        """
        return self._session_info.get_data_dir()

    def get_experiment_info(self) -> dict[str, str]:
        """Return the session layer's experiment context for procedure stamping.

        Returns:
            ``SessionManager.experiment_context()`` (experiment id/title, user
            identity), or ``{}`` when no session layer is wired or no
            experiment is open.
        """
        if self._session_manager is None:
            return {}
        return self._session_manager.experiment_context()

    # ------------------------------------------------------------------
    # Session persistence (content tier: sample info, procedure params, queue)
    # ------------------------------------------------------------------

    def _collect_session_state(self) -> session_store.SessionState:
        """Build a SessionState from the current UI, preserving procedure data.

        The Sample Info fields are read live. The procedure selection,
        parameters, and queue come from the open ProcedureWindow if there is
        one; otherwise the values loaded at startup are preserved unchanged.
        """
        info = self.get_sample_info()
        state = session_store.SessionState(
            sample_name=info["sample_name"],
            sample_id=info["sample_id"],
            comments=info["comments"],
            data_dir=self.get_data_dir(),
            selected_procedure=self._session.selected_procedure,
            procedure_params=self._session.procedure_params,
            queue=self._session.queue,
        )
        if self._procedure_window is not None:
            self._procedure_window.export_session_state(state)
        return state

    def _save_session(self) -> None:
        """Persist the current session to disk, tolerating write failures.

        When an experiment is open, GUI state follows the session bundle —
        ``session_manager.current_gui_state_path()`` inside the session
        folder — instead of the per-user AppData file, and the run queue is
        additionally promoted into the experiment record itself via
        ``set_queue`` so it survives independently of the autosave file. With
        no experiment open, behavior is unchanged (the per-user AppData
        file).
        """
        self._session = self._collect_session_state()
        session_manager = self._session_manager
        is_session_open = (
            session_manager is not None and session_manager.current_experiment() is not None
        )
        if is_session_open:
            path = session_manager.current_gui_state_path()
        else:
            path = app_settings.session_file_path(self._current_user_id)
        try:
            session_store.save(self._session, path)
        except OSError as exc:
            logger.warning("MonitorWindow: could not save session: %s", exc)
        if is_session_open:
            session_manager.set_queue([item.to_dict() for item in self._session.queue])

    def _on_new_session(self) -> None:
        """Clear the session to defaults after user confirmation."""
        reply = QMessageBox.question(
            self,
            "New Session",
            "Clear the current session (sample info, parameters, and queue) "
            "and start fresh?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._session = session_store.SessionState()
        self._session_info.apply_session(self._session)
        if self._procedure_window is not None:
            self._procedure_window.reset_session()
        self._save_session()

    def _open_login_dialog(self) -> None:
        """Open LoginDialog and switch to the picked user, if any."""
        if self._session_manager is None:
            QMessageBox.information(
                self, "Log In", "Session management is not available."
            )
            return
        dialog = LoginDialog(
            self._session_manager.roster, self._current_user_id, self
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._switch_user(dialog.selected_user_id())

    def _switch_user(self, user_id: str) -> None:
        """Save the outgoing user's fields and load the incoming user's own.

        Only the Session Info panel's sample fields, data dir, and app
        settings persistence follow the switch; a ProcedureWindow already
        open keeps its in-memory queue/params from before the switch (it
        re-reads whoever is current the next time it is built).

        Args:
            user_id: The roster id to switch to.
        """
        self._save_session()
        self._current_user_id = user_id
        app_settings.set_current_user_id(user_id)
        self._session = session_store.load(app_settings.session_file_path(user_id))
        self._session_info.apply_session(self._session)
        self._sync_current_user_label()

    def _open_load_session_dialog(self) -> None:
        """Open LoadSessionDialog and switch to the picked experiment, if any."""
        if self._session_manager is None:
            QMessageBox.information(
                self, "Load Session", "Session management is not available."
            )
            return
        dialog = LoadSessionDialog(self._session_manager, self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        experiment_id = dialog.selected_experiment_id()
        if experiment_id:
            self._switch_session(experiment_id)

    def _open_sessions_folder_dialog(self) -> None:
        """Browse for a new sessions root; persists for the next launch only."""
        if self._session_manager is None:
            QMessageBox.information(
                self, "Sessions Folder", "Session management is not available."
            )
            return
        selected = QFileDialog.getExistingDirectory(
            self, "Select Sessions Folder", str(app_settings.sessions_root())
        )
        if not selected:
            return
        app_settings.set_sessions_root(selected)
        self._status_bar.showMessage(
            "Sessions folder updated — applies fully on next launch", 5000
        )

    def _switch_session(self, experiment_id: str) -> None:
        """Save the outgoing session's fields and load the incoming session's own.

        Mirrors ``_switch_user``'s save-outgoing/load-incoming shape one
        level up, at the L6 session-record level: (1) persist the current
        GUI state to wherever it is currently targeted, (2) ask
        ``SessionManager`` to switch — a ``ValueError`` (unknown id, or the
        target is not open) surfaces as a warning and aborts before anything
        else changes, (3) load the target's own ``gui_state.json`` (a default
        ``SessionState`` when it has none yet) and apply it to the Session
        Info panel. A ``ProcedureWindow`` already open keeps its in-memory
        queue/params from before the switch — the same documented limitation
        ``_switch_user`` has — it re-reads whoever is current only the next
        time it is (re)built.

        Args:
            experiment_id: The store key of an open experiment to switch to.
        """
        if self._session_manager is None:
            return
        self._save_session()
        try:
            self._session_manager.switch_experiment(experiment_id)
        except ValueError as exc:
            QMessageBox.warning(self, "Could not switch session", str(exc))
            return
        gui_state_path = self._session_manager.current_gui_state_path()
        self._session = (
            session_store.load(gui_state_path)
            if gui_state_path is not None and gui_state_path.exists()
            else session_store.SessionState()
        )
        self._session_info.apply_session(self._session)

    def _sync_current_user_label(self) -> None:
        """Reflect the current login in the header label."""
        if not self._current_user_id:
            self._current_user_label.setText("Not logged in")
            return
        name = self._current_user_id
        if self._session_manager is not None:
            user = self._session_manager.roster.get(self._current_user_id)
            if user is not None and user.name:
                name = user.name
        self._current_user_label.setText(f"Logged in as {name}")

    def _open_instrument_info(self) -> None:
        """Open a read-only view of each VI's devices.yaml metadata block."""
        metadata = (
            read_instrument_metadata(self._active_config_path)
            if self._active_config_path
            else {}
        )
        InstrumentInfoDialog(metadata, self).exec()

    # ------------------------------------------------------------------
    # Signal connections
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._orchestrator.monitoring_changed.connect(
            lambda _on: self._sync_monitoring_btn()
        )
        self._orchestrator.state_changed.connect(self._on_state_changed)
        self._orchestrator.error_occurred.connect(self._on_error)
        self._orchestrator.action_blocked.connect(self._on_action_blocked)
        self._orchestrator.action_failed.connect(self._on_action_failed)
        self._orchestrator.action_succeeded.connect(self._on_action_confirmed)
        # Separate from InstrumentPanel's own states_updated connections
        # (each panel connects itself in its constructor) — this slot only
        # feeds the Trends quadrant and the optional Operations panel.
        self._orchestrator.states_updated.connect(self._on_states_updated)
        # run_finished fires only at run boundaries (not every tick), so
        # there is no teardown-race concern connecting it here directly.
        self._orchestrator.run_finished.connect(self._on_run_finished_for_logs)
        if self._cryogenics_recorder is not None:
            self._cryogenics_recorder.cryo_warning.connect(self._on_cryo_warning)
        if self._session_manager is not None:
            self._session_manager.experiment_changed.connect(
                self._on_session_experiment_changed
            )
            self._session_manager.store_health_changed.connect(
                self._on_store_health_changed
            )

    def _on_session_experiment_changed(self, record: dict) -> None:
        """Load a newly opened/switched session's gui_state.json, if it has one.

        Connected to ``SessionManager.experiment_changed`` for every path
        that can bring a *different* experiment live — Start Experiment,
        ``switch_experiment``, and the resume-on-construction case already
        handled once in ``__init__``. A brand-new experiment (just started
        via Start Experiment) has no ``gui_state.json`` yet — doing nothing
        in that case is what stops a default ``SessionState`` from wiping
        the sample fields the physicist just typed. A same-experiment
        re-emit (attendance/findings edits) is ignored outright.

        Args:
            record: ``ExperimentRecord.to_dict()``, or ``{}`` when none open.
        """
        experiment_id = record.get("experiment_id", "") if record else ""
        if not experiment_id or experiment_id == self._last_session_experiment_id:
            self._last_session_experiment_id = experiment_id or None
            return
        self._last_session_experiment_id = experiment_id
        if self._session_manager is None:
            return
        gui_state_path = self._session_manager.current_gui_state_path()
        if gui_state_path is None or not gui_state_path.exists():
            return
        self._session = session_store.load(gui_state_path)
        self._session_info.apply_session(self._session)

    def _on_store_health_changed(self, info: dict) -> None:
        """Surface a session-record save failure/recovery via the banner + status bar.

        ``ok=False`` shows a persistent banner error — the physicist should
        know before losing work that the record is not reaching disk.
        ``ok=True`` clears it (the banner's own dismiss, not another
        message) and confirms recovery as a routine status-bar note instead
        of a second banner.

        Args:
            info: ``{"ok": bool, "detail": str}`` from
                ``SessionManager.store_health_changed``.
        """
        if info.get("ok"):
            self._banner.dismiss()
            self._status_bar.showMessage("Session record saving recovered", 5000)
            return
        detail = info.get("detail", "")
        self._banner.show_message(
            f"Session record is NOT being saved: {detail}", BANNER_SEVERITY_ERROR
        )

    def _on_states_updated(self, state: dict) -> None:
        """Forward the per-tick state snapshot to the Trends and Operations panels.

        The WINDOW (not the child panels) is the connection receiver on
        purpose: Qt severs a receiver's connections at the start of its own
        destruction, so routing the tick through the window guarantees the
        Orchestrator's still-running timer can never reach a partially
        destroyed child tree. Connecting the panels directly re-introduced a
        teardown race (RuntimeError/segfault on a deleted plot curve when a
        tick landed mid-destruction under pytest-qt).

        Args:
            state: ``{vi_name: {field: value, ...}}`` from the Orchestrator.
        """
        self._trends.on_states_updated(state)
        if self._operations_panel is not None:
            self._operations_panel.on_states_updated(state)

    def _on_run_finished_for_logs(self, _manifest: dict) -> None:
        """Refresh the Logs page's tables after any run finishes.

        A run's manifest (procedure or operation) may have just produced a
        new servicing-log entry via the CryogenicsRecorder (connected ahead
        of this in main.py, so its write always lands before this refresh
        reads); cheap to call even when nothing changed.
        """
        self._servicing_log_page.refresh()

    def _on_cryo_warning(self, message: str) -> None:
        """Surface the recorder's low-helium advisory via the existing banner."""
        self._banner.show_message(message, BANNER_SEVERITY_WARNING)

    def _on_state_changed(self, state_name: str) -> None:
        """Update the status bar label and colour level when state changes.

        The status bar background is driven by a dynamic ``level`` QSS property
        (``""``/``"active"``/``"error"``). The restyle only fires when the level
        actually changes (same repolish pattern as the InstrumentPanel border).

        Args:
            state_name: The new state name string (e.g. ``"IDLE"``).
        """
        self._state_label.setText(f"State: {state_name}")
        logger.debug("MonitorWindow: orchestrator state → %s", state_name)

        if state_name in _ERROR_STATES:
            level = "error"
        elif state_name in _ACTIVE_STATES:
            level = "active"
        else:
            level = ""

        if level != self._status_level:
            self._status_level = level
            self._status_bar.setProperty("level", level)
            # Repolish the child label too: descendant selectors like
            # QStatusBar[level="error"] QLabel are resolved per-widget, so
            # repolishing only the status bar leaves the label's old colour.
            for widget in (self._status_bar, self._state_label):
                widget.style().unpolish(widget)
                widget.style().polish(widget)

    def _on_error(self, message: str) -> None:
        """Show a non-modal error banner when ERROR or EMERGENCY is entered.

        Replaces the old blocking ``QMessageBox.critical`` so repeated error
        signals no longer stack modal dialogs over the GUI.

        Args:
            message: Human-readable error description.
        """
        logger.error("MonitorWindow: %s", message)
        self._banner.show_message(message, BANNER_SEVERITY_ERROR)

    def _on_action_blocked(self, message: str) -> None:
        """Show a non-modal warning banner when the Orchestrator blocks an action.

        Args:
            message: Human-readable reason the action was blocked.
        """
        self._banner.show_message(message, BANNER_SEVERITY_WARNING)

    def _on_action_failed(self, vi_name: str, method_name: str, reason: str) -> None:
        """Show a non-modal error banner when a submitted GUI action raises.

        This is the uniform failure verdict of the control-validation
        standard: limit rejections and VI safety guards (e.g. the
        switch-heater mismatch refusal) arrive here with the reason string
        the VI wrote for the user.

        Args:
            vi_name: The VI the action targeted.
            method_name: The @control method that was called.
            reason: The exception message explaining why it was refused.
        """
        self._banner.show_message(
            f"{vi_name}.{method_name} failed: {reason}", BANNER_SEVERITY_ERROR
        )

    def _on_action_confirmed(self, vi_name: str, method_name: str) -> None:
        """Confirm a successful GUI action with a transient status-bar message.

        A self-expiring status-bar message (not the banner) on purpose:
        success is routine and should not demand a dismissal click, while
        failures (banner) must. ``showMessage`` temporarily overlays the
        permanent state label and restores it automatically.

        Args:
            vi_name: The VI the action targeted.
            method_name: The @control method that completed.
        """
        self._status_bar.showMessage(f"{vi_name}.{method_name} ✓ done", 4000)

    # ------------------------------------------------------------------
    # Window lifecycle + layout persistence
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Detach the log handler and persist geometry/splitter/trend state before closing.

        Detaching prevents the handler from writing to the destroyed
        ``QTextEdit`` after the window is gone (RuntimeError on a dead widget).
        Splitter proportions and trend selections are saved automatically
        here (no separate "Save layout" action, unlike the old dock-state
        save/restore) — there is nothing else for the user to arrange since
        panels can't be hidden, closed, or moved out of their quadrant.

        Args:
            event: The Qt close event.
        """
        self._save_session()
        self._log_panel.detach()
        settings = app_settings.get_settings()
        settings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        settings.setValue(_MAIN_SPLITTER_KEY, self._main_splitter.saveState())
        settings.setValue(_LEFT_SPLITTER_KEY, self._left_splitter.saveState())
        settings.setValue(_RIGHT_SPLITTER_KEY, self._right_splitter.saveState())
        self._trends.save_settings()
        super().closeEvent(event)

    def _restore_splitter_state(self) -> None:
        """Restore each quadrant splitter's saved proportions, defensively.

        ``QSplitter.restoreState()`` restores orientation and child count
        from the saved blob, not just sizes — applying a blob saved by a
        differently-shaped splitter (e.g. a stale value some other settings
        key never got cleared) would silently reshape this one. The
        orientation/count are captured before restoring and checked after;
        a mismatch reverts the restore rather than leaving a corrupted
        layout. A missing key or a ``restoreState()`` failure both silently
        keep the default proportions set in ``_build_ui()``.
        """
        settings = app_settings.get_settings()
        for splitter, key in (
            (self._main_splitter, _MAIN_SPLITTER_KEY),
            (self._left_splitter, _LEFT_SPLITTER_KEY),
            (self._right_splitter, _RIGHT_SPLITTER_KEY),
        ):
            state = settings.value(key)
            if state is None:
                continue
            expected_orientation = splitter.orientation()
            expected_count = splitter.count()
            expected_sizes = splitter.sizes()
            try:
                splitter.restoreState(state)
            except (TypeError, ValueError) as exc:
                logger.debug("MonitorWindow: could not restore %s: %s", key, exc)
                continue
            if splitter.orientation() != expected_orientation or splitter.count() != expected_count:
                logger.warning(
                    "MonitorWindow: %s restoreState() reshaped the splitter "
                    "(likely a stale settings value) — reverting to defaults.",
                    key,
                )
                splitter.setOrientation(expected_orientation)
                splitter.setSizes(expected_sizes)

    def _restore_monitor_state(self) -> None:
        """Restore trend panels and splitter proportions from QSettings, defensively.

        Called once at the end of ``__init__``, after the UI and menu are
        built. The saved trend count/keys are applied first (recreating the
        matching set of trend panels), then splitter proportions are
        restored. A missing key, wrong type, or corrupt JSON all silently
        fall back to the DEFAULT layout already built by ``_build_ui()``.
        """
        self._trends.restore_settings()
        self._restore_splitter_state()
