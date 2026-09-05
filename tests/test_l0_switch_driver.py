"""Tests for the SimKeithley705 scanner sim driver (L0)."""

from __future__ import annotations

import pytest

from cryosoft.drivers.sim_keithley_705 import SimKeithley705


def test_initial_state_all_open():
    d = SimKeithley705("SIM")
    assert d.closed_channels() == []


def test_close_channels_records_specs():
    d = SimKeithley705("SIM")
    d.close_channels(["1", "2"])
    assert d.closed_channels() == ["001", "002"]


def test_open_channels_removes_specs():
    d = SimKeithley705("SIM")
    d.close_channels(["1", "2"])
    d.open_channels(["1"])
    assert d.closed_channels() == ["002"]


def test_open_unknown_spec_is_ignored():
    d = SimKeithley705("SIM")
    d.close_channels(["1"])
    d.open_channels(["9"])  # not closed — no error
    assert d.closed_channels() == ["001"]


def test_open_all_clears_everything():
    d = SimKeithley705("SIM")
    d.close_channels(["1", "2", "3"])
    d.open_all()
    assert d.closed_channels() == []


def test_closed_channels_is_sorted_and_deduplicated():
    d = SimKeithley705("SIM")
    d.close_channels(["3", "1", "1"])
    assert d.closed_channels() == ["001", "003"]


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


def test_pole_mode_changes_the_addressable_channel_count():
    """Pole mode renumbers the scanner: 1-pole 40, 2-pole 20, 4-pole 10.

    Measured on the 12t-cryo 705. A route table is only meaningful alongside
    the mode it was written for, so the sim must model the count change.
    """
    d = SimKeithley705("SIM")
    for poles, last in ((1, 40), (2, 20), (4, 10)):
        d.set_pole_mode(poles)
        assert d.first_last_channel() == (1, last)


def test_pole_mode_change_reopens_channels():
    """Switching pole mode drops any closed channel — the numbering changed."""
    d = SimKeithley705("SIM")
    d.close_channel("15")
    assert d.closed_channels() == ["015"]
    d.set_pole_mode(4)
    assert d.closed_channels() == []


def test_channel_valid_in_2_pole_is_out_of_range_in_4_pole():
    """Channel 15 exists in 2-pole but not 4-pole, and the driver says so.

    This is the failure a config carried over from 2-pole would hit: the
    instrument discards the close and reports nothing on the bus. Under the
    driver error-reporting standard the 705's verification is explicit
    readback, so the discarded close surfaces as a typed error at the call
    site instead of as an unrouted measurement hours later.
    """
    from cryosoft.core.exceptions import CryoSoftInstrumentError

    d = SimKeithley705("SIM")
    d.set_pole_mode(2)
    d.close_channel("15")
    assert d.closed_channels() == ["015"]
    d.set_pole_mode(4)
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        d.close_channel("15")
    assert excinfo.value.code == "READBACK_MISMATCH"
    assert d.closed_channels() == []


def test_unsupported_pole_mode_rejected():
    from cryosoft.core.exceptions import CryoSoftCommunicationError

    d = SimKeithley705("SIM")
    with pytest.raises(CryoSoftCommunicationError, match="unsupported pole mode"):
        d.set_pole_mode(3)


def test_four_point_mode():
    d = SimKeithley705("SIM")
    assert d._pole_mode == 2  # Default
    d.set_four_point_mode()
    assert d._pole_mode == 4


def test_channel_specs_are_normalised_to_three_digits():
    """"5" and "005" address the same 705 channel (Cnnn wire format)."""
    d = SimKeithley705("SIM")
    d.close_channels(["5"])
    d.open_channels(["005"])
    assert d.closed_channels() == []


def test_out_of_range_channel_is_reported_by_readback():
    """A channel past the installed range never closes — and now says so.

    Models the real 705's worst failure mode: an out-of-range channel raises
    IDDCO on the instrument, which is reported nowhere on the bus, so the
    close is discarded in silence. The driver's readback verification (the
    driver error-reporting standard) is the only thing that can see it, and
    it raises ``READBACK_MISMATCH`` — so a config naming a non-existent
    channel fails here rather than as an unrouted measurement on hardware.
    """
    from cryosoft.core.exceptions import CryoSoftInstrumentError

    d = SimKeithley705("SIM")
    assert d.first_last_channel() == (1, 20)
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        d.close_channels(["21"])
    assert excinfo.value.code == "READBACK_MISMATCH"
    assert d.closed_channels() == []
    with pytest.raises(CryoSoftInstrumentError):
        d.close_channel("99")
    assert d.closed_channels() == []


def test_crosspoint_style_spec_is_rejected():
    """"1!1" is not the 705's format and must fail loudly, not silently."""
    from cryosoft.core.exceptions import CryoSoftCommunicationError

    d = SimKeithley705("SIM")
    with pytest.raises(CryoSoftCommunicationError, match="not a channel number"):
        d.close_channels(["1!1"])


def test_single_channel_close_and_open():
    d = SimKeithley705("SIM")
    d.close_channel("5")
    assert d.closed_channels() == ["005"]
    d.close_channel("12")
    assert d.closed_channels() == ["012"]
    d.open_channel("12")
    assert d.closed_channels() == []
