# Plan: operations, the Servicing Log, and cryogenics management

**Status:** proposal — no code yet.
**Scope:** three connected pieces. (1) **Operations**: a new L4 class for
multi-step *cryostat-servicing* actions (helium fill, sample change) that is
distinct from measurement procedures — higher submission priority, access to
a broader, explicitly-scoped hardware capability tier, verified
postconditions, and no required data file. (2) A **servicing-log framework**
(L6): per-setup, typed, human-editable logs of servicing events, where a new
log *kind* is a declaration rather than new code. The first kind is the
**cryogenics log** technical staff care about — who filled, start/end time,
start/end helium level, whether LN2 was filled too — plus an hourly
machine-recorded **helium record**. (3) **Cryogenics management** built on
both: a GUI sub-panel showing helium consumption over the last hours and a
"Fill helium" operation that forces all magnets to zero field and writes the
cryogenics log entry. Everything is config-gated so a cryostat that doesn't
need a feature carries zero UI footprint with zero code changes.
**Date:** 2026-07-19 (rev. 2: the fill is an *operation*, superseding the
earlier `run_kind = "service"` marker. rev. 3: the logbook generalized into
the servicing-log framework — editable, kind-declared logs; hourly helium
record; Monitor window gains a second page hosting all logs).

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
- **There is no servicing record of any kind.** The closest artifacts are
  per-run `RunRecord`s (only recorded inside an open experiment), free-text
  `ExperimentRecord.findings`, and the machine-only `logs/status.jsonl`.
  Who filled helium on Tuesday, and from what level to what level, is
  recorded nowhere.
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
3. **Completion** — verified postconditions and a servicing-log record
   instead of a mandatory dataset (§4.1).

The rule that keeps this safe: **an operation is never a bypass.** Its
extra capabilities are ordinary `@control` methods with config limits,
validated by the control-validation standard, executed on the tick by the
single writer, and watched by `evaluate_safety()` like everything else. The
only thing "privileged" about an operation is *which methods its plans may
name*.

Layer placement — a vertical slice, no new horizontal layer:

```
GUI   monitor pages, gui/cryogenics_panel.py,     config-gated composition; page 2 = logs
      gui/servicing_log_page.py
L6    session/servicing_log.py                    log-kind specs, stores, recorder (new)
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
package; the servicing log lands under C11/C12 exactly like the rest of
`cryosoft/session/`.

## 3. Concept model (→ `GLOSSARY.md` in the first implementation commit)

| Term | Definition |
|---|---|
| **Operation** | A multi-step cryostat-servicing action (subclass of `OperationBase`, L4): helium fill, sample change. Declarative like a procedure (returns plans, never touches VIs directly), but with operation-scope command access, tolerated safety flags, verified postconditions, an optional (not required) data file, and higher submission priority. Not listed among measurement procedures. |
| **Capability scope** | The tier a VI `@control` method belongs to: `"measurement"` (may appear in any plan) or `"operation"` (may appear only in operation plans; still a GUI control as before). Declared on the decorator, enforced at dispatch, conformance-checked. |
| **Postcondition gate** | A `Gate` an operation declares via `postcondition_gates()`: stepped after parking completes, and the operation only reports **done** once every gate holds. The difference between "the commands ran" and "the cryostat is verifiably in the promised state". |
| **Servicing log** | The L6 framework for per-setup, human-facing logs of servicing events. Each log is one **log kind**; entries are typed records, editable via **entry revisions**, persisted append-only. Independent of experiments; what technical staff consult and maintain. |
| **Log kind** | One declared servicing-log table: a key (`"cryogenics"`), a title, and an ordered field schema (reusing `ParamSpec`). Adding a log kind for a new setup is a declaration + config reference — the storage, revision handling, table view, and add/edit dialogs all follow automatically. The standards-over-one-off-code move, applied to record keeping. |
| **Cryogenics log** | The first log kind: one entry per cryogen fill — `person`, `start_utc`, `end_utc`, `helium_start_pct`, `helium_end_pct`, `ln2_filled` (bool), `notes`. Written automatically by the fill operation, addable and editable manually (fills done by hand, LN2 top-ups, corrections). |
| **Entry revision** | How editability and trustworthiness coexist: every entry has a stable `entry_id`; an edit appends a new revision (`revised_utc`, `revised_by`) rather than rewriting; a deletion appends a tombstone. The store presents the latest revision per entry; the full history stays on disk. The file itself is never rewritten. |
| **Helium record** | The machine companion to the cryogenics log: one `(utc, helium_pct, nitrogen_pct)` sample per hour, appended by the recorder from the monitoring tick. Powers the consumption display and start/end-level lookup; not human-editable. |

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
  unmet gate named, and the operation's servicing-log entry is marked
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

## 6. The Servicing Log (L6, `cryosoft/session/servicing_log.py`)

### 6.1 The framework: log kinds are declarations

One generic engine, N declared kinds. A log kind is:

```python
LogKindSpec(
    key="cryogenics",
    title="Cryogenics log",
    fields={  # ordered; reuses ParamSpec — the same currency the GUI already renders
        "person":           ParamSpec(type=str,   description="Who performed the fill"),
        "start_utc":        ParamSpec(type=str,   widget_hint="datetime"),
        "end_utc":          ParamSpec(type=str,   widget_hint="datetime"),
        "helium_start_pct": ParamSpec(type=float, unit="%"),
        "helium_end_pct":   ParamSpec(type=float, unit="%"),
        "ln2_filled":       ParamSpec(type=bool,  default=False),
        "notes":            ParamSpec(type=str,   default=""),
    },
)
```

Everything downstream is generic: `ServicingLogStore` persists entries of
any kind; the GUI's table view derives its columns from `fields`; the
add/edit dialog is rendered by the existing `param_form.py` machinery from
the same `ParamSpec`s. **Adding a servicing log for another setup — pump
maintenance, compressor service, transfer-line checks — is one
`LogKindSpec` plus one config line.** No new store code, no new GUI code.
Conformance auto-discovers every declared kind and checks its fields are
valid `ParamSpec`s with defaults, mirroring how procedures are checked.

At this stage exactly one kind ships: `cryogenics`.

### 6.2 Storage: editable for humans, append-only on disk

`ServicingLogStore` — one JSONL file per kind per setup
(`<store root>/servicing/<config_name>/<kind>.jsonl`), atomic appends,
tolerant loads. Editability comes from the **entry-revision model** (§3):
`add_entry()` / `revise_entry()` / `delete_entry()` all append; readers get
the latest revision per `entry_id` (tombstones hidden), and revision
history remains inspectable. A technician can correct a mistyped level or
add a forgotten fill from last week without anything ever being lost —
the property a servicing record needs to stay trustworthy.

Entries carry provenance: `source` = `"operation"` (written by a fill run,
with `run_id` linkage) or `"manual"`, plus `revised_by`/`revised_utc` on
every revision. Person attribution offers the session `UserRoster` as a
pick list with free-text fallback (technical staff need not be roster
users).

### 6.3 The helium record and the recorder

- **`HeliumRecordStore`** — the machine stream: one
  `(utc, helium_pct, nitrogen_pct)` sample per `history_sample_s`
  (**default 3600 s — hourly**, per the user-level requirement), appended
  from the monitoring tick, size-bounded rotation. ~24 lines/day: cheap
  enough to run forever, and on disk so consumption history survives
  restarts (unlike the RAM-only `MonitorHistory`, which keeps serving the
  fine-grained live trends).
- **`CryogenicsRecorder(QObject)`** — the automatic writer, constructed by
  `main.py` beside `SessionManager` (only when `cryogenics:` is present),
  driven purely by existing Orchestrator signals:
  - `states_updated` → decimate/append the helium record; threshold
    warnings with hysteresis (§7) — surfaced as a banner and noted in the
    application log, not as cryogenics-log entries (the fill log stays
    clean).
  - fill-operation manifests (`run_started`/`run_finished`) → compose the
    cryogenics-log entry: start/end UTC from the manifest, start/end level
    from the fill's own sampled data, person from the fill dialog,
    `ln2_filled` defaulting False (LN2 fills are manual and undetectable —
    the technician ticks the box, which is exactly what the edit feature
    is for), verified/unverified from the postcondition result, `run_id`
    linkage to the HDF5 level curve.
  - every operation (fill, sample change, future ones) also appends to a
    non-editable **operations stream** (kind `"operations"`, machine
    source only): start/end, parameters, terminal status,
    verified/unverified — the audit trail for "who warmed the VTI last
    night?".

Consumption is derived, not stored: one pure function (linear fit over the
requested window of the helium record, fill intervals excluded so a fill
doesn't read as negative consumption → %/h; × optional `helium_volume_l` →
L/h), shared by the GUI and any future agent tool.

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

Deliberately not a safety mechanism: the `helium_warning_pct` advisory
(crossing detected by the recorder with hysteresis — one warning per
low-helium episode, cleared only when the level rises back above threshold
plus a margin). It is surfaced as a banner and application-log line, never
by `evaluate_safety()` — warnings that trip EMERGENCY teach operators to
ignore EMERGENCY.

## 8. The first two operations

### 8.1 Helium fill (`procedures/operations/helium_fill.py`)

- `initiate()` → all magnets (`Station.magnet_vi_names()`, the new mirror
  of `switch_vi_names()`: registry-`system` VIs with class
  `vi_type == "magnet"`) → `Target(0.0)`; level meter → refresh FAST
  (operation-scope). Creates a DataManager: columns
  `unix_time, helium_pct` + per-magnet fields — every fill's curve is
  preserved and linkable from its cryogenics-log entry.
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
  (sanity that a fill actually happened — else the log entry is marked
  unverified).
- `tolerated_safety_flags = frozenset({"helium_low"})`.
- The fill dialog asks for the operator (roster pick list or free text);
  on finish the recorder writes the cryogenics-log entry (§6.3), which
  remains editable afterwards (LN2 checkbox, notes, corrections).

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
  operations stream as human-confirmed rather than machine-verified. The
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
  sample_period_s: 10.0          # fill-time sampling (FAST mode)
  history_sample_s: 3600.0       # helium record cadence — hourly
  helium_volume_l: 40.0          # optional; enables L/h display

servicing_logs:                  # which declared log kinds this setup keeps
  - cryogenics                   # (the operations stream is always on when any
                                 #  operation exists; it is not listed here)

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
ordered, durations positive, every `servicing_logs` entry names a declared
`LogKindSpec`, `needle_valve` is `"manual"` or resolves to an
operation-scope control.

## 10. GUI: the Monitor window gains pages

`MonitorWindow` becomes **paged** (a slim page switcher; the current 2×2
quadrant grid is page 1, unchanged in layout):

- **Page 1 — Monitor** (the existing quadrants):
  - The bottom-right stacked quadrant keeps "Other devices" and gains
    **Cryogenics** (`gui/cryogenics_panel.py`, built only when the
    `cryogenics:` block and a level VI exist): current He/N₂ levels,
    consumption (%/h, L/h), level-vs-time plot (1 h / 6 h / 24 h) from the
    helium record with fill markers overlaid, and **Fill helium** →
    operator prompt → `run_operation()`, toggling to **Stop filling** →
    `finish_operation()`. Live *status* stays on page 1 because it is
    monitoring; the *records* live on page 2.
  - The existing application-log view **moves off this quadrant to page
    2**, freeing the stack here.
- **Page 2 — Logs** (`gui/servicing_log_page.py`): the natural home for
  everything retrospective:
  - One table per configured servicing-log kind (columns derived from its
    `LogKindSpec`), newest first, with **Add entry** and **Edit** — both
    dialogs auto-rendered from the kind's `ParamSpec`s via the existing
    `param_form.py` machinery. Edits go through the revision model; a
    subtle "edited" marker exposes revision history on demand.
  - The read-only operations stream (filterable), and the relocated
    application `LogPanel`.
  - Adding a future log kind adds a table here automatically — zero new
    GUI code.
- Verdicts and progress arrive through the existing
  `action_*`/`run_*`/`status_message` surface; no new signal channels.
- Per the GUI standard: theme tokens, no blocking calls, qtbot geometry
  tests, offscreen screenshot verification. The page split touches
  `MonitorWindow._build_ui` composition only — child panels keep their
  single-receiver `states_updated` forwarding pattern.
- Rendering note for the consumption plot: the helium record has gaps
  whenever the app was closed or monitoring was off — render gaps as gaps,
  never interpolate across them.

## 11. Phasing (bottom-up, each phase lands green)

1. **Servicing-log foundation (L6):** `LogKindSpec` + the `cryogenics`
   kind, `ServicingLogStore` with the revision model, `HeliumRecordStore`
   (hourly), consumption function, `CryogenicsRecorder` against a mocked
   Orchestrator; GLOSSARY terms; conformance for kind declarations and
   session models.
2. **Operation substrate (L3/L4 + scope):** `OperationBase`,
   `run_operation()` with priority + EMERGENCY carve-out + tolerated
   flags, postcondition phase, `finish_operation()`; `@control` scope +
   dispatch enforcement; conformance + behavior tests (helium_low vs
   quench matrix, scope rejection).
3. **Helium fill (first operation):** `Station.magnet_vi_names()`, the
   operation against `sim_cryostat`, `cryogenics:` config block +
   validation, recorder's auto-entry path end-to-end.
4. **Sample change (second operation):** config block, operator-confirm
   postconditions; ITC needle-valve capability only where hardware
   supports it (driver + sim twin + VI method, operation-scope).
5. **GUI:** Monitor window pages, the Logs page (servicing-log tables with
   add/edit, operations stream, relocated LogPanel), the cryogenics panel
   on page 1; geometry tests + screenshots.

Each phase is independently useful; nothing above a phase blocks shipping
it. After Phase 1 a running setup is already accumulating the hourly helium
record and (once Phase 3 lands) writing clean fill entries — the Logs page
in Phase 5 is display, not function.

## 12. Phase 6 — operations panel

The cryogenics view becomes the **Operations panel**: the place an operator
looks to answer *what state is the setup in, is it ready right now for
operation X, and when will X next be needed?* Agreed design, implemented
exactly as follows:

- **Absorb, don't add.** The bottom-right selector entry renamed
  "Cryogenics" → **"Operations"**
  (`gui/cryogenics_panel.py` → `gui/operations_panel.py`,
  `CryogenicsPanel` → `OperationsPanel`). The existing cryogenics status
  section (levels, consumption, plot) stays at the top, still gated on the
  `cryogenics:` block; below it, one **operation card** per configured
  operation. The panel is available when cryogenics is enabled OR the
  `operations:` config is non-empty — a setup with only `sample_change`
  still gets the panel, minus the cryo section.
- **Per-condition checklist.** `OperationBase` gains a
  `readiness_conditions() -> tuple[ReadinessCondition, ...]` hook (default
  `()`). Each `ReadinessCondition` (a frozen dataclass: `key`, `label`,
  `check(state) -> bool`, optional `detail(state) -> str`) is evaluated
  purely from the Orchestrator's per-tick state snapshot — no extra
  hardware poll — and rendered as one checklist row (colored ✓/✗ icon,
  label, live detail text).
- **Button starts the prep, not gated on the checklist.** A card's button
  is active whenever the Orchestrator can accept the operation — the
  operation itself is what drives the setup to the safe state, so gating
  the button on readiness would be circular. While running, the button
  becomes "Finish `<name>`" (`orchestrator.finish_operation()`). Once the
  run has finished `done` AND every current readiness condition holds, the
  card shows a **ready banner** with the operation's `ready_message` class
  attribute (empty string = no banner); the banner clears the moment a
  condition stops holding or a new run starts.
- **Next-due line.** `OperationBase.next_due(context) -> NextDue | None`
  (default `None`; `NextDue` is a frozen dataclass: `due_unix`, `text`).
  `context` is a documented, extensible dict the GUI assembles fresh every
  tick: `"state"`, `"now_unix"`, and `"consumption_rate_pct_per_h"` (computed
  by the panel, reusing its existing throttled consumption fit — an
  operation must NOT import the session layer itself, contract C12, which
  is exactly why the rate is passed in rather than computed in
  `next_due()`). `HeliumFillOperation.next_due()` predicts time-to-warning
  from the measured rate; `SampleChangeOperation` has no schedule and
  overrides nothing.
- **Hybrid declaration — the core of the vision.** The operation *class*
  declares its readiness conditions, next-due logic, and `ready_message`;
  the *config* supplies thresholds (already true — the constructor's
  `**config`). A new `config_key: str = ""` class attribute on
  `OperationBase` is how the panel maps `operations:` config blocks to
  classes generically (`SampleChangeOperation.config_key =
  "sample_change"`); `gui/procedure_discovery.py` gains
  `discover_operations()`, the same pkgutil-walk pattern as
  `discover_procedures()`. Adding an operation to a setup = declare the
  class + add a config block; the panel needs **zero** per-operation code.
- **Sample change's operator confirmations** render as one checkbox per
  `operator_confirmations` entry while it is the active run (checkbox →
  `orchestrator.confirm_operation(key)`, disabled after checking —
  confirmations are one-way, mirroring the existing L4 contract exactly).

Implementation: `core/operation.py` (`ReadinessCondition`, `NextDue`, the
two hooks, `ready_message`/`config_key`); `procedures/operations/
helium_fill.py` and `sample_change.py` (concrete `readiness_conditions()`/
`next_due()`); `gui/procedure_discovery.py` (`discover_operations()`);
`gui/operations_panel.py` (`OperationsPanel`, `OperationCard` — the fully
generic per-operation widget, `OperatorDialog` — the generic operator-name
prompt replacing `FillOperatorDialog`); `gui/monitor_window.py` / `main.py`
(selector rename, `operations_config` wiring).
