# ---
# description: |
#   ProcedureWindow: PyQt6 window for building, queuing, and running measurement
#   procedures. A composition shell over the GUI components: the parameter
#   quadrant is a ProcedureParamsPanel (cryosoft.gui.procedure_params_panel,
#   which renders each procedure's ParamGroups via cryosoft.gui.param_form and
#   hosts the SweepAxisWidget), the run queue is a QueuePanel
#   (cryosoft.gui.queue_panel), and procedure classes come from
#   cryosoft.gui.procedure_discovery. Fixed 2x2 quadrant grid (top-left params,
#   top-right queue over a concise status log, bottom-left/right Plot 1/Plot 2),
#   the same nested-QSplitter pattern as MonitorWindow. Sample info is read from
#   MonitorWindow via callables. This module owns the quadrant assembly, the
#   Orchestrator signal wiring, the run/queue/abort flows, the live plots, and
#   session/geometry persistence.
# entry_point: Not run directly. Opened via MonitorWindow Procedures menu.
# dependencies:
#   - PyQt6 >= 6.5
#   - pyqtgraph >= 0.13
#   - cryosoft.core.station (Station)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.core.procedure (BaseProcedure)
#   - cryosoft.gui.procedure_discovery (procedure auto-discovery)
#   - cryosoft.gui.procedure_params_panel (ProcedureParamsPanel)
#   - cryosoft.gui.queue_panel (QueuePanel, QueueEntry)
#   - cryosoft.gui.live_plot_panel (LivePlotPanel)
#   - cryosoft.gui.window_geometry (geometry persistence helpers)
# input: |
#   Station instance, Orchestrator instance, and two callables (get_sample_info,
#   get_data_dir) provided by MonitorWindow.
# process: |
#   The params panel signals add/run requests and structural form changes; this
#   window validates the form into a procedure instance (the single
#   construction path shared by run-now and the queue), submits it through
#   orchestrator.run_procedure()/queue_procedure(), and keeps the plot axis
#   selectors in sync with the selected procedure and mux routes.
# output: |
#   A QMainWindow. Two live plots update via orchestrator.measurement_ready.
# ---

"""ProcedureWindow — procedure builder, queue, and live-data monitor (shell)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import qtawesome as qta
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import Station
from cryosoft.gui import window_geometry
from cryosoft.gui.live_plot_panel import LivePlotPanel
from cryosoft.gui.notification_banner import NotificationBanner
from cryosoft.gui.form_autosave import STATUS_PENDING, SessionState
from cryosoft.gui.procedure_discovery import discover_procedures
from cryosoft.gui.procedure_params_panel import ProcedureParamsPanel
from cryosoft.gui.queue_panel import QueueEntry, QueuePanel
from cryosoft.gui.theme import (
    BANNER_SEVERITY_ERROR,
    BANNER_SEVERITY_WARNING,
    BTN_CLASS_DANGER,
    BTN_CLASS_SECONDARY,
    PLOT_SERIES,
    TEXT_ON_ACCENT,
    TEXT_PRIMARY,
)

logger = logging.getLogger(__name__)

_GEOMETRY_KEY = "ProcedureWindow/geometry"  # QSettings key for saved window geometry
# Max lines kept in the concise Status log before old lines are trimmed
# (matches the Monitor detailed log's cap; bounds a long run's memory use).
_STATUS_MAX_LINES = 500


class ProcedureWindow(QMainWindow):
    """Procedure builder, queue manager, and live-data window.

    A fixed 2x2 quadrant grid, the same nested-QSplitter pattern as
    MonitorWindow: top-left is the :class:`ProcedureParamsPanel` (procedure
    selector + full parameter form), top-right is the :class:`QueuePanel`
    over a concise Status log (a draggable vertical split), and the bottom
    row is Plot 1 (left) / Plot 2 (right). Every splitter boundary is
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
        initial_session: Persisted form-autosave content to restore, if any.
        get_experiment_info: Callable returning the session layer's experiment
            context (``SessionManager.experiment_context()``), stamped into
            every built procedure as ``experiment_info``. ``None`` means no
            session layer is wired (unit tests) — procedures get ``{}``.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        get_sample_info: Callable[[], dict[str, str]],
        get_data_dir: Callable[[], str],
        parent: QWidget | None = None,
        initial_session: SessionState | None = None,
        get_experiment_info: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        super().__init__(parent)
        self._station = station
        self._orchestrator = orchestrator
        self._get_sample_info = get_sample_info
        self._get_data_dir = get_data_dir
        # Session-layer experiment context, stamped into every built
        # procedure's HDF5 metadata. None (unit tests, no session layer)
        # means "no experiment": procedures get an empty context.
        self._get_experiment_info = get_experiment_info

        # Active procedure reference (set on run)
        self._active_procedure: BaseProcedure | None = None
        # Live plot: full datapoint history (each entry is the enriched dict from measurement_ready)
        self._datapoints: list[dict] = []

        self.setWindowTitle("CryoSoft — Procedure")
        window_geometry.restore_or_center(self, _GEOMETRY_KEY, fraction=0.7)

        self._build_ui()
        self._connect_signals()

        # Render the first procedure's form only now that the plot panels
        # exist and the params panel's structure_changed is connected — the
        # initial emission drives the plot axis selectors.
        self._params_panel.initialize_selection()

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
        self._params_panel = ProcedureParamsPanel(self._station, discover_procedures())
        top_right = self._build_queue_quadrant()
        bottom_left, bottom_right = self._build_plot_quadrants()

        self._left_splitter = QSplitter(Qt.Orientation.Vertical)
        self._left_splitter.setObjectName("left_splitter")
        self._left_splitter.setChildrenCollapsible(False)
        self._left_splitter.addWidget(self._params_panel)
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
        # The left pane carries the four-column parameter form (Sweep, System,
        # Measurement, scanner), which needs more width than the queue/plot-2
        # pane; bias the initial split left so all four columns are visible
        # without a horizontal scrollbar at a ~1280 px window. Still draggable.
        self._main_splitter.setSizes([880, 380])

        root.addWidget(self._main_splitter, stretch=1)

        # ── Progress bar (full-width) ─────────────────────────────────
        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("progress_bar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        root.addWidget(self._progress_bar)

        # ── Control buttons ───────────────────────────────────────────
        root.addLayout(self._build_control_buttons())

    def _build_queue_quadrant(self) -> QWidget:
        """Build the top-right quadrant: the QueuePanel over the concise Status log.

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
        self._queue_panel = QueuePanel(
            self._station, self._orchestrator, get_experiment_info=self._experiment_info
        )
        self._queue_panel.setMinimumHeight(120)
        split.addWidget(self._queue_panel)
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
            route_selector_name="route1_selector",
            plot_object_name="live_plot",
        )
        self._plot2 = LivePlotPanel(
            "Plot 2", PLOT_SERIES[1],
            x_selector_name="x2_axis_selector",
            y_selector_name="y2_axis_selector",
            route_selector_name="route2_selector",
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

        self._params_panel.add_to_queue_requested.connect(self._on_add_to_queue)
        self._params_panel.run_now_requested.connect(self._on_run_now)
        self._params_panel.structure_changed.connect(self._populate_axis_selectors)
        self._params_panel.routes_changed.connect(self._refresh_route_selectors)

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

    def _collect_params(self) -> tuple[dict, dict, str, str] | None:
        """Read and validate all form inputs, composing the run context.

        Returns:
            ``(param_values, sample_info, data_dir, file_prefix)`` on success,
            or ``None`` if a field cannot be parsed.
        """
        param_values = self._params_panel.collect_values()
        if param_values is None:
            return None
        sample_info = self._get_sample_info()
        data_dir = self._get_data_dir()
        file_prefix = self._params_panel.file_prefix()
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
        cls = self._params_panel.current_class()
        if cls is None:
            return None

        try:
            return cls(
                station=self._station,
                sample_info=sample_info,
                data_directory=data_dir,
                file_prefix=file_prefix,
                experiment_info=self._experiment_info(),
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
        cls = self._params_panel.current_class()
        if cls is None:
            return

        # Construct through the shared _build_procedure_instance path (reusing the
        # params we already collected) instead of re-implementing cls(...) here.
        proc = self._build_procedure_instance(result)
        self._queue_panel.add_entry(
            QueueEntry(
                cls=cls,
                params=param_values,
                sample_info=sample_info,
                data_dir=data_dir,
                file_prefix=file_prefix,
                status=STATUS_PENDING,
                proc=proc,
            )
        )

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
            self._queue_panel.notify_aborted()
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
        self._queue_panel.notify_finished()

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
        meas_keys = cls.live_plot_measurement_keys(
            self._station, self._params_panel.current_selections()
        )
        keys = ["unix_time"] + list(cls.sweep_data_keys) + system_keys + meas_keys
        if not keys:
            return

        default_x = cls.default_x_key if cls.default_x_key in keys else keys[0]

        # Per-panel Y defaults: Plot 1 → voltage, Plot 2 → current.
        self._plot1.set_available_keys(keys, default_x, "voltage_V")
        self._plot2.set_available_keys(keys, default_x, "current_A")

        # Refresh route selectors based on currently-selected routes (if any).
        self._refresh_route_selectors()

    def _refresh_route_selectors(self) -> None:
        """Update plot route selectors with currently-selected routes.

        Checks if a mux group exists (scanner VI present) and which routes are
        checked; passes that to both plot panels' ``set_available_routes()``.
        If no scanner VI exists, passes None to hide the selector.
        """
        if self._params_panel.has_mux_group():
            routes: list[str] | None = self._params_panel.current_mux_routes()
        else:
            routes = None
        self._plot1.set_available_routes(routes)
        self._plot2.set_available_routes(routes)

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
    # Session persistence (procedure selection, params, queue)
    # ------------------------------------------------------------------

    def _experiment_info(self) -> dict[str, str]:
        """Return the current experiment context for procedure construction.

        Read at build time (not queue time): a queued run is stamped with the
        experiment that is open when it actually gets built and run.

        Returns:
            ``SessionManager.experiment_context()``, or ``{}`` when no session
            layer is wired.
        """
        if self._get_experiment_info is None:
            return {}
        return self._get_experiment_info()

    def _restore_session(self, session_state: SessionState) -> None:
        """Apply a loaded session to the procedure form and queue."""
        self._params_panel.restore_session(session_state)
        self._queue_panel.restore_items(
            session_state.queue, self._params_panel.procedure_by_name
        )

    def export_session_state(self, state: SessionState) -> None:
        """Write this window's selection, params, and queue into ``state``."""
        self._params_panel.export_session_state(state)
        state.queue = self._queue_panel.export_items()

    def reset_session(self) -> None:
        """Clear the queue and cached params, resetting the form to defaults."""
        self._queue_panel.reset()
        self._params_panel.reset()

    # ------------------------------------------------------------------
    # Window geometry + lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt override)
        """Persist the window geometry before the window closes.

        Args:
            event: The Qt close event.
        """
        window_geometry.save_geometry(self, _GEOMETRY_KEY)
        super().closeEvent(event)
