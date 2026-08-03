"""Simulated Keithley 2182A Nanovoltmeter driver."""

import random

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimKeithley2182A:
    """Simulated Keithley 2182A nanovoltmeter.

    Returns voltage readings with configurable Gaussian noise.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_keithley_2182a.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated Keithley 2182A.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::7::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._base_voltage: float = 1.5e-6  # Volts — simulated signal level
        self._noise_std: float = 1e-8       # Gaussian noise standard deviation
        self._range: float = 0.1            # Volts measurement range
        # Free-running (continuous initiation) is the instrument's power-up
        # default; CryoSoft's reading paths pin it off when they arm.
        self._continuous_initiation: bool = True

        # Test control flags
        self._simulate_error: bool = False
        # Connection-lifecycle standard: True once close() has released
        # the session; every command then fails (see _check_error).
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_voltage(self) -> float:
        """Return a single voltage reading with Gaussian noise.

        Returns:
            Voltage in Volts (float).
        """
        self._check_error()
        return self._base_voltage + random.gauss(0.0, self._noise_std)

    def set_range(self, range_v: float) -> None:
        """Set the measurement voltage range.

        Args:
            range_v: Full-scale voltage range in Volts.
        """
        self._range = float(range_v)

    def get_range(self) -> float:
        """Return the current voltage range setting in Volts."""
        self._check_error()
        return self._range

    def set_continuous_initiation(self, enabled: bool) -> None:
        """Turn the simulated free-running (continuous initiation) mode on/off.

        Mirrors the real driver's ``:INIT:CONT`` write. The flag is recorded
        so a test can assert the arming path pinned single-shot mode — and
        that nothing did so at Station build time.

        Args:
            enabled: True to leave the instrument free-running, False for
                single-shot.
        """
        self._check_error()
        self._continuous_initiation = bool(enabled)

    def get_idn(self) -> str:
        """Return simulated *IDN? response string."""
        self._check_error()
        return "KEITHLEY,2182A,SIM,1.0"

    # ------------------------------------------------------------------
    # Connection lifecycle (the connection-lifecycle standard)
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the simulated bus session; the instrument is left untouched.

        Idempotent and never raises. Afterwards every command — including
        ``get_idn()`` — raises ``CryoSoftCommunicationError`` via
        :meth:`_check_error`, modelling a released session so a
        use-after-disconnect bug fails in a test instead of on hardware.
        A closed driver is never reopened in place: the Station builds a
        fresh instance when the operator reconnects.
        """
        self._closed = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._closed:
            raise CryoSoftCommunicationError(
                "SimKeithley2182A: the session is closed — the driver was "
                "disconnected from CryoSoft",
                vi_name="SimKeithley2182A",
            )
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on Keithley 2182A",
                vi_name="SimKeithley2182A",
            )
