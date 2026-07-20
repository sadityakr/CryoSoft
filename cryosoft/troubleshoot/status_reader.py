# ---
# description: |
#   Reader and summarizer for the runtime operational-status log
#   (<AppData>/CryoSoft/logs/status.jsonl, written by the Orchestrator each
#   tick). Turns
#   the raw JSONL into a compact digest — current state, per-instrument ramp
#   progress and trend, watchdog alerts — and a plain-English rendering with a
#   triage note per fault code. Consumed by the `troubleshoot status` CLI and by
#   the troubleshoot-runtime skill.
# entry_point: Not run directly; used by cryosoft.troubleshoot.cli and agents.
# dependencies:
#   - json, pathlib (stdlib only)
# input: |
#   read_records() takes the path to status.jsonl and an optional window size.
# process: |
#   Parses the trailing records, folds them into a digest (latest record is
#   authoritative; earlier ones give a per-VI gap trend), and renders text.
# output: |
#   A digest dict and/or a human-readable string. No hardware, no imports from
#   cryosoft.core — it depends only on the log's JSON schema, so the troubleshoot
#   package stays decoupled from the live app (contract C10).
# ---

"""Read and explain the runtime operational-status log.

This is the runtime sibling of ``troubleshoot.engine``'s setup-time checks: it
answers "what is the running measurement doing, and is it stuck?" by reading the
log the Orchestrator writes, never by touching the live app. It depends only on
the JSONL record format, not on ``cryosoft.core``.
"""

from __future__ import annotations

import json
from pathlib import Path

# Plain-English meaning + first thing to check, per runtime fault code. Keyed by
# the string code as it appears in status.jsonl (the log is the contract), so
# this table stays independent of cryosoft.core.operational_status.
CODE_HELP: dict[str, str] = {
    "OK": "Normal — ramping/settling on schedule, or idle.",
    "VI_STALE": (
        "The instrument stopped returning fresh readings (values are cached). "
        "Check its connection and that it is powered and not hung."
    ),
    "VI_DISCONNECTED": (
        "Repeated communication failures — treated as off the bus. Check power, "
        "cabling, and address; run `troubleshoot check` with the app closed."
    ),
    "QUENCH": (
        "A magnet reported a quench. The run should be in EMERGENCY; verify the "
        "magnet state and helium level."
    ),
    "RAMP_STALLED": (
        "A ramp has not moved toward its target for several ticks. The setpoint "
        "is being sent but the value is not following, so suspect a "
        "controller/PID limit, a saturated heater, a thermal load, or the "
        "instrument not accepting setpoints."
    ),
    "STALLED_RUN": (
        "The run is wedged in a step that should be momentary (initiating/"
        "measuring/sweeping). Suspect a procedure step that is not returning or a "
        "measurement instrument that is not responding."
    ),
}


def read_records(log_path: str | Path, last: int | None = None) -> list[dict]:
    """Return parsed JSONL records from status.jsonl (the last *last* if given).

    Missing file → empty list. Unparseable lines are skipped, not fatal.
    """
    path = Path(log_path)
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if last is not None:
        lines = lines[-last:]
    records: list[dict] = []
    for ln in lines:
        try:
            records.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return records


def _trend_word(gaps: list[float]) -> str:
    """Classify a sequence of gaps as closing / widening / flat / unknown."""
    if len(gaps) < 2:
        return "unknown"
    if gaps[-1] < gaps[0] - 1e-9:
        return "closing"
    if gaps[-1] > gaps[0] + 1e-9:
        return "widening"
    return "flat"


def summarize(records: list[dict]) -> dict:
    """Fold a window of records into a digest (latest record is authoritative)."""
    if not records:
        return {"available": False}
    latest = records[-1]
    gaps: dict[str, list[float]] = {}
    for rec in records:
        for vi in rec.get("vis", []):
            g = vi.get("gap")
            if g is not None:
                gaps.setdefault(vi["vi_name"], []).append(g)
    return {
        "available": True,
        "orch_state": latest.get("orch_state"),
        "elapsed_in_state_s": latest.get("elapsed_in_state_s"),
        "verdict": latest.get("verdict"),
        "alerts": latest.get("alerts", []),
        "progress": latest.get("progress"),
        "vis": latest.get("vis", []),
        "trends": {name: _trend_word(g) for name, g in gaps.items()},
        "window": len(records),
    }


def render_text(digest: dict) -> str:
    """Render a digest as a plain-English block for the CLI and for agents."""
    if not digest.get("available"):
        return (
            "No operational-status log found (is the app running? "
            "<AppData>/CryoSoft/logs/status.jsonl)."
        )

    lines: list[str] = []
    lines.append(
        f"State: {digest['orch_state']}  "
        f"({digest['elapsed_in_state_s']}s in state)   Verdict: {digest['verdict']}"
    )
    if digest.get("progress") is not None:
        lines.append(f"Procedure progress: {digest['progress'] * 100:.0f}%")

    if digest["alerts"]:
        lines.append("Alerts:")
        lines.extend(f"  ! {a}" for a in digest["alerts"])

    lines.append("Instruments:")
    for vi in digest["vis"]:
        name = vi["vi_name"]
        trend = digest["trends"].get(name, "")
        gap = vi.get("gap")
        if vi.get("target") is not None and vi.get("value") is not None and gap is not None:
            eta = vi.get("eta_s")
            eta_str = f", ~{eta:.0f}s to target" if eta else ""
            lines.append(
                f"  {name}: {vi['value']:.4g} -> {vi['target']:.4g} "
                f"(gap {gap:.3g}, {vi.get('ramp_status')}, {trend}{eta_str}) [{vi['code']}]"
            )
        else:
            lines.append(f"  {name}: {vi.get('ramp_status')} [{vi['code']}]")

    codes = {vi["code"] for vi in digest["vis"]}
    if digest["verdict"] != "OK":
        codes.add(digest["verdict"])
    problem_codes = sorted(c for c in codes if c != "OK")
    if problem_codes:
        lines.append("What the codes mean:")
        lines.extend(f"  {c}: {CODE_HELP.get(c, 'Unknown code.')}" for c in problem_codes)

    return "\n".join(lines)
