# Operation concurrency and per-instrument error scoping

Status: design agreed 2026-07-21 (Aditya + Claude); rev. 2 same day
(immediate finish, hard ProcedureWindow separation). IMPLEMENTED — all four
phases landed on branch `claude/ops-concurrency` (2026-07-21/22), each with
a green harness. Known follow-up: `operations_panel.py` still passes the
retired `data_directory` kwarg into the fill factory (silently ignored).
Builds on `docs/plans/cryogenics-logbook.md` (the operations design). This plan
does not change the operation/procedure class split; it changes what an active
run blocks and what an error blocks.

## Problem statement

Three operator-facing problems, one root cause each:

1. **An operation locks the whole instrument.** While a helium fill runs, the
   Orchestrator is not IDLE, so `submit_vi_action()` refuses every manual
   front-panel action, including setting the VTI temperature, even though the
   fill only involves the level meter. Root cause: admission is gated on the
   single global state machine, not on what the run actually uses.
2. **Errors lock the whole instrument.** A stale sample thermometer parks the
   state machine in global ERROR and every instrument's controls die with it.
   Root cause: per-VI fault *detection* exists (Station stale/disconnected
   counters) but the *consequence* is always global; `error_occurred(str)`
   carries no instrument identity, and acknowledgment lives on the
   ProcedureWindow instead of where the operator watches (Monitor).
3. **Operations feel like procedures in the UI.** Ramp/status chatter lands in
   the ProcedureWindow log, Finish blocks on postcondition gates for up to
   `postcondition_timeout_s = 600` s with no feedback, and the helium fill
   writes an HDF5 run file nobody wants as a data artifact.

## Agreed decisions

- **Concurrency scope: manual controls only.** Manual VI actions are admitted
  during a running operation when the target VI is not claimed by it.
  Procedures remain exclusive (no two-lane run scheduling in this iteration);
  the claim model is designed so a second lane can be added later without
  redesign.
- **Error scope: per-VI faults + Monitor acknowledge.** Comm/stale faults
  quarantine only the affected VI. Safety flags (quench, helium_low) stay
  global EMERGENCY with the one-shot `standby_all()`; only the surface moves
  (structured events, acknowledge on the Monitor).
- **Finish is immediate (rev. 2).** Clicking Finish ends the operation now:
  the standby/parking commands are dispatched and the run terminates without
  any blocking postcondition phase. Postconditions are evaluated exactly once
  at finish and unmet ones are surfaced as a warning (operation card +
  cryogenics log record), never as a wait. The 600 s
  `postcondition_timeout_s` blocking phase is removed.
- **ProcedureWindow is for procedures only (rev. 2).** No operation run may
  drive any ProcedureWindow element: no status lines, no progress, no
  state-driven widgets. Operation status lives on the operation's card in the
  Monitor window.
- **Helium-fill level curve moves to the session cryogenics log.** The fill
  stops creating a DataManager/HDF5 file.

## Design

### 1. Claim-based admission (single tick, single writer, unchanged)

New hook on both run contracts (`BaseProcedure` and `OperationBase`):

```python
def claimed_vi_names(self) -> set[str] | None:
    """VIs this run owns. None (default) claims every system VI."""
```

- Default `None` = claim everything: nothing gets less safe by omission.
  Narrow claims are an explicit per-class opt-in
  (`HeliumFillOperation` → `{level meter}`; `SampleChangeOperation` → its
  temperature/needle-valve VIs, decided at implementation time).
- Orchestrator captures `_active_claims` in `_start_run()` and clears it on
  every run-teardown path (finish, abort, fail, emergency).
- `submit_vi_action()` admits an action while a run is active iff the target
  VI is not in `_active_claims`, the VI is not faulted (§3), and the state is
  not ERROR/EMERGENCY (existing EMERGENCY manual-override carve-out
  unchanged). Refusals name the owning run:
  `"level_meter is claimed by 'Helium fill'"`.
- The `_tick_body()` GUI-action drain gate mirrors the same predicate exactly,
  as today, so no queued action waits without a verdict.
- Manual *orchestrator ramps* (the GUI-initiated RAMPING mode) stay blocked
  while any run is active: the state machine slot is occupied. One-shot
  setpoint controls (e.g. ITC `set_temperature`) are the use case and they
  pass. This asymmetry is documented on the front panel refusal message.
- Unchanged safety invariants: all writes still flow through the one tick
  (no new thread, no direct GUI→VI path), `control_limits` still enforced in
  the VI base, the per-tick global safety check still runs, session-envelope
  target checks still apply to manual actions.

### 2. Operation UX: immediate finish, hard status separation

- **Immediate finish.** `finish_operation()` transitions the run to its
  standby phase on the next tick: dispatch the operation's `standby()` plan
  (parking/safe-off commands), evaluate `postcondition_gates()` exactly once,
  then end the run. Unmet postconditions do not block; they are reported as a
  warning on the operation card and recorded in the run record / cryogenics
  log (`postconditions_unmet: [...]`). The blocking postcondition sub-phase
  and `postcondition_timeout_s` are removed from the operation contract.
  The card still flips to a disabled "Finishing…" state for the (short)
  interval until the terminal signal arrives.
- **Hard status separation.** Orchestrator status/progress/measurement
  signals carry the run identity they already have in the run manifest
  (`run_id`, `kind`). ProcedureWindow ignores every signal belonging to
  `kind == "operation"` (status log, progress bar, plots, state-driven
  widgets). The active OperationCard shows the operation's own status line.

### 3. Per-instrument fault model

Three failure tiers by blast radius:

| Tier | Trigger | Consequence |
|---|---|---|
| VI fault | comm error streak, stale, disconnected | Quarantine that VI only |
| Run failure | active run's claimed VI faults, gate timeout, plan rejection | Run ends "failed"; instruments stay usable; state returns to IDLE |
| Safety flag | `evaluate_safety()` flag trips | Global EMERGENCY, one-shot `standby_all()` (unchanged) |

- **Fault registry.** Station grows a structured registry over its existing
  counters: `vi_faults: dict[str, FaultRecord]` with
  `kind` (stale/disconnected/comm), `message`, `since`, `acknowledged`.
  Emitted as a structured Qt signal (new `ErrorEvent` dataclass:
  `vi_name, kind, severity, message, timestamp`) replacing the
  string-only `error_occurred` payload (kept as a thin compat wrapper until
  the GUI is migrated).
- **Consequence scoping.** A stale *claimed* VI fails the run (as today) but
  the Orchestrator returns to IDLE with only that VI quarantined, instead of
  parking in global ERROR. A stale *unclaimed* VI during monitoring
  quarantines silently (banner + badge), nothing else stops.
- **Global ERROR survives** for exactly one case: an unhandled exception at
  the tick boundary (unknown blast radius). `recover_from_error()` unchanged.
- **EMERGENCY unchanged in semantics**, upgraded in surface: `check_safety`
  returns flag→originating-VI mapping so the emergency reason names the
  instrument; the Acknowledge control moves to the Monitor window banner and
  is removed from the ProcedureWindow (single home; two-stage
  acknowledge/manual-override behavior unchanged).
- **Monitor UI.** Faulted VI panels: controls disabled, fault badge,
  Acknowledge + Retry (retry resets the Station error counters and repolls
  once; success clears the fault).

### 4. Helium-fill curve to the cryogenics log

- `HeliumFillOperation` drops its DataManager. It samples the level curve
  into a bounded in-memory series during the run and hands it to the session
  layer on completion (extend `HeliumRecordStore` / `CryogenicsRecorder`
  with the curve alongside the existing fill record). Run-manifest
  `data_file` stays empty; `OperationBase` docs already state the data file
  is optional.

## Phasing (each phase lands with green harness before the next)

1. **Claims + admission gate.** `claimed_vi_names()`, orchestrator claim
   tracking, `submit_vi_action`/drain-gate predicate, refusal messages.
   Tests: action admitted on unclaimed VI mid-operation, refused on claimed
   VI, refused during procedures (default claim-all), teardown clears claims.
2. **Immediate finish + hard status separation.** Non-blocking finish path,
   postcondition one-shot warning, removal of the postcondition wait phase,
   run-kind-tagged status signals, ProcedureWindow operation-blind filter,
   card status line.
3. **Fault registry + structured errors + Monitor acknowledge.** Station
   `FaultRecord`, `ErrorEvent` signal, consequence scoping in the
   orchestrator failure paths, Monitor badges/acknowledge, emergency
   acknowledge relocation.
4. **Fill curve migration.** DataManager removal from the fill, cryogenics
   log extension.

Cross-cutting: GLOSSARY entries ("Claim", "Instrument fault", "Fault
acknowledge"), folder README updates in the same commits, conformance test
for `claimed_vi_names()` (must return `None` or a subset of configured VI
names).

## Explicitly out of scope

- Two-lane scheduling (procedure concurrent with an operation). The claim
  model is the enabler; the tick-loop refactor is a separate future plan.
- Per-instrument scoping of *safety* flags. A quench or low helium is a
  whole-cryostat event; global standby stays.
- Any change to driver or VI hardware I/O paths.
