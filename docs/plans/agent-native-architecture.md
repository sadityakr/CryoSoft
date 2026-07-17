# Plan: agent-native CryoSoft — human monitoring via UI

**Status:** proposal — no code yet. Companion to
`session-management-layer.md`; the roadmap in §8 here supersedes that
plan's §7 sequencing.
**Scope:** make the software operable by agents through the Session
Manager — a session-level agent that runs experiments and lower-level
agents that debug live measurements — while the human monitors and
overrides from the GUI. Build the *substrate* now; the autonomous
"experiment partner" is explicitly a later feature this structure must
not preclude.
**Date:** 2026-07-17

---

## 1. The vision, as decided (interview 2026-07-17)

| Question | Decision |
|---|---|
| How do agents reach a live system? | **The app hosts an agent API** (MCP server owned by the Session Manager). All agent requests funnel through the same Orchestrator single-writer path as the GUI; humans and agents coexist on a running system. |
| Agent autonomy over hardware | **Free within safety limits, plus session-specific limits**: an experiment can carry a narrower envelope than the instrument config (e.g. this sample must never see >2 T or <5 K even though the magnet allows 9 T). |
| Debug agents' powers | **Attendance-dependent**: when the session is flagged *unattended*, debug agents may take gentle recovery actions (pause, re-initiate a VI, adjust waits, resume) to keep a run alive; when *attended*, they diagnose and report only — the human decides. |
| Pre-flight before long runs | **Short probe measurement**: actually execute a miniature version of the run (a few points at safe values) and return the data + summary stats so the agent/human can judge signal, noise, and sanity before committing hours. |
| Human monitoring UI | **Agent action feed** (attributed log of every agent request and verdict), **chat with the session agent** inside the GUI, and **takeover & kill switches** (pause/revoke agent access, manual control, attendance flag). Remote notifications: later. |
| Agent runtime | **Both, in layers**: a lightweight embedded session agent (Claude Agent SDK) for chat and routine operation, and external Claude Code sessions connecting over the same MCP API for heavier debugging/development. Two client types, one API. |
| Ambition now | **Structure only**: the framework must natively allow a full experiment partner (analysis, proposing next experiments, drafting findings) later, without implementing it now. |
| Station-level CLI | **Not needed** — the MCP API is the scriptable surface; the existing troubleshoot CLI stays the only terminal entry point (driver-level, app closed). |
| Sequencing vs the eLab plan | **Interleave**: experiment records/store first (shared foundation), then the agent API and the eLab adapter as parallel tracks. |

## 2. Design principle: agents are clients, not a new layer

The six-layer architecture and its single-writer rule already answer the
hard question. Agents do **not** get a new path to hardware; they become
*one more client of the Session Manager (L6)*, exactly as the GUI is one
more client of the Orchestrator. Everything an agent does goes:

```
external Claude Code ──MCP──┐
embedded session agent ─────┤──► Agent Gateway ──► SessionManager ──► Orchestrator ──► Station ──► VIs
GUI (human) ────────────────┴──────────────────────────┘                    (single writer, unchanged)
```

Consequences:

- **No bypass exists to misuse.** The Gateway can only call the same
  public SessionManager/Orchestrator API the GUI uses; the tick loop,
  safety checks, and control-validation standard apply identically to a
  human click and an agent tool call.
- **One permission choke point.** Envelope checks, role checks, the kill
  switch, and the action feed all live in the Gateway/SessionManager —
  not scattered across layers.
- **The decorator philosophy extends to agents.** The GUI is
  auto-generated from `@monitored`/`@control` and `ParamSpec`s; the agent
  tool surface is auto-generated from the same declarations (§4.2). A new
  VI or procedure becomes agent-operable the moment the file exists, with
  zero gateway code — the same standards-over-one-off-code move.

New code lives inside the already-planned L6 package:

```
cryosoft/session/
    gateway/        # MCP server, tool registry, roles, envelope, feed
    assistant/      # embedded session-agent runtime (Agent SDK), later phase
```

Contracts C11/C12 from the session-management plan cover it unchanged
(session never imports GUI/drivers/VIs; nothing below GUI imports
session).

## 3. The Agent Gateway (`cryosoft/session/gateway/`)

### 3.1 Transport and the threading rule

The Qt process must serve MCP over a local transport (streamable HTTP on
`127.0.0.1`, bearer token) without ever blocking the tick loop. Design:

- A **transport thread** owns only the listening socket and HTTP/MCP
  framing. It never touches the Station, Orchestrator, or any VI.
- Every decoded tool call is handed to the **main thread** via a Qt
  queued connection; execution happens on the GUI/tick thread through the
  normal single-writer path; the result is handed back to the transport
  thread for the response.

This is the one sanctioned thread in the codebase, and it is outside the
tick/hardware path by construction — the rule "no threads, no blocking in
the tick path" is preserved because hardware work still happens solely on
the main thread. This must be written into the gateway README as a
standard, with a conformance-style test asserting gateway modules import
no VI/driver/station symbols.

Auth: the app mints a session token at startup, written to a
runtime file next to the operational-status log (readable by local
processes only); external Claude Code reads it via the MCP server config.
Remote access is out of scope (localhost only).

### 3.2 Roles and action classes

Every connection declares a **role**; every tool has an **action class**.
The matrix is the permission model:

| Action class | `observer` | `debug` | `session` | human (GUI) |
|---|---|---|---|---|
| **read** (state, records, files, feed) | ✔ | ✔ | ✔ | ✔ |
| **recovery** (pause/resume, VI initiate/standby, re-send config, adjust waits) | ✖ | ✔ *unattended* / ✖ *attended* | ✔ | ✔ |
| **run-control** (validate, probe run, queue, start, abort) | ✖ | ✖ | ✔ | ✔ |
| **envelope** (set session limits, attendance, arm/revoke agents) | ✖ | ✖ | ✖ | ✔ only |

- The **attendance flag** (attended/unattended) is session state, set
  from the GUI takeover panel, readable by every agent, and enforced
  server-side — a debug agent's recovery call while attended is rejected
  with a reason, not trusted to self-restraint.
- The **kill switch** gates the whole Gateway: *active* / *read-only* /
  *revoked*. Flipping it never blocks the human path.
- Emergency actions (global standby) are allowed to every role always —
  an agent must never be unable to make the system safe.

### 3.3 The session envelope (sample-specific limits)

A new record on the experiment: `SessionEnvelope` — per-variable bounds
narrower than the config limits (`max_field_T`, `min_temperature_K`,
`max_current_A`, …), set by the human when starting the experiment and
stored in the `ExperimentRecord`.

Enforcement is **in the Orchestrator, not the Gateway**, so it binds every
writer — a human slip in the GUI is caught by the same check as an agent
call:

- New public API `Orchestrator.set_session_envelope(envelope | None)`;
  the envelope is consulted wherever config limits already are: target
  validation on submission and the per-tick safety check.
- The SessionManager sets/clears it on experiment start/close.
- A violation is a normal blocked-action verdict (`action_failed` with
  reason), surfaced in the GUI banner and the action feed alike.

This is deliberate layering: config limits protect the *instrument*
(setup property, YAML), the envelope protects the *sample* (experiment
property, record). Glossary gets both sentences verbatim.

### 3.4 The action feed

Every Gateway request produces an `AgentAction` record: timestamp, agent
identity + role, tool, arguments, verdict (ok / blocked-envelope /
blocked-role / blocked-killswitch / failed), reason, and correlation to a
run when applicable. Written as JSONL per experiment
(`<data_dir>/experiments/<id>/agent_actions.jsonl` — same pattern as
`status.jsonl` and the troubleshoot transcript), and re-emitted as a Qt
signal for the GUI feed panel. The feed is append-only evidence: it is
how the human audits what agents did overnight, and how a debug agent
reconstructs what the session agent was doing.

## 4. The tool surface

### 4.1 Hand-written session tools (the stable core)

Owned by the Gateway, thin wrappers over SessionManager/Orchestrator
public API:

- `get_status()` — orchestrator state, active run, progress, attendance,
  kill-switch state, envelope. `get_live_state()` — latest station
  snapshot. `stream_events()` — manifests, verdicts, status messages.
- `list_experiments() / get_experiment() / get_run() / read_run_data()`
  — records + HDF5 read-back (columns, slices, metadata; never raw dumps).
- `list_procedures() / describe_procedure(name)` — procedures with their
  `ParamSpec` groups rendered as JSON schema (units, bounds, choices).
- `validate_run(procedure, params)` — the free, no-hardware check:
  bounds vs config + envelope, sweep-array well-formedness, duration
  estimate. (Not chosen as *the* pre-flight, but it is the probe run's
  cheap first step and costs nothing to expose.)
- `probe_run(procedure, params, probe_spec)` — §5.
- `queue_run / start / pause / resume / abort` — run control.
- `submit_vi_action(vi, method, kwargs)` — recovery-class VI actions.
- `read_operational_log()` / `read_agent_feed()` — the JSONL tails the
  troubleshoot-runtime skill already knows how to interpret.

### 4.2 Auto-generated instrument surface (the standard)

`describe_station()` is generated from the same source as the GUI panels:
every VI's `@monitored` fields and `@control` methods (names, docstrings,
declared `control_limits`) become tool-discoverable schema. Control calls
route through `submit_vi_action` with the action-class mapping declared
in one place (`recovery` by default; anything touching setpoints classed
`run-control`). **Agent-tool standard** (new written standard +
conformance test): every public `@control`/`@monitored` and every
procedure `ParamSpec` must render to valid JSON schema with a non-empty
description — which the conformance suite can check for every discovered
VI/procedure automatically, today, without a running gateway.

## 5. Probe runs: the pre-flight primitive

A first-class SessionManager operation, not an agent trick:

- `probe_run(procedure, params, probe_spec)` derives a miniature run from
  the real parameters: `probe_spec` names the number of points (default
  ~5), the sweep sub-range policy (`first_points` | `endpoints` |
  `around_start`), and reduced per-point averaging where the measurement
  VI's params allow it. Values must satisfy config limits *and* the
  envelope like any run.
- It executes through the normal Orchestrator path (INITIATING →
  MEASURING → STANDBY) producing a real HDF5 file tagged
  `kind="probe"` in its `RunRecord` and `/metadata/experiment_info`, so
  probe data is auditable but never confused with science data.
- The result returned to the caller: file path, the actual datapoints,
  and cheap derived stats (per-column mean/σ/min/max, NaN count,
  compliance/overload flags from the snapshot). **Judgement stays with
  the caller** — the agent (or human) decides "signal present, noise
  acceptable, proceed"; the framework's job is to make that evidence one
  tool call away. (A `verdict` field with heuristics can come later —
  structure allows it, v1 doesn't hardcode science.)
- GUI gets the same capability as a "Probe first" button on the queue
  item — humans want this too.

## 6. Agent runtimes

- **External Claude Code (first).** Zero runtime code in CryoSoft: an
  `.mcp.json`/skill update teaching sessions to find the local gateway
  (token file + URL), plus a new repo skill (`measure-session`) that
  documents the tool surface, the role model, and the probe-first
  discipline. The existing skills keep their places: `setup-supervisor`
  and the troubleshoot CLI for app-closed driver-level work,
  `troubleshoot-runtime` for log reading — the gateway adds the live
  *acting* surface. Debug agents are simply Claude Code sessions (or
  subagents of the session agent) connecting with `role=debug`.
- **Embedded session agent (later phase).** `cryosoft/session/assistant/`
  runs a Claude Agent SDK client in the app whose tools are in-process
  calls into the same Gateway (same roles, envelope, feed — the embedded
  agent gets no privileged path). The GUI chat panel talks to it; it can
  spawn debug subagents. Needs an API key in app settings (keyring, like
  the eLab key) and a visible cost/usage line in the UI. Because the tool
  surface and permissioning already exist, this phase is mostly SDK
  plumbing + chat UI — and the future "experiment partner" is *only* a
  more capable prompt/skill-set on this same runtime, which is exactly
  the "structure now, partner later" requirement.

## 7. GUI: monitoring the agents (`gui-edit` rules apply)

- **Agent panel** joining the bottom-right quadrant selector (alongside
  Other Devices/Log): the live action feed (attributed, color-coded by
  verdict) and connected-agent list with roles.
- **Takeover strip** in the header area: kill-switch tri-state
  (Active / Read-only / Revoked), attendance toggle (Attended /
  Unattended), and an "agents active" indicator visible from across the
  room.
- **Chat dock** for the embedded session agent (later phase, with §6).
- Envelope editor in the experiment header (from the session-management
  plan's GUI phase): the sample limits are typed in where the experiment
  is started.

## 8. Roadmap (interleaved; supersedes §7 of `session-management-layer.md`)

Foundation (serial):

- **F0 — core groundwork** = old Phase 0 **plus** `set_session_envelope`
  enforcement in the Orchestrator and run manifests carrying a
  `kind` field (run/probe).
- **F1 — records & store** = old Phase 1 (models, store, SessionManager,
  C11/C12), plus `SessionEnvelope` + attendance flag on the record/manager.

Then two independent tracks, landable in any order per slice:

Track A (agent-native):
- **A1 — read-only gateway**: transport thread + token, roles (all
  read-only), `get_status`/`get_live_state`/records/HDF5 tools, action
  feed (JSONL + signal), kill switch. Tests: gateway conformance (no
  hardware imports), feed journaling, a Claude-Code-shaped client harness
  against a sim station.
- **A2 — write path**: role/attendance matrix, envelope-checked
  run-control and recovery tools, `validate_run`, **probe runs**,
  `describe_station`/`describe_procedure` auto-generation + agent-tool
  conformance. Tests: end-to-end sim — an agent client probe-runs, then
  starts, then aborts a FieldSweep; envelope and attendance rejections
  asserted.
- **A3 — GUI surfaces**: agent panel, takeover strip, probe-first button,
  envelope editor.
- **A4 — embedded assistant + chat** (needs A2): SDK runtime, chat dock,
  debug-subagent spawning, `measure-session` skill.
- **A5 (optional) — thin CLI client**: `python -m cryosoft.ctl` — an HTTP
  client + argparse frontend over the *same* gateway API (same token,
  roles, envelope, feed; zero new hardware paths). Rationale: agents are
  natively fluent in CLIs, and a command-line surface works from any
  agent harness, plain terminals, and CI where MCP isn't wired — the
  lowest common denominator for the agentic vision, and the gateway's
  reference client / integration-test harness. Nearly free once A2
  exists. The heavier "headless station mode" (boot the stack without
  the GUI, process locking) stays out of scope.

Track B (eLab) — unchanged from the session-management plan: **B1**
adapter standard + sim + eLabFTW, **B2** renderers + publisher/outbox,
**B3** publish GUI. (The eventual experiment partner drafts eLab entries
through B's API — another reason the tracks stay decoupled.)

Explicitly deferred: experiment-partner behaviors, remote notifications,
remote (non-localhost) gateway access, probe-verdict heuristics.

Every slice ends with `make check` green; GLOSSARY rows land with their
code (Agent Gateway, Agent role, Action class, Session envelope,
Attendance, Action feed, Probe run, Embedded assistant).

## 9. Open questions

1. **Transport details**: streamable-HTTP MCP vs WebSocket; token file
   location and rotation. Decide at A1 with a spike against a real Claude
   Code client.
2. **Recovery-action catalogue**: the exact `@control` methods classed
   `recovery` vs `run-control` per VI type — needs a pass over the four
   VI roles with the physicist before A2.
3. **Envelope scope**: proposed to bind humans too (sample protection
   beats operator convenience) — confirm, since it changes GUI error UX.
4. **Embedded-agent cost controls**: per-session token budget and model
   choice in settings — decide at A4.
