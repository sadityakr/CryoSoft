# ---
# description: |
#   Typed vocabulary of frozen dataclasses shared by every CryoSoft layer.
#   Replaces the untyped nested dicts and anonymous tuples that drivers,
#   virtual instruments, the orchestrator, procedures and the data manager
#   currently exchange. Each class validates eagerly at construction so a
#   malformed plan fails at the boundary — with the guilty module in the
#   traceback — instead of deep inside the tick loop or the HDF5 writer.
# entry_point: Not run directly. Imported wherever a plan is built or consumed.
# dependencies:
#   - cryosoft.core.exceptions (DataSchemaError)
# input: |
#   Constructor arguments only. Procedures build Target / Command / PhasePlan /
#   StepPlan objects; the GUI reads ParamSpec / ParamGroup; the data manager
#   builds and checks DataSchema objects.
# process: |
#   Every dataclass is frozen and validates in __post_init__, raising ValueError
#   (bad value) or TypeError (wrong type) with a message naming the offending
#   field. Dict fields are defensively copied so later caller mutations cannot
#   leak into an already-constructed, notionally immutable plan.
# output: |
#   Immutable value objects. DataSchema additionally offers multiplexed() (derive
#   a per-route schema) and validate() (check one datapoint, raising
#   DataSchemaError listing every problem at once).
# last_updated: 2026-07-13
# ---

"""Typed vocabulary of frozen dataclasses shared across all CryoSoft layers."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from cryosoft.core.exceptions import DataSchemaError

__all__ = [
    "Target",
    "Command",
    "PhasePlan",
    "StepPlan",
    "ParamSpec",
    "ParamGroup",
    "DataSchema",
]

# Scalar Python types accepted for GUI-facing parameters and their HDF5 dtypes.
_PARAM_TYPES: tuple[type, ...] = (float, int, str, bool)
_ALLOWED_DTYPES: frozenset[str] = frozenset({"float", "int"})


def _is_real_number(value: Any) -> bool:
    """Return True if ``value`` is a real int or float, explicitly rejecting bool.

    ``bool`` is a subclass of ``int`` in Python, so ``isinstance(True, int)`` is
    True. Every numeric field in this module means a physical quantity, never a
    flag, so a stray ``True`` must not silently become ``1.0``.

    Args:
        value: The candidate to test.

    Returns:
        True for a non-bool ``int`` or ``float``, False otherwise.
    """
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class Target:
    """A desired end-state for one system variable (e.g. field, temperature).

    Frozen dataclass: once built it cannot be mutated, so a plan cannot be
    edited out from under the orchestrator after it is submitted.

    Attributes:
        target: The value to reach, in SI units (Tesla, Kelvin, Ampere …).
            Must be a finite real number; ``bool`` is rejected.
        rate: Optional ramp rate, forwarded verbatim to the target VI's
            ``start_ramp()`` — its unit is whatever that VI's ramp-rate unit
            is (e.g. K/min for the temperature controllers). If given it must
            be a finite real number strictly greater than zero.
        persistent: Optional flag (magnet persistent-mode request). If given it
            must be a ``bool``.
    """

    target: float
    rate: float | None = None
    persistent: bool | None = None

    def __post_init__(self) -> None:
        """Validate and normalise the fields.

        Raises:
            TypeError: If ``target``/``rate`` is not a real number, or
                ``persistent`` is not a bool.
            ValueError: If ``target`` is non-finite, or ``rate`` is non-finite
                or not strictly positive.
        """
        if not _is_real_number(self.target):
            raise TypeError(f"Target.target must be a real number, got {self.target!r}")
        if not math.isfinite(self.target):
            raise ValueError(f"Target.target must be finite, got {self.target!r}")
        object.__setattr__(self, "target", float(self.target))

        if self.rate is not None:
            if not _is_real_number(self.rate):
                raise TypeError(f"Target.rate must be a real number, got {self.rate!r}")
            if not math.isfinite(self.rate):
                raise ValueError(f"Target.rate must be finite, got {self.rate!r}")
            if self.rate <= 0:
                raise ValueError(f"Target.rate must be > 0, got {self.rate!r}")
            object.__setattr__(self, "rate", float(self.rate))

        if self.persistent is not None and not isinstance(self.persistent, bool):
            raise TypeError(
                f"Target.persistent must be a bool, got {self.persistent!r}"
            )


@dataclass(frozen=True)
class Command:
    """A single method call to dispatch on a named virtual instrument.

    The orchestrator is the sole writer to hardware; a Command is the typed
    request a procedure hands it — "call ``method`` on VI ``vi_name`` with
    ``kwargs``". ``kwargs`` is defensively copied so a caller that later mutates
    the dict it passed in cannot change this command's arguments.

    Attributes:
        vi_name: Name of the target virtual instrument. Non-empty string.
        method: Name of the VI method to call. Non-empty string and a valid
            Python identifier.
        kwargs: Keyword arguments for the call. Defensively copied.
    """

    vi_name: str
    method: str
    kwargs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate the fields and defensively copy ``kwargs``.

        Raises:
            TypeError: If ``vi_name``/``method`` is not a string, or ``kwargs``
                is not a dict.
            ValueError: If ``vi_name``/``method`` is empty, or ``method`` is not
                a valid Python identifier.
        """
        if not isinstance(self.vi_name, str):
            raise TypeError(f"Command.vi_name must be a str, got {self.vi_name!r}")
        if not self.vi_name:
            raise ValueError("Command.vi_name must be a non-empty str")
        if not isinstance(self.method, str):
            raise TypeError(f"Command.method must be a str, got {self.method!r}")
        if not self.method:
            raise ValueError("Command.method must be a non-empty str")
        if not self.method.isidentifier():
            raise ValueError(
                f"Command.method must be a valid Python identifier, got {self.method!r}"
            )
        if not isinstance(self.kwargs, dict):
            raise TypeError(f"Command.kwargs must be a dict, got {self.kwargs!r}")
        object.__setattr__(self, "kwargs", dict(self.kwargs))


def _validate_targets(targets: Any, owner: str) -> dict[str, Target]:
    """Validate a ``{name: Target}`` mapping and return a defensive copy.

    Args:
        targets: The mapping to validate.
        owner: Name of the owning class, used in error messages.

    Returns:
        A shallow copy of ``targets``.

    Raises:
        TypeError: If ``targets`` is not a mapping, a key is not a string, or a
            value is not a ``Target``.
        ValueError: If a key is an empty string.
    """
    if not isinstance(targets, dict):
        raise TypeError(f"{owner}.targets must be a dict, got {targets!r}")
    for name, tgt in targets.items():
        if not isinstance(name, str):
            raise TypeError(f"{owner}.targets key must be a str, got {name!r}")
        if not name:
            raise ValueError(f"{owner}.targets key must be a non-empty str")
        if not isinstance(tgt, Target):
            raise TypeError(
                f"{owner}.targets[{name!r}] must be a Target, got {tgt!r}"
            )
    return dict(targets)


def _validate_wait(wait_s: Any, owner: str) -> float:
    """Validate a non-negative wait time and return it coerced to float.

    Args:
        wait_s: The wait time in seconds.
        owner: Name of the owning class, used in error messages.

    Returns:
        ``wait_s`` as a float.

    Raises:
        TypeError: If ``wait_s`` is not a real number.
        ValueError: If ``wait_s`` is non-finite or negative.
    """
    if not _is_real_number(wait_s):
        raise TypeError(f"{owner}.wait_s must be a real number, got {wait_s!r}")
    if not math.isfinite(wait_s):
        raise ValueError(f"{owner}.wait_s must be finite, got {wait_s!r}")
    if wait_s < 0:
        raise ValueError(f"{owner}.wait_s must be >= 0, got {wait_s!r}")
    return float(wait_s)


@dataclass(frozen=True)
class PhasePlan:
    """What a procedure's ``initiate()`` and ``standby()`` return.

    A phase plan bundles the system targets to reach, an ORDERED sequence of
    virtual-instrument commands, and a settle time. Command order is
    semantically meaningful — e.g. a switch heater must settle before a source
    arms — so ``commands`` is normalised to a tuple and never reordered. The
    ``targets`` dict is defensively copied.

    Attributes:
        targets: Mapping of variable name to desired ``Target``. Defensively
            copied.
        commands: Ordered VI commands to dispatch, normalised to a tuple.
        wait_s: Settle time in seconds after applying targets/commands. Finite
            and non-negative.
    """

    targets: dict[str, Target]
    commands: tuple[Command, ...] = ()
    wait_s: float = 0.0

    def __post_init__(self) -> None:
        """Validate the fields; copy ``targets`` and normalise ``commands``.

        Raises:
            TypeError: If ``targets`` / a command / ``wait_s`` has the wrong type.
            ValueError: If a target key is empty, or ``wait_s`` is invalid.
        """
        object.__setattr__(self, "targets", _validate_targets(self.targets, "PhasePlan"))

        commands = tuple(self.commands)
        for i, cmd in enumerate(commands):
            if not isinstance(cmd, Command):
                raise TypeError(
                    f"PhasePlan.commands[{i}] must be a Command, got {cmd!r}"
                )
        object.__setattr__(self, "commands", commands)

        object.__setattr__(self, "wait_s", _validate_wait(self.wait_s, "PhasePlan"))


@dataclass(frozen=True)
class StepPlan:
    """What ``change_sweep_step()`` returns for the next sweep point.

    The orchestrator calls ``change_sweep_step()`` before each measurement; it
    returns a ``StepPlan`` for the next point, or ``None`` when the sweep is
    done. The ``targets`` dict is defensively copied.

    Attributes:
        targets: Mapping of variable name to desired ``Target`` for this point.
            Defensively copied.
        wait_s: Settle time in seconds before measuring. Finite and
            non-negative.
    """

    targets: dict[str, Target]
    wait_s: float

    def __post_init__(self) -> None:
        """Validate the fields and defensively copy ``targets``.

        Raises:
            TypeError: If ``targets`` or ``wait_s`` has the wrong type.
            ValueError: If a target key is empty, or ``wait_s`` is invalid.
        """
        object.__setattr__(self, "targets", _validate_targets(self.targets, "StepPlan"))
        object.__setattr__(self, "wait_s", _validate_wait(self.wait_s, "StepPlan"))


@dataclass(frozen=True)
class ParamSpec:
    """One GUI-facing procedure-parameter declaration.

    Replaces the per-parameter spec dicts (``{"type": float, "default": ...}``)
    procedures declare today. This is purely semantic — it names no Qt widget
    classes; ``widget_hint`` is an optional free-form hint, not a widget name.
    Setting ``structural=True`` means changing this parameter's value changes
    *which* parameter groups exist, so the GUI must re-derive the form when it
    changes. The ``choices`` dict is defensively copied.

    Attributes:
        type: The Python scalar type of the value: one of ``float, int, str,
            bool``.
        default: The initial value. Must be an instance of ``type`` (an ``int``
            is accepted for ``type=float``; a ``bool`` never satisfies a numeric
            type).
        unit: SI unit label for display (e.g. "T", "K"). GUI concern only.
        description: Human-readable help text.
        min: Optional inclusive lower bound. Numeric types only; excludes
            ``choices``.
        max: Optional inclusive upper bound. Numeric types only; excludes
            ``choices``.
        choices: Optional non-empty label→value dict rendering as a drop-down.
            Every value must be an instance of ``type`` and ``default`` must
            equal one of them. Mutually exclusive with ``min``/``max``.
            Defensively copied.
        structural: If True, changing this value re-derives the whole form.
        widget_hint: Optional non-empty display hint (e.g. "slider"); never a
            concrete Qt widget class name.
    """

    type: type
    default: Any
    unit: str = ""
    description: str = ""
    min: float | None = None
    max: float | None = None
    choices: dict[str, Any] | None = None
    structural: bool = False
    widget_hint: str | None = None

    def __post_init__(self) -> None:
        """Validate the declaration and defensively copy ``choices``.

        Raises:
            TypeError: If ``type`` is not one of the allowed scalar types, or a
                string/flag field has the wrong type.
            ValueError: If ``default`` does not match ``type``; if bounds are
                given for a non-numeric type, are inconsistent, or coexist with
                ``choices``; if ``choices`` is empty, contains a wrong-typed
                value, or excludes ``default``; or if ``widget_hint`` is empty.
        """
        if self.type not in _PARAM_TYPES:
            raise TypeError(
                f"ParamSpec.type must be one of (float, int, str, bool), "
                f"got {self.type!r}"
            )

        if not self._matches_type(self.default):
            raise ValueError(
                f"ParamSpec.default {self.default!r} is not a {self.type.__name__}"
            )

        for label, val in (("unit", self.unit), ("description", self.description)):
            if not isinstance(val, str):
                raise TypeError(f"ParamSpec.{label} must be a str, got {val!r}")

        if not isinstance(self.structural, bool):
            raise TypeError(
                f"ParamSpec.structural must be a bool, got {self.structural!r}"
            )

        if self.widget_hint is not None:
            if not isinstance(self.widget_hint, str):
                raise TypeError(
                    f"ParamSpec.widget_hint must be a str, got {self.widget_hint!r}"
                )
            if not self.widget_hint:
                raise ValueError("ParamSpec.widget_hint must be a non-empty str")

        if self.choices is not None:
            self._validate_choices()
        else:
            self._validate_bounds()

    def _matches_type(self, value: Any) -> bool:
        """Return True if ``value`` is a legal instance of ``self.type``.

        Applies the numeric nuance: an ``int`` is accepted where ``float`` is
        declared, but a ``bool`` never satisfies ``int`` or ``float`` (it must
        be checked before the ``int`` acceptance because ``bool`` subclasses
        ``int``).

        Args:
            value: The candidate value.

        Returns:
            True if ``value`` is acceptable for ``self.type``.
        """
        if self.type is bool:
            return isinstance(value, bool)
        if isinstance(value, bool):
            return False  # bool is never a valid int/float/str here
        if self.type is float:
            return isinstance(value, (int, float))
        return isinstance(value, self.type)

    def _validate_bounds(self) -> None:
        """Validate ``min``/``max`` when no ``choices`` are declared.

        Raises:
            TypeError: If a bound is not a real number.
            ValueError: If bounds are given for a non-numeric type, if
                ``min > max``, or if ``default`` falls outside the bounds.
        """
        if self.min is None and self.max is None:
            return
        if self.type not in (int, float):
            raise ValueError(
                f"ParamSpec.min/max are only valid for numeric types, "
                f"not {self.type.__name__}"
            )
        for name, bound in (("min", self.min), ("max", self.max)):
            if bound is not None and not _is_real_number(bound):
                raise TypeError(
                    f"ParamSpec.{name} must be a real number, got {bound!r}"
                )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(
                f"ParamSpec.min {self.min!r} must be <= max {self.max!r}"
            )
        if self.min is not None and self.default < self.min:
            raise ValueError(
                f"ParamSpec.default {self.default!r} is below min {self.min!r}"
            )
        if self.max is not None and self.default > self.max:
            raise ValueError(
                f"ParamSpec.default {self.default!r} is above max {self.max!r}"
            )

    def _validate_choices(self) -> None:
        """Validate the enumerated ``choices`` dict and copy it defensively.

        Raises:
            TypeError: If ``choices`` is not a dict.
            ValueError: If ``choices`` is empty, if any value is not an instance
                of ``type``, if ``default`` is not one of the values, or if any
                bound is set (bounds and choices are mutually exclusive).
        """
        if not isinstance(self.choices, dict):
            raise TypeError(f"ParamSpec.choices must be a dict, got {self.choices!r}")
        if not self.choices:
            raise ValueError("ParamSpec.choices must be a non-empty dict")
        if self.min is not None or self.max is not None:
            raise ValueError(
                "ParamSpec.choices and min/max are mutually exclusive; set only one"
            )
        for label, val in self.choices.items():
            if not self._matches_type(val):
                raise ValueError(
                    f"ParamSpec.choices[{label!r}] value {val!r} is not a "
                    f"{self.type.__name__}"
                )
        if self.default not in self.choices.values():
            raise ValueError(
                f"ParamSpec.default {self.default!r} is not one of the choice "
                f"values {list(self.choices.values())}"
            )
        object.__setattr__(self, "choices", dict(self.choices))


@dataclass(frozen=True)
class ParamGroup:
    """One rendered sub-panel of a procedure's parameter form.

    The GUI renders one ``QGroupBox`` per group, in list order. ``key`` is the
    stable identity used to cache the group's values across re-renders (e.g.
    "system", "measurement:keithley_delta_mode"), so it must survive form
    re-derivation even when titles change. The ``params`` dict is defensively
    copied.

    Attributes:
        key: Stable identity for value caching. Non-empty string.
        title: Human-readable panel heading. Non-empty string.
        params: Mapping of parameter name to ``ParamSpec``. Defensively copied.
    """

    key: str
    title: str
    params: dict[str, ParamSpec]

    def __post_init__(self) -> None:
        """Validate the fields and defensively copy ``params``.

        Raises:
            TypeError: If ``key``/``title`` is not a string, ``params`` is not a
                dict, a params key is not a string, or a value is not a
                ``ParamSpec``.
            ValueError: If ``key``/``title`` or a params key is an empty string.
        """
        for name, val in (("key", self.key), ("title", self.title)):
            if not isinstance(val, str):
                raise TypeError(f"ParamGroup.{name} must be a str, got {val!r}")
            if not val:
                raise ValueError(f"ParamGroup.{name} must be a non-empty str")

        if not isinstance(self.params, dict):
            raise TypeError(f"ParamGroup.params must be a dict, got {self.params!r}")
        for name, spec in self.params.items():
            if not isinstance(name, str):
                raise TypeError(f"ParamGroup.params key must be a str, got {name!r}")
            if not name:
                raise ValueError("ParamGroup.params key must be a non-empty str")
            if not isinstance(spec, ParamSpec):
                raise TypeError(
                    f"ParamGroup.params[{name!r}] must be a ParamSpec, got {spec!r}"
                )
        object.__setattr__(self, "params", dict(self.params))


@dataclass(frozen=True)
class DataSchema:
    """The declared HDF5 layout of one measurement run.

    Assembled at ``initiate()`` by composition: the sweep axis contributes its
    sweep column, the station its system columns, the measurement VI its arrays,
    and a multiplexer suffixes the arrays per route (see ``multiplexed``). This
    is the single owner of the run's shape contract, the thing that catches
    "HDF5 expected a different format" mismatches before any data is written.
    Both dicts are defensively copied.

    Attributes:
        sweep_columns: Mapping of scalar column name to dtype string. Allowed
            dtypes are "float" and "int". Defensively copied.
        arrays: Mapping of array name to its per-point length (an ``int`` > 0;
            ``bool`` is rejected). Defensively copied.
    """

    sweep_columns: dict[str, str]
    arrays: dict[str, int]

    def __post_init__(self) -> None:
        """Validate names, dtypes and lengths; copy both dicts.

        Raises:
            TypeError: If ``sweep_columns``/``arrays`` is not a dict, a name is
                not a string, or an array length is not an int.
            ValueError: If a name is empty, a dtype is not in the allowed set, or
                an array length is not strictly positive.
        """
        if not isinstance(self.sweep_columns, dict):
            raise TypeError(
                f"DataSchema.sweep_columns must be a dict, got {self.sweep_columns!r}"
            )
        for name, dtype in self.sweep_columns.items():
            if not isinstance(name, str):
                raise TypeError(
                    f"DataSchema.sweep_columns key must be a str, got {name!r}"
                )
            if not name:
                raise ValueError("DataSchema.sweep_columns key must be a non-empty str")
            if dtype not in _ALLOWED_DTYPES:
                raise ValueError(
                    f"DataSchema.sweep_columns[{name!r}] dtype {dtype!r} must be "
                    f"one of {sorted(_ALLOWED_DTYPES)}"
                )

        if not isinstance(self.arrays, dict):
            raise TypeError(f"DataSchema.arrays must be a dict, got {self.arrays!r}")
        for name, length in self.arrays.items():
            if not isinstance(name, str):
                raise TypeError(f"DataSchema.arrays key must be a str, got {name!r}")
            if not name:
                raise ValueError("DataSchema.arrays key must be a non-empty str")
            if isinstance(length, bool) or not isinstance(length, int):
                raise TypeError(
                    f"DataSchema.arrays[{name!r}] length must be an int, got {length!r}"
                )
            if length <= 0:
                raise ValueError(
                    f"DataSchema.arrays[{name!r}] length must be > 0, got {length!r}"
                )

        object.__setattr__(self, "sweep_columns", dict(self.sweep_columns))
        object.__setattr__(self, "arrays", dict(self.arrays))

    def multiplexed(
        self, routes: Sequence[str], scalar_columns: Sequence[str] = ()
    ) -> DataSchema:
        """Return a new schema with every array (and named scalars) per route.

        Each array ``name`` becomes ``f"{name}__{route}"`` for each route. The
        resulting ``arrays`` dict is ordered arrays-outer, routes-inner: all
        route variants of the first array appear first (in route order), then
        all variants of the second, and so on.

        By default ``sweep_columns`` is unchanged. The optional
        ``scalar_columns`` argument names sweep columns that are *also* expanded
        per route (e.g. a per-route ``n_valid`` becomes ``n_valid__Mux-Ch1``,
        ``n_valid__Mux-Ch2``, … and the original ``n_valid`` is removed). Each
        expanded scalar keeps the position it had among ``sweep_columns``; every
        other sweep column is passed through unchanged. Passing the default
        empty ``scalar_columns`` leaves ``sweep_columns`` byte-identical to this
        schema's, so existing single-array multiplexing is unaffected.

        Args:
            routes: Ordered, non-empty sequence of unique route names. Each must
                be a non-empty string containing neither "__" (the reserved
                array/route separator) nor "/" (illegal in an HDF5 dataset name).
            scalar_columns: Names of existing ``sweep_columns`` to expand once
                per route as well (same suffix rule as the arrays). Every name
                must already be a sweep column. Defaults to ``()`` (no scalar
                expansion — the historical behaviour).

        Returns:
            A new ``DataSchema`` with the expanded arrays (and expanded scalars).

        Raises:
            TypeError: If a route is not a string.
            ValueError: If ``routes`` is empty, a route is empty, contains "__"
                or "/", the routes are not unique, or a name in
                ``scalar_columns`` is not one of this schema's sweep columns.
        """
        routes = list(routes)
        if not routes:
            raise ValueError("DataSchema.multiplexed requires at least one route")
        seen: set[str] = set()
        for route in routes:
            if not isinstance(route, str):
                raise TypeError(f"DataSchema route must be a str, got {route!r}")
            if not route:
                raise ValueError("DataSchema route must be a non-empty str")
            if "__" in route:
                raise ValueError(
                    f"DataSchema route {route!r} must not contain '__' "
                    "(the array/route separator)"
                )
            if "/" in route:
                raise ValueError(
                    f"DataSchema route {route!r} must not contain '/' "
                    "(illegal in an HDF5 dataset name)"
                )
            if route in seen:
                raise ValueError(f"DataSchema route {route!r} is duplicated")
            seen.add(route)

        scalar_names = list(scalar_columns)
        for col in scalar_names:
            if col not in self.sweep_columns:
                raise ValueError(
                    f"DataSchema.multiplexed scalar column {col!r} is not one of "
                    f"the sweep columns {list(self.sweep_columns)}"
                )
        scalar_set = set(scalar_names)

        expanded_columns: dict[str, str] = {}
        for name, dtype in self.sweep_columns.items():
            if name in scalar_set:
                for route in routes:
                    expanded_columns[f"{name}__{route}"] = dtype
            else:
                expanded_columns[name] = dtype

        expanded: dict[str, int] = {}
        for name, length in self.arrays.items():
            for route in routes:
                expanded[f"{name}__{route}"] = length
        return DataSchema(sweep_columns=expanded_columns, arrays=expanded)

    def validate(self, datapoint: Mapping[str, Any]) -> None:
        """Check one datapoint against this schema, reporting every problem.

        Collects all mismatches rather than stopping at the first, so a caller
        fixing a malformed datapoint sees the complete list in a single
        ``DataSchemaError`` instead of one error per fix-and-rerun cycle.

        Checks performed:
            * every declared key is present (missing keys reported);
            * no undeclared keys are present (extra keys reported);
            * each declared array value has ``len()`` equal to its declared
              length (a wrong length, or a value with no ``len()``, is reported);
            * each declared sweep-column value is a real-number scalar (``bool``
              rejected; dtype "int" accepts ``int``, dtype "float" accepts
              ``int`` or ``float``).

        Args:
            datapoint: Mapping of column/array name to value to check.

        Returns:
            None if the datapoint conforms.

        Raises:
            DataSchemaError: If any check fails; the message lists all problems.
        """
        declared = set(self.sweep_columns) | set(self.arrays)
        present = set(datapoint)
        problems: list[str] = []

        for key in sorted(declared - present):
            problems.append(f"missing declared key {key!r}")
        for key in sorted(present - declared):
            problems.append(f"extra undeclared key {key!r}")

        for name, length in self.arrays.items():
            if name not in datapoint:
                continue
            value = datapoint[name]
            try:
                actual = len(value)
            except TypeError:
                problems.append(
                    f"array key {name!r} value {value!r} has no length "
                    f"(expected length {length})"
                )
                continue
            if actual != length:
                problems.append(
                    f"array key {name!r} has length {actual}, expected {length}"
                )

        for name, dtype in self.sweep_columns.items():
            if name not in datapoint:
                continue
            value = datapoint[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(
                    f"sweep column {name!r} value {value!r} is not a real-number scalar"
                )
            elif dtype == "int" and not isinstance(value, int):
                problems.append(
                    f"sweep column {name!r} value {value!r} is not an int (dtype 'int')"
                )

        if problems:
            raise DataSchemaError(
                "datapoint does not match schema: " + "; ".join(problems)
            )
