# docs/plans/

## Purpose

Design documents and roadmaps. One document per body of work, kept after
the work lands because shipped code cites these documents by path in its
docstrings: they are the "why" that a module docstring is too small to
hold. **A plan is never deleted once code references it.**

Three tiers:

- **Active** (this folder): what is being built now, or next. Plan new work
  from these.
- **`archive/`**: implemented or superseded. Still normative as *design
  references* where code cites them, never as roadmaps. Do not plan new
  work from an archived document.
- **`deferred/`**: designed, agreed, deliberately not scheduled. Revisit
  when its trigger condition arrives.

## How to use this folder

1. **Before planning anything**, read the active documents below. The
   framework document is the single current roadmap.
2. **Before changing a subsystem**, check `archive/` for the document that
   designed it. Its rationale is usually the answer to "why is it built
   this way".
3. **When a plan is fully implemented**, add an `ARCHIVED — IMPLEMENTED`
   banner naming the date, move it to `archive/`, and update every
   `docs/plans/<name>.md` reference in code in the same commit. Verify with:

   ```sh
   grep -rho "docs/plans/[a-z0-9/-]*\.md" cryosoft tests docs GLOSSARY.md README.md \
     | sort -u | while read p; do [ -f "$p" ] || echo "DANGLING $p"; done
   ```

4. **Never move or delete a referenced plan without rewriting its
   references.** This has gone wrong once already: commit `70c910a`
   ("Added docs", 2026-07-22) deleted four plan documents in the same
   commit that added one, leaving 41 dangling docstring references for
   three days. All four were restored from `70c910a^` on 2026-07-25.

## Active

| Document | Status | What it covers |
|---|---|---|
| [`agentic-instrumentation-framework.md`](agentic-instrumentation-framework.md) | **Proposal, current roadmap** | The unified vision. Defines the nine modules an agentic instrumentation system needs (capability manifest, state observation, result access, action verdicts, authority, safe state, cheap evaluation, audit, client surface), scores CryoSoft against them from a full code survey, and sequences seven phases. Supersedes both agent-facing roadmaps in `archive/`. |
| [`trend-history-persistence.md`](trend-history-persistence.md) | In progress | Tiered trend-history persistence (raw / 3-min / hourly). Untracked working document; owned by a parallel session. |

## Deferred

| Document | Trigger to revisit |
|---|---|
| [`deferred/complete-instrument-vis.md`](deferred/complete-instrument-vis.md) | UI groups, the `@query` verb, and mode exclusivity, so one rich VI per physical instrument replaces vendor software at the bench. **Overlaps the framework's Phase 1**: both extend the decorator metadata that generates the GUI and would generate the agent tool schema. Reconcile the two before starting either. |

## Archive

Implemented. Normative as design references, cited from shipped code.

| Document | Landed | Reference count | What it is the reference for |
|---|---|---|---|
| [`archive/cryogenics-logbook.md`](archive/cryogenics-logbook.md) | 2026-07 | 38 | Operations as an L4 class, the Servicing Log framework, cryogenics management, the readiness/next-due contract. The most heavily cited plan in the repository. |
| [`archive/operation-concurrency-and-error-scoping.md`](archive/operation-concurrency-and-error-scoping.md) | 2026-07-22 | 27 | Claims and the single admission predicate, runtime fault tiering, immediate operation finish, hard procedure/operation status separation. |
| [`archive/unified-servicing-log-and-run-recording.md`](archive/unified-servicing-log-and-run-recording.md) | 2026-07-23 | 27 | The flat `servicing` log kind, Recording sidecars, the `run_summary()` hand-off, the sample-change hold phase. |
| [`archive/unified-session-record.md`](archive/unified-session-record.md) | 2026-07 | 6 | The session record format: `schema_version`, bundle-relative data paths, save-failure surfacing, derived Data Directory. |

Superseded or partly built. Read for rationale, not for sequencing.

| Document | Status | Why it is kept |
|---|---|---|
| [`archive/session-management-layer.md`](archive/session-management-layer.md) | Partly implemented | Record model, store, and `SessionManager` shipped. **Its eLab/ELN track is genuinely unbuilt and is not covered by the framework roadmap** (`ElnLink` is unwired scaffolding, there is no `session/eln/`). This is the only design record for that work, so it is the one archived document that still describes live, unscheduled scope. |
| [`archive/session-handling-architecture.md`](archive/session-handling-architecture.md) | Superseded as roadmap | The umbrella session-bundle design. Its deliberately-deferred hardening (store lock, migration harness, snapshot cap, sealing, `runs/` escape hatch) remains the right design if the bundle ever needs it. |
| [`archive/agent-native-architecture.md`](archive/agent-native-architecture.md) | Superseded 2026-07-25 | The Agent Gateway design: roles and action classes, the session envelope, the action feed, probe runs. Carried forward largely intact. Its F0 turned out to be already implemented, and its §8 ordering is replaced. |
| [`archive/agentic-operation-roadmap.md`](archive/agentic-operation-roadmap.md) | Superseded 2026-07-25 | The field record of the 2026-07-22 Keithley 6221 `-221` diagnosis, still the strongest evidence for the safe-state work. Its Phases 1/2/4 became framework Phase 3; its Phases 3a/3b are deferred with reasons. |

## Conventions

- **Status line first.** Every document opens with its status
  (`proposal` / `IN PROGRESS` / `IMPLEMENTED <date>` / `ARCHIVED`). An
  archived document keeps its original status line underneath the archive
  banner rather than rewriting history, per the LOGBOOK convention that
  past entries are never rewritten.
- **Cite by path.** Code references a plan as
  `docs/plans/archive/<name>.md §<section>`, so the section survives edits
  to surrounding prose.
- **Sections are stable.** Code cites section numbers. Renumbering a
  section of a referenced plan is a breaking change to every docstring that
  cites it.
