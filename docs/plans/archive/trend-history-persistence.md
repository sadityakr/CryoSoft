# Tiered trend-history persistence (raw / 3-min / hourly)

> **ARCHIVED — IMPLEMENTED 2026-07-25.** All seven phases landed on
> `claude/trend-logging` and merged into `develop` (`9e52f1d`). Harness on the
> merged tree: ruff at the one pre-existing skill-template error, 12/12 import
> contracts, 1567 passed / 5 failed / 5 skipped, the failures all pre-existing
> (four from the absent `rtm2` vendor package, one documented load flake).
> Read for rationale, not as a roadmap. The original status line follows.

Status: Phases 0-6 implemented, Phase 7 (housekeeping) in progress.
Planning discussion complete (one independent Opus review, then redesigned
to a live cascading three-tier scheme after further discussion). **Revised
2026-07-25** after a full-codebase agentic survey
(`docs/plans/agentic-instrumentation-framework.md`): added Phase 0
(log-location and gitignore defects), reshaped the reader API for a second
non-human consumer, and folded in UTC/cadence correctness fixes.
Implemented phase by phase, `make check` green at each phase before moving
to the next.

## Problem statement

`MonitorHistory` (`cryosoft/gui/monitor_history.py`) is a pure in-RAM ring
buffer feeding the Monitor window's trend plots, 24h retention, but empty on
every restart. There is no disk-backed history at all today. Separately, the
codebase has several independent per-tick loops over station state producing
different downstream views (the human `"Monitor: ..."` debug line,
`status.jsonl`'s `VIHealth` records, `MonitorHistory`'s own inline
flattening), a real but narrower redundancy than first appeared once traced
through the code.

## Why this is also agentic infrastructure

Per the framework document's module M2 (state observation), CryoSoft today
can tell an agent what the system is doing *right now* but has no queryable
history. "Was the sample temperature stable overnight?" and "did helium drop
faster than usual last week?" are unanswerable. The tiered trend store is
that missing dimension, which means **the GUI plot panel is not its only
consumer** and the read API must be designed for both from the start. Three
consequences, folded into the design below:

- A reader returning ~28,800 raw rows for a 24h window is unusable by an LLM
  client. The primary public API is therefore an aggregate/query surface,
  with raw-row access as the lower-level primitive underneath it (§4).
- Tier selection is a property of the store, not of the GUI's window picker,
  so it lives in the reader module and every client inherits it (§4).
- The measurement-VI exclusion (§7) is a visible quirk for a human and a
  silent correctness trap for an agent, which reads an empty series as "the
  value was zero" rather than "this key is not persisted". The reader must
  distinguish those two cases explicitly.

## Agreed direction

- Persist trend data to disk in **three fixed-resolution tiers**, each with
  bounded retention, so the GUI can show 24h / 1 week / 1 year without either
  blowing up RAM or doing slow synchronous disk reads: **3 s for 24h, 3 min
  for 1 week, 1 h for 1 year**. Standard RRD/Graphite/Prometheus
  cascading-downsample pattern, not a novel scheme.
- Downsampling happens **live, incrementally, once per tick** (a small
  in-memory accumulator per tier, flushed on bucket boundaries), not as a
  batch job that reads and rewrites old raw files. This avoids any startup
  backlog-reprocessing risk and avoids large synchronous reads when a UI
  window changes, both real problems in an earlier single-tier plus
  batch-compaction design that an independent review caught.
- The canonical per-tick data source is `Station.last_state_flat()`
  (`core/station.py:523`, already exists) — system/level/switch VIs only,
  measurement VIs excluded, matching what `core/procedure.py` already uses
  for HDF5 sweep columns. No new flattening function.
- Retention is enforced entirely by `logging.handlers.TimedRotatingFileHandler`'s
  `backupCount`, mirroring how `status.jsonl` already works today
  (`core/logging_config.py:38-47`), no custom deletion/compaction code.
  Accepted consequence: retention is measured in *files*, not wall-clock
  time, so an app that is closed for a week spans more calendar time per
  file than the nominal figure. Acceptable for an always-on cryostat.

---

## Phase 0 — Log-location and gitignore defects (new, blocking)

Two verified defects that this plan would otherwise amplify.

### 0a. Rotated JSONL files are not gitignored, and this repo is public

`.gitignore:40-42` carries `cryosoft/logs/*.log`, `cryosoft/logs/*.log.*`,
and `cryosoft/logs/*.jsonl`. There is no `*.jsonl.*`. The `.log` case was
already patched (line 41); the `.jsonl` equivalent never was. Result:

```
$ git ls-files cryosoft/logs/
cryosoft/logs/status.jsonl.1     # 10.5 MB, tracked
cryosoft/logs/status.jsonl.2     # 10.5 MB, tracked
```

21 MB of station telemetry is already committed. `TimedRotatingFileHandler`
makes this worse in kind: backups are named `trend_history_raw.jsonl.2026-07-25`,
a *new filename every day*, matched by neither pattern. Three tiers would
begin committing roughly 9 MB/day into a public repository.

**Fix:** add `cryosoft/logs/*.jsonl.*` to `.gitignore`; `git rm --cached`
the two tracked backups (leave the working-tree files alone, they are live
data).

### 0b. The log directory is inside the OneDrive-synced tree

`setup_logging()` defaults to `Path(__file__).parent.parent / "logs"`
(`core/logging_config.py:27-30`), i.e. `cryosoft/logs/`, which currently
holds 56 MB inside the OneDrive-synced project folder. The plan's residual-risk
note rates the Windows rollover `PermissionError` as low severity, mitigated
by "readers opening/reading/closing quickly". That mitigation does not apply,
because the process holding the handle is OneDrive, not a reader. Adding
three files rotating daily multiplies rollover events, and continuous 3 s
writes inside a synced folder also risk conflict-copy files
(`trend_history_raw-DESKTOP.jsonl`) that a naive glob would parse as real data.

**Fix:** a single log-directory resolver, with precedence:

1. explicit `log_dir` argument (unchanged, used by tests),
2. `CRYOSOFT_LOG_DIR` environment variable,
3. `%LOCALAPPDATA%\CryoSoft\logs` on Windows / `~/.local/state/cryosoft/logs`
   elsewhere,
4. `cryosoft/logs/` as final fallback.

It must live in `core/logging_config.py` as `log_directory() -> Path`.
That module is contract-C1 foundation (imports nothing else in `cryosoft`),
and crucially `cryosoft/gui/app_settings.py` is **not** an option:
`troubleshoot/cli.py:80-81` needs the same resolver and contract C10 forbids
`troubleshoot` from importing `gui`.

Ripple, all in scope for this phase:

- `troubleshoot/cli.py:_transcript_dir()` delegates to `log_directory()`
  instead of `Path(cryosoft.__file__).parent / "logs"`.
- The `--log` default and its help text (`troubleshoot/cli.py:451,509`) and
  the "no log found" hint (`status_reader.py:125`) stop hardcoding the path
  and report the resolved one.
- `.claude/skills/troubleshoot-runtime/SKILL.md` and
  `.claude/skills/setup-supervisor/SKILL.md` document the resolver and the
  env-var override rather than a literal path.
- No migration of existing files. Logs are disposable; the new location
  simply starts empty. Say so in the docstring so nobody writes a migrator.

**Tests:** `test_logging_config.py` (new): precedence order of all four
resolution steps with a monkeypatched environment; `log_directory()` is pure
and creates nothing. Extend `test_troubleshoot_cli.py` to assert the CLI
resolves through `log_directory()`.

---

## Design

### 1. Four parallel JSONL loggers (`core/logging_config.py`)

| Logger name | File | Rotation | Retention |
|---|---|---|---|
| `cryosoft.status` | `status.jsonl` | **daily, `utc=True`** (was size-based) | `backupCount=7` |
| `cryosoft.trend_raw` | `trend_history_raw.jsonl` | daily, `utc=True` | `backupCount=2` |
| `cryosoft.trend_3min` | `trend_history_3min.jsonl` | daily, `utc=True` | `backupCount=8` |
| `cryosoft.trend_hourly` | `trend_history_hourly.jsonl` | weekly (`when="W0"`), `utc=True` | `backupCount=53` |

Each: `propagate=False`, JSON-only formatter (`"%(message)s"`),
idempotency-guarded like the existing `status_logger` setup.

Two changes beyond the original plan:

- **`status.jsonl` moves from `RotatingFileHandler` (10 MB × 3) to daily
  time-based rotation.** Its time coverage is currently an accident of VI
  count and tick rate; "the last 24 h of operational status" is the single
  most useful thing a debugging agent (or human) can ask for, and today it
  is not a guarantee. This also makes its backup names
  (`status.jsonl.2026-07-25`) consistent with the new tiers and with the
  Phase 0a ignore pattern. No reader change is needed: `status_reader.py`
  already globs and tolerates missing files.
- **`utc=True` on every handler.** Records carry `time.time()` epochs while
  `TimedRotatingFileHandler` defaults to local time. Left mixed, a DST shift
  produces a duplicated or missing rotation boundary once or twice a year,
  which in a tier meant to hold a full year is a real defect and is free to
  avoid now. Bucket alignment itself (§2) is epoch-modular and therefore
  already UTC-correct.

### 2. `cryosoft/core/tiered_trend_logger.py` (new, Qt-free)

`TieredTrendLogger.record(flat: dict[str, float], timestamp: float, orch_state: str | None = None) -> None`:

- Always writes one raw-tier line immediately:
  ```json
  {"t": 1753401600.123, "s": "RAMPING", "v": {"magnet_z_get_field": 1.5, ...}}
  ```
  Values are **nested under `"v"`**, not spread at top level, so no flat key
  can ever collide with `"t"`/`"s"` and the three tiers stay structurally
  parallel. `"s"` (orchestrator state) is omitted when `orch_state is None`.
  It is one short string per tick and makes the raw tier self-sufficient for
  "what was the system doing when this happened" without a timestamp join
  against `status.jsonl`.
- Updates a pending 3-min bucket accumulator (`{key: [min, max, sum, sumsq, count]}`),
  aligned by `timestamp - (timestamp % interval)` on epoch seconds (inherently
  UTC-aligned). On crossing into a new bucket, flushes the closed bucket and
  resets:
  ```json
  {"t": 1753401600.0, "n": 60,
   "v": {"magnet_z_get_field": {"min": 1.4, "max": 1.6, "mean": 1.5,
                                "std": 0.05, "count": 60}}}
  ```
- Same pattern for a 1-hour accumulator → `cryosoft.trend_hourly`.
- **`count` is written per key**, not just the bucket-level `n`. The
  accumulator already tracks it and discarding it makes the tiers
  non-composable: mean-of-means is wrong at unequal counts, which is exactly
  what a query spanning three days of 3-min buckets must do. Per-key rather
  than bucket-level because a key can appear mid-bucket when a VI comes
  online. `std` is written too (derived from `sumsq` at flush): for "was it
  stable", std is far more informative than min/max, which are
  outlier-dominated. Re-aggregating `std` across buckets is exact, not
  approximate: each bucket's `std` is a population standard deviation
  computed from exact sums, so its second moment about zero is exactly
  recoverable from `(mean, std, count)`, and the law of total variance
  reconstructs the exact combined variance from the summed second moments
  and counts. `mean`/`min`/`max`/`count` recombine exactly for the same
  reason (`mean` via count-weighting).
- **Raw-tier write cadence is decoupled from the tick.** A `min_raw_interval_s`
  constructor argument (default `1.0`) skips a raw write that arrives sooner
  than that since the last one; the accumulators still see every sample. The
  tick interval is config (`monitor.yaml: tick_interval_ms`), so at 500 ms
  the raw tier would otherwise grow 6× past the ~9 MB/day the retention math
  assumes. Aggregate tiers are unaffected by construction.
- Accepted gap: a crash/restart mid-bucket loses that one in-progress 3-min
  or 1-hour bucket (not corrupted, just never flushed). The raw tier still
  has the underlying points for anything within its retention.

Orchestrator's tick gains one call next to `_update_operational_status()`:
`self._tiered_trend_logger.record(self._station.last_state_flat(), time.time(), self._state.name)`,
inside a non-fatal try/except exactly like `_update_operational_status`. The
existing human debug line is left untouched: it needs measurement VIs and
string fields that `last_state_flat()` deliberately excludes.

### 3. `cryosoft/core/trend_history.py` (new, Qt-free)

Named `trend_history.py` rather than `trend_history_log.py`: it is now a
query surface over the store, not only a log parser.

```python
TIERS: dict[str, TierSpec]                    # name -> interval_s, logger, filename, retention_s

def pick_tier(window_s: float) -> str         # single home for tier selection
def persisted_keys(log_dir, tier) -> set[str]

# low-level primitive
def read_tier(log_dir, tier, window_s, now=None) -> list[tuple[float, dict]]

# primary API
def read_window(log_dir, keys, window_s, now=None) -> dict[str, list[tuple[float, float]]]
def summarize(log_dir, keys, window_s, now=None) -> dict[str, KeySummary]
def find_crossings(log_dir, key, threshold, window_s, direction="below", now=None) -> list[float]
```

- `read_tier` globs the tier's dated rotated files plus the live undated
  file, parses line-by-line, skips (never aborts on) a corrupt or truncated
  line, and returns oldest-first. Files whose name suggests a sync conflict
  copy are skipped (§Phase 0b).
- `pick_tier` maps a window to a tier (≤24h raw, ≤1w 3-min, else hourly).
  This decision lives here, not in the GUI's `TIME_WINDOWS`, so the plot
  panel, `cryosoft.ctl`, and any future gateway tool all inherit it instead
  of reimplementing it.
- `read_window` returns plot-ready `(t, value)` series, taking `mean` from
  aggregate tiers, and is what the GUI uses.
- `summarize` returns per key `{min, max, mean, std, count, first_t, last_t,
  tier, persisted}` and is the agent-facing entry point: evidence, not a
  28,800-row dump. Same shape and rationale as the framework plan's Phase 2
  `data_reader.summary_stats()`. On an aggregate tier every field, including
  `std`, is an exact recombination of the underlying buckets (law of total
  variance from each bucket's `mean`/`std`/`count`, §2), not an approximate
  pooled estimate.
- `find_crossings` returns timestamps where the series crossed a threshold,
  the one query beyond aggregation that operational reasoning actually needs
  ("when did helium last go below 30 %").
- Every function reports `persisted=False` for a requested key absent from
  the tier's `persisted_keys()`, so a caller can tell "no data in this
  window" from "this key is never written to disk" (§7).

### 4. GUI wiring

- `MonitorHistory.record_flat(flat, timestamp=None)`: same ring-buffer append
  as `record()`, for an already-flat dict. `record()` unchanged, still used on
  the live `states_updated` path (still includes measurement VIs, see §7).
- `TrendsQuadrant.__init__` replays `read_window(...)` at startup to rehydrate
  the in-RAM buffer.
- `trend_plot_panel.TIME_WINDOWS` gains `("7 d", 604800.0)` and
  `("1 y", 31536000.0)`. Windows ≤24h read RAM as today; longer windows call
  `read_window()`, which picks the tier itself (~3,840 lines for a week,
  ~8,760 for a year, both small enough to read synchronously).

### 5. Explicitly out of scope

- `build_operational_status()` and the content of `status.jsonl`. Only its
  **handler** changes (§1); its schema and readers do not.
- `core/procedure.py`'s HDF5 sweep-column writes via `last_state_flat()`,
  different trigger and cadence.
- The human `"Monitor: ..."` debug line.

### 6. Why this is a second file rather than an extension of `status.jsonl`

Worth recording, because it is the obvious objection. `status.jsonl`'s `vis`
list carries **one `value` per VI**, so it is lossy for multi-field VIs: a
level meter's helium and nitrogen readings collapse to a single number.
`last_state_flat()` keeps every numeric field of every non-measurement VI.
The raw trend tier is therefore the *more complete* historical state record
of the two, which both justifies the separate file and is the reason the
better query API belongs on this one.

### 7. Known, accepted asymmetries (two, running in opposite directions)

**Asymmetry 1 — measurement VIs (documented, deliberate).**
`MonitorHistory.record()` (live path) includes measurement VIs; all three
disk tiers do not, because `last_state_flat()` excludes them
(`core/station.py:535-536`). A trend panel on a measurement-VI key works
live but goes empty after a restart or on a >24h window. Deliberate and
tested. The reader's `persisted` flag (§3) is what keeps this from becoming
a silent wrong answer for a non-human caller.

**Asymmetry 2 — boolean fields (accepted during Phase 7 housekeeping, NOT
deliberately designed).** `last_state_flat()` has no `bool` guard: it keeps
anything passing `isinstance(value, (int, float))`, and `bool` is an `int`
subclass, so a boolean `@monitored` field (e.g.
`SwitchMatrixVI.hot_switching_enabled`) is coerced to `1.0`/`0.0` and IS
persisted to the raw tier (and folds into the aggregate tiers too).
`MonitorHistory.record()` explicitly excludes `bool` values, so the same
field never enters the live in-RAM history. User-visible consequence: the
opposite direction of Asymmetry 1 — a trend panel *gains* a key after a
restart or on a disk-backed window that was unavailable while running
live, rather than losing one. Neither `last_state_flat()` nor
`MonitorHistory.record()` should be changed to close this gap without an
explicit decision: `last_state_flat()` also feeds HDF5 sweep columns, so
changing its filtering would alter data files, and `MonitorHistory.record()`'s
bool exclusion may be relied on elsewhere. Pinned by
`test_l2_station.py::test_last_state_flat_coerces_bool_to_float_unlike_monitor_history`
and documented on `MonitorHistory`'s class docstring and the
`core/README.md` row for `tiered_trend_logger.py`.

---

## Phasing

0. **DONE.** **0a** `.gitignore` pattern + untrack the two committed
   `status.jsonl` backups. **0b** `log_directory()` resolver in
   `core/logging_config.py`, troubleshoot delegation, skill/doc path
   updates, tests.
1. **DONE.** `core/logging_config.py`: three trend loggers, `status.jsonl`
   to time-based rotation, `utc=True` throughout. Plus
   `core/tiered_trend_logger.py` and its tests. No orchestrator wiring yet.
2. **DONE.** `core/trend_history.py`: `read_tier`, `pick_tier`,
   `persisted_keys`, `read_window`, `summarize`, `find_crossings`, plus
   tests.
3. **DONE.** Wire `TieredTrendLogger` into `orchestrator.py`'s tick, plus a
   record-shape-pinning test.
4. **DONE.** `MonitorHistory.record_flat()` plus test.
5. **DONE.** `TrendsQuadrant` startup rehydration plus test.
6. **DONE.** `trend_plot_panel.py` `TIME_WINDOWS` and disk-backed
   `refresh()`, plus test.
7. **IN PROGRESS.** README/GLOSSARY housekeeping; removal of plan-document
   citations from code/test comments and docstrings (project now forbids
   them — see the code-reference standard); documenting the second,
   undocumented bool-persistence asymmetry (§7); a skill-path accuracy
   fix; final `make check`.

Phases 0a and 4 touch no shared files and may run in parallel with each
other; everything else is sequential on the phase before it.

## Testing plan

- `test_logging_config.py` (new): `log_directory()` precedence; handler types
  and `utc=True`; idempotent `setup_logging()`.
- `test_tiered_trend_logger.py`: bucket alignment (including a sample landing
  exactly on a boundary), min/mean/max/std/count correctness, per-key count
  when a key appears mid-bucket, `min_raw_interval_s` throttling,
  crash-mid-bucket behaviour.
- `test_trend_history.py`: live undated file included alongside rotated ones;
  corrupt/truncated line skipped, not fatal; conflict-copy filename skipped;
  `pick_tier` boundaries; `summarize` arithmetic against a hand-built fixture;
  cross-bucket mean re-aggregation weighted by `count`; `persisted=False` for
  a measurement-VI key; a tier whose backups have aged out returns a partial
  window without error; `find_crossings` direction handling.
- `test_monitor_history.py`: extended for `record_flat()`.
- Orchestrator test: pins the exact raw-tier JSON record shape, including
  `"v"` nesting and the `"s"` state field.
- `test_trends_quadrant.py` / `test_gui.py`: startup rehydration; `"7 d"` and
  `"1 y"` window round-trips; measurement-VI asymmetry.
- `test_troubleshoot_cli.py`: resolves its transcript/status path through
  `log_directory()`.

## Housekeeping required by project convention

- `core/README.md` and `gui/README.md` module tables updated in the same
  commit as the code that adds each module.
- `MonitorHistory` docstrings updated for `record_flat`.
- `GLOSSARY.md`: "trend tier" and "trend history" entries.
- `.claude/skills/troubleshoot-runtime/SKILL.md` and `setup-supervisor/SKILL.md`
  updated for the resolved log directory (Phase 0b).

## Known residual risk

`TimedRotatingFileHandler.doRollover()` can hit `PermissionError` on Windows
if another handle has the file open during rotation. `logging`'s default
`handleError` swallows this, so the tick survives; worst case is a missed
rotation, not a crash. Phase 0b removes the main cause (OneDrive holding
handles) by moving the log directory out of the synced tree.
