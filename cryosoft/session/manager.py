# ---
# description: |
#   SessionManager — the L6 façade and the single writer of experiment state.
#   Owns the experiment lifecycle (start/close, findings, attendance), records
#   every run automatically from the Orchestrator's run_started/run_finished
#   manifests, installs the experiment's session envelope on the Orchestrator,
#   and supplies experiment_context() for stamping HDF5 files. Qt-widget-free:
#   a QObject with signals, no gui imports (contract C11).
# entry_point: Not run directly. Constructed in cryosoft.main after the
#   Orchestrator; injected into the GUI like the ConfigCatalog.
# dependencies:
#   - cryosoft.core.orchestrator (run manifests, set_session_envelope)
#   - cryosoft.core.station (settings snapshots)
#   - cryosoft.session.store / cryosoft.session.models
# input: |
#   Orchestrator signals (run manifests) and GUI lifecycle calls
#   (start_experiment/close_experiment/set_findings/set_attended).
# process: |
#   On construction, resumes the store's active experiment (marking runs left
#   "running" by a crash as failed). Each run_started manifest opens a
#   RunRecord with a station settings snapshot; run_finished completes it.
#   Every mutation saves the record and re-emits it on experiment_changed.
# output: |
#   Persisted experiment records (via ExperimentStore) and the
#   experiment_changed / run_recorded signals the GUI renders.
# ---

"""SessionManager — the L6 façade and single writer of experiment state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.plan import SessionEnvelope
from cryosoft.core.station import Station
from cryosoft.session.models import (
    EXPERIMENT_STATUS_CLOSED,
    EXPERIMENT_STATUS_OPEN,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    ExperimentRecord,
    RunRecord,
    envelope_from_dict,
    envelope_to_dict,
)
from cryosoft.session.store import ExperimentStore, UserRoster

logger = logging.getLogger(__name__)


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class SessionManager(QObject):
    """The session layer's façade — the only object main/GUI (and later the
    Agent Gateway) talk to.

    Single-writer principle, one level up from the Orchestrator: exactly as
    all hardware writes flow through the Orchestrator, all experiment-record
    writes flow through this class. The GUI never edits records itself — it
    calls the lifecycle methods and renders the signals.

    Signals:
        experiment_changed (dict): The current experiment as a JSON-safe dict
            (``ExperimentRecord.to_dict()``), or ``{}`` when none is open.
            Emitted on start/close/resume and on every recorded mutation.
        run_recorded (dict): One ``RunRecord`` as a dict, emitted when a run
            is opened by a ``run_started`` manifest and again when its
            ``run_finished`` manifest completes it.
    """

    experiment_changed = pyqtSignal(dict)
    run_recorded = pyqtSignal(dict)

    def __init__(
        self,
        store: ExperimentStore,
        roster: UserRoster,
        orchestrator: Orchestrator,
        station: Station,
        config_name: str = "",
    ) -> None:
        """Wire into the Orchestrator and resume any active experiment.

        Args:
            store: The experiment store (normally rooted in the data dir).
            roster: The setup-local user roster.
            orchestrator: The active Orchestrator; its run manifests drive run
                recording, and its ``set_session_envelope()`` receives the
                experiment's envelope.
            station: The active Station (settings snapshots at run start).
            config_name: Identity of the active config, recorded on new
                experiments.
        """
        super().__init__()
        self._store = store
        self._roster = roster
        self._orchestrator = orchestrator
        self._station = station
        self._config_name = config_name
        self._experiment: ExperimentRecord | None = None

        orchestrator.run_started.connect(self._on_run_started)
        orchestrator.run_finished.connect(self._on_run_finished)

        self._resume_active_experiment()

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    @property
    def store(self) -> ExperimentStore:
        """The underlying experiment store."""
        return self._store

    @property
    def roster(self) -> UserRoster:
        """The setup-local user roster."""
        return self._roster

    def current_experiment(self) -> ExperimentRecord | None:
        """Return the open experiment record, or ``None``."""
        return self._experiment

    def experiment_context(self) -> dict[str, str]:
        """Return the context dict stamped into every HDF5 file of a run.

        This is what the GUI passes as ``experiment_info`` when constructing a
        procedure, ending up in ``/metadata/experiment_info``.

        Returns:
            ``{experiment_id, experiment_title, user_id, user_name}`` (plus
            ``eln_url`` once the experiment is published), or ``{}`` when no
            experiment is open.
        """
        if self._experiment is None:
            return {}
        user = self._roster.get(self._experiment.user_id)
        context = {
            "experiment_id": self._experiment.experiment_id,
            "experiment_title": self._experiment.title,
            "user_id": self._experiment.user_id,
            "user_name": user.name if user else "",
        }
        if self._experiment.eln_link is not None and self._experiment.eln_link.url:
            context["eln_url"] = self._experiment.eln_link.url
        return context

    # ------------------------------------------------------------------
    # Experiment lifecycle
    # ------------------------------------------------------------------

    def start_experiment(
        self,
        title: str,
        user_id: str,
        sample_info: dict[str, Any],
        envelope: SessionEnvelope | None = None,
        attended: bool = True,
    ) -> ExperimentRecord:
        """Open a new experiment and install its envelope on the Orchestrator.

        Args:
            title: Human title (also slugged into the experiment id).
            user_id: Roster key of the person running the experiment.
            sample_info: The sample fields to snapshot onto the record.
            envelope: Optional per-experiment sample bounds, enforced by the
                Orchestrator for every writer until the experiment closes.
            attended: Initial attendance flag.

        Returns:
            The persisted, now-active ``ExperimentRecord``.

        Raises:
            ValueError: If ``title`` is empty, another experiment is open, or
                ``user_id`` is not in the roster.
            OSError: If the record cannot be written.
        """
        if not title.strip():
            raise ValueError("Experiment title must not be empty")
        if self._experiment is not None:
            raise ValueError(
                f"Experiment {self._experiment.experiment_id!r} is still open; "
                "close it before starting a new one."
            )
        if self._roster.get(user_id) is None:
            raise ValueError(
                f"Unknown user {user_id!r} — add the user to the roster first."
            )
        created = _utc_now_iso()
        record = ExperimentRecord(
            experiment_id=self._store.make_experiment_id(title, created),
            title=title.strip(),
            user_id=user_id,
            sample_info=dict(sample_info),
            config_name=self._config_name,
            created_utc=created,
            status=EXPERIMENT_STATUS_OPEN,
            attended=attended,
            envelope=envelope_to_dict(envelope),
        )
        self._store.save(record)
        self._store.set_active(record.experiment_id)
        self._experiment = record
        self._orchestrator.set_session_envelope(envelope)
        logger.info(
            "Experiment %s started (user=%s, attended=%s)",
            record.experiment_id,
            user_id,
            attended,
        )
        self.experiment_changed.emit(record.to_dict())
        return record

    def close_experiment(self) -> None:
        """Close the open experiment and clear the envelope. No-op when none."""
        if self._experiment is None:
            return
        self._experiment.status = EXPERIMENT_STATUS_CLOSED
        self._experiment.closed_utc = _utc_now_iso()
        self._save_current()
        self._store.set_active(None)
        self._orchestrator.set_session_envelope(None)
        logger.info("Experiment %s closed", self._experiment.experiment_id)
        self._experiment = None
        self.experiment_changed.emit({})

    def set_findings(self, text: str) -> None:
        """Replace the experiment's free-text findings. No-op when none open.

        Args:
            text: The findings text (markdown).
        """
        if self._experiment is None:
            return
        self._experiment.findings = text
        self._save_current()
        self.experiment_changed.emit(self._experiment.to_dict())

    def set_attended(self, attended: bool) -> None:
        """Set the attendance flag. No-op when no experiment is open.

        Attended/unattended governs how much autonomy debug agents get (the
        agent-native plan): recovery actions are reserved for unattended
        sessions. Recorded here so the flag survives a restart.

        Args:
            attended: ``True`` when a human is present at the setup.
        """
        if self._experiment is None or self._experiment.attended == attended:
            return
        self._experiment.attended = attended
        self._save_current()
        logger.info(
            "Experiment %s attendance: %s",
            self._experiment.experiment_id,
            "attended" if attended else "unattended",
        )
        self.experiment_changed.emit(self._experiment.to_dict())

    # ------------------------------------------------------------------
    # Run recording (driven by the Orchestrator's manifests)
    # ------------------------------------------------------------------

    def _on_run_started(self, manifest: dict) -> None:
        """Open a ``RunRecord`` for a ``run_started`` manifest.

        Runs outside an experiment are not recorded — there is no record to
        attach them to (their HDF5 file still exists, unstamped).
        """
        if self._experiment is None:
            return
        settings = {
            vi_name: dict(vi_state)
            for vi_name, vi_state in self._station.cached_state.items()
        }
        run = RunRecord(
            run_id=str(manifest.get("run_id", "")),
            procedure=str(manifest.get("procedure", "")),
            kind=str(manifest.get("kind", "run")),
            params=dict(manifest.get("params") or {}),
            data_file=str(manifest.get("data_file", "")),
            started_utc=str(manifest.get("started_utc", "")),
            status=RUN_STATUS_RUNNING,
            settings_snapshot=settings,
        )
        self._experiment.runs.append(run)
        self._save_current()
        self.run_recorded.emit(run.to_dict())

    def _on_run_finished(self, manifest: dict) -> None:
        """Complete the matching ``RunRecord`` from a ``run_finished`` manifest."""
        if self._experiment is None:
            return
        run = self._experiment.find_run(str(manifest.get("run_id", "")))
        if run is None:
            logger.warning(
                "run_finished for unknown run %r — ignored", manifest.get("run_id")
            )
            return
        run.finished_utc = str(manifest.get("finished_utc", ""))
        run.status = str(manifest.get("status", RUN_STATUS_FAILED))
        run.reason = str(manifest.get("reason", ""))
        self._save_current()
        self.run_recorded.emit(run.to_dict())

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _save_current(self) -> None:
        """Persist the current record, tolerating write failures.

        A failed save must not crash a running measurement — it is logged and
        the in-memory record stays authoritative until the next save attempt.
        """
        if self._experiment is None:
            return
        try:
            self._store.save(self._experiment)
        except OSError:
            logger.exception(
                "Could not save experiment %s", self._experiment.experiment_id
            )

    def _resume_active_experiment(self) -> None:
        """Resume the store's active experiment on construction, if any.

        Runs left in ``running`` state (the app died mid-run) are marked
        failed — a record whose run cannot have survived the restart must not
        look like live work. The envelope stored on the record is re-installed
        on the Orchestrator.
        """
        active_id = self._store.get_active()
        if active_id is None:
            return
        record = self._store.load(active_id)
        if record is None or record.status != EXPERIMENT_STATUS_OPEN:
            logger.warning(
                "Active experiment %r missing or not open — clearing pointer",
                active_id,
            )
            try:
                self._store.set_active(None)
            except OSError:
                logger.exception("Could not clear the active-experiment pointer")
            return
        stale = [run for run in record.runs if run.status == RUN_STATUS_RUNNING]
        for run in stale:
            run.status = RUN_STATUS_FAILED
            run.reason = "application restarted while the run was in progress"
            run.finished_utc = run.finished_utc or _utc_now_iso()
        self._experiment = record
        if stale:
            self._save_current()
        self._orchestrator.set_session_envelope(
            envelope_from_dict(record.envelope)
        )
        logger.info("Resumed experiment %s (%d runs)", record.experiment_id, len(record.runs))
        self.experiment_changed.emit(record.to_dict())
