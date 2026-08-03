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
        # Connection-lifecycle standard: True once close() has released
        # the session; every command then fails (see _check_error).
        self._closed: bool = False

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

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._closed:
            raise CryoSoftCommunicationError(
                "SimOxfordILM210: the session is closed — the driver was "
                "disconnected from CryoSoft",
                vi_name="SimOxfordILM210",
            )
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on ILM 210",
                vi_name="SimOxfordILM210",
            )
