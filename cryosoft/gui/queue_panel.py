# ---
# description: |
#   QueuePanel: ProcedureWindow's run-queue group box — the queue list with
#   reorder/remove buttons and the Run Queue button, plus the queue's
#   bookkeeping: per-item lifecycle status (pending/running/done/failed),
#   keeping the Orchestrator's pending queue in sync with the GUI order, and
#   session restore/export of queued procedures. Extracted from
#   procedure_window.py; the GUI queue is the source of truth for what runs.
# entry_point: Not run directly. Hosted in ProcedureWindow's queue quadrant.
# dependencies:
#   - PyQt6 >= 6.5
#   - cryosoft.core.station (Station, to rebuild procedure instances)
#   - cryosoft.core.orchestrator (Orchestrator)
#   - cryosoft.gui.form_autosave (QueueItemState, status constants)
# input: |
#   QueueEntry objects built by ProcedureWindow from validated form values;
#   persisted QueueItemStates on session restore.
# process: |
#   add_entry() appends and arms the Orchestrator; run_queue() marks the first
#   pending entry running and starts the Orchestrator queue; the hosting
#   window forwards procedure_finished / abort to notify_finished() /
#   notify_aborted(), which finalise the running entry and promote the next.
#   Reorders and removals rebuild the Orchestrator's pending queue in place.
# output: |
#   The live queue list; procedures queued/run on the Orchestrator;
#   export_items() returns the persistable QueueItemStates.
# ---

"""QueuePanel — the run queue list, its statuses, and Orchestrator sync."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import qtawesome as qta
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

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

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@dataclass
class QueueEntry:
    """One row of the run queue, held in memory by the QueuePanel.

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


class QueuePanel(QGroupBox):
    """The Queue group box: list, reorder/remove buttons, and Run Queue.

    ObjectNames (``queue_list``, ``queue_up_btn``, ``queue_down_btn``,
    ``queue_remove_btn``, ``run_queue_btn``) are preserved API.

    Args:
        station: The active Station instance (rebuilds restored procedures).
        orchestrator: The active Orchestrator instance.
        parent: Optional Qt parent widget.
        get_experiment_info: Callable returning the session layer's experiment
            context, stamped into procedures rebuilt from a persisted queue.
            ``None`` means no session layer is wired — procedures get ``{}``.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: Orchestrator,
        parent: QWidget | None = None,
        get_experiment_info: Callable[[], dict[str, str]] | None = None,
    ) -> None:
        super().__init__("Queue", parent)
        self._station = station
        self._orchestrator = orchestrator
        self._get_experiment_info = get_experiment_info

        # Run queue as QueueEntry objects (spec + params + prefix + status).
        self._queue: list[QueueEntry] = []
        # True while a queued run is executing, so notify_finished advances
        # the queue's per-item status.
        self._queue_running = False

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

    # ------------------------------------------------------------------
    # Queue mutation
    # ------------------------------------------------------------------

    def add_entry(self, entry: QueueEntry) -> None:
        """Append an entry to the queue and arm its procedure on the Orchestrator.

        Args:
            entry: The frozen queue entry (with ``proc`` built, or ``None`` if
                construction was refused — the row still appears so the user
                sees what they queued).
        """
        self._queue.append(entry)
        self._refresh_queue_list()
        if entry.proc is not None:
            self._orchestrator.queue_procedure(entry.proc)

    def is_running(self) -> bool:
        """Return True while a queued run is executing."""
        return self._queue_running

    def _on_run_queue(self) -> None:
        """Start the queue run, marking the first pending item as running.

        Wraps ``Orchestrator.run_queue`` so the queue's per-item status reflects
        execution: the first pending entry becomes ``running`` and, from here on,
        ``notify_finished`` advances the status (see ``_advance_queue_status``).
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

    def _resync_orchestrator_queue(self) -> None:
        """Rebuild the Orchestrator's pending queue from this panel's entries.

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

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _entry_summary(self, entry: QueueEntry) -> str:
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
    # Session persistence
    # ------------------------------------------------------------------

    def _build_entry_procedure(self, entry: QueueEntry) -> BaseProcedure | None:
        """Build a procedure instance from a queue entry's stored values.

        Stamped with the experiment context read at build time, so a restored
        run belongs to the experiment that is open when it actually runs.
        """
        experiment_info = (
            self._get_experiment_info() if self._get_experiment_info else {}
        )
        try:
            return entry.cls(
                station=self._station,
                sample_info=entry.sample_info,
                data_directory=entry.data_dir,
                file_prefix=entry.file_prefix,
                experiment_info=experiment_info,
                **entry.params,
            )
        except (TypeError, ValueError) as exc:
            logger.warning(
                "session: could not rebuild queued %s: %s", entry.cls.name, exc
            )
            return None

    def restore_items(
        self,
        items: list[QueueItemState],
        procedure_lookup: Callable[[str], type[BaseProcedure] | None],
    ) -> None:
        """Rebuild the queue from persisted items, re-arming pending ones.

        Args:
            items: The persisted queue items.
            procedure_lookup: Maps a saved procedure name to its discovered
                class (``None`` for an unknown name, which is skipped).
        """
        self._queue.clear()
        self._orchestrator._procedure_queue.clear()
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
            entry = QueueEntry(
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

    def export_items(self) -> list[QueueItemState]:
        """Return the queue as persistable QueueItemStates."""
        return [
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

    def reset(self) -> None:
        """Clear the queue (GUI and Orchestrator) and stop status tracking."""
        self._queue.clear()
        self._orchestrator._procedure_queue.clear()
        self._queue_running = False
        self._refresh_queue_list()
