"""Real Keithley 705 scanner / matrix-switch driver (bench-verified command set)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

import pyvisa

from cryosoft.core.exceptions import (
    CryoSoftCommunicationError,
    CryoSoftInstrumentError,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# DDC COMMAND STRINGS — VERIFIED against the Keithley 705 manual (705-901-01F,
# Table 3-7 and section 3.5.7) and confirmed on the 12t-cryo instrument at
# bench commissioning 2026-07-20. The 705 is pre-SCPI: single-letter DDC
# commands terminated by "X".
#   close  one channel : "Cnnn X"   channel number, 1..last (see U8)
#   open   one channel : "Nnnn X"
#   open   all         : "RX"       reset; also returns display to FIRST channel
#   display channel    : "Bnnn X"   moves the DISPLAY only, closes nothing
#   read   all state   : "G2X"      -> "C001,S0,C002,S1,..."  S1 = closed
#   first/last channel : "U8X"      -> "F001,L020"
#   status word        : "U4X"      -> "705<A><D><E>..."  begins with the model
#
# Three findings from that bench session drive the code below:
#
# 1. REMOTE IS MANDATORY. With REN unasserted the 705 accepts and silently
#    DISCARDS every command — no error, no NAK, and the readback is unchanged.
#    A first probe that skipped REN looked exactly like a wrong command set.
#    __init__ therefore asserts REN and every write depends on it.
# 2. ONE CHANNEL PER COMMAND. The manual's format is "Cnnn" for a single
#    channel; the comma-joined "C1,2X" form this driver used previously is not
#    in the command set. Multi-channel closes are sent as separate commands.
# 3. "Cnnn,Sn" (the G0 readback) reports the DISPLAYED channel only, NOT the
#    closed set — closing a channel does not move the display. Reading it to
#    confirm a close reports S1 only for channel 1 and silently misses every
#    other channel. Use the G2 whole-buffer dump for state.
# ─────────────────────────────────────────────────────────────────────────────
_CMD_EXECUTE = "X"
_CMD_CLOSE_PREFIX = "C"
_CMD_OPEN_PREFIX = "N"
_CMD_RESET = "R"
_CMD_OPEN_ALL = _CMD_RESET + _CMD_EXECUTE
_CMD_BUFFER_STATE = "G2" + _CMD_EXECUTE
_CMD_FIRST_LAST = "U8" + _CMD_EXECUTE
_CMD_IDENTIFY = "U4" + _CMD_EXECUTE

# The G2 buffer dump names each channel as "Cnnn,Sn"; S1 means closed.
_CLOSED_FLAG = "S1"

# Pole-mode commands. The manual gives "A3 or A4" for 4-pole; the operator's
# proven LabVIEW program uses A4, so A4 is what this driver sends.
_POLE_COMMANDS = {1: "A1", 2: "A2", 4: "A4"}

# Seconds to wait after each DDC write. The reference LabVIEW program spaces
# every VISA write with an explicit Time Delay rather than streaming commands
# back-to-back; the 705 is a 1985-era instrument whose command processor and
# relay drive are slower than the bus. Kept small enough to be invisible next
# to the relay settle time the VI already dwells for.
_WRITE_DELAY_S = 0.05


class Keithley705:
    """Real Keithley 705 scanner (exclusive-mux crosspoint switch).

    Exposes the same public API as :class:`SimKeithley705`. The controlling VI
    enforces the exclusive-mux policy (open everything, then close the selected
    route); this driver only builds and sends the DDC commands and keeps a local
    model of which specs it has closed.

    .. warning::
        The 705 has no error reporting on the bus: a command it cannot execute
        (an out-of-range channel, or any command while REMOTE is unasserted) is
        discarded silently and the readback is unchanged. Never infer success
        from the absence of an exception.

    Verification method under the **driver error-reporting standard** (see
    ``drivers/README.md``): **explicit readback**. There is no error queue and
    no acknowledgement to poll, so every method that opens or closes a channel
    reads the G2 buffer dump back and raises
    ``CryoSoftInstrumentError(code="READBACK_MISMATCH")`` when the instrument's
    own state does not match what was asked for. This is the whole of the
    standard that this instrument can support, and it is why the standard is
    written as "error queue, protocol acknowledgement, **or** explicit
    readback, and the driver documents which".

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

        # REMOTE is mandatory: without REN the 705 silently discards every
        # command (see the command-block note). Assert it once, here, so no
        # caller can produce the silent-no-op failure mode.
        #
        # Under the connection-lifecycle standard this is bus-link access
        # (a GPIB control line, not a DDC command): it changes nothing the
        # instrument is doing, no channel opens or closes, and close() drops
        # it again so the operator gets the front panel back on disconnect.
        try:
            self._instr.control_ren(pyvisa.constants.VI_GPIB_REN_ASSERT_ADDRESS)
        except (pyvisa.VisaIOError, AttributeError) as exc:
            raise CryoSoftCommunicationError(
                f"Cannot assert GPIB REMOTE on Keithley 705 at "
                f"{resource_string}: {exc}. Without REMOTE the 705 accepts and "
                f"ignores all commands, so refusing to continue.",
                vi_name="Keithley705",
            ) from exc

        # Local model of closed specs, kept as a fallback only. The instrument
        # is authoritative — closed_channels() reads the G2 buffer dump.
        self._closed: set[str] = set()

        # Set by set_pole_mode(); re-asserted on every open_all().
        self._pole_mode: int | None = None

    # ------------------------------------------------------------------
    # Public API (identical to SimKeithley705)
    # ------------------------------------------------------------------

    def get_idn(self) -> str:
        """Return the instrument identification string.

        The 705 does not answer ``*IDN?`` (pre-SCPI) and has no dedicated
        identity command. The closest equivalent is the DDC status word
        (``U4X``), which begins with the model number — e.g.
        ``"7052001006000000:"`` for a 705 in 2-pole mode.

        Do **not** use the channel readback (``U0X`` -> ``"C001,S0"``) as an
        identity: that is live channel state, so it changes as soon as the
        scanner closes a channel and would break any identity check built on it.

        Returns:
            The stripped status word, beginning with ``"705"``.
        """
        return self._query(_CMD_IDENTIFY)

    def close(self) -> None:
        """Return the scanner to LOCAL and release the VISA session.

        The driver half of the connection-lifecycle standard (see
        ``drivers/README.md``): sends no DDC command, so every channel keeps
        whatever state the operator left it in — opening the channels is
        ``standby()``'s job, not a disconnect's. Dropping REMOTE is what makes
        the 705's own front panel usable again, which is the point of
        disconnecting; it is a GPIB control line, not an instrument command.
        Never raises: a disconnect must always succeed.
        """
        try:
            self._instr.control_ren(pyvisa.constants.VI_GPIB_REN_ADDRESS_GTL)
        except Exception as exc:  # noqa: BLE001 — best effort, close must not raise
            log.debug("Keithley 705: could not return to local: %s", exc)
        try:
            self._instr.close()
        except Exception as exc:  # noqa: BLE001 — best effort, close must not raise
            log.debug("Keithley 705: error closing VISA session: %s", exc)

    def first_last_channel(self) -> tuple[int, int]:
        """Return the instrument's first and last addressable channel numbers.

        Queries ``U8X``, which answers ``"Fnnn,Lnnn"``. The channel count
        depends on the installed cards and the pole mode, so this is the only
        reliable way to learn the valid range.

        Returns:
            ``(first, last)`` channel numbers, e.g. ``(1, 20)``.

        Raises:
            CryoSoftCommunicationError: If the response cannot be parsed.
        """
        raw = self._query(_CMD_FIRST_LAST)
        try:
            first_s, last_s = raw.split(",")
            return int(first_s.lstrip("Ff")), int(last_s.lstrip("Ll"))
        except (ValueError, AttributeError) as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705: cannot parse first/last channel from {raw!r}",
                vi_name="Keithley705",
            ) from exc

    def close_channels(self, specs: Sequence[str]) -> None:
        """Close (connect) the given channels, one DDC command each.

        The 705's close command takes a single channel (``Cnnn``); there is no
        multi-channel form, so *specs* are sent as separate commands.

        Args:
            specs: Channel numbers as strings (e.g. ``["1", "005"]``). Each is
                normalised to the instrument's three-digit form.

        Raises:
            CryoSoftInstrumentError: ``READBACK_MISMATCH`` if the instrument
                does not report every requested channel closed afterwards
                (the driver error-reporting standard: readback is this
                instrument's only verification).
        """
        wanted = {self._channel_spec(spec) for spec in specs}
        for spec in specs:
            self._write(
                f"{_CMD_CLOSE_PREFIX}{self._channel_spec(spec)}{_CMD_EXECUTE}"
            )
            self._closed.add(self._channel_spec(spec))
        self._verify_closed(
            lambda actual: wanted <= actual,
            f"close_channels({list(specs)!r})",
            f"channels {sorted(wanted)} closed",
        )

    def open_channels(self, specs: Sequence[str]) -> None:
        """Open (disconnect) the given channels, one DDC command each.

        Args:
            specs: Channel numbers as strings. Each is normalised to the
                instrument's three-digit form.

        Raises:
            CryoSoftInstrumentError: ``READBACK_MISMATCH`` if any requested
                channel is still reported closed afterwards.
        """
        unwanted = {self._channel_spec(spec) for spec in specs}
        for spec in specs:
            self._write(
                f"{_CMD_OPEN_PREFIX}{self._channel_spec(spec)}{_CMD_EXECUTE}"
            )
            self._closed.discard(self._channel_spec(spec))
        self._verify_closed(
            lambda actual: not (unwanted & actual),
            f"open_channels({list(specs)!r})",
            f"channels {sorted(unwanted)} open",
        )

    def open_all(self) -> None:
        """Open every channel (DDC reset), re-asserting the pole mode.

        When a pole mode has been set, it is re-sent with the reset as a single
        command (e.g. ``"A4RX"``), mirroring the operator's proven LabVIEW
        program. Reset returns the display to the FIRST channel, and pairing the
        mode with it means the scanner cannot drift into a different pole
        configuration mid-experiment — which would silently renumber every
        channel the route table names.

        Raises:
            CryoSoftInstrumentError: ``READBACK_MISMATCH`` if any channel is
                still reported closed afterwards. This one matters most: the
                exclusive-mux policy builds every route on the assumption
                that the reset actually opened everything.
        """
        if self._pole_mode is None:
            self._write(_CMD_OPEN_ALL)
        else:
            self._write(
                f"{_POLE_COMMANDS[self._pole_mode]}{_CMD_RESET}{_CMD_EXECUTE}"
            )
        self._closed.clear()
        self._verify_closed(
            lambda actual: not actual, "open_all()", "no channels closed"
        )

    def closed_channels(self) -> list[str]:
        """Return the channels the instrument reports as closed, sorted.

        Reads the G2 whole-buffer dump (``"C001,S0,C002,S1,..."``) rather than
        the local model, so a channel closed by the front panel — or a close
        the instrument refused — is reported truthfully.

        Note the G0 readback (``"Cnnn,Sn"``) is NOT usable here: it describes
        the displayed channel only, and closing a channel does not move the
        display.

        Returns:
            Sorted three-digit channel numbers currently closed, e.g.
            ``["005"]``.

        Raises:
            CryoSoftCommunicationError: If the buffer dump cannot be parsed.
        """
        raw = self._query(_CMD_BUFFER_STATE)
        fields = [f.strip() for f in raw.split(",")]
        if len(fields) % 2:
            raise CryoSoftCommunicationError(
                f"Keithley 705: malformed channel buffer dump {raw!r}",
                vi_name="Keithley705",
            )
        closed = [
            channel.lstrip("Cc")
            for channel, status in zip(fields[::2], fields[1::2], strict=True)
            if status.upper() == _CLOSED_FLAG
        ]
        self._closed = set(closed)
        return sorted(closed)

    @staticmethod
    def _channel_spec(spec: str) -> str:
        """Normalise a channel spec to the instrument's three-digit form.

        Args:
            spec: A channel number as a string, padded or not (``"5"``/``"005"``).

        Returns:
            The zero-padded three-digit channel number.

        Raises:
            CryoSoftCommunicationError: If *spec* is not a channel number. The
                705 addresses plain numbered channels; a crosspoint-style spec
                such as ``"1!1"`` is not its format and would be silently
                ignored by the instrument.
        """
        text = str(spec).strip()
        try:
            return f"{int(text):03d}"
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705: {spec!r} is not a channel number. The 705 "
                f"addresses numbered channels (see first_last_channel()).",
                vi_name="Keithley705",
            ) from exc

    def set_pole_mode(self, poles: int) -> None:
        """Set the scanner's pole configuration.

        The pole mode determines how the installed cards' terminals group into
        addressable channels, so it changes both the channel count and what a
        given channel number physically connects. Measured on the 12t-cryo 705:
        matrix/1-pole = 40 channels, 2-pole = 20, 4-pole = 10. Set this before
        interpreting any channel number.

        Args:
            poles: 1, 2 or 4. Use 4 for four-wire measurements, where one
                channel switches all four leads together.

        Raises:
            CryoSoftCommunicationError: If *poles* is not a supported mode.
        """
        try:
            cmd = _POLE_COMMANDS[int(poles)]
        except (KeyError, ValueError, TypeError) as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705: unsupported pole mode {poles!r}; "
                f"expected one of {sorted(_POLE_COMMANDS)}.",
                vi_name="Keithley705",
            ) from exc
        self._write(cmd + _CMD_EXECUTE)
        self._pole_mode = int(poles)
        # The mode renumbers the channels, so any previously closed channel no
        # longer means what it did. Drop the local model to match the sim.
        self._closed.clear()
        log.info("Keithley 705 set to %d-pole mode", int(poles))

    def set_four_point_mode(self) -> None:
        """Set the scanner to 4-pole (4-point) mode."""
        self.set_pole_mode(4)

    def close_channel(self, channel: str) -> None:
        """Open all channels first (exclusive mux) and close a single channel.

        The reset and the close are sent as two separate commands: the 705
        executes DDC commands in a fixed hierarchy (manual Table 3-8), not in
        the order written, so a combined ``"RC005X"`` string is not a reliable
        way to express "open everything, then close 5".

        Args:
            channel: Channel number as a string (e.g. ``"5"``).

        Raises:
            CryoSoftInstrumentError: ``READBACK_MISMATCH`` if the instrument
                does not report exactly this channel closed afterwards.
        """
        ch = self._channel_spec(channel)
        self._write(_CMD_OPEN_ALL)
        self._write(f"{_CMD_CLOSE_PREFIX}{ch}{_CMD_EXECUTE}")
        self._closed.clear()
        self._closed.add(ch)
        self._verify_closed(
            lambda actual: actual == {ch},
            f"close_channel({channel!r})",
            f"only channel {ch} closed",
        )

    def open_channel(self, channel: str) -> None:
        """Open (disconnect) a single channel.

        Args:
            channel: Channel number as a string (e.g. ``"5"``).

        Raises:
            CryoSoftInstrumentError: ``READBACK_MISMATCH`` if the channel is
                still reported closed afterwards.
        """
        ch = self._channel_spec(channel)
        self._write(f"{_CMD_OPEN_PREFIX}{ch}{_CMD_EXECUTE}")
        self._closed.discard(ch)
        self._verify_closed(
            lambda actual: ch not in actual,
            f"open_channel({channel!r})",
            f"channel {ch} open",
        )

    # ------------------------------------------------------------------
    # Verification (the driver error-reporting standard)
    # ------------------------------------------------------------------

    def _verify_closed(
        self,
        holds: Callable[[set[str]], bool],
        context: str,
        expectation: str,
    ) -> None:
        """Read the channel state back and raise if *holds* does not.

        The 705's whole implementation of the **driver error-reporting
        standard** (see ``drivers/README.md``). This instrument has no error
        queue and acknowledges nothing, so a command it cannot execute — an
        out-of-range channel (IDDCO), or any command at all while REMOTE is
        unasserted — is discarded with no trace on the bus and the readback
        simply stays as it was. Reading the state back and comparing it with
        what was asked for is the only truth available, and doing it inside
        the driver is what turns "the route silently never connected" into a
        typed error at the call site.

        Args:
            holds: Predicate over the set of channels the instrument reports
                closed; True means the command took effect.
            context: The driver call being verified, e.g. ``"close_channel('5')"``.
            expectation: Plain-English description of what was expected, for
                the error message.

        Raises:
            CryoSoftInstrumentError: ``READBACK_MISMATCH`` if *holds* is False.
        """
        actual = set(self.closed_channels())
        if holds(actual):
            return
        log.error(
            "Keithley 705: %s did not take effect — expected %s, instrument "
            "reports %s closed",
            context, expectation, sorted(actual),
        )
        raise CryoSoftInstrumentError(
            f"Keithley 705 did not carry out {context}: expected {expectation}, "
            f"but the instrument reports {sorted(actual)} closed. The 705 "
            f"discards a command it cannot execute (out-of-range channel, or "
            f"REMOTE unasserted) without reporting anything on the bus.",
            code="READBACK_MISMATCH",
            instrument_message=f"closed channels: {sorted(actual)}",
            context=context,
            vi_name="Keithley705",
        )

    # ------------------------------------------------------------------
    # Safe state (the safe-shutdown standard)
    # ------------------------------------------------------------------

    def safe_shutdown(self) -> None:
        """Unconditionally open every channel; never raises.

        The 705's half of the **safe-shutdown standard** (see
        ``drivers/README.md``): idempotent, callable from any leftover state.

        Safe idle for a scanner is every channel open — nothing routed to the
        sample, so whatever a source does next reaches nothing. The pole mode
        is re-asserted with the reset, exactly as :meth:`open_all` does, so
        the channel numbering cannot drift.

        Recovers from: any set of closed channels, including ones closed from
        the front panel or left by an abandoned route.
        """
        log.info("Keithley 705: safe shutdown — opening every channel.")
        try:
            self.open_all()
        except Exception as exc:  # noqa: BLE001 — safe shutdown must never raise
            log.warning("Keithley 705: safe shutdown could not open all: %s", exc)

    # ------------------------------------------------------------------
    # Private VISA helpers
    # ------------------------------------------------------------------

    def _write(self, cmd: str) -> None:
        """Write a DDC command, then pause for the instrument to process it.

        The 705 acknowledges nothing, so the pause is the only backpressure
        available: streaming commands back-to-back risks the instrument
        dropping one with no indication. The reference LabVIEW program spaces
        its writes the same way.
        """
        try:
            self._instr.write(cmd)
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705 write failed ({cmd!r}): {exc}",
                vi_name="Keithley705",
            ) from exc
        time.sleep(_WRITE_DELAY_S)

    def _query(self, cmd: str) -> str:
        """Send a DDC query and return the stripped response."""
        try:
            return self._instr.query(cmd).strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705 query failed ({cmd!r}): {exc}",
                vi_name="Keithley705",
            ) from exc
