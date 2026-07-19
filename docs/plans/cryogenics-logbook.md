# Plan: the Logbook and cryogenics management

**Status:** proposal — no code yet.
**Scope:** a setup-level logbook (timestamped operational events, independent
of experiments) and a cryogenics-management feature built on it: a GUI
sub-panel showing helium consumption over the last hours, a "Fill helium"
action that forces all magnets to zero field before the fill and records
start/end time and level, and the safety-layer changes needed to make a fill
startable exactly when helium is low. The whole feature is config-gated so a
dry cryostat carries zero UI footprint with zero code changes.
**Date:** 2026-07-19

---

## 1. Where we are today

A survey of the codebase (2026-07-19) established the starting point:

- **The level meter VI already has everything the fill needs at L1.**
  `CryogenLevelMeterVI` exposes `helium_level()`/`nitrogen_level()`
  (`@monitored`), the three-mode refresh standard (`set_refresh_rate`,
  0=STANDBY/1=SLOW/2=FAST — FAST is documented as "rapid polling used during
  a helium fill"), and the debounced `helium_low` safety flag via
  `evaluate_safety()`. No L0/L1 changes are required.
- **The Orchestrator's procedure lifecycle fits a fill.** A fill is a
  sequence "ramp things to targets, confirm, sample repeatedly, park" — the
  exact shape of `BaseProcedure` (`initiate()` → RAMPING → gates →
  MEASURING/SWEEPING loop → `standby()`). `procedures/README.md` explicitly
  allows non-sweep shapes to subclass `BaseProcedure` directly. Running the
  fill as a procedure buys, for free: the single-writer rule (all GUI magnet
  actions are refused while it runs — nobody can ramp a field mid-fill), the
  mandatory safety watchdog, pause/abort, progress and status feeds, and a
  run manifest (`run_started`/`run_finished`) that the session layer already
  records.
- **Zero-field enforcement needs no new machinery.** `initiate()` returns
  `targets = {each magnet: Target(0.0)}`; the RAMPING state completes only
  when every ramp reports done; an `initiation_gates()` `Gate` can then
  require |B| < ε to hold for a window before the fill counts as started —
  and the gate's name shows up in operational status while it waits.
- **Time-series history exists but is RAM-only.** `gui/monitor_history.py`
  keeps 24 h of every monitored value in ring buffers fed by
  `states_updated`, and `TrendPlotPanel` can already plot
  `level_meter_helium_level` over 15 min–24 h windows. Nothing is persisted:
  close the app and the consumption history is gone.
- **There is no logbook.** The closest artifacts are per-run `RunRecord`s
  (session layer, only recorded inside an open experiment), the free-text
  `ExperimentRecord.findings`, and the machine-only `logs/status.jsonl`.
  A timestamped stream of *operational* events (fills, cryogen warnings,
  operator notes) tied to the setup rather than to an experiment does not
  exist.
- **One genuine safety-layer gap.** `helium_low` trips EMERGENCY;
  `acknowledge_emergency()` is refused while the condition persists; and
  `run_procedure()` only starts from IDLE. Consequence: **when helium is
  actually low — the one time a fill matters — a software-driven fill can
  never start.** The operator would have to fill blind, outside the
  software, with no record. This is the part of the safety layer that needs
  improving (§6); everything else the feature needs is already there.
- **Strong precedents exist for the optionality mechanism.** Panels are
  discovered from the station config by `vi_type` (`MonitorWindow._build_ui`
  partitions VIs; `OtherDevicesPanel` renders "No other devices configured"
  when empty); the scanner is an availability bit resolved from
  `switch_vi_names()`; limits and roles live in `devices.yaml`. "Present in
  config → feature active" is already the house style.

## 2. Design principle: a vertical slice, not a new layer

Cryogenics management is **not a new horizontal layer** — it is one feature
whose pieces each belong to an existing layer, wired together by config:

```
GUI   gui/cryogenics_panel.py     sub-panel; built only when the feature is on
L6    session/logbook.py          logbook models + store + recorder (new)
L4    procedures/helium_fill.py   the fill as a service procedure (new)
L3    Orchestrator                tolerated-flags + graceful-finish (small, generic changes)
L2    Station                     magnet_vi_names() helper; config gains a cryogenics block
L1    CryogenLevelMeterVI         unchanged
L0    ILM drivers                 unchanged
```

A separate `cryosoft/cryogenics/` package was considered and rejected: it
would need its own import contracts, duplicate the session layer's
persistence patterns, and still have to reach into procedures and the GUI —
i.e. it would be a vertical slice pretending to be a layer. The existing
contracts C1–C12 cover every piece above unchanged (the logbook lands under
C11/C12 exactly like the rest of `cryosoft/session/`).

**Removability = config, not code.** The feature is active iff:

1. the station config registers a VI with registry `vi_type: level`, and
2. `devices.yaml` carries a `cryogenics:` block (§5).

A dry cryostat omits both → no panel, no fill procedure offered, no logbook
cryo events, nothing to delete. This mirrors how a station without a switch
VI simply has no reading-loop group.

## 3. The Logbook (L6, `cryosoft/session/logbook.py`)

### 3.1 Concept

| Term (→ GLOSSARY.md) | Definition |
|---|---|
| **Logbook** | A per-setup, append-only stream of timestamped operational events, independent of experiments. Lives beside the experiment store; survives app restarts; never rewritten. |
| **Logbook event** | One typed record: `utc`, `kind` (e.g. `helium_fill`, `cryo_warning`, `note`), `data` (kind-specific dict), optional `run_id` linkage. Tolerant-parse dataclass like every session model. |
| **Service procedure** | A procedure whose purpose is operating the cryostat rather than measuring a sample (`run_kind = "service"`). Recorded like any run; filtered out of measurement-procedure UI lists. |

### 3.2 Structure

- **Models** go in `session/models.py` (`LogbookEvent`), so the existing
  session-model conformance tests (construct from defaults, JSON-safe
  round-trip, junk-tolerant `from_dict`) cover them the moment they exist.
- **`LogbookStore`** — JSONL append-only file per setup
  (`<store root>/logbook/<config_name>.jsonl`), atomic appends, tolerant
  loads, windowed reads (`events(kind=None, since=None)`). Same discipline
  as `ExperimentStore`.
- **`HeliumHistoryStore`** — the persistence MonitorHistory lacks: a
  decimated `(utc, helium_pct, nitrogen_pct)` sample appended every
  `history_sample_s` (config, default 300 s), with size-bounded rotation.
  This is what makes "consumption over the last few hours" survive a
  restart and cost ~nothing on disk.
- **`CryogenicsRecorder(QObject)`** — the single writer to both stores,
  constructed by `main.py` beside `SessionManager` (only when the feature is
  on) and connected to the same Orchestrator signals the session layer uses:
  - `states_updated` → decimate and append helium history; compare against
    the config's warning threshold and log one `cryo_warning` event per
    crossing (hysteresis, not one per tick).
  - `run_started`/`run_finished` where `kind == "service"` and the procedure
    is the helium fill → compose the `helium_fill` event: start/end UTC from
    the manifest, start/end level from its own history, terminal status, and
    the run's HDF5 path (the full fill curve) as linkage.

  The recorder is to the logbook what `SessionManager` is to experiments:
  one writer, signal-driven, GUI-free. No Orchestrator changes are needed
  for any of this — the manifests and `states_updated` already carry
  everything.

Consumption is **derived, not stored**: a linear fit over the last N hours
of history gives %/h, times the optional `helium_volume_l` gives L/h. Done
in one pure function in `session/logbook.py` so the GUI and any future agent
tool share it.

## 4. The fill procedure (L4, `procedures/helium_fill.py`)

`HeliumFillProcedure(BaseProcedure)`, `name = "Helium Fill"`,
`run_kind = "service"`. Non-sweep shape, per the procedures README.

- **`initiate()`** → `PhasePlan(targets={m: Target(0.0) for m in magnets},
  commands=(Command(level_vi, "set_refresh_rate", {"mode": 2}),), wait_s=0)`.
  The magnet list comes from a new `Station.magnet_vi_names()` (registry
  `system` VIs whose class `vi_type == "magnet"` — the mirror of
  `switch_vi_names()`); the level VI name from the cryogenics config.
  Creates the DataManager: the fill writes an HDF5 run like any procedure,
  columns `unix_time, helium_pct` (+ per-magnet field columns), so every
  fill's level curve is preserved and linkable from the logbook event.
- **`initiation_gates()`** → one `Gate("zero_field", check=|B|<ε on every
  magnet from the station's cached state, window_s from config)`. The fill
  does not count as started until zero field is *confirmed and held*, and a
  stuck gate is visible by name in operational status.
- **`measure()`** → read `helium_level()` (and fields) via the station,
  save one datapoint. A stale/disconnected level meter raises here — the
  tick boundary contains it to ERROR with hardware already held at zero
  field, which is the correct failure mode for "we lost our eyes mid-fill".
- **`change_sweep_step()`** → `StepPlan(targets={}, wait_s=sample_period_s)`
  until any of: graceful stop requested (§4.1), level ≥
  `fill_target_pct` and non-rising for `fill_complete_window_s`, or
  `max_fill_duration_s` exceeded (safety timeout) → then `None`.
  `get_progress()` maps level between start and target.
- **`standby()`** → `PhasePlan(commands=(set_refresh_rate SLOW,))`; also on
  `abort()` as the measurement safe-off, so an aborted fill never leaves the
  meter in FAST.

### 4.1 Graceful finish

"Stop filling" must run `standby()` (park the meter, close the file, emit
`run_finished("done")`) — `abort_procedure()` deliberately skips parking.
Add one small, generic Orchestrator API:

- `Orchestrator.finish_procedure()` — request a graceful finish; sets a flag
  the procedure sees (`BaseProcedure.finish_requested`), so the next
  `change_sweep_step()` returns `None` and the normal STANDBY path runs.
  Useful beyond fills (e.g. ending any open-ended monitoring procedure), and
  a clean tool call for the future agent gateway.

## 5. Config (single source of truth)

New optional top-level block in `devices.yaml`:

```yaml
cryogenics:
  level_vi: level_meter          # must name a registered vi_type: level VI
  helium_warning_pct: 35.0       # advisory "please fill" threshold (> safety threshold)
  fill_target_pct: 90.0
  fill_zero_field_eps_T: 0.005
  fill_zero_field_window_s: 10.0
  fill_complete_window_s: 120.0
  max_fill_duration_s: 3600.0
  sample_period_s: 10.0          # fill-time sampling (FAST mode)
  history_sample_s: 300.0        # logbook helium-history decimation
  helium_volume_l: 40.0          # optional; enables L/h display
```

The safety threshold stays where it is (`helium_low_threshold` in the level
VI's `init_params`) — it is an instrument-protection property; the block
above is operational policy. Conformance additions: if the block is present,
`level_vi` must resolve to a registered level VI, thresholds must be
ordered (`helium_warning_pct > helium_low_threshold`), and all durations
positive. Absent block → feature off, nothing to validate.

## 6. Safety layer: what actually needs improving

The zero-field interlock needs **nothing new** — plan targets + gate +
single-writer blocking already guarantee it. Two real changes:

1. **Tolerated safety flags (small Orchestrator + BaseProcedure change).**
   `BaseProcedure.tolerated_safety_flags: frozenset[str] = frozenset()`;
   the fill declares `frozenset({"helium_low"})` — justified because its
   plan forces every magnet to zero and arms no measurement, so low helium
   is not a hazard *for this procedure*; it is its reason to exist. The tick
   subtracts the running procedure's tolerated flags before deciding on
   EMERGENCY; a *non*-tolerated flag (e.g. `quench`) still kills a fill
   instantly. Conformance: any procedure declaring tolerated flags must be
   `run_kind == "service"`, and a behavior test proves a fill survives
   `helium_low` while a quench still aborts it.

2. **Starting a remediation from EMERGENCY.** `run_procedure()` gains one
   carve-out: allowed from EMERGENCY iff every currently-active safety flag
   is tolerated by the submitted procedure (emergency entry already ran
   `standby_all`, so magnets are at zero or heading there). On finish, if
   flags have cleared → IDLE, else back to EMERGENCY. This closes the
   deadlock in §1 with the narrowest possible door: only a procedure that
   explicitly tolerates *exactly* the active condition may run.

Deliberately **not** a safety mechanism: the `helium_warning_pct` advisory.
It is surfaced by the recorder/panel (banner + logbook event), never by
`evaluate_safety()` — warnings that trip EMERGENCY teach operators to
ignore EMERGENCY.

## 7. GUI (`gui/cryogenics_panel.py`)

A third page in the bottom-right stacked quadrant (selector gains
"Cryogenics" beside "Other devices"/"Log"), built by `MonitorWindow` only
when the feature is on — same conditional-composition pattern as the
existing vi_type partitioning. Contents:

- Current He/N₂ levels + consumption rate (%/h, L/h when volume known),
  computed by the shared function in §3.2 from the persistent history.
- A pyqtgraph level-vs-time plot (reusing the `TrendPlotPanel`/
  `MonitorHistory` idioms; window selector 1 h/6 h/24 h) with fill events
  from the logbook overlaid as markers.
- **Fill helium** button → constructs `HeliumFillProcedure` from the config
  block and calls `orchestrator.run_procedure()`; while it runs the button
  becomes **Stop filling** → `orchestrator.finish_procedure()`. Verdicts
  arrive through the existing `action_*`/`run_*` signal surface; no new
  channels.
- Recent fill history: last few `helium_fill` events (start/end, levels,
  duration, status).

Per the GUI standard: theme tokens, no blocking calls, qtbot geometry test,
offscreen screenshot verification.

## 8. Phasing (bottom-up, each phase lands green)

1. **Logbook foundation (L6):** `LogbookEvent` model, `LogbookStore`,
   `HeliumHistoryStore`, consumption function, `CryogenicsRecorder` against
   a mocked Orchestrator; GLOSSARY terms; conformance inherits the models.
2. **Station + procedure (L2/L4):** `magnet_vi_names()`,
   `HeliumFillProcedure` against `sim_cryostat` (zero-field gate, FAST/SLOW
   round-trip, HDF5 fill curve, timeout/target termination); cryogenics
   config block + conformance checks.
3. **Safety extension (L3):** `tolerated_safety_flags`,
   EMERGENCY-start carve-out, `finish_procedure()`; behavior tests for the
   helium_low-during-fill and quench-during-fill matrix.
4. **GUI (top):** the panel, config-gated composition in `MonitorWindow`,
   fill-event overlay, geometry tests + screenshots.

Each phase is independently useful (a logbook without a fill button is
already a logbook; a fill procedure is already runnable from the procedure
window filtered list before the panel exists).
