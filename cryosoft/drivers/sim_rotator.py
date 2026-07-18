# ---
# description: |
#   Simulated driver for a generic motorized sample-rotation stage. Models
#   angular position moving toward a setpoint at a configurable rate, mirroring
#   SimOxfordIPS120's current-ramp simulation but for degrees instead of amps
#   and with no quench/switch-heater physics (a rotation stage has neither).
#   No VISA dependency — pure Python simulation using real wall-clock time.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Public methods control
#   and query the simulated sample angle and status.
# process: |
#   Uses time.time() to advance the simulated position toward the setpoint at
#   the configured rate (deg/min). Status transitions HOLD -> MOVING -> HOLD.
# output: |
#   Returns float position/setpoint values and str status via public API.
# last_updated: 2026-07-18
# ---

"""Simulated motorized sample-rotation stage driver."""

import time

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimRotator:
    """Simulated motorized sample-rotation stage.

    Models angular position moving toward a setpoint at a configurable rate.
    Status transitions: HOLD -> MOVING -> HOLD.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_rotator.
    """

    # Generous mechanical range of the simulated stage; the VI-level
    # control_limits (from the setup config) narrow this further.
    MAX_ANGLE_DEG = 180.0
    MIN_ANGLE_DEG = -180.0

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated rotator.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::26::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._position: float = 0.0    # Sample angle in degrees
        self._setpoint: float = 0.0    # Target angle in degrees
        self._rate: float = 1.0        # deg/min
        self._status: str = "HOLD"     # "HOLD" or "MOVING"
        self._last_update: float = time.time()

        # Test control flag
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_position(self) -> float:
        """Return the current sample angle in degrees."""
        self._check_error()
        self._update_simulation()
        return self._position

    def get_position_setpoint(self) -> float:
        """Return the current position setpoint in degrees."""
        self._check_error()
        return self._setpoint

    def set_position_setpoint(self, setpoint: float) -> None:
        """Set the target sample angle.

        Clamps the setpoint to [MIN_ANGLE_DEG, MAX_ANGLE_DEG]. If the
        difference from the current position exceeds 0.01 deg, transitions
        to MOVING status.

        Args:
            setpoint: Desired sample angle in degrees.
        """
        clamped = max(self.MIN_ANGLE_DEG, min(self.MAX_ANGLE_DEG, setpoint))
        self._setpoint = clamped

        if abs(self._setpoint - self._position) > 0.01:
            self._status = "MOVING"

    def hold(self) -> None:
        """Freeze the stage where it is."""
        self._check_error()
        self._update_simulation()
        self._setpoint = self._position
        self._status = "HOLD"

    def set_rate(self, rate: float) -> None:
        """Set the rotation rate.

        Args:
            rate: Rotation rate in deg/min. Must be positive.
        """
        if rate <= 0:
            raise ValueError(f"Rotation rate must be positive, got {rate}")
        self._rate = rate

    def get_status(self) -> str:
        """Return the current status string.

        Returns:
            One of "HOLD" or "MOVING".
        """
        self._check_error()
        self._update_simulation()
        return self._status

    def get_idn(self) -> str:
        """Return simulated identification string."""
        self._check_error()
        return "CRYOSOFT,SIM-ROTATOR,SIM,1.0"

    # ------------------------------------------------------------------
    # Internal simulation logic
    # ------------------------------------------------------------------

    def _update_simulation(self) -> None:
        """Advance simulated position toward setpoint based on elapsed real time."""
        now = time.time()
        dt_min = (now - self._last_update) / 60.0
        self._last_update = now

        if self._status != "MOVING":
            return

        max_step = self._rate * dt_min
        remaining = self._setpoint - self._position
        if abs(remaining) <= max_step:
            self._position = self._setpoint
            self._status = "HOLD"
        else:
            direction = 1 if remaining > 0 else -1
            self._position += direction * max_step

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on rotator",
                vi_name="SimRotator",
            )
