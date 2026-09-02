# The instrument thread: a responsive GUI without a second hardware writer

**Status: audit complete, proposal awaiting decisions (2026-09-02).**
Written to answer three questions: what threading work already exists on
the remote branches, how it should be merged, and what CryoSoft must become
so that agent operation, live analysis, and eLab export can run *while a
measurement is running*, with the operator-visible behaviour of `main`
preserved. It folds in the 2026-08-08 redline from the session that
produced the groundwork branch (its shipped steps, its withdrawn step, and
its five open decisions), verifies that redline's code claims against
today's `main`, and adds the design of the thread move itself, which the
redline left as a single 3–4 day line. The decisions this document needs
are collected in §8.

The one-sentence design: **the invariant worth keeping is "exactly one
thread touches instruments", not "the process has exactly one thread".**
Move the whole hardware stack (Orchestrator, Station, VIs, drivers, data
manager) onto one dedicated *instrument thread*, leave the GUI, the session
layer, the agent gateway, analysis and network I/O on the main thread, and
let the two sides speak only through Qt queued signals and one proxy object.
The Orchestrator's logic, the tick, the claims, the pause boundary, the
scheduler and the test suite that drives `_tick()` synchronously do not
change.

---

## 1. Audit: what exists on the branches

### 1.1 The branch first named holds no threading work

`claude/unified-offline-instrument-tags-pylahd` (tip `f4b4bd2`) was merged
into `main` as PR #13 on 2026-07-28. It is a strict ancestor of `main` and
its subject is the Availability standard and the connection-lifecycle
standard. Nothing on it concerns threads; there is nothing to merge from it.

### 1.2 No branch anywhere contains a thread

All 32 remote branches were scanned for `QThread`, `QThreadPool`,
`QRunnable`, `moveToThread`, `threading.Thread/Lock/Event` and
`concurrent.futures` inside `cryosoft/`. Every one returns zero files. The
codebase is, today, exactly what `CLAUDE.md` says it is: one `QTimer` on
the GUI thread drives everything.

### 1.3 The groundwork: `claude/monitor-responsiveness-procedures-w9o7bv`

Confirmed as the intended branch. Four commits, dated 2026-08-08, based on
`0d88757`, six commits behind `origin/main`. The session that produced it
set out to implement "Phase 0" of a thread-move plan and, by its own
redline, spent most of its value proving a third of that Phase 0 wrong
(§1.4). What shipped:

| Commit | What it does | Verdict |
|---|---|---|
| `c252291` Step 0.1 — `core/run_builder.py`, the headless factory | One `build_procedure()` replacing two widget-bound constructors in `ProcedureWindow` and `QueuePanel`; imports no Qt (asserted by a test that greps its own source); refuses by raising, never by returning `None`; `PROCEDURE_BUILD_ERRORS` names the refusal tuple. Also fixes a latent crash: `QueuePanel` caught `(TypeError, ValueError)` but a refusing procedure raises `CryoSoftConfigError`, which is not a `ValueError`, so a stale queue entry took the window down on session restore. | **Merge.** A run can now be built with no `QApplication` in the process. First brick of agent operation. |
| `de817f7` Step 0.4a — every refused action gets a verdict | Six entry points refused work silently: pause with no run, pause from a disallowed state, resume outside PAUSED, recover outside ERROR, abort during EMERGENCY, scanner toggle with no switch. Each now emits `action_blocked` naming the state it refused from. | **Merge, with a two-hunk manual resolution** (§1.5). Today a UX repair; after the thread move it is structural: a marshalled call returns before the engine has looked at it, so the emitted verdict is the only evidence the caller ever gets. |
| `6fe4862` Fix-along — path separators rejected on every platform | `a\b` was rejected on Windows and accepted on Linux. A real portability bug, not a test artefact: a folder written on the Linux rig splits into a nested path when opened on a Windows analysis machine. | **Merge.** Also the one test failing on `main` today (§1.6). |
| `bc017c0` Fix-along — `CLAUDE.md` stops counting the contracts | "C1–C12" while `pyproject.toml` held fifteen; the count had rotted twice and misled planning into proposing taken numbers. | **Merge.** The next contract is C16. |

`claude/agent-instrument-framework-audit-4vdfci` is the same four commits
plus `0568f4f`, a 621-line `agent-operative-architecture-audit.md` (under
`docs/plans/` on that branch) auditing the architecture against an external
agent-to-instrument protocol. Its D4 ("No thread. Ever.") and its §8
(corrections to `agentic-instrumentation-framework.md`) bear on this plan;
§3.5 reconciles them.

### 1.4 What the redline withdrew, and what it corrected

**Step 0.3 as planned is withdrawn.** It would have deleted the engine's
`_procedure_queue` and moved the advance decision into the GUI: the queue
watches `state_changed`, sees `"idle"`, starts the next run. An adversarial
review returned 33 blockers; three kill the step outright, and each is
verified against today's `main`:

1. *Operations starve.* `run_queue()` drains `_operation_queue` before
   procedures (the "queue-jumping, not preemption" rule). Deleting its call
   sites (`orchestrator.py:848`, `:2637`) means a queued operation never
   starts. The plan also missed the third caller: the GUI's own Run Queue
   button (`queue_panel.py:163`).
2. *The advance is re-entrant.* `_change_state()` emits `state_changed`
   synchronously (`orchestrator.py:1763`), so a GUI queue reacting to
   `"idle"` would start the next run *inside that emit*, ahead of the
   engine's own `run_queue()`.
3. *It is safety-relevant.* Five transitions to IDLE on `main`
   (`recover_from_error`, `_fail_run_for_fault`, the emergency
   acknowledge, a completed manual ramp, and the abort path) deliberately
   do not chain. `state_changed` carries only the state name, so an external
   listener cannot tell a clean finish from a quench acknowledgement, and
   would auto-start a queued procedure straight after an emergency.

**The replacement inverts the dependency instead of relocating the
scheduler.** `run_queue()` stays, all three call sites stay, operations
still drain first, the five no-chain transitions stay no-chain. The engine
gains a `next_procedure()` callback that a headless `RunQueue` supplies:
the GUI holds specs, the engine holds the one live object, and the engine
keeps sole authority over *when* a run starts. This supersedes the earlier
draft of this document, which proposed public `replace_queue()` /
`clear_queue()` methods; the pull seam is the better answer because it
removes the shared mutable queue entirely instead of wrapping it.

"Refuse if busy" (the GUI declining commands while the engine works) is
dropped outright, not rescheduled: never needed to reshape the seam, and
the same quench-acknowledgement hole applies to it.

**Two corrected claims.** `Station.get_state()` is not free of ownership
work: it mutates `_error_counts` and pops the condition registry via
`clear_fault()`, structures the GUI reads. And the verdict path was not
already complete, which is where 0.4a came from.

### 1.5 Trial merge

A trial merge of the audit branch onto `origin/main` was performed in a
scratch worktree (never pushed). Everything merges automatically except
`core/orchestrator.py`, which conflicts in exactly two hunks, both inside
`pause_procedure()` / `resume_procedure()`, because `main` restructured
those methods around `_enter_paused()` and the deferred pause request
(`6261da2`, `ca1f790`) after the branch was cut.

Resolution, verified by the full suite in the trial worktree (§1.6):

- `pause_procedure()`: keep `main`'s structure (defer from `MEASURING`,
  `_enter_paused()` from the pausable states) and add the branch's verdict
  as the fall-through after the pausable-state branch:
  `self.action_blocked.emit(f"Cannot pause while {self._state.value}")`.
  The "no run is active" verdict at the top merges automatically.
- `resume_procedure()`: keep `main`'s "withdraw a pending pause request"
  path and add the branch's verdict after it:
  `self.action_blocked.emit(f"Cannot resume while {self._state.value}")`.

The branch's refusal tests use `PAUSED` as their non-pausable state, so
they hold unchanged against `main`'s deferred-pause semantics; no test needs
editing.

### 1.6 Verification

Run on Linux, Python 3.11, `QT_QPA_PLATFORM=offscreen`, a fresh `.venv`.

| Check | `origin/main` (`0e25ac9`) | trial merge |
|---|---|---|
| `ruff check .` | pass | pass |
| `lint-imports` | 15 kept, 0 broken | 15 kept, 0 broken |
| `pytest -m "not hardware"` | 2301 passed, **1 failed**, 6 skipped | 2317 passed, 0 failed, 6 skipped |

The one failure on `main` is
`test_session_layer.py::test_start_experiment_rejects_separator_or_dot_dirname[a\\b]`,
exactly the Linux-only bug `6fe4862` fixes. `main` is red on Linux today and
the groundwork merge turns it green. (The redline reported 2304 passing on
its own base; the difference is what `main` gained since.)

---

## 2. Where the GUI actually freezes today

The redline's diagnosis, confirmed: the freeze is one blocked event loop,
not paused monitoring. `_tick_body()` calls `self._procedure.measure()`
inline, so the Qt event loop gets no cycles for the whole reading grid;
nothing repaints because nothing is asked to draw, and the OS paints "Not
responding". Two phases of the tick are long:

**The monitor poll.** `Station.get_state()` calls `vi.get_state()` on every
VI in sequence, and each `@monitored` method is one bus round-trip. On the
`12t-cryo` config that is 36 round-trips per tick (magnet 9, switch 6, level
meter 5, DC mode 5, sample temperature 4, VTI 3, delta mode 3, RTM2 1) over
GPIB and serial, against a 2 s tick. Tens of milliseconds each on a healthy
bus; a full pyvisa timeout (3 to 15 s, per driver) each on an unhealthy one.
A single unresponsive instrument therefore freezes the whole GUI for its
timeout on every tick until the Station marks it disconnected.

**The measurement.** Inside `measure()`, by design and documented as "the
tick's designated blocking phase":

- `SwitchMatrixVI.select_route()` sleeps `settle_time_s` (1.0 s on
  `12t-cryo`) per route change;
- `DCModeMeasurementVI.measure()` reads `n` voltages with `delay_s` between
  them;
- `TensormeterRTM2MeasurementVI.measure()` sleeps
  `averaging_time_s × (n + 1)` after triggering;
- the reading loop multiplies all of that by every `loop1 × loop2` value
  (routes × excitation currents) per datapoint.

A datapoint routinely takes 10 to 60 s; the GUI is frozen for all of it,
and so is every future agent, analysis or export feature that would share
that thread. Nothing in the repository mitigates this: there is no
`QApplication.processEvents()` in `cryosoft/` (correctly; it would make the
tick re-entrant).

The fix is a thread boundary, and the boundary has preconditions. Two
things harmless in a single-threaded program stop working the moment the
engine is elsewhere, and both were repaired on the branch before the move:
a marshalled call returns nothing (hence 0.4a), and construction lived on a
widget (hence 0.1). Two more are identified below and are not yet done:
synchronous reads of engine state (§3.3, step 0.4) and a shared mutable
queue (§1.4, steps 0.2 + 0.3).

---

## 3. Design

### 3.1 The invariant, restated

`CLAUDE.md` today: *"Single-threaded cooperative scheduling. One QTimer tick
drives everything ... There is no second thread and no concurrent bus
access, which is the design's answer to GPIB race conditions. Never add a
thread or a blocking call in the tick path."*

The property that answers GPIB races is *no concurrent bus access*. The
"no second thread" sentence is the implementation that has delivered it so
far, not the property itself. This plan proposes to rewrite the paragraph
as the **single hardware thread standard**:

> Exactly one thread, the instrument thread, ever touches a driver, a VI,
> the Station or the Orchestrator. It runs the one QTimer tick; ramps are
> generators that yield one step per tick. Every other thread (the GUI
> thread first among them) reaches the hardware only by posting a request
> to the instrument thread and reading a snapshot it published. There is
> never a second hardware writer and never a lock around a bus.

### 3.2 Two threads, one direction of ownership

```
GUI thread (main)                          Instrument thread
─────────────────────────────              ─────────────────────────────
QApplication, every widget                 QTimer tick
ExperimentManager (L6)                     Orchestrator (L3), the scheduler
CryogenicsRecorder                         Station (L2), VIs (L1), drivers (L0)
RunQueue (specs) ◄──next_procedure()──     DataManager (L5), procedure (L4)
OrchestratorProxy  ──queued requests──►    TieredTrendLogger, status.jsonl
StationInfo (frozen)                       TrendCheckRunner (holds Station)
StatusSnapshot (frozen, latest)  ◄──queued signals──
future: agent gateway, analysis
        buffer, eLab client
```

Ownership is one-directional. The instrument thread never calls into the
GUI except through the injected `next_procedure()` callback, which is a
pure function over specs and builds via `build_procedure()` on the engine's
thread. The GUI thread never holds a reference to a VI, a driver or the
live `Station` after step 0.4; it holds a proxy and two frozen snapshot
types. `TrendCheckRunner` holds a `Station` and publishes conditions into
it, so it moves with the Station.

### 3.3 No synchronous reads (redline D4, option A)

The largest behavioural change in Phase 0, and the one this document's
design depends on. Today the GUI has around 21 getter call sites into the
engine, six dynamic Station reads, three read-then-act pairs
(`procedure_window.py:458`, `:467`, `:480` check `active_run_kind()` and
act on the answer, a time-of-check race the instant the engine is
elsewhere), three direct writes to `orchestrator._procedure_queue`
(`queue_panel.py`), and one real hardware call from the GUI thread:
`instrument_front_panel.py:93` calls `self._vi.ping()`.

The alternative, a blocking cross-thread invoke with a timeout, is rejected
because it reintroduces exactly the freeze this plan exists to remove: the
engine may be forty seconds deep in `measure()`.

So: the GUI reads only its local mirror, every action is a request answered
by a verdict, every GUI guard clause becomes advisory rather than
authoritative ("ask, and let the engine refuse"), and the front panel's
Check button becomes a request whose result arrives as a signal.

### 3.4 The pieces

**`OrchestratorProxy`** (GUI-side `QObject`). Mirrors the public Orchestrator
surface (39 methods today), split by kind:

- *Commands* (`run_procedure`, `pause_procedure`, `submit_vi_action`,
  `acknowledge`, `connect_instrument`, ...) are forwarded as queued
  invocations. They return `None`; the outcome arrives on the existing
  `action_succeeded` / `action_failed` / `action_blocked` / `state_changed`
  signals. This is why 0.4a is a prerequisite.
- *Queries* (`state`, `availability`, `vi_faults`, `held_vi_names`,
  `override_active`, `manual_override_expires_at`, `active_run_kind`,
  `scanner_enabled`, `offline_reason`, `pause_pending`, `active_ramps`,
  `get_operational_status`, `is_monitoring`) are answered from the latest
  `StatusSnapshot`, never by a cross-thread call.
- *Signals* are re-exposed 1:1, so `orchestrator.states_updated.connect(...)`
  in a widget becomes `proxy.states_updated.connect(...)`. Qt's
  auto-connection picks a queued connection whenever emitter and receiver
  live on different threads, so every existing GUI slot keeps working one
  event-loop hop later.

A conformance test asserts the proxy mirrors every public `Orchestrator`
method with the same signature.

**`StatusSnapshot`** (frozen dataclass), emitted once per tick and on every
`_change_state()`, carrying every query above. **`StateChange`** payload on
`state_changed`: the redline's D1 asks whether `state_changed` should carry
a *cause*; this plan recommends yes (option A), and recommends the cause be
part of one typed, frozen payload standard shared with `StatusSnapshot`
rather than a bare `dict`, so the cross-thread payload contract is set once
while there is a single consumer to migrate.

**`StationInfo`** (frozen dataclass), captured by the Station once at build
(VI names, types, offline registry, monitored and control metadata,
`ParamSpec`s) and re-emitted on `connect_instrument` /
`disconnect_instrument`. Widgets build from it, never from
`Station.get_vi()`. `control_param_specs()` must become a pure read of
config and cached state (the audit document's D1 already asks; the
Lakeshore VI reads hardware there today), enforced by a conformance test.

**`RunQueue`** (headless, holds specs) and the **`next_procedure()`** pull
seam (§1.4). `QueueEntry.proc` is dropped; the queue holds
`(cls, params, sample_info, data_dir, file_prefix)` and the engine calls
`build_procedure()` when `run_queue()` decides it is time. Where it lives is
redline D3; when a spec is validated is redline D2.

**`InstrumentHost`** (`cryosoft/core/instrument_host.py`). Owns the
`QThread`. Builds the Station *inside* the thread so every pyvisa
`ResourceManager` and serial port is opened by the thread that will use it.
Constructs the Orchestrator there, starts and stops its `QTimer` through
queued slots (Qt refuses to start a timer from a foreign thread), and on
shutdown calls `Orchestrator.shutdown()`, quits the thread and joins it
with a bounded wait so a wedged VISA read cannot hang app exit. Two modes,
`threaded` and `inline`; in `inline` mode there is no thread and everything
is constructed on the caller's thread exactly as today. One GUI code path
serves both, which is what lets the whole GUI test suite run unchanged in
`inline` mode and a dedicated suite exercise `threaded` mode.

**Signal payload rule.** Every `dict` the Orchestrator emits is a fresh copy
the emitter never touches again. `Station.get_state()` returns a fresh outer
dict but shares the inner per-VI dicts with `_last_known_state`; copy at the
emit boundary. Frozen dataclasses for the new types.

### 3.5 Reconciling with the two agent-architecture documents

`agentic-instrumentation-framework.md` Phase 5 calls the MCP transport "the
one sanctioned thread" and the riskiest change in the programme. The audit
document's D4 answers "no thread, ever" and proposes a request spool drained
by the tick, a `QLocalServer` slot on the event loop, and an out-of-process
MCP adapter. Both were reasoning about a thread that would *talk to the
Station*. The instrument thread is the opposite move: it takes the hardware
away from the thread everything else runs on. D4's three rungs hold
verbatim; they run on the GUI thread as clients of `OrchestratorProxy`, and
the "transport thread" question dissolves. D4's wording should become "no
second hardware thread"; Phase 5's "one sanctioned thread" is retired. The
audit's §8 corrections to the framework plan are independent of this and
should be folded in regardless.

### 3.6 What is preserved from `main`, and what changes

Preserved, by construction: every hardware write still flows through the
tick and the drain gate; the pause boundary, the claims, the ramp scope,
the condition pipeline, stall detection, the scheduler and its
operations-first rule are untouched; a GUI click still lands *between
ticks* (today between two timer slots on the GUI thread, afterwards between
two timer slots on the instrument thread's event loop); every `_tick()` call
site in the tests keeps driving a plain `Orchestrator` synchronously.

Changed: GUI queries read the last published snapshot, one event-loop hop
stale; GUI guards are advisory; a queued run is validated per redline D2.

Command latency is *not* changed, but becomes visible differently. Today a
Pause or Abort click during a 20 s `measure()` cannot even be received
until it returns. Afterwards the click is received and acknowledged
instantly but still executes when `measure()` returns, because the
instrument thread is inside it. The GUI is responsive; the hardware is
exactly as responsive as before. Faster Abort would need VIs to poll a
cancellation flag between readings (decision 7).

---

## 4. Merge and build plan

Numbering follows the redline so the two documents can be read together.
Each step ends with `make check` green and is independently mergeable.
Nothing before Phase 1 changes behaviour on `main`.

### Phase 0 — the seam, no thread yet

| Step | What | Status / estimate |
|---|---|---|
| **Merge** | Merge `claude/agent-instrument-framework-audit-4vdfci` (five commits) onto `main` with the §1.5 resolution; index the audit document in `docs/plans/README.md`; update `core/README.md`'s `run_builder.py` row. | hours |
| 0.1 | Headless `build_procedure()` | shipped |
| 0.4a | Verdicts on six refusal paths | shipped |
| 0.2 | Queue as data: `RunQueue` of specs, drop `QueueEntry.proc`, remove the three direct writes to `_procedure_queue`. Validation timing per decision 2; home per decision 3. | 1 d |
| 0.3 | `next_procedure()` pull seam on the engine; `run_queue()` and all three call sites kept. Coupled to 0.2; land as one change. | 1 d |
| 0.4 | Engine mirror: `StatusSnapshot`, `StateChange` cause (decision 1), `StationInfo`, pure `control_param_specs()`, the payload copy rule, and the removal of every synchronous read listed in §3.3 including the front panel's `ping()`. | 2–3 d |
| 0.5 | Contract **C16**: `cryosoft.gui` may not import `cryosoft.core.station` except under `TYPE_CHECKING`; plus the signal-payload standard written into `core/README.md` and `GLOSSARY.md`. Conformance tests: proxy mirrors every public method; no `time.sleep` under `gui/`; no plan citations in code. | 0.5 d |
| 0.6 | Capability manifest with two renderers (GUI panels and agent tool schema); channel metadata per decision 5. **Off the thread's critical path**: needed for the agent gateway, not for the move. | 1–1.5 d, schedulable later |

### Phase 1 — the thread move

| Step | What | Estimate |
|---|---|---|
| 1.1 | `OrchestratorProxy` and `InstrumentHost(mode="inline")`. `main.py` builds the host and hands the proxy and `StationInfo` to the GUI; `ExperimentManager.set_experiment_envelope` and the recorder go through the proxy. Every widget takes the proxy. The GUI test suite runs unchanged in `inline` mode: the proof the proxy is transparent. | 2 d |
| 1.2 | `InstrumentHost(mode="threaded")`: `QThread`, Station built in-thread, timer start/stop via queued slots, bounded shutdown join, a thread-level exception boundary (the tick's own boundary degrades to `ERROR`; the thread must additionally never die silently: `CRITICAL` log, emit, keep the event loop alive so `shutdown()` can still reach it). `TrendCheckRunner` moves with the Station. | 2 d |
| 1.3 | `tests/test_instrument_thread.py`: sim station, real `QThread`, `qtbot.waitSignal` on every command's verdict, a frozen-GUI detector (a 50 ms GUI-thread timer must keep firing while a sim `measure()` sleeps 2 s), shutdown with a hung VI, the pause boundary end-to-end across the thread, and the redline's quench-then-queued-procedure scenario proving nothing auto-starts after an emergency acknowledge. | 1 d |
| 1.4 | Selection via `monitor.yaml` (`instrument_thread: true|false`) with `CRYOSOFT_INSTRUMENT_THREAD` as the CI override. Default `false`. Soak on `sim_real_cryostat` (1 s tick) under a scripted click storm for a multi-hour sim run; then on hardware. | 1–2 d + soak |

### Phase 2 — default on, and the standard

Flip the default; decide whether `inline` mode stays (decision 8); rewrite
the `CLAUDE.md` paragraph per §3.1; add **Instrument thread**, **Orchestrator
proxy**, **Status snapshot**, **Station info**, **Run queue** to
`GLOSSARY.md`; update `core/README.md`, `gui/README.md`; correct
`agentic-instrumentation-framework.md` Phase 5 and the audit document's D4
per §3.5. One day.

### After this plan: the features it exists for

- **Agent operation**: framework plan Phases 0–4 unchanged; the gateway is
  a GUI-thread client of the proxy; the live write path is D4 rung 1 or 2;
  MCP is an out-of-process adapter. 0.6's manifest feeds the tool schema.
- **Live analysis**: needs a data source that is not the HDF5 file the
  instrument thread is writing (decision 9).
- **eLab export**: network I/O on the GUI thread via `QNetworkAccessManager`
  or a network worker, triggered from `run_finished` with the run manifest;
  a session-layer (L6) feature (decision 10).

---

## 5. Risks and how each is bounded

| Risk | Bound |
|---|---|
| A widget keeps a live `Station`/VI reference and calls it from the GUI thread (a bus write from the wrong thread). | Step 0.4 removes every such call; C16 forbids the import; in `threaded` mode the host can hand the GUI a `Station` wrapper whose every method raises. |
| A GUI-side advance re-enters the engine or auto-starts after an emergency. | The scheduler never leaves the engine (§1.4); `state_changed` carries a cause (decision 1); test 1.3's quench scenario. |
| Time-of-check races on `active_run_kind()` and friends. | No synchronous reads (§3.3); guards become advisory and the engine refuses. |
| pyvisa/NI-VISA or a serial port opened on one thread and used on another. | Station is built inside the instrument thread; `connect_instrument()` already rebuilds a driver session and runs there by construction. |
| A hung VISA read blocks `shutdown()`. | Bounded join; `CRITICAL` log; the GUI exits. The instrument is left where it is, exactly as a process kill leaves it today. |
| Cross-thread mutation of an emitted payload. | Copy rule plus frozen dataclasses (step 0.4). |
| The GUI test suite silently stops covering the real wiring. | Every GUI test runs through the proxy in `inline` mode; the `threaded` suite covers the boundary itself. |
| Flakiness from real threads under pytest-qt. | Only `test_instrument_thread.py` starts a `QThread`; explicit `waitSignal` timeouts; sim station; marked for isolated retry in CI. |
| Doubling the GUI test matrix forever if `inline` mode is kept. | Decision 8. |

---

## 6. Estimate

Merge: hours. Phase 0 remaining (0.2 through 0.5): 4.5 to 5.5 days; 0.6 is
1 to 1.5 days more when the agent gateway needs it. Phase 1: 6 to 7 days
plus soak. Phase 2: one day. Roughly three weeks to a threaded default with
behaviour parity, before any agent, analysis or eLab feature starts. The
redline's own tally (9–12 days for Phase 0 plus its 3–4 day Phase 1) was
about a week short because the thread move was a single line; §4 Phase 1
is what that line expands to.

---

## 7. What this plan deliberately does not do

- It does not parallelise instrument I/O (no per-instrument threads, no
  thread pool). One bus, one writer.
- It does not move the scheduler out of the engine (§1.4).
- It does not make `measure()` cooperative or interruptible (decision 7).
- It does not change tick semantics, the state machine, procedures, VIs or
  drivers.
- It does not edit or weaken an import contract; C16 is an addition.

Noticed, not changed, and out of scope here: `procedure_window.py:452` and
`:475` still cite `operation-concurrency-and-error-scoping.md §2` in
docstrings, and nine sites in `main.py`, `gui/` and `session/` cite
`docs/plans/session-tier-and-terminology.md`. Both violate the
code-reference standard; the concept each names is already in the same
sentence, so the fix is to drop the citation. Step 0.5's conformance test
would catch these, so they must be cleaned before it lands.

---

## 8. Decisions needed

Recommendation first in each. Decisions 1 to 5 are the redline's, carried
over unchanged in substance; they block Phase 0. Decisions 6 to 11 come
from this audit; 6 blocks the merge, 7 and 8 block Phase 1, and 9 to 11 can
wait until Phase 2.

**1 — Does `state_changed` carry a reason?** *(blocks 0.2, 0.3, 0.4)*
Today `pyqtSignal(str)`. Five IDLE transitions do not chain, so `"idle"`
alone cannot distinguish a clean finish from a quench acknowledgement. Once
an agent or scheduler subscribes, and once it is a cross-thread payload
contract, this is much harder to change.
(A, recommended) Widen it now with a cause, as a typed frozen `StateChange`
payload, and set the payload standard while there is one consumer.
+0.5 d. (B) A second richer signal beside it: cheaper, two channels to
keep consistent forever. (C) Leave it, and accept that no out-of-process
client may ever decide to advance.

**2 — Is a queued run validated when added or when started?** *(blocks
0.2)* Today Add to queue constructs the procedure immediately, so a bad
parameter set is refused while the operator is at the form; a queue of
specs would surface the same refusal an hour later, mid-batch. Verified:
`BaseProcedure.__init__` touches no hardware and claims nothing, so
building twice is safe.
(A, recommended now) Build and discard at add time, build for real at
start. Today's behaviour exactly, one throwaway construction. (B) Split
`__init__` into validate and build: cleanest, touches every procedure, and
the only option that gives an agent a dry-run check; do it when the gateway
needs it. (C) Accept late failure with a "checked at start" queue state.

**3 — Where does the run queue live?** *(blocks 0.2, 0.5; sets C16's
wording)* It must sit below the GUI for an agent to drive it. Today queue
persistence is a GUI concern (`QueueItemState` in `gui/form_autosave.py`).
(A, recommended) `core/run_queue.py` owns order only; persistence stays
the caller's; the engine never imports it, the callback is injected.
(B) `session/run_queue.py` owns order and persistence, pulling
`QueueItemState` out of the GUI: better long-term home, larger diff.

**4 — Does the GUI ever read engine state synchronously?** *(blocks 0.4)*
(A, recommended, and assumed by every section above) No synchronous path;
local mirror plus request-and-verdict; the front panel's Check becomes a
request. (B) A blocking invoke with timeout: nearly mechanical, but it
reintroduces the freeze whenever the engine is inside `measure()`.

**5 — Do `@monitored` channels gain units and descriptions now?** *(blocks
0.6 only)* Channel keys are written into on-disk trend history.
(A, recommended) Metadata as a sidecar, keys untouched, additive across
every VI. (B) Restructure keys and migrate history files.

**6 — Merge scope.** (A, recommended) All five commits including the
621-line audit document, indexed as an audit that informs the framework
roadmap, its §8 corrections folded into the framework plan in a follow-up.
(B) Only the four code commits.

**7 — Abort and pause latency inside `measure()`.** (A, recommended for
this plan) A command lands after the current `measure()` returns, as today.
(B) Additionally give measurement VIs a cooperative cancellation check
between readings so Abort interrupts a datapoint: real VI-layer work (the
RTM2's single `(n+1) × averaging_time_s` sleep would need splitting) and a
new standard; its own plan if wanted.

**8 — Keep `inline` mode after Phase 2?** (A, recommended) Keep it for one
release as the hardware escape hatch and the mode the fast GUI tests run
in; decide again after a month of `threaded` on real hardware. (B) Delete
it at Phase 2 and run every GUI test threaded.

**9 — Live analysis data source.** (A, recommended) An in-memory
`RunBuffer` on the GUI thread fed by the existing `measurement_ready`
signal, which already carries every datapoint: zero file contention.
(B) HDF5 SWMR on the open file (h5py is not safe for one file across two
threads without it, and it is a format-level commitment). (C) Analysis only
on completed runs.

**10 — eLab scope.** Which system (elabFTW assumed), and which trigger:
automatic on `run_finished`, a manual "export to eLab" action, or both.
Not needed to start Phases 0–2.

**11 — The standard's wording.** Approve rewriting the `CLAUDE.md`
"Single-threaded cooperative scheduling" paragraph as the single hardware
thread standard (§3.1) at Phase 2. Until then the existing wording stands
and Phase 0 complies with it literally.
