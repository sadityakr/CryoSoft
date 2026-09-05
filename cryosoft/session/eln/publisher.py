"""The publisher — what gets queued when, and the GUI-side drain that sends it.

Three responsibilities, and deliberately no fourth:

1. **Render and queue.** A run that finishes is rendered to its final entry
   text *at that moment* (``templates.py``) and appended to that experiment's
   **Outbox**. The same happens on a manual export. Queuing touches no
   network, so it costs a finished run nothing.
2. **Drain.** ``drain_once()`` sends at most one queued job, and is meant to
   be called from a **QTimer in the GUI process** — never from the
   Orchestrator's tick. This keeps the repository's one-thread promise intact:
   a slow upload delays the next upload, never a hardware write.
3. **Record.** When the backend confirms an entry, the publisher hands the
   reference to ``ExperimentManager.set_run_eln_link()``. It never edits a
   record or writes a file itself — the manager is the single writer of
   experiment state, exactly as the Orchestrator is the single writer to
   hardware.

**Publishing is opt-in and never silent.** With no user-level settings file
(the default) nothing is constructed and nothing leaves the machine. With
``auto_publish`` off, only an explicit export queues anything. And nothing is
ever published for a run that belongs to no experiment: an ad-hoc run has no
record to attach an entry to, and uploading it would be a surprise.

**An approved draft is not a second write path.** ``export_draft()`` queues
one ordinary outbox job whose title, body and tags come from an approved
**draft entry** instead of from the renderers, stamping the model and prompt
digest into the entry's metadata. It is the same journal, the same
idempotency, the same drain — the draft is data, and only the text differs.

**Backends are discovered, not listed.** ``discover_backends()`` walks the
package for ``ElnAdapter`` subclasses and keys them by their declared
``backend``, so a new backend module is selectable from the settings file the
moment its file exists — no registry to edit, exactly as drivers, VIs, and
procedures are discovered elsewhere in this repository.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Mapping
from typing import Any

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from cryosoft.session.eln.adapter import ElnAdapter
from cryosoft.session.eln.drafting import DraftEntry, manifest_from_run
from cryosoft.session.eln.outbox import (
    DRAIN_IDLE,
    DRAIN_PUBLISHED,
    DRAIN_RETRY,
    JOB_PUBLISH_RUN,
    DrainResult,
    Outbox,
    OutboxJob,
)
from cryosoft.session.eln.settings import ElnSettings
from cryosoft.session.eln.templates import (
    render_run_body,
    render_run_metadata,
    render_run_title,
)
from cryosoft.session.manager import ExperimentManager
from cryosoft.session.models import ElnLink, RunRecord

logger = logging.getLogger(__name__)

#: Publish state: everything queued has reached the notebook.
PUBLISH_SYNCED = "synced"

#: Publish state: jobs are queued and the last attempt did not fail.
PUBLISH_PENDING = "pending"

#: Publish state: the last attempt failed; the queue is retrying with backoff.
PUBLISH_OFFLINE = "offline"

#: Publish state: no usable settings — the track is switched off entirely.
PUBLISH_DISABLED = "disabled"


def discover_backends() -> dict[str, type[ElnAdapter]]:
    """Return every available ELN backend, keyed by its ``backend`` id.

    Walks ``cryosoft.session.eln`` for ``ElnAdapter`` subclasses rather than
    consulting a hand-maintained table, so adding a backend is adding a file.

    Returns:
        ``{backend_id: adapter_class}``; ids are the classes' own declared
        ``backend`` attributes.
    """
    import cryosoft.session.eln as package

    backends: dict[str, type[ElnAdapter]] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        for value in vars(module).values():
            if (
                isinstance(value, type)
                and issubclass(value, ElnAdapter)
                and value is not ElnAdapter
                and value.__module__ == module.__name__
                and value.backend
            ):
                backends[value.backend] = value
    return backends


def _manifest_of(event: Any) -> dict[str, Any]:
    """Return the run manifest carried by whatever the caller passed.

    Accepts both shapes the application produces: a ``RunFinished`` contract
    event (whose ``manifest`` attribute holds it) and the Orchestrator's
    ``run_finished`` signal payload, which *is* the manifest dict.

    Args:
        event: A ``RunFinished`` event, or a plain manifest mapping.

    Returns:
        The manifest as a plain dict; empty when the argument is neither.
    """
    manifest = getattr(event, "manifest", None)
    if isinstance(manifest, dict):
        return dict(manifest)
    return dict(event) if isinstance(event, dict) else {}


class ElnPublisher(QObject):
    """Queues finished runs for the notebook and drains the queue off the tick.

    Signals:
        publish_state_changed (dict): ``{"state": str, "pending": int,
            "detail": str}`` — ``synced`` / ``pending`` / ``offline`` /
            ``disabled``, for a GUI status chip. Emitted after every drain and
            every enqueue.
        run_published (dict): ``{"run_id": str, "experiment_id": str,
            "eln_link": {...}}``, emitted once per run when the backend
            confirms its entry.
    """

    publish_state_changed = pyqtSignal(dict)
    run_published = pyqtSignal(dict)

    def __init__(
        self,
        manager: ExperimentManager,
        settings: ElnSettings | None = None,
        adapter: ElnAdapter | None = None,
    ) -> None:
        """Wire the publisher to one experiment manager.

        Args:
            manager: The session-layer façade. Supplies the open experiment,
                the store (for outbox paths and data-file resolution), the
                setup context the body is rendered from, and the single write
                path for the resulting ``ElnLink``.
            settings: The user-level ELN settings. ``None`` loads the
                defaults, which have publishing switched off.
            adapter: The backend to publish through. ``None`` builds one from
                ``settings.backend`` on first use; tests inject a
                ``SimElnAdapter``.
        """
        super().__init__()
        self._manager = manager
        self._settings = settings or ElnSettings()
        self._adapter = adapter
        self._outboxes: dict[str, Outbox] = {}
        self._state = PUBLISH_SYNCED if self._settings.enabled else PUBLISH_DISABLED
        self._timer = QTimer(self)
        self._timer.setInterval(max(1, round(self._settings.drain_interval_s * 1000.0)))
        self._timer.timeout.connect(self.drain_once)
        self._adopt_existing_outboxes()

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    @property
    def settings(self) -> ElnSettings:
        """The settings this publisher was built with."""
        return self._settings

    def pending_count(self) -> int:
        """Return how many jobs are queued across every known experiment."""
        return sum(len(outbox.pending()) for outbox in self._outboxes.values())

    def status(self) -> dict[str, Any]:
        """Return the current publish status, the shape the GUI chip renders.

        Returns:
            ``{"state": str, "pending": int, "detail": str}``.
        """
        return {"state": self._state, "pending": self.pending_count(), "detail": ""}

    # ------------------------------------------------------------------
    # The drain timer (owned here, started by whoever runs an event loop)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the drain timer. No-op when the track is not configured.

        Deliberately explicit rather than automatic in ``__init__``: the timer
        needs a running Qt event loop, so it is the application entry point —
        the GUI side — that decides when publishing goes live.
        """
        if not self._settings.is_configured:
            logger.info("ELN publishing is not configured — the drain timer stays off")
            return
        self._timer.start()
        logger.info(
            "ELN drain timer started (every %.0f s, backend=%s)",
            self._settings.drain_interval_s,
            self._settings.backend,
        )

    def stop(self) -> None:
        """Stop the drain timer (app exit / test teardown)."""
        self._timer.stop()

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def on_run_finished(
        self, event: Any, manifest: dict[str, Any] | None = None, data_path: str = ""
    ) -> str:
        """Queue a finished run for publication. Never raises.

        The auto-publish trigger. Connect it to the Orchestrator's
        ``run_finished`` signal, or call it with a ``RunFinished`` contract
        event; both carry the same manifest.

        Args:
            event: The ``RunFinished`` event, or the manifest dict the
                Orchestrator emits.
            manifest: The run manifest, when the caller has it separately.
                ``None`` takes it from ``event``.
            data_path: Absolute path of the run's data file. ``""`` resolves
                it from the recorded run through the store.

        Returns:
            The queued job's id, or ``""`` when nothing was queued (publishing
            off, auto-publish off, no experiment open, or the job was already
            queued).
        """
        if not self._settings.enabled:
            return ""
        if not self._settings.auto_publish:
            logger.debug("Auto-publish is off — run left for a manual export")
            return ""
        payload = manifest if manifest is not None else _manifest_of(event)
        return self._enqueue_run(str(payload.get("run_id", "")), payload, data_path)

    def export_run(self, run_id: str, data_path: str = "") -> str:
        """Queue one already-recorded run on demand — the manual "Publish now".

        Ignores ``auto_publish`` (the operator asked for this one explicitly)
        but not ``enabled``: with the track switched off there is nowhere to
        send it.

        Args:
            run_id: The run to publish, in the open experiment.
            data_path: Absolute path of the run's data file. ``""`` resolves
                it from the recorded run through the store.

        Returns:
            The queued job's id, or ``""`` when nothing was queued.
        """
        if not self._settings.enabled:
            return ""
        return self._enqueue_run(run_id, None, data_path)

    def export_draft(
        self, run_id: str, draft: DraftEntry | Mapping[str, Any], data_path: str = ""
    ) -> str:
        """Queue one run under an approved **draft entry**'s own text.

        The write half of the drafting track, and deliberately the SAME write
        half everything else uses: a draft becomes an ordinary outbox job
        whose title, body and tags come from the draft instead of from the
        renderers, with the drafting provenance stamped into the entry's
        metadata. It is idempotent by the same ``job_id``, so a run already
        queued is not queued again under a draft.

        Args:
            run_id: The run to publish, in the open experiment.
            draft: The approved draft — a ``DraftEntry``, or its JSON dict as
                the run record stores it (tolerantly loaded).
            data_path: Absolute path of the run's data file. ``""`` resolves
                it from the recorded run through the store.

        Returns:
            The queued job's id, or ``""`` when nothing was queued.
        """
        if not self._settings.enabled:
            return ""
        entry = draft if isinstance(draft, DraftEntry) else DraftEntry.from_dict(draft)
        return self._enqueue_run(run_id, None, data_path, draft=entry)

    def _enqueue_run(
        self,
        run_id: str,
        manifest: dict[str, Any] | None,
        data_path: str,
        draft: DraftEntry | None = None,
    ) -> str:
        """Render one run and append it to its experiment's outbox.

        Args:
            run_id: The run to publish.
            manifest: The run manifest when one is at hand (the auto-publish
                path), else ``None`` — the recorded ``RunRecord`` is then the
                source of truth.
            data_path: Absolute data-file path, or ``""`` to resolve it.
            draft: An approved **draft entry** whose title, body and tags
                replace the rendered ones, or ``None`` for the rendered entry.

        Returns:
            The queued job's id, or ``""``.
        """
        experiment = self._manager.current_experiment()
        if experiment is None:
            logger.debug("Run %r belongs to no experiment — nothing published", run_id)
            return ""
        if not run_id:
            logger.warning("A run with no id cannot be published")
            return ""
        run = experiment.find_run(run_id)
        if run is None and manifest is None:
            logger.warning("No recorded run %r to export", run_id)
            return ""

        facts = dict(manifest or {})
        if run is not None:
            facts = {**self._manifest_from_record(run), **facts}
        resolved = data_path or self._resolve_data_path(experiment.experiment_id, run)
        context = self._manager.experiment_context()
        metadata = render_run_metadata(facts, experiment.experiment_id, resolved)
        if draft is not None:
            # The drafting provenance travels with the entry, so the notebook
            # itself says which model wrote the prose and from which prompt —
            # the same accountability the Agent feed keeps locally.
            metadata = {
                **metadata,
                "draft_model": draft.model,
                "draft_prompt_digest": draft.prompt_digest,
            }
        job = OutboxJob(
            job_id=f"{JOB_PUBLISH_RUN}:{run_id}",
            kind=JOB_PUBLISH_RUN,
            experiment_id=experiment.experiment_id,
            run_id=run_id,
            title=(draft.title if draft is not None and draft.title else None)
            or render_run_title(facts, experiment.title),
            body_html=(
                draft.body_html
                if draft is not None and draft.body_html
                else render_run_body(
                    facts,
                    experiment_id=experiment.experiment_id,
                    experiment_title=experiment.title,
                    setup=context.get("setup"),
                    data_path=resolved,
                    findings=experiment.findings,
                )
            ),
            tags=sorted(
                {*self._settings.tags, *(draft.tags if draft is not None else ())}
            ),
            template_id=self._settings.template_id,
            metadata=metadata,
            data_path=resolved,
            max_attachment_bytes=self._settings.max_attachment_bytes,
        )
        outbox = self._outbox_for(experiment.experiment_id)
        queued = outbox.enqueue(job)
        self._publish_state(DRAIN_IDLE if queued else self._state)
        return job.job_id if queued else ""

    @staticmethod
    def _manifest_from_record(run: RunRecord) -> dict[str, Any]:
        """Return a manifest-shaped dict built from a recorded run.

        Lets a manual export render exactly what auto-publish would, from the
        record alone — the Orchestrator's manifest is long gone by then. One
        owner (``drafting.manifest_from_run()``), so a published run and a
        drafted one describe a run in the same words.

        Args:
            run: The recorded run.

        Returns:
            The manifest-shaped facts.
        """
        return manifest_from_run(run)

    def _resolve_data_path(self, experiment_id: str, run: RunRecord | None) -> str:
        """Return the absolute path of a run's data file, or ``""``.

        Args:
            experiment_id: The owning experiment's store key.
            run: The recorded run, or ``None``.

        Returns:
            The absolute path as a string; ``""`` when the run has no data
            file recorded.
        """
        if run is None or not run.data_file:
            return ""
        return str(self._manager.store.resolve_data_file(experiment_id, run.data_file))

    # ------------------------------------------------------------------
    # Drain
    # ------------------------------------------------------------------

    def drain_once(self) -> DrainResult:
        """Send at most one queued job. Never raises — a GUI timer calls this.

        Returns:
            The ``DrainResult`` of the attempt, or an idle result when there
            is nothing due, no usable settings, or no backend to build.
        """
        if not self._settings.is_configured:
            return DrainResult(state=DRAIN_IDLE)
        adapter = self._resolve_adapter()
        if adapter is None:
            return DrainResult(state=DRAIN_IDLE)
        for experiment_id, outbox in list(self._outboxes.items()):
            result = outbox.drain(adapter)
            if result.state == DRAIN_IDLE:
                continue
            if result.state == DRAIN_PUBLISHED and result.entry is not None:
                self._record_link(experiment_id, result)
            self._publish_state(result.state, result.detail)
            return result
        self._publish_state(DRAIN_IDLE)
        return DrainResult(state=DRAIN_IDLE)

    def _record_link(self, experiment_id: str, result: DrainResult) -> None:
        """Hand a confirmed entry reference to the manager and announce it.

        Args:
            experiment_id: The owning experiment's store key.
            result: The successful drain result.
        """
        if result.entry is None:
            return
        link = ElnLink.from_dict(result.entry.to_dict())
        self._manager.set_run_eln_link(experiment_id, result.run_id, link)
        self.run_published.emit(
            {
                "run_id": result.run_id,
                "experiment_id": experiment_id,
                "eln_link": link.to_dict(),
            }
        )

    def _resolve_adapter(self) -> ElnAdapter | None:
        """Return the backend adapter, building it on first use.

        Returns:
            The adapter, or ``None`` when the configured backend is unknown
            or refuses to construct (logged once per attempt, never raised).
        """
        if self._adapter is not None:
            return self._adapter
        backends = discover_backends()
        adapter_cls = backends.get(self._settings.backend)
        if adapter_cls is None:
            logger.error(
                "Unknown ELN backend %r — available: %s",
                self._settings.backend,
                ", ".join(sorted(backends)) or "(none)",
            )
            return None
        try:
            self._adapter = adapter_cls(self._settings.to_dict(include_secret=True))
        except Exception:
            logger.exception("Could not build the %s ELN adapter", self._settings.backend)
            return None
        return self._adapter

    # ------------------------------------------------------------------
    # Outbox bookkeeping
    # ------------------------------------------------------------------

    def _outbox_for(self, experiment_id: str) -> Outbox:
        """Return (and remember) the outbox of one experiment.

        Args:
            experiment_id: The store key.

        Returns:
            That experiment's ``Outbox``.
        """
        outbox = self._outboxes.get(experiment_id)
        if outbox is None:
            outbox = Outbox(
                self._manager.store.outbox_path(experiment_id),
                retry_base_s=self._settings.retry_base_s,
                retry_max_s=self._settings.retry_max_s,
            )
            self._outboxes[experiment_id] = outbox
        return outbox

    def _adopt_existing_outboxes(self) -> None:
        """Pick up outboxes left on disk by an earlier run of the application.

        What makes the queue survive a restart: a job queued yesterday, on a
        day the notebook was down, drains today without anyone re-opening its
        experiment. Tolerates an unreadable store directory — nothing here may
        stop the application from starting.
        """
        try:
            experiment_ids = self._manager.store.list_experiments()
        except OSError as exc:
            logger.warning("Could not scan for ELN outboxes: %s", exc)
            return
        for experiment_id in experiment_ids:
            if self._manager.store.outbox_path(experiment_id).exists():
                self._outbox_for(experiment_id)
        pending = self.pending_count()
        if pending:
            logger.info("Resumed %d queued ELN job(s) from disk", pending)

    def _publish_state(self, drain_state: str, detail: str = "") -> None:
        """Recompute the publish state and emit it.

        Args:
            drain_state: The last drain's outcome, or ``DRAIN_IDLE``.
            detail: Failure text to carry to the GUI, or ``""``.
        """
        pending = self.pending_count()
        if not self._settings.enabled:
            state = PUBLISH_DISABLED
        elif drain_state == DRAIN_RETRY:
            state = PUBLISH_OFFLINE
        elif pending:
            state = PUBLISH_PENDING
        else:
            state = PUBLISH_SYNCED
        self._state = state
        self.publish_state_changed.emit(
            {"state": state, "pending": pending, "detail": detail}
        )

