# ---
# description: |
#   MonitorWindow: the main CryoSoft window showing live instrument state.
#   Auto-generates one InstrumentPanel per VI registered in the Station.
#   Hosts global controls (Initiate All, Standby All) and a status bar that
#   reflects the Orchestrator state and any errors.
# entry_point: Not run directly. Instantiated in main.py.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.instrument_panel (InstrumentPanel)
# input: |
#   Station instance and Orchestrator instance.
# process: |
#   Iterates over all VI names in the station, creates one InstrumentPanel each,
#   arranges them in a flow grid, and connects Orchestrator signals to update
#   the status bar and show error dialogs.
# output: |
#   A QMainWindow that stays open for the lifetime of the application.
# last_updated: 2026-04-06
# ---

"""MonitorWindow — main CryoSoft monitor window."""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import Station
from cryosoft.gui.instrument_panel import InstrumentPanel

logger = logging.getLogger(__name__)

_COLUMNS = 2  # number of columns in the instrument grid


class MonitorWindow(QMainWindow):
    """Main window: live instrument monitor + global controls.

    One :class:`InstrumentPanel` is created for each VI in the Station.
    Panels are laid out in a scrollable grid of ``_COLUMNS`` columns.

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

        self.setWindowTitle("CryoSoft — Monitor")
        self.resize(900, 700)

        self._build_ui()
        self._connect_signals()

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

        # ── Instrument grid ───────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        grid_container = QWidget()
        self._grid = QGridLayout(grid_container)
        self._grid.setSpacing(8)

        vi_names = self._station.get_vi_names()
        for idx, vi_name in enumerate(vi_names):
            vi = self._station._virtual_instruments[vi_name]
            panel = InstrumentPanel(vi_name, vi, self._orchestrator, parent=self)
            panel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            row, col = divmod(idx, _COLUMNS)
            self._grid.addWidget(panel, row, col)

        scroll.setWidget(grid_container)
        root.addWidget(scroll)

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
