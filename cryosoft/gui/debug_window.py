# ---
# description: |
#   DebugWindow: read-only live diagnostics window for connection and
#   progress problems ("a device stopped communicating" / "this is taking
#   way longer than expected"). Renders the Orchestrator's operational_status
#   signal (the per-tick record cryosoft.core.operational_status assembles
#   and cryosoft.core.watchdog annotates) as a verdict badge, a per-instrument
#   status table, and an alerts feed, each paired with the same plain-English
#   fault-code vocabulary the offline troubleshoot CLI uses. A Copy
#   Diagnostics button puts a text summary on the clipboard for a support
#   message.
# entry_point: Not run directly. Opened via MonitorWindow's Debug menu.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.core.operational_status (RunFaultCode)
# input: |
#   An Orchestrator instance. Seeds from get_operational_status() at
#   construction, then updates live from the operational_status signal.
# process: |
#   Purely reactive to already-emitted Orchestrator data; makes no hardware
#   calls and reads no files, so it stays reliable even mid-incident, unlike
#   a competing bus scan against instruments the running app already holds
#   open.
# output: |
#   A QMainWindow. Nothing here can change Orchestrator or hardware state.
# ---

"""DebugWindow — live connection/progress diagnostics (read-only)."""

from __future__ import annotations

import logging

from PyQt6.QtGui import QCloseEvent, QColor
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QPushButton,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.operational_status import RunFaultCode
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.gui import window_geometry
from cryosoft.gui.theme import BTN_CLASS_SECONDARY, STATUS_ERROR, STATUS_OK, STATUS_WARN

logger = logging.getLogger(__name__)

_GEOMETRY_KEY = "DebugWindow/geometry"

# Plain-English cause per runtime fault code, mirroring
# cryosoft.troubleshoot.status_reader.CODE_HELP's vocabulary. Kept as its own
# copy rather than imported: the troubleshoot toolbox is a leaf entry point
# nothing else may import (contract C9), so the GUI carries its own copy of
# the same explanations for the live-run codes it can actually see.
_CODE_EXPLANATIONS: dict[str, str] = {
    RunFaultCode.OK.value: "Normal — ramping/settling on schedule, or idle.",
    RunFaultCode.VI_STALE.value: (
        "Stopped returning fresh readings (values are cached). Check its "
        "connection and that it is powered and not hung."
    ),
    RunFaultCode.VI_DISCONNECTED.value: (
        "Repeated communication failures — treated as off the bus. Check "
        "power, cabling, and address. Close the app and run "
        "`python -m cryosoft.troubleshoot check` for a full preflight."
    ),
    RunFaultCode.QUENCH.value: (
        "A magnet reported a quench. Verify the magnet state and helium "
        "level immediately."
    ),
    RunFaultCode.RAMP_STALLED.value: (
        "The setpoint is being sent but the value is not following — "
        "suspect a controller/PID limit, a saturated heater, a thermal "
        "load, or the instrument not accepting setpoints."
    ),
    RunFaultCode.STALLED_RUN.value: (
        "The run is wedged in a step that should be momentary — suspect a "
        "procedure step that is not returning, or a measurement instrument "
        "that is not responding."
    ),
}

# UI severity bucket per fault code, driving both the verdict badge's QSS
# property and each table row's status colour.
_CODE_SEVERITY: dict[str, str] = {
    RunFaultCode.OK.value: "ok",
    RunFaultCode.VI_STALE.value: "warning",
    RunFaultCode.RAMP_STALLED.value: "warning",
    RunFaultCode.STALLED_RUN.value: "warning",
    RunFaultCode.QUENCH.value: "error",
    RunFaultCode.VI_DISCONNECTED.value: "error",
}
_SEVERITY_COLOR = {"ok": STATUS_OK, "warning": STATUS_WARN, "error": STATUS_ERROR}


class DebugWindow(QMainWindow):
    """Read-only live diagnostics: instrument connection/progress health.

    Answers "did a device stop communicating?" and "is this taking way
    longer than expected?" while the app is running, from data the
    Orchestrator already collected each tick — it polls no hardware itself.

    Args:
        orchestrator: The active Orchestrator instance.
        parent: Optional Qt parent widget.
    """

    def __init__(self, orchestrator: Orchestrator, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._orchestrator = orchestrator
        self._latest_record: dict = {}
        self._badge_severity = ""

        self.setWindowTitle("CryoSoft — Diagnostics")
        window_geometry.restore_or_center(self, _GEOMETRY_KEY, fraction=0.5)

        self._build_ui()
        self._orchestrator.operational_status.connect(self._on_operational_status)

        seed = self._orchestrator.get_operational_status()
        if seed:
            self._on_operational_status(seed)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Run status</b>"))
        header.addStretch()
        self._verdict_badge = QLabel("—")
        self._verdict_badge.setObjectName("verdict_badge")
        self._verdict_badge.setProperty("class", "verdict_badge")
        # The longest code (STALLED_RUN/VI_DISCONNECTED) must fit without
        # wrapping; a floating sizeHint is not reliably honoured by the
        # QHBoxLayout against the header's stretch.
        self._verdict_badge.setMinimumWidth(140)
        self._verdict_badge.setWordWrap(False)
        header.addWidget(self._verdict_badge)
        root.addLayout(header)

        self._state_label = QLabel(
            "No live data yet — start monitoring to see live status."
        )
        self._state_label.setObjectName("debug_state_label")
        root.addWidget(self._state_label)

        root.addWidget(QLabel("<b>Instruments</b>"))
        self._table = QTableWidget(0, 3)
        self._table.setObjectName("debug_vi_table")
        self._table.setHorizontalHeaderLabels(["Instrument", "Status", "Detail"])
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        root.addWidget(self._table, stretch=1)

        root.addWidget(QLabel("<b>Alerts</b>"))
        self._alerts_view = QTextEdit()
        self._alerts_view.setObjectName("debug_alerts_view")
        self._alerts_view.setReadOnly(True)
        self._alerts_view.setMaximumHeight(100)
        self._alerts_view.setPlainText("No active alerts.")
        root.addWidget(self._alerts_view)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._copy_btn = QPushButton("Copy Diagnostics")
        self._copy_btn.setObjectName("copy_diagnostics_btn")
        self._copy_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._copy_btn.setToolTip(
            "Copy the current status, instrument table, and alerts as plain "
            "text — for pasting into a support message"
        )
        self._copy_btn.clicked.connect(self._on_copy_diagnostics)
        btn_row.addWidget(self._copy_btn)
        root.addLayout(btn_row)

        self.setCentralWidget(central)
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)

    # ------------------------------------------------------------------
    # Live updates
    # ------------------------------------------------------------------

    def _on_operational_status(self, record: dict) -> None:
        """Render one operational-status tick.

        Args:
            record: The dict from ``cryosoft.core.operational_status``,
                already annotated by the watchdog (verdict, alerts, per-VI
                codes).
        """
        self._latest_record = record

        orch_state = record.get("orch_state", "?")
        elapsed = record.get("elapsed_in_state_s", 0.0)
        self._state_label.setText(f"State: {orch_state}   ({elapsed:.0f}s in state)")

        verdict = record.get("verdict", RunFaultCode.OK.value)
        self._verdict_badge.setText(verdict)
        self._set_badge_severity(_CODE_SEVERITY.get(verdict, "warning"))

        vis = record.get("vis", [])
        self._table.setRowCount(len(vis))
        for row, vi in enumerate(vis):
            code = vi.get("code", RunFaultCode.OK.value)
            severity = _CODE_SEVERITY.get(code, "warning")
            detail = vi.get("detail") or _CODE_EXPLANATIONS.get(code, "")

            name_item = QTableWidgetItem(vi.get("vi_name", "?"))
            status_item = QTableWidgetItem(code)
            status_item.setForeground(QColor(_SEVERITY_COLOR[severity]))
            detail_item = QTableWidgetItem(detail)

            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, status_item)
            self._table.setItem(row, 2, detail_item)

        alerts = record.get("alerts", [])
        self._alerts_view.setPlainText("\n".join(alerts) if alerts else "No active alerts.")

    def _set_badge_severity(self, severity: str) -> None:
        """Set the verdict badge's ``severity`` QSS property, repolishing only on change.

        Args:
            severity: ``"ok"``, ``"warning"``, or ``"error"``.
        """
        if severity == self._badge_severity:
            return
        self._badge_severity = severity
        self._verdict_badge.setProperty("severity", severity)
        self._verdict_badge.style().unpolish(self._verdict_badge)
        self._verdict_badge.style().polish(self._verdict_badge)

    # ------------------------------------------------------------------
    # Copy diagnostics
    # ------------------------------------------------------------------

    def _render_diagnostics_text(self) -> str:
        """Render the latest record as a plain-English block for the clipboard.

        Returns:
            A multi-line string summarising state, alerts, and per-instrument
            status/explanation, or a placeholder if no tick has arrived yet.
        """
        record = self._latest_record
        if not record:
            return "No live status available — start monitoring first."

        lines = [
            f"State: {record.get('orch_state')}  "
            f"({record.get('elapsed_in_state_s', 0):.0f}s in state)   "
            f"Verdict: {record.get('verdict')}"
        ]
        alerts = record.get("alerts", [])
        if alerts:
            lines.append("Alerts:")
            lines.extend(f"  ! {a}" for a in alerts)
        lines.append("Instruments:")
        for vi in record.get("vis", []):
            code = vi.get("code", RunFaultCode.OK.value)
            lines.append(
                f"  {vi.get('vi_name')}: {code} — {_CODE_EXPLANATIONS.get(code, '')}"
            )
        return "\n".join(lines)

    def _on_copy_diagnostics(self) -> None:
        """Copy the current diagnostics text to the clipboard."""
        QApplication.clipboard().setText(self._render_diagnostics_text())
        self._status_bar.showMessage("Diagnostics copied to clipboard", 4000)

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Persist geometry before closing."""
        window_geometry.save_geometry(self, _GEOMETRY_KEY)
        super().closeEvent(event)
