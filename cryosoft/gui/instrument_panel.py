# ---
# description: |
#   InstrumentPanel: auto-generated QGroupBox for one Virtual Instrument.
#   Reads @monitored methods to create live-updating QLabel displays and
#   @control methods to create control rows: a QPushButton plus one input
#   widget per parameter. Controls that declare ParamSpecs render through
#   gui/param_form (combo for choices, checkbox for bool, tooltipped and
#   unit-labelled fields); bare @control methods keep plain QLineEdits.
#   Connects to Orchestrator.states_updated for live updates each monitor tick.
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
#   The panel uses no native QGroupBox title — QGroupBox can't embed a widget
#   next to its title text, so the name + status suffix live in a QLabel in a
#   custom header row alongside the LifecycleToggleButton, keeping the toggle
#   compact instead of its own full-width row. The toggle's state only
#   changes on Orchestrator's action_succeeded signal, not on click, so it
#   reflects confirmed state.
# output: |
#   A QGroupBox with live values and control buttons embedded in MonitorWindow.
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

from cryosoft.core.decorators import (
    get_control_methods,
    get_control_panel,
    get_control_specs,
    get_monitored_methods,
)
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.gui.lifecycle_toggle import LifecycleToggleButton
from cryosoft.gui.param_form import (
    build_param_tooltip,
    build_param_widget,
    collect_value,
)
from cryosoft.virtual_instruments.base import BaseVirtualInstrument

logger = logging.getLogger(__name__)


class InstrumentPanel(QGroupBox):
    """Auto-generated display + control panel for one Virtual Instrument.

    Created by MonitorWindow for each VI registered in the Station. The
    layout is derived entirely from decorator metadata — no hardcoded
    per-instrument widget lists.

    Args:
        vi_name: The VI's registered name (e.g. ``"magnet_z"``).
        vi: The VI instance (used for introspection only).
        orchestrator: Orchestrator whose ``states_updated`` signal drives updates.
        parent: Optional Qt parent widget.
        panel_controls: Optional allowlist of @control method names to show
            (a setup's ``monitor.yaml`` ``panels:`` entry for this VI). When
            None, each control's own declared ``panel=`` default decides.
            Display-only — a hidden control stays fully functional in the
            instrument's front panel.
    """

    def __init__(
        self,
        vi_name: str,
        vi: BaseVirtualInstrument,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
        panel_controls: list[str] | None = None,
    ) -> None:
        super().__init__(parent)  # no native title — see module docstring
        self._vi_name = vi_name
        self._vi = vi
        self._orchestrator = orchestrator
        self._panel_controls = panel_controls

        # Maps field name → value label widget
        self._value_labels: dict[str, QLabel] = {}
        # Maps method_name → {param_name → input widget}. Widgets built by
        # param_form when the control declares ParamSpecs (combo/checkbox/
        # line-edit), plain QLineEdits otherwise.
        self._control_inputs: dict[str, dict[str, QWidget]] = {}
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
        orchestrator.action_succeeded.connect(self._on_action_succeeded)

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout()
        outer.setSpacing(4)
        outer.setContentsMargins(8, 8, 8, 8)

        # ── Header: name/status label + lifecycle toggle, one compact row ──
        header_row = QHBoxLayout()
        self._name_label = QLabel(f"<b>{self._vi_name}</b>")
        self._name_label.setObjectName(f"{self._vi_name}_name_label")
        self._name_label.setProperty("class", "panel_name_label")
        header_row.addWidget(self._name_label)
        header_row.addStretch()
        self._lifecycle = LifecycleToggleButton(self._vi_name, self._submit_lifecycle, parent=self)
        header_row.addWidget(self._lifecycle)
        outer.addLayout(header_row)

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
            if not self._control_visible(method_name):
                continue
            outer.addWidget(self._build_control_row(method_name, params))

        # Absorb any extra vertical space so control rows never expand beyond
        # their natural height when the panel is stretched by the grid.
        outer.addStretch()

        self.setLayout(outer)

    def _control_visible(self, method_name: str) -> bool:
        """Decide whether one @control appears on this compact card.

        A config allowlist (``panels:`` in ``monitor.yaml``) wins when given;
        otherwise the control's own ``panel=`` declaration decides.

        Args:
            method_name: The @control method name.

        Returns:
            True when the control's row should be built.
        """
        if self._panel_controls is not None:
            return method_name in self._panel_controls
        return get_control_panel(getattr(self._vi, method_name))

    def _build_control_row(self, method_name: str, params: dict) -> QWidget:
        """Build one @control method row: button + input widgets.

        A parameter covered by a declared ``ParamSpec`` gets its widget from
        ``param_form.build_param_widget`` (drop-down for ``choices``, checkbox
        for ``bool``, else a line edit) with the unit in its label and the
        description/default/range in a tooltip. Parameters without a spec keep
        the legacy plain ``QLineEdit`` seeded from the signature default.

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

        specs = get_control_specs(getattr(self._vi, method_name))
        inputs: dict[str, QWidget] = {}
        for param_name, param_info in params.items():
            spec = specs.get(param_name)
            if spec is not None:
                label_text = (
                    f"{param_name} ({spec.unit}):" if spec.unit else f"{param_name}:"
                )
                lbl = QLabel(label_text)
                field = build_param_widget(param_name, spec)
                tooltip = build_param_tooltip(spec)
                lbl.setToolTip(tooltip)
                field.setToolTip(tooltip)
                if isinstance(field, QLineEdit):
                    field.setMaximumWidth(90)
            else:
                lbl = QLabel(f"{param_name}:")
                field = QLineEdit()
                field.setPlaceholderText(param_name)
                field.setMaximumWidth(90)
                default = param_info.get("default")
                if default is not None:
                    field.setText(str(default))
            field.setObjectName(f"{self._vi_name}_{method_name}_{param_name}_input")
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
            self._name_label.setProperty("status", new_status)
            # Qt only re-evaluates property-based QSS selectors (e.g.
            # QGroupBox[status="stale"]) after an unpolish/polish cycle;
            # setProperty alone does not repaint.
            for widget in (self, self._name_label):
                widget.style().unpolish(widget)
                widget.style().polish(widget)

            if new_status == "disconnected":
                self._name_label.setText(f"<b>{self._vi_name}</b>  [DISCONNECTED]")
            elif new_status == "stale":
                self._name_label.setText(f"<b>{self._vi_name}</b>  [stale]")
            else:
                self._name_label.setText(f"<b>{self._vi_name}</b>")

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
        specs = get_control_specs(vi_method) if vi_method else {}

        kwargs: dict[str, Any] = {}
        for param_name, field in inputs.items():
            spec = specs.get(param_name)
            if spec is not None:
                # Spec-built widget: an emptied line edit falls back to the
                # method's own default (a ParamSpec always declares one); an
                # unparseable entry aborts the submit with an explicit verdict
                # instead of sending a wrong-typed value onward.
                if isinstance(field, QLineEdit) and not field.text().strip():
                    continue
                try:
                    kwargs[param_name] = collect_value(field, spec)
                except (ValueError, TypeError):
                    from PyQt6.QtWidgets import QMessageBox
                    QMessageBox.warning(
                        self,
                        "Invalid Parameter",
                        f"'{field.text().strip()}' is not a valid "
                        f"{spec.type.__name__} for '{param_name}' "
                        f"({method_name}).",
                    )
                    return
                continue

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

    def _on_action_succeeded(self, vi_name: str, method_name: str) -> None:
        """Flip the lifecycle toggle once Orchestrator confirms initiate/standby ran.

        Args:
            vi_name: The VI the confirmed action was submitted for.
            method_name: The confirmed method name.
        """
        if vi_name != self._vi_name:
            return
        if method_name == "initiate":
            self._lifecycle.set_initiated(True)
        elif method_name == "standby":
            self._lifecycle.set_initiated(False)
