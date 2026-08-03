"""Tests for cryosoft.core.trend_history."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from cryosoft.core.trend_history import (
    TIERS,
    find_crossings,
    persisted_keys,
    pick_tier,
    read_tier,
    read_window,
    summarize,
)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """Write one JSONL record per line to ``path``."""
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _raw_record(t: float, values: dict[str, float]) -> dict:
    return {"t": t, "v": values}


def _bucket_record(t: float, key_stats: dict[str, dict]) -> dict:
    return {"t": t, "n": sum(s["count"] for s in key_stats.values()), "v": key_stats}


def _stats(min_v, max_v, mean, std, count) -> dict:
    return {"min": min_v, "max": max_v, "mean": mean, "std": std, "count": count}


# --------------------------------------------------------------------------
# read_tier: file discovery and merging
# --------------------------------------------------------------------------


def test_read_tier_merges_live_and_rotated_files_oldest_first(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    rotated = tmp_path / "trend_history_raw.jsonl.2026-07-24"

    now = 2_000_000.0
    _write_jsonl(rotated, [_raw_record(now - 100, {"a": 1.0})])
    _write_jsonl(live, [_raw_record(now - 50, {"a": 2.0}), _raw_record(now, {"a": 3.0})])

    records = read_tier(tmp_path, "raw", window_s=200.0, now=now)

    assert [t for t, _ in records] == [now - 100, now - 50, now]
    assert [v["a"] for _, v in records] == [1.0, 2.0, 3.0]


def test_read_tier_skips_corrupt_truncated_and_blank_lines(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    good1 = json.dumps(_raw_record(now - 10, {"a": 1.0}))
    good2 = json.dumps(_raw_record(now, {"a": 2.0}))
    corrupt = "{not json at all"
    truncated = json.dumps(_raw_record(now - 5, {"a": 1.5}))[:20]  # cut mid-object
    blank = ""

    live.write_text(
        "\n".join([good1, corrupt, truncated, blank, good2]) + "\n", encoding="utf-8"
    )

    records = read_tier(tmp_path, "raw", window_s=200.0, now=now)

    assert [t for t, _ in records] == [now - 10, now]
    assert [v["a"] for _, v in records] == [1.0, 2.0]


def test_read_tier_excludes_sync_conflict_copy_filenames(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    conflict = tmp_path / "trend_history_raw-DESKTOP-ABC123.jsonl"
    conflict2 = tmp_path / "trend_history_raw (conflicted copy 2026-07-25).jsonl"

    now = 2_000_000.0
    _write_jsonl(live, [_raw_record(now, {"a": 1.0})])
    _write_jsonl(conflict, [_raw_record(now, {"a": 999.0})])
    _write_jsonl(conflict2, [_raw_record(now, {"a": 888.0})])

    records = read_tier(tmp_path, "raw", window_s=200.0, now=now)

    assert len(records) == 1
    assert records[0][1]["a"] == 1.0


def test_read_tier_missing_dir_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    assert read_tier(missing, "raw", window_s=200.0) == []


def test_read_tier_missing_files_returns_empty(tmp_path: Path) -> None:
    assert read_tier(tmp_path, "raw", window_s=200.0) == []


def test_read_tier_filters_to_window(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    _write_jsonl(
        live,
        [
            _raw_record(now - 1000, {"a": 1.0}),  # outside window
            _raw_record(now - 50, {"a": 2.0}),  # inside
            _raw_record(now, {"a": 3.0}),  # inside (boundary)
        ],
    )

    records = read_tier(tmp_path, "raw", window_s=100.0, now=now)

    assert [v["a"] for _, v in records] == [2.0, 3.0]


def test_read_tier_partial_result_when_older_files_absent(tmp_path: Path) -> None:
    """A window extending past retention (only some files present) is partial, not an error."""
    live = tmp_path / "trend_history_3min.jsonl"
    now = 2_000_000.0
    _write_jsonl(live, [_bucket_record(now, {"a": _stats(1.0, 1.0, 1.0, 0.0, 1)})])
    # No rotated backups exist at all, even though the window asks for far more.
    records = read_tier(tmp_path, "3min", window_s=999_999.0, now=now)
    assert len(records) == 1


# --------------------------------------------------------------------------
# pick_tier
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "window_s,expected",
    [
        (86400.0, "raw"),
        (86401.0, "3min"),
        (604800.0, "3min"),
        (604801.0, "hourly"),
    ],
)
def test_pick_tier_boundaries(window_s: float, expected: str) -> None:
    assert pick_tier(window_s) == expected


def test_tiers_table_has_expected_filenames() -> None:
    assert TIERS["raw"].filename == "trend_history_raw.jsonl"
    assert TIERS["3min"].filename == "trend_history_3min.jsonl"
    assert TIERS["hourly"].filename == "trend_history_hourly.jsonl"


# --------------------------------------------------------------------------
# summarize: raw tier
# --------------------------------------------------------------------------


def test_summarize_raw_arithmetic(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    values = [1.0, 2.0, 3.0, 4.0]
    _write_jsonl(
        live, [_raw_record(now - (len(values) - i) * 1.0, {"a": v}) for i, v in enumerate(values)]
    )

    result = summarize(tmp_path, ["a"], window_s=100.0, now=now)
    s = result["a"]

    expected_mean = sum(values) / len(values)
    expected_std = math.sqrt(sum((v - expected_mean) ** 2 for v in values) / len(values))

    assert s.persisted is True
    assert s.tier == "raw"
    assert s.count == 4
    assert s.min == 1.0
    assert s.max == 4.0
    assert s.mean == pytest.approx(expected_mean)
    assert s.std == pytest.approx(expected_std)


# --------------------------------------------------------------------------
# summarize: aggregate tier, count-weighted mean regression guard
# --------------------------------------------------------------------------


def test_summarize_aggregate_count_weighted_mean_differs_from_naive(tmp_path: Path) -> None:
    """Unequal bucket counts: count-weighted mean must differ from mean-of-means.

    This is the regression guard for the entire reason per-key ``count`` is
    written by TieredTrendLogger: a plain mean-of-means is wrong once
    buckets have unequal sample counts.
    """
    live = tmp_path / "trend_history_3min.jsonl"
    now = 2_000_000.0
    bucket_a = _stats(min_v=0.0, max_v=2.0, mean=1.0, std=1.0, count=10)
    bucket_b = _stats(min_v=4.0, max_v=6.0, mean=5.0, std=1.0, count=100)
    _write_jsonl(
        live,
        [
            _bucket_record(now - 360.0, {"a": bucket_a}),
            _bucket_record(now - 180.0, {"a": bucket_b}),
        ],
    )

    result = summarize(tmp_path, ["a"], window_s=90000.0, now=now)
    s = result["a"]

    naive_mean_of_means = (bucket_a["mean"] + bucket_b["mean"]) / 2  # = 3.0
    expected_weighted_mean = (
        bucket_a["mean"] * bucket_a["count"] + bucket_b["mean"] * bucket_b["count"]
    ) / (bucket_a["count"] + bucket_b["count"])  # = 510/110 = 4.636...

    assert s.persisted is True
    assert s.tier == "3min"
    assert s.count == 110
    assert s.min == 0.0
    assert s.max == 6.0
    assert s.mean == pytest.approx(expected_weighted_mean)
    assert s.mean != pytest.approx(naive_mean_of_means)
    assert expected_weighted_mean == pytest.approx(510.0 / 110.0)


def _population_stats(values: list[float]) -> tuple[float, float, int]:
    """Mean, population std, and count of ``values`` computed directly."""
    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    return mean, math.sqrt(variance), n


def test_summarize_aggregate_std_is_exact_via_law_of_total_variance(tmp_path: Path) -> None:
    """Cross-bucket ``std`` recombination is exact, not an approximation.

    Two unequal-size groups of raw values (3 and 7) are pre-aggregated into
    two buckets exactly as TieredTrendLogger would flush them. summarize()
    must reproduce the *true* population std of all 10 raw values combined,
    to full float precision, via the law of total variance recovered from
    each bucket's (mean, std, count) — not the smaller number a naive
    pooled-within-bucket-only estimate (ignoring the spread between bucket
    means) would give.
    """
    group_a = [10.0, 12.0, 14.0]  # 3 values
    group_b = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]  # 7 values
    combined = group_a + group_b

    mean_a, std_a, count_a = _population_stats(group_a)
    mean_b, std_b, count_b = _population_stats(group_b)
    true_mean, true_std, true_count = _population_stats(combined)

    live = tmp_path / "trend_history_3min.jsonl"
    now = 2_000_000.0
    bucket_a = _stats(min(group_a), max(group_a), mean_a, std_a, count_a)
    bucket_b = _stats(min(group_b), max(group_b), mean_b, std_b, count_b)
    _write_jsonl(
        live,
        [
            _bucket_record(now - 360.0, {"a": bucket_a}),
            _bucket_record(now - 180.0, {"a": bucket_b}),
        ],
    )

    result = summarize(tmp_path, ["a"], window_s=90000.0, now=now)
    s = result["a"]

    assert s.count == true_count
    assert s.min == min(combined)
    assert s.max == max(combined)
    assert s.mean == pytest.approx(true_mean, rel=1e-12)
    assert s.std == pytest.approx(true_std, rel=1e-12)

    # Regression guard: the old (superseded) pooled-within-bucket-only
    # estimate ignores the spread *between* bucket means and therefore
    # underestimates the true combined spread whenever those means differ.
    naive_pooled_variance = (count_a * std_a**2 + count_b * std_b**2) / (count_a + count_b)
    naive_pooled_std = math.sqrt(naive_pooled_variance)
    assert naive_pooled_std < s.std
    assert naive_pooled_std != pytest.approx(s.std)


# --------------------------------------------------------------------------
# persisted flag
# --------------------------------------------------------------------------


def test_summarize_persisted_false_for_never_written_key(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    _write_jsonl(live, [_raw_record(now, {"a": 1.0})])

    result = summarize(tmp_path, ["a", "never_persisted_key"], window_s=100.0, now=now)

    assert result["a"].persisted is True
    assert result["never_persisted_key"].persisted is False
    assert result["never_persisted_key"].count == 0
    assert result["never_persisted_key"].mean is None


def test_summarize_persisted_true_but_empty_window_distinguishes_from_never_written(
    tmp_path: Path,
) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    # "a" was written, but far outside the requested window.
    _write_jsonl(live, [_raw_record(now - 100_000.0, {"a": 1.0})])

    result = summarize(tmp_path, ["a"], window_s=10.0, now=now)
    s = result["a"]

    assert s.persisted is True  # known to the store...
    assert s.count == 0  # ...but nothing in this window
    assert s.mean is None


def test_persisted_keys_empty_for_missing_dir(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    assert persisted_keys(missing, "raw") == set()


def test_persisted_keys_reads_recent_records(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    _write_jsonl(live, [_raw_record(now, {"a": 1.0, "b": 2.0})])

    keys = persisted_keys(tmp_path, "raw")

    assert keys == {"a", "b"}


# --------------------------------------------------------------------------
# read_window
# --------------------------------------------------------------------------


def test_read_window_raw_tier_scalar_and_missing_key(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    _write_jsonl(live, [_raw_record(now, {"a": 1.5})])

    series = read_window(tmp_path, ["a", "missing"], window_s=100.0, now=now)

    assert series["a"] == [(now, 1.5)]
    assert series["missing"] == []


def test_read_window_aggregate_tier_uses_mean(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_3min.jsonl"
    now = 2_000_000.0
    _write_jsonl(live, [_bucket_record(now, {"a": _stats(1.0, 3.0, 2.0, 0.5, 60)})])

    series = read_window(tmp_path, ["a"], window_s=200000.0, now=now)

    assert series["a"] == [(now, 2.0)]


# --------------------------------------------------------------------------
# find_crossings
# --------------------------------------------------------------------------


def _write_raw_series(path: Path, now: float, values: list[float]) -> None:
    records = [_raw_record(now - (len(values) - i) * 1.0, {"a": v}) for i, v in enumerate(values)]
    _write_jsonl(path, records)


def test_find_crossings_below(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    # 5, 4, 3, 2 (threshold 3): crosses below when value goes from >=3 to <3.
    _write_raw_series(live, now, [5.0, 4.0, 3.0, 2.0])

    crossings = find_crossings(tmp_path, "a", threshold=3.0, window_s=100.0, direction="below", now=now)

    assert len(crossings) == 1
    assert crossings[0] == now - 1.0  # the sample that landed at value 2.0


def test_find_crossings_above(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    _write_raw_series(live, now, [1.0, 2.0, 3.0, 4.0])

    crossings = find_crossings(tmp_path, "a", threshold=3.0, window_s=100.0, direction="above", now=now)

    assert len(crossings) == 1
    assert crossings[0] == now - 1.0


def test_find_crossings_both(tmp_path: Path) -> None:
    live = tmp_path / "trend_history_raw.jsonl"
    now = 2_000_000.0
    _write_raw_series(live, now, [1.0, 4.0, 1.0, 4.0])

    crossings = find_crossings(tmp_path, "a", threshold=3.0, window_s=100.0, direction="both", now=now)

    assert len(crossings) == 3


def test_find_crossings_invalid_direction_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        find_crossings(tmp_path, "a", threshold=3.0, window_s=100.0, direction="sideways")


def test_find_crossings_missing_dir_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    assert find_crossings(missing, "a", threshold=3.0, window_s=100.0) == []
