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
| Resistance tensor analyzer | Tensormeter RTM2 | Van-der-Pauw / Hall / Kelvin resistance-tensor measurement | 169.254.169.184:6340 (TCP — link-local, direct Ethernet/USB-adapter link, no DHCP; find via `rtm2.Discover()` if it changes) | Tensormeter RTM2 @ 169.254.169.184:6340 (synthesized — protocol has no `*IDN?`, see Known Quirks) |

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
- **RTM2 sample wiring:** a 100-200 kOhm test resistor connected across BNC
  ports 1 and 2 only (2026-07-23) — a 2-wire bench check, not a true
  4-terminal van-der-Pauw wiring (see `devices.yaml`'s `ch1_ch2` route and
  Known Quirks below). `max_current_A: 0.00001` (10 uA) in
  `tensormeter_measurement`'s `init_params` is sized for THIS resistor —
  rescale before wiring a lower-resistance sample.

## Safe testing limits (overrides)

Overrides the default in `setup-supervisor/references/safe-testing.md`
("switch heater — never in diagnostics").

| Instrument | Limit | Reason |
|---|---|---|
| Mercury iPS switch heater | May be cycled during diagnostics **only at small field**, and only after confirming the PSU's output current reading matches the magnet's actual persistent-mode field/current before closing the switch | Closing the switch onto a mismatched PSU/magnet current induces a large step; checking agreement first avoids that. Human-approved override, 2026-07-20. |

## Known Quirks & Setup-Specific Notes

- 2026-07-23: Tensormeter RTM2 commissioning findings (live, `169.254.169.184:6340`):
  1. **No `*IDN?` equivalent** — the TCP protocol has no identity-query
     command at all. `TensormeterRTM2.get_idn()` round-trips a harmless
     `gass` (Get-All-Server-Settings) query to confirm liveness and returns
     a synthesized `"Tensormeter RTM2 @ host:port"` string instead.
  2. **`rtm2-python` v1.2.3 library bug**: the device pushes its "Actual
     Analysis Mode" update tagged literally `mod?` (the vendor doc's own
     §3.12 name), but the library's own `_COMMANDS` registry only
     recognises `modq` — every `gass()` (and anything else that triggers
     this push) failed with `"Unknown incoming command: mod?"` until
     `TensormeterRTM2.__init__` patches an instance-level alias onto
     `self._rtm._COMMANDS`. See the code comment there for the full
     explanation.
  3. **Current setpoints need Control Mode 1 first**: `set_current_amplitude()`/
     `set_current_dc()` (`camp`/`cudc`) silently never confirm — looking
     exactly like a hang — while Control Mode (`cmod`) is `0` ("Direct
     Voltage Output", the power-on default). Control Mode `1` ("Feedback
     Voltage/Current Output") must be set first; `TensormeterRTM2MeasurementVI
     .initiate_measurement()` now does this automatically.
  4. **Root cause of the initial bad readings — a stale voltage setpoint,
     not an open circuit**: the confirmed resistor (200 kOhm, verified with
     a bench multimeter, well-connected, 2-point mode — shield unused) kept
     reading back wildly wrong (noisy, sign-flipping, near-zero delivered
     current) regardless of commanded current (tried 1 uA and 0.1 mA). The
     vendor's own User Guide §3.2 explains why: Control Mode 1 ("Feedback
     Voltage/Current Output") regulates like a CV/CC bench supply — the
     current AND voltage setpoints are both live simultaneously, and
     whichever is reached first governs. A stale `vodc` (DC voltage
     setpoint) of 0.01 V, left over from an earlier session, was silently
     the binding constraint the whole time: 0.01 V / 200 kOhm implies only
     ~50 nA, matching the ~65-75 nA actually observed no matter what
     current was commanded. Explicitly raising `vodc`/`vamp` to a safe
     ceiling (matching `vpro`) before sourcing current resolved this —
     `TensormeterRTM2MeasurementVI.initiate_measurement()` now does this
     automatically (zeros the unused DC setpoints, raises the AC voltage
     amplitude ceiling to `max_voltage_V`). Confirmed live: 2 uA commanded
     into the 200 kOhm resistor read back as ~196.7 kOhm mean (raw driver
     test, `set_current_dc`/DC path).
  5. **AC vs DC column mismatch**: `initiate_measurement()` sources via
     `current_amplitude_A` (the AC setpoint, `camp`), but `take_reading()`
     was reading the DC tensor columns (`res_a_dc_ohm`/`res_b_dc_ohm`) —
     with no DC component being sourced, that channel is just noise around
     zero. Fixed to read the 1st-harmonic (in-phase/real) AC column
     instead, which tracks the actually-sourced AC current.
  6. **Current setpoints need Control Mode 1 first** (see item 3 above)
     and **Waveform Mode must be "Continuous Sine Wave" (0)** —
     `initiate_measurement()` now reasserts both every time rather than
     trusting whatever a prior session left behind (a leftover Pulse Train
     waveform mode, with stale pulse parameters, was found live).
  7. **No confirmation echo when a setpoint's value doesn't change**: the
     RTM2 does not push a fresh state update when a setter's new value
     equals what it already holds (e.g. `set_current_dc(0.0)` when `cudc`
     was already `0.0`) — `_send_and_confirm()`'s wait would time out on
     this harmless no-op. Fixed with a `gass`-refresh fallback: if the
     cached value already matches what was requested, treat it as success.
  8. **`trig` aborts in-progress averaging**, and the device free-runs
     background sampling once armed: a single `trigger_demodulation()`
     call already yields many more buffered rows than `readings_per_point`
     within one `averaging_time_s`. Calling `trigger_demodulation()`
     repeatedly with a sleep in between (an earlier version of
     `take_reading()` did) kept interrupting the device before any
     averaging window ever completed. Fixed: trigger once effectively (n
     calls with no sleep between collapse to one, since only the last
     one's window survives), wait once for the whole batch to accumulate,
     then take the LAST `readings_per_point` rows (not the first — those
     can still be settling transients from before this call armed).
  9. Switch-matrix wiring and readback were solid throughout: `build_switch_state()`
     for `ch1_ch2` (`DRV-`=port2, `DRV+`=port1, `SNS-`=port2, `SNS+`=port1)
     produced word `16908546`, and the device's own `switch_status` column
     echoed that same value on every returned row — the bit-packing
     matches the vendor doc's worked examples exactly (also unit-tested,
     see `tests/test_l0_simulated.py::TestSimTensormeterRTM2`).
  10. **Residual noise still open**: after all the fixes above, a
      `ch1_ch2` / Kelvin-mode / AC read at 2 uA over 10 points landed with
      most samples clustered near the true ~200 kOhm (190-232 kOhm) but a
      few outliers remained (one as low as 5 kOhm, one as high as
      1.47 MOhm) — the mean/SEM the VI reports is still noisier than it
      should be. Plausible remaining causes: this is only a 2-wire
      connection (no lead/contact-resistance cancellation a true 4-wire
      wiring would give); the settling margin in `take_reading()` may
      still not be generous enough; or the AC excitation frequency
      (`lfrq`, currently a leftover 625 Hz) hasn't been chosen per the
      vendor's modulation-frequency guidance (User Guide §3.6 — should
      avoid mains harmonics). Not investigated further this session —
      worth another pass once a real 4-terminal sample is wired, or by
      simply increasing `readings_per_point`/`averaging_time_s` and
      discarding outliers.

- 2026-07-22: DC mode (K6220 + 2182A on its own GPIB address) hit `-221
  "Settings conflict"` on every `set_current()` call at real currents (e.g.
  ±100 µA), while completely silent at nA-scale test currents — no exception
  anywhere, only visible on the K6220's own front-panel display (the driver
  never polled `:SYST:ERR?`). Root cause, confirmed live: delta mode fixes
  the current range to match its configured high-current
  (`:SOUR:DELT:HIGH`/`:SOUR:DELT:LOW`) and leaves `:SOUR:CURR:RANG:AUTO`
  OFF, with nothing to undo it — verified `:SOUR:CURR:RANG:AUTO?` reading
  `0` and `:SOUR:CURR:RANG?` fixed at `2e-6` A after an earlier delta run,
  which silently rejects any later DC-mode current above that leftover
  ceiling. `Keithley6221.set_current()` now unconditionally sends
  `:SOUR:CURR:RANG:AUTO ON` before every `:SOUR:CURR` write (same
  defense-in-depth already applied to `:SOUR:SWE:ABOR`), and logs a WARNING
  via a new `:SYST:ERR?` check if a command is still rejected for any other
  reason. Full diagnosis in `LOGBOOK.md` (2026-07-22 entries) and
  `cryosoft/drivers/keithley_6221.py::set_current()`. Live-verified: forcing
  autorange ON immediately resolved ±100 µA sourcing with the error queue
  staying clean.
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
