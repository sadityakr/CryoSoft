# ---
# description: |
#   Behavioural tests for SwitchMatrixVI (L1): route-table validation failures
#   at construction, exclusive-mux select_route (verified via the sim driver's
#   closed_channels()), settle-time honoured, get_state() fields (active_route /
#   active_route_index), routes() order, open_all, and standby.
# entry_point: pytest tests/test_l1_switch_vi.py -v
# last_updated: 2026-07-13
# ---

"""Tests for SwitchMatrixVI (L1)."""

from __future__ import annotations

import pytest

from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.drivers.sim_keithley_705 import SimKeithley705
from cryosoft.virtual_instruments.switch.switch_matrix import SwitchMatrixVI

ROUTES = {
    "Mux-Ch1": ["1"],
    "Mux-Ch2": ["2"],
    "Mux-Ch3": ["3", "4"],
}


def _vi(routes=None, settle_time_s=0.0, **extra):
    driver = SimKeithley705("SIM")
    vi = SwitchMatrixVI(
        {"main": driver},
        routes=ROUTES if routes is None else routes,
        settle_time_s=settle_time_s,
        **extra,
    )
    vi.vi_name = "switch_matrix"
    return vi, driver


# ── Pole mode (a config-owned wiring property) ───────────────────────────────

def test_pole_mode_from_config_is_applied_to_the_instrument():
    """pole_mode reaches the driver at construction, before any route runs.

    The mode renumbers the scanner's channels, so it has to be applied before
    the route table means anything.
    """
    vi, driver = _vi(pole_mode=4)
    assert driver._pole_mode == 4
    assert driver.first_last_channel() == (1, 10)


def test_pole_mode_omitted_leaves_the_instrument_alone():
    """No pole_mode in config -> the driver's mode is untouched."""
    _, driver = _vi()
    assert driver._pole_mode == 2  # the sim's power-up default


def test_invalid_pole_mode_rejected():
    with pytest.raises(CryoSoftConfigError, match="pole_mode"):
        _vi(pole_mode=3)


def test_non_integer_pole_mode_rejected():
    with pytest.raises(CryoSoftConfigError, match="pole_mode"):
        _vi(pole_mode="4")


# ── Construction / validation ────────────────────────────────────────────────

def test_vi_type_and_routes_order():
    vi, _ = _vi()
    assert vi.vi_type == "switch"
    assert vi.routes() == ["Mux-Ch1", "Mux-Ch2", "Mux-Ch3"]


def test_empty_route_name_rejected():
    with pytest.raises(CryoSoftConfigError, match="non-empty"):
        _vi(routes={"": ["1"]})


def test_route_name_with_separator_rejected():
    with pytest.raises(CryoSoftConfigError, match="__"):
        _vi(routes={"a__b": ["1"]})


def test_route_name_with_slash_rejected():
    with pytest.raises(CryoSoftConfigError, match="/"):
        _vi(routes={"a/b": ["1"]})


def test_empty_channel_spec_list_rejected():
    with pytest.raises(CryoSoftConfigError, match="non-empty"):
        _vi(routes={"Mux-Ch1": []})


def test_negative_settle_time_rejected():
    with pytest.raises(CryoSoftConfigError, match="settle_time_s"):
        _vi(settle_time_s=-1.0)


def test_bool_settle_time_rejected():
    with pytest.raises(CryoSoftConfigError, match="settle_time_s"):
        _vi(settle_time_s=True)


# ── Exclusive-mux selection ──────────────────────────────────────────────────

def test_select_route_closes_only_that_route():
    vi, driver = _vi()
    vi.select_route("Mux-Ch1")
    assert driver.closed_channels() == ["001"]
    assert vi.active_route() == "Mux-Ch1"


def test_select_route_is_exclusive():
    """Selecting a second route opens everything first (only its channels close)."""
    vi, driver = _vi()
    vi.select_route("Mux-Ch1")
    vi.select_route("Mux-Ch3")
    assert driver.closed_channels() == ["003", "004"]  # Mux-Ch1's channel 1 is open again


def test_select_unknown_route_raises():
    vi, _ = _vi()
    with pytest.raises(ValueError, match="unknown route"):
        vi.select_route("Mux-Ch9")


def test_select_route_is_control():
    vi, _ = _vi()
    assert getattr(vi.select_route, "_is_control", False) is True


def test_settle_time_honoured(monkeypatch):
    vi, _ = _vi(settle_time_s=0.25)
    slept = {}
    import cryosoft.virtual_instruments.switch.switch_matrix as mod

    monkeypatch.setattr(mod.time, "sleep", lambda s: slept.setdefault("s", s))
    vi.select_route("Mux-Ch1")
    assert slept["s"] == pytest.approx(0.25)


# ── State / open_all / standby ───────────────────────────────────────────────

def test_get_state_reports_active_route_fields():
    vi, _ = _vi()
    state = vi.get_state()
    assert state["active_route"] == ""
    assert state["active_route_index"] == -1

    vi.select_route("Mux-Ch2")
    state = vi.get_state()
    assert state["active_route"] == "Mux-Ch2"
    assert state["active_route_index"] == 1


def test_open_all_clears_active_route():
    vi, driver = _vi()
    vi.select_route("Mux-Ch1")
    vi.open_all()
    assert driver.closed_channels() == []
    assert vi.active_route() == ""
    assert vi.active_route_index() == -1


def test_standby_opens_all():
    vi, driver = _vi()
    vi.select_route("Mux-Ch2")
    vi.standby()
    assert driver.closed_channels() == []
    assert vi.active_route() == ""


def test_ping_true_on_reachable_driver():
    vi, _ = _vi()
    assert vi.ping() is True


def test_evaluate_safety_default_empty():
    vi, _ = _vi()
    assert vi.evaluate_safety(vi.get_state()) == {}
