# ---
# description: |
#   Unit tests for the real Keithley6221 driver's error-queue observability
#   and the set_current() autorange fix:
#   - set_current()/set_compliance() must log a queued SCPI error (e.g. -221
#     "Settings conflict") instead of silently swallowing it.
#   - set_current() must unconditionally re-assert :SOUR:CURR:RANG:AUTO ON
#     before :SOUR:CURR, so a range delta mode left fixed (autorange off)
#     earlier in the session can never reject a later DC-mode current.
#   Constructs the driver with a mocked pyvisa instrument handle -- no real
#   hardware -- since both behaviors are invisible to the SimKeithley6221
#   twin (the sim does not model an instrument-side SCPI error queue or a
#   fixed/auto current range).
# entry_point: pytest tests/test_l0_keithley_6221_error_queue.py -v
# last_updated: 2026-07-22
# ---

from unittest.mock import MagicMock

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


def test_set_current_logs_warning_on_scpi_error(caplog):
    """set_current() must log a WARNING when :SYST:ERR? reports a real error."""
    driver = _driver_with_fake_instr(['-221,"Settings conflict"'])
    with caplog.at_level("WARNING"):
        driver.set_current(1e-4)
    assert any("-221" in rec.message for rec in caplog.records)
    assert any("set_current" in rec.message for rec in caplog.records)


def test_set_current_silent_when_error_queue_clean(caplog):
    """set_current() must not log anything when :SYST:ERR? reports no error."""
    driver = _driver_with_fake_instr(['0,"No error"'])
    with caplog.at_level("WARNING"):
        driver.set_current(1e-4)
    assert caplog.records == []


def test_set_compliance_logs_warning_on_scpi_error(caplog):
    """set_compliance() must also surface a queued SCPI error."""
    driver = _driver_with_fake_instr(['-221,"Settings conflict"'])
    with caplog.at_level("WARNING"):
        driver.set_compliance(10.0)
    assert any("-221" in rec.message for rec in caplog.records)
    assert any("set_compliance" in rec.message for rec in caplog.records)


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
