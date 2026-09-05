"""Real Oxford ILM 200 cryogen level meter driver (pure PyVISA)."""

from __future__ import annotations

import logging
import time

import pyvisa

from cryosoft.core.exceptions import (
    CryoSoftCommunicationError,
    CryoSoftInstrumentError,
)

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

        # No ISOBUS command is sent here. Under the connection-lifecycle
        # standard, building the Station must leave every instrument exactly
        # as the operator left it; the only command CryoSoft sends at build
        # time is the identity query. Level reads (R1/R2/X/V) work in local
        # mode, and set_refresh_rate() — the one control this driver has —
        # already takes and releases remote around its own writes.

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
    # Connection lifecycle (the connection-lifecycle standard)
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Hand the meter back to its front panel and release the VISA session.

        The driver half of the connection-lifecycle standard (see
        ``drivers/README.md``): sends no measurement-state command — the probe
        keeps whatever refresh rate it is on — and never raises. ``C2``
        (local, unlocked) is what returns control to the ILM 200's own front
        panel, which is the point of disconnecting; ``_set_remote`` already
        swallows its own failures.
        """
        self._set_remote(2)   # local & unlocked — operator has the front panel
        try:
            self._instr.close()
        except Exception as exc:  # noqa: BLE001 — best effort, close must not raise
            log.debug("ILM 200: error closing VISA session: %s", exc)

    # ------------------------------------------------------------------
    # Safe state (the safe-shutdown standard)
    # ------------------------------------------------------------------

    def safe_shutdown(self) -> None:
        """Take the level probe off continuous drive; never raises.

        The ILM 200's half of the **safe-shutdown standard** (see
        ``drivers/README.md``): idempotent, callable from any leftover state.

        Safe idle for a cryogen level meter is the pulsed refresh rate, not
        "off": the meter must keep reporting a level (that reading is a
        safety input for everything above it), but the FAST/continuous mode
        keeps the superconducting probe energised and boils helium, so it is
        the one state that must never be left behind. This drops the probe
        back to pulsed and hands the front panel back (``C2``, local
        unlocked).

        Recovers from: a probe left in FAST/continuous mode by an abandoned
        helium fill, and a meter left remote-locked.
        """
        log.info("ILM 200: safe shutdown — probe back to pulsed refresh.")
        try:
            self.set_refresh_rate(0)
        except Exception as exc:  # noqa: BLE001 — safe shutdown must never raise
            log.warning("ILM 200: safe shutdown could not set the refresh rate: %s", exc)
        self._set_remote(2)   # local & unlocked — _set_remote swallows its own failures

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
            reply = self._instr.read().strip()
        except pyvisa.VisaIOError as exc:
            raise CryoSoftCommunicationError(
                f"ILM200 execute failed ({cmd!r}): {exc}",
                vi_name="OxfordILM200",
            ) from exc
        self._check_acknowledgement(cmd, reply)
        return reply

    def _check_acknowledgement(self, cmd: str, reply: str) -> None:
        """Raise if the ISOBUS reply says the instrument refused *cmd*.

        The ILM 200's half of the **driver error-reporting standard** (see
        ``drivers/README.md``). ISOBUS has no error queue and no status byte;
        its verification is the **protocol acknowledgement**: every command is
        echoed back, and a command the instrument will not carry out — an
        unrecognised command, a channel that has no probe fitted, a control
        command it is not in a state to accept — is answered with ``?``
        followed by the command instead of the echo. Without this check that
        reply parses as garbage several lines later, or worse, as a level.

        Args:
            cmd: The command sent, without the ISOBUS ``@n`` prefix.
            reply: The instrument's stripped reply.

        Raises:
            CryoSoftInstrumentError: If the reply is a ``?`` refusal.
        """
        if not reply.startswith("?"):
            return
        log.error("ILM 200: refused command %r (reply %r)", cmd, reply)
        raise CryoSoftInstrumentError(
            f"ILM 200 refused command {cmd!r}: replied {reply!r}. An ISOBUS "
            f"'?' reply means the instrument did not carry the command out "
            f"(unknown command, unfitted channel, or wrong control mode).",
            code="?",
            instrument_message=reply,
            context=f"_execute({cmd!r})",
            vi_name="OxfordILM200",
        )
