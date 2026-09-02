# The instrument thread: a responsive GUI without a second hardware writer

**Status: audit complete, proposal awaiting decisions (2026-09-02).**
Written to answer three questions: what threading work already exists on
the remote branches, how it should be merged, and what CryoSoft must become
so that agent operation, live analysis, and eLab export can run *while a
measurement is running*, with the operator-visible behaviour of `main`
preserved. The decisions this document needs are collected in §8.

The one-sentence design: **the invariant worth keeping is "exactly one
thread touches instruments", not "the process has exactly one thread".**
Move the whole hardware stack (Orchestrator, Station, VIs, drivers, data
manager) onto one dedicated *instrument thread*, leave the GUI, the session
layer, the agent gateway, analysis and network I/O on the main thread, and
let the two sides speak only through Qt queued signals and one proxy object.
The Orchestrator's logic, the tick, the claims, the pause boundary and the
1,900-test suite that drives `_tick()` synchronously do not change.

---

## 1. Audit: what exists on the branches

### 1.1 The named branch holds no threading work

`claude/unified-offline-instrument-tags-pylahd` (tip `f4b4bd2`, "Make the
availability policy table enforce what it claims") was merged into `main`
as PR #13 on 2026-07-28. It is a strict ancestor of `main`, 49 commits
behind it, and its subject is the Availability standard and the
connection-lifecycle standard: offline instrument tags, `not_responding`,
the `TAG_POLICY` table. Nothing on it concerns threads. There is nothing to
merge from it.

### 1.2 No branch anywhere contains a thread

All 32 remote branches were scanned for `QThread`, `QThreadPool`,
`QRunnable`, `moveToThread`, `threading.Thread/Lock/Event` and
`concurrent.futures` inside `cryosoft/`. Every one returns zero files. The
codebase is, today, exactly what `CLAUDE.md` says it is: one `QTimer` on
the GUI thread drives everything.

### 1.3 The groundwork that does exist: `claude/monitor-responsiveness-procedures-w9o7bv`

This is almost certainly the branch the request had in mind. Four commits,
dated 2026-08-08, six commits behind `origin/main`. Its second commit's
message states the intent outright: *"This is a prerequisite for moving the
Orchestrator off the GUI thread rather than a cosmetic fix. A marshalled
cross-thread call cannot return a value, so the emitted verdict becomes the
only evidence a caller ever gets that its request was seen."*

| Commit | What it does | Verdict |
|---|---|---|
| `6fe4862` Reject both path separators in experiment folder names on every platform | Bug fix in `session/manager.py`: `"a\b"` was accepted as a folder name on Linux and rejected on Windows. Fixes a failing test on Linux. | **Merge.** Independent of threading, correct, tested. |
| `bc017c0` Stop restating the layer-contract count in CLAUDE.md | `CLAUDE.md` said "C1–C12"; `pyproject.toml` holds 15. Names `pyproject.toml` as the only source of truth. | **Merge.** Trivial and currently true. |
| `c252291` Extract the headless procedure factory as `core/run_builder.py` | One `build_procedure()` replacing two near-identical constructors in `ProcedureWindow` and `QueuePanel`; imports no Qt; `PROCEDURE_BUILD_ERRORS` names the refusal exceptions. Also fixes a latent crash: `QueuePanel` caught `(TypeError, ValueError)` but a refusing procedure raises `CryoSoftConfigError`, which is not a `ValueError`, so a stale queue entry took the window down on session restore. 171 lines of tests. | **Merge.** This is the first brick of agent operation: a run can be built from a script or a remote client. It is also exactly what the framework plan's Phase 4 gateway needs. |
| `de817f7` Give every refused lifecycle action an explicit verdict | `pause_procedure`, `resume_procedure`, `recover_from_error`, `abort_procedure` (during EMERGENCY) and `set_scanner_enabled` emitted nothing when they refused. Each now emits `action_blocked` with the reason, reusing the channel `submit_vi_action` already uses. 68 lines of tests. | **Merge, with a two-hunk manual resolution.** Conflicts with `main`'s pause-boundary rewrite (`6261da2`, `ca1f790`), see §1.5. The behaviour it adds is a *precondition* for the instrument thread: once a call is marshalled across threads, a silent refusal is undiagnosable. |

`claude/agent-instrument-framework-audit-4vdfci` is the same four commits
plus `0568f4f`, a 621-line `agent-operative-architecture-audit.md` (under
`docs/plans/` on that branch) that audits the architecture against an external agent-to-instrument
protocol. Its §4 D4 ("No thread. Ever.") and its §8 (corrections to
`agentic-instrumentation-framework.md`) bear directly on this plan; see
§3.4 for how they reconcile with an instrument thread.

### 1.4 What the groundwork does *not* do

Neither branch moves anything off the GUI thread, adds a proxy, snapshots
Station metadata for the GUI, or removes the GUI's private access to the
Orchestrator's queue. Those are the actual threading work and they are
specified in §4 of this document. The branch is groundwork, correctly
sequenced, about ten percent of the way.

### 1.5 Trial merge

A trial merge of the audit branch onto `origin/main` was performed in a
scratch worktree (never pushed). Result:

- `session/manager.py`, `gui/procedure_window.py`, `gui/queue_panel.py`,
  `core/run_builder.py`, `core/README.md`, `CLAUDE.md`, both test files and
  the audit document merge automatically.
- `core/orchestrator.py` conflicts in exactly two hunks, both inside
  `pause_procedure()` / `resume_procedure()`, because `main` restructured
  those methods around `_enter_paused()` and the deferred pause request
  after the branch was cut.

Resolution, verified by the full suite in the trial worktree (results in
§1.6):

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
exactly the Linux-only bug `6fe4862` fixes. So `main` is red on Linux today
and the groundwork merge turns it green, adding sixteen tests.

---

## 2. Where the GUI actually freezes today

The freeze is structural, not a bug. Every tick runs on the GUI thread
inside a `QTimer` slot (`core/orchestrator.py`, `_tick()`), so for the
duration of one tick no paint, click or keypress is processed. Two phases of
the tick are long:

**The monitor poll.** `Station.get_state()` calls `vi.get_state()` on every
VI in sequence, and each `@monitored` method is one bus round-trip. On the
`12t-cryo` config that is 36 round-trips per tick (magnet 9, switch 6, level
meter 5, DC mode 5, sample temperature 4, VTI 3, delta mode 3, RTM2 1) over
GPIB and serial, against a 2 s tick. Tens of milliseconds each on a healthy
bus; a full pyvisa timeout (3 to 15 s, per driver) each on an unhealthy one.
A single unresponsive instrument therefore freezes the whole GUI for its
timeout on every tick until the Station marks it disconnected.

**The measurement.** In `MEASURING` the tick calls `self._procedure.measure()`
synchronously. Inside it, by design and documented as "the tick's
designated blocking phase":

- `SwitchMatrixVI.select_route()` sleeps `settle_time_s` (1.0 s on
  `12t-cryo`) per route change;
- `DCModeMeasurementVI.measure()` reads `n` voltages with `delay_s` between
  them;
- `TensormeterRTM2MeasurementVI.measure()` sleeps
  `averaging_time_s × (n + 1)` after triggering;
- the reading loop multiplies all of that by every `loop1 × loop2` value
  (routes × excitation currents) per datapoint.

A datapoint routinely takes tens of seconds; the GUI is frozen for all of
it, and so is every future agent, analysis or export feature that would
share that thread.

Nothing in the repository mitigates this: there is no
`QApplication.processEvents()` in `cryosoft/` (correctly; it would make the
tick re-entrant), and the only `time.sleep` calls outside drivers are the
three measurement sleeps above.

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

Everything below follows from taking that sentence seriously.

### 3.2 Two threads, one direction of ownership

```
GUI thread (main)                          Instrument thread
─────────────────────────────              ─────────────────────────────
QApplication, every widget                 QTimer tick
ExperimentManager (L6)                     Orchestrator (L3)
CryogenicsRecorder, TrendCheckRunner*      Station (L2), VIs (L1), drivers (L0)
OrchestratorProxy  ──queued requests──►    DataManager (L5), procedure (L4)
StationInfo (frozen)                       TieredTrendLogger, status.jsonl
StatusSnapshot (frozen, latest)  ◄──queued signals──
future: agent gateway, analysis
        buffer, eLab client
```

`*` `TrendCheckRunner` holds a `Station` and calls
`Station.publish_conditions()`; it must move to the instrument thread with
the Station (it is a `QObject` with its own timer, so this is a
`moveToThread` at construction).

Ownership is one-directional. The instrument thread never calls into the
GUI; it emits. The GUI thread never holds a reference to a VI, a driver or
the live `Station` after Phase 2 below; it holds a proxy and two frozen
snapshot types.

### 3.3 The three new pieces

**`InstrumentHost`** (proposed `cryosoft/core/instrument_host.py`, or
`cryosoft/runtime.py` as a leaf beside `main.py`; decision D2). Owns the
`QThread`. Builds the Station *inside* the thread so every pyvisa
`ResourceManager` and serial port is opened by the thread that will use it
(VISA sessions are thread-affine in practice even where the spec says
otherwise). Constructs the Orchestrator there, starts and stops its
`QTimer` through queued slots (Qt refuses to start a timer from a foreign
thread), and on shutdown calls `Orchestrator.shutdown()`, quits the thread
and joins it with a bounded wait so a wedged VISA read cannot hang app exit
forever. Runs in two modes: `threaded` and `inline`. In `inline` mode there
is no thread; everything is constructed on the caller's thread exactly as
today. Both modes share one code path in the GUI, which is what lets the
whole GUI test suite run unchanged in `inline` mode and a dedicated suite
run in `threaded` mode.

**`OrchestratorProxy`** (GUI-side `QObject`). Mirrors all 39 public
Orchestrator methods, split by kind:

- *Commands* (`run_procedure`, `pause_procedure`, `submit_vi_action`,
  `acknowledge`, `connect_instrument`, ...): forwarded as queued
  invocations to the Orchestrator's thread. They return `None`. Their
  outcome arrives on the existing `action_succeeded` / `action_failed` /
  `action_blocked` / `state_changed` signals, which is precisely why the
  branch's verdict commit is a prerequisite.
- *Queries* (`state`, `availability`, `vi_faults`, `held_vi_names`,
  `override_active`, `manual_override_expires_at`, `active_run_kind`,
  `scanner_enabled`, `offline_reason`, `pause_pending`, `active_ramps`,
  `get_operational_status`, `is_monitoring`): answered from the latest
  `StatusSnapshot`, never by a cross-thread call. Most of these have no
  signal today; §4 Phase 1 adds one.
- *Signals*: re-exposed 1:1, so `orchestrator.states_updated.connect(...)`
  in a widget becomes `proxy.states_updated.connect(...)` and nothing else
  changes. Qt's auto-connection picks a queued connection whenever the
  emitter and receiver live on different threads, so every existing GUI
  slot keeps working; it just runs one event-loop hop later.

A conformance test asserts the proxy mirrors every public method of
`Orchestrator` with the same signature, so a method added to one cannot be
forgotten on the other.

**`StatusSnapshot`** and **`StationInfo`** (frozen dataclasses). The first
is emitted by the Orchestrator once per tick and on every state change and
carries everything a query above needs. The second is captured by the
Station once at build (VI names, types, offline registry, monitored and
control method metadata, `ParamSpec`s) and re-emitted whenever
`connect_instrument` / `disconnect_instrument` changes it. The GUI's
`InstrumentPanel` and `MonitorWindow` build their controls from
`StationInfo`, not from `Station.get_vi()`.

### 3.4 Reconciling with the two agent-architecture documents

`agentic-instrumentation-framework.md` Phase 5 calls the MCP transport
"the one sanctioned thread" and warns it is the riskiest change in the
programme because it would be *the* second thread. The audit document's D4
answers "no thread, ever" and proposes a request spool drained by the tick,
a `QLocalServer` slot on the event loop, and an out-of-process MCP adapter.

Both were reasoning about a thread that would *talk to the Station*. The
instrument thread is the opposite move: it takes the hardware *away* from
the thread everything else runs on. Once it exists, D4's three rungs still
hold verbatim, they just run on the GUI thread as clients of
`OrchestratorProxy`, and the "transport thread" question dissolves: an MCP
adapter can be a separate process (D4 rung 3) or an asyncio server on the
GUI thread's event loop; neither is a hardware thread. The wording of D4
should become "no second hardware thread", and Phase 5's "one sanctioned
thread" should be retired as a concept. §8 of the audit document (the
corrections to the framework plan) is independent of this and should be
folded in regardless (decision D3).

### 3.5 What is preserved from `main`, and the one thing that changes

Preserved, by construction:

- Every hardware write still flows through the Orchestrator's tick and
  drain gate; `_manual_action_admissible()` is still evaluated at drain.
- The pause boundary, the claims, the ramp scope, the condition pipeline,
  stall detection: untouched code.
- A GUI click still lands *between ticks*. Today the click's slot runs on
  the GUI thread between two timer slots; after the move, the queued
  invocation runs on the instrument thread's event loop between two timer
  slots. Same ordering, same code path.
- Every one of the 56 `_tick()` call sites across 5 test files keeps
  driving a plain `Orchestrator` synchronously. The Orchestrator does not
  learn about threads; the host does.

The one observable change: **GUI queries read the last published snapshot,
not live state.** A getter can be one event-loop hop stale. For the status
bar, availability badges and fault lists this is invisible. For the queue
panel it forces a real fix: it currently reaches into
`Orchestrator._procedure_queue` directly (three sites in
`gui/queue_panel.py`), which is unsafe cross-thread and must become public
`replace_queue()` / `clear_queue()` methods plus a `queue_changed` signal.

Command latency is *not* changed, but it becomes visible in a new way.
Today a Pause or Abort click during a 20 s `measure()` cannot even be
received until `measure()` returns. After the move the click is received
and acknowledged instantly (the button responds, the status line can say
"pause requested"), but the request still executes only when `measure()`
returns, because the instrument thread is inside it. The GUI is responsive;
the hardware is exactly as responsive as before. Making Abort faster would
require VIs to poll a cancellation flag between readings (decision D5).

---

## 4. Merge and build plan

Each phase ends with `make check` green and is independently mergeable.
Nothing in phases 0 to 2 changes behaviour on `main`.

### Phase 0 — Merge the groundwork *(hours)*

1. Merge `claude/agent-instrument-framework-audit-4vdfci` (or cherry-pick
   its five commits) onto `main`, resolving the two `orchestrator.py` hunks
   exactly as §1.5 and adjusting the one pause-verdict test.
2. Index `agent-operative-architecture-audit.md` in `docs/plans/README.md`
   as an audit that informs the framework roadmap (decision D3 decides
   whether its §8 corrections are folded into the framework plan in the
   same commit or later).
3. Update `core/README.md`'s `run_builder.py` row (the branch added it) and
   check the GLOSSARY needs nothing.

### Phase 1 — Complete the public API, no thread yet *(2–3 days)*

Behaviour unchanged; every item is a prerequisite for a safe proxy.

1. `StatusSnapshot` frozen dataclass and a `status_snapshot` signal emitted
   per tick and on every `_change_state()`; every GUI query listed in §3.3
   has a field in it.
2. `StationInfo` frozen dataclass, built by `Station` once and re-emitted on
   connect/disconnect. `InstrumentPanel`, `MonitorWindow`,
   `ProcedureWindow` and `QueuePanel` build from it. `control_param_specs()`
   becomes a pure read of config and cached state (the audit's D1 already
   asks for this; the Lakeshore VI reads hardware there today), enforced by
   a conformance test.
3. Public `replace_queue()` / `clear_queue()` / `queue_changed`; delete the
   three private accesses in `gui/queue_panel.py`.
4. Signal payload rule: every `dict` the Orchestrator emits is a fresh copy
   the emitter never touches again. `Station.get_state()` already returns a
   fresh outer dict but shares the inner per-VI dicts with
   `_last_known_state`; copy at the emit boundary.
5. Conformance tests: no module under `cryosoft/gui/` imports
   `cryosoft.core.station.Station` for anything but the type; no `time.sleep`
   under `cryosoft/gui/`; every public `Orchestrator` method appears in the
   proxy (test written now against a stub, activated in Phase 2).

### Phase 2 — Proxy and host in `inline` mode *(2–3 days)*

1. `OrchestratorProxy` and `InstrumentHost(mode="inline")`.
2. `main.py` constructs the host and hands the proxy and `StationInfo` to
   the GUI. `ExperimentManager.set_experiment_envelope` and the recorder
   connect through the proxy.
3. Every widget takes the proxy. The GUI test suite runs unchanged in
   `inline` mode; this is the proof that the proxy is transparent.

### Phase 3 — `threaded` mode behind a flag *(1 week including soak)*

1. `InstrumentHost(mode="threaded")`: `QThread`, Station built in-thread,
   timer start/stop via queued slots, bounded shutdown join, thread-level
   exception boundary (the tick's own boundary degrades to `ERROR`; the
   thread must additionally never die silently: log `CRITICAL`, emit, and
   keep the event loop alive so `shutdown()` can still reach it).
2. `TrendCheckRunner` moved to the instrument thread with the Station.
3. A `tests/test_instrument_thread.py` suite: sim station, real `QThread`,
   `qtbot.waitSignal` on every command's verdict, a frozen-GUI detector
   (a 50 ms GUI-thread timer must keep firing while a sim `measure()`
   sleeps 2 s), shutdown-with-hung-VI, and the pause-boundary scenario
   end-to-end across the thread boundary.
4. Selection via `monitor.yaml` (`instrument_thread: true|false`) with the
   environment override `CRYOSOFT_INSTRUMENT_THREAD` for CI matrices.
   Default `false` until soak is done.
5. Soak on `sim_real_cryostat` (1 s tick) for a full multi-hour sim run
   with the GUI driven by a scripted click storm; then on hardware.

### Phase 4 — Default on, and the standard *(1 day)*

1. Flip the default. Decide whether `inline` mode stays (decision D4).
2. Rewrite the `CLAUDE.md` paragraph per §3.1; add **Instrument thread**,
   **Orchestrator proxy**, **Status snapshot**, **Station info** to
   `GLOSSARY.md`; update `core/README.md`, `gui/README.md`.
3. Update `agentic-instrumentation-framework.md` Phase 5 and the audit
   document's D4 wording per §3.4.

### After this plan: the features it exists for

Each is its own plan; listed so the ordering is explicit.

- **Agent operation**: framework plan Phases 0–4 unchanged; the gateway is
  a GUI-thread client of the proxy; the live write path is D4 rung 1 or 2
  on the GUI thread; MCP is an out-of-process adapter (decision D7).
- **Live analysis**: needs a data source that is not the HDF5 file the
  instrument thread is writing (h5py is not safe for one file across two
  threads, and SWMR mode is a format-level commitment). Recommended: an
  in-memory `RunBuffer` on the GUI thread fed by the existing
  `measurement_ready` signal, which already carries every datapoint
  (decision D6).
- **eLab export**: network I/O on the GUI thread via `QNetworkAccessManager`
  (asynchronous, never blocks) or a dedicated network worker; triggered from
  `run_finished` with the run manifest; belongs to the session layer (L6).
  Which eLab system and which trigger is decision D8.

---

## 5. Risks and how each is bounded

| Risk | Bound |
|---|---|
| A widget keeps a live `Station`/VI reference and calls it from the GUI thread (a bus write from the wrong thread, the exact race the design forbids). | Phase 1 conformance tests; `StationInfo` replaces every such use; in `threaded` mode the host can hand the GUI a `Station` wrapper whose every method raises. |
| pyvisa/NI-VISA or a serial port opened on one thread and used on another. | Station is built inside the instrument thread (Phase 3 step 1). `connect_instrument()` already rebuilds a driver session; it runs on the instrument thread by construction. |
| A hung VISA read blocks `shutdown()`. | Bounded join; `CRITICAL` log; the GUI exits. The instrument is left where it is, exactly as a process kill leaves it today. |
| Cross-thread mutation of an emitted payload. | Phase 1 step 4 copy rule plus frozen dataclasses for the two new types. |
| The GUI test suite silently stops covering the real wiring. | Every GUI test runs through the proxy in `inline` mode (Phase 2); the `threaded` suite covers the boundary itself. |
| Test-time flakiness from real threads under pytest-qt. | The `threaded` suite is the only one that starts a `QThread`; it uses `waitSignal` with explicit timeouts and a sim station; it is marked so CI can retry it in isolation. |
| Doubling the GUI test matrix forever if `inline` mode is kept. | Decision D4. |

---

## 6. Estimate

Phase 0: hours. Phases 1–2: about one week. Phase 3: about one week
including soak. Phase 4: one day. Roughly three weeks to a threaded default
with behaviour parity, before any agent, analysis or eLab feature is
started. The groundwork branch covers Phase 0 and a small part of Phase 1.

---

## 7. What this plan deliberately does not do

- It does not parallelise instrument I/O (no per-instrument threads, no
  thread pool). One bus, one writer.
- It does not make `measure()` cooperative or interruptible (decision D5).
- It does not change tick semantics, the state machine, procedures, VIs or
  drivers.
- It does not touch the import-linter contracts. The proxy lives in `core`
  beside the Orchestrator (or in a leaf runtime module) and imports
  downward only; a new contract may be *added* to forbid `cryosoft.gui`
  from importing `cryosoft.core.station` except for typing.

---

## 8. Decisions needed before Phase 1

The recommendation is stated first in each.

**D1 — Architecture.** One instrument thread hosting Orchestrator + Station
+ drivers + data manager (recommended), versus keeping the Orchestrator on
the GUI thread and offloading only I/O (rejected: it creates a second
hardware writer and needs locks around every bus), versus a separate
process (heavier, no Qt signals, and it forecloses the in-process gateway
the framework plan is built on).

**D2 — Where the host lives.** `cryosoft/core/instrument_host.py` beside
the Orchestrator (recommended; it is L3 hosting) versus a new leaf
`cryosoft/runtime.py` beside `main.py`.

**D3 — Groundwork merge scope.** Merge all five commits including the
621-line audit document and index it (recommended), versus merging only the
four code commits. And: fold the audit's §8 corrections into the framework
plan in the same commit, or later.

**D4 — Keep `inline` mode after Phase 4?** Keep it (recommended for one
release: it is the escape hatch on hardware and the mode the fast GUI tests
run in), then decide again once `threaded` has soaked on real hardware for
a month. Or delete it at Phase 4 and run every GUI test threaded.

**D5 — Abort/Pause latency inside `measure()`.** Accept that a command
lands after the current `measure()` returns, as today (recommended for this
plan), or additionally give measurement VIs a cooperative cancellation
check between readings so Abort interrupts a datapoint. The second is real
VI-layer work (the RTM2's single `(n+1) × averaging_time_s` sleep would
need splitting) and a new standard; it should be its own plan if wanted.

**D6 — Live analysis data source.** In-memory `RunBuffer` on the GUI
thread fed by `measurement_ready` (recommended: zero file contention,
already carries every datapoint), versus HDF5 SWMR on the open file, versus
analysis only on completed runs.

**D7 — Agent transport.** Confirm the framework plan's sequence with the
audit's D4 rungs (in-process gateway on the GUI thread, then a request
spool or `QLocalServer`, then an out-of-process MCP adapter). This plan
removes the "one sanctioned thread" from that roadmap; confirm that the
MCP server never runs in-process on a thread of its own.

**D8 — eLab scope.** Which system (elabFTW is assumed), and which trigger:
automatic on `run_finished` with the run manifest and data path, or a
manual "export to eLab" action, or both. This decides whether it is a
session-layer feature with a network client, and it is not needed to start
Phases 0–4.

**D9 — The standard's wording.** Approve rewriting the `CLAUDE.md`
"Single-threaded cooperative scheduling" paragraph as the single hardware
thread standard in §3.1 at Phase 4. Until then the existing wording stands
and Phases 0–2 comply with it literally.
