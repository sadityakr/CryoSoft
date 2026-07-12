---
name: troubleshoot-runtime
description: Explain what a RUNNING CryoSoft measurement is doing and whether it is stuck, slow, or normal. Reads the live operational-status log (cryosoft/logs/status.jsonl, written by the Orchestrator each tick) via `python -m cryosoft.troubleshoot status` and interprets state, per-instrument ramp progress, ETA, and watchdog alerts in plain language for the operator. Use when the app is running and the user asks "why is this taking so long", "is it stuck", "what is it doing", "is this normal". NOT for setup-time instrument or config faults with the app closed — that is the setup-supervisor skill.
---

# troubleshoot-runtime — explain what a running measurement is doing

The user (often a PhD student operating the cryostat) is watching a run and
cannot tell whether a long ramp is normal or wedged. Your job is to read the
live status log and tell them, in plain language, what is happening and whether
to worry.

This works **while the app is running**. It only reads a log file
(`cryosoft/logs/status.jsonl`), so it never touches an instrument and never
needs the app closed. That is the opposite of `setup-supervisor`, which
diagnoses instruments with the app CLOSED.

## The one command

```
python -m cryosoft.troubleshoot status          # plain-English digest
python -m cryosoft.troubleshoot status --json    # structured, for you to parse
python -m cryosoft.troubleshoot status --last 20 # widen the trend window
```

It reads the tail of `status.jsonl` and prints: the orchestrator state and how
long it has been in it, the overall verdict, any watchdog alerts, and per
instrument `value -> target (gap, ramp_status, trend, ETA) [code]`. Exit code is
0 only when a log exists and the verdict is `OK`, so you can gate on it.

## How to answer the common questions

- **"Is it stuck?"** Look at `verdict` and `alerts`. `RAMP_STALLED` or
  `STALLED_RUN` is a high-confidence "yes, stuck" (the watchdog is deliberately
  lenient, so an alert is meaningful). `OK` with a gap that is `closing` is
  normal progress.
- **"Why is it taking so long?"** Look at the instrument's `gap`, `trend`, and
  ETA. If it is `closing` with a large ETA, it is simply a big/slow ramp, not a
  fault. If the gap is `flat` and not closing, it is stalling even if no alert
  has fired yet (the watchdog waits several ticks before flagging).
- **"What is it doing right now?"** Report the state, the procedure progress
  percentage, and which instrument is ramping toward what target.

## The division of labor: tool gives facts, you give the diagnosis

The `status` command reports facts and a fixed triage note per fault code (shown
under "What the codes mean"). That note is the *starting* hypothesis, not the
final word. The open-ended reasoning is yours:

- A `RAMP_STALLED` on a temperature controller with the heater near 100 percent
  points to "cannot reach setpoint" (thermal load, too-high target), whereas the
  same code with the heater idle points to the controller not driving at all.
  Read the instrument fields in the `--json` output to tell these apart.
- Correlate across instruments: a magnet stall during a field sweep plus a
  simultaneous `VI_STALE` on the magnet points at a communication problem, not a
  ramp problem.

## When to hand off

- If the code is `VI_STALE` or `VI_DISCONNECTED`, the instrument has stopped
  responding. Diagnosing that needs to probe the instrument, which requires the
  app CLOSED. Explain the finding, then hand off to `setup-supervisor` (which
  the user runs after closing the app).
- If the verdict is `QUENCH`, treat it as an emergency: the run should already
  be in EMERGENCY. Confirm the magnet and helium state; do not attempt to clear
  it from software.

## Do not act destructively without asking

Reading status is safe and needs no permission. But pausing, aborting, or
restarting a run is destructive (partial data, hardware ramps). Report what you
found and recommend an action; let the user decide. Never abort a run just
because a watchdog alert fired.
