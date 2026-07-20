# ---
# description: |
#   form_autosave: the GUI's form-autosave model (historically called the
#   "session model" — renamed so "session" is free for the L6 Session
#   Management layer, which manages experiments/runs/users; this module is
#   purely form persistence). A SessionState holds the content a user would
#   otherwise retype on every launch — sample metadata, data directory, the
#   last-selected procedure and its parameters, the run queue with per-item
#   status, and the live-plot panels' chosen X/Y/Loop selections. It
#   serialises to a single JSON file so a closed app is restored on the next
#   open. Deliberately Qt-free (stdlib only) so it
#   unit-tests without a QApplication; the *location* of the file is resolved
#   separately by app_settings.session_file_path().
# entry_point: Not run directly. Constructed and (de)serialised by the GUI windows.
# dependencies: []  # standard library only
# input: |
#   load(path) reads a JSON file previously written by save(). A missing or
#   malformed file yields a default SessionState rather than raising, so a
#   corrupt autosave can never block application startup.
# process: |
#   to_dict()/from_dict() convert between the dataclass tree and plain JSON
#   types, tolerating missing keys (older files) and ignoring unknown ones.
# output: |
#   save(state, path) writes the JSON autosave file atomically. load(path)
#   returns a SessionState.
# ---

"""form_autosave — the CryoSoft GUI's form-autosave model.

The GUI splits persistence into two tiers. Window geometry and dock layout
(machine-specific "chrome") stay in ``QSettings`` (the Windows registry). The
*content* the physicist typed and queued lives here, in a plain JSON file that
is inspectable, portable, and archivable next to the run data.

Naming note: this module was ``gui/session.py`` and its classes keep their
historical names (``SessionState``, the ``last_session.json`` file), so
existing autosave files keep loading unchanged. The word "session" now belongs
to the L6 Session Management layer (``cryosoft.session``), which manages
experiments, runs, and users — a different concept from this form autosave.

``SessionState`` is a ``@dataclass``: a class whose ``__init__`` and field
storage are generated from the annotated attributes below, so we declare the
shape once instead of hand-writing a constructor. ``load``/``save`` never raise
on bad input — a broken autosave file degrades to defaults rather than bricking
startup.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_DEFAULT_DATA_DIR = "C:/CryoData"

# Queue-item lifecycle states (a queued procedure moves pending -> running ->
# done|failed). Exposed as constants so callers never hard-code the strings.
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
_VALID_STATUSES = frozenset({STATUS_PENDING, STATUS_RUNNING, STATUS_DONE, STATUS_FAILED})


def _as_str(value: object, default: str = "") -> str:
    """Coerce a JSON value to ``str``, falling back to ``default`` on ``None``."""
    return default if value is None else str(value)


def _as_str_dict(value: object) -> dict[str, object]:
    """Return ``value`` if it is a dict, else an empty dict (defensive parse)."""
    return dict(value) if isinstance(value, dict) else {}


def _as_str_str_dict(value: object) -> dict[str, str]:
    """Return ``value`` as a ``dict[str, str]``, dropping non-string values."""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, str)}


@dataclass
class QueueItemState:
    """One entry in the persisted run queue.

    Attributes:
        procedure: The procedure's canonical ``name`` (as shown in the selector).
        params: Parameter values as collected from the form (JSON-serialisable).
        sample_info: The ``{sample_name, sample_id, comments}`` captured when the
            item was queued (a queued run keeps the metadata it was created with).
        data_dir: The data directory captured when the item was queued.
        status: One of ``pending``, ``running``, ``done``, ``failed``.
    """

    procedure: str = ""
    params: dict[str, object] = field(default_factory=dict)
    sample_info: dict[str, object] = field(default_factory=dict)
    data_dir: str = _DEFAULT_DATA_DIR
    file_prefix: str = ""
    status: str = STATUS_PENDING

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe dict representation."""
        return {
            "procedure": self.procedure,
            "params": dict(self.params),
            "sample_info": dict(self.sample_info),
            "data_dir": self.data_dir,
            "file_prefix": self.file_prefix,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: object) -> QueueItemState:
        """Build a ``QueueItemState`` from a parsed dict, tolerating bad input.

        Unknown keys are ignored; missing keys take their default. An
        unrecognised ``status`` is reset to ``pending`` so a hand-edited or
        future-version file cannot smuggle in an invalid state.
        """
        if not isinstance(data, dict):
            return cls()
        status = _as_str(data.get("status"), STATUS_PENDING)
        if status not in _VALID_STATUSES:
            status = STATUS_PENDING
        return cls(
            procedure=_as_str(data.get("procedure")),
            params=_as_str_dict(data.get("params")),
            sample_info=_as_str_dict(data.get("sample_info")),
            data_dir=_as_str(data.get("data_dir"), _DEFAULT_DATA_DIR),
            file_prefix=_as_str(data.get("file_prefix")),
            status=status,
        )


@dataclass
class SessionState:
    """The full persisted content of a CryoSoft session.

    Attributes:
        sample_name: Free-text sample name from the Sample Info panel.
        sample_id: Free-text sample ID.
        comments: Free-text comments.
        data_dir: Data directory path string.
        selected_procedure: ``name`` of the procedure last shown in the selector.
        procedure_params: Per-procedure last-used parameter values, keyed by the
            procedure ``name`` (so switching procedures and back restores each
            one's values independently).
        queue: The run queue, oldest first, with per-item status.
        plot_selections: ProcedureWindow's two live-plot panels' chosen X/Y
            axes and Loop 1/Loop 2 selections, keyed by panel id (``"plot1"``,
            ``"plot2"``); each value is ``LivePlotPanel.export_selection()``'s
            ``{"x": ..., "y": ..., "loop1": ..., "loop2": ...}`` dict.
    """

    sample_name: str = ""
    sample_id: str = ""
    comments: str = ""
    data_dir: str = _DEFAULT_DATA_DIR
    selected_procedure: str = ""
    procedure_params: dict[str, dict[str, object]] = field(default_factory=dict)
    queue: list[QueueItemState] = field(default_factory=list)
    plot_selections: dict[str, dict[str, str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe dict, stamped with the current schema version."""
        return {
            "version": _SCHEMA_VERSION,
            "sample_name": self.sample_name,
            "sample_id": self.sample_id,
            "comments": self.comments,
            "data_dir": self.data_dir,
            "selected_procedure": self.selected_procedure,
            "procedure_params": {
                name: dict(values) for name, values in self.procedure_params.items()
            },
            "queue": [item.to_dict() for item in self.queue],
            "plot_selections": {
                panel_id: dict(selection)
                for panel_id, selection in self.plot_selections.items()
            },
        }

    @classmethod
    def from_dict(cls, data: object) -> SessionState:
        """Build a ``SessionState`` from parsed JSON, tolerating bad input.

        Every field falls back to its default when missing or of the wrong
        type, so an older or partially-written file still loads. Unknown keys
        (e.g. from a newer version) are ignored.
        """
        if not isinstance(data, dict):
            return cls()
        raw_params = data.get("procedure_params")
        procedure_params: dict[str, dict[str, object]] = {}
        if isinstance(raw_params, dict):
            for name, values in raw_params.items():
                procedure_params[str(name)] = _as_str_dict(values)
        raw_queue = data.get("queue")
        queue = (
            [QueueItemState.from_dict(item) for item in raw_queue]
            if isinstance(raw_queue, list)
            else []
        )
        raw_plot_selections = data.get("plot_selections")
        plot_selections: dict[str, dict[str, str]] = {}
        if isinstance(raw_plot_selections, dict):
            for panel_id, selection in raw_plot_selections.items():
                plot_selections[str(panel_id)] = _as_str_str_dict(selection)
        return cls(
            sample_name=_as_str(data.get("sample_name")),
            sample_id=_as_str(data.get("sample_id")),
            comments=_as_str(data.get("comments")),
            data_dir=_as_str(data.get("data_dir"), _DEFAULT_DATA_DIR),
            selected_procedure=_as_str(data.get("selected_procedure")),
            procedure_params=procedure_params,
            queue=queue,
            plot_selections=plot_selections,
        )


def load(path: Path) -> SessionState:
    """Load a session from ``path``, returning defaults on any failure.

    Args:
        path: The JSON session file to read.

    Returns:
        The parsed ``SessionState``, or a default ``SessionState`` if the file
        is missing, unreadable, or not valid JSON. This function never raises:
        a corrupt session must not stop the application from opening.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return SessionState()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        logger.warning("session: %s is not valid JSON (%s); using defaults", path, exc)
        return SessionState()
    return SessionState.from_dict(parsed)


def save(state: SessionState, path: Path) -> None:
    """Write ``state`` to ``path`` as JSON, atomically.

    The file is written to a sibling ``.tmp`` path and then ``os.replace``-d
    over the target, so a crash mid-write leaves the previous session intact
    rather than a truncated file. The parent directory is created if needed.

    Args:
        state: The session to persist.
        path: The destination JSON file.

    Raises:
        OSError: If the parent directory cannot be created or the file cannot
            be written. Callers in the GUI wrap this so a failed save on close
            does not crash shutdown.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(state.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp_path, path)
