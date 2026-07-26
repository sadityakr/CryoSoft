# ---
# description: |
#   VTITemperatureControllerVI: behavior-based VI for a single-sensor,
#   single-heating-loop temperature controller that also controls a needle valve
#   (VTI — variable temperature insert). Extends SampleTemperatureControllerVI
#   with needle valve @monitored and @control methods.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.sample_temperature_controller (SampleTemperatureControllerVI)
#   - cryosoft.core.decorators (monitored, control)
# input: |
#   drivers = {"main": <temperature controller driver with needle valve support>}
#   Same init_params as SampleTemperatureControllerVI.
#   On the Oxford ITC503 the needle valve is driven by an auxiliary analog
#   output on the same controller, so a single driver entry is sufficient.
# process: |
#   All temperature ramp logic is inherited unchanged from
#   SampleTemperatureControllerVI. Adds get/set for the needle valve position
#   (0–100% open) and its own AUTO/MANUAL control mode, forwarded to
#   driver.get/set_needle_valve() and get/set_needle_valve_mode().
#   set_needle_valve() is refused while mode is AUTO (the instrument's own
#   gas-flow loop ignores explicit position commands in that mode — the
#   reported real-hardware symptom this fixes). standby() extends the
#   inherited heater lifecycle (MANUAL, zero output) by switching needle
#   valve mode to MANUAL and then closing it; initiate() is inherited
#   unchanged (heater AUTO).
# output: |
#   All SampleTemperatureControllerVI outputs plus needle_valve (%) and
#   needle_valve_mode (str) via @monitored; set_needle_valve available as
#   @control (refused unless mode is MANUAL); set_needle_valve_mode
#   (@control(panel=False), AUTO/MANUAL dropdown).
# last_updated: 2026-07-26
# ---

"""VTITemperatureControllerVI — extends SampleTemperatureControllerVI with needle valve."""

from __future__ import annotations

from typing import Any

from cryosoft.core.decorators import control, monitored
from cryosoft.core.exceptions import CryoSoftSafetyError
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.temperature.sample_temperature_controller import (
    SampleTemperatureControllerVI,
)


class VTITemperatureControllerVI(SampleTemperatureControllerVI):
    """Virtual Instrument for a VTI temperature controller with needle valve.

    Identical to ``SampleTemperatureControllerVI`` in all ramp and temperature
    control behaviour. Adds needle valve monitoring and control for managing
    the cryostat VTI helium flow, and extends ``standby()`` to also close the
    needle valve (see Lifecycle below).

    Needle valve mode
    -----------------
    Like the ITC503's heater, the needle valve has its own AUTO/MANUAL
    control mode (the two are two halves of the same combined instrument
    register): in AUTO the instrument's own gas-flow control loop drives the
    valve, and ``set_needle_valve()`` is refused; switch to MANUAL first via
    ``set_needle_valve_mode()``. The instrument powers up with gas flow in
    AUTO.

    Driver contract (additions to SampleTemperatureControllerVI)
    -------------------------------------------------------------
    The ``"main"`` driver must also implement:
    * ``get_needle_valve() -> float``         — percent open (0–100)
    * ``set_needle_valve(position: float)``   — set percent open (0–100)
    * ``get_needle_valve_mode() -> str``      — 'AUTO' or 'MANUAL'
    * ``set_needle_valve_mode(str)``          — set 'AUTO' or 'MANUAL'
    """

    # Control-validation standard: inherit the temperature/rate limits and ADD
    # the needle valve bound (a physical property of the valve, 0-100 % open,
    # not setup-dependent — so it is set directly rather than from config).
    control_limits = {
        **SampleTemperatureControllerVI.control_limits,
        "set_needle_valve": {"position": "needle_valve_pct"},
    }

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._limits["needle_valve_pct"] = (0.0, 100.0)

    # ------------------------------------------------------------------
    # @monitored methods — needle valve
    # ------------------------------------------------------------------

    @monitored
    def needle_valve(self) -> float:
        """Return the needle valve position as percent open (0–100)."""
        return self._driver.get_needle_valve()  # type: ignore[attr-defined]

    @monitored
    def needle_valve_mode(self) -> str:
        """Return the needle valve control mode: 'AUTO' or 'MANUAL'."""
        return self._driver.get_needle_valve_mode()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods — needle valve
    # ------------------------------------------------------------------

    @control
    def set_needle_valve(self, position: float) -> None:
        """Set the needle valve position.

        Args:
            position: Percent open, 0.0 (fully closed) to 100.0 (fully open).

        Raises:
            CryoSoftSafetyError: If the needle valve is in AUTO mode — the
                instrument's own gas-flow control loop drives the valve and
                ignores explicit position commands. Call
                ``set_needle_valve_mode('MANUAL')`` first.
        """
        if self._driver.get_needle_valve_mode() != "MANUAL":  # type: ignore[attr-defined]
            raise CryoSoftSafetyError(
                "Cannot set needle valve position while needle valve mode is "
                "AUTO — the instrument's own gas-flow control loop drives the "
                "valve and ignores explicit position commands. Call "
                "set_needle_valve_mode('MANUAL') first."
            )
        self._driver.set_needle_valve(position)  # type: ignore[attr-defined]

    # panel=False: like heater mode, needle valve mode is an occasional
    # bench/setup choice rather than a routine sweep control.
    @control(
        panel=False,
        params={
            "mode": ParamSpec(
                type=str,
                default="AUTO",
                choices={
                    "Auto (instrument-controlled)": "AUTO",
                    "Manual (operator-set position)": "MANUAL",
                },
                description="Needle valve (gas flow) control mode",
            ),
        },
    )
    def set_needle_valve_mode(self, mode: str) -> None:
        """Set the needle valve control mode.

        Args:
            mode: 'AUTO' lets the instrument's own gas-flow control loop
                drive the valve; 'MANUAL' allows ``set_needle_valve()`` to
                command an explicit position.

        Raises:
            ValueError: If ``mode`` is not 'AUTO' or 'MANUAL'.
        """
        self._driver.set_needle_valve_mode(mode)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def standby(self) -> None:
        """Put the heater in a safe idle state and close the needle valve.

        Extends ``SampleTemperatureControllerVI.standby()`` (heater MANUAL,
        zero output) by switching the needle valve to MANUAL mode and fully
        closing it, cutting off bath helium flow to the VTI while idle. The
        mode switch must happen first: while gas flow is in AUTO (the
        instrument's power-up default), the instrument's own control loop
        drives the valve and ignores a bare position command.
        """
        super().standby()
        self._driver.set_needle_valve_mode("MANUAL")  # type: ignore[attr-defined]
        self._driver.set_needle_valve(0.0)  # type: ignore[attr-defined]
