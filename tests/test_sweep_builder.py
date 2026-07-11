# ---
# description: |
#   Unit tests for cryosoft.core.sweep_builder: piecewise segmented sweeps,
#   custom CSV sweep loading, and hysteresis (forward+backward) extension.
# last_updated: 2026-07-11
# ---

import pytest

from cryosoft.core.sweep_builder import (
    SweepSegment,
    apply_hysteresis,
    build_piecewise_sweep,
    load_custom_sweep_csv,
)


# ── build_piecewise_sweep ──────────────────────────────────────────────────────


def test_empty_segments_returns_empty_list():
    assert build_piecewise_sweep([]) == []


def test_single_segment_is_linear_sweep():
    """One segment reduces to an ordinary start/end/step linear sweep."""
    sweep = build_piecewise_sweep([SweepSegment(start=0.0, end=1.0, step=0.25)])
    assert sweep == pytest.approx([0.0, 0.25, 0.5, 0.75, 1.0])


def test_single_segment_descending():
    sweep = build_piecewise_sweep([SweepSegment(start=1.0, end=0.0, step=0.5)])
    assert sweep == pytest.approx([1.0, 0.5, 0.0])


def test_segment_step_that_does_not_evenly_divide_is_adjusted():
    """Step is rounded to the nearest count that evenly divides the segment,
    so both endpoints are always hit exactly (no float drift, no leftover)."""
    sweep = build_piecewise_sweep([SweepSegment(start=0.0, end=1.0, step=0.3)])
    # abs(1.0-0.0)/0.3 = 3.33 -> rounds to 3 steps -> actual step = 1/3
    assert len(sweep) == 4
    assert sweep[0] == pytest.approx(0.0)
    assert sweep[-1] == pytest.approx(1.0)


def test_fine_subfield_between_coarse_segments():
    """The motivating case: coarse steps outside a region, fine steps inside it."""
    segments = [
        SweepSegment(start=1.0, end=0.1, step=0.1),
        SweepSegment(start=0.1, end=-0.1, step=0.01),
        SweepSegment(start=-0.1, end=-1.0, step=0.1),
    ]
    sweep = build_piecewise_sweep(segments)

    # Boundary points appear exactly once each (not duplicated across segments).
    assert sweep.count(0.1) == 1
    assert sweep.count(-0.1) == 1
    assert sweep[0] == pytest.approx(1.0)
    assert sweep[-1] == pytest.approx(-1.0)

    # Each segment contributes its own start + intermediate points but not its
    # end (the end is supplied by the next segment's start); the very last
    # segment's end is appended once at the very end.
    # seg1: 9 points (1.0 down to 0.2), seg2: 20 points (0.1 down to -0.09),
    # seg3: 9 points (-0.1 down to -0.9), plus the final appended -1.0.
    assert len(sweep) == 9 + 20 + 9 + 1


def test_noncontiguous_segments_raise():
    with pytest.raises(ValueError, match="contiguous"):
        build_piecewise_sweep(
            [
                SweepSegment(start=0.0, end=1.0, step=0.1),
                SweepSegment(start=2.0, end=3.0, step=0.1),  # gap: should start at 1.0
            ]
        )


def test_nonpositive_step_raises():
    with pytest.raises(ValueError, match="positive"):
        build_piecewise_sweep([SweepSegment(start=0.0, end=1.0, step=0.0)])
    with pytest.raises(ValueError, match="positive"):
        build_piecewise_sweep([SweepSegment(start=0.0, end=1.0, step=-0.1)])


# ── load_custom_sweep_csv ──────────────────────────────────────────────────────


def test_load_custom_sweep_csv(tmp_path):
    csv_file = tmp_path / "fields.csv"
    csv_file.write_text("1.0\n0.5\n\n0.0\n-0.5\n-1.0\n")

    values = load_custom_sweep_csv(str(csv_file))
    assert values == pytest.approx([1.0, 0.5, 0.0, -0.5, -1.0])


def test_load_custom_sweep_csv_rejects_multi_column_row(tmp_path):
    csv_file = tmp_path / "bad.csv"
    csv_file.write_text("1.0,2.0\n")

    with pytest.raises(ValueError, match="exactly one"):
        load_custom_sweep_csv(str(csv_file))


def test_load_custom_sweep_csv_rejects_unparseable_value(tmp_path):
    csv_file = tmp_path / "bad.csv"
    csv_file.write_text("not_a_number\n")

    with pytest.raises(ValueError, match="not a number"):
        load_custom_sweep_csv(str(csv_file))


def test_load_custom_sweep_csv_rejects_empty_file(tmp_path):
    csv_file = tmp_path / "empty.csv"
    csv_file.write_text("\n\n")

    with pytest.raises(ValueError, match="no sweep values"):
        load_custom_sweep_csv(str(csv_file))


def test_load_custom_sweep_csv_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_custom_sweep_csv(str(tmp_path / "does_not_exist.csv"))


# ── apply_hysteresis ────────────────────────────────────────────────────────────


def test_apply_hysteresis_basic():
    assert apply_hysteresis([-1.0, 0.0, 1.0]) == pytest.approx([-1.0, 0.0, 1.0, 0.0, -1.0])


def test_apply_hysteresis_does_not_duplicate_turning_point():
    result = apply_hysteresis([0.0, 1.0])
    assert result == pytest.approx([0.0, 1.0, 0.0])
    assert result.count(1.0) == 1


def test_apply_hysteresis_short_input_unchanged():
    assert apply_hysteresis([]) == []
    assert apply_hysteresis([5.0]) == [5.0]


def test_apply_hysteresis_composes_with_piecewise_sweep():
    """Hysteresis wraps naturally around a piecewise-built sweep."""
    base = build_piecewise_sweep(
        [
            SweepSegment(start=-1.0, end=-0.1, step=0.1),
            SweepSegment(start=-0.1, end=0.1, step=0.02),
            SweepSegment(start=0.1, end=1.0, step=0.1),
        ]
    )
    looped = apply_hysteresis(base)
    assert looped[: len(base)] == pytest.approx(base)
    assert looped[len(base)] == pytest.approx(base[-2])
    assert looped[-1] == pytest.approx(base[0])
