# Setup: 12T-Cryo

**Commissioned:** 2026-07-16
**Last updated:** 2026-07-20 — recommissioning pass: VTI, level meter, magnet coil now live
**Commissioning Agent:** Claude Copilot
**Human Verification:** Pending

## Purpose

Single-axis 12 Tesla superconducting magnet cryostat with integrated VTI temperature
control, sample thermometry, cryogen level monitoring, and delta-mode resistance
measurements.

## Hardware Inventory

| Instrument | Make/Model | Purpose | Address | IDN (discovered) |
|-----------|-----------|---------|---------|------------------|
| Magnet PSU | Oxford Mercury iPS | Superconducting magnet control (z-axis, vertical field) | ASRL5 | MERCURY IPS:223350003:2.5.09.000 |
| VTI Temp | Oxford ITC 503 | VTI temperature regulation | ASRL12 | ITC503 Version 1.11 (c) OXFORD 1997 |
| Sample Temp | Lakeshore 335 | Sample thermometry (heater + sensor) | GPIB0::30 | LSCI,MODEL335,LSA29H3 |
| Level Meter | Oxford ILM 210 | Helium level monitoring | ASRL11 | ILM200 Version 1.06 (c) OXFORD 1994 — see Known Quirks |
| Scanner | Keithley 705 | Channel scanner for delta-mode routing | GPIB0::17 | C001,S0 |
| Current Source | Keithley 6220 | AC current source for δV measurement | GPIB0::19 | KEITHLEY INSTRUMENTS INC.,MODEL 6220,4555659,D04 /700x |
| Nanovoltmeter | HP 34420A | Sensitive voltage readout (~1 nV) | GPIB0::27 | HEWLETT-PACKARD,34420A,0,3.0-5.0-2.0 |

Note: HP 34420A is not currently represented in `devices.yaml` — the K6220
drives the delta-mode measurement via its internal serial relay to a
Keithley 2182A; the 34420A entry above documents the physical instrument
present in the rack, not an active driver.

## Physical Wiring & Safety Notes

- **Magnet leads:** `mercury_ips` → connected to the superconducting magnet
  coil and persistent switch heater.
  - Rated to ±90 A (±12 T at 7.954 A/T)
- **Sample wiring:** 1–10 kΩ test resistor connected across leads
  - Keithley 6220 sourcing → HP 34420A voltage readback
  - Never exceed ±10 mA in diagnostics
- **VTI heater:** controlled through ITC 503
- **Sample heater:** integrated in Lakeshore 335
- **Level meter:** ILM 210, helium channel only (nitrogen channel present on
  the instrument but not in use on this setup)

## Safe testing limits (overrides)

Overrides the default in `setup-supervisor/references/safe-testing.md`
("switch heater — never in diagnostics").

| Instrument | Limit | Reason |
|---|---|---|
| Mercury iPS switch heater | May be cycled during diagnostics **only at small field**, and only after confirming the PSU's output current reading matches the magnet's actual persistent-mode field/current before closing the switch | Closing the switch onto a mismatched PSU/magnet current induces a large step; checking agreement first avoids that. Human-approved override, 2026-07-20. |

## Known Quirks & Setup-Specific Notes

- 2026-07-20: Delta mode (K6220 + 2182A via serial relay) hung intermittently
  and looked like a GPIB-vs-RS-232 comm problem, but the real cause was the
  K6220's `CALC1` calculation stage being disabled — the driver now forces it
  on plus the documented relay settings (19200 baud, CR, XON/XOFF) on every
  `initiate()`. Two bench-side gotchas to check if it recurs: (1) the 2182A
  must be displaying **Channel 1 (DCV1)**, not Channel 2/rear (`REAR`
  annunciator) — delta mode always reads Channel 1 regardless of which
  channel's cable is plugged into the front socket; (2) a marginal RS-232
  cable between the 6220 and 2182A produces "DATA CORRUPT/STALE" on the
  2182A and overflow-looking readings on the 6220 side. Full diagnosis in
  `LOGBOOK.md` (2026-07-20 entry) and
  `cryosoft/drivers/keithley_6221.py::_program_delta_mode()`. Live-verified
  at 1 nA and 1 µA into the bench's ~1.1 kΩ test resistor.
- 2026-07-20: ILM 210 reports its identity string as **"ILM200 Version
  1.06 (c) OXFORD 1994"**, not "ILM210" — human-confirmed the physical unit
  is genuinely an ILM 210; the ID string is a legacy/compatible string Oxford
  reused across the ILM200/210 family. `expect_idn` in `devices.yaml` is set
  to match the actual string ("ILM200"), not the model name on the case.
  Uses the same ISOBUS protocol/driver (`oxford_ilm210.py`).
- 2026-07-20: The ITC 503 entry was previously blocked ("pymeasure C3 command
  fails"). That issue was specific to a `pymeasure`-based driver attempt;
  the shipped `cryosoft.drivers.oxford_itc503.OxfordITC503` (pure PyVISA,
  ISOBUS protocol) communicates cleanly on ASRL12. Unblocked.
- ITC 503 uses **ISOBUS** protocol (command `V` for identity, not `*IDN?`)
- ILM 210 also uses **ISOBUS** protocol (command `V` for identity, `@1`
  instrument-number prefix on every command)
- HP 34420A is a precision nanovoltmeter; very sensitive to AC noise
- Magnet was in **persistent mode, leads open** during initial commissioning;
  as of 2026-07-20 leads are closed onto the coil (see Physical Wiring above)

## Addressing & Communication

- **Serial (ASRL):** Mercury iPS (ASRL5), ITC 503 (ASRL12), ILM 210 (ASRL11)
  - Baud rates and termination: see manual cheat sheets
- **GPIB0:** Lakeshore 335 (::30), Keithley 705 (::17), Keithley 6220 (::19),
  HP 34420A (::27, not driven directly — see note above)
  - All GPIB: termination is LF (\n)

## File & Directory Structure

```
12t-cryo/
├── setup.md                    (this file)
├── devices.yaml                (config with init_params, safety limits)
├── monitor.yaml                (live parameter display)
└── manuals/
    ├── mercury-ips.pdf         (not tracked, human provides)
    ├── itc503.pdf
    ├── lakeshore-335.pdf
    ├── ilm210.pdf
    ├── keithley-705.pdf
    ├── keithley-6220.pdf
    ├── hp-34420a.pdf
    └── notes/                  (extracted cheat sheets)
        ├── mercury-ips.md
        ├── itc503.md
        └── ...
```

## Commission Status

Initial pass (2026-07-16): Mercury iPS, Lakeshore 335, Keithley 6220, HP
34420A — signed off, magnet leads open (no coil connection).

Recommissioning pass (2026-07-20): ITC 503, ILM 210, Keithley 705 brought
into `devices.yaml`; magnet leads closed onto the coil + switch heater.

- [x] Preflight check (all drivers OK) — pending re-run this session
- [ ] L0 bench: each newly-added instrument IDN + one getter
- [ ] L1 bench: set/read-back (zero excitation)
- [ ] L2 bench: minimal excitation (≤10 mA magnet, ≤1 K VTI setpoint move);
      switch heater cycling only per the override above (small field, PSU/magnet
      current confirmed matched first)
- [ ] Full incident report written
- [ ] Signed off by human

---

**Next:** Run `python -m cryosoft.troubleshoot check --config 12t-cryo --json`
and iterate the preflight loop until all drivers report OK, then proceed to
the per-instrument bench.
