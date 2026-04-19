# ---
# description: |
#   SuperconductingMagnetPersistentVI: behavior-based VI for any superconducting
#   magnet power supply that includes a persistent-mode switch heater.
#   Extends SuperconductingMagnetVI with switch heater control and a full
#   persistent-mode ramp sequence: heat switch → wait → ramp → cool switch →
#   wait → PSU to zero. Wait steps are implemented as tick-count generators
#   (never time.sleep) so they are compatible with the Orchestrator tick loop.
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
# process: |
#   Overrides start_ramp() with a persistent-mode-aware generator.
#   Adds @monitored methods for switch heater state, coil current, and
#   persistent mode flag. Adds @control methods for switch heater and
#   persistent mode transitions.
# output: |
#   All SuperconductingMagnetVI outputs plus switch_heater_state (str),
#   coil_current (A), is_persistent (bool) via @monitored.
# last_updated: 2026-04-19
# ---

"""SuperconductingMagnetPersistentVI — VI for SC magnet PSUs with switch heater."""

from __future__ import annotations

from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.magnet.superconducting_magnet import SuperconductingMagnetVI


class SuperconductingMagnetPersistentVI(SuperconductingMagnetVI):
    """Virtual Instrument for a superconducting magnet PSU with persistent-mode switch heater.

    Persistent-mode ramp sequence
    -----------------------------
    When ``start_ramp()`` is called:

    1. Turn on switch heater — wait ``_warmup_ticks`` Orchestrator ticks.
    2. Ramp PSU current to match coil current (avoids a current jump through the
       superconducting switch when opening it).
    3. Ramp to target field.
    4. Turn off switch heater — wait ``_cooldown_ticks`` ticks.
    5. Enter persistent mode: PSU current returns to zero; coil holds field.

    The wait steps use tick counters so they do not block the Orchestrator event
    loop. ``ramp_status()`` returns ``"RAMPING"`` throughout the full sequence
    and ``"TARGET_REACHED"`` only when the magnet is in persistent mode at the
    target field.

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

    # ------------------------------------------------------------------
    # RampableVI override — persistent-mode-aware
    # ------------------------------------------------------------------

    def start_ramp(self, target: float) -> None:
        """Begin a persistent-mode-aware ramp to *target* tesla.

        Args:
            target: Target field in tesla.
        """
        target_A = target * self._amperes_per_tesla
        target_A = max(self._min_current, min(self._max_current, target_A))

        self._ramp_gen = self._persistent_ramp_generator(target_A)
        self._ramp_exhausted = False
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    def ramp_status(self) -> str:
        """Return current ramp state.

        Returns:
            ``"IDLE"``           — no ramp generator active.
            ``"TARGET_REACHED"`` — generator exhausted, magnet in persistent mode
                                   at the target field.
            ``"RAMPING"``        — sequence in progress.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if self._ramp_exhausted:
            driver = self._driver  # type: ignore[attr-defined]
            if driver.get_persistent_mode():
                return "TARGET_REACHED"
            return "RAMPING"
        return "RAMPING"

    # ------------------------------------------------------------------
    # Internal persistent-mode generator
    # ------------------------------------------------------------------

    def _persistent_ramp_generator(self, target_A: float) -> Generator:
        driver = self._driver  # type: ignore[attr-defined]

        # --- Step 1: Turn on switch heater and wait for warmup ---
        driver.set_switch_heater(True)
        for _ in range(self._warmup_ticks):
            yield

        # --- Step 2: Match PSU current to coil current before opening the switch ---
        coil_A = driver.get_coil_current()
        if abs(driver.get_current() - coil_A) > 0.01:
            driver.set_ramp_rate(self._default_ramp_rate)
            driver.set_current_setpoint(coil_A)
            # Wait until driver reaches coil current
            while driver.get_status() == "RAMPING":
                yield
            while abs(driver.get_current() - coil_A) > 0.01:
                yield

        # --- Step 3: Ramp to target using standard segment generator ---
        yield from self._ramp_generator(target_A)
        # Wait for hardware to report HOLD
        while driver.get_status() == "RAMPING":
            yield

        # --- Step 4: Cool switch heater and enter persistent mode ---
        driver.set_switch_heater(False)
        for _ in range(self._cooldown_ticks):
            yield

        # --- Step 5: Enter persistent mode; ramp PSU to zero ---
        driver.set_persistent_mode(True)
        driver.set_ramp_rate(self._default_ramp_rate)
        driver.set_current_setpoint(0.0)
        while driver.get_status() == "RAMPING":
            yield
        # Generator exhausted — ramp_status() will check persistent_mode flag

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

        Warning: use ``start_ramp()`` for safe field changes. Directly
        toggling the heater while the PSU current differs from the coil
        current will cause a quench.
        """
        self._driver.set_switch_heater(True)  # type: ignore[attr-defined]

    @control
    def switch_heater_off(self) -> None:
        """De-energise the switch heater (manual GUI use only)."""
        self._driver.set_switch_heater(False)  # type: ignore[attr-defined]

    @control
    def enter_persistent_mode(self) -> None:
        """Enter persistent mode (manual GUI use only).

        Assumes the switch heater has already been cooled. The PSU will ramp
        to zero; the coil current is captured first.
        """
        driver = self._driver  # type: ignore[attr-defined]
        driver.set_persistent_mode(True)
        driver.set_ramp_rate(self._default_ramp_rate)
        driver.set_current_setpoint(0.0)

    @control
    def exit_persistent_mode(self) -> None:
        """Exit persistent mode (manual GUI use only).

        Marks persistent mode off; caller must then heat the switch and ramp the
        PSU to match the coil current before changing the field.
        """
        self._driver.set_persistent_mode(False)  # type: ignore[attr-defined]
