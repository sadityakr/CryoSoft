# ---
# description: |
#   Real driver for the Keithley 705 scanner / matrix switch.
#   The 705 is a PRE-SCPI instrument driven by Keithley's own DDC command
#   language (single-letter commands terminated by 'X'). Exposes the same public
#   API as SimKeithley705: get_idn / close_channels / open_channels / open_all /
#   closed_channels.
#
#   !!! COMMAND STRINGS UNVERIFIED AGAINST HARDWARE !!!
#   No proven 705 reference script was found in the repository, so the DDC
#   command strings below (C / N / R / U0) are best-effort from the DDC family
#   convention and MUST be verified against the Keithley 705 manual at bench
#   commissioning BEFORE the first use on real hardware. The simulated twin
#   (SimKeithley705) is what the test suite runs against.
# entry_point: Not run directly; imported by the Virtual Instruments layer.
# dependencies:
#   - pyvisa >= 1.13
# input: |
#   Instantiated with a VISA resource string (e.g. 'GPIB0::17::INSTR').
#   Channel specs are opaque instrument-format strings (e.g. "1!1") passed
#   through verbatim from config; the driver does not parse route semantics.
# process: |
#   Builds DDC command strings from the channel specs and writes them over VISA.
#   Mirrors the sim's local closed-channel model so closed_channels() can report
#   what the driver has closed without an extra hardware query.
# output: |
#   None from the mutators; a str from get_idn(); a sorted list[str] from
#   closed_channels().
# last_updated: 2026-07-13
# ---

"""Real Keithley 705 scanner / matrix-switch driver (UNVERIFIED command set)."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pyvisa

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# UNVERIFIED DDC COMMAND STRINGS — verify against the Keithley 705 manual at
# bench commissioning before first use. The 705 uses single-letter DDC commands
# terminated by "X"; the forms below follow that convention but have NOT been
# confirmed on hardware:
#   close  N channels : "C<spec>,<spec>,...X"   (C = close crosspoint(s))
#   open   N channels : "N<spec>,<spec>,...X"   (N = open crosspoint(s))
#   open   all        : "RX"                    (R = reset / open all)
#   identify          : "U0X"                   (U0 = machine-status word)
# ─────────────────────────────────────────────────────────────────────────────
_CMD_EXECUTE = "X"
_CMD_CLOSE_PREFIX = "C"
_CMD_OPEN_PREFIX = "N"
_CMD_OPEN_ALL = "R" + _CMD_EXECUTE
_CMD_IDENTIFY = "U0" + _CMD_EXECUTE


class Keithley705:
    """Real Keithley 705 scanner (exclusive-mux crosspoint switch).

    Exposes the same public API as :class:`SimKeithley705`. The controlling VI
    enforces the exclusive-mux policy (open everything, then close the selected
    route); this driver only builds and sends the DDC commands and keeps a local
    model of which specs it has closed.

    .. warning::
        The DDC command strings used here are UNVERIFIED against hardware. No
        proven 705 script existed in the repository to copy them from. Verify
        every command against the Keithley 705 manual at bench commissioning
        before the first use on a real instrument.

    Driver contract:
    1. It is a Python class.
    2. ``__init__`` accepts a single VISA resource string.
    3. It is importable via ``cryosoft.drivers.keithley_705``.
    """

    def __init__(self, resource_string: str) -> None:
        """Open the VISA resource and configure terminations.

        Args:
            resource_string: VISA address, e.g. ``'GPIB0::17::INSTR'``.

        Raises:
            CryoSoftCommunicationError: If the resource cannot be opened.
        """
        self._rm = pyvisa.ResourceManager()
        try:
            self._instr = self._rm.open_resource(resource_string)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Cannot open Keithley 705 at {resource_string}: {exc}",
                vi_name="Keithley705",
            ) from exc

        self._instr.timeout = 5_000  # ms
        self._instr.write_termination = "\n"
        self._instr.read_termination = "\n"

        # Local model of closed specs (mirrors the sim) so closed_channels()
        # needs no extra hardware query.
        self._closed: set[str] = set()

    # ------------------------------------------------------------------
    # Public API (identical to SimKeithley705)
    # ------------------------------------------------------------------

    def get_idn(self) -> str:
        """Return the instrument identification / machine-status string.

        The 705 does not answer ``*IDN?`` (pre-SCPI). This queries the DDC
        machine-status word instead. UNVERIFIED — confirm at commissioning.

        Returns:
            The stripped instrument response.
        """
        return self._query(_CMD_IDENTIFY)

    def close_channels(self, specs: Sequence[str]) -> None:
        """Close (connect) the given channel specs.

        Args:
            specs: Instrument-format channel-spec strings (e.g. ``["1!1"]``),
                sent verbatim inside the DDC close command.
        """
        specs = [str(s) for s in specs]
        if not specs:
            return
        self._write(f"{_CMD_CLOSE_PREFIX}{','.join(specs)}{_CMD_EXECUTE}")
        self._closed.update(specs)

    def open_channels(self, specs: Sequence[str]) -> None:
        """Open (disconnect) the given channel specs.

        Args:
            specs: Instrument-format channel-spec strings to open. Sent verbatim
                inside the DDC open command.
        """
        specs = [str(s) for s in specs]
        if not specs:
            return
        self._write(f"{_CMD_OPEN_PREFIX}{','.join(specs)}{_CMD_EXECUTE}")
        self._closed.difference_update(specs)

    def open_all(self) -> None:
        """Open every channel (DDC reset)."""
        self._write(_CMD_OPEN_ALL)
        self._closed.clear()

    def closed_channels(self) -> list[str]:
        """Return the specs this driver has closed, sorted (local model).

        Returns:
            The sorted list of closed channel-spec strings.
        """
        return sorted(self._closed)

    def set_four_point_mode(self) -> None:
        """Set the scanner to 4-pole (4-point) mode."""
        self._write("A3X")

    def close_channel(self, channel: str) -> None:
        """Open all channels first (exclusive mux) and close a single channel.

        Args:
            channel: Channel specification string (e.g. "1").
        """
        ch = str(channel)
        self._write(f"R{_CMD_CLOSE_PREFIX}{ch}{_CMD_EXECUTE}")
        self._closed.clear()
        self._closed.add(ch)

    def open_channel(self, channel: str) -> None:
        """Open (disconnect) a single channel.

        Args:
            channel: Channel specification string (e.g. "1").
        """
        ch = str(channel)
        self._write(f"{_CMD_OPEN_PREFIX}{ch}{_CMD_EXECUTE}")
        self._closed.discard(ch)

    # ------------------------------------------------------------------
    # Private VISA helpers
    # ------------------------------------------------------------------

    def _write(self, cmd: str) -> None:
        """Write a DDC command, translating VISA errors to CryoSoft exceptions."""
        try:
            self._instr.write(cmd)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705 write failed ({cmd!r}): {exc}",
                vi_name="Keithley705",
            ) from exc

    def _query(self, cmd: str) -> str:
        """Send a DDC query and return the stripped response."""
        try:
            return self._instr.query(cmd).strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705 query failed ({cmd!r}): {exc}",
                vi_name="Keithley705",
            ) from exc
