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
#   max_voltage_V (default 10.0), configured_externally (default False — see
#   MeasurementInstrumentBase's "Externally configured instruments"
#   standard). initiate_measurement(current_amplitude_A, averaging_time_s,
#   analysis_mode, switch_sequence, readings_per_point, tensor_component)
#   must be called before the argument-less take_reading().
# process: |
#   initiate_measurement() optionally builds the configured switch_sequence's
#   states via the driver's build_switch_state() and arms set_switch_states(),
#   sets the Analysis Mode, averaging time, and source current amplitude, and
#   clears the device data buffer — unless configured_externally, in which
#   case only the data path is armed (channel selection, buffer, timing
#   readback, provenance snapshot) and the external tool's own configuration
#   is left untouched. take_reading() triggers readings_per_point
#   demodulation windows (one averaging_time_s apart) and reads back that
#   many Res A / Res B samples for the selected tensor_component.
# output: |
#   Mean/error/array triple per quantity: {"res_a_ohm": float, "res_a_ohm_error":
#   float, "res_a_ohm_array": list[float], "res_b_ohm": float,
#   "res_b_ohm_error": float, "res_b_ohm_array": list[float]}, arrays of
#   length readings_per_point, plus "n_valid": int (rows actually delivered
#   before NaN padding). Named after the firmware's own "Res A"/"Res B"
#   terms rather than asserting "sheet"/"Hall" semantics this VI cannot
#   verify — which physical quantity each represents depends on the
#   operator's chosen switch_sequence and analysis_mode, exactly as on the
#   real instrument. Plus "raw_channels_block": list[list[float]]
#   (readings_per_point x 44), every raw driver column for every reading —
#   see MeasurementInstrumentBase's "Raw diagnostic blocks" standard.
# last_updated: 2026-07-28
# ---

"""TensormeterRTM2MeasurementVI — resistance-tensor measurement method (RTM2)."""

from __future__ import annotations

import logging
import math
import time
from typing import Any, ClassVar

from cryosoft.core.decorators import control
from cryosoft.core.exceptions import CryoSoftConfigError
from cryosoft.core.plan import ParamSpec
from cryosoft.virtual_instruments.base import MeasurementInstrumentBase

log = logging.getLogger(__name__)

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

# Tensor components the driver's 44-column data block carries per side (Res A
# / Res B), each a "{c}" slot in the res_a_{c}_ohm / res_b_{c}_ohm column pair
# — see tensormeter_rtm2._DATA_COLUMNS. Data-path only (extracting a column
# pair writes nothing to the instrument), so this parameter stays ACTIVE even
# when configured_externally, unlike every excitation/analysis/routing param
# below.
_TENSOR_COMPONENTS: frozenset[str] = frozenset(
    {"dc", "1st_re", "1st_im", "2nd_re", "2nd_im", "3rd_re", "3rd_im"}
)

# Duplicated from the driver's documented data-block column order (vendor TCP
# Commands §3.1) — the VI layer cannot import cryosoft.drivers.* (layer
# contract C3), so it owns its own copy, same reasoning as
# _ANALYSIS_MODE_VALUES above. Must stay in lockstep with
# tensormeter_rtm2._DATA_COLUMNS; this is the raw diagnostic block's declared
# channel-label list (see MeasurementInstrumentBase's "Raw diagnostic
# blocks" standard).
_RAW_CHANNEL_NAMES: tuple[str, ...] = (
    "time_s",
    "input_voltage_dc_V",
    "current_dc_A",
    "output_voltage_dc_V",
    "resistance_2w_dc_ohm",
    "input_voltage_ampl_V",
    "current_ampl_A",
    "output_voltage_ampl_V",
    "impedance_2w_ac_ohm",
    "res_a_dc_ohm",
    "res_a_1st_re_ohm",
    "res_a_1st_im_ohm",
    "res_a_2nd_re_ohm",
    "res_a_2nd_im_ohm",
    "res_a_3rd_re_ohm",
    "res_a_3rd_im_ohm",
    "res_b_dc_ohm",
    "res_b_1st_re_ohm",
    "res_b_1st_im_ohm",
    "res_b_2nd_re_ohm",
    "res_b_2nd_im_ohm",
    "res_b_3rd_re_ohm",
    "res_b_3rd_im_ohm",
    "switch_status",
    "lockin_frequency_Hz",
    "voltage_dc_setpoint_V",
    "current_dc_setpoint_A",
    "current_dc_setpoint_A_2",
    "current_ampl_setpoint_A",
    "voltage_protection_V",
    "current_protection_A",
    "input_voltage_peak_range_fill",
    "current_peak_range_fill",
    "output_voltage_peak_range_fill",
    "reference_voltage_peak_range_fill",
    "voltage_input_range_V",
    "voltage_output_range_V",
    "current_range_A",
    "series_resistance_ohm",
    "sampling_duration_s",
    "lock_quality",
    "analysis_multisample_mode",
    "dio0_V",
    "dio1_V",
)


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
        #         "res_b_ohm_error": float, "res_b_ohm_array": list[float](5,),
        #         "n_valid": int,
        #         "raw_channels_block": list[list[float]](5, 44)}

    With ``init_params["configured_externally"] = True`` (see
    ``MeasurementInstrumentBase``'s "Externally configured instruments"
    standard), ``initiate_measurement()`` only arms the CryoSoft-owned data
    path and leaves ``current_amplitude_A``/``averaging_time_s``/
    ``analysis_mode``/``switch_sequence`` to whatever the external tool
    (e.g. TMCS) set; ``tensor_component`` still selects the extracted
    column pair either way.

    Driver contract
    ---------------
    ``"tensormeter"`` driver must implement: ``build_switch_state(...)``,
    ``set_switch_states(*states)``, ``set_analysis_mode(mode)``,
    ``set_averaging_time(seconds)``, ``set_current_amplitude(amps)``,
    ``clear_data()``, ``trigger_demodulation()``, ``read_new_data()``,
    ``get_idn() -> str``, and, for the externally configured standard:
    ``close()``, ``ensure_connected()``, ``get_averaging_time()``,
    ``get_settings_snapshot() -> dict``, ``select_data_channels(*indices)``.
    """

    display_label: str = "Resistance tensor (RTM2)"
    selector_label: ClassVar[str] = "Tensormeter RTM2"

    _ARRAY_KEYS, _SCALAR_COLUMNS = MeasurementInstrumentBase.quantity_columns(
        "res_a_ohm", "res_b_ohm"
    )
    measurement_data_keys: ClassVar[list[str]] = _ARRAY_KEYS
    measurement_scalar_columns: ClassVar[dict[str, str]] = {
        **_SCALAR_COLUMNS, "n_valid": "int"
    }
    # Raw diagnostic block (see MeasurementInstrumentBase's "Raw diagnostic
    # blocks" standard): every one of the driver's 44 raw columns, preserved
    # per reading alongside the res_a_ohm/res_b_ohm pair selected above.
    measurement_raw_blocks: ClassVar[dict[str, list[str]]] = {
        "raw_channels_block": list(_RAW_CHANNEL_NAMES)
    }
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
        "tensor_component": ParamSpec(
            type=str, default="1st_re",
            choices={
                "DC": "dc",
                "1st Harmonic (Re)": "1st_re",
                "1st Harmonic (Im)": "1st_im",
                "2nd Harmonic (Re)": "2nd_re",
                "2nd Harmonic (Im)": "2nd_im",
                "3rd Harmonic (Re)": "3rd_re",
                "3rd Harmonic (Im)": "3rd_im",
            },
            description=(
                "Tensor component extracted from the driver's data block "
                "into the saved res_a_ohm/res_b_ohm columns (res_a_{c}_ohm/"
                "res_b_{c}_ohm) — the pair that gets the statistically "
                "rigorous mean/SEM/n_valid treatment for analysis and "
                "session export. Every raw component (res_a_dc_ohm, "
                "res_a_1st_re_ohm, ...) is independently plottable "
                "regardless of this choice (see MeasurementInstrumentBase's "
                "'Raw diagnostic blocks' standard), so getting this wrong "
                "no longer hides data — it only decides which pair is "
                "analyzed. Data-path only — writes nothing to the "
                "instrument, so it stays active in externally configured "
                "mode too; the operator picks it to match the drive "
                "configured on the instrument (dc drive -> 'dc', sine AC -> "
                "'1st_re', harmonic studies -> '2nd_*'/'3rd_*')."
            ),
        ),
    }

    control_limits: ClassVar[dict[str, dict[str, str]]] = {
        "initiate_measurement": {"current_amplitude_A": "current_amplitude_A"},
    }

    # The externally-configured standard's self-description (see
    # MeasurementInstrumentBase's "Externally configured instruments"
    # section): the external tool (e.g. TMCS) owns excitation, analysis, and
    # routing. tensor_component and readings_per_point are data-path only
    # (they write nothing to the instrument) and stay out of this set, so
    # they remain operator-controlled — and hence rendered in the procedure
    # form — in every mode.
    externally_owned_parameters: ClassVar[frozenset[str]] = frozenset(
        {"current_amplitude_A", "averaging_time_s", "analysis_mode", "switch_sequence"}
    )

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
        self._tensor_component: str = "1st_re"
        # The externally configured standard's provenance snapshot (see
        # MeasurementInstrumentBase's "Externally configured instruments"
        # section): set by initiate_measurement() in external mode, read
        # (duck-typed) by the sweep procedure to record it into the run's
        # HDF5 /metadata. None until a run has armed in external mode.
        self.last_settings_snapshot: dict | None = None

        if self._configured_externally:
            # Detach-when-idle standard (see MeasurementInstrumentBase's
            # "Detached-idle lifecycle" / BaseVirtualInstrument's
            # "Detach-when-idle declaration"): born detached, so starting
            # CryoSoft while the vendor tool (TMCS) is open builds cleanly —
            # the RTM2 firmware serves only one TCP client at a time. This
            # VI has no @monitored fields, so idle-detached generates zero
            # tick-loop traffic.
            self._detach()

    @property
    def detach_when_idle(self) -> bool:
        """Declare the RTM2's single-client firmware fact (see the base standard).

        Narrows ``BaseVirtualInstrument.detach_when_idle`` from config
        state: the RTM2 only needs to release its session when the vendor
        tool (TMCS) owns configuration, never in internal mode.
        """
        return self._configured_externally

    # ------------------------------------------------------------------
    # MeasurementInstrumentBase implementation
    # ------------------------------------------------------------------

    def data_arrays(self, params) -> dict[str, int]:
        """Return ``{"res_a_ohm_array": n, "res_b_ohm_array": n}``, n = readings_per_point."""
        n = int(params["readings_per_point"])
        return {key: n for key in self.measurement_data_keys}

    def raw_block_row_counts(self, params) -> dict[str, int]:
        """Return ``{"raw_channels_block": n}``, n = readings_per_point."""
        return {"raw_channels_block": int(params["readings_per_point"])}

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
        tensor_component: str = "1st_re",
    ) -> None:
        """Arm the RTM2's Analysis Mode engine and configure the source.

        When ``self._configured_externally`` is true (see
        ``MeasurementInstrumentBase``'s "Externally configured instruments"
        standard), ``current_amplitude_A``, ``averaging_time_s``,
        ``analysis_mode``, and ``switch_sequence`` are accepted but NOT
        written to the instrument — the external tool (e.g. TMCS) owns
        excitation, analysis, and routing. Only the CryoSoft-owned data
        path (channel selection, buffer, timing readback, provenance
        snapshot) is armed; see ``_initiate_measurement_external()``.

        Args:
            current_amplitude_A: AC source current amplitude in Amperes.
                Ignored in externally configured mode.
            averaging_time_s: Averaging/sampling period in seconds. Ignored
                in externally configured mode — the actual value is read
                back from the instrument instead (see
                ``_initiate_measurement_external()``).
            analysis_mode: One of ``measurement_parameters["analysis_mode"]``'s
                choice values (e.g. ``"van_der_pauw"``). Ignored in
                externally configured mode.
            switch_sequence: Name of a configured ``routes`` switch-state
                cycle, or ``""`` to leave the switch matrix untouched.
                Ignored in externally configured mode.
            readings_per_point: Number of demodulation windows
                ``take_reading()`` averages per datapoint.
            tensor_component: One of ``measurement_parameters
                ["tensor_component"]``'s choice values (e.g. ``"1st_re"``)
                selecting which ``res_a_{c}_ohm``/``res_b_{c}_ohm`` column
                pair ``take_reading()`` extracts. Data-path only — active
                in every mode, including externally configured.

        Raises:
            ValueError: If ``analysis_mode``, ``switch_sequence``, or
                ``tensor_component`` is not recognised.
            CryoSoftCommunicationError: In externally configured mode, if
                the instrument is unreachable, held by the external tool,
                or hung (see ``_initiate_measurement_external()``).
        """
        if tensor_component not in _TENSOR_COMPONENTS:
            raise ValueError(
                f"initiate_measurement: unknown tensor_component "
                f"{tensor_component!r}; must be one of "
                f"{sorted(_TENSOR_COMPONENTS)}"
            )
        self._tensor_component = tensor_component

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
        else:
            cycle = None

        if self._configured_externally:
            self._initiate_measurement_external(
                driver, readings_per_point, current_amplitude_A,
                averaging_time_s, analysis_mode, switch_sequence,
            )
            return

        if cycle is not None:
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

    def _initiate_measurement_external(
        self,
        driver: Any,
        readings_per_point: int,
        current_amplitude_A: float,
        averaging_time_s: float,
        analysis_mode: str,
        switch_sequence: str,
    ) -> None:
        """Arm the data path only — the externally configured initiate branch.

        Implements the externally configured standard's
        ``initiate_measurement()`` contract (see
        ``MeasurementInstrumentBase``): never writes excitation, analysis,
        or routing state (the external tool, e.g. TMCS, owns it); verifies
        connectivity with a true round trip; arms only what
        ``take_reading()``'s decode and timing depend on; and records a
        provenance snapshot.

        Args:
            driver: The ``"tensormeter"`` driver (``self._main``).
            readings_per_point: Demodulation windows per point (CryoSoft-
                side data concept, still honored in this mode).
            current_amplitude_A: Ignored; logged as an ignored parameter.
            averaging_time_s: Ignored; logged as an ignored parameter (the
                actual value is read back from the instrument instead).
            analysis_mode: Ignored; logged as an ignored parameter.
            switch_sequence: Ignored; logged as an ignored parameter.

        Raises:
            CryoSoftCommunicationError: If the instrument cannot be
                reconnected, or the connectivity round trip fails (hung
                firmware, or the channel currently held by the external
                tool) — see ``TensormeterRTM2.ensure_connected()``/
                ``get_idn()``.
        """
        # (1) Acquire + connectivity: reacquire via the base's _attach()
        # (the detach-when-idle standard, see BaseVirtualInstrument) so
        # is_attached() correctly reflects the reacquired session for the
        # duration of the measurement window, then a TRUE round trip
        # (raises on a hung or externally-held channel, per the driver's
        # liveness fix), never a check that can succeed vacuously.
        self._attach()
        driver.get_idn()

        # (2) Data-path arming (CryoSoft-owned, always asserted): the
        # explicit full ascending channel list is confirmed the ONLY way to
        # restore the default 44-column decode take_reading() depends on —
        # a bare/empty selc collapses to zero rows, and a leftover subset
        # silently mis-keys every column (see
        # TensormeterRTM2.select_data_channels()). clear_data() discards
        # whatever the free-running instrument accumulated before this
        # session — the newd pointer is device-global, so a fresh session
        # would otherwise ingest a previous client's backlog.
        driver.select_data_channels(*range(44))
        driver.clear_data()

        # (3) Timing readback: the settle sleep in take_reading() must
        # reflect what the external tool actually set, not the ignored
        # averaging_time_s argument above.
        self._averaging_time_s = driver.get_averaging_time()

        # (4) Provenance snapshot: in external mode the snapshot — not the
        # ignored measurement_parameters — is the record of what the data
        # was actually taken with. The sweep procedure records this into
        # the run's HDF5 /metadata (see DataManager.record_settings_snapshot()).
        self.last_settings_snapshot = driver.get_settings_snapshot()
        log.info(
            "TensormeterRTM2MeasurementVI: externally configured — armed "
            "data path, recorded settings snapshot: %s",
            self.last_settings_snapshot,
        )

        # (6) Skip (external-tool-owned): set_control_mode, set_waveform_mode,
        # DC-setpoint zeroing, set_voltage_amplitude/set_voltage_protection,
        # set_analysis_mode, set_averaging_time, set_current_amplitude,
        # switch routing. The ignored-parameter list is derived from
        # externally_owned_parameters (the externally-configured standard's
        # self-description) rather than hardcoded here, so the ClassVar
        # stays the single source of truth.
        ignored_values = {
            "current_amplitude_A": current_amplitude_A,
            "averaging_time_s": averaging_time_s,
            "analysis_mode": analysis_mode,
            "switch_sequence": switch_sequence,
        }
        ignored_str = ", ".join(
            f"{name}={ignored_values[name]!r}"
            for name in sorted(self.externally_owned_parameters)
        )
        log.info(
            "TensormeterRTM2MeasurementVI: externally configured — ignoring "
            "%s (external tool owns excitation/analysis/routing)",
            ignored_str,
        )

        # (7) Soft consistency check: WARNING only, never a refusal —
        # external configuration is human-owned.
        self._warn_if_tensor_component_inconsistent(self.last_settings_snapshot)

        # (5) Internal state.
        self._readings_per_point = int(readings_per_point)
        self._initiated = True

    def _warn_if_tensor_component_inconsistent(self, snapshot: dict) -> None:
        """Log a WARNING when ``self._tensor_component`` looks inconsistent with *snapshot*.

        Soft consistency check only — never raises or refuses; external
        configuration is human-owned (see ``MeasurementInstrumentBase``'s
        "Externally configured instruments" standard), so this is a hint,
        not a gate. A wrong guess here no longer hides data — every raw
        component is independently plottable regardless of
        ``tensor_component`` (see the "Raw diagnostic blocks" standard's
        plot-column extension) — it only means ``res_a_ohm``/``res_b_ohm``,
        the pair with proper mean/SEM/n_valid statistics, was computed from
        the wrong component. Two checks, both guarded with ``.get()`` so a
        missing snapshot key never crashes:

        * A harmonic component (``1st_*``/``2nd_*``/``3rd_*``) is selected
          while the snapshot's Waveform Mode (``wfmd``) is Pulse Train
          (``1``) — harmonic demodulation assumes a continuous sine drive.
        * A non-``dc`` component is selected while the snapshot reports no
          AC current amplitude (``camp`` == 0) — nothing is being driven
          for a harmonic component to measure.

        Args:
            snapshot: The arming-time settings snapshot (``get_settings_
                snapshot()``'s return value).
        """
        component = self._tensor_component
        wfmd = snapshot.get("wfmd")
        if component != "dc" and wfmd == 1:
            log.warning(
                "TensormeterRTM2MeasurementVI: tensor_component=%r selected "
                "(a harmonic component) but the arming-time snapshot reports "
                "Waveform Mode = Pulse Train (wfmd=1) — harmonic demodulation "
                "expects a continuous sine drive.",
                component,
            )
        camp = snapshot.get("camp")
        if component != "dc" and camp is not None and float(camp) == 0.0:
            log.warning(
                "TensormeterRTM2MeasurementVI: tensor_component=%r selected "
                "but the arming-time snapshot reports zero AC current "
                "amplitude (camp=0) — no drive for a harmonic component to "
                "measure.",
                component,
            )

    def take_reading(self) -> dict[str, list[float] | float]:
        """Trigger ``readings_per_point`` demodulation windows and read the tensor.

        Row selection is LAST-*n*, not first-*n*: the RTM2 free-runs once
        armed, so the buffer can hold more than ``n`` rows by the time this
        reads it, and the earliest of those can still be a settling
        transient from whatever the source was doing just before this call
        (e.g. ramping from 0 A to the armed setpoint) — see the call
        sequence note below. Stats are computed over the delivered rows
        BEFORE they are padded to the declared length (the ``n_valid``
        standard's ordering, see ``MeasurementInstrumentBase``): filtering
        NaN out of an already-padded array would conflate CryoSoft's own
        padding with a NaN the instrument itself emitted (e.g. a
        ratiometric divide-by-zero, plausible under an externally
        configured analysis mode).

        Returns:
            The mean/error/array triple for both quantities (``res_a_ohm``,
            ``res_a_ohm_error``, ``res_a_ohm_array``, ``res_b_ohm``,
            ``res_b_ohm_error``, ``res_b_ohm_array``), arrays of length
            ``readings_per_point`` (fixed at ``initiate_measurement()``,
            NaN-padded if the instrument returns fewer rows than
            requested), plus ``n_valid`` — the number of rows the
            instrument actually delivered before padding.

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

        # Raw diagnostic block: every one of the driver's 44 columns for
        # every delivered row, preserved verbatim alongside the
        # tensor_component-selected pair below (see MeasurementInstrumentBase's
        # "Raw diagnostic blocks" standard). Row-axis padding mirrors the
        # res_a/res_b padding a few lines below; the channel axis is fixed by
        # _RAW_CHANNEL_NAMES and never padded.
        raw_block = [
            [float(row[name]) for name in _RAW_CHANNEL_NAMES] for row in rows
        ]
        block_pad = n - len(rows)
        raw_block += [[float("nan")] * len(_RAW_CHANNEL_NAMES)] * block_pad

        # The operator-chosen tensor_component picks which Res A/Res B
        # column pair to extract (see measurement_parameters
        # ["tensor_component"]) — data-path only, so this runs identically
        # regardless of configured_externally. Live commissioning
        # (2026-07-23) found the DC columns are near-zero noise under AC
        # drive, which is why the default is the 1st-harmonic (real) pair
        # rather than "dc".
        key_a = f"res_a_{self._tensor_component}_ohm"
        key_b = f"res_b_{self._tensor_component}_ohm"
        delivered_a = [float(row[key_a]) for row in rows]
        delivered_b = [float(row[key_b]) for row in rows]

        # n_valid: the delta-mode ordering (stats computed BEFORE padding).
        # It reports how many rows the instrument delivered — the
        # under-delivery count both quantities' extraction drew from. An
        # instrument-emitted NaN inside an individual delivered value
        # (e.g. a ratiometric divide-by-zero) is additionally excluded
        # from that quantity's own mean/SEM below, without narrowing this
        # single, per-row n_valid further.
        n_valid = len(rows)
        a_mean, a_error = self.mean_and_sem(
            [v for v in delivered_a if not math.isnan(v)]
        )
        b_mean, b_error = self.mean_and_sem(
            [v for v in delivered_b if not math.isnan(v)]
        )

        pad = n - len(rows)
        res_a = delivered_a + [float("nan")] * pad
        res_b = delivered_b + [float("nan")] * pad

        return {
            "res_a_ohm_array": res_a,
            "res_a_ohm": a_mean,
            "res_a_ohm_error": a_error,
            "res_b_ohm_array": res_b,
            "res_b_ohm": b_mean,
            "res_b_ohm_error": b_error,
            "n_valid": n_valid,
            "raw_channels_block": raw_block,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def standby(self) -> None:
        """Put the instrument in a safe-off idle state.

        Internal mode: zeros the source current. Externally configured
        mode: skips the zero (it would clobber the external tool's own
        source state — sample-access operations call ``standby()`` on
        every measurement VI, not just after a run this VI armed). The
        session release itself is no longer this method's job — the
        ``detach_when_idle`` property declared above makes
        ``BaseVirtualInstrument``'s detach-when-idle standard release the
        connection automatically once this method returns (see its class
        docstring), which is the automatic handoff that lets the operator
        open the vendor tool between runs.
        """
        if not self._configured_externally:
            self._main.set_current_amplitude(0.0)  # type: ignore[attr-defined]
        self._initiated = False
