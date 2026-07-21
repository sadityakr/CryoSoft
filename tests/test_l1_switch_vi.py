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


# ── Hot / cold switching order ───────────────────────────────────────────────

def test_hot_switching_is_on_by_default():
    vi, _ = _vi()
    assert vi.hot_switching_enabled() is True


def test_invalid_hot_switching_rejected():
    with pytest.raises(CryoSoftConfigError, match="hot_switching"):
        _vi(hot_switching="yes")


def _order_spy(driver, calls):
    """Record close_channels/open_channels/open_all call order on *driver*."""
    orig_close = driver.close_channels
    orig_open = driver.open_channels
    orig_open_all = driver.open_all

    def close(specs):
        calls.append(("close", tuple(specs)))
        return orig_close(specs)

    def open_(specs):
        calls.append(("open", tuple(specs)))
        return orig_open(specs)

    def open_all():
        calls.append(("open_all",))
        return orig_open_all()

    driver.close_channels = close
    driver.open_channels = open_
    driver.open_all = open_all


def test_hot_switching_closes_new_before_opening_old():
    vi, driver = _vi()  # hot_switching defaults True
    vi.select_route("Mux-Ch1")
    calls = []
    _order_spy(driver, calls)
    vi.select_route("Mux-Ch3")
    assert calls[0] == ("close", ("3", "4"))
    assert calls[1] == ("open", ("001",))
    assert driver.closed_channels() == ["003", "004"]


def test_cold_switching_opens_old_before_closing_new():
    vi, driver = _vi(hot_switching=False)
    vi.select_route("Mux-Ch1")
    calls = []
    _order_spy(driver, calls)
    vi.select_route("Mux-Ch3")
    assert calls[0] == ("open_all",)
    assert calls[1] == ("close", ("3", "4"))
    assert driver.closed_channels() == ["003", "004"]


def test_hot_switching_never_opens_when_nothing_was_closed():
    """First-ever connect: nothing stale, so no open call is made at all."""
    vi, driver = _vi()
    calls = []
    _order_spy(driver, calls)
    vi.select_route("Mux-Ch1")
    assert calls == [("close", ("1",))]


def test_hot_switching_tolerates_padding_when_channel_is_unchanged():
    """Re-selecting the same route in hot mode must not toggle the channel."""
    vi, driver = _vi()
    vi.select_route("Mux-Ch1")
    calls = []
    _order_spy(driver, calls)
    vi.select_route("Mux-Ch1")
    assert calls == [("close", ("1",))]  # no spurious open of the same channel


def test_set_hot_switching_toggles_at_runtime():
    vi, driver = _vi()
    assert vi.hot_switching_enabled() is True
    vi.set_hot_switching(False)
    assert vi.hot_switching_enabled() is False
    vi.select_route("Mux-Ch1")
    calls = []
    _order_spy(driver, calls)
    vi.select_route("Mux-Ch3")
    assert calls[0] == ("open_all",)


def test_set_hot_switching_is_control():
    vi, _ = _vi()
    assert getattr(vi.set_hot_switching, "_is_control", False) is True


# ── Manual raw-channel control (close_channel / open_channel) ───────────────

def test_close_channel_connects_by_raw_number_bypassing_routes():
    vi, driver = _vi()
    vi.close_channel("5")
    assert driver.closed_channels() == ["005"]
    assert vi.active_route() == ""  # not a named route


def test_close_channel_is_exclusive():
    vi, driver = _vi()
    vi.close_channel("5")
    vi.close_channel("7")
    assert driver.closed_channels() == ["007"]


def test_close_channel_clears_a_previously_active_route():
    vi, driver = _vi()
    vi.select_route("Mux-Ch1")
    vi.close_channel("5")
    assert vi.active_route() == ""
    assert driver.closed_channels() == ["005"]


def test_open_channel_opens_only_that_channel():
    vi, driver = _vi()
    vi.close_channel("5")
    driver.close_channels(["7"])  # simulate a second channel closed out of band
    vi.open_channel("5")
    assert driver.closed_channels() == ["007"]


def test_open_channel_clears_active_route():
    vi, driver = _vi()
    vi.select_route("Mux-Ch1")
    vi.open_channel("1")
    assert vi.active_route() == ""


def test_close_channel_and_open_channel_are_controls():
    vi, _ = _vi()
    assert getattr(vi.close_channel, "_is_control", False) is True
    assert getattr(vi.open_channel, "_is_control", False) is True


def test_active_channels_reflects_driver_readback():
    vi, _ = _vi()
    assert vi.active_channels() == ""
    vi.close_channel("5")
    assert vi.active_channels() == "005"


# ── Pole mode (runtime-settable) ─────────────────────────────────────────────

def test_pole_mode_monitored_reflects_config_value():
    vi, _ = _vi(pole_mode=4)
    assert vi.pole_mode() == 4


def test_pole_mode_monitored_is_zero_when_never_set():
    vi, _ = _vi()
    assert vi.pole_mode() == 0


def test_set_pole_mode_opens_everything_first():
    vi, driver = _vi(pole_mode=4)
    vi.select_route("Mux-Ch1")
    assert driver.closed_channels() == ["001"]
    vi.set_pole_mode(2)
    assert driver.closed_channels() == []
    assert vi.active_route() == ""
    assert vi.pole_mode() == 2
    assert driver._pole_mode == 2


def test_set_pole_mode_rejects_invalid_value():
    vi, _ = _vi()
    with pytest.raises(ValueError, match="poles"):
        vi.set_pole_mode(3)


def test_set_pole_mode_is_control_and_off_panel():
    from cryosoft.core.decorators import get_control_panel

    vi, _ = _vi()
    assert getattr(vi.set_pole_mode, "_is_control", False) is True
    assert get_control_panel(vi.set_pole_mode) is False


def test_select_route_and_close_channel_stay_on_panel():
    from cryosoft.core.decorators import get_control_panel

    vi, _ = _vi()
    assert get_control_panel(vi.select_route) is True
    assert get_control_panel(vi.close_channel) is True
    assert get_control_panel(vi.open_channel) is True
    assert get_control_panel(vi.set_hot_switching) is True
