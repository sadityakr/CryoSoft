# cryosoft/session/gateway — the Agent gateway (L6)

## Purpose

Let an autonomous client drive the instrument through exactly the same
**control contract** the GUI uses, with a permission model in front of it.
The engine has two clients — the human at the window and the agent — and
this folder is the second adapter of the one contract, not a second API: an
agent submits the same `Command`s, gets back the same `Verdict`s, and sees
the same `Event`s, so each client reflects what the other did for free.

What this folder adds on top of the contract is **authority**: who may take
which class of action, whether a human is watching, and whether the human
has closed the kill switch. Nothing here is a safety mechanism of its own —
the Orchestrator's admission rules, the control-validation standard's limits
and the **session envelope** bind every writer regardless. This layer
decides how much autonomy is granted *before* those checks run.

The folder's second job is to *publish* that contract as a **Tool
surface**: one callable per action, each with a JSON Schema for its
arguments. Nothing on it is hand-written. A command tool is rendered from
`CommandName` plus the Orchestrator method's own docstring and signature; a
capability tool is rendered from the station's declaration snapshot, one per
`(instrument, @control)`, with that control's `ParamSpec`s as the schema and
the CONFIG's limit as the bound. Only the session tools — the reads over the
experiment store, the run files and the two audit trails, and the two that
draft and publish this experiment's notebook entries — are declared by hand,
because they are not commands.

In-process only: no network, no thread, no socket. A transport is a later,
separate concern that will feed this same `submit()`.

## Architecture layer

**L6, inside the Session Manager.** Imports `cryosoft.core.*` and
`cryosoft.session.*` downward and nothing else — bound by the same
import-linter contracts as the rest of `cryosoft/session/`: **C11** (session
never imports gui/main/drivers/VIs/procedures) and **C12** (nothing below
the GUI imports session). C12 is the reason **Attendance** and the **Kill
switch** are values the Orchestrator holds rather than records this folder
reads: the enforcement point cannot see the session layer, so a policy the
session owns has to be pushed DOWN, exactly as the session envelope already
is.

## Entry (what comes in)

- **An engine**: any object exposing the contract's client surface —
  `submit(Command) -> request_id`, the `verdict_emitted` and `event_emitted`
  signals, and `station_info()`. The Orchestrator is that object today; a
  proxy over a transport will be it later, with no change here.
- **A declared identity**: a `Role` (`observer` / `debug` / `session`) and an
  actor id, stamped onto every `Command` the connection submits as
  `Actor(kind="agent", id=..., role=...)`.
- **The two policy values**, read off the latest `StatusSnapshot` the engine
  published: `attended` and `agent_gate`.
- **The station's declaration snapshot** (`StationInfo`), which is where a
  `submit_vi_action`'s target capability gets its VI kind, and therefore its
  action class — and where every capability tool gets its parameters, units,
  choices and bounds.
- **A `ToolContext`** (optional): the collaborators the session tools read
  through — the experiment façade, the run catalog a proposed run's class
  name is resolved through, the operational log to tail, and, for the ELN
  tools, the **Draft client** to ask and the publisher to queue through.
  Without it every command tool still works and the three declaration reads
  still answer from the mirror; a tool whose collaborator is absent is
  refused BY NAME saying which one. Whether an LLM is in the loop at all is
  therefore decided by whoever wires the gateway: with no `draft_client`
  there is no model, and this package never builds one of its own.
- **Tool calls**: `call_tool(name, args)`, where `args` is JSON and `name` is
  a name from `tool_schemas()`.

## Exit (what goes out)

- **Forwarded commands** on the engine's own `submit()`, actor stamped.
- **Agent-feed command records**, when a `Gateway` was given a feed: every
  command it submits — forwarded or refused here — is written to the
  experiment's **Agent feed** before the permission check, because what an
  agent TRIED is as much a part of accountability as what it managed. The
  answering verdict is recorded by the feed itself off the engine's own
  stream, so the two halves join on `request_id`. `permits()` records
  nothing: asking is not acting.
- **Refusal verdicts** on the same `verdict_emitted` stream every other
  verdict travels: `BLOCKED_ROLE`, with a `detail` dict naming the `rule`
  that refused (`role_matrix`, `attendance`, `kill_switch`, `unknown_role`,
  `unclassified_action`), the `role`, the `action_class` and the
  classification's own `rationale`. A client decides from the dict, never by
  parsing prose.
- **Read-only accessors** over the latest `StatusSnapshot` / `StationInfo`,
  so an agent answers every query from its mirror instead of calling into
  the engine. A refusal the engine never saw still carries a sequence number
  above everything the engine has said, so the two orderings merge.
- **The tool surface**: `tools()` gives the `ToolSpec`s and `tool_schemas()`
  gives the `{name, description, input_schema}` list a tool-use API reads.
  Re-rendered whenever the mirrored declaration is replaced, so an instrument
  that connects brings its capability tools with it.
- **One answer per tool call.** `call_tool()` never raises at its caller: an
  unknown tool, a schema violation, an absent collaborator and an unexpected
  failure are all one `FAILED`-shaped dict whose `detail` names the `rule`
  (`unknown_tool`, `schema`, `missing_collaborator`, `unknown_run`,
  `unknown_run_class`, `no_experiment`, `unexpected_error`, …). A command
  tool's answer carries the `request_id` and the engine's own verdict; a read
  tool's carries `result`.

## Interface contract

- **The permission standard** is `roles.py`'s module docstring, and the
  matrix is one table, `PERMISSION_MATRIX`. Authority is granted by adding a
  row, never by writing a branch:

  | Action class | `observer` | `debug` | `session` | operator (human) |
  |---|---|---|---|---|
  | **read** | permitted | permitted | permitted | permitted |
  | **recovery** | refused | unattended only | permitted | permitted |
  | **run_control** | refused | refused | permitted | permitted |
  | **envelope** | refused | refused | refused | permitted |

- **The human column is not in the table.** `authorize()` returns `None` for
  any actor that is not an `agent`. A permission model that could refuse the
  operator would be a hazard, not a safeguard.
- **Emergency standby sits outside the model**: permitted to every role, in
  every state, at every kill-switch setting, checked before anything else.
- **Envelope is nobody's**: the session envelope, attendance and the kill
  switch are the rules the other rows are judged by, so no role may change
  them.
- **The kill switch only ever subtracts.** `read_only` leaves an agent
  `read`-class actions alone; `revoked` leaves it nothing. It is enforced a
  second time inside `Orchestrator.submit()` — that is the authority, and
  this check is the front door that turns a generic refusal into a specific
  one.
- **No silent default.** An action with no row in a classification table is
  refused BY NAME with a reason saying the classification is missing.
  Conformance asserts every control every shipped config declares has a row,
  so that refusal is a bug report about an un-updated table, never a normal
  outcome.

### The tool surface is rendered, never written

- **A tool is never hand-written for a command.** One tool per `CommandName`,
  named for the command it submits; its description is the first paragraph of
  the Orchestrator method's docstring and each argument's description is that
  method's Google `Args:` entry, so the text an agent reads is the text a
  reader of the code reads.
- **A signature becomes a schema.** A scalar parameter renders straight from
  its annotation. Four commands' JSON `args` are translated by
  `Orchestrator.submit()` rather than being the method's parameters (a
  procedure travels as a class name plus its params, an envelope as a
  mapping, the kill switch as its `AgentGate` value); those are declared once
  in `COMMAND_ARG_SCHEMAS`, each with the rationale for why it deviates. A
  command whose signature carries a type the renderer cannot render and that
  has no entry there **fails to render** rather than being guessed at — the
  same no-silent-default rule the classification tables follow.
- **`submit_vi_action` is rendered per capability.** It is the one command
  whose arguments depend on what it targets, so it becomes one tool per
  `(instrument, @control)`, with the `ParamSpec`s as the schema, the unit
  published, the choices enumerated and the **configured** limit — not the
  declaration's — as `minimum`/`maximum`. An agent therefore reads the bound
  off the tool and is refused by the schema, naming the bound and its unit,
  before a `Command` is ever built. A declared default the configured limit
  would refuse is not published: a schema must never offer a default its own
  bound rejects.
- **Every declared parameter is required.** A control invoked with a
  parameter omitted would fall back to the method's default, and "ramp to
  0 T because the argument was left out" is not an outcome an agent should
  reach by accident.
- **Schemas are closed.** `additionalProperties: false`, so an unexpected key
  is refused rather than dropped.
- **Session tools are hand-declared because they are not commands.** They
  read the store, the run files, the operational log and the agent feed,
  answer "may I run this, and how long will it take?" without dispatching, or
  draft and publish this experiment's notebook entries. Every one is
  `read`-class except `probe_run`, which really is a `run_procedure` with a
  `ProbeSpec` and is classified — and refused — as one, and
  `publish_eln_entry`, which puts a permanent record of the experiment into
  the outside world on its behalf and is `run_control` for it. A run file is
  always reached through `ExperimentStore.resolve_data_file()`, never a
  caller-supplied path: a read tool that accepted an arbitrary path would be
  a file reader wearing an instrument's name.
- **Drafting reads; publishing acts; a human gates the two apart.**
  `draft_eln_entry` renders one finished run's facts into the draft prompt,
  asks one model, and returns the **draft entry** as data — it writes
  nothing and queues nothing, so it is `read`-class, and every role may ask
  for one. `publish_eln_entry` is the only tool that reaches the **Outbox**,
  and while the experiment is ATTENDED it refuses EVERY agent
  (`rule: approval_required`) and parks the draft on the run record for the
  human, so the refusal leaves the work where they will find it rather than
  discarding it. Unattended, the matrix decides as usual: `session`
  publishes, `debug` and `observer` are refused as they are for any other
  `run_control` action.
- **A tool that spends or changes something leaves a trail.** A session tool
  is answered inside this client, so no **Verdict** record would ever name
  it; the two ELN tools therefore declare `ToolSpec.recorded` and the gateway
  writes each call, its arguments, its answer and — for a draft — its cost
  line (`model`, `input_tokens`, `output_tokens`, `cost_usd`) to the **Agent
  feed**. Declared per tool, never derived from the action class: a `read`
  tool an agent polls every tick would drown the trail in observations, while
  a `read` tool that spends model tokens is exactly what the trail is for.
- **The three-way test.** Conformance diffs the rendered surface against
  `CommandName` and against every shipped config's capability manifest, both
  in both directions — the third leg alongside the contract-to-engine and
  contract-to-classification diffs.

### The action-class table is PROVISIONAL

`action_classes.py`'s classification — in particular every
recovery-versus-run-control call — is **PROVISIONAL, to be confirmed by the
physicist**. It was written from the VI docstrings, and the line between "an
agent may do this alone overnight to keep a run alive" and "this commands
the cryostat" is a judgement about a specific instrument rack, not something
derivable from a method signature. Every row carries a one-line rationale
for exactly that review, and four rows (the magnet's persistent-mode and
switch-heater capabilities) say in their rationale that they deliberately
deviate from the default rule below.

The default rule the rows were derived from:

1. A `@control` whose **capability scope** is `operation`, and each lifecycle
   action (`initiate` / `standby`), is `recovery`.
2. Anything that sets a setpoint, ramps, arms a measurement or sources
   current is `run_control`.
3. Anything that only reads is `read`.

## How to add a new module

1. Keep the dependency direction: gateway modules may import `core.*` and
   `session.*`, never `gui`/`main`/drivers/VIs/procedures (C11 fails the
   build otherwise), and nothing below the GUI may import back (C12).
2. Keep the import direction *inside* the folder one-way too:
   `action_classes.py` (what an action is) has no idea who is asking;
   `roles.py` (who may ask) imports it; `gateway.py` (a client asking)
   imports both. A rule that needed those arrows to point both ways would
   mean the split is in the wrong place.
3. **A new command or capability is a new table row, in the same commit.**
   A `CommandName` added to the contract needs a row in
   `COMMAND_ACTION_CLASSES`; a new `@control` or a new VI kind needs one in
   `CONTROL_ACTION_CLASSES` — each with its one-line rationale. Conformance
   diffs both tables against the contract and the shipped configs in both
   directions, so a missing row and a stale row both fail the harness.
4. **A new tool is a new declaration, not a new tool.** A command gets its
   tool from `CommandName`; a capability gets its tool from the station's
   snapshot. Write nothing in `tools.py` for either — if a command will not
   render, give it a `COMMAND_ARG_SCHEMAS` entry with its rationale. Only a
   genuinely non-command read is added to `SESSION_TOOLS`, and then its
   implementation goes in `SESSION_TOOL_FUNCTIONS` under the same key, which
   conformance diffs both ways.
5. New behavior needs its own tests in `tests/test_gateway.py` (the
   permission model) or `tests/test_gateway_tools.py` (the tool surface);
   conformance coverage is necessary but not sufficient.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `action_classes.py` | What an action IS, as declarative tables: one row per `CommandName`, one per `(VI kind, @control name)`, and the two lifecycle actions — each with the rationale a physicist reviews. **PROVISIONAL.** Resolves a `submit_vi_action` to its target's class through the station's declaration snapshot; refuses by name rather than defaulting. | `ActionClass`, `ClassifiedAction`, `UnclassifiedActionError`, `COMMAND_ACTION_CLASSES`, `CONTROL_ACTION_CLASSES`, `LIFECYCLE_ACTION_CLASSES`, `classify_command()`, `classify_control()` | `tests/test_gateway.py` + conformance |
| `gateway.py` | The in-process client an agent holds: one connection, one `Role`, one actor id. Stamps `Actor(kind="agent", ...)` on every command, runs `authorize()`, and either forwards to the engine or answers the request itself with a `BLOCKED_ROLE` verdict on the engine's OWN `verdict_emitted` stream. Mirrors the latest `StatusSnapshot`/`StationInfo` so every read — attendance and the gate included — is answered locally. Duck-typed on `EngineClient`, so it holds the Orchestrator today and a transport proxy later without noticing. No Qt import, no network, no thread. Also publishes the rendered surface: `tools()` / `tool_schemas()` re-render whenever the mirrored declaration is replaced, and `call_tool()` validates a call against its schema before routing it — a command tool through `submit()`, a session tool to its function after the same kill-switch and matrix checks. It answers every call and raises at none. | `Gateway` (`submit(name, args)`, `permits(name, args)`, optional `feed=` (the **Agent feed** every submitted command is written to before it is forwarded or refused), `call_tool(name, args)`, `tools()`, `tool_schemas()`, `tool(name)`, `status()`, `station()`, `state()`, `attended()`, `agent_gate()`, `role`, `actor`), `EngineClient` | `tests/test_gateway.py`, `tests/test_gateway_tools.py` |
| `roles.py` | Who may take an action of a given class: the `Role` enum, the `Permission` cell values, the one `PERMISSION_MATRIX` table that is the standard, and `authorize()` — the ordered checks (emergency standby, actor kind, role validity, classification, kill switch, matrix) that answer with `None` or one `BLOCKED_ROLE` verdict. | `Role`, `Permission`, `PERMISSION_MATRIX`, `authorize()` | `tests/test_gateway.py` + conformance |
| `tools.py` | The **Tool surface**, rendered not written: one command tool per `CommandName` (description from the Orchestrator method's docstring, schema from its signature or its `COMMAND_ARG_SCHEMAS` entry), one capability tool per `(instrument, @control)` the station declares (schema from the `ParamSpec`s, bounds from the config), and the hand-declared session tools that read the store, the run files and the two audit trails, and that draft and publish notebook entries. Validates a call against its schema and names the bound it violated. | `ToolSpec`, `ToolContext`, `ToolError`, `SESSION_TOOLS`, `COMMAND_ARG_SCHEMAS`, `render_tools()`, `render_command_tools()`, `render_capability_tools()`, `capability_tool_name()`, `validate_tool_args()`, `call_session_tool()` | `tests/test_gateway_tools.py` + conformance |
