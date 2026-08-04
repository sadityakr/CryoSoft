"""Operational-status record — the runtime troubleshooting signal.

Sibling to the offline ``cryosoft.troubleshoot`` engine: that classifies
*communication* faults at setup (app closed); this classifies *progress* during
a live run (app open, reading live Orchestrator/Station state). They share the
``str, Enum`` fault-code + JSON-ready record shape so the troubleshoot layer can
consume both uniformly. This module is pure data assembly and holds no
references to the Orchestrator, Station, or Qt, so it is unit-testable in
isolation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import Enum

from cryosoft.core.conditions import Condition


class RunFaultCode(str, Enum):
    """Stable, machine-readable runtime health codes.

    ``str, Enum`` makes each member serialize as its plain string value, so a
    record is directly JSON-ready.

    * ``OK``              — ramping/settling normally, or idle.
    * ``VI_STALE``        — the instrument stopped updating (cached values).
    * ``VI_DISCONNECTED`` — repeated comms failures; assumed off the bus.
    * ``QUENCH``          — a magnet reported a quench.
    * ``RAMP_STALLED``    — a ramp made no progress for several ticks
                            (``cryosoft.core.stall_detection``).
    * ``STALLED_RUN``     — RESERVED, no longer emitted. Previously a fixed
                            30 s timeout on any state assumed to last a single
                            tick, which was an assumption about how
                            procedures are written rather than a physical
                            fact (a long lock-in time constant or a heavily
                            averaged point can legitimately keep MEASURING
                            active past 30 s). The producer was removed;
                            the member is kept because
                            ``resources/mcp-compatibility.md`` documents
                            ``RunFaultCode`` values as a stable API that is
                            never renamed, and existing consumers
                            (``troubleshoot.status_reader``,
                            ``gui.diagnostics_window``) still map the string
                            for display so an old ``status.jsonl`` renders
                            correctly.
    """

    OK = "OK"
    VI_STALE = "VI_STALE"
    VI_DISCONNECTED = "VI_DISCONNECTED"
    QUENCH = "QUENCH"
    RAMP_STALLED = "RAMP_STALLED"
    STALLED_RUN = "STALLED_RUN"


# Higher = more severe; the record's overall verdict is the worst VI's code.
# Physical instrument faults (quench, lost comms) outrank a progress stall.
_SEVERITY: dict[RunFaultCode, int] = {
    RunFaultCode.OK: 0,
    RunFaultCode.VI_STALE: 2,
    RunFaultCode.RAMP_STALLED: 3,
    RunFaultCode.STALLED_RUN: 3,
    RunFaultCode.QUENCH: 4,
    RunFaultCode.VI_DISCONNECTED: 4,
}

_SEVERITY_BY_VALUE: dict[str, int] = {code.value: sev for code, sev in _SEVERITY.items()}


def _worse(a: RunFaultCode, b: RunFaultCode) -> RunFaultCode:
    """Return the more severe of two codes."""
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def worst_code(code_values: list[str]) -> str:
    """Return the most severe of a list of code string values (OK if empty)."""
    if not code_values:
        return RunFaultCode.OK.value
    return max(code_values, key=lambda c: _SEVERITY_BY_VALUE.get(c, 0))


@dataclass
class VIHealth:
    """One system VI's ramp-progress facts and verdict within a single tick.

    ``@dataclass`` auto-generates ``__init__``/``__repr__``/``__eq__`` from the
    field list — this is a pure data record, so that is exactly the intent
    (mirrors ``troubleshoot.engine.ProbeResult``).
    """

    vi_name: str
    value: float | None        # current value in user units (T, K)
    target: float | None       # active ramp target, same units
    gap: float | None          # |value - target|
    closing: float | None      # gap decrease since last tick (+ = converging)
    rate: float | None          # user units per minute
    eta_s: float | None        # gap / rate, seconds to target at current rate
    ramp_status: str           # RAMPING / TARGET_REACHED / IDLE
    phase: str | None          # sub-phase (e.g. warmup/ramping) or None if N/A
    code: RunFaultCode
    detail: str = ""

    def as_dict(self) -> dict:
        """Return a JSON-ready plain dict (RunFaultCode becomes its string)."""
        data = asdict(self)
        data["code"] = self.code.value
        return data


def _condition_as_dict(condition: Condition) -> dict:
    """Render one `Condition` as the JSON-safe shape carried in the record.

    Args:
        condition: The condition to render.

    Returns:
        A plain dict with ``key``, ``origin``, ``severity``, ``kind``,
        ``message``, ``affected`` (``"all"`` for a station-wide condition,
        else a sorted list of VI names), ``since``, and ``acknowledged``.
    """
    return {
        "key": condition.key,
        "origin": condition.origin,
        "severity": condition.severity,
        "kind": condition.kind,
        "message": condition.message,
        "affected": (
            "all" if condition.affected_vis is None else sorted(condition.affected_vis)
        ),
        "since": condition.since,
        "acknowledged": condition.acknowledged,
    }


def build_operational_status(
    *,
    orch_state: str,
    elapsed_in_state_s: float,
    state: dict[str, dict],
    ramp_info: dict[str, dict],
    prev_gaps: dict[str, float],
    wait_target_s: float | None = None,
    wait_elapsed_s: float | None = None,
    progress: float | None = None,
    active_gates: list[str] | None = None,
    conditions: Sequence[Condition] = (),
) -> tuple[dict, dict[str, float]]:
    """Assemble one operational-status record and the next-tick gap map.

    Pure: no hardware, no Qt, no I/O. The caller (Orchestrator) supplies the
    already-polled ``state`` snapshot and ``ramp_info`` (Station.get_ramp_status,
    which carries value/target/rate/ramp_status/phase per system VI) so this
    does not poll anything itself. Only unambiguous codes are set here; the
    heuristic stall verdict is layered on by ``cryosoft.core.stall_detection``.

    Args:
        orch_state: Orchestrator state name (e.g. ``"RAMPING"``).
        elapsed_in_state_s: Seconds since that state was entered.
        state: The station state snapshot ``{vi_name: {field: value, ...}}``,
            used for the ``_stale`` / ``_disconnected`` flags and magnet quench.
        ramp_info: ``{vi_name: {"value","target","rate","ramp_status","phase"}}``.
        prev_gaps: Per-VI gap from the previous tick, for the closing fact.
        wait_target_s / wait_elapsed_s: Settle-wait clock, if in a wait.
        progress: Procedure progress 0..1, if a procedure is running.
        active_gates: Names of the currently pending initiation/reading
            gates, if any (see ``cryosoft.core.gates.Gate``).
        conditions: This tick's System-Condition standard registry (see
            `cryosoft.core.conditions.Condition`) — the union of the
            Station's comm/safety/trend conditions and, when a session
            envelope is active, its envelope conditions. Defaults to empty,
            so callers that do not pass it (and old status.jsonl records
            written before this field existed) simply carry no conditions —
            additive and backward-compatible. Every advisory-severity
            condition also contributes one line to the returned record's
            ``alerts`` (see below) — the generic mechanism that makes a
            failing trend check visible in `DiagnosticsWindow` (which
            renders ``alerts``, not ``conditions``) without this module
            knowing anything about trend checks specifically; a
            hold/critical condition is already visible through its own
            enforcement (standby, EMERGENCY) and is not duplicated here.

    Returns:
        ``(record, new_gaps)`` — the JSON-ready record dict and the gap map to
        pass back as ``prev_gaps`` next tick.
    """
    vis: list[dict] = []
    new_gaps: dict[str, float] = {}
    verdict = RunFaultCode.OK

    for vi_name, ramp in ramp_info.items():
        vi_state = state.get(vi_name, {})
        value = ramp.get("value")
        target = ramp.get("target")
        rate = ramp.get("rate")
        ramp_status = ramp.get("ramp_status", "IDLE")
        phase = ramp.get("phase")

        gap: float | None = None
        closing: float | None = None
        eta_s: float | None = None
        if value is not None and target is not None:
            gap = abs(value - target)
            new_gaps[vi_name] = gap
            prev = prev_gaps.get(vi_name)
            if prev is not None:
                closing = prev - gap
            if rate:
                eta_s = gap / (abs(rate) / 60.0)

        code = RunFaultCode.OK
        detail = ""
        if vi_state.get("_disconnected") or ramp.get("_disconnected"):
            code, detail = RunFaultCode.VI_DISCONNECTED, "no response from instrument"
        elif vi_state.get("_stale") or ramp.get("_stale"):
            code, detail = RunFaultCode.VI_STALE, "instrument stopped updating"
        elif vi_state.get("magnet_status") == "QUENCH":
            code, detail = RunFaultCode.QUENCH, "magnet quench detected"

        vis.append(
            VIHealth(
                vi_name=vi_name,
                value=value,
                target=target,
                gap=gap,
                closing=closing,
                rate=rate,
                eta_s=eta_s,
                ramp_status=ramp_status,
                phase=phase,
                code=code,
                detail=detail,
            ).as_dict()
        )
        verdict = _worse(verdict, code)

    sorted_conditions = sorted(conditions, key=lambda c: c.key)
    # Advisory conditions have no enforcement of their own (see
    # cryosoft.core.conditions's severity ladder), so this is their only
    # visible trace short of reading status.jsonl directly — one alerts line
    # per condition, generic over origin (today: only "trend").
    advisory_alerts = [
        f"{c.key}: {c.message}" for c in sorted_conditions if c.severity == "advisory"
    ]

    record = {
        "orch_state": orch_state,
        "elapsed_in_state_s": round(elapsed_in_state_s, 1),
        "wait": (
            {"target_s": round(wait_target_s, 1), "elapsed_s": round(wait_elapsed_s or 0.0, 1)}
            if wait_target_s is not None
            else None
        ),
        "progress": progress,
        "verdict": verdict.value,
        "alerts": advisory_alerts,
        "vis": vis,
        "active_gates": list(active_gates) if active_gates else [],
        "conditions": [_condition_as_dict(c) for c in sorted_conditions],
    }
    return record, new_gaps
