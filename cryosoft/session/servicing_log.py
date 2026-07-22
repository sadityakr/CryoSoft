# ---
# description: |
#   The Servicing Log framework (L6, plan Phase 1 of
#   docs/plans/cryogenics-logbook.md §3/§6, now unifying per Phase 1 of
#   docs/plans/unified-servicing-log-and-run-recording.md): declared log kinds
#   (LogKindSpec/DECLARED_LOG_KINDS), append-only per-kind storage with an
#   entry-revision model (ServicingLogStore), the machine-recorded helium
#   sample stream (HeliumRecordStore), a pure consumption-rate function, the
#   automatic writer driven by Orchestrator signals (CryogenicsRecorder), and
#   a pure legacy-migration routine (migrate_legacy_servicing_log). One
#   generic engine, N declared kinds: adding a servicing log for another setup
#   is one LogKindSpec, no new store or GUI code. The legacy `cryogenics` +
#   `operations` kinds are superseded by the flat `servicing` kind (one
#   common field table for every entry, no per-kind fields, no `status`
#   field — see the plan §2) but both legacy kinds stay declared and
#   fully functional in this phase: nothing reads/writes `servicing.jsonl`
#   automatically yet, and the recorder still writes the legacy kinds. The
#   migration from legacy files to `servicing.jsonl` is a pure function plus a
#   thin store method, callable explicitly (a future startup hook or script);
#   it is not invoked from anywhere in this phase.
# entry_point: Not run directly. Stores are constructed in cryosoft.main
#   (Phase 3+) beside the SessionManager; CryogenicsRecorder is connected to
#   Orchestrator signals there. In Phase 1 all classes/functions are exercised
#   directly by tests against a mocked Orchestrator / synthetic legacy files.
# dependencies:
#   - cryosoft.core.plan (ParamSpec)
#   - cryosoft.session.models (ServiceLogEntry)
#   - PyQt6.QtCore (QObject, pyqtSignal)
# input: |
#   ServicingLogStore/HeliumRecordStore: plain values passed by callers (the
#   GUI's add/edit dialogs, CryogenicsRecorder). CryogenicsRecorder: the
#   Orchestrator's states_updated / run_started / run_finished payloads
#   (run_finished's "summary" key, when present, carries the fill's level
#   curve). migrate_legacy_servicing_log: the on-disk cryogenics.jsonl /
#   operations.jsonl files of one config's servicing-log root.
# process: |
#   LogKindSpec validates eagerly at construction (ValueError naming the
#   offender), mirroring core/plan.py. ServicingLogStore coerces every write
#   against the kind's ParamSpec fields and appends one JSON line per
#   revision; reads are tolerant (a corrupt line is skipped with a WARNING
#   log, never raised). HeliumRecordStore appends one (utc, helium_pct,
#   nitrogen_pct) sample per call and rotates (keeps the newest half) once the
#   file exceeds ~2 MB — the machine record may rotate; servicing logs never
#   do. consumption_rate_pct_per_h() is a pure least-squares fit, no I/O.
#   CryogenicsRecorder never raises out of a slot (broad try/except + log),
#   exactly like SessionManager's manifest handlers; extracting the level
#   curve from a run_finished manifest (_extract_level_curve_json) is
#   likewise fully tolerant of a missing/malformed "summary" key.
#   migrate_legacy_servicing_log reads the two legacy kinds through the
#   existing tolerant reader, matches a fill's operations line to its
#   cryogenics entry by an exact (started_utc, finished_utc) == (start_utc,
#   end_utc) match, writes one merged/derived "servicing" entry per pair/
#   unmatched entry (preserving the original created_utc so the merged
#   timeline stays chronological), converts an embedded level_curve JSON
#   string into a recordings/<id>.json sidecar, and renames the legacy files
#   to .bak. No-op (returns False) if servicing.jsonl already exists or
#   neither legacy file is present — safe to call unconditionally.
# output: |
#   Append-only JSONL files under <root>/<config_name>/{<kind>.jsonl,
#   helium_record.jsonl}; the cryo_warning(str) Qt signal for GUI banners;
#   migrate_legacy_servicing_log additionally writes
#   <root>/<config_name>/recordings/<id>.json sidecars and renames the legacy
#   files to <name>.jsonl.bak.
# ---

"""The Servicing Log framework (L6): declared log kinds, revisioned storage,
the helium record, the automatic recorder, and legacy-log migration.

See ``docs/plans/cryogenics-logbook.md`` §3 and §6, and
``docs/plans/unified-servicing-log-and-run-recording.md`` §2 (Phase 1) for
the design this implements, and ``GLOSSARY.md`` for the **Servicing log** /
**Log kind** / **Cryogenics log** / **Entry revision** / **Helium record** /
**Recording** definitions.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal

from cryosoft.core.plan import ParamSpec
from cryosoft.session.models import ServiceLogEntry

logger = logging.getLogger(__name__)

__all__ = [
    "LogKindSpec",
    "DECLARED_LOG_KINDS",
    "ServicingLogStore",
    "HeliumRecordStore",
    "consumption_rate_pct_per_h",
    "CryogenicsRecorder",
    "migrate_legacy_servicing_log",
]

# Machine record rotation threshold (module-level so tests can shrink it).
_ROTATION_BYTES = 2 * 1024 * 1024  # ~2 MB


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON line to ``path``, creating parent directories.

    Args:
        path: The JSONL file to append to.
        payload: JSON-serialisable object for the new line.

    Raises:
        OSError: If the directory cannot be created or the file written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _read_lines_tolerant(path: Path) -> list[dict[str, Any]]:
    """Read every JSON line in ``path``, skipping corrupt ones with a warning.

    Args:
        path: The JSONL file to read.

    Returns:
        One dict per well-formed line, in file order. ``[]`` if the file is
        missing.
    """
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            data = json.loads(raw_line)
        except (TypeError, ValueError) as exc:
            logger.warning("%s: skipping corrupt line %d (%s)", path, lineno, exc)
            continue
        if not isinstance(data, dict):
            logger.warning("%s: skipping non-object line %d", path, lineno)
            continue
        records.append(data)
    return records


# ── Log kinds: declarations ─────────────────────────────────────────────────


@dataclass(frozen=True)
class LogKindSpec:
    """One declared servicing-log table (GLOSSARY.md: **Log kind**).

    A log kind is a key, a title, and an ordered field schema reusing
    ``ParamSpec`` — the same currency the GUI already renders for procedure
    parameters. Everything downstream (storage, revision handling, table
    view, add/edit dialogs) is generic; adding a log kind for a new setup is
    one ``LogKindSpec`` plus one config line, never new store or GUI code.
    Validates eagerly at construction, mirroring ``core/plan.py``.

    Attributes:
        key: Stable identifier, e.g. ``"cryogenics"``. Non-empty, a valid
            Python identifier, and lowercase (used verbatim in file paths).
        title: Human-readable table heading. Non-empty string.
        fields: Ordered, non-empty mapping of field name to ``ParamSpec``.
            Every ``ParamSpec`` already requires a type-matching ``default``
            at its own construction, so every declared field has a usable
            default automatically. Defensively copied.
        editable: Whether entries of this kind may be added/revised/deleted
            through ``ServicingLogStore.add_entry`` et al. ``False`` marks a
            machine-only stream (e.g. ``"operations"``), writable only via
            ``ServicingLogStore.append_machine_entry``.
    """

    key: str
    title: str
    fields: dict[str, ParamSpec]
    editable: bool = True

    def __post_init__(self) -> None:
        """Validate the declaration and defensively copy ``fields``.

        Raises:
            TypeError: If ``key``/``title`` is not a str, ``fields`` is not a
                dict, a fields key is not a str, a fields value is not a
                ``ParamSpec``, or ``editable`` is not a bool.
            ValueError: If ``key`` is empty, not a valid identifier, or not
                lowercase; if ``title`` is empty; or if ``fields`` is empty or
                a key is empty.
        """
        if not isinstance(self.key, str):
            raise TypeError(f"LogKindSpec.key must be a str, got {self.key!r}")
        if not self.key or not self.key.isidentifier() or self.key != self.key.lower():
            raise ValueError(
                f"LogKindSpec.key must be a non-empty lowercase identifier, "
                f"got {self.key!r}"
            )

        if not isinstance(self.title, str):
            raise TypeError(f"LogKindSpec.title must be a str, got {self.title!r}")
        if not self.title:
            raise ValueError("LogKindSpec.title must be a non-empty str")

        if not isinstance(self.fields, dict):
            raise TypeError(f"LogKindSpec.fields must be a dict, got {self.fields!r}")
        if not self.fields:
            raise ValueError(f"LogKindSpec({self.key!r}).fields must be a non-empty dict")
        for name, spec in self.fields.items():
            if not isinstance(name, str) or not name:
                raise ValueError(
                    f"LogKindSpec({self.key!r}).fields key must be a non-empty str, "
                    f"got {name!r}"
                )
            if not isinstance(spec, ParamSpec):
                raise TypeError(
                    f"LogKindSpec({self.key!r}).fields[{name!r}] must be a ParamSpec, "
                    f"got {spec!r}"
                )
        object.__setattr__(self, "fields", dict(self.fields))

        if not isinstance(self.editable, bool):
            raise TypeError(f"LogKindSpec.editable must be a bool, got {self.editable!r}")


# The first (and, so far, only) editable log kind: one entry per cryogen
# fill (plan §6.1). Written automatically by the fill operation (Phase 3),
# addable/editable by hand (fills done manually, LN2 top-ups, corrections).
_CRYOGENICS_KIND = LogKindSpec(
    key="cryogenics",
    title="Cryogenics log",
    fields={
        "person": ParamSpec(
            type=str, default="", description="Who performed the fill"
        ),
        "start_utc": ParamSpec(
            type=str,
            default="",
            widget_hint="datetime",
            description="Fill start time (UTC, ISO 8601)",
        ),
        "end_utc": ParamSpec(
            type=str,
            default="",
            widget_hint="datetime",
            description="Fill end time (UTC, ISO 8601)",
        ),
        "helium_start_pct": ParamSpec(
            type=float, default=0.0, unit="%", description="Helium level at fill start"
        ),
        "helium_end_pct": ParamSpec(
            type=float, default=0.0, unit="%", description="Helium level at fill end"
        ),
        "ln2_filled": ParamSpec(
            type=bool, default=False, description="Whether LN2 was topped up too"
        ),
        "notes": ParamSpec(
            type=str, default="", description="Free-text notes / corrections"
        ),
        # Machine-populated only (docs/plans/operation-concurrency-and-error-
        # scoping.md §4): HeliumFillOperation's bounded in-memory level curve,
        # JSON-encoded ({"unix_time": [...], "helium_pct": [...]}), handed
        # off via run_summary() and written by CryogenicsRecorder alongside
        # this entry. "" for a manual entry (no run behind it) and for any
        # entry written before this field existed — ServicingLogStore never
        # rewrites old lines, so those simply lack the key entirely; readers
        # must use .get("level_curve", "") rather than indexing it.
        "level_curve": ParamSpec(
            type=str,
            default="",
            description="Level-vs-time curve sampled during the fill (JSON, machine-written)",
        ),
    },
    editable=True,
)

# The non-editable audit trail every operation appends to on finish (plan
# §6.3): "who warmed the VTI last night?". Machine source only.
_OPERATIONS_KIND = LogKindSpec(
    key="operations",
    title="Operations",
    fields={
        "operation": ParamSpec(type=str, default="", description="Operation name"),
        "params": ParamSpec(
            type=str, default="{}", description="Operation parameters (compact JSON)"
        ),
        "started_utc": ParamSpec(
            type=str,
            default="",
            widget_hint="datetime",
            description="Start time (UTC, ISO 8601)",
        ),
        "finished_utc": ParamSpec(
            type=str,
            default="",
            widget_hint="datetime",
            description="Finish time (UTC, ISO 8601)",
        ),
        "status": ParamSpec(
            type=str, default="", description="Terminal status (done/failed/aborted)"
        ),
        "verified": ParamSpec(
            type=bool, default=False, description="Whether postconditions were verified"
        ),
        "reason": ParamSpec(
            type=str, default="", description="Failure/abort reason, if any"
        ),
    },
    editable=False,
)

# The unified log kind (plan docs/plans/unified-servicing-log-and-run-
# recording.md §2): ONE flat common table for every servicing event —
# helium fills, sample changes, future operations, and hand-added entries
# all share exactly these fields, no kind-specific columns and no `status`
# field. Supersedes the split between `cryogenics` and `operations`, though
# both stay declared and functional in this phase (see the module docstring).
# `entry_kind` names what happened ("helium_fill" | "sample_change" | a
# future operation's key | "manual") — deliberately a plain str, not a
# `choices` enum, because future operation keys are open-ended and cannot be
# enumerated here. `origin` is a closed two-value set and does use `choices`.
_SERVICING_KIND = LogKindSpec(
    key="servicing",
    title="Servicing log",
    fields={
        "entry_kind": ParamSpec(
            type=str,
            default="manual",
            description=(
                'What happened: "helium_fill", "sample_change", another '
                'operation\'s key, or "manual" for a hand-added entry'
            ),
        ),
        "person": ParamSpec(
            type=str, default="", description="Who performed it"
        ),
        "start_utc": ParamSpec(
            type=str,
            default="",
            widget_hint="datetime",
            description="Servicing start time (UTC, ISO 8601)",
        ),
        "end_utc": ParamSpec(
            type=str,
            default="",
            widget_hint="datetime",
            description="Servicing end time (UTC, ISO 8601)",
        ),
        "helium_start_pct": ParamSpec(
            type=float, default=0.0, unit="%", description="Helium level at start"
        ),
        "helium_end_pct": ParamSpec(
            type=float, default=0.0, unit="%", description="Helium level at end"
        ),
        "ln2_start_pct": ParamSpec(
            type=float, default=0.0, unit="%", description="LN2 level at start"
        ),
        "ln2_end_pct": ParamSpec(
            type=float, default=0.0, unit="%", description="LN2 level at end"
        ),
        "notes": ParamSpec(
            type=str,
            default="",
            description="Free-text notes / corrections; the recorder appends "
            "machine remarks here (there is no status field)",
        ),
        "recording": ParamSpec(
            type=str,
            default="",
            description='Sidecar filename under "recordings/", "" if none',
        ),
        "origin": ParamSpec(
            type=str,
            default="manual",
            choices={"Machine": "machine", "Manual": "manual"},
            description="Who/what wrote this entry",
        ),
    },
    editable=True,
)

#: Registry of every declared log kind. Adding a kind for a new setup is one
#: entry here (plus, later, a config reference) — no other code changes.
DECLARED_LOG_KINDS: dict[str, LogKindSpec] = {
    _CRYOGENICS_KIND.key: _CRYOGENICS_KIND,
    _OPERATIONS_KIND.key: _OPERATIONS_KIND,
    _SERVICING_KIND.key: _SERVICING_KIND,
}


def _coerce_field(kind: str, name: str, value: Any, spec: ParamSpec) -> Any:
    """Coerce one value against its field's ``ParamSpec``, or raise.

    Mirrors ``ParamSpec._matches_type``'s numeric nuance (an ``int`` is
    accepted where ``float`` is declared; ``bool`` never satisfies a numeric
    or ``str`` type) but additionally *coerces* an accepted ``int`` to
    ``float`` so stored values match the declared type exactly. A field
    declaring ``choices`` (e.g. the ``servicing`` kind's ``origin``) further
    restricts the type-matched value to one of ``spec.choices.values()``.

    Args:
        kind: The owning log kind's key, for error messages.
        name: The field name, for error messages.
        value: The candidate value.
        spec: The field's ``ParamSpec``.

    Returns:
        ``value`` coerced to ``spec.type``.

    Raises:
        ValueError: If ``value`` is not a legal instance of ``spec.type``, or
            the field declares ``choices`` and ``value`` is none of them.
    """
    if spec.type is bool:
        coerced: Any = value
        if not isinstance(value, bool):
            raise ValueError(f"{kind}.{name} must be a bool, got {value!r}")
    elif isinstance(value, bool):
        raise ValueError(f"{kind}.{name} must be a {spec.type.__name__}, got bool {value!r}")
    elif spec.type is float:
        if isinstance(value, (int, float)):
            coerced = float(value)
        else:
            raise ValueError(f"{kind}.{name} must be a real number, got {value!r}")
    elif spec.type is int:
        if isinstance(value, int):
            coerced = value
        else:
            raise ValueError(f"{kind}.{name} must be an int, got {value!r}")
    elif spec.type is str:
        if isinstance(value, str):
            coerced = value
        else:
            raise ValueError(f"{kind}.{name} must be a str, got {value!r}")
    else:
        raise ValueError(f"{kind}.{name} has unsupported field type {spec.type!r}")  # pragma: no cover

    if spec.choices is not None and coerced not in spec.choices.values():
        raise ValueError(
            f"{kind}.{name} must be one of {sorted(spec.choices.values())}, got {coerced!r}"
        )
    return coerced


def _coerce_values(spec: LogKindSpec, values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and coerce a partial values mapping against a log kind's fields.

    Missing fields take the field's ``ParamSpec.default``; unknown keys are
    rejected (never silently dropped, so a typo in a GUI dialog surfaces
    immediately instead of vanishing).

    Args:
        spec: The log kind's declaration.
        values: The candidate values (may be a partial subset of the fields).

    Returns:
        A full ``{field_name: coerced_value}`` dict covering every declared
        field.

    Raises:
        TypeError: If ``values`` is not a mapping.
        ValueError: If ``values`` names a field the kind does not declare, or
            a value cannot be coerced to its field's type.
    """
    if not isinstance(values, dict):
        raise TypeError(f"values for log kind {spec.key!r} must be a dict, got {values!r}")
    unknown = sorted(set(values) - set(spec.fields))
    if unknown:
        raise ValueError(f"log kind {spec.key!r} has no field(s) {unknown}")
    return {
        name: _coerce_field(spec.key, name, values[name], field_spec)
        if name in values
        else field_spec.default
        for name, field_spec in spec.fields.items()
    }


# ── ServicingLogStore: editable for humans, append-only on disk ────────────


class ServicingLogStore:
    """Per-setup, per-kind append-only storage with the entry-revision model.

    One JSONL file per kind per setup: ``<root>/<config_name>/<kind>.jsonl``.
    Editability comes from the entry-revision model (GLOSSARY.md: **Entry
    revision**) — ``add_entry``/``revise_entry``/``delete_entry`` all *append*
    a new ``ServiceLogEntry`` sharing the earlier one's ``entry_id``; nothing
    already on disk is ever rewritten. Readers (``entries``) see the latest,
    non-deleted revision per ``entry_id``; ``revisions`` exposes the full
    history. Writes are validated/coerced against the kind's ``ParamSpec``
    fields; reads tolerate a corrupt line (skipped with a WARNING, never
    raised) exactly like ``session/store.py``.
    """

    def __init__(self, root: Path | str, config_name: str) -> None:
        """Remember the store root without touching the filesystem.

        Args:
            root: Directory holding one subfolder per config (normally
                ``<data_dir>/servicing``).
            config_name: Identity of the active config; entries of different
                configs never share a file.
        """
        self._root = Path(root)
        self._config_name = config_name

    def _path(self, kind: str) -> Path:
        """Return the JSONL path for ``kind`` (does not check it exists)."""
        return self._root / self._config_name / f"{kind}.jsonl"

    def _spec(self, kind: str) -> LogKindSpec:
        """Return the declared spec for ``kind``.

        Raises:
            ValueError: If ``kind`` is not in ``DECLARED_LOG_KINDS``.
        """
        spec = DECLARED_LOG_KINDS.get(kind)
        if spec is None:
            raise ValueError(
                f"unknown log kind {kind!r}; declared kinds are "
                f"{sorted(DECLARED_LOG_KINDS)}"
            )
        return spec

    def add_entry(
        self,
        kind: str,
        values: dict[str, Any],
        *,
        source: str = "manual",
        person: str = "",
        run_id: str = "",
    ) -> ServiceLogEntry:
        """Append a new entry (revision 1) to an editable log kind.

        Args:
            kind: The declared log kind's key.
            values: Field values (a subset is fine; missing fields take their
                declared default).
            source: Provenance — ``"manual"`` (default, a technician) or
                ``"operation"`` (an operation's recorder).
            person: Convenience provenance value. If non-empty and the kind
                declares a ``"person"`` field that ``values`` does not already
                set, it is folded into the stored values under that key —
                lets a caller (e.g. ``CryogenicsRecorder``) pass the operator
                without duplicating the field name.
            run_id: Linked run id when ``source == "operation"``.

        Returns:
            The new ``ServiceLogEntry`` (also appended to disk).

        Raises:
            ValueError: If ``kind`` is undeclared, not editable, names an
                undeclared field, or a value cannot be coerced.
        """
        spec = self._spec(kind)
        if not spec.editable:
            raise ValueError(
                f"log kind {kind!r} is not editable; use append_machine_entry()"
            )
        merged = dict(values)
        if person and "person" in spec.fields and "person" not in merged:
            merged["person"] = person
        coerced = _coerce_values(spec, merged)
        entry = ServiceLogEntry(
            entry_id=uuid.uuid4().hex,
            kind=kind,
            values=coerced,
            source=source,
            run_id=run_id,
            created_utc=_utc_now_iso(),
            revision=1,
        )
        _append_line(self._path(kind), entry.to_dict())
        return entry

    def revise_entry(
        self, kind: str, entry_id: str, values: dict[str, Any], *, revised_by: str
    ) -> ServiceLogEntry:
        """Append a new revision of ``entry_id`` with ``values`` merged in.

        Fields not named in ``values`` keep the previous revision's value
        (partial edits — e.g. correcting only ``notes`` — do not need to
        restate the whole entry). ``source``/``run_id``/``created_utc`` carry
        forward from the entry's history unchanged.

        Args:
            kind: The declared log kind's key.
            entry_id: The entry to revise.
            values: The fields to change.
            revised_by: Who made this revision.

        Returns:
            The new ``ServiceLogEntry``.

        Raises:
            ValueError: If ``kind`` is undeclared, not editable, ``entry_id``
                has no history, an unknown field is named, or a value cannot
                be coerced.
        """
        spec = self._spec(kind)
        if not spec.editable:
            raise ValueError(f"log kind {kind!r} is not editable")
        history = self.revisions(kind, entry_id)
        if not history:
            raise ValueError(f"no entry {entry_id!r} in log kind {kind!r}")
        latest = history[-1]
        merged = {**latest.values, **values}
        coerced = _coerce_values(spec, merged)
        entry = ServiceLogEntry(
            entry_id=entry_id,
            kind=kind,
            values=coerced,
            source=latest.source,
            run_id=latest.run_id,
            created_utc=latest.created_utc,
            revised_utc=_utc_now_iso(),
            revised_by=revised_by,
            revision=latest.revision + 1,
            deleted=False,
        )
        _append_line(self._path(kind), entry.to_dict())
        return entry

    def delete_entry(self, kind: str, entry_id: str, *, revised_by: str) -> ServiceLogEntry:
        """Append a tombstone revision of ``entry_id`` (never removes history).

        Args:
            kind: The declared log kind's key.
            entry_id: The entry to delete.
            revised_by: Who deleted it.

        Returns:
            The new tombstone ``ServiceLogEntry`` (``deleted=True``).

        Raises:
            ValueError: If ``kind`` is undeclared, not editable, or
                ``entry_id`` has no history.
        """
        spec = self._spec(kind)
        if not spec.editable:
            raise ValueError(f"log kind {kind!r} is not editable")
        history = self.revisions(kind, entry_id)
        if not history:
            raise ValueError(f"no entry {entry_id!r} in log kind {kind!r}")
        latest = history[-1]
        entry = ServiceLogEntry(
            entry_id=entry_id,
            kind=kind,
            values=dict(latest.values),
            source=latest.source,
            run_id=latest.run_id,
            created_utc=latest.created_utc,
            revised_utc=_utc_now_iso(),
            revised_by=revised_by,
            revision=latest.revision + 1,
            deleted=True,
        )
        _append_line(self._path(kind), entry.to_dict())
        return entry

    def append_machine_entry(self, kind: str, values: dict[str, Any]) -> ServiceLogEntry:
        """Append a one-shot, unrevisable entry (``source="machine"``).

        The only write path for a non-editable kind (e.g. ``"operations"``);
        also usable for an editable kind when a caller explicitly wants a
        machine-attributed, never-revised record.

        Args:
            kind: The declared log kind's key.
            values: Field values (a subset is fine; missing fields take their
                declared default).

        Returns:
            The new ``ServiceLogEntry`` (revision 1, ``source="machine"``).

        Raises:
            ValueError: If ``kind`` is undeclared, names an undeclared field,
                or a value cannot be coerced.
        """
        spec = self._spec(kind)
        coerced = _coerce_values(spec, values)
        entry = ServiceLogEntry(
            entry_id=uuid.uuid4().hex,
            kind=kind,
            values=coerced,
            source="machine",
            run_id="",
            created_utc=_utc_now_iso(),
            revision=1,
        )
        _append_line(self._path(kind), entry.to_dict())
        return entry

    def entries(self, kind: str) -> list[ServiceLogEntry]:
        """Return the latest, non-deleted revision of every entry, newest first.

        Args:
            kind: The declared log kind's key.

        Returns:
            One ``ServiceLogEntry`` per live ``entry_id``, sorted by
            ``created_utc`` descending (newest first). Corrupt lines are
            skipped with a WARNING log, never raised.

        Raises:
            ValueError: If ``kind`` is undeclared.
        """
        self._spec(kind)
        latest_by_id: dict[str, ServiceLogEntry] = {}
        for data in _read_lines_tolerant(self._path(kind)):
            entry = ServiceLogEntry.from_dict(data)
            current = latest_by_id.get(entry.entry_id)
            if current is None or entry.revision >= current.revision:
                latest_by_id[entry.entry_id] = entry
        visible = [entry for entry in latest_by_id.values() if not entry.deleted]
        visible.sort(key=lambda entry: entry.created_utc, reverse=True)
        return visible

    def revisions(self, kind: str, entry_id: str) -> list[ServiceLogEntry]:
        """Return the full revision history of one entry, oldest first.

        Args:
            kind: The declared log kind's key.
            entry_id: The entry to look up.

        Returns:
            Every revision (including tombstones) sorted by ``revision``
            ascending. ``[]`` if the entry has never been written.

        Raises:
            ValueError: If ``kind`` is undeclared.
        """
        self._spec(kind)
        history = [
            ServiceLogEntry.from_dict(data)
            for data in _read_lines_tolerant(self._path(kind))
        ]
        history = [entry for entry in history if entry.entry_id == entry_id]
        history.sort(key=lambda entry: entry.revision)
        return history

    def migrate_legacy(self, *, level_vi_name: str = "level_meter") -> bool:
        """Merge this store's legacy ``cryogenics``/``operations`` logs into ``servicing``.

        Thin hook onto the pure, idempotent ``migrate_legacy_servicing_log()``
        (see its docstring for the full merge/matching rules). Not called
        anywhere automatically in this phase.

        Args:
            level_vi_name: VI name used to build the sidecar channel key for
                a migrated level curve (``"<level_vi_name>.helium_pct"``).

        Returns:
            ``True`` if migration ran and wrote ``servicing.jsonl``; ``False``
            on a no-op (already migrated, or no legacy files present).
        """
        return migrate_legacy_servicing_log(
            self._root, self._config_name, level_vi_name=level_vi_name
        )


# ── HeliumRecordStore: the machine sample stream ────────────────────────────


class HeliumRecordStore:
    """The machine-recorded helium/nitrogen sample stream (GLOSSARY.md:
    **Helium record**).

    One ``(utc, helium_pct, nitrogen_pct)`` sample per call to ``append()``,
    written to ``<root>/<config_name>/helium_record.jsonl``. Unlike servicing
    logs, this file is a machine record with no editability requirement, so
    it may rotate: once it exceeds ``_ROTATION_BYTES`` (~2 MB), the oldest
    half of its lines is dropped in one atomic rewrite.
    """

    def __init__(self, root: Path | str, config_name: str) -> None:
        """Remember the store root without touching the filesystem.

        Args:
            root: Directory holding one subfolder per config (normally
                ``<data_dir>/servicing``, sibling to the servicing logs).
            config_name: Identity of the active config.
        """
        self._root = Path(root)
        self._config_name = config_name

    @property
    def path(self) -> Path:
        """The JSONL file this store reads/writes."""
        return self._root / self._config_name / "helium_record.jsonl"

    def append(self, utc_iso: str, helium_pct: float, nitrogen_pct: float) -> None:
        """Append one sample and rotate the file if it has grown too large.

        Args:
            utc_iso: Sample time as an ISO 8601 string (UTC).
            helium_pct: Helium level in percent.
            nitrogen_pct: Nitrogen level in percent.

        Raises:
            TypeError: If ``utc_iso`` is not a non-empty str, or a level is
                not a real number (``bool`` rejected).
            OSError: If the file cannot be written.
        """
        if not isinstance(utc_iso, str) or not utc_iso:
            raise TypeError(f"utc_iso must be a non-empty str, got {utc_iso!r}")
        for label, value in (("helium_pct", helium_pct), ("nitrogen_pct", nitrogen_pct)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{label} must be a real number, got {value!r}")
        _append_line(
            self.path,
            {
                "utc": utc_iso,
                "helium_pct": float(helium_pct),
                "nitrogen_pct": float(nitrogen_pct),
            },
        )
        self._rotate_if_large()

    def samples(self, since_utc: str | None = None) -> list[tuple[float, float, float]]:
        """Return every sample as ``(unix_time, helium_pct, nitrogen_pct)``.

        Args:
            since_utc: Optional ISO 8601 lower bound (inclusive); samples with
                an earlier ``utc`` string are excluded. Comparison is a plain
                string comparison, valid because every sample is written with
                the same UTC ISO 8601 format.

        Returns:
            Samples sorted by time ascending. Corrupt lines are skipped with
            a WARNING log, never raised. ``[]`` if the file is missing.
        """
        result: list[tuple[float, float, float]] = []
        for data in _read_lines_tolerant(self.path):
            try:
                utc = str(data["utc"])
                helium = float(data["helium_pct"])
                nitrogen = float(data["nitrogen_pct"])
                unix_time = datetime.fromisoformat(utc).timestamp()
            except (TypeError, ValueError, KeyError) as exc:
                logger.warning("%s: skipping malformed sample (%s)", self.path, exc)
                continue
            if since_utc is not None and utc < since_utc:
                continue
            result.append((unix_time, helium, nitrogen))
        result.sort(key=lambda sample: sample[0])
        return result

    def _rotate_if_large(self) -> None:
        """Atomically keep only the newest half of lines once over the threshold."""
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size <= _ROTATION_BYTES:
            return
        lines = self.path.read_text(encoding="utf-8").splitlines()
        keep = lines[len(lines) // 2 :]
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(
            "\n".join(keep) + ("\n" if keep else ""), encoding="utf-8"
        )
        os.replace(tmp_path, self.path)
        logger.info(
            "%s rotated: kept newest %d of %d lines", self.path, len(keep), len(lines)
        )


def consumption_rate_pct_per_h(
    samples: Sequence[tuple[float, float, float]],
    window_s: float,
    now_unix: float,
    fill_intervals: Sequence[tuple[float, float]] = (),
) -> float | None:
    """Least-squares helium consumption rate over a trailing window.

    Fits a line to ``helium_pct`` vs. time over
    ``[now_unix - window_s, now_unix]``, excluding any sample that falls
    inside an ``(start, end)`` fill interval (so a fill's rising level does
    not read as negative consumption).

    Sign convention: **positive return value means the level is falling**
    (consumption). A negative return value means the level net *rose* over
    the window outside of any declared fill interval (e.g. an unmarked
    top-up) — the caller decides whether that is worth surfacing.

    Args:
        samples: ``(unix_time, helium_pct, nitrogen_pct)`` tuples, any order.
        window_s: Trailing window length in seconds.
        now_unix: The window's end time (unix seconds).
        fill_intervals: ``(start_unix, end_unix)`` intervals to exclude.

    Returns:
        The consumption rate in %/h (positive = falling level), or ``None``
        when fewer than two usable points remain after filtering, or all
        usable points share the same timestamp (a degenerate fit).
    """
    window_start = now_unix - window_s
    usable = [
        (t, helium)
        for (t, helium, _nitrogen) in samples
        if window_start <= t <= now_unix
        and not any(start <= t <= end for start, end in fill_intervals)
    ]
    if len(usable) < 2:
        return None

    n = len(usable)
    t_mean = sum(t for t, _ in usable) / n
    h_mean = sum(h for _, h in usable) / n
    numerator = sum((t - t_mean) * (h - h_mean) for t, h in usable)
    denominator = sum((t - t_mean) ** 2 for t, _ in usable)
    if denominator == 0:
        return None

    slope_per_s = numerator / denominator  # %/s; negative when level falls
    return -slope_per_s * 3600.0


# ── Legacy migration: cryogenics + operations → servicing ───────────────────


def _normalize_entry_kind(operation_name: str) -> str:
    """Turn a legacy operations-kind ``operation`` name into an ``entry_kind``.

    Args:
        operation_name: The legacy ``operations`` entry's ``operation`` field
            (e.g. ``"Sample Change"``).

    Returns:
        A lowercase, underscore-joined key (e.g. ``"sample_change"``), or
        ``"manual"`` if ``operation_name`` is blank.
    """
    normalized = operation_name.strip().lower().replace(" ", "_")
    return normalized or "manual"


def _derive_recording_filename(run_id: str, start_utc: str) -> str:
    """Derive a stable sidecar filename for a migrated recording.

    Args:
        run_id: The originating run id, if known.
        start_utc: The entry's start time (ISO 8601), used as a fallback key
            when ``run_id`` is unavailable.

    Returns:
        A filesystem-safe ``"<id>.json"`` basename, stable across repeated
        migrations of the same entry.
    """
    if run_id:
        safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in run_id)
        return f"{safe}.json"
    try:
        unix_time = datetime.fromisoformat(start_utc).timestamp()
    except (TypeError, ValueError):
        return f"legacy_{uuid.uuid4().hex}.json"
    return f"legacy_{int(unix_time)}.json"


def _write_level_curve_sidecar(
    root: Path,
    config_name: str,
    level_curve_json: str,
    run_id: str,
    start_utc: str,
    level_vi_name: str,
) -> str:
    """Convert an embedded legacy ``level_curve`` JSON string into a sidecar file.

    Args:
        root: The servicing store's root directory.
        config_name: Identity of the active config.
        level_curve_json: The legacy ``cryogenics`` entry's ``level_curve``
            field (``{"unix_time": [...], "helium_pct": [...]}`` JSON, or
            ``""``).
        run_id: The originating run id, for the derived filename.
        start_utc: The entry's start time, fallback filename key.
        level_vi_name: VI name used to build the sidecar's channel key.

    Returns:
        The sidecar's filename (to store in the new entry's ``recording``
        field), or ``""`` if there was nothing to convert (blank/malformed
        input) — this is a best-effort conversion, never a reason to fail
        the whole migration.
    """
    if not level_curve_json:
        return ""
    try:
        curve = json.loads(level_curve_json)
    except (TypeError, ValueError) as exc:
        logger.warning("migrate_legacy_servicing_log: malformed level_curve (%s)", exc)
        return ""
    if not isinstance(curve, dict):
        return ""
    unix_time = curve.get("unix_time")
    helium_pct = curve.get("helium_pct")
    if not isinstance(unix_time, list) or not isinstance(helium_pct, list):
        return ""

    filename = _derive_recording_filename(run_id, start_utc)
    sidecar_path = root / config_name / "recordings" / filename
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "unix_time": unix_time,
        "channels": {f"{level_vi_name}.helium_pct": helium_pct},
    }
    sidecar_path.write_text(json.dumps(payload), encoding="utf-8")
    return filename


def _migrated_entry_dict(
    values: dict[str, Any],
    *,
    source: str,
    run_id: str,
    created_utc: str,
) -> dict[str, Any]:
    """Build one merged/derived ``servicing`` entry line, preserving its time.

    Bypasses ``ServicingLogStore.add_entry()`` deliberately: migration must
    preserve each entry's original ``created_utc`` (a fresh timestamp would
    scramble the merged timeline's chronological order), which ``add_entry``
    does not allow a caller to set. Returns the JSONL line dict instead of
    appending it, so ``migrate_legacy_servicing_log`` can write ALL lines in
    one atomic replace — a crash mid-migration must never leave a partial
    ``servicing.jsonl`` (the idempotency check would then treat the half-done
    migration as complete forever).

    Args:
        values: The new entry's field values (coerced against the
            ``servicing`` kind's ``ParamSpec`` fields).
        source: Provenance to record (carried over from the legacy entry).
        run_id: Linked run id, if any (carried over from the legacy entry).
        created_utc: The original entry's creation time, preserved verbatim.

    Returns:
        The entry serialised via ``ServiceLogEntry.to_dict()``.
    """
    spec = DECLARED_LOG_KINDS["servicing"]
    coerced = _coerce_values(spec, values)
    entry = ServiceLogEntry(
        entry_id=uuid.uuid4().hex,
        kind="servicing",
        values=coerced,
        source=source,
        run_id=run_id,
        created_utc=created_utc or _utc_now_iso(),
        revision=1,
    )
    return entry.to_dict()


def migrate_legacy_servicing_log(
    root: Path | str,
    config_name: str,
    *,
    level_vi_name: str = "level_meter",
) -> bool:
    """Merge legacy ``cryogenics.jsonl`` + ``operations.jsonl`` into ``servicing.jsonl``.

    Pure, idempotent, one-time rewrite (docs/plans/unified-servicing-log-and-
    run-recording.md, "Legacy migration"). Reads the two legacy log kinds
    through the existing tolerant ``ServicingLogStore`` reader (a corrupt
    line is skipped, never raised), matches a fill's ``operations`` entry to
    its ``cryogenics`` entry by an exact
    ``(started_utc, finished_utc) == (start_utc, end_utc)`` match, and writes
    one merged/derived ``"servicing"`` entry per matched pair, per unmatched
    ``cryogenics`` entry, and per unmatched ``operations`` entry (e.g. a
    sample change) — each preserving its original ``created_utc`` so the
    merged timeline stays chronological. ``ln2_filled=True`` becomes a
    ``"LN2 topped up"`` note (``ln2_*_pct`` stay ``0.0``, levels were never
    tracked for LN2 in the legacy schema); a non-``"done"`` legacy ``status``
    plus its ``params`` JSON are folded into ``notes`` (the new schema has no
    ``status`` field). An embedded ``level_curve`` JSON string becomes a
    ``recordings/<id>.json`` sidecar. Both legacy files are renamed to
    ``<name>.jsonl.bak`` (only if migration actually ran).

    Never invoked automatically — a caller (a startup hook, a one-off
    script) must call this or ``ServicingLogStore.migrate_legacy()``
    explicitly.

    Args:
        root: Directory holding one subfolder per config (the servicing
            store's root).
        config_name: Identity of the active config.
        level_vi_name: VI name used to build a migrated level curve's sidecar
            channel key (``"<level_vi_name>.helium_pct"``).

    Returns:
        ``True`` if migration ran (even if it wrote zero entries because the
        legacy files were empty); ``False`` on a no-op — ``servicing.jsonl``
        already exists, or neither legacy file is present.

    Raises:
        OSError: If a legacy file cannot be renamed or a sidecar cannot be
            written.
    """
    root = Path(root)
    config_dir = root / config_name
    servicing_path = config_dir / "servicing.jsonl"
    cryo_path = config_dir / "cryogenics.jsonl"
    ops_path = config_dir / "operations.jsonl"

    if servicing_path.is_file():
        return False
    if not cryo_path.is_file() and not ops_path.is_file():
        return False

    legacy_store = ServicingLogStore(root, config_name)
    cryo_entries = sorted(
        legacy_store.entries("cryogenics") if cryo_path.is_file() else [],
        key=lambda entry: entry.created_utc,
    )
    op_entries = sorted(
        legacy_store.entries("operations") if ops_path.is_file() else [],
        key=lambda entry: entry.created_utc,
    )

    matched_op_ids: set[str] = set()
    migrated_lines: list[dict[str, Any]] = []

    for cryo in cryo_entries:
        match = next(
            (
                op
                for op in op_entries
                if op.entry_id not in matched_op_ids
                and op.values.get("operation") == "Helium Fill"
                and op.values.get("started_utc") == cryo.values.get("start_utc")
                and op.values.get("finished_utc") == cryo.values.get("end_utc")
            ),
            None,
        )

        notes_parts: list[str] = []
        cryo_notes = str(cryo.values.get("notes", ""))
        if cryo_notes:
            notes_parts.append(cryo_notes)
        if cryo.values.get("ln2_filled"):
            notes_parts.append("LN2 topped up")

        run_id = cryo.run_id
        origin = "manual" if cryo.source == "manual" else "machine"
        if match is not None:
            matched_op_ids.add(match.entry_id)
            run_id = run_id or match.run_id
            origin = "machine"
            status = str(match.values.get("status", ""))
            if status != "done":
                notes_parts.append(f"legacy status={status}")
                reason = str(match.values.get("reason", ""))
                if reason:
                    notes_parts.append(f"reason={reason}")
                params = str(match.values.get("params", "{}"))
                if params and params != "{}":
                    notes_parts.append(f"params={params}")

        recording = _write_level_curve_sidecar(
            root,
            config_name,
            str(cryo.values.get("level_curve", "")),
            run_id,
            str(cryo.values.get("start_utc", "")),
            level_vi_name,
        )

        migrated_lines.append(_migrated_entry_dict(
            {
                "entry_kind": "helium_fill",
                "person": str(cryo.values.get("person", "")),
                "start_utc": str(cryo.values.get("start_utc", "")),
                "end_utc": str(cryo.values.get("end_utc", "")),
                "helium_start_pct": float(cryo.values.get("helium_start_pct", 0.0)),
                "helium_end_pct": float(cryo.values.get("helium_end_pct", 0.0)),
                "ln2_start_pct": 0.0,
                "ln2_end_pct": 0.0,
                "notes": "; ".join(part for part in notes_parts if part),
                "recording": recording,
                "origin": origin,
            },
            source=cryo.source,
            run_id=run_id,
            created_utc=cryo.created_utc,
        ))

    for op in op_entries:
        if op.entry_id in matched_op_ids:
            continue

        notes_parts = []
        status = str(op.values.get("status", ""))
        if status != "done":
            notes_parts.append(f"legacy status={status}")
            reason = str(op.values.get("reason", ""))
            if reason:
                notes_parts.append(f"reason={reason}")
        params = str(op.values.get("params", "{}"))
        if params and params != "{}":
            notes_parts.append(f"params={params}")

        # The legacy operations stream had no person column; the value lives
        # inside the params JSON. Backtracking "who did the last sample
        # change" is the unified log's whole point, so pull it out.
        person = ""
        try:
            parsed_params = json.loads(params)
            if isinstance(parsed_params, dict):
                person = str(parsed_params.get("person", ""))
        except (TypeError, ValueError):
            pass

        entry_kind = _normalize_entry_kind(str(op.values.get("operation", "")))

        migrated_lines.append(_migrated_entry_dict(
            {
                "entry_kind": entry_kind,
                "person": person,
                "start_utc": str(op.values.get("started_utc", "")),
                "end_utc": str(op.values.get("finished_utc", "")),
                "helium_start_pct": 0.0,
                "helium_end_pct": 0.0,
                "ln2_start_pct": 0.0,
                "ln2_end_pct": 0.0,
                "notes": "; ".join(part for part in notes_parts if part),
                "recording": "",
                "origin": "machine",
            },
            source=op.source,
            run_id=op.run_id,
            created_utc=op.created_utc,
        ))

    # Atomic commit: all lines land in one os.replace(), so a crash before
    # this point leaves no servicing.jsonl at all (the migration simply runs
    # again next time) instead of a partial file the idempotency check would
    # mistake for a completed migration.
    tmp_path = servicing_path.with_name(servicing_path.name + ".tmp")
    tmp_path.write_text(
        "".join(json.dumps(line) + "\n" for line in migrated_lines),
        encoding="utf-8",
    )
    os.replace(tmp_path, servicing_path)

    if cryo_path.is_file():
        cryo_path.replace(cryo_path.with_name(cryo_path.name + ".bak"))
    if ops_path.is_file():
        ops_path.replace(ops_path.with_name(ops_path.name + ".bak"))

    return True


# ── CryogenicsRecorder: the automatic writer ────────────────────────────────


class CryogenicsRecorder(QObject):
    """The automatic servicing-log/helium-record writer (plan §6.3).

    Driven purely by existing Orchestrator signals — this class never touches
    hardware or the Station, and never raises out of a slot (every public
    method guards its body with a broad try/except + log, exactly like
    ``SessionManager``'s manifest handlers): a malformed ``states_updated``
    payload or run manifest must not crash a running measurement.

    The public methods are plain, directly callable methods (not
    ``pyqtSlot``-decorated) so tests can call them with synthetic dicts
    without any real Orchestrator, and headless (no widgets are created or
    required — only a ``QObject``/``QCoreApplication`` instance).

    Signals:
        cryo_warning (str): Emitted once when the helium level drops below
            ``warning_pct`` (hysteresis: re-armed only once the level rises
            back above ``warning_pct + warning_clear_margin_pct``). This is
            advisory only — it never trips ``evaluate_safety()`` — so it is
            not written to any servicing log.
    """

    cryo_warning = pyqtSignal(str)

    def __init__(
        self,
        helium_store: HeliumRecordStore,
        servicing_store: ServicingLogStore,
        *,
        level_vi_name: str,
        warning_pct: float,
        history_sample_s: float = 3600.0,
        warning_clear_margin_pct: float = 3.0,
        fill_operation_name: str = "Helium Fill",
    ) -> None:
        """Configure the recorder against its two stores.

        Args:
            helium_store: Where hourly helium/nitrogen samples are appended.
            servicing_store: Where the cryogenics-log and operations-log
                entries are appended.
            level_vi_name: Key into the ``states_updated`` state dict naming
                the level-meter VI (``state[level_vi_name]`` carries
                ``helium_level``/``nitrogen_level``).
            warning_pct: Advisory helium threshold (%); crossing it below
                emits ``cryo_warning``.
            history_sample_s: Minimum seconds between helium-record appends
                (default 3600 s — hourly, per plan §6.3).
            warning_clear_margin_pct: Hysteresis margin (%) added to
                ``warning_pct`` before the warning re-arms.
            fill_operation_name: The operation ``procedure`` name that, on
                finish, produces a ``"cryogenics"`` log entry.
        """
        super().__init__()
        self._helium_store = helium_store
        self._servicing_store = servicing_store
        self._level_vi_name = level_vi_name
        self._warning_pct = float(warning_pct)
        self._history_sample_s = float(history_sample_s)
        self._warning_clear_margin_pct = float(warning_clear_margin_pct)
        self._fill_operation_name = fill_operation_name

        self._last_helium_pct: float | None = None
        self._last_nitrogen_pct: float | None = None
        self._last_reading_utc: str = ""
        self._last_append_monotonic: float | None = None
        self._warning_active = False

        # Level + time captured at the fill operation's start, consumed on finish.
        self._fill_start_helium_pct: float | None = None
        self._fill_start_utc: str = ""

    def on_states_updated(self, state: dict[str, Any]) -> None:
        """Track the latest levels; decimate into the helium record; warn.

        Args:
            state: The Orchestrator's full station state
                (``{vi_name: {method_name: value}}``). Anything malformed or
                missing the level VI is silently ignored — this is a
                best-effort observer, not a safety path.
        """
        try:
            self._on_states_updated(state)
        except Exception:
            logger.exception("CryogenicsRecorder.on_states_updated failed")

    def _on_states_updated(self, state: dict[str, Any]) -> None:
        if not isinstance(state, dict):
            return
        vi_state = state.get(self._level_vi_name)
        if not isinstance(vi_state, dict):
            return
        helium = vi_state.get("helium_level")
        nitrogen = vi_state.get("nitrogen_level")
        if isinstance(helium, bool) or not isinstance(helium, (int, float)):
            return
        if isinstance(nitrogen, bool) or not isinstance(nitrogen, (int, float)):
            nitrogen = 0.0

        helium = float(helium)
        nitrogen = float(nitrogen)
        self._last_helium_pct = helium
        self._last_nitrogen_pct = nitrogen
        self._last_reading_utc = _utc_now_iso()

        now_mono = time.monotonic()
        if (
            self._last_append_monotonic is None
            or (now_mono - self._last_append_monotonic) >= self._history_sample_s
        ):
            self._helium_store.append(self._last_reading_utc, helium, nitrogen)
            self._last_append_monotonic = now_mono

        self._check_warning(helium)

    def _check_warning(self, helium_pct: float) -> None:
        """Emit ``cryo_warning`` once per low-helium episode (hysteresis)."""
        if not self._warning_active and helium_pct < self._warning_pct:
            self._warning_active = True
            message = (
                f"Helium level {helium_pct:.1f}% is below the warning threshold "
                f"{self._warning_pct:.1f}%"
            )
            logger.warning(message)
            self.cryo_warning.emit(message)
        elif self._warning_active and helium_pct >= (
            self._warning_pct + self._warning_clear_margin_pct
        ):
            self._warning_active = False

    def on_run_started(self, manifest: dict[str, Any]) -> None:
        """Remember the level/time at the start of the fill operation.

        Args:
            manifest: The Orchestrator's ``run_started`` manifest.
        """
        try:
            self._on_run_started(manifest)
        except Exception:
            logger.exception("CryogenicsRecorder.on_run_started failed")

    def _on_run_started(self, manifest: dict[str, Any]) -> None:
        if not isinstance(manifest, dict):
            return
        if str(manifest.get("procedure", "")) != self._fill_operation_name:
            return
        self._fill_start_helium_pct = self._last_helium_pct
        self._fill_start_utc = str(manifest.get("started_utc", "")) or self._last_reading_utc

    def on_run_finished(self, manifest: dict[str, Any]) -> None:
        """Append the operations-stream entry, and the cryogenics entry for a fill.

        Args:
            manifest: The Orchestrator's ``run_finished`` manifest.
        """
        try:
            self._on_run_finished(manifest)
        except Exception:
            logger.exception("CryogenicsRecorder.on_run_finished failed")

    def _on_run_finished(self, manifest: dict[str, Any]) -> None:
        if not isinstance(manifest, dict):
            return
        status = str(manifest.get("status", ""))
        procedure = str(manifest.get("procedure", ""))

        if str(manifest.get("kind", "")) == "operation":
            params = manifest.get("params")
            params_json = json.dumps(
                params if isinstance(params, dict) else {}, sort_keys=True, default=str
            )
            self._servicing_store.append_machine_entry(
                "operations",
                {
                    "operation": procedure,
                    "params": params_json,
                    "started_utc": str(manifest.get("started_utc", "")),
                    "finished_utc": str(manifest.get("finished_utc", "")),
                    "status": status,
                    "verified": status == "done",
                    "reason": str(manifest.get("reason", "")),
                },
            )

        if procedure == self._fill_operation_name:
            params = manifest.get("params")
            person = ""
            if isinstance(params, dict):
                person = str(params.get("person", ""))
            notes = "" if status == "done" else f"unverified: {manifest.get('reason', '')}"
            self._servicing_store.add_entry(
                "cryogenics",
                {
                    "person": person,
                    "start_utc": self._fill_start_utc,
                    "end_utc": str(manifest.get("finished_utc", "")),
                    "helium_start_pct": self._fill_start_helium_pct or 0.0,
                    "helium_end_pct": self._last_helium_pct or 0.0,
                    "ln2_filled": False,
                    "notes": notes,
                    "level_curve": self._extract_level_curve_json(manifest),
                },
                source="operation",
                run_id=str(manifest.get("run_id", "")),
            )
            self._fill_start_helium_pct = None
            self._fill_start_utc = ""

    @staticmethod
    def _extract_level_curve_json(manifest: dict[str, Any]) -> str:
        """Extract the fill's level curve from the run manifest's ``summary``.

        Reads ``manifest["summary"]["level_curve"]`` (the Orchestrator's
        duck-typed ``run_summary()`` hand-off, docs/plans/operation-
        concurrency-and-error-scoping.md §4) — ``HeliumFillOperation``'s
        shape is ``{"unix_time": [...], "helium_pct": [...]}``. Tolerant of
        every malformed shape (missing ``summary``, non-dict curve, non-list
        series): this is a best-effort observer, never a reason to lose the
        rest of the cryogenics-log entry.

        Args:
            manifest: The Orchestrator's ``run_finished`` manifest.

        Returns:
            A compact JSON string of the curve, or ``""`` if unavailable/
            malformed (the ``cryogenics`` kind's ``level_curve`` field
            default).
        """
        summary = manifest.get("summary")
        if not isinstance(summary, dict):
            return ""
        curve = summary.get("level_curve")
        if not isinstance(curve, dict):
            return ""
        unix_time = curve.get("unix_time")
        helium_pct = curve.get("helium_pct")
        if not isinstance(unix_time, list) or not isinstance(helium_pct, list):
            return ""
        try:
            return json.dumps({"unix_time": unix_time, "helium_pct": helium_pct})
        except (TypeError, ValueError):
            logger.warning("CryogenicsRecorder: level_curve not JSON-serialisable; dropping it")
            return ""
