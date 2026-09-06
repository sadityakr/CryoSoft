# configs/

## Purpose
The single source of truth for what a given setup is made of. Each
subdirectory is one **station definition**: which drivers exist, which VIs are
built on top of them, their bus addresses, and their safety limits. Everything
above the driver layer (VI, Station, Orchestrator, Procedure, GUI) runs unchanged
against any config here; swapping the config is how the same code drives real or
simulated hardware. Limits are setup properties, so they live here and never in
code.

## Architecture layer
L2 input — consumed by the Station factory (`cryosoft/core/`, see
`build_station`). Configs contain no Python; they are declarative YAML data.

## Entry (what comes in)
A config is selected by directory path (e.g. `cryosoft/configs/sim_cryostat`).
The Station factory reads that directory's two YAML files. Dotted `class:` paths
in the YAML are imported at build time, and `drivers:` names in each VI must
resolve to a key in the `real_drivers` block.

## Exit (what goes out)
A fully-wired `Station`: driver instances keyed by name, VIs constructed via
`__init__(self, drivers, **init_params)` with their `init_params` (addresses,
limits, ramp segments) taken verbatim from `devices.yaml`, and a
monitor loop configured from `monitor.yaml`.

## Interface contract
Enforced by `tests/test_conformance.py::test_config_schema`, which auto-discovers
every `configs/<name>/` directory:
- The directory must contain a loadable `devices.yaml` and `monitor.yaml`.
- Every `class:` path must import.
- Every VI `drivers:` reference must name a key defined in `real_drivers`.

`devices.yaml` structure:
- `data_directory` — where HDF5 output is written.
- `real_drivers:` — map of instance name → `{class: <dotted driver path>,
  address: <VISA resource string>}`. Real configs use `cryosoft.drivers.<real>`;
  sim configs swap in the `Sim*` class at the same address slot.
- `virtual_instruments:` — map of VI name → `{class:, drivers: {role: driver_name},
  vi_type:, init_params: {...}}`. `vi_type` is the registry role
  (`system` / `measurement`). `init_params` carries the
  control-validation limits (`min/max_temperature_K`, `max_ramp_rate_K_per_min`,
  `max_current`, field bounds) and instrument-specific setup constants such as
  a magnet's `ramp_segments` or a Lakeshore 335's `initiate_heater_range`.
- `max_source_current_A` — REQUIRED on every VI that drives current through the
  sample, in amperes. Conformance-checked per shipped config, real setups
  included, because a missing ceiling lets an action source whatever the
  instrument can deliver. It bounds the sourced current symmetrically (±,
  current reversal is routine) and, on a voltage-sourcing VI, is converted to
  an amplitude bound through the series resistance it drives. Choose it from
  the SAMPLE WIRING where that figure is documented, and from the source's own
  maximum output otherwise (105 mA for a Keithley 6221); narrow it when a
  sample's safe current is measured. It is a property of the setup, so it
  lives here, never in the VI.

- `trends:` — optional; the trend checks this setup runs (see the section
  below).

`monitor.yaml` structure: a `monitor:` block with `tick_interval_ms` (the single
QTimer tick period), `max_vi_errors` (consecutive VI-error tolerance before
escalation) and the optional `instrument_thread` (see below). Optionally a
`panels:` block — see the section after that.

### `instrument_thread:` — the way back to one thread

**The default is `true`, and a config that says nothing gets it.** The
Station, the Orchestrator, every driver and the data manager live on the
instrument thread (GLOSSARY.md's **Instrument thread**), which is the single
hardware thread standard `CLAUDE.md` states: a slow instrument read cannot
freeze the window, and there is still exactly one writer on the bus.

`instrument_thread: false` asks for the temporary `inline` mode instead —
the same design with the engine on the GUI's own thread (GLOSSARY.md's
**Inline mode**). It lives here because it is a property of the setup, not of
the code: whether this machine's VISA layer has been exercised under a second
thread. Nothing a window shows or does changes with it — the same client
adapter, the same events — so the only reason to write it is a rack whose
drivers misbehave when the thread that opened their sessions is not the GUI's,
and the line is expected to go once that rack has had a day of hardware soak
with the thread on. `inline` itself is kept for one release after the flip and
is then removed.

`CRYOSOFT_INSTRUMENT_THREAD=0` (or `=1`) overrides this file for one launch,
which is how CI runs the same GUI suite both ways.

```yaml
monitor:
  tick_interval_ms: 1000
  max_vi_errors: 3
  # Omit the line to inherit the instrument thread; write it only to refuse.
  instrument_thread: false
```

The shipped config inherits the thread. A real setup writes the line only
after deciding its rack needs `inline` mode, and deletes it again once that
rack has had a day of hardware soak with the thread on.

### `panels:` — which controls a VI's monitor card shows

Card visibility is a two-layer decision, and THIS file is the layer users
edit; nobody edits a Virtual Instrument to customize their monitor:

1. **VI default** (in code): each `@control` declares `panel=True` (shown on
   the compact card, the default) or `panel=False` (front-panel window
   only). This is the VI author's shipped judgment of common use.
2. **Config override** (here): a `panels:` entry is a per-VI **allowlist
   that replaces the defaults entirely** — it can surface a `panel=False`
   control or hide a `panel=True` one. A VI absent from the block keeps its
   defaults.

```yaml
panels:
  temperature:
    controls: [set_temperature, set_pid]  # card shows exactly these two
  dc_measurement:
    controls: []                          # card shows no controls at all
```

Example: a lab that runs constant heater power lists `set_heater_power` for
their temperature VI here; another setup omits it — same VI code, different
cards. Visibility is presentation only: every control, listed or not, stays
available in the per-VI instrument front panel (the sliders icon on the
card), and `control_limits` safety enforcement is completely unaffected.
Conformance checks every listed VI and control name against `devices.yaml`,
so a typo fails CI instead of silently rendering a bare card. The companion
write-up from the VI side is `cryosoft/virtual_instruments/README.md`
("GUI presentation").

### `trends:` — which channels a trend check watches

The **Trend check** standard (`core/trend_checks.py`, GLOSSARY.md) evaluates
advisory judgements over the trend-history store on its own slow timer. The
checks themselves are declared here, per setup, because which channel to watch
and where its safe band lies are setup facts (a controller's range, a sample's
tolerance), not framework constants. The block is optional; absent, no check
runs and the scheduler still gets its default cadence.

```yaml
trends:
  refresh_interval_s: 60.0      # how often the in-app scheduler re-evaluates
  store_live_stale_ticks: 10    # CLI-only store-liveness check: ticks of silence
  checks:
    - key: temperature_temperature   # a flat state key: <vi_name>_<monitored method>
      low: 1.0                       # band, inclusive, in the key's own unit
      high: 320.0
      window_s: 3600.0               # trailing window the check judges
      # kind: channel_within_band    # default and the only shipped kind
      # name: sample_in_band         # default "<key>_within_band"
      # severity: advisory           # default; a trend check reports, never enforces
```

Each entry becomes one `TrendCheck` (`trend_checks.declared_checks()`), keyed
`trend:<name>` in the condition registry when it fails. Conformance checks that
every declared `key` is one this config's VIs can actually produce, so a typo
fails CI instead of a check silently reporting "no data" forever; a malformed
entry (unknown field, `low >= high`) is refused at startup and by
`python -m cryosoft.troubleshoot trends`.

## How to add a new module
1. Create `configs/<name>/` with a `devices.yaml` and a `monitor.yaml`.
2. In `real_drivers`, list each instrument with its driver class and address.
3. In `virtual_instruments`, build each VI on those drivers, setting `vi_type`
   and the `init_params` limits for that setup.
4. Conformance discovers the new directory automatically; run `make check`. For a
   guided setup with identity checks and a preflight report, use the
   `setup-commission` skill (writes a per-setup `setup.md`).

## Files
Two shipped configs — one per worked example; a real setup is added as a
sibling directory with the same two files (plus its `setup.md`).

- `sim_cryostat/` — fully simulated reference station, and the station every
  procedure/orchestrator test builds against: `magnet_z`
  (`SuperconductingMagnetVI` on `SimOxfordIPS120`), `temperature`
  (`Lakeshore335SampleTemperatureControllerVI` on `SimLakeshore335`) and
  `dc_measurement` (`DCSeparateMeasurementVI` on a `SimKeithley6221` +
  `SimKeithley2182A` pair).
  - `devices.yaml` — all `Sim*` drivers at `SIM::*` addresses; the canonical VI
    graph and `init_params`.
  - `monitor.yaml` — `tick_interval_ms: 3000`, `max_vi_errors: 3`.
  - `setup.md` — the per-setup ground truth (GLOSSARY.md's **setup.md**), in
    the `setup-commission` skill's template; the shipped one doubles as the
    example a real setup starts from.
- `sim_imaging/` — fully simulated widefield-imaging station, the second
  worked example: `magnet_z` (`SuperconductingMagnetVI` on `SimOxfordIPS120`),
  `stage_x` and `stage_y` (one `StageAxisVI` per axis, both on one
  `SimXYStage`, each with its own travel limit) and `camera`
  (`CameraMeasurementVI` on `SimCamera`, with the `roi` its scalar columns
  are taken over and the exposure range). The magnet's and the camera's
  addresses carry the same `@imaging` suffix, which is what couples the
  simulated sample to the simulated field (the **sim-coupling standard**,
  `drivers/README.md`); the stage joins no environment.
  - `devices.yaml` — the VI graph above and its `init_params`.
  - `monitor.yaml` — `tick_interval_ms: 3000`, `max_vi_errors: 3`.
  - `setup.md` — as above, for the imaging station: the `@imaging` coupling
    is the one wiring fact it records.

**A real setup and its sim twin.** The intended shape for a real rack is a
pair of config directories: one naming the real `cryosoft.drivers.*` classes at
their PyVISA addresses, and a digital twin with an IDENTICAL VI graph, VI names
and `init_params`, each real driver swapped for its `Sim*` equivalent. Nothing
above the driver layer changes between them, so the twin exercises the real
setup end to end with no hardware — and a config error shows up in the twin's
conformance run rather than at the rack.

tests: `tests/test_conformance.py` (schema), `tests/test_config_catalog.py`
(discovery / copy-on-edit fork / named versions), `tests/test_config_validation.py`
(limit and reference validation).
