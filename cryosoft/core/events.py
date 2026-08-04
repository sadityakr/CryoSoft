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
        kind: The blast-radius tier this event belongs to:
            ``"fault"`` (a VI-scoped comm/stale/disconnected fault that
            quarantines only that VI), ``"run_failure"`` (an active run's
            claimed VI faulted — the run fails, the machine returns to
            IDLE), ``"safety"`` (a tripped safety flag — global EMERGENCY),
            ``"internal"`` (an unhandled tick-boundary exception —
            global ERROR, unknown blast radius), or ``"safety_hold"`` (a
            VI-scoped safety-hold enforcement action — the Orchestrator
            re-asserting or failing to re-assert ``standby()`` on a VI held
            by a hold-severity safety condition; scoped to that one VI,
            never a blast radius beyond it, unlike ``"safety"`` above).
        severity: ``"warning"``, ``"error"``, or ``"emergency"``.
        message: Human-readable description, suitable for direct display.
        timestamp: Unix time the event was created (``time.time()``).
    """

    vi_name: str | None
    kind: str
    severity: str
    message: str
    timestamp: float
