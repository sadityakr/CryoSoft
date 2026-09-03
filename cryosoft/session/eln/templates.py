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
    setup_map = dict(setup or {})
    parts: list[str] = [
        _table(
            "Run",
            [
                ("Run id", manifest.get("run_id")),
                ("Procedure", manifest.get("procedure")),
                ("Kind", manifest.get("kind")),
                ("Started (UTC)", manifest.get("started_utc")),
                ("Finished (UTC)", manifest.get("finished_utc")),
                ("Outcome", manifest.get("status")),
                ("Reason", manifest.get("reason")),
                ("Data file", data_path),
            ],
        ),
        _table(
            "Experiment",
            [
                ("Experiment id", experiment_id),
                ("Title", experiment_title),
            ]
            if experiment_id or experiment_title
            else [],
        ),
    ]

    params = manifest.get("params")
    if isinstance(params, Mapping) and params:
        parts.append(
            _table(
                "Parameters",
                [(key, params[key]) for key in sorted(params, key=str)],
            )
        )

    setup_rows: list[tuple[str, object]] = []
    config_name = setup_map.get("config_name")
    if config_name:
        setup_rows.append(("Config", config_name))
    instruments = setup_map.get("instruments")
    if isinstance(instruments, Mapping):
        for vi_name in sorted(instruments, key=str):
            setup_rows.append((vi_name, instruments[vi_name]))
    parts.append(_table("Setup", setup_rows))

    if findings:
        parts.append(f"<h3>Findings</h3><p>{_text(findings)}</p>")

    parts.append("<p><em>Published by CryoSoft.</em></p>")
    return "".join(part for part in parts if part)


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
