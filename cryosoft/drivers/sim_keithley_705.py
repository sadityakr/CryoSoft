# ---
# description: |
#   Simulated driver for the Keithley 705 scanner / matrix switch.
#   Models an EXCLUSIVE-MUX crosspoint switch as a set of closed channel-spec
#   strings: close_channels() adds specs, open_channels() removes them, and
#   open_all() clears the set. No VISA dependency — pure Python simulation.
# entry_point: Not run directly; imported by the Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Channel specs are 705
#   channel numbers as strings ("5" or "005"), normalised to three digits and
#   range-checked against the installed channel count exactly as the real 705.
# process: |
#   Maintains self._closed, the set of currently-closed channel specs. The
#   inspection helper closed_channels() (sorted list) lets tests assert exclusive
#   selection without hardware. A _simulate_error flag raises on reads for
#   error-injection tests.
# output: |
#   None from the mutators; a str from get_idn(); a sorted list[str] from the
#   closed_channels() inspection helper.
# last_updated: 2026-07-13
# ---

"""Simulated Keithley 705 scanner / matrix-switch driver."""

from __future__ import annotations

from collections.abc import Sequence

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimKeithley705:
    """Simulated Keithley 705 scanner (exclusive-mux crosspoint switch).

    Tracks the set of closed channel specs. The controlling VI enforces the
    exclusive-mux policy (open everything, then close the selected route), so
    this driver only records what has been opened and closed; it never parses
    route semantics.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. ``__init__`` accepts a single VISA resource string (ignored for sim).
    3. It is importable via ``cryosoft.drivers.sim_keithley_705``.

    Its public API is identical to the real :class:`Keithley705` driver
    (conformance parity check).
    """

    def __init__(self, resource_string: str, last_channel: int = 20) -> None:
        """Initialise the simulated Keithley 705.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::17::INSTR'). Ignored.
            last_channel: Highest addressable channel, mirroring the installed
                card set the real instrument reports via ``U8X`` (the 12t-cryo
                705 reports ``F001,L020``).
        """
        _ = resource_string  # Explicitly ignored per driver contract
        self._closed: set[str] = set()
        self._pole_mode: int = 2  # Default 2-pole configuration
        self._simulate_error: bool = False
        self._first_channel: int = 1
        self._last_channel: int = int(last_channel)

    # ------------------------------------------------------------------
    # Public API (identical to the real Keithley705)
    # ------------------------------------------------------------------

    def get_idn(self) -> str:
        """Return the simulated DDC status word.

        Mirrors the real driver, which identifies via ``U4X`` — a status word
        beginning with the model number. The 705 has no ``*IDN?`` and no
        dedicated identity command.

        Returns:
            A 705-format status word beginning with ``"705"``.
        """
        self._check_error()
        return f"705{self._pole_mode}001006000000:"

    def first_last_channel(self) -> tuple[int, int]:
        """Return the simulated first and last addressable channel numbers.

        Returns:
            ``(first, last)``, e.g. ``(1, 20)``.
        """
        self._check_error()
        return (self._first_channel, self._last_channel)

    def close_channels(self, specs: Sequence[str]) -> None:
        """Close (connect) the given channels.

        Args:
            specs: Channel numbers as strings, normalised to three digits.
                Out-of-range channels are silently ignored (see
                :meth:`_channel_spec`).
        """
        for spec in specs:
            ch = self._channel_spec(spec)
            if self._in_range(ch):
                self._closed.add(ch)

    def open_channels(self, specs: Sequence[str]) -> None:
        """Open (disconnect) the given channels.

        Args:
            specs: Channel numbers as strings. Channels not currently closed
                are ignored.
        """
        for spec in specs:
            self._closed.discard(self._channel_spec(spec))

    def open_all(self) -> None:
        """Open every channel (clear the closed-channel set)."""
        self._closed.clear()

    def closed_channels(self) -> list[str]:
        """Return the currently-closed channels, sorted.

        Mirrors the real driver's G2 buffer-dump read.

        Returns:
            Sorted three-digit channel numbers currently closed.
        """
        self._check_error()
        return sorted(self._closed)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on Keithley 705",
                vi_name="SimKeithley705",
            )

    def set_pole_mode(self, poles: int) -> None:
        """Set the simulated scanner's pole configuration.

        Mirrors the real instrument, where the pole mode changes the number of
        addressable channels (measured on the 12t-cryo 705: 1-pole = 40,
        2-pole = 20, 4-pole = 10). The simulated channel count follows, so a
        route that is valid in 2-pole but out of range in 4-pole fails in tests
        rather than silently never closing on hardware.

        Args:
            poles: 1, 2 or 4.

        Raises:
            CryoSoftCommunicationError: If *poles* is not a supported mode.
        """
        self._check_error()
        channels = {1: 40, 2: 20, 4: 10}
        try:
            self._last_channel = channels[int(poles)]
        except (KeyError, ValueError, TypeError) as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705: unsupported pole mode {poles!r}; "
                f"expected one of {sorted(channels)}.",
                vi_name="SimKeithley705",
            ) from exc
        self._pole_mode = int(poles)
        self._closed.clear()

    def set_four_point_mode(self) -> None:
        """Set the simulated scanner to 4-pole (4-point) mode."""
        self.set_pole_mode(4)

    def close_channel(self, channel: str) -> None:
        """Open all channels first (exclusive mux) and close a single channel.

        Args:
            channel: Channel number as a string (e.g. ``"5"``).
        """
        self._check_error()
        ch = self._channel_spec(channel)
        self._closed.clear()
        if self._in_range(ch):
            self._closed.add(ch)

    def open_channel(self, channel: str) -> None:
        """Open (disconnect) a single channel.

        Args:
            channel: Channel number as a string (e.g. ``"5"``).
        """
        self._check_error()
        self._closed.discard(self._channel_spec(channel))

    # ------------------------------------------------------------------
    # Channel-spec modelling (mirrors the real instrument's behaviour)
    # ------------------------------------------------------------------

    def _in_range(self, spec: str) -> bool:
        """Return whether a normalised channel spec is addressable.

        Models the real 705's most dangerous failure mode: a channel beyond the
        installed range raises IDDCO on the instrument, which is reported
        nowhere on the bus — the close is simply discarded and the readback is
        unchanged. Silently dropping it here means a config naming a
        non-existent channel fails an assertion in tests instead of leaving an
        unrouted measurement running on hardware.

        Args:
            spec: A normalised three-digit channel number.

        Returns:
            True if the channel is within ``first_last_channel()``.
        """
        return self._first_channel <= int(spec) <= self._last_channel

    @staticmethod
    def _channel_spec(spec: str) -> str:
        """Normalise a channel spec to the instrument's three-digit form.

        Args:
            spec: A channel number as a string, padded or not.

        Returns:
            The zero-padded three-digit channel number.

        Raises:
            CryoSoftCommunicationError: If *spec* is not a channel number,
                matching the real driver. The 705 addresses numbered channels;
                a crosspoint spec like ``"1!1"`` is not its format.
        """
        text = str(spec).strip()
        try:
            return f"{int(text):03d}"
        except ValueError as exc:
            raise CryoSoftCommunicationError(
                f"Keithley 705: {spec!r} is not a channel number. The 705 "
                f"addresses numbered channels (see first_last_channel()).",
                vi_name="SimKeithley705",
            ) from exc
