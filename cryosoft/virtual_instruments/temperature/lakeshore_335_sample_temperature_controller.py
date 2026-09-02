"""Lakeshore335SampleTemperatureControllerVI — calibration curve and heater range."""

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
    control behaviour. Adds calibration-curve and heater-range monitoring and
    selection, both rendered on the instrument front panel as drop-downs
    (``panel=False``: occasional commissioning/bench actions, not routine
    ones, mirroring how PID tuning is kept off the compact card).

    Heater range is what actually switches heater power on: the instrument
    powers up with range Off, so the heater delivers no power — regardless
    of ``heater_mode`` (inherited from ``SampleTemperatureControllerVI``) or
    setpoint — until the range is set to Low, Medium, or High.

    Driver contract (additions to SampleTemperatureControllerVI)
    -------------------------------------------------------------
    The ``"main"`` driver must also implement:
    * ``get_sensor_curve(sensor_input: str) -> int``
    * ``set_sensor_curve(curve: int, sensor_input: str) -> None``
    * ``get_heater_range() -> str``  — 'OFF', 'LOW', 'MEDIUM', or 'HIGH'
    * ``set_heater_range(range_setting: str) -> None``
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

    # Lakeshore 335 heater range (RANGE, see the 335 manual §4.5.1.7.8): Off
    # switches heater power off entirely; Low/Medium/High select decade power
    # steps. Power-up default is Off.
    _HEATER_RANGE_CHOICES: ClassVar[dict[str, str]] = {
        "Off": "OFF",
        "Low": "LOW",
        "Medium": "MEDIUM",
        "High": "HIGH",
    }

    def control_param_specs(self, method_name: str) -> dict[str, ParamSpec]:
        """Render ``set_curve``/``set_heater_range`` as drop-downs defaulted to the current value.

        The choice lists are static (the instrument's own curve numbering and
        heater range settings), but the *default* is the instrument's
        currently-assigned value — known only per instance — so this
        instance-level hook is used even though the choices themselves need
        no runtime data.

        Args:
            method_name: The @control method name being rendered.

        Returns:
            The relevant drop-down spec for ``set_curve`` or
            ``set_heater_range``; the inherited declaration for every other
            control.
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
        if method_name == "set_heater_range":
            return {
                "range_setting": ParamSpec(
                    type=str,
                    default=self.heater_range(),
                    choices=self._HEATER_RANGE_CHOICES,
                    description=(
                        "Heater output power range — Off switches the heater "
                        "off entirely, regardless of heater mode or setpoint"
                    ),
                )
            }
        return super().control_param_specs(method_name)

    # ------------------------------------------------------------------
    # @monitored methods — calibration curve, heater range
    # ------------------------------------------------------------------

    # Dimensionless: an instrument curve number (INCRV), not a quantity.
    @monitored(
        unit="",
        description="Calibration curve number assigned to the sample sensor input",
    )
    def curve(self) -> int:
        """Return the calibration curve number assigned to the sample sensor input."""
        return self._driver.get_sensor_curve(self._SENSOR_INPUT)  # type: ignore[attr-defined]

    # Dimensionless: a named decade power step, not a quantity.
    @monitored(
        unit="",
        description="Heater output power range: OFF, LOW, MEDIUM or HIGH",
    )
    def heater_range(self) -> str:
        """Return the heater range ('OFF', 'LOW', 'MEDIUM', or 'HIGH')."""
        return self._driver.get_heater_range()  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # @control methods — calibration curve, heater range
    # ------------------------------------------------------------------

    # The choice list is static (the 335's own curve numbering); only the
    # DEFAULT is per-instrument, which is why control_param_specs() above
    # overrides this declaration with the currently-assigned curve.
    @control(
        panel=False,
        params={
            "curve": ParamSpec(
                type=int,
                default=0,
                choices=_CURVE_CHOICES,
                description="Calibration curve assigned to the sample sensor input",
            ),
        },
    )
    def set_curve(self, curve: int) -> None:
        """Assign a calibration curve to the sample sensor input.

        Args:
            curve: Curve number (0 = None, 1-20 = Standard, 21-59 = User).
        """
        self._driver.set_sensor_curve(int(curve), self._SENSOR_INPUT)  # type: ignore[attr-defined]

    # Static choices, per-instrument default — see set_curve above.
    @control(
        panel=False,
        params={
            "range_setting": ParamSpec(
                type=str,
                default="OFF",
                choices=_HEATER_RANGE_CHOICES,
                description=(
                    "Heater output power range — Off switches the heater off "
                    "entirely, regardless of heater mode or setpoint"
                ),
            ),
        },
    )
    def set_heater_range(self, range_setting: str) -> None:
        """Set the heater range, switching heater power on or off.

        Args:
            range_setting: One of 'OFF', 'LOW', 'MEDIUM', 'HIGH'. 'OFF'
                switches the heater off entirely; the other three select
                decade power steps (see the 335 manual §4.5.1.7.8).

        Raises:
            ValueError: If ``range_setting`` is not one of the four values.
        """
        self._driver.set_heater_range(range_setting)  # type: ignore[attr-defined]
