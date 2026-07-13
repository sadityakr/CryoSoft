# configs/

## Purpose
The single source of truth for what a given cryostat setup is made of. Each
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
limits, ramp segments, mux routes) taken verbatim from `devices.yaml`, and a
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
  (`system` / `measurement` / `level` / `switch`). `init_params` carries the
  control-validation limits (`min/max_temperature_K`, `max_ramp_rate_K_per_min`,
  `max_current`, field bounds), magnet `ramp_segments`, and switch `routes`.

`monitor.yaml` structure: a `monitor:` block with `tick_interval_ms` (the single
QTimer tick period) and `max_vi_errors` (consecutive VI-error tolerance before
escalation).

## How to add a new module
1. Create `configs/<name>/` with a `devices.yaml` and a `monitor.yaml`.
2. In `real_drivers`, list each instrument with its driver class and address.
3. In `virtual_instruments`, build each VI on those drivers, setting `vi_type`
   and the `init_params` limits/routes for that setup.
4. Conformance discovers the new directory automatically; run `make check`. For a
   guided setup with identity checks and a preflight report, use the
   `setup-commission` skill (writes a per-setup `setup.md`).

## Files
Each config directory holds the same two files; contents differ per setup.

- `sim_cryostat/` — fully simulated reference station (dual magnets X/Y, VTI +
  sample temperature, level meter, delta-mode and DC measurement VIs, 4-route
  switch matrix). The station the procedure/orchestrator tests build against.
  - `devices.yaml` — all `Sim*` drivers at `SIM::*` addresses; the canonical VI
    graph and `init_params`.
  - `monitor.yaml` — `tick_interval_ms: 3000`, `max_vi_errors: 3`.
- `a-sample-real-cryostat/` — real Kläui Lab station: Oxford Mercury iPS-M magnet,
  Oxford ITC 503 (VTI), Lakeshore 335 (sample), Oxford ILM 200 levels, Keithley
  6221 + 2182A delta-mode. Addresses partly placeholder pending bench check.
  - `devices.yaml` — real `cryosoft.drivers.*` classes at PyVISA addresses.
  - `monitor.yaml` — monitor loop settings.
- `sim_real_cryostat/` — digital twin of `a-sample-real-cryostat/`: identical VI
  graph, names, and `init_params`, with each real driver swapped for its `Sim*`
  equivalent (Lakeshore 335 stands in as a second `SimOxfordITC503`, since no
  `SimLakeshore335` exists). Lets the real setup be exercised end-to-end without
  hardware.
  - `devices.yaml` — sim drivers, real-config topology.
  - `monitor.yaml` — monitor loop settings.

tests: `tests/test_conformance.py` (schema), `tests/test_config_catalog.py`
(discovery / copy-on-edit fork / named versions), `tests/test_config_validation.py`
(limit and reference validation).
