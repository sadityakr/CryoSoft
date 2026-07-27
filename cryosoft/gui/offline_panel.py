# ---
# description: |
#   OfflineInstrumentPanel + OfflineFrontPanel: the GUI face of an instrument
#   that is not currently CryoSoft's — either it failed to connect at startup
#   (degraded build) or the operator released it (the connection-lifecycle
#   standard, see virtual_instruments/base.py). Both land in the Station's ONE
#   offline registry and render through this same card; only the wording
#   differs, keyed off OfflineInstrument.origin, because the degraded behavior
#   is deliberately identical. The card shows WHAT is offline and WHY in the
#   instrument grid and carries the Connect button (the offline card's half of
#   the ConnectionButton pair the live card shows as Disconnect); its detail
#   window repeats it with the full reason and a diagnosis hint. Both flow
#   through Orchestrator.connect_instrument(). On success MonitorWindow swaps
#   the card for a live InstrumentPanel.
# entry_point: Not run directly. Instantiated by MonitorWindow for each name
#   in Station.offline_vi_names().
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (OfflineInstrument)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.lifecycle_toggle (ConnectionButton)
# input: |
#   vi_name (str), the Station's OfflineInstrument record, Orchestrator.
# process: |
#   The card renders name + [OFFLINE]/[DISCONNECTED] + reason from the record,
#   plus a Connect button. The lazily created detail window submits
#   connect_instrument() and listens to action_failed / instrument_reconnected
#   to report the verdict inline.
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
from cryosoft.gui.lifecycle_toggle import ConnectionButton
from cryosoft.gui.theme import TEXT_PRIMARY

_HINT_TEXT = (
    "Check that the instrument is powered on and its cable and address match "
    "the config, then try to connect. For a deeper diagnosis run:\n"
    "python -m cryosoft.troubleshoot check"
)

_OPERATOR_HINT_TEXT = (
    "You released this instrument, so it is free for its own front panel or "
    "vendor software. Nothing was changed on it. Press Connect to hand it "
    "back to CryoSoft — then Initiate to bring it to its operating state."
)


class OfflineInstrumentPanel(QGroupBox):
    """Instrument-grid card for a VI CryoSoft does not currently hold.

    Covers both offline origins with one card, because the Station degrades
    them identically (see the connection-lifecycle standard on
    ``BaseVirtualInstrument``): an instrument that never connected, and one
    the operator deliberately released. Only the label, the note and the hint
    differ, so the operator can tell "something is wrong" from "I did this".

    Control-free apart from the one action that applies here: Connect — the
    offline half of the ConnectionButton pair whose live-card half reads
    Disconnect. Everything else on the station keeps working around it.

    Args:
        vi_name: The VI's configured name.
        info: The Station's offline record (reason shown verbatim; ``origin``
            selects the wording).
        orchestrator: Orchestrator handling the connect request.
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
        self._by_operator = "operator" in info.tags
        self.setObjectName(f"{vi_name}_offline_card")

        outer = QVBoxLayout()
        outer.setSpacing(4)
        outer.setContentsMargins(8, 8, 8, 8)

        header_row = QHBoxLayout()
        badge = "[DISCONNECTED]" if self._by_operator else "[OFFLINE]"
        self._name_label = QLabel(f"<b>{vi_name}</b>  {badge}")
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
            "Open the offline-instrument details (full reason and the "
            "Connect action)"
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
            "Released to its front panel — all other instruments are "
            "unaffected."
            if self._by_operator
            else "Not connected at startup — all other instruments are unaffected."
        )
        note_lbl.setObjectName(f"{vi_name}_offline_note")
        note_lbl.setProperty("class", "secondary_label")
        note_lbl.setWordWrap(True)
        outer.addWidget(note_lbl)

        self._status_lbl = QLabel("")
        self._status_lbl.setObjectName(f"{vi_name}_offline_card_status")
        self._status_lbl.setProperty("class", "secondary_label")
        self._status_lbl.setWordWrap(True)
        outer.addWidget(self._status_lbl)

        # Connect lives in the body, not the header: an offline card has room
        # to spare where a live card's header does not, so the one action that
        # applies here gets its word rather than the live card's compact icon.
        action_row = QHBoxLayout()
        action_row.setContentsMargins(0, 2, 0, 2)
        self._connect_btn = ConnectionButton(
            vi_name, "connect", self._on_connect_clicked, parent=self
        )
        action_row.addWidget(self._connect_btn)
        action_row.addStretch()
        outer.addLayout(action_row)

        outer.addStretch()
        self.setLayout(outer)

        orchestrator.action_failed.connect(self._on_action_failed)
        self.setMinimumWidth(300)
        self.setMinimumHeight(self.sizeHint().height())

        for widget in (self, self._name_label):
            widget.setProperty("status", "offline")
            widget.style().unpolish(widget)
            widget.style().polish(widget)

    def _on_connect_clicked(self) -> None:
        """Submit the connect request; the verdict arrives via signals."""
        self._status_lbl.setText("Connecting…")
        self._orchestrator.connect_instrument(self._vi_name)

    def _on_action_failed(self, vi_name: str, method_name: str, reason: str) -> None:
        """Report a failed connect attempt on the card itself.

        Success needs no handler here: MonitorWindow replaces this card with
        a live panel the moment ``instrument_reconnected`` fires.
        """
        if vi_name != self._vi_name or method_name != "connect":
            return
        self._status_lbl.setText(f"Still not reachable: {reason}")

    def _open_details(self) -> None:
        """Lazily create and show this VI's offline detail window."""
        if self._details is None:
            self._details = OfflineFrontPanel(
                self._vi_name,
                self._orchestrator,
                parent=self.window(),
                by_operator=self._by_operator,
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
    """Detail window for one offline VI: full reason, hint, Connect.

    The offline counterpart of :class:`InstrumentFrontPanel`. The connect
    request goes through ``Orchestrator.connect_instrument()`` (IDLE-gated);
    the verdict comes back via ``instrument_reconnected`` / ``action_failed``
    and is reported inline.

    Args:
        vi_name: The offline VI's configured name.
        orchestrator: Orchestrator handling the connect request; its
            station's offline registry provides the live failure reason.
        parent: The owning widget (parented, but flagged as a real window).
        by_operator: Whether the operator disconnected this instrument
            deliberately (the connection-lifecycle standard) rather than it
            failing to connect. Selects the wording only — the action and
            the code path are identical.
    """

    def __init__(
        self,
        vi_name: str,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
        by_operator: bool = False,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self._vi_name = vi_name
        self._orchestrator = orchestrator
        self._by_operator = by_operator
        self.setObjectName(f"{vi_name}_offline_front_panel")
        self.setWindowTitle(
            f"{vi_name} — Instrument Disconnected"
            if by_operator
            else f"{vi_name} — Instrument Offline"
        )
        self.setMinimumSize(420, 220)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        header = QLabel(
            f"<b>{vi_name}</b> is disconnected — CryoSoft is not holding it."
            if by_operator
            else f"<b>{vi_name}</b> failed to connect at startup."
        )
        header.setObjectName(f"{vi_name}_offline_detail_header")
        outer.addWidget(header)

        self._reason_lbl = QLabel("")
        self._reason_lbl.setObjectName(f"{vi_name}_offline_detail_reason")
        self._reason_lbl.setWordWrap(True)
        outer.addWidget(self._reason_lbl)
        self._refresh_reason()

        hint = QLabel(_OPERATOR_HINT_TEXT if by_operator else _HINT_TEXT)
        hint.setObjectName(f"{vi_name}_offline_detail_hint")
        hint.setProperty("class", "secondary_label")
        hint.setWordWrap(True)
        outer.addWidget(hint)

        action_row = QHBoxLayout()
        self._connect_btn = ConnectionButton(
            vi_name,
            "connect",
            self._on_connect_clicked,
            parent=self,
            # Keep this window's long-standing objectName (objectNames are
            # API) and keep it distinct from the card's own Connect button.
            object_name=f"{vi_name}_reconnect_btn",
        )
        self._status_lbl = QLabel("")
        self._status_lbl.setObjectName(f"{vi_name}_reconnect_status")
        self._status_lbl.setProperty("class", "secondary_label")
        action_row.addWidget(self._connect_btn)
        action_row.addWidget(self._status_lbl)
        action_row.addStretch()
        outer.addLayout(action_row)
        outer.addStretch()

        orchestrator.instrument_reconnected.connect(self._on_reconnected)
        orchestrator.action_failed.connect(self._on_action_failed)

    def _refresh_reason(self) -> None:
        """Show the offline registry's current reason."""
        self._reason_lbl.setText(self._orchestrator.offline_reason(self._vi_name))

    def _on_connect_clicked(self) -> None:
        """Submit the connect request; the verdict arrives via signals."""
        self._status_lbl.setText("Connecting…")
        self._orchestrator.connect_instrument(self._vi_name)

    def _on_reconnected(self, vi_name: str) -> None:
        """Report success; MonitorWindow swaps the card and closes us."""
        if vi_name != self._vi_name:
            return
        self._status_lbl.setText("Connected — instrument is live.")
        self._connect_btn.setEnabled(False)

    def _on_action_failed(self, vi_name: str, method_name: str, reason: str) -> None:
        """Report a failed connect attempt inline, with the fresh reason."""
        if vi_name != self._vi_name or method_name != "connect":
            return
        self._status_lbl.setText("Still not reachable.")
        self._refresh_reason()
