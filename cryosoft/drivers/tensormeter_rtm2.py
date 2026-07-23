# ---
# description: |
#   Real driver for the Tensormeter RTM2 (Tensor Instruments / HZDR
#   Innovation), an all-in-one resistance-tensor analyzer that replaces a
#   lock-in + SMU + DMM + switch matrix for van-der-Pauw / Hall / AC
#   transport measurements. Unlike every other driver in this repo, RTM2
#   has no VISA/GPIB interface: it is a proprietary big-endian binary TCP
#   protocol (vendor doc: "Tensormeter RTM2 TCP Commands", Oct 2023) served
#   directly by the instrument's own firmware on port 6340. This driver is
#   a thin, typed wrapper over the vendor's official `rtm2` Python package
#   (github.com/hzdrinno/rtm2-python, Apache-2.0), not a reimplementation
#   of the wire protocol.
# entry_point: Not run directly; imported by the Virtual Instruments layer.
# dependencies:
#   - rtm2 >= 1.2.3 (vendor's official RTM2 client library)
# input: |
#   Instantiated with "host" or "host:port" (port defaults to 6340, the
#   documented default). set_analysis_mode()/set_switch_states() must be
#   configured before read_new_data()/read_all_data() return meaningful
#   Res A / Res B tensor columns — see the class docstring.
# process: |
#   Every setter round-trips through the RTM2's own echo: send the command,
#   wait for the server's confirmation packet (which may report a
#   range-coerced value, never assume the value sent is the value applied),
#   and return the confirmed value. Every getter instead reads the
#   locally-cached last-known value (the RTM2 protocol has no "query
#   without side effect" — see _get_cached()), refreshing the cache with a
#   `gass` (Get-All-Server-Settings) round trip on first use.
# output: |
#   Plain float/int/str/bool from setters/getters; read_new_data()/
#   read_all_data() return list[dict[str, float]], one dict per data row,
#   keyed by the vendor doc's documented column names (TCP Commands §3.1).
# last_updated: 2026-07-23
# ---

"""Real Tensormeter RTM2 resistance-tensor analyzer driver."""

from __future__ import annotations

import logging
import math

import rtm2

from cryosoft.core.exceptions import CryoSoftCommunicationError

log = logging.getLogger(__name__)

_DEFAULT_PORT = 6340
_DEFAULT_TIMEOUT_S = 5.0

# Column layout of the data array returned by [newd]/[alld] when no
# [selc] channel filter has been applied (the default: "all channels in
# ascending order") — fixed by the vendor's TCP Commands reference §3.1.
# Column 26/27 are both documented as "Current DC Setpoint (A)" in the
# vendor doc (apparent documentation duplication); named distinctly here
# so the two never collide as dict keys.
_DATA_COLUMNS: tuple[str, ...] = (
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

# Bump commands (§3.10) confirm under the corresponding RANGE command's own
# name, never their own — e.g. sending "voru" gets back a "vorg"-tagged
# packet carrying the new range, not a "voru"-tagged one. Verified against
# the vendor doc's own worked example for [voru] and the installed `rtm2`
# package's _COMMANDS table (bump commands declare reply="").
_BUMP_CONFIRM_PARAM = {
    "viru": "virg", "vird": "virg",
    "voru": "vorg", "vord": "vorg",
    "crup": "crng", "crdn": "crng",
    "srup": "sres", "srdn": "sres",
}


def _values_match(cached: object, sent_args: tuple) -> bool:
    """Return True if *cached* already equals the value *sent_args* requested.

    Used by :meth:`TensormeterRTM2._send_and_confirm`'s no-echo fallback:
    a no-arg command matches any non-None cached value; a single numeric
    argument is compared with a float tolerance (the device may report a
    range-coerced value that differs in the last bit); a sequence argument
    (``swit``/``selc``/``puar``) is compared element-wise the same way.
    """
    if not sent_args:
        return True
    target = sent_args[0] if len(sent_args) == 1 else tuple(sent_args)
    if isinstance(cached, (int, float)) and isinstance(target, (int, float)):
        return math.isclose(float(cached), float(target), rel_tol=1e-6, abs_tol=1e-9)
    if isinstance(cached, (tuple, list)) and isinstance(target, (tuple, list)):
        return len(cached) == len(target) and all(
            _values_match(c, (t,)) for c, t in zip(cached, target)
        )
    return cached == target


class TensormeterRTM2:
    """Real Tensormeter RTM2 resistance-tensor analyzer driver.

    RTM2 replaces a lock-in + SMU + DMM + switch matrix with one box: an
    8-BNC-port switch matrix routes DRV+/DRV-/SNS+/SNS- to any contacts,
    and an onboard "Analysis Mode" (see :meth:`set_analysis_mode`) computes
    the resistance tensor itself — including a built-in Van-der-Pauw mode
    (mode 3) and Zero-Offset Hall mode (mode 2) — once armed with a
    synchronized switch-state sequence (:meth:`set_switch_states`) and a
    source current/voltage. CryoSoft does not reimplement van-der-Pauw
    math: it configures the mode + switch sequence + source and reads the
    firmware's own "Res A" / "Res B" tensor components back out of the
    data array (see :meth:`read_new_data`).

    The protocol has no `*IDN?` equivalent, so :meth:`get_idn` is
    synthesized: it round-trips a harmless `gass` (Get-All-Server-Settings)
    query to confirm liveness, then returns a fixed identity string.

    Driver contract:
    1. It is a Python class.
    2. __init__ accepts a single resource string, ``"host"`` or
       ``"host:port"`` (RTM2 has no VISA/GPIB interface — see the module
       docstring for why this deviates from every other driver here).
    3. It is importable via cryosoft.drivers.tensormeter_rtm2.
    """

    def __init__(self, resource_string: str) -> None:
        """Open the TCP connection to the RTM2.

        Args:
            resource_string: ``"host"`` or ``"host:port"`` (port defaults
                to 6340, RTM2's documented default). Use the vendor's
                ``rtm2.Discover()`` to find the instrument's IP on the LAN.

        Raises:
            CryoSoftCommunicationError: If the TCP connection cannot be
                established.
        """
        host, _, port_str = resource_string.partition(":")
        port = int(port_str) if port_str else _DEFAULT_PORT

        self._host = host
        self._port = port
        self._timeout_s = _DEFAULT_TIMEOUT_S
        self._rtm = rtm2.RTM2(host, port, timeout=self._timeout_s)
        try:
            self._rtm.connect()
        except ConnectionError as exc:
            raise CryoSoftCommunicationError(
                f"Cannot open Tensormeter RTM2 at {resource_string}: {exc}",
                vi_name="TensormeterRTM2",
            ) from exc

        # Live commissioning against real hardware (2026-07-23) found the
        # device pushes its "Actual Analysis Mode" state update tagged
        # literally "mod?" (matching the vendor doc's own §3.12 command
        # name), but rtm2-python v1.2.3's own _COMMANDS registry only
        # recognises "modq" — so every gass()/other burst that includes
        # this field fails with "Unknown incoming command: mod?" and
        # read_until() aborts early. Patch an instance-level alias (not the
        # shared class dict) so _parse_packet() decodes "mod?" with the
        # same >B/state spec as "modq"; get_detected_analysis_mode() reads
        # whichever key the device actually used.
        self._rtm._COMMANDS = {
            **self._rtm._COMMANDS, "mod?": self._rtm._COMMANDS["modq"],
        }

    # ------------------------------------------------------------------
    # Low-level command primitives
    # ------------------------------------------------------------------

    def _send_and_confirm(
        self, cmd: str, *args: float, wait_for: str | None = None, timeout: float | None = None
    ) -> object:
        """Send *cmd* with *args* and return the server's confirmed value.

        Args:
            cmd: The 4-character RTM2 command to send.
            *args: The command's arguments (int/float, per the vendor's
                per-command payload format).
            wait_for: The parameter name the confirmation arrives under, if
                different from *cmd* (see :data:`_BUMP_CONFIRM_PARAM`).
            timeout: Round-trip timeout in seconds; defaults to the
                connection's configured timeout.

        Returns:
            The confirmed value (echoed, possibly range-coerced by the
            instrument — never assume the value sent is the value applied).

        Raises:
            CryoSoftCommunicationError: On a transport failure, a
                protocol-level error, or a missing confirmation.
        """
        wait_for = wait_for or cmd
        try:
            result = self._rtm.read_until(
                wait_for, send=(cmd, *args), timeout=timeout or self._timeout_s
            )
        except (ConnectionError, ValueError) as exc:
            raise CryoSoftCommunicationError(
                f"Tensormeter RTM2 {cmd!r} failed: {exc}", vi_name="TensormeterRTM2"
            ) from exc
        if result.error:
            raise CryoSoftCommunicationError(
                f"Tensormeter RTM2 {cmd!r} protocol error: {result.error}",
                vi_name="TensormeterRTM2",
            )
        for upd in result.updates:
            if upd.parameter == wait_for:
                return upd.value

        # Live commissioning against real hardware (2026-07-23) found the
        # RTM2 does not push a fresh state update when a setter's new value
        # is identical to what the device already holds — e.g. re-sending
        # cudc(0.0) when cudc was already 0.0 from a prior call produces no
        # echo at all, so the wait above times out on a harmless no-op.
        # Fall back to a `gass` (Get-All-Server-Settings) round trip: if
        # the requested value is already the server's current value,
        # treat this as a successful no-op instead of raising.
        self._refresh_all_settings(timeout=timeout or self._timeout_s)
        cached = self._rtm.get_state().get(wait_for)
        if cached is not None and _values_match(cached, args):
            return cached

        raise CryoSoftCommunicationError(
            f"Tensormeter RTM2: no confirmation received for {cmd!r}",
            vi_name="TensormeterRTM2",
        )

    def _refresh_all_settings(self, timeout: float | None = None) -> None:
        """Round-trip `gass` and drain the burst of setting updates it triggers."""
        try:
            result = self._rtm.read_until(
                "any", send="gass", timeout=timeout or self._timeout_s, listen=0.5
            )
        except (ConnectionError, ValueError) as exc:
            raise CryoSoftCommunicationError(
                f"Tensormeter RTM2 'gass' failed: {exc}", vi_name="TensormeterRTM2"
            ) from exc
        if result.error:
            raise CryoSoftCommunicationError(
                f"Tensormeter RTM2 'gass' protocol error: {result.error}",
                vi_name="TensormeterRTM2",
            )

    def _get_cached(self, param: str) -> float:
        """Return the last-known value of *param*, refreshing via `gass` if unknown.

        The RTM2 protocol has no query-without-side-effect: most settings
        are only ever pushed as echoes of a set command or in a `gass`
        burst, so a getter reads the locally cached last-known value
        instead of resending a value.
        """
        state = self._rtm.get_state()
        if param not in state:
            self._refresh_all_settings()
            state = self._rtm.get_state()
        if param not in state:
            raise CryoSoftCommunicationError(
                f"Tensormeter RTM2: no cached value for {param!r} even after 'gass'",
                vi_name="TensormeterRTM2",
            )
        return float(state[param])

    # ------------------------------------------------------------------
    # Switch matrix (§3.11)
    # ------------------------------------------------------------------

    @staticmethod
    def build_switch_state(
        drv_minus: list[int], drv_plus: list[int], sns_minus: list[int], sns_plus: list[int]
    ) -> int:
        """Pack four BNC-port lists into one RTM2 switch-state word.

        Ported from the vendor's ``rtm2.SwitState()`` bit layout (TCP
        Commands §3.11: 8 BNC ports × 4 functions = 32 bits) so the sim
        twin can reproduce it without depending on the ``rtm2`` package.

        Args:
            drv_minus: BNC port numbers (1-8) wired to DRV-.
            drv_plus: BNC port numbers wired to DRV+.
            sns_minus: BNC port numbers wired to SNS-.
            sns_plus: BNC port numbers wired to SNS+.

        Returns:
            The U32 switch-state value accepted by :meth:`set_switch_states`.

        Raises:
            ValueError: If any port number is not an integer in 1..8.
        """
        result = 0
        for ports, offset in (
            (drv_minus, 0), (drv_plus, 8), (sns_minus, 16), (sns_plus, 24)
        ):
            for port in ports:
                if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 8):
                    raise ValueError(f"Invalid switch port: {port!r}. Expected integer 1..8.")
                result |= 1 << (port - 1 + offset)
        return result

    def set_switch_states(self, *states: int) -> None:
        """Define the switch-matrix state sequence.

        Multiple states cycle synchronously with the averaging-time
        sampling period (§3.5), which is how a van-der-Pauw contact
        permutation sequence is driven — the RTM2's own Analysis Mode
        engine (see :meth:`set_analysis_mode`) does the cycling and tensor
        computation once armed this way.

        Args:
            *states: One or more switch-state words, e.g. from
                :meth:`build_switch_state`.
        """
        self._send_and_confirm("swit", *[int(s) for s in states])

    # ------------------------------------------------------------------
    # Source setpoints (§3.7) — rampable: optional ramp_time_s
    # ------------------------------------------------------------------

    def set_current_amplitude(self, amps: float, ramp_time_s: float | None = None) -> float:
        """Set the AC current amplitude setpoint, optionally ramped.

        Args:
            amps: Current amplitude in Amperes.
            ramp_time_s: Optional linear ramp time in seconds; immediate if
                omitted.

        Returns:
            The confirmed (possibly range-coerced) setpoint in Amperes.
        """
        args = (float(amps),) if ramp_time_s is None else (float(amps), float(ramp_time_s))
        return self._send_and_confirm("camp", *args)

    def set_current_dc(self, amps: float, ramp_time_s: float | None = None) -> float:
        """Set the DC current setpoint, optionally ramped.

        Args:
            amps: DC current in Amperes.
            ramp_time_s: Optional linear ramp time in seconds; immediate if
                omitted.

        Returns:
            The confirmed (possibly range-coerced) setpoint in Amperes.
        """
        args = (float(amps),) if ramp_time_s is None else (float(amps), float(ramp_time_s))
        return self._send_and_confirm("cudc", *args)

    def set_voltage_amplitude(self, volts: float, ramp_time_s: float | None = None) -> float:
        """Set the AC voltage amplitude setpoint, optionally ramped.

        Args:
            volts: Voltage amplitude in Volts.
            ramp_time_s: Optional linear ramp time in seconds; immediate if
                omitted.

        Returns:
            The confirmed (possibly range-coerced) setpoint in Volts.
        """
        args = (float(volts),) if ramp_time_s is None else (float(volts), float(ramp_time_s))
        return self._send_and_confirm("vamp", *args)

    def set_voltage_dc(self, volts: float, ramp_time_s: float | None = None) -> float:
        """Set the DC voltage setpoint, optionally ramped.

        Args:
            volts: DC voltage in Volts.
            ramp_time_s: Optional linear ramp time in seconds; immediate if
                omitted.

        Returns:
            The confirmed (possibly range-coerced) setpoint in Volts.
        """
        args = (float(volts),) if ramp_time_s is None else (float(volts), float(ramp_time_s))
        return self._send_and_confirm("vodc", *args)

    # ------------------------------------------------------------------
    # Protection (§3.8)
    # ------------------------------------------------------------------

    def set_voltage_protection(self, volts: float) -> float:
        """Set the output voltage protection limit in Volts."""
        return float(self._send_and_confirm("vpro", float(volts)))

    def get_voltage_protection(self) -> float:
        """Return the last-known output voltage protection limit in Volts."""
        return self._get_cached("vpro")

    def set_current_protection(self, amps: float) -> float:
        """Set the current protection limit in Amperes."""
        return float(self._send_and_confirm("ipro", float(amps)))

    def get_current_protection(self) -> float:
        """Return the last-known current protection limit in Amperes."""
        return self._get_cached("ipro")

    # ------------------------------------------------------------------
    # Measurement ranges (§3.9) — zero/negative = auto-range
    # ------------------------------------------------------------------

    def set_voltage_input_range(self, volts: float) -> float:
        """Set the voltage input range in Volts (<= 0 enables auto-range)."""
        return float(self._send_and_confirm("virg", float(volts)))

    def get_voltage_input_range(self) -> float:
        """Return the last-known voltage input range in Volts (negative = auto)."""
        return self._get_cached("virg")

    def set_voltage_output_range(self, volts: float) -> float:
        """Set the voltage output range in Volts (<= 0 enables auto-range)."""
        return float(self._send_and_confirm("vorg", float(volts)))

    def get_voltage_output_range(self) -> float:
        """Return the last-known voltage output range in Volts (negative = auto)."""
        return self._get_cached("vorg")

    def set_current_range(self, amps: float) -> float:
        """Set the current measurement range in Amperes (<= 0 enables auto-range)."""
        return float(self._send_and_confirm("crng", float(amps)))

    def get_current_range(self) -> float:
        """Return the last-known current range in Amperes (negative = auto)."""
        return self._get_cached("crng")

    def set_series_resistance(self, ohms: float) -> float:
        """Set the series resistance in Ohms (<= 0 enables auto-select)."""
        return float(self._send_and_confirm("sres", float(ohms)))

    def get_series_resistance(self) -> float:
        """Return the last-known series resistance in Ohms (negative = auto)."""
        return self._get_cached("sres")

    def increment_voltage_input_range(self) -> float:
        """Bump the voltage input range up one step; disables auto-range."""
        return float(self._send_and_confirm("viru", wait_for=_BUMP_CONFIRM_PARAM["viru"]))

    def decrement_voltage_input_range(self) -> float:
        """Bump the voltage input range down one step; disables auto-range."""
        return float(self._send_and_confirm("vird", wait_for=_BUMP_CONFIRM_PARAM["vird"]))

    def increment_voltage_output_range(self) -> float:
        """Bump the voltage output range up one step; disables auto-range."""
        return float(self._send_and_confirm("voru", wait_for=_BUMP_CONFIRM_PARAM["voru"]))

    def decrement_voltage_output_range(self) -> float:
        """Bump the voltage output range down one step; disables auto-range."""
        return float(self._send_and_confirm("vord", wait_for=_BUMP_CONFIRM_PARAM["vord"]))

    def increment_current_range(self) -> float:
        """Bump the current range up one step; disables auto-range."""
        return float(self._send_and_confirm("crup", wait_for=_BUMP_CONFIRM_PARAM["crup"]))

    def decrement_current_range(self) -> float:
        """Bump the current range down one step; disables auto-range."""
        return float(self._send_and_confirm("crdn", wait_for=_BUMP_CONFIRM_PARAM["crdn"]))

    def increment_series_resistance(self) -> float:
        """Bump the series resistance up one step; disables auto-select."""
        return float(self._send_and_confirm("srup", wait_for=_BUMP_CONFIRM_PARAM["srup"]))

    def decrement_series_resistance(self) -> float:
        """Bump the series resistance down one step; disables auto-select."""
        return float(self._send_and_confirm("srdn", wait_for=_BUMP_CONFIRM_PARAM["srdn"]))

    # ------------------------------------------------------------------
    # Timing / lock-in (§3.5, 3.6, 3.19)
    # ------------------------------------------------------------------

    def set_averaging_time(self, seconds: float) -> float:
        """Set the averaging/sampling period in seconds."""
        return float(self._send_and_confirm("avgt", float(seconds)))

    def get_averaging_time(self) -> float:
        """Return the last-known averaging/sampling period in seconds."""
        return self._get_cached("avgt")

    def set_lockin_frequency(self, hz: float) -> float:
        """Set the lock-in frequency in Hertz."""
        return float(self._send_and_confirm("lfrq", float(hz)))

    def get_lockin_frequency(self) -> float:
        """Return the last-known lock-in frequency in Hertz."""
        return self._get_cached("lfrq")

    def set_phase_shift(self, cycles: float) -> float:
        """Set the DRV-vs-reference phase shift, in fractions of a cycle."""
        return float(self._send_and_confirm("phsh", float(cycles)))

    def get_phase_shift(self) -> float:
        """Return the last-known phase shift, in fractions of a cycle."""
        return self._get_cached("phsh")

    def set_reference_mux(self, mode: int) -> int:
        """Set the reference multiplexer input.

        Args:
            mode: 0=Off, 1-8=stay at BNC port 1-8, 9=follow SNS+,
                10=follow SNS-, 13=follow DRV+, 14=follow DRV-.
        """
        return int(self._send_and_confirm("refm", int(mode)))

    def set_phase_lock_source(self, mode: int) -> int:
        """Set the phase-lock source.

        Args:
            mode: 0=internal clock, 1=reference multiplexer.
        """
        return int(self._send_and_confirm("phlk", int(mode)))

    # ------------------------------------------------------------------
    # Analysis, control & waveform modes (§3.12, 3.13, 3.18)
    # ------------------------------------------------------------------

    def set_analysis_mode(self, mode: int) -> int:
        """Set the requested Analysis Mode.

        Args:
            mode: 0=Auto, 1=Kelvin, 2=Zero-Offset Hall, 3=Van-der-Pauw,
                4=Ratiometric, 5=Differential.
        """
        return int(self._send_and_confirm("amod", int(mode)))

    def get_detected_analysis_mode(self) -> int:
        """Return the firmware-detected actual Analysis Mode.

        Only meaningful once the device has reported it (e.g. after a
        mode/switch-task change or a `gass`) — see the vendor doc's note on
        "Requested" vs "Actual" Analysis Mode (§3.12): while requested mode
        is "Auto", the actual mode tracks the current switch task and is
        not always automatically pushed.

        Reads the "mod?" cache key: live commissioning (2026-07-23) found
        the real device pushes this update tagged literally "mod?" (the
        vendor doc's own §3.12 command name), not "modq" — see the
        ``_COMMANDS`` patch note in ``__init__``.
        """
        return int(self._get_cached("mod?"))

    def set_multisample_mode(self, mode: int) -> int:
        """Set the Multisample Mode.

        Args:
            mode: 0=Off, 1=Interleave, 2=Differential, 3=Ratiometric.
        """
        return int(self._send_and_confirm("mult", int(mode)))

    def set_control_mode(self, mode: int) -> int:
        """Set the DRV output Control Mode.

        Args:
            mode: 0=Direct Voltage Output, 1=Feedback Voltage/Current Output.
        """
        return int(self._send_and_confirm("cmod", int(mode)))

    def set_waveform_mode(self, mode: int) -> int:
        """Set the DRV output Waveform Mode.

        Args:
            mode: 0=Continuous Sine Wave, 1=Pulse Train, 2=Arbitrary Waveform.
        """
        return int(self._send_and_confirm("wfmd", int(mode)))

    def set_sns_preamp_mode(self, mode: int) -> int:
        """Set the SNS preamplifier type.

        Args:
            mode: 0=BJT Preamp, 1=FET Preamp.
        """
        return int(self._send_and_confirm("snsa", int(mode)))

    def set_coax_shell_mode(self, mode: int) -> int:
        """Set the BNC coax shell mode.

        Args:
            mode: 0=Ground Shells, 1=Active Guard (all active ports),
                2=Guard exclusive SNS ports only, 3=Guard exclusive DRV
                ports only.
        """
        return int(self._send_and_confirm("coax", int(mode)))

    # ------------------------------------------------------------------
    # Pulse / arbitrary waveform (§3.14) — included for driver completeness;
    # not used by TensormeterRTM2MeasurementVI.
    # ------------------------------------------------------------------

    def set_pulse_train(
        self,
        period_s: float,
        on_time_s: float,
        on_v: float,
        off_v: float,
        n_periods: float,
        start_phase: float,
    ) -> None:
        """Define a Pulse Train waveform (only used while Waveform Mode = Pulse Train).

        Args:
            period_s: Period time in seconds.
            on_time_s: "On" time in seconds.
            on_v: "On" voltage in Volts.
            off_v: "Off" voltage in Volts.
            n_periods: Duration, in number of periods.
            start_phase: Starting phase, as a fraction of one period.
        """
        self._send_and_confirm(
            "puar", float(period_s), float(on_time_s), float(on_v), float(off_v),
            float(n_periods), float(start_phase),
        )

    def set_arbitrary_waveform(self, points: list[tuple[float, float]]) -> None:
        """Define an Arbitrary Waveform (only used while Waveform Mode = Arbitrary).

        Args:
            points: List of (point_voltage_V, hold_time_s) pairs, at least one.
        """
        flat = [float(v) for pair in points for v in pair]
        self._send_and_confirm("puar", *flat)

    # ------------------------------------------------------------------
    # Software triggers (§3.15)
    # ------------------------------------------------------------------

    def trigger_demodulation(self) -> None:
        """Begin a new demodulation window immediately."""
        self._send_and_confirm("trig")

    def trigger_pulse(self) -> None:
        """Begin pulse train / arbitrary waveform output immediately."""
        self._send_and_confirm("puls")

    # ------------------------------------------------------------------
    # Measurement count (§3.16)
    # ------------------------------------------------------------------

    def set_measurement_count(self, n: int) -> int:
        """Set the device-side data-buffer measurement count.

        Args:
            n: Number of points still written before storage suspends;
                -1 means infinite/continuous (the device's default).
        """
        return int(self._send_and_confirm("meas", int(n)))

    # ------------------------------------------------------------------
    # Digital I/O (§3.17)
    # ------------------------------------------------------------------

    def set_digital_io(self, port: int, mode: int, voltage: float = 0.0) -> None:
        """Configure Digital I/O port 0 or 1.

        Args:
            port: 0 or 1.
            mode: DIO mode (0-4 input modes, 128-132 output modes — see
                the vendor doc §3.17).
            voltage: Analog output voltage in Volts, only meaningful for
                the DAC output mode (131).

        Raises:
            ValueError: If ``port`` is not 0 or 1.
        """
        if port == 0:
            cmd = "dio0"
        elif port == 1:
            cmd = "dio1"
        else:
            raise ValueError(f"port must be 0 or 1, got {port!r}")
        self._send_and_confirm(cmd, int(mode), float(voltage))

    # ------------------------------------------------------------------
    # Data channels & acquisition (§3.1-3.4)
    # ------------------------------------------------------------------

    def clear_data(self) -> None:
        """Delete all device-side buffered data rows."""
        self._send_and_confirm("cldt")

    def select_data_channels(self, *column_indices: int) -> None:
        """Select which data columns are sent by future `newd`/`alld` reads.

        Note:
            Once called, :meth:`read_new_data`/:meth:`read_all_data`'s
            fixed column-name decoding no longer applies (it assumes the
            default, unfiltered, ascending-order channel set). This method
            is provided for driver completeness; the shipped measurement
            VI never calls it.

        Args:
            *column_indices: Data-array column indices (see the vendor
                doc §3.1) to include, in the desired order.
        """
        self._send_and_confirm("selc", *[int(i) for i in column_indices])

    def read_new_data(self, timeout: float | None = None) -> list[dict[str, float]]:
        """Return data rows acquired since the last `newd`/`alld` call.

        Args:
            timeout: Round-trip timeout in seconds; defaults to the
                connection's configured timeout.

        Returns:
            One dict per row, keyed by :data:`_DATA_COLUMNS` (assumes the
            default channel selection — see :meth:`select_data_channels`).
        """
        return self._read_data("newd", timeout)

    def read_all_data(self, timeout: float | None = None) -> list[dict[str, float]]:
        """Return the whole device-side data buffer (up to 8192 rows).

        Args:
            timeout: Round-trip timeout in seconds; defaults to the
                connection's configured timeout.

        Returns:
            One dict per row, keyed by :data:`_DATA_COLUMNS` (assumes the
            default channel selection — see :meth:`select_data_channels`).
        """
        return self._read_data("alld", timeout)

    def _read_data(self, cmd: str, timeout: float | None) -> list[dict[str, float]]:
        try:
            result = self._rtm.read_until("data", send=cmd, timeout=timeout or self._timeout_s)
        except (ConnectionError, ValueError) as exc:
            raise CryoSoftCommunicationError(
                f"Tensormeter RTM2 {cmd!r} failed: {exc}", vi_name="TensormeterRTM2"
            ) from exc
        if result.error:
            raise CryoSoftCommunicationError(
                f"Tensormeter RTM2 {cmd!r} protocol error: {result.error}",
                vi_name="TensormeterRTM2",
            )
        return [dict(zip(_DATA_COLUMNS, (float(v) for v in row))) for row in result.data.tolist()]

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def get_idn(self) -> str:
        """Return a synthesized identity string.

        RTM2 has no `*IDN?`-equivalent command. This round-trips a
        harmless `gass` query to confirm the instrument is actually
        reachable, then returns a fixed identity string naming the host
        and port — the closest equivalent this protocol offers.

        Raises:
            CryoSoftCommunicationError: If the instrument does not respond.
        """
        self._refresh_all_settings()
        return f"Tensormeter RTM2 @ {self._host}:{self._port}"
