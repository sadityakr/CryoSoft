# ---
# description: |
#   LivePlotPanel: a self-contained live-plot widget (QGroupBox) with X/Y-axis
#   selectors, two optional reading-loop selectors (Loop 1 / Loop 2, one per
#   loop slot), and a themed pyqtgraph PlotWidget. Extracted from ProcedureWindow, which previously
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
#   a list of available axis keys via set_available_keys(), per-slot
#   reading-loop label maps via set_available_loop_labels() (optional; hidden
#   by default), a datapoint history (list of enriched dicts) via redraw(),
#   and a previously exported selection dict via restore_selection().
# process: |
#   set_available_keys() repopulates X/Y selectors, preserving still-valid
#   selections. set_available_loop_labels() shows/hides/enables each Loop
#   selector from its slot's label map (hidden if None, disabled if <2
#   entries, enabled if >=2). redraw() extracts scalar X/Y series
#   (suffix-composed as __A<i>__B<j> from the selected loop readings, with
#   progressive fallback for unsuffixed columns) from the datapoint history
#   and feeds them to the curve.
#   Changing any selector redraws against the last datapoint list the panel
#   was handed. export_selection()/restore_selection() round-trip the X/Y/Loop
#   choices as plain strings so ProcedureWindow can fold them into the GUI's
#   form-autosave SessionState (gui/form_autosave.py) — restore is a no-op for
#   any value not present in the selectors' current items.
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
    """A live X/Y plot panel: axis selectors, per-slot Loop selectors, and a themed pyqtgraph curve.

    This is a *widget extraction* — the pattern of pulling a repeated block of
    UI (here, ProcedureWindow's near-identical Plot 1 / Plot 2) into one
    reusable widget class so the two instances stay in lock-step and the parent
    shrinks. Each panel keeps a reference to the last datapoint history handed to
    ``redraw`` so that changing an axis selector can immediately redraw without
    the parent re-supplying the data. The two reading-loop selectors (one per
    loop slot) are hidden by default; call ``set_available_loop_labels`` to
    show them for looped measurements. Axis keys stay the PLAIN column names —
    the selected loop readings are composed onto them at draw time
    (``{key}__A{i}__B{j}``, with fallback for unsuffixed columns).

    Args:
        title: Group-box title (e.g. ``"Plot 1"``).
        series_color: Hex colour for the pen and symbols (from ``PLOT_SERIES``).
        x_selector_name: objectName for the X-axis combo (must match the legacy
            name, e.g. ``"x1_axis_selector"``, so ``findChild`` keeps working).
        y_selector_name: objectName for the Y-axis combo (e.g. ``"y_axis_selector"``).
        plot_object_name: objectName for the PlotWidget (e.g. ``"live_plot"``).
        loop1_selector_name: objectName for the slot-1 Loop combo
            (e.g. ``"plot1_loop1_selector"``).
        loop2_selector_name: objectName for the slot-2 Loop combo
            (e.g. ``"plot1_loop2_selector"``).
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        title: str,
        series_color: str,
        x_selector_name: str,
        y_selector_name: str,
        plot_object_name: str,
        loop1_selector_name: str = "",
        loop2_selector_name: str = "",
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

        # Reading-loop selectors, one per loop slot: each picks WHICH reading
        # of the datapoint is plotted (slot 1 labels A1, A2, ...; slot 2
        # labels B1, B2, ...). Hidden until set_available_loop_labels() is
        # called with a non-None map for the slot.
        self._loop_labels_ui: list[QLabel] = []
        self._loop_selectors: list[QComboBox] = []
        for ordinal, object_name in (
            ("1", loop1_selector_name or "loop1_selector"),
            ("2", loop2_selector_name or "loop2_selector"),
        ):
            label = QLabel(f"Loop {ordinal}:")
            axis_row.addWidget(label)
            selector = QComboBox()
            selector.setObjectName(object_name)
            selector.currentIndexChanged.connect(self._redraw)
            axis_row.addWidget(selector)
            label.setVisible(False)
            selector.setVisible(False)
            self._loop_labels_ui.append(label)
            self._loop_selectors.append(selector)

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

    def set_available_loop_labels(
        self, label_maps: tuple[dict[str, str] | None, dict[str, str] | None]
    ) -> None:
        """Show/hide/enable the two Loop selectors, one per reading-loop slot.

        Per slot: ``None`` means the selection offers no loop at all — the
        selector is hidden. ``{}`` means a loop is possible but that slot is
        off/static/invalid — visible but disabled. With two or more entries
        the selector is enabled: each item shows the display text (e.g.
        ``"A1 = Mux-Ch1"``) and carries the bare suffix label (``"A1"``) as
        its item data, which ``_redraw`` uses to pick the column.

        Args:
            label_maps: One ordered ``{suffix_label: display_text}`` map (or
                ``{}`` / ``None``) per loop slot, in slot order.
        """
        for label_widget, selector, labels in zip(
            self._loop_labels_ui, self._loop_selectors, label_maps
        ):
            if labels is None:
                label_widget.setVisible(False)
                selector.setVisible(False)
                continue
            label_widget.setVisible(True)
            selector.setVisible(True)
            if len(labels) < 2:
                selector.setEnabled(False)
                selector.blockSignals(True)
                selector.clear()
                selector.blockSignals(False)
            else:
                selector.setEnabled(True)
                prev = selector.currentData()
                selector.blockSignals(True)
                selector.clear()
                for label, display in labels.items():
                    selector.addItem(display, label)
                index = selector.findData(prev)
                selector.setCurrentIndex(index if index >= 0 else 0)
                selector.blockSignals(False)
        self._redraw()

    def export_selection(self) -> dict[str, str]:
        """Return this panel's current X/Y/Loop selections as plain strings.

        Returns:
            A dict with keys ``x``, ``y``, ``loop1``, ``loop2``. Loop values
            are the selector's item data (the bare suffix label, e.g.
            ``"A1"``), or ``""`` when that slot has no selection (hidden,
            disabled, or empty).
        """
        return {
            "x": self._x_selector.currentText(),
            "y": self._y_selector.currentText(),
            "loop1": str(self._loop_selectors[0].currentData() or ""),
            "loop2": str(self._loop_selectors[1].currentData() or ""),
        }

    def restore_selection(self, selection: dict[str, str]) -> None:
        """Apply a previously exported X/Y/Loop selection, defensively.

        Safe to call any time after ``set_available_keys``/
        ``set_available_loop_labels`` have populated the selectors: a value
        not present among the current items is silently ignored, leaving
        whatever default was already chosen.

        Args:
            selection: A dict as returned by ``export_selection`` (or a
                loaded/partial one — missing or unrecognised keys are
                no-ops).
        """
        x = selection.get("x")
        if x and self._x_selector.findText(x) >= 0:
            self._x_selector.setCurrentText(x)
        y = selection.get("y")
        if y and self._y_selector.findText(y) >= 0:
            self._y_selector.setCurrentText(y)
        for selector, key in zip(self._loop_selectors, ("loop1", "loop2")):
            value = selection.get(key)
            if not value:
                continue
            index = selector.findData(value)
            if index >= 0:
                selector.setCurrentIndex(index)
        self._redraw()

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

        # Selected suffix label per loop slot ("" when that slot is inactive).
        picked: list[str] = []
        for selector in self._loop_selectors:
            if (
                selector.isVisible()
                and selector.isEnabled()
                and selector.currentData()
            ):
                picked.append(str(selector.currentData()))
            else:
                picked.append("")
        slot1, slot2 = picked

        # Candidate suffixes, most-specific first, matching the reading-loop
        # column composition {name}__A{i}__B{j}. The trailing plain key is the
        # fallback for sweep columns, system state, etc., which the reading
        # loop never suffixes.
        suffixes: list[str] = []
        if slot1 and slot2:
            suffixes.append(f"__{slot1}__{slot2}")
        if slot1:
            suffixes.append(f"__{slot1}")
        if slot2:
            suffixes.append(f"__{slot2}")
        suffixes.append("")

        def _lookup(dp: dict, key: str):
            for suffix in suffixes:
                value = dp.get(f"{key}{suffix}")
                if value is not None:
                    return value
            return None

        xs = []
        ys = []
        for dp in self._datapoints:
            xs.append(_extract_scalar(_lookup(dp, x_key)))
            ys.append(_extract_scalar(_lookup(dp, y_key)))

        self._curve.setData(xs, ys)
        x_label = x_key.replace("_", " ")
        y_label = y_key.replace("_", " ")
        qualifiers = ", ".join(q for q in picked if q)
        if qualifiers:
            x_label += f" ({qualifiers})"
            y_label += f" ({qualifiers})"
        self._plot_widget.setLabel("bottom", x_label)
        self._plot_widget.setLabel("left", y_label)
