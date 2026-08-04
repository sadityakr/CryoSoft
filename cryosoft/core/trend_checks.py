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
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cryosoft.core.conditions import SEVERITIES, Condition
from cryosoft.core.trend_history import KeySummary, summarize

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


Predicate = Callable[[Mapping[str, KeySummary]], CheckOutcome]


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
        predicate: Pure function from ``{key: KeySummary}`` (exactly the
            `keys` above, over `window_s`) to a `CheckOutcome`. Knows
            nothing about Qt, the Orchestrator, or the Station.

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
    outcome = check.predicate(summaries)
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


def declared_checks(config: Mapping[str, float]) -> tuple[TrendCheck, ...]:
    """Return every trend check this setup should run.

    The standard's single registration point: the trend-check scheduler
    (`cryosoft.core.trend_check_runner`), the future CLI subcommand, and the
    conformance test all call this once and iterate whatever it returns —
    none of them special-case a check by name. Adding a check means adding a
    `TrendCheck` literal to the returned tuple here, reading its
    threshold(s) out of `config` (as returned by
    `cryosoft.core.station.read_trends_config()`) rather than hardcoding a
    number (`CLAUDE.md`: constants and limits in config, not code) — no
    other module in this standard needs to change.

    No check is declared yet: this module ships the mechanism, not the
    checks themselves.

    Args:
        config: This setup's trend-check config, e.g.
            ``read_trends_config(config_path)``.

    Returns:
        Every trend check this setup should run, in declaration order.
        Empty for now.
    """
    return ()
