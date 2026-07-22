# Complete Virtual Instruments: UI groups, @query verb, mode exclusivity + Keithley 6221 bench pilot

## Context

CryoSoft's monitor/front-panel GUI renders every VI from decorator metadata, but VIs expose only the narrow slice a procedure needs. The user wants VIs to be "complete": one rich VI per physical instrument whose front panel exposes most of what the instrument can do (arm delta mode, run the waveform generator, source DC, read back structured results), so operating and bench-testing an always-on cryostat no longer requires a terminal or vendor software. Three framework primitives are missing, each to be delivered as a written, machine-checked standard (per the standards-over-one-off-code principle), then proven on a Keithley 6221 (+2182A) pilot:

1. **UI groups**: declarative grouping of @monitored/@control/@query methods into titled boxes ("Delta mode", "Waveform", "DC source").
2. **@query verb**: an on-demand parameterized read dispatched through the Orchestrator, whose structured result (scalars with units + small arrays) travels back and renders generically in the panel. Availability gate identical to @control (IDLE / manual-ramping / emergency-override); user confirmed queries are NOT allowed during procedures.
3. **Mode exclusivity**: groups bound to instrument modes; controls of an inactive mode are refused in software (CryoSoftSafetyError) and greyed in the GUI. Safety-relevant: unattended persistent cryostat.

User decisions already made: one rich VI per physical box (method VIs stay for procedures); framework + 6221 pilot as first delivery; new registry role `vi_type: "bench"`; auto-standby-on-procedure-start hook DEFERRED (rely on shared-instrument discipline); the capability-based generic-measurement-method refactor (removing the method x instrument VI multiplication) is a FUTURE step recorded as a design directive, not in scope.

## Established facts (from exploration; all verified in current working tree)

- `core/decorators.py` stores @control metadata opaquely (contract C1: may not import ParamSpec or any spec type). `virtual_instruments/base.py.__init_subclass__` wraps @control with limit + logging wrappers and type-checks specs at class creation (base.py:140-234, 176-186).
- Orchestrator `submit_vi_action` (orchestrator.py:750) gates, queues to `_gui_action_queue`, drains in `_tick_body` step 3 (1148-1181); `Station.execute_vi_action` RETURNS the method result (station.py:631-647) but the Orchestrator discards it. Signals at orchestrator.py:159-172; `measurement_ready = pyqtSignal(dict)` is the dict-payload precedent.
- `Station.build_station` shares one driver instance across VIs by name (station.py:744-766). Mode discipline = idempotent self-recovering driver methods (GLOSSARY "Shared-instrument mode discipline"); sim `_mode` hook + handoff tests at test_l1_virtual_instruments.py:409-473.
- `core/plan.py` holds ParamSpec (294), ParamGroup (466), DataSchema.validate (515/671); importable by GUI (C8), Orchestrator (C5), Station (C4). New spec types go here.
- `gui/instrument_panel.py` renders flat rows (monitored 177-189, controls 192-195); `_build_control_row` grid stacking (280-347); `_submit_control` -> `submit_vi_action` (465); status restyle via dynamic property + unpolish/polish. `param_form.build_param_widget/build_param_tooltip/collect_value` are the reusable widget helpers.
- `monitor_window.py:452-460` filters panels by explicit vi_type sets (`{"system","level"}`, `"measurement"`, `"switch"`); a new role is invisible until added there. Conformance `CONFIG_VI_TYPES` (test_conformance.py:79) validates registry roles.
- 6221 driver has ONLY DC + delta today; NO waveform capability at any layer. Sim parity enforced by `test_sim_real_driver_api_parity` (test_conformance.py:241, identical names AND signatures). Sim hooks: `_mode`, `_meter_present`, `_delta_return_count`, `_simulate_error`, `_paired_meter`.
- gui-edit skill is mandatory for Wave 3: theme tokens only (never setStyleSheet), repolish children, objectNames are API, offscreen smoke script `.claude/skills/gui-edit/scripts/gui_smoke.py`.

## Design decisions

- **A. VI shape**: keep `DeltaModeMeasurementVI` / `DCSeparateMeasurementVI` untouched; add `Keithley6221BenchVI(BaseVirtualInstrument)` sharing the same driver instances, registry `vi_type: "bench"` (passive: polled + GUI actions only; never a ramp target; never in the procedure dropdown). Purely additive; shared-driver handoff is exactly what the mode discipline covers and tests.
- **B. Grouping**: `group="key"` kwarg on @monitored/@control/@query storing plain-str `_ui_group` (C1-safe). New frozen dataclass `UIGroup(key, title, mode="", entry=())` in `core/plan.py` (NOT ParamGroup, which is a procedure-form concept). VIs declare `ui_groups: ClassVar[tuple[UIGroup, ...]]`; `__init_subclass__` validates (keys unique, every tag matches a key, entry names are real @control members of that group). Empty `ui_groups` renders byte-identically to today: all existing VIs unchanged.
- **C. @query**: `query(func=None, *, params=None, schema=None, group="", panel=False)` in decorators.py with markers `_is_query`, `_query_specs`, `_query_schema` (opaque), plus helpers `get_query_methods` / `get_query_schema` / `get_ui_group`. New `QuerySchema(scalars: dict[str,str], arrays: dict[str,str])` (name -> SI unit) in plan.py with `validate(result)` reusing the DataSchema validation style + DataSchemaError. base.py wraps @query like @control (limit wrapper honors control_limits, logging, marker preservation) and type-checks schema/specs. Dispatch: `Orchestrator.submit_vi_query` (same gate; `action_blocked` on refusal) appends `{"kind": "query", ...}` to the EXISTING `_gui_action_queue` (missing kind defaults to "action"); drain calls new `Station.execute_vi_query` (verifies `_is_query` marker, returns result); Orchestrator validates against schema, emits new `query_ready = pyqtSignal(str, str, dict)`; failures -> `action_failed`. One-tick execution, documented soft budget "<= ~10 s; longer belongs in a procedure". Every @query param must have a default (round-trip testability).
- **D. Mode exclusivity**: `UIGroup.mode` binds the group; any VI with a moded group MUST expose `@monitored active_mode() -> str` (conformance-checked). New `_make_mode_wrapper` in base.py composed like `_make_limit_wrapper` (checked first): non-entry @control/@query of an inactive-mode group raises CryoSoftSafetyError before hardware. `entry` methods (mode-establishing, e.g. `arm_waveform`) are exempt so modes can be switched. GUI: `_on_states_updated` reads `active_mode`, sets dynamic property `modeActive="false"` on inactive group boxes (+ repolish, per gui-edit), `setEnabled(False)` on non-entry children; QSS dimming in theme.py with border reserved transparent (no layout jump). No cross-VI guard in v1 (user decision: defer; discipline + single-writer + procedure self-recovery suffice; document the residual "bench output stays live until next mode-establishing call").
- **E. Waveform driver scope** (minimal, no gold-plating): `configure_waveform(waveform="SIN", amplitude, frequency, offset)`, `arm_waveform()` (leads with `:SOUR:SWE:ABOR`, self-recovering), `start_waveform()`, `abort_waveform()` (restores DC baseline, output off), `get_waveform_armed()`, `get_mode() -> "DC"|"DELTA"|"WAVEFORM"` on BOTH real and sim 6221. All real SCPI docstrings + drivers/README.md carry "unverified on hardware" (model 705 precedent). No arbitrary-wave upload, no external trigger.
- **F. Paperwork**: standards of record in base.py docstring sections; GLOSSARY rows "UI group", "@query", "Mode-bound group / active_mode", "Bench VI" (+ update @monitored/@control and Shared-instrument mode discipline rows); folder READMEs updated in the same commit as the code they describe.

## Waves (each = one reviewable commit, `make check` green)

### Wave 1: core primitives
- `cryosoft/core/plan.py`: add `UIGroup`, `QuerySchema` (+validate).
- `cryosoft/core/decorators.py`: parametrized `@monitored`, `group=` on `@control`, new `query()`, discovery helpers. All new metadata opaque (C1).
- `cryosoft/virtual_instruments/base.py`: `ui_groups` ClassVar, @query wrapping, `_make_mode_wrapper`, class-creation validation, docstring standard sections.
- Tests: new `tests/test_ui_groups_and_query.py` (mock VIs: group validation failures at class creation, mode refusal + entry exemption, query limit enforcement, QuerySchema mismatch reporting); conformance additions: group declarations valid, moded-group => active_mode monitored, query declarations well-formed (schema present, params defaulted), mode refusal on sim-built VIs. Existing VIs pass vacuously.
- Docs: GLOSSARY rows, `virtual_instruments/README.md`.

### Wave 2: dispatch
- `cryosoft/core/station.py`: `execute_vi_query` (verifies `_is_query`; beside execute_vi_action ~:631).
- `cryosoft/core/orchestrator.py`: `submit_vi_query`, `kind` on queue entries, drain branch with schema validation, `query_ready` signal.
- Tests (orchestrator suite): query admitted in IDLE, blocked mid-procedure with action_blocked, query_ready payload, schema-violating result -> action_failed, non-@query method refused by Station; conformance query round-trip via sims.

### Wave 3: GUI (follow gui-edit skill)
- `cryosoft/gui/instrument_panel.py`: group-box rendering in `_build_layout` (flat ungrouped monitored, declared groups in order with monitored/control/query rows, flat ungrouped controls), `_build_query_row` + result area (objectName `{vi}_{method}_query_result`; scalars as label rows, arrays as one capped QTableWidget), `modeActive` handling in `_on_states_updated`, `query_ready` connection (window-level forward per destruction-order rule).
- `cryosoft/gui/theme.py`: group-box style + `QGroupBox[modeActive="false"]` dimming (tokens + build_stylesheet only).
- `cryosoft/gui/instrument_front_panel.py`: show all queries.
- Reuse: `param_form.build_param_widget/build_param_tooltip/collect_value`; existing `_build_control_row` grid logic.
- Tests (`tests/test_gui.py`, `_SpecControlVI` pattern): group boxes by objectName in declared order; ungrouped VI unchanged; inactive group disabled + property set; entry control enabled; submit spy sees submit_vi_query; result area populates on synthetic query_ready.
- Verify additionally: offscreen smoke `.claude/skills/gui-edit/scripts/gui_smoke.py`, inspect PNGs.

### Wave 4: L0 waveform
- `cryosoft/drivers/keithley_6221.py` + `sim_keithley_6221.py`: methods per decision E; sim `_mode` transitions model exclusivity + self-recovery.
- Tests (`tests/test_l0_simulated.py`): configure->arm->start->abort lifecycle; arm recovers from DELTA; abort restores DC baseline; get_mode tracks. Parity test covers signatures automatically.
- Docs: `drivers/README.md` waveform section + unverified-on-hardware note.

### Wave 5: pilot bench VI + role plumbing
- New `cryosoft/virtual_instruments/keithley_6221_bench.py`: `Keithley6221BenchVI(BaseVirtualInstrument)`, roles `{source, meter}`.
  - `ui_groups`: `("dc", "DC source", mode="DC", entry=("set_dc_current",))`, `("delta", "Delta mode", mode="DELTA", entry=("arm_delta",))`, `("waveform", "Waveform", mode="WAVEFORM", entry=("arm_waveform",))`.
  - @monitored: `active_mode`, `source_enabled`, `source_current_A`, `compliance_V`, `waveform_armed`.
  - @control: dc: `set_dc_current`, `set_compliance`, `set_output`; delta: `arm_delta(current_A, n_readings, delay_s, compliance_V, voltmeter_range_V)`, `stop_delta`; waveform: `arm_waveform(waveform, amplitude_A, frequency_Hz, offset_A)`, `start_waveform`, `stop_waveform`. `control_limits`: current/amplitude/offset -> `max_current`, compliance -> `max_compliance_V` (values from config init_params).
  - @query: `read_delta(n_readings=10, period_s=0.05)` -> QuerySchema(scalars n_valid/mean_V/std_V, arrays delta_voltage_V), NaN-padding short returns; `check_meter()` -> scalar presence/idn.
  - `standby()`: abort waveform + stop delta + output off (safe for Standby-All).
- Role plumbing: `station.py` registry docs accept `"bench"`; `test_conformance.py` `CONFIG_VI_TYPES` += "bench"; `monitor_window.py:452-460` include bench VIs (type tag "Bench").
- Configs: `configs/sim_cryostat/devices.yaml` + `configs/12t-cryo/devices.yaml` add `keithley_6221_bench` sharing existing `keithley_6221`/`keithley_2182a` drivers, init_params limits.
- Tests: `tests/test_l1_virtual_instruments.py` bench lifecycle on sims; extend shared-6221 handoff block (:409-473): bench waveform -> delta VI initiate recovers `_mode`; delta running -> bench entry control recovers; inactive-mode refusal end-to-end. Conformance covers the rest automatically.
- Docs (same commit): GLOSSARY "Bench VI"; `virtual_instruments/README.md`; `measurement/README.md` discipline update (bench entry controls are mode-establishing methods). Write `directives/complete-instrument-vis.md`: the three standards in brief + the FUTURE capability-based generic-method evolution (methods as generic classes over instrument-VI capabilities, N+M instead of NxM, registry then simplifies toward "system + other"), explicitly out of scope now.

## Verification (per wave and final)
- Per wave: `make check` from `.venv` (ruff, lint-imports 12/12, pytest non-hardware).
- Wave 3 additionally: `python .claude/skills/gui-edit/scripts/gui_smoke.py` offscreen screenshots, visual inspection.
- Final: run app against sim config (`.venv\Scripts\python -m cryosoft.main`, sim_cryostat) and exercise the bench card: mode switching, greying, delta read query result rendering.
- LOGBOOK.md entry per session (project rule); commits on `develop`, no Claude co-author line.

## Risks / notes
- Waveform SCPI unverified until a hardware bench session at the 12t-cryo; sim-first delivery is safe. `get_mode()` on real hardware derives from `:SOUR:WAVE:ARM?` / `:SOUR:DELT:ARM?`; if hardware reports differently only the driver method changes.
- A long query freezes the GUI for its duration (single-threaded by design); documented budget <= ~10 s, bench defaults kept small.
- Deferred by user decision: auto-standby of bench VIs on procedure start (residual: bench-armed output stays live until next mode-establishing call); the generic-measurement-method refactor (recorded in the directive).
- OneDrive/parallel-session hazard (memory): commit each wave promptly; verify edits survived before building on them.
