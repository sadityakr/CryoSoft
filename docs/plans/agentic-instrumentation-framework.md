# Agentic instrumentation: capability framework and phased roadmap

**Status:** proposal, no code yet. Supersedes the roadmap sections of
`archive/agent-native-architecture.md` (§8) and `archive/agentic-operation-roadmap.md`
("Implementation Roadmap"); both remain valid as design rationale and are
cited throughout.
**Scope:** define what modules an agentic instrumentation system needs,
score CryoSoft against that definition from a full code survey, and
sequence the next round of development.
**Date:** 2026-07-25
**Survey basis:** full read of `core/`, plus five parallel surveys of
drivers/troubleshoot, VIs/procedures, GUI, session/data/config, and
tests/skills. Every claim below carries a `path:line` anchor.

---

## 1. The framework: nine modules an agentic instrumentation system needs

An LLM agent operating a physical instrument is not a user with a faster
mouse. It cannot see a front panel, cannot infer intent from a blinking
LED, and cannot be trusted to remember what it left armed. Everything it
knows must arrive as structured text, and everything it does must be
refusable by the machine rather than by its own restraint.

That reduces to nine modules, in four tiers. The tiers are ordered by
dependency: nothing in **Act** is safe without **Know**, and nothing in
**Connect** is worth building before **Account**.

### Tier 1 — Know: the agent must be able to read the system

**M1. Capability manifest.** A machine-readable description of everything
the system can do: every readable quantity with its unit and meaning,
every action with its parameters, types, bounds, and a non-empty prose
description of what it physically does. This must be *generated* from the
same declarations that drive the rest of the system, never hand-maintained,
or it will drift the first week. The test of this module is blunt: can a
program render the whole instrument surface to JSON Schema without a human
writing a word of glue?

**M2. State observation.** A structured, timestamped snapshot of what the
system is doing right now: state machine position, per-instrument value
versus target, progress, ETA, and a fault code vocabulary that is an enum
rather than prose. Log text is not observation. An agent parsing prose is
an agent guessing.

**M3. Result access.** The agent must be able to read the data it just
produced: columns, slices, summary statistics, metadata. An agent that can
start a measurement but not read the result cannot judge whether the
measurement was any good, which is the entire point of having it there.

### Tier 2 — Act: the agent must be able to do things, refusably

**M4. Action with verdicts.** Every request gets a correlated, structured
response: did it happen, and if not, why not, in a machine-readable form.
Fire-and-forget plus a later broadcast signal is a GUI pattern; it does not
survive contact with a request/response tool call, and it does not survive
two agents acting at once.

**M5. Authority model.** Roles and action classes, per-experiment limits
narrower than the instrument's own, an attendance flag distinguishing
"human is watching" from "3 a.m., keep it alive", and a kill switch. The
critical design property: enforcement lives at the single-writer choke
point, not in the agent's prompt. An agent that is *asked* not to exceed
2 T is not limited to 2 T.

**M6. Safe state and reversibility.** A guaranteed idempotent "leave it
safe" per instrument, snapshot/restore around exploratory sequences, and
truthful error reporting from the hardware itself. Silent rejection is the
worst failure mode in this class of software: the agent believes it set a
current, the instrument disagrees, and everything downstream is fiction.

**M7. Cheap evaluation before commitment.** Three escalating rungs before
an agent burns six hours of cryogen: free validation with no hardware,
a duration/consumable estimate, and a real miniature execution (a probe
run) returning actual data. Judgement stays with the caller; the framework
supplies evidence.

### Tier 3 — Account: what the agent did must be reconstructible

**M8. Audit trail.** Append-only, attributed record of every agent request
and its verdict, correlated to runs. This is how a human audits an
unattended night, and how a debugging agent reconstructs what another
agent was doing. It is also the only defence against the failure mode where
nobody can say whether the anomaly in the data was physics or an agent.

### Tier 4 — Connect: the agent must be able to reach the system

**M9. Client surface and runtime.** A transport and a client model:
in-process for an embedded assistant, local network for external Claude
Code sessions, and a command-line client as the lowest common denominator.
This module is last deliberately. It is the most visible and the least
load-bearing: a gateway over an incomplete Tier 1 is a fast way to give an
agent confident access to bad information.

---

## 2. Scorecard: where CryoSoft actually stands

CryoSoft is further along than either prior plan document assumes, and the
remaining gaps are not where those documents put them.

| Module | State | Evidence |
|---|---|---|
| M1 Capability manifest | **Partial, and the gap is structural** | Generation machinery exists and is excellent; the metadata it reads is incomplete. See §2.1. |
| M2 State observation | **Built** | `status.jsonl`, one record per tick. See §2.2. |
| M3 Result access | **Missing** | No HDF5 read-back API anywhere in `cryosoft/`. See §2.3. |
| M4 Action with verdicts | **Missing the correlation half** | Verdicts exist as broadcast signals with no request ID. See §2.4. |
| M5 Authority model | **Half built, and the built half is the hard half** | Envelope + claims + admission predicate all live. See §2.5. |
| M6 Safe state | **Largely missing** | One driver of nine has error checking; none has `safe_shutdown()`. See §2.6. |
| M7 Cheap evaluation | **Missing** | No validate, no estimate, no probe run. See §2.7. |
| M8 Audit trail | **Missing for agents, precedent exists** | Three JSONL append-only logs already ship. See §2.8. |
| M9 Client surface | **Nothing** | No gateway, no MCP, no HTTP anywhere in the repo. See §2.9. |

### 2.1 M1: the generation machinery is done, the declarations are not

The hard part is finished. `InstrumentPanel._build_layout()`
(`gui/instrument_panel.py:165`) builds an entire instrument UI from
`get_monitored_methods()` / `get_control_methods()` with zero
per-instrument code; `param_form.build_param_widget()`
(`gui/param_form.py:166-210`) is the single ParamSpec-to-widget mapping;
`procedure_discovery.py:65-124` reflectively discovers every procedure and
operation and is deliberately Qt-free. `ParamSpec` (`core/plan.py:294-463`)
already carries type, default, unit, description, min/max, and choices, and
validates eagerly. This is exactly the substrate a tool-schema generator
needs, and it is already proven by the fact that the GUI is built on it.

What is missing is description coverage, and it is worse than "some
docstrings are thin":

- **`@monitored` captures no description field at all**
  (`core/decorators.py:58-74` attaches only `_is_monitored` and
  `_display_name`). Roughly 30 monitored fields across the VIs would render
  to schema as a bare name with no unit and no meaning. This is structural
  absence, not an empty string.
- **Ten `@control` methods take arguments with no `ParamSpec`**, so their
  parameters reach a schema as `{name, type, default}` with no unit,
  description, or bounds: `SuperconductingMagnetVI.set_field`,
  `SampleTemperatureControllerVI.set_temperature` / `set_ramp_rate`,
  `VTITemperatureControllerVI.set_needle_valve`,
  `CryogenLevelMeterVI.set_refresh_rate`, `RotatorVI.set_sample_angle` /
  `set_rate_sample_angle`, and the three measurement-VI current setters.
- **Six sweep-axis ParamSpecs ship with `description=""`**
  (`core/sweep_builder.py:245,264,265`, instantiated once per axis for
  `FieldSweep` and `TemperatureSweep`). These are invisible today because
  the existing conformance test `test_procedure_parameter_has_description`
  (`tests/test_conformance.py:333`) only iterates the three explicit
  parameter dicts, and axis params are merged in separately at
  `core/procedure.py:179-185`.
- **Bounds live in a second, disjoint channel.** `control_limits`
  (`virtual_instruments/base.py:105-107`) maps method/param to a limit name
  resolved from config at construction. A schema generator must
  cross-reference it by name; there is no single call returning "the
  complete spec for this control".
- **Some specs are instance-level only.** `SwitchMatrixVI` and
  `Lakeshore335SampleTemperatureControllerVI` override
  `control_param_specs()` to inject runtime choices (the config's route
  table). Static analysis cannot see these; the manifest must be built from
  a live Station.

Consequence: the manifest is roughly two days of declaration work away, not
a new subsystem. But it cannot be skipped, and the reason it has gone
unnoticed is that a blank GUI tooltip is a cosmetic defect while a blank
schema description is a functional one.

### 2.2 M2: built, and genuinely good

`Orchestrator._update_operational_status()`
(`core/orchestrator.py:1257-1297`) builds a record every tick and writes it
as one JSON line to `logs/status.jsonl` via a dedicated non-propagating
logger (`core/logging_config.py:38-47`, 10 MB rotating). The schema
(`core/operational_status.py:205-218`) carries `orch_state`,
`elapsed_in_state_s`, `wait`, `progress`, `verdict`, `alerts`,
`active_gates`, and a per-VI list of `{vi_name, value, target, gap,
closing, rate, eta_s, ramp_status, phase, code, detail}`. `code` is an enum
(`RunFaultCode`: OK, VI_STALE, VI_DISCONNECTED, QUENCH, RAMP_STALLED,
STALLED_RUN). The watchdog (`core/watchdog.py:74-135`) is deterministic
arithmetic, not heuristics.

This module needs nothing. It is the proof that the standards-driven
approach produces agent-ready surfaces as a side effect.

### 2.3 M3: nothing exists

`DataManager` exposes `save_datapoint()`, `close()`, `.filepath`,
`.last_datapoint` and nothing else (`core/data_manager.py:250-379`). Every
`h5py.File(..., "r")` in the repository is in a test file. Contract C7
(`pyproject.toml:161-174`) declares `data_manager` standalone, so the
reader must be a new sibling module with its own contract row rather than
a method on `DataManager`.

The HDF5 layout is well specified and easy to read back: `/metadata/`
attrs (all JSON-encoded), `/data/` with sweep columns `(N,)`, measurement
scalars `(N, n_loop1, n_loop2)`, arrays `(N, n_loop1, n_loop2, M)`, and
`/snapshots/` holding a JSON station snapshot per sweep index
(`core/data_manager.py:184-244, 341-345`).

### 2.4 M4: verdicts exist, correlation does not

`Orchestrator.submit_vi_action()` (`core/orchestrator.py:952`) checks
admission, appends a dict to `_gui_action_queue`, and returns `None`. The
tick drains the queue (`core/orchestrator.py:1554-1576`), calls
`Station.execute_vi_action()`, and emits `action_succeeded(vi_name,
method_name)` or `action_failed(vi_name, method_name, reason)`.

Three concrete defects for an agent client, all independently confirmed by
the GUI survey:

1. **No request ID.** Two queued actions on the same VI and method are
   indistinguishable in the verdict. A tool call cannot await its own
   result.
2. **The return value is thrown away.** `Station.execute_vi_action()`
   returns the method's result (`core/station.py:964-980`); the
   Orchestrator discards it. This was already noted in
   `docs/plans/deferred/complete-instrument-vis.md`.
3. **`reason` is `str(exception)`.** The control-validation standard
   produces a well-formed prose sentence naming VI, method, param, value,
   range, and limit name (`virtual_instruments/base.py:220-233`), but an
   agent wanting to retry inside the limit must regex it.

Structured error payloads do exist elsewhere and are the right precedent:
`ErrorEvent` (`core/events.py:29-56`, with `kind` and `severity` enums) and
`FaultRecord` (`core/station.py:109-139`).

### 2.5 M5: the difficult half is already enforced

This is the finding that most changes the sequencing. `SessionEnvelope` and
`EnvelopeBound` are implemented in `core/plan.py:739-940` and enforced by
the Orchestrator in both required places: every submitted `Target` before
dispatch (`core/orchestrator.py:639-641`) and every tick against live
readings, with a violation entering EMERGENCY exactly like a tripped safety
flag (`core/orchestrator.py:1524-1530`). `set_session_envelope()` is public
(`core/orchestrator.py:434`). `ExperimentRecord` carries both `envelope`
and an `attended` boolean (`session/models.py:328-462`), with
`SessionManager.set_attended()` (`session/manager.py:201-368`).

Alongside it, the claims system gives per-VI concurrency scoping through a
single admission predicate `_manual_action_admissible()`
(`core/orchestrator.py:880-950`) shared by submission and the tick drain,
which already refuses faulted VIs before any other rule.

So the agent-native plan's F0 is **done** and F1 is **substantially done**.
What is missing from M5 is the cheap half: roles, action classes, and the
kill switch, none of which require touching the tick loop. The `attended`
flag is stored but nothing reads it to gate behaviour.

### 2.6 M6: the weakest module, and the one with field evidence

- **Error-queue checking exists on one driver of nine.** `Keithley6221`
  polls `:SYST:ERR?` on exactly two of its 42 `self._write()` call sites
  (`drivers/keithley_6221.py:145-149, 157-158`; the checker at `:612-635`
  logs a WARNING and never raises). The 20-plus write sequence in
  `_program_delta_mode()` (`:348-412`), the most fragile code in the file,
  has zero checks. No other real driver has any equivalent.
- **Two drivers are better than the rest by protocol.**
  `OxfordMercuryiPS._write()` (`drivers/oxford_mercury_ips.py:284-305`)
  requires a `STAT:` acknowledgement per command and raises on `DENIED`;
  `TensormeterRTM2._send_and_confirm()` (`drivers/tensormeter_rtm2.py:210-266`)
  requires an echo. These are the pattern worth generalising.
- **One driver cannot be fixed.** `Keithley705` has no bus error reporting
  at all (`drivers/keithley_705.py:104-109`); readback is the only
  verification available. The standard must accommodate this rather than
  pretend otherwise.
- **No driver defines `safe_shutdown()`, `reset()`, or any named safe-idle
  method.** What exists is ad-hoc self-recovery inside ordinary setters:
  `Keithley6221.set_current()` unconditionally issues `:SOUR:SWE:ABOR` and
  `:SOUR:CURR:RANG:AUTO ON` first (`drivers/keithley_6221.py:111-149`),
  added after the 2026-07-22 commissioning incident where leftover
  delta-mode state silently rejected DC writes. That fix is correct and
  undiscoverable: a new VI author has no way to know the requirement exists.
- **There is no `DriverBase`.** The driver contract is duck-typed and
  enforced entirely by conformance tests. Any shared safe-shutdown standard
  must follow that model (a documented contract plus a test), not
  inheritance.

### 2.7 M7: absent, including the plumbing that would make it cheap

No `validate_run`, no duration estimate, no probe run. `RunRecord.kind`
exists and defaults to `"run"` (`session/models.py:274`), and the
Orchestrator populates it from `procedure.run_kind`
(`core/orchestrator.py:1156`), but **nothing in the repository ever emits
`"probe"`**: the docstring at `session/models.py:260` describing probe runs
is aspiration, not behaviour.

A complicating fact for probe runs: operations are constructed as
`(Station, **config)` while procedures take
`(Station, sample_info, data_directory, **param_values)`. Any generic
"derive a miniature run" helper needs two construction paths.

### 2.8 M8: no agent feed, but the pattern is established three times over

`status.jsonl` (per tick), `troubleshoot.jsonl` (one line per CLI
invocation, unconditional, `troubleshoot/cli.py:555-569`), and
`servicing.jsonl` (per servicing entry) all demonstrate the append-only
JSONL discipline. An `agent_actions.jsonl` is a copy of a solved problem.

### 2.9 M9: confirmed absent

No `cryosoft/session/gateway/`, no MCP server, no HTTP server, no
`.mcp.json`. `SessionManager`'s docstring forward-references "the Agent
Gateway" (`session/manager.py:71-73`) and the plan document says "proposal,
no code yet". The only live agent surface today is read-only log tailing
via `troubleshoot status`; every acting skill (setup-supervisor,
setup-commission, write-measurement-vi) requires the **app closed** because
serial instruments are exclusive-open.

---

## 3. What this changes about the sequencing

Both prior documents are right about their own subject and wrong about
priority, for the same reason: each was written from inside one problem.

`archive/agent-native-architecture.md` sequences the MCP gateway first (A1
read-only gateway, then A2 write path). But a gateway over today's
metadata would serve an agent a station description with roughly 30
unitless, undescribed monitored fields and eleven undescribed control
parameters. The transport is not the bottleneck; the vocabulary is.

`archive/agentic-operation-roadmap.md` sequences driver observability first, which
is correct engineering and correctly argued from the `-221` incident, but
it is instrument hygiene rather than agency. Done alone, it makes hardware
debugging faster for humans and changes nothing about whether an agent can
operate the system.

Three opinionated calls follow, each of which inverts something in the
prior plans.

**Call 1: self-description before transport.** The capability manifest is
the only module every other one depends on, it needs no new subsystem, and
its absence is currently invisible because a blank tooltip looks like a
cosmetic bug. Do it first, enforce it with a conformance test, and the GUI
gets better tooltips as a free side effect.

**Call 2: build the gateway in-process with a CLI client before adding MCP
transport.** The old plan makes the CLI client "A5 (optional)". That is
backwards. The transport thread is the single riskiest change in this
entire programme: it would be the only sanctioned thread in a codebase
whose central design answer to GPIB races is that there is exactly one. The
permission model (roles, action classes, kill switch, feed) is orthogonal
to it and can be tested exhaustively in-process against the existing sim
station fixtures, which already drive full multi-tick runs end to end
(`tests/test_helium_fill.py:99`). Splitting them means the permission model
lands with full test coverage and no concurrency risk, and the thread lands
alone with a narrow blast radius and a reference client that already works.

**Call 3: safe state before the write path, not after.** The old roadmap
puts `InstrumentContext` and rollback in "Week 4+, medium-term". But the
moment an agent can call `submit_vi_action` unattended, the absence of a
guaranteed safe-idle sequence stops being a debugging inconvenience and
becomes the thing standing between a crashed agent and an instrument left
armed overnight. M6 gates M5's write path.

---

## 4. The phased plan

Seven phases. Phases 1 to 3 are independent of each other and can land in
any order or in parallel; phase 4 requires 1 to 3; phases 5 to 7 are
strictly sequential after 4. Every phase ends with `make check` green
(ruff, 12 import contracts, full suite, currently 1504 tests) and lands its
GLOSSARY rows with its code.

### Phase 1 — Capability manifest (M1)

*Makes the system describable. No new runtime behaviour.*

1. **Extend `@monitored`** to accept optional `unit=` and `description=`
   (`core/decorators.py:58-74`), defaulting to `None` so every existing
   call site keeps working. Contract C1 forbids `decorators.py` from
   importing `ParamSpec`, so these stay plain strings, matching how
   `_control_specs` is already stored opaquely.
2. **Fill in the declarations**: units and descriptions on all monitored
   fields; `params=` ParamSpecs on the ten bare argument-taking controls;
   descriptions on the six sweep-axis specs
   (`core/sweep_builder.py:245,264,265`).
3. **New module `core/capability_manifest.py`**: `build_manifest(station)`
   returning a JSON-safe dict describing every VI (monitored fields with
   units, controls with merged `ParamSpec` + `control_limits` bounds +
   scope + `panel` flag) and every discovered procedure and operation
   (`ParamGroup`s rendered as JSON Schema). Built from a **live Station**
   so instance-level `control_param_specs()` overrides
   (`SwitchMatrixVI`, Lakeshore 335) are captured. Its home in `core/`
   keeps it usable by GUI, session, and a future gateway alike; it imports
   only `plan`, `decorators`, and `virtual_instruments.base`, so it sits
   inside the existing C4/C8 allowances without a new contract.
4. **Conformance test** `test_capability_manifest_is_complete`: every
   monitored field and every control parameter of every discovered VI, and
   every procedure/operation parameter including sweep-axis params, renders
   with a non-empty description and (for numerics) a unit. This is the
   standard that makes every future VI agent-operable the moment its file
   exists.

**Exit:** `build_manifest()` output validates as JSON Schema; the new
conformance test passes with zero exemptions. Expect it to fail loudly on
first run against the gaps in §2.1, which is the point.

**Cost:** ~2 days, almost all of it declaration writing, no design risk.

### Phase 2 — Verdicts, results, and evaluation (M3, M4, M7)

*Makes actions answerable and results readable.*

1. **Correlated verdicts.** Add an optional `request_id` to
   `submit_vi_action()` / `submit_global_action()`; carry it on the queued
   dict; emit it in the verdict. Introduce a frozen `ActionVerdict`
   (`core/events.py`, next to `ErrorEvent`): `request_id`, `vi_name`,
   `method`, `ok`, `code` (enum: `OK`, `BLOCKED_CLAIM`, `BLOCKED_FAULT`,
   `BLOCKED_STATE`, `BLOCKED_LIMIT`, `BLOCKED_ENVELOPE`, `FAILED`),
   `reason`, `result`, `timestamp`. Propagate the discarded return value
   from `Station.execute_vi_action()` into `result`. Keep the existing
   `action_succeeded` / `action_failed` / `action_blocked` signals emitting
   unchanged so no GUI code has to move in this phase.
2. **Structured limit rejections.** Have `_make_limit_wrapper`
   (`virtual_instruments/base.py:195-236`) attach structured fields
   (`param`, `value`, `lo`, `hi`, `limit_name`) to the raised
   `CryoSoftSafetyError` so the verdict's `code`/`reason` can be built
   without parsing prose. The message string stays byte-identical for the
   GUI banner.
3. **New module `core/data_reader.py`** with its own import-linter
   contract (C13) mirroring C7's standalone rule: `open_run(path)`,
   `list_columns()`, `read_slice()`, `summary_stats()` (per-column mean,
   sigma, min, max, NaN count), `read_metadata()`. Deliberately a sibling
   of `data_manager.py`, never a method on it.
4. **`validate_run(procedure_cls, params)`** on the SessionManager: build
   the procedure without dispatching anything, check every parameter
   against `ParamSpec` bounds, config `control_limits`, and the active
   `SessionEnvelope`; return structured findings plus a duration estimate
   from the sweep length and per-step waits. Free, no hardware, and the
   first rung of M7.
5. **Probe runs.** `run_kind = "probe"` honoured end to end: a
   `probe_spec` (point count, sub-range policy, reduced averaging) derives
   a miniature procedure through the normal Orchestrator path, producing a
   real HDF5 file tagged `kind="probe"` in its `RunRecord` and
   `/metadata/experiment_info`. Return path, datapoints, and
   `summary_stats()` to the caller; judgement stays with the caller. Note
   the two construction paths (procedures versus operations) from §2.7.

**Exit:** sim test where a caller submits an action with a request ID and
receives exactly one matching structured verdict; a probe run of
`FieldSweep` completes, writes a `kind="probe"` record, and its data reads
back through `data_reader` with correct column names and stats.

**Cost:** ~1 week. The `data_reader` and `validate_run` pieces are
mechanical; probe runs carry the real design work.

### Phase 3 — Safe state and hardware truth (M6)

*Makes it safe for anything, human or agent, to leave mid-sequence.*

1. **Error-reporting standard.** Written into `drivers/README.md`: every
   state-changing driver method must verify its write, by SCPI error queue,
   protocol acknowledgement, or explicit readback, and must document which.
   Implement it across the eight unchecked drivers, starting with
   `Keithley6221._program_delta_mode()`. `Keithley705` documents readback
   as its verification, per `drivers/keithley_705.py:104-109`.
2. **`safe_shutdown()` standard.** An idempotent, unconditional safe-idle
   sequence on every driver, documented with what leftover state it
   recovers from. Enforced by a conformance test asserting the method
   exists on every discovered driver and its sim twin, is callable twice,
   and leaves the sim in a known state. Follows the duck-typed contract
   model already used for `get_idn()`
   (`tests/test_conformance.py:194-220`); no `DriverBase` is introduced.
3. **Shared-instrument conformance test.** Precisely the test proposed in
   `archive/agentic-operation-roadmap.md` §2b: pollute a sim instrument with stale
   delta-mode state (autorange off, fixed range, sweep armed), then assert
   every VI sharing it still initiates and returns valid readings. This
   test would have caught the `-221` bug before hardware.
4. **`troubleshoot session` mode.** A sequence of commands executed in one
   persistent VISA session, JSON in and JSON out
   (`archive/agentic-operation-roadmap.md` §1b). Every CLI subcommand today opens
   and closes a fresh session (`troubleshoot/cli.py:160-164`), which is
   exactly why the `-221` session-state bug was invisible to diagnosis.
5. **Mark hardware tests.** The `hardware` marker is declared
   (`pyproject.toml:33-35`) and used by zero tests. Any bench test added
   here gets it, so `make check` stays hardware-free by construction rather
   than by accident.

**Exit:** every driver has a tested `safe_shutdown()`; the stale-state
conformance test passes for both 6221 VIs; a multi-command troubleshoot
session round-trips against a sim.

**Cost:** ~1 week, low design risk, high independent value. This phase pays
for itself in human debugging time even if the rest is never built.

### Phase 4 — In-process Agent Gateway (M5, M8)

*The permission model and the audit trail, with no network and no thread.*

New package `cryosoft/session/gateway/`, bound by contract C11 (no imports
of gui, main, drivers, VIs, or procedures) and covered by a gateway
conformance test asserting it imports no hardware symbols.

1. **Roles and action classes**: `observer` / `debug` / `session`, with the
   read / recovery / run-control / envelope action-class matrix from
   `archive/agent-native-architecture.md` §3.2. Enforced server-side in the
   gateway. Emergency standby is permitted to every role in every state:
   an agent must never be unable to make the system safe.
2. **Attendance gating**: the stored `attended` flag
   (`session/models.py`, `session/manager.py`) finally becomes
   load-bearing. Recovery-class actions are permitted to `debug` only while
   unattended; while attended, they are refused with a reason and the agent
   reports instead.
3. **Kill switch**: tri-state `active` / `read-only` / `revoked` gating the
   whole gateway, never able to block the human path.
4. **Action feed**: `AgentAction` records appended to
   `<experiments>/<id>/agent_actions.jsonl`, following the established
   pattern of `status.jsonl` and `troubleshoot.jsonl`, and re-emitted as a
   Qt signal for a future GUI panel.
5. **Tool surface**, thin wrappers over existing public API: `get_status`,
   `get_live_state`, `describe_station` (Phase 1's manifest),
   `describe_procedure`, `list_experiments` / `get_experiment` / `get_run`,
   `read_run_data` (Phase 2's reader), `validate_run`, `probe_run`,
   run control, `submit_vi_action` (Phase 2's verdicts),
   `read_operational_log`, `read_agent_feed`.
6. **`python -m cryosoft.ctl`**: an argparse CLI over the in-process
   gateway, JSON in and JSON out, matching the troubleshoot CLI's
   conventions. This is the reference client and the integration-test
   harness, promoted from the old plan's "A5 optional" to a first-class
   deliverable of this phase.

**Exit:** an end-to-end sim test in which a client validates, probe-runs,
then starts and aborts a `FieldSweep`; envelope, attendance, role, and
kill-switch refusals each asserted with their structured reason; every
request appearing in the feed with its verdict.

**Cost:** ~1.5 weeks. Mostly composition of Phases 1 to 3, which is the
argument for this ordering.

### Phase 5 — MCP transport

*The one sanctioned thread, landing alone.*

Streamable HTTP MCP on `127.0.0.1` with a bearer token minted at startup
into a runtime file. A transport thread owns only the socket and MCP
framing and never touches Station, Orchestrator, or any VI; every decoded
call crosses to the main thread by Qt queued connection, executes on the
tick thread through the normal single-writer path, and the result crosses
back. Written into the gateway README as a standard with a conformance test
asserting no hardware imports in transport modules.

Because Phase 4 already validated the whole tool surface in-process, this
phase changes no semantics: it is a second front door onto a tested API.
Ships with the `.mcp.json` wiring and a `measure-session` repo skill
documenting the tool surface, roles, and probe-first discipline.

**Exit:** an external Claude Code session performs the full Phase 4 test
scenario over MCP against a sim station.

**Cost:** ~1 week, but the highest design risk in the programme. Decide
streamable HTTP versus WebSocket with a spike against a real client before
committing.

### Phase 6 — GUI surfaces

Agent panel with the live attributed action feed and connected-agent list;
takeover strip in the header (kill-switch tri-state, attendance toggle,
a visible "agents active" indicator); envelope editor in the experiment
header; "Probe first" button on the queue item, since humans want probe
runs too. `gui-edit` skill rules apply, including the mandatory offscreen
screenshot verification.

The bottom-right quadrant already renders conditionally on config presence
(`gui/monitor_window.py:830-870`), which is the natural home for the agent
panel. Note the stale `gui/README.md` Files-table row for
`monitor_window.py`, which still describes a retired `OtherDevicesPanel`
behind a `QComboBox`; reconcile it in this phase.

**Cost:** ~1 week.

### Phase 7 — Embedded assistant

`cryosoft/session/assistant/`: a Claude Agent SDK runtime in-process whose
tools are direct calls into the same gateway, with the same roles,
envelope, and feed and no privileged path. Chat dock in the GUI, debug
subagent spawning, API key in keyring alongside the eLab key, and a visible
cost/usage line. Because the tool surface and permission model already
exist, this is SDK plumbing plus chat UI.

The "experiment partner" (analysis, proposing next experiments, drafting
findings) is a more capable prompt and skill set on this same runtime. It
is explicitly out of scope, and the structure above is what keeps it from
requiring a rewrite.

**Cost:** ~1.5 weeks.

---

## 5. Deferred, with reasons

- **Instrument transaction context manager with snapshot/rollback**
  (`archive/agentic-operation-roadmap.md` §3a). Phase 3's `safe_shutdown()` plus
  the existing claims system covers the realistic failure modes at
  materially lower cost. Revisit if bench experience shows agents needing
  mid-sequence rollback that safe-idle does not provide.
- **Driver proxy with structured diagnostic events**
  (`archive/agentic-operation-roadmap.md` §3b). Largely subsumed by Phase 2's
  `ActionVerdict` plus the existing per-tick `status.jsonl`. Revisit only
  if the remaining blind spot is specifically driver-level wire traffic.
- **Probe-verdict heuristics.** v1 returns evidence; encoding "signal
  present, noise acceptable" is science, not framework.
- **Remote (non-localhost) gateway access, remote notifications, headless
  station mode.**
- **eLab publishing track.** Designed in
  `archive/session-management-layer.md` (the adapter standard, renderers,
  publisher/outbox) and sequenced as Track B in
  `archive/agent-native-architecture.md` §8. Genuinely unbuilt: `ElnLink`
  (`session/models.py:210-245`) is unwired scaffolding and there is no
  `cryosoft/session/eln/`. Genuinely independent of everything above, so
  sequence it against this plan by preference, not dependency. It is the
  one live thread this document deliberately does not cover, and
  `archive/session-management-layer.md` is its only design record.

## 6. Decisions needed before Phase 1

1. **Does the envelope bind the human too?** It currently does, by
   construction, since enforcement is in the Orchestrator. This is the
   right default (sample protection over operator convenience) but it
   changes GUI error UX, so confirm it explicitly rather than by omission.
2. **Recovery versus run-control classification.** Which `@control` methods
   per VI role are `recovery` (a debug agent may call them unattended)
   versus `run-control`? Needs one pass over the VI roles before Phase 4,
   with the physicist, not by an agent guessing.
3. **Monitored-field units: decorator or config?** Phase 1 proposes the
   decorator, since a monitored field's unit is a property of the
   measurement, not the setup. Confirm, because it is hard to reverse once
   30 call sites carry it.
4. **Embedded-agent cost controls** (per-session token budget, model
   choice). Decide at Phase 7, not before.
