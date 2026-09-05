"""trend_checks — named temporal judgements over the trend-history store.

A trend check turns a window of `trend_history.summarize()` evidence into a
pass/fail/indeterminate verdict, e.g. "has the sample temperature been
stable for the last hour". Qt-free and `Station`-free by design, like its
neighbour `trend_history.py`: this module imports nothing from PyQt6, the
Orchestrator, or the Station, so a check is unit-testable against a synthetic
JSONL directory with no hardware and no GUI.

The standard this module defines: a check is a `TrendCheck` DECLARATION (a
name, the state keys and window it reads, its severity, and a predicate
function), not a hand-written function with its own control flow. `run_checks`
is the one runner that evaluates any number of declarations uniformly — it
never special-cases a check by name — and `to_condition`/`conditions_for` are
the one adapter that turns a failing check into the `Condition` currency the
rest of CryoSoft's System-Condition standard (`core/conditions.py`) already
understands. Adding a fourth check means adding a `TrendCheck` literal (with
its predicate) to `declared_checks()`'s returned tuple, reading any threshold
it needs out of the `config` mapping passed in — never hardcoded, see
`Station.read_trends_config()` — and nothing else in this module changes.

No data in a requested window is not a failure of the thing being measured,
and it is not a pass either: it is "cannot tell", which this module
represents as `CheckOutcome.passed=None`/`CheckResult.passed=None`, distinct
from `True`/`False`. `summarize()` already tells the two "no data" cases
apart (`persisted=False` — never persisted, e.g. a measurement-VI key never
reaches disk — versus `persisted=True, count=0` — persisted but nothing
landed in this window) and never raises; a predicate that ignores this and
divides by a zero count, or claims a definite pass/fail from an empty window,
is a defect. `no_data_outcome()` below is the shared helper every predicate
should call first. `to_condition()` never publishes a `Condition` for an
indeterminate result: the System-Condition standard's registry holds active
PROBLEMS, and "cannot tell" is not one.

A predicate takes two extra arguments beyond `summaries`, both derived from
the SAME evaluation `run_check()` already did — never re-declared or
re-decided by the predicate itself:

- The windowed series (`{key: [(t, value), ...]}` from
  `trend_history.read_window()`), for rate-of-change reasoning: `KeySummary`
  carries `first_t`/`last_t` but never the values sampled at those times, so
  a check that needs "how much did this change" (a boil-off rate, a
  ramp-completion ETA) cannot answer it from `summaries` alone, and adding
  those values to `KeySummary` itself would leak a rate-specific need into
  an aggregate-statistics type. A predicate that only needs aggregates
  (e.g. `sample_temperature_stable`) simply does not read this argument. On
  the `3min`/`hourly` tiers a series' values are bucket *means*, not true
  instantaneous samples (see `KeySummary.tier`); a predicate presenting one
  as if it were a raw reading on a window long enough to leave the raw tier
  is a defect.
- The window, in seconds, THIS evaluation actually queried. A predicate must
  report this rather than a value closed over at `declared_checks()` time:
  a caller (the troubleshoot CLI's `--window` override) may evaluate a
  `TrendCheck` with a different `window_s` than the one it was declared
  with (`dataclasses.replace(check, window_s=...)`), and evidence citing a
  stale window would misstate what was actually evaluated.

`run_check()` computes `summarize()` and `read_window()` over the same
`(keys, window_s, now)` once, from the same tier `pick_tier()` picks, and
hands both plus `check.window_s` to every predicate uniformly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cryosoft.core.conditions import SEVERITIES, Condition
from cryosoft.core.trend_history import KeySummary, read_window, summarize

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckOutcome:
    """A predicate's raw verdict, before the check's identity is attached.

    Attributes:
        passed: `True`/`False` for a definite verdict, or `None` when the
            window had no data to judge — see the module docstring's "no
            data" policy. Distinct from `False` so a caller can tell "fine"
            from "cannot tell".
        message: Human- and agent-readable verdict, citing the evidence
            numbers directly (this repository's first principle is that
            claims are traceable to their source).
        evidence: The `KeySummary` fields (or numbers derived from them) the
            verdict rested on.
    """

    passed: bool | None
    message: str
    evidence: dict[str, object]


WindowedSeries = Mapping[str, list[tuple[float, float]]]

Predicate = Callable[[Mapping[str, KeySummary], WindowedSeries, float], CheckOutcome]


@dataclass(frozen=True)
class TrendCheck:
    """One declared temporal judgement over the trend-history store.

    Attributes:
        name: Stable identity, e.g. ``"sample_temperature_stable"``. Used to
            key its published `Condition` (``f"trend:{name}"``) and to match
            a `CheckResult` back to its declaration.
        keys: Flat state keys to summarize, as in `Station.last_state_flat()`
            (e.g. ``"temperature_sample_temperature_K"``).
        window_s: Trailing window length in seconds, passed to
            `trend_history.summarize()`. Tier selection is not this check's
            decision — `trend_history.pick_tier()` is the single home for
            that.
        severity: A `cryosoft.core.conditions.SEVERITIES` rung. Every check
            shipped in this branch declares ``"advisory"`` — see
            `to_condition()`'s docstring for why nothing here forces that as
            a type-level restriction.
        predicate: Pure function from (``{key: KeySummary}``, the windowed
            series `{key: [(t, value), ...]}` — see `WindowedSeries` — and
            the actual window queried in seconds; see the module docstring)
            to a `CheckOutcome`, all three covering exactly the `keys` above
            over the window `run_check()` actually evaluated (normally
            `window_s`, but a caller may override it — see the module
            docstring). Knows nothing about Qt, the Orchestrator, or the
            Station.

    Raises:
        ValueError: If `__post_init__` finds the fields invalid — see its
            docstring.
    """

    name: str
    keys: tuple[str, ...]
    window_s: float
    severity: str
    predicate: Predicate

    def __post_init__(self) -> None:
        """Validate the declaration shape.

        Raises:
            ValueError: If `name` or `keys` is empty, if `window_s` is not
                positive, or if `severity` is not one of
                `cryosoft.core.conditions.SEVERITIES`.
        """
        if not self.name:
            raise ValueError("TrendCheck.name must be non-empty")
        if not self.keys:
            raise ValueError(f"TrendCheck {self.name!r}: keys must be non-empty")
        if self.window_s <= 0:
            raise ValueError(f"TrendCheck {self.name!r}: window_s must be positive")
        if self.severity not in SEVERITIES:
            raise ValueError(
                f"TrendCheck {self.name!r}: severity must be one of {SEVERITIES}, "
                f"got {self.severity!r}"
            )


@dataclass(frozen=True)
class CheckResult:
    """One `TrendCheck`'s verdict for one evaluation.

    Attributes:
        name: The originating `TrendCheck.name`.
        passed: See `CheckOutcome.passed` — `True`/`False`/`None`
            (indeterminate).
        message: See `CheckOutcome.message`.
        evidence: See `CheckOutcome.evidence`.
    """

    name: str
    passed: bool | None
    message: str
    evidence: dict[str, object]


def no_data_outcome(
    summaries: Mapping[str, KeySummary], keys: Sequence[str]
) -> CheckOutcome | None:
    """Return the shared "cannot tell" outcome if any of `keys` has no data.

    Every predicate should call this first: a key `summarize()` never
    persisted (`persisted=False`, e.g. a measurement-VI key that never
    reaches disk) or persisted but empty in this window (`persisted=True`,
    `count=0`) means the check cannot judge anything, which is neither a
    pass nor a failure of the thing being measured (see the module
    docstring's "no data" policy).

    Args:
        summaries: This evaluation's `{key: KeySummary}`, as passed to a
            `TrendCheck.predicate`.
        keys: The keys the predicate actually needs data for.

    Returns:
        A `CheckOutcome(passed=None, ...)` naming which key(s) had no data,
        or `None` if every key has at least one sample — proceed with the
        real judgement.
    """
    missing = [key for key in keys if summaries[key].count == 0]
    if not missing:
        return None
    never_persisted = [key for key in missing if not summaries[key].persisted]
    if never_persisted:
        detail = f"never persisted to disk: {', '.join(sorted(never_persisted))}"
    else:
        detail = f"no samples in the requested window: {', '.join(sorted(missing))}"
    return CheckOutcome(
        passed=None,
        message=f"Cannot tell — {detail}.",
        evidence={key: summaries[key] for key in missing},
    )


def run_check(check: TrendCheck, log_dir: Path, now: float | None = None) -> CheckResult:
    """Evaluate one declared check against a trend-history log directory.

    Args:
        check: The declaration to evaluate.
        log_dir: Directory containing the trend-history JSONL files, as
            resolved by `cryosoft.core.paths.log_directory()`.
        now: Reference "now" timestamp; defaults to `time.time()` inside
            `trend_history.summarize()`.

    Returns:
        This check's `CheckResult`.
    """
    summaries = summarize(log_dir, check.keys, check.window_s, now=now)
    series = read_window(log_dir, check.keys, check.window_s, now=now)
    outcome = check.predicate(summaries, series, check.window_s)
    return CheckResult(
        name=check.name, passed=outcome.passed, message=outcome.message, evidence=outcome.evidence
    )


def run_checks(
    checks: Sequence[TrendCheck], log_dir: Path, now: float | None = None
) -> list[CheckResult]:
    """Evaluate every declared check against one trend-history log directory.

    The standard's runner: a uniform loop over whatever `checks` is passed,
    with no per-check branch. A caller (the CLI, a scheduled refresh) is
    what decides which checks to evaluate; this function never reads
    `declared_checks()` itself.

    Args:
        checks: The declarations to evaluate, in any order.
        log_dir: Directory containing the trend-history JSONL files.
        now: Reference "now" timestamp, shared by every check in this call
            so a single evaluation is judged against one consistent instant.

    Returns:
        One `CheckResult` per check, in the same order as `checks`.
    """
    return [run_check(check, log_dir, now=now) for check in checks]


def to_condition(check: TrendCheck, result: CheckResult, since: float) -> Condition | None:
    """Build the `Condition` a failing check publishes, or `None`.

    Only a definite failure (`result.passed is False`) becomes a `Condition`
    — a pass publishes nothing (there is nothing wrong to report) and an
    indeterminate result (`result.passed is None`) publishes nothing either,
    per the module docstring's "no data" policy: the System-Condition
    registry holds active problems, and "cannot tell" is not one.

    `severity` is read off `check.severity` rather than hardcoded, so this
    adapter itself never has to change if a future check is ever promoted
    off `"advisory"` — every check declared in this branch happens to be
    `"advisory"` (see the review standard this work is held to: a trend
    check must never be able to interrupt the operator), which is a fact
    about what `declared_checks()` currently returns, not a restriction
    this function enforces.

    Args:
        check: The declaration `result` came from (for `severity`/`name`).
        result: This evaluation's verdict.
        since: Unix timestamp to stamp a fresh `Condition.since` with. If
            the same key is already active in the Station's registry,
            `Station.publish_conditions()` -> `_upsert_condition()` preserves
            the PRIOR `since`/`acknowledged` instead — see their docstrings.

    Returns:
        A `Condition` keyed ``f"trend:{check.name}"``, origin ``"trend"``,
        `affected_vis=None` (advisory conditions trigger no enforcement, so
        no VI needs naming), or `None` if `result.passed` is not `False`.
    """
    if result.passed is not False:
        return None
    return Condition(
        key=f"trend:{check.name}",
        origin="trend",
        severity=check.severity,
        kind=check.name,
        source_vis=(),
        affected_vis=None,
        message=result.message,
        since=since,
    )


def conditions_for(
    checks: Sequence[TrendCheck], results: Sequence[CheckResult], since: float
) -> list[Condition]:
    """Build every `Condition` this evaluation's failing checks publish.

    Args:
        checks: The declarations `results` were evaluated from.
        results: `run_checks(checks, ...)`'s output.
        since: Passed through to `to_condition()`.

    Returns:
        One `Condition` per check with `passed is False`, in `results`
        order.

    Raises:
        KeyError: If a `result.name` does not match any `check.name`.
    """
    by_name = {check.name: check for check in checks}
    conditions: list[Condition] = []
    for result in results:
        condition = to_condition(by_name[result.name], result, since)
        if condition is not None:
            conditions.append(condition)
    return conditions


# ── sample_temperature_stable ────────────────────────────────────────────────
# The flat state key from Station.last_state_flat(): the sample-sensor
# temperature-controller VI's @monitored temperature() method, verified
# against every sim-buildable shipped config by
# test_declared_trend_checks_name_real_state_keys.
_SAMPLE_TEMPERATURE_KEY = "temperature_vti_temperature"

# Mirrors cryosoft.core.station._TREND_DEFAULTS's values for these keys —
# see that dict's comment for why this is a mirror, not an import (contract
# C15). Used only as `.get()` fallbacks; a real setup's config always arrives
# here already merged by Station.read_trends_config().
_SAMPLE_TEMPERATURE_WINDOW_S = 3600.0
_SAMPLE_TEMPERATURE_STD_LIMIT_K = 0.1
_SAMPLE_TEMPERATURE_RANGE_LIMIT_K = 0.5


def _sample_temperature_stable_predicate(std_limit_K: float, range_limit_K: float) -> Predicate:
    """Build the `sample_temperature_stable` predicate for one setup's thresholds.

    Fails when either the trailing window's standard deviation exceeds
    ``std_limit_K`` or its full min-max spread exceeds ``range_limit_K`` —
    two different failure shapes (persistent slow drift raises std without
    necessarily widening the range much; a single spike widens the range
    without moving std much), both worth catching. Uses only `summarize()`'s
    exact recombined `std`/`min`/`max` — never re-derives them from raw
    rows, which would disagree with `summarize()` at tier boundaries (see
    the module docstring). Thresholds are fixed per-check declaration; the
    window is read off the predicate's own `window_s` argument (the window
    THIS evaluation actually queried — see the module docstring), never
    closed over, so a caller overriding `TrendCheck.window_s` never gets
    evidence citing a stale window.
    """

    def predicate(
        summaries: Mapping[str, KeySummary], series: WindowedSeries, window_s: float
    ) -> CheckOutcome:
        # `series` is unused: this check needs only the aggregate summary.
        no_data = no_data_outcome(summaries, [_SAMPLE_TEMPERATURE_KEY])
        if no_data is not None:
            return no_data
        summary = summaries[_SAMPLE_TEMPERATURE_KEY]
        # min/max/std are non-None here: no_data_outcome already ruled out
        # count == 0, and summarize() always populates them together.
        spread = summary.max - summary.min  # type: ignore[operator]
        evidence: dict[str, object] = {
            "std_K": summary.std,
            "min_K": summary.min,
            "max_K": summary.max,
            "range_K": spread,
            "count": summary.count,
            "window_s": window_s,
            "std_limit_K": std_limit_K,
            "range_limit_K": range_limit_K,
        }
        unstable = summary.std > std_limit_K or spread > range_limit_K  # type: ignore[operator]
        verdict = "unstable" if unstable else "stable"
        message = (
            f"Sample temperature {verdict} over {window_s / 3600.0:.2g} h: "
            f"std={summary.std:.3g} K (limit {std_limit_K} K), "
            f"range={spread:.3g} K (limit {range_limit_K} K), n={summary.count}."
        )
        return CheckOutcome(passed=not unstable, message=message, evidence=evidence)

    return predicate


# ── helium_consumption_normal ───────────────────────────────────────────────
# The flat state key: the "level_meter" VI's @monitored helium_level()
# method, in percent (0-100), verified the same way as the temperature key.
_HELIUM_LEVEL_KEY = "level_meter_helium_level"

_HELIUM_CONSUMPTION_WINDOW_S = 7200.0
_HELIUM_CONSUMPTION_RATE_LIMIT_PCT_PER_HOUR = 5.0


def _helium_consumption_normal_predicate(rate_limit_pct_per_hour: float) -> Predicate:
    """Build the `helium_consumption_normal` predicate for one setup's threshold.

    Units are percent-per-hour, matching `helium_level()`'s own percent
    reading directly — a litres-per-hour variant would need
    `cryogenics.helium_volume_l` (see `Station.read_cryogenics_config()`),
    which is optional and setup-specific, whereas percent is always
    available.

    The rate comes from the windowed series' two endpoints
    (`series[0]`/`series[-1]`), never from `KeySummary` (which has no
    endpoint *values*, only endpoint *timestamps* — see the module
    docstring) and never by re-deriving anything `summarize()` already
    gives exactly. On a window short enough to stay on the raw tier
    (`trend_history.pick_tier`, up to 24 h — this check's default 2 h
    window always qualifies) the endpoints are true instantaneous samples;
    if this check is ever evaluated with a longer window (e.g. the
    troubleshoot CLI's `--window` override), the endpoints become bucket
    *means* instead, which the message says explicitly rather than
    presenting a mean as if it were a reading.
    """

    def predicate(
        summaries: Mapping[str, KeySummary], series: WindowedSeries, window_s: float
    ) -> CheckOutcome:
        no_data = no_data_outcome(summaries, [_HELIUM_LEVEL_KEY])
        if no_data is not None:
            return no_data
        points = series[_HELIUM_LEVEL_KEY]
        summary = summaries[_HELIUM_LEVEL_KEY]
        if len(points) < 2:
            evidence: dict[str, object] = {
                "count": summary.count,
                "window_s": window_s,
            }
            return CheckOutcome(
                passed=None,
                message=(
                    f"Cannot tell — only {len(points)} helium-level sample(s) in the "
                    f"{window_s / 3600.0:.2g} h window; need at least two to compute a rate."
                ),
                evidence=evidence,
            )
        t0, v0 = points[0]
        t1, v1 = points[-1]
        elapsed_h = (t1 - t0) / 3600.0
        if elapsed_h <= 0.0:
            evidence = {"t0": t0, "t1": t1, "window_s": window_s}
            return CheckOutcome(
                passed=None,
                message=(
                    f"Cannot tell — the first and last helium-level samples in the "
                    f"window share a timestamp ({t0}); cannot compute a rate."
                ),
                evidence=evidence,
            )
        # Positive rate = falling level = consumption.
        rate_pct_per_hour = (v0 - v1) / elapsed_h
        endpoint_note = (
            f" (endpoints are {summary.tier!r}-tier bucket means, not instantaneous "
            "samples, on this longer window)"
            if summary.tier != "raw"
            else ""
        )
        evidence = {
            "level_start_pct": v0,
            "level_end_pct": v1,
            "elapsed_h": elapsed_h,
            "rate_pct_per_hour": rate_pct_per_hour,
            "rate_limit_pct_per_hour": rate_limit_pct_per_hour,
            "tier": summary.tier,
            "window_s": window_s,
        }
        too_fast = rate_pct_per_hour > rate_limit_pct_per_hour
        verdict = "high" if too_fast else "normal"
        message = (
            f"Helium consumption {verdict}: level {v0:.3g}% -> {v1:.3g}% over "
            f"{elapsed_h:.2g} h ({rate_pct_per_hour:.3g} %/h, limit "
            f"{rate_limit_pct_per_hour} %/h){endpoint_note}."
        )
        return CheckOutcome(passed=not too_fast, message=message, evidence=evidence)

    return predicate


def declared_checks(config: Mapping[str, float]) -> tuple[TrendCheck, ...]:
    """Return every trend check this setup should run.

    The standard's single registration point: the trend-check scheduler
    (`cryosoft.core.trend_check_runner`), the troubleshoot CLI's `trends`
    subcommand, and the conformance test all call this once and iterate
    whatever it returns — none of them special-case a check by name. Adding
    a check means adding a `TrendCheck` literal to the returned tuple here,
    reading its threshold(s) out of `config` (as returned by
    `cryosoft.core.station.read_trends_config()`) rather than hardcoding a
    number (`CLAUDE.md`: constants and limits in config, not code) — no
    other module in this standard needs to change.

    `trend_store_live` is deliberately NOT declared here: it is pull-only by
    design (see its own module, `cryosoft.troubleshoot.engine`) — it exists
    to catch a wedged or crashed application, and a check scheduled on this
    same process's own `QTimer` (`cryosoft.core.trend_check_runner`) cannot
    fire once that process is the thing that is hung, so it would report
    healthy in exactly the scenario it is built for. It is evaluated only
    from the troubleshoot CLI, a separate process reading the store's file
    state from outside.

    Args:
        config: This setup's trend-check config, e.g.
            ``read_trends_config(config_path)``.

    Returns:
        Every trend check this setup should run, in declaration order:
        `sample_temperature_stable`, `helium_consumption_normal`.
    """
    temperature_window_s = config.get("sample_temperature_window_s", _SAMPLE_TEMPERATURE_WINDOW_S)
    helium_window_s = config.get("helium_consumption_window_s", _HELIUM_CONSUMPTION_WINDOW_S)
    return (
        TrendCheck(
            name="sample_temperature_stable",
            keys=(_SAMPLE_TEMPERATURE_KEY,),
            window_s=temperature_window_s,
            severity="advisory",
            predicate=_sample_temperature_stable_predicate(
                std_limit_K=config.get("sample_temperature_std_limit_K", _SAMPLE_TEMPERATURE_STD_LIMIT_K),
                range_limit_K=config.get(
                    "sample_temperature_range_limit_K", _SAMPLE_TEMPERATURE_RANGE_LIMIT_K
                ),
            ),
        ),
        TrendCheck(
            name="helium_consumption_normal",
            keys=(_HELIUM_LEVEL_KEY,),
            window_s=helium_window_s,
            severity="advisory",
            predicate=_helium_consumption_normal_predicate(
                rate_limit_pct_per_hour=config.get(
                    "helium_consumption_rate_limit_pct_per_hour",
                    _HELIUM_CONSUMPTION_RATE_LIMIT_PCT_PER_HOUR,
                ),
            ),
        ),
    )
