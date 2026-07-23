# ---
# description: |
#   Simulated driver for the Tensormeter RTM2 resistance-tensor analyzer.
#   Models source/protection/range setpoints, switch-matrix state, and the
#   onboard Analysis Mode engine's tensor output (Res A / Res B) via a
#   configurable "true" sheet resistance and Hall resistance plus Gaussian
#   noise. No `rtm2` package dependency — pure Python, fully independent
#   of the real driver module (deliberately does not import
#   cryosoft.drivers.tensormeter_rtm2, so it constructs even if the vendor
#   package is not installed).
# entry_point: Not run directly; imported by the Virtual Instruments layer.
# dependencies: []
# input: |
#   Instantiated with a resource string (ignored). Public methods mirror
#   TensormeterRTM2 exactly (conformance-checked API parity).
# process: |
#   Stores every setter value; read_new_data()/read_all_data() synthesize
#   data rows from the stored source current and the configurable
#   _true_sheet_resistance_ohm / _true_hall_resistance_ohm test hooks (plus
#   _noise_ohm Gaussian noise) into res_a_dc_ohm / res_b_dc_ohm — good
#   enough to exercise a measurement VI's control flow and averaging logic,
#   without claiming to model the real lock-in/demodulation physics.
# output: |
#   Plain float/int/str/None from setters/getters; read_new_data()/
#   read_all_data() return list[dict[str, float]] with the same column
#   names as the real driver's fixed layout (vendor doc §3.1).
# last_updated: 2026-07-23
# ---

"""Simulated Tensormeter RTM2 resistance-tensor analyzer driver."""

from __future__ import annotations

import random

from cryosoft.core.exceptions import CryoSoftCommunicationError

# Duplicated verbatim from tensormeter_rtm2.py's _DATA_COLUMNS — kept
# independent (not imported) so this sim driver never depends on the real
# driver module (and, transitively, on the `rtm2` package being installed).
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


class SimTensormeterRTM2:
    """Simulated Tensormeter RTM2 resistance-tensor analyzer.

    This driver satisfies the three-rule driver contract:
    1. It is a Python class.
    2. __init__ accepts a single resource string (ignored for simulation).
    3. It is importable via cryosoft.drivers.sim_tensormeter_rtm2.
    """

    def __init__(self, resource_string: str) -> None:
        """Initialise the simulated RTM2.

        Args:
            resource_string: "host" or "host:port". Ignored.
        """
        _ = resource_string  # Explicitly ignored per driver contract

        # Source setpoints
        self._current_amplitude_A: float = 0.0
        self._current_dc_A: float = 0.0
        self._voltage_amplitude_V: float = 0.0
        self._voltage_dc_V: float = 0.0
        self._active_current_A: float = 0.0  # whichever source knob was set last

        # Protection
        self._voltage_protection_V: float = 10.0
        self._current_protection_A: float = 0.01

        # Ranges (negative = auto-range, mirrors the real protocol)
        self._voltage_input_range_V: float = -1.0
        self._voltage_output_range_V: float = -1.0
        self._current_range_A: float = -1.0
        self._series_resistance_ohm: float = -1.0

        # Timing / lock-in
        self._averaging_time_s: float = 0.1
        self._lockin_frequency_Hz: float = 1.0
        self._phase_shift_cycles: float = 0.0
        self._reference_mux: int = 0
        self._phase_lock_source: int = 0

        # Modes
        self._analysis_mode: int = 0
        self._detected_analysis_mode: int = 0
        self._multisample_mode: int = 0
        self._control_mode: int = 0
        self._waveform_mode: int = 0
        self._sns_preamp_mode: int = 0
        self._coax_shell_mode: int = 0

        # Switch matrix
        self._switch_states: list[int] = []

        # Measurement count / digital I/O
        self._measurement_count: int = -1
        self._dio_voltage: dict[int, float] = {0: 0.0, 1: 0.0}

        # Data buffer
        self._data_buffer: list[dict[str, float]] = []
        self._sent_count: int = 0
        self._time_s: float = 0.0

        # Physics test hooks
        self._true_sheet_resistance_ohm: float = 100.0
        self._true_hall_resistance_ohm: float = 0.0
        self._noise_ohm: float = 1e-3

        # Test control flag
        self._simulate_error: bool = False

    # ------------------------------------------------------------------
    # Switch matrix (§3.11)
    # ------------------------------------------------------------------

    @staticmethod
    def build_switch_state(
        drv_minus: list[int], drv_plus: list[int], sns_minus: list[int], sns_plus: list[int]
    ) -> int:
        """Pack four BNC-port lists into one RTM2 switch-state word.

        Identical bit layout to the real driver's ``build_switch_state()``
        (duplicated, not imported, per this file's independence policy —
        see the module docstring).

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
        """Store the switch-matrix state sequence."""
        self._check_error()
        self._switch_states = [int(s) for s in states]

    # ------------------------------------------------------------------
    # Source setpoints (§3.7)
    # ------------------------------------------------------------------

    def set_current_amplitude(self, amps: float, ramp_time_s: float | None = None) -> float:
        """Set the AC current amplitude setpoint (ramp_time_s stored but unused in sim)."""
        self._check_error()
        self._current_amplitude_A = float(amps)
        self._active_current_A = self._current_amplitude_A
        return self._current_amplitude_A

    def set_current_dc(self, amps: float, ramp_time_s: float | None = None) -> float:
        """Set the DC current setpoint (ramp_time_s stored but unused in sim)."""
        self._check_error()
        self._current_dc_A = float(amps)
        self._active_current_A = self._current_dc_A
        return self._current_dc_A

    def set_voltage_amplitude(self, volts: float, ramp_time_s: float | None = None) -> float:
        """Set the AC voltage amplitude setpoint (ramp_time_s stored but unused in sim)."""
        self._check_error()
        self._voltage_amplitude_V = float(volts)
        return self._voltage_amplitude_V

    def set_voltage_dc(self, volts: float, ramp_time_s: float | None = None) -> float:
        """Set the DC voltage setpoint (ramp_time_s stored but unused in sim)."""
        self._check_error()
        self._voltage_dc_V = float(volts)
        return self._voltage_dc_V

    # ------------------------------------------------------------------
    # Protection (§3.8)
    # ------------------------------------------------------------------

    def set_voltage_protection(self, volts: float) -> float:
        """Set the output voltage protection limit in Volts."""
        self._check_error()
        self._voltage_protection_V = float(volts)
        return self._voltage_protection_V

    def get_voltage_protection(self) -> float:
        """Return the output voltage protection limit in Volts."""
        self._check_error()
        return self._voltage_protection_V

    def set_current_protection(self, amps: float) -> float:
        """Set the current protection limit in Amperes."""
        self._check_error()
        self._current_protection_A = float(amps)
        return self._current_protection_A

    def get_current_protection(self) -> float:
        """Return the current protection limit in Amperes."""
        self._check_error()
        return self._current_protection_A

    # ------------------------------------------------------------------
    # Measurement ranges (§3.9, 3.10)
    # ------------------------------------------------------------------

    def set_voltage_input_range(self, volts: float) -> float:
        """Set the voltage input range in Volts (<= 0 enables auto-range)."""
        self._check_error()
        self._voltage_input_range_V = float(volts)
        return self._voltage_input_range_V

    def get_voltage_input_range(self) -> float:
        """Return the voltage input range in Volts (negative = auto)."""
        self._check_error()
        return self._voltage_input_range_V

    def set_voltage_output_range(self, volts: float) -> float:
        """Set the voltage output range in Volts (<= 0 enables auto-range)."""
        self._check_error()
        self._voltage_output_range_V = float(volts)
        return self._voltage_output_range_V

    def get_voltage_output_range(self) -> float:
        """Return the voltage output range in Volts (negative = auto)."""
        self._check_error()
        return self._voltage_output_range_V

    def set_current_range(self, amps: float) -> float:
        """Set the current measurement range in Amperes (<= 0 enables auto-range)."""
        self._check_error()
        self._current_range_A = float(amps)
        return self._current_range_A

    def get_current_range(self) -> float:
        """Return the current range in Amperes (negative = auto)."""
        self._check_error()
        return self._current_range_A

    def set_series_resistance(self, ohms: float) -> float:
        """Set the series resistance in Ohms (<= 0 enables auto-select)."""
        self._check_error()
        self._series_resistance_ohm = float(ohms)
        return self._series_resistance_ohm

    def get_series_resistance(self) -> float:
        """Return the series resistance in Ohms (negative = auto)."""
        self._check_error()
        return self._series_resistance_ohm

    def increment_voltage_input_range(self) -> float:
        """Bump the voltage input range up one step; disables auto-range."""
        self._check_error()
        self._voltage_input_range_V = abs(self._voltage_input_range_V) * 2 or 1.0
        return self._voltage_input_range_V

    def decrement_voltage_input_range(self) -> float:
        """Bump the voltage input range down one step; disables auto-range."""
        self._check_error()
        self._voltage_input_range_V = abs(self._voltage_input_range_V) / 2 or 0.5
        return self._voltage_input_range_V

    def increment_voltage_output_range(self) -> float:
        """Bump the voltage output range up one step; disables auto-range."""
        self._check_error()
        self._voltage_output_range_V = abs(self._voltage_output_range_V) * 2 or 1.0
        return self._voltage_output_range_V

    def decrement_voltage_output_range(self) -> float:
        """Bump the voltage output range down one step; disables auto-range."""
        self._check_error()
        self._voltage_output_range_V = abs(self._voltage_output_range_V) / 2 or 0.5
        return self._voltage_output_range_V

    def increment_current_range(self) -> float:
        """Bump the current range up one step; disables auto-range."""
        self._check_error()
        self._current_range_A = abs(self._current_range_A) * 2 or 1e-3
        return self._current_range_A

    def decrement_current_range(self) -> float:
        """Bump the current range down one step; disables auto-range."""
        self._check_error()
        self._current_range_A = abs(self._current_range_A) / 2 or 5e-4
        return self._current_range_A

    def increment_series_resistance(self) -> float:
        """Bump the series resistance up one step; disables auto-select."""
        self._check_error()
        self._series_resistance_ohm = abs(self._series_resistance_ohm) * 2 or 1e3
        return self._series_resistance_ohm

    def decrement_series_resistance(self) -> float:
        """Bump the series resistance down one step; disables auto-select."""
        self._check_error()
        self._series_resistance_ohm = abs(self._series_resistance_ohm) / 2 or 5e2
        return self._series_resistance_ohm

    # ------------------------------------------------------------------
    # Timing / lock-in (§3.5, 3.6, 3.19)
    # ------------------------------------------------------------------

    def set_averaging_time(self, seconds: float) -> float:
        """Set the averaging/sampling period in seconds."""
        self._check_error()
        self._averaging_time_s = float(seconds)
        return self._averaging_time_s

    def get_averaging_time(self) -> float:
        """Return the averaging/sampling period in seconds."""
        self._check_error()
        return self._averaging_time_s

    def set_lockin_frequency(self, hz: float) -> float:
        """Set the lock-in frequency in Hertz."""
        self._check_error()
        self._lockin_frequency_Hz = float(hz)
        return self._lockin_frequency_Hz

    def get_lockin_frequency(self) -> float:
        """Return the lock-in frequency in Hertz."""
        self._check_error()
        return self._lockin_frequency_Hz

    def set_phase_shift(self, cycles: float) -> float:
        """Set the DRV-vs-reference phase shift, in fractions of a cycle."""
        self._check_error()
        self._phase_shift_cycles = float(cycles)
        return self._phase_shift_cycles

    def get_phase_shift(self) -> float:
        """Return the phase shift, in fractions of a cycle."""
        self._check_error()
        return self._phase_shift_cycles

    def set_reference_mux(self, mode: int) -> int:
        """Set the reference multiplexer input."""
        self._check_error()
        self._reference_mux = int(mode)
        return self._reference_mux

    def set_phase_lock_source(self, mode: int) -> int:
        """Set the phase-lock source."""
        self._check_error()
        self._phase_lock_source = int(mode)
        return self._phase_lock_source

    # ------------------------------------------------------------------
    # Analysis, control & waveform modes (§3.12, 3.13, 3.18)
    # ------------------------------------------------------------------

    def set_analysis_mode(self, mode: int) -> int:
        """Set the requested Analysis Mode."""
        self._check_error()
        self._analysis_mode = int(mode)
        self._detected_analysis_mode = self._analysis_mode
        return self._analysis_mode

    def get_detected_analysis_mode(self) -> int:
        """Return the firmware-detected actual Analysis Mode."""
        self._check_error()
        return self._detected_analysis_mode

    def set_multisample_mode(self, mode: int) -> int:
        """Set the Multisample Mode."""
        self._check_error()
        self._multisample_mode = int(mode)
        return self._multisample_mode

    def set_control_mode(self, mode: int) -> int:
        """Set the DRV output Control Mode."""
        self._check_error()
        self._control_mode = int(mode)
        return self._control_mode

    def set_waveform_mode(self, mode: int) -> int:
        """Set the DRV output Waveform Mode."""
        self._check_error()
        self._waveform_mode = int(mode)
        return self._waveform_mode

    def set_sns_preamp_mode(self, mode: int) -> int:
        """Set the SNS preamplifier type."""
        self._check_error()
        self._sns_preamp_mode = int(mode)
        return self._sns_preamp_mode

    def set_coax_shell_mode(self, mode: int) -> int:
        """Set the BNC coax shell mode."""
        self._check_error()
        self._coax_shell_mode = int(mode)
        return self._coax_shell_mode

    # ------------------------------------------------------------------
    # Pulse / arbitrary waveform (§3.14)
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
        """Store a Pulse Train waveform definition (not simulated further)."""
        self._check_error()

    def set_arbitrary_waveform(self, points: list[tuple[float, float]]) -> None:
        """Store an Arbitrary Waveform definition (not simulated further)."""
        self._check_error()

    # ------------------------------------------------------------------
    # Software triggers (§3.15)
    # ------------------------------------------------------------------

    def trigger_demodulation(self) -> None:
        """Begin a new demodulation window: append one synthetic data row."""
        self._check_error()
        self._data_buffer.append(self._make_row())
        self._time_s += self._averaging_time_s

    def trigger_pulse(self) -> None:
        """Begin pulse train / arbitrary waveform output (no-op in sim)."""
        self._check_error()

    # ------------------------------------------------------------------
    # Measurement count (§3.16)
    # ------------------------------------------------------------------

    def set_measurement_count(self, n: int) -> int:
        """Set the device-side data-buffer measurement count."""
        self._check_error()
        self._measurement_count = int(n)
        return self._measurement_count

    # ------------------------------------------------------------------
    # Digital I/O (§3.17)
    # ------------------------------------------------------------------

    def set_digital_io(self, port: int, mode: int, voltage: float = 0.0) -> None:
        """Configure Digital I/O port 0 or 1.

        Raises:
            ValueError: If ``port`` is not 0 or 1.
        """
        self._check_error()
        if port not in (0, 1):
            raise ValueError(f"port must be 0 or 1, got {port!r}")
        self._dio_voltage[port] = float(voltage)

    # ------------------------------------------------------------------
    # Data channels & acquisition (§3.1-3.4)
    # ------------------------------------------------------------------

    def clear_data(self) -> None:
        """Delete all buffered data rows."""
        self._check_error()
        self._data_buffer = []
        self._sent_count = 0

    def select_data_channels(self, *column_indices: int) -> None:
        """Stored for parity; the sim always returns the full default column set."""
        self._check_error()

    def read_new_data(self, timeout: float | None = None) -> list[dict[str, float]]:
        """Return rows appended since the last read_new_data() call."""
        self._check_error()
        rows = self._data_buffer[self._sent_count :]
        self._sent_count = len(self._data_buffer)
        return rows

    def read_all_data(self, timeout: float | None = None) -> list[dict[str, float]]:
        """Return every buffered row."""
        self._check_error()
        return list(self._data_buffer)

    def _make_row(self) -> dict[str, float]:
        """Synthesize one data row from the stored setpoints and true-resistance hooks."""
        noise = lambda: random.gauss(0.0, self._noise_ohm)  # noqa: E731
        res_a = self._true_sheet_resistance_ohm + noise()
        res_b = self._true_hall_resistance_ohm + noise()
        current = self._active_current_A
        row = dict.fromkeys(_DATA_COLUMNS, 0.0)
        row.update(
            {
                "time_s": self._time_s,
                "current_dc_A": current,
                "output_voltage_dc_V": current * self._true_sheet_resistance_ohm,
                "resistance_2w_dc_ohm": res_a,
                "current_ampl_A": self._current_amplitude_A,
                "res_a_dc_ohm": res_a,
                "res_b_dc_ohm": res_b,
                "switch_status": float(self._switch_states[-1]) if self._switch_states else 0.0,
                "lockin_frequency_Hz": self._lockin_frequency_Hz,
                "voltage_dc_setpoint_V": self._voltage_dc_V,
                "current_dc_setpoint_A": self._current_dc_A,
                "current_dc_setpoint_A_2": self._current_dc_A,
                "current_ampl_setpoint_A": self._current_amplitude_A,
                "voltage_protection_V": self._voltage_protection_V,
                "current_protection_A": self._current_protection_A,
                "voltage_input_range_V": self._voltage_input_range_V,
                "voltage_output_range_V": self._voltage_output_range_V,
                "current_range_A": self._current_range_A,
                "series_resistance_ohm": self._series_resistance_ohm,
                "sampling_duration_s": self._averaging_time_s,
                "analysis_multisample_mode": float(self._analysis_mode),
                "dio0_V": self._dio_voltage[0],
                "dio1_V": self._dio_voltage[1],
            }
        )
        return row

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    def get_idn(self) -> str:
        """Return a simulated identity string."""
        self._check_error()
        return "Tensormeter RTM2 @ SIM"

    def _check_error(self) -> None:
        """Raise CryoSoftCommunicationError if error simulation is active."""
        if self._simulate_error:
            raise CryoSoftCommunicationError(
                "Simulated communication error on Tensormeter RTM2",
                vi_name="SimTensormeterRTM2",
            )
