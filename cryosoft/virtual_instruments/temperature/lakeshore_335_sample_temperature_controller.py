# ---
# description: |
#   Lakeshore335SampleTemperatureControllerVI: extends
#   SampleTemperatureControllerVI with calibration-curve selection over the
#   Lakeshore 335's INCRV command. Curve support is specific to the Lakeshore
#   335 driver (and its sim twin) — other sample-controller drivers (e.g. the
#   Oxford ITC503) have no curve concept, so this lives in a driver-specific
#   subclass rather than the shared base.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.temperature.sample_temperature_controller
#     (SampleTemperatureControllerVI)
#   - cryosoft.core.decorators (monitored, control)
#   - cryosoft.core.plan (ParamSpec)
# input: |
#   drivers = {"main": <Lakeshore335 or SimLakeshore335 instance>}.
#   Same init_params as SampleTemperatureControllerVI.
# process: |
#   All temperature ramp logic is inherited unchanged from
#   SampleTemperatureControllerVI. Adds get/set for the calibration curve
#   assigned to sensor input A (the input every inherited @monitored method
#   already reads), forwarded to driver.get_sensor_curve() /
#   driver.set_sensor_curve().
# output: |
#   All SampleTemperatureControllerVI outputs plus curve (int, dimensionless)
#   via @monitored; set_curve available as @control, rendered as a drop-down
#   of the instrument's valid curve numbers.
# last_updated: 2026-07-22
# ---

"""Lakeshore335SampleTemperatureControllerVI — adds calibration-curve selection."""

from __future__ import annotations

from typing import ClassVar

from cryosoft.core.decorators import control, monitored
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.temperature.sample_temperature_controller import (
    SampleTemperatureControllerVI,
)


class Lakeshore335SampleTemperatureControllerVI(SampleTemperatureControllerVI):
    """Virtual Instrument for a Lakeshore 335 sample temperature controller.

    Identical to ``SampleTemperatureControllerVI`` in all ramp and temperature
    control behaviour. Adds calibration-curve monitoring and selection for the
    sample sensor input, rendered on the instrument front panel as a
    drop-down (``panel=False``: an occasional commissioning action, not a
    routine one, mirroring how PID tuning is kept off the compact card).

    Driver contract (additions to SampleTemperatureControllerVI)
    -------------------------------------------------------------
    The ``"main"`` driver must also implement:
    * ``get_sensor_curve(sensor_input: str) -> int``
    * ``set_sensor_curve(curve: int, sensor_input: str) -> None``
    """

    # The sample sensor is wired to input A on every configured setup (the
    # same input every inherited @monitored reading already uses).
    _SENSOR_INPUT: ClassVar[str] = "A"

    # Lakeshore 335 curve numbering (INCRV, see the 335 manual): 0 = none,
    # 1-20 = factory Standard curves, 21-59 = User curves.
    _CURVE_CHOICES: ClassVar[dict[str, int]] = {
        "None (0)": 0,
        **{f"Standard {n}": n for n in range(1, 21)},
        **{f"User {n}": n for n in range(21, 60)},
    }

    def control_param_specs(self, method_name: str) -> dict[str, ParamSpec]:
        """Render ``set_curve``'s curve number as a drop-down, defaulted to the current curve.

        The choice list is static (the instrument's own curve numbering), but
        the *default* is the sensor's currently-assigned curve — known only
        per instance — so this instance-level hook is used even though the
        choices themselves need no runtime data.

        Args:
            method_name: The @control method name being rendered.

        Returns:
            The curve drop-down spec for ``set_curve``; the inherited
            declaration for every other control.
        """
        if method_name == "set_curve":
            return {
                "curve": ParamSpec(
                    type=int,
                    default=self.curve(),
                    choices=self._CURVE_CHOICES,
                    description="Calibration curve assigned to the sample sensor input",
                )
            }
        return super().control_param_specs(method_name)

    # ------------------------------------------------------------------
    # @monitored methods — calibration curve
    # ------------------------------------------------------------------

    @monitored
    def curve(self) -> int:
        """Return the calibration curve number assigned to the sample sensor input."""
        return self._driver.get_sensor_curve(self._SENSOR_INPUT)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods — calibration curve
    # ------------------------------------------------------------------

    @control(panel=False)
    def set_curve(self, curve: int) -> None:
        """Assign a calibration curve to the sample sensor input.

        Args:
            curve: Curve number (0 = None, 1-20 = Standard, 21-59 = User).
        """
        self._driver.set_sensor_curve(int(curve), self._SENSOR_INPUT)  # type: ignore[attr-defined]
