# ---
# description: |
#   LockInHarmonicMeasurementVI: single-driver Virtual Instrument for lock-in
#   first- and second-harmonic (1f/2f) transport measurements, sourced by the
#   lock-in's own internal oscillator (reference source "INT") through a
#   series resistor. Implements the self-describing measurement-method
#   standard (see MeasurementInstrumentBase). External-source (Keithley 6221,
#   synced via a common reference) is a planned follow-up — out of scope here.
# entry_point: Not run directly; instantiated by Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MeasurementInstrumentBase)
#   - cryosoft.core.decorators (control)
#   - cryosoft.core.plan (ParamSpec)
# input: |
#   drivers = {"lockin": <lock-in driver>}
#   init_params keys: series_resistance_ohm (the excitation series resistor,
#   a setup wiring constant; default 1e6).
#   initiate() must be called before take_reading(). Parameters (all keyword,
#   all defaulted): oscillator_amplitude_V, oscillator_frequency_Hz,
#   time_constant_s, n_readings.
# process: |
#   initiate() sets the lock-in to its internal reference and programs the
#   oscillator + time constant. take_reading() switches the demodulator
#   harmonic between 1 and 2 for each of n_readings pairs (a single-
#   demodulator lock-in reports one harmonic at a time), reading X/Y at each.
#   The excitation current is computed from the oscillator amplitude and the
#   series resistance (Ohm's law, series R >> sample R).
# output: |
#   {"x_1f_V": list[float], "y_1f_V": list[float], "x_2f_V": list[float],
#    "y_2f_V": list[float], "current_A": list[float]}, all length n_readings.
# last_updated: 2026-07-18
# ---

"""LockInHarmonicMeasurementVI — lock-in 1f/2f harmonic measurement (internal source)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from cryosoft.core.decorators import control
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

# Sentinel to detect the un-armed state.
_NOT_INITIATED = object()


class LockInHarmonicMeasurementVI(MeasurementInstrumentBase):
    """Measurement method for lock-in first- and second-harmonic (1f/2f) measurements.

    Uses one driver:
    * ``"lockin"`` — a phase-sensitive lock-in amplifier.

    Sources the AC excitation from the lock-in's own internal oscillator
    (reference source ``"INT"``) through an external series resistor, so the
    excitation current is ``oscillator_amplitude_V / series_resistance_ohm``
    (Ohm's law, valid when the series resistance is much larger than the
    sample resistance).

    Workflow (always run by a Procedure, never the GUI directly)::

        vi.initiate(oscillator_amplitude_V=1.0, oscillator_frequency_Hz=977.0)
        data = vi.take_reading()
        # data = {"x_1f_V": [...], "y_1f_V": [...], "x_2f_V": [...],
        #         "y_2f_V": [...], "current_A": [...]}, all length n_readings

    Harmonic behaviour
    ------------------
    Modeled after a single-demodulator lock-in (e.g. SRS SR830/860): only one
    harmonic can be demodulated at a time. ``take_reading()`` therefore
    switches the driver's harmonic setting between 1 and 2 for each of the
    ``n_readings`` reading pairs it collects, rather than reading both
    simultaneously — no two-lock-in or multi-demodulator hardware is assumed.

    External-source follow-up
    --------------------------
    Sourcing the AC excitation from an external Keithley 6221 (synced to the
    lock-in via a common reference) needs new AC/waveform driver capability
    on the 6221 that does not exist yet in this codebase; that is a separate,
    explicitly scoped follow-up, not implemented here.

    Driver contract
    ----------------
    The ``"lockin"`` driver must implement:
    * ``set_reference_source(str)`` / ``get_reference_source() -> str``
    * ``set_oscillator_amplitude(float)`` / ``get_oscillator_amplitude() -> float``
    * ``set_oscillator_frequency(float)`` / ``get_oscillator_frequency() -> float``
    * ``set_time_constant(float)`` / ``get_time_constant() -> float``
    * ``set_harmonic(int)`` / ``get_harmonic() -> int``
    * ``get_x() -> float`` / ``get_y() -> float``
    * ``get_idn() -> str``
    """

    display_label: str = "lock-in harmonic"
    selector_label: ClassVar[str] = "Lock-in 1f/2f (internal source)"

    measurement_data_keys: ClassVar[list[str]] = [
        "x_1f_V", "y_1f_V", "x_2f_V", "y_2f_V", "current_A",
    ]
    measurement_parameters: ClassVar[dict[str, ParamSpec]] = {
        "oscillator_amplitude_V": ParamSpec(
            type=float,
            default=1.0,
            unit="V",
            description="Lock-in internal oscillator (SINE OUT) amplitude, RMS",
        ),
        "oscillator_frequency_Hz": ParamSpec(
            type=float,
            default=977.0,
            unit="Hz",
            description="Lock-in internal oscillator frequency",
        ),
        "time_constant_s": ParamSpec(
            type=float,
            default=0.1,
            unit="s",
            description="Lock-in demodulator time constant",
        ),
        "n_readings": ParamSpec(
            type=int,
            default=10,
            min=1,
            description="1f/2f reading pairs collected per point",
        ),
    }

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        super().__init__(drivers, **init_params)
        self._lockin = drivers["lockin"]
        self._series_resistance_ohm: float = float(
            init_params.get("series_resistance_ohm", 1e6)
        )

        # Configuration state — set by initiate().
        self._armed: object = _NOT_INITIATED
        self._oscillator_amplitude_V: float = 1.0
        self._n_readings: int = 10

    # ------------------------------------------------------------------
    # Self-description
    # ------------------------------------------------------------------

    def data_arrays(self, params: Mapping[str, Any]) -> dict[str, int]:
        """Return per-array length ``n_readings`` for every declared data key.

        Args:
            params: Parameter mapping containing ``n_readings``.

        Returns:
            Per-point length for each 1f/2f data array.
        """
        n = int(params["n_readings"])
        return {key: n for key in self.measurement_data_keys}

    # ------------------------------------------------------------------
    # @control — arm the measurement
    # ------------------------------------------------------------------

    @control
    def initiate(
        self,
        oscillator_amplitude_V: float = 1.0,
        oscillator_frequency_Hz: float = 977.0,
        time_constant_s: float = 0.1,
        n_readings: int = 10,
    ) -> None:
        """Arm the lock-in for internal-source 1f/2f measurement.

        Args:
            oscillator_amplitude_V: Internal oscillator amplitude, RMS Volts.
            oscillator_frequency_Hz: Internal oscillator frequency in Hz.
            time_constant_s: Demodulator time constant in seconds.
            n_readings: Number of 1f/2f reading pairs ``take_reading()``
                collects per datapoint.
        """
        self._armed = True
        self._oscillator_amplitude_V = float(oscillator_amplitude_V)
        self._n_readings = int(n_readings)

        lockin = self._lockin  # type: ignore[attr-defined]
        lockin.set_reference_source("INT")
        lockin.set_oscillator_amplitude(self._oscillator_amplitude_V)
        lockin.set_oscillator_frequency(float(oscillator_frequency_Hz))
        lockin.set_time_constant(float(time_constant_s))

    # ------------------------------------------------------------------
    # take_reading — NOT @monitored, NOT @control (Procedure-only)
    # ------------------------------------------------------------------

    def take_reading(self) -> dict[str, list[float]]:
        """Collect one 1f/2f datapoint by switching harmonic between reads.

        Returns:
            ``{"x_1f_V": list(n_readings,), "y_1f_V": list(n_readings,),
            "x_2f_V": list(n_readings,), "y_2f_V": list(n_readings,),
            "current_A": list(n_readings,)}``.

        Raises:
            RuntimeError: If ``initiate()`` has not been called first.
        """
        if self._armed is _NOT_INITIATED:
            raise RuntimeError("initiate() must be called before take_reading().")

        lockin = self._lockin  # type: ignore[attr-defined]
        current = self._oscillator_amplitude_V / self._series_resistance_ohm

        x_1f: list[float] = []
        y_1f: list[float] = []
        x_2f: list[float] = []
        y_2f: list[float] = []
        currents: list[float] = []

        for _ in range(self._n_readings):
            lockin.set_harmonic(1)
            x_1f.append(float(lockin.get_x()))
            y_1f.append(float(lockin.get_y()))

            lockin.set_harmonic(2)
            x_2f.append(float(lockin.get_x()))
            y_2f.append(float(lockin.get_y()))

            currents.append(current)

        return {
            "x_1f_V": x_1f,
            "y_1f_V": y_1f,
            "x_2f_V": x_2f,
            "y_2f_V": y_2f,
            "current_A": currents,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Query IDN from the lock-in driver to verify it is reachable.

        Returns:
            True if the lock-in responds to ``get_idn()``.
        """
        try:
            self._lockin.get_idn()  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def standby(self) -> None:
        """Zero the oscillator amplitude and reset the initiated state."""
        self._lockin.set_oscillator_amplitude(0.0)  # type: ignore[attr-defined]
        self._armed = _NOT_INITIATED
