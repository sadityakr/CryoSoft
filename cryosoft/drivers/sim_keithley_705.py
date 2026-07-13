# ---
# description: |
#   Simulated driver for the Keithley 705 scanner / matrix switch.
#   Models an EXCLUSIVE-MUX crosspoint switch as a set of closed channel-spec
#   strings: close_channels() adds specs, open_channels() removes them, and
#   open_all() clears the set. No VISA dependency — pure Python simulation.
# entry_point: Not run directly; imported by the Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Channel specs are opaque
#   instrument-format strings (e.g. "1!1") passed through verbatim from config;
#   the driver never parses their route semantics.
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

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated Keithley 705.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::17::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract
        self._closed: set[str] = set()
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Public API (identical to the real Keithley705)
    # ------------------------------------------------------------------

    def get_idn(self) -> str:
        """Return the simulated identification string."""
        self._check_error()
        return "KEITHLEY,705,SIM,1.0"

    def close_channels(self, specs: Sequence[str]) -> None:
        """Close (connect) the given channel specs.

        Args:
            specs: Instrument-format channel-spec strings (e.g. ``["1!1"]``).
                Recorded verbatim in the closed-channel set.
        """
        for spec in specs:
            self._closed.add(str(spec))

    def open_channels(self, specs: Sequence[str]) -> None:
        """Open (disconnect) the given channel specs.

        Args:
            specs: Instrument-format channel-spec strings to remove from the
                closed-channel set. Specs not currently closed are ignored.
        """
        for spec in specs:
            self._closed.discard(str(spec))

    def open_all(self) -> None:
        """Open every channel (clear the closed-channel set)."""
        self._closed.clear()

    def closed_channels(self) -> list[str]:
        """Return the currently-closed channel specs, sorted (inspection helper).

        Not part of the real instrument's command set — it exposes the sim's
        internal model so tests can assert exclusive selection.

        Returns:
            The sorted list of closed channel-spec strings.
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
