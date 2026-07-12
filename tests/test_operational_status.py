# ---
# description: |
#   Unit tests for the pure operational-status record builder
#   (cryosoft.core.operational_status.build_operational_status). No Qt, no
#   hardware — verifies the record schema, gap/closing/eta computation, JSON
#   serializability, and the unambiguous fault-code verdicts.
# entry_point: pytest tests/test_operational_status.py -v
# last_updated: 2026-07-12
# ---

"""Tests for the operational-status record builder."""

from __future__ import annotations

import json

import pytest

from cryosoft.core.operational_status import RunFaultCode, build_operational_status


def test_normal_ramp_reports_gap_eta_and_ok():
    state = {"magnet_x": {"get_field": 0.5, "magnet_status": "RAMPING"}}
    ramp_info = {
        "magnet_x": {"value": 0.5, "target": 1.0, "rate": 0.5, "ramp_status": "RAMPING"},
    }
    record, gaps = build_operational_status(
        orch_state="RAMPING",
        elapsed_in_state_s=12.0,
        state=state,
        ramp_info=ramp_info,
        prev_gaps={},
    )
    assert record["orch_state"] == "RAMPING"
    assert record["verdict"] == "OK"
    vi = record["vis"][0]
    assert vi["vi_name"] == "magnet_x"
    assert vi["gap"] == pytest.approx(0.5)
    # eta = gap / (rate/60) = 0.5 / (0.5/60) = 60 s
    assert vi["eta_s"] == pytest.approx(60.0)
    assert vi["closing"] is None  # no previous gap
    assert gaps["magnet_x"] == pytest.approx(0.5)


def test_closing_is_gap_decrease_from_prev_tick():
    ramp_info = {"t": {"value": 48.0, "target": 50.0, "rate": 2.0, "ramp_status": "RAMPING"}}
    record, _ = build_operational_status(
        orch_state="RAMPING", elapsed_in_state_s=5.0, state={"t": {}},
        ramp_info=ramp_info, prev_gaps={"t": 3.0},
    )
    vi = record["vis"][0]
    assert vi["gap"] == pytest.approx(2.0)
    assert vi["closing"] == pytest.approx(1.0)  # 3.0 -> 2.0, converging


def test_stale_flag_sets_verdict():
    state = {"t": {"_stale": True}}
    ramp_info = {
        "t": {"value": None, "target": None, "rate": None,
              "ramp_status": "IDLE", "_stale": True},
    }
    record, _ = build_operational_status(
        orch_state="RAMPING", elapsed_in_state_s=1.0, state=state,
        ramp_info=ramp_info, prev_gaps={},
    )
    assert record["verdict"] == RunFaultCode.VI_STALE.value
    assert record["vis"][0]["code"] == "VI_STALE"


def test_disconnected_outranks_stale():
    state = {"t": {"_stale": True, "_disconnected": True}}
    ramp_info = {"t": {"value": None, "target": None, "rate": None, "ramp_status": "IDLE"}}
    record, _ = build_operational_status(
        orch_state="RAMPING", elapsed_in_state_s=1.0, state=state,
        ramp_info=ramp_info, prev_gaps={},
    )
    assert record["verdict"] == "VI_DISCONNECTED"


def test_quench_sets_verdict():
    state = {"magnet_x": {"magnet_status": "QUENCH"}}
    ramp_info = {"magnet_x": {"value": 1.0, "target": 2.0, "rate": 0.5, "ramp_status": "RAMPING"}}
    record, _ = build_operational_status(
        orch_state="RAMPING", elapsed_in_state_s=1.0, state=state,
        ramp_info=ramp_info, prev_gaps={},
    )
    assert record["verdict"] == "QUENCH"


def test_wait_block_present_only_during_wait():
    r1, _ = build_operational_status(
        orch_state="RAMPING", elapsed_in_state_s=1.0, state={}, ramp_info={},
        prev_gaps={}, wait_target_s=30.0, wait_elapsed_s=5.0,
    )
    assert r1["wait"] == {"target_s": 30.0, "elapsed_s": 5.0}
    r2, _ = build_operational_status(
        orch_state="RAMPING", elapsed_in_state_s=1.0, state={}, ramp_info={}, prev_gaps={},
    )
    assert r2["wait"] is None


def test_record_is_json_serializable():
    ramp_info = {"m": {"value": 0.0, "target": 1.0, "rate": 0.5, "ramp_status": "RAMPING"}}
    record, _ = build_operational_status(
        orch_state="RAMPING", elapsed_in_state_s=1.0, state={"m": {}},
        ramp_info=ramp_info, prev_gaps={},
    )
    json.dumps(record)  # must not raise (the record is written to status.jsonl)
