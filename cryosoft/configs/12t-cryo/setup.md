# Setup: 12T-Cryo

**Commissioned:** 2026-07-16  
**Commissioning Agent:** Claude Copilot  
**Human Verification:** Pending

## Purpose

Single-axis 12 Tesla superconducting magnet cryostat with integrated VTI temperature
control, sample thermometry, and delta-mode resistance measurements.

## Hardware Inventory

| Instrument | Make/Model | Purpose | Address | IDN (discovered) |
|-----------|-----------|---------|---------|------------------|
| Magnet PSU | Oxford Mercury iPS | Superconducting magnet control | ASRL5 | MERCURY IPS:223350003:2.5.09.000 |
| VTI Temp | Oxford ITC 503 | VTI temperature regulation | ASRL12 | ITC503 Version 1.11 |
| Sample Temp | Lakeshore 335 | Sample thermometry (heater + sensor) | GPIB0::30 | LSCI,MODEL335,LSA29H3 |
| Current Source | Keithley 6220 | AC current source for δV measurement | GPIB0::19 | KEITHLEY INSTRUMENTS INC.,MODEL 6220,4555659,D04 /700x |
| Nanovoltmeter | HP 34420A | Sensitive voltage readout (~1 nV) | GPIB0::27 | HEWLETT-PACKARD,34420A,0,3.0-5.0-2.0 |

## Physical Wiring & Safety Notes

- **Magnet lead current:** mercury_ips → sample leads, currently **open** (no sample)
  - Rated to ±90 A (±12 T at 7.954 A/T)
- **Sample wiring:** 1–10 kΩ test resistor connected across leads
  - Keithley 6220 sourcing → HP 34420A voltage readback
  - Never exceed ±10 mA in diagnostics
- **VTI heater:** controlled through ITC 503
- **Sample heater:** integrated in Lakeshore 335

## Known Quirks & Setup-Specific Notes

- ITC 503 uses **ISOBUS** protocol (command `V` for identity, not *IDN?)
- HP 34420A is a precision nanovoltmeter; very sensitive to AC noise
- Magnet currently in **persistent mode** (no energization during commissioning)

## Addressing & Communication

- **Serial (ASRL):** Mercury iPS, ITC 503
  - Baud rates and termination: see manual cheat sheets
- **GPIB0:** Lakeshore 335, Keithley 6220, HP 34420A
  - All GPIB: termination is LF (\\n)

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
    ├── keithley-6220.pdf
    ├── hp-34420a.pdf
    └── notes/                  (extracted cheat sheets)
        ├── mercury-ips.md
        ├── itc503.md
        └── ...
```

## Commission Status

- [ ] Preflight check (all drivers OK)
- [ ] L0 bench: each instrument IDN + one getter
- [ ] L1 bench: set/read-back (zero excitation)
- [ ] L2 bench: minimal excitation (≤10 mA magnet, ≤1 nA source, ≤1 K setpoint)
- [ ] Full incident report written
- [ ] Signed off by human

---

**Next:** Run `python -m cryosoft.troubleshoot check --config 12t-cryo --json`
and iterate preflight loop until all drivers report OK.
