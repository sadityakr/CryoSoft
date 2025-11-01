"""
Mock IPS120 Magnet Power Supply
================================

Mock implementation of the Oxford Instruments IPS120-10 Magnet Power Supply.

This driver simulates the behavior of a real IPS120-10 including:
- Field and current control
- Switch heater management
- Persistent and demand modes
- Activity control and sweep status
- Safety interlocks

Example:
    ips = MockIPS120()
    ips.enable_control()
    ips.set_field(0.5, sweep_rate=0.1)
    print(f"Field: {ips.field}T")
"""

from pymeasure.adapters import FakeAdapter
from pymeasure.instruments import Instrument
from ..base.magnet_base import MagnetBase
import logging
import time
import threading

log = logging.getLogger(__name__)


class MagnetError(ValueError):
    """Exception for magnet-related errors."""
    pass


class SwitchHeaterError(ValueError):
    """Exception for switch heater errors."""
    pass


class MockIPS120(Instrument, MagnetBase):
    """Mock Oxford IPS120-10 Magnet Power Supply.

    Simulates a real IPS120-10 with realistic behavior including:
    - Magnetic field ramping with simulated delays
    - Switch heater control with timing
    - Persistent mode operation
    - Safety interlocks and status checks
    """

    # Switch heater delay times (seconds)
    _SWITCH_HEATER_HEATING_DELAY = 0.5  # Reduced for faster testing
    _SWITCH_HEATER_COOLING_DELAY = 0.5

    def __init__(self, adapter=None, name="Mock IPS120", field_range=(-7, 7), **kwargs):
        """Initialize mock IPS120.

        Args:
            adapter: Communication adapter (uses FakeAdapter if None)
            name: Device name
            field_range: Tuple of (min_field, max_field) in Tesla
            **kwargs: Additional arguments passed to Instrument
        """
        if adapter is None:
            adapter = FakeAdapter()

        super().__init__(adapter, name, includeSCPI=False, **kwargs)

        # Field range
        if isinstance(field_range, (int, float)):
            self._field_range = [-abs(field_range), abs(field_range)]
        else:
            self._field_range = list(field_range)

        # Internal state
        self._field_setpoint = 0.0
        self._current_setpoint = 0.0
        self._field_measured = 0.0
        self._current_measured = 0.0
        self._persistent_field = 0.0
        self._sweep_rate = 0.1  # T/min
        self._control_mode = "RU"  # Remote & Unlocked
        self._activity = "hold"
        self._sweep_status = "at rest"
        self._switch_heater_enabled = False

        # Simulation parameters
        self._lock = threading.Lock()
        self._sweeping = False
        self._sweep_thread = None

        log.info(f"{name} initialized (range: {self._field_range[0]} to "
                f"{self._field_range[1]}T)")

    # ==================== Field Measurements ====================

    @property
    def field(self):
        """Current magnetic field in Tesla.

        Returns persistent_field if in persistent mode, otherwise measured field.
        """
        if not self._switch_heater_enabled and abs(self._persistent_field) > 0.001:
            # In persistent mode - return the persistent field
            return self._persistent_field
        return self._field_measured

    @property
    def demand_field(self):
        """Demand field (from power supply) in Tesla."""
        return self._field_measured  # In mock, measured = demand

    @property
    def persistent_field(self):
        """Persistent field (in magnet coil) in Tesla."""
        return self._persistent_field

    # ==================== Current Measurements ====================

    @property
    def current_measured(self):
        """Measured current in the power supply (A)."""
        # Approximate conversion: 1T = 14.3A for typical 7T magnet
        return self._field_measured * 14.3

    @property
    def demand_current(self):
        """Demand current in the power supply (A)."""
        return self._field_setpoint * 14.3

    # ==================== Setpoints ====================

    @property
    def field_setpoint(self):
        """Field setpoint in Tesla."""
        return self._field_setpoint

    @field_setpoint.setter
    def field_setpoint(self, value):
        """Set field setpoint in Tesla."""
        # Clamp to field range
        value = max(self._field_range[0], min(self._field_range[1], value))
        self._field_setpoint = value
        log.debug(f"Field setpoint: {value:.3f}T")

    @property
    def current_setpoint(self):
        """Current setpoint in Amps."""
        return self._current_setpoint

    @current_setpoint.setter
    def current_setpoint(self, value):
        """Set current setpoint in Amps."""
        self._current_setpoint = value

    # ==================== Sweep Rate ====================

    @property
    def sweep_rate(self):
        """Sweep rate in T/min."""
        return self._sweep_rate

    @sweep_rate.setter
    def sweep_rate(self, value):
        """Set sweep rate in T/min."""
        value = max(0.0, min(1.0, value))  # Typical range 0-1 T/min
        self._sweep_rate = value
        log.debug(f"Sweep rate: {value:.3f}T/min")

    # ==================== Control Mode ====================

    @property
    def control_mode(self):
        """Control mode (LL/RL/LU/RU)."""
        return self._control_mode

    @control_mode.setter
    def control_mode(self, value):
        """Set control mode."""
        valid_modes = ["LL", "RL", "LU", "RU"]
        if value not in valid_modes:
            raise ValueError(f"Invalid control mode: {value}. Must be one of {valid_modes}")
        self._control_mode = value
        log.debug(f"Control mode: {value}")

    # ==================== Activity ====================

    @property
    def activity(self):
        """Activity status."""
        return self._activity

    @activity.setter
    def activity(self, value):
        """Set activity mode."""
        valid_activities = ["hold", "to setpoint", "to zero", "clamp"]
        if value not in valid_activities:
            raise ValueError(f"Invalid activity: {value}")
        self._activity = value
        log.debug(f"Activity: {value}")

    # ==================== Sweep Status ====================

    @property
    def sweep_status(self):
        """Sweep status string."""
        return self._sweep_status

    # ==================== Switch Heater ====================

    @property
    def switch_heater_enabled(self):
        """Switch heater status (True=on/open, False=off/closed)."""
        return self._switch_heater_enabled

    @switch_heater_enabled.setter
    def switch_heater_enabled(self, value):
        """Enable/disable switch heater."""
        if value == self._switch_heater_enabled:
            return  # No change

        if value:
            # Turning on (opening switch)
            log.info(f"Switch heater ON (warming {self._SWITCH_HEATER_HEATING_DELAY}s)")
            time.sleep(self._SWITCH_HEATER_HEATING_DELAY)
            self._switch_heater_enabled = True
            log.debug("Switch heater fully on")
        else:
            # Turning off (closing switch)
            log.info(f"Switch heater OFF (cooling {self._SWITCH_HEATER_COOLING_DELAY}s)")

            # Store current field as persistent field
            self._persistent_field = self._field_measured

            time.sleep(self._SWITCH_HEATER_COOLING_DELAY)
            self._switch_heater_enabled = False
            log.debug(f"Switch heater fully off (persistent field: {self._persistent_field:.3f}T)")

    # ==================== Version ====================

    @property
    def version(self):
        """Device version string."""
        return "IPS120 Mock v1.0"

    # ==================== High-Level Control Methods ====================

    def enable_control(self):
        """Enable active control of magnet power supply."""
        log.info("Enabling control")
        self._control_mode = "RU"

        # Turn off clamping if active
        if self._activity == "clamp":
            self._activity = "hold"
            log.debug("Clamp released")

        # Turn on switch heater if at zero field
        if abs(self.field) < 0.01:
            log.debug("Enabling switch heater (at zero field)")
            self.switch_heater_enabled = True

        log.info("Control enabled")

    def disable_control(self):
        """Disable active control (only at 0T)."""
        log.info("Disabling control")

        if abs(self.field) > 0.01:
            raise MagnetError(
                f"Cannot disable control: field not at 0T (current: {self.field:.3f}T)"
            )

        self.switch_heater_enabled = False
        self._activity = "clamp"
        self._control_mode = "LU"

        log.info("Control disabled")

    def set_field(self, field, sweep_rate=None, persistent_mode_control=True):
        """Change magnetic field to specified value.

        Args:
            field: Target field in Tesla
            sweep_rate: Sweep rate in T/min (optional)
            persistent_mode_control: Allow persistent mode switching

        Raises:
            MagnetError: If in persistent mode and control not allowed
        """
        log.info(f"Setting field to {field:.3f}T (rate={sweep_rate}T/min)")

        # Check if field needs changing
        if abs(self.field - field) < 0.001:
            log.debug("Already at target field")
            return

        # Handle persistent mode
        if not self.switch_heater_enabled:
            log.debug("Magnet in persistent mode")
            if persistent_mode_control:
                self._disable_persistent_mode()
            else:
                raise MagnetError(
                    "Magnet in persistent mode but persistent_mode_control=False"
                )

        # Set sweep rate if provided
        if sweep_rate is not None:
            self.sweep_rate = sweep_rate

        # Start sweep
        if abs(field) < 0.001:
            log.debug("Going to zero")
            self._activity = "to zero"
            self._start_sweep(0.0)
        else:
            self._field_setpoint = field
            self._activity = "to setpoint"
            self._start_sweep(field)

        # Wait for sweep to complete
        self.wait_for_idle()
        time.sleep(0.1)  # Small additional delay

        # Enable persistent mode if not going to zero
        if persistent_mode_control and abs(field) > 0.001:
            self._enable_persistent_mode()

        log.info(f"Field set complete: {self.field:.3f}T")

    def _enable_persistent_mode(self):
        """Enable persistent mode."""
        log.debug("Enabling persistent mode")

        if self._sweep_status != "at rest":
            raise MagnetError("Cannot enable persistent mode while sweeping")

        if not self.switch_heater_enabled:
            log.debug("Already in persistent mode")
            return

        # Procedure: hold -> heater off
        self._activity = "hold"
        self.switch_heater_enabled = False

        # Ramp leads to zero
        self._start_sweep(0.0)
        self.wait_for_idle()

        log.info("Persistent mode enabled")

    def _disable_persistent_mode(self):
        """Disable persistent mode."""
        log.debug("Disabling persistent mode")

        if self.switch_heater_enabled:
            log.debug("Already not in persistent mode")
            return

        # Ensure leads at zero
        if abs(self._field_measured) > 0.01:
            log.warning("Leads not at zero, ramping down")
            self._start_sweep(0.0)
            self.wait_for_idle()

        # Match leads to persistent field
        target = self._persistent_field
        self._start_sweep(target)
        self.wait_for_idle()

        # Turn on switch heater
        self.switch_heater_enabled = True

        log.info(f"Persistent mode disabled (matched field: {target:.3f}T)")

    def wait_for_idle(self, delay=0.1, max_wait_time=None, should_stop=lambda: False):
        """Wait until magnet is at rest (not ramping).

        Args:
            delay: Time between checks in seconds
            max_wait_time: Maximum wait time in seconds (None = no limit)
            should_stop: Function returning True to stop early

        Raises:
            TimeoutError: If max_wait_time exceeded
        """
        start_time = time.time()

        while self._sweeping or self._sweep_status != "at rest":
            if should_stop():
                log.debug("Wait for idle stopped early")
                return

            if max_wait_time and (time.time() - start_time) > max_wait_time:
                raise TimeoutError(f"Timeout waiting for idle after {max_wait_time}s")

            time.sleep(delay)

    def _start_sweep(self, target_field):
        """Start field sweep to target (internal method)."""
        if self._sweeping:
            log.warning("Already sweeping, waiting for completion")
            self.wait_for_idle()

        self._sweeping = True
        self._sweep_status = "sweeping"

        # Start sweep in background thread
        self._sweep_thread = threading.Thread(
            target=self._sweep_to_field,
            args=(target_field,),
            daemon=True
        )
        self._sweep_thread.start()

    def _sweep_to_field(self, target_field):
        """Sweep field to target (background thread)."""
        start_field = self._field_measured
        delta = target_field - start_field

        if abs(delta) < 0.001:
            self._sweeping = False
            self._sweep_status = "at rest"
            self._activity = "hold"
            return

        # Calculate sweep time (sped up 20x for testing)
        sweep_time = abs(delta) / (self._sweep_rate / 60.0) / 20.0  # 20x faster

        # Simulate sweep with small steps
        steps = max(5, int(sweep_time / 0.05))  # At least 5 steps
        step_size = delta / steps
        step_time = sweep_time / steps

        for i in range(steps):
            self._field_measured += step_size
            time.sleep(step_time)

        # Final correction to exact target
        self._field_measured = target_field

        # Update state
        self._sweeping = False
        self._sweep_status = "at rest"
        self._activity = "hold"

        log.debug(f"Sweep complete: {self._field_measured:.3f}T")

    # ==================== Device Info ====================

    def __repr__(self):
        return (f"<MockIPS120(field={self._field_measured:.3f}T, "
                f"setpoint={self._field_setpoint:.3f}T, activity={self._activity})>")
