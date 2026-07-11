# ---
# description: |
#   Reusable sweep-array construction library shared by measurement Procedures.
#   Three composable building blocks: a piecewise/segmented sweep (different
#   step size per sub-range, e.g. coarse outside a region of interest and fine
#   inside it), a custom sweep loaded from a single-column CSV file, and a
#   hysteresis wrapper that turns a one-directional sweep into a forward+
#   backward loop. Procedures call these from their own _build_sweep_array()
#   override; this module never touches a Station, VI, or the GUI.
# entry_point: Not run directly; imported by Procedure subclasses.
# dependencies: none beyond the standard library.
# input: |
#   build_piecewise_sweep() takes a list of SweepSegment(start, end, step).
#   load_custom_sweep_csv() takes a path to a single-column CSV of numbers.
#   apply_hysteresis() takes any sweep array (list[float]).
# process: |
#   Segments are validated for contiguity (each segment's start must equal the
#   previous segment's end) and stitched together without duplicating shared
#   boundary points. Each segment's actual step size is the requested step
#   rounded to evenly divide the segment, so both endpoints are hit exactly.
# output: |
#   All functions return a plain list[float] sweep array, suitable to assign
#   directly to a Procedure's self._sweep.
# last_updated: 2026-07-11
# ---

"""sweep_builder — piecewise, CSV-custom, and hysteresis sweep construction."""

from __future__ import annotations

import csv
from dataclasses import dataclass


@dataclass
class SweepSegment:
    """One contiguous piece of a piecewise sweep: evenly spaced values from
    ``start`` to ``end`` at approximately ``step`` spacing.

    A ``dataclass`` is used instead of a plain dict so a segment is typo-proof
    (``segment.step`` instead of ``segment["step"]``) — ``@dataclass`` auto-
    generates ``__init__``, ``__repr__``, and ``__eq__`` from the three fields
    declared below, which is why the class body has no methods of its own.

    Attributes:
        start: First value of this segment, in the same units as the sweep.
        end: Last value of this segment.
        step: Requested spacing between points. The actual spacing used is
            ``(end - start) / n`` for the smallest ``n`` that keeps spacing at
            or below this value, so the segment lands on ``end`` exactly.
    """

    start: float
    end: float
    step: float


def build_piecewise_sweep(segments: list[SweepSegment]) -> list[float]:
    """Build a sweep with a different step size per sub-range.

    Example — coarse steps everywhere except a fine region near zero::

        build_piecewise_sweep([
            SweepSegment(start=1.0, end=0.1, step=0.1),      # 1 T -> 0.1 T, 0.1 T steps
            SweepSegment(start=0.1, end=-0.1, step=0.01),    # 0.1 T -> -0.1 T, 10 mT steps
            SweepSegment(start=-0.1, end=-1.0, step=0.1),    # -0.1 T -> -1 T, 0.1 T steps
        ])

    Segments must be contiguous: segment ``i``'s ``start`` must equal segment
    ``i - 1``'s ``end``. The shared boundary point between two segments is
    included only once. A single segment reduces to an ordinary linear sweep.

    Args:
        segments: Ordered, contiguous list of ``SweepSegment``.

    Returns:
        Flat list of sweep values, in the order the segments were given.
        Empty list if *segments* is empty.

    Raises:
        ValueError: If any segment has a non-positive ``step``, or if
            segments are not contiguous.
    """
    if not segments:
        return []

    points: list[float] = []
    for i, seg in enumerate(segments):
        if seg.step <= 0:
            raise ValueError(f"Segment {i} step must be positive, got {seg.step}")
        if i > 0 and seg.start != segments[i - 1].end:
            raise ValueError(
                f"Segment {i} start ({seg.start}) does not match segment "
                f"{i - 1} end ({segments[i - 1].end}); segments must be contiguous"
            )
        span = seg.end - seg.start
        n_steps = max(1, round(abs(span) / seg.step))
        actual_step = span / n_steps
        points.extend(seg.start + k * actual_step for k in range(n_steps))

    points.append(segments[-1].end)
    return points


def load_custom_sweep_csv(file_path: str) -> list[float]:
    """Load a sweep array from a single-column CSV file, one numeric value per row.

    Blank rows are skipped. Intended for arbitrary, non-uniform field (or other
    swept-variable) lists that cannot be expressed as start/end/step segments.

    Args:
        file_path: Path to the CSV file.

    Returns:
        List of parsed float values, in file order.

    Raises:
        ValueError: If a non-blank row has more than one column, if a value
            cannot be parsed as a float, or if the file has no values at all.
        FileNotFoundError: If *file_path* does not exist.
    """
    values: list[float] = []
    with open(file_path, encoding="utf-8", newline="") as f:
        for row_num, row in enumerate(csv.reader(f), start=1):
            if not row or all(cell.strip() == "" for cell in row):
                continue
            if len(row) != 1:
                raise ValueError(
                    f"{file_path}: row {row_num} must contain exactly one "
                    f"column, found {len(row)}"
                )
            try:
                values.append(float(row[0].strip()))
            except ValueError as exc:
                raise ValueError(
                    f"{file_path}: row {row_num} value {row[0]!r} is not a number"
                ) from exc

    if not values:
        raise ValueError(f"{file_path}: no sweep values found")
    return values


def apply_hysteresis(values: list[float]) -> list[float]:
    """Extend a one-directional sweep into a forward+backward hysteresis loop.

    Appends the reverse of *values*, excluding its last element, so the
    turning point at the end of the forward leg is not measured twice.
    E.g. ``[-1, 0, 1]`` becomes ``[-1, 0, 1, 0, -1]``.

    Args:
        values: A one-directional sweep array (e.g. from
            ``build_piecewise_sweep()`` or ``load_custom_sweep_csv()``).

    Returns:
        The forward sweep followed by the backward sweep. Returned unchanged
        if *values* has fewer than 2 points (nothing to reverse).
    """
    if len(values) < 2:
        return list(values)
    return list(values) + list(reversed(values[:-1]))
