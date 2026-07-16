# ---
# description: |
#   Real driver for the Lakeshore 335 temperature controller.
#   Pure PyVISA implementation communicating over GPIB. Exposes the same
#   public API as SimOxfordITC503 (minus needle-valve) so SampleTemperatureControllerVI
#   works without modification.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies:
#   - pyvisa >= 1.13
# input: |
#   Instantiated with a VISA resource string (e.g. 'GPIB0::12::INSTR').
#   Reads temperature from input channel A; controls setpoint and heater on output 1.
# process: |
#   All commands are standard Lakeshore SCPI. get_temperature() queries KRDG? A.
#   get/set_setpoint() use SETP? 1 / SETP 1,<val>. get_heater_output() uses HTR? 1.
# output: |
#   Returns float temperature (K), setpoint (K), and heater output (%) via public API.
# last_updated: 2026-04-19
# ---

"""Real Lakeshore 335 temperature controller driver (pure PyVISA)."""

from __future__ import annotations

import logging

import pyvisa

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)


class Lakeshore335:
    """Real Lakeshore 335 temperature controller.

    Reads temperature from input channel A and controls heater output 1.
    Exposes the same public API as SimOxfordITC503 (excluding needle-valve
    methods, which are VTI-only), so SampleTemperatureControllerVI works
    with this driver without modification.

    Driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string.
    3. It is importable via cryosoft.drivers.lakeshore_335.
    """

    def __init__(self, resource_string: str) -> None:
        """Open the VISA resource and configure timeouts.

        Args:
            resource_string: VISA address, e.g. ``'GPIB0::12::INSTR'``.

        Raises:
            CryoSoftCommunicationError: If the resource cannot be opened.
        """
        self._rm = pyvisa.ResourceManager()
        try:
            self._instr = self._rm.open_resource(resource_string)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Cannot open Lakeshore 335 at {resource_string}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

        self._instr.timeout = 5_000
        self._instr.write_termination = "\n"
        self._instr.read_termination = "\n"

    # ------------------------------------------------------------------
    # Public API  (matches SimOxfordITC503 subset used by SampleTemperatureControllerVI)
    # ------------------------------------------------------------------

    def get_temperature(self) -> float:
        """Return the current temperature from input channel A in Kelvin.

        Returns:
            Temperature in Kelvin.
        """
        raw = self._query("KRDG? A")
        try:
            return float(raw)
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse temperature from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def get_setpoint(self) -> float:
        """Return the temperature setpoint for output 1 in Kelvin.

        Returns:
            Setpoint in Kelvin.
        """
        raw = self._query("SETP? 1")
        try:
            return float(raw)
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse setpoint from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def set_setpoint(self, setpoint: float) -> None:
        """Set the temperature setpoint for output 1.

        Args:
            setpoint: Target temperature in Kelvin. Must be >= 0.

        Raises:
            ValueError: If setpoint is negative.
        """
        if setpoint < 0.0:
            raise ValueError(f"Setpoint must be >= 0 K, got {setpoint}")
        self._write(f"SETP 1,{setpoint:.4f}")

    def get_heater_output(self) -> float:
        """Return the heater output for output 1 as a percentage (0–100 %).

        Returns:
            Heater output percent.
        """
        raw = self._query("HTR? 1")
        try:
            return float(raw)
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse heater output from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def get_idn(self) -> str:
        """Return the instrument identification string."""
        return self._query("*IDN?").strip()

    def set_heater_output(self, output: float) -> None:
        """Set the manual heater output percentage.

        Args:
            output: Percent of maximum power in [0.0, 99.9].
        """
        clamped = max(0.0, min(99.9, output))
        self._write(f"MOUT 1,{clamped:.2f}")

    def get_heater_mode(self) -> str:
        """Return the heater control mode ('MANUAL' or 'AUTO')."""
        raw = self._query("OUTMODE? 1")
        try:
            mode = int(raw.split(",")[0])
            if mode == 3:
                return "MANUAL"
            elif mode == 1:
                return "AUTO"
            else:
                return f"OTHER({mode})"
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse OUTMODE from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def set_heater_mode(self, mode: str) -> None:
        """Set the heater control mode to 'MANUAL' or 'AUTO'.

        Args:
            mode: Must be 'MANUAL' or 'AUTO'.
        """
        if mode not in ("MANUAL", "AUTO"):
            raise ValueError(f"Heater mode must be 'MANUAL' or 'AUTO', got {mode}")

        # Avoid redundant writes that interrupt control loops
        try:
            if self.get_heater_mode() == mode:
                return
        except Exception:
            pass

        raw = self._query("OUTMODE? 1")
        try:
            parts = raw.split(",")
            input_ch = int(parts[1])
            powerup = int(parts[2])
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse OUTMODE from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

        target_mode = 3 if mode == "MANUAL" else 1
        self._write(f"OUTMODE 1,{target_mode},{input_ch},{powerup}")
        import time
        time.sleep(0.2)  # Allow control loop to reinitialize and settle

    def get_proportional_band(self) -> float:
        """Return the proportional band (P value) for output 1."""
        raw = self._query("PID? 1")
        try:
            return float(raw.split(",")[0])
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse PID from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def set_proportional_band(self, pb: float) -> None:
        """Set the proportional band (P value) for output 1.

        P is clamped to [0.0, 1000.0].
        """
        pb_clamped = max(0.0, min(1000.0, pb))
        raw = self._query("PID? 1")
        try:
            parts = raw.split(",")
            i_val = float(parts[1])
            d_val = float(parts[2])
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse PID from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc
        self._write(f"PID 1,{pb_clamped:.1f},{i_val:.1f},{d_val:.1f}")

    def get_integral_action_time(self) -> float:
        """Return the integral action time (I value) for output 1."""
        raw = self._query("PID? 1")
        try:
            return float(raw.split(",")[1])
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse PID from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def set_integral_action_time(self, iat: float) -> None:
        """Set the integral action time (I value) for output 1.

        I is clamped to [0.0, 1000.0].
        """
        iat_clamped = max(0.0, min(1000.0, iat))
        raw = self._query("PID? 1")
        try:
            parts = raw.split(",")
            p_val = float(parts[0])
            d_val = float(parts[2])
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse PID from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc
        self._write(f"PID 1,{p_val:.1f},{iat_clamped:.1f},{d_val:.1f}")

    def get_derivative_action_time(self) -> float:
        """Return the derivative action time (D value) for output 1."""
        raw = self._query("PID? 1")
        try:
            return float(raw.split(",")[2])
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse PID from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def set_derivative_action_time(self, dat: float) -> None:
        """Set the derivative action time (D value) for output 1.

        D is clamped to [0.0, 200.0].
        """
        dat_clamped = max(0.0, min(200.0, dat))
        raw = self._query("PID? 1")
        try:
            parts = raw.split(",")
            p_val = float(parts[0])
            i_val = float(parts[1])
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse PID from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc
        self._write(f"PID 1,{p_val:.1f},{i_val:.1f},{dat_clamped:.1f}")

    def get_auto_pid(self) -> bool:
        """Return whether Autotuning is active on output 1."""
        raw = self._query("TUNEST? 1")
        try:
            active = int(raw.split(",")[0])
            return active == 1
        except (ValueError, IndexError) as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse TUNEST from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def set_auto_pid(self, enabled: bool) -> None:
        """Enable or disable Autotuning on output 1."""
        if enabled:
            self._write("ATUNE 1,2")
        else:
            raw = self._query("OUTMODE? 1")
            self._write(f"OUTMODE 1,{raw}")

    def get_sensor_curve(self, sensor_input: str = "A") -> int:
        """Return the curve number assigned to the sensor input.

        Args:
            sensor_input: Sensor input channel ('A' or 'B', default 'A').

        Returns:
            The assigned curve number.
        """
        ch = str(sensor_input).upper()
        if ch not in ("A", "B"):
            raise ValueError(f"Sensor input must be 'A' or 'B', got {sensor_input}")
        raw = self._query(f"INCRV? {ch}")
        try:
            return int(raw)
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335: cannot parse curve from {raw!r}: {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def set_sensor_curve(self, curve: int, sensor_input: str = "A") -> None:
        """Assign a temperature sensor curve to a sensor input.

        Args:
            curve: Curve number (0 = None, 1-20 = Standard, 21-59 = User).
            sensor_input: Sensor input channel ('A' or 'B', default 'A').
        """
        ch = str(sensor_input).upper()
        if ch not in ("A", "B"):
            raise ValueError(f"Sensor input must be 'A' or 'B', got {sensor_input}")
        if not (0 <= curve <= 59):
            raise ValueError(f"Curve number must be in [0, 59], got {curve}")
        self._write(f"INCRV {ch},{curve}")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _write(self, cmd: str) -> None:
        try:
            self._instr.write(cmd)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335 write failed ({cmd!r}): {exc}",
                vi_name="Lakeshore335",
            ) from exc

    def _query(self, cmd: str) -> str:
        try:
            return self._instr.query(cmd).strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Lakeshore 335 query failed ({cmd!r}): {exc}",
                vi_name="Lakeshore335",
            ) from exc
