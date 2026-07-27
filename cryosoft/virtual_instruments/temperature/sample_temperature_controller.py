# ---
# description: |
#   SampleTemperatureControllerVI: behavior-based VI for any single-sensor,
#   single-heating-loop temperature controller used on the sample stage.
#   No needle valve. Implements a time-based ramp generator.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (TemperatureControllerBase)
#   - cryosoft.virtual_instruments.rampable (RampableVI)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <temperature controller driver instance>}
#   init_params keys: default_ramp_rate (K/min), tolerance (K).
# process: |
#   _ramp_generator yields each tick, computing the next intermediate setpoint
#   from time.monotonic(). ramp_status() checks generator exhaustion AND hardware
#   temperature proximity to setpoint within tolerance. initiate()/standby()
#   drive the heater mode/output lifecycle standard: initiate() sets AUTO,
#   standby() sets MANUAL with zero output.
# output: |
#   Logged temperature (K), setpoint (K), heater_output (%), heater_mode
#   ('AUTO'/'MANUAL') via @monitored; set_temperature and set_ramp_rate
#   available as @control; set_heater_mode, set_heater_output, and set_pid
#   (@control(panel=False)) in the front panel only. set_heater_output is
#   refused (CryoSoftSafetyError) unless heater_mode is MANUAL.
# last_updated: 2026-07-26
# ---

"""SampleTemperatureControllerVI — behavior-based VI for sample-stage temperature control."""

from __future__ import annotations

import time
from typing import Any, Generator

from cryosoft.core.decorators import control, monitored
from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import TemperatureControllerBase
from cryosoft.virtual_instruments.rampable import RampableVI


class SampleTemperatureControllerVI(TemperatureControllerBase, RampableVI):
    """Virtual Instrument for a single-sensor, single-loop sample temperature controller.

    This VI controls the sample stage temperature. It has no needle valve.
    Use ``VTITemperatureControllerVI`` for the VTI (bath) temperature, which
    includes needle valve control.

    Ramp behaviour
    --------------
    Uses a **time-based** ramp generator:

    1. ``start_ramp(target_K)`` records the start time and starting temperature,
       then calculates an intermediate setpoint each ``advance_ramp()`` tick.
    2. ``advance_ramp()`` sends the next ``driver.set_setpoint()`` command.
    3. ``ramp_status()`` reports ``"TARGET_REACHED"`` only when the generator is
       exhausted *and* the hardware temperature is within ``tolerance`` of target.

    Driver contract
    ---------------
    The ``"main"`` driver must implement:
    * ``get_temperature() -> float``  — current temperature in Kelvin
    * ``get_setpoint() -> float``     — current setpoint in Kelvin
    * ``set_setpoint(float)``         — set target temperature
    * ``get_heater_output() -> float`` — heater power 0–100%
    * ``get_heater_mode() -> str``    — 'AUTO' (closed-loop PID) or 'MANUAL'
      (open-loop, heater output set directly)
    * ``set_heater_mode(str)``        — set 'AUTO' or 'MANUAL'
    * ``set_heater_output(float)``    — set manual heater power 0–100%; used
      by ``standby()`` to command zero power, and by the ``@control`` of the
      same name (refused unless heater mode is MANUAL)

    Both the Lakeshore 335 and Oxford ITC 503 drivers implement heater mode
    with this same two-value vocabulary (the ITC 503's combined heater/gas
    ``AUTO``/``AM``/``MA``/``MANUAL`` states are collapsed to the heater half
    at the driver layer), so it lives here on the shared base rather than a
    driver-specific subclass. A driver-specific concept that gates whether
    the heater delivers power AT ALL regardless of mode — e.g. the Lakeshore
    335's heater range, which defaults to Off at power-up — is NOT part of
    this contract; see ``Lakeshore335SampleTemperatureControllerVI``.
    """

    # Control-validation standard (see BaseVirtualInstrument): temperature
    # and ramp-rate bounds are setup properties, read from the config's
    # init_params in __init__ (missing keys mean unbounded on that side).
    control_limits = {
        "set_temperature": {"target_K": "temperature_K"},
        "set_ramp_rate": {"rate_K_per_min": "ramp_rate_K_per_min"},
    }

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._driver = drivers["main"]

        self._default_ramp_rate: float = float(init_params.get("default_ramp_rate", 5.0))
        self._tolerance: float = float(init_params.get("tolerance", 0.5))

        max_temp = init_params.get("max_temperature_K")
        self._limits["temperature_K"] = (
            float(init_params.get("min_temperature_K", 0.0)),
            float(max_temp) if max_temp is not None else None,
        )
        max_rate = init_params.get("max_ramp_rate_K_per_min")
        self._limits["ramp_rate_K_per_min"] = (
            0.0,
            float(max_rate) if max_rate is not None else None,
        )

        self._ramp_gen: Generator | None = None
        self._ramp_exhausted: bool = True
        self._ramp_target: float | None = None
        self._ramp_rate: float | None = None
        #: The last setpoint written to the controller, in kelvin — the "next
        #: setpoint" of the ramp-introspection standard (RampableVI.
        #: ramp_setpoint). A time-based ramp advances it every tick, so it
        #: trails _ramp_target for the whole ramp.
        self._ramp_setpoint: float | None = None

    # ------------------------------------------------------------------
    # RampableVI implementation
    # ------------------------------------------------------------------

    def start_ramp(self, target: float, rate: float | None = None) -> None:
        """Begin a time-based temperature ramp to *target* kelvin.

        Args:
            target: Target temperature in kelvin.
            rate: Ramp rate in K/min. If None, uses ``_default_ramp_rate``.
        """
        self._ramp_target = float(target)
        rate_per_min = float(rate) if rate is not None else self._default_ramp_rate
        self._ramp_rate = rate_per_min
        # Cleared here, not in the generator: the first next() below writes
        # the first setpoint and records it, so a stale value from the
        # previous ramp is never reported for this one.
        self._ramp_setpoint = None

        self._ramp_gen = self._ramp_generator(self._ramp_target, rate_per_min)
        self._ramp_exhausted = False
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    def advance_ramp(self) -> None:
        """Advance the ramp generator by one tick."""
        if self._ramp_gen is None or self._ramp_exhausted:
            return
        try:
            next(self._ramp_gen)
        except StopIteration:
            self._ramp_exhausted = True

    def ramp_status(self) -> str:
        """Return current ramp state.

        Returns:
            ``"IDLE"``           — no active ramp.
            ``"RAMPING"``        — generator running or hardware still settling.
            ``"TARGET_REACHED"`` — generator finished and hardware within tolerance.
        """
        if self._ramp_gen is None:
            return "IDLE"
        if not self._ramp_exhausted:
            return "RAMPING"
        if self._ramp_target is None:
            return "IDLE"
        current_T = self._driver.get_temperature()  # type: ignore[attr-defined]
        if abs(current_T - self._ramp_target) <= self._tolerance:
            return "TARGET_REACHED"
        return "RAMPING"

    def stop_ramp(self) -> None:
        """Stop the ramp: kill the generator and pin the setpoint where we are.

        The controller would otherwise keep regulating toward the
        last-commanded intermediate setpoint; pinning the setpoint to the
        current temperature freezes the system at its present state.
        """
        self._ramp_gen = None
        self._ramp_exhausted = True
        self._ramp_target = None
        self._ramp_rate = None
        self._ramp_setpoint = None
        driver = self._driver  # type: ignore[attr-defined]
        driver.set_setpoint(driver.get_temperature())

    def ramp_target(self) -> float | None:
        """Return the active temperature target in kelvin, or ``None`` when idle."""
        return self._ramp_target

    def ramp_rate(self) -> float | None:
        """Return the active ramp rate in kelvin/min, or ``None`` when idle."""
        return self._ramp_rate

    def ramp_setpoint(self) -> float | None:
        """Return the setpoint last written to the controller, in kelvin.

        A time-based ramp walks the controller's setpoint from the starting
        temperature to the target at ``ramp_rate()``, so this is where the
        controller is regulating *now* — it reaches ``ramp_target()`` only on
        the ramp's final tick. Recorded by the generator as it writes each
        setpoint; no hardware read.
        """
        return self._ramp_setpoint

    def ramp_value(self) -> float | None:
        """Return the current temperature in kelvin (the value the ramp drives)."""
        return self.temperature()

    # ------------------------------------------------------------------
    # Internal generator
    # ------------------------------------------------------------------

    def _ramp_generator(self, target: float, rate_per_min: float) -> Generator:
        driver = self._driver  # type: ignore[attr-defined]
        start_time = time.monotonic()
        start_T: float = driver.get_temperature()

        direction = 1.0 if target > start_T else -1.0
        rate_per_s = rate_per_min / 60.0

        while True:
            elapsed_s = time.monotonic() - start_time
            new_setpoint = start_T + direction * rate_per_s * elapsed_s

            if direction > 0:
                new_setpoint = min(new_setpoint, target)
            else:
                new_setpoint = max(new_setpoint, target)

            driver.set_setpoint(new_setpoint)
            self._ramp_setpoint = new_setpoint

            if new_setpoint == target:
                return
            yield

    # ------------------------------------------------------------------
    # @monitored methods
    # ------------------------------------------------------------------

    @monitored
    def temperature(self) -> float:
        """Return the current temperature in kelvin."""
        return self._driver.get_temperature()  # type: ignore[attr-defined]

    @monitored
    def setpoint(self) -> float:
        """Return the current temperature setpoint in kelvin."""
        return self._driver.get_setpoint()  # type: ignore[attr-defined]

    @monitored
    def heater_output(self) -> float:
        """Return the heater output percentage (0–100%)."""
        return self._driver.get_heater_output()  # type: ignore[attr-defined]

    @monitored
    def heater_mode(self) -> str:
        """Return the heater control mode: 'AUTO' (closed-loop PID) or 'MANUAL'."""
        return self._driver.get_heater_mode()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods
    # ------------------------------------------------------------------

    @control
    def set_ramp_rate(self, rate_K_per_min: float) -> None:
        """Change the default temperature ramp rate.

        Args:
            rate_K_per_min: New ramp rate in kelvin per minute. Must be
                positive; the setup's upper bound (if any) comes from the
                config's ``max_ramp_rate_K_per_min``.

        Raises:
            ValueError: If the rate is zero or negative.
        """
        if float(rate_K_per_min) <= 0:
            raise ValueError(
                f"Ramp rate must be positive, got {rate_K_per_min} K/min"
            )
        self._default_ramp_rate = float(rate_K_per_min)

    @control
    def set_temperature(self, target_K: float) -> None:
        """Manually command a temperature ramp (GUI use; blocked during procedures).

        Args:
            target_K: Desired temperature in kelvin.
        """
        self.start_ramp(target_K)

    # panel=False: like PID tuning below, heater mode is an occasional bench
    # setting rather than a routine sweep control, so it lives in the
    # instrument front panel, not the compact monitor card.
    @control(
        panel=False,
        params={
            "mode": ParamSpec(
                type=str,
                default="AUTO",
                choices={"Auto (closed-loop PID)": "AUTO", "Manual (open-loop)": "MANUAL"},
                description="Heater control loop mode",
            ),
        },
    )
    def set_heater_mode(self, mode: str) -> None:
        """Set the heater control mode.

        Args:
            mode: 'AUTO' for closed-loop PID control to the setpoint, or
                'MANUAL' for open-loop control at a fixed heater output
                (see the driver's manual-output setter).

        Raises:
            ValueError: If ``mode`` is not 'AUTO' or 'MANUAL'.
        """
        self._driver.set_heater_mode(mode)  # type: ignore[attr-defined]

    # panel=False: manual heater output is only meaningful while heater mode
    # is MANUAL — an occasional override, not a routine sweep control. The
    # 0-99.9% bound is the instrument's own protocol range (both the
    # Lakeshore 335 MOUT command and the ITC503 heater property clamp to it),
    # not a setup limit, so it does not go through control_limits/config.
    @control(
        panel=False,
        params={
            "output_pct": ParamSpec(
                type=float, default=0.0, unit="%", min=0.0, max=99.9,
                description="Manual heater output power",
            ),
        },
    )
    def set_heater_output(self, output_pct: float) -> None:
        """Set the manual heater output power.

        Args:
            output_pct: Percent of maximum heater power, 0.0-99.9.

        Raises:
            CryoSoftSafetyError: If heater mode is AUTO — the closed-loop
                PID computes heater output from the setpoint and ignores an
                explicit manual command. Call ``set_heater_mode('MANUAL')``
                first.
        """
        if self._driver.get_heater_mode() != "MANUAL":  # type: ignore[attr-defined]
            raise CryoSoftSafetyError(
                "Cannot set heater output while heater mode is AUTO — the "
                "closed-loop PID computes heater output from the setpoint "
                "and ignores explicit manual commands. Call "
                "set_heater_mode('MANUAL') first."
            )
        self._driver.set_heater_output(float(output_pct))  # type: ignore[attr-defined]

    # panel=False: PID tuning is an occasional bench action — it lives in the
    # instrument front panel, never on the compact monitor card. The min/max
    # on the specs are the ITC 503's own protocol bounds (the driver clamps to
    # the same ranges); they are instrument constants, not setup limits, so
    # they do not go through control_limits/config.
    @control(
        panel=False,
        params={
            "p_K": ParamSpec(
                type=float, default=10.0, unit="K", min=0.0, max=1677.7,
                description="Proportional band",
            ),
            "i_min": ParamSpec(
                type=float, default=1.0, unit="min", min=0.0, max=140.0,
                description="Integral action time",
            ),
            "d_min": ParamSpec(
                type=float, default=0.0, unit="min", min=0.0, max=273.0,
                description="Derivative action time",
            ),
        },
    )
    def set_pid(
        self, p_K: float = 10.0, i_min: float = 1.0, d_min: float = 0.0
    ) -> None:
        """Program the controller's PID loop parameters.

        Extends the driver contract: the ``"main"`` driver must also implement
        ``set_proportional_band(float)``, ``set_integral_action_time(float)``
        and ``set_derivative_action_time(float)`` (the Oxford ITC 503 driver
        and its sim twin both do).

        Args:
            p_K: Proportional band in kelvin.
            i_min: Integral action time in minutes.
            d_min: Derivative action time in minutes.
        """
        self._driver.set_proportional_band(float(p_K))  # type: ignore[attr-defined]
        self._driver.set_integral_action_time(float(i_min))  # type: ignore[attr-defined]
        self._driver.set_derivative_action_time(float(d_min))  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Put the heater under closed-loop control.

        Sets heater mode to AUTO so the PID loop drives the heater output
        toward the setpoint (the standard startup state for a measurement).

        This is the ONLY place closed-loop control is switched on. The ITC
        503 driver used to force AUTO from its own ``__init__``, so merely
        starting CryoSoft took over the heater; under the
        connection-lifecycle standard (see ``BaseVirtualInstrument``)
        building the Station changes nothing the instrument is doing, and
        the operator decides when the loop is handed to CryoSoft by
        pressing Initiate.
        """
        self._driver.set_heater_mode("AUTO")  # type: ignore[attr-defined]

    def standby(self) -> None:
        """Put the heater in a safe idle state.

        Switches heater mode to MANUAL and commands zero output, so no
        closed-loop setpoint can drive heater power while idle.
        """
        self._driver.set_heater_mode("MANUAL")  # type: ignore[attr-defined]
        self._driver.set_heater_output(0.0)  # type: ignore[attr-defined]
