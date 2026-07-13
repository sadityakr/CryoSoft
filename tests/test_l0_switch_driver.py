# ---
# description: |
#   Unit tests for the SimKeithley705 scanner / matrix-switch sim driver: the
#   exclusive-mux state model (closed-channel set), open_all clearing, the
#   closed_channels() inspection helper, get_idn, and error injection.
# entry_point: pytest tests/test_l0_switch_driver.py -v
# last_updated: 2026-07-13
# ---

"""Tests for the SimKeithley705 scanner sim driver (L0)."""

from __future__ import annotations

import pytest

from cryosoft.drivers.sim_keithley_705 import SimKeithley705


def test_initial_state_all_open():
    d = SimKeithley705("SIM")
    assert d.closed_channels() == []


def test_close_channels_records_specs():
    d = SimKeithley705("SIM")
    d.close_channels(["1!1", "1!2"])
    assert d.closed_channels() == ["1!1", "1!2"]


def test_open_channels_removes_specs():
    d = SimKeithley705("SIM")
    d.close_channels(["1!1", "1!2"])
    d.open_channels(["1!1"])
    assert d.closed_channels() == ["1!2"]


def test_open_unknown_spec_is_ignored():
    d = SimKeithley705("SIM")
    d.close_channels(["1!1"])
    d.open_channels(["9!9"])  # not closed — no error
    assert d.closed_channels() == ["1!1"]


def test_open_all_clears_everything():
    d = SimKeithley705("SIM")
    d.close_channels(["1!1", "1!2", "1!3"])
    d.open_all()
    assert d.closed_channels() == []


def test_closed_channels_is_sorted_and_deduplicated():
    d = SimKeithley705("SIM")
    d.close_channels(["1!3", "1!1", "1!1"])
    assert d.closed_channels() == ["1!1", "1!3"]


def test_get_idn_string():
    d = SimKeithley705("SIM")
    idn = d.get_idn()
    assert isinstance(idn, str)
    assert "705" in idn


def test_simulate_error_raises():
    from cryosoft.core.exceptions import CryoSoftCommunicationError

    d = SimKeithley705("SIM")
    d._simulate_error = True
    with pytest.raises(CryoSoftCommunicationError):
        d.get_idn()
    with pytest.raises(CryoSoftCommunicationError):
        d.closed_channels()
