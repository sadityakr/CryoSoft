# Development plan: from data contracts to agent operation

**Status: build sequence, proposed 2026-09-02.** This document sequences
work that four other documents design. It adds no design of its own. Each
step names the document that owns it, so a reader who wants the reasoning
goes there, and a reader who wants the order stays here.

| Owner document | What it owns |
|---|---|
| `agentic-instrumentation-framework.md` | the nine modules, the manifest, verdicts, results, safe state, the gateway, MCP, GUI surfaces, the embedded assistant |
| `instrument-thread-and-responsive-gui.md` | the control contract, the instrument thread, the proxy, the pull seam, the status mirror |
| `control-ui-groups.md` | titled UI groups on `@monitored` / `@control`, grouped front panels, the group half of the manifest |
| `archive/session-management-layer.md` Part B | the ELN adapter contract and the offline-first publisher |
| `agent-operative-architecture-audit.md` (on the audit branch until Phase A merges it) | closing the direct write path, actor and accountability, observability corrections |

The ordering principle is the one the request stated: **contracts and
structure first.** Nothing that moves data across a thread, a process or a
model boundary is built until the shape of that data is a frozen,
serialisable, conformance-tested type. Every phase ends with `make check`
green, its GLOSSARY rows, and its folder READMEs updated in the same
commits as the code. Behaviour on `main` does not change until Phase C
flips a flag, and a flag flips it back.

---

## Assumptions taken from the open decisions

The instrument-thread plan's §8.2 leaves six decisions open and the UI
groups brief's §6 leaves three. This sequence builds on the recommendation
in each; changing one changes the step it is named in and nothing else.

| Decision | Assumed here |
|---|---|
| Run queue home | `session/run_queue.py`: order and validation, engine callback injected from `main.py` |
| Merge scope | all five commits of the audit branch, audit document indexed |
| Latency inside `measure()` | accepted; documented on `emergency_standby()`; cooperative cancellation is a later plan |
| `inline` mode | kept for one release after `threaded` becomes default |
| Analysis of the run in progress | in-memory `RunBuffer` fed by `Datapoint` events, same vocabulary as `data_reader` |
| `CLAUDE.md` wording | rewritten as the single hardware thread standard when `threaded` is default |
| Group boxes | front panel only; the compact card stays flat and honours `panels:` |
| Group member order | explicit `members` tuple on `UIGroup` |
| Name | `UIGroup` |
| Units for monitored fields | on the decorator (framework §6 item 3) |
| Envelope binds the human | yes, unchanged (framework §6 item 1) |

One decision is deliberately *not* assumed: which `@control` methods per VI
role are `recovery` versus `run-control` (framework §6 item 2). That pass
is scheduled as a working session with the physicist in Phase E and is the
only step in this plan an agent cannot do alone.

---

## The phases at a glance

```
A  Contracts and declarations      2 wk   ─────────┐
B  The seam (no thread yet)        1.5 wk          ├── B, D1 and B5 run in parallel after A
C  The move                        2 wk            │
D  Results, evaluation, safe state 2 wk   ─────────┘   D1 needs only A; D3 needs nothing
E  The agent gateway               1.5 wk   needs A, B; useful live once C is default
F  Surfaces: panel, MCP, assistant 2.5 wk   needs C, E
G  eLab publishing                 1.5 wk   needs A; LLM drafting needs E
```

About thirteen weeks serial, about ten with the parallel tracks taken.
Every estimate is for one person who knows the codebase; the audit branch's
own redline under-estimated the thread move by a week, and that correction
is already in C.

---

## Phase A — Contracts and declarations *(2 weeks)*

*Everything that will ever cross a boundary gets a frozen, JSON-safe type,
and every instrument describes itself completely. No runtime behaviour
changes.*

**A1. Merge the groundwork.** *(hours; thread plan §1.5)*
Merge the audit branch onto `main` with the two-hunk resolution recorded in
the thread plan. Index the audit document. `main` goes from one failing
Linux test to zero. Fold the audit's §8 corrections into the framework
document in the same series.

**A2. The control contract types.** *(3 days; thread plan §3.4, framework
Phase 2 steps 1–2)*
In `core/events.py` beside `ErrorEvent`: `Actor`, `Command` (with
`request_id`, `actor`, `name` from a `CommandName` enum of every public
Orchestrator command, JSON-safe `args`, `issued_at`), `Verdict` (the
framework's `ActionVerdict` plus `actor`; codes `OK`, `BLOCKED_STATE`,
`BLOCKED_CLAIM`, `BLOCKED_FAULT`, `BLOCKED_LIMIT`, `BLOCKED_ENVELOPE`,
`BLOCKED_ROLE`, `FAILED`), and the `Event` union (`StateChange` with
`cause` and `actor`, `StatusSnapshot`, `StationInfo`, `Readings`,
`Datapoint`, `RunStarted`, `RunFinished`, `QueueChanged`), each with `seq`
and `ts`. `to_json()` / `from_json()` on every type. `_make_limit_wrapper`
attaches structured fields (`param`, `value`, `lo`, `hi`, `limit_name`) to
the `CryoSoftSafetyError` it raises so a verdict never parses prose; the
message string stays byte-identical for the banner.
*Exit:* a conformance test round-trips every contract type through JSON;
`CommandName` covers every public Orchestrator method (test enumerates
both and diffs).

**A3. Complete the instrument declarations.** *(4 days; framework Phase 1
steps 1–2, UI groups brief §4.1, §4.2, §4.5)*
`@monitored(unit=, description=, group=)` and `@control(..., group=)` as
optional keywords storing plain strings (`decorators.py` may import no spec
type, contract C1). `UIGroup(key, title, description, members)` frozen in
`core/plan.py`; `ui_groups: ClassVar[tuple[UIGroup, ...]]` on
`BaseVirtualInstrument`; `__init_subclass__` validates unique keys, every
tag names a declared group, every member exists. Then the declaration
pass over every VI: units and descriptions on all monitored fields,
`params=` on the bare controls the framework lists, descriptions on the
sweep-axis specs, `measurement_parameters` installed as the arming
control's specs when it declares none, and groups on the delta-mode and one
temperature-controller VI as the brief's first two examples.
*Exit:* `test_capability_manifest_is_complete` passes with zero exemptions;
the six `initiate_measurement` controls render choices and units; a
dangling or duplicate group tag fails at import with a message naming the
method.

**A4. `StationInfo` and the capability manifest.** *(2 days; thread plan
§3.5, framework Phase 1 step 3, brief §4.4)*
`StationInfo` frozen dataclass built by the Station once from the
declarations of A3 plus the offline registry and merged `control_limits`
bounds; `core/capability_manifest.py` `build_manifest(station)` is its JSON
rendering, groups first in declared order, ungrouped items after.
`control_param_specs()` becomes a pure read of config and cached state,
conformance-enforced, so building the manifest never issues bus traffic.
*Exit:* `build_manifest()` validates as JSON Schema against the sim
station; a test asserts one VI's manifest groups equal its `ui_groups`.

**A5. The engine port and the operator sentinel.** *(2 days; thread plan
§3.4, audit Phase 4 step 1)*
`Orchestrator.submit(Command) -> request_id` dispatching by `CommandName`
to the existing methods; every public method gains an `actor` keyword
defaulting to the operator sentinel; `StatusSnapshot` emitted per tick and
per state change; `StateChange` emitted with `cause` and `actor` from
`_change_state()`. The existing `state_changed(str)` and the three
`action_*` signals keep emitting unchanged so no widget moves yet. Close
the silent refusals the audit's §8 lists (`acknowledge_fault` with no
fault, `acknowledge()` with nothing held, `submit_global_action` unknown).
*Exit:* a sim test submits a `Command` and receives exactly one `Verdict`
with its `request_id`; every refusal path emits.

**A6. Observability that can be trusted.** *(1 day; audit Phase 3 items
1–4)*
`ts`, `seq`, `schema`, `run_id`, `experiment_id`, `setup` added to the
per-tick status record; a record every tick even with monitoring off;
`--max-age` on `troubleshoot status` exiting non-zero on a stale log;
tail-from-end reads. Additive only.

**Phase A exit.** `make check` green. No GUI file changed except the
front-panel widgets that now get `ParamSpec`s from A3. A script with no
`QApplication` can build a sim station, print its manifest, submit a
`Command` and print the `Verdict`.

---

## Phase B — The seam, no thread yet *(1.5 weeks; B5 in parallel)*

*The GUI stops reading the engine synchronously and stops holding anything
the engine owns. Behaviour identical; the whole GUI suite is the proof.*

**B1. Queue as data, engine pulls.** *(2 days; thread plan §1.4, redline
0.2 + 0.3, framework Phase 2 step 4)*
`session/run_queue.py`: `RunQueue` of immutable specs, `QueueEntry.proc`
dropped, the three direct writes to `orchestrator._procedure_queue` gone.
`validate_run(procedure_cls, params)` on the `ExperimentManager` (build
without dispatch, check bounds, limits, envelope, return findings and a
duration estimate) called at add time. The engine gains
`next_procedure()`, injected from `main.py`; `run_queue()` and its three
callers stay; operations still drain first; the five no-chain IDLE
transitions stay no-chain. `QueueChanged` events carry the actor who
queued.
*Exit:* the redline's three killers each have a test: a queued operation
starts ahead of a queued procedure; no run starts inside a `state_changed`
emit; nothing auto-starts after an emergency acknowledge.

**B2. No synchronous reads.** *(3 days; thread plan §3.3)*
The status mirror on the GUI side, fed by `StatusSnapshot`, `StateChange`
and `StationInfo`. Every getter call site in `gui/` reads the mirror; the
three read-then-act pairs on `active_run_kind()` become "ask, and let the
engine refuse"; `instrument_front_panel.py`'s direct `ping()` becomes a
command whose result is a `Verdict`. `InstrumentPanel._build_layout()`
builds from `StationInfo`, one titled box per declared group in declared
order, ungrouped rows after, byte-identical for a VI with no groups
(offscreen screenshot diff). Signal payload rule: every emitted dict is a
copy.
*Exit:* a conformance test finds no call from `gui/` into `Orchestrator`
except through the proxy-to-be's method set, and no `Station.get_vi()` in
`gui/`.

**B3. Contracts and conformance.** *(1 day; thread plan step 0.5)*
New import contract: `cryosoft.gui` may not import `cryosoft.core.station`
except under `TYPE_CHECKING`. Conformance: no `time.sleep` under `gui/`;
no plan-document citation in code (clean the two in
`procedure_window.py` and the nine citing the session-tier plan first).
GLOSSARY rows: **Control contract**, **Actor**, **Run queue**, **UI
group**.

**B4. The proxy in `inline` mode.** *(2 days; thread plan Phase 1 step
1.1)*
`OrchestratorProxy`: one typed method per `CommandName`, one Qt signal per
event type; `InstrumentHost(mode="inline")` constructs everything on the
caller's thread exactly as today and hands the GUI the proxy and
`StationInfo`. Every widget, the `ExperimentManager` and the recorder take
the proxy. The GUI test suite runs unchanged.
*Exit:* the three-way conformance test passes: `CommandName` ⊆ proxy
methods ⊆ engine methods, and the gateway's tool list in Phase E will be
the third leg.

**B5. Close the direct write path.** *(3 days, parallel; audit Phase 0)*
Independent of every other step and a live hazard today: `Station.
execute_vi_action()` refuses underscore names, non-`@control` methods and
out-of-scope capabilities; `control_limits` for excitation current on the
DC, delta and lock-in VIs from a `max_source_current_A` init param added
to every shipped config; the coverage conformance test (every numeric
control parameter bounded or explicitly exempted with a rationale); the
envelope binds the direct action path; a public
`Orchestrator.emergency_standby(reason)` permitted in every state,
documented as landing after the current `measure()` returns; an envelope
editor in the Start Experiment dialog pre-filled from config limits.
*Exit:* the audit's Phase 0 exit test: five distinct refusals on the direct
path, each with a distinct reason; coverage test green with a documented
exemption list.

**Phase B exit.** GUI behaviour unchanged, verified by the unchanged GUI
suite plus screenshots. Every interaction between GUI and engine is a
`Command` or an `Event`.

---

## Phase C — The move *(2 weeks including soak)*

*The one step that changes the runtime. Behind a flag, then default.*

**C1. `threaded` mode.** *(3 days; thread plan Phase 1 step 1.2)*
`InstrumentHost(mode="threaded")`: `QThread`; Station built inside it so
every pyvisa `ResourceManager` and serial port is opened by the thread that
uses it; Orchestrator constructed there; timer started and stopped through
queued slots; bounded join on shutdown with a `CRITICAL` log if a VISA read
is wedged; a thread-level exception boundary that keeps the event loop
alive so `shutdown()` can still reach it. `TrendCheckRunner` moves with the
Station. `next_procedure()` reads the queue's immutable snapshot.

**C2. The thread suite.** *(2 days; thread plan step 1.3)*
`tests/test_instrument_thread.py`: sim station, real `QThread`,
`waitSignal` with explicit timeouts on every command's verdict; a
frozen-GUI detector (a 50 ms GUI-thread timer keeps firing while a sim
`measure()` sleeps 2 s); shutdown with a hung VI; the pause boundary
end-to-end across the thread; the quench-then-queued-procedure scenario;
an agent-actor command whose `StateChange` arrives on the GUI side with
`actor.kind == "agent"`.

**C3. Flag and soak.** *(1 day plus soak; thread plan step 1.4)*
`monitor.yaml` `instrument_thread: true|false`, `CRYOSOFT_INSTRUMENT_THREAD`
as the CI override, default `false`. CI runs the GUI suite in both modes.
Soak on `sim_real_cryostat` at a 1 s tick for a multi-hour run under a
scripted click storm; then a day on hardware with `threaded` on and the
flag one edit away.

**C4. Default on, and the standard.** *(1 day; thread plan Phase 2)*
Flip the default. Rewrite the `CLAUDE.md` paragraph as the single hardware
thread standard. GLOSSARY: **Instrument thread**, **Orchestrator proxy**,
**Status snapshot**, **Station info**. Update `core/README.md`,
`gui/README.md`; retire "the one sanctioned thread" from the framework's
Phase 5 and reword the audit's D4. `inline` stays for one release.

**Phase C exit.** The Monitor and Procedure windows repaint and accept
clicks during a twenty-second sim datapoint; a Pause click is acknowledged
at once and lands when the datapoint completes, exactly as before.

---

## Phase D — Results, evaluation, safe state *(2 weeks; D1 and D3 can start after A)*

*What the agent will read, how it will try before committing, and what
happens when anything leaves mid-sequence.*

**D1. Result access.** *(3 days; framework Phase 2 step 3, thread plan
decision 6)*
`core/data_reader.py` as a standalone sibling of `data_manager.py` with its
own import contract: `open_run`, `list_columns`, `read_slice`,
`summary_stats`, `read_metadata`. `RunBuffer` on the GUI side fed by
`Datapoint` events, exposing the same column and summary vocabulary, so a
consumer sees one API for the run in progress and the runs on disk. The
`Station.execute_vi_action()` return value reaches `Verdict.result`.

**D2. Probe runs and estimates.** *(4 days; framework Phase 2 steps 4–5)*
`run_kind = "probe"` honoured end to end from a `probe_spec`; a real HDF5
file tagged `kind="probe"`; `validate_run()` extended with the duration
estimate from sweep length and per-step waits. Operations gain a headless
construction path (the audit notes they have none).

**D3. Safe state and hardware truth.** *(1 week, parallel; framework
Phase 3)*
The driver error-reporting standard written into `drivers/README.md` and
implemented across the eight unchecked drivers starting with
`Keithley6221._program_delta_mode()`; `safe_shutdown()` on every driver and
its sim twin, conformance-enforced (exists, idempotent, leaves the sim in a
known state); the shared-instrument stale-state conformance test that would
have caught the `-221` incident; `troubleshoot session` mode; the
`hardware` marker on every bench test.

**Phase D exit.** A sim test submits a probe of `FieldSweep` with a request
id, receives its verdict, and reads the resulting file's columns and stats
through `data_reader`; every driver has a tested `safe_shutdown()`.

---

## Phase E — The agent gateway *(1.5 weeks)*

*The second adapter of the contract. In-process, no network, no thread.*

**E1. Roles, attendance, kill switch.** *(3 days; framework Phase 4 steps
1–3, audit Phase 4 step 3)*
`cryosoft/session/gateway/` under contract C11. Roles `observer` / `debug`
/ `session` with the read / recovery / run-control / envelope action-class
matrix; emergency standby permitted to every role in every state.
Attendance pushed down as a value via `Orchestrator.set_attendance()`,
mirroring `set_experiment_envelope()`, because C12 keeps the enforcement
point from reading the session layer. Kill switch tri-state, never able to
block the human path.
*The physicist's session:* one pass over the VI roles classifying each
`@control` as `recovery` or `run-control` (framework §6 item 2). Scheduled
here because E1 cannot ship without it and no agent should guess it.

**E2. The tool surface, rendered.** *(2 days; framework Phase 4 step 5,
thread plan §3.4)*
Tools are rendered from `CommandName` plus the manifest: no hand-written
tool per command. Session tools wrap `data_reader`, `validate_run`, probe
runs, the experiment store, the operational log and the agent feed. The
three-way conformance test gains its third leg: every `CommandName` has a
tool.

**E3. Feed and accountability.** *(2 days; framework Phase 4 step 4, audit
Phase 4 items 1, 2, 6)*
`agent_actions.jsonl` per experiment following the JSONL discipline;
`actor` on `RunRecord`, queued entries and `StepRecord`, so an agent's
self-confirmation of a physical step is distinguishable from the
physicist's; `params_digest` on operator confirmations; `actor` and
`request_id` added to the troubleshoot transcript so the two trails join.

**E4. `python -m cryosoft.ctl`.** *(2 days; framework Phase 4 step 6)*
The reference client: argparse over the in-process gateway, JSON in and
out, following the troubleshoot CLI's conventions. Then the live write path
at the audit's D4 rung 1: a request spool the tick drains at the same point
it drains manual actions, verdicts appended to a JSONL sink, so the CLI
can act on a running app.

**Phase E exit.** The framework's Phase 4 scenario on a sim station: a
client validates, probe-runs, starts and aborts a `FieldSweep`; envelope,
attendance, role and kill-switch refusals each asserted with a structured
reason; every request in the feed with its verdict; and the Monitor window
shows each of those actions with "agent" as the actor.

---

## Phase F — Surfaces: panel, transport, assistant *(2.5 weeks)*

**F1. The agent panel.** *(3 days; framework Phase 6)*
A filter of the event stream where `actor.kind == "agent"`, in the
bottom-right quadrant; the takeover strip in the header (kill-switch
tri-state, attendance toggle, "agents active"); the envelope editor moved
to the experiment header; "Probe first" on queue items. `gui-edit` rules
and offscreen screenshots.

**F2. Out-of-process transport.** *(4 days; framework Phase 5 reshaped,
audit D4 rungs 2–3)*
`QLocalServer` on the GUI thread's event loop feeding the same
`submit()`, then an MCP adapter as a separate process translating MCP
framing to that socket. No thread; the adapter cannot touch the Station
even in principle. `.mcp.json` wiring and a `measure-session` skill.
*Exit:* an external Claude Code session performs the Phase E scenario over
MCP against a sim station while the GUI shows it.

**F3. The embedded assistant.** *(1 week; framework Phase 7)*
A Claude Agent SDK runtime in-process whose tools are the gateway's, same
roles, same envelope, same feed; chat dock; API key in keyring; visible
cost line. API calls asynchronous on the GUI side; the instrument thread
never waits on a model.

---

## Phase G — eLab publishing *(1.5 weeks; independent after A)*

*Owned entirely by `archive/session-management-layer.md` Part B; listed
here only for its place in the order.*

**G1. Adapter and publisher.** *(1 week)* The ELN adapter contract with
elabFTW as the first backend; the offline-first outbox; `ElnLink` wired;
triggered by `RunFinished` events with the run manifest and data path, and
by a manual export action. All network I/O on the GUI side.

**G2. LLM drafting.** *(3 days; needs E)* A gateway tool that renders a
run's manifest, `summary_stats` and station snapshot into a draft entry;
attended sessions show it for approval, unattended sessions publish under
the role's permission. The draft is data through the same contract, not a
privileged path.

---

## What is deliberately not in this plan

- Per-instrument threads or a thread pool. One bus, one writer.
- Cooperative cancellation inside `measure()`. Its own plan, if the
  datapoint-granularity latency proves wrong in practice.
- Mode exclusivity, the `@query` verb, and the rest of
  `deferred/complete-instrument-vis.md`. Unchanged and still deferred.
- Synchronized sweep variables. Orthogonal; `ParamGroup` stays the
  procedure-form concept.
- Leased reservations, operator tokens, DIDs. Rejected by the audit for
  reasons that still hold.

---

## How to read progress

Each step lands as one or a few commits with `make check` green. The
milestones a reader can check without reading code:

| After | You can |
|---|---|
| A | print a sim station's manifest as JSON and get a `Verdict` back for a `Command`, with no `QApplication` |
| B | run the GUI exactly as before, with every interaction visibly a `Command` or `Event` in the log |
| C | click Pause during a twenty-second sim datapoint and watch the window keep painting |
| D | probe-run a sweep from a script and read its stats back |
| E | drive a running sim station from `cryosoft.ctl` and watch the Monitor window name the agent as the actor |
| F | do the same from an external Claude Code session over MCP, or from the chat dock |
| G | find the run in the eLab with a drafted entry |
