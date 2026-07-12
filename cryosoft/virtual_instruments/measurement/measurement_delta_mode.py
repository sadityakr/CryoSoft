# ---
# description: |
#   DeltaModeMeasurementVI: multi-driver Virtual Instrument pairing a Keithley
#   6221 current source with a Keithley 2182A nanovoltmeter for delta-mode
#   resistance measurements.  read_datapoint() is NOT @monitored — called only
#   by Procedure objects.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MeasurementInstrumentBase)
#   - cryosoft.core.decorators (control)
# input: |
#   drivers = {"source": <K6221 driver>, "meter": <K2182A driver>}
#   configure() must be called before read_datapoint().
#   Supported methods: "delta_mode" with params current (A), n_readings (int),
#   delay (s, default 0.01), compliance (V, default 1.0), range_2182a (V, default 0.01),
#   compliance_abort (bool, default True), cold_switch (bool, default False).
# process: |
#   configure() arms the delta engine via source.configure_and_start_delta().
#   read_datapoint() calls source.acquire_delta_readings() to collect samples.
#   standby() calls source.stop_delta_mode() to abort the running engine.
# output: |
#   {"voltage_V": list[float], "current_A": list[float]} with length n_readings.
# last_updated: 2026-04-19
# ---

"""DeltaModeMeasurementVI — Keithley 6221 + 2182A delta-mode measurement VI."""

from __future__ import annotations

from typing import Any

from cryosoft.core.decorators import control
from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

# Sentinel to detect unconfigured state.
_NOT_CONFIGURED = object()


class DeltaModeMeasurementVI(MeasurementInstrumentBase):
    """Virtual Instrument for delta-mode resistance measurements.

    Uses two drivers:
    * ``"source"`` — Keithley 6221 current source.
    * ``"meter"``  — Keithley 2182A nanovoltmeter.

    Workflow (always run by a Procedure, never the GUI directly)::

        vi.configure("delta_mode", current=1e-6, n_readings=100)
        data = vi.read_datapoint()
        # data = {"voltage_V": ndarray(100,), "current_A": ndarray(100,)}

    ``read_datapoint()`` is deliberately **not** tagged ``@monitored`` or
    ``@control`` because it is long-running and should only be triggered by
    the Procedure layer.
    """

    SUPPORTED_METHODS = ("delta_mode",)

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._source = drivers["source"]
        self._meter = drivers["meter"]

        # Configuration state — set by configure().
        self._method: object = _NOT_CONFIGURED
        self._meas_params: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # @control — configure the measurement
    # ------------------------------------------------------------------

    @control
    def configure(self, method: str, **params: Any) -> None:
        """Configure the measurement method and arm the delta engine.

        Must be called before ``read_datapoint()``. For ``"delta_mode"`` this
        calls ``source.configure_and_start_delta()`` immediately so the
        instrument is armed and ready to deliver readings on demand.

        Args:
            method: Measurement method name. Supported: ``"delta_mode"``.
            **params: Method-specific parameters.

                For ``"delta_mode"``:
                    - ``current`` (float): Peak delta current in Amperes.
                    - ``n_readings`` (int): Readings per acquisition call.
                    - ``delay`` (float, optional): Inter-transition delay (s). Default 0.01.
                    - ``compliance`` (float, optional): Voltage compliance (V). Default 1.0.
                    - ``range_2182a`` (float, optional): 2182A range (V). Default 0.01.
                    - ``compliance_abort`` (bool, optional): Abort on compliance. Default True.
                    - ``cold_switch`` (bool, optional): Cold-switch reversals. Default False.

        Raises:
            ValueError: If *method* is not supported.
        """
        if method not in self.SUPPORTED_METHODS:
            raise ValueError(
                f"Unsupported measurement method: {method!r}. "
                f"Supported: {self.SUPPORTED_METHODS}"
            )
        self._method = method
        self._meas_params = dict(params)

        if method == "delta_mode":
            current: float = float(params.get("current", 1e-6))
            n_readings: int = int(params.get("n_readings", 100))
            delay: float = float(params.get("delay", 0.01))
            compliance: float = float(params.get("compliance", 1.0))
            range_2182a: float = float(params.get("range_2182a", 0.01))
            compliance_abort: bool = bool(params.get("compliance_abort", True))
            cold_switch: bool = bool(params.get("cold_switch", False))
            self._source.configure_and_start_delta(  # type: ignore[attr-defined]
                current, n_readings, delay, compliance, range_2182a,
                compliance_abort, cold_switch,
            )

    # ------------------------------------------------------------------
    # read_datapoint — NOT @monitored, NOT @control (Procedure-only)
    # ------------------------------------------------------------------

    def read_datapoint(self) -> dict[str, list[float]]:
        """Acquire a measurement datapoint.

        Returns a flat dict of equal-length arrays.

        Returns:
            ``{"voltage_V": list, "current_A": list}``
            Arrays have length equal to *n_readings* from ``configure()``.

        Raises:
            RuntimeError: If ``configure()`` has not been called first.
        """
        if self._method is _NOT_CONFIGURED:
            raise RuntimeError(
                "configure() must be called before read_datapoint()."
            )

        if self._method == "delta_mode":
            return self._read_delta_mode()

        # Unreachable after configure() validation, but defensive:
        raise RuntimeError(f"Unknown method state: {self._method!r}")

    # ------------------------------------------------------------------
    # Internal acquisition logic
    # ------------------------------------------------------------------

    def _read_delta_mode(self) -> dict[str, list[float]]:
        """Collect readings from the already-armed delta engine.

        The engine was started in configure(). This call polls
        ``source.acquire_delta_readings()`` for the configured number of
        samples and packages them alongside the nominal current array.

        Returns:
            ``{"voltage_V": list, "current_A": list}``
        """
        current_A: float = float(self._meas_params.get("current", 1e-6))
        n_readings: int = int(self._meas_params.get("n_readings", 100))
        delay: float = float(self._meas_params.get("delay", 0.01))

        voltages: list[float] = self._source.acquire_delta_readings(  # type: ignore[attr-defined]
            n_readings, delay
        )

        # current_A array mirrors the number of readings actually returned.
        currents: list[float] = [current_A] * len(voltages)

        return {
            "voltage_V": voltages,
            "current_A": currents,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Query IDN from both drivers to verify they are reachable.

        Returns:
            True if both source and meter respond to ``get_idn()``.
        """
        try:
            self._source.get_idn()   # type: ignore[attr-defined]
            self._meter.get_idn()    # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def initiate(self) -> None:
        """Initialise both instruments to a known safe state."""
        self._source.set_current(0.0)   # type: ignore[attr-defined]

    def standby(self) -> None:
        """Abort the delta engine, turn off outputs, and reset state."""
        try:
            self._source.stop_delta_mode()   # type: ignore[attr-defined]
        except Exception:
            pass
        self._method = _NOT_CONFIGURED
        self._meas_params = {}
