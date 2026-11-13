# I2AS improvement: measurement-fault flags

*2026-11-13. For an agent porting this to I2AS. CryoSoft (`develop`) is the reference implementation.*

**Goal:** when a measurement instrument reports compliance, overflow or a stopped acquisition, the run fails and stops, and the operator sees which instrument failed and why. Today these errors are swallowed and the run looks stuck.

## Problem (check I2AS for the same patterns)

- **Compliance hidden as NaN:** the compliance bit was checked, logged, and the datapoint returned as NaN; the run carried on silently.
- **Overflow stored as data:** a Keithley 2182A over range returns `+9.9E37`; it was saved as a real voltage.
- **Stopped engine blocks the hardware thread:** after a 6221 delta compliance abort, the driver polled with 10 s timeouts x 3 per datapoint (~30 s frozen each point).

## Decisions (already made, do not re-ask)

1. **Fail and stop** on the first faulted datapoint: source to safe state, run recorded as failed with the reason, queue held. Not a machine-wide ERROR.
2. **No lock afterwards:** the next arm of that instrument clears the fault; no acknowledge click.

## Standard

- **Flags** (closed vocabulary): `compliance`, `overflow`, `no_data`, `instrument_error`.
- **Typed error** `MeasurementFaultError(flag, message)`, *not* a subclass of the communication error, so bus-retry code cannot swallow it.
- **Driver:** detects the instrument's documented error signal and raises it; fails fast. Sim twin models the same physics.
- **Measurement VI:** declares `measurement_fault_flags`; lets the error through `take_reading()` (never NaN). Zero valid samples = `no_data`.
- **Run engine:** catches it at the measure boundary, fails the run via the normal run-failure path, records an advisory (report-only) `measurement:<vi>` condition, cleared on the next arm.
- **Tests:** conformance (every measurement VI declares flags from the vocabulary), per-layer sim tests, one integration test (sweep into compliance fails with reason, source at 0, queue held).

## Instrument detection

| Instrument | Flag | Detection |
|---|---|---|
| Keithley 6221 DC | `compliance` | `:STAT:QUES:COND?` bit 1, once per datapoint (per-sample polling caused `-410` errors) |
| Keithley 6221 delta | `compliance` | after the first failed poll, latched `:STAT:QUES:EVEN?` bit 1 (verify on hardware) |
| Keithley 6221 delta | `no_data` | zero readings returned |
| Keithley 2182A | `overflow` | reading magnitude >= 9.9e37 |
| Tensormeter, lock-in | TBD | check manuals before declaring flags |
