# ---
# description: |
#   MonitorWindow: the main CryoSoft window showing live instrument state.
#   Content below the header/banner is an inner QMainWindow used purely as a
#   dock host (setDockNestingEnabled), with one QDockWidget per system/level
#   InstrumentPanel, one per TrendPlotPanel (1-4), and one each for Other
#   Devices / Log / Sample Info. A View menu exposes each dock's
#   toggleViewAction() plus Add trend plot / Save layout / Restore default
#   layout. Replaces the earlier splitter-grid + fixed Trends section layout,
#   which crushed panels at real window sizes.
# entry_point: Not run directly. Instantiated in main.py.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.instrument_panel (InstrumentPanel)
#   - cryosoft.gui.monitor_history (MonitorHistory)
#   - cryosoft.gui.trend_plot_panel (TrendPlotPanel)
# input: |
#   Station instance and Orchestrator instance.
# process: |
#   Iterates over all VI names in the station, splitting system/level VIs
#   into InstrumentPanel docks and measurement VIs into a compact Other
#   Devices dock. Builds the DEFAULT dock arrangement (two instrument
#   columns left, two stacked trend docks right, a tabbed Other
#   Devices/Log/Sample Info dock along the bottom) and captures its
#   saveState() in memory for "Restore default layout". Connects
#   Orchestrator signals for the state-driven status bar, the notification
#   banner (errors/blocked actions), and MonitorHistory recording that feeds
#   the trend docks. Owns ProcedureWindow and opens it lazily via the
#   Procedures menu.
# output: |
#   A QMainWindow that stays open for the lifetime of the application.
# ---

"""MonitorWindow — main CryoSoft monitor window."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import Station
from cryosoft.gui import app_settings  # import the module (not the function) so tests can monkeypatch the factory
from cryosoft.gui import session as session_store  # module import keeps save/load monkeypatchable
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.monitor_history import MonitorHistory
from cryosoft.gui.notification_banner import NotificationBanner
from cryosoft.gui.theme import (
    BANNER_SEVERITY_ERROR,
    BANNER_SEVERITY_WARNING,
    BTN_CLASS_PRIMARY,
    BTN_CLASS_SECONDARY,
    LOG_CRITICAL,
    LOG_DEBUG,
    LOG_ERROR,
    LOG_INFO,
    LOG_WARNING,
    TEXT_ON_ACCENT,
    TEXT_PRIMARY,
)
from cryosoft.gui.trend_plot_panel import TrendPlotPanel

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryosoft.core.config_catalog import ConfigCatalog

logger = logging.getLogger(__name__)

_MIN_TREND_PANELS = 1
_MAX_TREND_PANELS = 4
_DEFAULT_TREND_PANEL_COUNT = 2
_LOG_MAX_LINES = 500

# Default key-selection hints applied to the two default trend docks, in
# creation order, once MonitorHistory has keys (first key whose flat name
# contains the hint substring; falls back to the first available key).
_DEFAULT_TREND_KEY_HINTS = ("temperature", "level")

# QSettings keys for persisted window/layout state.
_GEOMETRY_KEY = "MonitorWindow/geometry"
_DOCK_STATE_KEY = "MonitorWindow/dock_state"
_TRENDS_KEY = "MonitorWindow/trends"

# Orchestrator state names that colour the status bar (dynamic 'level' property).
_ACTIVE_STATES = frozenset({
    OrchestratorState.INITIATING.value,
    OrchestratorState.RAMPING.value,
    OrchestratorState.MEASURING.value,
    OrchestratorState.SWEEPING.value,
    OrchestratorState.PAUSED.value,
})
_ERROR_STATES = frozenset({
    OrchestratorState.ERROR.value,
    OrchestratorState.EMERGENCY.value,
})


def _dock_features() -> QDockWidget.DockWidgetFeature:
    """Return the standard feature set (movable + closable + floatable) shared by every dock.

    Returns:
        The OR'd ``QDockWidget.DockWidgetFeature`` flags used on every dock
        this window creates.
    """
    return (
        QDockWidget.DockWidgetFeature.DockWidgetMovable
        | QDockWidget.DockWidgetFeature.DockWidgetClosable
        | QDockWidget.DockWidgetFeature.DockWidgetFloatable
    )


class _QtLogHandler(logging.Handler):
    """Logging handler that appends coloured HTML lines to a QTextEdit.

    Args:
        widget: The read-only QTextEdit to write into.
    """

    _LEVEL_COLOURS: dict[int, str] = {
        logging.DEBUG: LOG_DEBUG,
        logging.INFO: LOG_INFO,
        logging.WARNING: LOG_WARNING,
        logging.ERROR: LOG_ERROR,
        logging.CRITICAL: LOG_CRITICAL,
    }

    def __init__(self, widget: QTextEdit) -> None:
        super().__init__()
        self._widget = widget
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
        # Per-method VI polling noise is written to the file log only.
        if record.name.startswith("cryosoft.vi.") and record.levelno < logging.WARNING:
            return
        try:
            widget = self._widget
            if not widget or not widget.isVisible():
                return
            text = self.format(record)
            colour = self._LEVEL_COLOURS.get(record.levelno, TEXT_PRIMARY)
            bold_open = "<b>" if record.levelno >= logging.CRITICAL else ""
            bold_close = "</b>" if record.levelno >= logging.CRITICAL else ""
            html = f'<span style="color:{colour};">{bold_open}{text}{bold_close}</span>'
            widget.append(html)

            # Trim to _LOG_MAX_LINES to avoid unbounded growth
            doc = widget.document()
            while doc.blockCount() > _LOG_MAX_LINES:
                cursor = widget.textCursor()
                cursor.movePosition(cursor.MoveOperation.Start)
                cursor.select(cursor.SelectionType.BlockUnderCursor)
                cursor.removeSelectedText()
                cursor.deleteChar()

        except Exception:  # noqa: BLE001
            self.handleError(record)


class MonitorWindow(QMainWindow):
    """Main window: live instrument monitor, sample info, global controls, and log.

    Everything below the header/banner lives inside an inner QMainWindow
    (``dock_host``) used purely as a docking area — the standard Qt pattern
    for embedding a dock layout inside a larger window. Every panel is a
    QDockWidget: one ``dock_{vi_name}`` per system/level InstrumentPanel, one
    ``dock_trend_{n}`` per TrendPlotPanel (1-4, backed by a shared
    MonitorHistory), and one each for Other Devices, Log, and Sample Info.
    A View menu exposes each dock's toggle action plus "Add trend plot",
    "Save layout", and "Restore default layout". The Procedures menu opens
    ProcedureWindow lazily.

    Args:
        station: The active Station instance.
        orchestrator: The active Orchestrator instance.
        parent: Optional Qt parent widget.
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
    ) -> None:
        super().__init__(parent)
        self._station = station
        self._orchestrator = orchestrator
        self._procedure_window = None  # lazily created

        # Config management (optional — absent in unit tests that build the
        # window without a catalog). The Config menu is only built when a
        # catalog is provided.
        self._catalog = catalog
        self._active_config_path = active_config_path
        self._restart_callback = restart_callback
        self._startup_warning = startup_warning
        self._config_menu = None
        self._config_editor = None

        # Populated by _build_menu(); referenced by dock-creation helpers
        # that run earlier (during _build_ui()) so they must tolerate None.
        self._view_menu = None
        self._trend_actions_anchor = None
        self._add_trend_action = None

        # Persistent session *content* (sample metadata, procedure params, run
        # queue) — the second persistence tier, separate from the QSettings
        # window/dock *chrome*. Loaded here and applied to the fields once they
        # exist; re-saved on close and by the Session menu.
        self._session = session_store.load(app_settings.session_file_path())

        self.setWindowTitle("CryoSoft — Monitor")
        self._restore_geometry()

        self._build_ui()
        self._apply_session_fields()
        self._build_menu()
        self._connect_signals()
        self._restore_monitor_state()

        # Attach log handler after UI exists. Guard against a duplicate in case
        # the window is ever reconstructed within the same process (handlers
        # live on the shared "cryosoft" logger, so a leak would accumulate).
        self._log_handler = _QtLogHandler(self._log_widget)
        self._log_handler.setLevel(logging.DEBUG)
        cryosoft_logger = logging.getLogger("cryosoft")
        if self._log_handler not in cryosoft_logger.handlers:
            cryosoft_logger.addHandler(self._log_handler)

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
        """Build the Procedures and View menus.

        The View menu needs every dock to already exist (it lists their
        ``toggleViewAction()``s), so this runs after ``_build_ui()``.
        """
        menu_bar = self.menuBar()

        # Session menu is added first so it sits leftmost (menu order follows
        # addMenu() call order), matching the desktop convention for file/session
        # actions.
        session_menu = menu_bar.addMenu("Session")
        new_session_action = QAction("New Session", self)
        new_session_action.setToolTip(
            "Clear sample info, parameters, and queue and start a fresh session"
        )
        new_session_action.triggered.connect(self._on_new_session)
        session_menu.addAction(new_session_action)
        save_session_action = QAction("Save Session Now", self)
        save_session_action.setToolTip("Write the current session to disk immediately")
        save_session_action.triggered.connect(self._save_session)
        session_menu.addAction(save_session_action)

        # Config menu (only when config management is wired in).
        if self._catalog is not None:
            self._config_menu = menu_bar.addMenu("Config")
            self._populate_config_menu()

        proc_menu = menu_bar.addMenu("Procedures")
        open_action = QAction("Open Procedures…", self)
        open_action.setShortcut("Ctrl+P")
        open_action.triggered.connect(self._open_procedures)
        proc_menu.addAction(open_action)

        view_menu = menu_bar.addMenu("View")
        self._view_menu = view_menu

        for vi_name in self._system_vi_names:
            view_menu.addAction(self._instrument_docks[vi_name].toggleViewAction())
        view_menu.addAction(self._other_devices_dock.toggleViewAction())
        view_menu.addAction(self._log_dock.toggleViewAction())
        view_menu.addAction(self._sample_info_dock.toggleViewAction())

        # Trend dock actions live between this anchor separator and the next
        # one, so panels added/removed later can insert/remove around it
        # without disturbing the fixed-dock actions above.
        self._trend_actions_anchor = view_menu.addSeparator()
        for dock in self._trend_docks.values():
            view_menu.insertAction(self._trend_actions_anchor, dock.toggleViewAction())

        view_menu.addSeparator()
        self._add_trend_action = QAction("Add trend plot", self)
        self._add_trend_action.setToolTip(f"Add a trend plot (up to {_MAX_TREND_PANELS})")
        self._add_trend_action.triggered.connect(self._on_trend_add_clicked)
        view_menu.addAction(self._add_trend_action)
        self._update_trend_add_action_state()

        view_menu.addSeparator()
        save_layout_action = QAction("Save layout", self)
        save_layout_action.triggered.connect(self._on_save_layout)
        view_menu.addAction(save_layout_action)

        restore_default_action = QAction("Restore default layout", self)
        restore_default_action.triggered.connect(self._on_restore_default_layout)
        view_menu.addAction(restore_default_action)

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
            )
        self._procedure_window.show()
        self._procedure_window.raise_()
        self._procedure_window.activateWindow()

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

        # Shared ring-buffer history feeding all Trend plot panels. Qt-free
        # by design (see monitor_history.py), so it is created here rather
        # than inside TrendPlotPanel.
        self._history = MonitorHistory()

        # ── Header ────────────────────────────────────────────────────
        root.addLayout(self._build_header())

        # ── Notification banner (hidden until a warning/error arrives) ─
        self._banner = NotificationBanner()
        root.addWidget(self._banner)

        # ── Dock host ─────────────────────────────────────────────────
        # A QMainWindow is itself just a widget and needs no central widget;
        # used here purely as a docking area embedded inside the outer
        # window's layout (the standard Qt "dock host" pattern).
        self._dock_host = QMainWindow()
        self._dock_host.setObjectName("dock_host")
        self._dock_host.setDockNestingEnabled(True)
        self._dock_host.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
        )
        # A zero-size central widget keeps the surrounding dock areas from
        # sharing space with an empty middle region.
        placeholder = QWidget()
        placeholder.setMaximumSize(0, 0)
        self._dock_host.setCentralWidget(placeholder)

        # ── Instrument panel docks (system/level VIs) ───────────────────
        # Panels are built once, in config order, and kept in self._panels
        # for the lifetime of the window — recreating them would drop their
        # Orchestrator signal connections.
        self._panels: list[InstrumentPanel] = []
        self._instrument_docks: dict[str, QDockWidget] = {}
        for vi_name in self._system_vi_names:
            vi = self._station._virtual_instruments[vi_name]
            panel = InstrumentPanel(vi_name, vi, self._orchestrator, parent=self)
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._panels.append(panel)

            dock = QDockWidget(vi_name, self._dock_host)
            dock.setObjectName(f"dock_{vi_name}")
            dock.setFeatures(_dock_features())
            dock.setWidget(panel)
            self._instrument_docks[vi_name] = dock

        # ── Trend docks (populated by _apply_default_layout / restore) ──
        self._trend_panels: dict[str, TrendPlotPanel] = {}
        self._trend_docks: dict[str, QDockWidget] = {}
        self._trend_series_counter = 0
        # Keys the restore path still wants applied once MonitorHistory has
        # data for them (a fresh panel's Y combo is empty until the first
        # states_updated tick, so set_selected_key() at restore time is a
        # harmless no-op that we retry from _on_states_updated_for_history).
        self._pending_trend_keys: dict[str, str] = {}
        # Same retry pattern for the DEFAULT (non-restored) trend docks'
        # opportunistic temperature/level key selection.
        self._default_trend_key_hints: dict[str, str] = {}

        # ── Other Devices / Log / Sample Info docks ─────────────────────
        self._other_devices_dock = self._build_dock(
            "dock_other_devices", "Other Devices", self._build_other_devices_section(measurement_vis)
        )
        self._log_dock = self._build_dock("dock_log", "Log", self._build_log_section())
        self._sample_info_dock = self._build_dock(
            "dock_sample_info", "Sample Info", self._build_sample_info_section()
        )

        # ── Assemble the DEFAULT layout and remember it for later restore ──
        self._apply_default_layout()
        self._default_dock_state = self._dock_host.saveState()

        root.addWidget(self._dock_host)

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

    def _build_dock(self, object_name: str, title: str, widget: QWidget) -> QDockWidget:
        """Wrap a widget in a QDockWidget with the standard feature set.

        Args:
            object_name: The dock's objectName (mandatory — saveState() and
                findChild() both key off it).
            title: The dock's title-bar text.
            widget: The content widget the dock hosts.

        Returns:
            The assembled, not-yet-placed QDockWidget.
        """
        dock = QDockWidget(title, self._dock_host)
        dock.setObjectName(object_name)
        dock.setFeatures(_dock_features())
        dock.setWidget(widget)
        return dock

    def _build_header(self) -> QHBoxLayout:
        """Build the top toolbar with title and global action buttons.

        Returns:
            A QHBoxLayout containing the header widgets.
        """
        row = QHBoxLayout()

        title = QLabel("<b>CryoSoft</b>  — Instrument Monitor")
        row.addWidget(title)

        row.addStretch()

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

    def _build_sample_info_section(self) -> QWidget:
        """Build the sample-info form (session-level metadata).

        No outer QGroupBox: the dock's own title bar ("Sample Info") already
        supplies that chrome, so wrapping it again would waste vertical space
        in the compact bottom tab band.

        Returns:
            A QWidget with name, ID, comments, and data-dir form fields.
        """
        box = QWidget()
        form = QFormLayout(box)

        self._sample_name_input = QLineEdit()
        self._sample_name_input.setObjectName("sample_name_input")
        self._sample_name_input.setPlaceholderText("e.g. Si_001")
        form.addRow("Name:", self._sample_name_input)

        self._sample_id_input = QLineEdit()
        self._sample_id_input.setObjectName("sample_id_input")
        self._sample_id_input.setPlaceholderText("e.g. S2024-01")
        form.addRow("ID:", self._sample_id_input)

        self._comments_input = QTextEdit()
        self._comments_input.setObjectName("comments_input")
        self._comments_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        form.addRow("Comments:", self._comments_input)

        dir_row = QHBoxLayout()
        self._data_dir_input = QLineEdit("C:/CryoData")
        self._data_dir_input.setObjectName("data_dir_input")
        browse_btn = QPushButton("Browse…")
        browse_btn.setObjectName("browse_btn")
        browse_btn.setIcon(qta.icon("fa5s.folder-open", color=TEXT_PRIMARY))
        browse_btn.setToolTip("Choose the directory where run data is saved")
        browse_btn.clicked.connect(self._on_browse_dir)
        dir_row.addWidget(self._data_dir_input)
        dir_row.addWidget(browse_btn)
        form.addRow("Data Dir:", dir_row)

        return box

    def _build_other_devices_section(self, vi_names: list[str]) -> QWidget:
        """Build the compact Other Devices content: one row per measurement VI.

        Each row is name + connection dot/status + small icon Check/Initiate/
        Standby buttons — no tall card boxes. Rows stack vertically for up to
        three VIs; a tight 3-column grid is used beyond that so the dock's
        default height stays small regardless of how many measurement VIs a
        station has.

        Args:
            vi_names: Names of measurement VIs to display.

        Returns:
            A QWidget holding one compact status row per measurement VI (or a
            placeholder label if there are none).
        """
        container = QWidget()

        if not vi_names:
            layout = QVBoxLayout(container)
            layout.addWidget(QLabel("No other devices configured."))
            return container

        if len(vi_names) > 3:
            grid = QGridLayout(container)
            grid.setSpacing(6)
            columns = 3
            for idx, vi_name in enumerate(vi_names):
                row, col = divmod(idx, columns)
                grid.addWidget(self._build_device_status_row(vi_name), row, col)
        else:
            vlay = QVBoxLayout(container)
            vlay.setSpacing(4)
            vlay.setContentsMargins(6, 6, 6, 6)
            for vi_name in vi_names:
                vlay.addWidget(self._build_device_status_row(vi_name))
            vlay.addStretch()

        return container

    def _build_device_status_row(self, vi_name: str) -> QWidget:
        """Build one compact connection-check row for a measurement VI.

        A single ~32-40 px row: coloured dot + status text, then small icon
        Check/Initiate/Standby buttons with tooltips.

        Args:
            vi_name: Registered VI name (e.g. ``"keithley_delta_mode"``).

        Returns:
            A QWidget containing the assembled row.
        """
        row_widget = QWidget()
        row_widget.setObjectName(f"{vi_name}_device_row")
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(8)

        # Connection dot: styled via the dynamic 'status' QSS property
        # (theme.py QLabel[class="conn_dot"][status=...]), never setStyleSheet.
        dot = QLabel("●")
        dot.setObjectName(f"{vi_name}_conn_dot")
        dot.setProperty("class", "conn_dot")
        dot.setProperty("status", "unknown")
        row.addWidget(dot)

        name_lbl = QLabel(vi_name)
        row.addWidget(name_lbl)

        status_lbl = QLabel("Unknown")
        status_lbl.setObjectName(f"{vi_name}_conn_status")
        status_lbl.setProperty("class", "secondary_label")
        row.addWidget(status_lbl)

        row.addStretch()

        check_btn = QPushButton()
        check_btn.setObjectName(f"{vi_name}_check_btn")
        check_btn.setIcon(qta.icon("fa5s.plug", color=TEXT_PRIMARY))
        check_btn.setToolTip(f"Send an identity query to test the {vi_name} connection")

        vi = self._station._virtual_instruments[vi_name]

        def _on_check(checked: bool = False, _vi=vi, _dot=dot, _lbl=status_lbl) -> None:
            try:
                ok = _vi.ping()
            except Exception:
                ok = False
            new_status = "connected" if ok else "disconnected"
            _dot.setProperty("status", new_status)
            # Qt only re-evaluates property-based QSS selectors after an
            # unpolish/polish cycle (same pattern InstrumentPanel uses).
            _dot.style().unpolish(_dot)
            _dot.style().polish(_dot)
            _lbl.setText("Connected" if ok else "Not reachable")

        check_btn.clicked.connect(_on_check)

        initiate_btn = QPushButton()
        initiate_btn.setObjectName(f"{vi_name}_initiate_btn")
        initiate_btn.setIcon(qta.icon("fa5s.play", color=TEXT_PRIMARY))
        initiate_btn.setToolTip(f"Bring {vi_name} to its operating state")
        initiate_btn.clicked.connect(
            lambda checked=False, n=vi_name: self._orchestrator.submit_vi_action(n, "initiate")
        )

        standby_btn = QPushButton()
        standby_btn.setObjectName(f"{vi_name}_standby_btn")
        standby_btn.setIcon(qta.icon("fa5s.power-off", color=TEXT_PRIMARY))
        standby_btn.setToolTip(f"Return {vi_name} to a safe standby state")
        standby_btn.clicked.connect(
            lambda checked=False, n=vi_name: self._orchestrator.submit_vi_action(n, "standby")
        )

        row.addWidget(check_btn)
        row.addWidget(initiate_btn)
        row.addWidget(standby_btn)

        return row_widget

    def _build_log_section(self) -> QTextEdit:
        """Build the real-time log display widget.

        No outer QGroupBox: the dock's own title bar ("Log") already supplies
        that chrome.

        Returns:
            A read-only QTextEdit (objectName ``log_panel``).
        """
        self._log_widget = QTextEdit()
        self._log_widget.setObjectName("log_panel")
        self._log_widget.setReadOnly(True)
        self._log_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return self._log_widget

    # ------------------------------------------------------------------
    # Default layout
    # ------------------------------------------------------------------

    def _apply_default_layout(self) -> None:
        """Arrange every dock into the DEFAULT layout.

        Two instrument columns on the left (VIs distributed round-robin by
        config order — for the standard 5-VI sim_cryostat station this yields
        magnet_x/temperature_vti/level_meter in column 1 and
        magnet_y/temperature_sample in column 2), two trend docks stacked on
        the right, and a tabbed Other Devices/Log/Sample Info dock along the
        bottom (Log active). ``resizeDocks`` enforces the intended
        proportions explicitly — Qt's automatic split guesses are exactly
        what produced the crushed layout this replaces.
        """
        dh = self._dock_host

        self._build_default_trend_docks()

        col1 = self._system_vi_names[0::2]
        col2 = self._system_vi_names[1::2]

        if col1:
            first = self._instrument_docks[col1[0]]
            dh.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, first)
            for a, b in zip(col1, col1[1:]):
                dh.splitDockWidget(self._instrument_docks[a], self._instrument_docks[b], Qt.Orientation.Vertical)
            if col2:
                second = self._instrument_docks[col2[0]]
                dh.splitDockWidget(first, second, Qt.Orientation.Horizontal)
                for a, b in zip(col2, col2[1:]):
                    dh.splitDockWidget(self._instrument_docks[a], self._instrument_docks[b], Qt.Orientation.Vertical)

        dh.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self._other_devices_dock)
        dh.tabifyDockWidget(self._other_devices_dock, self._log_dock)
        dh.tabifyDockWidget(self._log_dock, self._sample_info_dock)
        self._log_dock.raise_()

        trend_ids = list(self._trend_docks.keys())
        if col1 and trend_ids:
            dh.resizeDocks(
                [self._instrument_docks[col1[0]], self._trend_docks[trend_ids[0]]],
                [55, 45],
                Qt.Orientation.Horizontal,
            )

        top_ref = None
        if col1:
            top_ref = self._instrument_docks[col1[0]]
        elif trend_ids:
            top_ref = self._trend_docks[trend_ids[0]]
        if top_ref is not None:
            dh.resizeDocks([top_ref, self._other_devices_dock], [1000, 220], Qt.Orientation.Vertical)

    def _build_default_trend_docks(self) -> None:
        """(Re)create exactly the default number of trend docks, stacked vertically.

        Replaces any existing trend panels/docks, then creates
        ``_DEFAULT_TREND_PANEL_COUNT`` fresh ones, docked on the right and
        split vertically, with opportunistic temperature/level default-key
        hints (applied once MonitorHistory has data — see
        ``_on_states_updated_for_history``).
        """
        for panel_id in list(self._trend_panels.keys()):
            self._remove_trend_panel_widget(panel_id)
        self._trend_series_counter = 0
        self._default_trend_key_hints.clear()

        created = [self._create_trend_panel() for _ in range(_DEFAULT_TREND_PANEL_COUNT)]
        docks = [dock for _, _, dock in created]
        if docks:
            self._dock_host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, docks[0])
            for a, b in zip(docks, docks[1:]):
                self._dock_host.splitDockWidget(a, b, Qt.Orientation.Vertical)

        for (panel_id, _panel, _dock), hint in zip(created, _DEFAULT_TREND_KEY_HINTS):
            self._default_trend_key_hints[panel_id] = hint

        self._update_trend_add_action_state()

    # ------------------------------------------------------------------
    # Trend docks
    # ------------------------------------------------------------------

    def _create_trend_panel(self) -> tuple[str, TrendPlotPanel, QDockWidget]:
        """Create and register a new TrendPlotPanel + its QDockWidget.

        Registers the panel/dock in ``self._trend_panels``/``self._trend_docks``
        and (if the View menu already exists) inserts its toggle action, but
        does NOT place the dock in ``dock_host`` — callers decide placement
        (the default layout stacks the initial pair vertically; later
        additions tabify onto the last trend dock via ``_place_trend_dock``).

        Returns:
            ``(panel_id, panel, dock)`` for the caller to place.
        """
        panel_id = f"trend_{self._next_trend_panel_index()}"
        panel = TrendPlotPanel(
            self._history, panel_id, series_index=self._trend_series_counter, parent=self
        )
        self._trend_series_counter += 1
        panel.remove_requested.connect(self._on_trend_remove_requested)

        dock = QDockWidget(f"Trend — {panel_id}", self._dock_host)
        dock.setObjectName(f"dock_{panel_id}")
        dock.setFeatures(_dock_features())
        dock.setWidget(panel)

        self._trend_panels[panel_id] = panel
        self._trend_docks[panel_id] = dock
        if self._view_menu is not None:
            self._view_menu.insertAction(self._trend_actions_anchor, dock.toggleViewAction())

        return panel_id, panel, dock

    def _place_trend_dock(self, dock: QDockWidget) -> None:
        """Dock a newly created trend dock: tabify onto the last trend dock, or dock right if it is the first.

        Args:
            dock: The (already registered, not yet placed) trend dock.
        """
        others = [d for d in self._trend_docks.values() if d is not dock]
        if others:
            self._dock_host.tabifyDockWidget(others[-1], dock)
        else:
            self._dock_host.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _add_trend_panel(self) -> str:
        """Create, place, and menu-wire a new trend panel (menu action / restore path).

        Returns:
            The new panel's ``panel_id``.
        """
        panel_id, _panel, dock = self._create_trend_panel()
        self._place_trend_dock(dock)
        self._update_trend_add_action_state()
        return panel_id

    def _next_trend_panel_index(self) -> int:
        """Return the smallest non-negative integer not already used in a panel_id.

        Returns:
            An index such that ``f"trend_{index}"`` is not already in use, so
            panel_ids never collide after panels are added and removed.
        """
        used: set[int] = set()
        for panel_id in self._trend_panels:
            try:
                used.add(int(panel_id.rsplit("_", 1)[-1]))
            except ValueError:
                continue
        index = 0
        while index in used:
            index += 1
        return index

    def _on_trend_add_clicked(self) -> None:
        """Add a trend panel via the View menu action, up to the cap."""
        if len(self._trend_panels) >= _MAX_TREND_PANELS:
            return
        self._add_trend_panel()

    def _on_trend_remove_requested(self, panel_id: str) -> None:
        """Remove a trend panel, never dropping below the minimum.

        This is a genuine removal (panel + dock destroyed), distinct from a
        dock's own close button, which just hides it (standard QDockWidget
        behavior) — the toggle action in the View menu brings it back.

        Args:
            panel_id: The panel_id echoed back by TrendPlotPanel.remove_requested.
        """
        if len(self._trend_panels) <= _MIN_TREND_PANELS:
            return
        self._remove_trend_panel_widget(panel_id)
        self._update_trend_add_action_state()

    def _remove_trend_panel_widget(self, panel_id: str) -> None:
        """Unconditionally drop a trend panel's dock and bookkeeping.

        Args:
            panel_id: The panel_id to remove. No-op if not present.
        """
        self._trend_panels.pop(panel_id, None)
        dock = self._trend_docks.pop(panel_id, None)
        if dock is not None:
            if self._view_menu is not None:
                self._view_menu.removeAction(dock.toggleViewAction())
            self._dock_host.removeDockWidget(dock)
            dock.setParent(None)
            dock.deleteLater()  # also deletes the TrendPlotPanel it owns via setWidget()
        self._pending_trend_keys.pop(panel_id, None)
        self._default_trend_key_hints.pop(panel_id, None)

    def _update_trend_add_action_state(self) -> None:
        """Enable/disable the "Add trend plot" action based on the current panel count."""
        if self._add_trend_action is not None:
            self._add_trend_action.setEnabled(len(self._trend_panels) < _MAX_TREND_PANELS)

    # ------------------------------------------------------------------
    # Layout persistence actions (View menu)
    # ------------------------------------------------------------------

    def _on_save_layout(self) -> None:
        """Persist the current dock arrangement and trend selections, with a status-bar confirmation."""
        settings = app_settings.get_settings()
        settings.setValue(_DOCK_STATE_KEY, self._dock_host.saveState())
        self._save_trends()
        self._status_bar.showMessage("Layout saved", 3000)

    def _on_restore_default_layout(self) -> None:
        """Rebuild the default trend docks, then reapply the captured default dock state."""
        self._build_default_trend_docks()
        if self._default_dock_state is not None:
            self._dock_host.restoreState(self._default_dock_state)
        else:
            # Should not normally happen (captured at construction time);
            # defensive fallback rebuilds the arrangement from scratch.
            self._apply_default_layout()
        self._status_bar.showMessage("Default layout restored", 3000)

    # ------------------------------------------------------------------
    # Public sample-info accessors (used by ProcedureWindow)
    # ------------------------------------------------------------------

    def get_sample_info(self) -> dict[str, str]:
        """Return the current sample info as a dict.

        Returns:
            Dict with keys ``sample_name``, ``sample_id``, ``comments``.
        """
        return {
            "sample_name": self._sample_name_input.text().strip(),
            "sample_id": self._sample_id_input.text().strip(),
            "comments": self._comments_input.toPlainText().strip(),
        }

    def get_data_dir(self) -> str:
        """Return the configured data directory path.

        Returns:
            Absolute path string; falls back to ``"C:/CryoData"`` if empty.
        """
        return self._data_dir_input.text().strip() or "C:/CryoData"

    # ------------------------------------------------------------------
    # Session persistence (content tier: sample info, procedure params, queue)
    # ------------------------------------------------------------------

    def _apply_session_fields(self) -> None:
        """Populate the Sample Info fields from the loaded session.

        Called once after ``_build_ui()`` (the fields must exist). Only the
        MonitorWindow-owned fields are applied here; the procedure selection,
        parameters, and queue held in ``self._session`` are applied by the
        ProcedureWindow when it opens.
        """
        state = self._session
        self._sample_name_input.setText(state.sample_name)
        self._sample_id_input.setText(state.sample_id)
        self._comments_input.setPlainText(state.comments)
        self._data_dir_input.setText(state.data_dir or "C:/CryoData")

    def _collect_session_state(self) -> session_store.SessionState:
        """Build a SessionState from the current UI, preserving procedure data.

        The Sample Info fields are read live. The procedure selection,
        parameters, and queue come from the open ProcedureWindow if there is
        one; otherwise the values loaded at startup are preserved unchanged, so
        closing without ever opening the ProcedureWindow does not discard its
        saved state.

        Returns:
            A ``SessionState`` snapshot ready to persist.
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

        A failed save (e.g. the app-data directory is not writable) is logged
        and swallowed so it cannot crash window shutdown.
        """
        self._session = self._collect_session_state()
        try:
            session_store.save(self._session, app_settings.session_file_path())
        except OSError as exc:
            logger.warning("MonitorWindow: could not save session: %s", exc)

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
        self._apply_session_fields()
        if self._procedure_window is not None:
            self._procedure_window.reset_session()
        self._save_session()

    # ------------------------------------------------------------------
    # Config management (menu + selection + restart)
    # ------------------------------------------------------------------

    def _populate_config_menu(self) -> None:
        """(Re)build the Config menu: a checkable list plus the editor entry."""
        if self._config_menu is None or self._catalog is None:
            return
        self._config_menu.clear()
        group = QActionGroup(self)
        group.setExclusive(True)
        active = self._active_config_path
        for entry in self._catalog.list_configs():
            label = entry.name + ("  (read-only)" if entry.read_only else "")
            action = QAction(label, self, checkable=True)
            path_str = str(entry.path)
            if active and Path(path_str).resolve() == Path(active).resolve():
                action.setChecked(True)
            action.triggered.connect(
                lambda _checked, p=path_str: self._on_select_config(p)
            )
            group.addAction(action)
            self._config_menu.addAction(action)

        self._config_menu.addSeparator()
        editor_action = QAction("Open Config Editor…", self)
        editor_action.setToolTip("Edit device/instrument configs with validation")
        editor_action.triggered.connect(self._on_open_config_editor)
        self._config_menu.addAction(editor_action)

    def _on_select_config(self, path: str) -> None:
        """Switch the active config to ``path`` after a warning, then restart.

        Args:
            path: The config directory to make active.
        """
        if self._active_config_path and (
            Path(path).resolve() == Path(self._active_config_path).resolve()
        ):
            return
        reply = QMessageBox.question(
            self,
            "Switch Config",
            f"Switch to config '{Path(path).name}'?\n\n"
            "CryoSoft will save the current session and restart to load it.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            self._populate_config_menu()  # revert the radio selection
            return
        self._apply_active_config(path)

    def _apply_active_config(self, path: str) -> None:
        """Persist ``path`` as active, save the session, and request a restart."""
        self._save_session()
        app_settings.set_config_active_path(path)
        if self._restart_callback is not None:
            self._restart_callback()

    def _on_open_config_editor(self) -> None:
        """Open the config editor window (lazily created)."""
        if self._catalog is None:
            return
        from cryosoft.gui.config_editor import ConfigEditorWindow

        if self._config_editor is None:
            self._config_editor = ConfigEditorWindow(
                self._catalog,
                active_config_path=self._active_config_path,
                apply_callback=self._apply_active_config,
                parent=self,
            )
        self._config_editor.show()
        self._config_editor.raise_()
        self._config_editor.activateWindow()

    # ------------------------------------------------------------------
    # Internal slots
    # ------------------------------------------------------------------

    def _on_browse_dir(self) -> None:
        """Open a directory browser and fill the data-dir field."""
        selected = QFileDialog.getExistingDirectory(
            self, "Select Data Directory", self._data_dir_input.text()
        )
        if selected:
            self._data_dir_input.setText(selected)

    # ------------------------------------------------------------------
    # Signal connections
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._orchestrator.state_changed.connect(self._on_state_changed)
        self._orchestrator.error_occurred.connect(self._on_error)
        self._orchestrator.action_blocked.connect(self._on_action_blocked)
        # Separate from InstrumentPanel's own states_updated connections
        # (each panel connects itself in its constructor) — this slot only
        # feeds MonitorHistory and the Trend plots.
        self._orchestrator.states_updated.connect(self._on_states_updated_for_history)

    def _on_states_updated_for_history(self, state: dict) -> None:
        """Record a state snapshot into MonitorHistory and refresh trend panels.

        Args:
            state: ``{vi_name: {field: value, ...}}`` from the Orchestrator.
        """
        self._history.record(state)
        for panel_id, panel in self._trend_panels.items():
            panel.refresh()

            pending_key = self._pending_trend_keys.get(panel_id)
            if pending_key is not None:
                panel.set_selected_key(pending_key)
                if panel.selected_key() == pending_key:
                    del self._pending_trend_keys[panel_id]
                continue

            hint = self._default_trend_key_hints.get(panel_id)
            if hint is not None:
                keys = self._history.keys()
                if keys:
                    panel.set_selected_key(self._pick_default_trend_key(hint, keys))
                    del self._default_trend_key_hints[panel_id]

    def _pick_default_trend_key(self, hint: str, keys: list[str]) -> str:
        """Pick the best default trend key for a hint substring (e.g. "temperature").

        Flat keys are ``{vi_name}_{field_name}``, and several VI names
        themselves contain hint words (e.g. ``temperature_vti``,
        ``temperature_sample``), which would make a plain substring search
        match a boring setting field (``temperature_sample_heater_output``)
        before the actual reading. This strips the known vi_name prefix first
        so the hint is matched against the FIELD name, falling back to a
        plain substring match over the whole key, and finally the first key,
        if nothing more specific matches.

        Args:
            hint: Substring to look for (e.g. ``"temperature"``, ``"level"``).
            keys: Non-empty, sorted list of MonitorHistory flat keys.

        Returns:
            The chosen flat key.
        """
        for key in keys:
            for vi_name in self._station.get_vi_names():
                prefix = f"{vi_name}_"
                if key.startswith(prefix) and hint in key[len(prefix):]:
                    return key
        for key in keys:
            if hint in key:
                return key
        return keys[0]

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

    # ------------------------------------------------------------------
    # Window geometry + lifecycle
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        """Restore the saved window geometry, or size to a fraction of the screen, centered.

        Geometry is persisted with ``QSettings``, which on Windows is backed by
        the registry (``HKCU\\Software\\CryoSoft\\CryoSoft``). If nothing is
        stored yet, the window is sized to ~90% of the available screen area
        and centered on it.
        """
        settings = app_settings.get_settings()
        saved = settings.value(_GEOMETRY_KEY)
        if saved is not None and self.restoreGeometry(saved) and self._geometry_on_screen():
            return
        # No saved geometry, a restore failure, or geometry that landed
        # off-screen (e.g. saved on a monitor that is no longer attached — the
        # usual cause of a window that "does not appear") all fall back to a
        # centered default sized to the primary screen.
        self._center_on_primary_screen()

    def _geometry_on_screen(self) -> bool:
        """Return True if the window frame overlaps an attached screen enough to see.

        ``restoreGeometry`` reports success even when it places the window on a
        screen that no longer exists, so this guards against an invisible
        window: it requires at least a 100x100 overlap with some screen's
        available area.

        Returns:
            True if a usable portion of the window is on an attached screen.
        """
        frame = self.frameGeometry()
        for screen in QApplication.screens():
            overlap = screen.availableGeometry().intersected(frame)
            if overlap.width() >= 100 and overlap.height() >= 100:
                return True
        return False

    def _center_on_primary_screen(self) -> None:
        """Size the window to ~90% of the primary screen and center it."""
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = int(available.width() * 0.9)
            height = int(available.height() * 0.9)
            self.resize(width, height)
            x = available.x() + (available.width() - width) // 2
            y = available.y() + (available.height() - height) // 2
            self.move(x, y)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Persist session + geometry and detach the log handler on close.

        Session *content* (sample info, procedure params, queue) is auto-saved
        here so reopening restores it. Window/dock *layout* is saved explicitly
        via the View menu's "Save layout" action, by design — not auto-saved.
        Removing the log handler prevents it from writing to the destroyed
        ``QTextEdit`` after the window is gone (RuntimeError on a dead widget).

        Args:
            event: The Qt close event.
        """
        self._save_session()
        logging.getLogger("cryosoft").removeHandler(self._log_handler)
        settings = app_settings.get_settings()
        settings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Layout persistence (restore path)
    # ------------------------------------------------------------------

    def _save_trends(self) -> None:
        """Persist the ordered list of trend panels' selected key and window."""
        ordered = [self._trend_panels[pid] for pid in self._trend_docks]
        data = [
            {"key": panel.selected_key(), "window_s": panel.selected_window_s()}
            for panel in ordered
        ]
        app_settings.get_settings().setValue(_TRENDS_KEY, json.dumps(data))

    def _apply_trend_restore(self, entries: list) -> None:
        """Replace the current trend docks with ones matching saved entries.

        Args:
            entries: Parsed JSON list of ``{"key": ..., "window_s": ...}``
                dicts, already validated to be a non-empty list.
        """
        valid_entries = [e for e in entries if isinstance(e, dict)][:_MAX_TREND_PANELS]
        if not valid_entries:
            return

        for panel_id in list(self._trend_panels.keys()):
            self._remove_trend_panel_widget(panel_id)
        self._default_trend_key_hints.clear()

        for entry in valid_entries:
            panel_id = self._add_trend_panel()
            panel = self._trend_panels[panel_id]

            window_s = entry.get("window_s")
            if isinstance(window_s, (int, float)) and not isinstance(window_s, bool):
                panel.set_selected_window_s(float(window_s))

            key = entry.get("key")
            if isinstance(key, str) and key:
                panel.set_selected_key(key)  # no-op now if history is still empty
                self._pending_trend_keys[panel_id] = key

    def _restore_monitor_state(self) -> None:
        """Restore trend docks and dock layout from QSettings, defensively.

        Called once at the end of ``__init__``, after the UI and menu are
        built. The saved trend count/keys are applied FIRST (recreating the
        matching set of trend docks), then ``dock_host.restoreState()`` is
        attempted — restoreState() needs every dock objectName it references
        to already exist. A missing key, wrong type, corrupt JSON, or a
        ``restoreState()`` failure all silently fall back to the DEFAULT
        layout already built by ``_build_ui()``.
        """
        settings = app_settings.get_settings()

        raw_trends = settings.value(_TRENDS_KEY)
        parsed = None
        if raw_trends:
            try:
                parsed = json.loads(raw_trends)
            except (TypeError, ValueError):
                parsed = None
        if isinstance(parsed, list) and parsed:
            self._apply_trend_restore(parsed)

        dock_state = settings.value(_DOCK_STATE_KEY)
        if dock_state is None:
            return
        try:
            restored = bool(self._dock_host.restoreState(dock_state))
        except (TypeError, ValueError) as exc:
            logger.debug("MonitorWindow: could not restore dock_host state: %s", exc)
            restored = False
        if not restored:
            logger.debug("MonitorWindow: dock_state present but invalid; kept default layout")
