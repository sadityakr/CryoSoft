# ---
# description: |
#   @monitored and @control decorators for CryoSoft Virtual Instruments.
#   These are marker decorators: they tag methods with metadata attributes
#   so that BaseVirtualInstrument.__init_subclass__ can discover them,
#   and the GUI can auto-generate panels. @control also carries a capability
#   scope ("measurement" or "operation", see docs/plans/cryogenics-logbook.md
#   §5) enforced at dispatch by Station.send_measurement_commands().
# last_updated: 2026-07-19
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

        @control(scope="operation")
        def switch_heater_on(self):
            self._driver.set_switch_heater(True)

The @monitored decorator marks a method that:
- Returns a value to be polled every monitor tick.
- Is displayed as a live-updating number on the GUI panel.
- Is called by get_state() to build the VI state dict.

The @control decorator marks a method that:
- Appears as a button (with text-box inputs for arguments) on the GUI panel.
- Is callable by the user only when no procedure is running.
- Arguments are inferred from the function signature for GUI form generation.
- Carries a capability scope: ``"measurement"`` (the default, usable by any
  plan) or ``"operation"`` (usable only by an operation's plan — see the
  capability-scope standard in GLOSSARY.md). GUI behavior is unchanged either
  way; a human in IDLE can still click any @control as before. The scope is
  enforced only at plan-dispatch time, by
  ``Station.send_measurement_commands()``.
"""

from __future__ import annotations

import functools
import inspect
import typing
from typing import Any, Callable

# The only valid @control capability scopes (plan §5). Anything else raises
# ValueError at decoration time — a typo in scope="opration" fails loudly at
# import time, not silently at dispatch.
VALID_CONTROL_SCOPES: frozenset[str] = frozenset({"measurement", "operation"})


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


def control(func: Callable | None = None, *, scope: str = "measurement") -> Callable:
    """Mark a method as a user-controllable action.

    Works both bare (``@control``, scope defaults to ``"measurement"``) and
    parametrized (``@control(scope="operation")``).

    The method will:
    1. Appear as a button on the GUI panel.
    2. Have text-box inputs auto-generated from its signature.
    3. Be blocked when a procedure is running.
    4. Be wrapped with logging by __init_subclass__.
    5. Carry a capability scope enforced at plan-dispatch time (see the module
       docstring and GLOSSARY.md's "Capability scope" entry).

    Args:
        func: The method being decorated (bare-decorator form only; ``None``
            when called parametrized, e.g. ``@control(scope=...)``).
        scope: ``"measurement"`` (default) or ``"operation"``.

    Returns:
        The wrapped method (bare form) or a decorator (parametrized form).

    Raises:
        ValueError: If ``scope`` is not one of ``VALID_CONTROL_SCOPES``.
    """
    if scope not in VALID_CONTROL_SCOPES:
        raise ValueError(
            f"@control scope must be one of {sorted(VALID_CONTROL_SCOPES)}, "
            f"got {scope!r}"
        )

    def _decorate(inner_func: Callable) -> Callable:
        @functools.wraps(inner_func)
        def wrapper(*args, **kwargs):
            return inner_func(*args, **kwargs)

        wrapper._is_control = True
        wrapper._display_name = inner_func.__name__
        wrapper._control_scope = scope

        # Resolve annotations (handles `from __future__ import annotations` string form).
        try:
            hints = typing.get_type_hints(inner_func)
        except Exception:
            hints = {}

        sig = inspect.signature(inner_func)
        params = {}
        for name, param in sig.parameters.items():
            if name == "self":
                continue
            param_info: dict[str, Any] = {"name": name}
            resolved_type = hints.get(name)
            if resolved_type is not None:
                param_info["type"] = resolved_type
            if param.default != inspect.Parameter.empty:
                param_info["default"] = param.default
            params[name] = param_info

        wrapper._control_params = params
        return wrapper

    if func is not None:
        # Bare form: @control
        return _decorate(func)
    # Parametrized form: @control(scope="operation")
    return _decorate


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


def get_control_scope(method: Callable) -> str:
    """Return a @control method's capability scope, defaulting to "measurement".

    Args:
        method: A callable, typically a bound or unbound VI method. A method
            never decorated with ``@control`` (or without the marker
            attribute at all) is treated as ``"measurement"``-scope — the
            enforcement default for undecorated methods.

    Returns:
        ``"measurement"`` or ``"operation"``.
    """
    return getattr(method, "_control_scope", "measurement")
