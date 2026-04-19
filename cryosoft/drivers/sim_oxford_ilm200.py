# ---
# description: |
#   Simulated driver for the Oxford Instruments ILM 200 cryogen level meter.
#   Models slowly drifting helium and nitrogen levels using real wall-clock time.
#   No VISA dependency — pure Python simulation.
# entry_point: Not run directly; imported by Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a VISA resource string (ignored). Exposes methods to
#   read helium/nitrogen levels and control the refresh rate mode.
# process: |
#   Helium drifts down at a configurable rate (%/min). A _force_helium_level
#   override is available for testing low-helium conditions.
# output: |
#   Returns float level percentages (0-100) and int refresh rate via public API.
# last_updated: 2026-04-19
# ---

"""Simulated Oxford ILM 200 Cryogen Level Meter driver."""

import time

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimOxfordILM200:
    """Simulated Oxford ILM 200 cryogen level meter.

    Models slowly drifting helium and (static) nitrogen levels.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single VISA resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_oxford_ilm200.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated ILM 200.

        Args:
            resource_string: VISA address (e.g. 'GPIB0::24::INSTR'). Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._helium_level: float = 80.0     # Percent
        self._nitrogen_level: float = 90.0   # Percent
        self._refresh_rate: int = 0          # 0 = slow, 1 = fast
        self._helium_drift_rate: float = 0.01  # %/min
        self._last_update: float = time.time()

        # Test control: override helium level reading (None = use simulation)
        self._force_helium_level: float | None = None

        # Test control flags
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_helium_level(self) -> float:
        """Return the helium level as a percentage (0–100%).

        If _force_helium_level is set, that value is returned directly.
        """
        self._check_error()
        self._update_simulation()
        if self._force_helium_level is not None:
            return float(self._force_helium_level)
        return self._helium_level

    def get_nitrogen_level(self) -> float:
        """Return the nitrogen level as a percentage (0–100%).

        Nitrogen stays nearly constant in this simulation.
        """
        self._check_error()
        return self._nitrogen_level

    def get_refresh_rate(self) -> int:
        """Return the current refresh rate mode (0 = slow, 1 = fast)."""
        self._check_error()
        return self._refresh_rate

    def set_refresh_rate(self, mode: int) -> None:
        """Set the refresh rate mode.

        Modes follow the three-mode standard:
          0 = STANDBY, 1 = SLOW continuous polling, 2 = FAST (used during helium fills).

        Args:
            mode: 0, 1, or 2.
        """
        if mode not in (0, 1, 2):
            raise ValueError(f"Refresh rate mode must be 0, 1, or 2, got {mode}")
        self._refresh_rate = mode

    # ------------------------------------------------------------------
    # Internal simulation logic
    # ------------------------------------------------------------------

    def _update_simulation(self) -> None:
        """Advance simulated helium level based on elapsed real time."""
        now = time.time()
        dt_min = (now - self._last_update) / 60.0
        self._last_update = now

        drift = self._helium_drift_rate * dt_min
        self._helium_level = max(0.0, self._helium_level - drift)

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on ILM 200",
                vi_name="SimOxfordILM200",
            )
