# ---
# description: |
#   DeltaModeMeasurementVI: multi-driver Virtual Instrument pairing a Keithley
#   6221 current source with a Keithley 2182A nanovoltmeter for delta-mode
#   resistance measurements. Implements the self-describing measurement-method
#   standard (see MeasurementInstrumentBase): initiate() arms the delta engine
#   and take_reading() collects one datapoint. take_reading() is NOT @monitored
#   — it is long-running and only driven by the Procedure layer.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MeasurementInstrumentBase)
#   - cryosoft.core.decorators (control)
#   - cryosoft.core.plan (ParamSpec)
# input: |
#   drivers = {"source": <K6221 driver>, "meter": <K2182A driver>}
#   initiate() must be called before take_reading(). Parameters (all keyword,
#   all defaulted): current (A), n_readings (int), voltmeter_range_V (V,
#   enumerated 2182A range), compliance_V (V), delay_s (s), compliance_abort
#   (bool), cold_switch (bool).
# process: |
#   initiate() arms the delta engine via source.configure_and_start_delta().
#   take_reading() calls source.acquire_delta_readings() to collect samples;
#   the delta engine can return FEWER than n_readings values (compliance abort
#   / repeated read failures), so the arrays are padded with NaN to n_readings
#   and the true count reported in the n_valid scalar column.
#   standby() calls source.stop_delta_mode() to abort the running engine.
# output: |
#   {"voltage_V": list[float](n_readings,), "current_A": list[float](n_readings,),
#    "n_valid": int} — arrays always exactly n_readings long.
# last_updated: 2026-07-13
# ---

"""DeltaModeMeasurementVI — Keithley 6221 + 2182A delta-mode measurement VI."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from cryosoft.core.decorators import control
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

# Sentinel to detect the un-armed state.
_NOT_INITIATED = object()


class DeltaModeMeasurementVI(MeasurementInstrumentBase):
    """Measurement method for delta-mode resistance measurements.

    Uses two drivers:
    * ``"source"`` — Keithley 6221 current source.
    * ``"meter"``  — Keithley 2182A nanovoltmeter.

    Workflow (always run by a Procedure, never the GUI directly)::

        vi.initiate(current=1e-6, n_readings=100)
        data = vi.take_reading()
        # data = {"voltage_V": list(100,), "current_A": list(100,), "n_valid": int}

    Implements the self-describing measurement-method standard documented on
    ``MeasurementInstrumentBase``. The delta engine can legitimately return
    fewer than ``n_readings`` samples (a compliance abort, or repeated read
    failures on the real 6221), so ``take_reading()`` pads both arrays with
    ``float("nan")`` up to ``n_readings`` and reports the real sample count in
    the ``n_valid`` scalar column — the fixed-shape guarantee the HDF5 layout
    depends on.

    ``take_reading()`` is deliberately **not** tagged ``@monitored`` because it
    is long-running and should only be triggered by the Procedure layer.

    Reading loop: unlike the DC method, the delta engine is armed with a fixed
    peak current by ``configure_and_start_delta()``, so the current cannot be
    reprogrammed in place. ``set_delta_current()`` therefore stops and re-arms
    the engine (see its docstring for the timing cost) — the reason the setter
    is named for the operation it performs rather than mirroring the DC VI's
    ``set_source_current``.
    """

    display_label: str = "delta-mode resistance"
    selector_label: ClassVar[str] = "Delta mode (6221 + 2182A)"

    # Reading-loop declaration: the peak delta current. Re-arms the engine
    # between readings (delta mode has no in-place current change), letting the
    # generic sweep procedure loop a list of currents at every sweep point.
    reading_setters: ClassVar[dict[str, str]] = {"current": "set_delta_current"}

    measurement_data_keys: ClassVar[list[str]] = ["voltage_V", "current_A"]
    measurement_scalar_columns: ClassVar[dict[str, str]] = {"n_valid": "int"}
    measurement_parameters: ClassVar[dict[str, ParamSpec]] = {
        "current": ParamSpec(
            type=float,
            default=1e-6,
            unit="A",
            description="Peak delta current (±I, reversed each cycle)",
        ),
        "n_readings": ParamSpec(
            type=int,
            default=100,
            min=1,
            description="Readings per point (delta mode)",
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
            description="Delta inter-transition delay (0 = hardware minimum)",
        ),
        "compliance_abort": ParamSpec(
            type=bool,
            default=True,
            description="Abort the delta run if the source reaches compliance",
        ),
        "cold_switch": ParamSpec(
            type=bool,
            default=False,
            description="Cold-switch between current reversals (lower thermal EMF)",
        ),
    }

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._source = drivers["source"]
        self._meter = drivers["meter"]

        # Configuration state — set by initiate().
        self._armed: object = _NOT_INITIATED
        self._current: float = 1e-6
        self._n_readings: int = 100
        self._delay_s: float = 0.01
        # The remaining armed parameters, retained verbatim so
        # set_delta_current() can re-arm the engine changing only the current.
        self._voltmeter_range_V: float = 0.01
        self._compliance_V: float = 1.0
        self._compliance_abort: bool = True
        self._cold_switch: bool = False

    # ------------------------------------------------------------------
    # Self-description
    # ------------------------------------------------------------------

    def data_arrays(self, params: Mapping[str, Any]) -> dict[str, int]:
        """Return ``{"voltage_V": n, "current_A": n}`` with n = n_readings.

        Args:
            params: Parameter mapping containing ``n_readings``.

        Returns:
            Per-point length for each delta-mode data array.
        """
        n = int(params["n_readings"])
        return {"voltage_V": n, "current_A": n}

    # ------------------------------------------------------------------
    # @control — arm the measurement
    # ------------------------------------------------------------------

    @control
    def initiate(
        self,
        current: float = 1e-6,
        n_readings: int = 100,
        voltmeter_range_V: float = 0.01,
        compliance_V: float = 1.0,
        delay_s: float = 0.01,
        compliance_abort: bool = True,
        cold_switch: bool = False,
    ) -> None:
        """Arm the delta engine so ``take_reading()`` can deliver on demand.

        Calls ``source.configure_and_start_delta()`` immediately so the
        instrument is armed and ready. The driver call sequence is unchanged
        from the previous ``configure()`` interface.

        Args:
            current: Peak delta current in Amperes (low level = -current).
            n_readings: Readings per ``take_reading()`` acquisition.
            voltmeter_range_V: Keithley 2182A measurement range in Volts.
            compliance_V: Source voltage compliance limit in Volts.
            delay_s: Inter-transition delay in seconds (0 = hardware minimum).
            compliance_abort: Abort the delta run if the source hits compliance.
            cold_switch: Cold-switch between current reversals.
        """
        self._armed = True
        self._current = float(current)
        self._n_readings = int(n_readings)
        self._delay_s = float(delay_s)
        self._voltmeter_range_V = float(voltmeter_range_V)
        self._compliance_V = float(compliance_V)
        self._compliance_abort = bool(compliance_abort)
        self._cold_switch = bool(cold_switch)

        self._source.configure_and_start_delta(  # type: ignore[attr-defined]
            float(current),
            int(n_readings),
            float(delay_s),
            float(compliance_V),
            float(voltmeter_range_V),
            bool(compliance_abort),
            bool(cold_switch),
        )

    # ------------------------------------------------------------------
    # @control — the reading-loop setter
    # ------------------------------------------------------------------

    @control
    def set_delta_current(self, current: float) -> None:
        """Re-arm the delta engine at a new peak current.

        The per-reading setter behind ``reading_setters["current"]``. Delta mode
        has no in-place current change: the 6221 latches its peak amplitude when
        ``configure_and_start_delta()`` arms the engine. This method therefore
        stops the engine and re-arms it with every other armed parameter
        (readings, delay, range, compliance, abort and cold-switch flags)
        unchanged, so only the current differs.

        Because it re-arms, each loop step pays a full delta start-up: the
        engine discards its running average and the first readings after the
        change include the source's settling transient. Callers that need
        settled data should size ``n_readings`` accordingly rather than assuming
        the DC method's cost-free current change.

        The value is the *peak* amplitude of a current that delta mode reverses
        each cycle (``+current`` / ``-current``), so looping the sign is
        redundant — ``1e-6`` and ``-1e-6`` arm the same waveform.

        Args:
            current: New peak delta current in Amperes.

        Raises:
            RuntimeError: If ``initiate()`` has not been called first.
        """
        if self._armed is _NOT_INITIATED:
            raise RuntimeError("initiate() must be called before set_delta_current().")

        self._current = float(current)
        source = self._source  # type: ignore[attr-defined]
        source.stop_delta_mode()
        source.configure_and_start_delta(
            self._current,
            self._n_readings,
            self._delay_s,
            self._compliance_V,
            self._voltmeter_range_V,
            self._compliance_abort,
            self._cold_switch,
        )

    # ------------------------------------------------------------------
    # take_reading — NOT @monitored, NOT @control (Procedure-only)
    # ------------------------------------------------------------------

    def take_reading(self) -> dict[str, list[float]]:
        """Collect one delta-mode datapoint from the armed engine.

        Polls ``source.acquire_delta_readings()`` for up to ``n_readings``
        samples. The engine can return fewer, so both arrays are padded with
        ``float("nan")`` to exactly ``n_readings`` and the real count reported
        as ``n_valid``.

        Returns:
            ``{"voltage_V": list(n_readings,), "current_A": list(n_readings,),
            "n_valid": int}``. Padded (invalid) positions are ``NaN`` in both
            arrays.

        Raises:
            RuntimeError: If ``initiate()`` has not been called first.
        """
        if self._armed is _NOT_INITIATED:
            raise RuntimeError("initiate() must be called before take_reading().")

        n = self._n_readings
        raw: list[float] = self._source.acquire_delta_readings(  # type: ignore[attr-defined]
            n, self._delay_s
        )

        voltages: list[float] = [float(v) for v in raw[:n]]
        n_valid = len(voltages)
        currents: list[float] = [self._current] * n_valid

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

    def standby(self) -> None:
        """Abort the delta engine, turn off outputs, and reset state."""
        try:
            self._source.stop_delta_mode()   # type: ignore[attr-defined]
        except Exception:
            pass
        self._armed = _NOT_INITIATED
