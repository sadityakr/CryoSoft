# ---
# description: |
#   SuperconductingMagnetPersistentVI: behavior-based VI for any superconducting
#   magnet power supply that includes a persistent-mode switch heater.
#   Extends SuperconductingMagnetVI with switch heater control and a
#   switch-heater-aware ramp sequence. Every ramp first matches the PSU output
#   to the coil current while the switch is still cold (heating the switch
#   across a PSU/coil mismatch quenches the magnet), then heats the switch.
#   start_ramp(target, persistent=True) supports two modes: persistent=True
#   (default) ramps, cools the switch, then parks the PSU at zero with the
#   coil holding the field — for a single set-and-hold. persistent=False
#   ramps directly to target, leaving the switch heater energised — for a
#   sequence of many ramps (e.g. a field sweep) that should only pay the
#   heater warmup once. A driver status of QUENCH aborts the sequence.
#   Wait steps are implemented as tick-count generators (never time.sleep)
#   so they are compatible with the Orchestrator tick loop.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.superconducting_magnet (SuperconductingMagnetVI)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <PSU driver with switch heater support>}
#   init_params: all params from SuperconductingMagnetVI plus
#   switch_heater_warmup_ticks (int, default 30): orchestrator ticks to wait
#   after turning heater on before ramping,
#   switch_heater_cooldown_ticks (int, default 30): ticks to wait after turning
#   heater off before the field is considered stable in persistent mode.
#   Station.process_system_targets() forwards an optional "persistent" key
#   from the system target dict through to start_ramp() if present.
# process: |
#   Overrides start_ramp() with a switch-heater-aware generator; overrides
#   magnet_current()/get_field() to read the coil current (not the PSU
#   current, which is zero) while the driver reports persistent mode.
#   Adds @monitored methods for switch heater state, coil current, and
#   persistent mode flag. Adds @control methods for switch heater and
#   persistent mode transitions.
# output: |
#   All SuperconductingMagnetVI outputs (now persistent-mode-correct) plus
#   switch_heater_state (str), coil_current (A), is_persistent (bool) via
#   @monitored.
# last_updated: 2026-07-12
# ---

"""SuperconductingMagnetPersistentVI — VI for SC magnet PSUs with switch heater."""

from __future__ import annotations

from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.virtual_instruments.magnet.superconducting_magnet import SuperconductingMagnetVI


class SuperconductingMagnetPersistentVI(SuperconductingMagnetVI):
    """Virtual Instrument for a superconducting magnet PSU with persistent-mode switch heater.

    Ramp modes
    ----------
    ``start_ramp(target, persistent=True)``:

    * ``persistent=True`` (default) — set-and-hold sequence:
      1. With the switch still cold, ramp the PSU to match the coil current
         (no-op unless the magnet was parked persistent). Matching MUST happen
         before the heater goes on: heating across a mismatch quenches.
      2. Turn on switch heater (skipped if already on) — wait ``_warmup_ticks``.
      3. Ramp to target field.
      4. Turn off switch heater — wait ``_cooldown_ticks`` ticks.
      5. Enter persistent mode: PSU current returns to zero; coil holds field.

    * ``persistent=False`` — repeated-ramp sequence (e.g. a field sweep):
      steps 1-3 only. The switch heater stays energised and the PSU keeps
      holding the target current directly, so the next ``start_ramp()`` call
      does not have to pay the heater warmup again.

    Any step that observes the driver status ``"QUENCH"`` stops the sequence
    immediately (no further commands are sent); the Station safety check is
    responsible for escalating a quench to EMERGENCY.

    ``magnet_current()`` / ``get_field()`` read the coil current (not the PSU
    current, which is zero) whenever the driver reports persistent mode —
    otherwise a field parked via ``persistent=True`` would read back as 0.

    The wait steps use tick counters so they do not block the Orchestrator event
    loop. ``ramp_status()`` returns ``"RAMPING"`` throughout the sequence and
    ``"TARGET_REACHED"`` once the ramp generator is exhausted (for
    ``persistent=False``) or once the magnet has actually reached persistent
    mode at the target field (for ``persistent=True``).

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

        # Tick counts for switch heater thermal equilibration.
        self._warmup_ticks: int = int(init_params.get("switch_heater_warmup_ticks", 30))
        self._cooldown_ticks: int = int(init_params.get("switch_heater_cooldown_ticks", 30))

        # Whether the ramp currently in flight (or just completed) should end
        # in persistent mode. Read by ramp_status() to pick the completion test.
        self._pending_persistent: bool = True

    # ------------------------------------------------------------------
    # RampableVI override — switch-heater-aware
    # ------------------------------------------------------------------

    def start_ramp(self, target: float, persistent: bool = True) -> None:
        """Begin a switch-heater-aware ramp to *target* tesla.

        Args:
            target: Target field in tesla.
            persistent: If True (default), end the ramp by cooling the switch
                and parking the PSU at zero with the coil holding the field.
                If False, leave the switch heater energised and the PSU
                holding the target directly — for a sequence of many ramps
                (e.g. a field sweep) that should only heat the switch once.
        """
        target_A = self._clamp_target_A(target * self._amperes_per_tesla)
        self._ramp_target_T = target_A / self._amperes_per_tesla

        self._pending_persistent = persistent
        self._ramp_gen = self._persistent_ramp_generator(target_A, persistent)
        self._ramp_exhausted = False
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    def ramp_status(self) -> str:
        """Return current ramp state.

        Returns:
            ``"IDLE"``           — no ramp generator active.
            ``"TARGET_REACHED"`` — ramp sequence complete: for a
                                   ``persistent=True`` ramp, the magnet is
                                   confirmed in persistent mode; for
                                   ``persistent=False``, the generator is
                                   exhausted (heater stays on, PSU holds).
            ``"RAMPING"``        — sequence in progress.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if self._ramp_exhausted:
            if not self._pending_persistent:
                return "TARGET_REACHED"
            driver = self._driver  # type: ignore[attr-defined]
            if driver.get_persistent_mode():
                return "TARGET_REACHED"
            return "RAMPING"
        return "RAMPING"

    # ------------------------------------------------------------------
    # Internal persistent-mode generator
    # ------------------------------------------------------------------

    def _persistent_ramp_generator(self, target_A: float, persistent: bool) -> Generator:
        driver = self._driver  # type: ignore[attr-defined]

        # --- Step 1: With the switch still cold (superconducting), match the
        # PSU output to the coil current BEFORE energising the heater.
        # Heating the switch across a PSU/coil mismatch forces the stored coil
        # current through the resistive switch — a quench. Matching first is
        # always safe: if the magnet was never persistent, coil == PSU and
        # this is a no-op.
        if driver.get_switch_heater_state() != "ON":
            coil_A = driver.get_coil_current()
            if abs(driver.get_current() - coil_A) > 0.01:
                driver.set_ramp_rate(self._default_ramp_rate)
                driver.set_current_setpoint(coil_A)
                # Wait until the PSU actually reaches the coil current.
                while (
                    driver.get_status() == "RAMPING"
                    or abs(driver.get_current() - coil_A) > 0.01
                ):
                    if driver.get_status() == "QUENCH":
                        return  # stop the sequence; safety monitoring handles it
                    yield
            driver.set_persistent_mode(False)

            # --- Step 2: Energise the switch heater (currents now matched)
            # and wait for thermal warmup ---
            driver.set_switch_heater(True)
            for _ in range(self._warmup_ticks):
                yield
        elif driver.get_persistent_mode():
            # Heater already on (e.g. mid-sweep persistent=False ramps): a
            # flag-based driver may still report persistent mode; clear it so
            # current/field readback follows the PSU again.
            driver.set_persistent_mode(False)

        # --- Step 3: Ramp to target using standard segment generator ---
        yield from self._ramp_generator(target_A)
        # Wait for hardware to report HOLD
        while driver.get_status() == "RAMPING":
            yield
        if driver.get_status() == "QUENCH":
            return  # stop the sequence; safety monitoring handles it

        if not persistent:
            # Switch heater stays energised; PSU holds target_A directly.
            # Generator exhausted — ramp_status() reports TARGET_REACHED now.
            return

        # --- Step 4: Cool switch heater (PSU == coil, so this is safe) ---
        driver.set_switch_heater(False)
        for _ in range(self._cooldown_ticks):
            yield

        # --- Step 5: Enter persistent mode; park PSU at zero (the cold
        # switch now carries the coil current) ---
        driver.set_persistent_mode(True)
        driver.set_ramp_rate(self._default_ramp_rate)
        driver.set_current_setpoint(0.0)
        while driver.get_status() == "RAMPING":
            yield
        # Generator exhausted — ramp_status() will check persistent_mode flag

    # ------------------------------------------------------------------
    # @monitored overrides — persistent-mode-correct current/field readback
    # ------------------------------------------------------------------

    @monitored
    def magnet_current(self) -> float:
        """Return the field-holding current in Amperes.

        While the driver is in persistent mode the PSU current is zero (it
        was ramped down in step 5 of the persistent ramp sequence) and the
        coil current is what actually holds the field, so this reads
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
        """Return True when the magnet is in persistent mode."""
        return self._driver.get_persistent_mode()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods — manual switch heater and persistent mode control
    # ------------------------------------------------------------------

    @control
    def switch_heater_on(self) -> None:
        """Energise the switch heater (manual GUI use only).

        Refused while the PSU output differs from the coil current: heating
        the switch across a mismatch forces the stored coil current through
        the resistive switch — a quench. Use ``start_ramp()`` for field
        changes; it sequences the current matching automatically.

        Raises:
            CryoSoftSafetyError: If PSU and coil currents are mismatched.
        """
        driver = self._driver  # type: ignore[attr-defined]
        psu_A = driver.get_current()
        coil_A = driver.get_coil_current()
        if abs(psu_A - coil_A) > 0.01:
            raise CryoSoftSafetyError(
                f"Refusing to energise switch heater: PSU output is "
                f"{psu_A:.3f} A but the coil holds {coil_A:.3f} A. Heating "
                f"the switch across this mismatch would quench the magnet. "
                f"Ramp the PSU to the coil current first (start_ramp does "
                f"this automatically)."
            )
        driver.set_switch_heater(True)

    @control
    def switch_heater_off(self) -> None:
        """De-energise the switch heater (manual GUI use only)."""
        self._driver.set_switch_heater(False)  # type: ignore[attr-defined]

    @control
    def enter_persistent_mode(self) -> None:
        """Park the PSU at zero with the coil holding the field (manual GUI use only).

        Only safe once the switch heater has been cooled (switch
        superconducting): the cold switch then carries the coil current while
        the PSU ramps to zero.
        """
        driver = self._driver  # type: ignore[attr-defined]
        driver.set_persistent_mode(True)
        driver.set_ramp_rate(self._default_ramp_rate)
        driver.set_current_setpoint(0.0)
