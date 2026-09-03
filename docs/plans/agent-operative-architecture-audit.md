# Agent-operative CryoSoft: a LAP-grounded architecture audit and revised sequencing

**Status:** proposal, no code yet. Revises the *sequencing* of
`agentic-instrumentation-framework.md`; that document's nine-module framework
and most of its phase content stand and are cited throughout.
**Scope:** audit the current architecture against the Lab Agent Protocol
(Zhu et al., arXiv:2606.03755v1, June 2026), decide the design changes that are
expensive to reverse, and sequence the work that makes this setup operable by
an agent.
**Date:** 2026-08-08
**Survey basis:** seven parallel evidence-anchored surveys — one per LAP
dimension — each adversarially verified by a second reader who re-opened every
`path:line` anchor. Findings that did not survive verification are excluded;
findings whose severity the verifier corrected carry the corrected severity.
Every claim below was re-checked against source before inclusion.

---

## 1. What LAP is worth here

LAP is a **v0.1 design specification with no implementation** — its own
comparison table states this plainly ("Running impl.: none (design)"). It is a
requirements document for the agent↔instrument edge, not a standard to conform
to. Read it that way and it is genuinely useful; read it as a spec to implement
and it will cost months building a federation for a lab with one cryostat.

Four ideas transfer, and they are the reason this audit exists:

1. **The three-way safety split.** LAP separates (A) device-physics safety
   (local, instantaneous, per-instrument), (B) authorization and accountability
   safety (crosses the human–agent trust boundary), and (C) workflow/integrity
   safety (compositional hazards, results taken under lapsed calibration,
   unbounded resource consumption). Each is enforced at the only layer that can
   see it. This is the sharpest lens in the paper and it maps cleanly onto
   CryoSoft's own layering.
2. **A capability description is a contract, not documentation.** What an agent
   cannot read, it cannot respect. A blank tooltip is cosmetic; a blank schema
   field is functional.
3. **A result without units, calibration, uncertainty and provenance is not a
   measurement.** Reproducibility must be a property of the file, not of the
   person who remembers what the config said.
4. **An error is re-planning data.** Every refusal should carry the structured
   context that lets an agent choose a different action without a human.

Three ideas do not transfer, and are rejected in §7 with reasons: leased
reservations, cryptographic operator tokens, and the whole L4 federation layer.

## 2. The finding that reorders everything

The existing plan's central sequencing call is **"self-description before
transport"** — vocabulary first, because a gateway over blank metadata gives an
agent confident access to bad information. That call is correct and this audit
does not overturn it.

But it is not the *first* call, because it assumes the write path is safe and
merely inarticulate. It is not.

**CryoSoft's safety machinery guards the plan path. An agent gateway would be
built on the other one.**

Every mechanism the architecture is proud of — the control-validation standard,
the capability-scope partition, the `ExperimentEnvelope`, the claims system —
protects `Target`s and `Command`s flowing through a procedure and the
Orchestrator's dispatch. The direct action path,
`Orchestrator.submit_vi_action()` → `Station.execute_vi_action()`, is the path
the existing plan's Phase 4 lists as "a thin wrapper over existing public API".
It is the least guarded surface in the system:

- `Station.execute_vi_action()` (`core/station.py:1585-1601`) is a bare
  `getattr(vi, method_name)` followed by a call. No `@control` check, no
  underscore-name check, no capability-scope check. It will invoke any attribute
  on the VI by name.
- The same repository already does this correctly one package over.
  `DriverBench.call()` (`troubleshoot/engine.py:626-640`) refuses a private
  name, refuses a name that is not a known method, and refuses a state-changing
  method unless `allow_write=True`. The contrast is internal, not aspirational.
- `ExperimentEnvelope` is checked in `_dispatch_targets()`
  (`core/orchestrator.py:651-667`) and per tick against live readings. It never
  sees a direct action. An agent sending `set_field` as an action rather than as
  a `Target` is outside the envelope entirely.
- `get_control_scope()` (`core/decorators.py:224-236`) is consumed only by
  `Station.send_measurement_commands()`. On the direct path it is dead metadata.

And the capabilities reachable through that unguarded path include the ones that
destroy samples:

**The excitation-current setters are bounded nowhere.** Only four VI families
declare `control_limits` at all — `rotator.py:49`,
`sample_temperature_controller.py:59` (inherited and extended at
`vti_temperature_controller.py:44`), `superconducting_magnet.py:49`, and
`tensormeter_rtm2_measurement.py:214`. The DC-mode, delta-mode and lock-in
harmonic measurement VIs declare none, so `initiate_measurement(current=…)`
(`measurement_dc_mode.py:155`, `measurement_delta_mode.py:190`) and the
reading-loop setters `set_dc_current` (`:226`) and `set_delta_current` (`:239`)
accept any value. The enforcement wrapper is opt-in by construction —
`base.py:433` reads `if limit_spec:`, so an undeclared method is simply never
checked — and **no conformance test requires the declaration to exist**. The
control-validation standard is "enforced if declared", and nothing forces
declaring. On the production setup both measurement VIs ship
`init_params: {}` (`configs/12t-cryo/devices.yaml:135, 141`), so there is not
even a config value to bound them with.

This is not an agent problem. It is a live hazard for the physicist too; the
agent surface merely makes it reachable programmatically at 3 a.m.

**The second confirmed critical is a silent data-corruption path.**
`Station.get_state()` (`core/station.py:806-849`) handles a failed poll
correctly for its *return* value — it builds a `stale` dict carrying `_stale`
and, past `max_errors`, `_disconnected`. But it never writes that dict back into
`self._last_known_state`; the cache keeps the last *successful* values with no
marker. `cached_state` (`:774-783`) returns exactly that cache, and
`last_state_flat()` (`:785-805`) — which produces the HDF5 sweep columns and
feeds `/snapshots/` — explicitly drops every `_`-prefixed key anyway. A sample
thermometer that stops answering during a six-hour field sweep therefore writes
a frozen, unmarked temperature into every remaining datapoint, and the file
gives a later reader no way to tell.

So the revised first move is not vocabulary and not transport. It is: **close
the write path and make the data honest.** Both are days of work, both are
worth doing even if no agent is ever connected, and both are prerequisites for
anything that follows.

## 3. Scorecard: CryoSoft against LAP's four primitives

Severities are post-verification. "Superior" means CryoSoft's answer is better
than what LAP specifies, not merely different.

| LAP primitive | CryoSoft today | Gap |
|---|---|---|
| **P1 InstrumentCard** — generated, signed capability + physical-limit description | Generation machinery complete and proven (the whole GUI is built from `get_monitored_methods()` / `get_control_methods()`). `ParamSpec` (`core/plan.py:278`) is a working `lap:Quantity` with eager validation. `control_limits` + `_make_limit_wrapper` is a working physicalLimits that refuses before any hardware command. | **Declarations, not machinery.** 34 `@monitored` declarations carry no unit and no description — `@monitored` captures only `_is_monitored` and `_display_name` (`core/decorators.py:66-67`). 8 of 33 `@control` declarations carry `params=` ParamSpecs. No capability declares hazard, reversibility, side effects, duration or intent tags. |
| **P2 Reservation** — leases, epochs, expiry, exclusive/shared-read | **Superior.** One process, one thread, one QTimer; every hardware write serialized through the tick's action drain. Per-VI claims with a declared owner (`claimed_vi_names()`), a single admission predicate shared by submit and drain (`core/orchestrator.py:1172-1293`) and **re-evaluated at drain time** — stronger than LAP's one-shot gate — plus guaranteed release on every teardown path. | **Liveness and accountability, not exclusion.** No actor identity anywhere in the repository. Refusals are prose on a broadcast signal with no holder, no code, no expiry. No bound on a hung run (`Gate` has no timeout). No arbitration between the app and the `troubleshoot` CLI or the `diagnose-connections` scripts, which open VISA independently. |
| **P3 Safety fence** — S0–S3 classes, operator tokens, informed consent | **Superior in two places.** Level-triggered hold enforcement re-asserts `standby()` every tick for as long as a condition holds (`_enforce_safety_holds()`), which LAP has no analogue for. The `operator_ack` step already exceeds LAP's informed-consent bar: human-readable label, live state detail, explicit consequence-naming skip warning, and an attestation recorded with a snapshot of physical conditions at that instant. `ExperimentEnvelope` **is** the standing scoped policy grant LAP recommends over per-action prompts — and it binds the human too. | **Classification and attribution.** CryoSoft classifies safety *conditions* by severity beautifully and *capabilities* by hazard not at all: `switch_heater_on` (a documented quench risk) and `set_refresh_rate` (harmless) are byte-identical in every machine-readable field. `StepRecord` has no actor, so an agent self-confirming a physical step is indistinguishable from the physicist. The envelope is fully implemented, fully enforced, and **never actually set** — every construction in the repository is in a test. Class (C) has no answer at all. |
| **P4 MeasurementResult** — units, calibration, uncertainty, provenance | **Ahead of LAP on uncertainty.** The mean/error/array convention machine-requires a `{quantity}_error` SEM column for every raw-sample array, conformance-enforced the moment a VI file exists. Provenance is substantial: merged params, sample info, arming commands, per-point UTC timestamps, and a full station snapshot at *every* sweep point — richer than LAP's single `environment` block. | **The numbers are meaningless without constants that never reach the file.** `amperes_per_tesla: 7.954` (`configs/12t-cryo/devices.yaml:89`) is the sole thing converting the recorded `field_T` column into a real field; edit it and two files that disagree by 20% in field are byte-indistinguishable in their metadata. `series_resistance_ohm` is worse — it has a hardcoded `1e6` fallback (`lockin_harmonic.py:135-136`) and is the sole divisor producing the recorded `current_A` (`:212`). Plus: no instrument identity in the file, no run id, no read-back API, and the stale-cache defect of §2. |

Two cross-cutting observations the primitive-by-primitive view misses:

**The house already invented its agent protocol, at the wrong layer.**
`troubleshoot/README.md:29-44` declares three invariants: every operation
terminates on its own; `FaultCode` values are API and are only ever added; read
and write are separate paths gated differently. Add its uniform `--json` output
and its per-invocation JSONL transcript and that is four of the things the
operational layer lacks, already solved, already documented, already used by the
shipped skills. It is unreachable from the running app by construction (contract
C10), and correctly so. The work is not to invent conventions — it is to
**promote these four to a house standard** and give `cryosoft.ctl` the same
shape.

**The conformance harness is what LAP says the field still needs.** LAP's stated
next step is "a reference implementation and conformance suite exercising all
four primitives on one laptop with no hardware". CryoSoft ships that: 56
auto-discovering conformance tests over five discovery helpers, 15 mechanically
enforced import contracts, a sim twin per real driver modelling failure physics,
composable fault-injection scenarios reusable from pytest *and* from the live
GUI, and two thin CI wrappers over one Makefile. This is the single greatest
asset in the audit, because it means agent-operability can be made a
**self-enforcing standard** rather than a feature that rots. §6 is the list.

## 4. Four design decisions to take before any code

Each is expensive to reverse once declarations exist at 30+ call sites.

### D1 — One StationCard, not N InstrumentCards

LAP's unit is one card per instrument, each independently addressable and
reservable. Adopting that shape here would be wrong three times over: the
Orchestrator is the sole writer, so per-instrument addressability advertises an
independence the architecture forbids; drivers are shared between VIs (in the
12T config `keithley_6220` is the source for both the delta-mode and DC-mode
VIs), so per-instrument cards would double-declare one physical device; and the
thing a physicist's goal actually maps to is a *procedure*, which has no
per-instrument home at all.

**Decide:** one `StationCard` per setup, with per-VI instrument entries and a
flat capability list addressed `vi_name.method_name` — the identifiers
`Command(vi_name, method, kwargs)` and `submit_vi_action(vi_name, method_name)`
already use. Procedures and operations are first-class card sections, not
capabilities. The safety-flag registry (flag → severity → producing VIs →
concerned VIs) is a station-level section, because it expresses cross-instrument
dependency that LAP explicitly punts to a Lab Coordinator that does not exist
here.

**Corollary:** the card splits into a **static** half (types, units,
descriptions, bounds, hazard, choices) that is hardware-free by construction and
a **live overlay** (current values, availability, conditions) sourced from
`Station.cached_state` — never from a fresh poll. This matters concretely: the
existing plan specifies building the manifest from a live Station, but
`Lakeshore335SampleTemperatureControllerVI.control_param_specs()` reads the
hardware, so a `describe()` from a CLI would issue bus traffic outside the tick.
Make "`control_param_specs()` is a pure read of config and cached state" a
written standard with a conformance test.

### D2 — Hazard is a property of the capability; the ceiling is a property of the setup

The naive reading of "constants and limits in config, not in code" pushes hazard
classification into every `devices.yaml`, where four setups can disagree about
whether energising a switch heater can quench a magnet. The rule is about
*numbers*, and its own justification says so ("limits are setup properties").
Whether an action is hazardous is a fact about physics, identical in every lab
that owns that magnet, and belongs in code beside the imperative check that
already knows it.

**Decide:** a floor-and-raise split.
- `hazard=` on `@control`, a closed four-value vocabulary validated at
  decoration time exactly as `scope` already is (`core/decorators.py:113-117`),
  stored as a plain string so contract C1 holds. Category base classes declare
  the default so every VI of a role inherits the right classification, exactly
  as `safety_flags` already does.
- A conformance test enforcing a **minimum floor per category** — no VI may
  declare a hazard below its base class's. This is LAP's answer to the
  mislabeled-card threat, and CryoSoft can enforce it where LAP can only
  recommend it.
- Numeric ceilings stay in config, including a new per-setup agent ceiling that
  lifts the `L0/L1/L2/L3` excitation ladder out of `setup-supervisor`'s skill
  prose (where it exists today, for the app-closed path only) and into the
  `safety:` block where limits belong.

**And keep `hazard` orthogonal to `scope`.** They will look mergeable and are
not: `initiate_measurement` must stay measurement-scope because reading loops
dispatch it at every sweep point, while being genuinely hazardous — it energises
a source into the sample. Two axes, two enforcement points: `scope` answers
"which kind of plan may contain this" and is enforced at dispatch; `hazard`
answers "what does this cost if it goes wrong" and is enforced in the admission
predicate against the envelope, the attendance flag, and any standing grant.

### D3 — Refusal codes are string enums, and the taxonomy is derived from the code that exists

LAP's `-33xxx` numeric codes exist only because JSON-RPC 2.0 requires an integer
`code`. CryoSoft has no JSON-RPC and will not for some time. A number is
unreadable in a JSONL log and in a GUI banner.

**Decide:** `RefusalCode(str, Enum)`, matching the two closed vocabularies the
repository already ships (`FaultCode`, `RunFaultCode`). JSON-ready, greppable,
readable in a banner, and trivially mapped to an integer by a future adapter.
Derive the members one-to-one from the refusal sites that exist today rather
than from LAP's table — the admission predicate's six branches, the limit
wrapper, the interlock guards, the capability-scope refusal, the envelope check,
the availability policy. **The enumeration is the deliverable**; the enum is
just its shape.

### D4 — No thread in the transport; the instrument thread is the one thread, per the single hardware thread standard

The existing plan calls the MCP transport thread "the single riskiest change in
this entire programme", and it is right — it would put a second thread on the
bus, which the single hardware thread standard
(`instrument-thread-and-responsive-gui.md` §3) forbids: the instrument thread
owns every driver, and a transport is a client of it like any other. But the
risk is a property of the proposed design, not of the requirement.

**Decide:** a three-rung ladder of monotonically decreasing risk, none of which
adds a thread.
1. **Tick-drained request spool.** A pure stdlib module scans a request
   directory and the tick drains it at the same point it already drains
   `_gui_action_queue`; verdicts append to a JSONL sink. This gives an agent a
   write path to a *running* app — the single biggest structural limitation
   today — with no socket, no thread, and no new failure mode.
2. **Same-thread local socket.** `QLocalServer`'s `readyRead` is an ordinary
   slot on the Qt event loop that already drives the tick. It parses the frame
   and appends to the same queue; it never executes anything. No reentrancy,
   because the tick and the slot cannot run concurrently on one thread.
3. **MCP adapter out of process**, translating MCP framing to rung 1 or 2.
   The transport then cannot touch the Station even in principle.

Rung 1 alone retires the highest-risk item in the programme. Note also that the
re-validation-at-drain property survives all three rungs unchanged, which is
what makes them safe.

## 5. The revised sequence

Phases 0–2 are the change of ordering; 3 onward largely re-sequences the
existing plan with its corrections folded in. Every phase ends with `make check`
green and lands its GLOSSARY rows with its code.

### Phase 0 — Close the write path *(days, no new subsystem)*

Nothing here is about agents. All of it is a live hazard today.

1. Mirror `DriverBench.call()`'s three checks in `Station.execute_vi_action()`:
   refuse an underscore-prefixed name, refuse a method without `_is_control`,
   and take an `allowed_scope` keyword defaulting to `"measurement"` exactly as
   `send_measurement_commands()` does.
2. Declare `control_limits` for excitation current on the DC-mode, delta-mode
   and lock-in VIs, populated from a `max_source_current_A` init param, copying
   `TensormeterRTM2MeasurementVI`'s pattern verbatim — and add the init params
   to every shipped config, including the two `init_params: {}` entries on the
   production setup.
3. **The coverage conformance test** (§6.1). This is the highest-leverage single
   change in the audit: it converts the control-validation standard from
   "enforced if declared" to "declared or explicitly exempted with a rationale".
4. Make the envelope bind the direct action path, so a bounded VI's setpoint
   controls are refused outside the envelope whether they arrive as a `Target`
   or as an action.
5. An unconditional S0 path: a public `Orchestrator.emergency_standby(reason)`
   that stops ramps and stands everything down on the caller's stack (the
   precedent `stop_ramp()` already sets), carved out of the admission predicate.
   Today `_enter_emergency()` is private and `standby_all` is refused in
   EMERGENCY — the one state where making the machine safe matters most.
6. Populate the envelope: an editor in the Start Experiment dialog, pre-filled
   from each system VI's config limit so the physicist narrows rather than
   composes from nothing. A safety mechanism nothing ever activates is not a
   safety mechanism.

**Exit:** a sim test asserting that a non-`@control` name, a private name, an
out-of-scope capability, an out-of-limit current and an out-of-envelope setpoint
are each refused on the direct action path with a distinct reason; the coverage
test passes with an explicit, documented exemption list.

### Phase 1 — Make the data honest *(days)*

Independent of Phase 0 and of everything after it. Worth doing for the physics
alone.

1. Fix the frozen-stale defect: write the `stale` dict back into
   `_last_known_state` on a failed poll, and emit a companion `{vi_name}_stale`
   numeric column from `last_state_flat()` so per-point staleness lands in the
   data, not only in a JSON snapshot.
2. `Station.provenance_snapshot()` → `/metadata/station_provenance`: per VI its
   class, `vi_type`, `init_params` (which is where every calibration constant
   and every limit already lives), driver aliases, classes and addresses, the
   live `get_idn()` string captured once at run start, and a `config_fingerprint`
   hash. This is LAP's `calibrationRef` and instrument identity in one object,
   and it makes a wrong constant recoverable months later.
3. `/metadata/run_id` and `/metadata/run_status` at close, so an aborted
   40-of-101-point run is distinguishable from a completed 40-point one without
   opening `experiment.json`.
4. Unit and `uncertainty_kind` as HDF5 dataset attributes, reusing the
   self-describing pattern the raw-block `axes`/`channel_names` attributes
   already establish.
5. Amend `ParamSpec.unit`'s docstring: it stops being "GUI concern only" the
   moment it is written into a data file, and leaving that sentence in place is
   how units stayed cosmetic.

**Exit:** a file written by this build can be interpreted with no access to the
running process and no access to the config repository.

### Phase 2 — Declarations and the refusal vocabulary *(1 week)*

This is the existing plan's Phase 1, widened. The widening is the point: the
plan scopes "vocabulary" to descriptive metadata and treats refusal reasons as a
Phase 2 detail, but a refusal vocabulary is vocabulary, it is the same kind of
work (declarations plus a conformance test), and an agent needs it just as
early.

1. `@monitored(unit=, description=)` in the dual bare/parametrized form
   `@control` already implements; fill in all 34 call sites.
2. `hazard=` per D2, with the category defaults and the minimum-floor test.
3. `params=` ParamSpecs on the remaining bare controls — but first override
   `control_param_specs()` on `MeasurementInstrumentBase` to serve
   `measurement_parameters` for `initiate_measurement` and every
   `reading_setters` target. That closes roughly ten undeclared controls with
   zero new declarations and removes a standing duplication hazard where the
   same parameter is described twice in two disjoint attributes.
4. `core/units.py`: a closed unit vocabulary plus a quantity-kind map, stdlib
   only, in the shape of `conditions.py`/`availability.py`, earning contract C16.
5. `RefusalCode` + a frozen `ActionVerdict` per D3; structured fields attached to
   `CryoSoftSafetyError` at both the limit wrapper *and* the interlock guards
   (the interlocks are the refusals an agent most needs to re-plan around);
   `_manual_action_admissible()` returns a typed `Admission` instead of
   `(bool, str)` — two call sites, zero behaviour change.
6. Enforce `ParamSpec.min/max/choices` on the submission path. They are checked
   today only for reading-loop value lists; `run_procedure()` accepts
   `field_steps=1` or `field_steps=10_000_000` without complaint.
7. `capability_spec(method_name)` on the VI base: the one accessor that merges
   the five channels a control's metadata is currently spread across, so no
   consumer re-implements the merge.

### Phase 3 — Observability that can be trusted *(days)*

The existing plan scores M2 "built — this module needs nothing". That is its one
clearly wrong call, and the reason is specific: the record carries no time.

1. Add `ts`, `seq`, `schema`, `run_id`, `experiment_id` and `setup` to the
   per-tick status record, all additive.
2. Emit a record every tick even when monitoring is off, so silence in
   `status.jsonl` unambiguously means the process is not ticking.
3. `--max-age` on `troubleshoot status`, defaulting to a few tick intervals,
   exiting non-zero on a stale log. Today the `troubleshoot-runtime` skill
   instructs an agent to gate on that exit code, and a three-day-old record from
   a crashed process yields a confident "RAMPING, ~1400 s to target" and exit 0.
   `check_trend_store_live` is the working template for the fix, in the same
   package.
4. Tail from the end and walk rotated files instead of reading the whole log.
5. `run_data.jsonl` — one line per datapoint with `run_id`, sweep index and the
   scalar columns. No agent-readable channel carries the measured quantity while
   a run is in progress, so no closed loop on physics is possible today, only on
   ramp progress. This is one line in an already-guarded tick block.
6. Three read-only Orchestrator accessors — `active_run_manifest()`,
   `operation_snapshot()`, `readiness()` — that free the human-in-the-loop
   handshake from the Qt widgets that currently compute it.

### Phase 4 — Authority and accountability *(1 week)*

The (B) tier of LAP's split, done without cryptography.

1. An `Actor` value on every public entry point, defaulting to a human sentinel
   so no GUI call site changes; on the run manifest, on `RunRecord`, on queued
   entries, and — the one that matters — on `StepRecord`, so an agent's
   self-confirmation of a physical step is no longer byte-identical to the
   physicist's.
2. `params_digest` on an operator confirmation: a hash over the canonicalised
   parameters plus the VI and method name. This is the *one* property of LAP's
   JWS token worth keeping, and the reason is not security — it is that the run
   record must be able to answer "was that 10 µA the value the human agreed to,
   or the 10 mA the agent retried with after the first reading looked noisy".
3. Attendance becomes load-bearing, pushed **down** as a value via
   `Orchestrator.set_attendance()`, mirroring `set_experiment_envelope()`
   exactly. The naive implementation — a gateway reading `ExperimentRecord` —
   cannot be reached from the enforcement point: contract C12 forbids anything
   below the GUI from importing the session layer, and the envelope already
   established the correct pattern for a session-owned policy enforced at the
   single writer.
4. Hazard classes gate against the envelope, attendance, and a standing grant.
5. Class (C) bounds: `max_duration_s` on a run (failing the run, not the
   station — the blast radius is one run), a required `timeout_s` on every
   `Gate`, and a resource budget on the `ExperimentEnvelope` rather than on a
   lease. The unit of cost here is the sample and the scientific question, which
   is exactly what an experiment is; a budget scoped to a lease resets every
   time an agent releases and re-takes, which is precisely the loop that burns
   the cryogen.
6. `agent_actions.jsonl` and `verdicts.jsonl`, following the established
   append-only JSONL discipline, and `actor`/`request_id`/`config` added to the
   existing troubleshoot transcript so the two audit trails join.

### Phase 5 — The in-process gateway, `cryosoft.ctl`, and `resolve()` *(1.5 weeks)*

Mostly composition of Phases 0–4, which is the argument for this ordering. Two
additions the existing plan does not account for:

- **The C11 blocker.** The planned `session/gateway/` cannot discover or build a
  procedure: contract C11 forbids `cryosoft.session` from importing
  `cryosoft.procedures` or `cryosoft.gui`, and `discover_procedures()` lives in
  `gui/procedure_discovery.py`. Move discovery to `cryosoft/procedures/registry.py`
  (legal under C6) and leave the GUI module as a re-export. Note also that
  operations are not covered by `run_builder`'s headless path — they are built
  by closures defined inline in the GUI — so the operations, which are precisely
  the runs carrying `operator_ack` steps, have no headless construction path at
  all today.
- **`resolve()` — and LAP has this backwards.** LAP puts `intent.resolve` on the
  Instrument Agent, i.e. the server holds the LLM. Here the server is the
  Orchestrator, whose defining constraint is that nothing in the tick path may
  block; an LLM call is seconds to minutes. So invert it: CryoSoft's `resolve()`
  is a **pure deterministic function** producing a frozen `Proposal`
  (`resolution_id`, procedure, fully-filled params, which values were defaulted
  and from what source, which bounds were checked, a duration estimate, and the
  clarifications it could not resolve) from ParamSpecs, `control_limits`, the
  envelope and cached state. The natural language belongs entirely to the client
  agent, which reads the StationCard and the Proposal. This is strictly better
  than LAP's arrangement and it is forced on us by the tick.

`cryosoft.ctl` is a new top-level leaf beside `main.py` and `troubleshoot/` with
its own contract pair — not a subcommand of `troubleshoot`, which C10 keeps
deliberately unable to reach the Orchestrator.

### Phase 6 — Live write path *(1 week)*

D4 rung 1, then rung 2. Every acting skill this repository ships today requires
the app **closed** because serial instruments are exclusive-open; the only skill
that works live is read-only through a log file. Closing that is what turns an
agent from a commissioning tool into an operator.

Land the cross-process runtime lock with it: a holder file written at app
startup that every bus-touching `troubleshoot` subcommand checks, with heartbeat
staleness as the expiry so a crashed app self-releases. This is the one place in
the whole system where a genuine lease is warranted — two *processes* really can
drive the same GPIB instrument, and today the only barrier is a sentence in a
README. Note the `diagnose-connections` skill ships standalone pyvisa scripts
outside `cryosoft/` entirely; they need the same check or the lock has a hole.

### Phase 7 — GUI surfaces and the embedded assistant

Unchanged from the existing plan's Phases 6–7, with one re-ordering already
applied: the envelope editor moves to Phase 0, because a permission model whose
bounds are never set is decoration.

## 6. The standards that make it stick

The most durable output of this programme is not the gateway. It is the set of
conformance tests that fail the moment someone adds a VI, driver, procedure or
config an agent could not safely operate. Ordered by leverage:

1. **`test_every_numeric_control_param_is_bounded_or_exempt`** — every
   float-annotated `@control` parameter on every discovered VI appears in
   `control_limits` or in a class-level `unbounded_controls` exemption set whose
   entries each require a one-line rationale in the declaring class's docstring.
   *Writable today as an expected-failure listing the current offenders.* This
   is the test that would have caught §2's critical.
2. **`test_capability_declares_hazard_at_or_above_its_category_floor`** — the
   mislabeled-card threat, mechanically closed.
3. **`test_monitored_field_is_described`** — every `@monitored` method declares a
   unit (for numerics) and a non-empty description.
4. **`test_declared_units_are_in_the_vocabulary`** — over every reachable
   `ParamSpec` and monitored declaration, against `core/units.py`.
5. **`test_control_param_specs_touches_no_hardware`** — the static/live card
   split of D1, enforced rather than hoped for.
6. **`test_monitored_fields_return_json_safe_scalars`** — built stations over the
   sim configs; today nothing asserts a monitored value survives the flat-state
   and trend paths, which drop non-scalars silently.
7. **`test_execute_vi_action_refuses_non_control_names`** — Phase 0's checks,
   asserted over every discovered VI.
8. **`test_conformance_discovery_is_non_empty`** — parametrized over the
   discovery helpers. Several can return `[]` and silently disable whole rows of
   the suite; a conformance harness that can vacuously pass is the one failure
   mode that invalidates everything above it.
9. **`tests/test_docs_conformance.py`** — every package folder has a README with
   the seven required headings; every shipped config is named in
   `configs/README.md` (fails today: `12t-cryo`, the production setup, is
   absent); no file under `cryosoft/` cites a plan document. That last one has
   nine violations today — `main.py:102, 219`, `gui/procedure_window.py:76`,
   `gui/monitor_window.py:153, 1029, 1182`, `gui/session_dialogs.py:33`,
   `session/models.py:531`, `session/store.py:278` all cite
   `docs/plans/session-tier-and-terminology.md`, and `session/models.py:331`
   plus `session/manager.py:323` cite "the agent-native plan", which was
   archived. The standard exists precisely because that citation now points at
   `docs/plans/archive/`.

Two harness repairs worth folding in, both found while auditing:

- **Sim-twin parity pairs by filename**, so the Mercury iPS driver — used by both
  production setups — is silently exempt. The exempt pair has in fact already
  drifted: `SimOxfordIPS120` exposes `reset_quench()` that the real driver does
  not have. Replace the filename convention with a declared `SIM_TWINS` map.
- **The production setup has no digital twin.** `12t-cryo` never reaches the four
  station-building conformance tests, and `DCSingleInstrumentVI` appears in no
  shipped config at all — four unbounded numeric control parameters that no test
  has ever constructed.

## 7. What to reject from LAP, and why

Stating these explicitly is as valuable as the adoption list, because each looks
like an obvious gap until the reason is written down.

- **Leased reservations** (`reservation.request/renew/release`, epochs,
  shared-read mode, pre-emption). Leases solve "many independent clients, one
  instrument, one of which may crash holding the lock". That problem does not
  exist in a single-process, single-threaded application. Building a lock
  manager would create a *second* source of truth for who owns `magnet_z`
  alongside the claims system. CryoSoft's re-validation at drain is strictly
  stronger than an epoch. Take the *expiry* idea only — already present in
  `acknowledge()`'s time-boxed window — and put a real lease only where there is
  genuinely no claim to extend: between processes (Phase 6).
- **JWS operator tokens, DIDs, a Safety Authority role, a `jti` redemption
  registry.** The approver is the person at the keyboard and the transport is a
  function call. Keep exactly one property — approval binds to the exact
  parameters — as a `params_digest` field, for reproducibility rather than
  security.
- **UCUM codes and QUDT quantity kinds.** The consumer here is an LLM agent, not
  a federation registry. An agent already knows what "T", "K", "A", "Ohm" and
  "%" mean; an unrecognised URI adds a lookup it cannot perform offline. A closed
  unit vocabulary in `core/units.py` with a conformance test gives the checkable
  property UCUM was there for, at a fraction of the cost.
- **Signed cards and signed results.** There is no trust boundary in a
  single-cryostat lab to justify a signing key, and introducing one adds a
  key-management failure mode with no attacker it defends against. The property
  actually needed is "which exact description was in force when this data was
  taken" — a content hash plus the existing config version history delivers it.
- **The whole L4 layer** — Lab Coordinator, Federation Registry,
  `registry.query`/`advertise`, cross-lab credentials. The seat LAP describes as
  "the only role that sees a workflow as a whole" is already occupied by the
  Orchestrator, and a coordinator beside it would be a second writer. Two L4
  *ideas* transfer as single fields rather than as roles: a per-experiment sample
  condition, and a resource budget. Multiple setups (`configs/<name>/`) do not
  change this: one app instance resolves exactly one config at startup.
- **`CalibrationExpired` as a mandatory rejection.** A hard date-based refusal
  would abort a good six-hour cryogen-burning run because a certificate lapsed,
  and would catch none of the errors that actually happen here. Split it:
  *recording* calibration provenance is unconditional (Phase 1) and is what makes
  the data honest; *checking* it becomes a structured finding from `resolve()` at
  run start, never a tick-path refusal.

## 8. Corrections to `agentic-instrumentation-framework.md`

Fold these in before its Phase 1 lands; several are anchors that have already
rotted.

- **`set_session_envelope()` is now `set_experiment_envelope()`**
  (`core/orchestrator.py:436`), and the type is `ExperimentEnvelope`. The
  violation messages inside `core/plan.py` still say "session envelope".
- **There are 15 import contracts, not 12.** The plan's proposed C13 for
  `data_reader` is taken (`core.conditions`); C14 and C15 are
  `core.availability` and `core.trend_checks`. The next free number is **C16**.
- **§2.2's verdict on M2 ("built — this module needs nothing") is wrong.** The
  record carries no timestamp, no run id and no schema version, and is written
  only while monitoring is on (§5 Phase 3).
- **§2.1's counts are stale.** 33 `@control` declarations, 8 with `params=`; 34
  `@monitored` declarations. The named list of ten bare controls is incomplete.
  The sweep-axis count is three per axis, not six total, and
  `test_procedure_parameter_has_description` misses them because it iterates the
  three explicit dicts rather than the merged union.
- **§2.4 is right that `Station.execute_vi_action()`'s return value is
  discarded, but cites `core/station.py:964-980`; it is at `:1585-1601`** (that
  range is `Station.conditions()`).
- **The plan predates commit `de817f7`** ("Give every refused lifecycle action an
  explicit verdict"), which fixed six silent refusal paths. At least four remain
  — `acknowledge_fault` with no fault, `acknowledge()` with nothing held,
  `submit_global_action` with an unknown action, `set_scanner_enabled`. Adopt the
  commit's *completeness rule* as a standard; do not adopt its signal-only
  mechanism, which is the one choice an agent client is measurably worse off for.
- **`RunFaultCode`'s stability contract points at `resources/mcp-compatibility.md`,
  which does not exist in the repository.** Move it into `core/README.md`,
  matching how `troubleshoot/README.md:36-38` states the same guarantee.
- **§2.7's probe-run plumbing note stands and gets worse:** operations have no
  headless construction path at all, not merely a second one.

## 9. Deferred, with reasons

- **Sample chain-of-custody.** LAP's own limitation 5 concedes it depends on
  barcodes, RFID and human procedure. The cheap high-value version — record on
  each `RunRecord` the run id of the most recent `sample_load` operation in this
  experiment, which the servicing log already has — turns "what thermal history
  did this sample have" from unanswerable into a lookup. A `SampleRecord` store
  with a stable `sample_uid` is worth building eventually (a sample is a
  free-text string in a `QLineEdit` today, and its identity does not survive
  across experiments), but it is a session-layer feature, not a fence.
- **Probe runs and duration estimates.** Both stand as specified in the existing
  plan's Phase 2, moved behind `resolve()` which is where their consumer lives.
  Do not adopt LAP's `estimatedDuration` as a *declaration* — for a swept
  measurement, duration is a function of the parameters, not a constant of the
  capability. Compute it.
- **`safe_shutdown()` and driver error-queue verification** (the existing plan's
  Phase 3). Unchanged and still worth it; independent of everything above and
  pays for itself in human debugging time. Phase 0 reduces its urgency by
  bounding what can be commanded in the first place, but it does not replace it.
- **Remote (non-localhost) access, notifications, headless station mode, the
  eLab publishing track.** Unchanged.
