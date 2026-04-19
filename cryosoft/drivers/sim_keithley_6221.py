# ---
# description: |
#   Simulated driver for the Keithley 6221 AC/DC current source.
#   Models source enable/disable, current output configuration, and delta-mode
#   operation. No VISA dependency — pure Python simulation.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Public methods configure
#   and control the simulated current source, including delta-mode setup.
# process: |
#   Stores source state and delta-mode configuration. When get_delta_readings()
#   is called, generates simulated voltage readings sourced from a paired
#   SimKeithley2182A meter if set, or returns zeros with noise otherwise.
# output: |
#   Returns bool source state, float current, and list[float] delta readings.
# last_updated: 2026-04-06
# ---

"""Simulated Keithley 6221 AC/DC Current Source driver."""

import time
from typing import TYPE_CHECKING

from cryosoft.core.exceptions import CryoSoftCommunicationError

if TYPE_CHECKING:
    from cryosoft.drivers.sim_keithley_2182a import SimKeithley2182A


class SimKeithley6221:
    """Simulated Keithley 6221 AC/DC current source.

    Supports source enable/disable, current configuration, and delta-mode
    operation (typically paired with a SimKeithley2182A nanovoltmeter).

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_keithley_6221.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated Keithley 6221.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::22::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._source_enabled: bool = False
        self._current: float = 0.0         # Amperes
        self._compliance: float = 0.1      # Volts (voltage compliance limit)

        # Delta-mode configuration
        self._delta_high_current: float = 0.0
        self._delta_n_readings: int = 1
        self._delta_delay: float = 0.01    # seconds

        # Set externally to link the 2182A for realistic delta simulation
        self._paired_meter: "SimKeithley2182A | None" = None

        # Stored delta readings after trigger
        self._delta_readings: list[float] = []

        # Test control flags
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_source_enabled(self) -> bool:
        """Return True if the current source output is enabled."""
        self._check_error()
        return self._source_enabled

    def set_source_enabled(self, enabled: bool) -> None:
        """Enable or disable the current source output.

        Args:
            enabled: True to enable, False to disable.
        """
        self._source_enabled = bool(enabled)

    def get_current(self) -> float:
        """Return the configured source current in Amperes."""
        self._check_error()
        return self._current

    def set_current(self, current: float) -> None:
        """Set the source current.

        Args:
            current: Desired current in Amperes.
        """
        self._current = float(current)

    def set_compliance(self, compliance_v: float) -> None:
        """Set the voltage compliance limit.

        Args:
            compliance_v: Maximum output voltage in Volts.
        """
        self._compliance = float(compliance_v)

    def get_compliance(self) -> float:
        """Return the configured voltage compliance limit in Volts."""
        self._check_error()
        return self._compliance

    def configure_delta_mode(
        self, high_current: float, n_readings: int, delay: float
    ) -> None:
        """Configure delta-mode measurement parameters.

        In the real instrument this programs the 6221 to alternate between
        +I and -I while triggering the 2182A. Here we just store the config.

        Args:
            high_current: Peak current magnitude for delta mode (A).
            n_readings: Number of reading pairs to acquire.
            delay: Delay between source transitions (seconds).
        """
        self._delta_high_current = float(high_current)
        self._delta_n_readings = int(n_readings)
        self._delta_delay = float(delay)
        self._delta_readings = []

    def trigger_delta_mode(self) -> None:
        """Start a delta-mode measurement sweep.

        Generates n_readings voltage samples. If a paired 2182A is attached
        its get_voltage() method is called; otherwise zeros with noise are used.
        """
        import random
        readings: list[float] = []
        for _ in range(self._delta_n_readings):
            if self._paired_meter is not None:
                readings.append(self._paired_meter.get_voltage())
            else:
                # Simulate a tiny noisy resistance signal
                readings.append(random.gauss(1.5e-6, 1e-8))
        self._delta_readings = readings

    def get_delta_readings(self) -> list[float]:
        """Return the voltage readings from the last delta-mode sweep.

        Returns:
            List of float voltage readings (length == n_readings configured).
        """
        self._check_error()
        return list(self._delta_readings)

    def get_idn(self) -> str:
        """Return simulated *IDN? response string."""
        self._check_error()
        return "KEITHLEY,6221,SIM,1.0"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on Keithley 6221",
                vi_name="SimKeithley6221",
            )
