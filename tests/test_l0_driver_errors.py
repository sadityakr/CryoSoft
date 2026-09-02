"""L0 tests for the driver error-reporting standard (see ``drivers/README.md``).

One pair of tests per instrument that has a real driver:

* the **real driver** is given a transport whose replies say "I refused
  that", and must raise ``CryoSoftInstrumentError`` carrying the
  instrument's own code; and
* the **sim twin** is driven through the wrong command sequence and must
  raise the same typed error with the same code, so the mistake fails here
  instead of on hardware.

The real drivers are exercised with their VISA session replaced by a mock —
``object.__new__`` skips ``__init__``'s bus open, exactly as
``test_l0_keithley_6221_error_queue.py`` already does. No test in this file
opens a resource; the bench counterparts carry the ``hardware`` marker and
live in ``tests/test_bench_hardware.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cryosoft.core.exceptions import (
    CryoSoftCommunicationError,
    CryoSoftInstrumentError,
)

# ── Keithley 6221 — SCPI error queue ──────────────────────────────────────────


def _fake_visa_driver(cls, query_replies):
    """Build a real driver with its VISA session mocked and __init__ skipped.

    Args:
        cls: The real driver class.
        query_replies: Iterable of replies the fake session's ``query()``
            returns, in order.

    Returns:
        The driver instance, ready for the method under test.
    """
    driver = object.__new__(cls)
    instr = MagicMock()
    instr.query.side_effect = query_replies
    instr.timeout = 5_000
    driver._instr = instr
    return driver


def test_keithley_6221_real_raises_typed_error_from_the_scpi_queue():
    """A queued ``-221`` after an output write becomes the typed error."""
    from cryosoft.drivers.keithley_6221 import Keithley6221

    driver = _fake_visa_driver(Keithley6221, ['-221,"Settings conflict"'])
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.set_source_enabled(True)
    assert excinfo.value.code == "-221"
    assert excinfo.value.instrument_message == "Settings conflict"


def test_keithley_6221_real_unparseable_queue_reply_is_not_read_as_clean():
    """Junk on the error queue is reported, never assumed to mean "no error"."""
    from cryosoft.drivers.keithley_6221 import Keithley6221

    driver = _fake_visa_driver(Keithley6221, ["<garbage>"])
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.set_source_enabled(True)
    assert excinfo.value.code == ""
    assert "<garbage>" in excinfo.value.instrument_message


def test_keithley_6221_real_clean_queue_raises_nothing():
    """``0,"No error"`` is the one reply that means the write landed."""
    from cryosoft.drivers.keithley_6221 import Keithley6221

    driver = _fake_visa_driver(Keithley6221, ['0,"No error"'])
    driver.set_source_enabled(True)


def test_sim_keithley_6221_refuses_output_switch_while_delta_is_armed():
    """The wrong sequence: switch the output by hand with delta armed.

    The real driver never does this — ``stop_delta_mode()`` sends
    ``:SOUR:SWE:ABOR`` *before* ``OUTP OFF`` — so a caller that skips the
    abort is exactly what this refusal exists to catch.
    """
    from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A
    from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

    source = SimKeithley6221("SIM")
    source._paired_meter = SimKeithley2182A("SIM")
    source.configure_and_start_delta(high_current=1e-6, n_readings=5, delay=0.01)

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        source.set_source_enabled(False)
    assert excinfo.value.code == "-221"
    assert excinfo.value.instrument_message == "Settings conflict"

    # ...and the documented recovery works: abort first, then switch.
    source.stop_delta_mode()
    source.set_source_enabled(False)


def test_sim_keithley_6221_reproduces_the_221_leftover_range_rejection():
    """The -221 incident itself: a pinned range left behind by delta mode.

    Live commissioning (2026-07-22) found delta mode leaves the source's
    current range FIXED with autorange OFF, so a later, larger DC current is
    rejected outright and the source silently keeps its old value.
    ``stop_delta_mode()`` deliberately does not undo that (neither does the
    real instrument), so the leftover state is still there afterwards.

    The current write is driven through ``_apply_current()`` — the sim's
    model of the bare ``:SOUR:CURR`` write — because the public
    ``set_current()`` is the *fix*: it re-asserts autorange first and is
    therefore immune, which is precisely what the next test pins.
    """
    from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A
    from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

    source = SimKeithley6221("SIM")
    source._paired_meter = SimKeithley2182A("SIM")
    source.configure_and_start_delta(high_current=1e-6, n_readings=5, delay=0.01)
    source.stop_delta_mode()
    assert source._autorange is False

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        source._apply_current("set_current(0.0001)", 1e-4)
    assert excinfo.value.code == "-221"
    assert source.get_current() == 0.0  # the source did NOT take the value


def test_sim_keithley_6221_set_current_recovers_from_the_leftover_range():
    """The fix: ``set_current()`` re-asserts autorange, so -221 cannot recur."""
    from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A
    from cryosoft.drivers.sim_keithley_6221 import SimKeithley6221

    source = SimKeithley6221("SIM")
    source._paired_meter = SimKeithley2182A("SIM")
    source.configure_and_start_delta(high_current=1e-6, n_readings=5, delay=0.01)
    source.stop_delta_mode()

    source.set_current(1e-4)
    assert source.get_current() == pytest.approx(1e-4)


# ── Keithley 2182A — SCPI error queue ─────────────────────────────────────────


def test_keithley_2182a_real_raises_typed_error_from_the_scpi_queue():
    """A refused range change must not be mistaken for a range change."""
    from cryosoft.drivers.keithley_2182a import Keithley2182A

    driver = _fake_visa_driver(
        Keithley2182A, ['-222,"Parameter data out of range"']
    )
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.set_range(1000.0)
    assert excinfo.value.code == "-222"
    assert excinfo.value.instrument_message == "Parameter data out of range"


def test_sim_keithley_2182a_refuses_a_range_above_the_channel_maximum():
    """The wrong sequence: ask for a range the channel does not have."""
    from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A

    meter = SimKeithley2182A("SIM")
    before = meter.get_range()
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        meter.set_range(1000.0)
    assert excinfo.value.code == "-222"
    assert meter.get_range() == before  # the range did NOT change


# ── Lakeshore 335 — IEEE-488.2 status byte ────────────────────────────────────


def test_lakeshore_335_real_raises_typed_error_from_the_event_status_register():
    """An execution-error bit in ``*ESR?`` becomes the typed error."""
    from cryosoft.drivers.lakeshore_335 import Lakeshore335

    driver = _fake_visa_driver(Lakeshore335, ["16"])  # bit 4 = execution error
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.set_heater_range("HIGH")
    assert excinfo.value.code == "ESR:0x10"
    assert "Execution error" in excinfo.value.instrument_message


def test_lakeshore_335_real_clean_event_status_raises_nothing():
    """A zero register is the only reading that means the command landed."""
    from cryosoft.drivers.lakeshore_335 import Lakeshore335

    driver = _fake_visa_driver(Lakeshore335, ["0"])
    driver.set_heater_range("OFF")


def test_sim_lakeshore_335_refuses_an_empty_user_curve_slot():
    """The wrong sequence: assign a USER curve slot that holds no curve."""
    from cryosoft.drivers.sim_lakeshore_335 import SimLakeshore335

    controller = SimLakeshore335("SIM")
    before = controller.get_sensor_curve("A")
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        controller.set_sensor_curve(45, "A")
    assert excinfo.value.code == "ESR:0x10"
    assert controller.get_sensor_curve("A") == before  # assignment did NOT happen


# ── Keithley 705 — explicit readback ──────────────────────────────────────────


def test_keithley_705_real_raises_readback_mismatch_when_a_close_is_discarded():
    """The 705 discards what it cannot do; only the readback can tell."""
    from cryosoft.drivers.keithley_705 import Keithley705

    driver = object.__new__(Keithley705)
    instr = MagicMock()
    # The G2 buffer dump the instrument answers with: nothing closed.
    instr.query.return_value = "C001,S0,C002,S0"
    driver._instr = instr
    driver._closed = set()
    driver._pole_mode = 2

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.close_channel("1")
    assert excinfo.value.code == "READBACK_MISMATCH"
    assert "close_channel" in excinfo.value.context


def test_sim_keithley_705_refuses_a_channel_past_the_installed_range():
    """The wrong sequence: route through a channel this frame does not have."""
    from cryosoft.drivers.sim_keithley_705 import SimKeithley705

    scanner = SimKeithley705("SIM")
    assert scanner.first_last_channel() == (1, 20)
    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        scanner.close_channel("42")
    assert excinfo.value.code == "READBACK_MISMATCH"
    assert scanner.closed_channels() == []


# ── Oxford ILM 200 / 210 — ISOBUS protocol acknowledgement ────────────────────


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        ("oxford_ilm200", "OxfordILM200"),
        ("oxford_ilm210", "OxfordILM210"),
    ],
)
def test_oxford_ilm_real_raises_typed_error_on_the_isobus_question_mark(
    module_name: str, class_name: str
) -> None:
    """``?`` + the command is the ISOBUS way of saying "I did not do that"."""
    import importlib

    module = importlib.import_module(f"cryosoft.drivers.{module_name}")
    driver = object.__new__(getattr(module, class_name))
    instr = MagicMock()
    instr.read.return_value = "?R1"
    driver._instr = instr

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver._execute("R1")
    assert excinfo.value.code == "?"
    assert excinfo.value.instrument_message == "?R1"


@pytest.mark.parametrize(
    ("module_name", "class_name"),
    [
        ("sim_oxford_ilm200", "SimOxfordILM200"),
        ("sim_oxford_ilm210", "SimOxfordILM210"),
    ],
)
def test_sim_oxford_ilm_refuses_a_channel_with_no_probe_fitted(
    module_name: str, class_name: str
) -> None:
    """The wrong sequence: read a level from a channel that has no probe.

    The failure a config naming a nitrogen channel this meter does not carry
    would otherwise produce — a plausible-looking number that is not a level.
    """
    import importlib

    module = importlib.import_module(f"cryosoft.drivers.{module_name}")
    meter = getattr(module, class_name)("SIM")
    meter._channels_fitted = {1}  # helium only, no nitrogen probe

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        meter.get_nitrogen_level()
    assert excinfo.value.code == "?"
    # The helium channel, which IS fitted, keeps working.
    assert isinstance(meter.get_helium_level(), float)


# ── Oxford ITC 503 — ISOBUS acknowledgement, via pymeasure ────────────────────


class _FakeOxfordVISAError(Exception):
    """Stands in for pymeasure's ``OxfordVISAError`` (matched by class name).

    The driver matches by name rather than by import, because pymeasure is
    imported lazily in its ``__init__`` — so this fake reproduces the real
    dispatch faithfully.
    """


_FakeOxfordVISAError.__name__ = "OxfordVISAError"


def test_oxford_itc503_real_separates_a_refusal_from_a_broken_link():
    """A refused command and a dead link are different facts, typed apart."""
    from cryosoft.drivers.oxford_itc503 import OxfordITC503

    driver = object.__new__(OxfordITC503)

    refusal = driver._write_failure(
        _FakeOxfordVISAError("?T4.2"), "set_setpoint", "ITC503: could not set setpoint"
    )
    assert isinstance(refusal, CryoSoftInstrumentError)
    assert refusal.code == "?"

    broken = driver._write_failure(
        OSError("serial port vanished"), "set_setpoint", "ITC503: could not set setpoint"
    )
    assert isinstance(broken, CryoSoftCommunicationError)
    assert not isinstance(broken, CryoSoftInstrumentError)


def test_sim_oxford_itc503_refuses_control_commands_while_in_local():
    """The wrong sequence: drive the controller without holding remote.

    Without remote the ITC carries no control command at all, and says so
    only with a ``?`` — the failure that otherwise reads as "the setpoint
    just never moves".
    """
    from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503

    controller = SimOxfordITC503("SIM")
    controller.set_setpoint(4.2)  # remote by default: fine
    controller._control_mode = "LU"  # local & unlocked, e.g. front-panel LOCAL

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        controller.set_setpoint(10.0)
    assert excinfo.value.code == "?"
    assert controller.get_setpoint() == pytest.approx(4.2)  # unchanged


# ── Oxford Mercury iPS / IPS 120 — protocol acknowledgement ───────────────────


@pytest.mark.parametrize("refusal", ["DENIED", "INVALID"])
def test_oxford_mercury_ips_real_raises_typed_error_on_a_refused_action(
    refusal: str,
) -> None:
    """``DENIED``/``INVALID`` on the STAT: line means the PSU did not act."""
    from cryosoft.drivers.oxford_mercury_ips import OxfordMercuryiPS

    driver = object.__new__(OxfordMercuryiPS)
    instr = MagicMock()
    instr.query.return_value = f"STAT:SET:DEV:GRPZ:PSU:ACTN:RTOS:{refusal}"
    driver._instr = instr

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver.hold()
    assert excinfo.value.code == refusal


def test_sim_oxford_ips120_refuses_a_ramp_rate_while_clamped():
    """The wrong sequence: program a clamped PSU without clearing the clamp."""
    from cryosoft.drivers.sim_oxford_ips120 import SimOxfordIPS120

    psu = SimOxfordIPS120("SIM")
    psu.set_ramp_rate(0.5)
    psu._simulate_clamp = True

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        psu.set_ramp_rate(1.0)
    assert excinfo.value.code == "DENIED"
    assert psu.get_ramp_rate() == pytest.approx(0.5)  # unchanged


# ── Tensormeter RTM2 — protocol acknowledgement ───────────────────────────────


def test_tensormeter_rtm2_real_raises_typed_error_on_a_protocol_error():
    """A protocol-level error is the RTM2 saying it did not apply the value."""
    from cryosoft.drivers.tensormeter_rtm2 import TensormeterRTM2

    driver = object.__new__(TensormeterRTM2)
    driver._timeout_s = 1.0
    result = MagicMock()
    result.error = "value out of range"
    result.updates = []
    rtm = MagicMock()
    rtm.read_until.return_value = result
    driver._rtm = rtm

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver._send_and_confirm("cudc", 1.0)
    assert excinfo.value.code == "PROTOCOL"
    assert excinfo.value.instrument_message == "value out of range"


def test_tensormeter_rtm2_real_raises_typed_error_when_nothing_is_confirmed():
    """No echo means the value cannot be assumed to be in force."""
    from cryosoft.drivers.tensormeter_rtm2 import TensormeterRTM2

    driver = object.__new__(TensormeterRTM2)
    driver._timeout_s = 1.0

    no_echo = MagicMock(error=None, updates=[])
    # The `gass` fallback answers healthily, but with nothing that shows the
    # requested value in force — so the driver still has no confirmation.
    gass_reply = MagicMock(error=None, updates=[MagicMock(parameter="camp", value=0.0)])
    rtm = MagicMock()
    rtm.read_until.side_effect = [no_echo, gass_reply]
    rtm.get_state.return_value = {}
    driver._rtm = rtm

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        driver._send_and_confirm("cudc", 1.0)
    assert excinfo.value.code == "NO_CONFIRMATION"


def test_sim_tensormeter_rtm2_refuses_a_setpoint_beyond_the_protection_limit():
    """The wrong sequence: source past the configured current protection."""
    from cryosoft.drivers.sim_tensormeter_rtm2 import SimTensormeterRTM2

    box = SimTensormeterRTM2("SIM")
    limit = box._current_protection_A

    with pytest.raises(CryoSoftInstrumentError) as excinfo:
        box.set_current_dc(limit * 10)
    assert excinfo.value.code == "PROTOCOL"
    # Nothing was driven into the sample (no public DC-current getter exists).
    assert box._current_dc_A == 0.0
