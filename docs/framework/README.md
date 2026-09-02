# The framework

## Purpose

A framework for building agent-operable control software for **home-built,
multi-instrument scientific setups** — the rigs assembled in one lab, from a
mix of vendors and eras, that no vendor will ever ship an agent interface for.

This folder holds the framework itself, stated independently of any particular
instrument. CryoSoft — the cryostat operating system in `cryosoft/` — is its
first and reference implementation, not the framework.

## The thesis

> Agent-operability is not a protocol you bolt on top. It is a property that
> emerges when every layer of an instrument stack **declares** what it can do,
> **refuses** in structured terms, and is **machine-checked** for both. The wire
> protocol is the last five percent, and the cheapest part.

The prevailing approach to the agent↔instrument problem is to standardize the
wire: define a protocol, publish a schema, and wait for adoption. That works
where there are many instruments, many labs, a federation to join, and vendors
with a commercial reason to comply. A home-built rig has none of those, and for
it the wire was never the bottleneck. The bottleneck is that **the software does
not know what it can do** — its own capabilities are implicit in method
signatures, its limits are implicit in whoever remembers the config, and its
refusals are English sentences. Fix that and any transport is a week of work.
Skip it and the transport gives an agent confident access to bad information.

## What is in here

| Document | What it is |
|---|---|
| [`declare-enforce-check.md`](declare-enforce-check.md) | The framework specification: four invariants, one repeatable mechanism, eight declarations, each with the reasoning for why it is there and what breaks without it. |

Related, in `../plans/`:

- [`agent-operative-architecture-audit.md`](../plans/agent-operative-architecture-audit.md)
  — the audit that produced this framework, including what a real implementation
  looks like when it has satisfied roughly half of it, and the sequencing to
  finish.
- [`agentic-instrumentation-framework.md`](../plans/agentic-instrumentation-framework.md)
  — the nine-module capability framework this refines. That document asks *what
  modules an agentic system needs*; this one asks *what makes them stay true*.

## Who this is for

Someone who has a working, home-built measurement setup with software they
already trust, and who wants an agent to be able to operate it without
rewriting it and without trusting the agent to behave.

It assumes:

- multiple instruments on shared, stateful buses (GPIB, serial, TCP), at least
  one of which is exclusive-open
- one rig per running application instance
- one trust domain — the people who can reach the software are the people
  allowed to operate the rig
- experiments that take hours and consume something irreplaceable: cryogen,
  beam time, sample lifetime, a thermal cycle

It does **not** address hard real-time control, multi-laboratory federation,
cross-institutional credentials, or any threat model involving an adversary
inside the lab. Those exclusions are load-bearing, not oversights; the spec
says why for each.

## Relationship to existing standards

Nothing here competes with an instrument-control standard. SiLA 2, OPC-UA/LADS,
SCPI and EPICS all live **below** this framework's Layer 0 and are encapsulated
by it; a driver may speak any of them. MCP and A2A address different edges
(agent↔tool and agent↔agent) and compose with it.

The closest neighbour is the Lab Agent Protocol (Zhu et al., arXiv:2606.03755,
June 2026), which addresses the same agent↔instrument edge and is a useful
requirements checklist. It is a v0.1 design specification with no
implementation, written for federated multi-instrument chemistry automation.
`declare-enforce-check.md` §7 states what this framework adopts from it, what it
adapts, and what it rejects, with a reason for each — including the places where
the two disagree outright.

## The claim worth testing

**The conformance suite is the specification, and it is executable.**

A specification document asks for compliance and receives it for about a month.
An auto-discovering conformance suite asks for nothing and receives it forever,
because a new module is covered the moment its file exists and the failure
message tells the author what to declare. That is the entire delivery mechanism
of this framework, and the reason it is shipped as a harness rather than as a
PDF.
