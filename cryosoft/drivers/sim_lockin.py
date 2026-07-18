# ---
# description: |
#   Simulated driver for a generic phase-sensitive lock-in amplifier. Models
#   an internal oscillator (reference source "INT") driving a nonlinear DUT,
#   and a single demodulator that reports X/Y at whichever harmonic is
#   currently selected — mirrors how a real single-demodulator lock-in
#   (e.g. SRS SR830/860) must have its harmonic switched between reads to
#   get both 1f and 2f. No specific real instrument's SCPI set is targeted
#   yet; this is the base contract a real driver will be written against
#   once a physical lock-in is connected and can be tested directly. No
#   VISA dependency — pure Python simulation.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Public methods
#   configure the internal oscillator, select the demodulated harmonic, and
#   read back the X/Y components at that harmonic.
# process: |
#   X/Y at the fundamental (1f) scale linearly with the oscillator amplitude
#   via a private response-ratio test hook; X/Y at the 2nd harmonic (2f)
#   scale with the amplitude squared (models a nonlinear DUT response),
#   both with small Gaussian noise.
# output: |
#   Returns float oscillator/demodulator settings and float X/Y readings (V).
# last_updated: 2026-07-18
# ---

"""Simulated phase-sensitive lock-in amplifier driver (base/internal-source only)."""

import random

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimLockIn:
    """Simulated lock-in amplifier: internal oscillator, single demodulator.

    Models a single-demodulator lock-in (like a real SRS SR830/860): only one
    harmonic can be demodulated at a time, selected via ``set_harmonic()``.
    Getting both 1f and 2f requires switching harmonic between reads.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_lockin.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated lock-in.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::8::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._reference_source: str = "INT"    # "INT" or "EXT"
        self._oscillator_amplitude_v: float = 0.0
        self._oscillator_frequency_hz: float = 977.0
        self._harmonic: int = 1
        self._time_constant_s: float = 0.1

        # Simulated DUT response: X_1f ~= response_1f_ratio * amplitude,
        # X_2f ~= response_2f_ratio * amplitude^2 (nonlinear). Private test
        # hooks, not part of the public API parity check.
        self._response_1f_ratio: float = 100.0   # V/V (linear gain to 1f)
        self._response_2f_ratio: float = 5.0      # V/V^2 (nonlinear gain to 2f)
        self._noise_std: float = 1e-6             # V

        # Test control flag
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Reference / oscillator configuration
    # ------------------------------------------------------------------

    def get_reference_source(self) -> str:
        """Return the reference source: "INT" (internal) or "EXT" (external)."""
        self._check_error()
        return self._reference_source

    def set_reference_source(self, source: str) -> None:
        """Set the reference source.

        Args:
            source: "INT" for the internal oscillator, "EXT" to lock to an
                externally supplied reference signal.

        Raises:
            ValueError: If source is not "INT" or "EXT".
        """
        if source not in ("INT", "EXT"):
            raise ValueError(f"reference source must be 'INT' or 'EXT', got {source!r}")
        self._reference_source = source

    def get_oscillator_amplitude(self) -> float:
        """Return the internal oscillator amplitude in Volts RMS."""
        self._check_error()
        return self._oscillator_amplitude_v

    def set_oscillator_amplitude(self, amplitude_v: float) -> None:
        """Set the internal oscillator amplitude.

        Args:
            amplitude_v: Desired oscillator amplitude in Volts RMS. Must be
                non-negative.

        Raises:
            ValueError: If amplitude_v is negative.
        """
        if amplitude_v < 0:
            raise ValueError(f"oscillator amplitude must be >= 0, got {amplitude_v}")
        self._oscillator_amplitude_v = float(amplitude_v)

    def get_oscillator_frequency(self) -> float:
        """Return the internal oscillator frequency in Hz."""
        self._check_error()
        return self._oscillator_frequency_hz

    def set_oscillator_frequency(self, frequency_hz: float) -> None:
        """Set the internal oscillator frequency.

        Args:
            frequency_hz: Desired oscillator frequency in Hz. Must be positive.

        Raises:
            ValueError: If frequency_hz is not positive.
        """
        if frequency_hz <= 0:
            raise ValueError(f"oscillator frequency must be positive, got {frequency_hz}")
        self._oscillator_frequency_hz = float(frequency_hz)

    # ------------------------------------------------------------------
    # Demodulator configuration
    # ------------------------------------------------------------------

    def get_harmonic(self) -> int:
        """Return the harmonic number the demodulator is currently locked to."""
        self._check_error()
        return self._harmonic

    def set_harmonic(self, n: int) -> None:
        """Select which harmonic of the reference the demodulator reports.

        Args:
            n: Harmonic number (1 = fundamental, 2 = second harmonic, ...).
                Must be a positive integer.

        Raises:
            ValueError: If n is not a positive integer.
        """
        if n < 1:
            raise ValueError(f"harmonic must be a positive integer, got {n}")
        self._harmonic = int(n)

    def get_time_constant(self) -> float:
        """Return the demodulator time constant in seconds."""
        self._check_error()
        return self._time_constant_s

    def set_time_constant(self, tc_s: float) -> None:
        """Set the demodulator time constant.

        Args:
            tc_s: Desired time constant in seconds. Must be positive.

        Raises:
            ValueError: If tc_s is not positive.
        """
        if tc_s <= 0:
            raise ValueError(f"time constant must be positive, got {tc_s}")
        self._time_constant_s = float(tc_s)

    # ------------------------------------------------------------------
    # Demodulated readings
    # ------------------------------------------------------------------

    def get_x(self) -> float:
        """Return the in-phase (X) component at the currently selected harmonic, in Volts."""
        self._check_error()
        return self._signal_at_harmonic() + random.gauss(0.0, self._noise_std)

    def get_y(self) -> float:
        """Return the quadrature (Y) component at the currently selected harmonic, in Volts.

        The simulated DUT response is modeled as purely in-phase, so Y is
        noise only.
        """
        self._check_error()
        return random.gauss(0.0, self._noise_std)

    def get_idn(self) -> str:
        """Return simulated identification string."""
        self._check_error()
        return "CRYOSOFT,SIM-LOCKIN,SIM,1.0"

    # ------------------------------------------------------------------
    # Internal simulation logic
    # ------------------------------------------------------------------

    def _signal_at_harmonic(self) -> float:
        """Return the noiseless simulated in-phase response at the selected harmonic."""
        amplitude = self._oscillator_amplitude_v
        if self._harmonic == 1:
            return self._response_1f_ratio * amplitude
        if self._harmonic == 2:
            return self._response_2f_ratio * amplitude**2
        return 0.0

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on lock-in amplifier",
                vi_name="SimLockIn",
            )
