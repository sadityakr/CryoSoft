# procedures/

## Purpose

The `procedures/` package contains concrete measurement procedures. Each procedure is a declarative class that describes *what* the experiment should do — which instruments to target, how to configure measurements, and how to step through a sweep. The `Orchestrator` handles *how* it executes (ramping, waiting, monitoring, data saving).

Procedures are the primary extension point for new experiment types. Adding a new measurement type means adding one file here.

## Architecture layer

L4 — Procedures. Sits above L3 (Orchestrator) and L2 (Station). Depends on `DataManager` (L5) for HDF5 output.

```
GUI → Orchestrator → Procedure → Station → Virtual Instruments → Drivers
                              ↘ DataManager → HDF5
```

## Entry (what comes in)

Each procedure receives at construction time:
- `station`: the `Station` instance (access to all VIs).
- `sample_info`: `{"sample_name": str, "sample_id": str, "comments": str}` — entered by the user in the GUI.
- `data_directory`: path where the HDF5 file will be created.
- `**param_values`: all procedure-specific parameters from the GUI form (must match `parameters` class attribute).

## Exit (what goes out)

The Orchestrator calls four methods in sequence:

| Method | Returns | Called when |
|--------|---------|-------------|
| `initiate()` | `(system_targets, measurement_commands, wait_time)` | Procedure starts |
| `change_sweep_step()` | `(system_targets, wait_time)` or `None` | After each measurement |
| `measure()` | nothing (writes to HDF5) | System is stable at current point |
| `standby()` | `(system_targets, measurement_commands, wait_time)` | Sweep complete or aborted |

**Dict formats:**

```python
# system_targets — what the cryostat should be (Orchestrator ramps to these)
{"magnet_x": {"target": 0.5}, "temperature_vti": {"target": 10.0}}

# measurement_commands — how to configure measurement VIs
{"iv_measurement": {"configure": {"method": "delta_mode", "current": 1e-6, "n_readings": 100}}}
```

No ramp rates in `system_targets` — rates come from YAML config in the VI.

## Interface contract

All procedures must subclass `BaseProcedure` from `cryosoft.core.procedure`:

```python
from cryosoft.core.procedure import BaseProcedure

class MyProcedure(BaseProcedure):
    name = "My Procedure"
    description = "One-line description"
    parameters = {
        "param_name": {"type": float, "default": 1.0, "unit": "T", "description": "..."},
    }

    def _build_sweep_array(self) -> list: ...
    def initiate(self) -> tuple[dict, dict, float]: ...
    def change_sweep_step(self) -> tuple[dict, float] | None: ...
    def measure(self) -> None: ...
    def standby(self) -> tuple[dict, dict, float]: ...
```

**Rules:**
- Procedures never import from `drivers/` or `virtual_instruments/` directly.
- Procedures access instruments only through `self._station` (VI methods).
- `measure()` must create a `DataManager` in `initiate()` (stored as `self._data_manager`) and call `self._data_manager.save_datapoint()`.
- `standby()` must call `self._data_manager.close()` to ensure data is flushed and trimmed.
- All parameters must be declared in `parameters` — no hardcoded values in logic.
- SI units everywhere: tesla, kelvin, amperes, volts, seconds.

## How to add a new procedure

1. Create `procedures/your_procedure.py` with the front-matter block (Workspace Rule 1).
2. Subclass `BaseProcedure` and implement all five required methods.
3. Declare all user-facing parameters in the `parameters` class attribute.
4. Build the sweep array in `_build_sweep_array()` from `self._params`.
5. Create a `DataManager` in `initiate()` with the correct `data_config`.
6. Write tests in `tests/test_l4_procedure.py` (or a new file for the new procedure).
7. Add the file to this README's file list below.

## Files

| File | Description |
|------|-------------|
| `__init__.py` | Package marker (empty) |
| `field_sweep_iv.py` | `FieldSweepIV` — sweeps magnetic field (magnet_x), measures IV via delta-mode (Keithley 6221 + 2182A). Requires: magnet_x, temperature_vti, iv_measurement VIs in Station. |
