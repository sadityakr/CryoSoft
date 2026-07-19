# Plan: operations, the Logbook, and cryogenics management

**Status:** proposal — no code yet.
**Scope:** three connected pieces. (1) **Operations**: a new L4 class for
multi-step *cryostat-servicing* actions (helium fill, sample change) that is
distinct from measurement procedures — higher submission priority, access to
a broader, explicitly-scoped hardware capability tier, verified
postconditions, and no required data file. (2) A setup-level **logbook**
(timestamped operational events, independent of experiments). (3)
**Cryogenics management** built on both: a GUI sub-panel showing helium
consumption over the last hours and a "Fill helium" operation that forces
all magnets to zero field and records start/end time and level. Everything
is config-gated so a cryostat that doesn't need a feature carries zero UI
footprint with zero code changes.
**Date:** 2026-07-19 (revised same day: the fill is now the first
*operation*, superseding the earlier `run_kind = "service"` procedure
marker, per design discussion).

---

## 1. Where we are today

A survey of the codebase (2026-07-19) established the starting point:

- **The level meter VI already has everything the fill needs at L1.**
  `CryogenLevelMeterVI` exposes `helium_level()`/`nitrogen_level()`
  (`@monitored`), the three-mode refresh standard (FAST is documented as
  "rapid polling used during a helium fill"), and the debounced
  `helium_low` safety flag via `evaluate_safety()`.
- **The Orchestrator's execution machinery fits servicing actions.** Ramp
  targets, gates, the two-phase STANDBY, pause/abort, error containment,
  watchdog, run manifests — a fill or sample change needs exactly this. What
  does *not* fit is the `BaseProcedure` contract wrapped around it:
  procedures are sweep-shaped (DataManager, `DataSchema`, `measurement_vi`
  selection, live-plot columns, progress in points). A servicing action has
  none of that.
- **The plan command surface is open.** `Station.send_measurement_commands`
  dispatches *any* `Command` to *any* method of *any* registered VI. Nothing
  but convention stops a procedure from carrying
  `Command("magnet_z", "switch_heater_on")`. If procedures are to be denied
  such capabilities while operations are allowed them, an enforcement
  mechanism is needed — none exists today.
- **Time-series history exists but is RAM-only.** `gui/monitor_history.py`
  keeps 24 h of every monitored value in ring buffers fed by
  `states_updated`. Nothing is persisted: close the app and the consumption
  history is gone.
- **There is no logbook.** The closest artifacts are per-run `RunRecord`s
  (only recorded inside an open experiment), free-text
  `ExperimentRecord.findings`, and the machine-only `logs/status.jsonl`. A
  timestamped stream of operational events tied to the *setup* rather than
  to an experiment does not exist.
- **One genuine safety-layer gap.** `helium_low` trips EMERGENCY;
  `acknowledge_emergency()` is refused while the condition persists;
  `run_procedure()` starts only from IDLE. Consequence: when helium is
  actually low — the one time a fill matters — a software-driven fill can
  never start (§7).
- **Strong precedents exist for the optionality mechanism.** Panels are
  discovered from the station config by `vi_type`; the scanner is an
  availability bit; limits and roles live in `devices.yaml`. "Present in
  config → feature active" is already the house style.

## 2. Design principle: two request types, one execution engine

Operations and procedures are **different contracts submitted to the same
single writer** — not two orchestration paths. Both speak the existing plan
currency (`PhasePlan` / `StepPlan` / `Target` / `Command` / `Gate`); both
are driven by the same tick loop, state machine, watchdog, and safety
checks. The differences live at exactly three points:

1. **Submission** — priority, queueing, and the EMERGENCY carve-out (§4.2).
2. **Dispatch** — the capability scope a plan's commands may carry (§5).
3. **Completion** — verified postconditions and logbook recording instead
   of a mandatory dataset (§4.1).

The rule that keeps this safe: **an operation is never a bypass.** Its
extra capabilities are ordinary `@control` methods with config limits,
validated by the control-validation standard, executed on the tick by the
single writer, and watched by `evaluate_safety()` like everything else. The
only thing "privileged" about an operation is *which methods its plans may
name*.

Layer placement — a vertical slice, no new horizontal layer:

```
GUI   gui/cryogenics_panel.py + operations UI    config-gated composition
L6    session/logbook.py                          logbook models + store + recorder (new)
L4    core/operation.py (OperationBase)           the new contract (new)
      procedures/operations/*.py                  concrete operations (new)
L3    Orchestrator                                run_operation(), scope enforcement hooks,
                                                  finish request, postcondition phase (small)
L2    Station                                     magnet_vi_names(); command-scope check
L1    VIs                                         @control gains a scope; ITC503 VI may gain
                                                  needle-valve control (hardware-dependent)
L0    drivers                                     unchanged (except optional ITC gas-flow method)
```

Contracts C1–C12 cover every piece unchanged; C6 (procedures are
hardware-blind) extends verbatim to `core.operation` and the operations
package.

## 3. Concept model (→ `GLOSSARY.md` in the first implementation commit)

| Term | Definition |
|---|---|
| **Operation** | A multi-step cryostat-servicing action (subclass of `OperationBase`, L4): helium fill, sample change. Declarative like a procedure (returns plans, never touches VIs directly), but with operation-scope command access, tolerated safety flags, verified postconditions, an optional (not required) data file, and higher submission priority. Not listed among measurement procedures. |
| **Capability scope** | The tier a VI `@control` method belongs to: `"measurement"` (may appear in any plan) or `"operation"` (may appear only in operation plans; still a GUI control as before). Declared on the decorator, enforced at dispatch, conformance-checked. |
| **Postcondition gate** | A `Gate` an operation declares via `postcondition_gates()`: stepped after parking completes, and the operation only reports **done** once every gate holds. The difference between "the commands ran" and "the cryostat is verifiably in the promised state". |
| **Logbook** | A per-setup, append-only stream of timestamped operational events, independent of experiments. Survives restarts; never rewritten. |
| **Logbook event** | One typed record: `utc`, `kind` (`operation`, `helium_fill`, `cryo_warning`, `note`), `data`, optional `run_id` linkage. Tolerant-parse dataclass like every session model. |

## 4. `OperationBase` (L4, `cryosoft/core/operation.py`)

### 4.1 Contract

Same four-phase surface the Orchestrator already drives, minus the sweep
machinery, plus three declarations procedures don't have:

- `initiate() -> PhasePlan` — initial targets/commands. **No DataManager
  required**; an operation that wants a dataset (the fill's level curve)
  may create one, and its manifest then carries the path like any run.
- `step() -> StepPlan | None` — next tick's waits/targets; `None` ends the
  operation. Open-ended operations honor the graceful-finish flag (§4.3).
- `sample() -> None` — optional per-tick observation hook (the fill logs a
  level point; a sample change does nothing). Default no-op.
- `standby() -> PhasePlan` / `abort() -> tuple[Command, ...]` — park /
  safe-off, as for procedures.
- `initiation_gates() -> tuple[Gate, ...]` — as for procedures.
- **`postcondition_gates() -> tuple[Gate, ...]`** *(new)* — stepped after
  `standby()`'s ramps complete. Only when all hold does the Orchestrator
  emit the manifest with status `done`; a timeout (per-gate `window_s` plus
  an operation-level `postcondition_timeout_s`) degrades to ERROR with the
  unmet gate named, and the logbook records the operation as
  **unverified**. Gates read the station's cached state — no extra polls.
- **`tolerated_safety_flags: frozenset[str] = frozenset()`** *(new)* —
  flags that do not abort *this* operation (§7).
- **`command_scope = "operation"`** *(fixed by the base class)* — see §5.
- Parameters come from config and `ParamSpec`s exactly like procedures
  (same form rendering if an operation ever needs a form), but operations
  are **not** returned by `discover_procedures()`; a parallel
  `discover_operations()` filters on `OperationBase`.

### 4.2 Priority semantics — written down, because they will be contested

- **Queue-jumping, not preemption.** `Orchestrator.run_operation(op)`
  starts from IDLE (or manual-ramp RAMPING, cancelling the manual ramp as
  `run_procedure` does). Queued operations always run before queued
  procedures. A *running* procedure is **never** auto-aborted by an
  operation: interrupting a 12-hour sweep stays an explicit human
  abort, then the operation starts. The refusal comes back as
  `action_blocked` with exactly that instruction.
- **While an operation runs, everything else is locked out** — this is the
  existing single-writer behavior (`submit_vi_action` and `run_procedure`
  refuse outside IDLE) and is what makes "ensure all magnets are at zero
  field" a guarantee rather than a hope: nobody can ramp a field mid-fill
  or mid-sample-change.
- **EMERGENCY carve-out**: `run_operation` is additionally allowed from
  EMERGENCY iff every currently-active safety flag is in the operation's
  `tolerated_safety_flags` (emergency entry already ran `standby_all`). On
  finish: flags cleared → IDLE, else back to EMERGENCY. This is the
  narrowest possible door for remediation, and it exists **only** on
  operations — `BaseProcedure` gets neither tolerated flags nor the
  carve-out.

### 4.3 Graceful finish

`Orchestrator.finish_operation()` sets a flag the operation reads
(`OperationBase.finish_requested`); the next `step()` returns `None` and
the normal STANDBY → postcondition path runs. (The same mechanism is worth
exposing for procedures later, but it ships here because "Stop filling"
needs it.)

## 5. The capability-scope standard (the enforceable privilege boundary)

- `@control` gains a `scope` argument, default `"measurement"`. Methods
  whose automated misuse is dangerous get `scope="operation"`:
  switch-heater on/off, persistent-mode entry/exit, needle-valve/gas-flow
  control, level-meter refresh mode. GUI behavior is unchanged — a human in
  IDLE can still click any `@control` as today.
- **Enforcement at dispatch**: command dispatch takes the submitting
  context's allowed scope. Plans from procedures may only carry commands
  whose target method is `"measurement"`-scope; operation plans may carry
  both. A violation raises `CryoSoftSafetyError` naming the method — the
  whole plan is rejected before any hardware is touched, contained to ERROR
  like an envelope violation. (Reading-loop setters and the measurement
  lifecycle are `"measurement"`-scope by definition, so no existing
  procedure changes behavior.)
- **Conformance additions**: scope values are valid; every method named in
  any VI's `reading_setters`/measurement lifecycle is measurement-scope;
  switch-heater and persistent-mode methods on the persistent magnet VI
  (and any future needle-valve method) are operation-scope; and a behavior
  test proves a procedure plan carrying an operation-scope command is
  rejected while the same command in an operation plan dispatches.

This converts today's "procedures don't touch the switch heater, by
convention" into a machine-checked rule — the standards-over-one-off-code
move, applied to privilege.

## 6. The Logbook (L6, `cryosoft/session/logbook.py`)

- **Models** in `session/models.py` (`LogbookEvent`), so existing
  session-model conformance (defaults-constructible, JSON-safe round-trip,
  junk-tolerant `from_dict`) covers them the moment they exist.
- **`LogbookStore`** — append-only JSONL per setup
  (`<store root>/logbook/<config_name>.jsonl`), atomic appends, tolerant
  loads, windowed reads. Same discipline as `ExperimentStore`.
- **`HeliumHistoryStore`** — the persistence MonitorHistory lacks: a
  decimated `(utc, helium_pct, nitrogen_pct)` sample every
  `history_sample_s` (default 300 s), size-bounded rotation. This makes
  "consumption over the last few hours" survive a restart.
- **`CryogenicsRecorder(QObject)`** — single writer to both stores,
  constructed by `main.py` beside `SessionManager` (only when the feature
  is on), driven purely by existing Orchestrator signals:
  - `states_updated` → decimate/append history; log one `cryo_warning`
    per threshold crossing (hysteresis, not per tick).
  - operation manifests (`run_started`/`run_finished`, kind
    `"operation"`) → compose the event: start/end UTC, start/end level
    from its own history, verified/unverified, terminal status, data-file
    linkage when present.

  **Every operation is logbook-recorded, always** — unlike measurement
  runs, which are only recorded inside an open experiment. Operations are
  precisely what you consult when asking "who warmed the VTI last night?".

Consumption is derived, not stored: one pure function (linear fit over the
last N hours → %/h; × optional `helium_volume_l` → L/h), shared by the GUI
and any future agent tool.

## 7. Safety layer: what actually changes

The zero-field interlock needs nothing new — plan targets + gates +
single-writer lockout already guarantee it. The changes, all attached to
operations (§4.2, §5):

1. **`tolerated_safety_flags` on `OperationBase`** — the fill declares
   `{"helium_low"}` (its plan forces every magnet to zero and arms
   nothing, so low helium is not a hazard for it; it is its reason to
   exist). The tick subtracts the *running operation's* tolerated flags
   before deciding on EMERGENCY; a non-tolerated flag (`quench`) still
   kills a fill instantly. Behavior tests cover the full matrix.
2. **Remediation start from EMERGENCY** — §4.2's carve-out closes the
   deadlock in §1.
3. **The capability scope** (§5) — strictly a *tightening*: procedures
   lose access they never legitimately had.

Deliberately not a safety mechanism: the `helium_warning_pct` advisory. It
is surfaced by the recorder/panel (banner + logbook event), never by
`evaluate_safety()` — warnings that trip EMERGENCY teach operators to
ignore EMERGENCY.

## 8. The first two operations

### 8.1 Helium fill (`procedures/operations/helium_fill.py`)

- `initiate()` → all magnets (`Station.magnet_vi_names()`, the new mirror
  of `switch_vi_names()`: registry-`system` VIs with class
  `vi_type == "magnet"`) → `Target(0.0)`; level meter → refresh FAST
  (operation-scope). Creates a DataManager: columns
  `unix_time, helium_pct` + per-magnet fields — every fill's curve is
  preserved and linkable from its logbook event.
- `initiation_gates()` → `Gate("zero_field", |B|<ε on every magnet from
  cached state, window_s from config)`. The fill does not count as started
  until zero field is confirmed *and held*; a stuck gate is visible by
  name in operational status.
- `sample()` → read + save helium level. A stale/disconnected meter raises
  → contained to ERROR with hardware already at zero field — the correct
  failure mode for losing our eyes mid-fill.
- `step()` → `StepPlan(wait_s=sample_period_s)` until: finish requested,
  level ≥ `fill_target_pct` and non-rising for `fill_complete_window_s`,
  or `max_fill_duration_s` exceeded.
- `standby()` → refresh SLOW; also from `abort()`, so an aborted fill never
  leaves the meter in FAST.
- `postcondition_gates()` → refresh mode is SLOW; level ≥ start level
  (sanity that a fill actually happened — else the logbook records
  unverified).
- `tolerated_safety_flags = frozenset({"helium_low"})`.

### 8.2 Sample change (`procedures/operations/sample_change.py`)

"Verify the cryostat is safe to open": magnets off, VTI at 300 K, needle
valve closed.

- `initiate()` → all magnets → `Target(0.0)` (the persistent magnet's own
  VI logic owns heater sequencing; explicit heater-off commands are
  operation-scope and available if the setup's exit sequence needs them);
  VTI (and sample controller, if configured) → 300 K; switch VI
  `open_all`; measurement VIs disarmed; needle valve closed **if the setup
  has the capability** (see below).
- `postcondition_gates()` → |B|<ε held on every magnet; switch-heater OFF
  confirmed on persistent magnets; VTI within tolerance of 300 K held for
  a window; valve confirmed closed.
- **The needle-valve reality check**: no needle-valve/gas-flow capability
  exists anywhere in the stack today. Where the ITC503 controls the valve,
  the driver + VTI VI gain a `close_needle_valve()` (operation-scope,
  sim-twinned, conformance-covered). Where the valve is manual, the config
  omits the capability and the postcondition becomes an **operator
  confirmation** in the GUI ("needle valve closed ✓"), recorded in the
  logbook event as human-confirmed rather than machine-verified. The
  postcondition declaration must support both, which is why gates and
  confirmations are declared, not hardcoded.
- Whether a sample change should also *position* things (e.g. rotator to a
  load angle) is an open per-setup question — the config block below
  leaves room (`extra_targets`), but v1 ships without it.

## 9. Config (single source of truth)

New optional top-level blocks in `devices.yaml` — a block absent means that
feature is off and hidden:

```yaml
cryogenics:
  level_vi: level_meter          # must name a registered vi_type: level VI
  helium_warning_pct: 35.0       # advisory; must exceed the VI's helium_low_threshold
  fill_target_pct: 90.0
  fill_zero_field_eps_T: 0.005
  fill_zero_field_window_s: 10.0
  fill_complete_window_s: 120.0
  max_fill_duration_s: 3600.0
  sample_period_s: 10.0
  history_sample_s: 300.0
  helium_volume_l: 40.0          # optional; enables L/h display

operations:
  sample_change:
    vti_vi: temperature_vti
    target_temperature_K: 300.0
    temperature_tolerance_K: 2.0
    temperature_window_s: 60.0
    zero_field_eps_T: 0.005
    zero_field_window_s: 10.0
    needle_valve: manual          # "manual" -> operator confirmation; or a VI capability ref
    postcondition_timeout_s: 7200.0
```

The `helium_low_threshold` stays in the level VI's `init_params` — it is an
instrument-protection property; the blocks above are operational policy.
Conformance: referenced VIs exist and have the right types, thresholds
ordered, durations positive, `needle_valve` is `"manual"` or resolves to an
operation-scope control.

## 10. GUI

- **Cryogenics panel** (`gui/cryogenics_panel.py`): a third page in the
  bottom-right stacked quadrant, built only when `cryogenics:` is present
  and the level VI exists. Current levels + consumption (%/h, L/h),
  level-vs-time plot (1 h/6 h/24 h) from the persistent history with fill
  events overlaid, recent-fill list, and **Fill helium** →
  `run_operation()`, toggling to **Stop filling** → `finish_operation()`.
- **Operations affordances**: a "Prepare for sample change" action
  (placement: the session-info quadrant or a small operations strip —
  decide at GUI phase), a progress/status line naming the active gate, and
  the operator-confirmation checkbox flow for human-verified
  postconditions. Verdicts arrive through the existing
  `action_*`/`run_*`/`status_message` surface; no new signal channels.
- Per the GUI standard: theme tokens, no blocking calls, qtbot geometry
  tests, offscreen screenshot verification.

## 11. Phasing (bottom-up, each phase lands green)

1. **Logbook foundation (L6):** `LogbookEvent`, `LogbookStore`,
   `HeliumHistoryStore`, consumption function, `CryogenicsRecorder`
   against a mocked Orchestrator; GLOSSARY terms.
2. **Operation substrate (L3/L4 + scope):** `OperationBase`,
   `run_operation()` with priority + EMERGENCY carve-out + tolerated
   flags, postcondition phase, `finish_operation()`; `@control` scope +
   dispatch enforcement; conformance + behavior tests (helium_low vs
   quench matrix, scope rejection).
3. **Helium fill (first operation):** `Station.magnet_vi_names()`, the
   operation against `sim_cryostat`, `cryogenics:` config block +
   validation.
4. **Sample change (second operation):** config block, operator-confirm
   postconditions; ITC needle-valve capability only where hardware
   supports it (driver + sim twin + VI method, operation-scope).
5. **GUI:** cryogenics panel + operations affordances, config-gated
   composition, geometry tests + screenshots.

Each phase is independently useful; nothing above a phase blocks shipping
it.
