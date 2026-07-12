# CryoSoft Glossary

Canonical definitions of project terms. When a human and an agent (or two
agents) talk about CryoSoft, these are the meanings in force. If a term is
used in code, docs, or conversation with a different meaning than defined
here, the glossary wins; if a new recurring term appears, add it here in the
same commit that introduces it.

## Architecture layers (bottom to top)

| Term | Definition |
|---|---|
| **Layer (L0–L5)** | The six-layer architecture: L0 Driver → L1 Virtual Instrument → L2 Station/Config → L3 Orchestrator → L4 Procedure → L5 Data Manager, with the GUI on top. Lower layers never import higher ones; the machine-checked rules live in `pyproject.toml` under `[tool.importlinter]`. |
| **Driver (real driver)** | L0. A Python class that talks to one physical instrument over PyVISA (GPIB/serial). Constructor takes exactly one required argument: the VISA resource string. Lives in `cryosoft/drivers/`, imports nothing from CryoSoft except `core.exceptions`. |
| **Sim driver** | A drop-in simulated twin of a real driver (`sim_` filename prefix), used for all testing without hardware. Must expose exactly the same public API as its real twin — enforced by `tests/test_conformance.py`. |
| **Virtual Instrument (VI)** | L1. A device-role abstraction (magnet, temperature controller, level meter, measurement) built on one or more drivers. VIs never import drivers; the Station constructs driver objects and injects them into the VI constructor as a `drivers` dict (dependency injection). All VIs subclass `BaseVirtualInstrument` and use `__init__(self, drivers, **init_params)`. |
| **Station** | L2. The runtime registry of all VIs, built from a config directory by `build_station()`. The only layer that touches both VIs and (via config) driver classes. Dispatches system targets, measurement commands, and safety checks. |
| **Config** | A directory under `cryosoft/configs/<name>/` holding `devices.yaml` (drivers, VIs, wiring, safety limits) and `monitor.yaml` (tick interval, error threshold). Configs are the single source of truth for addresses, limits, and mappings — never hardcode these. |
| **Shipped vs user config** | *Shipped* configs are the git-tracked, read-only baselines under `cryosoft/configs/`; the GUI never edits them (a safe fallback that cannot be corrupted). *User* configs are editable copies under `%APPDATA%/CryoSoft/configs/`, created by copy-on-edit (`ConfigCatalog.fork_shipped`). |
| **Config catalog** | `core.config_catalog.ConfigCatalog` — discovers shipped + user configs, forks shipped→user, and keeps a named **config version** history per user config (save/list/restore). |
| **Config version** | A saved snapshot of a user config under `.versions/<id>/` with a `meta.json` (label + timestamp). Every editor save creates one; restoring copies it back over the active files. |
| **Active config** | The config the app loads on launch, stored as a path in `QSettings` (`ActiveConfig/path`). Switching it warns, saves the session, and restarts. |
| **Startup fallback chain** | `build_station_with_fallback()` tries the active config, then its shipped baseline, then the always-loadable `sim_cryostat`, so a corrupted config surfaces a warning instead of crashing startup. Each candidate is checked by `validate_config_dir()` (a dry-run that imports classes and resolves driver refs without instantiating hardware). |
| **Orchestrator** | L3. The state machine that executes a procedure: it ticks, ramps system VIs toward targets, waits, triggers measurements, and enforces safety. Talks only to the Station. Monitoring runs in its tick loop. |
| **Procedure** | L4. A declarative measurement sequence (e.g. field sweep, temperature sweep) subclassing `BaseProcedure`. Declares *what* to do via the four-method interface; the Orchestrator decides *how*. Procedures never import drivers or VIs. |
| **Data Manager** | L5. Writes measurement data to HDF5 files. Standalone: knows nothing about instruments. Created by procedures in `initiate()`. |
| **GUI** | PyQt6 windows (monitor, procedure, instrument panels). Talks to the system only through the Orchestrator and Station APIs; never imports drivers or concrete VIs. |

## Runtime concepts

| Term | Definition |
|---|---|
| **Tick / tick loop** | The Orchestrator's periodic heartbeat (`tick_interval_ms` in `monitor.yaml`). Each tick polls VI state, advances ramps, and checks safety. |
| **System targets** | Dict returned by procedures, `{"vi_name": {"target": value, "rate": ...}}`, telling the Station which system VIs to ramp where. |
| **Measurement commands** | Dict returned by procedures, `{"vi_name": {"method": kwargs}}`, dispatched by the Station to measurement VIs. |
| **Ramp / RampableVI** | Gradual, rate-limited approach to a setpoint (field, temperature). VIs that support it implement the `RampableVI` interface (`start_ramp`, `advance_ramp`, `ramp_status`). |
| **`@monitored` / `@control`** | Decorators from `core/decorators.py` marking VI methods. `@monitored`: read-only, polled every tick into `get_state()`. `@control`: user-callable action, rendered as a button/input in the GUI instrument panel. |
| **vi_type (class)** | Class attribute of a VI describing its device role: `magnet`, `temperature`, `measurement`, `level`. Set by the typed base classes. |
| **vi_type (config/registry)** | The *registry* role a VI plays in a Station, set in `devices.yaml`: `system` (rampable, receives system targets), `measurement` (receives measurement commands), or `level` (safety monitoring). Distinct from the class `vi_type`. |
| **Persistent mode** | Superconducting-magnet operating mode where the field is held by a closed superconducting loop and the switch heater is off. Handled by `SuperconductingMagnetPersistentVI`. |
| **VISA / GPIB** | The instrument-communication standard (PyVISA library) and the bus most lab instruments use. A "resource string" like `GPIB0::19::INSTR` addresses one instrument. |
| **SI units rule** | All APIs use Tesla, Kelvin, Ampere, Volt, second. Display formatting (mK, µA) happens only in the GUI. |

## GUI concepts

| Term | Definition |
|---|---|
| **Dock layout** | The Monitor window's layout: everything below the header/banner lives inside an inner `QMainWindow` used purely as a docking area (`dock_host`, `setDockNestingEnabled(True)`). Every panel is a `QDockWidget` — one `dock_{vi_name}` per system/level `InstrumentPanel`, one `dock_trend_{n}` per `TrendPlotPanel`, plus `dock_other_devices`/`dock_log`/`dock_sample_info` — arranged via `addDockWidget`/`splitDockWidget`/`tabifyDockWidget`/`resizeDocks`. A View menu exposes each dock's `toggleViewAction()` for collapse/restore. Replaces the earlier fixed splitter grid, which crushed panels at real window sizes. |
| **MonitorHistory** | `cryosoft/gui/monitor_history.py`. A Qt-free, pure-Python ring buffer that accumulates flattened `{vi_name}_{field_name}` time-series history from `Orchestrator.states_updated`, one bounded `deque` per key, for the trend docks' plots. |
| **Trend docks** | 1–4 `TrendPlotPanel`s, each wrapped in its own `dock_trend_{n}` `QDockWidget`, each showing one variable vs wall-clock time read from a shared `MonitorHistory`. The View menu's "Add trend plot" action adds docks up to a cap of 4 (disabling once reached); each panel's own remove button drops it, never below a floor of 1 — distinct from a dock's own close button, which only hides it (recoverable via its View-menu toggle action). The two default docks opportunistically select a temperature/level reading (not a setting/rate field) once `MonitorHistory` has data. |

## Development harness

| Term | Definition |
|---|---|
| **Layer contracts (C1–C8)** | The machine-checked import rules in `pyproject.toml` `[tool.importlinter]`. Run with `make contracts`. A broken contract means the change crosses a layer boundary — route through the proper interface instead of editing the contract. |
| **Conformance tests** | `tests/test_conformance.py`. Auto-discover every driver, VI, procedure, and config and check they follow their interface contracts. A new module is covered automatically, with no test-writing needed. |
| **`make check`** | The blocking quality gate (lint + contracts + tests). Run it before declaring any work done; CI runs exactly the same targets. |
| **`hardware` marker** | Pytest marker for tests needing physical instruments. Excluded by `make test` and CI; run manually at the cryostat. |
| **LOGBOOK.md** | The running development log at the project root (newest first, not git-tracked). Every work session — human or agent — ends by prepending an entry. |
