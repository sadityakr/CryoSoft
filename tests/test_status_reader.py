# ---
# description: |
#   Tests for the runtime status reader (cryosoft.troubleshoot.status_reader)
#   and the `troubleshoot status` CLI subcommand: record parsing, digest/trend
#   folding, plain-English rendering with per-code triage help, and CLI exit
#   codes (0 only when a log exists and the verdict is OK).
# entry_point: pytest tests/test_status_reader.py -v
# last_updated: 2026-07-13
# ---

"""Tests for the runtime status reader and `troubleshoot status`."""

from __future__ import annotations

import json

from cryosoft.troubleshoot import status_reader
from cryosoft.troubleshoot.cli import main


def _write_log(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _rec(state, verdict, vis, alerts=None):
    return {
        "orch_state": state, "elapsed_in_state_s": 5.0, "verdict": verdict,
        "alerts": alerts or [], "progress": None, "vis": vis,
    }


def _vi(name, value, target, gap, code, ramp_status="RAMPING"):
    return {
        "vi_name": name, "value": value, "target": target, "gap": gap,
        "rate": 1.0, "eta_s": gap * 60.0, "ramp_status": ramp_status,
        "phase": None, "code": code,
    }


def test_read_records_missing_file(tmp_path):
    assert status_reader.read_records(tmp_path / "nope.jsonl") == []


def test_read_records_last_window(tmp_path):
    p = tmp_path / "status.jsonl"
    _write_log(p, [_rec("IDLE", "OK", []) for _ in range(5)])
    assert len(status_reader.read_records(p)) == 5
    assert len(status_reader.read_records(p, last=2)) == 2


def test_summarize_empty_is_unavailable():
    assert status_reader.summarize([]) == {"available": False}


def test_summarize_reports_closing_trend():
    recs = [
        _rec("RAMPING", "OK", [_vi("m", 8.0, 10.0, 2.0, "OK")]),
        _rec("RAMPING", "OK", [_vi("m", 9.0, 10.0, 1.0, "OK")]),
    ]
    d = status_reader.summarize(recs)
    assert d["available"] is True
    assert d["orch_state"] == "RAMPING"
    assert d["trends"]["m"] == "closing"


def test_render_stalled_shows_alert_and_code_help():
    rec = _rec(
        "RAMPING", "RAMP_STALLED", [_vi("temp", 48.0, 50.0, 2.0, "RAMP_STALLED")],
        alerts=["temp: ramp stalled (gap 2 not closing for 6 ticks)"],
    )
    text = status_reader.render_text(status_reader.summarize([rec]))
    assert "RAMP_STALLED" in text
    assert "ramp stalled" in text
    assert "not following" in text  # from CODE_HELP's RAMP_STALLED triage note


def test_render_no_log_message():
    assert "No operational-status log" in status_reader.render_text({"available": False})


def test_cli_status_ok_exits_zero(tmp_path, capsys):
    p = tmp_path / "status.jsonl"
    _write_log(p, [_rec("IDLE", "OK", [])])
    rc = main(["status", "--log", str(p), "--json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["verdict"] == "OK"


def test_cli_status_stalled_exits_one(tmp_path):
    p = tmp_path / "status.jsonl"
    _write_log(p, [_rec("RAMPING", "RAMP_STALLED",
                        [_vi("m", 1.0, 5.0, 4.0, "RAMP_STALLED")],
                        alerts=["m: ramp stalled"])])
    assert main(["status", "--log", str(p)]) == 1


def test_cli_status_missing_log_exits_one(tmp_path):
    assert main(["status", "--log", str(tmp_path / "none.jsonl")]) == 1
