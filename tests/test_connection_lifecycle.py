"""Connection-lifecycle standard: connect/disconnect across L0-L3."""

from __future__ import annotations

from pathlib import Path

import pytest

from cryosoft.core.exceptions import CryoSoftCommunicationError
from cryosoft.core.orchestrator import Orchestrator, OrchestratorState
from cryosoft.core.station import Station, build_station
from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A
from cryosoft.drivers.sim_oxford_itc503 import SimOxfordITC503
from cryosoft.virtual_instruments.base import BaseVirtualInstrument

SIM_CONFIG = Path(__file__).parent.parent / "cryosoft" / "configs" / "sim_cryostat"


@pytest.fixture
def station() -> Station:
    """A full sim station, built the way the app builds it."""
    return build_station(str(SIM_CONFIG))


# ── L0: the driver's released session ────────────────────────────────────────


def test_close_releases_the_session_and_is_idempotent():
    """close() twice is fine; afterwards the instrument is unreachable."""
    driver = SimOxfordITC503("SIM::TEST")
    assert driver.get_temperature() > 0
    driver.close()
    driver.close()
    with pytest.raises(CryoSoftCommunicationError):
        driver.get_temperature()


def test_close_does_not_change_instrument_state():
    """Releasing the session sends no command — rule 2 of the standard.

    The sim records the setpoint it was given; a close() that "helpfully"
    stood the instrument down would have to overwrite it.
    """
    driver = SimOxfordITC503("SIM::TEST")
    driver.set_setpoint(4.2)
    assert driver.get_setpoint() == pytest.approx(4.2)
    driver.close()
    # Reading through the closed session fails, but the modelled instrument
    # state behind it is untouched.
    assert driver._setpoint == pytest.approx(4.2)


def test_continuous_initiation_is_not_pinned_at_construction():
    """The 2182A's single-shot pin is an arming command, not a build command."""
    meter = SimKeithley2182A("SIM::TEST")
    assert meter._continuous_initiation is True  # the power-up default, untouched
    meter.set_continuous_initiation(False)
    assert meter._continuous_initiation is False


# ── L1: the VI's connection hooks ────────────────────────────────────────────


def test_ping_is_true_for_every_reachable_vi(station: Station):
    """The inherited ping() answers for every VI category, not just measurement.

    Before the standard, only measurement and switch VIs implemented ping()
    and the base returned False, so the front panel's "Check connection"
    button reported "Not reachable" for every magnet and thermometer.
    """
    assert station.get_vi_names()
    for vi_name in station.get_vi_names():
        assert station.get_vi(vi_name).ping() is True, vi_name


def test_ping_is_false_once_the_session_is_released(station: Station):
    """A VI whose driver was closed reports not-reachable rather than raising."""
    vi = station.get_vi("magnet_z")
    for driver in vi._drivers.values():
        driver.close()
    assert vi.ping() is False


def test_disconnect_hook_sends_no_commands(station: Station):
    """The VI-level disconnect() hook must not command the instrument."""
    vi = station.get_vi("temperature_sample")
    vi.set_temperature(10.0)
    setpoint_before = vi._driver.get_setpoint()
    vi.disconnect()
    assert vi._driver.get_setpoint() == pytest.approx(setpoint_before)


# ── L1: the detach-when-idle declaration ─────────────────────────────────────
# See BaseVirtualInstrument's "Detach-when-idle declaration" (part of the
# connection-lifecycle standard): a VI opts in by overriding the read-only
# detach_when_idle property; the base then releases the driver session
# automatically — via __init_subclass__'s wrap of a directly defined
# standby(), or via the base's own standby() for a VI that inherits it
# unchanged. TensormeterRTM2MeasurementVI is the one production VI that
# declares it; these tests use a plain double that does NOT hand-write the
# release at all, to prove it really is framework behaviour.


def test_detach_when_idle_is_false_by_default():
    """A VI that never overrides the property never detaches — the regression guard."""
    driver = _DetachableDriver("SIM::TEST")
    vi = _PlainVI({"main": driver})
    assert vi.detach_when_idle is False

    vi.standby()

    assert vi.is_attached() is True
    assert driver._closed is False


def test_declared_vi_is_born_attached():
    """Declaring detach_when_idle alone does not imply born-detached.

    Born-detached (RTM2's __init__ calling self._detach()) is each VI's own
    choice; the base only guarantees the release on standby().
    """
    vi = _DetachWhenIdleNoOverrideVI({"main": _DetachableDriver("SIM::TEST")})
    assert vi.is_attached() is True


def test_base_standby_detaches_for_a_vi_with_no_standby_override():
    """A VI that never defines its own standby() still detaches on standby().

    Exercises BaseVirtualInstrument.standby() itself (the fallback path
    __init_subclass__'s wrap does not touch, since there is nothing directly
    defined on this class to wrap).
    """
    driver = _DetachableDriver("SIM::TEST")
    vi = _DetachWhenIdleNoOverrideVI({"main": driver})
    assert vi.is_attached() is True

    vi.standby()

    assert vi.is_attached() is False
    assert driver._closed is True


def test_subclass_standby_still_detaches_via_the_init_subclass_wrap():
    """A VI's OWN standby() override still triggers the release automatically.

    _DetachWhenIdleWithOwnStandbyVI.standby() does nothing about the
    connection itself — the release comes entirely from
    __init_subclass__'s wrap, proving the declaration is enforced, not
    merely offered.
    """
    driver = _DetachableDriver("SIM::TEST")
    vi = _DetachWhenIdleWithOwnStandbyVI({"main": driver})

    vi.standby()

    assert vi.own_standby_ran is True  # the override's own body really ran
    assert vi.is_attached() is False
    assert driver._closed is True


def test_is_attached_tracks_manual_attach_and_detach():
    """is_attached() reflects _attach()/_detach() directly, not just standby()."""
    driver = _DetachableDriver("SIM::TEST")
    vi = _DetachWhenIdleNoOverrideVI({"main": driver})

    vi._detach()
    assert vi.is_attached() is False
    assert driver._closed is True

    vi._attach()
    assert vi.is_attached() is True
    assert driver._closed is False


def test_detach_is_idempotent_and_never_raises():
    driver = _DetachableDriver("SIM::TEST")
    vi = _DetachWhenIdleNoOverrideVI({"main": driver})

    vi._detach()
    vi._detach()  # idempotent, must not raise

    assert vi.is_attached() is False


def test_attach_never_raises_when_ensure_connected_fails():
    """A failing reattach must not block the caller — is_attached() stays False."""

    class _FailsToReconnectDriver(_DetachableDriver):
        def ensure_connected(self) -> None:
            raise CryoSoftCommunicationError("cannot reopen")

    driver = _FailsToReconnectDriver("SIM::TEST")
    vi = _DetachWhenIdleNoOverrideVI({"main": driver})
    vi._detach()

    vi._attach()  # must not raise

    assert vi.is_attached() is False


def test_ping_verify_and_release_through_the_base_for_a_plain_double():
    """The base's ping() reattaches, round-trips, then releases again — no VI code."""
    driver = _DetachableDriver("SIM::TEST")
    vi = _DetachWhenIdleNoOverrideVI({"main": driver})
    vi._detach()

    assert vi.ping() is True
    assert driver._closed is True  # released again after the round trip


def test_ping_returns_false_and_still_releases_when_unreachable():
    class _NeverAnswersDriver(_DetachableDriver):
        def get_idn(self) -> str:
            raise CryoSoftCommunicationError("hung")

    driver = _NeverAnswersDriver("SIM::TEST")
    vi = _DetachWhenIdleNoOverrideVI({"main": driver})
    vi._detach()

    assert vi.ping() is False
    assert driver._closed is True


def test_shared_driver_alias_with_detach_when_idle_is_flagged_not_permitted():
    """Two VIs' config naming the same driver alias, one detach_when_idle: the hazard.

    ``_detach()`` (``BaseVirtualInstrument``) iterates ``self._drivers`` and
    closes every one of them unconditionally — it has no notion of another
    VI still needing that same driver instance, because a VI may never
    import the Station to ask (Layer 1 cannot import Layer 2). Two VIs
    sharing one driver, one of them detach_when_idle, is exactly the
    configuration ``BaseVirtualInstrument``'s "Detach-when-idle
    declaration" docstring forbids: that VI's ``standby()`` would silently
    close a session the other VI still needs.

    This is why the fix lives at the config/conformance level
    (``tests/test_conformance.py::test_detach_when_idle_vi_owns_its_driver_
    aliases_exclusively``) rather than as a runtime guard inside
    ``_detach()`` itself, which a VI cannot make. This test proves the
    SAME predicate that conformance test (and ``Station.
    disconnect_instrument()``, for its own analogous release path) trusts —
    ``cryosoft.core.station._exclusive_aliases()`` — actually flags this
    scenario, rather than exercising the broken behaviour (letting
    ``standby()`` really close the shared driver and break the other VI).
    """
    from cryosoft.core.station import _exclusive_aliases

    driver = _DetachableDriver("SIM::TEST")
    detaching_vi = _DetachWhenIdleNoOverrideVI({"main": driver})
    sharing_vi = _PlainVI({"main": driver})
    assert detaching_vi._drivers["main"] is sharing_vi._drivers["main"], (
        "the hazard requires a literally shared driver instance"
    )
    assert detaching_vi.detach_when_idle is True

    # The two VIs' devices.yaml-shaped specs, as Station._vi_specs would
    # hold them: both name the same alias.
    vi_specs = {
        "detaching_vi": {"drivers": {"main": "shared_alias"}},
        "sharing_vi": {"drivers": {"main": "shared_alias"}},
    }
    role_aliases = vi_specs["detaching_vi"]["drivers"]

    exclusive = _exclusive_aliases(role_aliases, vi_specs, "detaching_vi")

    assert "shared_alias" not in exclusive, (
        "a driver alias another configured VI also names must never be "
        "reported exclusive — this is the constraint a detach_when_idle "
        "VI's config must satisfy"
    )


# ── L2: the Station degrades and restores ────────────────────────────────────


def test_disconnect_moves_the_vi_into_the_offline_registry(station: Station):
    """A disconnected VI degrades exactly like one that never connected."""
    ok, message = station.disconnect_instrument("magnet_z")

    assert ok is True
    assert "magnet_z" in message
    assert "magnet_z" not in station.get_vi_names()
    assert "magnet_z" in station.offline_vi_names()
    assert station.has_vi("magnet_z") is False
    info = station.get_offline_info("magnet_z")
    assert "operator" in info.tags
    assert info.vi_type == "system"


def test_disconnected_vi_leaves_the_rest_of_the_station_working(station: Station):
    """Everything else keeps polling — that is the whole point of degrading."""
    before = set(station.get_vi_names())
    station.disconnect_instrument("magnet_z")

    state = station.get_state()
    assert "magnet_z" not in state
    assert set(state) == before - {"magnet_z"}
    assert all(not s.get("_stale") for s in state.values())


def test_disconnect_closes_only_exclusively_owned_drivers(station: Station):
    """A driver shared with a live VI stays open.

    sim_cryostat wires the same 6221/2182A pair into three measurement VIs,
    so disconnecting one must not break the other two.
    """
    dc_vi = station.get_vi("dc_measurement")
    shared_source = dc_vi._drivers["source"]

    station.disconnect_instrument("dc_measurement")

    # Still open: keithley_delta_mode and keithley_dc_mode reference it.
    assert shared_source.get_idn()
    assert station.get_vi("keithley_delta_mode").ping() is True
    assert station.get_vi("keithley_dc_mode").ping() is True


def test_disconnect_closes_an_exclusively_owned_driver(station: Station):
    """The session really is released — that is what frees the front panel."""
    driver = station.get_vi("level_meter")._drivers["main"]

    station.disconnect_instrument("level_meter")

    with pytest.raises(CryoSoftCommunicationError):
        driver.get_idn()


def test_disconnect_then_connect_restores_the_vi(station: Station):
    """connect_instrument() is the one way back, for either offline origin."""
    station.disconnect_instrument("level_meter")
    assert "level_meter" not in station.get_vi_names()

    ok, message = station.connect_instrument("level_meter")

    assert ok is True
    assert "level_meter" in message
    assert "level_meter" in station.get_vi_names()
    assert station.offline_vi_names() == []
    # A freshly built driver, so the instrument answers again.
    assert station.get_vi("level_meter").ping() is True
    assert "level_meter" in station.get_state()


def test_disconnect_rejects_unknown_and_already_offline_names(station: Station):
    """Explicit verdicts, never exceptions — the control-validation style."""
    ok, message = station.disconnect_instrument("no_such_vi")
    assert ok is False
    assert "not a registered VI" in message

    station.disconnect_instrument("level_meter")
    ok, message = station.disconnect_instrument("level_meter")
    assert ok is False
    assert "already disconnected" in message


def test_disconnect_clears_a_standing_comm_fault(station: Station):
    """A VI nobody polls cannot be in a fault — the banner must not name it."""
    station._record_comm_condition("level_meter", "disconnected", "boom")
    assert "level_meter" in station.vi_faults()

    station.disconnect_instrument("level_meter")

    assert "level_meter" not in station.vi_faults()


def test_build_sends_only_the_identity_check(tmp_path):
    """An instrument that opens but never answers is offline, not live."""
    (tmp_path / "devices.yaml").write_text(
        "real_drivers:\n"
        "  mute_drv:\n"
        "    class: tests.test_connection_lifecycle._MuteDriver\n"
        '    address: "SIM::MUTE"\n'
        "virtual_instruments:\n"
        "  mute_vi:\n"
        "    class: tests.test_connection_lifecycle._PlainVI\n"
        "    drivers: {main: mute_drv}\n"
        "    vi_type: system\n"
    )
    (tmp_path / "monitor.yaml").write_text(
        "monitor:\n  tick_interval_ms: 1000\n  max_vi_errors: 3\n"
    )

    station = build_station(str(tmp_path))

    assert station.get_vi_names() == []
    assert station.offline_vi_names() == ["mute_vi"]
    reason = station.get_offline_info("mute_vi").reason
    assert "identity query" in reason
    assert "connect_failed" in station.get_offline_info("mute_vi").tags


# ── L3: the Orchestrator's gating and verdicts ───────────────────────────────


def test_orchestrator_disconnect_emits_the_verdict(station: Station, qtbot):
    """A successful disconnect reports through the standard action signals."""
    orch = Orchestrator(station, tick_interval_ms=10)
    try:
        with qtbot.waitSignals(
            [orch.instrument_disconnected, orch.action_succeeded], timeout=500
        ):
            orch.disconnect_instrument("magnet_z")
        assert station.has_vi("magnet_z") is False
    finally:
        orch.shutdown()


def test_orchestrator_disconnect_blocked_outside_idle(station: Station, qtbot):
    """Losing an instrument mid-run would escape the run's safety review."""
    orch = Orchestrator(station, tick_interval_ms=10)
    try:
        orch._state = OrchestratorState.MEASURING
        with qtbot.waitSignal(orch.action_blocked, timeout=500) as blocker:
            orch.disconnect_instrument("magnet_z")
        assert "magnet_z" in blocker.args[0]
        assert station.has_vi("magnet_z") is True
    finally:
        orch._state = OrchestratorState.IDLE
        orch.shutdown()


def test_orchestrator_disconnect_failure_emits_action_failed(
    station: Station, qtbot
):
    """An unknown VI yields action_failed with the reason, never an exception."""
    orch = Orchestrator(station, tick_interval_ms=10)
    try:
        with qtbot.waitSignal(orch.action_failed, timeout=500) as blocker:
            orch.disconnect_instrument("no_such_vi")
        assert blocker.args[1] == "disconnect"
    finally:
        orch.shutdown()


def test_orchestrator_releases_the_scanner_slot_on_disconnect(
    station: Station, qtbot
):
    """The scanner slot must not keep naming a VI that is gone."""
    orch = Orchestrator(station, tick_interval_ms=10)
    try:
        assert orch._scanner_vi_name == "switch_matrix"
        orch.disconnect_instrument("switch_matrix")
        assert orch._scanner_vi_name is None
        orch.connect_instrument("switch_matrix")
        assert orch._scanner_vi_name == "switch_matrix"
    finally:
        orch.shutdown()


def test_disconnect_survives_a_round_trip_through_the_tick(
    station: Station, qtbot
):
    """The monitor tick keeps running over a station missing an instrument."""
    orch = Orchestrator(station, tick_interval_ms=10)
    try:
        orch.disconnect_instrument("magnet_z")
        orch.start_monitoring()
        with qtbot.waitSignal(orch.states_updated, timeout=1000) as blocker:
            pass
        assert "magnet_z" not in blocker.args[0]
        assert orch.state == "IDLE"
    finally:
        orch.shutdown()


# ── Test doubles ─────────────────────────────────────────────────────────────


class _MuteDriver:
    """A driver whose session opens but whose instrument never answers.

    The failure the build-time identity check exists to catch: a live VISA
    session is not proof that anything is on the other end of the cable.
    """

    def __init__(self, resource_string: str) -> None:
        self.resource_string = resource_string
        self.closed = False

    def get_idn(self) -> str:
        raise CryoSoftCommunicationError("no response to the identity query")

    def close(self) -> None:
        self.closed = True


class _PlainVI(BaseVirtualInstrument):
    """A VI double with no overrides — it inherits every lifecycle hook."""

    vi_type = "system"


class _DetachableDriver:
    """A driver double modelling the detach-when-idle standard's session.

    ``close()``/``ensure_connected()`` model the TCP-style session a
    single-client instrument holds; a command sent while closed raises,
    exactly like the real drivers' use-after-close failure.
    """

    def __init__(self, resource_string: str) -> None:
        self.resource_string = resource_string
        self._closed = False

    def get_idn(self) -> str:
        if self._closed:
            raise CryoSoftCommunicationError("closed")
        return "TEST,DETACHABLE,0,0"

    def close(self) -> None:
        self._closed = True

    def ensure_connected(self) -> None:
        self._closed = False


class _DetachWhenIdleNoOverrideVI(BaseVirtualInstrument):
    """Declares detach_when_idle but relies entirely on the inherited standby().

    Proves the release is framework behaviour — BaseVirtualInstrument's own
    standby() and ping() — not something every declaring VI must hand-write,
    the way TensormeterRTM2MeasurementVI no longer does.
    """

    vi_type = "measurement"

    @property
    def detach_when_idle(self) -> bool:
        return True


class _DetachWhenIdleWithOwnStandbyVI(BaseVirtualInstrument):
    """Declares detach_when_idle AND defines its own standby().

    Its standby() does nothing about the connection — the release comes
    entirely from __init_subclass__'s wrap, proving that wrap fires for a
    directly defined standby() too, not just the base's fallback.
    """

    vi_type = "measurement"
    own_standby_ran: bool = False

    @property
    def detach_when_idle(self) -> bool:
        return True

    def standby(self) -> None:
        self.own_standby_ran = True
