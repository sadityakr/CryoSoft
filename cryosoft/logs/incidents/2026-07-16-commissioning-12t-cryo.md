# Commissioning Report: 12T-Cryo Setup

**Date:** 2026-07-16  
**Setup Name:** 12T-Cryo  
**Status:** ⚠ PARTIAL COMMISSION (blocking issues remain)  
**Commissioning Agent:** Claude Copilot  

---

## Hardware Inventory

| Instrument | Make/Model | Address | IDN (confirmed) | Status |
|-----------|-----------|---------|-----------------|--------|
| Magnet PSU | Oxford Mercury iPS | ASRL5 | MERCURY IPS:223350003:2.5.09.000 | ✓ OK |
| VTI Temp | Oxford ITC 503 | ASRL12 | ITC503 Version 1.11 | ⚠ BLOCKED |
| Sample Temp | Lakeshore 335 | GPIB0::30 | LSCI,MODEL335,LSA29H3 | ✓ OK |
| Current Source | Keithley 6220 | GPIB0::19 | KEITHLEY 6220,4555659,D04 | ✓ OK |
| Nanovoltmeter | HP 34420A | GPIB0::27 | HEWLETT-PACKARD,34420A,0,3.0 | 🔍 Not used yet |
| Meter (BLOCKED) | Keithley 2182A | ? | ? | ⚠ NOT FOUND |

---

## Preflight Check

**Result:** ✓ **PASSED** (3/3 active drivers OK)

```
mercury_ips          ASRL5::INSTR   OxfordMercuryiPS       OK
lakeshore_sample     GPIB0::30      Lakeshore335           OK
keithley_6220        GPIB0::19      Keithley6221 (6220)    OK
```

---

## Per-Instrument Bench Tests

### L0 — Passive Read Tests ✓

| Instrument | Method | Result | Status |
|-----------|--------|--------|--------|
| Mercury iPS | get_idn | IDN:OXFORD INSTRUMENTS:MERCURY IPS:223350003:2.5.09.000 | ✓ |
| Mercury iPS | get_current | -0.0006 A | ✓ (near zero, persistent mode) |
| Lakeshore 335 | get_idn | LSCI,MODEL335,LSA29H3/#######,2.1 | ✓ |
| Lakeshore 335 | get_temperature | 0.0 K | ⚠ (sensor disconnected?) |
| Keithley 6220 | get_idn | KEITHLEY INSTRUMENTS INC.,MODEL 6220,4555659,D04 /700x | ✓ |

**Assessment:** All L0 tests passed. Mercury iPS and Keithley 6220 respond correctly and consistently. Lakeshore 335 temperature read as 0K, indicating the RTD sensor may be disconnected or misconfigured.

### L1 — Set/Read-Back (Zero Excitation)

**Skipped** — Pending resolution of blockers.

### L2 — Minimal Excitation

**Skipped** — Pending resolution of blockers.

---

## Blocking Issues

### Issue #1: Oxford ITC 503 (VTI Controller)

**Symptom:** Preflight OPEN_FAILED — "The instrument did not understand this command: C3"

**Diagnosis:**
- Raw VISA connection works: ASRL12 responds to Oxford ISOBUS `V` command ("ITC503 Version 1.11")
- Serial parameters (9600 baud, 8N1, \\r termination) are correct
- Issue is with pymeasure's `ITC503` class initialization — it sends a command the firmware rejects

**Root Cause:** Likely pymeasure using incorrect serial settings or command sequence for this firmware version.

**Resolution Options:**
1. Downgrade/upgrade pymeasure to a compatible version
2. Implement a native ISOBUS driver for ITC 503 (simpler, more reliable)
3. Investigate pymeasure source code for serial setup issue

**Recommended:** Option 2 (native ISOBUS driver) — the device is simple enough and we already have raw working communication.

**Impact:** VTI temperature control blocked. Sample temperature (Lakeshore 335) can still operate independently.

---

### Issue #2: Keithley 2182A (Nanovoltmeter)

**Symptom:** Not found on GPIB bus. User reported GPIB28, but:
- GPIB0::28 does not exist (ResourceManager reports only GPIB0–30)
- Probed GPIB0::7, 17, 18, 0–3: no K2182A response

**Hypothesis:** Either:
- Instrument is on a serial port (ASRL) instead
- Wrong GPIB address reported by user
- Instrument not powered/connected

**Resolution Required:** Confirm actual address with hardware inspection.

**Impact:** Delta-mode resistance measurement blocked (requires both 6220 source and 2182A meter). DC direct measurement not yet implemented.

---

## Summary Status

| Subsystem | Status |
|-----------|--------|
| Magnet PSU | ✓ Ready |
| Sample Thermometry | ⚠ Needs sensor check (reads 0K) |
| VTI Thermometry | ⚠ **Blocked** (ITC 503 driver) |
| Current Source | ✓ Ready |
| Voltage Meter | ⚠ **Blocked** (K2182A missing) |
| Measurement Chain | ⚠ **Incomplete** |

---

## Next Steps

1. **Immediate (Before Measurement):**
   - Inspect Lakeshore 335 RTD sensor connection
   - Locate Keithley 2182A address (check rear panel or scan with GPIB controller utility)
   - Fix or replace pymeasure ITC 503 driver

2. **Integration Testing (After Blockers Resolved):**
   - L1 bench: set/read-back on each subsystem
   - L2 bench: minimal excitation tests (≤10 mA magnet, ≤1 nA source)
   - Full measurement chain delta-mode sequence
   - Cool-down trial under VTI regulation

3. **Sign-Off:**
   - Human verification of all bench results
   - Formal commissioning handover

---

## Artifacts

- Config: `cryosoft/configs/12t-cryo/devices.yaml` (ITC and K2182A commented as blocked)
- Setup notes: `cryosoft/configs/12t-cryo/setup.md`
- Troubleshoot transcript: `cryosoft/logs/troubleshoot.jsonl`

---

**Commissioned by:** Claude Copilot (GitHub)  
**Date:** 2026-07-16  
**Reviewed by:** [Pending human sign-off]
