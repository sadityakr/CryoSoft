# ---
# description: |
#   Unit tests for cryosoft.core.plan — the typed vocabulary of frozen
#   dataclasses (Target, Command, PhasePlan, StepPlan, ParamSpec, ParamGroup,
#   DataSchema). Covers construction happy paths and defaults, frozen-ness,
#   every eager validation rule (each asserting the error names the offending
#   field), the DataSchema.multiplexed / validate behaviours, and defensive
#   copying of dict fields.
# last_updated: 2026-07-13
# ---

import dataclasses

import pytest

from cryosoft.core.exceptions import DataSchemaError
from cryosoft.core.plan import (
    Command,
    DataSchema,
    EnvelopeBound,
    ParamGroup,
    ParamSpec,
    PhasePlan,
    SessionEnvelope,
    StepPlan,
    Target,
)


# ── Target ────────────────────────────────────────────────────────────────────


def test_target_happy_and_defaults():
    t = Target(1.5)
    assert t.target == 1.5
    assert t.rate is None
    assert t.persistent is None
    t2 = Target(2, rate=0.1, persistent=True)
    assert t2.target == 2.0 and isinstance(t2.target, float)
    assert t2.rate == 0.1
    assert t2.persistent is True


def test_target_bool_rejected():
    with pytest.raises(TypeError, match="Target.target"):
        Target(True)


def test_target_nan_inf_rejected():
    with pytest.raises(ValueError, match="Target.target"):
        Target(float("nan"))
    with pytest.raises(ValueError, match="Target.target"):
        Target(float("inf"))


def test_target_rate_must_be_positive():
    with pytest.raises(ValueError, match="Target.rate"):
        Target(1.0, rate=0.0)
    with pytest.raises(ValueError, match="Target.rate"):
        Target(1.0, rate=-1.0)


def test_target_rate_nonfinite_rejected():
    with pytest.raises(ValueError, match="Target.rate"):
        Target(1.0, rate=float("inf"))


def test_target_persistent_type():
    with pytest.raises(TypeError, match="Target.persistent"):
        Target(1.0, persistent=1)


def test_target_frozen():
    t = Target(1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.target = 2.0  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.nonexistent = 3  # type: ignore[attr-defined]


# ── Command ───────────────────────────────────────────────────────────────────


def test_command_happy_and_default_kwargs():
    c = Command("magnet", "set_field")
    assert c.vi_name == "magnet"
    assert c.method == "set_field"
    assert c.kwargs == {}
    c2 = Command("src", "arm", kwargs={"level": 1e-6})
    assert c2.kwargs == {"level": 1e-6}


def test_command_empty_vi_name():
    with pytest.raises(ValueError, match="Command.vi_name"):
        Command("", "m")


def test_command_empty_method():
    with pytest.raises(ValueError, match="Command.method"):
        Command("vi", "")


def test_command_method_must_be_identifier():
    with pytest.raises(ValueError, match="Command.method"):
        Command("vi", "not a method")


def test_command_frozen():
    c = Command("vi", "m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.vi_name = "other"  # type: ignore[misc]


def test_command_kwargs_defensive_copy():
    payload = {"a": 1}
    c = Command("vi", "m", kwargs=payload)
    payload["a"] = 999
    payload["b"] = 2
    assert c.kwargs == {"a": 1}


# ── PhasePlan ─────────────────────────────────────────────────────────────────


def test_phaseplan_happy_and_defaults():
    p = PhasePlan(targets={"field": Target(1.0)})
    assert p.commands == ()
    assert p.wait_s == 0.0


def test_phaseplan_commands_normalized_to_tuple_order_preserved():
    c1 = Command("switch", "close")
    c2 = Command("source", "arm")
    p = PhasePlan(targets={}, commands=[c1, c2])
    assert isinstance(p.commands, tuple)
    assert p.commands == (c1, c2)


def test_phaseplan_bad_target_value():
    with pytest.raises(TypeError, match="PhasePlan.targets"):
        PhasePlan(targets={"field": "not a target"})


def test_phaseplan_empty_target_key():
    with pytest.raises(ValueError, match="PhasePlan.targets"):
        PhasePlan(targets={"": Target(1.0)})


def test_phaseplan_bad_command():
    with pytest.raises(TypeError, match="PhasePlan.commands"):
        PhasePlan(targets={}, commands=["nope"])


def test_phaseplan_wait_negative():
    with pytest.raises(ValueError, match="PhasePlan.wait_s"):
        PhasePlan(targets={}, wait_s=-1.0)


def test_phaseplan_frozen():
    p = PhasePlan(targets={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.wait_s = 5.0  # type: ignore[misc]


def test_phaseplan_targets_defensive_copy():
    targets = {"field": Target(1.0)}
    p = PhasePlan(targets=targets)
    targets["temp"] = Target(4.2)
    assert "temp" not in p.targets


# ── StepPlan ──────────────────────────────────────────────────────────────────


def test_stepplan_happy():
    s = StepPlan(targets={"field": Target(0.5)}, wait_s=2.0)
    assert s.wait_s == 2.0


def test_stepplan_bad_wait_type():
    with pytest.raises(TypeError, match="StepPlan.wait_s"):
        StepPlan(targets={}, wait_s="soon")


def test_stepplan_frozen():
    s = StepPlan(targets={}, wait_s=0.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.wait_s = 1.0  # type: ignore[misc]


# ── ParamSpec ─────────────────────────────────────────────────────────────────


def test_paramspec_happy_and_defaults():
    p = ParamSpec(type=float, default=1.0)
    assert p.unit == "" and p.description == ""
    assert p.min is None and p.max is None
    assert p.choices is None and p.structural is False and p.widget_hint is None


def test_paramspec_int_default_ok_for_float_type():
    p = ParamSpec(type=float, default=3)
    assert p.default == 3


def test_paramspec_bool_default_not_accepted_as_int():
    with pytest.raises(ValueError, match="ParamSpec.default"):
        ParamSpec(type=int, default=True)


def test_paramspec_bool_default_not_accepted_as_float():
    with pytest.raises(ValueError, match="ParamSpec.default"):
        ParamSpec(type=float, default=True)


def test_paramspec_bool_type_accepts_bool():
    p = ParamSpec(type=bool, default=True)
    assert p.default is True


def test_paramspec_default_wrong_type():
    with pytest.raises(ValueError, match="ParamSpec.default"):
        ParamSpec(type=int, default="five")


def test_paramspec_bad_type():
    with pytest.raises(TypeError, match="ParamSpec.type"):
        ParamSpec(type=list, default=[])


def test_paramspec_bounds_ok():
    p = ParamSpec(type=float, default=5.0, min=0.0, max=10.0)
    assert p.min == 0.0 and p.max == 10.0


def test_paramspec_bounds_reject_non_numeric_type():
    with pytest.raises(ValueError, match="ParamSpec.min/max"):
        ParamSpec(type=str, default="x", min=0.0)


def test_paramspec_default_below_min():
    with pytest.raises(ValueError, match="ParamSpec.default"):
        ParamSpec(type=float, default=-1.0, min=0.0)


def test_paramspec_default_above_max():
    with pytest.raises(ValueError, match="ParamSpec.default"):
        ParamSpec(type=float, default=11.0, max=10.0)


def test_paramspec_min_greater_than_max():
    with pytest.raises(ValueError, match="ParamSpec.min"):
        ParamSpec(type=float, default=5.0, min=10.0, max=0.0)


def test_paramspec_choices_ok():
    p = ParamSpec(type=int, default=2, choices={"low": 1, "high": 2})
    assert p.choices == {"low": 1, "high": 2}


def test_paramspec_choices_empty():
    with pytest.raises(ValueError, match="ParamSpec.choices"):
        ParamSpec(type=int, default=1, choices={})


def test_paramspec_choices_value_wrong_type():
    with pytest.raises(ValueError, match="ParamSpec.choices"):
        ParamSpec(type=int, default=1, choices={"a": 1, "b": "two"})


def test_paramspec_choices_default_not_among_values():
    with pytest.raises(ValueError, match="ParamSpec.default"):
        ParamSpec(type=int, default=9, choices={"a": 1, "b": 2})


def test_paramspec_choices_and_bounds_mutually_exclusive():
    with pytest.raises(ValueError, match="mutually exclusive"):
        ParamSpec(type=int, default=1, min=0, choices={"a": 1})


def test_paramspec_widget_hint_empty():
    with pytest.raises(ValueError, match="ParamSpec.widget_hint"):
        ParamSpec(type=float, default=1.0, widget_hint="")


def test_paramspec_structural_flag():
    p = ParamSpec(type=bool, default=False, structural=True)
    assert p.structural is True


def test_paramspec_frozen():
    p = ParamSpec(type=float, default=1.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.default = 2.0  # type: ignore[misc]


def test_paramspec_choices_defensive_copy():
    choices = {"a": 1, "b": 2}
    p = ParamSpec(type=int, default=1, choices=choices)
    choices["c"] = 3
    assert "c" not in p.choices


# ── ParamGroup ────────────────────────────────────────────────────────────────


def test_paramgroup_happy():
    g = ParamGroup(
        key="system", title="System", params={"field": ParamSpec(type=float, default=0.0)}
    )
    assert g.key == "system"
    assert "field" in g.params


def test_paramgroup_empty_key():
    with pytest.raises(ValueError, match="ParamGroup.key"):
        ParamGroup(key="", title="T", params={})


def test_paramgroup_empty_title():
    with pytest.raises(ValueError, match="ParamGroup.title"):
        ParamGroup(key="k", title="", params={})


def test_paramgroup_bad_param_value():
    with pytest.raises(TypeError, match="ParamGroup.params"):
        ParamGroup(key="k", title="T", params={"x": "not a spec"})


def test_paramgroup_empty_param_key():
    with pytest.raises(ValueError, match="ParamGroup.params"):
        ParamGroup(key="k", title="T", params={"": ParamSpec(type=int, default=0)})


def test_paramgroup_frozen():
    g = ParamGroup(key="k", title="T", params={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        g.title = "other"  # type: ignore[misc]


def test_paramgroup_params_defensive_copy():
    params = {"a": ParamSpec(type=int, default=0)}
    g = ParamGroup(key="k", title="T", params=params)
    params["b"] = ParamSpec(type=int, default=1)
    assert "b" not in g.params


# ── DataSchema ────────────────────────────────────────────────────────────────


def test_dataschema_happy():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={"voltage_V": 10})
    assert s.sweep_columns == {"field_T": "float"}
    assert s.arrays == {"voltage_V": 10}


def test_dataschema_bad_dtype():
    with pytest.raises(ValueError, match="sweep_columns"):
        DataSchema(sweep_columns={"field_T": "complex"}, arrays={})


def test_dataschema_array_length_must_be_positive():
    with pytest.raises(ValueError, match="arrays"):
        DataSchema(sweep_columns={}, arrays={"v": 0})


def test_dataschema_array_length_bool_rejected():
    with pytest.raises(TypeError, match="arrays"):
        DataSchema(sweep_columns={}, arrays={"v": True})


def test_dataschema_empty_column_name():
    with pytest.raises(ValueError, match="sweep_columns"):
        DataSchema(sweep_columns={"": "float"}, arrays={})


def test_dataschema_frozen():
    s = DataSchema(sweep_columns={}, arrays={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.arrays = {}  # type: ignore[misc]


def test_dataschema_defensive_copy():
    arrays = {"v": 10}
    s = DataSchema(sweep_columns={}, arrays=arrays)
    arrays["w"] = 5
    assert "w" not in s.arrays


# ── DataSchema.multiplexed ────────────────────────────────────────────────────


def test_multiplexed_expands_and_orders_deterministically():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={"voltage_V": 10})
    out = s.multiplexed(["Mux-Ch1", "Mux-Ch2"])
    assert list(out.arrays.keys()) == ["voltage_V__Mux-Ch1", "voltage_V__Mux-Ch2"]
    assert out.arrays["voltage_V__Mux-Ch1"] == 10
    assert out.arrays["voltage_V__Mux-Ch2"] == 10
    # sweep_columns unchanged
    assert out.sweep_columns == {"field_T": "float"}


def test_multiplexed_arrays_outer_routes_inner():
    s = DataSchema(sweep_columns={}, arrays={"a": 2, "b": 3})
    out = s.multiplexed(["r1", "r2"])
    assert list(out.arrays.keys()) == ["a__r1", "a__r2", "b__r1", "b__r2"]


def test_multiplexed_empty_routes():
    s = DataSchema(sweep_columns={}, arrays={"a": 1})
    with pytest.raises(ValueError, match="at least one route"):
        s.multiplexed([])


def test_multiplexed_route_with_separator():
    s = DataSchema(sweep_columns={}, arrays={"a": 1})
    with pytest.raises(ValueError, match="a__b"):
        s.multiplexed(["a__b"])


def test_multiplexed_route_with_slash():
    s = DataSchema(sweep_columns={}, arrays={"a": 1})
    with pytest.raises(ValueError, match="a/b"):
        s.multiplexed(["a/b"])


def test_multiplexed_duplicate_route():
    s = DataSchema(sweep_columns={}, arrays={"a": 1})
    with pytest.raises(ValueError, match="duplicated"):
        s.multiplexed(["r1", "r1"])


def test_multiplexed_scalar_columns_expanded_per_route():
    """Named scalar columns are expanded per route; the original is removed."""
    s = DataSchema(
        sweep_columns={"field_T": "float", "n_valid": "int"},
        arrays={"voltage_V": 10},
    )
    out = s.multiplexed(["Mux-Ch1", "Mux-Ch2"], scalar_columns=["n_valid"])
    # Arrays expanded as before.
    assert list(out.arrays.keys()) == ["voltage_V__Mux-Ch1", "voltage_V__Mux-Ch2"]
    # n_valid expanded per route, dtype preserved, original removed; the
    # unnamed field_T column passes through unchanged and keeps its position.
    assert out.sweep_columns == {
        "field_T": "float",
        "n_valid__Mux-Ch1": "int",
        "n_valid__Mux-Ch2": "int",
    }
    assert "n_valid" not in out.sweep_columns


def test_multiplexed_unknown_scalar_column_rejected():
    """A scalar_columns name absent from sweep_columns raises ValueError."""
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={"a": 1})
    with pytest.raises(ValueError, match="nope"):
        s.multiplexed(["r1", "r2"], scalar_columns=["nope"])


def test_multiplexed_default_scalar_columns_unchanged():
    """The default (no scalar_columns) leaves sweep_columns byte-identical."""
    s = DataSchema(
        sweep_columns={"field_T": "float", "n_valid": "int"},
        arrays={"voltage_V": 3},
    )
    out = s.multiplexed(["r1", "r2"])
    assert out.sweep_columns == {"field_T": "float", "n_valid": "int"}
    assert list(out.arrays.keys()) == ["voltage_V__r1", "voltage_V__r2"]


# ── DataSchema.validate ───────────────────────────────────────────────────────


def test_validate_passes():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={"voltage_V": 3})
    assert s.validate({"field_T": 1.0, "voltage_V": [0.1, 0.2, 0.3]}) is None


def test_validate_int_dtype_accepts_int():
    s = DataSchema(sweep_columns={"n": "int"}, arrays={})
    assert s.validate({"n": 5}) is None


def test_validate_missing_key():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={})
    with pytest.raises(DataSchemaError, match="missing declared key 'field_T'"):
        s.validate({})


def test_validate_extra_key():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={})
    with pytest.raises(DataSchemaError, match="extra undeclared key 'junk'"):
        s.validate({"field_T": 1.0, "junk": 5})


def test_validate_wrong_array_length():
    s = DataSchema(sweep_columns={}, arrays={"voltage_V": 3})
    with pytest.raises(DataSchemaError, match="voltage_V"):
        s.validate({"voltage_V": [1, 2]})


def test_validate_array_no_length():
    s = DataSchema(sweep_columns={}, arrays={"voltage_V": 3})
    with pytest.raises(DataSchemaError, match="no length"):
        s.validate({"voltage_V": 42})


def test_validate_wrong_scalar_type():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={})
    with pytest.raises(DataSchemaError, match="field_T"):
        s.validate({"field_T": "high"})


def test_validate_bool_scalar_rejected():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={})
    with pytest.raises(DataSchemaError, match="field_T"):
        s.validate({"field_T": True})


def test_validate_int_dtype_rejects_float():
    s = DataSchema(sweep_columns={"n": "int"}, arrays={})
    with pytest.raises(DataSchemaError, match="not an int"):
        s.validate({"n": 5.0})


def test_validate_reports_multiple_problems_together():
    s = DataSchema(sweep_columns={"field_T": "float"}, arrays={"voltage_V": 3})
    with pytest.raises(DataSchemaError) as excinfo:
        s.validate({"field_T": "bad", "voltage_V": [1, 2], "junk": 1})
    msg = str(excinfo.value)
    assert "field_T" in msg
    assert "voltage_V" in msg
    assert "junk" in msg


# ── EnvelopeBound / SessionEnvelope ──────────────────────────────────────────

class TestEnvelopeBound:
    def test_requires_at_least_one_bound(self):
        with pytest.raises(ValueError):
            EnvelopeBound()

    def test_rejects_min_above_max(self):
        with pytest.raises(ValueError):
            EnvelopeBound(min_value=2.0, max_value=1.0)

    def test_rejects_non_numeric_and_non_finite(self):
        with pytest.raises(TypeError):
            EnvelopeBound(max_value=True)
        with pytest.raises(ValueError):
            EnvelopeBound(max_value=float("inf"))

    def test_violation_messages_and_pass(self):
        bound = EnvelopeBound(min_value=-0.5, max_value=0.5)
        assert bound.violation(0.0) is None
        assert "below the session minimum" in bound.violation(-1.0)
        assert "above the session maximum" in bound.violation(1.0)
        # Non-numeric state values can never trip a numeric envelope.
        assert bound.violation("HOLDING") is None


class TestSessionEnvelope:
    def test_rejects_empty_bounds(self):
        with pytest.raises(ValueError):
            SessionEnvelope(bounds={})

    def test_rejects_wrong_value_type(self):
        with pytest.raises(TypeError):
            SessionEnvelope(bounds={"magnet_z": (0.0, 1.0)})

    def test_check_target(self):
        env = SessionEnvelope(
            bounds={"magnet_z": EnvelopeBound(min_value=-2.0, max_value=2.0)}
        )
        assert env.check_target("magnet_z", 1.0) is None
        assert env.check_target("other_vi", 99.0) is None  # unbounded VI
        message = env.check_target("magnet_z", 3.0)
        assert "session envelope" in message and "magnet_z" in message

    def test_check_state_uses_state_key_and_skips_missing(self):
        env = SessionEnvelope(
            bounds={
                "temperature_sample": EnvelopeBound(
                    min_value=4.0, state_key="temperature"
                ),
                "magnet_z": EnvelopeBound(max_value=2.0),  # no state_key: skipped
            }
        )
        violations = env.check_state(
            {"temperature_sample": {"temperature": 2.0}, "magnet_z": {"get_field": 9.0}}
        )
        assert len(violations) == 1
        assert "temperature_sample" in violations[0]
        # VI or key absent from the snapshot -> staleness, not a violation.
        assert env.check_state({}) == []
        assert env.check_state({"temperature_sample": {}}) == []
