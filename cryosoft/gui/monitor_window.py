# ---
# description: |
#   MonitorWindow: the main CryoSoft window showing live instrument state.
#   Content below the header/banner is a fixed 2x2 quadrant grid, built from
#   nested QSplitters (one horizontal, two vertical): top-left is a scrollable
#   2-column instrument monitor/control list, top-right is a Trends panel
#   whose plots auto-arrange into a ceil(sqrt(N)) grid, bottom-left is Sample
#   Info, and bottom-right is Other Devices / Log behind a QComboBox
#   selector. Splitter boundaries are draggable but nothing can be closed,
#   detached, or floated — replaces the earlier QDockWidget host, which let
#   panels overlap, hide each other, and become glitchy to resize at real
#   window sizes.
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
#   Splits system/level VIs into a 2-column instrument grid (top-left) and
#   measurement plus switch VIs into a compact Other Devices list
#   (bottom-right, behind the selector; switch rows are display-only). Trend
#   plots (1-4, backed by a shared MonitorHistory) live
#   in a QGridLayout whose column count is ceil(sqrt(panel_count)),
#   recomputed on every add/remove. Connects Orchestrator signals for the
#   state-driven status bar, the notification banner (errors/blocked
#   actions), and MonitorHistory recording that feeds the trend plots. Owns
#   ProcedureWindow and opens it lazily via the Procedures menu.
# output: |
#   A QMainWindow that stays open for the lifetime of the application.
# ---

"""MonitorWindow — main CryoSoft monitor window."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import TYPE_CHECKING

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QActionGroup, QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import Station
from cryosoft.gui import app_settings  # import the module (not the function) so tests can monkeypatch the factory
from cryosoft.gui import form_autosave as session_store  # module import keeps save/load monkeypatchable
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.lifecycle_toggle import LifecycleToggleButton
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

# Default key-selection hints applied to the two default trend panels, in
# creation order, once MonitorHistory has keys (first key whose flat name
# contains the hint substring; falls back to the first available key).
_DEFAULT_TREND_KEY_HINTS = ("temperature", "level")

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

    Everything below the header/banner is a fixed 2x2 quadrant grid built
    from nested QSplitters: top-left is a scrollable 2-column instrument
    monitor/control list, top-right is the Trends panel (auto ceil(sqrt(N))
    grid of TrendPlotPanel), bottom-left is Sample Info, and bottom-right is
    Other Devices / Log behind a QComboBox selector. Every splitter boundary
    is draggable; nothing in the grid can be closed, detached, or floated.

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

        # Persistent session *content* (sample metadata, procedure params, run
        # queue) — a second persistence tier separate from the QSettings window
        # state. Loaded here and applied to the fields once they exist; re-saved
        # on close and by the Session menu.
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
        """Build the Procedures menu.

        There is no View menu: every quadrant is always visible and nothing
        can be hidden, so there is nothing to toggle. Trend plots are added
        via the button inside the Trends quadrant itself.
        """
        menu_bar = self.menuBar()

        # Session menu is added first so it sits leftmost (menu order follows
        # addMenu() call order).
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
        switch_vis = [
            n for n in self._station.get_vi_names()
            if self._station.get_vi_type(n) == "switch"
        ]

        # Live-updated switch VI row labels, keyed by VI name and refreshed on
        # every monitor tick by _on_states_updated_switches. Populated in
        # _build_switch_status_row; empty when the station has no switch VIs.
        self._switch_route_labels: dict[str, QLabel] = {}
        self._switch_conn_dots: dict[str, QLabel] = {}
        self._switch_conn_status: dict[str, QLabel] = {}

        # Shared ring-buffer history feeding all Trend plot panels. Qt-free
        # by design (see monitor_history.py), so it is created here rather
        # than inside TrendPlotPanel.
        self._history = MonitorHistory()

        # ── Header ────────────────────────────────────────────────────
        root.addLayout(self._build_header())

        # ── Notification banner (hidden until a warning/error arrives) ─
        self._banner = NotificationBanner()
        root.addWidget(self._banner)

        # ── Trend bookkeeping (populated by _build_default_trend_panels) ──
        self._trend_panels: dict[str, TrendPlotPanel] = {}
        self._trend_series_counter = 0
        # Keys the restore path still wants applied once MonitorHistory has
        # data for them (a fresh panel's Y combo is empty until the first
        # states_updated tick, so set_selected_key() at restore time is a
        # harmless no-op that we retry from _on_states_updated_for_history).
        self._pending_trend_keys: dict[str, str] = {}
        # Same retry pattern for the DEFAULT (non-restored) trend panels'
        # opportunistic temperature/level key selection.
        self._default_trend_key_hints: dict[str, str] = {}

        # ── Fixed 2x2 quadrant grid ──────────────────────────────────
        top_left = self._build_instruments_quadrant()
        top_right = self._build_trends_quadrant()
        bottom_left = self._build_sample_info_quadrant()
        bottom_right = self._build_other_devices_log_quadrant(measurement_vis, switch_vis)

        self._left_splitter = QSplitter(Qt.Orientation.Vertical)
        self._left_splitter.setObjectName("left_splitter")
        self._left_splitter.setChildrenCollapsible(False)
        self._left_splitter.addWidget(top_left)
        self._left_splitter.addWidget(bottom_left)
        self._left_splitter.setSizes([750, 250])

        self._right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._right_splitter.setObjectName("right_splitter")
        self._right_splitter.setChildrenCollapsible(False)
        self._right_splitter.addWidget(top_right)
        self._right_splitter.addWidget(bottom_right)
        self._right_splitter.setSizes([750, 250])

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("main_splitter")
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._left_splitter)
        self._main_splitter.addWidget(self._right_splitter)
        self._main_splitter.setSizes([600, 600])

        root.addWidget(self._main_splitter)

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

    # ------------------------------------------------------------------
    # Quadrant construction
    # ------------------------------------------------------------------

    def _build_instruments_quadrant(self) -> QWidget:
        """Build the top-left quadrant: a scrollable 2-column instrument grid.

        Panels are built once, in config order, and kept in self._panels for
        the lifetime of the window — recreating them would drop their
        Orchestrator signal connections.

        Returns:
            A QWidget containing the title, and a QScrollArea of InstrumentPanels.
        """
        container = QWidget()
        container.setObjectName("instruments_quadrant")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Instruments</b>"))

        self._panels: list[InstrumentPanel] = []
        grid_container = QWidget()
        grid = QGridLayout(grid_container)
        grid.setSpacing(6)
        for idx, vi_name in enumerate(self._system_vi_names):
            vi = self._station._virtual_instruments[vi_name]
            panel = InstrumentPanel(vi_name, vi, self._orchestrator, parent=self)
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

    def _build_trends_quadrant(self) -> QWidget:
        """Build the top-right quadrant: Trends, auto-arranged in a ceil(sqrt(N)) grid.

        Returns:
            A QWidget containing the title/Add button row, and a QScrollArea
            of the trend-panel grid.
        """
        container = QWidget()
        container.setObjectName("trends_quadrant")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("<b>Trends</b>"))
        toolbar.addStretch()
        self._add_trend_btn = QPushButton("Add trend plot")
        self._add_trend_btn.setObjectName("add_trend_btn")
        self._add_trend_btn.setIcon(qta.icon("fa5s.plus", color=TEXT_PRIMARY))
        self._add_trend_btn.setToolTip(f"Add a trend plot (up to {_MAX_TREND_PANELS})")
        self._add_trend_btn.clicked.connect(self._on_trend_add_clicked)
        toolbar.addWidget(self._add_trend_btn)
        outer.addLayout(toolbar)

        self._trends_grid_container = QWidget()
        self._trends_grid = QGridLayout(self._trends_grid_container)
        self._trends_grid.setSpacing(6)

        scroll = QScrollArea()
        scroll.setObjectName("trends_scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._trends_grid_container)
        outer.addWidget(scroll)

        self._build_default_trend_panels()
        self._update_trend_add_action_state()
        return container

    def _build_sample_info_quadrant(self) -> QWidget:
        """Build the bottom-left quadrant: Sample Info.

        Returns:
            A QWidget containing the title and a QScrollArea of the sample-info form.
        """
        container = QWidget()
        container.setObjectName("sample_info_quadrant")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Sample Info</b>"))

        scroll = QScrollArea()
        scroll.setObjectName("sample_info_scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._build_sample_info_section())
        outer.addWidget(scroll)
        return container

    def _build_other_devices_log_quadrant(
        self, measurement_vis: list[str], switch_vis: list[str]
    ) -> QWidget:
        """Build the bottom-right quadrant: Other Devices / Log behind a selector.

        A QComboBox picks which of the two always-built views is visible in
        the QStackedWidget below it — this keeps the quadrant's footprint
        constant regardless of how many measurement VIs a station has,
        rather than showing both stacked and always-visible.

        Args:
            measurement_vis: Names of measurement VIs to display in Other Devices.
            switch_vis: Names of switch/scanner VIs to display (display-only rows).

        Returns:
            A QWidget containing the selector row and the QStackedWidget.
        """
        container = QWidget()
        container.setObjectName("other_devices_log_quadrant")
        outer = QVBoxLayout(container)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)

        selector_row = QHBoxLayout()
        selector_row.addWidget(QLabel("<b>View:</b>"))
        self._devices_log_selector = QComboBox()
        self._devices_log_selector.setObjectName("devices_log_selector")
        self._devices_log_selector.addItems(["Other Devices", "Log"])
        self._devices_log_selector.currentIndexChanged.connect(self._on_devices_log_selector_changed)
        selector_row.addWidget(self._devices_log_selector)
        selector_row.addStretch()
        outer.addLayout(selector_row)

        self._devices_log_stack = QStackedWidget()
        self._devices_log_stack.setObjectName("devices_log_stack")

        other_devices_scroll = QScrollArea()
        other_devices_scroll.setObjectName("other_devices_scroll")
        other_devices_scroll.setWidgetResizable(True)
        other_devices_scroll.setWidget(
            self._build_other_devices_section(measurement_vis, switch_vis)
        )
        self._devices_log_stack.addWidget(other_devices_scroll)

        self._devices_log_stack.addWidget(self._build_log_section())

        outer.addWidget(self._devices_log_stack)
        return container

    def _build_sample_info_section(self) -> QWidget:
        """Build the sample-info form (session-level metadata).

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

    def _build_other_devices_section(
        self, measurement_vis: list[str], switch_vis: list[str]
    ) -> QWidget:
        """Build the compact Other Devices content: one row per measurement/switch VI.

        Each measurement row is name + connection dot/status + a Check button
        and a LifecycleToggleButton; each switch row is the display-only
        analogue (name + display label + connection dot/status + live active
        route, no buttons). Rows stack vertically for up to three VIs total; a
        tight 3-column grid is used beyond that so the quadrant's natural height
        stays small regardless of how many devices a station has.

        Args:
            measurement_vis: Names of measurement VIs to display.
            switch_vis: Names of switch/scanner VIs to display (display-only).

        Returns:
            A QWidget holding one compact status row per device (or a
            placeholder label if there are none).
        """
        container = QWidget()

        rows = [self._build_device_status_row(n) for n in measurement_vis]
        rows += [self._build_switch_status_row(n) for n in switch_vis]

        if not rows:
            layout = QVBoxLayout(container)
            layout.addWidget(QLabel("No other devices configured."))
            return container

        if len(rows) > 3:
            grid = QGridLayout(container)
            grid.setSpacing(6)
            columns = 3
            for idx, row_widget in enumerate(rows):
                row, col = divmod(idx, columns)
                grid.addWidget(row_widget, row, col)
        else:
            vlay = QVBoxLayout(container)
            vlay.setSpacing(4)
            vlay.setContentsMargins(6, 6, 6, 6)
            for row_widget in rows:
                vlay.addWidget(row_widget)
            vlay.addStretch()

        return container

    def _build_device_status_row(self, vi_name: str) -> QWidget:
        """Build one compact connection-check row for a measurement VI.

        A single ~32-40 px row: coloured dot + status text, then a small
        icon Check button and a LifecycleToggleButton.

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

        lifecycle = LifecycleToggleButton(
            vi_name,
            lambda action, n=vi_name: self._orchestrator.submit_vi_action(n, action),
            parent=row_widget,
        )

        def _on_action_succeeded(v: str, m: str, _lc=lifecycle, _n=vi_name) -> None:
            if v != _n:
                return
            if m == "initiate":
                _lc.set_initiated(True)
            elif m == "standby":
                _lc.set_initiated(False)

        self._orchestrator.action_succeeded.connect(_on_action_succeeded)

        row.addWidget(check_btn)
        row.addWidget(lifecycle)

        return row_widget

    def _build_switch_status_row(self, vi_name: str) -> QWidget:
        """Build one display-only status row for a switch/scanner VI.

        Same visual house style as :meth:`_build_device_status_row` (connection
        dot + status text), but with no Check button and no lifecycle toggle —
        a switch is monitored, not driven, from this section. Adds the VI's
        ``display_label`` (e.g. "Scanner (mux)") and a live active-route label
        that both refresh on the monitor tick via
        :meth:`_on_states_updated_switches`.

        Args:
            vi_name: Registered switch VI name (e.g. ``"switch_matrix"``).

        Returns:
            A QWidget containing the assembled display-only row.
        """
        row_widget = QWidget()
        row_widget.setObjectName(f"{vi_name}_switch_row")
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(8)

        # Connection dot: styled via the dynamic 'status' QSS property, same as
        # the measurement rows. Refreshed live from the state cache, so it
        # starts "unknown" until the first tick arrives.
        dot = QLabel("●")
        dot.setObjectName(f"{vi_name}_conn_dot")
        dot.setProperty("class", "conn_dot")
        dot.setProperty("status", "unknown")
        row.addWidget(dot)

        name_lbl = QLabel(vi_name)
        row.addWidget(name_lbl)

        label_lbl = QLabel(self._station.measurement_label(vi_name))
        label_lbl.setObjectName(f"{vi_name}_display_label")
        label_lbl.setProperty("class", "secondary_label")
        row.addWidget(label_lbl)

        row.addStretch()

        status_lbl = QLabel("Unknown")
        status_lbl.setObjectName(f"{vi_name}_conn_status")
        status_lbl.setProperty("class", "secondary_label")
        row.addWidget(status_lbl)

        route_lbl = QLabel("Route: —")
        route_lbl.setObjectName(f"{vi_name}_active_route")
        row.addWidget(route_lbl)

        self._switch_conn_dots[vi_name] = dot
        self._switch_conn_status[vi_name] = status_lbl
        self._switch_route_labels[vi_name] = route_lbl

        return row_widget

    def _build_log_section(self) -> QTextEdit:
        """Build the real-time log display widget.

        Returns:
            A read-only QTextEdit (objectName ``log_panel``).
        """
        self._log_widget = QTextEdit()
        self._log_widget.setObjectName("log_panel")
        self._log_widget.setReadOnly(True)
        self._log_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        return self._log_widget

    def _on_devices_log_selector_changed(self, index: int) -> None:
        """Switch the bottom-right quadrant between Other Devices and Log.

        Args:
            index: The selector's new current index (0 = Other Devices, 1 = Log).
        """
        self._devices_log_stack.setCurrentIndex(index)

    # ------------------------------------------------------------------
    # Trend panels
    # ------------------------------------------------------------------

    def _build_default_trend_panels(self) -> None:
        """(Re)create exactly the default number of trend panels.

        Replaces any existing trend panels, then creates
        ``_DEFAULT_TREND_PANEL_COUNT`` fresh ones, with opportunistic
        temperature/level default-key hints (applied once MonitorHistory has
        data — see ``_on_states_updated_for_history``).
        """
        for panel_id in list(self._trend_panels.keys()):
            self._remove_trend_panel_widget(panel_id)
        self._trend_series_counter = 0
        self._default_trend_key_hints.clear()

        for _ in range(_DEFAULT_TREND_PANEL_COUNT):
            self._add_trend_panel()

        for panel_id, hint in zip(self._trend_panels.keys(), _DEFAULT_TREND_KEY_HINTS):
            self._default_trend_key_hints[panel_id] = hint

    def _create_trend_panel(self) -> tuple[str, TrendPlotPanel]:
        """Create and register a new TrendPlotPanel.

        Registers the panel in ``self._trend_panels`` but does NOT place it
        in the grid — callers call ``_relayout_trend_grid()``.

        Returns:
            ``(panel_id, panel)`` for the caller.
        """
        panel_id = f"trend_{self._next_trend_panel_index()}"
        panel = TrendPlotPanel(
            self._history, panel_id, series_index=self._trend_series_counter, parent=self
        )
        self._trend_series_counter += 1
        panel.remove_requested.connect(self._on_trend_remove_requested)

        self._trend_panels[panel_id] = panel
        return panel_id, panel

    def _relayout_trend_grid(self) -> None:
        """Rebuild the trend grid: current panels arranged in a ceil(sqrt(N)) grid.

        Recomputed from scratch on every add/remove — cheap at N<=4 and
        avoids tracking incremental grid positions separately from
        ``self._trend_panels``' insertion order.
        """
        grid = self._trends_grid
        while grid.count():
            grid.takeAt(0)  # widgets are reparented into the grid on addWidget; not deleted here

        panels = list(self._trend_panels.values())
        if not panels:
            return
        columns = math.ceil(math.sqrt(len(panels)))
        for idx, panel in enumerate(panels):
            row, col = divmod(idx, columns)
            grid.addWidget(panel, row, col)

    def _add_trend_panel(self) -> str:
        """Create, place, and grid-arrange a new trend panel.

        Returns:
            The new panel's ``panel_id``.
        """
        panel_id, _panel = self._create_trend_panel()
        self._relayout_trend_grid()
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
        """Add a trend panel via the quadrant's Add button, up to the cap."""
        if len(self._trend_panels) >= _MAX_TREND_PANELS:
            return
        self._add_trend_panel()

    def _on_trend_remove_requested(self, panel_id: str) -> None:
        """Remove a trend panel, never dropping below the minimum.

        Args:
            panel_id: The panel_id echoed back by TrendPlotPanel.remove_requested.
        """
        if len(self._trend_panels) <= _MIN_TREND_PANELS:
            return
        self._remove_trend_panel_widget(panel_id)
        self._relayout_trend_grid()
        self._update_trend_add_action_state()

    def _remove_trend_panel_widget(self, panel_id: str) -> None:
        """Unconditionally drop a trend panel's widget and bookkeeping.

        Does not relayout the grid — callers that need the grid consistent
        immediately after (as opposed to before a batch of further adds)
        call ``_relayout_trend_grid()`` themselves.

        Args:
            panel_id: The panel_id to remove. No-op if not present.
        """
        panel = self._trend_panels.pop(panel_id, None)
        if panel is not None:
            self._trends_grid.removeWidget(panel)
            panel.setParent(None)
            panel.deleteLater()
        self._pending_trend_keys.pop(panel_id, None)
        self._default_trend_key_hints.pop(panel_id, None)

    def _update_trend_add_action_state(self) -> None:
        """Enable/disable the "Add trend plot" button based on the current panel count."""
        self._add_trend_btn.setEnabled(len(self._trend_panels) < _MAX_TREND_PANELS)

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
        """Persist the current session to disk, tolerating write failures."""
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
        """Switch the active config to ``path`` after a warning, then restart."""
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
        """Persist the config at ``path`` as active, save the session, restart."""
        self._save_session()
        entry = self._catalog.get_by_path(path) if self._catalog is not None else None
        if entry is not None:
            app_settings.set_config_active(entry.name, entry.source)
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
        self._orchestrator.action_failed.connect(self._on_action_failed)
        self._orchestrator.action_succeeded.connect(self._on_action_confirmed)
        # Separate from InstrumentPanel's own states_updated connections
        # (each panel connects itself in its constructor) — this slot only
        # feeds MonitorHistory and the Trend plots.
        self._orchestrator.states_updated.connect(self._on_states_updated_for_history)
        # Refresh the display-only switch/scanner rows (connection + active
        # route) from the same per-tick snapshot. No-op when there are no
        # switch VIs (the label dicts are empty).
        self._orchestrator.states_updated.connect(self._on_states_updated_switches)

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

    def _on_states_updated_switches(self, state: dict) -> None:
        """Refresh the display-only switch rows from a per-tick state snapshot.

        Updates each switch VI's connection dot/status and live active-route
        label. Connection is derived the same way the rest of the GUI reads
        it: a ``_disconnected`` flag in the snapshot means "not reachable",
        otherwise a present snapshot means "connected". The active route shows
        the route name, or an em dash when no route is closed.

        Args:
            state: ``{vi_name: {field: value, ...}}`` from the Orchestrator.
        """
        for vi_name, route_lbl in self._switch_route_labels.items():
            vi_state = state.get(vi_name)
            if vi_state is None:
                continue

            route = vi_state.get("active_route", "")
            route_lbl.setText(f"Route: {route}" if route else "Route: —")

            dot = self._switch_conn_dots[vi_name]
            status_lbl = self._switch_conn_status[vi_name]
            if vi_state.get("_disconnected"):
                new_status, text = "disconnected", "Not reachable"
            else:
                new_status, text = "connected", "Connected"
            if dot.property("status") != new_status:
                dot.setProperty("status", new_status)
                # Qt re-evaluates property-based QSS selectors only after an
                # unpolish/polish cycle (same pattern the Check button uses).
                dot.style().unpolish(dot)
                dot.style().polish(dot)
            status_lbl.setText(text)

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
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = int(available.width() * 0.9)
            height = int(available.height() * 0.9)
            self.resize(width, height)
            x = available.x() + (available.width() - width) // 2
            y = available.y() + (available.height() - height) // 2
            self.move(x, y)

    def _geometry_on_screen(self) -> bool:
        """Return True if the window frame overlaps an attached screen enough to see.

        ``restoreGeometry`` reports success even when it places the window on a
        screen that no longer exists, so this guards against an invisible
        window: it requires at least a 100x100 overlap with some screen's
        available area.
        """
        frame = self.frameGeometry()
        for screen in QApplication.screens():
            overlap = screen.availableGeometry().intersected(frame)
            if overlap.width() >= 100 and overlap.height() >= 100:
                return True
        return False

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Detach the log handler and persist geometry/splitter/trend state before closing.

        Removing the handler prevents it from writing to the destroyed
        ``QTextEdit`` after the window is gone (RuntimeError on a dead widget).
        Splitter proportions and trend selections are saved automatically
        here (no separate "Save layout" action, unlike the old dock-state
        save/restore) — there is nothing else for the user to arrange since
        panels can't be hidden, closed, or moved out of their quadrant.

        Args:
            event: The Qt close event.
        """
        self._save_session()
        logging.getLogger("cryosoft").removeHandler(self._log_handler)
        settings = app_settings.get_settings()
        settings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        settings.setValue(_MAIN_SPLITTER_KEY, self._main_splitter.saveState())
        settings.setValue(_LEFT_SPLITTER_KEY, self._left_splitter.saveState())
        settings.setValue(_RIGHT_SPLITTER_KEY, self._right_splitter.saveState())
        self._save_trends()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Layout persistence (restore path)
    # ------------------------------------------------------------------

    def _save_trends(self) -> None:
        """Persist the ordered list of trend panels' selected key and window."""
        data = [
            {"key": panel.selected_key(), "window_s": panel.selected_window_s()}
            for panel in self._trend_panels.values()
        ]
        app_settings.get_settings().setValue(_TRENDS_KEY, json.dumps(data))

    def _apply_trend_restore(self, entries: list) -> None:
        """Replace the current trend panels with ones matching saved entries.

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

        self._restore_splitter_state()
