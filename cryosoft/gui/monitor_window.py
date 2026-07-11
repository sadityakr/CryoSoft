# ---
# description: |
#   MonitorWindow: the main CryoSoft window showing live instrument state.
#   System/level VI panels sit in a resizable splitter grid (configurable
#   column count). A Trends section shows live time-series plots fed from a
#   MonitorHistory ring buffer. Measurement VIs sit in a compact "Other
#   Devices" section. The bottom row is a 50/50 splitter: Log (left) and
#   Sample Info (right). A Procedures menu opens ProcedureWindow.
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
#   Iterates over all VI names in the station, splits them by vi_type into the
#   main grid (system/level) and Other Devices section (measurement). System
#   panels are distributed row-major into a splitter-of-rows whose column
#   count is user-configurable. Connects Orchestrator signals for the
#   state-driven status bar, the notification banner (errors/blocked
#   actions), and MonitorHistory recording that feeds the Trends section.
#   Owns ProcedureWindow and opens it lazily via the Procedures menu.
# output: |
#   A QMainWindow that stays open for the lifetime of the application.
# last_updated: 2026-07-12
# ---

"""MonitorWindow — main CryoSoft monitor window."""

from __future__ import annotations

import json
import logging

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import Station
from cryosoft.gui import app_settings  # import the module (not the function) so tests can monkeypatch the factory
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

logger = logging.getLogger(__name__)

_DEFAULT_GRID_COLUMNS = 2  # default columns in the system VI instrument grid
_GRID_COLUMN_CHOICES = ("2", "3", "4")
_MIN_TREND_PANELS = 1
_MAX_TREND_PANELS = 4
_DEFAULT_TREND_PANEL_COUNT = 2
_LOG_MAX_LINES = 500

# QSettings keys for persisted window/layout state.
_GEOMETRY_KEY = "MonitorWindow/geometry"
_MAIN_SPLITTER_KEY = "MonitorWindow/main_splitter"
_LOG_INFO_SPLITTER_KEY = "MonitorWindow/log_info_splitter"
_GRID_COLUMNS_KEY = "MonitorWindow/grid_columns"
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

    System and level VIs get InstrumentPanels laid out in a splitter grid
    (a vertical QSplitter of rows, each row a horizontal QSplitter) whose
    column count the user can change live via a "Columns" selector. A
    "Trends" section below the grid hosts up to four TrendPlotPanels fed by
    a shared MonitorHistory ring buffer. Measurement VIs appear in a
    separate "Other Devices" section below that. A real-time log panel
    shows the running cryosoft logger output. The Procedures menu opens
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
    ) -> None:
        super().__init__(parent)
        self._station = station
        self._orchestrator = orchestrator
        self._procedure_window = None  # lazily created

        self.setWindowTitle("CryoSoft — Monitor")
        self._restore_geometry()

        self._build_menu()
        self._build_ui()
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

    # ------------------------------------------------------------------
    # Menu bar
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()
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

        system_vis = [
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

        # ── System / level VI grid ─────────────────────────────────────
        # Panels are built once, in config order, and kept in self._panels
        # for the lifetime of the window. Changing the column count only
        # reparents these same instances into new row splitters — recreating
        # them would drop their Orchestrator signal connections.
        self._panels: list[InstrumentPanel] = []
        for vi_name in system_vis:
            vi = self._station._virtual_instruments[vi_name]
            panel = InstrumentPanel(vi_name, vi, self._orchestrator, parent=self)
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self._panels.append(panel)

        self._grid_columns = _DEFAULT_GRID_COLUMNS
        self._grid_vsplitter = self._build_grid_splitter(self._grid_columns)

        # ── VI grid scroll area ────────────────────────────────────────
        # Only the instrument grid scrolls when there are more panels than fit;
        # the rest of the window keeps its layout. setWidgetResizable(True) lets
        # the grid expand to fill the viewport when there is room. The
        # splitter-of-rows is set directly as the scroll area's widget (a
        # QSplitter is itself a QWidget, so no extra wrapping container).
        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setWidget(self._grid_vsplitter)
        self._grid_scroll.setMinimumHeight(200)

        # ── Trends section ───────────────────────────────────────────
        trends_section = self._build_trends_section()
        trends_section.setMinimumHeight(200)

        # ── Lower section ──────────────────────────────────────────────
        lower_widget = QWidget()
        lower_widget.setMinimumHeight(380)
        lower_layout = QVBoxLayout(lower_widget)
        lower_layout.setSpacing(6)
        lower_layout.setContentsMargins(0, 0, 0, 0)

        if measurement_vis:
            lower_layout.addWidget(self._build_other_devices_section(measurement_vis))

        log_section = self._build_log_section()
        sample_info_section = self._build_sample_info_section()
        # Keep both panes readable; setChildrenCollapsible(False) stops a drag
        # from crushing either pane to zero width (the reported "collapse" bug).
        log_section.setMinimumWidth(200)
        sample_info_section.setMinimumWidth(200)
        self._log_info_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._log_info_splitter.setObjectName("log_info_splitter")
        self._log_info_splitter.setChildrenCollapsible(False)
        self._log_info_splitter.addWidget(log_section)
        self._log_info_splitter.addWidget(sample_info_section)
        self._log_info_splitter.setStretchFactor(0, 1)
        self._log_info_splitter.setStretchFactor(1, 1)
        lower_layout.addWidget(self._log_info_splitter)

        # ── Vertical splitter: VI grid / Trends / lower section ─────────
        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.setObjectName("main_splitter")
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._grid_scroll)
        self._main_splitter.addWidget(trends_section)
        self._main_splitter.addWidget(lower_widget)
        self._main_splitter.setSizes([420, 260, 320])
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

    def _build_grid_splitter(self, columns: int) -> QSplitter:
        """Build a vertical splitter of horizontal row splitters for the VI grid.

        Distributes ``self._panels`` row-major, up to ``columns`` panels per
        row, preserving config order. Reparents the existing panel instances
        (does not create new ones).

        Args:
            columns: Number of panels per row.

        Returns:
            The assembled vertical QSplitter (objectName ``grid_vsplitter``),
            containing one row QSplitter (objectName ``grid_row_splitter_{i}``)
            per row of panels.
        """
        vsplitter = QSplitter(Qt.Orientation.Vertical)
        vsplitter.setObjectName("grid_vsplitter")
        vsplitter.setChildrenCollapsible(False)

        for row_idx, start in enumerate(range(0, len(self._panels), columns)):
            row_splitter = QSplitter(Qt.Orientation.Horizontal)
            row_splitter.setObjectName(f"grid_row_splitter_{row_idx}")
            row_splitter.setChildrenCollapsible(False)
            for panel in self._panels[start:start + columns]:
                # QSplitter.addWidget() reparents the widget: a Qt widget can
                # only have one parent, so this automatically detaches the
                # panel from whatever splitter (or the window) held it before.
                row_splitter.addWidget(panel)
            vsplitter.addWidget(row_splitter)

        return vsplitter

    def _reflow_grid(self, columns: int) -> None:
        """Rebuild the grid splitter for a new column count, reparenting panels.

        Args:
            columns: New number of panels per row.
        """
        new_vsplitter = self._build_grid_splitter(columns)
        # QScrollArea.setWidget() takes ownership of the new widget and
        # schedules the old one for deletion; the panels were already
        # reparented out of it by _build_grid_splitter(), so nothing of
        # value is lost.
        self._grid_scroll.setWidget(new_vsplitter)
        self._grid_vsplitter = new_vsplitter

    def _set_grid_columns(self, columns: int, save_previous: bool = True) -> None:
        """Change the grid column count, reflowing panels and swapping splitter state.

        Args:
            columns: New column count (2, 3, or 4).
            save_previous: If True, save the current density's splitter sizes
                before reflowing (so returning to it later restores them).
        """
        if save_previous:
            self._save_grid_density_splitters(self._grid_columns)
        self._reflow_grid(columns)
        self._grid_columns = columns
        self._restore_grid_density_splitters(columns)

    def _on_columns_changed(self, text: str) -> None:
        """Handle the Columns selector changing: reflow and persist the choice.

        Args:
            text: The selector's new text ("2", "3", or "4").
        """
        try:
            columns = int(text)
        except ValueError:
            return
        if columns == self._grid_columns:
            return
        self._set_grid_columns(columns, save_previous=True)
        app_settings.get_settings().setValue(_GRID_COLUMNS_KEY, self._grid_columns)

    # ------------------------------------------------------------------
    # Trends section
    # ------------------------------------------------------------------

    def _build_trends_section(self) -> QGroupBox:
        """Build the Trends section: an add button and a splitter of TrendPlotPanels.

        Returns:
            A QGroupBox (objectName ``trends_section``) with a header row and
            a horizontal splitter (objectName ``trends_splitter``) holding
            the default set of TrendPlotPanels.
        """
        box = QGroupBox("Trends")
        box.setObjectName("trends_section")
        vlay = QVBoxLayout(box)

        header = QHBoxLayout()
        header.addWidget(QLabel("Live trend plots of instrument readings over time."))
        header.addStretch()
        self._trend_add_button = QPushButton()
        self._trend_add_button.setObjectName("trend_add_button")
        self._trend_add_button.setIcon(qta.icon("fa5s.plus", color=TEXT_PRIMARY))
        self._trend_add_button.setToolTip(f"Add a trend plot (up to {_MAX_TREND_PANELS})")
        self._trend_add_button.clicked.connect(self._on_trend_add_clicked)
        header.addWidget(self._trend_add_button)
        vlay.addLayout(header)

        self._trends_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._trends_splitter.setObjectName("trends_splitter")
        self._trends_splitter.setChildrenCollapsible(False)
        vlay.addWidget(self._trends_splitter)

        self._trend_panels: dict[str, TrendPlotPanel] = {}
        self._trend_series_counter = 0
        # Keys the restore path still wants applied once MonitorHistory has
        # data for them (a fresh panel's Y combo is empty until the first
        # states_updated tick, so set_selected_key() at restore time is a
        # harmless no-op that we retry from _on_states_updated_for_history).
        self._pending_trend_keys: dict[str, str] = {}
        for _ in range(_DEFAULT_TREND_PANEL_COUNT):
            self._add_trend_panel()

        return box

    def _add_trend_panel(self) -> str:
        """Create and host a new TrendPlotPanel.

        Returns:
            The new panel's ``panel_id``.
        """
        panel_id = f"trend_{self._next_trend_panel_index()}"
        panel = TrendPlotPanel(
            self._history, panel_id, series_index=self._trend_series_counter, parent=self
        )
        self._trend_series_counter += 1
        panel.remove_requested.connect(self._on_trend_remove_requested)
        self._trends_splitter.addWidget(panel)
        self._trend_panels[panel_id] = panel
        self._update_trend_add_button_state()
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
        """Add a trend panel, up to the cap."""
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
        self._update_trend_add_button_state()

    def _remove_trend_panel_widget(self, panel_id: str) -> None:
        """Unconditionally drop a trend panel's widget and bookkeeping.

        Args:
            panel_id: The panel_id to remove. No-op if not present.
        """
        panel = self._trend_panels.pop(panel_id, None)
        if panel is None:
            return
        panel.setParent(None)
        panel.deleteLater()
        self._pending_trend_keys.pop(panel_id, None)

    def _update_trend_add_button_state(self) -> None:
        """Enable/disable the add button based on the current panel count."""
        self._trend_add_button.setEnabled(len(self._trend_panels) < _MAX_TREND_PANELS)

    def _build_header(self) -> QHBoxLayout:
        """Build the top toolbar with title and global action buttons.

        Returns:
            A QHBoxLayout containing the header widgets.
        """
        row = QHBoxLayout()

        title = QLabel("<b>CryoSoft</b>  — Instrument Monitor")
        row.addWidget(title)

        columns_label = QLabel("Columns:")
        row.addWidget(columns_label)
        self._columns_selector = QComboBox()
        self._columns_selector.setObjectName("grid_columns_selector")
        self._columns_selector.addItems(list(_GRID_COLUMN_CHOICES))
        self._columns_selector.setCurrentText(str(_DEFAULT_GRID_COLUMNS))
        self._columns_selector.setToolTip("Number of columns in the instrument grid above")
        self._columns_selector.currentTextChanged.connect(self._on_columns_changed)
        row.addWidget(self._columns_selector)

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

    def _build_sample_info_section(self) -> QGroupBox:
        """Build the sample-info group box (session-level metadata).

        Returns:
            A QGroupBox with name, ID, comments, and data-dir fields.
        """
        box = QGroupBox("Sample Info")
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

    def _build_other_devices_section(self, vi_names: list[str]) -> QGroupBox:
        """Build the Other Devices section — connection status cards for measurement VIs.

        Shows only what is connected and lifecycle (Initiate/Standby).
        Configuration is always handled by procedures, not the monitor.

        Args:
            vi_names: Names of measurement VIs to display.

        Returns:
            A QGroupBox with one compact status card per measurement VI.
        """
        box = QGroupBox("Other Devices")
        h_layout = QHBoxLayout(box)
        h_layout.setSpacing(8)

        for vi_name in vi_names:
            h_layout.addWidget(self._build_device_status_card(vi_name))

        h_layout.addStretch()
        return box

    def _build_device_status_card(self, vi_name: str) -> QGroupBox:
        """Build a connection-check status card for a measurement VI.

        Shows a coloured dot + status text, a "Check" button that sends an
        IDN query, and Initiate/Standby lifecycle buttons.

        Args:
            vi_name: Registered VI name (e.g. ``"keithley_delta_mode"``).

        Returns:
            A compact QGroupBox with connection indicator and lifecycle buttons.
        """
        box = QGroupBox(vi_name)
        vlay = QVBoxLayout(box)

        # ── Connection indicator row ───────────────────────────────────
        conn_row = QHBoxLayout()
        dot = QLabel("●")
        dot.setStyleSheet("color: gray; font-size: 16px;")
        dot.setObjectName(f"{vi_name}_conn_dot")
        status_lbl = QLabel("Unknown")
        status_lbl.setObjectName(f"{vi_name}_conn_status")
        status_lbl.setProperty("class", "secondary_label")
        check_btn = QPushButton("Check")
        check_btn.setObjectName(f"{vi_name}_check_btn")
        check_btn.setIcon(qta.icon("fa5s.plug", color=TEXT_PRIMARY))
        check_btn.setToolTip("Send an identity query to test the connection")
        # No max width: the size hint must fit icon + text ("Check" was
        # truncated by the old fixed cap once the icon was added).

        vi = self._station._virtual_instruments[vi_name]

        def _on_check(checked: bool = False, _vi=vi, _dot=dot, _lbl=status_lbl) -> None:
            try:
                ok = _vi.ping()
            except Exception:
                ok = False
            if ok:
                _dot.setStyleSheet("color: #4ec94e; font-size: 16px;")
                _lbl.setText("Connected")
            else:
                _dot.setStyleSheet("color: #e74c3c; font-size: 16px;")
                _lbl.setText("Not reachable")

        check_btn.clicked.connect(_on_check)
        conn_row.addWidget(dot)
        conn_row.addWidget(status_lbl)
        conn_row.addStretch()
        conn_row.addWidget(check_btn)
        vlay.addLayout(conn_row)

        # ── Lifecycle buttons ──────────────────────────────────────────
        btn_row = QHBoxLayout()
        initiate_btn = QPushButton("Initiate")
        initiate_btn.setObjectName(f"{vi_name}_initiate_btn")
        initiate_btn.setIcon(qta.icon("fa5s.play", color=TEXT_PRIMARY))
        initiate_btn.setToolTip("Bring this instrument to its operating state")
        initiate_btn.clicked.connect(
            lambda checked=False, n=vi_name: self._orchestrator.submit_vi_action(n, "initiate")
        )
        standby_btn = QPushButton("Standby")
        standby_btn.setObjectName(f"{vi_name}_standby_btn")
        standby_btn.setIcon(qta.icon("fa5s.power-off", color=TEXT_PRIMARY))
        standby_btn.setToolTip("Return this instrument to a safe standby state")
        standby_btn.clicked.connect(
            lambda checked=False, n=vi_name: self._orchestrator.submit_vi_action(n, "standby")
        )
        btn_row.addWidget(initiate_btn)
        btn_row.addWidget(standby_btn)
        btn_row.addStretch()
        vlay.addLayout(btn_row)

        return box

    def _build_log_section(self) -> QGroupBox:
        """Build the real-time log display panel.

        Returns:
            A QGroupBox containing a read-only QTextEdit with dark background.
        """
        box = QGroupBox("Log")
        vlay = QVBoxLayout(box)
        self._log_widget = QTextEdit()
        self._log_widget.setObjectName("log_panel")
        self._log_widget.setReadOnly(True)
        self._log_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        vlay.addWidget(self._log_widget)
        return box

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
        """Restore the saved window geometry, or size to a fraction of the screen.

        Geometry is persisted with ``QSettings``, which on Windows is backed by
        the registry (``HKCU\\Software\\CryoSoft\\CryoSoft``). If nothing is
        stored yet, the window is sized to ~70% of the available screen area.
        """
        settings = app_settings.get_settings()
        saved = settings.value(_GEOMETRY_KEY)
        if saved is not None and self.restoreGeometry(saved):
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(int(available.width() * 0.7), int(available.height() * 0.7))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Detach the log handler and persist geometry/layout before the window closes.

        Removing the handler prevents it from writing to the destroyed
        ``QTextEdit`` after the window is gone (RuntimeError on a dead widget).

        Args:
            event: The Qt close event.
        """
        logging.getLogger("cryosoft").removeHandler(self._log_handler)
        settings = app_settings.get_settings()
        settings.setValue(_GEOMETRY_KEY, self.saveGeometry())
        # saveState() serializes a splitter's pane sizes (and collapsed
        # state) into an opaque QByteArray; restoreState() on the matching
        # splitter later reproduces the same sizes.
        settings.setValue(_MAIN_SPLITTER_KEY, self._main_splitter.saveState())
        settings.setValue(_LOG_INFO_SPLITTER_KEY, self._log_info_splitter.saveState())
        settings.setValue(_GRID_COLUMNS_KEY, self._grid_columns)
        self._save_grid_density_splitters(self._grid_columns)
        self._save_trends()
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Layout persistence (grid density + trends)
    # ------------------------------------------------------------------

    def _save_grid_density_splitters(self, columns: int) -> None:
        """Persist the current grid splitter sizes under this column count's key.

        Args:
            columns: The column count these sizes belong to.
        """
        settings = app_settings.get_settings()
        settings.setValue(f"MonitorWindow/grid/cols{columns}/vsplitter", self._grid_vsplitter.saveState())
        for i in range(self._grid_vsplitter.count()):
            row_splitter = self._grid_vsplitter.widget(i)
            settings.setValue(f"MonitorWindow/grid/cols{columns}/row{i}", row_splitter.saveState())

    def _restore_grid_density_splitters(self, columns: int) -> None:
        """Restore grid splitter sizes previously saved for this column count.

        Silently does nothing for a density that was never saved (a fresh
        density falls back to the splitters' natural equal-share sizes).

        Args:
            columns: The column count to restore sizes for.
        """
        settings = app_settings.get_settings()
        vstate = settings.value(f"MonitorWindow/grid/cols{columns}/vsplitter")
        if vstate is not None:
            try:
                self._grid_vsplitter.restoreState(vstate)
            except (TypeError, ValueError) as exc:
                logger.debug("MonitorWindow: could not restore grid_vsplitter state: %s", exc)
        for i in range(self._grid_vsplitter.count()):
            rstate = settings.value(f"MonitorWindow/grid/cols{columns}/row{i}")
            if rstate is None:
                continue
            row_splitter = self._grid_vsplitter.widget(i)
            try:
                row_splitter.restoreState(rstate)
            except (TypeError, ValueError) as exc:
                logger.debug("MonitorWindow: could not restore grid_row_splitter %d state: %s", i, exc)

    def _save_trends(self) -> None:
        """Persist the ordered list of trend panels' selected key and window."""
        ordered = [self._trends_splitter.widget(i) for i in range(self._trends_splitter.count())]
        data = [
            {"key": panel.selected_key(), "window_s": panel.selected_window_s()}
            for panel in ordered
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

        self._update_trend_add_button_state()

    def _restore_monitor_state(self) -> None:
        """Restore splitter sizes, grid column count, and trend panels from QSettings.

        Called once at the end of ``__init__``, after the UI is built. Every
        stored value is read defensively: a missing key, wrong type, or
        corrupt JSON silently falls back to the default already built by
        ``_build_ui``.
        """
        settings = app_settings.get_settings()

        main_state = settings.value(_MAIN_SPLITTER_KEY)
        if main_state is not None:
            try:
                self._main_splitter.restoreState(main_state)
            except (TypeError, ValueError) as exc:
                logger.debug("MonitorWindow: could not restore main_splitter state: %s", exc)

        log_info_state = settings.value(_LOG_INFO_SPLITTER_KEY)
        if log_info_state is not None:
            try:
                self._log_info_splitter.restoreState(log_info_state)
            except (TypeError, ValueError) as exc:
                logger.debug("MonitorWindow: could not restore log_info_splitter state: %s", exc)

        try:
            saved_columns = int(settings.value(_GRID_COLUMNS_KEY, _DEFAULT_GRID_COLUMNS))
        except (TypeError, ValueError):
            saved_columns = _DEFAULT_GRID_COLUMNS
        if str(saved_columns) not in _GRID_COLUMN_CHOICES:
            saved_columns = _DEFAULT_GRID_COLUMNS

        if saved_columns != self._grid_columns:
            self._set_grid_columns(saved_columns, save_previous=False)
        else:
            self._restore_grid_density_splitters(self._grid_columns)

        self._columns_selector.blockSignals(True)
        self._columns_selector.setCurrentText(str(self._grid_columns))
        self._columns_selector.blockSignals(False)

        raw_trends = settings.value(_TRENDS_KEY)
        parsed = None
        if raw_trends:
            try:
                parsed = json.loads(raw_trends)
            except (TypeError, ValueError):
                parsed = None
        if isinstance(parsed, list) and parsed:
            self._apply_trend_restore(parsed)
