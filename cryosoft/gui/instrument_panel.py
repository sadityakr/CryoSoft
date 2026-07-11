# ---
# description: |
#   InstrumentPanel: auto-generated QGroupBox for one Virtual Instrument.
#   Reads @monitored methods to create live-updating QLabel displays and
#   @control methods to create QPushButton + QLineEdit widgets. Connects
#   to Orchestrator.states_updated for live updates each monitor tick.
# entry_point: Not run directly. Instantiated by MonitorWindow.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.decorators (get_monitored_methods, get_control_methods)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.virtual_instruments.base (BaseVirtualInstrument)
# input: |
#   vi_name (str), vi instance, Orchestrator instance.
# process: |
#   __init__ introspects the VI for @monitored and @control methods and builds
#   the layout. _on_states_updated() updates values each tick and, only when the
#   connection status changes, flips the QSS `status` property (ok/stale/disconnected).
# output: |
#   A QGroupBox with live values and control buttons embedded in MonitorWindow.
# last_updated: 2026-04-17
# ---

"""InstrumentPanel — auto-generated per-VI monitor panel."""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.decorators import get_control_methods, get_monitored_methods
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.gui.theme import BTN_CLASS_PRIMARY, BTN_CLASS_SECONDARY
from cryosoft.virtual_instruments.base import BaseVirtualInstrument

logger = logging.getLogger(__name__)


class InstrumentPanel(QGroupBox):
    """Auto-generated display + control panel for one Virtual Instrument.

    Created by MonitorWindow for each VI registered in the Station. The
    layout is derived entirely from decorator metadata — no hardcoded
    per-instrument widget lists.

    Args:
        vi_name: The VI's registered name (e.g. ``"magnet_x"``).
        vi: The VI instance (used for introspection only).
        orchestrator: Orchestrator whose ``states_updated`` signal drives updates.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        vi_name: str,
        vi: BaseVirtualInstrument,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(vi_name, parent)
        self._vi_name = vi_name
        self._vi = vi
        self._orchestrator = orchestrator

        # Maps field name → value label widget
        self._value_labels: dict[str, QLabel] = {}
        # Maps method_name → {param_name → QLineEdit}
        self._control_inputs: dict[str, dict[str, QLineEdit]] = {}
        # Current status ("ok"/"stale"/"disconnected"). Drives the QSS
        # `status` property; tracked so styling is only re-applied on change.
        self._status = "ok"

        self._build_layout()

        # Keep panels readable: never let the grid squeeze a panel below the
        # width of its content, and give it a natural minimum height from the
        # assembled layout's sizeHint.
        self.setMinimumWidth(300)
        self.setMinimumHeight(self.sizeHint().height())

        orchestrator.states_updated.connect(self._on_states_updated)
        orchestrator.action_blocked.connect(self._on_action_blocked)

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout()
        outer.setSpacing(4)
        outer.setContentsMargins(8, 12, 8, 8)

        # ── Monitored fields ──────────────────────────────────────────
        for method_name in get_monitored_methods(self._vi):
            row = QHBoxLayout()
            display_name = method_name.replace("_", " ")
            lbl = QLabel(f"{display_name}:")
            lbl.setMinimumWidth(130)
            val = QLabel("—")
            val.setObjectName(f"{self._vi_name}_{method_name}_value")
            val.setProperty("class", "value_readout")
            self._value_labels[method_name] = val
            row.addWidget(lbl)
            row.addWidget(val)
            row.addStretch()
            outer.addLayout(row)

        # ── Control methods ───────────────────────────────────────────
        for method_name, params in get_control_methods(self._vi).items():
            outer.addWidget(self._build_control_row(method_name, params))

        # ── Lifecycle buttons ─────────────────────────────────────────
        btn_row = QHBoxLayout()
        initiate_btn = QPushButton("Initiate")
        initiate_btn.setObjectName(f"{self._vi_name}_initiate_btn")
        initiate_btn.setProperty("class", BTN_CLASS_PRIMARY)
        initiate_btn.clicked.connect(lambda: self._submit_lifecycle("initiate"))
        standby_btn = QPushButton("Standby")
        standby_btn.setObjectName(f"{self._vi_name}_standby_btn")
        standby_btn.setProperty("class", BTN_CLASS_SECONDARY)
        standby_btn.clicked.connect(lambda: self._submit_lifecycle("standby"))
        btn_row.addWidget(initiate_btn)
        btn_row.addWidget(standby_btn)
        btn_row.addStretch()
        outer.addLayout(btn_row)

        # Absorb any extra vertical space so control rows never expand beyond
        # their natural height when the panel is stretched by the grid.
        outer.addStretch()

        self.setLayout(outer)

    def _build_control_row(self, method_name: str, params: dict) -> QWidget:
        """Build one @control method row: button + input fields.

        Args:
            method_name: Name of the @control method.
            params: ``{param_name: {"type": type, "default": value, ...}}``

        Returns:
            A QWidget containing the assembled row.
        """
        container = QWidget()
        # Fixed vertical policy prevents the container from expanding when the
        # parent panel is given extra height, which would make the button thin.
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 2, 0, 2)

        btn_label = method_name.replace("_", " ").title()
        btn = QPushButton(btn_label)
        btn.setObjectName(f"{self._vi_name}_{method_name}_btn")
        row.addWidget(btn)

        inputs: dict[str, QLineEdit] = {}
        for param_name, param_info in params.items():
            lbl = QLabel(f"{param_name}:")
            field = QLineEdit()
            field.setObjectName(f"{self._vi_name}_{method_name}_{param_name}_input")
            field.setPlaceholderText(param_name)
            field.setMaximumWidth(90)
            default = param_info.get("default")
            if default is not None:
                field.setText(str(default))
            inputs[param_name] = field
            row.addWidget(lbl)
            row.addWidget(field)

        self._control_inputs[method_name] = inputs
        row.addStretch()

        btn.clicked.connect(
            lambda checked=False, mn=method_name: self._submit_control(mn)
        )
        return container

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_states_updated(self, full_state: dict) -> None:
        """Update displayed values and border from the station state dict.

        Args:
            full_state: ``{vi_name: {field: value, ...}}`` from Orchestrator.
        """
        vi_state = full_state.get(self._vi_name, {})
        is_stale = vi_state.get("_stale", False)
        is_disconnected = vi_state.get("_disconnected", False)

        for method_name, label in self._value_labels.items():
            value = vi_state.get(method_name)
            if value is None:
                label.setText("—")
            elif isinstance(value, float):
                label.setText(f"{value:.5g}")
            else:
                label.setText(str(value))

        if is_disconnected:
            new_status = "disconnected"
        elif is_stale:
            new_status = "stale"
        else:
            new_status = "ok"

        # Only restyle when the status actually changes. Restyling every tick
        # would force a needless full repolish of the panel each 3 s.
        if new_status != self._status:
            self._status = new_status
            self.setProperty("status", new_status)
            # Qt only re-evaluates property-based QSS selectors (e.g.
            # QGroupBox[status="stale"]) after an unpolish/polish cycle;
            # setProperty alone does not repaint.
            self.style().unpolish(self)
            self.style().polish(self)

            if new_status == "disconnected":
                self.setTitle(f"{self._vi_name}  [DISCONNECTED]")
            elif new_status == "stale":
                self.setTitle(f"{self._vi_name}  [stale]")
            else:
                self.setTitle(self._vi_name)

    def _on_action_blocked(self, message: str) -> None:
        """Show a dialog when the Orchestrator blocks a GUI action.

        Only shown if the blocked action was for this VI.

        Args:
            message: Human-readable reason for the block.
        """
        if self._vi_name not in message:
            return
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.warning(self, "Action Blocked", message)

    # ------------------------------------------------------------------
    # Action dispatch
    # ------------------------------------------------------------------

    def _submit_control(self, method_name: str) -> None:
        """Read input fields and submit a @control action to the Orchestrator.

        Args:
            method_name: The @control method to call.
        """
        inputs = self._control_inputs.get(method_name, {})
        vi_method = getattr(self._vi, method_name, None)
        params_meta = getattr(vi_method, "_control_params", {}) if vi_method else {}

        kwargs: dict[str, Any] = {}
        for param_name, field in inputs.items():
            raw = field.text().strip()
            meta = params_meta.get(param_name, {})
            param_type = meta.get("type", str)
            has_default = "default" in meta

            if not raw:
                if not has_default:
                    from PyQt6.QtWidgets import QMessageBox
                    QMessageBox.warning(
                        self,
                        "Missing Parameter",
                        f"'{param_name}' is required for {method_name}.",
                    )
                    return
                continue  # omit; let the method use its own default

            try:
                kwargs[param_name] = param_type(raw)
            except (ValueError, TypeError):
                logger.warning(
                    "InstrumentPanel: could not coerce '%s' to %s for param '%s'",
                    raw,
                    param_type,
                    param_name,
                )
                kwargs[param_name] = raw

        self._orchestrator.submit_vi_action(self._vi_name, method_name, **kwargs)

    def _submit_lifecycle(self, action: str) -> None:
        """Submit an initiate or standby action for this VI.

        Args:
            action: ``"initiate"`` or ``"standby"``.
        """
        self._orchestrator.submit_vi_action(self._vi_name, action)
