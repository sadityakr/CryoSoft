# ---
# description: |
#   DCModeMeasurementVI: multi-driver Virtual Instrument pairing a Keithley
#   6221 current source with a Keithley 2182A nanovoltmeter for DC
#   resistance measurements. initiate_measurement() arms the current source,
#   voltmeter range, and compliance. take_reading() collects the readings.
#   read_now() is a bench-test-only control that triggers a manual read and
#   caches it in the last_voltage_V / last_mean_voltage_V / last_n_valid
#   monitored fields so the front panel can show what the Keithley returned.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MeasurementInstrumentBase)
#   - cryosoft.core.decorators (control, monitored)
#   - cryosoft.core.plan (ParamSpec)
# input: |
#   drivers = {"source": <K6221 driver>, "meter": <K2182A driver>}
#   initiate_measurement() must be called before take_reading() or read_now().
# process: |
#   initiate_measurement() sets compliance and range and starts sourcing current.
#   take_reading() collects n_readings voltage samples. If compliance_abort is
#   enabled, it monitors source.is_in_compliance() before each reading and
#   stops early if triggered, padding remaining spots with NaN. read_now()
#   simply calls take_reading() and stores the result for display; it is the
#   human-facing bench-test trigger, never called by a Procedure.
#   standby() zeros the current source and resets the armed state.
# output: |
#   Mean/error/array triple per quantity: {"voltage_V": float, "voltage_V_error":
#   float, "voltage_V_array": list[float](n_readings,), "current_A": float,
#   "current_A_error": float, "current_A_array": list[float](n_readings,),
#   "n_valid": int}
# last_updated: 2026-07-22
# ---

"""DCModeMeasurementVI — Keithley 6221 + 2182A DC-mode measurement VI."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any, ClassVar

from cryosoft.core.decorators import control, monitored
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
        # data = {"voltage_V": float, "voltage_V_error": float,
        #         "voltage_V_array": list(100,), "current_A": float,
        #         "current_A_error": float, "current_A_array": list(100,),
        #         "n_valid": int}

    Bench-testing from the GUI front panel uses ``read_now()`` instead:
    after ``initiate_measurement()`` arms the instruments, clicking
    "Read Now" collects the same ``n_readings`` samples and surfaces them
    through the ``last_voltage_V`` / ``last_mean_voltage_V`` / ``last_n_valid``
    monitored fields so an operator can confirm a configured current yields
    sane readings before running a procedure.
    """

    display_label: str = "DC mode resistance"
    selector_label: ClassVar[str] = "DC mode (6221 + 2182A)"

    # Reading-loop declaration: the DC current. Can be changed in-place cheaply.
    reading_setters: ClassVar[dict[str, str]] = {"current": "set_dc_current"}

    _ARRAY_KEYS, _SCALAR_COLUMNS = MeasurementInstrumentBase.quantity_columns(
        "voltage_V", "current_A"
    )
    measurement_data_keys: ClassVar[list[str]] = _ARRAY_KEYS
    measurement_scalar_columns: ClassVar[dict[str, str]] = {
        **_SCALAR_COLUMNS, "n_valid": "int"
    }
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

        # Cached result of the last manual read_now() bench-test call, so the
        # last_*/n_valid monitored fields below can report it. None until the
        # first read_now() (take_reading() itself never touches this cache —
        # it stays Procedure-only, see the "take_reading" section).
        self._last_reading: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Self-description
    # ------------------------------------------------------------------

    def data_arrays(self, params: Mapping[str, Any]) -> dict[str, int]:
        """Return ``{"voltage_V_array": n, "current_A_array": n}`` with n = n_readings.

        Args:
            params: Parameter mapping containing ``n_readings``.

        Returns:
            Per-point length for each data array.
        """
        try:
            n = int(params["n_readings"])
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"DC VI: n_readings={params.get('n_readings')!r} cannot be "
                f"converted to int — check the form field is not blank. ({exc})"
            ) from exc
        if n < 1:
            raise ValueError(f"DC VI: n_readings must be >= 1, got {n!r}")
        return {key: n for key in self.measurement_data_keys}

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
        logger.debug(
            "initiate_measurement called: current=%r, n_readings=%r, "
            "voltmeter_range_V=%r, compliance_V=%r, delay_s=%r, compliance_abort=%r",
            current, n_readings, voltmeter_range_V, compliance_V, delay_s, compliance_abort,
        )
        try:
            n_readings_int = int(n_readings)
        except (ValueError, TypeError) as exc:
            raise ValueError(
                f"DC VI: n_readings={n_readings!r} cannot be converted to int "
                f"— check the form field is not blank. ({exc})"
            ) from exc
        if n_readings_int < 1:
            raise ValueError(f"DC VI: n_readings must be >= 1, got {n_readings_int!r}")

        self._armed = True
        self._current = float(current)
        self._n_readings = n_readings_int
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
    # @monitored — last manual read_now() result, for front-panel bench testing
    # ------------------------------------------------------------------

    @monitored
    def last_voltage_V(self) -> float | None:
        """Most recent valid voltage from the last read_now() call, or None."""
        if self._last_reading is None or self._last_reading["n_valid"] == 0:
            return None
        return self._last_reading["voltage_V_array"][self._last_reading["n_valid"] - 1]

    @monitored
    def last_mean_voltage_V(self) -> float | None:
        """Mean voltage from the last read_now() call, or None.

        Reads the mean ``take_reading()`` already computed (via
        ``mean_and_sem``) rather than recomputing it, so there is one source
        of truth for the mean/error/array triple.
        """
        if self._last_reading is None or self._last_reading["n_valid"] == 0:
            return None
        return self._last_reading["voltage_V"]

    @monitored
    def last_n_valid(self) -> int | None:
        """Valid-reading count from the last read_now() call, or None."""
        if self._last_reading is None:
            return None
        return self._last_reading["n_valid"]

    # ------------------------------------------------------------------
    # @control — manual bench-test read
    # ------------------------------------------------------------------

    @control(panel=False)
    def read_now(self) -> None:
        """Take one manual reading from the front panel and cache it for display.

        Bench-test hook: calls ``take_reading()`` and stores the result so the
        ``last_voltage_V`` / ``last_mean_voltage_V`` / ``last_n_valid``
        monitored fields report it on the next tick. Distinct from
        ``take_reading()`` itself, which stays Procedure-only per the
        measurement-method standard — this is the human-facing equivalent for
        checking that a configured current actually produces sane readings
        before running a procedure.

        Raises:
            RuntimeError: If ``initiate_measurement()`` has not been called first.
        """
        self._last_reading = self.take_reading()

    # ------------------------------------------------------------------
    # take_reading — NOT @monitored, NOT @control (Procedure-only)
    # ------------------------------------------------------------------

    def take_reading(self) -> dict[str, list[float] | float]:
        """Collect one DC-mode datapoint from the armed instruments.

        Takes ``n_readings`` voltage samples. If ``compliance_abort`` is
        enabled, checks compliance ONCE before the sampling loop (not before
        every individual reading) and skips the whole datapoint (all NaN) if
        already tripped.

        Live commissioning against a real 6221 (2026-07-22) found that
        alternating ``is_in_compliance()``'s ``:STATus:QUEStionable:
        CONDition?`` query with ``meter.get_voltage()``'s serial-relay
        ``SEND``/``ENTer?`` pair on every single reading corrupted the 622x's
        GPIB output-queue state, producing ``-410 "Query INTERRUPTED"`` on
        nearly every iteration (confirmed against the 622x manual's Query
        Error / QYE definition — reading an empty output queue — and
        reproduced identically at 0.05s, 0.1s, and 1.0s inter-reading
        delays, ruling out a timing race). Delta mode has never hit this: its
        reading loop polls only one query type (``:CALC1:DATA:FRES?``) and
        relies on the instrument's own hardware compliance-abort flag
        (``:SOUR:DELT:CAB``) instead of a per-sample software poll. Checking
        once here matches that pattern. Trade-off: a compliance trip mid-loop
        is no longer caught until the *next* ``take_reading()`` call rather
        than immediately — accepted because the per-reading interleaving was
        actively corrupting the read path it was meant to protect.

        Returns:
            The mean/error/array triple for both quantities (``voltage_V``,
            ``voltage_V_error``, ``voltage_V_array``, ``current_A``,
            ``current_A_error``, ``current_A_array``) plus ``n_valid``.

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

        if self._compliance_abort and source.is_in_compliance():
            logger.warning("Keithley 6221 is in compliance — skipping DC measurement.")
        else:
            for i in range(n):
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

        # Pad with NaN if we stopped early (or skipped entirely)
        pad = n - n_valid
        if pad > 0:
            nan = float("nan")
            voltages.extend([nan] * pad)
            currents.extend([nan] * pad)

        v_mean, v_error = self.mean_and_sem(voltages[:n_valid])
        c_mean, c_error = self.mean_and_sem(currents[:n_valid])

        return {
            "voltage_V_array": voltages,
            "voltage_V": v_mean,
            "voltage_V_error": v_error,
            "current_A_array": currents,
            "current_A": c_mean,
            "current_A_error": c_error,
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
