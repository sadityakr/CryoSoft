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
            self._itc = ITC503(resource_string, clear_buffer=False)
            self._itc.adapter.connection.write_termination = "\r"
            # Bus-link access, not instrument state: "Remote Unlocked" is what
            # makes the ISOBUS carry commands at all (without it set_setpoint()
            # is a silent no-op), and "Unlocked" deliberately leaves the LOC/REM
            # button live so the operator can still take the front panel.
            # close() returns the controller to Local on disconnect.
            #
            # NOTHING that changes what the cryostat is doing is sent here —
            # the connection-lifecycle standard: building the Station leaves
            # every instrument exactly as the operator left it. In particular
            # the heater/gas mode is NOT forced to AUTO here (it used to be,
            # which silently started closed-loop control on every app start);
            # that is a setup command and now lives in
            # ``SampleTemperatureControllerVI.initiate()``.
            self._itc.control_mode = "RU"   # Remote Unlocked
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
    # Connection lifecycle (the connection-lifecycle standard)
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Return the controller to Local and release the ISOBUS session.

        The driver half of the connection-lifecycle standard (see
        ``drivers/README.md``): sends no control-state command — the heater
        mode, setpoint and needle valve all keep whatever the operator left
        them on, because disconnecting is not a safe-off action (that is the
        VI's ``standby()``) — and never raises. ``LU`` (Local Unlocked)
        undoes the ``RU`` taken at construction, handing the ITC's own front
        panel back, which is the point of disconnecting.
        """
        try:
            self._itc.control_mode = "LU"   # Local Unlocked
        except Exception as exc:  # noqa: BLE001 — best effort, close must not raise
            log.debug("ITC503: could not return to local control: %s", exc)
        try:
            self._itc.adapter.connection.close()
        except Exception as exc:  # noqa: BLE001 — best effort, close must not raise
            log.debug("ITC503: error closing ISOBUS session: %s", exc)

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
            return float(self._itc.gasflow)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read gasflow: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_needle_valve(self, position: float) -> None:
        """Set the needle-valve (gas-flow) position.

        Args:
            position: Percent open in [0.0, 99.9]. Values > 99.9 are clamped.
        """
        clamped = max(0.0, min(99.9, position))
        try:
            self._itc.gasflow = clamped
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set gasflow to {clamped}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_heater_output(self, output: float) -> None:
        """Set the manual heater output percentage.

        Args:
            output: Percent of maximum voltage/power in [0.0, 99.9].
        """
        clamped = max(0.0, min(99.9, output))
        try:
            self._itc.heater = clamped
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set heater output to {clamped}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_heater_mode(self) -> str:
        """Return the heater control mode ('MANUAL' or 'AUTO')."""
        try:
            mode = self._itc.heater_gas_mode
            if mode in ("AUTO", "AM"):
                return "AUTO"
            return "MANUAL"
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read heater mode: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_heater_mode(self, mode: str) -> None:
        """Set the heater control mode to 'MANUAL' or 'AUTO'.

        Args:
            mode: Must be 'MANUAL' or 'AUTO'.
        """
        if mode not in ("MANUAL", "AUTO"):
            raise ValueError(f"Heater mode must be 'MANUAL' or 'AUTO', got {mode}")
        try:
            current_gas = self.get_needle_valve_mode()
            if mode == "AUTO":
                new_mode = "AUTO" if current_gas == "AUTO" else "AM"
            else:
                new_mode = "MA" if current_gas == "AUTO" else "MANUAL"
            self._itc.heater_gas_mode = new_mode
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set heater mode to {mode}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_needle_valve_mode(self) -> str:
        """Return the needle valve control mode ('MANUAL' or 'AUTO')."""
        try:
            mode = self._itc.heater_gas_mode
            if mode in ("AUTO", "MA"):
                return "AUTO"
            return "MANUAL"
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read needle valve mode: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_needle_valve_mode(self, mode: str) -> None:
        """Set the needle valve control mode to 'MANUAL' or 'AUTO'.

        Args:
            mode: Must be 'MANUAL' or 'AUTO'.
        """
        if mode not in ("MANUAL", "AUTO"):
            raise ValueError(f"Needle valve mode must be 'MANUAL' or 'AUTO', got {mode}")
        try:
            current_heater = self.get_heater_mode()
            if mode == "AUTO":
                new_mode = "AUTO" if current_heater == "AUTO" else "MA"
            else:
                new_mode = "AM" if current_heater == "AUTO" else "MANUAL"
            self._itc.heater_gas_mode = new_mode
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set needle valve mode to {mode}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_proportional_band(self) -> float:
        """Return the proportional band in Kelvin."""
        try:
            return float(self._itc.proportional_band)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read proportional band: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_proportional_band(self, pb: float) -> None:
        """Set the proportional band in Kelvin.

        Args:
            pb: Proportional band in Kelvin. Must be in [0.0, 1677.7].
        """
        clamped = max(0.0, min(1677.7, pb))
        try:
            self._itc.proportional_band = clamped
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set proportional band to {clamped}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_integral_action_time(self) -> float:
        """Return the integral action time in minutes."""
        try:
            return float(self._itc.integral_action_time)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read integral action time: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_integral_action_time(self, iat: float) -> None:
        """Set the integral action time in minutes.

        Args:
            iat: Integral action time in minutes. Must be in [0.0, 140.0].
        """
        clamped = max(0.0, min(140.0, iat))
        try:
            self._itc.integral_action_time = clamped
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set integral action time to {clamped}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_derivative_action_time(self) -> float:
        """Return the derivative action time in minutes."""
        try:
            return float(self._itc.derivative_action_time)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read derivative action time: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_derivative_action_time(self, dat: float) -> None:
        """Set the derivative action time in minutes.

        Args:
            dat: Derivative action time in minutes. Must be in [0.0, 273.0].
        """
        clamped = max(0.0, min(273.0, dat))
        try:
            self._itc.derivative_action_time = clamped
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set derivative action time to {clamped}: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def get_auto_pid(self) -> bool:
        """Return whether Auto-PID is enabled."""
        try:
            return bool(self._itc.auto_pid)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not read auto_pid status: {exc}",
                vi_name="OxfordITC503",
            ) from exc

    def set_auto_pid(self, enabled: bool) -> None:
        """Enable or disable Auto-PID control.

        Args:
            enabled: True to enable Auto-PID, False to disable.
        """
        try:
            self._itc.auto_pid = bool(enabled)
        except Exception as exc:
            raise CryoSoftCommunicationError(
                f"ITC503: could not set auto_pid to {enabled}: {exc}",
                vi_name="OxfordITC503",
            ) from exc
