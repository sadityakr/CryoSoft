# ---
# description: |
#   Real driver for the Keithley 2182A nanovoltmeter.
#   Communicates via PyVISA (GPIB). In delta-mode sweeps the 2182A is
#   triggered and read by the 6221 automatically; this driver is used for
#   direct voltage reads (DC mode) and range configuration.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies:
#   - pyvisa >= 1.13
# input: |
#   Instantiated with a VISA resource string (e.g. 'GPIB0::7::INSTR').
#   Call set_range() before get_voltage() to configure the measurement range.
# process: |
#   get_voltage() issues a READ? command which triggers a single measurement
#   and returns the result. set_range() sends :SENS:VOLT:CHAN1:RANG.
# output: |
#   Returns float voltage readings and the current range setting.
# last_updated: 2026-04-19
# ---

"""Real Keithley 2182A nanovoltmeter driver."""

from __future__ import annotations

import logging

import pyvisa

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)


class Keithley2182A:
    """Real Keithley 2182A nanovoltmeter.

    Exposes the same public API as SimKeithley2182A.

    Note: In Keithley delta-mode measurements the 2182A is triggered and read
    by the 6221 via the trigger bus and serial relay — this driver is used only
    for standalone DC voltage readings and range setup.

    Driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string.
    3. It is importable via cryosoft.drivers.keithley_2182a.
    """

    def __init__(self, resource_string: str) -> None:
        """Open the VISA resource.

        Args:
            resource_string: VISA address, e.g. ``'GPIB0::7::INSTR'``.

        Raises:
            CryoSoftCommunicationError: If the resource cannot be opened.
        """
        self._rm = pyvisa.ResourceManager()
        try:
            self._instr = self._rm.open_resource(resource_string)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Cannot open Keithley 2182A at {resource_string}: {exc}",
                vi_name="Keithley2182A",
            ) from exc

        self._instr.timeout = 5_000
        self._instr.write_termination = "\n"
        self._instr.read_termination = "\n"

    # ------------------------------------------------------------------
    # Public API  (matches SimKeithley2182A)
    # ------------------------------------------------------------------

    def get_voltage(self) -> float:
        """Trigger and return a single DC voltage reading in Volts.

        Issues READ? which initiates a new measurement and fetches the result.

        Returns:
            Voltage in Volts.
        """
        raw = self._query("READ?")
        # Response may contain multiple comma-separated values; take channel 1.
        vals = [v.strip() for v in raw.split(",") if v.strip()]
        return float(vals[0])

    def set_range(self, range_v: float) -> None:
        """Set the DC voltage measurement range.

        Args:
            range_v: Full-scale voltage range in Volts
                     (e.g. 0.01 for 10 mV, 0.1 for 100 mV).
        """
        self._write(f":SENS:VOLT:CHAN1:RANG {range_v:.4e}")

    def get_range(self) -> float:
        """Return the current DC voltage range setting in Volts."""
        return float(self._query(":SENS:VOLT:CHAN1:RANG?"))

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
                f"Keithley 2182A write failed ({cmd!r}): {exc}",
                vi_name="Keithley2182A",
            ) from exc

    def _query(self, cmd: str) -> str:
        try:
            return self._instr.query(cmd).strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 2182A query failed ({cmd!r}): {exc}",
                vi_name="Keithley2182A",
            ) from exc
