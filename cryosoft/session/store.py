# ---
# description: |
#   Persistence for the L6 Session Management layer. ExperimentStore keeps one
#   folder per experiment under <root>/ (normally one session folder under
#   cryosoft.core.paths.measurement_root() / "sessions") with
#   an experiment.json, a gui_state.json, and a data/ folder for the run's
#   HDF5 files (sub-folders allowed), plus an active.json pointer so a restart
#   resumes the open experiment. relativize_data_file()/resolve_data_file()
#   implement the bundle-relative data-path rule (see GLOSSARY.md/README.md)
#   so a session folder copied or moved elsewhere still resolves. SessionStore
#   is the tier above it: one folder per session under
#   <measurement_root>/sessions/ (session.json), plus its own active.json
#   resume pointer one level up from ExperimentStore's — the two active.json
#   files track different things (active session vs. active experiment) and
#   must not be confused. UserRoster keeps the setup-local users.json. All
#   three follow the proven disk discipline of gui/form_autosave.py and the
#   ConfigCatalog: atomic writes (.tmp + os.replace), tolerant loads
#   (corrupt/missing files degrade instead of raising), lazy directory
#   creation (nothing is created until something is actually saved).
# entry_point: Not run directly. Constructed in cryosoft.main, owned by the
#   ExperimentManager.
# dependencies: []  # stdlib + cryosoft.session.models
# input: |
#   load()/list_experiments()/get_active() (and SessionStore's
#   list_sessions()/get_active()) read JSON files previously written by
#   save()/set_active(); missing or malformed files yield None/[]/defaults.
# process: |
#   Records round-trip through models.to_dict()/from_dict(); every write goes
#   to a sibling .tmp path and is os.replace()-d over the target. A loaded
#   record whose schema_version is newer than models.SCHEMA_VERSION logs a
#   WARNING but still loads tolerantly — callers enforce read-only behavior.
# output: |
#   <root>/<experiment_id>/experiment.json, gui_state.json, data/,
#   <root>/active.json, and the roster file passed to UserRoster.
#   <measurement_root>/sessions/<session_id>/session.json and
#   <measurement_root>/sessions/active.json (SessionStore).
# ---

"""Disk persistence for sessions, experiments, and the user roster (L6)."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from cryosoft.session.models import SCHEMA_VERSION, ExperimentRecord, Session, User

logger = logging.getLogger(__name__)

_EXPERIMENT_FILENAME = "experiment.json"
_SESSION_FILENAME = "session.json"
_ACTIVE_FILENAME = "active.json"
_GUI_STATE_FILENAME = "gui_state.json"
_DATA_DIRNAME = "data"


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Mirrors ``session.manager._utc_now_iso()``/``session.servicing_log.
    _utc_now_iso()`` exactly; duplicated rather than imported to avoid a
    circular import (``manager.py`` imports this module).
    """
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: object) -> None:
    """Write ``payload`` as JSON to ``path`` atomically, creating parents.

    Args:
        path: Destination file.
        payload: JSON-serialisable object.

    Raises:
        OSError: If the directory cannot be created or the file written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp_path, path)


def _read_json(path: Path) -> object | None:
    """Read JSON from ``path``, returning ``None`` on any failure (tolerant)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("session store: %s is not valid JSON (%s)", path, exc)
        return None


class ExperimentStore:
    """One-folder-per-experiment store rooted inside the data directory.

    Layout::

        <root>/
            active.json                     {"active": "<experiment_id>", ...}
            <experiment_id>/
                experiment.json
                gui_state.json              # GUI-authored, opaque to this store
                data/                       # HDF5 files; sub-folders allowed
                    <sub-folders>/

    The store creates nothing on construction — directories appear on the
    first ``save()``, so pointing it at a data directory that does not exist
    yet (or is on an unmounted drive) costs nothing until an experiment is
    actually started.
    """

    def __init__(self, root: Path) -> None:
        """Remember the store root without touching the filesystem.

        Args:
            root: Directory holding the experiment folders (normally
                ``<measurement_root>/sessions/<session_id>``, one active
                ``SessionStore`` session's own folder).
        """
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The store's root directory."""
        return self._root

    def make_experiment_id(self, title: str, created_utc: str) -> str:
        """Derive a unique experiment id from the title and creation date.

        ``YYYYMMDD_<slug>`` with a ``_2``, ``_3`` … suffix on collision, so
        ids stay human-readable in the filesystem and unique in the store.

        Args:
            title: The experiment title (any text; slugged).
            created_utc: ISO 8601 creation time (its date part is used).

        Returns:
            A store-unique experiment id.
        """
        date_part = re.sub(r"[^0-9]", "", created_utc[:10]) or "00000000"
        slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_") or "experiment"
        base = f"{date_part}_{slug}"
        candidate = base
        counter = 2
        existing = set(self.list_experiments())
        while candidate in existing:
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def list_experiments(self) -> list[str]:
        """Return every stored experiment id (sorted; [] when none/no root)."""
        if not self._root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in self._root.iterdir()
            if entry.is_dir() and (entry / _EXPERIMENT_FILENAME).is_file()
        )

    def load(self, experiment_id: str) -> ExperimentRecord | None:
        """Load one experiment record, tolerating a corrupt file.

        Args:
            experiment_id: The store key.

        Returns:
            The record, or ``None`` when missing/unreadable/not JSON. The
            record still loads (tolerant-parse) even when its
            ``schema_version`` is newer than this app's ``SCHEMA_VERSION``
            (logged at WARNING) — callers that must not silently re-save a
            future-format record check ``record.schema_version`` themselves
            (see ``ExperimentManager.switch_experiment``/``_save_current``).
        """
        data = _read_json(self._root / experiment_id / _EXPERIMENT_FILENAME)
        if data is None:
            return None
        record = ExperimentRecord.from_dict(data)
        if record.schema_version > SCHEMA_VERSION:
            logger.warning(
                "Experiment %s was written by a newer app (schema_version=%d > %d); "
                "loading read-only",
                experiment_id,
                record.schema_version,
                SCHEMA_VERSION,
            )
        return record

    def data_dir(self, experiment_id: str) -> Path:
        """Return the experiment's data folder (``<root>/<experiment_id>/data``).

        Args:
            experiment_id: The store key.

        Returns:
            The path, which may not exist yet — nothing here creates it; the
            data manager ``mkdir -p``s it lazily when a run actually saves.
        """
        return self._root / experiment_id / _DATA_DIRNAME

    def gui_state_path(self, experiment_id: str) -> Path:
        """Return the experiment's GUI-state file path.

        Args:
            experiment_id: The store key.

        Returns:
            ``<root>/<experiment_id>/gui_state.json`` (may not exist yet).
        """
        return self._root / experiment_id / _GUI_STATE_FILENAME

    def relativize_data_file(self, experiment_id: str, path: str | Path) -> str:
        """Return ``path`` relative to the experiment's session folder, when inside it.

        The write side of the bundle-relative data-path rule: a run saved
        anywhere under ``<root>/<experiment_id>`` (normally inside ``data/``,
        sub-folders included) is stored relative so the whole folder can be
        copied or moved elsewhere and still resolve. A path outside the
        session folder (the physicist deliberately pointed Data Dir
        elsewhere) is stored absolute, unchanged.

        Args:
            experiment_id: The store key.
            path: The run's data file path, normally absolute.

        Returns:
            A POSIX-style bundle-relative string (e.g. ``"data/xyz.h5"`` or
            ``"data/heating_runs/xyz.h5"``) when ``path`` is inside
            ``<root>/<experiment_id>``, else the absolute path string
            unchanged.
        """
        session_folder = (self._root / experiment_id).resolve()
        resolved = Path(path).resolve()
        if resolved.is_relative_to(session_folder):
            return resolved.relative_to(session_folder).as_posix()
        return str(resolved)

    def resolve_data_file(self, experiment_id: str, stored: str) -> Path:
        """Resolve a stored ``data_file`` string back to a real path, tolerantly.

        The read side of the bundle-relative data-path rule. Resolution
        order: a relative stored path joins the session folder; an absolute
        path is used as-is when it still exists; a dangling absolute path
        (an old record whose session folder was moved) falls back to a
        recursive basename search under ``<root>/<experiment_id>/data``; if
        nothing is found there either, the original path is returned
        unchanged.

        Args:
            experiment_id: The store key.
            stored: The ``RunRecord.data_file`` string as read from disk.

        Returns:
            The best-effort real path to the data file.
        """
        candidate = Path(stored)
        if not candidate.is_absolute():
            return self._root / experiment_id / candidate
        if candidate.exists():
            return candidate
        match = next(self.data_dir(experiment_id).rglob(candidate.name), None)
        return match if match is not None else candidate

    def save(self, record: ExperimentRecord) -> None:
        """Persist ``record`` atomically under its ``experiment_id``.

        Args:
            record: The record to write; ``experiment_id`` must be non-empty.

        Raises:
            ValueError: If ``record.experiment_id`` is empty.
            OSError: If the file cannot be written.
        """
        if not record.experiment_id:
            raise ValueError("ExperimentRecord.experiment_id must be set before save()")
        path = self._root / record.experiment_id / _EXPERIMENT_FILENAME
        _write_json_atomic(path, record.to_dict())

    def get_active(self) -> str | None:
        """Return the persisted active experiment id, or ``None``."""
        data = _read_json(self._root / _ACTIVE_FILENAME)
        if isinstance(data, dict) and isinstance(data.get("active"), str):
            return data["active"] or None
        return None

    def set_active(self, experiment_id: str | None) -> None:
        """Persist (or clear) the active experiment pointer.

        Args:
            experiment_id: The id to resume on next start, or ``None`` to
                clear the pointer.

        Raises:
            OSError: If the pointer file cannot be written.
        """
        _write_json_atomic(
            self._root / _ACTIVE_FILENAME,
            {"active": experiment_id or "", "schema_version": SCHEMA_VERSION},
        )


class SessionStore:
    """One-folder-per-session store rooted at ``<measurement_root>/sessions``.

    The tier above ``ExperimentStore``: a session is a named, resumable,
    per-user folder holding multiple experiments (see
    ``docs/plans/session-tier-and-terminology.md``, "Filesystem layout").
    Each session's own ``ExperimentStore`` is rooted one level deeper, at
    ``<root>/<session_id>``.

    Layout::

        <root>/                             <measurement_root>/sessions
            active.json                     {"active": "<session_id>", ...}
            <session_id>/
                session.json
                <experiment_id>/            an ExperimentStore rooted here

    This ``active.json`` tracks the active *session*; it is a distinct file
    from ``ExperimentStore``'s own ``active.json``, which lives one level
    deeper (inside a session folder) and tracks that session's active
    *experiment*. The two must never be confused.

    The store creates nothing on construction — directories appear on the
    first ``save()``, exactly like ``ExperimentStore``.
    """

    def __init__(self, root: Path) -> None:
        """Remember the store root without touching the filesystem.

        Args:
            root: Directory holding the session folders (normally
                ``<measurement_root>/sessions``).
        """
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The store's root directory."""
        return self._root

    def make_session_id(self, name: str, created_utc: str) -> str:
        """Derive a unique session id from the name and creation date.

        ``YYYYMMDD_<slug>`` with a ``_2``, ``_3`` … suffix on collision —
        the same scheme as ``ExperimentStore.make_experiment_id``, checked
        against ``list_sessions()`` instead of ``list_experiments()``.

        Args:
            name: The session's display name (any text; slugged).
            created_utc: ISO 8601 creation time (its date part is used).

        Returns:
            A store-unique session id.
        """
        date_part = re.sub(r"[^0-9]", "", created_utc[:10]) or "00000000"
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "session"
        base = f"{date_part}_{slug}"
        candidate = base
        counter = 2
        existing = set(self.list_sessions())
        while candidate in existing:
            candidate = f"{base}_{counter}"
            counter += 1
        return candidate

    def list_sessions(self, user_id: str | None = None) -> list[str]:
        """Return every stored session id (sorted; [] when none/no root).

        Args:
            user_id: When given, only session ids owned by this roster key
                are returned. Applying this filter loads every session's
                ``session.json`` (there is no index of ``user_id`` outside
                the files themselves), so on a setup with a large session
                roster this is O(n) file reads, not a cheap directory
                listing — the same honest cost tradeoff
                ``ExperimentStore.list_experiments()`` accepts for
                directory-only listing, extended here because the filter
                needs data that isn't in the filename.

        Returns:
            Sorted session ids, optionally filtered by owner.
        """
        if not self._root.is_dir():
            return []
        session_ids = sorted(
            entry.name
            for entry in self._root.iterdir()
            if entry.is_dir() and (entry / _SESSION_FILENAME).is_file()
        )
        if user_id is None:
            return session_ids
        return [
            session_id
            for session_id in session_ids
            if (session := self.load(session_id)) is not None
            and session.user_id == user_id
        ]

    def create_session(self, name: str, user_id: str) -> Session:
        """Create, save, and return a new ``Session`` owned by ``user_id``.

        Args:
            name: The session's display name.
            user_id: Roster key of the owner.

        Returns:
            The newly created, already-saved ``Session``.

        Raises:
            OSError: If the file cannot be written.
        """
        created = _utc_now_iso()
        session = Session(
            session_id=self.make_session_id(name, created),
            user_id=user_id,
            name=name,
            created_utc=created,
            last_opened_utc=created,
        )
        self.save(session)
        return session

    def load(self, session_id: str) -> Session | None:
        """Load one session record, tolerating a corrupt file.

        Args:
            session_id: The store key.

        Returns:
            The record, or ``None`` when missing/unreadable/not JSON. The
            record still loads (tolerant-parse) even when its
            ``schema_version`` is newer than this app's ``SCHEMA_VERSION``
            (logged at WARNING) — same contract as ``ExperimentStore.load``.
        """
        data = _read_json(self._root / session_id / _SESSION_FILENAME)
        if data is None:
            return None
        session = Session.from_dict(data)
        if session.schema_version > SCHEMA_VERSION:
            logger.warning(
                "Session %s was written by a newer app (schema_version=%d > %d); "
                "loading read-only",
                session_id,
                session.schema_version,
                SCHEMA_VERSION,
            )
        return session

    def save(self, session: Session) -> None:
        """Persist ``session`` atomically under its ``session_id``.

        Args:
            session: The record to write; ``session_id`` must be non-empty.

        Raises:
            ValueError: If ``session.session_id`` is empty.
            OSError: If the file cannot be written.
        """
        if not session.session_id:
            raise ValueError("Session.session_id must be set before save()")
        path = self._root / session.session_id / _SESSION_FILENAME
        _write_json_atomic(path, session.to_dict())

    def get_active(self) -> str | None:
        """Return the persisted active session id, or ``None``."""
        data = _read_json(self._root / _ACTIVE_FILENAME)
        if isinstance(data, dict) and isinstance(data.get("active"), str):
            return data["active"] or None
        return None

    def set_active(self, session_id: str | None) -> None:
        """Persist (or clear) the active session pointer.

        Args:
            session_id: The id to resume on next start, or ``None`` to clear
                the pointer.

        Raises:
            OSError: If the pointer file cannot be written.
        """
        _write_json_atomic(
            self._root / _ACTIVE_FILENAME,
            {"active": session_id or "", "schema_version": SCHEMA_VERSION},
        )


class UserRoster:
    """The setup-local user roster, one JSON file.

    Identity, not authentication: users belong to the setup (they live next to
    the app settings, not inside one data directory).
    """

    def __init__(self, path: Path) -> None:
        """Remember the roster file path without touching the filesystem.

        Args:
            path: The ``users.json`` file location.
        """
        self._path = Path(path)

    def list_users(self) -> list[User]:
        """Return every roster user (tolerant: [] on a missing/corrupt file)."""
        data = _read_json(self._path)
        if not isinstance(data, list):
            return []
        users = [User.from_dict(item) for item in data]
        return [user for user in users if user.user_id]

    def get(self, user_id: str) -> User | None:
        """Return the user with ``user_id``, or ``None``.

        Args:
            user_id: The roster key to look up.
        """
        for user in self.list_users():
            if user.user_id == user_id:
                return user
        return None

    def add(self, user: User) -> None:
        """Add ``user`` to the roster (replacing any same-``user_id`` entry).

        Args:
            user: The user to store; ``user_id`` must be non-empty.

        Raises:
            ValueError: If ``user.user_id`` is empty.
            OSError: If the roster file cannot be written.
        """
        if not user.user_id:
            raise ValueError("User.user_id must be set before add()")
        users = [u for u in self.list_users() if u.user_id != user.user_id]
        users.append(user)
        _write_json_atomic(self._path, [u.to_dict() for u in users])
