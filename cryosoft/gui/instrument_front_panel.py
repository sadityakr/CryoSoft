# ---
# description: |
#   InstrumentFrontPanel: a per-VI child window showing the instrument's FULL
#   capability surface — every @monitored value live and every @control method,
#   including the ones the compact monitor card hides (panel=False defaults or
#   a monitor.yaml panels: allowlist). Rendered entirely from decorator
#   metadata by embedding an InstrumentPanel whose allowlist is "everything",
#   so a new VI capability appears here the moment it is declared, with zero
#   per-instrument GUI code. This is the GUI bench-test surface: uncommon
#   actions (PID setting, auto-tune, heater power) live here, and every click
#   still flows through Orchestrator.submit_vi_action() with control-limit
#   validation intact.
# entry_point: Not run directly. Lazily created by InstrumentPanel via its
#   front-panel header icon.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.decorators (get_control_methods)
#   - cryosoft.gui.instrument_panel (InstrumentPanel)
# input: |
#   vi_name (str), vi instance, Orchestrator instance, and the owning widget
#   (used as Qt parent so the window closes with the application).
# process: |
#   Builds a titled window (Qt.WindowType.Window on a parented QWidget: a real
#   top-level window that is still destroyed with its parent) containing a
#   QScrollArea over one InstrumentPanel constructed with panel_controls set
#   to every @control name — the allowlist override that shows the full set.
# output: |
#   A reusable child window; callers show()/raise_() it on each icon click.
# ---

"""InstrumentFrontPanel — the full-capability window for one VI."""

from __future__ import annotations

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.decorators import get_control_methods
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.gui.instrument_panel import InstrumentPanel
from cryosoft.gui.theme import TEXT_PRIMARY
from cryosoft.virtual_instruments.base import BaseVirtualInstrument


class InstrumentFrontPanel(QWidget):
    """Child window rendering one VI's complete monitored + control surface.

    Args:
        vi_name: The VI's registered name.
        vi: The VI instance (introspection only; actions go via the
            Orchestrator).
        orchestrator: Orchestrator whose signals drive the embedded panel.
        parent: The owning widget. The window is parented (so it is destroyed
            with the application) but flagged ``Qt.WindowType.Window`` so it
            floats as a real window.
    """

    def __init__(
        self,
        vi_name: str,
        vi: BaseVirtualInstrument,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName(f"{vi_name}_front_panel")
        self.setWindowTitle(f"{vi_name} — Instrument Front Panel")
        self.setMinimumSize(420, 320)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)

        # ── Connection check row: a harmless identity query (vi.ping()),
        # never an arming/initiate action — the front panel's bench-test
        # equivalent of the old Other Devices "Check" button. ─────────────
        check_row = QHBoxLayout()
        check_btn = QPushButton("Check connection")
        check_btn.setObjectName(f"{vi_name}_check_btn")
        check_btn.setIcon(qta.icon("fa5s.plug", color=TEXT_PRIMARY))
        check_btn.setToolTip(
            f"Send an identity query to test the {vi_name} connection "
            "(does not initiate or arm anything)"
        )
        self._check_status = QLabel("")
        self._check_status.setObjectName(f"{vi_name}_check_status")
        self._check_status.setProperty("class", "secondary_label")
        check_btn.clicked.connect(self._on_check)
        check_row.addWidget(check_btn)
        check_row.addWidget(self._check_status)
        check_row.addStretch()
        outer.addLayout(check_row)
        self._vi = vi

        # The allowlist override shows EVERY control, regardless of each
        # control's panel= default or the setup's monitor.yaml allowlist.
        all_controls = list(get_control_methods(vi))
        self._panel = InstrumentPanel(
            vi_name,
            vi,
            orchestrator,
            parent=self,
            panel_controls=all_controls,
            show_front_panel_button=False,
        )

        scroll = QScrollArea()
        scroll.setObjectName(f"{vi_name}_front_panel_scroll")
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._panel)
        outer.addWidget(scroll)

    def _on_check(self) -> None:
        """Ping the instrument and report the verdict inline."""
        try:
            ok = self._vi.ping()
        except Exception:  # noqa: BLE001 — any failure means "not reachable"
            ok = False
        self._check_status.setText("Connected" if ok else "Not reachable")
