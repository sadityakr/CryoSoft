---
name: write-measurement-vi
description: Author a new measurement Virtual Instrument (VI) for CryoSoft - the driver contract, the MeasurementInstrumentBase self-description and lifecycle, how @monitored/@control map onto the monitor card and instrument front panel, how to declare a loop1/loop2-loopable parameter (reading_setters), and how to bench-test the real instrument safely. Use when the user asks to add support for a new measurement instrument, write a measurement VI, or wire a new instrument into a procedure's reading loop.
---

# write-measurement-vi — authoring a measurement Virtual Instrument

Goal: a new instrument goes from "vendor library" to "selectable in a
procedure, with a working reading loop, verified against real hardware" with
minimal new code and zero changes to the core (CLAUDE.md's "standards over
one-off code" principle). This skill is the measurement-method-specific
walkthrough of that standard; `cryosoft/virtual_instruments/measurement/README.md`
and `MeasurementInstrumentBase`'s own docstring in
`cryosoft/virtual_instruments/base.py` are the enforced, canonical text —
this skill exists to connect that contract to the GUI-visible behavior it
produces, which the docstrings don't show.

Copy `references/measurement_vi_template.py` as your starting point — it has
every section below stubbed out with `TODO`s in place.

Reference implementations to read alongside this skill:
- `cryosoft/virtual_instruments/measurement/dc_separate_measurement.py`
  (`DCSeparateMeasurementVI`) — simplest two-driver case, with a working
  `reading_setters` example.
- `cryosoft/virtual_instruments/measurement/tensormeter_rtm2_measurement.py`
  (`TensormeterRTM2MeasurementVI`) — single-driver case over a non-VISA (TCP)
  instrument; richer `control_limits`/config-owned-route handling.
- `cryosoft/virtual_instruments/switch/switch_matrix.py` (`SwitchMatrixVI`) —
  not a measurement VI, but the canonical example of a **dynamic**,
  config-owned loopable parameter (`reading_parameters` built at runtime).

## 1. The driver comes first (L0)

Build and test the driver + sim twin before writing any VI code
(CLAUDE.md: "build bottom-up, test at every layer"). The three-rule
contract, mechanically enforced by `tests/test_conformance.py`:

1. One public class per module, `__init__(self, resource_string: str)` —
   exactly one required argument. This is a VISA string (`"GPIB0::19::INSTR"`)
   for most instruments; a non-VISA instrument (TCP, USB-serial-with-its-own-
   protocol, ...) may use a different string convention (e.g. `"host:port"`)
   as long as the driver's module docstring says so explicitly — see
   `tensormeter_rtm2.py`'s module docstring for the precedent.
2. `get_idn() -> str` with no required arguments — the universal reachability
   probe used by the troubleshoot preflight and this VI's own `ping()`.
3. A `sim_<name>.py` twin exposing the **identical public method names and
   parameter names** (not necessarily identical defaults/types — the
   conformance parity check only compares `list(signature.parameters)`).
   The sim twin has **zero third-party dependencies**, must not import the
   real driver module (so it still constructs if the real vendor package is
   missing), and models the instrument's actual failure modes via a private
   `_simulate_error` (or similarly-named) test hook.

**Gotcha that breaks `test_driver_module_contract`:** a driver module must
define **exactly one public class**. If you need a small structured return
type (e.g. a decoded data row), do NOT add a second `class`/`@dataclass` at
module level — either prefix it `_Private` (excluded from the "public
classes" scan) or, simpler, just return a plain `dict[str, float]`/`list`.
Same reasoning for module-level constants: plain dicts/tuples are fine
(the conformance scan only inspects classes and functions), enum-as-class
is not.

## 2. The VI's self-description (what makes it "a measurement method")

Subclass `MeasurementInstrumentBase` (or `DCMeasurementBase` if your shape is
exactly "N samples of voltage_V + current_A" — most new instruments are not,
so subclass the base directly). Declare, as `ClassVar`s:

```python
_ARRAY_KEYS, _SCALAR_COLUMNS = MeasurementInstrumentBase.quantity_columns(
    "res_a_ohm", "res_b_ohm"          # your quantity names, no "_array"/"_error" suffix
)
measurement_data_keys: ClassVar[list[str]] = _ARRAY_KEYS
measurement_scalar_columns: ClassVar[dict[str, str]] = _SCALAR_COLUMNS
measurement_parameters: ClassVar[dict[str, ParamSpec]] = { ... }   # your GUI-facing knobs
selector_label: ClassVar[str] = "Short GUI drop-down name"          # optional
```

**Always derive `measurement_data_keys`/`measurement_scalar_columns` from
`quantity_columns()`, never hand-write the `_array`/`_error` suffixes** — the
mean/error/array convention is machine-checked
(`test_measurement_vi_mean_error_array_convention`) and `quantity_columns()`
is the one place that gets it right.

`measurement_parameters` must be **non-empty** — even a single-knob
instrument needs at least one `ParamSpec`. `control_limits`-bound parameters
need a value in `self._limits` populated in `__init__` **even when the
config passes no `init_params` at all** — conformance's
`test_measurement_vi_round_trip` builds every measurement VI as
`vi_cls(sim_drivers)`, no `init_params`, so give every limit a safe internal
default (`init_params.get("max_current_A", 0.01)`), never require it.

## 3. Lifecycle methods — what actually runs, when

| Method | Called by | Must NOT do |
|---|---|---|
| `data_arrays(params) -> dict[str, int]` | Procedure, before arming (to size the HDF5 layout) | Touch hardware |
| `initiate_measurement(**params) -> None` | Procedure's `initiate()`, or an explicit front-panel action | — this IS the arming step |
| `initiate() -> None` | Bulk "Initiate All", the card's lifecycle toggle | **Never arm, never source.** Inherited from the base as a harmless `ping()`-based connection check — do not override it to call `initiate_measurement()` |
| `take_reading() -> dict` | Once per sweep point, no arguments | Assume anything not fixed at `initiate_measurement()`. NaN-pad every array to the length `data_arrays()` declared for the same params; compute `self.mean_and_sem()` only over the valid (non-NaN) samples |
| `standby() -> None` | End of run, abort, or explicit action | Leave a source energised |
| `ping() -> bool` | `initiate()`, GUI reachability checks | Poll anything expensive — should be near-instant |

`initiate_measurement` is normally `@control(panel=False)` — arming is a
deliberate act, reachable from the front panel and procedures, never from
the compact monitor card.

## 4. GUI surface: monitor card vs. instrument front panel

Both are driven by the SAME decorator metadata (`cryosoft/gui/instrument_panel.py`)
— the difference is which controls are shown, not how they render:

- **`@monitored`** methods (no arguments) become live-value rows, polled every
  tick into `get_state()`, on **both** the compact monitor card and the front
  panel. **A VI with zero `@monitored` methods shows nothing but a name and
  status header on its card** — if you want a live number visible between
  runs (last reading, raw driver getter, connection state), add one.
- **`@control`** methods become a button + input row. `panel=True` (the
  default) puts it on the compact monitor card; `panel=False` keeps it
  front-panel-only. The **front panel window shows every control regardless
  of `panel=`** (`instrument_front_panel.py` overrides the card's own
  filtering) — so `panel=False` only hides something from the *compact*
  card, never from the full instrument window.
- A setup's `monitor.yaml` `panels:` block is a per-VI **allowlist override**
  of which `panel=False` controls also show on that VI's card — it does not
  gate whether the card exists at all (every registered measurement VI gets
  one automatically).
- Each `@control` parameter renders from its `ParamSpec` (`type`, `default`,
  `unit`, `description`, and either `min`/`max` OR `choices` — mutually
  exclusive). A `str` param with `choices` renders as a dropdown; without
  `choices` it's a free-text box with no validation beyond type coercion.
- **If a control's valid values only exist after construction** (e.g. a
  route/channel name from `init_params`, not known at class-definition time),
  do not try to bake them into the decorator — override
  `control_param_specs(method_name)` to inject a dynamic `ParamSpec` with
  `choices` built from your instance state. See `SwitchMatrixVI.control_param_specs()`
  for the pattern (`select_route`'s `route` param rendered from
  `self._routes`).
- A measurement VI's `measurement_parameters` render the same way in a
  **procedure's** parameter form (see §5) — but that path has no
  `control_param_specs`-style dynamic-choices hook today, so a
  config-dependent measurement parameter (e.g. "which configured channel
  sequence") currently has to be a plain free-text `str` there, matched
  against your own route table inside `initiate_measurement()` (raise
  `ValueError` naming the valid options if it doesn't match — see
  `TensormeterRTM2MeasurementVI.initiate_measurement()`).

## 5. Selecting the method and looping channels in a procedure

No extra code is needed for a new measurement VI to be *selectable*: the
generic sweep procedure lists every VI returned by
`station.measurement_vi_names()` in its "Method" drop-down
(`station.get_param_groups()`/`gui/procedure_params_panel.py`), and renders
your `measurement_parameters` as the form group beneath it the moment the VI
is registered in `devices.yaml`.

**Looping is opt-in and easy to forget.** A `measurement_parameters` entry
is *not* automatically loopable across sweep points — that requires a
second declaration:

```python
reading_setters: ClassVar[dict[str, str]] = {"current_A": "set_source_current"}
```

mapping a `measurement_parameters` name to a **dedicated setter method**
(itself normally `@control`-decorated, with `control_limits` if bounded)
that reprograms *just* that value between readings, without re-running the
whole `initiate_measurement()` arm sequence. See
`DCSeparateMeasurementVI.set_source_current()` for the simple, static-choice
case, and `SwitchMatrixVI.reading_setters = {"route": "select_route"}` +
its `reading_parameters` property override for the dynamic, config-owned
case (route names only exist after `__init__` reads `init_params["routes"]`).

Without a `reading_setters` entry, the parameter is **invisible to the
Reading-loop panel** — the whole run is locked to one value for it. This is
the single easiest thing to forget when porting a multi-channel instrument:
declaring the parameter in `measurement_parameters` is necessary but not
sufficient for it to be loop1/loop2-loopable.

Contract, machine-enforced by conformance:
- every `reading_setters` key names a real `measurement_parameters` entry;
- every value names a real method whose signature accepts the parameter
  under its own name;
- the setter must reconfigure the reading only — `take_reading()` must still
  return exactly `measurement_data_keys` at the lengths `data_arrays()`
  declared, after any setter call.

Optionally declare `reading_safe_off: ClassVar[str] = "method_name"` — a
safe-off action (e.g. `open_all` on a switch) the procedure dispatches at
standby/abort if this VI's parameter took part in the loop.

## 6. Wiring into a config

`devices.yaml`:
```yaml
real_drivers:
  my_instrument:
    class: cryosoft.drivers.my_instrument.MyInstrument
    address: "GPIB0::7::INSTR"     # or host:port, etc. — match the driver's own convention
    expect_idn: "..."              # only after step 8 below has shown the REAL string; never invent one

virtual_instruments:
  my_measurement:
    class: cryosoft.virtual_instruments.measurement.my_instrument_measurement.MyInstrumentMeasurementVI
    drivers: {main: my_instrument}   # role name -> driver alias; see the role-collision note below
    vi_type: measurement
    init_params: {...}
```

**Role-name collision trap** when registering your sim driver in
`tests/test_conformance.py`'s `_SIM_MEASUREMENT_DRIVER_CLASSES` (needed so
the generic round-trip conformance test can build your VI): pick a role key
that isn't already claimed by an unrelated sim class (`source`, `meter`,
`main`, `lockin`, `tensormeter`, ...) unless your driver is genuinely
API-compatible with whatever that existing role maps to. Reusing an
unrelated role silently hands your VI the wrong sim driver instance —
`main` is already `SimKeithley2400`, for example.

## 7. Testing the real instrument — do this before calling it done

A driver/VI that only passes against its own sim twin has not been
validated against the instrument it claims to support. Live commissioning
regularly surfaces things no amount of code review catches — see
`cryosoft/configs/12t-cryo/setup.md`'s Tensormeter RTM2 entry for a worked
example of six real, non-obvious bugs (a vendor-library command-table typo,
a hidden precondition, a CV/CC-style stale-setpoint interaction, an
AC/DC column mismatch, a leftover session mode, and a trigger/averaging
race) that only live testing found. Follow this order, every time:

1. **`make check` green with sim only, first.** Never start live testing
   from a codebase with a known-broken test.
2. **Test the raw driver directly**, in a throwaway script, before wrapping
   it in the VI. `driver = MyInstrument("<real address>")`; call getters,
   then the smallest possible setter, one at a time. This isolates driver
   bugs from VI-logic bugs — conflating them wastes far more time than it
   saves.
3. **Read the vendor's actual protocol/command reference before guessing at
   semantics from a method's name.** "Current setpoint" methods that turn
   out to interact with a separate "voltage setpoint" in a CV/CC-style
   regulation loop, or a "trigger" command that aborts rather than adds to
   an in-progress measurement, are exactly the kind of behavior a plausible
   English name will not tell you.
4. **Start at the safest possible excitation level** (this setup's usual
   floor: nA-scale current, mK-scale temperature moves — see
   `setup-supervisor/references/safe-testing.md`) and only increase after
   the response scales the way physics predicts. Never jump straight to a
   "real" test value.
5. **Wrap every live call in `try`/`finally` that zeros/safes the output.**
   One left-on source across a script crash is exactly the incident this
   discipline exists to prevent.
6. **Check the instrument's actual last-known state before trusting a
   "fresh" assumption.** Instruments remember settings across sessions
   (stale setpoints, a leftover mode) that silently interact with today's
   test. If a reading doesn't match physical expectation, suspect leftover
   state before suspecting new code — dump every cached setting you can
   read and look for anything nonzero/non-default that shouldn't be.
7. **Verify a single raw sample before trusting an averaged one.** Averaging
   a wrong single-sample reading over `readings_per_point` just produces a
   wrong average with a falsely small error bar — confirm one reading is
   physically sane first.
8. **Then test the shipped VI class itself, end-to-end**, exactly as a
   procedure would call it — `vi.initiate_measurement(**your_test_values)`
   then `vi.take_reading()` — in a real script against real hardware. This
   is the only step that catches VI-layer bugs a driver-only test can't see
   (wrong column read for the sourcing mode actually used, a wiring/mode
   mismatch, etc.).
9. **Record every finding in `setup.md`'s Known Quirks section and in
   `LOGBOOK.md`**, even ones fixed the same session — these are exactly the
   facts a future agent (or you, next week) cannot re-derive from the code
   alone.
10. **Never leave a config with a guessed address or setpoint.** Use the
    vendor's own discovery mechanism if one exists, and record the
    confirmed value with a dated comment explaining how it was found.

## Verification checklist before calling a new measurement VI done

- [ ] `pytest -m "not hardware" tests/test_l0_simulated.py -k <YourSim> -v`
- [ ] `pytest -m "not hardware" tests/test_conformance.py -v` (driver contract,
      sim/real parity, VI contract, measurement self-description,
      mean/error/array convention, round-trip, `reading_setters` contract if
      declared)
- [ ] `make check` (ruff + import-linter contracts + full non-hardware suite)
      fully green
- [ ] Live: driver-only test, then VI-level test, both against real hardware,
      both logged in `setup.md` + `LOGBOOK.md`
- [ ] GUI smoke: open the front panel for this VI, confirm every declared
      `@control` renders sensibly (including the ones you expect to be
      hidden from the compact card) and every `@monitored` value updates
- [ ] If loopable channels/parameters were requested: confirm the parameter
      actually appears in the procedure's Reading-loop panel, not just in
      its own parameter form
