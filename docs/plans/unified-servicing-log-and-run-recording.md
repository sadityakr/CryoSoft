# Unified servicing log and per-run parameter recording

Status: APPROVED 2026-07-23 (Aditya) — flat common table, no status field,
symmetric He/LN2 start+end levels, everything editable via revisions. Builds on
`docs/plans/operation-concurrency-and-error-scoping.md` (claims, immediate
finish, `run_summary()` hand-off) and `docs/plans/cryogenics-logbook.md`
(the servicing-log framework). Supersedes, once implemented, the split
between the `cryogenics` log kind and the machine-only `operations` stream.

## Problem statement

Three gaps between the implemented operations framework and the intended
servicing workflow:

1. **The sample change ends before the servicing starts.** Its `step()`
   returns `None` immediately, so the run terminates as soon as the parking
   ramps complete — before the operator opens the cryostat. The physical
   servicing happens outside any run: nothing is recorded, the ready banner
   only appears post-run, and the logged `started_utc`/`finished_utc` cover
   the ramp-down, not the actual sample change.
2. **Backtracking an issue means checking several logs.** A fill is in
   `cryogenics.jsonl`, every operation in `operations.jsonl`, manual notes in
   the former only. There is no single chronological timeline of "what was
   done to the cryostat".
3. **Per-run data has no general home.** The fill's level curve is embedded
   as a JSON string inside its log entry — workable for one bounded curve,
   wrong for "all system parameters recorded during a servicing" (many
   channels over a possibly hour-long run).

## Agreed direction (2026-07-23 discussion)

- Sample change becomes a true servicing run: it holds while the operator
  works, records system parameters throughout, and ends on Finish.
- `cryogenics` + `operations` merge into ONE log (`servicing`), because the
  operator backtracking an issue wants one timeline, not several files.
- Per-run recordings go to per-run sidecar JSON files, referenced from the
  log entry. No HDF5. CSV export is a view over these formats, never a
  migration.

## Design

### 1. Sample-change hold phase (align with the fill's lifecycle)

The fill already has the intended shape (initiate → sample every tick →
operator finishes). The sample change adopts it:

- `initiate()` unchanged (ramp magnets to 0 T, VTI to target, open switch,
  standby measurement VIs).
- `step()` no longer returns `None` immediately: the run stays active in a
  hold phase until `finish_operation()` (the card's Finish click). The
  existing immediate-finish path then applies unchanged (standby plan,
  one-shot postconditions, `run_summary()`).
- `sample()` records the parameter recording (§3) once per `sample_period_s`
  (new config key, `operations.sample_change:` block).
- The ready banner moves from "after the run finished" to "mid-run, once
  every readiness condition holds" for operations that declare a hold phase
  (the card already re-evaluates `readiness_conditions()` every tick; only
  the show-condition changes). The banner text answers "you can open the
  cryostat NOW"; Finish answers "I am done".
- Claims, tolerated flags, postconditions, operator confirmations: all
  unchanged.

### 2. One `servicing` log kind (merges `cryogenics` + `operations`)

One JSONL file per setup — `<sessions_root>/servicing/<config_name>/
servicing.jsonl` — same append-only storage, tolerant reader, and revision
mechanism as today. **Flat common table** (decided 2026-07-23): every entry,
regardless of kind, has exactly the same fields — no kind-specific columns:

| Field | Type | Notes |
|---|---|---|
| `entry_kind` | str | `"helium_fill" \| "sample_change" \| <future operation keys> \| "manual"` |
| `person` | str | who performed it |
| `start_utc` / `end_utc` | str | ISO 8601 UTC; for machine entries the true servicing window |
| `helium_start_pct` / `helium_end_pct` | float | helium level at start/end |
| `ln2_start_pct` / `ln2_end_pct` | float | LN2 level at start/end (replaces the old `ln2_filled` bool — a top-up shows as the level jump) |
| `notes` | str | free text; the recorder appends machine remarks here (abort reason, `unmet: <gates>`) — there is NO `status` field |
| `recording` | str | sidecar filename under `recordings/`, `""` if none |
| `origin` | str | `"machine" \| "manual"` |

- **Levels are stamped for every kind, not just fills**: the recorder
  captures helium and LN2 percent from the latest state snapshot (the same
  source `helium_record.jsonl` samples) at run start and run finish, so a
  sample change records the helium it cost, too. Manual entries default the
  levels to 0.0 and the user fills in what they know.
- **Everything is addable and editable** through the existing entry-revision
  mechanism, for both origins. No per-field locking: revisions are
  append-only, so the original machine-written line is always preserved in
  history — auditability comes from the storage model, not from edit
  restrictions. The per-kind `editable=False` machine-stream concept
  disappears with the `operations` kind itself.
- `CryogenicsRecorder.on_run_finished()` writes ONE entry per run instead of
  two (today: a cryogenics entry for fills plus an operations line for
  everything). Operation params (previously the operations stream's `params`
  JSON) are no longer logged as a column; anything worth keeping goes into
  `notes`.

**What stays separate**: `helium_record.jsonl` (hourly ambient level
history) is a continuous sensor series, not an event log — merging it would
bury the events under thousands of samples. The per-run recording sidecars
(§3) also stay separate files.

**Legacy migration**: a one-time migration (on first store open at the new
schema version, or an explicit script) rewrites existing `cryogenics.jsonl`
and `operations.jsonl` entries into `servicing.jsonl`; a fill's operations
line and cryogenics entry are matched by timestamps and merged into one
entry; originals kept as `.bak`. Old `ln2_filled=True` maps to a
`"LN2 topped up"` note (levels unknown → 0.0); old `status != "done"` and
`params` map into `notes`; old embedded `level_curve` JSON strings are
written out as recording sidecars during migration. The viewer reads
exactly one file afterwards. (Rejected alternative: viewer merges three
files forever — contradicts the single-timeline goal.)

### 3. Per-run parameter recording (sidecar files)

```
servicing/<config_name>/
  servicing.jsonl            # the one log table (one line per entry)
  helium_record.jsonl        # ambient level history (unchanged)
  recordings/<run_id>.json   # full time series for one run
```

- One JSON file per recorded run:
  `{"unix_time": [...], "channels": {"<vi>.<value>": [...], ...}}`.
- Bounded by the fill curve's existing stride-doubling decimation
  (`_MAX_CURVE_POINTS`-style cap), generalised to multi-channel — extract
  the fill's decimator into a small shared recorder helper on
  `OperationBase` (opt-in; an operation declares which channels to record,
  default none).
- Hand-off unchanged: the operation samples in memory, `run_summary()`
  returns the series through the run manifest, `CryogenicsRecorder` writes
  the sidecar and puts its filename in the entry's `recording` field.
- The fill migrates from the embedded `level_curve` JSON-string field to a
  sidecar like every other recording; legacy embedded curves are converted
  to sidecars by the migration (§2), so the unified schema has no
  `level_curve` field at all.

### 4. Viewer + export

- The servicing log page renders ONE chronological table over
  `servicing.jsonl`: filter chips by `entry_kind`, row click opens details
  incl. the recording plot (load the sidecar lazily).
- Two export actions: "Export table as CSV" (one row per entry) and
  "Export recording as CSV" (time column + one column per channel).

## Phasing (each phase lands with green harness)

1. **Schema + store**: the flat `servicing` LogKindSpec, store support
   (add/revise for both origins), migration routine (incl. legacy embedded
   curves → sidecars) + tests over synthetic legacy files. DONE.
2. **Recorder rewiring**: `CryogenicsRecorder` writes single merged entries
   with He/LN2 start+end levels for every run kind, and recording sidecars;
   fill's curve moves to a sidecar. DONE (2026-07-23): `CryogenicsRecorder`
   now writes ONLY `"servicing"` (one entry per finished operation run of
   any kind), `HeliumFillOperation.run_summary()` returns the generic
   `{"recording": {...}, "start_pct": ..., "end_pct": ...}` shape, and
   `cryosoft.main` calls `ServicingLogStore.migrate_legacy()` once at
   startup; the shipped sim configs' `servicing_logs:` lists now declare
   `servicing` instead of `cryogenics`. The legacy `cryogenics`/`operations`
   kinds stay declared (readable, `cryogenics` still manually editable) for
   any not-yet-migrated setup's history, but the recorder never writes them
   again.
3. **Sample-change hold phase**: step() hold, sample() recording, mid-run
   ready banner, config keys, tests (run spans Finish click; recording
   lands; timestamps cover the true servicing window). DONE (2026-07-23):
   `OperationBase` grew a shared decimating multi-channel recorder helper
   (`_record_sample()`/`_recording_dict()`/`_MAX_RECORDING_POINTS`,
   generalising the fill's old `_MAX_CURVE_POINTS`/curve fields —
   `HeliumFillOperation` now uses it, behavior-identical); a new
   `hold_for_operator: bool` class attribute on `OperationBase`
   (`SampleChangeOperation` sets it `True`). `SampleChangeOperation.step()`
   now always returns a `StepPlan` (never `None` on its own), so the run
   holds until `finish_operation()`; `sample()` records the VTI temperature
   plus every magnet's field once per the new `sample_period_s` config key
   (default 10 s, `operations.sample_change:` block) into the shared
   recorder; `run_summary()` hands it off as `{"recording": {...}}`.
   `OperationCard._sync_ready_banner()` shows the banner mid-run, once every
   readiness condition holds, for `hold_for_operator` operations (the fill's
   post-run-only banner is unchanged).
4. **Viewer + export**: unified table, filter chips, recording plot, the
   two CSV exports.

Cross-cutting: GLOSSARY updates (Servicing log entry, Recording, Hold
phase), folder READMEs in the same commits, conformance coverage for
declared recording channels (must be `<vi>.<value>` names resolvable against
the config).

## Explicitly out of scope

- Any change to claims/admission, immediate finish, or the fault model.
- SQLite/HDF5 storage (rejected: event rate is a few entries per week;
  JSONL keeps append-only, sync-safe, greppable properties).
- Recording during procedures (procedures have HDF5 runs already).
