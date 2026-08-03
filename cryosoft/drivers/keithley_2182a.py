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

    def set_continuous_initiation(self, enabled: bool) -> None:
        """Turn the instrument's free-running (continuous initiation) mode on/off.

        Live commissioning (2026-07-22, GPIB0::28) found the instrument ships
        with continuous initiation on (``:INIT:CONT?`` -> 1, ``:TRIG:COUN?``
        -> ~9.9e37 — free-running). ``READ?``'s implicit ``:INIT`` is then
        always rejected as "-213 Init ignored" (harmless — ``:FETCh?`` still
        returns a fresh conversion since one is always in flight) but it fills
        the error queue on every ``get_voltage()`` call. Pinning single-shot
        mode makes each ``READ?`` do exactly what it looks like it does: one
        trigger, one fresh reading, idle in between, zero spurious errors.

        This is an instrument-state command, so under the connection-lifecycle
        standard it belongs to the arming path (a measurement VI's
        ``initiate_measurement()``), never to driver construction — building
        the Station must leave every instrument exactly as the operator left
        it.

        Args:
            enabled: True to leave the instrument free-running, False (what
                every CryoSoft reading path wants) for single-shot.
        """
        self._write(":INIT:CONT " + ("ON" if enabled else "OFF"))

    def get_voltage(self) -> float:
        """Trigger and return a single DC voltage reading in Volts.

        Issues READ? which initiates a new measurement and fetches the result.

        Returns:
            Voltage in Volts.
        """
        raw = self._query("READ?")
        # Response may contain multiple comma-separated values; take channel 1.
        vals = [v.strip() for v in raw.split(",") if v.strip()]
        try:
            return float(vals[0])
        except (IndexError, ValueError) as exc:
            # Empty or garbage response — surface as a communication error so
            # the stale-value handling upstream applies instead of a crash.
            raise CryoSoftCommunicationError(
                f"Keithley 2182A: unparseable READ? response: {raw!r}",
                vi_name="Keithley2182A",
            ) from exc

    def set_range(self, range_v: float) -> None:
        """Set the DC voltage measurement range.

        Args:
            range_v: Full-scale voltage range in Volts
                     (e.g. 0.01 for 10 mV, 0.1 for 100 mV).
        """
        self._write(f":SENS:VOLT:CHAN1:RANG {range_v:.4e}")

    def get_range(self) -> float:
        """Return the current DC voltage range setting in Volts."""
        raw = self._query(":SENS:VOLT:CHAN1:RANG?")
        try:
            return float(raw)
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 2182A: unparseable range response: {raw!r}",
                vi_name="Keithley2182A",
            ) from exc

    def get_idn(self) -> str:
        """Return the instrument identification string."""
        return self._query("*IDN?").strip()

    # ------------------------------------------------------------------
    # Connection lifecycle (the connection-lifecycle standard)
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the VISA session; the instrument is left exactly as it is.

        The driver half of the connection-lifecycle standard (see
        ``drivers/README.md``): returns the bus session, sends no
        instrument-state command, and never raises — a disconnect must
        always succeed. ``GTL`` hands the front panel back to the operator,
        which is the whole point of disconnecting. A closed driver is never
        reopened in place; the Station builds a fresh instance to reconnect.
        """
        try:
            self._instr.control_ren(pyvisa.constants.VI_GPIB_REN_ADDRESS_GTL)
        except Exception as exc:  # noqa: BLE001 — best effort, close must not raise
            log.debug("Keithley 2182A: could not return to local: %s", exc)
        try:
            self._instr.close()
        except Exception as exc:  # noqa: BLE001 — best effort, close must not raise
            log.debug("Keithley 2182A: error closing VISA session: %s", exc)

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
