"""Body renderers — what CryoSoft writes *into* an ELN entry.

Two cooperating levels of "template" exist, and this module is the second:

1. **Backend-side entry templates** (categories, team defaults) stay the
   lab's: an adapter creates an entry *from* a template id chosen in settings,
   so an existing eLabFTW template keeps working untouched.
2. **CryoSoft-side body renderers** — this module. Plain Python functions, no
   template-language dependency, producing a deterministic, self-contained
   HTML fragment: no ``<script>``, no ``<link>``, no ``<img>``, no external
   URL of any kind, so the entry renders identically in the notebook, in an
   export, and in a test snapshot. Every value is HTML-escaped.

The unit rendered is one **run**: the run id and its experiment, the procedure
and its parameters, the setup (config identity and instruments) it ran on, the
timestamps, the outcome, and where the data file lives. That is exactly the
content of the Orchestrator's run manifest plus the session layer's experiment
context, which is why the publisher can render a job at the moment the run
finishes and queue the finished text — an outbox job never has to re-render
later against state that has since moved on.

Two bodies exist, built from the same shared row builders so a run reads
identically in both: ``render_run_body()`` for a published run, and
``render_draft_body()`` for a **draft entry**, which puts a drafted summary
above the facts it was drafted from. The drafted prose is model output and is
therefore escaped like any other value — ``render_prose_section()`` is the one
door untrusted text comes through, and it can introduce no markup at all.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CELL_CHARS = 400


def _text(value: object) -> str:
    """Return an HTML-escaped, length-capped rendering of one scalar value.

    Args:
        value: Any manifest value.

    Returns:
        Escaped text; ``"—"`` for ``None``/empty, truncated with an ellipsis
        beyond ``_MAX_CELL_CHARS`` so one pathological parameter cannot
        produce a megabyte of entry body.
    """
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        raw = repr(value)
    elif isinstance(value, (list, tuple)):
        raw = ", ".join(str(item) for item in value)
    elif isinstance(value, Mapping):
        raw = ", ".join(f"{key}={value[key]}" for key in sorted(value, key=str))
    else:
        raw = str(value)
    if len(raw) > _MAX_CELL_CHARS:
        raw = raw[:_MAX_CELL_CHARS] + "…"
    return html.escape(raw)


def _rows(pairs: list[tuple[str, object]]) -> str:
    """Render ``(label, value)`` pairs as HTML table rows.

    Args:
        pairs: Ordered label/value pairs.

    Returns:
        The concatenated ``<tr>`` markup.
    """
    return "".join(
        f"<tr><th align='left'>{html.escape(str(label))}</th>"
        f"<td>{_text(value)}</td></tr>"
        for label, value in pairs
    )


def _table(caption: str, pairs: list[tuple[str, object]]) -> str:
    """Render a captioned two-column table, or an empty string when no rows.

    Args:
        caption: Section heading.
        pairs: Ordered label/value pairs; an empty list renders nothing.

    Returns:
        The ``<h3>`` + ``<table>`` markup.
    """
    if not pairs:
        return ""
    return (
        f"<h3>{html.escape(caption)}</h3>"
        f"<table border='1' cellpadding='4' cellspacing='0'>{_rows(pairs)}</table>"
    )


def render_run_title(
    manifest: Mapping[str, Any], experiment_title: str = ""
) -> str:
    """Render the ELN entry title for one run.

    Args:
        manifest: The run manifest (``procedure``, ``run_id``, ``started_utc``).
        experiment_title: The owning experiment's title, prefixed when given.

    Returns:
        A single-line plain-text title (not HTML — backends set titles as text).
    """
    procedure = str(manifest.get("procedure") or "Run")
    started = str(manifest.get("started_utc") or "")
    stem = f"{procedure} — {started}" if started else procedure
    return f"{experiment_title} — {stem}" if experiment_title else stem


def _run_rows(
    manifest: Mapping[str, Any], data_path: str = ""
) -> list[tuple[str, object]]:
    """Return the ``Run`` table's label/value pairs for one manifest.

    Shared by every body renderer here, so a run reads identically in a
    published entry and in a draft.

    Args:
        manifest: The run manifest.
        data_path: Where the run's data file lives, or ``""``.

    Returns:
        The ordered label/value pairs.
    """
    return [
        ("Run id", manifest.get("run_id")),
        ("Procedure", manifest.get("procedure")),
        ("Kind", manifest.get("kind")),
        ("Started (UTC)", manifest.get("started_utc")),
        ("Finished (UTC)", manifest.get("finished_utc")),
        ("Outcome", manifest.get("status")),
        ("Reason", manifest.get("reason")),
        ("Data file", data_path),
    ]


def _experiment_rows(
    experiment_id: str, experiment_title: str
) -> list[tuple[str, object]]:
    """Return the ``Experiment`` table's rows, or an empty list when unknown.

    Args:
        experiment_id: The owning experiment's id, or ``""``.
        experiment_title: The owning experiment's title, or ``""``.

    Returns:
        The ordered label/value pairs; empty when neither is known, which
        renders nothing at all.
    """
    if not (experiment_id or experiment_title):
        return []
    return [("Experiment id", experiment_id), ("Title", experiment_title)]


def _param_rows(manifest: Mapping[str, Any]) -> list[tuple[str, object]]:
    """Return the ``Parameters`` table's rows, sorted by key.

    Args:
        manifest: The run manifest, whose ``params`` are rendered.

    Returns:
        The ordered label/value pairs; empty when the run declared none.
    """
    params = manifest.get("params")
    if not isinstance(params, Mapping) or not params:
        return []
    return [(str(key), params[key]) for key in sorted(params, key=str)]


def _setup_rows(setup: Mapping[str, Any] | None) -> list[tuple[str, object]]:
    """Return the ``Setup`` table's rows, sorted by instrument name.

    Args:
        setup: The setup tier of ``ExperimentManager.experiment_context()``
            (``config_name`` plus per-VI ``instruments``), or ``None``.

    Returns:
        The ordered label/value pairs.
    """
    setup_map = dict(setup or {})
    rows: list[tuple[str, object]] = []
    config_name = setup_map.get("config_name")
    if config_name:
        rows.append(("Config", config_name))
    instruments = setup_map.get("instruments")
    if isinstance(instruments, Mapping):
        for vi_name in sorted(instruments, key=str):
            rows.append((str(vi_name), instruments[vi_name]))
    return rows


def render_prose_section(caption: str, text: str) -> str:
    """Render free text as escaped paragraphs under a heading.

    The one door untrusted prose comes through: a drafted summary is model
    output, so it is escaped and split on blank lines into ``<p>`` elements
    rather than inserted as markup. Nothing here can introduce a tag, a link
    or a script into an entry body. A URL the prose happens to mention
    survives as inert text — escaping is what removes every way it could be
    fetched or followed, so the body still renders identically in the
    notebook, in an export, and in a test snapshot.

    Args:
        caption: Section heading.
        text: The prose; blank input renders nothing.

    Returns:
        The ``<h3>`` + ``<p>`` markup, or ``""`` when *text* is empty.
    """
    paragraphs = [block.strip() for block in text.strip().split("\n\n")]
    rendered = "".join(
        f"<p>{html.escape(block)}</p>" for block in paragraphs if block
    )
    if not rendered:
        return ""
    return f"<h3>{html.escape(str(caption))}</h3>{rendered}"


def render_stats_section(stats: Mapping[str, Mapping[str, Any]]) -> str:
    """Render per-column summary statistics as one table.

    Deterministic: columns and their statistics are emitted in sorted order,
    so the same statistics always render byte-identical HTML.

    Args:
        stats: ``{column: Stats.to_json()}`` — the NaN-aware summary
            ``core.data_reader.summary_stats()`` produces per column.

    Returns:
        The ``<h3>`` + ``<table>`` markup, or ``""`` when there is nothing to
        summarise.
    """
    columns = [name for name in sorted(stats, key=str) if isinstance(stats[name], Mapping)]
    if not columns:
        return ""
    fields = ("count", "min", "max", "mean", "std", "first", "last")
    header = "".join(f"<th align='left'>{html.escape(name)}</th>" for name in ("Column", *fields))
    rows = "".join(
        "<tr>"
        + f"<th align='left'>{html.escape(str(name))}</th>"
        + "".join(f"<td>{_text(stats[name].get(field))}</td>" for field in fields)
        + "</tr>"
        for name in columns
    )
    return (
        "<h3>Column statistics</h3>"
        f"<table border='1' cellpadding='4' cellspacing='0'>"
        f"<tr>{header}</tr>{rows}</table>"
    )


def render_run_body(
    manifest: Mapping[str, Any],
    experiment_id: str = "",
    experiment_title: str = "",
    setup: Mapping[str, Any] | None = None,
    data_path: str = "",
    findings: str = "",
) -> str:
    """Render one run's ELN entry body.

    Deterministic: parameters and instruments are emitted in sorted key order,
    so the same manifest always renders byte-identical HTML.

    Args:
        manifest: The Orchestrator's run manifest — ``run_id``, ``procedure``,
            ``kind``, ``params``, ``started_utc``, ``finished_utc``,
            ``status``, ``reason``.
        experiment_id: The owning experiment's id, or ``""``.
        experiment_title: The owning experiment's title, or ``""``.
        setup: The setup tier of ``ExperimentManager.experiment_context()``
            (``config_name`` plus per-VI ``instruments``), or ``None``.
        data_path: Where the run's data file lives, rendered as plain text
            (the file itself is attached separately by the publisher).
        findings: Optional free-text science notes to include verbatim
            (escaped).

    Returns:
        A self-contained HTML fragment.
    """
    parts: list[str] = [
        _table("Run", _run_rows(manifest, data_path)),
        _table("Experiment", _experiment_rows(experiment_id, experiment_title)),
        _table("Parameters", _param_rows(manifest)),
        _table("Setup", _setup_rows(setup)),
    ]
    if findings:
        parts.append(f"<h3>Findings</h3><p>{_text(findings)}</p>")
    parts.append("<p><em>Published by CryoSoft.</em></p>")
    return "".join(part for part in parts if part)


def render_draft_body(
    summary: str,
    manifest: Mapping[str, Any],
    stats: Mapping[str, Mapping[str, Any]] | None = None,
    experiment_id: str = "",
    experiment_title: str = "",
    setup: Mapping[str, Any] | None = None,
    station: Mapping[str, Any] | None = None,
    data_path: str = "",
    operator_note: str = "",
) -> str:
    """Render one **draft entry**'s body: a drafted summary over the facts.

    The same self-contained, escaped, deterministic HTML ``render_run_body()``
    produces, in the order a reviewer reads it: the drafted prose first, then
    the facts it was drafted from, so a human approving the entry can check
    every sentence against the tables below it. The summary is escaped like
    any other value — it is model output, not markup.

    Args:
        summary: The drafted prose. Escaped, never inserted as markup.
        manifest: The run manifest.
        stats: ``{column: Stats.to_json()}``, or ``None``.
        experiment_id: The owning experiment's id, or ``""``.
        experiment_title: The owning experiment's title, or ``""``.
        setup: The setup tier of ``ExperimentManager.experiment_context()``.
        station: The **Station info** snapshot as JSON (``setup`` and
            ``instruments``), or ``None`` — what the run actually ran on.
        data_path: Where the run's data file lives.
        operator_note: The operator's own note passed into the draft, if any.

    Returns:
        A self-contained HTML fragment.
    """
    parts: list[str] = [
        render_prose_section("Summary (drafted, unreviewed)", summary),
        _table("Run", _run_rows(manifest, data_path)),
        _table("Experiment", _experiment_rows(experiment_id, experiment_title)),
        _table("Parameters", _param_rows(manifest)),
        render_stats_section(dict(stats or {})),
        _table("Setup", _setup_rows(setup) + _station_rows(station)),
    ]
    if operator_note:
        parts.append(f"<h3>Operator note</h3><p>{_text(operator_note)}</p>")
    parts.append(
        "<p><em>Drafted by CryoSoft; reviewed by a human before publication.</em></p>"
    )
    return "".join(part for part in parts if part)


def _station_rows(station: Mapping[str, Any] | None) -> list[tuple[str, object]]:
    """Return the station-declaration rows appended to the ``Setup`` table.

    Args:
        station: The **Station info** snapshot as JSON, or ``None``.

    Returns:
        The setup name and one row per declared instrument, sorted by name.
    """
    snapshot = dict(station or {})
    rows: list[tuple[str, object]] = []
    name = snapshot.get("setup")
    if name:
        rows.append(("Setup", name))
    instruments = snapshot.get("instruments")
    if isinstance(instruments, (list, tuple)):
        declared = [item for item in instruments if isinstance(item, Mapping)]
        for item in sorted(declared, key=lambda entry: str(entry.get("name", ""))):
            rows.append(
                (
                    str(item.get("name", "")),
                    f"{item.get('kind', '') or 'instrument'} "
                    f"({item.get('vi_class', '') or 'unknown class'})",
                )
            )
    return rows


def render_run_metadata(
    manifest: Mapping[str, Any], experiment_id: str = "", data_path: str = ""
) -> dict[str, Any]:
    """Render the JSON-safe metadata block stored alongside the entry body.

    The machine-readable twin of the body: the same facts as flat scalars, so
    a later search in the notebook can filter by run id, procedure, or
    outcome without parsing HTML.

    Args:
        manifest: The run manifest.
        experiment_id: The owning experiment's id, or ``""``.
        data_path: Where the run's data file lives.

    Returns:
        A flat, JSON-safe dict.
    """
    return {
        "run_id": str(manifest.get("run_id") or ""),
        "experiment_id": experiment_id,
        "procedure": str(manifest.get("procedure") or ""),
        "kind": str(manifest.get("kind") or ""),
        "started_utc": str(manifest.get("started_utc") or ""),
        "finished_utc": str(manifest.get("finished_utc") or ""),
        "status": str(manifest.get("status") or ""),
        "reason": str(manifest.get("reason") or ""),
        "data_file": data_path,
        "source": "cryosoft",
    }
