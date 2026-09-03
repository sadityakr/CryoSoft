"""The **Assistant transcript** — one conversation, written down as evidence.

**A conversation with the embedded assistant is evidence, exactly like the
Agent feed.** The feed already records what an autonomous client *did* — the
commands it submitted, the verdicts that answered them, the recorded tool
calls. What it cannot record is what the assistant was *asked* and what it
*said*, because those never reach the engine at all. This module writes that
half to ``assistant_transcript.jsonl`` inside the experiment's own folder, so
copying the folder copies the whole story: the question, the answer, and every
tool call in between, joinable to the feed by tool name and time.

The record standard
-------------------

One JSON object per line, appended, never rewritten — the same journal-of-
facts discipline the **Agent feed** follows, and for the same reason: every
line is a distinct thing that happened, ordered by its ``seq``, and nothing
here ever supersedes an earlier line.

Every record carries every key below; a value that does not apply to this
record kind is ``null``, never a missing key, so a reader never has to guess
whether an absent key means "no" or "old file".

======================= ============== =========================================
Field                   Type           Meaning
======================= ============== =========================================
``schema``              int            Record schema version, ``SCHEMA_VERSION``.
``ts``                  float          Epoch seconds the record was written.
``seq``                 int            Per-file counter, from 1, strictly increasing.
``experiment_id``       str            The owning experiment's store key.
``turn``                int            Which exchange this belongs to, from 1.
``record``              str            ``"user"``, ``"assistant"`` or ``"tool"``.
``role``                str            The **Role** the assistant was acting under.
``text``                str | null     The message text; user and assistant records.
``tool``                str | null     The **Tool spec**'s name; tool records only.
``args``                obj | null     The arguments the tool was called with.
``verdict``             obj | null     ``{"code", "reason"}`` — the tool's answer.
``detail``              obj | null     The structured half of that answer, or the
                                       assistant message's ``stop_reason``.
``cost``                obj | null     The **cost line** an assistant message cost:
                                       ``model``, ``input_tokens``,
                                       ``output_tokens``, ``cost_usd``.
======================= ============== =========================================

One record per message and per tool call, in the order they happened. A turn
is therefore one ``user`` record followed by alternating ``assistant`` and
``tool`` records until the assistant answers without asking for a tool.

Recording never raises into its caller: it sits on the path a physicist's
question travels, and a full disk must degrade to a logged warning rather
than swallow the answer.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "RECORD_USER",
    "RECORD_ASSISTANT",
    "RECORD_TOOL",
    "AssistantTranscript",
    "read_transcript",
]

#: Version of the record shape documented above. Bump it when the shape
#: changes; adding a field is a bump, renaming or retyping one is forbidden.
SCHEMA_VERSION = 1

#: What the physicist asked.
RECORD_USER = "user"

#: What the assistant answered, and what that answer cost.
RECORD_ASSISTANT = "assistant"

#: One tool call the assistant made through the **Agent gateway**, and the
#: answer it got back — refusals included, verbatim.
RECORD_TOOL = "tool"


def _append_line(path: Path, payload: dict[str, Any]) -> None:
    """Append one JSON line to ``path``, creating parent directories.

    Args:
        path: The JSONL file to append to.
        payload: JSON-serialisable object for the new line.

    Raises:
        OSError: If the directory cannot be created or the file written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def read_transcript(
    path: str | Path, since_seq: int | None = None
) -> list[dict[str, Any]]:
    """Read an assistant transcript, oldest first, tolerating a corrupt line.

    The read side of the record standard. A line that is not readable JSON, or
    does not hold an object, is skipped with a WARNING rather than failing the
    read — one mangled line must never strand the rest of the conversation,
    exactly as in ``agent_feed.py`` and ``store.py``.

    Args:
        path: The transcript file, normally from
            ``ExperimentStore.assistant_transcript_path()``.
        since_seq: When given, only records with a ``seq`` strictly greater
            than this are returned — how a reader follows a live conversation
            without re-reading what it already has.

    Returns:
        One JSON-safe dict per well-formed record, in file order. ``[]`` when
        the file does not exist or cannot be read.
    """
    file_path = Path(path)
    try:
        raw = file_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning("Could not read the assistant transcript %s: %s", file_path, exc)
        return []

    records: list[dict[str, Any]] = []
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            logger.warning("Skipping corrupt transcript line %s:%d", file_path, number)
            continue
        if not isinstance(payload, dict):
            logger.warning(
                "Skipping non-object transcript line %s:%d", file_path, number
            )
            continue
        if since_seq is not None:
            seq = payload.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq <= since_seq:
                continue
        records.append(payload)
    return records


class AssistantTranscript:
    """The append-only conversation record for one experiment.

    One instance per experiment folder. Cheap to construct — the file is read
    once, on the first write, to pick the sequence number up where an earlier
    process left it, and is never held open.

    Attributes:
        experiment_id: The experiment this transcript belongs to, stamped on
            every record so a file copied out of its folder still says what it
            belongs to.
    """

    def __init__(self, path: str | Path, experiment_id: str = "") -> None:
        """Bind to one transcript file.

        Args:
            path: The journal file, from
                ``ExperimentStore.assistant_transcript_path()`` — the store
                owns where it sits, this class owns what is in it. Neither it
                nor its parent is created until something is actually
                recorded.
            experiment_id: The owning experiment's store key.
        """
        self._path = Path(path)
        self.experiment_id = str(experiment_id)
        self._seq: int | None = None

    @property
    def path(self) -> Path:
        """The journal file this transcript reads and appends to."""
        return self._path

    # ── Recording ─────────────────────────────────────────────────────

    def record_user(self, turn: int, role: str, text: str) -> dict[str, Any]:
        """Record what the physicist asked.

        Args:
            turn: Which exchange this belongs to, from 1.
            role: The **Role** the assistant is acting under.
            text: The question, verbatim.

        Returns:
            The record as written, so a caller can render exactly what the
            evidence file holds rather than a second rendering of its own.
        """
        return self._append(
            record=RECORD_USER, turn=turn, role=role, text=text
        )

    def record_assistant(
        self,
        turn: int,
        role: str,
        text: str,
        cost: dict[str, Any] | None = None,
        stop_reason: str = "",
    ) -> dict[str, Any]:
        """Record what the assistant said, and what saying it cost.

        Args:
            turn: Which exchange this belongs to.
            role: The **Role** the assistant is acting under.
            text: The generated text, joined across the answer's text blocks.
            cost: The **cost line** of this one model call — ``model``,
                ``input_tokens``, ``output_tokens``, ``cost_usd``.
            stop_reason: Why the model stopped, as the vendor reported it.

        Returns:
            The record as written.
        """
        return self._append(
            record=RECORD_ASSISTANT,
            turn=turn,
            role=role,
            text=text,
            detail={"stop_reason": stop_reason} if stop_reason else None,
            cost=dict(cost) if cost else None,
        )

    def record_tool(
        self,
        turn: int,
        role: str,
        tool: str,
        args: dict[str, Any] | None,
        answer: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Record one tool call and the answer the gateway gave it.

        Every call is recorded, refusals included and verbatim: what the
        assistant TRIED to do is as much a part of the record as what it
        managed to do, which is the same rule the **Agent feed** follows for
        a submitted command.

        Args:
            turn: Which exchange this belongs to.
            role: The **Role** the assistant is acting under.
            tool: The tool's name, as published by the **Tool surface**.
            args: The arguments it was called with.
            answer: The gateway's answer dict.

        Returns:
            The record as written.
        """
        payload = dict(answer or {})
        return self._append(
            record=RECORD_TOOL,
            turn=turn,
            role=role,
            tool=tool,
            args=dict(args or {}),
            verdict={
                "code": str(payload.get("code", "")),
                "reason": str(payload.get("reason", "")),
            },
            detail=dict(payload.get("detail") or {}) or None,
        )

    # ── The one write path ────────────────────────────────────────────

    def _append(
        self,
        *,
        record: str,
        turn: int,
        role: str,
        text: str | None = None,
        tool: str | None = None,
        args: dict[str, Any] | None = None,
        verdict: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        cost: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one record, filling in every key of the standard.

        Never raises: a transcript that cannot be written must not swallow the
        answer the physicist is waiting for.

        Args:
            record: Which record kind this is (``RECORD_*``).
            turn: Which exchange this belongs to.
            role: The **Role** the assistant is acting under.
            text: The message text, on a user or assistant record.
            tool: The tool's name, on a tool record.
            args: The arguments, on a tool record.
            verdict: ``{"code", "reason"}``, on a tool record.
            detail: The structured half of a tool answer, or an assistant
                record's ``stop_reason``.
            cost: The **cost line**, on an assistant record.

        Returns:
            The record as written (or as it would have been written, when the
            write itself failed) — never ``None``, so a caller always has
            something to render.
        """
        payload: dict[str, Any] = {
            "schema": SCHEMA_VERSION,
            "ts": time.time(),
            "seq": 0,
            "experiment_id": self.experiment_id,
            "turn": int(turn),
            "record": record,
            "role": role,
            "text": text,
            "tool": tool,
            "args": args,
            "verdict": verdict,
            "detail": detail,
            "cost": cost,
        }
        try:
            payload["seq"] = self._next_seq()
            _append_line(self._path, payload)
        except Exception:  # noqa: BLE001 — recording must never disturb the turn
            logger.exception("assistant transcript: recording failed (non-fatal)")
        return payload

    def _next_seq(self) -> int:
        """Return the sequence number for the next record.

        Seeded once, from the highest ``seq`` already in the file, so a
        transcript continued by a later process keeps counting instead of
        restarting at 1 and making the conversation unorderable.

        Returns:
            A number strictly greater than every ``seq`` written so far.
        """
        if self._seq is None:
            self._seq = max(
                (
                    payload["seq"]
                    for payload in read_transcript(self._path)
                    if isinstance(payload.get("seq"), int)
                    and not isinstance(payload.get("seq"), bool)
                ),
                default=0,
            )
        self._seq += 1
        return self._seq
