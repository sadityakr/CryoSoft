# ---
# description: |
#   ProcedureWindow: PyQt6 window for building, queuing, and running measurement
#   procedures. Auto-generates parameter forms from a procedure's ParamGroups
#   (BaseProcedure.get_param_groups()), mapping each ParamSpec to a Qt input via
#   cryosoft.gui.param_form (the one place that names widget classes).
#   A procedure that declares sweep_axis gets a SweepAxisWidget (linear /
#   segments / CSV, with hysteresis) in the Sweep column instead of flat text
#   fields — this file is the only GUI code sweep-shape support needs; new
#   procedures never touch it. Fixed 2x2 quadrant grid (top-left params,
#   top-right queue over a concise status log, bottom-left/right Plot 1/Plot 2),
#   the same nested-QSplitter pattern as MonitorWindow. Sample info is read from
#   MonitorWindow via callables.
# entry_point: Not run directly. Opened via MonitorWindow Procedures menu.
# dependencies:
#   - PyQt6 >= 6.5
#   - pyqtgraph >= 0.13
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.gui.param_form (ParamSpec -> Qt widget mapping)
#   - cryosoft.gui.sweep_axis_widget (SweepAxisWidget)
#   - cryosoft.procedures.* (auto-discovered subclasses)
# input: |
#   Station instance, Orchestrator instance, and two callables (get_sample_info,
#   get_data_dir) provided by MonitorWindow.
# process: |
#   _discover_procedures() imports all modules in cryosoft/procedures/ and collects
#   BaseProcedure subclasses. The selected procedure's ParamGroups (from
#   get_param_groups()) drive form generation via cryosoft.gui.param_form;
#   sweep_axis (if declared) drives a SweepAxisWidget instead of flat
#   fields for its hidden parameters. A filename-prefix field above the form is
#   captured per queue entry, so each queued procedure can save under a different
#   filename. Queued procedures are stored as (cls, params, sample_info, data_dir,
#   file_prefix) tuples. Execution goes through orchestrator.run_procedure().
# output: |
#   A QMainWindow. Two live plots update via orchestrator.measurement_ready.
# ---

"""ProcedureWindow — procedure builder, queue, and live-data monitor."""

from __future__ import annotations

import importlib
import logging
import pkgutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
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
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.plan import ParamGroup, ParamSpec
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import Station
from cryosoft.gui import app_settings  # import the module (not the function) so tests can monkeypatch the factory
from cryosoft.gui import param_form
from cryosoft.gui.live_plot_panel import LivePlotPanel
from cryosoft.gui.notification_banner import NotificationBanner
from cryosoft.gui.session import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    QueueItemState,
    SessionState,
)
from cryosoft.gui.sweep_axis_widget import SweepAxisWidget
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
# Max width (px) for the Sweep parameter column. Its inputs are narrow, so this
# stops it expanding to an equal third and crowding out the Measurement column.
_SWEEP_COLUMN_MAX_WIDTH = 260
# Max lines kept in the concise Status log before old lines are trimmed
# (matches the Monitor detailed log's cap; bounds a long run's memory use).
_STATUS_MAX_LINES = 500


@dataclass
class _QueueEntry:
    """One row of the run queue, held in memory by the ProcedureWindow.

    Pairs the procedure spec with its captured parameters, filename prefix, and
    a lifecycle ``status``. The built ``proc`` instance is kept for pending
    entries so the Orchestrator queue can be rebuilt from the GUI's entries
    without re-reading the form; it is ``None`` for entries restored as done.
    """

    cls: type[BaseProcedure]
    params: dict[str, Any]
    sample_info: dict[str, str]
    data_dir: str
    file_prefix: str = ""
    status: str = STATUS_PENDING
    proc: BaseProcedure | None = field(default=None, repr=False)


def _all_subclasses(cls: type) -> list[type]:
    """Return every transitive subclass of *cls* (depth-first, deduplicated).

    ``type.__subclasses__()`` lists only *direct* subclasses, so it would miss a
    concrete procedure sitting under an intermediate base such as
    ``SweepMeasureProcedure``. This walks the whole tree.
    """
    result: list[type] = []
    seen: set[type] = set()
    for sub in cls.__subclasses__():
        if sub not in seen:
            seen.add(sub)
            result.append(sub)
        result.extend(_all_subclasses(sub))
    return result


def _discover_procedures() -> list[type[BaseProcedure]]:
    """Import all modules in cryosoft.procedures and return concrete procedures.

    Returns every named ``BaseProcedure`` subclass at any depth (so a procedure
    under an intermediate base like ``SweepMeasureProcedure`` is found), skipping
    unnamed intermediate bases.

    Returns:
        List of concrete BaseProcedure subclasses (not the base or intermediate
        bases, which carry no ``name``).
    """
    import cryosoft.procedures as _pkg

    pkg_path = Path(_pkg.__file__).parent
    for _, module_name, _ in pkgutil.iter_modules([str(pkg_path)]):
        try:
            importlib.import_module(f"cryosoft.procedures.{module_name}")
        except Exception:
            logger.exception("ProcedureWindow: failed to import cryosoft.procedures.%s", module_name)

    subclasses: list[type[BaseProcedure]] = []
    seen: set[type] = set()
    for cls in _all_subclasses(BaseProcedure):
        if getattr(cls, "name", "") and cls not in seen:
            seen.add(cls)
            subclasses.append(cls)
    return subclasses


class ProcedureWindow(QMainWindow):
    """Procedure builder, queue manager, and live-data window.

    A fixed 2x2 quadrant grid, the same nested-QSplitter pattern as
    MonitorWindow: top-left is the procedure selector + full parameter form
    (Sweep/System/Measurement, filling the quadrant rather than capped at a
    fixed height with empty space below), top-right is the Queue over a
    concise Status log (a draggable vertical split), and the bottom row is
    Plot 1 (left) / Plot 2 (right). Every splitter boundary is
    draggable; nothing in the grid can be closed or detached. A full-width
    progress bar and control buttons sit below the grid.

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
        # Run queue as _QueueEntry objects (spec + params + prefix + status).
        self._queue: list[_QueueEntry] = []
        # Per-procedure last-typed flat parameter text, keyed by procedure name.
        self._procedure_params: dict[str, dict[str, str]] = {}
        self._current_procedure_name: str = ""
        # True while a queued run is executing, so procedure_finished advances
        # the queue's per-item status.
        self._queue_running = False
        # Widgets for the parameter form (rebuilt on procedure selection)
        # Parameter widgets, keyed by parameter name. Type varies with the
        # spec: QLineEdit (plain number/text), QComboBox (enumerated choices),
        # or QCheckBox (bool). Built and read via cryosoft.gui.param_form.
        self._param_inputs: dict[str, QWidget] = {}
        # The parameter groups currently rendered (from get_param_groups), the
        # non-sweep group boxes keyed by ParamGroup.key, and the container's
        # horizontal layout — together they let a structural change re-render
        # only the group(s) that changed and leave the rest (and the axis
        # widget) untouched.
        self._current_groups: list[ParamGroup] = []
        self._group_boxes: dict[str, QGroupBox] = {}
        self._param_hbox: QHBoxLayout | None = None
        # Sweep-axis editor for the selected procedure, if it declares one
        self._axis_widget: SweepAxisWidget | None = None
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

        # ── Fixed 2x2 quadrant grid ────────────────────────────────────
        top_left = self._build_params_quadrant()
        top_right = self._build_queue_quadrant()
        bottom_left, bottom_right = self._build_plot_quadrants()

        self._left_splitter = QSplitter(Qt.Orientation.Vertical)
        self._left_splitter.setObjectName("left_splitter")
        self._left_splitter.setChildrenCollapsible(False)
        self._left_splitter.addWidget(top_left)
        self._left_splitter.addWidget(bottom_left)
        self._left_splitter.setSizes([600, 400])

        self._right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._right_splitter.setObjectName("right_splitter")
        self._right_splitter.setChildrenCollapsible(False)
        self._right_splitter.addWidget(top_right)
        self._right_splitter.addWidget(bottom_right)
        self._right_splitter.setSizes([600, 400])

        self._main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._main_splitter.setObjectName("main_splitter")
        self._main_splitter.setChildrenCollapsible(False)
        self._main_splitter.addWidget(self._left_splitter)
        self._main_splitter.addWidget(self._right_splitter)
        self._main_splitter.setSizes([720, 480])

        root.addWidget(self._main_splitter, stretch=1)

        # ── Progress bar (full-width) ─────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("progress_bar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        root.addWidget(self._progress_bar)

        # ── Control buttons ───────────────────────────────────────────
        root.addLayout(self._build_control_buttons())

    def _build_params_quadrant(self) -> QWidget:
        """Build the top-left quadrant: procedure selector, Add/Run buttons, and the full parameter form.

        The parameter scroll area has no height cap and no trailing stretch
        — it fills whatever height the quadrant is given, instead of sitting
        at a fixed small height with empty space below it.

        Returns:
            QWidget containing the quadrant layout.
        """
        widget = QWidget()
        widget.setObjectName("params_quadrant")
        col = QVBoxLayout(widget)
        col.setSpacing(8)
        col.setContentsMargins(0, 0, 4, 0)

        # ── Procedure selector + Add/Run buttons (one row) ─────────────
        sel_row = QHBoxLayout()
        sel_row.addWidget(QLabel("Procedure:"))
        self._proc_selector = QComboBox()
        self._proc_selector.setObjectName("procedure_selector")
        for cls in self._procedures:
            self._proc_selector.addItem(getattr(cls, "name", cls.__name__))
        self._proc_selector.currentIndexChanged.connect(self._on_procedure_selected)
        sel_row.addWidget(self._proc_selector)
        sel_row.addStretch()

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
        sel_row.addWidget(add_btn)
        sel_row.addWidget(run_now_btn)
        col.addLayout(sel_row)

        # ── Filename prefix (carried into the queue per entry) ─────────
        prefix_row = QHBoxLayout()
        prefix_row.addWidget(QLabel("Filename prefix:"))
        self._file_prefix_input = QLineEdit()
        self._file_prefix_input.setObjectName("file_prefix_input")
        self._file_prefix_input.setPlaceholderText(
            "optional — defaults to procedure name"
        )
        self._file_prefix_input.setToolTip(
            "Prepended to the saved HDF5 filename, still followed by a unique "
            "timestamp. Captured when a procedure is added to the queue, so "
            "each queued entry can carry its own filename."
        )
        prefix_row.addWidget(self._file_prefix_input)
        col.addLayout(prefix_row)

        # ── Parameters (scroll area; rebuilt on selector change) ──────
        self._param_scroll = QScrollArea()
        self._param_scroll.setObjectName("param_scroll")
        self._param_scroll.setWidgetResizable(True)
        col.addWidget(self._param_scroll)

        return widget

    def _build_queue_quadrant(self) -> QWidget:
        """Build the top-right quadrant: the Queue list over the concise Status log.

        A vertical splitter stacks the Queue (list + management buttons) above
        the Status log so the queue/status height ratio is draggable — the
        quadrant has spare height that the status feed can use.

        Returns:
            QWidget containing the quadrant layout.
        """
        widget = QWidget()
        widget.setObjectName("queue_quadrant")
        col = QVBoxLayout(widget)
        col.setSpacing(0)
        col.setContentsMargins(4, 0, 0, 0)

        split = QSplitter(Qt.Orientation.Vertical)
        split.setObjectName("queue_status_splitter")
        split.setChildrenCollapsible(False)
        queue = self._build_queue_section()
        queue.setMinimumHeight(120)
        split.addWidget(queue)
        split.addWidget(self._build_status_section())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        col.addWidget(split)
        return widget

    def _build_status_section(self) -> QGroupBox:
        """Build the concise procedure Status log shown below the Queue.

        A read-only, timestamped, line-capped feed of procedure milestones
        driven by the Orchestrator's ``status_message`` signal: start, ramp to
        each setpoint, settle, measure each point, park, finish, plus
        pause/abort/error. This is the concise counterpart to the Monitor
        window's detailed per-tick log; it never carries raw instrument
        traffic, so a user watching a slow field ramp sees what is happening
        without reading GPIB detail.

        Returns:
            A QGroupBox containing the read-only status QTextEdit
            (objectName ``status_log``).
        """
        box = QGroupBox("Status")
        vlay = QVBoxLayout(box)
        vlay.setContentsMargins(6, 6, 6, 6)
        self._status_log = QTextEdit()
        self._status_log.setObjectName("status_log")
        self._status_log.setReadOnly(True)
        self._status_log.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._status_log.setMinimumHeight(80)
        vlay.addWidget(self._status_log)
        return box

    def _build_param_form(self, cls: type[BaseProcedure]) -> QWidget:
        """Auto-generate the three-column parameter form from the procedure's ParamGroups.

        Reads the ordered groups from ``cls.get_param_groups(station)`` (Sweep /
        System / Measurement by default, empty ones skipped) and renders each
        non-sweep group as its own ``QGroupBox`` via
        ``param_form.build_group_box``. The Sweep column is special: it always
        hosts the ``SweepAxisWidget`` (linear / segments / CSV, with hysteresis)
        when ``cls`` declares a ``sweep_axis``, and the flat ``sweep`` group's
        fields (if any) are added beneath it in the same capped-width column —
        exactly as before this became ParamSpec-driven.

        Widget construction, labels, and tooltips all come from
        ``cryosoft.gui.param_form`` (the one place that maps a ``ParamSpec`` to a
        Qt widget); this method only arranges the resulting boxes and records
        each input in ``self._param_inputs`` for ``_collect_params`` to read.

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
        self._group_boxes = {}
        self._param_hbox = hbox
        self._axis_widget = None

        groups = cls.get_param_groups(self._station, self._current_selections())
        self._current_groups = groups
        groups_by_key = {group.key: group for group in groups}

        sweep_box = QGroupBox("Sweep")
        # The sweep inputs are narrow (single values / a compact 2-col table),
        # so cap the column width instead of letting it expand to an equal
        # third — otherwise it sits half-empty and pushes the Measurement
        # column out of view. Maximum policy lets it hug even narrower content;
        # the hard cap overrides the hidden Segments table's wide size hint.
        # System/Measurement (default Expanding) absorb the freed width.
        sweep_box.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        sweep_box.setMaximumWidth(_SWEEP_COLUMN_MAX_WIDTH)
        sweep_col = QVBoxLayout(sweep_box)
        sweep_col.setSpacing(4)
        if cls.sweep_axis is not None:
            self._axis_widget = SweepAxisWidget(cls.sweep_axis)
            sweep_col.addWidget(self._axis_widget)
        sweep_group = groups_by_key.get("sweep")
        if sweep_group is not None:
            form, widgets = param_form.build_form_layout(sweep_group.params)
            self._register_group_widgets(sweep_group, widgets)
            sweep_col.addLayout(form)
        sweep_col.addStretch()
        # The sweep box is always at layout index 0; the non-sweep group boxes
        # follow it in get_param_groups order (index 1, 2, ...), which
        # _rerender_groups relies on to insert a rebuilt group at the right spot.
        hbox.addWidget(sweep_box)

        # Every remaining group (System, the Measurement-method selector, the
        # selected VI's params, and any a subclass adds) becomes its own box, in
        # the order get_param_groups returned them.
        for group in groups:
            if group.key == "sweep":
                continue
            box, widgets = param_form.build_group_box(group)
            self._register_group_widgets(group, widgets)
            hbox.addWidget(box)
            self._group_boxes[group.key] = box

        return container

    # ------------------------------------------------------------------
    # Structural re-render (a structural param changes which groups exist)
    # ------------------------------------------------------------------

    def _register_group_widgets(
        self, group: ParamGroup, widgets: dict[str, QWidget]
    ) -> None:
        """Record a group's input widgets and wire up any structural ones.

        Args:
            group: The ``ParamGroup`` the widgets belong to.
            widgets: ``{param_name: QWidget}`` from ``param_form``.
        """
        for name, widget in widgets.items():
            self._param_inputs[name] = widget
            if group.params[name].structural:
                self._connect_structural(widget, group.params[name])

    def _connect_structural(self, widget: QWidget, spec: ParamSpec) -> None:
        """Connect a structural param widget's change signal to a re-render."""
        if isinstance(widget, QComboBox):
            widget.currentIndexChanged.connect(self._on_structural_changed)
        elif isinstance(widget, QCheckBox):
            widget.stateChanged.connect(self._on_structural_changed)
        elif isinstance(widget, QLineEdit):
            widget.editingFinished.connect(self._on_structural_changed)

    def _current_selections(self) -> dict[str, Any]:
        """Return the current values of every rendered structural parameter.

        Fed back into ``get_param_groups`` so the form re-derives from the user's
        structural choices (e.g. which measurement VI is selected).
        """
        selections: dict[str, Any] = {}
        for group in self._current_groups:
            for name, spec in group.params.items():
                if not spec.structural:
                    continue
                widget = self._param_inputs.get(name)
                if widget is None:
                    continue
                try:
                    selections[name] = param_form.collect_value(widget, spec)
                except (ValueError, TypeError):
                    pass
        return selections

    def _on_structural_changed(self, *_args: Any) -> None:
        """Re-derive the form after a structural parameter changed.

        Caches the currently-typed values, re-derives the groups from the new
        selections, rebuilds ONLY the group boxes whose key changed (leaving the
        sweep/system boxes and the axis widget as the same widget instances),
        then re-applies cached values and refreshes the plot axis selectors.
        """
        index = self._proc_selector.currentIndex()
        if index < 0 or index >= len(self._procedures):
            return
        cls = self._procedures[index]
        self._cache_current_params()
        self._rerender_groups(cls, self._current_selections())
        self._apply_cached_params(self._current_procedure_name)
        self._populate_axis_selectors(cls)

    def _rerender_groups(
        self, cls: type[BaseProcedure], selections: dict[str, Any]
    ) -> None:
        """Diff current vs new groups by key; rebuild only the boxes that changed.

        Args:
            cls: The selected procedure class.
            selections: Current structural-parameter values.
        """
        old_by_key = {group.key: group for group in self._current_groups}
        new_groups = cls.get_param_groups(self._station, selections)
        new_by_key = {group.key: group for group in new_groups}
        new_nonsweep_keys = [g.key for g in new_groups if g.key != "sweep"]

        # Remove boxes whose key is gone; drop their widgets from the registry.
        for key in list(self._group_boxes):
            if key not in new_by_key:
                box = self._group_boxes.pop(key)
                old_group = old_by_key.get(key)
                if old_group is not None:
                    for name in old_group.params:
                        self._param_inputs.pop(name, None)
                if self._param_hbox is not None:
                    self._param_hbox.removeWidget(box)
                box.setParent(None)
                box.deleteLater()

        # Add boxes for newly-present keys at their position (sweep box is 0).
        for pos, key in enumerate(new_nonsweep_keys):
            if key in self._group_boxes:
                continue
            group = new_by_key[key]
            box, widgets = param_form.build_group_box(group)
            self._register_group_widgets(group, widgets)
            if self._param_hbox is not None:
                self._param_hbox.insertWidget(1 + pos, box)
            self._group_boxes[key] = box

        self._current_groups = new_groups

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
        run_queue_btn.clicked.connect(self._on_run_queue)
        btn_row.addWidget(up_btn)
        btn_row.addWidget(down_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch()
        btn_row.addWidget(run_queue_btn)
        vlay.addLayout(btn_row)

        return box

    def _build_plot_quadrants(self) -> tuple[QWidget, QWidget]:
        """Build the bottom-left/bottom-right quadrants: Plot 1 and Plot 2.

        Both panels are ``LivePlotPanel`` instances; legacy objectNames are
        passed through unchanged so ``findChild`` keeps resolving them. Each
        sits in its own quadrant (left_splitter's bottom / right_splitter's
        bottom) rather than a separate dedicated splitter — the same
        left/right boundary that divides params from queue above also
        divides Plot 1 from Plot 2 below.

        Returns:
            ``(plot1, plot2)`` widgets, not yet placed in a splitter.
        """
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
        return self._plot1, self._plot2

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
        self._orchestrator.status_message.connect(self._on_status_message)

    # ------------------------------------------------------------------
    # Slot handlers
    # ------------------------------------------------------------------

    def _on_status_message(self, text: str) -> None:
        """Append one timestamped milestone line to the concise Status log.

        Trims to ``_STATUS_MAX_LINES`` so a long run cannot grow the log
        without bound (same approach as the Monitor detailed log).

        Args:
            text: Milestone text from the Orchestrator's status_message signal.
        """
        self._status_log.append(f"{time.strftime('%H:%M:%S')}  {text}")
        doc = self._status_log.document()
        while doc.blockCount() > _STATUS_MAX_LINES:
            cursor = self._status_log.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.select(cursor.SelectionType.BlockUnderCursor)
            cursor.removeSelectedText()
            cursor.deleteChar()

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
        # Re-apply any previously-typed flat values for the incoming procedure.
        self._apply_cached_params(self._current_procedure_name)

    def _collect_params(self) -> tuple[dict, dict, str, str] | None:
        """Read and validate all form inputs.

        Returns:
            ``(param_values, sample_info, data_dir, file_prefix)`` on success,
            or ``None`` if a field cannot be parsed.
        """
        index = self._proc_selector.currentIndex()
        if index < 0 or index >= len(self._procedures):
            return None

        axis_keys = self._axis_widget.param_keys() if self._axis_widget is not None else set()

        param_values: dict[str, Any] = {}
        # Gather across ALL currently-rendered groups (Sweep flat params, System,
        # the measurement-method selector, and the selected VI's params), rather
        # than cls.parameters — a generic procedure's measurement params are
        # station-dependent and never appear in the class's static parameters.
        for group in self._current_groups:
            for param_name, spec in group.params.items():
                if param_name in axis_keys:
                    continue
                widget = self._param_inputs.get(param_name)
                if widget is None:
                    continue
                try:
                    # param_form owns the ParamSpec -> value mapping (choices ->
                    # mapped value, bool -> checkbox, else parse text by spec.type).
                    param_values[param_name] = param_form.collect_value(widget, spec)
                except (ValueError, TypeError):
                    # Only a text field can fail to parse; choices/bool never raise.
                    raw = widget.text().strip() if hasattr(widget, "text") else ""
                    QMessageBox.warning(
                        self,
                        "Invalid Parameter",
                        f"Cannot parse '{raw}' as {spec.type.__name__} for parameter '{param_name}'.",
                    )
                    return None

        if self._axis_widget is not None:
            try:
                param_values.update(self._axis_widget.get_params())
            except ValueError as exc:
                QMessageBox.warning(self, "Invalid Sweep Parameters", str(exc))
                return None

        sample_info = self._get_sample_info()
        data_dir = self._get_data_dir()
        file_prefix = self._file_prefix_input.text().strip()
        return param_values, sample_info, data_dir, file_prefix

    def _build_procedure_instance(
        self, collected: tuple[dict, dict, str, str] | None = None
    ) -> BaseProcedure | None:
        """Build a procedure instance from current (or supplied) form values.

        This is the single construction path shared by the run-now and the
        queue flows, so a procedure is only ever instantiated in one place.

        Args:
            collected: An already-validated ``(param_values, sample_info,
                data_dir, file_prefix)`` tuple from ``_collect_params``. When
                ``None`` the form is read afresh (the run-now path).

        Returns:
            A ready ``BaseProcedure`` instance, or ``None`` on validation error.
        """
        if collected is None:
            collected = self._collect_params()
        if collected is None:
            return None
        param_values, sample_info, data_dir, file_prefix = collected
        index = self._proc_selector.currentIndex()
        cls = self._procedures[index]

        try:
            return cls(
                station=self._station,
                sample_info=sample_info,
                data_directory=data_dir,
                file_prefix=file_prefix,
                **param_values,
            )
        except Exception as exc:
            # A procedure may refuse construction (e.g. a nonzero field
            # requested on a magnet this station does not have). Surface it
            # as a form error — an uncaught raise in a Qt slot kills the app.
            logger.warning("Procedure %s refused construction: %s", cls.name, exc)
            QMessageBox.warning(self, "Cannot Build Procedure", str(exc))
            return None

    def _on_add_to_queue(self) -> None:
        """Freeze current form values and add a procedure entry to the queue."""
        result = self._collect_params()
        if result is None:
            return
        param_values, sample_info, data_dir, file_prefix = result
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
            file_prefix=file_prefix,
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
            # Record the aborted queue item as failed (abort_procedure does not
            # emit procedure_finished; it goes IDLE and auto-runs the next item).
            if self._queue_running:
                self._advance_queue_status(STATUS_FAILED)
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
        """Reset progress bar, clear the active procedure, advance the queue."""
        self._progress_bar.setValue(100)
        self._active_procedure = None
        # A queued run finished cleanly: mark it done and promote the next item
        # (the Orchestrator auto-chains run_queue() right after this signal).
        if self._queue_running:
            self._advance_queue_status(STATUS_DONE)

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
        # Measurement columns are instance/selection-dependent for a generic
        # procedure (they come from the chosen measurement VI), so ask the class
        # for them given the current structural selections rather than reading a
        # static class attribute.
        meas_keys = cls.live_plot_measurement_keys(self._station, self._current_selections())
        keys = ["unix_time"] + list(cls.sweep_data_keys) + system_keys + meas_keys
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

    def _on_run_queue(self) -> None:
        """Start the queue run, marking the first pending item as running.

        Wraps ``Orchestrator.run_queue`` so the queue's per-item status reflects
        execution: the first pending entry becomes ``running`` and, from here on,
        ``procedure_finished`` advances the status (see ``_advance_queue_status``).
        """
        first_pending = next(
            (e for e in self._queue if e.status == STATUS_PENDING), None
        )
        if first_pending is None:
            return
        self._queue_running = True
        first_pending.status = STATUS_RUNNING
        self._refresh_queue_list()
        self._orchestrator.run_queue()

    def _advance_queue_status(self, final_status: str) -> None:
        """Finalise the running entry and promote the next pending one."""
        for entry in self._queue:
            if entry.status == STATUS_RUNNING:
                entry.status = final_status
                break
        next_pending = next(
            (e for e in self._queue if e.status == STATUS_PENDING), None
        )
        if next_pending is not None:
            next_pending.status = STATUS_RUNNING
        else:
            self._queue_running = False
        self._refresh_queue_list()

    def _resync_orchestrator_queue(self) -> None:
        """Rebuild the Orchestrator's pending queue from this window's entries.

        The GUI queue is the source of truth. After a reorder or removal, the
        Orchestrator's not-yet-started queue is rebuilt in-place from the pending
        entries (each holds its built ``proc``), so it always matches the GUI —
        removing the old index-alignment fragility. The currently-running item
        was already popped by ``run_queue`` and is not re-added.
        """
        self._orchestrator._procedure_queue[:] = [
            entry.proc
            for entry in self._queue
            if entry.status == STATUS_PENDING and entry.proc is not None
        ]

    def _queue_move_up(self) -> None:
        """Move the selected queue item up by one position."""
        row = self._queue_list.currentRow()
        if row <= 0:
            return
        self._queue[row - 1], self._queue[row] = self._queue[row], self._queue[row - 1]
        self._resync_orchestrator_queue()
        self._refresh_queue_list()
        self._queue_list.setCurrentRow(row - 1)

    def _queue_move_down(self) -> None:
        """Move the selected queue item down by one position."""
        row = self._queue_list.currentRow()
        if row < 0 or row >= len(self._queue) - 1:
            return
        self._queue[row], self._queue[row + 1] = self._queue[row + 1], self._queue[row]
        self._resync_orchestrator_queue()
        self._refresh_queue_list()
        self._queue_list.setCurrentRow(row + 1)

    def _queue_remove(self) -> None:
        """Remove the selected item from the queue."""
        row = self._queue_list.currentRow()
        if row < 0 or row >= len(self._queue):
            return
        self._queue.pop(row)
        self._resync_orchestrator_queue()
        self._refresh_queue_list()

    def _entry_summary(self, entry: _QueueEntry) -> str:
        """Return the one-line queue summary for a queue entry (prefix-aware)."""
        summary_parts = self._queue_summary_parts(entry.cls, entry.params)
        label = f"[{entry.file_prefix}] {entry.cls.name}" if entry.file_prefix else entry.cls.name
        return f"{label} ({', '.join(summary_parts)})"

    def _refresh_queue_list(self) -> None:
        """Rebuild the QListWidget from self._queue, annotating non-pending status."""
        self._queue_list.clear()
        for idx, entry in enumerate(self._queue):
            label = f"{idx + 1}. {self._entry_summary(entry)}"
            if entry.status != STATUS_PENDING:
                label = f"{label}  — {entry.status}"
            self._queue_list.addItem(QListWidgetItem(label))

    @staticmethod
    def _queue_summary_parts(cls: type[BaseProcedure], params: dict) -> list[str]:
        """Build a short "key=value" summary of a procedure's sweep for the queue list.

        A sweep_axis-declaring procedure gets a mode-aware one-liner (e.g.
        ``field=-1.0->1.0`` or ``field=segments(3)``) instead of dumping the
        raw hidden parameter names.

        Args:
            cls: The procedure class.
            params: Its collected parameter values.

        Returns:
            A list of up to 3 "key=value" strings.
        """
        if cls.sweep_axis is not None:
            k = cls.sweep_axis.key
            mode = params.get(f"{k}_mode", "linear")
            if mode == "segments":
                n = len(params.get(f"{k}_segments", []))
                return [f"{k}=segments({n})"]
            if mode == "csv":
                return [f"{k}=csv"]
            return [f"{k}={params[f'{k}_start']}->{params[f'{k}_end']}"]

        sweep_keys = list(cls.sweep_parameters.keys()) or list(cls.parameters.keys())
        return [f"{k}={params[k]}" for k in sweep_keys[:3]]

    # ------------------------------------------------------------------
    # Session persistence (procedure selection, params, queue)
    # ------------------------------------------------------------------

    def _cache_current_params(self) -> None:
        """Store the current form's raw field text, keyed by (group, param).

        Raw text (not validated values) is cached so persistence never triggers
        the "Invalid Parameter" dialog. Keys are ``"{group.key}::{param_name}"``
        so a parameter that exists in more than one structural variant (e.g.
        ``voltmeter_range_V``, a drop-down under the delta VI but a text field
        under the DC VI) is cached separately per variant — switching Delta → DC
        → Delta restores each side's typed value. The cache accumulates across
        selections (entries are never dropped), so a value typed under one
        measurement VI survives switching away and back. The ``measurement_vi``
        selection itself is cached the same way. Sweep-axis widget state is not
        cached to the form (queue entries preserve full params).
        """
        if not (self._current_procedure_name and self._param_inputs):
            return
        cache = self._procedure_params.setdefault(self._current_procedure_name, {})
        for group in self._current_groups:
            for name in group.params:
                widget = self._param_inputs.get(name)
                if widget is not None:
                    cache[f"{group.key}::{name}"] = param_form.get_widget_raw(widget)

    def _apply_cached_params(self, procedure_name: str) -> None:
        """Fill the current form's fields with cached values, keyed by (group, param).

        Restores structural selections first (with signals blocked) and
        re-derives the groups once, so a cached ``measurement_vi`` selects the
        right measurement group before its parameter values are restored —
        without a recursive signal cascade that could cross-contaminate a
        shared parameter name (e.g. ``voltmeter_range_V``) between variants.
        """
        cached = self._procedure_params.get(procedure_name)
        if not cached:
            return

        # 1. Restore structural values (signals blocked), then re-render once so
        #    the correct variant groups are present.
        restored_structural = False
        for group in self._current_groups:
            for name, spec in group.params.items():
                if not spec.structural:
                    continue
                widget = self._param_inputs.get(name)
                raw = cached.get(f"{group.key}::{name}")
                if widget is not None and raw is not None:
                    widget.blockSignals(True)
                    param_form.set_widget_raw(widget, str(raw))
                    widget.blockSignals(False)
                    restored_structural = True
        if restored_structural:
            index = self._proc_selector.currentIndex()
            if 0 <= index < len(self._procedures):
                self._rerender_groups(
                    self._procedures[index], self._current_selections()
                )

        # 2. Restore all values on the now-final set of groups.
        for group in self._current_groups:
            for name in group.params:
                widget = self._param_inputs.get(name)
                raw = cached.get(f"{group.key}::{name}")
                if widget is not None and raw is not None:
                    widget.blockSignals(True)
                    param_form.set_widget_raw(widget, str(raw))
                    widget.blockSignals(False)

    def _procedure_by_name(self, name: str) -> type[BaseProcedure] | None:
        """Return the discovered procedure class whose name matches, or None."""
        for cls in self._procedures:
            if getattr(cls, "name", cls.__name__) == name:
                return cls
        return None

    def _select_procedure_by_name(self, name: str) -> None:
        """Select ``name`` in the dropdown and rebuild its form."""
        for i, cls in enumerate(self._procedures):
            if getattr(cls, "name", cls.__name__) == name:
                if self._proc_selector.currentIndex() == i:
                    self._on_procedure_selected(i)  # signal won't fire; rebuild directly
                else:
                    self._proc_selector.setCurrentIndex(i)  # fires _on_procedure_selected
                return

    def _build_entry_procedure(self, entry: _QueueEntry) -> BaseProcedure | None:
        """Build a procedure instance from a queue entry's stored values."""
        try:
            return entry.cls(
                station=self._station,
                sample_info=entry.sample_info,
                data_directory=entry.data_dir,
                file_prefix=entry.file_prefix,
                **entry.params,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "session: could not rebuild queued %s: %s", entry.cls.name, exc
            )
            return None

    def _restore_queue(self, items: list[QueueItemState]) -> None:
        """Rebuild the queue from persisted items, re-arming pending ones."""
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
                file_prefix=item.file_prefix,
                status=status,
            )
            if status == STATUS_PENDING:
                entry.proc = self._build_entry_procedure(entry)
                if entry.proc is not None:
                    self._orchestrator.queue_procedure(entry.proc)
            self._queue.append(entry)
        self._refresh_queue_list()

    def _restore_session(self, session_state: SessionState) -> None:
        """Apply a loaded session to the procedure form and queue."""
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
            self._on_procedure_selected(self._proc_selector.currentIndex())
        self._restore_queue(session_state.queue)

    def export_session_state(self, state: SessionState) -> None:
        """Write this window's selection, params, and queue into ``state``."""
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
                file_prefix=entry.file_prefix,
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
        if saved is not None and self.restoreGeometry(saved) and self._geometry_on_screen():
            return
        # Fall back when there is no saved geometry, restore fails, or the
        # geometry landed off-screen (monitor no longer attached).
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = int(available.width() * 0.7)
            height = int(available.height() * 0.7)
            self.resize(width, height)
            self.move(
                available.x() + (available.width() - width) // 2,
                available.y() + (available.height() - height) // 2,
            )

    def _geometry_on_screen(self) -> bool:
        """Return True if the window frame overlaps an attached screen enough to see."""
        frame = self.frameGeometry()
        for screen in QApplication.screens():
            overlap = screen.availableGeometry().intersected(frame)
            if overlap.width() >= 100 and overlap.height() >= 100:
                return True
        return False

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Persist the window geometry before the window closes.

        Args:
            event: The Qt close event.
        """
        app_settings.get_settings().setValue(_GEOMETRY_KEY, self.saveGeometry())
        super().closeEvent(event)
