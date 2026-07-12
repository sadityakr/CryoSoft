# ---
# description: |
#   LifecycleToggleButton: one state-dependent button (+ status glow dot)
#   replacing the old separate Initiate/Standby QPushButton pair. Shared by
#   InstrumentPanel and MonitorWindow's compact Other Devices rows so both
#   places render and update identically.
# entry_point: Not run directly. Instantiated by InstrumentPanel / MonitorWindow.
# dependencies:
#   - PyQt6 >= 6.5
#   - qtawesome
# input: |
#   A callback invoked with "initiate" or "standby" when clicked.
# process: |
#   The button starts in the standby ("Initiate", red dot) state. Clicking it
#   only submits the opposite action via the callback — it does NOT flip
#   state optimistically. set_initiated() is the only thing that changes the
#   displayed state, and callers wire it to Orchestrator.action_succeeded so
#   the glow reflects confirmed instrument state, not a hopeful click.
# output: |
#   A QWidget (dot + button). set_initiated(bool) updates it; is_initiated()
#   reads it back.
# ---

"""LifecycleToggleButton — single state-dependent Initiate/Standby control."""

from __future__ import annotations

from collections.abc import Callable

import qtawesome as qta
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from cryosoft.gui.theme import BTN_CLASS_PRIMARY, BTN_CLASS_SECONDARY, TEXT_ON_ACCENT, TEXT_PRIMARY


class LifecycleToggleButton(QWidget):
    """A single Initiate/Standby toggle with a status glow dot.

    Args:
        vi_name: The VI's registered name, used to scope objectNames.
        on_toggle: Called with ``"initiate"`` or ``"standby"`` (the opposite
            of the current displayed state) when the button is clicked.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        vi_name: str,
        on_toggle: Callable[[str], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._vi_name = vi_name
        self._on_toggle = on_toggle
        self._initiated = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)

        self._dot = QLabel("●")
        self._dot.setObjectName(f"{vi_name}_lifecycle_dot")
        self._dot.setProperty("class", "lifecycle_dot")
        self._dot.setProperty("status", "standby")
        row.addWidget(self._dot)

        self._btn = QPushButton()
        self._btn.setObjectName(f"{vi_name}_lifecycle_btn")
        row.addWidget(self._btn)

        self._btn.clicked.connect(self._on_click)
        self._render()

    def _on_click(self) -> None:
        self._on_toggle("standby" if self._initiated else "initiate")

    def is_initiated(self) -> bool:
        """Return the currently displayed state (True = initiated)."""
        return self._initiated

    def set_initiated(self, initiated: bool) -> None:
        """Update the displayed state. No-op if already showing that state.

        Args:
            initiated: True to show the "Standby" / green-dot state, False
                for "Initiate" / red-dot.
        """
        if initiated == self._initiated:
            return
        self._initiated = initiated
        self._render()

    def _render(self) -> None:
        if self._initiated:
            self._btn.setText("Standby")
            self._btn.setProperty("class", BTN_CLASS_SECONDARY)
            self._btn.setIcon(qta.icon("fa5s.power-off", color=TEXT_PRIMARY))
            self._btn.setToolTip(f"Return {self._vi_name} to a safe standby state")
            self._dot.setProperty("status", "initiated")
        else:
            self._btn.setText("Initiate")
            self._btn.setProperty("class", BTN_CLASS_PRIMARY)
            self._btn.setIcon(qta.icon("fa5s.play", color=TEXT_ON_ACCENT))
            self._btn.setToolTip(f"Bring {self._vi_name} to its operating state")
            self._dot.setProperty("status", "standby")

        # Qt only re-evaluates property-based QSS selectors after an
        # unpolish/polish cycle (same pattern InstrumentPanel's status border uses).
        for widget in (self._btn, self._dot):
            widget.style().unpolish(widget)
            widget.style().polish(widget)
