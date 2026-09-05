"""Tests for cryosoft.core.trend_checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cryosoft.core.conditions import Condition
from cryosoft.core.trend_checks import (
    CheckOutcome,
    CheckResult,
    TrendCheck,
    conditions_for,
    declared_checks,
    no_data_outcome,
    run_check,
    run_checks,
    to_condition,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _raw_record(t: float, values: dict[str, float]) -> dict:
    return {"t": t, "v": values}


# One trivial throwaway predicate/check, exercising the run_check()/
# run_checks() machinery generically — the real declared checks
# (sample_temperature_stable) gets its own dedicated predicate tests below.
def _stability_predicate(key: str, std_limit: float):
    def predicate(summaries, series, window_s):
        del series, window_s  # unused: this fixture predicate only needs the summary
        no_data = no_data_outcome(summaries, [key])
        if no_data is not None:
            return no_data
        summary = summaries[key]
        evidence = {"std": summary.std, "count": summary.count}
        if summary.std <= std_limit:
            return CheckOutcome(
                True, f"std={summary.std:.3g} <= {std_limit} over {summary.count} samples", evidence
            )
        return CheckOutcome(
            False, f"std={summary.std:.3g} > {std_limit} over {summary.count} samples", evidence
        )

    return predicate


def _fixture_check(
    name: str = "temp_stable", key: str = "temp", std_limit: float = 0.5, severity: str = "advisory"
) -> TrendCheck:
    return TrendCheck(
        name=name,
        keys=(key,),
        window_s=3600.0,
        severity=severity,
        predicate=_stability_predicate(key, std_limit),
    )


# ── TrendCheck validation ───────────────────────────────────────────────────


def test_trend_check_rejects_empty_name():
    with pytest.raises(ValueError, match="name"):
        TrendCheck(name="", keys=("a",), window_s=1.0, severity="advisory", predicate=lambda s, sr, w: None)


def test_trend_check_rejects_empty_keys():
    with pytest.raises(ValueError, match="keys"):
        TrendCheck(name="x", keys=(), window_s=1.0, severity="advisory", predicate=lambda s, sr, w: None)


def test_trend_check_rejects_non_positive_window():
    with pytest.raises(ValueError, match="window_s"):
        TrendCheck(name="x", keys=("a",), window_s=0.0, severity="advisory", predicate=lambda s, sr, w: None)


def test_trend_check_rejects_unknown_severity():
    with pytest.raises(ValueError, match="severity"):
        TrendCheck(name="x", keys=("a",), window_s=1.0, severity="urgent", predicate=lambda s, sr, w: None)


# ── no_data_outcome ──────────────────────────────────────────────────────────


def test_no_data_outcome_none_when_data_present(tmp_path: Path):
    now = 2_000_000.0
    _write_jsonl(tmp_path / "trend_history_raw.jsonl", [_raw_record(now, {"temp": 1.0})])
    check = _fixture_check()
    result = run_check(check, tmp_path, now=now)
    assert result.passed is not None


def test_no_data_outcome_never_persisted_is_indeterminate(tmp_path: Path):
    # No files at all: the key was never persisted.
    check = _fixture_check()
    result = run_check(check, tmp_path, now=2_000_000.0)
    assert result.passed is None
    assert "never persisted" in result.message


def test_no_data_outcome_persisted_but_empty_window_is_indeterminate(tmp_path: Path):
    now = 2_000_000.0
    # The key is persisted (appears in the tier's files) but has no samples
    # inside the requested window — write one sample far outside window_s.
    _write_jsonl(
        tmp_path / "trend_history_raw.jsonl", [_raw_record(now - 10_000_000.0, {"temp": 1.0})]
    )
    check = _fixture_check()
    result = run_check(check, tmp_path, now=now)
    assert result.passed is None
    assert "no samples" in result.message


# ── run_check / run_checks ───────────────────────────────────────────────────


def test_run_check_passes_for_stable_data(tmp_path: Path):
    now = 2_000_000.0
    records = [_raw_record(now - i, {"temp": 10.0}) for i in range(5)]
    _write_jsonl(tmp_path / "trend_history_raw.jsonl", records)
    check = _fixture_check(std_limit=0.5)
    result = run_check(check, tmp_path, now=now)
    assert result.passed is True
    assert result.name == "temp_stable"
    assert result.evidence["count"] == 5


def test_run_check_fails_for_unstable_data(tmp_path: Path):
    now = 2_000_000.0
    records = [_raw_record(now - i, {"temp": v}) for i, v in enumerate([0.0, 20.0, 0.0, 20.0])]
    _write_jsonl(tmp_path / "trend_history_raw.jsonl", records)
    check = _fixture_check(std_limit=0.5)
    result = run_check(check, tmp_path, now=now)
    assert result.passed is False


def test_run_checks_evaluates_every_check_uniformly(tmp_path: Path):
    now = 2_000_000.0
    _write_jsonl(
        tmp_path / "trend_history_raw.jsonl",
        [_raw_record(now, {"a": 1.0, "b": 100.0})],
    )
    checks = [_fixture_check(name="a_stable", key="a"), _fixture_check(name="b_stable", key="b")]
    results = run_checks(checks, tmp_path, now=now)
    assert [r.name for r in results] == ["a_stable", "b_stable"]


# ── to_condition / conditions_for ────────────────────────────────────────────


def test_to_condition_none_for_passing_result():
    check = _fixture_check()
    result = CheckResult(name=check.name, passed=True, message="ok", evidence={})
    assert to_condition(check, result, since=100.0) is None


def test_to_condition_none_for_indeterminate_result():
    check = _fixture_check()
    result = CheckResult(name=check.name, passed=None, message="cannot tell", evidence={})
    assert to_condition(check, result, since=100.0) is None


def test_to_condition_builds_advisory_condition_for_failure():
    check = _fixture_check(name="temp_stable")
    result = CheckResult(name="temp_stable", passed=False, message="unstable", evidence={"std": 9.0})
    condition = to_condition(check, result, since=100.0)
    assert isinstance(condition, Condition)
    assert condition.key == "trend:temp_stable"
    assert condition.origin == "trend"
    assert condition.severity == "advisory"
    assert condition.affected_vis is None
    assert condition.message == "unstable"
    assert condition.since == 100.0


def test_conditions_for_only_includes_failures(tmp_path: Path):
    now = 2_000_000.0
    _write_jsonl(
        tmp_path / "trend_history_raw.jsonl",
        [_raw_record(now - i, {"stable": 5.0, "unstable": v}) for i, v in enumerate([0.0, 50.0])],
    )
    checks = [
        _fixture_check(name="stable_check", key="stable"),
        _fixture_check(name="unstable_check", key="unstable"),
    ]
    results = run_checks(checks, tmp_path, now=now)
    conditions = conditions_for(checks, results, since=now)
    assert [c.key for c in conditions] == ["trend:unstable_check"]


# ── declared_checks ───────────────────────────────────────────────────────────

# A fully-merged trends: config, mirroring what Station.read_trends_config()
# actually hands declared_checks() at runtime — real usage never calls it
# with a partial dict.
_FULL_TRENDS_CONFIG: dict[str, float] = {
    "refresh_interval_s": 60.0,
    "sample_temperature_window_s": 3600.0,
    "sample_temperature_std_limit_K": 0.1,
    "sample_temperature_range_limit_K": 0.5,
    "store_live_stale_ticks": 10.0,
}


def test_declared_checks_ships_one_advisory_check():
    checks = declared_checks(_FULL_TRENDS_CONFIG)
    assert [c.name for c in checks] == ["sample_temperature_stable"]
    assert all(c.severity == "advisory" for c in checks)


def test_declared_checks_falls_back_on_missing_config_keys():
    # declared_checks() is called with {} by this test only — real callers
    # always pass Station.read_trends_config()'s fully-merged dict.
    checks = declared_checks({})
    assert [c.name for c in checks] == ["sample_temperature_stable"]


def test_declared_checks_never_hardcodes_window_reading_config():
    checks = declared_checks(_FULL_TRENDS_CONFIG)
    by_name = {c.name: c for c in checks}
    assert by_name["sample_temperature_stable"].window_s == 3600.0

    overridden = dict(_FULL_TRENDS_CONFIG)
    overridden["sample_temperature_window_s"] = 1800.0
    checks2 = {c.name: c for c in declared_checks(overridden)}
    assert checks2["sample_temperature_stable"].window_s == 1800.0


# ── sample_temperature_stable ────────────────────────────────────────────────


def _temperature_check(config: dict[str, float] | None = None) -> TrendCheck:
    checks = {c.name: c for c in declared_checks(config or _FULL_TRENDS_CONFIG)}
    return checks["sample_temperature_stable"]


def test_sample_temperature_stable_passes_for_flat_readings(tmp_path: Path):
    now = 2_000_000.0
    records = [
        _raw_record(now - i, {"temperature_vti_temperature": 4.2}) for i in range(0, 3600, 100)
    ]
    _write_jsonl(tmp_path / "trend_history_raw.jsonl", records)
    result = run_check(_temperature_check(), tmp_path, now=now)
    assert result.passed is True
    assert result.evidence["std_K"] == pytest.approx(0.0, abs=1e-9)
    assert result.evidence["count"] == len(records)
    assert result.evidence["window_s"] == 3600.0


def test_sample_temperature_stable_fails_for_oscillating_readings(tmp_path: Path):
    now = 2_000_000.0
    values = [4.0, 4.6, 4.0, 4.6, 4.0, 4.6]
    records = [
        _raw_record(now - i * 60, {"temperature_vti_temperature": v})
        for i, v in enumerate(values)
    ]
    _write_jsonl(tmp_path / "trend_history_raw.jsonl", records)
    result = run_check(_temperature_check(), tmp_path, now=now)
    assert result.passed is False
    assert result.evidence["std_K"] > 0.1
    assert result.evidence["range_K"] == pytest.approx(0.6)
    assert "std=" in result.message and "range=" in result.message


def test_sample_temperature_stable_indeterminate_on_empty_window(tmp_path: Path):
    result = run_check(_temperature_check(), tmp_path, now=2_000_000.0)
    assert result.passed is None


def test_sample_temperature_stable_defined_verdict_on_single_sample(tmp_path: Path):
    now = 2_000_000.0
    _write_jsonl(
        tmp_path / "trend_history_raw.jsonl", [_raw_record(now, {"temperature_vti_temperature": 4.2})]
    )
    result = run_check(_temperature_check(), tmp_path, now=now)
    # One sample: std/range are both exactly zero, a defined (passing) verdict.
    assert result.passed is True
    assert result.evidence["count"] == 1
    assert result.evidence["std_K"] == 0.0
    assert result.evidence["range_K"] == 0.0
