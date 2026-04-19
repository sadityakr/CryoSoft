# ---
# description: |
#   ProcedureWindow: PyQt6 window for building, queuing, and running measurement
#   procedures. Auto-generates parameter forms from BaseProcedure.parameters dicts.
#   Two-column top layout (params left, queue right) with two live pyqtgraph plots
#   spanning full-width below. Sample info is read from MonitorWindow via callables.
# entry_point: Not run directly. Opened via MonitorWindow Procedures menu.
# dependencies:
#   - PyQt6 >= 6.5
#   - pyqtgraph >= 0.13
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.procedures.* (auto-discovered subclasses)
# input: |
#   Station instance, Orchestrator instance, and two callables (get_sample_info,
#   get_data_dir) provided by MonitorWindow.
# process: |
#   _discover_procedures() imports all modules in cryosoft/procedures/ and collects
#   BaseProcedure subclasses. The selected procedure's parameters dict drives form
#   generation. Queued procedures are stored as (cls, params) tuples. Execution
#   goes through orchestrator.run_procedure().
# output: |
#   A QMainWindow. Two live plots update via orchestrator.measurement_ready.
# last_updated: 2026-04-19
# ---

"""ProcedureWindow — procedure builder, queue, and live-data monitor."""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import Station
from cryosoft.gui.theme import BTN_CLASS_DANGER, BTN_CLASS_PRIMARY, BTN_CLASS_SECONDARY

logger = logging.getLogger(__name__)


def _discover_procedures() -> list[type[BaseProcedure]]:
    """Import all modules in cryosoft.procedures and return BaseProcedure subclasses.

    Returns:
        List of concrete BaseProcedure subclasses (not the base class itself).
    """
    import cryosoft.procedures as _pkg

    pkg_path = Path(_pkg.__file__).parent
    for _, module_name, _ in pkgutil.iter_modules([str(pkg_path)]):
        try:
            importlib.import_module(f"cryosoft.procedures.{module_name}")
        except Exception:
            logger.exception("ProcedureWindow: failed to import cryosoft.procedures.%s", module_name)

    subclasses: list[type[BaseProcedure]] = []
    for cls in BaseProcedure.__subclasses__():
        if cls is not BaseProcedure and getattr(cls, "name", ""):
            subclasses.append(cls)
    return subclasses


class ProcedureWindow(QMainWindow):
    """Procedure builder, queue manager, and live-data window.

    Layout:
    - Top: QSplitter(Horizontal) — left pane (selector + params + buttons),
      right pane (queue).
    - Bottom: QSplitter(Horizontal) — Plot 1 and Plot 2 side-by-side.
    - Full-width progress bar and control buttons below plots.

    Sample info and data directory are read from MonitorWindow via
    ``get_sample_info`` and ``get_data_dir`` callables injected at construction.

    Args:
        station: The active Station instance.
        orchestrator: The active Orchestrator instance.
        get_sample_info: Callable returning ``{sample_name, sample_id, comments}``.
        get_data_dir: Callable returning the data directory path string.
        parent: Optional Qt parent widget.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        get_sample_info: Callable[[], dict[str, str]],
        get_data_dir: Callable[[], str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._station = station
        self._orchestrator = orchestrator
        self._get_sample_info = get_sample_info
        self._get_data_dir = get_data_dir

        self._procedures: list[type[BaseProcedure]] = _discover_procedures()
        # Queue items: list of (procedure_class, params_dict, sample_info_dict, data_dir)
        self._queue: list[tuple[type[BaseProcedure], dict, dict, str]] = []
        # Widgets for the parameter form (rebuilt on procedure selection)
        self._param_inputs: dict[str, QLineEdit] = {}
        # Active procedure reference (set on run)
        self._active_procedure: BaseProcedure | None = None
        # Live plot: full datapoint history (each entry is the enriched dict from measurement_ready)
        self._datapoints: list[dict] = []

        self.setWindowTitle("CryoSoft — Procedure")
        self.resize(1200, 820)

        self._build_ui()
        self._connect_signals()

        if self._procedures:
            self._on_procedure_selected(0)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # ── Top: params (left) | queue (right) ───────────────────────
        root.addWidget(self._build_top_splitter(), stretch=2)

        # ── Bottom: Plot 1 | Plot 2 ───────────────────────────────────
        root.addWidget(self._build_plot_section_dual(), stretch=3)

        # ── Progress bar (full-width) ─────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("progress_bar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        root.addWidget(self._progress_bar)

        # ── Control buttons ───────────────────────────────────────────
        root.addLayout(self._build_control_buttons())

    def _build_top_splitter(self) -> QSplitter:
        """Build the horizontal splitter that separates params (left) and queue (right).

        Returns:
            QSplitter with left widget (selector + form + buttons) and
            right widget (queue).
        """
        splitter = QSplitter(Qt.Orientation.Horizontal)

        splitter.addWidget(self._build_left_column())
        splitter.addWidget(self._build_right_column())
        splitter.setSizes([720, 480])
        return splitter

    def _build_left_column(self) -> QWidget:
        """Build the left pane: procedure selector, parameter form, Add/Run buttons.

        Returns:
            QWidget containing the left-column layout.
        """
        widget = QWidget()
        col = QVBoxLayout(widget)
        col.setSpacing(8)
        col.setContentsMargins(0, 0, 4, 0)

        # ── Procedure selector ────────────────────────────────────────
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Procedure:"))
        self._proc_selector = QComboBox()
        self._proc_selector.setObjectName("procedure_selector")
        for cls in self._procedures:
            self._proc_selector.addItem(getattr(cls, "name", cls.__name__))
        self._proc_selector.currentIndexChanged.connect(self._on_procedure_selected)
        sel_row.addWidget(self._proc_selector)
        sel_row.addStretch()
        col.addLayout(sel_row)

        # ── Parameters (scroll area; rebuilt on selector change) ──────
        self._param_scroll = QScrollArea()
        self._param_scroll.setWidgetResizable(True)
        self._param_scroll.setMaximumHeight(250)
        col.addWidget(self._param_scroll)

        # ── Add / Run buttons ─────────────────────────────────────────
        action_row = QHBoxLayout()
        add_btn = QPushButton("Add to Queue")
        add_btn.setObjectName("add_to_queue_btn")
        add_btn.setProperty("class", BTN_CLASS_SECONDARY)
        add_btn.clicked.connect(self._on_add_to_queue)
        run_now_btn = QPushButton("Run Now")
        run_now_btn.setObjectName("run_now_btn")
        run_now_btn.setProperty("class", BTN_CLASS_PRIMARY)
        run_now_btn.clicked.connect(self._on_run_now)
        action_row.addWidget(add_btn)
        action_row.addWidget(run_now_btn)
        action_row.addStretch()
        col.addLayout(action_row)

        col.addStretch()
        return widget

    def _build_right_column(self) -> QWidget:
        """Build the right pane: queue list with management buttons.

        Returns:
            QWidget containing the right-column layout.
        """
        widget = QWidget()
        col = QVBoxLayout(widget)
        col.setSpacing(0)
        col.setContentsMargins(4, 0, 0, 0)
        col.addWidget(self._build_queue_section())
        return widget

    def _build_param_form(self, cls: type[BaseProcedure]) -> QWidget:
        """Auto-generate a three-column parameter form from the procedure's parameter groups.

        Renders sweep_parameters, system_parameters, and measurement_parameters
        in side-by-side QGroupBox panels so users can distinguish the different
        kinds of input at a glance.

        Args:
            cls: The BaseProcedure subclass to introspect.

        Returns:
            A QWidget containing the three-column form.
        """
        container = QWidget()
        hbox = QHBoxLayout(container)
        hbox.setSpacing(8)
        hbox.setContentsMargins(0, 0, 0, 0)
        self._param_inputs.clear()

        groups = [
            ("Sweep", cls.sweep_parameters),
            ("System", cls.system_parameters),
            ("Measurement", cls.measurement_parameters),
        ]

        for group_title, group_params in groups:
            box = QGroupBox(group_title)
            form = QFormLayout(box)
            form.setSpacing(4)
            for param_name, spec in group_params.items():
                unit = spec.get("unit", "")
                desc = spec.get("description", param_name)
                label_text = f"{desc} ({unit}):" if unit else f"{desc}:"
                field = QLineEdit(str(spec.get("default", "")))
                field.setObjectName(f"param_{param_name}_input")
                self._param_inputs[param_name] = field
                form.addRow(label_text, field)
            hbox.addWidget(box)

        return container

    def _build_queue_section(self) -> QGroupBox:
        """Build the queue group with list + reorder/remove buttons.

        Returns:
            A QGroupBox containing the queue list and management buttons.
        """
        box = QGroupBox("Queue")
        vlay = QVBoxLayout(box)

        self._queue_list = QListWidget()
        self._queue_list.setObjectName("queue_list")
        vlay.addWidget(self._queue_list)

        btn_row = QHBoxLayout()
        up_btn = QPushButton("↑")
        up_btn.setObjectName("queue_up_btn")
        up_btn.setMaximumWidth(40)
        up_btn.clicked.connect(self._queue_move_up)
        down_btn = QPushButton("↓")
        down_btn.setObjectName("queue_down_btn")
        down_btn.setMaximumWidth(40)
        down_btn.clicked.connect(self._queue_move_down)
        remove_btn = QPushButton("✕ Remove")
        remove_btn.setObjectName("queue_remove_btn")
        remove_btn.clicked.connect(self._queue_remove)
        run_queue_btn = QPushButton("Run Queue")
        run_queue_btn.setObjectName("run_queue_btn")
        run_queue_btn.setProperty("class", BTN_CLASS_PRIMARY)
        run_queue_btn.clicked.connect(self._orchestrator.run_queue)
        btn_row.addWidget(up_btn)
        btn_row.addWidget(down_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        btn_row.addWidget(run_queue_btn)
        vlay.addLayout(btn_row)

        return box

    def _build_plot_section_dual(self) -> QSplitter:
        """Build two side-by-side live-plot panels in a horizontal splitter.

        Returns:
            QSplitter containing Plot 1 (left) and Plot 2 (right).
        """
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_plot1())
        splitter.addWidget(self._build_plot2())
        return splitter

    def _build_plot1(self) -> QGroupBox:
        """Build the first live-plot panel (Y1 selector + PlotWidget).

        Returns:
            QGroupBox for Plot 1.
        """
        box = QGroupBox("Plot 1")
        vlay = QVBoxLayout(box)

        x_row = QHBoxLayout()
        x_row.addWidget(QLabel("X axis:"))
        self._x1_axis_selector = QComboBox()
        self._x1_axis_selector.setObjectName("x1_axis_selector")
        self._x1_axis_selector.currentTextChanged.connect(self._redraw_plot)
        x_row.addWidget(self._x1_axis_selector)
        x_row.addStretch()
        vlay.addLayout(x_row)

        y_row = QHBoxLayout()
        y_row.addWidget(QLabel("Y axis:"))
        self._y_axis_selector = QComboBox()
        self._y_axis_selector.setObjectName("y_axis_selector")
        self._y_axis_selector.currentTextChanged.connect(self._redraw_plot)
        y_row.addWidget(self._y_axis_selector)
        y_row.addStretch()
        vlay.addLayout(y_row)

        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setObjectName("live_plot")
        self._plot_widget.setMinimumHeight(150)
        self._plot_widget.setLabel("bottom", "Field (T)")
        self._plot_widget.setLabel("left", "Value")
        self._plot_curve1 = self._plot_widget.plot([], [], pen="c", symbol="o", symbolSize=4)
        vlay.addWidget(self._plot_widget)

        return box

    def _build_plot2(self) -> QGroupBox:
        """Build the second live-plot panel (Y2 selector + PlotWidget).

        Returns:
            QGroupBox for Plot 2.
        """
        box = QGroupBox("Plot 2")
        vlay = QVBoxLayout(box)

        x_row = QHBoxLayout()
        x_row.addWidget(QLabel("X axis:"))
        self._x2_axis_selector = QComboBox()
        self._x2_axis_selector.setObjectName("x2_axis_selector")
        self._x2_axis_selector.currentTextChanged.connect(self._redraw_plot2)
        x_row.addWidget(self._x2_axis_selector)
        x_row.addStretch()
        vlay.addLayout(x_row)

        y_row = QHBoxLayout()
        y_row.addWidget(QLabel("Y axis:"))
        self._y2_axis_selector = QComboBox()
        self._y2_axis_selector.setObjectName("y2_axis_selector")
        self._y2_axis_selector.currentTextChanged.connect(self._redraw_plot2)
        y_row.addWidget(self._y2_axis_selector)
        y_row.addStretch()
        vlay.addLayout(y_row)

        self._plot_widget2 = pg.PlotWidget()
        self._plot_widget2.setObjectName("live_plot_2")
        self._plot_widget2.setMinimumHeight(150)
        self._plot_widget2.setLabel("bottom", "Field (T)")
        self._plot_widget2.setLabel("left", "Value")
        self._plot_curve2 = self._plot_widget2.plot([], [], pen="m", symbol="o", symbolSize=4)
        vlay.addWidget(self._plot_widget2)

        return box

    def _build_control_buttons(self) -> QHBoxLayout:
        """Build Pause / Resume / Abort / Emergency-Acknowledge buttons.

        Returns:
            A QHBoxLayout containing the control buttons.
        """
        row = QHBoxLayout()

        pause_btn = QPushButton("Pause")
        pause_btn.setObjectName("pause_btn")
        pause_btn.setProperty("class", BTN_CLASS_SECONDARY)
        pause_btn.clicked.connect(self._orchestrator.pause_procedure)

        resume_btn = QPushButton("Resume")
        resume_btn.setObjectName("resume_btn")
        resume_btn.setProperty("class", BTN_CLASS_SECONDARY)
        resume_btn.clicked.connect(self._orchestrator.resume_procedure)

        abort_btn = QPushButton("Abort")
        abort_btn.setObjectName("abort_btn")
        abort_btn.setProperty("class", BTN_CLASS_DANGER)
        abort_btn.clicked.connect(self._on_abort)

        self._ack_btn = QPushButton("ACKNOWLEDGE EMERGENCY")
        self._ack_btn.setObjectName("ack_emergency_btn")
        self._ack_btn.setVisible(False)
        self._ack_btn.clicked.connect(self._orchestrator.acknowledge_emergency)

        row.addWidget(pause_btn)
        row.addWidget(resume_btn)
        row.addWidget(abort_btn)
        row.addStretch()
        row.addWidget(self._ack_btn)
        return row

    # ------------------------------------------------------------------
    # Signal connections
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._orchestrator.procedure_progress.connect(self._on_progress)
        self._orchestrator.measurement_ready.connect(self._on_measurement_ready)
        self._orchestrator.procedure_finished.connect(self._on_procedure_finished)
        self._orchestrator.state_changed.connect(self._on_state_changed)

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    def _on_procedure_selected(self, index: int) -> None:
        """Rebuild the parameter form and axis selectors when procedure changes.

        Args:
            index: Index of the selected procedure in the dropdown.
        """
        if index < 0 or index >= len(self._procedures):
            return
        cls = self._procedures[index]
        param_widget = self._build_param_form(cls)
        self._param_scroll.setWidget(param_widget)
        self._populate_axis_selectors(cls)

    def _collect_params(self) -> tuple[dict, dict, str] | None:
        """Read and validate all form inputs.

        Returns:
            ``(param_values, sample_info, data_dir)`` on success, or ``None``
            if a field cannot be parsed.
        """
        index = self._proc_selector.currentIndex()
        if index < 0 or index >= len(self._procedures):
            return None
        cls = self._procedures[index]

        param_values: dict[str, Any] = {}
        for param_name, spec in cls.parameters.items():
            raw = self._param_inputs[param_name].text().strip()
            param_type = spec.get("type", str)
            try:
                param_values[param_name] = param_type(raw)
            except (ValueError, TypeError):
                QMessageBox.warning(
                    self,
                    "Invalid Parameter",
                    f"Cannot parse '{raw}' as {param_type.__name__} for parameter '{param_name}'.",
                )
                return None

        sample_info = self._get_sample_info()
        data_dir = self._get_data_dir()
        return param_values, sample_info, data_dir

    def _build_procedure_instance(self) -> BaseProcedure | None:
        """Build a procedure instance from current form values.

        Returns:
            A ready ``BaseProcedure`` instance, or ``None`` on validation error.
        """
        result = self._collect_params()
        if result is None:
            return None
        param_values, sample_info, data_dir = result
        index = self._proc_selector.currentIndex()
        cls = self._procedures[index]

        return cls(
            station=self._station,
            sample_info=sample_info,
            data_directory=data_dir,
            **param_values,
        )

    def _on_add_to_queue(self) -> None:
        """Freeze current form values and add a procedure entry to the queue."""
        result = self._collect_params()
        if result is None:
            return
        param_values, sample_info, data_dir = result
        index = self._proc_selector.currentIndex()
        cls = self._procedures[index]

        self._queue.append((cls, param_values, sample_info, data_dir))

        sweep_keys = list(cls.sweep_parameters.keys()) or list(cls.parameters.keys())
        summary_parts = [f"{k}={param_values[k]}" for k in sweep_keys[:3]]
        summary = f"{cls.name} ({', '.join(summary_parts)})"
        item = QListWidgetItem(f"{len(self._queue)}. {summary}")
        self._queue_list.addItem(item)

        proc = cls(
            station=self._station,
            sample_info=sample_info,
            data_directory=data_dir,
            **param_values,
        )
        self._orchestrator.queue_procedure(proc)

    def _on_run_now(self) -> None:
        """Build and immediately run the current procedure via the Orchestrator."""
        proc = self._build_procedure_instance()
        if proc is None:
            return
        self._active_procedure = proc
        self._reset_plot(proc)
        self._orchestrator.run_procedure(proc)

    def _on_abort(self) -> None:
        """Ask for confirmation, then abort the running procedure."""
        answer = QMessageBox.question(
            self,
            "Abort Procedure",
            "Abort the running procedure? The data file will be saved as-is.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._orchestrator.abort_procedure()

    def _on_progress(self, fraction: float) -> None:
        """Update the progress bar.

        Args:
            fraction: 0.0–1.0 progress from the Orchestrator.
        """
        self._progress_bar.setValue(int(fraction * 100))

    def _on_measurement_ready(self, datapoint: dict) -> None:
        """Store the new datapoint and redraw both plots.

        Args:
            datapoint: Latest enriched data dict emitted by the Orchestrator.
        """
        self._datapoints.append(datapoint)
        self._redraw_plot()
        self._redraw_plot2()

    def _on_procedure_finished(self) -> None:
        """Reset progress bar and clear the active procedure reference."""
        self._progress_bar.setValue(100)
        self._active_procedure = None

    def _on_state_changed(self, state_name: str) -> None:
        """Show/hide the emergency-acknowledge button based on state.

        Args:
            state_name: New Orchestrator state string.
        """
        is_emergency = state_name == OrchestratorState.EMERGENCY.value
        self._ack_btn.setVisible(is_emergency)

    # ------------------------------------------------------------------
    # Plot helpers
    # ------------------------------------------------------------------

    def _populate_axis_selectors(self, cls: type[BaseProcedure]) -> None:
        """Populate all four axis selectors from cls metadata + current station state.

        Safe to call before a run starts (reads from monitor cache, no poll).
        Preserves existing selection when keys overlap (e.g. after a procedure
        type change), so the user's choices survive switching procedures.

        Args:
            cls: The BaseProcedure subclass currently selected.
        """
        system_keys = list(self._station.last_state_flat().keys())
        keys = ["unix_time"] + cls.sweep_data_keys + system_keys + cls.measurement_data_keys
        if not keys:
            return

        default_x = cls.default_x_key if cls.default_x_key in keys else keys[0]

        for sel in (self._x1_axis_selector, self._x2_axis_selector,
                    self._y_axis_selector, self._y2_axis_selector):
            prev = sel.currentText()
            sel.blockSignals(True)
            sel.clear()
            sel.addItems(keys)
            sel.setCurrentText(prev if prev in keys else keys[0])
            sel.blockSignals(False)

        # Apply sensible defaults only when the current selection is blank/unknown
        if self._x1_axis_selector.currentText() not in keys:
            self._x1_axis_selector.setCurrentText(default_x)
        if self._x2_axis_selector.currentText() not in keys:
            self._x2_axis_selector.setCurrentText(default_x)
        if self._y_axis_selector.currentText() not in keys and "voltage_V" in keys:
            self._y_axis_selector.setCurrentText("voltage_V")
        if self._y2_axis_selector.currentText() not in keys and "current_A" in keys:
            self._y2_axis_selector.setCurrentText("current_A")

    def _reset_plot(self, proc: BaseProcedure) -> None:
        """Clear datapoints and refresh axis selectors for a new run.

        Args:
            proc: The procedure about to run.
        """
        self._datapoints.clear()
        self._plot_curve1.setData([], [])
        self._plot_curve2.setData([], [])
        self._progress_bar.setValue(0)
        self._populate_axis_selectors(type(proc))

    @staticmethod
    def _extract_scalar(raw: Any) -> float:
        """Convert a datapoint value to a plottable float."""
        if raw is None:
            return float("nan")
        return float(np.mean(raw)) if hasattr(raw, "__len__") else float(raw)

    def _redraw_plot(self) -> None:
        """Redraw Plot 1 from the stored datapoint history."""
        x_key = self._x1_axis_selector.currentText()
        y_key = self._y_axis_selector.currentText()
        xs = [self._extract_scalar(dp.get(x_key)) for dp in self._datapoints]
        ys = [self._extract_scalar(dp.get(y_key)) for dp in self._datapoints]
        self._plot_curve1.setData(xs, ys)
        self._plot_widget.setLabel("bottom", x_key.replace("_", " "))
        self._plot_widget.setLabel("left", y_key.replace("_", " "))

    def _redraw_plot2(self) -> None:
        """Redraw Plot 2 from the stored datapoint history."""
        x_key = self._x2_axis_selector.currentText()
        y_key = self._y2_axis_selector.currentText()
        xs = [self._extract_scalar(dp.get(x_key)) for dp in self._datapoints]
        ys = [self._extract_scalar(dp.get(y_key)) for dp in self._datapoints]
        self._plot_curve2.setData(xs, ys)
        self._plot_widget2.setLabel("bottom", x_key.replace("_", " "))
        self._plot_widget2.setLabel("left", y_key.replace("_", " "))

    # ------------------------------------------------------------------
    # Queue management helpers
    # ------------------------------------------------------------------

    def _queue_move_up(self) -> None:
        """Move the selected queue item up by one position."""
        row = self._queue_list.currentRow()
        if row <= 0:
            return
        self._queue[row - 1], self._queue[row] = self._queue[row], self._queue[row - 1]
        self._refresh_queue_list()
        self._queue_list.setCurrentRow(row - 1)

    def _queue_move_down(self) -> None:
        """Move the selected queue item down by one position."""
        row = self._queue_list.currentRow()
        if row < 0 or row >= len(self._queue) - 1:
            return
        self._queue[row], self._queue[row + 1] = self._queue[row + 1], self._queue[row]
        self._refresh_queue_list()
        self._queue_list.setCurrentRow(row + 1)

    def _queue_remove(self) -> None:
        """Remove the selected item from the queue."""
        row = self._queue_list.currentRow()
        if row < 0 or row >= len(self._queue):
            return
        self._queue.pop(row)
        self._orchestrator._procedure_queue.pop(row)
        self._refresh_queue_list()

    def _refresh_queue_list(self) -> None:
        """Rebuild the QListWidget from self._queue."""
        self._queue_list.clear()
        for idx, (cls, params, _sample, _dir) in enumerate(self._queue):
            sweep_keys = list(cls.sweep_parameters.keys()) or list(cls.parameters.keys())
            summary_parts = [f"{k}={params[k]}" for k in sweep_keys[:3]]
            summary = f"{cls.name} ({', '.join(summary_parts)})"
            self._queue_list.addItem(f"{idx + 1}. {summary}")
