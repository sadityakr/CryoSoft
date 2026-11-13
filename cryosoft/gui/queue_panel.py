"""QueuePanel — the run-queue list and its per-item lifecycle status.

The panel renders the queue; it does not own it. Waiting runs live in the
session layer as immutable **run specs** (`session/run_queue.py`), the engine
pulls the next one when it is ready, and this widget holds nothing the engine
owns — no built procedure, no reach into a private engine list. What it adds
on top of the queue is the one thing the queue has no opinion about: the
per-item lifecycle status an operator watches (pending -> running -> done,
aborted or failed), which outlives the spec, since a run that has started is
no longer waiting.

Every status is read off the engine's event stream, never guessed from a
button click: ``QueueChanged`` says what is waiting and whether the queue is
**held**, ``RunStarted``/``RunFinished`` name the queue entry they belong to
(the manifest's ``spec_id``), so a run started or ended by an agent, a fault
or an emergency is shown exactly like one the operator clicked.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import qtawesome as qta
from PyQt6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from cryosoft.core import events as ev
from cryosoft.core.orchestrator_proxy import OrchestratorProxy
from cryosoft.core.plan import ProbeSpec
from cryosoft.core.procedure import BaseProcedure
from cryosoft.gui.form_autosave import (
    STATUS_ABORTED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    QueueItemState,
)
from cryosoft.gui.theme import (
    BTN_CLASS_PRIMARY,
    BTN_CLASS_SECONDARY,
    LOG_ERROR,
    LOG_WARNING,
    STATUS_OK,
    TEXT_MUTED,
    TEXT_ON_ACCENT,
    TEXT_PRIMARY,
    ACCENT,
)
from cryosoft.session.run_queue import (
    KIND_PROCEDURE,
    RunQueueHost,
    RunSpec,
    RunValidation,
    queue_document,
    specs_from_document,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    # The GUI holds a Station only as a type (contract C19).
    from cryosoft.core.station import Station

logger = logging.getLogger(__name__)

#: The reduction a "Probe first" click asks for. The **Probe run** standard's
#: own defaults (first/middle/last, no averaging, waits capped) — a probe the
#: operator did not have to design is a probe they will actually run.
DEFAULT_PROBE_SPEC = ProbeSpec()

#: The filename prefix a probe queued from here carries, so its file is
#: recognisable on disk before anything is opened. The run itself also
#: declares ``run_kind = "probe"``, which is the authoritative marker.
PROBE_FILE_PREFIX = "probe"

#: The statuses a row keeps after its run left the queue — the history.
_FINISHED_STATUSES = frozenset({STATUS_DONE, STATUS_ABORTED, STATUS_FAILED})

#: ``RunFinished.status`` -> the row status it ends in. The engine reports a
#: clean end as ``done`` (older producers said ``completed``).
_FINISHED_STATUS = {
    "done": STATUS_DONE,
    "completed": STATUS_DONE,
    "aborted": STATUS_ABORTED,
    "failed": STATUS_FAILED,
}

#: One icon and colour per row status. Colours are theme tokens; every one
#: is >= 3:1 against the list's white surface (non-text marks).
_STATUS_ICONS = {
    STATUS_PENDING: ("fa5s.clock", TEXT_MUTED),
    STATUS_RUNNING: ("fa5s.play-circle", ACCENT),
    STATUS_DONE: ("fa5s.check-circle", STATUS_OK),
    STATUS_ABORTED: ("fa5s.stop-circle", LOG_WARNING),
    STATUS_FAILED: ("fa5s.times-circle", LOG_ERROR),
}

#: The export dialog's filter: a queue file is JSON.
_QUEUE_FILE_FILTER = "CryoSoft queue (*.json)"
#: The import dialog's filters: a queue file or an experiment's saved state,
#: from any folder — and any file at all, which is refused by name if it is
#: neither.
_IMPORT_FILE_FILTER = (
    "Queue or experiment state (*.json);;All files (*)"
)


@dataclass
class QueueEntry:
    """One row of the queue list: the queued spec plus its lifecycle status.

    The spec is the queue's own immutable record of what was asked for; the
    status is this panel's, because it describes what happened to the run
    afterwards and outlives the spec's time in the queue — a running or
    finished entry has already been pulled out of it.

    Attributes:
        spec: The queued ``RunSpec``.
        status: One of ``pending``, ``running``, ``done``, ``aborted``,
            ``failed``.
        started: True once the engine reported ``RunStarted`` for it. A row
            the engine pulled but has not started yet is ``running`` with
            ``started`` False — it resolves to ``failed``/``aborted`` if the
            start never happens.
        reason: Why it ended as it did (a failure's message), or ``""``.
    """

    spec: RunSpec
    status: str = STATUS_PENDING
    started: bool = False
    reason: str = ""


class QueuePanel(QGroupBox):
    """The Queue group box: list, per-item actions, hold, and Run Queue.

    Per-item actions act on the selected row: reorder, remove, and **Probe
    first**, which queues a cheap **probe run** of that row ahead of it — the
    same procedure and the same instruments, reduced to minutes, so an
    hours-long run is committed to only after something has answered "would
    this actually work". A probe goes through the same ``RunQueueHost`` add
    every other run does, so the setup's limits and the open experiment's
    envelope judge it identically, and what validation said about it — its
    findings and its **Duration estimate** — is shown inline and hung on the
    row as a tooltip.

    Whole-queue actions: **Run Queue** (which reads *Resume Queue* while the
    queue is held), **Hold after current run** (the **queue hold**), and the
    *More* menu — import and export a **queue file**, clear the finished
    rows, clear the waiting runs. A strip above the list says, in words, why
    the queue is held and what to do about it.

    ObjectNames (``queue_list``, ``queue_up_btn``, ``queue_down_btn``,
    ``queue_remove_btn``, ``queue_probe_btn``, ``queue_probe_label``,
    ``run_queue_btn``, ``queue_hold_btn``, ``queue_more_btn``,
    ``queue_hold_label``, ``queue_summary_label``) are preserved API.

    Args:
        station: The active Station instance (needed to validate and build a
            queued run headlessly).
        orchestrator: The client's ``OrchestratorProxy``.
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
        get_sample_info: Returns the current sample metadata. An imported
            queue file's runs are measured on the sample mounted NOW, so they
            take this rather than whatever the file was written with. ``None``
            keeps the file's.
        get_data_dir: Returns the directory runs write into now (the open
            experiment's data folder); imported runs take it, and the
            import/export dialogs open there. ``None`` keeps the file's.
    """

    def __init__(
        self,
        station: Station,
        orchestrator: OrchestratorProxy,
        parent: QWidget | None = None,
        get_experiment_info: Callable[[], dict[str, str]] | None = None,
        queue_host: RunQueueHost | None = None,
        procedure_classes: Sequence[type[BaseProcedure]] = (),
        get_sample_info: Callable[[], dict[str, str]] | None = None,
        get_data_dir: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__("Queue", parent)
        self._station = station
        self._orchestrator = orchestrator
        self._get_experiment_info = get_experiment_info
        self._get_sample_info = get_sample_info
        self._get_data_dir = get_data_dir
        self._classes: dict[str, type[BaseProcedure]] = {
            cls.__name__: cls for cls in procedure_classes
        }
        self._host = queue_host or self._standalone_host()

        # The rendered rows: every pending spec the queue holds, plus the
        # running/finished ones it no longer does.
        self._queue: list[QueueEntry] = []
        # The entry a reorder wants to stay selected. Held across the
        # rebuild rather than applied once, because the QueueChanged that
        # rebuilds the list can arrive after the reorder call has
        # returned — which is what happens with the engine on the
        # instrument thread.
        self._keep_selected: str | None = None
        # ``{spec_id: what validation said about it}`` — the findings and the
        # duration estimate a "Probe first" click produced, hung on the row
        # as its tooltip so the caveat stays attached to the run it is about
        # across every rebuild of the list.
        self._spec_notes: dict[str, str] = {}
        # The engine's **queue hold**, as the last QueueChanged reported it.
        self._held = False
        self._hold_reason = ""
        # Set when the waiting runs came back from an experiment's saved
        # state, so the strip can say where they came from until they run.
        self._restored_count = 0

        # The engine's one event stream, under whichever name this client
        # offers it. A client CONSUMES the stream rather than relaying it, so
        # the proxy renames ``event_emitted`` to ``event``; a window handed a
        # bare Orchestrator (the inline construction path the GUI suites take)
        # still finds the engine's own signal. The same deliberate rename
        # ``StatusMirror.of()`` accommodates for the mirror.
        stream = getattr(orchestrator, "event_emitted", None)
        if stream is None:
            stream = orchestrator.event
        stream.connect(self._on_event)

        vlay = QVBoxLayout(self)

        # Why the queue is held, and what to do about it — in words, above
        # the list, because a held queue that only shows a changed button
        # caption looks like a queue that is stuck.
        self._hold_label = QLabel("")
        self._hold_label.setObjectName("queue_hold_label")
        self._hold_label.setWordWrap(True)
        self._hold_label.setVisible(False)
        vlay.addWidget(self._hold_label)

        self._queue_list = QListWidget()
        self._queue_list.setObjectName("queue_list")
        self._queue_list.currentRowChanged.connect(lambda _row: self._update_buttons())
        vlay.addWidget(self._queue_list)

        # How many rows are in each state, on its own line: in the button row
        # it was the first thing squeezed out at the window's 50/50 split.
        self._summary_label = QLabel("")
        self._summary_label.setObjectName("queue_summary_label")
        self._summary_label.setProperty("class", "secondary_label")
        vlay.addWidget(self._summary_label)

        btn_row = QHBoxLayout()
        self._up_btn = QPushButton()
        self._up_btn.setObjectName("queue_up_btn")
        self._up_btn.setIcon(qta.icon("fa5s.arrow-up", color=TEXT_PRIMARY))
        self._up_btn.setToolTip("Move the selected waiting run up")
        self._up_btn.clicked.connect(self._queue_move_up)
        self._down_btn = QPushButton()
        self._down_btn.setObjectName("queue_down_btn")
        self._down_btn.setIcon(qta.icon("fa5s.arrow-down", color=TEXT_PRIMARY))
        self._down_btn.setToolTip("Move the selected waiting run down")
        self._down_btn.clicked.connect(self._queue_move_down)
        self._remove_btn = QPushButton("Remove")
        self._remove_btn.setObjectName("queue_remove_btn")
        self._remove_btn.setIcon(qta.icon("fa5s.trash", color=TEXT_PRIMARY))
        self._remove_btn.setToolTip(
            "Remove the selected run: a waiting run leaves the queue, a "
            "finished row leaves the list. A running run cannot be removed — "
            "abort it first."
        )
        self._remove_btn.clicked.connect(self._queue_remove)
        self._probe_btn = QPushButton("Probe first")
        self._probe_btn.setObjectName("queue_probe_btn")
        self._probe_btn.setIcon(qta.icon("fa5s.vial", color=TEXT_PRIMARY))
        self._probe_btn.setToolTip(
            "Queue a cheap probe of the selected run — same procedure, same "
            "instruments, reduced to minutes — ahead of it, so you find out "
            "whether it works before committing the hours."
        )
        self._probe_btn.clicked.connect(self._queue_probe_first)
        self._more_btn = QPushButton("More")
        self._more_btn.setObjectName("queue_more_btn")
        self._more_btn.setIcon(qta.icon("fa5s.ellipsis-h", color=TEXT_PRIMARY))
        self._more_btn.setToolTip("Import, export and clear the queue")
        self._more_menu = QMenu(self._more_btn)
        self._import_action = self._more_menu.addAction(
            qta.icon("fa5s.file-import", color=TEXT_PRIMARY), "Import queue…"
        )
        self._import_action.setToolTip(
            "Append the runs of a queue file, or of an earlier experiment's "
            "gui_state.json, from any folder (each run is re-checked against "
            "this setup and experiment)"
        )
        self._import_action.triggered.connect(self._on_import)
        self._export_action = self._more_menu.addAction(
            qta.icon("fa5s.file-export", color=TEXT_PRIMARY), "Export queue…"
        )
        self._export_action.setToolTip("Save the waiting runs to a queue file")
        self._export_action.triggered.connect(self._on_export)
        self._more_menu.addSeparator()
        self._clear_finished_action = self._more_menu.addAction(
            qta.icon("fa5s.broom", color=TEXT_PRIMARY), "Clear finished rows"
        )
        self._clear_finished_action.triggered.connect(self.clear_finished)
        self._clear_waiting_action = self._more_menu.addAction(
            qta.icon("fa5s.trash-alt", color=TEXT_PRIMARY), "Clear waiting runs…"
        )
        self._clear_waiting_action.triggered.connect(self._on_clear_waiting)
        self._more_menu.setToolTipsVisible(True)
        self._more_btn.setMenu(self._more_menu)
        # Two rows, not one: the per-item actions (which act on the selected
        # run) on the first, and the whole-queue actions on the second.
        # Keeping them all on one row pushed this group box's minimum width
        # past what the window's 50/50 split can give it at 1280 px, which
        # took the width back out of the parameter form and put a horizontal
        # scrollbar under it.
        btn_row.addWidget(self._up_btn)
        btn_row.addWidget(self._down_btn)
        btn_row.addWidget(self._remove_btn)
        btn_row.addWidget(self._probe_btn)
        btn_row.addStretch()
        btn_row.addWidget(self._more_btn)
        vlay.addLayout(btn_row)

        # What the last probe was told about itself: the validation's
        # findings and its **Duration estimate**, shown inline rather than in
        # a modal, because a caveat is something to read beside the queue
        # rather than dismiss to get back to it.
        self._probe_label = QLabel("")
        self._probe_label.setObjectName("queue_probe_label")
        self._probe_label.setProperty("class", "secondary_label")
        self._probe_label.setWordWrap(True)
        self._probe_label.setVisible(False)
        vlay.addWidget(self._probe_label)

        run_row = QHBoxLayout()
        run_row.addStretch()
        self._hold_btn = QPushButton("Hold after run")
        self._hold_btn.setObjectName("queue_hold_btn")
        self._hold_btn.setProperty("class", BTN_CLASS_SECONDARY)
        self._hold_btn.setIcon(qta.icon("fa5s.hand-paper", color=TEXT_PRIMARY))
        self._hold_btn.setCheckable(True)
        self._hold_btn.setToolTip(
            "Let the current run finish, then stop: the next waiting run "
            "starts only when you press Resume Queue. Click again to withdraw "
            "the hold."
        )
        self._hold_btn.clicked.connect(self._on_hold_clicked)
        run_row.addWidget(self._hold_btn)
        self._run_queue_btn = QPushButton("Run Queue")
        self._run_queue_btn.setObjectName("run_queue_btn")
        self._run_queue_btn.setProperty("class", BTN_CLASS_PRIMARY)
        self._run_queue_btn.setIcon(qta.icon("fa5s.forward", color=TEXT_ON_ACCENT))
        self._run_queue_btn.setToolTip("Run the waiting runs in order")
        self._run_queue_btn.clicked.connect(self._on_run_queue)
        run_row.addWidget(self._run_queue_btn)
        vlay.addLayout(run_row)

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
        """Return True while a queued run is executing (or about to start)."""
        return any(entry.status == STATUS_RUNNING for entry in self._queue)

    def is_held(self) -> bool:
        """Return True while the engine reports the queue as held."""
        return self._held

    def waiting_count(self) -> int:
        """Return how many runs are waiting in the queue."""
        return sum(1 for entry in self._queue if entry.status == STATUS_PENDING)

    def clear_waiting(self) -> None:
        """Remove every waiting run from the queue (no confirmation).

        Rows that already ran stay as history; a running row is unaffected —
        ending it is the Abort button's job.
        """
        self._host.clear()
        self._restored_count = 0

    def clear_finished(self) -> None:
        """Drop every finished (done, aborted, failed) row from the list."""
        self._queue = [
            entry for entry in self._queue if entry.status not in _FINISHED_STATUSES
        ]
        self._refresh_queue_list()

    # ------------------------------------------------------------------
    # The event stream: the only source of row status
    # ------------------------------------------------------------------

    def _on_event(self, event: object) -> None:
        """Update the rows from one engine event.

        The single subscription that makes this panel a VIEW: a run queued by
        an agent, popped by the engine, started, aborted or failed shows up
        here for the same reason the operator's own click does.

        Args:
            event: One event from the engine's stream. ``QueueChanged``,
                ``RunStarted``, ``RunFinished`` and ``StateChange`` are read;
                anything else is ignored.
        """
        if isinstance(event, ev.QueueChanged):
            self._held = bool(event.held)
            self._hold_reason = event.hold_reason if event.held else ""
            self._sync_from_queue()
        elif isinstance(event, ev.RunStarted):
            entry = self._entry_for_manifest(event.manifest)
            if entry is not None:
                entry.status = STATUS_RUNNING
                entry.started = True
                self._restored_count = 0
                self._refresh_queue_list()
        elif isinstance(event, ev.RunFinished):
            entry = self._entry_for_manifest(event.manifest)
            if entry is None:
                # A run that never reached RunStarted carries no spec_id; it
                # can only be the row the engine pulled and was starting.
                entry = self._unstarted_running_entry()
            if entry is not None:
                entry.status = _FINISHED_STATUS.get(event.status, STATUS_FAILED)
                entry.reason = event.reason
                self._refresh_queue_list()
        elif isinstance(event, ev.StateChange) and event.state in ("ERROR", "EMERGENCY"):
            # A run that failed while still being set up never emits
            # RunFinished (it never started); the engine's fall into ERROR or
            # EMERGENCY is what says it is over.
            entry = self._unstarted_running_entry()
            if entry is not None:
                entry.status = STATUS_FAILED
                entry.reason = f"did not start (engine went to {event.state})"
                self._refresh_queue_list()

    def _entry_for_manifest(self, manifest: Any) -> QueueEntry | None:
        """Return the row a run manifest names by ``spec_id``, if any.

        Args:
            manifest: ``RunStarted.manifest`` / ``RunFinished.manifest``.

        Returns:
            The row, or ``None`` for a run that did not come from this queue
            (e.g. Run now).
        """
        spec_id = manifest.get("spec_id", "") if isinstance(manifest, dict) else ""
        if not spec_id:
            return None
        for entry in self._queue:
            if entry.spec.spec_id == spec_id:
                return entry
        return None

    def _unstarted_running_entry(self) -> QueueEntry | None:
        """Return the row the engine pulled but has not reported started."""
        for entry in self._queue:
            if entry.status == STATUS_RUNNING and not entry.started:
                return entry
        return None

    def _sync_from_queue(self) -> None:
        """Re-derive the pending rows from the queue, keeping the finished ones.

        The queue is the source of truth for what is still waiting and in what
        order; this panel is the source of truth for what already happened.
        A pending row whose spec has left the queue either STARTED (the host
        says the engine took it — it becomes ``running`` and stays as history)
        or was removed (it disappears). Rows that are no longer pending stay
        where they are, in the order they ran, and the pending rows below them
        are rebuilt from the snapshot so an entry keeps its ``QueueEntry`` —
        and therefore its status — across a reorder.
        """
        snapshot = self._host.snapshot()
        waiting = {spec.spec_id for spec in snapshot}
        kept: list[QueueEntry] = []
        for entry in self._queue:
            if entry.status == STATUS_PENDING and entry.spec.spec_id not in waiting:
                if not self._host.was_taken(entry.spec.spec_id):
                    continue
                # A newer pull means an older row that never started is not
                # going to: the engine has moved past it.
                stale = self._unstarted_running_entry()
                if stale is not None:
                    stale.status = STATUS_FAILED
                    stale.reason = stale.reason or "did not start"
                entry.status = STATUS_RUNNING
            kept.append(entry)
        if self._held and self._hold_reason in (
            ev.QUEUE_HOLD_ABORTED,
            ev.QUEUE_HOLD_FAILED,
        ):
            # The queue was held because a run ended badly; a pulled row that
            # never started is the run that did.
            stale = next(
                (e for e in kept if e.status == STATUS_RUNNING and not e.started),
                None,
            )
            if stale is not None:
                stale.status = (
                    STATUS_ABORTED
                    if self._hold_reason == ev.QUEUE_HOLD_ABORTED
                    else STATUS_FAILED
                )
        by_id = {entry.spec.spec_id: entry for entry in kept}
        history = [entry for entry in kept if entry.status != STATUS_PENDING]
        pending = [
            by_id.get(spec.spec_id) or QueueEntry(spec=spec) for spec in snapshot
        ]
        if not pending:
            self._restored_count = 0
        self._queue = history + pending
        self._refresh_queue_list()
        if self._keep_selected is not None:
            self._select_spec(self._keep_selected)
            self._keep_selected = None

    # ------------------------------------------------------------------
    # Whole-queue actions
    # ------------------------------------------------------------------

    def _on_run_queue(self) -> None:
        """Ask the engine to start (or resume) the queue.

        Nothing is marked here: the engine decides whether and when the next
        run starts, and the row turns ``running`` when the engine pulls it.
        Pressing this is also what releases a **queue hold**.
        """
        if self.waiting_count() == 0:
            return
        self._restored_count = 0
        self._orchestrator.run_queue()

    def _on_hold_clicked(self, checked: bool) -> None:
        """Set or withdraw the **queue hold** ("stop after the current run").

        Args:
            checked: The button's new checked state.
        """
        # The button shows what the ENGINE says, not what was clicked: put it
        # back until the QueueChanged that answers this arrives.
        self._hold_btn.setChecked(self._held)
        self._orchestrator.set_queue_hold(checked)

    def _on_clear_waiting(self) -> None:
        """Remove every waiting run, after confirmation."""
        count = self.waiting_count()
        if count == 0:
            return
        answer = QMessageBox.question(
            self,
            "Clear Waiting Runs",
            f"Remove all {count} waiting run(s) from the queue? A run in "
            "progress is not affected.",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.clear_waiting()

    def _dialog_dir(self) -> str:
        """Return the directory the import/export dialogs open in."""
        if self._get_data_dir is None:
            return ""
        return self._get_data_dir() or ""

    def _on_export(self) -> None:
        """Save the waiting runs to a **queue file** chosen by the operator."""
        specs = self._host.snapshot()
        if not specs:
            QMessageBox.information(
                self, "Export Queue", "There are no waiting runs to export."
            )
            return
        start = str(Path(self._dialog_dir()) / "queue.json") if self._dialog_dir() else "queue.json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export queue", start, _QUEUE_FILE_FILTER
        )
        if not path:
            return
        self.export_queue_file(path)

    def export_queue_file(self, path: str | Path) -> bool:
        """Write the waiting runs to *path* as a **queue file**.

        Args:
            path: The file to write.

        Returns:
            True on success; a write failure is reported in a dialog.
        """
        document = queue_document(self._host.snapshot(), setup=self._setup_name())
        try:
            Path(path).write_text(json.dumps(document, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))
            return False
        logger.info("Exported %d queued run(s) to %s", len(document["runs"]), path)
        self._show_probe_note(f"Exported {len(document['runs'])} run(s) to {path}")
        return True

    def _setup_name(self) -> str:
        """Return the setup this queue runs on, for the queue file's header.

        A declaration read — the name the Station was built from — never an
        instrument call.
        """
        reader = getattr(self._station, "setup_name", None)
        name = reader() if callable(reader) else None
        return name or ""

    def _on_import(self) -> None:
        """Append the runs of a **queue file** chosen by the operator."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Import queue", self._dialog_dir(), _IMPORT_FILE_FILTER
        )
        if path:
            self.import_queue_file(path)

    def import_queue_file(self, path: str | Path) -> int:
        """Append every run of a **queue file** that passes validation.

        The file may also be an experiment's saved GUI state
        (``gui_state.json``), so an earlier experiment's queue can be reused
        without having been exported. Each run is re-validated against this setup and the experiment open
        now (``RunQueueHost.add_from_spec``), and takes the sample and data
        directory in use now: a queue file is a plan, and the plan is being
        carried out on today's sample. What was refused, and why, is reported
        in one dialog at the end rather than one per run.

        Args:
            path: The queue file to read.

        Returns:
            How many runs were queued.
        """
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(document, dict) and "format" not in document and "queue" in document:
                # An experiment's saved GUI state (gui_state.json): every run
                # it listed, whatever became of it — reusing an earlier
                # experiment's plan is the point of importing one.
                specs, problems = self._specs_from_saved_state(document)
                setup = ""
            else:
                specs, setup, problems = specs_from_document(document)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Cannot Import Queue", f"{path}:\n{exc}")
            return 0
        sample_info = self._get_sample_info() if self._get_sample_info else None
        data_dir = self._get_data_dir() if self._get_data_dir else None
        added = 0
        for index, spec in enumerate(specs, start=1):
            changes: dict[str, Any] = {}
            if sample_info is not None:
                changes["sample_info"] = dict(sample_info)
            if data_dir:
                changes["data_directory"] = data_dir
            queued, validation = self._host.add_from_spec(
                dataclasses.replace(spec, **changes)
            )
            if queued is None:
                problems.append(f"run {index} ({spec.run_class}): " + "; ".join(validation.messages()))
            else:
                added += 1
        here = self._setup_name()
        lines = [f"Queued {added} of {len(specs)} run(s) from {Path(path).name}."]
        if setup and here and setup != here:
            lines.append(f"The file was written for setup '{setup}', this is '{here}'.")
        if problems:
            lines.append("Not queued:")
            lines.extend(f"• {problem}" for problem in problems)
            QMessageBox.warning(self, "Import Queue", "\n".join(lines))
        else:
            self._show_probe_note(lines[0])
        logger.info("Imported %d of %d run(s) from %s", added, len(specs), path)
        return added

    def _specs_from_saved_state(
        self, document: dict[str, Any]
    ) -> tuple[list[RunSpec], list[str]]:
        """Turn an experiment's saved GUI state into specs to import.

        Args:
            document: A parsed ``gui_state.json`` (``FormAutosaveState``).

        Returns:
            ``(specs, problems)`` — one spec per item whose procedure this
            window knows (by display name or class name), one problem line
            per item it does not.

        Raises:
            ValueError: If the document lists no queue.
        """
        raw = document.get("queue")
        if not isinstance(raw, list):
            raise ValueError("the file holds no run queue")
        by_name: dict[str, type[BaseProcedure]] = {}
        for cls in self._classes.values():
            by_name[cls.__name__] = cls
            if getattr(cls, "name", ""):
                by_name[cls.name] = cls
        specs: list[RunSpec] = []
        problems: list[str] = []
        for index, payload in enumerate(raw, start=1):
            item = QueueItemState.from_dict(payload)
            cls = by_name.get(item.procedure)
            if cls is None:
                problems.append(f"run {index}: unknown procedure {item.procedure!r}")
                continue
            try:
                specs.append(
                    RunSpec(
                        kind=KIND_PROCEDURE,
                        run_class=cls.__name__,
                        params=dict(item.params),
                        sample_info=dict(item.sample_info),
                        data_directory=item.data_dir,
                        file_prefix=item.file_prefix,
                        probe_spec=dict(item.probe_spec),
                    )
                )
            except (TypeError, ValueError) as exc:
                problems.append(f"run {index}: {exc}")
        return specs, problems

    # ------------------------------------------------------------------
    # Per-item actions
    # ------------------------------------------------------------------

    def _selected_entry(self) -> QueueEntry | None:
        """Return the selected row, or ``None``."""
        row = self._queue_list.currentRow()
        if row < 0 or row >= len(self._queue):
            return None
        return self._queue[row]

    def _selected_pending(self) -> QueueEntry | None:
        """Return the selected row when it is still waiting, else ``None``.

        A row that is running or finished has already left the queue, so there
        is nothing to reorder or probe.
        """
        entry = self._selected_entry()
        return entry if entry is not None and entry.status == STATUS_PENDING else None

    def _queue_move_up(self) -> None:
        """Move the selected queue item up by one position."""
        self._move_selected(-1)

    def _queue_move_down(self) -> None:
        """Move the selected queue item down by one position."""
        self._move_selected(1)

    def _move_selected(self, offset: int) -> None:
        """Move the selected pending entry and keep the selection on it.

        The selection is restored twice on purpose. Once here, for a client
        whose ``QueueChanged`` came back inside ``move()`` and has already
        rebuilt the list; and once from ``_sync_from_queue()`` when it comes
        back later instead — which is what happens with the engine on the
        instrument thread, where the rebuild lands after this method has
        returned and would otherwise clear the selection the operator was
        working with.

        Args:
            offset: ``-1`` to move up, ``+1`` to move down.
        """
        entry = self._selected_pending()
        if entry is None:
            return
        self._keep_selected = entry.spec.spec_id
        if self._host.move(entry.spec.spec_id, offset):
            self._select_spec(entry.spec.spec_id)
        else:
            self._keep_selected = None

    def _queue_probe_first(self) -> None:
        """Queue a **probe run** of the selected item, ahead of the item.

        The whole point of a probe is that it comes FIRST: the same procedure
        class driving the same instruments through the same code path,
        reduced until it costs minutes, so an hours-long run is committed to
        only once something has already answered "would this actually work".
        So the probe is validated and queued through the ``RunQueueHost``
        exactly as any other run — the setup's limits and the open
        experiment's envelope judge it identically — and then moved to sit
        immediately before the run it probes.

        A refusal is a modal, like every other refusal that answers a direct
        click. What validation said about an accepted probe — its caveats and
        its **Duration estimate** — is shown inline and hung on the row as a
        tooltip instead, because it is something to read beside the queue
        rather than dismiss to get back to it.
        """
        entry = self._selected_pending()
        if entry is None:
            return
        spec = entry.spec
        if spec.kind != KIND_PROCEDURE:
            QMessageBox.warning(
                self,
                "Cannot Probe This Item",
                "Only a procedure can be probed — an operation is a servicing "
                "action, not a measurement to reduce.",
            )
            return
        run_class = self._classes.get(spec.run_class)
        if run_class is None:
            QMessageBox.warning(
                self,
                "Cannot Probe This Item",
                f"{spec.run_class} is not among the procedures this window "
                "discovered, so there is nothing to build a probe from.",
            )
            return
        probe, validation = self._host.add(
            run_class,
            spec.params,
            kind=spec.kind,
            sample_info=spec.sample_info,
            data_directory=spec.data_directory,
            file_prefix=spec.file_prefix or PROBE_FILE_PREFIX,
            probe_spec=DEFAULT_PROBE_SPEC.to_json(),
        )
        if probe is None:
            logger.warning(
                "Refused to queue a probe of %s: %s",
                spec.run_class,
                "; ".join(validation.messages()),
            )
            QMessageBox.warning(
                self, "Cannot Queue Probe", "\n".join(validation.messages())
            )
            return
        self._move_before(probe.spec_id, spec.spec_id)
        self._spec_notes[probe.spec_id] = self._probe_note(validation)
        self._keep_selected = probe.spec_id
        self._sync_from_queue()
        self._show_probe_note(self._spec_notes[probe.spec_id])

    def _move_before(self, spec_id: str, target_id: str) -> None:
        """Move one waiting run to sit immediately before another.

        The queue's own ``move()`` takes an offset within a bucket, which is
        what a reorder button needs; this is the position-based move a probe
        needs, expressed in terms of it.

        Args:
            spec_id: The entry to move (a probe, freshly appended).
            target_id: The entry it must end up in front of.
        """
        order = [waiting.spec_id for waiting in self._host.snapshot()]
        if spec_id not in order or target_id not in order:
            return
        offset = order.index(target_id) - order.index(spec_id)
        if offset:
            self._host.move(spec_id, offset)

    @staticmethod
    def _probe_note(validation: RunValidation) -> str:
        """Render what validation said about a probe, for the row's tooltip.

        Args:
            validation: The ``RunValidation`` the host answered with.

        Returns:
            One line naming the estimate (with the first of its assumptions,
            since an estimate a client cannot qualify is worse than none) and
            every finding, or a plain statement that there was nothing to
            report.
        """
        parts: list[str] = []
        seconds = validation.duration_estimate_s
        if seconds is not None:
            parts.append(f"probe ≈ {seconds / 60:.1f} min")
            assumptions = validation.estimate.assumptions if validation.estimate else ()
            if assumptions:
                parts.append(f"assuming {assumptions[0]}")
        parts.extend(validation.messages())
        return " · ".join(parts) if parts else "probe queued — nothing to report"

    def _show_probe_note(self, note: str) -> None:
        """Show one line of feedback inline under the queue.

        Args:
            note: The text to show (a probe's findings, an import result).
        """
        self._probe_label.setText(note)
        self._probe_label.setVisible(bool(note))

    def _queue_remove(self) -> None:
        """Remove the selected row.

        A waiting row is removed through the host. A finished row is history
        this panel alone holds — the engine's queue no longer knows it — so it
        is simply dropped from the list, as is a row the engine pulled but
        never started. A run that is actually running stays: ending it is
        Abort's job.
        """
        entry = self._selected_entry()
        if entry is None:
            return
        if entry.status == STATUS_PENDING:
            self._host.remove(entry.spec.spec_id)
            return
        if entry.status == STATUS_RUNNING and entry.started:
            return
        self._queue.remove(entry)
        self._refresh_queue_list()

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
        if spec.probe_spec and PROBE_FILE_PREFIX not in spec.file_prefix:
            # A probe is never science data, so it says so in the queue too —
            # once: a spec whose own file prefix already says "probe" is not
            # made clearer by saying it twice.
            label = f"{label} (probe)"
        if cls is None:
            return label
        summary_parts = self._queue_summary_parts(cls, spec.params)
        return f"{label} ({', '.join(summary_parts)})"

    def _entry_tooltip(self, entry: QueueEntry) -> str:
        """Return the row's tooltip: status, why, and what the run is."""
        lines = [f"Status: {entry.status}"]
        if entry.reason:
            lines.append(f"Reason: {entry.reason}")
        note = self._spec_notes.get(entry.spec.spec_id)
        if note:
            lines.append(note)
        sample = entry.spec.sample_info.get("sample_name") if entry.spec.sample_info else ""
        if sample:
            lines.append(f"Sample: {sample}")
        if entry.spec.data_directory:
            lines.append(f"Data: {entry.spec.data_directory}")
        return "\n".join(lines)

    def _refresh_queue_list(self) -> None:
        """Rebuild the list from ``self._queue``, then the strip and buttons."""
        selected = self._selected_entry()
        selected_id = selected.spec.spec_id if selected is not None else None
        self._queue_list.blockSignals(True)
        self._queue_list.clear()
        for idx, entry in enumerate(self._queue):
            label = f"{idx + 1}. {self._entry_summary(entry)}"
            if entry.status == STATUS_RUNNING and not entry.started:
                label = f"{label}  — starting"
            elif entry.status != STATUS_PENDING:
                label = f"{label}  — {entry.status}"
            icon_name, color = _STATUS_ICONS.get(entry.status, _STATUS_ICONS[STATUS_PENDING])
            item = QListWidgetItem(qta.icon(icon_name, color=color), label)
            item.setToolTip(self._entry_tooltip(entry))
            self._queue_list.addItem(item)
        self._queue_list.blockSignals(False)
        if selected_id is not None:
            self._select_spec(selected_id)
        self._refresh_hold_strip()
        self._refresh_summary()
        self._update_buttons()

    def _refresh_hold_strip(self) -> None:
        """Say in words why the queue is held, or where it came from."""
        waiting = self.waiting_count()
        text = ""
        if self._held and waiting:
            if self._hold_reason == ev.QUEUE_HOLD_ABORTED:
                text = (
                    "Queue on hold: the last run was aborted. Check, edit or "
                    "remove the waiting runs, then press Resume Queue."
                )
            elif self._hold_reason == ev.QUEUE_HOLD_FAILED:
                text = (
                    "Queue on hold: the last run failed. Fix the cause, then "
                    "press Resume Queue."
                )
            elif self.is_running():
                text = "The queue will hold after the current run finishes."
            elif self._restored_count:
                text = (
                    f"Restored {self._restored_count} waiting run(s) from this "
                    "experiment. Press Resume Queue to start them."
                )
            else:
                text = "Queue on hold. Press Resume Queue to continue."
        elif self._restored_count and waiting:
            text = (
                f"Restored {self._restored_count} waiting run(s) from this "
                "experiment. Press Run Queue to start them."
            )
        self._hold_label.setText(text)
        self._hold_label.setVisible(bool(text))

    def _refresh_summary(self) -> None:
        """Count the rows by status, e.g. ``3 waiting · 1 running · 2 done``."""
        order = (
            (STATUS_PENDING, "waiting"),
            (STATUS_RUNNING, "running"),
            (STATUS_DONE, "done"),
            (STATUS_ABORTED, "aborted"),
            (STATUS_FAILED, "failed"),
        )
        parts = []
        for status, word in order:
            count = sum(1 for entry in self._queue if entry.status == status)
            if count:
                parts.append(f"{count} {word}")
        self._summary_label.setText(" · ".join(parts) if parts else "Queue empty")

    def _update_buttons(self) -> None:
        """Enable exactly the actions that would do something right now."""
        waiting = self.waiting_count()
        selected = self._selected_entry()
        pending = selected is not None and selected.status == STATUS_PENDING
        self._up_btn.setEnabled(pending)
        self._down_btn.setEnabled(pending)
        self._probe_btn.setEnabled(pending)
        self._remove_btn.setEnabled(
            selected is not None
            and not (selected.status == STATUS_RUNNING and selected.started)
        )
        self._run_queue_btn.setText("Resume Queue" if self._held else "Run Queue")
        self._run_queue_btn.setToolTip(
            "Release the hold and start the next waiting run"
            if self._held
            else "Run the waiting runs in order"
        )
        self._run_queue_btn.setEnabled(waiting > 0 and not self.is_running())
        self._hold_btn.setEnabled(waiting > 0)
        self._hold_btn.setChecked(self._held)
        self._export_action.setEnabled(waiting > 0)
        self._clear_waiting_action.setEnabled(waiting > 0)
        self._clear_finished_action.setEnabled(
            any(entry.status in _FINISHED_STATUSES for entry in self._queue)
        )

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
            return [f"{k}={params.get(f'{k}_start')}->{params.get(f'{k}_end')}"]

        sweep_keys = list(cls.sweep_parameters.keys()) or list(cls.parameters.keys())
        return [f"{k}={params.get(k)}" for k in sweep_keys[:3]]

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def restore_items(
        self,
        items: list[QueueItemState],
        procedure_lookup: Callable[[str], type[BaseProcedure] | None],
    ) -> None:
        """Rebuild the queue from an experiment's saved items.

        Replaces whatever the panel showed: the queue belongs to the
        experiment, so another experiment's rows never linger. Waiting items
        are re-validated against the setup and envelope in force now
        (``RunQueueHost.add_from_spec``) — one that no longer passes comes
        back as a ``failed`` row naming why, rather than waiting in the queue
        to fail when it is pulled. Finished items come back as history. A
        restored queue is **held**, so it never starts on its own — not even
        by chaining after an unrelated Run-now run; Resume Queue starts it.

        Args:
            items: The persisted queue items.
            procedure_lookup: Maps a saved procedure name to its discovered
                class (``None`` for an unknown name, which is skipped).
        """
        self._host.clear()
        self._queue = []
        self._spec_notes.clear()
        self._probe_label.setVisible(False)
        history: list[QueueEntry] = []
        restored = 0
        for item in items:
            cls = procedure_lookup(item.procedure)
            if cls is None:
                logger.warning(
                    "session: unknown procedure %r in saved queue; skipping",
                    item.procedure,
                )
                continue
            spec = RunSpec(
                kind=KIND_PROCEDURE,
                run_class=cls.__name__,
                params=dict(item.params),
                sample_info=dict(item.sample_info),
                data_directory=item.data_dir,
                file_prefix=item.file_prefix,
                probe_spec=dict(item.probe_spec),
            )
            # A "running" item never finished (app closed mid-run): it is put
            # back as waiting, so the operator decides whether to run it again.
            if item.status in (STATUS_PENDING, STATUS_RUNNING):
                queued, validation = self._host.add_from_spec(spec)
                if queued is not None:
                    restored += 1
                    continue
                history.append(
                    QueueEntry(
                        spec=spec,
                        status=STATUS_FAILED,
                        reason="not re-queued: " + "; ".join(validation.messages()),
                    )
                )
            else:
                history.append(QueueEntry(spec=spec, status=item.status, reason=item.reason))
        self._queue = history
        self._restored_count = restored
        if restored:
            self._orchestrator.set_queue_hold(True)
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
                probe_spec=dict(entry.spec.probe_spec),
                reason=entry.reason,
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
        """Clear the queue (rows and specs)."""
        self._host.clear()
        self._queue.clear()
        self._spec_notes.clear()
        self._restored_count = 0
        self._probe_label.setVisible(False)
        self._refresh_queue_list()
