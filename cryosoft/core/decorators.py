# ---
# description: |
#   @monitored and @control decorators for CryoSoft Virtual Instruments.
#   These are marker decorators: they tag methods with metadata attributes
#   so that BaseVirtualInstrument.__init_subclass__ can discover them,
#   and the GUI can auto-generate panels.
# last_updated: 2026-04-06
# ---

"""Decorators for Virtual Instrument methods.

Usage:
    class MyVI(BaseVirtualInstrument):
        @monitored
        def temperature(self) -> float:
            return self._driver.get_temperature()

        @control
        def set_temperature(self, target_K: float):
            self._driver.set_setpoint(target_K)

The @monitored decorator marks a method that:
- Returns a value to be polled every monitor tick.
- Is displayed as a live-updating number on the GUI panel.
- Is called by get_state() to build the VI state dict.

The @control decorator marks a method that:
- Appears as a button (with text-box inputs for arguments) on the GUI panel.
- Is callable by the user only when no procedure is running.
- Arguments are inferred from the function signature for GUI form generation.
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable


def monitored(func: Callable) -> Callable:
    """Mark a method as a monitored variable.

    The method will be:
    1. Called every monitor tick by get_state().
    2. Displayed on the GUI panel as a live value.
    3. Wrapped with logging by __init_subclass__.

    The method must take no arguments (besides self) and return a value.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    wrapper._is_monitored = True
    wrapper._display_name = func.__name__
    return wrapper


def control(func: Callable) -> Callable:
    """Mark a method as a user-controllable action.

    The method will:
    1. Appear as a button on the GUI panel.
    2. Have text-box inputs auto-generated from its signature.
    3. Be blocked when a procedure is running.
    4. Be wrapped with logging by __init_subclass__.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    wrapper._is_control = True
    wrapper._display_name = func.__name__

    # Extract parameter info for GUI form generation
    sig = inspect.signature(func)
    params = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        param_info: dict[str, Any] = {"name": name}
        if param.annotation != inspect.Parameter.empty:
            param_info["type"] = param.annotation
        if param.default != inspect.Parameter.empty:
            param_info["default"] = param.default
        params[name] = param_info

    wrapper._control_params = params
    return wrapper


def get_monitored_methods(cls_or_instance) -> list[str]:
    """Return names of all @monitored methods on a class or instance."""
    methods = []
    for name in dir(cls_or_instance):
        try:
            attr = getattr(cls_or_instance, name)
        except AttributeError:
            continue
        if callable(attr) and getattr(attr, "_is_monitored", False):
            methods.append(name)
    return methods


def get_control_methods(cls_or_instance) -> dict[str, dict]:
    """Return {method_name: param_info_dict} for all @control methods."""
    methods = {}
    for name in dir(cls_or_instance):
        try:
            attr = getattr(cls_or_instance, name)
        except AttributeError:
            continue
        if callable(attr) and getattr(attr, "_is_control", False):
            methods[name] = getattr(attr, "_control_params", {})
    return methods
