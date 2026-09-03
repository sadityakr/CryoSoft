"""QueuePanel — the run-queue list and its per-item lifecycle status.

The panel renders the queue; it does not own it. Waiting runs live in the
session layer as immutable **run specs** (`session/run_queue.py`), the engine
pulls the next one when it is ready, and this widget holds nothing the engine
owns — no built procedure, no reach into a private engine list. What it adds
on top of the queue is the one thing the queue has no opinion about: the
per-item lifecycle status an operator watches (pending -> running -> done or
failed), which outlives the spec, since a run that has started is no longer
waiting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import qtawesome as qta
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core.events import QueueChanged
from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.procedure import BaseProcedure
from cryosoft.core.station import Station
from cryosoft.gui.form_autosave import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    QueueItemState,
)
from cryosoft.gui.theme import BTN_CLASS_PRIMARY, TEXT_ON_ACCENT, TEXT_PRIMARY
from cryosoft.session.run_queue import KIND_PROCEDURE, RunQueueHost, RunSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

logger = logging.getLogger(__name__)


@dataclass
class QueueEntry:
    """One row of the queue list: the queued spec plus its lifecycle status.

    The spec is the queue's own immutable record of what was asked for; the
    status is this panel's, because it describes what happened to the run
    afterwards and outlives the spec's time in the queue — a running or
    finished entry has already been pulled out of it.

    Attributes:
        spec: The queued ``RunSpec``.
        status: One of ``pending``, ``running``, ``done``, ``failed``.
    """

    spec: RunSpec
    status: str = STATUS_PENDING


class QueuePanel(QGroupBox):
    """The Queue group box: list, reorder/remove buttons, and Run Queue.

    ObjectNames (``queue_list``, ``queue_up_btn``, ``queue_down_btn``,
    ``queue_remove_btn``, ``run_queue_btn``) are preserved API.

    Args:
        station: The active Station instance (needed to validate and build a
            queued run headlessly).
        orchestrator: The active Orchestrator instance.
        parent: Optional Qt parent widget.
        get_experiment_info: Callable returning the session layer's experiment
            context, stamped into a queued run when it is built. ``None``
            means no session layer is wired — runs get ``{}``.
        queue_host: The session layer's run queue (normally
            ``ExperimentManager.run_queue_host``). ``None`` means no session
            layer is wired: the panel builds a queue of its own from
            *procedure_classes* so the window still works standalone, and
            adopts the engine's pull seam only if nobody else has claimed it.
        procedure_classes: The discovered procedure classes, used to render a
            spec's summary line and to build the standalone queue's catalog.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
        get_experiment_info: Callable[[], dict[str, str]] | None = None,
        queue_host: RunQueueHost | None = None,
        procedure_classes: Sequence[type[BaseProcedure]] = (),
    ) -> None:
        super().__init__("Queue", parent)
        self._station = station
        self._orchestrator = orchestrator
        self._get_experiment_info = get_experiment_info
        self._classes: dict[str, type[BaseProcedure]] = {
            cls.__name__: cls for cls in procedure_classes
        }
        self._host = queue_host or self._standalone_host()

        # The rendered rows: every pending spec the queue holds, plus the
        # running/finished ones it no longer does.
        self._queue: list[QueueEntry] = []
        # True while a queued run is executing, so notify_finished advances
        # the queue's per-item status.
        self._queue_running = False

        orchestrator.event_emitted.connect(self._on_event)

        vlay = QVBoxLayout(self)

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

        self._sync_from_queue()

    def _standalone_host(self) -> RunQueueHost:
        """Build this window's own run queue, for a window with no session layer.

        Unit tests (and any launch without an ``ExperimentManager``) still get
        a working queue: the setup's own ``control_limits`` guard every add
        exactly as they do in production — there is simply no open experiment,
        so no envelope narrows them. The engine's pull seam is adopted only if
        nothing has claimed it, so this can never displace the wiring
        ``main.py`` installed.

        Returns:
            A ``RunQueueHost`` over this window's discovered procedures.
        """
        host = RunQueueHost(
            station=self._station,
            run_catalog=dict(self._classes),
            publish=lambda actor: self._orchestrator.publish_queue(actor=actor),
            experiment_info=self._get_experiment_info,
        )
        if self._orchestrator.next_procedure is None:
            self._orchestrator.next_procedure = host.next_run
            self._orchestrator.queue_snapshot = host.entries
        return host

    # ------------------------------------------------------------------
    # Queue mutation
    # ------------------------------------------------------------------

    def add_run(
        self,
        procedure_cls: type[BaseProcedure],
        params: dict[str, Any],
        sample_info: dict[str, str],
        data_directory: str,
        file_prefix: str = "",
    ) -> RunSpec | None:
        """Queue one run, refusing it with its findings if validation fails.

        Validation happens here, at add time: the run is built headlessly and
        thrown away, so a parameter outside a setup limit or the open
        experiment's envelope is refused while the operator is still looking
        at the form — not an hour later when the run would have started. The
        refusal is a modal because it answers a direct click, exactly like the
        Run-now path's build refusal.

        Args:
            procedure_cls: The procedure class to queue.
            params: The parameter values collected from the form.
            sample_info: Sample metadata captured with the item.
            data_directory: Data directory captured with the item.
            file_prefix: Optional filename prefix.

        Returns:
            The queued ``RunSpec``, or ``None`` when it was refused.
        """
        spec, validation = self._host.add(
            procedure_cls,
            params,
            sample_info=sample_info,
            data_directory=data_directory,
            file_prefix=file_prefix,
        )
        if spec is None:
            logger.warning(
                "Refused to queue %s: %s",
                procedure_cls.__name__,
                "; ".join(validation.messages()),
            )
            QMessageBox.warning(
                self, "Cannot Queue Run", "\n".join(validation.messages())
            )
            return None
        return spec

    def is_running(self) -> bool:
        """Return True while a queued run is executing."""
        return self._queue_running

    def _on_event(self, event: object) -> None:
        """Re-render the queue whenever the engine says it changed.

        The single subscription that makes this panel a VIEW: a run queued by
        an agent, popped by the engine, or reordered elsewhere shows up here
        for the same reason the operator's own click does.

        Args:
            event: One event from the engine's stream; anything but a
                ``QueueChanged`` is ignored.
        """
        if isinstance(event, QueueChanged):
            self._sync_from_queue()

    def _sync_from_queue(self) -> None:
        """Re-derive the pending rows from the queue, keeping the finished ones.

        The queue is the source of truth for what is still waiting and in what
        order; this panel is the source of truth for what already happened.
        Rows that are no longer pending stay where they are, in the order they
        ran, and the pending rows below them are rebuilt from the snapshot so
        an entry keeps its ``QueueEntry`` — and therefore its status — across a
        reorder.
        """
        by_id = {entry.spec.spec_id: entry for entry in self._queue}
        history = [entry for entry in self._queue if entry.status != STATUS_PENDING]
        pending = [
            by_id.get(spec.spec_id) or QueueEntry(spec=spec)
            for spec in self._host.snapshot()
        ]
        self._queue = history + pending
        self._refresh_queue_list()

    def _on_run_queue(self) -> None:
        """Start the queue run, marking the first pending item as running.

        Wraps ``Orchestrator.run_queue`` so the queue's per-item status
        reflects execution: the first pending entry becomes ``running`` and,
        from here on, ``notify_finished`` advances the status (see
        ``_advance_queue_status``). The engine still decides *when* the run
        starts — this asks, it never starts anything itself.
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

    def notify_finished(self) -> None:
        """Advance the queue after a clean procedure finish (if a run is active).

        The Orchestrator auto-chains ``run_queue()`` right after emitting
        ``procedure_finished``, so this only updates per-item status.
        """
        if self._queue_running:
            self._advance_queue_status(STATUS_DONE)

    def notify_aborted(self) -> None:
        """Record the aborted queue item as failed (if a run is active).

        ``abort_procedure`` does not emit ``procedure_finished``; it goes IDLE
        and auto-runs the next item, so the running entry is finalised here.
        """
        if self._queue_running:
            self._advance_queue_status(STATUS_FAILED)

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

    def _selected_pending(self) -> QueueEntry | None:
        """Return the selected row when it is still waiting, else ``None``.

        A row that is running or finished has already left the queue, so there
        is nothing to remove or reorder.
        """
        row = self._queue_list.currentRow()
        if row < 0 or row >= len(self._queue):
            return None
        entry = self._queue[row]
        return entry if entry.status == STATUS_PENDING else None

    def _queue_move_up(self) -> None:
        """Move the selected queue item up by one position."""
        entry = self._selected_pending()
        if entry is None:
            return
        if self._host.move(entry.spec.spec_id, -1):
            self._select_spec(entry.spec.spec_id)

    def _queue_move_down(self) -> None:
        """Move the selected queue item down by one position."""
        entry = self._selected_pending()
        if entry is None:
            return
        if self._host.move(entry.spec.spec_id, 1):
            self._select_spec(entry.spec.spec_id)

    def _queue_remove(self) -> None:
        """Remove the selected item from the queue."""
        entry = self._selected_pending()
        if entry is None:
            return
        self._host.remove(entry.spec.spec_id)

    def _select_spec(self, spec_id: str) -> None:
        """Keep the selection on one entry after the list was rebuilt."""
        for row, entry in enumerate(self._queue):
            if entry.spec.spec_id == spec_id:
                self._queue_list.setCurrentRow(row)
                return

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _entry_summary(self, entry: QueueEntry) -> str:
        """Return the one-line queue summary for a queue entry (prefix-aware)."""
        spec = entry.spec
        cls = self._classes.get(spec.run_class)
        name = getattr(cls, "name", "") or spec.run_class
        label = f"[{spec.file_prefix}] {name}" if spec.file_prefix else name
        if cls is None:
            return label
        summary_parts = self._queue_summary_parts(cls, spec.params)
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
    # Session persistence
    # ------------------------------------------------------------------

    def restore_items(
        self,
        items: list[QueueItemState],
        procedure_lookup: Callable[[str], type[BaseProcedure] | None],
    ) -> None:
        """Rebuild the queue from persisted items, re-queueing the pending ones.

        A restored spec is put back exactly as it was saved rather than
        re-validated: the operator gets the queue they left behind, and a
        stored value that has since gone out of bounds is reported when the
        engine pulls the run, not silently dropped while reopening a session.

        Args:
            items: The persisted queue items.
            procedure_lookup: Maps a saved procedure name to its discovered
                class (``None`` for an unknown name, which is skipped).
        """
        self._host.clear()
        self._queue.clear()
        history: list[QueueEntry] = []
        for item in items:
            cls = procedure_lookup(item.procedure)
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
            spec = RunSpec(
                kind=KIND_PROCEDURE,
                run_class=cls.__name__,
                params=dict(item.params),
                sample_info=dict(item.sample_info),
                data_directory=item.data_dir,
                file_prefix=item.file_prefix,
            )
            if status == STATUS_PENDING:
                self._host.add_spec(spec)
            else:
                history.append(QueueEntry(spec=spec, status=status))
        self._queue = history
        self._sync_from_queue()

    def export_items(self) -> list[QueueItemState]:
        """Return the queue as persistable QueueItemStates."""
        return [
            QueueItemState(
                procedure=self._saved_name(entry.spec.run_class),
                params=dict(entry.spec.params),
                sample_info=dict(entry.spec.sample_info),
                data_dir=entry.spec.data_directory,
                file_prefix=entry.spec.file_prefix,
                status=entry.status,
            )
            for entry in self._queue
        ]

    def _saved_name(self, run_class: str) -> str:
        """Return the display name a queue item is persisted under.

        Saved state names a procedure the way the selector does, so the file
        format is unchanged by the queue moving to class names internally.
        """
        cls = self._classes.get(run_class)
        return getattr(cls, "name", "") or run_class

    def reset(self) -> None:
        """Clear the queue (rows and specs) and stop status tracking."""
        self._host.clear()
        self._queue.clear()
        self._queue_running = False
        self._refresh_queue_list()
