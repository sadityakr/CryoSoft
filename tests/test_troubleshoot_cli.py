# ---
# description: |
#   Behavior tests for the troubleshoot CLI: subcommand grammar, --json output,
#   exit codes (0 all-OK / 1 any-fault), config resolution by name and path,
#   the read/write permission split at the CLI surface, and the JSONL
#   transcript. All in-process via cli.main(argv); the VISA bus and transcript
#   directory are monkeypatched.
# last_updated: 2026-07-12
# ---

"""Troubleshoot CLI tests — the command grammar and exit codes are API for skills."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cryosoft.troubleshoot import cli
from tests.test_troubleshoot_engine import (
    _THIS,
    FakeInstrument,
    FakeResourceManager,
    make_config,
)


@pytest.fixture(autouse=True)
def isolated_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send every invocation's transcript into the test's tmp dir."""
    transcript_dir = tmp_path / "logs"
    monkeypatch.setattr(cli, "_transcript_dir", lambda: transcript_dir)
    return transcript_dir


@pytest.fixture()
def fake_bus(monkeypatch: pytest.MonkeyPatch) -> FakeResourceManager:
    rm = FakeResourceManager(
        {
            "GPIB0::7::INSTR": FakeInstrument(reply="KEITHLEY,2182A,123,C01"),
            "ASRL10::INSTR": FakeInstrument(reply="IPS120-10 Version 3.07"),
        }
    )
    monkeypatch.setattr(cli, "_rm_factory", lambda: rm)
    return rm


@pytest.fixture()
def sim_config(tmp_path: Path) -> str:
    return make_config(
        tmp_path / "cfg",
        {
            "meter": {
                "class": "cryosoft.drivers.sim_keithley_2182a.SimKeithley2182A",
                "address": "SIM::CLI",
            }
        },
    )


def _json_out(capsys: pytest.CaptureFixture) -> dict:
    return json.loads(capsys.readouterr().out)


# ── scan / probe ──────────────────────────────────────────────────────────────


def test_scan_lists_resources_json(fake_bus, capsys) -> None:
    assert cli.main(["scan", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["resources"] == ["ASRL10::INSTR", "GPIB0::7::INSTR"]
    assert "probes" not in payload


def test_scan_probe_skips_serial_by_default(fake_bus, capsys) -> None:
    assert cli.main(["scan", "--probe", "--json"]) == 0
    probes = _json_out(capsys)["probes"]
    assert [p["address"] for p in probes] == ["GPIB0::7::INSTR"]
    assert probes[0]["code"] == "OK"


def test_scan_probe_serial_opt_in(fake_bus, capsys) -> None:
    assert cli.main(["scan", "--probe-serial", "--json"]) == 0
    probes = _json_out(capsys)["probes"]
    assert [p["address"] for p in probes] == ["ASRL10::INSTR", "GPIB0::7::INSTR"]


def test_probe_ok_exit_zero(fake_bus, capsys) -> None:
    assert cli.main(["probe", "GPIB0::7::INSTR", "--json"]) == 0
    assert _json_out(capsys)["idn"] == "KEITHLEY,2182A,123,C01"


def test_probe_missing_address_exit_one(fake_bus, capsys) -> None:
    assert cli.main(["probe", "GPIB0::99::INSTR", "--json"]) == 1
    assert _json_out(capsys)["code"] == "OPEN_FAILED"


# ── check ─────────────────────────────────────────────────────────────────────


def test_check_sim_config_green(sim_config, capsys) -> None:
    assert cli.main(["check", "--config", sim_config, "--no-bus", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["ok"] is True
    assert payload["results"][0]["code"] == "OK"


def test_check_failing_driver_exit_one(tmp_path, capsys) -> None:
    config = make_config(
        tmp_path / "bad",
        {"dead": {"class": f"{_THIS}.OpenFailsDriver", "address": "GPIB0::2::INSTR"}},
    )
    assert cli.main(["check", "--config", config, "--no-bus", "--json"]) == 1
    assert _json_out(capsys)["results"][0]["code"] == "OPEN_FAILED"


def test_check_resolves_shipped_config_by_name(capsys) -> None:
    """--config sim_cryostat resolves against the shipped configs folder."""
    assert cli.main(["check", "--config", "sim_cryostat", "--no-bus", "--json"]) == 0
    payload = _json_out(capsys)
    assert payload["ok"] is True
    assert "sim_cryostat" in payload["config"]


def test_unknown_config_name_is_a_clean_error() -> None:
    with pytest.raises(SystemExit):
        cli.main(["check", "--config", "no-such-setup", "--no-bus"])


# ── methods / idn / read / write ──────────────────────────────────────────────


def test_methods_reports_read_write_classification(sim_config, capsys) -> None:
    assert cli.main(["methods", "meter", "--config", sim_config, "--json"]) == 0
    methods = {m["name"]: m for m in _json_out(capsys)["methods"]}
    assert methods["get_voltage"]["read_only"] is True
    assert methods["set_range"]["read_only"] is False


def test_idn_command(sim_config, capsys) -> None:
    assert cli.main(["idn", "meter", "--config", sim_config, "--json"]) == 0
    assert _json_out(capsys)["result"] == "KEITHLEY,2182A,SIM,1.0"


def test_read_calls_getter(sim_config, capsys) -> None:
    assert cli.main(["read", "meter", "get_voltage", "--config", sim_config, "--json"]) == 0
    assert isinstance(_json_out(capsys)["result"], float)


def test_read_refuses_writing_method(sim_config, capsys) -> None:
    """The CLI-level read/write split: 'read' never reaches a set_* method."""
    exit_code = cli.main(
        ["read", "meter", "set_range", "0.1", "--config", sim_config, "--json"]
    )
    assert exit_code == 1
    assert "changes instrument state" in _json_out(capsys)["error"]


def test_write_allows_setter_with_coercion(sim_config, capsys) -> None:
    assert cli.main(
        ["write", "meter", "set_range", "0.1", "--config", sim_config, "--json"]
    ) == 0
    payload = _json_out(capsys)
    assert payload["method"] == "set_range"
    assert payload["args"] == ["0.1"]


def test_adhoc_bench_via_class_and_address(capsys) -> None:
    """TARGET + --address benches a driver with no config entry (driver dev)."""
    assert cli.main(
        ["idn", f"{_THIS}.AlwaysUpDriver", "--address", "GPIB0::30::INSTR", "--json"]
    ) == 0
    assert _json_out(capsys)["result"] == "ACME,MODEL1,SN42,9.9"


class FlakyDriver:
    """Fails every second read — the intermittent/timing fault signature."""

    _calls = 0

    def __init__(self, resource_string: str) -> None:
        type(self)._calls = 0

    def get_value(self) -> float:
        type(self)._calls += 1
        if type(self)._calls % 2 == 0:
            raise OSError("timeout")
        return 1.0


def test_read_repeat_all_ok(sim_config, capsys) -> None:
    exit_code = cli.main(
        ["read", "meter", "get_voltage", "--config", sim_config,
         "--repeat", "3", "--interval", "0", "--json"]
    )
    assert exit_code == 0
    payload = _json_out(capsys)
    assert payload["failures"] == 0
    assert len(payload["outcomes"]) == 3


def test_read_repeat_exposes_intermittency(capsys) -> None:
    exit_code = cli.main(
        ["read", "tests.test_troubleshoot_cli.FlakyDriver", "get_value",
         "--address", "SIM::FLAKY", "--repeat", "4", "--interval", "0", "--json"]
    )
    assert exit_code == 1
    payload = _json_out(capsys)
    assert payload["failures"] == 2
    assert [o["ok"] for o in payload["outcomes"]] == [True, False, True, False]


# ── query / send ──────────────────────────────────────────────────────────────


def test_query_without_raw_handle_is_clean_error(sim_config, capsys) -> None:
    exit_code = cli.main(
        ["query", "meter", "*IDN?", "--config", sim_config, "--json"]
    )
    assert exit_code == 1
    assert "raw VISA handle" in _json_out(capsys)["error"]


# ── transcript ────────────────────────────────────────────────────────────────


def test_transcript_appends_one_jsonl_line_per_invocation(
    sim_config, isolated_transcript: Path, capsys
) -> None:
    cli.main(["idn", "meter", "--config", sim_config, "--json"])
    cli.main(["read", "meter", "set_range", "1", "--config", sim_config, "--json"])
    lines = (
        (isolated_transcript / "troubleshoot.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert first["ok"] is True and first["argv"][0] == "idn"
    assert second["ok"] is False and "error" in second["payload"]
    assert "ts" in first
