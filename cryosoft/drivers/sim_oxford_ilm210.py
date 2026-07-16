# ---
# description: |
#   Simulated driver for the Oxford Instruments ILM 210 cryogen level meter.
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
# last_updated: 2026-07-16
# ---

"""Simulated Oxford ILM 210 Cryogen Level Meter driver."""

import time

from cryosoft.core.exceptions import CryoSoftCommunicationError


class SimOxfordILM210:
    """Simulated Oxford ILM 210 cryogen level meter.

    Models slowly drifting helium and (static) nitrogen levels.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated ILM 210.

        Args:
            resource_string: VISA address. Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        self._helium_level: float = 80.0     # Percent
        self._nitrogen_level: float = 90.0   # Percent
        self._refresh_rate: int = 0          # 0 = slow/standby, 1 = slow, 2 = fast
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
        """Return the helium level as a percentage (0–100%)."""
        self._check_error()
        self._update_simulation()
        if self._force_helium_level is not None:
            return float(self._force_helium_level)
        return self._helium_level

    def get_nitrogen_level(self) -> float:
        """Return the nitrogen level as a percentage (0–100%)."""
        self._check_error()
        return self._nitrogen_level

    def get_refresh_rate(self) -> int:
        """Return the current refresh rate mode (0 = standby, 1 = slow, 2 = fast)."""
        self._check_error()
        return self._refresh_rate

    def set_refresh_rate(self, mode: int) -> None:
        """Set the refresh rate mode.

        Args:
            mode: 0, 1, or 2.
        """
        if mode not in (0, 1, 2):
            raise ValueError(f"Refresh rate mode must be 0, 1, or 2, got {mode}")
        self._refresh_rate = mode

    def get_idn(self) -> str:
        """Return simulated identification string (matches OxfordILM210)."""
        self._check_error()
        return "OXFORD,ILM210,SIM,1.0"

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
                "Simulated communication error on ILM 210",
                vi_name="SimOxfordILM210",
            )
