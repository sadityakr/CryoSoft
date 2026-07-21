# ---
# description: |
#   Behavior tests for the Servicing Log framework (cryosoft/session/servicing_log.py,
#   Phase 1 of docs/plans/cryogenics-logbook.md): LogKindSpec validation,
#   ServicingLogStore's entry-revision model (add/revise/delete round-trips,
#   tombstone hiding, write validation, tolerant loads, non-editable-kind
#   refusal), HeliumRecordStore (decimation via the recorder, rotation),
#   consumption_rate_pct_per_h (fit + fill-interval exclusion + sign
#   convention), and CryogenicsRecorder end-to-end against synthetic
#   states_updated/run_started/run_finished payloads (no real Orchestrator).
# last_updated: 2026-07-19
# ---

import json
import logging
import time

import pytest

from cryosoft.core.plan import ParamSpec
from cryosoft.session.servicing_log import (
    DECLARED_LOG_KINDS,
    CryogenicsRecorder,
    HeliumRecordStore,
    LogKindSpec,
    ServicingLogStore,
    consumption_rate_pct_per_h,
)

CONFIG_NAME = "sim_cryostat"


@pytest.fixture
def servicing_store(tmp_path):
    return ServicingLogStore(tmp_path / "servicing", CONFIG_NAME)


@pytest.fixture
def helium_store(tmp_path):
    return HeliumRecordStore(tmp_path / "servicing", CONFIG_NAME)


# ── LogKindSpec validation ──────────────────────────────────────────────────


def test_declared_cryogenics_kind_shape():
    spec = DECLARED_LOG_KINDS["cryogenics"]
    assert spec.editable is True
    assert list(spec.fields) == [
        "person",
        "start_utc",
        "end_utc",
        "helium_start_pct",
        "helium_end_pct",
        "ln2_filled",
        "notes",
        "level_curve",
    ]
    assert spec.fields["helium_start_pct"].type is float
    assert spec.fields["ln2_filled"].default is False
    assert spec.fields["level_curve"].type is str
    assert spec.fields["level_curve"].default == ""


def test_declared_operations_kind_is_not_editable():
    spec = DECLARED_LOG_KINDS["operations"]
    assert spec.editable is False
    assert set(spec.fields) == {
        "operation",
        "params",
        "started_utc",
        "finished_utc",
        "status",
        "verified",
        "reason",
    }


def test_log_kind_spec_rejects_bad_key():
    with pytest.raises(ValueError, match="lowercase identifier"):
        LogKindSpec(key="Not Valid", title="X", fields={"a": ParamSpec(type=str, default="")})
    with pytest.raises(ValueError, match="lowercase identifier"):
        LogKindSpec(key="", title="X", fields={"a": ParamSpec(type=str, default="")})


def test_log_kind_spec_rejects_empty_title():
    with pytest.raises(ValueError, match="title"):
        LogKindSpec(key="x", title="", fields={"a": ParamSpec(type=str, default="")})


def test_log_kind_spec_rejects_empty_fields():
    with pytest.raises(ValueError, match="fields"):
        LogKindSpec(key="x", title="X", fields={})


def test_log_kind_spec_rejects_non_paramspec_field():
    with pytest.raises(TypeError, match="ParamSpec"):
        LogKindSpec(key="x", title="X", fields={"a": "not a paramspec"})


def test_log_kind_spec_fields_defensively_copied():
    fields = {"a": ParamSpec(type=str, default="")}
    spec = LogKindSpec(key="x", title="X", fields=fields)
    fields["b"] = ParamSpec(type=int, default=0)
    assert "b" not in spec.fields


# ── ServicingLogStore: add / revise / delete round-trip ────────────────────


def _fill_values(**overrides):
    values = {
        "person": "jdoe",
        "start_utc": "2026-07-19T10:00:00+00:00",
        "end_utc": "2026-07-19T11:00:00+00:00",
        "helium_start_pct": 40.0,
        "helium_end_pct": 90.0,
        "ln2_filled": False,
        "notes": "",
    }
    values.update(overrides)
    return values


def test_add_entry_round_trip(servicing_store):
    entry = servicing_store.add_entry("cryogenics", _fill_values())
    assert entry.revision == 1
    assert entry.source == "manual"
    assert entry.entry_id
    assert entry.values["person"] == "jdoe"
    assert entry.values["helium_start_pct"] == 40.0

    fetched = servicing_store.entries("cryogenics")
    assert len(fetched) == 1
    assert fetched[0] == entry


def test_add_entry_fills_missing_fields_with_defaults(servicing_store):
    entry = servicing_store.add_entry("cryogenics", {"person": "asmith"})
    assert entry.values["notes"] == ""
    assert entry.values["ln2_filled"] is False
    assert entry.values["helium_start_pct"] == 0.0


def test_add_entry_person_kwarg_folds_into_values(servicing_store):
    entry = servicing_store.add_entry("cryogenics", {}, person="operator-1")
    assert entry.values["person"] == "operator-1"

    # Explicit values["person"] wins over the kwarg.
    entry2 = servicing_store.add_entry(
        "cryogenics", {"person": "explicit"}, person="operator-1"
    )
    assert entry2.values["person"] == "explicit"


def test_revise_entry_preserves_history_and_updates_latest(servicing_store):
    original = servicing_store.add_entry("cryogenics", _fill_values(notes="typo"))
    revised = servicing_store.revise_entry(
        "cryogenics", original.entry_id, {"notes": "corrected"}, revised_by="tech1"
    )

    assert revised.entry_id == original.entry_id
    assert revised.revision == 2
    assert revised.values["notes"] == "corrected"
    # Untouched fields carry forward from the previous revision.
    assert revised.values["person"] == "jdoe"
    assert revised.revised_by == "tech1"
    assert revised.created_utc == original.created_utc

    latest = servicing_store.entries("cryogenics")
    assert len(latest) == 1
    assert latest[0].values["notes"] == "corrected"

    history = servicing_store.revisions("cryogenics", original.entry_id)
    assert [e.revision for e in history] == [1, 2]
    assert history[0].values["notes"] == "typo"
    assert history[1].values["notes"] == "corrected"


def test_delete_entry_tombstones_and_hides_from_entries(servicing_store):
    entry = servicing_store.add_entry("cryogenics", _fill_values())
    servicing_store.add_entry("cryogenics", _fill_values(person="other"))
    assert len(servicing_store.entries("cryogenics")) == 2

    tombstone = servicing_store.delete_entry("cryogenics", entry.entry_id, revised_by="tech1")
    assert tombstone.deleted is True
    assert tombstone.revision == 2

    remaining = servicing_store.entries("cryogenics")
    assert len(remaining) == 1
    assert remaining[0].values["person"] == "other"

    # Full history is still inspectable.
    history = servicing_store.revisions("cryogenics", entry.entry_id)
    assert len(history) == 2
    assert history[-1].deleted is True


def test_entries_newest_first_by_created_time(servicing_store):
    first = servicing_store.add_entry("cryogenics", _fill_values(notes="first"))
    second = servicing_store.add_entry("cryogenics", _fill_values(notes="second"))
    # Revising the first entry must not change its created_utc / ordering.
    servicing_store.revise_entry(
        "cryogenics", first.entry_id, {"notes": "first-edited"}, revised_by="tech"
    )

    entries = servicing_store.entries("cryogenics")
    assert [e.values["notes"] for e in entries] == ["second", "first-edited"]
    assert entries[0].entry_id == second.entry_id


def test_revise_unknown_entry_raises(servicing_store):
    with pytest.raises(ValueError, match="no entry"):
        servicing_store.revise_entry("cryogenics", "nope", {}, revised_by="tech")


def test_delete_unknown_entry_raises(servicing_store):
    with pytest.raises(ValueError, match="no entry"):
        servicing_store.delete_entry("cryogenics", "nope", revised_by="tech")


# ── Write validation ─────────────────────────────────────────────────────────


def test_add_entry_rejects_unknown_field(servicing_store):
    with pytest.raises(ValueError, match="no field"):
        servicing_store.add_entry("cryogenics", {"not_a_field": 1})


def test_add_entry_rejects_wrong_type(servicing_store):
    with pytest.raises(ValueError):
        servicing_store.add_entry("cryogenics", {"helium_start_pct": "not a number"})
    with pytest.raises(ValueError):
        servicing_store.add_entry("cryogenics", {"ln2_filled": "yes"})


def test_add_entry_unknown_kind_raises(servicing_store):
    with pytest.raises(ValueError, match="unknown log kind"):
        servicing_store.add_entry("bogus_kind", {})


# ── Tolerant loads ───────────────────────────────────────────────────────────


def test_entries_tolerates_corrupt_line(servicing_store, caplog):
    good = servicing_store.add_entry("cryogenics", _fill_values())
    path = servicing_store._path("cryogenics")
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")

    with caplog.at_level(logging.WARNING):
        entries = servicing_store.entries("cryogenics")
    assert len(entries) == 1
    assert entries[0].entry_id == good.entry_id
    assert any("corrupt" in record.message for record in caplog.records)


# ── Non-editable kind refusal ────────────────────────────────────────────────


def test_operations_kind_refuses_manual_writes(servicing_store):
    with pytest.raises(ValueError, match="not editable"):
        servicing_store.add_entry("operations", {"operation": "Helium Fill"})
    entry = servicing_store.add_entry("cryogenics", _fill_values())
    with pytest.raises(ValueError, match="not editable"):
        servicing_store.revise_entry("operations", entry.entry_id, {}, revised_by="x")
    with pytest.raises(ValueError, match="not editable"):
        servicing_store.delete_entry("operations", entry.entry_id, revised_by="x")


def test_append_machine_entry_works_on_non_editable_kind(servicing_store):
    entry = servicing_store.append_machine_entry(
        "operations",
        {
            "operation": "Helium Fill",
            "started_utc": "2026-07-19T10:00:00+00:00",
            "finished_utc": "2026-07-19T11:00:00+00:00",
            "status": "done",
            "verified": True,
        },
    )
    assert entry.source == "machine"
    assert entry.values["operation"] == "Helium Fill"
    assert entry.values["params"] == "{}"  # default, not overridden
    fetched = servicing_store.entries("operations")
    assert len(fetched) == 1
    assert fetched[0] == entry


# ── HeliumRecordStore ────────────────────────────────────────────────────────


def test_helium_record_append_and_samples(helium_store):
    helium_store.append("2026-07-19T10:00:00+00:00", 50.0, 80.0)
    helium_store.append("2026-07-19T11:00:00+00:00", 45.0, 79.0)
    samples = helium_store.samples()
    assert len(samples) == 2
    assert samples[0][0] < samples[1][0]  # ascending by time
    assert samples[0][1] == 50.0
    assert samples[1][1] == 45.0


def test_helium_record_samples_since_filter(helium_store):
    helium_store.append("2026-07-19T10:00:00+00:00", 50.0, 80.0)
    helium_store.append("2026-07-19T12:00:00+00:00", 40.0, 78.0)
    samples = helium_store.samples(since_utc="2026-07-19T11:00:00+00:00")
    assert len(samples) == 1
    assert samples[0][1] == 40.0


def test_helium_record_tolerates_corrupt_line(helium_store, caplog):
    helium_store.append("2026-07-19T10:00:00+00:00", 50.0, 80.0)
    with helium_store.path.open("a", encoding="utf-8") as f:
        f.write("garbage\n")
    with caplog.at_level(logging.WARNING):
        samples = helium_store.samples()
    assert len(samples) == 1


def test_helium_record_rejects_bad_types(helium_store):
    with pytest.raises(TypeError):
        helium_store.append("2026-07-19T10:00:00+00:00", True, 80.0)
    with pytest.raises(TypeError):
        helium_store.append("", 50.0, 80.0)


def test_helium_record_rotation(helium_store, monkeypatch):
    import cryosoft.session.servicing_log as servicing_log_module

    monkeypatch.setattr(servicing_log_module, "_ROTATION_BYTES", 500)
    for i in range(50):
        helium_store.append(f"2026-07-19T10:{i:02d}:00+00:00", float(i), 80.0)

    size = helium_store.path.stat().st_size
    assert size <= 500 * 2  # rotation kicked in at least once, file stays bounded

    samples = helium_store.samples()
    # The newest samples must survive rotation; the oldest ones are gone.
    values = [s[1] for s in samples]
    assert values[-1] == 49.0
    assert len(values) < 50
    assert not list(helium_store.path.parent.glob("*.tmp"))


# ── consumption_rate_pct_per_h ───────────────────────────────────────────────


def test_consumption_rate_detects_falling_level():
    # Falling 1%/hour over 4 hours -> positive consumption rate.
    now = 4 * 3600.0
    samples = [(float(h) * 3600.0, 100.0 - h, 80.0) for h in range(5)]
    rate = consumption_rate_pct_per_h(samples, window_s=4 * 3600.0, now_unix=now)
    assert rate == pytest.approx(1.0, abs=1e-6)


def test_consumption_rate_negative_when_level_rises():
    now = 4 * 3600.0
    samples = [(float(h) * 3600.0, 50.0 + h, 80.0) for h in range(5)]
    rate = consumption_rate_pct_per_h(samples, window_s=4 * 3600.0, now_unix=now)
    assert rate == pytest.approx(-1.0, abs=1e-6)


def test_consumption_rate_none_with_fewer_than_two_points():
    assert consumption_rate_pct_per_h([], window_s=3600.0, now_unix=0.0) is None
    assert consumption_rate_pct_per_h([(0.0, 50.0, 80.0)], window_s=3600.0, now_unix=0.0) is None


def test_consumption_rate_excludes_fill_interval():
    # A steady -2%/h consumption trend, with one anomalous topped-up reading
    # in the middle (inside the declared fill interval) that would otherwise
    # distort the fit. Excluding the interval recovers the true trend.
    samples = [
        (0.0, 100.0, 80.0),
        (3600.0, 98.0, 80.0),
        (5400.0, 150.0, 80.0),  # spike inside the fill interval
        (7200.0, 96.0, 80.0),
        (10800.0, 94.0, 80.0),
        (14400.0, 92.0, 80.0),
    ]
    now = 14400.0
    rate_with_fill = consumption_rate_pct_per_h(samples, window_s=14400.0, now_unix=now)
    rate_excluding_fill = consumption_rate_pct_per_h(
        samples, window_s=14400.0, now_unix=now, fill_intervals=[(3600.0, 7200.0)]
    )
    assert rate_excluding_fill == pytest.approx(2.0, abs=1e-6)  # true underlying trend
    assert rate_with_fill != pytest.approx(rate_excluding_fill, abs=0.1)


def test_consumption_rate_window_excludes_old_samples():
    samples = [(0.0, 100.0, 80.0), (3600.0, 10.0, 80.0), (7200.0, 55.0, 80.0), (10800.0, 50.0, 80.0)]
    # Narrow window only sees the last two points (falling 5% over 1h).
    rate = consumption_rate_pct_per_h(samples, window_s=3600.0, now_unix=10800.0)
    assert rate == pytest.approx(5.0, abs=1e-6)


# ── CryogenicsRecorder ────────────────────────────────────────────────────────


LEVEL_VI = "level_meter"


@pytest.fixture
def recorder(helium_store, servicing_store, qtbot):
    return CryogenicsRecorder(
        helium_store,
        servicing_store,
        level_vi_name=LEVEL_VI,
        warning_pct=35.0,
        history_sample_s=3600.0,
        warning_clear_margin_pct=3.0,
        fill_operation_name="Helium Fill",
    )


def _state(helium_pct, nitrogen_pct=80.0):
    return {LEVEL_VI: {"helium_level": helium_pct, "nitrogen_level": nitrogen_pct}}


def test_recorder_headless_construction_and_call(recorder, helium_store):
    """The recorder works with plain method calls, no widgets, offscreen-safe."""
    recorder.on_states_updated(_state(50.0))
    assert len(helium_store.samples()) == 1


def test_recorder_decimates_helium_record(recorder, helium_store, monkeypatch):
    fake_time = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_time[0])

    recorder.on_states_updated(_state(50.0))  # first call always appends
    fake_time[0] += 1.0  # far short of the 3600s cadence
    recorder.on_states_updated(_state(49.0))
    fake_time[0] += 1.0
    recorder.on_states_updated(_state(48.0))

    assert len(helium_store.samples()) == 1

    fake_time[0] += 3600.0  # cadence elapsed
    recorder.on_states_updated(_state(47.0))
    assert len(helium_store.samples()) == 2


def test_recorder_warning_hysteresis(recorder):
    warnings = []
    recorder.cryo_warning.connect(warnings.append)

    recorder.on_states_updated(_state(50.0))  # above threshold, no warning
    assert not warnings

    recorder.on_states_updated(_state(30.0))  # below 35% -> warns once
    assert len(warnings) == 1

    recorder.on_states_updated(_state(29.0))  # still below -> no repeat
    assert len(warnings) == 1

    recorder.on_states_updated(_state(37.0))  # below clear margin (35+3=38) -> still armed low
    assert len(warnings) == 1

    recorder.on_states_updated(_state(39.0))  # above margin -> re-armed
    recorder.on_states_updated(_state(30.0))  # drops again -> warns a second time
    assert len(warnings) == 2


def test_recorder_ignores_malformed_state(recorder, helium_store):
    recorder.on_states_updated({})
    recorder.on_states_updated({LEVEL_VI: "not a dict"})
    recorder.on_states_updated({LEVEL_VI: {"helium_level": "not a number"}})
    recorder.on_states_updated(None)  # type: ignore[arg-type]
    assert helium_store.samples() == []


def test_recorder_records_operations_stream_on_finish(recorder, servicing_store):
    recorder.on_run_finished(
        {
            "kind": "operation",
            "procedure": "Sample Change",
            "run_id": "op1",
            "params": {"target_temperature_K": 300.0},
            "started_utc": "2026-07-19T09:00:00+00:00",
            "finished_utc": "2026-07-19T09:30:00+00:00",
            "status": "done",
            "reason": "",
        }
    )
    ops = servicing_store.entries("operations")
    assert len(ops) == 1
    assert ops[0].values["operation"] == "Sample Change"
    assert ops[0].values["verified"] is True
    assert "target_temperature_K" in ops[0].values["params"]


def test_recorder_writes_cryogenics_entry_for_fill(recorder, servicing_store):
    recorder.on_states_updated(_state(40.0))  # baseline level before the fill

    recorder.on_run_started(
        {
            "run_id": "fill1",
            "procedure": "Helium Fill",
            "kind": "operation",
            "started_utc": "2026-07-19T10:00:00+00:00",
        }
    )
    recorder.on_states_updated(_state(90.0))  # level after the fill completes

    recorder.on_run_finished(
        {
            "run_id": "fill1",
            "procedure": "Helium Fill",
            "kind": "operation",
            "params": {"person": "jdoe"},
            "started_utc": "2026-07-19T10:00:00+00:00",
            "finished_utc": "2026-07-19T10:45:00+00:00",
            "status": "done",
            "reason": "",
            "summary": {
                "level_curve": {"unix_time": [1.0, 2.0], "helium_pct": [40.0, 90.0]},
                "start_pct": 40.0,
                "end_pct": 90.0,
            },
        }
    )

    cryo_entries = servicing_store.entries("cryogenics")
    assert len(cryo_entries) == 1
    entry = cryo_entries[0]
    assert entry.source == "operation"
    assert entry.run_id == "fill1"
    assert entry.values["person"] == "jdoe"
    assert entry.values["helium_start_pct"] == 40.0
    assert entry.values["helium_end_pct"] == 90.0
    assert entry.values["ln2_filled"] is False
    assert entry.values["notes"] == ""
    assert json.loads(entry.values["level_curve"]) == {
        "unix_time": [1.0, 2.0],
        "helium_pct": [40.0, 90.0],
    }

    # And the fill also produced an operations-stream audit entry.
    ops = servicing_store.entries("operations")
    assert len(ops) == 1
    assert ops[0].values["operation"] == "Helium Fill"


def test_recorder_writes_empty_level_curve_when_summary_missing(recorder, servicing_store):
    """A run_finished manifest with no "summary" key still writes a valid entry."""
    recorder.on_run_started(
        {"run_id": "fill3", "procedure": "Helium Fill", "started_utc": "2026-07-19T10:00:00+00:00"}
    )
    recorder.on_run_finished(
        {
            "run_id": "fill3",
            "procedure": "Helium Fill",
            "kind": "operation",
            "params": {},
            "started_utc": "2026-07-19T10:00:00+00:00",
            "finished_utc": "2026-07-19T10:05:00+00:00",
            "status": "done",
            "reason": "",
        }
    )
    entry = servicing_store.entries("cryogenics")[0]
    assert entry.values["level_curve"] == ""


@pytest.mark.parametrize(
    "summary",
    [
        None,
        "not a dict",
        {"level_curve": "not a dict"},
        {"level_curve": {"unix_time": "not a list", "helium_pct": [1.0]}},
        {"level_curve": {"unix_time": [1.0]}},  # missing helium_pct
    ],
)
def test_recorder_ignores_malformed_level_curve_summary(recorder, servicing_store, summary):
    """A malformed/partial "summary" never raises; level_curve just defaults to ""."""
    recorder.on_run_started(
        {"run_id": "fill4", "procedure": "Helium Fill", "started_utc": "2026-07-19T10:00:00+00:00"}
    )
    recorder.on_run_finished(
        {
            "run_id": "fill4",
            "procedure": "Helium Fill",
            "kind": "operation",
            "params": {},
            "started_utc": "2026-07-19T10:00:00+00:00",
            "finished_utc": "2026-07-19T10:05:00+00:00",
            "status": "done",
            "reason": "",
            "summary": summary,
        }
    )
    entry = servicing_store.entries("cryogenics")[0]
    assert entry.values["level_curve"] == ""


def test_old_format_cryogenics_line_without_level_curve_still_reads(servicing_store):
    """A pre-existing JSONL line with no level_curve key stays readable (no rewrite)."""
    path = servicing_store._path("cryogenics")
    path.parent.mkdir(parents=True, exist_ok=True)
    old_line = {
        "entry_id": "abc123",
        "kind": "cryogenics",
        "values": {
            "person": "jdoe",
            "start_utc": "2026-07-19T10:00:00+00:00",
            "end_utc": "2026-07-19T11:00:00+00:00",
            "helium_start_pct": 40.0,
            "helium_end_pct": 90.0,
            "ln2_filled": False,
            "notes": "",
            # deliberately no "level_curve" key — pre-dates this field
        },
        "source": "operation",
        "run_id": "old_run",
        "created_utc": "2026-07-19T11:00:00+00:00",
        "revised_utc": "",
        "revised_by": "",
        "revision": 1,
        "deleted": False,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(old_line) + "\n")

    entries = servicing_store.entries("cryogenics")
    assert len(entries) == 1
    assert entries[0].values["person"] == "jdoe"
    assert "level_curve" not in entries[0].values
    assert entries[0].values.get("level_curve", "") == ""


def test_recorder_marks_unverified_fill_in_notes(recorder, servicing_store):
    recorder.on_run_started(
        {"run_id": "fill2", "procedure": "Helium Fill", "started_utc": "2026-07-19T10:00:00+00:00"}
    )
    recorder.on_run_finished(
        {
            "run_id": "fill2",
            "procedure": "Helium Fill",
            "kind": "operation",
            "params": {},
            "started_utc": "2026-07-19T10:00:00+00:00",
            "finished_utc": "2026-07-19T10:10:00+00:00",
            "status": "failed",
            "reason": "level meter stale",
        }
    )
    entry = servicing_store.entries("cryogenics")[0]
    assert "unverified" in entry.values["notes"]
    assert "level meter stale" in entry.values["notes"]


def test_recorder_ignores_malformed_manifests(recorder, servicing_store):
    recorder.on_run_started(None)  # type: ignore[arg-type]
    recorder.on_run_started("junk")  # type: ignore[arg-type]
    recorder.on_run_finished(None)  # type: ignore[arg-type]
    recorder.on_run_finished({"kind": "operation"})  # missing everything else: must not raise
    # A run_finished with no matching run_started for the fill still must not raise.
    recorder.on_run_finished(
        {"procedure": "Helium Fill", "kind": "operation", "status": "done", "params": {}}
    )
