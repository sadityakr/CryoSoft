# ---
# description: |
#   Simulated driver for the Keithley 2182A nanovoltmeter.
#   Returns voltage readings with configurable Gaussian noise. Can be paired
#   with SimKeithley6221 for delta-mode simulation. No VISA dependency.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Public methods read
#   voltage and configure the measurement range.
# process: |
#   Each get_voltage() call returns base_voltage + Gaussian noise sampled
#   from N(0, noise_std). The base voltage and noise level are configurable
#   via internal attributes for testing purposes.
# output: |
#   Returns float voltage readings with noise. get_range() / set_range()
#   control the measurement range setting.
# last_updated: 2026-04-06
# ---

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

        # Test control flags
        self._simulate_error: bool = False

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on Keithley 2182A",
                vi_name="SimKeithley2182A",
            )
