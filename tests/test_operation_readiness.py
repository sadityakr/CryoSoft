# ---
# description: |
#   Behavior tests for the readiness/next-due contract added to OperationBase
#   in Phase 6 (docs/plans/cryogenics-logbook.md §12): the ReadinessCondition/
#   NextDue dataclasses, OperationBase's defaults, HeliumFillOperation's
#   zero_field checklist row + next_due() prediction math, and
#   SampleChangeOperation's four checklist rows (including heater_off
#   presence/absence and the needle_valve_confirmed/confirmed() flow). Qt-free
#   — no Orchestrator ticking, just direct calls against synthetic state
#   snapshots and context dicts, mirroring how OperationCard drives them.
# last_updated: 2026-07-19
# ---

from __future__ import annotations

import pytest

from cryosoft.core.operation import NextDue, OperationBase, ReadinessCondition
from cryosoft.core.station import build_station
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
from cryosoft.procedures.operations.sample_change import SampleChangeOperation

CONFIG_PATH = "cryosoft/configs/sim_cryostat"


@pytest.fixture
def station():
    return build_station(CONFIG_PATH)


# ── Dataclasses ─────────────────────────────────────────────────────────────


def test_readiness_condition_is_frozen_and_carries_check_and_detail():
    condition = ReadinessCondition(
        key="zero_field",
        label="All magnets at zero field",
        check=lambda state: True,
        detail=lambda state: "ok",
    )
    assert condition.key == "zero_field"
    assert condition.label == "All magnets at zero field"
    assert condition.check({}) is True
    assert condition.detail({}) == "ok"
    with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
        condition.key = "other"


def test_readiness_condition_detail_defaults_to_none():
    condition = ReadinessCondition(key="k", label="L", check=lambda state: True)
    assert condition.detail is None


def test_next_due_is_frozen_and_carries_due_unix_and_text():
    due = NextDue(due_unix=123.0, text="Fill due in ~1.0 h")
    assert due.due_unix == 123.0
    assert due.text == "Fill due in ~1.0 h"
    with pytest.raises(Exception):
        due.text = "other"


def test_next_due_allows_none_due_unix():
    due = NextDue(due_unix=None, text="Fill due: consumption unknown")
    assert due.due_unix is None


# ── OperationBase defaults ───────────────────────────────────────────────────


class _MinimalOperation(OperationBase):
    """A bare OperationBase subclass exercising only the base defaults."""

    name = "Minimal Operation"


def test_operation_base_readiness_next_due_defaults():
    op = _MinimalOperation()
    assert op.readiness_conditions() == ()
    assert op.next_due({"state": {}, "now_unix": 0.0, "consumption_rate_pct_per_h": None}) is None


def test_operation_base_ready_message_and_config_key_default_empty():
    assert OperationBase.ready_message == ""
    assert OperationBase.config_key == ""
    assert _MinimalOperation.ready_message == ""
    assert _MinimalOperation.config_key == ""


# ── HeliumFillOperation: readiness (zero_field) ──────────────────────────────


def test_helium_fill_readiness_conditions_empty_when_no_magnets():
    """A station with no registered magnets never gets a zero_field row."""

    class _NoMagnetStation:
        def has_vi(self, name):
            return name == "level_meter"

        def magnet_vi_names(self):
            return []

    op = HeliumFillOperation(_NoMagnetStation())
    assert op.readiness_conditions() == ()


def test_helium_fill_readiness_zero_field_true(station):
    op = HeliumFillOperation(station)
    (condition,) = op.readiness_conditions()
    assert condition.key == "zero_field"
    state = {name: {"get_field": 0.0} for name in station.magnet_vi_names()}
    assert condition.check(state) is True


def test_helium_fill_readiness_zero_field_false_names_worst_offender(station):
    op = HeliumFillOperation(station)
    (condition,) = op.readiness_conditions()
    magnets = station.magnet_vi_names()
    state = {name: {"get_field": 0.0} for name in magnets}
    state[magnets[0]] = {"get_field": 1.5}
    assert condition.check(state) is False
    assert condition.detail(state) == f"{magnets[0]} at 1.50 T"


def test_helium_fill_readiness_zero_field_worst_offender_is_largest_magnitude(station):
    op = HeliumFillOperation(station)
    (condition,) = op.readiness_conditions()
    magnets = station.magnet_vi_names()
    assert len(magnets) >= 2
    state = {magnets[0]: {"get_field": 0.2}, magnets[1]: {"get_field": -0.9}}
    assert condition.detail(state) == f"{magnets[1]} at -0.90 T"


def test_helium_fill_readiness_zero_field_missing_reading_fails_with_detail(station):
    op = HeliumFillOperation(station)
    (condition,) = op.readiness_conditions()
    magnets = station.magnet_vi_names()
    state = {name: {} for name in magnets}  # no get_field key at all
    assert condition.check(state) is False
    assert "unavailable" in condition.detail(state)


# ── HeliumFillOperation: next_due() ──────────────────────────────────────────


def _ctx(level=None, rate=None, now_unix=1_000_000.0, level_vi="level_meter"):
    state = {} if level is None else {level_vi: {"helium_level": level}}
    return {"state": state, "now_unix": now_unix, "consumption_rate_pct_per_h": rate}


def test_next_due_falling_level_computes_correct_hours(station):
    op = HeliumFillOperation(station, helium_warning_pct=30.0)
    due = op.next_due(_ctx(level=50.0, rate=2.0))
    assert due is not None
    assert due.due_unix == pytest.approx(1_000_000.0 + 10.0 * 3600.0)
    assert "10.0 h" in due.text
    assert "level 50.0 %" in due.text
    assert "warning at 30.0 %" in due.text


def test_next_due_humanizes_to_days_above_24_hours(station):
    op = HeliumFillOperation(station, helium_warning_pct=30.0)
    due = op.next_due(_ctx(level=90.0, rate=1.0))  # 60 h -> 2.5 d
    assert due is not None
    assert "2.5 d" in due.text


def test_next_due_rate_none_is_consumption_unknown(station):
    op = HeliumFillOperation(station)
    due = op.next_due(_ctx(level=50.0, rate=None))
    assert due == NextDue(None, "Fill due: consumption unknown")


def test_next_due_level_missing_is_consumption_unknown(station):
    op = HeliumFillOperation(station)
    due = op.next_due(_ctx(level=None, rate=1.0))
    assert due == NextDue(None, "Fill due: consumption unknown")


def test_next_due_rate_not_positive_is_level_not_falling(station):
    op = HeliumFillOperation(station)
    for rate in (0.0, -1.0):
        due = op.next_due(_ctx(level=50.0, rate=rate))
        assert due == NextDue(None, "Fill due: level not falling")


def test_next_due_level_at_or_below_warning_is_overdue(station):
    op = HeliumFillOperation(station, helium_warning_pct=30.0)
    for level in (30.0, 10.0):
        due = op.next_due(_ctx(level=level, rate=1.0))
        assert due == NextDue(None, "Fill overdue (level below warning threshold)")


def test_next_due_reads_the_configured_level_vi(station):
    op = HeliumFillOperation(station, level_vi="level_meter", helium_warning_pct=30.0)
    ctx = _ctx(level=50.0, rate=2.0, level_vi="level_meter")
    assert op.next_due(ctx) is not None
    # A snapshot for a different VI name never resolves a level.
    ctx_wrong_vi = _ctx(level=50.0, rate=2.0, level_vi="some_other_vi")
    assert op.next_due(ctx_wrong_vi) == NextDue(None, "Fill due: consumption unknown")


# ── SampleChangeOperation: readiness (four rows) ─────────────────────────────


def test_sample_change_readiness_conditions_keys(station):
    op = SampleChangeOperation(station)
    keys = [c.key for c in op.readiness_conditions()]
    assert keys == ["zero_field", "heater_off", "vti_at_target", "needle_valve_confirmed"]


def test_sample_change_zero_field_true_and_false(station):
    op = SampleChangeOperation(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    magnets = station.magnet_vi_names()

    zero_state = {name: {"get_field": 0.0} for name in magnets}
    assert conditions["zero_field"].check(zero_state) is True

    nonzero_state = dict(zero_state)
    nonzero_state[magnets[0]] = {"get_field": 2.0}
    assert conditions["zero_field"].check(nonzero_state) is False
    assert conditions["zero_field"].detail(nonzero_state) == f"{magnets[0]} at 2.00 T"


def test_sample_change_heater_off_absent_from_snapshot_holds_vacuously(station):
    """No magnet in the live snapshot exposes switch_heater_state -> holds trivially."""
    op = SampleChangeOperation(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    magnets = station.magnet_vi_names()
    state = {name: {"get_field": 0.0} for name in magnets}  # no switch_heater_state key
    assert conditions["heater_off"].check(state) is True
    assert "no switch heater" in conditions["heater_off"].detail(state)


def test_sample_change_heater_off_present_true_and_false(station):
    op = SampleChangeOperation(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    magnets = station.magnet_vi_names()

    off_state = {name: {"switch_heater_state": "OFF"} for name in magnets}
    assert conditions["heater_off"].check(off_state) is True

    on_state = dict(off_state)
    on_state[magnets[0]] = {"switch_heater_state": "ON"}
    assert conditions["heater_off"].check(on_state) is False
    assert magnets[0] in conditions["heater_off"].detail(on_state)


def test_sample_change_vti_at_target_true_and_false_with_detail(station):
    op = SampleChangeOperation(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    vti_name = "temperature_vti"

    at_target = {vti_name: {"temperature": 300.0}}
    assert conditions["vti_at_target"].check(at_target) is True
    assert conditions["vti_at_target"].detail(at_target) == "currently 300.0 K"

    off_target = {vti_name: {"temperature": 250.3}}
    assert conditions["vti_at_target"].check(off_target) is False
    assert conditions["vti_at_target"].detail(off_target) == "currently 250.3 K"


def test_sample_change_needle_valve_confirmed_uses_confirmed_and_ignores_snapshot(station):
    op = SampleChangeOperation(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    condition = conditions["needle_valve_confirmed"]
    assert condition.detail is None

    assert condition.check({"anything": "irrelevant"}) is False
    op.confirm("needle_valve")
    assert condition.check({"anything": "irrelevant"}) is True


def test_sample_change_config_key_and_ready_message():
    assert SampleChangeOperation.config_key == "sample_change"
    assert SampleChangeOperation.ready_message
    assert HeliumFillOperation.ready_message
    assert HeliumFillOperation.config_key == ""  # wired via cryogenics_config, not config_key
