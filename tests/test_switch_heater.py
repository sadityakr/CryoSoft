"""Unit tests for the SwitchHeater wall-clock readiness state object."""

from cryosoft.virtual_instruments.magnet.switch_heater import SwitchHeater


def _fake_clock():
    """Return (clock_callable, setter) driving a mutable 'now'."""
    now = {"t": 0.0}
    return (lambda: now["t"]), (lambda t: now.__setitem__("t", t))


def test_warmup_readiness_is_time_based():
    clock, set_now = _fake_clock()
    sh = SwitchHeater(warmup_s=60, cooldown_s=60, clock=clock)
    assert not sh.is_on
    assert not sh.is_ready()

    sh.turn_on()
    assert sh.is_on
    assert not sh.is_ready()
    assert sh.seconds_until_ready() == 60.0

    set_now(59.9)
    assert not sh.is_ready()
    set_now(60.0)
    assert sh.is_ready()
    assert sh.seconds_until_ready() == 0.0


def test_turn_on_when_already_on_does_not_restart_warmup():
    clock, set_now = _fake_clock()
    sh = SwitchHeater(warmup_s=60, clock=clock)
    sh.turn_on()
    set_now(30.0)
    sh.turn_on()  # already on — must NOT reset the 60 s timer
    set_now(60.0)
    assert sh.is_ready()


def test_cooldown_is_time_based():
    clock, set_now = _fake_clock()
    sh = SwitchHeater(warmup_s=60, cooldown_s=60, clock=clock)
    sh.turn_on()
    set_now(100.0)
    sh.turn_off()
    assert not sh.is_cold()
    assert not sh.is_ready()  # off is never ready-to-ramp
    set_now(160.0)
    assert sh.is_cold()


def test_seconds_until_ready_zero_when_off():
    clock, _ = _fake_clock()
    sh = SwitchHeater(warmup_s=60, clock=clock)
    assert sh.seconds_until_ready() == 0.0
