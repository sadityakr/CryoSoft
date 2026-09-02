# Declare · Enforce · Check

**A framework for agent-operable control software on home-built,
multi-instrument scientific setups.**

Version 0.1 — 2026-08-08. Status: specification, with one reference
implementation (CryoSoft) at roughly half conformance.

---

## 0. How to read this

The framework has four parts, and they are ordered by how expensive they are to
change later:

1. **Four architectural invariants** (§1) — properties the system must have.
   Retrofitting any of these means restructuring; decide them before the first
   driver.
2. **One repeatable mechanism** (§2) — Declare → Enforce → Check. Everything
   else in the framework is an instance of it.
3. **Eight declarations** (§3) — what each layer must state about itself. These
   are additive; a system can satisfy them one at a time in any order that
   respects §4.
4. **What falls out for free** (§4) — the agent-facing surfaces you do *not*
   build, because they become projections of §3.

Every point carries its reasoning, because a rule whose justification is not
written down is a rule that gets exempted the first time it is inconvenient.
Where a claim is illustrated from the reference implementation it carries a
`path:line` anchor; those are examples, not requirements.

---

## 1. Four architectural invariants

### I1 — Single writer

**The rule.** Exactly one thread. One periodic tick drives everything. Every
write to hardware is serialized through one choke point. Long operations are
generators that yield one step per tick, never blocking loops.

**The obvious reason.** Instrument buses are stateful and their sessions are not
reentrant. Two writers interleaving commands on a GPIB line, or on a serial
instrument that serves one connection at a time, corrupt mode state in ways that
surface as wrong numbers rather than as errors. A shared bus with two writers is
a race condition with a physics consequence.

**The reason that matters for agents.** Single-writer turns mutual exclusion
from a *protocol* problem into a *structural* one. Every multi-client instrument
standard — SiLA, OPC-UA, LAP — has to invent leases, expiry, renewal, epochs and
conflict errors, because each assumes many clients can reach the hardware and
one of them might crash holding the lock. If there is exactly one writer, that
entire primitive is unnecessary: exclusion is not something you enforce, it is
something that cannot be violated.

And what remains is *stronger* than a lease. A lease can lapse while its holder
is still working; a claim held by a run cannot, because the claim and the run
are the same object with the same lifetime. In the reference implementation the
admission predicate is re-evaluated at **drain** time, not only at submit time
(`core/orchestrator.py:1172-1293`, checked at both `:1305` and `:2113`), so an
action queued under one ownership state and executed under another is caught
without any epoch counter. That is a property a distributed protocol has to work
for and this architecture gets by construction.

**What it forbids.** No second thread. No blocking call anywhere the tick can
reach. This has one consequence people try to route around and must not: **an
LLM call can never be in the tick path.** Inference is seconds to minutes.
Everything in §4 that involves natural language is therefore a pure function on
one side and a client on the other.

**The honest cost.** No hard real-time inner loops. Microsecond servo control,
fast lock-in feedback, pump-probe timing — anything whose deadline is shorter
than a tick — must live *below* the driver, in firmware or a dedicated
controller, and be commanded at the task level. State this in your own
documentation rather than discovering it during a commissioning run.

### I2 — Strictly downward dependencies, mechanically enforced

**The rule.** Layers are numbered, dependencies point only downward, and the
direction is enforced by import contracts that run in CI on every push. A
contract is never weakened to make something pass.

**The obvious reason.** It prevents the tangle. Everyone agrees with this and
almost nobody enforces it, which is why almost everyone has the tangle.

**The reason that matters for agents.** A capability description is a
*projection* of the lower layers into a document. If a lower layer can reach
upward, that projection has cycles: what an instrument "can do" starts depending
on what is currently running, and there is no well-defined static description to
give an agent before it acts. Layer discipline is the precondition for
self-description, not merely good hygiene.

It also converts safety claims into checked facts. "The transport cannot touch
the instruments" is a code-review promise in most systems. Under a mechanically
enforced contract it is a build failure.

**The pattern that keeps it honest.** When a higher layer owns a policy that a
lower layer must enforce, do not reach up — **push the value down**. In the
reference implementation the per-experiment safety envelope is owned by the
session layer (L6) and enforced by the Orchestrator (L3), which may not import
L6; it is installed as a typed value through `set_experiment_envelope()`
(`core/orchestrator.py:436`). Every subsequent session-owned policy — attendance,
resource budgets, standing grants — follows that same channel. This is not
ceremony; it is what keeps the *enforcement point* single even though the
*ownership* is layered.

**The discipline.** Never grant an exemption to make a test pass. Propose the
architectural change instead. A weakened contract is a lie that compounds,
because the next author reads the contract, not the exemption.

### I3 — Physics in code, setup in config

**The rule.** Two kinds of fact, two homes.

- **Code**, on the class beside the check that knows it: hazard class,
  reversibility, side effects, interlock logic, capability semantics, the
  physics of a failure mode.
- **Config**, per setup: instrument addresses, numeric limits, calibration
  constants, per-rig ceilings, variable mappings.

**The test that resolves any argument.** *If two labs owned this identical
instrument, would this value differ?* If yes, it is config. If no, it is code.
The maximum current this sample tolerates differs. Whether energising a switch
heater across a PSU/coil current mismatch can quench the magnet does not.

**Why the split is about falsifiability, not tidiness.** A number hardcoded in
code is wrong for exactly one rig and is invisible — nobody greps source for
their own instrument's ceiling. A number in config is wrong in one file, in one
place, that a human reads when commissioning. That much is conventional.

The less obvious half is the reverse direction, and it is the one people get
wrong when they take "constants in config" too literally. A *classification* in
config can differ between four setups that own the same instrument, which makes
it **unfalsifiable** — no test can check YAML against physics. Put the
classification in code, next to the imperative interlock that already encodes
the same knowledge, and a conformance test can assert the two agree.

**The resolution: floor-and-raise.** Code declares the floor — what this
capability is, in any lab. Config may narrow it — what this rig, this sample,
this wiring tolerates — and may never widen it. A conformance test asserts the
direction.

**A failure mode this invariant is meant to catch.** Config can silently
*disable* a code-declared protection. In the reference implementation
`configs/12t-cryo/devices.yaml:199` sets `configured_externally: true`, which by
design bypasses that instrument's declared current and voltage limits because an
external vendor tool owns the excitation. That is a legitimate operating mode and
a documented one — but it means the declared limits are not in force, and any
self-description that reports them without saying so is lying to the agent.
**Config-side overrides of code-side declarations must be surfaced in the
capability description, not just honoured at runtime.**

### I4 — Simulation parity

**The rule.** Every real driver has a simulated twin with an identical public
API that models the instrument's physics *including its failure modes*. The
entire system is exercisable end to end with no hardware.

**Three reasons, in increasing importance.**

1. CI can run. Obvious and true.
2. A wrong command sequence fails in a test rather than on a magnet. The sim
   must therefore model refusals, not just returns: a source that silently
   ignores a write while a sweep engine is armed, an instrument that answers
   stale values after a mode change, a PSU that reports a quench.
3. **The decisive one.** An agent's first action against an unfamiliar
   capability is exploratory. If the only place to explore is real hardware, you
   cannot let an agent explore, and "agent-operable" reduces to "agent may
   execute a script a human already validated" — which is automation, not
   agency. Simulation parity is what makes *let it try* a safe sentence.

It also raises what conformance can assert. With a faithful sim you can check
**behaviour**, not just shape: pollute an instrument into a bad state and assert
every VI sharing it still initiates and returns valid readings.

**The failure mode to guard.** Parity that pairs real to sim *by filename
convention* drifts silently the moment a driver is named differently. Declare
the mapping explicitly and assert the pairing is total. In the reference
implementation this exact hole exists today: the magnet PSU driver used by both
production setups is exempt from the parity test because its sim twin has a
different name, and the exempt pair has in fact already diverged —
`sim_oxford_ips120.py:212` exposes a `reset_quench()` the real driver does not
have.

---

## 2. The mechanism: Declare → Enforce → Check

Everything in §3 is an instance of one three-part pattern. It is the whole
framework; the eight declarations are just where it is applied.

Every capability of the system is:

1. **Declared** — a machine-readable statement, on a decorator, a class
   attribute, or config.
2. **Enforced** — at exactly *one* choke point, by inheritance, so that an
   author cannot opt out by omission.
3. **Checked** — by an auto-discovering conformance test that fails the moment a
   new module violates it.

### Why all three, and why none is sufficient alone

**Declaration alone rots.** A docstring saying "keep this under 10 µA" is not a
bound. It is a hope with good formatting. The first author who has not read it
exceeds it.

**Enforcement alone is invisible.** A check buried in a method body protects
that method and teaches nobody. The reference implementation has a perfect
specimen: `Keithley6221.set_current()` unconditionally issues `:SOUR:SWE:ABOR`
and re-asserts autorange before every write (`drivers/keithley_6221.py:116-117`),
added after a commissioning incident in which leftover delta-mode state caused
DC writes to be silently rejected. The fix is correct, and its docstring
explains it at length. It is still undiscoverable *as a class of requirement* —
the author of the next current source has no way to learn that this category of
bug exists. Enforcement without declaration protects one instance and no
successors.

**Checking alone is archaeology.** A test that enumerates known modules
documents the past. It says nothing about the module added next week, which is
the only one you actually needed protection from.

Together they are **self-propagating**: a new module is covered the moment its
file exists, and the failure message tells its author exactly what to declare.

### Why enforcement must be inherited, not called

If enforcement is "remember to call `validate()` at the top of your method", it
will be forgotten — not by careless people, by tired people at 2 a.m. adding one
more instrument. Wrap it at class creation instead. In the reference
implementation `BaseVirtualInstrument.__init_subclass__` wraps every `@control`
method with limit enforcement at class-definition time
(`virtual_instruments/base.py:267`), so an instrument author cannot opt out
by omission — only by an explicit, visible, reviewable declaration.

**The principle: the default must be safe, and every deviation must be a written
act.** An unbounded parameter should require someone to type the word
"unbounded" and give a reason, not merely to forget.

### Why the check must auto-discover

A test that lists the modules it covers is a list, and lists go stale silently.
A test that walks the package, finds every driver, virtual instrument, procedure
and config, and parametrizes over what it finds **cannot** go stale. This is the
difference between a suite that describes the system and one that governs it.

Two corollaries that are easy to miss and expensive to skip:

- **Assert the discovery is non-empty.** A helper that returns `[]` because a
  package was renamed silently disables every test parametrized over it. A
  conformance suite that can vacuously pass invalidates everything built on top
  of it, so test the tests.
- **Test coverage, not just correctness.** This is the single most common
  mistake, and the reference implementation makes it: three conformance tests
  check that *declared* limits name real methods, are populated by every config,
  and actually reject out-of-range values — and nothing checks that *undeclared*
  ones do not exist. The enforcement wrapper reads `if limit_spec:`
  (`virtual_instruments/base.py:433`), so a capability with no declaration is
  simply never checked. The standard is "enforced if declared" and nothing forces
  declaring. Checking that what you declared is right is easy and comforting;
  checking that nothing escaped declaration is the test that actually protects
  you.

### The property this buys

> The answer to "did anyone remember to declare X on the new instrument?" is
> **"the build would have failed."**

That sentence is the entire value proposition. It is what makes the framework
survive its author's attention, a new student, a rushed week before a beam time,
and — the case it was built for — an agent adding an instrument on its own.

---

## 3. The eight declarations

Each is stated as: **what** is declared, **where** it is enforced, **what** the
conformance test asserts, and **why** — what specifically breaks without it.

### D1 — Capability and parameter schema

**Declare.** For every callable capability: its parameters with type, unit,
bounds, allowed values, and a prose description of what it physically does.

**Enforce.** An inherited wrapper that rejects an out-of-range value *before any
hardware command is sent*.

**Check.** Every numeric parameter that reaches hardware is bounded **or**
appears in an explicit exemption set whose entries each carry a written
rationale.

**Why.** This is the agent's entire view of the machine. It cannot see a front
panel, cannot infer intent from a blinking LED, and cannot be trusted to
remember what it left armed. Everything it knows arrives as structured text.

A parameter with no unit is a number with no meaning. A parameter with no bound
is an invitation. The asymmetry that makes this declaration the first one: for a
human, a missing tooltip is a cosmetic defect; for an agent, a missing schema
field is a functional one — and because the human notices only the cosmetic
version, this gap is invisible until an agent is connected and it is too late to
be surprised.

**The coverage/correctness distinction is the whole point of this declaration.**
See §2. If you implement one conformance test from this document, implement this
one.

**Design note — prose is a declaration.** Require a non-empty description and
enforce it. "What does this physically do" is the field an agent grounds
against, and it is the field that gets left empty because the method name looked
self-explanatory to the person who wrote it.

### D2 — Hazard class

**Declare.** A closed, small vocabulary of hazard levels, per capability,
defaulted on the category base class so every instrument of a role inherits the
right classification.

**Enforce.** In the admission decision, against the envelope (D5), the
attendance state, and any standing grant.

**Check.** A **minimum floor per instrument category**: no capability may
declare a hazard below the floor its category base class sets.

**Why.** "What does this cost if it goes wrong" is orthogonal to "is this value
in range". A perfectly in-range value can still be an action you want a human to
have agreed to. Without this axis, a capability that can quench a magnet and one
that changes a display refresh rate are **byte-identical in every
machine-readable field** — same decorator, same shape, same enforcement. Any
authority decision then has to be made from a hand-maintained table in a
gateway, and a hand-maintained table is a list, and lists rot (§2).

**Why the floor test matters more than the classification.** The one threat a
capability description cannot defend against by construction is a *correctly
declared lie*: a capability that understates its own hazard, through haste or to
reduce friction. Signatures do not catch it, because the declaration is
genuinely the author's. A per-category floor catches it mechanically. This is
the framework's answer to a problem the protocol literature can only recommend
against.

**Design note — keep it orthogonal to dispatch scope.** If your system also
partitions capabilities by *which kind of plan may contain them*, that is a
different axis and merging the two is expensive to undo. A capability can be
routine to dispatch and genuinely hazardous: in the reference implementation
`initiate_measurement` must remain dispatchable by any reading loop — it runs at
every sweep point — while being the capability that energises a source into the
sample. Two axes, two enforcement points, two vocabularies.

**Design note — set the taxonomy so the top rung is rare.** The correct use of
the highest hazard level is "actions the software will not perform under any
standing grant". If the top rung fires several times a night, operators learn to
approve without reading, and the declaration has made things worse than no
declaration. Rarity is a design requirement, not an outcome.

### D3 — Telemetry semantics

**Declare.** For every monitored quantity: its unit, its physical kind, and a
description. Units drawn from a **closed vocabulary** the system defines.

**Enforce.** Nothing at runtime — this declaration is read, not obeyed.

**Check.** Every monitored field is described and carries a unit drawn from the
vocabulary; every monitored value is a JSON-safe scalar matching its declared
type.

**Why.** The live channel is what a closed loop reads. A bare float that might
be Kelvin or Tesla is not observability — it is a number an agent will guess
about, correctly most of the time, which is the worst possible failure rate.

**Why a closed local vocabulary rather than an international code system.** The
consumer here is a language model, not a federation registry. It already knows
what `T`, `K`, `A`, `Ohm` and `%` mean; an unrecognised ontology URI adds a
lookup it cannot perform offline and buys nothing it did not already have. What
you actually wanted from a code system was the *checkable* property — "this unit
is one the system knows about" — and a thirty-line module with a frozen set plus
one conformance test delivers exactly that, at a cost proportional to the
benefit. Adopt the discipline, not the ontology.

**Design note — decide unit's home once.** A monitored quantity's unit is a
property of the measurement, not of the rig, so it belongs in code with the
declaration. This is easy to reverse on day one and painful once thirty call
sites carry it.

### D4 — Refusal vocabulary

**Declare.** A closed enumeration of refusal reasons, derived from the refusals
your code actually produces, plus a structured payload per reason carrying the
facts an agent needs to re-plan.

**Enforce.** Every refusal site returns or raises a structured object. The
human-readable sentence is **derived from** the object, never the reverse.

**Check.** No public entry point returns having done nothing and said nothing.

**Why.** An error is a re-planning instruction. Compare:

> `Cannot control magnet_z: claimed by running FieldSweep`

with

> `{code: CLAIMED_BY_RUN, vi: "magnet_z", holder: "FieldSweep", retry_after_s: 40}`

The first makes an agent parse prose written for a status bar, and it will get
it wrong the first time someone rewords the message. The second makes it wait
forty seconds and retry. Same information, one of them actionable.

**Why this is cheap, and why that is surprising.** Almost every system with this
gap already *has* the structured facts at the refusal site and destroys them by
formatting. The reference implementation holds the parameter, value, low bound,
high bound and limit name in scope and interpolates all five into an f-string
(`virtual_instruments/base.py:453-459`); it holds the full condition object in
the admission predicate and returns `(False, str)`. **This is a serialization
problem, not a capability problem**, which is why it costs days rather than
weeks and why it is worth doing early.

**Design note — derive the vocabulary from your code, never from a spec.** A
taxonomy copied from someone else's protocol will contain members you never
raise and omit the ones you raise constantly. Enumerate your actual refusal
sites first — the admission rules, the limit wrapper, the interlock guards, the
scope check, the envelope check, the availability policy — and let the
enumeration be the deliverable. The enum is just its shape.

**Design note — string members, not integers.** Numeric error codes exist in
wire protocols because the wire format demands an integer. Absent that
constraint, a string member is JSON-ready, greppable in a log, readable in a
banner, and trivially mapped to a number later if a transport ever needs one.

**Design note — refuse loudly, never silently.** The worst failure mode in this
class of software is silent rejection: the agent believes it set a current, the
instrument disagrees, and everything downstream is fiction. This applies at
three levels and all three need auditing — the bus (an instrument that accepts
and ignores), the dispatcher (a command for an unknown method quietly skipped),
and the API (an entry point that returns `None` having done nothing).

### D5 — Envelope

**Declare.** Per-experiment bounds, narrower than the instrument's own, on the
quantities that can damage what is currently mounted.

**Enforce.** At the single writer (I1), on **every** write path, checked both
against submitted setpoints *and* against live readings each tick.

**Check.** The envelope binds every path that can move hardware — including
direct capability calls, not only planned ones.

**Why an envelope at all, given D1 already bounds parameters.** Instrument
limits protect the *instrument*, and they cannot protect the *sample*, because
the sample changes weekly and the instrument does not. A magnet good to 9 T with
a device on it that dies above 2 T needs a second bound with a different
lifetime and a different owner. One is a property of the rig; the other is a
property of this week's experiment.

**Why it must bind the human too.** This is the design point most often gotten
wrong, and the reasoning inverts the intuition. A bound that applies only to
agents is a bound that gets circumvented by a human doing the agent a favour —
and worse, it encodes the assumption that the human is the safe operator, which
is exactly false at hour six of an unattended night. Enforce at the single
writer and it binds every writer by construction. It changes your error UX;
accept that.

**Why this is also the consent mechanism.** An envelope agreed once, at
experiment setup, is a *standing scoped grant*: bounded in value, bounded in
time by the experiment, attributable to the person who set it. The alternative —
prompting per action — generates so many prompts on an overnight campaign that
operators learn to approve without reading, which is worse than no prompt at
all. **Get informed consent once, with the parameters in front of the human, and
then let the machine hold the line.** Reserve per-action confirmation for the
rare top hazard rung (D2).

**Design note — an envelope nothing sets is not a safety mechanism.** Build the
editor with the enforcement, default it from the instrument limits so the
physicist narrows rather than composes from nothing, and warn when an experiment
opens without one. The reference implementation has a complete, correct,
tick-enforced envelope in which every construction in the repository is inside a
test.

### D6 — Attribution

**Declare.** An actor on every state-changing entry point and on every human
confirmation: who asked for this, in what role.

**Enforce.** At the entry points, defaulted to a human sentinel so existing
callers are unaffected.

**Check.** Every state-changing public entry point accepts an actor; every
recorded confirmation carries one.

**Why.** The question a run record must be able to answer is not "what happened"
but "**who decided**". Without an actor, an agent confirming a physical step is
byte-for-byte identical to the physicist confirming it, and the audit trail
cannot distinguish a human agreeing to 10 µA from an agent retrying at 10 mA
because the first reading looked noisy.

It is also the only defence against the failure mode that will end an
autonomous-operation programme faster than any hardware damage: **nobody can say
whether the anomaly in the data was physics or an agent.** One unexplained
feature in a dataset, one afternoon of arguing about it, and the agent gets
switched off — not because it was unsafe, but because it made the data
unfalsifiable.

**The one property worth taking from cryptographic approval tokens.** Bind each
approval to a digest of the exact parameters it approved — the canonicalised,
unit-normalised parameter set plus the capability's identity — so that changing
any value invalidates the approval. **Not for security**: in a single-lab, single
trust domain the approver is the person at the keyboard and the transport is a
function call, so signatures, decentralised identifiers, replay registries and a
distinct signing authority are ceremony with no adversary. Take it for
**reproducibility**: an approval that survives a parameter change is not a record
of what was approved. Keep the digest, keep single use, keep expiry, drop the
cryptography.

**Design note — informed consent means human-readable.** Whatever is shown to
the human before they approve must carry the actual parameter values and the
declared consequence, never an opaque identifier. An approval flow that shows a
hash is a flow that trains people to click.

### D7 — Provenance

**Declare.** In the data file itself: units per column, the calibration
constants that produced the numbers, instrument identity and firmware, the exact
parameters and their digest, timing, the run's identity and terminal status, and
an explicit staleness marker per reading.

**Enforce.** At the data writer, unconditionally.

**Check.** A run file can be interpreted by a reader with no access to the
running process and no access to the configuration repository.

**Why.** A file that cannot be interpreted without the live process and the
config repo is not data — it is a cache with a long expiry. The sharpest form of
this in the reference implementation: the recorded field column is meaningless
without an amperes-per-tesla constant that lives only in a YAML file
(`configs/12t-cryo/devices.yaml:89`) and is never written into the file. Edit
that constant and two datasets that disagree by twenty percent in field are
**byte-indistinguishable in their metadata**. This is not a hypothetical; it is a
one-line config edit away at all times, and it is exactly the "physically valid
but scientifically invalid" failure the protocol literature names.

**Why staleness belongs here.** A frozen reading written without a marker is
worse than a missing one, because it looks like data. If your polling layer
degrades gracefully by returning last-known values, that graceful degradation
must be *visible in the file*, or the graceful part is a lie. Audit this
specifically: it is a common defect and it hides in the difference between what
a poll returns and what its cache retains.

**Why calibration is declared and warned about, never auto-refused.** It is
tempting to make a lapsed calibration date a hard refusal. Do not. A date-based
refusal aborts a good six-hour cryogen-burning run because a certificate expired,
catches none of the errors that actually happen (a wrong constant, an un-nulled
phase, a mis-wired contact), and gets disabled within a month — and a disabled
check protects nothing. Split it: **recording** calibration provenance is
unconditional and is what makes the data honest; **checking** it is a structured
finding surfaced before the run starts, which a human may override and which is
recorded either way.

**Design note — the test is blunt.** Can someone interpret this file in five
years with no access to your machine? If not, the declaration is incomplete.

### D8 — Safe state

**Declare.** One idempotent, unconditional path per instrument to a known safe
idle state, and one system-wide entry point that invokes them all.

**Enforce.** Carved out of the permission model as a dedicated narrow entry
point — never by relaxing the general admission rules, which are correct as they
stand.

**Check.** The safe-state path is admitted in **every** state, asserted state by
state, including the error and emergency states.

**Why it may bypass the permission model when nothing else may.** Because of an
asymmetry no other action has: **the worst case of "make it safe" is that the
instrument was already safe.** Every other action has a bad worst case, which is
why every other action is gated. Write that asymmetry into the standard, because
it is the entire justification and without it the exception looks arbitrary and
will eventually be widened.

**The failure this prevents, stated concretely.** An agent notices it is doing
something wrong and tries to stand the system down — and is refused, because the
system is in the state where standing down matters most. The reference
implementation has exactly this shape today: the emergency state refuses every
capability call for every instrument unless a human at the GUI unlocks an
override, and the bulk standby action is queued through that same gate. The
policy intent is right in the plan; the code makes the caller who most needs the
action the one who cannot have it.

**Design note — why it must be a separate method, not a flag.** A general
predicate with a "this is an emergency" bypass parameter is a predicate whose
bypass will be passed by something else within a year. A dedicated method that
can do only one thing — stop motion, stand instruments down — is safe to admit
unconditionally precisely *because* its blast radius is fixed by its
implementation rather than by its caller.

**Design note — it must report per-instrument outcomes.** "Make everything safe"
that swallows per-instrument failures and returns nothing leaves the caller
unable to learn which instrument is still live. That is the one case where a
best-effort loop must still produce a verdict per target.

---

## 4. What you get for free

The point of §3 is that the agent-facing surfaces stop being things you design
and become things you project. Once the eight declarations hold:

**The capability description is generated.** You never write it and it cannot
drift, because it is a projection of the same declarations that drive
enforcement and the user interface. Two design notes:

- **Choose one unit of description, and let the architecture choose it.** If one
  writer owns all hardware, the honest unit is one *setup* description with a
  flat capability list addressed by `instrument.capability` — the identifiers
  your write path already takes. One description per instrument advertises
  independent addressability that the single-writer invariant forbids, and
  double-declares hardware shared between two logical instruments.
- **Split static from live.** The static half — types, units, bounds, hazards,
  choices — must be derivable with no hardware access, or `describe()` from a
  command line puts bus traffic outside the tick. The live half — current
  values, availability, active conditions — comes from the polling cache, never
  a fresh poll. Make "capability description touches no hardware" a checked
  standard, not an intention.

**Actions become answerable.** A refusal is already a structured object (D4), so
a request/response verdict is a matter of correlating it to a request identity
rather than of inventing an error model.

**Goal grounding becomes a pure function.** A resolve step maps a partially
specified goal onto a fully specified, bounds-checked proposal — filled
parameters, the defaults it chose and where they came from, the checks it ran,
an estimated duration, and the questions it could not answer. It reads
declarations; it contains no model.

This is the place where this framework **disagrees with the protocol
literature outright**, and the disagreement is worth stating on its own terms.
LAP places its natural-language grounding method on the *instrument* side — the
server holds the model. For any system with a control loop that is inadmissible:
the server is the thing that must never block, and inference is seconds to
minutes (I1). Invert it. The natural language belongs entirely to the **client
agent**, which reads the capability description and the proposal; the instrument
side stays deterministic, replayable, and testable without a model in the loop.
The result is strictly better than the protocol's own arrangement, and the
architecture forced it.

**The gateway becomes a thin projection.** A transport that carries already-typed
requests to an already-structured verdict surface is a week of work.

**And the ordering follows.** Everyone builds the gateway first, because it is
the visible part and it demonstrates well. It is the part that should be built
**last**. A transport over incomplete declarations is a fast way to give an agent
confident access to bad information — and confidently wrong is the one failure
mode that survives review, because it looks like it worked.

---

## 5. Conformance: the deliverable

The framework ships as a harness, not as a document. A specification asks for
compliance and receives it for about a month; an auto-discovering conformance
suite asks for nothing and receives it forever.

The minimum suite, ordered by leverage:

| # | The test asserts | Declaration |
|---|---|---|
| 1 | Every numeric parameter reaching hardware is bounded or explicitly exempted with a rationale | D1 |
| 2 | No capability declares a hazard below its category floor | D2 |
| 3 | Every monitored field is described, with a unit from the closed vocabulary | D3 |
| 4 | Every monitored value is a JSON-safe scalar matching its declared type | D3 |
| 5 | No public entry point returns having done nothing and said nothing | D4 |
| 6 | The envelope binds every write path, direct calls included | D5 |
| 7 | Every state-changing entry point accepts an actor | D6 |
| 8 | A run file round-trips through a reader with no process and no config repo | D7 |
| 9 | The safe-state path is admitted in every state | D8 |
| 10 | The capability description touches no hardware | §4 |
| 11 | Every real driver has a declared sim twin with a matching public API | I4 |
| 12 | Every discovery helper returns a non-empty result | §2 |

Test 12 is not padding. A helper that silently returns an empty list disables
every test parametrized over it, and a conformance suite that can vacuously pass
invalidates everything above it. Test the tests.

---

## 6. The skeleton

What a lab adopting this framework clones:

- the layered package structure, with the import contracts already written
- base classes carrying the eight declarations as requirements
- the conformance suite — which, because it auto-discovers, **runs on an empty
  repository** and reports everything not yet declared
- the simulation standard and one worked real/sim driver pair
- one build target that runs lint, contracts and tests, wrapped thinly by CI so
  the gate cannot diverge between local and remote

The adoption path: clone, run the build, read the failures as a to-do list,
implement drivers, instruments and procedures for your rig, and stop when it is
green. **Green means agent-operable** — not because someone read a document and
complied, but because the harness declined to pass until it was true.

CryoSoft is the reference implementation, not the framework: a 12 T cryostat
with nine real drivers, four setups, seven layers, fifteen import contracts and
roughly 1,900 tests. It satisfies I1–I4 and roughly half of D1–D8; the audit in
`../plans/agent-operative-architecture-audit.md` states precisely which half and
sequences the rest. A worked example that is honest about what it has not
finished is more useful than one that claims completeness, and the gap list is
itself an argument for the framework: every item on it went unnoticed for as
long as it was not machine-checked.

---

## 7. Relationship to the Lab Agent Protocol

LAP (Zhu et al., arXiv:2606.03755v1, June 2026) addresses the same edge and is
the closest neighbour to this work. It is a v0.1 design specification with no
implementation, written for federated, multi-instrument, multi-laboratory
automation. Engaging it precisely is worth more than dismissing it.

| LAP element | Here | Reasoning |
|---|---|---|
| Three-way safety split — device-physics / authorization / workflow-integrity | **Adopted wholesale** | The sharpest idea in the paper. It is a question about *where each check can be answered*, and it maps directly onto layered enforcement: D1 is device-physics, D2/D5/D6 are authorization, D7 and run-level bounds are workflow-integrity. |
| Capability card — generated, machine-checkable | **Adopted, re-scoped** | One description per *setup*, not per instrument (§4). Single-writer makes per-instrument addressability a false advertisement. |
| Hazard classification | **Adopted, extended** | With a per-category *floor* enforced by conformance — which closes the mislabeled-capability threat the paper concedes it cannot solve. |
| Errors as re-planning data | **Adopted, re-shaped** | String enum members rather than integers; the taxonomy derived from actual refusal sites rather than copied (D4). |
| Physically honest results | **Adopted, simplified** | Units, calibration, uncertainty and provenance are mandatory (D7); an international unit ontology is not, because the consumer is a model, not a registry. |
| Confirm-before-actuate | **Adopted, inverted** | The proposal step is a pure deterministic function on the instrument side; the model lives in the client. The paper puts it the other way, which a control loop cannot afford (§4). |
| Approval tokens bound to parameters | **One property kept** | The parameter digest, for reproducibility. Signatures, identifiers, replay registries and a separate signing authority are dropped: no adversary, no transport, no second party (D6). |
| Leased reservations with expiry and epochs | **Rejected** | Solves "many clients, one instrument, one may crash holding the lock". Single-writer removes the problem and yields a stronger property (I1). A lease *between processes* is warranted; between callers it is not. |
| Mandatory rejection on lapsed calibration | **Rejected as a rule, adopted as a finding** | A hard date-based refusal aborts good runs, catches none of the real errors, and gets disabled. Record unconditionally; warn before the run (D7). |
| Lab Coordinator, Federation Registry, cross-lab credentials | **Rejected** | The seat described as "the only role that sees a workflow as a whole" is already occupied by the single writer, and a coordinator beside it would be a second writer. Two ideas transfer as *fields* rather than roles: a per-experiment sample condition and a resource budget. |

**The structural difference, stated plainly.** LAP standardizes the wire between
an agent and an instrument, and its cost is amortised across a field: many
laboratories, many instruments, eventually vendors shipping conformant
interfaces. That is the right shape for the problem it names.

This framework standardizes the **declarations inside one instrument stack** and
makes them machine-checked. Its cost is paid once by one lab and its benefit is
that the description cannot rot. For a home-built rig — no vendor cooperation,
no federation to join, one physicist, one trust domain — that is the binding
constraint, and the wire was never it.

The two are compatible. A system that satisfies D1–D8 can expose any wire
protocol as a projection, in a week, whenever one is worth having. A system that
adopts a wire protocol without D1–D8 has an agent-shaped interface to software
that still does not know what it can do.
