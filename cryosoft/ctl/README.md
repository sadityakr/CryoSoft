# cryosoft/ctl — the Reference client

## Purpose

`python -m cryosoft.ctl` is the terminal end of the **Agent gateway**: one
JSON answer per invocation, over the same **Tool surface** an in-process
agent is offered, judged by the same permission model and answered by the
same **Verdict**. It exists for three readers at once — a physicist checking
a running experiment from a shell, an agent harness that has no tool-use
runtime yet, and the integration tests, which drive this package's `main()`
rather than a hand-built client so the thing under test is the thing that
ships.

It adds **no authority of its own**. Everything it can do, an in-process
client can already do; what this package contributes is a *process boundary*
— argparse in front, JSON out the back, and two ways of reaching an engine
that is either built here or already running somewhere else.

## Architecture layer

**A leaf entry point, above everything.** Like `cryosoft.main` and
`cryosoft.troubleshoot`, nothing in `cryosoft` imports it. It imports
`cryosoft.core.*`, `cryosoft.session.*` and `cryosoft.troubleshoot.*`
downward, plus `cryosoft.procedures` for the one job an entry point owns:
**discovery**. A run travels as a class *name*, and neither the engine
(contract C5) nor the session layer (C11) may import the procedures package,
so whoever owns discovery resolves that name and hands the catalog down —
which is exactly what `cryosoft.main` does before it builds anything.
Contract **C20** freezes the rest of the boundary: no GUI, no drivers, no
virtual instruments, ever.

## Entry (what comes in)

- **A command line**, in the grammar `python -m cryosoft.ctl [connection
  options] <subcommand> [arguments]`. Connection options come *before* the
  subcommand because they choose which engine it runs against.
- **A mode.** `--offline <config_dir>` builds a whole instrument stack in
  this process (`InstrumentHost(mode="inline")`, the Station, the engine and
  the session layer over it); without it the client is **live** and talks to
  an application that is already running through the **Request spool**.
- **A declared identity.** `--role observer|debug|session` (default
  `observer`) and `--actor` (default `ctl:<user>@<host>`), stamped onto
  every command as `Actor(kind="agent", ...)`.
- **Tool arguments** as JSON: `--args '{"target_T": 1.5}'`, `--args @file`,
  or `--args -` to read one object from stdin.

## Exit (what goes out)

- **One JSON object on stdout**, always — the gateway's own answer dict
  (`ok`, `code`, `detail.rule`, `result` or `verdict`) with `mode`, `role`
  and `actor` in front of it. Logging goes to stderr, so stdout stays
  parseable.
- **One of three exit codes**:

  | Code | Meaning |
  |---|---|
  | `0` | The tool answered and the answer is `ok`: a `Verdict` of `OK`, or a read that succeeded. |
  | `1` | The request was reached and REFUSED or FAILED — a `BLOCKED_*` verdict, a schema violation, an unknown tool, an absent collaborator. `detail.rule` says which. |
  | `2` | No engine was reached, so nothing was asked: no spool, a verdict that never arrived within `--timeout`, a config that would not build. Also argparse's own code for a usage error. |

- **A request file and a feed record.** Live, every command becomes one
  `requests/<request_id>.json` the running tick drains; offline, every
  command a non-operator actor submits is written to the open experiment's
  **Agent feed** before it is forwarded or refused.

## Interface contract

- **The subcommands are the tool surface, not a list of their own.**
  `tools`, `schema <tool>` and `call <tool> [--args JSON]` reach every tool
  the station publishes; the rest are shorthands for the calls that are made
  often enough to deserve a word — the reads (`status`, `station`,
  `manifest`, `runs`, `feed`) and the four interventions (`pause`, `resume`,
  `abort`, `emergency-standby --reason`). **A new command or capability
  needs no code here**: it renders into `tools` and is reachable through
  `call` the moment it is declared.
- **The two modes differ in transport, never in authority.** The declared
  role travels on the request and is judged at the engine; live, it is
  capped a second time by the setup's `monitor.yaml` `spool_max_role`
  (`observer` by default). No refusal is decided by this process alone, and
  a refusal here is always also a refusal there.
- **A client never opens or closes an experiment.** That is `envelope`-class
  work, which the permission matrix grants to no role: the physicist opens
  the experiment and the client works inside the one that is open. With none
  open, every command tool still works and the session tools that need a
  record refuse by name (`detail.rule == "no_experiment"`).
- **Reads are answered from a mirror, not by asking the engine.** Offline,
  the gateway's own mirror; live, the files the running engine mirrors into
  the spool (`station.json`, `events.jsonl`) plus the session store on disk.
  The one read a live client cannot answer is `validate_run`, which builds
  the proposed run against the Station — it refuses naming the Station as
  the missing collaborator, rather than answering "no findings", which would
  read as approval.
- **The offline tick is driven by hand.** A one-shot command has no Qt event
  loop, so the timer the engine starts never fires; `CtlClient.pump()` calls
  the engine's own tick directly, which is also what lets an invocation
  report the verdict of a command the engine queued for its tick
  (`submit_vi_action` is the one that does).
- **`main(argv, client=...)`** takes an already-open client, so a scenario —
  a sequence of commands against a station that remembers what the last one
  did — can be driven against one in-process stack. Nothing else about the
  invocation changes.
- **Nothing prompts.** Authorisation is the harness's job, and a hung prompt
  is the worst failure mode an agent can be given.

## How to add a new module

1. **Do not add a subcommand for a new command or capability.** It is
   already reachable through `call`; a shorthand is only worth adding for
   something a *person* types often, and it must route through `_call()` so
   the shorthand and the equivalent `call` differ in nothing but the words
   typed.
2. Keep the dependency direction: `core`, `session`, `troubleshoot` and
   `procedures` (for discovery) downward, nothing else — contract C20 fails
   the build otherwise, and nothing in `cryosoft` may import this package.
3. A new *transport* is a new engine adapter beside `SpoolEngine`, satisfying
   the gateway's `EngineClient` shape (`submit`, `verdict_emitted`,
   `event_emitted`, `station_info()`). Everything above it — the subcommands,
   the exit codes, the answer shape — is then unchanged, which is the point.
4. New behaviour needs its own tests in `tests/test_ctl.py` (the grammar,
   the two modes, the exit codes) or `tests/test_phase_e_scenario.py` (the
   end-to-end scenario); conformance coverage is necessary but not
   sufficient.

## Files

| File | Responsibility | Key public API | Owning test |
|------|----------------|----------------|-------------|
| `__main__.py` | The entry point `python -m cryosoft.ctl` runs. Nothing but `sys.exit(main())`. | — | `tests/test_ctl.py` |
| `cli.py` | The command grammar and the answer shape: the argparse tree, one handler per subcommand (each routing through `_call()`), the JSON printer, and the three exit codes. Turns any failure into an answer rather than a traceback. | `build_parser()`, `main(argv, client=...)`, `EXIT_OK`, `EXIT_REFUSED`, `EXIT_UNREACHABLE` | `tests/test_ctl.py` |
| `client.py` | The two ways of reaching an engine, behind one object: `--offline` builds the Station, the engine and the session layer in this process and drives the tick by hand; live wraps the **Request spool** as an `EngineClient` (`SpoolEngine`) whose `submit()` writes one request file and waits for the running tick's verdict, and reads the session store on disk (`_StoredExperiments`) so `runs`, `feed` and the run reads work out of process. Raises `CtlUnreachable` — and only that — for "I never got to ask". | `CtlClient` (`gateway`, `call()`, `pump()`, `close()`), `SpoolEngine`, `open_client()`, `CtlUnreachable`, `default_actor_id()` | `tests/test_ctl.py` |
| `discovery.py` | The run catalog this entry point hands down: import every module of `cryosoft.procedures`, then catalog every named subclass by class name. One broken module is logged and skipped rather than leaving the client with no catalog. | `discover_run_catalog()` | `tests/test_ctl.py` |
