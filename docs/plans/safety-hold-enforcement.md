# Safety hold enforcement: making a hold a maintained invariant

**Status: proposal, not started.** Written 2026-08-04. Supersedes the
enforcement half of `safety-hold-enforcement-and-diagnostics.md`, whose
diagnosis is confirmed and whose proposed fix is revised below. Its sibling
`trend-checks-temporal-analysis.md` covers the diagnostics half; the two are
independent and can be built in either order.

Shipped code must not cite this document (`docs/plans/README.md`).

## The bug

Helium runs low, `helium_low` trips, the operator acknowledges and unlocks
manual control, sets a field during the unlocked window. Five minutes later
the override expires and the UI returns to "held" — and the field is never
driven back down. The magnet sits at whatever was last commanded, merely
locked against further changes.

Verified against `develop` at 2026-08-04:

- `orchestrator.py:1939-1955` dispatches `standby()` only for keys in
  `current_keys - self._known_condition_keys`.
- `_known_condition_keys` is overwritten at `:1942` before the dispatch loop,
  so a condition that persists is never "new" again.
- Override pruning at `:1975-1980` filters on `now < until` and touches
  nothing else.
- `_manual_action_admissible()` reads the expiry live at `:1165`.

So expiry revokes *permission* and never re-commands *hardware*. A second,
independent gap sits in the same block: a `standby()` that raises is logged
and swallowed (`:1952-1955`) with no retry, so a failed safe-state command
silently never happens again.

## The fix in one sentence

Replace the edge-triggered onset dispatch with a level-triggered invariant:
for as long as a VI is held and not overridden, it is driven to standby and
kept there.

## Design

### 1. A base-derived `standby_status()`, with no VI-author burden

The enforcement loop needs to know whether a held VI is at its safe state,
heading there, or neither. Two rejected approaches, recorded because the
reasoning matters:

- **Reading `magnet_state()` directly from the Orchestrator** (the superseded
  report's proposal). Wrong for two reasons. First, `magnet_state()` exists
  only on the two magnet VIs, not on `MagnetBase` and not on the VI base, so
  the tick loop would hardcode today's coincidence that every held VI happens
  to be a magnet (only `MagnetBase` declares a `safety_concerns()` today,
  `base.py:806`). Second, and worse, `magnet_state()` returns `"ramping"`
  without saying *ramping to what*: a magnet the operator sent to 2 T during
  the unlock window, still in flight when the window lapses, would be
  classified as converging and exempted from enforcement forever.
- **A declared accessor each VI author implements.** Correct but it puts a
  standing obligation on everyone who ever writes a VI, for a fact the base
  can derive.

**Chosen: command provenance, tracked entirely by the base.** The base does
not need to know what the safe state physically is. It needs to know whether
the last motion command this VI received was the standby command, and whether
that command has run its course.

The hook already exists. `base.py:311-313`, inside `__init_subclass__`,
already wraps a directly-defined `standby()`:

```python
standby_method = vars(cls).get("standby")
if callable(standby_method):
    cls.standby = BaseVirtualInstrument._make_detach_wrapper(standby_method)
```

That wrap exists today for the detach-when-idle rule and uses the same
inherited-enforcement idiom as `@control`. Extend it, and add the same
treatment to two more methods:

| Wrapped method | Effect on `self._standby_commanded` |
|---|---|
| `standby()` | Set `True`, **after** the wrapped call returns, so a raise leaves it `False`. |
| `start_ramp()` | Set `False`. Anyone moving the VI elsewhere invalidates the standby. |
| `stop_ramp()` | Set `False`. Frozen mid-ramp is not converging. |

Then, on `BaseVirtualInstrument`:

```python
def standby_status(self) -> str:
    """Whether this VI is at the safe idle state ``standby()`` drives it to.

    "reached"    — at safe idle; nothing to enforce.
    "converging" — standby() is underway and will arrive; do not re-command.
    "away"       — neither; standby() must be re-issued.
    """
    if not isinstance(self, RampableVI):
        return "reached"        # standby() is one instantaneous command
    if not self._standby_commanded:
        return "away"
    return "converging" if self.ramp_status() == "RAMPING" else "reached"
```

No VI author ever writes anything. A measurement VI whose `standby()` just
disables an output reports `"reached"` because it is not `RampableVI`. A new
rampable VI inherits correct behaviour from wraps it never sees.

Three properties worth stating explicitly, because they are why this shape
was chosen:

- **It closes the 2 T hole for free.** `set_field(2.0)` reaches
  `start_ramp(2.0)`, which clears the flag, so the VI reads `"away"` at
  expiry even though `ramp_status()` says `RAMPING`. No tesla, no
  magnet-specific knowledge.
- **It prevents the wedged-magnet failure.** `start_ramp` unconditionally
  rebuilds the generator and consumes its first step
  (`superconducting_magnet.py:105-110`). A naive "re-issue whenever not at
  standby" loop would discard the in-flight generator every tick and the
  magnet would never arrive — the enforcement would become the thing
  preventing the safe state. The `"converging"` rung is what makes that
  impossible.
- **Onset and re-assertion become one code path.** `_standby_commanded` is
  `False` on a freshly built VI, so the first trip of a hold already reads
  `"away"` and dispatches. The onset-diff block at `orchestrator.py:1943-1955`
  is therefore **deleted**, not fixed. This change removes a mechanism.

**Known limitation, to be stated in the docstring:** provenance is not
verification. It knows the command was issued and its ramp finished, not that
the magnet is at zero. A PSU that silently ignores the ramp reports
`"reached"`. `magnet_state()`'s `abs(psu_A) <= 0.01` check
(`superconducting_magnet.py:332-334`) is strictly stronger, and
`standby_status()` stays overridable so a magnet override can be added if a
real incident ever justifies it. Not built here: it would mean either two
copies (the accessor is duplicated on `SuperconductingMagnetVI` and
`SuperconductingMagnetPersistentVI`, it is not on `MagnetBase`) or lifting
`magnet_state()` to `MagnetBase` first, which is a separate refactor.

### 2. Level-triggered enforcement in the tick loop

Enforcement stays in the Orchestrator: it is the sole writer to hardware, and
that is not negotiable. One new private method, replacing the deleted onset
block:

```python
def _enforce_safety_holds(self, verdict: Verdict) -> None:
    """Drive every held, un-overridden VI to standby, and keep it there."""
```

For each `vi_name` in `verdict.held_vis` where
`not self.override_active(vi_name)` (the accessor already exists,
`orchestrator.py:993`) and `vi.standby_status() == "away"`: call `standby()`,
announce it, count a failure if it raises.

The comm-origin branch of the onset diff (the per-VI fault event at
`:1945-1949`) is unaffected and stays exactly as it is. Only the safety-hold
branch is replaced.

### 3. Announcement

Re-assertion moves hardware without the operator asking, which is correct for
a hazard interlock and unacceptable if it happens silently: an operator who
finds the field changed with no explanation learns to distrust the safety
system. Every re-assertion emits:

- `_emit_status()` naming the VI and the condition, and
- an `ErrorEvent` (`core/events.py`, `kind="safety"`), so anything on the
  structured signal sees it rather than only the status line.

The Monitor window already renders an `mm:ss` override countdown. Extend its
text to say what happens at zero, so the re-assertion is predictable before
it occurs rather than explained after.

### 4. Escalation when the invariant cannot be met

If a VI stays `"away"` across N consecutive enforcement attempts, the
invariant is not merely unsatisfied, it is unsatisfiable: hardware is
ignoring the command, or the driver is failing silently. That is a genuine,
currently-undetectable failure. Log `CRITICAL` (the reserved level for safety
events, per `CLAUDE.md`) and emit an `ErrorEvent`. `N` and the retry interval
come from config, alongside `safety.manual_override_timeout_s`.

This also closes the swallowed-exception gap: a `standby()` that raises
leaves the VI `"away"`, so it is retried on the next attempt instead of never
again, and repeated failure now escalates instead of accumulating log lines
nobody reads.

Rate limiting: enforcement attempts must not fire every tick. Gate on a
configured minimum interval between attempts for the same VI. The
`"converging"` rung already prevents the common case, but a VI stuck at
`"away"` would otherwise be re-commanded at tick rate.

### 5. Why this is already general across safety flags

The concern that it should handle "more safety flags later, not just helium"
is already satisfied by construction, and it is worth understanding why so
that nothing gets built to achieve it.

Enforcement keys on `Condition.severity == "hold"` and the condition's
`affected_vis`. It never mentions `helium_low`. The Severity ladder
(`GLOSSARY.md`) already routes any flag: a VI declares a flag and its
severity in `safety_flags`, another VI declares dependence in
`safety_concerns()`, and `Station.update_conditions()` scopes the resulting
condition to exactly the concerned VIs. `helium_low` is simply the only
hold-severity flag declared today (`LevelMeterBase`). A new one is a
declaration on a category base plus a config threshold, and it inherits
enforcement with no Orchestrator change at all.

The one thing that is genuinely not general: `standby()` is the only safe
action. If some future flag ever needs a different safe response, that
belongs as a declaration on the VI, never as a special case in the tick loop.
Say so in the docstring and build nothing for it now.

**Naming:** do not call this a watchdog, even though the sibling plan frees
the word. The term's history in this repository is exactly the confusion
being cleaned up, and this mechanism has a precise name already: it is the
**Safety hold** standard's enforcement, made level-triggered. Add a **Hold
enforcement** entry to `GLOSSARY.md` and use that.

## Phases

Single commit, or two if the base contract is worth landing alone.

1. `standby_status()` on `BaseVirtualInstrument` plus the three
   `__init_subclass__` wraps, with tests. Independently landable: nothing
   consumes it yet, and it changes no behaviour.
2. `_enforce_safety_holds()`, deletion of the onset-diff safety branch,
   announcement, escalation, config keys, GLOSSARY updates.

## Testing

- **The reported bug, as a regression test.** Trip `helium_low`, acknowledge,
  set a field, advance the clock past the override, tick, assert the magnet
  is commanded back to zero. This test fails on `develop` today and is the
  reason the work exists.
- **The 2 T hole.** Same setup, but assert enforcement fires while the
  operator's ramp is still in flight (`ramp_status() == "RAMPING"` and
  `standby_status() == "away"` simultaneously).
- **No wedging.** While a magnet is converging to zero, assert `standby()` is
  not re-issued on subsequent ticks and the ramp completes. This is the test
  that would have caught the naive implementation.
- **Failed `standby()` is retried.** Make `standby()` raise once, assert a
  later tick retries, assert repeated failure escalates to `CRITICAL` plus an
  `ErrorEvent`.
- **Generality.** Declare a second synthetic hold-severity flag on a
  non-magnet VI in a test config and assert it is enforced identically with
  no production-code change. This is the test that proves the claim in §5.
- **Non-rampable VIs.** A measurement VI reports `"reached"` and is never
  re-commanded.
- **Announcement.** Assert the status message and the `ErrorEvent` fire on
  re-assertion.

`make check` gates the work: `ruff check .`, `lint-imports`,
`pytest -m "not hardware"`.

## Documentation to update in the same commit

- `GLOSSARY.md` **Safety hold** and **Severity ladder** both currently say a
  held VI is "stood by once on onset". This work makes that sentence false;
  both must be rewritten to describe the maintained invariant.
- A new **Hold enforcement** entry.
- `GLOSSARY.md` **Hold acknowledge**: its documented pruning rationale is
  unchanged and stays, but note that expiry now re-asserts.
- `cryosoft/virtual_instruments/README.md`: `standby_status()` as part of the
  VI contract, including that authors inherit it and need write nothing.
- `cryosoft/core/README.md`: the enforcement method in the Orchestrator's
  tick description.
- `monitor_window.py:640`'s informal "safety watchdog" comment: reword to
  point at the real mechanism this work creates.

## Out of scope

- A magnet-specific physics-checking `standby_status()` override (see the
  limitation in §1).
- Any change to `_hold_override_until` pruning semantics. Pruning by expiry
  only is a deliberate, documented choice (`GLOSSARY.md`'s **Hold
  acknowledge**) that stops a flapping flag from forcing a fresh acknowledge
  on every re-trip, and this work does not disturb it.
- The `resume_procedure()` gap that **Hold acknowledge** already records as
  known and accepted (a claimed VI moved manually during a paused override).
  Related, deliberately deferred, and would need its own design.
- Everything in the sibling trend-checks plan. In particular, do **not** add
  a `HOLD_NOT_ENFORCED` fault code: it would exist to detect the bug this
  work fixes, by having a second subsystem recompute a fact the Orchestrator
  owns. The correct diagnostic signal is §4's escalation event, published by
  the enforcement loop because it is the only component that knows it tried
  and failed.
