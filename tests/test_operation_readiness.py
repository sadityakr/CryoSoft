# ---
# description: |
#   Behavior tests for the readiness/next-due contract added to OperationBase
#   in Phase 6: the ReadinessCondition/
#   NextDue dataclasses, OperationBase's defaults, HeliumFillOperation's
#   zero_field checklist row + next_due() prediction math, and the shared
#   _SampleAccessOperationBase's three checklist rows (zero_field/
#   vti_at_target/needle_valve_confirmed, the last via the confirmed()/
#   confirm() flow) exercised through both concrete subclasses,
#   SampleLoadOperation and SampleUnloadOperation, which share this behavior
#   entirely (parametrized rather than duplicated). Qt-free — no
#   Orchestrator ticking, just direct calls against synthetic state
#   snapshots and context dicts, mirroring how OperationCard drives them.
# last_updated: 2026-07-27
# ---

from __future__ import annotations

import pytest

from cryosoft.core.operation import NextDue, OperationBase, ReadinessCondition
from cryosoft.core.station import build_station
from cryosoft.procedures.operations.helium_fill import HeliumFillOperation
from cryosoft.procedures.operations.sample_load import SampleLoadOperation
from cryosoft.procedures.operations.sample_unload import SampleUnloadOperation

CONFIG_PATH = "cryosoft/configs/sim_cryostat"
_SAMPLE_ACCESS_CLASSES = [SampleLoadOperation, SampleUnloadOperation]
_SAMPLE_ACCESS_IDS = ["load", "unload"]


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
    state = {name: {"magnet_state": "standby"} for name in station.magnet_vi_names()}
    assert condition.check(state) is True


def test_helium_fill_readiness_zero_field_false_names_first_offender(station):
    op = HeliumFillOperation(station)
    (condition,) = op.readiness_conditions()
    magnets = station.magnet_vi_names()
    state = {name: {"magnet_state": "standby"} for name in magnets}
    state[magnets[0]] = {"magnet_state": "holding"}
    assert condition.check(state) is False
    assert condition.detail(state) == f"{magnets[0]} holding"


def test_helium_fill_readiness_zero_field_missing_reading_fails_with_detail(station):
    op = HeliumFillOperation(station)
    (condition,) = op.readiness_conditions()
    magnets = station.magnet_vi_names()
    state = {name: {} for name in magnets}  # no magnet_state key at all
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


# ── _SampleAccessOperationBase (via SampleLoadOperation/SampleUnloadOperation):
# readiness (three rows) ──────────────────────────────────────────────────────


@pytest.mark.parametrize("op_cls", _SAMPLE_ACCESS_CLASSES, ids=_SAMPLE_ACCESS_IDS)
def test_sample_access_readiness_conditions_keys(op_cls, station):
    """The checklist is the magnet check, then the step sequence in order."""
    op = op_cls(station)
    keys = [c.key for c in op.readiness_conditions()]
    assert keys == ["zero_field"] + [s.key for s in op.steps()]
    assert keys == [
        "zero_field",
        "warm_vti",
        "close_needle_valve",
        "open_access_valve",
        "move_rod",
        "close_access_valve",
        "flush",
    ]


@pytest.mark.parametrize("op_cls", _SAMPLE_ACCESS_CLASSES, ids=_SAMPLE_ACCESS_IDS)
def test_sample_access_zero_field_true_and_false(op_cls, station):
    op = op_cls(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    magnets = station.magnet_vi_names()

    zero_state = {name: {"magnet_state": "standby"} for name in magnets}
    assert conditions["zero_field"].check(zero_state) is True

    nonzero_state = dict(zero_state)
    nonzero_state[magnets[0]] = {"magnet_state": "holding"}
    assert conditions["zero_field"].check(nonzero_state) is False
    assert conditions["zero_field"].detail(nonzero_state) == f"{magnets[0]} holding"


@pytest.mark.parametrize("op_cls", _SAMPLE_ACCESS_CLASSES, ids=_SAMPLE_ACCESS_IDS)
def test_sample_access_warm_step_row_shows_the_live_temperature(op_cls, station):
    """A pending step's detail is live; a recorded one reports its outcome."""
    op = op_cls(station)  # default target_temperature_K = 290.0
    conditions = {c.key: c for c in op.readiness_conditions()}
    row = conditions["warm_vti"]
    vti_name = "temperature_vti"

    at_target = {vti_name: {"temperature": 290.0}}
    off_target = {vti_name: {"temperature": 250.3}}

    # Pending: the row is not yet met, and shows where the VTI actually is.
    assert row.check(at_target) is False
    assert row.detail(at_target) == "currently 290.0 K"
    assert row.detail(off_target) == "currently 250.3 K"

    # Recorded done: met, regardless of what the snapshot says.
    op.confirm("warm_vti")
    assert row.check(off_target) is True
    assert row.detail(off_target) == "done"


@pytest.mark.parametrize("op_cls", _SAMPLE_ACCESS_CLASSES, ids=_SAMPLE_ACCESS_IDS)
def test_sample_access_step_rows_read_outcomes_not_the_snapshot(op_cls, station):
    """An operator-ack row depends on the record alone — no hardware can verify it."""
    op = op_cls(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    row = conditions["close_needle_valve"]

    assert row.check({"anything": "irrelevant"}) is False
    op.confirm("close_needle_valve")
    assert row.check({"anything": "irrelevant"}) is True


@pytest.mark.parametrize("op_cls", _SAMPLE_ACCESS_CLASSES, ids=_SAMPLE_ACCESS_IDS)
def test_sample_access_skipped_step_row_stays_unmet_and_says_so(op_cls, station):
    """A skip must never read as done — the panel has to show the override."""
    op = op_cls(station)
    conditions = {c.key: c for c in op.readiness_conditions()}
    row = conditions["flush"]

    op.skip_step("flush")
    assert row.check({}) is False
    assert row.detail({}) == "skipped by operator"


def test_sample_access_config_key_and_ready_message():
    assert SampleLoadOperation.config_key == "sample_load"
    assert SampleUnloadOperation.config_key == "sample_unload"
    assert SampleLoadOperation.config_key != SampleUnloadOperation.config_key
    assert SampleLoadOperation.ready_message
    assert SampleUnloadOperation.ready_message
    assert HeliumFillOperation.ready_message
    assert HeliumFillOperation.config_key == ""  # wired via cryogenics_config, not config_key
