# docs/plans/

## Purpose

Design documents and roadmaps, one per body of work. They record the
reasoning behind a decision at the time it was taken: the alternatives
weighed, the evidence, the trade-off accepted. That is context a module
docstring is too small to hold and a diff cannot recover.

**Shipped code must never cite a document in this folder.** See the
code-reference standard in `CLAUDE.md`. Code and its folder READMEs have to
present the complete picture on their own, to a reader with only the
repository checked out. These documents are written for whoever is *planning*
work, not for whoever is reading a docstring.

Three tiers:

- **Active** (this folder): what is being built now, or next. Plan new work
  from these.
- **`archive/`**: implemented or superseded. Read for rationale, never as a
  roadmap. Do not plan new work from an archived document.
- **`deferred/`**: designed, agreed, deliberately not scheduled. Revisit
  when its trigger condition arrives.

## How to use this folder

1. **Before planning anything**, read the active documents below. The
   framework document is the single current roadmap.
2. **Before changing a subsystem**, check `archive/` for the document that
   designed it. Its rationale is usually the answer to "why is it built
   this way".
3. **When a plan is fully implemented**, add an `ARCHIVED — IMPLEMENTED`
   banner naming the date, move it to `archive/`, and update the index
   tables below in the same commit. Nothing in `cryosoft/` should need
   touching: no code cites these paths. Verify that stays true, and that
   what *does* cite a plan (this index, other plans) is not left dangling:

   ```sh
   # 1. No plan reference in shipped code or tests. Must print nothing.
   grep -rnE "docs/plans|plan §" cryosoft tests --include=*.py
   # 2. No dangling path in the docs that legitimately do cite plans.
   grep -rho "docs/plans/[a-z0-9/-]*\.md" docs GLOSSARY.md README.md \
     | sort -u | while read p; do [ -f "$p" ] || echo "DANGLING $p"; done
   ```

4. **Why the code-reference standard exists.** Commit `70c910a` ("Added
   docs", 2026-07-22) deleted four plan documents in the same commit that
   added one, leaving 41 dangling docstring references for three days. All
   four were restored from `70c910a^` on 2026-07-25 and the references were
   removed from code entirely, which is the durable fix: a citation that
   cannot exist cannot dangle. Check 1 above is what keeps it that way, and
   belongs in `tests/test_conformance.py` so it is enforced rather than
   remembered.

## Active

| Document | Status | What it covers |
|---|---|---|
| [`development-plan-contracts-to-agents.md`](development-plan-contracts-to-agents.md) | **Build sequence, current** | Sequences the work the framework, the instrument-thread plan, the UI-groups brief and the archived eLab design each own, contracts first: A contracts and declarations, B the seam, C the thread move, D results and safe state, E the agent gateway, F panel/MCP/assistant, G eLab. Names the owner document per step, states the assumptions taken from the open decisions, and gives a checkable milestone per phase. Adds no design of its own. |
| [`agentic-instrumentation-framework.md`](agentic-instrumentation-framework.md) | **Proposal, current roadmap** | The unified vision. Defines the nine modules an agentic instrumentation system needs (capability manifest, state observation, result access, action verdicts, authority, safe state, cheap evaluation, audit, client surface), scores CryoSoft against them from a full code survey, and sequences seven phases. Supersedes both agent-facing roadmaps in `archive/`. |
| [`instrument-thread-and-responsive-gui.md`](instrument-thread-and-responsive-gui.md) | **Audit complete, proposal awaiting decisions** | Audits the remote branches for threading work (none exists; the groundwork is four commits on `claude/monitor-responsiveness-procedures-w9o7bv`, trial-merged clean against `main`), folds in that session's 2026-08-08 redline (steps 0.1 and 0.4a shipped, step 0.3's GUI-owned queue advance withdrawn in favour of an engine-pull `next_procedure()` seam), locates where the GUI freezes today (the synchronous `measure()` and the sequential monitor poll inside the tick), and proposes the single hardware thread standard: Orchestrator + Station + drivers on one instrument thread, GUI/session/agent/analysis on the main thread, joined by an `OrchestratorProxy` and frozen `StatusSnapshot`/`StationInfo` types. Phase 0 (the seam), Phase 1 (the move), Phase 2 (the standard). Subordinate to the framework document: records in its §8.1 which questions the framework already settles (channel metadata, eager `validate_run()`, correlated `ActionVerdict`s, `data_reader`, the eLab track, agent transport) and leaves seven open in §8.2. |
| [`agent-operative-architecture-audit.md`](agent-operative-architecture-audit.md) | **Audit, revises sequencing** | Audits the architecture against the Lab Agent Protocol and revises the framework's sequencing: Phase 0 closes the direct write path (`execute_vi_action()` scope, excitation `control_limits`, a public `emergency_standby(reason)`), Phase 3 makes the operational log trustworthy, Phase 4 adds actor and accountability, and D4 fixes the agent transport as out-of-process with no thread in the engine. Its §8 corrections (contract count, the four silent refusals) feed the development plan's Phases A, B5 and E. |
| [`control-ui-groups.md`](control-ui-groups.md) | **Intent brief, for planning** | Lets a VI declare titled UI groups on `@monitored` / `@control` (`group=`, `UIGroup`, `ui_groups`), validated at class creation, rendered as titled boxes on the front panel and emitted by the capability manifest from the same metadata. Presentation and description only: nothing crosses the action queue. Also fixes the six `initiate_measurement` controls that declare no `params=`. Takes the group primitive from `deferred/complete-instrument-vis.md`; feeds the framework's Phase 1 manifest and the instrument-thread plan's `StationInfo`. Three decisions in its §6. |
| [`config-directory-migration.md`](config-directory-migration.md) | **Proposal, not started** | Moves real per-site configs (`12t-cryo/`, possibly `a-sample-real-cryostat/`) out of the git-tracked shipped tier into the already-existing user-config directory; consolidates the duplicated `app_settings.py`/`cli.py` path logic into `cryosoft/core/paths.py`; moves incident reports and `connection_status.json` to a new `data_directory()`. Companion to the `log_directory()` split already landed this session. |
| [`session-tier-and-terminology.md`](session-tier-and-terminology.md) | **Proposal, not started** | Adds a new Session tier (root → session → experiment → data files, 1 user : many sessions) with hard-enforced folder containment and a fixed `measurement_root()`; resolves "session" currently naming four unrelated things (`SessionManager`→`ExperimentManager`, `SessionState`→`FormAutosaveState`, `SessionEnvelope`→`ExperimentEnvelope`, etc.). Three open sub-questions flagged in the doc: the `gui_state.json` session/experiment field split, the machine-settings-file format for the fixed root, and where the manual data-migration note for existing users lives. |
| [`synchronized-secondary-sweep-axes.md`](synchronized-secondary-sweep-axes.md) | **Proposal, not started** | Adds a `secondary_sweep_axes` concept to `SweepMeasureProcedure` (additive to the existing single `sweep_axis`) so a procedure can sweep more than one quantity in lockstep, sharing one step index. Applies it to `TemperatureSweep`: VTI and sample-stage temperature sweep together with independent start/end, instead of sample being held at one constant setpoint. Fixes a latent bug where VTI's VI presence was only checked when `set_vti_temperature` was on, even though the axis readback needs it unconditionally. Sets up (but does not build) the same mechanism for a future multi-magnet/angular `FieldSweep` variant. |

## Deferred

| Document | Trigger to revisit |
|---|---|
| [`deferred/complete-instrument-vis.md`](deferred/complete-instrument-vis.md) | UI groups, the `@query` verb, and mode exclusivity, so one rich VI per physical instrument replaces vendor software at the bench. **Overlaps the framework's Phase 1**: both extend the decorator metadata that generates the GUI and would generate the agent tool schema. Reconcile the two before starting either. |

## Archive

Implemented. Read for the rationale behind a subsystem, never as a roadmap.

| Document | Landed | What it designed |
|---|---|---|
| [`archive/cryogenics-logbook.md`](archive/cryogenics-logbook.md) | 2026-07 | Operations as an L4 class, the Servicing Log framework, cryogenics management, the readiness/next-due contract. The broadest of these by far. |
| [`archive/operation-concurrency-and-error-scoping.md`](archive/operation-concurrency-and-error-scoping.md) | 2026-07-22 | Claims and the single admission predicate, runtime fault tiering, immediate operation finish, hard procedure/operation status separation. |
| [`archive/unified-servicing-log-and-run-recording.md`](archive/unified-servicing-log-and-run-recording.md) | 2026-07-23 | The flat `servicing` log kind, Recording sidecars, the `run_summary()` hand-off, the sample-change hold phase. |
| [`archive/unified-session-record.md`](archive/unified-session-record.md) | 2026-07 | The session record format: `schema_version`, bundle-relative data paths, save-failure surfacing, derived Data Directory. |
| [`archive/trend-history-persistence.md`](archive/trend-history-persistence.md) | 2026-07-25 | The tiered trend-history store: three fixed-resolution disk tiers downsampled live, the reader's aggregate-first query surface and exact cross-bucket recombination, the `log_directory()` resolver, and the two live/disk asymmetries. |
| [`archive/tensormeter-raw-channel-capture.md`](archive/tensormeter-raw-channel-capture.md) | 2026-07-28 | The "raw diagnostic block" convention on `MeasurementInstrumentBase` (`measurement_raw_blocks`, `raw_block_row_counts()`), parallel to the mean/error/array convention, letting `TensormeterRTM2MeasurementVI` save all 44 raw driver channels per reading via a real `(n_loop1, n_loop2, rows, cols)` HDF5 axis (`DataSchema.measurement_blocks`). |
| [`archive/safety-hold-enforcement.md`](archive/safety-hold-enforcement.md) | 2026-08-04 | Makes a safety hold a maintained invariant instead of a one-shot command: a held VI is driven to standby and kept there for as long as the hold is active and unacknowledged. Adds the base-derived `standby_status()` (command provenance tracked by `__init_subclass__` wraps, zero VI-author burden), replaces the edge-triggered onset dispatch with level-triggered `_enforce_safety_holds()`, announces every re-assertion, and escalates when the invariant cannot be met. Fixed the reported bug where a field set during an acknowledge window was never driven back down after the override expired. |
| [`archive/trend-checks-temporal-analysis.md`](archive/trend-checks-temporal-analysis.md) | 2026-08-05 | The judgement layer over the tiered trend-history store: named, config-thresholded checks answering "has this been behaving over the last N hours" (temperature stability, helium consumption rate, store liveness) for an agent or a sleeping operator. Publishes failures on the Severity ladder's `advisory` rung, through `Station.publish_conditions()`, so there is one condition registry rather than two. Also renamed the stall detector (`core/watchdog.py` → `stall_detection.py`) and fixed its threshold, which had been expressed in ticks against a per-setup tick interval. |
| [`archive/raw-block-channel-plot-columns.md`](archive/raw-block-channel-plot-columns.md) | 2026-07-28 | Makes every raw diagnostic block channel independently plottable in the Procedure window's live trend plots via a row-mean scalar reduction at the procedure layer (`SweepMeasureProcedure._raw_block_channel_columns`/`measure()`), with zero change to the block's own `(n_loop1, n_loop2, rows, cols)` HDF5 shape. Narrows `tensor_component`'s documented role (which component is *analyzed*, not which is *displayed*). |

Superseded or partly built. Read for rationale, not for sequencing.

| Document | Status | Why it is kept |
|---|---|---|
| [`archive/session-management-layer.md`](archive/session-management-layer.md) | Partly implemented | Record model, store, and `SessionManager` shipped. **Its eLab/ELN track is genuinely unbuilt and is not covered by the framework roadmap** (`ElnLink` is unwired scaffolding, there is no `session/eln/`). This is the only design record for that work, so it is the one archived document that still describes live, unscheduled scope. |
| [`archive/session-handling-architecture.md`](archive/session-handling-architecture.md) | Superseded as roadmap | The umbrella session-bundle design. Its deliberately-deferred hardening (store lock, migration harness, snapshot cap, sealing, `runs/` escape hatch) remains the right design if the bundle ever needs it. |
| [`archive/safety-hold-enforcement-and-diagnostics.md`](archive/safety-hold-enforcement-and-diagnostics.md) | Superseded 2026-08-04 | The combined report that split into the two documents above it. Its diagnosis of the enforcement gap was confirmed and carried forward; its proposed fix was revised (the `magnet_state()`-based re-assertion it suggests would wedge a magnet mid-ramp), and its characterisation of the stall detector as an offline, CLI-facing layer was wrong. Kept for the file:line survey and the naming table. |
| [`archive/agent-native-architecture.md`](archive/agent-native-architecture.md) | Superseded 2026-07-25 | The Agent Gateway design: roles and action classes, the session envelope, the action feed, probe runs. Carried forward largely intact. Its F0 turned out to be already implemented, and its §8 ordering is replaced. |
| [`archive/agentic-operation-roadmap.md`](archive/agentic-operation-roadmap.md) | Superseded 2026-07-25 | The field record of the 2026-07-22 Keithley 6221 `-221` diagnosis, still the strongest evidence for the safe-state work. Its Phases 1/2/4 became framework Phase 3; its Phases 3a/3b are deferred with reasons. |

## Conventions

- **Status line first.** Every document opens with its status
  (`proposal` / `IN PROGRESS` / `IMPLEMENTED <date>` / `ARCHIVED`). An
  archived document keeps its original status line underneath the archive
  banner rather than rewriting history, per the LOGBOOK convention that
  past entries are never rewritten.
- **One plan may cite another**, by path and section
  (`docs/plans/archive/<name>.md §3`). These documents are a connected set
  and citing across them is how a superseding plan credits what it replaces.
  Keep such paths current when a document moves; check 2 above finds the
  strays.
- **Shipped code cites none of them.** A plan reference in `cryosoft/` or
  `tests/` is a defect, not a style preference: the citation outlives the
  document and tells a reader nothing without it. Name the concept inline
  and point at `GLOSSARY.md`, the folder `README.md`, or the owning class.
  Vendor manual sections are the one durable exception, and belong in the
  driver implementing them.
