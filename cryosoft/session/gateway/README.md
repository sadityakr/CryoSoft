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
  action class.

## Exit (what goes out)

- **Forwarded commands** on the engine's own `submit()`, actor stamped.
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
4. New behavior needs its own tests in `tests/test_gateway.py`; conformance
   coverage is necessary but not sufficient.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `action_classes.py` | What an action IS, as declarative tables: one row per `CommandName`, one per `(VI kind, @control name)`, and the two lifecycle actions — each with the rationale a physicist reviews. **PROVISIONAL.** Resolves a `submit_vi_action` to its target's class through the station's declaration snapshot; refuses by name rather than defaulting. | `ActionClass`, `ClassifiedAction`, `UnclassifiedActionError`, `COMMAND_ACTION_CLASSES`, `CONTROL_ACTION_CLASSES`, `LIFECYCLE_ACTION_CLASSES`, `classify_command()`, `classify_control()` | `tests/test_gateway.py` + conformance |
| `gateway.py` | The in-process client an agent holds: one connection, one `Role`, one actor id. Stamps `Actor(kind="agent", ...)` on every command, runs `authorize()`, and either forwards to the engine or answers the request itself with a `BLOCKED_ROLE` verdict on the engine's OWN `verdict_emitted` stream. Mirrors the latest `StatusSnapshot`/`StationInfo` so every read — attendance and the gate included — is answered locally. Duck-typed on `EngineClient`, so it holds the Orchestrator today and a transport proxy later without noticing. No Qt import, no network, no thread. | `Gateway` (`submit(name, args)`, `permits(name, args)`, `status()`, `station()`, `state()`, `attended()`, `agent_gate()`, `role`, `actor`), `EngineClient` | `tests/test_gateway.py` |
| `roles.py` | Who may take an action of a given class: the `Role` enum, the `Permission` cell values, the one `PERMISSION_MATRIX` table that is the standard, and `authorize()` — the ordered checks (emergency standby, actor kind, role validity, classification, kill switch, matrix) that answer with `None` or one `BLOCKED_ROLE` verdict. | `Role`, `Permission`, `PERMISSION_MATRIX`, `authorize()` | `tests/test_gateway.py` + conformance |
