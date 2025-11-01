**Prompt for Claude Code: Cryostat Instrument Control Architecture**

### Overview

You are helping design a **modular, multi-level instrument control software** for a cryogenic experimental setup. The goal is to create a clean, extensible architecture that allows the integration and automation of multiple devices via YAML-based configuration.

### Device Categories

The setup includes three categories of devices:

1. **Temperature controllers**
2. **Superconducting magnet power supplies** (referred to as **magnets**)
3. **Level meters** (helium/nitrogen)

Each category can include between 1 and 3 devices. Specific instruments may vary, but all share the same functionalities with slightly different drivers.

The **Keithley instruments (6221, 2400, 2182)** are *not* part of the logical instruments. They exist as **separate devices** in the `InstrumentRegistry`.

### Configuration Schema (Example)

```yaml
InstrumentRegistry:
  LogicalInstruments:
    Cryostat:
      VTITemperatureController:
        driver: lakeshore_336
      SampleTemperatureController2(optional):
        driver: oxford_itc503
      Magnet1:
        driver: oxford_ips120
      Magnet2(optional):
        driver: oxford_ips120
      Magnet3(optional):
        driver: oxford_ips120
      Helium level meter:
        driver: oxford_ilm
      Nitrogen level meter (optional):
        driver: oxford_ilm

  Keithley6221:
    driver: keithley_6221
  Keithley2400:
    driver: keithley_2400
  Keithley2182:
    driver: keithley_2182
```

### Layered Architecture

To ensure clean separation of responsibilities, the system should follow a **three-layered architecture**:

1. **Driver Layer:**

   * Handles low-level communication with each physical device.
   * Contains only fundamental read/write and hardware interaction logic.

2. **Action Layer:**

   * Introduces a higher abstraction level above drivers.
   * Contains **instrument-specific, multi-step commands**, such as:

     * `initiate()`
     * `standby()`
     * `ramp_temperature(target, rate)`
     * `ramp_field(target, rate)`
   * Encapsulates procedural sequences that combine multiple driver calls safely.
   * Reduces the complexity and direct responsibility of the logical instrument layer.
   * Each device category (magnet, temperature controller, level meter) implements its own action handler with consistent interfaces.
   * Instrument specific

3. **Logical Instrument Layer (Cryostat):**

   * Renamed from `LogicalInstrument` to **`Cryostat`**.
   * Provides hierarchical access to logical devices and their high-level action interfaces:

     ```python
     cryostat["magnet1"].set_field(0.5)
     cryostat["temperature_controller1"].set_setpoint(4.2)
     ```
   * Each logical device (temperature controller, magnet, level meter) is accessible directly from the Cryostat instance.
   * **1-second read/write lock** for all I/O operations to ensure safe multi-threaded access and enable live monitoring.
   * Provides **live monitoring** functions for:

     * **Magnets:** field, current, heater status
     * **Temperature controllers:** temperature, setpoint, heater power
     * **Level meters:** helium/nitrogen level, measurement mode
   * Delegates control operations (initiate, ramping, standby) to the **Action Layer**.
   * Can be accessed like any other instrument in pymeasure system @PYMEASURE_REFERENCE.md.

#### Keithley Instruments

* Exist as separate entries in the `InstrumentRegistry`.
* Not part of the Cryostat’s logical instrument layer.
* Controlled independently using their drivers or custom routines.

### Architectural Design Principles

To ensure modularity, maintainability, and scalability, follow the **SOLID principles**:

1. **Single Responsibility Principle** – Each class handles one clear responsibility (e.g., driver communication, procedural control, monitoring, YAML parsing).
2. **Open/Closed Principle** – Add new device types or drivers without modifying existing code; support driver discovery through configuration.
3. **Liskov Substitution Principle** – Any subclass (e.g., different magnet driver) should be fully interchangeable without breaking higher-level code.
4. **Interface Segregation Principle** – Each device type should implement only relevant interfaces (e.g., `IMeasurable`, `IControllable`, `IActionable`).
5. **Dependency Inversion Principle** – High-level Cryostat logic depends on abstract action interfaces, not concrete driver implementations.

### Software Goals

* The Cryostat class should behave like a unified **`Instrument`** in `pymeasure`.
* YAML configuration defines which classes of action layer to use and number of instruments avaiable..
* The system must support driver interchangeability, runtime configuration, and layering of logic.
* Code should be thread-safe and scalable to multiple device groups.

### Deliverable

Implement a modular Python codebase that meets these design requirements. Include clear abstractions for:

* Driver base classes
* Action layer modules for each device type
* Logical device classes (Cryostat and subcomponents)
* Instrument registry and YAML loader

The resulting design should allow seamless execution of commands like:

```python
cryostat["magnet1"].set_field(0.5)
cryostat["temperature_controller1"].set_setpoint(4.2)
cryostat["temperature_controller1"].ramp_temperature(4.2, rate=0.5)
cryostat["magnet1"].ramp_field(0.5, rate=0.1)
keithley_2400.measure_iv_curve()
```
