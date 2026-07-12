# ---
# description: |
#   Unit tests for the runtime watchdog (cryosoft.core.watchdog). Drives scripted
#   sequences of ticks through build_operational_status + apply_watchdog and
#   asserts exactly when RAMP_STALLED / STALLED_RUN fire and when they must stay
#   quiet (converging ramp, switch-heater warmup phase, long RAMPING state).
# entry_point: pytest tests/test_watchdog.py -v
# last_updated: 2026-07-12
# ---

"""Tests for the deterministic runtime watchdog."""

from __future__ import annotations

from cryosoft.core.operational_status import build_operational_status
from cryosoft.core.watchdog import WatchdogState, apply_watchdog


def _run_ticks(values, target, rate, *, phase=None, ramp_status="RAMPING", config=None):
    """Feed measured values through build + watchdog; return the per-tick records.

    One system VI "m" ramping toward *target*; each entry in *values* is its
    measured value on that tick. prev_gaps and WatchdogState are threaded across
    ticks exactly as the Orchestrator does.
    """
    prev_gaps: dict[str, float] = {}
    wd = WatchdogState()
    records = []
    for v in values:
        ramp_info = {
            "m": {"value": v, "target": target, "rate": rate,
                  "ramp_status": ramp_status, "phase": phase},
        }
        record, prev_gaps = build_operational_status(
            orch_state="RAMPING", elapsed_in_state_s=1.0, state={"m": {}},
            ramp_info=ramp_info, prev_gaps=prev_gaps,
        )
        record, wd = apply_watchdog(record, wd, config)
        records.append(record)
    return records


def test_converging_ramp_never_stalls():
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]  # gap shrinks each tick
    records = _run_ticks(values, target=10.0, rate=1.0)
    assert all(r["verdict"] == "OK" for r in records)
    assert all(not r["alerts"] for r in records)


def test_flat_ramp_stalls_after_threshold():
    values = [5.0] * 9  # gap frozen: tick0 has no delta, then 8 non-closing ticks
    records = _run_ticks(values, target=10.0, rate=1.0)
    # Below threshold (default stall_ticks=6): still quiet at the 4th record.
    assert records[3]["verdict"] == "OK"
    assert not records[3]["alerts"]
    # Past threshold: stalled, with a per-VI code and a human alert.
    assert records[-1]["verdict"] == "RAMP_STALLED"
    stalled = records[-1]["vis"][0]
    assert stalled["code"] == "RAMP_STALLED"
    assert "not closing" in stalled["detail"]
    assert any("stalled" in a for a in records[-1]["alerts"])


def test_warmup_phase_never_stalls():
    values = [5.0] * 12  # frozen gap, but the field is meant to hold during warmup
    records = _run_ticks(values, target=10.0, rate=1.0, phase="warmup")
    assert all(r["verdict"] == "OK" for r in records)
    assert all(not r["alerts"] for r in records)


def test_reached_ramp_not_flagged():
    values = [10.0] * 12  # at target, not RAMPING
    records = _run_ticks(values, target=10.0, rate=1.0, ramp_status="TARGET_REACHED")
    assert all(r["verdict"] == "OK" for r in records)


def test_progress_resets_the_stall_counter():
    # Flat for a few ticks, then one real step of progress, then flat again —
    # the counter resets, so it never reaches the threshold.
    values = [5.0, 5.0, 5.0, 5.0, 6.0, 6.0, 6.0, 6.0]
    records = _run_ticks(values, target=10.0, rate=1.0)
    assert all(r["verdict"] == "OK" for r in records)


def test_transient_state_dwell_flags_stalled_run():
    record = {"orch_state": "MEASURING", "elapsed_in_state_s": 45.0,
              "verdict": "OK", "alerts": [], "vis": []}
    record, _ = apply_watchdog(record, WatchdogState())
    assert record["verdict"] == "STALLED_RUN"
    assert any("wedged" in a for a in record["alerts"])


def test_transient_state_ok_within_budget():
    record = {"orch_state": "MEASURING", "elapsed_in_state_s": 5.0,
              "verdict": "OK", "alerts": [], "vis": []}
    record, _ = apply_watchdog(record, WatchdogState())
    assert record["verdict"] == "OK"
    assert not record["alerts"]


def test_long_ramping_state_is_not_a_wedge():
    # RAMPING may legitimately last a long time (slow ramp); only transient
    # states count as wedged. Progress there is judged per-VI, not by dwell.
    record = {"orch_state": "RAMPING", "elapsed_in_state_s": 9999.0,
              "verdict": "OK", "alerts": [], "vis": []}
    record, _ = apply_watchdog(record, WatchdogState())
    assert record["verdict"] == "OK"
    assert not record["alerts"]
