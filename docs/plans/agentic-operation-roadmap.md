# Roadmap: Agentic Operation & Instrumentation Debugging

**Author:** Claude Code (diagnostic session 2026-07-22)  
**Status:** Design document for discussion  
**Scope:** Improvements needed for safe parallel agent execution and rapid hardware diagnostics  
**Related:** `LOGBOOK.md` (2026-07-22), `cryosoft/drivers/keithley_6221.py::_check_error_queue()`

---

## Executive Summary

This session diagnosed a Keithley 6221 `-221 "Settings conflict"` error that required live hardware testing to isolate. The root cause — leftover autorange-off state from delta mode — was invisible to the app entirely (silent SCPI rejection, no exception, only visible on the instrument's front panel). The fix is now live, but the debugging process revealed three critical gaps:

1. **Diagnostic tools don't replicate app behavior** — single-shot queries hide session-state bugs
2. **No observability of queued SCPI errors** — rejected commands silently fail unless someone checks the front panel
3. **No safety isolation for agent test sequences** — if an agent runs a diagnostic and crashes mid-sequence, the instrument stays in an unknown state

These gaps will compound as you add more agents running in parallel (concurrent diagnostics, procedures, monitoring). This document proposes concrete fixes, ordered by impact and effort.

---

## Problem Statement: Why This Matters Now

### What happened (2026-07-22)

User reported: "DC-mode loop with ±100 µA currents shows -221 error."

**Initial hypothesis (wrong):** Compliance-ordering in DC-mode VI's `initiate_measurement()`.  
**Reality:** Delta mode had run earlier in the same session, left the 6221's current range **fixed at 2 µA** with autorange OFF. DC mode's ±100 µA requests (50× that fixed ceiling) were silently rejected by the instrument — no exception raised anywhere, only visible on the 6220's front-panel error display.

**Why the app never caught it:**
- Driver's `set_current()` doesn't poll `:SYST:ERR?` (SCPI error queue), only raises on VISA communication failures
- `cryosoft.log` shows the call completed cleanly: `"keithley_dc_mode.set_dc_current -> None"`
- Readback confirmed the source stayed at its old value, but no warning logged
- User had to physically check the 6220's screen to see the error

**Why diagnostics failed:**
- `troubleshoot` CLI opens a fresh VISA session per command — each call resets the instrument to defaults, hiding any session-state bug
- Single queries like `:SOUR:CURR:RANG?` show "2e-6" (stale leftover) but don't prove it was *causing* the problem
- No way to run a **sequence** of commands in one persistent session without writing a separate Python script

**Why this compounds with agents:**
- Multiple agents (a diagnostic agent, a procedure-running agent, a monitor) touching the same shared 6221 means state pollution from one agent affects the others
- If an agent dies during a test (crash, timeout, abort), the instrument is left armed/dirty
- Agents have no "before/after" snapshot or rollback capability
- No structured visibility into "what actually happened on the wire"

### Why agent-parallel operation makes it urgent

As you build out agentic features (auto-diagnosis, adaptive procedures, real-time optimization), you'll have:

- **Concurrent diagnostic agents** (troubleshoot skill, performance profiler) running while a procedure is active
- **Recovery agents** (auto-retry, fallback logic) that need to undo failed attempts safely
- **Multi-instrument coordination** (magnet + sample temperature + current source all changing) with no built-in transaction isolation

Without the fixes below, you'll see:
- Diagnostic sequences mysteriously fail when procedures are running (state interference)
- Agents leaving instruments armed/dirty, breaking downstream measurements
- Hours spent re-diagnosing issues that were actually just "previous agent left it in a weird state"
- Silent failures masked as procedure bugs (instrument rejected the command, but the app thinks it succeeded)

---

## Proposed Solutions

Organized by **phase** (can be done in parallel), **effort**, and **impact** on agentic safety.

### Phase 1: Observability (Immediate, Low Effort, High Impact)

#### 1a. Extend error-queue checking to all sensitive driver writes

**What:** Add `:SYST:ERR?` polling after every driver call that can be silently rejected, not just new ones.

**How:**
```python
# In Keithley6221 driver:
def set_compliance(self, compliance_v: float) -> None:
    self._write(f":SOUR:CURR:COMP {compliance_v:.4e}")
    self._check_error_queue(f"set_compliance({compliance_v!r})")

# In Keithley2182A driver (add if missing):
def set_range(self, range_v: float) -> None:
    self._write(f":SENS:VOLT:RANG {range_v:.4e}")
    self._check_error_queue(f"set_range({range_v!r})")
```

**Why:** The 2026-07-22 session added error-queue checking to `set_current()`, but `set_compliance()` was already there — it should have been checked too. Any SCPI command that *sets* state can be rejected; only *queries* are guaranteed safe. Making this habitual in the driver layer means agents and procedures get error visibility for free.

**Effort:** 30 minutes — grep for `self._write()` calls in driver files, add `_check_error_queue()` after each one that isn't already checked.

**Testing:** Existing `tests/test_l0_keithley_6221_error_queue.py` + add similar tests for any other driver `set_*` methods touched.

---

#### 1b. Add persistent-session mode to troubleshoot skill

**What:** Allow a sequence of commands to run in a single VISA session, matching app behavior.

**How:**
```bash
troubleshoot session <driver> <protocol> [--id <checkpoint-id>]
```

Input (stdin or file):
```
# Sequence of commands in one session
write set_current 0.0
read get_current
query :SOUR:CURR:RANG?
write set_compliance 10.0
read get_compliance
query :SYST:ERR?
# ... more commands ...
```

Output (JSON):
```json
{
  "session_id": "...",
  "commands": [
    {"cmd": "write set_current 0.0", "error": null},
    {"cmd": "query :SOUR:CURR:RANG?", "response": "2.000000E-06"},
    ...
  ]
}
```

**Why:** This is the missing diagnostic tool. The `-221` bug took hours to isolate because single-shot queries hide session-state dependencies. A diagnostic agent could use this to "replay the user's procedure step-by-step and show me where it fails" without opening/closing the VISA session between commands.

**Effort:** 2–3 hours (extend existing `cryosoft/troubleshoot/engine.py` to track session state; add a new CLI mode).

**Testing:** Unit test that verifies a sequence runs in one session (mock VISA instrument, confirm session object is reused).

---

### Phase 2: Safety & Isolation (Short-term, Medium Effort, High Impact)

#### 2a. Standard safe-shutdown for all drivers

**What:** A consistent "leave-it-idle" sequence every driver exposes, called on agent exit or error.

**How:**
```python
# In DriverBase or each driver class:
def safe_shutdown(self) -> None:
    """Unconditionally restore safe idle state.
    
    Called by:
    - Agent cleanup handlers (on agent exit, success or failure)
    - Orchestrator on emergency stop
    - VI standby() methods
    
    Must be idempotent (calling twice is safe).
    """
    # Driver-specific safe-idle sequence
    # E.g., for Keithley6221:
    #   - zero current: set_current(0.0) -> :SOUR:CURR 0
    #   - abort engines: :SOUR:SWE:ABOR
    #   - re-enable autorange: :SOUR:CURR:RANG:AUTO ON
    #   - verify: get_idn() (responsiveness check)
```

**Why:** Right now there's no standard "I'm done, leave it safe" — `standby()` is inconsistently implemented across VIs. As agents proliferate, you need a guaranteed cleanup. The `-221` fix happened to include the right sequence (`set_current(0.0)` → `:SOUR:SWE:ABOR` + `:SOUR:CURR:RANG:AUTO ON`), but agents can't know that without reading the driver code.

**Effort:** 1 hour — document the pattern, add `safe_shutdown()` to `Keithley6221`, `Keithley2182A`, and any other multi-mode drivers; wire it into agent exit hooks.

**Testing:** Unit test per driver (mock instrument, verify the right SCPI commands fire in the right order).

---

#### 2b. Shared-instrument conformance test

**What:** Automatic test that every VI sharing the 6221 can recover from arbitrary prior state.

**How:**
```python
# In tests/test_conformance.py or new tests/test_shared_instrument_discipline.py:

@pytest.mark.parametrize("vi_name,init_params", [
    ("keithley_dc_mode", {"current": 1e-6, "n_readings": 10, "compliance_V": 10.0}),
    ("keithley_delta_mode", {"current_high": 1e-4, "current_low": -1e-4, ...}),
    ("lock_in_harmonic", {...}),  # future
])
def test_vi_recovers_from_stale_delta_state(vi_name, init_params):
    """Calling initiate_measurement() must succeed even if the instrument
    was left in a broken state by a prior VI (delta armed, range fixed,
    autorange off, compliance at 100V, etc.)."""
    
    vi = instantiate_vi(vi_name)
    source = vi._source
    
    # Simulate delta mode left the instrument in a broken state
    source._write(":SOUR:DELT:HIGH 1e-3")   # delta mode armed
    source._write(":SOUR:DELT:LOW -1e-3")
    source._write(":SOUR:CURR:RANG:AUTO OFF")  # autorange off (leftover)
    
    # Verify the instrument is actually broken at this point
    assert source._query(":SOUR:CURR:RANG:AUTO?") == "0"
    
    # The VI should still work
    vi.initiate_measurement(**init_params)
    data = vi.take_reading()
    
    # At least one valid reading (not all NaN)
    assert data["n_valid"] > 0, "VI failed to recover from stale delta state"
```

**Why:** This test would have caught the `-221` bug **before** it hit real hardware. It encodes the shared-instrument mode discipline (from the README) as an executable contract, so future VI developers inherit the requirement automatically. Without it, every new VI is a potential regression (someone might forget to force autorange).

**Effort:** 2 hours — write the test template, instantiate it for DC and delta mode, add to conformance suite.

**Testing:** Runs on every `pytest -m "not hardware"` (simulator-only, no real 6220 touched).

---

### Phase 3: Transaction Isolation (Medium-term, Higher Effort, Critical for Agents)

#### 3a. Instrument context manager for safe test sequences

**What:** A context manager that snaps instrument state, runs a sequence, and rolls back on failure.

**How:**
```python
from cryosoft.drivers.instrument_context import InstrumentContext

# Agent or skill code:
with InstrumentContext(source_driver, meter_driver) as ctx:
    # All driver calls go through ctx
    ctx.write("set_current", 1e-4)
    ctx.write("set_compliance", 10.0)
    voltage = ctx.read("get_voltage")
    
    if voltage < -0.5:  # something wrong
        raise ValueError(f"Unexpected voltage: {voltage}")
        # On exit: automatic rollback to snapped state

# Success: automatic safe-shutdown (zero current, abort engines, etc.)
# Failure: rollback + safe-shutdown
```

**Implementation sketch:**
```python
class InstrumentContext:
    def __init__(self, *drivers):
        self.drivers = drivers
        self.snapshots = {}
        self.dirty = True
    
    def __enter__(self):
        # Snapshot state (read key registers)
        for driver in self.drivers:
            self.snapshots[driver.name] = {
                "current": driver.get_current(),
                "compliance": driver.get_compliance(),
                "autorange": driver._query(":SOUR:CURR:RANG:AUTO?"),
                "sweep_armed": driver._query(":SOUR:SWE?"),
            }
        return self
    
    def write(self, method_name, *args, **kwargs):
        method = getattr(self.drivers[0], method_name)
        method(*args, **kwargs)
        # Log the call, check error queue
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            # Rollback: restore snapped state
            self._restore_snapshot()
        # Always finalize: safe-shutdown
        for driver in self.drivers:
            driver.safe_shutdown()
        return False  # don't suppress exception
    
    def _restore_snapshot(self):
        for driver, snap in self.snapshots.items():
            driver.set_current(snap["current"])
            driver.set_compliance(snap["compliance"])
            driver._write(f":SOUR:CURR:RANG:AUTO {snap['autorange']}")
            # ... etc
```

**Why:** As agents run diagnostics or auto-recovery sequences, they need to know "if I fail mid-sequence, I leave things how I found them." This is essential for safe parallelism — if agent A's diagnostic crashes while testing delta mode, agent B's DC-mode procedure doesn't suddenly inherit a broken 6221.

**Effort:** 3–4 hours — design the snapshot/restore logic, add to a new `cryosoft/core/instrument_context.py`, wire into agent lifecycle hooks.

**Testing:** Unit tests (mock drivers), integration test (real simulator).

---

#### 3b. Agent-safe driver access layer with structured diagnostics

**What:** Wrap driver method calls with logging, error-queue checking, and state tracking, so agents have a structured feed of "what actually happened."

**How:**
```python
# Define diagnostic event
@dataclass
class DiagnosticEvent:
    timestamp: datetime
    driver_name: str
    method: str
    args: tuple
    kwargs: dict
    result: Any
    error: str | None
    scpi_error: str | None  # from :SYST:ERR?
    state_before: dict  # key registers
    state_after: dict

# Wrap driver methods
class DriverProxy:
    def __init__(self, driver, on_event: Callable[[DiagnosticEvent], None]):
        self._driver = driver
        self._on_event = on_event
    
    def __getattr__(self, method_name):
        method = getattr(self._driver, method_name)
        if not callable(method):
            return method
        
        def wrapped(*args, **kwargs):
            event = DiagnosticEvent(
                timestamp=datetime.now(),
                driver_name=self._driver.name,
                method=method_name,
                args=args,
                kwargs=kwargs,
                state_before=self._capture_state(),
                ...
            )
            try:
                result = method(*args, **kwargs)
                event.result = result
                event.error = None
                event.scpi_error = self._driver._query(":SYST:ERR?")
            except Exception as exc:
                event.error = str(exc)
                event.scpi_error = self._driver._query(":SYST:ERR?")
                raise
            finally:
                event.state_after = self._capture_state()
                self._on_event(event)
            return result
        
        return wrapped
```

**Why:** Agents need structured, real-time feedback on instrument behavior. Right now they have `cryosoft.log` (unstructured text) and `get_*()` queries (data only, no context). With this, an agent can subscribe to `on_event` and adapt: "last call returned error -221, try forcing autorange" (auto-recovery) or "state didn't change as expected, log it and escalate to human."

**Effort:** 2–3 hours (thin wrapper layer, event dataclass, hook into Station/Orchestrator).

**Testing:** Unit test (verify events are emitted correctly), integration test (agent subscribes and reacts).

---

### Phase 4: Documentation & Standards (Ongoing)

#### 4a. "Shared-Instrument Mode Discipline" conformance standard

**What:** Expand the existing README note into a full testable standard, like the driver/VI/procedure contracts already do.

**Current state:** `virtual_instruments/measurement/README.md` has a paragraph describing the pattern. Not enforced.

**Proposed:**
```markdown
## Shared-Instrument Mode Discipline Standard

**Applies to:** Any Virtual Instrument or driver that shares a physical instrument
(e.g., K6221 running delta mode one tick, then DC mode the next).

**Contract:**
- Every `initiate_measurement()` (or equivalent setup method) must call
  `driver.safe_shutdown()` first, unconditionally, before any other writes.
- `safe_shutdown()` must be idempotent (calling twice is safe).
- The driver must document what leftover states it recovers from.

**Conformance test:** `test_vi_recovers_from_stale_<mode>_state()` per VI.

**Why:** Prevents silent failures when instrument state drifts between procedures/agents.
```

**Effort:** 1 hour — write the standard, link from README, add to GLOSSARY.

---

#### 4b. Agent debugging handbook

**What:** Living guide for agents writing safe diagnostic/recovery sequences.

**Topics:**
- Using `troubleshoot session` to build and test sequences
- `InstrumentContext` for safe rollback
- Interpreting `:SYST:ERR?` and recovery strategies per instrument
- Common state traps (autorange, sweep armed, compliance set) and how to force-clear them
- Example: "Diagnose a K6221 that's stuck at compliance"

**Effort:** 2 hours (compile from this session's findings + design docs).

---

## Implementation Roadmap

### Week 1 (Immediate)
- [ ] **Phase 1a:** Extend error-queue checking to all driver writes (30 min)
- [ ] **Phase 1b:** Add `troubleshoot session` mode (2–3 hr)
- [ ] **Phase 2a:** Standardize `safe_shutdown()` across drivers (1 hr)

### Week 2–3 (Short-term)
- [ ] **Phase 2b:** Shared-instrument conformance test (2 hr)
- [ ] **Phase 4a/b:** Document the standard and agent handbook (3 hr)

### Week 4+ (Medium-term, can run in parallel)
- [ ] **Phase 3a:** Instrument context manager (3–4 hr)
- [ ] **Phase 3b:** Driver proxy + diagnostic events (2–3 hr)
- [ ] Integration: wire both into agent lifecycle, test end-to-end

---

## Experience-Based Arguments

### Why this matters (hard-won lessons from 2026-07-22)

**Argument 1: Silent failures compound faster than visible ones**

The `-221` error was silent for hours because:
- No exception was raised (app-level code succeeded)
- No log entry existed (driver didn't check error queue)
- Only the instrument's front panel showed it (human had to physically look)

By the time we diagnosed it, the user had already reloaded the GUI, run multiple procedures, and spent an hour wondering if it was a code regression. With Phase 1 (error-queue checking + persistent-session diagnostics), the next similar bug would take 15 minutes to find, not 3 hours.

**Argument 2: Session-state bugs are invisible to single-shot queries**

We tested at 1 nA (safe) repeatedly and got clean results. Real operation at 100 µA failed. The difference — autorange-off from a prior session — was invisible in a single query (`:SOUR:CURR:RANG?` reads the register, but doesn't prove it's *causing* the current-set to fail). Only a persistent-session replay exposed it. As you add more agents, session pollution will get worse, not better — you need the observability built in from day one.

**Argument 3: Agents need deterministic safety guarantees, not best-effort cleanup**

If an agent diagnostic crashes mid-sequence, relying on `try...finally` and hoping `standby()` gets called is fragile. With `InstrumentContext`, rollback is automatic and verified — agents can be fearless about running test sequences because they know the instrument will be left in a known state regardless of what happens.

**Argument 4: Shared resources need transaction isolation**

The 6221 is shared by DC mode, delta mode, and (future) lock-in. Right now there's no guarantee that switching between them is safe — each VI has its own recovery logic, and if one is wrong or incomplete (like DC mode's missing autorange reset), it silently breaks the next VI. The conformance test makes this a hard requirement, not a soft convention.

**Argument 5: Debugging agents need structured data, not log text**

The agent troubleshoot skill would be far more powerful if it got structured diagnostic events ("this call failed with error X at time T, and state changed from A to B") instead of having to parse `cryosoft.log` or re-run commands to figure out what happened. Phase 3b is the foundation for building intelligent recovery logic.

---

## Alternatives Considered (and Rejected)

### "Just add more tests"
**Why it's not enough:** Tests run on simulators in isolation. Real hardware in a shared session has emergent behaviors (state pollution, mode interactions) that can't be fully simulated. Tests are necessary but not sufficient — you also need runtime observability (error-queue checking) and isolation (context managers).

### "Agents should handle cleanup themselves"
**Why it fails:** Requires every agent to know every driver's safe-shutdown sequence. Unmaintainable and error-prone. The standard `safe_shutdown()` + automatic `InstrumentContext` cleanup is the DRY approach.

### "Rebase/rewind on failure instead of rolling back"
**Why it's not feasible:** A SCPI command sent to a real instrument can't be "undone" — if you set compliance to 100V, sending set_compliance(1V) afterward doesn't undo the first write, it just overwrites it. You have to restore *all* state that might have been touched.

---

## Success Metrics

After implementing these phases, you should see:

1. **Diagnosis time for hardware bugs:** < 30 min (was 3+ hours for `-221`)
2. **Zero silent SCPI failures:** Every rejected command logged as a WARNING
3. **Agent test sequences:** Can run safely without manual cleanup
4. **Shared-instrument regression risk:** Near zero (conformance test catches misses)
5. **New VI onboarding:** Developer reads handbook, VIs inherit safety practices automatically

---

## References

- `LOGBOOK.md`: 2026-07-22 entries (full diagnosis narrative)
- `cryosoft/drivers/keithley_6221.py::_check_error_queue()`: Implementation of error-queue polling
- `cryosoft/virtual_instruments/measurement/README.md`: Current shared-instrument discipline notes
- `tests/test_l0_keithley_6221_error_queue.py`: Conformance test template for error observability
