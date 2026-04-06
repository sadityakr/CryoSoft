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
#   - numpy
# input: |
#   drivers = {"source": <K6221 driver>, "meter": <K2182A driver>}
#   configure() must be called before read_datapoint().
#   Supported methods: "delta_mode" with params current (A), n_readings (int).
# process: |
#   configure() stores method and params. read_datapoint() drives the source
#   and collects n_readings voltage measurements via the meter, returning arrays.
#   Raises RuntimeError if called before configure().
# output: |
#   {"voltage_V": np.ndarray, "current_A": np.ndarray} with length n_readings.
# last_updated: 2026-04-06
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
        """Configure the measurement method and parameters.

        Must be called before ``read_datapoint()``.

        Args:
            method: Measurement method name. Supported: ``"delta_mode"``.
            **params: Method-specific parameters.

                For ``"delta_mode"``:
                    - ``current`` (float): Source current in amperes.
                    - ``n_readings`` (int): Number of reading pairs to acquire.

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
        """Perform a delta-mode measurement sequence.

        Alternates source current polarity between readings to cancel
        thermoelectric offsets.

        Returns:
            ``{"voltage_V": list, "current_A": list}``
        """
        current_A: float = float(self._meas_params.get("current", 1e-6))
        n_readings: int = int(self._meas_params.get("n_readings", 100))

        voltages: list[float] = []
        currents: list[float] = []

        source = self._source  # type: ignore[attr-defined]
        meter = self._meter    # type: ignore[attr-defined]

        for i in range(n_readings):
            # Alternate polarity for delta (offset cancellation).
            polarity = 1.0 if i % 2 == 0 else -1.0
            applied_I = polarity * current_A

            source.set_current(applied_I)
            voltage = meter.get_voltage()

            voltages.append(float(voltage))
            currents.append(applied_I)

        source.set_current(0.0)  # Return to zero after acquisition.

        return {
            "voltage_V": voltages,
            "current_A": currents,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initiate(self) -> None:
        """Initialise both instruments to a known safe state."""
        self._source.set_current(0.0)   # type: ignore[attr-defined]

    def standby(self) -> None:
        """Turn off outputs and reset both instruments."""
        self._source.set_current(0.0)   # type: ignore[attr-defined]
        self._method = _NOT_CONFIGURED
        self._meas_params = {}
