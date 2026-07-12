# Triage tree — symptom → commands → interpretation

Always start at L0 of the excitation ladder: passive evidence (logs), then
`check --json`. Branch on what comes back. Every path ends in one of the three
terminal states (software fix / hardware handoff / incident report).

## Entry points by symptom

| Symptom (as humans phrase it) | First moves |
|---|---|
| "Instrument X is dead / no reading" | `check --json`; branch on X's FaultCode below |
| "Values are frozen / not updating" | grep `cryosoft.log` for `_stale` / `disconnected` on that VI; then `read <alias> <getter> --repeat 10` |
| "Readings are intermittent / sometimes fail" | `read --repeat 20 --interval 0.2`, then again `--interval 2`; compare failure rates |
| "Values are nonsense / wrong magnitude" | `idn <alias>` (is it the right instrument?); `read` the raw getter; compare against the unit conventions (SI everywhere) and the VI's conversion init_params |
| "App won't start / starts in sim mode unexpectedly" | the startup fallback fired: run `check --config <intended>` — expect `CONFIG_INVALID` or a hardware fault; also read the startup banner text in the log |
| "Ramp never finishes / magnet won't move" | log first (ramp status transitions), then `read` the magnet getters; do NOT write to a magnet without the human confirming system state |
| "New driver doesn't work" | `probe <address> --idn-command <cmd>` raw, then `methods` / `read` via `--address` ad-hoc bench; compare raw reply vs driver parsing |

## FaultCode branches

For each code: causes in ranked order, the discriminating next step, terminal
state.

### `CONFIG_INVALID`
Software (L2). Read the detail string: missing file, bad YAML, or unimportable
class. Fix the config or the import; re-run `check`. Terminal: software fix.

### `ADDRESS_NOT_ON_BUS`
Physical, almost always. In order: instrument powered off → cable unplugged /
broken → wrong address in `devices.yaml` vs the instrument's switch → (serial
only) USB-serial adapter unplugged, so the port itself vanished.
Discriminate: `scan` — if a *similar* address is present (e.g. GPIB0::12
expected, GPIB0::13 listed), `probe` the listed one and compare the IDN to the
expected instrument; that finds address-switch mismatches without touching
hardware. Terminal: hardware handoff (one instruction at a time), except the
"config had the wrong address" case, which is a software fix + `setup.md`
quirk entry.

### `OPEN_FAILED`
The resource exists but will not open. Causes: the main CryoSoft app (or NI
MAX, or another session) still holds the port → wrong resource type in the
address string. Discriminate: ask the human to confirm the app is closed;
retry; check the address format against `scan` output. Terminal: usually
procedural (close the other program), else software fix (address string).

### `NO_RESPONSE`
Opens but silent. Causes: (serial) wrong baud/termination settings → wrong
protocol (SCPI `*IDN?` sent to a pre-SCPI Oxford instrument — retry `probe`
with `--idn-command V`) → instrument hung (human power-cycles) → dead
interface board. Discriminate: cheat sheet (`manuals/notes/`) for the correct
serial parameters and identify command; try the protocol-correct probe before
concluding hardware. Terminal: software fix if protocol/config, hardware
handoff otherwise.

### `WRONG_IDN`
An instrument answered — the wrong one. Causes: swapped cables or swapped
addresses between two instruments (classic after re-racking) → stale
`expect_idn` after an instrument was replaced. Discriminate: `idn` every
configured alias and build the actual address→identity map; propose the
minimal swap that fixes it. Terminal: hardware handoff (re-cable) or software
fix (correct the config), plus a `setup.md` note either way.

### `GARBLED_RESPONSE`
Something answered with junk. Causes: (serial) baud mismatch → wrong
termination characters → driver parsing bug. Discriminate: `query <alias>
"<identify cmd>"` (gated: needs approval) and inspect the raw bytes — if raw
looks right but the driver's `read`/`idn` mangles it, the fault is L0 driver
parsing by construction. Terminal: software fix (driver) or hardware/settings
handoff (instrument's comm settings panel).

### `DRIVER_ERROR`
A Python exception that is not a communication error: the driver itself is
buggy (or the instrument returned something the driver never anticipated —
which is still a driver bug: drivers must fail as communication errors, not
crashes). Terminal: software fix in the driver, with a regression test.

### All `OK` but the symptom persists
The fault is above L0: VI conversion/logic (L1), init_params (L2), tick timing
(L3), or procedure logic (L4) — or physical but invisible to VISA (sample
wiring, thermal contact). Move to the runtime log around the failure time,
reproduce with the sim config if the logic is suspect (`--config
sim_cryostat`), and compare. If the software chain is clean end-to-end, hand
the physical checklist (wiring, sample contact) to the human.

## Discriminator tests (reusable moves)

- **Raw vs driver:** same request via `query` (raw) and via `read` (driver).
  Raw OK + driver fails = L0 bug, no judgment required.
- **Repeat/interval sweep:** failure rate that drops as `--interval` grows =
  timing/settling fault (add/lengthen waits in the driver, or lengthen the
  Orchestrator tick), not a dead instrument.
- **Sim substitution:** run the same flow against `sim_cryostat`. Works on
  sim + fails on real with identical software = the fault is at L0 or below.
- **Identity sweep:** `idn` across all aliases to catch swaps in one pass.

## Exit discipline

State the diagnosis with its evidence and a confidence level. If confidence
is low after the tree is exhausted, that IS the incident-report branch — do
not force a conclusion, and never apply a speculative fix to make a symptom
disappear.
