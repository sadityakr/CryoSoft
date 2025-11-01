"""
Device Wrapper
==============

Wraps driver and action layer with thread-safe locking.

Provides transparent access to driver properties and action methods
while ensuring thread-safe I/O through global locking.
"""

import logging
from typing import Any


class DeviceWrapper:
    """Thread-safe wrapper for driver + action layer.

    Combines a driver with its action handler and provides:
    - Transparent property access through global lock
    - Convenient high-level action methods
    - Automatic logging of operations

    Example:
        >>> from cryostat.drivers.mock.mock_itc503 import MockITC503
        >>> from cryostat.actions.temperature_actions import TemperatureActions
        >>> from cryostat.core.lock_manager import ThreadSafeLock
        >>>
        >>> driver = MockITC503()
        >>> lock = ThreadSafeLock()
        >>> wrapper = DeviceWrapper("temp_ctrl", driver, TemperatureActions, lock)
        >>>
        >>> # Access driver properties (automatically locked)
        >>> temp = wrapper.temperature_1
        >>> wrapper.temperature_setpoint = 4.2
        >>>
        >>> # Access action methods
        >>> wrapper.initiate()
        >>> wrapper.ramp_to_temperature(10.0, rate=1.0)
    """

    def __init__(self, name: str, driver: Any, action_class: type, lock: Any):
        """Initialize device wrapper.

        Args:
            name: Unique device name (e.g., "magnet1", "temp_controller1")
            driver: Driver instance (must implement appropriate base interface)
            action_class: Action class (e.g., TemperatureActions)
            lock: ThreadSafeLock instance for I/O locking
        """
        # Store as object attributes (not subject to __getattr__ interception)
        object.__setattr__(self, '_name', name)
        object.__setattr__(self, '_driver', driver)
        object.__setattr__(self, '_lock', lock)
        object.__setattr__(self, '_log', logging.getLogger(f"cryostat.{name}"))

        # Create action handler instance
        object.__setattr__(self, '_actions', action_class(driver))

        self._log.debug(f"DeviceWrapper created for '{name}' ({driver.name})")

    @property
    def name(self) -> str:
        """Device name."""
        return self._name

    @property
    def driver(self) -> Any:
        """Underlying driver instance."""
        return self._driver

    @property
    def actions(self) -> Any:
        """Action handler instance."""
        return self._actions

    def __getattr__(self, attr: str) -> Any:
        """Intercept attribute access and proxy to driver (with locking).

        First tries actions, then falls back to driver. All access is
        protected by the global lock.

        Args:
            attr: Attribute name

        Returns:
            Attribute value from actions or driver

        Raises:
            AttributeError: If attribute not found in actions or driver

        Example:
            >>> temp = wrapper.temperature_1  # Proxied to driver.temperature_1
            >>> wrapper.ramp_to_temperature(4.2)  # Proxied to actions method
        """
        # Check if it's an action method first
        if hasattr(self._actions, attr):
            action_attr = getattr(self._actions, attr)

            # If it's a method, wrap it with locking
            if callable(action_attr):
                def locked_action_method(*args, **kwargs):
                    with self._lock:
                        self._log.debug(f"Calling action: {attr}()")
                        return action_attr(*args, **kwargs)
                return locked_action_method
            else:
                # Non-callable action attribute (unlikely but handle it)
                with self._lock:
                    return action_attr

        # Check if it's a driver property
        if hasattr(self._driver, attr):
            driver_attr = getattr(self._driver, attr)

            # If it's a callable (method), wrap with locking
            if callable(driver_attr):
                def locked_driver_method(*args, **kwargs):
                    with self._lock:
                        self._log.debug(f"Calling driver method: {attr}()")
                        return driver_attr(*args, **kwargs)
                return locked_driver_method
            else:
                # It's a property - read with locking
                with self._lock:
                    self._log.debug(f"Reading: {attr}")
                    return driver_attr

        # Attribute not found
        raise AttributeError(
            f"Device '{self._name}' has no attribute '{attr}' "
            f"(checked actions and driver)"
        )

    def __setattr__(self, attr: str, value: Any):
        """Intercept attribute setting and proxy to driver (with locking).

        Args:
            attr: Attribute name
            value: Value to set

        Raises:
            AttributeError: If attribute not found or not settable

        Example:
            >>> wrapper.temperature_setpoint = 4.2  # Proxied to driver
        """
        # Handle internal attributes normally
        if attr.startswith('_'):
            object.__setattr__(self, attr, value)
            return

        # Try to set on driver (with locking)
        if hasattr(self._driver, attr):
            with self._lock:
                self._log.debug(f"Writing: {attr} = {value}")
                setattr(self._driver, attr, value)
                return

        # Attribute not settable
        raise AttributeError(
            f"Device '{self._name}' cannot set attribute '{attr}' "
            f"(not found in driver or is read-only)"
        )

    # ==================== Convenience Methods ====================
    # These provide easier access to common actions

    def initiate(self) -> bool:
        """Initialize device for operation.

        Delegates to action layer's initiate() method.

        Returns:
            bool: True if initialization successful
        """
        return self._actions.initiate()

    def get_status(self) -> dict:
        """Get current device status.

        Delegates to action layer's get_status() method.

        Returns:
            dict: Status information
        """
        with self._lock:
            return self._actions.get_status()

    def __repr__(self):
        return (f"<DeviceWrapper(name='{self._name}', "
               f"driver={self._driver.__class__.__name__})>")

    def __str__(self):
        return f"Device '{self._name}' ({self._driver.name})"


# Export public API
__all__ = ['DeviceWrapper']
