# Safety-hold enforcement gap, and standardizing the diagnostics/agent-tooling surface

**Status: SUPERSEDED 2026-08-04**, by two successor plans that split its two
halves into independent bodies of work: `safety-hold-enforcement.md` (Problem
1) and `trend-checks-temporal-analysis.md` (Problems 2 and 2b). Its diagnosis
of Problem 1 was confirmed against the code and carried forward; its proposed
*fix* was revised (the `magnet_state()`-based re-assertion it suggests would
wedge a magnet mid-ramp — see the successor's §1). Its characterisation of
`core/watchdog.py` as an offline, CLI-facing layer was **wrong**: the layer
runs on every Orchestrator monitoring tick (`orchestrator.py:1632-1634`), and
the CLI is a downstream consumer of verdicts already written to
`status.jsonl`. Kept for the file:line survey and the naming table, which
remain accurate. Read for rationale, not as a roadmap.

**Original status: draft, for discussion.** Written to hand off for review
(with the OPUS agent) before any code changes. Nothing described here has been
implemented; file:line references are to the current `develop` branch as of
2026-08-04.

## Problem 1: a safety hold does not re-enforce itself

### The reported symptom

Helium runs low → `helium_low` trips → the operator acknowledges and
unlocks manual control → sets a magnet field during the unlocked window →
five minutes later the override expires and the UI returns to "held" — but
the field is never driven back down. The magnet just sits at whatever was
last commanded, now merely locked against *further* changes.

### Why: dispatch is edge-triggered on the wrong signal

`Condition`/`Verdict` (`cryosoft/core/conditions.py`) is a pure policy layer:
given the set of currently-active conditions, `decide()` computes which VIs
are held (`Verdict.held_vis`). It holds no memory of its own and issues no
commands — enforcement is entirely the Orchestrator's job, in
`_tick_body()`.

Two independent mechanisms currently do that enforcement, and only one of
them is level-triggered:

1. **Onset dispatch** (`orchestrator.py:1939-1955`). Each tick, a
   `new_keys = current_condition_keys - self._known_condition_keys` diff
   finds conditions that are new *this tick*. For a new hold-severity safety
   condition, every affected VI gets one `standby()` call. This fires
   exactly once per condition onset — never again while the same condition
   (same `Condition.key`, e.g. `"safety:helium_low"`) persists.

2. **Override unlock/expiry** (`Orchestrator.acknowledge()`,
   `orchestrator.py:910-941`; pruning at `orchestrator.py:1966-1979`).
   `acknowledge()` writes `_hold_override_until[condition.key] = now + 300s`
   (`manual_override_timeout_s`, default 300s, `Station.read_safety_config()`).
   `_manual_action_admissible()` (`orchestrator.py:1153-1188`) checks this
   timestamp live and admits manual control while it hasn't lapsed.
   `_tick_body()` prunes expired entries **by expiry only, never because the
   condition itself disappeared** — a deliberate choice, documented in
   `GLOSSARY.md`'s **Hold acknowledge** entry, to stop a flapping flag near
   its threshold from forcing a fresh acknowledge on every re-trip.

The gap: pruning an expired override is not treated as a fresh onset of the
hold. The condition's key was never removed from `_known_condition_keys`
(the flag never cleared), so mechanism 1 never fires again. The only thing
that happens at expiry is manual control being refused going forward — the
VI itself is never told to stand down.

A second, independent gap in the same code: `standby()` failures are
swallowed with no retry —

```python
# orchestrator.py:1950-1955
elif condition.origin == "safety" and condition.severity == "hold":
    for vi_name in sorted(condition.affected_vis or ()):
        try:
            self._station.get_vi(vi_name).standby()
        except Exception:
            logger.exception("standby failed on held VI '%s'", vi_name)
```

If this one-shot call raises, the VI is never re-commanded either, and
nothing about the log line distinguishes "handled" from "silently failed
forever."

### Goal: an actual enforcement invariant, not a one-shot command

The intended behavior — "a held VI is driven to standby and *stays* there
for as long as the hold is active and unacknowledged" — is currently only
approximated by a single edge-triggered command. Two changes close both
gaps, and neither requires a new subsystem; both extend the existing Safety
Hold standard (`GLOSSARY.md`'s **Safety hold** / **Hold acknowledge**
entries) in place:

1. **Fix the edge.** Trigger the dispatch on a derived, per-VI boolean —
   `effective_hold(vi) = vi in held_vis and not override_active(vi)` —
   computed fresh every tick, rather than on `Condition` key novelty. This
   correctly fires on both the original trip *and* override expiry, since
   expiry is exactly `effective_hold` flipping `False → True` again while
   the underlying condition never left `held_vis`.

2. **Add a rate-limited re-assertion.** While `effective_hold(vi)` is
   `True`, if `vi.magnet_state() != "standby"` (the existing accessor —
   `GLOSSARY.md`'s **Magnet state**, already the "is this magnet actually
   idle" source of truth used elsewhere), re-issue `standby()`. Rate-limited
   (e.g. only if the last dispatch was more than N seconds ago, or only
   while not already `"ramping"`) so it doesn't spam commands mid-standby
   sequence. This is what makes the invariant self-healing: it also covers
   the swallowed-exception case above, since a failed `standby()` leaves
   `magnet_state()` un-idle and gets retried on a later tick instead of
   silently never happening again.

Both are small, tick-loop-local changes to the same block of code that
already exists at `orchestrator.py:1939-1955` — not a new module, not a new
GUI surface, not a new vocabulary term.

### Naming note

I initially reached for "watchdog" to describe change 2. That collides with
an existing, narrower, unrelated meaning already in this codebase (see
Problem 2) — recommend NOT calling this a watchdog. It's simply what the
Safety Hold standard's onset dispatch was always supposed to guarantee,
made level-triggered instead of edge-triggered.

## Problem 2: "watchdog"/"diagnostics" name three or four different things

Investigating Problem 1 surfaced that this codebase already overloads
"watchdog" and "diagnostics" across unrelated subsystems, which is worth
untangling on its own before adding anything new:

| Term as used | What it actually is | Where |
|---|---|---|
| `core/watchdog.py`'s `apply_watchdog()` | Pure, read-only stall-detection *judgement* layer. Takes an `operational_status` record, layers a verdict (`RAMP_STALLED`, `STALLED_RUN`) onto it based on whether a ramp's gap is closing or a transient orchestrator state has run too long. Never touches hardware, never calls `standby()` or anything else — output only. | `cryosoft/core/watchdog.py` |
| "safety watchdog" (comment) | Informal shorthand for "the Orchestrator's tick loop must keep polling/enforcing even when idle-stop is refused." Not a named subsystem, no code object corresponds to it. | `cryosoft/gui/monitor_window.py:640` |
| Hold/Acknowledge surface | A **control** surface embedded directly in `MonitorWindow`: notification banner, "Acknowledge & unlock" button, "(mm:ss)" countdown. Driven by `Orchestrator.held_vi_names()` / `conditions()` / `manual_override_expires_at()` — the Condition/Verdict machinery from Problem 1. Exists so the operator can *act*. | `cryosoft/gui/monitor_window.py:481-1553` |
| `DiagnosticsWindow` | A separate popup (Diagnostics menu → "Open Diagnostics…"). **Read-only**: instrument table + alerts view + copy-to-clipboard, fed by `operational_status.py` + `core/watchdog.py`'s stall verdicts. No controls. Answers "is the run stuck?", never "what's blocking my controls?". | `cryosoft/gui/diagnostics_window.py` |
| `troubleshoot/` CLI + `status_reader.py` | Offline/agent-facing consumer of the *same* `operational_status` stream (`status.jsonl`), used by the `troubleshoot-runtime` skill for "why is this taking so long" queries. Shares the `RunFaultCode` vocabulary with `DiagnosticsWindow`. | `cryosoft/troubleshoot/` |

Two things fall out of this table that are worth flagging explicitly:

- **The stall-detection watchdog cannot see the Problem 1 bug at all.**
  `apply_watchdog()` only judges `ramp_status == "RAMPING"` VIs and
  transient orchestrator states. A magnet parked at a nonzero field after an
  override lapsed is not "ramping" — `magnet_state()` reads `"holding"` or
  `"persistent"`, both indistinguishable from normal operation to this
  code. The two subsystems are not just differently named, they cover
  genuinely disjoint failure classes. If visibility into a held-but-not-
  enforced VI is wanted on the diagnostics side too, that's a new
  `RunFaultCode` (e.g. `HOLD_NOT_ENFORCED`) fed by comparing
  `held_vis`/override state against `magnet_state()` — a natural sibling
  addition to `operational_status.py`, but a *separate* piece of work from
  the Problem 1 fix, not the same code path.

- **"Diagnostics" names both a control surface and a read-only one.** The
  Monitor window's hold banner and the standalone `DiagnosticsWindow` are
  reached from the same mental "something's wrong, tell me why" impulse but
  serve different purposes and share no code. Whether that's a problem
  worth fixing (e.g. clearer naming, or surfacing a pointer from one to the
  other) or is fine as two purposely distinct surfaces is an open question,
  not a pre-decided recommendation.

## Problem 2b (open question): how useful are the agent/diagnostics tools, actually?

Distinct from naming, and the harder question: is the current diagnostic
stack — `operational_status.py`, `core/watchdog.py`, `DiagnosticsWindow`,
`troubleshoot/status_reader.py`, the `troubleshoot-runtime` skill — earning
its scope, or is it under-used machinery that adds surface area without
much payoff?

What it's demonstrably good at: telling "ramp is slow" apart from "ramp is
wedged" without a naive fixed timeout, which false-positives on legitimate
long ramps (the persistent-magnet warmup/cooldown sequence is explicitly
exempted via `_NO_MOTION_PHASES`, `core/watchdog.py:19`). That's a real
judgement call an operator or agent would otherwise have to make by eye.

What's unclear, worth raising with OPUS:

- **Coverage is narrow by design** — ramp progress and transient-state
  duration only. It has no opinion on comm faults' severity tiering, safety
  holds, EMERGENCY, or anything in Problem 1/2's territory. Is that
  intentional scoping that should stay narrow, or a sign the "diagnostics"
  umbrella needs to widen to be actually useful for the "is this system
  currently OK" question an agent would ask?
- **Thresholds are unvalidated.** `WatchdogConfig`'s docstring says
  defaults are "deliberately lenient... tighten these against real runs
  once 'that one was stuck' / 'that one was fine' feedback exists"
  (`core/watchdog.py:29-34`). Has that feedback loop ever actually run? If
  not, the stall verdict's real-world false-negative rate is unknown.
- **Usage is unverified.** Is `troubleshoot-runtime` (the skill) or the
  `DiagnosticsWindow` popup actually being reached for during real
  operation, or is this a built-but-dormant capability? Worth checking
  before investing further in it versus consolidating it into something
  simpler.
- **Duplication risk with Problem 1's fix.** Once the Orchestrator itself
  enforces holds correctly (Problem 1), does the diagnostics layer still
  need its own visibility into hold state, or would that just be two
  systems tracking the same fact for two different audiences (operator vs.
  agent)? If both are worth keeping, they should share one source of truth
  rather than compute their own.

## What this report is not

No implementation is proposed here. The purpose is to lay out, precisely
and with line references, what currently exists and where it's
tangled, so the enforcement fix (Problem 1) and any diagnostics
consolidation (Problem 2) can be scoped as separate, deliberate pieces of
work rather than one another's collateral damage.
