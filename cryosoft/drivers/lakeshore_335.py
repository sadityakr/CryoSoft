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
