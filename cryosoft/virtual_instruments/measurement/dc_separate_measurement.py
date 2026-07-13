# ---
# description: |
#   DCSeparateMeasurementVI: behavior-based VI for DC resistance measurements
#   using a dedicated current source and a separate voltmeter.
#   initiate() arms the source with a fixed current, compliance, voltmeter
#   range and readings-per-point. take_reading() collects that many voltage
#   samples at the fixed current.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (DCMeasurementBase)
#   - cryosoft.core.decorators (control)
# input: |
#   drivers = {"source": <current source driver>, "meter": <voltmeter driver>}
#   initiate(current_A, compliance_A, voltmeter_range_V, readings_per_point)
#   must be called before the argument-less take_reading().
# process: |
#   initiate() stores measurement parameters and programs both instruments.
#   take_reading() acquires readings_per_point voltage samples and returns them
#   alongside a constant current array.
# output: |
#   {"voltage_V": list[float], "current_A": list[float]} of length
#   readings_per_point.
# last_updated: 2026-07-13
# ---

"""DCSeparateMeasurementVI — DC measurement with separate current source + voltmeter."""

from __future__ import annotations

from typing import Any, ClassVar

from cryosoft.core.decorators import control
from cryosoft.virtual_instruments.base import DCMeasurementBase

_NOT_INITIATED = object()


class DCSeparateMeasurementVI(DCMeasurementBase):
    """Virtual Instrument for DC resistance measurements with separate instruments.

    Uses two drivers:
    * ``"source"`` — current source (e.g. Keithley 6221).
    * ``"meter"``  — voltmeter (e.g. Keithley 2182A nanovoltmeter).

    Workflow::

        vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1,
                    readings_per_point=50)
        data = vi.take_reading()
        # data = {"voltage_V": list[float](50,), "current_A": list[float](50,)}

    To swap to a single-instrument SMU, replace this VI with
    ``DCSingleInstrumentVI`` in the YAML config. The procedure is unchanged.

    Driver contract
    ---------------
    ``"source"`` driver must implement:
    * ``set_current(float)``
    * ``set_compliance(float)``
    * ``get_idn() -> str``

    ``"meter"`` driver must implement:
    * ``get_voltage() -> float``
    * ``set_range(float)``
    * ``get_idn() -> str``
    """

    # Short drop-down name: separate current source + nanovoltmeter, versus the
    # single-SMU DCSingleInstrumentVI.
    selector_label: ClassVar[str] = "DC (6221 + 2182A)"

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._source = drivers["source"]
        self._meter = drivers["meter"]

        self._current_A: object = _NOT_INITIATED
        self._compliance_A: float = 1e-3
        self._voltmeter_range_V: float = 0.1
        self._readings_per_point: int = 10

    # ------------------------------------------------------------------
    # DCMeasurementBase implementation
    # ------------------------------------------------------------------

    @control
    def initiate(
        self,
        current_A: float = 1e-6,
        compliance_A: float = 1e-3,
        voltmeter_range_V: float = 0.1,
        readings_per_point: int = 10,
    ) -> None:
        """Arm both instruments and configure measurement parameters.

        Args:
            current_A: DC source current in Amperes.
            compliance_A: Current compliance in Amperes.
            voltmeter_range_V: Full-scale voltage range in Volts.
            readings_per_point: Number of voltage samples ``take_reading()``
                collects per datapoint.
        """
        self._current_A = float(current_A)
        self._compliance_A = float(compliance_A)
        self._voltmeter_range_V = float(voltmeter_range_V)
        self._readings_per_point = int(readings_per_point)

        source = self._source  # type: ignore[attr-defined]
        meter = self._meter    # type: ignore[attr-defined]
        source.set_compliance(self._compliance_A)
        source.set_current(self._current_A)
        meter.set_range(self._voltmeter_range_V)

    def take_reading(self) -> dict[str, list[float]]:
        """Acquire ``readings_per_point`` DC voltage samples at the fixed current.

        Returns:
            ``{"voltage_V": list[float], "current_A": list[float]}`` of length
            ``readings_per_point`` (fixed at ``initiate()``).

        Raises:
            RuntimeError: If ``initiate()`` has not been called first.
        """
        if self._current_A is _NOT_INITIATED:
            raise RuntimeError("initiate() must be called before take_reading().")

        current = float(self._current_A)
        meter = self._meter  # type: ignore[attr-defined]

        voltages: list[float] = []
        currents: list[float] = []
        for _ in range(self._readings_per_point):
            voltages.append(float(meter.get_voltage()))
            currents.append(current)
        return {"voltage_V": voltages, "current_A": currents}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Query IDN from both drivers to verify they are reachable."""
        try:
            self._source.get_idn()  # type: ignore[attr-defined]
            self._meter.get_idn()   # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def standby(self) -> None:
        """Zero the current source and reset the initiated state."""
        self._source.set_current(0.0)  # type: ignore[attr-defined]
        self._current_A = _NOT_INITIATED
