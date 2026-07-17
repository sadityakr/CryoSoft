# ---
# description: |
#   OtherDevicesPanel: the compact Other Devices section of MonitorWindow's
#   bottom-right quadrant — one connection-check row per measurement VI
#   (dot + status + Check button + LifecycleToggleButton) and one display-only
#   row per switch/scanner VI (dot + status + live active-route label).
#   Extracted from monitor_window.py.
# entry_point: Not run directly. Hosted inside MonitorWindow's Other Devices /
#   Log stacked quadrant.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.lifecycle_toggle (LifecycleToggleButton)
# input: |
#   Station (VI instances for ping/labels), Orchestrator (submit_vi_action,
#   action_succeeded), and per-tick state snapshots via on_states_updated().
# process: |
#   Rows stack vertically for up to three VIs total; a tight 3-column grid is
#   used beyond that so the quadrant's natural height stays small. Switch rows
#   refresh their connection dot/status and active route on every monitor tick.
# output: |
#   Live device-status rows; VI lifecycle actions submitted to the Orchestrator.
# ---

"""OtherDevicesPanel — measurement-VI check rows and display-only switch rows."""

from __future__ import annotations

import qtawesome as qta
from PyQt6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import Station
from cryosoft.gui.lifecycle_toggle import LifecycleToggleButton
from cryosoft.gui.theme import TEXT_PRIMARY


class OtherDevicesPanel(QWidget):
    """Compact status rows for measurement and switch VIs.

    Each measurement row is name + connection dot/status + a Check button and
    a LifecycleToggleButton; each switch row is the display-only analogue
    (name + display label + connection dot/status + live active route, no
    buttons) — a switch is monitored, not driven, from this section.

    The hosting window connects the Orchestrator's ``states_updated`` signal
    to :meth:`on_states_updated` so the switch rows track the live route.

    Args:
        station: The active Station instance.
        orchestrator: The active Orchestrator instance.
        measurement_vis: Names of measurement VIs to display.
        switch_vis: Names of switch/scanner VIs to display (display-only).
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        measurement_vis: list[str],
        switch_vis: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._station = station
        self._orchestrator = orchestrator

        # Live-updated switch VI row labels, keyed by VI name and refreshed on
        # every monitor tick by on_states_updated. Empty when the station has
        # no switch VIs.
        self._switch_route_labels: dict[str, QLabel] = {}
        self._switch_conn_dots: dict[str, QLabel] = {}
        self._switch_conn_status: dict[str, QLabel] = {}

        rows = [self._build_device_status_row(n) for n in measurement_vis]
        rows += [self._build_switch_status_row(n) for n in switch_vis]

        if not rows:
            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("No other devices configured."))
            return

        # Rows stack vertically for up to three VIs total; a tight 3-column
        # grid is used beyond that so the quadrant's natural height stays
        # small regardless of how many devices a station has.
        if len(rows) > 3:
            grid = QGridLayout(self)
            grid.setSpacing(6)
            columns = 3
            for idx, row_widget in enumerate(rows):
                row, col = divmod(idx, columns)
                grid.addWidget(row_widget, row, col)
        else:
            vlay = QVBoxLayout(self)
            vlay.setSpacing(4)
            vlay.setContentsMargins(6, 6, 6, 6)
            for row_widget in rows:
                vlay.addWidget(row_widget)
            vlay.addStretch()

    def _build_device_status_row(self, vi_name: str) -> QWidget:
        """Build one compact connection-check row for a measurement VI.

        A single ~32-40 px row: coloured dot + status text, then a small
        icon Check button and a LifecycleToggleButton.

        Args:
            vi_name: Registered VI name (e.g. ``"keithley_delta_mode"``).

        Returns:
            A QWidget containing the assembled row.
        """
        row_widget = QWidget()
        row_widget.setObjectName(f"{vi_name}_device_row")
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(8)

        # Connection dot: styled via the dynamic 'status' QSS property
        # (theme.py QLabel[class="conn_dot"][status=...]), never setStyleSheet.
        dot = QLabel("●")
        dot.setObjectName(f"{vi_name}_conn_dot")
        dot.setProperty("class", "conn_dot")
        dot.setProperty("status", "unknown")
        row.addWidget(dot)

        name_lbl = QLabel(vi_name)
        row.addWidget(name_lbl)

        status_lbl = QLabel("Unknown")
        status_lbl.setObjectName(f"{vi_name}_conn_status")
        status_lbl.setProperty("class", "secondary_label")
        row.addWidget(status_lbl)

        row.addStretch()

        check_btn = QPushButton()
        check_btn.setObjectName(f"{vi_name}_check_btn")
        check_btn.setIcon(qta.icon("fa5s.plug", color=TEXT_PRIMARY))
        check_btn.setToolTip(f"Send an identity query to test the {vi_name} connection")

        vi = self._station._virtual_instruments[vi_name]

        def _on_check(checked: bool = False, _vi=vi, _dot=dot, _lbl=status_lbl) -> None:
            try:
                ok = _vi.ping()
            except Exception:
                ok = False
            new_status = "connected" if ok else "disconnected"
            _dot.setProperty("status", new_status)
            # Qt only re-evaluates property-based QSS selectors after an
            # unpolish/polish cycle (same pattern InstrumentPanel uses).
            _dot.style().unpolish(_dot)
            _dot.style().polish(_dot)
            _lbl.setText("Connected" if ok else "Not reachable")

        check_btn.clicked.connect(_on_check)

        lifecycle = LifecycleToggleButton(
            vi_name,
            lambda action, n=vi_name: self._orchestrator.submit_vi_action(n, action),
            parent=row_widget,
        )

        def _on_action_succeeded(v: str, m: str, _lc=lifecycle, _n=vi_name) -> None:
            if v != _n:
                return
            if m == "initiate":
                _lc.set_initiated(True)
            elif m == "standby":
                _lc.set_initiated(False)

        self._orchestrator.action_succeeded.connect(_on_action_succeeded)

        row.addWidget(check_btn)
        row.addWidget(lifecycle)

        return row_widget

    def _build_switch_status_row(self, vi_name: str) -> QWidget:
        """Build one display-only status row for a switch/scanner VI.

        Same visual house style as :meth:`_build_device_status_row` (connection
        dot + status text), but with no Check button and no lifecycle toggle.
        Adds the VI's ``display_label`` (e.g. "Scanner (mux)") and a live
        active-route label that both refresh on the monitor tick via
        :meth:`on_states_updated`.

        Args:
            vi_name: Registered switch VI name (e.g. ``"switch_matrix"``).

        Returns:
            A QWidget containing the assembled display-only row.
        """
        row_widget = QWidget()
        row_widget.setObjectName(f"{vi_name}_switch_row")
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(8)

        # Connection dot: styled via the dynamic 'status' QSS property, same as
        # the measurement rows. Refreshed live from the state cache, so it
        # starts "unknown" until the first tick arrives.
        dot = QLabel("●")
        dot.setObjectName(f"{vi_name}_conn_dot")
        dot.setProperty("class", "conn_dot")
        dot.setProperty("status", "unknown")
        row.addWidget(dot)

        name_lbl = QLabel(vi_name)
        row.addWidget(name_lbl)

        label_lbl = QLabel(self._station.measurement_label(vi_name))
        label_lbl.setObjectName(f"{vi_name}_display_label")
        label_lbl.setProperty("class", "secondary_label")
        row.addWidget(label_lbl)

        row.addStretch()

        status_lbl = QLabel("Unknown")
        status_lbl.setObjectName(f"{vi_name}_conn_status")
        status_lbl.setProperty("class", "secondary_label")
        row.addWidget(status_lbl)

        route_lbl = QLabel("Route: —")
        route_lbl.setObjectName(f"{vi_name}_active_route")
        row.addWidget(route_lbl)

        self._switch_conn_dots[vi_name] = dot
        self._switch_conn_status[vi_name] = status_lbl
        self._switch_route_labels[vi_name] = route_lbl

        return row_widget

    def on_states_updated(self, state: dict) -> None:
        """Refresh the display-only switch rows from a per-tick state snapshot.

        Updates each switch VI's connection dot/status and live active-route
        label. Connection is derived the same way the rest of the GUI reads
        it: a ``_disconnected`` flag in the snapshot means "not reachable",
        otherwise a present snapshot means "connected". The active route shows
        the route name, or an em dash when no route is closed. No-op when
        there are no switch VIs (the label dicts are empty).

        Args:
            state: ``{vi_name: {field: value, ...}}`` from the Orchestrator.
        """
        for vi_name, route_lbl in self._switch_route_labels.items():
            vi_state = state.get(vi_name)
            if vi_state is None:
                continue

            route = vi_state.get("active_route", "")
            route_lbl.setText(f"Route: {route}" if route else "Route: —")

            dot = self._switch_conn_dots[vi_name]
            status_lbl = self._switch_conn_status[vi_name]
            if vi_state.get("_disconnected"):
                new_status, text = "disconnected", "Not reachable"
            else:
                new_status, text = "connected", "Connected"
            if dot.property("status") != new_status:
                dot.setProperty("status", new_status)
                # Qt re-evaluates property-based QSS selectors only after an
                # unpolish/polish cycle (same pattern the Check button uses).
                dot.style().unpolish(dot)
                dot.style().polish(dot)
            status_lbl.setText(text)
