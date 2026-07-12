# ---
# description: |
#   Runtime watchdog: deterministic stall detection layered on top of an
#   operational-status record. Pure rules (compare + count), no AI. Detects a
#   ramp that has stopped closing its gap for several consecutive ticks
#   (RAMP_STALLED) and a run wedged in a transient state far too long
#   (STALLED_RUN), and writes plain-English alerts into the record.
# entry_point: Not run directly; called by the Orchestrator after each record.
# dependencies:
#   - dataclasses (stdlib)
#   - cryosoft.core.operational_status (RunFaultCode, worst_code)
# input: |
#   apply_watchdog() takes the record from build_operational_status(), the
#   WatchdogState carried across ticks (per-VI non-closing tick counts), and an
#   optional WatchdogConfig of thresholds.
# process: |
#   Per system VI actually ramping (and not in a persistent-magnet no-motion
#   phase), counts consecutive ticks whose gap did not shrink by more than a
#   noise floor; flags RAMP_STALLED once that count crosses stall_ticks. Also
#   flags a transient orchestrator state that has lasted beyond transient_max_s.
# output: |
#   The same record with per-VI codes upgraded, an "alerts" list of
#   human-readable strings, and the overall verdict recomputed; plus the new
#   WatchdogState for the next tick.
# ---

"""Runtime watchdog — deterministic ramp/run stall detection.

The record from ``build_operational_status`` carries the *facts* (gap, closing,
elapsed). This layer makes the *judgement*: has a ramp stopped making progress,
or has the run wedged? It is pure arithmetic over the record plus a small
carried counter, so it is fully unit-testable — a scripted sequence of ticks
can assert the alert fires at exactly the right tick and stays quiet through a
normal switch-heater warmup.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cryosoft.core.operational_status import RunFaultCode, worst_code

# Persistent-magnet sub-phases where the field deliberately holds still — the
# watchdog must not read these expected pauses as a stall.
_NO_MOTION_PHASES = frozenset({"matching", "warmup", "cooldown", "parking"})

# States meant to last a single tick; sitting in one means the run is wedged.
# RAMPING is intentionally NOT here — a slow ramp is normal and is judged by the
# per-VI gap-closing check instead.
_TRANSIENT_STATES = frozenset({"INITIATING", "MEASURING", "SWEEPING"})


@dataclass
class WatchdogConfig:
    """Tunable thresholds. Defaults are deliberately lenient (quiet beats eager).

    A watchdog that cries wolf gets ignored, so v1 favours late detection over
    false alarms; tighten these against real runs once "that one was stuck" /
    "that one was fine" feedback exists.
    """

    noise_floor: float = 1e-3       # gap must shrink by more than this to count as progress
    stall_ticks: int = 6            # consecutive non-closing ticks before RAMP_STALLED
    transient_max_s: float = 30.0   # a transient state lasting longer than this is wedged


@dataclass
class WatchdogState:
    """State carried across ticks: per-VI consecutive non-closing tick counts."""

    stuck_ticks: dict[str, int] = field(default_factory=dict)


def apply_watchdog(
    record: dict,
    state: WatchdogState,
    config: WatchdogConfig | None = None,
) -> tuple[dict, WatchdogState]:
    """Layer heuristic stall verdicts onto an operational-status record.

    Pure over ``record`` (mutated in place — it is freshly built each tick) and
    the carried ``state``. Reads no hardware and no clock of its own.

    Args:
        record: The dict from ``build_operational_status``.
        state: Per-VI non-closing tick counts from the previous tick.
        config: Thresholds; defaults if omitted.

    Returns:
        ``(record, new_state)`` — the record with per-VI codes upgraded to
        RAMP_STALLED where warranted, an ``"alerts"`` list, and the overall
        ``"verdict"`` recomputed; and the WatchdogState for the next tick.
    """
    config = config or WatchdogConfig()
    new_stuck = dict(state.stuck_ticks)
    alerts: list[str] = list(record.get("alerts", []))
    orch_state = record.get("orch_state", "")
    elapsed = record.get("elapsed_in_state_s", 0.0)

    for vi in record.get("vis", []):
        name = vi["vi_name"]
        ramping = vi.get("ramp_status") == "RAMPING"
        phase = vi.get("phase")
        if not ramping or phase in _NO_MOTION_PHASES:
            # Not ramping, or in an expected no-motion phase: reset, don't judge.
            new_stuck[name] = 0
            continue

        closing = vi.get("closing")
        if closing is None:
            # First tick with a gap — no delta yet, so nothing to judge.
            new_stuck.setdefault(name, 0)
            continue

        if closing > config.noise_floor:
            new_stuck[name] = 0                       # meaningful progress
        else:
            new_stuck[name] = new_stuck.get(name, 0) + 1

        if new_stuck[name] >= config.stall_ticks and vi.get("code") == RunFaultCode.OK.value:
            vi["code"] = RunFaultCode.RAMP_STALLED.value
            gap = vi.get("gap")
            gap_str = f"{gap:.3g}" if gap is not None else "?"
            vi["detail"] = f"gap {gap_str} not closing for {new_stuck[name]} ticks"
            alerts.append(f"{name}: ramp stalled ({vi['detail']})")

    run_codes: list[str] = []
    if orch_state in _TRANSIENT_STATES and elapsed > config.transient_max_s:
        run_codes.append(RunFaultCode.STALLED_RUN.value)
        alerts.append(f"run wedged in {orch_state} for {elapsed:.0f}s")

    codes = [vi.get("code", RunFaultCode.OK.value) for vi in record.get("vis", [])] + run_codes
    record["verdict"] = worst_code(codes)
    record["alerts"] = alerts
    return record, WatchdogState(stuck_ticks=new_stuck)
