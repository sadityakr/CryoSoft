# CryoSoft

Instrument-agnostic cryostat operating system — Kläui Lab, JGU Mainz.

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

## Architecture (reference)

```
┌─────────────────────────────────────────────────┐
│  GUI                                            │
│  PyQt6 windows — auto-generated from decorator │
│  metadata (@monitored / @control).              │
├─────────────────────────────────────────────────┤
│  Procedures (L4)                                │
│  Declarative sweep classes. Declare system      │
│  targets and measurement commands per step.     │
├─────────────────────────────────────────────────┤
│  Orchestrator (L3)                              │
│  Cooperative QTimer state machine. Advances     │
│  ramps, dispatches measurements, safety checks. │
├─────────────────────────────────────────────────┤
│  Station + YAML Config (L2)                     │
│  VI registry built from devices.yaml.           │
├─────────────────────────────────────────────────┤
│  Virtual Instruments (L1)                       │
│  Typed wrappers with @monitored / @control.     │
│  Behavior-named (SuperconductingMagnetVI, not   │
│  IPS120VI) — swap hardware via YAML only.       │
├─────────────────────────────────────────────────┤
│  Drivers (L0)                                   │
│  Any Python class: PyVISA, PyMeasure, custom.  │
│  Simulated versions for every real driver.     │
└─────────────────────────────────────────────────┘
```

**Layer boundary rules:**
- Drivers never import from VIs or above.
- VIs never import from the Orchestrator or above.
- Procedures never import drivers — only the Station.
- The GUI never talks to drivers or VIs directly — only through the Orchestrator.

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

---

See [STATUS.md](STATUS.md) for the full implementation log.
