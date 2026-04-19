# CryoSoft

Instrument-agnostic cryostat operating system — Kläui Lab, JGU Mainz.

---

## Contents

- [Motivation](#motivation)
- [Architecture](#architecture)
- [Install](#install)
- [Run](#run)
- [Config: connect a real cryostat](#config-connect-a-real-cryostat)
- [Add a new driver](#add-a-new-driver)
- [Add a new procedure](#add-a-new-procedure)
- [Current drivers](#current-drivers)

---

## Motivation

A typical cryostat measurement setup is not a fixed machine — it is a composition of interchangeable instruments. The magnet power supply might be an Oxford IPS 120-10 in one setup and an Oxford Mercury iPS in another. The temperature controller might be an Oxford ITC 503 or a Lakeshore 335. The measurement chain for transport experiments might use a Keithley 6221 + 2182A in delta mode, or a Keithley 2400 SMU in a different configuration. Each time a component is swapped, or the setup is moved to a different cryostat, conventional lab software requires a significant rewrite.

CryoSoft was written to eliminate that rewrite. The core idea is borrowed from the [QCoDeS](https://qcodes.github.io/Qcodes/) station concept: instruments are registered in a central **Station** through a YAML configuration file, and every measurement procedure talks to the Station — never to a specific instrument directly. Swapping a magnet PSU means editing one line in a YAML file. No Python changes, no refactoring.

Beyond instrument abstraction, CryoSoft addresses a second problem common in cryogenic labs: the need for continuous, unattended monitoring. Running a magnet sweep or a temperature ramp overnight requires confidence that the system will respond safely to unexpected events — a sudden pressure spike in the helium line, an anomalous heater output, or a cryogen level drop. CryoSoft's **Orchestrator** is a cooperative state machine that runs a monitoring tick on every cycle alongside any active procedure. If a safety threshold is breached, the system transitions to standby autonomously, without requiring a human to be present.

Every data point saved by CryoSoft is accompanied by a full snapshot of the system state at the moment of acquisition: sample temperature, VTI temperature, heater powers, magnetic field, and cryogen level. This metadata travels with the data in HDF5 format, making it possible to retrospectively correlate measurement artifacts with transient cryogenic events — a pressure fluctuation, a helium refill, a heater glitch — and remove or flag them during analysis.

The goal of CryoSoft is to standardize the measurement workflow across any instrument configuration: write a procedure once, run it on any compatible setup.

---

## Architecture

CryoSoft is organized in six strict layers. Each layer only knows about the layer immediately below it. This constraint is what makes instrument swapping safe — a change at L0 cannot propagate upward unless the VI interface is also changed.

```
  ┌──────────────────────────────────────────────────────────────────┐
  │  GUI  (PyQt6)                                                    │
  │  Auto-generated panels from @monitored / @control decorators.   │
  │  Displays live state; dispatches user commands to Orchestrator.  │
  └───────────────────────────┬──────────────────────────────────────┘
                              │
  ┌───────────────────────────▼──────────────────────────────────────┐
  │  Orchestrator  (L3)                                              │
  │  QTimer-driven cooperative state machine. On every tick:        │
  │    1. Poll all VIs → update system state snapshot               │
  │    2. Check safety limits → go to standby if breached           │
  │    3. Advance the active procedure by one step                  │
  │    4. Write data point + full metadata to Data Manager          │
  └────────────┬──────────────────────────────┬───────────────────── ┘
               │                              │
  ┌────────────▼────────────┐   ┌─────────────▼──────────────────────┐
  │  Procedures  (L4)       │   │  Data Manager  (L5)                │
  │  Declarative sweep      │   │  HDF5 writer. Each data point      │
  │  classes. Define sweep  │   │  carries a full system-state       │
  │  targets and measure    │   │  snapshot as metadata.             │
  │  commands per step.     │   └────────────────────────────────────┘
  │  Instrument-agnostic.   │
  └────────────┬────────────┘
               │
  ┌────────────▼──────────────────────────────────────────────────────┐
  │  Station + YAML Config  (L2)                                      │
  │  Reads devices.yaml. Instantiates drivers and wraps them in VIs.  │
  │  The Station is the only place that knows which instruments are   │
  │  physically present.                                              │
  └────────────┬──────────────────────────────────────────────────────┘
               │
  ┌────────────▼──────────────────────────────────────────────────────┐
  │  Virtual Instruments  (L1)                                        │
  │  Typed wrappers with behavior-based names:                        │
  │    SuperconductingMagnetVI   ← any magnet PSU                    │
  │    SampleTemperatureVI       ← any temperature controller        │
  │    DeltaModeMeasurementVI    ← any delta-mode source+meter pair  │
  │    CryogenLevelMeterVI       ← any level meter                   │
  │  Decorated with @monitored / @control for auto GUI generation.   │
  │  Swap the underlying driver via YAML — the VI interface is fixed. │
  └────┬──────────────┬────────────────┬─────────────────────────────┘
       │              │                │
  ┌────▼─────┐  ┌─────▼──────┐  ┌─────▼────────────┐
  │ Oxford   │  │ Oxford     │  │ Keithley 6221    │
  │ Mercury  │  │ ITC 503 /  │  │ + 2182A  /  2400 │
  │ iPS /    │  │ Lakeshore  │  │ (swap via YAML)  │
  │ IPS 120  │  │ 335        │  └──────────────────┘
  │ (swap    │  │ (swap      │
  │ via YAML)│  │ via YAML)  │
  └──────────┘  └────────────┘

  L0 — Real Drivers: any Python class wrapping PyVISA, PyMeasure, or
  a custom protocol. A simulated version exists for every real driver.
```

**Layer boundary rules:**
- Drivers (L0) never import from VIs or above.
- VIs (L1) never import from the Orchestrator or above.
- Procedures (L4) never import drivers — only the Station (L2).
- The GUI never talks to drivers or VIs directly — only through the Orchestrator.

---

## Install

```bash
cd CryoSoft
python -m venv cryosoft/.venv
cryosoft/.venv/Scripts/activate       # Windows
pip install pyqt6==6.7.1 pyqtgraph h5py numpy ruamel.yaml pyvisa pymeasure
```

> PyQt6 6.11+ has a DLL conflict with Anaconda on Windows. Pin to 6.7.1.

---

## Run

**Simulation mode (no hardware):**

```bash
cryosoft/.venv/Scripts/activate
python -m cryosoft.main
```

Starts against `cryosoft/configs/sim_cryostat/` — all drivers are simulated.

**Real hardware:**

```bash
python -m cryosoft.main --config cryosoft/configs/a-sample-real-cryostat
```

Or edit the `config_path` line in `cryosoft/main.py` to point to your config directory.

**Tests:**

```bash
pytest tests/ -q
```

---

## Config: connect a real cryostat

1. Create a new directory: `cryosoft/configs/your_cryostat/`
2. Copy `devices.yaml` from `sim_cryostat/` and replace driver classes, VISA addresses, and `init_params`.
3. Copy `monitor.yaml` and set `tick_interval_ms` (1000 ms = 1 s per tick is typical for real hardware).
4. Pass `--config cryosoft/configs/your_cryostat` at launch. No Python changes needed.

**Key `devices.yaml` fields:**

```yaml
real_drivers:
  my_magnet:
    class: cryosoft.drivers.oxford_mercury_ips.OxfordMercuryiPS
    address: "ASRL10::INSTR"

virtual_instruments:
  magnet:
    class: cryosoft.virtual_instruments.magnet.superconducting_magnet_persistent.SuperconductingMagnetPersistentVI
    drivers: {main: my_magnet}
    vi_type: system
    init_params:
      amperes_per_tesla: 7.954
      max_current: 90.0
      min_current: -90.0
      default_ramp_rate: 1.2       # A/min
      ramp_segments:
        - {max_current_A: 44.0, rate_A_per_min: 12.0}
        - {max_current_A: 76.0, rate_A_per_min: 6.0}
        - {max_current_A: 84.0, rate_A_per_min: 2.4}
        - {max_current_A: 90.0, rate_A_per_min: 1.2}
      switch_heater_warmup_ticks: 60    # ticks × tick_interval_ms = wall-clock wait
      switch_heater_cooldown_ticks: 60
```

See `cryosoft/configs/a-sample-real-cryostat/devices.yaml` for a complete real-hardware example.

---

## Add a new driver

A driver is any Python class satisfying three rules:

1. It is a Python class.
2. `__init__` accepts a single VISA resource string.
3. It lives under `cryosoft/drivers/` and is importable by dotted path.

Steps:

1. Create `cryosoft/drivers/your_instrument.py`.
2. Create `cryosoft/drivers/sim_your_instrument.py` — simulated physics, no hardware needed.
3. Reference it in `devices.yaml` under `real_drivers`.
4. Run the driver test harness (`tests/driver_test_harness.py`) before connecting to the VI layer.

Example skeleton:

```python
class YourInstrument:
    def __init__(self, resource_string: str) -> None:
        import pyvisa
        self._instr = pyvisa.ResourceManager().open_resource(resource_string)

    def get_value(self) -> float:
        return float(self._instr.query("READ?"))
```

---

## Add a new procedure

1. Create `cryosoft/procedures/your_procedure.py`.
2. Subclass `BaseProcedure`; set `name`, `sweep_parameters`, `system_parameters`, `measurement_parameters`; implement the four methods (`initiate`, `change_sweep_step`, `measure`, `standby`).
3. The procedure appears automatically in the GUI dropdown on next launch.

---

## Current drivers

| File | Instrument |
|------|-----------|
| `keithley_6221.py` / `sim_keithley_6221.py` | Keithley 6221 current source (delta mode) |
| `keithley_2182a.py` / `sim_keithley_2182a.py` | Keithley 2182A nanovoltmeter |
| `sim_keithley_2400.py` | Keithley 2400 SMU (sim only) |
| `oxford_mercury_ips.py` / `sim_oxford_ips120.py` | Oxford Mercury iPS-M / IPS 120-10 magnet PSU |
| `oxford_itc503.py` / `sim_oxford_itc503.py` | Oxford ITC 503 temperature controller |
| `oxford_ilm200.py` / `sim_oxford_ilm200.py` | Oxford ILM 200 cryogen level meter |
| `lakeshore_335.py` | Lakeshore 335 temperature controller |
