# ---
# description: |
#   SuperconductingMagnetPersistentVI: behavior-based VI for any superconducting
#   magnet power supply that includes a persistent-mode switch heater. Extends
#   SuperconductingMagnetVI with a manual persistent-mode toggle and a switch
#   heater whose thermal settling is timed in wall-clock SECONDS via a
#   SwitchHeater state object (independent of the Orchestrator tick rate).
#
#   Two operating modes, selected by the persistent-mode toggle (default OFF):
#   * Normal (OFF): a field change guarantees the switch heater is energised and
#     warm (matching the PSU to the coil current first, since heating across a
#     mismatch quenches), then ramps the PSU. The heater is left ON, so a field
#     sweep pays the 60 s warmup once, not per point. Manual heater controls are
#     refused here (the VI owns the heater).
#   * Persistent (ON): the user drives the switch heater and PSU current
#     directly (turn heater off, ramp PSU to zero -> magnet persistent). Turning
#     the heater ON is refused unless the PSU matches the coil current (quench
#     guard); turning it OFF is always allowed. The toggle can only be turned
#     back OFF while the heater is on. Procedures refuse to run in this mode.
#   A driver status of QUENCH aborts the ramp sequence.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.superconducting_magnet (SuperconductingMagnetVI)
#   - cryosoft.virtual_instruments.magnet.switch_heater (SwitchHeater)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <PSU driver with switch heater support>}
#   init_params: all params from SuperconductingMagnetVI plus
#   switch_heater_warmup_s (float, default 60): wall-clock seconds after the
#   heater is energised before the coil can be driven, and
#   switch_heater_cooldown_s (float, default 60): seconds after de-energising
#   before the switch is cold.
# process: |
#   Overrides start_ramp() to pick a normal vs manual ramp generator by the
#   persistent-mode toggle; overrides magnet_current()/get_field() to read the
#   coil current when the magnet is physically persistent (heater off). Adds
#   @monitored switch_heater_state / coil_current / is_persistent /
#   persistent_mode_enabled, and @control enable/disable_persistent_mode and
#   manual switch_heater_on / switch_heater_off.
# output: |
#   All SuperconductingMagnetVI outputs (now persistent-mode-correct) plus
#   switch_heater_state (str), coil_current (A), is_persistent (bool),
#   persistent_mode_enabled (bool) via @monitored.
# last_updated: 2026-07-12
# ---

"""SuperconductingMagnetPersistentVI — VI for SC magnet PSUs with switch heater."""

from __future__ import annotations

from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.virtual_instruments.magnet.superconducting_magnet import SuperconductingMagnetVI
from cryosoft.virtual_instruments.magnet.switch_heater import SwitchHeater


class SuperconductingMagnetPersistentVI(SuperconductingMagnetVI):
    """Virtual Instrument for a superconducting magnet PSU with persistent-mode switch heater.

    Operating modes
    ---------------
    The ``persistent_mode`` toggle (default OFF, ``enable_persistent_mode()`` /
    ``disable_persistent_mode()``) selects behaviour. It is a control-access
    mode, distinct from the physical ``is_persistent`` state (coil holds field,
    heater off).

    * Normal mode (toggle OFF) — procedures and everyday use. ``start_ramp()``:
      1. If the heater is off, match the PSU to the coil current (a no-op unless
         the magnet was parked persistent; matching MUST precede energising, as
         heating across a mismatch quenches), then energise the heater.
      2. Wait (wall-clock) until the switch is warm — 60 s once; no wait if the
         heater was already on and warm, so a sweep pays warmup once.
      3. Ramp the PSU to target. The heater is left ON.
      Manual ``switch_heater_on/off`` are refused in this mode.

    * Persistent mode (toggle ON) — manual persistent-park workflow.
      ``start_ramp()`` ramps the PSU current directly with no heater
      enforcement. ``switch_heater_off()`` is allowed anytime;
      ``switch_heater_on()`` is refused unless PSU matches coil (quench guard).
      The toggle can only be turned back OFF while the heater is on.

    Any step that observes the driver status ``"QUENCH"`` stops the ramp
    immediately; the Station safety check escalates a quench to EMERGENCY.

    ``magnet_current()`` / ``get_field()`` read the frozen coil current whenever
    the magnet is physically persistent (heater off), so a parked field reads
    back correctly instead of following the PSU. The warmup wait is timed by the
    ``SwitchHeater`` state object in wall-clock seconds and yields each tick, so
    it never blocks the Orchestrator loop.

    Driver contract
    ---------------
    All methods from ``SuperconductingMagnetVI`` plus:
    * ``get_switch_heater_state() -> str``  — "ON" | "OFF"
    * ``set_switch_heater(bool)``           — energise / de-energise
    * ``get_coil_current() -> float``       — Amperes (persistent coil current)
    * ``get_persistent_mode() -> bool``
    * ``set_persistent_mode(bool)``
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)

        # Switch heater thermal settling, timed in WALL-CLOCK SECONDS (not
        # Orchestrator ticks) via a state object, so warmup is a fixed 60 s
        # regardless of the tick interval and an already-warm heater incurs no
        # wait. The object is the VI's timing authority; the driver holds the
        # physical on/off, kept in sync through _energize_heater/_deenergize_heater.
        self._heater = SwitchHeater(
            warmup_s=float(init_params.get("switch_heater_warmup_s", 60.0)),
            cooldown_s=float(init_params.get("switch_heater_cooldown_s", 60.0)),
        )

        # Manual-unlock toggle (default OFF). OFF = normal operation: field
        # changes enforce the switch heater ON and the VI owns the heater. ON =
        # the user drives the switch heater and PSU current directly (the
        # persistent-park workflow). Procedures run only in normal mode.
        self._persistent_mode_enabled: bool = False

        # Current ramp sub-phase, for the watchdog (no-motion phases must not
        # read as a stall). Set as the generator walks its steps.
        self._phase: str = "idle"

    # ------------------------------------------------------------------
    # RampableVI override — switch-heater-aware
    # ------------------------------------------------------------------

    def start_ramp(self, target: float, persistent: bool = True) -> None:
        """Begin a switch-heater-aware ramp to *target* tesla.

        Behaviour is governed by the persistent-mode toggle, not by the
        *persistent* argument (which is ignored, kept only so callers can pass
        it uniformly to either magnet flavour):

        * Normal mode (toggle OFF): guarantee the switch heater is energised and
          warm, then ramp the PSU to *target*. The heater is left ON — there is
          no per-ramp cooldown or persistent park, so a field sweep pays the
          60 s warmup once.
        * Persistent mode (toggle ON): ramp the PSU current directly to *target*
          with no heater enforcement (the manual persistent-park workflow).

        Args:
            target: Target field in tesla.
            persistent: Ignored — see above.
        """
        _ = persistent
        target_A = self._clamp_target_A(target * self._amperes_per_tesla)
        self._ramp_target_T = target_A / self._amperes_per_tesla

        if self._persistent_mode_enabled:
            self._ramp_gen = self._manual_ramp_generator(target_A)
        else:
            self._ramp_gen = self._normal_ramp_generator(target_A)
        self._ramp_exhausted = False
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    # ramp_status() is inherited from SuperconductingMagnetVI: both generators
    # below run to completion (PSU at HOLD) before the generator is exhausted,
    # so the base "exhausted + driver HOLD -> TARGET_REACHED" test is correct.

    # ------------------------------------------------------------------
    # Switch-heater helpers (keep driver + timing state object in sync)
    # ------------------------------------------------------------------

    def _energize_heater(self) -> None:
        """Turn the switch heater ON (driver) and start its warmup clock.

        The caller MUST have matched the PSU output to the coil current first:
        the driver quenches if energised across a mismatch.
        """
        self._driver.set_switch_heater(True)  # type: ignore[attr-defined]
        self._heater.turn_on()

    def _deenergize_heater(self) -> None:
        """Turn the switch heater OFF (driver) and start its cooldown clock."""
        self._driver.set_switch_heater(False)  # type: ignore[attr-defined]
        self._heater.turn_off()

    def ramp_phase(self) -> str:
        """Return the active persistent-ramp sub-phase.

        One of ``matching`` / ``warmup`` / ``ramping`` / ``cooldown`` /
        ``parking`` while a ramp is in flight, or ``"idle"`` when none is
        active. The watchdog suppresses stall detection during the no-motion
        phases (matching/warmup/cooldown/parking), where the field deliberately
        holds still and a flat gap is expected, not a fault.
        """
        return "idle" if self._ramp_gen is None else self._phase

    # ------------------------------------------------------------------
    # Internal ramp generators (normal vs manual/persistent-mode)
    # ------------------------------------------------------------------

    def _normal_ramp_generator(self, target_A: float) -> Generator:
        """Normal mode: ensure the heater is on and warm, then ramp; leave it on."""
        driver = self._driver  # type: ignore[attr-defined]
        self._phase = "matching"

        # Guarantee the switch heater is energised before driving the coil. If
        # it is off (e.g. the magnet was parked persistent), first match the PSU
        # output to the frozen coil current — energising across a mismatch
        # quenches. Matching is a no-op when coil == PSU (never was persistent).
        if driver.get_switch_heater_state() != "ON":
            coil_A = driver.get_coil_current()
            if abs(driver.get_current() - coil_A) > 0.01:
                driver.set_ramp_rate(self._default_ramp_rate)
                driver.set_current_setpoint(coil_A)
                while (
                    driver.get_status() == "RAMPING"
                    or abs(driver.get_current() - coil_A) > 0.01
                ):
                    if driver.get_status() == "QUENCH":
                        return
                    yield
            self._energize_heater()
        else:
            # The heater is already energised at the driver, but this VI instance
            # may not know it: normal mode deliberately leaves the heater ON, so
            # after an app restart the driver reports "ON" while the freshly
            # constructed SwitchHeater still tracks OFF. Without this adoption
            # is_ready() can never become true and the warmup wait below spins
            # forever (silently — "warmup" is a watchdog no-motion phase).
            # turn_on() is a no-op when already tracked on, so a sweep still pays
            # the warmup only once; on first adoption it starts the clock now,
            # costing at most one extra warmup on an already-warm heater.
            self._heater.turn_on()

        # Wait (wall-clock) until the switch is warm. No wait if already warm,
        # so a field sweep only pays the warmup on the first point.
        self._phase = "warmup"
        while not self._heater.is_ready():
            if driver.get_status() == "QUENCH":
                return
            yield

        # Ramp to target; the heater stays ON (no cooldown, no persistent park).
        self._phase = "ramping"
        yield from self._ramp_generator(target_A)
        while driver.get_status() == "RAMPING":
            yield

    def _manual_ramp_generator(self, target_A: float) -> Generator:
        """Persistent mode: ramp the PSU current directly, no heater enforcement.

        With the heater ON this moves the field; with it OFF only the PSU
        current moves and the coil holds the field (this is how the user parks
        the magnet: heater off, then ramp the PSU to zero).
        """
        driver = self._driver  # type: ignore[attr-defined]
        self._phase = "ramping"
        yield from self._ramp_generator(target_A)
        while driver.get_status() == "RAMPING":
            yield

    # ------------------------------------------------------------------
    # @monitored overrides — persistent-mode-correct current/field readback
    # ------------------------------------------------------------------

    @monitored
    def magnet_current(self) -> float:
        """Return the field-holding current in Amperes.

        While the magnet is persistent (switch heater off) the PSU current may
        differ from the field-holding current, so this reads the frozen
        ``get_coil_current()`` instead of the inherited ``get_current()``.
        """
        driver = self._driver  # type: ignore[attr-defined]
        if driver.get_persistent_mode():
            return driver.get_coil_current()
        return driver.get_current()

    @monitored
    def get_field(self) -> float:
        """Return the current magnetic field in tesla (persistent-mode-correct)."""
        return self.magnet_current() / self._amperes_per_tesla

    # ------------------------------------------------------------------
    # @monitored methods — switch heater / persistent mode
    # ------------------------------------------------------------------

    @monitored
    def switch_heater_state(self) -> str:
        """Return 'ON' if the switch heater is energised, 'OFF' otherwise."""
        return self._driver.get_switch_heater_state()  # type: ignore[attr-defined]

    @monitored
    def coil_current(self) -> float:
        """Return the persistent coil current in Amperes.

        Differs from ``magnet_current()`` (PSU output) when in persistent mode.
        """
        return self._driver.get_coil_current()  # type: ignore[attr-defined]

    @monitored
    def is_persistent(self) -> bool:
        """Return True when the magnet is physically persistent (switch heater off)."""
        return self._driver.get_persistent_mode()  # type: ignore[attr-defined]

    @monitored
    def persistent_mode_enabled(self) -> bool:
        """Return True when the manual persistent-mode toggle is on.

        Distinct from ``is_persistent`` (the physical coil-holds-field state):
        this is the control-access mode. When True the user drives the switch
        heater and PSU directly and procedures refuse to run; when False the VI
        manages the heater automatically (normal operation).
        """
        return self._persistent_mode_enabled

    # ------------------------------------------------------------------
    # @control methods — persistent-mode toggle and manual switch heater
    # ------------------------------------------------------------------

    @control(scope="operation")
    def enable_persistent_mode(self) -> None:
        """Enter persistent mode: unlock manual switch-heater / PSU control.

        In persistent mode the field is not auto-managed: the user turns the
        switch heater on/off and ramps the PSU directly (the persistent-park
        workflow). Procedures refuse to start while any magnet is in this mode.
        """
        self._persistent_mode_enabled = True

    @control(scope="operation")
    def disable_persistent_mode(self) -> None:
        """Return to normal operation. Refused unless the switch heater is on.

        Normal operation requires the switch heater energised (the field is
        driven through the warm switch). Leaving persistent mode while the
        heater is off would break that invariant, so it is refused until the
        user turns the heater on (match the PSU to the coil current first, then
        energise).

        Raises:
            CryoSoftSafetyError: If the switch heater is currently off.
        """
        if self._driver.get_switch_heater_state() != "ON":  # type: ignore[attr-defined]
            raise CryoSoftSafetyError(
                "Cannot leave persistent mode while the switch heater is off. "
                "Turn the switch heater on first (ramp the PSU to the coil "
                "current, then energise the heater), then disable persistent mode."
            )
        self._persistent_mode_enabled = False

    @control(scope="operation")
    def switch_heater_on(self) -> None:
        """Energise the switch heater (manual; persistent mode only).

        Refused in normal mode (the VI manages the heater there). Refused while
        the PSU output differs from the coil current: heating the switch across
        a mismatch forces the stored coil current through the resistive switch,
        a quench.

        Raises:
            CryoSoftSafetyError: If not in persistent mode, or PSU and coil
                currents are mismatched.
        """
        if not self._persistent_mode_enabled:
            raise CryoSoftSafetyError(
                "The switch heater is managed automatically in normal "
                "operation. Enable persistent mode for manual control."
            )
        driver = self._driver  # type: ignore[attr-defined]
        psu_A = driver.get_current()
        coil_A = driver.get_coil_current()
        if abs(psu_A - coil_A) > 0.01:
            raise CryoSoftSafetyError(
                f"Refusing to energise switch heater: PSU output is "
                f"{psu_A:.3f} A but the coil holds {coil_A:.3f} A. Heating "
                f"the switch across this mismatch would quench the magnet. "
                f"Ramp the PSU to the coil current first."
            )
        self._energize_heater()

    @control(scope="operation")
    def switch_heater_off(self) -> None:
        """De-energise the switch heater (manual; persistent mode only).

        Allowed at any time in persistent mode (the check is only on turning it
        on). Refused in normal mode, where the VI manages the heater.

        Raises:
            CryoSoftSafetyError: If not in persistent mode.
        """
        if not self._persistent_mode_enabled:
            raise CryoSoftSafetyError(
                "The switch heater is managed automatically in normal "
                "operation. Enable persistent mode for manual control."
            )
        self._deenergize_heater()
