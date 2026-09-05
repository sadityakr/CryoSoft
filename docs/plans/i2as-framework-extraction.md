# I2AS — Instrument to Agentic Station: extracting the framework from CryoSoft

**Status:** Approved 2026-09-05 (two rounds of decisions, §2 and §12). Execution
starts with phase 1. Branch `feature/i2as-framework` (from the integration branch
head `c5392da`, which carries the analysis stage).
**Scope:** turn this repository into a self-standing, publishable framework in which a
user writes only virtual instruments, procedures, analysis recipes and one YAML
config, and gets an agent-operable measurement station in return. Everything
cryostat-specific leaves; two worked examples stay: a trimmed sim cryostat
(transport under field and temperature) and a new sim imaging setup (a camera
under field), so the repository shows two classes of procedure, not one.
**Date:** 2026-09-05
**Survey basis:** four parallel full-read surveys of `core/ctl/troubleshoot/mcp`,
`drivers/virtual_instruments/procedures/configs`, `gui/` + `main.py`, and
`session/analysis/tests/docs`. Every keep/drop decision below names its file.

---

## 1. Problem

CryoSoft already contains the whole idea: independent drivers → virtual
instruments with machine-readable capability cards → a single-writer station
machine that enforces limits, envelopes and safety flags → a session with data,
analysis and a lab notebook → an agent gateway over all of it. But the repo is
86 k lines of source plus 62 k of tests, and a large share of it is one
laboratory's cryostat: helium levels and fills, a switch matrix, a rotator, a
persistent-mode magnet, a servicing log, three real racks, eleven instruments
and their real drivers. A PhD student opening this repo sees a cryostat
operating system, not a framework they can point an agent at.

The paper's claim is narrower and stronger: *describe the instrument and the
science; the framework supplies the orchestration, the safety and the agent.*
The repository that ships with the paper has to look like that claim.

## 2. Decisions taken (with the user, 2026-09-05)

| Question | Decision |
|---|---|
| Name | Package `i2as`, repo `i2as`, spelled **I2AS** in prose: Instrument to Agentic Station. |
| GUI | Minimal operator GUI: monitor window, procedure/queue window with the eLab tab, eLab setup dialog, agent feed and takeover strip. Servicing log, operations panel, config editor, diagnostics window, debug menus go. |
| Worked example | Trimmed sim cryostat: sim magnet, sim temperature controller, one sim source+meter measurement pair; one real/sim driver twin as the driver-contract exemplar; Field Sweep as the primary procedure. |
| Delivery | This plan first; then execution in phases on the branch, `make check` green after each. The branch head is what becomes the public repo. |
| Second example | **Imaging with field, widefield camera** (MOKE/Kerr-microscope style): a sim camera returns a frame per exposure, a sim XY stage positions the sample, the magnet sweeps. One frame per field step; the recipe renders a montage and an ROI hysteresis loop. §5.8, §6.8. |
| Design documents | **Not shipped.** `docs/plans/` leaves the repository entirely in phase 6; the README carries the architecture story on its own. This plan stays in CryoSoft's history and on the branch until then. |
| Second-round confirmations | Embedded assistant leaves the core; Field Sweep, Temperature Sweep and Time Series all stay; the Lakeshore VI variant is the temperature example; MIT unchanged; `sim_cryostat`/`magnet_z` keep their names, `temperature_vti` → `temperature`; import-linter contracts lose only the lines naming deleted modules, never a rule. |

## 3. What the user writes, what the framework gives

This table is the thesis of the framework and belongs at the top of the new
README. Line counts are today's shipped examples.

| The user writes | Size | Standard it implements | What appears without further code |
|---|---|---|---|
| A driver (real + sim twin) | 300–500 lines each | Driver contract: one class, one VISA string, `get_idn/close/safe_shutdown`, verified writes | Bench CLI, connection lifecycle, error attribution, sim parity check |
| A virtual instrument | 200–500 lines | VI contract: `@monitored(unit, description)`, `@control(params=ParamSpec…)`, `control_limits`, `safety_flags`, ramp hooks | Monitor card, front panel, capability manifest, one MCP tool per control, limit enforcement, ramp tracking, ETA, stall detection, status log |
| A procedure | ~170 lines | Six axis hooks on `SweepMeasureProcedure` | Parameter form, queue validation, probe run, duration estimate, HDF5 run file, run manifest, agent `run_procedure`/`probe_run` |
| An analysis recipe | ~55–380 lines | `AnalysisRecipe.analyse(run, context)` | Out-of-process analysis, figures, the analysed notebook entry, preview-and-approve, agent `run_analysis` |
| One config folder | ~65 lines YAML | Config schema | The station, its limits, its ceilings, the manifest, the setup name in every record |

Two shipped setups make the point that the framework is not about one kind
of physics: `sim_cryostat` (a transport measurement swept in field and
temperature) and `sim_imaging` (a camera frame per field step over a
positioned sample). They share the magnet VI and the whole framework; they
differ only in the files the table above says a user writes.

And what the framework guarantees for the agent, in the vocabulary of the
nine-module framework (`agentic-instrumentation-framework.md`): a generated
capability manifest (M1), per-tick structured status (M2), read access to
every run's data (M3), one verdict per command (M4), roles × action classes,
envelope, attendance and kill switch enforced at the single writer (M5),
idempotent safe state per instrument (M6), validate/estimate/probe before
commitment (M7), an append-only agent feed (M8), and an MCP gateway plus a CLI
client (M9).

## 4. Target layout and size

```
i2as/
  core/                 contract + engine (events, plan, decorators, station, orchestrator, procedure, data)
  drivers/              4 sim + 2 real: the driver contract and its exemplar twin
  virtual_instruments/  base, rampable, magnet/, temperature/, stage/, measurement/ (transport + camera)
  procedures/           field_sweep, temperature_sweep, time_series, field_imaging
  configs/sim_cryostat/ transport example (+ setup.md template)
  configs/sim_imaging/  imaging example
  session/              experiments, runs, run queue, agent feed, maintenance log, analysis runner
  session/eln/          adapter contract, eLabFTW, sim, outbox, publisher, templates, settings
  session/gateway/      roles, action classes, tools, gateway, local server
  analysis/             recipe contract, discovery, worker, three recipes
  gui/                  33 modules: monitor, procedure/queue, eLab tab, agent feed, dialogs
  mcp/  ctl/  troubleshoot/   MCP server, CLI client, doctor CLI
  main.py
tests/                  ~70 files, conformance suite derived from the package name
docs/user-docs/
.claude/skills/         write-measurement-vi, setup-commission, setup-supervisor, measure-session, troubleshoot-runtime, gui-edit
```

Estimated size after the cut (source lines, excluding tests):

| Package | Today | After | Note |
|---|---|---|---|
| core | 24 500 | ~13 000 | operations, switch, magnet specifics, cryogenics out; config readers split out |
| drivers | 8 200 | ~2 600 | 6 kept files + sim camera + sim stage |
| virtual_instruments | 7 500 | ~4 400 | base + rampable + 4 kept VIs + camera + stage |
| procedures | 2 100 | ~900 | 3 kept axis procedures + field imaging, no operations |
| session | 18 300 | ~15 600 | maintenance log generalised, assistant out |
| analysis | 2 500 | ~2 800 | + the image-stack recipe |
| gui | 17 300 | ~13 700 | 6 modules dropped, monitor window trimmed |
| mcp + ctl + troubleshoot | 6 100 | 6 100 | 12 string edits |
| **total** | **~86 000** | **~58 500** | about half of what remains is docstring: the standards live there; the imaging example adds ~1 500 |

Conciseness for the reader comes from §3, not from the total: the surface a
student touches is a few hundred lines per file kind, and every file kind has
exactly one shipped exemplar to copy. A later, optional pass can move the
long standard docstrings into `docs/standards/` (§13).

## 5. Inventory: keep, drop, generalise

### 5.1 core/

Keep verbatim: `decorators.py`, `events.py` (reword 4 docstring examples),
`availability.py`, `gates.py`, `estimates.py`, `sweep_builder.py`,
`run_builder.py`, `config_catalog.py`, `capability_manifest.py` (edit the two
`kind`/`role` enum descriptions), `orchestrator_proxy.py`, `status_mirror.py`,
`instrument_host.py`, `request_spool.py`, `data_manager.py`, `data_reader.py`,
`run_buffer.py`, `tiered_trend_logger.py`, `trend_history.py`,
`trend_check_runner.py`, `logging_config.py`, `exceptions.py`, `paths.py`.

Drop: `operation.py` (984 lines; the servicing-operation contract has no user
in the example and its concept is cryostat servicing).

Split or generalise:

| File | Change |
|---|---|
| `station.py` | Remove `magnet_vi_names`, `persistent_mode_magnets`, `switch_vi_names`, `set/get_scanner_enabled`, `measurement_selector_label`, `_CRYOGENICS_DEFAULTS`, `read_cryogenics_config`, `read_servicing_logs_config`, `_SAMPLE_ACCESS_DEFAULTS`, `read_operations_config`, helium keys in `_TREND_DEFAULTS`, the `sim_cryostat` fallback literal. Move the remaining `read_*_config()` readers into `core/config.py`. Add `vi_names_by_base(cls)` so procedures discover instruments by role (§6.3). |
| `orchestrator.py` | Remove the operations subsystem (10 methods, 393 lines), `set_scanner_enabled`/`scanner_enabled`, the persistent-magnet guard; drop `RUN_OPERATION`, `QUEUE_OPERATION`, `CONFIRM_OPERATION`, `SKIP_OPERATION_STEP`, `FINISH_OPERATION`, `SET_SCANNER_ENABLED` from `CommandName`; `OWNER_SCOPED_COMMANDS` shrinks to `abort_procedure`. Remove the documented-dead `RunFaultCode.STALLED_RUN`. |
| `plan.py` | Drop `Target.persistent` (magnet persistent mode leaked into the generic target). |
| `stall_detection.py` | `_NO_MOTION_PHASES` becomes a VI declaration (`RampableVI.no_motion_phases`, default empty). |
| `operational_status.py` | Replace `RunFaultCode.QUENCH` and the hardcoded `magnet_status == "QUENCH"` read with `RunFaultCode.CRITICAL_FLAG`, raised when any VI's `safety_flags` entry of severity `critical` is active (§6.2). |
| `trend_checks.py` | Keep the declaration/runner mechanism; delete `helium_consumption_normal`; generalise the sample-temperature check into `channel_within_band` whose channel key and band come from the config `trends:` block. |
| `procedure.py` | Remove the switch-matrix branch (`switch_vi_names`/`scanner_enabled`); replace the hardcoded magnet/temperature names with role discovery (§6.3). |
| `conditions.py`, `ramps.py`, `trend_history.py`, `request_spool.py`, `README.md` | Docstring rewording only. |

### 5.2 drivers/

| Keep | Why |
|---|---|
| `lakeshore_335.py` + `sim_lakeshore_335.py` | The driver-contract exemplar twin: pure PyVISA SCPI, status-byte verification, filename-paired so parity is enforced, and load-bearing (the example temperature controller runs on it). |
| `keithley_2182a.py` + `sim_keithley_2182a.py` | Second, smaller specimen of the other verification form (error queue); the meter half of the measurement pair. |
| `sim_keithley_6221.py` | Source half of the measurement pair (sim only; the real 6221 with its delta engine is 783 lines of one lab's needs). |
| `sim_oxford_ips120.py` | The only magnet sim; models ramping and the critical fault. |

Drop: `keithley_6221.py`, `keithley_705.py`, `sim_keithley_705.py`,
`sim_keithley_2400.py`, `oxford_itc503.py` (pymeasure), `sim_oxford_itc503.py`,
`oxford_mercury_ips.py`, `oxford_ilm200.py`, `sim_oxford_ilm200.py`,
`oxford_ilm210.py`, `sim_oxford_ilm210.py`, `sim_lockin.py`, `sim_rotator.py`,
`tensormeter_rtm2.py`, `sim_tensormeter_rtm2.py`. The `rtm2` git dependency
and `pymeasure` leave `pyproject.toml`.

### 5.3 virtual_instruments/

Keep: `base.py`, `rampable.py`, `magnet/superconducting_magnet.py`,
`temperature/sample_temperature_controller.py`,
`temperature/lakeshore_335_sample_temperature_controller.py` (the only shipped
example of dynamic `control_param_specs()` choices under the purity rule),
`measurement/dc_separate_measurement.py` (declares the reference
`reading_setters` entry, so the reading loop stays demonstrable).

Drop: `level/`, `rotator/`, `switch/`, `magnet/superconducting_magnet_persistent.py`,
`magnet/switch_heater.py`, `temperature/vti_temperature_controller.py`,
`measurement/{dc_single_instrument, measurement_dc_mode, measurement_delta_mode,
lockin_harmonic, tensormeter_rtm2_measurement}.py`.

In `base.py`: `LevelMeterBase` and `RotatorBase` go; `MagnetBase.safety_concerns()`
returns the empty default (nothing produces `helium_low` any more).
`MagnetBase.safety_flags = {"quench": "critical"}` stays: it is the example of a
critical flag and drives §6.2.

### 5.4 procedures/ and configs/

Keep `field_sweep.py`, `temperature_sweep.py`, `time_series.py` with the
hardcoded VI names replaced per §6.3. Drop `operations/` entirely.

One config, `configs/sim_cryostat/`: `magnet_z` (SuperconductingMagnetVI on
SimOxfordIPS120), `temperature_vti` renamed `temperature`
(Lakeshore335SampleTemperatureControllerVI on SimLakeshore335), `dc_measurement`
(DCSeparateMeasurementVI on SimKeithley6221 + SimKeithley2182A). About 65
lines, plus `monitor.yaml` and a `setup.md` written from the 12t template.
Drop `12t-cryo/`, `a-sample-real-cryostat/`, `sim_real_cryostat/`; the
README keeps the idea of a real config and its sim twin as a paragraph.

### 5.5 session/

Keep verbatim: `agent_feed.py`, `analysis_runner.py`, `manager.py`,
`models.py`, `run_queue.py`, `store.py`, all of `eln/`, and `gateway/`
except one split.

| File | Change |
|---|---|
| `servicing_log.py` → `maintenance_log.py` | Keep `LogKindSpec`, `DECLARED_LOG_KINDS`, `ServicingLogStore` (renamed `MaintenanceLogStore`) and the revision model. Drop `HeliumRecordStore`, `consumption_rate_pct_per_h`, `CryogenicsRecorder`, `migrate_legacy_servicing_log`, the three cryogen kinds. Ship one neutral `maintenance` kind so the conformance section has a subject. |
| `gateway/action_classes.py` | Keep `ActionClass`, `COMMAND_ACTION_CLASSES`, `LIFECYCLE_ACTION_CLASSES`, `classify_*`. Delete `CONTROL_ACTION_CLASSES` (per-instrument rows for one rack) in favour of the declaration on the control itself (§6.1). |
| `gateway/tools.py` | Six command tools disappear with their commands (27 → 21). The 20 session tools stay. |
| `assistant/` | Leaves the core (§12, decision 1). |

### 5.6 analysis/

Unchanged, all nine files. Zero cryostat coupling.

### 5.7 gui/ and main.py

Drop six modules (3 333 lines): `servicing_log_page.py`, `operations_panel.py`,
`config_editor.py`, `config_menu.py`, `diagnostics_window.py`,
`assistant_dock.py`.

Trim inside kept files:

- `monitor_window.py` (2 032 → ~1 720): the six cryostat constructor kwargs,
  the operations sub-panel and its splitter constants, `_on_operation_status`,
  `_on_run_finished_for_logs`, `_current_person_for_logs`, the scanner
  checkbox, the Diagnostics and Config menus. `LogPanel` moves to a bare Logs
  page (keeps `page_tab_bar`/`page_stack`). "Instrument Info…" moves to the
  User menu.
- `main.py` (~90 lines): the cryogenics block, the assistant block, the
  operations half of `run_catalog`, six kwargs, the fallback config name.
- `trends_quadrant.py`: both copies of the `magnet_z` key-migration map.
- `procedure_discovery.py`: `discover_operations()`.
- `queue_panel.py`, `procedure_window.py`, `param_form.py`,
  `monitor_history.py`, `ramp_tracker_panel.py`, `instrument_panel.py`:
  reword comments and docstring examples.
- `theme.py`: the `assistant_chip` block. Keep `verdict_badge` (the envelope
  editor uses it).

`pyproject.toml` contract C8's exception list loses `config_editor` and
`operations_panel`.

### 5.8 New: the imaging example

All new code, written to the existing standards so conformance covers it the
moment the files exist. (§5.9 below lists the packages shipped as-is.)

| File | Lines (est.) | What it is |
|---|---|---|
| `drivers/sim_camera.py` | ~250 | Sim widefield camera: exposure, binning, ROI, `get_frame()` → 2D `uint16` array. Frame physics: a domain pattern that switches with the applied field with hysteresis (coercive field, nucleation noise), so a field sweep produces a recognisable loop. The field is fed in by the sim station the same way the sim magnet's sim meter pair shares state today. |
| `drivers/sim_xy_stage.py` | ~200 | Sim XY sample stage: `move_to(x, y)`, `position()`, finite speed, travel limits, `stop()`. |
| `virtual_instruments/stage/xy_stage.py` (+ `StageBase` in `base.py`) | ~300 | `XYStageVI`: rampable in two axes, `@control(move_to, params=x_m, y_m)` with `control_limits` from the config travel range, `@monitored` position, `stop` as a recovery action. The second rampable class beside magnet and temperature, and the second subject of `vi_names_by_base()`. |
| `virtual_instruments/measurement/camera.py` | ~350 | `CameraMeasurementVI`: `measurement_parameters` exposure_s, binning, frames_per_step; one image block `frame` (§6.8); scalar columns `roi_mean` / `roi_std` from a config-declared ROI so the live plot and the generic sweep recipe still have a scalar to show. |
| `procedures/field_imaging.py` | ~200 | `FieldImaging(SweepMeasureProcedure)`: the field axis as in Field Sweep, plus stage position as system parameters (`stage_x_m`, `stage_y_m`), a saturation pre-step so the first frame is the reference, and `frames_per_step` averaging. Same six hooks; the reference frame is what makes it a distinct procedure class rather than Field Sweep with a camera. |
| `analysis/recipes/field_image_stack.py` | ~300 | Serves `FieldImaging`: montage of frames against field (subsampled to ≤ 12 panels), difference images against the reference frame, and the ROI-mean hysteresis loop with coercive-field estimate as `ResultValue`s. |
| `configs/sim_imaging/` | ~60 | `magnet_z` (SuperconductingMagnetVI on SimOxfordIPS120), `stage` (XYStageVI on SimXYStage), `camera` (CameraMeasurementVI on SimCamera), `roi` and travel limits in `init_params`. |
| Tests | ~900 | `test_l0_sim_camera_stage.py`, `test_l1_camera_stage_vis.py`, `test_field_imaging_procedure.py`, recipe cases in `test_analysis.py`, an end-to-end run in `test_analysis_end_to_end.py`; conformance discovers the rest. |

The camera is the shipped example of a 2D block, so the raw-block section of
the measurement README is rewritten around frames instead of being cut with
the tensormeter.

### 5.9 mcp/, ctl/, troubleshoot/

Ship as-is. Edits: `translate.INSTRUCTIONS`, `SERVER_NAME`, the three
`i2as://` URIs, `status_reader.CODE_HELP` (QUENCH row → CRITICAL_FLAG),
`troubleshoot/cli.py` fallback config and two help strings,
`ctl/discovery.py` package list without operations, `engine.py`'s
pymeasure special case.

## 6. Generalisations: the design changes inside the cut

These are the places where a cryostat concept sat in the framework. Each one
moves the judgement to the file the user writes.

### 6.1 Action class is declared on the control

Today `gateway/action_classes.py` holds a table classifying every
`@control` of every VI (`magnet_z.set_field` → run_control, `stop` →
recovery…). The file itself says the classification "is a physics judgement
about a particular instrument rack". That is exactly the user's judgement, so
it belongs in the VI:

```python
@control(scope="measurement", params={...}, action_class="run_control")
def set_field(self, field_T: float) -> None: ...

@control(scope="operation", action_class="recovery")
def stop(self) -> None: ...
```

Default `run_control` (the most restrictive class an agent can ever hold).
`decorators.py` validates the value at import against the four names; the
gateway reads it from `StationInfo.ControlInfo` (which gains one field). A
conformance test asserts every shipped control declares it explicitly, so the
example teaches the habit. The roles × classes matrix in `roles.py` is
unchanged.

### 6.2 Critical fault is a declared flag, not a magnet word

`RunFaultCode.QUENCH` and `vi_state["magnet_status"] == "QUENCH"` become
`RunFaultCode.CRITICAL_FLAG`, produced when any active safety flag has
severity `critical`. The producer side already exists
(`safety_flags = {"quench": "critical"}` on `MagnetBase`); only the consumer
in `operational_status.py`, the emergency transition in the orchestrator,
`status_reader.CODE_HELP`, and the GUI banner text change. The sim magnet
still trips EMERGENCY on a quench; a user's laser interlock or pressure
switch does the same by declaring one flag.

### 6.3 Procedures find instruments by role, not by name

`field_sweep.py` hardcodes `magnet_z` and `temperature_vti` (a known defect
recorded in `procedures/README.md`). Add to Station
`vi_names_by_base(MagnetBase)` / `vi_names_by_base(TemperatureControllerBase)`,
already mirrored by the existing `measurement_vi_names()`. Each axis
procedure gains a `ParamSpec` with `choices` filled from that discovery at
construction (`field_vi`, `temperature_vi`), defaulting to the only candidate
when there is one. Time Series builds `_END_CHANNELS` from the same
discovery. The GUI form and the agent tool schema pick the choices up
automatically because both render `ParamSpec`.

### 6.4 Ramp phases without motion are a VI declaration

`stall_detection._NO_MOTION_PHASES` lists persistent-mode sub-phases of one
magnet. `RampableVI` gets `no_motion_phases: ClassVar[frozenset[str]] = frozenset()`;
the shipped magnet declares nothing (the persistent variant is gone).

### 6.5 Trend checks are declared in the config

Keep the `TrendCheck` mechanism and runner. Replace the two concrete checks
by one generic `channel_within_band(key, low, high, window_s)` whose
instances come from the config `trends:` block; the example config declares
one on the sample temperature.

### 6.6 Config parsing is one file

Twelve `read_*_config()` functions and their default tables leave
`station.py` for `core/config.py`. The YAML schema does not change; the
station class drops to ~2 400 lines and the config surface a user reads is
one module.

### 6.7 Per-user paths are one module

`paths.py`, `eln/settings.py`, `troubleshoot/cli.py` and `mcp/client.py` each
re-implement the AppData/XDG resolution (flagged in
`config-directory-migration.md`). Consolidate into `paths.py` before the
rename, so the eight literal sites become two.

### 6.8 Image blocks: a frame is a 2D block, not a row of channels

Today a measurement VI may declare `measurement_raw_blocks = {name: [channel
labels]}` and the run file stores `(N, [n_loop1, n_loop2,] rows, cols)` with
`channel_names` on the dataset. A camera frame has the same shape but no
channel per column. Add one declaration form beside it:

```python
measurement_image_blocks: ClassVar[dict[str, ImageBlock]] = {
    "frame": ImageBlock(height_px=256, width_px=256, unit="counts", description="…"),
}
```

`DataManager` writes it through the same dataset path with
`attrs["block_kind"] = "image"` and `unit`, no `channel_names`;
`data_reader` gains `ROLE_IMAGE` beside `ROLE_RAW_BLOCK` and a
`read_image(name, index)` helper; `RunSource` reports it as a column with
role `image`. Nothing else changes: the live plot ignores image columns, the
generic sweep recipe plots the scalar columns, the image-stack recipe reads
frames. Conformance requires each declared block's `height_px`/`width_px`
to match what `take_reading()` actually returns from the sim.

## 7. Phases

Each phase ends with `make check` green in both thread modes and one commit
on `feature/i2as-framework`. Phases 1, 2 and 4 are independent of each other
and can run in parallel worktrees; 3 depends on 1 and 2 (it uses
`vi_names_by_base()` and the `StageBase`); 5 depends on 1–4; 6–8 are
sequential.

| # | Phase | Contents | Exit |
|---|---|---|---|
| 0 | Plan | This document; `docs/plans/README.md` row. | Approved by the user. |
| 1 | Cut the cryostat verticals | Operations (core, orchestrator, commands, tools, GUI panel, procedures, tests). Servicing/cryogenics (`servicing_log` split, recorder, GUI page, `main.py` block, config blocks, three conformance sections). Switch matrix and `scanner_enabled` across eight files. Level meter, rotator, lock-in, tensormeter, persistent magnet, VTI VI, dc-mode/delta VIs, their drivers, three configs, 28 test files. `rtm2`/`pymeasure` out of `pyproject.toml`. | Suite green with the reduced sim config; contracts pass with dead module lines removed (never a weakened rule). |
| 2 | Generalise | §6.1–6.8 (6.8 adds the image-block declaration, writer and reader with tests, before any camera exists). New conformance tests: explicit `action_class` on every shipped control; procedure `description` non-empty; `no_motion_phases` is a frozenset; image blocks match the sim's frame shape. | Field Sweep runs end to end with `field_vi`/`temperature_vi` chosen by discovery; `test_scenario_emergency` passes on `CRITICAL_FLAG`. |
| 3 | Imaging example | §5.8: sim camera, sim stage, `StageBase`, `XYStageVI`, `CameraMeasurementVI`, `FieldImaging`, `field_image_stack`, `configs/sim_imaging/`, their tests and folder READMEs; the measurement README's raw-block section rewritten around frames. | `FieldImaging` runs on `sim_imaging` through the GUI and through the gateway as an agent; the recipe produces the montage and the loop; conformance discovers 2 configs, 6 VIs, 4 procedures, 3 recipes. |
| 4 | Trim the GUI | §5.7. Re-home `LogPanel` and "Instrument Info…". Screenshot verification per `gui-edit`. | `test_gui.py` minus the listed cases green in both modes; screenshots of both windows inspected. |
| 5 | Tests and fixtures | `tests/scenarios.py` and `tests/instrument_modes.py` on the minimal station; swap `helium_fill` out of `test_estimates`, `test_l3_orchestrator`, `test_run_queue`; retarget `test_connection_lifecycle` to the kept sims; `test_conformance.py` derives every module prefix from `PACKAGE = i2as.__name__`; `CONTROL_LIMIT_EXEMPTIONS` and `_SIM_MEASUREMENT_DRIVER_CLASSES` shrink. | Conformance discovers exactly the shipped 8 drivers, 6 VIs, 4 procedures, 2 configs, 3 recipes. |
| 6 | Docs and skills | New `README.md` (§3 table, layer story, both examples, install, run a sim, "add a VI / procedure / recipe / config", agent API table from `gateway/README.md`, roles matrix). `GLOSSARY.md` minus ~17 cryogen/magnet/switch terms, plus the imaging terms. Folder READMEs to the surviving files. `CLAUDE.md` for I2AS. `docs/plans/` (this document included), `docs/Issues/` removed. Skills: keep `write-measurement-vi`, `gui-edit`; genericise `setup-commission`, `setup-supervisor`, `measure-session`, `troubleshoot-runtime`; drop `diagnose-connections`. Add `setup.md` template. | Folder-README conformance green; no `docs/plans` citation in code. |
| 7 | Rename | `cryosoft` → `i2as` everywhere (~4 100 occurrences) with deliberate handling of the ~60 runtime literals in §8; console scripts `i2as`, `i2as-ctl`, `i2as-doctor`, `i2as-mcp`; CI workflows; `.mcp.json`. | `pip install -e .[dev]` from a clean venv, `make check` green, `python -m i2as.main` opens on the sim, `i2as-mcp` serves the tool list, `i2as-ctl status` answers. |
| 8 | Cut the repo | Fresh clone of the branch, `make check`, tag `v0.1.0`, push `feature/i2as-framework:main` to the new `i2as` remote. History stays (the CryoSoft lineage is the provenance the paper cites). | The new repo passes CI from its own default branch. |

Rename is second to last on purpose: every phase 1–6 diff remains readable against
CryoSoft history and can be cherry-picked back into `develop` if a fix
belongs to both.

## 8. Rename: the literals that are not just text

Six classes of runtime-load-bearing strings; each gets one deliberate edit
and one test.

| Class | Sites | New value |
|---|---|---|
| Per-user state paths (AppData / XDG) | `core/paths.py:51,53,68`, `eln/settings.py:118-119`, `troubleshoot/cli.py:84`, `mcp/client.py:105-106` | `I2AS` / `i2as`; after §6.7 only `paths.py` holds them. Migration note in `docs/user-docs/`. |
| QSettings organisation/application, `setApplicationName` | `gui/app_settings.py:16-17`, `troubleshoot/cli.py:54-55`, `main.py:289` | `"I2AS"` |
| Logger names and the VI-logger prefix the GUI filters on | `logging_config.py`, `tiered_trend_logger.py`, `orchestrator.py:676,3755,3758`, `virtual_instruments/base.py` (`cryosoft.vi.`), `gui/log_panel.py:46`, `mcp/__main__.py:44` | `i2as.*`; `LOGGER_ROOT = "i2as"` constant in `logging_config.py` that `base.py` and `log_panel.py` import instead of repeating the prefix. |
| Wire and schema identifiers | `mcp/translate.py:98,123-125`, `mcp/sdk.py:173`, `capability_manifest.py:72`, `gateway/local_server.py:159`, `instrument_host.py:457`, `eln/settings.py:418` (default tag), `eln/templates.py:422` (`source`) | `i2as`, `i2as://status|station|manifest`, `i2as.capability_manifest/1`, `i2as-gateway-<pid>`, `i2as-instrument` |
| Dynamic imports and subprocess argv | `session/analysis_runner.py:300`, `analysis/discovery.py:65,482`, `gui/procedure_discovery.py:55`, `ctl/discovery.py:33`, every `class:` path in `devices.yaml`, 35 literals in `test_conformance.py` | Derived from `__name__` where a module is at hand; `i2as.…` in YAML. |
| Environment variables (13) | `CRYOSOFT_LOG_DIR`, `_INSTRUMENT_THREAD`, `_MEASUREMENT_ROOT`, `_ELAB_APIKEY`, `_ELN_SETTINGS`, `_GATEWAY_DESCRIPTOR`, `_MCP_*` (5), `_TEST_SETTLE_TIMEOUT_S`, `_ASSISTANT_APIKEY` (leaves with the assistant) | `I2AS_*` |

`pyproject.toml`: `name`, `packages`, `root_package`, and every module path in
the 23 import-linter contracts. The contract rules themselves do not change;
lines naming deleted modules are removed.

## 9. Tests

101 files today. After the cut: 28 dropped with their subjects (drivers 7,
VIs 6, servicing and operations 6, setup-specific procedures and scenarios 5,
GUI servicing/operations/config-editor/assistant 4), 2 support fixtures
rewritten (`scenarios.py`, `instrument_modes.py`), the rest kept with the
edits in phase 5. New tests: the four conformance rules in phase 2, a
`CRITICAL_FLAG` emergency scenario, role discovery in `test_l4_procedure.py`,
image-block write/read in `test_l5_data_manager.py`/`test_l5_data_reader.py`,
the imaging example's five files (§5.8), and one end-to-end test per example
that runs the sim procedure through the gateway as an agent, analyses it and
parks the notebook entry (the transport half exists in
`test_analysis_end_to_end.py`; it gains the agent leg and an imaging twin).

`test_conformance.py` stays the crown jewel: auto-discovery means a user's
new VI or recipe is checked the moment the file exists. It loses the
cryogenics, operations and excitation-ceiling-per-shipped-setup sections and
gains the three rules above.

## 10. Docs that ship

- `README.md`: rewritten around §3 and the layer story; both examples; the
  agent tool table and roles matrix from `gateway/README.md`; install; run a
  sim; the four "add a …" walkthroughs; how to connect a real rack (config +
  sim twin).
- `GLOSSARY.md`: ~190 of 208 terms survive, plus image block, stage, frame,
  reference frame.
- Folder READMEs: every functional folder, per the folder-README standard.
  Together with the docstrings they are the complete design record that
  ships; `docs/plans/` does not (§2).
- `docs/user-docs/`: keep, add the state-path migration note.
- `LICENSE`: MIT, unchanged.

## 11. Verification plan

After every phase: `ruff check .`, `lint-imports`, `pytest -m "not hardware"`
threaded and inline, from `.venv`. After phase 3: the imaging run's report
figures inspected. After phase 4: offscreen screenshots of the monitor window
and the procedure window on both configs per the `gui-edit` skill. After
phase 7: fresh-venv install and the four console scripts. After phase 8: CI
on the new repo.

## 12. Decisions taken in the second round (2026-09-05)

1. **Embedded assistant**: out of I2AS core; stays in CryoSoft.
2. **Procedures**: Field Sweep, Temperature Sweep, Time Series stay; Field
   Imaging is added.
3. **Temperature VI**: the Lakeshore variant, for its dynamic choices example.
4. **Design record**: not shipped. `docs/plans/` is removed in phase 6.
5. **Licence**: MIT unchanged; CryoSoft named only in the README's provenance
   line and the git history.
6. **Example naming**: `sim_cryostat`, `magnet_z`, `dc_measurement` keep their
   names; `temperature_vti` → `temperature`. The imaging setup is
   `sim_imaging` with `magnet_z`, `stage`, `camera`.
7. **Contracts**: lines naming deleted modules are removed from the
   import-linter contracts; no rule is weakened.
8. **Imaging kind**: widefield camera (not scanning probe).

## 13. Out of scope (later, separately)

- Moving the long standard docstrings into `docs/standards/` (would take core
  from ~13 000 to ~8 000 lines without a behaviour change).
- Deleting `inline` mode (already scheduled for one release after the
  instrument thread landed).
- A `cookiecutter`-style `i2as new-vi` / `i2as new-procedure` scaffold (the
  analysis recipe already has one; the others are natural follow-ups).
- Rewriting `sim_oxford_ips120` as a generic "sim rampable source" (the sim
  magnet's physics is the example, and it is tested).
- The per-procedure recipe-preference editor noted in the analysis stage's
  design.
- A live frame view in the procedure window (today the two live plots show
  scalar columns; the imaging example shows `roi_mean` live and the frames in
  the analysed entry). Natural follow-up once the image block exists.
- A scanning-probe imaging example (stage raster over a point detector
  through the reading loop); the widefield camera was chosen first.

## 14. Cutting the repository

The branch head is the repository: `git push git@github.com:<owner>/i2as.git
feature/i2as-framework:main`. No history rewrite. CryoSoft's `develop` and
`main` are untouched; fixes that belong to both are cherry-picked while the
rename commit is still the branch tip (everything before it applies to
CryoSoft as-is).
