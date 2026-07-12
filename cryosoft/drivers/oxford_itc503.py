# ---
# description: |
#   Real driver for the Oxford Instruments ITC 503 temperature controller.
#   Thin wrapper around pymeasure's ITC503 class.  Exposes the same API as
#   SimOxfordITC503 so VTITemperatureControllerVI and SampleTemperatureControllerVI
#   work without modification.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies:
#   - pymeasure >= 0.11
#   - pyvisa >= 1.13
# input: |
#   Instantiated with a VISA resource string (e.g. 'GPIB0::24::INSTR').
#   Sets control mode to Remote-Unlocked on init.
# process: |
#   Delegates all property access to pymeasure.instruments.oxfordinstruments.ITC503.
#   get_needle_valve() / set_needle_valve() map to the ITC503 gas_flow property
#   which controls the needle-valve / gas-flow analog output (0-99.9 %).
# output: |
#   Returns float temperature (K), setpoint (K), heater output (%), and
#   needle-valve position (%) via public API.
# last_updated: 2026-04-19
# ---

"""Real Oxford ITC 503 temperature controller driver (pymeasure wrapper)."""

from __future__ import annotations

import logging

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)


class OxfordITC503:
    """Real Oxford ITC 503 temperature controller.

    Wraps ``pymeasure.instruments.oxfordinstruments.ITC503`` and exposes
    the same public API as SimOxfordITC503.

    The needle-valve / gas-flow output (gas_flow property in pymeasure)
    corresponds to ``get_needle_valve()`` / ``set_needle_valve()`` here.

    Driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string.
    3. It is importable via cryosoft.drivers.oxford_itc503.
    """

    def __init__(self, resource_string: str) -> None:
        """Connect to the ITC 503 and set remote-unlocked mode.

        Args:
            resource_string: VISA address, e.g. ``'GPIB0::24::INSTR'``.

        Raises:
            CryoSoftCommunicationError: If pymeasure or VISA connection fails.
        """
        try:
            from pymeasure.instruments.oxfordinstruments import ITC503
        except ImportError as exc:
            raise CryoSoftCommunicationError(
                "pymeasure is required for OxfordITC503. "
                "Install with: pip install pymeasure",
                vi_name="OxfordITC503",
            ) from exc

        try:
            self._itc = ITC503(resource_string)
            # Enable programmatic control — essential for set_setpoint() to work
            self._itc.control_mode = "RU"   # Remote Unlocked
            self._itc.heater_gas_mode = "AUTO"
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"Cannot connect to ITC 503 at {resource_string}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    # ------------------------------------------------------------------
    # Public API  (matches SimOxfordITC503)
    # ------------------------------------------------------------------

    def get_temperature(self) -> float:
        """Return the current temperature from sensor 1 in Kelvin."""
        try:
            return float(self._itc.temperature_1)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read temperature_1: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_setpoint(self) -> float:
        """Return the temperature setpoint in Kelvin."""
        try:
            return float(self._itc.temperature_setpoint)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read setpoint: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_setpoint(self, setpoint: float) -> None:
        """Set the temperature setpoint.

        Args:
            setpoint: Target temperature in Kelvin. Must be >= 0.

        Raises:
            ValueError: If setpoint is negative.
        """
        if setpoint < 0.0:
            raise ValueError(f"Setpoint must be >= 0 K, got {setpoint}")
        try:
            self._itc.temperature_setpoint = setpoint
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set setpoint to {setpoint} K: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_heater_output(self) -> float:
        """Return the heater output as a percentage (0–100 %)."""
        try:
            return float(self._itc.heater)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read heater output: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_idn(self) -> str:
        """Return the instrument identification/version string.

        The ITC 503 predates SCPI and does not answer ``*IDN?``; the ISOBUS
        ``V`` command returns the firmware version string instead (same
        convention as the Oxford ILM 200 driver).
        """
        try:
            # pymeasure's Oxford base exposes the V command as `version` on
            # recent releases; fall back to a raw ask("V") if it is absent.
            version = getattr(self._itc, "version", None)
            if version is None:
                version = self._itc.ask("V")
            return str(version).strip()
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read version string: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    # ------------------------------------------------------------------
    # Needle-valve API  (VTITemperatureControllerVI only)
    # ------------------------------------------------------------------

    def get_needle_valve(self) -> float:
        """Return the needle-valve (gas-flow) position as a percentage (0–100).

        Maps to the ITC503's auxiliary analog output (gas_flow).

        Returns:
            Float in [0.0, 100.0].
        """
        try:
            return float(self._itc.gas_flow)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read gas_flow: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_needle_valve(self, position: float) -> None:
        """Set the needle-valve (gas-flow) position.

        Args:
            position: Percent open in [0.0, 99.9]. Values > 99.9 are clamped.
        """
        clamped = max(0.0, min(99.9, position))
        try:
            self._itc.gas_flow = clamped
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set gas_flow to {clamped}: {exc}",
                vi_name="OxfordITC503",
            ) from exc
