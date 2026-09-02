from unittest.mock import MagicMock

import pytest

from cryosoft.core.exceptions import CryoSoftInstrumentError
from cryosoft.drivers.keithley_6221 import Keithley6221


def _driver_with_fake_instr(query_side_effect):
    """Construct a Keithley6221 with __init__'s VISA open bypassed.

    Args:
        query_side_effect: Iterable of return values for the fake
            instrument's query() calls (drives what :SYST:ERR? returns).

    Returns:
        The driver, ready to call set_current()/set_compliance() on.
    """
    driver = object.__new__(Keithley6221)
    fake_instr = MagicMock()
    fake_instr.query.side_effect = query_side_effect
    driver._instr = fake_instr
    return driver


def test_set_current_raises_typed_error_on_scpi_error():
    """set_current() must raise the typed error when :SYST:ERR? reports one.

    The driver error-reporting standard: a queued refusal is a fact the
    caller has to see, because the source did NOT take the value and every
    reading taken afterwards is fiction. The instrument's own code and
    message ride on the exception rather than being flattened into prose.
    """
    driver = _driver_with_fake_instr(['-221,"Settings conflict"'])
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.set_current(1e-4)
    assert excinfo.value.code == "-221"
    assert excinfo.value.instrument_message == "Settings conflict"
    assert "set_current" in excinfo.value.context


def test_set_current_silent_when_error_queue_clean(caplog):
    """set_current() must not log anything when :SYST:ERR? reports no error."""
    driver = _driver_with_fake_instr(['0,"No error"'])
    with caplog.at_level("WARNING"):
        driver.set_current(1e-4)
    assert caplog.records == []


def test_set_compliance_raises_typed_error_on_scpi_error():
    """set_compliance() must also surface a queued SCPI error as the typed error."""
    driver = _driver_with_fake_instr(['-221,"Settings conflict"'])
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.set_compliance(10.0)
    assert excinfo.value.code == "-221"
    assert "set_compliance" in excinfo.value.context


def test_set_compliance_silent_when_error_queue_clean(caplog):
    """set_compliance() must not log anything when the error queue is clean."""
    driver = _driver_with_fake_instr(['0,"No error"'])
    with caplog.at_level("WARNING"):
        driver.set_compliance(10.0)
    assert caplog.records == []


def test_set_current_reasserts_autorange_before_setting_current():
    """set_current() must send :SOUR:CURR:RANG:AUTO ON before :SOUR:CURR.

    Live commissioning (2026-07-22) found delta mode leaves the 6221's
    current range FIXED (autorange off) at whatever range fit its
    configured high-current, with nothing to undo it afterward — a later
    DC-mode set_current() at a larger magnitude was then rejected outright
    (-221 "Settings conflict") on every single call, confirmed on real
    hardware: :SOUR:CURR:RANG:AUTO? read back 0 (off), fixed at the 2 uA
    range, while the failing calls tried to source 100 uA. Forcing
    :SOUR:CURR:RANG:AUTO ON on real hardware immediately resolved it. Pins
    the fix: autorange must be unconditionally reasserted every call, the
    same defense-in-depth already applied to :SOUR:SWE:ABOR.
    """
    driver = _driver_with_fake_instr(['0,"No error"'])
    driver.set_current(1e-4)

    written = [call.args[0] for call in driver._instr.write.call_args_list]
    assert ":SOUR:CURR:RANG:AUTO ON" in written
    autorange_idx = written.index(":SOUR:CURR:RANG:AUTO ON")
    curr_idx = next(i for i, cmd in enumerate(written) if cmd.startswith(":SOUR:CURR "))
    assert autorange_idx < curr_idx, (
        "RANG:AUTO ON must be sent before the :SOUR:CURR value write, else "
        "the value write can still be rejected by a leftover fixed range"
    )
