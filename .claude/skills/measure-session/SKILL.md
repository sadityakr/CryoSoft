---
name: measure-session
description: Drive a RUNNING I2AS station from this session through the cryosoft MCP server - read the station, validate and probe a run before starting one, watch it, and stop it. Covers the role you were granted, what each refusal means, the kill switch and attendance, and what the physicist sees in the GUI while you act. Use when the user asks you to run, queue, validate, probe, watch, pause or abort a measurement, to change an instrument setpoint, or to explain what the station is doing right now. NOT for editing I2AS's own source (that is ordinary repository work) and NOT for diagnosing instruments with the app closed - that is the setup-supervisor skill.
---

# measure-session — driving a live station from outside the app

You are talking to a **running** I2AS application through the `cryosoft`
MCP server. That server is an adapter in its own process: it holds one
connection to the app's gateway, and everything you call is judged by the
app before it reaches an instrument. There is a physicist at, or
responsible for, this station. Act like a colleague at their bench.

## What the surface is

Every tool you see was rendered by the application from its own command
contract and from the station's declaration — nobody wrote them for you.
Three consequences:

- **The tool list is this station.** `magnet_z__set_field` exists because
  this setup has a z magnet; the bounds in its schema are that setup's
  configured limits. Another station offers different tools.
- **The descriptions are the code's own docstrings.** Read them. They are
  the same text a person reading the source reads.
- **Re-list when the station changes.** Connecting an instrument adds its
  tools. Nothing is cached (`ttlMs` is 0 for that reason).

Three resources answer the questions you should ask first:

| Resource | What it tells you |
|---|---|
| `cryosoft://status` | State, active run, per-instrument readings and ramps, faults, holds, **attendance** and the **kill switch**. |
| `cryosoft://station` | Every configured instrument, what it reads, what it can be asked to do, within which bounds. |
| `cryosoft://manifest` | The same declaration with capabilities resolved into groups. |

## Your role, and what it will not let you do

At the handshake this session declared a role, and the application granted
it only if its own ceiling allows it. Find out which one you got — it is in
the adapter's startup log and in every refusal you receive.

| Role | Read | Recovery (pause/resume, connect, re-send config, adjust waits) | Run control (start/stop a run, command energy into the station) |
|---|---|---|---|
| `observer` | yes | no | no |
| `debug` | yes | only while the experiment is **unattended** | no |
| `session` | yes | yes | yes |

Nobody — no role — may change the **session envelope**, **attendance**, or
the **kill switch**. Those are the rules you are judged by; if you could
widen them you would not be bounded by them. `set_experiment_envelope`,
`set_attendance` and `set_agent_gate` are on the tool list because the
surface is rendered from the whole contract, and they will refuse you. That
is correct, not a bug: ask the physicist to make the change in the GUI.

**Emergency standby is the exception.** `emergency_standby` is permitted to
every role, in every state, whatever the kill switch says. If you can see
that the station is in danger, use it. Never hesitate over authority when
the answer is "make it safe".

## The run in flight belongs to whoever started it

You are not the only actor here. Every run has an **owner** — the actor
that started it (a queued run: whoever queued it) — and `read_status`
publishes it as `run.owner`, so you can always see whose run you are looking
at before you touch it. Ownership is a fact about the run, not a lock: it
reserves nothing, expires never, and ends when the run does.

On a run you do not own, one call is refused: `abort_procedure` — the one
that ends somebody's result. The refusal names the owner (`detail.rule == "run_owner"`,
`detail.owner`). Everything else is untouched: reads, pausing, resuming,
stopping a ramp and emergency standby are never owner-scoped, because
holding the station where it stands destroys nobody's work.

If you genuinely must act anyway, the same call takes `override_owner: true`
and a `reason` — and that is a **takeover**: it is recorded on the run's own
`RunFinished`, in the agent feed, and as a distinct row in the physicist's
Agent panel reading "took over &lt;owner&gt;'s run: &lt;your reason&gt;". An
override without a reason is refused (`override_reason_required`). So:
**ask the physicist first.** Take a run over only when you cannot ask and
the situation demands it, and say plainly, in the reason, why — that
sentence is what someone reads months later when they ask what happened to
that measurement.

## Read a refusal; do not retry it

A refused call comes back as a normal result — `isError: true` with the
app's own answer inside — carrying a `code` and a `detail.rule` naming
exactly what refused you:

| `detail.rule` | Meaning | What to do |
|---|---|---|
| `role_matrix` | Your role does not grant this class of action. | Report what you would do and why; ask the physicist to do it or to raise the ceiling. |
| `attendance` | You are `debug` and a human is watching. | Diagnose and **report**. The human decides. |
| `kill_switch` | The physicist set the gate to `read_only` or `revoked`. | Stop acting. Say what you were about to do. |
| `run_owner` | The run in flight is another actor's, and this call would end it. | Ask the owner or the physicist. Only if you cannot, re-send with `override_owner` and a `reason` — a recorded takeover. |
| `override_reason_required` | You asked to take a run over without saying why. | Re-send with a reason that would satisfy the physicist reading it later. |
| `unclassified_action` | The action has no declared class in the app. | A gap in the application, not something to work around. Report it. |
| `schema` | An argument was out of bounds or the wrong shape. | Fix the argument; the message names the bound and its unit. |
| `unknown_tool` | The surface has no such tool. | Re-read `tools/list`; an instrument may be disconnected. |

Retrying a refusal unchanged is always wrong. So is reaching for a
different tool that achieves the same forbidden thing.

## The order of operations for a measurement

Do not start a run because you were asked to start a run. Do this:

1. **Read `cryosoft://status`.** Is a run already going? Is the state
   `ERROR`, `PAUSED` or `EMERGENCY`? Is a fault outstanding? Is anyone
   attending? What is the kill switch set to? If any of that is unexpected,
   say so before doing anything else.
2. **Read `cryosoft://station`** if you have not this session, so you are
   proposing parameters this station can actually reach.
3. **`validate_run`.** It answers whether the run may be queued at all —
   declared bounds, a headless build of the procedure, the setup's limits,
   the session envelope — and tells you roughly how long it would take.
   Every finding it returns is a sentence you should relay, not a number
   to skim past. A run that fails validation is not a run to force.
4. **`probe_run`.** The same procedure, driving the same instruments,
   through the same code path, subsampled so it costs minutes rather than
   hours. It is how you find a wrong column, an unreachable setpoint or a
   misspelled measurement VI before committing the station's time. **Probe before the first run of anything new**, and say what the
   probe showed.
5. **Then start it** — `run_procedure` to start now, `queue_procedure` to
   put it behind what is already queued. Report the request id you got
   back.
6. **Watch it.** State changes and status snapshots arrive on their own as
   log notifications while you hold the session; you do not poll for them.
   Read `cryosoft://status` when you need the full picture.
7. **Stopping.** `pause_procedure` / `resume_procedure` for a hold;
   `abort_procedure` to end the run. Aborting discards nothing already
   written, but it does end the measurement — say what you are about to
   abort and why before you do it, unless the situation is unsafe, in which
   case act first. If the run is not yours, see "The run in flight belongs
   to whoever started it" above before you reach for abort.

## Long runs, and being alone with the station

**Attendance** is the physicist's statement about themselves, made in the
GUI. When it is off, they have left the instrument in your care and a
`debug` session may act to keep a run alive. When it is on, they are
watching, and a `debug` session's job is to tell them what it sees.

The **kill switch** (`agent_gate`) is their brake: `active` (normal),
`read_only` (you may look, not touch) and `revoked` (you may do nothing but
make the station safe). It appears in `cryosoft://status`. If it changes
mid-session, stop and acknowledge it.

Neither is yours to change.

## Everything you do is visible, and permanent

- **The physicist's window shows your actions as they happen**, labelled
  with the actor id this session declared and the role it was granted —
  they see you being refused exactly as they see you being obeyed.
- **Every command and every verdict is appended to the experiment's agent
  feed**, joined by request id, alongside the run records. `read_agent_feed`
  reads it back; `read_operational_log` reads the app's own tick log.
- So **name yourself honestly** and **say out loud what you are about to
  do** before a run-control action. A physicist reading the feed tomorrow
  should be able to reconstruct your reasoning from it.

## If the adapter will not start

It exits with the reason on stderr. The usual ones:

- *No gateway descriptor* — the application is not running, or its
  `monitor.yaml` does not say `gateway_server: true`. Both are the
  physicist's to change; the second is a deliberate setup decision.
- *The role was refused* — the app hands out at most the role its
  `gateway_max_role` names, and this session asked for more.
- *A different wire schema* — the adapter and the running application are
  from different versions of I2AS.

Report which one it was. Do not work around it by editing configuration
files: opening a station to an autonomous client is the physicist's
decision to make.
