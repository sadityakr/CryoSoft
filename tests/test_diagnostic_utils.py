# ---
# description: |
#   Regression tests for the CryoSoft diagnostic utilities used by .claude/skills.
#   Verifies Oxford instrument discovery when generic *IDN? detection fails.
# entry_point: pytest tests/test_diagnostic_utils.py -q
# last_updated: 2026-07-16
# ---

from __future__ import annotations

from pathlib import Path
import sys

import pytest

# Allow importing the diagnostic utilities from the .claude skills folder.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / ".claude" / "skills" / "diagnose-connections"))

import diagnostic_utils


class DummyPortInfo:
    def __init__(self, device: str, description: str) -> None:
        self.device = device
        self.description = description


class FakeSerial:
    def __init__(self, port: str, timeout: float = 0.5) -> None:
        self.port = port
        self.timeout = timeout
        self.written = b""

    def write(self, data: bytes) -> int:
        self.written = data
        return len(data)

    def read(self, size: int = 1) -> bytes:
        return b""

    def close(self) -> None:
        pass


def test_discover_oxford_instrument_returns_itc503(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_query_oxford_instrument(port: str, instrument_type: str, command: str, baudrate: int, timeout_ms: int) -> dict:
        if instrument_type == "ITC503":
            return {
                "success": True,
                "response": "ITC503 Version 1.11 (c) OXFORD 1997",
                "parsed": "ITC503 Version 1.11 (c) OXFORD 1997",
            }
        return {"success": False, "response": None, "parsed": None, "error": "No response"}

    monkeypatch.setattr(diagnostic_utils, "query_oxford_instrument", fake_query_oxford_instrument)

    result = diagnostic_utils.discover_oxford_instrument("COM12", timeout_ms=100)

    assert result is not None
    assert result["instrument_type"] == "ITC503"
    assert "ITC503 Version" in result["parsed"]


def test_enumerate_serial_ports_falls_back_to_oxford_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "serial.tools.list_ports.comports",
        lambda: [DummyPortInfo("COM12", "Prolific USB-to-Serial Comm Port (COM12)")],
    )
    monkeypatch.setattr("serial.Serial", FakeSerial)
    monkeypatch.setattr(
        diagnostic_utils,
        "discover_oxford_instrument",
        lambda port, baudrate=9600, timeout_ms=100: {
            "instrument_type": "ITC503",
            "response": "ITC503 Version 1.11 (c) OXFORD 1997",
            "parsed": "ITC503 Version 1.11 (c) OXFORD 1997",
        },
    )

    ports = diagnostic_utils.enumerate_serial_ports(test_query=True, timeout_ms=100)

    assert len(ports) == 1
    assert ports[0]["port"] == "COM12"
    assert ports[0]["available"] is True
    assert ports[0]["device_type"] == "ITC503"
    assert "ITC503 Version" in ports[0]["device"]
    assert ports[0]["error"] is None
