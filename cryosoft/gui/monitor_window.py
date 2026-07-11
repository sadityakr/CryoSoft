# ---
# description: |
#   MonitorWindow: the main CryoSoft window showing live instrument state.
#   System/level VI panels are always visible (no scroll). Measurement VIs sit
#   in a compact "Other Devices" section. The bottom row is a 50/50 splitter:
#   Log (left) and Sample Info (right). A Procedures menu opens ProcedureWindow.
# entry_point: Not run directly. Instantiated in main.py.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.instrument_panel (InstrumentPanel)
# input: |
#   Station instance and Orchestrator instance.
# process: |
#   Iterates over all VI names in the station, splits them by vi_type into the
#   main grid (system/level) and Other Devices section (measurement). Connects
#   Orchestrator signals for status bar and error dialogs. Owns ProcedureWindow
#   and opens it lazily via the Procedures menu.
# output: |
#   A QMainWindow that stays open for the lifetime of the application.
# last_updated: 2026-04-18
# ---

"""MonitorWindow — main CryoSoft monitor window."""

from __future__ import annotations

import logging

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QAction, QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import Station
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.theme import (
    BTN_CLASS_PRIMARY,
    BTN_CLASS_SECONDARY,
    LOG_CRITICAL,
    LOG_DEBUG,
    LOG_ERROR,
    LOG_INFO,
    LOG_WARNING,
)

logger = logging.getLogger(__name__)

_COLUMNS = 2  # columns in the system VI instrument grid
_LOG_MAX_LINES = 500
_GEOMETRY_KEY = "MonitorWindow/geometry"  # QSettings key for saved window geometry


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
            colour = self._LEVEL_COLOURS.get(record.levelno, "#d4d4d4")
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

    System and level VIs get InstrumentPanels in a scrollable 2-column grid.
    Measurement VIs appear in a separate "Other Devices" section below.
    A real-time log panel shows the running cryosoft logger output.
    The Procedures menu opens ProcedureWindow lazily.

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

        # ── Header ────────────────────────────────────────────────────
        root.addLayout(self._build_header())

        # ── System / level VI grid ─────────────────────────────────────
        grid_container = QWidget()
        self._grid = QGridLayout(grid_container)
        self._grid.setSpacing(8)
        n_rows = (len(system_vis) + _COLUMNS - 1) // _COLUMNS
        for c in range(_COLUMNS):
            self._grid.setColumnStretch(c, 1)
        for r in range(n_rows):
            self._grid.setRowStretch(r, 1)

        for idx, vi_name in enumerate(system_vis):
            vi = self._station._virtual_instruments[vi_name]
            panel = InstrumentPanel(vi_name, vi, self._orchestrator, parent=self)
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            row, col = divmod(idx, _COLUMNS)
            self._grid.addWidget(panel, row, col)

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
        log_info_splitter = QSplitter(Qt.Orientation.Horizontal)
        log_info_splitter.setChildrenCollapsible(False)
        log_info_splitter.addWidget(log_section)
        log_info_splitter.addWidget(sample_info_section)
        log_info_splitter.setStretchFactor(0, 1)
        log_info_splitter.setStretchFactor(1, 1)
        lower_layout.addWidget(log_info_splitter)

        # ── VI grid scroll area ────────────────────────────────────────
        # Only the instrument grid scrolls when there are more panels than fit;
        # the rest of the window keeps its layout. setWidgetResizable(True) lets
        # the grid expand to fill the viewport when there is room.
        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setWidget(grid_container)
        self._grid_scroll.setMinimumHeight(200)

        # ── Vertical splitter between VI grid and lower section ────────
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setChildrenCollapsible(False)
        main_splitter.addWidget(self._grid_scroll)
        main_splitter.addWidget(lower_widget)
        main_splitter.setSizes([500, 400])
        root.addWidget(main_splitter)

        # ── Content widget is the central widget directly (no outer scroll) ──
        self.setCentralWidget(content_widget)

        # ── Status bar ────────────────────────────────────────────────
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._state_label = QLabel("State: IDLE")
        self._status_bar.addWidget(self._state_label)

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
        initiate_all_btn.clicked.connect(
            lambda: self._orchestrator.submit_global_action("initiate_all")
        )

        standby_all_btn = QPushButton("Standby All")
        standby_all_btn.setObjectName("standby_all_btn")
        standby_all_btn.setProperty("class", BTN_CLASS_SECONDARY)
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
        check_btn.setMaximumWidth(60)

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
        initiate_btn.clicked.connect(
            lambda checked=False, n=vi_name: self._orchestrator.submit_vi_action(n, "initiate")
        )
        standby_btn = QPushButton("Standby")
        standby_btn.setObjectName(f"{vi_name}_standby_btn")
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

    def _on_state_changed(self, state_name: str) -> None:
        """Update the status bar label when the Orchestrator state changes.

        Args:
            state_name: The new state name string (e.g. ``"IDLE"``).
        """
        self._state_label.setText(f"State: {state_name}")
        logger.debug("MonitorWindow: orchestrator state → %s", state_name)

    def _on_error(self, message: str) -> None:
        """Show an error dialog when ERROR or EMERGENCY state is entered.

        Args:
            message: Human-readable error description.
        """
        QMessageBox.critical(self, "CryoSoft Error", message)

    # ------------------------------------------------------------------
    # Window geometry + lifecycle
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        """Restore the saved window geometry, or size to a fraction of the screen.

        Geometry is persisted with ``QSettings``, which on Windows is backed by
        the registry (``HKCU\\Software\\CryoSoft\\CryoSoft``). If nothing is
        stored yet, the window is sized to ~70% of the available screen area.
        """
        settings = QSettings("CryoSoft", "CryoSoft")
        saved = settings.value(_GEOMETRY_KEY)
        if saved is not None and self.restoreGeometry(saved):
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(int(available.width() * 0.7), int(available.height() * 0.7))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Detach the log handler and persist geometry before the window closes.

        Removing the handler prevents it from writing to the destroyed
        ``QTextEdit`` after the window is gone (RuntimeError on a dead widget).

        Args:
            event: The Qt close event.
        """
        logging.getLogger("cryosoft").removeHandler(self._log_handler)
        QSettings("CryoSoft", "CryoSoft").setValue(_GEOMETRY_KEY, self.saveGeometry())
        super().closeEvent(event)
