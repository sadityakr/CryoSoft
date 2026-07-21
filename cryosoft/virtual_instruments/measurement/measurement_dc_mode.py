# ---
# description: |
#   DCModeMeasurementVI: multi-driver Virtual Instrument pairing a Keithley
#   6221 current source with a Keithley 2182A nanovoltmeter for DC
#   resistance measurements. initiate_measurement() arms the current source,
#   voltmeter range, and compliance. take_reading() collects the readings.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MeasurementInstrumentBase)
#   - cryosoft.core.decorators (control)
#   - cryosoft.core.plan (ParamSpec)
# input: |
#   drivers = {"source": <K6221 driver>, "meter": <K2182A driver>}
#   initiate_measurement() must be called before take_reading().
# process: |
#   initiate_measurement() sets compliance and range and starts sourcing current.
#   take_reading() collects n_readings voltage samples. If compliance_abort is
#   enabled, it monitors source.is_in_compliance() before each reading and
#   stops early if triggered, padding remaining spots with NaN.
#   standby() zeros the current source and resets the armed state.
# output: |
#   {"voltage_V": list[float](n_readings,), "current_A": list[float](n_readings,),
#    "n_valid": int}
# last_updated: 2026-07-22
# ---

"""DCModeMeasurementVI — Keithley 6221 + 2182A DC-mode measurement VI."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from cryosoft.core.decorators import control
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

# Sentinel to detect the un-armed state.
_NOT_INITIATED = object()

logger = logging.getLogger(__name__)


class DCModeMeasurementVI(MeasurementInstrumentBase):
    """Measurement method for DC resistance measurements using K6221 and K2182A.

    Uses two drivers:
    * ``"source"`` — Keithley 6221 current source.
    * ``"meter"``  — Keithley 2182A nanovoltmeter.

    Both source and meter roles typically point to the same ``keithley_6220``
    driver in the config because the 2182A is cabled via RS-232 serial to the
    6221.

    Workflow (always run by a Procedure, never the GUI directly)::

        vi.initiate_measurement(current=1e-6, n_readings=100)
        data = vi.take_reading()
        # data = {"voltage_V": list(100,), "current_A": list(100,), "n_valid": int}
    """

    display_label: str = "DC mode resistance"
    selector_label: ClassVar[str] = "DC mode (6221 + 2182A)"

    # Reading-loop declaration: the DC current. Can be changed in-place cheaply.
    reading_setters: ClassVar[dict[str, str]] = {"current": "set_dc_current"}

    measurement_data_keys: ClassVar[list[str]] = ["voltage_V", "current_A"]
    measurement_scalar_columns: ClassVar[dict[str, str]] = {"n_valid": "int"}
    measurement_parameters: ClassVar[dict[str, ParamSpec]] = {
        "current": ParamSpec(
            type=float,
            default=1e-6,
            unit="A",
            description="DC source current",
        ),
        "n_readings": ParamSpec(
            type=int,
            default=100,
            min=1,
            description="Number of DC readings per point",
        ),
        "voltmeter_range_V": ParamSpec(
            type=float,
            default=0.01,
            unit="V",
            choices={
                "10 mV": 0.01,
                "100 mV": 0.1,
                "1 V": 1.0,
                "10 V": 10.0,
                "100 V": 100.0,
            },
            description="Keithley 2182A voltmeter measurement range",
        ),
        "compliance_V": ParamSpec(
            type=float,
            default=1.0,
            unit="V",
            description="Source voltage compliance limit",
        ),
        "delay_s": ParamSpec(
            type=float,
            default=0.01,
            unit="s",
            description="Inter-reading delay",
        ),
        "compliance_abort": ParamSpec(
            type=bool,
            default=True,
            description="Abort the measurement if the source reaches compliance",
        ),
    }

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._source = drivers["source"]
        self._meter = drivers["meter"]

        # Configuration state — set by initiate_measurement().
        self._armed: object = _NOT_INITIATED
        self._current: float = 1e-6
        self._n_readings: int = 100
        self._delay_s: float = 0.01
        self._voltmeter_range_V: float = 0.01
        self._compliance_V: float = 1.0
        self._compliance_abort: bool = True

    # ------------------------------------------------------------------
    # Self-description
    # ------------------------------------------------------------------

    def data_arrays(self, params: Mapping[str, Any]) -> dict[str, int]:
        """Return ``{"voltage_V": n, "current_A": n}`` with n = n_readings.

        Args:
            params: Parameter mapping containing ``n_readings``.

        Returns:
            Per-point length for each data array.
        """
        n = int(params["n_readings"])
        return {"voltage_V": n, "current_A": n}

    # ------------------------------------------------------------------
    # @control — arm the measurement
    # ------------------------------------------------------------------

    @control(panel=False)
    def initiate_measurement(
        self,
        current: float = 1e-6,
        n_readings: int = 100,
        voltmeter_range_V: float = 0.01,
        compliance_V: float = 1.0,
        delay_s: float = 0.01,
        compliance_abort: bool = True,
    ) -> None:
        """Arm both instruments and configure measurement parameters.

        Args:
            current: DC source current in Amperes.
            n_readings: Readings per ``take_reading()`` acquisition.
            voltmeter_range_V: Voltmeter measurement range in Volts.
            compliance_V: Source voltage compliance limit in Volts.
            delay_s: Inter-reading delay in seconds.
            compliance_abort: Abort the run if the source hits compliance.
        """
        self._armed = True
        self._current = float(current)
        self._n_readings = int(n_readings)
        self._delay_s = float(delay_s)
        self._voltmeter_range_V = float(voltmeter_range_V)
        self._compliance_V = float(compliance_V)
        self._compliance_abort = bool(compliance_abort)

        source = self._source  # type: ignore[attr-defined]
        meter = self._meter    # type: ignore[attr-defined]

        source.set_compliance(self._compliance_V)
        meter.set_range(self._voltmeter_range_V)
        source.set_current(self._current)

    # ------------------------------------------------------------------
    # @control — the reading-loop setter
    # ------------------------------------------------------------------

    @control
    def set_dc_current(self, current: float) -> None:
        """Reprogram the source current without re-arming the measurement.

        The per-reading setter behind ``reading_setters["current"]``. Changing
        the current is cheap in DC mode (does not require re-arming the trace
        buffer).

        Args:
            current: New DC source current in Amperes.

        Raises:
            RuntimeError: If ``initiate_measurement()`` has not been called first.
        """
        if self._armed is _NOT_INITIATED:
            raise RuntimeError("initiate_measurement() must be called before set_dc_current().")

        self._current = float(current)
        self._source.set_current(self._current)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # take_reading — NOT @monitored, NOT @control (Procedure-only)
    # ------------------------------------------------------------------

    def take_reading(self) -> dict[str, list[float]]:
        """Collect one DC-mode datapoint from the armed instruments.

        Takes ``n_readings`` voltage samples. If ``compliance_abort`` is enabled
        and the source hits compliance, stops early and pads with NaN.

        Returns:
            ``{"voltage_V": list, "current_A": list, "n_valid": int}``.

        Raises:
            RuntimeError: If ``initiate_measurement()`` has not been called first.
        """
        if self._armed is _NOT_INITIATED:
            raise RuntimeError("initiate_measurement() must be called before take_reading().")

        n = self._n_readings
        voltages: list[float] = []
        currents: list[float] = []
        n_valid = 0

        source = self._source  # type: ignore[attr-defined]
        meter = self._meter    # type: ignore[attr-defined]

        for i in range(n):
            if self._compliance_abort:
                if source.is_in_compliance():
                    logger.warning("Keithley 6221 is in compliance — aborting DC measurement early.")
                    break

            try:
                v = float(meter.get_voltage())
                voltages.append(v)
                currents.append(self._current)
                n_valid += 1
            except Exception as exc:
                logger.error("DC read error at index %d: %s", i, exc)
                break

            if i < n - 1 and self._delay_s > 0:
                time.sleep(self._delay_s)

        # Pad with NaN if we stopped early
        pad = n - n_valid
        if pad > 0:
            nan = float("nan")
            voltages.extend([nan] * pad)
            currents.extend([nan] * pad)

        return {
            "voltage_V": voltages,
            "current_A": currents,
            "n_valid": n_valid,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Query IDN from both drivers to verify they are reachable."""
        try:
            self._source.get_idn()   # type: ignore[attr-defined]
            self._meter.get_idn()    # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def standby(self) -> None:
        """Zero the current source output and reset the armed state."""
        try:
            self._source.set_current(0.0)   # type: ignore[attr-defined]
        except Exception:
            pass
        self._armed = _NOT_INITIATED
