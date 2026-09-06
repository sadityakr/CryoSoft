# I2AS — Instrument to Agentic Station

I2AS is a framework for turning a rack of laboratory instruments into a
measurement station that a human operates from a desktop window and an
agent operates through a permission-checked tool surface, with the same
safety machinery binding both. You write the parts only you can know — how
each instrument is driven, what a measurement is, what the analysis says,
and one YAML file naming the rack and its limits — and the framework
supplies the rest: the single-writer engine that ramps, settles, measures
and saves; limit and envelope enforcement; the safety state machine; the
run queue; the HDF5 run file and its reader; the experiment record and the
lab-notebook entry; and an agent gateway whose every tool is rendered from
the declarations you already wrote.

The framework was extracted from CryoSoft, a cryostat operating system;
the two shipped examples are simulated so the whole system runs, and is
tested, without a single instrument attached.

## What you write, what you get

| You write | Size | The standard it implements | What appears without further code |
|---|---|---|---|
| A driver (real + sim twin) | 300–500 lines each | Driver contract: one class, one VISA string, `get_idn` / `close` / `safe_shutdown`, verified writes | Bench CLI, connection lifecycle, error attribution, sim/real API parity check |
| A virtual instrument | 200–500 lines | VI contract: `@monitored(unit, description)`, `@control(params=ParamSpec…, action_class=…)`, `control_limits`, `safety_flags`, ramp hooks | Monitor card, front panel, capability manifest, one agent tool per control, limit enforcement, ramp tracking, ETA, stall detection, status log |
| A procedure | ~200 lines | Six axis hooks on `SweepMeasureProcedure`, instruments found by role | Parameter form, queue validation, probe run, duration estimate, HDF5 run file, run manifest, agent `run_procedure` / `probe_run` |
| An analysis recipe | ~55–380 lines | `AnalysisRecipe.analyse(run, context)` | Out-of-process analysis, figures, the analysed notebook entry, preview-then-approve, agent `run_analysis` |
| One config folder | ~70 lines of YAML | Config schema | The station, its limits, its excitation ceiling, its trend checks, the manifest, the setup name in every record |

## Architecture

Seven layers, dependencies pointing strictly downward, every boundary
checked by import-linter:

```
  drivers (L0)  ──▶  virtual instruments (L1)  ──▶  station + config (L2)
       ──▶  orchestrator (L3)  ──▶  procedures (L4)  ──▶  data manager (L5)
       ──▶  session: experiments, runs, queue, analysis, notebook (L6)
       ──▶  agent gateway (in-process) ──▶ MCP adapter / CLI (separate processes)
  GUI: PyQt6 windows holding an Orchestrator proxy, never the engine
```

- **Drivers** are plain classes over PyVISA. Every real driver has a sim
  twin with an identical public API that models the instrument's physics and
  its refusals, so a wrong command sequence fails in a test.
- **Virtual instruments** wrap drivers in behaviour-named capabilities
  (`set_field`, `take_reading`) declared with `@monitored` / `@control`. One
  declaration feeds the GUI card, the tooltip, the capability manifest and
  the agent's tool schema.
- **The station** builds VIs from `devices.yaml` and owns the per-tick state
  snapshot and the unified condition registry.
- **The orchestrator** is a state machine (IDLE, INITIATING, RAMPING,
  MEASURING, SWEEPING, STANDBY, PAUSED, ERROR, EMERGENCY) and the sole
  writer to hardware: one timer tick drives polling, ramps, safety and the
  active procedure, and every client — the GUI and the agent alike — sends
  it a `Command`, receives exactly one `Verdict`, and sees every consequence
  as an `Event`.
- **Procedures** are sweep recipes: six hooks on `SweepMeasureProcedure`
  say which axis to sweep and what to hold; the base class runs any
  measurement VI the station has, chosen at run time.
- **The data manager** writes one HDF5 file per run; `data_reader` and the
  live `RunBuffer` answer the same read vocabulary over a finished file and
  the run in flight.
- **The session layer** records experiments and runs, validates and queues
  runs, runs analysis recipes in a worker process, and publishes the
  analysed entry to an electronic lab notebook after a human approves it.
- **The agent gateway** puts a role × action-class permission model in
  front of the control contract and renders the tool surface; the MCP
  adapter and the `ctl` CLI reach it from other processes.

**Exactly one thread — the instrument thread — ever touches the
Orchestrator, the Station, a VI, a driver or the data manager.** The GUI,
the session layer, the gateway, analysis and every network call live on the
main thread and meet the engine only through the control contract, over
queued connections carrying copies. No lock, no second writer, no blocking
call in the tick path.

## The two shipped examples

Both are fully simulated and share the magnet VI and the whole framework;
they differ only in the files the table above says a user writes.

**`sim_cryostat`** — a transport measurement under field and temperature.
A superconducting magnet (`magnet_z`, on a sim Oxford IPS 120), a sample
temperature controller (`temperature`, on a sim Lakeshore 335) and a DC
source-and-measure pair (`dc_measurement`, a sim Keithley 6221 sourcing
current into a sim 2182A nanovoltmeter). Three procedures run on it:
**Field Sweep** (ramp the field point by point, optionally holding a
temperature, take a reading at each point), **Temperature Sweep** (the same
with the axes swapped) and **Time Series** (readings on a fixed cadence
while the operator drives the station by hand, ending on a duration or when
a watched channel crosses a value). The DC VI declares one loopable
parameter, so the reading loop — several excitation currents per sweep
point, stored on a real array axis — is demonstrated here.

**`sim_imaging`** — a widefield camera imaging magnetic domains under
field. The same magnet, a two-axis sample stage (`stage_x` / `stage_y`, one
VI per axis over one sim driver) and a camera (`camera`, a sim whose frame
is a domain pattern that switches with the applied field, with hysteresis).
The magnet and the camera share a *sim environment* — both addresses carry
an `@imaging` suffix — so the simulated sample responds to the simulated
field without either driver importing the other. **Field Imaging** sweeps
the field from a saturated start and takes `frames_per_step` exposures at
each point, storing each averaged frame as an *image block* in the run
file beside the ROI-mean scalar the live plot shows. The
`field_image_stack` recipe then renders a montage of frames against field,
difference images against the reference frame, and the ROI-mean hysteresis
loop with a coercive-field estimate.

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # equivalently: make install
```

`pyproject.toml` is the single source of truth for dependencies. Two
optional extras: `analysis` adds matplotlib for figures in analysis
recipes (a recipe that draws nothing needs nothing; `dev` already includes
it), and `mcp` adds the official protocol package as an alternative framing
for the MCP adapter (the stdlib shim is the default and the one the tests
run).

The session layer needs a **measurement root** — the machine-level folder
every experiment and run record lives under. Set `I2AS_MEASUREMENT_ROOT`
or write `measurement_root:` into the machine-level `App-config.yaml`
(`core/paths.py` documents the precedence); an unconfigured installation
refuses to start rather than inventing a location.

## Run

**The desktop application:**

```bash
i2as --config sim_imaging          # equivalently: python -m i2as.main --config sim_imaging
```

`--config` names a shipped config or a user copy of one and opens it for
this launch. Without it, the app opens the *active config* — the one saved
on this machine from the last successful launch — and falls back to the
always-loadable `sim_cryostat`. The startup chain is `--config → active
config → its shipped baseline → sim_cryostat`, so a broken config produces
a warning instead of a crash. To make a config the persistent default
instead, mark it active once:

```bash
python -c "from i2as.gui import app_settings; app_settings.set_config_active('sim_imaging', 'shipped')"
```

Two windows open: the Monitor window (instrument cards, trends, the
experiment header, the ramp tracker and the agent panel with the kill
switch and attendance controls) and the Procedure window (parameter form,
run queue, two live plots, and the eLab tab where an analysed entry is
previewed and published).

**The MCP server**, for an agent session driving a *running* application:

```bash
python -m i2as.mcp --role observer --actor-id my-agent
```

It finds the running app through the descriptor the app publishes beside
its socket, and serves only when the app's `monitor.yaml` says
`gateway_server: true`; the app hands out at most the role its
`gateway_max_role` names (both default closed). `.mcp.json` at the
repository root declares this server for a Claude Code session.

**The reference CLI**, one JSON answer per call over the same tool surface:

```bash
python -m i2as.ctl --offline sim_cryostat tools          # build a sim station in-process
python -m i2as.ctl --offline sim_imaging call read_station_info
python -m i2as.ctl --role observer status                # live: through the request spool
```

Live mode writes one request file into the running app's request spool
(`monitor.yaml`: `request_spool: true`, capped at `spool_max_role`) and
reads the verdict the tick appends. Exit codes are the API: `0` answered
`ok`, `1` refused or failed (`detail.rule` says why), `2` no engine reached.

**The doctor CLI**, for a setup with the application closed:

```bash
python -m i2as.troubleshoot check --config sim_cryostat --json   # preflight every driver
python -m i2as.troubleshoot status --max-age 30                  # what a RUNNING app is doing
python -m i2as.troubleshoot session                              # report on the newest experiment
```

## The agent surface

Every tool is rendered, never written: one command tool per engine command,
one capability tool per `(instrument, @control)` the station declares, and
twenty hand-declared session tools that read the store, the run files and
the audit trails or reach the notebook and the analysis stage. An agent
connects under a **role**; every action has an **action class**; one table
decides:

| Action class | `observer` | `debug` | `session` | operator (human) |
|---|---|---|---|---|
| **read** | permitted | permitted | permitted | permitted |
| **recovery** | refused | unattended only | permitted | permitted |
| **run_control** | refused | refused | permitted | permitted |
| **envelope** | refused | refused | refused | permitted |

The human is not in the table: `authorize()` never refuses an operator.
`emergency_standby` is exempt — permitted to every role, in every state, at
every kill-switch setting. The envelope row belongs to nobody: the session
envelope, attendance and the kill switch are the rules the other rows are
judged by.

**Command tools** (`session/gateway/action_classes.py`):

| Class | Tools |
|---|---|
| run_control | `run_procedure`, `queue_procedure`, `run_queue`, `abort_procedure` (owner-scoped: an agent that did not start the run needs `override_owner` and a `reason`, recorded as a takeover), `acknowledge` |
| recovery | `pause_procedure`, `resume_procedure`, `submit_global_action`, `stop_ramp`, `connect_instrument`, `disconnect_instrument`, `ping_instrument`, `acknowledge_fault`, `retry_fault`, `recover_from_error`, `start_monitoring`, `stop_monitoring`, `emergency_standby` (exempt from the matrix) |
| envelope | `set_experiment_envelope`, `set_attendance`, `set_agent_gate` |

**Capability tools**, named `<instrument>__<control>`, carry the class each
VI declares on its `@control(action_class=…)`, with the config's limits as
the schema bounds. On the shipped setups:

| Instrument | run_control | recovery | read |
|---|---|---|---|
| `magnet_z` | `set_field` | | |
| `temperature` | `set_temperature`, `set_heater_mode`, `set_heater_output`, `set_heater_range` | `set_ramp_rate`, `set_pid`, `set_curve` | |
| `dc_measurement` | `initiate_measurement`, `set_source_current` | | `read_now` |
| `stage_x`, `stage_y` | `set_position` | `stop` | |
| `camera` | `initiate_measurement` | | `read_now` |

The two lifecycle actions every VI has, `initiate` and `standby`, are
`recovery`.

**Session tools** (`session/gateway/tools.py`, `SESSION_TOOLS`):

| Class | Tools |
|---|---|
| read | `read_status`, `read_station_info`, `read_manifest`, `list_runs`, `read_run_columns`, `read_run_slice`, `read_run_stats`, `read_run_metadata`, `validate_run`, `read_experiment`, `read_operational_log`, `read_agent_feed`, `draft_eln_entry` (recorded), `list_analysis_recipes`, `read_analysis_recipe`, `read_analysis_report` |
| run_control | `probe_run` (a real, reduced run), `publish_eln_entry` (recorded; refused for every agent while the experiment is attended — the draft is parked for the human), `write_analysis_recipe` (recorded), `run_analysis` (recorded) |

`validate_run` answers "may this be queued, and how long will it take?"
without touching hardware; `probe_run` runs the same procedure on the same
instruments through the same code path, subsampled to a few points, so a
wrong column or an unreachable setpoint shows up in minutes. The MCP
adapter also serves three resources — `i2as://status`,
`i2as://station`, `i2as://manifest` — over the matching read tools.

## Safety

Every writer — the operator's click, a procedure's plan, an agent's tool
call — passes the same checks at the single hardware writer:

- **Control limits from config.** Every bounded `@control` parameter names a
  limit in `control_limits`; the value comes from the setup's `init_params`
  and the base class refuses an out-of-range call before any bus command.
  Conformance fails a numeric control parameter with no limit and no written
  physical reason.
- **The excitation ceiling.** Every VI that drives current through the
  sample must be given `max_source_current_A` by its config; the framework
  turns it into a symmetric bound on every current-commanding control.
- **Safety flags with severities.** A VI declares `safety_flags` — each flag
  its `evaluate_safety()` can report, mapped to `advisory`, `hold` or
  `critical`. A hold stands down the VIs that declare a concern for it and
  keeps them there, every tick, until the flag clears; a **critical flag**
  (the sim magnet's `quench`) drives the whole station to EMERGENCY, and the
  operational-status record reports it as `CRITICAL_FLAG` whatever the flag
  was called.
- **The session envelope.** Per-experiment bounds narrower than the setup's
  limits, set by the human when the experiment opens, checked on every
  planned target, every manual setpoint and every live reading; a violation
  is a critical condition.
- **Attendance and the kill switch.** Two values the session layer pushes
  into the engine: whether a human is watching (a `debug` agent may take
  recovery actions only when nobody is), and the tri-state gate
  (`active` / `read_only` / `revoked`) enforced inside `Orchestrator.submit()`
  for every agent command — never for the human, and never for
  `emergency_standby`.
- **Run ownership.** The actor that started the run owns it; an agent that
  did not may not abort it without an explicit, recorded takeover.
- **Verdicts.** Every command is answered exactly once with a code (`OK`,
  `BLOCKED_STATE`, `BLOCKED_CLAIM`, `BLOCKED_FAULT`, `BLOCKED_LIMIT`,
  `BLOCKED_ENVELOPE`, `BLOCKED_ROLE`, `FAILED`) and a structured `detail`,
  so a client decides from data, never by parsing prose.
- **The agent feed.** Every command a non-operator actor submitted, every
  verdict answering one, every state change it caused and every recorded
  tool call is appended to the experiment's `agent_actions.jsonl`, joined by
  request id to the engine's per-tick status log.

## Adding to a station

Each kind of file has exactly one shipped exemplar to copy and one folder
README stating the standard it implements. Conformance tests auto-discover
new files, so a new module is checked the moment it exists.

**A driver.** Copy `i2as/drivers/lakeshore_335.py` and its twin
`sim_lakeshore_335.py`: one public class, `__init__(self, resource: str)`,
`get_idn()`, `close()`, `safe_shutdown()`, and every state-changing write
verified (error queue, status byte, protocol acknowledgement or readback).
The sim models the physics and the refusals. Standard: `drivers/README.md`.

**A virtual instrument.** Subclass the typed base (`MagnetBase`,
`TemperatureControllerBase`, `StageBase`, `MeasurementInstrumentBase`), add
`RampableVI` if it ramps, tag reads `@monitored(unit=, description=)` and
actions `@control(params=, action_class=)`, declare `control_limits` and
read every limit from `init_params`. `virtual_instruments/stage/stage_axis.py`
is the smallest rampable example; `measurement/camera.py` the measurement
example with an image block; `measurement/dc_separate_measurement.py` the
one with a loopable parameter. Standard: `virtual_instruments/README.md`
and `MeasurementInstrumentBase`'s docstring.

**A procedure.** Copy `i2as/procedures/field_sweep.py`: subclass
`SweepMeasureProcedure`, declare a `sweep_axis`, name the instrument roles
you need in `role_parameters`, and implement the six axis hooks. The
parameter form, queue validation, probe run, duration estimate and agent
tools follow. Standard: `procedures/README.md`.

**An analysis recipe.** `python -m i2as.analysis new-recipe <name>
--dir <experiment>/analysis/recipes` scaffolds one;
`analysis/recipes/field_image_stack.py` is the shipped example serving one
procedure. A recipe reads the run only through the run-source vocabulary and
draws only through its context. Standard: `analysis/README.md` and
`AnalysisRecipe`'s docstring.

**A config.** Copy `i2as/configs/sim_cryostat/`: `devices.yaml` names
the drivers, the VIs on them and every limit; `monitor.yaml` the tick period
and the gateway, spool and thread switches; `setup.md` the wiring and quirks
a future reader needs. Standard: `configs/README.md`.

## Connecting a real rack

1. Write the real driver and its sim twin (the parity test pairs them by
   filename). Keep the sim honest: it should refuse what the instrument
   refuses.
2. Create a config directory naming the real driver classes at their VISA
   addresses, and a *twin* config with the identical VI graph on the sim
   classes, so the whole setup runs end to end with no hardware and a config
   error shows up in the twin's conformance run.
3. Add `expect_idn` to every `real_drivers` entry once the real identity
   string is known — a case-insensitive substring the instrument's
   `get_idn()` must contain, so a swapped cable is caught as `WRONG_IDN`.
4. Write `setup.md` from the shipped template: instrument purposes, wiring,
   safe-testing overrides, dated quirks.
5. Run the preflight with the application closed —
   `python -m i2as.troubleshoot check --config <name> --json` — and fix
   one fault at a time; `bench-l0` reads one passive getter per driver at
   zero excitation. The `setup-commission` and `setup-supervisor` skills in
   `.claude/skills/` are the guided form of this workflow.

## Testing

```bash
make check     # ruff check . && lint-imports && pytest -m "not hardware", both thread modes
```

`make check` is the gate; CI runs exactly these targets. Three parts:
the numbered import-linter contracts in `pyproject.toml` enforce the layer
boundaries; `tests/test_conformance.py` auto-discovers every driver, VI,
procedure, config and recipe and checks it against its standard (if it
fails on your module, fix the module); and the behaviour suites cover each
layer. The GUI suite runs twice — on the instrument thread (the default)
and in the temporary `inline` mode (`make test-instrument-inline`,
`I2AS_INSTRUMENT_THREAD=0`) — which is what keeps "nothing above the
thread boundary can tell" true. Tests marked `hardware` need instruments
and are excluded.

## Design record

The standards live where the code is: in the base-class docstrings
(`BaseVirtualInstrument`, `MeasurementInstrumentBase`, `BaseProcedure`,
`AnalysisRecipe`, `Orchestrator`), in each folder's `README.md` (Purpose,
Architecture layer, Entry, Exit, Interface contract, How to add a new
module, Files) and in `GLOSSARY.md`, the canonical vocabulary. Comments cite
those and never a design document, so the repository presents the complete
picture on its own. `CLAUDE.md` is the contributor guide.

## Licence

MIT — see `LICENSE`.
