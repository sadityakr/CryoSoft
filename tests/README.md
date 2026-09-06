# tests/

## Purpose

The full test suite for the framework. Three kinds of tests live here:

1. **Layer tests** (`test_l0_*` … `test_l5_*`, `test_gui.py`, and feature
   tests): behavior tests for each architecture layer, written against the
   simulated drivers so everything runs without hardware.
2. **Conformance tests** (`test_conformance.py`): auto-discovering interface
   checks. They iterate over the drivers, VI, procedures, configs and recipes
   packages at runtime, so any *new* module is tested automatically the
   moment it exists. This is the safety net that lets coding agents add
   drivers, procedures and recipes without silently breaking the system's
   contracts. Every package name and path the file scans is derived once
   from the package object (`PACKAGE`, `PACKAGE_DIR`, `CONFIGS_DIR`), never
   spelled out, so a package rename is a no-op for it. Two of them check the
   tree itself rather than a package:
   `test_no_plan_document_citation_under_cryosoft` (the code-reference
   standard — no `.py` file and no folder `README.md` under the package cites
   a document in `docs/plans/`; a vendor manual's section number is not a
   citation and never trips it, and the allowlist is empty by construction,
   held there by `test_plan_citation_allowlist_is_empty`) and
   `test_no_blocking_sleep_in_gui_sources` (nothing under `gui/` blocks the
   one shared thread with `time.sleep`).
3. **End-to-end tests on the shipped examples**: the two example setups —
   `sim_cryostat` (the transport example: a magnet, a temperature controller
   and a DC source/meter pair, run by Field Sweep) and `sim_imaging` (the
   imaging example: a magnet, a two-axis stage and a camera, run by Field
   Imaging) — are each driven as a whole. `test_agent_end_to_end.py` runs
   both through the agent gateway; `test_analysis_end_to_end.py` runs both
   through the analysis stage to the sim notebook; `test_scenario_*.py` run
   the transport example through every client surface and hazard.

Layer *import* boundaries are not tested here — they are enforced by
import-linter (`make contracts`, config in `pyproject.toml`).

## Architecture layer

Cross-cutting: tests exist for every layer, L0 through the GUI, plus the
session layer's agent gateway, the analysis stage and the client tools.

## Entry (what comes in)

`pytest` (or `make test`) discovers everything in this folder per
`[tool.pytest.ini_options]` in `pyproject.toml`. Tests import the package
directly and use the simulated drivers plus the two shipped example configs,
`sim_cryostat` and `sim_imaging`; no hardware, network, or display is needed
(GUI tests run offscreen in CI via `QT_QPA_PLATFORM=offscreen`; the analysis
worker is a real subprocess that reads a run file and reaches no instrument).

## Exit (what goes out)

Pass/fail results. Some tests write temporary HDF5 files via pytest's
`tmp_path` fixture; nothing is written into the repository.

## Interface contract

Tests requiring physical instruments must be marked `@pytest.mark.hardware`;
`make test` and CI exclude them. Everything else must pass on a bare machine.
Shared fixtures belong in `conftest.py`.

`instrument_modes.py` (not itself a test file) is what lets one suite run in
both instrument modes. **A plain `pytest` run is threaded**, like the
application: the Station and the Orchestrator live on the **Instrument
thread**, and `CRYOSOFT_INSTRUMENT_THREAD=0` is what takes a run back to the
temporary **Inline mode**. `tests/test_gui.py`'s fixtures build through an
`InstrumentHost` in whichever mode is selected and hand the windows the
`OrchestratorProxy` the application hands them, so the same assertions are
checked both ways (`make test-instrument-inline`, which CI runs after
`make test`). Write a new GUI test against the threaded default — a test that
asserts on the mirror immediately after a click passes inline and fails
threaded, which is the bug, not the suite. It carries
the **tick helper** family a test needs once it is behind the client boundary:
`on_engine()` runs a call where the engine lives and waits for it,
`set_on_engine()` forces one engine attribute, `tick_engine()` replaces a bare
`orchestrator._tick()`, `ticks_paused()` holds the tick timer while a test
forces a state the next tick would undo, and `settled()` waits out the round
trip a GUI action makes — all no-ops or direct calls inline, so a test reads
the same either way.

**Every wait in those helpers is bounded.** `drain()` runs the client's event
loop until the queue is dry, until `max_events` (20 000) deliveries have
landed, or until `timeout_s` is up — and the last of those raises
`EngineNotSettled` (a `TimeoutError`) naming how many events it drained, the
engine's state and its tick interval. Without the bound, an engine that keeps
its client fed — a fast tick with a visible `MonitorWindow` attached — left
`drain()` spinning at 100 % CPU until the run was killed, with nothing in the
report to say why. The default bound is 10 s and comes from
`CRYOSOFT_TEST_SETTLE_TIMEOUT_S`, so a slow machine can widen every helper at
once (`CRYOSOFT_TEST_SETTLE_TIMEOUT_S=30 pytest tests/test_gui.py`); a single
call that legitimately needs longer passes its own `timeout_s`
(`settled(orchestrator, timeout_s=30)`, `on_engine(orchestrator, call,
timeout_s=30)`, likewise `tick_engine()`, `set_on_engine()` and
`ticks_paused()`). The deadline is read between passes of the event loop, so
a drain can overrun it by one pass: the bound guarantees that a wait ends and
says why, not when. `test_instrument_modes.py` is the helper family's own
test file.

`scenarios.py` (not itself a test file) names the sim-driver state-injection
recipes every hazard/fault test needs — a declared safety hold, quench, a
disconnected instrument, a measurement instrument erroring instead of
returning data, a procedure running — as composable functions instead of each
test hand-rolling driver flags plus a `qtbot.waitUntil`. Each has a plain
`apply_*` form (sets the driver attribute, no wait — importable from outside
pytest, e.g. `scripts/run_scenario.py`) and, where convergence matters, a
wrapped form that also waits. They take the station the caller built from a
shipped config and name only the VIs the `sim_cryostat` example registers
(`magnet_z`, `temperature`, `dc_measurement`). See `test_scenarios.py` for
usage.

**Speeding up a sim run.** The sim magnet ramps at the realistic rate its
config declares, and a procedure's settle waits are real wall-clock seconds.
A test that drives a run to completion sets
`station.magnet_z._default_ramp_rate = 6000.0` and
`station.magnet_z._ramp_segments = []`, passes zero waits, and builds the
Orchestrator with `tick_interval_ms=10`; a run is then a few dozen ticks. A
probe run (`probe_spec`) caps every seconds-valued parameter, the camera's
exposure included, so the imaging example's probe passes a `max_wait_s` no
smaller than its exposure.

## How to add a new module

- **Testing a new driver / VI / procedure / recipe:** you get conformance
  coverage for free, but still add a behavior test file (`test_<feature>.py`)
  exercising what the module actually does, using its sim driver.
- **Adding a shipped example** (a config with its procedure and recipe): it
  is discovered by conformance the moment the directory exists. Add it to
  `EXAMPLES` in `test_agent_end_to_end.py` and a `wired_*` fixture to
  `test_analysis_end_to_end.py`, so the run → analyse → park → approve →
  publish story is proven for it too.
- **A conformance test fails on your new module:** fix the module to match the
  contract (the assertion message says what is expected). Do not weaken or
  special-case the conformance tests.
- **Your new VI has an unbounded numeric `@control` parameter:**
  `test_every_numeric_control_param_is_bounded_or_exempt` fails until that
  parameter is either declared in `control_limits` (its value coming from the
  config's `init_params`, never from code) or written into
  `CONTROL_LIMIT_EXEMPTIONS` in `test_conformance.py` with a one-line physical
  reason a range cannot bound it — an enumerated mode, a dimensionless count, a
  timing parameter, a tuning constant. The table is checked in both directions:
  `test_no_stale_control_limit_exemptions` fails on a row whose parameter has
  since gained a limit, so the exemptions stay as short as the code allows.
- **Adding a new contract:** extend `test_conformance.py` with a discovery
  helper + parametrized test, and document the contract in GLOSSARY.md.

## How agents run the suite

The suite is large; run the narrow slice while iterating and the full gate only
before handing work back. Paths use the project `.venv`.

1. **While iterating, run only the owning test file:**
   `./.venv/Scripts/python.exe -m pytest tests/test_<area>.py -q`.
2. **Full check before handing back:**
   `./.venv/Scripts/python.exe -m pytest -m "not hardware" -q --tb=no`, and read
   only the tail: the FAILED list and the summary line.
3. **On failure:** `./.venv/Scripts/python.exe -m pytest --lf --tb=short -q`
   reruns only the failed tests with short tracebacks.
4. **Report the summary line plus failed test IDs upward**; never paste full
   passing output.

### Which test file owns which source folder (routing table)

Editing a source file? Run its owner first. Every driver / VI / procedure /
config / recipe also has automatic `test_conformance.py` coverage on top of
these.

| Editing... | Owning test file(s) |
|------------|---------------------|
| `cryosoft/drivers/*` | `tests/test_l0_simulated.py`, `tests/test_l0_lakeshore_335.py`, `tests/test_l0_driver_errors.py`, `tests/test_l0_sim_camera_stage.py` |
| `cryosoft/virtual_instruments/*` | `tests/test_l1_virtual_instruments.py`, `tests/test_l1_new_vis.py`, `tests/test_measurement_dc_vi.py`, `tests/test_l1_camera_stage_vis.py`, `tests/test_connection_lifecycle.py` |
| `cryosoft/core/station.py`, `config.py`, `config_catalog.py`, `capability_manifest.py` | `tests/test_l2_station.py`, `tests/test_config_validation.py`, `tests/test_config_catalog.py`, `tests/test_capability_manifest.py`, `tests/test_direct_action_path.py` (`execute_vi_action()`'s refusals) |
| `cryosoft/core/orchestrator.py` | `tests/test_l3_orchestrator.py`, `tests/test_direct_action_path.py`, `tests/test_scenario_pause.py`, `tests/test_scenario_emergency.py` |
| `cryosoft/core/instrument_host.py`, `orchestrator_proxy.py`, `status_mirror.py` | `tests/test_orchestrator_proxy.py` (the temporary inline mode), `tests/test_instrument_thread.py` (the threaded default), `tests/test_status_mirror.py` |
| `cryosoft/core/events.py`, `conditions.py`, `availability.py`, `gates.py`, `ramps.py` | `tests/test_conformance.py` (the contract-type specimens), `tests/test_conditions.py`, `tests/test_availability.py`, `tests/test_core_gates.py`, `tests/test_ramps.py` |
| `cryosoft/core/procedure.py`, `cryosoft/procedures/*` | `tests/test_l4_procedure.py`, `tests/test_new_procedures.py`, `tests/test_time_series_procedure.py`, `tests/test_field_imaging_procedure.py`, `tests/test_probe_runs.py` |
| `cryosoft/core/plan.py`, `sweep_builder.py`, `run_builder.py`, `estimates.py` | `tests/test_plan.py`, `tests/test_sweep_builder.py`, `tests/test_run_builder.py`, `tests/test_estimates.py` |
| `cryosoft/core/request_spool.py` | `tests/test_request_spool.py` |
| `cryosoft/core/data_manager.py`, `data_reader.py`, `run_buffer.py` (L5) | `tests/test_l5_data_manager.py`, `tests/test_l5_data_reader.py`, `tests/test_run_buffer.py` |
| `cryosoft/core/trend_*.py`, `tiered_trend_logger.py`, `operational_status.py`, `stall_detection.py` | `tests/test_trend_checks.py`, `tests/test_trend_check_runner.py`, `tests/test_trend_history.py`, `tests/test_tiered_trend_logger.py`, `tests/test_operational_status.py`, `tests/test_stall_detection.py` |
| `cryosoft/core/paths.py`, `logging_config.py`, `cryosoft/main.py` | `tests/test_paths.py`, `tests/test_logging_config.py`, `tests/test_main.py` |
| `cryosoft/session/manager.py`, `store.py`, `models.py`, `run_queue.py` (L6) | `tests/test_session_layer.py`, `tests/test_run_queue.py` + session-model conformance |
| `cryosoft/session/maintenance_log.py` (L6) | `tests/test_maintenance_log.py` + log-kind conformance |
| `cryosoft/session/eln/` (L6) | `tests/test_eln.py` + ELN-adapter conformance; `tests/test_analysis_end_to_end.py` |
| `cryosoft/session/gateway/`, `agent_feed.py` | `tests/test_gateway.py`, `tests/test_gateway_tools.py`, `tests/test_gateway_server.py`, `tests/test_agent_feed.py`, `tests/test_scenario_agent.py`, `tests/test_agent_end_to_end.py` |
| `cryosoft/session/analysis_runner.py`, `cryosoft/analysis/` | `tests/test_analysis.py`, `tests/test_analysis_runner.py`, `tests/test_analysis_end_to_end.py`, `tests/test_agent_end_to_end.py` + recipe conformance |
| `cryosoft/ctl/`, `cryosoft/mcp/` | `tests/test_ctl.py`, `tests/test_phase_e_scenario.py`, `tests/test_mcp_adapter.py`, `tests/test_scenario_agent.py` |
| `cryosoft/gui/param_form.py`, `monitor_window.py`, `procedure_window.py`, `instrument_panel.py`, `notification_banner.py`, `theme.py`, `live_plot_panel.py`, `app_settings.py`, `log_panel.py` | `tests/test_gui.py`, `tests/test_scenario_gui.py` |
| `cryosoft/gui/sweep_axis_widget.py` | `tests/test_sweep_axis_widget.py` |
| `cryosoft/gui/lifecycle_toggle.py` | `tests/test_lifecycle_toggle.py` |
| `cryosoft/gui/form_autosave.py` | `tests/test_form_autosave.py` |
| `cryosoft/gui/monitor_history.py` | `tests/test_monitor_history.py` |
| `cryosoft/gui/trend_plot_panel.py`, `trends_quadrant.py`, `ramp_tracker_panel.py` | `tests/test_trend_plot_panel.py`, `tests/test_trends_quadrant.py`, `tests/test_ramp_tracker_panel.py` |
| `cryosoft/gui/agent_panel.py`, `takeover_strip.py`, the experiment header's envelope editor | `tests/test_agent_panel.py` |
| `cryosoft/gui/analysis_panel.py`, `eln_settings_dialog.py` | `tests/test_analysis_panel.py`, `tests/test_eln_settings_dialog.py` |
| `cryosoft/troubleshoot/*`, operational status / stall detection | `tests/test_troubleshoot_cli.py`, `tests/test_troubleshoot_engine.py`, `tests/test_troubleshoot_session_report.py`, `tests/test_operational_status.py`, `tests/test_status_reader.py`, `tests/test_stall_detection.py` |

## Files

- `conftest.py` — shared fixtures (logging setup, an isolated measurement root).
- `instrument_modes.py` — building a host in the session's instrument mode, and the tick helpers a test needs to reach the engine across the boundary (see above).
- `test_instrument_modes.py` — the harness testing itself: the settle bound on `drain()`, `on_engine()` and `settled()`, its `CRYOSOFT_TEST_SETTLE_TIMEOUT_S` default and per-call override, and the two ways a drain ends without hanging (`max_events` reached, or `EngineNotSettled` with the engine named). Runs in both instrument modes.
- `scenarios.py` — the composable sim-driver scenarios (see above); `test_scenarios.py` is its own test file.
- `mocks/` — shared mock objects, including `bus_spy.py`: recording shims over a
  live driver's public methods, for proving a path issues no instrument traffic
  (an empty call log, rather than trust).
- `test_foundation.py` — core exceptions, decorators, logging config.
- `test_conformance.py` — auto-discovering interface conformance (see above).
- **L0 drivers:** `test_l0_simulated.py`, `test_l0_lakeshore_335.py`, `test_l0_driver_errors.py` (the driver error-reporting standard, one pair per shipped instrument), `test_l0_sim_camera_stage.py` (the imaging example's sims: the shared sim environment, the camera and the XY stage).
- **L1 virtual instruments:** `test_l1_virtual_instruments.py`, `test_l1_new_vis.py`, `test_measurement_dc_vi.py`, `test_l1_camera_stage_vis.py` (the stage axis and the camera measurement VI over their sims), `test_connection_lifecycle.py` (the connection-lifecycle standard, L0–L3: `ping()`, `disconnect()`, the detach-when-idle declaration, a station reconnecting one VI).
- **L2 station + config:** `test_l2_station.py`, `test_config_validation.py`, `test_config_catalog.py`, `test_capability_manifest.py` (the **Station info** declaration snapshot and its **Capability manifest** rendering: declared order, group resolution, the offline branch, the JSON Schema and its validator, and the `python -m cryosoft.core.capability_manifest` entry point).
- **The client boundary:** `test_orchestrator_proxy.py` (the `OrchestratorProxy` and the `InstrumentHost` in the temporary `inline` mode, whose tests go with it); `test_instrument_thread.py` (the same seam across a real `QThread`, which is the default the application ships in — GLOSSARY.md's **Instrument thread**: thread affinity, one verdict per command posted across the boundary, the frozen-GUI detector, payload ownership, the run queue's two crossings, the pause boundary and a quench end to end, and a bounded shutdown over a read that never returns). A test that used to call `orchestrator._tick()` directly must not do so across the boundary: `instrument_modes.py`'s `tick_engine()` is the **tick helper** — it runs the tick where the engine lives and waits for it, so the caller still gets the synchronous "that tick has happened" it relied on. `test_status_mirror.py` is the mirror's own file.
- **L3 orchestrator:** `test_l3_orchestrator.py`; `test_direct_action_path.py` (the direct action path: the five refusals a manual action can meet — private name, non-capability, out-of-scope capability, out-of-limit value, out-of-envelope setpoint — plus `emergency_standby()` from every state); `test_conditions.py` and `test_availability.py` (the System-Condition and Availability standards' policy cores); `test_core_gates.py`, `test_ramps.py`; `test_request_spool.py` (the file-based request spool and the tick drain that reads it).
- **L4 procedures + planning:** `test_l4_procedure.py`, `test_new_procedures.py`, `test_time_series_procedure.py`, `test_field_imaging_procedure.py` (the imaging example's procedure, headless and through a real Orchestrator to a run file of frames), `test_plan.py`, `test_sweep_builder.py`, `test_run_builder.py` (the one headless construction path), `test_probe_runs.py` (the probe standard), `test_estimates.py` (the duration-estimate standard).
- **L5 data manager and reader:** `test_l5_data_manager.py`, `test_l5_data_reader.py` (reading back what the writer wrote, image blocks included), `test_run_buffer.py` (the run in flight, equivalent to the file on disk).
- **L6 session management:** `test_session_layer.py`, `test_run_queue.py` (the run queue as data, `validate_run`'s three checks), `test_maintenance_log.py`, `test_eln.py` (ELN publishing — sim adapter and a fake HTTP transport, never a live notebook), `test_analysis.py` (the recipe contract, discovery, the worker CLI), `test_analysis_runner.py` (the runner against a stand-in worker), `test_agent_feed.py` (the agent feed: one experiment's non-operator action trail).
- **The agent gateway and its clients:** `test_gateway.py` (the permission model: roles, action classes, the kill switch, attendance, the envelope), `test_gateway_tools.py` (the tool surface: command, capability, run, analysis and ELN tools, all answered and nothing raised), `test_gateway_server.py` (the JSON-RPC transport over a local socket), `test_ctl.py` and `test_phase_e_scenario.py` (the reference client, offline and live), `test_mcp_adapter.py` (the MCP adapter and its import allowlist).
- **End to end on the shipped examples:** `test_agent_end_to_end.py` (one test per example — an agent reads the manifest, validates, probes, runs, waits for `RunFinished`, analyses and reads the report through the gateway; a human approves; the sim notebook gets one entry with the figures; the feed carries the trail), `test_analysis_end_to_end.py` (the transport half of the same story: a finished run's manifest through the real worker to the parked entry, for both examples, plus a failing recipe falling back to facts), `test_scenario_agent.py` (the agent family through every client surface: in-process threaded gateway, refusals mid-run, `cryosoft.ctl`, the local socket, the MCP shim, two agents at once), `test_scenario_lifecycle.py` (a run through the client boundary, checked at every layer), `test_scenario_pause.py` (the pause boundary), `test_scenario_emergency.py` (emergency standby, safety trips, instrument faults during a run), `test_scenario_gui.py` (GUI scenarios for a running measurement, both instrument modes), `test_scenarios.py`.
- **GUI (pytest-qt, offscreen):** `test_gui.py`, `test_sweep_axis_widget.py`, `test_lifecycle_toggle.py`, `test_form_autosave.py`, `test_monitor_history.py`, `test_trend_plot_panel.py`, `test_trends_quadrant.py`, `test_ramp_tracker_panel.py`, `test_agent_panel.py` (the **Agent panel**, the **Takeover strip** and the experiment header's envelope editor — built over an `InstrumentHost` like `test_gui.py`, so it runs in both instrument modes), `test_analysis_panel.py` (the eLab tab over stub collaborators), `test_eln_settings_dialog.py` (the eLab setup dialog over the sim ELN adapter).
- **Troubleshooting / operational status:** `test_troubleshoot_cli.py`, `test_troubleshoot_engine.py`, `test_troubleshoot_session_report.py`, `test_operational_status.py`, `test_status_reader.py`, `test_stall_detection.py`.
- **Trend history:** `test_trend_checks.py`, `test_trend_check_runner.py`, `test_trend_history.py`, `test_tiered_trend_logger.py`.
- **Installation and startup:** `test_paths.py`, `test_logging_config.py`, `test_main.py`.
