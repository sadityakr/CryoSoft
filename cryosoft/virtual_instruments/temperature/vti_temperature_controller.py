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
#   (0–100% open) which is forwarded to driver.get_needle_valve() /
#   driver.set_needle_valve().
# output: |
#   All SampleTemperatureControllerVI outputs plus needle_valve (%) via
#   @monitored; set_needle_valve available as @control.
# last_updated: 2026-04-19
# ---

"""VTITemperatureControllerVI — extends SampleTemperatureControllerVI with needle valve."""

from __future__ import annotations

from typing import Any

from cryosoft.core.decorators import control, monitored
from cryosoft.virtual_instruments.temperature.sample_temperature_controller import (
    SampleTemperatureControllerVI,
)


class VTITemperatureControllerVI(SampleTemperatureControllerVI):
    """Virtual Instrument for a VTI temperature controller with needle valve.

    Identical to ``SampleTemperatureControllerVI`` in all ramp and temperature
    control behaviour. Adds needle valve monitoring and control for managing
    the cryostat VTI helium flow.

    Driver contract (additions to SampleTemperatureControllerVI)
    -------------------------------------------------------------
    The ``"main"`` driver must also implement:
    * ``get_needle_valve() -> float``         — percent open (0–100)
    * ``set_needle_valve(position: float)``   — set percent open (0–100)
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)

    # ------------------------------------------------------------------
    # @monitored methods — needle valve
    # ------------------------------------------------------------------

    @monitored
    def needle_valve(self) -> float:
        """Return the needle valve position as percent open (0–100)."""
        return self._driver.get_needle_valve()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods — needle valve
    # ------------------------------------------------------------------

    @control
    def set_needle_valve(self, position: float) -> None:
        """Set the needle valve position.

        Args:
            position: Percent open, 0.0 (fully closed) to 100.0 (fully open).
        """
        self._driver.set_needle_valve(position)  # type: ignore[attr-defined]
