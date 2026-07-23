# ---
# description: |
#   TensormeterRTM2MeasurementVI: resistance-tensor measurement method for
#   the Tensormeter RTM2. Unlike the Keithley-style measurement VIs, the
#   RTM2 firmware itself computes the tensor (van-der-Pauw / Hall / Kelvin
#   / ratiometric / differential — see its onboard "Analysis Mode") once
#   armed with a source current and a switch-matrix state sequence; this
#   VI configures that engine and reads back its own "Res A" / "Res B"
#   tensor components rather than reimplementing any tensor math.
# entry_point: Not run directly; instantiated by the Station factory.
# dependencies:
#   - cryosoft.virtual_instruments.base (MeasurementInstrumentBase)
#   - cryosoft.core.decorators (control)
#   - cryosoft.core.exceptions (CryoSoftConfigError)
# input: |
#   drivers = {"tensormeter": <TensormeterRTM2-style driver>}. init_params:
#   routes (dict[str, list[dict]] — named switch-state cycles, each state a
#   {"drv_minus": [...], "drv_plus": [...], "sns_minus": [...], "sns_plus":
#   [...]} mapping of BNC port lists 1-8), max_current_A (default 0.01),
#   max_voltage_V (default 10.0). initiate_measurement(current_amplitude_A,
#   averaging_time_s, analysis_mode, switch_sequence, readings_per_point)
#   must be called before the argument-less take_reading().
# process: |
#   initiate_measurement() optionally builds the configured switch_sequence's
#   states via the driver's build_switch_state() and arms set_switch_states(),
#   sets the Analysis Mode, averaging time, and source current amplitude, and
#   clears the device data buffer. take_reading() triggers readings_per_point
#   demodulation windows (one averaging_time_s apart) and reads back that many
#   Res A / Res B samples.
# output: |
#   Mean/error/array triple per quantity: {"res_a_ohm": float, "res_a_ohm_error":
#   float, "res_a_ohm_array": list[float], "res_b_ohm": float,
#   "res_b_ohm_error": float, "res_b_ohm_array": list[float]}, arrays of
#   length readings_per_point. Named after the firmware's own "Res A"/"Res B"
#   terms rather than asserting "sheet"/"Hall" semantics this VI cannot
#   verify — which physical quantity each represents depends on the
#   operator's chosen switch_sequence and analysis_mode, exactly as on the
#   real instrument.
# last_updated: 2026-07-23
# ---

"""TensormeterRTM2MeasurementVI — resistance-tensor measurement method (RTM2)."""

from __future__ import annotations

import math
import time
from typing import Any, ClassVar

from cryosoft.core.decorators import control
from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

# Duplicated from the driver's documented Analysis Mode encoding (vendor TCP
# Commands §3.12) — the VI layer cannot import cryosoft.drivers.* (layer
# contract C3), so it owns its own copy of this small, protocol-fixed mapping.
_ANALYSIS_MODE_VALUES: dict[str, int] = {
    "auto": 0,
    "kelvin": 1,
    "zero_offset_hall": 2,
    "van_der_pauw": 3,
    "ratiometric": 4,
    "differential": 5,
}

# A configured switch-state cycle entry must supply exactly these four BNC
# port lists (see TensormeterRTM2.build_switch_state()).
_ROUTE_KEYS = frozenset({"drv_minus", "drv_plus", "sns_minus", "sns_plus"})


class TensormeterRTM2MeasurementVI(MeasurementInstrumentBase):
    """Virtual Instrument for RTM2 resistance-tensor measurements.

    Uses one driver:
    * ``"tensormeter"`` — the Tensormeter RTM2 (or its sim twin).

    Workflow::

        vi.initiate_measurement(current_amplitude_A=1e-3, averaging_time_s=0.05,
                    analysis_mode="van_der_pauw", switch_sequence="vdp_standard",
                    readings_per_point=5)
        data = vi.take_reading()
        # data = {"res_a_ohm": float, "res_a_ohm_error": float,
        #         "res_a_ohm_array": list[float](5,), "res_b_ohm": float,
        #         "res_b_ohm_error": float, "res_b_ohm_array": list[float](5,)}

    Driver contract
    ---------------
    ``"tensormeter"`` driver must implement: ``build_switch_state(...)``,
    ``set_switch_states(*states)``, ``set_analysis_mode(mode)``,
    ``set_averaging_time(seconds)``, ``set_current_amplitude(amps)``,
    ``clear_data()``, ``trigger_demodulation()``, ``read_new_data()``,
    ``get_idn() -> str``.
    """

    display_label: str = "Resistance tensor (RTM2)"
    selector_label: ClassVar[str] = "Tensormeter RTM2"

    _ARRAY_KEYS, _SCALAR_COLUMNS = MeasurementInstrumentBase.quantity_columns(
        "res_a_ohm", "res_b_ohm"
    )
    measurement_data_keys: ClassVar[list[str]] = _ARRAY_KEYS
    measurement_scalar_columns: ClassVar[dict[str, str]] = _SCALAR_COLUMNS
    measurement_parameters: ClassVar[dict[str, ParamSpec]] = {
        "current_amplitude_A": ParamSpec(
            type=float, default=1e-3, unit="A",
            description="RTM2 AC source current amplitude",
        ),
        "averaging_time_s": ParamSpec(
            type=float, default=0.05, unit="s", min=0.0,
            description="RTM2 averaging/sampling period",
        ),
        "analysis_mode": ParamSpec(
            type=str, default="van_der_pauw",
            choices={
                "Auto": "auto",
                "Kelvin": "kelvin",
                "Zero-Offset Hall": "zero_offset_hall",
                "Van der Pauw": "van_der_pauw",
                "Ratiometric": "ratiometric",
                "Differential": "differential",
            },
            description="RTM2 onboard Analysis Mode",
        ),
        "switch_sequence": ParamSpec(
            type=str, default="",
            description=(
                "Configured route name (station config 'routes') cycling the "
                "switch matrix. Empty leaves the switch matrix as previously "
                "configured — e.g. wired by hand for bench work."
            ),
        ),
        "readings_per_point": ParamSpec(
            type=int, default=5, min=1,
            description="Demodulation windows averaged per point",
        ),
    }

    control_limits: ClassVar[dict[str, dict[str, str]]] = {
        "initiate_measurement": {"current_amplitude_A": "current_amplitude_A"},
    }

    def __init__(self, drivers: dict[str, object], **init_params: Any) -> None:
        """Validate the switch-sequence route table and safety limits from config.

        Args:
            drivers: ``{"tensormeter": <RTM2 driver>}``.
            **init_params: May provide ``routes`` (named switch-state
                cycles), ``max_current_A`` (default 0.01), ``max_voltage_V``
                (default 10.0).

        Raises:
            CryoSoftConfigError: If ``routes`` is malformed — not a mapping,
                an empty/non-string route name, an empty cycle, or a switch
                state missing/mis-keying the four BNC port lists.
        """
        super().__init__(drivers, **init_params)
        self._main = drivers["tensormeter"]

        routes_raw = init_params.get("routes", {}) or {}
        if not isinstance(routes_raw, dict):
            raise CryoSoftConfigError(
                f"TensormeterRTM2MeasurementVI 'routes' must be a mapping, "
                f"got {routes_raw!r}"
            )
        validated_routes: dict[str, list[dict[str, list[int]]]] = {}
        for name, cycle in routes_raw.items():
            if not isinstance(name, str) or not name:
                raise CryoSoftConfigError(
                    f"TensormeterRTM2MeasurementVI route name must be a "
                    f"non-empty str, got {name!r}"
                )
            if not isinstance(cycle, (list, tuple)) or not cycle:
                raise CryoSoftConfigError(
                    f"TensormeterRTM2MeasurementVI route {name!r} must map to "
                    f"a non-empty list of switch states"
                )
            validated_cycle: list[dict[str, list[int]]] = []
            for state_cfg in cycle:
                if not isinstance(state_cfg, dict) or set(state_cfg) != _ROUTE_KEYS:
                    raise CryoSoftConfigError(
                        f"TensormeterRTM2MeasurementVI route {name!r} switch "
                        f"state must be a mapping with exactly keys "
                        f"{sorted(_ROUTE_KEYS)}, got {state_cfg!r}"
                    )
                validated_cycle.append(
                    {key: [int(p) for p in ports] for key, ports in state_cfg.items()}
                )
            validated_routes[name] = validated_cycle
        self._routes: dict[str, list[dict[str, list[int]]]] = validated_routes

        max_current_A = float(init_params.get("max_current_A", 0.01))
        self._limits["current_amplitude_A"] = (-max_current_A, max_current_A)
        # max_voltage_V is not yet wired to a bounded @control parameter (RTM2
        # is current-sourced in this VI's workflow); recorded for the setup's
        # documented safety envelope and future use.
        self._max_voltage_V: float = float(init_params.get("max_voltage_V", 10.0))

        self._averaging_time_s: float = 0.05
        self._readings_per_point: int = 5
        self._initiated: bool = False

    # ------------------------------------------------------------------
    # MeasurementInstrumentBase implementation
    # ------------------------------------------------------------------

    def data_arrays(self, params) -> dict[str, int]:
        """Return ``{"res_a_ohm_array": n, "res_b_ohm_array": n}``, n = readings_per_point."""
        n = int(params["readings_per_point"])
        return {key: n for key in self.measurement_data_keys}

    # panel=False: arming is a deliberate act — reachable from the front
    # panel and from procedures, never from the compact monitor card.
    @control(panel=False)
    def initiate_measurement(
        self,
        current_amplitude_A: float = 1e-3,
        averaging_time_s: float = 0.05,
        analysis_mode: str = "van_der_pauw",
        switch_sequence: str = "",
        readings_per_point: int = 5,
    ) -> None:
        """Arm the RTM2's Analysis Mode engine and configure the source.

        Args:
            current_amplitude_A: AC source current amplitude in Amperes.
            averaging_time_s: Averaging/sampling period in seconds.
            analysis_mode: One of ``measurement_parameters["analysis_mode"]``'s
                choice values (e.g. ``"van_der_pauw"``).
            switch_sequence: Name of a configured ``routes`` switch-state
                cycle, or ``""`` to leave the switch matrix untouched.
            readings_per_point: Number of demodulation windows
                ``take_reading()`` averages per datapoint.

        Raises:
            ValueError: If ``analysis_mode`` or ``switch_sequence`` is not
                recognised.
        """
        mode_int = _ANALYSIS_MODE_VALUES.get(analysis_mode)
        if mode_int is None:
            raise ValueError(
                f"initiate_measurement: unknown analysis_mode {analysis_mode!r}; "
                f"must be one of {sorted(_ANALYSIS_MODE_VALUES)}"
            )

        driver = self._main  # type: ignore[attr-defined]

        if switch_sequence:
            cycle = self._routes.get(switch_sequence)
            if cycle is None:
                raise ValueError(
                    f"initiate_measurement: unknown switch_sequence "
                    f"{switch_sequence!r}; configured sequences are "
                    f"{list(self._routes)}"
                )
            states = [driver.build_switch_state(**state_cfg) for state_cfg in cycle]
            driver.set_switch_states(*states)

        # Live commissioning against real hardware (2026-07-23) found the
        # RTM2 silently ignores camp/cudc current-setpoint confirmations
        # while Control Mode is 0 ("Direct Voltage Output", the power-on
        # default) — set_current_amplitude() below would hang waiting for
        # an echo that never arrives. Control Mode 1 ("Feedback
        # Voltage/Current Output") must be set first for a current setpoint
        # to be honoured.
        driver.set_control_mode(1)
        # Reassert a plain continuous sine wave regardless of whatever
        # Waveform Mode a prior session/VI left the instrument in (e.g.
        # Pulse Train, found leftover during live commissioning
        # 2026-07-23) — this VI's take_reading() reads the 1st-harmonic
        # AC tensor column, which only means the sourced quantity when
        # the drive is actually a sine wave.
        driver.set_waveform_mode(0)

        # Per the vendor's own User Guide §3.2, Control Mode 1 regulates
        # like a CV/CC bench supply: the current AND voltage setpoints are
        # both live, and whichever is reached first governs. Live
        # commissioning (2026-07-23) found a stale vodc=0.01V left over
        # from an earlier session silently became the binding constraint —
        # every current_amplitude_A commanded thereafter was clamped to
        # whatever tiny current that leftover 0.01V implied for the DUT's
        # actual resistance, regardless of the requested value. Zero the
        # unused DC setpoints and raise the AC voltage amplitude ceiling to
        # max_voltage_V (config-owned, see __init__) so the CURRENT
        # setpoint below is what actually governs, not a leftover voltage.
        driver.set_current_dc(0.0)
        driver.set_voltage_dc(0.0)
        driver.set_voltage_amplitude(self._max_voltage_V)
        driver.set_voltage_protection(self._max_voltage_V)

        driver.set_analysis_mode(mode_int)
        driver.set_averaging_time(float(averaging_time_s))
        driver.set_current_amplitude(float(current_amplitude_A))
        driver.clear_data()

        self._averaging_time_s = float(averaging_time_s)
        self._readings_per_point = int(readings_per_point)
        self._initiated = True

    def take_reading(self) -> dict[str, list[float] | float]:
        """Trigger ``readings_per_point`` demodulation windows and read the tensor.

        Returns:
            The mean/error/array triple for both quantities (``res_a_ohm``,
            ``res_a_ohm_error``, ``res_a_ohm_array``, ``res_b_ohm``,
            ``res_b_ohm_error``, ``res_b_ohm_array``), arrays of length
            ``readings_per_point`` (fixed at ``initiate_measurement()``,
            NaN-padded if the instrument returns fewer rows than requested).

        Raises:
            RuntimeError: If ``initiate_measurement()`` has not been called first.
        """
        if not self._initiated:
            raise RuntimeError("initiate_measurement() must be called before take_reading().")

        driver = self._main  # type: ignore[attr-defined]
        n = self._readings_per_point

        # Live commissioning (2026-07-23) found two things that shape this
        # sequence: (1) trigger_demodulation() ABORTS any in-progress
        # averaging window and starts a fresh one (vendor doc), so calling
        # it repeatedly with a sleep in between (an earlier version of this
        # method did) kept interrupting the device before any window ever
        # completed — n calls in a tight loop instead collapse to a single
        # effective trigger on real hardware, since only the last one's
        # window survives to completion; (2) once armed, the RTM2 free-runs
        # background sampling, so a single trigger already yields many more
        # than one buffered row within one averaging_time_s. The one
        # consolidated sleep below lets roughly n settled windows
        # accumulate; taking the LAST n rows (not the first n) skips any
        # settling transient from whatever the source was doing just
        # before this call (e.g. ramping from 0 A to the armed setpoint).
        # A sim driver that appends exactly one row per trigger call is
        # unaffected by either change — the last n of exactly n rows is
        # the same n rows, and repeated calls simply append n rows outright.
        for _ in range(n):
            driver.trigger_demodulation()
        time.sleep(self._averaging_time_s * (n + 1))

        rows = driver.read_new_data()[-n:]
        # initiate_measurement() sources via current_amplitude_A (the AC
        # setpoint, camp) — live commissioning (2026-07-23) found the "Res
        # A"/"Res B" DC columns are correspondingly near-zero noise (no DC
        # component is being sourced), while the 1st-harmonic (in-phase,
        # real) column tracks the AC excitation actually being driven.
        res_a = [float(row["res_a_1st_re_ohm"]) for row in rows]
        res_b = [float(row["res_b_1st_re_ohm"]) for row in rows]
        res_a += [float("nan")] * (n - len(res_a))
        res_b += [float("nan")] * (n - len(res_b))

        a_mean, a_error = self.mean_and_sem([v for v in res_a if not math.isnan(v)])
        b_mean, b_error = self.mean_and_sem([v for v in res_b if not math.isnan(v)])
        return {
            "res_a_ohm_array": res_a,
            "res_a_ohm": a_mean,
            "res_a_ohm_error": a_error,
            "res_b_ohm_array": res_b,
            "res_b_ohm": b_mean,
            "res_b_ohm_error": b_error,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Query IDN from the driver to verify it is reachable."""
        try:
            self._main.get_idn()  # type: ignore[attr-defined]
            return True
        except Exception:
            return False

    def standby(self) -> None:
        """Zero the source current and reset the initiated state."""
        self._main.set_current_amplitude(0.0)  # type: ignore[attr-defined]
        self._initiated = False
