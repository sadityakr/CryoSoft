# virtual_instruments/level/

## Purpose
Virtual instruments for cryogen level meters. Provides a standardised interface
for reading helium and nitrogen levels and detecting low-helium conditions,
regardless of which level monitor is installed.

## Architecture layer
L1 — Virtual Instruments.

## Entry (what comes in)
Driver dict `{"main": <level meter real driver>}` and `init_params`:
`helium_low_threshold` (%, default 20), `buffer_size` (int, default 5).

## Exit (what goes out)
`@monitored` readings: `helium_level() → float (%)`,
`nitrogen_level() → float (%)`, `get_refresh_rate() → int`.
`@control` actions: `set_refresh_rate(mode: int)` where STANDBY=0, SLOW=1, FAST=2.
`helium_low() → bool` — majority-vote over a rolling buffer for noise immunity.
`evaluate_safety()` reports the debounced `{"helium_low": ...}` verdict to
`Station.check_safety()` every tick (a sustained low level, or a disconnected
level meter, escalates to EMERGENCY).

## Interface contract
All classes here inherit `LevelMeterBase` from `virtual_instruments/base.py`.

## How to add a new level VI
1. Subclass `CryogenLevelMeterVI` or `LevelMeterBase`.
2. Map the instrument's refresh modes to the STANDBY/SLOW/FAST constants.
3. Register in `devices.yaml`.
4. Add tests to `tests/test_l1_new_vis.py`.

## Files
- `cryogen_level_meter.py` — `CryogenLevelMeterVI`: rolling-buffer helium low
  detection, standardised 3-mode refresh rate interface. Key API:
  `@monitored helium_level` / `nitrogen_level` / `get_refresh_rate`,
  `@control set_refresh_rate`, `helium_low()`, `evaluate_safety()`. tests:
  `tests/test_l1_new_vis.py` (`TestCryogenLevelMeterVI`),
  `tests/test_l1_virtual_instruments.py`.
- `__init__.py` — package marker. tests: none.
