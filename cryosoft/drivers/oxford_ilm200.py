# ---
# description: |
#   Real driver for the Oxford Instruments ILM 200 cryogen level meter.
#   Pure PyVISA implementation — no QCoDeS dependency.  Communicates over
#   RS-232 (ASRL) using the Oxford ISOBUS protocol (commands prefixed @N).
#   Exposes the same API as SimOxfordILM200.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies:
#   - pyvisa >= 1.13
# input: |
#   Instantiated with a VISA resource string (e.g. 'ASRL11::INSTR').
#   The ISOBUS instrument number is hardcoded to 1 (standard for ILM200).
#   Serial settings: 9600 baud, 8 data bits, 2 stop bits, no parity.
# process: |
#   All commands are prefixed with '@1' per the ISOBUS protocol.  A 70 ms
#   pause is inserted after every write before reading the response, matching
#   the timing used in the original OxfordInstruments_ILM200 driver.
# output: |
#   Returns float helium/nitrogen levels (0-100 %) and int refresh-rate mode.
# last_updated: 2026-04-19
# ---

"""Real Oxford ILM 200 cryogen level meter driver (pure PyVISA)."""

from __future__ import annotations

import logging
import time

import pyvisa

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)

_ISOBUS_NUMBER = 1      # standard instrument number for ILM200 in F008


class OxfordILM200:
    """Real Oxford ILM 200 cryogen level meter.

    Communicates via RS-232 (ASRL VISA resource) using the Oxford ISOBUS
    serial protocol.  Commands are prefixed with ``@1`` and a 70 ms settling
    delay is applied after each write.

    Exposes the same public API as SimOxfordILM200.

    Driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string.
    3. It is importable via cryosoft.drivers.oxford_ilm200.
    """

    def __init__(self, resource_string: str) -> None:
        """Open the serial VISA resource and configure port settings.

        Args:
            resource_string: VISA address, e.g. ``'ASRL11::INSTR'``.

        Raises:
            CryoSoftCommunicationError: If the resource cannot be opened.
        """
        self._rm = pyvisa.ResourceManager()
        try:
            self._instr = self._rm.open_resource(resource_string)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Cannot open ILM 200 at {resource_string}: {exc}",
                vi_name="OxfordILM200",
            ) from exc

        self._instr.timeout = 3_000
        # ILM200 serial settings
        self._instr.baud_rate = 9600
        self._instr.data_bits = 8
        self._instr.parity = pyvisa.constants.Parity.none
        self._instr.flow_control = pyvisa.constants.VI_ASRL_FLOW_NONE
        # Two stop bits — required by ILM200 protocol
        self._instr.set_visa_attribute(
            pyvisa.constants.VI_ATTR_ASRL_STOP_BITS,
            pyvisa.constants.VI_ASRL_STOP_TWO,
        )
        self._instr.write_termination = "\r"
        self._instr.read_termination = "\r"

        # Put instrument into remote-unlocked mode so control commands work
        self._set_remote(3)

    # ------------------------------------------------------------------
    # Public API  (matches SimOxfordILM200)
    # ------------------------------------------------------------------

    def get_helium_level(self) -> float:
        """Return the helium level as a percentage (0–100 %).

        Queries channel 1 (He) via the R1 command.

        Returns:
            Helium fill level in percent.
        """
        resp = self._execute("R1")
        try:
            return float(resp.replace("R", "").strip()) / 10.0
        except (ValueError, AttributeError) as exc:
            raise CryoSoftCommunicationError(
                f"ILM200: cannot parse helium level from {resp!r}: {exc}",
                vi_name="OxfordILM200",
            ) from exc

    def get_nitrogen_level(self) -> float:
        """Return the nitrogen level as a percentage (0–100 %).

        Queries channel 2 (N2) via the R2 command.

        Returns:
            Nitrogen fill level in percent.
        """
        resp = self._execute("R2")
        try:
            return float(resp.replace("R", "").strip()) / 10.0
        except (ValueError, AttributeError) as exc:
            raise CryoSoftCommunicationError(
                f"ILM200: cannot parse nitrogen level from {resp!r}: {exc}",
                vi_name="OxfordILM200",
            ) from exc

    def get_refresh_rate(self) -> int:
        """Return the current channel-1 refresh rate mode.

        Parses the status byte from the X command.

        Returns:
            0 = STANDBY, 1 = SLOW, 2 = FAST.
        """
        resp = self._execute("X")
        try:
            # Status format: XabcSuuvvwwRzz
            # Channel 1 status occupies positions 5-6 (hex byte 'uu')
            if len(resp) >= 7:
                ch1_hex = resp[5:7]
                ch1_bits = int(ch1_hex, 16)
                if ch1_bits & 0x02:
                    return 2    # FAST (continuous)
                elif ch1_bits & 0x04:
                    return 1    # SLOW (pulsed)
        except (ValueError, IndexError):
            pass
        return 0    # STANDBY or unknown

    def set_refresh_rate(self, mode: int) -> None:
        """Set the channel-1 probe refresh rate.

        Args:
            mode: 0 = STANDBY (slow), 1 = SLOW, 2 = FAST (continuous).

        Raises:
            ValueError: If mode is not 0, 1, or 2.
        """
        if mode not in (0, 1, 2):
            raise ValueError(f"Refresh rate mode must be 0, 1, or 2, got {mode}")

        self._set_remote(1)   # remote locked for control commands
        if mode == 2:
            self._execute("T1")   # continuous / fast
        else:
            self._execute("S1")   # slow pulsed (covers both 0 and 1)
        self._set_remote(3)   # back to remote unlocked

    def get_idn(self) -> str:
        """Return the instrument version string."""
        return self._execute("V").strip()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _set_remote(self, mode: int) -> None:
        """Set ISOBUS remote/local control mode.

        Args:
            mode: 0=local locked, 1=remote locked, 2=local unlocked,
                  3=remote unlocked.
        """
        try:
            self._execute(f"C{mode}")
        except Exception as exc:
            log.warning("ILM200: could not set remote mode %d: %s", mode, exc)

    def _execute(self, cmd: str) -> str:
        """Write an ISOBUS command and return the response.

        Prefixes the command with ``@{number}`` and waits 70 ms for the
        instrument to prepare its response.

        Args:
            cmd: Single-character (or short) command string, e.g. ``'R1'``.

        Returns:
            Stripped response string.
        """
        full_cmd = f"@{_ISOBUS_NUMBER}{cmd}"
        try:
            self._instr.write(full_cmd)
            time.sleep(0.07)        # 70 ms settling — from original driver
            return self._instr.read().strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"ILM200 execute failed ({cmd!r}): {exc}",
                vi_name="OxfordILM200",
            ) from exc
