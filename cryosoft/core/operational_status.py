# ---
# description: |
#   Operational-status record builder: the runtime "why is the run stuck /
#   taking so long?" signal. A pure function that assembles one machine-readable
#   status record per Orchestrator tick from the already-polled station state
#   and Station.get_ramp_status(). No hardware, no Qt, no I/O — the Orchestrator
#   owns emission and logging; this module owns the data shape and the verdict.
# entry_point: Not run directly; called by the Orchestrator each tick.
# dependencies:
#   - dataclasses, enum (stdlib only)
# input: |
#   build_operational_status() takes the orchestrator state name, elapsed time
#   in that state, the station state snapshot, the ramp-status aggregate, and
#   the previous tick's per-VI gaps (for the closing-rate fact).
# process: |
#   For each system VI it computes gap-to-target, gap closing since last tick,
#   and an ETA, and assigns an unambiguous RunFaultCode (stale / disconnected /
#   quench / ok). Heuristic stall detection is deliberately NOT done here yet.
# output: |
#   A JSON-ready dict record (the schema the troubleshoot layer reads from
#   logs/status.jsonl) plus the new per-VI gap map for the next tick.
# ---

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

from dataclasses import asdict, dataclass
from enum import Enum


class RunFaultCode(str, Enum):
    """Stable, machine-readable runtime health codes.

    ``str, Enum`` makes each member serialize as its plain string value, so a
    record is directly JSON-ready. Only unambiguous conditions are emitted
    today; heuristic codes (a "possibly stuck" ramp) are added with the
    watchdog increment.

    * ``OK``              — ramping/settling normally, or idle.
    * ``VI_STALE``        — the instrument stopped updating (cached values).
    * ``VI_DISCONNECTED`` — repeated comms failures; assumed off the bus.
    * ``QUENCH``          — a magnet reported a quench.
    """

    OK = "OK"
    VI_STALE = "VI_STALE"
    VI_DISCONNECTED = "VI_DISCONNECTED"
    QUENCH = "QUENCH"


# Higher = more severe; the record's overall verdict is the worst VI's code.
_SEVERITY: dict[RunFaultCode, int] = {
    RunFaultCode.OK: 0,
    RunFaultCode.VI_STALE: 2,
    RunFaultCode.QUENCH: 3,
    RunFaultCode.VI_DISCONNECTED: 3,
}


def _worse(a: RunFaultCode, b: RunFaultCode) -> RunFaultCode:
    """Return the more severe of two codes."""
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


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
    code: RunFaultCode
    detail: str = ""

    def as_dict(self) -> dict:
        """Return a JSON-ready plain dict (RunFaultCode becomes its string)."""
        data = asdict(self)
        data["code"] = self.code.value
        return data


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
) -> tuple[dict, dict[str, float]]:
    """Assemble one operational-status record and the next-tick gap map.

    Pure: no hardware, no Qt, no I/O. The caller (Orchestrator) supplies the
    already-polled ``state`` snapshot and ``ramp_info`` (Station.get_ramp_status,
    which carries value/target/rate/ramp_status per system VI) so this does not
    poll anything itself.

    Args:
        orch_state: Orchestrator state name (e.g. ``"RAMPING"``).
        elapsed_in_state_s: Seconds since that state was entered.
        state: The station state snapshot ``{vi_name: {field: value, ...}}``,
            used for the ``_stale`` / ``_disconnected`` flags and magnet quench.
        ramp_info: ``{vi_name: {"value","target","rate","ramp_status", ...}}``.
        prev_gaps: Per-VI gap from the previous tick, for the closing fact.
        wait_target_s / wait_elapsed_s: Settle-wait clock, if in a wait.
        progress: Procedure progress 0..1, if a procedure is running.

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
                code=code,
                detail=detail,
            ).as_dict()
        )
        verdict = _worse(verdict, code)

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
        "vis": vis,
    }
    return record, new_gaps
