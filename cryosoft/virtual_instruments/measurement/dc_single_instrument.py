# ---
# description: |
#   DCSingleInstrumentVI: behavior-based VI for DC resistance measurements
#   using a single source-measure unit (SMU) that both sources current and
#   measures voltage. Same method contract as DCSeparateMeasurementVI;
#   procedures can swap between the two by changing only the YAML config.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (DCMeasurementBase)
#   - cryosoft.core.decorators (control)
# input: |
#   drivers = {"main": <SMU driver instance, e.g. Keithley 2400>}
#   initiate(current_A, compliance_A, voltmeter_range_V, readings_per_point)
#   must be called before the argument-less take_reading().
# process: |
#   initiate() programs the SMU with source current, compliance and range and
#   stores readings_per_point. take_reading() reads voltage that many times at
#   constant current and returns arrays of voltages and currents.
# output: |
#   {"voltage_V": list[float], "current_A": list[float]} of length
#   readings_per_point.
# last_updated: 2026-07-13
# ---

"""DCSingleInstrumentVI — DC measurement with a single SMU (e.g. Keithley 2400)."""

from __future__ import annotations

from typing import Any

from cryosoft.core.decorators import control
from cryosoft.virtual_instruments.base import DCMeasurementBase

_NOT_INITIATED = object()


class DCSingleInstrumentVI(DCMeasurementBase):
    """Virtual Instrument for DC resistance measurements with a single SMU.

    Uses one driver (``"main"``) that can both source current and measure voltage.

    Workflow::

        vi.initiate(current_A=1e-6, compliance_A=1e-3, voltmeter_range_V=0.1,
                    readings_per_point=50)
        data = vi.take_reading()
        # data = {"voltage_V": list[float](50,), "current_A": list[float](50,)}

    To swap to a two-instrument setup, replace this VI with
    ``DCSeparateMeasurementVI`` in the YAML config. The procedure is unchanged.

    Driver contract
    ---------------
    The ``"main"`` driver must implement:
    * ``set_current(float)``         — set source current in Amperes
    * ``set_compliance(float)``      — set compliance voltage in Volts
    * ``set_range(float)``           — set measurement range in Volts
    * ``get_current() -> float``     — return sourced current in Amperes
    * ``get_voltage() -> float``     — return measured voltage in Volts
    * ``get_idn() -> str``           — instrument identification string
    """

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._instrument = drivers["main"]

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
        """Arm the SMU and configure measurement parameters.

        Args:
            current_A: DC source current in Amperes.
            compliance_A: Compliance voltage limit in Volts.
            voltmeter_range_V: Full-scale voltage measurement range in Volts.
            readings_per_point: Number of voltage samples ``take_reading()``
                collects per datapoint.
        """
        self._current_A = float(current_A)
        self._compliance_A = float(compliance_A)
        self._voltmeter_range_V = float(voltmeter_range_V)
        self._readings_per_point = int(readings_per_point)

        instr = self._instrument  # type: ignore[attr-defined]
        instr.set_compliance(self._compliance_A)
        instr.set_current(self._current_A)
        instr.set_range(self._voltmeter_range_V)

    def take_reading(self) -> dict[str, list[float]]:
        """Acquire ``readings_per_point`` voltage samples at the fixed current.

        Returns:
            ``{"voltage_V": list[float], "current_A": list[float]}`` of length
            ``readings_per_point`` (fixed at ``initiate()``).

        Raises:
            RuntimeError: If ``initiate()`` has not been called first.
        """
        if self._current_A is _NOT_INITIATED:
            raise RuntimeError("initiate() must be called before take_reading().")

        instr = self._instrument  # type: ignore[attr-defined]
        current = float(self._current_A)

        voltages: list[float] = []
        currents: list[float] = []
        for _ in range(self._readings_per_point):
            voltages.append(float(instr.get_voltage()))
            currents.append(current)
        return {"voltage_V": voltages, "current_A": currents}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Query IDN from the SMU to verify it is reachable."""
        try:
            self._instrument.get_idn()  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def standby(self) -> None:
        """Zero the source current and reset the initiated state."""
        self._instrument.set_current(0.0)  # type: ignore[attr-defined]
        self._current_A = _NOT_INITIATED
