# ---
# description: |
#   Real driver for the Oxford Instruments Mercury iPS-M magnet power supply.
#   Communicates via PyVISA (ASRL serial or TCPIP resource string) using the
#   Oxford SCPI READ:/SET: hierarchy documented in MercuryiPS_driver.py.
#   Exposes the same API as SimOxfordIPS120 for drop-in VI compatibility.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies:
#   - pyvisa >= 1.13
# input: |
#   Instantiated with a VISA resource string. Serial default: 'ASRL10::INSTR'.
#   Ethernet alternative: 'TCPIP0::192.168.0.x::7020::SOCKET'.
#   All SCPI commands target the GRPZ (Z-axis) power supply module.
# process: |
#   All state queries use READ:DEV:GRPZ:PSU:... and set commands use
#   SET:DEV:GRPZ:PSU:...  set_current_setpoint() automatically issues
#   ACTN:RTOS so the magnet starts ramping immediately, matching the
#   auto-ramp behaviour of SimOxfordIPS120.set_current_setpoint().
# output: |
#   Returns float current/ramp-rate, str status ('HOLD'/'RAMPING'/'QUENCH'),
#   str heater state ('ON'/'OFF'), and bool persistent-mode flag.
# last_updated: 2026-04-19
# ---

"""Real Oxford Mercury iPS-M magnet power supply driver."""

from __future__ import annotations

import logging
import time

import pyvisa

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)

# Map Mercury ACTN strings to the three-state vocabulary used by the VI layer.
_ACTN_TO_STATUS: dict[str, str] = {
    "HOLD": "HOLD",
    "RTOS": "RAMPING",   # ramping to setpoint
    "RTOZ": "RAMPING",   # ramping to zero
    "CLMP": "HOLD",      # clamped — safe fallback
}


class OxfordMercuryiPS:
    """Real Oxford Mercury iPS-M magnet power supply driver.

    Exposes the same public API as SimOxfordIPS120.

    All SCPI commands address the GRPZ group (single Z-axis magnet).
    Ramp rate is in A/min — the same unit used by the ramp_segments config
    and the VI layer.

    Driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string.
    3. It is importable via cryosoft.drivers.oxford_mercury_ips.
    """

    MAX_CURRENT: float = 90.0
    MIN_CURRENT: float = -90.0

    def __init__(self, resource_string: str) -> None:
        """Open the VISA resource and configure serial settings if needed.

        Args:
            resource_string: VISA address, e.g. ``'ASRL10::INSTR'`` or
                ``'TCPIP0::192.168.1.100::7020::SOCKET'``.

        Raises:
            CryoSoftCommunicationError: If the resource cannot be opened.
        """
        self._rm = pyvisa.ResourceManager()
        try:
            self._instr = self._rm.open_resource(resource_string)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Cannot open Mercury iPS-M at {resource_string}: {exc}",
                vi_name="OxfordMercuryiPS",
            ) from exc

        self._instr.timeout = 10_000
        self._instr.write_termination = "\n"
        self._instr.read_termination = "\n"

        if resource_string.upper().startswith("ASRL"):
            # Serial port settings for RS-232 connection
            self._instr.baud_rate = 9600
            self._instr.data_bits = 8
            self._instr.stop_bits = pyvisa.constants.StopBits.one
            self._instr.parity = pyvisa.constants.Parity.none
            self._instr.flow_control = pyvisa.constants.VI_ASRL_FLOW_NONE

    # ------------------------------------------------------------------
    # Current API  (matches SimOxfordIPS120)
    # ------------------------------------------------------------------

    def get_current(self) -> float:
        """Return the current PSU output current in Amperes."""
        resp = self._query("READ:DEV:GRPZ:PSU:SIG:CURR?")
        return self._parse_float(resp, "DEV:GRPZ:PSU:SIG:CURR", "A")

    def get_current_setpoint(self) -> float:
        """Return the current setpoint in Amperes."""
        resp = self._query("READ:DEV:GRPZ:PSU:SIG:CSET?")
        return self._parse_float(resp, "DEV:GRPZ:PSU:SIG:CSET", "A")

    def set_current_setpoint(self, setpoint: float) -> None:
        """Set the target current and immediately trigger a ramp.

        Clamps to [MIN_CURRENT, MAX_CURRENT].  Automatically issues ACTN:RTOS
        so the magnet starts ramping, matching SimOxfordIPS120 behaviour.

        Args:
            setpoint: Desired current in Amperes.
        """
        clamped = max(self.MIN_CURRENT, min(self.MAX_CURRENT, setpoint))
        self._write(f"SET:DEV:GRPZ:PSU:SIG:CSET:{clamped:.6f}")
        time.sleep(0.1)
        self._write("SET:DEV:GRPZ:PSU:ACTN:RTOS")

    def set_ramp_rate(self, rate: float) -> None:
        """Set the current ramp rate.

        Args:
            rate: Ramp rate in A/min. Must be positive.
        """
        if rate <= 0:
            raise ValueError(f"Ramp rate must be positive, got {rate}")
        self._write(f"SET:DEV:GRPZ:PSU:SIG:RCST:{rate:.4f}")

    def get_status(self) -> str:
        """Return the magnet status.

        Returns:
            One of ``'HOLD'``, ``'RAMPING'``, or ``'QUENCH'``.
        """
        resp = self._query("READ:DEV:GRPZ:PSU:ACTN?")
        # Response: STAT:DEV:GRPZ:PSU:ACTN:HOLD  (or RTOS / RTOZ / CLMP)
        try:
            actn = resp.split(":")[-1].strip()
            return _ACTN_TO_STATUS.get(actn, "HOLD")
        except Exception:
            log.warning("Could not parse Mercury status from %r", resp)
            return "HOLD"

    # ------------------------------------------------------------------
    # Switch heater / persistent mode API  (matches SimOxfordIPS120)
    # ------------------------------------------------------------------

    def get_switch_heater_state(self) -> str:
        """Return ``'ON'`` if the switch heater is energised, ``'OFF'`` otherwise."""
        resp = self._query("READ:DEV:GRPZ:PSU:SIG:SWHT?")
        try:
            return resp.split(":")[-1].strip()
        except Exception:
            log.warning("Could not parse heater status from %r", resp)
            return "OFF"

    def set_switch_heater(self, state: bool) -> None:
        """Energise (True) or de-energise (False) the persistent mode switch heater.

        Args:
            state: True to turn on, False to turn off.
        """
        cmd = "SET:DEV:GRPZ:PSU:SIG:SWHN:" + ("ON" if state else "OFF")
        self._write(cmd)
        time.sleep(1.0)     # hardware acknowledgment delay from old driver

    def get_coil_current(self) -> float:
        """Return the persistent (coil) current in Amperes.

        When the switch heater is off this differs from the PSU output.
        """
        resp = self._query("READ:DEV:GRPZ:PSU:SIG:PCUR?")
        try:
            return self._parse_float(resp, "DEV:GRPZ:PSU:SIG:PCUR", "A")
        except Exception:
            # Fall back to PSU current if persistent current is unavailable
            return self.get_current()

    def get_persistent_mode(self) -> bool:
        """Return True when the magnet is in persistent mode.

        Persistent mode is inferred: heater OFF and field held by the coil.
        """
        return self.get_switch_heater_state() == "OFF"

    def set_persistent_mode(self, persistent: bool) -> None:
        """Enter or exit persistent mode.

        The VI layer (SuperconductingMagnetPersistentVI) manages this through
        the switch heater and ramp generators; this method is a no-op here.

        Args:
            persistent: True to signal entering persistent mode.
        """
        # Persistent mode on the Mercury is managed entirely via set_switch_heater()
        # and the ACTN commands. The VI handles the full sequence.
        _ = persistent

    def get_idn(self) -> str:
        """Return the instrument identification string."""
        resp = self._query("*IDN?")
        return resp.strip()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_float(self, response: str, noun: str, unit: str) -> float:
        """Extract a float value from an Oxford STAT: response.

        Expected format: ``STAT:DEV:GRPZ:PSU:SIG:CURR:12.345A``

        Args:
            response: Raw response string from the instrument.
            noun: The noun part of the command (after STAT:).
            unit: The unit suffix to strip (e.g. 'A', 'V').

        Returns:
            Parsed float value.

        Raises:
            CryoSoftCommunicationError: If parsing fails.
        """
        try:
            prefix = f"STAT:{noun}:"
            value_str = response.replace(prefix, "").strip().rstrip(unit).strip()
            return float(value_str)
        except (ValueError, AttributeError) as exc:
            raise CryoSoftCommunicationError(
                f"Mercury iPS: cannot parse float from {response!r} "
                f"(expected noun={noun!r}, unit={unit!r}): {exc}",
                vi_name="OxfordMercuryiPS",
            ) from exc

    def _write(self, cmd: str) -> None:
        try:
            self._instr.write(cmd)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Mercury iPS write failed ({cmd!r}): {exc}",
                vi_name="OxfordMercuryiPS",
            ) from exc

    def _query(self, cmd: str) -> str:
        try:
            return self._instr.query(cmd).strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Mercury iPS query failed ({cmd!r}): {exc}",
                vi_name="OxfordMercuryiPS",
            ) from exc
