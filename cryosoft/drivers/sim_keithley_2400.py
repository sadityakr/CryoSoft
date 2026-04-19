# ---
# description: |
#   Simulated driver for the Keithley 2400 SourceMeter (SMU).
#   Models a single-instrument source-measure unit that both sources current
#   and measures voltage, used for DC resistance measurements without a
#   separate current source and voltmeter.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Public methods
#   set source current/compliance/range and read back measured voltage.
# process: |
#   Stores the sourced current. Returns voltage = _resistance * _current + noise.
#   Noise is Gaussian with sigma 1e-8 V to match realistic nanovoltmeter noise.
# output: |
#   Returns float voltage/current values and str IDN via public API.
# last_updated: 2026-04-19
# ---

"""Simulated Keithley 2400 SourceMeter (SMU) driver."""

from __future__ import annotations

import random

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimKeithley2400:
    """Simulated Keithley 2400 SourceMeter.

    Models a source-measure unit: sources a DC current and measures
    the resulting voltage across the sample.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_keithley_2400.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated Keithley 2400.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::24::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._current: float = 0.0          # Sourced current in Amperes
        self._compliance: float = 1.0       # Compliance voltage in Volts
        self._range: float = 1.0            # Voltage measurement range in Volts
        self._resistance: float = 1500.0    # Simulated sample resistance in Ohms

        # Test control flags
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_current(self, current: float) -> None:
        """Set the sourced current.

        Args:
            current: Source current in Amperes.
        """
        self._current = current

    def get_current(self) -> float:
        """Return the currently sourced current in Amperes."""
        self._check_error()
        return self._current

    def set_compliance(self, compliance_v: float) -> None:
        """Set the voltage compliance limit.

        Args:
            compliance_v: Maximum output voltage in Volts.
        """
        self._compliance = compliance_v

    def get_compliance(self) -> float:
        """Return the compliance voltage in Volts."""
        self._check_error()
        return self._compliance

    def set_range(self, range_v: float) -> None:
        """Set the voltage measurement range.

        Args:
            range_v: Measurement range in Volts.
        """
        self._range = range_v

    def get_range(self) -> float:
        """Return the current voltage measurement range in Volts."""
        self._check_error()
        return self._range

    def get_voltage(self) -> float:
        """Return the measured voltage in Volts.

        Simulates V = R * I with Gaussian noise (sigma = 1e-8 V).

        Returns:
            Measured voltage in Volts.
        """
        self._check_error()
        noise = random.gauss(0.0, 1e-8)
        return self._resistance * self._current + noise

    def get_idn(self) -> str:
        """Return the instrument identification string."""
        self._check_error()
        return "KEITHLEY,2400,SIM,1.0"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on Keithley 2400",
                vi_name="SimKeithley2400",
            )
