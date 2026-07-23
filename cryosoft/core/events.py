# ---
# description: |
#   ErrorEvent: the structured payload carried by Orchestrator.error_event
#   (docs/plans/operation-concurrency-and-error-scoping.md §3). Replaces the
#   string-only error_occurred payload with a typed record naming which VI
#   (if any) an error concerns, at what blast-radius tier ("kind"), and how
#   severe it is. A tiny, dependency-free module (no Station/Orchestrator
#   imports) so both core.orchestrator (the emitter) and cryosoft.gui (the
#   consumer) can import it without crossing any layer contract.
# entry_point: Not run directly.
# dependencies: []
# input: |
#   Constructed by the Orchestrator at the point an error/fault/emergency is
#   detected.
# process: |
#   Plain frozen dataclass — no behavior.
# output: |
#   One ErrorEvent instance per Orchestrator.error_event emission.
# last_updated: 2026-07-21
# ---

"""ErrorEvent — structured error/fault payload (core, dependency-free)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorEvent:
    """One structured error/fault notification.

    Attributes:
        vi_name: The VI the event concerns, or ``None`` for a machine-wide
            event with no single originating instrument (e.g. an unhandled
            tick-boundary exception). May also be a comma-joined list of
            names when more than one VI is implicated (e.g. an EMERGENCY
            tripped by more than one instrument's safety flag).
        kind: The blast-radius tier this event belongs to (plan §3):
            ``"fault"`` (a VI-scoped comm/stale/disconnected fault that
            quarantines only that VI), ``"run_failure"`` (an active run's
            claimed VI faulted — the run fails, the machine returns to
            IDLE), ``"safety"`` (a tripped safety flag — global EMERGENCY),
            or ``"internal"`` (an unhandled tick-boundary exception —
            global ERROR, unknown blast radius).
        severity: ``"warning"``, ``"error"``, or ``"emergency"``.
        message: Human-readable description, suitable for direct display.
        timestamp: Unix time the event was created (``time.time()``).
    """

    vi_name: str | None
    kind: str
    severity: str
    message: str
    timestamp: float
