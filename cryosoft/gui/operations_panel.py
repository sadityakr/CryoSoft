# ---
# description: |
#   CryogenicsPanel: MonitorWindow page 1's optional bottom-right selector
#   entry (alongside "Other devices"), built only when the active config
#   declares a cryogenics: block AND the station has the named level VI
#   (docs/plans/cryogenics-logbook.md §9/§10). Shows current He/N2 levels
#   (from states_updated), consumption (%/h, optionally L/h) over a
#   selectable window, a level-vs-time plot with gaps rendered as gaps and
#   fill events overlaid, and the Fill helium / Stop filling control.
# entry_point: Not run directly. Built by MonitorWindow._build_bottom_right_quadrant
#   only when cryogenics is enabled.
# dependencies:
#   - PyQt6 >= 6.5
#   - pyqtgraph >= 0.13
#   - qtawesome
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.procedures.operations.helium_fill (HeliumFillOperation) — the
#     GUI importing an operation class is allowed (see gui/README.md and
#     pyproject.toml contract C8, which only forbids drivers/concrete VIs)
#   - cryosoft.session.servicing_log (HeliumRecordStore, ServicingLogStore,
#     consumption_rate_pct_per_h)
# input: |
#   The active cryogenics: config (Station.read_cryogenics_config), a
#   HeliumRecordStore and ServicingLogStore, per-tick state snapshots via
#   on_states_updated(), and Orchestrator run_started/run_finished/
#   action_blocked (connected directly here, following the same precedent as
#   OtherDevicesPanel's own action_succeeded connection — only states_updated
#   routes through the window, see monitor_window.py's teardown-race note).
# process: |
#   on_states_updated() updates the He/N2 readouts and (throttled to avoid
#   re-reading the helium-record file on every tick) recomputes the
#   consumption rate and redraws the level-vs-time plot, which breaks the
#   curve (does not interpolate) across any gap wider than twice the
#   configured history_sample_s. Fill helium opens a small operator-name
#   dialog, then constructs HeliumFillOperation(station, person=...,
#   data_directory=..., **cryogenics_config) and calls
#   orchestrator.run_operation(); the button becomes Stop filling (tracked
#   via run_started/run_finished manifests named "Helium Fill") and calls
#   orchestrator.finish_operation().
# output: |
#   A QWidget hosted (scrolled) in MonitorWindow's bottom-right quadrant.
#   Side effect: submits an operation to the Orchestrator.
# last_updated: 2026-07-19
# ---

"""CryogenicsPanel — live He/N2 levels, consumption, trend, and Fill helium."""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pyqtgraph as pg
import qtawesome as qta
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.station import Station
from cryosoft.gui.theme import BTN_CLASS_DANGER, BTN_CLASS_PRIMARY, PLOT_SERIES, TEXT_PRIMARY
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
from cryosoft.session.servicing_log import consumption_rate_pct_per_h

if TYPE_CHECKING:
    from collections.abc import Callable

    from cryosoft.session.servicing_log import HeliumRecordStore, ServicingLogStore

logger = logging.getLogger(__name__)

__all__ = ["CryogenicsPanel", "FillOperatorDialog"]

# Consumption / plot time-window options (plan §10: 1 h / 6 h / 24 h).
_CONSUMPTION_WINDOWS: list[tuple[str, float]] = [
    ("1 h", 3600.0),
    ("6 h", 21600.0),
    ("24 h", 86400.0),
]
_DEFAULT_WINDOW_LABEL = "1 h"

# Minimum real seconds between consumption/plot recomputes, so a fast
# Orchestrator tick does not re-read the helium-record file every 3 s in
# production. States_updated still drives every call; this only throttles
# the (comparatively expensive) file read + refit inside it — not a QTimer.
_RECOMPUTE_MIN_INTERVAL_S = 5.0


class FillOperatorDialog(QDialog):
    """Small dialog asking for the operator name before starting a fill.

    Args:
        prefill: Initial text for the operator-name field (typically the
            active experiment's user).
        parent: Optional Qt parent widget.
    """

    def __init__(self, prefill: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Fill Helium")
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Force all magnets to zero field and fill the helium reservoir."))
        form = QFormLayout()
        self._name_edit = QLineEdit(prefill)
        self._name_edit.setObjectName("fill_operator_name_input")
        form.addRow("Operator name:", self._name_edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def operator_name(self) -> str:
        """Return the entered operator name, stripped."""
        return self._name_edit.text().strip()


class CryogenicsPanel(QWidget):
    """Live cryogenics status: levels, consumption, trend plot, Fill helium.

    Args:
        station: The active Station (passed to ``HeliumFillOperation``).
        orchestrator: The active Orchestrator (``run_operation``,
            ``finish_operation``, and the ``run_started``/``run_finished``
            signals this panel connects directly, per the existing
            OtherDevicesPanel precedent).
        cryogenics_config: The resolved ``cryogenics:`` block
            (``Station.read_cryogenics_config()``'s result — every key
            defaulted).
        helium_store: Where the hourly helium/nitrogen samples live.
        servicing_store: Where cryogenics-log fill entries live (used to
            overlay fill markers and exclude fill intervals from the
            consumption fit).
        get_data_dir: Callable returning the app's configured data directory,
            passed to ``HeliumFillOperation`` as ``data_directory``.
        get_current_person: Callable returning the attribution prefill for
            the Fill dialog.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        cryogenics_config: dict[str, Any],
        helium_store: HeliumRecordStore,
        servicing_store: ServicingLogStore | None,
        get_data_dir: Callable[[], str],
        get_current_person: Callable[[], str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cryogenics_panel")
        self._station = station
        self._orchestrator = orchestrator
        self._cryogenics_config = dict(cryogenics_config)
        self._helium_store = helium_store
        self._servicing_store = servicing_store
        self._get_data_dir = get_data_dir
        self._get_current_person = get_current_person or (lambda: "")

        self._level_vi_name: str = str(cryogenics_config.get("level_vi", "level_meter"))
        volume = cryogenics_config.get("helium_volume_l")
        self._helium_volume_l: float | None = float(volume) if volume else None
        self._history_sample_s: float = float(cryogenics_config.get("history_sample_s", 3600.0))
        self._gap_threshold_s: float = 2.0 * self._history_sample_s

        self._fill_running = False
        self._last_recompute_mono: float | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        outer.setSpacing(4)
        outer.addWidget(QLabel("<b>Cryogenics</b>"))

        levels_row = QHBoxLayout()
        self._helium_label = QLabel("He: — %")
        self._helium_label.setObjectName("cryo_helium_level_label")
        self._helium_label.setProperty("class", "value_readout")
        levels_row.addWidget(self._helium_label)
        self._nitrogen_label = QLabel("N₂: — %")
        self._nitrogen_label.setObjectName("cryo_nitrogen_level_label")
        self._nitrogen_label.setProperty("class", "value_readout")
        levels_row.addWidget(self._nitrogen_label)
        levels_row.addStretch()
        outer.addLayout(levels_row)

        consumption_row = QHBoxLayout()
        consumption_row.addWidget(QLabel("Consumption window:"))
        self._window_combo = QComboBox()
        self._window_combo.setObjectName("cryo_window_combo")
        for label, _seconds in _CONSUMPTION_WINDOWS:
            self._window_combo.addItem(label)
        self._window_combo.setCurrentText(_DEFAULT_WINDOW_LABEL)
        self._window_combo.currentTextChanged.connect(self._recompute)
        consumption_row.addWidget(self._window_combo)
        self._consumption_label = QLabel("Consumption: —")
        self._consumption_label.setObjectName("cryo_consumption_label")
        consumption_row.addWidget(self._consumption_label)
        consumption_row.addStretch()
        outer.addLayout(consumption_row)

        self._plot_widget = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._plot_widget.setObjectName("cryo_plot")
        self._plot_widget.setMinimumHeight(140)
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.setLabel("left", "Helium (%)")
        level_pen = pg.mkPen(PLOT_SERIES[0], width=2)
        # connect='finite' breaks the line at any inserted NaN point (see
        # _build_gapped_series) — the gap is never bridged by a straight
        # line, matching "gaps rendered as gaps, no interpolation" (plan §10).
        self._curve = self._plot_widget.plot([], [], pen=level_pen, connect="finite")
        self._fill_markers = pg.ScatterPlotItem(
            symbol="t1", size=12, brush=pg.mkBrush(PLOT_SERIES[1]), pen=None
        )
        self._plot_widget.addItem(self._fill_markers)
        outer.addWidget(self._plot_widget)

        fill_row = QHBoxLayout()
        self._fill_btn = QPushButton()
        self._fill_btn.setObjectName("cryo_fill_btn")
        self._fill_btn.clicked.connect(self._on_fill_clicked)
        fill_row.addWidget(self._fill_btn)
        fill_row.addStretch()
        outer.addLayout(fill_row)
        self._sync_fill_button()

        # Direct connection (not routed through the window's states_updated
        # forwarding): run_started/run_finished fire only at run boundaries,
        # not on every tick, so there is no teardown-race concern — the same
        # precedent OtherDevicesPanel's action_succeeded connection follows.
        self._orchestrator.run_started.connect(self._on_run_started)
        self._orchestrator.run_finished.connect(self._on_run_finished)

        self._recompute()

    # ------------------------------------------------------------------
    # Live updates
    # ------------------------------------------------------------------

    def on_states_updated(self, state: dict[str, Any]) -> None:
        """Refresh the He/N2 readouts and (throttled) the consumption/plot.

        Args:
            state: ``{vi_name: {field: value, ...}}`` from the Orchestrator.
        """
        vi_state = state.get(self._level_vi_name)
        if not isinstance(vi_state, dict):
            return
        helium = vi_state.get("helium_level")
        nitrogen = vi_state.get("nitrogen_level")
        if isinstance(helium, (int, float)) and not isinstance(helium, bool):
            self._helium_label.setText(f"He: {helium:.1f} %")
        if isinstance(nitrogen, (int, float)) and not isinstance(nitrogen, bool):
            self._nitrogen_label.setText(f"N₂: {nitrogen:.1f} %")

        now_mono = time.monotonic()
        if (
            self._last_recompute_mono is None
            or (now_mono - self._last_recompute_mono) >= _RECOMPUTE_MIN_INTERVAL_S
        ):
            self._recompute()
            self._last_recompute_mono = now_mono

    def _recompute(self) -> None:
        """Recompute the consumption rate and redraw the level plot."""
        samples = self._helium_store.samples() if self._helium_store is not None else []
        window_s = dict(_CONSUMPTION_WINDOWS).get(
            self._window_combo.currentText(), dict(_CONSUMPTION_WINDOWS)[_DEFAULT_WINDOW_LABEL]
        )
        now = time.time()
        fill_intervals = self._fill_intervals()
        rate = consumption_rate_pct_per_h(samples, window_s, now, fill_intervals)
        if rate is None:
            self._consumption_label.setText("Consumption: —")
        else:
            text = f"Consumption: {rate:.2f} %/h"
            if self._helium_volume_l:
                text += f" ({rate * self._helium_volume_l / 100.0:.2f} L/h)"
            self._consumption_label.setText(text)

        xs, ys = _build_gapped_series(samples, self._gap_threshold_s)
        self._curve.setData(xs, ys)

        marker_x, marker_y = self._fill_marker_points()
        self._fill_markers.setData(marker_x, marker_y)

    def _fill_intervals(self) -> tuple[tuple[float, float], ...]:
        """Return ``(start_unix, end_unix)`` for every cryogenics-log fill entry."""
        if self._servicing_store is None:
            return ()
        intervals: list[tuple[float, float]] = []
        for entry in self._servicing_store.entries("cryogenics"):
            try:
                start = datetime.fromisoformat(str(entry.values.get("start_utc"))).timestamp()
                end = datetime.fromisoformat(str(entry.values.get("end_utc"))).timestamp()
            except (TypeError, ValueError):
                continue
            intervals.append((start, end))
        return tuple(intervals)

    def _fill_marker_points(self) -> tuple[list[float], list[float]]:
        """Return marker (x, y) points at each fill's start time/level."""
        if self._servicing_store is None:
            return [], []
        xs: list[float] = []
        ys: list[float] = []
        for entry in self._servicing_store.entries("cryogenics"):
            try:
                start = datetime.fromisoformat(str(entry.values.get("start_utc"))).timestamp()
                level = float(entry.values.get("helium_start_pct", 0.0))
            except (TypeError, ValueError):
                continue
            xs.append(start)
            ys.append(level)
        return xs, ys

    # ------------------------------------------------------------------
    # Fill helium / Stop filling
    # ------------------------------------------------------------------

    def _on_fill_clicked(self) -> None:
        if self._fill_running:
            self._orchestrator.finish_operation()
            return
        dialog = FillOperatorDialog(self._get_current_person(), parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        person = dialog.operator_name()
        operation = HeliumFillOperation(
            self._station,
            person=person,
            data_directory=self._get_data_dir(),
            **self._cryogenics_config,
        )
        self._orchestrator.run_operation(operation)

    def _on_run_started(self, manifest: dict[str, Any]) -> None:
        if str(manifest.get("procedure", "")) != HeliumFillOperation.name:
            return
        self._fill_running = True
        self._sync_fill_button()

    def _on_run_finished(self, manifest: dict[str, Any]) -> None:
        if str(manifest.get("procedure", "")) != HeliumFillOperation.name:
            return
        self._fill_running = False
        self._sync_fill_button()

    def _sync_fill_button(self) -> None:
        if self._fill_running:
            self._fill_btn.setText("Stop filling")
            self._fill_btn.setProperty("class", BTN_CLASS_DANGER)
            self._fill_btn.setIcon(qta.icon("fa5s.stop", color=TEXT_PRIMARY))
            self._fill_btn.setToolTip("Request a graceful stop of the running helium fill")
        else:
            self._fill_btn.setText("Fill helium")
            self._fill_btn.setProperty("class", BTN_CLASS_PRIMARY)
            self._fill_btn.setIcon(qta.icon("fa5s.tint", color=TEXT_PRIMARY))
            self._fill_btn.setToolTip("Force all magnets to zero field and fill the helium reservoir")
        self._fill_btn.style().unpolish(self._fill_btn)
        self._fill_btn.style().polish(self._fill_btn)


def _build_gapped_series(
    samples: list[tuple[float, float, float]], gap_threshold_s: float
) -> tuple[list[float], list[float]]:
    """Build (x, y) arrays for the level curve, inserting NaN across gaps.

    A NaN point is inserted whenever consecutive samples are separated by
    more than ``gap_threshold_s`` — with the curve's ``connect='finite'``,
    this breaks the line rather than interpolating a false straight line
    across a period the app was closed or monitoring was off.

    Args:
        samples: ``(unix_time, helium_pct, nitrogen_pct)`` tuples, any order.
        gap_threshold_s: Gap size (seconds) above which a break is inserted.

    Returns:
        Parallel ``(x, y)`` lists, chronological order.
    """
    ordered = sorted(samples, key=lambda sample: sample[0])
    xs: list[float] = []
    ys: list[float] = []
    prev_t: float | None = None
    for t, helium, _nitrogen in ordered:
        if prev_t is not None and (t - prev_t) > gap_threshold_s:
            xs.append(prev_t + 1e-3)
            ys.append(float("nan"))
        xs.append(t)
        ys.append(helium)
        prev_t = t
    return xs, ys
