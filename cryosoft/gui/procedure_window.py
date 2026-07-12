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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
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
from cryosoft.gui import app_settings  # import the module (not the function) so tests can monkeypatch the factory
from cryosoft.gui.live_plot_panel import LivePlotPanel
from cryosoft.gui.notification_banner import NotificationBanner
from cryosoft.gui.session import (
    STATUS_PENDING,
    STATUS_RUNNING,
    QueueItemState,
    SessionState,
)
from cryosoft.gui.theme import (
    BANNER_SEVERITY_ERROR,
    BANNER_SEVERITY_WARNING,
    BTN_CLASS_DANGER,
    BTN_CLASS_PRIMARY,
    BTN_CLASS_SECONDARY,
    PLOT_SERIES,
    TEXT_ON_ACCENT,
    TEXT_PRIMARY,
)

logger = logging.getLogger(__name__)

_GEOMETRY_KEY = "ProcedureWindow/geometry"  # QSettings key for saved window geometry


@dataclass
class _QueueEntry:
    """One row of the run queue, held in memory by the ProcedureWindow.

    A ``@dataclass`` (auto-generated ``__init__`` from the fields below) that
    pairs the procedure spec with its captured parameters and a lifecycle
    ``status``. The built ``proc`` instance is kept for pending entries so the
    Orchestrator queue can be rebuilt from the GUI's entries without
    re-reading the form; it is ``None`` for entries restored as already-done.

    Attributes:
        cls: The BaseProcedure subclass for this run.
        params: Validated parameter values captured when the entry was queued.
        sample_info: Sample metadata captured at queue time.
        data_dir: Data directory captured at queue time.
        status: One of the ``session.STATUS_*`` values.
        proc: The built procedure instance for a pending entry, else ``None``.
    """

    cls: type[BaseProcedure]
    params: dict[str, Any]
    sample_info: dict[str, str]
    data_dir: str
    status: str = STATUS_PENDING
    proc: BaseProcedure | None = field(default=None, repr=False)


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
        initial_session: SessionState | None = None,
    ) -> None:
        super().__init__(parent)
        self._station = station
        self._orchestrator = orchestrator
        self._get_sample_info = get_sample_info
        self._get_data_dir = get_data_dir

        self._procedures: list[type[BaseProcedure]] = _discover_procedures()
        # Run queue as _QueueEntry objects (spec + params + lifecycle status).
        self._queue: list[_QueueEntry] = []
        # Per-procedure last-typed parameter text, keyed by procedure name, so
        # switching procedures and back restores each one's field values.
        self._procedure_params: dict[str, dict[str, str]] = {}
        # Name of the procedure whose form is currently shown (for param cache
        # bookkeeping across selection changes).
        self._current_procedure_name: str = ""
        # True while a queued run is executing, so procedure_finished advances
        # the queue's per-item status.
        self._queue_running = False
        # Widgets for the parameter form (rebuilt on procedure selection)
        self._param_inputs: dict[str, QLineEdit] = {}
        # Active procedure reference (set on run)
        self._active_procedure: BaseProcedure | None = None
        # Live plot: full datapoint history (each entry is the enriched dict from measurement_ready)
        self._datapoints: list[dict] = []

        self.setWindowTitle("CryoSoft — Procedure")
        self._restore_geometry()

        self._build_ui()
        self._connect_signals()

        if self._procedures:
            self._on_procedure_selected(0)

        if initial_session is not None:
            self._restore_session(initial_session)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)

        # ── Notification banner (hidden until a warning/error arrives) ─
        self._banner = NotificationBanner()
        root.addWidget(self._banner)

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
        # setChildrenCollapsible(False) + per-pane minimums stop a drag from
        # crushing either the params or the queue pane to zero width.
        splitter.setChildrenCollapsible(False)

        left_column = self._build_left_column()
        right_column = self._build_right_column()
        left_column.setMinimumWidth(300)
        right_column.setMinimumWidth(250)
        splitter.addWidget(left_column)
        splitter.addWidget(right_column)
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
        add_btn.setIcon(qta.icon("fa5s.plus", color=TEXT_PRIMARY))
        add_btn.setToolTip("Add the current procedure and parameters to the run queue")
        add_btn.clicked.connect(self._on_add_to_queue)
        run_now_btn = QPushButton("Run Now")
        run_now_btn.setObjectName("run_now_btn")
        run_now_btn.setProperty("class", BTN_CLASS_PRIMARY)
        run_now_btn.setIcon(qta.icon("fa5s.play", color=TEXT_ON_ACCENT))
        run_now_btn.setToolTip("Run the current procedure immediately")
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

        The label IS the canonical parameter name — the same key used in the
        procedure code and stored under ``/metadata/procedure_params`` in the
        HDF5 output — plus its unit: ``f"{param_name} ({unit}):"`` when the
        spec declares a non-empty ``unit``, else ``f"{param_name}:"``. The
        prose ``description`` (plus default and min/max range, when present)
        moves into a tooltip set on both the input field and its form label,
        so the human-readable explanation is one hover away without cluttering
        the form or diverging from the name actually written to disk.

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
                label_text = f"{param_name} ({unit}):" if unit else f"{param_name}:"
                field = QLineEdit(str(spec.get("default", "")))
                field.setObjectName(f"param_{param_name}_input")
                tooltip = self._build_param_tooltip(spec)
                field.setToolTip(tooltip)
                self._param_inputs[param_name] = field
                form.addRow(label_text, field)
                row_label = form.labelForField(field)
                if row_label is not None:
                    row_label.setToolTip(tooltip)
            hbox.addWidget(box)

        return container

    @staticmethod
    def _build_param_tooltip(spec: dict) -> str:
        """Build the hover-tooltip text for one parameter spec.

        Assembles, in order and skipping any part whose source data is absent:
        the ``description`` sentence (a period is appended if missing), then
        ``Default: {default} {unit}.``, then a ``Range: ...`` line built from
        ``min``/``max`` (one-sided phrasing — ``Min: ...`` / ``Max: ...`` — when
        only one bound is declared).

        Args:
            spec: A single parameter's spec dict (``type``, ``default``, and
                optionally ``unit``, ``min``, ``max``, ``description``).

        Returns:
            A single plain-text string, parts joined with a space.
        """
        unit = spec.get("unit", "")
        parts: list[str] = []

        description = spec.get("description")
        if description:
            parts.append(description if description.endswith(".") else f"{description}.")

        if "default" in spec:
            unit_suffix = f" {unit}" if unit else ""
            parts.append(f"Default: {spec['default']}{unit_suffix}.")

        has_min = "min" in spec
        has_max = "max" in spec
        if has_min and has_max:
            unit_suffix = f" {unit}" if unit else ""
            parts.append(f"Range: {spec['min']} to {spec['max']}{unit_suffix}.")
        elif has_min:
            unit_suffix = f" {unit}" if unit else ""
            parts.append(f"Min: {spec['min']}{unit_suffix}.")
        elif has_max:
            unit_suffix = f" {unit}" if unit else ""
            parts.append(f"Max: {spec['max']}{unit_suffix}.")

        return " ".join(parts)

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
        up_btn = QPushButton()
        up_btn.setObjectName("queue_up_btn")
        up_btn.setIcon(qta.icon("fa5s.arrow-up", color=TEXT_PRIMARY))
        up_btn.setToolTip("Move the selected queue item up")
        up_btn.setMaximumWidth(40)
        up_btn.clicked.connect(self._queue_move_up)
        down_btn = QPushButton()
        down_btn.setObjectName("queue_down_btn")
        down_btn.setIcon(qta.icon("fa5s.arrow-down", color=TEXT_PRIMARY))
        down_btn.setToolTip("Move the selected queue item down")
        down_btn.setMaximumWidth(40)
        down_btn.clicked.connect(self._queue_move_down)
        remove_btn = QPushButton("Remove")
        remove_btn.setObjectName("queue_remove_btn")
        remove_btn.setIcon(qta.icon("fa5s.trash", color=TEXT_PRIMARY))
        remove_btn.setToolTip("Remove the selected item from the queue")
        remove_btn.clicked.connect(self._queue_remove)
        run_queue_btn = QPushButton("Run Queue")
        run_queue_btn.setObjectName("run_queue_btn")
        run_queue_btn.setProperty("class", BTN_CLASS_PRIMARY)
        run_queue_btn.setIcon(qta.icon("fa5s.forward", color=TEXT_ON_ACCENT))
        run_queue_btn.setToolTip("Run all queued procedures in order")
        run_queue_btn.clicked.connect(self._orchestrator.run_queue)
        btn_row.addWidget(up_btn)
        btn_row.addWidget(down_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        btn_row.addWidget(run_queue_btn)
        vlay.addLayout(btn_row)

        return box

    def _build_plot_section_dual(self) -> QSplitter:
        """Build two side-by-side LivePlotPanels in a horizontal splitter.

        Both panels are ``LivePlotPanel`` instances (widget extraction of the
        formerly duplicated Plot 1 / Plot 2 code). The legacy objectNames are
        passed through unchanged so ``findChild`` keeps resolving them.

        Returns:
            QSplitter containing Plot 1 (left) and Plot 2 (right).
        """
        splitter = QSplitter(Qt.Orientation.Horizontal)
        # Keep both plots usable; non-collapsible with a sane minimum size so a
        # drag cannot squeeze either plot into an unreadable sliver.
        splitter.setChildrenCollapsible(False)
        self._plot1 = LivePlotPanel(
            "Plot 1", PLOT_SERIES[0],
            x_selector_name="x1_axis_selector",
            y_selector_name="y_axis_selector",
            plot_object_name="live_plot",
        )
        self._plot2 = LivePlotPanel(
            "Plot 2", PLOT_SERIES[1],
            x_selector_name="x2_axis_selector",
            y_selector_name="y2_axis_selector",
            plot_object_name="live_plot_2",
        )
        for panel in (self._plot1, self._plot2):
            panel.setMinimumWidth(250)
            panel.setMinimumHeight(150)
            splitter.addWidget(panel)
        return splitter

    def _build_control_buttons(self) -> QHBoxLayout:
        """Build Pause / Resume / Abort / Emergency-Acknowledge buttons.

        Returns:
            A QHBoxLayout containing the control buttons.
        """
        row = QHBoxLayout()

        pause_btn = QPushButton("Pause")
        pause_btn.setObjectName("pause_btn")
        pause_btn.setProperty("class", BTN_CLASS_SECONDARY)
        pause_btn.setIcon(qta.icon("fa5s.pause", color=TEXT_PRIMARY))
        pause_btn.setToolTip("Pause the running procedure at the next safe point")
        pause_btn.clicked.connect(self._orchestrator.pause_procedure)

        resume_btn = QPushButton("Resume")
        resume_btn.setObjectName("resume_btn")
        resume_btn.setProperty("class", BTN_CLASS_SECONDARY)
        resume_btn.setIcon(qta.icon("fa5s.play", color=TEXT_PRIMARY))
        resume_btn.setToolTip("Resume the paused procedure")
        resume_btn.clicked.connect(self._orchestrator.resume_procedure)

        abort_btn = QPushButton("Abort")
        abort_btn.setObjectName("abort_btn")
        abort_btn.setProperty("class", BTN_CLASS_DANGER)
        abort_btn.setIcon(qta.icon("fa5s.stop", color=TEXT_ON_ACCENT))
        abort_btn.setToolTip("Stop the running procedure and save data as-is")
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
        self._orchestrator.error_occurred.connect(
            lambda msg: self._banner.show_message(msg, BANNER_SEVERITY_ERROR)
        )
        self._orchestrator.action_blocked.connect(
            lambda msg: self._banner.show_message(msg, BANNER_SEVERITY_WARNING)
        )

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
        # Preserve the outgoing procedure's typed values before rebuilding.
        self._cache_current_params()
        cls = self._procedures[index]
        param_widget = self._build_param_form(cls)
        self._param_scroll.setWidget(param_widget)
        self._populate_axis_selectors(cls)
        self._current_procedure_name = getattr(cls, "name", cls.__name__)
        # Re-apply any previously-typed values for the incoming procedure.
        self._apply_cached_params(self._current_procedure_name)

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

    def _build_procedure_instance(
        self, collected: tuple[dict, dict, str] | None = None
    ) -> BaseProcedure | None:
        """Build a procedure instance from current (or supplied) form values.

        This is the single construction path shared by the run-now and the
        queue flows, so a procedure is only ever instantiated in one place.

        Args:
            collected: An already-validated ``(param_values, sample_info,
                data_dir)`` tuple from ``_collect_params``. When ``None`` the
                form is read afresh (the run-now path).

        Returns:
            A ready ``BaseProcedure`` instance, or ``None`` on validation error.
        """
        if collected is None:
            collected = self._collect_params()
        if collected is None:
            return None
        param_values, sample_info, data_dir = collected
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

        # Construct through the shared _build_procedure_instance path (reusing the
        # params we already collected) instead of re-implementing cls(...) here.
        proc = self._build_procedure_instance(result)
        entry = _QueueEntry(
            cls=cls,
            params=param_values,
            sample_info=sample_info,
            data_dir=data_dir,
            status=STATUS_PENDING,
            proc=proc,
        )
        self._queue.append(entry)
        self._refresh_queue_list()

        if proc is not None:
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
        self._plot1.redraw(self._datapoints)
        self._plot2.redraw(self._datapoints)

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
        """Populate both plot panels' axis selectors from cls metadata + station state.

        Safe to call before a run starts (reads from monitor cache, no poll).
        Each panel preserves its existing selection when keys overlap (e.g. after
        a procedure type change), so the user's choices survive switching
        procedures. Kept as a thin loop delegating to ``LivePlotPanel`` so
        existing callers do not change.

        Args:
            cls: The BaseProcedure subclass currently selected.
        """
        system_keys = list(self._station.last_state_flat().keys())
        keys = ["unix_time"] + cls.sweep_data_keys + system_keys + cls.measurement_data_keys
        if not keys:
            return

        default_x = cls.default_x_key if cls.default_x_key in keys else keys[0]

        # Per-panel Y defaults: Plot 1 → voltage, Plot 2 → current.
        self._plot1.set_available_keys(keys, default_x, "voltage_V")
        self._plot2.set_available_keys(keys, default_x, "current_A")

    def _reset_plot(self, proc: BaseProcedure) -> None:
        """Clear datapoints and refresh axis selectors for a new run.

        Args:
            proc: The procedure about to run.
        """
        self._datapoints.clear()
        self._plot1.clear()
        self._plot2.clear()
        self._progress_bar.setValue(0)
        self._populate_axis_selectors(type(proc))

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
        # Pending entries mirror the Orchestrator queue 1:1 and in order, so the
        # same row indexes the instance to drop.
        if row < len(self._orchestrator._procedure_queue):
            self._orchestrator._procedure_queue.pop(row)
        self._refresh_queue_list()

    def _entry_summary(self, entry: _QueueEntry) -> str:
        """Return the one-line queue summary for a queue entry.

        Args:
            entry: The queue entry to summarise.

        Returns:
            ``"<ProcedureName> (k1=v1, k2=v2, ...)"`` using up to three sweep
            (or, if none, plain) parameter keys.
        """
        cls = entry.cls
        sweep_keys = list(cls.sweep_parameters.keys()) or list(cls.parameters.keys())
        summary_parts = [
            f"{k}={entry.params[k]}" for k in sweep_keys[:3] if k in entry.params
        ]
        return f"{cls.name} ({', '.join(summary_parts)})"

    def _refresh_queue_list(self) -> None:
        """Rebuild the QListWidget from self._queue, annotating non-pending status."""
        self._queue_list.clear()
        for idx, entry in enumerate(self._queue):
            label = f"{idx + 1}. {self._entry_summary(entry)}"
            if entry.status != STATUS_PENDING:
                label = f"{label}  — {entry.status}"
            self._queue_list.addItem(QListWidgetItem(label))

    # ------------------------------------------------------------------
    # Session persistence (procedure selection, params, queue)
    # ------------------------------------------------------------------

    def _cache_current_params(self) -> None:
        """Store the current form's raw text under the current procedure name.

        Raw text (not validated values) is cached so persistence never triggers
        the "Invalid Parameter" dialog and half-typed values survive a switch.
        """
        if self._current_procedure_name and self._param_inputs:
            self._procedure_params[self._current_procedure_name] = {
                name: field.text() for name, field in self._param_inputs.items()
            }

    def _apply_cached_params(self, procedure_name: str) -> None:
        """Fill the current form with cached values for ``procedure_name``, if any.

        Args:
            procedure_name: The procedure whose cached values to apply. Keys not
                present in the current form are ignored.
        """
        cached = self._procedure_params.get(procedure_name)
        if not cached:
            return
        for name, value in cached.items():
            field = self._param_inputs.get(name)
            if field is not None:
                field.setText(str(value))

    def _procedure_by_name(self, name: str) -> type[BaseProcedure] | None:
        """Return the discovered procedure class whose name matches, or None."""
        for cls in self._procedures:
            if getattr(cls, "name", cls.__name__) == name:
                return cls
        return None

    def _select_procedure_by_name(self, name: str) -> None:
        """Select ``name`` in the dropdown and rebuild its form.

        Rebuilds the form even when the index does not change (so a restored
        selection that equals the default still gets its cached params applied).

        Args:
            name: The procedure name to select. A no-op if not found.
        """
        for i, cls in enumerate(self._procedures):
            if getattr(cls, "name", cls.__name__) == name:
                if self._proc_selector.currentIndex() == i:
                    self._on_procedure_selected(i)  # signal won't fire; rebuild directly
                else:
                    self._proc_selector.setCurrentIndex(i)  # fires _on_procedure_selected
                return

    def _build_entry_procedure(self, entry: _QueueEntry) -> BaseProcedure | None:
        """Build a procedure instance from a queue entry's stored values.

        Unlike ``_build_procedure_instance`` (which reads the live form), this
        rebuilds from an entry's frozen params/sample/data — used when re-arming
        a restored queue.

        Args:
            entry: The queue entry to instantiate.

        Returns:
            The built procedure, or ``None`` if construction fails (logged).
        """
        try:
            return entry.cls(
                station=self._station,
                sample_info=entry.sample_info,
                data_directory=entry.data_dir,
                **entry.params,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "session: could not rebuild queued %s: %s", entry.cls.name, exc
            )
            return None

    def _restore_queue(self, items: list[QueueItemState]) -> None:
        """Rebuild the queue from persisted items, re-arming pending ones.

        Items whose procedure name is unknown (e.g. a procedure removed since
        the session was saved) are skipped with a warning. Pending items (and
        any that were mid-run when the app closed) are rebuilt and re-queued in
        the Orchestrator; already-completed items are shown for reference but
        not re-run.

        Args:
            items: The persisted queue, oldest first.
        """
        self._queue.clear()
        self._orchestrator._procedure_queue.clear()
        for item in items:
            cls = self._procedure_by_name(item.procedure)
            if cls is None:
                logger.warning(
                    "session: unknown procedure %r in saved queue; skipping",
                    item.procedure,
                )
                continue
            # A "running" item never finished (app closed mid-run) — treat as pending.
            status = (
                STATUS_PENDING
                if item.status in (STATUS_PENDING, STATUS_RUNNING)
                else item.status
            )
            entry = _QueueEntry(
                cls=cls,
                params=dict(item.params),
                sample_info=dict(item.sample_info),
                data_dir=item.data_dir,
                status=status,
            )
            if status == STATUS_PENDING:
                entry.proc = self._build_entry_procedure(entry)
                if entry.proc is not None:
                    self._orchestrator.queue_procedure(entry.proc)
            self._queue.append(entry)
        self._refresh_queue_list()

    def _restore_session(self, session_state: SessionState) -> None:
        """Apply a loaded session to the procedure form and queue.

        Args:
            session_state: The session loaded at startup.
        """
        self._procedure_params = {
            name: dict(values)
            for name, values in session_state.procedure_params.items()
        }
        # Suppress caching the current default values over the restored ones
        # while we switch the selector.
        self._current_procedure_name = ""
        if session_state.selected_procedure:
            self._select_procedure_by_name(session_state.selected_procedure)
        else:
            # No saved selection: re-render the current procedure so any cached
            # params for it are applied.
            self._on_procedure_selected(self._proc_selector.currentIndex())
        self._restore_queue(session_state.queue)

    def export_session_state(self, state: SessionState) -> None:
        """Write this window's selection, params, and queue into ``state``.

        Called by MonitorWindow when persisting the session. Mutates ``state``
        in place (the MonitorWindow owns the sample-info fields on the same
        object).

        Args:
            state: The SessionState to populate.
        """
        self._cache_current_params()
        state.selected_procedure = self._current_procedure_name
        state.procedure_params = {
            name: dict(values) for name, values in self._procedure_params.items()
        }
        state.queue = [
            QueueItemState(
                procedure=getattr(entry.cls, "name", entry.cls.__name__),
                params=entry.params,
                sample_info=entry.sample_info,
                data_dir=entry.data_dir,
                status=entry.status,
            )
            for entry in self._queue
        ]

    def reset_session(self) -> None:
        """Clear the queue and cached params, resetting the form to defaults."""
        self._queue.clear()
        self._orchestrator._procedure_queue.clear()
        self._queue_running = False
        self._procedure_params.clear()
        self._current_procedure_name = ""  # suppress caching stale values
        self._refresh_queue_list()
        if self._procedures:
            self._on_procedure_selected(self._proc_selector.currentIndex())

    # ------------------------------------------------------------------
    # Window geometry + lifecycle
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        """Restore the saved window geometry, or size to a fraction of the screen.

        Geometry is persisted with ``QSettings``, which on Windows is backed by
        the registry (``HKCU\\Software\\CryoSoft\\CryoSoft``). If nothing is
        stored yet, the window is sized to ~70% of the available screen area.
        """
        settings = app_settings.get_settings()
        saved = settings.value(_GEOMETRY_KEY)
        if saved is not None and self.restoreGeometry(saved):
            return
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            self.resize(int(available.width() * 0.7), int(available.height() * 0.7))

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Persist the window geometry before the window closes.

        Args:
            event: The Qt close event.
        """
        app_settings.get_settings().setValue(_GEOMETRY_KEY, self.saveGeometry())
        super().closeEvent(event)
