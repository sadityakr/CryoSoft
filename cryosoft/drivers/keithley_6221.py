# ---
# description: |
#   Real driver for the Keithley 6221 AC/DC current source.
#   Implements DC source control and the full Keithley delta-mode SCPI sequence
#   (configure, arm, start, acquire) ported from the working lab implementation
#   in resources/old-drivers/Kiethley 6221/simple_delta_tk_logic.py.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies:
#   - pyvisa >= 1.13
# input: |
#   Instantiated with a VISA resource string (e.g. 'GPIB0::19::INSTR').
#   configure_and_start_delta() must be called before acquire_delta_readings().
#   DC set_current() can be used independently (e.g. during initiate/standby).
# process: |
#   Delta mode: programs 6221 + 2182A via serial relay, arms (:SOUR:DELT:ARM),
#   initiates (:INIT:IMM), then polls :CALC1:DATA:FRES? for each reading.
#   DC mode: direct :SOUR:CURR writes with OUTP ON/OFF.
# output: |
#   Returns float/bool state values and list[float] delta readings via public API.
# last_updated: 2026-04-19
# ---

"""Real Keithley 6221 AC/DC current source driver."""

from __future__ import annotations

import logging
import time

import pyvisa

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)


class Keithley6221:
    """Real Keithley 6221 current source.

    Exposes the same public API as SimKeithley6221, extended with a split
    delta-mode lifecycle used by DeltaModeMeasurementVI::

        driver.configure_and_start_delta(current, n_readings, delay, ...)
        readings = driver.acquire_delta_readings(n_readings, period)
        driver.stop_delta_mode()

    The legacy trigger_delta_mode() / get_delta_readings() pair is also
    preserved for test-harness compatibility.

    Driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string.
    3. It is importable via cryosoft.drivers.keithley_6221.
    """

    def __init__(self, resource_string: str) -> None:
        """Open the VISA resource and configure timeouts.

        Args:
            resource_string: VISA address, e.g. ``'GPIB0::19::INSTR'``.

        Raises:
            CryoSoftCommunicationError: If the resource cannot be opened.
        """
        self._rm = pyvisa.ResourceManager()
        try:
            self._instr = self._rm.open_resource(resource_string)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Cannot open Keithley 6221 at {resource_string}: {exc}",
                vi_name="Keithley6221",
            ) from exc

        self._instr.timeout = 15_000        # ms — delta ARM+INIT can be slow
        self._instr.write_termination = "\n"
        self._instr.read_termination = "\n"

        # Cached delta-mode config (used by both legacy and new interfaces)
        self._delta_high_current: float = 0.0
        self._delta_n_readings: int = 1
        self._delta_delay: float = 0.01
        self._delta_readings: list[float] = []

    # ------------------------------------------------------------------
    # DC source API  (matches SimKeithley6221)
    # ------------------------------------------------------------------

    def get_source_enabled(self) -> bool:
        """Return True if the current source output is enabled."""
        return self._query("OUTP?").strip() == "1"

    def set_source_enabled(self, enabled: bool) -> None:
        """Enable or disable the current source output.

        Args:
            enabled: True to turn the output on.
        """
        self._write("OUTP " + ("ON" if enabled else "OFF"))

    def get_current(self) -> float:
        """Return the configured source current in Amperes."""
        return float(self._query(":SOUR:CURR?"))

    def set_current(self, current: float) -> None:
        """Set the DC source current; enables output for non-zero values.

        Args:
            current: Desired current in Amperes.
        """
        self._write(f":SOUR:CURR {current:.9e}")
        self._write("OUTP " + ("ON" if current != 0.0 else "OFF"))

    def set_compliance(self, compliance_v: float) -> None:
        """Set the voltage compliance limit.

        Args:
            compliance_v: Maximum output voltage in Volts.
        """
        self._write(f":SOUR:CURR:COMP {compliance_v:.4e}")

    def get_compliance(self) -> float:
        """Return the configured voltage compliance limit in Volts."""
        return float(self._query(":SOUR:CURR:COMP?"))

    def get_idn(self) -> str:
        """Return the instrument identification string."""
        return self._query("*IDN?").strip()

    # ------------------------------------------------------------------
    # Legacy delta API  (matches SimKeithley6221 — keeps tests green)
    # ------------------------------------------------------------------

    def configure_delta_mode(
        self, high_current: float, n_readings: int, delay: float
    ) -> None:
        """Store delta-mode parameters (legacy 3-arg interface).

        On the real instrument this only caches the values; call
        start_delta_mode() or configure_and_start_delta() to program hardware.

        Args:
            high_current: Peak delta current magnitude (A).
            n_readings: Readings per acquisition call.
            delay: Delay between source transitions (s).
        """
        self._delta_high_current = float(high_current)
        self._delta_n_readings = int(n_readings)
        self._delta_delay = float(delay)
        self._delta_readings = []

    def trigger_delta_mode(self) -> None:
        """Legacy all-in-one: program, arm, start, and acquire.

        Uses the cached parameters from configure_delta_mode().
        """
        self._program_delta_mode(
            self._delta_high_current,
            self._delta_n_readings,
            self._delta_delay,
        )
        self._arm_and_start()
        self._delta_readings = self._poll_readings(
            self._delta_n_readings, self._delta_delay
        )

    def get_delta_readings(self) -> list[float]:
        """Return voltage readings from the last delta-mode acquisition."""
        return list(self._delta_readings)

    # ------------------------------------------------------------------
    # Split delta lifecycle  (used by DeltaModeMeasurementVI)
    # ------------------------------------------------------------------

    def configure_and_start_delta(
        self,
        high_current: float,
        n_readings: int,
        delay: float,
        compliance: float = 1.0,
        range_2182a: float = 0.01,
    ) -> None:
        """Configure the 6221 for delta mode and start the measurement engine.

        Combines configure_delta_mode() + configure_2182a_delta_mode() +
        start_delta() from simple_delta_tk_logic.py into a single call.

        After this returns, the instrument is armed and running.  Call
        acquire_delta_readings() to collect samples, stop_delta_mode() to
        abort.

        Args:
            high_current: Peak delta current magnitude (A). Low level = -high.
            n_readings: Stored for the matching acquire_delta_readings() call.
            delay: Inter-transition delay (s); 0 uses hardware minimum (INF).
            compliance: Voltage compliance limit (V).
            range_2182a: 2182A measurement range (V), e.g. 0.01 for 10 mV.
        """
        self._delta_high_current = high_current
        self._delta_n_readings = n_readings
        self._delta_delay = delay

        self._program_delta_mode(high_current, n_readings, delay, compliance, range_2182a)
        self._arm_and_start()

    def acquire_delta_readings(
        self, n_readings: int, period: float = 0.01
    ) -> list[float]:
        """Poll *n_readings* delta-voltage samples from the running instrument.

        The instrument must already be armed and running (call
        configure_and_start_delta() first).

        Ported from delta_acquire() in simple_delta_tk_logic.py.

        Args:
            n_readings: Number of readings to collect.
            period: Wait between successive :CALC1:DATA:FRES? polls (s).

        Returns:
            List of delta-voltage readings in Volts (length ≤ n_readings).
        """
        self._delta_readings = self._poll_readings(n_readings, period)
        return list(self._delta_readings)

    def stop_delta_mode(self) -> None:
        """Abort the running delta measurement and disable the source output."""
        for cmd in (":SOUR:SWE:ABOR", "OUTP OFF"):
            try:
                self._write(cmd)
            except CryoSoftCommunicationError:
                pass

    # ------------------------------------------------------------------
    # Private SCPI helpers
    # ------------------------------------------------------------------

    def _program_delta_mode(
        self,
        high_current: float,
        n_readings: int,
        delay: float,
        compliance: float = 1.0,
        range_2182a: float = 0.01,
    ) -> None:
        """Send the full delta-mode configuration SCPI sequence.

        Ported from configure_delta_mode() + configure_2182a_delta_mode()
        in simple_delta_tk_logic.py.  The 2182A is programmed via the
        6221's serial relay (:SYST:COMM:SER:SEND).
        """
        low_current = -high_current

        self._write(":SOUR:SWE:ABOR")                           # abort any running sweep
        self._write(f":SOUR:DELT:HIGH {high_current:.9e}")
        self._write(f":SOUR:DELT:LOW  {low_current:.9e}")
        self._write(f":SOUR:CURR:COMP {compliance:.4e}")
        self._write(":SOUR:DELT:UNIT V")

        if delay == 0.0:
            self._write(":SOUR:DELT:DELay INF")
        else:
            self._write(f":SOUR:DELT:DELay {delay:.6f}")

        self._write(":SOUR:DELT:COUN INF")                      # run continuously across sweep points
        self._write(":SOUR:DELT:CAB ON")                        # compliance abort enabled
        self._write(":SOUR:DELT:CSW OFF")                       # cold switch off
        self._write(":TRAC:POIN 65536")
        self._write(":TRAC:FEED:CONT NEXT")

        # 2182A configuration via 6221 serial relay
        relay = ":SYST:COMM:SER:SEND "
        self._write(f'{relay}":SENS1:VOLT:RANG {range_2182a:.4e}"')
        self._write(f'{relay}":SENS1:VOLT:DFIL 1"')
        self._write(f'{relay}":SENS1:VOLT:DFIL:TCON REP"')
        self._write(f'{relay}":SENS1:VOLT:DFIL:COUN 2"')
        self._write(f'{relay}":SENS1:VOLT:DFIL:WIND 0.01"')
        self._write(f'{relay}":SENS1:VOLT:LPAS 1"')
        self._write(f'{relay}":SENS1:VOLT:NPLC 2"')

        log.debug(
            "K6221 delta configured: I=±%.3e A, N=%d, delay=%.4f s, "
            "compliance=%.3f V, 2182A range=%.4f V",
            high_current, n_readings, delay, compliance, range_2182a,
        )

    def _arm_and_start(self) -> None:
        """Arm and initiate the delta measurement engine.

        Ported from start_delta() in simple_delta_tk_logic.py.
        Extends the timeout to 15 s during the init sequence.
        """
        orig = self._instr.timeout
        self._instr.timeout = 15_000
        try:
            self._write(":SOUR:DELT:ARM")
            time.sleep(1.0)
            self._write(":INIT:IMM")
            time.sleep(2.0)
            log.info("Keithley 6221 delta mode armed and running.")
        finally:
            self._instr.timeout = orig

    def _poll_readings(self, n: int, period: float) -> list[float]:
        """Collect *n* readings from the running delta engine.

        Ported from delta_acquire() in simple_delta_tk_logic.py.

        Args:
            n: Number of readings to acquire.
            period: Wait between successive polls (s).

        Returns:
            List of float voltage readings (Volts).
        """
        self._write(":TRAC:CLE")
        time.sleep(0.1)

        readings: list[float] = []
        orig = self._instr.timeout
        self._instr.timeout = 10_000        # 10 s per poll
        consecutive_failures = 0

        try:
            for i in range(n):
                try:
                    raw = self._query(":CALC1:DATA:FRES?")
                    vals = [float(v) for v in raw.strip().split(",") if v.strip()]
                    if vals:
                        readings.append(vals[0])
                        consecutive_failures = 0
                        log.debug("K6221 reading %d/%d: %.6e V", i + 1, n, vals[0])
                    else:
                        log.warning("Empty delta reading at index %d — skipping.", i)
                        consecutive_failures += 1
                except Exception as exc:
                    log.error("Delta read error at index %d: %s", i, exc)
                    consecutive_failures += 1

                if consecutive_failures >= 3:
                    log.error("3 consecutive failures — aborting acquisition early.")
                    break

                if i < n - 1:
                    time.sleep(period)
        finally:
            self._instr.timeout = orig

        return readings

    def _write(self, cmd: str) -> None:
        """Write a SCPI command, translating VISA errors to CryoSoft exceptions."""
        try:
            self._instr.write(cmd)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 6221 write failed ({cmd!r}): {exc}",
                vi_name="Keithley6221",
            ) from exc

    def _query(self, cmd: str) -> str:
        """Send a SCPI query and return the stripped response."""
        try:
            return self._instr.query(cmd).strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 6221 query failed ({cmd!r}): {exc}",
                vi_name="Keithley6221",
            ) from exc
