# Trend checks: named temporal judgements over the trend-history store

**Status: proposal, not started.** Written 2026-08-04. Supersedes the
diagnostics half of `safety-hold-enforcement-and-diagnostics.md`. Its sibling
`safety-hold-enforcement.md` covers the enforcement half; the two are
independent and can be built in either order.

Shipped code must not cite this document (`docs/plans/README.md`).

## The capability this is for

An operator is asleep, or an agent is supervising a run. The question is
"has everything been fine for the last eight hours" and the answer has to be
short, evidence-backed, and cheap to obtain. Concretely:

- Has the sample temperature been stable, or has it been oscillating?
- Has helium consumption over the last two hours been higher than normal?
- Is the measurement still actually progressing, or has the application
  wedged?

None of these are answerable from an instantaneous reading. All three are
judgements over a window, and nothing in the codebase makes one today.

## What already exists, and must not be rebuilt

The data layer for this is complete. `cryosoft/core/trend_history.py` is a
Qt-free reader over the tiered store that `core/tiered_trend_logger.py`
writes every tick:

| Piece | Where | What it gives this work |
|---|---|---|
| Three tiers | `trend_history.py:65-69` | raw (3 s samples, ~3 days), `3min` (~9 days), `hourly` (~1 year). Windows from minutes to a year are already served. |
| `pick_tier(window_s)` | `:123` | Single home for "which tier serves this window". Do not re-decide this. |
| `summarize(log_dir, keys, window_s)` | `:446` | Per-key `KeySummary` with **exact** min/max/mean/std/count, recombined across buckets via the law of total variance (`:368-378`). This is the primary input for every check below. |
| `find_crossings(log_dir, key, threshold, window_s, direction)` | `:485` | Timestamps where a key crossed a threshold. Its docstring already names the use case: "when did helium last drop below 30%". |
| `read_window` / `read_tier` | `:308` / `:196` | Plot series and the raw primitive. Checks should not need these. |

`summarize()`'s docstring (`:449-453`) states the intent outright: "Evidence,
not a raw-row dump: this is what an operator or an LLM agent asking 'was X
stable' or 'what was the range of Y' should call". The module header
(`:8-12`) gives the reason: a reader returning ~28,800 raw rows for a 24 h
window is unusable by an LLM client.

**So the missing piece is judgement, not data, and not storage.** A plan that
adds a new store, a new logger, or a new reader has gone wrong.

## What is missing

1. **No named check.** Nothing turns a `KeySummary` into "this is fine" or
   "this is not fine". Every consumer would have to invent its own
   thresholds.
2. **No thresholds anywhere.** What counts as an unstable temperature or an
   excessive boil-off rate is a property of the setup, so it belongs in
   config (`CLAUDE.md`: constants and limits in config, not in code).
3. **No surface.** `python -m cryosoft.troubleshoot` has eleven subcommands
   (`troubleshoot/cli.py:462-536`) and none of them asks a temporal question.
4. **The one temporal judgement that does exist is disconnected from all of
   this.** `core/watchdog.py` carries a per-VI consecutive-non-closing-tick
   counter to detect a stalled ramp. It is short-horizon (seconds), reads the
   live `operational_status` record rather than the trend store, and its
   thresholds are hardcoded. Phase 1 below cleans it; it is deliberately not
   merged into the check standard (see "Deferred").

## Design

### The Trend check standard

A check is a declaration, not a function someone writes from scratch. Adding
one should mean adding a declaration plus a threshold in config, with no
change to the runner, the CLI, or the GUI. That is this repository's
standards-over-one-off-code principle applied to temporal analysis.

Proposed home: `cryosoft/core/trend_checks.py`. The name pairs with
`trend_history.py` the way a judge pairs with a store, so the layering is
legible from the filename alone. Qt-free and `Station`-free like its
neighbour, so it is unit-testable against a synthetic JSONL directory with no
hardware and no GUI.

Shape (illustrative, settle it in the fork):

```python
@dataclass(frozen=True)
class TrendCheck:
    name: str                 # "sample_temperature_stable"
    keys: tuple[str, ...]     # flat state keys, as in Station.last_state_flat()
    window_s: float           # how far back to look
    severity: str             # severity-ladder rung; see below
    # plus the predicate, as a small named function taking dict[str, KeySummary]

@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str              # human- and agent-readable, cites the numbers
    evidence: dict            # the KeySummary fields the verdict rested on
```

`evidence` is not decoration. A check that says "temperature unstable" without
the std, the window, and the sample count is an assertion an agent cannot
verify, and this repository's first principle is that claims are traceable to
their source.

### Where results go: the advisory rung, already reserved

The System-Condition standard (`core/conditions.py`, `GLOSSARY.md`'s **System
condition** and **Severity ladder**) declares three severities, and
`"advisory"` is described as "a reserved rung; nothing maps to it yet".

**A failed trend check is that rung's first real producer.** Recommended
design: a failing check publishes an advisory-severity `Condition` into the
one existing registry rather than into a parallel store of its own.

Consequences, all of them good:

- One registry answers "what is currently wrong", instead of two systems
  tracking overlapping facts for two audiences. This is precisely the
  duplication the superseded report warned about.
- Zero change to the enforcement layer. Advisory means reported, never
  enforced, so `decide()` ignores it, no VI is held, and no hardware moves.
- The GUI and the CLI inherit it: anything already rendering conditions gets
  trend verdicts without new plumbing.

Open question for the fork, flagged rather than pre-decided: should any check
ever be `"hold"` severity instead (a runaway boil-off arguably should stop a
run)? Start every check advisory. Promoting one later is a one-word change in
its declaration, and that asymmetry is the argument for starting low.

### The noise boundary: recorded and diagnosable, never interrupting

**Settled 2026-08-04.** A trend check must not be able to interrupt the
operator. Many temporal judgements are inherently low-confidence, and a layer
that pushes a banner every time a temperature wobbles trains the operator to
dismiss banners, which costs more than the layer is worth.

The existing surfaces already draw this line, so nothing new is needed to
enforce it:

- `operational_status.py:230` serializes every condition regardless of
  severity, so an advisory trend condition lands in `status.jsonl`
  automatically. That is the persistent history an agent reads after the fact.
- `DiagnosticsWindow` renders `record["alerts"]` (`:216-217`), not
  `record["conditions"]`. A failing check therefore also appends one `alerts`
  line, exactly as the stall detector does. Diagnostics is opt-in and
  read-only, so this is wanted visibility rather than noise.
- The Monitor banner filters `severity == "hold"`
  (`monitor_window.py:1536-1537`) and returns early unless a VI is held.
  Advisory severity is structurally unable to reach it.

That last point is what makes "start every check advisory" load-bearing rather
than merely cautious: advisory is the severity that cannot interrupt. Promoting
a check to `"hold"` is what would put it on the operator's banner, which is the
real reason promotion stays deferred.

One check does not fit this shape. `trend_store_live` exists to catch a wedged
or crashed application, and an in-process timer cannot report that its own
process is hung. It has to run from outside, reading the store's file state, so
it lives in the CLI path only and publishes nothing. That split follows from
what the check detects, not from a preference.

### Cadence

These checks must not run every tick. Their windows are hours; re-evaluating
a two-hour window every second is wasted file I/O on the tick path, and the
tick path is the one place this architecture forbids blocking work.

Proposed: a separate slow timer, default 60 s, interval from config. The
`Orchestrator` owns it (it owns the tick loop and is the only component
allowed to schedule work), but the check functions themselves stay pure and
know nothing about Qt or the Orchestrator.

### The three checks to build first

Chosen because they are exactly the questions that motivated this work, and
because each exercises a different part of the query surface:

1. **`sample_temperature_stable`** — `summarize()` over the temperature key,
   window ~1 h. Fails when `std` exceeds a configured limit, or when
   `max - min` exceeds a configured band. Exercises aggregate recombination.
2. **`helium_consumption_normal`** — boil-off rate over ~2 h, from the level
   key's endpoints or from `find_crossings()`. Fails when the implied rate
   exceeds a configured litres-per-hour (or percent-per-hour) limit.
   Exercises rate-of-change reasoning, which `KeySummary` alone does not give
   — expect to need first/last values, so confirm early whether `summarize()`
   suffices or a small addition is warranted.
3. **`trend_store_live`** — is the store still receiving records at all?
   Fails when the newest record is older than a few multiples of
   `tick_interval_ms`. This is the "is the measurement still running" check,
   and it is the one that catches a wedged or crashed application, which no
   in-process watchdog can ever catch by construction. Deliberately cheap:
   one file mtime plus one record.

Check 3 is the highest-value of the three for the overnight-supervision use
case and the easiest to get right. Build it first.

### Surface

Extend the troubleshoot CLI with one subcommand, following the existing
pattern in `troubleshoot/cli.py:462-536` (every subcommand supports
`--json`):

```
python -m cryosoft.troubleshoot trends [--window 8h] [--json]
```

Output: one line per check, pass/fail, message, and the evidence numbers.
This is the thing an agent calls at 3 AM instead of reading a log.

Note the existing architectural constraint: `troubleshoot/status_reader.py`
deliberately does not import `cryosoft.core` (`status_reader.py:14-16`), so
the CLI stays independent of the running application. Decide in the fork
whether the trends subcommand can import `core.trend_checks` (it is Qt-free
and Station-free, so probably yes) or whether the check declarations need to
be readable without importing `core`. Check the import-linter contracts in
`pyproject.toml` before assuming either.

## Phases

**Phase 1 — clean and rename the existing stall detector.** Mechanical, no
new capability, `make check` proves it.

- `core/watchdog.py` → `core/stall_detection.py`; `apply_watchdog()` →
  `apply_stall_verdict()`; `WatchdogConfig` → `StallConfig`; `WatchdogState`
  → `StallState`; `tests/test_watchdog.py` → `tests/test_stall_detection.py`.
- **Delete `STALLED_RUN`** (`watchdog.py:102-104`). It is a hardcoded 30 s
  timeout on `INITIATING`/`MEASURING`/`SWEEPING`, resting on the assumption
  at `:21` that those states last a single tick. That is an assumption about
  how procedures are written, not a physical fact: a long lock-in time
  constant or a heavily averaged point makes `MEASURING` legitimately exceed
  it. It also needs no cross-tick state, so it contradicts the module's own
  stated design principle (`:22-23`) that a fixed timeout is the wrong
  instrument. Confirm nothing depends on the code string before deleting —
  `troubleshoot-runtime/SKILL.md` names it in prose, and
  `diagnostics_window.py:52-70` maps it for display.
- **Fix `stall_ticks` to be seconds, not ticks.** Today it is `6` ticks
  (`watchdog.py:37`) while `tick_interval_ms` is per-setup config: 1000 ms in
  `sim_real_cryostat`/`a-sample-real-cryostat`, 2000 ms in `12t-cryo`,
  3000 ms in `sim_cryostat`. The same threshold therefore means 6 s, 12 s,
  and 18 s on three setups, and editing `monitor.yaml` silently changes what
  "stalled" means. Take seconds in config, convert to ticks at construction.
- Then move the thresholds into `devices.yaml`, alongside
  `safety.manual_override_timeout_s`. Do the units fix *first*: exposing a
  number in config whose meaning depends on another config value is worse
  than leaving it hardcoded.
- Scope, measured: 71 occurrences of "watchdog" across 18 files.
  `pyproject.toml` has none, so no import-linter contract names the module.
  The bulk is `orchestrator.py` (11), the module itself (12), its test (9);
  the rest is prose in `troubleshoot-runtime/SKILL.md` (5),
  `operational_status.py` (3), `superconducting_magnet_persistent.py` (3),
  `core/README.md` (2), `tests/README.md` (2), `rampable.py` (2),
  `GLOSSARY.md` (1), and single mentions in `operation.py`, `cli.py`,
  `diagnostics_window.py`, `test_l3_orchestrator.py`.
- Two exclusions: `docs/plans/archive/` mentions stay untouched (archived
  plans are dated records), and `monitor_window.py:640`'s "safety watchdog"
  comment is the unrelated informal shorthand — it belongs to the sibling
  plan, which gives it a real mechanism to point at.

**Phase 2 — the Trend check standard.** `core/trend_checks.py`, the
`TrendCheck`/`CheckResult` types, the runner, the advisory-`Condition`
publication path, config-driven thresholds, and a conformance test that every
declared check names keys that a shipped config can actually produce.

**Phase 3 — the three checks and the CLI subcommand.** Build
`trend_store_live` first.

**Phase 4 — deferred, do not build here.** Nothing currently speaks
unprompted: a failing check lands in a registry and a log that someone has to
go read. Making it volunteer (a Monitor banner, a desktop notification, a
push to a supervising agent) is a separate decision about notification
policy, and it is the change that would decide whether this layer earns its
keep. Flag it, do not build it.

## Testing

Every phase ships tests with it, per the repository's build-bottom-up rule.

- Phase 1: the eight existing tests in `test_watchdog.py` move and keep
  passing minus the `STALLED_RUN` cases; add one asserting that the
  seconds→ticks conversion gives the same wall-clock behaviour at 1000 ms and
  3000 ms tick intervals. That test is the regression guard for the unit bug.
- Phase 2/3: checks are pure functions over a synthetic tier directory.
  Write JSONL fixtures directly (a stable temperature, an oscillating one, a
  fast boil-off, a store that stopped) and assert the verdict and the
  evidence numbers. No hardware, no Qt, no Station.
- One integration test that a failing check reaches
  `Station.conditions()` as an advisory-severity `Condition` and that
  `decide()` leaves `held_vis` empty for it. That is the test that proves
  advisory really means "no enforcement".

`make check` (ruff, lint-imports, `pytest -m "not hardware"`) gates each
phase.

## Out of scope

- Any change to what the trend store writes or retains. The tiers are
  settled (`archive/trend-history-persistence.md`).
- Merging stall detection into the Trend check standard. They share a shape
  (window → judgement) but differ in cadence (per-tick vs per-minute) and
  data source (live record vs disk store). Unifying them now is speculative
  generality; revisit once three or more checks exist and the common shape is
  observed rather than predicted.
- The notification path (Phase 4).
- Anything in the sibling enforcement plan. A trend check never moves
  hardware.
