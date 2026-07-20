# ---
# description: |
#   SessionManager — the L6 façade and the single writer of experiment state.
#   Owns the experiment lifecycle (start/close, findings, attendance), records
#   every run automatically from the Orchestrator's run_started/run_finished
#   manifests, installs the experiment's session envelope on the Orchestrator,
#   and supplies experiment_context() — a two-tier {setup, experiment} dict —
#   for stamping HDF5 files. The Setup tier (config identity + each VI's
#   optional devices.yaml metadata block) is read once at construction via
#   read_instrument_metadata() and is present even with no experiment open.
#   Qt-widget-free: a QObject with signals, no gui imports (contract C11).
# entry_point: Not run directly. Constructed in cryosoft.main after the
#   Orchestrator; injected into the GUI like the ConfigCatalog.
# dependencies:
#   - cryosoft.core.orchestrator (run manifests, set_session_envelope)
#   - cryosoft.core.station (settings snapshots, read_instrument_metadata)
#   - cryosoft.session.store / cryosoft.session.models
# input: |
#   Orchestrator signals (run manifests) and GUI lifecycle calls
#   (start_experiment/close_experiment/set_findings/set_attended/set_queue/
#   switch_experiment).
# process: |
#   On construction, resumes the store's active experiment (marking runs left
#   "running" by a crash as failed). Each run_started manifest opens a
#   RunRecord (data_file relativized against the session folder) with a
#   station settings snapshot; run_finished completes it. Every mutation
#   saves the record and re-emits it on experiment_changed; a save that
#   fails/recovers is surfaced once via store_health_changed. switch_experiment
#   swaps the live experiment without closing the outgoing one.
# output: |
#   Persisted experiment records (via ExperimentStore) and the
#   experiment_changed / run_recorded / store_health_changed signals the GUI
#   renders.
# ---

"""SessionManager — the L6 façade and single writer of experiment state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from cryosoft.core.orchestrator import Orchestrator
from cryosoft.core.plan import SessionEnvelope
from cryosoft.core.station import Station, read_instrument_metadata
from cryosoft.session.models import (
    EXPERIMENT_STATUS_CLOSED,
    EXPERIMENT_STATUS_OPEN,
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    SCHEMA_VERSION,
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
        store_health_changed (dict): ``{"ok": bool, "detail": str}``, emitted
            by ``_save_current()`` the first time a save fails (``ok=False``,
            ``detail`` the ``OSError`` text) and again the first time a save
            succeeds after failures (``ok=True``). One boolean of internal
            state; no retry machinery.
    """

    experiment_changed = pyqtSignal(dict)
    run_recorded = pyqtSignal(dict)
    store_health_changed = pyqtSignal(dict)

    def __init__(
        self,
        store: ExperimentStore,
        roster: UserRoster,
        orchestrator: Orchestrator,
        station: Station,
        config_name: str = "",
        config_path: str | None = None,
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
            config_path: Directory of the active config, read once for each
                VI's optional ``metadata:`` block (``read_instrument_metadata``).
                ``None`` (e.g. in unit tests) just means no instrument
                metadata is stamped — never an error.
        """
        super().__init__()
        self._store = store
        self._roster = roster
        self._orchestrator = orchestrator
        self._station = station
        self._config_name = config_name
        self._instrument_metadata = (
            read_instrument_metadata(config_path) if config_path else {}
        )
        self._experiment: ExperimentRecord | None = None
        self._store_save_ok = True

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

    def experiment_context(self) -> dict[str, Any]:
        """Return the two-tier context dict stamped into every run's metadata.

        This is what the GUI passes as ``experiment_info`` when constructing a
        procedure, ending up whole (as one JSON blob) in
        ``/metadata/experiment_info``. Nests the two tiers this layer knows
        about — Setup (the config/instrument identity, true regardless of
        whether an experiment is open) and Experiment (this named group of
        runs, empty when none is open). The third tier, per-run measurement
        metadata, is stamped separately by the procedure itself.

        Returns:
            ``{"setup": {"config_name": ..., "instruments": {vi_name: {...}}},
            "experiment": {...} or {}}``. The experiment sub-dict, when
            present, has ``experiment_id``, ``experiment_title``, ``user_id``,
            ``user_name``, ``attended``, and ``eln_link`` (``{}`` until
            published).
        """
        setup = {
            "config_name": self._config_name,
            "instruments": dict(self._instrument_metadata),
        }
        if self._experiment is None:
            return {"setup": setup, "experiment": {}}
        user = self._roster.get(self._experiment.user_id)
        experiment = {
            "experiment_id": self._experiment.experiment_id,
            "experiment_title": self._experiment.title,
            "user_id": self._experiment.user_id,
            "user_name": user.name if user else "",
            "attended": self._experiment.attended,
            "eln_link": (
                self._experiment.eln_link.to_dict()
                if self._experiment.eln_link is not None
                else {}
            ),
        }
        return {"setup": setup, "experiment": experiment}

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

    def set_queue(self, items: list[dict[str, Any]]) -> None:
        """Replace the open experiment's run queue. No-op when none is open.

        The queue is GUI-authored, opaque JSON — this layer stores and
        round-trips it but never interprets its shape (the GUI's
        ``QueueItemState`` is the only place that knows it; contract C11
        forbids this package from importing ``cryosoft.gui``).

        Args:
            items: The queue items, each an opaque JSON-safe dict.
        """
        if self._experiment is None:
            return
        self._experiment.queue = items
        self._save_current()

    def switch_experiment(self, experiment_id: str) -> ExperimentRecord:
        """Switch to a different **open** experiment without closing the current one.

        Deactivates the current in-memory experiment by simply ceasing to
        track it — its own record is left exactly as last saved (still
        ``status == "open"`` on disk); ``close_experiment()``'s
        finalize-and-prompt-findings semantics are untouched and remain the
        only way to actually close an experiment. Re-installs the target's
        envelope on the Orchestrator the same way ``start_experiment``/
        ``_resume_active_experiment`` do, and updates the store's active
        pointer.

        Args:
            experiment_id: The store key of an open experiment to switch to.

        Returns:
            The newly active ``ExperimentRecord``.

        Raises:
            ValueError: If ``experiment_id`` is unknown, its record's
                ``status`` is not ``"open"``, or its ``schema_version`` is
                newer than this app's ``SCHEMA_VERSION`` — a future-format
                record must never become the live, mutable experiment of an
                older app.
        """
        record = self._store.load(experiment_id)
        if record is None:
            raise ValueError(f"Unknown experiment {experiment_id!r}")
        if record.status != EXPERIMENT_STATUS_OPEN:
            raise ValueError(
                f"Experiment {experiment_id!r} is not open (status={record.status!r})"
            )
        if record.schema_version > SCHEMA_VERSION:
            raise ValueError(
                f"Experiment {experiment_id!r} was written by a newer app "
                f"(schema_version={record.schema_version} > {SCHEMA_VERSION}); "
                "refusing to switch to it"
            )
        self._experiment = record
        self._store.set_active(record.experiment_id)
        self._orchestrator.set_session_envelope(envelope_from_dict(record.envelope))
        logger.info("Switched to experiment %s", record.experiment_id)
        self.experiment_changed.emit(record.to_dict())
        return record

    def current_data_dir(self) -> Path | None:
        """Return the open experiment's data folder, or ``None`` when none is open."""
        if self._experiment is None:
            return None
        return self._store.data_dir(self._experiment.experiment_id)

    def current_gui_state_path(self) -> Path | None:
        """Return the open experiment's GUI-state file path, or ``None`` when none is open."""
        if self._experiment is None:
            return None
        return self._store.gui_state_path(self._experiment.experiment_id)

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
        raw_data_file = str(manifest.get("data_file", ""))
        data_file = (
            self._store.relativize_data_file(self._experiment.experiment_id, raw_data_file)
            if raw_data_file
            else ""
        )
        run = RunRecord(
            run_id=str(manifest.get("run_id", "")),
            procedure=str(manifest.get("procedure", "")),
            kind=str(manifest.get("kind", "run")),
            params=dict(manifest.get("params") or {}),
            data_file=data_file,
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

        A failed save must not crash a running measurement — it is logged,
        the in-memory record stays authoritative until the next save
        attempt, and ``store_health_changed`` tells the GUI so a stale disk
        copy is never silent (emitted once on the first failure, and once
        again on the first successful save after failures — no retry
        machinery, one boolean of internal state).

        A record whose ``schema_version`` is newer than this app's
        ``SCHEMA_VERSION`` is never written back — belt-and-suspenders for
        the read-only rule; such a record should never have become
        ``self._experiment`` in the first place (see ``switch_experiment``).
        """
        if self._experiment is None:
            return
        if self._experiment.schema_version > SCHEMA_VERSION:
            logger.warning(
                "Refusing to overwrite experiment %s: its schema_version=%d > %d",
                self._experiment.experiment_id,
                self._experiment.schema_version,
                SCHEMA_VERSION,
            )
            return
        try:
            self._store.save(self._experiment)
        except OSError as exc:
            logger.error("Could not save experiment %s: %s", self._experiment.experiment_id, exc)
            if self._store_save_ok:
                self._store_save_ok = False
                self.store_health_changed.emit({"ok": False, "detail": str(exc)})
            return
        if not self._store_save_ok:
            self._store_save_ok = True
            logger.info("Experiment %s save recovered", self._experiment.experiment_id)
            self.store_health_changed.emit({"ok": True, "detail": ""})

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
