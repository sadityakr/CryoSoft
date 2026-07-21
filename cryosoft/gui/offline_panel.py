# ---
# description: |
#   OfflineInstrumentPanel + OfflineFrontPanel: the GUI face of an instrument
#   that failed to connect at startup (Station's offline registry, degraded
#   build). The card shows WHAT is offline and WHY in the instrument grid,
#   styled like a disconnected panel; its detail window carries the one
#   reconnect control ("Try reconnect" — the offline counterpart of the
#   Initiate button), which flows through Orchestrator.retry_reconnect().
#   On success MonitorWindow swaps the card for a live InstrumentPanel.
# entry_point: Not run directly. Instantiated by MonitorWindow for each name
#   in Station.offline_vi_names().
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (OfflineInstrument)
#   - cryosoft.core.orchestrator (Orchestrator)
# input: |
#   vi_name (str), the Station's OfflineInstrument record, Orchestrator.
# process: |
#   The card renders name + [OFFLINE] + reason from the record. The lazily
#   created detail window submits retry_reconnect() and listens to
#   action_failed / instrument_reconnected to report the verdict inline.
# output: |
#   A QGroupBox card for the instrument grid and its floating detail window.
# ---

"""OfflineInstrumentPanel — grid card and detail window for an offline VI."""

from __future__ import annotations

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import OfflineInstrument
from cryosoft.gui.theme import BTN_CLASS_PRIMARY, TEXT_ON_ACCENT, TEXT_PRIMARY

_HINT_TEXT = (
    "Check that the instrument is powered on and its cable and address match "
    "the config, then try to reconnect. For a deeper diagnosis run:\n"
    "python -m cryosoft.troubleshoot check"
)


class OfflineInstrumentPanel(QGroupBox):
    """Instrument-grid card for a VI that failed to connect at startup.

    Deliberately control-free: it states the fault and opens the detail
    window, where the single "Try reconnect" action lives (the offline
    counterpart of a live card's Initiate button). Everything else on the
    station keeps working around it.

    Args:
        vi_name: The VI's configured name.
        info: The Station's offline record (reason shown verbatim).
        orchestrator: Orchestrator handling the reconnect request.
        parent: Optional Qt parent widget.
        type_tag: Optional role label ("Measurement", "Scanner"), mirroring
            the live cards so the grid stays recognisable.
    """

    def __init__(
        self,
        vi_name: str,
        info: OfflineInstrument,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
        type_tag: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._vi_name = vi_name
        self._orchestrator = orchestrator
        self._details: OfflineFrontPanel | None = None  # lazily created
        self.setObjectName(f"{vi_name}_offline_card")

        outer = QVBoxLayout()
        outer.setSpacing(4)
        outer.setContentsMargins(8, 8, 8, 8)

        header_row = QHBoxLayout()
        self._name_label = QLabel(f"<b>{vi_name}</b>  [OFFLINE]")
        self._name_label.setObjectName(f"{vi_name}_offline_name_label")
        self._name_label.setProperty("class", "panel_name_label")
        header_row.addWidget(self._name_label)
        if type_tag:
            tag_lbl = QLabel(type_tag)
            tag_lbl.setObjectName(f"{vi_name}_offline_type_tag")
            tag_lbl.setProperty("class", "secondary_label")
            header_row.addWidget(tag_lbl)
        header_row.addStretch()
        details_btn = QPushButton()
        details_btn.setObjectName(f"{vi_name}_offline_details_btn")
        details_btn.setIcon(qta.icon("fa5s.sliders-h", color=TEXT_PRIMARY))
        details_btn.setToolTip(
            "Open the offline-instrument details (full failure reason and "
            "the Try Reconnect action)"
        )
        details_btn.clicked.connect(self._open_details)
        header_row.addWidget(details_btn)
        outer.addLayout(header_row)

        reason_lbl = QLabel(info.reason)
        reason_lbl.setObjectName(f"{vi_name}_offline_reason")
        reason_lbl.setProperty("class", "secondary_label")
        reason_lbl.setWordWrap(True)
        outer.addWidget(reason_lbl)

        note_lbl = QLabel(
            "Not connected at startup — all other instruments are unaffected."
        )
        note_lbl.setObjectName(f"{vi_name}_offline_note")
        note_lbl.setProperty("class", "secondary_label")
        note_lbl.setWordWrap(True)
        outer.addWidget(note_lbl)

        outer.addStretch()
        self.setLayout(outer)
        self.setMinimumWidth(300)
        self.setMinimumHeight(self.sizeHint().height())

        for widget in (self, self._name_label):
            widget.setProperty("status", "offline")
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _open_details(self) -> None:
        """Lazily create and show this VI's offline detail window."""
        if self._details is None:
            self._details = OfflineFrontPanel(
                self._vi_name,
                self._orchestrator,
                parent=self.window(),
            )
        self._details.show()
        self._details.raise_()
        self._details.activateWindow()

    def close_details(self) -> None:
        """Close the detail window, if open (called before the card is
        replaced by a live panel on successful reconnect)."""
        if self._details is not None:
            self._details.close()
            self._details = None


class OfflineFrontPanel(QWidget):
    """Detail window for one offline VI: full reason, hint, Try Reconnect.

    The offline counterpart of :class:`InstrumentFrontPanel`. The reconnect
    request goes through ``Orchestrator.retry_reconnect()`` (IDLE-gated); the
    verdict comes back via ``instrument_reconnected`` / ``action_failed`` and
    is reported inline.

    Args:
        vi_name: The offline VI's configured name.
        orchestrator: Orchestrator handling the reconnect request; its
            station's offline registry provides the live failure reason.
        parent: The owning widget (parented, but flagged as a real window).
    """

    def __init__(
        self,
        vi_name: str,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._vi_name = vi_name
        self._orchestrator = orchestrator
        self.setObjectName(f"{vi_name}_offline_front_panel")
        self.setWindowTitle(f"{vi_name} — Instrument Offline")
        self.setMinimumSize(420, 220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        header = QLabel(f"<b>{vi_name}</b> failed to connect at startup.")
        header.setObjectName(f"{vi_name}_offline_detail_header")
        outer.addWidget(header)

        self._reason_lbl = QLabel("")
        self._reason_lbl.setObjectName(f"{vi_name}_offline_detail_reason")
        self._reason_lbl.setWordWrap(True)
        outer.addWidget(self._reason_lbl)
        self._refresh_reason()

        hint = QLabel(_HINT_TEXT)
        hint.setObjectName(f"{vi_name}_offline_detail_hint")
        hint.setProperty("class", "secondary_label")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        action_row = QHBoxLayout()
        self._reconnect_btn = QPushButton("Try Reconnect")
        self._reconnect_btn.setObjectName(f"{vi_name}_reconnect_btn")
        self._reconnect_btn.setProperty("class", BTN_CLASS_PRIMARY)
        self._reconnect_btn.setIcon(qta.icon("fa5s.plug", color=TEXT_ON_ACCENT))
        self._reconnect_btn.setToolTip(
            "Rebuild this instrument's connection (allowed while no "
            "procedure is running). The app may pause for a few seconds "
            "if the instrument is still unreachable."
        )
        self._reconnect_btn.clicked.connect(self._on_reconnect_clicked)
        self._status_lbl = QLabel("")
        self._status_lbl.setObjectName(f"{vi_name}_reconnect_status")
        self._status_lbl.setProperty("class", "secondary_label")
        action_row.addWidget(self._reconnect_btn)
        action_row.addWidget(self._status_lbl)
        action_row.addStretch()
        outer.addLayout(action_row)
        outer.addStretch()

        orchestrator.instrument_reconnected.connect(self._on_reconnected)
        orchestrator.action_failed.connect(self._on_action_failed)

    def _refresh_reason(self) -> None:
        """Show the offline registry's current failure reason."""
        self._reason_lbl.setText(self._orchestrator.offline_reason(self._vi_name))

    def _on_reconnect_clicked(self) -> None:
        """Submit the reconnect request; the verdict arrives via signals."""
        self._status_lbl.setText("Reconnecting…")
        self._orchestrator.retry_reconnect(self._vi_name)

    def _on_reconnected(self, vi_name: str) -> None:
        """Report success; MonitorWindow swaps the card and closes us."""
        if vi_name != self._vi_name:
            return
        self._status_lbl.setText("Reconnected — instrument is live.")
        self._reconnect_btn.setEnabled(False)

    def _on_action_failed(self, vi_name: str, method_name: str, reason: str) -> None:
        """Report a failed reconnect attempt inline, with the fresh reason."""
        if vi_name != self._vi_name or method_name != "reconnect":
            return
        self._status_lbl.setText("Still not reachable.")
        self._refresh_reason()
