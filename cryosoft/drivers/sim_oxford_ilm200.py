"""Simulated Oxford ILM 200 Cryogen Level Meter driver."""

import logging
import time

from cryosoft.core.exceptions import (
    CryoSoftCommunicationError,
    CryoSoftInstrumentError,
)

log = logging.getLogger(__name__)


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

        # Instrument-error model (the driver error-reporting standard,
        # drivers/README.md): which measurement channels actually have a
        # probe fitted. A command naming an unfitted channel is answered
        # with the ISOBUS '?' refusal, not with a level — the failure mode
        # a config that names a nitrogen channel this meter does not have
        # would otherwise show as a nonsense reading.
        self._channels_fitted: set[int] = {1, 2}

        # Test control flags
        self._simulate_error: bool = False
        # Connection-lifecycle standard: True once close() has released
        # the session; every command then fails (see _check_error).
        self._closed: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_helium_level(self) -> float:
        """Return the helium level as a percentage (0–100%).

        If _force_helium_level is set, that value is returned directly.
        """
        self._check_error()
        self._refuse_if_unfitted(1, "R1", "get_helium_level()")
        self._update_simulation()
        if self._force_helium_level is not None:
            return float(self._force_helium_level)
        return self._helium_level

    def get_nitrogen_level(self) -> float:
        """Return the nitrogen level as a percentage (0–100%).

        Nitrogen stays nearly constant in this simulation.
        """
        self._check_error()
        self._refuse_if_unfitted(2, "R2", "get_nitrogen_level()")
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
        self._refuse_if_unfitted(
            1, "T1" if mode == 2 else "S1", f"set_refresh_rate({mode!r})"
        )
        self._refresh_rate = mode

    def get_idn(self) -> str:
        """Return simulated identification string (matches OxfordILM200)."""
        self._check_error()
        return "OXFORD,ILM200,SIM,1.0"

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
    # Instrument-error model (the driver error-reporting standard)
    # ------------------------------------------------------------------

    def _refuse_if_unfitted(self, channel: int, cmd: str, context: str) -> None:
        """Answer the ISOBUS ``?`` refusal for a channel with no probe fitted.

        Mirrors what the real meter puts on the wire: instead of a level (or
        an echo), it replies ``?`` + the command, which the real driver's
        ``_check_acknowledgement()`` turns into this same typed error.

        Args:
            channel: The ISOBUS channel the command addresses (1 = He, 2 = N2).
            cmd: The ISOBUS command that would have been sent.
            context: The driver call being refused.

        Raises:
            CryoSoftInstrumentError: ``?`` if *channel* has no probe fitted.
        """
        if channel in self._channels_fitted:
            return
        raise CryoSoftInstrumentError(
            f"Simulated ILM 200 refused command {cmd!r}: replied '?{cmd}'. "
            f"An ISOBUS '?' reply means the instrument did not carry the "
            f"command out (here: no probe fitted on channel {channel}).",
            code="?",
            instrument_message=f"?{cmd}",
            context=context,
            vi_name="SimOxfordILM200",
        )

    # ------------------------------------------------------------------
    # Safe state (the safe-shutdown standard)
    # ------------------------------------------------------------------

    def safe_shutdown(self) -> None:
        """Take the simulated probe off continuous drive; never raises.

        Safe idle for a level meter is the pulsed refresh rate, not "off" —
        the level is a safety input and must keep arriving, but FAST mode
        keeps the probe energised and boils helium. See the real driver.
        """
        log.info("SimOxfordILM200: safe shutdown — probe back to pulsed refresh.")
        self._refresh_rate = 0

    def _is_in_safe_state(self) -> bool:
        """Return True when the probe is not in FAST/continuous mode."""
        return self._refresh_rate != 2

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
                "SimOxfordILM200: the session is closed — the driver was "
                "disconnected from CryoSoft",
                vi_name="SimOxfordILM200",
            )
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on ILM 200",
                vi_name="SimOxfordILM200",
            )
