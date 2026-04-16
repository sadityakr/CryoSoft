# ---
# description: |
#   MonitorWindow: the main CryoSoft window showing live instrument state.
#   Auto-generates InstrumentPanels for system/level VIs in a scrollable grid
#   and measurement VIs in a separate "Other Devices" section. Hosts global
#   controls, sample info, a real-time log panel, and a menu to open ProcedureWindow.
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
# last_updated: 2026-04-16
# ---

"""MonitorWindow — main CryoSoft monitor window."""

from __future__ import annotations

import logging
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
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
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import Station
from cryosoft.gui.instrument_panel import InstrumentPanel

logger = logging.getLogger(__name__)

_COLUMNS = 2  # columns in the system VI instrument grid
_LOG_MAX_LINES = 500


class _QtLogHandler(logging.Handler):
    """Logging handler that appends coloured HTML lines to a QTextEdit.

    Args:
        widget: The read-only QTextEdit to write into.
    """

    _LEVEL_COLOURS: dict[int, str] = {
        logging.DEBUG: "#808080",
        logging.INFO: "#d4d4d4",
        logging.WARNING: "#ce9178",
        logging.ERROR: "#f44747",
        logging.CRITICAL: "#ff0000",
    }

    def __init__(self, widget: QTextEdit) -> None:
        super().__init__()
        self._widget = widget
        self.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                              datefmt="%H:%M:%S")
        )

    def emit(self, record: logging.LogRecord) -> None:
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
        self.resize(960, 860)

        self._build_menu()
        self._build_ui()
        self._connect_signals()

        # Attach log handler after UI exists
        self._log_handler = _QtLogHandler(self._log_widget)
        self._log_handler.setLevel(logging.DEBUG)
        logging.getLogger("cryosoft").addHandler(self._log_handler)

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
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)
        root.setContentsMargins(8, 8, 8, 8)

        # ── Header: global state + control buttons ────────────────────
        root.addLayout(self._build_header())

        # ── Sample info ───────────────────────────────────────────────
        root.addWidget(self._build_sample_info_section())

        # ── System / level VI instrument grid ─────────────────────────
        system_vis = [
            n for n in self._station.get_vi_names()
            if self._station.get_vi_type(n) in {"system", "level"}
        ]
        measurement_vis = [
            n for n in self._station.get_vi_names()
            if self._station.get_vi_type(n) == "measurement"
        ]

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        grid_container = QWidget()
        self._grid = QGridLayout(grid_container)
        self._grid.setSpacing(8)

        for idx, vi_name in enumerate(system_vis):
            vi = self._station._virtual_instruments[vi_name]
            panel = InstrumentPanel(vi_name, vi, self._orchestrator, parent=self)
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row, col = divmod(idx, _COLUMNS)
            self._grid.addWidget(panel, row, col)

        scroll.setWidget(grid_container)
        root.addWidget(scroll)

        # ── Other Devices (measurement VIs) ───────────────────────────
        if measurement_vis:
            root.addWidget(self._build_other_devices_section(measurement_vis))

        # ── Log panel ─────────────────────────────────────────────────
        root.addWidget(self._build_log_section())

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
        initiate_all_btn.clicked.connect(
            lambda: self._orchestrator.submit_global_action("initiate_all")
        )

        standby_all_btn = QPushButton("Standby All")
        standby_all_btn.setObjectName("standby_all_btn")
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
        self._comments_input.setMaximumHeight(50)
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
        """Build the Other Devices section for measurement VIs.

        Args:
            vi_names: Names of measurement VIs to display.

        Returns:
            A QGroupBox containing one InstrumentPanel per measurement VI.
        """
        box = QGroupBox("Other Devices")
        h_layout = QHBoxLayout(box)
        h_layout.setSpacing(8)

        for vi_name in vi_names:
            vi = self._station._virtual_instruments[vi_name]
            panel = InstrumentPanel(vi_name, vi, self._orchestrator, parent=self)
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            h_layout.addWidget(panel)

        h_layout.addStretch()
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
        self._log_widget.setMaximumHeight(160)
        self._log_widget.setStyleSheet(
            "QTextEdit { background-color: #1e1e1e; font-family: monospace; font-size: 11px; }"
        )
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
