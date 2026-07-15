# ---
# description: |
#   LivePlotPanel: a self-contained live-plot widget (QGroupBox) with X/Y-axis
#   selectors, an optional route selector (for multiplexed measurements), and a
#   themed pyqtgraph PlotWidget. Extracted from ProcedureWindow, which previously
#   duplicated Plot 1 / Plot 2 almost verbatim. Each panel owns its selectors and
#   curve, repopulates its axis choices from a key list, and redraws itself from
#   a datapoint history it is handed.
# entry_point: Not run directly. Instantiated by ProcedureWindow (two panels).
# dependencies:
#   - PyQt6 >= 6.5
#   - pyqtgraph >= 0.13
#   - numpy
# input: |
#   Constructor objectName strings (preserved so findChild-by-name still works),
#   a list of available axis keys via set_available_keys(), route names via
#   set_available_routes() (optional; hidden by default), and a datapoint
#   history (list of enriched dicts) via redraw().
# process: |
#   set_available_keys() repopulates X/Y selectors, preserving still-valid
#   selections. set_available_routes() shows/hides/enables the route selector
#   based on availability (hidden if empty, disabled if <2 routes, enabled if
#   >=2). redraw() extracts scalar X/Y series (suffixed with __<route> when a
#   route is selected) from the datapoint history and feeds them to the curve.
#   Changing any selector redraws against the last datapoint list the panel
#   was handed.
# output: |
#   A QGroupBox embedded in ProcedureWindow's bottom splitter, updating live.
# ---

"""LivePlotPanel — reusable live X/Y plot panel for ProcedureWindow."""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger(__name__)


def _extract_scalar(raw: Any) -> float:
    """Convert a datapoint value to a plottable float.

    A measurement value may be a scalar or an array (e.g. a buffer of samples);
    arrays are reduced to their mean so a single point can be plotted.

    Args:
        raw: The raw value pulled from a datapoint dict (may be ``None``,
            a scalar, or an array-like).

    Returns:
        The value as a float, or NaN when the value is ``None``.
    """
    if raw is None:
        return float("nan")
    return float(np.mean(raw)) if hasattr(raw, "__len__") else float(raw)


class LivePlotPanel(QGroupBox):
    """A live X/Y plot panel: two axis selectors, an optional route selector, and a themed pyqtgraph curve.

    This is a *widget extraction* — the pattern of pulling a repeated block of
    UI (here, ProcedureWindow's near-identical Plot 1 / Plot 2) into one
    reusable widget class so the two instances stay in lock-step and the parent
    shrinks. Each panel keeps a reference to the last datapoint history handed to
    ``redraw`` so that changing an axis selector can immediately redraw without
    the parent re-supplying the data. The route selector is optional and hidden
    by default; call ``set_available_routes`` to show it for multiplexed measurements.

    Args:
        title: Group-box title (e.g. ``"Plot 1"``).
        series_color: Hex colour for the pen and symbols (from ``PLOT_SERIES``).
        x_selector_name: objectName for the X-axis combo (must match the legacy
            name, e.g. ``"x1_axis_selector"``, so ``findChild`` keeps working).
        y_selector_name: objectName for the Y-axis combo (e.g. ``"y_axis_selector"``).
        route_selector_name: objectName for the route combo (e.g. ``"route1_selector"``).
        plot_object_name: objectName for the PlotWidget (e.g. ``"live_plot"``).
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        title: str,
        series_color: str,
        x_selector_name: str,
        y_selector_name: str,
        route_selector_name: str,
        plot_object_name: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)

        # Last datapoint history handed to redraw(); a selector change redraws
        # against this without the parent re-supplying it.
        self._datapoints: list[dict] = []

        vlay = QVBoxLayout(self)

        axis_row = QHBoxLayout()
        axis_row.addWidget(QLabel("X axis:"))
        self._x_selector = QComboBox()
        self._x_selector.setObjectName(x_selector_name)
        self._x_selector.currentTextChanged.connect(self._redraw)
        axis_row.addWidget(self._x_selector)

        axis_row.addWidget(QLabel("Y axis:"))
        self._y_selector = QComboBox()
        self._y_selector.setObjectName(y_selector_name)
        self._y_selector.currentTextChanged.connect(self._redraw)
        axis_row.addWidget(self._y_selector)

        self._route_label = QLabel("Route:")
        axis_row.addWidget(self._route_label)
        self._route_selector = QComboBox()
        self._route_selector.setObjectName(route_selector_name)
        self._route_selector.currentTextChanged.connect(self._redraw)
        axis_row.addWidget(self._route_selector)

        axis_row.addStretch()
        vlay.addLayout(axis_row)

        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setObjectName(plot_object_name)
        self._plot_widget.setMinimumHeight(150)
        self._plot_widget.setLabel("bottom", "Field (T)")
        self._plot_widget.setLabel("left", "Value")
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        pen = pg.mkPen(series_color, width=2)
        self._curve = self._plot_widget.plot(
            [], [], pen=pen, symbol="o", symbolSize=5,
            symbolBrush=series_color, symbolPen=series_color,
        )
        vlay.addWidget(self._plot_widget)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_available_keys(
        self, keys: list[str], default_x: str, default_y: str | None
    ) -> None:
        """Repopulate both axis selectors, preserving a still-valid selection.

        Safe to call before a run starts. When a selector's current choice is
        still present in the new key list it is kept, so the user's axis choices
        survive switching procedures. ``blockSignals`` prevents a redraw storm
        while the combos are being rebuilt.

        Args:
            keys: The full list of selectable axis keys.
            default_x: The X-axis key to fall back to when the current X choice
                is not in ``keys``.
            default_y: The Y-axis key to fall back to when the current Y choice
                is not in ``keys``, or ``None`` for no Y default.
        """
        if not keys:
            return

        for sel in (self._x_selector, self._y_selector):
            prev = sel.currentText()
            sel.blockSignals(True)
            sel.clear()
            sel.addItems(keys)
            sel.setCurrentText(prev if prev in keys else keys[0])
            sel.blockSignals(False)

        # Apply sensible defaults only when the current selection is blank/unknown.
        if self._x_selector.currentText() not in keys:
            self._x_selector.setCurrentText(default_x)
        if (
            default_y is not None
            and self._y_selector.currentText() not in keys
            and default_y in keys
        ):
            self._y_selector.setCurrentText(default_y)

    def set_available_routes(self, routes: list[str] | None) -> None:
        """Show/hide/enable the route selector based on multiplexed measurement availability.

        When no scanner VI is present on the station, routes is None — selector
        is hidden (not needed, declutters the UI). When a scanner is present but
        fewer than 2 routes are selected, selector is visible but disabled (switch
        is available, but no multiplexing is active; pass empty list). When 2+ routes
        are selected, selector is enabled and lists the route names, letting the
        user pick which route's data to plot.

        Args:
            routes: List of selected route names, empty list if scanner present but
                no routes selected, or None if no scanner VI on station.
        """
        if routes is None:
            self._route_label.setVisible(False)
            self._route_selector.setVisible(False)
            return

        self._route_label.setVisible(True)
        self._route_selector.setVisible(True)

        if len(routes) < 2:
            self._route_selector.setEnabled(False)
            self._route_selector.blockSignals(True)
            self._route_selector.clear()
            self._route_selector.blockSignals(False)
        else:
            self._route_selector.setEnabled(True)
            prev = self._route_selector.currentText()
            self._route_selector.blockSignals(True)
            self._route_selector.clear()
            self._route_selector.addItems(routes)
            if prev in routes:
                self._route_selector.setCurrentText(prev)
            else:
                self._route_selector.setCurrentIndex(0)
            self._route_selector.blockSignals(False)

    def redraw(self, datapoints: list[dict]) -> None:
        """Store the datapoint history and redraw the curve from it.

        Args:
            datapoints: Full datapoint history (each entry an enriched dict).
        """
        self._datapoints = datapoints
        self._redraw()

    def clear(self) -> None:
        """Empty the curve (does not touch the selectors)."""
        self._datapoints = []
        self._curve.setData([], [])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _redraw(self) -> None:
        """Redraw the curve from the stored datapoint history and relabel axes."""
        x_key = self._x_selector.currentText()
        y_key = self._y_selector.currentText()

        route = ""
        if (
            self._route_selector.isVisible()
            and self._route_selector.isEnabled()
            and self._route_selector.currentText()
        ):
            route = self._route_selector.currentText()

        # Extract X and Y series. When a route is selected, try the suffixed key
        # first (for measurement columns), then fall back to unsuffixed (for sweep
        # columns, system state, etc. which are not multiplexed).
        xs = []
        ys = []
        for dp in self._datapoints:
            if route:
                x_val = dp.get(f"{x_key}__{route}")
                if x_val is None:
                    x_val = dp.get(x_key)
                y_val = dp.get(f"{y_key}__{route}")
                if y_val is None:
                    y_val = dp.get(y_key)
            else:
                x_val = dp.get(x_key)
                y_val = dp.get(y_key)
            xs.append(_extract_scalar(x_val))
            ys.append(_extract_scalar(y_val))

        self._curve.setData(xs, ys)
        x_label = x_key.replace("_", " ")
        y_label = y_key.replace("_", " ")
        if route:
            x_label += f" ({route})"
            y_label += f" ({route})"
        self._plot_widget.setLabel("bottom", x_label)
        self._plot_widget.setLabel("left", y_label)
