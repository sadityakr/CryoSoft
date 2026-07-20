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
#   Delta mode: forces the serial relay to known-good settings, verifies the
#   2182A responds on it (:SOUR:DELT:NVPR?), enables the CALC1 stage that
#   produces delta readings, programs 6221 + 2182A via serial relay, arms
#   (:SOUR:DELT:ARM), initiates (:INIT:IMM), then polls :CALC1:DATA:FRES?
#   for each reading.
#   DC mode: direct :SOUR:CURR writes with OUTP ON/OFF.
# output: |
#   Returns float/bool state values and list[float] delta readings via public API.
#   set_current() and stop_delta_mode() both reassert :SOUR:CURR:MODE FIX so
#   the instrument recovers to plain DC mode regardless of what mode a prior
#   measurement VI sharing this driver left it in (shared-instrument mode
#   discipline).
# last_updated: 2026-07-20
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
        return self._query_float(":SOUR:CURR?")

    def set_current(self, current: float) -> None:
        """Set the DC source current; enables output for non-zero values.

        Unconditionally reasserts fixed-current mode first, mirroring how
        ``_program_delta_mode()`` always leads with ``:SOUR:SWE:ABOR``. This
        makes the call self-recovering regardless of whether the instrument
        was previously left in delta mode by another measurement VI sharing
        this same physical 6221 (see the "shared-instrument mode discipline"
        standard in ``virtual_instruments/measurement/README.md``) — callers
        never need to know or care what mode the instrument was last in.

        Args:
            current: Desired current in Amperes.
        """
        self._write(":SOUR:CURR:MODE FIX")
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
        return self._query_float(":SOUR:CURR:COMP?")

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
        compliance_abort: bool = True,
        cold_switch: bool = False,
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
            compliance_abort: Abort the delta run if the source hits compliance
                (``:SOUR:DELT:CAB``).
            cold_switch: Enable cold-switching between current reversals
                (``:SOUR:DELT:CSW``).

        Raises:
            CryoSoftCommunicationError: If the 6221 does not detect a 2182A
                on its RS-232 serial relay (``:SOUR:DELT:NVPR?`` returns 0).
        """
        self._delta_high_current = high_current
        self._delta_n_readings = n_readings
        self._delta_delay = delay

        self._program_delta_mode(
            high_current, n_readings, delay, compliance, range_2182a,
            compliance_abort, cold_switch,
        )
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
        """Abort the running delta measurement and return to a plain idle state.

        Reasserts fixed-current mode (not just aborting the sweep) so the
        instrument is left in the same documented idle baseline regardless of
        which measurement method runs next — see the "shared-instrument mode
        discipline" standard in ``virtual_instruments/measurement/README.md``.
        This is defense-in-depth: ``set_current()`` is already self-recovering
        on its own, but a human inspecting the instrument between runs (or a
        future driver method that doesn't go through ``set_current()``)
        should still find it in a known state.
        """
        for cmd in (":SOUR:SWE:ABOR", ":SOUR:CURR:MODE FIX", "OUTP OFF"):
            try:
                self._write(cmd)
            except CryoSoftCommunicationError:
                pass

    # ------------------------------------------------------------------
    # Private SCPI helpers
    # ------------------------------------------------------------------

    # RS-232 relay parameters the 2182A must be configured to match (Tektronix/
    # Keithley's documented delta-mode setup: 19.2k baud, XON/XOFF flow
    # control, CR terminator — see "How to configure the Model 6220-6221 and
    # 2182A for Delta Mode"). Fixed protocol constants, not a per-setup value.
    _RELAY_BAUD = 19200
    _RELAY_TERM = "CR"
    _RELAY_PACE = "XON"

    def _program_delta_mode(
        self,
        high_current: float,
        n_readings: int,
        delay: float,
        compliance: float = 1.0,
        range_2182a: float = 0.01,
        compliance_abort: bool = True,
        cold_switch: bool = False,
    ) -> None:
        """Send the full delta-mode configuration SCPI sequence.

        Ported from configure_delta_mode() + configure_2182a_delta_mode()
        in simple_delta_tk_logic.py, with two additions found necessary on
        real hardware (see commissioning incident
        2026-07-20-delta-mode-hang-12t-cryo.md):

        1. The 6221's serial relay port is explicitly forced to the
           documented baud/terminator/flow-control settings on every call,
           instead of trusting whatever was last left on the instrument —
           a flow-control mismatch here manifests on the 2182A as
           intermittent "DATA CORRUPT/STALE" and produces garbage readings.
        2. ``:SOUR:DELT:NVPR?`` is checked before arming and raises a clear
           ``CryoSoftCommunicationError`` if the 6221 does not see a 2182A on
           the relay — this turns a silent misconfiguration (2182A powered
           off, cabled wrong, or left in GPIB mode on its own front panel)
           into an immediate, actionable failure instead of a confusing
           overflow/timeout discovered several calls later.

        The 2182A itself is programmed via the 6221's serial relay
        (:SYST:COMM:SER:SEND); nothing here opens a direct GPIB session to
        the 2182A, by design — the 2182A must be left on RS-232 for delta
        mode to reach it at all.
        """
        low_current = -high_current

        self._write(":SOUR:SWE:ABOR")                           # abort any running sweep
        time.sleep(0.3)                                         # let a prior run actually stop cycling
                                                                 # before reconfiguring — re-arming while
                                                                 # the old delta cycle is still winding down
                                                                 # produced "Query INTERRUPTED" (-410) on
                                                                 # the very next query below.
        self._write("*CLS")                                     # clean state: clear stale errors/events

        # Force the relay's serial settings to the documented values so a
        # leftover mismatch from a prior session can't silently corrupt data.
        self._write(f":SYST:COMM:SER:BAUD {self._RELAY_BAUD}")
        self._write(f":SYST:COMM:SER:TERM {self._RELAY_TERM}")
        self._write(f":SYST:COMM:SER:PACE {self._RELAY_PACE}")

        if self._query(":SOUR:DELT:NVPR?").strip() != "1":
            raise CryoSoftCommunicationError(
                "Keithley 6221: no 2182A detected on the RS-232 serial relay "
                "(:SOUR:DELT:NVPR? returned 0). Check that the 2182A is "
                "powered on, cabled to the 6221's RS-232 port, and set to "
                "RS-232 (not GPIB) on its own front panel.",
                vi_name="Keithley6221",
            )

        # The mX+b/delta calculation stage (CALC1) must be enabled or every
        # subsequent :CALC1:DATA:FRESh? read blocks forever waiting for a
        # reading that can never arrive — and because SCPI commands are
        # processed in order, that one stuck query jams the 6221's entire
        # command queue behind it (looks like a dead instrument on GPIB,
        # requires a power cycle to clear).
        self._write(":CALC1:STAT ON")

        self._write(f":SOUR:DELT:HIGH {high_current:.9e}")
        self._write(f":SOUR:DELT:LOW  {low_current:.9e}")
        self._write(f":SOUR:CURR:COMP {compliance:.4e}")
        self._write(":UNIT V")                                  # reading units (pymeasure delta_unit); NOT :SOUR:DELT:UNIT — that header doesn't exist and errors -113

        if delay == 0.0:
            self._write(":SOUR:DELT:DELay INF")
        else:
            self._write(f":SOUR:DELT:DELay {delay:.6f}")

        self._write(":SOUR:DELT:COUN INF")                      # run continuously across sweep points
        self._write(f":SOUR:DELT:CAB {'ON' if compliance_abort else 'OFF'}")  # compliance abort
        self._write(f":SOUR:DELT:CSW {'ON' if cold_switch else 'OFF'}")       # cold switch
        self._write(":TRAC:CLE")                                # drop any stale buffer contents
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

    def _query_float(self, cmd: str) -> float:
        """Query and parse a float, treating garbage responses as comm errors.

        A malformed response (electrical noise, mixed-up read buffer) would
        otherwise raise a bare ValueError that bypasses the stale-value
        handling in Station.get_state().
        """
        raw = self._query(cmd)
        try:
            return float(raw)
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 6221: unparseable response to {cmd!r}: {raw!r}",
                vi_name="Keithley6221",
            ) from exc
